#!/usr/bin/env python3
"""Verify the frozen oracle/corpus or compare a candidate. Never refresh expected results.
Run through the approved export/runner. Candidate output belongs in a separate new file.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import fixture_guard

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
FROZEN_LOCK_SHA256='69a735b984328309873a54b3800f6ea301901044b1d8a9390f62fa5f37973191'

def sha(b):return hashlib.sha256(b).hexdigest()

ADDITIVE_LOCKS={'supplement-lock.json': '46dcb1440daf5fdeaabf1bdec509ebf0eb77d43ab7995f53e3e65e6a8b6a0c05', 'extended-lock.json': '25666b2d15df9130fbc8a9c92e57601941c34f27488054d4d792651255bfd32c', 'distinct-lock.json': 'a94a0b8e3762deff2dc6487f5782fcadfc02644758a7ffbd65c25cf3644f5f79', 'decode-lock.json': '41d470f085975f9f20bf967cd88dfea82289e204f56307882d29f7133d256c97', 'undefined-lock.json': 'caf76fa52722f4c96c4b1d5b2103153fea990ec04826089110bf2f068c20d0fc', 'admission-lock.json': '2b4d21e2ff2b2a17f669f81aee81309bca93643885f832c1bb6cd3e8448d3c0e'}

ADDITIVE_LOCKS['spelling-lock.json']='84d2d28a9967ac9ff14b6a1a0cedf6ed80aff0182e713c0bd21c8d2724b5fa72'

ADDITIVE_LOCKS['hex-lock.json']='c73aae3cf80b6ae8d42096f32c874e6bf53a4158b1171acd055e15a3fddaf02b'


def verify():
    if sha((HERE/'reference-lock.json').read_bytes()) != FROZEN_LOCK_SHA256: raise ValueError('reference lock identity changed')
    for name,digest in ADDITIVE_LOCKS.items():
        if sha((HERE/name).read_bytes()) != digest: raise ValueError('additive lock identity changed')
    for name in ADDITIVE_LOCKS:
        extra=json.loads((HERE/name).read_text())
        files=extra.get('files',extra)
        base=ROOT/'test' if name=='distinct-lock.json' else HERE
        for path,digest in files.items():
            if sha((base/path).read_bytes())!=digest:raise ValueError('frozen additive file changed: '+path)
    lock=json.loads((HERE/'reference-lock.json').read_text())
    for name,digest in lock['files'].items():
        if sha((HERE/name).read_bytes())!=digest:raise ValueError('frozen reference changed: '+name)
    cases=json.loads((HERE/'cases.json').read_text());ref=json.loads((HERE/'reference.json').read_text())['results']
    if len(cases)!=lock['caseCount'] or len(ref)!=len(cases) or len({c['id'] for c in cases})!=len(cases):raise ValueError('corpus count/identity mismatch')
    return lock,cases,ref


def comparison(reference,candidate):
    # Owned release errors retain exact message/code. viem's diagnostic rendering
    # includes library version/docs/formatting: compare its precise named category.
    # Raw original diagnostic bytes remain in reference.json for independent review.
    a=dict(reference);b=dict(candidate)
    if a['outcome']=='throw' and b['outcome']=='throw':
        a.pop('errorBytes',None);b.pop('errorBytes',None)
        for x in (a,b):
            e=x['error'];x['error']={'name':e['name']}
            if e['name']=='Error':x['error']['message']=e['message']
    return a==b


VERIFICATION_PLAN_SHA256='49cdd0f5c42ba47136f4788afb0790e2ac6beca1f41513259062ab38b9ffb58e'
HEX_VERIFICATION_PLAN_SHA256='7e02f5335dfa886ed0bfc38bc5eee91f3db5cfd5f84cafd6b5220ff233b06adf'
POLICY_SHA256='feeacc40fc4ff867b773a5c8e345297e4047648198f68195746d58b0a8518888'


def identity(value):
    return sha(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())


def load_policy():
    raw=(HERE/'admission-policy.json').read_bytes()
    if sha(raw)!=POLICY_SHA256:raise ValueError('admission policy identity changed')
    return json.loads(raw)


def load_groups():
    verify()
    groups=[]
    for name,prefix in [('initial',''),('supplement','supplement-'),('width-url','width-url-'),('pins','pin-'),('distinct','distinct-'),('decode','decode-'),('admission','admission-'),('spelling','spelling-'),('hex','hex-')]:
        cases=json.loads((HERE/(prefix+'cases.json')).read_text())
        reference=json.loads((HERE/(prefix+'reference.json')).read_text())['results']
        ids=[c['id'] for c in cases]
        if len(ids)!=len(set(ids)) or ids!=[r['id'] for r in reference]:raise ValueError('missing/duplicate/reordered cases: '+name)
        groups.append((name,cases,reference))
    # These four inputs are also spelled out in the frozen undefined-reference.mjs.
    cases=[{'id':'undefined-'+str(i),'index':i} for i in range(4)]
    values=json.loads((HERE/'undefined-reference.json').read_text())
    if len(values)!=len(cases):raise ValueError('undefined corpus incomplete')
    groups.append(('undefined',cases,[{'id':c['id'],'outcome':'return','value':v} for c,v in zip(cases,values)]))
    return groups


async def evaluate(group,c):
    # Test adapter for the three separately captured reference formats. No expected
    # value enters the candidate path; all others use the original input-only adapter.
    import candidate
    from tools.lib import abi, release_json as j
    from tools.release.native import rpc
    if group=='pins':
        trace=[]
        async def read(method,params):
            trace.append({'method':method,'params':params})
            return c['observedChain'] if method=='eth_chainId' else c['header']
        try:
            value=await rpc.pinned(c['chainId'],read,c['tag'])
            return {'id':c['id'],'outcome':'return','value':value,'outputBytes':candidate.wire(value).encode().hex(),'trace':trace}
        except j.ReleaseError as e:
            return {'id':c['id'],'outcome':'throw','error':{'name':'Error','message':str(e)},'trace':trace}
    if group=='decode':
        try:
            value=candidate.pack(abi.decode_function(c['abi'],c['data']))
            return {'id':c['id'],'outcome':'return','value':value,'outputBytes':candidate.wire(value).encode().hex()}
        except abi.AbiError as e:
            return {'id':c['id'],'outcome':'throw','error':{'name':e.code,'message':str(e)}}
    if group=='undefined':
        v=[abi.UNDEFINED,{'x':abi.UNDEFINED},[abi.UNDEFINED,None],{'x':None}][c['index']]
        canonical,wire=j.canonical(v),j.wire_json(v)
        return {'id':c['id'],'outcome':'return','value':{'undefined':canonical is abi.UNDEFINED,'text':None if canonical is abi.UNDEFINED else canonical,'wire':None if wire is abi.UNDEFINED else wire}}
    return await candidate.run(c)


def classify(groups,actual,policy):
    expected_keys=[name+':'+c['id'] for name,cases,_ in groups for c in cases]
    if list(actual)!=expected_keys or len(set(expected_keys))!=len(expected_keys):
        raise ValueError('missing/duplicate/reordered/unexpected candidate case')
    deviations=policy['deviations'];rules={r['id']:r for r in deviations}
    if len(rules)!=len(deviations):raise ValueError('duplicate admission rule')
    ledger=[];seen=set()
    for name,cases,refs in groups:
        if [c['id'] for c in cases]!=[r['id'] for r in refs]:raise ValueError('reference case identities changed')
        for c,r in zip(cases,refs):
            key=name+':'+c['id'];a=actual[key];rule=rules.get(c['id']) if name=='admission' else None
            if rule:
                seen.add(c['id'])
                if identity(c)!=rule['caseSha256'] or identity(r)!=rule['referenceOutcomeSha256']:
                    raise ValueError('admission rule not bound to frozen case/reference')
                required=rule['requiredCandidate']
                good=(a.get('id')==c['id'] and a.get('outcome')==required['outcome'] and a.get('error',{}).get('name')==required['errorName'] and a.get('trace')==required['trace'] and not comparison(r,a))
                category='native-admission-deviation' if good else 'unexplained-mismatch'
            else:
                category='exact-match' if comparison(r,a) else 'unexplained-mismatch'
            ledger.append({'case':key,'classification':category,'reference':r,'candidate':a})
    if seen!=set(rules):raise ValueError('unused/missing admission rule')
    counts={kind:sum(x['classification']==kind for x in ledger) for kind in ['exact-match','native-admission-deviation','unexplained-mismatch']}
    return {'status':'FAIL' if counts['unexplained-mismatch'] else 'PASS','scope':'frozen Foundation corpus under explicit admission amendment; not raw equivalence','cases':len(ledger),'exactMatches':counts['exact-match'],'admissionDeviations':counts['native-admission-deviation'],'unexplainedMismatches':counts['unexplained-mismatch'],'policySha256':POLICY_SHA256,'originalCases':sum(not x['case'].startswith(('spelling:','hex:')) for x in ledger),'existingCases':sum(not x['case'].startswith('hex:') for x in ledger),'supplementalHexCases':sum(x['case'].startswith('hex:') for x in ledger),'newCanonicalSpellingCases':sum(x['case'].startswith('spelling:') for x in ledger),'ledger':ledger}


async def full_comparison():
    fixture_guard.prepare()
    groups=load_groups();actual={}
    for name,cases,_ in groups:
        for c in cases:actual[name+':'+c['id']]=await evaluate(name,c)
    return classify(groups,actual,load_policy())


def suite_ids(suite):
    import unittest
    result=[]
    for test in suite:
        result.extend(suite_ids(test) if isinstance(test,unittest.TestSuite) else [test.id()])
    return result


def required_test_ids():
    raw=(HERE/'verification-plan.json').read_bytes()
    if sha(raw)!=VERIFICATION_PLAN_SHA256:raise ValueError('verification plan identity changed')
    extra=(HERE/'hex-verification-plan.json').read_bytes()
    if sha(extra)!=HEX_VERIFICATION_PLAN_SHA256:raise ValueError('hex verification plan identity changed')
    return sorted(json.loads(raw)['testIds']+json.loads(extra)['testIds'])


def validate_discovery(ids):
    required=required_test_ids()
    if len(ids)!=len(set(ids)) or sorted(ids)!=required:raise ValueError('required test missing, duplicated or disabled')


def discovery_passed(result):
    return result.wasSuccessful() and not result.skipped and not result.expectedFailures


def strict_discovery():
    import unittest
    suite=unittest.defaultTestLoader.discover(str(ROOT/'test/toolchain'),pattern='test_*.py')
    validate_discovery(suite_ids(suite))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    # unittest normally treats skips/expected failures as success; the delivery
    # gate must not. Full comparison independently requires every frozen case.
    passed=discovery_passed(result)
    report=asyncio.run(full_comparison())
    report['testsRun']=result.testsRun
    report['skipped']=len(result.skipped);report['expectedFailures']=len(result.expectedFailures)
    if not passed:report['status']='FAIL'
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('mode',choices=['verify-reference','compare','test']);ap.add_argument('--out',type=Path,required=True);args=ap.parse_args()
    if not (ROOT/'EXPORT-MANIFEST.txt').is_file():raise ValueError('approved export required')
    if args.out.exists():raise ValueError('refusing to replace existing evidence')
    if any(p.lower().startswith('.env') or any(m in p.lower() for m in ('secret','credential','keystore','private_key','mnemonic')) for p in args.out.parts): raise ValueError('unsafe output path')
    cursor=Path(os.path.abspath(args.out)).anchor
    for part in Path(os.path.abspath(args.out)).parts[1:]:
        cursor=Path(cursor)/part
        if cursor.is_symlink(): raise ValueError('symlink evidence path')
    if args.out.is_symlink() or args.out.with_suffix('.candidate.json').exists(): raise ValueError('refusing existing sidecar')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    fixture_guard.prepare()
    lock,cases,ref=verify()
    if args.mode=='verify-reference':
        for path,digest in lock['sourceSha256'].items():
            if sha((ROOT/path).read_bytes())!=digest:raise ValueError('not original reference source: '+path)
        captures = [
            ('reference.mjs','cases.json','reference.json'),
            ('reference.mjs','supplement-cases.json','supplement-reference.json'),
            ('reference.mjs','width-url-cases.json','width-url-reference.json'),
            ('reference.mjs','distinct-cases.json','distinct-reference.json'),
            ('reference.mjs','admission-cases.json','admission-reference.json'),
            ('reference.mjs','spelling-cases.json','spelling-reference.json'),
            ('reference.mjs','hex-cases.json','hex-reference.json'),
            ('hex-witness-reference.mjs','hex-cases.json','hex-witness-reference.json'),
            ('pin-reference.mjs','pin-cases.json','pin-reference.json'),
            ('decode-reference.mjs','decode-cases.json','decode-reference.json'),
            ('undefined-reference.mjs',None,'undefined-reference.json'),
        ]
        reproduced=[]
        for script,inputs,expected in captures:
            command=['node',str(HERE/script)]+([str(HERE/inputs)] if inputs else [])
            result=subprocess.check_output(command,cwd=ROOT)
            if result!=(HERE/expected).read_bytes():raise ValueError('reference no longer reproduces frozen bytes: '+expected)
            reproduced.append({'file':expected,'sha256':sha(result)})
        report={'status':'PASS','cases':sum(len(x[1]) for x in load_groups()),'reproduced':reproduced}
    elif args.mode=='compare':
        report=asyncio.run(full_comparison())
    else:
        report=strict_discovery()
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,indent=2)+'\n')
    print(report['status'],report.get('cases',report.get('testsRun')),'cases/tests')
    if report['status']!='PASS':sys.exit(1)


if __name__=='__main__':main()
