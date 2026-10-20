"""Mainnet SlpDistributor pre-deployment, then the V4 release reusing it. Test-only, synthetic loopback (case A).

1. The unsigned definitions of ``tools.release.native.distributor_predeploy`` are executed from the deployer.
2. The read-only verification accepts the result: the addresses equal the predictions, the runtimes are the
   canonical / recorded OZ v4 ones, the admin is the shared ProxyAdmin, whose own runtime and owner are the recorded
   ones, the Timelock is at 600 s, and distribution is disabled (keeper 0, no whitelist).
3. The keeper is configured by a 600 s Timelock operation of the GOV Safe (configuration phase).
4. The release bundle carries ``review.predeployed``. The native preflight then emits no second Timelock or
   SlpDistributor definition. P-0 reuses the three contracts, and H -> U completes with every check PASS on the same
   proxy.
5. Negative controls: a duplicate deployment definition, a wrong proxy, a wrong owner, a mismatched implementation,
   missing evidence and forged transaction evidence are all refused.

The synthetic fixture has its own ProxyAdmin and Safe addresses, so the plan is built with that fixture as its
recorded topology and is labelled SYNTHETIC. The registry-bound default is exercised by ``PlanTest``.
"""
import asyncio
import copy
import glob
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi, release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import checkpoint, distributor_predeploy as dp, inputs, plans  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-mainnet-v1.json'
READY = CANONICAL_READY and TOPOLOGY.is_file()
ZERO32 = '0x' + '00' * 32
REGISTRY_PA = '0x4238ea4adfa2bd6a5fc9b5e245dc1900cf0258aa'
REGISTRY_GOV = '0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca'


def predeploy_artifacts():
    import localchain as lc
    return {'SlpDistributor': lc.canonical('SlpDistributor'), 'TimelockController': lc.canonical('TimelockController'),
            'tupV4': lc.legacy_fixture('mainnet', 'TransparentUpgradeableProxy_V4')}


def refused(test, code, fn, *args):
    with test.assertRaises(j.ReleaseError) as raised:
        fn(*args)
    test.assertIn(code, str(raised.exception))
    return str(raised.exception)


class PlanTest(unittest.TestCase):
    def test_create_address_rule(self):
        # well-known vector: 0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0 nonce 0..3
        sender = '0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0'
        self.assertEqual([dp.create_address(sender, n) for n in range(4)],
                         ['0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d', '0x343c43a37d37dff08ae8c4a11544c718abb4fcf8',
                          '0xf778b86fa74e846c4f0a1fbd1335fe81c00a0c91', '0xfffd933a0bc612844eaf0c6fe3e5b8e9b6c1d19c'])

    def test_recorded_identities(self):
        r = dp.recorded_mainnet()
        self.assertEqual((r['proxyAdmin'], r['govSafe']), (REGISTRY_PA, REGISTRY_GOV))

    @unittest.skipUnless(CANONICAL_READY, 'NOT RUN: canonical build missing')
    def test_registry_bound_plan_and_refusals(self):
        arts = predeploy_artifacts()
        a = lambda c: '0x' + c * 20
        real = dp.governance_inputs()
        self.assertIsNone(real['deployerNonce'])            # the owner supplies it before signing; never invented
        # the owner's mainnet addresses; the nonce 0 below is a TEST-ONLY placeholder, never a deployment input
        base = {'chainId': 1672, 'deployer': real['deployer'], 'deployerNonce': 0, 'govSafe': real['gov'],
                'canSafe': real['can'], 'guardian': real['guardian'], 'keeper': '0x' + '00' * 20, 'destinations': [],
                'proxyAdmin': REGISTRY_PA}
        self.assertEqual(real['gov'], REGISTRY_GOV)
        p = dp.plan(base, arts)
        self.assertTrue(p['status'].startswith('UNSIGNED') and 'PREDICTED' in p['status'] and p['deployable'] is False)
        self.assertEqual(p['recordedIdentities']['class'], 'RECORDED MAINNET')
        self.assertEqual(p['expected']['proxyAdminOwner'], REGISTRY_GOV)
        self.assertIn('receive() is open', p['funds'])
        self.assertNotIn('deploymentBlockers', p)                           # no Safe evidence is required
        # the five transactions: order, kinds, sender, consecutive nonces, predicted CREATE addresses, full bytes
        txs = p['transactions']
        self.assertEqual([(t['step'], t['kind'], t['from'], t['nonce']) for t in txs],
                         [(n, k, real['deployer'], n - 1) for n, k in ((1, 'CREATE'), (2, 'CREATE'), (3, 'CALL'), (4, 'CALL'), (5, 'CREATE'))])
        self.assertEqual(p['predicted'], {'implementation': dp.create_address(real['deployer'], 0),
                                          'timelock': dp.create_address(real['deployer'], 1),
                                          'proxy': dp.create_address(real['deployer'], 4)})
        self.assertEqual(dp.transaction_problems(p, p['inputs'], p['predicted']), [])
        self.assertTrue(txs[1]['data'].endswith(dp.timelock_args(real['gov'], real['deployer'])))
        self.assertTrue(txs[4]['data'].endswith(dp.proxy_args(p['predicted']['implementation'], REGISTRY_PA, p['predicted']['timelock'],
                                                              '0x' + '00' * 20, [], real['guardian'])))     # keeper 0, no SLPs
        self.assertEqual((txs[2]['to'], txs[3]['to']), (p['predicted']['timelock'],) * 2)
        # CAN and the Guardian may be one address or two
        self.assertEqual(dp.plan({**base, 'guardian': real['can']}, arts)['inputs']['guardian'], real['can'])
        for name, key in (('SlpDistributor', 'creationBytecode'), ('TimelockController', 'creationBytecode'), ('tupV4', 'creationCode')):
            altered = {k: dict(v) for k, v in arts.items()}
            altered[name][key] = altered[name][key][:-2] + ('00' if altered[name][key][-2:] != '00' else '01')
            refused(self, 'PREDEPLOY_ARTIFACT', dp.plan, base, altered)
        for bad, code in (({'proxyAdmin': a('55')}, 'PREDEPLOY_PROXY_ADMIN'), ({'govSafe': a('22')}, 'PREDEPLOY_GOV_SAFE'),
                          ({'chainId': 688689}, 'PREDEPLOY_CHAIN'), ({'canSafe': REGISTRY_GOV}, 'PREDEPLOY_ACTOR_SEPARATION'),
                          ({'guardian': REGISTRY_GOV}, 'PREDEPLOY_ACTOR_SEPARATION'),
                          ({'keeper': real['guardian']}, 'PREDEPLOY_ACTOR_SEPARATION'), ({'deployerNonce': -1}, 'PREDEPLOY_INPUT')):
            refused(self, code, dp.plan, {**base, **bad}, arts)


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the CampaignAnvilFixture topologies in this export')
class PredeployThenUpgradeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import execution
        cls.ex = execution
        cls.facts = {}
        cls.r = execution.mainnet('A', waiting=True, name='test-distributor-predeploy', predeploy=cls.predeploy,
                                  then=cls.chain_evidence)
        cls.v = {c['id']: c for c in cls.r['checks']}
        cls.doc = json.loads((execution.OUT / 'test-distributor-predeploy' / 'run.json').read_text())
        # the pre-P-1 bundle exactly as the native CLI read it
        path = execution.OUT / 'test-distributor-predeploy' / 'pre-p1' / 'release-config.json'
        cls.config = j.checked_json(str(path), j.raw_sha256(path.read_bytes()))
        cls.inventory = j.checked_json(cls.config['inventoryFile'])
        cls.artifacts = {name: j.checked_json(p) for name, p in cls.config['artifactFiles'].items()}

    @classmethod
    def predeploy(cls, run, actors):
        import localchain as lc
        import safe_exec
        chain, m = run.chain, run.m
        nonce = int(chain.rpc('eth_getTransactionCount', [actors['deployer'], 'latest']), 16)
        recorded = {'source': 'SYNTHETIC mainnet fixture (execution-mainnet-v1)', 'proxyAdmin': m['proxyAdmin'],
                    'govSafe': m['safe'], 'proxyAdminRuntimeKeccak': dp.PROXY_ADMIN_RUNTIME, 'proxyRuntimeKeccak': dp.TUP_V4_RUNTIME}
        p = dp.plan({'chainId': 1672, 'deployer': actors['deployer'], 'deployerNonce': nonce, 'govSafe': m['safe'],
                     'canSafe': actors['can'], 'guardian': actors['guardian'], 'keeper': '0x' + '00' * 20, 'destinations': [],
                     'proxyAdmin': m['proxyAdmin']}, predeploy_artifacts(), recorded=recorded)
        got, hashes = {}, []
        for tx in p['transactions']:
            if tx['kind'] == 'CREATE':
                got[tx['step']], rec = chain.create(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['data'])
            else:
                _, rec = chain.send(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['to'], tx['data'])
                assert rec['status'] == 1, tx
            hashes.append({'step': tx['step'], 'hash': rec['transactionHash'].lower()})
        tl, proxy = got[2], got[5]
        code = lambda x: keccak256(bytes.fromhex(chain.code(x)[2:]))
        has = lambda role, who: chain.boolean(tl, 'hasRole(bytes32,address)', dp.ROLE[role], who)
        distribute = abi.encode_function({'type': 'function', 'name': 'distribute', 'inputs': [
            {'type': 'tuple[]', 'components': [{'type': 'address'}, {'type': 'uint256'}]}]}, [[[actors['keeper'], 1]]])
        keeper_probe = all(chain.reverts(proxy, distribute, who) for who in (actors['keeper'], actors['deployer'], m['safe']))
        observed = {'addresses': {'implementation': got[1], 'timelock': tl, 'proxy': proxy},
                    'implementationRuntimeKeccak': code(got[1]), 'timelockRuntimeKeccak': code(tl), 'proxyRuntimeKeccak': code(proxy),
                    'proxyAdminRuntimeKeccak': code(m['proxyAdmin']), 'proxyAdminOwner': chain.addr(m['proxyAdmin'], 'owner()'),
                    'adminSlot': '0x' + chain.slot(proxy, dp.EIP1967_ADMIN)[-40:], 'implementationSlot': '0x' + chain.slot(proxy, dp.EIP1967_IMPL)[-40:],
                    'initializedVersion': lc.initialized_version(chain, proxy), 'owner': chain.addr(proxy, 'owner()'),
                    'pendingOwner': chain.addr(proxy, 'pendingOwner()'),
                    'guardian': chain.addr(proxy, 'guardian()'), 'keeper': chain.addr(proxy, 'keeper()'),
                    'destinations': [x.lower() for x in abi.decode_parameters([{'type': 'address[]'}], chain.call(proxy, 'getSlps()'))[0]],
                    'minDelay': chain.uint(tl, 'getMinDelay()'), 'paused': chain.boolean(proxy, 'paused()'),
                    'roles': {'govProposer': has('proposer', m['safe']), 'govExecutor': has('executor', m['safe']),
                              'canCanceller': has('canceller', actors['can']), 'deployerAdmin': has('admin', actors['deployer']),
                              'deployerProposer': has('proposer', actors['deployer']), 'timelockSelfAdmin': has('admin', tl)},
                    'distributeByAnyoneReverts': keeper_probe}
        # the Guardian's Timelock roles, read from the chain (CAN / Guardian may be one address or two)
        observed['guardianRoles'] = {r: has(r, actors['guardian']) for r in ('proposer', 'executor', 'canceller')}
        cls.facts.update(plan=p, observed=observed, failures=dp.verify(observed, p))
        # a wrong ProxyAdmin owner is caught from the chain read, not from the inputs
        cls.facts['wrongPaOwner'] = dp.verify({**observed, 'proxyAdminOwner': actors['deployer']}, p)
        # no pre-upgrade configuration: the keeper stays 0 until U sets it in the same Timelock batch
        cls.facts['keeperBeforeU'] = chain.addr(proxy, 'keeper()')
        cls.facts['releaseKeeper'] = actors['keeper'].lower()
        cls.facts['proxy'] = proxy
        record = {'plan': p, 'planSha256': j.canonical_sha256(json.loads(json.dumps(p))), 'transactions': hashes}
        cls.facts['record'] = record
        return {'timelock': tl, 'distributor': got[1], 'distributorProxy': proxy, 'record': record}

    @classmethod
    def chain_evidence(cls, run, ctx):
        """The release checkpoint check of the five transactions (``checkpoint.verify_predeployed``), read from the
        loopback node at the H checkpoint block and at the post-U (R4) head, plus failing evidence."""
        async def rpc(method, params):
            return run.chain.rpc(method, params)
        cls.facts['keeperAfterU'] = run.chain.addr(cls.facts['proxy'], 'keeper()')
        blocks = {'H': hex(next(s['block'] for s in run.stages if s['stage'] == 'H')), 'R4': run.chain.block()['number']}
        step5 = next(t['block'] for t in run.chain.transcript if t['label'] == 'PREDEPLOY.5')
        record = cls.facts['record']

        def outcome(rec, block):
            try:
                asyncio.run(checkpoint.verify_predeployed({'review': {'predeployed': rec}}, rpc, block))
                return 'PASS'
            except j.ReleaseError as error:
                return str(error)

        def variant(change):
            rec = copy.deepcopy(record)
            change(rec)
            return rec
        cls.facts['chain'] = {
            'H': outcome(record, blocks['H']), 'R4': outcome(record, blocks['R4']),
            'beforeStep5': outcome(record, hex(step5 - 1)),
            'unknownHash': outcome(variant(lambda r: r['transactions'][4].update(hash='0x' + 'ee' * 32)), blocks['R4']),
            'swappedSteps': outcome(variant(lambda r: (r['transactions'][2].update(hash=record['transactions'][3]['hash']),
                                                       r['transactions'][3].update(hash=record['transactions'][2]['hash']))), blocks['R4']),
            'nonceMismatch': outcome(variant(lambda r: r['plan']['transactions'][1].update(nonce=r['plan']['transactions'][1]['nonce'] + 1)), blocks['H']),
            'inputMismatch': outcome(variant(lambda r: r['plan']['transactions'][0].update(
                data=r['plan']['transactions'][0]['data'][:-2] + ('00' if r['plan']['transactions'][0]['data'][-2:] != '00' else '01'))), blocks['H']),
            'missingTransaction': outcome(variant(lambda r: r['transactions'].pop()), blocks['H']),
        }
        return []

    # -- the pre-deployment itself ------------------------------------------------------------------
    def test_predeploy_verified(self):
        self.assertEqual(self.facts['failures'], [])
        # a Guardian that also held a Timelock role it does not hold as CAN is refused
        guardian_is_can = self.facts['plan']['inputs']['guardian'] == self.facts['plan']['inputs']['canSafe']
        for extra in ({'proposer': True}, {'executor': True}, {'canceller': not guardian_is_can}):
            bad = {**self.facts['observed'], 'guardianRoles': {**self.facts['observed']['guardianRoles'], **extra}}
            self.assertIn('guardianTimelockRoles', dp.verify(bad, self.facts['plan']))
        o = self.facts['observed']
        self.assertEqual(o['minDelay'], 600)
        self.assertTrue(o['distributeByAnyoneReverts'])
        self.assertEqual((o['keeper'], o['destinations']), ('0x' + '00' * 20, []))
        self.assertIn('proxyAdminOwner==recorded GOV Safe', self.facts['wrongPaOwner'])
        self.assertTrue(self.facts['plan']['status'].startswith('SYNTHETIC TOPOLOGY'))

    # -- the release reuses it ------------------------------------------------------------------------
    def test_v4_upgrade_reuses_the_proxy(self):
        p = self.facts['plan']['predicted']
        stage = next(s for s in self.doc['stages'] if s['stage'] == 'P-0')
        self.assertEqual(stage['addresses']['distributorProxy'], p['proxy'])
        self.assertEqual(stage['addresses']['timelock'], p['timelock'])
        self.assertEqual(self.r['ledgers']['FS-1.A']['aggregate'], 'PASS',
                         sorted(k for k, c in self.v.items() if c['verdict'] != 'PASS')[:10])
        self.assertNotIn('P-0 SlpDistributor proxy (v4 TUP)', [t['label'] for t in self.r['transcript']])
        self.assertNotIn('P-0 TimelockController', [t['label'] for t in self.r['transcript']])
        self.assertEqual(self.facts['keeperBeforeU'], '0x' + '00' * 20)                  # no pre-upgrade configuration
        self.assertEqual(self.facts['keeperAfterU'], self.facts['releaseKeeper'])        # set inside the U batch

    def test_cli_preflight_emits_no_second_deployment(self):
        stage = next(s for s in self.doc['stages'] if s['stage'] == 'P-0.definitions')
        self.assertNotIn('TimelockController', stage['contracts'])
        self.assertNotIn('SlpDistributor', stage['contracts'])
        self.assertEqual(stage['reused']['contracts']['SlpDistributor']['proxy'], self.facts['plan']['predicted']['proxy'])
        self.assertEqual(stage['reused']['contracts']['TimelockController']['address'], self.facts['plan']['predicted']['timelock'])
        self.assertEqual(stage['reused']['planSha256'], self.facts['record']['planSha256'])

    def test_h_and_u_use_the_same_proxy(self):
        u = plans.build_u(inputs.ReleaseContext(self.config, self.inventory, self.artifacts))
        proxy = self.facts['plan']['predicted']['proxy'][2:]
        st = self.config['addresses']['stpros']
        # upgradeAndCall(StPROS, ...): the first argument word is the StPROS proxy
        migration = [t for t in u['transactions'] if t['to'] == self.config['addresses']['proxyAdmin']
                     and t['data'][10:74].endswith(st[2:])]
        self.assertEqual(len(migration), 1)
        self.assertIn(proxy, migration[0]['data'])                 # migrateFromV1(slp = the pre-deployed proxy, ...)

    def test_chain_evidence_at_h_and_r4_checkpoints(self):
        c = self.facts['chain']
        self.assertEqual((c['H'], c['R4']), ('PASS', 'PASS'))
        self.assertIn('PREDEPLOY_AFTER_CHECKPOINT', c['beforeStep5'])
        self.assertIn('PREDEPLOY_NOT_MINED', c['unknownHash'])
        self.assertIn('PREDEPLOY_TRANSACTION', c['swappedSteps'])
        self.assertIn('PREDEPLOY_TRANSACTION', c['nonceMismatch'])
        self.assertIn('PREDEPLOY_TRANSACTION', c['inputMismatch'])
        self.assertIn('PREDEPLOY_EVIDENCE', c['missingTransaction'])

    def test_full_creation_bytes_are_bound(self):
        """Every CREATE byte and every transaction field is bound: a change anywhere is refused even though the
        predicted addresses and the expected runtimes in the plan stay exactly as they were."""
        def flip(data, at):
            return data[:at] + ('0' if data[at] != '0' else '1') + data[at + 1:]
        tx = lambda c, n: c['review']['predeployed']['plan']['transactions'][n - 1]
        txs = lambda c: c['review']['predeployed']['plan']['transactions']
        other = '0x' + 'ab' * 20
        cases = {
            'step 1 creation code': lambda c: tx(c, 1).update(data=flip(tx(c, 1)['data'], 200)),
            'step 2 creation code (prefix)': lambda c: tx(c, 2).update(data=flip(tx(c, 2)['data'], 200)),
            'step 5 creation code (prefix)': lambda c: tx(c, 5).update(data=flip(tx(c, 5)['data'], 200)),
            'step 2 constructor argument': lambda c: tx(c, 2).update(data=flip(tx(c, 2)['data'], len(tx(c, 2)['data']) - 1)),
            'step 5 constructor argument': lambda c: tx(c, 5).update(data=flip(tx(c, 5)['data'], len(tx(c, 5)['data']) - 70)),
            'step 1 trailing bytes': lambda c: tx(c, 1).update(data=tx(c, 1)['data'] + '00'),
            'step 3 call argument': lambda c: tx(c, 3).update(data=flip(tx(c, 3)['data'], len(tx(c, 3)['data']) - 1)),
            'step 3 target': lambda c: tx(c, 3).update(to=other),
            'step 1 kind': lambda c: tx(c, 1).update(kind='CALL'),
            'step 5 with a target': lambda c: tx(c, 5).update(to=other),
            'step 2 predicted address': lambda c: tx(c, 2).update(predictedAddress=other),
            'step 4 sender': lambda c: tx(c, 4).update(**{'from': other}),
            'step 4 nonce': lambda c: tx(c, 4).update(nonce=tx(c, 4)['nonce'] + 1),
            'order 3 <-> 4 (positions)': lambda c: txs(c).insert(2, txs(c).pop(3)),
        }

        def swap_contents(c):
            a, b = tx(c, 3), tx(c, 4)
            a['data'], b['data'] = b['data'], a['data']
        cases['order 3 <-> 4 (contents)'] = swap_contents
        plan0 = self.config['review']['predeployed']['plan']
        for name, change in cases.items():
            config = self.mutated(change)
            self.rehash(config)
            plan = config['review']['predeployed']['plan']
            with self.subTest(name):
                self.assertEqual((plan['predicted'], plan['expected']), (plan0['predicted'], plan0['expected']))
                refused(self, 'PREDEPLOY_BINDING', inputs.validate_predeployed, config, self.artifacts)
        self.assertEqual(dp.binding_problems(plan0, self.config, self.artifacts), [])

    # -- negative controls on the release bundle --------------------------------------------------------
    def mutated(self, change):
        config = copy.deepcopy(self.config)
        change(config)
        return config

    def rehash(self, config):
        record = config['review']['predeployed']
        record['planSha256'] = j.canonical_sha256(record['plan'])
        return config

    def test_without_the_record_the_definitions_would_deploy_again(self):
        config = self.mutated(lambda c: c['review'].pop('predeployed'))
        contracts = [d['contract'] for d in plans.deployment_definitions(config, self.artifacts)['definitions']]
        self.assertIn('TimelockController', contracts)
        self.assertIn('SlpDistributor', contracts)
        reused = plans.deployment_definitions(self.config, self.artifacts)
        self.assertNotIn('SlpDistributor', [d['contract'] for d in reused['definitions']])

    def test_binding_refusals(self):
        other = '0x' + 'ab' * 20
        plan = lambda c: c['review']['predeployed']['plan']
        cases = {   # name: (change, rehash the plan, refusal code, detail)
            'tampered plan': (lambda c: plan(c)['inputs'].update(keeper=other), False, 'PREDEPLOY_PLAN_HASH', ''),
            'wrong proxy': (lambda c: plan(c)['predicted'].update(proxy=other), True, 'PREDEPLOY_BINDING',
                            'planned proxy is not addresses.distributor'),
            'wrong owner (GOV)': (lambda c: plan(c)['inputs'].update(govSafe=other), True, 'PREDEPLOY_BINDING',
                                  'planned GOV Safe is not actors.gov'),
            'wrong Timelock': (lambda c: c['addresses'].update(timelock=other), False, 'PREDEPLOY_BINDING',
                               'planned Timelock is not addresses.timelock'),
            'implementation mismatch': (lambda c: c['implementations'].update(distributor=other), False, 'PREDEPLOY_BINDING',
                                        'planned implementation is not implementations.distributor'),
            'wrong ProxyAdmin': (lambda c: plan(c)['inputs'].update(proxyAdmin=other), True, 'PREDEPLOY_BINDING',
                                 'planned ProxyAdmin is not addresses.proxyAdmin'),
            'wrong nonce': (lambda c: plan(c)['inputs'].update(deployerNonce=plan(c)['inputs']['deployerNonce'] + 1), True,
                            'PREDEPLOY_BINDING', 'CREATE addresses'),
            'not 600 s': (lambda c: plan(c)['expected'].update(minDelay=86400), True, 'PREDEPLOY_BINDING', 'not 600 s'),
            'testnet': (lambda c: c.update(chainId=688689), False, 'PREDEPLOY_CHAIN', ''),
        }
        for name, (change, rehash, code, detail) in cases.items():
            config = self.mutated(change)
            if rehash:
                self.rehash(config)
            with self.subTest(name):
                self.assertIn(detail, refused(self, code, inputs.validate_predeployed, config, self.artifacts))

    def test_missing_evidence_is_blocked(self):
        for change in (lambda r: r.pop('transactions'), lambda r: r['transactions'].pop(),
                       lambda r: r['transactions'][0].update(hash='0x1234')):
            config = self.mutated(lambda c: change(c['review']['predeployed']))
            message = refused(self, 'PREDEPLOY_EVIDENCE', plans.deployment_definitions, config, self.artifacts)
            self.assertIn('BLOCKED', message)

    def test_preflight_checks_the_reused_contracts_on_chain_state(self):
        dist = self.config['addresses']['distributor']
        wrong_owner = self.mutated(lambda c: c['observed']['proxies'][dist].update(owner='0x' + 'ab' * 20))
        refused(self, 'UNEXPECTED_OWNER', inputs.preflight, wrong_owner, self.inventory, self.artifacts)
        wrong_code = self.mutated(lambda c: c['observed']['code'].update({dist: '0x6080'}))
        refused(self, 'PROXY_RUNTIME', inputs.preflight, wrong_code, self.inventory, self.artifacts)
        wrong_impl = self.mutated(lambda c: c['observed']['proxies'][dist].update(implementation='0x' + 'ab' * 20))
        with self.assertRaises(j.ReleaseError):
            inputs.preflight(wrong_impl, self.inventory, self.artifacts)
        self.assertEqual(inputs.preflight(self.config, self.inventory, self.artifacts)['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
