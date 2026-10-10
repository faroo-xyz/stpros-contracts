import { encodeFunctionData, decodeFunctionResult, encodeAbiParameters, parseAbi, keccak256, stringToHex } from "viem";
import { addr, canonical, ZERO, ZERO32, SLOTS, ROLES } from "./core.mjs";
import { PINNED_TIMELOCK_RUNTIME_HASH } from "./timelock-history.mjs";

export const LOSS_ABI = parseAbi([
  "function proposeSettlement(uint8,uint256,address)", "function setGuardian(address)", "function unpause()",
  "function schedule(address,uint256,bytes,bytes32,bytes32,uint256)",
  "function execute(address,uint256,bytes,bytes32,bytes32) payable",
  "function scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
  "function executeBatch(address[],uint256[],bytes[],bytes32,bytes32) payable", "function cancel(bytes32)",
]);
export const ROUND_FIELDS = ["roundIndex", "openedAt", "openWindow", "openDeadline", "lockDuration", "settleTimelockWindow", "roundCap", "perAddressCap", "minSubscription", "performanceFeeBps", "maxLossBps"];
const READS = parseAbi([
  ...[...ROUND_FIELDS, "SETTLEMENT_GRACE", "settlementDeadline", "totalManagedAssets", "getMinDelay"].map(n => `function ${n}() view returns(uint256)`),
  ...["factory", "asset", "owner", "pendingOwner", "guardian", "timelock", "beacon", "implementation", "counterparty", "feeRecipient"].map(n => `function ${n}() view returns(address)`),
  "function phase() view returns(uint8)", "function paused() view returns(bool)", "function retired() view returns(bool)",
  "function isFactoryVault(address) view returns(bool)", "function getAllProxies() view returns(address[])",
  "function balanceOf(address) view returns(uint256)", "function getTimestamp(bytes32) view returns(uint256)",
  "function hasRole(bytes32,address) view returns(bool)",
]);
const BEACON_SLOT = `0x${(BigInt(keccak256(stringToHex("eip1967.proxy.beacon"))) - 1n).toString(16)}`;
const check = (ok, code) => { if (!ok) throw new Error(`BLOCKED — ${code}`); };
const uint = (v, name) => { check(typeof v === "bigint" || (typeof v === "string" && /^(0|[1-9][0-9]*)$/.test(v)), `UINT_${name}`); const n = BigInt(v); check(n >= 0n && n < 2n ** 256n, `UINT_${name}`); return n; };
const hash = v => typeof v === "string" && /^0x[0-9a-f]{64}$/.test(v);
const tx = (to, functionName, args) => ({ to, value: "0", data: encodeFunctionData({ abi: LOSS_ABI, functionName, args }) });
const identities = ["vault", "factory", "timelock", "asset", "beacon", "factoryImplementation", "vaultImplementation", "proxyAdmin"];

function validatePlan(p, now) {
  check(p.schema === "faroo-loss-contingency-v1" && ["simulation", "release"].includes(p.mode), "PLAN_SCHEMA");
  check(Number.isSafeInteger(p.chainId) && p.chainId > 0, "CHAIN_ID");
  for (const n of [...identities, "guardian", "replacementGuardian", "gov", "can", "operator", "counterparty", "feeRecipient"]) addr(p[n], n);
  check(new Set(identities.map(n => p[n])).size === identities.length, "IDENTITY_ALIAS");
  check(new Set([p.gov,p.can,p.timelock,p.guardian]).size === 4, "AUTHORITY_ALIAS");
  check(![p.gov,p.can,p.timelock,p.counterparty,p.feeRecipient].includes(p.operator), "OPERATOR_ALIAS");
  check(![p.guardian,p.gov,p.can,p.timelock,p.counterparty,p.feeRecipient, ...identities.map(n=>p[n])].includes(p.replacementGuardian), "REPLACEMENT_GUARDIAN");
  check(typeof p.reviewReference === "string" && p.reviewReference.trim().length > 0 && typeof p.marginReviewReference === "string" && p.marginReviewReference.trim().length > 0, "INDEPENDENT_REVIEW_REQUIRED");
  const reviewedAt = uint(p.reviewedAt, "reviewedAt"), expiresAt = uint(p.expiresAt, "expiresAt");
  check(reviewedAt <= now && now <= expiresAt && expiresAt <= reviewedAt + 3600n, "STALE_REVIEW");
  check(uint(p.marginSeconds,"margin") > 0n, "REVIEWED_MARGIN_REQUIRED");
  check(p.predecessor === ZERO32 && hash(p.saltA) && hash(p.saltB) && p.saltA !== p.saltB, "INDEPENDENT_OPERATION_INPUT");
  uint(p.approvedLossAmount,"loss"); uint(p.minDelay,"delay");
  for (const n of ROUND_FIELDS) uint(p.round?.[n],n);
  for (const n of identities) check(hash(p.runtimeHashes?.[n]), `REVIEWED_RUNTIME_${n}`);
}

// Pure construction from independently reviewed inputs and a pinned observation.
// This never certifies signatures, full history, or future runtime preconditions.
export function buildLossContingency(p, s, now) {
  now = uint(now,"now"); validatePlan(p,now);
  check(s.chainId === p.chainId && hash(s.blockHash), "OBSERVATION_CHAIN");
  const observedAt = uint(s.timestamp,"timestamp"); uint(s.blockNumber,"blockNumber");
  check(observedAt <= now + 30n && now <= observedAt + 120n, "STALE_OBSERVATION");
  for (const n of identities) check(s.identities?.[n] === p[n] && s.runtimeHashes?.[n] === p.runtimeHashes[n], `IDENTITY_${n}`);
  check(s.vaultFactory === p.factory && s.isFactoryVault === true && s.allProxies?.includes(p.vault), "CANONICAL_FACTORY_VAULT");
  check(s.factoryOwner === p.timelock && s.factoryTimelock === p.timelock && s.pendingOwner === ZERO && s.beaconOwner === p.factory && s.proxyAdminOwner === p.timelock, "GOVERNANCE_WIRING");
  check(s.guardian === p.guardian && s.counterparty === p.counterparty && s.feeRecipient === p.feeRecipient, "ACTOR_DRIFT");
  check(s.authoritiesValid === true, "AUTHORITY_GETTERS");
  check(canonical(s.round) === canonical(p.round), "ROUND_DRIFT");
  check(s.phase === 1 && s.retired === false, "UNSUPPORTED_PHASE_OR_EXISTING_PROPOSAL");
  check(typeof s.paused === "boolean", "PAUSE_UNKNOWN");
  const delay = uint(s.minDelay,"minDelay");
  check(delay > 0n && delay === uint(p.minDelay,"minDelay"), "TIMELOCK_DELAY_DRIFT");
  if(p.mode === "release") check(delay === (p.chainId === 1672 ? 172800n : 300n) && [1672,688689].includes(p.chainId), "POLICY_DELAY");
  const grace = uint(s.grace,"grace"), window = uint(p.round.settleTimelockWindow,"window");
  check(grace === 2592000n && window > 0n && window <= 1987200n, "ROUND_TIMING");
  const maturity = uint(p.round.openDeadline,"openDeadline") + uint(p.round.lockDuration,"lockDuration");
  const deadline = maturity + grace, cutoff = deadline - window;
  check(deadline === uint(s.settlementDeadline,"D"), "DEADLINE_GETTER");
  const amount = uint(p.approvedLossAmount,"loss"), managed = uint(s.totalManagedAssets,"managed"), bps = uint(p.round.maxLossBps,"bps");
  check(bps <= 5000n && amount <= managed * bps / 10000n, "LOSS_CAP");
  check(uint(s.vaultBalance,"backing") >= managed, "VAULT_BACKING");
  // Future-skewed block time cannot give an optimistic scheduling deadline.
  const scheduleAt = now > observedAt ? now : observedAt;
  const readyAt = scheduleAt + delay, margin = uint(p.marginSeconds,"margin");
  const executionAt = readyAt > maturity ? readyAt : maturity;
  check(executionAt + margin <= cutoff, "LOSS_SCHEDULING_WINDOW_CLOSED");
  const loss = tx(p.vault,"proposeSettlement",[1,amount,ZERO]);
  const paths = {};
  for (const name of ["A","B"]) {
    const calls = name === "A" ? [loss] : [tx(p.factory,"setGuardian",[p.replacementGuardian]),tx(p.factory,"unpause",[]),loss];
    const targets = calls.map(c=>c.to), values = calls.map(()=>0n), payloads = calls.map(c=>c.data), salt = p[`salt${name}`];
    const args = name === "A" ? [p.vault,0n,loss.data,ZERO32,salt] : [targets,values,payloads,ZERO32,salt];
    const types = name === "A" ? ["address","uint256","bytes","bytes32","bytes32"] : ["address[]","uint256[]","bytes[]","bytes32","bytes32"];
    const id = keccak256(encodeAbiParameters(types.map(type=>({type})),args));
    if (p.frozenOperationIds !== undefined) check(p.frozenOperationIds[name] === id, "FROZEN_OPERATION_ID_MISMATCH");
    paths[name] = { kind: name === "A" ? "single" : "batch", id, targets, values: values.map(String), payloads, predecessor: ZERO32, salt, calls,
      schedule: tx(p.timelock,name === "A" ? "schedule" : "scheduleBatch",[...args,delay]),
      execute: tx(p.timelock,name === "A" ? "execute" : "executeBatch",args), cancel: tx(p.timelock,"cancel",[id]),
      executionPreconditions: [name === "A" ? "Factory unpaused" : "Factory paused; reviewed replacement Guardian still approved", "Operation Ready, unchanged identity/round/terms/authority/backing, matured LOCKED round, timestamp <= proposalCutoff", "A Ready timestamp alone is not executability; recheck immediately before execution"] };
  }
  return { schema: p.schema, status: p.mode === "simulation" ? "SIMULATION ONLY" : "UNSIGNED REVIEW ARTIFACT", deployable: false,
    chainId: p.chainId, vault:p.vault, factory:p.factory, timelock:p.timelock, guardian:p.guardian, replacementGuardian:p.replacementGuardian,
    approvedLossAmount: amount.toString(), round: p.round, phase: "LOCKED", factoryPaused:s.paused,
    contractIdentity: {addresses:s.identities,runtimeHashes:s.runtimeHashes},
    authorityInputs: {gov:p.gov,can:p.can,operator:p.operator,counterparty:p.counterparty,feeRecipient:p.feeRecipient},
    accounting: {totalManagedAssets:s.totalManagedAssets,vaultBalance:s.vaultBalance},
    approval: {reviewedAt:p.reviewedAt,expiresAt:p.expiresAt},
    observation: {blockNumber:s.blockNumber,blockHash:s.blockHash,timestamp:s.timestamp}, reviewReference:p.reviewReference,
    timing:{D:deadline.toString(),proposalCutoff:cutoff.toString(),minDelay:delay.toString(),marginSeconds:margin.toString(),marginReviewReference:p.marginReviewReference,
      latestScheduleAt:(cutoff-delay-margin).toString(),earliestReadyIfScheduledNow:readyAt.toString(), earliestExecutionIfScheduledNow:executionAt.toString()}, paths,
    schedulingPreconditions:["Independently approve BOTH exact operations before latestScheduleAt; schedule each with at least current minDelay", "Fresh canonical identity and current getter checks; reconcile ALL action-bearing operations with authoritative GC-03 history", "No existing proposal replacement is supported by this builder; re-review separately"],
    cancellation:["After one path executes, authorized CAN or GOV must cancel the unused pending ID and reconcile selected Done / unused Unset", "Waiting and Ready both remain pending, even when a target currently reverts; never silently rebuild a frozen operation", "Retain full targets, values, payloads, salts, timestamps, execution/cancellation receipts in final GC-03 inventory"],
    evidenceRequirements:["Independent raw plan hash and runtime/proxy/immutable identity review", "Actual Safe identities, signer independence, nonce and execution approvals", "Complete authenticated GC-03 action-bearing history, dependency state and exact operation reconciliation", "Fresh execution simulation and monitoring; this artifact is not scheduling or execution authorization"],
    limitations:["No signatures, scheduling, execution, cancellation, deployment or broadcast performed", "No actual Safe authorization or complete history attested by this builder", "Pause races, OPS pre-emption, state changes and missed cutoff remain possible; if neither reviewed path can execute before cutoff: BLOCKED", "Calldata contains no round index guard: cancel stale unused operations; never reuse in a later round", "Non-LOCKED phases and replacement of existing proposals are unsupported; contract economics unchanged"] };
}

// Reads are pinned to one block; identities are compared to separately approved hashes.
export async function observeLossContingency(p, rpc, registry, now = () => BigInt(Math.floor(Date.now()/1000))) {
  validatePlan(p,now());
  if (p.mode === "release") {
    const r = p.chainId === 1672 ? registry.mainnet : p.chainId === 688689 ? registry.testnet : undefined;
    check(r?.chain_id === p.chainId && r.factories.includes(p.factory) && r.stpros === p.asset && r.proxy_admin === p.proxyAdmin && r.expected_beacons[p.beacon]?.owner === p.factory, "RECORDED_CANONICAL_TOPOLOGY");
    check(p.runtimeHashes.timelock === PINNED_TIMELOCK_RUNTIME_HASH, "PINNED_TIMELOCK_RUNTIME_REQUIRED");
  }
  check(Number(BigInt(await rpc("eth_chainId",[]))) === p.chainId,"RPC_CHAIN");
  const h = await rpc("eth_getBlockByNumber",["latest",false]);
  check(h?.number && h.timestamp && hash(h.hash),"BLOCK_MISSING");
  const call = async (to,functionName,args=[]) => decodeFunctionResult({abi:READS,functionName,data:await rpc("eth_call",[{to,data:encodeFunctionData({abi:READS,functionName,args})},h.number])});
  const slot = async (to,key) => { const v = await rpc("eth_getStorageAt",[to,key,h.number]); check(hash(v) && v.slice(2,26) === "0".repeat(24),"ADDRESS_SLOT"); return `0x${v.slice(-40)}`; };
  const s = {chainId:p.chainId,blockNumber:BigInt(h.number).toString(),blockHash:h.hash,timestamp:BigInt(h.timestamp).toString(),identities:{},runtimeHashes:{},round:{}};
  for(const n of identities) {
    const code = await rpc("eth_getCode",[p[n],h.number]); check(typeof code === "string" && /^0x(?:[0-9a-f]{2})+$/i.test(code),`NO_CODE_${n}`);
    s.runtimeHashes[n] = keccak256(code); s.identities[n] = p[n];
  }
  s.identities.factoryImplementation = await slot(p.factory,SLOTS.implementation);
  s.identities.proxyAdmin = await slot(p.factory,SLOTS.admin);
  s.identities.beacon = await slot(p.vault,BEACON_SLOT);
  check((await call(p.factory,"beacon")).toLowerCase() === p.beacon,"FACTORY_BEACON");
  s.identities.vaultImplementation = (await call(p.beacon,"implementation")).toLowerCase();
  s.identities.asset = (await call(p.vault,"asset")).toLowerCase();
  for(const n of ROUND_FIELDS) s.round[n] = String(await call(p.vault,n));
  for(const [key,target,fn,args] of [
    ["vaultFactory",p.vault,"factory"], ["isFactoryVault",p.factory,"isFactoryVault",[p.vault]], ["allProxies",p.factory,"getAllProxies"],
    ["factoryOwner",p.factory,"owner"],["factoryTimelock",p.factory,"timelock"],["pendingOwner",p.factory,"pendingOwner"],
    ["beaconOwner",p.beacon,"owner"],["proxyAdminOwner",p.proxyAdmin,"owner"],["guardian",p.factory,"guardian"],
    ["counterparty",p.vault,"counterparty"],["feeRecipient",p.vault,"feeRecipient"], ["phase",p.vault,"phase"],["retired",p.vault,"retired"],
    ["paused",p.factory,"paused"],["minDelay",p.timelock,"getMinDelay"],["grace",p.vault,"SETTLEMENT_GRACE"],
    ["settlementDeadline",p.vault,"settlementDeadline"],["totalManagedAssets",p.vault,"totalManagedAssets"],["vaultBalance",p.asset,"balanceOf",[p.vault]],
  ]) { const v = await call(target,fn,args); s[key] = typeof v === "string" ? v.toLowerCase() : Array.isArray(v) ? v.map(a=>a.toLowerCase()) : typeof v === "bigint" ? v.toString() : v; }
  const authorityChecks = [[p.vault,ROLES.admin,p.timelock,true],[p.vault,ROLES.operator,p.operator,true],
    [p.timelock,ROLES.admin,p.timelock,true],[p.timelock,ROLES.proposer,p.gov,true],[p.timelock,ROLES.executor,p.gov,true],[p.timelock,ROLES.canceller,p.gov,true],
    [p.timelock,ROLES.canceller,p.can,true],[p.timelock,ROLES.executor,ZERO,false],
    [p.timelock,ROLES.proposer,p.can,false],[p.timelock,ROLES.executor,p.can,false]];
  s.authoritiesValid = true;
  for(const [target,role,account,expected] of authorityChecks) if(await call(target,"hasRole",[role,account]) !== expected) s.authoritiesValid = false;
  let result = buildLossContingency(p,s,now());
  for(const path of Object.values(result.paths)) check(await call(p.timelock,"getTimestamp",[path.id]) === 0n,"OPERATION_ALREADY_SCHEDULED_OR_DONE");
  const end = await rpc("eth_getBlockByNumber",["latest",false]);
  check(end?.hash && BigInt(end.number) >= BigInt(h.number) && BigInt(end.timestamp) >= BigInt(h.timestamp) && BigInt(end.timestamp) - BigInt(h.timestamp) <= 120n,"STALE_SCAN");
  check((await rpc("eth_getBlockByNumber",[h.number,false]))?.hash === h.hash,"REORG");
  check(Number(BigInt(await rpc("eth_chainId",[]))) === p.chainId,"RPC_CHAIN");
  result = buildLossContingency(p,s,now());
  return {...result, observation:{...result.observation, checks:"Pinned code, proxy/beacon, relationship, authority subset, round, balances and unused IDs; full history remains external"}};
}
