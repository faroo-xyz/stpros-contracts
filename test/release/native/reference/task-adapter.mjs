// Test-only loading of the original Task callbacks; no Hardhat process or wallet.
import fs from'node:fs';import vm from'node:vm';import{createRequire}from'node:module';import{pathToFileURL}from'node:url';
const root=process.cwd(),require=createRequire(root+'/package.json'),ts=require('typescript');
const core=await import(pathToFileURL(root+'/tasks/release/core.mjs'));
const ro=await import(pathToFileURL(root+'/tasks/release/read-only.mjs'));
const timelock=await import(pathToFileURL(root+'/tasks/release/timelock-history.mjs'));
const compiled=ts.transpileModule(fs.readFileSync(root+'/tasks/release/index.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
export async function invokeTask(name,args,rpc,io){
 const actions={},exports={};
 const task=n=>{const b={addOption(){return b;},setInlineAction(fn){actions[n]=fn;return b;},build(){return b;}};return b;};
 const modules={'hardhat/config':{task},'./core.mjs':core,'./timelock-history.mjs':timelock,'./read-only.mjs':{...ro,readOnlyRpc:(url,simulation=false,localOnly=false)=>{io.factories.push({url,simulation,localOnly});ro.readOnlyRpc(url,simulation,localOnly);return rpc;}}};
 vm.runInNewContext(compiled,{exports,require:n=>{if(!(n in modules))throw Error('Unexpected original Task import');return modules[n];},console:{log:value=>io.stdout.push(String(value)+'\n')}},{filename:root+'/tasks/release/index.ts'});
 await actions[name](args);return{stdout:io.stdout.join('')};
}
