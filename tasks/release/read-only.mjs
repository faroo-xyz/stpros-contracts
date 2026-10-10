import { residualReads, termNames, termsHash, residualTopics, replayResidual } from "./residual-replay.mjs";
import { replayPool, poolTopics } from "./pool-replay.mjs";
import { replayOperations, operationTopics, checkOperationPlan, PINNED_TIMELOCK_RUNTIME_HASH } from "./timelock-history.mjs";
import { historySession } from "./history.mjs";
import { encodeFunctionData, decodeFunctionResult, decodeAbiParameters, parseAbi, keccak256, stringToHex } from "viem";
import { requireThat, addr, SLOTS, ZERO, canonical, exactSet, expectations, coreExpectation, buildU, verifyRuntime, residualVault, isTestnetRecovery, validateConfig, validateInventory } from "./core.mjs";
const READS = parseAbi([
    "function bridgeVault() view returns(address)", "function totalManagedAssets() view returns(uint256)", "function owner() view returns(address)", "function guardian() view returns(address)", "function pendingOwner() view returns(address)", "function getMinDelay() view returns(uint256)", "function hasRole(bytes32,address) view returns(bool)", "function getAllProxies() view returns(address[])", "function beacon() view returns(address)", "function implementation() view returns(address)", "function balanceOf(address) view returns(uint256)", "function totalSupply() view returns(uint256)", "function paused() view returns(bool)",
    "function queuedWithdrawal() view returns(uint256)", "function completedWithdrawal() view returns(uint256)", "function withdrawalHead(address) view returns(uint256)", "function withdrawalTail(address) view returns(uint256)", "function maxRedeem(address) view returns(uint256)",
    "function getOwners() view returns(address[])", "function getThreshold() view returns(uint256)", "function nonce() view returns(uint256)", "function getModulesPaginated(address,uint256) view returns(address[],address)", "function getStorageAt(uint256,uint256) view returns(bytes)",
    "function oracle() view returns(address)", "function weth() view returns(address)", "function vTokenAddresses(address) view returns(bool)", "function keeper() view returns(address)", "function getSlps() view returns(address[])", "function roundTermsHash(address) view returns(bytes32)", "function tokenToVToken(address) view returns(address)", "function asset() view returns(address)", "function poolInfo(address) view returns(uint256,uint256)", "function totalCanWithdrawAmount() view returns(uint256)", "function unbondingPeriod() view returns(uint256)", "function maxWithdrawCount() view returns(uint256)", "function slp() view returns(address)", "function commissionAccount() view returns(address)", "function commissionRatePpm() view returns(uint256)", "function maxUpdateAmount() view returns(uint256)", "function updateInterval() view returns(uint256)", "function phase() view returns(uint8)", "function roundIndex() view returns(uint256)", "function factory() view returns(address)", "function isFactoryVault(address) view returns(bool)", "function getTimestamp(bytes32) view returns(uint256)", "function isOperationDone(bytes32) view returns(bool)", "function lastUpdateAt(address) view returns(uint256)"
]);
const allowed = new Set(["eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_getStorageAt", "eth_call", "eth_getLogs", "eth_estimateGas", "eth_getTransactionByHash"]);
const hex = n => `0x${BigInt(n).toString(16)}`;
const topic = s => keccak256(stringToHex(s));
export function readOnlyRpc(url, simulation = false, localOnly = false) {
    const parsed = new URL(url);
    requireThat(!parsed.username && !parsed.password, "RPC_CREDENTIALS_FORBIDDEN");
    const publicEndpoint = ["https://rpc.pharos.xyz/", "https://atlantic.dplabs-internal.com/"].includes(parsed.href);
    requireThat(publicEndpoint || (simulation && ["http:", "https:"].includes(parsed.protocol) && ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname) && parsed.pathname === "/" && !parsed.search && !parsed.hash), "RPC_NOT_ALLOWLISTED");
    if (localOnly) requireThat(simulation && ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname), "LOCAL_RPC_REQUIRED");
    let id = 0, running = false, lastRequest = 0;
    const queue = [];
    async function drain() {
        while (queue.length) {
            const batch = queue.splice(0, 20), requests = batch.map(x => x.request);
            try {
                let bodies;
                for (let attempt = 0; ; attempt++) {
                    const wait = publicEndpoint ? Math.max(0, 350 - (Date.now() - lastRequest)) : 0;
                    if (wait) await new Promise(resolve => setTimeout(resolve, wait));
                    lastRequest = Date.now();
                    const response = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(batch.length === 1 ? requests[0] : requests), signal: AbortSignal.timeout(30000), redirect: "error" });
                    const body = response.ok ? await response.json() : undefined;
                    bodies = batch.length === 1 ? [body] : body;
                    const retry = [429, 500, 502, 503, 504].includes(response.status) || (Array.isArray(bodies) && bodies.some(b => [-32011, -32603].includes(b?.error?.code)));
                    if (retry && attempt < 2) { await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt)); continue; }
                    requireThat(response.ok, "RPC_HTTP", String(response.status)); break;
                }
                requireThat(Array.isArray(bodies) && bodies.length === batch.length && new Set(bodies.map(b => b?.id)).size === batch.length, "RPC_BATCH_INCOMPLETE");
                // Validate the whole batch before accepting any result. No missing slot
                // or failed log window may be silently converted into an empty result.
                for (const { request } of batch) {
                    const body = bodies.find(b => b?.id === request.id);
                    requireThat(body?.jsonrpc === "2.0", "RPC_RESPONSE_ID");
                    requireThat(!body.error, "RPC_ERROR", body.error?.message);
                    requireThat(Object.hasOwn(body, "result"), "RPC_MISSING_RESULT");
                }
                for (const item of batch) item.resolve(bodies.find(b => b.id === item.request.id).result);
            } catch (error) { for (const item of batch) item.reject(error); }
        }
        running = false;
    }
    return async (method, params) => {
        requireThat(allowed.has(method), "RPC_METHOD_REFUSED", method);
        if (method === "eth_getLogs")
            requireThat(BigInt(params[0].toBlock) >= BigInt(params[0].fromBlock) && BigInt(params[0].toBlock) - BigInt(params[0].fromBlock) < 1000n, "LOG_RANGE");
        return new Promise((resolve, reject) => {
            queue.push({ request: { jsonrpc: "2.0", id: ++id, method, params }, resolve, reject });
            if (!running) { running = true; setTimeout(drain, 0); }
        });
    };
}

function reader(rpc, block) { return async (address, functionName, args = [], abi = READS) => decodeFunctionResult({ abi, functionName, data: await rpc("eth_call", [{ to: address, data: encodeFunctionData({ abi, functionName, args }) }, block]) }); }
async function pinned(c, rpc, tag = "latest") {
    requireThat(Number(BigInt(await rpc("eth_chainId", []))) === c.chainId, "RPC_CHAIN");
    const h = await rpc("eth_getBlockByNumber", [tag, false]);
    requireThat(h?.hash && h.number && h.timestamp, "BLOCK_MISSING");
    if (tag !== "latest")
        requireThat(BigInt(h.number) === BigInt(tag), "BLOCK_NUMBER_MISMATCH");
    return { header: h, block: hex(BigInt(h.number)) };
}
async function logs(rpc, target, from, to, topics) {
    requireThat(from > 0 && to >= from, "HISTORY_RANGE");
    if (rpc.historyLogs) return rpc.historyLogs(target, from, to, topics);
    requireThat(await rpc("eth_getCode", [target, hex(from - 1)]) === "0x", "INVENTORY_START_HAS_CODE", target);
    return rangeLogs(rpc, target, from, to, topics);
}
async function rangeLogs(rpc, target, from, to, topics) {
    if (rpc.historyLogs) return rpc.historyLogs(target, from, to, topics);
    const out = [];
    for (let start = from; start <= to; start += 1000) {
        const rows = await rpc("eth_getLogs", [{ address: target, fromBlock: hex(start), toBlock: hex(Math.min(start + 999, to)), topics: [topics] }]);
        for (const row of rows)
            requireThat(!row.removed && row.address.toLowerCase() === target && Number(BigInt(row.blockNumber)) >= start && Number(BigInt(row.blockNumber)) <= Math.min(start + 999, to), "LOG_REORG_OR_TARGET");
        out.push(...rows);
    }
    return out.sort((a, b) => Number(BigInt(a.blockNumber) - BigInt(b.blockNumber)) || Number(BigInt(a.logIndex) - BigInt(b.logIndex)));
}
export async function scanRoles(c, inventory, rpc, block) {
    const grant = topic("RoleGranted(bytes32,address,address)"), revoke = topic("RoleRevoked(bytes32,address,address)"), admin = topic("RoleAdminChanged(bytes32,bytes32,bytes32)");
    const call = reader(rpc, block), result = {};
    for (const target of Object.keys(inventory.roles)) {
        const events = [], current = new Set(), ever = new Set();
        for (const l of await logs(rpc, target, inventory.fromBlock, Number(BigInt(block)), [grant, revoke, admin])) {
            requireThat(l.topics[0] !== admin, "ROLE_ADMIN_CHANGE_REQUIRES_REVIEW");
            const role = l.topics[1], account = `0x${l.topics[2].slice(-40)}`, key = `${role}:${account}`;
            ever.add(key);
            if (l.topics[0] === grant)
                current.add(key);
            else
                current.delete(key);
            events.push({ event: l.topics[0] === grant ? "RoleGranted" : "RoleRevoked", role, account, block: Number(BigInt(l.blockNumber)), logIndex: Number(BigInt(l.logIndex)), transactionHash: l.transactionHash });
        }
        await Promise.all([...ever].map(async key => requireThat(await call(target, "hasRole", [key.slice(0, 66), key.slice(67)]) === current.has(key), "ROLE_STATE_DRIFT", target)));
        result[target] = { events, current: [...current].sort(), ever: [...ever].sort() };
    }
    return result;
}
/// Full Transfer and Withdraw histories, including zero-balance former holders and queue owners.
export async function scanEconomics(c, inventory, rpc, block, post = false, migrated = post) {
    const call = reader(rpc, block), holders = {};
    for (const token of Object.keys(inventory.holders)) {
        const accounts = new Set(), ledger = new Map();
        for (const l of await logs(rpc, token, inventory.fromBlock, Number(BigInt(block)), [topic("Transfer(address,address,uint256)")])) {
            const from = `0x${l.topics[1].slice(-40)}`, to = `0x${l.topics[2].slice(-40)}`, amount = BigInt(l.data);
            for (const [a, delta] of [[from, -amount], [to, amount]])
                if (a !== ZERO) {
                    accounts.add(a);
                    ledger.set(a, (ledger.get(a) ?? 0n) + delta);
                }
        }
        let total = 0n;
        const rows = await Promise.all([...accounts].sort().map(async a => {
            const balance = await call(token, "balanceOf", [a]);
            requireThat(balance === ledger.get(a), "TRANSFER_REPLAY", token);
            total += balance;
            return { address: a, balance: balance.toString() };
        }));
        const supply = await call(token, "totalSupply");
        requireThat(total === supply, "HOLDER_SUM", token);
        holders[token] = { accounts: rows, totalSupply: supply.toString() };
        if (!post) {
            exactSet(inventory.holders[token].accounts.map(a => a.address), [...accounts], "HOLDER_TRUNCATION");
            for (const row of rows)
                requireThat(BigInt(inventory.holders[token].accounts.find(a => a.address === row.address).balance) === BigInt(row.balance), "HOLDER_BALANCE_DRIFT");
        }
    }
    const token = c.addresses.stpros, owners = new Set();
    for (const l of await logs(rpc, token, inventory.fromBlock, Number(BigInt(block)), [topic("Withdraw(address,address,address,uint256,uint256)")]))
        owners.add(`0x${l.topics[3].slice(-40)}`);
    const tuple = c.chainId === 1672 && !migrated ? "(uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod)" : "(uint256 queued,uint256 pending,uint256 createdAt,uint256 unbondingPeriod,address receiver)";
    const abi = parseAbi([`function getWithdrawals(address) view returns(${tuple}[])`]);
    let sum = 0n;
    const records = await Promise.all([...owners].sort().map(async owner => {
        const [head, tail, rows] = await Promise.all([call(token, "withdrawalHead", [owner]), call(token, "withdrawalTail", [owner]), call(token, "getWithdrawals", [owner], abi)]);
        requireThat(tail - head === BigInt(rows.length), "QUEUE_RANGE");
        for (const r of rows)
            sum += r.pending;
        return { owner, head: head.toString(), tail: tail.toString(), records: rows.map(r => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, typeof v === "bigint" ? v.toString() : v]))) };
    }));
    const queued = await call(token, "queuedWithdrawal"), completed = await call(token, "completedWithdrawal");
    requireThat(sum === queued - completed, "QUEUE_SUM");
    if (!post)
        requireThat(canonical(records) === canonical(inventory.withdrawals.accounts), "QUEUE_RECORDS_DRIFT");
    if (migrated)
        for (const f of c.factories)
            for (const v of f.vaults.filter(v => v.endState === "RETIRED"))
                for (const h of holders[v.address].accounts)
                    requireThat(await call(v.address, "maxRedeem", [h.address]) === BigInt(h.balance), "RETIRED_REDEEM");
    return { holders, withdrawals: { accounts: records, queued: queued.toString(), completed: completed.toString(), sumPending: sum.toString() } };
}
async function checkSafes(c, rpc, block, checkNonce) {
    if (c.mode !== "release")
        return;
    const call = reader(rpc, block);
    const unique = [...new Map(Object.entries(c.review.safes).map(([role, s]) => [s.address, [role, s]])).values()];
    await Promise.all(unique.map(async ([role, s]) => {
        requireThat(keccak256(await rpc("eth_getCode", [s.address, block])) === s.runtimeHash, "SAFE_CODEHASH", role);
        const singleton = await rpc("eth_getStorageAt", [s.address, "0x0", block]);
        requireThat(`0x${singleton.slice(-40)}` === s.singleton && keccak256(await rpc("eth_getCode", [s.singleton, block])) === s.singletonHash, "SAFE_SINGLETON");
        exactSet((await call(s.address, "getOwners")).map(a => a.toLowerCase()), s.owners, "SAFE_OWNERS");
        for (const owner of s.owners) requireThat(await rpc("eth_getCode", [owner, block]) === "0x", "INCOMPLETE_SAFE_SIGNER_CONTROL", `${role}: contract/delegated signer requires independently established control evidence`);
        requireThat(await call(s.address, "getThreshold") === BigInt(s.threshold), "SAFE_THRESHOLD");
        if (checkNonce)
            requireThat(await call(s.address, "nonce") === BigInt(s.nonce), "SAFE_NONCE");
        const [modules, next] = await call(s.address, "getModulesPaginated", ["0x0000000000000000000000000000000000000001", 1n]);
        requireThat(modules.length === 0 && next.toLowerCase() === "0x0000000000000000000000000000000000000001", "SAFE_MODULES");
        // Safe's public StorageAccessible API; guard slot is the canonical Safe slot, not proxy storage.
        const guard = await call(s.address, "getStorageAt", [BigInt(topic("guard_manager.guard.address")), 1n]);
        requireThat(BigInt(guard) === 0n, "SAFE_GUARD");
    }));
}
async function checkProxy(rpc, block, address, expected, version) {
    const ad = await rpc("eth_getStorageAt", [address, SLOTS.admin, block]), im = await rpc("eth_getStorageAt", [address, SLOTS.implementation, block]);
    const init = BigInt(await rpc("eth_getStorageAt", [address, SLOTS.initialized, block]));
    requireThat(`0x${ad.slice(-40)}` === expected.admin && `0x${im.slice(-40)}` === expected.implementation, "PROXY_STORAGE_DRIFT", address);
    requireThat((init & ((1n << 64n) - 1n)) === BigInt(version) && ((init >> 64n) & 255n) === 0n, "INITIALIZER_VERSION", address);
}
async function observeState(c, inventory, rpc, block, reviewed, expectedRoles, checkNonce = true) {
    const call = reader(rpc, block), residualStates = {};
    await Promise.all(Object.entries(c.observed.code).map(async ([address, expected]) => requireThat(await rpc("eth_getCode", [address, block]) === expected, "CODE_DRIFT", address)));
    for (const [address, p] of Object.entries(c.observed.proxies)) {
        await checkProxy(rpc, block, address, p, p.version);
        requireThat((await call(address, "owner")).toLowerCase() === p.owner, "OWNER_DRIFT", address);
        if (p.paused !== null)
            requireThat(await call(address, "paused") === p.paused, "PAUSE_DRIFT");
        if (p.version === 4)
            requireThat((await call(address, "guardian")).toLowerCase() === c.actors.guardian && (await call(address, "pendingOwner")) === ZERO, "GUARDIAN_OR_PENDING_OWNER");
    }
    requireThat((await call(c.addresses.proxyAdmin, "owner")).toLowerCase() === c.observed.proxyAdminOwner, "PA_OWNER_DRIFT");
    requireThat(Number(await call(c.addresses.timelock, "getMinDelay")) === c.observed.timelock.minDelay, "DELAY_DRIFT");
    for (const f of c.factories) {
        const state = c.observed.factories[f.address];
        requireThat((await call(f.address, "beacon")).toLowerCase() === f.beacon, "BEACON_DRIFT");
        requireThat((await call(f.beacon, "owner")).toLowerCase() === f.address, "BEACON_OWNER");
        requireThat((await call(f.beacon, "implementation")).toLowerCase() === state.implementation, "BEACON_IMPLEMENTATION");
        requireThat(canonical((await call(f.address, "getAllProxies")).map(a => a.toLowerCase())) === canonical(state.vaults), "VAULT_DISCOVERY_DRIFT");
        for (const v of f.vaults) {
            const x = c.observed.vaults[v.address];
            const phase = Number(await call(v.address, "phase")), round = await call(v.address, "roundIndex");
            if (c.chainId === 1672 && v.endState === "GOVERNED") requireThat(phase === 3, "MAIN_VAULT_PHASE");
            if (reviewed || !residualVault(c, v)) requireThat(phase === x.phase && round === BigInt(x.round), "VAULT_STATE_DRIFT");
            if (residualVault(c, v)) residualStates[v.address] = await residualState(c, rpc, block, v.address, phase, round);
            requireThat(await call(f.address, "isFactoryVault", [v.address]), "VAULT_REGISTRY");
            requireThat((await call(v.address, "factory")).toLowerCase() === f.address && (await call(v.address, "asset")).toLowerCase() === c.addresses.stpros, "VAULT_CANONICAL");
            if (reviewed || !residualVault(c, v)) requireThat(await call(c.addresses.assertions, "roundTermsHash", [v.address]) === x.termsHash, "VAULT_TERMS_DRIFT");
        }
    }
    const roles = await scanRoles(c, inventory, rpc, block);
    for (const [target, r] of Object.entries(roles)) {
        requireThat(canonical(r.events.filter(ev => ev.block <= c.observed.blockNumber)) === canonical(inventory.roles[target].events), "ROLE_LOG_INVENTORY_MISMATCH", target);
        const residual = c.factories.flatMap(f => f.vaults).some(v => v.address === target && residualVault(c, v));
        if (!reviewed && !residual) {
            if (expectedRoles) {
                const expected = expectedRoles.filter(x => x.target === target);
                exactSet(r.current, expected.filter(x => x.expected).map(x => `${x.role}:${x.account}`), "UNEXPECTED_AUTHORITY");
                for (const ev of r.events.filter(ev => ev.block > c.observed.blockNumber))
                    requireThat(expected.some(x => x.role === ev.role && x.account === ev.account && x.expected === (ev.event === "RoleGranted")), "UNREVIEWED_ROLE_CHANGE", target);
            }
            else
                requireThat(r.events.length === inventory.roles[target].events.length, "UNREVIEWED_ROLE_CHANGE", target);
        }
    }
    const economic = await scanEconomics(c, inventory, rpc, block, !reviewed, false);
    const x = c.observed.core, d = c.addresses;
    requireThat((await call(d.stpros, "oracle")).toLowerCase() === d.oracle, "ORACLE_POINTER");
    requireThat((await call(d.bridge, "weth")).toLowerCase() === d.asset && await call(d.bridge, "vTokenAddresses", [d.stpros]), "BRIDGE_CONFIGURATION");
    requireThat((await call(d.distributor, "keeper")).toLowerCase() === c.actors.keeper, "KEEPER");
    requireThat(canonical((await call(d.distributor, "getSlps")).map(a => a.toLowerCase())) === canonical(c.destinations), "DESTINATIONS");
    for (const a of c.destinations)
        requireThat(await rpc("eth_getCode", [a, block]) !== "0x", "DESTINATION_NO_CODE");
    requireThat((await call(d.stpros, "asset")).toLowerCase() === d.asset, "ASSET");
    if (c.chainId === 688689) {
        requireThat((await call(d.oracle, "tokenToVToken", [d.asset])).toLowerCase() === d.stpros && await call(d.oracle, "vTokenAddresses", [d.stpros]), "CANONICAL");
        requireThat((await call(d.stpros, "bridgeVault")).toLowerCase() === d.bridge && (await call(d.stpros, "slp")).toLowerCase() === d.distributor, "LEGACY_CORE_WIRING");
    }
    const core = {};
    for (const [field, name] of [["supply", "totalSupply"], ["reserve", "totalCanWithdrawAmount"], ["queued", "queuedWithdrawal"], ["completed", "completedWithdrawal"], ["unbondingPeriod", "unbondingPeriod"], ["maxWithdrawCount", "maxWithdrawCount"]]) {
        core[field] = await call(d.stpros, name);
        if (reviewed || ["unbondingPeriod", "maxWithdrawCount"].includes(field))
            requireThat(core[field] === BigInt(x[field]), "CORE_DRIFT", field);
    }
    [core.poolT, core.poolV] = await call(d.oracle, "poolInfo", [d.asset]);
    core.wrapped = await call(d.asset, "balanceOf", [d.stpros]);
    core.escrow = await call(d.stpros, "balanceOf", [d.stpros]);
    if (reviewed)
        for (const field of ["poolT", "poolV", "wrapped", "escrow"])
            requireThat(core[field] === BigInt(x[field]), "REVIEWED_ECONOMIC_DRIFT", field);
    if (c.chainId === 1672)
        requireThat(core.poolT === 0n && core.poolV === 0n && core.reserve === core.queued - core.completed && core.escrow >= core.reserve && core.wrapped >= core.reserve && core.wrapped - core.reserve >= core.supply - core.escrow, "V1_LIVE_BACKING");
    else
        requireThat(core.poolV === core.supply && core.reserve === 0n, "V3_LIVE_ACCOUNTING");
    const vaultAssets = {};
    for (const f of c.factories)
        for (const v of f.vaults) {
            const tma = await call(v.address, "totalManagedAssets");
            requireThat(await call(d.stpros, "balanceOf", [v.address]) >= tma, "VAULT_LIVE_BACKING");
            vaultAssets[v.address] = tma;
        }
    if (c.chainId === 688689)
        for (const [field, name] of [["reporter", "slp"], ["treasury", "commissionAccount"], ["commissionPpm", "commissionRatePpm"], ["maxUpdateAmount", "maxUpdateAmount"], ["interval", "updateInterval"]])
            requireThat(String(await call(d.oracle, name)).toLowerCase() === String(x[field]).toLowerCase(), "ORACLE_CONFIG_DRIFT", field);
    // Initial/preschedule nonce is the independently reviewed signing nonce, at both blocks.
    await checkSafes(c, rpc, block, checkNonce);
    return { economic, core, vaultAssets, roles, residualStates };
}
async function residualState(c, rpc, block, target, phase, round) {
    const call = reader(rpc, block);
    const terms = await Promise.all(termNames.map(n => call(target, n, [], residualReads)));
    requireThat(termsHash(terms) === await call(c.addresses.assertions, "roundTermsHash", [target]), "RESIDUAL_TERMS_HASH");
    const mode = await call(target, "settleMode", [], residualReads), funded = await call(target, "profitFunded", [], residualReads), amount = await call(target, "settleAmount", [], residualReads);
    const managed = await call(target, "totalManagedAssets");
    requireThat(await call(c.addresses.stpros, "balanceOf", [target]) >= managed + (phase === 2 && mode === 0 && funded ? amount : 0n), "RESIDUAL_FUNDED_BACKING");
    return { phase, round, terms };
}
// The reviewed block is stable evidence; it is not synonymous with the latest block.
export async function reviewedContext(c, rpc, stage = "pre", nowSeconds = Math.floor(Date.now() / 1000)) {
    const reviewed = await pinned(c, rpc, hex(c.observed.blockNumber)), head = await pinned(c, rpc);
    requireThat(reviewed.header.hash === c.observed.blockHash, "REVIEWED_PIN_HASH");
    requireThat(Number(BigInt(reviewed.header.timestamp)) === c.observed.timestamp, "REVIEWED_PIN_TIMESTAMP");
    requireThat(BigInt(head.block) >= BigInt(reviewed.block) && BigInt(head.header.timestamp) >= BigInt(reviewed.header.timestamp), "HEAD_BEFORE_REVIEW");
    const headTime = Number(BigInt(head.header.timestamp));
    requireThat(headTime <= nowSeconds + 30 && nowSeconds - headTime <= 120, "STALE_CURRENT_HEAD");
    if (stage === "pre")
        requireThat(nowSeconds - c.observed.timestamp <= 3600, "REVIEW_EXPIRED");
    return { reviewed, head, nowSeconds, startedAt: Date.now() };
}
async function canonicalAgain(context, rpc) {
    for (const pin of [context.reviewed, context.head])
        requireThat((await rpc("eth_getBlockByNumber", [pin.block, false]))?.hash === pin.header.hash, "PIN_REORG");
    const latest = await rpc("eth_getBlockByNumber", ["latest", false]);
    const now = context.nowSeconds + (Date.now() - context.startedAt) / 1000;
    requireThat(latest?.hash && BigInt(latest.number) >= BigInt(context.head.block), "CHECKPOINT_HEAD_MISSING");
    return Number(BigInt(latest.timestamp)) <= now + 30 && now - Number(BigInt(latest.timestamp)) <= 120 && now - Number(BigInt(context.head.header.timestamp)) <= 120 && BigInt(latest.timestamp) - BigInt(context.head.header.timestamp) <= 120n;
}
const DEPOSIT = topic("Deposit(address,address,uint256,uint256)"), WITHDRAW = topic("Withdraw(address,address,address,uint256,uint256)"), CLAIM = topic("WithdrawalCompleted(address,address,uint256)");
const words = (log, n) => decodeAbiParameters(Array.from({ length: n }, () => ({ type: "uint256" })), log.data);
/// Reconcile queued records exactly from permitted Withdraw/claim events, not an unchecked drift flag.
async function reconcileEconomics(c, inventory, rpc, block, before, after, post = false) {
    const from = c.observed.blockNumber + 1, to = Number(BigInt(block));
    if (from > to)
        return;
    const upgraded = topic("Upgraded(address)");
    const rows = await rangeLogs(rpc, c.addresses.stpros, from, to, [DEPOSIT, WITHDRAW, CLAIM, upgraded]);
    // The original proxy/version is checked at the authenticated historical pin.
    // Mainnet V1 aggregates a claim call; recorded testnet V3 (initializer 2)
    // and R4 emit per record. Only the checked upgrade event changes this model.
    const legacyVersion = c.observed.proxies[c.addresses.stpros].version;
    requireThat(legacyVersion === c.legacy[c.addresses.stpros].version &&
        ((c.chainId === 1672 && legacyVersion === 1) || (c.chainId === 688689 && legacyVersion === 2)), "CLAIM_EVENT_MODEL");
    let r4 = false;
    const accounts = new Map(before.economic.withdrawals.accounts.map(a => [a.owner, structuredClone(a)]));
    let q = BigInt(before.economic.withdrawals.queued), completed = BigInt(before.economic.withdrawals.completed);
    for (const l of rows) {
        if (l.topics[0] === upgraded) {
            requireThat(post && !r4 && `0x${l.topics[1].slice(-40)}` === c.implementations.stpros, "UNREVIEWED_UPGRADE_HISTORY");
            r4 = true;
            if (c.chainId === 1672) for (const a of accounts.values()) for (const r of a.records) r.receiver = ZERO;
            continue;
        }
        if (l.topics[0] === DEPOSIT) {
            continue;
        }
        const owner = `0x${l.topics[l.topics[0] === WITHDRAW ? 3 : 1].slice(-40)}`;
        let a = accounts.get(owner);
        if (!a) {
            a = { owner, head: "0", tail: "0", records: [] };
            accounts.set(owner, a);
        }
        const amount = words(l, l.topics[0] === WITHDRAW ? 2 : 1)[0];
        if (l.topics[0] === WITHDRAW) {
            const h = await rpc("eth_getBlockByNumber", [l.blockNumber, false]);
            requireThat(h?.hash === l.blockHash, "ECONOMIC_EVENT_BLOCK");
            const record = { queued: q.toString(), pending: amount.toString(), createdAt: BigInt(h.timestamp).toString(), unbondingPeriod: before.core.unbondingPeriod.toString() };
            if (c.chainId === 688689 || r4) {
                const receiver = `0x${l.topics[2].slice(-40)}`;
                record.receiver = r4 && receiver === owner ? ZERO : receiver;
            }
            a.records.push(record);
            a.tail = (BigInt(a.tail) + 1n).toString();
            q += amount;
        }
        else {
            const aggregate = legacyVersion === 1 && !r4;
            const receiver = `0x${l.topics[2].slice(-40)}`;
            requireThat(a.records.length > 0, "CLAIM_WITHOUT_RECORD");
            requireThat(amount > 0n, "CLAIM_AMOUNT"); // Neither legacy version emits on zero payout.
            requireThat(receiver !== ZERO, "CLAIM_RECEIVER");
            const h = await rpc("eth_getBlockByNumber", [l.blockNumber, false]);
            requireThat(h?.hash === l.blockHash, "ECONOMIC_EVENT_BLOCK");
            let remaining = amount;
            while (remaining > 0n) {
                const r = a.records[0];
                requireThat(r, "CLAIM_AMOUNT");
                const pending = BigInt(r.pending);
                // V1's fully reserved path consumes whole records; a maxRecords
                // limit can stop at any record boundary, never inside a record.
                requireThat(pending > 0n && (aggregate ? remaining >= pending : remaining <= pending), "CLAIM_AMOUNT");
                requireThat(!r.receiver || r.receiver === ZERO || receiver === r.receiver, "CLAIM_RECEIVER");
                requireThat(BigInt(h.timestamp) >= BigInt(r.createdAt) + BigInt(r.unbondingPeriod), "CLAIM_UNBONDING");
                const paid = aggregate ? pending : remaining;
                remaining -= paid;
                if (paid === pending) {
                    a.records.shift();
                    a.head = (BigInt(a.head) + 1n).toString();
                } else {
                    r.pending = (pending - paid).toString();
                    r.queued = (BigInt(r.queued) + paid).toString();
                }
            }
            completed += amount;
        }
    }
    requireThat(q === BigInt(after.economic.withdrawals.queued) && completed === BigInt(after.economic.withdrawals.completed), "UNRECONCILED_QUEUE_TOTALS");
    const expected = [...accounts.values()].sort((a, b) => a.owner.localeCompare(b.owner));
    requireThat(canonical(expected) === canonical(after.economic.withdrawals.accounts), "UNRECONCILED_WITHDRAWAL_RECORDS");
    if (c.chainId === 688689) {
        const poolRows = (await Promise.all([c.addresses.stpros, c.addresses.oracle].map(target => rangeLogs(rpc, target, from, to, poolTopics)))).flat().sort((a, b) => Number(BigInt(a.blockNumber) - BigInt(b.blockNumber)) || Number(BigInt(a.logIndex) - BigInt(b.logIndex)));
        replayPool(c, poolRows, before.core, after.core);
    }
    for (const f of c.factories)
        for (const v of f.vaults) {
            let tma = before.vaultAssets[v.address];
            const residual = residualVault(c, v), finalized = topic("SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)");
            const vaultRows = await rangeLogs(rpc, v.address, from, to, [DEPOSIT, WITHDRAW, ...(residual ? residualTopics : [])]);
            for (const l of vaultRows) {
                if ([DEPOSIT, WITHDRAW].includes(l.topics[0])) tma += (l.topics[0] === DEPOSIT ? 1n : -1n) * words(l, 2)[0];
                else if (l.topics[0] === finalized) {
                    const [amount, netProfit, fee] = words(l, 4), mode = BigInt(l.topics[2]);
                    requireThat(mode <= 1n && (mode === 0n ? netProfit + fee === amount : netProfit === 0n && fee === 0n), "RESIDUAL_SETTLEMENT_ACCOUNTING");
                    tma += mode === 0n ? netProfit : -amount;
                }
                requireThat(tma >= 0n, "RESIDUAL_NEGATIVE_ASSETS");
            }
            if (residual) {
                const call = reader(rpc, block), phase = Number(await call(v.address, "phase")), round = await call(v.address, "roundIndex");
                const state = after.residualStates?.[v.address] ?? await residualState(c, rpc, block, v.address, phase, round);
                const h = await rpc("eth_getBlockByNumber", [block, false]);
                await replayResidual(before.residualStates[v.address], state, vaultRows.filter(l => residualTopics.includes(l.topics[0])), BigInt(h.timestamp), post, c.chainId, row => rpc("eth_getTransactionByHash", [row.transactionHash]));
            }
            requireThat(tma === after.vaultAssets[v.address], "UNRECONCILED_VAULT_ASSETS", v.address);
        }
}
async function physicalIdentity(c, rpc, context) {
    const targets = [c.addresses.oracle, c.addresses.stpros, c.addresses.bridge, c.addresses.distributor, c.addresses.proxyAdmin, c.addresses.timelock, c.addresses.assertions, c.addresses.asset, ...c.factories.flatMap(f => [f.address, f.beacon, ...f.vaults.map(v => v.address)])];
    await Promise.all(targets.map(async target => {
        const reviewed = await rpc("eth_getCode", [target, context.reviewed.block]);
        requireThat(reviewed !== "0x" && reviewed === await rpc("eth_getCode", [target, context.head.block]), "PHYSICAL_CODE_DRIFT", target);
    }));
}
// No transient upgrade/admin change may hide behind an unchanged current implementation slot.
async function upgradeHistory(c, rpc, block, expectedProxies = [], post = false) {
    const upgrade = topic("Upgraded(address)"), admin = topic("AdminChanged(address,address)"), beacon = topic("BeaconUpgraded(address)");
    const proxies = [c.addresses.oracle, c.addresses.stpros, c.addresses.bridge, c.addresses.distributor, ...c.factories.map(f => f.address)];
    for (const target of [...proxies, ...c.factories.map(f => f.beacon)]) {
        const wanted = proxies.includes(target) ? expectedProxies.find(p => p.proxy === target)?.implementation : post ? c.implementations.vault : undefined;
        const prior = proxies.includes(target) ? c.legacy[target].implementation : c.factories.find(f => f.beacon === target).legacyImplementation;
        const events = await rangeLogs(rpc, target, c.observed.blockNumber + 1, Number(BigInt(block)), [upgrade, admin, beacon]);
        requireThat(events.length === (wanted && wanted !== prior ? 1 : 0), "UNREVIEWED_UPGRADE_HISTORY", target);
        for (const l of events)
            requireThat(l.topics[0] === upgrade && `0x${l.topics[1].slice(-40)}` === wanted, "UNREVIEWED_UPGRADE_HISTORY", target);
    }
}
// Detect transient configuration changes too. R4 migration events must belong to
// the same transaction as a checked implementation upgrade; final values are also
// checked by ReleaseAssertions. Other parameter changes require a new review.
async function governanceHistory(c, rpc, block, stage = "pre", proxies = []) {
    const config = ["SlpChanged(address,address)", "BridgeVaultChanged(address,address)", "OracleChanged(address,address)", "MaxWithdrawCountChanged(uint256)", "UnbondingPeriodChanged(uint256,uint256)", "MaxAprPpmChanged(uint256)", "VTokenSet(address,bool)", "VTokenSet(address,bool,bool)", "WethChanged(address,address)", "UpdateLimitsChanged(uint256,uint256)", "CommissionConfigChanged(address,uint256)", "CommissionVTokenSet(address,address)", "KeeperChanged(address,address)", "SlpWhitelistUpdated(address,bool)", "GuardianChanged(address,address)", "TimelockChanged(address,address)", "ApprovedPerformanceFeeChanged(uint16,uint16)", "CounterpartyWhitelistUpdated(address,bool)", "MinDelayChange(uint256,uint256)", "Paused(address)", "Unpaused(address)"] .map(topic);
    const ownership = topic("OwnershipTransferred(address,address)"), pending = topic("OwnershipTransferStarted(address,address)"), upgraded = topic("Upgraded(address)");
    const owners = new Map(Object.entries(c.observed.proxies).map(([a, p]) => [a, p.owner]));
    owners.set(c.addresses.proxyAdmin, c.observed.proxyAdminOwner);
    for (const f of c.factories) owners.set(f.beacon, f.address);
    const targets = [...owners.keys(), c.addresses.timelock];
    for (const target of targets) {
        const rows = await rangeLogs(rpc, target, c.observed.blockNumber + 1, Number(BigInt(block)), [...config, ownership, pending, upgraded]);
        let transferred = false, currentOwner = owners.get(target);
        for (const l of rows) {
            if (l.topics[0] === upgraded) continue; // checked separately by upgradeHistory
            if ([ownership, pending].includes(l.topics[0])) {
                const expected = target === c.addresses.proxyAdmin ? c.addresses.timelock : proxies.find(p => p.proxy === target)?.owner;
                requireThat(stage !== "pre" && expected && (!transferred || (stage === "R4" && rows.some(u => u.topics[0] === upgraded && u.transactionHash === l.transactionHash))) && `0x${l.topics[1].slice(-40)}` === currentOwner && `0x${l.topics[2].slice(-40)}` === expected, "UNREVIEWED_OWNERSHIP_HISTORY", target);
                if (l.topics[0] === ownership) { transferred = true; currentOwner = expected; }
            } else {
                requireThat((stage === "R4" || (stage === "H" && proxies.some(p => p.proxy === target && p.guarded))) && target !== c.addresses.timelock && rows.some(u => u.topics[0] === upgraded && u.transactionHash === l.transactionHash && u.blockHash === l.blockHash), "UNREVIEWED_CONFIGURATION_HISTORY", target);
            }
        }
    }
}
async function timelockInventory(c, inventory, rpc, block, pending, doneId) {
    const call = reader(rpc, block);
    requireThat(typeof rpc.historyLogs === "function", "TIMELOCK_AUTHENTICATED_HISTORY_REQUIRED");
    const sourceRuntimeHash = keccak256(await rpc("eth_getCode", [c.addresses.timelock, block]));
    requireThat(sourceRuntimeHash === PINNED_TIMELOCK_RUNTIME_HASH, "TIMELOCK_PROOF_RUNTIME");
    const head = await rpc("eth_getBlockByNumber", [block, false]);
    requireThat(head?.timestamp && BigInt(head.number) === BigInt(block), "TIMELOCK_CHECKPOINT_BLOCK");
    const rows = await logs(rpc, c.addresses.timelock, inventory.fromBlock, Number(BigInt(block)), operationTopics);
    const result = await replayOperations(rows, async row => {
        const h = await rpc("eth_getBlockByNumber", [row.blockNumber, false]);
        requireThat(h?.hash === row.blockHash, "TIMELOCK_EVENT_BLOCK");
        return BigInt(h.timestamp);
    }, id => call(c.addresses.timelock, "getTimestamp", [id]), BigInt(head.timestamp));
    checkOperationPlan(result, pending, doneId);
    return {...result, actionBearingCoverage:"PASS", sourceRuntimeHash, assumptions:"Complete independently reviewed creation-to-checkpoint archive and truthful canonical RPC journal; whole-operation omission is not detectable by parsing alone"};
}
// Historical authentication and archive views have no 120s deadline. Only the
// final current-state checkpoint does. A slow pass catches up with the same journal,
// re-reads authoritative views at the new block, and never mutates the original U.
async function atFreshCheckpoint(c, inventory, rawRpc, stage, options, verify) {
    validateConfig(c);
    validateInventory(c, inventory, c.review.inventorySha256);
    const started = Date.now(), initialNow = options.nowSeconds ?? Math.floor(started / 1000);
    const now = () => initialNow + (Date.now() - started) / 1000;
    const session = await historySession(c, inventory, rawRpc), rpc = session.rpc;
    const first = await reviewedContext(c, rpc, stage, now());
    const historical = await observeState(c, inventory, rpc, first.reviewed.block, true);
    for (let attempt = 0; attempt < 3; attempt++) {
        const context = await reviewedContext(c, rpc, stage, now());
        await session.extend(context.head);
        // An expensive first catch-up may consume the window; do not call it current.
        if (!(await canonicalAgain(context, rpc))) continue;
        await physicalIdentity(c, rpc, context);
        const result = await verify(rpc, context, historical);
        await session.checkPins();
        if (await canonicalAgain(context, rpc))
            return { ...result, history: session.statistics(), checkpointAttempts: attempt + 1 };
    }
    throw new Error("CHECKPOINT_SCAN_EXPIRED: catch-up limit reached");
}
export async function recheckObserved(c, inventory, rpc, options = {}) {
    return atFreshCheckpoint(c, inventory, rpc, "pre", options, async (rpc, context, historical) => {
        const current = context.reviewed.block === context.head.block ? historical : await observeState(c, inventory, rpc, context.head.block, false);
        await reconcileEconomics(c, inventory, rpc, context.head.block, historical, current);
        await upgradeHistory(c, rpc, context.head.block);
        await governanceHistory(c, rpc, context.head.block);
        const operations = await timelockInventory(c, inventory, rpc, context.head.block, []);
        return { status: "PASS", scope: "authenticated history and current pre-schedule checks; action-bearing operation coverage with dependency verification", reviewedBlock: c.observed.blockNumber, reviewedHash: c.observed.blockHash, currentBlock: Number(BigInt(context.head.block)), currentHash: context.head.header.hash, economic: current.economic, timelockOperations: operations, operationBundle: "UNCHANGED", noncePolicy: "reviewed signing nonce required" };
    });
}
/// Reuses the frozen pre-H bundle; never rebuild operation calldata from a post-H state.
export async function verifyCheckpoint(c, inventory, artifacts, rpc, stage = "R4", options = {}) {
    requireThat(stage === "H" || stage === "R4", "CHECKPOINT");
    return atFreshCheckpoint(c, inventory, rpc, stage, options, async (rpc, context, historical) => {
    const { header, block } = context.head, call = reader(rpc, block), e = expectations(c, inventory, artifacts, stage), u = buildU(c, inventory, artifacts);
    if (c.chainId === 1672) for (const v of e.vaults.filter(v => v.governed)) requireThat(Number(await call(v.vault, "phase")) === 3, "MAIN_VAULT_PHASE");
    const operations = await timelockInventory(c, inventory, rpc, block, stage === "H" ? [u.operation.id] : [], stage === "R4" ? u.operation.id : undefined);
    if (stage === "H") {
        const handoff = structuredClone(c);
        handoff.observed.proxyAdminOwner = c.addresses.timelock;
        for (const p of e.proxies)
            Object.assign(handoff.observed.proxies[p.proxy], { owner: p.owner, implementation: p.implementation, version: p.guarded ? 4 : c.legacy[p.proxy].version });
        const current = await observeState(handoff, inventory, rpc, block, false, e.roles, false);
        await reconcileEconomics(c, inventory, rpc, block, historical, current);
    }
    for (const name of ["oracle", "stpros", "bridge", "distributor", "factory", "vault", "timelock", "assertions", "proxyAdmin"])
        verifyRuntime(await rpc("eth_getCode", [c.implementations[name] ?? c.addresses[name], block]), artifacts[name], name === "factory" ? c.implementations.vault : undefined);
    for (const p of e.proxies)
        await checkProxy(rpc, block, p.proxy, { admin: c.addresses.proxyAdmin, implementation: p.implementation }, p.guarded ? 4 : c.legacy[p.proxy].version);
    await upgradeHistory(c, rpc, block, e.proxies, stage === "R4");
    const roles = await scanRoles(c, inventory, rpc, block);
    for (const [target, r] of Object.entries(roles))
        requireThat(canonical(r.events.filter(ev => ev.block <= c.observed.blockNumber)) === canonical(inventory.roles[target].events), "ROLE_LOG_INVENTORY_MISMATCH", target);
    const residuals = c.factories.flatMap(f => f.vaults).filter(v => residualVault(c, v)).map(v => v.address);
    for (const [target, r] of Object.entries(roles))
        if (!residuals.includes(target)) {
            exactSet(r.current, e.roles.filter(x => x.target === target && x.expected).map(x => `${x.role}:${x.account}`), "UNEXPECTED_AUTHORITY");
            for (const ev of r.events.filter(ev => ev.block > c.observed.blockNumber))
                requireThat(e.roles.some(x => x.target === target && x.role === ev.role && x.account === ev.account && x.expected === (ev.event === "RoleGranted")), "UNREVIEWED_ROLE_CHANGE");
        }
    const fn = stage === "R4" ? "assertR4" : isTestnetRecovery(c) ? "assertTestnetRecoveryHandoff" : "assertHandoff";
    const assertionResult = await rpc("eth_call", [{ to: c.addresses.assertions, data: encodeFunctionData({ abi: artifacts.assertions.abi, functionName: fn, args: [e, stage === "R4" ? coreExpectation(c) : u.operation.id] }) }, block]);
    requireThat(assertionResult === "0x", "ASSERTION_RESULT_MISSING");
    if (stage === "R4")
        requireThat(await call(c.addresses.timelock, "isOperationDone", [u.operation.id]), "OPERATION_NOT_DONE");
    // H/execution and postcheck inspect Safe structure but do not reuse a consumed H nonce.
    // Execution signing nonces must come from a separately reviewed, fresh checkpoint artifact.
    await checkSafes(c, rpc, block, false);
    if (stage === "H" && c.mode === "release") {
        const evidence = options.executionEvidence;
        requireThat(evidence?.operationId === u.operation.id && evidence.forkReference && evidence.freshFork === "PASS" && evidence.fs6 === "PASS", "EXECUTION_EVIDENCE_REQUIRED");
        const pin = await rpc("eth_getBlockByNumber", [hex(evidence.blockNumber), false]);
        requireThat(pin?.hash === evidence.blockHash && BigInt(pin.number) <= BigInt(block) && Number(BigInt(header.timestamp) - BigInt(pin.timestamp)) <= 3600, "EXECUTION_EVIDENCE_STALE");
        requireThat(Number.isSafeInteger(evidence.uGas) && evidence.uGas > 0 && evidence.uGas <= (c.releaseGates.confirmedGasCap ?? 2200000), "EXECUTION_GAS_GATE");
        for (const role of ["gov", "can"])
            requireThat(evidence.nonces?.[role] !== undefined && await call(c.review.safes[role].address, "nonce") === BigInt(evidence.nonces[role]), "EXECUTION_NONCE", role);
    }
    const economics = await scanEconomics(c, inventory, rpc, block, true, stage === "R4");
    if (stage === "R4") {
        const [poolT, poolV] = await call(c.addresses.oracle, "poolInfo", [c.addresses.asset]);
        const vaultAssets = Object.fromEntries(await Promise.all(c.factories.flatMap(f => f.vaults).map(async v => [v.address, await call(v.address, "totalManagedAssets")])));
        await reconcileEconomics(c, inventory, rpc, block, historical, { economic: economics, core: { poolT, poolV }, vaultAssets }, true);
    }
    await governanceHistory(c, rpc, block, stage, e.proxies);
    return { status: "PASS", scope: stage === "H" ? "action-bearing pending operations, roles and implementation checks; action-bearing operation coverage with dependency verification" : "post-state invariants, versions, roles and economics; action-bearing operation coverage with dependency verification", reviewedBlock: c.observed.blockNumber, reviewedHash: c.observed.blockHash, block: Number(BigInt(block)), blockHash: header.hash, operationId: u.operation.id, economics, timelockOperations: operations, roleInventory: roles, residualVaultRoles: { targets: residuals, status: stage === "R4" ? "INFORMATIONAL — verified retired; roles inert" : "OWNER-ACCEPTED EXPOSURE UNTIL U" }, notProven: ["Cross-transaction economic deltas: require execution-frame evidence; pre-H balances cannot be frozen across user activity.", "Binding FS-1 through FS-7 and FS-6 gas gate remain Batch 04."] };
    });
}
export async function estimateGas(c, transactions, actor, rpc) {
    requireThat(c.mode === "simulation", "LOCAL_ESTIMATION_ONLY");
    const estimates = [];
    for (const t of transactions) {
        try {
            estimates.push({ to: t.to, gas: await rpc("eth_estimateGas", [{ from: actor, to: t.to, data: t.data, value: hex(t.value) }]), status: "PRELIMINARY" });
        }
        catch (error) {
            estimates.push({ to: t.to, status: "INCOMPLETE", reason: error.message });
        }
    }
    return { bindingGate: "NOT RUN — Batch 04 FS-6", atomicBatch: false, estimates };
}
