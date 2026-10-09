#!/usr/bin/env python3
"""Storage-layout snapshots: linear slots and ERC-7201 namespace roots.

  snapshots/storage/<Contract>.json                  HEAD (solc storageLayout + AST)
  snapshots/deployed/<chain>/storage/<Record>.json   recorded storageLayout of live implementations

Namespaces are found from `@custom:storage-location erc7201:<id>` annotations on
structs in the contract's linearized inheritance chain; each root is computed
with an independent Keccak and cross-checked against the `...StorageLocation`
constant declared next to it, when present.

Usage: storage.py [--check]
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
from keccak import erc7201_slot  # noqa: E402

DEPLOYED = {
    "mainnet": ["StPROS_Implementation", "Oracle_Implementation", "YieldVaultFactory_Implementation", "YieldVault"],
    "testnet": ["StPROS_Implementation", "Oracle_Implementation", "BridgeVault_Implementation",
                "SlpDistributor_Implementation", "YieldVaultFactory_Implementation", "YieldVault"],
}
NS_RE = re.compile(r"@custom:storage-location\s+erc7201:([\w.\-]+)")
CONST_RE = re.compile(r"bytes32\s+(?:private|internal)\s+constant\s+(\w+)\s*=\s*(0x[0-9a-fA-F]{64})")


def linear(layout):
    types = layout.get("types") or {}
    rows = []
    for s in layout.get("storage", []):
        t = s["type"]
        rows.append({"slot": int(s["slot"]), "offset": s["offset"], "label": s["label"],
                     "type": types.get(t, {}).get("label", t), "bytes": int(types.get(t, {}).get("numberOfBytes", 0))})
    return sorted(rows, key=lambda r: (r["slot"], r["offset"]))


def structs(layout):
    """Member layout of every struct type reachable from linear storage."""
    out = {}
    for t in (layout.get("types") or {}).values():
        if t.get("encoding") == "inplace" and t.get("members"):
            out[t["label"]] = [{"slot": int(m["slot"]), "offset": m["offset"], "label": m["label"],
                                "type": layout["types"].get(m["type"], {}).get("label", m["type"])}
                               for m in t["members"]]
    return dict(sorted(out.items()))


def ast_index(asts):
    """id -> (node, source key) for every ContractDefinition."""
    idx = {}
    for key, ast in asts.items():
        for node in ast.get("nodes", []):
            if node.get("nodeType") == "ContractDefinition":
                idx[node["id"]] = (node, key)
    return idx


def namespaces(contract_node, idx, sources_text):
    out = []
    for base_id in contract_node.get("linearizedBaseContracts", []):
        base, key = idx[base_id]
        for n in base.get("nodes", []):
            if n.get("nodeType") != "StructDefinition":
                continue
            doc = (n.get("documentation") or {}).get("text", "")
            m = NS_RE.search(doc)
            if not m:
                continue
            ns = m.group(1)
            root = erc7201_slot(ns)
            consts = {c: v.lower() for c, v in CONST_RE.findall(sources_text.get(key, ""))}
            declared = sorted(c for c, v in consts.items() if v == root)
            out.append({"namespace": ns, "root": root, "struct": f"{base['name']}.{n['name']}",
                        "declared_constant": declared})
    return sorted(out, key=lambda x: x["namespace"])


def generate(check, problems):
    head = build.build_head("default")
    resolver = build.Resolver()
    texts = {}
    for job in head["asts"].values():
        for key in job:
            if key not in texts:
                try:
                    texts[key] = open(resolver.key_to_path(key), encoding="utf-8").read()
                except (KeyError, OSError):
                    texts[key] = ""
    written = []
    for fqn, c in sorted(head["contracts"].items()):
        root_key, name = fqn.rsplit(":", 1)
        idx = ast_index(head["asts"][root_key])
        node = next((n for n, k in idx.values() if k == root_key and n["name"] == name), None)
        if node is None or node.get("contractKind") != "contract":
            continue
        ns = namespaces(node, idx, texts)
        for x in ns:
            # The independently computed root must be the constant the source uses.
            if not x["declared_constant"]:
                problems.append(f"{name}: no constant in {x['struct']}'s source equals computed root {x['root']}")
        doc = {"contract": fqn, "source": "HEAD", "abstract": bool(node.get("abstract")),
               "linear": linear(c.get("storageLayout") or {}),
               "structs": structs(c.get("storageLayout") or {}), "namespaces": ns}
        path = common.repo_path("snapshots", "storage", name + ".json")
        common.emit_or_check(path, doc, check, problems)
        written.append(path)
    for chain, recs in DEPLOYED.items():
        for rec_name in recs:
            rel = os.path.join("deployments", chain, rec_name + ".json")
            rec = common.read_json(common.repo_path(rel))
            doc = {"record": rel, "address": rec["address"].lower(), "linear": linear(rec.get("storageLayout") or {}),
                   "structs": structs(rec.get("storageLayout") or {})}
            path = common.repo_path("snapshots", "deployed", chain, "storage", rec_name + ".json")
            common.emit_or_check(path, doc, check, problems)
            written.append(path)
    for d in [common.repo_path("snapshots", "storage")] + [common.repo_path("snapshots", "deployed", ch, "storage") for ch in DEPLOYED]:
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
    print(f"storage: {'no differences' if args.check else 'wrote'} ({len(written)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
