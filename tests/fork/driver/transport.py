"""The only network boundary of the Fork-campaign collector. Test-only; it never runs without an approved authorization.

Every HTTP POST of a collection passes through one ``HttpGate`` per chain run:

- the endpoint is the chain's reviewed public endpoint, resolved by the native ``_endpoint`` (exact
  allowlist, credentials refused); nothing is read from the environment;
- the native ``_post`` sends it (no proxy discovery, redirects refused); a 3xx status from an injected
  transport is refused the same way;
- one POST is in flight at a time, at least ``PACING_MS`` apart across all callers;
- each POST, retries included, is reserved in a persistent ``BudgetLedger`` *before* it is sent.

``ReleaseRpc`` is used unchanged with ``post=gate.post``, so its allowlist, ``LOG_RANGE`` guard, 20-item
groups, response-ID correlation and two-retry policy all still apply. ``Lane`` serializes every caller of
that one ``ReleaseRpc`` and proves from the gate's send log that each gathered group of at most 20 calls
went out as exactly one JSON-RPC batch. ``LogsAdapter`` turns a chunk of windows into validated rows, or
raises ``CollectionStop``; it never retries by itself and splits a window only when the provider message
is in the reviewed classifier, which is empty. ``BalanceReader`` is the single ``eth_getBalance`` path:
one address, one block, the ``ReleaseRpc`` retry policy, and ``NOT_OBSERVED`` (never zero) when the method
is unavailable.

The ledger, not the process, holds the request count. It is append-only, hash-chained, fsynced before each
POST, guarded against concurrent use and checked against a high-water mark and the run journals, so neither
a restart, a new invocation nor a truncated file resets it.
"""
import asyncio
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.lib import release_json as j  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release import logscan  # noqa: E402
from tools.release.native import rpc as native_rpc  # noqa: E402
from tools.release.native.history import validate_rows  # noqa: E402

TransportError = native_rpc.TransportError
ReleaseError = j.ReleaseError
CHAINS = {'mainnet': 1672, 'testnet': 688689}
PACING_MS = 350
TIMEOUT_MS = 30000
GROUP_LIMIT = 20
WINDOW_LIMIT = 1000           # inclusive blocks per eth_getLogs request (ReleaseRpc: toBlock - fromBlock < 1000)
MAX_DEPTH = 10
# FR-D1 read methods. eth_estimateGas is a native ReleaseRpc method but not part of a collection.
COLLECTION_METHODS = frozenset(('eth_chainId', 'eth_getBlockByNumber', 'eth_getLogs', 'eth_getCode',
                                'eth_getStorageAt', 'eth_call', 'eth_getTransactionByHash'))
BALANCE_METHOD = 'eth_getBalance'
METHOD_UNAVAILABLE = -32601
NOT_OBSERVED = 'NOT_OBSERVED'
# CR-D9 (a): reviewed exact provider messages that mean "range or response too large". Empty: the first real
# refusal stops with its message journaled; Agent A reviews it and a separate change may add it here.
REVIEWED_SIZE_ERRORS = frozenset()
# The ReleaseRpc failures that end a chunk without fallback (retries exhausted or a broken envelope).
TERMINAL_CODES = frozenset(('RPC_HTTP', 'RPC_BATCH_INCOMPLETE', 'RPC_RESPONSE_ID', 'RPC_MISSING_RESULT'))
ROW_KEYS = ('address', 'topics', 'data', 'blockNumber', 'blockHash', 'transactionHash', 'transactionIndex',
            'logIndex', 'removed')
_QUANTITY = re.compile(r'0x(0|[1-9a-f][0-9a-f]*)')


class CollectionStop(Exception):
    """A clean stop. ``status`` is STOP (resumable), BLOCKED, REFUSED or INCOMPLETE; nothing partial was written."""

    def __init__(self, code, detail='', status='STOP'):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(f'{status}: {code}' + (f': {detail}' if detail else ''))


class BudgetExhausted(CollectionStop):
    """The authorized POST ceiling is reached. The run is resumable only under a new authorization."""

    def __init__(self, detail):
        super().__init__('HTTP_BUDGET_EXHAUSTED', detail, 'STOP')


def normalized_message(detail):
    return ' '.join(str(detail).split())


def line_sha(line):
    return hashlib.sha256(line).hexdigest()


def _canonical_line(record):
    return (json.dumps(record, separators=(',', ':'), sort_keys=True) + '\n').encode()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BudgetLedger:
    """Cumulative POST count of one authorized collection (all chains' runs that share the authorization).

    Files below ``directory``: ``<id>.jsonl`` (append-only, each line carries the SHA-256 of the previous
    line), ``<id>.hwm.json`` (the highest sequence ever written and its line digest) and ``<id>.lock``
    (present while an invocation holds the ledger). The first line fixes the ceiling and the restart bound.
    """

    SCHEMA = 'faroo-fork-http-budget-v1'

    def __init__(self, directory, authorization, *, invocation, clock_ms=None):
        self.directory = Path(directory)
        self.authorization = authorization
        self.id = authorization['authorizationId']
        self.ceiling = authorization['maxHttpPosts']
        self.max_restarts = authorization['maxRestarts']
        self.invocation = invocation
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self.path = self.directory / f'{self.id}.jsonl'
        self.hwm = self.directory / f'{self.id}.hwm.json'
        self.lock = self.directory / f'{self.id}.lock'
        self.used = 0
        self.opens = 0
        self.seq = -1
        self.head = '0' * 64
        self.genesis = None
        self._fd = None
        self._held = False

    # -- lifecycle -------------------------------------------------------------------------------------
    def open(self, known_heads=(), expected_genesis=None):
        """Takes the exclusive lock, verifies the chain, the high-water mark and every (seq, sha) the run
        journals recorded, then journals this invocation. Refuses at the ceiling or past the restart bound."""
        if self._held:
            raise CollectionStop('BUDGET_LEDGER_REOPENED', self.id, 'REFUSED')
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise CollectionStop('BUDGET_LEDGER_LOCKED', f'{self.lock.name} exists: another invocation holds the ledger '
                                 'or a stale lock needs manual review', 'REFUSED') from None
        try:
            os.write(fd, json.dumps({'invocation': self.invocation, 'pid': os.getpid()}).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        self._held = True
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EACCES):
                    raise CollectionStop('BUDGET_LEDGER_LOCKED', 'ledger file is locked', 'REFUSED') from None
                raise
            self._load(known_heads, expected_genesis)
            if self.genesis is None:
                self._append({'kind': 'genesis', 'schema': self.SCHEMA, 'authorizationId': self.id,
                              'authorizationSha256': self.authorization['sha256'], 'ceiling': self.ceiling,
                              'maxRestarts': self.max_restarts})
                self.genesis = self.head
            if self.used >= self.ceiling:
                self._append({'kind': 'refused', 'reason': 'ceiling reached at open', 'invocation': self.invocation})
                raise BudgetExhausted(f'{self.used}/{self.ceiling} POSTs already used')
            if self.opens > self.max_restarts:
                self._append({'kind': 'refused', 'reason': 'restart bound', 'invocation': self.invocation})
                raise CollectionStop('SUPERVISOR_BOUND', f'{self.opens} invocations already opened; at most '
                                     f'{self.max_restarts} restarts are authorized', 'REFUSED')
            self.opens += 1
            self._append({'kind': 'open', 'invocation': self.invocation, 'restart': self.opens - 1})
        except BaseException:
            self.close(record=False)
            raise
        return self

    def _load(self, known_heads, expected_genesis=None):
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        if raw and not raw.endswith(b'\n'):
            raise CollectionStop('BUDGET_LEDGER_CORRUPT', 'truncated last line', 'REFUSED')
        prev, digests = '0' * 64, []
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise CollectionStop('BUDGET_LEDGER_CORRUPT', f'line {index}', 'REFUSED') from None
            if (not isinstance(record, dict) or record.get('seq') != index or record.get('prev') != prev
                    or _canonical_line(record) != line):
                raise CollectionStop('BUDGET_LEDGER_CORRUPT', f'line {index} breaks the chain', 'REFUSED')
            kind = record.get('kind')
            if index == 0:
                if (kind != 'genesis' or record.get('authorizationId') != self.id
                        or record.get('authorizationSha256') != self.authorization['sha256']
                        or record.get('ceiling') != self.ceiling or record.get('maxRestarts') != self.max_restarts):
                    raise CollectionStop('BUDGET_LEDGER_FOREIGN', 'genesis differs from the authorization', 'REFUSED')
                self.genesis = line_sha(line)
            elif kind == 'attempt':
                self.used += 1
            elif kind == 'open':
                self.opens += 1
            elif kind not in ('close', 'refused'):
                raise CollectionStop('BUDGET_LEDGER_CORRUPT', f'line {index}: kind {kind!r}', 'REFUSED')
            prev = line_sha(line)
            digests.append(prev)
        self.seq, self.head = len(lines) - 1, prev
        if expected_genesis is not None and self.genesis != expected_genesis:
            raise CollectionStop('JOURNAL_LEDGER', 'the run root was collected under another budget ledger', 'REFUSED')
        marks = list(known_heads)
        if self.hwm.exists():
            mark = json.loads(self.hwm.read_text())
            marks.append((mark['seq'], mark['sha256']))
        elif lines:
            raise CollectionStop('BUDGET_LEDGER_ROLLBACK', 'high-water mark missing for a non-empty ledger', 'REFUSED')
        for seq, sha in marks:
            if not (isinstance(seq, int) and 0 <= seq < len(digests) and digests[seq] == sha):
                raise CollectionStop('BUDGET_LEDGER_ROLLBACK', f'recorded ledger position {seq} is not in the ledger',
                                     'REFUSED')

    def _append(self, record):
        if not self._held or self._fd is None:
            raise CollectionStop('BUDGET_LEDGER_NOT_OPEN', self.id, 'REFUSED')
        record = {**record, 'seq': self.seq + 1, 'prev': self.head, 'atMs': int(self._clock())}
        line = _canonical_line(record)
        os.write(self._fd, line)
        os.fsync(self._fd)
        self.seq, self.head = self.seq + 1, line_sha(line)
        tmp = self.hwm.with_name(self.hwm.name + '.tmp')
        tmp.write_text(json.dumps({'seq': self.seq, 'sha256': self.head}))
        with open(tmp, 'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, self.hwm)
        _fsync_dir(self.directory)
        return self.seq, self.head

    def reserve(self, items, purpose):
        """Records one POST before it is sent. At the ceiling it raises before anything is sent."""
        if self.used >= self.ceiling:
            raise BudgetExhausted(f'{self.used}/{self.ceiling} POSTs used')
        self._append({'kind': 'attempt', 'items': items, 'purpose': purpose, 'invocation': self.invocation})
        self.used += 1
        return self.seq

    def position(self):
        return self.seq, self.head

    def close(self, record=True):
        try:
            if record and self._held and self._fd is not None:
                self._append({'kind': 'close', 'invocation': self.invocation, 'used': self.used})
        finally:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
            if self._held:
                self._held = False
                try:
                    self.lock.unlink()
                except FileNotFoundError:
                    pass


class HttpGate:
    """Every POST of one chain run. ``post`` is injectable only for offline simulation; the default is the
    native ``_post``, which a gate refuses to use unless the ledger's authorization is an approved one."""

    def __init__(self, chain, ledger, *, clock_ms=None, sleep=None, post=None, approved=frozenset()):
        if chain not in CHAINS:
            raise CollectionStop('GATE_CHAIN', str(chain), 'REFUSED')
        self.chain = chain
        self.url = PUBLIC_RPC[chain]
        public, self.resolved = native_rpc._endpoint(self.url, False, False)
        if not public:
            raise CollectionStop('GATE_ENDPOINT', self.url, 'REFUSED')
        self.simulation = post is not None
        if not self.simulation and ledger.authorization.get('sha256') not in approved:
            raise CollectionStop('FR_D1_NOT_AUTHORIZED', 'no approved owner authorization for public read-only '
                                 'collection; the native transport is refused', 'REFUSED')
        self.ledger = ledger
        self._post = post or native_rpc._post
        self._clock = clock_ms or (lambda: time.time() * 1000)
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._last = None
        self._balance = set()
        self.sent = []               # items per POST, in send order

    async def post(self, url, body, timeout_ms):
        """The ``post`` callable of ``ReleaseRpc(PUBLIC_RPC[chain], post=gate.post)``."""
        try:
            resolved = native_rpc._endpoint(url, False, False)[1]
        except (ReleaseError, TransportError):
            resolved = None
        if resolved != self.resolved:
            raise CollectionStop('GATE_ENDPOINT', f'{url!r} is not the endpoint of {self.chain}', 'REFUSED')
        items = body if isinstance(body, list) else [body]
        if not items or len(items) > GROUP_LIMIT:
            raise CollectionStop('GATE_BODY', f'{len(items)} items', 'REFUSED')
        for item in items:
            if not (isinstance(item, dict) and set(item) == {'jsonrpc', 'id', 'method', 'params'}
                    and item['jsonrpc'] == '2.0' and item['method'] in COLLECTION_METHODS
                    and item['method'] in self.ledger.authorization['methods']):
                raise CollectionStop('GATE_METHOD', repr(item.get('method') if isinstance(item, dict) else item),
                                     'REFUSED')
        return await self._send(body, len(items), 'rpc', timeout_ms)

    def permit_balance(self, addresses, block):
        for address in addresses:
            self._balance.add((address.lower(), int(block)))

    async def post_balance(self, body, timeout_ms):
        ok = (isinstance(body, dict) and set(body) == {'jsonrpc', 'id', 'method', 'params'} and body['jsonrpc'] == '2.0'
              and body['method'] == BALANCE_METHOD and BALANCE_METHOD in self.ledger.authorization['methods'] and isinstance(body['params'], list) and len(body['params']) == 2
              and isinstance(body['params'][0], str) and isinstance(body['params'][1], str)
              and _QUANTITY.fullmatch(body['params'][1]) is not None
              and (body['params'][0].lower(), int(body['params'][1], 16)) in self._balance)
        if not ok:
            raise CollectionStop('GATE_BALANCE_REFUSED', 'eth_getBalance outside the permitted address and block',
                                 'REFUSED')
        return await self._send(body, 1, 'balance', timeout_ms)

    async def _send(self, body, items, purpose, timeout_ms):
        async with self._lock:
            if self._last is not None:
                wait = PACING_MS - (self._clock() - self._last)
                if wait > 0:
                    await self._sleep(wait / 1000)
            self.ledger.reserve(items, purpose)
            self._last = self._clock()
            self.sent.append(items)
            try:
                status, response = await asyncio.wait_for(self._post(self.resolved, body, timeout_ms), timeout_ms / 1000)
            except asyncio.TimeoutError:
                raise TransportError('TimeoutError', 'The operation was aborted due to timeout') from None
            if not isinstance(status, int) or 300 <= status < 400:
                raise TransportError('TypeError', 'fetch failed')
            return status, response


def release_rpc(gate, *, clock_ms=None, sleep=None):
    """The native transport bound to the gate (the only way the collector builds one)."""
    return native_rpc.ReleaseRpc(gate.url, post=gate.post, clock_ms=clock_ms or gate._clock,
                                 sleep=sleep or gate._sleep)


class Lane:
    """Serializes all callers of one ReleaseRpc. ``group`` gathers at most 20 calls from one coroutine and
    checks in the gate log that they left as a single batch (every POST since, retries included, carried
    exactly that many items). Returns each result or exception in call order."""

    def __init__(self, rpc, gate):
        self.rpc, self.gate = rpc, gate
        self._lock = asyncio.Lock()

    async def group(self, calls):
        if not 1 <= len(calls) <= GROUP_LIMIT:
            raise CollectionStop('GROUP_SIZE', str(len(calls)), 'REFUSED')
        async with self._lock:
            mark = len(self.gate.sent)
            results = await asyncio.gather(*(self.rpc(method, params) for method, params in calls),
                                           return_exceptions=True)
            sent = self.gate.sent[mark:]
        if any(n != len(calls) for n in sent):
            raise CollectionStop('GROUP_ASSEMBLY', f'{len(calls)} calls left as POSTs of {sent}')
        for result in results:
            if isinstance(result, CollectionStop):
                raise result
        return results

    async def values(self, calls):
        """All results, or a STOP naming the failure. Used for every read except logs."""
        results = await self.group(calls)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            raise stop_for(failures[0])
        return results

    async def call(self, method, params):
        return (await self.values([(method, params)]))[0]


def stop_for(error):
    if isinstance(error, CollectionStop):
        return error
    if isinstance(error, ReleaseError):
        return CollectionStop(error.code, _error_detail(error))
    if isinstance(error, TransportError):
        return CollectionStop('TRANSPORT_ERROR', f'{error.name}: {error}')
    return CollectionStop('UNEXPECTED_ERROR', f'{type(error).__name__}: {error}', 'REFUSED')


def _error_detail(error):
    """The provider message of a ReleaseError (``code: message``) or the text of any other error."""
    if isinstance(error, CollectionStop):
        return normalized_message(error.detail)
    text = str(error)
    if isinstance(error, ReleaseError):
        prefix = error.code + ': '
        text = text[len(prefix):] if text.startswith(prefix) else ''
    return normalized_message(text)


def check_rows(rows, addresses, a, b):
    """Slims and validates one window's result with the native row rule; any defect is a STOP."""
    if not isinstance(rows, list):
        raise CollectionStop('LOG_RESULT_TYPE', f'[{a}, {b}]: {type(rows).__name__}')
    out = []
    for row in rows:
        if not isinstance(row, dict) or any(k not in row for k in ROW_KEYS):
            raise CollectionStop('LOG_ROW_KEYS', f'[{a}, {b}]')
        if row['removed'] is not False:
            raise CollectionStop('LOG_REMOVED', f'[{a}, {b}] block {row["blockNumber"]}')
        out.append(logscan.slim(row))
    try:
        validate_rows(out, addresses, a, b)
    except ReleaseError as error:
        raise CollectionStop(error.code, f'[{a}, {b}]') from None
    except (KeyError, TypeError, ValueError, TransportError) as error:
        raise CollectionStop('HISTORY_LOG_IDENTITY', f'[{a}, {b}]: {error!r}') from None
    return out


class LogsAdapter:
    """Asynchronous eth_getLogs over a Lane (COLLECTION-DESIGN-PROPOSAL.md §3.4)."""

    def __init__(self, lane, *, classifier=REVIEWED_SIZE_ERRORS, max_depth=MAX_DEPTH, journal=None):
        self.lane, self.classifier, self.max_depth = lane, frozenset(classifier), max_depth
        self.journal = journal or (lambda event: None)

    @staticmethod
    def _params(addresses, a, b):
        return [{'address': sorted(addresses), 'fromBlock': hex(a), 'toBlock': hex(b)}]

    async def chunk(self, addresses, windows):
        """``windows``: 1..20 contiguous canonical windows. Returns [(a, b, count, rows)] tiling them exactly."""
        windows = [tuple(w) for w in windows]
        if not 1 <= len(windows) <= GROUP_LIMIT:
            raise CollectionStop('CHUNK_WINDOWS', f'{len(windows)} windows', 'REFUSED')
        for i, (a, b) in enumerate(windows):
            if not (isinstance(a, int) and isinstance(b, int) and 0 <= b - a < WINDOW_LIMIT
                    and (i == 0 or a == windows[i - 1][1] + 1)):
                raise CollectionStop('CHUNK_WINDOWS', f'window [{a}, {b}]', 'REFUSED')
        results = await self.lane.group([('eth_getLogs', self._params(addresses, a, b)) for a, b in windows])
        errors = [r for r in results if isinstance(r, BaseException)]
        if not errors:
            out = [(a, b, len(rows), rows) for (a, b), rows in
                   zip(windows, (check_rows(r, addresses, a, b) for r, (a, b) in zip(results, windows)))]
        elif len(errors) != len(results):
            raise CollectionStop('MIXED_GROUP_RESULT', f'chunk [{windows[0][0]}, {windows[-1][1]}]: '
                                 f'{len(results) - len(errors)} values and {len(errors)} errors')
        elif all(isinstance(e, TransportError) for e in errors) or all(
                isinstance(e, ReleaseError) and e.code == 'RPC_ERROR' for e in errors):
            first = errors[0]
            self.journal({'event': 'perWindowFallback', 'from': windows[0][0], 'to': windows[-1][1],
                          'cause': first.code if isinstance(first, ReleaseError) else f'TransportError:{first.name}',
                          'detail': _error_detail(first)})
            out = []
            for a, b in windows:
                out += await self.window(addresses, a, b)
        elif all(isinstance(e, ReleaseError) and e.code in TERMINAL_CODES for e in errors):
            raise stop_for(errors[0])
        else:
            first = errors[0]
            raise CollectionStop('ADAPTER_DEFECT', f'{type(first).__name__}: {first}', 'REFUSED')
        self._check_chunk(addresses, windows, out)
        return out

    async def window(self, addresses, a, b, depth=0):
        [result] = await self.lane.group([('eth_getLogs', self._params(addresses, a, b))])
        if not isinstance(result, BaseException):
            rows = check_rows(result, addresses, a, b)
            return [(a, b, len(rows), rows)]
        if isinstance(result, ReleaseError) and result.code == 'RPC_ERROR' \
                and normalized_message(_error_detail(result)) in self.classifier:
            if a == b:
                raise CollectionStop('SINGLE_BLOCK_REFUSED', f'block {a}: {_error_detail(result)}')
            if depth >= self.max_depth:
                raise CollectionStop('SUBDIVISION_DEPTH', f'[{a}, {b}] at depth {depth}')
            middle = (a + b) // 2
            self.journal({'event': 'split', 'from': a, 'to': b, 'at': middle, 'depth': depth,
                          'detail': _error_detail(result)})
            return await self.window(addresses, a, middle, depth + 1) + \
                await self.window(addresses, middle + 1, b, depth + 1)
        self.journal({'event': 'windowStop', 'from': a, 'to': b, 'class': type(result).__name__,
                      'code': getattr(result, 'code', getattr(result, 'name', '')), 'detail': _error_detail(result)})
        if isinstance(result, ReleaseError) and result.code == 'RPC_ERROR':
            raise CollectionStop('UNCLASSIFIED_RPC_ERROR', f'[{a}, {b}]: {_error_detail(result)}')
        raise stop_for(result)

    @staticmethod
    def _check_chunk(addresses, windows, out):
        """Sub-windows tile every canonical window exactly; rows are unique across the chunk."""
        cursor = windows[0][0]
        for a, b, _, _ in out:
            if a != cursor or b < a:
                raise CollectionStop('CHUNK_TILING', f'sub-window [{a}, {b}] at {cursor}')
            cursor = b + 1
        if cursor != windows[-1][1] + 1:
            raise CollectionStop('CHUNK_TILING', f'ends at {cursor - 1}')
        for a, b, _, _ in out:
            # a sub-window never crosses a canonical window boundary
            if not any(s <= a and b <= e for s, e in windows):
                raise CollectionStop('CHUNK_TILING', f'sub-window [{a}, {b}] crosses a canonical window')
        rows = [r for _, _, _, rs in out for r in rs]
        try:
            validate_rows(rows, addresses, windows[0][0], windows[-1][1])
        except ReleaseError as error:
            raise CollectionStop(error.code, 'across the chunk') from None


class BalanceReader:
    """The one eth_getBalance of a collection: the StPROS proxy at the anchor (CR-D10 option A)."""

    def __init__(self, gate, chain, allowed_addresses, block, *, sleep=None):
        if type(block) is not int or block <= 0:
            raise CollectionStop('BALANCE_BLOCK', repr(block), 'REFUSED')
        self.gate, self.chain, self.block = gate, chain, block
        self.allowed = frozenset(a.lower() for a in allowed_addresses)
        self._sleep = sleep or gate._sleep
        self._id = 10 ** 9
        gate.permit_balance(self.allowed, block)

    async def get(self, address, block):
        """The balance as an int, or NOT_OBSERVED if the provider lacks the method. Anything else is a STOP."""
        if not isinstance(address, str) or address.lower() not in self.allowed:
            raise CollectionStop('BALANCE_ADDRESS', repr(address), 'REFUSED')
        if type(block) is not int or block != self.block:
            raise CollectionStop('BALANCE_BLOCK', repr(block), 'REFUSED')
        self._id += 1
        rid = self._id                      # local: concurrent reads never share an expected ID
        body = {'jsonrpc': '2.0', 'id': rid, 'method': BALANCE_METHOD, 'params': [address.lower(), hex(block)]}
        for attempt in range(3):
            status, response = await self.gate.post_balance(body, TIMEOUT_MS)
            retry = status in native_rpc.RETRY_HTTP or (
                isinstance(response, dict) and isinstance(response.get('error'), dict)
                and response['error'].get('code') in native_rpc.RETRY_RPC)
            if retry and attempt < 2:
                await self._sleep(2 ** attempt)
                continue
            if not 200 <= status < 300:
                raise CollectionStop('RPC_HTTP', str(status))
            break
        if not (isinstance(response, dict) and response.get('jsonrpc') == '2.0'
                and native_rpc._same_id(response.get('id'), rid)
                and (('result' in response) != ('error' in response))):
            raise CollectionStop('BALANCE_ENVELOPE', 'malformed eth_getBalance response')
        if 'error' in response:
            error = response['error']
            if isinstance(error, dict) and error.get('code') == METHOD_UNAVAILABLE:
                return NOT_OBSERVED
            raise CollectionStop('RPC_ERROR', normalized_message(error.get('message', '') if isinstance(error, dict) else ''))
        result = response['result']
        if not isinstance(result, str) or _QUANTITY.fullmatch(result) is None:
            raise CollectionStop('BALANCE_QUANTITY', repr(result))
        return int(result, 16)
