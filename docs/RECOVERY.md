# Handoff failure and recovery

No recovery below is automatic. It requires fresh read-only identity checks and reviewed unsigned calldata; no task signs or broadcasts. Never change a proxy admin, renounce ownership or bypass guarded migration to recover. Do not modify a pending operation's payload under the same operation ID.

| State | Required response |
|---|---|
| Wrong codehash, immutable, canonical asset, version, delay, Safe state or incomplete inventory | Stop; investigate and obtain reviewed inputs. No force, implementation-only upgrade or guessed address |
| P-1 partly completed | Fresh snapshot/inventory; skip already neutralized steps. If roles remain but the cleanup admin is absent, stop |
| A: both P-1 actors available | Cancel test round, revoke all roles, renounce admin last, transfer Factory_test to TL |
| B: Factory_test owner unavailable | Explicit D10 owner sign-off; admin cleanup only in P-1; H item 0 migrates Factory_test to TL; U omits duplicate migration and retires legacy locked vault |
| C: test-vault admin unavailable | Explicit D10 owner sign-off; cancel round and transfer factory; notify holders they may redeem; stale admin exposure lasts until U retirement |
| D: both unavailable | Explicit D10 owner sign-off; no P-1 transaction; B-style H item 0; stale test-vault authority remains until U retirement |
| H reverts | Atomic MultiSend leaves no partial H state; correct reviewed inputs and re-sign |
| H completed, U pending | Reuse frozen operation; execution checkpoint verifies identities, all roles and pending ID. CAN/GOV may cancel using the emitted unsigned cancel call |
| U reverts | Entire U rolls back, including Oracle if StPROS fails. Diagnose, cancel, then review and schedule a new operation with the full delay |
| T-1 partially processed | Builder skips only vaults whose complete current roles prove handoff finished; remaining vaults are processed by E2 |
| E2 unavailable on testnet | Explicit recovery approval; all 15 vaults RETIRED; preserve/report inert stale roles. Factory owner is forced to TL in U_t; recovery handoff assertion makes this distinction visible |
| Approved T-3 incomplete after PA handoff | Use explicitly reviewed `testnetRecovery.incompleteT3` with owner approval; the recovery assertion verifies the listed remaining legacy owners, current config/history and PA→TL. U_t forces core ownership to TL. E1 availability is still required for T-4 |
| T-2/T-3 already completed | Builder omits completed owner transfers using fresh state; T-4 is still the last E1 step |
| E1 unavailable before T-4 | BLOCKED. An offline plan is not proof of signer availability; do not silently replace E1 |
| Gas exceeds binding cap | Stop. FS-6 real-fork H and U cap is 2.2M each unless a higher cap is independently confirmed. A split needs a new approved amendment |
| Core-only contingency | `R4-CORE-ONLY — INCOMPLETE`; full R4 remains unmet. This batch does not generate U1/U2 |

A testnet designated-multisig alternative without supported Safe introspection is explicitly unsupported by this release adapter and needs review before emitting release-ready artifacts. Missing actors, Safe owners/nonces, APR, reporting cap, Treasury, keeper or whitelist are external release gates, not values for the tool to invent.

The preliminary local smoke measurements exclude real Safe overhead, historical inventory size and real Pharos execution conditions. They cannot close FS-6. Batch 04 owns the full FS-1…FS-7 real-fork sequence, all recovery cases, exact execution-frame preservation and binding gas gates. Batch 01 stops for independent review; it does not start Batch 02 or authorize release.

A reviewed pin behind the head is not itself a failure. Verify its canonical hash and independently reconcile current state. Pre-schedule evidence expires after one hour; slow scans and stale heads fail after the documented 120-second limit. After H, keep the original operation bundle and supply separately reviewed fresh execution/fork evidence and current GOV/CAN nonces. Do not reset the pin or rebuild a pending operation to work around an unexplained owner/role/code/configuration/ledger change. Missing historical RPC data is a hard stop. Postcheck accepts consumed nonces but never a changed Safe authority configuration.

### Residual-state evidence at release checkpoints

The residual recovery exception is limited to explicitly approved C/D and
E2-unavailable retirement targets. The historical anchor, complete role/holder
history, physical code, proxy/beacon identity and canonical factory/asset wiring
remain mandatory. Current backing also includes refundable funded PROFIT.

The checker reconciles round lifecycle events and settlement asset deltas.
For a new legacy round it binds all round parameters to the authenticated
`openNextRound` transaction, then checks the full terms tuple against current
views. A terms change without sufficient evidence is rejected. The present
transaction proof supports direct calls to the residual Vault; internal Safe
calls and legacy debug setters lacking complete events require additional
independently reviewed evidence and are not general drift exceptions. Keep
GOVERNED vault round/terms checks strict. Final retirement and creation closure
requirements remain unchanged.

Testnet Oracle replay checks share mint/burn events, PoolUpdated post-state and
Deposit/Withdraw events in transaction order. It resets arithmetic NAV dust only
when the independently replayed pool shares reach zero; otherwise all remaining
NAV must match. Commission events and share issuance are checked separately.


### Approved closure boundaries (2026-10-10)

Mainnet GOVERNED release handoff requires SETTLED at every gate; do not force a
business settlement or weaken phase checks to make a release pass. OPS must be
independent of GOV/CAN addresses and reviewed signer/control sets; OPS==reviewed
GRD remains allowed. Unproven contract signer control stops release validation.

Safe-forwarded residual openNextRound remains `INCOMPLETE_SAFE_RECOVERY_EVIDENCE`
until an actual reviewed historical Safe implementation/authority/nonce and
canonical successful transaction/receipt/inner-call proof is available. No real
Safe proof was supplied with this implementation package; the local forwarding
model is a rejection-boundary test only. Direct calls continue to work. Unknown
Safe versions, MultiSend, delegatecalls, modules/nested forwarders and unexplained
legacy debug setters remain unsupported. Required future evidence is the actual
proxy/singleton/runtime, owner/threshold authorization, nonce/hash, CALL target,
value/calldata, successful Safe execution, matching Vault events and full
round/economic/backing reconciliation. No self-approved bypass is accepted.

Timelock recovery inspects all pending action-bearing operations and referenced
predecessor states, including blocked dependencies. Empty completion or funding
does not exempt a nonempty successor from inventory. Full empty-ID history is
not claimed. Preserve the frozen U identity; cancellation and a new reviewed
schedule are separate governed operations, not edits to an existing operation.
