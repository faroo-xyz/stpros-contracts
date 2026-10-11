import fs from'node:fs';import vm from'node:vm';import{createRequire}from'node:module';import{pathToFileURL}from'node:url';import{createHash}from'node:crypto';
const root=process.cwd(),require=createRequire(root+'/package.json'),v=require('viem'),sources={};
function read(name){const text=fs.readFileSync(root+'/tasks/release/'+name,'utf8');sources['tasks/release/'+name]=createHash('sha256').update(text).digest('hex');return text;}
function extract(name,start,end,result){const s=read(name);return vm.runInNewContext(s.slice(s.indexOf(start),s.indexOf(end))+';'+result,{parseAbi:v.parseAbi});}
const [core,stMigrate]=extract('core.mjs','const ABI =','export function fail','[ABI,ST_MIGRATE]');
const reads=extract('read-only.mjs','const READS =','const allowed =','READS');
const poolEvents=extract('pool-replay.mjs','const abi =','export function replayPool','abi');
const timelockEvents=extract('timelock-history.mjs','const abi =','export const operationTopics','abi');
const residualEvents=extract('residual-replay.mjs','const abi =','// Only event-explained','abi');
const residual=await import(pathToFileURL(root+'/tasks/release/residual-replay.mjs'));
const withdrawalsV1=v.parseAbi(['function getWithdrawals(address) view returns((uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod)[])']);
const withdrawalsV3=v.parseAbi(['function getWithdrawals(address) view returns((uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod,address receiver)[])']);
const round=n=>v.parseAbi([`function openNextRound((${residual.termNames.slice(0,n).map(x=>'uint256 '+x).join(',')}) params)`]);
console.log(JSON.stringify({sourceCommit:'4d9d48db1facb2ae54242f902a1344536ba21dfb',sourceSha256:sources,schemas:{core,stMigrate,reads,poolEvents,timelockEvents,residualEvents,residualReads:residual.residualReads,withdrawalsV1,withdrawalsV3,openRoundV1:round(9),openRoundV3:round(8)}},null,2));
