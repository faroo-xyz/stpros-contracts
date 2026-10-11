"""Test-only adapter. It sees inputs, never the reference expected values."""
import asyncio
import json
from pathlib import Path
import sys
import fixture_guard

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'tools/lib'),str(ROOT)]
from tools.lib import abi
from tools.lib import release_json as j
from tools.release.native.rpc import ReleaseRpc, TransportError
from keccak import keccak256


def revive(x):
    if isinstance(x,dict) and set(x)=={'$bigint'}:return abi.BigInt(x['$bigint'])
    if isinstance(x,list):return list(map(revive,x))
    if isinstance(x,dict):return {k:revive(v) for k,v in x.items()}
    return x


def pack(x):
    if x is abi.UNDEFINED:return {'$undefined':True}
    if isinstance(x,abi.BigInt):return {'$bigint':str(x)}
    if isinstance(x,list):return list(map(pack,x))
    if isinstance(x,dict):return {k:pack(v) for k,v in x.items()}
    return x


def wire(x):
    if isinstance(x,dict):return '{'+','.join(j.quote(k)+':'+wire(v) for k,v in x.items())+'}'
    if isinstance(x,list):return '['+','.join(map(wire,x))+']'
    return j.canonical(x)


def error(e):
    if isinstance(e,abi.AbiError):return {'name':e.code,'message':str(e),'shortMessage':None}
    if isinstance(e,j.ReleaseError):return {'name':'Error','message':str(e),'shortMessage':None}
    if isinstance(e,TransportError):return {'name':e.name,'message':str(e),'shortMessage':None}
    if isinstance(e,FileNotFoundError):return {'name':'Error','message':"ENOENT: no such file or directory, lstat '/private/tmp/faroo-foundation-inputs/absent.json'",'shortMessage':None}
    if isinstance(e,json.JSONDecodeError):return {'name':'SyntaxError','message':str(e),'shortMessage':None}
    return {'name':type(e).__name__,'message':str(e),'shortMessage':None}


async def run(c):
    trace=[]
    try:
        op=c['op']
        if op=='hash':value=keccak256(bytes.fromhex(c['data'][2:]))
        elif op=='canonical':value=j.canonical(j.parse_json(c['raw']) if 'raw'in c else revive(c['value']))
        elif op=='encode':value=abi.encode_parameters(c['params'],revive(c['values']))
        elif op=='decode':value=abi.decode_parameters(c['params'],c['data'])
        elif op=='function':value=abi.encode_function(c['abi'],revive(c['values']))
        elif op=='event':value=abi.decode_event(c['abi'],c['topics'],c['data'])
        elif op=='file':
            fixture_guard.prepare()
            d=Path('/private/tmp/faroo-foundation-inputs');d.mkdir(exist_ok=True)
            f=d/'input.json';f.write_text(c.get('content','{"x":1}'));link=d/'alias.json';link.unlink(missing_ok=True);link.symlink_to(f)
            requested={'symlink':link,'hidden':d/'.blocked/input.json','extension':d/'input.txt','missing':d/'absent.json'}.get(c['kind'],f)
            if c['kind']=='extension':requested.write_text('{}')
            trace.append({'path':str(requested),'operation':'checkedJson'})
            expected=j.raw_sha256(f.read_bytes()) if c.get('hash')=='correct' else '0'*64 if c.get('hash')=='wrong' else c.get('hash')
            try:value=j.checked_json(str(requested),expected)
            finally:link.unlink()
        elif op=='rpc':
            now=1800000000000;attempt=0
            async def sleep(seconds):
                nonlocal now
                trace.append({'sleep':int(seconds*1000)});now+=seconds*1000
            async def post(url,body,timeout):
                nonlocal attempt
                trace.append({'url':url,'method':'POST','body':body,'timeout':timeout,'redirect':'error'})
                steps=c.get('responses',[{'kind':'ok'}]);step=steps[min(attempt,len(steps)-1)];attempt+=1;kind=step['kind']
                if kind=='throw':raise TransportError(step.get('name','TypeError'),step.get('message','fetch failed'))
                req=body if isinstance(body,list) else [body]
                result=[{'jsonrpc':'2.0','id':r['id'],'result':step.get('value',r['params'])} for r in req]
                if kind=='reverse':result.reverse()
                if kind=='missing':result.pop()
                if kind=='duplicate' and len(result)>1:result[1]=result[0]
                if kind=='wrongid':result[0]['id']=999
                if kind=='stringid':result[0]['id']=str(result[0]['id'])
                if kind=='noresult':del result[0]['result']
                if kind=='error':result=[{'jsonrpc':'2.0','id':r['id'],'error':{'code':step['code'],'message':'synthetic rpc failure'}} for r in req]
                if kind=='nullerror':result[0]['error']=None
                if kind=='nullbody':result=[None]
                if kind=='version':result[0]['jsonrpc']='1.0'
                if kind=='badjson':raise TransportError('SyntaxError','synthetic malformed JSON')
                return step.get('status',200), result if isinstance(body,list) else result[0]
            rpc=ReleaseRpc(c['url'],c.get('simulation',False),c.get('localOnly',False),post=post,clock_ms=lambda:now,sleep=sleep)
            values=await asyncio.gather(*(rpc(*call) for call in c['calls']),return_exceptions=True)
            value=[{'status':'rejected','error':error(x)} if isinstance(x,Exception) else {'status':'fulfilled','value':pack(x)} for x in values]
        else:raise ValueError('unknown operation')
        value=pack(value)
        return {'id':c['id'],'outcome':'return','value':value,'outputBytes':(value if isinstance(value,str) else wire(value)).encode().hex(),'trace':trace}
    except Exception as e:return {'id':c['id'],'outcome':'throw','error':error(e),'errorBytes':str(e).encode().hex(),'trace':trace}


async def main():
    cases=json.loads(Path(sys.argv[1]).read_text());results=[]
    for c in cases:results.append(await run(c))
    print(json.dumps({'results':results},indent=2,ensure_ascii=True))


if __name__=='__main__':asyncio.run(main())
