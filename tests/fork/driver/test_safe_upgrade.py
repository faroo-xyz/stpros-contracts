"""Regression for the testnet GOV_t Safe upgrade route (``execution.testnet(governance=...)``, ``safe_governance``).
Test-only.

Layers:
- offline: the release-mode signer policy;
- loopback anvil (needs the CampaignAnvilFixture topologies and the forge output of SyntheticSafe.sol in this
  export):
  - ``checked_execute`` refusals and failures on a local Safe model;
  - the full T-0 ... T-5 / U_t upgrade through the GOV_t Safe, followed by the POST checks and the E2E hook;
  - recovery case (f) through the GOV_t and CAN_t Safes;
  - route refusals before T-0, and an inner U_t failure behind a successful outer Safe transaction.

Every run here is SYNTHETIC (local Safe models, loopback node): it proves the route, never a deployed Safe, and it is
never real-state Fork evidence.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import safe_governance as sg  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-testnet-v3.json'
READY = CANONICAL_READY and TOPOLOGY.is_file() and (sg.SAFE_OUT / 'SyntheticSafeProxy.json').is_file()
SKIP = 'NOT RUN: needs the canonical build, the CampaignAnvilFixture topologies and the SyntheticSafe forge output'
MODEL = {'source': 'SYNTHETIC_MODEL'}
U_T_GAS = 'FS-6.U_t.U_t.execTransaction.receipt<=2.2M'


def _addr(label):
    return '0x' + keccak256(label.encode())[-40:]


def _stages(result):
    import release_config as rc
    return json.loads((rc.EXEC / 'runs' / result['run'] / 'run.json').read_text())['stages']


def _verdicts(result, prefix=''):
    return {c['id']: c['verdict'] for c in result['checks'] if c['id'].startswith(prefix)}


class PolicyTest(unittest.TestCase):
    def _safes(self):
        owners = lambda role, n: [_addr(f'{role}{i}') for i in range(n)]
        return {'gov': {'address': _addr('gov'), 'owners': owners('gov', 5), 'threshold': 3},
                'can': {'address': _addr('can'), 'owners': owners('can', 5), 'threshold': 3},
                'guardian': {'address': _addr('grd'), 'owners': owners('grd', 4), 'threshold': 2},
                'ops': {'address': _addr('ops'), 'owners': owners('ops', 4), 'threshold': 2}}

    def test_release_profile_passes(self):
        self.assertEqual(sg.policy_problems(self._safes(), deployer=_addr('d'), keeper=_addr('k'), reporter=_addr('r')), [])

    def test_violations(self):
        s = self._safes(); s['gov']['threshold'] = 2
        self.assertTrue(any('GOV_THRESHOLD' in p for p in sg.policy_problems(s)))
        s = self._safes(); s['gov']['owners'] = s['gov']['owners'][:4]
        self.assertTrue(any('GOV_THRESHOLD' in p for p in sg.policy_problems(s)))
        s = self._safes(); s['can']['threshold'] = 2
        self.assertTrue(any('CAN_THRESHOLD' in p for p in sg.policy_problems(s)))
        s = self._safes(); s['can']['owners'][0] = s['gov']['owners'][0]
        self.assertTrue(any('SAFE_SIGNER_OVERLAP: gov and can' in p for p in sg.policy_problems(s)))
        s = self._safes(); s['ops']['owners'][0] = s['can']['owners'][1]
        self.assertTrue(any('SAFE_SIGNER_OVERLAP: ops and can' in p for p in sg.policy_problems(s)))
        s = self._safes(); s['can']['address'] = s['gov']['address']
        self.assertTrue(any('same Safe' in p for p in sg.policy_problems(s)))
        s = self._safes()
        self.assertTrue(any('DEPLOYER_SIGNER' in p for p in sg.policy_problems(s, deployer=s['ops']['owners'][0])))
        self.assertTrue(any('OPERATIONAL_SAFE_SIGNER: the keeper signs the gov' in p
                            for p in sg.policy_problems(s, keeper=s['gov']['owners'][2])))
        self.assertTrue(any('OPERATIONAL_SAFE_SIGNER: the reporter signs the can' in p
                            for p in sg.policy_problems(s, reporter=s['can']['owners'][2])))


@unittest.skipUnless(READY, SKIP)
class CheckedExecuteTest(unittest.TestCase):
    """``checked_execute`` on a local Safe model: refusals send nothing, and an inner failure is never hidden."""

    @classmethod
    def setUpClass(cls):
        import execution
        cls.loop = execution.Run('execution-testnet-v3', 'test-safe-checked')
        cls.chain = cls.loop.chain
        cls.spec = sg.deploy_model(cls.chain, 'testnet-unit', ('gov',))['gov']
        # a target that records calls (accepts everything) and one that reverts everything
        cls.ok_target, cls.bad_target = _addr('accept all'), _addr('reject all')
        cls.chain.rpc('anvil_setCode', [cls.ok_target, '0x00'])
        cls.chain.rpc('anvil_setCode', [cls.bad_target, '0x60006000fd'])

    @classmethod
    def tearDownClass(cls):
        cls.loop.close()

    def setUp(self):
        self.snap = self.chain.snapshot()

    def tearDown(self):
        self.chain.revert(self.snap)

    def _sent(self, label):
        return [t for t in self.chain.transcript if t['label'].startswith(label)]

    def test_identity_matches_the_model(self):
        self.assertEqual(sg.safe_problems(self.chain, 'gov', self.spec, check_nonce=True), [])
        for key, value in (('runtimeHash', '0x' + '11' * 32), ('singleton', _addr('other singleton')), ('threshold', 2),
                           ('owners', self.spec['owners'][:4] + [_addr('x')]), ('nonce', 7)):
            problems = sg.safe_problems(self.chain, 'gov', {**self.spec, key: value}, check_nonce=True)
            self.assertTrue(problems, key)

    def test_valid_execution(self):
        receipt, rows, facts = sg.checked_execute(self.chain, 'unit.ok', self.spec, self.ok_target, '0x1234',
                                                  expected_nonce=self.spec['nonce'],
                                                  inner_check=lambda r: (True, ''))
        self.assertEqual({r['id']: r['verdict'] for r in rows if r['verdict'] != 'PASS'}, {})
        self.assertEqual(facts['nonceAfter'], facts['nonceBefore'] + 1)
        self.assertEqual(len(facts['approvalGas']), self.spec['threshold'] - 1)
        self.assertEqual(facts['safeEvents'], ['SUCCESS'])

    def test_wrong_signer_sends_nothing(self):
        signers = self.spec['owners'][:2] + [_addr('not an owner')]
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.signer', self.spec, self.ok_target, '0x', signers=signers)
        self.assertIsNone(receipt)
        self.assertEqual({r['id']: r['verdict'] for r in rows}['unit.signer.signers.validOrderedThreshold'], 'FAIL')
        self.assertEqual(self._sent('unit.signer'), [])

    def test_too_few_signers_send_nothing(self):
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.few', self.spec, self.ok_target, '0x', signers=self.spec['owners'][:2])
        self.assertIsNone(receipt)
        self.assertIn('FAIL', [r['verdict'] for r in rows if r['id'] == 'unit.few.signers.validOrderedThreshold'])
        self.assertEqual(self._sent('unit.few'), [])

    def test_wrong_threshold_sends_nothing(self):
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.threshold', {**self.spec, 'threshold': 2}, self.ok_target, '0x')
        self.assertIsNone(receipt)
        self.assertEqual({r['id']: r['verdict'] for r in rows}['unit.threshold.safe.threshold==approved'], 'FAIL')
        self.assertEqual(self._sent('unit.threshold'), [])

    def test_wrong_nonce_sends_nothing(self):
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.nonce', self.spec, self.ok_target, '0x',
                                              expected_nonce=self.spec['nonce'] + 1)
        self.assertIsNone(receipt)
        self.assertEqual({r['id']: r['verdict'] for r in rows}['unit.nonce.safe.nonce==expected'], 'FAIL')
        self.assertEqual(self._sent('unit.nonce'), [])

    def test_wrong_safe_identity_sends_nothing(self):
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.identity', {**self.spec, 'owners': self.spec['owners'][1:] + [_addr('y')]},
                                              self.ok_target, '0x')
        self.assertIsNone(receipt)
        self.assertEqual({r['id']: r['verdict'] for r in rows}['unit.identity.safe.owners==approved'], 'FAIL')
        receipt, rows, _ = sg.checked_execute(self.chain, 'unit.nocode', {**self.spec, 'address': _addr('no code')}, self.ok_target, '0x')
        self.assertIsNone(receipt)
        self.assertEqual(rows[0]['verdict'], 'FAIL')

    def test_inner_failure_is_not_masked(self):
        # safeTxGas != 0: the Safe catches the inner revert, emits ExecutionFailure and the outer transaction succeeds
        receipt, rows, facts = sg.checked_execute(self.chain, 'unit.masked', self.spec, self.bad_target, '0x', safe_tx_gas=100_000,
                                                  inner_check=lambda r: (False, 'inner effect absent'))
        v = {r['id']: r['verdict'] for r in rows}
        self.assertEqual(int(receipt['status'], 16), 1)                      # the outer receipt alone would look fine
        self.assertEqual(facts['safeEvents'], ['FAILURE'])
        for rid in ('unit.masked.executionSuccess', 'unit.masked.outer==inner', 'unit.masked.innerSuccess', 'unit.masked.innerEffect'):
            self.assertEqual(v[rid], 'FAIL', rid)

    def test_required_rejection_keeps_the_nonce(self):
        receipt, rows, facts = sg.checked_execute(self.chain, 'unit.reject', self.spec, self.bad_target, '0x', expect=False)
        v = {r['id']: r['verdict'] for r in rows}
        self.assertEqual(int(receipt['status'], 16), 0)
        self.assertEqual(facts['nonceAfter'], facts['nonceBefore'])
        self.assertEqual({k: x for k, x in v.items() if x != 'PASS'}, {})


@unittest.skipUnless(READY, SKIP)
class SafeUpgradeTest(unittest.TestCase):
    """T-0 ... T-5 / U_t through the GOV_t Safe, POST checks, then the E2E hook on the same S_post."""

    @classmethod
    def setUpClass(cls):
        import e2e
        import execution
        import governance
        cls.ex = execution
        cls.out = execution.OUT / 'test-safe-upgrade' / 'e2e'
        if cls.out.is_dir():
            import shutil
            shutil.rmtree(cls.out)
        cls.e2e = {}
        hook = e2e.hook(cls.out, result=cls.e2e, scenarios={'A01', 'A04', 'A05', 'A06', 'C06'})
        cls.r = execution.testnet('normal', name='test-safe-upgrade', governance=MODEL,
                                  then=e2e.compose(governance.authority_only, hook))
        cls.v = _verdicts(cls.r)

    def test_route_and_identity(self):
        route = next(s for s in _stages(self.r) if s['stage'] == 'governanceRoute')
        self.assertEqual((route['mode'], route['class']), ('SAFE', 'SYNTHETIC'))
        self.assertEqual((route['gov']['threshold'], len(route['gov']['owners'])), (3, 5))
        self.assertGreaterEqual(route['can']['threshold'], 3)
        self.assertFalse(set(route['gov']['owners']) & set(route['can']['owners']))
        for rid in ('SAFE.gov.identity==approved', 'SAFE.can.identity==approved', 'SAFE.policy.signerIsolation',
                    'SAFE.T-0.GOV_t.proposer&executor', 'SAFE.T-0.CAN_t.canceller', 'SAFE.T-0.CAN_t.notProposerNorExecutor',
                    'SAFE.T-0.noEOAProposer', 'P-0.timelock.roles.exact'):
            self.assertEqual(self.v.get(rid), 'PASS', rid)

    def test_t5_and_u_t_through_the_safe(self):
        for label in ('T-5.scheduleBatch', 'U_t'):
            for check in ('safe.owners==approved', 'safe.threshold==approved', 'safe.nonce==expected', 'signers.validOrderedThreshold',
                          'safeTxHash==independentEIP712', 'approvalsRecorded', 'executionSuccess', 'outer==inner',
                          'innerSuccess', 'innerEffect', 'nonceAdvanced'):
                self.assertEqual(self.v.get(f'{label}.{check}'), 'PASS', f'{label}.{check}')
        self.assertEqual(self.v.get('SAFE.gov.nonce.afterU_t'), 'PASS')
        labels = [t['label'] for t in self.r['transcript']]
        self.assertIn('T-5.scheduleBatch.execTransaction', labels)
        self.assertIn('U_t.execTransaction', labels)
        self.assertNotIn('U_t.executeBatch', labels)                      # no direct EOA upgrade
        for rid in ('SAFE.U_t.beforeDelay.outcomeAsRequired', 'SAFE.U_t.beforeDelay.nonce+0', 'GC-03.afterT-5.exactlyU',
                    'GC-03.afterU_t.doneNoPending', 'reject.U_t.second', 'reject.U_t.byCAN'):
            self.assertEqual(self.v.get(rid), 'PASS', rid)
        self.assertTrue([i for i in self.v if i.startswith('reject.SAFE.U_t.byGOVSigner.')])

    def test_gas_frames(self):
        breakdown = next(s for s in _stages(self.r) if s['stage'] == 'U_t.gasBreakdown')
        self.assertTrue(breakdown['frame'].startswith('SAFE_OUTER_RECEIPT'))
        self.assertEqual(len(breakdown['items']), 22)
        self.assertEqual(len(breakdown['approvalGas']), 2)
        self.assertEqual(self.v.get('U_t.gas.innerItemsWithinOuterReceipt'), 'PASS')
        self.assertEqual(self.v.get(U_T_GAS), 'FAIL')                    # measured: the synthetic U_t exceeds 2.2M (FR-D3)

    def test_post_checks_and_ledgers(self):
        import test_execution as te
        te.only_gas_blocked(self, self.r, {U_T_GAS, 'E2E.formal'})        # E2E.formal is BLOCKED by design (SYNTHETIC)
        self.assertEqual(self.v.get('E2E.execution'), 'PASS')
        self.assertEqual(self.r['ledgers']['FS-2.normal']['aggregate'], 'PASS')
        for sid in ('FS-5t.relations', 'FS-5t.partialClaims', 'FS-5t.vaults', 'FS-3t.1'):
            self.assertEqual(self.r['ledgers'][sid]['aggregate'], 'PASS', sid)
        self.assertGreaterEqual(sum(1 for i in self.v if i.startswith('POST-')), 20)

    def test_e2e_continues_from_s_post(self):
        doc = self.e2e
        ctx = doc['context']
        self.assertEqual(ctx['postcheck']['verdict'], 'PASS', ctx['postcheck'].get('why'))
        self.assertEqual((ctx['postcheck']['route'], ctx['postcheck']['upgradeTx']), ('SAFE', 'U_t.execTransaction'))
        self.assertTrue(ctx['postcheck']['sPostLinked'])
        self.assertEqual(ctx['governance']['mode'], 'SAFE')
        self.assertEqual(ctx['governanceProblems'], [])
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        self.assertEqual(v['formal'], 'BLOCKED')
        self.assertTrue(any('GOV_t Safe route (SYNTHETIC)' in b for b in v['blockers']))
        rows = {r['id']: r for r in doc['rows']}
        for rid in ('A01.deposit', 'A04.redeem', 'A05.withdrawCompleteToPROS()', 'A06.first.maxRecords1', 'A08.realLiquidity'):
            self.assertEqual(rows[rid]['verdict'], 'PASS', rid)
        tl_ops = [r for r in doc['rows'] if r.get('caller') == ctx['addresses']['timelock'] and r['verdict'] == 'PASS']
        self.assertTrue(tl_ops)                                           # Timelock operations by the GOV_t Safe after U_t
        self.assertEqual({r['route'] for r in tl_ops}, {'Safe approved-hash'})
        self.assertFalse((self.out / 'POST-UPGRADE-USER-E2E.json').exists())
        self.assertTrue((self.out / 'E2E-HARNESS-SELF-TEST.json').is_file())


@unittest.skipUnless(READY, SKIP)
class SafeRouteControlTest(unittest.TestCase):
    """Recovery through the Safes, and every refusal: nothing after a refused or failed Safe step runs."""

    @classmethod
    def setUpClass(cls):
        import execution
        cls.ex = execution

    def _hooked(self):
        called = []
        return called, lambda run, ctx: called.append(1) or []

    def test_recovery_f_through_gov_and_can_safes(self):
        r = self.ex.testnet('f', name='test-safe-f', governance=MODEL)
        v = _verdicts(r)
        self.assertEqual(r['ledgers']['FS-2.f']['aggregate'], 'PASS', sorted(k for k, x in v.items() if x != 'PASS')[:10])
        for rid in ('FS-2.f.U_t.reverts.outcomeAsRequired', 'FS-2.f.cancelByCAN.executionSuccess', 'FS-2.f.reschedule.executionSuccess',
                    'FS-2.f.atomicRollback', 'FS-2.f.operationUnset', 'SAFE.gov.nonce.afterU_t', 'U_t.executionSuccess'):
            self.assertEqual(v.get(rid), 'PASS', rid)

    def _blocked_before_t0(self, name, governance=MODEL, patch=None):
        called, hook = self._hooked()
        if patch:
            with mock.patch.object(sg, 'deploy_model', patch):
                r = self.ex.testnet('normal', name=name, governance=governance, then=hook)
        else:
            r = self.ex.testnet('normal', name=name, governance=governance, then=hook)
        labels = [t['label'] for t in r['transcript']]
        self.assertFalse([l for l in labels if l.startswith(('P-0', 'T-', 'U_t'))], 'nothing may run after a refused route')
        self.assertEqual(called, [])
        self.assertNotEqual(r['ledgers']['FS-2.normal']['aggregate'], 'PASS')
        return _verdicts(r)

    def test_unapproved_record_blocks_before_t0(self):
        path = Path(self.ex.OUT) / 'test-safe-unapproved-record.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 'faroo-e2e-release-record-v1'}))
        import hashlib
        v = self._blocked_before_t0('test-safe-norecord', {'source': 'APPROVED_RECORD',
                                                           'record': (path, hashlib.sha256(path.read_bytes()).hexdigest())})
        self.assertEqual(v.get('INPUT.record.approved'), 'BLOCKED')
        self.assertEqual(v.get('INPUT.notUsable'), 'BLOCKED')

    def _patched(self, **over):
        real = sg.deploy_model

        def patched(chain, network, roles, **kw):
            out = real(chain, network, roles, **kw)
            out['gov'].update(over)
            return out
        return patched

    def test_wrong_threshold_blocks_before_t0(self):
        v = self._blocked_before_t0('test-safe-threshold', patch=self._patched(threshold=2))
        self.assertEqual(v.get('SAFE.gov.identity==approved'), 'FAIL')
        self.assertEqual(v.get('SAFE.route.notUsable'), 'BLOCKED')

    def test_wrong_safe_identity_blocks_before_t0(self):
        v = self._blocked_before_t0('test-safe-runtime', patch=self._patched(runtimeHash='0x' + '22' * 32))
        self.assertEqual(v.get('SAFE.gov.identity==approved'), 'FAIL')

    def test_wrong_nonce_blocks_before_t0(self):
        v = self._blocked_before_t0('test-safe-nonce', patch=self._patched(nonce=5))
        self.assertEqual(v.get('SAFE.gov.identity==approved'), 'FAIL')

    def test_wrong_signer_stops_at_t5(self):
        import execution
        real = execution.sg.checked_execute

        def wrong(chain, label, spec, to, data, **kw):
            if label == 'T-5.scheduleBatch':
                kw['signers'] = spec['owners'][:2] + [_addr('intruder')]
            return real(chain, label, spec, to, data, **kw)
        called, hook = self._hooked()
        with mock.patch.object(execution.sg, 'checked_execute', wrong):
            r = self.ex.testnet('normal', name='test-safe-signer', governance=MODEL, then=hook)
        v = _verdicts(r)
        self.assertEqual(v.get('T-5.scheduleBatch.signers.validOrderedThreshold'), 'FAIL')
        self.assertEqual(v.get('T-5.scheduleBatch.notExecuted'), 'FAIL')
        labels = [t['label'] for t in r['transcript']]
        self.assertFalse([l for l in labels if l.startswith(('T-5', 'U_t'))])
        self.assertEqual(called, [])
        self.assertEqual(r['ledgers']['FS-2.normal']['aggregate'], 'FAIL')

    def test_inner_upgrade_failure_behind_outer_success_stops(self):
        """U_t sent with safeTxGas != 0 while its inner executeBatch reverts (the case-f disclosed write): the outer
        Safe transaction succeeds, the operation is not Done, and the run stops before the POST checks and the hook."""
        import execution
        import release_config as rc
        real = execution.sg.checked_execute
        vault = rc.load_topology('execution-testnet-v3')['meta']['vaults'][14]

        def masked(chain, label, spec, to, data, **kw):
            if label == 'U_t':
                v = vault
                slot = execution._probe_slot(chain, v, 'roundCap()')
                chain.rpc('anvil_setStorageAt', [v, slot, '0x' + format(int(chain.slot(v, slot), 16) + 1, '064x')])
                kw['safe_tx_gas'] = 3_000_000
            return real(chain, label, spec, to, data, **kw)
        called, hook = self._hooked()
        with mock.patch.object(execution.sg, 'checked_execute', masked):
            r = self.ex.testnet('normal', name='test-safe-masked', governance=MODEL, then=hook)
        v = _verdicts(r)
        outer = next(t for t in r['transcript'] if t['label'] == 'U_t.execTransaction')
        self.assertEqual(outer['status'], 1)                              # the outer receipt alone looks successful
        for rid in ('U_t.executionSuccess', 'U_t.outer==inner', 'U_t.innerSuccess', 'U_t.innerEffect'):
            self.assertEqual(v.get(rid), 'FAIL', rid)
        self.assertFalse([i for i in v if i.startswith('POST-')])          # no POST check ran on a failed upgrade
        self.assertEqual(called, [])                                       # and the E2E hook never ran
        self.assertEqual(r['ledgers']['FS-2.normal']['aggregate'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
