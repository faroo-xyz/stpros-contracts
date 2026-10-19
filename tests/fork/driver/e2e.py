"""Post-upgrade user and operations E2E harness (UPGRADE-RUNBOOK §6 of the V4 readiness package). Test-only.

The harness runs on the node of a completed upgrade, at the post-upgrade snapshot ``S_post``. The same code runs
on two kinds of node:
- the synthetic loopback rehearsal (``execution.testnet`` / ``execution.mainnet`` with ``then=hook()``);
- a real-state local Fork, launched by ``fork_launcher`` under its FR-D1 authorization. The harness itself never
  opens a node or an RPC endpoint.

What it does:
1. Loads the frozen entry specification (``e2e_spec.json``, derived from the approved ABI inventory) and verifies it
   against the canonical artifact ABIs (``cache/native-r4``). The check covers completeness, selectors and artifact
   hashes. Any problem makes the run FAIL.
2. Validates the post-upgrade context: chain, anchor, source, addresses, actors, postcheck, history, governance
   route. Missing context is BLOCKED. Synthetic context can never yield a formal PASS.
3. Runs every scenario case from a fresh derivation of ``S_post``, then restores it and verifies the restoration
   by a state fingerprint. A failed restoration FAILs the run and BLOCKs the remaining cases.
4. For every operation, records:
   - the case, entry signature and selector, caller and caller class, and node origin;
   - the transaction hash, status and gas;
   - the expected and actual outcome, with exact error matching on the revert selector;
   - the decoded events;
   - native, WPROS, stPROS and vault-share deltas;
   - the invariants before and after.

   An expected revert passes only when the error matches **and** nothing changed.
5. Computes per-entry coverage against the caller policy, and the verdicts:
   - execution: PASS or FAIL;
   - formal gate: PASS, FAIL or BLOCKED.

   Then it writes machine-readable JSON and a Markdown summary. Only a REAL context may write
   ``POST-UPGRADE-USER-E2E.md``.
"""
import ast
import contextlib
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
import approved_inputs  # noqa: E402

SCHEMA = 'faroo-post-upgrade-e2e-v1'
# Integration contracts outside the V4 scope (RewardsVault, staking proxies): events the harness decodes.
INTEGRATION_EVENTS = ('TransferredBack(address,uint256)', 'EthDeposited(address,uint256)', 'OracleNotified(address,uint256)',
                      'SlpPharosProxySet(address,address)')
SPEC_SCHEMA = 'faroo-e2e-spec-v1'
SPEC_PATH = HERE / 'e2e_spec.json'
CANONICAL = ROOT / 'cache/native-r4'
CONTRACTS = ('StPROS', 'YieldVault', 'YieldVaultFactory', 'Oracle', 'BridgeVault', 'SlpDistributor')
EXTRA_ABIS = ('TimelockController',)
POLICIES = ('PERMISSIONLESS', 'SELF_SCOPED', 'ROLE_RESTRICTED', 'CALLER_RESTRICTED', 'OWNER_ONLY', 'OWNER_OR_GUARDIAN',
            'DISABLED', 'INITIALIZER', 'MIGRATION', 'INTERNAL_HOOK', 'READ')
RESTRICTED = ('ROLE_RESTRICTED', 'CALLER_RESTRICTED', 'OWNER_ONLY', 'OWNER_OR_GUARDIAN', 'DISABLED', 'INITIALIZER',
              'MIGRATION', 'INTERNAL_HOOK')
REAL, CONSTRUCTED, SYNTHETIC = 'REAL', 'CONSTRUCTED', 'SYNTHETIC'
PASS, FAIL, BLOCKED, NOT_RUN, NEEDS_REVIEW, NOT_APPLICABLE = ('PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'NEEDS_REVIEW',
                                                              'NOT_APPLICABLE')
SEVERITY = {NOT_APPLICABLE: -1, PASS: 0, NOT_RUN: 1, NEEDS_REVIEW: 2, BLOCKED: 3, FAIL: 4}
# Mandatory REAL scenarios (UPGRADE-RUNBOOK §6.2) and the blocking acceptance sets (§6.6).
MANDATORY_REAL = ('A01', 'A05', 'A08', 'B07', 'B09', 'C01', 'C02', 'C05')
# Mainnet: the governed vault must be SETTLED at U (GC-01), so a REAL forced par (B07) is unreachable there. B07 runs
# CONSTRUCTED on mainnet. This chain-specific reading of §6.2 is flagged for review (COVERAGE-AND-GAPS).
MANDATORY_REAL_MAINNET = ('A01', 'A05', 'A08', 'B09', 'C01', 'C02', 'C05')
USER_PATHS = ('A01', 'A02', 'A04', 'A05', 'A06', 'B01', 'B06', 'B09')
OPS_PATHS = ('C01', 'C02', 'C03', 'C04', 'C05', 'C06')
PRIVILEGED = ('gov', 'can', 'guardian', 'ops', 'keeper', 'reporter', 'treasury', 'e1', 'e2', 'deployer')
ZERO = '0x' + '00' * 20
UINT_MAX = 2 ** 256 - 1


class E2EError(RuntimeError):
    """A harness precondition or integrity rule failed."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _lower(a):
    return a.lower() if isinstance(a, str) else a


# ============================================================================================ specification
_ERR = re.compile(r'reverts? (?:with )?([A-Z][A-Za-z0-9]+)')


def unauthorized_error(text):
    """The first named error of a spec's unauthorized-caller expectation (None for NOT APPLICABLE)."""
    if text.startswith('NOT APPLICABLE'):
        return None
    m = _ERR.search(text)
    return m.group(1) if m else None


SPEC_KEYS = ('contract', 'signature', 'selector', 'mutability', 'policy', 'class', 'authorizedCallers',
             'unauthorizedCaller', 'stateConditions', 'success', 'failures', 'status', 'statusReason')


def derive_spec(inventory, inventory_sha256):
    """The harness specification derived from the approved inventory (``faroo-v4-e2e-abi-inventory-v2``)."""
    if inventory.get('schema') != 'faroo-v4-e2e-abi-inventory-v2':
        raise E2EError('not an approved E2E ABI inventory (v2)')
    entries = []
    for e in inventory['entries']:
        d = {k: e[k] for k in SPEC_KEYS}
        d['unauthorizedError'] = unauthorized_error(e['unauthorizedCaller'])
        d['effective'] = f"{e['effective']['contract']} ({e['effective']['source']}:{e['effective']['line']})"
        entries.append(d)
    entries.sort(key=lambda d: (CONTRACTS.index(d['contract']), d['signature']))
    return {'schema': SPEC_SCHEMA, 'derivedFrom': {'schema': inventory['schema'], 'sha256': inventory_sha256},
            'artifactSha256': inventory['artifactSha256'], 'entries': entries}


def load_spec(path=SPEC_PATH):
    spec = json.loads(Path(path).read_text())
    if spec.get('schema') != SPEC_SCHEMA:
        raise E2EError('E2E spec schema')
    return spec


def artifact_entries(canonical=CANONICAL):
    """{(contract, signature): selector or None} from the canonical artifact ABIs, plus their file hashes."""
    out, hashes = {}, {}
    for c in CONTRACTS:
        raw = (Path(canonical) / c / 'artifact.json').read_bytes()
        hashes[c] = _sha(raw)
        for item in json.loads(raw)['abi']:
            if item['type'] == 'receive':
                out[(c, 'receive()')] = None
            elif item['type'] == 'function':
                sig = abi.item_signature(item)
                out[(c, sig)] = keccak256(sig.encode())[:10]
    return out, hashes


def verify_spec(spec, canonical=CANONICAL):
    """Problems between the spec and the canonical ABIs (empty list = consistent)."""
    problems = []
    actual, hashes = artifact_entries(canonical)
    if hashes != spec['artifactSha256']:
        problems.append(f'artifact hashes differ from the approved inventory: {sorted(k for k in hashes if hashes[k] != spec["artifactSha256"].get(k))}')
    listed = {}
    for e in spec['entries']:
        key = (e['contract'], e['signature'])
        if key in listed:
            problems.append(f'duplicate spec entry {key}')
        listed[key] = e['selector']
        if e['policy'] not in POLICIES:
            problems.append(f'unknown policy {e["policy"]} for {key}')
    for key in sorted(set(actual) - set(listed)):
        problems.append(f'ABI entry missing from the spec: {key[0]}.{key[1]}')
    for key in sorted(set(listed) - set(actual)):
        problems.append(f'spec entry not in the ABI: {key[0]}.{key[1]}')
    for key in sorted(set(actual) & set(listed)):
        if actual[key] != listed[key]:
            problems.append(f'selector mismatch {key[0]}.{key[1]}: spec {listed[key]} ABI {actual[key]}')
    writes = sum(1 for e in spec['entries'] if e['mutability'] not in ('view', 'pure'))
    if (writes, len(spec['entries']) - writes) != (112, 157):
        problems.append(f'entry totals {writes}/{len(spec["entries"]) - writes}, expected 112/157')
    return problems


# ============================================================================================ context
CONTEXT_FIELDS = {
    'chainId': int, 'evidence': str, 'anchor': dict, 'source': dict, 'addresses': dict, 'actors': dict,
    'actorClasses': dict, 'postcheck': dict, 'history': dict, 'governance': dict, 'formal': dict, 'integrations': dict,
}
ADDRESS_KEYS = ('stpros', 'wpros', 'oracle', 'bridge', 'distributor', 'factory', 'timelock', 'proxyAdmin')
ACTOR_KEYS = ('gov', 'can', 'guardian', 'ops', 'keeper', 'reporter', 'treasury', 'e1', 'e2')


def context_problems(ctx):
    """Missing or malformed context (each one BLOCKS the run)."""
    out = []
    for key, kind in CONTEXT_FIELDS.items():
        if not isinstance(ctx.get(key), kind):
            out.append(f'context field {key} missing or not {kind.__name__}')
    if out:
        return out
    if ctx['evidence'] not in (REAL, SYNTHETIC):
        out.append('evidence must be REAL or SYNTHETIC')
    for k in ADDRESS_KEYS:
        if not re.fullmatch(r'0x[0-9a-f]{40}', str(ctx['addresses'].get(k, ''))):
            out.append(f'address {k} missing')
    for k in ('governedVaults', 'retiredVaults'):
        if not isinstance(ctx['addresses'].get(k), list):
            out.append(f'address list {k} missing')
    for k in ACTOR_KEYS:
        if not re.fullmatch(r'0x[0-9a-f]{40}', str(ctx['actors'].get(k, ''))):
            out.append(f'actor {k} missing')
    if not ctx['anchor'].get('block') or not ctx['anchor'].get('hash'):
        out.append('anchor block/hash missing')
    if not ctx['source'].get('commit') or not ctx['source'].get('artifactSha256'):
        out.append('source commit / artifact hashes missing')
    if ctx.get('recordMismatch'):
        out.append(ctx['recordMismatch'])
    if ctx['postcheck'].get('verdict') != 'PASS':
        out.append(f'upgrade postcheck {ctx["postcheck"].get("verdict", "missing")}: {ctx["postcheck"].get("why", [])[:3]}')
    if not isinstance(ctx['history'].get('owners'), list):
        out.append('history owners missing')
    if ctx['governance'].get('mode') not in ('EOA', 'SAFE') or not ctx['governance'].get('delay'):
        out.append('governance route missing')
    return out


def formal_blockers(ctx):
    """Reasons the formal E2E gate cannot PASS for this context, whatever the executed rows show."""
    out = []
    if ctx['evidence'] != REAL:
        out.append('SYNTHETIC context: the harness self-test never yields a formal E2E PASS')
    synthetic_actors = sorted(k for k, v in ctx['actorClasses'].items() if v != REAL)
    if synthetic_actors:
        out.append(f'actors without a real approved identity: {synthetic_actors}')
    if ctx['postcheck'].get('verdict') != 'PASS':
        out.append(f'upgrade postcheck not PASS: {ctx["postcheck"].get("failed", [])[:5]}')
    if not ctx['history'].get('authenticated'):
        out.append('history not authenticated (FH-D2): authenticated = false')
    if ctx.get('record') is None:
        out.append('no approved E2E release record: ' + '; '.join(ctx.get('recordProblems') or ['none supplied']))
    for reason in ctx['history'].get('authenticationReasons', []):
        out.append(f'history: {reason}')
    out += [f'governance: {g}' for g in ctx.get('governanceProblems', [])]
    route = ctx.get('upgradeRoute', {})
    if route.get('mode') != 'SAFE' or not route.get('approved'):
        out.append(f'upgrade route: {route.get("why", "the upgrade was not executed through the approved GOV Safe")}')
    if (ctx.get('releaseInputs') or {}).get('source') != 'APPROVED_RECORD':
        out.append('release inputs SYNTHETIC: simulation policy and labelled actors, never release parameters')
    if ctx.get('rehearsal'):
        out.append(f'rehearsal setup applied before S_post ({len(ctx["rehearsal"])} construction steps): not release evidence')
    if ctx['evidence'] == REAL and not ctx['integrations'].get('approved'):
        out.append('integrations (RewardsVault, caller, staking proxies) not from the approved release record')
    for flag, why in (('readAuthorization', 'no read authorization for the final re-run (TD-12)'),
                      ('ownerInputsApproved', 'owner release inputs not approved (TD-1 ... TD-11)'),
                      ('safeManifestApproved', 'Safe identity manifest not approved (FR-X8)')):
        if not ctx['formal'].get(flag):
            out.append(why)
    return out


# ============================================================================================ expectations
class Success:
    kind = 'success'

    def __init__(self, effects=None, events=()):
        self.effects, self.events = effects, tuple(events)

    def describe(self):
        return 'success' + (f' with events {list(self.events)}' if self.events else '')


class Revert:
    """``error`` is an error name declared in a canonical ABI, or an exact signature such as
    ``InsufficientBalance()`` for an integration contract."""
    kind = 'revert'

    def __init__(self, error):
        self.error = error

    def describe(self):
        return f'revert {self.error}'


class RevertEmpty:
    """A revert without data (for example a bare ``require`` in an external token)."""
    kind = 'revert-empty'

    def describe(self):
        return 'revert with empty data'


class Noop:
    """Returns normally, moves nothing and changes no tracked state."""
    kind = 'noop'

    def __init__(self, events=()):
        self.events = tuple(events)

    def describe(self):
        return 'no-op (success, no state change)'


class Review:
    """The expected failure is not yet specified (NEEDS_REVIEW): the observation is recorded, never PASS."""
    kind = 'review'

    def __init__(self, question):
        self.question = question

    def describe(self):
        return f'NEEDS_REVIEW: {self.question}'


def revert_data(error):
    """The revert data of an RPC error raised by the loopback node (None if absent)."""
    text = str(error)
    try:
        payload = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        payload = None
    if isinstance(payload, dict):
        data = payload.get('data')
        if isinstance(data, dict):
            data = data.get('data')
        if isinstance(data, str) and data.startswith('0x'):
            return data.lower()
        message = str(payload.get('message', ''))
    else:
        message = text
    m = re.search(r'0x[0-9a-fA-F]{8,}', message)
    return m.group(0).lower() if m else ('0x' if 'revert' in message.lower() else None)


# ============================================================================================ harness
class Harness:
    def __init__(self, chain, ctx, spec=None, canonical=CANONICAL):
        self.chain, self.ctx = chain, ctx
        self.spec = spec if spec is not None else load_spec()
        self.entries = {f"{e['contract']}.{e['signature']}": e for e in self.spec['entries']}
        self.rows, self.cases, self.problems = [], [], []
        self.case_state = None
        self.broken = None
        self.canonical = Path(canonical)
        self.abis = self._abis()
        self.errors = self._errors()
        self.events = self._events()
        self.owners = {_lower(o) for o in ctx.get('history', {}).get('owners', [])}
        self.tracked = set()
        self.fresh = set()
        self.base_accounts = frozenset()
        self.base_owners = frozenset()
        self.base = None
        self.fp0 = None
        self.counter = 0

    # -- ABIs ---------------------------------------------------------------------------------------------
    def _abis(self):
        out = {}
        for c in CONTRACTS + EXTRA_ABIS:
            path = self.canonical / c / 'artifact.json'
            if path.is_file():
                out[c] = json.loads(path.read_text())['abi']
        out['WPROS'] = [
            {'type': 'function', 'name': 'deposit', 'inputs': [], 'stateMutability': 'payable'},
            {'type': 'function', 'name': 'withdraw', 'inputs': [{'type': 'uint256'}], 'stateMutability': 'nonpayable'},
            {'type': 'function', 'name': 'approve', 'inputs': [{'type': 'address'}, {'type': 'uint256'}], 'stateMutability': 'nonpayable'},
            {'type': 'function', 'name': 'transfer', 'inputs': [{'type': 'address'}, {'type': 'uint256'}], 'stateMutability': 'nonpayable'},
            {'type': 'event', 'name': 'Deposit', 'inputs': [{'type': 'address', 'indexed': True}, {'type': 'uint256'}]},
            {'type': 'event', 'name': 'Withdrawal', 'inputs': [{'type': 'address', 'indexed': True}, {'type': 'uint256'}]}]
        return out

    def _errors(self):
        out = {}
        for c, items in self.abis.items():
            for item in items:
                if item['type'] == 'error':
                    out.setdefault(item['name'], set()).add(keccak256(abi.item_signature(item).encode())[:10])
        return out

    def _events(self):
        out = {keccak256(sig.encode()): sig.split('(')[0] for sig in INTEGRATION_EVENTS}
        for c, items in self.abis.items():
            for item in items:
                if item['type'] == 'event':
                    out[keccak256(abi.item_signature(item).encode())] = item['name']
        return out

    def calldata(self, contract, signature, *values):
        for item in self.abis[contract]:
            if item['type'] == 'function' and abi.item_signature(item) == signature:
                return abi.encode_function(item, list(values))
        raise E2EError(f'{contract}.{signature} not in the canonical ABI')

    def selector_of(self, error_name):
        if '(' in error_name:
            return {keccak256(error_name.encode())[:10]}
        sels = self.errors.get(error_name)
        if not sels:
            raise E2EError(f'error {error_name} not declared in any canonical ABI')
        return sels

    # -- addresses and accounts ---------------------------------------------------------------------------
    @property
    def a(self):
        return self.ctx['addresses']

    @property
    def actors(self):
        return self.ctx['actors']

    def account(self, label):
        """A fresh isolated account (deterministic per run and label); recorded as CONSTRUCTED."""
        acct = '0x' + keccak256(f'faroo e2e {self.ctx["chainId"]} {label}'.encode())[-40:]
        self.tracked.add(acct)
        self.fresh.add(acct)
        return acct

    def caller_class(self, who):
        who = _lower(who)
        for k, v in self.actors.items():
            if _lower(v) == who:
                return f'actor:{k}'
        if who == _lower(self.a['timelock']):
            return 'timelock'
        if who in self.owners:
            return 'history-owner'
        return 'isolated' if who in self.tracked else 'other'

    def privileged(self, who):
        who = _lower(who)
        return who in {_lower(self.actors[k]) for k in PRIVILEGED if k in self.actors} | {_lower(self.a['timelock']),
                                                                                           _lower(self.a['proxyAdmin'])}

    # -- construction primitives (mark the current case CONSTRUCTED) --------------------------------------
    def _constructed(self, what):
        if self.case_state is not None:
            self.case_state['constructions'].append(what)

    def fund(self, who, wei):
        self._constructed(f'fund {who[:10]}')
        self.chain.fund(who, self.chain.balance(who) + wei)

    def set_code(self, who, code):
        self._constructed(f'setCode {who[:10]}')
        self.chain.rpc('anvil_setCode', [who, code])
        self.tracked.add(_lower(who))

    def warp(self, seconds):
        if self.case_state is not None:
            self.case_state['timeAdvanced'] += seconds
        self.chain.warp(seconds)

    def _gov_send(self, label, to, data, expect):
        g = self.ctx['governance']
        if g['mode'] == 'EOA':
            receipt, rec = self.chain.send(f'E2E.gov.{label}', self.actors['gov'], to, data, expect=expect)
        else:
            import safe_exec
            _, _, receipt, rec = safe_exec.execute(self.chain, f'E2E.gov.{label}', g['safe'], g['owners'], g['threshold'],
                                                   safe_exec.safe_tx(to, data, 0), expect=expect)
        status = int(receipt['status'], 16)
        if expect and status != 1:
            raise E2EError(f'governance operation {label} failed')
        return status

    def safe_of(self, who):
        """The verified Safe behind an actor address ({owners, threshold}), or None for an EOA."""
        return (self.ctx.get('safes') or {}).get(_lower(who))

    def send_as(self, label, sender, to, data, *, value=0, expect=True):
        """One transaction by ``sender``. A Safe actor goes through the approved-hash route of ``safe_exec`` (owners
        approve, the last owner executes); the Safe's own events are dropped from the returned receipt."""
        safe = self.safe_of(sender)
        if safe is None:
            return self.chain.send(label, sender, to, data or '0x', value=value, expect=expect)
        if value:
            raise E2EError(f'{label}: a Safe actor sends no value in this harness')
        import safe_exec
        records, _, receipt, _ = safe_exec.execute(self.chain, label, _lower(sender), safe['owners'], safe['threshold'],
                                                   safe_exec.safe_tx(to, data or '0x', 0), expect=expect)
        receipt = {**receipt, 'logs': [l for l in receipt.get('logs', []) if _lower(l['address']) != _lower(sender)]}
        rec = {**records[-1], 'route': 'Safe approved-hash', 'safe': _lower(sender)}
        return receipt, rec

    # -- reads --------------------------------------------------------------------------------------------
    def u(self, to, sig, *args):
        return self.chain.uint(to, sig, *args)

    def balances(self, accounts, vaults=()):
        a = self.a
        out = {}
        for acct in sorted({_lower(x) for x in accounts}):
            row = {'native': self.chain.balance(acct), 'wpros': self.u(a['wpros'], 'balanceOf(address)', acct),
                   'stpros': self.u(a['stpros'], 'balanceOf(address)', acct)}
            for v in vaults:
                row[f'vault:{v}'] = self.u(v, 'balanceOf(address)', acct)
            out[acct] = row
        return out

    def records(self, owner):
        raw = self.chain.call(self.a['stpros'], 'getWithdrawals(address)', owner)
        rows = abi.decode_parameters([{'type': 'tuple[]', 'components': [{'type': 'uint256'}] * 4 + [{'type': 'address'}]}], raw)[0]
        return [{'queued': r[0], 'pending': r[1], 'createdAt': r[2], 'unbondingPeriod': r[3], 'receiver': _lower(r[4])}
                for r in rows]

    def invariants(self):
        """The §6.5 invariants on the current state: {id: (ok, detail)}."""
        a, out = self.a, {}
        st, oracle, wpros = a['stpros'], a['oracle'], a['wpros']
        supply = self.u(st, 'totalSupply()')
        pool = self.chain.call(oracle, 'poolInfo(address)', wpros)
        out['I1.supply==poolV'] = (supply == abi.dec_uint(pool, 1), f'{supply} vs {abi.dec_uint(pool, 1)}')
        q, c = self.u(st, 'queuedWithdrawal()'), self.u(st, 'completedWithdrawal()')
        pending, fifo = 0, True
        for owner in sorted(self.owners):
            recs = self.records(owner)
            pending += sum(r['pending'] for r in recs)
            fifo &= all(recs[i]['queued'] <= recs[i + 1]['queued'] for i in range(len(recs) - 1))
            head, tail = self.u(st, 'withdrawalHead(address)', owner), self.u(st, 'withdrawalTail(address)', owner)
            fifo &= head <= tail and tail - head == len(recs)
        out['I2.Q-C==sumPending'] = (q - c == pending, f'Q-C {q - c} vs pending {pending} over {len(self.owners)} owners')
        out['I3.fifo'] = (fifo, 'record order / head-tail')
        backing = []
        for v in a['governedVaults'] + a['retiredVaults']:
            if self.u(st, 'balanceOf(address)', v) < self.u(v, 'totalManagedAssets()'):
                backing.append(v)
        out['I4.vaultBacking'] = (not backing, f'under-backed {backing}')
        authority = []
        for key in ('stpros', 'oracle', 'bridge', 'distributor', 'factory'):
            if self.chain.addr(a[key], 'owner()') != _lower(a['timelock']):
                authority.append(f'{key}.owner')
            if self.chain.addr(a[key], 'guardian()') != _lower(self.actors['guardian']):
                authority.append(f'{key}.guardian')
        out['I5.authority'] = (not authority, f'changed {authority}')
        return out

    def fingerprint(self, accounts=None, owners=None):
        """A digest of the protocol state plus the balances of ``accounts`` (default: every tracked account),
        time-independent."""
        a = self.a
        st, oracle, wpros = a['stpros'], a['oracle'], a['wpros']
        doc = {'st': [self.u(st, 'totalSupply()'), self.u(st, 'queuedWithdrawal()'), self.u(st, 'completedWithdrawal()'),
                      self.chain.boolean(st, 'paused()'), self.u(st, 'unbondingPeriod()')],
               'pool': self.chain.call(oracle, 'poolInfo(address)', wpros), 'oraclePaused': self.chain.boolean(oracle, 'paused()'),
               'last': self.u(oracle, 'lastUpdateAt(address)', wpros),
               'bridge': [self.chain.balance(a['bridge']), self.u(wpros, 'balanceOf(address)', a['bridge']), self.chain.boolean(a['bridge'], 'paused()')],
               'dist': [self.chain.balance(a['distributor']), self.chain.boolean(a['distributor'], 'paused()'),
                        self.chain.addr(a['distributor'], 'keeper()'), self.chain.call(a['distributor'], 'getSlps()')],
               'factory': [self.chain.boolean(a['factory'], 'paused()'), self.chain.addr(a['factory'], 'owner()')],
               'vaults': [[self.u(v, 'phase()'), self.u(v, 'totalManagedAssets()'), self.u(v, 'totalSupply()'), self.u(v, 'roundIndex()'),
                           self.chain.boolean(v, 'retired()')] for v in a['governedVaults'] + a['retiredVaults']],
               'owners': {o: [r['pending'] for r in self.records(o)] for o in sorted(self.owners if owners is None else owners)},
               'tracked': self.balances(self.tracked if accounts is None else accounts)}
        return _sha(json.dumps(doc, sort_keys=True, default=str).encode())

    # -- snapshot discipline ------------------------------------------------------------------------------
    def begin(self):
        problems = context_problems(self.ctx)
        if not problems and self.ctx['evidence'] == REAL and not getattr(self.chain, 'forked', False):
            problems.append('REAL evidence claimed on a loopback node that is not a Fork of the real chain')
        if not problems and self.ctx['evidence'] == REAL and self.ctx.get('rehearsal'):
            problems.append('a rehearsal setup changed the node before S_post: never REAL evidence')
        if problems:
            self.problems += [f'BLOCKED context: {p}' for p in problems]
            return False
        spec_problems = verify_spec(self.spec, self.canonical)
        if spec_problems:
            self.problems += [f'FAIL spec: {p}' for p in spec_problems]
        self.verify_record()
        self.base = self.chain.snapshot()
        self.base_accounts = frozenset(self.tracked | {_lower(x) for x in self.actors.values()})
        self.base_owners = frozenset(self.owners)
        self.fp0 = self.fingerprint(self.base_accounts, self.base_owners)
        return not spec_problems

    def spec_sha256(self):
        """Canonical digest of the specification this run uses (what an approved record binds)."""
        return _sha(json.dumps(self.spec, sort_keys=True).encode())

    def verify_record(self):
        """Binding and identity checks of the approved release record. Without a pinned record nothing becomes REAL."""
        import campaign_input
        import e2e_identity
        record = self.ctx.get('record')
        if record is None:
            return
        if record.get('chainId') != self.ctx['chainId'] or record.get('anchor') != self.ctx['anchor']:
            self.problems.append('BLOCKED record: approved for another chain or anchor')
            self.ctx['record'] = None
            return
        fails = e2e_identity.binding_problems(record, self.spec_sha256(), self.spec, artifact_entries(self.canonical)[1])
        self.problems += fails
        ident = e2e_identity.identity_problems(self.chain, self.ctx, record, campaign_input.APPROVED_SAFE_IDENTITY_MANIFESTS)
        self.problems += ident
        if fails or ident:
            return
        self.ctx['actors'].update({k: _lower(v) for k, v in record['actors'].items()})
        for k in e2e_identity.ROLE_KEYS:
            self.ctx['actorClasses'][k] = REAL
        self.ctx['formal']['safeManifestApproved'] = True
        safes = {}
        for role in e2e_identity.SAFE_ROLES:
            st = e2e_identity.safe_state(self.chain, record['safes'][role]['address'])
            safes[_lower(record['safes'][role]['address'])] = {'role': role, 'owners': st['owners'], 'threshold': st['threshold']}
        self.ctx['safes'] = safes
        gov = e2e_identity.safe_state(self.chain, record['actors']['gov'])
        self.ctx['governance'] = {'mode': 'SAFE', 'safe': _lower(record['actors']['gov']), 'owners': gov['owners'],
                                  'threshold': gov['threshold'], 'delay': self.ctx['governance']['delay'], 'source': 'approved record, Fork-verified'}
        route = self.ctx.get('upgradeRoute', {})
        if (route.get('mode') == 'SAFE' and route.get('class') != 'SYNTHETIC'
                and _lower(route.get('safe')) == _lower(record['actors']['gov'])):
            route['approved'] = True
        integ = record.get('integrations') or {}
        self.ctx['integrations'] = {**integ, 'approved': True}

    def restore(self):
        try:
            self.chain.revert(self.base)
        except Exception as error:     # noqa: BLE001 - any restoration failure is evidence
            self.broken = f'snapshot restore raised {type(error).__name__}: {str(error)[:200]}'
            return False
        self.base = self.chain.snapshot()
        fp = self.fingerprint(self.base_accounts, self.base_owners)
        self.owners = set(self.base_owners)
        if fp != self.fp0:
            self.broken = f'state after restore differs from S_post (fingerprint {fp[:12]} != {self.fp0[:12]})'
            return False
        dirty = sorted(acct for acct, b in self.balances(self.fresh).items()
                       if any(b.values()) or self.chain.code(acct) not in ('0x', '0x0') or self.records(acct))
        if dirty:
            self.broken = f'fresh accounts not empty after restore: {dirty[:3]}'
            return False
        return True

    @contextlib.contextmanager
    def case(self, cid, title, scenario, intended=REAL):
        """One independent case derived from S_post. ``intended`` is REAL only for an unmodified real-state case."""
        state = {'id': cid, 'title': title, 'scenario': scenario, 'intended': intended, 'constructions': [],
                 'timeAdvanced': 0, 'exception': None}
        self.cases.append(state)
        if self.broken:
            state['skipped'] = f'BLOCKED: {self.broken}'
            self.rows.append(self._row_skeleton(cid, scenario, None, BLOCKED, f'case not run: {self.broken}'))
            yield None
            return
        self.case_state = state
        try:
            yield self
        except E2EError as error:
            state['exception'] = f'E2EError: {error}'
            self.rows.append(self._row_skeleton(cid, scenario, None, FAIL, f'harness error: {error}'))
        except Exception as error:     # noqa: BLE001 - recorded, never silent
            state['exception'] = f'{type(error).__name__}: {str(error)[:300]}'
            self.rows.append(self._row_skeleton(cid, scenario, None, FAIL, f'exception: {state["exception"]}'))
        finally:
            self.case_state = None
            if not self.restore():
                self.rows.append(self._row_skeleton(f'{cid}.restore', scenario, None, FAIL, self.broken))

    def row_class(self):
        state = self.case_state
        if self.ctx['evidence'] != REAL:
            return SYNTHETIC
        if state is None or state['intended'] != REAL or state['constructions']:
            return CONSTRUCTED
        return REAL

    def _row_skeleton(self, rid, scenario, entry, verdict, detail):
        return {'id': rid, 'scenario': scenario, 'entry': entry, 'selector': None, 'class': self.row_class(),
                'verdict': verdict, 'detail': detail}

    # -- operations ---------------------------------------------------------------------------------------
    def _revert_probe(self, sender, to, data, value):
        request = {'from': sender, 'to': to, 'data': data, 'gas': hex(100_000_000)}
        if value:
            request['value'] = hex(value)
        try:
            self.chain.rpc('eth_call', [request, 'latest'])
            return None
        except ValueError as error:
            return revert_data(error) or '0x'

    def op(self, rid, entry, target, sender, data, *, expect, value=0, watch=(), vaults=(), note='', via=None):
        """One recorded operation sent directly by ``sender``. ``entry`` is 'Contract.signature' of the frozen spec.
        ``via`` names an integration contract whose call reaches the entry (the calldata then targets ``via``)."""
        def transport():
            observed = None
            if expect.kind in ('revert', 'revert-empty', 'review'):
                observed = self._revert_probe(sender, target, data or '0x', value)
            receipt, rec = self.send_as(f'E2E.{rid}', sender, target, data or '0x', value=value,
                                        expect=expect.kind in ('success', 'noop'))
            return receipt, rec, observed
        return self._record(rid, entry, target, sender, data, expect, value, watch, vaults, note, via, transport)

    def gov_call(self, rid, entry, target, data, *, expect, watch=(), vaults=(), note='', via=None):
        """One recorded operation executed by the Timelock through the governance route (schedule, delay, execute)."""
        tl = self.a['timelock']

        def transport():
            g = self.ctx['governance']
            salt = '0x' + keccak256(f'faroo e2e {rid} {self.counter}'.encode())[2:]
            self.counter += 1
            sched = self.calldata('TimelockController', 'schedule(address,uint256,bytes,bytes32,bytes32,uint256)',
                                  target, 0, data, '0x' + '00' * 32, salt, g['delay'])
            execute = self.calldata('TimelockController', 'execute(address,uint256,bytes,bytes32,bytes32)',
                                    target, 0, data, '0x' + '00' * 32, salt)
            self._constructed(f'TL operation {rid}')
            self._gov_send(f'{rid}.schedule', tl, sched, True)
            self.chain.warp(g['delay'] + 1)
            if self.case_state is not None:
                self.case_state['timeAdvanced'] += g['delay'] + 1
            observed = self._revert_probe(tl, target, data, 0) if expect.kind in ('revert', 'revert-empty', 'review') else None
            ok = expect.kind in ('success', 'noop')
            if g['mode'] == 'EOA':
                receipt, rec = self.chain.send(f'E2E.{rid}.execute', self.actors['gov'], tl, execute, expect=ok)
            else:
                import safe_exec
                _, _, receipt, rec = safe_exec.execute(self.chain, f'E2E.{rid}.execute', g['safe'], g['owners'], g['threshold'],
                                                       safe_exec.safe_tx(tl, execute, 0), expect=ok)
                rec = {'status': int(receipt['status'], 16), 'transactionHash': receipt['transactionHash'],
                       'gasUsed': int(receipt['gasUsed'], 16), 'route': 'Safe approved-hash', 'safe': _lower(g['safe'])}
            return receipt, rec, observed
        return self._record(rid, entry, target, tl, data, expect, 0, watch, vaults, note, via, transport)

    def _record(self, rid, entry, target, sender, data, expect, value, watch, vaults, note, via, transport):
        spec = self.entries.get(entry)
        if spec is None:
            raise E2EError(f'{rid}: entry {entry} is not in the frozen spec')
        problems = []
        if via is None:
            if spec['selector'] is None:
                if data not in ('0x', ''):
                    problems.append('receive() must be called with empty calldata')
            elif not (data or '').lower().startswith(spec['selector']):
                problems.append(f'calldata selector {(data or "")[:10]} != spec {spec["selector"]}')
        scenario = rid.split('.')[0]
        a = self.a
        accounts = {sender, *watch, a['stpros'], a['bridge'], a['distributor'], *(vaults or ())}
        self.tracked |= {_lower(x) for x in watch}
        self.note_owner(spec, data)
        pre_bal = self.balances(accounts, vaults)
        pre_inv = self.invariants()
        pre_fp = self.fingerprint() if expect.kind in ('revert', 'revert-empty', 'noop', 'review') else None
        receipt, rec, observed = transport()
        status = rec['status']
        logs = receipt.get('logs', [])
        events = [{'address': _lower(l['address']), 'event': self.events.get(l['topics'][0].lower(), l['topics'][0]) if l['topics'] else None,
                   'topics': l['topics'], 'data': l['data']} for l in logs]
        post_bal = self.balances(accounts, vaults)
        post_inv = self.invariants()
        deltas = {acct: {k: post_bal[acct][k] - pre_bal[acct][k] for k in pre_bal[acct] if post_bal[acct][k] != pre_bal[acct][k]}
                  for acct in pre_bal}
        deltas = {k: v for k, v in deltas.items() if v}
        inv_failed = [k for k, (ok, _) in post_inv.items() if not ok]
        effects = []
        verdict = PASS
        if expect.kind == 'success':
            if status != 1:
                problems.append('required success reverted')
            else:
                names = {e['event'] for e in events}
                missing = [e for e in expect.events if e not in names]
                if missing:
                    problems.append(f'missing events {missing}')
                if expect.effects:
                    effects = [{'check': n, 'ok': bool(ok), 'detail': str(d)} for n, ok, d in expect.effects(self, pre_bal, post_bal, receipt, events)]
                    problems += [f'effect {e["check"]}: {e["detail"]}' for e in effects if not e['ok']]
        elif expect.kind == 'noop':
            if status != 1:
                problems.append('required no-op reverted')
            if deltas:
                problems.append(f'no-op moved assets {deltas}')
            if self.fingerprint() != pre_fp:
                problems.append('no-op changed tracked state')
            unexpected = [e['event'] for e in events if e['event'] not in expect.events]
            if unexpected:
                problems.append(f'no-op emitted {unexpected}')
        else:
            if status != 0:
                problems.append('required revert succeeded')
            if deltas:
                problems.append(f'failed operation moved assets {deltas}')
            if self.fingerprint() != pre_fp:
                problems.append('failed operation changed tracked state')
            if expect.kind == 'revert':
                wanted = self.selector_of(expect.error)
                if observed is None or observed[:10] not in wanted:
                    problems.append(f'revert data {str(observed)[:74]} is not {expect.error} {sorted(wanted)}')
            elif expect.kind == 'revert-empty':
                if observed != '0x':
                    problems.append(f'revert data {observed} is not empty')
        if inv_failed:
            problems.append(f'invariants failed after the operation: {inv_failed}')
        if problems:
            verdict = FAIL
        elif expect.kind == 'review':
            verdict = NEEDS_REVIEW
        row = {'id': rid, 'scenario': scenario, 'case': self.case_state['id'] if self.case_state else None,
               'entry': entry, 'policy': spec['policy'], 'selector': spec['selector'], 'target': _lower(target),
               'via': _lower(via) if via else None,
               'caller': _lower(sender), 'callerClass': self.caller_class(sender), 'privilegedCaller': self.privileged(sender),
               'class': self.row_class(), 'node': 'fork' if getattr(self.chain, 'forked', False) else 'loopback',
               'timeAdvanced': self.case_state['timeAdvanced'] if self.case_state else 0,
               'constructions': list(self.case_state['constructions']) if self.case_state else [],
               'value': str(value), 'expected': expect.describe(), 'transactionHash': rec['transactionHash'],
               'route': rec.get('route', 'direct'),
               'status': status, 'gasUsed': rec['gasUsed'], 'observedRevertData': observed, 'events': events,
               'deltas': {k: {kk: str(vv) for kk, vv in v.items()} for k, v in deltas.items()},
               'invariantsBefore': {k: v[0] for k, v in pre_inv.items()}, 'invariantsAfter': {k: v[0] for k, v in post_inv.items()},
               'effects': effects, 'note': note, 'verdict': verdict, 'detail': '; '.join(problems)[:600]}
        if expect.kind == 'review':
            row['reviewQuestion'] = expect.question
        self.rows.append(row)
        return row

    def note_owner(self, spec, data):
        """A StPROS withdraw / redeem may create a record for its ``owner`` argument: include it in I2 beforehand."""
        if spec['contract'] == 'StPROS' and spec['signature'] in ('withdraw(uint256,address,address)',
                                                                  'redeem(uint256,address,address)') and len(data or '') >= 10 + 64 * 3:
            self.owners.add('0x' + data[10 + 64 * 2 + 24:10 + 64 * 3].lower())

    def read_row(self, rid, entry, target, data, check=None):
        """A view call on the current state: PASS if it returns (and ``check(value)`` holds)."""
        spec = self.entries.get(entry)
        if spec is None:
            raise E2EError(f'{rid}: entry {entry} is not in the frozen spec')
        problems = []
        if not data.lower().startswith(spec['selector']):
            problems.append('selector mismatch')
        try:
            value = self.chain.raw_call(target, data)
        except ValueError as error:
            value, problems = None, problems + [f'view reverted: {str(revert_data(error))[:40]}']
        if value is not None and check is not None:
            ok, detail = check(value)
            if not ok:
                problems.append(str(detail))
        self.rows.append({'id': rid, 'scenario': rid.split('.')[0], 'case': self.case_state['id'] if self.case_state else None,
                          'entry': entry, 'policy': 'READ', 'selector': spec['selector'], 'target': _lower(target),
                          'class': self.row_class(), 'value': (value or '')[:200], 'expected': 'returns',
                          'verdict': FAIL if problems else PASS, 'detail': '; '.join(problems)})

    def check_row(self, rid, ok, detail, *, otherwise=BLOCKED, value=None):
        """An integration or wiring fact without a spec entry (it does not count towards ABI coverage): PASS when
        ``ok``, otherwise ``otherwise`` (BLOCKED for missing code, permission or wiring; FAIL for a broken invariant)."""
        self.rows.append({'id': rid, 'scenario': rid.split('.')[0], 'case': self.case_state['id'] if self.case_state else None,
                          'entry': None, 'class': self.row_class(), 'expected': 'integration fact', 'value': value,
                          'verdict': PASS if ok else otherwise, 'detail': '' if ok else detail})
        return ok

    def note(self, rid, entry, verdict, detail, scenario=None):
        """A row without an operation: NOT_RUN, BLOCKED or NOT_APPLICABLE with its reason (never PASS)."""
        if verdict == PASS:
            raise E2EError('a note row cannot be PASS')
        if entry is not None and entry not in self.entries:
            raise E2EError(f'{rid}: entry {entry} is not in the frozen spec')
        self.rows.append({'id': rid, 'scenario': scenario or rid.split('.')[0],
                          'case': self.case_state['id'] if self.case_state else None, 'entry': entry,
                          'class': self.row_class(), 'verdict': verdict, 'detail': detail})

    # -- coverage and verdicts ----------------------------------------------------------------------------
    def coverage(self):
        """Per-entry coverage against the caller policy (UPGRADE-RUNBOOK §6.1)."""
        by_entry = {}
        for r in self.rows:
            if r.get('entry'):
                by_entry.setdefault(r['entry'], []).append(r)
        out = {}
        for key, e in self.entries.items():
            rows = by_entry.get(key, [])
            passed = [r for r in rows if r['verdict'] == PASS]
            policy, why = e['policy'], []
            if policy == 'READ':
                ok = bool(passed)
            elif policy == 'PERMISSIONLESS':
                ok = any(r.get('expected', '').startswith('success') and not r.get('privilegedCaller') for r in passed)
                if any('revert' in r.get('expected', '') and r.get('note') == 'unauthorized' for r in rows):
                    ok, why = False, ['unauthorized-caller row on a permissionless entry (specification error)']
            elif policy == 'SELF_SCOPED':
                ok = any(r.get('expected', '').startswith(('success', 'no-op')) for r in passed)
                if key.endswith('renounceRole(bytes32,address)'):
                    kinds = {r.get('note') for r in passed}
                    ok = {'self-renounce', 'bad-confirmation', 'no-role'} <= kinds
                    if not ok:
                        why.append(f'renounceRole behaviours covered {sorted(k for k in kinds if k)}')
            else:
                ok = any(r.get('note') == 'unauthorized' for r in passed)
                if e['class'] in ('U', 'P') and policy not in ('INTERNAL_HOOK',):
                    ok = ok and any(r.get('expected', '').startswith(('success', 'no-op')) for r in passed)
                if key.split('.', 1)[1] in ('pause()', 'unpause()'):
                    ok = ok and any(r.get('expected', '').startswith('success') for r in passed)
            review = [r for r in rows if r['verdict'] == NEEDS_REVIEW]
            failed = [r for r in rows if r['verdict'] == FAIL]
            status = FAIL if failed else (PASS if ok else (NOT_RUN if not rows else BLOCKED))
            if status == PASS and (review or e['status'] != 'SPECIFIED'):
                status = NEEDS_REVIEW
            out[key] = {'policy': policy, 'class': e['class'], 'rows': len(rows), 'passRows': len(passed),
                        'status': status, 'why': why}
        return out

    def verdicts(self):
        cov = self.coverage()
        executed = [r for r in self.rows]
        failures = [r['id'] for r in executed if r['verdict'] == FAIL] + [p for p in self.problems if p.startswith('FAIL')]
        if self.broken:
            failures.append(f'snapshot discipline: {self.broken}')
        if (self.ctx.get('postcheck') or {}).get('verdict') == 'FAIL':
            failures.append(f'upgrade postcheck FAIL: {self.ctx["postcheck"].get("why", [])[:3]}')
        uncovered = sorted(k for k, v in cov.items() if v['status'] in (NOT_RUN, BLOCKED))
        review = sorted(k for k, v in cov.items() if v['status'] == NEEDS_REVIEW)
        scenarios = {}
        for r in executed:
            scenarios.setdefault(r['scenario'], []).append(r)
        def scen_status(sid, need_real=False):
            rows = scenarios.get(sid, [])
            if not rows:
                return NOT_RUN
            worst = max((r['verdict'] for r in rows), key=SEVERITY.get)
            if need_real and not any(r['verdict'] == PASS and r['class'] == REAL for r in rows):
                return BLOCKED if worst != FAIL else FAIL
            return worst
        mandatory = {s: scen_status(s, need_real=True)
                     for s in (MANDATORY_REAL_MAINNET if self.ctx['chainId'] == 1672 else MANDATORY_REAL)}
        user = {s: scen_status(s) for s in USER_PATHS}
        ops = {s: scen_status(s) for s in OPS_PATHS}
        execution = FAIL if failures else PASS
        blockers = [p for p in self.problems if p.startswith('BLOCKED')]
        blockers += formal_blockers(self.ctx) if not context_problems(self.ctx) else []
        if uncovered:
            blockers.append(f'{len(uncovered)} entries without coverage')
        if review:
            blockers.append(f'{len(review)} entries NEEDS_REVIEW: {review[:6]}')
        for group, items in (('mandatory REAL', mandatory), ('user paths', user), ('operations', ops)):
            bad = sorted(s for s, v in items.items() if v != PASS)
            if bad:
                blockers.append(f'{group} not PASS: {bad}')
        formal = FAIL if failures else (BLOCKED if blockers else PASS)
        return {'execution': execution, 'formal': formal, 'failures': failures[:50], 'blockers': blockers,
                'uncovered': uncovered, 'needsReview': review, 'mandatoryReal': mandatory, 'userPaths': user,
                'operations': ops}

    # -- outputs ------------------------------------------------------------------------------------------
    def evidence(self):
        cov = self.coverage()
        totals = {}
        for v in cov.values():
            t = totals.setdefault(v['policy'], {'entries': 0, 'covered': 0})
            t['entries'] += 1
            t['covered'] += v['status'] == PASS
        counts = {}
        for r in self.rows:
            counts[r['verdict']] = counts.get(r['verdict'], 0) + 1
        classes = {}
        for r in self.rows:
            classes[r['class']] = classes.get(r['class'], 0) + 1
        return {'schema': SCHEMA, 'chainId': self.ctx['chainId'], 'evidence': self.ctx['evidence'],
                'anchor': self.ctx['anchor'], 'source': self.ctx['source'],
                'spec': {'sha256': _sha(json.dumps(self.spec, sort_keys=True).encode()), 'derivedFrom': self.spec['derivedFrom']},
                'context': {**{k: self.ctx.get(k) for k in ('addresses', 'actors', 'actorClasses', 'postcheck', 'governance', 'formal',
                                                            'integrations')},
                            'history': {k: v for k, v in (self.ctx.get('history') or {}).items() if k not in ('owners', 'holders')},
                            'upgradeRoute': self.ctx.get('upgradeRoute'), 'governanceProblems': self.ctx.get('governanceProblems'),
                            'recordSha256': self.ctx.get('recordSha256'), 'recordProblems': self.ctx.get('recordProblems'),
                            'specSha256': self.spec_sha256(), 'safes': self.ctx.get('safes'),
                            'rehearsal': self.ctx.get('rehearsal')},
                'problems': self.problems, 'verdicts': self.verdicts(), 'coverage': cov, 'coverageTotals': totals,
                'rowCounts': counts, 'rowClasses': classes, 'cases': self.cases, 'rows': self.rows}

    def write(self, directory, name=None):
        directory = Path(directory)
        doc = self.evidence()
        # The official name needs REAL evidence on a Fork node with a valid context; anything else is a self-test.
        real = (doc['evidence'] == REAL and getattr(self.chain, 'forked', False)
                and not any(p.startswith('BLOCKED context') for p in self.problems))
        name = name or ('POST-UPGRADE-USER-E2E' if real else 'E2E-HARNESS-SELF-TEST')
        if name == 'POST-UPGRADE-USER-E2E' and not real:
            raise E2EError('only a REAL context on a Fork node may write POST-UPGRADE-USER-E2E')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{name}.json').write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + '\n')
        (directory / f'{name}.md').write_text(render_markdown(doc))
        return doc


def render_markdown(doc):
    v = doc['verdicts']
    real = doc['evidence'] == REAL
    title = ('Post-upgrade user and operations E2E' if real else
             'E2E harness self-test (SYNTHETIC: not a release report and not POST-UPGRADE-USER-E2E)')
    lines = [f'# {title}', '', f"Chain {doc['chainId']}; evidence {doc['evidence']}; anchor {doc['anchor']}; "
             f"source {doc['source'].get('commit')}; spec {doc['spec']['sha256'][:16]}.", '',
             f"**Execution verdict: {v['execution']}. Formal E2E verdict: {v['formal']}.**", '']
    if v['blockers']:
        lines += ['Blockers:', ''] + [f'- {b}' for b in v['blockers']] + ['']
    if v['failures']:
        lines += ['Failures:', ''] + [f'- {f}' for f in v['failures']] + ['']
    lines += ['| Caller policy | Entries | Covered (PASS) |', '|---|---|---|']
    for p, t in sorted(doc['coverageTotals'].items()):
        lines.append(f"| {p} | {t['entries']} | {t['covered']} |")
    lines += ['', '| Group | Scenario | Status |', '|---|---|---|']
    for group in ('mandatoryReal', 'userPaths', 'operations'):
        for s, st in v[group].items():
            lines.append(f'| {group} | {s} | {st} |')
    lines += ['', f"Rows: {doc['rowCounts']}; classes: {doc['rowClasses']}.", '',
              '| Row | Entry | Caller | Class | Expected | Status | Verdict | Detail |', '|---|---|---|---|---|---|---|---|']
    for r in doc['rows']:
        lines.append(f"| {r['id']} | `{r.get('entry') or '-'}` | {r.get('callerClass', '-')} | {r['class']} | "
                     f"{str(r.get('expected', '-'))[:60]} | {r.get('status', '-')} | {r['verdict']} | {str(r.get('detail', ''))[:120]} |")
    return '\n'.join(lines) + '\n'


# ============================================================================================ driver adapter
def context_from_run(run, p0, actors, *, record=None, integrations=None):
    """Build the harness context from an ``execution`` run at its post-upgrade point. Formal inputs come only from
    evidence: the run's postcheck and transcript, the topology binding, and an approved release record."""
    import e2e_identity
    import models
    import release_config as rc
    m, chain = run.m, run.chain
    mainnet = m['chain'] == 'mainnet'
    tl = p0['timelock']
    st = m['stpros']
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    vaults = [v.lower() for v in m['vaults']] if not mainnet else [m['vault001'].lower(), m['testVault'].lower()]
    governed = [vaults[14]] if not mainnet and not chain.boolean(vaults[14], 'retired()') else ([vaults[0]] if mainnet else [])
    retired = [v for v in vaults if v not in governed]
    binding = run.topo.get('binding') or {}
    acts = {k: _lower(v) for k, v in actors.items()}
    acts.update(e1=_lower(m['e1']), e2=_lower(m['e2']))
    # the effective operational actors are the ones the upgraded contracts hold (release inputs carry them over)
    acts['reporter'] = chain.addr(m['oracle'], 'slp()')
    acts['keeper'] = chain.addr(p0.get('distributorProxy') or m['distributor'], 'keeper()')
    real = run.real
    classes = {k: (REAL if real and k in ('e1', 'e2') else SYNTHETIC) for k in ACTOR_KEYS}
    delay = chain.uint(tl, 'getMinDelay()')
    post = e2e_identity.postcheck(run)
    governance_problems = []
    if mainnet:
        safe = e2e_identity.safe_state(chain, m['safe'])
        acts['gov'] = _lower(m['safe'])
        governance = {'mode': 'SAFE', 'safe': _lower(m['safe']), 'owners': safe.get('owners', []),
                      'threshold': safe.get('threshold'), 'delay': delay, 'source': 'Safe state on the node at S_post'}
        if sorted(safe.get('owners', [])) != sorted(_lower(o) for o in m['safeOwners']) or safe.get('threshold') != m['safeThreshold']:
            governance_problems.append('the Safe signers on the node differ from the topology signers used for H/U (stale signer set)')
        route = {'mode': 'SAFE', 'safe': _lower(m['safe']), 'approved': False,
                 'why': 'mainnet U executed through the GOV Safe route; the Safe is approved only by an approved record'}
    elif (getattr(run, 'gov_route', None) or {}).get('mode') == 'SAFE':
        r = run.gov_route
        safe = e2e_identity.safe_state(chain, r['gov']['address'])
        acts['gov'], acts['can'] = r['gov']['address'], r['can']['address']
        governance = {'mode': 'SAFE', 'safe': r['gov']['address'], 'owners': safe.get('owners', []), 'threshold': safe.get('threshold'),
                      'delay': delay, 'source': f'GOV_t Safe route of the run ({r["class"]}), read from the node at S_post'}
        if sorted(safe.get('owners', [])) != sorted(r['gov']['owners']) or safe.get('threshold') != r['gov']['threshold']:
            governance_problems.append('the GOV_t Safe signers on the node differ from the signers that executed T-5/U_t (stale signer set)')
        route = {'mode': 'SAFE', 'safe': r['gov']['address'], 'approved': False, 'class': r['class'],
                 'why': f'testnet U_t executed through the GOV_t Safe route ({r["class"]}); the Safe is approved only by an approved record'}
    else:
        governance = {'mode': 'EOA', 'delay': delay, 'source': 'execution.testnet synthetic GOV_t EOA (legacy test path)'}
        route = {'mode': 'EOA', 'approved': False, 'from': post.get('upgradeTxFrom'),
                 'why': 'testnet U_t was executed by a synthetic GOV_t EOA (legacy test path; use the GOV_t Safe route)'}
    holders = sorted(_lower(u) for u in m.get('users', []))
    ctx = {
        'chainId': m['chainId'], 'evidence': REAL if real else SYNTHETIC,
        # the anchor an approved record binds: the Fork block, or the node head when the run started
        'anchor': approved_inputs.run_anchor(run) if hasattr(run, 'start_anchor') else
                  {'block': binding.get('forkBlock') or int(chain.block()['number'], 16), 'hash': binding.get('forkBlockHash') or chain.block()['hash']},
        'source': {'commit': e2e_identity.FROZEN_COMMIT + ' (export tree verified by the run script)',
                   'artifactSha256': artifact_entries()[1], 'executionSource': binding.get('executionSource')},
        'addresses': {'stpros': _lower(st), 'wpros': _lower(m['wpros']), 'oracle': _lower(m['oracle']),
                      'bridge': _lower(p0.get('bridgeProxy') or m['bridge']),
                      'distributor': _lower(p0.get('distributorProxy') or m['distributor']),
                      'factory': _lower(m['factory']), 'timelock': _lower(tl), 'proxyAdmin': _lower(m['proxyAdmin']),
                      'governedVaults': governed, 'retiredVaults': retired,
                      'counterparty': _lower(m.get('counterparty') or m.get('counterpartyMain') or ZERO),
                      'extraFactories': [_lower(m['factoryTest'])] if mainnet else []},
        'actors': acts, 'actorClasses': classes,
        'postcheck': post,
        'history': {'owners': sorted(o for o in model.records), 'holders': holders,
                    'vaultHolders': ({_lower(m['vault001']): [_lower(m['vault001Holder'])]} if m.get('vault001Holder') else {}),
                    'authenticated': False, 'authenticationReasons': [],
                    'source': 'synthetic fixture history' if not real else 'content-verified history'},
        'governance': governance, 'governanceProblems': governance_problems, 'upgradeRoute': route,
        'safes': ({governance['safe']: {'role': 'gov', 'owners': governance['owners'], 'threshold': governance['threshold']}}
                  if governance['mode'] == 'SAFE' else {}),
        'formal': {'readAuthorization': False, 'ownerInputsApproved': False, 'safeManifestApproved': False},
        'integrations': {**(integrations or {'rewardsVault': None, 'rewardsCaller': None, 'stakingProxies': []}), 'approved': False},
        'releaseInputs': getattr(run, 'release_inputs', None) or {'source': 'SYNTHETIC', 'label': 'mainnet simulation policy'},
    }
    # the record that drove the upgrade is the E2E record: inherited when none is given, refused when another one is
    run_record = (getattr(run, 'gov_route', None) or {}).get('record')
    if run_record:
        if record is None:
            record = (Path(run_record[0]), run_record[1])
        elif record[1] != run_record[1]:
            ctx['recordMismatch'] = f'E2E record {record[1][:16]} is not the record {run_record[1][:16]} that drove the upgrade'
    return apply_record(ctx, run, record)


def apply_record(ctx, run, record):
    """The record-dependent context: the record itself (only if pinned and well formed), history authentication and
    the formal read / owner-input flags. ``record`` is ``(path, raw_sha256)`` or None. Identity and binding checks
    against the node follow in ``Harness.verify_record``."""
    import e2e_identity
    import scenarios as fs
    doc, problems = e2e_identity.load_record(*record) if record else (None, ['no record supplied'])
    authenticated, reasons = e2e_identity.history_authenticated(run, doc, fs.FINAL_GATE_PROVENANCE_IMPLEMENTED)
    ctx.update(record=doc, recordProblems=problems, recordSha256=record[1] if record else None)
    ctx['history'].update(authenticated=authenticated, authenticationReasons=reasons)
    # owner inputs count only when every item is approved in the record AND the upgrade was driven by that record and
    # its INPUT checks all passed on the node
    run_inputs = getattr(run, 'release_inputs', None) or {}
    input_rows = [c for c in run.checks if c['id'].startswith('INPUT.')]
    ctx['formal'].update(readAuthorization=e2e_identity.read_authorization(run, doc),
                         ownerInputsApproved=bool(doc and not approved_inputs.missing_approvals(doc)
                                                  and run_inputs.get('source') == 'APPROVED_RECORD'
                                                  and run_inputs.get('recordSha256') == ctx.get('recordSha256')
                                                  and input_rows and all(c['verdict'] == 'PASS' for c in input_rows)))
    return ctx


def hook(out_dir=None, *, spec=None, scenarios=None, record=None, integrations=None, setup=None, mutate=None, result=None):
    """A ``then(run, ctx)`` hook for ``execution.testnet`` / ``execution.mainnet``: runs the harness at S_post.

    ``record`` is ``(path, raw_sha256)`` of the E2E release record. It counts only if pinned in
    ``e2e_identity.APPROVED_E2E_RECORDS``.
    ``setup(h, run)`` prepares a rehearsal (``e2e_rehearsal``) before S_post is taken. It is only meaningful on synthetic
    nodes: it can never make evidence REAL.
    ``mutate(h)`` is for the negative controls only.
    ``result`` (a dict) receives the evidence document."""
    def then(run, ctx):
        import e2e_scenarios
        context = context_from_run(run, ctx['p0'], ctx['actors'], record=record, integrations=integrations)
        h = Harness(run.chain, context, spec)
        h.run = run
        if setup:
            h.ctx.setdefault('rehearsal', [])
            setup(h, run)
            h.ctx['rehearsal'] = h.ctx['rehearsal'] or ['setup']
        if mutate:
            mutate(h)
        if h.begin():
            e2e_scenarios.run_all(h, scenarios)
        doc = h.write(out_dir or (run.dir / 'e2e'))
        if result is not None:
            result.update(doc)
        v = doc['verdicts']
        sev = {'PASS': 'PASS', 'FAIL': 'FAIL', 'BLOCKED': 'BLOCKED'}
        run.add({'id': 'E2E.execution', 'verdict': sev[v['execution']], 'expected': '1', 'actual': '1' if v['execution'] == 'PASS' else '0',
                 'detail': '; '.join(v['failures'])[:300]},
                {'id': 'E2E.formal', 'verdict': sev[v['formal']], 'expected': '1',
                 'actual': '1' if v['formal'] == 'PASS' else '0', 'detail': '; '.join(v['blockers'])[:300]})
        return ['E2E']
    return then


def compose(*thens):
    """Chain several ``then(run, ctx)`` hooks (for example ``governance.authority_only`` and ``hook()``); the
    returned ledger ids are concatenated in order."""
    def then(run, ctx):
        ids = []
        for t in thens:
            ids += t(run, ctx) or []
        return ids
    return then
