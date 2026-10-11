# Source map

| Path | Concrete purpose |
| --- | --- |
| `contracts/` | Protocol, authority and atomic release assertions |
| `lib/` | Exact dependency import closure, identities and licenses |
| `deployments/` | Immutable historical deployment records |
| `snapshots/` | Reviewed historical ABI/storage/runtime/state/gas baselines |
| `tools/ci/` | Native compiler and 13-target source/artifact gate |
| `tools/lib/` | ABI/Keccak, strict JSON identity, read-only RPC and bytecode helpers |
| `tools/release/native/` | Unsigned plans, history/economic checkpoints and LOSS recovery |
| `tools/release/` | Registry, recorded-bytecode reconstruction, interfaces, provenance and log archives |
| `tools/snapshot/` | Reproducible historical snapshot generation/comparison |
| `tools/verify/` | Safe export, isolated execution and explicit historical checks |
| `test/fixtures/legacy/` | Authoritative recorded legacy migration bytecode |
| `test/fixtures/native-foundation/` | Independent ABI/Keccak witnesses and selected primitive vectors |
| `test/fixtures/native-release/` | Original upgrade-plan and LOSS security values |
| `test/fixtures/r4/` | Recorded state and Timelock runtime witnesses |
| `test/helpers/`, `test/mocks/` | Legacy topology and boundary test doubles |
| `test/release/`, `test/r4/` | Upgrade, recovery, custody, governance and policy regressions |
| `test/toolchain/` | Compiler drift, primitive and isolation controls |
| `test/legacy/`, `test/attack/`, `test/invariant/` | Historical behavior, security regressions and accounting properties |
| `test/fork/`, `test/gas/` | Explicitly gated chain-feasibility and gas baselines; not default commands |

Root contract tests protect business behavior during tooling changes. Retired
framework/application sources and complete reference harnesses remain in Research.
