#!/usr/bin/env python3
"""Windowed, cached, rate-limited `eth_getLogs` scanner and inventory builder.

B0 / PRE-15 / T-0 input. Read-only; public RPC only; never loads `.env`.

The Pharos public RPC (both chains) rejects `eth_getLogs` ranges above 1,000
blocks, so the scan walks fixed 1,000-block windows. Windows are grouped into
fixed chunks; each chunk is fetched with one JSON-RPC batch request and stored
as one cache file. A run can be interrupted at any point and resumed; already
cached chunks are never re-fetched.

Subcommands
  plan       pin the scan: end block, start block, address set (discovered at
             the pinned block), existence proof that no address had code before
             the start block. Refuses to overwrite an existing plan.
  scan       fetch missing chunks (resumable; --max-requests bounds one run)
  status     coverage of the cached chunks against the plan
  inventory  replay the cached logs and cross-check every derived fact against
             live state at the pinned block: role inventory (all role ids),
             ownership and upgrade histories, vault share holders, StPROS
             withdrawal records. Every check is PASS, FAIL or INCOMPLETE.
  selftest   offline tests of windowing, coverage and replay logic

Completeness rules (never weakened):
  - coverage is COMPLETE only if cached chunks tile [start, end] exactly, all
    chunks carry the plan's filter id, and every address had no code at
    start-1 (so no event predates the scan);
  - a derived inventory is PASS only if coverage is COMPLETE and every live
    cross-check at the pinned block agrees; coverage gaps give INCOMPLETE.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import abi  # noqa: E402
import common  # noqa: E402
import rpc  # noqa: E402
from keccak import keccak256  # noqa: E402

TOOL_VERSION = "1"
WINDOW = 1000  # measured limit on both chains (evidence/onchain-*-2026-10-09.txt)
CHUNK_WINDOWS = 20
CACHE_ROOT = common.repo_path("tools", "release", ".cache", "logscan")
ZERO = "0x" + "0" * 40

T = {name: abi.event_topic(sig) for name, sig in {
    "RoleGranted": "RoleGranted(bytes32,address,address)",
    "RoleRevoked": "RoleRevoked(bytes32,address,address)",
    "RoleAdminChanged": "RoleAdminChanged(bytes32,bytes32,bytes32)",
    "OwnershipTransferred": "OwnershipTransferred(address,address)",
    "Upgraded": "Upgraded(address)",
    "AdminChanged": "AdminChanged(address,address)",
    "ProxyBeaconUpgraded": "BeaconUpgraded(address)",
    "Transfer": "Transfer(address,address,uint256)",
    "Withdraw": "Withdraw(address,address,address,uint256,uint256)",
    "YieldVaultCreated": "YieldVaultCreated(address,address)",
    "BeaconCreated": "BeaconCreated(address,address)",
}.items()}

EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
OZ_INITIALIZABLE = "0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00"
DEFAULT_ADMIN_ROLE = "0x" + "0" * 64
KNOWN_ROLE_NAMES = ["OPERATOR_ROLE", "PROPOSER_ROLE", "EXECUTOR_ROLE", "CANCELLER_ROLE",
                    "GUARDIAN_ROLE", "KEEPER_ROLE", "MINTER_ROLE", "PAUSER_ROLE", "UPGRADER_ROLE"]


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- windowing

def windows(start, end, size=WINDOW):
    out = []
    s = start
    while s <= end:
        e = min(s + size - 1, end)
        out.append((s, e))
        s = e + 1
    return out


def chunks(start, end, per_chunk=CHUNK_WINDOWS, size=WINDOW):
    w = windows(start, end, size)
    return [w[i:i + per_chunk] for i in range(0, len(w), per_chunk)]


def filter_id(addresses):
    return hashlib.sha256(json.dumps(sorted(addresses)).encode()).hexdigest()[:16]


def coverage(plan, chunk_docs):
    """Return (complete, problems, covered_blocks).

    Complete iff the chunks carrying the plan's filter id are exactly the
    expected chunks and each chunk's windows tile it without gap or overlap.
    Chunks from another filter id are reported but never counted."""
    problems = []
    fid = plan["filter_id"]
    docs = [d for d in chunk_docs if d.get("filter_id") == fid]
    other = len(chunk_docs) - len(docs)
    expected = {(c[0][0], c[-1][1]) for c in chunks(plan["start"], plan["end"], plan["chunk_windows"])}
    have = {(d["from"], d["to"]) for d in docs}
    missing = sorted(expected - have)
    extra = sorted(have - expected)
    if missing:
        problems.append(f"{len(missing)} chunks missing, first {missing[:3]}")
    if extra:
        problems.append(f"{len(extra)} unexpected chunks {extra[:3]}")
    covered = 0
    for d in docs:
        if (d["from"], d["to"]) not in expected:
            continue
        cur = d["from"]
        for a, b, _n in sorted(tuple(w) for w in d["windows"]):
            if a != cur or b < a:
                problems.append(f"chunk {d['from']}-{d['to']} windows do not tile at {a}")
                break
            cur = b + 1
        else:
            if cur != d["to"] + 1:
                problems.append(f"chunk {d['from']}-{d['to']} windows end at {cur - 1}")
            else:
                covered += d["to"] - d["from"] + 1
    complete = not problems
    if other:
        problems.append(f"{other} chunk files with a different filter id (ignored)")
    return complete, problems, covered


# ---------------------------------------------------------------- cache io

def chain_dir(chain):
    return os.path.join(CACHE_ROOT, chain)


def plan_path(chain):
    return os.path.join(chain_dir(chain), "plan.json")


def chunk_path(chain, a, b):
    return os.path.join(chain_dir(chain), "chunks", f"{a:010d}-{b:010d}.json")


def load_plan(chain):
    p = plan_path(chain)
    if not os.path.exists(p):
        sys.exit(f"no plan for {chain}; run `logscan.py plan --chain {chain}` first")
    return common.read_json(p)


def load_chunks(chain):
    d = os.path.join(chain_dir(chain), "chunks")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            out.append(common.read_json(os.path.join(d, fn)))
    return out


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- plan

def cmd_plan(args):
    chain = args.chain
    if os.path.exists(plan_path(chain)) and not args.force:
        plan = common.read_json(plan_path(chain))
        print(f"plan exists (pinned end {plan['end']}); use --force to re-pin. Cached chunks with "
              f"the same filter id are reused.")
        return 0
    reg = common.registry()[chain]
    c = rpc.Client(chain, url=args.rpc, min_interval=args.min_interval, verbose=True)
    cid = c.chain_id()
    if cid != reg["chain_id"]:
        sys.exit(f"chain id mismatch: rpc {cid} registry {reg['chain_id']}")
    head = c.block_number()
    end = args.pin if args.pin else head - args.confirmations
    blk = c.call("eth_getBlockByNumber", [hex(end), False])

    labels = {}
    start = None
    for name, rel, rec in common.deployment_records(chain):
        a = rec.get("address")
        if not a:
            continue
        labels.setdefault(a.lower(), set()).add(f"record:{name}")
        bn = (rec.get("receipt") or {}).get("blockNumber")
        if bn is not None:
            bn = int(bn, 16) if isinstance(bn, str) else int(bn)
            start = bn if start is None else min(start, bn)
    labels.setdefault(reg["proxy_admin"], set()).add("proxy_admin")
    vaults = []
    for f in reg["factories"]:
        beacon = abi.dec_addr(c.eth_call(f, abi.calldata("beacon()"), end))
        labels.setdefault(beacon, set()).add(f"beacon_of:{f}")
        n = abi.dec_uint(c.eth_call(f, abi.calldata("totalProxies()"), end))
        for i in range(n):
            v = abi.dec_addr(c.eth_call(f, abi.calldata("allProxies(uint256)", i), end))
            labels.setdefault(v, set()).add(f"vault:{f}[{i}]")
            vaults.append({"factory": f, "index": i, "vault": v})
    addresses = sorted(labels)

    # Existence proof: no address had code at start-1, so no log predates the scan.
    pre = c.batch([("eth_getCode", [a, hex(start - 1)]) for a in addresses])
    predates = [a for a, code in zip(addresses, pre) if code not in ("0x", "0x0", None)]

    plan = {
        "addresses": [{"address": a, "labels": sorted(labels[a])} for a in addresses],
        "chain": chain,
        "chain_id": cid,
        "chunk_windows": CHUNK_WINDOWS,
        "created_utc": utcnow(),
        "end": end,
        "end_block_hash": blk["hash"],
        "end_timestamp": int(blk["timestamp"], 16),
        "filter_id": filter_id(addresses),
        "head_at_plan": head,
        "rpc": c.url,
        "start": start,
        "start_basis": "minimum receipt.blockNumber over deployments/" + chain + "/*.json",
        "addresses_with_code_before_start": predates,
        "tool_version": TOOL_VERSION,
        "vaults": vaults,
        "window": WINDOW,
    }
    common.write_json(plan_path(chain), plan)
    nchunks = len(chunks(start, end, CHUNK_WINDOWS))
    print(f"plan {chain}: blocks {start}..{end} ({end - start + 1} blocks), {len(addresses)} addresses, "
          f"{len(vaults)} vaults, {nchunks} chunks; predating code: {predates or 'none'}")
    return 0


# ---------------------------------------------------------------- scan

def fetch_window_logs(c, addresses, a, b, depth=0):
    """Fetch one window; on a non-retryable range/size error split it."""
    try:
        logs = c.call("eth_getLogs", [{"address": addresses, "fromBlock": hex(a), "toBlock": hex(b)}])
        return [(a, b, len(logs))], logs
    except rpc.RpcError:
        if a == b or depth > 10:
            raise
        mid = (a + b) // 2
        w1, l1 = fetch_window_logs(c, addresses, a, mid, depth + 1)
        w2, l2 = fetch_window_logs(c, addresses, mid + 1, b, depth + 1)
        return w1 + w2, l1 + l2


def slim(log):
    return {k: log[k] for k in ("address", "topics", "data", "blockNumber", "blockHash",
                                "transactionHash", "transactionIndex", "logIndex", "removed") if k in log}


def cmd_scan(args):
    chain = args.chain
    plan = load_plan(chain)
    addresses = [x["address"] for x in plan["addresses"]]
    c = rpc.Client(chain, url=args.rpc or plan["rpc"], min_interval=args.min_interval, verbose=True,
                   transport_retries=1)
    all_chunks = chunks(plan["start"], plan["end"], plan["chunk_windows"])
    todo = [ch for ch in all_chunks if not os.path.exists(chunk_path(chain, ch[0][0], ch[-1][1]))]
    print(f"scan {chain}: {len(all_chunks) - len(todo)}/{len(all_chunks)} chunks cached, {len(todo)} to fetch",
          flush=True)
    t0 = time.monotonic()
    done = 0
    for ch in todo:
        if args.max_requests and c.requests >= args.max_requests:
            print(f"stopping: --max-requests {args.max_requests} reached (resumable)")
            break
        calls = [("eth_getLogs", [{"address": addresses, "fromBlock": hex(a), "toBlock": hex(b)}]) for a, b in ch]
        wins, logs = [], []
        try:
            res = c.batch(calls)
            for (a, b), r in zip(ch, res):
                wins.append([a, b, len(r)])
                logs += r
        except rpc.RpcError as e:
            # fall back to per-window requests (with splitting) for this chunk
            print(f"  chunk {ch[0][0]}-{ch[-1][1]}: batch failed ({e}); per-window fallback", flush=True)
            wins, logs = [], []
            for a, b in ch:
                w, lg = fetch_window_logs(c, addresses, a, b)
                wins += [list(x) for x in w]
                logs += lg
        if any(lg.get("removed") for lg in logs):
            sys.exit(f"chunk {ch[0][0]}-{ch[-1][1]} returned removed logs below the pin; re-plan deeper")
        atomic_write(chunk_path(chain, ch[0][0], ch[-1][1]), {
            "chain": chain, "filter_id": plan["filter_id"], "from": ch[0][0], "to": ch[-1][1],
            "windows": wins, "logs": [slim(lg) for lg in logs], "fetched_utc": utcnow()})
        done += 1
        if done % 10 == 0 or done == len(todo):
            el = time.monotonic() - t0
            eta = el / done * (len(todo) - done)
            print(f"  {done}/{len(todo)} chunks, block {ch[-1][1]}, requests {c.requests}, retries {c.retries}, "
                  f"elapsed {el:.0f}s, eta {eta:.0f}s", flush=True)
    return cmd_status(args)


def cmd_status(args):
    plan = load_plan(args.chain)
    docs = load_chunks(args.chain)
    complete, problems, covered = coverage(plan, docs)
    total = plan["end"] - plan["start"] + 1
    nlogs = sum(len(d["logs"]) for d in docs if d.get("filter_id") == plan["filter_id"])
    print(f"status {args.chain}: blocks {plan['start']}..{plan['end']} covered {covered}/{total} "
          f"({100.0 * covered / total:.2f}%), logs {nlogs}, coverage {'COMPLETE' if complete else 'INCOMPLETE'}")
    for p in problems:
        print("  -", p)
    return 0 if complete else 3


# ---------------------------------------------------------------- inventory

def sorted_logs(plan, docs):
    seen = {}
    for d in docs:
        if d.get("filter_id") != plan["filter_id"]:
            continue
        for lg in d["logs"]:
            key = (lg["transactionHash"], lg["logIndex"])
            seen[key] = lg
    return sorted(seen.values(), key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))


def ev_ref(lg):
    return {"block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"], "log_index": int(lg["logIndex"], 16)}


def replay_roles(logs):
    """Return {contract: {"history": [...], "holders": {role: [acct]}, "admin_changes": [...]}}."""
    out = {}
    for lg in logs:
        t0 = lg["topics"][0]
        if t0 not in (T["RoleGranted"], T["RoleRevoked"], T["RoleAdminChanged"]):
            continue
        a = lg["address"].lower()
        st = out.setdefault(a, {"history": [], "holders": {}, "admin_changes": []})
        if t0 == T["RoleAdminChanged"]:
            st["admin_changes"].append({**ev_ref(lg), "role": lg["topics"][1], "previous_admin_role": lg["topics"][2],
                                        "new_admin_role": lg["topics"][3]})
            continue
        role, acct, sender = lg["topics"][1], abi.topic_addr(lg["topics"][2]), abi.topic_addr(lg["topics"][3])
        ev = "RoleGranted" if t0 == T["RoleGranted"] else "RoleRevoked"
        st["history"].append({**ev_ref(lg), "event": ev, "role": role, "account": acct, "sender": sender})
        hs = st["holders"].setdefault(role, [])
        if ev == "RoleGranted":
            if acct in hs:
                raise ValueError(f"RoleGranted for existing holder {acct} on {a} (OZ never emits this)")
            hs.append(acct)
        else:
            if acct not in hs:
                raise ValueError(f"RoleRevoked for non-holder {acct} on {a} (OZ never emits this)")
            hs.remove(acct)
    for st in out.values():
        st["holders"] = {r: sorted(h) for r, h in st["holders"].items()}
    return out


def replay_balances(logs, token):
    bal = {}
    for lg in logs:
        if lg["address"].lower() != token or lg["topics"][0] != T["Transfer"] or len(lg["topics"]) != 3:
            continue
        f, t, v = abi.topic_addr(lg["topics"][1]), abi.topic_addr(lg["topics"][2]), int(lg["data"], 16)
        if f != ZERO:
            bal[f] = bal.get(f, 0) - v
        if t != ZERO:
            bal[t] = bal.get(t, 0) + v
    return bal


def replay_last(logs, address, topic, decode):
    hist = []
    for lg in logs:
        if lg["address"].lower() == address and lg["topics"][0] == topic:
            hist.append({**ev_ref(lg), **decode(lg)})
    return hist


def role_names(plan_role_ids):
    names = {DEFAULT_ADMIN_ROLE: "DEFAULT_ADMIN_ROLE"}
    for n in KNOWN_ROLE_NAMES:
        names[keccak256(n.encode())] = n
    return {r: names.get(r, "UNKNOWN_ROLE_ID") for r in plan_role_ids}


def status_of(coverage_complete, mismatches):
    if mismatches:
        return "FAIL"
    return "PASS" if coverage_complete else "INCOMPLETE"


def batch_calls(c, items, block):
    """items: list of (to, data). Batched eth_call at the pinned block."""
    out = []
    for i in range(0, len(items), 20):
        part = items[i:i + 20]
        out += c.batch([("eth_call", [{"to": to, "data": data}, hex(block)]) for to, data in part])
    return out


def cmd_inventory(args):
    chain = args.chain
    plan = load_plan(chain)
    reg = common.registry()[chain]
    docs = load_chunks(chain)
    complete, cov_problems, covered = coverage(plan, docs)
    if plan.get("addresses_with_code_before_start"):
        complete = False
        cov_problems.append(f"addresses with code before start: {plan['addresses_with_code_before_start']}")
    logs = sorted_logs(plan, docs)
    pin = plan["end"]
    c = rpc.Client(chain, url=args.rpc or plan["rpc"], min_interval=args.min_interval, verbose=True)
    out_dir = args.out or os.path.join(chain_dir(chain), "inventory")
    labels = {x["address"]: x["labels"] for x in plan["addresses"]}
    summary = {"chain": chain, "chain_id": plan["chain_id"], "pinned_block": pin,
               "pinned_block_hash": plan["end_block_hash"], "pinned_timestamp": plan["end_timestamp"],
               "scan_start": plan["start"], "filter_id": plan["filter_id"], "log_count": len(logs),
               "coverage": "COMPLETE" if complete else "INCOMPLETE", "coverage_problems": cov_problems,
               "covered_blocks": covered, "generated_utc": utcnow(), "checks": {}}

    # pinned block hash must still be canonical
    blk = c.call("eth_getBlockByNumber", [hex(pin), False])
    if blk["hash"] != plan["end_block_hash"]:
        summary["coverage"] = "INCOMPLETE"
        complete = False
        cov_problems.append("pinned block hash changed (reorg); re-plan")

    # --- vault discovery cross-check
    created = []
    for lg in logs:
        if lg["topics"][0] == T["YieldVaultCreated"] and lg["address"].lower() in reg["factories"]:
            created.append({"factory": lg["address"].lower(), "vault": abi.topic_addr(lg["topics"][1]), **ev_ref(lg)})
    from_chain = sorted((v["factory"], v["vault"]) for v in plan["vaults"])
    from_logs = sorted((v["factory"], v["vault"]) for v in created)
    mism = []
    if from_chain != from_logs:
        mism.append({"allProxies": from_chain, "YieldVaultCreated": from_logs})
    for f, n in reg["expected_vault_count"].items():
        got = sum(1 for v in plan["vaults"] if v["factory"] == f)
        if got != n:
            mism.append({"factory": f, "expected_vault_count": n, "allProxies_count": got})
    summary["checks"]["vault_discovery"] = {"status": status_of(complete, mism), "mismatches": mism,
                                            "vaults": plan["vaults"], "created_events": created}

    # --- role inventory (all role ids) for every vault
    roles = replay_roles(logs)
    vault_addrs = [v["vault"] for v in plan["vaults"]]
    role_report = {}
    candidates = sorted(set(reg["accounts"]) | {reg["proxy_admin"]} | set(reg["factories"]))
    op_role = keccak256(b"OPERATOR_ROLE")
    for v in vault_addrs:
        st = roles.get(v, {"history": [], "holders": {}, "admin_changes": []})
        pairs = sorted({(h["role"], h["account"]) for h in st["history"]})
        # every (role, account) ever seen + candidates x {ADMIN, OPERATOR_ROLE}
        probe = sorted(set(pairs) | {(r, a) for r in (DEFAULT_ADMIN_ROLE, op_role) for a in candidates})
        res = batch_calls(c, [(v, abi.calldata("hasRole(bytes32,address)", r, a)) for r, a in probe], pin)
        live = {(r, a): abi.dec_bool(x) for (r, a), x in zip(probe, res)}
        mism = []
        for (r, a), has in live.items():
            exp = a in st["holders"].get(r, [])
            if has != exp:
                mism.append({"role": r, "account": a, "replay": exp, "live_hasRole": has})
        all_roles = sorted({h["role"] for h in st["history"]} | {DEFAULT_ADMIN_ROLE})
        role_report[v] = {
            "labels": labels.get(v, []),
            "status": status_of(complete, mism) if st["history"] else "FAIL",
            "history": st["history"],
            "admin_changes": st["admin_changes"],
            "holders_at_pin": {r: st["holders"].get(r, []) for r in all_roles},
            "role_names": role_names(all_roles),
            "operator_role_hash": op_role,
            "operator_role_holders": st["holders"].get(op_role, []),
            "accounts_ever_holding_any_role": sorted({h["account"] for h in st["history"]}),
            "live_crosscheck_pairs": len(probe),
            "mismatches": mism,
        }
    common.write_json(os.path.join(out_dir, "roles.json"), role_report)
    summary["checks"]["role_inventory"] = {
        "status": "PASS" if all(r["status"] == "PASS" for r in role_report.values()) and complete else
                  ("FAIL" if any(r["status"] == "FAIL" for r in role_report.values()) else "INCOMPLETE"),
        "vaults": {v: {"status": r["status"], "holders_at_pin": r["holders_at_pin"],
                       "events": len(r["history"]), "unknown_role_ids":
                       [k for k, n in r["role_names"].items() if n == "UNKNOWN_ROLE_ID"]}
                   for v, r in role_report.items()}}

    # --- ownership histories (Ownable contracts that emitted OwnershipTransferred)
    owners = {}
    owned = sorted({lg["address"].lower() for lg in logs if lg["topics"][0] == T["OwnershipTransferred"]})
    res = batch_calls(c, [(a, abi.calldata("owner()")) for a in owned], pin)
    for a, x in zip(owned, res):
        hist = replay_last(logs, a, T["OwnershipTransferred"],
                           lambda lg: {"previous": abi.topic_addr(lg["topics"][1]), "new": abi.topic_addr(lg["topics"][2])})
        final = hist[-1]["new"] if hist else None
        live = abi.dec_addr(x)
        mism = [] if final == live else [{"replay_owner": final, "live_owner": live}]
        owners[a] = {"labels": labels.get(a, []), "history": hist, "owner_at_pin": live,
                     "status": status_of(complete, mism), "mismatches": mism}
    common.write_json(os.path.join(out_dir, "owners.json"), owners)
    summary["checks"]["ownership_histories"] = {
        "status": status_of(complete, [m for o in owners.values() for m in o["mismatches"]]),
        "owners_at_pin": {a: o["owner_at_pin"] for a, o in owners.items()}}

    # --- upgrade histories (TUP Upgraded/AdminChanged; beacon Upgraded)
    upgrades = {}
    up_addrs = sorted({lg["address"].lower() for lg in logs if lg["topics"][0] in (T["Upgraded"], T["AdminChanged"])})
    beacons = {a for a, ls in labels.items() if any(x.startswith("beacon_of:") for x in ls)}
    known_impls = {}
    for name, rel, rec in common.deployment_records(chain):
        if rec.get("address"):
            known_impls.setdefault(rec["address"].lower(), []).append(name)
    for a in up_addrs:
        ups = replay_last(logs, a, T["Upgraded"], lambda lg: {"implementation": abi.topic_addr(lg["topics"][1])})
        adm = replay_last(logs, a, T["AdminChanged"],
                          lambda lg: {"previous": "0x" + lg["data"][26:66], "new": "0x" + lg["data"][90:130]})
        if a in beacons:
            live_impl = abi.dec_addr(c.eth_call(a, abi.calldata("implementation()"), pin))
            live_admin = None
        else:
            live_impl = "0x" + c.get_storage(a, EIP1967_IMPL, pin)[-40:]
            live_admin = "0x" + c.get_storage(a, EIP1967_ADMIN, pin)[-40:]
        mism = []
        if not ups or ups[-1]["implementation"] != live_impl:
            mism.append({"replay_impl": ups[-1]["implementation"] if ups else None, "live_impl": live_impl})
        if live_admin is not None and (not adm or adm[-1]["new"] != live_admin):
            mism.append({"replay_admin": adm[-1]["new"] if adm else None, "live_admin": live_admin})
        for u in ups:
            u["deployment_records"] = known_impls.get(u["implementation"], [])
        upgrades[a] = {"labels": labels.get(a, []), "kind": "beacon" if a in beacons else "eip1967_proxy",
                       "upgrades": ups, "admin_changes": adm, "implementation_at_pin": live_impl,
                       "admin_at_pin": live_admin, "status": status_of(complete, mism), "mismatches": mism}
    common.write_json(os.path.join(out_dir, "upgrades.json"), upgrades)
    summary["checks"]["upgrade_histories"] = {
        "status": status_of(complete, [m for u in upgrades.values() for m in u["mismatches"]]),
        "implementations_at_pin": {a: u["implementation_at_pin"] for a, u in upgrades.items()},
        "implementations_not_in_records": sorted({x["implementation"] for u in upgrades.values()
                                                  for x in u["upgrades"] if not x["deployment_records"]})}

    # --- share holders: every vault, plus StPROS
    holders = {}
    tokens = vault_addrs + [reg["stpros"]]
    for tok in tokens:
        bal = replay_balances(logs, tok)
        accts = sorted(bal)
        res = batch_calls(c, [(tok, abi.calldata("balanceOf(address)", a)) for a in accts], pin)
        live = {a: abi.dec_uint(x) for a, x in zip(accts, res)}
        supply = abi.dec_uint(c.eth_call(tok, abi.calldata("totalSupply()"), pin))
        mism = [{"account": a, "replay": bal[a], "live": live[a]} for a in accts if bal[a] != live[a]]
        if sum(bal.values()) != supply:
            mism.append({"sum_replay": sum(bal.values()), "totalSupply": supply})
        nonzero = {a: v for a, v in bal.items() if v}
        holders[tok] = {"labels": labels.get(tok, []), "totalSupply_at_pin": str(supply),
                        "holders": {a: str(v) for a, v in sorted(nonzero.items(), key=lambda kv: -kv[1])},
                        "holder_count": len(nonzero), "accounts_touched": len(accts),
                        "status": status_of(complete, mism), "mismatches": mism[:50],
                        "mismatch_count": len(mism)}
    common.write_json(os.path.join(out_dir, "holders.json"), holders)
    summary["checks"]["share_holders"] = {
        "status": status_of(complete, [m for h in holders.values() for m in h["mismatches"]]),
        "tokens": {t: {"status": h["status"], "holder_count": h["holder_count"],
                       "totalSupply_at_pin": h["totalSupply_at_pin"]} for t, h in holders.items()}}

    # --- withdrawal records (StPROS request queue): Σ pending == Q − C
    st = reg["stpros"]
    req_owners = sorted({abi.topic_addr(lg["topics"][3]) for lg in logs
                         if lg["address"].lower() == st and lg["topics"][0] == T["Withdraw"] and len(lg["topics"]) == 4})
    req_count = sum(1 for lg in logs if lg["address"].lower() == st and lg["topics"][0] == T["Withdraw"])
    res = batch_calls(c, [(st, abi.calldata("getWithdrawals(address)", a)) for a in req_owners], pin)
    ht = batch_calls(c, [(st, abi.calldata(sig, a)) for a in req_owners
                         for sig in ("withdrawalHead(address)", "withdrawalTail(address)")], pin)
    q = abi.dec_uint(c.eth_call(st, abi.calldata("queuedWithdrawal()"), pin))
    cmp_ = abi.dec_uint(c.eth_call(st, abi.calldata("completedWithdrawal()"), pin))
    r_ = abi.dec_uint(c.eth_call(st, abi.calldata("totalCanWithdrawAmount()"), pin))
    records = {}
    total_pending = 0
    mism = []
    for i, (a, x) in enumerate(zip(req_owners, res)):
        recs = abi.dec_tuple_array(x)
        head, tail = abi.dec_uint(ht[2 * i]), abi.dec_uint(ht[2 * i + 1])
        if tail - head != len(recs):
            mism.append({"owner": a, "head": head, "tail": tail, "records": len(recs)})
        if not recs:
            continue
        width = len(recs[0])
        fields = ["queued", "pending", "createdAt", "unbondingPeriod", "receiver"][:width]
        rows = []
        for rr in recs:
            row = {f: str(v) for f, v in zip(fields, rr)}
            if width >= 5:
                row["receiver"] = "0x" + format(rr[4], "040x")
            rows.append(row)
            total_pending += rr[1]
        records[a] = {"head": head, "tail": tail, "records": rows}
    relation = {"Q_queuedWithdrawal": str(q), "C_completedWithdrawal": str(cmp_),
                "R_totalCanWithdrawAmount": str(r_), "Q_minus_C": str(q - cmp_),
                "sum_pending": str(total_pending), "sum_pending_equals_Q_minus_C": total_pending == q - cmp_,
                "R_equals_Q_minus_C": r_ == q - cmp_}
    if total_pending != q - cmp_:
        mism.append({"sum_pending": total_pending, "Q_minus_C": q - cmp_})
    common.write_json(os.path.join(out_dir, "withdrawals.json"), {
        "stpros": st, "request_events": req_count, "request_owners": len(req_owners),
        "owners_with_outstanding_records": len(records), "relation": relation, "records": records,
        "status": status_of(complete, mism), "mismatches": mism})
    summary["checks"]["withdrawal_records"] = {"status": status_of(complete, mism), "relation": relation,
                                               "request_events": req_count, "request_owners": len(req_owners),
                                               "owners_with_outstanding_records": len(records)}

    statuses = [v["status"] for v in summary["checks"].values()]
    summary["overall"] = "FAIL" if "FAIL" in statuses else ("PASS" if all(s == "PASS" for s in statuses) else "INCOMPLETE")
    summary["rpc_requests"] = c.requests
    common.write_json(os.path.join(out_dir, "summary.json"), summary)
    print(f"inventory {chain}: overall {summary['overall']}; " +
          ", ".join(f"{k}={v['status']}" for k, v in summary["checks"].items()))
    print(f"written to {out_dir}")
    return 0 if summary["overall"] == "PASS" else 3


# ---------------------------------------------------------------- selftest

def cmd_selftest(_args):
    assert windows(0, 2499) == [(0, 999), (1000, 1999), (2000, 2499)]
    assert windows(5, 5) == [(5, 5)]
    cs = chunks(0, 45_999, per_chunk=20)
    assert len(cs) == 3 and cs[0][0] == (0, 999) and cs[-1][-1] == (45_000, 45_999)
    plan = {"start": 0, "end": 45_999, "chunk_windows": 20, "filter_id": "f"}

    def doc(ch, fid="f"):
        return {"filter_id": fid, "from": ch[0][0], "to": ch[-1][1], "windows": [[a, b, 0] for a, b in ch]}
    ok, probs, cov = coverage(plan, [doc(c) for c in cs])
    assert ok and cov == 46_000, probs
    ok, probs, _ = coverage(plan, [doc(c) for c in cs[:2]])
    assert not ok and "missing" in probs[0]
    ok, probs, _ = coverage(plan, [doc(cs[0]), doc(cs[1], "other"), doc(cs[2])])
    assert not ok
    gap = doc(cs[0]); gap["windows"] = gap["windows"][1:]
    ok, probs, _ = coverage(plan, [gap, doc(cs[1]), doc(cs[2])])
    assert not ok and "tile" in " ".join(probs)
    # role replay
    def lg(topic, role, acct, blk, idx, addr="0xv"):
        return {"address": addr, "topics": [topic, role, "0x" + acct.rjust(64, "0"), "0x" + "1".rjust(64, "0")],
                "blockNumber": hex(blk), "logIndex": hex(idx), "transactionHash": f"0x{blk:x}{idx:x}", "data": "0x"}
    R = DEFAULT_ADMIN_ROLE
    logs = [lg(T["RoleGranted"], R, "aa", 1, 0), lg(T["RoleGranted"], R, "bb", 2, 0), lg(T["RoleRevoked"], R, "aa", 3, 0)]
    rr = replay_roles(logs)
    assert rr["0xv"]["holders"][R] == ["0x" + "bb".rjust(40, "0")]
    try:
        replay_roles([lg(T["RoleRevoked"], R, "aa", 1, 0)])
        raise AssertionError("revoke of non-holder must raise")
    except ValueError:
        pass
    # balances
    tl = {"address": "0xt", "topics": [T["Transfer"], "0x" + "0" * 64, "0x" + "aa".rjust(64, "0")],
          "data": hex(5), "blockNumber": "0x1", "logIndex": "0x0", "transactionHash": "0x1"}
    assert replay_balances([tl], "0xt") == {"0x" + "aa".rjust(40, "0"): 5}
    assert status_of(True, []) == "PASS" and status_of(False, []) == "INCOMPLETE" and status_of(True, [1]) == "FAIL"
    print("logscan selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "scan", "status", "inventory"):
        p = sub.add_parser(name)
        p.add_argument("--chain", required=True, choices=sorted(common.CHAINS))
        p.add_argument("--rpc", help="override public RPC URL (must be a public, read-only endpoint)")
        p.add_argument("--min-interval", type=float, default=1.0, help="seconds between HTTP requests")
        if name == "plan":
            p.add_argument("--pin", type=int, help="pinned end block (default: head - confirmations)")
            p.add_argument("--confirmations", type=int, default=120)
            p.add_argument("--force", action="store_true", help="re-pin an existing plan")
        if name == "scan":
            p.add_argument("--max-requests", type=int, default=0)
        if name == "inventory":
            p.add_argument("--out", type=common.user_path, help="output directory (default: cache/<chain>/inventory)")
    sub.add_parser("selftest")
    args = ap.parse_args()
    return {"plan": cmd_plan, "scan": cmd_scan, "status": cmd_status, "inventory": cmd_inventory,
            "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
