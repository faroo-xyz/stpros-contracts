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
        raw=(ROOT/'test/fixtures/native-release/loss.json.gz').read_bytes()
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


if __name__=='__main__':unittest.main()
