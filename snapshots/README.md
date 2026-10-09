# Snapshots (B0 baselines)

Structural baselines (evidence class ST) and deployed-state facts (pinned
blocks). Every file is produced by a tool and checked by the same tool with
`--check`, which regenerates in memory and fails on any difference.
`tools/verify/check_all.sh` runs all checks.

| Path | Content | Tool |
|---|---|---|
| `abi/<Contract>.json` | HEAD functions (selector), events (topic0), errors (selector) | `tools/snapshot/abi.py` |
| `storage/<Contract>.json` | HEAD linear slots, struct member layouts, ERC-7201 namespace roots (computed independently and matched to the source constant) | `tools/snapshot/storage.py` |
| `bytecode.json` | HEAD runtime/initcode size, EIP-170/EIP-3860 headroom and code hashes for Hardhat's `default` and `production` profiles; which recorded deployments are byte-identical to a HEAD build | `tools/snapshot/bytecode.py` |
| `capabilities.json` | HEAD externally callable functions with modifiers and a derived authority class; unauthenticated state-changing entry points; structural opcode scan and upgrade-path selectors for HEAD builds and the live recorded implementations | `tools/snapshot/capabilities.py` |
| `gas.json` | In-test gas baselines (±5% band) for HEAD and recorded V1 user operations | `tools/snapshot/gas.py` |
| `deployed/pins.json` | Pinned blocks used by every deployed-state artifact | written once per baseline |
| `deployed/<chain>/state.json` | Proxies, implementations, admins, versions, owners, beacons, vaults, StPROS accounting and PRE-4 relations, Safe configuration at the pinned block | `tools/snapshot/deployed.py` |
| `deployed/<chain>/abi/`, `deployed/<chain>/storage/` | Recorded ABI and storage layout of the live implementations | `abi.py`, `storage.py` |
| `deployed/provenance.json` | Record ⇄ recompiled bytecode ⇄ on-chain code ⇄ git blob identity of every deployment record, and the implementation history of every proxy and beacon | `tools/release/provenance.py --recompile --onchain --history` |

The HEAD build used by `abi.py`, `storage.py`, `bytecode.py` and
`capabilities.py` (`tools/snapshot/build.py`) reproduces Hardhat's
standard-JSON input (source keys, remappings, metadata settings); `bytecode.json`
records the deployments it reproduces byte for byte.

Top-level `*.json` files other than the ones above are Foundry snapshot-cheatcode
output and are ignored by git.
