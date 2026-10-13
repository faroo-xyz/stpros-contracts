#!/usr/bin/env python3
"""Legacy bytecode fixtures for tests (tests/fixtures/legacy/<chain>/*.json).

Fixtures are extracted from the *recorded deployment artifacts* of the
implementations that are live on each chain (never from the current HEAD
source), plus the runtime bytecode of the real WPROS contract read from the
chain at the pinned block. Each fixture carries its provenance:

  - source record path and sha256,
  - creation/runtime keccak (runtime also with CBOR metadata stripped),
  - the git identity of every project source (blob, introducing commit) or
    "metadata-only" when no committed blob matches,
  - the on-chain code keccak at the pinned block and whether it equals the
    recorded runtime (the address must currently be the live implementation).

Usage
  fixtures.py            regenerate all fixtures (needs public RPC for on-chain identity)
  fixtures.py --check    regenerate in memory and diff against the files
  fixtures.py --offline  skip RPC: reuse the on-chain section stored in each
                         existing fixture, re-derive everything else (with
                         --check this is the fast, network-free CI mode)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import common  # noqa: E402
import evm  # noqa: E402
import rpc  # noqa: E402
from keccak import keccak256  # noqa: E402

OUT = common.repo_path("tests", "fixtures", "legacy")
PINS = common.read_json(common.repo_path("snapshots", "deployed", "pins.json"))
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# name -> (record, role). Every implementation record here must be the live
# implementation of its proxy or beacon at the pinned block (checked).
FIXTURES = {
    "mainnet": {
        "StPROS_V1": ("StPROS_Implementation", "live implementation of StPROS proxy 0x6b0a44c6"),
        "Oracle_V1": ("Oracle_Implementation", "live implementation of Oracle proxy 0x051713fd"),
        "YieldVaultFactory_V1": ("YieldVaultFactory_Implementation",
                                 "live implementation of YieldVaultFactory 0xc9fb7dc5 (Factory_test 0xbc3a9090 runs identical runtime 0x9d40ca58)"),
        "YieldVault_V1": ("YieldVault", "main beacon 0x8618bfb5 implementation (beacon B2 0xb20f0409 runs identical runtime 0x2b2ef1f0)"),
        "ProxyAdmin_V4": ("DefaultProxyAdmin", "OZ v4 ProxyAdmin (hardhat-deploy solc_0.8) owning all TUPs"),
        "TransparentUpgradeableProxy_V4": ("StPROS_Proxy", "OZ v4 TransparentUpgradeableProxy (identical creation and runtime for every TUP on both chains)"),
    },
    "testnet": {
        "StPROS_V3": ("StPROS_Implementation", "live implementation of StPROS proxy 0xb1437aee"),
        "Oracle_V3": ("Oracle_Implementation", "live implementation of Oracle proxy 0x6bd39d03"),
        "BridgeVault_V3": ("BridgeVault_Implementation", "live implementation of BridgeVault proxy 0xb79db650 (pre-cc73de9, blob be37b4a5)"),
        "SlpDistributor_V3": ("SlpDistributor_Implementation", "live implementation of SlpDistributor proxy 0x008a7856"),
        "YieldVaultFactory_V3": ("YieldVaultFactory_Implementation", "live implementation of YieldVaultFactory 0x558de0e2 (runtime 36,576 bytes, above EIP-170)"),
        "YieldVault_V3": ("YieldVault", "beacon 0xaa2f4320 implementation (uncommitted source with debug setters; source only in metadata)"),
        "ProxyAdmin_V4": ("DefaultProxyAdmin", "OZ v4 ProxyAdmin"),
        "TransparentUpgradeableProxy_V4": ("StPROS_Proxy", "OZ v4 TransparentUpgradeableProxy"),
    },
}
WPROS_STATIC_SLOTS = 10  # name/symbol/decimals live in low slots on both WPROS contracts


def project_source_identity(meta, gi):
    out = {}
    for k, v in meta["sources"].items():
        if not k.startswith("project/"):
            continue
        g = gi.lookup(v["keccak256"]) if gi else None
        out[k] = {"keccak256": v["keccak256"],
                  "git_blob": g["blob"] if g else None,
                  "introduced_in": g["commits_newest_first"][-1] if g else None,
                  "identity": "git" if g else "metadata-only (no committed blob matches)"}
    return out


def build(chain, name, record, role, gi, client, existing):
    rel = os.path.join("deployments", chain, record + ".json")
    rec = common.read_json(common.repo_path(rel))
    import json
    meta = json.loads(rec["metadata"])
    target = next(iter(meta["settings"]["compilationTarget"].items()))
    creation, runtime = rec["bytecode"], rec["deployedBytecode"]
    fx = {
        "name": name,
        "chain": chain,
        "chainId": common.CHAINS[chain]["chain_id"],
        "role": role,
        "sourceRecord": rel,
        "sourceRecordSha256": common.sha256_file(common.repo_path(rel)),
        "recordedAddress": rec["address"].lower(),
        "contract": f"{target[0]}:{target[1]}",
        "compiler": meta["compiler"]["version"],
        "optimizer": meta["settings"].get("optimizer"),
        "evmVersion": meta["settings"].get("evmVersion"),
        "creationCode": creation,
        "creationKeccak": keccak256(common.hexbytes(creation)),
        "runtimeCode": runtime,
        "runtimeKeccak": keccak256(common.hexbytes(runtime)),
        "runtimeSize": len(common.hexbytes(runtime)),
        "runtimeKeccakWithoutMetadata": evm.code_identity(runtime)["keccak256_without_metadata"],
        "argsData": rec.get("argsData"),
        "projectSources": project_source_identity(meta, gi),
    }
    fx["onchain"] = onchain_identity(chain, fx["recordedAddress"], fx["runtimeKeccak"], client, existing)
    return fx


def onchain_identity(chain, address, runtime_keccak, client, existing):
    if client is None:
        if existing and "onchain" in existing:
            return existing["onchain"]
        return {"status": "NOT_RUN"}
    pin = PINS[chain]
    code = client.get_code(address, pin)
    k = keccak256(common.hexbytes(code))
    return {"block": pin, "address": address, "codeKeccak": k,
            "status": "MATCH" if k == runtime_keccak else "MISMATCH"}


def build_wpros(chain, client, existing):
    reg = common.registry()[chain]
    addr = reg["wpros"]
    if client is None:
        if not existing:
            raise SystemExit(f"--offline needs an existing WPROS fixture for {chain}")
        return existing
    pin = PINS[chain]
    code = client.get_code(addr, pin)
    b = common.hexbytes(code)
    storage = []
    for s in range(WPROS_STATIC_SLOTS):
        v = client.get_storage(addr, hex(s), pin)
        if int(v, 16):
            storage.append({"slot": "0x" + format(s, "064x"), "value": "0x" + v[2:].rjust(64, "0")})
    body, cbor = evm.split_metadata(b)
    return {
        "name": "WPROS",
        "chain": chain,
        "chainId": common.CHAINS[chain]["chain_id"],
        "role": "real WPROS runtime read from chain (no deployment record exists); static storage slots copied so name/symbol/decimals read as on-chain",
        "address": addr,
        "block": pin,
        "runtimeCode": code,
        "runtimeKeccak": keccak256(b),
        "runtimeSize": len(b),
        "cborMetadata": evm.parse_cbor_metadata(cbor) if cbor else {},
        "staticStorage": storage,
        "nativeTransferPattern": "transfer (2300-gas stipend: PUSH2 0x08fc * iszero(value))"
        if any(n == "PUSH2" and d == bytes.fromhex("08fc") for _, n, d in evm.instructions(b)) else "unknown",
    }


def live_implementation_check(chain, client):
    """Every *_V* implementation fixture must be the live implementation now."""
    if client is None:
        return []
    reg = common.registry()[chain]
    pin = PINS[chain]
    problems = []
    for proxy, exp in reg["expected_proxies"].items():
        impl = "0x" + client.get_storage(proxy, EIP1967_IMPL, pin)[-40:]
        if impl != exp["implementation"]:
            problems.append(f"{chain} proxy {proxy} implementation {impl} != registry {exp['implementation']}")
    for beacon, exp in reg["expected_beacons"].items():
        import abi
        impl = abi.dec_addr(client.eth_call(beacon, abi.calldata("implementation()"), pin))
        if impl != exp["implementation"]:
            problems.append(f"{chain} beacon {beacon} implementation {impl} != registry {exp['implementation']}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from provenance import GitIndex
    gi = GitIndex()
    problems = []
    manifest = {"pins": PINS, "fixtures": {}}
    for chain, items in FIXTURES.items():
        client = None if args.offline else rpc.Client(chain, min_interval=1.2)
        problems += live_implementation_check(chain, client)
        for name, (record, role) in items.items():
            path = os.path.join(OUT, chain, name + ".json")
            existing = common.read_json(path) if os.path.exists(path) else None
            fx = build(chain, name, record, role, gi, client, existing)
            if fx["onchain"].get("status") == "MISMATCH" and not name.startswith(("TransparentUpgradeableProxy", "ProxyAdmin")):
                problems.append(f"{chain}/{name}: recorded runtime != on-chain code at {fx['recordedAddress']}")
            common.emit_or_check(path, fx, args.check, problems)
            manifest["fixtures"][f"{chain}/{name}"] = {
                "runtimeKeccak": fx["runtimeKeccak"], "creationKeccak": fx["creationKeccak"],
                "onchain": fx["onchain"].get("status"), "sourceRecord": fx["sourceRecord"]}
        path = os.path.join(OUT, chain, "WPROS.json")
        existing = common.read_json(path) if os.path.exists(path) else None
        wp = build_wpros(chain, client, existing)
        if keccak256(common.hexbytes(wp["runtimeCode"])) != wp["runtimeKeccak"]:
            problems.append(f"{chain}/WPROS: stored runtime does not hash to runtimeKeccak")
        common.emit_or_check(path, wp, args.check, problems)
        manifest["fixtures"][f"{chain}/WPROS"] = {"runtimeKeccak": wp["runtimeKeccak"], "onchain": "READ_AT_PIN",
                                                  "address": wp["address"]}
    common.emit_or_check(os.path.join(OUT, "manifest.json"), manifest, args.check, problems)
    if problems:
        print("\n".join(problems))
        return 1
    print("fixtures --check: no differences" if args.check else f"wrote fixtures under {os.path.relpath(OUT, common.REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
