#!/usr/bin/env python3
"""CB-1 fork-feasibility and gas-estimation probe (B0). Read-only toward the network.

1. Starts `anvil --fork-url <public RPC> --fork-block-number <pin>` inside a fresh
   .env-free export (tools/verify/export.sh + run.sh: empty environment), with a
   compute-unit budget so the provider's rate limit is respected.
2. Checks fork fidelity: chain id, block, and a set of storage/eth_call reads equal
   to the same reads on the public archive node at the pinned block.
3. eth_estimateGas, on the fork and on the public node, for handoff-shaped calls
   from their real authorities (ProxyAdmin no-op upgrade, ownership transfers,
   vault role grant/renounce) and for deploying the HEAD StPROS implementation.
4. On the LOCAL fork only: impersonates the Safe and executes the no-op upgrade
   (same implementation) to record actual gasUsed. Nothing is ever sent to a
   public endpoint except read-only calls (eth_call/eth_estimateGas/eth_get*).

Usage: fork_probe.py --chain mainnet [--out FILE] [--port 8547]
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.append(os.path.join(HERE, "..", "snapshot"))  # after lib: snapshot/abi.py must not shadow lib/abi.py

import abi  # noqa: E402
import common  # noqa: E402
import rpc  # noqa: E402

PINS = common.read_json(common.repo_path("snapshots", "deployed", "pins.json"))
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
DEAD = "0x000000000000000000000000000000000000dEaD"


class Local:
    """JSON-RPC to the local anvil fork; refuses any non-loopback URL."""

    def __init__(self, port):
        self.url = f"http://127.0.0.1:{port}"

    def call(self, method, params):
        assert self.url.startswith("http://127.0.0.1:")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
        if "error" in out:
            raise rpc.RpcError(f"{method}: {out['error'].get('message')}")
        return out["result"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def probe(chain, port, out_path):
    reg = common.registry()[chain]
    pin = PINS[chain]
    url = rpc.PUBLIC_RPC[chain]
    live = rpc.Client(chain, min_interval=1.2, verbose=True)
    result = {"chain": chain, "pin": pin, "rpc": url, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    tmp = tempfile.mkdtemp(prefix="faroo-fork-")
    dest = os.path.join(tmp, "export")
    subprocess.run([common.repo_path("tools", "verify", "export.sh"), dest], check=True, capture_output=True)
    log = open(os.path.join(tmp, "anvil.log"), "w")
    ver = subprocess.run([common.repo_path("tools", "verify", "run.sh"), dest, "anvil", "--version"],
                         capture_output=True).stdout.decode().strip().splitlines()[0]
    result["anvil_version"] = ver
    proc = subprocess.Popen([common.repo_path("tools", "verify", "run.sh"), dest, "anvil",
                             "--fork-url", url, "--fork-block-number", str(pin), "--port", str(port),
                             "--host", "127.0.0.1", "--compute-units-per-second", "20", "--retries", "10",
                             "--timeout", "60000", "--silent"], stdout=log, stderr=subprocess.STDOUT)
    loc = Local(port)
    try:
        t0 = time.time()
        while True:
            try:
                cid = int(loc.call("eth_chainId", []), 16)
                break
            except Exception:
                if proc.poll() is not None or time.time() - t0 > 120:
                    log.flush()
                    result["status"] = "FAIL"
                    result["error"] = "anvil did not start: " + open(log.name).read()[-2000:]
                    return result
                time.sleep(1)
        result["startup_seconds"] = round(time.time() - t0, 1)
        checks = {"chain_id": [cid, reg["chain_id"]], "block_number": [int(loc.call("eth_blockNumber", []), 16), pin]}

        st = reg["stpros"]
        reads = [("StPROS.totalSupply", st, abi.calldata("totalSupply()")),
                 ("StPROS.queuedWithdrawal", st, abi.calldata("queuedWithdrawal()")),
                 ("StPROS.completedWithdrawal", st, abi.calldata("completedWithdrawal()")),
                 ("ProxyAdmin.owner", reg["proxy_admin"], abi.calldata("owner()"))]
        for f in reg["factories"]:
            reads.append((f"Factory({f[:8]}).totalProxies", f, abi.calldata("totalProxies()")))
        for name, to, data in reads:
            a = loc.call("eth_call", [{"to": to, "data": data}, "latest"])
            b = live.eth_call(to, data, pin)
            checks[name] = [a, b]
        for p in sorted(reg["expected_proxies"]):
            checks[f"impl_slot({p[:8]})"] = [loc.call("eth_getStorageAt", [p, IMPL_SLOT, "latest"]),
                                             live.get_storage(p, IMPL_SLOT, pin)]
        fidelity = {k: {"fork": v[0], "live": v[1], "equal": v[0] == v[1]} for k, v in checks.items()}
        result["fidelity"] = fidelity
        result["fidelity_status"] = "PASS" if all(x["equal"] for x in fidelity.values()) else "FAIL"

        # gas estimates for handoff-shaped calls from their real authorities
        pa = reg["proxy_admin"]
        owner_pa = abi.dec_addr(loc.call("eth_call", [{"to": pa, "data": abi.calldata("owner()")}, "latest"]))
        calls = []
        for p, exp in sorted(reg["expected_proxies"].items()):
            calls.append((f"ProxyAdmin.upgrade({exp['name']}, same impl) [noop]", owner_pa, pa,
                          abi.calldata("upgrade(address,address)", p, exp["implementation"])))
        calls.append(("ProxyAdmin.transferOwnership(dead)", owner_pa, pa, abi.calldata("transferOwnership(address)", DEAD)))
        for p, exp in sorted(reg["expected_proxies"].items()):
            o = "0x" + loc.call("eth_getStorageAt", [p, "0x9016d09d72d40fdae2fd8ceac6b6234c7706214fd39c1cd1e609a0528c199300", "latest"])[-40:]
            calls.append((f"{exp['name']}.transferOwnership(dead) by {o[:8]}", o, p, abi.calldata("transferOwnership(address)", DEAD)))
        plan_path = common.repo_path("tools", "release", ".cache", "logscan", chain, "plan.json")
        if os.path.exists(plan_path):
            for v in common.read_json(plan_path)["vaults"][:2]:
                vault = v["vault"]
                roles_path = common.repo_path("tools", "release", ".cache", "logscan", chain, "inventory", "roles.json")
                admins = []
                if os.path.exists(roles_path):
                    admins = common.read_json(roles_path).get(vault, {}).get("holders_at_pin", {}).get("0x" + "0" * 64, [])
                for adm in admins[:1]:
                    calls.append((f"vault {vault[:8]}.grantRole(ADMIN, dead) by {adm[:8]}", adm, vault,
                                  abi.calldata("grantRole(bytes32,address)", 0, DEAD)))
                    calls.append((f"vault {vault[:8]}.renounceRole(ADMIN, self) by {adm[:8]}", adm, vault,
                                  abi.calldata("renounceRole(bytes32,address)", 0, adm)))
        import build
        head = build.build_head("production")
        creation = "0x" + head["contracts"]["project/contracts/StPROS.sol:StPROS"]["evm"]["bytecode"]["object"]
        est = {}
        for name, frm, to, data in calls:
            row = {"from": frm, "to": to}
            for label, fn in (("fork", lambda: loc.call("eth_estimateGas", [{"from": frm, "to": to, "data": data}, "latest"])),
                              ("live", lambda: live.call("eth_estimateGas", [{"from": frm, "to": to, "data": data}, hex(pin)]))):
                try:
                    row[label] = int(fn(), 16)
                except Exception as e:  # noqa: BLE001
                    row[label] = f"ERROR: {str(e)[:160]}"
            est[name] = row
        dep = {}
        for label, fn in (("fork", lambda: loc.call("eth_estimateGas", [{"from": owner_pa, "data": creation}, "latest"])),
                          ("live", lambda: live.call("eth_estimateGas", [{"from": owner_pa, "data": creation}, hex(pin)]))):
            try:
                dep[label] = int(fn(), 16)
            except Exception as e:  # noqa: BLE001
                dep[label] = f"ERROR: {str(e)[:160]}"
        est["deploy HEAD StPROS implementation (production profile)"] = dep
        result["estimate_gas"] = est

        # LOCAL fork only: impersonated no-op upgrade, actual gasUsed
        target, exp = sorted(reg["expected_proxies"].items())[0]
        loc.call("anvil_impersonateAccount", [owner_pa])
        loc.call("anvil_setBalance", [owner_pa, hex(10**20)])
        txh = loc.call("eth_sendTransaction", [{"from": owner_pa, "to": pa,
                                                "data": abi.calldata("upgrade(address,address)", target, exp["implementation"])}])
        rcpt = None
        for _ in range(60):
            rcpt = loc.call("eth_getTransactionReceipt", [txh])
            if rcpt:
                break
            time.sleep(1)
        impl_after = "0x" + loc.call("eth_getStorageAt", [target, IMPL_SLOT, "latest"])[-40:]
        result["local_impersonated_noop_upgrade"] = {
            "proxy": target, "name": exp["name"], "from": owner_pa,
            "status": rcpt["status"] if rcpt else None, "gasUsed": int(rcpt["gasUsed"], 16) if rcpt else None,
            "implementation_after": impl_after, "implementation_unchanged": impl_after == exp["implementation"],
            "note": "executed on the local anvil fork only; never broadcast"}
        ok = (result["fidelity_status"] == "PASS" and rcpt and rcpt["status"] == "0x1"
              and all(isinstance(r.get("fork"), int) for r in est.values()))
        result["status"] = "PASS" if ok else "FAIL"
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)
        result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if out_path:
            common.write_json(out_path, result)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", required=True, choices=sorted(common.CHAINS))
    ap.add_argument("--out", type=common.user_path)
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    res = probe(args.chain, args.port or free_port(), args.out)
    print(json.dumps({k: res.get(k) for k in ("chain", "pin", "status", "fidelity_status", "startup_seconds")}))
    return 0 if res.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
