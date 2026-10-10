// Synthetic addresses and bytecodes ONLY. Not a deployment configuration.
import { readFileSync } from "node:fs";
import { keccak256 } from "viem";
import { ROLES,ZERO,ZERO32,canonical,sha256 } from "../../tasks/release/core.mjs";
export const address=n=>`0x${n.toString(16).padStart(40,"0")}`;
export function fixture(chainId=1672) {
 const a=address;
 const assertions=JSON.parse(readFileSync(new URL("../../out/ReleaseAssertions.sol/ReleaseAssertions.json",import.meta.url)));
 const actors=Object.fromEntries(["gov","can","guardian","ops","deployer","treasury","reporter","keeper","legacyCoreOwner","legacyFactoryOwner","legacyTestVaultAdmin"].map((name,i)=>[name,a(i+1)]));
 if(chainId===1672)actors.legacyCoreOwner=actors.gov;
 const addresses=Object.fromEntries(["timelock","proxyAdmin","assertions","oracle","stpros","bridge","distributor","asset"].map((name,i)=>[name,a(i+20)]));
 const implementations=Object.fromEntries(["oracle","stpros","bridge","distributor","factory","vault"].map((name,i)=>[name,a(i+40)]));
 const artifacts={},code={};
 for(const [n,name] of ["oracle","stpros","bridge","distributor","factory","vault","timelock","assertions","proxyAdmin"].entries()) {
  const runtime=`0x60${(n+1).toString(16).padStart(2,"0")}00`;
  artifacts[name]={deployedBytecode:runtime,immutableReferences:{},abi:name==="assertions"?assertions.abi:[]};
  code[implementations[name]??addresses[name]]=runtime;
 }
 artifacts.factory.deployedBytecode=`0x7f${"00".repeat(32)}00`;
 artifacts.factory.immutableReferences={"0":[{start:1,length:32}]};
 code[implementations.factory]=`0x7f${implementations.vault.slice(2).padStart(64,"0")}00`;
 const factories=Array.from({length:chainId===1672?2:1},(_,i)=>({address:a(60+i),beacon:a(70+i),legacyOwner:chainId===1672?(i===0?actors.gov:actors.legacyFactoryOwner):actors.legacyFactoryOwner,legacyImplementation:a(80+i),legacyRuntimeHash:keccak256("0x600100"),vaults:Array.from({length:chainId===1672?1:15},(_,j)=>({address:a(100+i*20+j),admin:chainId===1672?(i===0?actors.gov:actors.legacyTestVaultAdmin):actors.legacyFactoryOwner,endState:(chainId===1672?i===0:j===14)?"GOVERNED":"RETIRED",proxyRuntimeHash:keccak256("0x600200")}))}));
 const observed={blockNumber:1000,blockHash:`0x${"ab".repeat(32)}`,code,proxies:{},factories:{},vaults:{},proxyAdminOwner:actors.legacyCoreOwner,timelock:{minDelay:chainId===1672?172800:300},core:{canonical:addresses.stpros,asset:addresses.asset,reserve:"0",queued:"0",completed:"0",escrow:"0",wrapped:"0",supply:"0",poolT:"0",poolV:"0",unbondingPeriod:chainId===1672?604800:0,maxWithdrawCount:100,reporter:actors.reporter,treasury:actors.treasury,commissionPpm:100000,maxUpdateAmount:"100000000000000000000",interval:1}};
 const legacy={};
 for(const [idx,[proxy,name]] of [[addresses.oracle,"oracle"],[addresses.stpros,"stpros"],[addresses.bridge,"bridge"],[addresses.distributor,"distributor"],...factories.map(f=>[f.address,"factory"])].entries()) {
  const fresh=chainId===1672&&["bridge","distributor"].includes(name),implementation=fresh?implementations[name]:a(150+idx);
  const version=fresh?4:name==="oracle"?(chainId===1672?1:3):name==="stpros"?(chainId===1672?1:2):1;
  if(!fresh)code[implementation]="0x600300";
  code[proxy]="0x600400";
  legacy[proxy]={implementation,version,runtimeHash:keccak256(code[implementation]),proxyRuntimeHash:keccak256(code[proxy])};
  observed.proxies[proxy]={admin:addresses.proxyAdmin,implementation,version,initializing:false,owner:fresh?addresses.timelock:name==="factory"?factories.find(f=>f.address===proxy).legacyOwner:actors.legacyCoreOwner,paused:name==="distributor"&&!fresh?null:false};
 }
 for(const f of factories){code[f.legacyImplementation]="0x600100";observed.factories[f.address]={beacon:f.beacon,beaconOwner:f.address,implementation:f.legacyImplementation,vaults:f.vaults.map(v=>v.address)};for(const v of f.vaults){code[v.address]="0x600200";observed.vaults[v.address]={round:"1",termsHash:ZERO32,phase:3,factory:f.address,asset:addresses.stpros};}}
 const roles={};
 const roleTarget=(target,keys)=>{const events=keys.map((key,i)=>({event:"RoleGranted",role:key.slice(0,66),account:key.slice(67),block:1,logIndex:i,transactionHash:ZERO32}));roles[target]={status:"PASS",mismatches:[],adminChanges:[],events,current:[...keys],ever:[...keys]};};
 roleTarget(addresses.timelock,[`${ROLES.admin}:${addresses.timelock}`,`${ROLES.proposer}:${actors.gov}`,`${ROLES.executor}:${actors.gov}`,`${ROLES.canceller}:${actors.gov}`,`${ROLES.canceller}:${actors.can}`]);
 for(const f of factories)for(const v of f.vaults)roleTarget(v.address,[`${ROLES.admin}:${v.admin}`]);
 const holders=Object.fromEntries([addresses.stpros,...factories.flatMap(f=>f.vaults.map(v=>v.address))].map(token=>[token,{status:"PASS",mismatches:[],accounts:[],totalSupply:"0"}]));
 const inventory={complete:true,startCodeAbsent:true,chainId,fromBlock:1,toBlock:1000,blockHash:observed.blockHash,windows:[{from:1,to:1000}],roles,holders,withdrawals:{status:"PASS",sumPending:"0",queued:"0",completed:"0",accounts:[]}};
 const c={mode:"simulation",releaseId:`SIMULATION-ONLY-${chainId}`,chainId,actors,addresses,implementations,factories,legacy,observed,destinations:[a(200)],policy:{minDelay:chainId===1672?172800:300,expectedGrossAprPpm:250000,maxAprPpm:500000,maxUpdateAmount:"100000000000000000000",updateInterval:3600,approvedPerformanceFeeBps:0,commissionRatePpm:100000},review:{artifactSha256:Object.fromEntries(Object.entries(artifacts).map(([k,v])=>[k,sha256(canonical(v))])),inventorySha256:sha256(canonical(inventory)),sourceCommit:"7afc95860c0544f0e68af333c1cd75b5efac40a4"}};
 return {c,inventory,artifacts};
}
export function reapprove(f) {f.c.review.inventorySha256=sha256(canonical(f.inventory));return f;}
