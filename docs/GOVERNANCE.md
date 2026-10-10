# Governance and release authority

The research repository's frozen audit roadmap and R1-final + A1 amendments are authoritative. This development tooling does not authorize deployment. Development occurs on `main`; all changes remain uncommitted until requested.

| Authority | Approved responsibility |
|---|---|
| GOV | Sole Timelock proposer and executor; mainnet Safe 3/5 after signer rotation |
| CAN | Independent Safe, threshold at least 3; canceller, never proposer/executor |
| GRD | Independent fast-response guardian Safe; pause only, cannot unpause or upgrade |
| OPS | Vault operating role; may share GRD; never Timelock or counterparty/fee recipient |
| Timelock | Owns shared recorded OZ v4 ProxyAdmin and all core/Factory proxies; LOSS authority |
| Factory | Owns its existing beacon; only Timelock can upgrade it |
| Treasury | Reviewed Safe receiving mainnet future-yield commission |
| Deployer | No retained Timelock role or Safe signer authority after setup |

Mainnet delay is exactly 172,800 seconds; testnet is exactly 300 seconds. Timelock is its own sole default admin, GOV has proposer/executor/canceller, CAN has canceller, and there is no open executor. GOV/CAN/GRD signer sets must be disjoint. The release config supplies actual Safe identities, singleton hashes, owners, thresholds, nonce, empty module lists and zero guard; read-only preflight checks them. Testnet designated multisig alternatives require a separate reviewed adapter; this implementation deliberately supports the stricter Safe profile for release output. Simulations may use labelled synthetic actors.

`ReleaseAssertions` has no storage, privileged function, delegatecall, external state-changing call or administrator. Every function is view/pure. It checks reviewed expectations by STATICCALL. Proxy admin/implementation checks go through the **recorded OZ v4 ProxyAdmin** public `getProxyAdmin`/`getProxyImplementation` wrappers. It never pretends its own SLOAD can inspect another contract. Initializer versions are checked with pinned RPC storage reads at the OZ namespaced initializer slot.

A role-array hash binds calldata; it does **not** establish inventory completeness. The off-chain gate rescans every RoleGranted/RoleRevoked event for all role IDs, from a block before code existed, in contiguous windows of at most 1,000 blocks; unexpected RoleAdminChanged requires review. It compares the replay to actual `hasRole` state. Before execution and after migration, every live role outside owner-approved residual retirement targets must exactly match the frozen expected set, including formerly unknown actors. C/D and E2-unavailable vault roles remain completely inventoried but informational: they are explicitly accepted exposure before U and inert only after verified retirement. An RPC provider capable of omitting historical data remains a trust assumption; independently reviewed complete history and release rehearsal are mandatory.

The product policy is unchanged: mainnet launch commission 100,000 ppm on future yield, Treasury Safe recipient; approved APR is twice owner-selected expected gross APR and at most 500,000 ppm; update interval at least one hour. Factory launch performance fee is zero, future changes are Timelock-only and capped at 3,000 bps. Historical round fees and their 10,000 denominator remain unchanged. New unbonding settings have a one-day floor and thirty-day ceiling; migrations preserve the historical mainnet seven-day and testnet zero settings and individual withdrawal records.

Legacy direct Oracle/StPROS upgrades, beacon upgrades and generic ownership-handoff tasks now refuse before touching the runtime. Previously deprecated initializer/setter tasks still refuse. Release builders never produce `changeProxyAdmin`, `renounceOwnership`, an implementation-only core upgrade, or an unguarded initializer. Other ordinary administrative tasks are not a release workflow; only reviewed Timelock calldata is authorized for an R4 release.

The RPC checks distinguish the approved historical block from the current chain head. They independently bind every selected historical target (including Oracle and testnet BridgeVault/SlpDistributor) to the chain-specific registry; mainnet fresh BridgeVault/SlpDistributor require separate reviewed deployment records. Standalone release builders enforce that registry binding too. Current proxy/Beacon code and upgrade/admin history are checked in addition to getters. No atomic assertion was moved off-chain during review closure; the Solidity refactor preserves its public ABI and only consolidates repeated Vault asset reads.
