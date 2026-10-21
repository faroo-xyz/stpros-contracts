# Pharos mainnet (chainId 1672): V4 deployment records

V4 upgrade executed on 2026-10-21. Release id `faroo-v4-mainnet-rev3-4slp-20261021`, Timelock operation `0x6918b2948262402d63bce93439012cbff5a451430e7dcf4c8f13d6da46f2c959`.

The files one level up (`deployments/mainnet/*.json`) are the historical V1 records. They are append-only and unchanged: tools and fixtures read them as the pre-upgrade state, so the V4 records live in this subdirectory.

| File | Content |
|---|---|
| `Oracle.json`, `StPROS.json`, `YieldVaultFactory.json`, `BridgeVault.json`, `SlpDistributor.json`, `YieldVault.json` | User-facing contracts: the proxy address with the V4 ABI, the current implementation and the proxy admin. `YieldVault.json` is the official vault 001, a beacon proxy |
| `*_Implementation.json` | V4 implementations: ABI, storage layout, deployment transaction and receipt, constructor arguments, runtime hash checked on chain against the canonical artifact, artifact field hashes from `tools/ci/r4-reference.json` |
| `BridgeVault_Proxy.json`, `SlpDistributor_Proxy.json` | The new OpenZeppelin v4 TransparentUpgradeableProxy deployments |
| `TimelockController.json` | Governance Timelock (600 s): GOV proposer, executor and canceller; CAN canceller |
| `ReleaseAssertions.json` | Release assertions used by H (`assertHandoff`) and U (`assertR4`) |
| `ReleaseAssertions_P0_Superseded.json` | P-0 step 6, superseded (two-Factory shape), kept as a record |
| `V4_UpgradeReceipt.json` | The executed handoff (H) and upgrade (U): transactions, gas, implementations before and after, parameters, SLPs, keeper, state-preservation checks |
| `addresses.json` | Every V4 address by role, the SLP destinations, the actors, and the out-of-scope test factory and vault |
| `generation-summary.json` | Generation and verification summary (block, records, no problems) |

**How these files were produced.** Read-only. Each deployment transaction was read from chain: deployer, nonce, contract address, status. Each runtime was checked at block 20,814,599 against the canonical build (`tools/ci/canonical.py`). The generator and the full evidence are in the research repository: `development-history/v4-release-readiness/mainnet-v4-release-gates-20261021/` (`scripts/make_v4_deployment_records.py`, `mainnet-execution/`).
