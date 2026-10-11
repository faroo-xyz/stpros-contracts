// Reference-only observer. Operational source modules are loaded without edits.
import fs from 'node:fs';import path from 'node:path';import{fileURLToPath}from'node:url';
import{AsyncLocalStorage}from'node:async_hooks';import{syncBuiltinESMExports}from'node:module';
import{createHash}from'node:crypto';
export const context=new AsyncLocalStorage();
const original={read:fs.readFileSync,lstat:fs.lstatSync,write:fs.writeFileSync,mkdtemp:fs.mkdtempSync,now:Date.now};
const output=process.env.FAROO_RELEASE_CAPTURE_DIR;
if(!output||!output.startsWith('/private/tmp/faroo-native-release/'))throw Error('Explicit owned capture directory required');
const suite=path.basename(process.argv[1]||'supplement').replace(/[^a-zA-Z0-9_.-]/g,'_');
const inputRoot='/private/tmp/faroo-native-release/inputs/'+suite;
fs.mkdirSync(inputRoot,{recursive:true});fs.mkdirSync(output,{recursive:true});
const records=[];let serial=0,clock=original.now;
const encode=x=>x===undefined?{$undefined:true}:typeof x==='bigint'?{$bigint:String(x)}:typeof x==='function'?{$function:true}:typeof x==='number'&&!Number.isFinite(x)?{$number:String(x)}:Array.isArray(x)?x.map(encode):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,encode(v)])):x;
export const pack=encode;
export const error=e=>({name:e.name,message:e.message,...(e.code?{code:e.code}:{}),...(e.syscall?{syscall:e.syscall}:{}),...(e.path?{path:e.path}:{})});
const sha=b=>createHash('sha256').update(b).digest('hex');
function now(){const value=clock();const s=context.getStore();if(s?.record){s.record.clocks.push(value);s.record.events.push({kind:'clock',index:s.record.clocks.length-1});}return value;}
Object.defineProperty(Date,'now',{configurable:true,get:()=>now,set:fn=>{clock=fn===now?original.now:fn;}});
fs.mkdtempSync=function(prefix,...args){if(String(prefix).includes('faroo-history-test-'))return original.mkdtemp(inputRoot+'/archive-',...args);return original.mkdtemp(prefix,...args);};
function pathname(value){return value instanceof URL?fileURLToPath(value):typeof value==='string'?value:null;}
function refuseSensitive(n){if(n&&n.split('/').some(p=>p.toLowerCase().startsWith('.env')||/^(keys?|credentials?|secrets?|keystores?|private[-_]?keys?|id_rsa|id_ed25519)$/i.test(p)))throw Error('Capture refuses sensitive path without access');}
fs.readFileSync=function(file,...args){const n=pathname(file),s=context.getStore();if(s?.record)refuseSensitive(n);const result=original.read(file,...args);
 if(s?.record&&n?.startsWith(inputRoot+'/')){const bytes=Buffer.isBuffer(result)?result:Buffer.from(result);s.record.files[n]={raw:bytes.toString('base64'),sha256:sha(bytes)};s.record.events.push({kind:'read',path:n,sha256:sha(bytes)});}return result;};
fs.lstatSync=function(file,...args){const n=pathname(file),s=context.getStore();if(s?.record)refuseSensitive(n);try{const value=original.lstat(file,...args);if(s?.record&&n){s.record.events.push({kind:'lstat',path:n,type:value.isSymbolicLink()?'symlink':value.isDirectory()?'directory':'file'});}return value;}catch(e){if(s?.record&&n)s.record.events.push({kind:'lstat',path:n,error:error(e)});throw e;}};
fs.writeFileSync=function(file,...args){const s=context.getStore();if(s?.record){const n=pathname(file);refuseSensitive(n);s.record.events.push({kind:'write',path:n});}return original.write(file,...args);};
syncBuiltinESMExports();
globalThis.fetch=async()=>{throw Error('Reference capture forbids unmocked network');};
function callback(rec,fn,label){
 const wrapped=(...args)=>{const entry={label,args:encode(args),order:rec.callbacks.length};rec.callbacks.push(entry);rec.events.push({kind:'callback-start',index:entry.order});
  try{const result=fn(...args);if(result&&typeof result.then==='function')return result.then(v=>{entry.outcome='return';entry.value=encode(v);rec.events.push({kind:'callback-end',index:entry.order});return v;},e=>{entry.outcome='throw';entry.error=error(e);rec.events.push({kind:'callback-end',index:entry.order});throw e;});entry.outcome='return';entry.value=encode(result);rec.events.push({kind:'callback-end',index:entry.order});return result;}
  catch(e){entry.outcome='throw';entry.error=error(e);rec.events.push({kind:'callback-end',index:entry.order});throw e;}};
 for(const[k,v]of Object.entries(fn))wrapped[k]=typeof v==='function'?callback(rec,v,label+'.'+k):v;
 return wrapped;
}
export function capture(name,fn,args){
 const parent=context.getStore();if(parent?.record)return fn(...args);
 const rec={id:suite+':'+String(++serial).padStart(4,'0'),test:parent?.test||'supplement',function:name,arguments:args.map((x,i)=>typeof x==='function'?{$callback:String(i)}:encode(x)),files:{},callbacks:[],clocks:[],events:[]};records.push(rec);
 const actual=args.map((x,i)=>typeof x==='function'?callback(rec,x,String(i)):x);
 return context.run({record:rec,test:rec.test},()=>{
  const finish=(kind,value)=>{rec.outcome=kind;if(kind==='return')rec.value=encode(value);else rec.error=error(value);rec.argumentsAfter=args.map((x,i)=>typeof x==='function'?{$callback:String(i)}:encode(x));};
  try{const value=fn(...actual);if(value&&typeof value.then==='function')return value.then(v=>{finish('return',v);return v;},e=>{finish('throw',e);throw e;});finish('return',value);return value;}catch(e){finish('throw',e);throw e;}
 });
}
process.on('exit',()=>{const file=path.join(output,suite+'.json');original.write(file,JSON.stringify({origin:'SYNTHETIC TEST ONLY; original operational functions unmodified',node:process.version,records},null,2)+'\n',{flag:'wx'});});
