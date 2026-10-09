#!/usr/bin/env python3
"""ABI snapshots: functions (selector), events (topic0) and errors (selector).

  snapshots/abi/<Contract>.json                 HEAD contracts (contracts/)
  snapshots/deployed/<chain>/abi/<Record>.json  recorded ABIs of the live implementations

Usage: abi.py [--check]   (--check regenerates in memory and fails on any difference)
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

import build  # noqa: E402
import common  # noqa: E402
from keccak import keccak256  # noqa: E402

# Recorded implementation ABIs that are live at the pinned blocks (see fixtures.py).
DEPLOYED = {
    "mainnet": ["StPROS_Implementation", "Oracle_Implementation", "YieldVaultFactory_Implementation",
                "YieldVault", "DefaultProxyAdmin", "StPROS_Proxy"],
    "testnet": ["StPROS_Implementation", "Oracle_Implementation", "BridgeVault_Implementation",
                "SlpDistributor_Implementation", "YieldVaultFactory_Implementation", "YieldVault",
                "DefaultProxyAdmin", "StPROS_Proxy"],
}


def canonical_type(p):
    t = p["type"]
    if t.startswith("tuple"):
        inner = ",".join(canonical_type(c) for c in p["components"])
        return f"({inner}){t[5:]}"
    return t


def signature(item):
    return f"{item['name']}({','.join(canonical_type(i) for i in item.get('inputs', []))})"


def summarize(abi):
    fns, evs, errs, special = [], [], [], []
    for it in abi:
        kind = it["type"]
        if kind == "function":
            sig = signature(it)
            fns.append({"signature": sig, "selector": keccak256(sig.encode())[:10],
                        "stateMutability": it.get("stateMutability"),
                        "outputs": [canonical_type(o) for o in it.get("outputs", [])]})
        elif kind == "event":
            sig = signature(it)
            evs.append({"signature": sig, "topic0": keccak256(sig.encode()), "anonymous": it.get("anonymous", False),
                        "indexed": [bool(i.get("indexed")) for i in it.get("inputs", [])]})
        elif kind == "error":
            sig = signature(it)
            errs.append({"signature": sig, "selector": keccak256(sig.encode())[:10]})
        else:
            special.append({"type": kind, "stateMutability": it.get("stateMutability")})
    key = lambda x: x["signature"]  # noqa: E731
    sels = [f["selector"] for f in fns]
    return {"functions": sorted(fns, key=key), "events": sorted(evs, key=key), "errors": sorted(errs, key=key),
            "special": sorted(special, key=lambda x: x["type"]),
            "selector_collisions": sorted({s for s in sels if sels.count(s) > 1})}


def generate(check, problems):
    head = build.build_head("default")
    written = []
    for fqn, c in sorted(head["contracts"].items()):
        name = build.short_name(fqn)
        doc = {"contract": fqn, "source": "HEAD", **summarize(c["abi"])}
        path = common.repo_path("snapshots", "abi", name + ".json")
        common.emit_or_check(path, doc, check, problems)
        written.append(path)
    for chain, records in DEPLOYED.items():
        for rec_name in records:
            rel = os.path.join("deployments", chain, rec_name + ".json")
            rec = common.read_json(common.repo_path(rel))
            doc = {"record": rel, "address": rec["address"].lower(), "contract": rec.get("contractName"),
                   **summarize(rec["abi"])}
            path = common.repo_path("snapshots", "deployed", chain, "abi", rec_name + ".json")
            common.emit_or_check(path, doc, check, problems)
            written.append(path)
    # unexpected files (a removed contract must fail the check, not linger)
    for d in [common.repo_path("snapshots", "abi")] + [common.repo_path("snapshots", "deployed", ch, "abi") for ch in DEPLOYED]:
        if os.path.isdir(d):
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if p not in written:
                    problems.append(f"UNEXPECTED {os.path.relpath(p, common.REPO)}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    problems = []
    written = generate(args.check, problems)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"abi: {'no differences' if args.check else 'wrote'} ({len(written)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
