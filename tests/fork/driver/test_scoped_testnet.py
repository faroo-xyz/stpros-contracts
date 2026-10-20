"""Testnet Scoped Upgrade (StPROS V2 -> V4 + BridgeVault V1 -> V4). Test-only, synthetic loopback.

Separate from the full testnet V4 release flow (``test_execution`` FS-2): the scope label, ledger and evidence are
``SCOPED-T`` only.
"""
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-testnet-v3.json'
READY = CANONICAL_READY and TOPOLOGY.is_file()


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the CampaignAnvilFixture topologies in this export')
class ScopedTestnetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import scoped_testnet
        cls.st = scoped_testnet
        cls.r = scoped_testnet.run_scoped('scoped', name='test-scoped-testnet')
        cls.v = {c['id']: c for c in cls.r['checks']}

    def test_every_check_passes(self):
        bad = {k: (c['expected'][:80], c['actual'][:80], c.get('detail', '')[:120]) for k, c in self.v.items()
               if c['verdict'] not in ('PASS',)}
        self.assertEqual(bad, {})
        self.assertEqual(self.r['ledger']['aggregate'], 'PASS')
        self.assertEqual(self.r['ledger']['verdict'], 'INCOMPLETE')          # synthetic: never FS evidence

    def test_scope_is_separate_from_the_full_flow(self):
        self.assertTrue(all(k.startswith(('SCOPED-T.', 'tx.SCOPED-T.', 'reject.SCOPED-T.')) for k in self.v))
        self.assertEqual(self.r['ledger']['scenario'], 'SCOPED-T')
        upgraded = [t['label'] for t in self.r['transcript'] if t['label'].startswith('SCOPED-T.upgrade.')]
        self.assertEqual(upgraded, ['SCOPED-T.upgrade.BridgeVault', 'SCOPED-T.upgrade.StPROS'])   # order, nothing else
        for forbidden in ('Oracle', 'SlpDistributor', 'YieldVault', 'Factory', 'Beacon', 'retire'):
            self.assertFalse(any(forbidden.lower() in t['label'].lower() and 'upgrade' in t['label'].lower()
                                 and 'stillUpgradeable' not in t['label'] for t in self.r['transcript']), forbidden)

    def test_required_rows_present(self):
        for rid in ('SCOPED-T.post.stpros.implementation==V4', 'SCOPED-T.post.bridge.implementation==V4',
                    'SCOPED-T.post.oracle.implementation.unchanged', 'SCOPED-T.post.distributor.implementation.unchanged',
                    'SCOPED-T.post.factory.implementation.unchanged', 'SCOPED-T.post.beacon.implementation.unchanged',
                    'SCOPED-T.post.proxyAdmin.owner.unchanged(E1)', 'SCOPED-T.post.stpros.holderBalances',
                    'SCOPED-T.post.stpros.withdrawalRecords', 'SCOPED-T.post.I-4.sumPending==Q-C',
                    'tx.SCOPED-T.factory.stillUpgradeableByE1', 'SCOPED-T.between.stprosV2.claim.payout==model',
                    'SCOPED-T.user.depositWithPROS.shares', 'SCOPED-T.user.wrapped.sink',
                    'SCOPED-T.claim.withdrawComplete().payout==model', 'SCOPED-T.claim.withdrawCompleteToPROS(uint256).payout==model',
                    'SCOPED-T.oracle.update.I-1.supply==poolV', 'SCOPED-T.unpaused', 'SCOPED-T.vault.redeem.stPROSReachedHolder'):
            self.assertIn(rid, self.v)
        self.assertTrue(any(k.startswith('SCOPED-T.fifo.partial.') for k in self.v))
        for i in range(14):
            self.assertIn(f'SCOPED-T.post.vault{i}.stPROS.unchanged', self.v)


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the CampaignAnvilFixture topologies in this export')
class StprosOnlyNegativeTest(unittest.TestCase):
    """The dependency: StPROS V4 over BridgeVault V1 cannot pay a wrapped claim."""

    @classmethod
    def setUpClass(cls):
        import scoped_testnet
        cls.r = scoped_testnet.run_scoped('stpros-only', name='test-scoped-testnet-stpros-only')
        cls.v = {c['id']: c for c in cls.r['checks']}

    def test_wrapped_claim_reverts_native_claim_pays(self):
        bad = {k: c.get('detail', '') for k, c in self.v.items() if c['verdict'] != 'PASS'}
        self.assertEqual(bad, {})
        for rid in ('SCOPED-T.neg.bridge.stillV1', 'reject.SCOPED-T.neg.stprosOnly.wrappedClaim.reverts',
                    'SCOPED-T.neg.stprosOnly.nativeClaim.paid'):
            self.assertIn(rid, self.v)


if __name__ == '__main__':
    unittest.main()
