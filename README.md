# Faroo contracts

The Solidity protocol and historical deployment records are preserved. The
supported toolchain uses Python, the reviewed native solc binary and Foundry.
Solidity dependencies are vendored byte-for-byte; no package installation is needed.

```sh
make build
make toolchain-test
dest=$(tools/verify/export.sh | tail -1)
tools/verify/run.sh "$dest" python3 -m tools.release.native --help
```

Always use the allowlisted export and empty-environment runner. Never invoke
Foundry in this checkout: it automatically discovers environment files.

The CLI prepares unsigned deployment definitions and upgrade plans, verifies
read-only checkpoints, and prepares reviewed LOSS contingencies. It cannot sign,
submit, deploy or execute transactions. Local simulation never authorizes release.

- [Protocol specification](docs/SPEC.md)
- [Upgrade preparation](docs/UPGRADES.md)
- [Governance](docs/GOVERNANCE.md) and [recovery](docs/RECOVERY.md)
- [Toolchain identity and commands](docs/TOOLCHAIN.md)
- [Source map](docs/CODE_MAP.md) and [repository rules](docs/TOOLCHAIN.md#repository-rules)

Historical deployments, snapshots and recorded legacy bytecode remain essential
upgrade inputs. Historical B0 failures and incomplete evidence remain unchanged.
Research and retired tooling live separately in `faroo-contracts-reserch`.
