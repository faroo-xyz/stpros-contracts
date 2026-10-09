#!/usr/bin/env bash
# Regression tests for the isolation guards of export.sh and run.sh.
#
# Every case builds a throwaway git repository under a fresh temp directory,
# containing a copy of export.sh/run.sh and synthetic, secret-free files
# (contents are fixed placeholder strings). Each negative case plants exactly
# one unsafe path shape and must be refused (non-zero exit) without leaving a
# partial export behind. Positive cases must still export. No real
# environment file is created, read or referenced.
#
# Usage: tools/verify/export_guard_tests.sh
set -u

REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
WORK=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/faroo-export-guard-XXXXXX")" && pwd -P)
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0
ok() { echo "PASS  $1"; PASS=$((PASS + 1)); }
ko() { echo "FAIL  $1"; FAIL=$((FAIL + 1)); }

G() { git -C "$1" -c user.name=fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false "${@:2}"; }

# A clean fixture repository: a few allowlisted files and minimal Solidity packages.
fixture() {
  local d="$WORK/repos/$1"
  mkdir -p "$d/tools/verify" "$d/contracts" "$d/test"
  cp "$REPO/tools/verify/export.sh" "$REPO/tools/verify/run.sh" "$d/tools/verify/"
  echo 'contract A {}' > "$d/contracts/A.sol"
  echo 'contract T {}' > "$d/test/T.sol"
  printf 'node_modules/\n' > "$d/.gitignore"
  for p in @openzeppelin/contracts @openzeppelin/contracts-upgradeable forge-std; do
    mkdir -p "$d/node_modules/.pnpm/$p/src"
    printf '{\n  "version": "0.0.0",\n}\n' > "$d/node_modules/.pnpm/$p/package.json"
    echo 'library L {}' > "$d/node_modules/.pnpm/$p/src/L.sol"
    mkdir -p "$d/node_modules/$(dirname "$p")"
    ln -s "$( [ "${p%%/*}" = "@openzeppelin" ] && echo ../.pnpm/$p || echo .pnpm/$p)" "$d/node_modules/$p"
  done
  G "$d" init -q
  G "$d" add -A
  G "$d" commit -q -m fixture
  echo "$d"
}

# expect_refusal NAME REPO DEST [args...]: export must fail and DEST must not exist.
expect_refusal() {
  local name="$1" r="$2" dest="$3"; shift 3
  if out=$("$r/tools/verify/export.sh" "$@" "$dest" 2>&1); then
    ko "$name (export unexpectedly succeeded)"
  elif [ -e "$dest" ]; then
    ko "$name (refused but left $dest behind)"
  else
    ok "$name -> ${out##*export.sh: }"
  fi
}

expect_export() {
  local name="$1" r="$2" dest="$3"; shift 3
  if out=$("$r/tools/verify/export.sh" "$@" "$dest" 2>&1) && [ -f "$dest/EXPORT-MANIFEST.txt" ]; then
    ok "$name"
  else
    ko "$name: $out"
  fi
}

OUT="$WORK/out"
mkdir -p "$OUT"
n=0
# next destination (global, no subshell so the counter advances)
nd() { n=$((n + 1)); D="$OUT/e$n"; }

# ---------------------------------------------------------------- positive controls
r=$(fixture clean)
nd; d="$D"; expect_export "clean worktree export" "$r" "$d"
[ -f "$d/contracts/A.sol" ] && [ -f "$d/node_modules/forge-std/src/L.sol" ] && ok "clean export contains sources and deps" || ko "clean export contents"
nd; d="$D"; expect_export "clean --head export" "$r" "$d" --head
nd; d="$D"; expect_export "clean --with-hardhat export" "$r" "$d" --with-hardhat

# ---------------------------------------------------------------- allowlisted paths (worktree)
r=$(fixture wt_envdir); mkdir -p "$r/contracts/.env"; echo 'contract X {}' > "$r/contracts/.env/X.sol"
nd; expect_refusal "worktree: .env directory component" "$r" "$D"

r=$(fixture wt_case); mkdir -p "$r/contracts/sub"; echo 'placeholder' > "$r/contracts/sub/.ENV.local"
nd; expect_refusal "worktree: .ENV.local (case-insensitive) basename" "$r" "$D"

r=$(fixture wt_envrc); echo 'placeholder' > "$r/test/.envrc"
nd; expect_refusal "worktree: .envrc component" "$r" "$D"

r=$(fixture wt_mixed); mkdir -p "$r/test/.Env.d/x"; echo 'contract X {}' > "$r/test/.Env.d/x/X.sol"
nd; expect_refusal "worktree: .Env.d mid-path component" "$r" "$D"

r=$(fixture wt_symfile); mkdir -p "$WORK/outside"; echo 'contract O {}' > "$WORK/outside/O.sol"
ln -s "$WORK/outside/O.sol" "$r/contracts/O.sol"
nd; expect_refusal "worktree: symlinked file pointing outside" "$r" "$D"

r=$(fixture wt_symrel); ln -s ../contracts/A.sol "$r/test/A2.sol"
nd; expect_refusal "worktree: relative symlink (any symlink refused)" "$r" "$D"

r=$(fixture wt_symdir); ln -s "$WORK/outside" "$r/contracts/linked"
nd; expect_refusal "worktree: symlinked directory component" "$r" "$D"

r=$(fixture wt_tracked_env); echo 'placeholder' > "$r/contracts/.env.example"; G "$r" add -f contracts/.env.example; G "$r" commit -q -m t
nd; expect_refusal "worktree: tracked .env.example" "$r" "$D"

# ---------------------------------------------------------------- --head exports
r=$(fixture head_tracked_env); echo 'placeholder' > "$r/contracts/.env.example"; G "$r" add -f contracts/.env.example; G "$r" commit -q -m t
rm "$r/contracts/.env.example"
nd; expect_refusal "--head: committed .env.example (absent from worktree)" "$r" "$D" --head

r=$(fixture head_envdir); mkdir -p "$r/test/.ENV"; echo 'contract X {}' > "$r/test/.ENV/X.sol"; G "$r" add -f test/.ENV/X.sol; G "$r" commit -q -m t
rm -rf "$r/test/.ENV"
nd; expect_refusal "--head: committed .ENV directory component" "$r" "$D" --head

r=$(fixture head_symlink); ln -s ../outside/O.sol "$r/contracts/S.sol"; G "$r" add contracts/S.sol; G "$r" commit -q -m t
rm "$r/contracts/S.sol"
nd; expect_refusal "--head: committed symlink (mode 120000)" "$r" "$D" --head

r=$(fixture head_gitlink); G "$r" update-index --add --cacheinfo 160000,"$(G "$r" rev-parse HEAD)",contracts/sub; G "$r" commit -q -m t
nd; expect_refusal "--head: submodule entry (mode 160000)" "$r" "$D" --head

# ---------------------------------------------------------------- dependencies (default mode)
r=$(fixture dep_pkg_outside); mkdir -p "$WORK/outside/pkg/src"; echo 'library M {}' > "$WORK/outside/pkg/src/M.sol"
rm "$r/node_modules/forge-std"; ln -s "$WORK/outside/pkg" "$r/node_modules/forge-std"
nd; expect_refusal "deps: package directory resolving outside node_modules" "$r" "$D"

r=$(fixture dep_inner_symlink); ln -s L.sol "$r/node_modules/.pnpm/forge-std/src/L2.sol"
nd; expect_refusal "deps: symlink inside a Solidity package" "$r" "$D"

r=$(fixture dep_envdir); mkdir -p "$r/node_modules/.pnpm/forge-std/src/.env"; echo 'library E {}' > "$r/node_modules/.pnpm/forge-std/src/.env/E.sol"
nd; expect_refusal "deps: .env directory component in a Solidity package" "$r" "$D"

# ---------------------------------------------------------------- dependencies (--with-hardhat)
r=$(fixture hh_escape); ln -s ../../outside "$r/node_modules/escape"
nd; expect_refusal "--with-hardhat: relative symlink escaping node_modules" "$r" "$D" --with-hardhat

r=$(fixture hh_abs); ln -s "$r/node_modules/.pnpm" "$r/node_modules/abs"
nd; expect_refusal "--with-hardhat: absolute symlink target" "$r" "$D" --with-hardhat

r=$(fixture hh_envtarget); ln -s ./.env/x "$r/node_modules/envtarget"
nd; expect_refusal "--with-hardhat: symlink target with .env component" "$r" "$D" --with-hardhat

r=$(fixture hh_envlink); ln -s .pnpm "$r/node_modules/.Env"
nd; expect_refusal "--with-hardhat: env-named symlink" "$r" "$D" --with-hardhat

r=$(fixture hh_vendor_env)
mkdir -p "$r/node_modules/.pnpm/vendor/.ENV"
echo 'placeholder' > "$r/node_modules/.pnpm/vendor/.env.example"
echo 'placeholder' > "$r/node_modules/.pnpm/vendor/.ENV/x"
ln -s ../.pnpm/missing "$r/node_modules/.pnpm/dangling"   # dangling but inside node_modules
nd; d="$D"; expect_export "--with-hardhat: vendor .env names excluded, dangling in-tree link allowed" "$r" "$d" --with-hardhat
if [ -z "$(find "$d" \( -iname '.env' -o -iname '.env.*' -o -iname '.envrc' \) -print -quit 2>/dev/null)" ] && [ -L "$d/node_modules/.pnpm/dangling" ]; then
  ok "--with-hardhat: export has no env-named entry and keeps the in-tree link"
else
  ko "--with-hardhat: vendor env exclusion"
fi

# ---------------------------------------------------------------- destination
r=$(fixture dst)
expect_refusal "DEST with a .Env component" "$r" "$OUT/x/.Env/exp"
[ -e "$OUT/x" ] && ko "DEST refusal created a parent directory" || ok "DEST refusal created nothing"
expect_refusal "DEST inside the repository" "$r" "$r/inside-export"
mkdir -p "$WORK/anc/sub"; echo 'placeholder' > "$WORK/anc/.ENV.local"
expect_refusal "ancestor of DEST holds an environment-file name" "$r" "$WORK/anc/sub/exp"
rm -f "$WORK/anc/.ENV.local"

# ---------------------------------------------------------------- run.sh
r=$(fixture runsh)
nd; d="$D"; "$r/tools/verify/export.sh" "$d" >/dev/null 2>&1
if "$r/tools/verify/run.sh" "$d" true >/dev/null 2>&1; then ok "run.sh: clean export runs"; else ko "run.sh: clean export runs"; fi
cp -R "$d" "$OUT/r1"; ln -s "$WORK/outside" "$OUT/r1/escape"
if "$r/tools/verify/run.sh" "$OUT/r1" true >/dev/null 2>&1; then ko "run.sh: symlink escaping export"; else ok "run.sh: refuses symlink escaping export"; fi
cp -R "$d" "$OUT/r2"; echo 'placeholder' > "$OUT/r2/test/.EnV"
if "$r/tools/verify/run.sh" "$OUT/r2" true >/dev/null 2>&1; then ko "run.sh: env-named file in export"; else ok "run.sh: refuses env-named file in export"; fi
mkdir -p "$OUT/r3/.env.d"; cp -R "$d" "$OUT/r3/.env.d/exp"
if "$r/tools/verify/run.sh" "$OUT/r3/.env.d/exp" true >/dev/null 2>&1; then ko "run.sh: .env path component"; else ok "run.sh: refuses .env path component"; fi
if "$r/tools/verify/run.sh" "$r" true >/dev/null 2>&1; then ko "run.sh: live repository"; else ok "run.sh: refuses the live (fixture) repository"; fi

echo "export guard tests: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
