"""Focused primitive/corpus/admission regressions; no real RPC or signer."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tools/lib'),str(ROOT/'test/toolchain/equivalence')]
from tools.lib import abi, release_json as j, common
from tools.release.native import rpc
import candidate
import run as harness
import fixture_guard


class FoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_guard.prepare()
        cls.lock,cls.cases,cls.reference=harness.verify()
        cls.byid={c['id']:c for c in cls.cases};cls.expected={c['id']:c for c in cls.reference}

    def test_frozen_complete_case_set(self):
        self.assertEqual(len(self.cases),192)
        self.assertEqual([x['id'] for x in self.cases],[x['id'] for x in self.reference])
        self.assertTrue(any(x['outcome']=='throw' for x in self.reference))
        self.assertEqual(set(c['op'] for c in self.cases),{'hash','encode','decode','function','event','canonical','file','rpc'})

    def test_all_frozen_differential_cases(self):
        async def execute():
            for c,expected in zip(self.cases,self.reference):
                with self.subTest(case=c['id']):
                    actual=await candidate.run(c)
                    self.assertTrue(harness.comparison(expected,actual),str(actual)[:700])
        asyncio.run(execute())

    def test_additive_rpc_cases(self):
        directory=ROOT/'test/toolchain/equivalence'
        lock=json.loads((directory/'supplement-lock.json').read_text())
        for name,digest in lock['files'].items():self.assertEqual(harness.sha((directory/name).read_bytes()),digest)
        cases=json.loads((directory/'supplement-cases.json').read_text());expected=json.loads((directory/'supplement-reference.json').read_text())['results']
        async def execute():
            for c,r in zip(cases,expected):
                with self.subTest(case=c['id']):self.assertTrue(harness.comparison(r,await candidate.run(c)))
        asyncio.run(execute())

    def test_all_uint_widths_and_URL_normalization(self):
        directory=ROOT/'test/toolchain/equivalence'
        lock=json.loads((directory/'extended-lock.json').read_text())
        for name,digest in lock['files'].items():self.assertEqual(harness.sha((directory/name).read_bytes()),digest)
        cases=json.loads((directory/'width-url-cases.json').read_text())
        expected=json.loads((directory/'width-url-reference.json').read_text())['results']
        self.assertEqual(len(cases),134)
        async def execute():
            for c,r in zip(cases,expected):
                with self.subTest(case=c['id']):
                    actual=await candidate.run(c)
                    self.assertTrue(harness.comparison(r,actual),str(actual)[:500])
        asyncio.run(execute())

    def test_original_private_pinned_function_equivalence(self):
        directory=ROOT/'test/toolchain/equivalence'
        cases=json.loads((directory/'pin-cases.json').read_text())
        reference=json.loads((directory/'pin-reference.json').read_text())
        self.assertEqual(reference['sourceSha256'],harness.sha((ROOT/'tasks/release/read-only.mjs').read_bytes()))
        async def execute():
            for c,expected in zip(cases,reference['results']):
                trace=[]
                async def read(method,params):
                    trace.append({'method':method,'params':params})
                    return c['observedChain'] if method=='eth_chainId' else c['header']
                with self.subTest(case=c['id']):
                    try:
                        value=await rpc.pinned(c['chainId'],read,c['tag'])
                        actual={'id':c['id'],'outcome':'return','value':value,'outputBytes':candidate.wire(value).encode().hex(),'trace':trace}
                    except j.ReleaseError as e:
                        actual={'id':c['id'],'outcome':'throw','error':{'name':'Error','message':str(e)},'trace':trace}
                    self.assertEqual(actual,expected)
        asyncio.run(execute())

    def test_distinct_assertion_fields(self):
        directory=ROOT/'test/toolchain/equivalence'
        lock=json.loads((directory/'distinct-lock.json').read_text())
        for name,digest in lock.items():self.assertEqual(harness.sha((ROOT/'test'/name).read_bytes()),digest)
        c=json.loads((directory/'distinct-cases.json').read_text())[0]
        expected=json.loads((directory/'distinct-reference.json').read_text())['results'][0]
        self.assertTrue(harness.comparison(expected,asyncio.run(candidate.run(c))))
        decoded=abi.decode_parameters(c['abi']['inputs'],'0x'+expected['value'][10:])
        self.assertEqual(j.canonical(candidate.pack(decoded)),j.canonical(c['values']))

    def test_function_decode_and_undefined_arguments(self):
        directory=ROOT/'test/toolchain/equivalence'
        lock=json.loads((directory/'decode-lock.json').read_text())
        for name,digest in lock.items():self.assertEqual(harness.sha((directory/name).read_bytes()),digest)
        cases=json.loads((directory/'decode-cases.json').read_text())
        expected=json.loads((directory/'decode-reference.json').read_text())['results']
        self.assertEqual(len(cases),11)
        for c,r in zip(cases,expected):
            with self.subTest(case=c['id']):
                try:
                    value=candidate.pack(abi.decode_function(c['abi'],c['data']))
                    self.assertEqual(r['outcome'],'return')
                    self.assertEqual(candidate.wire(value).encode().hex(),r['outputBytes'])
                except abi.AbiError as error:
                    self.assertEqual(r['outcome'],'throw');self.assertEqual(error.code,r['error']['name'])

    def test_missing_undefined_and_null_domains(self):
        d=ROOT/'test/toolchain/equivalence'
        lock=json.loads((d/'undefined-lock.json').read_text())
        for name,digest in lock.items():self.assertEqual(harness.sha((d/name).read_bytes()),digest)
        values=[abi.UNDEFINED,{'x':abi.UNDEFINED},[abi.UNDEFINED,None],{'x':None}]
        result=[]
        for v in values:
            c,w=j.canonical(v),j.wire_json(v)
            result.append({'undefined':c is abi.UNDEFINED,'text':None if c is abi.UNDEFINED else c,'wire':None if w is abi.UNDEFINED else w})
        self.assertEqual(result,json.loads((d/'undefined-reference.json').read_text()))

    def test_complete_accounting_and_reviewed_admission(self):
        groups=[g for g in harness.load_groups() if g[0] not in ('spelling','hex')]
        all_results=asyncio.run(harness.full_comparison())
        actual={x['case']:x['candidate'] for x in all_results['ledger'] if not x['case'].startswith(('spelling:','hex:'))}
        report=harness.classify(groups,actual,harness.load_policy())
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['cases'],358)
        self.assertEqual(report['exactMatches'],356)
        self.assertEqual(report['admissionDeviations'],2)
        self.assertEqual(report['unexplainedMismatches'],0)
        self.assertEqual({x['case'] for x in report['ledger'] if x['classification']=='native-admission-deviation'},
                         {'admission:admission-odd-bytes','admission:admission-nonhex-bytes'})

    def test_canonical_byte_spelling_exact_reference(self):
        # Previously blocking cases remain exact, case-sensitive regressions.
        group=next(g for g in harness.load_groups() if g[0]=='spelling')
        for c,r in zip(group[1],group[2]):
            with self.subTest(case=c['id']):
                actual=asyncio.run(harness.evaluate('spelling',c))
                self.assertTrue(harness.comparison(r,actual),'Canonical byte spelling differs from frozen viem output')

    def test_complete_extended_hex_gate(self):
        groups=harness.load_groups();report=asyncio.run(harness.full_comparison())
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['cases'],sum(len(cases) for _,cases,_ in groups))
        self.assertEqual(report['admissionDeviations'],len(harness.load_policy()['deviations']))
        self.assertEqual(report['exactMatches'],report['cases']-report['admissionDeviations'])
        self.assertEqual(report['unexplainedMismatches'],0)
        existing=[g for g in groups if g[0]!='hex']
        actual={x['case']:x['candidate'] for x in report['ledger'] if not x['case'].startswith('hex:')}
        prior=harness.classify(existing,actual,harness.load_policy())
        self.assertEqual((prior['cases'],prior['exactMatches'],prior['admissionDeviations'],prior['unexplainedMismatches']),(360,358,2,0))

    def test_hex_octets_hashes_and_safe_text_domains(self):
        group=next(g for g in harness.load_groups() if g[0]=='hex')
        witness=json.loads((ROOT/'test/toolchain/equivalence/hex-witness-reference.json').read_text())['results']
        self.assertEqual([c['id'] for c in group[1]],[w['id'] for w in witness])
        for c,w in zip(group[1],witness):
            with self.subTest(case=c['id']):
                actual=asyncio.run(candidate.run(c))
                self.assertEqual(actual['outcome'],'return')
                encoded=actual['value'];self.assertEqual(encoded,w['encoded'])
                params=c['abi']['inputs'] if c['op']=='function' else c['params']
                body='0x'+encoded[10:] if c['op']=='function' else encoded
                self.assertEqual(candidate.pack(abi.decode_parameters(params,body)),w['decoded'])
                self.assertEqual(candidate.keccak256(bytes.fromhex(encoded[2:])),w['keccak'])
                self.assertEqual(j.raw_sha256(encoded.encode()),w['rawTextSha256'])
                if c['op']=='function':
                    # A synthetic unsigned envelope exercises ABI output through
                    # the existing text/hash primitives; this is not a Safe port.
                    safe={'version':'1.0','chainId':'31337','createdAt':0,
                          'meta':{'name':'SIMULATION ONLY — Hex spelling witness',
                                  'description':'No broadcast. Atomic MultiSend required; verify all inputs immediately before signing.',
                                  'txBuilderVersion':'1.18.0','createdFromSafeAddress':'0x'+'34'*20},
                          'transactions':[{'to':'0x'+'12'*20,'value':'0','data':encoded,'contractMethod':None,'contractInputsValues':None}],
                          'simulationOnly':True,'deployable':False}
                    self.assertEqual(j.wire_json(safe),w['safeJson'])
                    self.assertEqual(j.canonical_sha256(safe),w['safeCanonicalSha256'])

    def test_wrong_case_folding_detected(self):
        original=abi._encode
        with patch.object(abi,'_encode',lambda p,v:original(p,v).lower()):
            report=asyncio.run(harness.full_comparison())
        self.assertEqual(report['status'],'FAIL')
        mismatches={x['case'] for x in report['ledger'] if x['classification']=='unexplained-mismatch'}
        self.assertTrue({'spelling:canonical-spelling-uppercase','spelling:canonical-spelling-mixedcase',
                         'hex:hex-fixed-1-upper','hex:hex-fixed-32-mixed','hex:hex-array-bytes[][]',
                         'hex:hex-nested-tuple','hex:hex-function-scheduleBatch','hex:hex-function-grantRole'} <= mismatches)
        self.assertEqual(report['admissionDeviations'],2)

    def test_admission_rule_rejects_permissive_encoder_and_wrong_error(self):
        original=abi._hex
        for replacement in (lambda value,**kw: bytes.fromhex(value[2:].ljust(2,'0')) if value=='0xa' else original(value,**kw),
                            lambda value,**kw: b'\0' if value=='0xgg' else original(value,**kw)):
            with patch.object(abi,'_hex',replacement):
                report=asyncio.run(harness.full_comparison())
            self.assertEqual(report['status'],'FAIL')
            self.assertTrue(any(x['classification']=='unexplained-mismatch' and not x['case'].startswith('spelling:') for x in report['ledger']))
        with self.assertRaises(abi.AbiError) as error:abi.decode_parameters([{'type':'uint256'}],'0xgg')
        self.assertEqual(error.exception.code,'BaseError')
        with self.assertRaises(abi.AbiError) as error:abi.decode_parameters([{'type':'uint256'}],None)
        self.assertEqual(error.exception.code,'TypeError')

    def test_gate_rejects_missing_extra_skipped_and_unapproved_results(self):
        groups=harness.load_groups();policy=harness.load_policy()
        report=asyncio.run(harness.full_comparison())
        actual={x['case']:x['candidate'] for x in report['ledger']}
        missing=dict(actual);missing.pop(next(iter(missing)))
        extra=dict(actual);extra['unapproved:extra']={}
        for bad in [missing,extra,dict(reversed(list(actual.items())))]:
            with self.assertRaisesRegex(ValueError,'candidate case'):harness.classify(groups,bad,policy)
        for changed in [{'outcome':'skip'}, {'outcome':'return','value':'wrong'}]:
            bad=copy.deepcopy(actual);bad[next(iter(bad))]=changed
            self.assertGreater(harness.classify(groups,bad,policy)['unexplainedMismatches'],report['unexplainedMismatches'])
        # A real successful case with a wrong byte cannot become an approved deviation.
        bad=copy.deepcopy(actual);key='initial:abi-nested';bad[key]['outputBytes']='00'
        self.assertGreater(harness.classify(groups,bad,policy)['unexplainedMismatches'],report['unexplainedMismatches'])
        bad_policy=copy.deepcopy(policy);bad_policy['deviations'].append(copy.deepcopy(policy['deviations'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate admission rule'):harness.classify(groups,actual,bad_policy)
        bad_policy=copy.deepcopy(policy);bad_policy['deviations'][0]['caseSha256']='0'*64
        with self.assertRaisesRegex(ValueError,'not bound'):harness.classify(groups,actual,bad_policy)
        with patch.object(harness,'POLICY_SHA256','0'*64),self.assertRaisesRegex(ValueError,'policy identity'):harness.load_policy()

    def test_every_frozen_lock_and_additive_file_is_checked(self):
        original=Path.read_bytes
        names=['reference-lock.json',*harness.ADDITIVE_LOCKS,'admission-cases.json','admission-reference.json',
               'supplement-cases.json','width-url-reference.json','pin-cases.json','distinct-reference.json',
               'decode-reference.json','undefined-reference.json','hex-reference.json','hex-witness-reference.json']
        for name in names:
            def corrupt(p):return b'changed' if p.name==name else original(p)
            with self.subTest(file=name),patch.object(Path,'read_bytes',corrupt),self.assertRaises(ValueError):harness.verify()

    def test_discovery_requires_every_check_without_skips(self):
        ids=harness.required_test_ids()
        harness.validate_discovery(ids)
        for bad in [ids[:-1],ids+ids[:1]]:
            with self.assertRaisesRegex(ValueError,'required test'):harness.validate_discovery(bad)
        for attr in ['skipped','expectedFailures']:
            result=unittest.TestResult();setattr(result,attr,[('synthetic','disabled')])
            self.assertFalse(harness.discovery_passed(result))
        with patch.object(harness,'VERIFICATION_PLAN_SHA256','0'*64),self.assertRaisesRegex(ValueError,'verification plan'):harness.validate_discovery(ids)

    def test_existing_B0_and_four_serialization_domains(self):
        baseline=json.loads((ROOT/'test/toolchain/equivalence/b0-reference.json').read_text())
        self.assertEqual([abi.enc_word(x) for x in [0,1,-1,True,'0x'+'12'*20]],baseline['encWords'])
        self.assertEqual(abi.calldata('balanceOf(address)','0x'+'12'*20),baseline['calldata'])
        data='0x'+''.join(abi.enc_word(x) for x in [32,2,1,2,3,4]);self.assertEqual(abi.dec_tuple_array(data),baseline['tupleArray'])
        self.assertTrue(abi.dec_bool('0x'+abi.enc_word(2)))
        with self.assertRaises(abi.AbiError):abi.decode_parameters([{'type':'bool'}],'0x'+abi.enc_word(2))
        self.assertEqual(j.raw_sha256(b' {"x":1}\n'),baseline['rawSha256'])
        self.assertEqual(j.historical_filter_id(['0x'+'34'*20,'0x'+'12'*20]),baseline['historicalFilter'])
        value={'z':[1,False,None],'a':'汉字😀'}
        self.assertEqual(common.canonical_json(value),baseline['commonJson'])
        self.assertEqual(common.canonical_json(value),baseline['nativeJson'])
        self.assertNotEqual(j.canonical(value),baseline['commonJson'])
        self.assertNotEqual(j.raw_sha256(b'{"x":1}'),baseline['rawSha256'])

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

    def test_wrong_keccak_rule_detected(self):
        c=self.byid['hash-136']
        with patch.object(candidate,'keccak256',lambda b:'0x'+hashlib.sha3_256(b).hexdigest()):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_wrong_ABI_offset_rule_detected(self):
        c=self.byid['abi-nested'];original=abi._word
        with patch.object(abi,'_word',lambda n:original(n+32)):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_wrong_JSON_serializer_detected(self):
        c=self.byid['json-1']
        with patch.object(j,'canonical',common.canonical_json):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_wrong_approval_hash_rule_detected(self):
        c=self.byid['file-hash-wrong']
        with patch.object(j,'checked_json',lambda path,expected:j.parse_json(Path(path).read_bytes())):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_wrong_RPC_id_rule_detected(self):
        c=self.byid['rpc-response-wrongid']
        with patch.object(rpc,'_same_id',lambda a,b:True):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_wrong_RPC_authority_rule_detected(self):
        c=self.byid['rpc-deny-eth_sendTransaction']
        # The test transport is the frozen fake fetch. No actual method is sent.
        with patch.object(rpc,'METHODS',rpc.METHODS|{'eth_sendTransaction'}):actual=asyncio.run(candidate.run(c))
        self.assertFalse(harness.comparison(self.expected[c['id']],actual))

    def test_refuses_corrupted_frozen_reference(self):
        original=Path.read_bytes
        def corrupt(p):return b'changed' if p.name=='reference.json' else original(p)
        with patch.object(Path,'read_bytes',corrupt),self.assertRaisesRegex(ValueError,'frozen reference changed'):harness.verify()

    def test_scope_has_no_native_cli_or_signing_dependency(self):
        # Keep the locked Foundation test identity while enforcing the newly
        # authorized additive CLI surface. No primitive corpus or policy changes.
        from tools.release.native import __main__ as native_cli
        self.assertEqual(native_cli.RELEASE_ACTIONS, ('preflight', 'authority', 'p1-runbook',
                         'build-handoff', 'build-u', 'estimate-gas', 'testnet-handoff', 'postcheck'))
        groups=native_cli.parser()._subparsers._group_actions[0].choices
        self.assertEqual(set(groups), {'release', 'artifact'})
        self.assertEqual(set(groups['artifact']._subparsers._group_actions[0].choices), {'normalize'})
        self.assertEqual(rpc.METHODS,{'eth_chainId','eth_getBlockByNumber','eth_getCode','eth_getStorageAt','eth_call','eth_getLogs','eth_estimateGas','eth_getTransactionByHash'})


if __name__=='__main__':unittest.main()
