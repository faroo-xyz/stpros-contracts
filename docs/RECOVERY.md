# Recovery

## Purpose

This is the recovery manual for the deployed V4 protocol on Pharos Mainnet: what to do when custody, the redemption
queue, the Oracle, a YieldVault round or a governed upgrade goes wrong. Roles and the Timelock process are in
[GOVERNANCE.md](GOVERNANCE.md); parameter values are in [PARAMETERS.md](PARAMETERS.md).

Rules that apply to every case:
- No recovery is automatic. Each needs fresh read-only identity checks at a pinned block and reviewed, unsigned
  calldata. No repository command signs or broadcasts.
- Governance actions other than a pause go through the Timelock and wait its delay (600 seconds at block 20,815,667).
  There is no faster path.
- Never change a proxy admin, renounce ownership or bypass a guarded migration to recover.
- Never modify a pending Timelock operation. Cancel it and schedule a new, reviewed operation with the full delay.
- Keep user records unchanged: no recovery skips, reprices, reorders or edits redemption records.
- When identities, reconciliation or funding are unresolved, keep the affected function paused and report
  **BLOCKED**.

## Emergency controls

| Control | Who | Effect |
|---|---|---|
| `StPROS.pause()` | Guardian Safe, or the Timelock as owner | Stops deposits and new redemption requests, and commission minting. Claims continue |
| `Oracle.pause()` | Guardian Safe, or the Timelock | Stops yield reports (`update`) and, through `onMint` / `onRedeem`, deposits and new requests. Claims continue |
| `BridgeVault.pause()` | Guardian Safe, or the Timelock | Stops every claim payout (`withdrawToken`, `withdrawTokenWrapped`). `emergencyWithdraw` stays available to the owner |
| `SlpDistributor.pause()` | Guardian Safe, or the Timelock | Stops `distribute`. Deposits still arrive at the distributor |
| `YieldVaultFactory.pause()` | Guardian Safe, or the Timelock | Stops vault deposits, settlement proposals, profit funding and finalization of the Factory's vaults |
| `unpause()` on any of them | Timelock only | Restores the paused functions |
| `setGuardian(address)` | Timelock only | Replaces the pausing account of one contract |
| `cancel(id)` on the Timelock | GOV Safe or CAN Safe | Removes a Waiting or Ready operation before it executes |

Pausing StPROS or the Oracle does **not** pause claims. To stop payouts, pause the BridgeVault.

## Oracle anomalies

The Oracle accepts a report only within the interval, the APR allowance and the absolute cap
([PARAMETERS.md](PARAMETERS.md#oracle-and-yield-reporting)). The reporter cannot reduce `T` or mint shares to itself.
Bounded is not the same as correct: the Oracle cannot tell reward from other funds
([SLP-STAKING.md](SLP-STAKING.md#known-risk-principal-reported-as-yield)).

| Situation | Response |
|---|---|
| Unexpected or wrong report pending or likely | Pause the Oracle (Guardian). This also stops deposits and new requests; claims continue |
| Reporter compromised or wrong | Timelock: `setSlp(newReporter)`, or `setUpdateLimits(0, interval)` to disable reporting until a reviewed reporter is in place |
| `T` known to be wrong | Timelock: `setTokenAmount(WPROS, expectedOldT, newT)`. It is a compare-and-set: it reverts if `T` changed after review, and it never changes `V`. Commission shares minted by an earlier report are not reversed |
| `canUpdate` returns true but a report reverts | `canUpdate` is advisory; commission minting can still fail (for example while StPROS is paused). Investigate before retrying |

Unpause only after the cause is understood and the configuration is verified.

## BridgeVault custody recovery and FIFO resumption

This section is the custody and queue recovery procedure (referred to in the tests as LAB-OBS-01).

Outstanding withdrawal liabilities are `Q - C`, where `Q = queuedWithdrawal`
and `C = completedWithdrawal`. Reconcile this value with the sum of every current
record's pending amount and the complete history of requests and prior payments.
BridgeVault's **current available balance** is distinct from this liability and
from Oracle-reported NAV. For the canonical native-backed WPROS registration,
`BridgeVault.getBalance(asset)` measures native liquidity; donated WPROS does not
become available native liquidity until successfully unwrapped. Verify actual
registration, asset and BridgeVault identity before using either balance.

The funding frontier is `F = current BridgeVault balance + C`. For a mature record
with queued baseline `q` and pending amount `p`, the accounting release is bounded
by `min(F - q, p)` when `F > q`. This is an interval-accounting calculation, not a
reservation of available custody. `canWithdrawalAmount` is **advisory**: pause,
registration, timing, intervening claims and privileged custody recovery can prevent
payment even when the preview is positive.

Example from the local regression: Alice has [0,10), Bob [10,20), with 20 PROS
funded. Bob receives 10 first. An authorized 5-PROS recovery leaves custody 5 and
C=10, so F=15. Alice's calculated release remains 10, but the entire claim reverts
because custody is only 5. Re-funding by 4 still leaves custody 9 and still reverts;
custody of 10 pays her full 10. Rights and queue records are preserved, and the
custody check prevents overpayment. Partial liquidity need not produce a partial
claim when the currently calculated release is larger than custody. This accepted
operational limitation does not add skipping, repricing or reordering of records,
and receiver semantics remain unchanged.

Conservative recovery workflow:

1. Pause affected BridgeVault operations where appropriate and coordinate custody
   outflows; pausing StPROS/Oracle alone does not pause claims.
2. At a pinned block record Q, C, Q−C, actual native/wrapped balances, registrations,
   current BridgeVault/asset identities and every outstanding record.
3. Reconcile queue intervals, receiver bindings, maturity snapshots, previous
   out-of-order payments and all recovery outflows against complete history.
4. Restore independently reviewed liquidity. The conservative all-queue condition
   is available custody **at least Q−C** for the relevant asset liabilities, with
   no conflicting claims on the same reserve. Account for new requests and other
   registered liabilities; a single observed preview is not total solvency proof.
5. Confirm complete record sums and current balances again, and verify the relevant
   complete claim calls are fully payable, including multi-record aggregate release,
   registration, pause and native/wrapped payout requirements. Document any partial
   resumption as a separately reviewed liquidity limitation, not restored solvency.
6. Only after reconciliation and payment checks pass may authorized governance
   unpause BridgeVault and resume operations. Continue checking as claims execute.

**Replacing the BridgeVault.** If custody must move to a new BridgeVault, deploy a reviewed proxy under the shared
ProxyAdmin, owned by the Timelock, and execute one Timelock batch: `oldBridge.emergencyWithdraw(address(0), newBridge,
balance)` followed by `StPROS.setBridgeVault(newBridge)`. Before execution, check the recipient, amount, asset, order and
the identities (runtime, implementation, admin) of StPROS and both BridgeVaults against reviewed values; take the
expected old BridgeVault from the reviewed topology, not from `StPROS.bridgeVault()`. Then follow the workflow above
before claims resume.

Alert on custody below Q−C, frontier reductions after recovery/repointing, positive
previews exceeding payable custody, repeated `InsufficientBalance` or stalled mature
records, unexpected receiver/registration changes, and queue/history reconciliation
failures. External SLP/bridge solvency and future funding remain separate evidence
requirements. Neither the preview nor a local regression proves them. Keep claims
paused or report **BLOCKED** when identities, reconciliation or full release funding
are unresolved; do not modify tickets to force resumption.

## SLP layer incidents

The SLP execution contracts and the RewardsVault are administered by the SLP administrator, outside the Timelock
([SLP-STAKING.md](SLP-STAKING.md#changeable-by-governance-or-by-the-slp-administrator)). From the core contracts,
governance can:
- pause the SlpDistributor (Guardian) to stop all keeper allocations;
- remove a destination from the whitelist (Timelock: `setSlps([destination], false)`) or replace the keeper
  (`setKeeper`);
- replace or disable the Oracle reporter (see [Oracle anomalies](#oracle-anomalies)).

If principal reached the RewardsVault, it must not be reported. Pause the Oracle before the next `notifyOracle()`:
`Oracle.update` reverts while the Oracle is paused, so the balance cannot be reported. `notifyOracle()` is the
RewardsVault's only outflow ([SLP-STAKING.md](SLP-STAKING.md#known-risk-principal-reported-as-yield)); any other remedy
is in the hands of the SLP administrator, outside the Timelock. The core contracts cannot recover funds held by the SLP
contracts.

## YieldVault incidents

| Situation | Response |
|---|---|
| A round must settle at a loss | Follow [LOSS contingency planning](GOVERNANCE.md#loss-contingency-planning): paired Timelock operations, scheduled before the proposal cutoff with a reviewed margin |
| A round must be stopped before settlement | Timelock: `YieldVaultFactory.emergencyCancel(vault)`. The round goes to SETTLED at principal; a funded PROFIT proposal is refunded |
| A vault must be closed for good | Timelock: `YieldVaultFactory.retireVault(vault)`. Terminal; a funded PROFIT proposal is refunded and redemptions stay available |
| Nobody settles a round | After `settlementDeadline()`, anyone can call `forceParSettlement()` on a round that is LOCKED or has an unfunded proposal |
| Unused or stale LOSS operation | Cancel it (CAN or GOV) before the vault opens its next round ([GOVERNANCE.md](GOVERNANCE.md#loss-contingency-planning)) |

An expired scheduling window, replacement of an existing proposal, or an unused stale Ready operation needs explicit
re-review or cancellation. There is no pause-independent LOSS call and no deadline bypass.

## During a governed upgrade

These cases apply while a future governed release is in progress ([UPGRADES.md](UPGRADES.md)).

| State | Required response |
|---|---|
| Wrong codehash, immutable, canonical asset, version, delay or role state, or incomplete inventory | Stop. Investigate and obtain reviewed inputs. No forced or implementation-only upgrade and no guessed address |
| Safe transaction that schedules the operation reverts | Nothing applies; correct the reviewed inputs and sign again |
| Operation scheduled, not yet executed | Keep the scheduled operation. Before execution, verify identities, roles and the pending ID again. GOV or CAN can cancel it with the prepared unsigned cancel call |
| Timelock batch reverts | The whole batch rolls back. Diagnose, cancel, then review and schedule a new operation with the full delay |
| Gas above the reviewed cap | Stop. Splitting an atomic upgrade needs a new reviewed design |
| Only the core contracts can be verified | `assertCoreR4` is a diagnostic only. Report the release as INCOMPLETE; the full release assertion remains unmet |
| Unexplained owner, role, code, configuration or ledger change after review | Stop. Do not reset the pinned block or rebuild a pending operation to work around it |

A reviewed block behind the chain head is not a failure by itself: verify its canonical hash and reconcile the current
state. Missing historical RPC data is a hard stop. Recovery cases of the unexecuted testnet sequence are in
[UPGRADES.md](UPGRADES.md#testnet-v4-sequence-not-executed).
