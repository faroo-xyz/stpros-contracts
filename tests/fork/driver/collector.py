"""Read-only collector of one chain's campaign history at an anchor ``B`` (COLLECTION-DESIGN-PROPOSAL.md §4). Test-only.

``Collector.run`` executes the reviewed spec, cheapest disqualifier first, and writes below
``<historyRoot>/<runId>/`` only:

  spec.json, segments/<id>/plan.json, segments/<id>/chunks/*.json, headers/part-*.json, headers.json,
  gas-envelope.json, observation.json, state-observation.json, inventory/*.json, inventory.json, manifest.json,
  journal.jsonl

The immutable B0 archive is referenced in place (``<historyRoot>/<b0Dir>/``) by its pinned SHA-256 values and
is never copied or rewritten. Every network read goes through ``transport`` (one ``HttpGate`` with a persistent
``BudgetLedger``); nothing reads the environment, a key or a wallet, and nothing is ever sent.

A real collection needs an owner FR-D1 authorization whose SHA-256 is pinned in
``APPROVED_COLLECTION_AUTHORIZATIONS`` (``fr_d1_approvals.COLLECTION``); that set is empty, so only a SIMULATION
authorization over an injected offline transport can run. A public collection also needs the v2 schema
(``fr_d1_scope``): its campaign envelope is checked against the code, the spec against the envelope, and the anchor
and spec SHA-256 are pinned in the budget ledger before the run's first read. Statuses written here are ``COLLECTED`` and ``CONTENT_VERIFIED``; the state
observation's B-local section statuses come from ``state_observation.check``. No code path writes a C-level or
approval status.

Resume re-validates every cached plan, chunk and header part before reuse: a leftover ``.tmp`` is discarded and
refetched, an invalid cached file is a STOP (never a silent refetch), a plan is write-once, and a run root is
bound to one spec SHA-256.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collection_plan as cp  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import headers as hd  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import inventory as inv  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib import release_json as j  # noqa: E402

AUTHORIZATION_SCHEMA = 'faroo-fork-collection-authorization-v1'
AUTH_KEYS = frozenset({'schema', 'authorizationId', 'mode', 'chains', 'methods', 'maxHttpPosts', 'maxRestarts',
                       'statement'})
AUTH_MODES = ('SIMULATION', 'PUBLIC_READ_ONLY')
# FR-D1 owner authorizations of public read-only collection, pinned by a separate reviewed change in
# ``fr_d1_approvals.py``. Empty: no real collection is authorized. Never add an entry from this package.
APPROVED_COLLECTION_AUTHORIZATIONS = approvals.COLLECTION
AUTHORIZED_METHODS = tuple(sorted(tp.COLLECTION_METHODS | {tp.BALANCE_METHOD}))
JOURNAL_SCHEMA = 'faroo-fork-collection-journal-v1'
RESTARTABLE = frozenset(('RPC_HTTP', 'TRANSPORT_ERROR'))
EMPTY_CODE = ('0x', '0x0')
SETTLED = 3
_RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
ZERO_HASH = '0x' + '00' * 32
GAS_ENVELOPE_SCHEMA = 'faroo-fork-gas-envelope-v1'
TX_BASE_GAS, CREATE_GAS, CODE_DEPOSIT_GAS = 21_000, 32_000, 200
GAS_ENVELOPE_STATEMENT = ('provider data at collection time (OBSERVED); not a per-transaction cap approval. evmLowerBound '
                          'assumes the Ethereum gas schedule for creation (G-A1, NOT VERIFIED for Pharos)')


def _canonical(doc):
    return cp.canonical_bytes(doc)


def load_authorization(path, sha256, *, bind_sources=True):
    """Reads an authorization by raw SHA-256. PUBLIC_READ_ONLY is refused unless pinned as approved. A v2 record
    (``fr_d1_scope.COLLECTION_SCHEMA``) is checked against the code's envelope first."""
    _, doc = scope.read_record(path, sha256, 'AUTHORIZATION_HASH')
    v2 = isinstance(doc, dict) and doc.get('schema') == scope.COLLECTION_SCHEMA
    ok = (isinstance(doc, dict) and set(doc) == (scope.COLLECTION_KEYS if v2 else AUTH_KEYS)
          and doc['schema'] in (AUTHORIZATION_SCHEMA, scope.COLLECTION_SCHEMA)
          and doc['mode'] in AUTH_MODES and isinstance(doc['authorizationId'], str)
          and _RUN_ID.fullmatch(doc['authorizationId']) is not None
          and isinstance(doc['chains'], list) and doc['chains'] and set(doc['chains']) <= set(h.CHAINS)
          and isinstance(doc['methods'], list) and set(doc['methods']) <= set(AUTHORIZED_METHODS)
          and type(doc['maxHttpPosts']) is int and doc['maxHttpPosts'] > 0
          and type(doc['maxRestarts']) is int and doc['maxRestarts'] >= 0 and isinstance(doc['statement'], str))
    if not ok:
        raise CollectionStop('AUTHORIZATION_KEYS', str(path), 'REFUSED')
    if doc['mode'] == 'SIMULATION' and 'SIMULATION' not in doc['statement']:
        raise CollectionStop('AUTHORIZATION_KEYS', 'a simulation authorization must say so', 'REFUSED')
    if v2:
        scope.check_collection_authorization(doc, bind_sources=bind_sources)
    if doc['mode'] == 'PUBLIC_READ_ONLY' and sha256 not in APPROVED_COLLECTION_AUTHORIZATIONS:
        raise CollectionStop('FR_D1_NOT_AUTHORIZED', 'owner FR-D1 authorization is not approved', 'REFUSED')
    return {**doc, 'sha256': sha256}


def _inside(root, path):
    root, path = Path(root), Path(path)
    probe = path
    while probe != root:
        if probe.is_symlink():
            raise CollectionStop('RUN_ROOT_SYMLINK', str(probe), 'REFUSED')
        if probe.parent == probe:
            raise CollectionStop('WRITE_OUTSIDE_RUN_ROOT', str(path), 'REFUSED')
        probe = probe.parent
    if not str(path.resolve()).startswith(str(Path(root).resolve()) + os.sep):
        raise CollectionStop('WRITE_OUTSIDE_RUN_ROOT', str(path), 'REFUSED')


def atomic_write(root, relative, data):
    """Writes ``root/relative`` through a fsynced ``.tmp`` and a rename. Refuses anything outside ``root``."""
    if relative.startswith('/') or '..' in relative.split('/') or '\\' in relative:
        raise CollectionStop('WRITE_OUTSIDE_RUN_ROOT', relative, 'REFUSED')
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    _inside(root, path)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    tp._fsync_dir(path.parent)
    return hashlib.sha256(data).hexdigest()


class Journal:
    """Append-only, hash-chained diagnostics of one run root (excluded from every comparison)."""

    def __init__(self, path, clock_ms):
        self.path, self._clock = Path(path), clock_ms
        self.records, self.head = [], '0' * 64
        if self.path.exists():
            prev = '0' * 64
            for i, line in enumerate(self.path.read_bytes().splitlines(keepends=True)):
                record = json.loads(line)
                if record.get('seq') != i or record.get('prev') != prev or tp._canonical_line(record) != line:
                    raise CollectionStop('JOURNAL_CHAIN', f'line {i}', 'REFUSED')
                prev = tp.line_sha(line)
                self.records.append(record)
            self.head = prev

    def append(self, event, **fields):
        record = {**fields, 'event': event, 'seq': len(self.records), 'prev': self.head, 'atMs': int(self._clock())}
        line = tp._canonical_line(record)
        with open(self.path, 'ab') as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.records.append(record)
        self.head = tp.line_sha(line)
        return record

    def ledger_positions(self):
        return [(r['ledgerSeq'], r['ledgerHead']) for r in self.records if 'ledgerSeq' in r]

    def ledger_genesis(self):
        found = {r['ledgerGenesis'] for r in self.records if 'ledgerGenesis' in r}
        if len(found) > 1:
            raise CollectionStop('JOURNAL_LEDGER', 'more than one budget ledger recorded', 'REFUSED')
        return next(iter(found), None)


def b0_segment(history_root, b0_dir, chain):
    """The manifest entry of the immutable B0 archive below ``history_root/b0_dir``, checked against the pin."""
    prefix = h.B0_PREFIX[chain]
    root = Path(history_root)
    plan_rel = f'{b0_dir}/plan.json'
    raw = (root / plan_rel).read_bytes()
    if hashlib.sha256(raw).hexdigest() != prefix['planSha256']:
        raise CollectionStop('B0_PREFIX_MODIFIED', 'B0 plan differs from the pinned archive', 'REFUSED')
    chunks = sorted((root / b0_dir / 'chunks').glob('*.json'), key=lambda p: p.name)
    refs = [{'path': str(p.relative_to(root)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in chunks]
    if len(refs) != prefix['chunkCount'] or h.chunk_list_sha256(r['sha256'] for r in refs) != prefix['chunkListSha256']:
        raise CollectionStop('B0_PREFIX_MODIFIED', 'B0 chunk list differs from the pinned archive', 'REFUSED')
    return {'planFile': plan_rel, 'planSha256': prefix['planSha256'], 'chunks': refs}, json.loads(raw)


def validate_chunk(doc, plan, a, b, addresses):
    """The rules a newly fetched chunk satisfies, re-applied to a cached one. Returns the rows."""
    ok = (isinstance(doc, dict) and set(doc) == h.CHUNK_KEYS and doc['chain'] == plan['chain']
          and doc['filter_id'] == plan['filter_id'] and doc['from'] == a and doc['to'] == b
          and isinstance(doc['windows'], list) and isinstance(doc['logs'], list) and isinstance(doc['fetched_utc'], str))
    if not ok:
        raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] identity')
    grid = cp.logscan.windows(a, b, cp.WINDOW)
    cursor = a
    for item in doc['windows']:
        if not (isinstance(item, list) and len(item) == 3 and all(type(x) is int for x in item)):
            raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] window entry')
        x, y, n = item
        if x != cursor or y < x or not any(s <= x and y <= e for s, e in grid):
            raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] window [{x}, {y}]')
        if sum(x <= int(r['blockNumber'], 16) <= y for r in doc['logs']) != n:
            raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] window count [{x}, {y}]')
        cursor = y + 1
    if cursor != b + 1:
        raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] windows end at {cursor - 1}')
    try:
        rows = tp.check_rows(doc['logs'], addresses, a, b)
    except CollectionStop as stop:
        raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] {stop.code}') from None
    if rows != doc['logs']:
        raise CollectionStop('CACHED_CHUNK_INVALID', f'[{a}, {b}] rows are not in slim form')
    return rows


def _header_entry(raw, number):
    ok = (isinstance(raw, dict) and isinstance(raw.get('number'), str) and isinstance(raw.get('timestamp'), str)
          and h.full_hash(raw.get('hash')) and h.full_hash(raw.get('parentHash')))
    if not ok or int(raw['number'], 16) != number:
        raise CollectionStop('HEADER_MISSING_OR_MISMATCH', f'block {number}')
    return {'number': number, 'hash': raw['hash'], 'parentHash': raw['parentHash'], 'timestamp': int(raw['timestamp'], 16)}


class Collector:
    def __init__(self, spec, spec_sha256, history_root, run_id, gate, *, b0_dir, utc, clock_ms=None,
                 observe=None, reviewed_runtimes=None, reviewed_state=None, classifier=None, collector='B',
                 reference=None):
        self.spec, self.spec_sha256 = spec, spec_sha256
        self.history_root = Path(history_root)
        if _RUN_ID.fullmatch(run_id) is None or run_id == b0_dir:
            raise CollectionStop('RUN_ID', run_id, 'REFUSED')
        self.run_id, self.run_root = run_id, self.history_root / run_id
        self.gate, self.b0_dir, self.utc = gate, b0_dir, utc
        self._clock = clock_ms or (lambda: time.time() * 1000)
        self.observe = observe
        self.reviewed_runtimes = reviewed_runtimes
        self.reviewed_state = reviewed_state
        self.classifier = tp.REVIEWED_SIZE_ERRORS if classifier is None else classifier
        self.collector = collector
        self.reference = reference          # S2: the verified S1 ledger reference (fr_d1_scope.s1_reference)
        self.chain = spec['chain']

    # -- step 0 ----------------------------------------------------------------------------------------
    def preflight(self):
        auth = self.gate.ledger.authorization
        if self.chain not in auth['chains']:
            raise CollectionStop('AUTHORIZATION_SCOPE', f'{self.chain} not authorized', 'REFUSED')
        if auth['mode'] == 'SIMULATION' and not self.gate.simulation:
            raise CollectionStop('FR_D1_NOT_AUTHORIZED', 'a simulation authorization cannot drive the native transport',
                                 'REFUSED')
        if auth['mode'] == 'PUBLIC_READ_ONLY' and (self.gate.simulation
                                                   or auth['sha256'] not in APPROVED_COLLECTION_AUTHORIZATIONS):
            raise CollectionStop('FR_D1_NOT_AUTHORIZED', 'owner FR-D1 authorization is not approved', 'REFUSED')
        if auth['mode'] == 'PUBLIC_READ_ONLY':
            # P1-D04: an authorized public collection writes its run root and its budget ledger only below
            # <export>/cache_forge/rehearsal/fork-campaign/collection/ of an approved export.
            import fork_mode
            fork_mode.require_export_root(self.history_root, 'collection')
            fork_mode.require_export_root(self.gate.ledger.directory, 'collection')
            if 'scope' not in auth:
                raise CollectionStop('AUTHORIZATION_SCOPE_REQUIRED', 'a public collection needs a v2 authorization '
                                     'that binds the campaign envelope', 'REFUSED')
        spec_bytes = _canonical(self.spec)
        if hashlib.sha256(spec_bytes).hexdigest() != self.spec_sha256:
            raise CollectionStop('SPEC_SHA', 'the spec differs from the reviewed SHA-256', 'REFUSED')
        self.b0_ref, b0_plan = b0_segment(self.history_root, self.b0_dir, self.chain)
        try:
            cp.check_spec(self.spec, b0_plan)
        except h.HistoryRefused as error:
            raise CollectionStop(error.code, str(error), 'REFUSED') from None
        if 'scope' in auth:
            scope.check_spec(self.spec, auth)
        self.b0_plan = b0_plan
        self.run_root.mkdir(parents=True, exist_ok=True)
        _inside(self.history_root, self.run_root / 'spec.json')
        existing = self.run_root / 'spec.json'
        if existing.exists():
            if existing.read_bytes() != spec_bytes:
                raise CollectionStop('SPEC_RUN_ROOT_MISMATCH', 'the run root belongs to another spec', 'REFUSED')
        else:
            atomic_write(self.run_root, 'spec.json', spec_bytes)
        self.journal = Journal(self.run_root / 'journal.jsonl', self._clock)

    # -- helpers ---------------------------------------------------------------------------------------
    def _ledger_mark(self):
        seq, head = self.gate.ledger.position()
        return {'ledgerSeq': seq, 'ledgerHead': head}

    async def _headers(self, numbers):
        out = {}
        for i in range(0, len(numbers), tp.GROUP_LIMIT):
            group = numbers[i:i + tp.GROUP_LIMIT]
            values = await self.lane.values([('eth_getBlockByNumber', [hex(n), False]) for n in group])
            for n, raw in zip(group, values):
                out[n] = _header_entry(raw, n)
        return out

    async def _code_before(self, addresses, block):
        """Addresses with code at ``block``. Any read error is BLOCKED (FR-X7), never 'no code'."""
        found = []
        for i in range(0, len(addresses), tp.GROUP_LIMIT):
            group = addresses[i:i + tp.GROUP_LIMIT]
            try:
                codes = await self.lane.values([('eth_getCode', [a, hex(block)]) for a in group])
            except CollectionStop as stop:
                if stop.code != 'RPC_ERROR':
                    raise                     # transport, budget and envelope failures are ordinary STOPs
                raise CollectionStop('ARCHIVE_STATE_UNAVAILABLE', f'getCode at {block}: {stop.code} {stop.detail} (FR-X7)',
                                     'BLOCKED') from None
            for address, code in zip(group, codes):
                if not isinstance(code, str) or not re.fullmatch(r'0x([0-9a-f]{2})*', code):
                    raise CollectionStop('ARCHIVE_STATE_UNAVAILABLE', f'getCode({address}, {block})', 'BLOCKED')
                if code not in EMPTY_CODE:
                    found.append(address)
        return found

    # -- steps 1-3 -------------------------------------------------------------------------------------
    async def anchor_checks(self):
        chain_id = await self.lane.call('eth_chainId', [])
        if not isinstance(chain_id, str) or int(chain_id, 16) != h.CHAINS[self.chain]:
            raise CollectionStop('RPC_CHAIN', repr(chain_id), 'REFUSED')
        latest = await self.lane.call('eth_getBlockByNumber', ['latest', False])
        if not isinstance(latest, dict) or not isinstance(latest.get('number'), str):
            raise CollectionStop('HEADER_MISSING_OR_MISMATCH', 'latest')
        self.head = int(latest['number'], 16)
        anchor = self.spec['anchor']
        if self.head - anchor['block'] < self.spec['finalityDepth']['blocks']:
            raise CollectionStop('ANCHOR_TOO_RECENT', f'head {self.head}, anchor {anchor["block"]}', 'REFUSED')
        pin = self.spec['pin']
        got = await self._headers(sorted({anchor['block'], pin['block']}))
        if got[anchor['block']]['hash'] != anchor['hash']:
            raise CollectionStop('HISTORY_REORG', 'anchor hash differs from the spec')
        if got[pin['block']]['hash'] != pin['hash']:
            raise CollectionStop('HISTORY_REORG', 'B0 pin hash differs from the pinned archive')
        self.anchor_header = got[anchor['block']]
        if self.chain == 'mainnet' and anchor['block'] > pin['block']:
            await self.gc01()

    async def gc01(self):
        """FR-X5: mainnet vault 001 must be SETTLED at B, by phase() and by the stored phase slot."""
        factory = h.registry('mainnet')['factories'][0]
        vault = next(v['vault'] for v in self.b0_plan['vaults'] if v['factory'] == factory and v['index'] == 0)
        block = hex(self.spec['anchor']['block'])
        phase, slot = await self.lane.values([('eth_call', [{'to': vault, 'data': abi.calldata('phase()')}, block]),
                                              ('eth_getStorageAt', [vault, hex(4), block])])
        if abi.dec_uint(phase) != SETTLED or int(slot, 16) & 0xff != SETTLED:
            raise CollectionStop('GC01_NOT_SETTLED', f'vault 001 phase {abi.dec_uint(phase)}, stored {int(slot, 16) & 0xff}',
                                 'BLOCKED')
        self.journal.append('gc01', vault=vault, phase=SETTLED)

    # -- steps 4-7 -------------------------------------------------------------------------------------
    async def ensure_plan(self, seg):
        rel = cp.plan_file(seg['id'])
        path = self.run_root / rel
        if path.exists():
            plan = json.loads(path.read_bytes())
            if not cp.plan_matches(self.spec, seg, plan) or _canonical(plan) != path.read_bytes():
                raise CollectionStop('PLAN_EXISTS_FOR_ANOTHER_SPEC', rel, 'REFUSED')
            return plan
        end = (await self._headers([seg['end']]))[seg['end']]
        if seg['end'] == self.spec['anchor']['block'] and end['hash'] != self.spec['anchor']['hash']:
            raise CollectionStop('HISTORY_REORG', f'{seg["id"]} end differs from the anchor')
        if seg['end'] == self.spec['pin']['block'] and end['hash'] != self.spec['pin']['hash']:
            raise CollectionStop('HISTORY_REORG', f'{seg["id"]} end differs from the B0 pin')
        addresses = [e['address'] for e in seg['addresses']]
        code = await self._code_before(addresses, seg['start'] - 1)
        if seg['start'] == self.spec['fromBlock'] and code:
            raise CollectionStop('CODE_BEFORE_FROM_BLOCK', f'{code}: no pre-fromBlock scan start without CR-D11', 'BLOCKED')
        plan = cp.plan_doc(self.spec, seg, end_hash=end['hash'], end_timestamp=end['timestamp'],
                           head_at_plan=self.head, code_before_start=code, created_utc=self.utc())
        digest = atomic_write(self.run_root, rel, _canonical(plan))
        self.journal.append('plan', segment=seg['id'], sha256=digest, **self._ledger_mark())
        return plan

    async def ensure_chunks(self, seg, plan):
        addresses = [e['address'] for e in seg['addresses']]
        adapter = tp.LogsAdapter(self.lane, classifier=self.classifier,
                                 journal=lambda event: self.journal.append(
                                     'adapter', segment=seg['id'], **{('kind' if k == 'event' else k): v
                                                                      for k, v in event.items()}))
        refs, rows = [], []
        for group in cp.chunk_grid(seg):
            a, b = group[0][0], group[-1][1]
            rel = cp.chunk_file(seg['id'], a, b)
            path = self.run_root / rel
            tmp = path.with_name(path.name + '.tmp')
            if tmp.exists():
                tmp.unlink()
                self.journal.append('tmpDiscarded', path=rel)
            if path.exists():
                raw = path.read_bytes()
                try:
                    doc = json.loads(raw)
                except json.JSONDecodeError:
                    raise CollectionStop('CACHED_CHUNK_INVALID', f'{rel}: not JSON') from None
                chunk_rows = validate_chunk(doc, plan, a, b, addresses)
                if _canonical(doc) != raw:
                    raise CollectionStop('CACHED_CHUNK_INVALID', f'{rel}: not in canonical form')
                digest = hashlib.sha256(raw).hexdigest()
            else:
                before = self.gate.ledger.used
                result = await adapter.chunk(addresses, group)
                chunk_rows = [r for _, _, _, rs in result for r in rs]
                doc = {'chain': self.chain, 'fetched_utc': self.utc(), 'filter_id': plan['filter_id'], 'from': a,
                       'to': b, 'logs': chunk_rows, 'windows': [[x, y, n] for x, y, n, _ in result]}
                validate_chunk(doc, plan, a, b, addresses)
                digest = atomic_write(self.run_root, rel, _canonical(doc))
                self.journal.append('chunk', segment=seg['id'], path=rel, sha256=digest, endpoint=self.gate.url,
                                    method='eth_getLogs', filterId=plan['filter_id'],
                                    windows=[[x, y] for x, y, _, _ in result], split=len(result) != len(group),
                                    posts=self.gate.ledger.used - before, **self._ledger_mark())
            refs.append({'path': f'{self.run_id}/{rel}', 'sha256': digest})
            rows += chunk_rows
        return refs, rows

    def discovered(self, rows):
        """Vaults created by a registry factory after the B0 vaults, with their CREATE(factory, 3 + index) check."""
        reg = h.registry(self.chain)
        counts = cp.b0_vault_counts(self.b0_plan, self.chain)
        known = {v['vault'] for v in self.b0_plan['vaults']}
        found = []
        for row in sorted(rows, key=lambda r: (int(r['blockNumber'], 16), int(r['logIndex'], 16))):
            if row['topics'] and row['topics'][0] == cp.logscan.T['YieldVaultCreated'] and row['address'] in reg['factories']:
                vault = abi.topic_addr(row['topics'][1])
                if vault in known:
                    continue
                factory = row['address']
                index = counts[factory]
                counts[factory] += 1
                if vault != cp.create_address(factory, cp.VAULT_NONCE_OFFSET + index):
                    raise CollectionStop('UNKNOWN_CREATION_PATH', f'{vault} is not CREATE({factory}, '
                                         f'{cp.VAULT_NONCE_OFFSET + index})', 'REFUSED')
                found.append({'factory': factory, 'index': index, 'vault': vault})
                known.add(vault)
        return found

    # -- step 8 ----------------------------------------------------------------------------------------
    def required_headers(self, plans, rows):
        spec, pin = self.spec, self.spec['pin']['block']
        need = {spec['anchor']['block']: {'anchor'}, pin: {'b0Pin'}}
        for seg, plan in plans:
            need.setdefault(plan['end'], set()).add('planEnd')
            if spec['headerScope'] == 'H1' or (spec['headerScope'] == 'H2' and plan['start'] > pin):
                for _, end in cp.logscan.windows(plan['start'], plan['end'], cp.WINDOW):
                    need.setdefault(end, set()).add('windowEnd')
        for row in rows:
            n = int(row['blockNumber'], 16)
            if hd.is_b0_row(row, spec):
                if spec['b0EventBlockRecheck']:
                    need.setdefault(n, set()).add('b0EventBlock')
            else:
                need.setdefault(n, set()).add('eventBlock')
        return need

    async def ensure_headers(self, need):
        numbers = sorted(need)
        entries = {}
        for k in range(0, len(numbers), tp.GROUP_LIMIT):
            group = numbers[k:k + tp.GROUP_LIMIT]
            rel = f'headers/part-{k // tp.GROUP_LIMIT:05d}.json'
            path = self.run_root / rel
            tmp = path.with_name(path.name + '.tmp')
            if tmp.exists():
                tmp.unlink()
                self.journal.append('tmpDiscarded', path=rel)
            if path.exists():
                raw = path.read_bytes()
                doc = json.loads(raw)
                if (_canonical(doc) != raw or not isinstance(doc, dict) or doc.get('numbers') != group
                        or [e.get('number') for e in doc.get('entries', [])] != group
                        or any(set(e) != {'number', 'hash', 'parentHash', 'timestamp'} or not h.full_hash(e['hash'])
                               or not h.full_hash(e['parentHash']) or type(e['timestamp']) is not int
                               for e in doc['entries'])):
                    raise CollectionStop('CACHED_HEADERS_INVALID', rel)
                got = {e['number']: e for e in doc['entries']}
            else:
                got = await self._headers(group)
                digest = atomic_write(self.run_root, rel, _canonical({'numbers': group,
                                                                      'entries': [got[n] for n in group]}))
                self.journal.append('headers', path=rel, sha256=digest, **self._ledger_mark())
            entries.update(got)
        return entries

    def check_headers(self, entries, plans, rows, need):
        spec = self.spec
        if entries[spec['anchor']['block']]['hash'] != spec['anchor']['hash']:
            raise CollectionStop('HISTORY_REORG', 'anchor')
        if entries[spec['pin']['block']]['hash'] != spec['pin']['hash']:
            raise CollectionStop('HISTORY_REORG', 'B0 pin')
        for _, plan in plans:
            if entries[plan['end']]['hash'] != plan['end_block_hash']:
                raise CollectionStop('HISTORY_REORG', f'plan end {plan["end"]}')
        for row in rows:
            n = int(row['blockNumber'], 16)
            if ('eventBlock' in need.get(n, ()) or 'b0EventBlock' in need.get(n, ())) and row['blockHash'] != entries[n]['hash']:
                raise CollectionStop('HISTORY_EVENT_REORG', f'block {n}')
        for n, e in entries.items():
            parent = entries.get(n - 1)
            if parent is not None and e['parentHash'] != parent['hash']:
                raise CollectionStop('HISTORY_REORG', f'parent link {n - 1} -> {n}')

    async def gas_envelope(self, entries):
        """One POST (FR-D3 / FR-D4 evidence): the anchor header and every ``cp.GAS_ENVELOPE`` sample whose block this
        run re-read as a B0 event block, with its transaction. None when the chain has no samples."""
        if not cp.GAS_ENVELOPE.get(self.chain):
            return None
        anchor = self.spec['anchor']
        samples = [s for s in cp.GAS_ENVELOPE[self.chain] if s['block'] in entries]
        calls = [('eth_getBlockByNumber', [hex(anchor['block']), False])]
        for sample in samples:
            calls += [('eth_getBlockByNumber', [hex(sample['block']), False]),
                      ('eth_getTransactionByHash', [sample['tx']])]
        try:
            values = await self.lane.values(calls)
        except CollectionStop as stop:
            if stop.code != 'RPC_ERROR':            # transport, budget and envelope STOPs propagate
                raise
            # A provider error answer fails the whole batch (native ReleaseRpc): recorded, never retried here.
            self.journal.append('gasEnvelope', status='NOT_COLLECTED', detail=stop.detail[:300], **self._ledger_mark())
            return {'schema': GAS_ENVELOPE_SCHEMA, 'chain': self.chain, 'chainId': h.CHAINS[self.chain],
                    'status': 'NOT_COLLECTED', 'detail': f'{stop.code}: {stop.detail[:300]}',
                    'statement': GAS_ENVELOPE_STATEMENT}
        doc = gas_envelope_doc(self.chain, anchor, entries, samples, values)
        self.journal.append('gasEnvelope', status='OBSERVED', samples=len(samples), **self._ledger_mark())
        return doc

    # -- the run ---------------------------------------------------------------------------------------
    async def run(self, invocation):
        self.preflight()
        ledger = self.gate.ledger
        genesis = self.journal.ledger_genesis()
        ledger.open(known_heads=self.journal.ledger_positions(), expected_genesis=genesis)
        try:
            self.journal.append('invocation', invocation=invocation, ledgerGenesis=ledger.genesis,
                                specSha256=self.spec_sha256, collector=self.collector, **self._ledger_mark())
            self.rpc = tp.release_rpc(self.gate)
            self.lane = tp.Lane(self.rpc, self.gate)
            try:
                pin = scope.bind_run(self)              # before the first read; None for a v1 SIMULATION run
                if pin is not None:
                    self.journal.append('scopePin', **{k: v for k, v in pin.items() if k != 'newVaults'},
                                        **self._ledger_mark())
                return await self._run()
            except CollectionStop as stop:
                self.journal.append('stop', code=stop.code, status=stop.status, detail=stop.detail[:500],
                                    **self._ledger_mark())
                raise
        finally:
            ledger.close()

    async def _run(self):
        spec = self.spec
        await self.anchor_checks()
        segments, plans, all_rows = [self.b0_ref], [], []
        for seg in spec['segments']:
            if seg['checkpoint'] is not None:
                plan, entry, rows = self.reuse(seg)
                segments.append(entry)
                plans.append((seg, plan))
                all_rows += rows
                continue
            plan = await self.ensure_plan(seg)
            refs, rows = await self.ensure_chunks(seg, plan)
            segments.append({'planFile': f'{self.run_id}/{cp.plan_file(seg["id"])}',
                             'planSha256': hashlib.sha256((self.run_root / cp.plan_file(seg['id'])).read_bytes()).hexdigest(),
                             'chunks': refs})
            plans.append((seg, plan))
            all_rows += rows
            if seg['id'] == 'S1':
                found = self.discovered(rows)
                if found != spec['newVaults']:
                    raise CollectionStop('NEW_VAULT_REQUIRES_SPEC', json.dumps(found), 'INCOMPLETE')
        if self.discovered(all_rows) != spec['newVaults']:
            raise CollectionStop('TARGETS_NOT_CLOSED', 'discovery changed on the second pass', 'INCOMPLETE')
        b0_rows = self._b0_rows() if spec['b0EventBlockRecheck'] else []
        need = self.required_headers(plans, all_rows + b0_rows)
        imported, need_fetch = self.imported_headers(plans, need)
        entries = {**await self.ensure_headers({n: r for n, r in need.items() if n in need_fetch}), **imported}
        self.check_headers(entries, plans, all_rows + b0_rows, need)
        envelope = await self.gas_envelope(entries)
        manifest = {'schema': h.SCHEMA, 'chain': self.chain, 'chainId': h.CHAINS[self.chain],
                    'fromBlock': spec['fromBlock'], 'blockNumber': spec['anchor']['block'],
                    'blockHash': spec['anchor']['hash'], 'inventorySha256': ZERO_HASH,
                    'targets': sorted({t for s in [self.b0_plan, *[p for _, p in plans]] for t in
                                       [a['address'] for a in s['addresses']]}),
                    'segments': segments}
        verified = self._verify(manifest)
        derived = hi.derive(verified, self.chain)
        outputs = {} if envelope is None else {'gas-envelope.json': envelope}
        if self.observe is not None:
            balance = tp.BalanceReader(self.gate, self.chain, [spec['balance']['address']], spec['balance']['block'])
            observation, state = await self.observe(self, verified, derived, balance)
            outputs['observation.json'] = observation
            outputs['state-observation.json'] = state
        rechecked = await self._headers(sorted({spec['anchor']['block'], *(p['end'] for _, p in plans)}))
        for n, e in rechecked.items():
            if e['hash'] != entries[n]['hash']:
                raise CollectionStop('HISTORY_REORG', f'block {n} changed at the end-of-collection recheck')
        sidecar = {'schema': hd.SCHEMA, 'chain': self.chain, 'chainId': h.CHAINS[self.chain], 'collector': self.collector,
                   'scope': spec['headerScope'], 'collectedAt': {'first': self.utc(), 'recheck': self.utc()},
                   'headers': [{**entries[n], 'reason': sorted(need[n], key=hd.REASONS.index)} for n in sorted(need)],
                   'rechecks': [{'number': n, 'hash': e['hash']} for n, e in sorted(rechecked.items())],
                   'imported': self.imports}
        files = {'headers.json': atomic_write(self.run_root, 'headers.json', _canonical(sidecar))}
        for name, doc in outputs.items():
            files[name] = atomic_write(self.run_root, name, _canonical(doc))
        summary = {'status': 'COLLECTED', 'chain': self.chain, 'runId': self.run_id, 'specSha256': self.spec_sha256,
                   'httpPosts': self.gate.ledger.used, 'inventory': 'NOT_BOUND'}
        if 'observation.json' in outputs:
            summary.update(self._inventory(verified, derived, outputs['observation.json'], manifest))
        files['manifest.json'] = atomic_write(self.run_root, 'manifest.json', _canonical(manifest))
        self.journal.append('collected', files=files, **self._ledger_mark())
        summary['files'] = files
        summary['journalHead'] = self.journal.head
        return summary

    def reuse(self, seg):
        """An approved immutable segment (CR-D6 Strategy C): verified by its pinned SHA-256 values and re-validated
        chunk by chunk, referenced in place and never re-collected or rewritten."""
        ref = seg['checkpoint']
        raw = (self.history_root / ref['planFile']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref['planSha256']:
            raise CollectionStop('CHECKPOINT_MODIFIED', ref['planFile'], 'REFUSED')
        plan = json.loads(raw)
        if not cp.plan_matches(self.spec, seg, plan):
            raise CollectionStop('CHECKPOINT_MODIFIED', f'{ref["planFile"]}: not this segment', 'REFUSED')
        addresses = [e['address'] for e in seg['addresses']]
        rows = []
        for (a, b), chunk in zip(((g[0][0], g[-1][1]) for g in cp.chunk_grid(seg)), ref['chunks']):
            data = (self.history_root / chunk['path']).read_bytes()
            if hashlib.sha256(data).hexdigest() != chunk['sha256']:
                raise CollectionStop('CHECKPOINT_MODIFIED', chunk['path'], 'REFUSED')
            rows += validate_chunk(json.loads(data), plan, a, b, addresses)
        if len(ref['chunks']) != len(cp.chunk_grid(seg)):
            raise CollectionStop('CHECKPOINT_MODIFIED', 'chunk count', 'REFUSED')
        self.journal.append('checkpointReused', segment=seg['id'], planSha256=ref['planSha256'])
        return plan, {'planFile': ref['planFile'], 'planSha256': ref['planSha256'], 'chunks': ref['chunks']}, rows

    def imported_headers(self, plans, need):
        """Header entries of reused segments come from their approved sidecars; every other block is fetched.
        The anchor and the B0 pin are always re-read."""
        self.imports, imported = [], {}
        fresh = {self.spec['anchor']['block'], self.spec['pin']['block']}
        for seg, plan in plans:
            ref = seg['checkpoint']
            if ref is None:
                continue
            raw = (self.history_root / ref['headersFile']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref['headersSha256']:
                raise CollectionStop('CHECKPOINT_MODIFIED', ref['headersFile'], 'REFUSED')
            source = {e['number']: e for e in json.loads(raw)['headers']}
            numbers = sorted(n for n in need if plan['start'] <= n <= plan['end'] and n not in fresh
                             and n in source)
            for n in numbers:
                imported[n] = {k: source[n][k] for k in ('number', 'hash', 'parentHash', 'timestamp')}
            self.imports.append({'headersSha256': ref['headersSha256'], 'numbers': numbers})
        return imported, set(need) - set(imported)

    def _b0_rows(self):
        rows = []
        for ref in self.b0_ref['chunks']:
            rows += h._load(self.history_root, ref['path'], ref['sha256'], 'HISTORY_CHUNK_HASH_REQUIRED')['logs']
        return rows

    def _verify(self, manifest):
        try:
            return h.verify_manifest(manifest, self.history_root, self.chain)
        except h.HistoryRefused as error:
            raise CollectionStop(error.code, str(error), error.status) from None

    def _inventory(self, verified, derived, observation, manifest):
        """B0-format inventory docs from the accepted check_observation; the normalized inventory only when the
        accepted normalizer takes them. The manifest binds that inventory's SHA-256, or none."""
        docs, _ = hi.check_observation(derived, observation, self.chain, self.reviewed_runtimes)
        for name, doc in docs.items():
            atomic_write(self.run_root, f'inventory/{name}', _canonical(doc))
        try:
            normalized = inv.normalize(docs, self.chain, h.registry(self.chain)['stpros'])
        except (inv.InventoryRefused, KeyError) as error:
            self.journal.append('inventory', status='NOT_NORMALIZED', detail=str(error)[:300])
            return {'inventory': 'NOT_NORMALIZED', 'inventoryOverall': docs['summary.json']['overall']}
        digest = atomic_write(self.run_root, 'inventory.json', inv.canonical(normalized))
        manifest['inventorySha256'] = '0x' + digest
        return {'inventory': 'BOUND', 'inventorySha256': '0x' + digest, 'inventoryOverall': docs['summary.json']['overall']}


def _quantity(raw, key):
    value = raw.get(key) if isinstance(raw, dict) else None
    return int(value, 16) if isinstance(value, str) and re.fullmatch(r'0x[0-9a-f]+', value) else None


def calldata_gas(data):
    return sum(4 if b == 0 else 16 for b in bytes.fromhex(data[2:]))


def gas_envelope_doc(chain, anchor, entries, samples, values):
    """``faroo-fork-gas-envelope-v1`` from one POST: the anchor header, then (header, transaction) per sample.

    Each sample block must be a header this run already verified (``entries``); a different hash is a reorg STOP.
    Everything else is recorded, never decided: a sample whose header or transaction is null, incomplete or not the
    expected direct creation is ``INCOMPLETE`` / ``MISMATCH`` and contributes nothing to the summary."""
    head = values[0]
    if not isinstance(head, dict) or head.get('number') != hex(anchor['block']) or head.get('hash') != anchor['hash']:
        raise CollectionStop('HISTORY_REORG', 'anchor header differs at the gas-envelope read')
    rows = []
    for k, sample in enumerate(samples):
        block, tx = values[1 + 2 * k], values[2 + 2 * k]
        block = block if isinstance(block, dict) else {}
        if block and block.get('hash') != entries[sample['block']]['hash']:
            raise CollectionStop('HISTORY_EVENT_REORG', f'gas-envelope block {sample["block"]}')
        listed = block.get('transactions') if isinstance(block.get('transactions'), list) else None
        row = {'label': sample['label'], 'block': sample['block'], 'blockHash': block.get('hash'), 'tx': sample['tx'],
               'address': sample['address'], 'runtimeBytes': sample['runtimeBytes'],
               'blockGasUsed': _quantity(block, 'gasUsed'), 'blockGasLimit': _quantity(block, 'gasLimit'),
               'txCount': None if listed is None else len(listed), 'txGasLimit': None, 'inputBytes': None,
               'inputSha256': None, 'calldataGas': None, 'evmLowerBound': None, 'singleTxGasUsed': None,
               'status': 'INCOMPLETE'}
        if block and isinstance(tx, dict) and isinstance(tx.get('input'), str) and re.fullmatch(r'0x([0-9a-f]{2})*', tx['input']):
            creation = (tx.get('to') is None and isinstance(tx.get('from'), str) and _quantity(tx, 'nonce') is not None
                        and cp.create_address(tx['from'].lower(), _quantity(tx, 'nonce')) == sample['address'])
            placed = (tx.get('hash') == sample['tx'] and _quantity(tx, 'blockNumber') == sample['block']
                      and tx.get('blockHash') == block['hash'] and listed is not None and sample['tx'] in listed)
            row['txGasLimit'] = _quantity(tx, 'gas')
            row['inputBytes'] = len(tx['input']) // 2 - 1
            row['inputSha256'] = hashlib.sha256(bytes.fromhex(tx['input'][2:])).hexdigest()
            row['calldataGas'] = calldata_gas(tx['input'])
            row['evmLowerBound'] = TX_BASE_GAS + CREATE_GAS + row['calldataGas'] + CODE_DEPOSIT_GAS * sample['runtimeBytes']
            complete = None not in (row['txGasLimit'], row['blockGasUsed'], row['blockGasLimit'], row['txCount'])
            row['status'] = ('VERIFIED' if complete else 'INCOMPLETE') if creation and placed else 'MISMATCH'
            if row['status'] == 'VERIFIED' and row['txCount'] == 1:
                row['singleTxGasUsed'] = row['blockGasUsed']
        rows.append(row)
    ok = [r for r in rows if r['status'] == 'VERIFIED']
    return {'schema': GAS_ENVELOPE_SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'status': 'OBSERVED',
            'anchor': {'block': anchor['block'], 'hash': anchor['hash'], 'gasLimit': _quantity(head, 'gasLimit')},
            'samples': rows,
            'summary': {'maxAcceptedTxGasLimit': max((r['txGasLimit'] for r in ok), default=None),
                        'maxSingleTxGasUsed': max((r['singleTxGasUsed'] for r in ok
                                                   if r['singleTxGasUsed'] is not None), default=None),
                        'maxEvmLowerBound': max((r['evmLowerBound'] for r in ok), default=None)},
            'statement': GAS_ENVELOPE_STATEMENT}


def content_verify(history_root, run_id, spec, *, manifest_sha256=None):
    """CONTENT_VERIFIED: the accepted ``history.authenticate`` and ``headers.verify`` at the reviewed scope."""
    root = Path(history_root)
    manifest_rel = f'{run_id}/manifest.json'
    digest = manifest_sha256 or hashlib.sha256((root / manifest_rel).read_bytes()).hexdigest()
    manifest = json.loads((root / manifest_rel).read_bytes())
    verified = h.authenticate(manifest_rel, digest, root, chain=spec['chain'], anchor_block=spec['anchor']['block'],
                              anchor_hash=spec['anchor']['hash'], inventory_sha256=manifest['inventorySha256'])
    sidecar_raw = (root / run_id / 'headers.json').read_bytes()
    header_result = hd.verify(verified, json.loads(sidecar_raw), spec, root)
    return {'status': 'CONTENT_VERIFIED', 'manifestSha256': digest,
            'headersSha256': hashlib.sha256(sidecar_raw).hexdigest(), 'provenance': verified['provenance'],
            'headers': {k: v for k, v in header_result.items() if k != 'timestamps'}}, verified, header_result


async def supervise(make_collector, *, max_restarts, cooldown_s, sleep):
    """CR-D3 (b): restarts a collection after a retry-exhausted STOP, at most ``max_restarts`` times, with a
    cooldown. Every restart re-opens the same ledger (counted against its restart bound and ceiling) and
    re-validates the cache. Integrity, budget and design STOPs are never restarted."""
    restarts = 0
    while True:
        try:
            return await make_collector().run(f'supervised-{restarts}')
        except CollectionStop as stop:
            if stop.code not in RESTARTABLE or restarts >= max_restarts:
                raise
            restarts += 1
            await sleep(cooldown_s)
