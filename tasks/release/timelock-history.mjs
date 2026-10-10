// GC-03: complete action-bearing history under the reviewed archive/RPC assumptions.
// Parsing supplied rows alone never attests that the provider supplied every row.
import { decodeEventLog, encodeAbiParameters, parseAbi, keccak256, stringToHex } from "viem";
import { requireThat, exactSet, canonical, ZERO32 } from "./core.mjs";
export const PINNED_TIMELOCK_RUNTIME_HASH = "0xb884c9eeacf02e5a2d3b461ac21224e263170761d4fd4987cd2378b7136d8764";
export const OPERATION_COVERAGE_DEFINITION = "faroo-action-bearing-v1";
const abi = parseAbi([
    "event CallScheduled(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data,bytes32 predecessor,uint256 delay)",
    "event CallExecuted(bytes32 indexed id,uint256 indexed index,address target,uint256 value,bytes data)",
    "event Cancelled(bytes32 indexed id)", "event CallSalt(bytes32 indexed id,bytes32 salt)",
]);
export const operationTopics = ["CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)", "CallExecuted(bytes32,uint256,address,uint256,bytes)", "Cancelled(bytes32)", "CallSalt(bytes32,bytes32)"].map(s => keccak256(stringToHex(s)));
const sameTransaction = (a, b) => a.transactionHash === b.transactionHash && a.blockHash === b.blockHash;
function identity(id, calls, predecessor, salt) {
    const batch = keccak256(encodeAbiParameters([{type:"address[]"},{type:"uint256[]"},{type:"bytes[]"},{type:"bytes32"},{type:"bytes32"}], [calls.map(c=>c.target),calls.map(c=>c.value),calls.map(c=>c.data),predecessor,salt]));
    const single = calls.length === 1 ? keccak256(encodeAbiParameters([{type:"address"},{type:"uint256"},{type:"bytes"},{type:"bytes32"},{type:"bytes32"}], [calls[0].target,calls[0].value,calls[0].data,predecessor,salt])) : undefined;
    requireThat(id === batch || id === single, "TIMELOCK_OPERATION_ID");
    return id === batch ? "batch" : "single";
}
function state(timestamp, checkpointTime) {
    if (timestamp === 0n) return "Unset";
    if (timestamp === 1n) return "Done";
    return checkpointTime === undefined ? "Pending" : timestamp > checkpointTime ? "Waiting" : "Ready";
}
export async function replayOperations(rows, timestampAt, getTimestamp, checkpointTime) {
    const operations = new Map(), events = [], seen = new Set(), episodes = [], emptyMetadata = new Map();
    const dependencies = new Set(), executedDependencies = new Set(), blockTimes = new Map();
    const decoded = rows.map(row => ({ row, ...decodeEventLog({ abi, ...row, strict: true }) }));
    const actionIds = new Set(decoded.filter(e => e.eventName === "CallScheduled").map(e => e.args.id));
    for (let i = 0; i < rows.length; i++) {
        const row = rows[i], key = `${row.blockHash}:${BigInt(row.logIndex)}`;
        requireThat(!seen.has(key), "TIMELOCK_DUPLICATE_LOG"); seen.add(key);
        if (!blockTimes.has(row.blockHash)) {
            const timestamp = await timestampAt(row);
            requireThat(typeof timestamp === "bigint" && timestamp >= 0n, "TIMELOCK_EVENT_TIMESTAMP");
            blockTimes.set(row.blockHash,timestamp);
        }
        if (i) requireThat(BigInt(row.blockNumber) > BigInt(rows[i-1].blockNumber) || (row.blockHash === rows[i-1].blockHash && BigInt(row.blockNumber) === BigInt(rows[i-1].blockNumber) && BigInt(row.logIndex) > BigInt(rows[i-1].logIndex)), "TIMELOCK_LOG_ORDER");
    }
    const record = e => events.push({ event:e.eventName, id:e.args.id, block:e.row.blockNumber, logIndex:e.row.logIndex, transactionHash:e.row.transactionHash });
    for (let pos = 0; pos < decoded.length;) {
        const first = decoded[pos++], { row, eventName: kind, args: a } = first, id = a.id;
        record(first);
        const prior = operations.get(id);
        if (kind === "CallScheduled") {
            requireThat(!prior || prior.status === "cancelled", "TIMELOCK_RESCHEDULE_HISTORY");
            const group = [a];
            // No external call occurs inside scheduleBatch: its indices and salt are contiguous.
            while (pos < decoded.length && decoded[pos].eventName === kind && decoded[pos].args.id === id && sameTransaction(row, decoded[pos].row)) {
                record(decoded[pos]); group.push(decoded[pos++].args);
            }
            requireThat(group.every((g,i) => g.index === BigInt(i)), "TIMELOCK_CALL_INDICES");
            requireThat(group.every(g => g.delay === a.delay && g.predecessor === a.predecessor), "TIMELOCK_BATCH_METADATA");
            let salt = ZERO32;
            const next = decoded[pos];
            if (next?.eventName === "CallSalt" && next.args.id === id && sameTransaction(row,next.row)) {
                requireThat(next.args.salt !== ZERO32, "TIMELOCK_SALT_HISTORY"); salt = next.args.salt; record(next); pos++;
            }
            const calls = group.map(g => ({target:g.target.toLowerCase(),value:g.value,data:g.data}));
            const shape = identity(id,calls,a.predecessor,salt), scheduledAt = blockTimes.get(row.blockHash), timestamp = scheduledAt+a.delay;
            requireThat(timestamp > 1n, "TIMELOCK_SCHEDULE_TIMESTAMP");
            const op = {id, status:"pending", timestamp, calls, shape, predecessor:a.predecessor, salt, delay:a.delay, scheduledAt, readyAt:timestamp, scheduleTransaction:row.transactionHash, executionCount:0};
            // OZ can cancel/re-schedule the executing ID inside a target call when
            // the historical delay is zero. The unchanged hash commits identical
            // calls; only a same-transaction continuation can persist successfully.
            if (prior?.executionCount) {
                requireThat(prior.executionTransaction === row.transactionHash && prior.executionBlockHash === row.blockHash && canonical(prior.calls) === canonical(calls), "TIMELOCK_PARTIAL_EXECUTION");
                op.executionCount = prior.executionCount;
                op.executionTransaction = prior.executionTransaction;
                op.executionBlockHash = prior.executionBlockHash;
            }
            operations.set(id,op); episodes.push(op);
            if (a.predecessor !== ZERO32) dependencies.add(a.predecessor);
        } else if (kind === "CallExecuted") {
            requireThat(prior?.status === "pending" && a.index === BigInt(prior.executionCount) && a.index < BigInt(prior.calls.length), "TIMELOCK_EXECUTION_HISTORY");
            const call = {target:a.target.toLowerCase(),value:a.value,data:a.data};
            requireThat(canonical(call) === canonical(prior.calls[prior.executionCount]), "TIMELOCK_EXECUTION_HISTORY");
            requireThat(!prior.executionTransaction || (prior.executionTransaction === row.transactionHash && prior.executionBlockHash === row.blockHash), "TIMELOCK_PARTIAL_EXECUTION");
            requireThat(blockTimes.get(row.blockHash) >= prior.timestamp, "TIMELOCK_EXECUTED_EARLY");
            if (prior.predecessor !== ZERO32) {
                const predecessor = operations.get(prior.predecessor);
                requireThat(!actionIds.has(prior.predecessor) || predecessor?.status === "done", "TIMELOCK_PREDECESSOR_ORDER");
                executedDependencies.add(prior.predecessor);
            }
            prior.executionTransaction = row.transactionHash; prior.executionBlockHash = row.blockHash;
            prior.executionCount++;
            // Interleaved execution events for OTHER operations do not reset this index.
            if (prior.executionCount === prior.calls.length) { prior.status = "done"; prior.timestamp = 1n; }
        } else if (kind === "Cancelled" && actionIds.has(id)) {
            requireThat(prior?.status === "pending", "TIMELOCK_CANCEL_HISTORY");
            prior.status = "cancelled"; prior.timestamp = 0n;
        } else {
            // With complete lifetime CallScheduled coverage, an orphan Salt/Cancelled
            // ID is empty. Its silent transitions are NOT reconstructed or certified.
            requireThat(!actionIds.has(id), "TIMELOCK_SALT_HISTORY");
            const empty = emptyMetadata.get(id) ?? {events:[]};
            if (kind === "CallSalt") {
                requireThat(a.salt !== ZERO32 && (!empty.salt || empty.salt === a.salt) && empty.lastEvent !== "CallSalt", "TIMELOCK_SALT_HISTORY");
                empty.salt = a.salt;
            } else requireThat(kind === "Cancelled", "TIMELOCK_EMPTY_METADATA");
            empty.lastEvent = kind; empty.events.push({event:kind,transactionHash:row.transactionHash,block:row.blockNumber}); emptyMetadata.set(id,empty);
        }
    }
    const observed = new Map();
    async function timestamp(id, code) {
        if (!observed.has(id)) {
            const value = await getTimestamp(id);
            requireThat(typeof value === "bigint" && value >= 0n, code, id);
            observed.set(id,value);
        }
        return observed.get(id);
    }
    for (const [id,op] of operations) {
        requireThat(op.executionCount === 0 || op.status === "done", "TIMELOCK_PARTIAL_EXECUTION");
        requireThat(await timestamp(id,"TIMELOCK_OPERATION_STATE") === op.timestamp, "TIMELOCK_OPERATION_STATE",id);
        op.currentState = state(op.timestamp,checkpointTime);
    }
    const predecessorStates = {};
    for (const id of dependencies) {
        const value = await timestamp(id,"TIMELOCK_DEPENDENCY_STATE");
        requireThat(!executedDependencies.has(id) || value === 1n, "TIMELOCK_DEPENDENCY_NOT_DONE",id);
        predecessorStates[id] = {timestamp:value,state:state(value,checkpointTime),done:value===1n,actionBearing:actionIds.has(id)};
    }
    for (const [id,empty] of emptyMetadata) {
        empty.timestamp = await timestamp(id,"TIMELOCK_EMPTY_STATE");
        empty.currentState = state(empty.timestamp,checkpointTime);
        empty.historyCoverage = "NOT_CLAIMED — silent empty schedule/execute transitions";
    }
    return {definition:OPERATION_COVERAGE_DEFINITION,replayStatus:"PASS",actionBearingCoverage:"UNATTESTED",dependencyVerification:"PASS",literalAllOperationIds:"NOT_CLAIMED",events,episodes,operations:Object.fromEntries(operations),predecessorStates,emptyMetadata:Object.fromEntries(emptyMetadata),pending:[...operations].filter(([,op])=>op.status==="pending").map(([id])=>id).sort()};
}
export function checkOperationPlan(inventory, expectedPending, doneId) {
    exactSet(inventory.pending,expectedPending,"TIMELOCK_OUTSTANDING_OPERATIONS");
    if (doneId) requireThat(inventory.operations[doneId]?.status === "done","TIMELOCK_RELEASE_NOT_EXECUTED");
    for (const id of [...expectedPending,...(doneId?[doneId]:[])])
        requireThat(inventory.operations[id]?.predecessor === ZERO32,"TIMELOCK_RELEASE_PREDECESSOR");
}
export function requireActionBearingCoverage(result) {
    const inventory = result?.timelockOperations;
    requireThat(inventory?.definition === OPERATION_COVERAGE_DEFINITION && inventory.actionBearingCoverage === "PASS" && inventory.replayStatus === "PASS" && inventory.literalAllOperationIds === "NOT_CLAIMED" && inventory.dependencyVerification === "PASS" && inventory.sourceRuntimeHash === PINNED_TIMELOCK_RUNTIME_HASH && /^[0-9a-f]{64}$/.test(result?.history?.authenticatedManifestSha256 ?? "") && result.history.through >= (result.currentBlock ?? result.block), "INCOMPLETE_TIMELOCK_ACTION_COVERAGE");
}
