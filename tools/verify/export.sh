#!/usr/bin/env bash
# Build an isolated, .env-free export of this repository for forge / anvil /
# halmos / hardhat runs (VERIFICATION-AND-AUDIT-PLAN §1.4).
#
# Foundry auto-loads `.env` from the working directory *and its parents*, and
# hardhat.config.ts imports `dotenv/config`. Neither may ever run in the live
# working tree. This script copies only allowlisted, git-known regular files
# into a fresh directory.
#
# Isolation rules (all enforced before any file is read, copied or passed to
# git, and identical for worktree, --head and --with-hardhat exports):
#   - every relative path is validated component by component: no empty, `.`
#     or `..` component, no control characters, and no component named `.env`,
#     `.env.*` or `.envrc` (case-insensitive) anywhere in the path;
#   - worktree: no component of the path may be a symlink, the entry must be a
#     regular file, and it must resolve inside the repository;
#   - --head: only blob entries with mode 100644/100755 are exported (symlinks
#     and submodules are refused), read by their object id;
#   - dependencies: package directories must resolve inside node_modules/,
#     the copied Solidity trees may contain no symlinks, and in --with-hardhat
#     mode every symlink must stay inside node_modules/ (lexically, and after
#     resolution when it resolves); `.env`-named entries in third-party
#     packages are excluded by name and never opened;
#   - the export's destination may not have a `.env` component, and neither
#     the export nor any ancestor of it may contain a `.env*` entry;
#   - any refusal deletes the partial export.
#
# Usage:
#   tools/verify/export.sh [--head] [--with-hardhat] [DEST]
#
#   --head          export the committed tree (HEAD) only; default is the
#                   working tree (tracked + untracked, non-ignored files)
#   --with-hardhat  also export the Hardhat project files and copy
#                   node_modules (needed only to run `hardhat test`)
#   DEST            target directory (must not exist); default is
#                   ${TMPDIR:-/tmp}/faroo-export-<sha>-<utc timestamp>
#
# The export path is printed as the last line of output.
set -euo pipefail

die() { echo "export.sh: $*" >&2; exit 1; }

# A single path component that names an environment file (case-insensitive,
# because the default macOS filesystem is case-insensitive).
is_env_component() {
  case "$1" in
    .[eE][nN][vV] | .[eE][nN][vV].* | .[eE][nN][vV][rR][cC]) return 0 ;;
  esac
  return 1
}

# Validate a repository-relative path as a string, before it is used.
# Paths with an env component are refused without being printed.
validate_rel() {
  local p="$1" rest part
  case "$p" in
    "" | /*) die "refusing an empty or absolute path" ;;
    *$'\n'* | *$'\r'* | *$'\t'*) die "refusing a path with a control character" ;;
  esac
  rest="$p"
  while :; do
    part="${rest%%/*}"
    case "$part" in
      "" | . | ..) die "refusing a path with an empty, '.' or '..' component: $p" ;;
    esac
    is_env_component "$part" && die "refusing a path with a .env component (path not printed)"
    [ "$rest" = "$part" ] && break
    rest="${rest#*/}"
  done
  return 0
}

# Validate an absolute path (DEST) component by component.
validate_abs_no_env() {
  local rest="${1#/}" part
  while [ -n "$rest" ]; do
    part="${rest%%/*}"
    is_env_component "$part" && die "refusing a destination path with a .env component"
    [ "$rest" = "$part" ] && break
    rest="${rest#*/}"
  done
  return 0
}

# Every symlink below TOP must stay inside BOUNDARY: relative target, no env
# component in the link or its target, lexically inside BOUNDARY, and (when it
# resolves) resolving inside BOUNDARY. Reads link metadata only (lstat/readlink);
# never follows a link into a directory and never opens a file.
check_symlinks() {
  python3 - "$1" "$2" <<'PY'
import os, sys
top, boundary = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])
def envlike(name):
    n = name.lower()
    return n == ".env" or n.startswith(".env.") or n == ".envrc"
def inside(p):
    return p == boundary or p.startswith(boundary + os.sep)
bad = []
for d, dirs, files in os.walk(top, followlinks=False):
    for name in list(dirs) + files:
        p = os.path.join(d, name)
        if not os.path.islink(p):
            continue
        if envlike(name):
            bad.append(f"env-named symlink in {d}")
            continue
        t = os.readlink(p)
        if os.path.isabs(t):
            bad.append(f"absolute symlink target: {p}")
        elif any(envlike(x) for x in t.replace("\\", "/").split("/")):
            bad.append(f"symlink target with a .env component: {p}")
        elif not inside(os.path.normpath(os.path.join(d, t))):
            bad.append(f"symlink target outside {boundary}: {p}")
        elif os.path.exists(p) and not inside(os.path.realpath(p)):
            bad.append(f"symlink resolves outside {boundary}: {p}")
    # never descend into env-named directories
    dirs[:] = [x for x in dirs if not envlike(x)]
for b in bad:
    print("export.sh: refusing " + b, file=sys.stderr)
sys.exit(1 if bad else 0)
PY
}

MODE=worktree
WITH_HARDHAT=0
DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --head) MODE=head ;;
    --with-hardhat) WITH_HARDHAT=1 ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$DEST" ] || die "more than one DEST"; DEST="$1" ;;
  esac
  shift
done

ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
ROOT_REAL=$(cd "$ROOT" && pwd -P)
HEAD_SHA=$(git -C "$ROOT" rev-parse HEAD)
if [ -z "$DEST" ]; then
  DEST="${TMPDIR:-/tmp}/faroo-export-${HEAD_SHA:0:7}-$(date -u +%Y%m%dT%H%M%SZ)"
fi
DEST="${DEST%/}"
case "$DEST" in /*) ;; *) DEST="$(pwd -P)/$DEST" ;; esac
validate_abs_no_env "$DEST"
[ -e "$DEST" ] && die "destination already exists: $DEST"
case "$DEST/" in
  "$ROOT"/* | "$ROOT_REAL"/*) die "destination must be outside the repository" ;;
esac

# Paths that may enter an export.
ALLOW=(contracts test tools snapshots deployments docs foundry.toml AGENTS.md README.md Makefile hardhat.config.ts package.json pnpm-lock.yaml)
if [ "$WITH_HARDHAT" = 1 ]; then
  ALLOW+=(tsconfig.json tasks deploy rocketh contants scripts)
fi
for a in "${ALLOW[@]}"; do validate_rel "$a"; done

NM_REAL=""
if [ -d "$ROOT/node_modules" ]; then
  NM_REAL=$(cd "$ROOT/node_modules" && pwd -P)
  [ "$NM_REAL" = "$ROOT_REAL/node_modules" ] || die "node_modules must be a real directory of the repository"
fi

# An ancestor `.env` would be picked up by Foundry's parent-directory search.
parent=$(dirname "$DEST")
mkdir -p "$parent"
parent=$(cd "$parent" && pwd -P)
case "$parent/" in
  "$ROOT_REAL"/*) die "destination must be outside the repository" ;;
esac
dir="$parent"
shopt -s nocaseglob
while :; do
  for f in "$dir"/.env "$dir"/.env.* "$dir"/.envrc; do
    [ -e "$f" ] && die "refusing: an ancestor of DEST ($dir) contains an environment file"
  done
  [ "$dir" = "/" ] && break
  dir=$(dirname "$dir")
done
shopt -u nocaseglob

# ---------------------------------------------------------------- pass 1: validate
# Nothing below reads or copies a file until every entry has passed.
LIST=$(mktemp)
DONE=0
cleanup() {
  rm -f "$LIST"
  [ "$DONE" = 1 ] || rm -rf "$DEST"
}
trap cleanup EXIT

PATHS=()   # validated relative paths to copy
OBJECTS=() # --head: object id per path
deleted=0
if [ "$MODE" = head ]; then
  git -C "$ROOT" ls-tree -r -z --full-tree HEAD -- "${ALLOW[@]}" > "$LIST"
  while IFS= read -r -d '' rec; do
    meta="${rec%%$'\t'*}"
    path="${rec#*$'\t'}"
    validate_rel "$path"
    set -- $meta
    mode="$1" type="$2" oid="$3"
    [ "$type" = blob ] || die "refusing a non-blob tree entry ($type): $path"
    case "$mode" in
      100644 | 100755) ;;
      120000) die "refusing a committed symlink: $path" ;;
      *) die "refusing tree entry mode $mode: $path" ;;
    esac
    PATHS+=("$path")
    OBJECTS+=("$oid")
  done < "$LIST"
else
  git -C "$ROOT" ls-files -z --cached --others --exclude-standard -- "${ALLOW[@]}" > "$LIST"
  while IFS= read -r -d '' path; do
    validate_rel "$path"
    # No component may be a symlink (lstat only; nothing is followed).
    prefix="" rest="$path"
    while :; do
      part="${rest%%/*}"
      prefix="${prefix:+$prefix/}$part"
      [ -L "$ROOT/$prefix" ] && die "refusing a symlink in an allowlisted path: $prefix"
      [ "$rest" = "$part" ] && break
      rest="${rest#*/}"
    done
    if [ ! -e "$ROOT/$path" ]; then
      deleted=$((deleted + 1))
      continue
    fi
    [ -f "$ROOT/$path" ] || die "refusing a non-regular file: $path"
    real_dir=$(cd "$ROOT/$(dirname "$path")" && pwd -P)
    case "$real_dir/" in
      "$ROOT_REAL"/*) ;;
      *) die "refusing a path resolving outside the repository: $path" ;;
    esac
    PATHS+=("$path")
  done < "$LIST"
fi

# Dependencies: resolve and validate before copying.
SOL_PKGS=("@openzeppelin/contracts" "@openzeppelin/contracts-upgradeable" "forge-std")
SOL_FILES=()   # "<pkg real dir>|<relative file>"
if [ "$WITH_HARDHAT" = 1 ]; then
  [ -n "$NM_REAL" ] || die "missing node_modules (run pnpm install in the repository)"
  check_symlinks "$NM_REAL" "$NM_REAL" || die "unsafe symlink in node_modules"
else
  for pkg in "${SOL_PKGS[@]}"; do
    validate_rel "node_modules/$pkg"
    [ -d "$ROOT/node_modules/$pkg" ] || die "missing dependency node_modules/$pkg (run pnpm install in the repository)"
    pkg_real=$(cd "$ROOT/node_modules/$pkg" && pwd -P)
    case "$pkg_real/" in
      "$NM_REAL"/*) ;;
      *) die "dependency node_modules/$pkg resolves outside node_modules" ;;
    esac
    [ -z "$(find "$pkg_real" -type l | head -1)" ] || die "refusing a symlink inside node_modules/$pkg"
    while IFS= read -r -d '' f; do
      f="${f#./}"
      validate_rel "node_modules/$pkg/$f"
      SOL_FILES+=("$pkg|$pkg_real|$f")
    done < <(cd "$pkg_real" && find . -type f \( -name '*.sol' -o -name 'package.json' \) -print0)
  done
fi

# ---------------------------------------------------------------- pass 2: copy
mkdir "$DEST"
i=0
for path in "${PATHS[@]+"${PATHS[@]}"}"; do
  mkdir -p "$DEST/$(dirname "$path")"
  if [ "$MODE" = head ]; then
    git -C "$ROOT" cat-file blob "${OBJECTS[$i]}" > "$DEST/$path"
  else
    cp -p "$ROOT/$path" "$DEST/$path"
  fi
  i=$((i + 1))
done
count=$i

if [ "$WITH_HARDHAT" = 1 ]; then
  # Hardhat needs the whole dependency tree. pnpm's relative symlinks were
  # checked above and stay inside the copy; env-named entries are excluded.
  rsync -a --exclude '.[eE][nN][vV]' --exclude '.[eE][nN][vV].*' --exclude '.[eE][nN][vV][rR][cC]' \
    "$NM_REAL/" "$DEST/node_modules/"
else
  for entry in "${SOL_FILES[@]}"; do
    pkg="${entry%%|*}"; rest="${entry#*|}"
    pkg_real="${rest%%|*}"; f="${rest#*|}"
    mkdir -p "$DEST/node_modules/$pkg/$(dirname "$f")"
    cp -p "$pkg_real/$f" "$DEST/node_modules/$pkg/$f"
  done
fi

# ---------------------------------------------------------------- post-conditions
# Symlinks first (so the name scan below never leaves the export), then names.
check_symlinks "$DEST" "$DEST" || die "export contains an unsafe symlink"
if [ -n "$(find "$DEST" \( -iname '.env' -o -iname '.env.*' -o -iname '.envrc' \) -print -quit)" ]; then
  die "refusing: export contains an environment file"
fi

ver() { sed -n 's/^  "version": "\(.*\)",$/\1/p' "$NM_REAL/$1/package.json" | head -1; }
{
  echo "source_root: $ROOT"
  echo "source_head: $HEAD_SHA"
  echo "mode: $MODE"
  echo "with_hardhat: $WITH_HARDHAT"
  echo "created_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "allowlist: ${ALLOW[*]}"
  echo "files: $count"
  echo "deleted_in_worktree: $deleted"
  echo "openzeppelin_contracts: $(ver @openzeppelin/contracts)"
  echo "openzeppelin_contracts_upgradeable: $(ver @openzeppelin/contracts-upgradeable)"
  echo "forge_std: $(ver forge-std)"
  if [ "$MODE" = worktree ]; then
    echo "worktree_changes:"
    git -C "$ROOT" status --porcelain=v1 --untracked-files=all -- "${ALLOW[@]}" | sed 's/^/  /'
  fi
} > "$DEST/EXPORT-MANIFEST.txt"

DONE=1
echo "export.sh: $count files, mode=$MODE, hardhat=$WITH_HARDHAT"
echo "$DEST"
