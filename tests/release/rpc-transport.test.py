"""Offline response-correlation regressions; never contacts an RPC endpoint."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from lib.rpc import Client, RpcError

class CorrelationTests(unittest.TestCase):
    def client(self, response):
        c = Client('mainnet', max_retries=1)
        c._post = lambda body: response(body)
        return c

    def test_single_and_batch_valid_order_and_null(self):
        c = self.client(lambda b: {'jsonrpc':'2.0','id':b['id'],'result':None})
        self.assertIsNone(c.call('eth_chainId', []))
        c = self.client(lambda b: [{'jsonrpc':'2.0','id':r['id'],'result':r['params']} for r in reversed(b)])
        self.assertEqual(c.batch([('eth_call',[1]),('eth_call',[2])]), [[1],[2]])
        self.assertEqual(c.batch([]), [])

    def test_rejects_malformed_envelopes_without_retry(self):
        for body in [None, [], {}, {'jsonrpc':'1.0','id':1,'result':0}, {'jsonrpc':'2.0','id':True,'result':0}, {'jsonrpc':'2.0','id':'1','result':0}, {'jsonrpc':'2.0','id':2,'result':0}, {'jsonrpc':'2.0','id':1}, {'jsonrpc':'2.0','id':1,'result':0,'error':None}, {'jsonrpc':'2.0','id':1,'error':None}, {'jsonrpc':'2.0','id':1,'error':{'code':-32011}}]:
            with self.subTest(body=body):
                c = self.client(lambda b: body)
                with self.assertRaises(RpcError): c.call('eth_chainId', [])
                self.assertEqual(c.retries, 0)

    def test_batch_missing_unknown_duplicate_and_whole_rejection(self):
        good = {'jsonrpc':'2.0','id':1,'result':'ok'}
        for response in [[good], [good,good], [good,dict(good,id=99)], None, {'jsonrpc':'2.0','id':None,'error':{'code':-32011,'message':'retry'}}]:
            with self.subTest(response=response):
                c = self.client(lambda b: response)
                with self.assertRaises(RpcError): c.batch([('eth_chainId',[])]*2)
                self.assertEqual(c.retries, 0)

    @patch('lib.rpc.time.sleep')
    def test_bounded_retries_only_failed_correlated_items(self, sleep):
        requests = []
        def response(body):
            requests.append(body)
            return [{'jsonrpc':'2.0','id':r['id'], **({'error':{'code':-32011,'message':'busy'}} if len(requests)==1 and i==1 else {'result':r['params']})} for i,r in enumerate(body)]
        c = self.client(response)
        self.assertEqual(c.batch([('eth_call',[1]),('eth_call',[2])]), [[1],[2]])
        self.assertEqual([len(r) for r in requests], [2,1])
        c = self.client(lambda b: [{'jsonrpc':'2.0','id':r['id'],'error':{'code':-32011,'message':'busy'}} for r in b])
        with self.assertRaises(RpcError): c.batch([('eth_call',[])])
        self.assertEqual(c.retries,1)
        c = self.client(lambda b: {'jsonrpc':'2.0','id':b['id'],'error':{'code':-32011,'message':'busy'}})
        with self.assertRaises(RpcError): c.call('eth_call',[])
        self.assertEqual(c.retries,1)

if __name__ == '__main__': unittest.main()
