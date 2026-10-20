"""Mainnet V4 release scope: one Factory, one governed Vault, SlpDistributor configured inside U. Test-only, synthetic
loopback on the recorded mainnet fixture (class C).

The flow:
1. pre-deploy the SlpDistributor (keeper 0, no SLPs) and its 600 s Timelock;
2. P-0 reusing them;
3. a single-Factory release bundle through the native CLI (preflight, p1-runbook, build-handoff, build-u);
4. H through the GOV Safe MultiSend, ≥ 600 s, then U through the GOV Safe.

ReleaseAssertions runs on chain at both points: `assertHandoff` in H checks the pre-U state, `assertR4` in U checks
the final one.

Checked:
- Factory_test and the test vault appear in no P-1 / H / U transaction and stay untouched;
- a zero keeper with an empty list adds no call and still passes;
- a keeper with SLPs adds `setSlps` then `setKeeper` before `assertR4`, in the same batch;
- the Oracle migration parameters are the V4 mainnet profile (or the given overrides);
- the StPROS waiting period: H leaves the pre-upgrade value; U sets 1 day after the StPROS upgrade; queued requests
  keep their own snapshot and a request made after U snapshots 1 day;
- OPERATOR goes to OPS = GOV, DEFAULT_ADMIN stays with the Timelock, and a direct LOSS by OPS is refused;
- U gas stays within the 2.2M cap.
"""
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi, release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import distributor_predeploy as dp  # noqa: E402
from tools.release.native._shared import ROLES, SLOTS  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-mainnet-v1.json'
READY = CANONICAL_READY and TOPOLOGY.is_file()
ZERO = '0x' + '00' * 20
GAS_CAP = 2_200_000


def labelled(tag):
    return '0x' + keccak256(f'faroo v4 scope {tag}'.encode())[-40:]


STUB_INITCODE = '0x6001600c60003960016000f300'          # deploys a 1-byte runtime (STOP): a contract standing in for an SLP
V1_RECORDS = [{'type': 'tuple[]', 'components': [{'type': 'uint256'}] * 4}]
V4_RECORDS = [{'type': 'tuple[]', 'components': [{'type': 'uint256'}] * 4 + [{'type': 'address'}]}]


def records(chain, stpros, owner, v4):
    """(queued, pending, createdAt, unbondingPeriod) of each queued request of ``owner`` (V1 or V4 layout)."""
    rows = abi.decode_parameters(V4_RECORDS if v4 else V1_RECORDS, chain.call(stpros, 'getWithdrawals(address)', owner))[0]
    return [tuple(int(x) for x in list(r)[:4]) for r in rows]


def run_flow(name, keeper, slp_count, slps=None, overrides=None):
    """``slps``: explicit SLP addresses (given 1-byte stub code on the loopback node) instead of ``slp_count`` new stubs;
    ``overrides``: {'policy': {...}, 'actors': {...}} applied to the built config (e.g. the real mainnet parameters)."""
    import execution as ex
    import localchain as lc
    import release_config as rc
    import safe_exec
    run = ex.Run('execution-mainnet-v1', name)
    try:
        chain, m = run.chain, run.m
        # owner topology: GOV = OPS; CAN != Guardian
        actors = {**rc.mainnet_actors(m), 'guardian': rc.actor_set('mainnet')['guardian']}
        arts = {'SlpDistributor': lc.canonical('SlpDistributor'), 'TimelockController': lc.canonical('TimelockController'),
                'tupV4': lc.legacy_fixture('mainnet', 'TransparentUpgradeableProxy_V4')}
        recorded = {'source': 'SYNTHETIC mainnet fixture', 'proxyAdmin': m['proxyAdmin'], 'govSafe': m['safe'],
                    'proxyAdminRuntimeKeccak': dp.PROXY_ADMIN_RUNTIME, 'proxyRuntimeKeccak': dp.TUP_V4_RUNTIME}
        nonce = int(chain.rpc('eth_getTransactionCount', [actors['deployer'], 'latest']), 16)
        plan = dp.plan({'chainId': 1672, 'deployer': actors['deployer'], 'deployerNonce': nonce, 'govSafe': m['safe'],
                        'canSafe': actors['can'], 'guardian': actors['guardian'], 'keeper': ZERO, 'destinations': [],
                        'proxyAdmin': m['proxyAdmin']}, arts, recorded=recorded)
        got, hashes = {}, []
        for tx in plan['transactions']:
            if tx['kind'] == 'CREATE':
                got[tx['step']], rec = chain.create(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['data'])
            else:
                _, rec = chain.send(f'PREDEPLOY.{tx["step"]}', tx['from'], tx['to'], tx['data'])
            hashes.append({'step': tx['step'], 'hash': rec['transactionHash'].lower()})
        run.predeployed = {'plan': plan, 'planSha256': j.canonical_sha256(plan), 'transactions': hashes}
        existing = {'timelock': got[2], 'distributor': got[1], 'distributorProxy': got[5]}
        p0, identity, _ = lc.deploy_p0(chain, network='mainnet', actors=actors, proxy_admin=m['proxyAdmin'],
                                       stpros=m['stpros'], destinations=[], existing=existing)
        assert all(r['verdict'] == 'PASS' for r in identity), identity
        legacy_before = {'factoryTestOwner': chain.addr(m['factoryTest'], 'owner()'),
                         'beaconTestImplementation': chain.addr(m['beaconTest'], 'implementation()'),
                         'testVaultPhase': chain.uint(m['testVault'], 'phase()'),
                         'testVaultStPROS': chain.uint(m['stpros'], 'balanceOf(address)', m['testVault']),
                         'factoryTestImplementation': ex.implementation(chain, m['factoryTest'])}
        # SLPs must be contracts (SlpDistributor and the release tool refuse code-less SLPs): synthetic stubs
        if slps is None:
            slps = [chain.create(f'SLP stub {i}', labelled('slp stub deployer'), STUB_INITCODE)[0] for i in range(slp_count)]
        else:
            slps = [x.lower() for x in slps]
            for x in slps:
                if chain.code(x) in ('0x', '0x0'):
                    chain.rpc('anvil_setCode', [x, '0x00'])     # synthetic node: give the real SLP address code
        # a request queued before H on V1 code (its waiting-period snapshot must survive U)
        st = m['stpros']
        early_user, late_user = labelled('early redeemer'), labelled('late redeemer')
        for who in (early_user, late_user):
            chain.fund(who, 50 * 10 ** 18)
        chain.send('pre-H.depositWithPROS', early_user, st, abi.calldata('depositWithPROS()'), value=20 * 10 ** 18)
        chain.send('pre-H.redeem', early_user, st, abi.calldata('redeem(uint256,address,address)', 4 * 10 ** 18, early_user, early_user))
        holders = [x.lower() for x in m['users']] + [early_user]
        records_before = {who: records(chain, st, who, False) for who in holders}
        config, inv = rc.build(chain, run.topo, p0, {**actors, 'keeper': keeper}, single_factory=True)
        config['destinations'] = list(slps)
        for section, values in (overrides or {}).items():
            config[section].update(values)
        pre = run.cli('pre', 'preflight', config, inv, 'mainnet')
        p1 = run.cli('pre', 'p1-runbook', config, inv, 'mainnet')
        h = run.cli('pre', 'build-handoff', config, inv, 'mainnet')
        u = run.cli('pre', 'build-u', config, inv, 'mainnet')
        tl = p0['timelock']
        _, _, h_receipt, _ = safe_exec.execute(chain, 'H', m['safe'], m['safeOwners'], m['safeThreshold'],
                                               safe_exec.safe_tx(m['multiSendCallOnly'], safe_exec.multisend_calldata(h['transactions']), 1))
        h_ok = int(h_receipt['status'], 16) == 1
        unbonding_after_h = chain.uint(st, 'unbondingPeriod()')
        early = chain.reverts(tl, u['execute']['data'], m['safe'])
        chain.warp(601)
        _, _, u_receipt, _ = safe_exec.execute(chain, 'U', m['safe'], m['safeOwners'], m['safeThreshold'],
                                               safe_exec.safe_tx(u['execute']['to'], u['execute']['data'], 0))
        dist, vault = p0['distributorProxy'], m['vault001']
        unbonding_after_u = chain.uint(st, 'unbondingPeriod()')
        # a request queued after U snapshots the new waiting period; the earlier ones keep theirs
        chain.send('post-U.depositWithPROS', late_user, st, abi.calldata('depositWithPROS()'), value=20 * 10 ** 18)
        chain.send('post-U.redeem', late_user, st, abi.calldata('redeem(uint256,address,address)', 4 * 10 ** 18, late_user, late_user))
        records_after = {who: records(chain, st, who, True) for who in holders + [late_user]}
        has = lambda target, role, who: chain.boolean(target, 'hasRole(bytes32,address)', ROLES[role], who)
        loss = abi.calldata('proposeSettlement(uint8,uint256,address)', 1, 10 ** 18, ZERO)

        def revert_text(to, data, sender):
            try:
                chain.rpc('eth_call', [{'from': sender, 'to': to, 'data': data}, 'latest'])
                return ''
            except ValueError as error:
                return str(error)
        not_timelock = keccak256(b'NotTimelock()')[2:10]
        return {
            'preflight': pre, 'definitions': [d['contract'] for d in pre['deploymentDefinitions']['definitions']],
            'p1Steps': p1['steps'], 'h': h, 'u': u, 'hOk': h_ok, 'uBeforeDelayRefused': early,
            'uOk': int(u_receipt['status'], 16) == 1, 'uGas': int(u_receipt['gasUsed'], 16),
            'keeper': chain.addr(dist, 'keeper()'),
            'slps': [x.lower() for x in abi.decode_parameters([{'type': 'address[]'}], chain.call(dist, 'getSlps()'))[0]],
            'unbonding': {'observedBeforeH': int(config['observed']['core']['unbondingPeriod']), 'afterH': unbonding_after_h,
                          'afterU': unbonding_after_u, 'recordsBefore': records_before, 'recordsAfter': records_after,
                          'earlyUser': early_user, 'lateUser': late_user,
                          'policyMinimum': chain.uint(st, 'MIN_UNBONDING()'), 'policyMaximum': chain.uint(st, 'MAX_UNBONDING()')},
            'oracle': {'slp': chain.addr(m['oracle'], 'slp()'), 'commissionAccount': chain.addr(m['oracle'], 'commissionAccount()'),
                       'commissionRatePpm': chain.uint(m['oracle'], 'commissionRatePpm()'),
                       'maxUpdateAmount': chain.uint(m['oracle'], 'maxUpdateAmount()'), 'updateInterval': chain.uint(m['oracle'], 'updateInterval()'),
                       'maxAprPpm': chain.uint(m['oracle'], 'maxAprPpm()')},
            'config': config, 'actors': actors, 'meta': m, 'p0': p0, 'slpStubs': slps,
            'vaultRoles': {'operatorOps': has(vault, 'operator', actors['ops']), 'adminTimelock': has(vault, 'admin', tl),
                           'adminGov': has(vault, 'admin', m['safe'])},
            'opsDirectLossRefused': not_timelock in revert_text(vault, loss, actors['ops']),   # NotTimelock, checked first
            'stprosSink': chain.addr(m['stpros'], 'slp()'),
            'legacyBefore': legacy_before,
            'legacyAfter': {'factoryTestOwner': chain.addr(m['factoryTest'], 'owner()'),
                            'beaconTestImplementation': chain.addr(m['beaconTest'], 'implementation()'),
                            'testVaultPhase': chain.uint(m['testVault'], 'phase()'),
                            'testVaultStPROS': chain.uint(m['stpros'], 'balanceOf(address)', m['testVault']),
                            'factoryTestImplementation': ex.implementation(chain, m['factoryTest'])},
            'transcriptTargets': {t['to'] for t in chain.transcript if t.get('to')},
        }
    finally:
        run.close()


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the execution topologies in this export')
class MainnetV4ScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keeper = labelled('keeper')
        cls.unconfigured = run_flow('test-v4-scope-unconfigured', ZERO, 0)
        cls.configured = run_flow('test-v4-scope-configured', cls.keeper, 2)

    def legacy(self, r):
        m = r['meta']
        return {m['factoryTest'].lower(), m['testVault'].lower(), m['beaconTest'].lower()}

    def test_single_factory_topology_and_no_factory_test_transaction(self):
        for r in (self.unconfigured, self.configured):
            self.assertEqual(r['preflight']['status'], 'PASS')
            self.assertEqual(r['p1Steps'], [])                                       # no P-1 at all
            self.assertEqual(len(r['config']['factories']), 1)
            self.assertNotIn('TimelockController', r['definitions'])
            self.assertNotIn('SlpDistributor', r['definitions'])
            targets = {t['to'].lower() for t in r['h']['transactions']} | {t['to'].lower() for t in r['u']['transactions']}
            self.assertFalse(targets & self.legacy(r), targets & self.legacy(r))
            self.assertFalse({x.lower() for x in r['transcriptTargets']} & self.legacy(r))   # nothing sent to them
            self.assertEqual(r['legacyBefore'], r['legacyAfter'])                     # untouched
            self.assertTrue(r['hOk'] and r['uBeforeDelayRefused'] and r['uOk'])        # assertHandoff / assertR4 passed on chain

    def test_unconfigured_distributor_passes_with_no_extra_call(self):
        r = self.unconfigured
        notes = [t.get('note') for t in r['u']['transactions']]
        self.assertNotIn('SlpDistributor keeper', notes)
        self.assertNotIn('SlpDistributor whitelist', notes)
        self.assertEqual((r['keeper'], r['slps']), (ZERO, []))

    def test_configured_distributor_is_set_inside_u(self):
        r = self.configured
        txs = r['u']['transactions']
        notes = [t.get('note') for t in txs]
        i_slps, i_keeper = notes.index('SlpDistributor whitelist'), notes.index('SlpDistributor keeper')
        self.assertLess(i_slps, i_keeper)                                            # whitelist before keeper
        self.assertEqual(i_keeper, len(txs) - 2)                                     # right before assertR4
        self.assertEqual(txs[-1]['to'], r['config']['addresses']['assertions'])
        self.assertEqual({txs[i_slps]['to'], txs[i_keeper]['to']}, {r['p0']['distributorProxy']})
        self.assertEqual(len(r['slpStubs']), 2)
        self.assertEqual((r['keeper'], sorted(r['slps'])), (self.keeper, sorted(r['slpStubs'])))
        self.assertEqual(r['h']['transactions'][-1]['to'], r['config']['addresses']['assertions'])   # H: pre-U state only

    def test_oracle_migration_parameters_are_the_v4_mainnet_profile(self):
        for r in (self.unconfigured, self.configured):
            p, a = r['config']['policy'], r['config']['actors']
            self.assertEqual((p['expectedGrossAprPpm'], p['maxAprPpm'], p['maxUpdateAmount'], p['updateInterval']),
                             (100000, 300000, '80000000000000000000000', 72000))
            self.assertEqual(r['oracle'], {'slp': a['reporter'], 'commissionAccount': a['treasury'], 'commissionRatePpm': 100000,
                                           'maxUpdateAmount': 80000 * 10 ** 18, 'updateInterval': 72000, 'maxAprPpm': 300000})
            self.assertEqual(r['stprosSink'], r['p0']['distributorProxy'])

    def test_unbonding_period_is_set_in_u_only(self):
        for r in (self.unconfigured, self.configured):
            u = r['unbonding']
            self.assertEqual(u['observedBeforeH'], 7 * 86400)
            self.assertEqual(u['afterH'], 7 * 86400)                                  # H keeps the pre-upgrade value
            self.assertEqual(u['afterU'], 86400)                                      # U sets the V4 policy value
            self.assertEqual((u['policyMinimum'], u['policyMaximum']), (86400, 30 * 86400))   # contract bounds unchanged
            txs, st = r['u']['transactions'], r['config']['addresses']['stpros']
            notes = [t.get('note') for t in txs]
            i = notes.index('StPROS unbonding period')
            self.assertEqual(txs[i]['to'], st)
            self.assertEqual(txs[i]['data'], '0x' + keccak256(b'setUnbondingPeriod(uint256)')[2:10] + format(86400, '064x'))
            upgraded = [k for k, t in enumerate(txs) if t['to'] == r['config']['addresses']['proxyAdmin']
                        and t['data'][10:74].lower() == '00' * 12 + st[2:].lower()]   # upgradeAndCall(StPROS, ...)
            self.assertEqual(upgraded, [i - 1])                                       # right after the StPROS upgrade
            self.assertNotIn('StPROS unbonding period', [t.get('note') for t in r['h']['transactions']])
            self.assertFalse(any(t['data'][:10] == txs[i]['data'][:10] for t in r['h']['transactions']))

    def test_queued_requests_keep_their_snapshot(self):
        for r in (self.unconfigured, self.configured):
            u = r['unbonding']
            before, after = u['recordsBefore'], u['recordsAfter']
            self.assertTrue(before[u['earlyUser']])
            for who, rows in before.items():
                self.assertEqual(after[who][:len(rows)], rows, who)                   # every pre-H record unchanged
            self.assertTrue(all(row[3] == 7 * 86400 for row in before[u['earlyUser']]))
            self.assertEqual([row[3] for row in after[u['lateUser']]], [86400])       # a new request snapshots 1 day

    def test_vault_roles_and_loss(self):
        for r in (self.unconfigured, self.configured):
            self.assertEqual(r['vaultRoles'], {'operatorOps': True, 'adminTimelock': True, 'adminGov': False})
            self.assertTrue(r['opsDirectLossRefused'])                               # OPS = GOV cannot LOSS directly

    def test_u_gas_within_cap(self):
        for r in (self.unconfigured, self.configured):
            self.assertLessEqual(r['uGas'], GAS_CAP, r['uGas'])


if __name__ == '__main__':
    unittest.main()
