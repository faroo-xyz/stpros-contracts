"""FR-D1 approval pins: raw SHA-256 values of reviewed authorization and reconciliation records. Test-only.

This is the only file an activation changes. It is deliberately outside every authorization's source binding
(``fr_d1_scope.SOURCE_BINDING_FILES``): an authorization binds the SHA-256 of the enforcing sources, and the
approval of that authorization is its own SHA-256 here, so the two cannot depend on each other.

An entry is added only by a separate change that applies exactly the hashes Agent A accepted for the exact
records it reviewed. Never add an entry from a collector, a proxy or any of their outputs.
"""

# Owner FR-D1 collection authorizations (S1 / S2, schema ``faroo-fork-collection-authorization-v2``).
COLLECTION = frozenset({
    '6963e58be871b15ff3be92bbd0654cbd784b183fecd1c9050379a6b1a10782c1',  # fr-d1-s1-testnet-sl20261018
    'f4ca9cf3ccb8a5241aefaaa51497171e877f8f7e25f701bd6cdac19f2a4c28cf',  # fr-d1-s2-testnet-sl20261018
    '00c6ab29bf49d545608306a1543b309b555e04e943ad3cd4358018227512b669',  # fr-d1-s1-mainnet-sl20261018
    '71357ba54d74ccaa04aad6acb39dc02cacdda9993effd3fefa2e5618c05efe74',  # fr-d1-s2-mainnet-sl20261018
})
# Owner FR-D1 local real-state Fork authorizations (S3, schema ``faroo-fork-execution-authorization-v2``).
FORK = frozenset({
    '3dd3ab07248c6e74d156d93978d00ea63c5d2cd6767d867f53f610ae560fd96c',  # fr-d1-s3-testnet-sl20261018
    '19442bc39ee398f7e9daa9bc71818bf78019ee196ed239cb07bd05e37a77599f',  # fr-d1-s3-mainnet-sl20261018
})
# Agent A's accepted S1/S2 reconciliation records (schema ``faroo-fork-s1-s2-reconciliation-v1``). An S3 fork
# anchor binds only through one of these.
RECONCILIATIONS = frozenset()
