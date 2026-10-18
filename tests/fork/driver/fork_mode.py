"""Real-state Fork boundary for a future authorized run. Fail-closed by default. Test-only.

A real-state Fork is anvil's fork mode pointed at a public endpoint at a pinned block: anvil itself fetches the state
it needs, so the driver cannot route those reads through ``transport.HttpGate``. This module provides the same
controls for such a run, to sit in front of anvil:

- ``load_authorization``: schema ``faroo-fork-execution-authorization-v1`` bound by raw SHA-256. A
  ``LOCAL_FORK_PUBLIC_READ_ONLY`` authorization is refused unless its SHA-256 is in
  ``APPROVED_FORK_AUTHORIZATIONS`` (``fr_d1_approvals.FORK``), which is empty: FR-D1 is not approved.
  ``SIMULATION`` authorizations admit loopback upstreams only. A public upstream also needs the v2 schema
  (``fr_d1_scope``): S3 stage, the measured read methods, and a fork block and hash bound by
  ``fr_d1_scope.bind_fork_anchor`` to the S1/S2 ledgers and an accepted Agent A reconciliation record.
- ``require_export_root`` (P1-D04): every output of an authorized run lives under
  ``<export>/cache_forge/rehearsal/fork-campaign/<area>/`` of an approved export (``EXPORT-MANIFEST.txt``, no
  ``.git``, no symlink component). The collector enforces it for public collection too.
- ``ForkBudgetProxy``: a 127.0.0.1 HTTP endpoint that anvil uses as its fork URL. Each POST must be canonical
  JSON-RPC 2.0 with methods in the authorization's list and at most ``GROUP_LIMIT`` items; it is recorded in the
  persistent ``transport.BudgetLedger`` *before* it is forwarded (so the cumulative ceiling holds across retries,
  restarts and invocations), paced, and forwarded only to the authorization's single endpoint with no proxy and
  no redirect. At the ceiling every further request is refused and the run stops. The ledger must belong to the
  proxy's own authorization. Under a v2 authorization every read must be at the bound fork block, and a chain id or
  fork-header answer that contradicts the bound anchor stops the proxy.
No fork launcher is part of the driver: the accepted WP-04.1 isolation rule (``test_driver.IsolationTest``) keeps
any fork flag out of these sources. The loopback-only rehearsal that starts anvil in fork mode behind this proxy is
a research evidence script (``execution-readiness/evidence/scripts/fork_witness.py``); moving it into the driver
needs an approved amendment of that rule. Nothing here signs, broadcasts or deploys.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import fr_d1_approvals as approvals  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import transport as tp  # noqa: E402
from transport import BudgetExhausted, CollectionStop  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release.native import rpc as native_rpc  # noqa: E402

SCHEMA = 'faroo-fork-execution-authorization-v1'
KEYS = frozenset({'schema', 'authorizationId', 'mode', 'chain', 'endpoint', 'methods', 'forkBlock', 'forkBlockHash',
                  'maxHttpPosts', 'maxRestarts', 'pacingMs', 'retention', 'statement'})
MODES = ('SIMULATION', 'LOCAL_FORK_PUBLIC_READ_ONLY')
# The read methods a forked anvil is expected to need. The exact set must be measured in the first authorized
# run (``ForkBudgetProxy.methods_seen``); anything else is refused and stops the run.
FORK_METHODS = ('eth_chainId', 'eth_getBlockByNumber', 'eth_getBalance', 'eth_getCode', 'eth_getStorageAt',
                'eth_getTransactionCount', 'eth_getAccountInfo', 'eth_getProof', 'eth_feeHistory', 'eth_gasPrice')
CHAIN_IDS = {'mainnet': 1672, 'testnet': 688689}
# FR-D1 owner authorizations of a local Fork of public state (``fr_d1_approvals.FORK``). Empty: no real-state Fork
# is authorized. An entry is added only by a separate reviewed change, never by this package.
APPROVED_FORK_AUTHORIZATIONS = approvals.FORK
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_HASH = re.compile(r'0x[0-9a-f]{64}')


def _stop(code, detail='', status='REFUSED'):
    return CollectionStop(code, detail, status)


# --------------------------------------------------------------------------------------------- authorization
def load_authorization(path, sha256, approved=APPROVED_FORK_AUTHORIZATIONS):
    _, doc = scope.read_record(path, sha256, 'AUTHORIZATION_HASH')
    v2 = isinstance(doc, dict) and doc.get('schema') == scope.FORK_SCHEMA
    ok = (isinstance(doc, dict) and set(doc) == (scope.FORK_KEYS if v2 else KEYS)
          and doc['schema'] in (SCHEMA, scope.FORK_SCHEMA) and doc['mode'] in MODES
          and isinstance(doc['authorizationId'], str) and _ID.fullmatch(doc['authorizationId']) is not None
          and doc['chain'] in CHAIN_IDS and isinstance(doc['methods'], list) and doc['methods']
          and set(doc['methods']) <= set(FORK_METHODS)
          and (v2 or (type(doc['forkBlock']) is int and doc['forkBlock'] > 0
                      and isinstance(doc['forkBlockHash'], str) and _HASH.fullmatch(doc['forkBlockHash']) is not None))
          and type(doc['maxHttpPosts']) is int and doc['maxHttpPosts'] > 0
          and type(doc['maxRestarts']) is int and doc['maxRestarts'] >= 0
          and type(doc['pacingMs']) is int and doc['pacingMs'] >= tp.PACING_MS
          and isinstance(doc['retention'], str) and isinstance(doc['statement'], str))
    if not ok:
        raise _stop('AUTHORIZATION_KEYS', str(path))
    if v2:
        scope.check_fork_authorization(doc)
    if doc['mode'] == 'SIMULATION':
        if 'SIMULATION' not in doc['statement']:
            raise _stop('AUTHORIZATION_KEYS', 'a simulation authorization must say so')
        native_rpc._endpoint(doc['endpoint'], True, True)          # loopback upstream only
    else:
        if doc['endpoint'] not in (PUBLIC_RPC[doc['chain']], scope.endpoint(doc['chain'])):
            raise _stop('FORK_ENDPOINT', f'{doc["endpoint"]!r} is not the allowlisted endpoint of {doc["chain"]}')
        if sha256 not in approved:
            raise _stop('FR_D1_NOT_AUTHORIZED', 'owner FR-D1 authorization of a real-state Fork is not approved')
    return {**doc, 'sha256': sha256}


def require_export_root(path, area):
    """P1-D04: ``path`` must be inside ``<export>/cache_forge/rehearsal/fork-campaign/<area>/`` of an approved export."""
    path = Path(path)
    if not path.is_absolute():
        raise _stop('EXPORT_ROOT', f'{path} is not absolute')
    probe = path
    while probe != probe.parent:
        if probe.is_symlink():
            raise _stop('EXPORT_ROOT_SYMLINK', str(probe))
        probe = probe.parent
    resolved = path.resolve()
    for export in resolved.parents:
        if (export / 'EXPORT-MANIFEST.txt').is_file():
            if (export / '.git').exists():
                raise _stop('EXPORT_ROOT', f'{export} is a repository checkout, not an export')
            base = export / 'cache_forge/rehearsal/fork-campaign' / area
            if resolved == base or base in resolved.parents:
                return export
            raise _stop('EXPORT_ROOT', f'{resolved} is not below {base}')
    raise _stop('EXPORT_ROOT', f'{resolved} is not inside an approved export')


# --------------------------------------------------------------------------------------------- the proxy
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _stop('FORK_REDIRECT', newurl, 'STOP')


def http_forward(url, raw, timeout_s):
    """POST ``raw`` to ``url`` with no environment proxy and no redirect. Returns (status, body bytes)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, data=raw, headers={'content-type': 'application/json'}, method='POST')
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class ForkBudgetProxy:
    def __init__(self, auth, ledger, *, forward=None, clock_s=time.monotonic, sleep=time.sleep):
        self.auth, self.ledger = auth, ledger
        self.upstream = auth['endpoint']
        if auth['mode'] != 'SIMULATION' and auth['sha256'] not in APPROVED_FORK_AUTHORIZATIONS:
            raise _stop('FR_D1_NOT_AUTHORIZED', 'the proxy refuses a public upstream without an approved authorization')
        if auth['mode'] != 'SIMULATION' and 'scope' not in auth:
            raise _stop('FORK_SCOPE_REQUIRED', 'a public upstream needs a v2 authorization with a bound fork anchor')
        self.scoped = 'scope' in auth
        if self.scoped and not (auth.get('anchorBinding') and type(auth.get('forkBlock')) is int
                                and isinstance(auth.get('forkBlockHash'), str)):
            raise _stop('FORK_ANCHOR_NOT_BOUND', 'bind the fork block to the S1/S2 evidence first (bind_fork_anchor)',
                        'BLOCKED')
        if self.scoped and auth['mode'] != 'SIMULATION' and \
                auth['anchorBinding'].get('reconciliationSha256') not in approvals.RECONCILIATIONS:
            raise _stop('FORK_ANCHOR_NOT_BOUND', 'the fork anchor is not bound by an accepted reconciliation', 'BLOCKED')
        if ledger is not None and ledger.authorization.get('sha256') != auth['sha256']:
            raise _stop('FORK_LEDGER_FOREIGN', 'the budget ledger belongs to another authorization')
        self.forward = forward or http_forward
        self.clock, self.sleep = clock_s, sleep
        self.last = None
        self.methods_seen, self.refused, self.forwarded = {}, [], 0
        self.stopped = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('content-length', '0')))
                status, body = proxy.handle(raw)
                self.send_response(status)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(('127.0.0.1', 0), Handler)     # one request at a time
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, raw):
        try:
            body = json.loads(raw)
            items = body if isinstance(body, list) else [body]
            if not items or len(items) > tp.GROUP_LIMIT:
                raise _stop('FORK_BODY', f'{len(items)} items')
            for item in items:
                if not (isinstance(item, dict) and item.get('jsonrpc') == '2.0' and set(item) <= {'jsonrpc', 'id', 'method', 'params'}
                        and item.get('method') in self.auth['methods']):
                    raise _stop('FORK_METHOD', repr(item.get('method') if isinstance(item, dict) else item))
                if self.scoped:
                    scope.check_fork_request(item, self.auth['forkBlock'], self.auth['forkBlockHash'])
                self.methods_seen[item['method']] = self.methods_seen.get(item['method'], 0) + 1
            if self.stopped:
                raise self.stopped
            self.ledger.reserve(len(items), 'fork')                    # before anything is sent
            if self.last is not None:
                wait = self.auth['pacingMs'] / 1000 - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            status, out = self.forward(self.upstream, raw, 30)
            self.forwarded += 1
            if self.scoped:
                try:
                    scope.check_fork_response(items, out, self.auth['scope']['chainId'], self.auth['forkBlockHash'])
                except CollectionStop as stop:
                    self.stopped = stop                 # the upstream contradicts the bound anchor: final
                    raise
            return status, out
        except (BudgetExhausted, CollectionStop) as stop:
            if isinstance(stop, BudgetExhausted) and self.stopped is None:
                self.stopped = stop                 # the ceiling is final for this proxy
            self.refused.append({'code': stop.code, 'detail': stop.detail[:200]})
            return 429 if isinstance(stop, BudgetExhausted) else 403, json.dumps(
                {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32005 if isinstance(stop, BudgetExhausted) else -32601,
                                                         'message': f'{stop.status}: {stop.code}'}}).encode()
        except (ValueError, TypeError) as error:
            self.refused.append({'code': 'FORK_BODY', 'detail': str(error)[:200]})
            return 400, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"REFUSED: FORK_BODY"}}'


def simulation_authorization(directory, chain, endpoint, block, block_hash, *, ceiling=100_000, restarts=1):
    """A SIMULATION authorization over a loopback upstream (never valid for a public endpoint)."""
    doc = {'schema': SCHEMA, 'authorizationId': f'simulation-fork-{chain}', 'mode': 'SIMULATION', 'chain': chain,
           'endpoint': endpoint, 'methods': list(FORK_METHODS), 'forkBlock': block, 'forkBlockHash': block_hash,
           'maxHttpPosts': ceiling, 'maxRestarts': restarts, 'pacingMs': tp.PACING_MS,
           'retention': 'local test directory only', 'statement': 'SIMULATION ONLY: loopback upstream, not FR-D1'}
    path = Path(directory) / f'{doc["authorizationId"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, sort_keys=True))
    return load_authorization(path, hashlib.sha256(path.read_bytes()).hexdigest())
