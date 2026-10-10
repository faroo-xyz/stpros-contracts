import { createHash } from "node:crypto";
import { readFileSync, lstatSync } from "node:fs";
import { resolve, parse, relative } from "node:path";
import { encodeFunctionData, encodeAbiParameters, parseAbi, keccak256, stringToHex, isAddress } from "viem";
const RECORDED_REGISTRY = JSON.parse(readFileSync(new URL("../../tools/release/registry.json", import.meta.url), "utf8"));
export const ZERO = "0x0000000000000000000000000000000000000000";
export const ZERO32 = `0x${"00".repeat(32)}`;
export const ROLES = { admin: ZERO32, proposer: keccak256(stringToHex("PROPOSER_ROLE")), executor: keccak256(stringToHex("EXECUTOR_ROLE")), canceller: keccak256(stringToHex("CANCELLER_ROLE")), operator: keccak256(stringToHex("OPERATOR_ROLE")) };
export const SLOTS = { admin: `0x${(BigInt(keccak256(stringToHex("eip1967.proxy.admin"))) - 1n).toString(16).padStart(64, "0")}`, implementation: "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", initialized: "0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00" };
const ABI = parseAbi([
    "function transferOwnership(address)", "function acceptOwnership()", "function grantRole(bytes32,address)", "function revokeRole(bytes32,address)", "function renounceRole(bytes32,address)",
    "function upgradeAndCall(address,address,bytes) payable", "function upgradeBeaconTo(address)", "function retireVault(address)", "function closeCreation()", "function emergencyCancel(address)",
    "function scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)", "function executeBatch(address[],uint256[],bytes[],bytes32,bytes32) payable", "function cancel(bytes32)",
    "function migrateFromV1((address reporter,address vToken,uint256 maxUpdateAmount,uint256 updateInterval,uint256 maxAprPpm,address commissionAccount,uint256 commissionRatePpm,address guardian,address timelock))",
    "function migrateFromV3(address,uint256,address,address)", "function migrateFromV2(address,address)", "function migrateToR4(address,address)",
]);
const ST_MIGRATE = parseAbi(["function migrateFromV1(address,address,address,address)"]);
export function fail(code, detail = "") { throw new Error(`${code}${detail ? `: ${detail}` : ""}`); }
export function requireThat(ok, code, detail = "") { if (!ok)
    fail(code, detail); }
export function addr(a, label = "address") { requireThat(typeof a === "string" && a === a.toLowerCase() && isAddress(a) && a.toLowerCase() !== ZERO, "ADDRESS", label); return a.toLowerCase(); }
export function canonical(value) {
    if (typeof value === "bigint")
        return JSON.stringify(value.toString());
    if (Array.isArray(value))
        return `[${value.map(canonical).join(",")}]`;
    if (value && typeof value === "object")
        return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
    return JSON.stringify(value);
}
export function sha256(data) { return createHash("sha256").update(data).digest("hex"); }
export function checkedJson(path, expectedHash) {
    const absolute = resolve(path);
    let cursor = parse(absolute).root;
    for (const part of relative(cursor, absolute).split("/")) {
        requireThat(!part.startsWith(".") && !/(^keys?$|credentials?|secrets?|keystores?|private[-_]?keys?|id_rsa|id_ed25519)/i.test(part), "UNSAFE_INPUT_PATH");
        cursor = resolve(cursor, part);
        requireThat(!lstatSync(cursor).isSymbolicLink(), "SYMLINK_INPUT");
    }
    requireThat(absolute.endsWith(".json"), "JSON_INPUT_REQUIRED");
    const bytes = readFileSync(absolute);
    if (expectedHash)
        requireThat(sha256(bytes) === expectedHash, "REVIEWED_FILE_HASH", absolute);
    return JSON.parse(bytes.toString("utf8"));
}
export function verifyRuntime(actual, reviewedArtifact, binding) {
    requireThat(typeof actual === "string" && actual !== "0x", "NO_CODE");
    let code = reviewedArtifact.deployedBytecode;
    requireThat(typeof code === "string" && /^0x[0-9a-fA-F]+$/.test(code), "REVIEWED_ARTIFACT_RUNTIME");
    const refs = Object.values(reviewedArtifact.immutableReferences ?? {}).flat();
    if (binding) {
        requireThat(refs.length > 0, "IMMUTABLE_REFERENCES_MISSING");
        requireThat(Object.keys(reviewedArtifact.immutableReferences).length === 1, "UNEXPECTED_IMMUTABLES");
        const word = addr(binding).slice(2).padStart(64, "0");
        for (const ref of refs) {
            requireThat(ref.length === 32, "IMMUTABLE_WIDTH");
            const offset = 2 + ref.start * 2;
            requireThat(offset + 64 <= code.length, "IMMUTABLE_OFFSET");
            code = code.slice(0, offset) + word + code.slice(offset + 64);
        }
    }
    else
        requireThat(refs.length === 0, "BINDING_REQUIRED");
    const expected = keccak256(code);
    requireThat(keccak256(actual) === expected, "RUNTIME_CODEHASH");
    return expected;
}
export function operation(transactions, predecessor = ZERO32, salt) {
    requireThat(transactions.length > 0 && /^0x[0-9a-f]{64}$/i.test(salt), "OPERATION_INPUT");
    const targets = transactions.map(t => t.to), values = transactions.map(t => BigInt(t.value)), payloads = transactions.map(t => t.data);
    const id = keccak256(encodeAbiParameters([{ type: "address[]" }, { type: "uint256[]" }, { type: "bytes[]" }, { type: "bytes32" }, { type: "bytes32" }], [targets, values, payloads, predecessor, salt]));
    return { targets, values, payloads, predecessor, salt, id };
}
export function verifyOperation(op, txs, releaseId) {
    const expected = operation(txs, ZERO32, keccak256(stringToHex(releaseId)));
    requireThat(canonical(op) === canonical(expected), "OPERATION_MISMATCH");
    return expected;
}
function tx(to, name, args = [], note = "") { return { to: addr(to), value: "0", data: encodeFunctionData({ abi: ABI, functionName: name, args }), note }; }
export function exactSet(a, b, code) { requireThat(a.length === new Set(a).size && canonical([...a].sort()) === canonical([...b].sort()), code); }
function roleArray(rows) { return rows.map(x => ({ target: addr(x.target), role: x.role, account: addr(x.account), expected: !!x.expected })); }
export function roleDigest(rows) { return keccak256(encodeAbiParameters([{ type: "tuple[]", components: [{ name: "target", type: "address" }, { name: "role", type: "bytes32" }, { name: "account", type: "address" }, { name: "expected", type: "bool" }] }], [roleArray(rows)])); }
/// Input inventory is the independently reviewed complete event-replay artifact, not a caller list.
export function validateInventory(c, inventory, approvedSha256) {
    requireThat(approvedSha256 && sha256(canonical(inventory)) === approvedSha256, "INVENTORY_HASH");
    requireThat(inventory.complete === true && inventory.chainId === c.chainId && inventory.blockHash === c.observed.blockHash, "INVENTORY_COVERAGE");
    requireThat(Number.isSafeInteger(inventory.fromBlock) && inventory.fromBlock > 0 && inventory.startCodeAbsent === true && inventory.toBlock === c.observed.blockNumber, "INVENTORY_RANGE");
    let next = inventory.fromBlock;
    for (const w of inventory.windows ?? []) {
        requireThat(w.from === next && w.to >= w.from && w.to - w.from < 1000, "INVENTORY_GAP");
        next = w.to + 1;
    }
    requireThat(next === inventory.toBlock + 1, "INVENTORY_GAP");
    const targets = [c.addresses.timelock, ...c.factories.flatMap(f => f.vaults.map(v => v.address))].map(x => addr(x));
    exactSet(Object.keys(inventory.roles).map(x => addr(x)), targets, "INVENTORY_TARGETS");
    exactSet(Object.keys(inventory.holders).map(x => addr(x)), [c.addresses.stpros, ...c.factories.flatMap(f => f.vaults.map(v => v.address))].map(x => addr(x)), "HOLDER_TARGETS");
    for (const target of targets) {
        const r = inventory.roles[target];
        requireThat(r.status === "PASS" && r.mismatches.length === 0 && r.adminChanges.length === 0, "ROLE_INVENTORY");
        const replay = new Map(), ever = new Set();
        for (const ev of r.events) {
            requireThat(ev.block >= inventory.fromBlock && ev.block <= inventory.toBlock, "ROLE_EVENT_RANGE");
            requireThat(ev.event === "RoleGranted" || ev.event === "RoleRevoked", "ROLE_EVENT");
            const key = `${ev.role}:${addr(ev.account)}`;
            ever.add(key);
            replay.set(key, ev.event === "RoleGranted");
        }
        const live = [...replay].filter(([, value]) => value).map(([key]) => key).sort();
        exactSet(r.current, live, "ROLE_REPLAY");
        exactSet(r.ever, [...ever], "ROLE_TRUNCATION");
    }
    for (const h of Object.values(inventory.holders)) {
        requireThat(h.status === "PASS" && h.mismatches.length === 0, "HOLDER_INVENTORY");
        requireThat(h.accounts.length === new Set(h.accounts.map(x => addr(x.address))).size, "DUPLICATE_HOLDER");
        requireThat(h.accounts.reduce((s, a) => s + BigInt(a.balance), 0n) === BigInt(h.totalSupply), "HOLDER_SUM");
    }
    requireThat(inventory.withdrawals.status === "PASS" && BigInt(inventory.withdrawals.sumPending) === BigInt(inventory.withdrawals.queued) - BigInt(inventory.withdrawals.completed), "QUEUE_INVENTORY");
    return inventory;
}
export function validateConfig(c) {
    requireThat(c && (c.mode === "simulation" || c.mode === "release"), "MODE");
    requireThat(c.chainId === 1672 || c.chainId === 688689, "CHAIN");
    requireThat(c.releaseId && typeof c.releaseId === "string" && !c.split, "NO_DEFAULT_SPLIT");
    for (const name of ["gov", "can", "guardian", "ops", "deployer", "treasury", "reporter", "keeper", "legacyCoreOwner", "legacyFactoryOwner", "legacyTestVaultAdmin"])
        addr(c.actors?.[name], name);
    for (const name of ["timelock", "proxyAdmin", "assertions", "oracle", "stpros", "bridge", "distributor", "asset"])
        addr(c.addresses?.[name], name);
    for (const name of ["oracle", "stpros", "bridge", "distributor", "factory", "vault"])
        addr(c.implementations?.[name], name);
    const a = c.actors;
    requireThat(![a.gov, a.can, a.guardian, a.ops, c.addresses.timelock].includes(a.deployer), "DEPLOYER_AUTHORITY");
    requireThat(a.gov !== a.can && a.gov !== a.guardian && a.can !== a.guardian && a.ops !== c.addresses.timelock && a.ops !== a.gov && a.ops !== a.can, "ACTOR_SEPARATION");
    // V3 preserves its existing reporter; the configured replacement is not used.
    const reporter = addr(c.chainId === 1672 ? a.reporter : c.observed?.core?.reporter, "effective reporter");
    for (const operational of [reporter, a.keeper])
        requireThat(![a.gov, a.can, c.addresses.timelock].includes(operational), "OPERATIONAL_TIMELOCK_AUTHORITY");
    requireThat(Array.isArray(c.destinations), "DESTINATIONS_REQUIRED");
    for (const destination of c.destinations)
        addr(destination);
    requireThat(c.policy?.minDelay === (c.chainId === 1672 ? 172800 : 300), "POLICY_DELAY");
    requireThat(c.policy.expectedGrossAprPpm !== undefined && BigInt(c.policy.maxAprPpm) === 2n * BigInt(c.policy.expectedGrossAprPpm) && BigInt(c.policy.maxAprPpm) <= 500000n, "APR_POLICY");
    requireThat(BigInt(c.policy.maxUpdateAmount) > 0n && BigInt(c.policy.updateInterval) >= 3600n, "REPORTING_LIMITS");
    requireThat(c.policy.approvedPerformanceFeeBps === 0, "FEE_POLICY");
    if (c.chainId === 1672)
        requireThat(c.policy.commissionRatePpm === 100000, "COMMISSION_POLICY");
    requireThat(c.factories?.length === (c.chainId === 1672 ? 2 : 1), "FACTORY_COUNT");
    const vaults = c.factories.flatMap(f => f.vaults);
    requireThat(vaults.length === (c.chainId === 1672 ? 2 : 15), "VAULT_COUNT");
    exactSet(vaults.map(v => addr(v.address)), vaults.map(v => addr(v.address)), "DUPLICATE_VAULT");
    for (const [i, f] of c.factories.entries()) {
        addr(f.address);
        addr(f.beacon);
        addr(f.legacyOwner);
        requireThat(c.chainId !== 1672 || f.vaults.length === 1, "MAINNET_VAULT_COUNT");
        for (const [j, v] of f.vaults.entries()) {
            addr(v.admin);
            const governed = c.chainId === 1672 ? i === 0 : j === 14 && !c.testnetRecovery?.e2Unavailable;
            requireThat(v.endState === (governed ? "GOVERNED" : "RETIRED"), "VAULT_RECONCILIATION");
        }
    }
    requireThat(["A", "B", "C", "D"].includes(c.p1Case ?? "A"), "FALLBACK_CASE");
    if ((c.p1Case ?? "A") !== "A")
        requireThat(c.fallbackApproval?.ownerApproved === true && c.fallbackApproval.reference, "FALLBACK_OWNER_APPROVAL");
    for (const key of ["e2Unavailable", "incompleteT3"]) if (c.testnetRecovery?.[key] !== undefined) requireThat(typeof c.testnetRecovery[key] === "boolean", "RECOVERY_FLAG");
    if (isTestnetRecovery(c))
        requireThat(c.chainId === 688689 && c.testnetRecovery.ownerApproval, "TESTNET_RECOVERY_APPROVAL");
    requireThat(c.observed?.blockHash && Number.isSafeInteger(c.observed.blockNumber), "PIN_REQUIRED");
    if (c.chainId === 1672) requireThat(c.observed.vaults?.[c.factories[0].vaults[0].address]?.phase === 3, "MAIN_VAULT_PHASE");
    requireThat(c.review?.artifactSha256 && c.review.inventorySha256 && c.review.sourceCommit, "REVIEW_REQUIRED");
    if (c.mode === "release") {
        requireThat(c.review.approvalReference && c.releaseGates?.independentReview === "CLEAR" && c.releaseGates?.freshFork === "PASS" && c.releaseGates?.fs6 === "PASS", "RELEASE_GATES_INCOMPLETE");
        const gates = c.releaseGates, cap = gates.confirmedGasCap ?? 2200000;
        requireThat(Number.isSafeInteger(cap) && cap >= 2200000 && (cap === 2200000 || gates.gasCapApproval), "GAS_CAP_APPROVAL");
        requireThat(Number.isSafeInteger(gates.hGas) && Number.isSafeInteger(gates.uGas) && gates.hGas > 0 && gates.uGas > 0 && gates.hGas <= cap && gates.uGas <= cap, "FS6_GAS_GATE");
        requireThat(gates.forkReference && gates.forkBlockHash === c.observed.blockHash && Number.isSafeInteger(c.observed.timestamp) && Number.isSafeInteger(gates.forkTimestamp) && gates.forkTimestamp <= c.observed.timestamp && c.observed.timestamp - gates.forkTimestamp <= 3600, "FRESH_FORK_EVIDENCE");
        requireThat(c.releaseGates.counterpartyConsent === true && c.releaseGates.dappReady === true, "EXTERNAL_GATES_INCOMPLETE");
        const safeByAddress = new Map();
        for (const role of ["gov", "can", "guardian", "ops", "treasury"]) {
            const safe = c.review.safes?.[role];
            requireThat(safe && safe.address === a[role] && safe.runtimeHash && safe.singleton && safe.singletonHash && safe.nonce !== undefined && safe.owners.length >= safe.threshold && safe.threshold > 0 && safe.modules.length === 0 && safe.guard === ZERO, "SAFE_CONFIGURATION", role);
            requireThat(!safeByAddress.has(safe.address) || canonical(safeByAddress.get(safe.address)) === canonical(safe), "SAFE_ALIAS_CONFIGURATION");
            safeByAddress.set(safe.address, safe);
            for (const owner of safe.owners)
                addr(owner, "Safe owner");
            exactSet(safe.owners, safe.owners, "DUPLICATE_SAFE_OWNER");
            requireThat(!safe.owners.includes(a.deployer), "DEPLOYER_SIGNER", role);
        }
        if (a.ops === a.guardian) requireThat(canonical(c.review.safes.ops) === canonical(c.review.safes.guardian), "SAFE_ALIAS_CONFIGURATION");
        for (const role of ["gov", "can"]) requireThat(!c.review.safes[role].owners.some(owner => [reporter, a.keeper].includes(owner)), "OPERATIONAL_SAFE_SIGNER", role);
        requireThat(c.review.safes.gov.threshold === 3 && c.review.safes.gov.owners.length === 5, "GOV_THRESHOLD");
        requireThat(c.review.safes.can.threshold >= 3, "CAN_THRESHOLD");
        for (const [x, y] of [["gov", "can"], ["gov", "guardian"], ["can", "guardian"], ["ops", "gov"], ["ops", "can"]])
            requireThat(!c.review.safes[x].owners.some(v => c.review.safes[y].owners.includes(v)), "SAFE_SIGNER_OVERLAP");
    }
    return c;
}
/// Bind release targets to the already reviewed public legacy registry. Simulations are separate.
export function validateRecordedTopology(c, registry) {
    if (c.mode !== "release")
        return;
    requireThat(c.chainId === 1672 || c.chainId === 688689, "CHAIN");
    const recorded = registry[c.chainId === 1672 ? "mainnet" : "testnet"];
    const targetByName = Object.fromEntries(Object.entries(recorded.expected_proxies).map(([proxy, entry]) => [entry.name, proxy]));
    const required = { oracle: "Oracle", stpros: "StPROS" };
    if (c.chainId === 688689)
        Object.assign(required, { bridge: "BridgeVault", distributor: "SlpDistributor" });
    for (const [field, name] of Object.entries(required))
        requireThat(c.addresses[field] === targetByName[name], "RECORDED_TARGET", field);
    requireThat(c.addresses.proxyAdmin === recorded.proxy_admin && c.addresses.stpros === recorded.stpros && c.addresses.asset === recorded.wpros, "RECORDED_CORE_TARGETS");
    requireThat(canonical(c.factories.map(f => f.address)) === canonical(recorded.factories), "RECORDED_FACTORIES");
    const used = [c.addresses.oracle, c.addresses.stpros, c.addresses.bridge, c.addresses.distributor, ...c.factories.map(f => f.address)];
    exactSet(Object.keys(c.legacy), used, "RECORDED_PROXY_COVERAGE");
    for (const [proxy, entry] of Object.entries(recorded.expected_proxies))
        requireThat(used.includes(proxy) && c.legacy[proxy]?.implementation === entry.implementation && c.legacy[proxy]?.version === entry.version, "RECORDED_LEGACY_IMPLEMENTATION", proxy);
    for (const f of c.factories) {
        const b = recorded.expected_beacons[f.beacon];
        requireThat(b?.owner === f.address && b?.implementation === f.legacyImplementation && f.vaults.length === recorded.expected_vault_count[f.address], "RECORDED_BEACON", f.address);
    }
    if (c.chainId === 1672)
        for (const field of ["bridge", "distributor"]) {
            const d = c.review.freshDeployments?.[field], proxy = c.addresses[field];
            requireThat(d && d.chainId === 1672 && d.proxy === proxy && d.implementation === c.implementations[field] && d.proxyAdmin === c.addresses.proxyAdmin && d.owner === c.addresses.timelock && d.guardian === c.actors.guardian && d.version === 4 && d.approvalReference, "REVIEWED_FRESH_DEPLOYMENT", field);
            requireThat(!Object.hasOwn(recorded.expected_proxies, proxy) && c.legacy[proxy]?.implementation === d.implementation && c.legacy[proxy]?.version === 4, "FRESH_NOT_LEGACY", field);
        }
}
function artifactHash(artifact) { return sha256(canonical(artifact)); }
export function preflight(c, inventory, artifacts) {
    validateConfig(c);
    validateRecordedTopology(c, RECORDED_REGISTRY);
    validateInventory(c, inventory, c.review.inventorySha256);
    const o = c.observed;
    for (const name of ["oracle", "stpros", "bridge", "distributor", "factory", "vault", "timelock", "assertions", "proxyAdmin"]) {
        requireThat(artifacts[name] && c.review.artifactSha256[name] === artifactHash(artifacts[name]), "ARTIFACT_REVIEW_HASH", name);
        const address = c.implementations[name] ?? c.addresses[name];
        verifyRuntime(o.code[addr(address)], artifacts[name], name === "factory" ? c.implementations.vault : undefined);
    }
    const proxies = [c.addresses.oracle, c.addresses.stpros, c.addresses.bridge, c.addresses.distributor, ...c.factories.map(f => f.address)];
    exactSet(Object.keys(o.proxies), proxies, "PROXY_COVERAGE");
    for (const proxy of proxies) {
        const state = o.proxies[addr(proxy)];
        requireThat(state && state.admin === c.addresses.proxyAdmin, "PROXY_ADMIN", proxy);
        const expectedVersion = proxy === c.addresses.oracle ? (c.chainId === 1672 ? 1 : 3) : proxy === c.addresses.stpros ? (c.chainId === 1672 ? 1 : 2) : c.chainId === 1672 && [c.addresses.bridge, c.addresses.distributor].includes(proxy) ? 4 : 1;
        requireThat(state.version === expectedVersion, "UNAPPROVED_INITIALIZER_VERSION");
        requireThat(state.implementation === c.legacy[addr(proxy)].implementation && state.version === c.legacy[addr(proxy)].version && !state.initializing, "LEGACY_VERSION_OR_IDENTITY", proxy);
        requireThat(keccak256(o.code[state.implementation]) === c.legacy[addr(proxy)].runtimeHash, "LEGACY_CODEHASH", proxy);
    }
    for (const f of c.factories) {
        const state = o.factories[f.address];
        requireThat(state && state.beacon === f.beacon && state.beaconOwner === f.address && state.implementation === f.legacyImplementation, "BEACON_IDENTITY");
        requireThat(keccak256(o.code[state.implementation]) === f.legacyRuntimeHash, "BEACON_CODEHASH");
        exactSet(state.vaults, f.vaults.map(v => v.address), "VAULT_DISCOVERY");
    }
    requireThat([c.actors.legacyCoreOwner, c.addresses.timelock].includes(o.proxyAdminOwner), "UNEXPECTED_PA_OWNER");
    for (const [proxy, state] of Object.entries(o.proxies)) {
        const f = c.factories.find(f => f.address === proxy);
        const fresh = c.chainId === 1672 && [c.addresses.bridge, c.addresses.distributor].includes(proxy);
        requireThat([fresh ? c.addresses.timelock : f ? f.legacyOwner : c.actors.legacyCoreOwner, c.addresses.timelock].includes(state.owner), "UNEXPECTED_OWNER", proxy);
        requireThat(state.paused === false || (proxy === c.addresses.distributor && state.version !== 4 && state.paused === null), "PAUSED_OR_UNKNOWN", proxy);
        requireThat(c.legacy[proxy].proxyRuntimeHash && keccak256(o.code[proxy]) === c.legacy[proxy].proxyRuntimeHash, "PROXY_RUNTIME", proxy);
    }
    for (const [name, artifactName] of [["bridge", "bridge"], ["distributor", "distributor"]])
        if (c.chainId === 1672) {
            const p = o.proxies[c.addresses[name]];
            requireThat(p.implementation === c.implementations[artifactName] && p.version === 4, "FRESH_CORE_IDENTITY");
        }
    if (c.chainId === 1672)
        requireThat(c.actors.legacyCoreOwner === c.actors.gov && c.factories[0].vaults[0].admin === c.actors.gov, "MAINNET_SAFE_ACTOR");
    for (const f of c.factories)
        for (const v of f.vaults) {
            requireThat(o.vaults[v.address]?.factory === f.address && o.vaults[v.address]?.asset === c.addresses.stpros, "VAULT_CANONICAL");
            requireThat(keccak256(o.code[v.address]) === v.proxyRuntimeHash, "VAULT_PROXY_RUNTIME");
        }
    const t = o.timelock;
    requireThat(t.minDelay === c.policy.minDelay, "TIMELOCK_DELAY");
    const wanted = [`${ROLES.admin}:${c.addresses.timelock}`, `${ROLES.proposer}:${c.actors.gov}`, `${ROLES.executor}:${c.actors.gov}`, `${ROLES.canceller}:${c.actors.gov}`, `${ROLES.canceller}:${c.actors.can}`];
    exactSet(inventory.roles[c.addresses.timelock].current, wanted, "TIMELOCK_ROLES");
    requireThat(o.core.canonical === c.addresses.stpros && o.core.asset === c.addresses.asset, "CANONICAL_PAIR");
    if (c.chainId === 1672) {
        requireThat(inventory.roles[c.factories[0].vaults[0].address].current.includes(`${ROLES.admin}:${c.actors.gov}`), "MISSING_H_ADMIN");
        const x = o.core;
        requireThat(BigInt(x.reserve) === BigInt(x.queued) - BigInt(x.completed) && BigInt(x.escrow) >= BigInt(x.reserve) && BigInt(x.wrapped) - BigInt(x.reserve) >= BigInt(x.supply) - BigInt(x.escrow), "V1_BACKING");
        requireThat(BigInt(x.poolT) === 0n && BigInt(x.poolV) === 0n, "V1_POOL");
        requireThat(o.vaults[c.factories[0].vaults[0].address].phase === 3, "MAIN_VAULT_PHASE");
    }
    return { status: "PASS", scope: "reviewed-input validation; RPC recheck required for release output", deployable: c.mode === "release", simulationOnly: c.mode === "simulation", block: o.blockNumber, blockHash: o.blockHash };
}
export function isTestnetRecovery(c) { return c.testnetRecovery?.e2Unavailable === true || c.testnetRecovery?.incompleteT3 === true; }
export function residualVault(c, v) { return v.endState === "RETIRED" && ((c.chainId === 1672 && ["C", "D"].includes(c.p1Case)) || c.testnetRecovery?.e2Unavailable); }
function allRoleKeys(inventory, target) { return [...new Set([...inventory.roles[target].ever, `${ROLES.admin}:${ZERO}`])].filter(k => !k.endsWith(`:${ZERO}`)); }
function cleanup(inventory, vault, keep, renouncer) {
    const out = [];
    for (const key of inventory.roles[vault].current) {
        const role = key.slice(0, 66), account = key.slice(67);
        if (!((account === keep || account === renouncer) && role === ROLES.admin))
            out.push(tx(vault, "revokeRole", [role, account], "complete-inventory revocation"));
    }
    if (renouncer)
        out.push(tx(vault, "renounceRole", [ROLES.admin, renouncer], "legacy admin renounces last"));
    return out;
}
export function expectations(c, inventory, artifacts, stage) {
    const post = stage === "R4", a = c.actors, d = c.addresses, roles = [];
    const add = (target, role, account, expected) => { if (!roles.some(r => r.target === target && r.role === role && r.account === account))
        roles.push({ target, role, account, expected }); };
    for (const key of inventory.roles[d.timelock].ever) {
        const role = key.slice(0, 66), account = key.slice(67);
        add(d.timelock, role, account, inventory.roles[d.timelock].current.includes(key));
    }
    for (const f of c.factories)
        for (const v of f.vaults) {
            // Approved stale roles become inert after retirement; do not give a stale admin a migration veto.
            if (residualVault(c, v))
                continue;
            const pairs = allRoleKeys(inventory, v.address);
            for (const key of pairs) {
                const role = key.slice(0, 66), account = key.slice(67);
                add(v.address, role, account, (v.endState === "RETIRED" && ((["C", "D"].includes(c.p1Case) && c.chainId === 1672) || c.testnetRecovery?.e2Unavailable) && inventory.roles[v.address].current.includes(key)) || v.endState === "GOVERNED" && ((role === ROLES.admin && account === d.timelock) || (post && role === ROLES.operator && account === a.ops)));
            }
            add(v.address, ROLES.admin, d.timelock, v.endState === "GOVERNED");
            add(v.address, ROLES.operator, a.ops, post && v.endState === "GOVERNED");
        }
    const proxyRows = [[d.oracle, "oracle"], [d.stpros, "stpros"], [d.bridge, "bridge"], [d.distributor, "distributor"], ...c.factories.map(f => [f.address, "factory"])];
    const proxies = proxyRows.map(([proxy, name]) => {
        const guarded = post || (c.chainId === 1672 && (["bridge", "distributor"].includes(name) || (["B", "D"].includes(c.p1Case) && proxy === c.factories[1].address)));
        const implementation = guarded ? c.implementations[name] : c.legacy[proxy].implementation;
        const implementationHash = guarded ? verifyRuntime(c.observed.code[implementation], artifacts[name], name === "factory" ? c.implementations.vault : undefined) : c.legacy[proxy].runtimeHash;
        return { proxy, owner: !post && ((c.testnetRecovery?.e2Unavailable && name === "factory") || (c.testnetRecovery?.incompleteT3 && name !== "factory")) ? c.observed.proxies[proxy].owner : d.timelock, implementation, implementationHash, guarded, paused: false };
    });
    const beacons = c.factories.map(f => ({ factory: f.address, beacon: f.beacon, implementation: post ? c.implementations.vault : f.legacyImplementation, implementationHash: post ? keccak256(artifacts.vault.deployedBytecode) : f.legacyRuntimeHash, hardened: post || (c.chainId === 1672 && f === c.factories[1] && ["B", "D"].includes(c.p1Case)), creationClosed: post && c.chainId === 1672 && f === c.factories[1] }));
    const vaults = c.factories.flatMap(f => f.vaults.map(v => ({ vault: v.address, factory: f.address, round: BigInt(c.observed.vaults[v.address].round), termsHash: c.observed.vaults[v.address].termsHash, phase: (post && v.endState === "RETIRED") || (!post && c.chainId === 1672 && v.endState === "RETIRED" && ["A", "C"].includes(c.p1Case ?? "A")) ? 3 : c.observed.vaults[v.address].phase, hardened: post, retired: post && v.endState === "RETIRED", governed: v.endState === "GOVERNED", residualRecovery: !!residualVault(c, v) })));
    return { chainId: BigInt(c.chainId), timelock: d.timelock, timelockHash: keccak256(artifacts.timelock.deployedBytecode), minDelay: BigInt(c.policy.minDelay), gov: a.gov, can: a.can, guardian: a.guardian, ops: a.ops, deployer: a.deployer, proxyAdmin: d.proxyAdmin, proxyAdminHash: keccak256(artifacts.proxyAdmin.deployedBytecode), inventoryHash: roleDigest(roles), roles, proxies, beacons, vaults };
}
export function coreExpectation(c) { const d = c.addresses, a = c.actors, x = c.observed.core; return { oracle: d.oracle, stpros: d.stpros, bridge: d.bridge, distributor: d.distributor, asset: d.asset, reporter: c.chainId === 1672 ? a.reporter : x.reporter, treasury: c.chainId === 1672 ? a.treasury : x.treasury, keeper: a.keeper, commissionPpm: BigInt(c.chainId === 1672 ? 100000 : x.commissionPpm), aprPpm: BigInt(c.policy.maxAprPpm), maxUpdateAmount: BigInt(c.chainId === 1672 ? c.policy.maxUpdateAmount : x.maxUpdateAmount), interval: BigInt(c.chainId === 1672 ? c.policy.updateInterval : Math.max(x.interval, 3600)), unbondingPeriod: BigInt(x.unbondingPeriod), maxWithdrawCount: BigInt(x.maxWithdrawCount), mainnetV1: c.chainId === 1672, destinations: c.destinations }; }
export function buildU(c, inventory, artifacts) {
    preflight(c, inventory, artifacts);
    const d = c.addresses, a = c.actors, i = c.implementations, txs = [];
    const upgrade = (proxy, impl, data) => txs.push(tx(d.proxyAdmin, "upgradeAndCall", [proxy, impl, data]));
    const om = c.chainId === 1672 ? encodeFunctionData({ abi: ABI, functionName: "migrateFromV1", args: [{ reporter: a.reporter, vToken: d.stpros, maxUpdateAmount: BigInt(c.policy.maxUpdateAmount), updateInterval: BigInt(c.policy.updateInterval), maxAprPpm: BigInt(c.policy.maxAprPpm), commissionAccount: a.treasury, commissionRatePpm: 100000n, guardian: a.guardian, timelock: d.timelock }] }) : encodeFunctionData({ abi: ABI, functionName: "migrateFromV3", args: [d.asset, BigInt(c.policy.maxAprPpm), a.guardian, d.timelock] });
    upgrade(d.oracle, i.oracle, om);
    upgrade(d.stpros, i.stpros, c.chainId === 1672 ? encodeFunctionData({ abi: ST_MIGRATE, functionName: "migrateFromV1", args: [d.distributor, d.bridge, a.guardian, d.timelock] }) : encodeFunctionData({ abi: ABI, functionName: "migrateFromV2", args: [a.guardian, d.timelock] }));
    if (c.chainId === 688689)
        for (const n of ["bridge", "distributor"])
            upgrade(d[n], i[n], encodeFunctionData({ abi: ABI, functionName: "migrateToR4", args: [a.guardian, d.timelock] }));
    for (const [index, f] of c.factories.entries()) {
        if (!(c.chainId === 1672 && index === 1 && ["B", "D"].includes(c.p1Case)))
            upgrade(f.address, i.factory, encodeFunctionData({ abi: ABI, functionName: "migrateToR4", args: [a.guardian, d.timelock] }));
        txs.push(tx(f.address, "upgradeBeaconTo", [i.vault]));
        for (const v of f.vaults)
            txs.push(v.endState === "GOVERNED" ? tx(v.address, "grantRole", [ROLES.operator, a.ops]) : tx(f.address, "retireVault", [v.address]));
        if (c.chainId === 1672 && index === 1)
            txs.push(tx(f.address, "closeCreation"));
    }
    txs.push({ to: d.assertions, value: "0", data: encodeFunctionData({ abi: artifacts.assertions.abi, functionName: "assertR4", args: [expectations(c, inventory, artifacts, "R4"), coreExpectation(c)] }), note: "on-chain subset; initializer/inventory completeness remain off-chain" });
    const op = operation(txs, ZERO32, keccak256(stringToHex(c.releaseId)));
    return { transactions: txs, operation: op, execute: tx(d.timelock, "executeBatch", [op.targets, op.values, op.payloads, op.predecessor, op.salt]), cancel: tx(d.timelock, "cancel", [op.id]), simulationOnly: c.mode === "simulation", deployable: c.mode === "release" };
}
export function requireP1Complete(c, inventory) {
    if (c.chainId !== 1672 || c.mode !== "release")
        return;
    const f = c.factories[1], v = f.vaults[0], k = c.p1Case ?? "A";
    if (["A", "C"].includes(k))
        requireThat(c.observed.proxies[f.address].owner === c.addresses.timelock && c.observed.vaults[v.address].phase === 3, "P1_OWNER_OR_PHASE_INCOMPLETE");
    if (["A", "B"].includes(k))
        requireThat(inventory.roles[v.address].current.length === 0, "P1_ROLES_INCOMPLETE");
}
export function buildHandoff(c, inventory, artifacts) {
    requireP1Complete(c, inventory);
    requireThat(c.observed.proxyAdminOwner === c.actors.legacyCoreOwner, "H_ALREADY_OR_PARTIALLY_DONE");
    for (const proxy of [c.addresses.oracle, c.addresses.stpros, c.factories[0].address])
        requireThat(c.observed.proxies[proxy].owner === c.actors.legacyCoreOwner, "H_CORE_OWNER_STATE");
    requireThat(c.chainId === 1672, "MAINNET_ONLY");
    const u = buildU(c, inventory, artifacts), d = c.addresses, a = c.actors;
    const txs = [];
    if (["B", "D"].includes(c.p1Case))
        txs.push(tx(d.proxyAdmin, "upgradeAndCall", [c.factories[1].address, c.implementations.factory, encodeFunctionData({ abi: ABI, functionName: "migrateToR4", args: [a.guardian, d.timelock] })], "owner-approved fallback item 0"));
    txs.push(tx(d.proxyAdmin, "transferOwnership", [d.timelock]), ...[d.stpros, d.oracle, c.factories[0].address].map(x => tx(x, "transferOwnership", [d.timelock])));
    const v = c.factories[0].vaults[0];
    txs.push(tx(v.address, "grantRole", [ROLES.admin, d.timelock]), ...cleanup(inventory, v.address, d.timelock, a.legacyCoreOwner));
    const op = u.operation;
    txs.push(tx(d.timelock, "scheduleBatch", [op.targets, op.values, op.payloads, op.predecessor, op.salt, BigInt(c.policy.minDelay)]));
    txs.push({ to: d.assertions, value: "0", data: encodeFunctionData({ abi: artifacts.assertions.abi, functionName: "assertHandoff", args: [expectations(c, inventory, artifacts, "H"), op.id] }) });
    return safeBuilder(c, txs, "Mainnet H — one atomic Safe MultiSend", a.legacyCoreOwner);
}
export function p1Runbook(c, inventory, artifacts) {
    preflight(c, inventory, artifacts);
    requireThat(c.chainId === 1672, "MAINNET_ONLY");
    const f = c.factories[1], v = f.vaults[0], actor = f.legacyOwner, k = c.p1Case ?? "A", calls = [];
    if (["A", "C"].includes(k) && c.observed.proxies[f.address].owner !== c.addresses.timelock) {
        if (c.observed.vaults[v.address].phase !== 3)
            calls.push({ actor, ...tx(f.address, "emergencyCancel", [v.address]) });
    }
    if (["A", "B"].includes(k) && inventory.roles[v.address].current.length) {
        requireThat(inventory.roles[v.address].current.includes(`${ROLES.admin}:${v.admin}`), "MISSING_CLEANUP_ADMIN");
        calls.push(...cleanup(inventory, v.address, undefined, v.admin).map(t => ({ actor: v.admin, ...t })));
    }
    if (["A", "C"].includes(k) && c.observed.proxies[f.address].owner !== c.addresses.timelock)
        calls.push({ actor, ...tx(f.address, "transferOwnership", [c.addresses.timelock]) });
    return { case: k, simulationOnly: c.mode === "simulation", deployable: c.mode === "release", steps: calls, residual: ["C", "D"].includes(k) ? "Owner-accepted stale test-vault authority remains until U retirement. Case C: notify holders to redeem while SETTLED." : "No legacy authority after H", recovery: "Resume only from a freshly reviewed state. Factory owner unavailable or wrong: owner-approved B/D item 0 before PA handoff; never change proxy admin." };
}
export function testnetHandoff(c, inventory, artifacts) {
    requireThat(c.chainId === 688689, "TESTNET_ONLY");
    const u = buildU(c, inventory, artifacts), d = c.addresses, a = c.actors, f = c.factories[0];
    const t1 = [];
    for (const v of f.vaults) {
        if (c.testnetRecovery?.e2Unavailable)
            continue;
        if (!inventory.roles[v.address].current.includes(`${ROLES.admin}:${v.admin}`)) {
            requireThat(inventory.roles[v.address].current.every(k => k === `${ROLES.admin}:${d.timelock}`) && (v.endState !== "GOVERNED" || inventory.roles[v.address].current.includes(`${ROLES.admin}:${d.timelock}`)), "T1_PARTIAL_ROLE_STATE");
            continue;
        }
        t1.push(...cleanup(inventory, v.address, v.admin, undefined).filter(t => !t.note?.includes("renounces")));
        if (v.endState === "GOVERNED")
            t1.push(tx(v.address, "grantRole", [ROLES.admin, d.timelock]));
        t1.push(tx(v.address, "renounceRole", [ROLES.admin, v.admin]));
    }
    const op = u.operation;
    return { simulationOnly: c.mode === "simulation", deployable: c.mode === "release", stages: [{ stage: "T-0", note: "Reviewed deployments, full inventory and 15-vault reconciliation are prerequisites" }, { stage: "T-1", actor: a.legacyFactoryOwner, transactions: t1 }, { stage: "T-2", actor: a.legacyFactoryOwner, transactions: c.testnetRecovery?.e2Unavailable || c.observed.proxies[f.address].owner === d.timelock ? [] : [tx(f.address, "transferOwnership", [d.timelock])] }, { stage: "T-3", actor: a.legacyCoreOwner, transactions: c.testnetRecovery?.incompleteT3 ? [] : [d.stpros, d.oracle, d.bridge, d.distributor].filter(x => c.observed.proxies[x].owner !== d.timelock).map(x => tx(x, "transferOwnership", [d.timelock])) }, { stage: "T-4", actor: a.legacyCoreOwner, transactions: c.observed.proxyAdminOwner === d.timelock ? [] : [tx(d.proxyAdmin, "transferOwnership", [d.timelock])] }, { stage: "T-5", actor: a.gov, transactions: [tx(d.timelock, "scheduleBatch", [op.targets, op.values, op.payloads, ZERO32, op.salt, BigInt(c.policy.minDelay)])], assertion: { to: d.assertions, data: encodeFunctionData({ abi: artifacts.assertions.abi, functionName: isTestnetRecovery(c) ? "assertTestnetRecoveryHandoff" : "assertHandoff", args: [expectations(c, inventory, artifacts, "H"), op.id] }) } }, { stage: "U_t", actor: a.gov, transactions: [u.execute] }] };
}
export function safeBuilder(c, transactions, name, actor) { return { version: "1.0", chainId: String(c.chainId), createdAt: 0, meta: { name: `${c.mode === "simulation" ? "SIMULATION ONLY — " : ""}${name}`, description: "No broadcast. Atomic MultiSend required; verify all inputs immediately before signing.", txBuilderVersion: "1.18.0", createdFromSafeAddress: actor }, transactions: transactions.map(({ to, value, data }) => ({ to, value, data, contractMethod: null, contractInputsValues: null })), simulationOnly: c.mode === "simulation", deployable: c.mode === "release" }; }
export function deploymentDefinitions(c, artifacts) {
    validateConfig(c);
    return { simulationOnly: c.mode === "simulation", execute: false, definitions: [{ contract: "TimelockController", args: [c.policy.minDelay, [c.actors.gov], [c.actors.gov], c.actors.deployer], after: [tx(c.addresses.timelock, "grantRole", [ROLES.canceller, c.actors.can]), tx(c.addresses.timelock, "renounceRole", [ROLES.admin, c.actors.deployer])], mustVerify: "Complete TL role inventory, self-only admin, no deployer authority before any handoff" }, { contract: "YieldVault", args: [], reviewedRuntimeHash: keccak256(artifacts.vault.deployedBytecode) }, { contract: "YieldVaultFactory", args: [c.implementations.vault], mustVerify: "Full runtime and immutable binding before constructor use, schedule and execution" }, { contract: "BridgeVault", args: [], freshProxy: c.chainId === 1672 ? { proxyAdmin: c.addresses.proxyAdmin, initializer: "initialize(address,address,bool,address)", args: [c.addresses.timelock, c.addresses.stpros, true, c.actors.guardian] } : null }, { contract: "SlpDistributor", args: [], freshProxy: c.chainId === 1672 ? { proxyAdmin: c.addresses.proxyAdmin, initializer: "initialize(address,address,address[],address)", args: [c.addresses.timelock, c.actors.keeper, c.destinations, c.actors.guardian] } : null }, { contract: "ReleaseAssertions", args: [], authority: "none" }] };
}
