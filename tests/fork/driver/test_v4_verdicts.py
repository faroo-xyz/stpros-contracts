"""UPGRADE / BUSINESS / OVERALL verdicts of a V4 Fork run document and the session ledger (pure; no node).

A run document is crafted per case; ``v4_fork.verdicts`` and ``v4_session.ledger_of`` are the production functions.
"""
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2])]
import v4_fork as vf  # noqa: E402
import v4_session as vs  # noqa: E402

CASES = ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'G-INTEGRATION')


def doc(*, stages=True, r4='PASS', gas_ok=True, status='COMPLETED', cases=None, error=None):
    d = {'status': status, 'error': error, 'stoppedAt': None, 'identity': [{'id': 'x', 'ok': True}], 'notRun': [],
         'stages': {}}
    if stages:
        d['stages'] = {'H': {'ok': True, 'status': 1, 'gasUsed': 808_345, 'transactionHash': '0x1'},
                       'Hcheckpoint': {'status': 'PASS'}, 'UbeforeDelay': {'refused': True},
                       'U': {'ok': True, 'status': 1, 'gasUsed': 1_586_699, 'transactionHash': '0x2'},
                       'Ugas': {'ok': gas_ok, 'gasUsed': 1_586_699 if gas_ok else 2_300_000, 'cap': 2_200_000},
                       'R4': {'status': r4, **({'error': 'SYNTHETIC'} if r4 != 'PASS' else {})}}
    if cases is not None:
        d['acceptance'] = {k: {'verdict': v, 'evidence': [], 'notApplicable': [], 'blocked': None} for k, v in cases.items()}
    d.update(vf.verdicts(d))
    return d


def all_pass(**override):
    return {**{c: 'PASS' for c in CASES}, **override}


class V4VerdictTest(unittest.TestCase):
    def test_everything_passes_is_the_only_overall_pass(self):
        d = doc(cases=all_pass())
        self.assertEqual((d['upgradeVerdict'], d['businessVerdict'], d['overallVerdict']), ('PASS', 'PASS', 'PASS'))
        self.assertEqual(d['releaseGate'], 'OPEN (technical only)')
        self.assertTrue(d['authorization'].startswith('NOT A BROADCAST AUTHORIZATION'))

    def test_one_required_case_fail_fails_overall(self):
        for case in ('A', 'D', 'E', 'G'):
            with self.subTest(case=case):
                d = doc(cases=all_pass(**{case: 'FAIL'}))
                self.assertEqual((d['upgradeVerdict'], d['businessVerdict'], d['overallVerdict']), ('PASS', 'FAIL', 'FAIL'))

    def test_g_accounting_pass_with_integration_blocked_is_not_all_pass(self):
        d = doc(cases=all_pass(**{'G-INTEGRATION': 'BLOCKED'}))
        self.assertEqual((d['caseVerdicts']['G'], d['caseVerdicts']['G-INTEGRATION']), ('PASS', 'BLOCKED'))
        self.assertEqual((d['businessVerdict'], d['overallVerdict']), ('BLOCKED', 'BLOCKED'))

    def test_e_not_applicable_shows_partial(self):
        d = doc(cases=all_pass(E='PARTIAL'))
        self.assertEqual((d['businessVerdict'], d['overallVerdict']), ('PARTIAL', 'PARTIAL'))
        d = doc(cases=all_pass(E='PARTIAL', **{'G-INTEGRATION': 'BLOCKED'}))        # BLOCKED outranks PARTIAL
        self.assertEqual(d['overallVerdict'], 'BLOCKED')
        d = doc(cases=all_pass(E='PARTIAL', F='FAIL'))                             # FAIL outranks both
        self.assertEqual(d['overallVerdict'], 'FAIL')

    def test_r4_refused_never_opens_the_gate(self):
        d = doc(r4='REFUSED', status='STOPPED', error={'code': 'R4_NOT_PASS', 'detail': ''})
        self.assertEqual((d['upgradeVerdict'], d['businessVerdict'], d['overallVerdict']), ('FAIL', 'NOT RUN', 'FAIL'))
        self.assertTrue(d['releaseGate'].startswith('CLOSED: R4_NOT_PASS'), d['releaseGate'])

    def test_gas_over_the_cap_closes_the_gate(self):
        d = doc(gas_ok=False, status='FAIL', error={'code': 'U_GAS_OVER_CAP', 'detail': ''})
        self.assertEqual((d['upgradeVerdict'], d['overallVerdict']), ('FAIL', 'FAIL'))
        self.assertTrue(d['releaseGate'].startswith('CLOSED: U_GAS_OVER_CAP'))

    def test_a_stop_before_h_is_a_failure(self):
        d = doc(stages=False, status='STOPPED', error={'code': 'IDENTITY_MISMATCH', 'detail': ''})
        self.assertEqual((d['upgradeVerdict'], d['overallVerdict']), ('NOT RUN', 'FAIL'))
        self.assertTrue(d['releaseGate'].startswith('CLOSED: IDENTITY_MISMATCH'))

    def test_upgrade_pass_without_a_g_is_incomplete(self):
        d = doc(cases=None)
        self.assertEqual((d['upgradeVerdict'], d['businessVerdict'], d['overallVerdict']), ('PASS', 'NOT RUN', 'INCOMPLETE'))

    def test_an_unknown_case_verdict_is_a_failure(self):
        d = doc(cases=all_pass(C='SKIPPED'))
        self.assertEqual((d['businessVerdict'], d['overallVerdict']), ('FAIL', 'FAIL'))

    # -- required-case completeness -----------------------------------------------------------------------------
    def test_only_a_present_is_incomplete(self):
        d = doc(cases={'A': 'PASS'})
        self.assertEqual((d['businessVerdict'], d['overallVerdict']), ('INCOMPLETE', 'INCOMPLETE'))
        self.assertEqual(d['missingCases'], ['B', 'C', 'D', 'E', 'F', 'G', 'G-INTEGRATION'])

    def test_a_to_g_pass_without_g_integration_is_incomplete(self):
        cases = all_pass()
        del cases['G-INTEGRATION']
        d = doc(cases=cases)
        self.assertEqual((d['businessVerdict'], d['overallVerdict'], d['missingCases']),
                         ('INCOMPLETE', 'INCOMPLETE', ['G-INTEGRATION']))

    def test_a_fail_is_not_hidden_by_missing_cases(self):
        d = doc(cases={'A': 'PASS', 'B': 'FAIL', 'C': 'PASS'})
        self.assertEqual((d['businessVerdict'], d['overallVerdict']), ('FAIL', 'FAIL'))
        self.assertEqual(d['missingCases'], ['D', 'E', 'F', 'G', 'G-INTEGRATION'])
        self.assertEqual(d['caseVerdicts'], {'A': 'PASS', 'B': 'FAIL', 'C': 'PASS'})      # each result kept

    def test_all_eight_pass(self):
        d = doc(cases=all_pass())
        self.assertEqual((d['businessVerdict'], d['overallVerdict'], d['missingCases']), ('PASS', 'PASS', []))

    def test_all_eight_present_with_g_integration_blocked(self):
        d = doc(cases=all_pass(**{'G-INTEGRATION': 'BLOCKED'}))
        self.assertEqual((d['businessVerdict'], d['overallVerdict'], d['missingCases']), ('BLOCKED', 'BLOCKED', []))

    def test_the_ledger_lists_missing_cases(self):
        ledger = vs.ledger_of(doc(cases={'A': 'PASS', 'G': 'PASS'}))
        self.assertEqual((ledger['verdict'], ledger['missingCases']), ('INCOMPLETE', ['B', 'C', 'D', 'E', 'F', 'G-INTEGRATION']))
        rows = {c['id']: c['verdict'] for c in ledger['checks']}
        self.assertEqual([k for k, v in rows.items() if v == 'MISSING'],
                         [f'acceptance.{c}' for c in ('B', 'C', 'D', 'E', 'F', 'G-INTEGRATION')])
        self.assertEqual(rows['BUSINESS'], 'INCOMPLETE')

    def test_the_ledger_verdict_is_overall(self):
        for cases, overall in ((all_pass(), 'PASS'), (all_pass(**{'G-INTEGRATION': 'BLOCKED'}), 'BLOCKED'),
                               (all_pass(E='PARTIAL'), 'PARTIAL'), (all_pass(B='FAIL'), 'FAIL')):
            with self.subTest(overall=overall):
                ledger = vs.ledger_of(doc(cases=cases))
                self.assertEqual((ledger['verdict'], ledger['aggregate']), (overall, overall))
                rows = {c['id']: c['verdict'] for c in ledger['checks']}
                self.assertEqual(rows['UPGRADE'], 'PASS')
                self.assertEqual(rows['BUSINESS'], overall)
                self.assertEqual(rows['acceptance.G-INTEGRATION'], cases['G-INTEGRATION'])
        ledger = vs.ledger_of(doc(r4='REFUSED', status='STOPPED', error={'code': 'R4_NOT_PASS', 'detail': ''}))
        self.assertEqual((ledger['verdict'], ledger['upgradeVerdict']), ('FAIL', 'FAIL'))


if __name__ == '__main__':
    unittest.main()
