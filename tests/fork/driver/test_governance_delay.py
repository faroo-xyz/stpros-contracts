"""Mainnet Timelock delay policy: 600 s through H -> U and configuration, then the separate switch to 86,400 s.
Test-only, synthetic loopback (``execution.mainnet`` case A with the local Safe model).

Covers:
- the 600 s upgrade, with U leaving the delay at 600 s;
- configuration operations at 600 s (and their refusal before ready);
- an unauthorized direct ``updateDelay`` (Safe without the Timelock, or an EOA);
- refusal of the switch while a short-delay operation is pending, and its CAN cancellation;
- the residual risk the precondition guards against;
- the switch itself, including the rollback of a premature execution;
- the acceptance: 86,400 s, operation Done, short schedules refused, long schedules ready only after 86,400 s.
"""
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import governance_delay as gd  # noqa: E402

CANONICAL_READY = (ROOT / 'cache/native-r4/StPROS/artifact.json').is_file()
TOPOLOGY = ROOT / 'cache_forge/rehearsal/fork-campaign/execution/execution-mainnet-v1.json'
READY = CANONICAL_READY and TOPOLOGY.is_file()
ZERO32 = '0x' + '00' * 32


def _fn(name, *types):
    return {'type': 'function', 'name': name, 'inputs': [{'type': t} for t in types]}


SCHEDULE = _fn('schedule', 'address', 'uint256', 'bytes', 'bytes32', 'bytes32', 'uint256')
EXECUTE = _fn('execute', 'address', 'uint256', 'bytes', 'bytes32', 'bytes32')


class PlanTest(unittest.TestCase):
    """Offline: the plan's preconditions and encodings."""

    T, G, C = '0x' + '11' * 20, '0x' + '22' * 20, '0x' + '33' * 20

    def test_plan_encodes_self_call_with_current_delay(self):
        p = gd.switch_plan(chain_id=1672, timelock=self.T, gov_safe=self.G, can_safe=self.C, salt='0x' + '01' * 32,
                           current_delay=600, pending_operation_ids=[])
        self.assertEqual(p['status'], 'UNSIGNED REVIEW ARTIFACT')
        self.assertFalse(p['deployable'])
        self.assertEqual(p['call']['target'], self.T)                         # a Timelock self-call
        self.assertEqual(p['call']['data'], abi.encode_function(_fn('updateDelay', 'uint256'), [86400]))
        sched = p['safeTransactions'][0]['data']
        self.assertEqual(sched, abi.encode_function(SCHEDULE, [self.T, 0, p['call']['data'], ZERO32, '0x' + '01' * 32, 600]))
        self.assertEqual(p['operationId'], gd.operation_id(self.T, 0, p['call']['data'], ZERO32, '0x' + '01' * 32))

    def test_refusals(self):
        kw = dict(timelock=self.T, gov_safe=self.G, can_safe=self.C, salt='0x' + '01' * 32, pending_operation_ids=[])
        for bad in (dict(chain_id=688689, current_delay=600), dict(chain_id=1672, current_delay=86400),
                    dict(chain_id=1672, current_delay=172800)):
            with self.assertRaises(Exception):
                gd.switch_plan(**{**kw, **bad})
        blocked = gd.switch_plan(chain_id=1672, current_delay=600, **{**kw, 'pending_operation_ids': ['0x' + 'ab' * 32]})
        self.assertEqual((blocked['status'], blocked['code']), ('BLOCKED', 'PENDING_SHORT_DELAY_OPERATIONS'))
        self.assertEqual(blocked['unsignedCanCancel'][0]['safe'], self.C)


@unittest.skipUnless(READY, 'NOT RUN: needs the canonical build and the CampaignAnvilFixture topologies in this export')
class MainnetDelayPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import execution
        cls.ex = execution
        cls.r = execution.mainnet('A', waiting=True, name='test-delay-policy', then=cls.drill)
        cls.v = {c['id']: c for c in cls.r['checks'] if c['id'].startswith('DELAY.')}

    @staticmethod
    def drill(run, ctx):
        import safe_exec
        from models import truth
        chain, m = run.chain, run.m
        tl, safe, owners, threshold = ctx['p0']['timelock'], m['safe'], m['safeOwners'], m['safeThreshold']
        can = ctx['actors']['can']
        dist = ctx['p0']['distributorProxy']
        rows = []

        def gov(label, to, data, expect=True):
            _, checks, receipt, _ = safe_exec.execute(chain, f'DELAY.{label}', safe, owners, threshold,
                                                      safe_exec.safe_tx(to, data, 0), expect=expect)
            return int(receipt['status'], 16) == 1

        def scheduled(label, target, data, salt, delay, expect=True):
            return gov(f'{label}.schedule', tl, abi.encode_function(SCHEDULE, [target, 0, data, ZERO32, salt, delay]), expect)

        def executed(label, target, data, salt, expect=True):
            return gov(f'{label}.execute', tl, abi.encode_function(EXECUTE, [target, 0, data, ZERO32, salt]), expect)

        delay = lambda: chain.uint(tl, 'getMinDelay()')
        rows.append(truth('DELAY.postU.minDelay==600', delay() == 600, f'{delay()}'))
        # configuration operation at 600 s: not before ready, then executes
        keeper = '0x' + keccak256(b'faroo delay config keeper').lower()[-40:]
        set_keeper = abi.calldata('setKeeper(address)', keeper)
        s1 = '0x' + keccak256(b'faroo delay config 1')[2:]
        rows.append(truth('DELAY.config.schedule@600', scheduled('config', dist, set_keeper, s1, 600)))
        chain.warp(300)
        rows.append(truth('DELAY.config.executeBefore600.refused', not executed('config.early', dist, set_keeper, s1, expect=False)))
        chain.warp(301)
        rows.append(truth('DELAY.config.executeAfter600', executed('config', dist, set_keeper, s1)))
        rows.append(truth('DELAY.config.applied', chain.addr(dist, 'keeper()') == keeper))
        # unauthorized: nobody but the Timelock itself changes the delay
        upd = abi.encode_function(_fn('updateDelay', 'uint256'), [86400])
        rows.append(truth('DELAY.unauthorized.safeDirect.refused', not gov('direct', tl, upd, expect=False)))
        rows.append(truth('DELAY.unauthorized.eoaDirect.refused', chain.reverts(tl, upd, owners[0])))
        rows.append(truth('DELAY.unauthorized.unchanged', delay() == 600))
        # a short-delay operation pending before the switch blocks the plan until CAN cancels it
        s2 = '0x' + keccak256(b'faroo delay pending 2')[2:]
        other = abi.calldata('setKeeper(address)', '0x' + keccak256(b'faroo delay other keeper').lower()[-40:])
        scheduled('pending', dist, other, s2, 600)
        pending_id = gd.operation_id(dist, 0, other, ZERO32, s2)
        salt = '0x' + keccak256(b'faroo delay switch')[2:]
        blocked = gd.switch_plan(chain_id=1672, timelock=tl, gov_safe=safe, can_safe=can, salt=salt, current_delay=delay(),
                                 pending_operation_ids=[pending_id])
        rows.append(truth('DELAY.plan.blockedByPendingShortOperation', blocked['status'] == 'BLOCKED'
                          and blocked['pending'] == [pending_id]))
        # risk the precondition guards against (discarded snapshot): an earlier 600 s operation stays executable
        snap = chain.snapshot()
        plan_unchecked = gd.switch_plan(chain_id=1672, timelock=tl, gov_safe=safe, can_safe=can, salt=salt, current_delay=600,
                                        pending_operation_ids=[])
        gov('risk.switch.schedule', tl, plan_unchecked['safeTransactions'][0]['data'])
        chain.warp(601)
        gov('risk.switch.execute', tl, plan_unchecked['safeTransactions'][1]['data'])
        rows.append(truth('DELAY.risk.oldShortOperationStillExecutableAfterSwitch',
                          delay() == 86400 and executed('risk.old', dist, other, s2)))
        chain.revert(snap)
        _, rec = chain.send('DELAY.cancel.byCAN', can, tl, blocked['unsignedCanCancel'][0]['data'])
        rows.append(truth('DELAY.pending.cancelledByCAN', rec['status'] == 1 and not chain.boolean(tl, 'isOperation(bytes32)', pending_id)))
        plan = gd.switch_plan(chain_id=1672, timelock=tl, gov_safe=safe, can_safe=can, salt=salt, current_delay=delay(),
                              pending_operation_ids=[])
        op = plan['operationId']
        has = lambda role, who: chain.boolean(tl, 'hasRole(bytes32,address)', '0x' + keccak256(role)[2:], who)
        before = {'minDelay': delay(), 'govProposer': has(b'PROPOSER_ROLE', safe), 'govExecutor': has(b'EXECUTOR_ROLE', safe),
                  'canCanceller': has(b'CANCELLER_ROLE', can), 'pending': [], 'switchState': 'Unset'}
        rows.append(truth('DELAY.switch.checkBefore', gd.check_before(before, plan) == [], str(gd.check_before(before, plan))))
        # the switch: schedule with 600 s; a premature execute rolls back; after 600 s it executes
        rows.append(truth('DELAY.switch.schedule', gov('switch.schedule', tl, plan['safeTransactions'][0]['data'])))
        rows.append(truth('DELAY.switch.prematureExecute.refused', not gov('switch.early', tl, plan['safeTransactions'][1]['data'],
                                                                        expect=False) and delay() == 600))
        chain.warp(601)
        rows.append(truth('DELAY.switch.execute', gov('switch.execute', tl, plan['safeTransactions'][1]['data'])))
        # acceptance
        s3, s4 = '0x' + keccak256(b'faroo delay after 3')[2:], '0x' + keccak256(b'faroo delay after 4')[2:]
        short_refused = not scheduled('after.short', dist, set_keeper, s3, 600, expect=False)
        after = {'minDelay': delay(), 'switchState': 'Done' if chain.boolean(tl, 'isOperationDone(bytes32)', op) else 'not done',
                 'pending': [], 'shortScheduleRefused': short_refused}
        rows.append(truth('DELAY.switch.checkAfter', gd.check_after(after, plan) == [], str(gd.check_after(after, plan))))
        rows.append(truth('DELAY.after.longSchedule', scheduled('after.long', dist, set_keeper, s4, 86400)))
        chain.warp(601)
        rows.append(truth('DELAY.after.notReadyAt600', not executed('after.early', dist, set_keeper, s4, expect=False)))
        chain.warp(86400)
        rows.append(truth('DELAY.after.readyAt86400', executed('after', dist, set_keeper, s4)))
        run.add(*rows)
        return ['DELAY']

    def test_upgrade_ran_at_600(self):
        self.assertEqual(self.r['ledgers']['FS-1.A']['aggregate'], 'PASS')

    def test_delay_policy_rows(self):
        bad = {k: v['detail'] for k, v in self.v.items() if v['verdict'] != 'PASS'}
        self.assertEqual(bad, {})
        for rid in ('DELAY.postU.minDelay==600', 'DELAY.config.executeBefore600.refused', 'DELAY.config.executeAfter600',
                    'DELAY.unauthorized.safeDirect.refused', 'DELAY.unauthorized.eoaDirect.refused',
                    'DELAY.plan.blockedByPendingShortOperation', 'DELAY.risk.oldShortOperationStillExecutableAfterSwitch',
                    'DELAY.pending.cancelledByCAN', 'DELAY.switch.checkBefore', 'DELAY.switch.prematureExecute.refused',
                    'DELAY.switch.execute', 'DELAY.switch.checkAfter', 'DELAY.after.notReadyAt600', 'DELAY.after.readyAt86400'):
            self.assertIn(rid, self.v)


if __name__ == '__main__':
    unittest.main()
