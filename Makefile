# All execution stays in the allowlisted export and empty-environment runner.
# Native build requires only Python, reviewed native solc and installed OZ sources.
.NOTPARALLEL:
.PHONY: build toolchain-test
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := build
build:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 tools/ci/canonical.py; echo "Artifacts: $$dest/cache/native-r4"
toolchain-test:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 -m unittest discover -s test/toolchain -p 'test_*.py' -v
