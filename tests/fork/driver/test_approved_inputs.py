"""Regression for APPROVED_RECORD mode (``approved_inputs``, ``execution.testnet(governance=APPROVED_RECORD)``). Test-only.

A **synthetic fixture stands in for the owner's approval**:
- it deploys local Safe models for GOV, CAN, GRD, OPS and Treasury, and whitelists two SLP destinations with code
  on the V3 SlpDistributor (as E1, the pre-upgrade owner);
- it writes a release record from that node state, and pins the record and the Safe manifests **in-process only**
  (``mock.patch``).

None of this is an approval, a real Safe or real-state Fork evidence. Every run here is SYNTHETIC, and the formal E2E
verdict stays BLOCKED.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import approved_inputs as ai  # noqa: E402
import safe_governance as sg  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-testnet-v3.json'
READY = CANONICAL_READY and TOPOLOGY.is_file() and (sg.SAFE_OUT / 'SyntheticSafeProxy.json').is_file()
SKIP = 'NOT RUN: needs the canonical build, the CampaignAnvilFixture topologies and the SyntheticSafe forge output'
FIXTURE = 'SYNTHETIC FIXTURE standing in for an owner approval (test only; never an approval)'


def _addr(label):
    return '0x' + keccak256(label.encode())[-40:]


DESTINATIONS = [_addr('fixture destination one'), _addr('fixture destination two')]


def _state(run):
    """Synthetic stand-in for the approved on-chain state: Safes for every Safe role, and an ordered SLP list."""
    chain, m = run.chain, run.m
    run.fixture_safes = sg.deploy_model(chain, 'testnet-approved-fixture', ai.SAFE_ROLES)
    for d in DESTINATIONS:
        chain.rpc('anvil_setCode', [d, '0x00'])
    data = abi.encode_function({'type': 'function', 'name': 'setSlps', 'inputs': [{'type': 'address[]'}, {'type': 'bool'}]},
                               [DESTINATIONS, True])
    _, rec = chain.send('fixture.setSlps', m['e1'], m['distributor'], data)
    assert rec['status'] == 1, 'fixture setSlps failed'


def _record(run):
    """The record an owner would approve for this node's fixture state, at its anchor."""
    import e2e
    import e2e_identity
    chain, m = run.chain, run.m
    dests, digest = ai.destinations_now(chain, m['distributor'])
    safes = {r: {**s, 'safeManifestSha256': f'fixture-manifest-{r}'} for r, s in run.fixture_safes.items()}
    spec = e2e.load_spec()
    return {
        'schema': e2e_identity.RECORD_SCHEMA, 'chainId': m['chainId'], 'anchor': run._head(),
        'binding': {'frozenCommit': e2e_identity.FROZEN_COMMIT, 'specSha256': e2e._sha(json.dumps(spec, sort_keys=True).encode()),
                    'inventorySha256': spec['derivedFrom']['sha256'], 'artifactSha256': e2e.artifact_entries()[1]},
        'actors': {**{r: s['address'] for r, s in safes.items()}, 'deployer': _addr('fixture approved deployer'),
                   'reporter': chain.addr(m['oracle'], 'slp()'), 'keeper': chain.addr(m['distributor'], 'keeper()')},
        'safes': safes,
        'history': {'s1ManifestSha256': (run.topo.get('binding') or {}).get('s1ManifestSha256')},
        'readAuthorization': {'specSha256': None},
        'ownerInputs': {
            'approvalReference': FIXTURE,
            'approvals': {i: FIXTURE for i in ai.BUILD_ITEMS + ai.GATE_ITEMS},
            'destinations': dests, 'destinationsHash': digest,
            'policy': {'expectedGrossAprPpm': 50_000, 'maxAprPpm': 100_000,
                       'maxUpdateAmount': str(chain.uint(m['oracle'], 'maxUpdateAmount()')),
                       'updateInterval': max(chain.uint(m['oracle'], 'updateInterval()'), 3600)},
            'commission': {'decision': 'K1', 'account': chain.addr(m['oracle'], 'commissionAccount()'),
                           'ratePpm': chain.uint(m['oracle'], 'commissionRatePpm()'), 'treasuryGateException': FIXTURE},
            'gas': {'estimator': 'a', 'confirmedGasCap': 6_000_000}},
        'integrations': {}}


class Approval:
    """SYNTHETIC stand-in for the owner's approval of the fixture state: ``prepare`` builds the fixture state, writes
    the record for it (``mutate`` injects a fault), and pins the record and its Safe manifests in-process."""

    def __init__(self, directory, *, mutate=None, pin=True, manifests=True):
        import campaign_input
        import e2e_identity
        self.directory, self.mutate, self.pin, self.manifests = Path(directory), mutate, pin, manifests
        self.governance = {'source': 'APPROVED_RECORD', 'record': None}
        self.pins, self.manifest_pins = set(), set()
        self.patches = [mock.patch.object(e2e_identity, 'APPROVED_E2E_RECORDS', self.pins),
                        mock.patch.object(campaign_input, 'APPROVED_SAFE_IDENTITY_MANIFESTS', self.manifest_pins)]
        self.record = None

    def prepare(self, run):
        _state(run)
        record = _record(run)
        if self.mutate:
            self.mutate(record)
        raw = json.dumps(record, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        path = self.directory / f'record-{sha[:12]}.json'
        path.write_bytes(raw)
        if self.pin:
            self.pins.add(sha)
        if self.manifests:
            self.manifest_pins.update(s['safeManifestSha256'] for s in record['safes'].values())
        self.record, self.governance['record'] = record, (path, sha)

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


def _v(result):
    return {c['id']: c['verdict'] for c in result['checks']}


@unittest.skipUnless(READY, SKIP)
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import execution
        cls.ex = execution
        cls.tmp = tempfile.TemporaryDirectory(dir=str(execution.OUT.parent))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_with(self, name, *, mutate=None, then=None, pin=True, manifests=True, patch_build=None):
        with Approval(self.tmp.name, mutate=mutate, pin=pin, manifests=manifests) as ap:
            ctx = mock.patch.object(self.ex.rc, 'build', patch_build) if patch_build else None
            if ctx:
                ctx.start()
            try:
                r = self.ex.testnet('normal', name=name, governance=ap.governance, prepare=ap.prepare, then=then)
            finally:
                if ctx:
                    ctx.stop()
        return r, ap

    def stopped_before_t1(self, r):
        labels = [t['label'] for t in r['transcript']]
        self.assertFalse([l for l in labels if l.startswith(('T-', 'U_t'))], 'nothing after the refusal may run')
        self.assertNotEqual(r['ledgers']['FS-2.normal']['aggregate'], 'PASS')


class ApprovedRecordRunTest(Base):
    """The record drives every role and parameter; the upgraded state and the E2E use the same record."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import e2e
        import governance
        cls.out = cls.ex.OUT / 'test-approved-run' / 'e2e'
        if cls.out.is_dir():
            import shutil
            shutil.rmtree(cls.out)
        cls.doc = {}
        hook = e2e.hook(cls.out, result=cls.doc, scenarios={'A01', 'A05', 'C06'})
        with Approval(cls.tmp.name) as ap:
            cls.r = cls.ex.testnet('normal', name='test-approved-run', prepare=ap.prepare, governance=ap.governance,
                                   then=e2e.compose(governance.authority_only, hook))
        cls.record, cls.sha = ap.record, ap.governance['record'][1]
        cls.v = _v(cls.r)

    def test_inputs_checked_before_t0(self):
        inputs = {i: x for i, x in self.v.items() if i.startswith('INPUT.') and not i.startswith('INPUT.post')}
        self.assertEqual({i: x for i, x in inputs.items() if x != 'PASS'}, {})
        for rid in ('INPUT.record.anchor==fork', 'INPUT.record.sourceBinding', 'INPUT.safes.manifestsApproved',
                    'INPUT.safes.signerPolicy', 'INPUT.keeper==fork', 'INPUT.reporter==fork', 'INPUT.destinations.ordered==fork',
                    'INPUT.destinations.hash==fork', 'INPUT.policy.rule', 'INPUT.policy.maxUpdateAmount==fork',
                    'INPUT.policy.updateInterval==max(fork,1h)', 'INPUT.commission.K1.fork==approvedAccount',
                    'INPUT.commission.K1.treasuryGate', 'INPUT.gasCap.approved', 'INPUT.config==record',
                    *(f'INPUT.safes.{r}.identity==fork' for r in ai.SAFE_ROLES)):
            self.assertIn(rid, inputs)
        self.assertEqual(self.record['ownerInputs']['destinations'], DESTINATIONS)       # a real, ordered, non-empty list

    def test_roles_and_parameters_come_from_the_record(self):
        act = self.record['actors']
        p0_creations = [t for t in self.r['transcript'] if t['label'].startswith('P-0 ') and t.get('contractAddress')]
        self.assertTrue(p0_creations)
        self.assertEqual({t['from'] for t in p0_creations}, {act['deployer']})          # the approved deployer, not a label
        for rid in ('INPUT.post.maxAprPpm', 'INPUT.post.maxUpdateAmount', 'INPUT.post.updateInterval', 'INPUT.post.keeper',
                    'INPUT.post.reporter', 'INPUT.post.destinations.ordered', 'INPUT.post.commissionUnchanged',
                    'INPUT.post.timelock.GOV', 'INPUT.post.timelock.CAN', 'INPUT.post.ops.governedVault',
                    *(f'INPUT.post.guardian.{k}' for k in ('oracle', 'stpros', 'bridge', 'distributor', 'factory'))):
            self.assertEqual(self.v.get(rid), 'PASS', rid)
        self.assertEqual(self.r['ledgers']['FS-2.normal']['aggregate'], 'PASS')
        stage = next(s for s in json.loads((self.ex.OUT / 'test-approved-run' / 'run.json').read_text())['stages']
                     if s['stage'] == 'releaseInputs')
        self.assertEqual((stage['source'], stage['recordSha256'], stage['policy']['maxAprPpm']), ('APPROVED_RECORD', self.sha, 100_000))

    def test_gas_gates_stay_blocked_off_real_state(self):
        self.assertEqual(self.v.get('FS-6.U_t.testnetCapResponse'), 'BLOCKED')     # an approved cap counts only on real gas
        self.assertEqual(self.v.get('FS-6.U_t.U_t.execTransaction.receipt<=2.2M'), 'FAIL')
        self.assertNotIn('FS-6.U_t.bindingEstimator.approvedA', self.v)

    def test_e2e_uses_the_same_record(self):
        ctx = self.doc['context']
        self.assertEqual(ctx['recordSha256'], self.sha)                                # inherited from the run
        self.assertEqual([p for p in self.doc['problems'] if 'identity' in p or 'binding' in p or 'record' in p.lower()], [])
        self.assertEqual({k for k, c in ctx['actorClasses'].items() if c == 'REAL'},
                         {'gov', 'can', 'guardian', 'ops', 'keeper', 'reporter', 'treasury'})
        self.assertTrue(ctx['formal']['ownerInputsApproved'])
        self.assertTrue(ctx['formal']['safeManifestApproved'])
        self.assertTrue(ctx['upgradeRoute']['approved'])
        v = self.doc['verdicts']
        self.assertEqual(v['execution'], 'PASS', v['failures'])
        self.assertEqual(v['formal'], 'BLOCKED')                                         # SYNTHETIC node, FH-D2, no TD-12
        for reason in ('SYNTHETIC context', 'FH-D2', 'read authorization'):
            self.assertTrue(any(reason in b for b in v['blockers']), reason)
        self.assertFalse(any('release inputs SYNTHETIC' in b for b in v['blockers']))
        self.assertFalse((self.out / 'POST-UPGRADE-USER-E2E.json').exists())


def _set(path, value):
    def mutate(record):
        target = record
        keys = path.split('.')
        for k in keys[:-1]:
            target = target[k]
        if value is DELETE:
            del target[keys[-1]]
        else:
            target[keys[-1]] = value(record) if callable(value) else value
    return mutate


DELETE = object()


class RefusedBeforeT0Test(Base):
    """Every build-input fault stops the run before P-0: exactly the injected fault refuses the record."""

    def _stop(self, name, rid, verdict='FAIL', also=(), mutate=None, **kw):
        called = []
        r, _ = self.run_with(name, mutate=mutate, then=lambda run, ctx: called.append(1) or [], **kw)
        v = _v(r)
        self.assertEqual(v.get(rid), verdict, (rid, sorted(i for i, x in v.items() if x != 'PASS')[:8]))
        self.assertEqual(v.get('INPUT.notUsable'), 'BLOCKED')
        self.assertEqual({i for i, x in v.items() if x != 'PASS' and not i.startswith('FS-6.')}, {rid, 'INPUT.notUsable', *also})
        self.assertFalse([t for t in r['transcript'] if t['label'].startswith('P-0')])
        self.stopped_before_t1(r)
        self.assertEqual(called, [])

    def test_unpinned_record(self):
        self._stop('test-approved-unpinned', 'INPUT.record.approved', 'BLOCKED', pin=False)

    def test_unpinned_manifests(self):
        self._stop('test-approved-manifest', 'INPUT.safes.manifestsApproved', 'BLOCKED', manifests=False)

    def test_other_anchor(self):
        self._stop('test-approved-anchor', 'INPUT.record.anchor==fork', mutate=_set('anchor', {'block': 1, 'hash': '0x' + '00' * 32}))

    def test_other_source(self):
        self._stop('test-approved-source', 'INPUT.record.sourceBinding', mutate=_set('binding.frozenCommit', '0' * 40))

    def test_other_chain(self):
        self._stop('test-approved-chain', 'INPUT.record.chainId', mutate=_set('chainId', 1672))

    def test_deployer_is_a_gov_signer(self):
        self._stop('test-approved-deployer', 'INPUT.safes.signerPolicy',
                   mutate=_set('actors.deployer', lambda rec: rec['safes']['gov']['owners'][0]))

    def test_keeper_differs(self):
        self._stop('test-approved-keeper', 'INPUT.keeper==fork', mutate=_set('actors.keeper', _addr('other keeper')))

    def test_reporter_differs(self):
        self._stop('test-approved-reporter', 'INPUT.reporter==fork', mutate=_set('actors.reporter', _addr('other reporter')))

    def test_destinations_order_differs(self):
        self._stop('test-approved-order', 'INPUT.destinations.ordered==fork',
                   mutate=_set('ownerInputs.destinations', lambda rec: list(reversed(rec['ownerInputs']['destinations']))))

    def test_apr_not_approved(self):
        self._stop('test-approved-apr', 'INPUT.policy.approved', 'BLOCKED', mutate=_set('ownerInputs.approvals.policy', DELETE))

    def test_apr_above_ceiling(self):
        self._stop('test-approved-apr-rule', 'INPUT.policy.rule',
                   mutate=_set('ownerInputs.policy', {'expectedGrossAprPpm': 300_000, 'maxAprPpm': 600_000,
                                                      'maxUpdateAmount': '1', 'updateInterval': 3600}))

    def test_eoa_for_a_safe_role(self):
        def mutate(rec):
            rec['actors']['guardian'] = _addr('guardian eoa')
            del rec['safes']['guardian']
        self._stop('test-approved-eoa', 'INPUT.safes.guardian.present', also=('INPUT.safes.manifestsApproved',), mutate=mutate)

    def test_gov_threshold_differs(self):
        self._stop('test-approved-threshold', 'INPUT.safes.gov.identity==fork', also=('INPUT.safes.signerPolicy',),
                   mutate=_set('safes.gov.threshold', 2))


class GateAndMismatchTest(Base):
    """Gates that stay BLOCKED while the upgrade runs, a tampered config, a different E2E record, and roles that differ
    from the record on the upgraded state: none of them can end in a formal PASS."""

    def test_commission_not_approved_stays_blocked(self):
        r, _ = self.run_with('test-approved-commission', mutate=_set('ownerInputs.approvals.commission', DELETE))
        v = _v(r)
        self.assertEqual(v.get('INPUT.commission.approved'), 'BLOCKED')
        self.assertEqual(v.get('INPUT.post.commissionUnchanged'), 'PASS')               # never changed by the driver
        self.assertEqual(r['ledgers']['FS-2.normal']['aggregate'], 'BLOCKED')

    def test_k1_without_ruling_stays_blocked(self):
        r, _ = self.run_with('test-approved-k1', mutate=_set('ownerInputs.commission.treasuryGateException', DELETE))
        self.assertEqual(_v(r).get('INPUT.commission.K1.treasuryGate'), 'BLOCKED')

    def test_k2_without_on_chain_change_stays_blocked(self):
        def mutate(rec):
            rec['ownerInputs']['commission'] = {'decision': 'K2', 'account': rec['actors']['treasury'], 'ratePpm': 100000}
        r, _ = self.run_with('test-approved-k2', mutate=mutate)
        v = _v(r)
        self.assertEqual(v.get('INPUT.commission.K2.fork==treasurySafe'), 'FAIL')     # the driver does not change it
        self.assertEqual(v.get('INPUT.post.commissionUnchanged'), 'PASS')

    def test_config_guardian_differs_from_record(self):
        import release_config as rc
        real = rc.build

        def tampered(*a, **kw):
            config, inv = real(*a, **kw)
            config = {**config, 'actors': {**config['actors'], 'guardian': _addr('migrated guardian')}}
            return config, inv
        r, _ = self.run_with('test-approved-config', patch_build=tampered)
        self.assertEqual(_v(r).get('INPUT.config==record'), 'FAIL')
        self.stopped_before_t1(r)

    def _drift(self, name, drift, *, after_s_post=False):
        """``drift(run, timelock)`` changes the upgraded state either before S_post is recorded (the state the E2E must
        verify, as a Fork whose roles differ from the record) or after it (the E2E must refuse to start)."""
        import e2e
        doc = {}
        hook = e2e.hook(self.ex.OUT / name / 'e2e', result=doc, scenarios={'A01'})
        real_post_hook = self.ex._post_hook

        def post_hook(run, p0, actors, guarded):
            if not after_s_post:
                drift(run, p0['timelock'])
            return real_post_hook(run, p0, actors, guarded)

        def then(run, ctx):
            if after_s_post:
                drift(run, ctx['p0']['timelock'])
            return hook(run, ctx)
        with mock.patch.object(self.ex, '_post_hook', post_hook):
            r, ap = self.run_with(name, then=then)
        return r, doc

    def _as_timelock(self, run, tl, target, data):
        _, rec = run.chain.send('drift', tl, target, data)
        self.assertEqual(rec['status'], 1)

    def _guardian_drift(self, run, tl):
        self._as_timelock(run, tl, run.m['stpros'], abi.calldata('setGuardian(address)', _addr('drifted guardian')))

    def test_guardian_differs_from_record_blocks_e2e(self):
        r, doc = self._drift('test-approved-grd-drift', self._guardian_drift)
        self.assertTrue(any('stpros.guardian() is not the approved GRD' in p for p in doc['problems']), doc['problems'][:4])
        self.assertNotEqual(doc['verdicts']['formal'], 'PASS')
        self.assertNotIn('REAL', set(doc['context']['actorClasses'].values()))
        self.assertFalse(doc['context']['formal']['safeManifestApproved'])

    def test_ops_differs_from_record_blocks_e2e(self):
        operator = '0x' + keccak256(b'OPERATOR_ROLE')[2:]

        def drift(run, tl):
            v, ops = run.m['vaults'][14], run.approved['inputs']['actors']['ops']
            self._as_timelock(run, tl, v, abi.calldata('revokeRole(bytes32,address)', operator, ops))
            self._as_timelock(run, tl, v, abi.calldata('grantRole(bytes32,address)', operator, _addr('other ops')))
        r, doc = self._drift('test-approved-ops-drift', drift)
        self.assertTrue(any('OPS lacks OPERATOR_ROLE' in p for p in doc['problems']), doc['problems'][:4])
        self.assertNotEqual(doc['verdicts']['formal'], 'PASS')
        self.assertNotIn('REAL', set(doc['context']['actorClasses'].values()))

    def test_state_moved_after_s_post_blocks_e2e(self):
        r, doc = self._drift('test-approved-moved', self._guardian_drift, after_s_post=True)
        self.assertEqual(doc['rows'], [])
        self.assertTrue(any('would not start from S_post' in p for p in doc['problems']))
        self.assertEqual(doc['verdicts']['execution'], 'FAIL')

    def test_e2e_with_another_record_is_blocked(self):
        import e2e
        other = Path(self.tmp.name) / 'another-record.json'
        other.write_text(json.dumps({'schema': 'faroo-e2e-release-record-v1', 'note': 'another record'}))
        doc = {}
        r, _ = self.run_with('test-approved-otherrecord',
                             then=e2e.hook(self.ex.OUT / 'test-approved-otherrecord' / 'e2e', result=doc,
                                           record=(other, hashlib.sha256(other.read_bytes()).hexdigest()), scenarios={'A01'}))
        self.assertEqual(doc['rows'], [])
        self.assertTrue(any('is not the record' in p and 'drove the upgrade' in p for p in doc['problems']))
        self.assertEqual(doc['verdicts']['formal'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
