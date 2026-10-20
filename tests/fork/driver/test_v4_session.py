"""The S3 session wiring of the V4 scenario, end to end on loopback (SIMULATION; never a public endpoint).

Upstream: the SYNTHETIC world of ``test_v4_fork`` (recorded mainnet fixture with the pre-deployment, P-0 and the
ReleaseAssertions created before the anchor). The Fork: ``fork_launcher.launch`` behind the real ``ForkBudgetProxy``
with a SIMULATION **v3** record whose ``historicalReads`` are the reads the release checkpoints make (the archive pin
header, the code absence of the four V4 contracts before their coverage, the five pre-deployment transactions). The
scenario is ``v4_session.scenario``, the one a real session calls, fed with an S1-format manifest of the archive.

What this proves offline: the release tool's H checkpoint and R4 postcheck work through the bounded proxy with only the
listed historical reads; the runner's gates and A-G run on the forked node; the session result has the ledger shape the
reviewed ``s3_run`` reads. SYNTHETIC adaptations are those of ``test_v4_fork`` (loaded fixture state, SLP stub code,
labelled actors) and are never real-state evidence.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib.keccak import keccak256  # noqa: E402
import test_v4_fork as tvf  # noqa: E402

READY = tvf.READY


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the execution topologies in this export')
class V4SessionWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fork_launcher as fl
        import fork_mode as fm
        import fr_d1_scope as scope
        import transport as tp
        import v4_session as vs
        run, deployed, actors, inputs, history, pin = tvf.synthetic_world('test-v4-session-upstream')
        cls.upstream = run
        head = run.chain.block()
        anchor, anchor_hash = int(head['number'], 16), head['hash']
        root = Path(run.archive_root)
        rel = lambda p: str(Path(p).relative_to(root))  # noqa: E731
        manifest = {'schema': 'faroo-fork-history-v1', 'chain': 'mainnet', 'chainId': 1672, 'fromBlock': 1,
                    'blockNumber': anchor, 'blockHash': anchor_hash, 'inventorySha256': '0x' + '00' * 32,
                    'targets': run.archive_targets,
                    'segments': [{'planFile': rel(s['planFile']), 'planSha256': s['planSha256'],
                                  'chunks': [{'path': rel(c['path']), 'sha256': c['sha256']} for c in s['chunks']]}
                                 for s in history]}
        (root / 's1-sim').mkdir(parents=True, exist_ok=True)
        (root / 's1-sim/manifest.json').write_text(json.dumps(manifest))
        msha = hashlib.sha256((root / 's1-sim/manifest.json').read_bytes()).hexdigest()
        topo = {**run.topo, 'history': run.archive_rows,
                'binding': {'forkBlock': anchor, 'forkBlockHash': anchor_hash, 's1ManifestSha256': msha,
                            'executionSource': 'SIMULATION'}}
        c = deployed['contracts']
        plan = inputs['predeployed']['plan']
        reads = [{'method': 'eth_getBlockByNumber', 'params': [hex(pin), True], 'purpose': 'archive pin',
                  'expect': {'hash': run.chain.rpc('eth_getBlockByNumber', [hex(pin), False])['hash']}}]
        reads += [{'method': 'eth_getCode', 'params': [c[n]['address'], hex(pin)], 'expect': {'code': '0x'},
                   'purpose': f'{n} absent before its coverage'} for n in ('timelock', 'distributorProxy', 'bridgeProxy', 'assertions')]
        reads += [{'method': 'eth_getTransactionByHash', 'params': [e['hash']], 'purpose': f'predeploy step {t["step"]}',
                   'expect': {'from': plan['inputs']['deployer'], 'nonce': t['nonce'],
                              'to': None if t['kind'] == 'CREATE' else t['to'],
                              'inputKeccak': keccak256(bytes.fromhex(t['data'][2:]))}}
                  for t, e in zip(plan['transactions'], inputs['predeployed']['transactions'])]
        validity = {'notBeforeUtc': '2026-10-01T00:00:00Z', 'notAfterUtc': '2026-12-31T00:00:00Z', 'stopConditions': ['SIMULATION']}
        record = scope.fork_record('mainnet', mode='SIMULATION', ident='sim-s3-mainnet-v4wiring', upstream=run.chain.node.url + '/',
                                   ceiling=20_000, restarts=1, s1_sha256='1' * 64, s2_sha256='2' * 64, validity=validity,
                                   retention='local test directory only', owner={'decision': 'SIMULATION ONLY'},
                                   prerequisites=['SIMULATION ONLY'], statement='SIMULATION ONLY: V4 session wiring',
                                   bases=('0' * 40, '0' * 40), historical_reads=reads)
        (ROOT / 'cache_forge/rehearsal').mkdir(parents=True, exist_ok=True)
        cls._tmp = tempfile.TemporaryDirectory(dir=ROOT / 'cache_forge/rehearsal')
        d = Path(cls._tmp.name)
        path = d / 'sim-s3-mainnet-v4wiring.json'
        path.write_bytes(scope.record_bytes(record))
        auth = fm.load_authorization(path, hashlib.sha256(path.read_bytes()).hexdigest())
        auth = {**auth, 'forkBlock': anchor, 'forkBlockHash': anchor_hash,
                'anchorBinding': {'reconciliationSha256': '3' * 64, 'specSha256': '4' * 64}}
        ledger = tp.BudgetLedger(d / 'budget', auth, invocation='v4-session-wiring').open()
        proxy, node = fl.launch(auth, ledger, ROOT)
        try:
            cls.result = vs.scenario(node, topo, root, 's1-sim', name='test-v4-session-fork', synthetic={
                'deployed': deployed, 'actors': actors, 'inputs': inputs, 'createdStart': pin + 1,
                'rpcFactory': tvf.synthetic_rpc(run.m)})
            cls.proxy = {'forwarded': proxy.forwarded, 'methods': dict(proxy.methods_seen), 'refused': list(proxy.refused),
                         'stopped': None if proxy.stopped is None else proxy.stopped.code, 'ledgerUsed': ledger.used}
            cls.reads = reads
        finally:
            node.close()
            proxy.__exit__(None, None, None)
            ledger.close()
        (run.dir / 'v4-session-wiring.json').write_text(json.dumps(
            {'proxy': cls.proxy, 'v4': cls.result['v4'], 'ledger': cls.result['ledgers']}, indent=1, sort_keys=True, default=str))

    @classmethod
    def tearDownClass(cls):
        cls.upstream.close()
        cls._tmp.cleanup()

    def test_the_session_scenario_passes_through_the_bounded_proxy(self):
        v4 = self.result['v4']
        self.assertEqual((v4['status'], v4['stoppedAt'], v4['error']), ('COMPLETED', None, None))
        self.assertEqual((v4['upgradeVerdict'], v4['businessVerdict'], v4['overallVerdict']), ('PASS', 'BLOCKED', 'BLOCKED'))
        for stage in ('Hcheckpoint', 'R4'):
            self.assertEqual(v4['stages'][stage]['status'], 'PASS', v4['stages'][stage])
        self.assertEqual(v4['binding']['class'], 'SYNTHETIC SIMULATION')
        ledger = self.result['ledgers']['V4.release']
        self.assertEqual((ledger['verdict'], ledger['upgradeVerdict'], ledger['businessVerdict']), ('BLOCKED', 'PASS', 'BLOCKED'))
        # the ledger points to the complete run document by SHA-256 (the ledger itself is a summary)
        ev = v4['evidence']
        self.assertEqual(hashlib.sha256(Path(ev['path']).read_bytes()).hexdigest(), ev['sha256'])
        full = json.loads(Path(ev['path']).read_text())
        self.assertTrue(full['stages']['U']['transactionHash'] and full['acceptance']['G']['receipts'] and full['transcript'])

    def test_only_listed_historical_reads_left_the_fork(self):
        self.assertIsNone(self.proxy['stopped'])
        self.assertEqual(self.proxy['refused'], [])
        self.assertEqual(self.proxy['methods'].get('eth_getTransactionByHash'), 5)       # anvil caches each one
        self.assertTrue(set(self.proxy['methods']) <= {'eth_chainId', 'eth_getBlockByNumber', 'eth_getBalance',
                                                       'eth_getTransactionCount', 'eth_getCode', 'eth_getStorageAt',
                                                       'eth_getTransactionByHash'})

    def test_acceptance_ran_after_the_gates_with_separate_g_verdicts(self):
        acc = self.result['v4']['acceptance']
        self.assertEqual({k: v['verdict'] for k, v in acc.items() if v['verdict'] == 'FAIL'}, {})
        self.assertEqual(acc['G-INTEGRATION']['verdict'], 'BLOCKED')
        self.assertEqual(acc['G']['verdict'], 'PASS')
        checks = {c['id']: c['verdict'] for c in self.result['ledgers']['V4.release']['checks']}
        self.assertEqual(checks['acceptance.G-INTEGRATION'], 'BLOCKED')
        self.assertTrue(all(checks[k] == 'PASS' for k in ('identity', 'H', 'Hcheckpoint', 'UbeforeDelay', 'U', 'Ugas', 'R4', 'UPGRADE')))
        self.assertEqual(checks['BUSINESS'], 'BLOCKED')

    def test_a_foreign_manifest_is_refused(self):
        import v4_session as vs
        from localchain import ExecutionError
        with self.assertRaises(ExecutionError):
            vs.archive_segments(self.upstream.archive_root, 's1-sim', 'f' * 64)


if __name__ == '__main__':
    unittest.main()
