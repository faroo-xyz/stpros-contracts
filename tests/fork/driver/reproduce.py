"""Normalized comparison of two independent collections (``faroo-fork-collection-comparison-v1``). Test-only.

``compare`` takes two run descriptions (B's and C's) and returns EQUAL, DIFFERENT or INCOMPLETE under the
normalization of COLLECTION-DESIGN-PROPOSAL.md §2.6 (CR-D8):

- files are compared as parsed JSON, never as raw bytes;
- only collection-time metadata is excluded (``plan.created_utc``, ``plan.head_at_plan``,
  ``chunk.fetched_utc``, the journal, the header sidecar's ``collector``/``collectedAt``, and the state
  observation's pointer to its own v1 file. That pointer is excluded only because each side's v1 file may differ
  from the other's in formatting; it is first verified on each side against the raw SHA-256 of that side's own
  ``observation.json`` bytes and the manifest anchor, and a wrong binding is refused before any comparison;
- provider-forced window splits are merged back onto the plan's canonical 1,000-block grid: counts are
  summed, rows unioned, empty windows kept, and a split is reported as information only;
- every other field is exact: chain, range, filter, address set, labels, pre-start code, plan end hash and
  timestamp, endpoint, chunk boundaries, per-canonical-window counts, every row field, every header field on
  the blocks both sides read, and every observation value.

The result is a comparison fact only. Whether it makes a section ``REREAD_EQUAL`` or a run
``INDEPENDENTLY_REPRODUCED`` is recorded by the independent reviewer, never by this tool or by the collector,
and a comparison of a run with itself (equal manifest SHA-256) is refused.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collection_plan as cp  # noqa: E402
import headers as hd  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import state_observation as so  # noqa: E402
from history import HistoryRefused  # noqa: E402

SCHEMA = 'faroo-fork-collection-comparison-v1'
PLAN_EXCLUDED = frozenset({'created_utc', 'head_at_plan'})
CHUNK_EXCLUDED = frozenset({'fetched_utc'})
STATE_EXCLUDED = frozenset({'observationV1Sha256'})
ANCHOR_KEYS = ('chain', 'chainId', 'block', 'blockHash')
SIDE_KEYS = frozenset({'role', 'root', 'manifest', 'headers', 'observation', 'stateObservation'})
FILE_KEYS = frozenset({'path', 'sha256'})
STATEMENT = ('comparison facts only: REREAD_EQUAL / INDEPENDENTLY_REPRODUCED are recorded by the independent '
             'reviewer, not by this tool')
_QUANTITY = re.compile(r'0x(0|[1-9a-f][0-9a-f]*)')
_HASH = re.compile(r'0x[0-9a-f]{64}')
_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
_DATA = re.compile(r'0x([0-9a-f]{2})*')
LIMIT = 50


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise HistoryRefused(code, detail, status)


class Missing(Exception):
    """An evidence file of one side is absent: the comparison is INCOMPLETE, never EQUAL."""


def _load(side, key):
    ref = side[key]
    if ref is None:
        raise Missing(f'{side["role"]}: {key} not supplied')
    _require(isinstance(ref, dict) and set(ref) == FILE_KEYS, 'COMPARE_INPUT', key)
    path = Path(side['root']) / ref['path']
    if not path.is_file():
        raise Missing(f'{side["role"]}: {ref["path"]} missing')
    return h._load(side['root'], ref['path'], ref['sha256'], 'COMPARE_FILE_HASH')


def _load_raw(side, key):
    """(parsed document, raw SHA-256 of the bytes on disk). The digest is recomputed from the file and must still
    equal the reference: a caller-supplied digest never stands in for the file content."""
    doc = _load(side, key)
    raw = hashlib.sha256((Path(side['root']) / side[key]['path']).read_bytes()).hexdigest()
    _require(raw == side[key]['sha256'], 'COMPARE_FILE_HASH', f'{side["role"]}: {key} changed while it was read')
    return _jsonable(doc), raw


def _bind(role, v1, v1_raw_sha256, state, manifest):
    """One side's own state -> v1 binding: schemas, the exact pointer encoding (64 lowercase hex characters, no
    prefix), the pointer equal to the raw SHA-256 of that side's v1 bytes, and both documents at the manifest
    anchor. Refused, never compared, when it does not hold."""
    _require(isinstance(v1, dict) and v1.get('schema') == hi.OBSERVATION_SCHEMA, 'COMPARE_STATE_BINDING',
             f'{role}: observation.json is not {hi.OBSERVATION_SCHEMA}')
    _require(isinstance(state, dict) and state.get('schema') == so.SCHEMA, 'COMPARE_STATE_BINDING',
             f'{role}: state-observation.json is not {so.SCHEMA}')
    pointer = state.get('observationV1Sha256')
    _require(h.raw_hash(pointer), 'COMPARE_STATE_BINDING',
             f'{role}: observationV1Sha256 must be a raw SHA-256 (64 lowercase hex characters)')
    _require(pointer == v1_raw_sha256, 'COMPARE_STATE_BINDING',
             f'{role}: observationV1Sha256 is not the raw SHA-256 of its own observation.json')
    anchor = tuple(_jsonable(manifest[k]) for k in ('chain', 'chainId', 'blockNumber', 'blockHash'))
    for name, doc in (('observation', v1), ('stateObservation', state)):
        _require(tuple(doc.get(k) for k in ANCHOR_KEYS) == anchor, 'COMPARE_STATE_BINDING',
                 f'{role}: {name} is not at the manifest anchor {anchor}')


def _quantity(value, where):
    _require(isinstance(value, str) and _QUANTITY.fullmatch(value.lower()) is not None, 'COMPARE_QUANTITY', where)
    return int(value, 16)


def _lower(value, pattern, where):
    _require(isinstance(value, str) and pattern.fullmatch(value.lower()) is not None, 'COMPARE_FIELD', where)
    return value.lower()


def normalize_row(row, where):
    _require(isinstance(row, dict) and set(row) == set(cp.logscan.slim(row)) and len(row) == 9, 'COMPARE_ROW', where)
    _require(row['removed'] is False, 'COMPARE_ROW', f'{where}: removed')
    _require(isinstance(row['topics'], list), 'COMPARE_ROW', where)
    return {'address': _lower(row['address'], _ADDRESS, where),
            'topics': [_lower(t, _HASH, where) for t in row['topics']],
            'data': _lower(row['data'], _DATA, where),
            'blockNumber': _quantity(row['blockNumber'], where), 'blockHash': _lower(row['blockHash'], _HASH, where),
            'transactionHash': _lower(row['transactionHash'], _HASH, where),
            'transactionIndex': _quantity(row['transactionIndex'], where),
            'logIndex': _quantity(row['logIndex'], where), 'removed': False}


def _number(value):
    return int(value) if isinstance(value, float) and value == int(value) else value


def _jsonable(value):
    """checked_json yields floats for JSON integers; restore exact integers before comparing."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return _number(value)


def normalize_segment(root, seg):
    """(plan without excluded keys, chunk boundaries, per-canonical-window rows, split notes)."""
    plan = _jsonable(h._load(root, seg['planFile'], seg['planSha256'], 'COMPARE_FILE_HASH'))
    grid = cp.logscan.windows(plan['start'], plan['end'], cp.WINDOW)
    windows = {w: {'count': 0, 'rows': []} for w in grid}
    bounds, splits, keys = [], [], set()
    for ref in seg['chunks']:
        chunk = _jsonable(h._load(root, ref['path'], ref['sha256'], 'COMPARE_FILE_HASH'))
        bounds.append([chunk['from'], chunk['to']])
        for a, b, count in chunk['windows']:
            home = [w for w in grid if w[0] <= a and b <= w[1]]
            _require(len(home) == 1, 'COMPARE_WINDOW', f'[{a}, {b}] is not inside one canonical window')
            if (a, b) != home[0]:
                splits.append([a, b])
            windows[home[0]]['count'] += count
        for i, row in enumerate(chunk['logs']):
            n = normalize_row(row, f'{ref["path"]}[{i}]')
            key = (n['blockNumber'], n['logIndex'])
            _require(key not in keys, 'COMPARE_DUPLICATE_ROW', str(key))
            keys.add(key)
            home = [w for w in grid if w[0] <= n['blockNumber'] <= w[1]]
            _require(len(home) == 1, 'COMPARE_ROW', f'block {n["blockNumber"]} outside the plan')
            windows[home[0]]['rows'].append(n)
    for w in windows.values():
        w['rows'].sort(key=lambda r: (r['blockNumber'], r['logIndex']))
        _require(w['count'] == len(w['rows']), 'COMPARE_WINDOW_COUNT')
    shaped = {k: v for k, v in plan.items() if k not in PLAN_EXCLUDED}
    shaped['end_block_hash'] = _lower(plan['end_block_hash'], _HASH, 'end_block_hash')
    return shaped, bounds, windows, splits, plan


def _diff(a, b, path='$', out=None):
    out = [] if out is None else out
    if len(out) >= LIMIT:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append({'path': f'{path}.{key}', 'b': a.get(key, '<absent>'), 'c': b.get(key, '<absent>')})
            else:
                _diff(a[key], b[key], f'{path}.{key}', out)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f'{path}[{i}]', out)
    elif a != b or type(a) is not type(b):
        out.append({'path': path, 'b': a, 'c': b})
    return out


def _side_manifest(side, spec):
    manifest = _load(side, 'manifest')
    prefix = {'start': spec['fromBlock'], 'end': spec['pin']['block'], 'filterId': spec['b0']['filterId'],
              'endBlockHash': spec['pin']['hash'], 'planSha256': spec['b0']['planSha256'],
              'chunkCount': spec['b0']['chunkCount'], 'chunkListSha256': spec['b0']['chunkListSha256']}
    verified = h.verify_manifest(manifest, side['root'], spec['chain'], prefix)
    return manifest, verified


def compare(spec, b, c):
    """``spec``: the reviewed spec both sides collected from. ``b``/``c``: {role, root, manifest, headers,
    observation, stateObservation}, each file as {path, sha256} relative to that side's history root."""
    for side in (b, c):
        _require(isinstance(side, dict) and set(side) == SIDE_KEYS, 'COMPARE_INPUT', 'side keys')
    _require(b['manifest'] is not None and c['manifest'] is not None
             and b['manifest']['sha256'] != c['manifest']['sha256'], 'NOT_INDEPENDENT',
             'both sides name the same manifest: a run cannot reproduce itself')
    differences, incomplete, splits, side_issues = [], [], {'b': [], 'c': []}, []
    doc = {'schema': SCHEMA, 'chain': spec['chain'], 'statement': STATEMENT,
           'b': {k: b[k] for k in ('role', 'manifest', 'headers', 'observation', 'stateObservation')},
           'c': {k: c[k] for k in ('role', 'manifest', 'headers', 'observation', 'stateObservation')}}
    try:
        (bm, bv), (cm, cv) = _side_manifest(b, spec), _side_manifest(c, spec)
    except Missing as error:
        doc.update(segments=[], headers=None, observations=None, differences=[], incomplete=[str(error)],
                   splits=splits, sideIssues=[], verdict='INCOMPLETE')
        return doc
    differences += _diff({k: _jsonable(bm[k]) for k in ('chain', 'chainId', 'fromBlock', 'blockNumber', 'blockHash',
                                                       'inventorySha256', 'targets')},
                         {k: _jsonable(cm[k]) for k in ('chain', 'chainId', 'fromBlock', 'blockNumber', 'blockHash',
                                                       'inventorySha256', 'targets')}, '$.manifest')
    bseg = {(s['start'], s['end'], s['filterId']): s for s in bv['segments']}
    cseg = {(s['start'], s['end'], s['filterId']): s for s in cv['segments']}
    raw_b = {(s['start'], s['end'], s['filterId']): r for s, r in zip(bv['segments'], bm['segments'])}
    raw_c = {(s['start'], s['end'], s['filterId']): r for s, r in zip(cv['segments'], cm['segments'])}
    segments = []
    for key in sorted(set(bseg) | set(cseg)):
        if key not in bseg or key not in cseg:
            differences.append({'path': f'$.segments{list(key)}', 'b': key in bseg, 'c': key in cseg})
            continue
        if bseg[key]['planSha256'] == spec['b0']['planSha256'] or cseg[key]['planSha256'] == spec['b0']['planSha256']:
            same = (bseg[key]['planSha256'], bseg[key]['chunkListSha256']) == \
                (cseg[key]['planSha256'], cseg[key]['chunkListSha256'])
            segments.append({'segment': list(key), 'kind': 'B0 prefix (by SHA-256)', 'equal': same})
            if not same:
                differences.append({'path': f'$.segments{list(key)}.b0', 'b': bseg[key]['planSha256'],
                                    'c': cseg[key]['planSha256']})
            continue
        bp, bb, bw, bs, bplan = normalize_segment(b['root'], raw_b[key])
        cp_, cb, cw, cs, cplan = normalize_segment(c['root'], raw_c[key])
        local = _diff(bp, cp_, f'$.segments{list(key)}.plan')
        local += _diff(bb, cb, f'$.segments{list(key)}.chunkBounds')
        local += _diff(sorted(bw), sorted(cw), f'$.segments{list(key)}.windowGrid')
        for w in sorted(set(bw) & set(cw)):
            local += _diff(bw[w], cw[w], f'$.segments{list(key)}.window[{w[0]},{w[1]}]')
        splits['b'] += bs
        splits['c'] += cs
        for role, plan in (('b', bplan), ('c', cplan)):
            if plan['head_at_plan'] - plan['end'] < spec['finalityDepth']['blocks']:
                side_issues.append({'side': role, 'segment': list(key), 'rule': 'head_at_plan - end >= finality depth'})
        segments.append({'segment': list(key), 'equal': not local, 'differences': local[:LIMIT]})
        differences += local
    header_result = None
    try:
        bh, ch = _load(b, 'headers'), _load(c, 'headers')
        for side, verified, sidecar in (('b', bv, bh), ('c', cv, ch)):
            need = _minimum(verified, spec, side_root(b if side == 'b' else c))
            have = {e['number'] for e in sidecar['headers']}
            if not need <= have:
                incomplete.append(f'{side}: headers miss the minimum independent set {sorted(need - have)[:5]}')
        bhe = {e['number']: e for e in _jsonable(bh)['headers']}
        che = {e['number']: e for e in _jsonable(ch)['headers']}
        shared = sorted(set(bhe) & set(che))
        local = []
        for n in shared:
            local += _diff({k: bhe[n][k] for k in ('number', 'hash', 'parentHash', 'timestamp')},
                           {k: che[n][k] for k in ('number', 'hash', 'parentHash', 'timestamp')}, f'$.headers[{n}]')
        header_result = {'shared': len(shared), 'equal': not local}
        differences += local
    except Missing as error:
        incomplete.append(str(error))
    loaded = {}
    for role, side, manifest in (('b', b, bm), ('c', c, cm)):
        for key in ('observation', 'stateObservation'):
            try:
                loaded[role, key] = _load_raw(side, key)
            except Missing as error:
                incomplete.append(str(error))
        if (role, 'observation') in loaded and (role, 'stateObservation') in loaded:
            (v1, v1_raw), (state, _) = loaded[role, 'observation'], loaded[role, 'stateObservation']
            _bind(role, v1, v1_raw, state, manifest)
    observation_result = {}
    for key, excluded in (('observation', frozenset()), ('stateObservation', STATE_EXCLUDED)):
        if ('b', key) not in loaded or ('c', key) not in loaded:
            observation_result[key] = None
            continue
        x, y = loaded['b', key][0], loaded['c', key][0]
        local = _diff({k: v for k, v in x.items() if k not in excluded},
                      {k: v for k, v in y.items() if k not in excluded}, f'$.{key}')
        observation_result[key] = {'equal': not local}
        differences += local
    if side_issues:
        incomplete.append('a side violates the finality-depth planning rule')
    verdict = 'DIFFERENT' if differences else ('INCOMPLETE' if incomplete else 'EQUAL')
    doc.update(segments=segments, headers=header_result, observations=observation_result,
               differences=differences[:LIMIT], differenceCount=len(differences), incomplete=incomplete,
               splits=splits, sideIssues=side_issues, verdict=verdict)
    return doc


def side_root(side):
    return side['root']


def _minimum(verified, spec, root):
    """C's minimum independent header set (H3): event blocks, plan ends, the anchor and the pin."""
    need, _ = hd.required(verified, {**spec, 'headerScope': 'H3', 'b0EventBlockRecheck': False}, root)
    return set(need)
