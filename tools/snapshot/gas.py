#!/usr/bin/env python3
"""Gas baselines (snapshots/gas.json) from test/gas/GasBaseline.t.sol.

Builds a fresh .env-free export (tools/verify/export.sh), runs the gas suite
there with a minimal environment (tools/verify/run.sh), and collects the
`gas:<key>` log lines. Values are in-test measurements (warm access list
within the test transaction, no 21,000 intrinsic gas, includes the external
CALL overhead): regression baselines, not absolute transaction costs.

  gas.py           regenerate snapshots/gas.json
  gas.py --check   fail if a key is added/removed or a value leaves the ±5% band
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import common  # noqa: E402

PATH = common.repo_path("snapshots", "gas.json")
TOLERANCE = 0.05
LINE_RE = re.compile(r"^\s*gas:([\w.]+):\s*(\d+)\s*$", re.M)


def measure():
    tmp = tempfile.mkdtemp(prefix="faroo-gas-")
    dest = os.path.join(tmp, "export")
    try:
        subprocess.run([common.repo_path("tools", "verify", "export.sh"), dest], check=True, capture_output=True)
        r = subprocess.run([common.repo_path("tools", "verify", "run.sh"), dest, "forge", "test",
                            "--match-path", "test/gas/GasBaseline.t.sol", "-vv"], capture_output=True)
        out = r.stdout.decode()
        if r.returncode != 0:
            sys.exit("gas suite failed:\n" + out[-3000:] + r.stderr.decode()[-2000:])
        forge = subprocess.run([common.repo_path("tools", "verify", "run.sh"), dest, "forge", "--version"],
                               capture_output=True, check=True).stdout.decode().splitlines()[0]
        return {k: int(v) for k, v in LINE_RE.findall(out)}, forge
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    values, forge = measure()
    if not values:
        sys.exit("no gas:<key> lines found")
    if not args.check:
        common.write_json(PATH, {"tolerance": TOLERANCE, "measured_with": forge,
                                 "method": "in-test gasleft() delta around one external call; warm access list; "
                                           "no intrinsic gas; HEAD behind the recorded v4 TUP with WPROS9, "
                                           "legacyV1 on recorded mainnet bytecode with the real WPROS runtime",
                                 "baselines": dict(sorted(values.items()))})
        print(f"gas: wrote {len(values)} baselines")
        return 0
    base = common.read_json(PATH)["baselines"]
    problems = []
    for k in sorted(set(base) | set(values)):
        if k not in values:
            problems.append(f"REMOVED {k}")
        elif k not in base:
            problems.append(f"ADDED {k} = {values[k]}")
        else:
            delta = (values[k] - base[k]) / base[k]
            if abs(delta) > TOLERANCE:
                problems.append(f"OUT OF BAND {k}: {base[k]} -> {values[k]} ({delta:+.1%})")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"gas --check: {len(values)} baselines within ±{TOLERANCE:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
