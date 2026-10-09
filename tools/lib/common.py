"""Shared helpers for the B0 verification tools.

Rules every tool follows:
- never read `.env*` or any secret; RPC endpoints are public and passed
  explicitly (see `rpc.PUBLIC_RPC`);
- outputs are canonical JSON (sorted keys, 2-space indent, trailing newline)
  so that regeneration is byte-deterministic and `--check` can diff exactly.
"""

import hashlib
import json
import os
import subprocess
import sys

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(LIB_DIR, "..", ".."))

CHAINS = {
    "mainnet": {"chain_id": 1672, "label": "Pharos mainnet"},
    "testnet": {"chain_id": 688689, "label": "Pharos Atlantic testnet"},
}

# Registry of reviewed public facts (COMPATIBILITY-AND-MIGRATION.md §1).
REGISTRY_PATH = os.path.join(REPO, "tools", "release", "registry.json")


def repo_path(*parts):
    return os.path.join(REPO, *parts)


SECRET_NAME_MARKERS = ("secret", "credential", "mnemonic", "keystore", "private_key", "privatekey", "id_rsa", "id_ed25519")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def is_secret_like(path):
    """True if any component of `path` (as given, or after resolving symlinks)
    looks like an environment or key file. Matching is by name only; the file
    is never opened."""
    candidates = [path]
    try:
        candidates.append(os.path.realpath(path))
    except OSError:
        pass
    for p in candidates:
        for part in str(p).replace("\\", "/").split("/"):
            low = part.lower()
            if low == ".env" or low.startswith(".env."):
                return True
            if any(m in low for m in SECRET_NAME_MARKERS) or low.endswith(SECRET_SUFFIXES):
                return True
    return False


def refuse_secret_path(path):
    """Abort if a tool is pointed at a path that looks like a secret (any
    component named .env / .env.* / key material), before touching it."""
    if is_secret_like(path):
        sys.exit(f"refusing to touch secret-like path {path}")


def user_path(path):
    """Validate a path supplied on a tool's command line (argparse `type=`)."""
    if is_secret_like(path):
        raise SystemExit(f"refusing secret-like path argument {path}")
    return path


def is_safe_solidity_source(path):
    """Git provenance indexing reads only these historical blobs."""
    return path.startswith("contracts/") and path.endswith(".sol") and not is_secret_like(path)


def canonical_json(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path, obj):
    refuse_secret_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = canonical_json(obj)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    return data


def read_json(path):
    refuse_secret_path(path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256_file(path):
    refuse_secret_path(path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, cwd=REPO, check=True):
    res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, check=check)
    return res.stdout


def head_commit():
    return git("rev-parse", "HEAD").decode().strip()


def registry():
    return read_json(REGISTRY_PATH)


def deployment_records(chain):
    """Yield (name, relative_path, record) for deployments/<chain>/*.json."""
    d = repo_path("deployments", chain)
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        rel = os.path.join("deployments", chain, fn)
        yield fn[:-5], rel, read_json(repo_path(rel))


def hexbytes(h):
    h = h[2:] if h.startswith("0x") else h
    return bytes.fromhex(h)


def lower_addr(a):
    return a.lower() if isinstance(a, str) else a


class CheckFailure(Exception):
    pass


def assert_no_env_in_ancestors(path):
    """Foundry loads `.env` from the cwd and every parent directory."""
    d = os.path.realpath(path)
    while True:
        for name in os.listdir(d) if os.path.isdir(d) else []:
            if name == ".env" or name.startswith(".env."):
                sys.exit(f"refusing to run a Foundry binary below {d}: it contains {name}")
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def run_foundry(args, input_bytes=None):
    """Run forge/cast/anvil offline-safe: in a fresh temp directory (no `.env`
    in it or any ancestor) with an empty environment except PATH and HOME.
    Every Foundry binary calls load_dotenv() at startup, for every subcommand,
    so it must never run with the live repository as its working directory."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="faroo-foundry-") as tmp:
        assert_no_env_in_ancestors(tmp)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        return subprocess.run(args, cwd=tmp, env=env, input=input_bytes, capture_output=True, check=True).stdout


def run_check(path, new_text):
    """Compare a regenerated artifact with the file on disk (no write)."""
    if not os.path.exists(path):
        return f"MISSING {os.path.relpath(path, REPO)}"
    with open(path, encoding="utf-8") as fh:
        old = fh.read()
    if old != new_text:
        return f"CHANGED {os.path.relpath(path, REPO)}"
    return None


def emit_or_check(path, obj, check, problems):
    """Write `obj` to `path`, or in check mode record a difference."""
    text = canonical_json(obj)
    if check:
        p = run_check(path, text)
        if p:
            problems.append(p)
    else:
        write_json(path, obj)
    return text
