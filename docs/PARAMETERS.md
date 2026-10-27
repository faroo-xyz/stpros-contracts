# Parameters

## Purpose

This is the single reference for the current V4 parameters on Pharos Mainnet: their values, units, the getter that
returns them, who can change them and what they mean. Other documents link here instead of repeating values.

- **Source of values.** Read-only `eth_call` at **block 20,815,667** (2026-10-21 07:29:17 UTC) on
  `https://rpc.pharos.xyz`. They agree with the executed upgrade record
  [`V4_UpgradeReceipt.json`](../deployments/mainnet/v4/V4_UpgradeReceipt.json). Re-read the getters before relying on a
  value: governance can change mutable parameters. The governance delay below has a separate
  2026-10-27 execution verification linked from its row.
- **"Timelock"** in *Mutable by* means a call scheduled by the GOV Safe on the TimelockController and executed after the
  Timelock delay ([GOVERNANCE.md](GOVERNANCE.md)). A **constant** can change only through a contract upgrade, which also
  goes through the Timelock.
- `1 PROS` = 10^18 wei. ppm = parts per million (1,000,000 = 100 %). bps = basis points (10,000 = 100 %).

## Oracle and yield reporting

Oracle `0x051713fd66845a13bf23baca008c5c22c27ccb58`.

| Parameter | Current value | Unit | Source (getter) | Mutable by (setter) | Notes |
|---|---|---|---|---|---|
| Expected gross APR | 10 % | — | Not on chain. Owner product decision of 2026-10-20 | — | Planning reference only. **Not a promised or guaranteed yield** |
| Maximum reporting APR | 300,000 (30 %) | ppm | `maxAprPpm()` | Timelock: `setMaxAprPpm(uint256)`, at most `MAX_APR_PPM` | Ceiling on reported growth relative to `T` and elapsed time. **Not a yield** |
| Hard APR ceiling | 500,000 (50 %) | ppm | `MAX_APR_PPM()` | Constant | Bounds `setMaxAprPpm` |
| Absolute cap per report | 80,000 PROS (`80000000000000000000000`) | wei | `maxUpdateAmount()` | Timelock: `setUpdateLimits(maxUpdateAmount, updateInterval)` | Upper bound per report; 0 disables reporting. The APR allowance below is usually lower |
| Minimum report interval | 72,000 (20 h) | seconds | `updateInterval()` | Timelock: `setUpdateLimits(...)`, at least 3,600 | A minimum. **Reports are not scheduled**; a longer gap is allowed |
| Catch-up window | 604,800 (7 days) | seconds | `MAX_CATCH_UP()` | Constant | Elapsed time counted by the APR allowance is capped at 7 days |
| Commission rate | 100,000 (10 %) | ppm | `commissionRatePpm()` | Timelock: `setCommissionConfig(account, ratePpm)`, at most 1,000,000 | Share of each accepted report, minted as stPROS. Deposits are not charged; any amount the Oracle accepts as a report is, including non-reward funds reported by mistake |
| Commission recipient (Treasury) | `0x3754fd5ec997c07ca528bb0bdcb0b4f5b93605df` | address | `commissionAccount()` | Timelock: `setCommissionConfig(...)` | |
| Reporter | `0xd0c44d3079e97ff285dd7a3e4e0246320a9487e5` (SlpPharosRewardsVault) | address | `slp()` | Timelock: `setSlp(address)` | The only account allowed to call `update` |
| Registered vToken for WPROS | StPROS proxy | address | `tokenToVToken(WPROS)`, `vTokenAddresses(StPROS)` | Timelock: `setCommissionVToken`, `setVToken` | |
| Last report time | 1,792,565,616 (2026-10-21 06:53:36 UTC) | Unix seconds | `lastUpdateAt(WPROS)` | Each accepted report | Set by the V4 migration. No yield report had been accepted since, as of block 20,815,667 |
| Paused | no | — | `paused()` | Guardian or Timelock: `pause()`; Timelock only: `unpause()` | |

**How much one report can add.** A report of `Δ` PROS is accepted only if
`Δ ≤ min(maxUpdateAmount, T × maxAprPpm × min(elapsed, 7 days) / (365 days × 10^6))`, with
`elapsed ≥ updateInterval`. With `T` ≈ 974,849 PROS at block 20,815,667:

| Elapsed since last report | Allowance (≈) |
|---|---|
| 20 h (the minimum) | 667.7 PROS |
| 7 days or more | 5,608.7 PROS |

The 80,000 PROS cap binds only when `T` is above ≈ 116.8 million PROS at a 20-hour spacing, or ≈ 13.9 million PROS
after a full 7-day gap. Below that, the APR allowance is the effective limit. The allowance restarts at every accepted
report; unused allowance beyond 7 days is not carried forward. Reports carry no accrual period, so a later report may
include earlier unreported yield within its own allowance, and the allowance may exceed the yield actually earned.

## Pool state (snapshot, block 20,815,667)

| Quantity | Value | Getter |
|---|---|---|
| `T`: pooled PROS | 974,848.994537647201066334 | `Oracle.poolInfo(WPROS).tokenAmount` |
| `V`: pooled shares | 974,848.994537647201066334 | `Oracle.poolInfo(WPROS).vTokenAmount` |
| Exchange rate | 1.000000 PROS per stPROS | `StPROS.convertToAssets(1e18)` |
| stPROS total supply | 974,848.994537647201066334 | `StPROS.totalSupply()` |
| Outstanding redemptions (Q − C) | 1,130.594039586951392499 PROS | `queuedWithdrawal() − completedWithdrawal()` |
| BridgeVault liquidity | 1,130.584039586951392499 PROS | `BridgeVault.getBalance(WPROS)` |
| SlpDistributor balance | 974,849.004537647201066334 PROS | native balance |

These are state, not parameters. They change with every deposit, request, claim, report and allocation.

## Redemption queue

StPROS `0x6b0a44c64190279f7034b77c13a566e914fe5ec4`.

| Parameter | Current value | Unit | Source (getter) | Mutable by (setter) | Notes |
|---|---|---|---|---|---|
| Waiting period for new requests | 86,400 (1 day) | seconds | `unbondingPeriod()` | Timelock: `setUnbondingPeriod(uint256)`, within the bounds below | Copied into each new record. **A minimum, not a payout date**: a claim also needs BridgeVault liquidity. Records created before the V4 upgrade keep their 604,800-second (7-day) period |
| Minimum waiting period | 86,400 (1 day) | seconds | `MIN_UNBONDING()` | Constant | Lower bound for the setter |
| Maximum waiting period | 2,592,000 (30 days) | seconds | `MAX_UNBONDING()` | Constant | Upper bound for the setter |
| Outstanding records per owner | 5 | records | `maxWithdrawCount()` | Timelock: `setMaxWithdrawCount(uint256)`, 1 to 32 | Another request reverts with `ExceedMaxWithdrawCount` |
| Deposit destination | SlpDistributor `0xf772b375f93650f798bf724ce7a9a45c9e7a579f` | address | `slp()` | Timelock: `setSlp(address)` | |
| Redemption vault | BridgeVault `0xf1df1cb080256f48e8702c24149136e4a6dec25e` | address | `bridgeVault()` | Timelock: `setBridgeVault(address)` | |
| Oracle | `0x051713fd66845a13bf23baca008c5c22c27ccb58` | address | `oracle()` | No setter; upgrade only | |
| Paused | no | — | `paused()` | Guardian or Timelock / Timelock | Blocks deposits and new requests, not claims |

The Pharos network unbonding period, which applies to staked PROS, is a different value set by the network
([SLP-STAKING.md](SLP-STAKING.md#principal-flow)). The stPROS waiting period does not depend on it, but the liquidity
for claims can.

## BridgeVault and SlpDistributor

| Parameter | Current value | Source (getter) | Mutable by (setter) | Notes |
|---|---|---|---|---|
| BridgeVault wrapped-native asset | WPROS `0x52c48d4213107b20bc583832b0d951fb9ca8f0b0` | `BridgeVault.weth()` | Timelock: `setVToken(vToken, enabled, isWeth)` | Liquidity is held as native PROS |
| BridgeVault registered vToken | StPROS (one registration) | `vTokenByAsset(WPROS)`, `vTokenAddressCount()` | Timelock: `setVToken(...)` | Unregistering StPROS stops claims |
| BridgeVault paused | no | `paused()` | Guardian or Timelock / Timelock | Blocks claim payouts |
| SlpDistributor keeper | `0x8736e68f37c1156e09445ac2d395a139e58fcab4` | `keeper()` | Timelock: `setKeeper(address)` | |
| SlpDistributor whitelist | 4 entries (order as returned) | `getSlps()`, `slpCount()` | Timelock: `setSlps(address[], bool)` | Purpose of each entry: [SLP-STAKING.md](SLP-STAKING.md#a-whitelisted-address-is-not-a-principal-destination) |
| SlpDistributor paused | no | `paused()` | Guardian or Timelock / Timelock | Blocks `distribute` only |

## YieldVault

### Factory

YieldVaultFactory `0xc9fb7dc52b0fb92c417d481442d2641637483881`.

| Parameter | Current value | Unit | Source (getter) | Mutable by (setter) | Notes |
|---|---|---|---|---|---|
| Approved performance fee for new rounds | 0 | bps | `approvedPerformanceFeeBps()` | Timelock: `setApprovedPerformanceFeeBps(uint16)` | Every new round must use exactly this fee. Rounds already open keep their stored fee |
| Maximum performance fee | 3,000 (30 %) | bps | `MAX_PERFORMANCE_FEE_BPS()` | Constant | |
| Vault creation closed | no | — | `creationClosed()` | Timelock: `closeCreation()` (permanent) | |
| Paused | no | — | `paused()` | Guardian or Timelock / Timelock | Blocks vault deposits, settlement proposals, profit funding and finalization |
| Official vaults | 1: vault 001 | — | `getAllProxies()`, `isFactoryVault(address)` | Timelock: `createYieldVault(...)` | |

### Round bounds (constants of every vault)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_OPEN_WINDOW` / `MAX_OPEN_WINDOW` | 1 day / 90 days | Subscription window length |
| `MAX_OPEN_EXTENSION` | 30 days | Largest single extension of an open window |
| `MAX_OPEN_DELAY` | 30 days | Latest scheduled start of a round after `openNextRound` |
| `MIN_LOCK_DURATION` / `MAX_LOCK_DURATION` | 1 second / 365 days | Lock period length |
| `MIN_SETTLE_TIMELOCK` / `MAX_SETTLE_TIMELOCK` | 1 second / 23 days | Delay between a settlement proposal and finalization |
| `SETTLEMENT_GRACE` | 30 days | `settlementDeadline() = openDeadline + lockDuration + 30 days` |
| `MIN_PROPOSAL_WINDOW` | 7 days | Reserved before the deadline: `MAX_SETTLE_TIMELOCK = 30 − 7 days` |
| `MAX_LOSS_BPS` | 5,000 (50 %) | Largest loss a round may allow |
| `MAX_PERFORMANCE_FEE_BPS` | 3,000 (30 %) | Largest performance fee a round may use |
| Share decimals | 24 | stPROS decimals (18) plus a decimals offset of 6 |

### Vault 001: current round

Vault `0x36f3d19dda7ed1428e3014ae6f2a75d70393b7e6` ("Faroo RWA Hybrid Vault 001", `FRHV001`). The operator sets these
values for each round with `openNextRound`. They apply to round 8 only.

| Parameter | Round 8 value | Getter |
|---|---|---|
| Phase | SETTLED (3) | `phase()` |
| Open window | 257,400 s (71.5 h) | `openWindow()` |
| Lock duration | 604,800 s (7 days) | `lockDuration()` |
| Settlement timelock window | 600 s | `settleTimelockWindow()` |
| Round cap | 2,000,000 stPROS | `roundCap()` |
| Per-address cap | 1,000,000 stPROS | `perAddressCap()` |
| Minimum subscription | 1 stPROS | `minSubscription()` |
| Performance fee | 0 bps | `performanceFeeBps()` |
| Maximum loss | 0 bps | `maxLossBps()` |
| Managed assets | 353,216.308301068113383132 stPROS | `totalManagedAssets()` |
| Counterparty and fee recipient | Treasury `0x3754fd5ec997c07ca528bb0bdcb0b4f5b93605df` | `counterparty()`, `feeRecipient()` |

## Governance

| Parameter | Current value | Source (getter) | Mutable by | Notes |
|---|---|---|---|---|
| Timelock minimum delay | 86,400 seconds (24 h) | `TimelockController.getMinDelay()` | The Timelock itself: `updateDelay(uint256)`, scheduled by GOV and executed after the current delay | Raised from 600 seconds by the separately executed 2026-10-27 Timelock operation. See the [execution record](../governance/mainnet/20261027-delay-1day/README.md) and [GOVERNANCE.md](GOVERNANCE.md#timelock-delay) |
| Guardian of all core contracts | `0xa046988889871c6c7abc69e63cccf9adb3e57ef5` | `guardian()` on each contract | Timelock: `setGuardian(address)` per contract | Pause only |
| Owner of all core contracts and the ProxyAdmin | TimelockController | `owner()` | Two-step `transferOwnership` / `acceptOwnership`, through the Timelock | `renounceOwnership` is disabled on the core contracts |

## Values that changed with V4

The product policy approved before the mainnet parameters were final, archived in the research repository, stated
different values for these items. They are superseded on mainnet:

| Item | Earlier design value | Mainnet V4 value |
|---|---|---|
| Waiting period for new requests | 7 days ("unchanged") | 1 day (set in the upgrade transaction) |
| Operational APR rule | 2 × expected gross APR | 3 × expected gross APR: 30 % for 10 % expected |
| Report interval | at least 1 hour (contract minimum) | 20 hours |
| Timelock delay | 48 hours in the earliest plan | 600 seconds during configuration; raised to 86,400 seconds by a separate operation on 2026-10-27 |
| Historical test factory and its vault | Retired in the release | Left out of V4 by owner decision; not upgraded and not retired ([GOVERNANCE.md](GOVERNANCE.md#out-of-scope-legacy-contracts)) |
