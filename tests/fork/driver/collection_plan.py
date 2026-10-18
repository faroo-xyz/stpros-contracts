"""Pure planning for a Fork-campaign history collection (spec ``faroo-fork-collection-spec-v1``). Test-only.

The spec is the reviewed input of both collections (B's and C's): chain, the common ``fromBlock``, the B0
pin, the anchor ``B`` and its hash, the header scope and the A' segment layout (CR-D6):

- ``S1`` post-pin ``[pin + 1, B]`` over every B0 target plus the campaign's extra targets (mainnet GOV Safe);
- ``S2`` Safe-only pre-pin ``[fromBlock, pin]`` (mainnet), which also re-reads the pin hash as its plan end;
- ``S3`` / ``S4`` for vaults created after the pin: ``[fromBlock, pin]`` and ``[pin + 1, B]`` over them.

Every derived value is a function of the spec inputs, and ``check_spec`` re-derives the whole document, so
an edited spec is refused. Labels follow one rule (B0 labels, ``safe:GOV``, ``vault:<factory>[<index>]``) and
a new vault must be ``CREATE(factory, 3 + index)``. ``budget`` restates the cost-document formulas without I/O.
Nothing here reads the network, the environment or a secret.
"""
import hashlib
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history as h  # noqa: E402
from history import HistoryRefused  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.lib.keccak import keccak256_bytes  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402

logscan = h.logscan
SPEC_SCHEMA = 'faroo-fork-collection-spec-v1'
TOOL_VERSION = 'faroo-fork-collector-1'
CHUNK_WINDOWS = 20
WINDOW = 1000
# FH-D5 (a): an unverified offline planning parameter, never a finality guarantee.
FINALITY_DEPTH_ASSUMPTION = 120
# CR-D1: H1 is the reference scope. A weaker scope needs an independently recorded approval, pinned here by a
# separate reviewed change; none exists.
HEADER_SCOPES = ('H1', 'H2', 'H3')
APPROVED_HEADER_SCOPES = frozenset({'H1'})
SAFE_LABEL = 'safe:GOV'
VAULT_NONCE_OFFSET = 3       # every recorded vault is CREATE(factory, 3 + index); every beacon CREATE(factory, 2)
BEACON_NONCE = 2
SPEC_KEYS = frozenset({'schema', 'chain', 'chainId', 'rpc', 'fromBlock', 'pin', 'anchor', 'finalityDepth',
                       'headerScope', 'b0EventBlockRecheck', 'b0', 'extraTargets', 'newVaults', 'segments',
                       'balance', 'toolVersion'})
SEGMENT_KEYS = frozenset({'id', 'kind', 'start', 'end', 'chunkWindows', 'addresses', 'vaults', 'startBasis',
                          'checkpoint'})
CHECKPOINT_KEYS = frozenset({'planFile', 'planSha256', 'chunks', 'headersFile', 'headersSha256'})
# CR-D6 Strategy C: immutable segments approved for reuse, by plan SHA-256, pinned here by a separate reviewed
# change (the approval record of an earlier collection). Empty: no checkpoint is approved, so every segment is
# collected. Never add an entry from a collector or from its own output.
APPROVED_CHECKPOINTS = {}


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise HistoryRefused(code, detail, status)


def canonical_bytes(doc):
    """The collector's file encoding (the B0 logscan ``atomic_write`` encoding)."""
    return json.dumps(doc, separators=(',', ':'), sort_keys=True).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _rlp_int(n):
    if n == 0:
        return b'\x80'
    raw = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return raw if len(raw) == 1 and raw[0] < 0x80 else bytes([0x80 + len(raw)]) + raw


def create_address(deployer, nonce):
    """``CREATE`` address of ``deployer`` at ``nonce`` (RLP([address, nonce]), keccak, low 20 bytes)."""
    payload = bytes([0x94]) + bytes.fromhex(deployer[2:]) + _rlp_int(nonce)
    return '0x' + keccak256_bytes(bytes([0xc0 + len(payload)]) + payload)[-20:].hex()


def vault_label(factory, index):
    return f'vault:{factory}[{index}]'


def b0_targets(b0_plan):
    """The B0 plan's addresses with their recorded label sets."""
    _require(isinstance(b0_plan, dict) and set(b0_plan) == h.PLAN_KEYS, 'SPEC_B0_PLAN', 'B0 plan keys')
    out = []
    for entry in b0_plan['addresses']:
        _require(isinstance(entry, dict) and set(entry) == {'address', 'labels'}
                 and isinstance(entry['labels'], list) and entry['labels'], 'SPEC_B0_PLAN', 'B0 address entry')
        out.append({'address': entry['address'], 'labels': sorted(entry['labels'])})
    return sorted(out, key=lambda e: e['address'])


def b0_vault_counts(b0_plan, chain):
    counts = {f: 0 for f in h.registry(chain)['factories']}
    for v in b0_plan['vaults']:
        counts[v['factory']] = max(counts[v['factory']], v['index'] + 1)
    return counts


def _segment(seg_id, kind, start, end, addresses, vaults, basis):
    return {'id': seg_id, 'kind': kind, 'start': start, 'end': end, 'chunkWindows': CHUNK_WINDOWS,
            'addresses': sorted(addresses, key=lambda e: e['address']), 'vaults': vaults, 'startBasis': basis,
            'checkpoint': None}


def _checkpoint(chain, seg, ref):
    """An approved immutable segment for reuse: its plan, chunk list and header sidecar SHA-256 values must be
    exactly the pinned approval, for exactly this segment's range and filter."""
    _require(isinstance(ref, dict) and set(ref) == CHECKPOINT_KEYS and isinstance(ref['chunks'], list) and ref['chunks']
             and all(isinstance(c, dict) and set(c) == {'path', 'sha256'} for c in ref['chunks']), 'SPEC_CHECKPOINT',
             seg['id'])
    approved = APPROVED_CHECKPOINTS.get(ref['planSha256'])
    filter_id = j.historical_filter_id([e['address'] for e in seg['addresses']])
    _require(approved is not None, 'CHECKPOINT_NOT_APPROVED', f'{seg["id"]}: plan {ref["planSha256"][:12]} is not pinned')
    _require(approved == {'chain': chain, 'start': seg['start'], 'end': seg['end'], 'filterId': filter_id,
                          'chunkListSha256': h.chunk_list_sha256(c['sha256'] for c in ref['chunks']),
                          'headersSha256': ref['headersSha256']}, 'CHECKPOINT_NOT_APPROVED',
             f'{seg["id"]}: the checkpoint differs from its approval')
    return {'planFile': ref['planFile'], 'planSha256': ref['planSha256'],
            'chunks': [{'path': c['path'], 'sha256': c['sha256']} for c in ref['chunks']],
            'headersFile': ref['headersFile'], 'headersSha256': ref['headersSha256']}


def spec(chain, *, anchor_block, anchor_hash, b0_plan, b0_plan_sha256, new_vaults=(), header_scope='H1',
         b0_event_block_recheck=True, checkpoints=None):
    """Builds the collection spec. ``new_vaults`` are (factory, index, vault) triples found after the pin."""
    _require(chain in h.CHAINS, 'SPEC_CHAIN', str(chain))
    prefix = h.B0_PREFIX[chain]
    _require(b0_plan_sha256 == prefix['planSha256'], 'SPEC_B0_PLAN', 'B0 plan is not the pinned archive plan')
    _require(header_scope in HEADER_SCOPES, 'SPEC_HEADER_SCOPE', str(header_scope))
    _require(header_scope in APPROVED_HEADER_SCOPES, 'HEADER_SCOPE_NOT_APPROVED',
             f'{header_scope} needs an independently recorded CR-D1 approval; H1 is the reviewed default')
    _require(type(anchor_block) is int and anchor_block >= prefix['end'], 'SPEC_ANCHOR', 'anchor before the B0 pin')
    _require(h.full_hash(anchor_hash), 'SPEC_ANCHOR', 'anchor hash')
    if anchor_block == prefix['end']:
        _require(anchor_hash == prefix['endBlockHash'], 'SPEC_ANCHOR', 'pin anchor must carry the B0 pin hash')
    _require(isinstance(b0_event_block_recheck, bool), 'SPEC_B0_RECHECK')
    reg = h.registry(chain)
    targets = b0_targets(b0_plan)
    known = {t['address'] for t in targets}
    _require(b0_plan['start'] == prefix['start'] and b0_plan['end'] == prefix['end']
             and b0_plan['filter_id'] == prefix['filterId'], 'SPEC_B0_PLAN', 'B0 plan range')
    counts = b0_vault_counts(b0_plan, chain)
    vaults = []
    for factory, index, vault in sorted(new_vaults, key=lambda v: (v[0], v[1])):
        _require(factory in reg['factories'], 'SPEC_NEW_VAULT', f'{factory} is not a registry factory')
        _require(type(index) is int and index >= counts[factory], 'SPEC_NEW_VAULT', f'{vault}: index {index}')
        _require(vault == create_address(factory, VAULT_NONCE_OFFSET + index), 'UNKNOWN_CREATION_PATH',
                 f'{vault} is not CREATE({factory}, {VAULT_NONCE_OFFSET + index})')
        _require(vault not in known, 'SPEC_NEW_VAULT', f'{vault} is already a B0 target')
        vaults.append({'factory': factory, 'index': index, 'vault': vault})
        known.add(vault)
    for factory in reg['factories']:
        indices = sorted(v['index'] for v in vaults if v['factory'] == factory)
        _require(indices == list(range(counts[factory], counts[factory] + len(indices))), 'SPEC_NEW_VAULT',
                 f'{factory}: new vault indices must follow the B0 vaults without gap')
    extra = [{'address': a, 'labels': [SAFE_LABEL]} for a in h.EXTRA_TARGETS[chain]]
    _require(not ({e['address'] for e in extra} & {t['address'] for t in targets}), 'SPEC_TARGETS',
             'an extra target is already a B0 target')
    from_block, pin = prefix['start'], prefix['end']
    vault_entries = [{'address': v['vault'], 'labels': [vault_label(v['factory'], v['index'])]} for v in vaults]
    segments = []
    if anchor_block > pin:
        segments.append(_segment('S1', 'post-pin', pin + 1, anchor_block, targets + extra, [],
                                 'B0 pin + 1; B0 targets and campaign extra targets'))
    if extra:
        segments.append(_segment('S2', 'extra-pre-pin', from_block, pin, extra, [],
                                 'common fromBlock (B0 start); campaign extra targets'))
    if vaults:
        segments.append(_segment('S3', 'new-vaults-pre-pin', from_block, pin, vault_entries, vaults,
                                 'common fromBlock (B0 start); vaults created after the pin'))
        segments.append(_segment('S4', 'new-vaults-post-pin', pin + 1, anchor_block, vault_entries, vaults,
                                 'B0 pin + 1; vaults created after the pin'))
    checkpoints = dict(checkpoints or {})
    for seg in segments:
        if seg['id'] in checkpoints:
            _require(seg['end'] <= pin, 'SPEC_CHECKPOINT', f'{seg["id"]}: only pre-pin segments are immutable')
            seg['checkpoint'] = _checkpoint(chain, seg, checkpoints.pop(seg['id']))
    _require(not checkpoints, 'SPEC_CHECKPOINT', f'no such segment: {sorted(checkpoints)}')
    return {'schema': SPEC_SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'rpc': PUBLIC_RPC[chain],
            'fromBlock': from_block, 'pin': {'block': pin, 'hash': prefix['endBlockHash']},
            'anchor': {'block': anchor_block, 'hash': anchor_hash},
            'finalityDepth': {'blocks': FINALITY_DEPTH_ASSUMPTION, 'status': 'ASSUMPTION (FH-D5); not verified'},
            'headerScope': header_scope, 'b0EventBlockRecheck': b0_event_block_recheck,
            'b0': {'planSha256': prefix['planSha256'], 'chunkListSha256': prefix['chunkListSha256'],
                   'chunkCount': prefix['chunkCount'], 'filterId': prefix['filterId'],
                   'targets': [t['address'] for t in targets]},
            'extraTargets': list(h.EXTRA_TARGETS[chain]), 'newVaults': vaults, 'segments': segments,
            'balance': {'address': reg['stpros'], 'block': anchor_block}, 'toolVersion': TOOL_VERSION}


def check_spec(doc, b0_plan):
    """Refuses a spec that is not exactly what ``spec`` derives from its own inputs."""
    _require(isinstance(doc, dict) and set(doc) == SPEC_KEYS and doc.get('schema') == SPEC_SCHEMA, 'SPEC_KEYS')
    _require(all(isinstance(s, dict) and set(s) == SEGMENT_KEYS for s in doc['segments']), 'SPEC_KEYS', 'segment')
    rebuilt = spec(doc['chain'], anchor_block=doc['anchor']['block'], anchor_hash=doc['anchor']['hash'],
                   b0_plan=b0_plan, b0_plan_sha256=doc['b0']['planSha256'],
                   new_vaults=[(v['factory'], v['index'], v['vault']) for v in doc['newVaults']],
                   header_scope=doc['headerScope'], b0_event_block_recheck=doc['b0EventBlockRecheck'],
                   checkpoints={s['id']: s['checkpoint'] for s in doc['segments'] if s['checkpoint'] is not None})
    _require(canonical_bytes(rebuilt) == canonical_bytes(doc), 'SPEC_NOT_DERIVED',
             'the spec differs from the deterministic derivation of its inputs')
    return doc


def segment(doc, seg_id):
    found = [s for s in doc['segments'] if s['id'] == seg_id]
    _require(len(found) == 1, 'SPEC_SEGMENT', seg_id)
    return found[0]


def chunk_grid(seg):
    return logscan.chunks(seg['start'], seg['end'], seg['chunkWindows'], WINDOW)


def plan_file(seg_id):
    return f'segments/{seg_id}/plan.json'


def chunk_file(seg_id, a, b):
    return f'segments/{seg_id}/chunks/{a:010d}-{b:010d}.json'


def plan_doc(doc, seg, *, end_hash, end_timestamp, head_at_plan, code_before_start, created_utc):
    """A B0-schema plan (``history.PLAN_KEYS`` exactly) for one segment. ``code_before_start`` is the list of
    segment addresses that had code at ``start - 1`` (read at plan time; an archive error never becomes [])."""
    addresses = [e['address'] for e in seg['addresses']]
    _require(set(code_before_start) <= set(addresses), 'PLAN_CODE_BEFORE_START')
    return {'addresses': [{'address': e['address'], 'labels': list(e['labels'])} for e in seg['addresses']],
            'addresses_with_code_before_start': sorted(code_before_start), 'chain': doc['chain'],
            'chain_id': doc['chainId'], 'chunk_windows': seg['chunkWindows'], 'created_utc': created_utc,
            'end': seg['end'], 'end_block_hash': end_hash, 'end_timestamp': end_timestamp,
            'filter_id': j.historical_filter_id(addresses), 'head_at_plan': head_at_plan, 'rpc': doc['rpc'],
            'start': seg['start'], 'start_basis': f'{SPEC_SCHEMA} {seg["id"]}: {seg["startBasis"]}',
            'tool_version': doc['toolVersion'], 'vaults': list(seg['vaults']), 'window': WINDOW}


def plan_matches(doc, seg, plan):
    """The spec-determined part of an existing plan (resume): everything except what was read at plan time."""
    fixed = plan_doc(doc, seg, end_hash=plan.get('end_block_hash'), end_timestamp=plan.get('end_timestamp'),
                     head_at_plan=plan.get('head_at_plan'), code_before_start=[],
                     created_utc=plan.get('created_utc'))
    fixed['addresses_with_code_before_start'] = plan.get('addresses_with_code_before_start')
    return isinstance(plan, dict) and set(plan) == h.PLAN_KEYS and all(
        plan[k] == fixed[k] for k in h.PLAN_KEYS if k not in ('end_block_hash', 'end_timestamp', 'head_at_plan',
                                                               'created_utc', 'addresses_with_code_before_start'))


def scan_start_candidate(block, method):
    """A code-search result (CR-D11 N1/N3). Labelled as a scan-start boundary only, never a creation block."""
    return {'label': 'scanStart', 'block': block, 'method': method,
            'creationIdentity': 'UNAPPROVED: a code search is not creation evidence (CR-D11; N4 needs FR-X8)'}


def binary_code_search(has_code, low, high):
    """First block in (low, high] whose predecessor has no code and which has code, by bisection."""
    probes = []
    while high - low > 1:
        middle = (low + high) // 2
        probes.append(middle)
        if has_code(middle):
            high = middle
        else:
            low = middle
    return scan_start_candidate(high, 'binary'), probes


def kary_code_search(has_code, low, high, k=20):
    """The same search with k probes per round (one batch each)."""
    probes = []
    while high - low > 1:
        step = max(1, math.ceil((high - low) / k))
        points = list(range(low + step, high, step))[:k - 1]
        probes += points
        hits = [p for p in points if has_code(p)]
        if hits:
            high = hits[0]
            low = max([p for p in points if p < high] + [low])
        else:
            low = points[-1] if points else low
            if not points:
                break
    return scan_start_candidate(high, f'{k}-ary'), probes


# Cost-document inputs (RPC-COST-AND-OPTIMIZATION.md §0, §3): ASSUMPTION/CARRIED values, not measurements.
COST = {
    'mainnet': {'blockTime': 1.225, 'eventBlocksPerWindow': (1169, 8701), 'prePinGrid': 8701,
                'prePinSafeEventBlocks': 30, 'fixed': 7, 'observation': 30, 'b0EventBlocks': 1169},
    'testnet': {'blockTime': 1.833, 'eventBlocksPerWindow': (425, 7660), 'prePinGrid': 7659,
                'prePinSafeEventBlocks': 0, 'fixed': 6, 'observation': 35, 'b0EventBlocks': 425},
}


def w_inc(chain, days):
    return math.ceil(days * 86400 / COST[chain]['blockTime'] / WINDOW)


def budget(chain, w_inc_windows, new_vaults=0, header_scope='H1'):
    """POST counts of one B collection by category (no retries or fallbacks). Headers are counted over the
    de-duplicated block set: window ends of the new segments at the scope, event blocks, the anchor and the
    B0 pin. S1/S4 and S2/S3 share one window grid each, so their window ends coincide."""
    _require(header_scope in HEADER_SCOPES, 'SPEC_HEADER_SCOPE', str(header_scope))
    c = COST[chain]
    num, den = c['eventBlocksPerWindow']
    pre_segments = (1 if chain == 'mainnet' else 0) + (1 if new_vaults else 0)
    post_segments = 1 + (1 if new_vaults else 0)
    post_groups = math.ceil(w_inc_windows / CHUNK_WINDOWS) * post_segments
    pre_groups = math.ceil(c['prePinGrid'] / CHUNK_WINDOWS) * pre_segments
    event_blocks = math.ceil(w_inc_windows * num / den) + (c['prePinSafeEventBlocks'] if chain == 'mainnet' else 0)
    pre_ends = c['prePinGrid'] if pre_segments else 0          # the last one is the pin
    if header_scope == 'H1':
        window_ends = w_inc_windows + pre_ends + (0 if pre_segments else 1)
    elif header_scope == 'H2':
        window_ends = w_inc_windows + 1                          # post-pin window ends (incl. B) and the pin
    else:
        window_ends = 2                                          # the anchor and the pin
    headers = math.ceil((window_ends + event_blocks) / CHUNK_WINDOWS)
    subtotal = c['fixed'] + post_groups + pre_groups + headers + c['observation']
    return {'chain': chain, 'headerScope': header_scope, 'wInc': w_inc_windows, 'fixed': c['fixed'],
            'postPinLogPosts': post_groups, 'prePinLogPosts': pre_groups, 'headerPosts': headers,
            'observationPosts': c['observation'], 'subtotal': subtotal,
            'b0EventBlockRecheckPosts': math.ceil(c['b0EventBlocks'] / CHUNK_WINDOWS),
            'label': 'ESTIMATE from CARRIED B0 ratios and ASSUMPTION block times; excludes retries and fallbacks'}
