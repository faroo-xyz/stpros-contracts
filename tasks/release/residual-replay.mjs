import { decodeEventLog, decodeFunctionData, parseAbi, keccak256, stringToHex, encodeAbiParameters } from "viem";
import { requireThat } from "./core.mjs";
export const termNames = ["openWindow", "lockDuration", "settleTimelockWindow", "roundCap", "perAddressCap", "minSubscription", "performanceFeeBps", "maxLossBps", "openedAt", "openDeadline", "counterparty", "feeRecipient"];
export const residualReads = parseAbi([...termNames.map((n, i) => `function ${n}() view returns(${i >= 10 ? "address" : "uint256"})`), "function settleMode() view returns(uint8)", "function settleAmount() view returns(uint256)", "function profitFunded() view returns(bool)"]);
export function termsHash(terms) { return keccak256(encodeAbiParameters([{ type: "bytes32[12]" }], [terms.map(x => `0x${BigInt(x).toString(16).padStart(64, "0")}`)])); }
const signatures = ["RoundOpened(uint256,uint256,uint256,uint256)", "OpenPeriodExtended(uint256,uint256,uint256)", "RoundLocked(uint256,uint256,uint256)", "SettlementProposed(uint256,uint8,uint256,uint256,bool,address)", "SettlementCancelled(uint256,uint256)", "SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)", "EmergencyCancelled(uint256)", "Retired(uint256)", "ParSettlementForced(uint256,uint256)"];
export const residualTopics = signatures.map(s => keccak256(stringToHex(s)));
const abi = parseAbi([
    "event RoundOpened(uint256 indexed roundIndex,uint256 openedAt,uint256 openDeadline,uint256 roundCap)",
    "event OpenPeriodExtended(uint256 indexed roundIndex,uint256 previousOpenDeadline,uint256 newOpenDeadline)",
    "event RoundLocked(uint256 indexed roundIndex,uint256 lockedAt,uint256 lockMaturity)",
    "event SettlementProposed(uint256 indexed roundIndex,uint8 indexed mode,uint256 amount,uint256 proposedAt,bool funded,address indexed funder)",
    "event SettlementCancelled(uint256 indexed roundIndex,uint256 at)",
    "event SettlementFinalized(uint256 indexed roundIndex,uint8 indexed mode,uint256 amount,uint256 netProfit,uint256 fee,uint256 settledAt)",
    "event EmergencyCancelled(uint256 at)", "event Retired(uint256 at)", "event ParSettlementForced(uint256 indexed roundIndex,uint256 at)",
]);
// Only event-explained legacy lifecycle changes are accepted. Debug setters without
// sufficient events remain fail-closed and require separately authenticated evidence.
export async function replayResidual(before, after, rows, timestamp, post, chainId, transactionAt) {
    let round = BigInt(before.round), phase = before.phase, terms = [...before.terms], retired = false;
    for (const row of rows) {
        const { eventName: n, args: a } = decodeEventLog({ abi, ...row, strict: true });
        if (n === "RoundOpened") {
            requireThat(phase === 3 && a.roundIndex === round + 1n && !retired, "RESIDUAL_ROUND_SEQUENCE");
            const transaction = await transactionAt(row);
            requireThat(transaction?.hash === row.transactionHash && transaction.blockHash === row.blockHash && BigInt(transaction.blockNumber) === BigInt(row.blockNumber), "RESIDUAL_ROUND_CALL_EVIDENCE");
            requireThat(transaction.to?.toLowerCase() === row.address, "RESIDUAL_ROUND_CALL_EVIDENCE", "INCOMPLETE_SAFE_RECOVERY_EVIDENCE: reviewed historical Safe implementation, authority, nonce and execution/receipt proof required; no forwarding-model bypass");
            const fields = termNames.slice(0, chainId === 1672 ? 9 : 8).map(n => `uint256 ${n}`).join(",");
            const call = decodeFunctionData({ abi: parseAbi([`function openNextRound((${fields}) params)`]), data: transaction.input });
            requireThat(call.functionName === "openNextRound", "RESIDUAL_ROUND_CALL_EVIDENCE");
            const params = call.args[0];
            for (let i=0; i<8; i++) terms[i] = params[termNames[i]];
            requireThat(a.roundCap === BigInt(terms[3]) && a.openDeadline === a.openedAt + BigInt(terms[0]) && (chainId !== 1672 || params.openedAt === 0n || params.openedAt === a.openedAt), "RESIDUAL_ROUND_PARAMETERS");
            round = a.roundIndex; phase = 0;
            terms[8] = a.openedAt; terms[9] = a.openDeadline;
        } else {
            if (a.roundIndex !== undefined) requireThat(a.roundIndex === round, "RESIDUAL_ROUND_SEQUENCE");
            if (n === "OpenPeriodExtended") {
                requireThat(phase === 0 && a.previousOpenDeadline === BigInt(terms[9]) && a.newOpenDeadline > a.previousOpenDeadline, "RESIDUAL_EXTENSION"); terms[9] = a.newOpenDeadline;
            } else if (n === "RoundLocked") { requireThat(phase === 0 && a.lockedAt === BigInt(terms[9]), "RESIDUAL_LOCK_SEQUENCE"); phase = 1; }
            else if (n === "SettlementProposed") { requireThat([0,1,2].includes(phase), "RESIDUAL_PROPOSAL_SEQUENCE"); phase = 2; }
            else if (n === "SettlementCancelled") { requireThat(phase === 2, "RESIDUAL_CANCEL_SEQUENCE"); phase = 1; }
            else if (n === "SettlementFinalized") { requireThat(phase === 2, "RESIDUAL_FINALIZE_SEQUENCE"); phase = 3; }
            else if (n === "Retired") { requireThat(post && !retired, "RESIDUAL_RETIRE_SEQUENCE"); phase = 3; retired = true; }
            else { requireThat(n === "EmergencyCancelled" || (post && n === "ParSettlementForced"), "RESIDUAL_EVENT"); phase = 3; }
        }
    }
    if (phase === 0 && timestamp >= BigInt(terms[9])) phase = 1;
    requireThat(round === BigInt(after.round) && phase === after.phase, "RESIDUAL_STATE_UNEXPLAINED");
    // Bind every term to authenticated calldata; event presence alone cannot
    // explain V3 debug setters. Internal Safe calls need additional evidence.
    for (let i = 0; i < terms.length; i++)
        requireThat(BigInt(terms[i]) === BigInt(after.terms[i]), "RESIDUAL_TERMS_UNEXPLAINED");
    requireThat(!post || retired, "RESIDUAL_RETIRE_EVENT_MISSING");
}
