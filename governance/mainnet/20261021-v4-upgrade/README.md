# stPROS V4 upgrade: executed H / U Safe files

This directory preserves the two original Safe Transaction Builder files used for
the Pharos Mainnet V4 upgrade on 2026-10-21. These are historical execution inputs.

| Stage | Original file | Safe nonce | Mainnet transaction | Block |
|---|---|---|---|---|
| H: transfer authority and schedule U | [REVIEW-ONLY-mainnet-v4-handoff.safe.json](REVIEW-ONLY-mainnet-v4-handoff.safe.json) | 18 | `0x8649402fccf056bf523ba53cec87ac4425ed466347460eb4069345ce80097add` | 20,812,661 |
| U: execute the upgrade through the Timelock | [REVIEW-ONLY-mainnet-v4-upgrade.safe.json](REVIEW-ONLY-mainnet-v4-upgrade.safe.json) | 19 | `0xac1fd830e147cae17262763fee24e81cc27d06149bba897deb55c2cf1bf31e1b` | 20,813,316 |

- Network: Pharos Mainnet, chain ID `1672`.
- GOV Safe: `0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca`.
- Timelock: `0xc30db0543412c6881a543390d2c21fbbff42805b`.
- Release ID: `faroo-v4-mainnet-rev3-4slp-20261021`.
- Operation ID: `0x6918b2948262402d63bce93439012cbff5a451430e7dcf4c8f13d6da46f2c959`.

## Original files and preparation status

The files came from the sibling research repository:

```text
faroo-contracts-reserch/development-history/v4-release-readiness/
  mainnet-v4-release-gates-20261021/review-only-final/
```

The release directory's `mainnet-execution/MAINNET-V4-EXECUTION-RECORD.md`
records the owner's decision to use these files and the successful H/U transactions.
The original `PREVIEW-MANIFEST.json` binds these SHA-256 values:

| File | SHA-256 |
|---|---|
| H | `ec371c940c56055a047916ba76193d36ff641bd75ad039a4d67a903c8c32768e` |
| U | `490a76483e3396eb95ff92984aad9da1a75af5638f6ab116f746b19fdde63dfb` |

The original `REVIEW ONLY`, `NOT FOR SIGNING`, `simulationOnly` and `deployable`
labels are preserved byte for byte. They describe the preparation and review
state recorded at that time. Subsequent execution is documented separately;
successful chain execution does not retrospectively certify missing release gates.

## Read-only execution verification

The 2026-10-27 verification used the public read-only RPC `https://rpc.pharos.xyz`.
See [verification.json](verification.json) for the initial receipt and payload checks.
The later recheck is recorded under `historicalV4Recheck` in the
[Timelock execution verification](../20261027-delay-1day/execution-verification.json).

- Both files match the original bytes and manifest hashes.
- H's on-chain Safe `execTransaction` delegates to MultiSend. All eight unpacked
  calls match the H file in order, including targets, amounts, calldata and operation types.
- U's on-chain Safe call targets Timelock `executeBatch` and matches the U file exactly.
- Both receipt block hashes match the production upgrade record. Both receipts
  succeeded and contain GOV Safe `ExecutionSuccess` events.
- The Safe nonces were 18 and 19. H's `scheduleBatch` and U's `executeBatch` carry
  identical operation arguments, with a 600-second scheduling delay.
- H emitted nine `CallScheduled` events and U emitted nine `CallExecuted` events
  for this operation. The recomputed operation ID matches. Execution followed
  scheduling by 625 seconds, and the operation was Done at the upgrade block.

[checksums.sha256](checksums.sha256) fixes the original file bytes. The authoritative
production upgrade record remains
[`deployments/mainnet/v4/V4_UpgradeReceipt.json`](../../../deployments/mainnet/v4/V4_UpgradeReceipt.json).
Historical deployment records are unchanged by this archive.
