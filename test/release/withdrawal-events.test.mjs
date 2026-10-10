import test from "node:test";
import assert from "node:assert/strict";
import { keccak256, stringToHex } from "viem";
import { rpcFixture, hash, TRANSFER, DEPOSIT, WITHDRAW, CLAIM } from "./read-only-fixture.mjs";
import { address } from "./fixture.mjs";
import { ZERO, expectations } from "../../tasks/release/core.mjs";
import { recheckObserved, verifyCheckpoint } from "../../tasks/release/read-only.mjs";
const user = address(900), receiver = address(901);
const topic = s => keccak256(stringToHex(s));

function queued(chain = 1672, amounts = [1, 2, 3]) {
    const f = rpcFixture(chain), d = f.c.addresses, q = amounts.reduce((a,b)=>a+b,0), v1 = chain === 1672;
    const core = { supply:String(v1?q+10:10), wrapped:String(v1?q+10:0), escrow:String(v1?q:0), reserve:String(v1?q:0), queued:String(q), completed:"0", poolT:String(v1?0:10), poolV:String(v1?0:10) };
    for (const x of [f.c.observed.core, f.historical.observed.core, f.current.observed.core]) Object.assign(x,core);
    let prior = 0;
    const records = amounts.map((amount,i)=>{
        const record = {queued:String(prior),pending:String(amount),createdAt:String(f.pin.timestamp-700000+3+i),unbondingPeriod:String(v1?604800:0)};
        if (!v1) record.receiver = receiver;
        prior += amount; return record;
    });
    f.inventory.withdrawals={status:"PASS",queued:String(q),completed:"0",sumPending:String(q),accounts:[{owner:user,head:"0",tail:String(amounts.length),records}]};
    f.inventory.holders[d.stpros]={status:"PASS",mismatches:[],totalSupply:core.supply,accounts:[{address:user,balance:"10"},...(v1?[{address:d.stpros,balance:String(q)}]:[])]};
    for (const s of [f.historical,f.current]) {
        s.withdrawals=structuredClone(f.inventory.withdrawals.accounts);s.balances[d.stpros]={[user]:"10"};
        f.log(s,d.stpros,TRANSFER,[ZERO,user],[q+10],2);f.log(s,d.stpros,DEPOSIT,[user,user],[q+10,q+10],2);
        amounts.forEach((amount,i)=>{f.log(s,d.stpros,TRANSFER,[user,v1?d.stpros:ZERO],[amount],3+i);f.log(s,d.stpros,WITHDRAW,[user,receiver,user],[amount,amount],3+i);});
    }
    f.archive();return f;
}
function claim(f, amount, count, payoutReceiver = receiver) {
    const d=f.c.addresses,x=f.current.observed.core,a=f.current.withdrawals[0];
    if (f.c.chainId===1672) {
        f.log(f.current,d.stpros,TRANSFER,[d.stpros,ZERO],[amount]);
        for (const key of ["supply","wrapped","escrow","reserve"]) x[key]=String(BigInt(x[key])-BigInt(amount));
    }
    f.log(f.current,d.stpros,CLAIM,[user,payoutReceiver],[amount]);
    a.records.splice(0,count);a.head=String(BigInt(a.head)+BigInt(count));x.completed=String(BigInt(x.completed)+BigInt(amount));
}
const run = f => recheckObserved(f.c,f.inventory,f.rpc,{nowSeconds:f.now});

test("F09 V1 aggregate completion of two records preserves the remaining snapshot",async()=>{
    const f=queued();claim(f,3,2);assert.equal((await run(f)).status,"PASS");
    assert.deepEqual(f.current.withdrawals[0],{owner:user,head:"2",tail:"3",records:[{queued:"3",pending:"3",createdAt:String(f.pin.timestamp-700000+5),unbondingPeriod:"604800"}]});
    assert.equal(f.current.observed.core.queued,"6");assert.equal(f.current.observed.core.completed,"3");
    claim(f,3,1,user);assert.equal((await run(f)).status,"PASS"); // V1 claim-time receiver may change.
});
test("F09 V1 aggregate completion of three records",async()=>{const f=queued();claim(f,6,3);assert.equal((await run(f)).status,"PASS");});
test("F09 V1 later unmatured records remain untouched",async()=>{
    const f=queued();for(const rows of [f.inventory.withdrawals.accounts,f.historical.withdrawals,f.current.withdrawals])rows[0].records[2].unbondingPeriod="999999";
    f.archive();claim(f,3,2);assert.equal((await run(f)).status,"PASS");
    claim(f,3,1);await assert.rejects(run(f),/CLAIM_UNBONDING/);
});
for(const [label,mutate,error] of [
    ["zero event payout",f=>f.current.logs.at(-1).data=hash(0),/CLAIM_AMOUNT/],
    ["beyond all pending",f=>f.current.logs.at(-1).data=hash(7),/CLAIM_AMOUNT/],
    ["fraction of fully funded V1 record",f=>f.current.logs.at(-1).data=hash(2),/CLAIM_AMOUNT/],
    ["wrong owner",f=>f.current.logs.at(-1).topics[1]=hash(999),/CLAIM_WITHOUT_RECORD/],
    ["zero receiver",f=>f.current.logs.at(-1).topics[2]=hash(0),/CLAIM_RECEIVER/],
    ["skip immature head",f=>{for(const rows of [f.inventory.withdrawals.accounts,f.historical.withdrawals])rows[0].records[0].unbondingPeriod="999999";f.archive();},/CLAIM_UNBONDING/],
])test(`F09 aggregate rejects ${label}`,async()=>{const f=queued();claim(f,3,2);mutate(f);await assert.rejects(run(f),error);});
test("F09 V3 remains per record and receiver-bound",async()=>{
    const f=queued(688689);claim(f,1,1);claim(f,2,1);assert.equal((await run(f)).status,"PASS");
    f.current.logs.at(-1).topics[2]=hash(999);await assert.rejects(run(f),/CLAIM_RECEIVER/);
});
test("F09 V3 rejects aggregation across records",async()=>{const f=queued(688689);claim(f,3,2);await assert.rejects(run(f),/CLAIM_AMOUNT/);});

function upgrade(f) {
    f.operationHistory(true); f.now = f.head.timestamp + 1;
    const e=expectations(f.c,f.inventory,f.artifacts,"R4"),d=f.c.addresses;
    f.current.observed.proxyAdminOwner=d.timelock;
    for(const p of e.proxies) {
        Object.assign(f.current.observed.proxies[p.proxy],{owner:p.owner,implementation:p.implementation,version:4});
        if(p.implementation!==f.c.legacy[p.proxy].implementation)f.log(f.current,p.proxy,topic("Upgraded(address)"),[p.implementation],[]);
    }
    for(const factory of f.c.factories){f.current.observed.factories[factory.address].implementation=f.c.implementations.vault;f.log(f.current,factory.beacon,topic("Upgraded(address)"),[f.c.implementations.vault],[]);}
    for(const r of e.roles){const old=f.inventory.roles[r.target].current.includes(`${r.role}:${r.account}`);if(old!==r.expected)f.log(f.current,r.target,topic(`${r.expected?"RoleGranted":"RoleRevoked"}(bytes32,address,address)`),[r.role,r.account,d.timelock],[]);}
    if(f.c.chainId===1672){const x=f.current.observed.core;f.log(f.current,d.stpros,TRANSFER,[d.stpros,ZERO],[x.escrow]);x.supply=String(BigInt(x.supply)-BigInt(x.escrow));Object.assign(x,{escrow:"0",reserve:"0",wrapped:"0",poolT:x.supply,poolV:x.supply});}
}
for(const chain of [1672,688689])test(`F09 ${chain} claims immediately before/after authenticated upgrade use different event models`,async()=>{
    const f=queued(chain,[1,2,3,4]);
    if(chain===1672)claim(f,3,2);else{claim(f,1,1);claim(f,2,1);}
    upgrade(f);
    for(const amount of [3,4])f.log(f.current,f.c.addresses.stpros,CLAIM,[user,receiver],[amount]);
    f.current.withdrawals[0]={owner:user,head:"4",tail:"4",records:[]};f.current.observed.core.completed="10";
    const check=()=>verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"R4",{nowSeconds:f.now});
    assert.equal((await check()).status,"PASS");
    f.current.logs.at(-2).data=hash(7);f.current.logs.pop();await assert.rejects(check(),/CLAIM_AMOUNT/);
});

test("F09 an unreviewed implementation cannot switch the event model",async()=>{
    const f=queued();claim(f,3,2);upgrade(f);
    const row=f.current.logs.find(l=>l.address===f.c.addresses.stpros&&l.topics[0]===topic("Upgraded(address)"));row.topics[1]=hash(999);
    await assert.rejects(verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"R4",{nowSeconds:f.now}),/UNREVIEWED_UPGRADE_HISTORY/);
});
