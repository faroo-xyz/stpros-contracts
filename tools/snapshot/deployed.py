#!/usr/bin/env python3
"""Deployed-state snapshot at the pinned blocks (snapshots/deployed/<chain>/state.json).

Per chain: ProxyAdmin (owner, code), every proxy (EIP-1967 implementation and
admin slots, OZ v5 Initializable/Ownable/Pausable namespaces, code hashes, and
whether the implementation equals the registry expectation and a recorded
deployment), beacons, every factory vault, StPROS/Oracle accounting with the
COMPATIBILITY §5.4 PRE-4 relations, WPROS code, and (mainnet) the Safe's
threshold, owners, modules and guard.

All reads use the pinned block from snapshots/deployed/pins.json, so `--check`
re-reads the same archive state and must reproduce the file exactly.

Usage: deployed.py [--check] [--chain mainnet|testnet]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import abi  # noqa: E402
import common  # noqa: E402
import rpc  # noqa: E402
from keccak import keccak256  # noqa: E402

PINS = common.read_json(common.repo_path("snapshots", "deployed", "pins.json"))
SLOT = {
    "implementation": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "admin": "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
    "initialized": "0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00",
    "owner": "0x9016d09d72d40fdae2fd8ceac6b6234c7706214fd39c1cd1e609a0528c199300",
    "paused": "0xcd5ed15c6e187e77e9aee88184c21f4f2182ab5827cb3b7e07fbedcd63f03300",
}
SAFE_GUARD_SLOT = keccak256(b"guard_manager.guard.address")
SENTINEL = "0x0000000000000000000000000000000000000001"


class Reader:
    def __init__(self, chain):
        self.c = rpc.Client(chain, min_interval=1.2, verbose=True)
        self.block = PINS[chain]

    def call(self, to, sig, *args):
        return self.c.eth_call(to, abi.calldata(sig, *args), self.block)

    def u(self, to, sig, *args):
        return abi.dec_uint(self.call(to, sig, *args))

    def a(self, to, sig, *args):
        return abi.dec_addr(self.call(to, sig, *args))

    def slot(self, addr, s):
        return self.c.get_storage(addr, s, self.block)

    def slot_addr(self, addr, s):
        return "0x" + self.slot(addr, s)[-40:]

    def slot_uint(self, addr, s):
        return int(self.slot(addr, s), 16)

    def code(self, addr):
        code = self.c.get_code(addr, self.block)
        return {"size": len(common.hexbytes(code)), "keccak256": keccak256(common.hexbytes(code))}

    def balance(self, addr):
        return int(self.c.call("eth_getBalance", [addr, hex(self.block)]), 16)


def recorded_runtime_index():
    out = {}
    for chain in common.CHAINS:
        for name, rel, rec in common.deployment_records(chain):
            if rec.get("deployedBytecode") and rec.get("metadata"):
                out.setdefault(keccak256(common.hexbytes(rec["deployedBytecode"])), []).append(rel)
    return out


def snapshot(chain):
    reg = common.registry()[chain]
    r = Reader(chain)
    rec_idx = recorded_runtime_index()
    blk = r.c.call("eth_getBlockByNumber", [hex(r.block), False])
    doc = {"chain": chain, "chain_id": reg["chain_id"], "block": r.block, "block_hash": blk["hash"],
           "timestamp": int(blk["timestamp"], 16)}
    pa = reg["proxy_admin"]
    doc["proxy_admin"] = {"address": pa, "owner": r.a(pa, "owner()"), "code": r.code(pa),
                          "code_matches_records": rec_idx.get(r.code(pa)["keccak256"], [])}
    proxies = {}
    for p, exp in sorted(reg["expected_proxies"].items()):
        impl = r.slot_addr(p, SLOT["implementation"])
        icode = r.code(impl)
        proxies[p] = {
            "name": exp["name"],
            "implementation": impl,
            "implementation_expected": exp["implementation"],
            "implementation_is_expected": impl == exp["implementation"],
            "implementation_code": icode,
            "implementation_code_matches_records": rec_idx.get(icode["keccak256"], []),
            "admin": r.slot_addr(p, SLOT["admin"]),
            "admin_is_proxy_admin": r.slot_addr(p, SLOT["admin"]) == pa,
            "proxy_code": r.code(p),
            "initialized_version": r.slot_uint(p, SLOT["initialized"]) & ((1 << 64) - 1),
            "initialized_version_expected": exp["version"],
            "owner": r.slot_addr(p, SLOT["owner"]),
            "paused": r.slot_uint(p, SLOT["paused"]) & 0xFF == 1,
        }
    doc["proxies"] = proxies
    beacons = {}
    for b, exp in sorted(reg["expected_beacons"].items()):
        impl = r.a(b, "implementation()")
        beacons[b] = {"role": exp["role"], "owner": r.a(b, "owner()"), "owner_expected": exp["owner"],
                      "implementation": impl, "implementation_expected": exp["implementation"],
                      "implementation_code": r.code(impl),
                      "implementation_code_matches_records": rec_idx.get(r.code(impl)["keccak256"], []),
                      "beacon_code": r.code(b)}
    doc["beacons"] = beacons
    factories = {}
    for f in reg["factories"]:
        n = r.u(f, "totalProxies()")
        vaults = []
        for i in range(n):
            v = r.a(f, "allProxies(uint256)", i)
            vaults.append({
                "index": i, "vault": v, "phase": r.u(v, "phase()"), "roundIndex": r.u(v, "roundIndex()"),
                "totalManagedAssets": str(r.u(v, "totalManagedAssets()")), "totalSupply": str(r.u(v, "totalSupply()")),
                "asset": r.a(v, "asset()"), "factory": r.a(v, "factory()"),
                "asset_balance": str(r.u(r.a(v, "asset()"), "balanceOf(address)", v)),
                "counterparty": r.a(v, "counterparty()"), "feeRecipient": r.a(v, "feeRecipient()"),
                "initialized_version": r.slot_uint(v, SLOT["initialized"]) & ((1 << 64) - 1),
                "beacon_proxy_code": r.code(v)})
        factories[f] = {"owner": r.a(f, "owner()"), "beacon": r.a(f, "beacon()"), "paused": r.u(f, "paused()") == 1,
                        "vault_count": n, "vaults": vaults}
    doc["factories"] = factories

    st, w = reg["stpros"], reg["wpros"]
    S = r.u(st, "totalSupply()")
    E = r.u(st, "balanceOf(address)", st)
    R = r.u(st, "totalCanWithdrawAmount()")
    Q = r.u(st, "queuedWithdrawal()")
    C = r.u(st, "completedWithdrawal()")
    W = r.u(w, "balanceOf(address)", st)
    oracle = r.a(st, "oracle()")
    pool = r.call(oracle, "poolInfo(address)", w)
    acct = {"S_totalSupply": str(S), "E_escrow": str(E), "R_totalCanWithdrawAmount": str(R),
            "Q_queuedWithdrawal": str(Q), "C_completedWithdrawal": str(C), "W_wprosBalance": str(W),
            "native_balance": str(r.balance(st)), "tracked_slot9": str(r.slot_uint(st, "0x9")),
            "maxWithdrawCount": r.u(st, "maxWithdrawCount()"), "unbondingPeriod": r.u(st, "unbondingPeriod()"),
            "oracle": oracle, "oracle_pool_wpros": [str(abi.dec_uint(pool, 0)), str(abi.dec_uint(pool, 1))]}
    acct["PRE4_relations"] = {
        "R_eq_Q_minus_C": R == Q - C,
        "E_ge_R": E >= R,
        "W_minus_R_ge_S_minus_E": W - R >= S - E,
        "pool_is_zero": abi.dec_uint(pool, 0) == 0 and abi.dec_uint(pool, 1) == 0,
        "note": "PRE-4 is a mainnet V1 precondition; on testnet (V3) these are recorded for information only",
    }
    doc["stpros"] = acct
    doc["wpros"] = {"address": w, "code": r.code(w), "totalSupply": str(r.u(w, "totalSupply()"))}
    if chain == "mainnet":
        safe = proxies[st]["owner"]
        owners = r.call(safe, "getOwners()")
        ws = abi.words(owners)
        n = int(ws[1], 16)
        mods = r.call(safe, "getModulesPaginated(address,uint256)", SENTINEL, 10)
        mws = abi.words(mods)
        mn = int(mws[2], 16)
        doc["safe"] = {"address": safe, "threshold": r.u(safe, "getThreshold()"),
                       "owners": ["0x" + x[24:] for x in ws[2:2 + n]], "nonce": r.u(safe, "nonce()"),
                       "modules": ["0x" + x[24:] for x in mws[3:3 + mn]],
                       "guard": r.slot_addr(safe, SAFE_GUARD_SLOT), "code": r.code(safe)}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--chain", choices=sorted(common.CHAINS))
    args = ap.parse_args()
    problems = []
    for chain in ([args.chain] if args.chain else sorted(common.CHAINS)):
        doc = snapshot(chain)
        common.emit_or_check(common.repo_path("snapshots", "deployed", chain, "state.json"), doc, args.check, problems)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"deployed: {'no differences' if args.check else 'wrote state.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
