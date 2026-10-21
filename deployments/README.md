# Deployments

## Purpose

This directory holds the deployment records of the Faroo contracts: addresses, ABIs, bytecode, storage layouts,
deployment transactions and upgrade receipts. Records are **append-only**. Historical files are never rewritten,
because upgrade tooling, legacy fixtures and snapshots read them as evidence of the pre-upgrade state.

## Where to start

| You need | Use |
|---|---|
| Current mainnet addresses | [`mainnet/v4/addresses.json`](mainnet/v4/addresses.json): the **single authoritative source** |
| A machine-readable index: addresses, ABI files, status of every record | [`registry.json`](registry.json) |
| The V4 ABI of a contract | `mainnet/v4/<Contract>.json`, key `abi` (table below) |
| What the V4 upgrade changed | [`mainnet/v4/V4_UpgradeReceipt.json`](mainnet/v4/V4_UpgradeReceipt.json) |
| Historical mainnet V1 records | [`mainnet/`](mainnet/README.md) |
| Testnet records | [`testnet/`](testnet/README.md) |

## Layout

```text
deployments/
├── README.md                 this file
├── registry.json             derived index (generated; never edit by hand)
├── mainnet/                  Pharos Mainnet, chainId 1672
│   ├── README.md             what each historical file is
│   ├── .chain                chain id and genesis hash
│   ├── *.json                V1 records (pre-upgrade): kept unchanged for tools and evidence
│   └── v4/                   V4 records: CURRENT
│       ├── README.md
│       ├── addresses.json    authoritative V4 address table
│       ├── <Contract>.json   proxy address + V4 ABI (use for integration)
│       ├── <Contract>_Implementation.json, *_Proxy.json, TimelockController.json, ReleaseAssertions*.json
│       └── V4_UpgradeReceipt.json, generation-summary.json
└── testnet/                  Pharos Atlantic Testnet, chainId 688689 (pre-V4)
    ├── README.md
    ├── .chain
    └── *.json
```

## Mainnet V4 contracts and ABIs

Addresses are in the [README](../README.md#mainnet-deployment) and in `mainnet/v4/addresses.json`. Use the **proxy**
address with the ABI file in the first column. Each implementation record holds the same ABI plus the deployment
transaction, receipt, constructor arguments, storage layout and the runtime hash verified on chain.

| Contract | ABI file (proxy, use this) | Implementation record | Proxy kind | Third-party use |
|---|---|---|---|---|
| StPROS | [`mainnet/v4/StPROS.json`](mainnet/v4/StPROS.json) | [`StPROS_Implementation.json`](mainnet/v4/StPROS_Implementation.json) | Transparent (OZ v4) | Deposits, redemption requests, claims |
| Oracle | [`mainnet/v4/Oracle.json`](mainnet/v4/Oracle.json) | [`Oracle_Implementation.json`](mainnet/v4/Oracle_Implementation.json) | Transparent (OZ v4) | Read-only: rate, pool, limits |
| BridgeVault | [`mainnet/v4/BridgeVault.json`](mainnet/v4/BridgeVault.json) | [`BridgeVault_Implementation.json`](mainnet/v4/BridgeVault_Implementation.json) | Transparent (OZ v4); deployment: [`BridgeVault_Proxy.json`](mainnet/v4/BridgeVault_Proxy.json) | Read-only: liquidity. Claims go through StPROS |
| SlpDistributor | [`mainnet/v4/SlpDistributor.json`](mainnet/v4/SlpDistributor.json) | [`SlpDistributor_Implementation.json`](mainnet/v4/SlpDistributor_Implementation.json) | Transparent (OZ v4); deployment: [`SlpDistributor_Proxy.json`](mainnet/v4/SlpDistributor_Proxy.json) | Operational (keeper) |
| YieldVaultFactory | [`mainnet/v4/YieldVaultFactory.json`](mainnet/v4/YieldVaultFactory.json) | [`YieldVaultFactory_Implementation.json`](mainnet/v4/YieldVaultFactory_Implementation.json) | Transparent (OZ v4) | Read-only: authenticate vaults (`isFactoryVault`) |
| YieldVault 001 | [`mainnet/v4/YieldVault.json`](mainnet/v4/YieldVault.json) | [`YieldVault_Implementation.json`](mainnet/v4/YieldVault_Implementation.json) | Beacon proxy | Vault deposits and redemptions (stPROS) |
| TimelockController | [`mainnet/v4/TimelockController.json`](mainnet/v4/TimelockController.json) | (not a proxy) | — | Governance; watch its events |
| ProxyAdmin | [`mainnet/DefaultProxyAdmin.json`](mainnet/DefaultProxyAdmin.json) | (not a proxy; same contract since V1) | — | Governance |
| ReleaseAssertions | [`mainnet/v4/ReleaseAssertions.json`](mainnet/v4/ReleaseAssertions.json) | (not a proxy) | — | None: used once by the V4 release transactions |

**ABI source of truth.** The `abi` array in these records is the compiler ABI of the canonical V4 build. No separate
ABI-only files are kept, so there is no second copy to drift. To extract one:

```sh
python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["abi"], sys.stdout, indent=1)' \
  deployments/mainnet/v4/StPROS.json > StPROS.abi.json
```

WPROS is external to this repository. Its interface is `contracts/interfaces/IWPROS.sol` (`deposit`, `withdraw`)
plus standard ERC-20.

## Status of records

`registry.json` classifies every JSON file in this directory:

| Status | Meaning | Examples |
|---|---|---|
| CURRENT | Describes the verified current production deployment | `mainnet/v4/*`, `mainnet/DefaultProxyAdmin.json` |
| LEGACY | Historical record kept for evidence and tooling. The contract may still exist, but this file is not the integration source | `mainnet/*_Proxy.json`, `mainnet/YieldVaultFactory_test*.json`, `mainnet/YieldVault_test.json` |
| SUPERSEDED | An implementation, ABI or contract that has been replaced | `mainnet/StPROS.json` (V1 ABI), `mainnet/*_Implementation.json`, `mainnet/v4/ReleaseAssertions_P0_Superseded.json` |
| TESTNET | Atlantic testnet record. Testnet has not been upgraded to V4 | `testnet/*` |

Not production addresses: the historical test factory `0xbc3a9090…2d00` and its vault `0xc775dfaa…591b` were left out
of V4 by owner decision. Neither does any implementation address.

## Rules for this directory

- **Append-only.** Do not edit, rename, move or delete an existing record. New deployments get new files; a new release
  gets a new subdirectory, as `v4/` did.
- **Do not add JSON files at the top of `mainnet/` or `testnet/`.** Tools treat `deployments/<chain>/*.json` as the
  recorded legacy deployment set: ABI, storage and bytecode snapshots, legacy fixtures, provenance and fork tests.
  Subdirectories and Markdown files are not read by them.
- **`registry.json` is generated.** The generator lives in the research repository
  (`development-history/v4-release-readiness/developer-docs-20261021/scripts/build_deployments_registry.py`). It
  refuses to run if an address disagrees with `addresses.json` or a JSON file is unclassified. Its `--check` mode also
  detects edited records through their SHA-256.
- `tools/release/registry.json` is a different file: the release tooling's legacy-identity registry. It is not a
  deployment index.
