#!/usr/bin/env bash
# Native-only export. No package installation or framework runtime is needed.
set -euo pipefail
exec python3 "$(dirname "$0")/export.py" "$@"
