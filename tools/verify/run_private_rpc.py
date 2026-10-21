#!/usr/bin/env python3
"""Runs ``tools/verify/run.sh`` with the caller's private endpoint handed to the export process through a pipe.

    tools/verify/run_private_rpc.py EXPORT_DIR [VAR=value ...] command [args ...]

``PHAROS_RPC_URL`` is read here, in memory only, and written into an anonymous pipe whose read end is inherited by
``run.sh`` and the command it starts. Only the descriptor number (``FAROO_PRIVATE_RPC_FD=<n>``) appears on the command
line; the URL is never an argument, an environment variable of ``run.sh`` or of the command, a file or an output.
``run.sh`` keeps every check and its empty environment. The export side reads the pipe once
(``tests/fork/driver/private_rpc.take``) and closes it. Without the variable this refuses, with no fallback endpoint.
"""
import os
from pathlib import Path
import sys

VARIABLE = 'PHAROS_RPC_URL'
FD_VARIABLE = 'FAROO_PRIVATE_RPC_FD'
MAX_URL_BYTES = 2048


def main(argv):
    if len(argv) < 3:
        print('usage: run_private_rpc.py EXPORT_DIR [VAR=value ...] command [args ...]', file=sys.stderr)
        return 2
    if any(a.startswith(f'{VARIABLE}=') or a.startswith(f'{FD_VARIABLE}=') for a in argv[2:]):
        print(f'run_private_rpc: {VARIABLE} / {FD_VARIABLE} are never passed as arguments', file=sys.stderr)
        return 2
    value = os.environ.get(VARIABLE)
    if not value:
        print(f'run_private_rpc: {VARIABLE} is not set; no fallback endpoint is used', file=sys.stderr)
        return 2
    data = value.encode()
    del value
    if len(data) > MAX_URL_BYTES:
        print(f'run_private_rpc: {VARIABLE} is longer than {MAX_URL_BYTES} bytes', file=sys.stderr)
        return 2
    read_fd, write_fd = os.pipe()
    view = memoryview(data)
    while view:
        view = view[os.write(write_fd, view):]
    os.close(write_fd)
    del data, view
    os.set_inheritable(read_fd, True)
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TERM') if k in os.environ}
    run_sh = str(Path(__file__).resolve().with_name('run.sh'))
    os.execve('/bin/bash', ['bash', run_sh, argv[1], f'{FD_VARIABLE}={read_fd}', *argv[2:]], env)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
