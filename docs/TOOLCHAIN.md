# Native toolchain

## Build identity

`make build` exports the current safe source tree and runs `tools/ci/canonical.py`
through the empty-environment runner. Python's standard library, reviewed native
solc and vendored Solidity sources are the only build dependencies.

Solidity 0.8.28 (`7893614a`), Cancun, optimizer 200, non-IR, literal-content IPFS
metadata are frozen. The exact compiler binary SHA-256 is checked before execution.
The current pin is platform-specific; other binaries need independent review.

`lib/DEPENDENCIES.json` binds OpenZeppelin 5.6.1 and forge-std 1.9.4 source bytes
and original licenses. Only the required Solidity import closure is vendored.
Effective compiler source names remain `project/contracts/...` and
`npm/@openzeppelin/<package>@5.6.1/...` despite changed physical paths.

All 13 protected targets preserve original ABI/selectors, storage/namespaces,
creation/runtime bytecode, metadata, link/immutable references and source closures.
Only configuration binding migrated to native files. Research preserves the
unchanged original reference and complete before-state artifacts. Never refresh
protected artifact hashes to obtain a pass.

The build writes per-contract input, output, artifact and build-info JSON plus a
manifest under the export's `cache/native-r4/`. Stale local generated files are
never inputs. No Node or package-manager installation is required.

## Commands

Create a fresh export with `tools/verify/export.sh`. Execute every command through
`tools/verify/run.sh EXPORT ...`. Exports copy only safe Git-known native paths,
reject symlinks, and exclude ignored local data and platform metadata.

| Command | Purpose |
| --- | --- |
| `python3 tools/ci/canonical.py` | Exact protected artifact compilation |
| `python3 -m tools.release.native artifact normalize ...` | Unapproved candidate artifact |
| `python3 -m tools.release.native release preflight ...` | Bundle admission, deployment definitions and state verification |
| `release authority` | Reviewed role inventory and completeness boundaries |
| `release p1-runbook` | Mainnet actor-specific preparation and recovery |
| `release build-handoff` | Atomic H Safe calldata |
| `release build-u` | Exact U/U_t, operation IDs and unsigned cancellation calldata |
| `release testnet-handoff` | E1/E2/GOV stages and approved recovery |
| `release estimate-gas` | Local individual estimates; never atomic FS-6 certification |
| `release postcheck` | Fresh identity, authority, version and economic checks |
| `python3 -m tools.release.native loss contingency ...` | Reviewed LOSS alternatives and pinned observations |

Abbreviated `release` rows use the same `python3 -m tools.release.native` prefix.
Use each action's `--help`. Inputs and independent review hashes are explicit.
RPC permits only reviewed public read-only endpoints or explicit local simulations.
Production tooling has no signing, write RPC or transaction-submission surface.

Deployment preparation validates configuration, constructors, candidate artifacts
and immutable-bound runtime. Recorded v4 ProxyAdmin identity is independent of
the current vendored v5 implementation. [UPGRADES](UPGRADES.md) specifies exact
sequences, bundle admission, historical evidence and approval requirements.

## Focused checks

```sh
dest=$(tools/verify/export.sh | tail -1)
tools/verify/run.sh "$dest" python3 tools/ci/canonical.py
tools/verify/run.sh "$dest" python3 -m unittest discover -s test/toolchain -p 'test_*.py' -v
tools/verify/run.sh "$dest" python3 test/release/native/gate.py
tools/verify/run.sh "$dest" python3 -m unittest discover -s test/release/native -p 'test_*.py' -v
tools/verify/run.sh "$dest" python3 test/release/rpc-transport.test.py -v
```

Run Release checks sequentially: historical replay uses one owned synthetic
input directory. Selected original Node/viem goldens retain exact record hashes;
complete historical corpora and retired harnesses are archived in Research.
No framework-dependent reference runner remains in production.

Focused Foundry/OZ tests cover proxy, beacon, migration, authority, custody and
recovery properties. Test-only Anvil rehearsals load recorded legacy bytecode
with synthetic actors; no actual Safe signatures or public-chain operations.
Their local transport is separate from the production read-only RPC client.
`make upgrade-test` runs the focused native/codec/LOSS suite;
`make upgrade-rehearsal` builds canonical artifacts, exports complete local
fixtures and runs six loopback-only Anvil cases. The local actor handoff model
is not a Safe signature or public-chain readiness proof.

Historical snapshot tools remain for reproducibility and `--check` comparisons.
Normal build/test does not refresh snapshots. B0 ABI/storage/bytecode differences
remain FAIL-existing. Missing historical creation, namespace and proxy/beacon
provenance remains INCOMPLETE. Safe-forwarded residual evidence limitations remain.
Real-state forks/FS-1–7, actual Safe/explorer checks, broad fuzz/formal campaigns
and production readiness are not established by local checks.

## Scope

Application Tasks, ordinary views, wallet actions and their unsigned-intent
replacements are retired. The four owner-retired operations have no replacement
CLI. Internal upgrade/LOSS observations remain; Solidity methods and storage are
unchanged. The former framework graph, plugin registrations and privileged routes
are retired together. No execution interface replaces them.

Ignored user-owned research and protected storage are excluded from exports.
Uncertain local material requires a documented provenance decision before cleanup.
