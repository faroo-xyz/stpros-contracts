# Upgrades

## Purpose

This document describes the upgrade architecture of the Faroo contracts, the release tooling that prepared the V4
upgrade, and what a future upgrade must review and prove. It is for maintainers and reviewers, not for integrators. Roles and the Timelock process are in
[GOVERNANCE.md](GOVERNANCE.md); failure handling is in [RECOVERY.md](RECOVERY.md#during-a-governed-upgrade); build and
isolation rules are in [TOOLCHAIN.md](TOOLCHAIN.md).

The mainnet V4 upgrade was executed on 2026-10-21 (handoff at block 20,812,661, upgrade at block 20,813,316). Its
as-executed record is [`deployments/mainnet/v4/V4_UpgradeReceipt.json`](../deployments/mainnet/v4/V4_UpgradeReceipt.json).
The step-by-step V4 preparation and its approvals are archived in the research repository.

No command in this repository signs, broadcasts, deploys, commits or pushes. All native build and Foundry commands run
only inside an isolated export through `tools/verify/run.sh`. Do not supply environment files, private material or
wallet clients.

## Scope of the release tooling

Two things are separate:

- **The upgrade architecture is general.** The protocol stays upgradeable through the shared ProxyAdmin, the Factory's
  vault beacon and the Timelock ([below](#upgrade-architecture)). Any future implementation goes through that path.
- **The native release builder is specific to V4.** `python3 -m tools.release.native release ...` implements the V4
  release that was executed on mainnet and prepared for testnet. It is not a general tool for an arbitrary future
  version.

What the builder binds to V4:

| Part | V4 binding |
|---|---|
| `release build-u` | Builds exactly the V4 batch. Mainnet: Oracle `migrateFromV1`, StPROS `migrateFromV1`, Factory `migrateToR4`, the beacon upgrade, the vault role grants, the waiting-period and SlpDistributor configuration, then `assertR4`. Testnet: Oracle `migrateFromV3`, StPROS `migrateFromV2`, BridgeVault, SlpDistributor and Factory `migrateToR4`, then `assertR4` |
| `release build-handoff`, `release testnet-handoff`, `release p1-runbook` | The V4 ownership handoff (`assertHandoff`, `assertTestnetRecoveryHandoff`) and its preparation |
| `release preflight`, `release postcheck` | The pre-V4 legacy state as input, version 4 after the upgrade, the V4 expectations of `ReleaseAssertions` and the V4 role and configuration rules |
| `ReleaseAssertions` | `assertHandoff`, `assertR4` and `assertCoreR4` check the V4 end state only |

The rules behind the builder remain the reference for any governed release: reviewed artifacts and bundle, history
authentication, coverage boundaries, the Timelock operation inventory, freshness and fail-closed checks. Some generic
parts can be reused after review, for example `artifact normalize` and the history authentication. `loss contingency`
prepares LOSS plans for the current V4 vaults.

**A future upgrade must**
- review its new migration code (reinitializer, `MigrationGuard` version gate, state carried over);
- review its storage layout against the protected snapshots;
- review or rebuild the transaction-building logic for its own batch;
- provide a release assertion for the new version;
- collect its own execution evidence: fork, gas, history and checkpoint.

Do not reuse the V4 transactions or calldata, and do not treat `assertR4` or a V4 postcheck as acceptance of a new
version. Extending the builder for a new release is a reviewed tooling change of its own.

## Upgrade architecture

| Component | Upgrade mechanism | Authority |
|---|---|---|
| StPROS, Oracle, BridgeVault, SlpDistributor, YieldVaultFactory | OpenZeppelin v4 `TransparentUpgradeableProxy`; `ProxyAdmin.upgrade` / `upgradeAndCall` on the shared ProxyAdmin `0x4238ea4adfa2bd6a5fc9b5e245dc1900cf0258aa` | Timelock (owner of the ProxyAdmin) |
| YieldVault implementation | `UpgradeableBeacon` owned by the Factory; `YieldVaultFactory.upgradeBeaconTo` | Timelock (owner of the Factory) |
| TimelockController | Not upgradeable. Roles and delay change only through its own operations | Timelock |
| SLP execution contracts and RewardsVault | Their own ProxyAdmin `0x480eb384681777bf21e72b8c8538987c2801b4b7` | SLP administrator, **outside the Timelock** |

Rules for core upgrades:
- Use the **recorded OZ v4** ProxyAdmin runtime reference, not the vendored OZ v5 ProxyAdmin. A new core proxy uses
  the recorded OZ v4 `TransparentUpgradeableProxy` creation code under the shared ProxyAdmin; an OZ v5 proxy would
  create its own ProxyAdmin.
- Migrations run through `upgradeAndCall` into a guarded reinitializer. `MigrationGuard` checks the proxy admin and the
  previous version before `reinitializer(n)`; fresh initialization is constructor-only. Never upgrade a core contract
  implementation-only when its migration is required, and never call an unguarded initializer.
- Release builders never produce `changeProxyAdmin`, `renounceOwnership` or an implementation-only core upgrade.
- `assertCoreR4` is a contingency diagnostic only; it never satisfies the full release assertion.
- Oracle and StPROS migrations are adjacent in one atomic operation. A Factory is upgraded before its beacon when the
  new vault implementation reads new Factory state.

### Storage layout

Upgrades must keep the existing layout. The protected layouts are in `snapshots/` and are compared with `--check`
([TOOLCHAIN.md](TOOLCHAIN.md#focused-checks)).

- Factory slot 4 packs `timelock` (bytes 0–19), `creationClosed` (byte 20) and `approvedPerformanceFeeBps`
  (bytes 21–22). V4 introduced no new linear Factory slot.
- YieldVault storage ends with `retired` in slot 22. The Oracle's `maxAprPpm` is slot 9. Deprecated VToken slots stay
  in place.
- Guardian and `Ownable2Step` storage is namespaced (`GuardedOwnableUpgradeable`).
- Fields added by an upgrade must be zero-safe: legacy proxies read the zero value until configured.

## Procedure

The V4 release followed these steps with the commands below. A future release keeps the same sequence and evidence
requirements, with commands and assertions reviewed for that release ([scope](#scope-of-the-release-tooling)).

1. **Build and review artifacts.** Build in an export (solc 0.8.28, Cancun, optimizer 200). Normalize each candidate:
   `python3 -m tools.release.native artifact normalize --build-info BUILD_INFO.json --compiler-output OUTPUT.json
   --source SOURCE --contract CONTRACT` prints the artifact and its digest; it never marks it approved. Vault runtime is
   frozen independently; Factory reconstruction patches every 32-byte occurrence of its sole immutable to the exact
   reviewed Vault implementation.
2. **Write the reviewed input bundle** ([below](#reviewed-input-bundle)) and have it approved independently. Its raw
   SHA-256 is the `--review-hash`.
3. **Preflight.** `release preflight` checks the bundle, the deployment definitions and the pinned and current chain
   state, including the complete history ([authenticated history](#authenticated-history-and-incremental-release-checkpoints)).
4. **Deploy new implementations or proxies** from the reviewed definitions. Deployment is outside this repository's
   tooling; the definitions never execute it. Record every deployment in `deployments/` (append-only).
5. **Schedule** the upgrade as one Timelock operation from the GOV Safe. Salt is `keccak256(UTF8(releaseId))`;
   predecessor is zero. The operation ID is the OpenZeppelin hash of targets, values, payloads, predecessor and salt.
   The last call of the batch is the release assertion (`ReleaseAssertions`), so the batch reverts if the end state is
   wrong.
6. **Execution checkpoint.** After the delay, run `release preflight --checkpoint execution` with the **same frozen
   bundle**, plus a separately reviewed checkpoint file (`--checkpoint-review`, `--checkpoint-review-hash`) that binds
   the operation ID, the fork evidence, the measured gas and the current Safe nonces. This renews execution evidence
   without changing the operation.
7. **Execute** from the GOV Safe with identical fields.
8. **Postcheck.** `release postcheck` re-reads identity, version, authority and economic state and checks that the
   operation is Done. Record the result.

Independent review is required for the artifacts, the bundle, the checkpoint file and the calldata. JSON `PASS` fields
are not proof that a fork or a review happened. Simulations are allowed before the release gates; their output is
visibly non-deployable (`deployable: false`).

### Commands

| Native action (after `python3 -m tools.release.native`) | Result |
|---|---|
| `release preflight` | Validate the reviewed bundle; full pinned-state and history recheck in release mode; offline deployment definitions |
| `release build-u` | The V4 upgrade batch ([scope](#scope-of-the-release-tooling)): ordered calls, exact operation ID, unsigned execute and cancel calls |
| `release estimate-gas` | Local individual estimates only; failures are INCOMPLETE; never an atomic gas certification |
| `release postcheck` | Fresh post-state identity, version, authority and economic scan, release assertion and operation Done |
| `release authority` | Full reviewed historical role inventory and completeness assumptions |
| `release build-handoff` | Mainnet V4 handoff Safe calldata (the handoff was executed at block 20,812,661) |
| `release p1-runbook` | Mainnet Factory_test preparation of the original V4 scope; not used, because Factory_test was left out of V4 |
| `release testnet-handoff` | Testnet stages and approved recovery ([below](#testnet-v4-sequence-not-executed)) |

All `release` actions implement the V4 release. Use each action's `--help`. RPC is explicit: the two public read-only endpoints, or localhost for simulations. Write
RPC methods and redirects are refused.

## Reviewed input bundle

The `release ACTION` commands take `--release-config /absolute/safe/bundle.json` and
`--review-hash <independently approved raw-file SHA-256>`. Release mode also binds the legacy proxy, asset, shared
ProxyAdmin and beacon targets to `tools/release/registry.json`.

The release JSON contains `mode`, `chainId`, `releaseId`, all `actors`, `addresses`, `implementations`, `factories`
with ordered vaults, `policy`, ordered `destinations`, `legacy`, a pinned `observed` snapshot, `inventoryFile`,
`artifactFiles`, `historyFile` and `review`. Addresses are canonical lowercase. Frozen synthetic examples are in
`tests/release/native/corpus/`; their addresses and bytecodes are **SIMULATION ONLY**.

`review` binds the source commit, each normalized artifact's canonical-JSON SHA-256, the inventory's canonical-JSON
SHA-256, `historySha256` (raw file) and the approval reference. A bundle cannot self-approve by substituting observed
code as its expected hash. Keep reviewed artifacts, the configuration approval and RPC observations independently
sourced. Missing code, a proxy in place of an implementation, wrong bytecode, absent immutable references or a wrong
binding all fail. Incomplete data fails closed.

`mode: release` also requires `releaseGates`: `independentReview: "CLEAR"`, `freshFork: "PASS"` and `fs6: "PASS"`
(atomic gas), the measured `hGas` / `uGas` within the gas cap (2,200,000 unless a higher cap is separately approved),
fork evidence bound to the reviewed block hash and at most one hour older than the reviewed snapshot, `counterpartyConsent: true` and `dappReady: true`. These are
documentary approvals: the builder does not run the gates itself.

The inventory covers the chain and pin, a pre-creation `fromBlock`, gap-free windows, role events and current and
ever-held role sets for the Timelock and every vault, holder accounts (including zero-balance former holders), and
outstanding withdrawal records of every historical queue owner. Preflight replays Transfer events into balances and
supply and Withdraw events into the queue-owner set; record sums must equal `Q − C`. A caller list or a supply sum alone
is not enough.

**Freshness.** The reviewed snapshot is checked by exact block number, hash and timestamp; it need not equal `latest`.
Pre-schedule review age is at most one hour. The current head must be at most 120 seconds old (at most 30 seconds of
future clock skew), and the scan must finish within a 120-second head advance. Both anchor hashes are checked again at
completion. A reorg, missing archive data, a stale scan, an implementation or admin event outside the reviewed
sequence, or an unexplained state change fails closed. Permitted user activity (deposits, claims, reports) is
reconciled against events; unexplained ledger or record changes are rejected.

**Nonces.** Initial preflight checks the reviewed Safe nonce. The execution checkpoint checks separately approved fresh
nonces. Postcheck accepts consumed nonces but never a changed Safe authority configuration.

## Coverage boundaries

On-chain (release assertion): runtime identities, proxy admin and implementation getters, owners and guardians, exact
delay, specified roles, beacons, canonical core and vault relationships, governed round terms and phase, Factory fee,
creation closure and retirement, backing, reporting configuration, pool and supply relations, and the pending
operation. `ReleaseAssertions` has no storage, privileged function, delegatecall or state-changing external call; every
function is view or pure and checks by STATICCALL. Proxy admin and implementation checks go through the recorded OZ v4
ProxyAdmin's `getProxyAdmin` / `getProxyImplementation`.

Off-chain (release tooling): complete actor discovery, initializer version slots (pinned storage reads at the
OpenZeppelin namespaced slot), provenance and approval, per-holder and per-withdrawal enumeration, full factory
discovery, before/after economic conservation, and fork, dApp and gas acceptance. `AccessControl` is not enumerable, so
a role granted to a never-seen account is invisible to the on-chain subset; only the off-chain role-history replay
detects it. A role-array hash binds calldata but does not prove inventory completeness.

Native balance deltas and exact supply preservation **cannot** be proved by comparing a pre-schedule snapshot with a
later block that includes legitimate user activity; they need execution-frame or fork evidence. Postcheck reports its
limited scope. A later yield report can invalidate an immediate-migration rate assertion: preserve execution-time
evidence, do not relax assertions.

### Authenticated history and incremental release checkpoints

Live native release checks require `historyFile` in the independently reviewed
release configuration and `review.historySha256` (the **raw file** SHA-256).
The history file uses the following structure; addresses and hashes below are
schema placeholders, never deployable values:

```json
{
  "schema": "faroo-history-v1",
  "chainId": 1672,
  "fromBlock": 11074115,
  "blockNumber": 0,
  "blockHash": "REVIEWED_ANCHOR_HASH",
  "inventorySha256": "SAME_AS_CONFIG_REVIEW_INVENTORY_SHA256",
  "targets": ["SORTED_EXACT_HISTORY_TARGET_SET"],
  "segments": [{
    "planFile": "/absolute/safe/plan.json",
    "planSha256": "RAW_PLAN_SHA256",
    "chunks": [{"path": "/absolute/safe/chunk.json", "sha256": "RAW_CHUNK_SHA256"}]
  }]
}
```

`blockNumber` / `blockHash` equal the original reviewed configuration anchor.
`inventorySha256` binds the normalized role, holder and withdrawal inventory.
`historyTargets(config)` defines the exact set: core proxies, ProxyAdmin,
Timelock, assertion contract, asset, every Factory, Beacon and Vault. Paths must
be non-secret JSON regular files with no symlink components. The CLI inherits
its existing independent configuration-file approval requirement; generating
hashes locally is not independent approval.

The plan and chunk JSON schema of the original history archive is reused,
including chain ID, start/end, canonical end hash, exact address filter ID,
per-window log counts, and raw chunk hashes in the new manifest. Every required
target must be covered from the inventory start through the reviewed anchor,
without gaps or overlapping segments. Each archive window is at most 1,000
blocks, including empty windows. The original inventory plans and chunks remain
immutable. Only their numeric chunk JSON members are relevant; platform metadata
is not input.

The original archive alone is **not** a current release proof. New
Timelock, assertion or proxy addresses, and blocks after its pin, require
separately collected and reviewed supplemental plans and chunks. A new target
must have complete pre-creation-to-anchor coverage too. No fabricated address,
empty-history assertion, `complete: true` flag, or hash generated from unchecked
RPC output substitutes for this review. Absent archives, hash mismatches,
incomplete target or window coverage, unavailable archive state or a changed
canonical anchor stop verification.

Within one invocation, historical logs are authenticated locally and authoritative
historical views are checked once. Incremental logs are fetched for **all targets
and topics together**, one request per non-overlapping 1,000-block window, then
shared by role, Transfer, withdrawal, configuration and upgrade checks. Every
window is awaited, including empty results; failures cannot mean “no events.”
New holders/owners/roles are discovered from events and verified against pinned
views. Record metadata, per-record queued baselines, global Q/C, balances, supply,
Oracle pool changes and Vault asset changes are reconciled. V4 same-owner requests
store an unbound receiver; recorded V3 requests bind even a same-owner receiver.
V1 records gain the zero receiver field at the V4 upgrade, without changing
historical unbonding snapshots.

Independent view reads are batched up to 20 with strict response-ID/slot checks.
Public requests have a 350 ms minimum interval; 429/selected 5xx and rate-limit RPC
errors receive at most two backoff retries. Other failures stop immediately.
Historical authentication has no current-head timeout. Final current-state evidence
must remain within 120 seconds; a slow pass catches up from its last fully scanned
block and rechecks current views at the new head (at most three passes). Historical,
window-end and event-block canonical hashes are checked. The original reviewed
configuration and the scheduled operation are never replaced during the Timelock delay.

This is not a proof against a dishonest RPC provider returning consistently
fabricated logs/state; independently reviewed archive evidence and correct public
read-only RPC responses remain explicit trust assumptions. Large account sets or
persistent provider slowness can still fail the final freshness gate.

### Timelock operation inventory

The read-only release checkpoint replays all Timelock `CallScheduled`, `CallSalt`, `CallExecuted` and `Cancelled`
events from the authenticated creation-spanning archive and its gap-free incremental journal. It reconstructs single and
batch operation IDs, cancellation, re-scheduling and completion, and checks on-chain timestamps.

- Coverage is every **action-bearing** operation (at least one target call, including zero-value and empty-calldata
  calls) plus the state of every referenced predecessor. Discovery of event-silent empty operation IDs is not claimed.
- Outstanding means Waiting or Ready, even with an unresolved predecessor.
- A release requires no outstanding action-bearing operation before scheduling, exactly the frozen upgrade operation
  between scheduling and execution, and none afterwards, with the upgrade operation Done after its delay.
- A removed proposer does not remove its queued operations. Unrelated pending operations must be reviewed and cancelled
  before the release can pass. `ReleaseAssertions` does not enumerate operations.
- Any fresh artifact, address or policy input change needs a newly reviewed candidate. Never mutate a scheduled
  operation or rebuild its calldata from a later state.

## Testnet V4 sequence (not executed)

Testnet (chainId 688689) has not been upgraded to V4. At testnet block 31,692,763 every recorded proxy still used its
pre-V4 implementation, and the contracts were owned by externally owned accounts
([deployments/testnet](../deployments/testnet/README.md)). The prepared sequence, built by `release testnet-handoff`,
uses a Timelock delay of 300 seconds:

1. **T-0** inventories all 15 testnet vaults.
2. **T-1** E2 revokes every old vault role, grants the testnet Timelock `DEFAULT_ADMIN_ROLE` only on vault index 14,
   then renounces its own admin role on each vault.
3. **T-2** E2 transfers the Factory; **T-3** E1 transfers the four core owners; **T-4** E1 transfers the ProxyAdmin
   last.
4. **T-5** GOV schedules the upgrade U_t and calls the handoff assertion separately.
5. **U_t** migrates Oracle V3 (with the explicit canonical asset), StPROS V2, BridgeVault, SlpDistributor and the
   Factory; upgrades the beacon; grants OPS on vault 14 and retires the other 14 vaults; the full release assertion is
   last.

| Testnet state | Required response |
|---|---|
| T-1 partly processed | The builder skips only vaults whose complete current roles prove the handoff finished; E2 processes the rest |
| E2 unavailable | Explicit recovery approval. All 15 vaults are retired, inert stale roles are preserved and reported, and the distinct `assertTestnetRecoveryHandoff` is used. U_t forces the Factory owner to the Timelock |
| T-3 incomplete after the ProxyAdmin handoff | Use the reviewed `testnetRecovery.incompleteT3` flag with owner approval. The recovery assertion checks the listed remaining owners; U_t forces core ownership to the Timelock. E1 is still required for T-4. This flag never permits a mainnet partial handoff |
| T-2 / T-3 already completed | The builder omits completed transfers from fresh state; T-4 is still the last E1 step |
| E1 unavailable before T-4 | BLOCKED. An offline plan is not proof of signer availability |

Testnet-specific migration behaviour: the Oracle V3 migration keeps the testnet commission configuration, reporter and
non-1:1 pool accounting. The historical testnet waiting period of zero stays in effect for new requests until governance
sets a value, which must then be between one and thirty days; existing zero-wait records stay zero-wait.

For E2-unavailable retirement targets, the residual exception covers only reconciled phase, round and terms transitions
of those vaults; the historical anchor, role history, code, proxy and beacon identity, factory and asset wiring, final
retirement and backing remain mandatory. A testnet multisig without supported Safe introspection is unsupported. The
detailed design and its review history are archived in the research repository.
