import {registerHooks}from'node:module';import{fileURLToPath,pathToFileURL}from'node:url';import path from'node:path';
import './capture-runtime.mjs';
const runtime=pathToFileURL(path.join(path.dirname(fileURLToPath(import.meta.url)),'capture-runtime.mjs')).href;
const selected={
 'core.mjs':['verifyRuntime','operation','verifyOperation','roleDigest','validateInventory','validateConfig','validateRecordedTopology','preflight','expectations','coreExpectation','buildU','requireP1Complete','buildHandoff','p1Runbook','testnetHandoff','safeBuilder','deploymentDefinitions'],
 'artifacts.mjs':['freezeArtifact'],'history.mjs':['authenticateHistory','historyTargets'],
 'timelock-history.mjs':['replayOperations','checkOperationPlan','requireActionBearingCoverage'],
 'pool-replay.mjs':['replayPool'],'residual-replay.mjs':['replayResidual','termsHash'],
 'read-only.mjs':['scanRoles','scanEconomics','reviewedContext','recheckObserved','verifyCheckpoint','estimateGas']};
registerHooks({resolve(specifier,ctx,next){
 const parent=ctx.parentURL||'';
 if(parent.endsWith('.test.mjs')){
  if(specifier==='node:test')return {url:'faroo-capture:test',shortCircuit:true};
  const result=next(specifier,ctx);if(result.url.includes('/tasks/release/')&&selected[path.basename(result.url)])return {url:'faroo-capture:'+result.url,shortCircuit:true};return result;
 }
 return next(specifier,ctx);
},load(url,ctx,next){
 if(url==='faroo-capture:test')return {format:'module',shortCircuit:true,source:`import original from 'node:test';import {context} from '${runtime}';export * from 'node:test';export default function test(name,options,fn){if(typeof options==='function'){fn=options;options=undefined;}const wrapped=(...args)=>context.run({test:name},()=>fn(...args));return options===undefined?original(name,wrapped):original(name,options,wrapped);}`};
 if(url.startsWith('faroo-capture:')){const original=url.slice('faroo-capture:'.length),names=selected[path.basename(original)];return {format:'module',shortCircuit:true,source:`export * from '${original}';import * as source from '${original}';import {capture} from '${runtime}';`+names.map(n=>`export const ${n}=(...args)=>capture('${path.basename(original)}.${n}',source.${n},args);`).join('\n')};}
 return next(url,ctx);
}});
