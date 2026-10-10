import test from 'node:test';
import assert from 'node:assert/strict';
import {parseAbi,encodeEventTopics,encodeAbiParameters,keccak256,stringToHex} from 'viem';
import {fixture,address} from './fixture.mjs';
import {rpcFixture,hash} from './read-only-fixture.mjs';
import {validateConfig,preflight,buildU,canonical,ZERO32,ZERO,operation} from '../../tasks/release/core.mjs';
import {recheckObserved,verifyCheckpoint} from '../../tasks/release/read-only.mjs';
import {replayOperations,checkOperationPlan,requireActionBearingCoverage,PINNED_TIMELOCK_RUNTIME_HASH} from '../../tasks/release/timelock-history.mjs';
const run=f=>recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now});
for(const phase of [0,1,2])test(`GC01 input and fresh observation reject mainnet phase ${phase}`,async()=>{
 const f=rpcFixture();f.c.observed.vaults[f.c.factories[0].vaults[0].address].phase=phase;assert.throws(()=>preflight(f.c,f.inventory,f.artifacts),/MAIN_VAULT_PHASE/);
 const g=rpcFixture();g.current.observed.vaults[g.c.factories[0].vaults[0].address].phase=phase;await assert.rejects(run(g),/MAIN_VAULT_PHASE/);
});
test('GC01 SETTLED accepted and frozen U unchanged by checking',async()=>{const f=rpcFixture(),id=canonical(buildU(f.c,f.inventory,f.artifacts).operation);const r=await run(f);assert.equal(r.status,'PASS');requireActionBearingCoverage(r);assert.equal(canonical(buildU(f.c,f.inventory,f.artifacts).operation),id);});
for(const phase of [0,1,2])test(`GC01 testnet governed phase ${phase} remains supported`,async()=>{const f=rpcFixture(688689),v=f.c.factories[0].vaults[14].address;for(const o of [f.c.observed,f.historical.observed,f.current.observed])o.vaults[v].phase=phase;f.archive();assert.equal((await run(f)).status,'PASS');});
for(const actor of ['gov','can','timelock'])test(`GC02 OPS equals ${actor} rejects`,()=>{const {c}=fixture();c.actors.ops=c.actors[actor]??c.addresses[actor];assert.throws(()=>validateConfig(c),/ACTOR_SEPARATION/);});
for(const role of ['gov','can'])test(`GC02 independent Safe addresses with ${role}/OPS owner overlap reject`,()=>{const f=rpcFixture();f.c.review.safes.ops.owners[0]=f.c.review.safes[role].owners[0];assert.throws(()=>validateConfig(f.c),/SAFE_SIGNER_OVERLAP/);});
test('GC02 OPS equals reviewed GRD uses same identity and passes historical/current checks',async()=>{const f=rpcFixture();f.c.actors.ops=f.c.actors.guardian;f.c.review.safes.ops=f.c.review.safes.guardian;for(const s of [f.historical,f.current])s.safes.ops=structuredClone(s.safes.guardian);f.archive();assert.equal((await run(f)).status,'PASS');});
test('GC02 conflicting alias configurations are rejected before deduplicating views',()=>{const f=rpcFixture();f.c.actors.ops=f.c.actors.guardian;f.c.review.safes.ops.address=f.c.actors.guardian;assert.throws(()=>validateConfig(f.c),/SAFE_ALIAS_CONFIGURATION/);});
test('GC02 actual current OPS signer rotation is detected',async()=>{const f=rpcFixture();f.current.safes.ops.owners[0]=address(999);await assert.rejects(run(f),/SAFE_OWNERS/);});
for(const at of ['historical','current'])test(`GC02 ${at} contract/delegated signer without control proof is INCOMPLETE`,async()=>{const f=rpcFixture(),owner=f.c.review.safes.ops.owners[0];f[at].observed.code[owner]='0x600000';await assert.rejects(run(f),/INCOMPLETE_SAFE_SIGNER_CONTROL/);});
for(const [field,mutate,error] of [['singleton',s=>s.singleton=address(999),/SAFE_SINGLETON/],['threshold',s=>s.threshold=1,/SAFE_THRESHOLD/],['module',s=>s.modules=[address(999)],/SAFE_MODULES/],['guard',s=>s.guard=address(999),/SAFE_GUARD/]])test(`GC02 actual OPS ${field} mismatch rejects`,async()=>{const f=rpcFixture();mutate(f.current.safes.ops);await assert.rejects(run(f),error);});
test('GC03 canonical runtime identity cannot be replaced by reviewed synthetic code',async()=>{const f=rpcFixture();for(const o of [f.c.observed,f.historical.observed,f.current.observed])o.code[f.c.addresses.timelock]='0x600000';await assert.rejects(run(f),/TIMELOCK_PROOF_RUNTIME/);});
const ABI=parseAbi(['event CallScheduled(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data,bytes32 predecessor,uint256 delay)','event CallExecuted(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data)','event CallSalt(bytes32 indexed id,bytes32 salt)','event Cancelled(bytes32 indexed id)']);
function history(){
 const rows=[],states=new Map(),times=new Map();let tx=1,now=100n;
 const op=(n=2,salt=ZERO32,predecessor=ZERO32,single=false)=>{const calls=Array.from({length:n},(_,i)=>({target:address(50+i),value:BigInt(i),data:i?'0x':'0x1234'}));const id=single?keccak256(encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},{type:'bytes32'},{type:'bytes32'}],[calls[0].target,calls[0].value,calls[0].data,predecessor,salt])):keccak256(encodeAbiParameters([{type:'address[]'},{type:'uint256[]'},{type:'bytes[]'},{type:'bytes32'},{type:'bytes32'}],[calls.map(x=>x.target),calls.map(x=>x.value),calls.map(x=>x.data),predecessor,salt]));return {id,calls,salt,predecessor};};
 function emit(name,o,index=0){const c=o.calls[index];const data=name==='Cancelled'?'0x':name==='CallSalt'?o.salt:encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},...(name==='CallScheduled'?[{type:'bytes32'},{type:'uint256'}]:[])],[c.target,c.value,c.data,...(name==='CallScheduled'?[o.predecessor,10n]:[])]);rows.push({topics:encodeEventTopics({abi:ABI,eventName:name,args:{id:o.id,index:BigInt(index)}}),data,blockNumber:hash(tx),blockHash:hash(tx),transactionHash:hash(tx),logIndex:hash(rows.length)});times.set(hash(tx),now);}
 function next(){tx++;now+=20n;}
 function schedule(o){for(let i=0;i<o.calls.length;i++)emit('CallScheduled',o,i);if(o.salt!==ZERO32)emit('CallSalt',o);states.set(o.id,now+10n);next();}
 function cancel(o){emit('Cancelled',o);states.set(o.id,0n);next();}
 function execute(o){for(let i=0;i<o.calls.length;i++)emit('CallExecuted',o,i);states.set(o.id,1n);next();}
 return {rows,states,times,op,emit,next,schedule,cancel,execute,run:()=>replayOperations(rows,async l=>times.get(l.blockHash),async id=>states.get(id),1000n)};
}
for(const [label,n,salt,single] of [['single zero',1,ZERO32,true],['single salt',1,hash(3),true],['batch one',1,ZERO32,false],['batch many salt',3,hash(7),false]])test(`GC03 ${label} reconstructs identity and lifecycle`,async()=>{const f=history(),o=f.op(n,salt,ZERO32,single);f.schedule(o);f.cancel(o);f.schedule(o);f.execute(o);const r=await f.run();assert.equal(r.operations[o.id].shape,single?'single':'batch');assert.equal(r.operations[o.id].currentState,'Done');assert.equal(r.episodes[0].status,'cancelled');assert.equal(r.episodes[1].status,'done');checkOperationPlan(r,[],o.id);});
test('GC03 valid nested execution interleaves without losing outer indices',async()=>{const f=history(),outer=f.op(2,hash(1)),inner=f.op(1,hash(2),ZERO32,true);f.schedule(outer);f.schedule(inner);f.emit('CallExecuted',outer,0);f.emit('CallExecuted',inner,0);f.emit('CallExecuted',outer,1);f.states.set(outer.id,1n);f.states.set(inner.id,1n);const r=await f.run();assert.deepEqual(r.pending,[]);assert.equal(r.operations[outer.id].executionCount,2);assert.equal(r.operations[inner.id].executionCount,1);});
test('GC03 empty metadata is observed without claiming silent transitions',async()=>{const f=history(),a=f.op(0,hash(8)),b=f.op(0);f.schedule(a);f.cancel(a);f.schedule(a);f.states.set(a.id,1n);f.cancel(b);f.states.set(b.id,220n);const r=await f.run();assert.deepEqual(r.pending,[]);assert.equal(r.emptyMetadata[a.id].currentState,'Done');assert.equal(r.emptyMetadata[b.id].currentState,'Ready');assert.equal(r.literalAllOperationIds,'NOT_CLAIMED');});
for(const ts of [0n,1n,2000n])test(`GC03 pending action with predecessor timestamp ${ts} remains outstanding`,async()=>{const f=history(),pred=f.op(0,hash(44)),o=f.op(1,ZERO32,pred.id);f.states.set(pred.id,ts);f.schedule(o);const r=await f.run();assert.deepEqual(r.pending,[o.id]);assert.equal(r.predecessorStates[pred.id].done,ts===1n);assert.throws(()=>checkOperationPlan(r,[]),/OUTSTANDING/);});
test('GC03 missing predecessor response cannot be inferred Done',async()=>{const f=history(),o=f.op(1,ZERO32,hash(99));f.schedule(o);await assert.rejects(f.run(),/DEPENDENCY_STATE/);});
test('GC03 executed action requires referenced predecessor Done',async()=>{const f=history(),o=f.op(1,ZERO32,hash(99));f.schedule(o);f.execute(o);f.states.set(hash(99),0n);await assert.rejects(f.run(),/DEPENDENCY_NOT_DONE/);f.states.set(hash(99),1n);assert.equal((await f.run()).predecessorStates[hash(99)].done,true);});
for(const [label,mutate,error] of [
 ['missing tail',f=>f.rows.splice(1,1),/OPERATION_ID/],['missing index',f=>f.rows.shift(),/INDICES/],['duplicate event',f=>f.rows.splice(1,0,f.rows[0]),/DUPLICATE/],['incorrect calldata',f=>f.rows[0].data=encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},{type:'bytes32'},{type:'uint256'}],[f.o.calls[0].target,f.o.calls[0].value,'0x9999',f.o.predecessor,10n]),/OPERATION_ID/],['incorrect salt',f=>f.rows.at(-1).data=hash(88),/OPERATION_ID/],['incorrect predecessor',f=>{const data=f.rows[0].data;f.rows[0].data=data.slice(0,194)+hash(77).slice(2)+data.slice(258);},/METADATA|OPERATION_ID/],['incorrect timestamp',f=>f.states.set(f.o.id,999n),/OPERATION_STATE/],['missing salt',f=>f.rows.pop(),/OPERATION_ID/]
])test(`GC03 rejects ${label}`,async()=>{const f=history();f.o=f.op(2,hash(7));f.schedule(f.o);mutate(f);await assert.rejects(f.run(),error);});
test('GC03 unknown execution and incomplete batch execution reject',async()=>{const f=history(),o=f.op();f.emit('CallExecuted',o);await assert.rejects(f.run(),/EXECUTION_HISTORY/);const g=history(),p=g.op();g.schedule(p);g.emit('CallExecuted',p,0);g.states.set(p.id,1n);await assert.rejects(g.run(),/PARTIAL_EXECUTION/);});
test('GC03 one-call zero-address no-value empty-data call still counts as action',async()=>{const f=history(),o=f.op(1);o.calls[0]={target:ZERO,value:0n,data:'0x'};// The generic Timelock permits a zero target even though Faroo's release builder does not.
 o.id=keccak256(encodeAbiParameters([{type:'address[]'},{type:'uint256[]'},{type:'bytes[]'},{type:'bytes32'},{type:'bytes32'}],[[ZERO],[0n],['0x'],ZERO32,ZERO32]));f.schedule(o);const r=await f.run();assert.deepEqual(r.pending,[o.id]);});
test('GC03 gate requires archive and runtime attestations, not merely parsed rows',async()=>{const f=history();const r=await f.run();assert.equal(r.actionBearingCoverage,'UNATTESTED');assert.throws(()=>requireActionBearingCoverage({timelockOperations:r}),/INCOMPLETE_TIMELOCK_ACTION_COVERAGE/);assert.throws(()=>requireActionBearingCoverage({timelockOperations:{...r,actionBearingCoverage:'PASS',sourceRuntimeHash:PINNED_TIMELOCK_RUNTIME_HASH}}),/INCOMPLETE_TIMELOCK_ACTION_COVERAGE/);});
test('GC03 missing whole operation is an evidence limitation, not parser-certified completeness',async()=>{
 const f=history(),o=f.op();f.schedule(o);assert.deepEqual((await f.run()).pending,[o.id]);
 let reads=0;const omitted=await replayOperations([],async()=>100n,async()=>{reads++;return 110n;},1000n);
 assert.deepEqual(omitted.pending,[]);assert.equal(reads,0);assert.equal(omitted.actionBearingCoverage,'UNATTESTED');
 assert.throws(()=>requireActionBearingCoverage({timelockOperations:omitted}),/INCOMPLETE_TIMELOCK_ACTION_COVERAGE/);
});
test('GC03 canceled timestamp cannot conceal a rescheduled outstanding operation',async()=>{const f=history(),o=f.op();f.schedule(o);f.cancel(o);f.schedule(o);f.states.set(o.id,0n);await assert.rejects(f.run(),/TIMELOCK_OPERATION_STATE/);});
test('GC03 current Done cannot replace missing execution history',async()=>{const f=history(),o=f.op();f.schedule(o);f.states.set(o.id,1n);await assert.rejects(f.run(),/TIMELOCK_OPERATION_STATE/);});
test('GC03 known predecessor must already be executed in replay',async()=>{const f=history(),pred=f.op(1,hash(30)),o=f.op(1,hash(31),pred.id);f.schedule(pred);f.schedule(o);f.execute(o);f.states.set(pred.id,1n);await assert.rejects(f.run(),/TIMELOCK_PREDECESSOR_ORDER/);});
test('GC03 empty cancellation metadata still requires canonical timestamp evidence',async()=>{const f=history(),empty=f.op(0);f.cancel(empty);await assert.rejects(replayOperations(f.rows,async()=>undefined,async()=>0n),/TIMELOCK_EVENT_TIMESTAMP/);});

test('GC03 historical zero-delay cancel/reschedule can continue the same executing batch',async()=>{
 const f=history(),o=f.op(2);f.schedule(o);f.emit('CallExecuted',o,0);f.emit('Cancelled',o);
 for(let i=0;i<2;i++) {f.emit('CallScheduled',o,i);const c=o.calls[i];f.rows.at(-1).data=encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},{type:'bytes32'},{type:'uint256'}],[c.target,c.value,c.data,o.predecessor,0n]);}
 f.emit('CallExecuted',o,1);f.states.set(o.id,1n);
 const r=await f.run();assert.equal(r.operations[o.id].currentState,'Done');assert.equal(r.operations[o.id].executionCount,2);assert.equal(r.episodes[0].status,'cancelled');
 // Without the final execution event, cancellation cannot hide a partial batch.
 f.rows.pop();await assert.rejects(f.run(),/TIMELOCK_PARTIAL_EXECUTION/);
});
