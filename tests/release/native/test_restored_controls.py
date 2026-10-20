"""Independent negatives for off-chain governance gates whose only historical negatives were archived.

The frozen corpus (reference lock, case hashes) is reused only as unchanged passing inputs.
Each check feeds a realistic adverse observation into the REAL guard at its actual call site
(not a stubbed failure): a non-SETTLED mainnet governed Vault at the fresh-observation and
execution checkpoints (GC-01 A), a rotated Safe owner set at H/R4 (GC-02 C), an extra
Waiting/Ready action-bearing operation or a missing frozen U (GC-03 B), and non-direct
residual round evidence (GPT-S-002). Removing a guarded call site makes these tests fail.
"""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate
from tools.lib import release_json as j
from tools.release.native import inputs, checkpoint, timelock, residual
from tools.release.native._shared import ZERO32

FRESH_1672 = 'history.test.mjs:0001'                      # recheckObserved, current head != reviewed pin
EXECUTION_H = 'read-only.integration.test.mjs:0074'       # verifyCheckpoint stage H (frozen U pending)
POSTCHECK_R4 = 'read-only.integration.test.mjs:0092'      # verifyCheckpoint stage R4 (U Done)
ROTATED_OWNER = '0x' + 'ab' * 20
EXTRA_ID = '0x' + '5e' * 32


def caller():
    # sys._getframe performs no file I/O (the replay harness records every lstat/read).
    frame = sys._getframe(2)
    if frame.f_code.co_name == 'observe_state':
        # Distinguish the reviewed-pin observation from the fresh current-head observation.
        return 'observe_state:reviewed' if frame.f_locals.get('reviewed') else 'observe_state:current'
    return frame.f_code.co_name


class RestoredGateNegatives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These wrong-rule controls replay frozen R4 cases, several of them mainnet release configs without the V4
        # delay profile. They run under the gate's R4 conformance (the V4 mainnet release rule removed, test-only),
        # so each control still reaches the rule it tests; the V4 rule itself is covered by the gate's production
        # pass and test_v4_delay_policy.
        r4 = gate.r4_conformance(); r4.start(); cls.addClassCleanup(r4.stop)
        wanted = {FRESH_1672, EXECUTION_H, POSTCHECK_R4}
        cls.cases = {rec['id']: rec for rec in gate.records() if rec['id'] in wanted}
        if set(cls.cases) != wanted:
            raise AssertionError('Required passing witness missing from frozen corpus')

    def run_case(self, case_id):
        return asyncio.run(gate.run_case(self.cases[case_id]))

    def expect_rejection(self, case_id, code, target):
        baseline = self.run_case(case_id)
        self.assertTrue(baseline['pass'], json.dumps(baseline, default=str)[:600])
        hits = []
        with target(hits):
            changed = self.run_case(case_id)
        self.assertGreater(len(hits), 0, 'guarded call site was not reached')
        self.assertFalse(changed['pass'])
        self.assertEqual(changed['actual']['outcome'], 'throw')
        self.assertTrue(changed['actual']['error']['message'].startswith(code), changed['actual'])

    # GC-01 A: fresh observation and execution/postcheck checkpoints evaluate the current phase.
    def non_settled_at(self, site, phase):
        original = inputs.require_mainnet_phase
        def observed(hits):
            def check(value):
                if caller() == site:
                    hits.append(value)
                    return original(phase)                    # current Vault reports `phase`
                return original(value)
            return patch.object(inputs, 'require_mainnet_phase', check)
        return observed

    def test_fresh_observation_rejects_non_settled_governed_vault(self):
        for phase in (0, 1, 2):
            with self.subTest(phase=phase):
                self.expect_rejection(FRESH_1672, 'MAIN_VAULT_PHASE', self.non_settled_at('observe_state:current', phase))

    def test_execution_checkpoint_rejects_non_settled_governed_vault(self):
        for case_id in (EXECUTION_H, POSTCHECK_R4):
            with self.subTest(case=case_id):
                self.expect_rejection(case_id, 'MAIN_VAULT_PHASE', self.non_settled_at('verify', 1))

    # GC-02 C: a Safe owner change after review is detected at H and R4 checkpoints.
    def rotated_owners(self, hits):
        original = checkpoint.exact_set
        def check(actual, expected, code):
            if code == 'SAFE_OWNERS':
                hits.append(code)
                return original([*actual, ROTATED_OWNER], expected, code)
            return original(actual, expected, code)
        return patch.object(checkpoint, 'exact_set', check)

    def test_safe_owner_rotation_detected_at_execution_and_postcheck(self):
        for case_id in (EXECUTION_H, POSTCHECK_R4):
            with self.subTest(case=case_id):
                self.expect_rejection(case_id, 'SAFE_OWNERS', self.rotated_owners)

    # GC-03 B: Waiting/Ready action-bearing operations remain outstanding at every stage.
    def extra_pending(self, hits):
        original = timelock.check_operation_plan
        def check(inventory, expected, *done):
            hits.append(list(expected))
            changed = dict(inventory, pending=[*inventory['pending'], EXTRA_ID],
                           operations={**inventory['operations'], EXTRA_ID: {'status': 'pending', 'predecessor': ZERO32}})
            return original(changed, expected, *done)
        return patch.object(timelock, 'check_operation_plan', check)

    def test_extra_outstanding_operation_rejected_pre_h_and_post(self):
        for case_id in (FRESH_1672, EXECUTION_H, POSTCHECK_R4):
            with self.subTest(case=case_id):
                self.expect_rejection(case_id, 'TIMELOCK_OUTSTANDING_OPERATIONS', self.extra_pending)

    def missing_frozen_u(self, hits):
        original = timelock.check_operation_plan
        def check(inventory, expected, *done):
            hits.append(list(expected))
            return original(dict(inventory, pending=[]), expected, *done)
        return patch.object(timelock, 'check_operation_plan', check)

    def test_h_checkpoint_requires_frozen_u_still_pending(self):
        self.expect_rejection(EXECUTION_H, 'TIMELOCK_OUTSTANDING_OPERATIONS', self.missing_frozen_u)


class OperationPlanUnit(unittest.TestCase):
    U = '0x' + '0a' * 32
    X = '0x' + '0b' * 32

    def inventory(self, pending, **status):
        operations = {key: {'status': value, 'predecessor': ZERO32} for key, value in status.items()}
        return {'pending': pending, 'operations': operations}

    def rejects(self, code, *args):
        with self.assertRaises(j.ReleaseError) as raised:
            timelock.check_operation_plan(*args)
        self.assertEqual(raised.exception.code, code)

    def test_pre_schedule_requires_empty_outstanding_set(self):
        timelock.check_operation_plan(self.inventory([]), [])
        self.rejects('TIMELOCK_OUTSTANDING_OPERATIONS', self.inventory([self.X], **{self.X: 'pending'}), [])

    def test_h_requires_exactly_frozen_u_with_zero_predecessor(self):
        timelock.check_operation_plan(self.inventory([self.U], **{self.U: 'pending'}), [self.U])
        self.rejects('TIMELOCK_OUTSTANDING_OPERATIONS', self.inventory([], **{self.U: 'done'}), [self.U])
        self.rejects('TIMELOCK_OUTSTANDING_OPERATIONS',
                     self.inventory([self.U, self.X], **{self.U: 'pending', self.X: 'pending'}), [self.U])
        bad = self.inventory([self.U], **{self.U: 'pending'}); bad['operations'][self.U]['predecessor'] = self.X
        self.rejects('TIMELOCK_RELEASE_PREDECESSOR', bad, [self.U])

    def test_post_requires_u_done_and_nothing_outstanding(self):
        timelock.check_operation_plan(self.inventory([], **{self.U: 'done'}), [], self.U)
        self.rejects('TIMELOCK_RELEASE_NOT_EXECUTED', self.inventory([], **{self.U: 'cancelled'}), [], self.U)
        self.rejects('TIMELOCK_OUTSTANDING_OPERATIONS',
                     self.inventory([self.X], **{self.U: 'done', self.X: 'pending'}), [], self.U)


class PhaseAndResidualUnit(unittest.TestCase):
    def test_mainnet_governed_phase_must_be_settled(self):
        inputs.require_mainnet_phase(3)
        for phase in (0, 1, 2):
            with self.subTest(phase=phase), self.assertRaises(j.ReleaseError) as raised:
                inputs.require_mainnet_phase(phase)
            self.assertEqual(raised.exception.code, 'MAIN_VAULT_PHASE')

    def test_residual_round_evidence_must_be_a_direct_vault_call(self):
        vault = '0x' + '0c' * 20
        residual.require_direct_call({'to': vault.upper().replace('0X', '0x')}, {'address': vault})
        for transaction in ({'to': '0x' + '0d' * 20}, {}, {'to': None}):
            with self.subTest(transaction=transaction), self.assertRaises(j.ReleaseError) as raised:
                residual.require_direct_call(transaction, {'address': vault})
            self.assertEqual(raised.exception.code, 'RESIDUAL_ROUND_CALL_EVIDENCE')
            self.assertIn('INCOMPLETE_SAFE_RECOVERY_EVIDENCE', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
