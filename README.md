# Faroo stPROS

stPROS is a liquid staking token for the Pharos network.

- Users deposit native **PROS** or wrapped **WPROS** and receive **stPROS**, an ERC-20 / ERC-4626 share token.
- Each stPROS share is a claim on a pro-rata part of the PROS accounted for by the protocol.
- Deposited PROS is allocated to Pharos staking through the SLP (staking liquidity provider) layer.
- Staking rewards are reported to an on-chain Oracle. Each accepted report raises the PROS value of every share.
- Redemption is a queued request. Users claim PROS or WPROS once the waiting period has elapsed **and** redemption
  liquidity is available.
- stPROS is a standard token. Other DeFi protocols can hold it, transfer it and build on it.

stPROS does not guarantee principal, a fixed return, an APR or a redemption date. The share value moves only with the
yield reports the Oracle accepts, within on-chain limits.

## Key features

| Feature | What it means |
|---|---|
| ERC-20 / ERC-4626 | stPROS follows ERC-4626 with WPROS as the asset. ERC-4626 `withdraw` / `redeem` **create a redemption request**; they do not pay out immediately |
| Native PROS deposits | `depositWithPROS()` takes PROS as `msg.value` and mints stPROS to the caller |
| WPROS deposits | `deposit` / `mint` take WPROS after an ERC-20 approval |
| Queue-based redemptions | Each request burns its shares, fixes its PROS amount and joins a global first-in, first-out queue. A request becomes claimable after its waiting period, once redemption liquidity covers its queue position |
| Oracle share accounting | The Oracle tracks pooled PROS (`T`) and stPROS supply (`V`). The exchange rate is `T / V` |
| SLP capital allocation | Deposits go to the SlpDistributor. A keeper allocates them only to whitelisted destinations: the staking contracts and the redemption vault |
| Yield reporting and commission | One authorized reporter adds staking rewards to `T`, bounded by an APR ceiling, an absolute cap and a minimum interval. The protocol commission is charged on each accepted report and minted as new stPROS shares to the Treasury |
| Timelock governance | Every core contract and the proxy admin are owned by an OpenZeppelin TimelockController. A separate guardian can pause but not unpause |
| YieldVault | A separate fixed-term vault product that accepts stPROS as its asset. It is not an stPROS deposit route |

## Architecture overview

Solid arrows move assets. Dotted arrows are calls that change accounting or read state without moving assets.

```mermaid
flowchart LR
    U["User / integrator"]
    ST["StPROS<br/>(ERC-4626 shares)"]
    OR["Oracle<br/>(T / V accounting)"]
    SD["SlpDistributor"]
    SP["SlpPharosProxy"]
    PS["Pharos Staking<br/>(system contract)"]
    RV["SlpPharosRewardsVault<br/>(Oracle reporter)"]
    BV["BridgeVault<br/>(redemption liquidity)"]
    YV["YieldVault<br/>(fixed-term product)"]

    U -- "PROS / WPROS deposit" --> ST
    ST -- "PROS" --> SD
    SD -- "keeper allocation" --> SP
    SP -- "delegate" --> PS
    PS -- "claimStake: stake returned" --> SP
    SP -- "transferBack" --> SD
    SD -- "keeper allocation" --> BV
    SD -- "reconciled rewards only" --> RV
    PS -- "claimReward" --> SP
    SP -- "rewards" --> RV
    RV -. "update(WPROS, amount)" .-> OR
    RV -- "reported PROS" --> SP
    ST -. "onMint / onRedeem, exchange rate" .-> OR
    OR -. "mintCommission" .-> ST
    U -- "withdrawComplete: claim" --> ST
    BV -- "PROS / WPROS payout" --> U
    U -- "stPROS" --> YV
```

The SlpDistributor → RewardsVault route is for reconciled reward funds only, never user principal; see
[SLP staking](docs/SLP-STAKING.md). [Architecture](docs/ARCHITECTURE.md) explains each contract and flow.

## Mainnet deployment

**Pharos Mainnet, chainId 1672. Release V4, executed on 2026-10-21** (upgrade transaction `0xac1fd830…f31e1b`, block
20,813,316). Addresses were verified at block 20,814,599; getters were re-read at block 20,815,667.

Integrate with the **proxy** address. Never call an implementation address: implementations hold no protocol state.

| Contract | Proxy (use this) | Implementation | ABI |
|---|---|---|---|
| StPROS | `0x6b0a44c64190279f7034b77c13a566e914fe5ec4` | `0xfd59860894e01773e9780b0d1dfae4ca2b56708d` | [`v4/StPROS.json`](deployments/mainnet/v4/StPROS.json) |
| Oracle | `0x051713fd66845a13bf23baca008c5c22c27ccb58` | `0x09c5ec5eb4e717c3c472d8c89812b7fad32929b5` | [`v4/Oracle.json`](deployments/mainnet/v4/Oracle.json) |
| BridgeVault | `0xf1df1cb080256f48e8702c24149136e4a6dec25e` | `0xa7d36cb9da47ba5123f9070fba4bb5239c58387e` | [`v4/BridgeVault.json`](deployments/mainnet/v4/BridgeVault.json) |
| SlpDistributor | `0xf772b375f93650f798bf724ce7a9a45c9e7a579f` | `0x05a224a9cdd84cfca21866ae50fa0cd663e3752f` | [`v4/SlpDistributor.json`](deployments/mainnet/v4/SlpDistributor.json) |
| YieldVaultFactory | `0xc9fb7dc52b0fb92c417d481442d2641637483881` | `0xfe34322dcd367b6cc3067a034e8f64b488bfbae9` | [`v4/YieldVaultFactory.json`](deployments/mainnet/v4/YieldVaultFactory.json) |
| YieldVault 001 | `0x36f3d19dda7ed1428e3014ae6f2a75d70393b7e6` (beacon proxy) | `0xd42fef079a694fc3f6259a614ae271cba2d5ee65` (via beacon `0x8618bfb59de57c6bff52638bf0c56f04cc58ed88`) | [`v4/YieldVault.json`](deployments/mainnet/v4/YieldVault.json) |

| Governance and asset | Address |
|---|---|
| TimelockController | `0xc30db0543412c6881a543390d2c21fbbff42805b` |
| ProxyAdmin (OpenZeppelin v4, owned by the Timelock) | `0x4238ea4adfa2bd6a5fc9b5e245dc1900cf0258aa` |
| WPROS | `0x52c48d4213107b20bc583832b0d951fb9ca8f0b0` |

The authoritative source is [`deployments/mainnet/v4/addresses.json`](deployments/mainnet/v4/addresses.json). The
machine-readable index is [`deployments/registry.json`](deployments/registry.json). Governance accounts, the SLP layer
and the out-of-scope historical contracts are listed in [deployments](deployments/README.md) and
[governance](docs/GOVERNANCE.md).

## Getting started

| Document | Read it to |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Understand the contracts, their asset flows and their limits |
| [Integration](docs/INTEGRATION.md) | Integrate stPROS: deposits, redemption requests, claims, events and examples |
| [SLP staking](docs/SLP-STAKING.md) | Understand Pharos staking, rewards and the SLP layer, and its risks |
| [Parameters](docs/PARAMETERS.md) | Look up current values and who can change them |
| [Governance](docs/GOVERNANCE.md) | Review roles, the Timelock, permissions and LOSS planning |
| [Recovery](docs/RECOVERY.md) | Handle pauses, custody and queue recovery, Oracle anomalies and vault incidents |
| [Developer toolchain](docs/TOOLCHAIN.md) | Build, test and work on the repository; repository layout |
| [Deployment addresses and ABIs](deployments/README.md) | Find mainnet V4, testnet and historical records |

Maintainers preparing a future upgrade also read [Upgrades](docs/UPGRADES.md).

## Security

- Smart contracts carry risk, including the risk of total loss. The Oracle, the keeper, the reporter, the SLP
  destinations and governance are trusted roles with bounded powers; [governance](docs/GOVERNANCE.md) and
  [SLP staking](docs/SLP-STAKING.md) describe them.
- The protocol commission is charged on every amount the Oracle accepts as a yield report. The contracts cannot tell
  whether a reported amount is real reward. If non-reward funds are reported, they raise the share value and are
  charged commission as well ([SLP staking](docs/SLP-STAKING.md#known-risk-principal-reported-as-yield)).

### Audit scope

| Scope | Contracts | Status |
|---|---|---|
| Core audit package | The 12 core files at frozen commit `a110037`: StPROS, VToken, Oracle, BridgeVault, SlpDistributor, YieldVault, YieldVaultFactory, GuardedOwnableUpgradeable, MigrationGuard, ReleaseAssertions, IOracle, IWPROS | Frozen audit package prepared for external audit. This repository records neither its delivery to an auditor nor a completed external audit report |
| SLP Extension | `SlpPharosProxy`, `SlpPharosRewardsVault` | Not in the `a110037` package. The owner reports that the ABDK statement of work lists it as a separate scope; that document is not stored here. **These two contracts must not be considered externally audited** |

Internal reviews are not an external audit. The SLP contracts are deployed and administered outside this repository's
release process ([deployment status](docs/SLP-STAKING.md#deployment-status)).

## License

GPL-3.0, as declared in each source file.
