"""The V4 Fork runner (``v4_fork``) on a SYNTHETIC loopback state. Test-only (class C); never a public endpoint.

Before the anchor, the recorded mainnet fixture receives the SlpDistributor pre-deployment, P-0 and the
ReleaseAssertions, exactly as on mainnet (the runner must find them and deploy nothing). A two-segment archive
(legacy targets from block 1; every target after the pin) is built from the node's own logs in the B0 plan/chunk
schema. The runner then does H -> H checkpoint -> 600 s -> U -> R4 postcheck with the release tool's own checkpoints.

SYNTHETIC adaptations, listed so they are never read as real-state evidence:
- the fixture state is loaded, not created: the legacy targets have no creation history, so the archive starts at
  block 1 and the fixture rows (block 1) carry the node's block-1 hash;
- the real SLP addresses receive 1-byte stub code (on a real-state Fork they are the real contracts);
- actors are the labelled synthetic ones of ``release_config`` (on a real-state Fork: the reviewed addresses).
"""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import distributor_predeploy as dp  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-mainnet-v1.json'
READY = CANONICAL_READY and TOPOLOGY.is_file()
ZERO = '0x' + '00' * 20
STUB = '0x00'


def labelled(tag):
    return '0x' + keccak256(f'faroo v4 fork {tag}'.encode())[-40:]


def segment_files(chain, directory, name, addresses, start, end, rows):
    """One B0-schema plan and chunk (absolute paths) over ``addresses`` for [start, end] (SYNTHETIC)."""
    addresses = sorted(addresses)
    mine = [r for r in rows if r['address'] in addresses and start <= int(r['blockNumber'], 16) <= end]
    windows, w = [], start
    while w <= end:
        e = min(w + 999, end)
        windows.append([w, e, sum(w <= int(r['blockNumber'], 16) <= e for r in mine)])
        w = e + 1
    head = chain.rpc('eth_getBlockByNumber', [hex(end), False])
    plan = {'addresses': [{'address': a, 'labels': ['synthetic']} for a in addresses],
            'addresses_with_code_before_start': [], 'chain': 'mainnet', 'chain_id': 1672, 'chunk_windows': len(windows),
            'created_utc': '2026-10-21T00:00:00Z', 'end': end, 'end_block_hash': head['hash'],
            'end_timestamp': int(head['timestamp'], 16), 'filter_id': j.historical_filter_id(addresses),
            'head_at_plan': end, 'rpc': 'https://rpc.pharos.xyz', 'start': start, 'start_basis': 'SYNTHETIC',
            'tool_version': 'synthetic', 'vaults': [], 'window': 1000}
    chunk = {'chain': 'mainnet', 'fetched_utc': '2026-10-21T00:00:00Z', 'filter_id': plan['filter_id'], 'from': start,
             'to': end, 'windows': windows, 'logs': mine}
    directory = Path(directory) / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'plan.json').write_text(json.dumps(plan))
    (directory / 'chunk.json').write_text(json.dumps(chunk))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    return {'planFile': str(directory / 'plan.json'), 'planSha256': sha(directory / 'plan.json'),
            'chunks': [{'path': str(directory / 'chunk.json'), 'sha256': sha(directory / 'chunk.json')}]}


def synthetic_world(name):
    """The fixture plus the deployed V4 topology before the anchor. Returns (run, deployed, actors, inputs, history)."""
    import execution as ex
    import localchain as lc
    import release_config as rc
    import v4_fork as vf
    run = ex.Run('execution-mainnet-v1', name)
    chain, m = run.chain, run.m
    actors = {**rc.mainnet_actors(m), 'guardian': rc.actor_set('mainnet')['guardian']}
    pin = int(chain.block()['number'], 16)
    chain.warp(12)
    # SlpDistributor pre-deployment (5 transactions) and P-0 reusing it, as on mainnet
    arts = {'SlpDistributor': lc.canonical('SlpDistributor'), 'TimelockController': lc.canonical('TimelockController'),
            'tupV4': lc.legacy_fixture('mainnet', 'TransparentUpgradeableProxy_V4')}
    recorded = {'source': 'SYNTHETIC mainnet fixture', 'proxyAdmin': m['proxyAdmin'], 'govSafe': m['safe'],
                'proxyAdminRuntimeKeccak': dp.PROXY_ADMIN_RUNTIME, 'proxyRuntimeKeccak': dp.TUP_V4_RUNTIME}
    first = int(chain.rpc('eth_getTransactionCount', [actors['deployer'], 'latest']), 16)
    plan = dp.plan({'chainId': 1672, 'deployer': actors['deployer'], 'deployerNonce': first, 'govSafe': m['safe'],
                    'canSafe': actors['can'], 'guardian': actors['guardian'], 'keeper': ZERO, 'destinations': [],
                    'proxyAdmin': m['proxyAdmin']}, arts, recorded=recorded)
    got, hashes = {}, []
    for tx in plan['transactions']:
        if tx['kind'] == 'CREATE':
            got[tx['step']], rec = chain.create(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['data'])
        else:
            _, rec = chain.send(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['to'], tx['data'])
        hashes.append({'step': tx['step'], 'hash': rec['transactionHash'].lower()})
    predeployed = {'plan': plan, 'planSha256': j.canonical_sha256(plan), 'transactions': hashes}
    existing = {'timelock': got[2], 'distributor': got[1], 'distributorProxy': got[5]}
    p0, identity, _ = lc.deploy_p0(chain, network='mainnet', actors=actors, proxy_admin=m['proxyAdmin'],
                                   stpros=m['stpros'], destinations=[], existing=existing)
    assert all(r['verdict'] == 'PASS' for r in identity), identity
    inputs = {**vf.REAL_INPUTS, 'predeployed': predeployed}
    for slp in inputs['destinations']:                        # SYNTHETIC: the real SLP addresses need code here
        chain.rpc('anvil_setCode', [slp, STUB])
    chain.warp(12)
    # the deployed descriptor: the same runtimes as mainnet, synthetic addresses and nonces
    nonce_of = {dp.create_address(actors['deployer'], n): n for n in range(first, first + 40)}
    names = {'timelock': got[2], 'distributor': got[1], 'distributorProxy': got[5], **{k: p0[k] for k in (
        'vault', 'factory', 'oracle', 'stpros', 'bridge', 'bridgeProxy', 'assertions')}}
    deployed = {'deployer': actors['deployer'], 'contracts': {
        k: {**vf.DEPLOYED['contracts'][k], 'address': a.lower(), 'nonce': nonce_of[a.lower()]} for k, a in names.items()}}
    run.start_anchor = run._head()
    anchor = run.start_anchor['block']
    # the archive: legacy targets from block 1 to the pin, every release target after it (fixture rows + node logs)
    legacy = [m['oracle'], m['stpros'], m['proxyAdmin'], m['factory'], m['beacon'], m['vault001']]
    new = [deployed['contracts'][n]['address'] for n in vf.CREATED_TARGETS]
    block1 = chain.rpc('eth_getBlockByNumber', ['0x1', False])['hash']
    rows = [{**r, 'blockHash': r.get('blockHash') or block1} for r in rc.history(chain, run.topo, legacy + new)]
    directory = run.dir / 'archive'
    history = [segment_files(chain, directory, 'a', legacy, 1, pin, rows),
               segment_files(chain, directory, 'b', legacy + new, pin + 1, anchor, rows)]
    run.archive_rows, run.archive_root, run.archive_targets = rows, directory, sorted(legacy + new)
    return run, deployed, actors, inputs, history, pin


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the execution topologies in this export')
class V4ForkSyntheticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import v4_acceptance as va
        import v4_fork as vf
        run, deployed, actors, inputs, history, pin = synthetic_world('test-v4-fork-synthetic')
        cls.run_ = run
        try:
            cls.legacy_before = legacy_state(run)
            cls.doc = vf.run_release(run, deployed, actors, inputs, history_segments=history, created_start=pin + 1,
                                     history_from_block=1, rpc_factory=synthetic_rpc(run.m), then=va.acceptance)
            (run.dir / 'v4-fork-synthetic.json').write_text(json.dumps(cls.doc, indent=1, sort_keys=True, default=str))
            cls.legacy_after = legacy_state(run)
            cls.transcript = {t['to'] for t in run.chain.transcript if t.get('to')}
            cls.deployed = deployed
        finally:
            run.close()

    def test_nothing_is_deployed_and_identity_holds(self):
        self.assertTrue(all(r['ok'] for r in self.doc['identity']), [r for r in self.doc['identity'] if not r['ok']])
        self.assertEqual(self.doc['createsByRunner'], 0)                            # the runner deploys nothing
        # the release tool still lists the P-0 definitions (informational, unsigned); they are never executed: on
        # mainnet those contracts are the deployed ones (identity above)
        self.assertEqual(sorted(self.doc['plan']['definitions']), ['BridgeVault', 'ReleaseAssertions', 'YieldVault',
                                                                   'YieldVaultFactory'])
        self.assertEqual(self.doc['plan']['p1Steps'], [])

    def test_h_checkpoint_600_s_u_r4(self):
        self.assertEqual((self.doc['status'], self.doc['stoppedAt'], self.doc['error'], self.doc['notRun']),
                         ('COMPLETED', None, None, []))
        # the upgrade passed; A-G has G-INTEGRATION BLOCKED (no real RewardsVault here), so nothing reads as all-PASS
        self.assertEqual((self.doc['upgradeVerdict'], self.doc['businessVerdict'], self.doc['overallVerdict']),
                         ('PASS', 'BLOCKED', 'BLOCKED'))
        self.assertEqual(self.doc['releaseGate'], 'OPEN (technical only)')
        self.assertTrue(self.doc['authorization'].startswith('NOT A BROADCAST AUTHORIZATION'))
        self.assertTrue(any(t['label'] == 'U.execTransaction' for t in self.doc['transcript']))
        s = self.doc['stages']
        self.assertTrue(s['H']['ok'] and s['U']['ok'])                       # receipt status and Safe inner success
        self.assertEqual(s['H']['status'], 1)
        self.assertEqual(s['Hcheckpoint']['status'], 'PASS', s['Hcheckpoint'])
        self.assertTrue(s['UbeforeDelay']['refused'])
        self.assertEqual(s['U']['status'], 1)
        self.assertLessEqual(s['U']['gasUsed'], 2_200_000)
        self.assertEqual(s['R4']['status'], 'PASS', s['R4'])

    def test_final_state_is_the_rev2_release(self):
        f = self.doc['final']
        self.assertEqual(f['oracle']['maxAprPpm'], 300000)
        self.assertEqual((f['oracle']['maxUpdateAmount'], f['oracle']['updateInterval']), (str(80000 * 10 ** 18), 72000))
        self.assertEqual(f['stpros']['unbondingPeriod'], 86400)
        self.assertEqual((f['distributor']['keeper'], f['distributor']['slps']), (f['expected']['keeper'], f['expected']['slps']))
        self.assertEqual(f['timelock']['minDelay'], 600)

    def test_created_targets_are_in_the_history_proof(self):
        c = self.deployed['contracts']
        self.assertEqual(sorted(e['address'] for e in self.doc['bundle']['createdTargets']),
                         sorted(c[n]['address'] for n in ('timelock', 'distributorProxy', 'bridgeProxy', 'assertions')))
        self.assertNotIn(self.run_.m['wpros'], self.doc['bundle']['historyTargets'])     # asset: identity only

    def test_acceptance_a_to_g(self):
        acc = self.doc['acceptance']
        failed = {k: [r for r in v['rows'] if not r['ok']][:3] for k, v in acc.items() if v['verdict'] == 'FAIL'}
        self.assertEqual(failed, {})
        for cid in ('A', 'B', 'C', 'D', 'E', 'G'):
            self.assertEqual(acc[cid]['verdict'], 'PASS', (cid, acc[cid].get('blocked'), acc[cid].get('notApplicable')))
        self.assertIn('CONSTRUCTED_LIQUIDITY', acc['D']['evidence'] + acc['E']['evidence'])
        self.assertIn('TEST_USER_FUNDS', acc['A']['evidence'])
        self.assertIn(acc['F']['verdict'], ('PASS', 'BLOCKED'), acc['F'])
        self.assertIn('CONSTRUCTED_REPORTER_CALL', acc['G']['evidence'])          # no real RewardsVault here
        self.assertEqual(acc['G-INTEGRATION']['verdict'], 'BLOCKED')              # reported apart from accounting
        table = next(n['table'] for n in acc['G']['notes'] if isinstance(n, dict) and 'table' in n)
        self.assertEqual([r['intervalSeconds'] for r in table], [72000, 72000, 86400])

    def test_factory_test_is_untouched(self):
        m = self.run_.m
        self.assertEqual(self.legacy_before, self.legacy_after)
        self.assertFalse({m['factoryTest'].lower(), m['testVault'].lower(), m['beaconTest'].lower()} & self.transcript)


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the execution topologies in this export')
class V4ForkGateTest(unittest.TestCase):
    """Each gate stops the run where it fails; nothing after it executes and the evidence keeps the failure."""

    @classmethod
    def setUpClass(cls):
        run, deployed, actors, inputs, history, pin = synthetic_world('test-v4-fork-gates')
        cls.run_, cls.args = run, (deployed, actors, inputs)
        cls.kw = dict(history_segments=history, created_start=pin + 1, history_from_block=1, rpc_factory=synthetic_rpc(run.m))
        cls.anchor = run.chain.snapshot()

    @classmethod
    def tearDownClass(cls):
        cls.run_.close()

    def setUp(self):
        self.run_.chain.revert(self.anchor)
        self.anchor = type(self).anchor = self.run_.chain.snapshot()

    def release(self, deployed=None, **kw):
        import v4_fork as vf
        calls = []

        def then(run, ctx):
            calls.append('acceptance')
            return {}
        d, actors, inputs = self.args
        start = len(self.run_.chain.transcript)              # the transcript is cumulative; this run's part only
        doc = vf.run_release(self.run_, deployed or d, actors, inputs, then=then, **{**self.kw, **kw})
        return doc, calls, [t['label'] for t in self.run_.chain.transcript[start:]]

    def stopped(self, doc, stage, code):
        self.assertEqual((doc['status'], doc['stoppedAt'], doc['error']['code']), ('STOPPED', stage, code), doc.get('error'))
        self.assertEqual(doc['overallVerdict'], 'FAIL')
        self.assertTrue(doc['releaseGate'].startswith('CLOSED'))

    def test_identity_mismatch_builds_nothing(self):
        import copy
        d = copy.deepcopy(self.args[0])
        d['contracts']['assertions']['runtime'] = '0x' + '00' * 32
        doc, calls, labels = self.release(d)
        self.stopped(doc, 'identity', 'IDENTITY_MISMATCH')
        self.assertNotIn('bundle', doc)
        self.assertEqual((labels, calls), ([], []))                          # nothing sent at all
        self.assertIn('H', doc['notRun'])

    def test_h_checkpoint_refused_stops_before_u(self):
        import v4_fork as vf
        real = vf.checkpoint

        def refuse_h(chain, action, *a, **k):
            return {'status': 'REFUSED', 'error': 'SYNTHETIC_REFUSAL'} if action == 'preflight' else real(chain, action, *a, **k)
        with mock.patch.object(vf, 'checkpoint', refuse_h):
            doc, calls, labels = self.release()
        self.stopped(doc, 'Hcheckpoint', 'H_CHECKPOINT_NOT_PASS')
        self.assertTrue(doc['stages']['H']['ok'])                             # H itself is kept as evidence
        self.assertNotIn('U', doc['stages'])
        self.assertFalse([x for x in labels if x.startswith('U.')])            # U never sent
        self.assertEqual(calls, [])

    def test_u_not_refused_before_the_delay_stops(self):
        with mock.patch.object(type(self.run_.chain), 'reverts', lambda *a, **k: False):
            doc, calls, labels = self.release()
        self.stopped(doc, 'UbeforeDelay', 'U_NOT_REFUSED_BEFORE_DELAY')
        self.assertFalse([x for x in labels if x.startswith('U.')])

    def test_u_failure_stops_before_r4(self):
        import v4_fork as vf
        real = vf.checkpoint

        def then_break(chain, action, *a, **k):
            out = real(chain, action, *a, **k)
            if action == 'preflight':                        # after a passing H checkpoint: an SLP loses its code
                chain.rpc('anvil_setCode', [self.args[2]['destinations'][0], '0x'])
            return out
        with mock.patch.object(vf, 'checkpoint', then_break):
            doc, calls, _ = self.release()
        self.stopped(doc, 'U', 'U_FAILED')
        self.assertFalse(doc['stages']['U']['ok'])
        self.assertNotIn('R4', doc['stages'])
        self.assertEqual(calls, [])

    def test_r4_refused_runs_no_acceptance(self):
        import v4_fork as vf
        real = vf.checkpoint

        def refuse_r4(chain, action, *a, **k):
            return {'status': 'REFUSED', 'error': 'SYNTHETIC_REFUSAL'} if action == 'postcheck' else real(chain, action, *a, **k)
        with mock.patch.object(vf, 'checkpoint', refuse_r4):
            doc, calls, _ = self.release()
        self.stopped(doc, 'R4', 'R4_NOT_PASS')
        self.assertTrue(doc['stages']['U']['ok'])
        self.assertTrue(doc['releaseGate'].startswith('CLOSED: R4_NOT_PASS'), doc['releaseGate'])   # never OPEN
        self.assertEqual((doc['upgradeVerdict'], doc['overallVerdict']), ('FAIL', 'FAIL'))
        self.assertEqual(calls, [])
        self.assertIn('acceptance', doc['notRun'])

    def test_u_gas_above_the_cap_fails_the_gate(self):
        doc, calls, _ = self.release(gas_cap=1_000_000)
        self.assertEqual((doc['status'], doc['error']['code'], doc['upgradeVerdict'], doc['overallVerdict']),
                         ('FAIL', 'U_GAS_OVER_CAP', 'FAIL', 'FAIL'))
        self.assertTrue(doc['releaseGate'].startswith('CLOSED: U_GAS_OVER_CAP'))
        self.assertEqual(doc['stages']['R4']['status'], 'PASS')              # R4 kept as evidence
        self.assertEqual(calls, [])                                          # no A-G after a failed gate
        self.assertIn('acceptance', doc['notRun'])


def synthetic_rpc(meta):
    """SYNTHETIC ONLY: the fixture state is loaded at genesis, so the legacy targets show code at block 0. This
    answers exactly ``eth_getCode(legacy target, 0x0)`` with '0x' (their pre-creation state, which a loaded fixture
    lacks); every other read goes to the node. A real-state Fork never uses it (the read is a real public read)."""
    from tools.release.native.rpc import ReleaseRpc
    legacy = {meta[k].lower() for k in ('oracle', 'stpros', 'proxyAdmin', 'factory', 'beacon', 'vault001')}

    def factory(url, simulation=False):
        real = ReleaseRpc(url, simulation)

        async def rpc(method, params):
            if method == 'eth_getCode' and params[1] == '0x0' and str(params[0]).lower() in legacy:
                return '0x'
            return await real(method, params)
        return rpc
    return factory


def legacy_state(run):
    chain, m = run.chain, run.m
    return {'factoryTestOwner': chain.addr(m['factoryTest'], 'owner()'),
            'beaconTestImplementation': chain.addr(m['beaconTest'], 'implementation()'),
            'testVaultPhase': chain.uint(m['testVault'], 'phase()'),
            'testVaultStPROS': chain.uint(m['stpros'], 'balanceOf(address)', m['testVault'])}


if __name__ == '__main__':
    unittest.main()
