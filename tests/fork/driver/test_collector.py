"""Offline tests of the collection foundation: transport, budget, plan, collector, headers and comparison.

Run inside an approved export:
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_collector.py' -v

Every chain here is a FAKE chain injected as the gate's ``post`` (SIMULATION ONLY): fabricated hashes, logs
and state on registry addresses. No test opens a socket. The optional real-archive test reads the immutable B0
archive only when FAROO_B0_ARCHIVE and FAROO_B0_INVENTORY_DIR are supplied; otherwise it is NOT RUN.
"""
import asyncio
import bisect
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collection_plan as cp  # noqa: E402
import collector as col  # noqa: E402
import headers as hd  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import reproduce as rp  # noqa: E402
import transport as tp  # noqa: E402
from history import HistoryRefused  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release.native import rpc as native_rpc  # noqa: E402

T = h.logscan.T
REG = {c: h.registry(c) for c in h.CHAINS}
FROM, PIN, ANCHOR, HEAD = 100, 2999, 4500, 4700
ZERO20 = '0x' + '00' * 20
ADMIN = '0x' + '00' * 32
E2 = '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1'
ALICE, BOB = '0x' + 'a1' * 20, '0x' + 'b2' * 20
SAFE = h.GOV_SAFE


def run(coro):
    return asyncio.run(coro)


def word(address):
    return '0x' + address[2:].rjust(64, '0')


def amount(value):
    return '0x' + format(value, '064x')


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.now, self.sleeps = start, []

    def ms(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds * 1000


class FakeChain:
    """A deterministic JSON-RPC node (SIMULATION ONLY). ``faults`` are consulted in order for every POST."""

    def __init__(self, chain, rows=(), *, head=HEAD, code=None, storage=None, calls=None, balances=None,
                 hash_overrides=None, tag='fake'):
        self.chain, self.head, self.tag = chain, head, tag
        self.hash_overrides = dict(hash_overrides or {})
        self.rows = sorted(rows, key=lambda r: (int(r['blockNumber'], 16), int(r['logIndex'], 16)))
        self._keys = [int(r['blockNumber'], 16) for r in self.rows]
        self.code = dict(code or {})          # address -> (first block with code, code)
        self.storage = dict(storage or {})    # (address, slot int) -> word
        self.calls = dict(calls or {})        # (address, calldata) -> return data
        self.balances = dict(balances or {})  # address -> int, or 'UNAVAILABLE'
        self.faults = []
        self.posts, self.bodies = 0, []
        self.header_overrides = {}
        self.block_extra = {}                 # n -> extra header fields (gasUsed, gasLimit, transactions)
        self.txs = {}                         # hash -> eth_getTransactionByHash result

    def hash(self, n):
        if n in self.hash_overrides:
            return self.hash_overrides[n]
        return '0x' + hashlib.sha256(f'{self.chain} {self.tag} block {n}'.encode()).hexdigest()

    def header(self, n):
        if n in self.header_overrides:
            return self.header_overrides[n]
        if n > self.head or n < 0:
            return None
        return {'number': hex(n), 'hash': self.hash(n), 'parentHash': self.hash(n - 1), 'timestamp': hex(1_700_000_000 + n),
                **self.block_extra.get(n, {})}

    def row(self, address, topics, block, index, data='0x', tx=None):
        return {'address': address, 'topics': topics, 'data': data, 'blockNumber': hex(block), 'blockHash': self.hash(block),
                'transactionHash': '0x' + hashlib.sha256((tx or f'tx {block} {index}').encode()).hexdigest(),
                'transactionIndex': '0x0', 'logIndex': hex(index), 'removed': False}

    def add_rows(self, rows):
        self.rows = sorted([*self.rows, *rows], key=lambda r: (int(r['blockNumber'], 16), int(r['logIndex'], 16)))
        self._keys = [int(r['blockNumber'], 16) for r in self.rows]

    def answer(self, item):
        method, params, rid = item['method'], item['params'], item['id']

        def ok(result):
            return {'jsonrpc': '2.0', 'id': rid, 'result': result}
        if method == 'eth_chainId':
            return ok(hex(h.CHAINS[self.chain]))
        if method == 'eth_getBlockByNumber':
            n = self.head if params[0] == 'latest' else int(params[0], 16)
            return ok(self.header(n))
        if method == 'eth_getLogs':
            f = params[0]
            a, b = int(f['fromBlock'], 16), int(f['toBlock'], 16)
            addresses = set(f['address'])
            lo, hi = bisect.bisect_left(self._keys, a), bisect.bisect_right(self._keys, b)
            return ok([copy.deepcopy(r) for r in self.rows[lo:hi] if r['address'] in addresses])
        if method == 'eth_getCode':
            first, code = self.code.get(params[0].lower(), (None, '0x'))
            return ok(code if first is not None and int(params[1], 16) >= first else '0x')
        if method == 'eth_getStorageAt':
            return ok(self.storage.get((params[0].lower(), int(params[1], 16)), '0x' + '00' * 32))
        if method == 'eth_call':
            key = (params[0]['to'].lower(), params[0]['data'].lower())
            if key not in self.calls:
                return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': 3, 'message': 'execution reverted'}}
            value = self.calls[key]
            return ok(value(int(params[1], 16)) if callable(value) else value)
        if method == 'eth_getTransactionByHash':
            return ok(copy.deepcopy(self.txs.get(params[0])))
        if method == 'eth_getBalance':
            value = self.balances.get(params[0].lower(), 0)
            if value == 'UNAVAILABLE':
                return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'the method does not exist'}}
            return ok(hex(value))
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'the method does not exist'}}

    async def post(self, url, body, timeout_ms):
        self.posts += 1
        self.bodies.append(copy.deepcopy(body))
        for fault in list(self.faults):
            result = fault(self, body)
            if result is not None:
                return result
        if isinstance(body, list):
            return 200, [self.answer(item) for item in body]
        return 200, self.answer(body)


def once(fn, times=1):
    """A fault that fires on the first ``times`` matching POSTs and then stays silent."""
    state = {'left': times}

    def fault(chain, body):
        if state['left'] <= 0:
            return None
        result = fn(chain, body)
        if result is not None:
            state['left'] -= 1
        return result
    return fault


def is_logs(body):
    items = body if isinstance(body, list) else [body]
    return all(i['method'] == 'eth_getLogs' for i in items)


def authorization(root, *, ceiling=100_000, restarts=3, ident='sim-auth', mode='SIMULATION'):
    doc = {'schema': col.AUTHORIZATION_SCHEMA, 'authorizationId': ident, 'mode': mode, 'chains': ['mainnet', 'testnet'],
           'methods': list(col.AUTHORIZED_METHODS), 'maxHttpPosts': ceiling, 'maxRestarts': restarts,
           'statement': 'SIMULATION ONLY: offline fake-chain test input; not an owner FR-D1 authorization'}
    path = Path(root) / f'{ident}.json'
    path.write_text(json.dumps(doc))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def gate_for(root, chain, fake, clock, *, ceiling=100_000, restarts=3, ident='sim-auth', invocation='inv'):
    path, digest = authorization(root, ceiling=ceiling, restarts=restarts, ident=ident)
    auth = col.load_authorization(path, digest)
    ledger = tp.BudgetLedger(Path(root) / 'budget', auth, invocation=invocation, clock_ms=clock.ms)
    return tp.HttpGate(chain, ledger, clock_ms=clock.ms, sleep=clock.sleep, post=fake.post)


class World:
    """A synthetic B0 archive on registry addresses plus a fake chain that continues it (SIMULATION ONLY)."""

    def __init__(self, root, chain, *, new_vault=False, safe_code_from=105, post_rows=(), tag='fake', head=HEAD,
                 protocol=None):
        self.root, self.chain = Path(root), chain
        self.fake = FakeChain(chain, head=head, tag=tag)
        reg = REG[chain]
        self.stpros = reg['stpros']
        self.factories = list(reg['factories'])
        self.beacons = {f: cp.create_address(f, 2) for f in self.factories}
        self.vaults = {f: cp.create_address(f, 3) for f in self.factories}
        self.new_vault = (self.factories[0], 1, cp.create_address(self.factories[0], 4)) if new_vault else None
        f = self.fake
        if protocol is None:
            rows = [f.row(self.stpros, [T['Upgraded'], word('0x' + '5e' * 20)], 105, 0),
                    f.row(self.stpros, [T['Transfer'], word(ZERO20), word(ALICE)], 200, 0, amount(30))]
        else:
            rows = []
        labels = {self.stpros: ['record:StPROS']}
        vault_list = []
        for i, factory in enumerate(self.factories):
            beacon, vault = self.beacons[factory], self.vaults[factory]
            tx = f'create {vault}'
            rows += [f.row(factory, [T['OwnershipTransferred'], word(ZERO20), word(E2)], 110, i),
                     f.row(factory, [T['BeaconCreated'], word(beacon), word('0x' + '1f' * 20)], 120, 2 * i),
                     f.row(beacon, [T['Upgraded'], word('0x' + '1f' * 20)], 120, 2 * i + 1),
                     f.row(factory, [T['YieldVaultCreated'], word(vault), word(E2)], 150 + i, 0, tx=tx),
                     f.row(vault, [T['RoleGranted'], ADMIN, word(E2), word(factory)], 150 + i, 1, tx=tx),
                     f.row(vault, [T['ProxyBeaconUpgraded'], word(beacon)], 150 + i, 2, tx=tx)]
            labels[factory] = ['record:YieldVaultFactory']
            labels[beacon] = [f'beacon_of:{factory}']
            labels[vault] = [cp.vault_label(factory, 0)]
            vault_list.append({'factory': factory, 'index': 0, 'vault': vault})
        extra_post = []
        if protocol is not None:
            model = protocol(self, f)
            rows += model['rows']
            for address, label in model['labels'].items():
                labels.setdefault(address, label)
            extra_post = model['post']
        self.b0_rows = rows
        addresses = sorted(labels)
        self.b0_dir = 'b0'
        windows = h.logscan.chunks(FROM, PIN, 2)
        fid = j.historical_filter_id(addresses)
        shas = []
        for group in windows:
            a, b = group[0][0], group[-1][1]
            got = [r for r in rows if a <= int(r['blockNumber'], 16) <= b]
            chunk = {'chain': chain, 'fetched_utc': '2026-10-09T00:00:00Z', 'filter_id': fid, 'from': a, 'to': b,
                     'logs': got, 'windows': [[x, y, sum(x <= int(r['blockNumber'], 16) <= y for r in got)] for x, y in group]}
            path = self.root / self.b0_dir / 'chunks' / f'{a:010d}-{b:010d}.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cp.canonical_bytes(chunk))
            shas.append(hashlib.sha256(path.read_bytes()).hexdigest())
        plan = {'addresses': [{'address': a, 'labels': labels[a]} for a in addresses], 'addresses_with_code_before_start': [],
                'chain': chain, 'chain_id': h.CHAINS[chain], 'chunk_windows': 2, 'created_utc': '2026-10-09T00:00:00Z',
                'end': PIN, 'end_block_hash': f.hash(PIN), 'end_timestamp': 1_700_000_000 + PIN, 'filter_id': fid,
                'head_at_plan': PIN + 120, 'rpc': PUBLIC_RPC[chain], 'start': FROM, 'start_basis': 'synthetic B0',
                'tool_version': '1', 'vaults': vault_list, 'window': 1000}
        (self.root / self.b0_dir / 'plan.json').write_bytes(json.dumps(plan).encode())
        self.b0_plan = plan
        self.prefix = {'start': FROM, 'end': PIN, 'filterId': fid, 'endBlockHash': f.hash(PIN),
                       'planSha256': hashlib.sha256((self.root / self.b0_dir / 'plan.json').read_bytes()).hexdigest(),
                       'chunkCount': len(shas), 'chunkListSha256': h.chunk_list_sha256(shas)}
        post = [f.row(self.stpros, [T['Transfer'], word(ALICE), word(BOB)], 3100, 0, amount(10))] + extra_post
        if chain == 'mainnet':
            setup = '0x' + hashlib.sha256(b'synthetic SafeSetup').hexdigest()
            post.append(f.row(SAFE, [setup], 3300, 0))
            rows_safe = [f.row(SAFE, [setup], 111, 0)]
            f.code[SAFE] = (safe_code_from, '0x6080')
        else:
            rows_safe = []
        if self.new_vault:
            factory, index, vault = self.new_vault
            tx = f'create {vault}'
            post += [f.row(factory, [T['YieldVaultCreated'], word(vault), word(E2)], 3200, 0, tx=tx),
                     f.row(vault, [T['RoleGranted'], ADMIN, word(E2), word(factory)], 3200, 1, tx=tx),
                     f.row(vault, [T['ProxyBeaconUpgraded'], word(self.beacons[factory])], 3200, 2, tx=tx)]
            f.code[vault] = (3200, '0x6080')
        f.add_rows([*rows, *rows_safe, *post, *post_rows])
        for address in addresses:
            f.code.setdefault(address, (101, '0x6080'))
        if chain == 'mainnet':
            vault001 = self.vaults[self.factories[0]]
            f.calls[(vault001, abi.calldata('phase()'))] = amount(3)
            f.storage[(vault001, 4)] = amount(3)

    def patch(self):
        return mock.patch.dict(h.B0_PREFIX, {self.chain: self.prefix})

    def spec(self, **kw):
        new = [self.new_vault] if self.new_vault else []
        anchor = kw.pop('anchor', ANCHOR)
        return cp.spec(self.chain, anchor_block=anchor, anchor_hash=self.fake.hash(anchor),
                       b0_plan=self.b0_plan, b0_plan_sha256=self.prefix['planSha256'], new_vaults=kw.pop('new', new), **kw)

    def collector(self, spec, gate, clock, *, run_id='run-b', utc='2026-10-17T00:00:00Z', **kw):
        return col.Collector(spec, hashlib.sha256(cp.canonical_bytes(spec)).hexdigest(), self.root, run_id, gate,
                             b0_dir=self.b0_dir, utc=lambda: utc, clock_ms=clock.ms, **kw)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)       # runs after every later-registered cleanup (ledger closes)
        self.root = Path(self._tmp.name).resolve()

    def stop(self, fn, code, status=None):
        with self.assertRaises(CollectionStop) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        if status:
            self.assertEqual(ctx.exception.status, status, str(ctx.exception))
        return ctx.exception


# ------------------------------------------------------------------------------------------------ A. transport
class AdapterTest(Tmp):
    def setup(self, chain='testnet', rows=()):
        self.clock = Clock()
        self.fake = FakeChain(chain)
        self.fake.add_rows(rows)
        self.gate = gate_for(self.root, chain, self.fake, self.clock)
        self.gate.ledger.open()
        self.addCleanup(self.gate.ledger.close)
        self.lane = tp.Lane(tp.release_rpc(self.gate), self.gate)
        return tp.LogsAdapter(self.lane)

    def windows(self, n, start=1000):
        return [(start + 1000 * i, start + 1000 * i + 999) for i in range(n)]

    def test_twenty_windows_are_one_post_in_window_order(self):
        fake = FakeChain('testnet')
        target = REG['testnet']['stpros']
        rows = [fake.row(target, [T['Transfer'], word(ALICE), word(BOB)], 1000 + 1000 * i + 5, 0, amount(i)) for i in range(20)]
        adapter = self.setup(rows=rows)
        out = run(adapter.chunk([target], self.windows(20)))
        self.assertEqual(self.fake.posts, 1)
        self.assertEqual(self.gate.sent, [20])
        body = self.fake.bodies[0]
        self.assertEqual([int(i['params'][0]['fromBlock'], 16) for i in body], [a for a, _ in self.windows(20)])
        self.assertEqual([n for _, _, n, _ in out], [1] * 20)

    def test_lane_serializes_concurrent_reads_and_detects_a_bypass(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']

        async def both():
            return await asyncio.gather(adapter.chunk([target], self.windows(20)),
                                        self.lane.call('eth_getBlockByNumber', ['0x10', False]))
        run(both())
        self.assertEqual(sorted(self.gate.sent), [1, 20])
        # A caller that bypasses the lane joins the group: the assembly check refuses it.

        async def bypass():
            return await asyncio.gather(self.lane.group([('eth_getLogs', tp.LogsAdapter._params([target], a, b))
                                                          for a, b in self.windows(20)]),
                                        self.lane.rpc('eth_chainId', []), return_exceptions=True)
        results = run(bypass())
        self.assertIsInstance(results[0], CollectionStop)
        self.assertEqual(results[0].code, 'GROUP_ASSEMBLY')

    def test_window_and_group_bounds(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']
        self.stop(lambda: run(adapter.chunk([target], self.windows(21))), 'CHUNK_WINDOWS', 'REFUSED')
        self.stop(lambda: run(adapter.chunk([target], [(1000, 2000)])), 'CHUNK_WINDOWS', 'REFUSED')
        self.stop(lambda: run(adapter.chunk([target], [(1000, 1999), (2001, 2999)])), 'CHUNK_WINDOWS', 'REFUSED')
        self.assertEqual(self.fake.posts, 0)
        run(adapter.chunk([target], [(1000, 1999)]))
        self.assertEqual(self.fake.posts, 1)
        # ReleaseRpc's own inclusive bound: toBlock - fromBlock >= 1000 never leaves the process.
        with self.assertRaises(j.ReleaseError) as ctx:
            run(self.lane.rpc('eth_getLogs', tp.LogsAdapter._params([target], 1000, 2000)))
        self.assertEqual(ctx.exception.code, 'LOG_RANGE')
        self.assertEqual(self.fake.posts, 1)

    def test_swapped_ids_are_correlated_and_rows_outside_their_window_stop(self):
        fake = FakeChain('testnet')
        target = REG['testnet']['stpros']
        rows = [fake.row(target, [T['Transfer'], word(ALICE), word(BOB)], 1500, 0, amount(1)),
                fake.row(target, [T['Transfer'], word(ALICE), word(BOB)], 2500, 0, amount(2))]
        adapter = self.setup(rows=rows)
        self.fake.faults.append(once(lambda c, body: (200, list(reversed([c.answer(i) for i in body])))
                                     if isinstance(body, list) else None))
        out = run(adapter.chunk([target], self.windows(2)))
        self.assertEqual([(a, n, rs[0]['blockNumber']) for a, _, n, rs in out], [(1000, 1, '0x5dc'), (2000, 1, '0x9c4')])
        # A response that names the right ID but carries another window's rows is caught by validate_rows.

        def shifted(c, body):
            if not isinstance(body, list):
                return None
            answers = [c.answer(i) for i in body]
            answers[0]['result'], answers[1]['result'] = answers[1]['result'], answers[0]['result']
            return 200, answers
        self.fake.faults[:] = [once(shifted)]
        self.stop(lambda: run(adapter.chunk([target], self.windows(2))), 'HISTORY_LOG_RANGE')

    def test_item_error_falls_back_per_window_and_stops_when_unclassified(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']

        def item_error(c, body):
            items = body if isinstance(body, list) else [body]
            if any(i['params'][0]['fromBlock'] == hex(2000) and i['params'][0]['toBlock'] == hex(2999) for i in items):
                return 200, [({'jsonrpc': '2.0', 'id': i['id'], 'error': {'code': -32005, 'message': 'query  returned more than 10000 results'}}
                              if i['params'][0]['fromBlock'] == hex(2000) else c.answer(i)) for i in items] \
                    if isinstance(body, list) else {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32005,
                                                    'message': 'query returned more than 10000 results'}}
            return None
        self.fake.faults.append(item_error)
        stop = self.stop(lambda: run(adapter.chunk([target], self.windows(3))), 'UNCLASSIFIED_RPC_ERROR')
        self.assertIn('query returned more than 10000 results', stop.detail)
        self.assertEqual(self.gate.sent, [3, 1, 1])   # group, then window 1 ok, window 2 stops

    def test_classified_error_splits_deterministically(self):
        fake = FakeChain('testnet')
        target = REG['testnet']['stpros']
        rows = [fake.row(target, [T['Transfer'], word(ALICE), word(BOB)], b, 0, amount(1)) for b in (2100, 2600, 2999)]
        clock = Clock()
        self.fake, self.clock = fake, clock
        self.gate = gate_for(self.root, 'testnet', fake, clock)
        self.gate.ledger.open()
        self.addCleanup(self.gate.ledger.close)
        self.lane = tp.Lane(tp.release_rpc(self.gate), self.gate)
        fake.add_rows(rows)
        message = 'response size exceeded'

        def too_big(c, body):
            items = body if isinstance(body, list) else [body]
            bad = [i for i in items if int(i['params'][0]['toBlock'], 16) - int(i['params'][0]['fromBlock'], 16) >= 500
                   and int(i['params'][0]['fromBlock'], 16) >= 2000 and int(i['params'][0]['toBlock'], 16) <= 2999]
            if not bad:
                return None
            answers = [{'jsonrpc': '2.0', 'id': i['id'], 'error': {'code': -32005, 'message': message}} if i in bad
                       else c.answer(i) for i in items]
            return 200, answers if isinstance(body, list) else answers[0]
        fake.faults.append(too_big)
        adapter = tp.LogsAdapter(self.lane, classifier={message})
        out = run(adapter.chunk([target], self.windows(3)))
        # [2000, 2999] is refused (width >= 500) and splits once at 2499; both halves are accepted.
        self.assertEqual([(a, b, n) for a, b, n, _ in out],
                         [(1000, 1999, 0), (2000, 2499, 1), (2500, 2999, 2), (3000, 3999, 0)])
        self.assertEqual(self.gate.sent, [3, 1, 1, 1, 1, 1])
        # The same chunk at a single block that is still refused: STOP, never deeper.
        fake.faults[:] = [lambda c, body: (200, {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32005, 'message': message}})
                          if not isinstance(body, list) else (200, [{'jsonrpc': '2.0', 'id': i['id'],
                                                                     'error': {'code': -32005, 'message': message}} for i in body])]
        self.stop(lambda: run(adapter.window([target], 5000, 5000)), 'SINGLE_BLOCK_REFUSED')
        shallow = tp.LogsAdapter(self.lane, classifier={message}, max_depth=2)
        self.stop(lambda: run(shallow.window([target], 5000, 5999)), 'SUBDIVISION_DEPTH')

    def test_retry_policy_is_release_rpc_and_counts_every_attempt(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']
        self.fake.faults.append(once(lambda c, body: (429, None), times=2))
        run(adapter.chunk([target], self.windows(2)))
        self.assertEqual(self.fake.posts, 3)
        self.assertEqual(self.gate.ledger.used, 3)
        self.assertEqual([s for s in self.clock.sleeps if s >= 1], [1, 2])
        self.fake.faults[:] = [lambda c, body: (429, None)]
        self.stop(lambda: run(adapter.chunk([target], self.windows(2))), 'RPC_HTTP')
        self.assertEqual(self.fake.posts, 6)

    def test_transport_error_falls_back_once_and_never_splits(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']

        def drop_groups(c, body):
            if isinstance(body, list):
                raise native_rpc.TransportError('TypeError', 'fetch failed')
            return None
        self.fake.faults.append(drop_groups)
        out = run(adapter.chunk([target], self.windows(3)))
        self.assertEqual(self.gate.sent, [3, 1, 1, 1])
        self.assertEqual(len(out), 3)

        def drop_all(c, body):
            raise native_rpc.TransportError('TimeoutError', 'aborted')
        self.fake.faults[:] = [drop_all]
        classified = tp.LogsAdapter(self.lane, classifier={'aborted', 'TimeoutError: aborted'})
        self.stop(lambda: run(classified.window([target], 1000, 1999)), 'TRANSPORT_ERROR')

    def test_envelope_defects_stop_without_fallback(self):
        adapter = self.setup()
        target = REG['testnet']['stpros']
        cases = {
            'RPC_BATCH_INCOMPLETE': lambda c, body: (200, [c.answer(i) for i in body][:-1]),
            'RPC_RESPONSE_ID': lambda c, body: (200, [{**c.answer(i), 'id': i['id'] + 1000} for i in body]),
            'RPC_MISSING_RESULT': lambda c, body: (200, [{'jsonrpc': '2.0', 'id': i['id']} for i in body]),
        }
        for code, fault in cases.items():
            before = self.fake.posts
            self.fake.faults[:] = [fault]
            self.stop(lambda: run(adapter.chunk([target], self.windows(2))), code)
            self.assertEqual(self.fake.posts - before, 1, code)

    def test_removed_and_malformed_rows_stop(self):
        fake = FakeChain('testnet')
        target = REG['testnet']['stpros']
        row = fake.row(target, [T['Transfer'], word(ALICE), word(BOB)], 1500, 0, amount(1))
        adapter = self.setup(rows=[{**row, 'removed': True}])
        self.stop(lambda: run(adapter.chunk([target], self.windows(1))), 'LOG_REMOVED')
        self.fake.rows = [{k: v for k, v in row.items() if k != 'removed'}]
        self.fake._keys = [1500]
        self.stop(lambda: run(adapter.chunk([target], self.windows(1))), 'LOG_ROW_KEYS')

    def test_mixed_group_results_are_never_accepted(self):
        class Mixed:
            gate = None

            async def group(self, calls):
                return [[], native_rpc.TransportError('TypeError', 'fetch failed')]
        adapter = tp.LogsAdapter(Mixed())
        self.stop(lambda: run(adapter.chunk([REG['testnet']['stpros']], [(1000, 1999), (2000, 2999)])), 'MIXED_GROUP_RESULT')

    def test_reviewed_classifier_is_empty(self):
        self.assertEqual(tp.REVIEWED_SIZE_ERRORS, frozenset())
        self.assertEqual(tp.LogsAdapter(None).classifier, frozenset())
        self.assertIs(inspect.signature(col.Collector).parameters['classifier'].default, None)


# ------------------------------------------------------------------------------------------------ B. gate and budget
class GateTest(Tmp):
    def setup(self, chain='testnet', **kw):
        self.clock = Clock()
        self.fake = FakeChain(chain)
        self.gate = gate_for(self.root, chain, self.fake, self.clock, **kw)
        self.gate.ledger.open()
        self.addCleanup(self.gate.ledger.close)
        return self.gate

    def test_endpoint_is_the_chain_allowlist_entry(self):
        gate = self.setup()
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}
        # Refused endpoints are derived from the reviewed ones (no remote literal in driver sources); none is sent.
        testnet = PUBLIC_RPC['testnet']
        for url in (PUBLIC_RPC['mainnet'], testnet.replace('atlantic.dplabs-internal.com', 'evil.example'),
                    testnet.replace('//', '//user:pw@'), testnet.replace('https:', 'http:'),
                    testnet.split(':')[0][:4] + '://127.0.0.1:8545/'):
            self.stop(lambda: run(gate.post(url, body, 1000)), 'GATE_ENDPOINT', 'REFUSED')
        self.assertEqual(self.fake.posts, 0)
        with self.assertRaises(CollectionStop):
            run(native_rpc.ReleaseRpc(PUBLIC_RPC['mainnet'], post=gate.post)('eth_chainId', []))
        self.assertEqual(self.fake.posts, 0)
        self.assertEqual(run(gate.post(PUBLIC_RPC['testnet'], body, 1000))[0], 200)
        self.assertEqual(run(gate.post(PUBLIC_RPC['testnet'] + '/', body, 1000))[0], 200)

    def test_only_fr_d1_methods_pass(self):
        gate = self.setup()
        for method in ('eth_estimateGas', 'eth_sendRawTransaction', 'eth_getBalance', 'eth_accounts'):
            self.stop(lambda: run(gate.post(gate.url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': []}, 1000)),
                      'GATE_METHOD', 'REFUSED')
        with self.assertRaises(j.ReleaseError):
            run(tp.release_rpc(gate)('eth_sendRawTransaction', ['0x00']))
        self.assertEqual(self.fake.posts, 0)

    def test_gate_enforces_the_authorization_method_list(self):
        clock, fake = Clock(), FakeChain('testnet')
        path, digest = authorization(self.root, ident='narrow')
        doc = json.loads(path.read_text())
        doc['methods'] = ['eth_chainId', 'eth_getBlockByNumber', 'eth_getLogs']
        path.write_text(json.dumps(doc))
        auth = col.load_authorization(path, hashlib.sha256(path.read_bytes()).hexdigest())
        ledger = tp.BudgetLedger(self.root / 'budget', auth, invocation='n', clock_ms=clock.ms)
        gate = tp.HttpGate('testnet', ledger, clock_ms=clock.ms, sleep=clock.sleep, post=fake.post)
        ledger.open()
        self.addCleanup(ledger.close)
        with self.assertRaises(CollectionStop) as ctx:
            run(tp.release_rpc(gate)('eth_getCode', [REG['testnet']['stpros'], '0x1']))
        self.assertEqual(ctx.exception.code, 'GATE_METHOD')
        reader = tp.BalanceReader(gate, 'testnet', [REG['testnet']['stpros']], 4500)
        self.stop(lambda: run(reader.get(REG['testnet']['stpros'], 4500)), 'GATE_BALANCE_REFUSED', 'REFUSED')
        self.assertEqual(fake.posts, 0)
        run(tp.release_rpc(gate)('eth_chainId', []))
        self.assertEqual(fake.posts, 1)

    def test_redirect_is_refused_without_a_second_post(self):
        gate = self.setup()
        self.fake.faults.append(lambda c, body: (302, None))
        try:
            run(tp.release_rpc(gate)('eth_chainId', []))
            outcome = 'accepted'
        except Exception as error:      # noqa: BLE001 - the class of the refusal is the assertion
            outcome = type(error).__name__
        self.assertEqual(outcome, 'TransportError')
        self.assertEqual(self.fake.posts, 1)

    def test_pacing_is_shared_and_posts_never_overlap(self):
        gate = self.setup()
        reader = tp.BalanceReader(gate, 'testnet', [REG['testnet']['stpros']], 4500)
        lane = tp.Lane(tp.release_rpc(gate), gate)
        times, inflight = [], {'n': 0, 'max': 0}
        original = self.fake.post

        async def watched(url, body, timeout_ms):
            inflight['n'] += 1
            inflight['max'] = max(inflight['max'], inflight['n'])
            times.append(self.clock.now)
            await asyncio.sleep(0)
            try:
                return await original(url, body, timeout_ms)
            finally:
                inflight['n'] -= 1
        gate._post = watched

        async def mix():
            await asyncio.gather(lane.call('eth_chainId', []), reader.get(REG['testnet']['stpros'], 4500),
                                 lane.call('eth_getBlockByNumber', ['0x1', False]), reader.get(REG['testnet']['stpros'], 4500))
        run(mix())
        self.assertEqual(inflight['max'], 1)
        self.assertEqual(len(times), 4)
        self.assertTrue(all(b - a >= tp.PACING_MS for a, b in zip(times, times[1:])), times)

    def test_budget_counts_retries_and_refuses_before_sending(self):
        gate = self.setup(ceiling=4)
        self.fake.faults.append(once(lambda c, body: (429, None), times=2))
        run(tp.release_rpc(gate)('eth_chainId', []))
        self.assertEqual((self.fake.posts, gate.ledger.used), (3, 3))
        run(tp.release_rpc(gate)('eth_chainId', []))
        self.assertEqual(gate.ledger.used, 4)
        with self.assertRaises(tp.BudgetExhausted):
            run(tp.release_rpc(gate)('eth_chainId', []))
        self.assertEqual(self.fake.posts, 4)

    def test_native_transport_needs_an_approved_authorization(self):
        path, digest = authorization(self.root, mode='SIMULATION')
        auth = col.load_authorization(path, digest)
        ledger = tp.BudgetLedger(self.root / 'budget', auth, invocation='x')
        self.stop(lambda: tp.HttpGate('mainnet', ledger), 'FR_D1_NOT_AUTHORIZED', 'REFUSED')
        path, digest = authorization(self.root, mode='PUBLIC_READ_ONLY', ident='real-auth')
        self.stop(lambda: col.load_authorization(path, digest), 'FR_D1_NOT_AUTHORIZED', 'REFUSED')
        self.assertEqual(col.APPROVED_COLLECTION_AUTHORIZATIONS, frozenset({
            '6963e58be871b15ff3be92bbd0654cbd784b183fecd1c9050379a6b1a10782c1',  # fr-d1-s1-testnet-sl20261018
            'f4ca9cf3ccb8a5241aefaaa51497171e877f8f7e25f701bd6cdac19f2a4c28cf',  # fr-d1-s2-testnet-sl20261018
            '00c6ab29bf49d545608306a1543b309b555e04e943ad3cd4358018227512b669',  # fr-d1-s1-mainnet-sl20261018
            '71357ba54d74ccaa04aad6acb39dc02cacdda9993effd3fefa2e5618c05efe74',  # fr-d1-s2-mainnet-sl20261018
        }))
        self.assertFalse(ledger.lock.exists())

    def test_balance_reader_scope_and_envelope(self):
        gate = self.setup()
        stpros = REG['testnet']['stpros']
        self.fake.balances[stpros] = 7
        reader = tp.BalanceReader(gate, 'testnet', [stpros], 4500)
        self.assertEqual(run(reader.get(stpros, 4500)), 7)
        self.stop(lambda: run(reader.get('0x' + '11' * 20, 4500)), 'BALANCE_ADDRESS', 'REFUSED')
        for block in (4499, 'latest', True, 4500.0):
            self.stop(lambda: run(reader.get(stpros, block)), 'BALANCE_BLOCK', 'REFUSED')
        self.stop(lambda: run(gate.post_balance({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getBalance',
                                                 'params': [stpros, 'latest']}, 1000)), 'GATE_BALANCE_REFUSED', 'REFUSED')
        self.stop(lambda: run(gate.post_balance({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getBalance',
                                                 'params': ['0x' + '11' * 20, hex(4500)]}, 1000)), 'GATE_BALANCE_REFUSED')
        self.assertEqual(self.fake.posts, 1)
        bad = {'wrong id': lambda c, b: (200, {**c.answer(b), 'id': 5}),
               'no jsonrpc': lambda c, b: (200, {k: v for k, v in c.answer(b).items() if k != 'jsonrpc'}),
               'both': lambda c, b: (200, {**c.answer(b), 'error': {'code': 1, 'message': 'x'}}),
               'non-canonical': lambda c, b: (200, {**c.answer(b), 'result': '0x07'}),
               'decimal': lambda c, b: (200, {**c.answer(b), 'result': '7'})}
        for name, fault in bad.items():
            self.fake.faults[:] = [fault]
            with self.assertRaises(CollectionStop, msg=name):
                run(reader.get(stpros, 4500))
        self.fake.faults[:] = []
        self.fake.balances[stpros] = 'UNAVAILABLE'
        self.assertEqual(run(reader.get(stpros, 4500)), tp.NOT_OBSERVED)
        self.assertNotEqual(run(reader.get(stpros, 4500)), 0)

    def test_balance_retry_parity_with_release_rpc(self):
        stpros = REG['testnet']['stpros']
        scripts = {'two 429 then ok': [(429, None), (429, None)], 'three 429': [(429, None)] * 3,
                   'rpc -32011 twice': ['RPC', 'RPC'], 'transport': ['TRANSPORT']}
        for name, script in scripts.items():
            outcomes = []
            for kind in ('balance', 'code'):
                with tempfile.TemporaryDirectory() as tmp:
                    clock, fake = Clock(), FakeChain('testnet')
                    gate = gate_for(tmp, 'testnet', fake, clock)
                    gate.ledger.open()
                    steps = list(script)

                    def fault(c, body, steps=steps):
                        if not steps:
                            return None
                        step = steps.pop(0)
                        if step == 'TRANSPORT':
                            raise native_rpc.TransportError('TypeError', 'fetch failed')
                        if step == 'RPC':
                            return 200, {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32011, 'message': 'cu limit'}}
                        return step
                    fake.faults.append(fault)
                    try:
                        if kind == 'balance':
                            run(tp.BalanceReader(gate, 'testnet', [stpros], 4500).get(stpros, 4500))
                        else:
                            run(tp.release_rpc(gate)('eth_getCode', [stpros, hex(4500)]))
                        outcome = 'ok'
                    except (CollectionStop, j.ReleaseError) as error:
                        outcome = error.code if hasattr(error, 'code') else 'error'
                    except native_rpc.TransportError:
                        outcome = 'transport'
                    finally:
                        gate.ledger.close()
                    outcomes.append((fake.posts, [s for s in clock.sleeps if s >= 1],
                                     {'RPC_ERROR': 'rpc', 'RPC_HTTP': 'http'}.get(outcome, outcome)))
            self.assertEqual(outcomes[0], outcomes[1], name)

    def test_native_helpers_are_pinned(self):
        self.assertEqual(list(inspect.signature(native_rpc._endpoint).parameters), ['url', 'simulation', 'local_only'])
        self.assertEqual(list(inspect.signature(native_rpc._post).parameters), ['url', 'body', 'timeout_ms'])
        self.assertTrue(inspect.iscoroutinefunction(native_rpc._post))
        source = inspect.getsource(native_rpc._post)
        self.assertIn('ProxyHandler({})', source)
        self.assertIn('_NoRedirect()', source)
        self.assertEqual(native_rpc._endpoint(PUBLIC_RPC['mainnet'], False, False), (True, PUBLIC_RPC['mainnet'] + '/'))
        self.assertEqual(native_rpc.RETRY_HTTP, frozenset((429, 500, 502, 503, 504)))
        self.assertEqual(native_rpc.RETRY_RPC, frozenset((-32011, -32603)))
        self.assertTrue(tp.COLLECTION_METHODS < native_rpc.METHODS | {'eth_getTransactionByHash'})
        self.assertNotIn('eth_getBalance', native_rpc.METHODS)
        # no environment access in the collector path (environment-file references are covered by the accepted
        # driver isolation test)
        for module in (tp, col, cp, hd, rp):
            self.assertNotRegex(inspect.getsource(module), r'os\.environ|getenv')


class LedgerTest(Tmp):
    def ledger(self, invocation='i', ceiling=5, restarts=3, ident='sim-auth'):
        path, digest = authorization(self.root, ceiling=ceiling, restarts=restarts, ident=ident)
        return tp.BudgetLedger(self.root / 'budget', col.load_authorization(path, digest), invocation=invocation)

    def use(self, ledger, n):
        for _ in range(n):
            ledger.reserve(1, 'rpc')

    def test_cumulative_across_restarts_and_exact_boundary(self):
        a = self.ledger('a').open()
        self.use(a, 3)
        a.close()
        b = self.ledger('b').open()
        self.assertEqual(b.used, 3)
        self.use(b, 2)
        with self.assertRaises(tp.BudgetExhausted):
            b.reserve(1, 'rpc')
        b.close()
        with self.assertRaises(tp.BudgetExhausted):
            self.ledger('c').open()
        with self.assertRaises(tp.BudgetExhausted):
            self.ledger('a').open()          # an old or new invocation id makes no difference

    def test_stale_lock_is_refused_and_never_removed(self):
        a = self.ledger('a').open()
        self.use(a, 1)
        a.close()
        lock = self.root / 'budget' / 'sim-auth.lock'
        lock.write_text('{"invocation": "crashed", "pid": 1}')       # a crashed invocation's leftover
        self.stop(lambda: self.ledger('b').open(), 'BUDGET_LEDGER_LOCKED', 'REFUSED')
        self.assertTrue(lock.exists())
        lock.unlink()                                                # manual review, then removal
        self.assertEqual(self.ledger('b').open().used, 1)

    def test_restart_bound(self):
        for i in range(3):
            self.ledger(f'r{i}', restarts=2).open().close()
        self.stop(lambda: self.ledger('r3', restarts=2).open(), 'SUPERVISOR_BOUND', 'REFUSED')

    def test_concurrent_open_is_refused(self):
        a = self.ledger('a').open()
        self.stop(lambda: self.ledger('b').open(), 'BUDGET_LEDGER_LOCKED', 'REFUSED')
        a.close()
        self.ledger('b').open().close()

    def test_rollback_tamper_and_foreign_ledgers_are_refused(self):
        a = self.ledger('a').open()
        self.use(a, 3)
        mark = a.position()
        a.close()
        path = self.root / 'budget' / 'sim-auth.jsonl'
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        path.write_bytes(b''.join(lines[:-2]))                       # truncation
        self.stop(lambda: self.ledger('b').open(), 'BUDGET_LEDGER_ROLLBACK', 'REFUSED')
        hwm = self.root / 'budget' / 'sim-auth.hwm.json'
        saved = hwm.read_bytes()
        hwm.unlink()                                                  # truncation plus a removed high-water mark
        self.stop(lambda: self.ledger('b').open(), 'BUDGET_LEDGER_ROLLBACK', 'REFUSED')
        # consistent truncation of both files: a run journal's recorded position still catches it
        prefix = b''.join(lines[:-2])
        path.write_bytes(prefix)
        hwm.write_text(json.dumps({'seq': len(lines[:-2]) - 1, 'sha256': tp.line_sha(lines[-3])}))
        self.stop(lambda: self.ledger('b').open(known_heads=[mark]), 'BUDGET_LEDGER_ROLLBACK', 'REFUSED')
        path.write_bytes(raw)
        hwm.write_bytes(saved)
        edited = raw.replace(b'"purpose":"rpc"', b'"purpose":"rpX"', 1)
        path.write_bytes(edited)
        self.stop(lambda: self.ledger('b').open(), 'BUDGET_LEDGER_CORRUPT', 'REFUSED')
        path.write_bytes(raw)
        self.stop(lambda: self.ledger('b', ceiling=50).open(), 'BUDGET_LEDGER_FOREIGN', 'REFUSED')
        self.assertEqual(self.ledger('b').open(known_heads=[mark]).used, 3)


# ------------------------------------------------------------------------------------------------ C. plan
class PlanTest(Tmp):
    def test_a_prime_layout_labels_and_derivation(self):
        for chain in ('testnet', 'mainnet'):
            w = World(self.root / chain, chain, new_vault=True)
            with w.patch():
                spec = w.spec()
                self.assertEqual(cp.check_spec(copy.deepcopy(spec), w.b0_plan), spec)
                ids = [s['id'] for s in spec['segments']]
                self.assertEqual(ids, ['S1', 'S3', 'S4'])           # no GOV Safe log segment on either chain
                s1 = cp.segment(spec, 'S1')
                self.assertEqual((s1['start'], s1['end']), (PIN + 1, ANCHOR))
                labels = {e['address']: e['labels'] for e in s1['addresses']}
                for entry in w.b0_plan['addresses']:
                    self.assertEqual(labels[entry['address']], sorted(entry['labels']))
                self.assertEqual(spec['extraTargets'], list(h.EXTRA_TARGETS[chain]))
                self.assertFalse(any(SAFE == e['address'] for seg in spec['segments'] for e in seg['addresses']))
                for address, entry in h.V4_CREATED[chain].items():                # post-pin only, labelled by contract
                    self.assertEqual(labels[address], [f'v4:{entry["name"]}'])
                    self.assertEqual([s['id'] for s in spec['segments'] if address in {e['address'] for e in s['addresses']}],
                                     ['S1'])
                factory, index, vault = w.new_vault
                self.assertEqual(cp.segment(spec, 'S4')['addresses'], [{'address': vault, 'labels': [f'vault:{factory}[1]']}])
                edited = copy.deepcopy(spec)
                edited['segments'][0]['addresses'][0]['labels'] = ['other']
                with self.assertRaises(HistoryRefused):
                    cp.check_spec(edited, w.b0_plan)
                with self.assertRaises(HistoryRefused) as ctx:
                    w.spec(header_scope='H2')
                self.assertEqual(ctx.exception.code, 'HEADER_SCOPE_NOT_APPROVED')
                with self.assertRaises(HistoryRefused) as ctx:
                    w.spec(new=[(factory, 1, '0x' + '99' * 20)])
                self.assertEqual(ctx.exception.code, 'UNKNOWN_CREATION_PATH')

    def test_create_derivation_reproduces_every_recorded_vault_and_beacon(self):
        count = 0
        for chain in ('mainnet', 'testnet'):
            state = json.loads((ROOT / 'snapshots/deployed' / chain / 'state.json').read_text())
            for factory, f in state['factories'].items():
                self.assertEqual(cp.create_address(factory, cp.BEACON_NONCE), f['beacon'])
                for v in f['vaults']:
                    self.assertEqual(cp.create_address(factory, cp.VAULT_NONCE_OFFSET + v['index']), v['vault'])
                    count += 1
                    self.assertNotEqual(cp.create_address(factory, cp.VAULT_NONCE_OFFSET + v['index'] + 1), v['vault'])
        self.assertEqual(count, 17)

    def test_budget_reproduces_the_cost_document(self):
        doc = {('mainnet', 10): (36, 477, 986), ('mainnet', 15): (53, 497, 1023), ('mainnet', 30): (106, 557, 1136),
               ('testnet', 15): (36, 38, 115), ('testnet', 30): (71, 75, 187)}
        for (chain, days), expected in doc.items():
            b = cp.budget(chain, cp.w_inc(chain, days))
            self.assertEqual((b['postPinLogPosts'], b['headerPosts'], b['subtotal']), expected, (chain, days))
        # RPC-COST §3.1 prints 26 header POSTs / 91 subtotal for testnet at 10 days; the de-duplicated header set
        # gives 25 / 90 (recorded as a one-POST arithmetic discrepancy of the estimate, FINDINGS P1-I02).
        b = cp.budget('testnet', cp.w_inc('testnet', 10))
        self.assertEqual((b['postPinLogPosts'], b['headerPosts'], b['subtotal']), (24, 25, 90))
        self.assertEqual([cp.budget('mainnet', cp.w_inc('mainnet', d), header_scope='H2')['subtotal'] for d in (10, 15, 30)],
                         [551, 588, 701])
        self.assertEqual((cp.budget('mainnet', 1)['b0EventBlockRecheckPosts'], cp.budget('testnet', 1)['b0EventBlockRecheckPosts']),
                         (59, 22))

    def test_creation_search_is_not_creation_evidence(self):
        def has_code(n):
            return 10 <= n < 20 or 50 <= n <= 100
        binary, probes = cp.binary_code_search(has_code, 0, 100)
        self.assertEqual((binary['block'], probes), (50, [50, 25, 37, 43, 46, 48, 49]))
        kary, _ = cp.kary_code_search(has_code, 0, 100, 20)
        self.assertEqual(kary['block'], 10)
        for result in (binary, kary):
            self.assertFalse(has_code(result['block'] - 1))
            self.assertEqual(result['label'], 'scanStart')
            self.assertNotIn('creationBlock', json.dumps(result))
            self.assertIn('UNAPPROVED', result['creationIdentity'])
        self.assertNotIn('creationBlock', inspect.getsource(col))


# ------------------------------------------------------------------------------------------------ C/D. collector
def collect(world, *, run_id='run-b', utc='2026-10-17T00:00:00Z', ceiling=100_000, ident='sim-auth', spec=None,
            invocation='inv', fake=None, **kw):
    clock = Clock()
    gate = gate_for(world.root, world.chain, fake or world.fake, clock, ceiling=ceiling, ident=ident, invocation=invocation)
    spec = spec or world.spec()
    collector = world.collector(spec, gate, clock, run_id=run_id, utc=utc, **kw)
    return run(collector.run(invocation)), gate, spec


class CollectorTest(Tmp):
    def test_end_to_end_both_chains_content_verified(self):
        for chain in ('testnet', 'mainnet'):
            w = World(self.root / chain, chain, new_vault=True)
            with w.patch():
                summary, gate, spec = collect(w)
                self.assertEqual(summary['status'], 'COLLECTED')
                self.assertEqual(gate.ledger.used, w.fake.posts)
                result, verified, headers = col.content_verify(w.root, 'run-b', spec)
                self.assertEqual(result['status'], 'CONTENT_VERIFIED')
                self.assertFalse(result['provenance']['authenticated'])
                self.assertIn((w.new_vault[2]), verified['targets'])
                self.assertNotIn(SAFE, verified['targets'])
                self.assertFalse(any(SAFE in json.dumps(b) for b in w.fake.bodies if is_logs(b)))
                self.assertEqual(headers['scope'], 'H1')
                sidecar = json.loads((w.root / 'run-b/headers.json').read_text())
                ends = {e['number'] for e in sidecar['headers'] if 'windowEnd' in e['reason']}
                for seg in spec['segments']:
                    self.assertTrue({b for _, b in h.logscan.windows(seg['start'], seg['end'])} <= ends)
                for name in os.listdir(w.root / 'run-b'):
                    self.assertFalse(name.endswith('.tmp'))
                self.assertNotIn('REREAD_EQUAL', json.dumps(summary))

    def test_resume_after_a_stop_at_every_post_is_byte_identical(self):
        w = World(self.root / 'ref', 'mainnet', new_vault=True)
        with w.patch():
            collect(w)
        reference = {p.relative_to(w.root / 'run-b'): p.read_bytes() for p in (w.root / 'run-b').rglob('*.json')}
        total = w.fake.posts
        self.assertGreater(total, 10)
        for k in range(1, total + 1):
            v = World(self.root / f'kill{k}', 'mainnet', new_vault=True)
            v.fake.faults.append(lambda c, body, k=k: (400, None) if c.posts == k else None)
            with v.patch():
                with self.assertRaises(CollectionStop) as ctx:
                    collect(v)
                self.assertEqual(ctx.exception.code, 'RPC_HTTP', k)
                summary, gate, _ = collect(v, invocation='resume')
            self.assertEqual(summary['status'], 'COLLECTED', k)
            got = {p.relative_to(v.root / 'run-b'): p.read_bytes() for p in (v.root / 'run-b').rglob('*.json')}
            self.assertEqual(got, reference, k)
            # every attempt, including the refused one and the repeated reads, stays in the one ledger
            self.assertEqual(gate.ledger.used, v.fake.posts, k)
            self.assertGreaterEqual(v.fake.posts, total + 1)

    def test_exhausted_run_cannot_continue_under_a_fresh_ledger(self):
        w = World(self.root, 'testnet')
        with w.patch():
            with self.assertRaises(tp.BudgetExhausted):
                collect(w, ceiling=6, ident='small')
            self.stop(lambda: collect(w, ceiling=6, ident='small', invocation='again'), 'HTTP_BUDGET_EXHAUSTED')
            self.stop(lambda: collect(w, ident='fresh', invocation='bypass'), 'JOURNAL_LEDGER', 'REFUSED')
            self.assertEqual(w.fake.posts, 6)

    def test_interrupted_atomic_write_leaves_tmp_that_is_discarded(self):
        w = World(self.root, 'testnet')
        with w.patch():
            real = os.replace
            calls = {'n': 0}

            def crash(src, dst):
                if str(src).endswith('.json.tmp') and '/chunks/' in str(src):
                    calls['n'] += 1
                    if calls['n'] == 1:
                        raise OSError('simulated crash')
                return real(src, dst)
            with mock.patch.object(col.os, 'replace', crash):
                with self.assertRaises(OSError):
                    collect(w)
            leftovers = list((w.root / 'run-b').rglob('*.tmp'))
            self.assertEqual(len(leftovers), 1)
            summary, _, _ = collect(w, invocation='resume')
            self.assertEqual(summary['status'], 'COLLECTED')
            journal = (w.root / 'run-b/journal.jsonl').read_text()
            self.assertIn('tmpDiscarded', journal)
            self.assertEqual(list((w.root / 'run-b').rglob('*.tmp')), [])

    def test_corrupt_cached_chunk_stops_without_refetch(self):
        w = World(self.root, 'testnet')
        with w.patch():
            collect(w)
            chunk = sorted((w.root / 'run-b/segments/S1/chunks').glob('*.json'))[0]
            doc = json.loads(chunk.read_bytes())
            for edit in ('count', 'filter', 'garbage'):
                original = chunk.read_bytes()
                if edit == 'count':
                    doc2 = copy.deepcopy(doc); doc2['windows'][0][2] += 1
                    chunk.write_bytes(cp.canonical_bytes(doc2))
                elif edit == 'filter':
                    doc2 = copy.deepcopy(doc); doc2['filter_id'] = '0' * 16
                    chunk.write_bytes(cp.canonical_bytes(doc2))
                else:
                    chunk.write_bytes(b'{"broken"')
                before = w.fake.posts
                with self.assertRaises(CollectionStop) as ctx:
                    collect(w, invocation=f'again-{edit}')
                self.assertEqual(ctx.exception.code, 'CACHED_CHUNK_INVALID', edit)
                # the stop happens before any log request for that chunk
                self.assertFalse(any(is_logs(b) for b in w.fake.bodies[before:]))
                chunk.write_bytes(original)

    def test_wrong_spec_resume_and_plan_for_another_spec_are_refused(self):
        w = World(self.root, 'testnet')
        with w.patch():
            collect(w)
            other = w.spec(anchor=ANCHOR - 10)
            self.stop(lambda: collect(w, spec=other, invocation='x'), 'SPEC_RUN_ROOT_MISMATCH', 'REFUSED')
            spec = w.spec()
            clock = Clock()
            gate = gate_for(w.root, 'testnet', w.fake, clock, invocation='y')
            bad = w.collector(spec, gate, clock)
            bad.spec_sha256 = '0' * 64
            self.stop(lambda: run(bad.run('y')), 'SPEC_SHA', 'REFUSED')
            plan = w.root / 'run-b/segments/S1/plan.json'
            doc = json.loads(plan.read_bytes())
            doc['addresses'][0]['labels'] = ['edited']
            plan.write_bytes(cp.canonical_bytes(doc))
            self.stop(lambda: collect(w, invocation='z'), 'PLAN_EXISTS_FOR_ANOTHER_SPEC', 'REFUSED')

    def test_stop_and_blocked_outcomes(self):
        cases = []

        def case(name, chain, setup, code, status=None, **kw):
            cases.append((name, chain, setup, code, status, kw))
        case('chain id', 'testnet', lambda w: w.fake.faults.append(
            lambda c, b: (200, {'jsonrpc': '2.0', 'id': b['id'], 'result': '0x1'}) if not isinstance(b, list)
            and b['method'] == 'eth_chainId' else None), 'RPC_CHAIN', 'REFUSED')
        case('anchor too recent', 'testnet', lambda w: setattr(w.fake, 'head', ANCHOR + 50), 'ANCHOR_TOO_RECENT', 'REFUSED')
        case('anchor reorg', 'testnet', lambda w: w.fake.header_overrides.update(
            {ANCHOR: {**w.fake.header(ANCHOR), 'hash': '0x' + '77' * 32}}), 'HISTORY_REORG')
        case('header null', 'testnet', lambda w: w.fake.header_overrides.update({3999: None}), 'HEADER_MISSING_OR_MISMATCH')
        case('gc01', 'mainnet', lambda w: w.fake.storage.update({(w.vaults[w.factories[0]], 4): amount(1)}),
             'GC01_NOT_SETTLED', 'BLOCKED')
        case('new vault code before fromBlock', 'mainnet', lambda w: w.fake.code.update({w.new_vault[2]: (50, '0x6080')}),
             'CODE_BEFORE_FROM_BLOCK', 'BLOCKED', new_vault=True)
        case('archive missing', 'mainnet', lambda w: w.fake.faults.append(
            lambda c, b: (200, [{'jsonrpc': '2.0', 'id': i['id'], 'error': {'code': -32000, 'message': 'missing trie node'}}
                                for i in b]) if isinstance(b, list) and b[0]['method'] == 'eth_getCode'
            and b[0]['params'][1] == hex(FROM - 1) else (200, {'jsonrpc': '2.0', 'id': b['id'],
                                                              'error': {'code': -32000, 'message': 'missing trie node'}})
            if not isinstance(b, list) and b['method'] == 'eth_getCode' and b['params'][1] == hex(FROM - 1) else None),
             'ARCHIVE_STATE_UNAVAILABLE', 'BLOCKED', new_vault=True)
        case('undeclared new vault', 'testnet', None, 'NEW_VAULT_REQUIRES_SPEC', 'INCOMPLETE', undeclared=True)
        case('removed log', 'testnet', lambda w: w.fake.add_rows([{**w.fake.row(w.stpros, [T['Transfer'], word(BOB), word(ALICE)],
                                                                                    3600, 3, amount(1)), 'removed': True}]),
             'LOG_REMOVED')
        case('event block reorg', 'testnet', lambda w: w.fake.header_overrides.update(
            {3100: {**w.fake.header(3100), 'hash': '0x' + '66' * 32}}), 'HISTORY_EVENT_REORG')
        for name, chain, setup, code, status, kw in cases:
            with self.subTest(name):
                w = World(self.root / name.replace(' ', '-'), chain,
                          new_vault=kw.get('undeclared', False) or kw.get('new_vault', False))
                if setup:
                    setup(w)
                with w.patch():
                    spec = w.spec(new=[]) if kw.get('undeclared') else w.spec()
                    with self.assertRaises(CollectionStop) as ctx:
                        collect(w, spec=spec)
                    self.assertEqual(ctx.exception.code, code, str(ctx.exception))
                    if status:
                        self.assertEqual(ctx.exception.status, status)
                    self.assertFalse((w.root / 'run-b/manifest.json').exists())
                    self.assertIn('"event":"stop"', (w.root / 'run-b/journal.jsonl').read_text())

    def test_reorg_at_the_end_of_collection_recheck(self):
        w = World(self.root, 'testnet')
        state = {'n': 0}

        def late(c, body):
            items = body if isinstance(body, list) else [body]
            if all(i['method'] == 'eth_getBlockByNumber' for i in items) and any(i['params'][0] == hex(ANCHOR) for i in items):
                state['n'] += 1
                if state['n'] >= 4:
                    answers = [c.answer(i) for i in items]
                    for a in answers:
                        if a['result'] and a['result']['number'] == hex(ANCHOR):
                            a['result'] = {**a['result'], 'hash': '0x' + '55' * 32}
                    return 200, answers if isinstance(body, list) else answers[0]
            return None
        w.fake.faults.append(late)
        with w.patch():
            with self.assertRaises(CollectionStop) as ctx:
                collect(w)
        self.assertEqual(ctx.exception.code, 'HISTORY_REORG')

    def test_supervisor_restarts_only_retry_exhaustion_and_counts_restarts(self):
        w = World(self.root, 'testnet')
        w.fake.faults.append(once(lambda c, body: (503, None), times=3))
        with w.patch():
            clock = Clock()
            spec = w.spec()
            path, digest = authorization(w.root, restarts=1)
            auth = col.load_authorization(path, digest)
            made = []

            def make():
                ledger = tp.BudgetLedger(w.root / 'budget', auth, invocation=f'sup{len(made)}', clock_ms=clock.ms)
                gate = tp.HttpGate('testnet', ledger, clock_ms=clock.ms, sleep=clock.sleep, post=w.fake.post)
                made.append(gate)
                return w.collector(spec, gate, clock)
            summary = run(col.supervise(make, max_restarts=1, cooldown_s=60, sleep=clock.sleep))
            self.assertEqual(summary['status'], 'COLLECTED')
            self.assertEqual(len(made), 2)
            self.assertIn(60, clock.sleeps)
            self.assertEqual(made[-1].ledger.used, w.fake.posts)
            w.fake.faults[:] = [lambda c, body: (503, None)]
            with self.assertRaises(CollectionStop) as ctx:
                run(col.supervise(make, max_restarts=5, cooldown_s=1, sleep=clock.sleep))
            self.assertIn(ctx.exception.code, ('SUPERVISOR_BOUND', 'RPC_HTTP'))

    def test_approved_checkpoint_is_reused_by_sha_and_never_recollected(self):
        w = World(self.root, 'mainnet', new_vault=True)
        with w.patch():
            collect(w)
            manifest = json.loads((w.root / 'run-b/manifest.json').read_bytes())
            s3 = next(s for s in manifest['segments'] if 'segments/S3/' in s['planFile'])
            headers_sha = hashlib.sha256((w.root / 'run-b/headers.json').read_bytes()).hexdigest()
            ref = {'planFile': s3['planFile'], 'planSha256': s3['planSha256'], 'chunks': s3['chunks'],
                   'headersFile': 'run-b/headers.json', 'headersSha256': headers_sha}
            anchor = ANCHOR + 50
            with self.assertRaises(HistoryRefused) as ctx:
                w.spec(anchor=anchor, checkpoints={'S3': ref})
            self.assertEqual(ctx.exception.code, 'CHECKPOINT_NOT_APPROVED')
            approval = {s3['planSha256']: {'chain': 'mainnet', 'start': FROM, 'end': PIN,
                                           'filterId': j.historical_filter_id([w.new_vault[2]]),
                                           'chunkListSha256': h.chunk_list_sha256(c['sha256'] for c in s3['chunks']),
                                           'headersSha256': headers_sha}}
            with mock.patch.dict(cp.APPROVED_CHECKPOINTS, approval):
                spec = w.spec(anchor=anchor, checkpoints={'S3': ref})
                self.assertEqual(cp.check_spec(copy.deepcopy(spec), w.b0_plan), spec)
                before = len(w.fake.bodies)
                summary, _, _ = collect(w, spec=spec, run_id='run-refresh')
                self.assertEqual(summary['status'], 'COLLECTED')
                fetched = [i for b in w.fake.bodies[before:] for i in (b if isinstance(b, list) else [b])
                           if i['method'] == 'eth_getLogs']
                self.assertTrue(fetched)
                self.assertFalse(any(int(i['params'][0]['toBlock'], 16) <= PIN for i in fetched))
                result, verified, headers = col.content_verify(w.root, 'run-refresh', spec)
                self.assertEqual(result['status'], 'CONTENT_VERIFIED')
                self.assertGreater(headers['imported'], 0)
                sidecar = json.loads((w.root / 'run-refresh/headers.json').read_text())
                self.assertEqual(sidecar['imported'][0]['headersSha256'], headers_sha)
                # a modified checkpoint file is refused, never silently re-collected
                chunk = w.root / s3['chunks'][0]['path']
                chunk.write_bytes(chunk.read_bytes().replace(b'"fetched_utc":"', b'"fetched_utc":"X'))
                self.stop(lambda: collect(w, spec=spec, run_id='run-refresh-2'), 'CHECKPOINT_MODIFIED', 'REFUSED')
            self.assertEqual(cp.APPROVED_CHECKPOINTS, {})

    def test_write_outside_run_root_is_refused(self):
        self.stop(lambda: col.atomic_write(self.root, '../x.json', b'{}'), 'WRITE_OUTSIDE_RUN_ROOT', 'REFUSED')
        (self.root / 'out').mkdir()
        os.symlink(self.root / 'out', self.root / 'link')
        self.stop(lambda: col.atomic_write(self.root, 'link/x.json', b'{}'), 'RUN_ROOT_SYMLINK', 'REFUSED')



# ------------------------------------------------------------------------------------------------ gas envelope
DEPLOYER = '0x' + 'de' * 20


def creation(fake, block, *, nonce=7, runtime=100, to=None, data='0x6080' + '00' * 10):
    """A sample whose block is a synthetic B0 event block and whose transaction creates ``CREATE(DEPLOYER, nonce)``."""
    tx = '0x' + hashlib.sha256(f'deploy {block}'.encode()).hexdigest()
    sample = {'label': f'Sample{block}', 'block': block, 'runtimeBytes': runtime,
              'address': cp.create_address(DEPLOYER, nonce), 'tx': tx}
    fake.block_extra[block] = {'gasUsed': hex(5_000_000), 'gasLimit': hex(1_000_000_000), 'transactions': [tx]}
    fake.txs[tx] = {'hash': tx, 'blockNumber': hex(block), 'blockHash': fake.hash(block), 'from': DEPLOYER,
                    'nonce': hex(nonce), 'to': to, 'gas': hex(7_000_000), 'input': data}
    return sample


class GasEnvelopeTest(Tmp):
    def test_doc_records_and_never_decides(self):
        fake = FakeChain('testnet')
        anchor = {'block': ANCHOR, 'hash': fake.hash(ANCHOR)}
        fake.block_extra[ANCHOR] = {'gasLimit': hex(1_000_000_000)}
        good = creation(fake, 150)
        entries = {150: {'hash': fake.hash(150)}}

        def doc(sample, block=None, tx='same', head=None):
            values = [head or fake.header(ANCHOR), block or fake.header(sample['block']),
                      fake.txs[sample['tx']] if tx == 'same' else tx]
            return col.gas_envelope_doc('testnet', anchor, entries, [sample], values)
        d = doc(good)
        row = d['samples'][0]
        self.assertEqual(row['status'], 'VERIFIED')
        self.assertEqual((row['txCount'], row['singleTxGasUsed'], row['txGasLimit']), (1, 5_000_000, 7_000_000))
        self.assertEqual(row['calldataGas'], 2 * 16 + 10 * 4)
        self.assertEqual((row['inputBytes'], row['inputSha256']),
                         (12, hashlib.sha256(bytes.fromhex('6080' + '00' * 10)).hexdigest()))
        self.assertEqual(row['evmLowerBound'], 21_000 + 32_000 + 72 + 200 * 100)
        self.assertEqual(d['summary'], {'maxAcceptedTxGasLimit': 7_000_000, 'maxSingleTxGasUsed': 5_000_000,
                                        'maxEvmLowerBound': 53_072 + 20_000})
        self.assertEqual(d['anchor']['gasLimit'], 1_000_000_000)
        self.assertIn('not a per-transaction cap approval', d['statement'])
        # two transactions in the block: the block gas is only an upper bound
        two = {**fake.header(150), 'transactions': [good['tx'], '0x' + '11' * 32]}
        self.assertIsNone(doc(good, block=two)['samples'][0]['singleTxGasUsed'])
        # a wrong creation, another nonce, a missing or unlisted transaction: recorded, never counted
        for name, tx in (('call', {**fake.txs[good['tx']], 'to': DEPLOYER}),
                         ('nonce', {**fake.txs[good['tx']], 'nonce': '0x8'}),
                         ('other block', {**fake.txs[good['tx']], 'blockNumber': hex(151)})):
            with self.subTest(name):
                bad = doc(good, tx=tx)
                self.assertEqual(bad['samples'][0]['status'], 'MISMATCH')
                self.assertEqual(bad['summary']['maxAcceptedTxGasLimit'], None)
        self.assertEqual(doc(good, tx=None)['samples'][0]['status'], 'INCOMPLETE')
        self.assertEqual(doc(good, block={**fake.header(150), 'transactions': []})['samples'][0]['status'], 'MISMATCH')
        no_gas = {k: v for k, v in fake.header(150).items() if k != 'gasUsed'}
        self.assertEqual(doc(good, block=no_gas)['samples'][0]['status'], 'INCOMPLETE')
        failed = col.gas_envelope_doc('testnet', anchor, entries, [good], [fake.header(ANCHOR), None, None])   # null results
        self.assertEqual((failed['samples'][0]['status'], failed['samples'][0]['blockHash']), ('INCOMPLETE', None))
        # a header that differs from the verified one is a reorg STOP, never a record
        self.stop(lambda: doc(good, block={**fake.header(150), 'hash': '0x' + '66' * 32}), 'HISTORY_EVENT_REORG')
        self.stop(lambda: doc(good, head={**fake.header(ANCHOR), 'hash': '0x' + '55' * 32}), 'HISTORY_REORG')

    def test_collection_reads_samples_at_reread_b0_event_blocks_in_one_post(self):
        w = World(self.root, 'mainnet')
        sample = creation(w.fake, 150)
        absent = {**creation(w.fake, 160, nonce=9), 'label': 'not a B0 event block'}
        w.fake.block_extra[ANCHOR] = {'gasLimit': hex(1_000_000_000)}
        with w.patch(), mock.patch.dict(cp.GAS_ENVELOPE, {'mainnet': (sample, absent)}):
            summary, gate, spec = collect(w)
        self.assertIn('gas-envelope.json', summary['files'])
        doc = json.loads((w.root / 'run-b/gas-envelope.json').read_text())
        self.assertEqual([r['block'] for r in doc['samples']], [150])
        self.assertEqual(doc['samples'][0]['status'], 'VERIFIED')
        self.assertEqual(doc['summary']['maxAcceptedTxGasLimit'], 7_000_000)
        reads = [b for b in w.fake.bodies if isinstance(b, list)
                 and any(i['method'] == 'eth_getTransactionByHash' for i in b)]
        self.assertEqual(len(reads), 1)
        self.assertEqual([(i['method'], i['params'][0]) for i in reads[0]],
                         [('eth_getBlockByNumber', hex(ANCHOR)), ('eth_getBlockByNumber', hex(150)),
                          ('eth_getTransactionByHash', sample['tx'])])
        self.assertEqual(gate.ledger.used, w.fake.posts)
        self.assertIn('"event":"gasEnvelope"', (w.root / 'run-b/journal.jsonl').read_text())

    def test_a_provider_error_is_recorded_transport_stops_propagate(self):
        w = World(self.root, 'mainnet')
        sample = creation(w.fake, 150)

        def tx_error(c, body):
            if isinstance(body, list) and any(i['method'] == 'eth_getTransactionByHash' for i in body):
                return 200, [c.answer(i) if i['method'] != 'eth_getTransactionByHash' else
                             {'jsonrpc': '2.0', 'id': i['id'], 'error': {'code': -32000, 'message': 'pruned'}} for i in body]
        w.fake.faults.append(tx_error)
        with w.patch(), mock.patch.dict(cp.GAS_ENVELOPE, {'mainnet': (sample,)}):
            summary, _, _ = collect(w)
            self.assertEqual(summary['status'], 'COLLECTED')
            doc = json.loads((w.root / 'run-b/gas-envelope.json').read_text())
            self.assertEqual((doc['status'], doc['detail']), ('NOT_COLLECTED', 'RPC_ERROR: pruned'))
            self.assertNotIn('samples', doc)
            w2 = World(self.root / 'w2', 'mainnet')
            creation(w2.fake, 150)
            w2.fake.faults.append(lambda c, body: (503, None) if isinstance(body, list) and any(
                i['method'] == 'eth_getTransactionByHash' for i in body) else None)
            with self.assertRaises(CollectionStop) as ctx:
                collect(w2)
            self.assertIn(ctx.exception.code, ('RPC_HTTP', 'TRANSPORT_ERROR'))
            self.assertFalse((w2.root / 'run-b/manifest.json').exists())

    def test_samples_are_the_recorded_deployments(self):
        names = {'YieldVaultFactory_Implementation': 'YieldVaultFactory_Implementation', 'YieldVault': 'YieldVault',
                 'YieldVaultFactory_test_Implementation': 'YieldVaultFactory_test_Implementation'}
        for chain, samples in cp.GAS_ENVELOPE.items():
            self.assertEqual(sorted(cp.GAS_ENVELOPE), ['mainnet', 'testnet'])
            for sample in samples:
                record = json.loads((ROOT / 'deployments' / chain / f'{names[sample["label"]]}.json').read_text())
                self.assertEqual(sample['address'], record['address'].lower())
                self.assertEqual(sample['runtimeBytes'], (len(record['deployedBytecode']) - 2) // 2)
                self.assertEqual(sample['block'], int(record['receipt']['blockNumber'], 16))

# ------------------------------------------------------------------------------------------------ C. headers.verify
class HeadersVerifyTest(Tmp):
    def setUp(self):
        super().setUp()
        # The new vault adds the pre-pin segment S3 (empty windows) and post-pin event blocks; a second post-pin
        # transfer at 3101 makes two adjacent event blocks for the parent-link check.
        self.w = World(self.root, 'mainnet', new_vault=True)
        self.w.fake.add_rows([self.w.fake.row(self.w.stpros, [T['Transfer'], word(BOB), word(ALICE)], 3101, 0, amount(1))])
        self.patcher = self.w.patch()
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        collect(self.w)
        self.spec = self.w.spec()
        _, self.verified, _ = col.content_verify(self.w.root, 'run-b', self.spec)
        self.sidecar = json.loads((self.w.root / 'run-b/headers.json').read_text())

    def check(self, doc, code, status='REFUSED'):
        with self.assertRaises(HistoryRefused) as ctx:
            hd.verify(self.verified, doc, self.spec, self.w.root)
        self.assertEqual((ctx.exception.code, ctx.exception.status), (code, status), str(ctx.exception))

    def entry(self, doc, number):
        return next(e for e in doc['headers'] if e['number'] == number)

    def test_valid_sidecar(self):
        try:
            result = hd.verify(self.verified, self.sidecar, self.spec, self.w.root)['status']
        except Exception as error:      # noqa: BLE001 - a refusal of the collector's own sidecar is the failure
            result = repr(error)
        self.assertEqual(result, 'VERIFIED')

    def test_row_hash_differs_from_header(self):
        doc = copy.deepcopy(self.sidecar)
        e = self.entry(doc, 3100)
        e['hash'] = '0x' + '44' * 32
        self.check(doc, 'HISTORY_EVENT_REORG')

    def test_missing_event_block_header(self):
        doc = copy.deepcopy(self.sidecar)
        doc['headers'] = [e for e in doc['headers'] if e['number'] != 3200]
        self.check(doc, 'HEADER_MISSING', 'INCOMPLETE')

    def test_plan_end_and_anchor_mismatch(self):
        doc = copy.deepcopy(self.sidecar)
        self.entry(doc, ANCHOR)['timestamp'] += 1
        self.check(doc, 'HEADER_PLAN_END')
        doc = copy.deepcopy(self.sidecar)
        self.entry(doc, ANCHOR)['hash'] = '0x' + '33' * 32
        self.check(doc, 'HISTORY_REORG')
        doc = copy.deepcopy(self.sidecar)
        doc['rechecks'] = [r if r['number'] != ANCHOR else {**r, 'hash': '0x' + '22' * 32} for r in doc['rechecks']]
        self.check(doc, 'HISTORY_REORG')
        doc = copy.deepcopy(self.sidecar)
        doc['rechecks'] = []
        self.check(doc, 'HEADER_RECHECK_MISSING', 'INCOMPLETE')

    def test_scope_downgrade_and_missing_window_end(self):
        doc = copy.deepcopy(self.sidecar)
        doc['scope'] = 'H2'
        self.check(doc, 'HEADER_SCOPE_DOWNGRADE')
        doc = copy.deepcopy(self.sidecar)
        doc['headers'] = [e for e in doc['headers'] if e['number'] != 1099]       # an empty pre-pin window end
        self.check(doc, 'HEADER_MISSING', 'INCOMPLETE')
        with self.assertRaises(HistoryRefused) as ctx:
            hd.verify(self.verified, self.sidecar, {**self.spec, 'headerScope': 'H2'}, self.w.root)
        self.assertEqual(ctx.exception.code, 'HEADER_SCOPE_NOT_APPROVED')

    def test_parent_link_and_unknown_reason(self):
        doc = copy.deepcopy(self.sidecar)
        self.assertIn('eventBlock', self.entry(doc, 3100)['reason'])
        self.assertIn('eventBlock', self.entry(doc, 3101)['reason'])
        self.assertIn('b0EventBlock', self.entry(doc, 110)['reason'])
        self.entry(doc, 3101)['parentHash'] = '0x' + '11' * 32
        self.check(doc, 'HEADER_CHAIN')
        doc = copy.deepcopy(self.sidecar)
        doc['headers'] = sorted(doc['headers'] + [{'number': 4499, 'hash': '0x' + '12' * 32, 'parentHash': '0x' + '12' * 32,
                                                   'timestamp': 1, 'reason': ['eventBlock']}], key=lambda e: e['number'])
        self.check(doc, 'HEADER_REASON')


# ------------------------------------------------------------------------------------------------ E. comparison
class CompareTest(Tmp):
    def side(self, w, run_id, role, observation=True):
        def ref(name):
            path = w.root / run_id / name
            return {'path': f'{run_id}/{name}', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} if path.exists() else None
        return {'role': role, 'root': w.root, 'manifest': ref('manifest.json'), 'headers': ref('headers.json'),
                'observation': ref('observation.json') if observation else None,
                'stateObservation': ref('state-observation.json') if observation else None}

    def pair(self, chain='mainnet', c_split=False):
        n = len(list(self.root.iterdir()))
        w = World(self.root / f'b{n}', chain)
        v = World(self.root / f'c{n}', chain)
        message = 'response size exceeded'
        if c_split:
            v.fake.faults.append(lambda c, body: (200, [{'jsonrpc': '2.0', 'id': i['id'], 'error': {'code': -32005, 'message': message}}
                                                        if int(i['params'][0]['fromBlock'], 16) == 3000
                                                        and int(i['params'][0]['toBlock'], 16) - 3000 > 400 else c.answer(i)
                                                        for i in (body if isinstance(body, list) else [body])])
                                 if is_logs(body) and isinstance(body, list) else (
                                     (200, {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32005, 'message': message}})
                                     if is_logs(body) and int(body['params'][0]['fromBlock'], 16) == 3000
                                     and int(body['params'][0]['toBlock'], 16) - 3000 > 400 else None))
        with w.patch():
            spec = w.spec()
            collect(w, utc='2026-10-17T00:00:00Z')
        v.prefix, v.b0_plan = w.prefix, w.b0_plan
        with w.patch():
            collect(v, utc='2026-10-18T09:00:00Z', run_id='run-c', spec=spec,
                    fake=_HeadShift(v.fake, 37), classifier={message} if c_split else None)
        return w, v, spec

    def write_obs(self, world, run_id, value, *, v1_format=cp.canonical_bytes):
        """A v1 observation and its companion state observation at the run's manifest anchor, the state document
        bound by the raw SHA-256 of the v1 bytes actually written. Returns those bytes."""
        manifest = json.loads((world.root / run_id / 'manifest.json').read_bytes())
        anchor = {'chain': manifest['chain'], 'chainId': manifest['chainId'], 'block': manifest['blockNumber'],
                  'blockHash': manifest['blockHash']}
        raw = v1_format({'schema': hi.OBSERVATION_SCHEMA, **anchor, 'value': value})
        (world.root / run_id / 'observation.json').write_bytes(raw)
        state = {'schema': rp.so.SCHEMA, **anchor, 'observationV1Sha256': hashlib.sha256(raw).hexdigest(), 'value': value}
        (world.root / run_id / 'state-observation.json').write_bytes(cp.canonical_bytes(state))
        return raw

    def test_independent_collections_compare_equal_and_splits_are_normalized(self):
        for split in (False, True):
            w, v, spec = self.pair(c_split=split)
            self.write_obs(w, 'run-b', 1)
            self.write_obs(v, 'run-c', 1)
            with w.patch():
                doc = rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(v, 'run-c', 'C'))
            self.assertEqual(doc['verdict'], 'EQUAL', doc['differences'])
            self.assertEqual(bool(doc['splits']['c']), split)
            self.assertNotRegex(json.dumps(doc), 'REREAD_EQUAL"|INDEPENDENTLY_REPRODUCED"|APPROVAL_RECORD_PREPARED|SOURCE_BOUND')

    def test_altered_content_is_different(self):
        w, v, spec = self.pair()
        self.write_obs(w, 'run-b', 1)
        self.write_obs(v, 'run-c', 1)
        # one data byte in C's chunk, re-hashed consistently (C's archive still content-verifies)
        manifest_path = v.root / 'run-c/manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        for seg in manifest['segments']:
            for ref in seg['chunks']:
                path = v.root / ref['path']
                doc = json.loads(path.read_bytes())
                with_data = [r for r in doc['logs'] if len(r['data']) > 2]
                if with_data and 'run-c' in ref['path']:
                    row = with_data[0]
                    row['data'] = row['data'][:-1] + ('0' if row['data'][-1] != '0' else '1')
                    path.write_bytes(cp.canonical_bytes(doc))
                    ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                    break
        manifest_path.write_bytes(cp.canonical_bytes(manifest))
        with w.patch():
            doc = rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(v, 'run-c', 'C'))
        self.assertEqual(doc['verdict'], 'DIFFERENT')
        self.assertTrue(any('.data' in d['path'] for d in doc['differences']))
        # an observation value
        w2, v2, spec2 = self.pair()
        self.write_obs(w2, 'run-b', 1)
        self.write_obs(v2, 'run-c', 2)
        with w2.patch():
            doc = rp.compare(spec2, self.side(w2, 'run-b', 'B'), self.side(v2, 'run-c', 'C'))
        self.assertEqual(doc['verdict'], 'DIFFERENT')

    def test_each_side_binds_its_state_observation_to_its_own_v1_bytes(self):
        w, v, spec = self.pair()
        b_raw = self.write_obs(w, 'run-b', 1)
        reformatted = lambda d: json.dumps(dict(reversed(list(d.items()))), indent=2).encode()  # noqa: E731
        c_raw = self.write_obs(v, 'run-c', 1, v1_format=reformatted)
        self.assertNotEqual(hashlib.sha256(b_raw).hexdigest(), hashlib.sha256(c_raw).hexdigest())
        state_path, v1_path = v.root / 'run-c/state-observation.json', v.root / 'run-c/observation.json'
        bound = json.loads(state_path.read_bytes())

        def compare():
            # side() re-hashes every file, so each altered document below is consistently referenced
            return rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(v, 'run-c', 'C'))

        def refused(code='COMPARE_STATE_BINDING'):
            with self.assertRaises(HistoryRefused) as ctx:
                compare()
            self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        with w.patch():
            # different raw v1 bytes (formatting only), each side correctly bound: EQUAL
            self.assertEqual(compare()['verdict'], 'EQUAL')
            c_sha = hashlib.sha256(c_raw).hexdigest()
            for pointer in (hashlib.sha256(b_raw).hexdigest(),                               # the other side's v1
                            hashlib.sha256(cp.canonical_bytes(json.loads(c_raw))).hexdigest(),  # canonical, not raw
                            '0' * 64, '0x' + c_sha, c_sha.upper(), c_sha[:63], 'run-c', 7, None):
                state_path.write_bytes(cp.canonical_bytes({**bound, 'observationV1Sha256': pointer}))
                refused()
            state_path.write_bytes(cp.canonical_bytes({k: x for k, x in bound.items() if k != 'observationV1Sha256'}))
            refused()
            state_path.write_bytes(cp.canonical_bytes(bound))
            self.assertEqual(compare()['verdict'], 'EQUAL')
            # the v1 file rewritten after binding (same parsed content): the stale pointer is refused
            v1_path.write_bytes(cp.canonical_bytes(json.loads(c_raw)))
            refused()
            v1_path.write_bytes(c_raw)
            # both documents must sit at the manifest anchor
            state_path.write_bytes(cp.canonical_bytes({**bound, 'block': bound['block'] - 1}))
            refused()
            moved = reformatted({**json.loads(c_raw), 'block': bound['block'] - 1})
            v1_path.write_bytes(moved)
            state_path.write_bytes(cp.canonical_bytes({**bound, 'block': bound['block'] - 1,
                                                       'observationV1Sha256': hashlib.sha256(moved).hexdigest()}))
            refused()
            v1_path.write_bytes(c_raw)
            state_path.write_bytes(cp.canonical_bytes({**bound, 'schema': hi.OBSERVATION_SCHEMA}))
            refused()
            state_path.write_bytes(cp.canonical_bytes(bound))
            # a caller-supplied digest never stands in for the bytes on disk
            side = self.side(v, 'run-c', 'C')
            side['observation']['sha256'] = hashlib.sha256(b_raw).hexdigest()
            with self.assertRaises(HistoryRefused) as ctx:
                rp.compare(spec, self.side(w, 'run-b', 'B'), side)
            self.assertEqual(ctx.exception.code, 'COMPARE_FILE_HASH')
            self.assertEqual(compare()['verdict'], 'EQUAL')

    def test_absent_evidence_is_incomplete_and_self_comparison_is_refused(self):
        w, v, spec = self.pair()
        self.write_obs(w, 'run-b', 1)
        with w.patch():
            doc = rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(v, 'run-c', 'C', observation=False))
            self.assertEqual(doc['verdict'], 'INCOMPLETE')
            with self.assertRaises(HistoryRefused) as ctx:
                rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(w, 'run-b', 'C'))
            self.assertEqual(ctx.exception.code, 'NOT_INDEPENDENT')
            (v.root / 'run-c/headers.json').unlink()
            self.write_obs(v, 'run-c', 1)
            doc = rp.compare(spec, self.side(w, 'run-b', 'B'), self.side(v, 'run-c', 'C'))
            self.assertEqual(doc['verdict'], 'INCOMPLETE')


class _HeadShift:
    """C's vantage: the same chain served a few blocks later (different head_at_plan)."""

    def __init__(self, fake, shift):
        self.fake, self.shift = fake, shift
        fake.head += shift

    def __getattr__(self, name):
        return getattr(self.fake, name)


# ------------------------------------------------------------------------------------------------ real B0 (optional)
class RealB0CollectionTest(unittest.TestCase):
    """A' collection over a fake post-pin chain with the real, unmodified B0 prefix (SIMULATION ONLY above the pin)."""

    def setUp(self):
        archive, directory = os.environ.get('FAROO_B0_ARCHIVE'), os.environ.get('FAROO_B0_INVENTORY_DIR')
        if not archive or not directory:
            self.skipTest('NOT RUN: set FAROO_B0_ARCHIVE and FAROO_B0_INVENTORY_DIR to the archived B0 evidence')
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.directory = Path(directory)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                m = re.fullmatch(r'logscan/(mainnet|testnet)/chunks/([0-9]+-[0-9]+\.json)', member.name)
                if member.isfile() and m:
                    target = self.root / 'b0' / m[1] / 'chunks' / m[2]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_prime_over_the_real_b0_prefix(self):
        for chain in ('testnet', 'mainnet'):
            plan_raw = (self.directory / chain / 'plan.json').read_bytes()
            (self.root / 'b0' / chain / 'plan.json').write_bytes(plan_raw)
            plan = json.loads(plan_raw)
            rows = []
            for p in sorted((self.root / 'b0' / chain / 'chunks').glob('*.json')):
                rows += json.loads(p.read_bytes())['logs']
            pin = plan['end']
            overrides = {int(r['blockNumber'], 16): r['blockHash'] for r in rows}
            overrides[pin] = plan['end_block_hash']
            anchor = pin + 2500
            fake = FakeChain(chain, rows, head=anchor + 200, hash_overrides=overrides, tag='real-b0')
            for a in plan['addresses']:
                fake.code[a['address']] = (plan['start'], '0x6080')
            fake.add_rows([fake.row(REG[chain]['stpros'], [T['Transfer'], word(ZERO20), word(ALICE)], pin + 7, 0, amount(1))])
            if chain == 'mainnet':
                fake.code[SAFE] = (plan['start'] + 10, '0x6080')
                fake.add_rows([fake.row(SAFE, ['0x' + '5a' * 32], plan['start'] + 20, 0)])
                vault001 = next(v['vault'] for v in plan['vaults'] if v['factory'] == REG[chain]['factories'][0] and v['index'] == 0)
                fake.calls[(vault001, abi.calldata('phase()'))] = amount(3)
                fake.storage[(vault001, 4)] = amount(3)
            spec = cp.spec(chain, anchor_block=anchor, anchor_hash=fake.hash(anchor), b0_plan=plan,
                           b0_plan_sha256=h.B0_PREFIX[chain]['planSha256'])
            clock = Clock()
            (self.root / f'auth-{chain}').mkdir()
            gate = gate_for(self.root / f'auth-{chain}', chain, fake, clock)
            c = col.Collector(spec, hashlib.sha256(cp.canonical_bytes(spec)).hexdigest(), self.root, f'run-{chain}', gate,
                              b0_dir=f'b0/{chain}', utc=lambda: '2026-10-17T00:00:00Z', clock_ms=clock.ms)
            summary = run(c.run('real-b0'))
            self.assertEqual(summary['status'], 'COLLECTED')
            result, verified, _ = col.content_verify(self.root, f'run-{chain}', spec)
            self.assertEqual(result['status'], 'CONTENT_VERIFIED')
            self.assertEqual(verified['b0Prefix']['chunkListSha256'], h.B0_PREFIX[chain]['chunkListSha256'])
            self.assertEqual(gate.ledger.used, fake.posts)
            # every gas-envelope sample is a re-read B0 event block of the real archive and its deployment row
            sidecar = json.loads((self.root / f'run-{chain}/headers.json').read_text())
            b0_blocks = {e['number'] for e in sidecar['headers'] if 'b0EventBlock' in e['reason']}
            envelope = json.loads((self.root / f'run-{chain}/gas-envelope.json').read_text())
            self.assertEqual([r['block'] for r in envelope['samples']], [x['block'] for x in cp.GAS_ENVELOPE[chain]])
            for x in cp.GAS_ENVELOPE[chain]:
                self.assertIn(x['block'], b0_blocks)
                self.assertTrue(any(r['transactionHash'] == x['tx'] and r['address'] == x['address']
                                    and int(r['blockNumber'], 16) == x['block'] for r in rows))


if __name__ == '__main__':
    unittest.main()
