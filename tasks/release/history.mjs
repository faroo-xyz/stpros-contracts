// Authenticated B0-compatible archives plus one gap-free, shared incremental journal.
// No archive may assert its own approval: the config's independently reviewed hash binds it.
import { checkedJson, requireThat, canonical, sha256, addr } from "./core.mjs";
const hex = n => `0x${BigInt(n).toString(16)}`;
const integer = n => Number.isSafeInteger(n) && n >= 0;
const order = (a, b) => Number(BigInt(a.blockNumber) - BigInt(b.blockNumber)) || Number(BigInt(a.logIndex) - BigInt(b.logIndex));
export function historyTargets(c) {
    return [...new Set([c.addresses.oracle, c.addresses.stpros, c.addresses.bridge, c.addresses.distributor, c.addresses.proxyAdmin, c.addresses.timelock, c.addresses.assertions, c.addresses.asset, ...c.factories.flatMap(f => [f.address, f.beacon, ...f.vaults.map(v => v.address)])])].map(a => addr(a)).sort();
}
function validateRows(rows, addresses, from, to) {
    requireThat(Array.isArray(rows), "HISTORY_ROWS_MISSING");
    const seen = new Set();
    for (const l of rows) {
        requireThat(l && !l.removed && addresses.includes(l.address) && /^0x[0-9a-f]{64}$/.test(l.blockHash) && /^0x[0-9a-f]{64}$/.test(l.transactionHash), "HISTORY_LOG_IDENTITY");
        const n = Number(BigInt(l.blockNumber));
        requireThat(integer(n) && n >= from && n <= to && BigInt(l.logIndex) >= 0n && Array.isArray(l.topics) && l.topics.every(t => /^0x[0-9a-f]{64}$/.test(t)) && /^0x([0-9a-f]{2})*$/.test(l.data), "HISTORY_LOG_RANGE");
        const key = `${n}:${BigInt(l.logIndex)}`;
        requireThat(!seen.has(key), "HISTORY_DUPLICATE_LOG"); seen.add(key);
    }
}
export function authenticateHistory(c, inventory) {
    requireThat(c.historyFile && /^[a-f0-9]{64}$/.test(c.review.historySha256 ?? ""), "HISTORY_PROOF_REQUIRED");
    const proof = checkedJson(c.historyFile, c.review.historySha256);
    requireThat(proof.schema === "faroo-history-v1" && proof.chainId === c.chainId && proof.blockNumber === c.observed.blockNumber && proof.blockHash === c.observed.blockHash && proof.fromBlock === inventory.fromBlock && proof.inventorySha256 === c.review.inventorySha256, "HISTORY_BINDING");
    const targets = historyTargets(c), coverage = new Map(targets.map(a => [a, []])), rows = [], anchors = [];
    requireThat(canonical(proof.targets) === canonical(targets) && Array.isArray(proof.segments) && proof.segments.length > 0, "HISTORY_TARGETS");
    for (const segment of proof.segments) {
        requireThat(/^[a-f0-9]{64}$/.test(segment.planSha256 ?? ""), "HISTORY_PLAN_HASH_REQUIRED");
        const p = checkedJson(segment.planFile, segment.planSha256), addresses = p.addresses.map(a => addr(a.address));
        requireThat(p.chain_id === c.chainId && p.chain === (c.chainId === 1672 ? "mainnet" : "testnet") && p.window === 1000 && integer(p.start) && integer(p.end) && p.start >= proof.fromBlock && p.end >= p.start && p.end <= proof.blockNumber, "HISTORY_PLAN_RANGE");
        requireThat(new Set(addresses).size === addresses.length && addresses.length > 0 && /^0x[0-9a-f]{64}$/.test(p.end_block_hash), "HISTORY_PLAN_TARGETS");
        // B0 uses Python json.dumps(sorted(addresses)), whose list separator is ', '.
        requireThat(p.filter_id === sha256(`[${[...addresses].sort().map(JSON.stringify).join(", ")}]`).slice(0, 16), "HISTORY_FILTER");
        requireThat(anchors.every(a => a.number !== p.end || a.hash === p.end_block_hash), "HISTORY_ANCHOR_CONFLICT");
        anchors.push({ number: p.end, hash: p.end_block_hash });
        for (const target of targets.filter(t => addresses.includes(t))) coverage.get(target).push([p.start, p.end]);
        let next = p.start;
        for (const ref of segment.chunks) {
            requireThat(/^[a-f0-9]{64}$/.test(ref.sha256 ?? ""), "HISTORY_CHUNK_HASH_REQUIRED");
            const chunk = checkedJson(ref.path, ref.sha256);
            requireThat(chunk.chain === p.chain && chunk.filter_id === p.filter_id && chunk.from === next && integer(chunk.to) && chunk.to >= next && chunk.to <= p.end, "HISTORY_CHUNK_GAP");
            validateRows(chunk.logs, addresses, chunk.from, chunk.to);
            let window = chunk.from;
            for (const [a, b, count] of chunk.windows) {
                requireThat(a === window && integer(b) && b >= a && b - a < 1000 && b <= chunk.to && integer(count), "HISTORY_WINDOW_GAP");
                requireThat(chunk.logs.filter(l => Number(BigInt(l.blockNumber)) >= a && Number(BigInt(l.blockNumber)) <= b).length === count, "HISTORY_WINDOW_COUNT");
                window = b + 1;
            }
            requireThat(window === chunk.to + 1, "HISTORY_WINDOW_GAP");
            rows.push(...chunk.logs.filter(l => targets.includes(l.address))); next = chunk.to + 1;
        }
        requireThat(next === p.end + 1, "HISTORY_CHUNK_GAP");
    }
    for (const ranges of coverage.values()) {
        let next = proof.fromBlock;
        for (const [a, b] of ranges.sort((a, b) => a[0] - b[0])) {
            requireThat(a === next, "HISTORY_TARGET_GAP_OR_OVERLAP"); next = b + 1;
        }
        requireThat(next === proof.blockNumber + 1, "HISTORY_TARGET_GAP_OR_OVERLAP");
    }
    validateRows(rows, targets, proof.fromBlock, proof.blockNumber);
    return { proof, targets, rows: rows.sort(order), anchors };
}
export async function historySession(c, inventory, rawRpc) {
    const { proof, targets, rows, anchors } = authenticateHistory(c, inventory);
    // Immutable block reads are memoized only within this invocation. Header reads are
    // never memoized; every reused prefix is checked against canonical hashes again.
    const cache = new Map(), pins = new Map(anchors.map(a => [a.number, a.hash]));
    let through = proof.blockNumber, logRequests = 0, tip;
    async function checkPins() {
        // A canonical tip hash commits to its ancestry. Before advancing it, the
        // previous tip is rechecked, so a reorg cannot be hidden between windows.
        for (const [n, hash] of [...pins, ...(tip ? [tip] : [])]) {
            const h = await rawRpc("eth_getBlockByNumber", [hex(n), false]);
            requireThat(h?.hash === hash && BigInt(h.number) === BigInt(n), "HISTORY_REORG");
        }
    }
    await checkPins();
    for (const target of targets)
        requireThat(await rawRpc("eth_getCode", [target, hex(proof.fromBlock - 1)]) === "0x", "INVENTORY_START_HAS_CODE", target);
    async function rpc(method, params) {
        if (["eth_call", "eth_getCode", "eth_getStorageAt"].includes(method) && params.at(-1) !== "latest") {
            const key = canonical([method, params]);
            if (!cache.has(key)) cache.set(key, rawRpc(method, params));
            return cache.get(key);
        }
        return rawRpc(method, params);
    }
    rpc.historyLogs = (target, from, to, topics) => {
        requireThat(targets.includes(target) && from >= proof.fromBlock && to <= through, "HISTORY_UNCOVERED_RANGE");
        return rows.filter(l => l.address === target && Number(BigInt(l.blockNumber)) >= from && Number(BigInt(l.blockNumber)) <= to && topics.includes(l.topics[0]));
    };
    async function extend(head) {
        await checkPins();
        const to = Number(BigInt(head.block));
        requireThat(to >= through, "HISTORY_HEAD_REGRESSION");
        for (let start = through + 1; start <= to; start += 1000) {
            const end = Math.min(start + 999, to), header = await rawRpc("eth_getBlockByNumber", [hex(end), false]);
            requireThat(header?.hash && BigInt(header.number) === BigInt(end), "HISTORY_WINDOW_HEADER");
            // All addresses in one request, all event topics. Empty windows are explicit.
            const batch = await rawRpc("eth_getLogs", [{ address: targets, fromBlock: hex(start), toBlock: hex(end) }]);
            logRequests++;
            validateRows(batch, targets, start, end);
            const eventPins = new Map(batch.map(l => [Number(BigInt(l.blockNumber)), l.blockHash]));
            for (const [n, hash] of eventPins) {
                const h = await rawRpc("eth_getBlockByNumber", [hex(n), false]);
                requireThat(h?.hash === hash && BigInt(h.number) === BigInt(n) && batch.filter(l => Number(BigInt(l.blockNumber)) === n).every(l => l.blockHash === hash), "HISTORY_EVENT_REORG");
            }
            requireThat((await rawRpc("eth_getBlockByNumber", [hex(end), false]))?.hash === header.hash, "HISTORY_WINDOW_REORG");
            await checkPins();
            rows.push(...batch.sort(order)); tip = [end, header.hash]; through = end;
        }
        requireThat((await rawRpc("eth_getBlockByNumber", [head.block, false]))?.hash === head.header.hash, "HISTORY_HEAD_REORG");
        tip = [to, head.header.hash];
    }
    return { rpc, extend, checkPins, statistics: () => ({ historicalRpcLogRequests: 0, incrementalRpcLogRequests: logRequests, through, authenticatedManifestSha256: c.review.historySha256 }) };
}
