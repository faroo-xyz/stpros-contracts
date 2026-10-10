import test from "node:test";
import assert from "node:assert/strict";
import { decodeFunctionData,parseAbi,keccak256,stringToHex,encodeAbiParameters } from "viem";
import { fixture,reapprove,address } from "./fixture.mjs";
import { preflight,validateConfig,buildU,buildHandoff,p1Runbook,testnetHandoff,verifyOperation,verifyRuntime,expectations,ROLES,ZERO32,SLOTS } from "../../tasks/release/core.mjs";
import { readOnlyRpc,scanRoles,scanEconomics } from "../../tasks/release/read-only.mjs";
const upgrade=parseAbi(["function upgradeAndCall(address,address,bytes) payable"]);
const fCall=tx=>decodeFunctionData({abi:upgrade,data:tx.data});
test("mainnet H/U frozen order, full second beacon retirement and Safe format",()=>{
 const {c,inventory,artifacts}=fixture(),u=buildU(c,inventory,artifacts),h=buildHandoff(c,inventory,artifacts);
 assert.equal(preflight(c,inventory,artifacts).status,"PASS");assert.equal(u.transactions.length,10);
 assert.deepEqual(u.transactions.map(t=>t.to),[c.addresses.proxyAdmin,c.addresses.proxyAdmin,c.addresses.proxyAdmin,c.factories[0].address,c.factories[0].vaults[0].address,c.addresses.proxyAdmin,c.factories[1].address,c.factories[1].address,c.factories[1].address,c.addresses.assertions]);
 assert.equal(fCall(u.transactions[0]).args[0],c.addresses.oracle);assert.equal(fCall(u.transactions[1]).args[0],c.addresses.stpros);
 assert.equal(u.operation.predecessor,ZERO32);verifyOperation(u.operation,u.transactions,c.releaseId);
 assert.equal(h.version,"1.0");assert.equal(h.simulationOnly,true);assert.equal(h.deployable,false);assert.equal(h.transactions.at(-1).to,c.addresses.assertions);
 const assertion=decodeFunctionData({abi:artifacts.assertions.abi,data:h.transactions.at(-1).data});assert.equal(assertion.functionName,"assertHandoff");assert.equal(assertion.args[1],u.operation.id);
});
for(const field of ["id","salt","predecessor","targets","values","payloads"])test(`reject mutated operation ${field}`,()=>{
 const {c,inventory,artifacts}=fixture(),u=buildU(c,inventory,artifacts),op=structuredClone(u.operation);
 if(Array.isArray(op[field]))op[field][0]=field==="targets"?address(999):field==="values"?1n:"0x";else op[field]=`0x${"11".repeat(32)}`;
 assert.throws(()=>verifyOperation(op,u.transactions,c.releaseId),/OPERATION_MISMATCH/);
});
for(const k of ["B","C","D"])test(`owner-approved fallback ${k}, no duplicate Factory migration`,()=>{
 const f=fixture();f.c.p1Case=k;assert.throws(()=>buildU(f.c,f.inventory,f.artifacts),/FALLBACK_OWNER_APPROVAL/);
 f.c.fallbackApproval={ownerApproved:true,reference:"LOCAL-ONLY-APPROVAL"};const u=buildU(f.c,f.inventory,f.artifacts),h=buildHandoff(f.c,f.inventory,f.artifacts),p=p1Runbook(f.c,f.inventory,f.artifacts);
 assert.equal(u.transactions.length,k==="C"?10:9);assert.equal(p.case,k);
 if(k!=="C") assert.equal(fCall(h.transactions[0]).args[0].toLowerCase(),f.c.factories[1].address);
 const e=expectations(f.c,f.inventory,f.artifacts,"R4");const row=e.roles.find(r=>r.target===f.c.factories[1].vaults[0].address&&r.account===f.c.factories[1].vaults[0].admin);assert.equal(row?.expected,k==="B"?false:undefined);
});
test("testnet T0 through T5, canonical V3 token, 15-vault reconciliation",()=>{
 const f=fixture(688689),u=buildU(f.c,f.inventory,f.artifacts),h=testnetHandoff(f.c,f.inventory,f.artifacts);
 assert.deepEqual(h.stages.map(s=>s.stage),["T-0","T-1","T-2","T-3","T-4","T-5","U_t"]);assert.equal(u.transactions.length,22);
 const oracle=fCall(u.transactions[0]);const d=decodeFunctionData({abi:parseAbi(["function migrateFromV3(address,uint256,address,address)"]),data:oracle.args[2]});assert.equal(d.args[0].toLowerCase(),f.c.addresses.asset);
 const e=expectations(f.c,f.inventory,f.artifacts,"R4");assert.equal(e.vaults.filter(v=>v.retired).length,14);assert.equal(e.vaults[14].governed,true);
});
test("testnet E2-unavailable approved recovery retires all, retains inert inventoried roles",()=>{
 const f=fixture(688689);f.c.testnetRecovery={e2Unavailable:true,ownerApproval:"LOCAL-ONLY"};for(const v of f.c.factories[0].vaults)v.endState="RETIRED";
 const h=testnetHandoff(f.c,f.inventory,f.artifacts);assert.equal(h.stages[1].transactions.length,0);assert.equal(h.stages[2].transactions.length,0);
 assert.equal(expectations(f.c,f.inventory,f.artifacts,"R4").vaults.filter(v=>v.retired).length,15);
});
for(const [label,change,reason] of [
 ["missing release gates",f=>f.c.mode="release",/RELEASE_GATES_INCOMPLETE/],
 ["wrong delay",f=>f.c.policy.minDelay=1,/POLICY_DELAY/],
 ["wrong APR",f=>f.c.policy.maxAprPpm=500001,/APR_POLICY/],
 ["missing inventory",f=>delete f.inventory.roles[f.c.factories[0].vaults[0].address],/INVENTORY_HASH/],
 ["truncated role list",f=>{f.inventory.roles[f.c.factories[0].vaults[0].address].ever=[];reapprove(f)},/ROLE_TRUNCATION/],
 ["stale TL privilege",f=>{const r=f.inventory.roles[f.c.addresses.timelock],key=`${ROLES.admin}:${address(999)}`;r.current.push(key);r.ever.push(key);r.events.push({event:"RoleGranted",role:ROLES.admin,account:address(999),block:2});reapprove(f)},/TIMELOCK_ROLES/],
 ["wrong PA",f=>f.c.observed.proxies[f.c.addresses.oracle].admin=address(999),/PROXY_ADMIN/],
 ["wrong version",f=>f.c.observed.proxies[f.c.addresses.oracle].version=2,/UNAPPROVED_INITIALIZER_VERSION/],
 ["wrong beacon owner",f=>f.c.observed.factories[f.c.factories[0].address].beaconOwner=address(999),/BEACON_IDENTITY/],
 ["wrong immutable",f=>f.c.observed.code[f.c.implementations.factory]=`0x7f${address(999).slice(2).padStart(64,"0")}00`,/RUNTIME_CODEHASH/],
 ["implementation proxy substitution",f=>f.c.observed.code[f.c.implementations.vault]="0x600400",/RUNTIME_CODEHASH/],
 ["no code",f=>f.c.observed.code[f.c.implementations.vault]="0x",/NO_CODE/],
 ["unapproved artifact",f=>f.artifacts.vault.deployedBytecode="0x1234",/ARTIFACT_REVIEW_HASH/],
 ["inventory gap",f=>{f.inventory.windows[0].from=2;reapprove(f)},/INVENTORY_GAP/],
 ["unexpected owner",f=>f.c.observed.proxies[f.c.addresses.oracle].owner=address(999),/UNEXPECTED_OWNER/],
 ["paused",f=>f.c.observed.proxies[f.c.addresses.oracle].paused=true,/PAUSED_OR_UNKNOWN/],
 ["split request",f=>f.c.split=true,/NO_DEFAULT_SPLIT/],
])test(`preflight refuses ${label}`,()=>{const f=fixture();change(f);assert.throws(()=>preflight(f.c,f.inventory,f.artifacts),reason);});
test("immutable width and missing binding fail closed",()=>{const f=fixture();assert.throws(()=>verifyRuntime(f.c.observed.code[f.c.implementations.factory],f.artifacts.factory),/BINDING_REQUIRED/);f.artifacts.factory.immutableReferences[0][0].length=20;assert.throws(()=>verifyRuntime("0x01",f.artifacts.factory,f.c.implementations.vault),/IMMUTABLE_WIDTH/);});
test("EIP1967 slots independently derived",()=>{for(const name of ["admin","implementation"])assert.equal(SLOTS[name],`0x${(BigInt(keccak256(stringToHex(`eip1967.proxy.${name}`)))-1n).toString(16)}`);});
test("read-only transport refuses mutating methods before fetching",async()=>{const rpc=readOnlyRpc("http://127.0.0.1:8545",true);await assert.rejects(rpc("eth_sendTransaction",[]),/RPC_METHOD_REFUSED/);assert.throws(()=>readOnlyRpc("https://example.com"),/RPC_NOT_ALLOWLISTED/);await assert.rejects(rpc("eth_getLogs",[{fromBlock:"0x0",toBlock:"0x3e8"}]),/LOG_RANGE/);});
test("full role scan detects omitted privileged holder independently of supplied list",async()=>{
 const f=fixture();const target=f.c.addresses.timelock,account=address(999),grant=keccak256(stringToHex("RoleGranted(bytes32,address,address)"));
 const inv={fromBlock:1,roles:{[target]:{}}};const rpc=async(method)=>method==="eth_getCode"?"0x":method==="eth_getLogs"?[{address:target,topics:[grant,ROLES.admin,`0x${account.slice(2).padStart(64,"0")}`],data:"0x",blockNumber:"0x1",logIndex:"0x0",transactionHash:ZERO32}]:encodeAbiParameters([{type:"bool"}],[true]);
 const result=await scanRoles(f.c,inv,rpc,"0x1");assert.deepEqual(result[target].current,[`${ROLES.admin}:${account}`]);
});
test("holder scan rejects an omitted zero-balance historical holder",async()=>{
 const f=fixture(),token=f.c.addresses.stpros,holder=address(999),zero=`0x${"00".repeat(32)}`,transfer=keccak256(stringToHex("Transfer(address,address,uint256)"));
 const transferLogs=[{address:token,topics:[transfer,zero,`0x${holder.slice(2).padStart(64,"0")}`],data:`0x${"0".repeat(63)}1`,blockNumber:"0x1",logIndex:"0x0"},{address:token,topics:[transfer,`0x${holder.slice(2).padStart(64,"0")}`,zero],data:`0x${"0".repeat(63)}1`,blockNumber:"0x1",logIndex:"0x1"}];
 const rpc=async(method,params)=>method==="eth_getCode"?"0x":method==="eth_getLogs"?(params[0].topics[0][0]===transfer?transferLogs:[]):encodeAbiParameters([{type:"uint256"}],[0n]);
 const inv={fromBlock:1,holders:{[token]:{accounts:[]}}};await assert.rejects(scanEconomics(f.c,inv,rpc,"0x1"),/HOLDER_TRUNCATION/);
});
test("fresh and malformed reviewed artifact production settings",async()=>{
 const {freezeArtifact}=await import("../../tasks/release/artifacts.mjs");
 assert.throws(()=>freezeArtifact({solcVersion:"0.8.28",input:{settings:{optimizer:{enabled:false,runs:200},evmVersion:"cancun"}}},{},"x","X"),/PRODUCTION_BUILD_SETTINGS/);
 const result=freezeArtifact({solcVersion:"0.8.28",input:{settings:{optimizer:{enabled:true,runs:200},evmVersion:"cancun"}}},{contracts:{x:{X:{abi:[],evm:{deployedBytecode:{object:"600000"}}}}}},"x","X");
 assert.equal(result.artifact.deployedBytecode,"0x600000");assert.match(result.reviewStatus,/UNREVIEWED/);
});
test("recorded target binding refuses an arbitrary canonical target even with coherent config",async()=>{
 const {validateRecordedTopology}=await import("../../tasks/release/core.mjs");const f=fixture();f.c.mode="release";
 const registry={mainnet:{proxy_admin:f.c.addresses.proxyAdmin,stpros:f.c.addresses.stpros,wpros:f.c.addresses.asset,factories:f.c.factories.map(f=>f.address),expected_proxies:Object.fromEntries(Object.entries(f.c.legacy).filter(([p])=>![f.c.addresses.bridge,f.c.addresses.distributor].includes(p)).map(([p,s])=>[p,{name:p===f.c.addresses.oracle?"Oracle":p===f.c.addresses.stpros?"StPROS":"YieldVaultFactory",implementation:s.implementation,version:s.version}])),expected_beacons:Object.fromEntries(f.c.factories.map(f=>[f.beacon,{owner:f.address,implementation:f.legacyImplementation}])),expected_vault_count:Object.fromEntries(f.c.factories.map(f=>[f.address,f.vaults.length]))}};
 f.c.review.freshDeployments=Object.fromEntries(["bridge","distributor"].map(k=>[k,{chainId:1672,proxy:f.c.addresses[k],implementation:f.c.implementations[k],proxyAdmin:f.c.addresses.proxyAdmin,owner:f.c.addresses.timelock,guardian:f.c.actors.guardian,version:4,approvalReference:"SIMULATION"}]));
 validateRecordedTopology(f.c,registry);f.c.addresses.asset=address(999);assert.throws(()=>validateRecordedTopology(f.c,registry),/RECORDED_CORE_TARGETS/);
});
test("approved C residual role churn cannot veto terminal retirement calldata",()=>{
 const f=fixture();f.c.p1Case="C";f.c.fallbackApproval={ownerApproved:true,reference:"LOCAL-ONLY"};const before=buildU(f.c,f.inventory,f.artifacts).operation.id;
 const target=f.c.factories[1].vaults[0].address,r=f.inventory.roles[target],account=address(999),key=`${ROLES.operator}:${account}`;
 r.ever.push(key);r.current.push(key);r.events.push({event:"RoleGranted",role:ROLES.operator,account,block:2,logIndex:0,transactionHash:ZERO32});reapprove(f);
 assert.equal(buildU(f.c,f.inventory,f.artifacts).operation.id,before);
});
