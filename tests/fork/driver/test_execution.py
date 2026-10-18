"""Tests of the offline Fork-execution driver, its models, the FS-6 procedure, the S-012 scan, the release
inventory adapter, the real-state Fork boundary and the 80-scenario coverage.

Run inside an approved export, after ``tools/ci/canonical.py`` and the fixture export:
  tools/verify/run.sh "$dest" python3 tools/ci/canonical.py
  tools/verify/run.sh "$dest" forge test --offline --match-path 'tests/fork/{ExecutionTopology,ForwardFixWitness}.t.sol' --match-test test_exportExecution
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_execution.py' -v

Offline unit tests run anywhere. Loopback tests (recorded legacy runtimes on a local Anvil node, zero accounts, no
fork URL, no network) are skipped with an explicit NOT RUN reason outside such an export. Every result here is
SYNTHETIC (class C); no ledger may carry a final PASS.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collector as col  # noqa: E402
import coverage  # noqa: E402
import fork_mode as fm  # noqa: E402
import gasgate  # noqa: E402
import history_inventory as hi  # noqa: E402
import models  # noqa: E402
import oracles  # noqa: E402
import preconditions  # noqa: E402
import release_config as rc  # noqa: E402
import release_inventory as ri  # noqa: E402
import scenarios  # noqa: E402
import transport as tp  # noqa: E402
from transport import BudgetExhausted, CollectionStop  # noqa: E402

GAS_BLOCKED = ('bindingEstimator', 'deploymentGasGate', 'testnetCapResponse')
# the reviewed forward-fix build does not exist: FS-4 R3 reports it BLOCKED next to its test-only mechanism witness
REVIEW_BLOCKED = ('.R3.reviewedForwardFixBuild',)


def word(a):
    return '0x' + a[2:].rjust(64, '0')


def amount(v):
    return '0x' + format(v, '064x')


def log(address, topic, topics=(), data='0x', block=1, index=0):
    return {'address': address, 'topics': [rc.TOPICS[topic], *topics], 'data': data, 'blockNumber': hex(block),
            'logIndex': hex(index), 'transactionHash': '0x' + format(block * 1000 + index, '064x')}


def loopback_ready():
    if shutil.which('anvil') is None:
        return 'NOT RUN: anvil is not available in this environment'
    if not (ROOT / 'EXPORT-MANIFEST.txt').is_file():
        return 'NOT RUN: Foundry binaries run only inside an approved export'
    if not (ROOT / 'cache/native-r4/TimelockController/artifact.json').is_file():
        return 'NOT RUN: run tools/ci/canonical.py in this export first'
    for name in ('execution-mainnet-v1', 'execution-testnet-v3'):
        if not (rc.EXEC / f'{name}-state.json').is_file():
            return 'NOT RUN: run tests/fork/ExecutionTopology.t.sol (test_exportExecution*) in this export first'
    if not (rc.EXEC / 'forward-fix-witness.json').is_file():
        return 'NOT RUN: run tests/fork/ForwardFixWitness.t.sol (test_exportExecution*) in this export first'
    return None


RECOVERY_IDS = ('{g}.8.native.A.id==independent', '{g}.8.native.refuses.amountAboveCap', '{g}.8.native.refuses.alreadyScheduled',
                '{g}.8.A.notConsumedBeforeMaturity', '{g}.8.A.reconcile.detectsUnusedPending', '{g}.8.A.reconciled',
                '{g}.8.B.reconciled', '{g}.8.B.guardianReplaced', '{g}.8.cutoff.exactAccepted', '{g}.8.cutoff.pastNotConsumed',
                '{g}.8.opsIsGrd.reconciled', '{g}.8.A.final.tma', '{g}.8.residual.unusedBReplacedGuardian',
                '{g}.4.staleLoss.residual.newDepositorBearsStaleLoss', '{g}.4.staleLoss.control.reconciled',
                '{d}.R4.F.preserved', '{d}.R4.fundedClaimsPreserved', '{d}.R4.recordsUnchanged',
                '{d}.R4.gate.reviewedBundleAccepted', '{d}.R4.gate.identitiesAccepted', '{d}.R4.gate.canonicalPassed',
                '{d}.R4.replace.executedReviewedId', '{d}.R4.partial.gatePassed',
                '{d}.R4.gate.wrongRecipient.rejected.CUSTODY_RECIPIENT', '{d}.R4.gate.wrongRecipient.noTransactionSent',
                '{d}.R4.gate.wrongAmount.rejected.CUSTODY_AMOUNT', '{d}.R4.gate.wrongAsset.rejected.CUSTODY_ASSET',
                '{d}.R4.gate.swappedOrder.rejected.CUSTODY_ORDER', '{d}.R4.gate.extraReRegistration.rejected.CUSTODY_ITEM_COUNT',
                '{d}.R4.gate.withdrawOnly.rejected.CUSTODY_ITEM_COUNT', '{d}.R4.gate.repointElsewhere.rejected.CUSTODY_REPOINT_VALUE',
                '{d}.R4.gate.wrongWithdrawTarget.rejected.CUSTODY_WITHDRAW_TARGET',
                '{d}.R4.gate.excessAmount.rejected.CUSTODY_BALANCE', '{d}.R4.gate.rogueNewVault.rejected.CUSTODY_NEW_OWNER',
                '{d}.R4.gate.tampered.rejected.CUSTODY_OPERATION_NOT_PENDING',
                '{d}.R4.gate.drift.rejected.CUSTODY_AMOUNT_NOT_FULL', '{d}.R4.gate.drift.notExecuted',
                '{d}.R4.pre.observedPointer==reviewedOldBridge',
                '{d}.R4.gate.stprosPointerDrift.rejected.CUSTODY_STPROS_POINTER', '{d}.R4.gate.stprosPointerDrift.noTransactionSent',
                '{d}.R4.gate.oldImplementationDrift.rejected.CUSTODY_OLD_IMPLEMENTATION',
                '{d}.R4.gate.oldImplementationDrift.noTransactionSent',
                '{d}.R4.gate.oldAdminDrift.rejected.CUSTODY_OLD_ADMIN', '{d}.R4.gate.oldAdminDrift.noTransactionSent',
                '{d}.R4.gate.oldRuntimeChanged.rejected.CUSTODY_OLD_RUNTIME', '{d}.R4.gate.oldRuntimeChanged.noTransactionSent',
                '{d}.R4.gate.oldRuntimeUnknown.rejected.CUSTODY_OLD_RUNTIME', '{d}.R4.gate.oldRuntimeUnknown.noTransactionSent',
                '{d}.R4.gate.stprosImplementationDrift.gettersStillExpected',
                '{d}.R4.gate.stprosImplementationDrift.rejected.CUSTODY_STPROS_IMPLEMENTATION',
                '{d}.R4.gate.stprosImplementationDrift.noTransactionSent',
                '{d}.R4.gate.unknownProfile.rejected.CUSTODY_UNKNOWN_PROFILE', '{d}.R4.gate.unknownProfile.noTransactionSent',
                '{d}.R4.gate.inconsistentPlan.rejected.CUSTODY_PLAN_INCONSISTENT',
                '{d}.R4.gate.releaseModePlan.rejected.CUSTODY_RELEASE_PLAN_UNAPPROVED',
                '{d}.R4.gate.driftAfterSchedule.rejected.CUSTODY_STPROS_IMPLEMENTATION',
                '{d}.R4.gate.driftAfterSchedule.notExecuted', '{d}.R4.gate.driftAfterSchedule.custodyUntouched',
                '{d}.R4.ungated.wrongRecipient.executesOnChain.postReconciliationDetects', '{d}.R4.ungated.excess.notRepointed',
                '{d}.R4.ungated.reRegister.atomicRollback',
                '{d}.R3.statePreserved', '{d}.R3.rollback.statePreserved', '{d}.R3.vault.markerViaEveryLiveVault')


def _stages(result):
    """The stage records of a finished run, from its written run document."""
    return json.loads((rc.EXEC / 'runs' / result['run'] / 'run.json').read_text())['stages']


def _require_ids(testcase, result, ids, gov, drill):
    """Each named check exists exactly once and is PASS (a drill that silently stops early fails here)."""
    for template in ids:
        sid = template.format(g=gov, d=drill)
        got = [c['verdict'] for c in result['checks'] if c['id'] == sid]
        testcase.assertEqual(got, ['PASS'], sid)


def nonpass(result):
    return {(c['verdict'], c['id']) for c in result['checks'] if c['verdict'] != 'PASS'}


def only_gas_blocked(testcase, result, allowed_fail=()):
    unexpected = {(v, i) for v, i in nonpass(result)
                  if not (v == 'BLOCKED' and i.endswith(GAS_BLOCKED)) and i not in allowed_fail
                  and not (v == 'BLOCKED' and i.startswith('FS-4') and i.endswith(REVIEW_BLOCKED))}
    testcase.assertEqual(unexpected, set(), sorted(unexpected)[:10])
    for ledger in result['ledgers'].values():
        scenarios.require_ledger(ledger)
        testcase.assertNotEqual(ledger['verdict'], 'PASS', ledger['scenario'])


# ================================================================================================ offline
class QueueModelTest(unittest.TestCase):
    """FIFO cumAvail model (FS-5, AL-06, F-07) with its wrong-rule controls."""
    ST, A, B = '0x' + '11' * 20, '0x' + 'aa' * 20, '0x' + 'bb' * 20

    def rows(self):
        def withdraw(owner, assets, i):
            return log(self.ST, 'Withdraw', [word(owner), word(owner), word(owner)], amount(assets) + amount(assets)[2:], 1, i)
        return [withdraw(self.A, 10, 0), withdraw(self.B, 5, 1), withdraw(self.A, 7, 2),
                log(self.ST, 'WithdrawalCompleted', [word(self.A), word(self.A)], amount(4), 2, 0)]

    def test_baselines_partial_consumption_and_prediction(self):
        q = models.QueueModel(self.rows(), self.ST)
        self.assertEqual([(r['baseline'], r['pending']) for r in q.records[self.A]], [(4, 6), (15, 7)])
        self.assertEqual(q.pending(), 18)
        # custody covers up to 12: A's first record releases 12 - 4 = 8 capped at its pending 6; next needs > 15
        self.assertEqual(q.predict(self.A, 12, [True, True]), [6])
        self.assertEqual(q.predict(self.A, 30, [True, True]), [6, 7])
        self.assertEqual(q.predict(self.A, 30, [True, False]), [6])
        self.assertEqual(q.predict(self.A, 30, [True, True], max_records=1), [6])
        self.assertEqual(q.predict(self.B, 12, [True]), [2])            # partial: 12 - baseline 10

    def test_wrong_rules_disagree(self):
        right = models.QueueModel(self.rows(), self.ST)
        no_bump = models.QueueModel(self.rows(), self.ST, 'WRONG_NO_PARTIAL_BUMP')
        no_base = models.QueueModel(self.rows(), self.ST, 'WRONG_IGNORE_BASELINE')
        self.assertNotEqual(no_bump.predict(self.A, 8, [True]), right.predict(self.A, 8, [True]))
        self.assertNotEqual(no_base.predict(self.B, 12, [True]), right.predict(self.B, 12, [True]))
        with self.assertRaises(ValueError):
            models.QueueModel(self.rows(), self.ST, 'GUESS')

    def test_completion_beyond_queue_refused(self):
        rows = [*self.rows(), log(self.ST, 'WithdrawalCompleted', [word(self.B), word(self.B)], amount(99), 3, 0)]
        with self.assertRaises(ValueError):
            models.QueueModel(rows, self.ST)


class VaultAndAuthorityModelTest(unittest.TestCase):
    def test_retirement_rule(self):
        f = '0x' + 'cc' * 20
        self.assertEqual(models.retirement(2, 0, 7, True, f), (3, 7, f))       # funded PROFIT: exact refund
        self.assertEqual(models.retirement(2, 0, 7, False, None), (3, 0, None))  # unfunded: nothing moves
        self.assertEqual(models.retirement(2, 0, 7, False, f), (3, 0, None))     # unfunded, proposer known: nothing
        self.assertEqual(models.retirement(2, 1, 7, False, None), (3, 0, None))  # LOSS proposal: nothing moves
        self.assertEqual(models.retirement(1, 0, 0, False, None), (3, 0, None))  # LOCKED -> SETTLED at par

    def test_role_sets(self):
        tl, ops, gov, can = ('0x' + c * 40 for c in '1234')
        roles = {'proposer': '0xp', 'executor': '0xe', 'canceller': '0xc'}
        self.assertEqual(models.timelock_roles(tl, gov, can, roles),
                         {f'{models.ADMIN}:{tl}', f'0xp:{gov}', f'0xe:{gov}', f'0xc:{gov}', f'0xc:{can}'})
        self.assertEqual(models.vault_roles('H', 'GOVERNED', tl, ops, '0xo'), {f'{models.ADMIN}:{tl}'})
        self.assertEqual(models.vault_roles('U', 'GOVERNED', tl, ops, '0xo'), {f'{models.ADMIN}:{tl}', f'0xo:{ops}'})
        self.assertEqual(models.vault_roles('U', 'RETIRED', tl, ops, '0xo', {'x'}), {'x'})

    def test_custody_wrong_rules_fail_with_positive_donation(self):
        pre = dict(bridgeNative=11, slpNative=0, stNative=7, wprosAtSt=101, reserve=30, queued=50, completed=20,
                   supply=900, escrow=30, poolT=0, poolV=0)
        post = dict(bridgeNative=41, slpNative=71, stNative=7, wprosAtSt=0, reserve=0, queued=50, completed=20,
                    supply=870, escrow=0, poolT=870, poolV=870)
        self.assertTrue(all(c['verdict'] == 'PASS' for c in oracles.u_frame(pre, post, 30)))
        for rule in oracles.RULES[1:]:
            self.assertTrue(any(c['verdict'] == 'FAIL' for c in oracles.u_frame(pre, post, 30, rule)), rule)


class RecoveryModelsTest(unittest.TestCase):
    """The FS-3.4 / 3.8 / FS-4 R4 expectations, each with a wrong rule that must disagree."""

    def test_loss_cap_and_finalization(self):
        tma, bps = 100 * 10 ** 18 + 7, 2000
        cap = tma * bps // 10000
        self.assertEqual(models.loss_settlement(tma, cap, bps), {'tma': tma - cap, 'counterparty': cap})
        self.assertIsNone(models.loss_settlement(tma, cap + 1, bps))                           # LossExceedManaged
        self.assertIsNotNone(models.loss_settlement(tma, cap + 1, bps, 'WRONG_CAP_INCLUSIVE_PLUS_ONE'))
        self.assertEqual(models.redeem_value(10 ** 24, 10 ** 18, 10 ** 24), (10 ** 18 + 1) * 10 ** 24 // (10 ** 24 + 10 ** 6))

    def test_claim_is_all_or_nothing_against_custody(self):
        self.assertEqual(models.claim_outcome([4, 6], 10), [4, 6])
        self.assertIsNone(models.claim_outcome([4, 6], 9))                                    # docs/RECOVERY.md
        self.assertEqual(models.claim_outcome([4, 6], 9, 'WRONG_PARTIAL_PAYOUT'), [9])
        self.assertEqual(models.custody_frontier(5, 10), 15)
        st = '0x' + 'aa' * 20
        rows = [log(st, 'WithdrawalCompleted', [word('0x' + '01' * 20), word('0x' + '02' * 20)], amount(3)),
                log(st, 'WithdrawalCompleted', [word('0x' + '01' * 20), word('0x' + '02' * 20)], amount(4), index=1),
                log('0x' + 'bb' * 20, 'WithdrawalCompleted', [word('0x' + '01' * 20), word('0x' + '02' * 20)], amount(9), index=2)]
        self.assertEqual(models.completed_from_history(rows, st), 7)

    def test_state_comparison_reports_every_differing_slot(self):
        import xf_witness
        a = {('0x1', '0x0'): '0x01', ('0x1', '0x1'): 'BLOCK_TIMESTAMP'}
        self.assertEqual(xf_witness.compare(a, dict(a)), [])
        b = {**a, ('0x1', '0x0'): '0x02', ('0x2', '0x0'): '0x03'}
        self.assertEqual([(d['address'], d['slot']) for d in xf_witness.compare(a, b)], [('0x1', '0x0'), ('0x2', '0x0')])
        self.assertEqual(xf_witness.unknown_callees([{'to': '0x' + '00' * 19 + '01'}, {'to': '0xabc'}], {'0xABC'}), [])
        self.assertEqual(xf_witness.unknown_callees([{'to': '0xdef'}], set()), ['0xdef'])


class CustodyBundleCheckTest(unittest.TestCase):
    """The pure pre-execution bundle check of the R4 custody replacement (no chain)."""

    def setUp(self):
        import recovery_drills as rd
        self.rd = rd
        self.old, self.new, self.st = '0x' + '0a' * 20, '0x' + '0b' * 20, '0x' + '0c' * 20
        self.amount = 7 * 10 ** 18
        calls = [(self.old, rd.abi.calldata('emergencyWithdraw(address,address,uint256)', rd.ZERO, self.new, self.amount)),
                 (self.st, rd.abi.calldata('setBridgeVault(address)', self.new))]
        salt = rd.salt('unit')
        self.plan = {'oldBridge': self.old, 'newBridge': self.new, 'stpros': self.st, 'recipient': self.new,
                     'amount': self.amount, 'calls': calls, 'salt': salt, 'operationId': rd.batch_id(calls, salt)}

    def ew(self, token, to, amount, target=None):
        return (target or self.old, self.rd.abi.calldata('emergencyWithdraw(address,address,uint256)', token, to, amount))

    def test_reviewed_bundle_passes_and_every_deviation_is_named(self):
        rd, plan, calls = self.rd, self.plan, self.plan['calls']
        self.assertEqual(rd.check_bundle(plan, calls), [])
        wrong = '0x' + 'ee' * 20
        cases = {
            'CUSTODY_RECIPIENT': [self.ew(rd.ZERO, wrong, self.amount), calls[1]],
            'CUSTODY_AMOUNT': [self.ew(rd.ZERO, self.new, self.amount + 1), calls[1]],
            'CUSTODY_ASSET': [self.ew(wrong, self.new, self.amount), calls[1]],
            'CUSTODY_WITHDRAW_TARGET': [self.ew(rd.ZERO, self.new, self.amount, target=wrong), calls[1]],
            'CUSTODY_ORDER': [calls[1], calls[0]],
            'CUSTODY_ITEM_COUNT': calls[:1],
            'CUSTODY_UNEXPECTED_CALL': [calls[0], (self.st, rd.abi.calldata('pause()'))],
            'CUSTODY_REPOINT_TARGET': [calls[0], (wrong, calls[1][1])],
            'CUSTODY_REPOINT_VALUE': [calls[0], (self.st, rd.abi.calldata('setBridgeVault(address)', wrong))],
            'CUSTODY_CALLDATA_LENGTH': [(self.old, calls[0][1] + '00' * 32), calls[1]],
        }
        for code, bundle in cases.items():
            problems = rd.check_bundle(plan, bundle)
            self.assertTrue(any(p.startswith(code) for p in problems), (code, problems))
        # an exact bundle under a different reviewed salt is a different operation
        self.assertIn('CUSTODY_OPERATION_ID', rd.check_bundle({**plan, 'salt': rd.salt('other')}, calls))

    def test_plan_consistency_is_fail_closed(self):
        rd = self.rd
        profile = {'proxyRuntime': '0x' + '11' * 32, 'implementation': '0x' + '12' * 20, 'artifact': 'BridgeVault',
                   'admin': '0x' + '13' * 20, 'version': 4}
        tl = '0x' + '0d' * 20
        plan = {**self.plan, 'schema': rd.CUSTODY_SCHEMA, 'mode': 'simulation', 'topologySource': rd.SYNTHETIC_TOPOLOGY,
                'timelock': tl, 'reviewedTimelock': tl, 'asset': '0x' + '0e' * 20,
                'profiles': {'oldBridge': 'p', 'newBridge': 'p', 'stpros': 'p'}, 'approvedProfiles': {'p': profile}}
        self.assertEqual(rd.check_plan(plan), [])
        cases = {'CUSTODY_RELEASE_PLAN_UNAPPROVED': {'mode': 'release'},
                 'CUSTODY_PLAN_INCONSISTENT': {'recipient': '0x' + 'ee' * 20},
                 'CUSTODY_PLAN_ALIAS': {'newBridge': self.old},
                 'CUSTODY_UNKNOWN_PROFILE': {'profiles': {'oldBridge': 'unknown', 'newBridge': 'p', 'stpros': 'p'}},
                 'CUSTODY_PLAN_CALLS': {'amount': self.amount + 1},
                 'CUSTODY_PLAN_OPERATION_ID': {'operationId': '0x' + '00' * 32},
                 'CUSTODY_PLAN_SCHEMA': {'schema': 'other'}}
        for code, change in cases.items():
            problems = rd.check_plan({**plan, **change})
            self.assertTrue(any(p.startswith(code) for p in problems), (code, problems))
        # a timelock that differs from the reviewed P-0 Timelock is inconsistent
        self.assertTrue(any(p.startswith('CUSTODY_PLAN_INCONSISTENT') for p in rd.check_plan({**plan, 'reviewedTimelock': self.old})))


class GasGateTest(unittest.TestCase):
    def test_calibration_needs_three_receipts(self):
        self.assertEqual(gasgate.calibration_k([(100, 90), (110, 100)])['status'], 'INCOMPLETE')
        k = gasgate.calibration_k([(100, 90), (110, 100), (99, 99)])
        self.assertAlmostEqual(k['k'], 100 / 90)
        with self.assertRaises(gasgate.GasGateError):
            gasgate.calibration_k([(1, 0), (1, 1), (1, 1)])

    def test_options_and_fail_closed_gate(self):
        rec = {'estimate': 2_000_000, 'gasUsed': 1_900_000}
        self.assertIsNone(gasgate.FR_D2_DECISION)
        self.assertEqual(gasgate.gate(rec)['verdict'], 'BLOCKED')
        self.assertEqual(gasgate.gate(rec, kind='P-0')['verdict'], 'BLOCKED')
        self.assertEqual(gasgate.gate(rec, decision='a')['verdict'], 'PASS')
        self.assertEqual(gasgate.gate(rec, decision='c')['verdict'], 'FAIL')          # 1.32 x 2.0M > 2.2M
        self.assertEqual(gasgate.gate(rec, decision='b')['verdict'], 'INCOMPLETE')    # no k
        self.assertEqual(gasgate.gate(rec, decision='b', k=1.2)['verdict'], 'FAIL')
        with self.assertRaises(gasgate.GasGateError):
            gasgate.gate(rec, decision='z')

    def test_trial_and_ecdsa_correction(self):
        self.assertTrue(gasgate.trial_ok({'estimate': 100, 'gasLimit': 150, 'gasLimitRule': '1.5x estimate', 'status': 1}))
        self.assertFalse(gasgate.trial_ok({'estimate': 100, 'gasLimit': 30_000_000, 'gasLimitRule': 'local ceiling', 'status': 1}))
        self.assertEqual(gasgate.ecdsa_correction(3)['delta'], 3 * 3000 - 2 * 2100)


class PreconditionsTest(unittest.TestCase):
    def test_overflow_bounds_and_force_par(self):
        ok = preconditions.scan_terms('v', {'openDeadline': '100', 'lockDuration': '50', 'openWindow': '10',
                                            'maxLossBps': '0', 'openedAt': '90'}, 1, False, 100 + 50 + preconditions.GRACE)
        self.assertEqual((ok['status'], ok['overflow'], ok['forceParEligibleAfterU']), ('PASS', False, True))
        big = preconditions.scan_terms('v', {'openDeadline': str(2 ** 255), 'lockDuration': str(2 ** 255), 'openWindow': '1',
                                             'maxLossBps': '0', 'openedAt': '0'}, 1, False, 0)
        self.assertEqual((big['status'], big['overflow']), ('FAIL', True))
        drift = preconditions.scan_terms('v', {'openDeadline': '1', 'lockDuration': str(400 * 86400), 'openWindow': '1',
                                               'maxLossBps': '20000', 'openedAt': '0'}, 2, True, 0)
        self.assertEqual((drift['status'], drift['outOfBound'], drift['forceParEligibleAfterU']),
                         ('DRIFT', ['lockDuration', 'maxLossBps'], False))
        self.assertEqual(preconditions.scan_terms('v', {'openDeadline': '1'}, 1, False, 0)['status'], 'INCOMPLETE')
        doc = {'block': 5, 'blockTimestamp': 10, 'vaults': {'v1': {'profile': 'UNKNOWN_PROFILE'}}}
        self.assertEqual(preconditions.scan_state(doc)['status'], 'INCOMPLETE')


class ForkBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def auth(self, **kw):
        doc = {'schema': fm.SCHEMA, 'authorizationId': 'fork-test', 'mode': 'SIMULATION', 'chain': 'mainnet',
               'endpoint': 'http://127.0.0.1:9/', 'methods': list(fm.FORK_METHODS), 'forkBlock': 10,
               'forkBlockHash': '0x' + 'ab' * 32, 'maxHttpPosts': 3, 'maxRestarts': 1, 'pacingMs': 350,
               'retention': 'test', 'statement': 'SIMULATION ONLY test'}
        doc.update(kw)
        path = self.root / f'auth-{len(list(self.root.iterdir()))}.json'
        path.write_text(json.dumps(doc))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def refused(self, code, **kw):
        path, sha = self.auth(**kw)
        with self.assertRaises(CollectionStop) as ctx:
            fm.load_authorization(path, sha)
        self.assertEqual(ctx.exception.code, code)

    def test_authorization_is_fail_closed(self):
        self.assertEqual(fm.APPROVED_FORK_AUTHORIZATIONS, frozenset({
            '3dd3ab07248c6e74d156d93978d00ea63c5d2cd6767d867f53f610ae560fd96c',  # fr-d1-s3-testnet-sl20261018
            '19442bc39ee398f7e9daa9bc71818bf78019ee196ed239cb07bd05e37a77599f',  # fr-d1-s3-mainnet-sl20261018
        }))
        from tools.lib.rpc import PUBLIC_RPC
        self.refused('FR_D1_NOT_AUTHORIZED', mode='LOCAL_FORK_PUBLIC_READ_ONLY', endpoint=PUBLIC_RPC['mainnet'])
        self.refused('FORK_ENDPOINT', mode='LOCAL_FORK_PUBLIC_READ_ONLY', endpoint=PUBLIC_RPC['testnet'])
        self.refused('AUTHORIZATION_KEYS', methods=['eth_sendRawTransaction'])
        self.refused('AUTHORIZATION_KEYS', pacingMs=10)
        self.refused('AUTHORIZATION_KEYS', statement='a fork')
        path, sha = self.auth(endpoint=PUBLIC_RPC['mainnet'])          # simulation over a public endpoint
        with self.assertRaises(Exception):
            fm.load_authorization(path, sha)
        path, sha = self.auth()
        with self.assertRaises(CollectionStop) as ctx:
            fm.load_authorization(path, '0' * 64)
        self.assertEqual(ctx.exception.code, 'AUTHORIZATION_HASH')
        # even with the authorization pinned, the proxy refuses a public upstream without the source constant
        path, sha = self.auth(mode='LOCAL_FORK_PUBLIC_READ_ONLY', endpoint=PUBLIC_RPC['mainnet'])
        auth = fm.load_authorization(path, sha, approved={sha})
        with self.assertRaises(CollectionStop) as ctx:
            fm.ForkBudgetProxy(auth, None)
        self.assertEqual(ctx.exception.code, 'FR_D1_NOT_AUTHORIZED')

    def test_export_root_rule(self):
        export = self.root / 'export'
        area = export / 'cache_forge/rehearsal/fork-campaign/collection'
        area.mkdir(parents=True)
        (export / 'EXPORT-MANIFEST.txt').write_text('{}')
        self.assertEqual(fm.require_export_root(area / 'run-1', 'collection'), export)
        for path, code in ((self.root / 'elsewhere', 'EXPORT_ROOT'), (export / 'cache_forge/other', 'EXPORT_ROOT'),
                           (Path('relative/path'), 'EXPORT_ROOT')):
            with self.assertRaises(CollectionStop) as ctx:
                fm.require_export_root(path, 'collection')
            self.assertEqual(ctx.exception.code, code)
        (export / 'cache_forge/rehearsal/fork-campaign/link').symlink_to(area)
        with self.assertRaises(CollectionStop) as ctx:
            fm.require_export_root(export / 'cache_forge/rehearsal/fork-campaign/link/x', 'collection')
        self.assertEqual(ctx.exception.code, 'EXPORT_ROOT_SYMLINK')
        (export / '.git').mkdir()
        with self.assertRaises(CollectionStop) as ctx:
            fm.require_export_root(area, 'collection')
        self.assertEqual(ctx.exception.code, 'EXPORT_ROOT')

    def test_proxy_charges_every_request_and_stops_at_the_ceiling(self):
        path, sha = self.auth(methods=['eth_chainId', 'eth_getCode'])
        auth = fm.load_authorization(path, sha)
        sent, sleeps = [], []
        forward = lambda url, raw, t: (sent.append(raw) or (200, b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'))  # noqa: E731
        ledger = tp.BudgetLedger(self.root / 'budget', auth, invocation='a').open()
        proxy = fm.ForkBudgetProxy(auth, ledger, forward=forward, clock_s=lambda: 0.0, sleep=sleeps.append)
        self.addCleanup(proxy.server.server_close)                    # handle() only: the server never starts
        call = lambda m: proxy.handle(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': []}).encode())  # noqa: E731
        self.assertEqual(call('eth_chainId')[0], 200)
        self.assertEqual(call('eth_sendRawTransaction')[0], 403)       # method outside the authorization: not charged
        self.assertEqual(call('eth_getCode')[0], 200)
        self.assertEqual(proxy.handle(json.dumps([{'jsonrpc': '2.0', 'id': i, 'method': 'eth_chainId', 'params': []}
                                                  for i in range(tp.GROUP_LIMIT + 1)]).encode())[0], 403)
        self.assertEqual(proxy.handle(b'not json')[0], 400)
        self.assertEqual(call('eth_chainId')[0], 200)
        self.assertEqual((ledger.used, len(sent)), (3, 3))
        self.assertTrue(sleeps)                                       # paced
        self.assertEqual(call('eth_chainId')[0], 429)                 # ceiling 3 reached: refused before sending
        self.assertEqual(len(sent), 3)
        self.assertIsInstance(proxy.stopped, BudgetExhausted)
        ledger.close()
        # the ceiling survives a new invocation of the same authorization
        with self.assertRaises(BudgetExhausted):
            tp.BudgetLedger(self.root / 'budget', auth, invocation='b').open()

    def test_public_collection_requires_the_export_root(self):
        """P1-D04: with an FR-D1 authorization pinned (test-only patch of the empty set), a run root outside an
        approved export is refused before anything is sent."""
        import test_collector as tc
        export = self.root / 'export'
        area = export / 'cache_forge/rehearsal/fork-campaign/collection'
        area.mkdir(parents=True)
        (export / 'EXPORT-MANIFEST.txt').write_text('{}')
        auth_path, auth_sha = tc.authorization(self.root, ident='public-test', mode='PUBLIC_READ_ONLY')
        # each of the two paths is checked on its own: the other one is inside the export area
        for name, world_root, budget, bad in (('history', self.root / 'w1', area / 'budget1', self.root / 'w1'),
                                              ('ledger', area / 'w2', self.root / 'budget2', self.root / 'budget2')):
            world = tc.World(world_root, 'testnet')
            with mock.patch.object(col, 'APPROVED_COLLECTION_AUTHORIZATIONS', frozenset({auth_sha})):
                auth = col.load_authorization(auth_path, auth_sha)
                ledger = tp.BudgetLedger(budget, auth, invocation='inv')
                gate = tp.HttpGate('testnet', ledger, approved={auth_sha})   # native transport, never reached
                with world.patch():
                    collector = world.collector(world.spec(), gate, tc.Clock())
                    with self.assertRaises(CollectionStop) as ctx:
                        collector.preflight()
            self.assertEqual(ctx.exception.code, 'EXPORT_ROOT', name)
            self.assertIn(str(bad.resolve()), ctx.exception.detail, name)
            self.assertFalse(gate.sent, name)


class CoverageTest(unittest.TestCase):
    def test_every_scenario_is_covered_and_none_passes(self):
        r = coverage.report()
        ids = [s['id'] for s in r['scenarios']]
        self.assertEqual(len(ids), 80)
        self.assertEqual(ids, scenarios.catalog_ids())
        self.assertEqual(r['realStateCounts'], {'NOT RUN': 80})
        keys = {'executor', 'caseConfiguration', 'assertions', 'negativeControls', 'rehearsal', 'realEvidenceRequired',
                'ownerDecisionsRequired', 'runnableOnceAuthorized', 'evidenceClass', 'realStateStatus'}
        for s in r['scenarios']:
            self.assertTrue(keys <= set(s), s['id'])
            self.assertNotIn('PASS', json.dumps({k: v for k, v in s.items() if k != 'rehearsalVerdict'}).replace('PASSes', ''))
            if s['chain'] == 'mainnet' and s['fs'] in ('FS-1', 'FS-1a', 'FS-7'):
                self.assertIn('FR-X8', s['ownerDecisionsRequired'], s['id'])
        self.assertEqual(scenarios.report()['counts']['NOT RUN'], 80)
        self.assertFalse(scenarios.FINAL_GATE_PROVENANCE_IMPLEMENTED)
        # closeout accounting: the executor status of each scenario, never relabelled from a ledger
        self.assertEqual(r['executorCounts'], {'IMPLEMENTED': 76, 'PARTIAL': 2, 'BLOCKED': 2})
        by_id = {s['id']: s for s in r['scenarios']}
        for sid in ('FS-4m.R3', 'FS-4t.R3'):
            self.assertTrue(by_id[sid]['executor'].startswith('PARTIAL'), sid)
        for sid in ('FS-1a.xi', 'FS-2.e'):
            self.assertTrue(by_id[sid]['executor'].startswith('BLOCKED'), sid)
        for s in r['scenarios']:
            for key in ('syntheticRunCompleted', 'recordedBytecodeSemantics', 'independentEvidenceRequired'):
                self.assertIn(key, s, s['id'])


class ReleaseInventoryAdapterTest(unittest.TestCase):
    """The accepted derivation and v1 observation of a synthetic P2 run -> the release inventory."""

    @classmethod
    def setUpClass(cls):
        import test_state_observation as tso
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name).resolve()
        cls.b = tso.build(root / 'mainnet', 'mainnet')
        w = cls.b['world']
        with w.patch():
            _, cls.verified, _ = col.content_verify(w.root, 'run-b', cls.b['spec'])
        cls.derived = hi.derive(cls.verified, 'mainnet')
        cls.v1 = json.loads((w.root / 'run-b/observation.json').read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def build(self, v1=None):
        return ri.build(self.verified, self.derived, v1 or self.v1, 'mainnet', '0x' + '7e' * 20)

    def test_sections_reconcile_and_the_new_timelock_is_incomplete(self):
        with self.assertRaises(ri.InventoryIncomplete) as ctx:
            self.build()
        doc = ctx.exception.document
        self.assertFalse(doc['complete'])
        self.assertIn('Timelock history not collected', str(ctx.exception))
        vaults = [v for vs in self.derived['factories'].values() for v in vs]
        self.assertEqual(len(self.derived['factories']), 2)                      # both mainnet factories
        for v in vaults:
            self.assertEqual(doc['roles'][v]['status'], 'PASS', doc['roles'][v]['mismatches'])
        for token, section in doc['holders'].items():
            self.assertEqual(section['status'], 'PASS', (token, section['mismatches']))
        self.assertEqual(doc['withdrawals']['status'], 'PASS')

    def test_disagreements_fail(self):
        v1 = copy.deepcopy(self.v1)
        token = next(iter(v1['tokens']))
        account = next(iter(v1['tokens'][token]['balanceOf']))
        v1['tokens'][token]['balanceOf'][account] = str(int(v1['tokens'][token]['balanceOf'][account]) + 1)
        with self.assertRaises(ri.InventoryIncomplete) as ctx:
            self.build(v1)
        self.assertEqual(ctx.exception.document['holders'][token]['status'], 'FAIL')
        v1 = copy.deepcopy(self.v1)
        vault = next(iter(v1['roles']))
        role = next(iter(v1['roles'][vault]))
        acct = next(iter(v1['roles'][vault][role]))
        v1['roles'][vault][role][acct] = not v1['roles'][vault][role][acct]
        with self.assertRaises(ri.InventoryIncomplete) as ctx:
            self.build(v1)
        self.assertEqual(ctx.exception.document['roles'][vault]['status'], 'FAIL')
        v1 = copy.deepcopy(self.v1)
        v1['withdrawals']['queued'] = str(int(v1['withdrawals']['queued']) + 1)
        with self.assertRaises(ri.InventoryIncomplete) as ctx:
            self.build(v1)
        self.assertEqual(ctx.exception.document['withdrawals']['status'], 'FAIL')
        v1 = copy.deepcopy(self.v1)
        v1['block'] -= 1
        with self.assertRaises(ri.InventoryIncomplete):
            self.build(v1)


class ScanOverObservationTest(unittest.TestCase):
    def test_s012_over_a_synthetic_state_observation(self):
        import test_state_observation as tso
        with tempfile.TemporaryDirectory() as d:
            b = tso.build(Path(d).resolve() / 'testnet', 'testnet')
            doc = json.loads((b['world'].root / 'run-b/state-observation.json').read_bytes())
        report = preconditions.scan_state(doc)
        self.assertEqual(len(report['vaults']), len(doc['vaults']))
        self.assertTrue(all(r['status'] in ('PASS', 'DRIFT') for r in report['vaults']), report)
        huge = copy.deepcopy(doc)
        v = next(iter(huge['vaults']))
        huge['vaults'][v]['terms']['lockDuration'] = str(2 ** 256 - 1)
        self.assertEqual(preconditions.scan_state(huge)['status'], 'FAIL')


# ================================================================================================ loopback
class Loopback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = loopback_ready()
        if reason:
            raise unittest.SkipTest(reason)
        import execution
        cls.ex = execution


class MainnetCasesTest(Loopback):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import governance
        cls.results = {c: cls.ex.mainnet(c, then=governance.drills if c == 'A' else None) for c in 'ABCD'}

    def test_cases_a_to_d(self):
        for case, r in self.results.items():
            only_gas_blocked(self, r)
            self.assertEqual(r['ledgers'][f'FS-1.{case}']['aggregate'], 'PASS', case)
            self.assertEqual(r['ledgers'][f'FS-1.{case}']['verdict'], 'INCOMPLETE')
            for stage in ('P-1', f'H.{case}', f'U.{case}'):
                self.assertEqual(r['ledgers'][f'FS-6m.{stage}']['verdict'], 'BLOCKED')
            ids = {c['id'] for c in r['checks']}
            for required in ('U.custody.C-5.M-1.wprosBefore==dBridge+dSlp', 'GC-03.afterU.doneNoPending', 'U.second',
                             'POST-8.testVault.retired', f'shape.U.{case}'):
                self.assertTrue(any(i.endswith(required) for i in ids), (case, required))
        a = self.results['A']
        self.assertEqual(a['ledgers']['FS-5m.permutations']['aggregate'], 'PASS')
        self.assertTrue(any(c['id'].startswith('FS-5.perm6') for c in a['checks']))      # forward, reverse, >= 5 seeded
        self.assertEqual({c['id'] for c in a['checks'] if c['id'].startswith('wait.claim.') and c['id'].endswith('modelPending')}.__len__(), 6)
        for sid in ('FS-3m.1', 'FS-3m.2', 'FS-3m.6', 'FS-4m.R0', 'FS-4m.R1', 'FS-4m.R2'):
            self.assertEqual(a['ledgers'][sid]['aggregate'], 'PASS', sid)
        for sid in ('FS-3m.4', 'FS-3m.8', 'FS-4m.R4'):
            self.assertEqual(a['ledgers'][sid]['aggregate'], 'PASS', sid)
        self.assertEqual(a['ledgers']['FS-4m.R3']['aggregate'], 'BLOCKED')                # reviewed fix build absent
        _require_ids(self, a, RECOVERY_IDS, 'FS-3m', 'FS-4m')
        self.assertTrue(any(c['id'] == 'H.authorityGate.outcomeAsExpected' and c['verdict'] == 'PASS' for c in a['checks']))
        residual = [c for c in self.results['C']['checks'] if c['id'] == 'FS-1.residual.exposure<=testVaultTVL']
        self.assertEqual([c['verdict'] for c in residual], ['PASS'])

    def test_deterministic_replay(self):
        again = self.ex.mainnet('B', name='mainnet-B-replay')
        strip = lambda r: [(t['label'], t['status'], t['gasUsed'], t['transactionHash']) for t in r['transcript']]  # noqa: E731
        self.assertEqual(strip(again), strip(self.results['B']))
        self.assertEqual([(c['id'], c['verdict'], c['actual']) for c in again['checks']],
                         [(c['id'], c['verdict'], c['actual']) for c in self.results['B']['checks']])


class AdverseVariantsTest(Loopback):
    def test_variants(self):
        # Observed, not tolerated: xi is BLOCKED by the fail-closed native preflight; viii (a) is a FINDING (the
        # beacon-before-factory reordering executes and assertR4 passes; only the off-chain shape check flags it).
        expected = {'xi': ('BLOCKED', {'FS-1a.xi.nativeToolingRefusesUnexpectedOwner'}),
                    'viii': ('FAIL', {'FS-1a.viii.beaconBeforeFactoryMigration.revertOrAssertR4Failure'})}
        for variant in ('i', 'ii', 'iii', 'iv', 'v', 'vi', 'viii', 'ix', 'ix-new', 'x', 'xi', 'xii'):
            r = self.ex.mainnet('A', variant=variant)
            aggregate, allowed = expected.get(variant, ('PASS', set()))
            only_gas_blocked(self, r, allowed)
            self.assertEqual({i for v, i in nonpass(r) if v in ('FAIL', 'BLOCKED') and i.startswith('FS-1a')}, allowed, variant)
            self.assertEqual(r['ledgers'][f'FS-1a.{variant}']['aggregate'], aggregate, variant)
            if variant == 'viii':
                for name in ('beaconBeforeFactoryMigration', 'factoryTestRetirementBeforeMigration'):
                    flagged = [c for c in r['checks'] if c['id'] == f'FS-1a.viii.{name}.offchainShapeCheckFlags']
                    self.assertEqual([c['verdict'] for c in flagged], ['PASS'], name)
                # XF-01 witness: only known callees, a non-vacuous comparison, identical end storage
                for suffix in ('noUnknownCallee', 'comparisonSeesTheMigration', 'sameWrittenState'):
                    got = [c['verdict'] for c in r['checks'] if c['id'] == f'FS-1a.viii.beaconBeforeFactoryMigration.xf01.{suffix}']
                    self.assertEqual(got, ['PASS'], suffix)
            if variant in ('ix', 'ix-new', 'vi'):
                got = [c['verdict'] for c in r['checks'] if c['id'] == f'FS-1a.{variant}.authorityGate.detectsDrift']
                self.assertEqual(got, ['PASS'], variant)
            if variant == 'xi':
                for suffix in ('exposure.wrongOwnerControlsB2', 'item0.ownerRestoredToTL'):
                    got = [c['verdict'] for c in r['checks'] if c['id'] == f'FS-1a.xi.witness.{suffix}']
                    self.assertEqual(got, ['PASS'], suffix)
                stage = next(s for s in _stages(r) if s['stage'] == 'FS-1a.xi.witness')
                self.assertIn('UNAPPROVED_INITIALIZER_VERSION', stage['nativeAfterItem0'])

    def test_alternative_a_is_recorded_not_a_release_path(self):
        r = self.ex.mainnet('A', variant='ALT-A')
        self.assertEqual(set(r['alternativeA']), {'literal', 'uFirst'})
        for name, outcome in r['alternativeA'].items():
            self.assertIn('executed', outcome)
            if not outcome['executed']:
                self.assertIsNotNone(outcome['firstFailingItem'], name)
        self.assertEqual(r['ledgers']['FS-1.ALT-A']['verdict'], 'INCOMPLETE')


class TestnetCasesTest(Loopback):
    def test_normal_and_recovery(self):
        import governance
        u_t_gas = 'FS-6.U_t.U_t.executeBatch.receipt<=2.2M'
        for case in self.ex.TESTNET_CASES:
            r = self.ex.testnet(case, then=governance.drills if case == 'normal' else None)
            allowed = {u_t_gas, 'FS-2.e.E1UnavailableBeforeT-4'}
            only_gas_blocked(self, r, allowed)
            self.assertEqual(r['ledgers'][f'FS-2.{case}']['aggregate'], 'BLOCKED' if case == 'e' else 'PASS', case)
            if case in ('normal', 'b', 'c', 'd'):
                # measured, not tolerated: the synthetic 15-vault U_t exceeds the frozen 2.2M cap (FR-A2 / FR-D3)
                gas = [c for c in r['checks'] if c['id'] == u_t_gas]
                self.assertEqual([c['verdict'] for c in gas], ['FAIL'])
            if case == 'normal':
                for sid in ('FS-5t.relations', 'FS-5t.partialClaims', 'FS-5t.vaults', 'FS-3t.1', 'FS-3t.6', 'FS-4t.R0',
                            'FS-3t.4', 'FS-3t.8', 'FS-4t.R4'):
                    self.assertEqual(r['ledgers'][sid]['aggregate'], 'PASS', sid)
                self.assertEqual(r['ledgers']['FS-4t.R3']['aggregate'], 'BLOCKED')
                _require_ids(self, r, RECOVERY_IDS, 'FS-3t', 'FS-4t')
                breakdown = next(s for s in _stages(r) if s['stage'] == 'U_t.gasBreakdown')
                self.assertEqual(len(breakdown['items']), 22)


class NegativeControlsTest(Loopback):
    """A limited set of controls that must turn a correct run into a FAIL or a refusal."""

    def test_eventless_role_change_makes_the_inventory_fail_and_the_cli_refuse(self):
        import localchain as lc
        topo = rc.load_topology('execution-testnet-v3')
        m = topo['meta']
        with lc.LocalChain(m['chainId'], m['timestamp']) as chain:
            chain.load(topo['state'])
            actors = {**rc.actor_set('testnet'), 'gov': '0x' + '6f' * 20}
            actors['keeper'] = chain.addr(m['distributor'], 'keeper()')
            p0, _, _ = lc.deploy_p0(chain, network='testnet', actors=actors, proxy_admin=m['proxyAdmin'],
                                    stpros=m['stpros'], destinations=[])
            config, inv = rc.build(chain, topo, p0, actors)
            self.assertTrue(all(s['status'] == 'PASS' for s in inv['roles'].values()))
            # remove E2's ADMIN on vault 3 by a storage write: no RoleRevoked event, the history no longer agrees
            vault = m['vaults'][3]
            slot = _role_slot(chain, vault, models.ADMIN, m['e2'])
            chain.rpc('anvil_setStorageAt', [vault, slot, '0x' + '00' * 32])
            config, inv = rc.build(chain, topo, p0, actors)
            self.assertEqual(inv['roles'][vault]['status'], 'FAIL')
            path, sha = rc.write_bundle(rc.EXEC / 'runs' / 'negative-inventory', config, inv, rc.artifacts('testnet'))
            with self.assertRaises(Exception) as ctx:
                rc.run_cli('testnet-handoff', path, sha)
            self.assertIn('ROLE_INVENTORY', str(ctx.exception))

    def test_reordered_bundle_is_detected(self):
        labels = {'0x' + '01' * 20: 'proxyAdmin'}
        good = [('TL', 'proxyAdmin', 'transferOwnership'), ('TL', 'proxyAdmin', 'closeCreation')]
        txs = [{'to': '0x' + '01' * 20, 'data': scenarios.SIGS['closeCreation']},
               {'to': '0x' + '01' * 20, 'data': scenarios.SIGS['transferOwnership'] + '00' * 32}]
        self.assertTrue(scenarios.shape_mismatches(good, txs, labels))


def _role_slot(chain, vault, role, account):
    """OZ v5 AccessControl storage (ERC-7201 namespace) of hasRole(role, account), located by probing."""
    from tools.lib.keccak import keccak256
    base = int('0x02dd7bc7dec4dceedda775e58dd541e08a116c6c53815c0bd028192f7b626800', 16)
    inner = int(keccak256(bytes.fromhex(role[2:] + format(base, '064x'))), 16)
    slot = int(keccak256(bytes.fromhex(account[2:].rjust(64, '0') + format(inner, '064x'))), 16)
    if int(chain.slot(vault, hex(slot)), 16) != 1:
        raise AssertionError('role slot probe failed')
    return hex(slot)


class AuthorityGateTest(Loopback):
    """XF-02: the authority evidence gate on the recorded mainnet V1 state (no P-0 needed)."""

    def setUp(self):
        import authority_gate
        import localchain as lc
        self.ag = authority_gate
        self.topo = rc.load_topology('execution-mainnet-v1')
        self.m = self.topo['meta']
        self.chain = lc.LocalChain(self.m['chainId'], self.m['timestamp'])
        self.addCleanup(self.chain.close)
        self.chain.load(self.topo['state'])
        self.chain.warp(12)
        self.pin = int(self.chain.block()['number'], 16)
        self.targets = [self.m['vault001'], self.m['factory'], self.m['stpros']]
        self.reviewed = self.ag.reviewed_identities(self.chain, self.targets, self.pin)
        self.extra = '0x' + '5e' * 20

    def run_gate(self, events=()):
        return self.ag.gate(self.chain, 'g', self.targets, self.pin, rc.history(self.chain, self.topo, self.targets),
                            self.reviewed, events)[1]

    def operator(self):
        from tools.release.native._shared import ROLES
        return ROLES['operator']

    def test_clean_state_passes(self):
        self.assertEqual(self.run_gate(), [])

    def test_grant_to_a_never_seen_account_is_detected_unless_reviewed(self):
        from tools.lib import abi
        receipt, _ = self.chain.send('grant', self.m['safe'], self.m['vault001'],
                                     abi.calldata('grantRole(bytes32,address)', self.operator(), self.extra))
        problems = self.run_gate()
        self.assertTrue(any(p.startswith('UNREVIEWED_AUTHORITY_EVENT RoleGranted') for p in problems), problems)
        logs = self.chain.rpc('eth_getTransactionReceipt', [receipt['transactionHash']])['logs']
        self.assertEqual(self.run_gate([(l['transactionHash'], l['logIndex']) for l in logs]), [])

    def test_code_identity_change_without_an_event_is_detected(self):
        from tools.release.native._shared import SLOTS
        self.chain.rpc('anvil_setStorageAt', [self.m['stpros'], SLOTS['implementation'], word(self.m['oracleImpl'])])
        self.assertTrue(any(p.startswith('CODE_IDENTITY') for p in self.run_gate()))

    def test_eventless_role_write_is_the_stated_residual(self):
        """Pinned boundary, not a detection: a test cheat writes a role without an event or a code change; the gate
        passes and hasRole is true. On the reviewed runtimes no such path exists (authority_gate docstring)."""
        held = sorted(models.replay_roles(self.topo['history'], self.m['vault001']))[0]
        try:
            _role_slot(self.chain, self.m['vault001'], held[:66], held[67:])
        except AssertionError:
            self.skipTest('NOT RUN: vault 001 role storage is not the OZ v5 namespace')
        slot = _role_slot_unchecked(self.operator(), self.extra)
        self.chain.rpc('anvil_setStorageAt', [self.m['vault001'], slot, word('0x01')])
        self.assertTrue(self.chain.boolean(self.m['vault001'], 'hasRole(bytes32,address)', self.operator(), self.extra))
        self.assertEqual(self.run_gate(), [])


def _role_slot_unchecked(role, account):
    from tools.lib.keccak import keccak256
    base = int('0x02dd7bc7dec4dceedda775e58dd541e08a116c6c53815c0bd028192f7b626800', 16)
    inner = int(keccak256(bytes.fromhex(role[2:] + format(base, '064x'))), 16)
    return hex(int(keccak256(bytes.fromhex(account[2:].rjust(64, '0') + format(inner, '064x'))), 16))


class ProxyRoundTripTest(Loopback):
    """The budget proxy over real HTTP in front of a loopback node (no fork): every request is charged before it
    is forwarded, a refused method never reaches the upstream, and the ceiling stops forwarding."""

    def test_http_round_trip_and_ceiling(self):
        import localchain as lc
        with tempfile.TemporaryDirectory(dir=ROOT / 'cache_forge/rehearsal') as d, lc.LocalChain(1672, 1_760_000_000) as upstream:
            upstream.warp(12)                                     # a fork block must be > 0
            head = upstream.block()
            auth = fm.simulation_authorization(d, 'mainnet', upstream.node.url + '/', int(head['number'], 16), head['hash'],
                                               ceiling=3)
            ledger = tp.BudgetLedger(Path(d) / 'budget', auth, invocation='rt').open()
            try:
                with fm.ForkBudgetProxy(auth, ledger, sleep=lambda s: None) as proxy:
                    import urllib.request
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                    def post(method, params):
                        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                        req = urllib.request.Request(proxy.url, data=body, headers={'content-type': 'application/json'})
                        try:
                            with opener.open(req, timeout=10) as response:
                                return response.status, json.load(response)
                        except urllib.error.HTTPError as error:
                            return error.code, json.loads(error.read())
                    self.assertEqual(post('eth_chainId', [])[1]['result'], hex(1672))
                    self.assertEqual(post('eth_sendTransaction', [{}])[0], 403)
                    self.assertEqual(post('eth_getLogs', [{}])[0], 403)
                    self.assertEqual(post('eth_getBalance', ['0x' + '00' * 20, 'latest'])[0], 200)
                    self.assertEqual(post('eth_getCode', ['0x' + '00' * 20, 'latest'])[0], 200)
                    self.assertEqual(post('eth_chainId', [])[0], 429)
                    self.assertEqual((proxy.forwarded, ledger.used), (3, 3))
            finally:
                ledger.close()


if __name__ == '__main__':
    unittest.main()
