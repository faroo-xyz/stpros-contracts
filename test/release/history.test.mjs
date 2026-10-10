import test from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, unlinkSync } from "node:fs";
import { keccak256, stringToHex } from "viem";
import { rpcFixture, hash, TRANSFER, WITHDRAW, CLAIM } from "./read-only-fixture.mjs";
import { address, reapprove } from "./fixture.mjs";
import { ZERO, ROLES, sha256, canonical } from "../../tasks/release/core.mjs";
import { authenticateHistory } from "../../tasks/release/history.mjs";
import { recheckObserved } from "../../tasks/release/read-only.mjs";
const run = (f, rpc = f.rpc) => recheckObserved(f.c, f.inventory, rpc, { nowSeconds: f.now });
function approve(f, value) { const raw = JSON.stringify(value); writeFileSync(f.c.historyFile, raw); f.c.review.historySha256 = sha256(raw); }
for (const [chain, start, end, streams] of [[1672,11074115,19774476,7],[688689,23532577,31190661,33]]) {
    test(`B0-scale ${chain}: authenticated historical windows, only three incremental log requests`, async () => {
        const f = rpcFixture(chain), shift = start - 1;
        f.inventory.fromBlock = start; f.inventory.toBlock = end; f.inventory.windows = [];
        for (let a = start; a <= end; a += 1000) f.inventory.windows.push({ from:a, to: Math.min(a + 999, end) });
        f.c.observed.blockNumber = end; f.pin.number = end;
        f.head.number = end + 2501; f.head.hash = hash(f.head.number);
        for (const r of Object.values(f.inventory.roles)) for (const e of r.events) e.block += shift;
        for (const s of [f.historical,f.current]) for (const l of s.logs) { const n=Number(BigInt(l.blockNumber))+shift; l.blockNumber=`0x${n.toString(16)}`;l.blockHash=hash(n); }
        reapprove(f); f.archive();
        const result = await run(f), logCalls = f.calls.filter(x => x.method === "eth_getLogs");
        assert.equal(result.history.historicalRpcLogRequests,0); assert.equal(logCalls.length,3);
        assert.ok(logCalls.every(x => BigInt(x.params[0].fromBlock)>BigInt(end) && Array.isArray(x.params[0].address)));
        assert.equal(result.currentBlock,end+2501);
        console.log(JSON.stringify({chain, historicalWindows:f.inventory.windows.length, oldPreLogRequests:2*streams*f.inventory.windows.length, oldHLogRequests:3*streams*f.inventory.windows.length,newHistoricalLogRequests:0,newIncrementalLogRequests:logCalls.length}));
    });
}
for (const [label, mutate, error] of [
    ["missing approval", (f,p) => delete f.c.review.historySha256, /HISTORY_PROOF_REQUIRED/],
    ["changed chunk", (f,p) => writeFileSync(p.chunk.path,"{}"), /REVIEWED_FILE_HASH/],
    ["missing archive", (f,p) => unlinkSync(p.chunk.path), /ENOENT/],
    ["wrong chain", (f,p) => {p.manifest.chainId=688689;approve(f,p.manifest);}, /HISTORY_BINDING/],
    ["missing target", (f,p) => {p.manifest.targets.pop();approve(f,p.manifest);}, /HISTORY_TARGETS/],
    ["missing chunk", (f,p) => {p.manifest.segments[0].chunks=[];approve(f,p.manifest);}, /HISTORY_CHUNK_GAP/],
    ["missing historical window", (f,p) => {const x={chain:"mainnet",filter_id:sha256(`[${p.manifest.targets.map(JSON.stringify).join(", ")}]`).slice(0,16),from:1,to:1000,windows:[],logs:f.historical.logs};p.manifest.segments[0].chunks=[p.write("gap.json",x)];approve(f,p.manifest);}, /HISTORY_WINDOW_GAP/],
    ["wrong inventory binding", (f,p) => {p.manifest.inventorySha256="0".repeat(64);approve(f,p.manifest);}, /HISTORY_BINDING/],
]) test(`authenticated baseline rejects ${label}`, async()=>{const f=rpcFixture(),p=f.archive();mutate(f,p);await assert.rejects(run(f),error);});
test("missing incremental window response is never treated as an empty window", async()=>{
    const f=rpcFixture(); await assert.rejects(run(f,async(m,p)=>m==="eth_getLogs"?undefined:f.rpc(m,p)),/HISTORY_ROWS_MISSING/);
});
test("omitted economic log is caught by authoritative current balance",async()=>{
    const f=rpcFixture();f.deposit();await assert.rejects(run(f,async(m,p)=>m==="eth_getLogs"?[]:f.rpc(m,p)),/HOLDER_SUM/);
});
test("new holder is discovered through Transfer without a new deposit",async()=>{
    const f=rpcFixture(),d=f.c.addresses,u=address(900),v=address(901);f.deposit();
    f.log(f.current,d.stpros,TRANSFER,[u,v],[3]);f.current.balances[d.stpros]={[u]:"7",[v]:"3"};
    assert.deepEqual((await run(f)).economic.holders[d.stpros].accounts,[{address:u,balance:"7"},{address:v,balance:"3"}]);
});
test("new withdrawal owner, partial then full claims and FIFO records reconcile",async()=>{
    const f=rpcFixture(688689),d=f.c.addresses,u=address(900),v=address(901);f.deposit();
    f.log(f.current,d.stpros,TRANSFER,[u,ZERO],[4]);f.log(f.current,d.stpros,WITHDRAW,[u,v,u],[4,4]);
    f.log(f.current,d.stpros,CLAIM,[u,v],[2]);
    Object.assign(f.current.observed.core,{supply:"6",poolT:"6",poolV:"6",queued:"4",completed:"2"});
    f.current.balances[d.stpros][u]="6";
    const r={queued:"2",pending:"2",createdAt:String(f.head.timestamp),unbondingPeriod:"0",receiver:v};
    f.current.withdrawals=[{owner:u,head:"0",tail:"1",records:[r]}];
    assert.equal((await run(f)).status,"PASS");
    f.log(f.current,d.stpros,TRANSFER,[u,ZERO],[2]);f.log(f.current,d.stpros,WITHDRAW,[u,u,u],[2,2]);
    f.log(f.current,d.stpros,CLAIM,[u,v],[2]);f.log(f.current,d.stpros,CLAIM,[u,u],[2]);
    Object.assign(f.current.observed.core,{supply:"4",poolT:"4",poolV:"4",queued:"6",completed:"6"});
    f.current.balances[d.stpros][u]="4";f.current.withdrawals=[{owner:u,head:"2",tail:"2",records:[]}];
    assert.equal((await run(f)).status,"PASS");
});
test("recorded V1 unbound claim-time receiver and full reserve payout",async()=>{
    const f=rpcFixture(),d=f.c.addresses,u=address(900),v=address(901);f.deposit();
    f.log(f.current,d.stpros,TRANSFER,[u,d.stpros],[4]);f.log(f.current,d.stpros,WITHDRAW,[u,u,u],[4,4]);
    f.current.balances[d.stpros][u]="6";
    Object.assign(f.current.observed.core,{escrow:"4",reserve:"4",queued:"4"});
    f.current.withdrawals=[{owner:u,head:"0",tail:"1",records:[{queued:"0",pending:"4",createdAt:String(f.head.timestamp),unbondingPeriod:"604800"}]}];
    // Move this actual pending queue into the independently reviewed historical anchor.
    for(const l of f.current.logs) if(Number(BigInt(l.blockNumber))===f.head.number){l.blockNumber="0x3";l.blockHash=hash(3);}
    Object.assign(f.historical,structuredClone(f.current));f.c.observed.core=structuredClone(f.current.observed.core);
    f.inventory.holders[d.stpros]={status:"PASS",mismatches:[],accounts:[{address:u,balance:"6"},{address:d.stpros,balance:"4"}],totalSupply:"10"};
    f.inventory.withdrawals={status:"PASS",accounts:structuredClone(f.historical.withdrawals),queued:"4",completed:"0",sumPending:"4"};
    // Mature snapshot predates the recent review; no invented current unbonding setting.
    for(const rows of [f.inventory.withdrawals.accounts,f.historical.withdrawals,f.current.withdrawals]) rows[0].records[0].createdAt=String(f.pin.timestamp-700000+3);
    f.archive();f.head.timestamp=f.pin.timestamp+1;f.now=f.head.timestamp+1;
    f.log(f.current,d.stpros,TRANSFER,[d.stpros,ZERO],[4]);f.log(f.current,d.stpros,CLAIM,[u,v],[4]);
    Object.assign(f.current.observed.core,{supply:"6",wrapped:"6",escrow:"0",reserve:"0",completed:"4"});f.current.withdrawals=[{owner:u,head:"1",tail:"1",records:[]}];
    assert.equal((await run(f)).status,"PASS");
});
for(const event of ["RoleGranted(bytes32,address,address)","OwnershipTransferred(address,address)","UpdateLimitsChanged(uint256,uint256)","Upgraded(address)"])
    test(`transient ${event} cannot hide behind restored state`,async()=>{
        const f=rpcFixture(688689),t=keccak256(stringToHex(event));
        if(event.startsWith("Role")) {
            f.log(f.current,f.c.addresses.timelock,t,[ROLES.admin,address(999),f.c.actors.gov],[]);
            f.log(f.current,f.c.addresses.timelock,keccak256(stringToHex("RoleRevoked(bytes32,address,address)")),[ROLES.admin,address(999),f.c.actors.gov],[]);
        } else if(event.startsWith("Ownership")) f.log(f.current,f.c.addresses.oracle,t,[f.c.actors.legacyCoreOwner,address(999)],[]);
        else if(event.startsWith("Update")) f.log(f.current,f.c.addresses.oracle,t,[],[100,3600]);
        else f.log(f.current,f.c.factories[0].beacon,t,[address(999)],[]);
        await assert.rejects(run(f),/UNREVIEWED_(ROLE_CHANGE|OWNERSHIP_HISTORY|CONFIGURATION_HISTORY|UPGRADE_HISTORY)/);
    });
test("recent event hash reorganization rejected",async()=>{
    const f=rpcFixture();f.deposit();f.current.logs.at(-1).blockHash=hash(777);await assert.rejects(run(f),/HISTORY_EVENT_REORG/);
});
test("slow pass catches up once, retains original bundle inputs and scans each delta only once",async()=>{
    const f=rpcFixture(),before=canonical(f.c),clock=Date.now;let elapsed=0,latest=0;
    Date.now=()=>1800000000000+elapsed;
    try {
        const rpc=async(m,p)=>{if(m==="eth_getBlockByNumber"&&p[0]==="latest"&&++latest===4){elapsed+=121000;f.head.number++;f.head.timestamp+=121;f.head.hash=hash(f.head.number);}return f.rpc(m,p);};
        const r=await run(f,rpc);assert.equal(r.checkpointAttempts,2);assert.equal(r.currentBlock,1002);assert.equal(canonical(f.c),before);
        assert.equal(f.calls.filter(x=>x.method==="eth_getLogs").length,2);
    } finally {Date.now=clock;}
});
test("persistent stale checkpoints remain fail-closed after bounded catch-up",async()=>{
    const f=rpcFixture(),clock=Date.now;let elapsed=0,latest=0;Date.now=()=>1800000000000+elapsed;
    try {await assert.rejects(run(f,async(m,p)=>{if(m==="eth_getBlockByNumber"&&p[0]==="latest"&&++latest>=3&&latest%2===1){elapsed+=121000;f.head.number++;f.head.timestamp+=121;f.head.hash=hash(f.head.number);}return f.rpc(m,p);}),/CHECKPOINT_SCAN_EXPIRED/);}finally{Date.now=clock;}
});
