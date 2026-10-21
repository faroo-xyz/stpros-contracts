# Source map

Developer documentation: [ARCHITECTURE](ARCHITECTURE.md), [INTEGRATION](INTEGRATION.md),
[SLP-STAKING](SLP-STAKING.md), [PARAMETERS](PARAMETERS.md), [GOVERNANCE](GOVERNANCE.md). Specification and release
design: [SPEC](SPEC.md), [UPGRADES](UPGRADES.md), [RECOVERY](RECOVERY.md), [TOOLCHAIN](TOOLCHAIN.md).
[DEPLOYED-STATE](DEPLOYED-STATE.md) is a generated pre-V4 snapshot.

| Path | Concrete purpose |
| --- | --- |
| `contracts/` | Protocol, authority and atomic release assertions. `SlpPharosProxy.sol` and `SlpPharosRewardsVault.sol` are SLP-layer sources outside the canonical build ([TOOLCHAIN](TOOLCHAIN.md#sources-outside-the-canonical-build)) |
| `lib/` | Exact dependency import closure, identities and licenses |
| `deployments/` | Append-only deployment records: mainnet V1 (top level), mainnet V4 (`mainnet/v4/`, current), testnet; index `registry.json` ([README](../deployments/README.md)) |
| `snapshots/` | Reviewed historical ABI/storage/runtime/state/gas baselines |
| `tools/ci/` | Native compiler and 13-target source/artifact gate |
| `tools/lib/` | ABI/Keccak, strict JSON identity, read-only RPC and bytecode helpers |
| `tools/release/native/` | Unsigned plans, history/economic checkpoints and LOSS recovery |
| `tools/release/` | Registry, recorded-bytecode reconstruction, interfaces, provenance and log archives |
| `tools/snapshot/` | Reproducible historical snapshot generation/comparison |
| `tools/verify/` | Safe export, isolated execution and explicit historical checks |
| `tests/fixtures/legacy/` | Authoritative recorded legacy migration bytecode |
| `tests/fixtures/native-foundation/` | Independent ABI/Keccak witnesses and selected primitive vectors |
| `tests/fixtures/native-release/` | Original upgrade-plan and LOSS security values |
| `tests/fixtures/upgrade-reference/` | Recorded R4 upgrade state and Timelock runtime witnesses |
| `tests/helpers/`, `tests/mocks/` | Shared protocol test base, legacy topology and boundary test doubles |
| `tests/release/`, `tests/protocol/` | Upgrade, recovery, custody, governance and policy regressions |
| `tests/toolchain/` | Compiler drift, primitive and isolation controls |
| `tests/legacy/`, `tests/security/`, `tests/invariant/` | Historical behavior, security regressions and accounting properties |
| `tests/fork/`, `tests/gas/` | Explicitly gated chain-feasibility and gas baselines; not default commands |

Root contract tests protect business behavior during tooling changes. Retired
framework/application sources and complete reference harnesses remain in Research.
