"""The only real-state Fork launcher of the driver: anvil forked through ``fork_mode.ForkBudgetProxy``. Test-only.

``launch(auth, ledger, cwd)`` starts the budget proxy for one loaded fork authorization, then a local anvil whose
fork URL is that proxy's 127.0.0.1 address and nothing else, at the authorization's bound fork block. Before the node
is returned it must report the authorization's chain id and, at the fork block, the bound block hash; otherwise it is
stopped (``FORK_CHAIN_MISMATCH`` / ``FORK_ANCHOR_MISMATCH``). The controls are the proxy's:

- a public upstream needs an approved v2 S3 authorization whose fork block and hash were bound by
  ``fr_d1_scope.bind_fork_anchor`` to the S1/S2 ledgers and an accepted reconciliation (``fr_d1_approvals``);
  without it the proxy refuses before any request (``FR_D1_NOT_AUTHORIZED`` / ``FORK_ANCHOR_NOT_BOUND``);
- every upstream POST is a read method of the authorization, at the bound block, reserved in the persistent
  budget ledger before it is forwarded, paced, and refused at the ceiling;
- an authorization that lists any write method is refused here (``FORK_WRITE_METHOD``), so a transaction can only
  ever execute on the local node: anvil keeps senders impersonated locally and sends nothing upstream.

A public run is additionally confined (``require_isolation``): the working directory must be an approved export
below ``APPROVED_EXPORT_PARENT`` (``fork_mode.require_export_root``: an export manifest, no ``.git``, no symlink),
and the ledger must live in that export's ``cache_forge/rehearsal/fork-campaign/fork/`` area. A repository checkout
or a path reached through a symbolic link is refused before the proxy exists.

Every failure after a process was started stops what the launcher started: the anvil process (terminated, then
killed after a bounded wait) and the proxy server. ``preflight`` checks, per chain and anchor, the exact gates a
scenario depends on; ``code_identities`` reads runtime code on the Fork for a reviewer (FR-X8). Neither approves
anything. The isolation rule (``test_driver.IsolationTest``) admits a fork flag in this file only, and only with the
proxy's loopback URL.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import anvil  # noqa: E402
import campaign_input as ci  # noqa: E402
import fork_mode as fm  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import history as h  # noqa: E402
import owner_config as oc  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib.keccak import keccak256_bytes  # noqa: E402

WRITE_METHODS = frozenset({'eth_sendRawTransaction', 'eth_sendTransaction', 'eth_sign', 'eth_signTransaction',
                           'personal_sign', 'eth_signTypedData_v4'})
LOOPBACK_PREFIX = 'http://127.0.0.1:'
APPROVED_EXPORT_PARENT = Path('/Volumes/project/tmp')
FORK_AREA = 'fork'
STARTUP_S = 120


def _stop(code, detail='', status='REFUSED'):
    return CollectionStop(code, detail, status)


def require_isolation(cwd, ledger, parent=APPROVED_EXPORT_PARENT):
    """A public run executes only inside an approved export below ``parent`` with its ledger in the Fork area."""
    if ledger is None:
        raise _stop('FORK_ISOLATION', 'a public run needs its persistent budget ledger')
    cwd = Path(cwd)
    if not cwd.is_absolute() or cwd.resolve() != cwd.absolute() or any(p.is_symlink() for p in (cwd, *cwd.parents)):
        raise _stop('FORK_ISOLATION', f'{cwd} is not a symlink-free absolute path')
    export = fm.require_export_root(Path(ledger.directory).absolute(), FORK_AREA)       # manifest, no .git, no symlink
    if export.resolve() != cwd.resolve():
        raise _stop('FORK_ISOLATION', f'the working directory {cwd} is not the ledger export {export}')
    base = Path(parent).resolve()
    if base not in export.resolve().parents:
        raise _stop('FORK_ISOLATION', f'{export} is not below {base}')
    return export


class ForkNode(anvil.LoopbackNode):
    """anvil forked through a running ``ForkBudgetProxy``; the same read / send helpers as ``anvil.LoopbackNode``.

    ``anvil.LoopbackNode.__init__`` refuses any fork URL and stays unchanged; this class starts its own process and
    terminates it on every startup failure."""

    def __init__(self, proxy, cwd, *, startup_s=STARTUP_S):
        if not proxy.url.startswith(LOOPBACK_PREFIX) or not proxy.thread.is_alive():
            raise _stop('FORK_PROXY', f'{proxy.url!r} is not a running loopback budget proxy')
        auth = proxy.auth
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        argv = ['anvil', '--accounts', '0', '--quiet', '--host', '127.0.0.1', '--port', str(self.port),
                '--fork-url', proxy.url, '--fork-block-number', str(auth['forkBlock']), '--no-storage-caching',
                '--retries', '0', '--fork-retry-backoff', '1000', '--timeout', '30000', '--hardfork', 'cancun',
                '--gas-limit', '1000000000', '--block-base-fee-per-gas', '0', '--gas-price', '0',
                '--disable-code-size-limit', '--no-mining']
        self.process = subprocess.Popen(argv, cwd=cwd, env={k: os.environ[k] for k in ('PATH', 'HOME') if k in os.environ},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f'http://127.0.0.1:{self.port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.counter = 0
        try:
            self._start(proxy, auth, startup_s)
        except BaseException:
            self.close()
            raise

    def _start(self, proxy, auth, startup_s):
        deadline = time.monotonic() + startup_s
        while True:
            if self.process.poll() is not None:
                raise _stop('FORK_NODE_EXITED', f'anvil exited (proxy refused: {proxy.refused[-1:]})', 'STOP')
            if proxy.stopped is not None:
                raise _stop('FORK_PROXY_STOPPED', f'{proxy.stopped.code}: {proxy.stopped.detail[:200]}', 'STOP')
            try:
                self.chain_id = int(self.rpc('eth_chainId', []), 16)
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise _stop('FORK_NODE_TIMEOUT', 'anvil did not answer', 'STOP') from None
                time.sleep(0.05)
        try:
            block = self.rpc('eth_getBlockByNumber', [hex(auth['forkBlock']), False])
        except (OSError, ValueError) as error:
            raise _stop('FORK_ANCHOR_MISMATCH', f'fork block unreadable: {error}', 'STOP') from None
        self.timestamp = int(block['timestamp'], 16) if isinstance(block, dict) else 0
        self.fork_block = block

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def launch(auth, ledger, cwd):
    """(proxy, node) for a loaded fork authorization and its open ledger. The caller closes both (``node.close()``,
    then ``proxy.__exit__``); ``run`` does it. Nothing the launcher started survives a failure."""
    if WRITE_METHODS & set(auth['methods']):
        raise _stop('FORK_WRITE_METHOD', f'{sorted(WRITE_METHODS & set(auth["methods"]))}')
    if auth['mode'] != 'SIMULATION':
        require_isolation(cwd, ledger)
    proxy = fm.ForkBudgetProxy(auth, ledger)          # refuses an unapproved or unbound public upstream here
    proxy.__enter__()
    node = None
    try:
        node = ForkNode(proxy, cwd, startup_s=STARTUP_S)
        expected = fm.CHAIN_IDS[auth['chain']]
        if node.chain_id != expected:
            raise _stop('FORK_CHAIN_MISMATCH', f'node chain id {node.chain_id}, authorization {expected}', 'STOP')
        if not isinstance(node.fork_block, dict) or node.fork_block.get('hash') != auth['forkBlockHash'] \
                or int(node.fork_block.get('number', '0x0'), 16) != auth['forkBlock']:
            raise _stop('FORK_ANCHOR_MISMATCH', f'fork block {auth["forkBlock"]} does not carry the bound hash', 'STOP')
    except BaseException:
        if node is not None:
            node.close()
        proxy.__exit__(None, None, None)
        raise
    return proxy, node


def run(auth, ledger, cwd, flow):
    """``flow(node)`` on a launched Fork; returns the flow result and the proxy's accounting."""
    proxy, node = launch(auth, ledger, cwd)
    try:
        result = flow(node)
    finally:
        node.close()
        proxy.__exit__(None, None, None)
    return {'result': result, 'forwardedPosts': proxy.forwarded, 'ledgerUsed': ledger.used,
            'methods': dict(sorted(proxy.methods_seen.items())), 'refused': proxy.refused,
            'stopped': None if proxy.stopped is None else proxy.stopped.code}


def code_identities(node, block, expected):
    """FR-X8 preflight reads on a launched Fork: ``expected`` = {address: official runtime keccak} from an
    independent source (the candidate manifest). Returns one row per address; MATCH only for equal keccak of
    non-empty code at ``block``. The result is evidence for a reviewer, never an approval."""
    rows = []
    for address, official in sorted(expected.items()):
        code = node.rpc('eth_getCode', [address, hex(block)])
        digest = '0x' + keccak256_bytes(bytes.fromhex(code[2:])).hex() if isinstance(code, str) else None
        rows.append({'address': address, 'official': official.lower(), 'observed': digest,
                     'codeBytes': (len(code) - 2) // 2 if isinstance(code, str) else None,
                     'result': 'MATCH' if code not in (None, '0x') and digest == official.lower() else 'MISMATCH'})
    return rows


# ------------------------------------------------------------------------------------------------ preflight
def _read(path, sha256):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def reconciliation_gate(chain, auth, reconciliation):
    """PRESENT only for the pinned reconciliation that names exactly this chain, this S3 authorization's S1/S2
    records and the authorization's bound fork block and hash."""
    path, sha = reconciliation if reconciliation else (None, None)
    doc = _read(path, sha) if path else None
    binding = auth.get('anchorBinding') or {}
    policy = auth.get('scope', {}).get('forkAnchor', {})
    problems = []
    if auth.get('chain') != chain or fm.CHAIN_IDS.get(chain) != h.CHAINS.get(chain):
        problems.append('authorization chain')
    if sha not in approvals.RECONCILIATIONS:
        problems.append('reconciliation not pinned')
    if binding.get('reconciliationSha256') != sha:
        problems.append('the fork anchor was bound by another reconciliation')
    if doc is None:
        problems.append('reconciliation record missing or not the named bytes')
    elif [doc.get('chain'), doc.get('chainId'), doc.get('anchorBlock'), doc.get('anchorHash'),
          doc.get('s1AuthorizationSha256'), doc.get('s2AuthorizationSha256'), doc.get('specSha256'),
          doc.get('verdict'), doc.get('reviewer')] != [
            chain, h.CHAINS.get(chain), auth.get('forkBlock'), auth.get('forkBlockHash'),
            policy.get('s1AuthorizationSha256'), policy.get('s2AuthorizationSha256'), binding.get('specSha256'),
            'RECONCILED', 'Agent A']:
        problems.append('the reconciliation names another chain, anchor, evidence or verdict')
    return {'gate': 'S1/S2 reconciliation for this chain and anchor', 'state': 'BLOCKED' if problems else 'PRESENT',
            'problems': problems, 'reconciliationSha256': sha}


def safe_manifest_gate(auth, manifest):
    """PRESENT only for a pinned Safe identity manifest of this chain's GOV Safe, observed at this fork anchor,
    whose expected singleton and MultiSendCallOnly come from an official registry entry for chain 1672."""
    path, sha = manifest if manifest else (None, None)
    doc = _read(path, sha) if path else None
    problems = []
    if auth.get('chain') != 'mainnet':
        problems.append('the GOV Safe path exists on mainnet only')
    if sha not in ci.APPROVED_SAFE_IDENTITY_MANIFESTS:
        problems.append('Safe identity manifest not approved (FR-X8)')
    if doc is None:
        problems.append('manifest missing or not the named bytes')
    else:
        registry = doc.get('officialRegistry', {})
        if [doc.get('chainId'), doc.get('safe'), doc.get('block'), doc.get('blockHash')] != [
                1672, h.GOV_SAFE, auth.get('forkBlock'), auth.get('forkBlockHash')]:
            problems.append('the manifest names another chain, Safe or anchor')
        if registry.get('safe_l2', {}).get('networkEntry') != 'canonical' or \
                registry.get('multi_send_call_only', {}).get('networkEntry') != 'canonical':
            problems.append('the manifest is not bound to official canonical entries for chain 1672')
    return {'gate': 'FR-X8 Safe identity manifest for this Safe and anchor', 'state': 'BLOCKED' if problems else 'PRESENT',
            'problems': problems, 'manifestSha256': sha, 'affects': 'every mainnet H/U case'}


def preflight(chain, *, auth=None, reconciliation=None, manifest=None, rows=None, observed=None):
    """The exact gates a real-state run of ``chain`` depends on, as data. A gate approved for another chain,
    anchor, Safe or reconciliation is never reused. Nothing is approved here."""
    auth = auth or {}
    gates = [reconciliation_gate(chain, auth, reconciliation)]
    if chain == 'mainnet':
        gates.append(safe_manifest_gate(auth, manifest))
        return gates
    anchor = {'block': auth.get('forkBlock'), 'hash': auth.get('forkBlockHash')}
    results = oc.check(chain, anchor, rows or [], observed or {}) if auth else []
    for entry in oc.CHANGES:
        if entry['chain'] != chain:
            continue
        r = next((x for x in results if x['id'] == entry['id']), None)
        usable = r is not None and r['result'] == 'MATCH' and entry['standing'] != 'OWNER_DECISION_PENDING'
        gates.append({'gate': f'{entry["role"]} expectation ({entry["standing"]})',
                      'state': 'PRESENT' if usable else ('BLOCKED' if r is None or r['result'] == 'MATCH' else 'FAIL'),
                      'check': r, 'affects': 'assertR4_t and every testnet U_t case'})
    return gates
