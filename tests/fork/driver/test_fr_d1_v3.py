"""S3 v3 records: the exact ``historicalReads`` a Fork of the deployed V4 topology needs (test-only).

The record names each public read outside the fork block (method and exact parameters, in the form a forked anvil
was measured to forward) and the answer it must get. The proxy forwards only those; any other historical read is
refused, and a different answer stops the proxy. The loopback round trips use a local upstream anvil and a
SIMULATION record (never a public endpoint); they need anvil inside an approved export, otherwise NOT RUN.
"""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import fork_launcher as fl  # noqa: E402
import fork_mode as fm  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

TX = '0x' + 'ab' * 32
ADDR = '0x' + '9c' * 20
SENDER = '0x' + '5e' * 20
VALIDITY = {'notBeforeUtc': '2026-10-01T00:00:00Z', 'notAfterUtc': '2026-12-31T00:00:00Z',
            'stopConditions': ['SIMULATION ONLY']}


def reads(*, tx=TX, block='0x5', header_hash='0x' + 'cd' * 32, code_block='0x4', sender=SENDER, nonce=0,
          input_keccak='0x' + '11' * 32):
    return [{'method': 'eth_getBlockByNumber', 'params': [block, True], 'expect': {'hash': header_hash}, 'purpose': 'pin'},
            {'method': 'eth_getCode', 'params': [ADDR, code_block], 'expect': {'code': '0x'}, 'purpose': 'absence'},
            {'method': 'eth_getTransactionByHash', 'params': [tx], 'purpose': 'predeploy',
             'expect': {'from': sender, 'nonce': nonce, 'to': None, 'inputKeccak': input_keccak}}]


def record(upstream, historical, ident='sim-s3-mainnet-v3test'):
    return scope.fork_record('mainnet', mode='SIMULATION', ident=ident, upstream=upstream, ceiling=1000, restarts=1,
                             s1_sha256='1' * 64, s2_sha256='2' * 64, validity=VALIDITY,
                             retention='local test directory only', owner={'decision': 'SIMULATION ONLY'},
                             prerequisites=['SIMULATION ONLY'], statement='SIMULATION ONLY: v3 test record',
                             bases=('0' * 40, '0' * 40), historical_reads=historical)


class V3RecordTest(unittest.TestCase):
    def refused(self, fn, code):
        with self.assertRaises(CollectionStop) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception

    def test_a_v3_record_is_v2_plus_the_read_list(self):
        doc = record('http://127.0.0.1:1/', reads())
        self.assertEqual(doc['schema'], scope.FORK_SCHEMA_V3)
        self.assertEqual(doc['methods'], list(scope.FORK_STATE_METHODS) + [scope.TX_METHOD])
        scope.check_fork_authorization(doc)
        v2 = record('http://127.0.0.1:1/', None)
        self.assertEqual(v2['schema'], scope.FORK_SCHEMA)                          # v2 unchanged
        self.assertNotIn('historicalReads', v2['scope'])
        scope.check_fork_authorization(v2)
        v2['methods'].append(scope.TX_METHOD)
        self.refused(lambda: scope.check_fork_authorization(v2), 'AUTHORIZATION_METHODS')

    def test_malformed_read_lists_are_refused(self):
        base = reads()
        cases = {
            'empty': [],
            'too many': [dict(base[1], params=[ADDR, hex(4 + i)]) for i in range(65)],
            'header without full transactions': [dict(base[0], params=['0x5', False])],
            'code expected present': [dict(base[1], expect={'code': '0x60'})],
            'leading zero block': [dict(base[1], params=[ADDR, '0x04'])],
            'state read': [{'method': 'eth_getStorageAt', 'params': [ADDR, '0x0', '0x4'], 'expect': {}, 'purpose': 'x'}],
            'logs': [{'method': 'eth_getLogs', 'params': [{}], 'expect': {}, 'purpose': 'x'}],
            'duplicate': [base[1], copy.deepcopy(base[1])],
            'no purpose': [dict(base[1], purpose=' ')],
        }
        for why, historical in cases.items():
            with self.subTest(why=why):
                self.refused(lambda: scope.check_fork_authorization(record('http://127.0.0.1:1/', historical)),
                             'AUTHORIZATION_HISTORICAL_READS')

    def test_only_exact_entries_pass_the_request_rule(self):
        r = reads()
        ok = lambda m, p: scope.check_fork_request({'method': m, 'params': p}, 9, '0x' + 'ee' * 32, r)  # noqa: E731
        ok('eth_getBlockByNumber', ['0x5', True])
        ok('eth_getCode', [ADDR, '0x4'])
        ok('eth_getTransactionByHash', [TX])
        ok('eth_getCode', [ADDR, '0x9'])                                                # the fork block: v2 rule
        for m, p in (('eth_getBlockByNumber', ['0x5', False]), ('eth_getBlockByNumber', ['0x6', True]),
                     ('eth_getCode', [ADDR, '0x3']), ('eth_getCode', ['0x' + '9d' * 20, '0x4']),
                     ('eth_getTransactionByHash', ['0x' + 'ac' * 32]), ('eth_getStorageAt', [ADDR, '0x0', '0x4'])):
            with self.subTest(method=m, params=p):
                self.refused(lambda: ok(m, p), 'FORK_PARAMS')
        self.refused(lambda: scope.check_fork_request({'method': 'eth_getCode', 'params': [ADDR, '0x4']}, 9,
                                                      '0x' + 'ee' * 32, None), 'FORK_PARAMS')       # v2: refused

    def test_a_different_answer_stops(self):
        r = reads(input_keccak=keccak256(b'\x60\x00'))
        items = [{'jsonrpc': '2.0', 'id': i, 'method': x['method'], 'params': x['params']} for i, x in enumerate(r)]
        good = [{'id': 0, 'result': {'hash': '0x' + 'cd' * 32, 'number': '0x5'}}, {'id': 1, 'result': '0x'},
                {'id': 2, 'result': {'hash': TX, 'blockNumber': '0x3', 'from': SENDER, 'nonce': '0x0', 'to': None,
                                     'input': '0x6000'}}]
        scope.check_fork_response(items, json.dumps(good), 1672, '0x' + 'ee' * 32, r, 9)
        bad = {'header hash': (0, {'hash': '0x' + 'ce' * 32, 'number': '0x5'}), 'code present': (1, '0x60'),
               'tx after the fork block': (2, dict(good[2]['result'], blockNumber='0xa')),
               'tx other sender': (2, dict(good[2]['result'], **{'from': '0x' + '5f' * 20})),
               'tx other nonce': (2, dict(good[2]['result'], nonce='0x1')),
               'tx other input': (2, dict(good[2]['result'], input='0x6001')),
               'tx is a call': (2, dict(good[2]['result'], to=ADDR)),
               'missing answer': (2, None)}
        for why, (i, result) in bad.items():
            with self.subTest(why=why):
                body = copy.deepcopy(good)
                body[i]['result'] = result
                stop = self.refused(lambda: scope.check_fork_response(items, json.dumps(body), 1672, '0x' + 'ee' * 32,
                                                                      r, 9), 'FORK_HISTORICAL_READ_MISMATCH')
                self.assertEqual(stop.status, 'STOP')

    def test_the_transaction_method_is_a_v3_method_only(self):
        with tempfile.TemporaryDirectory() as d:
            v2 = record('http://127.0.0.1:1/', None)
            v2['methods'] = v2['methods'] + [scope.TX_METHOD]
            path = Path(d) / 'v2.json'
            path.write_bytes(scope.record_bytes(v2))
            import hashlib
            with self.assertRaises(CollectionStop) as ctx:
                fm.load_authorization(path, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(ctx.exception.code, 'AUTHORIZATION_KEYS')


@unittest.skipUnless(shutil.which('anvil') and (ROOT / 'EXPORT-MANIFEST.txt').is_file(),
                     'NOT RUN: Foundry binaries run only inside an approved export')
class V3LoopbackTest(unittest.TestCase):
    """A forked anvil behind the real proxy: the listed historical reads reach the upstream; others never do."""

    def setUp(self):
        import localchain as lc
        (ROOT / 'cache_forge/rehearsal').mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / 'cache_forge/rehearsal')
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.up = lc.LocalChain(1672, 1_760_000_000)
        self.addCleanup(self.up.close)
        self.up.warp(12)
        init = '0x60006000a06001601160003960016000f300'            # emits a log, leaves a 1-byte runtime
        self.contract, rec = self.up.create('v3 test creation', SENDER, init)
        self.tx = rec['transactionHash'].lower()
        self.created = rec['block']
        for _ in range(3):
            self.up.warp(12)
        self.head = self.up.block()
        tx = self.up.rpc('eth_getTransactionByHash', [self.tx])
        self.reads = [
            {'method': 'eth_getBlockByNumber', 'params': [hex(self.created), True],
             'expect': {'hash': self.up.rpc('eth_getBlockByNumber', [hex(self.created), False])['hash']}, 'purpose': 'pin'},
            {'method': 'eth_getCode', 'params': [self.contract, hex(self.created - 1)], 'expect': {'code': '0x'},
             'purpose': 'absence before creation'},
            {'method': 'eth_getTransactionByHash', 'params': [self.tx], 'purpose': 'creation transaction',
             'expect': {'from': tx['from'].lower(), 'nonce': int(tx['nonce'], 16), 'to': None,
                        'inputKeccak': keccak256(bytes.fromhex(tx['input'][2:]))}}]

    def launch_flow(self, historical, flow, name):
        import hashlib
        doc = record(self.up.node.url + '/', historical, ident=f'sim-s3-mainnet-{name}')
        path = self.dir / f'{name}.json'
        path.write_bytes(scope.record_bytes(doc))
        auth = fm.load_authorization(path, hashlib.sha256(path.read_bytes()).hexdigest())
        auth = {**auth, 'forkBlock': int(self.head['number'], 16), 'forkBlockHash': self.head['hash'],
                'anchorBinding': {'reconciliationSha256': '3' * 64, 'specSha256': '4' * 64}}
        ledger = tp.BudgetLedger(self.dir / f'budget-{name}', auth, invocation='v3-test').open()
        self.addCleanup(ledger.close)
        return fl.run(auth, ledger, ROOT, flow)

    def test_listed_reads_pass_and_others_are_refused(self):
        def flow(node):
            out = {'tx': node.rpc('eth_getTransactionByHash', [self.tx])['hash'],
                   'absent': node.rpc('eth_getCode', [self.contract, hex(self.created - 1)]),
                   'pin': node.rpc('eth_getBlockByNumber', [hex(self.created), False])['hash']}
            try:
                node.rpc('eth_getCode', [self.contract, hex(self.created - 2)])     # not listed
                out['unlisted'] = 'ANSWERED'
            except (ValueError, OSError):
                out['unlisted'] = 'REFUSED'
            return out
        out = self.launch_flow(self.reads, flow, 'pass')
        self.assertEqual(out['result']['tx'], self.tx)
        self.assertEqual(out['result']['absent'], '0x')
        self.assertEqual(out['result']['pin'], self.reads[0]['expect']['hash'])
        self.assertEqual(out['result']['unlisted'], 'REFUSED')
        self.assertIn('FORK_PARAMS', [r['code'] for r in out['refused']])
        self.assertIsNone(out['stopped'])
        self.assertEqual(out['methods'].get('eth_getTransactionByHash'), 1)

    def test_a_wrong_recorded_answer_stops_the_proxy(self):
        wrong = copy.deepcopy(self.reads)
        wrong[2]['expect']['nonce'] += 1

        def flow(node):
            try:
                node.rpc('eth_getTransactionByHash', [self.tx])
                return 'ANSWERED'
            except (ValueError, OSError):
                return 'REFUSED'
        out = self.launch_flow(wrong, flow, 'stop')
        self.assertEqual(out['result'], 'REFUSED')
        self.assertEqual(out['stopped'], 'FORK_HISTORICAL_READ_MISMATCH')


if __name__ == '__main__':
    unittest.main()
