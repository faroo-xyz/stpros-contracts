#!/usr/bin/env bash
# Run every B0 gate in --check mode. Never writes snapshots.
#
# Usage: tools/verify/check_all.sh [--offline]
#   --offline  skip the checks that need the public RPC (deployed state,
#              on-chain fixture identity, provenance on-chain/history)
set -uo pipefail

OFFLINE=0
[ "${1:-}" = "--offline" ] && OFFLINE=1
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$ROOT"

fail=0
run() {
  local name="$1"; shift
  if out=$("$@" 2>&1); then
    echo "PASS  $name"
  else
    echo "FAIL  $name"; echo "$out" | tail -20 | sed 's/^/      /'; fail=1
  fi
}
skip() { echo "NOT RUN  $1 (--offline)"; }

run "selftest keccak" python3 tools/lib/keccak.py
run "selftest abi" python3 -c "import sys; sys.path.insert(0,'tools/lib'); import abi; abi._selftest()"
run "selftest logscan" python3 tools/release/logscan.py selftest
run "export/run isolation guards" python3 -m unittest discover -s test/toolchain -p test_export.py -v
run "legacy interfaces" python3 tools/release/legacy_interfaces.py --check
run "snapshot abi" python3 tools/snapshot/abi.py --check
run "snapshot storage" python3 tools/snapshot/storage.py --check
run "snapshot bytecode" python3 tools/snapshot/bytecode.py --check
run "snapshot capabilities" python3 tools/snapshot/capabilities.py --check
run "snapshot gas" python3 tools/snapshot/gas.py --check
run "deployed-state doc" python3 tools/snapshot/deployed_doc.py --check
if [ "$OFFLINE" = 1 ]; then
  run "fixtures (offline)" python3 tools/release/fixtures.py --check --offline
  skip "snapshot deployed"
  skip "provenance (recompile, on-chain, history)"
else
  run "fixtures" python3 tools/release/fixtures.py --check
  run "snapshot deployed" python3 tools/snapshot/deployed.py --check
  run "provenance" python3 tools/release/provenance.py --recompile --onchain --history --check \
      ${FAROO_SOLC_0_8_10:+--solc 0.8.10=$FAROO_SOLC_0_8_10}
fi
exit $fail
