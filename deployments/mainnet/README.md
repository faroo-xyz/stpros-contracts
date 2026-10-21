# Pharos Mainnet records (chainId 1672)

**Current deployment: V4, in [`v4/`](v4/README.md).** Integrate with `v4/addresses.json` and the `v4/<Contract>.json`
ABIs.

The JSON files directly in this directory are the **V1 records**, from before the V4 upgrade of 2026-10-21. They are
kept unchanged because upgrade tooling, legacy fixtures (`tests/fixtures/legacy/`) and snapshots (`snapshots/`) read
them as the recorded pre-upgrade state. Several of these files carry the address of a contract that is still live (a
proxy) but describe its **old** implementation or ABI. Do not use them for integration.

| File | Address | Status | What it is |
|---|---|---|---|
| `DefaultProxyAdmin.json` | `0x4238ea4a…58aa` | CURRENT | Shared OZ v4 ProxyAdmin. Same contract and ABI after V4; owner is now the Timelock |
| `Oracle.json` | `0x051713fd…cb58` | SUPERSEDED | Oracle proxy address with the **V1 ABI**. Use `v4/Oracle.json` |
| `Oracle_Implementation.json` | `0x7746ef54…5717` | SUPERSEDED | Oracle V1 implementation |
| `Oracle_Proxy.json` | `0x051713fd…cb58` | LEGACY | Proxy deployment record; the proxy is still the Oracle address |
| `StPROS.json` | `0x6b0a44c6…5ec4` | SUPERSEDED | stPROS proxy address with the **V1 ABI**. Use `v4/StPROS.json` |
| `StPROS_Implementation.json` | `0xc096f8e4…c57b` | SUPERSEDED | StPROS V1 implementation |
| `StPROS_Proxy.json` | `0x6b0a44c6…5ec4` | LEGACY | Proxy deployment record; the proxy is still the stPROS address |
| `YieldVault.json` | `0xba6f3053…9ab9` | SUPERSEDED | YieldVault **V1 beacon implementation**, not a vault. The official vault is `v4/YieldVault.json` |
| `YieldVaultFactory.json` | `0xc9fb7dc5…3881` | SUPERSEDED | Factory proxy address with the V1 ABI. Use `v4/YieldVaultFactory.json` |
| `YieldVaultFactory_Implementation.json` | `0x772e27a3…5706` | SUPERSEDED | Factory V1 implementation |
| `YieldVaultFactory_Proxy.json` | `0xc9fb7dc5…3881` | LEGACY | Proxy deployment record; the proxy is still the Factory address |
| `YieldVaultFactory_test.json`, `_test_Implementation.json`, `_test_Proxy.json` | `0xbc3a9090…2d00` | LEGACY | Historical test factory. **Out of V4 scope**: still V1 and owned by an externally owned account, not the Timelock. Not a production address |
| `YieldVault_test.json` | `0x2b2ef1f0…9dd3` | LEGACY | Beacon implementation of the test factory, not a vault. Out of V4 scope |
| `.chain` | — | — | `{"chainId":"1672", genesisHash}` used by the tooling |

Implementation history and the pre-V4 state at block 19,774,476 are in the generated
[DEPLOYED-STATE.md](../../docs/DEPLOYED-STATE.md) (historical).
