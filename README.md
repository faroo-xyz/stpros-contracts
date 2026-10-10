# Faroo contracts — R4

Faroo separates staking NAV/share accounting, native custody, withdrawal queues,
and round-based YieldVault products. Read [SPEC](docs/SPEC.md) for product rules,
[GOVERNANCE](docs/GOVERNANCE.md) for authorities, and [CODE_MAP](docs/CODE_MAP.md)
for source and verification entry points. The frozen design and six-batch roadmap
live in the sibling `faroo-contracts-reserch/architecture-hardening/` repository.

## Local preparation and focused verification

Never run Foundry or Hardhat in this checkout: both can load environment files.
Use the allowlisted export and empty-environment runner (see [AGENTS](AGENTS.md)):

```sh
dest=$(tools/verify/export.sh --with-hardhat | tail -1)
tools/verify/run.sh "$dest" python3 tools/ci/canonical.py
# Transitional typings/Rocketh support only:
tools/verify/run.sh "$dest" node node_modules/hardhat/dist/src/cli.js compile --build-profile production
tools/verify/run.sh "$dest" node node_modules/typescript/bin/tsc --noEmit
tools/verify/run.sh "$dest" FOUNDRY_PROFILE=final forge build
tools/verify/run.sh "$dest" node --test test/tasks/*.test.mjs test/release/*.test.mjs
tools/verify/run.sh "$dest" FOUNDRY_PROFILE=final FOUNDRY_INVARIANT_RUNS=256 FOUNDRY_INVARIANT_DEPTH=32 FOUNDRY_INVARIANT_FAIL_ON_REVERT=true forge test --match-path 'test/invariant/*.t.sol' -vv
```

Dependencies must already be installed from the pinned `pnpm-lock.yaml` before
export. Hardhat compile generates real contract typings and Rocketh artifacts;
`tsc` in a clean export before that step is not the generated-artifact baseline.
Node release tests require the preceding Forge build to supply
`out/ReleaseAssertions.sol/ReleaseAssertions.json`. Select additional Solidity
unit/regression tests with `--match-path`, `--match-contract` or `--match-test`.
Do not substitute these commands for the later frozen CI/fork/formal gates.

Final/production compilation is Solidity **0.8.28**, optimizer **200**, **Cancun**.
Foundry's default unoptimized profile is not the release profile. Deployment
identity uses the reviewed source names, versioned dependency remappings, literal
source/IPFS metadata and immutable bindings from `tools/snapshot/build.py`; a
normal test-build hash alone is not production runtime attestation.

`snapshots/` contains immutable B0 historical evidence. `deployments/` and recorded
V1/V3 fixtures are historical witnesses. Current ignored `out/`, `artifacts/` and
`generated/` are local build products, not replacement B0 evidence. Current R4
reviewed build evidence is version-attributed in Research.

Native production builds use `make build` (isolated automatically), or the native
command above; see [TOOLCHAIN](docs/TOOLCHAIN.md). Hardhat is retained for supported
Task execution, typings, Rocketh and explorer verification during migration.

## Operational boundaries

`tasks/release/` builds unsigned H/U plans and verifies authenticated evidence.
Follow [UPGRADES](docs/UPGRADES.md) and [RECOVERY](docs/RECOVERY.md); a locally
passing test is not release approval. All nine old `deploy/` entries refuse and
retain their CLI tags. `upgrade:testnet` retains an explicit refusing alias for
the obsolete CounterV2 command. The 20 obsolete legacy task names have been removed;
`deploy` still refuses before runtime access. Read-only tasks, public deposit/approval commands,
OPS `yv:open-next-round`, and existing unsigned builders retain their semantics.
They are not blanket authorization to sign or broadcast.

The retained OPS round-opening CLI is a direct wallet command, not Safe execution
support. Its Safe/operator integration remains deferred pending owner confirmation;
release-time Safe evidence requirements are unchanged.
