# R4 implementation draft — B1 + B2

Status: **Oracle V3 technical migration implemented; awaiting independent review, not a release specification**. Target: frozen research architecture R1-final + A1. The research reports under `development-history/B1/`, `B2/` and `B1-B2-INTEGRATED-REVIEW.md` record implementation evidence and acceptance gaps. No B1/B2 commit or deployment is authorized by this draft.

## Authority and migration

The existing transparent proxies and v4 ProxyAdmin remain. `MigrationGuard` checks the ERC-1967 admin slot, then the previous initialized version, before reinitialization to version 4. Fresh initializers are constructor-only. YieldVault beacon proxies remain at initializer version 1.

`GuardedOwnableUpgradeable` preserves the OZ Ownable and Pausable namespaces, adds OZ Ownable2Step, and uses ERC-7201 `faroo.storage.Guardian` at `0x8a182d357ef2af68f85f69b7302e5a52adb7e215ee455b89eee471e22cd8a700`. Guardian or owner may pause; only owner may unpause or rotate the guardian. Renunciation reverts. Initial and migrated governance must be a contract exposing positive `getMinDelay()`. Exact Timelock code identity and roles remain release checks; this interface check alone does not prove trusted governance.

V1 migration requires the zero-pool/zero-sink fingerprint, canonical StPROS registration, compatible BridgeVault, `R == Q - C`, `W >= R`, `E >= R`, and `W - R >= S - E`. It unwraps exactly R into the bridge, forwards the remaining wrapped balance to the sink, burns the entire escrow including donated shares, and seeds pool T = V = remaining supply. Forced native is left inert. Queue records, Q and C are preserved. Deposits/redemptions and claims share the transient reentrancy guard.

**Approved Oracle V3 correction:** `migrateFromV3(address token, uint256 aprPpm, address guardian, address timelock)` uses `onlyProxyAdmin` → `onlyFromVersion(3)` → `reinitializer(4)`. The supplied token must have a nonzero, code-bearing, registered canonical vToken whose historical `asset()` matches it. The owner confirmed a single WPROS/stPROS pool; the recorded inventory must verify that pair because mappings cannot prove absence of additional keys. No addresses are hardcoded and no multi-pool registry is introduced.

Migration preserves the existing non-1:1 T/V, mappings, reporter, absolute cap, commission settings, balances/supply/queues and pause state. It sets `lastUpdateAt[token] = block.timestamp`, normalizes `updateInterval = max(oldInterval, 1 hours)`, validates the explicitly supplied APR against the existing 500,000-ppm ceiling, and migrates governance. The one-hour floor and four-argument ABI are technically approved; no upper interval cap is imposed. Zero APR disables positive reporting until governance configures it. Operational nonzero APR and commission economics remain separate approval gates. The sequence remains Oracle first using historical StPROS.asset(), then StPROS, BridgeVault and Distributor.

Fresh StPROS initialization takes asset, owner, name, symbol, oracle, sink, bridge and guardian atomically, since mutable `setOracle` was removed. Oracle/Bridge/Distributor/Factory initializers take guardian configuration; Factory also takes the Timelock pointer. Fresh core configuration is exercised on v4 proxies in the current gas/unit suite. The release bootstrap ordering must account for BridgeVault's call to `StPROS.asset()` and remains a B3 deployment-tooling check. Existing production proxies use migration entry points.

## Claims and accounting

`withdrawComplete()` / `(uint256)` / `(address)` / `(address,uint256)` pay WPROS. `withdrawCompleteToPROS()` / `(uint256)` pay native, falling back to WPROS if the recipient rejects native. Claims are unaffected by StPROS or Oracle pause; BridgeVault pause applies to custody payouts.

A new request to its owner stores receiver zero (unbound). Unbound records, including all V1 records, honor the claim-time receiver. Other receivers are bound at request time. `maxRecords` counts fully consumed records; zero imposes no additional limit. Queue capacity is configurable within 1–32; historical records retain their waiting-period snapshots. The current draft preserves the legacy allowed unbonding interval 0–30 days pending D7.

Oracle pool mutation requires the canonical registered vToken. `initializePool` accepts only an empty pool and matching supply. Timelocked `setTokenAmount` is compare-and-set and never rewrites share accounting. Zero remaining supply zeroes token accounting. Reporter updates require nonzero supply and satisfy both absolute and time-proportional APR bounds. Commission uses the D6 exact-formula draft `floor(f * V / (T + delta - f))`. `canUpdate` is advisory: it checks local bounds and paused commission recipients but cannot guarantee external execution.

BridgeVault rejects zero-asset registrations; permissionless `unwrapWrapped` restores donated wrapped liquidity. Emergency withdrawal remains a timelocked recovery power that can reduce queued-claim liquidity. Distributor payouts require an unpaused keeper and contract-code whitelist entries; external destination safety is still an assumption.

## YieldVault and Factory

OPS manages bounded rounds and PROFIT proposals. New LOSS and replacement/cancellation of an existing LOSS require the factory Timelock, independently of roles. Timelock may cancel PROFIT for recovery. Finalization is permissionless and factory-pausable.

A1 deadline `D = openDeadline + lockDuration + SETTLEMENT_GRACE` is derived, with no new timestamp storage. Every proposal/replacement requires `now + settleTimelockWindow <= D`. At D, unsynchronized OPEN is first synchronized; LOCKED or an unfunded proposal can force par settlement without transfers and regardless of factory pause. Funded PROFIT (including zero) and LOSS require `finalize`, hence governance unpause if paused. Cancellation refunds the original funder.

Creation pins the admin to `factory.timelock()` and requires a distinct nonzero operator. Vault initialization writes authority dependencies before guarded role grants (A1.5). Admin grants may only target the current Timelock. Canonical factory caller enforcement does not prevent lookalikes using attacker-owned factories: integrations still need `isFactoryVault` plus `vault.factory()` checks.

`retireVault` refunds any funded proposal and permanently sets the vault to SETTLED. Even stale roles cannot reopen it. Redemption stays available and never reads factory pause. `closeCreation` is irreversible except by upgrade. Existing rounds retain their stored terms; limits apply to new rounds and cumulative open extensions. Scheduled OPEN rounds permit redemption before `openedAt`, while deposits wait.

Factory initialization uses a separately deployed Vault implementation supplied to the Factory implementation constructor and stored as an immutable. The constructor rejects an address without code. This keeps each implementation deployment separate and avoids a nested creation costing over 5.1M gas. This removes embedded Vault creation code from Factory runtime to meet EIP-170. Each fresh factory proxy still creates and owns its own beacon. Existing factories/beacons are never replaced by this mechanism; the migration only appends governance fields, followed by the explicit beacon upgrade.

## Product gates and verification limits

The Oracle V3 ABI correction and one-hour minimum update interval are technically approved. Migration uses the existing 500,000-ppm APR ceiling and seven-day catch-up bound; the operational APR is not selected. Other draft D8/D9 constants remain recommendations: OPEN 1–90 days; lock at most 365 days; scheduled opening at most 30 days away; LOSS cap 5,000 bps; grace 30 days; proposal window 7 days; maximum settlement window 23 days. Those remaining round parameters are **not approved economic policy**. D17 retains the old 10,000-bps fee cap rather than silently adopting 3,000 bps. Commission rate/account, APR value and reporter/sink are explicit configuration. D6 exact commission, D7 floor, D9/D18 behavior and D19 counterparty terms remain acceptance gates.

Linear additions are restricted to Oracle slot 9 (`maxAprPpm`), Factory slot 4 (`timelock`, packed `creationClosed`) and YieldVault slot 22 (`retired`). Deprecated VToken slots stay intact. ABI consumers must update initialization/creation tuples, new roles and all intentional removals. Tasks for R4 creation, LOSS and retirement emit governance calldata; B3 upgrade/deployment tooling is not implemented here.

Targeted R4 regressions and structural comparisons are separate evidence, not a full-suite PASS. All 106 original core/vault unit cases have been ported without reducing their count. Historical migration assertions use recorded fixtures; unsafe legacy setter/initializer assertions now check rejection and the new authorized paths. B0 snapshots are unchanged. Full CI, live forks, formal verification, the full Attack Lab and release rehearsal are NOT RUN in this batch. One overly broad local test glob accidentally included B0 legacy/gas cases; this execution-scope mistake is recorded in the integrated report, and subsequent runs use exact contract filters.
