# R4 approved product policy and implementation

Status: development implementation for independent review, not release authorization. This specification follows R1-final + A1, the approved four-argument Oracle V3 amendment and the owner-approved final product policy dated 2026-10-10. The approval record and exact verification results are in the research repository's `architecture-hardening/amendments/FINAL-PRODUCT-POLICY.md` and development-history reports. Historical decisions remain archived there.

## Approved policy

**Oracle.** Mainnet launch commission is 100,000 ppm (10%) of future reported yield, using `c = floor(f * V / (T + delta - f))`. Deposits/principal and the migration itself are not charged. Configure the existing V1 migration parameters with that rate and a reviewed Treasury Safe supplied at release; rate/recipient changes thereafter require the owner, which is Timelock. The contract retains its existing configurable rate mechanism rather than hardcoding the launch rate or a Safe address. On-chain nonzero/rate-bound checks do not establish that a recipient is a reviewed Safe: release checks must attest its actual identity/signers and the 100,000-ppm launch calldata.

Oracle minimum update interval is 1 hour, maximum catch-up 7 days, hard APR ceiling 500,000 ppm. Operational maxAprPpm is twice expected gross APR, subject to that ceiling; no actual expected APR has been supplied. Keep the absolute per-update cap. The deposit sink is SlpDistributor. The reporter is an approved SLP contract or dedicated service capable of satisfying the APR/interval constraints.

**Performance fees.** Every Factory has `uint16 approvedPerformanceFeeBps`, initially zero. Only its owner/Timelock can call `setApprovedPerformanceFeeBps(uint16)`, bounded by 3,000 bps. OPS must supply exactly the approved fee for the first and every subsequent round. The round snapshots it in the existing `performanceFeeBps` field. Policy changes affect only rounds opened after the change; even if changed while OPEN, LOCKED or proposed, current/historical settlement reads its stored fee and uses the unchanged 10,000-bps denominator. Historical stored fees above the new-round ceiling remain valid and are not revalidated during upgrade. Future nonzero fees need a governance transaction, not an upgrade.

**Unbonding.** Future `setUnbondingPeriod` calls require 1–30 days. Initial/mainnet 7-day configuration is unchanged. Neither migration nor a setter rewrites any existing withdrawal's captured waiting period. The historical testnet zero global setting survives V3 migration; it remains effective for new requests until governance configures a new value, at which point the one-day minimum applies. Zero-wait historical records stay zero-wait.

**Vault timing and LOSS.** OPEN is 1–90 days; lock <=365 days; scheduled opening delay <=30 days; round loss <=5,000 bps; settlement grace 30 days; minimum proposal window 7 days; maximum settlement window 23 days. Existing one-second minimum lock/settlement windows remain compatible. Existing rounds retain their recorded parameters. LOSS proposal/replacement/cancellation is Timelock-only; OPS controls bounded round operations and PROFIT. A scheduled OPEN permits redemption before openedAt; deposits wait.

## Implemented accounting, governance and compatibility

The existing v4 TransparentUpgradeableProxy/ProxyAdmin and Factory-owned beacon topology remain. MigrationGuard checks the proxy admin and previous version before reinitializer(4); fresh initialization is constructor-only. Guardian/Ownable2Step storage is namespaced. Guardian may pause, only owner may unpause/rotate it, and renunciation rejects. The implementation checks code-bearing positive-delay governance; exact approved delays, Safe identities and Timelock roles are release checks.

StPROS V1 migration uses donation-tolerant relational backing/queue checks, transfers exactly the legacy reserve into BridgeVault, forwards the remaining wrapped custody to SlpDistributor, burns the full escrow and seeds T = V = remaining supply. It preserves queue records and Q/C. All six V1 claim signatures remain: withdrawComplete overloads pay WPROS; withdrawCompleteToPROS overloads pay native with WPROS fallback. Unbound records honor claim-time receivers; bound records keep request-time receivers. maxRecords counts fully consumed records; zero imposes no additional limit. Queue capacity is configurable within 1–32. Claims are unaffected by StPROS/Oracle pause but respect BridgeVault custody pause. User entry points share the transient reentrancy guard.

Oracle V3 migration remains `migrateFromV3(address,uint256,address,address)` with onlyProxyAdmin -> onlyFromVersion(3) -> reinitializer(4). The confirmed single WPROS/stPROS pool is validated through the existing registered canonical mapping and asset() relation; the historical inventory supplies the single-pool assumption, with no hardcoded deployment address or enumeration. Preserve non-1:1 T/V, supply, balances/queue, mappings, reporter, absolute cap, commission account/rate and pause. Normalize interval to max(old,1 hour), reset lastUpdateAt[token], accept an explicitly supplied bounded APR (including zero), and migrate governance. Upgrade Oracle before StPROS, BridgeVault and Distributor. The mainnet launch policy never overwrites testnet commission during V3 migration.

Oracle accounting is canonical-vToken-only; empty-pool initialization and CAS NAV repair never arbitrarily rewrite share accounting. Zero remaining supply zeroes token accounting. Reporter updates require nonzero supply and both interval and APR/absolute bounds. Commission logic is unchanged by the final product-policy task. canUpdate remains advisory rather than a guarantee of external mint success.

BridgeVault rejects zero-asset registrations, supports wrapped claims and permissionless donated-WPROS unwrapping, and retains Timelock custody recovery. Distributor requires an unpaused keeper and contract-code whitelist entries. External destination safety remains a release review/monitoring requirement.

Vault initialization writes authority dependencies before guarded role grants. Canonical factory/admin/operator pinning remains; lookalikes on attacker-owned factories are outside this guarantee and require registry checks in integrations. retireVault refunds funded proposals and permanently settles the vault despite stale roles; redemption remains available. closeCreation is terminal unless upgraded. Factory's immutable constructor dependency is a separately deployed reviewed Vault implementation; mandatory full runtime Codehash/binding checks are specified in the Factory technical amendment. Existing factories retain their beacons until an explicit upgrade.

### Storage and upgrade ordering

The performance policy packs into Factory slot 4: timelock bytes 0–19, creationClosed byte 20, approvedPerformanceFeeBps bytes 21–22. No new linear slot is introduced. Vault storage is unchanged (retired remains slot 22); Oracle's existing maxAprPpm remains slot 9; deprecated VToken slots remain intact. Factory must be upgraded before its beacon so new round openings can read the approved-fee getter. Fresh and recorded legacy Factory/Factory_test proxies initialize the new fee to zero by zero-initialized unused storage; old rounds retain their stored fees.

## Approved governance design — not attested deployment state

Mainnet Timelock minimum delay 48 hours; testnet 300 seconds. Use Timelock-first P-1 -> H -> U, GOV-only executor, independent CAN with disjoint signers and threshold >=3. GRD is a fast Safe (2-of-4 recommended); OPS is separate from Timelock and may initially use the reviewed GRD Safe. Remove deployer from GOV before release. Permanently retire Factory_test and its legacy vault. Testnet vault[14] is GOVERNED; other 14 are RETIRED. Do not pause V1 during H->U, because native claims would be interrupted. No live Safes, Timelocks or release transactions are created by the policy implementation task.

## Deadline economics

A1 derives D = openDeadline + lockDuration + SETTLEMENT_GRACE; every proposal/replacement requires now + settleTimelockWindow <= D. At D, effective LOCKED (including unsynchronized OPEN) or an unfunded proposal can settle at par without asset transfers, even while paused. Funded PROFIT, zero PROFIT and LOSS remain binding and finalize under the existing unpause dependency. A late LOSS authorization may forfeit the counterparty's loss payment; unfunded profit can be lost to holders if force wins. OPS retains the already documented yield-only cancellation/refund behavior but cannot authorize LOSS principal outflows.

These consequences are approved policy. Signed counterparty agreement remains an external release prerequisite; it is not proven by code or owner design approval.

## External release inputs still required

Actual expected gross APR and selected bounded operational APR; absolute cap/reporting cadence; approved reporter identity/suitability; reviewed Treasury Safe address/code/owners/threshold; GOV/CAN/GRD/OPS Safe identities and signer separation; actual Timelock delays/roles/ownership; deployer removal; Factory/Vault runtime codehash and constructor binding; team authority for migration/retirement; counterparty agreement; and the planned release/fork/monitoring checks. None is inferred, fabricated or declared satisfied here.

Focused test, compilation, storage/ABI/size results are reported in the research repository. Full CI, formal assurance and public-chain release rehearsal remain outside this task. Historical snapshots and deployment records are unchanged. Independent review is required before any commit or release action is separately authorized.
