#!/usr/bin/env python3
"""Capability snapshot (structural, evidence class ST): who can call what.

For every HEAD contract: each externally callable function (including inherited
ones; most-derived definition wins) with its selector, mutability, modifiers,
a derived authority class, and flags (pausable, nonReentrant, payable,
references msg.sender in its body). Initializers without any access modifier
are classed `initializer-unauthenticated`.

For HEAD builds and the recorded live implementations: a structural opcode
scan (DELEGATECALL / CALLCODE / SELFDESTRUCT) and presence of upgrade-path
selectors. The opcode scan is linear disassembly of the executable code with
CBOR metadata stripped; it is structural, not a semantic proof.

  snapshots/capabilities.json

Usage: capabilities.py [--check]
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

import build  # noqa: E402
import common  # noqa: E402
import evm  # noqa: E402
from keccak import selector  # noqa: E402

UPGRADE_SELECTORS = {
    selector(s): s for s in [
        "upgradeTo(address)", "upgradeToAndCall(address,bytes)", "changeAdmin(address)",
        "changeProxyAdmin(address,address)", "upgrade(address,address)", "upgradeAndCall(address,address,bytes)",
        "upgradeBeaconTo(address)", "renounceOwnership()", "transferOwnership(address)",
    ]
}
DEPLOYED = {
    "mainnet": ["StPROS_Implementation", "Oracle_Implementation", "YieldVaultFactory_Implementation", "YieldVault",
                "DefaultProxyAdmin", "StPROS_Proxy"],
    "testnet": ["StPROS_Implementation", "Oracle_Implementation", "BridgeVault_Implementation",
                "SlpDistributor_Implementation", "YieldVaultFactory_Implementation", "YieldVault"],
}
SENDER_RE = re.compile(r"\bmsg\.sender\b|\b_msgSender\(\)")
PAUSE_MODS = {"whenNotPaused", "whenPaused", "whenFactoryNotPaused"}
INIT_MODS = {"initializer", "reinitializer", "onlyInitializing"}


def src_slice(text, src):
    start, length, _ = (int(x) for x in src.split(":"))
    return text.encode()[start:start + length].decode(errors="replace")


def authority(mods, mutability, kind, body_sender):
    names = [m["name"] for m in mods]
    if mutability in ("view", "pure"):
        return "view"
    auth = []
    for m in mods:
        if m["name"] == "onlyOwner":
            auth.append("owner")
        elif m["name"] == "onlyRole":
            auth.append(f"role:{m['args']}")
        elif m["name"].startswith("only") and m["name"] not in ("onlyInitializing",):
            auth.append(m["name"][4:].lower() or m["name"])
    if any(n in INIT_MODS for n in names):
        return "initializer" + ("" if auth else "-unauthenticated") + ("+" + "+".join(auth) if auth else "")
    if auth:
        return "+".join(auth)
    if kind in ("receive", "fallback"):
        return kind + (" (inline sender check)" if body_sender else "")
    return "inline-sender-check" if body_sender else "public"


def contract_functions(contract_node, idx, texts):
    seen = {}
    for base_id in contract_node["linearizedBaseContracts"]:
        base, key = idx[base_id]
        text = texts.get(key, "")
        for n in base.get("nodes", []):
            if n.get("nodeType") != "FunctionDefinition":
                continue
            kind = n.get("kind")
            if kind == "constructor":
                continue
            if kind == "function" and n.get("visibility") not in ("public", "external"):
                continue
            if kind == "function" and not n.get("implemented", True):
                continue
            ident = n.get("functionSelector") or kind
            if ident in seen:
                continue
            mods = []
            for m in n.get("modifiers", []):
                nm = m["modifierName"].get("name") or m["modifierName"].get("namePath")
                args = ",".join(src_slice(text, a["src"]) for a in (m.get("arguments") or []))
                mods.append({"name": nm, "args": args})
            body = src_slice(text, n["body"]["src"]) if n.get("body") else ""
            body_sender = bool(SENDER_RE.search(body))
            params = ",".join(p["typeDescriptions"]["typeString"].replace("contract ", "").replace("struct ", "")
                              for p in n["parameters"]["parameters"])
            seen[ident] = {
                "selector": ("0x" + n["functionSelector"]) if n.get("functionSelector") else None,
                "name": n.get("name") or kind,
                "params": params,
                "kind": kind,
                "stateMutability": n.get("stateMutability"),
                "defined_in": base["name"],
                "modifiers": [m["name"] + (f"({m['args']})" if m["args"] else "") for m in mods],
                "authority": authority(mods, n.get("stateMutability"), kind, body_sender),
                "pausable": any(m["name"] in PAUSE_MODS for m in mods),
                "nonReentrant": any(m["name"] == "nonReentrant" for m in mods),
                "body_references_sender": body_sender,
            }
    return sorted(seen.values(), key=lambda r: (r["kind"], r["name"], r["params"]))


def code_scan(runtime_hex, abi_selectors):
    code = common.hexbytes(runtime_hex)
    ops = evm.opcode_presence(code)
    return {"forbidden_opcodes_present": {k: v for k, v in ops.items() if v},
            "upgrade_path_selectors": sorted(UPGRADE_SELECTORS[s] for s in abi_selectors if s in UPGRADE_SELECTORS)}


def generate():
    head = build.build_head("default")
    prod = build.build_head("production")
    resolver = build.Resolver()
    doc = {"head": {}, "deployed": {}}
    for fqn, c in sorted(head["contracts"].items()):
        root_key, name = fqn.rsplit(":", 1)
        asts = head["asts"][root_key]
        idx = {}
        for key, ast in asts.items():
            for node in ast.get("nodes", []):
                if node.get("nodeType") == "ContractDefinition":
                    idx[node["id"]] = (node, key)
        texts = {}
        for key in asts:
            try:
                texts[key] = open(resolver.key_to_path(key), encoding="utf-8").read()
            except (KeyError, OSError):
                texts[key] = ""
        node = next((n for n, k in idx.values() if k == root_key and n["name"] == name), None)
        if node is None or node.get("contractKind") != "contract":
            continue
        fns = contract_functions(node, idx, texts)
        entry = {"contract": fqn, "abstract": bool(node.get("abstract")), "functions": fns}
        if build.is_deployable(c):
            sels = ["0x" + s for s in c["evm"]["methodIdentifiers"].values()]
            entry["code_scan_default_profile"] = code_scan(c["evm"]["deployedBytecode"]["object"], sels)
            entry["code_scan_production_profile"] = code_scan(prod["contracts"][fqn]["evm"]["deployedBytecode"]["object"], sels)
        entry["unauthenticated_state_changing"] = sorted(
            f"{f['name']}({f['params']})" for f in fns
            if f["authority"] in ("public", "initializer-unauthenticated") and f["kind"] == "function"
            and f["stateMutability"] not in ("view", "pure"))
        doc["head"][name] = entry
    for chain, recs in DEPLOYED.items():
        for rec_name in recs:
            rel = os.path.join("deployments", chain, rec_name + ".json")
            rec = common.read_json(common.repo_path(rel))
            sels = []
            for it in rec["abi"]:
                if it["type"] == "function":
                    sig = f"{it['name']}({','.join(i['type'] for i in it['inputs'])})"
                    sels.append(selector(sig))
            doc["deployed"][f"{chain}/{rec_name}"] = {"record": rel, "address": rec["address"].lower(),
                                                      **code_scan(rec["deployedBytecode"], sels)}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    problems = []
    common.emit_or_check(common.repo_path("snapshots", "capabilities.json"), generate(), args.check, problems)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"capabilities: {'no differences' if args.check else 'wrote snapshots/capabilities.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
