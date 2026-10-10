import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { keccak256, stringToHex } from "viem";
import { rpcFixture, hash, TRANSFER, DEPOSIT, WITHDRAW, CLAIM } from "./read-only-fixture.mjs";
import { address, reapprove } from "./fixture.mjs";
import { preflight, validateRecordedTopology, buildU, buildHandoff, ROLES, ZERO, canonical } from "../../tasks/release/core.mjs";
import { recheckObserved, verifyCheckpoint } from "../../tasks/release/read-only.mjs";
const run = f => { f.archive(); return recheckObserved(f.c, f.inventory, f.rpc, { nowSeconds: f.now }); };
for (const chain of [1672, 688689])
    for (const newer of [false, true])
        test(`complete release preflight ${chain}, newer head=${newer}, reaches every Safe`, async () => {
            const f = rpcFixture(chain);
            if (!newer)
                Object.assign(f.head, f.pin);
            preflight(f.c, f.inventory, f.artifacts);
            const result = await run(f);
            assert.equal(result.status, "PASS");
            for (const safe of Object.values(f.c.review.safes))
                assert.ok(f.calls.some(x => x.method === "eth_call" && x.params[0].to === safe.address));
            assert.equal(result.reviewedBlock, 1000);
            assert.equal(result.currentBlock, f.head.number);
        });
for (const [label, mutate, code] of [
    ["owners", f => f.current.safes.gov.owners[0] = address(999), /SAFE_OWNERS/],
    ["threshold", f => f.current.safes.gov.threshold = 1, /SAFE_THRESHOLD/],
    ["nonce", f => f.current.safes.gov.nonce = 1, /SAFE_NONCE/],
    ["module", f => f.current.safes.gov.modules = [address(999)], /SAFE_MODULES/],
    ["guard", f => f.current.safes.gov.guard = address(999), /SAFE_GUARD/],
    ["singleton", f => f.current.safes.gov.singleton = address(999), /SAFE_SINGLETON/],
    ["review hash", f => f.pin.hash = hash(999), /HISTORY_BINDING/],
    ["expired review", f => { f.now += 3601; f.head.timestamp = f.now; }, /REVIEW_EXPIRED/],
    ["stale provider", f => f.now += 121, /STALE_CURRENT_HEAD/],
    ["owner", f => f.current.observed.proxies[f.c.addresses.oracle].owner = address(999), /OWNER_DRIFT/],
    ["implementation", f => f.current.observed.proxies[f.c.addresses.oracle].implementation = address(999), /PROXY_STORAGE_DRIFT/],
    ["beacon", f => f.current.observed.factories[f.c.factories[0].address].implementation = address(999), /BEACON_IMPLEMENTATION/],
    ["factory registry", f => f.current.observed.factories[f.c.factories[0].address].vaults.push(address(999)), /VAULT_DISCOVERY_DRIFT/],
    ["missing historical data", f => f.setMissing(), /HISTORY_REORG/],
    ["RPC error", f => f.setRpcFailure(), /SIMULATED_RPC_FAILURE/],
])
    test(`full RPC preflight rejects ${label}`, async () => { const f = rpcFixture(); mutate(f); await assert.rejects(run(f), code); });
test("canonical pin is checked again after all RPC reads", async () => {
    const f = rpcFixture();
    const rpc = async (method, params) => { if (method === "eth_getBlockByNumber" && params[0] === "0x3e8" && f.calls.some(x => x.method === "eth_call" && x.params.at(-1) === "0x3e9"))
        f.setReorg(); return f.rpc(method, params); };
    await assert.rejects(recheckObserved(f.c, f.inventory, rpc, { nowSeconds: f.now }), /HISTORY_REORG/);
});
test("new privileged holder after review cannot hide behind the old inventory", async () => {
    const f = rpcFixture(), target = f.c.addresses.timelock;
    f.log(f.current, target, keccak256(stringToHex("RoleGranted(bytes32,address,address)")), [ROLES.admin, address(999), f.c.actors.gov], []);
    await assert.rejects(run(f), /UNREVIEWED_ROLE_CHANGE/);
});
for (const field of ["reporter", "treasury", "commissionPpm", "maxUpdateAmount", "interval"])
    test(`testnet critical Oracle ${field} drift rejected`, async () => {
        const f = rpcFixture(688689);
        f.current.observed.core[field] = ["reporter", "treasury"].includes(field) ? address(999) : "99999";
        await assert.rejects(run(f), /ORACLE_CONFIG_DRIFT/);
    });
for (const field of ["oracle", "bridge", "distributor", "canonical"])
    test(`legacy ${field} wiring cannot be substituted`, async () => {
        const f = rpcFixture(688689);
        f.current.wiring[field] = address(999);
        await assert.rejects(run(f), /ORACLE_POINTER|LEGACY_CORE_WIRING|CANONICAL/);
    });
for (const chain of [1672, 688689])
    test(`permitted ${chain} deposit is reconciled without regenerating approved calldata`, async () => {
        const f = rpcFixture(chain), before = buildU(f.c, f.inventory, f.artifacts).operation;
        f.deposit();
        assert.equal((await run(f)).status, "PASS");
        assert.equal(canonical(buildU(f.c, f.inventory, f.artifacts).operation), canonical(before));
    });
test("unreconciled pool change rejected despite coherent share balances", async () => {
    const f = rpcFixture(688689);
    f.deposit();
    f.current.observed.core.poolT = "11";
    await assert.rejects(run(f), /UNRECONCILED_POOL/);
});
test("unreconciled queue counters rejected even if Q-C and empty record sum still match", async () => {
    const f = rpcFixture();
    f.current.observed.core.queued = "1";
    f.current.observed.core.completed = "1";
    await assert.rejects(run(f), /UNRECONCILED_QUEUE_TOTALS/);
});
function queuedV3() {
    const f = rpcFixture(688689), d = f.c.addresses, user = address(900);
    for (const x of [f.c.observed.core, f.historical.observed.core, f.current.observed.core])
        Object.assign(x, { supply: "6", poolT: "6", poolV: "6", wrapped: "0", escrow: "0", reserve: "0", queued: "4", completed: "0" });
    const row = { owner: user, head: "0", tail: "1", records: [{ queued: "0", pending: "4", createdAt: String(f.pin.timestamp - 700000 + 3), unbondingPeriod: "604800", receiver: user }] };
    f.inventory.withdrawals = { status: "PASS", accounts: [row], queued: "4", completed: "0", sumPending: "4" };
    f.inventory.holders[d.stpros] = { status: "PASS", mismatches: [], totalSupply: "6", accounts: [{ address: user, balance: "6" }] };
    for (const s of [f.historical, f.current]) {
        s.withdrawals = structuredClone([row]);
        s.balances[d.stpros] = { [user]: "6" };
        f.log(s, d.stpros, TRANSFER, [ZERO, user], [10], 2);
        f.log(s, d.stpros, DEPOSIT, [user, user], [10, 10], 2);
        f.log(s, d.stpros, TRANSFER, [user, ZERO], [4], 3);
        f.log(s, d.stpros, WITHDRAW, [user, user, user], [4, 4], 3);
    }
    reapprove(f);
    f.log(f.current, d.stpros, CLAIM, [user, user], [2]);
    f.current.observed.core.completed = "2";
    Object.assign(f.current.withdrawals[0].records[0], { pending: "2", queued: "2" });
    return f;
}
test("recorded V3 equivalent partial claim advances per-record queued but preserves global queued", async () => {
    const f = queuedV3();
    assert.equal((await run(f)).status, "PASS");
    assert.equal(f.current.observed.core.queued, "4");
    f.log(f.current, f.c.addresses.stpros, CLAIM, [address(900), address(900)], [2]);
    Object.assign(f.current.withdrawals[0], { head: "1", records: [] });
    f.current.observed.core.completed = "4";
    assert.equal((await run(f)).status, "PASS");
});
for (const [field, value] of [["queued", "0"], ["pending", "1"], ["unbondingPeriod", "0"], ["receiver", address(901)]])
    test(`partial claim rejects incorrect ${field}`, async () => {
        const f = queuedV3(); f.current.withdrawals[0].records[0][field] = value;
        await assert.rejects(run(f), /UNRECONCILED_WITHDRAWAL_RECORDS|QUEUE_SUM/);
    });
for (const [label, mutate, error] of [
    ["bound receiver", f => f.current.logs.at(-1).topics[2] = hash(901), /CLAIM_RECEIVER/],
    ["oversized payout", f => f.current.logs.at(-1).data = hash(5), /CLAIM_AMOUNT/],
    ["zero payout", f => f.current.logs.at(-1).data = hash(0), /CLAIM_AMOUNT/],
    ["unmatured snapshot", f => { for (const rows of [f.inventory.withdrawals.accounts, f.historical.withdrawals, f.current.withdrawals]) rows[0].records[0].unbondingPeriod = "999999"; reapprove(f); }, /CLAIM_UNBONDING/],
]) test(`claim replay rejects ${label}`, async () => { const f = queuedV3(); mutate(f); await assert.rejects(run(f), error); });
function recordedFixture(chain) {
    const f = rpcFixture(chain), registry = JSON.parse(readFileSync(new URL("../../tools/release/registry.json", import.meta.url))), r = registry[chain === 1672 ? "mainnet" : "testnet"];
    const byName = Object.fromEntries(Object.entries(r.expected_proxies).map(([p, v]) => [v.name, p]));
    Object.assign(f.c.addresses, { oracle: byName.Oracle, stpros: r.stpros, proxyAdmin: r.proxy_admin, asset: r.wpros });
    if (chain === 688689)
        Object.assign(f.c.addresses, { bridge: byName.BridgeVault, distributor: byName.SlpDistributor });
    f.c.legacy = Object.fromEntries(Object.entries(r.expected_proxies).map(([p, v]) => [p, { implementation: v.implementation, version: v.version }]));
    f.c.factories = r.factories.map(p => { const [beacon, b] = Object.entries(r.expected_beacons).find(([, v]) => v.owner === p); return { address: p, beacon, legacyImplementation: b.implementation, vaults: Array.from({ length: r.expected_vault_count[p] }, (_, i) => ({ address: address(1000 + i) })) }; });
    if (chain === 1672)
        for (const field of ["bridge", "distributor"]) {
            const v = f.c.review.freshDeployments[field];
            Object.assign(v, { proxyAdmin: r.proxy_admin });
            f.c.legacy[v.proxy] = { implementation: v.implementation, version: 4 };
        }
    return { ...f, registry };
}
for (const chain of [1672, 688689])
    test(`complete recorded target binding ${chain}`, () => { const f = recordedFixture(chain); validateRecordedTopology(f.c, f.registry); });
for (const chain of [1672, 688689])
    for (const field of ["oracle", "stpros", "proxyAdmin", "bridge", "distributor"])
        test(`coherent ${chain} ${field} substitution rejected`, () => {
            const f = recordedFixture(chain);
            f.c.addresses[field] = address(9999);
            if (field !== "proxyAdmin")
                f.c.legacy[address(9999)] = { implementation: address(9998), version: 1 };
            assert.throws(() => validateRecordedTopology(f.c, f.registry), /RECORDED_|REVIEWED_FRESH_/);
        });
for (const chain of [1672, 688689])
    for (const what of ["factory", "beacon", "vaults"])
        test(`${chain} ${what} binding cannot drift`, () => {
            const f = recordedFixture(chain), x = f.c.factories.at(-1);
            if (what === "factory")
                x.address = address(999);
            if (what === "beacon")
                x.beacon = address(999);
            if (what === "vaults")
                x.vaults.pop();
            assert.throws(() => validateRecordedTopology(f.c, f.registry), /RECORDED_/);
        });
async function handoffFixture(chain = 1672) {
    const f = rpcFixture(chain);
    const { expectations } = await import("../../tasks/release/core.mjs"), e = expectations(f.c, f.inventory, f.artifacts, "H");
    f.head.timestamp = f.pin.timestamp + f.c.policy.minDelay;
    f.now = f.head.timestamp + 1;
    f.current.observed.proxyAdminOwner = f.c.addresses.timelock;
    for (const p of e.proxies)
        Object.assign(f.current.observed.proxies[p.proxy], { owner: p.owner, implementation: p.implementation, version: p.guarded ? 4 : f.c.legacy[p.proxy].version });
    for (const r of e.roles) {
        const old = f.inventory.roles[r.target].current.includes(`${r.role}:${r.account}`);
        if (old !== r.expected)
            f.log(f.current, r.target, keccak256(stringToHex(`${r.expected ? "RoleGranted" : "RoleRevoked"}(bytes32,address,address)`)), [r.role, r.account, f.c.actors.gov], []);
    }
    f.current.safes.gov.nonce = 1;
    f.evidence = { operationId: buildU(f.c, f.inventory, f.artifacts).operation.id, forkReference: "SIMULATED-FRESH-FORK", freshFork: "PASS", fs6: "PASS", blockNumber: f.head.number, blockHash: f.head.hash, uGas: 1500000, nonces: { gov: 1, can: 0 } };
    return f;
}
test("execution checkpoint retains old canonical bundle across 48h and validates fresh signing nonce", async () => {
    const f = await handoffFixture(), before = canonical(buildU(f.c, f.inventory, f.artifacts).operation);
    const result = await verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence });
    assert.equal(result.status, "PASS");
    assert.equal(canonical(buildU(f.c, f.inventory, f.artifacts).operation), before);
    f.evidence.nonces.gov = 0;
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence }), /EXECUTION_NONCE/);
});
test("execution checkpoint requires fresh independently reviewed fork evidence", async () => {
    const f = await handoffFixture();
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now }), /EXECUTION_EVIDENCE_REQUIRED/);
    f.evidence.blockNumber = f.pin.number;
    f.evidence.blockHash = f.pin.hash;
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence }), /EXECUTION_EVIDENCE_STALE/);
});
test("testnet execution rechecks Oracle configuration rather than relying only on assertHandoff", async () => {
    const f = await handoffFixture(688689);
    f.current.observed.core.interval = "9999";
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence }), /ORACLE_CONFIG_DRIFT/);
});
test("transient upgrade cannot hide behind restored current slot", async () => {
    const f = rpcFixture();
    f.log(f.current, f.c.addresses.oracle, keccak256(stringToHex("Upgraded(address)")), [address(999)], []);
    await assert.rejects(run(f), /UNREVIEWED_UPGRADE_HISTORY/);
});
test("Beacon code drift rejected even when getter responses remain plausible", async () => {
    const f = rpcFixture();
    f.current.observed.code[f.c.factories[0].beacon] = "0x600501";
    await assert.rejects(run(f), /PHYSICAL_CODE_DRIFT/);
});
test("execution allows reconciled deposits during mandatory delay with original operation ID", async () => {
    const f = await handoffFixture(), id = f.evidence.operationId;
    f.deposit();
    const result = await verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence });
    assert.equal(result.operationId, id);
});
async function r4Fixture(chain = 1672) {
    const f = await handoffFixture(chain);
    const { expectations } = await import("../../tasks/release/core.mjs"), e = expectations(f.c, f.inventory, f.artifacts, "R4");
    for (const p of e.proxies) {
        Object.assign(f.current.observed.proxies[p.proxy], { owner: p.owner, implementation: p.implementation, version: 4 });
        if (p.implementation !== f.c.legacy[p.proxy].implementation)
            f.log(f.current, p.proxy, keccak256(stringToHex("Upgraded(address)")), [p.implementation], []);
    }
    for (const factory of f.c.factories) {
        f.current.observed.factories[factory.address].implementation = f.c.implementations.vault;
        f.log(f.current, factory.beacon, keccak256(stringToHex("Upgraded(address)")), [f.c.implementations.vault], []);
    }
    const vault = f.c.factories.flatMap(x => x.vaults).find(v => v.endState === "GOVERNED").address;
    f.log(f.current, vault, keccak256(stringToHex("RoleGranted(bytes32,address,address)")), [ROLES.operator, f.c.actors.ops, f.c.addresses.timelock], []);
    f.current.safes.gov.nonce = 2;
    return f;
}
test("postcheck verifies current version/history and accepts already consumed governance nonce", async () => {
    const f = await r4Fixture();
    const result = await verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "R4", { nowSeconds: f.now });
    assert.equal(result.status, "PASS");
    f.current.observed.proxies[f.c.addresses.oracle].version = 3;
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, f.rpc, "R4", { nowSeconds: f.now }), /INITIALIZER_VERSION/);
});
test("approved testnet T-3 incomplete recovery verifies actual old owners and retains atomic U",async()=>{
 const f=await handoffFixture(688689);f.c.testnetRecovery={incompleteT3:true,ownerApproval:"SIMULATED-EXISTING-RECOVERY"};
 for(const key of ["oracle","stpros","bridge","distributor"])f.current.observed.proxies[f.c.addresses[key]].owner=f.c.actors.legacyCoreOwner;
 const {testnetHandoff}=await import("../../tasks/release/core.mjs"),plan=testnetHandoff(f.c,f.inventory,f.artifacts);assert.equal(plan.stages.find(s=>s.stage==="T-3").transactions.length,0);
 f.evidence.operationId=buildU(f.c,f.inventory,f.artifacts).operation.id;
 assert.equal((await verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"H",{nowSeconds:f.now,executionEvidence:f.evidence})).status,"PASS");
 f.current.observed.proxies[f.c.addresses.oracle].owner=address(9999);await assert.rejects(verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"H",{nowSeconds:f.now,executionEvidence:f.evidence}),/OWNER_DRIFT/);
});
test("local gas transport refuses public RPC even in simulation mode",async()=>{
 const {readOnlyRpc}=await import("../../tasks/release/read-only.mjs");assert.throws(()=>readOnlyRpc("https://rpc.pharos.xyz",true,true),/LOCAL_RPC_REQUIRED/);
 const rpc=readOnlyRpc("http://127.0.0.1:8545",true,true);await assert.rejects(rpc("eth_sendRawTransaction",[]),/RPC_METHOD_REFUSED/);
});
for (const [chain, field] of [[1672, "oracle"], [688689, "oracle"], [688689, "bridge"], [688689, "distributor"]]) {
    test(`all other preflight checks pass for coherent ${chain} ${field} substitution, registry still rejects`, () => {
        const f = rpcFixture(chain), old = f.c.addresses[field], replacement = address(9999), historicalEntry = structuredClone(f.c.legacy[old]);
        function replace(value) {
            if (typeof value === "string") return value === old ? replacement : value;
            if (Array.isArray(value)) return value.map(replace);
            if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k === old ? replacement : k, replace(v)]));
            return value;
        }
        f.c = replace(f.c); f.inventory = replace(f.inventory);
        // Preserve the unused correct historical entry: precisely the old validator's bypass.
        f.c.legacy[old] = historicalEntry; reapprove(f);
        assert.equal(preflight({ ...f.c, mode: "simulation" }, f.inventory, f.artifacts).status, "PASS");
        assert.throws(() => preflight(f.c, f.inventory, f.artifacts), /RECORDED_TARGET/);
    });
}
test("transport rejects incomplete or mismatched JSON-RPC responses", async () => {
    const { readOnlyRpc } = await import("../../tasks/release/read-only.mjs"), original = globalThis.fetch;
    try {
        globalThis.fetch = async () => ({ ok: true, json: async () => ({ jsonrpc: "2.0", id: 1 }) });
        await assert.rejects(readOnlyRpc("http://127.0.0.1:8545", true)("eth_chainId", []), /RPC_MISSING_RESULT/);
        globalThis.fetch = async () => ({ ok: true, json: async () => ({ jsonrpc: "2.0", id: 9, result: "0x688" }) });
        await assert.rejects(readOnlyRpc("http://127.0.0.1:8545", true)("eth_chainId", []), /RPC_RESPONSE_ID/);
    } finally { globalThis.fetch = original; }
});
test("missing view-assertion response is not a successful checkpoint", async () => {
    const f = await handoffFixture(), { decodeFunctionData } = await import("viem");
    const rpc = async (method, params) => {
        if (method === "eth_call" && params[0].to === f.c.addresses.assertions && decodeFunctionData({ abi: f.artifacts.assertions.abi, data: params[0].data }).functionName.startsWith("assert")) return undefined;
        return f.rpc(method, params);
    };
    await assert.rejects(verifyCheckpoint(f.c, f.inventory, f.artifacts, rpc, "H", { nowSeconds: f.now, executionEvidence: f.evidence }), /ASSERTION_RESULT_MISSING/);
});

test("post-R4 same-owner request is unbound, partial payout to another receiver reconciles", async () => {
    const f = await r4Fixture(688689), d = f.c.addresses, u = address(900), v = address(901);
    f.deposit();
    f.log(f.current,d.stpros,TRANSFER,[u,ZERO],[4]);
    f.log(f.current,d.stpros,WITHDRAW,[u,u,u],[4,4]);
    f.log(f.current,d.stpros,CLAIM,[u,v],[2]);
    Object.assign(f.current.observed.core,{supply:"6",poolT:"6",poolV:"6",queued:"4",completed:"2"});
    f.current.balances[d.stpros][u]="6";
    f.current.withdrawals=[{owner:u,head:"0",tail:"1",records:[{queued:"2",pending:"2",createdAt:String(f.head.timestamp),unbondingPeriod:"0",receiver:ZERO}]}];
    assert.equal((await verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"R4",{nowSeconds:f.now})).status,"PASS");
    f.current.withdrawals[0].records[0].queued="0";
    await assert.rejects(verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"R4",{nowSeconds:f.now}),/UNRECONCILED_WITHDRAWAL_RECORDS/);
});
test("legitimate reporter pool increase remains reconciled",async()=>{
    const f=rpcFixture(688689);f.deposit();
    f.log(f.current,f.c.addresses.oracle,keccak256(stringToHex("TokenAmountUpdated(address,uint256,uint256)")),[f.c.addresses.asset],[2,12]);
    f.current.observed.core.poolT="12";assert.equal((await run(f)).status,"PASS");
});
test("read-only transport batches independent views and binds unordered responses to IDs",async()=>{
    const {readOnlyRpc}=await import("../../tasks/release/read-only.mjs"),previous=globalThis.fetch;let calls=0;
    try {
        globalThis.fetch=async(url,options)=>{calls++;const requests=JSON.parse(options.body);assert.equal(requests.length,3);return {ok:true,json:async()=>requests.map(r=>({jsonrpc:"2.0",id:r.id,result:r.params[0]})).reverse()};};
        const rpc=readOnlyRpc("http://127.0.0.1:8545",true);
        assert.deepEqual(await Promise.all([1,2,3].map(n=>rpc("eth_getCode",[String(n),"0x1"]))),["1","2","3"]);assert.equal(calls,1);
    } finally {globalThis.fetch=previous;}
});
test("read-only transport rejects a missing batch slot",async()=>{
    const {readOnlyRpc}=await import("../../tasks/release/read-only.mjs"),previous=globalThis.fetch;
    try {globalThis.fetch=async()=>({ok:true,json:async()=>[{jsonrpc:"2.0",id:1,result:"0x"}]});const rpc=readOnlyRpc("http://127.0.0.1:8545",true);const results=await Promise.allSettled([rpc("eth_getCode",["a","0x1"]),rpc("eth_getCode",["b","0x1"])]);assert.ok(results.every(r=>r.status==="rejected"&&r.reason.message==="RPC_BATCH_INCOMPLETE"));}finally{globalThis.fetch=previous;}
});

test("execution rejects a former reviewed admin regrant followed by revoke",async()=>{
    const f=await handoffFixture(),target=f.c.factories[0].vaults[0].address;
    for(const event of ["RoleGranted","RoleRevoked"])f.log(f.current,target,keccak256(stringToHex(`${event}(bytes32,address,address)`)),[ROLES.admin,f.c.actors.gov,f.c.addresses.timelock],[]);
    await assert.rejects(verifyCheckpoint(f.c,f.inventory,f.artifacts,f.rpc,"H",{nowSeconds:f.now,executionEvidence:f.evidence}),/UNREVIEWED_ROLE_CHANGE/);
});
