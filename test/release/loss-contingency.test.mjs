import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { encodeAbiParameters, encodeFunctionData, decodeFunctionData, parseAbi, keccak256, stringToHex } from 'viem';
import { buildLossContingency, observeLossContingency, LOSS_ABI, ROUND_FIELDS } from '../../tasks/release/loss-contingency.mjs';
import { ZERO, ZERO32, SLOTS, ROLES } from '../../tasks/release/core.mjs';
const a = n => `0x${n.toString(16).padStart(40,'0')}`;
const h = n => `0x${n.toString(16).padStart(64,'0')}`;
const now = 2_000_000n;
function fixture() {
  const p={schema:'faroo-loss-contingency-v1',mode:'simulation',chainId:31337,
    vault:a(1),factory:a(2),timelock:a(3),asset:a(4),beacon:a(5),factoryImplementation:a(6),vaultImplementation:a(7),proxyAdmin:a(8),
    guardian:a(9),replacementGuardian:a(10),gov:a(11),can:a(12),operator:a(13),counterparty:a(14),feeRecipient:a(15),
    reviewReference:'LOCAL TEST ONLY',marginReviewReference:'LOCAL 1 hour review',reviewedAt:String(now-1n),expiresAt:String(now+1800n),marginSeconds:'3600',
    predecessor:ZERO32,saltA:h(20),saltB:h(21),approvedLossAmount:String(50n*10n**18n),minDelay:'172800',
    round:{roundIndex:'1',openedAt:'1727200',openWindow:'86400',openDeadline:'1813600',lockDuration:'86400',settleTimelockWindow:'3600',roundCap:'1000000000000000000000000',perAddressCap:'0',minSubscription:'1',performanceFeeBps:'0',maxLossBps:'5000'},runtimeHashes:{}};
  const identities=Object.fromEntries(['vault','factory','timelock','asset','beacon','factoryImplementation','vaultImplementation','proxyAdmin'].map(n=>[n,p[n]]));
  for(const n of Object.keys(identities))p.runtimeHashes[n]=keccak256('0x600000');
  const s={chainId:p.chainId,blockNumber:'100',blockHash:h(100),timestamp:String(now),identities,runtimeHashes:{...p.runtimeHashes},
    vaultFactory:p.factory,isFactoryVault:true,allProxies:[p.vault],factoryOwner:p.timelock,factoryTimelock:p.timelock,pendingOwner:ZERO,beaconOwner:p.factory,proxyAdminOwner:p.timelock,
    guardian:p.guardian,counterparty:p.counterparty,feeRecipient:p.feeRecipient,authoritiesValid:true,round:{...p.round},phase:1,retired:false,paused:false,minDelay:p.minDelay,
    grace:'2592000',settlementDeadline:'4492000',totalManagedAssets:String(100n*10n**18n),vaultBalance:String(100n*10n**18n)};
  return {p,s};
}
const build=({p,s})=>buildLossContingency(p,s,now);
test('A single and B ordered atomic batch have exact independent OZ operation identities',()=>{
  const {p,s}=fixture(),r=buildLossContingency(p,s,now),{A,B}=r.paths;
  assert.equal(r.status,'SIMULATION ONLY');assert.equal(r.deployable,false);
  assert.deepEqual(B.targets,[p.factory,p.factory,p.vault]);assert.deepEqual(B.values,['0','0','0']);
  assert.deepEqual(B.calls.map(c=>decodeFunctionData({abi:LOSS_ABI,data:c.data}).functionName),['setGuardian','unpause','proposeSettlement']);
  assert.deepEqual(decodeFunctionData({abi:LOSS_ABI,data:A.calls[0].data}).args,[1,BigInt(p.approvedLossAmount),ZERO]);
  assert.equal(A.id,keccak256(encodeAbiParameters([{type:'address'},{type:'uint256'},{type:'bytes'},{type:'bytes32'},{type:'bytes32'}],[p.vault,0n,A.payloads[0],ZERO32,p.saltA])));
  assert.equal(B.id,keccak256(encodeAbiParameters([{type:'address[]'},{type:'uint256[]'},{type:'bytes[]'},{type:'bytes32'},{type:'bytes32'}],[B.targets,[0n,0n,0n],B.payloads,ZERO32,p.saltB])));
  for(const [name,path] of Object.entries(r.paths)) {
    assert.equal(decodeFunctionData({abi:LOSS_ABI,data:path.schedule.data}).functionName,name==='A'?'schedule':'scheduleBatch');
    assert.equal(decodeFunctionData({abi:LOSS_ABI,data:path.schedule.data}).args.at(-1),172800n);
    assert.equal(decodeFunctionData({abi:LOSS_ABI,data:path.execute.data}).functionName,name==='A'?'execute':'executeBatch');
    assert.deepEqual(decodeFunctionData({abi:LOSS_ABI,data:path.cancel.data}).args,[path.id]);
  }
  assert.equal(r.timing.D,s.settlementDeadline);assert.equal(r.timing.proposalCutoff,'4488400');
  assert.equal(r.timing.latestScheduleAt,'4312000');assert.match(r.cancellation.join(' '),/CAN or GOV/);
});
test('existing LOSS calldata bytes remain compatible',()=>{
  const f=fixture(),call=build(f).paths.A.calls[0];
  const legacyAbi=parseAbi(['function proposeSettlement(uint8 mode,uint256 amount,address fundFrom)']);
  assert.equal(call.data,encodeFunctionData({abi:legacyAbi,functionName:'proposeSettlement',args:[1,BigInt(f.p.approvedLossAmount),ZERO]}));
  // The existing task is deliberately untouched; its actual action is checked by CLI smoke evidence.
  const old=readFileSync('tasks/yieldVault.ts','utf8');assert.match(old,/task\("yield-vault:loss-calldata"/);
});
test('both states produce both paths; pause state does not alter approved IDs',()=>{
  const f=fixture(),first=build(f);f.s.paused=true;const second=build(f);
  assert.deepEqual(second.paths,first.paths);assert.equal(second.factoryPaused,true);
});
test('exact scheduling margin boundary accepts; next second is blocked',()=>{
  const f=fixture();f.s.timestamp='4312000';f.p.reviewedAt='4311999';f.p.expiresAt='4313000';
  buildLossContingency(f.p,f.s,4312000n);
  assert.throws(()=>buildLossContingency(f.p,f.s,4312001n),/SCHEDULING_WINDOW_CLOSED/);
});
for(const [name,mutate,err] of [
  ['wrong chain',f=>f.s.chainId=1672,/OBSERVATION_CHAIN/],
  ['wrong Factory',f=>f.s.vaultFactory=a(99),/CANONICAL_FACTORY/],
  ['unregistered Vault',f=>f.s.isFactoryVault=false,/CANONICAL_FACTORY/],
  ['omitted Vault',f=>f.s.allProxies=[],/CANONICAL_FACTORY/],
  ['wrong Vault identity',f=>f.s.identities.vault=a(99),/IDENTITY_vault/],
  ['wrong implementation',f=>f.s.identities.vaultImplementation=a(99),/IDENTITY_vaultImplementation/],
  ['wrong runtime',f=>f.s.runtimeHashes.factory=h(99),/IDENTITY_factory/],
  ['changed owner',f=>f.s.factoryOwner=a(99),/GOVERNANCE_WIRING/],
  ['changed Timelock pointer',f=>f.s.factoryTimelock=a(99),/GOVERNANCE_WIRING/],
  ['changed guardian',f=>f.s.guardian=a(99),/ACTOR_DRIFT/],
  ['pending ownership',f=>f.s.pendingOwner=a(99),/GOVERNANCE_WIRING/],
  ['invalid replacement',f=>f.p.replacementGuardian=f.p.guardian,/REPLACEMENT_GUARDIAN/],
  ['replacement aliases GOV',f=>f.p.replacementGuardian=f.p.gov,/REPLACEMENT_GUARDIAN/],
  ['missing margin approval',f=>delete f.p.marginReviewReference,/INDEPENDENT_REVIEW/],
  ['zero margin',f=>f.p.marginSeconds='0',/REVIEWED_MARGIN/],
  ['stale approval',f=>f.p.expiresAt=String(now-1n),/STALE_REVIEW/],
  ['stale head',f=>f.s.timestamp=String(now-121n),/STALE_OBSERVATION/],
  ['future head',f=>f.s.timestamp=String(now+31n),/STALE_OBSERVATION/],
  ['wrong round',f=>f.s.round.roundIndex='2',/ROUND_DRIFT/],
  ['changed round terms',f=>f.s.round.maxLossBps='4999',/ROUND_DRIFT/],
  ['existing proposal',f=>f.s.phase=2,/UNSUPPORTED_PHASE/],
  ['settled',f=>f.s.phase=3,/UNSUPPORTED_PHASE/],
  ['retired',f=>f.s.retired=true,/UNSUPPORTED_PHASE/],
  ['unknown pause',f=>delete f.s.paused,/PAUSE_UNKNOWN/],
  ['changed delay',f=>f.s.minDelay='172801',/TIMELOCK_DELAY/],
  ['incorrect D',f=>f.s.settlementDeadline='4492001',/DEADLINE_GETTER/],
  ['over loss cap',f=>f.p.approvedLossAmount=String(50n*10n**18n+1n),/LOSS_CAP/],
  ['missing backing',f=>f.s.vaultBalance='0',/VAULT_BACKING/],
  ['bad roles',f=>f.s.authoritiesValid=false,/AUTHORITY_GETTERS/],
  ['nonzero predecessor',f=>f.p.predecessor=h(1),/INDEPENDENT_OPERATION/],
  ['identical salts',f=>f.p.saltB=f.p.saltA,/INDEPENDENT_OPERATION/],
  ['negative amount',f=>f.p.approvedLossAmount='-1',/UINT_loss/],
  ['unsafe numeric amount',f=>f.p.approvedLossAmount=1e30,/UINT_loss/],
])test(`reject ${name}`,()=>{const f=fixture();mutate(f);assert.throws(()=>build(f),err);});
test('frozen IDs never silently change with amount, guardian, salt or target',()=>{
  for(const mutate of [f=>f.p.approvedLossAmount='1',f=>f.p.replacementGuardian=a(55),f=>f.p.saltA=h(88),f=>{f.p.vault=a(55);f.s.identities.vault=a(55);f.s.allProxies=[a(55)];}]) {
    const f=fixture(),r=build(f);f.p.frozenOperationIds={A:r.paths.A.id,B:r.paths.B.id};build(f);mutate(f);
    assert.throws(()=>build(f),/FROZEN_OPERATION_ID_MISMATCH/);
  }
});

// Deterministic RPC adapter exercises pinned reads and rejection boundaries only.
// The security execution tests use actual contracts in LossContingency.t.sol.
function rpcFixture() {
  const f=fixture(),{p,s}=f,calls=[];
  const nums=[...ROUND_FIELDS,'SETTLEMENT_GRACE','settlementDeadline','totalManagedAssets','getMinDelay'];
  const addrs=['factory','asset','owner','pendingOwner','guardian','timelock','beacon','implementation','counterparty','feeRecipient'];
  const abi=parseAbi([...nums.map(n=>`function ${n}() view returns(uint256)`),...addrs.map(n=>`function ${n}() view returns(address)`),
    'function phase() view returns(uint8)','function paused() view returns(bool)','function retired() view returns(bool)',
    'function isFactoryVault(address) view returns(bool)','function getAllProxies() view returns(address[])',
    'function balanceOf(address) view returns(uint256)','function getTimestamp(bytes32) view returns(uint256)','function hasRole(bytes32,address) view returns(bool)']);
  const header={number:'0x64',hash:s.blockHash,timestamp:`0x${now.toString(16)}`};
  f.override=()=>undefined;
  f.rpc=async (method,params)=>{
    calls.push([method,params]);const overridden=f.override(method,params);if(overridden!==undefined)return overridden;
    if(method==='eth_chainId')return `0x${p.chainId.toString(16)}`;
    if(method==='eth_getBlockByNumber')return header;
    assert.equal(params.at(-1),'0x64');
    if(method==='eth_getCode')return '0x600000';
    if(method==='eth_getStorageAt')return `0x${(params[1]===SLOTS.implementation?p.factoryImplementation:params[1]===SLOTS.admin?p.proxyAdmin:p.beacon).slice(2).padStart(64,'0')}`;
    assert.equal(method,'eth_call');const {functionName:n,args}=decodeFunctionData({abi,data:params[0].data});const to=params[0].to;
    let type,value;
    if(nums.includes(n)){type='uint256';value=BigInt(p.round[n]??({SETTLEMENT_GRACE:s.grace,settlementDeadline:s.settlementDeadline,totalManagedAssets:s.totalManagedAssets,getMinDelay:s.minDelay}[n]));}
    else if(addrs.includes(n)){type='address';value=({factory:s.vaultFactory,asset:p.asset,owner:to===p.beacon?p.factory:p.timelock,pendingOwner:s.pendingOwner,guardian:s.guardian,timelock:p.timelock,beacon:p.beacon,implementation:p.vaultImplementation,counterparty:p.counterparty,feeRecipient:p.feeRecipient})[n];}
    else if(n==='phase'){type='uint8';value=s.phase;}
    else if(n==='getAllProxies'){type='address[]';value=s.allProxies;}
    else if(['getTimestamp','balanceOf'].includes(n)){type='uint256';value=n==='getTimestamp'?0n:BigInt(s.vaultBalance);}
    else {type='bool';value=n==='hasRole'? !(args[1]===ZERO || (args[1].toLowerCase()===p.can && args[0]!==ROLES.canceller)):s[n];}
    return encodeAbiParameters([{type}],[value]);
  };
  f.calls=calls;return f;
}
test('fresh reader uses only allowlisted read methods and pinned state',async()=>{
  const f=rpcFixture(),r=await observeLossContingency(f.p,f.rpc,{},()=>now);
  assert.equal(r.paths.A.id,build(f).paths.A.id);
  assert.deepEqual([...new Set(f.calls.map(c=>c[0]))].sort(),['eth_call','eth_chainId','eth_getBlockByNumber','eth_getCode','eth_getStorageAt']);
});
for(const [name,override,err] of [
  ['RPC chain',m=>m==='eth_chainId'?'0x1':undefined,/RPC_CHAIN/],
  ['missing code',m=>m==='eth_getCode'?'0x':undefined,/NO_CODE/],
  ['wrong slot',m=>m==='eth_getStorageAt'?h(99):undefined,/IDENTITY_/],
  ['reorg',(m,p)=>m==='eth_getBlockByNumber'&&p[0]==='0x64'?{hash:h(999)}:undefined,/REORG/],
  ['pending operation',(m,p)=>m==='eth_call'&&p[0].data.startsWith(keccak256(stringToHex('getTimestamp(bytes32)')).slice(0,10))?h(1):undefined,/ALREADY_SCHEDULED/],
])test(`reader rejects ${name}`,async()=>{const f=rpcFixture();f.override=override;await assert.rejects(observeLossContingency(f.p,f.rpc,{},()=>now),err);});
test('synthetic topology cannot become release output',async()=>{
  const f=rpcFixture();f.p.mode='release';f.p.chainId=1672;
  const registry=JSON.parse(readFileSync('tools/release/registry.json','utf8'));
  await assert.rejects(observeLossContingency(f.p,f.rpc,registry,()=>now),/RECORDED_CANONICAL_TOPOLOGY/);
  assert.equal(f.calls.length,0);
});

test('release reader requires pinned OZ Timelock runtime even for an otherwise canonical plan',async()=>{
  const f=rpcFixture();f.p.mode='release';f.p.chainId=1672;
  const r={mainnet:{chain_id:1672,factories:[f.p.factory],stpros:f.p.asset,proxy_admin:f.p.proxyAdmin,expected_beacons:{[f.p.beacon]:{owner:f.p.factory}}}};
  await assert.rejects(observeLossContingency(f.p,f.rpc,r,()=>now),/PINNED_TIMELOCK_RUNTIME_REQUIRED/);
  assert.equal(f.calls.length,0);
});
test('reader checks freshness again after all reads',async()=>{
  const f=rpcFixture();let clockReads=0;
  await assert.rejects(observeLossContingency(f.p,f.rpc,{},()=>++clockReads>2?now+121n:now),/STALE_OBSERVATION/);
});
test('OPS may equal reviewed GRD without weakening the separate Guardian fixture',()=>{
  const f=fixture();f.p.operator=f.p.guardian;build(f);
});
