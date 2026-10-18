"""Strict validator for Fork-campaign inputs (schema ``faroo-fork-campaign-input-v1``).

Test-only. The validated file is the exact file the Solidity loader reads, bound by its SHA-256.
Optional values are explicit zero sentinels, never omitted keys or JSON null, so every key set is
exact. Nothing here reads a secret, opens a network connection or derives an expected value from
the contracts under test.
"""
import hashlib
import json
from pathlib import Path
import re

import history

SCHEMA = 'faroo-fork-campaign-input-v1'
TOP_KEYS = frozenset({'schema', 'campaign', 'chain', 'chainId', 'evidenceClass', 'fork', 'source',
                      'inventory', 'actors', 'safe', 'decisions'})
SOURCE_KEYS = frozenset({'productionHead', 'exportManifestSha256', 'r4ReferenceSha256',
                         'nativeR4ManifestSha256'})
INVENTORY_KEYS = frozenset({'path', 'sha256', 'anchorBlock', 'complete', 'historyManifestSha256'})
FORK_KEYS = frozenset({'block', 'blockHash'})
ACTOR_KEYS = frozenset({'address', 'kind', 'label'})
SAFE_KEYS = frozenset({'mode', 'identityManifestSha256'})
SAFE_MODES = ('ABSENT', 'SYNTHETIC', 'APPROVED_MANIFEST')
# Mirror of the Solidity `SafeTrustAnchor.APPROVED_IDENTITY_MANIFEST_SHA256`: SHA-256 values of
# independently approved Safe identity manifests, pinned in reviewed source. Empty: no approved
# manifest exists (FR-X8), so a real-state Safe path is BLOCKED. Never add an entry derived from
# observed Fork code or from a caller-supplied review reference.
APPROVED_SAFE_IDENTITY_MANIFESTS = frozenset()
CHAINS = {'mainnet': (1672, 'M'), 'testnet': (688689, 'T')}
EVIDENCE_CLASSES = ('SYNTHETIC', 'E_HIST', 'E_FRESH')
ACTOR_KINDS = ('REAL', 'LABELLED_SYNTHETIC')
REQUIRED_ACTORS = {
    'mainnet': ('GOV', 'CAN', 'GRD', 'OPS', 'TREASURY', 'REPORTER', 'KEEPER', 'E1', 'E2'),
    'testnet': ('GOV_T', 'CAN_T', 'GRD_T', 'OPS_T', 'REPORTER', 'KEEPER', 'E1', 'E2'),
}
# Reviewed public identities (tools/release/registry.json). A REAL actor in one of these roles
# must be exactly this address; E1/E2 must be REAL in any Fork evidence class.
REAL_IDENTITIES = {
    'mainnet': {'E1': '0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd',
                'E2': '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1',
                'GOV': '0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca'},
    'testnet': {'E1': '0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd',
                'E2': '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1'},
}
# Owner/A decisions that gate acceptance (BLOCKERS-AND-DECISIONS §5). UNDECIDED is explicit.
DECISIONS = {
    'FR-D2': ('UNDECIDED', 'a', 'b', 'c'),
    'FR-D3': ('UNDECIDED', 'a', 'b', 'c'),
    'FR-D4': ('UNDECIDED', 'a', 'b', 'c'),
    'FR-D5': ('UNDECIDED', 'a', 'b'),
    'FR-D6': ('UNDECIDED', 'a', 'b', 'c'),
    'FR-D7': ('UNDECIDED', 'keep', 'waived'),
    'FR-D8': ('UNDECIDED', 'a', 'b'),
}
INPUT_ROOT = 'cache_forge/rehearsal/fork-campaign/'
HISTORY_CONTENT_ONLY = ('history manifest content verified (faroo-fork-history-v1); chain provenance not '
                        'authenticated: canonical anchor recheck NOT RUN, no independent approval')
# IVH-D01 / FH-D11: v1 inputs cannot bind a complete state observation (faroo-fork-state-observation-v1), so every
# E_FRESH input stays INCOMPLETE until a separately reviewed input version binds and verifies one.
STATE_OBSERVATION_BLOCKER = 'state observation (IVH-D01/FH-D11) not bound or not verified'
ZERO_HASH = '0x' + '0' * 64
ZERO_ADDRESS = '0x' + '0' * 40
_ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
_HASH = re.compile(r'0x[0-9a-f]{64}')
_LABEL = re.compile(r'[A-Za-z0-9 ._:/()+-]{1,96}')


class InputRefused(ValueError):
    """The input is malformed or lacks a required evidence element."""


def _require(condition, message):
    if not condition:
        raise InputRefused(message)


def _keys(obj, expected, where):
    _require(isinstance(obj, dict), f'{where} must be an object')
    _require(set(obj) == expected, f'unexpected key set at {where}: '
             f'missing {sorted(expected - set(obj))}, unknown {sorted(set(obj) - expected)}')


def _int(value, where):
    # bool is an int subclass in Python; reject it explicitly.
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, f'{where} must be a non-negative integer')
    return value


def _address(value, where, allow_zero=False):
    _require(isinstance(value, str) and _ADDRESS.fullmatch(value) is not None, f'{where} must be a 20-byte hex address')
    _require(allow_zero or int(value, 16) != 0, f'{where} must not be the zero address')
    return value.lower()


def _hash(value, where):
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None, f'{where} must be a lowercase 32-byte hex value')
    return value


def load_pins(root):
    pins = json.loads((Path(root) / 'snapshots/deployed/pins.json').read_text())
    return {'mainnet': pins['mainnet'], 'testnet': pins['testnet']}


def validate(doc, pins):
    """Validates a parsed input. Returns the list of acceptance blockers that do not make the
    input malformed but must be reported (BLOCKED / INCOMPLETE), for example an undecided
    estimator or an absent Safe identity."""
    _keys(doc, TOP_KEYS, '$')
    _require(doc['schema'] == SCHEMA, 'schema')
    _require(doc['chain'] in CHAINS, 'chain')
    chain = doc['chain']
    _require(doc['chainId'] == CHAINS[chain][0] and doc['campaign'] == CHAINS[chain][1],
             'chain, chainId and campaign disagree')
    _require(doc['evidenceClass'] in EVIDENCE_CLASSES, 'evidenceClass')
    cls = doc['evidenceClass']
    blockers = []

    _keys(doc['source'], SOURCE_KEYS, '.source')
    _require(isinstance(doc['source']['productionHead'], str)
             and re.fullmatch(r'[0-9a-f]{40}', doc['source']['productionHead']) is not None, 'productionHead')
    for key in ('exportManifestSha256', 'r4ReferenceSha256', 'nativeR4ManifestSha256'):
        _hash(doc['source'][key], f'.source.{key}')

    _keys(doc['fork'], FORK_KEYS, '.fork')
    block = _int(doc['fork']['block'], '.fork.block')
    block_hash = _hash(doc['fork']['blockHash'], '.fork.blockHash')
    inv = doc['inventory']
    _keys(inv, INVENTORY_KEYS, '.inventory')
    path = inv['path']
    _require(isinstance(path, str) and path and not path.startswith('/') and '..' not in path.split('/')
             and '\\' not in path, '.inventory.path must be relative to the rehearsal cache')
    _hash(inv['sha256'], '.inventory.sha256')
    anchor = _int(inv['anchorBlock'], '.inventory.anchorBlock')
    _require(isinstance(inv['complete'], bool), '.inventory.complete must be boolean')
    history_sha = _hash(inv['historyManifestSha256'], '.inventory.historyManifestSha256')
    if cls == 'SYNTHETIC':
        _require(block == 0 and block_hash == ZERO_HASH, 'SYNTHETIC input must not claim a fork block')
    else:
        _require(block_hash != ZERO_HASH, 'fork block hash missing')
        _require(anchor == block, 'inventory anchor differs from fork block')
        _require(inv['complete'], 'INCOMPLETE: inventory history not complete to the fork block')
        if cls == 'E_HIST':
            _require(block == pins[chain], 'E-hist must fork at the B0 pin')
        else:
            _require(block > pins[chain], 'E-fresh must fork after the B0 pin')
            _require(history_sha != ZERO_HASH, 'INCOMPLETE: E-fresh requires an authenticated history manifest')

    actors = doc['actors']
    _require(isinstance(actors, dict), '.actors must be an object')
    _require(set(actors) == set(REQUIRED_ACTORS[chain]),
             f'actor mapping must be exactly {sorted(REQUIRED_ACTORS[chain])}')
    seen = {}
    for role, actor in sorted(actors.items()):
        _keys(actor, ACTOR_KEYS, f'.actors.{role}')
        address = _address(actor['address'], f'.actors.{role}.address')
        _require(actor['kind'] in ACTOR_KINDS, f'.actors.{role}.kind')
        _require(isinstance(actor['label'], str) and _LABEL.fullmatch(actor['label']) is not None,
                 f'.actors.{role}.label')
        _require(address not in seen, f'actors must be distinct ({role} and {seen.get(address)})')
        seen[address] = role
        real = REAL_IDENTITIES[chain].get(role)
        if actor['kind'] == 'REAL':
            _require(real is None or address == real, f'REAL {role} differs from the reviewed identity')
        if cls != 'SYNTHETIC' and role in ('E1', 'E2'):
            _require(actor['kind'] == 'REAL', f'{role} must be the real identity in Fork evidence')
        if actor['kind'] == 'LABELLED_SYNTHETIC':
            blockers.append(('INCOMPLETE', f'{role} is a labelled synthetic actor (FR-D5/FR-D6)'))

    safe = doc['safe']
    _keys(safe, SAFE_KEYS, '.safe')
    _require(safe['mode'] in SAFE_MODES, '.safe.mode')
    manifest = _hash(safe['identityManifestSha256'], '.safe.identityManifestSha256')
    if safe['mode'] == 'APPROVED_MANIFEST':
        # A caller cannot attest a Safe: only a pinned, independently approved manifest counts.
        _require(manifest in APPROVED_SAFE_IDENTITY_MANIFESTS,
                 'BLOCKED: Safe identity manifest is not independently approved')
    else:
        _require(manifest == ZERO_HASH, 'Safe identity manifest given without APPROVED_MANIFEST mode')
        if safe['mode'] == 'SYNTHETIC':
            _require(cls == 'SYNTHETIC', 'a synthetic Safe is only valid for SYNTHETIC evidence')
            blockers.append(('INCOMPLETE', 'Safe path uses synthetic models, not real Safe bytecode'))
        else:
            blockers.append(('BLOCKED', 'Safe path: no independently approved Safe identity manifest (FR-X8)'))

    decisions = doc['decisions']
    _keys(decisions, frozenset(DECISIONS), '.decisions')
    for key, allowed in DECISIONS.items():
        _require(decisions[key] in allowed, f'.decisions.{key}')
        if decisions[key] == 'UNDECIDED':
            blockers.append(('BLOCKED' if key in ('FR-D2', 'FR-D3') else 'INCOMPLETE', f'{key} undecided'))
    if cls != 'E_FRESH':
        blockers.append(('INCOMPLETE', f'evidence class {cls} cannot close an FS row'))
    else:
        # A declared hash and `complete: true` are not provenance (review finding 2).
        blockers.append(('INCOMPLETE', 'authenticated history not verified: the native history verifier did not run'))
        blockers.append(('INCOMPLETE', STATE_OBSERVATION_BLOCKER))
        blockers.append(('INCOMPLETE', 'final FS acceptance gate not implemented (fail-closed)'))
    return blockers


def sha256_file(path):
    """SHA-256 as a 0x-prefixed lowercase hex string (the schema's hash format)."""
    return '0x' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bind_to_export(doc, export_root):
    """Checks the source bindings against the files of the export it will run in. Returns the
    content-verified history (or None when no history manifest is declared or required)."""
    root = Path(export_root)
    manifest_path = root / 'EXPORT-MANIFEST.txt'
    _require(manifest_path.is_file(), 'not an approved export (no EXPORT-MANIFEST.txt)')
    manifest = json.loads(manifest_path.read_text())
    src = doc['source']
    _require(manifest['sourceHead'] == src['productionHead'], 'export source head differs from the input')
    _require(sha256_file(manifest_path) == src['exportManifestSha256'], 'export manifest SHA-256 mismatch')
    _require(sha256_file(root / 'tools/ci/r4-reference.json') == src['r4ReferenceSha256'], 'r4-reference SHA-256 mismatch')
    native = root / 'cache/native-r4/manifest.json'
    _require(native.is_file(), 'BLOCKED: canonical artifacts missing; run tools/ci/canonical.py in the export first')
    _require(sha256_file(native) == src['nativeR4ManifestSha256'], 'native R4 manifest SHA-256 mismatch')
    native_doc = json.loads(native.read_text())
    _require('0x' + native_doc['referenceSha256'] == src['r4ReferenceSha256'],
             'canonical artifacts built from another reference')
    inventory = root / INPUT_ROOT / doc['inventory']['path']
    _require(inventory.is_file(), 'INCOMPLETE: inventory file missing')
    _require(sha256_file(inventory) == doc['inventory']['sha256'], 'inventory SHA-256 mismatch')
    declared = doc['inventory']['historyManifestSha256']
    if doc['evidenceClass'] == 'E_FRESH' or (doc['evidenceClass'] == 'E_HIST' and declared != ZERO_HASH):
        manifest = root / INPUT_ROOT / 'history' / (declared + '.json')
        _require(manifest.is_file(), 'INCOMPLETE: history manifest file missing for the declared SHA-256')
        _require(sha256_file(manifest) == declared, 'history manifest SHA-256 mismatch')
        # Content verification against the fork identity and the inventory binding. A verified
        # manifest is still not chain provenance (`history.provenance()`).
        try:
            return history.authenticate(manifest.name, declared[2:], manifest.parent, chain=doc['chain'],
                                        anchor_block=doc['fork']['block'], anchor_hash=doc['fork']['blockHash'],
                                        inventory_sha256=doc['inventory']['sha256'])
        except history.HistoryRefused as error:
            raise InputRefused(f'history manifest: {error}') from None
    return None


def load(path, export_root):
    """Reads, validates and binds an input file. Returns (document, sha256 '0x…', blockers)."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InputRefused(f'not JSON: {error}') from None
    blockers = validate(doc, load_pins(export_root))
    if bind_to_export(doc, export_root) is not None:
        blockers.append(('INCOMPLETE', HISTORY_CONTENT_ONLY))
    return doc, '0x' + hashlib.sha256(raw).hexdigest(), blockers
