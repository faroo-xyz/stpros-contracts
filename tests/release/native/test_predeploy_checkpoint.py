"""The H and R4 checkpoints re-prove a reused SlpDistributor pre-deployment (``checkpoint.verify_predeployed``).

Witnesses: the frozen passing ``verifyCheckpoint`` cases of stage H and stage R4, replayed with their recorded RPC
transcripts under the gate's R4 conformance (they are mainnet release cases without the V4 delay profile).
``verify_predeployed`` is replaced by a probe that records its call and either passes or raises the failure a
chain read would raise. So these tests prove that each checkpoint calls it once, with its own checkpoint reader and
block, before it can succeed, and that every failure stops the checkpoint. The function's chain logic itself is
tested against a loopback node in ``tests/fork/driver/test_distributor_predeploy.py``.
"""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import checkpoint  # noqa: E402

EXECUTION_H = 'read-only.integration.test.mjs:0074'       # verifyCheckpoint stage H (frozen U pending)
POSTCHECK_R4 = 'read-only.integration.test.mjs:0092'      # verifyCheckpoint stage R4 (U Done)
FAILURES = ('PREDEPLOY_NOT_MINED', 'PREDEPLOY_AFTER_CHECKPOINT', 'PREDEPLOY_TRANSACTION', 'PREDEPLOY_EVIDENCE')


class CheckpointPredeployTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r4 = gate.r4_conformance(); r4.start(); cls.addClassCleanup(r4.stop)
        wanted = {EXECUTION_H, POSTCHECK_R4}
        cls.cases = {rec['id']: rec for rec in gate.records() if rec['id'] in wanted}
        assert set(cls.cases) == wanted, 'checkpoint witnesses missing from the frozen corpus'

    def replay(self, case_id, probe):
        with patch.object(checkpoint, 'verify_predeployed', probe):
            return asyncio.run(gate.run_case(self.cases[case_id]))

    def test_each_checkpoint_calls_it_with_its_own_reads(self):
        for case_id, stage in ((EXECUTION_H, 'H'), (POSTCHECK_R4, 'R4')):
            sequence, original = [], checkpoint.check_proxy

            async def probe(config, reader, block):
                sequence.append(('predeploy', reader, block, config))

            async def check_proxy(rpc, block, *args):
                sequence.append(('proxy', rpc, block, None))
                return await original(rpc, block, *args)
            with patch.object(checkpoint, 'check_proxy', check_proxy):
                row = self.replay(case_id, probe)
            with self.subTest(stage=stage):
                self.assertTrue(row['pass'], row['actual'])
                probes = [i for i, e in enumerate(sequence) if e[0] == 'predeploy']
                self.assertEqual(len(probes), 1)
                _, reader, block, config = sequence[probes[0]]
                self.assertEqual(config['chainId'], 1672)
                # the checkpoint's own proxy reads that follow use the same reader and the same head block
                after = [e for e in sequence[probes[0] + 1:] if e[0] == 'proxy']
                self.assertTrue(after)
                self.assertTrue(all(e[1] is reader and e[2] == block for e in after))

    def test_unmodified_checkpoint_still_passes(self):
        async def probe(config, reader, block):
            return None
        for case_id in (EXECUTION_H, POSTCHECK_R4):
            with self.subTest(case=case_id):
                self.assertTrue(self.replay(case_id, probe)['pass'])

    def test_every_failure_stops_the_checkpoint(self):
        for case_id in (EXECUTION_H, POSTCHECK_R4):
            for code in FAILURES:
                async def probe(config, reader, block, code=code):
                    raise j.ReleaseError(code, 'step 5')
                row = self.replay(case_id, probe)
                with self.subTest(case=case_id, code=code):
                    self.assertFalse(row['pass'])
                    self.assertEqual(row['actual']['outcome'], 'throw')
                    self.assertTrue(row['actual']['error']['message'].startswith(code), row['actual'])


if __name__ == '__main__':
    unittest.main()
