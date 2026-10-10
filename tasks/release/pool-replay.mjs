import { decodeEventLog, parseAbi, keccak256, stringToHex } from "viem";
import { requireThat, ZERO } from "./core.mjs";
const signatures = [
    "Transfer(address,address,uint256)", "Deposit(address,address,uint256,uint256)", "Withdraw(address,address,address,uint256,uint256)",
    "PoolUpdated(address,uint256,uint256,bool)", "TokenAmountUpdated(address,uint256,uint256)", "SetTokenAmount(address,uint256,uint256)",
    "CommissionMinted(address,address,uint256)", "CommissionMinted(address,address,address,uint256,uint256)",
];
export const poolTopics = signatures.map(s => keccak256(stringToHex(s)));
const abi = parseAbi([
    "event Transfer(address indexed from,address indexed to,uint256 value)",
    "event Deposit(address indexed sender,address indexed owner,uint256 assets,uint256 shares)",
    "event Withdraw(address indexed sender,address indexed receiver,address indexed owner,uint256 assets,uint256 shares)",
    "event PoolUpdated(address indexed token,uint256 tokenAmount,uint256 vTokenAmount,bool isMint)",
    "event TokenAmountUpdated(address indexed token,uint256 addedAmount,uint256 newTokenAmount)",
    "event SetTokenAmount(address indexed token,uint256 tokenAmount,uint256 vTokenAmount)",
    "event CommissionMinted(address indexed caller,address indexed account,uint256 shares)",
    "event CommissionMinted(address indexed token,address indexed vToken,address indexed account,uint256 tokenAmount,uint256 vTokenAmount)",
]);
export function replayPool(c, rows, before, after) {
    let t = BigInt(before.poolT), v = BigInt(before.poolV);
    let burnOrMint, poolEvent, shareCommission, oracleCommission;
    const sameTx = (a, b) => a.transactionHash === b.transactionHash && a.blockHash === b.blockHash;
    const d = c.addresses;
    for (const row of rows) {
        const { eventName: name, args: a } = decodeEventLog({ abi, ...row, strict: true });
        if (row.address === d.stpros && name === "Transfer") {
            if (a.from !== ZERO && a.to !== ZERO) continue;
            requireThat(!burnOrMint && a.value > 0n && !(a.from === ZERO && a.to === ZERO), "POOL_SHARE_SEQUENCE");
            burnOrMint = { ...row, ...a }; continue;
        }
        if (row.address === d.oracle) {
            requireThat(a.token.toLowerCase() === d.asset, "UNKNOWN_POOL_ACTIVITY");
            requireThat(name !== "SetTokenAmount", "UNREVIEWED_POOL_SETTER");
            if (name === "PoolUpdated") {
                requireThat(!poolEvent && burnOrMint && sameTx(burnOrMint, row), "POOL_EVENT_SEQUENCE");
                poolEvent = { ...row, ...a }; continue;
            }
            if (name === "CommissionMinted") {
                requireThat(!oracleCommission && shareCommission && sameTx(shareCommission, row), "POOL_COMMISSION_SEQUENCE");
                oracleCommission = { ...row, ...a }; continue;
            }
            requireThat(name === "TokenAmountUpdated" && a.addedAmount > 0n && v > 0n && !poolEvent, "POOL_UPDATE_SEQUENCE");
            const fee = a.addedAmount * BigInt(c.observed.core.commissionPpm) / 1000000n;
            const commissionShares = fee * v / (t + a.addedAmount - fee);
            if (commissionShares > 0n) {
                requireThat(burnOrMint && shareCommission && oracleCommission && sameTx(oracleCommission, row) && sameTx(burnOrMint, row), "POOL_COMMISSION_SEQUENCE");
                requireThat(burnOrMint.from === ZERO && burnOrMint.to.toLowerCase() === c.observed.core.treasury && burnOrMint.value === commissionShares && shareCommission.shares === commissionShares && shareCommission.caller.toLowerCase() === d.oracle && shareCommission.account.toLowerCase() === c.observed.core.treasury && oracleCommission.vToken.toLowerCase() === d.stpros && oracleCommission.account.toLowerCase() === c.observed.core.treasury && oracleCommission.tokenAmount === fee && oracleCommission.vTokenAmount === commissionShares, "POOL_COMMISSION_ACCOUNTING");
            } else requireThat(!burnOrMint && !shareCommission && !oracleCommission, "POOL_UNEXPLAINED_SHARES");
            t += a.addedAmount; v += commissionShares;
            requireThat(a.newTokenAmount === t, "POOL_UPDATE_AMOUNT");
            burnOrMint = shareCommission = oracleCommission = undefined; continue;
        }
        requireThat(row.address === d.stpros, "POOL_EVENT_TARGET");
        if (name === "CommissionMinted") {
            requireThat(burnOrMint && !shareCommission && sameTx(burnOrMint, row), "POOL_COMMISSION_SEQUENCE");
            shareCommission = { ...row, ...a }; continue;
        }
        requireThat(["Deposit", "Withdraw"].includes(name) && burnOrMint && poolEvent && sameTx(burnOrMint, row) && sameTx(poolEvent, row) && !shareCommission && !oracleCommission, "POOL_EVENT_SEQUENCE");
        const mint = name === "Deposit";
        requireThat(a.assets > 0n && a.shares > 0n && burnOrMint.value === a.shares && (mint ? burnOrMint.from === ZERO && burnOrMint.to.toLowerCase() === a.owner.toLowerCase() : burnOrMint.to === ZERO && burnOrMint.from.toLowerCase() === a.owner.toLowerCase()), "POOL_SHARE_ACCOUNTING");
        if (mint) { t += a.assets; v += a.shares; }
        else {
            requireThat(t >= a.assets && v >= a.shares, "POOL_REDEMPTION_BOUNDS");
            t -= a.assets; v -= a.shares;
            if (v === 0n) t = 0n;
        }
        requireThat(poolEvent.isMint === mint && poolEvent.tokenAmount === t && poolEvent.vTokenAmount === v, "POOL_EVENT_ACCOUNTING");
        burnOrMint = poolEvent = undefined;
    }
    requireThat(!burnOrMint && !poolEvent && !shareCommission && !oracleCommission, "POOL_INCOMPLETE_SEQUENCE");
    requireThat(t === BigInt(after.poolT) && v === BigInt(after.poolV), "UNRECONCILED_POOL");
}
