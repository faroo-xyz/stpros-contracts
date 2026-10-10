import { residualReads, termNames, termsHash } from "../../tasks/release/residual-replay.mjs";
import { buildU } from "../../tasks/release/core.mjs";
// Deterministic, in-memory RPC only. Synthetic identities are never deployment inputs.
import { readFileSync, mkdtempSync, writeFileSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sha256 } from "../../tasks/release/core.mjs";
import { historyTargets } from "../../tasks/release/history.mjs";
const archiveRoots = [];
process.on("exit", () => { for (const root of archiveRoots) rmSync(root, { recursive: true, force: true }); });
import { parseAbi, decodeFunctionData, encodeFunctionResult, encodeAbiParameters, keccak256, stringToHex } from "viem";
import { fixture, address, reapprove } from "./fixture.mjs";
import { ZERO, ZERO32, SLOTS, ROLES } from "../../tasks/release/core.mjs";
export const hash = n => `0x${BigInt(n).toString(16).padStart(64, "0")}`;
const topic = s => keccak256(stringToHex(s));
const word = a => `0x${a.slice(2).padStart(64, "0")}`;
export const TRANSFER = topic("Transfer(address,address,uint256)"), DEPOSIT = topic("Deposit(address,address,uint256,uint256)"), WITHDRAW = topic("Withdraw(address,address,address,uint256,uint256)"), CLAIM = topic("WithdrawalCompleted(address,address,uint256)");
const abi = parseAbi([
    "function owner() view returns(address)", "function guardian() view returns(address)", "function pendingOwner() view returns(address)", "function getMinDelay() view returns(uint256)", "function hasRole(bytes32,address) view returns(bool)", "function getAllProxies() view returns(address[])", "function beacon() view returns(address)", "function implementation() view returns(address)", "function balanceOf(address) view returns(uint256)", "function totalSupply() view returns(uint256)", "function paused() view returns(bool)", "function queuedWithdrawal() view returns(uint256)", "function completedWithdrawal() view returns(uint256)", "function withdrawalHead(address) view returns(uint256)", "function withdrawalTail(address) view returns(uint256)", "function maxRedeem(address) view returns(uint256)", "function getOwners() view returns(address[])", "function getThreshold() view returns(uint256)", "function nonce() view returns(uint256)", "function getModulesPaginated(address,uint256) view returns(address[],address)", "function getStorageAt(uint256,uint256) view returns(bytes)", "function oracle() view returns(address)", "function weth() view returns(address)", "function vTokenAddresses(address) view returns(bool)", "function keeper() view returns(address)", "function getSlps() view returns(address[])", "function roundTermsHash(address) view returns(bytes32)", "function tokenToVToken(address) view returns(address)", "function asset() view returns(address)", "function poolInfo(address) view returns(uint256,uint256)", "function totalCanWithdrawAmount() view returns(uint256)", "function unbondingPeriod() view returns(uint256)", "function maxWithdrawCount() view returns(uint256)", "function slp() view returns(address)", "function bridgeVault() view returns(address)", "function commissionAccount() view returns(address)", "function commissionRatePpm() view returns(uint256)", "function maxUpdateAmount() view returns(uint256)", "function updateInterval() view returns(uint256)", "function phase() view returns(uint8)", "function roundIndex() view returns(uint256)", "function factory() view returns(address)", "function isFactoryVault(address) view returns(bool)", "function totalManagedAssets() view returns(uint256)", "function getTimestamp(bytes32) view returns(uint256)", "function isOperationDone(bytes32) view returns(bool)"
]);
export function rpcFixture(chainId = 1672) {
    const f = fixture(chainId);
    // Known historical addresses come from the reviewed registry; all RPC responses remain synthetic.
    const registry = JSON.parse(readFileSync(new URL("../../tools/release/registry.json", import.meta.url))), r = registry[chainId === 1672 ? "mainnet" : "testnet"], mapping = new Map();
    const byName = Object.fromEntries(Object.entries(r.expected_proxies).map(([p, e]) => [e.name, p]));
    for (const [key, value] of Object.entries({ oracle: byName.Oracle, stpros: r.stpros, proxyAdmin: r.proxy_admin, asset: r.wpros, ...(chainId === 688689 ? { bridge: byName.BridgeVault, distributor: byName.SlpDistributor } : {}) }))
        mapping.set(f.c.addresses[key], value);
    f.c.factories.forEach((factory, i) => { mapping.set(factory.address, r.factories[i]); const [beacon, entry] = Object.entries(r.expected_beacons).find(([, e]) => e.owner === r.factories[i]); mapping.set(factory.beacon, beacon); mapping.set(factory.legacyImplementation, entry.implementation); });
    for (const [proxy, p] of Object.entries(f.c.legacy)) {
        const recorded = r.expected_proxies[mapping.get(proxy)];
        if (recorded)
            mapping.set(p.implementation, recorded.implementation);
    }
    function remap(value) { if (typeof value === "string")
        return mapping.get(value) ?? value; if (Array.isArray(value))
        return value.map(remap); if (value && typeof value === "object")
        return Object.fromEntries(Object.entries(value).map(([k, v]) => [mapping.get(k) ?? k, remap(v)])); return value; }
    f.c = remap(f.c);
    f.inventory = remap(f.inventory);
    reapprove(f);
    const { c, inventory } = f;
    const timestamp = 1800000000;
    c.observed.timestamp = timestamp;
    c.mode = "release";
    c.review.approvalReference = "SIMULATED-INDEPENDENT-REVIEW";
    c.releaseGates = { independentReview: "CLEAR", freshFork: "PASS", fs6: "PASS", forkReference: "SIMULATED", forkBlockHash: c.observed.blockHash, forkTimestamp: timestamp, hGas: 1000000, uGas: 1500000, counterpartyConsent: true, dappReady: true };
    c.review.safes = {};
    for (const [i, role] of ["gov", "can", "guardian", "ops", "treasury"].entries()) {
        const singleton = address(300), code = "0x600900", safe = { address: c.actors[role], runtimeHash: keccak256(code), singleton, singletonHash: keccak256(code), owners: Array.from({ length: role === "gov" ? 5 : 3 }, (_, j) => address(400 + i * 10 + j)), threshold: role === "gov" || role === "can" ? 3 : 2, nonce: 0, modules: [], guard: ZERO };
        c.review.safes[role] = safe;
        c.observed.code[safe.address] = code;
        c.observed.code[singleton] = code;
    }
    c.review.freshDeployments = Object.fromEntries(["bridge", "distributor"].map(field => [field, { chainId: 1672, proxy: c.addresses[field], implementation: c.implementations[field], proxyAdmin: c.addresses.proxyAdmin, owner: c.addresses.timelock, guardian: c.actors.guardian, version: 4, approvalReference: "SIMULATED-FRESH" }]));
    for (const factory of c.factories)
        c.observed.code[factory.beacon] = "0x600500";
    c.observed.code[c.addresses.asset] = "0x600600";
    for (const v of Object.values(c.observed.vaults)) { v.terms = Array(12).fill("0"); v.termsHash = termsHash(v.terms); }
    const pin = { number: 1000, hash: c.observed.blockHash, timestamp }, head = { number: 1001, hash: hash(1001), timestamp: timestamp + 1 };
    const raw = [];
    for (const [target, r] of Object.entries(inventory.roles))
        for (const ev of r.events) {
            ev.logIndex = raw.length;
            raw.push({ address: target, topics: [topic("RoleGranted(bytes32,address,address)"), ev.role, word(ev.account), word(c.actors.gov)], data: "0x", blockNumber: "0x1", blockHash: hash(1), logIndex: `0x${ev.logIndex.toString(16)}`, transactionHash: ev.transactionHash });
        }
    reapprove(f);
    function state() { return { observed: structuredClone(c.observed), safes: structuredClone(c.review.safes), balances: {}, withdrawals: structuredClone(inventory.withdrawals.accounts), logs: structuredClone(raw), tma: {}, wiring: {}, operationTimestamps: {}, transactions: {} }; }
    const historical = state(), current = state(), calls = [];
    let reorg = false, missingHistory = false, rpcFailure = false;
    const allAbi = [...abi, ...residualReads, ...f.artifacts.assertions.abi, ...parseAbi([chainId === 1672 ? "function getWithdrawals(address) view returns((uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod)[])" : "function getWithdrawals(address) view returns((uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod,address receiver)[])"])];
    function header(n) { if (n === pin.number)
        return pin; if (n === head.number)
        return head; return { number: n, hash: hash(n), timestamp: n > pin.number ? timestamp + n - pin.number : timestamp - 700000 + n }; }
    async function rpc(method, params) {
        calls.push({ method, params });
        if (rpcFailure)
            throw new Error("SIMULATED_RPC_FAILURE");
        if (method === "eth_chainId")
            return `0x${chainId.toString(16)}`;
        if (method === "eth_getBlockByNumber") {
            const n = params[0] === "latest" ? head.number : Number(BigInt(params[0]));
            if (missingHistory && n === pin.number)
                return null;
            const h = header(n);
            return { ...h, hash: reorg && n === 1000 ? hash(9999) : h.hash, number: `0x${n.toString(16)}`, timestamp: `0x${h.timestamp.toString(16)}` };
        }
        if (method === "eth_getTransactionByHash") return current.transactions[params[0]];
        const tag = method === "eth_getLogs" ? params[0].toBlock : params.at(-1), n = Number(BigInt(tag)), s = n <= pin.number ? historical : current, o = s.observed, d = c.addresses;
        if (method === "eth_getCode")
            return n < inventory.fromBlock ? "0x" : o.code[params[0]] ?? (c.destinations.includes(params[0]) ? "0x6000" : "0x");
        if (method === "eth_getLogs") {
            const q = params[0], topics = q.topics?.[0], addresses = Array.isArray(q.address) ? q.address : [q.address];
            return s.logs.filter(l => addresses.includes(l.address) && BigInt(l.blockNumber) >= BigInt(q.fromBlock) && BigInt(l.blockNumber) <= BigInt(q.toBlock) && (!topics || topics.includes(l.topics[0])));
        }
        if (method === "eth_getStorageAt") {
            const [target, slot] = params, p = o.proxies[target], safe = Object.values(s.safes).find(x => x.address === target);
            if (slot === "0x0" && safe)
                return word(safe.singleton);
            if (slot === SLOTS.admin)
                return word(p.admin);
            if (slot === SLOTS.implementation)
                return word(p.implementation);
            if (slot === SLOTS.initialized)
                return hash(p.version);
            throw new Error(`unexpected slot ${slot}`);
        }
        if (method !== "eth_call")
            throw new Error(`forbidden mock method ${method}`);
        const target = params[0].to, { functionName: name, args = [] } = decodeFunctionData({ abi: allAbi, data: params[0].data });
        const a = args.map(v => typeof v === "string" ? v.toLowerCase() : v), x = o.core, proxy = o.proxies[target], factory = c.factories.find(f => f.address === target), beacon = c.factories.find(f => f.beacon === target), vault = o.vaults[target], safe = Object.values(s.safes).find(v => v.address === target);
        let result;
        if (termNames.includes(name)) return encodeFunctionResult({ abi: allAbi, functionName: name, result: termNames.indexOf(name) >= 10 ? address(Number(BigInt(vault.terms[termNames.indexOf(name)]))) : BigInt(vault.terms[termNames.indexOf(name)]) });
        if (["settleMode", "settleAmount", "profitFunded"].includes(name)) return encodeFunctionResult({ abi: allAbi, functionName: name, result: name === "profitFunded" ? (vault.funded ?? false) : BigInt(vault[name] ?? 0) });
        if (name.startsWith("assert"))
            return "0x"; // Solidity execution is independently covered by local atomic tests.
        switch (name) {
            case "owner":
                result = proxy?.owner ?? (target === d.proxyAdmin ? o.proxyAdminOwner : beacon ? s.observed.factories[beacon.address].beaconOwner : ZERO);
                break;
            case "guardian":
                result = c.actors.guardian;
                break;
            case "pendingOwner":
                result = ZERO;
                break;
            case "paused":
                result = proxy.paused;
                break;
            case "getMinDelay":
                result = BigInt(o.timelock.minDelay);
                break;
            case "getAllProxies":
                result = o.factories[target].vaults;
                break;
            case "beacon":
                result = o.factories[target].beacon;
                break;
            case "implementation":
                result = o.factories[beacon.address].implementation;
                break;
            case "roundTermsHash":
                result = o.vaults[a[0]].termsHash;
                break;
            case "roundIndex":
                result = BigInt(vault.round);
                break;
            case "phase":
                result = vault.phase;
                break;
            case "factory":
                result = vault.factory;
                break;
            case "asset":
                result = vault ? vault.asset : x.asset;
                break;
            case "isFactoryVault":
                result = factory.vaults.some(v => v.address === a[0]);
                break;
            case "totalManagedAssets":
                result = BigInt(s.tma[target] ?? 0);
                break;
            case "oracle":
                result = s.wiring.oracle ?? d.oracle;
                break;
            case "weth":
                result = s.wiring.weth ?? d.asset;
                break;
            case "tokenToVToken":
                result = s.wiring.canonical ?? d.stpros;
                break;
            case "vTokenAddresses":
                result = true;
                break;
            case "bridgeVault":
                result = s.wiring.bridge ?? d.bridge;
                break;
            case "slp":
                result = target === d.stpros ? (s.wiring.distributor ?? d.distributor) : x.reporter;
                break;
            case "keeper":
                result = c.actors.keeper;
                break;
            case "getSlps":
                result = c.destinations;
                break;
            case "commissionAccount":
                result = x.treasury;
                break;
            case "commissionRatePpm":
                result = BigInt(x.commissionPpm);
                break;
            case "maxUpdateAmount":
                result = BigInt(x.maxUpdateAmount);
                break;
            case "updateInterval":
                result = BigInt(x.interval);
                break;
            case "totalSupply":
                result = target === d.stpros ? BigInt(x.supply) : 0n;
                break;
            case "balanceOf":
                result = target === d.asset && a[0] === d.stpros ? BigInt(x.wrapped) : target === d.stpros && a[0] === d.stpros ? BigInt(x.escrow) : BigInt(s.balances[target]?.[a[0]] ?? 0);
                break;
            case "queuedWithdrawal":
                result = BigInt(x.queued);
                break;
            case "completedWithdrawal":
                result = BigInt(x.completed);
                break;
            case "totalCanWithdrawAmount":
                result = BigInt(x.reserve);
                break;
            case "unbondingPeriod":
                result = BigInt(x.unbondingPeriod);
                break;
            case "maxWithdrawCount":
                result = BigInt(x.maxWithdrawCount);
                break;
            case "poolInfo":
                result = [BigInt(x.poolT), BigInt(x.poolV)];
                break;
            case "withdrawalHead":
                result = BigInt(s.withdrawals.find(r => r.owner === a[0])?.head ?? 0);
                break;
            case "withdrawalTail":
                result = BigInt(s.withdrawals.find(r => r.owner === a[0])?.tail ?? 0);
                break;
            case "getWithdrawals":
                result = (s.withdrawals.find(r => r.owner === a[0])?.records ?? []).map(r => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, k === "receiver" ? v : BigInt(v)])));
                break;
            case "hasRole": {
                let yes = false;
                for (const l of s.logs.filter(l => l.address === target && l.topics[1] === a[0] && l.topics[2] === word(a[1])))
                    yes = l.topics[0] === topic("RoleGranted(bytes32,address,address)");
                result = yes;
                break;
            }
            case "getOwners":
                result = safe.owners;
                break;
            case "getThreshold":
                result = BigInt(safe.threshold);
                break;
            case "nonce":
                result = BigInt(safe.nonce);
                break;
            case "getModulesPaginated":
                result = [safe.modules, address(1)];
                break;
            case "getStorageAt":
                result = word(safe.guard);
                break;
            case "getTimestamp":
                result = s.operationTimestamps[a[0]] ?? 0n;
                break;
            case "isOperationDone":
                result = s.operationTimestamps[a[0]] === 1n;
                break;
            case "maxRedeem":
                result = BigInt(s.balances[target]?.[a[0]] ?? 0);
                break;
            default: throw new Error(`unexpected ${name} at ${target}`);
        }
        return encodeFunctionResult({ abi: allAbi, functionName: name, result });
    }
    function log(state, target, signature, topics, amounts, number = head.number) { state.logs.push({ address: target, topics: [signature, ...topics.map(v => v.length === 42 ? word(v) : v)], data: encodeAbiParameters(amounts.map(() => ({ type: "uint256" })), amounts.map(BigInt)), blockNumber: `0x${number.toString(16)}`, blockHash: header(number).hash, logIndex: `0x${state.logs.length.toString(16)}`, transactionHash: hash(8000 + state.logs.length) }); }
    function deposit(amount = 10n) { const s = current, x = s.observed.core, user = address(900), tx = hash(900000 + s.logs.length), start = s.logs.length; log(s, c.addresses.stpros, TRANSFER, [ZERO, user], [amount]); if (chainId === 688689) log(s, c.addresses.oracle, topic("PoolUpdated(address,uint256,uint256,bool)"), [c.addresses.asset], [amount,amount,1]); log(s, c.addresses.stpros, DEPOSIT, [user, user], [amount, amount]); for (const l of s.logs.slice(start)) l.transactionHash = tx; s.balances[c.addresses.stpros] = { [user]: amount.toString() }; x.supply = amount.toString(); if (chainId === 1672)
        x.wrapped = amount.toString();
    else {
        x.poolT = amount.toString();
        x.poolV = amount.toString();
    } }
    function operationHistory(post = false) {
        const op = buildU(c, inventory, f.artifacts).operation;
        const schedule = topic("CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)"), execute = topic("CallExecuted(bytes32,uint256,address,uint256,bytes)"), cancelled = topic("Cancelled(bytes32)"), salt = topic("CallSalt(bytes32,bytes32)");
        current.logs = current.logs.filter(l => l.address !== c.addresses.timelock || ![schedule, execute, cancelled, salt].includes(l.topics[0]));
        current.operationTimestamps = {};
        if (post && head.number === pin.number + 1) { head.number++; head.hash = hash(head.number); }
        const scheduleBlock = post ? pin.number + 1 : head.number;
        if (post) head.timestamp = Math.max(head.timestamp, pin.timestamp + c.policy.minDelay + 2);
        for (const executing of post ? [false,true] : [false]) {
            const number = executing ? head.number : scheduleBlock;
            for (let i=0; i<op.targets.length; i++) {
                const values = [op.targets[i], op.values[i], op.payloads[i], ...(!executing ? [op.predecessor, BigInt(c.policy.minDelay)] : [])];
                current.logs.push({address:c.addresses.timelock, topics:[executing ? execute : schedule,op.id,hash(i)], data:encodeAbiParameters([{type:"address"},{type:"uint256"},{type:"bytes"},...(!executing ? [{type:"bytes32"},{type:"uint256"}] : [])],values),blockNumber:`0x${number.toString(16)}`,blockHash:header(number).hash,logIndex:`0x${current.logs.length.toString(16)}`,transactionHash:hash(executing ? 990002 : 990001)});
            }
        }
        current.logs.push({address:c.addresses.timelock,topics:[salt,op.id],data:op.salt,blockNumber:`0x${scheduleBlock.toString(16)}`,blockHash:header(scheduleBlock).hash,logIndex:`0x${current.logs.length.toString(16)}`,transactionHash:hash(990001)});
        current.operationTimestamps[op.id] = post ? 1n : BigInt(header(scheduleBlock).timestamp + c.policy.minDelay);
        return op.id;
    }
    const archiveRoot = mkdtempSync(join(realpathSync(tmpdir()), "faroo-history-test-")); archiveRoots.push(archiveRoot);
    function archive() {
        reapprove(f);
        const targets = historyTargets(c), filter_id = sha256(`[${targets.map(JSON.stringify).join(", ")}]`).slice(0, 16);
        function write(name, value) { const path = join(archiveRoot, name), raw = JSON.stringify(value); writeFileSync(path, raw); return { path, sha256: sha256(raw) }; }
        const windows = [];
        for (let a = inventory.fromBlock; a <= pin.number; a += 1000) {
            const b = Math.min(a + 999, pin.number);
            windows.push([a, b, historical.logs.filter(l => Number(BigInt(l.blockNumber)) >= a && Number(BigInt(l.blockNumber)) <= b).length]);
        }
        const chunk = write("chunk.json", { chain: chainId === 1672 ? "mainnet" : "testnet", filter_id, from: inventory.fromBlock, to: pin.number, windows, logs: historical.logs });
        const plan = write("plan.json", { chain: chainId === 1672 ? "mainnet" : "testnet", chain_id: chainId, start: inventory.fromBlock, end: pin.number, end_block_hash: pin.hash, window: 1000, addresses: targets.map(address => ({ address })), filter_id });
        const manifest = { schema: "faroo-history-v1", chainId, fromBlock: inventory.fromBlock, blockNumber: pin.number, blockHash: pin.hash, inventorySha256: c.review.inventorySha256, targets, segments: [{ planFile: plan.path, planSha256: plan.sha256, chunks: [chunk] }] };
        const proof = write("manifest.json", manifest); c.historyFile = proof.path; c.review.historySha256 = proof.sha256;
        return { manifest, chunk, plan, write };
    }
    archive();
    return { ...f, operationHistory, archive, pin, head, historical, current, calls, rpc, log, deposit, now: timestamp + 2, setReorg: () => reorg = true, setMissing: () => missingHistory = true, setRpcFailure: () => rpcFailure = true, seal: () => reapprove(f) };
}
