// Replay frozen inputs against original source, including recorded callback causality.
import fs from'node:fs';import path from'node:path';import zlib from'node:zlib';import{createHash}from'node:crypto';import{pathToFileURL,fileURLToPath}from'node:url';import{syncBuiltinESMExports}from'node:module';
import {invokeTask}from'./task-adapter.mjs';
const root=process.cwd(),here=path.dirname(fileURLToPath(import.meta.url)),corpus=path.resolve(here,'../corpus');
const original={read:fs.readFileSync,lstat:fs.lstatSync,now:Date.now};
const sha=b=>createHash('sha256').update(b).digest('hex');
const lockRaw=original.read(corpus+'/reference-lock.json'),lock=JSON.parse(lockRaw);
if(sha(lockRaw)!=='bf0049a12b14a4a10f11705a32d21552f2d5da5de1000c67de4aba86e707d4cc')throw Error('Frozen release lock changed');
for(const[n,h]of Object.entries(lock.files))if(sha(original.read(path.resolve(corpus,'..',n)))!==h)throw Error('Frozen corpus changed: '+n);
for(const[n,h]of Object.entries(lock.sourceSha256))if(sha(original.read(root+'/'+n))!==h)throw Error('Original source identity changed: '+n);
if(!fs.existsSync(root+'/EXPORT-MANIFEST.txt'))throw Error('Approved export required');
const modules={};for(const name of ['core','artifacts','history','timelock-history','pool-replay','residual-replay','read-only'])modules[name+'.mjs']=await import(pathToFileURL(root+'/tasks/release/'+name+'.mjs'));
const inputRoot='/private/tmp/faroo-native-release/inputs',marker='FAROO NATIVE RELEASE SYNTHETIC INPUTS; reference-lock-bound replay only\n';
function noSymlinks(n){let cursor='/';for(const part of n.split('/').filter(Boolean)){cursor=path.join(cursor,part);try{if(original.lstat(cursor).isSymbolicLink())throw Error('Symlink replay path');}catch(error){if(error.code!=='ENOENT')throw error;}}}
noSymlinks(inputRoot+'/OWNER.txt');
if(!fs.existsSync(inputRoot)){fs.mkdirSync(inputRoot,{recursive:true});fs.writeFileSync(inputRoot+'/OWNER.txt',marker,{flag:'wx'});}
if(original.read(inputRoot+'/OWNER.txt','utf8')!==marker)throw Error('Unowned input root');
function safe(n){if(!n.startsWith(inputRoot+'/')||path.resolve(n)!==n||!n.endsWith('.json')||n.split('/').some(p=>p.startsWith('.')||/^(keys?|credentials?|secrets?|keystores?|private[-_]?keys?)$/i.test(p)))throw Error('Unsafe replay input');let cursor='/';for(const p of n.split('/').filter(Boolean)){cursor=path.join(cursor,p);try{if(original.lstat(cursor).isSymbolicLink())throw Error('Symlink replay input');}catch(e){if(e.code!=='ENOENT')throw e;}}}
function prepare(rec){for(const[n,v]of Object.entries(rec.files)){safe(n);const raw=Buffer.from(v.raw,'base64');if(sha(raw)!==v.sha256)throw Error('Input digest mismatch');fs.mkdirSync(path.dirname(n),{recursive:true});fs.writeFileSync(n,raw);}for(const e of rec.events)if(e.kind==='lstat'&&e.error?.code==='ENOENT'){safe(e.path);try{fs.unlinkSync(e.path);}catch(error){if(error.code!=='ENOENT')throw error;}}}
const pack=x=>x===undefined?{$undefined:true}:typeof x==='bigint'?{$bigint:String(x)}:typeof x==='function'?{$function:true}:typeof x==='number'&&!Number.isFinite(x)?{$number:String(x)}:Array.isArray(x)?x.map(pack):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,pack(v)])):x;
const revive=x=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===1&&'$undefined'in x?undefined:x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===1&&'$bigint'in x?BigInt(x.$bigint):x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).length===1&&'$number'in x?Number(x.$number):Array.isArray(x)?x.map(revive):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,revive(v)])):x;
const err=e=>({name:e.name,message:e.message,...(e.code?{code:e.code}:{}),...(e.syscall?{syscall:e.syscall}:{}),...(e.path?{path:e.path}:{})});
const canonical=v=>JSON.stringify(v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,JSON.parse(canonical(v[k]))])):Array.isArray(v)?v.map(x=>JSON.parse(canonical(x))):v);
// Only file-access diagnostics use relocatable export prefixes. Returned values
// and calldata are compared verbatim, including any path strings they contain.
const normalizeIo=v=>Array.isArray(v)?v.map(normalizeIo):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).map(([k,x])=>[k,normalizeIo(x)])):typeof v==='string'?[root,'/private/tmp/faroo-native-release/reference'].reduce((x,p)=>x===p||x.startsWith(p+'/')?'$REPO'+x.slice(p.length):x,v):v;
let active;
fs.lstatSync=function(n,...args){try{const v=original.lstat(n,...args);if(active)active.io.push({kind:'lstat',path:String(n),type:v.isSymbolicLink()?'symlink':v.isDirectory()?'directory':'file'});return v;}catch(e){if(active)active.io.push({kind:'lstat',path:String(n),error:err(e)});throw e;}};
fs.readFileSync=function(n,...args){const v=original.read(n,...args);if(active&&String(n).startsWith(inputRoot+'/'))active.io.push({kind:'read',path:String(n),sha256:sha(Buffer.isBuffer(v)?v:Buffer.from(v))});return v;};
syncBuiltinESMExports();globalThis.fetch=async()=>{throw Error('Unmocked reference network forbidden');};
class Tape{
 constructor(rec){this.rec=rec;this.cursor=0;this.taken=new Set();this.pending=new Map();this.events=rec.events.filter(e=>e.kind.startsWith('callback-'));this.failure=null;this.clock=0;this.io=[];this.started=[];}
 callback(label){return(...args)=>{const key=canonical(pack(args)),entry=this.rec.callbacks.find(x=>x.label===label&&!this.taken.has(x.order)&&canonical(x.args)===key);if(!entry){this.failure='Unexpected callback '+label+' '+key.slice(0,160);return Promise.reject(Error(this.failure));}this.taken.add(entry.order);this.started.push(entry.order);const result=new Promise((resolve,reject)=>this.pending.set(entry.order,{resolve,reject,entry}));this.drain();return result;};}
 drain(){while(this.cursor<this.events.length){const e=this.events[this.cursor];if(e.kind==='callback-start'){if(!this.taken.has(e.index))return;this.cursor++;}else{const p=this.pending.get(e.index);if(!p)return;this.pending.delete(e.index);this.cursor++;if(p.entry.outcome==='throw'){const error=Error(p.entry.error.message);Object.assign(error,p.entry.error);p.reject(error);}else p.resolve(revive(p.entry.value));}}}
 now(){if(this.clock>=this.rec.clocks.length){this.failure='Unexpected clock read';throw Error(this.failure);}return this.rec.clocks[this.clock++];}
 async finish(){for(let i=0;i<50&&(this.cursor<this.events.length||this.pending.size);i++)await new Promise(resolve=>setImmediate(resolve));if(this.failure)throw Error(this.failure);if(this.cursor!==this.events.length||this.taken.size!==this.rec.callbacks.length)throw Error('Incomplete callback transcript');if(this.clock!==this.rec.clocks.length)throw Error('Clock coverage mismatch');}
}
const output=process.argv[2];if(!output||fs.existsSync(output))throw Error('Fresh output required');
if(!path.isAbsolute(output)||output.split('/').some(p=>p.toLowerCase().startsWith('.env')||/secret|credential|keystore|private[-_]?key|mnemonic/i.test(p)))throw Error('Unsafe evidence output');
noSymlinks(output);
const filter=process.argv[3],results=[];let count=0;
for(const name of Object.keys(lock.files).filter(n=>n.endsWith('.json.gz'))){const data=JSON.parse(zlib.gunzipSync(original.read(path.resolve(corpus,'..',name))));for(const rec of data.records){if(filter&&!rec.id.includes(filter))continue;prepare(rec);const tape=new Tape(rec);const args=rec.arguments.map(x=>x&&Object.hasOwn(x,'$callback')?tape.callback(x.$callback):revive(x));active=tape;Date.now=()=>tape.now();let actual;
 try{const split=rec.function.lastIndexOf('.'),module=rec.function.slice(0,split),fn=rec.function.slice(split+1);let timer;const work=module==='index.ts'?invokeTask(fn,...args):modules[module][fn](...args);let value;try{value=await Promise.race([Promise.resolve(work),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('HARNESS_CALLBACK_TRANSCRIPT_DEADLOCK')),3000);})]);}finally{clearTimeout(timer);}actual={outcome:'return',value:pack(value)};}catch(e){actual={outcome:'throw',error:err(e)};}
 active=null;Date.now=original.now;
 let transcriptError;try{await tape.finish();}catch(e){transcriptError=e.message;}
 const expected=rec.outcome==='return'?{outcome:'return',value:rec.value}:{outcome:'throw',error:rec.error};
 const expectedIo=rec.events.filter(e=>e.kind==='read'||e.kind==='lstat');const equal=canonical(actual)===canonical(expected)&&canonical(pack(args))===canonical(rec.argumentsAfter.map(x=>x&&Object.hasOwn(x,'$callback')?{$function:true}:x));
 const ioEqual=canonical(normalizeIo(tape.io))===canonical(normalizeIo(expectedIo));
 const result={id:rec.id,pass:equal&&!transcriptError&&ioEqual,...(equal?{}:{expected,actual}),...(transcriptError?{transcriptError}:{}),...(!ioEqual?{ioMismatch:true}:{})};results.push(result);
 if(!result.pass)console.error(JSON.stringify({id:rec.id,function:rec.function,transcriptError,actualError:actual.error,ioMismatch:result.ioMismatch}));if(++count%50===0)console.error('Replayed '+count+' frozen cases');
 }}
const report={status:results.every(x=>x.pass)&&(filter||results.length===lock.caseCount)?'PASS':'FAIL',scope:filter?'subset':'complete',cases:results.length,referenceLockSha256:sha(lockRaw),runnerSha256:sha(original.read(fileURLToPath(import.meta.url))),taskAdapterSha256:sha(original.read(here+'/task-adapter.mjs')),results};fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n',{flag:'wx'});console.log(report.status,report.cases);if(report.status!=='PASS')process.exitCode=1;
