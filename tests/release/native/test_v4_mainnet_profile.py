"""V4 mainnet release profile (product decision, 2026-10-20), offline.

- Oracle: ``maxAprPpm == 3 x expectedGrossAprPpm`` inside the V4 mainnet release scope; the contract's 500,000 ppm
  ceiling still applies. Everything outside the scope (frozen R4 corpus, testnet, the recorded two-Factory mainnet
  rehearsals) keeps the 2 x rule.
- StPROS: ``policy.unbondingPeriod`` must be 86,400 s in scope and absent outside. U sets it with
  ``setUnbondingPeriod`` right after the StPROS upgrade; H does not; ``assertR4`` expects it.
- R4 economics replay: a request queued after U snapshots the new waiting period, earlier ones keep theirs; any other
  change of the waiting period is refused.
- R4 history: StPROS's ``UnbondingPeriodChanged`` is accepted only in the transaction that upgrades StPROS (U).

The base config is the corpus's accepted mainnet release preflight config with the V4 profile applied.
"""
import asyncio
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
from test_v4_delay_policy import V4_MAINNET_POLICY, approved_governance, corpus  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import checkpoint, economics, inputs, plans  # noqa: E402
from tools.release.native._shared import ZERO, hash_text  # noqa: E402

SET_UNBONDING = '0x' + hash_text('setUnbondingPeriod(uint256)')[2:10]
WEEK, DAY = 7 * 86400, 86400


class V4MainnetProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, declared = corpus()
        release = next(r for r in rows if r['id'] in declared and r['function'] == 'core.mjs.preflight'
                       and r['outcome'] == 'return')
        cls.args = gate.revive(release['arguments'])
        cls.simulation = next(gate.revive(r['arguments']) for r in rows if r['function'] == 'core.mjs.preflight'
                              and r['outcome'] == 'return' and gate.revive(r['arguments'])[0].get('mode') == 'simulation'
                              and gate.revive(r['arguments'])[0].get('chainId') == 1672)

    def v4(self, **policy):
        c = copy.deepcopy(self.args[0])
        c['policy'] = {**c['policy'], 'timelockDelayPolicy': 'V4-CONFIG-600', 'minDelay': 600}
        approved_governance(c)
        c['policy'].update(policy)
        for key, value in list(c['policy'].items()):
            if value is None:
                del c['policy'][key]
        return c

    def refused(self, code, fn, *args):
        with self.assertRaises(j.ReleaseError) as raised:
            fn(*args)
        self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    # --- APR -------------------------------------------------------------------------------------------------
    def test_three_times_apr_in_scope(self):
        c = self.v4()
        self.assertTrue(inputs.v4_mainnet_scope(c))
        self.assertEqual(inputs.validate_config(c)['policy']['maxAprPpm'], 300000)
        inputs.validate_config(self.v4(expectedGrossAprPpm=150000, maxAprPpm=450000))      # any 3 x within the ceiling
        for policy in ({'maxAprPpm': 200000},                                  # the old 2 x
                       {'maxAprPpm': 299999}, {'maxAprPpm': 300001},
                       {'expectedGrossAprPpm': 200000, 'maxAprPpm': 600000},    # 3 x above MAX_APR_PPM (500,000)
                       {'expectedGrossAprPpm': None}):
            with self.subTest(**policy):
                self.refused('APR_POLICY', inputs.validate_config, self.v4(**policy))

    def test_two_times_apr_outside_scope(self):
        sim = copy.deepcopy(self.simulation[0])                                  # frozen R4 mainnet simulation
        self.assertFalse(inputs.v4_mainnet_scope(sim))
        self.assertEqual(int(sim['policy']['maxAprPpm']), 2 * int(sim['policy']['expectedGrossAprPpm']))
        inputs.validate_config(sim)
        sim['policy']['maxAprPpm'] = 3 * int(sim['policy']['expectedGrossAprPpm'])
        self.refused('APR_POLICY', inputs.validate_config, sim)
        # testnet and a two-Factory mainnet V4 simulation stay at 2 x
        self.assertEqual(inputs.apr_multiple({'chainId': 688689, 'mode': 'release',
                                              'policy': {'timelockDelayPolicy': 'V4-CONFIG-600'}}), 2)
        self.assertEqual(inputs.apr_multiple({'chainId': 1672, 'mode': 'simulation', 'factories': [{}, {}],
                                              'policy': {'timelockDelayPolicy': 'V4-CONFIG-600'}}), 2)

    # --- waiting period: policy --------------------------------------------------------------------------------
    def test_unbonding_policy(self):
        self.assertEqual(inputs.target_unbonding_period(self.v4()), DAY)
        for value in (None, DAY - 1, 3600, WEEK, 14 * DAY, '86400'):
            with self.subTest(value=value):
                self.refused('UNBONDING_POLICY', inputs.validate_config, self.v4(unbondingPeriod=value))
        sim = copy.deepcopy(self.simulation[0])
        self.assertEqual(inputs.target_unbonding_period(sim), int(sim['observed']['core']['unbondingPeriod']))
        sim['policy']['unbondingPeriod'] = DAY                                   # never silently ignored outside scope
        self.refused('UNBONDING_POLICY', inputs.validate_config, sim)

    # --- waiting period: U, H and the R4 expectation -------------------------------------------------------
    def test_u_sets_the_waiting_period_after_the_stpros_upgrade_and_h_does_not(self):
        c = self.v4()
        self.assertEqual(int(c['observed']['core']['unbondingPeriod']), WEEK)
        context = inputs.ReleaseContext(c, self.args[1], self.args[2])
        # the corpus inventory is the recorded two-Factory one, so preflight's recorded-topology check cannot pass
        # offline on this single-Factory config; it runs for real in the loopback flow (test_mainnet_v4_scope).
        # Here only the batch composition is checked.
        with patch.object(inputs.ReleaseContext, 'preflight', lambda self: None):
            u = plans.build_u(context)['transactions']
            h = plans.build_handoff(context)['transactions']
        st, pa = c['addresses']['stpros'], c['addresses']['proxyAdmin']
        sets = [k for k, t in enumerate(u) if t['data'].startswith(SET_UNBONDING)]
        self.assertEqual(len(sets), 1)
        i = sets[0]
        self.assertEqual((u[i]['to'], u[i]['data']), (st, SET_UNBONDING + format(DAY, '064x')))
        self.assertEqual(u[i - 1]['to'], pa)
        self.assertEqual(u[i - 1]['data'][10:74], '00' * 12 + st[2:])          # upgradeAndCall(StPROS, ...)
        self.assertEqual(u[-1]['to'], c['addresses']['assertions'])              # assertR4 stays last
        self.assertEqual(plans.core_expectation(c)['unbondingPeriod'], DAY)       # assertR4 expects 1 day
        self.assertFalse([t for t in h if t['data'].startswith(SET_UNBONDING)])   # H changes nothing
        # outside the scope: no call, and R4 expects the observed value (frozen behaviour)
        sim = self.simulation[0]
        self.assertEqual(plans.core_expectation(sim)['unbondingPeriod'], int(sim['observed']['core']['unbondingPeriod']))


# --- R4 economics replay --------------------------------------------------------------------------------------
ST, IMPL, ALICE, BOB = ('0x' + format(n, '040x') for n in (0x57, 0x5701, 0xa11ce, 0xb0b))
WORD = lambda a: '0x' + '00' * 12 + a[2:]
UINT = lambda *v: '0x' + ''.join(format(x, '064x') for x in v)
WITHDRAW = hash_text('Withdraw(address,address,address,uint256,uint256)')       # independent of the module constants
UNBONDING_CHANGED = hash_text('UnbondingPeriodChanged(uint256,uint256)')
UPGRADED = hash_text('Upgraded(address)')


def replay_config():
    return {'chainId': 1672, 'mode': 'release', 'policy': {'timelockDelayPolicy': 'V4-CONFIG-600', 'unbondingPeriod': DAY},
            'factories': [{'vaults': []}], 'observed': {'blockNumber': 10, 'proxies': {ST: {'version': 1}}},
            'legacy': {ST: {'version': 1}}, 'addresses': {'stpros': ST}, 'implementations': {'stpros': IMPL}}


def withdraw(owner, amount, block):
    return {'topics': [WITHDRAW, WORD(owner), WORD(owner), WORD(owner)], 'data': UINT(amount, amount),
            'blockNumber': hex(block), 'blockHash': '0x' + format(block, '064x'), 'transactionHash': '0x' + format(block, '064x')}


def change(old, new, block):
    return {'topics': [UNBONDING_CHANGED], 'data': UINT(old, new), 'blockNumber': hex(block),
            'blockHash': '0x' + format(block, '064x'), 'transactionHash': '0x' + format(block, '064x')}


def upgrade(block):
    return {'topics': [UPGRADED, WORD(IMPL)], 'data': '0x', 'blockNumber': hex(block),
            'blockHash': '0x' + format(block, '064x'), 'transactionHash': '0x' + format(block, '064x')}


def replay(rows, after_records, post=True):
    async def fake_range_logs(rpc, target, from_block, to_block, topics):
        return [r for r in rows if r['topics'][0] in topics] if target == ST else []

    async def rpc(method, params):
        assert method == 'eth_getBlockByNumber'
        n = int(params[0], 16)
        return {'hash': '0x' + format(n, '064x'), 'timestamp': hex(1000 + n)}
    queued = sum(int(r['pending']) for a in after_records for r in a['records'])
    before = {'economic': {'withdrawals': {'accounts': [], 'queued': '0', 'completed': '0'}}, 'core': {'unbondingPeriod': WEEK},
              'vaultAssets': {}}
    after = {'economic': {'withdrawals': {'accounts': sorted(after_records, key=lambda a: a['owner']), 'queued': str(queued),
                                          'completed': '0'}}, 'vaultAssets': {}}
    with patch.object(economics, 'range_logs', fake_range_logs):
        asyncio.run(economics.reconcile_economics(replay_config(), None, rpc, '0x40', before, after, post))


def account(owner, *records):
    return {'owner': owner, 'head': '0', 'tail': str(len(records)), 'records': list(records)}


def record(queued, pending, block, period):
    return {'queued': str(queued), 'pending': str(pending), 'createdAt': str(1000 + block), 'unbondingPeriod': str(period),
            'receiver': ZERO}


class R4WaitingPeriodReplayTest(unittest.TestCase):
    def test_new_requests_follow_the_u_change_and_old_ones_keep_theirs(self):
        rows = [withdraw(ALICE, 5, 11), upgrade(20), change(WEEK, DAY, 20), withdraw(BOB, 3, 21)]
        replay(rows, [account(ALICE, record(0, 5, 11, WEEK)), account(BOB, record(5, 3, 21, DAY))])

    def test_without_the_change_the_old_period_is_expected(self):
        # a request after an upgrade that did not change the period keeps the old one (frozen behaviour)
        replay([upgrade(20), withdraw(BOB, 3, 21)], [account(BOB, record(0, 3, 21, WEEK))])
        with self.assertRaises(j.ReleaseError) as raised:          # the chain would hold 1 day: mismatch detected
            replay([upgrade(20), withdraw(BOB, 3, 21)], [account(BOB, record(0, 3, 21, DAY))])
        self.assertTrue(str(raised.exception).startswith('UNRECONCILED_WITHDRAWAL_RECORDS'))

    def test_any_other_change_is_refused(self):
        for rows, post in (([change(WEEK, DAY, 15), upgrade(20)], True),            # before the StPROS upgrade
                           ([upgrade(20), change(WEEK, 3600, 20)], True),            # not the policy value
                           ([upgrade(20), change(DAY, DAY, 20)], True),              # wrong old value
                           ([upgrade(20), change(WEEK, DAY, 20), change(DAY, DAY, 22)], True),   # a second change
                           ([upgrade(20), change(WEEK, DAY, 20)], False)):           # not a post-U reconciliation
            with self.subTest(rows=len(rows), post=post):
                with self.assertRaises(j.ReleaseError) as raised:
                    replay(rows, [], post)
                self.assertTrue(str(raised.exception).startswith(('UNREVIEWED_UNBONDING_HISTORY', 'UNREVIEWED_UPGRADE_HISTORY')),
                                str(raised.exception))


# --- R4 history: the StPROS configuration event of U ----------------------------------------------------------
class R4StprosHistoryTest(unittest.TestCase):
    TL, PA = '0x' + format(0x71, '040x'), '0x' + format(0x9a, '040x')

    def check(self, rows, stage='R4'):
        async def fake_range_logs(rpc, target, from_block, to_block, topics):
            return rows if target == ST else []
        cfg = {'addresses': {'proxyAdmin': self.PA, 'timelock': self.TL},
               'observed': {'blockNumber': 10, 'proxyAdminOwner': self.TL, 'proxies': {ST: {'owner': self.TL}}}, 'factories': []}
        with patch.object(checkpoint, 'range_logs', fake_range_logs):
            asyncio.run(checkpoint.governance_history(cfg, None, '0x40', stage, []))

    def test_unbonding_change_in_the_upgrade_transaction_passes(self):
        self.check([upgrade(20), change(WEEK, DAY, 20)])

    def test_unbonding_change_elsewhere_is_refused(self):
        for rows, stage in (([upgrade(20), change(WEEK, DAY, 21)], 'R4'),       # another transaction
                            ([change(WEEK, DAY, 21)], 'R4'),                     # no upgrade at all
                            ([upgrade(20), change(WEEK, DAY, 20)], 'H')):        # before U (StPROS not H-guarded)
            with self.subTest(stage=stage, rows=len(rows)):
                with self.assertRaises(j.ReleaseError) as raised:
                    self.check(rows, stage)
                self.assertTrue(str(raised.exception).startswith('UNREVIEWED_CONFIGURATION_HISTORY'), str(raised.exception))


if __name__ == '__main__':
    unittest.main()
