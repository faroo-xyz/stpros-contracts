# All execution stays in the allowlisted export and empty-environment runner.
# Native build requires only Python, reviewed native solc and vendored sources.
.NOTPARALLEL:
.PHONY: build toolchain-test upgrade-test upgrade-rehearsal
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := build
build:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 tools/ci/canonical.py; echo "Artifacts: $$dest/cache/native-r4"
toolchain-test:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 -m unittest discover -s test/toolchain -p 'test_*.py' -v
upgrade-test:
	@dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" python3 test/release/native/gate.py; \
	tools/verify/run.sh "$$dest" python3 -m unittest discover -s test/release/native -p 'test_*.py' -v; \
	tools/verify/run.sh "$$dest" FOUNDRY_PROFILE=final forge test --offline --match-contract '^(FoundationWitnessTest|NativeReleaseWitnessTest|LossContingencyTest)$$' -vv
upgrade-rehearsal:
	@dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" python3 tools/ci/canonical.py; \
	tools/verify/run.sh "$$dest" FOUNDRY_PROFILE=final forge test --offline --match-contract 'Anvil.*Fixture' --match-test '^test_exportAnvil' -vv; \
	tools/verify/run.sh "$$dest" python3 test/release/native/rehearsal.py
