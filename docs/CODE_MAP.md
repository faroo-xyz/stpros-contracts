# Faroo code map

Read SPEC → GOVERNANCE → UPGRADES/RECOVERY before using an operational command.
The sibling Research repository owns design decisions, frozen roadmap, audit
reports and build/reproduction evidence; this repository owns implementations
and permanent tests.

| Area | Source | Responsibility / verification |
|---|---|---|
| NAV | `contracts/Oracle.sol` | Canonical T/V, bounded yield reports, commission; INV-A |
| Shares and queue | `contracts/VToken.sol`, `StPROS.sol` | ERC4626 rounding, FIFO requests, partial claims and native deposits; INV-A |
| Custody | `contracts/BridgeVault.sol`, `SlpDistributor.sol` | Withdrawal payouts versus keeper deposit dispatch; faithful WPROS9 tests |
| Round products | `contracts/YieldVault.sol`, `YieldVaultFactory.sol` | Backed managed assets, fixed settlement deadlines, canonical beacon/registry and retirement; INV-B/C |
| Access and migration | `contracts/access/`, `contracts/upgrade/` | Guardian pause, Timelock ownership, version/admin migration guards; INV-C and recorded-bytecode release tests |
| Atomic handoff | `contracts/release/ReleaseAssertions.sol` | H/U assertions, GC-01/02 and rollback; `test/release/` |
| Reviewed planning | `tasks/release/` | Unsigned builders, action-bearing Timelock history/dependencies, authenticated replay and Safe configuration checks |
| Legacy CLI | `tasks/*.ts`, `deploy/*.ts` | Mixed views/user operations and explicitly refusing privileged aliases; `test/tasks/` |
| Historical provenance | `tools/release/`, `tools/lib/` | Python archive/fixture/RPC verification, separate from JS release-time replay |
| Build identity | `tools/snapshot/`, `tools/verify/` | Pinned production compilation, immutable B0 comparison, secret-free export/runner |
| Current models | `test/invariant/` | Bounded stateful A/B/C handlers, independent ghosts, counters and wrong-rule controls |
| Historical witnesses | `test/helpers/LegacyTopology.sol`, `test/fixtures/legacy/`, `deployments/` | Actual recorded implementations; never replace with current source for historical claims |

The README gives preparation order and final compiler settings. Operational Safe
signer/control verification is an off-chain evidence obligation; invariant actors
model authority addresses only. A/B model authorized Timelock calls directly;
C executes real delayed operations. Migration/atomic H/U regressions remain in
`test/release/`, not a second invariant release builder.

`tasks/example.ts` remains a harmless demonstration compatibility command.
`test/mocks/MockWPROS.sol` is retained historical test source, not approved for
native payout fidelity. Use `WPROS9.sol`. The `contants/` spelling and published
address defaults remain compatibility surfaces; defaults are not authenticated
release inventory. No deletion or cosmetic contract rewrite is needed here.
