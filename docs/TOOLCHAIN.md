# Canonical R4 tooling

`make build` exports the allowlisted source/dependencies, enters the empty-environment
runner and invokes `tools/ci/canonical.py`. No Hardhat process or Node runtime is
needed for this compilation. `make toolchain-test` runs the focused Python controls
in a separate export. No target deploys, signs or broadcasts.

Install dependencies from the existing pnpm lock before exporting. Native solc must
already be installed at the existing SVM location, or pass `--solc /absolute/path`
inside the runner. The binary SHA-256 and full version must match
`tools/ci/r4-reference.json` **before** compilation. The initial supported binary is
solc 0.8.28+commit.7893614a.Darwin.appleclang (SHA-256
`81515b0e53deaa266d549545ccaac0a5a96e6d4e8201c77f673b2c710976d9ea`).
Other platforms require separately reviewed binary pins; downloads are not automatic.
Both OZ packages must be 5.6.1, and every source in each root's closure is hash-bound.

The reference preserves Cancun, optimizer 200, implicit viaIR=false, literal source
metadata, `bytecodeHash: ipfs`, `project/contracts/...`, versioned npm identities and
existing remapping order. Each root compiles in its own job using the same Python
input builder as historical snapshot tools. Native solc receives the full JSON
closure with `--no-import-callback`. Unexpected configuration, source identity,
closure, dependency, ABI, selector, storage/namespace, bytecode, metadata or immutable
reference drift fails. Never change a setting or refresh a reference just to pass.

Outputs in the **export** at `cache/native-r4/<Contract>/` include `input.json`,
`output.json`, `artifact.json` and `build-info.json`. The manifest binds compiler,
reference, tooling and inputs. Artifacts contain complete creation/runtime templates,
Keccak runtime hashes, ABI/selectors, linear storage, ERC-7201 roots and immutable
references. A failed invocation removes the old success manifest. The reference
records source commit/tree, not the current tooling commit; tooling hashes bind the
actual compiler implementation used. No artifact implies independent release approval.
The existing `tasks/release/artifacts.mjs` accepts the build-info envelope.

The R4 reference is attributed to source commit
`37c43827abf3630c717dbc2fa21cc6e76fcd0dd5`. Nine components' ABI, storage, full runtime,
immutable references and closures were compared with the committed governance closure
build in Research at `64cf64e46ac796dbeb615a518b8e9e42513e9ae0`. Historical evidence
has no creation bytecode or complete namespace artifact for those jobs, and no
standalone current OZ v5 proxy/beacon root artifacts. These historical comparisons
remain **INCOMPLETE**. Their exact pre-change build fingerprints are frozen for
forward regression and independent review; they are not retrospectively approved.

Do not equate the current OZ v5 ProxyAdmin with the recorded v4 ProxyAdmin used by
legacy topology/release. Do not equate zero-placeholder Factory/proxy runtime with a
bound deployment. `bind_runtime` accepts complete explicit immutable-ID/address
bindings and checks ranges/placeholders; release identity still uses the reviewed
Vault address and existing release verifier. No address is invented. Foundry test
artifacts have separate source identities and are not canonical production hashes.

`snapshots/` remains immutable B0 evidence. Existing snapshot entry points retain
both default and production profiles and the same output shape. Native R4 reference
checking does not replace B0 checks or refresh historical files.

## Transitional commands

20 obsolete Task names are removed: `oracle:upgrade`, `oracle:upgrade-v2`,
`oracle:upgrade-v3`, `stpros:upgrade`, `stpros:upgrade-v2`, `perms:transfer-owner`,
`perms:transfer-proxy-admin`, `perms:migrate-owners`, `yv:set-phase`, `yv:grant-role`,
`yv:revoke-role`, `yvf:add-counterparty`, `yvf:remove-counterparty`,
`yvf:emergency-cancel`, `yvf:transfer-owner`, `yvf:upgrade-beacon`,
`stpros:set-oracle`, `stpros:set-unbonding-period`, `oracle:set-pool-info`, `example`.
The 19 privileged aliases were already refusing; there are no replacement stubs.

Keep `tasks/deprecatedDeploy.ts`, all nine `deploy/*.ts` stubs and the refusal in
`upgrade:testnet` while the plugin remains installed. All eight `release:*` commands,
LOSS contingency and legacy LOSS calldata, unsigned Factory create/retire/closure,
read-only views, ordinary user writes and OPS round-opening remain. These existing
writes are not permission to execute transactions during development. Explorer
verification still submits to the explorer API and depends on Hardhat build info.

Focused verification after exporting with `--with-hardhat`:

```sh
tools/verify/run.sh "$dest" python3 tools/ci/canonical.py
tools/verify/run.sh "$dest" python3 -m unittest discover -s test/toolchain -p 'test_*.py' -v
tools/verify/run.sh "$dest" FOUNDRY_PROFILE=final forge build contracts/release/ReleaseAssertions.sol
tools/verify/run.sh "$dest" node --test test/tasks/*.test.mjs test/release/*.test.mjs
```

Task tests use genuine native ABIs and mocked HRE reads, and inspect actual Hardhat
help/deploy refusal. They never send transactions. For TypeScript diagnosis compare
like-prepared exports: Hardhat-generated typings and Rocketh artifacts affect the
result. Record missing-preparation errors and existing diagnostics explicitly;
never report an overall PASS from zero diagnostic delta.

## Native trust primitives (implementation awaiting independent review)

The additive typed API in `tools/lib/abi.py` provides parameter and function
encoding/decoding plus strict-mode indexed-event decoding from reviewed JSON ABIs.
`BigInt` and `UNDEFINED` preserve the reference engine's result-type distinctions.
The original static B0 API remains available. These APIs do not attest a contract,
approve an ABI, sign a transaction or replace any registered Task.

The decoder intentionally follows pinned viem admission: for example, it ignores
some noncanonical padding, accepts unaligned dynamic offsets, and trims leading
NUL bytes in decoded strings. Solidity's own decoder can reject or retain data
differently. The local Solidity witnesses distinguish these behaviors explicitly;
do not use the compatibility decoder as proof of canonical Solidity encoding.

`tools/lib/release_json.py` separates compact release canonical JSON, ordinary
wire JSON, raw SHA-256 and the historical filter ID. Existing native/snapshot
pretty JSON is unchanged. Use explicit `BigInt` for integer values that represent
JS BigInt. Parsed JSON numbers use IEEE-754 Number semantics. The checked reader
retains the original helper's optional-hash behavior; only `approved_json` requires
an external raw-file digest. Computing a digest never creates independent approval.

`tools/release/native/rpc.py` is a read-only library and pin primitive. It retains
the release URL/method, batching, retry, timeout and response-correlation policy,
including the reference's acceptance of `result` with `error: null`. It has no
native release CLI, signing provider, deployment executor or unrestricted method
forwarder. Complete checkpoint/history/Safe/LOSS migrations remain separate work.

Permanent reference cases and exact original Node outputs live in
`test/toolchain/equivalence/`. The original 192-case corpus is immutable; additive
width/URL/pin/function/undefined cases have separate pinned manifests. All expected
results came from the committed Node/viem baseline, never the Python candidate.
ABI library diagnostic formatting is retained in reference evidence and compared by
its precise failure category; release-owned failure messages and successful bytes
are compared exactly. Required cases cannot be silently skipped or refreshed.

From an approved export with installed dependencies:

```sh
tools/verify/run.sh "$dest" python3 -m unittest discover -s test/toolchain -p 'test_*.py' -v
tools/verify/run.sh "$dest" FOUNDRY_PROFILE=final forge test --match-path test/toolchain/FoundationWitness.t.sol -vv
tools/verify/run.sh "$dest" python3 test/toolchain/equivalence/run.py compare --out /absolute/safe/new-result.json
```

`verify-reference` uses an export of the original source commit and refuses source
or frozen-output drift. Copy only the explicitly listed harness files into that
reference export; do not copy candidate `tools/` files. The input fixture directory
`/private/tmp/faroo-foundation-inputs` is reserved for sequential test runs using
synthetic data; reference and candidate run sequentially and have separate outputs.
Research records exact capture/reproduction commands and dependency identities.
No command above runs a full release, accesses a public RPC or renews approval.

The new typed Python encoder admits only `0x` followed by whole hexadecimal
bytes. The independently requested admission amendment explicitly excludes odd
nibbles and nonhex encoder inputs. `admission-policy.json` binds only the two
frozen cases to their exact inputs, original outcomes and required native refusal.
The original 358 cases now account for 356 exact outcomes and two admission
deviations. Malformed hex decoding reports `BaseError`; null remains `TypeError`.
No existing Release or governance implementation uses these candidate primitives.

The typed encoder now retains original letter casing in admitted dynamic `bytes`
and fixed `bytesN` values, including nested arrays, tuples and function calldata.
Internal encoding fragments are even-length hex text without a prefix; lengths
and head/tail offsets remain measured in bytes. Only byte-value spelling is
preserved. Generated uint/bool/address/string words and selectors keep their
previous representation; Keccak continues to hash decoded octets.

Both original spelling counterexamples now match exactly. All 360 pre-existing
cases account for 358 exact outcomes and the same two admission deviations.
A separately frozen 102-case viem supplement covers every fixed-byte width,
dynamic-byte boundaries, nested arrays/tuples, functions, Timelock single/batch
payloads, independent decoding and Safe JSON/text hash domains. The complete
462-case result is 460 exact outcomes, two approved admission deviations and
zero unexplained mismatches. These totals are derived from executed cases.

`compare` validates every original and supplemental lock and compares successful
outputs case-sensitively. It never folds case to obtain equivalence. Full discovery
also requires all methods from the original and additive verification plans and
rejects missing tests, skips and expected failures:

```sh
tools/verify/run.sh "$dest" python3 test/toolchain/equivalence/run.py test --out /absolute/safe/new-discovery.json
```

Implementation status: **READY FOR INDEPENDENT IMPLEMENTATION REVIEW**. This is
not independent approval or permission to replace operational Release consumers.
Research retains all preceding blockers and failed logs with exact identities;
its current handoff records the tested files, independent witnesses, negative
controls and unchanged historical limitations. No downstream migration is started.
