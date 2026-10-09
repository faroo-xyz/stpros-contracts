#!/usr/bin/env python3
"""Bytecode snapshot: sizes, EIP-170/EIP-3860 headroom and code identity of the
HEAD contracts under both Hardhat profiles, plus which recorded deployments are
byte-identical to a HEAD build (this also validates that the snapshot build is
Hardhat-faithful).

  snapshots/bytecode.json

Usage: bytecode.py [--check]
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

import build  # noqa: E402
import common  # noqa: E402
import evm  # noqa: E402
from keccak import keccak256  # noqa: E402

EIP170 = 24_576
EIP3860 = 49_152


def recorded_runtimes():
    """(exact runtime keccak -> records, runtime-without-CBOR keccak -> records)."""
    exact, body = {}, {}
    for chain in common.CHAINS:
        for name, rel, rec in common.deployment_records(chain):
            if rec.get("deployedBytecode") and rec.get("metadata"):
                ident = evm.code_identity(rec["deployedBytecode"])
                exact.setdefault(ident["keccak256"], []).append(rel)
                body.setdefault(ident["keccak256_without_metadata"], []).append(rel)
    return exact, body


def generate():
    recorded, recorded_body = recorded_runtimes()
    doc = {"limits": {"eip170_runtime": EIP170, "eip3860_initcode": EIP3860}, "profiles": {}}
    for profile in ("default", "production"):
        head = build.build_head(profile)
        rows = {}
        for fqn, c in sorted(head["contracts"].items()):
            if not build.is_deployable(c):
                continue
            runtime = bytes.fromhex(c["evm"]["deployedBytecode"]["object"])
            initcode = bytes.fromhex(c["evm"]["bytecode"]["object"])
            ident = evm.code_identity(runtime.hex())
            k = ident["keccak256"]
            rows[build.short_name(fqn)] = {
                "contract": fqn,
                "runtime_size": len(runtime),
                "eip170_headroom": EIP170 - len(runtime),
                "initcode_size": len(initcode),
                "eip3860_headroom": EIP3860 - len(initcode),
                "runtime_keccak256": k,
                "runtime_keccak256_without_metadata": ident["keccak256_without_metadata"],
                "initcode_keccak256": keccak256(initcode),
                "identical_to_recorded_deployments": sorted(recorded.get(k, [])),
                # Same executable code, different CBOR metadata hash (e.g. Hardhat added a
                # forge-std remapping when the build also compiled test files).
                "identical_except_metadata_to_recorded_deployments": sorted(
                    set(recorded_body.get(ident["keccak256_without_metadata"], [])) - set(recorded.get(k, []))),
            }
        doc["profiles"][profile] = rows
    doc["compiler"] = build.SOLC_VERSION
    doc["openzeppelin"] = {"contracts": head["oz"], "contracts-upgradeable": head["ozu"]}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    problems = []
    path = common.repo_path("snapshots", "bytecode.json")
    doc = generate()
    over = [f"{p}/{n}" for p, rows in doc["profiles"].items() for n, r in rows.items() if r["eip170_headroom"] < 0]
    common.emit_or_check(path, doc, args.check, problems)
    if over:
        print(f"note: above EIP-170 in some profile: {over}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"bytecode: {'no differences' if args.check else 'wrote snapshots/bytecode.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
