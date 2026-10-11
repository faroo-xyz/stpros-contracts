#!/usr/bin/env python3
"""Source provenance of deployed implementations.

Promoted from the research tool `evidence/tools/match.py`. It links every
deployment record to its historical source identity, without ever using the
current HEAD source as a stand-in:

  on-chain code (pinned block)
      == record.deployedBytecode                      (--onchain)
      == solc(metadata.sources, metadata.settings)    (--recompile)
  metadata.sources[*].keccak256 == keccak(literal content)
      -> git blob(s) and the commits that contain them (newest first)
      -> npm sources compared with the vendored reviewed source package

Modes
  --legacy-format           print the original match.py report
  --check-evidence FILE     compare the legacy report with a research evidence
                            file byte for byte (B0 acceptance criterion 1)
  (default)                 write snapshots/deployed/provenance.json
  --check                   regenerate and diff instead of writing

Read-only. RPC is used only with --onchain (public endpoints, pinned blocks
from snapshots/deployed/pins.json). Never reads `.env`.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import common  # noqa: E402
import evm  # noqa: E402
import rpc  # noqa: E402
from keccak import keccak256  # noqa: E402

REPO = common.REPO
PINS_PATH = common.repo_path("snapshots", "deployed", "pins.json")
OUT_PATH = common.repo_path("snapshots", "deployed", "provenance.json")
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


# ------------------------------------------------------------ git index

class GitIndex:
    """keccak(content) -> blob, blob -> [(commit7, path)] over `git rev-list --all`
    for the `contracts` tree, as match.py built it, but restricted to
    `contracts/**/*.sol` blobs: no other historical blob is ever read
    (`git ls-tree` lists names only; `cat-file` runs only on safe paths)."""

    def __init__(self, revs=("--all",)):
        g = lambda *a: common.git(*a)  # noqa: E731
        self.commits = g("rev-list", *revs).decode().split()
        self.idx = {}
        self.skipped_paths = set()
        seen = set()
        for c in self.commits:
            for line in g("ls-tree", "-r", "--full-tree", c, "--", "contracts").decode().splitlines():
                meta, path = line.split("\t")
                if meta.split()[1] != "blob" or not common.is_safe_solidity_source(path):
                    self.skipped_paths.add(path)
                    continue
                blob = meta.split()[2]
                if blob in seen:
                    self.idx.setdefault(("blob", blob), []).append((c[:7], path))
                    continue
                seen.add(blob)
                h = keccak256(g("cat-file", "-p", blob))
                self.idx.setdefault(h, [])
                self.idx[("blob", blob)] = [(c[:7], path)]
                self.idx[h].append(blob)
        self.order = {c[:7]: i for i, c in enumerate(self.commits)}

    def lookup(self, h):
        blobs = self.idx.get(h, [])
        if not blobs:
            return None
        cs = []
        for b in blobs:
            cs += self.idx.get(("blob", b), [])
        cs = sorted(set(cs), key=lambda x: self.order[x[0]])
        return {"blob": blobs[0], "commits_newest_first": [c for c, _ in cs],
                "paths": sorted({p for _, p in cs})}


def metadata_of(rec):
    m = rec.get("metadata")
    if not isinstance(m, str):
        return None
    return json.loads(m)


# ------------------------------------------------------------ legacy report

def legacy_report(gi):
    lines = []
    for f in sorted(glob.glob(REPO + "/deployments/*/*.json")):
        d = common.read_json(f)
        m = metadata_of(d)
        if m is None:
            continue
        srcs = {k: v for k, v in m.get("sources", {}).items() if k.startswith("project/")}
        if not srcs:
            continue
        lines.append(" ".join(["==", f.split("deployments/")[1], str(d.get("address"))]))
        for k, v in srcs.items():
            h = v.get("keccak256")
            r = gi.lookup(h)
            if r is None:
                lines.append(" ".join(["   ", k, h[:12], "NO MATCH IN GIT HISTORY"]))
                continue
            cs = r["commits_newest_first"]
            lines.append(" ".join(["   ", k, h[:12], "blob", r["blob"][:8], "commits newest..oldest:",
                                   repr(cs[:3]), "...", repr(cs[-2:]), str(len(cs))]))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ compiler

def find_solc(version, overrides):
    short = version.split("+")[0]
    if short in overrides:
        return overrides[short]
    for base in (os.path.expanduser("~/Library/Application Support/svm"), os.path.expanduser("~/.svm")):
        p = os.path.join(base, short, f"solc-{short}")
        if os.path.exists(p):
            return p
    return None


_compile_cache = {}


def recompile(meta, solc):
    key = (keccak256(json.dumps(meta, sort_keys=True).encode()), solc)
    if key in _compile_cache:
        return _compile_cache[key]
    settings = dict(meta["settings"])
    target_file, target_name = next(iter(settings.pop("compilationTarget").items()))
    settings["outputSelection"] = {target_file: {target_name: ["evm.bytecode.object", "evm.deployedBytecode.object"]}}
    std = {"language": meta.get("language", "Solidity"),
           "sources": {k: {"content": v["content"]} for k, v in meta["sources"].items()},
           "settings": settings}
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([solc, "--standard-json"], input=json.dumps(std).encode(),
                           capture_output=True, cwd=tmp, check=False)
    out = json.loads(r.stdout)
    errors = [e for e in out.get("errors", []) if e.get("severity") == "error"]
    if errors:
        res = {"error": errors[0].get("formattedMessage", "")[:500]}
    else:
        c = out["contracts"][target_file][target_name]["evm"]
        res = {"creation": c["bytecode"]["object"], "runtime": c["deployedBytecode"]["object"]}
    _compile_cache[key] = res
    return res


def strip0x(h):
    return h[2:] if h.startswith("0x") else h


# ------------------------------------------------------------ npm sources

def npm_installed_path(src_key):
    # npm/@openzeppelin/contracts@5.6.1/token/ERC20/ERC20.sol
    parts = src_key.split("/")
    if parts[0] != "npm":
        return None, None
    if parts[1].startswith("@"):
        pkg, ver = parts[2].rsplit("@", 1)
        name, rest = parts[1] + "/" + pkg, parts[3:]
    else:
        name, ver = parts[1].rsplit("@", 1)
        rest = parts[2:]
    packages = common.read_json(common.repo_path("lib/DEPENDENCIES.json"))["packages"]
    package = packages.get(name)
    if package is None:
        return None, (ver, None)
    base = common.repo_path(package["directory"])
    return os.path.join(base, *rest), (ver, package["version"])



# ------------------------------------------------------------ main analysis

def analyse(gi, solc_overrides, do_recompile, do_onchain, pins):
    report = {"generated_from_head": common.head_commit(), "records": {}}
    clients = {}
    by_runtime = {}
    records = []
    for chain in sorted(common.CHAINS):
        for name, rel, rec in common.deployment_records(chain):
            records.append((chain, name, rel, rec))
            if rec.get("deployedBytecode"):
                by_runtime.setdefault(keccak256(common.hexbytes(rec["deployedBytecode"])), []).append(rel)
    for chain, name, rel, rec in records:
        meta = metadata_of(rec)
        entry = {"record_sha256": common.sha256_file(common.repo_path(rel)), "address": (rec.get("address") or "").lower() or None}
        if meta is None:
            entry["note"] = "no compiler metadata (not a contract deployment record)"
            report["records"][rel] = entry
            continue
        target = next(iter(meta["settings"]["compilationTarget"].items()))
        st = meta["settings"]
        entry.update({
            "contract": f"{target[0]}:{target[1]}",
            "compiler": meta["compiler"]["version"],
            "settings": {"evmVersion": st.get("evmVersion"), "optimizer": st.get("optimizer"),
                         "bytecodeHash": st.get("metadata", {}).get("bytecodeHash"),
                         "useLiteralContent": st.get("metadata", {}).get("useLiteralContent")},
            "metadata_keccak256": keccak256(rec["metadata"].encode()),
            "creation": evm.code_identity(rec["bytecode"]),
            "runtime": evm.code_identity(rec["deployedBytecode"]),
            "args_data": rec.get("argsData"),
            "receipt_block": int(rec["receipt"]["blockNumber"], 16) if isinstance((rec.get("receipt") or {}).get("blockNumber"), str) else None,
            "transaction": (rec.get("transaction") or {}).get("hash"),
        })
        same = sorted(r for r in by_runtime.get(entry["runtime"]["keccak256"], []) if r != rel)
        entry["same_runtime_as"] = same
        sources = {}
        for k, v in meta["sources"].items():
            content = v.get("content")
            s = {"keccak256": v.get("keccak256")}
            s["literal_content_matches_keccak"] = content is not None and keccak256(content.encode()) == v.get("keccak256")
            if k.startswith("project/"):
                s["origin"] = "project"
                g = gi.lookup(v.get("keccak256"))
                if g is None:
                    s["git"] = None
                    s["git_status"] = "NO MATCH IN GIT HISTORY (source recoverable only from metadata literal content)"
                else:
                    # Only history-stable facts: the blob and the commit that
                    # introduced it (newer commits would churn the snapshot).
                    s["git"] = {"blob": g["blob"], "introduced_in": g["commits_newest_first"][-1]}
                    s["git_status"] = "MATCH"
            elif k.startswith("npm/"):
                s["origin"] = "npm"
                p, (ver, installed) = npm_installed_path(k)
                s["npm_version"] = ver
                if installed == ver and p and os.path.exists(p):
                    s["installed_match"] = keccak256(open(p, "rb").read()) == v.get("keccak256")
                else:
                    s["installed_match"] = None
            else:
                s["origin"] = "vendored:" + k.split("/")[0]
            sources[k] = s
        entry["sources"] = sources
        if do_recompile:
            solc = find_solc(meta["compiler"]["version"], solc_overrides)
            if solc is None:
                entry["recompile"] = {"status": "NOT_RUN", "reason": f"solc {meta['compiler']['version']} not available"}
            else:
                out = recompile(meta, solc)
                if "error" in out:
                    entry["recompile"] = {"status": "ERROR", "error": out["error"]}
                else:
                    ce = out["creation"] == strip0x(rec["bytecode"])
                    re_ = out["runtime"] == strip0x(rec["deployedBytecode"])
                    entry["recompile"] = {"status": "MATCH" if ce and re_ else "MISMATCH",
                                          "creation_equal": ce, "runtime_equal": re_,
                                          "solc_sha256": common.sha256_file(solc)}
        if do_onchain and entry["address"]:
            c = clients.setdefault(chain, rpc.Client(chain, min_interval=0.4))
            pin = pins[chain]
            code = c.get_code(entry["address"], pin)
            code_k = keccak256(common.hexbytes(code))
            oc = {"block": pin, "code_keccak256": code_k, "code_size": len(common.hexbytes(code)),
                  "equals_record_runtime": code_k == entry["runtime"]["keccak256"]}
            if not oc["equals_record_runtime"]:
                impl = "0x" + c.get_storage(entry["address"], EIP1967_IMPL, pin)[-40:]
                oc["eip1967_implementation"] = impl
                oc["code_matches_records"] = sorted(by_runtime.get(code_k, []))
                oc["note"] = ("record describes the implementation ABI/bytecode behind this proxy; on-chain code "
                              "is the proxy" if impl != "0x" + "0" * 40 else "MISMATCH")
            entry["onchain"] = oc
        report["records"][rel] = entry
    return report


def implementation_history(pins, inventories):
    """For every implementation address in the inventories' upgrade histories,
    fetch its code at the pin and match it against deployment records."""
    by_runtime = {}
    for chain in common.CHAINS:
        for name, rel, rec in common.deployment_records(chain):
            if rec.get("deployedBytecode"):
                by_runtime.setdefault(keccak256(common.hexbytes(rec["deployedBytecode"])), []).append(rel)
    out = {}
    for chain, path in inventories.items():
        if not os.path.exists(path):
            out[chain] = {"status": "NOT_RUN", "reason": f"{path} missing"}
            continue
        ups = common.read_json(path)
        c = rpc.Client(chain, min_interval=0.4)
        ch = {}
        for proxy, u in sorted(ups.items()):
            rows = []
            for x in u["upgrades"]:
                code = c.get_code(x["implementation"], pins[chain])
                k = keccak256(common.hexbytes(code))
                rows.append({"block": x["block"], "implementation": x["implementation"], "code_keccak256": k,
                             "code_size": len(common.hexbytes(code)),
                             "matching_records": sorted(by_runtime.get(k, []))})
            ch[proxy] = {"labels": u["labels"], "kind": u["kind"], "history": rows}
        out[chain] = ch
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy-format", action="store_true")
    ap.add_argument("--check-evidence", type=common.user_path)
    ap.add_argument("--revs", nargs="+", default=["--all"],
                    help="git revisions to index (default --all, as match.py); pass the ref tips "
                         "recorded in the B0 report to reproduce the 2026-10-09 evidence after HEAD moves")
    ap.add_argument("--recompile", action="store_true")
    ap.add_argument("--onchain", action="store_true")
    ap.add_argument("--history", action="store_true",
                    help="match every historical implementation from the logscan upgrade inventories")
    ap.add_argument("--solc", action="append", default=[], metavar="VERSION=PATH")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=OUT_PATH, type=common.user_path)
    args = ap.parse_args()

    gi = GitIndex(args.revs)
    if args.legacy_format or args.check_evidence:
        text = legacy_report(gi)
        if args.check_evidence:
            common.refuse_secret_path(args.check_evidence)
            want = open(args.check_evidence).read()
            if want == text:
                print(f"legacy provenance report reproduces {args.check_evidence} byte for byte "
                      f"({len(text.splitlines())} lines)")
                return 0
            import difflib
            sys.stdout.writelines(difflib.unified_diff(want.splitlines(True), text.splitlines(True),
                                                       "evidence", "regenerated"))
            return 1
        sys.stdout.write(text)
        return 0

    overrides = dict(s.split("=", 1) for s in args.solc)
    for path in overrides.values():
        common.user_path(path)
    pins = common.read_json(PINS_PATH) if os.path.exists(PINS_PATH) else {}
    if (args.onchain or args.history) and not pins:
        sys.exit(f"--onchain/--history need pinned blocks in {PINS_PATH}")
    rep = analyse(gi, overrides, args.recompile, args.onchain, pins)
    rep["modes"] = {"recompile": args.recompile, "onchain": args.onchain, "history": args.history}
    rep["pins"] = pins if (args.onchain or args.history) else None
    rep.pop("generated_from_head")  # keep the snapshot independent of unrelated commits
    if args.history:
        inv = {ch: common.repo_path("tools", "release", ".cache", "logscan", ch, "inventory", "upgrades.json")
               for ch in common.CHAINS}
        rep["implementation_history"] = implementation_history(pins, inv)
    problems = []
    common.emit_or_check(args.out, rep, args.check, problems)
    # summary
    recs = [r for r in rep["records"].values() if "contract" in r]
    rc = [r.get("recompile", {}).get("status") for r in recs]
    oc = [r.get("onchain", {}).get("equals_record_runtime") for r in recs if "onchain" in r]
    nomatch = sorted({k for r in recs for k, s in r["sources"].items() if s.get("git_status", "").startswith("NO MATCH")})
    npm_bad = sorted({k for r in recs for k, s in r["sources"].items() if s.get("installed_match") is False})
    lit_bad = sorted({k for r in recs for k, s in r["sources"].items() if not s["literal_content_matches_keccak"]})
    print(f"records with metadata: {len(recs)}; recompile: " +
          ", ".join(f"{s}={rc.count(s)}" for s in sorted(set(map(str, rc)))))
    if oc:
        print(f"on-chain code == record runtime: {sum(1 for x in oc if x)}/{len(oc)} (others are proxy-address records)")
    print(f"project sources with no git match: {nomatch or 'none'}")
    print(f"npm sources differing from installed packages: {npm_bad or 'none'}")
    print(f"sources whose literal content does not hash to the metadata keccak: {lit_bad or 'none'}")
    if args.check:
        print("\n".join(problems) if problems else "provenance --check: no differences")
        return 1 if problems else 0
    print(f"wrote {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
