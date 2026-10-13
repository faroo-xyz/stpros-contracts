#!/usr/bin/env python3
"""Copy only safe, Git-known native sources into a fresh isolated directory."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ALLOW = ('contracts', 'tests', 'tools', 'lib', 'snapshots', 'deployments', 'docs',
         'foundry.toml', 'README.md', 'Makefile')
# Owner-local Agent configuration is never exported, whether tracked, untracked or absent.
LOCAL_AGENT = ('agents.md', 'agent.md', '.claude')


def allowed_name(path):
    for part in Path(path).parts:
        low = part.lower()
        if low in LOCAL_AGENT:
            return False
        if low.startswith(('.env', 'env.')) or any(x in low for x in
                ('secret', 'credential', 'private_key', 'privatekey', 'private-key',
                 'keystore', 'mnemonic', 'id_rsa', 'id_ed25519')):
            return False
        if low.endswith(('.pem', '.key', '.p12', '.pfx')) or part in ('.DS_Store', '__pycache__'):
            return False
    return True


def safe_relative(path):
    if not path or Path(path).is_absolute() or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('Invalid relative export path')
    if any(ord(c) < 32 for c in path) or not allowed_name(path):
        raise ValueError('Protected or invalid path refused')
    return path


def no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symlink in export path refused')


def isolated_destination(dest):
    if not dest.is_absolute() or not allowed_name(dest):
        raise ValueError('Invalid export destination')
    no_links(dest)
    if dest.exists() or dest.resolve().is_relative_to(ROOT):
        raise ValueError('Export destination must be fresh and outside the checkout')
    for parent in dest.parents:
        if parent.is_dir():
            # Name-only startup guard; never open or report a protected entry.
            if any(n.lower().startswith('.env') for n in os.listdir(parent)):
                raise ValueError('Unsafe export ancestor; refusing environment discovery')


def export(dest):
    isolated_destination(dest)
    exclusions = [':(exclude,icase,glob)**/'+p for p in
                  ('.env*', 'env.*', '*credential*', '*secret*', '*private*key*',
                   '*keystore*', '*mnemonic*', '*.pem', '*.key', '*.p12', '*.pfx')]
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached',
                                   '--others', '--exclude-standard', '--', *ALLOW, *exclusions])
    files = []
    for name in sorted(set(raw.decode().split('\0')) - {''}):
        if not allowed_name(name):
            continue
        safe_relative(name)
        source = ROOT/name
        no_links(source)
        if not source.exists():
            continue
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError('Nonregular export input')
        files.append((name, source))
    # Validate the complete path set before reading any source content.
    dest.mkdir(parents=True)
    identities = {}
    for name, source in files:
        target = dest/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(source.stat().st_mode & 0o777)
        identities[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {'schema': 'faroo-native-export-v1', 'sourceHead': subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(), 'files': identities}
    (dest/'EXPORT-MANIFEST.txt').write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
    print(f'export: {len(files)} native source files')
    print(dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('destination', nargs='?', type=Path)
    args = parser.parse_args()
    dest = args.destination or Path('/private/tmp')/('faroo-native-'+datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    try:
        export(dest)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error)+'\n')


if __name__ == '__main__':
    main()
