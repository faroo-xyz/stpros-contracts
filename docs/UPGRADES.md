# Offline release construction and verification

All native build and Foundry commands run only inside the native allowlisted export through `tools/verify/run.sh`. Never invoke them in the live checkout. No command below signs, broadcasts, deploys, commits or pushes. Do not supply environment files, private material or wallet clients.

## Reviewed input bundle

The eight `python3 -m tools.release.native release ACTION` commands take `--release-config /absolute/safe/bundle.json` and `--review-hash <independently-approved raw-file SHA-256>`. RPC is explicit: the two approved public read-only endpoints, or localhost for simulations. Write RPC methods and redirects are refused.

Release-mode CLI also binds the legacy proxy, asset, shared ProxyAdmin and beacon targets to `tools/release/registry.json`.

The release JSON contains `mode`, `chainId`, `releaseId`, all `actors`, `addresses`, `implementations`, `factories` with ordered vaults, `policy`, ordered `destinations`, `legacy`, a pinned `observed` snapshot, `inventoryFile`, `artifactFiles` and `review`. Addresses must be canonical lowercase. Frozen synthetic input examples are retained with the upgrade vectors in `tests/release/native/corpus/`; their addresses and bytecodes are **SIMULATION ONLY**, never valid release references. Local test output always says `deployable: false`.

`review` binds the source commit, each normalized compiler artifact's canonical-JSON SHA-256, the complete inventory's canonical-JSON SHA-256, approval reference and actual Safe configurations. The independently approved configuration's raw-file hash is separate from those canonical hashes. A bundle cannot self-approve by substituting observed code as its expected hash. Keep reviewed artifacts, raw configuration approval and RPC observation independently sourced. Candidate artifact hashes in the research evidence require independent review before release use.

Normalized compiler artifacts contain ABI, full deployed bytecode and compiler immutable references. `python3 -m tools.release.native artifact normalize --build-info BUILD_INFO.json --compiler-output OUTPUT.json --source SOURCE --contract CONTRACT` prints a candidate artifact and its digest from a production build (solc 0.8.28, Cancun, optimizer 200). It never marks that artifact approved. Use the **recorded OZ v4** ProxyAdmin runtime reference, not the vendored OZ v5 ProxyAdmin. Vault runtime is frozen independently; Factory reconstruction patches every 32-byte occurrence of its sole immutable to the exact reviewed Vault implementation. Missing code, a proxy substituted for an implementation, wrong bytecode, absent immutable references or a wrong binding all fail.

Inventory fields include chain/pin, pre-creation `fromBlock`, terminal block, gap-free windows, role events/current/ever sets for Timelock and every vault, holder accounts including zero-balance former holders, and outstanding withdrawal records grouped by every historical Withdraw owner with head/tail. RPC preflight independently replays Transfer events into balances/supply and Withdraw events into the complete queue-owner set; record sums must equal Q−C. A caller list or supply sum alone is insufficient.

`mode: release` also requires independently reviewed gates: CLEAR tooling review, fresh fork PASS, FS-6 PASS, counterparty consent and dApp readiness. These are documentary approvals, not claims that the builder itself ran the future gates. Actual Safes, APR, absolute reporting cap, whitelist, operator, reporter, keeper, Treasury, release ID and reviewed deployment identities remain external inputs. Incomplete data fails closed. A simulation is allowed before these release gates, with visibly nondeployable output.

## Commands and sequencing

| Native action (after `python3 -m tools.release.native`) | Result |
|---|---|
| `release preflight` | Validate reviewed bundle, full pinned state/inventory RPC recheck in release mode, offline deployment definitions |
| `release p1-runbook` | Mainnet per-actor P-1 transactions, approved A–D recovery case and residuals |
| `release build-handoff` | Mainnet H Safe Transaction Builder JSON; atomic MultiSend required |
| `release build-u` | Ordered U/U_t calls, exact operation ID, unsigned execute and cancel calls |
| `release estimate-gas` | Local-only individual estimates; failures are INCOMPLETE; never FS-6 |
| `release testnet-handoff` | Separate actors and T-0…T-5/U_t stages with view assertion after T-5 |
| `release postcheck` | Fresh post-state identity/version/authority/economic scan plus assertR4 and operation Done |
| `release authority` | Full reviewed historical role inventory and completeness assumptions |

The reviewed snapshot is checked by exact block number, hash and timestamp; it need not equal `latest`. Pre-schedule review age is at most one hour. The current head must be no more than 120 seconds old (at most 30 seconds future clock skew); the scan must finish within a 120-second head advance. Both anchor hashes are checked again at completion. An unrelated new block does not require new calldata or approval. A reorg, missing archive data, stale scan, implementation/admin event outside the reviewed sequence, or unexplained state change fails closed.

Initial preflight checks the reviewed state and current state independently: code, proxies, beacons, complete vault discovery, owners, roles, Safe internals, signing nonces, wiring and Oracle configuration. Full Transfer/Withdraw histories discover new holders and queue owners. Permitted deposits/claims are reconciled against events; existing withdrawal metadata stays identical, FIFO pending reductions match claims, Q/C totals match events, Vault TMA matches ERC-4626 flows, and testnet pool changes match deposits/redemptions/reporting. Unexplained ledger or record changes are rejected. These checks rely on complete honest archive RPC data, not a caller-supplied permission-to-drift flag.

Use `release preflight --checkpoint execution` with the **same frozen pre-H bundle**. The canonical old pin remains valid across the mandatory delay; the current H state, critical configuration and economic changes are independently checked. Release mode also requires `--checkpoint-review /safe/execution.json --checkpoint-review-hash <independently-approved SHA-256>`. This separate, freshly reviewed artifact binds `operationId`, `blockNumber`, `blockHash`, `forkReference`, `freshFork: "PASS"`, `fs6: "PASS"`, measured `uGas`, and current `nonces.gov` / `nonces.can`. Its canonical fork block must be no more than one hour behind the current checkpoint. This renews execution/fork/signing evidence without changing U's targets, payloads, predecessor, salt or ID.

Nonce semantics are explicit: initial preflight checks the reviewed nonce at the historical and current signing state. H/execution checks Safe structure and separately approved fresh signing nonces, not the consumed H nonce. Postcheck checks structure and completed operation but does not demand that an already consumed signing nonce remain unchanged. Safe owners/threshold/module/guard changes are never treated as ordinary user activity. Governance calldata must still be independently approved; JSON PASS fields alone are not proof that a fork or independent review occurred.

Mainnet primary sequence:

1. P-0 offline definitions: Timelock, separately reviewed Vault/Factory implementations, fresh BridgeVault and SlpDistributor proxies owned by TL using the existing v4 ProxyAdmin, and ReleaseAssertions. Grant CAN, remove temporary deployer admin and verify complete Timelock history. Definitions never execute deployment.
2. P-1: verify Factory_test/B2 identities; factory owner emergency-cancels if needed; test-vault admin revokes every inventoried role and renounces ADMIN last; factory owner transfers Factory_test to TL. Release H generation requires completed P-1 state (case A).
3. H, **one atomic Safe MultiSend**: PA ownership to TL; StPROS/Oracle/main Factory ownership to TL; vault 001 grants TL ADMIN and revokes all old roles, GOV renounces ADMIN last; schedule U; `assertHandoff` including pending U.
4. U, **one atomic Timelock batch** after 48h: Oracle V1 upgradeAndCall; immediately StPROS V1 upgradeAndCall; main Factory migration; main beacon upgrade; vault 001 OPS grant; Factory_test migration; B2 upgrade; test-vault retirement; Factory_test closeCreation; `assertR4` last.

For B/D, approved fallback H item 0 migrates Factory_test before PA handoff, and U omits its repeated migration. C/D preserve inventoried stale test-vault roles as explicitly accepted, inert roles after retirement. The mainnet governed vault must always be SETTLED. For the separate retirement target, H retains the reviewed phase in non-residual cases; owner-approved C/D residuals require explained transitions rather than a frozen phase. The governed vault keeps its round/terms; retired vault safety depends on final SETTLED/retired/backing, not freezing terms an owner-accepted stale admin could change during H→U.

Testnet: T-0 inventories all 15 vaults. T-1 E2 revokes every old role, grants TL ADMIN only for index 14, then renounces E2 ADMIN on each. T-2 E2 transfers Factory; T-3 E1 transfers four core owners; T-4 E1 transfers PA last. T-5 GOV schedules U_t and calls the handoff assertion separately. U_t migrates Oracle V3 with explicit canonical asset, StPROS V2, BridgeVault, SlpDistributor and Factory; upgrades the beacon; grants OPS at index 14 and retires the other 14; full assertR4 is last. Approved E2-unavailable recovery retires all 15 and uses the distinctly named `assertTestnetRecoveryHandoff`; stale roles remain visible and inert after U_t.

Salt is `keccak256(UTF8(releaseId))`; predecessor is zero. ID is the OZ ABI-encoded hash of targets, values, payloads, predecessor and salt. `verifyOperation` checks the entire object. Oracle and StPROS are always adjacent in a single atomic operation. No default U1/U2 split exists. `assertCoreR4` is only a contingency diagnostic and cannot satisfy the full release gate.

## Coverage boundaries

On-chain: runtime identities, v4 proxy admin/implementation getters, owners/guardians, exact delay, specified roles, beacons, canonical core/vault relationships, governed round terms/phase, zero launch Factory fee, creation closure/retirement, backing, reporting configuration, pool/supply and V1 custody relations, and pending operation.

Off-chain: complete actor discovery, initializer version slots, Safe internals, provenance/approval, per-holder/withdrawal enumeration, full factory discovery, before/after transaction-frame economic conservation, fork/dApp/gas acceptance. POST-3 native balance deltas and POST-4 exact supply/TMA preservation **cannot** be proved by comparing a pre-H snapshot with a later block that includes legitimate user activity. They require the reviewed migration implementation plus execution-frame/fork evidence in Batch 04. Postcheck reports its limited scope; it does not certify all PRE/POST gates. A later yield update can invalidate an immediate-migration rate assertion: preserve execution-time evidence, do not relax assertions.

For owner-approved C/D and E2-unavailable recovery, retired-vault roles are fully rescanned and reported as informational after retirement, not enforced as an immutable pre-H role set. H reports their explicitly accepted exposure until U. Code identity, factory ownership, terminal retirement and backing remain mandatory. A stale admin must not gain a migration veto by granting or renouncing an otherwise inert role. Normal GOVERNED and A/B-cleaned vault role sets remain exact.

The existing approved testnet T-3-incomplete recovery is explicit as `testnetRecovery.incompleteT3: true` plus its review/owner approval. The runbook omits T-3, uses the recovery handoff assertion with the reviewed remaining core owners, and still requires T-4 PA ownership to TL. U_t retains all four core migrations and final TL ownership checks. This flag never permits a mainnet partial handoff.

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

The native B0 `plan.json` and chunk JSON schema is reused, including chain ID,
start/end, canonical end hash, exact address filter ID, per-window log counts,
and raw chunk hashes in the new manifest. Every required target must be covered
from the inventory start through the reviewed anchor, without gaps or overlapping
segments. Each archive window is at most 1,000 blocks, including empty windows.
Original B0 inventory plans/chunks remain immutable. Only the numeric chunk JSON
members of the approved B0 archive are relevant; platform metadata is not input.

B0 alone is **not** a current release proof. New Timelock/assertion/fresh proxy
addresses and blocks after the B0 pin require separately collected and reviewed
supplemental plans/chunks. A new target must have complete pre-creation-to-anchor
coverage too. No fabricated address, empty-history assertion, `complete: true`
flag, or hash generated from unchecked RPC output substitutes for this review.
Absent archives, hash mismatches, incomplete target/window coverage, unavailable
archive state or a changed canonical anchor stop verification.

Within one invocation, historical logs are authenticated locally and authoritative
historical views are checked once. Incremental logs are fetched for **all targets
and topics together**, one request per non-overlapping 1,000-block window, then
shared by role, Transfer, withdrawal, configuration and upgrade checks. Every
window is awaited, including empty results; failures cannot mean “no events.”
New holders/owners/roles are discovered from events and verified against pinned
views. Record metadata, per-record queued baselines, global Q/C, balances, supply,
Oracle pool changes and Vault asset changes are reconciled. R4 same-owner requests
store an unbound receiver; recorded V3 requests bind even a same-owner receiver.
V1 records gain the zero receiver field at the checked R4 upgrade, without
changing historical unbonding snapshots.

Independent view reads are batched up to 20 with strict response-ID/slot checks.
Public requests have a 350 ms minimum interval; 429/selected 5xx and rate-limit RPC
errors receive at most two backoff retries. Other failures stop immediately.
Historical authentication has no current-head timeout. Final current-state evidence
must remain within 120 seconds; a slow pass catches up from its last fully scanned
block and rechecks current views at the new head (at most three passes). Historical,
window-end and event-block canonical hashes are checked. The original reviewed
configuration and scheduled U are never replaced during the 48-hour delay.

This is not a proof against a dishonest RPC provider returning consistently
fabricated logs/state; independently reviewed archive evidence and correct public
read-only RPC responses remain explicit trust assumptions. Large account sets or
persistent provider slowness can still fail the final freshness gate. Missing
release proof inputs and future full-fork/transaction-frame preservation/FS-6 gates
remain release blockers. No new deployment or release approval is granted here.

## Authenticated release inventory and remediation checks

The read-only release checkpoint replays all Timelock `CallScheduled`,
`CallSalt`, `CallExecuted` and `Cancelled` events from the authenticated
creation-spanning archive and its gap-free incremental journal. It reconstructs
single/batch operation IDs, cancellation/re-scheduling and completion, and checks
on-chain timestamps. The reviewed plan permits no outstanding action-bearing operation before
scheduling, exactly the frozen U ID at H/execution, and none after U; U must be
recorded executed after its delay. A removed proposer does not remove its queued
operations. Unrelated pending action-bearing operations require review and cancellation before
this release plan can pass. ReleaseAssertions does not enumerate operations.

Reporter and keeper cannot be GOV, CAN or Timelock. Testnet uses the reporter
preserved from the authenticated Oracle configuration, not an unused replacement
input. The R4 atomic assertion independently checks this separation. GC-02 C requires OPS distinct from GOV/CAN/Timelock and its reviewed signer-control set disjoint from GOV/CAN. OPS may share the same reviewed GRD Safe. Alias configurations must agree; unproven contract/delegated signer control is INCOMPLETE.

`VaultCheck.residualRecovery` was added by the preceding remediation, changing
the original frozen ABI. This governance closure retains that calldata shape. Only approved mainnet
C/D residual retirement targets or testnet E2-unavailable retirement targets may
set it. GOVERNED vaults cannot use it. Historical state remains strictly checked;
H permits reconciled residual phase/round/terms transitions, and U still requires
terminal SETTLED retirement, backing, canonical identity and creation closure
where specified. New assertion artifacts and newly reviewed H/U calldata are
required; an already scheduled operation cannot be rewritten.

GC-01 A is now approved: the mainnet GOVERNED historical Vault must be
SETTLED at input, current pre-schedule, actual H, execution checkpoint and final
U. OPEN/LOCKED/SETTLE_PROPOSED are rejected, even if a caller supplies matching
expected phase calldata. Mainnet `Common.vaults` keeps the builder's canonical
order: GOVERNED first, retirement target second; atomic assertions also enforce
that classification so clearing a flag cannot disable the phase/round/terms guards.
Historical rounds, residual recovery, testnet phases
and the Vault business state machine are unchanged. No arbitrary timing buffer.

GC-03 B is now approved: `actionBearingCoverage: PASS` means creation-to-current
coverage of operations containing at least one call, under the independently
reviewed archive/RPC assumptions and pinned runtime. `literalAllOperationIds:
NOT_CLAIMED` remains explicit. `dependencyVerification` records authoritative
state for every referenced predecessor, including silent empty IDs. Pure parsing
returns `UNATTESTED`; only the authenticated checkpoint binds coverage and the
source runtime. Release CLI checks these scoped statuses and the current history
anchor; it no longer requires impossible global empty-ID discovery. Interleaved
nested executions are accumulated by operation, not contiguous event fragments.
Empty CallSalt/Cancelled metadata is retained without inventing silent history.
A whole omitted operation cannot be discovered by hashing other supplied rows.

The U predecessor is always zero. Waiting and Ready actions with unresolved
predecessors still count as outstanding. Any fresh artifact/address or policy
input change needs a newly reviewed candidate; never mutate an already scheduled
operation or rebuild its calldata from a post-H state.
