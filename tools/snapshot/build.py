"""Hardhat-faithful solc builds of the HEAD contracts for snapshot generators.

The standard-JSON input mirrors what Hardhat 3 produced for the recorded
deployments (see any deployments/*/*.json metadata): source keys
`project/contracts/...` and `npm/@openzeppelin/<pkg>@<version>/...`, the same
remapping contexts, `useLiteralContent` and `bytecodeHash: ipfs`. A HEAD build
therefore yields byte-identical bytecode to a Hardhat build of the same source,
which `bytecode.py` verifies against the recorded deployments built from HEAD.

One compilation job per contracts/ root file with its import closure, as
Hardhat does. Only solc is executed (no Foundry/Hardhat, so no `.env` loading).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import common  # noqa: E402

SOLC_VERSION = "0.8.28"
PROFILES = {
    # hardhat.config.ts: default = { version: "0.8.28" } (solc defaults: optimizer off)
    "default": {"optimizer": {"enabled": False, "runs": 200}},
    # hardhat.config.ts: production = optimizer enabled, runs 200
    "production": {"optimizer": {"enabled": True, "runs": 200}},
}
IMPORT_RE = re.compile(r'^\s*import\s+(?:[^"\']*?\s+from\s+)?["\']([^"\']+)["\']\s*;', re.M)


def npm_version(pkg):
    return common.read_json(common.repo_path("node_modules", pkg, "package.json"))["version"]


def find_solc():
    for base in (os.path.expanduser("~/Library/Application Support/svm"), os.path.expanduser("~/.svm")):
        p = os.path.join(base, SOLC_VERSION, f"solc-{SOLC_VERSION}")
        if os.path.exists(p):
            return p
    sys.exit(f"solc {SOLC_VERSION} not found in the svm cache")


class Resolver:
    """Maps import strings to Hardhat-style source keys and file paths."""

    def __init__(self):
        self.oz = npm_version("@openzeppelin/contracts")
        self.ozu = npm_version("@openzeppelin/contracts-upgradeable")
        self.npm = {
            "@openzeppelin/contracts/": (f"npm/@openzeppelin/contracts@{self.oz}/", "@openzeppelin/contracts"),
            "@openzeppelin/contracts-upgradeable/": (f"npm/@openzeppelin/contracts-upgradeable@{self.ozu}/",
                                                     "@openzeppelin/contracts-upgradeable"),
        }
        self.remappings = [
            f"npm/@openzeppelin/contracts-upgradeable@{self.ozu}/:@openzeppelin/contracts/=npm/@openzeppelin/contracts@{self.oz}/",
            f"project/:@openzeppelin/contracts-upgradeable/=npm/@openzeppelin/contracts-upgradeable@{self.ozu}/",
            f"project/:@openzeppelin/contracts/=npm/@openzeppelin/contracts@{self.oz}/",
        ]

    def key_to_path(self, key):
        common.refuse_secret_path(key)
        if not key.endswith(".sol") or os.path.normpath(key) != key:
            raise ValueError("invalid Solidity source identity")
        if key.startswith("project/contracts/"):
            return common.repo_path(key[len("project/"):])
        for prefix_key, pkg in self.npm.values():
            if key.startswith(prefix_key):
                return common.repo_path("node_modules", pkg, key[len(prefix_key):])
        raise KeyError(key)

    def resolve(self, from_key, imp):
        if imp.startswith("."):
            return os.path.normpath(os.path.join(os.path.dirname(from_key), imp))
        for prefix, (prefix_key, _pkg) in self.npm.items():
            if imp.startswith(prefix):
                return prefix_key + imp[len(prefix):]
        raise KeyError(f"unresolvable import {imp!r} in {from_key}")

    def closure(self, root_key):
        seen, todo = {}, [root_key]
        while todo:
            k = todo.pop()
            if k in seen:
                continue
            path = Path(self.key_to_path(k))
            common.refuse_secret_path(str(path))
            boundary = Path(common.repo_path("contracts" if k.startswith("project/") else "node_modules")).resolve()
            if not path.resolve().is_relative_to(boundary):
                raise ValueError("source escaped approved boundary")
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            seen[k] = content
            for imp in IMPORT_RE.findall(content):
                todo.append(self.resolve(k, imp))
        return seen


def root_files():
    out = []
    for dirpath, _dirs, files in os.walk(common.repo_path("contracts")):
        for f in files:
            if f.endswith(".sol"):
                out.append("project/" + os.path.relpath(os.path.join(dirpath, f), common.REPO))
    return sorted(out)


def standard_input(root_key, profile, resolver, extra_outputs=()):
    sources = resolver.closure(root_key)
    settings = {
        "evmVersion": "cancun",
        "optimizer": PROFILES[profile]["optimizer"],
        "metadata": {"useLiteralContent": True, "bytecodeHash": "ipfs"},
        "remappings": resolver.remappings,
        "outputSelection": {
            root_key: {"*": ["abi", "storageLayout", "evm.bytecode.object", "evm.deployedBytecode.object",
                             "evm.methodIdentifiers", *extra_outputs]},
            "*": {"": ["ast"]},
        },
    }
    std = {"language": "Solidity", "sources": {k: {"content": v} for k, v in sorted(sources.items())},
           "settings": settings}
    return std


def compile_input(std, solc):
    """Execute only the supplied closure; solc cannot fetch missing imports."""
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([solc, "--standard-json", "--no-import-callback"],
                           input=json.dumps(std).encode(), capture_output=True,
                           cwd=tmp, env={}, check=True)
    out = json.loads(r.stdout)
    errs = [e for e in out.get("errors", []) if e.get("severity") == "error"]
    if errs:
        raise RuntimeError(f"solc error: {errs[0].get('formattedMessage')}")
    return out


def compile_root(root_key, profile, resolver, solc, extra_outputs=()):
    return compile_input(standard_input(root_key, profile, resolver, extra_outputs), solc)


_cache = {}


def build_head(profile="default"):
    """Return {"contracts": {"<root>:<Name>": output}, "asts": {root: {source key: ast}}}.

    AST node ids are only unique within one compilation job, so ASTs are kept
    per root job."""
    if profile in _cache:
        return _cache[profile]
    resolver = Resolver()
    solc = find_solc()
    contracts, asts = {}, {}
    for root in root_files():
        out = compile_root(root, profile, resolver, solc)
        for name, c in out.get("contracts", {}).get(root, {}).items():
            contracts[f"{root}:{name}"] = c
        asts[root] = {k: v["ast"] for k, v in out.get("sources", {}).items()}
    res = {"contracts": contracts, "asts": asts, "solc": solc, "oz": resolver.oz, "ozu": resolver.ozu}
    _cache[profile] = res
    return res


def short_name(fqn):
    return fqn.rsplit(":", 1)[1]


def is_deployable(c):
    return bool(c["evm"]["deployedBytecode"]["object"])
