"""Tests of the real-state topology (``real_state``). Test-only, offline.

Needs FAROO_REAL_STATE_RUNS (a directory holding, per run id ``s1-<chain>-r1``, a staged B0 prefix and the archived
run root) and FAROO_LAUNCHER_EVIDENCE (records, S1/S2 ledgers, final reconciliations); otherwise NOT RUN.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collector as col  # noqa: E402
import fork_mode as fm  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import history as h  # noqa: E402
from localchain import ExecutionError  # noqa: E402
import real_state as rs  # noqa: E402
import release_config as rc  # noqa: E402

NOW = '2026-10-19T00:00:00Z'
MULTISEND = '0x9641d764fc13c8b624c04430c7356c1c7c8102e2'


class RealTopologyTest(unittest.TestCase):
    def setUp(self):
        runs, evidence = os.environ.get('FAROO_REAL_STATE_RUNS'), os.environ.get('FAROO_LAUNCHER_EVIDENCE')
        if not runs or not evidence:
            self.skipTest('NOT RUN: set FAROO_REAL_STATE_RUNS and FAROO_LAUNCHER_EVIDENCE')
        self.runs, self.ev = Path(runs), Path(evidence)
        self.records = json.loads((self.ev / 'records/records.json').read_text())['records']

    def record(self, stage, chain):
        e = next(x for x in self.records if (x['stage'], x['chain']) == (stage, chain))
        return self.ev / 'records' / e['file'], e['sha256']

    def bound(self, chain):
        p3, s3 = self.record('S3', chain)
        (p1, h1), (p2, h2) = self.record('S1', chain), self.record('S2', chain)
        rec = self.ev / f'reconciliation/{chain}.reconciliation.json'
        return scope.bind_fork_anchor(fm.load_authorization(p3, s3), s1=(p1, h1, self.ev / f'runs/s1-{chain}/budget'),
                                      s2=(p2, h2, self.ev / f'runs/s2-{chain}/budget'),
                                      reconciliation=(rec, hashlib.sha256(rec.read_bytes()).hexdigest()),
                                      load=col.load_authorization, now_utc=NOW)

    def topo(self, chain, auth=None, **kw):
        run_id = f's1-{chain}-r1'
        return rs.topology(chain, history_root=self.runs / run_id, run_id=run_id, auth=auth or self.bound(chain), **kw)

    def test_mainnet_topology_is_the_real_chain(self):
        t = self.topo('mainnet', multisend_call_only=MULTISEND)
        m, reg = t['meta'], h.registry('mainnet')
        self.assertEqual((m['evidenceClass'], m['chainId']), ('E_FRESH', 1672))
        self.assertEqual((m['stpros'], m['proxyAdmin'], m['safe']), (reg['stpros'], reg['proxy_admin'], h.GOV_SAFE))
        self.assertEqual((m['factory'], m['factoryTest']), ('0xc9fb7dc52b0fb92c417d481442d2641637483881',
                                                            '0xbc3a9090bbf303d7c49ff8f3a7e9f48679b42d00'))
        self.assertEqual((m['vault001'], m['testVault']), ('0x36f3d19dda7ed1428e3014ae6f2a75d70393b7e6',
                                                           '0xc775dfaa7565f2aca5ddf5dd6837ceb846de591b'))
        self.assertEqual((m['safeThreshold'], len(m['safeOwners']), m['multiSendCallOnly']), (3, 5, MULTISEND))
        self.assertEqual(t['state'], None)
        self.assertEqual((t['binding']['forkBlock'], t['binding']['provenance']['authenticated']), (20_582_384, False))
        self.assertGreater(len(t['history']), 100)
        synthetic = set(rc.actor_set('mainnet').values())
        flat = {v for v in m.values() if isinstance(v, str)} | {x for v in m.values() if isinstance(v, list) for x in v}
        self.assertFalse(synthetic & flat, 'a labelled synthetic actor entered the real topology')

    def test_testnet_topology_carries_the_owner_confirmed_reporter_and_the_pending_keeper(self):
        t = self.topo('testnet')
        m = t['meta']
        self.assertEqual(len(m['vaults']), 15)
        self.assertEqual(m['vaults'][14], '0x133475720bebd7e0335a9af8e5118ed0b19815e2')
        self.assertEqual(m['expectedReporter'], '0xe45493245c4bc3a573d35b191d8674f861e1ef45')
        self.assertEqual((m['keeper'], m['keeperStanding'], m['keeperCheck']),
                         ('0x2fa913f7637fb691ce3bf6d2bae8502bd2b1914b', 'OWNER_DECISION_PENDING', 'MATCH'))
        self.assertEqual(m['distributor'], '0x008a7856fecf9a73f5011f77b020877f5d74a7f2')
        self.assertEqual(m['profitFunder'], m['e2'])
        self.assertEqual((m['destinations'], m['destinationsStanding']),
                         (['0x464017cdc3c2af2b5b525fde03ac93f15172db43', '0x4a25cbf7bfa5383ff8490c44859097203442ae05',
                           '0xb79db65038a11fa8f5a361e5ff265842b3619ddc'], 'OWNER_DECISION_PENDING'))
        # the topology history carries what the owner-config gate binds (block hash, tx, log index)
        import owner_config as oc
        anchor = {'block': t['binding']['forkBlock'], 'hash': t['binding']['forkBlockHash']}
        observed = {('oracle', 'slp'): m['expectedReporter'], ('stpros', 'slp'): m['distributor'],
                    ('slpDistributor', 'keeper'): m['keeper']}
        self.assertEqual([c['result'] for c in oc.check('testnet', anchor, t['history'], observed)], ['MATCH'] * 3)

    def test_a_foreign_anchor_or_missing_safe_identity_is_refused(self):
        auth = self.bound('mainnet')
        for name, edit in (('block', {'forkBlock': auth['forkBlock'] + 1}), ('hash', {'forkBlockHash': '0x' + '11' * 32}),
                           ('spec', {'anchorBinding': {**auth['anchorBinding'], 'specSha256': '0' * 64}}),
                           ('chain', {'chain': 'testnet'})):
            with self.subTest(name), self.assertRaises(ExecutionError):
                self.topo('mainnet', auth={**auth, **edit}, multisend_call_only=MULTISEND)
        with self.assertRaises(ExecutionError):
            self.topo('mainnet', auth=auth)
        with self.assertRaises(ExecutionError):
            self.topo('testnet', auth=auth)                           # the mainnet binding for a testnet topology


if __name__ == '__main__':
    unittest.main()
