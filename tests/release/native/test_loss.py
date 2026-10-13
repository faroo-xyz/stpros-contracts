"""Reviewed LOSS recovery evidence; no application-operation interfaces."""
import asyncio
import gzip
import hashlib
import json
from pathlib import Path
import sys
import unittest
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path[:0]=[str(HERE),str(ROOT)]
from gate import pack, revive, canonical, error_value
from tools.lib import abi, release_json as j
from tools.release.native import loss
from tools.release.native import __main__ as cli
from tools.release.native.rpc import TransportError
from unittest.mock import patch

class LossTape:
    def __init__(self, rec):
        self.rec,self.callback_index,self.clock_index=rec,0,0
        self.events=[]

    def callback(self, label, synchronous=False):
        def start(args):
            index=self.callback_index
            if index>=len(self.rec['callbacks']):raise AssertionError('Unexpected callback')
            row=self.rec['callbacks'][index]
            if row['label']!=label or canonical(pack(list(args)))!=canonical(row['args']):raise AssertionError('Callback order/arguments mismatch')
            self.callback_index+=1;self.events.append({'kind':'callback-start','index':index})
            return row
        def finish(row):
            self.events.append({'kind':'callback-end','index':row['order']})
            if row['outcome']=='throw':raise TransportError(row['error']['name'],row['error']['message'])
            return revive(row['value'])
        if synchronous:
            return lambda *args:finish(start(args))
        async def call(*args):
            row=start(args);await asyncio.sleep(0);return finish(row)
        return call

    def clock(self):
        index=self.clock_index
        if index>=len(self.rec['clocks']):raise AssertionError('Unexpected clock')
        self.events.append({'kind':'clock','index':index});self.clock_index+=1
        return self.rec['clocks'][index]

class LossTests(unittest.TestCase):
    def test_frozen_security_and_recovery_cases(self):
        raw=(ROOT/'tests/fixtures/native-release/loss.json.gz').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'fe8667e2e2a49936a500c893f659243fbf55e59d62c0cdc2e3b2a6f2b661859b')
        rows=json.loads(gzip.decompress(raw))
        async def check(rec):
            tape=LossTape(rec)
            args=[tape.callback(x['$callback'],i==3) if isinstance(x,dict) and '$callback' in x else revive(x) for i,x in enumerate(rec['arguments'])]
            try:
                value=loss.build_loss_contingency(*args) if rec['function'].endswith('buildLossContingency') else await loss.observe_loss_contingency(*args)
                actual={'outcome':'return','value':pack(value)}
            except Exception as error:actual={'outcome':'throw','error':error_value(error)}
            expected={'outcome':'return','value':rec['value']} if rec['outcome']=='return' else {'outcome':'throw','error':rec['error']}
            self.assertEqual(canonical(actual),canonical(expected),rec['id'])
            self.assertEqual(tape.events,rec['events'],rec['id'])
            self.assertEqual(tape.callback_index,len(rec['callbacks']))
            self.assertEqual(tape.clock_index,len(rec['clocks']))
        for rec in rows:
            with self.subTest(case=rec['id']):asyncio.run(check(rec))

    def test_approval_required_before_IO(self):
        with patch.object(j,'checked_json',side_effect=AssertionError('Unexpected I/O')):
            with self.assertRaises(j.ReleaseError):asyncio.run(cli.run_loss())

    def test_wrong_timing_rule_is_detected(self):
        # A closed scheduling window must remain closed at the actual boundary.
        with self.assertRaises(j.ReleaseError):loss.require_scheduling_window(101,10,110)
        loss.require_scheduling_window(100,10,110)


class IndependentLossBoundaryTests(unittest.TestCase):
    """Independent boundaries; expected values are computed here, never read from the tape.

    The recorded case 0001 supplies only well-formed synthetic inputs. Timing follows the
    contract: proposal valid iff now + settleTimelockWindow <= D, D = maturity + 30 days.
    """
    GRACE = 2592000

    def setUp(self):
        raw = (ROOT/'tests/fixtures/native-release/loss.json.gz').read_bytes()
        rows = json.loads(gzip.decompress(raw))
        base = next(r for r in rows if r['id'] == 'loss-contingency.test.mjs:0001')
        self.plan, self.state, self.clock = revive(base['arguments'])
        self.now = int(self.clock)

    def build(self):
        return loss.build_loss_contingency(self.plan, self.state, self.clock)

    def blocked(self, code):
        with self.assertRaises(j.ReleaseError) as raised:
            self.build()
        self.assertEqual(str(raised.exception), 'BLOCKED — '+code)

    def set_round(self, **values):
        for name, value in values.items():
            self.plan['round'][name] = str(value)
        self.state['round'] = json.loads(json.dumps(self.plan['round']))
        r = self.plan['round']
        self.state['settlementDeadline'] = str(int(r['openDeadline'])+int(r['lockDuration'])+self.GRACE)

    def timing(self):
        r = self.plan['round']
        maturity = int(r['openDeadline'])+int(r['lockDuration'])
        cutoff = maturity+self.GRACE-int(r['settleTimelockWindow'])
        ready = max(self.now, int(self.state['timestamp']))+int(self.plan['minDelay'])
        return maturity, ready, cutoff

    def test_readiness_dominates_and_exact_margin_boundary(self):
        maturity, ready, cutoff = self.timing()
        self.assertGreater(ready, maturity)
        self.plan['marginSeconds'] = str(cutoff-ready)
        out = self.build()['timing']
        self.assertEqual(int(out['earliestReadyIfScheduledNow']), ready)
        self.assertEqual(int(out['earliestExecutionIfScheduledNow']), ready)
        self.assertEqual(int(out['proposalCutoff']), cutoff)
        self.assertEqual(int(out['latestScheduleAt']), cutoff-int(self.plan['minDelay'])-(cutoff-ready))
        self.plan['marginSeconds'] = str(cutoff-ready+1)
        self.blocked('LOSS_SCHEDULING_WINDOW_CLOSED')

    def test_maturity_later_than_readiness_sets_execution(self):
        # Observed while LOCKED but before maturity: execution = max(ready, maturity) = maturity.
        self.set_round(openDeadline=self.now-100, openedAt=self.now-100-86400, lockDuration=10*86400)
        maturity, ready, cutoff = self.timing()
        self.assertGreater(maturity, ready)
        self.plan['marginSeconds'] = str(cutoff-maturity)
        out = self.build()['timing']
        self.assertEqual(int(out['earliestReadyIfScheduledNow']), ready)
        self.assertEqual(int(out['earliestExecutionIfScheduledNow']), maturity)
        self.assertEqual(int(out['D']), maturity+self.GRACE)
        self.plan['marginSeconds'] = str(cutoff-maturity+1)          # passes only if readiness were used
        self.assertLessEqual(ready+cutoff-maturity+1, cutoff)
        self.blocked('LOSS_SCHEDULING_WINDOW_CLOSED')

    def test_review_lifetime_is_at_most_3600_seconds(self):
        reviewed = int(self.plan['reviewedAt'])
        self.plan['expiresAt'] = str(reviewed+3600)
        self.build()
        self.plan['expiresAt'] = str(reviewed+3601)
        self.blocked('STALE_REVIEW')

    def test_expired_and_future_dated_approvals(self):
        self.plan['expiresAt'] = str(self.now)
        self.build()                                                    # expiry instant still valid
        self.plan['expiresAt'] = str(self.now-1)
        self.blocked('STALE_REVIEW')
        self.plan['reviewedAt'], self.plan['expiresAt'] = str(self.now+1), str(self.now+100)
        self.blocked('STALE_REVIEW')
        self.plan['reviewedAt'], self.plan['expiresAt'] = str(self.now), str(self.now)
        self.build()

    def test_any_reviewed_round_field_drift_blocks(self):
        for name in loss.ROUND_FIELDS:
            with self.subTest(field=name):
                self.setUp()
                self.state['round'][name] = str(int(self.state['round'][name])+1)
                self.blocked('ROUND_DRIFT')

    def test_frozen_operation_identity_drift_blocks(self):
        # Pinned IDs were recomputed with an independent encoder (Foundry cast abi-encode +
        # keccak, research attack-lab/final-tree-review/evidence/id-witness.log).
        a = '0xf8fcd9159e3ba81b204c860f2be60d4c21f1e8d5568210b05011efeeb991afae'
        b = '0x5a5d7727f9191fd9d5552f9c9cf430425c6ba0e2942df661ed81cb4428e681f3'
        out = self.build()
        self.assertEqual((out['paths']['A']['id'], out['paths']['B']['id']), (a, b))
        self.plan['frozenOperationIds'] = {'A': a, 'B': b}
        self.build()
        for path in ('A', 'B'):
            with self.subTest(path=path):
                self.plan['frozenOperationIds'] = {'A': a, 'B': b, path: '0x'+'11'*32}
                self.blocked('FROZEN_OPERATION_ID_MISMATCH')
        self.plan['frozenOperationIds'] = {'A': a, 'B': b}
        self.plan['approvedLossAmount'] = str(50*10**18-1)               # changed payload, old frozen IDs
        self.blocked('FROZEN_OPERATION_ID_MISMATCH')
        self.plan['approvedLossAmount'] = str(50*10**18)
        self.plan['saltB'] = '0x'+'00'*31+'16'
        self.blocked('FROZEN_OPERATION_ID_MISMATCH')


if __name__=='__main__':unittest.main()
