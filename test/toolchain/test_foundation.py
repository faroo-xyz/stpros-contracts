"""Upgrade primitive goldens and input/RPC boundaries; original references archived."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.lib import abi, release_json as j
from tools.lib.keccak import keccak256
from tools.release.native import rpc


def revive(x):
    if isinstance(x,dict) and set(x)=={'$bigint'}:return abi.BigInt(x['$bigint'])
    if isinstance(x,list):return [revive(v) for v in x]
    if isinstance(x,dict):return {k:revive(v) for k,v in x.items()}
    return x


def pack(x):
    if x is abi.UNDEFINED:return {'$undefined':True}
    if isinstance(x,abi.BigInt):return {'$bigint':str(x)}
    if isinstance(x,list):return [pack(v) for v in x]
    if isinstance(x,dict):return {k:pack(v) for k,v in x.items()}
    return x


def evaluate(c):
    if c['op']=='hash':return keccak256(bytes.fromhex(c['data'][2:]))
    if c['op']=='function':return abi.encode_function(c['abi'],revive(c['values']))
    if c['op']=='encode':return abi.encode_parameters(c['params'],revive(c['values']))
    if c['op']=='decode':return pack(abi.decode_parameters(c['params'],c['data']))
    raise AssertionError('Unrecognized primitive')


class FoundationTests(unittest.TestCase):
    def test_original_upgrade_primitive_values(self):
        raw=(ROOT/'test/fixtures/native-foundation/primitives.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'63110d61809d00b2ff370e2dc136e5a501df01ae792efa935df6a370c34a769e')
        for row in json.loads(raw):
            c,r=row['input'],row['expected']
            with self.subTest(case=c['id']):
                if r['outcome']=='return':self.assertEqual(evaluate(c),r['value'])
                else:
                    with self.assertRaises(abi.AbiError):evaluate(c)

    def test_two_reviewed_typed_byte_admission_deviations(self):
        for value in ['0xa','0xgg']:
            with self.assertRaises(abi.AbiError):abi.encode_parameters([{'type':'bytes'}],[value])

    def test_keccak_is_not_sha3(self):
        raw=bytes(range(136))
        self.assertNotEqual(keccak256(raw),'0x'+hashlib.sha3_256(raw).hexdigest())

    def test_json_types_unicode_and_raw_approval(self):
        self.assertEqual(j.canonical(j.parse_json('{"n":9007199254740993}')),'{"n":9007199254740992}')
        self.assertEqual(j.canonical({'n':abi.BigInt(9007199254740993)}),'{"n":"9007199254740993"}')
        self.assertNotEqual(j.canonical({}),j.canonical({'x':None}))
        self.assertNotEqual(j.canonical({'x':False}),j.canonical({'x':0}))
        self.assertEqual(j.wire_json({'2':2,'1':1,'z':'a'}),'{"1":1,"2":2,"z":"a"}')
        with self.assertRaises(TypeError):j.wire_json({'x':abi.BigInt(1)})
        for token in ['NaN','Infinity','-Infinity']:
            with self.assertRaises(ValueError):j.parse_json(token)

    def test_approved_path_rejects_before_open(self):
        with patch.object(Path,'read_bytes',side_effect=AssertionError('unexpected content access')):
            for path in ['/private/tmp/.blocked/value.json','/private/tmp/keys/value.json']:
                with self.subTest(path=path),self.assertRaisesRegex(j.ReleaseError,'UNSAFE_INPUT_PATH'):j.checked_json(path)
            with self.assertRaisesRegex(j.ReleaseError,'INDEPENDENT_REVIEW_HASH_REQUIRED'):j.approved_json('/private/tmp/unread.json',None)

    def test_transport_no_redirect_and_no_environment_proxy(self):
        with self.assertRaises(rpc.TransportError):rpc._NoRedirect().redirect_request(None,None,302,'',{},'https://example.invalid')
        captured=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self):return b'{"jsonrpc":"2.0","id":1,"result":"0x688"}'
        class Opener:
            def open(self,request,timeout):captured.append((request.full_url,request.data,timeout));return Response()
        def opener(*handlers):
            self.assertEqual(handlers[0].proxies,{})
            self.assertIsInstance(handlers[1],rpc._NoRedirect)
            return Opener()
        with patch.object(rpc.urllib.request,'build_opener',opener):
            value=asyncio.run(rpc.ReleaseRpc('HTTPS://RPC.PHAROS.XYZ:443/')('eth_chainId',[]))
        self.assertEqual(value,'0x688');self.assertEqual(captured[0][0],'https://rpc.pharos.xyz/');self.assertEqual(captured[0][2],30)

    def test_pinned_params_never_replaced_by_latest(self):
        calls=[]
        async def post(url,body,timeout):
            calls.append(body);return 200,{'jsonrpc':'2.0','id':body['id'],'result':'0x'}
        c=rpc.ReleaseRpc('http://localhost:8545',True,post=post)
        asyncio.run(c('eth_getCode',['0x'+'12'*20,'0x123']))
        self.assertEqual(calls[0]['params'][-1],'0x123')

if __name__=='__main__':unittest.main()
