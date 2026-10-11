"""Own the fixed synthetic input root before either differential engine writes it."""
from pathlib import Path
import os

ROOT = Path('/private/tmp/faroo-foundation-inputs')
MARKER = 'Faroo synthetic Foundation fixtures; no user data.\n'


def prepare():
    try:
        ROOT.mkdir(mode=0o700)
        (ROOT / 'OWNER.txt').write_text(MARKER)
    except FileExistsError:
        pass
    if ROOT.is_symlink() or not ROOT.is_dir():
        raise ValueError('unsafe synthetic fixture root')
    owner = ROOT / 'OWNER.txt'
    if owner.is_symlink() or not owner.is_file() or owner.read_text() != MARKER:
        raise ValueError('synthetic fixture root is not owned by this harness')
    for name in ['input.json', 'input.txt']:
        if (ROOT / name).is_symlink():
            raise ValueError('symlink at synthetic fixture file')
    alias = ROOT / 'alias.json'
    if alias.is_symlink() and os.readlink(alias) != str(ROOT / 'input.json'):
        raise ValueError('unexpected synthetic fixture alias')
