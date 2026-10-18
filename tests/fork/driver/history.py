"""Content verification of a Fork-campaign history manifest (schema ``faroo-fork-history-v1``).

Test-only. The manifest has the structure of the native ``faroo-history-v1`` release manifest
(``docs/UPGRADES.md``, "Authenticated history and incremental release checkpoints") and reuses the
B0 plan/chunk schema, but its target set is the campaign's, not ``history_targets(config)`` of a
release configuration: no Timelock or assertion contract exists before a release, and the mainnet
GOV Safe is an additional target. The per-row, raw-hash, filter-id and window rules are the native
ones (``tools/release/native/history.py``); ``test_history.py`` checks the segment/chunk walk
against ``authenticate_history`` differentially.

What this module establishes is **content integrity**: the files hash to the bound values, the
archive tiles every target from one pre-creation ``fromBlock`` to the anchor without gap, overlap or
duplicate, the immutable B0 archive is reused byte-for-byte, the anchor matches the campaign fork
block, and the target set is closed over vault discovery. It does not establish chain provenance:
canonical anchor hashes are not rechecked (no RPC here) and no independent approval of the manifest
exists. ``authenticated`` is therefore always False, and every consumer keeps the history INCOMPLETE.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.lib import release_json as j  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release import logscan  # noqa: E402
from tools.release.native._shared import bigint  # noqa: E402
from tools.release.native.history import validate_rows, full_hash, raw_hash, integer  # noqa: E402
from tools.release.native.views import log_order  # noqa: E402

SCHEMA = 'faroo-fork-history-v1'
CHAINS = {'mainnet': 1672, 'testnet': 688689}
MANIFEST_KEYS = frozenset({'schema', 'chain', 'chainId', 'fromBlock', 'blockNumber', 'blockHash',
                           'inventorySha256', 'targets', 'segments'})
SEGMENT_KEYS = frozenset({'planFile', 'planSha256', 'chunks'})
CHUNK_REF_KEYS = frozenset({'path', 'sha256'})
# The B0 `logscan.py` plan and chunk key sets; supplemental plans/chunks use the same schema.
PLAN_KEYS = frozenset({'addresses', 'addresses_with_code_before_start', 'chain', 'chain_id', 'chunk_windows',
                       'created_utc', 'end', 'end_block_hash', 'end_timestamp', 'filter_id', 'head_at_plan', 'rpc',
                       'start', 'start_basis', 'tool_version', 'vaults', 'window'})
CHUNK_KEYS = frozenset({'chain', 'fetched_utc', 'filter_id', 'from', 'logs', 'to', 'windows'})
_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
_SHA = re.compile(r'0x[0-9a-f]{64}')

# Immutable B0 archive (research `development-history/B0/evidence/inventory/`, archived 2026-10-09).
# Raw SHA-256 of `plan.json` and of the ordered list of raw chunk SHA-256 values
# (`sha256(json.dumps([chunk_sha256, ...]))`, chunks ordered by first block). The per-chunk hashes
# are the ones recorded independently by B3 (`B3/evidence/review-closure/f07-f08/archive-audit.json`,
# archive SHA-256 79deed4d…b3ad). These pin identity only: B3 recorded the archive manifest as a
# candidate for review, not as a release approval.
B0_PREFIX = {
    'mainnet': {'start': 11074115, 'end': 19774476, 'filterId': '87aaae15724f35e6',
                'endBlockHash': '0xd9285ddf3631842a0197fcf0d8fb9b5e66b1f23704f33be6d972faf0bdbdd59f',
                'planSha256': 'a88b8ee49c5e20b754f480fabe79b5ae2a6cef4634741a5fad36b9d2caf6e593',
                'chunkCount': 436,
                'chunkListSha256': 'ced7afcd1faca17efe79fd5b0b161af930af372473b40e57b4fdf5396838ea5d'},
    'testnet': {'start': 23532577, 'end': 31190661, 'filterId': '3602c9d0f99b6311',
                'endBlockHash': '0x6a444a987794de12106afcfb05619b0198837b6cdc55a4d959efefac0bd5f8b4',
                'planSha256': '6c1c649b640fd651797b7ce3e425c5786311812f166de14c25c99004fea9cf00',
                'chunkCount': 383,
                'chunkListSha256': 'ede624d0307bd3879613a24e27a98c15d269d6f4c467c11e17375ed4f88fa5dd'},
}
# Campaign log targets beyond the B0 plan: none. The mainnet GOV Safe (`tools/release/registry.json`, the
# `GOV` identity of `campaign_input.REAL_IDENTITIES`) is observed by state reads at the anchor only (singleton,
# owners, threshold, nonce, modules, guard, fallback handler); its event history (about 8.7M pre-pin blocks) is
# not collected in the streamlined upgrade-readiness scope (owner decision 2026-10-18). WPROS is deliberately
# not a target either (state reads only; see the research design record).
EXTRA_TARGETS = {'mainnet': (), 'testnet': ()}
GOV_SAFE = '0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca'


class HistoryRefused(ValueError):
    """The manifest is malformed, incomplete or inconsistent. ``status`` is REFUSED or INCOMPLETE."""

    def __init__(self, code, detail='', status='REFUSED'):
        self.code, self.status = code, status
        super().__init__(f'{status}: {code}' + (f': {detail}' if detail else ''))


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise HistoryRefused(code, detail, status)


def _int(value):
    return int(value)


def chunk_list_sha256(shas):
    return hashlib.sha256(json.dumps(list(shas)).encode()).hexdigest()


def registry(chain):
    return json.loads((ROOT / 'tools/release/registry.json').read_text())[chain]


def _load(root, relative, expected_sha256, code):
    """Reads a JSON file below `root` through the native reviewed-file reader (raw SHA-256, no
    symlink, no hidden or secret-looking component)."""
    _require(isinstance(relative, str) and relative and not relative.startswith('/')
             and '..' not in relative.split('/') and '\\' not in relative, code, 'path must be relative to the history root')
    _require(raw_hash(expected_sha256), code, 'raw SHA-256 required')
    try:
        return j.checked_json(str(Path(root).resolve() / relative), expected_sha256)
    except j.ReleaseError as error:
        raise HistoryRefused(code, str(error)) from None
    except (OSError, ValueError) as error:
        raise HistoryRefused(code, f'{relative}: {error}') from None


def _rows(rows, addresses, from_block, to_block, where):
    try:
        validate_rows(rows, addresses, from_block, to_block)
    except j.ReleaseError as error:
        raise HistoryRefused(error.code, where) from None
    except (KeyError, TypeError, ValueError) as error:
        raise HistoryRefused('HISTORY_LOG_IDENTITY', f'{where}: {error!r}') from None


def verify_archive(manifest, root, chain):
    """Structural and coverage verification of a parsed manifest for its *declared* targets.
    Returns the sorted rows and the segment facts. Raises HistoryRefused on any defect."""
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS, 'HISTORY_MANIFEST_KEYS')
    _require(manifest['schema'] == SCHEMA, 'HISTORY_SCHEMA')
    _require(chain in CHAINS and manifest['chain'] == chain and manifest['chainId'] == CHAINS[chain], 'HISTORY_CHAIN')
    _require(integer(manifest['fromBlock']) and integer(manifest['blockNumber'])
             and 0 < manifest['fromBlock'] <= manifest['blockNumber'], 'HISTORY_RANGE')
    _require(full_hash(manifest['blockHash']), 'HISTORY_ANCHOR_HASH')
    _require(isinstance(manifest['inventorySha256'], str) and _SHA.fullmatch(manifest['inventorySha256']) is not None,
             'HISTORY_BINDING', 'inventorySha256 must be a 0x-prefixed SHA-256')
    from_block, anchor = _int(manifest['fromBlock']), _int(manifest['blockNumber'])
    targets = manifest['targets']
    _require(isinstance(targets, list) and targets
             and all(isinstance(t, str) and _ADDRESS.fullmatch(t) and int(t, 16) for t in targets)
             and targets == sorted(set(targets)), 'HISTORY_TARGETS', 'sorted, unique, lowercase, nonzero')
    segments = manifest['segments']
    _require(isinstance(segments, list) and segments, 'HISTORY_TARGETS', 'no segment')
    coverage = {t: [] for t in targets}
    rows, anchors, facts = [], {}, []
    for segment in segments:
        _require(isinstance(segment, dict) and set(segment) == SEGMENT_KEYS
                 and isinstance(segment['chunks'], list) and segment['chunks'], 'HISTORY_SEGMENT_KEYS')
        plan = _load(root, segment['planFile'], segment['planSha256'], 'HISTORY_PLAN_HASH_REQUIRED')
        _require(isinstance(plan, dict) and set(plan) == PLAN_KEYS, 'HISTORY_PLAN_KEYS')
        _require(plan['chain'] == chain and plan['chain_id'] == CHAINS[chain] and plan['rpc'] == PUBLIC_RPC[chain],
                 'HISTORY_PLAN_RANGE', 'chain identity')
        _require(plan['window'] == 1000 and integer(plan['chunk_windows']) and plan['chunk_windows'] >= 1
                 and integer(plan['start']) and integer(plan['end'])
                 and from_block <= plan['start'] <= plan['end'] <= anchor, 'HISTORY_PLAN_RANGE')
        start, end = _int(plan['start']), _int(plan['end'])
        _require(isinstance(plan['addresses'], list)
                 and all(isinstance(a, dict) and isinstance(a.get('address'), str) for a in plan['addresses']),
                 'HISTORY_PLAN_TARGETS')
        addresses = [a['address'] for a in plan['addresses']]
        _require(addresses and len(set(addresses)) == len(addresses) and all(_ADDRESS.fullmatch(a) for a in addresses)
                 and full_hash(plan['end_block_hash']), 'HISTORY_PLAN_TARGETS')
        _require(set(addresses) <= set(targets), 'HISTORY_PLAN_TARGETS', 'plan address outside the target set')
        _require(plan['filter_id'] == j.historical_filter_id(addresses), 'HISTORY_FILTER')
        _require(isinstance(plan['addresses_with_code_before_start'], list), 'HISTORY_PLAN_KEYS')
        if start == from_block:
            # Pre-creation evidence: no target of a segment starting at fromBlock had code at fromBlock-1.
            _require(plan['addresses_with_code_before_start'] == [], 'HISTORY_START_HAS_CODE', segment['planFile'],
                     'INCOMPLETE')
        _require(anchors.get(end, plan['end_block_hash']) == plan['end_block_hash'], 'HISTORY_ANCHOR_CONFLICT')
        anchors[end] = plan['end_block_hash']
        if end == anchor:
            _require(plan['end_block_hash'] == manifest['blockHash'], 'HISTORY_ANCHOR_MISMATCH', segment['planFile'])
        for target in addresses:
            coverage[target].append((start, end))
        chunk_shas, next_block = [], start
        for ref in segment['chunks']:
            _require(isinstance(ref, dict) and set(ref) == CHUNK_REF_KEYS, 'HISTORY_SEGMENT_KEYS')
            chunk = _load(root, ref['path'], ref['sha256'], 'HISTORY_CHUNK_HASH_REQUIRED')
            _require(isinstance(chunk, dict) and set(chunk) == CHUNK_KEYS, 'HISTORY_CHUNK_KEYS', ref['path'])
            _require(chunk['chain'] == plan['chain'] and chunk['filter_id'] == plan['filter_id']
                     and integer(chunk['from']) and chunk['from'] == next_block
                     and integer(chunk['to']) and next_block <= chunk['to'] <= end, 'HISTORY_CHUNK_GAP', ref['path'])
            _rows(chunk['logs'], addresses, chunk['from'], chunk['to'], ref['path'])
            window = chunk['from']
            _require(isinstance(chunk['windows'], list), 'HISTORY_WINDOW_GAP')
            for item in chunk['windows']:
                _require(isinstance(item, list) and len(item) == 3, 'HISTORY_WINDOW_GAP')
                w_start, w_end, count = item
                _require(w_start == window and integer(w_end) and w_end >= w_start and w_end - w_start < 1000
                         and w_end <= chunk['to'] and integer(count), 'HISTORY_WINDOW_GAP', ref['path'])
                _require(sum(w_start <= _int(bigint(r['blockNumber'])) <= w_end for r in chunk['logs']) == count,
                         'HISTORY_WINDOW_COUNT', ref['path'])
                window = w_end + 1
            _require(window == chunk['to'] + 1, 'HISTORY_WINDOW_GAP', ref['path'])
            rows.extend(chunk['logs'])
            chunk_shas.append(ref['sha256'])
            next_block = _int(chunk['to']) + 1
        _require(next_block == end + 1, 'HISTORY_CHUNK_GAP', segment['planFile'])
        facts.append({'planFile': segment['planFile'], 'planSha256': segment['planSha256'], 'start': start, 'end': end,
                      'filterId': plan['filter_id'], 'addresses': sorted(addresses),
                      'endTimestamp': plan['end_timestamp'], 'chunkCount': len(chunk_shas),
                      'chunkListSha256': chunk_list_sha256(chunk_shas)})
    for target, ranges in coverage.items():
        next_block = from_block
        for start, end in sorted(ranges):
            _require(start == next_block, 'HISTORY_TARGET_GAP_OR_OVERLAP', target, 'INCOMPLETE' if start > next_block else 'REFUSED')
            next_block = end + 1
        _require(next_block == anchor + 1, 'HISTORY_TARGET_GAP_OR_OVERLAP', target, 'INCOMPLETE')
    # Native per-row rule over the union: identity, range and duplicate (block, logIndex).
    _rows(rows, targets, from_block, anchor, 'across segments')
    rows = sorted(rows, key=log_order)
    block_hashes = {}
    for row in rows:
        number = _int(bigint(row['blockNumber']))
        _require(block_hashes.setdefault(number, row['blockHash']) == row['blockHash']
                 and anchors.get(number, row['blockHash']) == row['blockHash'], 'HISTORY_BLOCK_HASH_CONFLICT', str(number))
    return {'chain': chain, 'fromBlock': from_block, 'blockNumber': anchor, 'blockHash': manifest['blockHash'],
            'inventorySha256': manifest['inventorySha256'], 'targets': list(targets), 'rows': rows, 'segments': facts}


def required_targets(chain, b0_plan_addresses, rows):
    """B0 plan targets, the campaign's extra targets and every vault created by a registry factory
    in the verified rows (the set must be closed over discovery)."""
    factories = set(registry(chain)['factories'])
    created = {'0x' + r['topics'][1][-40:] for r in rows
               if r['topics'] and r['topics'][0] == logscan.T['YieldVaultCreated'] and r['address'] in factories
               and len(r['topics']) >= 2}
    return sorted(set(b0_plan_addresses) | set(EXTRA_TARGETS[chain]) | created)


def verify_manifest(manifest, root, chain, prefix=None):
    """verify_archive plus the campaign rules: the B0 archive is reused byte-for-byte as the
    prefix of every B0 target, and the target set equals `required_targets`."""
    prefix = B0_PREFIX[chain] if prefix is None else prefix
    result = verify_archive(manifest, root, chain)
    b0 = [s for s in result['segments'] if s['planSha256'] == prefix['planSha256']]
    shaped = [s for s in result['segments'] if (s['start'], s['end'], s['filterId'])
              == (prefix['start'], prefix['end'], prefix['filterId'])]
    _require(len(shaped) <= 1 and b0 == shaped, 'B0_PREFIX_MODIFIED', 'a B0-shaped segment differs from the pinned archive')
    _require(len(b0) == 1, 'B0_PREFIX_REQUIRED', 'the immutable B0 archive must be the historical prefix', 'INCOMPLETE')
    seg = b0[0]
    _require(seg['chunkCount'] == prefix['chunkCount'] and seg['chunkListSha256'] == prefix['chunkListSha256'],
             'B0_PREFIX_MODIFIED', 'chunk list differs from the pinned archive')
    _require(result['blockNumber'] >= prefix['end'], 'HISTORY_RANGE', 'anchor before the B0 pin')
    if result['blockNumber'] == prefix['end']:
        _require(result['blockHash'] == prefix['endBlockHash'], 'HISTORY_ANCHOR_MISMATCH', 'B0 pin hash')
    required = required_targets(chain, seg['addresses'], result['rows'])
    missing = sorted(set(required) - set(result['targets']))
    _require(not missing, 'HISTORY_TARGET_MISSING', ', '.join(missing), 'INCOMPLETE')
    unexpected = sorted(set(result['targets']) - set(required))
    _require(not unexpected, 'HISTORY_TARGET_UNEXPECTED', ', '.join(unexpected))
    result['b0Prefix'] = {'planSha256': seg['planSha256'], 'chunkListSha256': seg['chunkListSha256'],
                          'start': seg['start'], 'end': seg['end']}
    result['provenance'] = provenance()
    return result


def provenance():
    """Fixed statement: nothing in this module upgrades content integrity to chain provenance."""
    return {'contentIntegrity': 'VERIFIED', 'canonicalAnchorRecheck': 'NOT RUN',
            'independentApproval': 'ABSENT', 'authenticated': False,
            'statement': 'content integrity only; not independently authenticated chain provenance'}


def authenticate(relative_path, manifest_sha256, root, *, chain, anchor_block, anchor_hash, inventory_sha256,
                 prefix=None):
    """Loads `root/relative_path` bound by its raw SHA-256 and verifies it against the campaign's
    fork identity and inventory binding. `inventory_sha256` and `anchor_hash` are '0x…' values."""
    manifest = _load(root, relative_path, manifest_sha256, 'HISTORY_PROOF_REQUIRED')
    result = verify_manifest(manifest, root, chain, prefix)
    _require(result['blockNumber'] == anchor_block and result['blockHash'] == anchor_hash, 'HISTORY_BINDING',
             'anchor differs from the campaign fork block')
    _require(result['inventorySha256'] == inventory_sha256, 'HISTORY_BINDING', 'inventory SHA-256 differs')
    result['manifestSha256'] = manifest_sha256
    return result
