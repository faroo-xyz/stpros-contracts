# Governance transaction archives

This directory preserves Safe transaction packages and their provenance. Each
package distinguishes unsigned preparation from historical execution verified
against chain receipts. Original file labels and review warnings are retained;
archiving never establishes release approval or current on-chain state.
Authoritative deployment and upgrade records remain in `deployments/`.

| Network | Prepared on | Package | Status |
|---|---|---|---|
| Pharos Mainnet (1672) | 2026-10-21 | [V4 handoff and upgrade](mainnet/20261021-v4-upgrade/README.md) | Executed and verified; original review-only labels retained |
| Pharos Mainnet (1672) | 2026-10-27 | [Timelock delay: 600 to 86,400 seconds](mainnet/20261027-delay-1day/README.md) | Both Safe transactions executed; 24-hour delay verified |

See [Governance](../docs/GOVERNANCE.md) for roles and the two-stage Timelock process.
Preserve the exact archived payloads. Changed calls or salts need a new operation
and a separate preparation package; do not overwrite a package used for signing.
