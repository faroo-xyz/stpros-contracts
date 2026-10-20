"""V4 mainnet release delay policy (owner policy of 2026-10-20) on the native release entry points.

A mainnet (1672) release config is deployable only with ``policy.timelockDelayPolicy = 'V4-CONFIG-600'`` and
``minDelay = 600``. A missing profile, the legacy 172,800 s, the post-switch 86,400 s or any other value is refused
before validateConfig, preflight, build-handoff, build-u or deploymentDefinitions produce anything. Simulation
configs may keep the legacy policy and are never deployable.

Inputs are the frozen corpus's own configs (``gate.records``), so nothing here is hand-built.
"""
import copy
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import inputs, plans  # noqa: E402

CODE = 'POLICY_DELAY_PROFILE'


def approved_governance(config):
    """One role-valid V4 topology (GOV = OPS, CAN = Guardian). V4 accepts any role-valid topology and never reads
    Safe internals; the Safe records set here are inert."""
    a, safes = config['actors'], config.get('review', {}).get('safes')
    a['ops'], a['guardian'] = a['gov'], a['can']
    if safes:
        safes['can'] = {**safes['can'], 'threshold': 2, 'owners': ['0x' + format(0xc000 + i, '040x') for i in range(5)]}
        safes['ops'], safes['guardian'] = copy.deepcopy(safes['gov']), copy.deepcopy(safes['can'])
    return config


def corpus():
    _, declared = gate.deviations()
    rows = list(gate.records())
    return rows, declared


class V4MainnetReleasePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.declared = corpus()
        by_id = {r['id']: r for r in rows}
        # a mainnet release preflight case the R4 engine accepted (it is a declared V4 refusal now)
        cls.release = next(r for r in rows if r['id'] in cls.declared and r['function'] == 'core.mjs.preflight'
                           and r['outcome'] == 'return')
        cls.args = gate.revive(cls.release['arguments'])
        # a mainnet simulation preflight case the R4 engine accepted (legacy policy, kept)
        cls.simulation = next(r for r in rows if r['function'] == 'core.mjs.preflight' and r['outcome'] == 'return'
                              and gate.revive(r['arguments'])[0].get('mode') == 'simulation'
                              and gate.revive(r['arguments'])[0].get('chainId') == 1672)
        cls.by_id = by_id

    def config(self, profile, delay, mode='release'):
        config = copy.deepcopy(self.args[0])
        config['mode'] = mode
        config['policy'] = dict(config['policy'])
        config['policy'].pop('timelockDelayPolicy', None)
        if profile is not None:
            config['policy']['timelockDelayPolicy'] = profile
            if profile == 'V4-CONFIG-600':
                approved_governance(config)      # a role-valid V4 topology
        config['policy']['minDelay'] = delay
        return config

    def refused(self, code, fn, *args):
        with self.assertRaises(j.ReleaseError) as raised:
            fn(*args)
        self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    def test_validate_config_values(self):
        for profile, delay, code in ((None, 172800, CODE), (None, 600, CODE), (None, 86400, CODE), ('UNKNOWN', 600, CODE),
                                     ('V4-CONFIG-600', 172800, 'POLICY_DELAY'), ('V4-CONFIG-600', 86400, 'POLICY_DELAY'),
                                     ('V4-CONFIG-600', 300, 'POLICY_DELAY')):
            with self.subTest(profile=profile, delay=delay):
                self.refused(code, inputs.validate_config, self.config(profile, delay))
        inputs.validate_config(self.config('V4-CONFIG-600', 600))           # the only admitted mainnet release policy

    def test_every_release_entry_point_refuses_the_legacy_policy(self):
        inventory, artifacts = self.args[1], self.args[2]
        for profile, delay in ((None, 172800), (None, 600), ('V4-CONFIG-600', 86400)):
            config = self.config(profile, delay)
            context = inputs.ReleaseContext(config, inventory, artifacts)
            code = CODE if profile is None else 'POLICY_DELAY'
            for name, fn, args in (('preflight', inputs.preflight, (config, inventory, artifacts)),
                                   ('build-handoff', plans.build_handoff, (context,)),
                                   ('build-u', plans.build_u, (context,)),
                                   ('deploymentDefinitions', plans.deployment_definitions, (config, artifacts))):
                with self.subTest(entry=name, profile=profile, delay=delay):
                    self.refused(code, fn, *args)

    def test_declared_corpus_refusals_are_mainnet_release_only(self):
        for case_id in self.declared:
            rec = self.by_id[case_id]
            if not rec['function'].startswith('core.mjs.'):
                continue
            config = gate.revive(rec['arguments'])[0]
            self.assertEqual((config['mode'], config['chainId']), ('release', 1672))
            self.assertNotIn('timelockDelayPolicy', config['policy'])

    def test_legacy_simulation_is_never_deployable(self):
        args = gate.revive(self.simulation['arguments'])
        out = inputs.preflight(*args)
        self.assertEqual((out['status'], out['deployable'], out['simulationOnly']), ('PASS', False, True))
        self.assertTrue(plans.deployment_definitions(args[0], args[2])['simulationOnly'])


if __name__ == '__main__':
    unittest.main()
