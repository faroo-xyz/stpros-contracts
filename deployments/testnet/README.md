# Pharos Atlantic Testnet records (chainId 688689)

**Testnet has not been upgraded to V4.** At testnet block 31,692,763 (read-only check, 2026-10-21) every proxy below
still used the implementation recorded here, the core contracts and the ProxyAdmin were owned by externally owned
accounts (`0x4597…01dd`; the Factory by `0xb29e…adf1`), and the StPROS waiting period was 0. The V4 testnet sequence in [UPGRADES.md](../../docs/UPGRADES.md) has not been executed. Do not
assume testnet behaves like mainnet V4.

| Contract | Proxy | Recorded implementation | Files |
|---|---|---|---|
| StPROS | `0xb1437aee…2e9a` | `0x64dc1e8b…f576` | `StPROS.json` (earlier ABI), `StPROS_Implementation.json`, `StPROS_Proxy.json`, `StPROS_UpgradeReceipt.json` (implementation upgrade at block 31,153,535) |
| Oracle | `0x6bd39d03…b0bc` | `0xf4caf689…5384` | `Oracle.json` (earlier ABI), `Oracle_Implementation.json`, `Oracle_Proxy.json` |
| BridgeVault | `0xb79db650…9ddc` | `0xafe333e1…8e47` | `BridgeVault.json`, `BridgeVault_Implementation.json`, `BridgeVault_Proxy.json` |
| SlpDistributor | `0x008a7856…a7f2` | `0x6a3dd866…2a45` | `SlpDistributor.json`, `SlpDistributor_Implementation.json`, `SlpDistributor_Proxy.json` |
| YieldVaultFactory | `0x558de0e2…124c` | `0xf51f16d9…20d1` | `YieldVaultFactory.json`, `YieldVaultFactory_Implementation.json`, `YieldVaultFactory_Proxy.json` |
| ProxyAdmin (OZ v4) | `0x79d60282…d667` | — | `DefaultProxyAdmin.json` |
| YieldVault beacon implementation | — | `0x2e388f1a…5984` | `YieldVault.json` (not a vault) |

Full addresses and the status of each file are in [`../registry.json`](../registry.json) (`networks.testnet`). The
pre-V4 testnet state at block 31,190,661, including the 15 testnet vaults, is in the generated
[DEPLOYED-STATE.md](../../docs/DEPLOYED-STATE.md).

These files are inputs of the testnet upgrade tooling, legacy fixtures and snapshots. They are append-only.
