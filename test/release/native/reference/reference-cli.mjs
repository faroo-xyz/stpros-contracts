// Synthetic adapter executes the original TypeScript Task callbacks, unchanged.
import fs from'node:fs';import path from'node:path';import vm from'node:vm';import{createRequire}from'node:module';import{pathToFileURL}from'node:url';
import {capture,context} from './capture-runtime.mjs';
const root=process.cwd(),require=createRequire(root+'/package.json'),ts=require('typescript');
const core=await import(pathToFileURL(root+'/tasks/release/core.mjs')),ro=await import(pathToFileURL(root+'/tasks/release/read-only.mjs')),tl=await import(pathToFileURL(root+'/tasks/release/timelock-history.mjs'));
const {fixture}=await import(pathToFileURL(root+'/test/release/fixture.mjs'));
const {rpcFixture}=await import(pathToFileURL(root+'/test/release/read-only-fixture.mjs'));
const originalTests=fs.readFileSync(root+'/test/release/read-only.integration.test.mjs','utf8');
const ast=ts.createSourceFile('reference.mjs',originalTests,ts.ScriptTarget.ESNext,true,ts.ScriptKind.JS);
const helpers=ast.statements.filter(x=>ts.isFunctionDeclaration(x)&&['handoffFixture','r4Fixture'].includes(x.name?.text)).map(x=>x.getText(ast)).join('\n');
const helperPath=root+'/test/release/native-capture-fixtures.mjs';
fs.writeFileSync(helperPath,`import {keccak256,stringToHex}from'viem';import{rpcFixture}from'./read-only-fixture.mjs';import{buildU,ROLES}from'../../tasks/release/core.mjs';\n${helpers}\nexport{handoffFixture,r4Fixture};\n`,{flag:'wx'});
const {handoffFixture,r4Fixture}=await import(pathToFileURL(helperPath));
const compiled=ts.transpileModule(fs.readFileSync(root+'/tasks/release/index.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const prefix='/private/tmp/faroo-native-release/inputs/reference-cli.mjs';let serial=0;
function argsFor(f){const dir=prefix+'/case-'+(++serial);fs.mkdirSync(dir,{recursive:true});const write=(n,v)=>{const p=dir+'/'+n+'.json';fs.writeFileSync(p,JSON.stringify(v));return p;};
 f.c.inventoryFile=write('inventory',f.inventory);f.c.artifactFiles=Object.fromEntries(Object.entries(f.artifacts).map(([n,v])=>[n,write('artifact-'+n,v)]));
 const releaseConfig=write('config',f.c),args={releaseConfig,reviewHash:core.sha256(fs.readFileSync(releaseConfig)),rpc:f.c.mode==='simulation'?'http://127.0.0.1:8545':'https://rpc.pharos.xyz',checkpoint:'pre',checkpointReview:'',checkpointReviewHash:''};
 if(f.evidence){args.checkpointReview=write('execution',f.evidence);args.checkpointReviewHash=core.sha256(fs.readFileSync(args.checkpointReview));}
 return args;
}
async function action(name,args,rpc,io){
 const actions={},exports={};
 const task=(n)=>{const builder={addOption(){return builder;},setInlineAction(fn){actions[n]=fn;return builder;},build(){return builder;}};return builder;};
 const modules={'hardhat/config':{task},'./core.mjs':core,'./timelock-history.mjs':tl,'./read-only.mjs':{...ro,readOnlyRpc:(url,simulation=false,localOnly=false)=>{io.factories.push({url,simulation,localOnly});ro.readOnlyRpc(url,simulation,localOnly);return rpc;}}};
 vm.runInNewContext(compiled,{exports,require:n=>{if(!(n in modules))throw Error('Unexpected Task import: '+n);return modules[n];},console:{log:s=>io.stdout.push(String(s)+'\n')}},{filename:root+'/tasks/release/index.ts'});
 return actions[name](args);
}
async function run(label,name,f,change=()=>{}){
 Date.now=()=>1000*(f.now??1800000002);const args=argsFor(f);change(args,f);const io={factories:[],stdout:[]};
 const rpc=f.rpc??(async()=> '0x12345');
 await context.run({test:label},async()=>{try{await capture('index.ts.'+name,async(a,cb,trace)=>{await action(name,a,cb,trace);return {stdout:trace.stdout.join('')};},[args,rpc,io]);}catch{}});
}
for(const chain of [1672,688689])for(const name of ['preflight','authority','build-u','p1-runbook','build-handoff','testnet-handoff','estimate-gas']){
 if((chain===1672&&name==='testnet-handoff')||(chain===688689&&['p1-runbook','build-handoff'].includes(name)))continue;
 await run('CLI simulation '+chain+' '+name,'release:'+name,fixture(chain));
}
for(const chain of [1672,688689])for(const name of ['preflight','authority','build-u',...(chain===688689?['testnet-handoff']:[])])await run('CLI live synthetic '+chain+' '+name,'release:'+name,rpcFixture(chain));
for(const chain of [1672,688689]){
 await run('CLI execution frozen bundle '+chain,'release:preflight',await handoffFixture(chain),a=>a.checkpoint='execution');
 await run('CLI postcheck consumed nonce '+chain,'release:postcheck',await r4Fixture(chain));
}
for(const [label,change]of [
 ['missing config',a=>a.releaseConfig=''],['missing hash',a=>a.reviewHash=''],['invalid hash',a=>a.reviewHash='bad'],['wrong hash',a=>a.reviewHash='0'.repeat(64)],['uppercase hash',a=>a.reviewHash=a.reviewHash.toUpperCase()],
 ['invalid checkpoint',a=>a.checkpoint='later'],['unknown RPC',a=>a.rpc='https://example.invalid'],['missing RPC',a=>a.rpc='']
])await run('CLI refuses '+label,'release:preflight',rpcFixture(),change);
for(const [label,change]of [['missing execution proof',a=>a.checkpointReview=''],['missing proof hash',a=>a.checkpointReviewHash=''],['wrong proof hash',a=>a.checkpointReviewHash='0'.repeat(64)]])await run('CLI execution refuses '+label,'release:preflight',await handoffFixture(),a=>{a.checkpoint='execution';change(a);});
await run('CLI local gas rejects public RPC','release:estimate-gas',fixture(),a=>a.rpc='https://rpc.pharos.xyz');
await run('CLI local gas missing RPC','release:estimate-gas',fixture(),a=>a.rpc='');
const mixed=fixture();let gas=0;mixed.rpc=async()=>{if(++gas===2)throw Error('SYNTHETIC_GAS_REVERT');return'0x12345';};await run('CLI per-call gas stays incomplete','release:estimate-gas',mixed);
