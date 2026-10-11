// Test-only observer of the committed reference. Never imports Python candidate code.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const root=process.cwd(), require=createRequire(root+'/package.json');
const v=await import(pathToFileURL(require.resolve('viem')).href);
const core=await import(pathToFileURL(root+'/tasks/release/core.mjs').href);
const ro=await import(pathToFileURL(root+'/tasks/release/read-only.mjs').href);
const revive=x=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===1&&'$bigint'in x?BigInt(x.$bigint):Array.isArray(x)?x.map(revive):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,w])=>[k,revive(w)])):x;
const pack=x=>typeof x==='bigint'?{$bigint:String(x)}:x===undefined?{$undefined:true}:Array.isArray(x)?x.map(pack):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,w])=>[k,pack(w)])):x;
const sha=b=>createHash('sha256').update(b).digest('hex');
const err=e=>({name:e.name,message:e.message,shortMessage:e.shortMessage??null});
async function run(c){const trace=[];try{let value;
 switch(c.op){
 case 'hash':value=v.keccak256(c.data);break;
 case 'canonical':value=core.canonical(c.raw!==undefined?JSON.parse(c.raw):revive(c.value));break;
 case 'encode':value=v.encodeAbiParameters(c.params,revive(c.values));break;
 case 'decode':value=v.decodeAbiParameters(c.params,c.data);break;
 case 'function':value=v.encodeFunctionData({abi:[c.abi],functionName:c.abi.name,args:revive(c.values)});break;
 case 'event':value=v.decodeEventLog({abi:[c.abi],data:c.data,topics:c.topics,strict:true});break;
 case 'file':{const dir='/private/tmp/faroo-foundation-inputs';fs.mkdirSync(dir,{recursive:true});const file=dir+'/input.json';fs.writeFileSync(file,c.content??'{"x":1}');const link=dir+'/alias.json';if(fs.existsSync(link))fs.unlinkSync(link);fs.symlinkSync(file,link);const requested=c.kind==='symlink'?link:c.kind==='hidden'?dir+'/.blocked/input.json':c.kind==='extension'?dir+'/input.txt':c.kind==='missing'?dir+'/absent.json':file;if(c.kind==='extension')fs.writeFileSync(requested,'{}');try{trace.push({path:requested,operation:'checkedJson'});value=core.checkedJson(requested,c.hash==='correct'?sha(fs.readFileSync(file)):c.hash==='wrong'?'0'.repeat(64):c.hash);}finally{fs.unlinkSync(link);}break;}
 case 'rpc':{
 const save={fetch:globalThis.fetch,setTimeout:globalThis.setTimeout,now:Date.now,timeout:AbortSignal.timeout};let time=1800000000000,attempt=0;
 Date.now=()=>time;AbortSignal.timeout=ms=>({timeout:ms});
 globalThis.setTimeout=(fn,ms,...a)=>{if(ms){trace.push({sleep:ms});time+=ms;}return save.setTimeout(fn,0,...a);};
 globalThis.fetch=async(url,o)=>{const body=JSON.parse(o.body);trace.push({url,method:o.method,body,timeout:o.signal.timeout,redirect:o.redirect});const step=c.responses?.[Math.min(attempt++,c.responses.length-1)]??{kind:'ok'};
 if(step.kind==='throw'){const e=new Error(step.message??'fetch failed');e.name=step.name??'TypeError';throw e;}
 const req=Array.isArray(body)?body:[body];let result=req.map(r=>({jsonrpc:'2.0',id:r.id,result:step.value??r.params}));
 if(step.kind==='reverse')result.reverse();if(step.kind==='missing')result.pop();if(step.kind==='duplicate'&&result.length>1)result[1]=result[0];
 if(step.kind==='wrongid')result[0].id=999;if(step.kind==='stringid')result[0].id=String(result[0].id);if(step.kind==='noresult')delete result[0].result;
 if(step.kind==='error')result=result.map(r=>({jsonrpc:'2.0',id:r.id,error:{code:step.code,message:'synthetic rpc failure'}}));
 if(step.kind==='nullerror')result[0].error=null;if(step.kind==='nullbody')result=[null];if(step.kind==='version')result[0].jsonrpc='1.0';
 const status=step.status??200;return {ok:status>=200&&status<300,status,json:async()=>{if(step.kind==='badjson')throw new SyntaxError('synthetic malformed JSON');return Array.isArray(body)?result:result[0];}};};
 try{const rpc=ro.readOnlyRpc(c.url,c.simulation??false,c.localOnly??false);value=(await Promise.allSettled(c.calls.map(x=>rpc(x[0],x[1])))).map(x=>x.status==='fulfilled'?{status:x.status,value:pack(x.value)}:{status:x.status,error:err(x.reason)});}finally{globalThis.fetch=save.fetch;globalThis.setTimeout=save.setTimeout;Date.now=save.now;AbortSignal.timeout=save.timeout;}break;}
 default:throw Error('unknown operation');}
 const result=pack(value),bytes=Buffer.from(typeof value==='string'?value:JSON.stringify(result),'utf8').toString('hex');return {id:c.id,outcome:'return',value:result,outputBytes:bytes,trace};
 }catch(e){return {id:c.id,outcome:'throw',error:err(e),errorBytes:Buffer.from(e.message).toString('hex'),trace};}}
const cases=JSON.parse(fs.readFileSync(process.argv[2]));const results=[];for(const c of cases)results.push(await run(c));
process.stdout.write(JSON.stringify({origin:'SYNTHETIC TEST ONLY',node:process.version,viem:JSON.parse(fs.readFileSync(root+'/node_modules/viem/package.json')).version,results},null,2)+'\n');
