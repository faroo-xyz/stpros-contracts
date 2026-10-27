# stPROS governance Timelock: executed switch to one day

Task-ID: BD-NINGBO-STPROS-TIMELOCK-1DAY

Both Safe transactions have executed on Pharos Mainnet. The Timelock minimum delay
is **86,400 seconds (24 hours)**, and this operation is Done. The two archived Safe
JSON files remain byte-identical to the unsigned preparation files used for execution.

| Item | Value |
|---|---|
| Network | Pharos Mainnet, chain ID `1672` |
| GOV Safe | `0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca` |
| Timelock / recipient | `0xc30db0543412c6881a543390d2c21fbbff42805b` |
| Value / Safe operation | `0 PROS` / `CALL` |
| Minimum delay change | `600` to `86400` seconds |
| Operation ID | `0x9d22e0c9d0150ec6414dab01bb79eef24158e29cc2bb4a92676b4ba16c33c44b` |

## Executed transactions

| Step | Safe nonce | Mainnet transaction | Block |
|---|---|---|---|
| Schedule | 20 | `0xc2f4576f98d0cb5c3174df79e1300e377f0cbcf94e69cdc03c341b3f4a9e2f5e` | 21,395,039 |
| Execute | 21 | `0x38274c2922cd88eeb47b45ef90f0908a039dc9a3e7c4de9cc9cdd3a490d1fa5e` | 21,406,401 |

The execution followed scheduling by **11,301 seconds**.


The first transaction scheduled a Timelock self-call to `updateDelay(86400)` with
the then-current 600-second delay. Scheduling left the minimum delay at 600 seconds.
The second transaction executed the identical target, value, payload, predecessor
and salt after the operation became Ready. Its `MinDelayChange` event records
`600` to `86400`. These were separate Safe transactions.

The original preparation filenames are retained:

- [01-schedule.safe.json](01-schedule.safe.json)
- [02-execute.safe.json](02-execute.safe.json)

## Verification and provenance

[execution-verification.json](execution-verification.json) records the actual
transaction hashes, Safe transaction hashes and nonces, receipt block identities,
exact payload comparisons and post-execution checks pinned at block **21,409,020**
(`0x4c5bbcdfee681007f3d253de840ced18836a82d1e0c05e92c480ac81dfd5ab22`):

- Both Safe payloads match the corresponding archived file exactly, with successful
  receipts and GOV Safe `ExecutionSuccess` events.
- The scheduling block still had a 600-second delay; the execution block has 86,400 seconds.
- `getMinDelay() == 86400` and `isOperationDone(operationId) == true` at the checked block.
- The action-bearing Timelock history has no remaining Waiting or Ready operation.
  Raising the minimum delay does not change old operations' Ready times; the history
  check therefore also covers older short-delay calls.
- A read-only `eth_call` attempting a new 600-second schedule is refused with
  `TimelockInsufficientDelay(600, 86400)`. No test transaction was sent to the public chain.
- The earlier V4 H/U transaction payloads and successful receipts were rechecked,
  and their Timelock operation remains Done.

[archive.json](archive.json) preserves the original preparation snapshot, including
its then-unsigned status and the scope of evidence available at that time. Those
fields are historical; the separate execution record establishes subsequent chain
execution. [checksums.sha256](checksums.sha256) fixes the Safe file bytes.

Preparation and post-execution evidence originated in the sibling research repository:
`faroo-contracts-reserch/architecture-hardening/governance-delay-20261027/`.
The full baseline scan was pinned at block 21,389,389, with a preparation recheck
through 21,389,688. The execution record extends that baseline through its checked
block using log requests of at most 1,000 blocks and the repository's operation replay.
It retains the single-provider scope: independently authenticated GC-03 coverage
and retrospective release-approval certification are not claimed. Silent empty
operation IDs are outside the action-bearing coverage definition.

The generation policy is
[`governance_delay.py`](../../../tools/release/native/governance_delay.py);
roles and governance behavior are described in
[`GOVERNANCE.md`](../../../docs/GOVERNANCE.md). The archive can be inspected without
running research code. Re-read chain state before relying on mutable parameters.
