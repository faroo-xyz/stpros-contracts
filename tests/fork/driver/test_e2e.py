"""Permanent regression for the post-upgrade E2E harness (e2e.py, e2e_scenarios.py). Test-only.

Layers:
- offline: spec integrity against the canonical ABIs, context and formal-gate rules, revert-data and coverage logic;
- loopback anvil (needs the CampaignAnvilFixture topologies in this export): full synthetic runs on both chains;
- negative controls: an injected fault must turn the harness verdict to FAIL or BLOCKED, never PASS.

No public RPC, no Fork of public state. The loopback node is the one ``execution`` owns.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import e2e  # noqa: E402
import e2e_identity as ident  # noqa: E402
import e2e_rehearsal as reh  # noqa: E402

CANONICAL_READY = (e2e.CANONICAL / 'StPROS' / 'artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-testnet-v3.json'
ANVIL_READY = CANONICAL_READY and TOPOLOGY.is_file()
SAFE_READY = ANVIL_READY and (reh.SAFE_OUT / 'SyntheticSafeProxy.json').is_file()
RUNTIMES = reh.runtimes_dir()
INTEGRATIONS_READY = ANVIL_READY and RUNTIMES is not None


def _run(network, pre=None, **kw):
    import execution
    result = {}
    fn = execution.testnet if network == 'testnet' else execution.mainnet
    case = 'normal' if network == 'testnet' else 'A'
    name = kw.pop('name')
    out = execution.OUT / name / 'e2e'
    if out.is_dir():                                   # test-owned output of an earlier run in this export
        import shutil
        shutil.rmtree(out)
    hook = e2e.hook(out, result=result, **kw)
    fn(case, name=name, then=e2e.compose(pre, hook) if pre else hook)
    return result, out


def _write_record(h, directory, **over):
    """A well-formed E2E release record for the node in ``h`` (what an approved record would carry)."""
    doc = {'schema': ident.RECORD_SCHEMA, 'chainId': h.ctx['chainId'], 'anchor': h.ctx['anchor'],
           'binding': {'frozenCommit': ident.FROZEN_COMMIT, 'specSha256': h.spec_sha256(),
                       'inventorySha256': h.spec['derivedFrom']['sha256'], 'artifactSha256': e2e.artifact_entries(h.canonical)[1]},
           'actors': {k: h.actors[k] for k in ident.ROLE_KEYS}, 'safes': {},
           'history': {'s1ManifestSha256': (h.run.topo.get('binding') or {}).get('s1ManifestSha256')},
           'readAuthorization': {'specSha256': None}, 'ownerInputs': {}, 'integrations': {}}
    for key, value in over.items():
        doc[key] = {**doc[key], **value} if isinstance(doc.get(key), dict) and isinstance(value, dict) else value
    raw = json.dumps(doc, sort_keys=True).encode()
    path = Path(directory) / 'record.json'
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _install_record(h, directory, pins, *, pin=True, **over):
    path, sha = _write_record(h, directory, **over)
    if pin:
        pins.add(sha)
    e2e.apply_record(h.ctx, h.run, (path, sha))
    return sha


class SpecTest(unittest.TestCase):
    def test_vendored_spec_shape(self):
        spec = e2e.load_spec()
        writes = [e for e in spec['entries'] if e['mutability'] not in ('view', 'pure')]
        self.assertEqual((len(writes), len(spec['entries']) - len(writes)), (112, 157))
        self.assertEqual({x['policy'] for x in writes}, set(e2e.POLICIES) - {'READ'})
        for e_ in writes:
            if e_['policy'] in e2e.RESTRICTED:
                self.assertTrue(e_['unauthorizedError'], e_['signature'])
            if e_['policy'] == 'PERMISSIONLESS':
                self.assertIsNone(e_['unauthorizedError'], e_['signature'])
        review = sorted(f"{x['contract']}.{x['signature']}" for x in spec['entries'] if x['status'] != 'SPECIFIED')
        self.assertEqual(review, ['StPROS.deposit(uint256,address)', 'StPROS.mint(uint256,address)'])

    def test_spec_matches_approved_inventory(self):
        path = os.environ.get('FAROO_E2E_INVENTORY')
        if not path:
            self.skipTest('NOT RUN: set FAROO_E2E_INVENTORY to the approved research inventory')
        import hashlib
        raw = Path(path).read_bytes()
        derived = e2e.derive_spec(json.loads(raw), hashlib.sha256(raw).hexdigest())
        self.assertEqual(derived, e2e.load_spec())

    @unittest.skipUnless(CANONICAL_READY, 'NOT RUN: needs tools/ci/canonical.py output in the export')
    def test_spec_matches_canonical_abis(self):
        self.assertEqual(e2e.verify_spec(e2e.load_spec()), [])

    @unittest.skipUnless(CANONICAL_READY, 'NOT RUN: needs tools/ci/canonical.py output in the export')
    def test_spec_negatives(self):
        spec = e2e.load_spec()
        missing = copy.deepcopy(spec); missing['entries'] = [x for x in missing['entries'] if x['signature'] != 'fundProfit()']
        self.assertTrue(any('missing from the spec' in p for p in e2e.verify_spec(missing)))
        wrong = copy.deepcopy(spec)
        next(x for x in wrong['entries'] if x['signature'] == 'finalize()')['selector'] = '0xdeadbeef'
        self.assertTrue(any('selector mismatch' in p for p in e2e.verify_spec(wrong)))
        extra = copy.deepcopy(spec)
        extra['entries'].append(dict(extra['entries'][0], signature='notInAbi()', selector='0x12345678'))
        self.assertTrue(any('not in the ABI' in p for p in e2e.verify_spec(extra)))
        hashes = copy.deepcopy(spec); hashes['artifactSha256']['StPROS'] = '00' * 32
        self.assertTrue(any('artifact hashes' in p for p in e2e.verify_spec(hashes)))
        dup = copy.deepcopy(spec); dup['entries'].append(dup['entries'][0])
        self.assertTrue(any('duplicate' in p for p in e2e.verify_spec(dup)))


class LogicTest(unittest.TestCase):
    def test_unauthorized_error_parsing(self):
        self.assertEqual(e2e.unauthorized_error('revert OwnableUnauthorizedAccount(caller)'), 'OwnableUnauthorizedAccount')
        self.assertEqual(e2e.unauthorized_error('a direct native send by a user or another contract reverts OnlyAssetCanSendETH'),
                         'OnlyAssetCanSendETH')
        self.assertIsNone(e2e.unauthorized_error('NOT APPLICABLE: permissionless'))

    def test_revert_data_parsing(self):
        self.assertEqual(e2e.revert_data(ValueError(str({'code': 3, 'message': 'execution reverted', 'data': '0xAbCd1234'}))), '0xabcd1234')
        self.assertEqual(e2e.revert_data(ValueError(str({'code': 3, 'message': 'execution reverted: custom error 0x681a8218: 44'}))),
                         '0x681a8218')
        self.assertEqual(e2e.revert_data(ValueError(str({'code': -32003, 'message': 'execution reverted'}))), '0x')

    def _ctx(self, **over):
        a = '0x' + '11' * 20
        ctx = {'chainId': 688689, 'evidence': e2e.REAL, 'anchor': {'block': 1, 'hash': '0x' + '22' * 32},
               'source': {'commit': 'a110037', 'artifactSha256': {'StPROS': 'x'}},
               'addresses': {**{k: a for k in e2e.ADDRESS_KEYS}, 'governedVaults': [a], 'retiredVaults': []},
               'actors': {k: a for k in e2e.ACTOR_KEYS}, 'actorClasses': {k: e2e.REAL for k in e2e.ACTOR_KEYS},
               'postcheck': {'verdict': 'PASS'}, 'history': {'owners': [], 'authenticated': True},
               'governance': {'mode': 'EOA', 'delay': 300},
               'formal': {'readAuthorization': True, 'ownerInputsApproved': True, 'safeManifestApproved': True},
               'integrations': {'approved': True}, 'record': {'schema': ident.RECORD_SCHEMA},
               'releaseInputs': {'source': 'APPROVED_RECORD'},
               'upgradeRoute': {'mode': 'SAFE', 'safe': a, 'approved': True}}
        ctx.update(over)
        return ctx

    def test_context_and_formal_gate(self):
        self.assertEqual(e2e.context_problems(self._ctx()), [])
        self.assertEqual(e2e.formal_blockers(self._ctx()), [])
        self.assertTrue(e2e.context_problems(self._ctx(anchor={})))
        missing = self._ctx(); del missing['history']
        self.assertTrue(e2e.context_problems(missing))
        self.assertTrue(any('SYNTHETIC' in b for b in e2e.formal_blockers(self._ctx(evidence=e2e.SYNTHETIC))))
        classes = {k: e2e.REAL for k in e2e.ACTOR_KEYS}; classes['ops'] = e2e.SYNTHETIC
        self.assertTrue(any('real approved identity' in b for b in e2e.formal_blockers(self._ctx(actorClasses=classes))))
        self.assertTrue(any('FH-D2' in b for b in e2e.formal_blockers(self._ctx(history={'owners': [], 'authenticated': False}))))
        self.assertTrue(any('postcheck' in b for b in e2e.formal_blockers(self._ctx(postcheck={'verdict': 'FAIL'}))))
        for flag in ('readAuthorization', 'ownerInputsApproved', 'safeManifestApproved'):
            formal = {'readAuthorization': True, 'ownerInputsApproved': True, 'safeManifestApproved': True, flag: False}
            self.assertTrue(e2e.formal_blockers(self._ctx(formal=formal)), flag)
        self.assertTrue(any('no approved E2E release record' in b for b in e2e.formal_blockers(self._ctx(record=None))))
        self.assertTrue(any('upgrade route' in b for b in e2e.formal_blockers(self._ctx(upgradeRoute={'mode': 'EOA', 'approved': False}))))
        self.assertTrue(any('upgrade route' in b for b in e2e.formal_blockers(self._ctx(upgradeRoute={'mode': 'SAFE', 'approved': False}))))
        self.assertTrue(any('integrations' in b for b in e2e.formal_blockers(self._ctx(integrations={'approved': False}))))
        self.assertTrue(any('rehearsal' in b for b in e2e.formal_blockers(self._ctx(rehearsal=['setCode']))))
        self.assertTrue(any('stale' in b for b in e2e.formal_blockers(self._ctx(governanceProblems=['stale signer set']))))
        self.assertTrue(any('postcheck MISSING' in p for p in e2e.context_problems(self._ctx(postcheck={'verdict': 'MISSING'}))))
        self.assertTrue(any('release inputs SYNTHETIC' in b for b in e2e.formal_blockers(self._ctx(releaseInputs={'source': 'SYNTHETIC'}))))
        self.assertTrue(any('drove the upgrade' in p for p in
                            e2e.context_problems(self._ctx(recordMismatch='E2E record a is not the record b that drove the upgrade'))))


    def test_report_name_guard(self):
        class Chain:
            forked = False
        for evidence, forked, problems in ((e2e.SYNTHETIC, True, []), (e2e.REAL, False, []),
                                           (e2e.REAL, True, ['BLOCKED context: anchor missing'])):
            h = object.__new__(e2e.Harness)
            h.evidence = lambda ev=evidence: {'evidence': ev}
            h.chain = Chain(); h.chain.forked = forked
            h.problems = problems
            with self.assertRaises(e2e.E2EError):
                e2e.Harness.write(h, '/nonexistent-never-written', name='POST-UPGRADE-USER-E2E')


class RecordTest(unittest.TestCase):
    """Offline rules of the approved release record, its bindings, the upgrade postcheck and history authentication."""

    def _record(self, directory, **over):
        doc = {'schema': ident.RECORD_SCHEMA, 'chainId': 688689, 'anchor': {}, 'binding': {}, 'actors': {}, 'safes': {},
               'history': {'authenticated': True}, 'readAuthorization': {}, 'ownerInputs': {}, 'integrations': {}}
        doc.update(over)
        raw = json.dumps(doc).encode()
        path = Path(directory) / 'r.json'
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_record_needs_pin_hash_and_fields(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as d:
            path, sha = self._record(d)
            doc, problems = ident.load_record(path, sha)
            self.assertIsNone(doc)
            self.assertTrue(any('not independently approved' in p for p in problems))
            with mock.patch.object(ident, 'APPROVED_E2E_RECORDS', frozenset({sha})):
                doc, problems = ident.load_record(path, sha)
                self.assertEqual(problems, [])
                self.assertEqual(doc['chainId'], 688689)
                self.assertIsNone(ident.load_record(path, '00' * 32)[0])            # wrong expected hash
            path, sha = self._record(d, schema='other')
            with mock.patch.object(ident, 'APPROVED_E2E_RECORDS', frozenset({sha})):
                self.assertIsNone(ident.load_record(path, sha)[0])
            path, sha = self._record(d)
            raw = json.loads(path.read_text()); del raw['safes']
            path.write_text(json.dumps(raw)); sha = hashlib.sha256(path.read_bytes()).hexdigest()
            with mock.patch.object(ident, 'APPROVED_E2E_RECORDS', frozenset({sha})):
                doc, problems = ident.load_record(path, sha)
                self.assertIsNone(doc)
                self.assertTrue(any('safes missing' in p for p in problems))

    def test_modified_spec_policy_breaks_binding(self):
        spec = e2e.load_spec()
        sha = e2e._sha(json.dumps(spec, sort_keys=True).encode())
        record = {'binding': {'frozenCommit': ident.FROZEN_COMMIT, 'specSha256': sha, 'inventorySha256': spec['derivedFrom']['sha256'],
                              'artifactSha256': spec['artifactSha256']}}
        self.assertEqual(ident.binding_problems(record, sha, spec, spec['artifactSha256']), [])
        modified = copy.deepcopy(spec)
        entry = next(x for x in modified['entries'] if x['signature'] == 'fundProfit()')
        entry['policy'] = 'OWNER_ONLY'                       # a known policy: verify_spec alone cannot see the change
        msha = e2e._sha(json.dumps(modified, sort_keys=True).encode())
        problems = ident.binding_problems(record, msha, modified, spec['artifactSha256'])
        self.assertTrue(any(p.startswith('FAIL binding: E2E spec') for p in problems))
        other = dict(record['binding'], frozenCommit='0' * 40)
        self.assertTrue(any('frozen commit' in p for p in ident.binding_problems({'binding': other}, sha, spec, spec['artifactSha256'])))
        self.assertTrue(any('artifacts' in p for p in ident.binding_problems(record, sha, spec, {'StPROS': '00'})))

    def _fake_run(self, checks, tx=True, status=1):
        class Run:
            pass
        run = Run()
        run.m = {'chainId': 688689}
        run.chain = Run()
        run.chain.transcript = ([{'label': 'U_t.executeBatch', 'status': status, 'expectSuccess': True, 'transactionHash': '0xab',
                                  'from': '0xcd'}] if tx else [])
        run.checks = checks
        run.topo = {'binding': {'provenance': {'authenticated': False}, 's1ManifestSha256': 'aa'}}
        run.real = False
        return run

    def test_postcheck_requires_executed_upgrade_and_post_checks(self):
        base = [{'id': 'tx.U_t.executeBatch', 'verdict': 'PASS'}, {'id': 'GC-03.afterU_t.doneNoPending', 'verdict': 'PASS'}]
        posts = [{'id': f'POST-{i}', 'verdict': 'PASS'} for i in range(22)]
        self.assertEqual(ident.postcheck(self._fake_run(base + posts))['verdict'], 'PASS')
        self.assertEqual(ident.postcheck(self._fake_run(base))['verdict'], 'MISSING')              # zero POST checks
        self.assertEqual(ident.postcheck(self._fake_run(base + posts[:5]))['verdict'], 'MISSING')  # skipped POST checks
        self.assertEqual(ident.postcheck(self._fake_run(base + posts, tx=False))['verdict'], 'MISSING')
        self.assertEqual(ident.postcheck(self._fake_run(base + posts, status=0))['verdict'], 'FAIL')
        failed = posts[:-1] + [{'id': 'POST-x', 'verdict': 'FAIL'}]
        self.assertEqual(ident.postcheck(self._fake_run(base + failed))['verdict'], 'FAIL')
        self.assertEqual(ident.postcheck(self._fake_run(base[:1] + posts))['verdict'], 'FAIL')

    def test_history_authentication_cannot_be_claimed(self):
        run = self._fake_run([])
        ok, reasons = ident.history_authenticated(run, {'history': {'authenticated': True, 's1ManifestSha256': 'aa'}}, False)
        self.assertFalse(ok)
        self.assertTrue(any('FH-D2' in r for r in reasons))
        run.topo['binding']['provenance']['authenticated'] = True
        self.assertFalse(ident.history_authenticated(run, {'history': {'s1ManifestSha256': 'aa'}}, False)[0])
        self.assertFalse(ident.history_authenticated(run, None, True)[0])
        self.assertFalse(ident.history_authenticated(run, {'history': {'s1ManifestSha256': 'bb'}}, True)[0])
        self.assertTrue(ident.history_authenticated(run, {'history': {'s1ManifestSha256': 'aa'}}, True)[0])
        self.assertFalse(ident.read_authorization(run, {'readAuthorization': {'specSha256': None}}))   # not a real run


@unittest.skipUnless(ANVIL_READY, 'NOT RUN: run the canonical build and the CampaignAnvilFixture tests in this export first')
class SyntheticRunTest(unittest.TestCase):
    """Full harness on the synthetic loopback rehearsals of both chains."""

    def _assert_full(self, doc, out):
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        self.assertEqual(v['formal'], 'BLOCKED')
        self.assertTrue(any('SYNTHETIC context' in b for b in v['blockers']))
        self.assertEqual(set(doc['rowClasses']), {e2e.SYNTHETIC})
        self.assertEqual(doc['rowCounts'].get('FAIL', 0), 0)
        cov = doc['coverage']
        self.assertEqual(len(cov), 269)
        bad = {k: c['status'] for k, c in cov.items() if c['status'] != 'PASS'}
        self.assertEqual(bad, {'StPROS.deposit(uint256,address)': 'NEEDS_REVIEW', 'StPROS.mint(uint256,address)': 'NEEDS_REVIEW'})
        self.assertEqual(v['needsReview'], sorted(bad))
        self.assertTrue((out / 'E2E-HARNESS-SELF-TEST.json').is_file())
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())
        for r in doc['rows']:
            if r['verdict'] == 'PASS' and r.get('expected', '').startswith('revert'):
                self.assertEqual(r['deltas'], {}, r['id'])
                self.assertEqual(r['status'], 0, r['id'])

    def test_testnet_full(self):
        doc, out = _run('testnet', name='test-e2e-testnet')
        self._assert_full(doc, out)
        ids = {r['id'] for r in doc['rows']}
        for required in ('B07.forcePar', 'B07.holderRedeemsAtPar', 'A05.withdrawCompleteToPROS()', 'A08.realLiquidity', 'A08.constructedPartial', 'C08.selfRenounce',
                         'C08.badConfirmation', 'C08.noRole', 'C09.bridgeReceive', 'C09.distributorReceive', 'A03.userDirectSend',
                         'A03.fromWPROS', 'A10.reenter', 'B04.fundProfit', 'B06.finalize'):
            self.assertIn(required, ids)
        fund = next(r for r in doc['rows'] if r['id'] == 'B04.fundProfit')
        self.assertFalse(fund['privilegedCaller'])
        self._assert_record_free(doc)

    def _assert_record_free(self, doc):
        v = doc['verdicts']
        self.assertTrue(any('no approved E2E release record' in b for b in v['blockers']))
        self.assertTrue(any('upgrade route' in b for b in v['blockers']))
        self.assertEqual(doc['context']['postcheck']['verdict'], 'PASS')
        self.assertGreaterEqual(doc['context']['postcheck']['postChecks'], 20)
        rows = {r['id']: r for r in doc['rows']}
        self.assertEqual(rows['C04.proxies']['verdict'], 'NOT_RUN')
        self.assertEqual(rows['C02.rewardsVault']['verdict'], 'NOT_RUN')
        self.assertEqual(rows['A08.constructedPartial']['verdict'], 'PASS')
        self.assertEqual(rows['A08.realLiquidity']['verdict'], 'PASS')

    def test_mainnet_full(self):
        doc, out = _run('mainnet', name='test-e2e-mainnet')
        self._assert_full(doc, out)
        self.assertNotIn('B07', doc['verdicts']['mandatoryReal'])
        self._assert_record_free(doc)
        self.assertEqual(doc['context']['governance']['mode'], 'SAFE')
        self.assertEqual(doc['context']['governanceProblems'], [])


@unittest.skipUnless(ANVIL_READY, 'NOT RUN: run the canonical build and the CampaignAnvilFixture tests in this export first')
class NegativeControlTest(unittest.TestCase):
    """Each injected fault must make the harness refuse a PASS."""

    def _neg(self, name, scenarios, mutate=None, **kw):
        doc, _ = _run('testnet', name=f'test-e2e-neg-{name}', scenarios=scenarios, mutate=mutate, **kw)
        return doc

    def test_balance_mismatch_fails(self):
        def mutate(h):
            send = h.chain.send

            def leaky(label, sender, to, data, **kw):
                out = send(label, sender, to, data, **kw)
                if label.startswith('E2E.A05.withdrawCompleteToPROS()'):
                    h.chain.fund(sender, h.chain.balance(sender) + 1)      # an unexplained extra wei to the claimer
                return out
            h.chain.send = leaky
        doc = self._neg('balance', {'A05'}, mutate)
        row = next(r for r in doc['rows'] if r['id'] == 'A05.withdrawCompleteToPROS()')
        self.assertEqual(row['verdict'], 'FAIL')
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')

    def test_wrong_expected_error_fails(self):
        def mutate(h):
            h.entries['SlpDistributor.setKeeper(address)']['unauthorizedError'] = 'NotKeeper'
        doc = self._neg('error', {'C07'}, mutate)
        bad = [r for r in doc['rows'] if r.get('entry') == 'SlpDistributor.setKeeper(address)']
        self.assertTrue(bad and all(r['verdict'] == 'FAIL' for r in bad))
        self.assertEqual(doc['verdicts']['formal'], 'FAIL')

    def test_missing_entry_fails_closed(self):
        spec = copy.deepcopy(e2e.load_spec())
        spec['entries'] = [x for x in spec['entries'] if x['signature'] != 'forceParSettlement()']
        doc = self._neg('missing', {'B07'}, spec=spec)
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')
        self.assertEqual(doc['verdicts']['formal'], 'FAIL')
        self.assertTrue(any('missing from the spec' in p for p in doc['problems']))
        self.assertEqual(doc['rows'], [])                   # nothing runs on an inconsistent spec

    def test_unmapped_operation_fails(self):
        def mutate(h):
            del h.entries['YieldVault.forceParSettlement()']     # spec still consistent; the scenario's entry is unmapped
        doc = self._neg('unmapped', {'B07'}, mutate)
        self.assertTrue(any(r['verdict'] == 'FAIL' and 'not in the frozen spec' in r.get('detail', '') for r in doc['rows']))
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')

    def test_wrong_spec_selector_fails_closed(self):
        spec = copy.deepcopy(e2e.load_spec())
        next(x for x in spec['entries'] if x['contract'] == 'BridgeVault' and x['signature'] == 'unwrapWrapped()')['selector'] = '0xdeadbeef'
        doc = self._neg('specselector', {'C05'}, spec=spec)
        self.assertEqual(doc['verdicts']['formal'], 'FAIL')
        self.assertTrue(any('selector mismatch' in p for p in doc['problems']))

    def test_wrong_call_selector_fails(self):
        def mutate(h):
            calldata = h.calldata

            def wrong(contract, signature, *values):
                data = calldata(contract, signature, *values)
                return '0xdeadbeef' + data[10:] if signature == 'unwrapWrapped()' else data
            h.calldata = wrong
        doc = self._neg('callselector', {'C05'}, mutate)
        row = next(r for r in doc['rows'] if r['id'] == 'C05.unwrapWrapped')
        self.assertEqual(row['verdict'], 'FAIL')
        self.assertIn('selector', row['detail'])

    def test_snapshot_restore_failure_blocks(self):
        def mutate(h):
            h.chain.revert = lambda snapshot_id: None          # restoration silently skipped
        doc = self._neg('restore', {'A01', 'A04'}, mutate)
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')
        self.assertTrue(any(r['id'].endswith('.restore') and r['verdict'] == 'FAIL' for r in doc['rows']))
        self.assertTrue(any(r['verdict'] == 'BLOCKED' for r in doc['rows']))

    def test_state_change_during_expected_revert_fails(self):
        def mutate(h):
            send = h.chain.send

            def drift(label, sender, to, data, **kw):
                out = send(label, sender, to, data, **kw)
                if label == 'E2E.A01.zeroValue':
                    h.chain.fund(h.a['bridge'], h.chain.balance(h.a['bridge']) + 1)
                return out
            h.chain.send = drift
        doc = self._neg('drift', {'A01'}, mutate)
        row = next(r for r in doc['rows'] if r['id'] == 'A01.zeroValue')
        self.assertEqual(row['verdict'], 'FAIL')

    def test_real_claim_on_loopback_is_blocked(self):
        def mutate(h):
            h.ctx['evidence'] = e2e.REAL
        doc, out = _run('testnet', name='test-e2e-neg-realloopback', scenarios={'A05'}, mutate=mutate)
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())
        self.assertTrue((out / 'E2E-HARNESS-SELF-TEST.json').is_file())
        self.assertEqual(doc['rows'], [])
        self.assertTrue(any('loopback node' in p for p in doc['problems']))
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    def test_real_context_without_coverage_is_blocked(self):
        def mutate(h):
            h.chain.forked = True                         # simulate a Fork node: the gate logic, not the evidence, is tested
            write = h.write

            def write_and_reset(directory, name=None):
                try:
                    # a simulated Fork must never produce the official report name
                    return write(directory, name='E2E-NEGATIVE-CONTROL-SIMULATED-FORK')
                finally:
                    h.chain.forked = False                # the owning run must still close its loopback node
            h.write = write_and_reset
            h.ctx['evidence'] = e2e.REAL
            h.ctx['actorClasses'] = {k: e2e.REAL for k in e2e.ACTOR_KEYS}
            h.ctx['history']['authenticated'] = True
            h.ctx['formal'] = {'readAuthorization': True, 'ownerInputsApproved': True, 'safeManifestApproved': True}
        doc, out = _run('testnet', name='test-e2e-neg-realctx', scenarios={'A05'}, mutate=mutate)
        self.assertTrue((out / 'E2E-NEGATIVE-CONTROL-SIMULATED-FORK.json').is_file())
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS')
        self.assertEqual(v['formal'], 'BLOCKED')           # coverage incomplete, mandatory REAL scenarios missing
        self.assertTrue(any('without coverage' in b for b in v['blockers']))
        self.assertTrue(any('no approved E2E release record' in b for b in v['blockers']))

    def test_missing_context_is_blocked(self):
        def mutate(h):
            del h.ctx['history']
        doc = self._neg('noctx', {'A01'}, mutate)
        self.assertEqual(doc['rows'], [])
        self.assertTrue(any(p.startswith('BLOCKED context') for p in doc['problems']))
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    # -- formal inputs ------------------------------------------------------------------------------------
    def _with_record(self, name, scenarios, *, pin=True, spec=None, network='testnet', setup=None, manifests=frozenset(), **over):
        pins = set()
        directory = Path(tempfile.mkdtemp(prefix='e2e-record-', dir=os.environ.get('TMPDIR')))

        def mutate(h):
            _install_record(h, directory, pins, pin=pin, **{k: (v(h) if callable(v) else v) for k, v in over.items()})
        import campaign_input
        with mock.patch.object(ident, 'APPROVED_E2E_RECORDS', pins), \
                mock.patch.object(campaign_input, 'APPROVED_SAFE_IDENTITY_MANIFESTS', frozenset(manifests)):
            doc, _ = _run(network, name=f'test-e2e-neg-{name}', scenarios=scenarios, mutate=mutate, spec=spec, setup=setup)
        return doc

    def test_unpinned_record_is_blocked(self):
        doc = self._with_record('unpinned', {'A01'}, pin=False)
        self.assertTrue(any('not independently approved' in p for p in doc['context']['recordProblems']))
        self.assertTrue(any('no approved E2E release record' in b for b in doc['verdicts']['blockers']))
        self.assertEqual(set(doc['context']['actorClasses'].values()), {e2e.SYNTHETIC})
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    def test_forged_role_confirmation_is_blocked(self):
        forged = '0x' + '42' * 20
        doc = self._with_record('forgedroles', {'A01'},
                                actors=lambda h: {**{k: h.actors[k] for k in ident.ROLE_KEYS}, 'gov': forged, 'guardian': forged},
                                safes=lambda h: {r: {'address': forged if r in ('gov', 'guardian') else h.actors[r], 'owners': [forged],
                                                     'threshold': 1, 'safeManifestSha256': 'x'} for r in ident.SAFE_ROLES})
        problems = doc['problems']
        self.assertTrue(any('GOV is not the Timelock proposer' in p for p in problems))
        self.assertTrue(any('guardian() is not the approved GRD' in p for p in problems))
        self.assertTrue(any('Safe' in p and 'has no code' in p for p in problems))
        self.assertTrue(any('manifest is not approved' in p for p in problems) or any('no code' in p for p in problems))
        self.assertEqual(set(doc['context']['actorClasses'].values()), {e2e.SYNTHETIC})
        self.assertFalse(doc['context']['formal']['safeManifestApproved'])
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    def test_record_claiming_authenticated_history_is_blocked(self):
        doc = self._with_record('authclaim', {'A01'},
                                history=lambda h: {'authenticated': True, 's1ManifestSha256': (h.run.topo.get('binding') or {}).get('s1ManifestSha256')})
        hist = doc['context']['history']
        self.assertFalse(hist['authenticated'])
        self.assertTrue(any('FINAL_GATE_PROVENANCE_IMPLEMENTED' in r for r in hist['authenticationReasons']))
        self.assertTrue(any('FH-D2' in b for b in doc['verdicts']['blockers']))
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    def test_modified_spec_policy_with_pinned_record_fails(self):
        approved = e2e._sha(json.dumps(e2e.load_spec(), sort_keys=True).encode())
        spec = copy.deepcopy(e2e.load_spec())
        next(x for x in spec['entries'] if x['signature'] == 'fundProfit()')['policy'] = 'OWNER_ONLY'
        self.assertEqual(e2e.verify_spec(spec), [])           # the spec is self-consistent: only the binding sees it
        doc = self._with_record('specpolicy', {'A01'}, spec=spec, binding={'specSha256': approved})
        self.assertTrue(any(p.startswith('FAIL binding: E2E spec') for p in doc['problems']))
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')
        self.assertEqual(doc['verdicts']['formal'], 'FAIL')

    def test_skipped_post_checks_block(self):
        def strip(run, ctx):
            run.checks[:] = [c for c in run.checks if not c['id'].startswith('POST-')]
        doc, _ = _run('testnet', name='test-e2e-neg-nopost', pre=strip, scenarios={'A01'})
        self.assertEqual(doc['rows'], [])
        self.assertEqual(doc['context']['postcheck']['verdict'], 'MISSING')
        self.assertEqual(doc['context']['postcheck']['postChecks'], 0)
        self.assertTrue(any('postcheck MISSING' in p for p in doc['problems']))
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    def test_failed_upgrade_tx_blocks(self):
        def drop(run, ctx):
            run.chain.transcript[:] = [t for t in run.chain.transcript if t['label'] != 'U_t.executeBatch']
        doc, _ = _run('testnet', name='test-e2e-neg-noupgrade', pre=drop, scenarios={'A01'})
        self.assertEqual(doc['rows'], [])
        self.assertTrue(any('U_t.executeBatch not in the transcript' in w for w in doc['context']['postcheck']['why']))

    def test_missing_c04_on_simulated_real_is_blocked(self):
        doc, out = _run('testnet', name='test-e2e-neg-noc04', scenarios={'C04'}, mutate=_simulate_fork)
        row = next(r for r in doc['rows'] if r['id'] == 'C04.proxies')
        self.assertEqual(row['verdict'], 'BLOCKED')
        self.assertEqual(doc['verdicts']['operations']['C04'], 'BLOCKED')
        self.assertTrue(any("operations not PASS" in b and 'C04' in b for b in doc['verdicts']['blockers']))
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())

    def test_mainnet_stale_signers_are_blocked(self):
        def stale(run, ctx):
            run.m['safeOwners'] = list(run.m['safeOwners'])[1:] + ['0x' + '43' * 20]
        doc, _ = _run('mainnet', name='test-e2e-neg-stalesigners', pre=stale, scenarios={'A01'})
        self.assertTrue(any('stale signer' in g for g in doc['context']['governanceProblems']))
        self.assertTrue(any('governance: ' in b and 'stale' in b for b in doc['verdicts']['blockers']))

    @unittest.skipUnless(SAFE_READY, 'NOT RUN: needs the forge output of tests/fork/synthetic/SyntheticSafe.sol')
    def test_testnet_safe_owner_mismatch_is_blocked(self):
        state = {}

        def setup(h, run):
            state['safes'], state['states'] = reh.setup_safes(h, run)

        def safes(h):
            entries = reh.safe_entries(state['safes'], state['states'], 'm1')
            entries['ops'] = {**entries['ops'], 'owners': entries['ops']['owners'][1:] + ['0x' + '44' * 20]}
            return entries
        doc = self._with_record('safeowners', {'A01'}, setup=setup, safes=safes, manifests={'m1'})
        self.assertTrue(any('ops Safe owners on the node differ' in p for p in doc['problems']))
        self.assertEqual(set(doc['context']['actorClasses'].values()), {e2e.SYNTHETIC})
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')

    @unittest.skipUnless(INTEGRATIONS_READY, 'NOT RUN: set FAROO_E2E_SLP_RUNTIMES to the staged recorded runtimes')
    def test_rewards_vault_pointing_elsewhere_is_blocked(self):
        def setup(h, run):
            reh.setup_integrations(h, run, RUNTIMES, proxy_target=reh.address_of(h.ctx['chainId'], 'proxyChannel'))
        doc, _ = _run('testnet', name='test-e2e-neg-rvpointer', scenarios={'C02'}, setup=setup)
        rows = {r['id']: r for r in doc['rows']}
        self.assertEqual(rows['C02.rv.forwardTarget']['verdict'], 'BLOCKED')
        self.assertNotIn('C02.c.notify', rows)                # nothing is notified through a mis-wired vault
        self.assertEqual(doc['verdicts']['operations']['C02'], 'BLOCKED')

    @unittest.skipUnless(INTEGRATIONS_READY, 'NOT RUN: set FAROO_E2E_SLP_RUNTIMES to the staged recorded runtimes')
    def test_rewards_forwarding_leak_fails(self):
        def setup(h, run):
            reh.setup_integrations(h, run, RUNTIMES)

        def mutate(h):
            send = h.chain.send
            target = h.ctx['integrations']['rewardsTarget']

            def leak(label, sender, to, data, **kw):
                out = send(label, sender, to, data, **kw)
                if label == 'E2E.C02.c.notify':           # the proxy ends one wei short of the notified amount
                    h.chain.fund(target, h.chain.balance(target) - 1)
                return out
            h.chain.send = leak
        doc, _ = _run('testnet', name='test-e2e-neg-rvleak', scenarios={'C02'}, setup=setup, mutate=mutate)
        row = next(r for r in doc['rows'] if r['id'] == 'C02.c.notify')
        self.assertEqual(row['verdict'], 'FAIL')
        self.assertIn('forwarded to the configured proxy', row['detail'])
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')


def _simulate_fork(h):
    """Gate logic under a simulated Fork node (never real evidence): REAL context, never the official name."""
    h.chain.forked = True
    write = h.write

    def write_and_reset(directory, name=None):
        try:
            return write(directory, name='E2E-NEGATIVE-CONTROL-SIMULATED-FORK')
        finally:
            h.chain.forked = False
    h.write = write_and_reset
    h.ctx['evidence'] = e2e.REAL


@unittest.skipUnless(ANVIL_READY, 'NOT RUN: run the canonical build and the CampaignAnvilFixture tests in this export first')
class InterruptionTest(unittest.TestCase):
    """An RPC, node or snapshot failure mid-E2E: completed cases keep their evidence, the interrupted case is FAIL, later
    cases are BLOCKED, the document is marked PARTIAL, and no complete-report name is written."""

    def _run(self, name, mutate, scenarios=('A01', 'A04', 'A05')):
        import execution
        out = execution.OUT / name / 'e2e'
        if out.is_dir():
            import shutil
            shutil.rmtree(out)
        doc = {}
        r = execution.testnet('normal', name=name, then=e2e.hook(out, result=doc, scenarios=set(scenarios), mutate=mutate))
        return doc, out, {c['id']: c for c in r['checks'] if c['id'].startswith('E2E.')}

    def _assert_partial(self, doc, out, checks, reason, completed_min=1):
        self.assertEqual(doc['completeness']['status'], 'PARTIAL')
        self.assertTrue(any(reason in x for x in doc['completeness']['reasons']), doc['completeness'])
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'FAIL')
        self.assertNotEqual(v['formal'], 'PASS')
        self.assertEqual(checks['E2E.execution']['verdict'], 'FAIL')
        self.assertEqual(checks['E2E.completeness']['verdict'], 'FAIL')
        passed = [r for r in doc['rows'] if r['verdict'] == 'PASS' and r.get('transactionHash')]
        self.assertGreaterEqual(len(passed), completed_min)                    # completed cases keep their evidence
        self.assertTrue(all(r.get('deltas') is not None and 'effects' in r for r in passed))
        self.assertTrue((out / 'E2E-HARNESS-SELF-TEST-PARTIAL.json').is_file())
        self.assertFalse((out / 'E2E-HARNESS-SELF-TEST.json').exists())       # never the name of a complete report
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())
        self.assertIn('PARTIAL / INCOMPLETE', (out / 'E2E-HARNESS-SELF-TEST-PARTIAL.md').read_text())

    def _after_first_case(self, h, attr, fail):
        """Patch ``h.<attr>`` so that it raises once the first case has started its restore."""
        real = getattr(h, attr)
        state = {'cases': 0}
        case = h.case

        def counting_case(*a, **kw):
            state['cases'] += 1
            return case(*a, **kw)
        h.case = counting_case

        def patched(*a, **kw):
            if state['cases'] >= 1 and h.case_state is None and fail(a, kw):
                raise ValueError("{'code': -32603, 'message': 'failed to get account: error sending request (simulated)'}")
            return real(*a, **kw)
        setattr(h, attr, patched)

    def _assert_blocked_after(self, doc, step):
        restore = [r for r in doc['rows'] if r['id'].endswith('.restore')]
        self.assertEqual(len(restore), 1)
        self.assertEqual(restore[0]['verdict'], 'FAIL')
        self.assertIn(f'snapshot restore ({step}) raised ValueError', restore[0]['detail'])
        after = doc['rows'][doc['rows'].index(restore[0]) + 1:]
        self.assertTrue(any(r['verdict'] == 'BLOCKED' and 'case not run' in r['detail'] for r in after))
        self.assertFalse([r['id'] for r in after if r['verdict'] == 'PASS'])   # nothing after the break can PASS

    def test_snapshot_revert_raises(self):
        def mutate(h):
            real = h.chain.revert
            calls = {'n': 0}

            def revert(snapshot_id):
                calls['n'] += 1
                if calls['n'] == 2:
                    raise ValueError('snapshot revert failed (simulated)')
                return real(snapshot_id)
            h.chain.revert = revert
        doc, out, checks = self._run('test-e2e-int-revert', mutate)
        self._assert_partial(doc, out, checks, 'S_post not restorable')
        self._assert_blocked_after(doc, 'revert')

    def test_fingerprint_read_interrupted(self):
        doc, out, checks = self._run('test-e2e-int-fingerprint', lambda h: self._after_first_case(h, 'fingerprint', lambda a, kw: True))
        self._assert_partial(doc, out, checks, 'S_post not restorable')
        self._assert_blocked_after(doc, 'fingerprint')

    def test_balance_read_interrupted_in_restore(self):
        doc, out, checks = self._run('test-e2e-int-balances',
                                     lambda h: self._after_first_case(h, 'balances', lambda a, kw: a and a[0] is h.fresh))
        self._assert_partial(doc, out, checks, 'S_post not restorable')
        self._assert_blocked_after(doc, 'fresh accounts')

    def _abort_on_case(self, h, number):
        case = h.case
        state = {'n': 0}

        def failing_case(*a, **kw):
            state['n'] += 1
            if state['n'] == number:
                raise ValueError('upstream TLS EOF between cases (simulated)')
            return case(*a, **kw)
        h.case = failing_case

    def test_abort_after_completed_cases(self):
        doc, out, checks = self._run('test-e2e-int-abort', lambda h: self._abort_on_case(h, 4))
        self._assert_partial(doc, out, checks, 'harness aborted: scenarios: ValueError', completed_min=3)
        self.assertEqual(len(doc['cases']), 3)                                 # exactly the cases that ran
        self.assertTrue(all(c.get('exception') is None for c in doc['cases']))

    def test_abort_on_simulated_fork_writes_no_official_report(self):
        def mutate(h):
            self._abort_on_case(h, 3)
            h.chain.forked = True
            h.ctx['evidence'] = e2e.REAL
            write = h.write

            def write_and_reset(directory, name=None):
                try:
                    return write(directory)
                finally:
                    h.chain.forked = False
            h.write = write_and_reset
        doc, out, checks = self._run('test-e2e-int-realabort', mutate)
        self.assertEqual(doc['completeness']['status'], 'PARTIAL')
        self.assertFalse((out / 'POST-UPGRADE-USER-E2E.json').exists())
        self.assertTrue((out / 'POST-UPGRADE-USER-E2E-PARTIAL.json').is_file())
        self.assertNotEqual(doc['verdicts']['formal'], 'PASS')
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')

    def test_report_write_failure_is_recorded_separately(self):
        def mutate(h):
            self._abort_on_case(h, 3)

            def failing_write(directory, name=None):
                raise OSError('disk full (simulated)')
            h.write = failing_write
        doc, out, checks = self._run('test-e2e-int-write', mutate)
        self.assertEqual(checks['E2E.evidenceWrite']['verdict'], 'FAIL')
        self.assertIn('OSError: disk full', checks['E2E.evidenceWrite']['detail'])
        self.assertIn('upstream TLS EOF', checks['E2E.completeness']['detail'])  # the original failure is not overwritten
        emergency = json.loads((out / 'E2E-EVIDENCE-WRITE-FAILED.json').read_text())
        self.assertIn('upstream TLS EOF', emergency['aborted'])
        self.assertIn('disk full', emergency['writeError'])
        self.assertTrue(any(r['verdict'] == 'PASS' for r in emergency['rows']))   # completed rows survive
        self.assertEqual(emergency['completeness']['status'], 'PARTIAL')
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')

    def test_normal_path_unchanged(self):
        doc, out, checks = self._run('test-e2e-int-normal', None, scenarios=('A01',))
        self.assertNotIn('completeness', doc)
        self.assertTrue((out / 'E2E-HARNESS-SELF-TEST.json').is_file())
        self.assertFalse(list(out.glob('*PARTIAL*')))
        self.assertNotIn('E2E.completeness', checks)
        self.assertEqual(doc['verdicts']['execution'], 'PASS')


@unittest.skipUnless(SAFE_READY, 'NOT RUN: needs the forge output of tests/fork/synthetic/SyntheticSafe.sol')
class RehearsalTest(unittest.TestCase):
    """Positive rehearsals of the real-state inputs on synthetic nodes (formal verdict stays BLOCKED)."""

    def test_testnet_safe_route(self):
        doc, _ = _run('testnet', name='test-e2e-reh-safes', scenarios={'C06', 'B05', 'B01', 'C08', 'D'},
                      setup=lambda h, run: reh.setup_safes(h, run))
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        self.assertEqual(v['formal'], 'BLOCKED')
        self.assertTrue(any('rehearsal setup applied' in b for b in v['blockers']))
        self.assertTrue(any('synthetic GOV_t EOA' in b for b in v['blockers']))
        ctx = doc['context']
        self.assertEqual(ctx['governance']['mode'], 'SAFE')
        self.assertEqual(len(ctx['safes']), len(ident.SAFE_ROLES))
        by_safe = [r for r in doc['rows'] if r.get('route') == 'Safe approved-hash']
        classes = {r['callerClass'] for r in by_safe}
        self.assertTrue({'actor:guardian', 'actor:ops'} <= classes, classes)
        self.assertTrue(any(r['verdict'] == 'PASS' and r['expected'].startswith('revert') for r in by_safe))
        tl_rows = [r for r in doc['rows'] if r.get('caller') == ctx['addresses']['timelock'] and r['verdict'] == 'PASS']
        self.assertTrue(tl_rows)                               # Timelock operations scheduled and executed by the GOV Safe

    def test_pinned_record_verified_on_rehearsal(self):
        state, pins = {}, set()
        directory = Path(tempfile.mkdtemp(prefix='e2e-record-', dir=os.environ.get('TMPDIR')))

        def setup(h, run):
            safes, states = reh.setup_safes(h, run)
            raw = h.chain.call(h.a['distributor'], 'getSlps()')
            _install_record(h, directory, pins, safes=reh.safe_entries(safes, states, 'm1'),
                            actors={k: h.actors[k] for k in ident.ROLE_KEYS},
                            ownerInputs={'destinations': [x.lower() for x in e2e.abi.decode_parameters([{'type': 'address[]'}], raw)[0]],
                                         'commission': {'decision': 'K2', 'account': safes['treasury'],
                                                        'ratePpm': h.u(h.a['oracle'], 'commissionRatePpm()')}})
            state['ok'] = True
        import campaign_input
        with mock.patch.object(ident, 'APPROVED_E2E_RECORDS', pins), \
                mock.patch.object(campaign_input, 'APPROVED_SAFE_IDENTITY_MANIFESTS', frozenset({'m1'})):
            doc, _ = _run('testnet', name='test-e2e-reh-record', scenarios={'C06'}, setup=setup)
        ctx = doc['context']
        self.assertTrue(state.get('ok'))
        self.assertEqual([p for p in doc['problems'] if 'identity' in p or 'binding' in p], [])
        self.assertEqual({k for k, c in ctx['actorClasses'].items() if c == e2e.REAL}, set(ident.ROLE_KEYS))
        self.assertTrue(ctx['formal']['safeManifestApproved'])
        self.assertEqual(ctx['governance']['source'], 'approved record, Fork-verified')
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        self.assertEqual(v['formal'], 'BLOCKED')               # still synthetic, unauthenticated, EOA upgrade route
        for reason in ('SYNTHETIC context', 'FH-D2', 'rehearsal setup applied', 'upgrade route'):
            self.assertTrue(any(reason in b for b in v['blockers']), reason)
        self.assertFalse(any('no approved E2E release record' in b for b in v['blockers']))

    @unittest.skipUnless(INTEGRATIONS_READY, 'NOT RUN: set FAROO_E2E_SLP_RUNTIMES to the staged recorded runtimes')
    def test_integrations_rehearsal(self):
        doc, _ = _run('testnet', name='test-e2e-reh-integrations', scenarios={'C01', 'C02', 'C04'},
                      setup=lambda h, run: reh.setup_integrations(h, run, RUNTIMES))
        v = doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        rows = {r['id']: r for r in doc['rows']}
        for rid in ('C02.rv.isReporter', 'C02.rv.callerAuthorized', 'C02.rv.forwardTarget', 'C02.real.notify', 'C02.c.notify',
                    'C02.c.aboveAllowance', 'C02.c.unauthorized', 'C02.c.tooFrequent'):
            self.assertEqual(rows[rid]['verdict'], 'PASS', rid)
        notify = rows['C02.c.notify']
        self.assertTrue(all(e['ok'] for e in notify['effects']), notify['effects'])
        self.assertEqual(notify['via'], doc['context']['integrations']['rewardsVault'])
        self.assertEqual(rows['C02.real.notify']['expected'], 'revert ZeroUpdateAmount')
        proxies = doc['context']['integrations']['stakingProxies']
        for p in proxies:
            for suffix in ('bridgeVault', 'rewardsVault', 'whitelisted', 'operator'):
                self.assertEqual(rows[f'C04.{p[:10]}.{suffix}']['verdict'], 'PASS')
            for suffix in ('deposit', 'distribute', 'transferBack', 'transferBack.unauthorized', 'transferBack.aboveBalance'):
                self.assertEqual(rows[f'C04.{p[:10]}.{suffix}']['verdict'], 'PASS', suffix)
        self.assertEqual(v['operations']['C04'], 'PASS')


if __name__ == '__main__':
    unittest.main()
