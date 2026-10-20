"""V4 governance in the native release and LOSS validation: roles and permissions only (owner decision, 2026-10-20).

- GOV may equal OPS or not; CAN may equal the Guardian or not.
- GOV != CAN, the Guardian is never GOV, OPS is never CAN / Guardian, and no role is the Timelock (the
  ReleaseAssertions ACTOR_SEPARATION rule).
- Safe internals (code, singleton, owners, threshold, modules, guard, nonce, signer evidence) are not required and
  not read.
- A frozen R4 config (no ``timelockDelayPolicy``) keeps the R4 rules, so the frozen corpus replays exactly.

The base config is the corpus's own accepted mainnet release preflight config, relabelled to the V4 policy, with the
owner-supplied mainnet addresses (``tools/release/mainnet-governance.json``).
"""
import asyncio
import copy
import gzip
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import checkpoint, distributor_predeploy as dp, inputs, loss  # noqa: E402

REAL = dp.governance_inputs()


def other(n):
    return '0x' + format(0xbeef00 + n, '040x')


class V4GovernanceRolesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, declared = gate.deviations()
        rec = next(r for r in gate.records() if r['id'] in declared and r['function'] == 'core.mjs.preflight'
                   and r['outcome'] == 'return')
        cls.r4 = gate.revive(rec['arguments'])[0]

    def v4(self, **roles):
        """The mainnet V4 release config with the real governance addresses and no Safe data at all."""
        c = copy.deepcopy(self.r4)
        c['policy'] = {**c['policy'], 'timelockDelayPolicy': 'V4-CONFIG-600', 'minDelay': 600}
        for role in ('gov', 'ops', 'can', 'guardian', 'deployer'):
            c['actors'][role] = REAL[role]
        c['actors'].update(roles)
        c['review'].pop('safes', None)
        return c

    def refused(self, code, config):
        with self.assertRaises(j.ReleaseError) as raised:
            inputs.validate_config(config)
        self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    def test_real_mainnet_addresses_give_a_valid_config(self):
        self.assertEqual((REAL['gov'], REAL['ops']), ('0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca',) * 2)
        self.assertEqual(REAL['can'], '0xb3c8a7f291e128e5d1ff3e2aa09fe8a9b38fce76')
        self.assertEqual(REAL['guardian'], '0xa046988889871c6c7abc69e63cccf9adb3e57ef5')
        self.assertEqual(REAL['deployer'], '0x8ce84e9fa0101d317d8956d73610ad3e0e219d41')
        self.assertIsNone(REAL['deployerNonce'])                         # never invented
        c = self.v4()
        self.assertEqual(inputs.validate_config(c)['actors']['can'], REAL['can'])
        self.assertNotIn('safes', c['review'])                          # no Safe data needed

    def test_can_guardian_and_gov_ops_may_be_equal_or_not(self):
        for roles in ({}, {'guardian': REAL['can']}, {'ops': other(1)}, {'ops': other(1), 'guardian': REAL['can']}):
            with self.subTest(**roles):
                inputs.validate_config(self.v4(**roles))

    def test_business_separation_stays(self):
        for roles in ({'can': REAL['gov']}, {'guardian': REAL['gov']}, {'ops': REAL['can']}, {'ops': REAL['guardian']}):
            with self.subTest(**roles):
                self.refused('ACTOR_SEPARATION', self.v4(**roles))
        c = self.v4(); c['actors']['can'] = c['addresses']['timelock']
        self.refused('ACTOR_SEPARATION', c)

    def test_no_safe_internals_are_required_or_read(self):
        c = self.v4()
        # any Safe metadata is irrelevant, even nonsense values (nothing reads it)
        c['review']['safes'] = {'gov': {'threshold': 0, 'owners': []}}
        inputs.validate_config(c)

        async def no_rpc(method, params):
            raise AssertionError(f'Safe state was read: {method}')
        for check_nonce in (True, False):
            self.assertIsNone(asyncio.run(checkpoint.check_safes(self.v4(), no_rpc, '0x1', check_nonce)))

    def test_r4_configs_keep_the_r4_rules(self):
        c = copy.deepcopy(self.r4); c['mode'] = 'simulation'
        inputs.validate_config(c)
        for change in (lambda a: a.update(ops=a['gov']), lambda a: a.update(guardian=a['can'])):
            c = copy.deepcopy(self.r4); c['mode'] = 'simulation'; change(c['actors'])
            self.refused('ACTOR_SEPARATION', c)


class V4LossRolesTest(unittest.TestCase):
    """LOSS under V4: operator = GOV or not, Guardian = CAN or not; the plan still runs only through the Timelock."""

    def setUp(self):
        rows = json.loads(gzip.decompress((ROOT/'tests/fixtures/native-release/loss.json.gz').read_bytes()))
        base = next(r for r in rows if r['id'] == 'loss-contingency.test.mjs:0001')
        self.plan, self.state, self.clock = gate.revive(base['arguments'])

    def v4(self, **roles):
        p = copy.deepcopy(self.plan)
        p.update(chainId=1672, timelockDelayPolicy='V4-CONFIG-600', **roles)
        return p

    def refused(self, code, plan):
        with self.assertRaises(j.ReleaseError) as raised:
            loss.validate_plan(plan, int(self.clock))
        self.assertIn(code, str(raised.exception))

    def test_v4_roles(self):
        p = self.plan
        for roles in ({}, {'operator': p['gov']}, {'guardian': p['can']}, {'operator': p['gov'], 'guardian': p['can']}):
            with self.subTest(**roles):
                loss.validate_plan(self.v4(**roles), int(self.clock))
        self.refused('AUTHORITY_ALIAS', self.v4(can=p['gov']))
        self.refused('AUTHORITY_ALIAS', self.v4(guardian=p['gov']))
        self.refused('OPERATOR_ALIAS', self.v4(operator=p['can']))
        self.refused('OPERATOR_ALIAS', self.v4(operator=p['guardian']))
        self.refused('OPERATOR_ALIAS', self.v4(operator=p['timelock']))

    def test_r4_plan_keeps_r4_rules(self):
        p = copy.deepcopy(self.plan); p['operator'] = p['gov']
        self.refused('OPERATOR_ALIAS', p)


if __name__ == '__main__':
    unittest.main()
