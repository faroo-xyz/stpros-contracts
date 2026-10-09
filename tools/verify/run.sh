#!/usr/bin/env bash
# Run a command inside an export created by tools/verify/export.sh, with a
# minimal environment (no inherited RPC URLs, keys or FOUNDRY_* overrides).
#
# Usage:
#   tools/verify/run.sh EXPORT_DIR [VAR=value ...] command [args ...]
#
# Only PATH, HOME, TERM and explicitly passed VAR=value pairs reach the
# command. Pass public RPC endpoints explicitly, e.g.
#   tools/verify/run.sh "$dest" FAROO_FORK_RPC_MAINNET=https://rpc.pharos.xyz forge test --mt testFork
set -euo pipefail

die() { echo "run.sh: $*" >&2; exit 1; }
[ $# -ge 2 ] || die "usage: run.sh EXPORT_DIR [VAR=value ...] command [args ...]"
# Same component rule as export.sh: no `.env`, `.env.*` or `.envrc` component
# anywhere in the path (case-insensitive).
rest="${1#/}"
while [ -n "$rest" ]; do
  part="${rest%%/*}"
  case "$part" in
    .[eE][nN][vV] | .[eE][nN][vV].* | .[eE][nN][vV][rR][cC]) die "refusing an export path with a .env component" ;;
  esac
  [ "$rest" = "$part" ] && break
  rest="${rest#*/}"
done
EXPORT_DIR=$(cd "$1" && pwd -P); shift
[ -f "$EXPORT_DIR/EXPORT-MANIFEST.txt" ] || die "$EXPORT_DIR is not an export (no EXPORT-MANIFEST.txt)"

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
ROOT_REAL=$(cd "$ROOT" && pwd -P)
case "$EXPORT_DIR/" in
  "$ROOT"/* | "$ROOT_REAL"/*) die "refusing to run inside the live repository" ;;
esac
# Every symlink in the export must resolve inside it (dangling links reach
# nothing); only then is a name scan of the export meaningful.
if [ -n "$(find "$EXPORT_DIR" -type l -print -quit)" ]; then
  if [ -n "$(find "$EXPORT_DIR" -type l -print0 | xargs -0 readlink | grep -m1 '^/' || true)" ]; then
    die "refusing: export contains an absolute symlink"
  fi
  if [ -n "$(find "$EXPORT_DIR" -type l -print0 | xargs -0 realpath -q 2>/dev/null | grep -v -m1 "^$EXPORT_DIR/" || true)" ]; then
    die "refusing: export contains a symlink resolving outside it"
  fi
fi
if [ -n "$(find "$EXPORT_DIR" \( -iname '.env' -o -iname '.env.*' -o -iname '.envrc' \) -print -quit)" ]; then
  die "refusing: export contains an environment file"
fi
# Foundry's load_dotenv() also searches every parent directory of the cwd.
dir=$(dirname "$EXPORT_DIR")
shopt -s nocaseglob
while :; do
  for f in "$dir"/.env "$dir"/.env.* "$dir"/.envrc; do
    [ -e "$f" ] && die "refusing: an ancestor of the export ($dir) contains an environment file"
  done
  [ "$dir" = "/" ] && break
  dir=$(dirname "$dir")
done
shopt -u nocaseglob

ENVV=("PATH=$PATH" "HOME=$HOME" "TERM=${TERM:-dumb}")
while [ $# -gt 0 ] && [[ "$1" == *=* ]] && [[ "$1" != -* ]]; do
  ENVV+=("$1"); shift
done
[ $# -ge 1 ] || die "missing command"

cd "$EXPORT_DIR"
exec env -i "${ENVV[@]}" "$@"
