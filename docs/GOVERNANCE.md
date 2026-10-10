# Governance and release authority

The research repository's frozen audit roadmap and R1-final + A1 amendments are authoritative. This development tooling does not authorize deployment. Development occurs on `main`; all changes remain uncommitted until requested.

| Authority | Approved responsibility |
|---|---|
| GOV | Sole Timelock proposer and executor; mainnet Safe 3/5 after signer rotation |
| CAN | Independent Safe, threshold at least 3; canceller, never proposer/executor |
| GRD | Independent fast-response guardian Safe; pause only, cannot unpause or upgrade |
| OPS | Vault operating role; may share the same reviewed GRD Safe; never GOV, CAN, Timelock or counterparty/fee recipient |
| Timelock | Owns shared recorded OZ v4 ProxyAdmin and all core/Factory proxies; LOSS authority |
| Factory | Owns its existing beacon; only Timelock can upgrade it |
| Treasury | Reviewed Safe receiving mainnet future-yield commission |
| Deployer | No retained Timelock role or Safe signer authority after setup |

Mainnet delay is exactly 172,800 seconds; testnet is exactly 300 seconds. Timelock is its own sole default admin, GOV has proposer/executor/canceller, CAN has canceller, and there is no open executor. GOV/CAN/GRD signer sets must be disjoint. OPS must be address- and signer-control-independent of GOV/CAN; OPS==GRD is allowed only with the same reviewed Safe configuration. Distinct Safe addresses do not establish independent controllers. Contract/delegated signers with unproven control evidence fail with `INCOMPLETE_SAFE_SIGNER_CONTROL`; the current verifier does not guess their controlling keys. The release config supplies actual Safe identities, singleton hashes, owners, thresholds, nonce, empty module lists and zero guard; read-only preflight checks them. Testnet designated multisig alternatives require a separate reviewed adapter; this implementation deliberately supports the stricter Safe profile for release output. Simulations may use labelled synthetic actors.

`ReleaseAssertions` has no storage, privileged function, delegatecall, external state-changing call or administrator. Every function is view/pure. It checks reviewed expectations by STATICCALL. Proxy admin/implementation checks go through the **recorded OZ v4 ProxyAdmin** public `getProxyAdmin`/`getProxyImplementation` wrappers. It never pretends its own SLOAD can inspect another contract. Initializer versions are checked with pinned RPC storage reads at the OZ namespaced initializer slot.

A role-array hash binds calldata; it does **not** establish inventory completeness. The off-chain gate rescans every RoleGranted/RoleRevoked event for all role IDs, from a block before code existed, in contiguous windows of at most 1,000 blocks; unexpected RoleAdminChanged requires review. It compares the replay to actual `hasRole` state. Before execution and after migration, every live role outside owner-approved residual retirement targets must exactly match the frozen expected set, including formerly unknown actors. C/D and E2-unavailable vault roles remain completely inventoried but informational: they are explicitly accepted exposure before U and inert only after verified retirement. An RPC provider capable of omitting historical data remains a trust assumption; independently reviewed complete history and release rehearsal are mandatory.

The product policy is unchanged: mainnet launch commission 100,000 ppm on future yield, Treasury Safe recipient; approved APR is twice owner-selected expected gross APR and at most 500,000 ppm; update interval at least one hour. Factory launch performance fee is zero, future changes are Timelock-only and capped at 3,000 bps. Historical round fees and their 10,000 denominator remain unchanged. New unbonding settings have a one-day floor and thirty-day ceiling; migrations preserve the historical mainnet seven-day and testnet zero settings and individual withdrawal records.

Legacy direct Oracle/StPROS upgrades, beacon upgrades, ownership-handoff and deprecated initializer/setter Tasks are no longer registered. The main `deploy` override and nine deploy stubs still refuse before runtime access. Release builders never produce `changeProxyAdmin`, `renounceOwnership`, an implementation-only core upgrade, or an unguarded initializer. Other ordinary administrative tasks are not a release workflow; only reviewed Timelock calldata is authorized for an R4 release.

The RPC checks distinguish the approved historical block from the current chain head. They independently bind every selected historical target (including Oracle and testnet BridgeVault/SlpDistributor) to the chain-specific registry; mainnet fresh BridgeVault/SlpDistributor require separate reviewed deployment records. Standalone release builders enforce that registry binding too. Current proxy/Beacon code and upgrade/admin history are checked in addition to getters. No atomic assertion was moved off-chain during review closure; the Solidity refactor preserves its public ABI and only consolidates repeated Vault asset reads.

## Approved governance closure (2026-10-10)

The conversation-approved GC-01 A / GC-02 C / GC-03 B amendment narrows mainnet
governed-Vault handoff to effective SETTLED throughout reviewed input, fresh
pre-schedule, H, execution checkpoint and final U. Residual/testnet rules and
Vault economics remain unchanged. Timelock coverage is explicitly all
**action-bearing** operations (one or more target calls) plus referenced
predecessor states; global event-silent empty-ID discovery is not claimed.
Pending includes Waiting/Ready actions even with blocked predecessors. Pre set
empty, H exactly frozen U with zero predecessor, post set empty and U Done.
The source proof, complete authenticated archive/journal and pinned OZ runtime
are prerequisites; a parser alone does not prove whole-operation completeness.

## LOSS contingency planning (LAB-F-01, approved 2026-10-10)

A Guardian pause can make a scheduled LOSS miss the fixed proposal cutoff and
indirectly change who bears the round outcome. GRD cannot directly transfer custody
or change NAV. This is an operational mitigation with residual timing/economic
risk, not a contract-level fix. The effect can reach the approved round loss cap
(50% maximum); it is not bounded solely by a short outage duration.

For one reviewed LOSS amount and round, prepare and independently schedule both:

| Path | Exact ordered target calls | Required execution state |
|---|---|---|
| A | Vault `proposeSettlement(LOSS, approvedAmount, address(0))` | Factory unpaused |
| B | Factory `setGuardian(reviewedReplacementGuardian)`; Factory `unpause()`; same Vault LOSS call | Factory paused; one atomic Timelock batch |

B rolls back in full if any call fails, including `unpause()` when already
unpaused. A fails while paused. Neither path works in both pause states. Replacing
GRD is effective only when B succeeds; approve the exact replacement independently.
No tooling may invent a Guardian, Safe, signer approval, amount or new round terms.

Compute `D = openDeadline + lockDuration + SETTLEMENT_GRACE` and
`proposalCutoff = D - settleTimelockWindow`. Schedule **both** operations no later
than `proposalCutoff - actualMinDelay - reviewedMargin`. The margin is a positive,
explicitly reviewed operational input, not a release constant. Ensure maturity
also leaves the margin before cutoff. Exact cutoff is accepted by the contract;
operators must retain the reviewed margin rather than plan for last-second
inclusion. If neither reviewed path can safely execute before cutoff, report
**BLOCKED** and independently re-review; never extend D or silently change amount,
Guardian, calldata, salts or operation IDs. OPS pre-emption, new pauses, unavailable
signers, transaction inclusion and other state changes remain risks.

`yield-vault:loss-contingency` is a separate unsigned/read-only task. The existing
`yield-vault:loss-calldata` retains its single-call JSON interface. The new task
checks current chain, approved runtimes, Factory proxy admin/implementation,
Vault beacon/implementation/asset, Factory registry membership and Vault back-link,
owners, authority getters, round terms, delay, backing and selected operation IDs.
Release mode binds historical canonical Factory/asset/Beacon/ProxyAdmin identities
to the existing chain registry and the Timelock to the GC-03 pinned OZ runtime.
Full runtime hashes (including Factory immutable binding) must be independently
approved from known source/artifacts, never copied from unchecked observations.
The pure builder is not a chain attestation; release-oriented output must use the
observing task. Fresh read-only state is evidence at a pinned block, not a promise
that it remains unchanged through scheduling or execution.

The task supports new paired plans for effective LOCKED, non-retired rounds only;
it refuses existing proposals and selected IDs already scheduled or Done.
Existing proposal replacement, new unregistered Factory deployments, and arbitrary
proxy topologies need separate review. All artifacts remain `deployable: false`:
actual Safe identities, signer/control independence, nonces and execution approvals
are external requirements. Synthetic local identities are simulation-only.

After the selected path executes, **CAN may cancel the unused pending operation;
GOV retains its approved canceller capability**. Verify the selected ID is Done,
the unused ID is Unset after cancellation, and the full final operation inventory
matches the actual execution and cancellation receipts. The unused operation may
still replace a proposal or act on a later round: LOSS calldata has no round-index
argument. A currently reverting Ready call is not closed. Waiting and Ready both
remain pending, including blocked dependencies. Keep the original immutable bundle;
a changed payload means a new operation requiring new review and the full delay.
GC-03 action-bearing history and reconciliation remain authoritative. This task's
two-ID check neither enumerates all operations nor satisfies GC-03 by itself. Paired
LOSS operations do not bypass the existing H/U release inventory constraints.

Monitor each intended/scheduled LOSS for cutoff, latest scheduling time, actual
delay, Waiting/Ready/Done/cancelled status, Factory pause and Guardian identity.
Escalate before the paired scheduling window closes, immediately on a pause near
cutoff, on a missed cancellation, or on round/amount/role/owner/code/asset drift.
Unexpected history, a changed Safe/Guardian approval, insufficient backing, revoked
authority, a stale/reorged observation or failure of either execution simulation
requires independent re-review. Recheck runtime conditions immediately before
execution, including finalization's separate pause/window dependency. A Ready
operation alone is insufficient. No command or runbook here authorizes a mainnet
or testnet transaction.

### LOSS plan input and read-only usage

Run only in the allowlisted export through `tools/verify/run.sh`:

```bash
tools/verify/run.sh "$dest" node node_modules/hardhat/dist/src/cli.js yield-vault:loss-contingency --plan /absolute/path/to/reviewed-loss-plan.json --review-hash "$REVIEWED_PLAN_SHA256" --rpc "$PUBLIC_READ_ONLY_RPC"
```

The operator must independently supply `REVIEWED_PLAN_SHA256` and
`PUBLIC_READ_ONLY_RPC`. This example does not authorize execution of any
governance transaction.

No wallet client, signature or transaction is acquired. Missing proof inputs are
blocked; the owner approval for developing this tool is not a release plan approval.
The JSON schema is `faroo-loss-contingency-v1`:

| Input fields | Reviewed meaning |
|---|---|
| `mode`, `chainId` | `simulation` or `release`; integer chain ID (release: 1672/688689) |
| `vault`, `factory`, `timelock`, `asset`, `beacon`, `factoryImplementation`, `vaultImplementation`, `proxyAdmin` | Exact lowercase nonzero approved contract identities |
| `guardian`, `replacementGuardian`, `gov`, `can`, `operator`, `counterparty`, `feeRecipient` | Exact reviewed actors; replacement differs from current GRD and excluded authorities/recipients |
| `runtimeHashes` | Map keyed by the eight contract identity names above, full runtime Keccak-256 for each |
| `round` | Exact decimal-string `roundIndex`, `openedAt`, `openWindow`, `openDeadline`, `lockDuration`, `settleTimelockWindow`, `roundCap`, `perAddressCap`, `minSubscription`, `performanceFeeBps`, `maxLossBps` |
| `approvedLossAmount`, `minDelay` | Nonnegative decimal strings in raw asset units / seconds; observed delay must match |
| `reviewedAt`, `expiresAt`, `reviewReference` | Independently approved Unix-second strings and evidence reference; review expires within one hour |
| `marginSeconds`, `marginReviewReference` | Positive decimal seconds and independent operational-margin decision |
| `predecessor`, `saltA`, `saltB` | Zero bytes32 predecessor; distinct reviewed bytes32 salts |
| optional `frozenOperationIds` | Exact `{ "A": "0x…", "B": "0x…" }`; any reconstructed ID mismatch blocks, never rewrites |

Fresh heads are at most 120 seconds old with at most 30 seconds future clock skew;
scans must finish within a 120-second head advance and recheck the canonical hash.
Those are observation freshness limits, not the independently chosen scheduling
margin. Fresh observations never renew an expired plan or existing operation.
