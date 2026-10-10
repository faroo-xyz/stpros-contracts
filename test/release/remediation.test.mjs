import test from "node:test";
import assert from "node:assert/strict";
import { encodeEventTopics, encodeFunctionData, encodeAbiParameters, parseAbi, keccak256, stringToHex } from "viem";
import { fixture, address } from "./fixture.mjs";
import { rpcFixture, hash } from "./read-only-fixture.mjs";
import { operation, validateConfig, ZERO, ZERO32, buildU, expectations, canonical } from "../../tasks/release/core.mjs";
import { replayOperations, checkOperationPlan, requireActionBearingCoverage } from "../../tasks/release/timelock-history.mjs";
import { replayPool } from "../../tasks/release/pool-replay.mjs";
import { recheckObserved, verifyCheckpoint } from "../../tasks/release/read-only.mjs";
import { termsHash } from "../../tasks/release/residual-replay.mjs";
const topic = s => keccak256(stringToHex(s));
for (const chain of [1672,688689]) for (const field of ["reporter","keeper"]) for (const authority of ["gov","can","timelock"])
    test(`effective ${chain} ${field} cannot equal ${authority}`,()=>{
        const {c}=fixture(chain), target=c.actors[authority]??c.addresses[authority];
        if(chain===688689&&field==="reporter") c.observed.core.reporter=target; else c.actors[field]=target;
        assert.throws(()=>validateConfig(c),/OPERATIONAL_TIMELOCK_AUTHORITY/);
    });
test("testnet uses preserved reporter; OPS/GRD alias remains supported",()=>{
    const {c}=fixture(688689); c.actors.reporter=c.actors.gov; c.actors.ops=c.actors.guardian; validateConfig(c);
});
const tlAbi=parseAbi(["event CallScheduled(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data,bytes32 predecessor,uint256 delay)","event CallExecuted(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data)","event Cancelled(bytes32 indexed id)"]);
const OP_ID=operation([20,21].map(i=>({to:address(i),value:0n,data:"0x1234"})),ZERO32,ZERO32).id;
const SINGLE_ID=operation([{to:address(20),value:0n,data:"0x1234"}],ZERO32,ZERO32).id;
function operationsFixture(){
 const rows=[],state=new Map();let tx=1,time=100n;
 function event(name,id=OP_ID,count=2){
  for(let i=0;i<(name==='Cancelled'?1:count);i++) rows.push({topics:encodeEventTopics({abi:tlAbi,eventName:name,args:{id,index:BigInt(i)}}),data:name==='Cancelled'?'0x':encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},...(name==='CallScheduled'?[{type:'bytes32'},{type:'uint256'}]:[])],[address(20+i),0n,'0x1234',...(name==='CallScheduled'?[ZERO32,10n]:[])]),transactionHash:hash(tx),blockHash:hash(tx),blockNumber:hash(tx),logIndex:hash(rows.length),time});
  state.set(id,name==='Cancelled'?0n:name==='CallExecuted'?1n:time+10n);tx++;time+=20n;
 }
 return {rows,state,event,run:()=>replayOperations(rows,async l=>l.time,async id=>state.get(id)??0n)};
}
test("Timelock multi-call IDs, cancellation, reschedule, execution, plan",async()=>{
 const f=operationsFixture();f.event('CallScheduled');f.event('Cancelled');f.event('CallScheduled');f.event('CallExecuted');f.event('CallScheduled',SINGLE_ID,1);
 const r=await f.run();assert.equal(r.events.length,8);checkOperationPlan(r,[SINGLE_ID],OP_ID);assert.throws(()=>checkOperationPlan(r,[]),/OUTSTANDING/);assert.throws(()=>checkOperationPlan(r,[SINGLE_ID],hash(3)),/NOT_EXECUTED/);
});
for(const [label,mutate,error] of [
 ['duplicate log',f=>f.rows.splice(1,0,f.rows[0]),/DUPLICATE/],
 ['duplicate index',f=>f.rows[1].topics[2]=hash(0),/INDICES/],
 ['missing index',f=>f.rows.shift(),/INDICES/],
 ['state mismatch',f=>f.state.set(OP_ID,0n),/OPERATION_STATE/],
 ['reschedule pending',f=>f.event('CallScheduled'),/RESCHEDULE/],
 ['execution wrong payload',f=>{f.event('CallExecuted');f.rows.at(-1).data=f.rows.at(-2).data;},/EXECUTION_HISTORY/],
]) test(`Timelock rejects ${label}`,async()=>{const f=operationsFixture();f.event('CallScheduled');mutate(f);await assert.rejects(f.run(),error);});
test("Timelock accepts empty cancellation metadata but rejects unknown execution and early execution",async()=>{
 const empty=operationsFixture();empty.event('Cancelled');assert.equal((await empty.run()).emptyMetadata[OP_ID].currentState,'Unset');
 const unknown=operationsFixture();unknown.event('CallExecuted');await assert.rejects(unknown.run(),/HISTORY/);
 const f=operationsFixture();f.event('CallScheduled');f.event('CallExecuted');f.rows.at(-1).time=101n;f.rows.at(-2).time=101n;await assert.rejects(f.run(),/EARLY/);
});
function poolFixture(assets=3n,shares=2n,T=10n,V=6n){
 const {c}=fixture(688689),rows=[],user=address(900),d=c.addresses;
 const add=(target,sig,topics,values)=>rows.push({address:target,topics:[topic(sig),...topics.map(x=>x.length===42?hash(BigInt(x)):x)],data:encodeAbiParameters(values.map(()=>({type:'uint256'})),values),transactionHash:hash(1),blockHash:hash(1),logIndex:hash(rows.length)});
 const remain=V-shares,after={poolT:remain===0n?0n:T-assets,poolV:remain};
 add(d.stpros,'Transfer(address,address,uint256)',[user,ZERO],[shares]);
 add(d.oracle,'PoolUpdated(address,uint256,uint256,bool)',[d.asset],[after.poolT,remain,0n]);
 add(d.stpros,'Withdraw(address,address,address,uint256,uint256)',[user,user,user],[assets,shares]);
 return {c,rows,before:{poolT:T,poolV:V},after,run(){return replayPool(c,rows,this.before,this.after);}};
}
for(const [label,a,s,t,v] of [['ordinary',3n,2n,10n,6n],['last share',10n,6n,10n,6n],['positive dust',9n,6n,10n,6n],['nonzero remaining supply',9n,5n,10n,6n]])test(`pool replay ${label}`,()=>poolFixture(a,s,t,v).run());
for(const [label,mutate,error] of [
 ['unexplained NAV',f=>f.after.poolT=999n,/UNRECONCILED_POOL/],
 ['unexplained shares',f=>f.after.poolV=999n,/UNRECONCILED_POOL/],
 ['wrong pool event',f=>f.rows[1].data=encodeAbiParameters([{type:'uint256'},{type:'uint256'},{type:'bool'}],[0n,0n,false]),/POOL_EVENT_ACCOUNTING/],
 ['missing burn',f=>f.rows.shift(),/POOL_EVENT_SEQUENCE/],
 ['wrong burn amount',f=>f.rows[0].data=hash(1),/POOL_SHARE_ACCOUNTING/],
 ['wrong burn owner',f=>f.rows[0].topics[1]=hash(999),/POOL_SHARE_ACCOUNTING/],
 ['wrong transaction',f=>f.rows[1].transactionHash=hash(99),/POOL_EVENT_SEQUENCE/],
 ['event after withdrawal',f=>[f.rows[1],f.rows[2]]=[f.rows[2],f.rows[1]],/POOL_EVENT_SEQUENCE/],
 ['unmatched event',f=>f.rows.pop(),/POOL_INCOMPLETE_SEQUENCE/],
])test(`pool rejects ${label}`,()=>{const f=poolFixture();mutate(f);assert.throws(()=>f.run(),error);});
function recovery(chain=1672,p1='C'){
 const f=rpcFixture(chain),c=f.c;
 if(chain===1672){c.p1Case=p1;c.fallbackApproval={ownerApproved:true,reference:'reviewed residual recovery'};}
 else {c.testnetRecovery={e2Unavailable:true,ownerApproval:'reviewed E2 recovery'};for(const v of c.factories[0].vaults)v.endState='RETIRED';}
 f.archive();return f;
}
function residualRound(f){
 const v=f.c.chainId===1672?f.c.factories[1].vaults[0]:f.c.factories[0].vaults[0],s=f.current.observed.vaults[v.address],at=BigInt(f.head.timestamp);
 f.log(f.current,v.address,topic('RoundOpened(uint256,uint256,uint256,uint256)'),[hash(2)],[at,at+100n,100n]);
 Object.assign(s,{round:'2',phase:0});s.terms[0]='100';s.terms[3]='100';s.terms[8]=String(at);s.terms[9]=String(at+100n);s.termsHash=termsHash(s.terms);
 const fields=["openWindow","lockDuration","settleTimelockWindow","roundCap","perAddressCap","minSubscription","performanceFeeBps","maxLossBps",...(f.c.chainId===1672?["openedAt"]:[])];
 const l=f.current.logs.at(-1);f.current.transactions[l.transactionHash]={hash:l.transactionHash,blockHash:l.blockHash,blockNumber:l.blockNumber,to:v.address,input:encodeFunctionData({abi:parseAbi([`function openNextRound((${fields.map(n=>`uint256 ${n}`).join(',')}) params)`]),functionName:'openNextRound',args:[Object.fromEntries(fields.map((n,i)=>[n,BigInt(s.terms[i])]))]})};return v;
}
for(const [chain,p1] of [[1672,'C'],[1672,'D'],[688689,'A']]) test(`approved residual ${chain}/${p1} reconciles real round transition; unexplained terms rejected`,async()=>{
 const f=recovery(chain,p1),v=residualRound(f),id=buildU(f.c,f.inventory,f.artifacts).operation.id;
 assert.equal((await recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now})).status,'PASS');
 assert.equal(buildU(f.c,f.inventory,f.artifacts).operation.id,id);
 f.current.observed.vaults[v.address].round='3';await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/RESIDUAL_STATE_UNEXPLAINED/);
});
test("unexplained residual terms without events fail; governed never takes exception",async()=>{
 const f=recovery(),v=f.c.factories[1].vaults[0];f.current.observed.vaults[v.address].terms[1]='99';f.current.observed.vaults[v.address].termsHash=termsHash(f.current.observed.vaults[v.address].terms);
 await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/RESIDUAL_TERMS_UNEXPLAINED/);
 const g=rpcFixture();g.current.observed.vaults[g.c.factories[0].vaults[0].address].round='2';await assert.rejects(recheckObserved(g.c,g.inventory,g.rpc,{nowSeconds:g.now}),/VAULT_STATE_DRIFT/);
});
test("complete preflight refuses unrelated pending Timelock operation",async()=>{
 const f=rpcFixture();f.operationHistory();await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/TIMELOCK_OUTSTANDING_OPERATIONS/);
});
function checkpointState(f, stage='H'){
 const c=f.c,d=c.addresses,e=expectations(c,f.inventory,f.artifacts,stage);
 f.head.timestamp=f.pin.timestamp+c.policy.minDelay+2;f.now=f.head.timestamp+1;
 f.current.observed.proxyAdminOwner=d.timelock;
 for(const p of e.proxies){Object.assign(f.current.observed.proxies[p.proxy],{owner:p.owner,implementation:p.implementation,version:p.guarded?4:c.legacy[p.proxy].version});if(p.implementation!==c.legacy[p.proxy].implementation)f.log(f.current,p.proxy,topic('Upgraded(address)'),[p.implementation],[]);}
 if(stage==='R4')for(const b of e.beacons){f.current.observed.factories[b.factory].implementation=b.implementation;f.log(f.current,b.beacon,topic('Upgraded(address)'),[b.implementation],[]);}
 for(const r of e.roles){const old=f.inventory.roles[r.target].current.includes(`${r.role}:${r.account}`);if(old!==r.expected)f.log(f.current,r.target,topic(`${r.expected?'RoleGranted':'RoleRevoked'}(bytes32,address,address)`),[r.role,r.account,c.actors.gov],[]);}
 const id=f.operationHistory(stage==='R4');f.now=f.head.timestamp+1;
 const options={nowSeconds:f.now,executionEvidence:{operationId:id,forkReference:'local',freshFork:'PASS',fs6:'PASS',blockNumber:f.head.number,blockHash:f.head.hash,uGas:1000000,nonces:{gov:0,can:0}}};
 return ()=>verifyCheckpoint(c,f.inventory,f.artifacts,f.rpc,stage,options);
}
for(const [chain,p1] of [[1672,'C'],[1672,'D'],[688689,'A']])test(`residual ${chain}/${p1} H checkpoint accepts reconstructed changed terms and keeps U fixed`,async()=>{
 const f=recovery(chain,p1);const check=checkpointState(f);residualRound(f);const old=canonical(buildU(f.c,f.inventory,f.artifacts).operation);
 assert.equal((await check()).status,'PASS');assert.equal(canonical(buildU(f.c,f.inventory,f.artifacts).operation),old);
 const v=chain===1672?f.c.factories[1].vaults[0]:f.c.factories[0].vaults[0];
 f.current.observed.vaults[v.address].terms[1]='123';f.current.observed.vaults[v.address].termsHash=termsHash(f.current.observed.vaults[v.address].terms);
 await assert.rejects(check(),/RESIDUAL_TERMS_UNEXPLAINED/);
});
test('zero-supply dust normalization passes complete testnet post-U checkpoint; wrong balances fail',async()=>{
 const f=rpcFixture(688689),d=f.c.addresses,user=address(900),S=90n,T=180n,A=179n;
 for(const x of [f.c.observed.core,f.historical.observed.core,f.current.observed.core])Object.assign(x,{supply:String(S),poolT:String(T),poolV:String(S)});
 f.inventory.holders[d.stpros]={status:'PASS',mismatches:[],totalSupply:String(S),accounts:[{address:user,balance:String(S)}]};
 for(const s of [f.historical,f.current]){s.balances[d.stpros]={[user]:String(S)};f.log(s,d.stpros,topic('Transfer(address,address,uint256)'),[ZERO,user],[S],2);}f.archive();
 const check=checkpointState(f,'R4');assert.equal((await check()).status,'PASS');
 f.log(f.current,d.stpros,topic('Transfer(address,address,uint256)'),[user,ZERO],[S]);
 f.log(f.current,d.oracle,topic('PoolUpdated(address,uint256,uint256,bool)'),[d.asset],[0,0,0]);
 f.log(f.current,d.stpros,topic('Withdraw(address,address,address,uint256,uint256)'),[user,user,user],[A,S]);
 for(const l of f.current.logs.slice(-3))l.transactionHash=hash(999001);
 Object.assign(f.current.observed.core,{supply:'0',poolT:'0',poolV:'0',queued:String(A),interval:3600});f.current.balances[d.stpros][user]='0';
 f.current.withdrawals=[{owner:user,head:'0',tail:'1',records:[{queued:'0',pending:String(A),createdAt:String(f.head.timestamp),unbondingPeriod:'0',receiver:ZERO}]}];
 assert.equal((await check()).status,'PASS');f.current.balances[d.stpros][user]='1';await assert.rejects(check(),/TRANSFER_REPLAY/);
});
test('post-U rejects unrelated operation even when expected U is done',async()=>{
 const f=rpcFixture(688689),check=checkpointState(f,'R4');assert.equal((await check()).status,'PASS');
 const scheduled=f.current.logs.filter(l=>l.address===f.c.addresses.timelock&&l.topics[0]===topic('CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)'));
 const op=buildU(f.c,f.inventory,f.artifacts).operation,extraSalt=hash(5555),extra=operation(op.targets.map((to,i)=>({to,value:op.values[i],data:op.payloads[i]})),op.predecessor,extraSalt).id;for(const l of scheduled)f.current.logs.push({...l,topics:[l.topics[0],extra,l.topics[2]],transactionHash:hash(888888),logIndex:hash(f.current.logs.length)});
 f.current.logs.push({...scheduled[0],topics:[topic("CallSalt(bytes32,bytes32)"),extra],data:extraSalt,transactionHash:hash(888888),logIndex:hash(f.current.logs.length)});
 f.current.operationTimestamps[extra]=BigInt(f.pin.timestamp+1+f.c.policy.minDelay);
 await assert.rejects(check(),/TIMELOCK_OUTSTANDING_OPERATIONS/);
});

test('parsing alone never authorizes action-bearing release coverage',async()=>{
 const f=operationsFixture();const result=await f.run();assert.equal(result.actionBearingCoverage,"UNATTESTED");assert.equal(result.literalAllOperationIds,"NOT_CLAIMED");
 assert.throws(()=>requireActionBearingCoverage({timelockOperations:result}),/INCOMPLETE_TIMELOCK_ACTION_COVERAGE/);
 assert.throws(()=>requireActionBearingCoverage({}),/INCOMPLETE_TIMELOCK_ACTION_COVERAGE/);
});
test('residual funded profit backing cannot be excluded from the current checkpoint',async()=>{
 const f=recovery(),v=f.c.factories[1].vaults[0];
 for(const state of [f.c.observed,f.historical.observed,f.current.observed])Object.assign(state.vaults[v.address],{phase:2,funded:true,settleAmount:'1',settleMode:0});
 f.archive();await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/RESIDUAL_FUNDED_BACKING/);
});
test('residual zero-profit round can return to SETTLED with new authenticated terms',async()=>{
 const f=recovery(),v=residualRound(f),s=f.current.observed.vaults[v.address];
 // One short historical round, still inside the one-hour pre-schedule review window.
 f.head.timestamp+=101;f.now=f.head.timestamp+1;
 for(const l of f.current.logs.filter(l=>Number(BigInt(l.blockNumber))===f.head.number))l.blockHash=f.head.hash;
 f.log(f.current,v.address,topic('RoundLocked(uint256,uint256,uint256)'),[hash(2)],[BigInt(s.terms[9]),BigInt(s.terms[9])+1n]);
 f.log(f.current,v.address,topic('SettlementProposed(uint256,uint8,uint256,uint256,bool,address)'),[hash(2),hash(0),ZERO],[0,BigInt(f.head.timestamp),1]);
 f.log(f.current,v.address,topic('SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)'),[hash(2),hash(0)],[0,0,0,BigInt(f.head.timestamp)]);
 s.phase=3;
 assert.equal((await recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now})).status,'PASS');
 f.current.tma[v.address]='1';f.current.balances[f.c.addresses.stpros]={[v.address]:'1'};
 await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/TRANSFER_REPLAY|HOLDER_SUM|UNRECONCILED_VAULT_ASSETS/);
});
for(const stage of ['H','R4'])test(`GC02 signer rotation is detected at ${stage} checkpoint`,async()=>{
 const f=rpcFixture(688689),check=checkpointState(f,stage);requireActionBearingCoverage(await check());
 f.current.safes.ops.owners[0]=address(9999);await assert.rejects(check(),/SAFE_OWNERS/);
});
for(const phase of [0,1,2])test(`GC01 execution checkpoint rejects mainnet governed phase ${phase}`,async()=>{
 const f=rpcFixture(),check=checkpointState(f,'H');f.current.observed.vaults[f.c.factories[0].vaults[0].address].phase=phase;
 await assert.rejects(check(),/MAIN_VAULT_PHASE/);
});
for(const chain of [1672,688689])test(`Safe forwarding remains evidence-required for ${chain} residual activity`,async()=>{
 const f=recovery(chain),v=residualRound(f),row=f.current.logs.at(-1),tx=f.current.transactions[row.transactionHash];
 tx.to=f.c.actors.guardian;
 await assert.rejects(recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now}),/INCOMPLETE_SAFE_RECOVERY_EVIDENCE/);
 assert.equal(f.c.factories.flatMap(x=>x.vaults).find(x=>x.address===v.address).endState,'RETIRED');
});
