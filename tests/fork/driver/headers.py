"""Canonical block-identity sidecar ``faroo-fork-headers-v1`` and its offline verification. Test-only.

``verify`` checks a sidecar against a content-verified manifest (``history.verify_manifest``) and its
reviewed spec, at the spec's header scope (CR-D1; H1 by default):

- ``anchor``: the anchor header equals the manifest and spec hash;
- ``planEnd``: every new segment's plan end equals the plan's ``end_block_hash`` and ``end_timestamp``;
- ``b0Pin``: the B0 pin equals the pinned B0 hash;
- ``eventBlock``: every block carrying a row of a new segment has the hash every such row names;
- ``windowEnd``: every canonical window end of every new segment (H1);
- ``b0EventBlock``: every block carrying a B0 row, when the spec asks for the recheck;
- consecutive headers link by ``parentHash``; the end-of-collection ``rechecks`` repeat every anchor and
  plan-end hash.

A missing header is INCOMPLETE; a mismatch, an unknown reason or a weaker scope is REFUSED. Passing means
the provider's header-by-number view agreed with the archive at collection time. It is not a consensus or
light-client proof, and it says nothing about logs a provider omitted from an empty window.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collection_plan as cp  # noqa: E402
import history as h  # noqa: E402
from history import HistoryRefused  # noqa: E402

SCHEMA = 'faroo-fork-headers-v1'
KEYS = frozenset({'schema', 'chain', 'chainId', 'collector', 'scope', 'collectedAt', 'headers', 'rechecks', 'imported'})
ENTRY_KEYS = frozenset({'number', 'hash', 'parentHash', 'timestamp', 'reason'})
REASONS = ('anchor', 'planEnd', 'b0Pin', 'eventBlock', 'windowEnd', 'b0EventBlock')
STATEMENT = ('provider header-by-number view at collection time; not consensus- or light-client-verified; '
             'empty windows rest on provider honesty')


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise HistoryRefused(code, detail, status)


def _number(row):
    return int(row['blockNumber'], 16)


def is_b0_row(row, spec):
    """B0 rows are exactly the rows at or below the pin from a B0 target; every other row is new."""
    return _number(row) <= spec['pin']['block'] and row['address'] in set(spec['b0']['targets'])


def required(verified, spec, root):
    """{block: {reasons}} the spec's scope requires, plus the plan documents of the new segments."""
    scope = spec['headerScope']
    _require(scope in cp.HEADER_SCOPES, 'HEADER_SCOPE', str(scope))
    pin = spec['pin']['block']
    need, plans = {}, []

    def add(number, reason):
        need.setdefault(number, set()).add(reason)

    add(spec['anchor']['block'], 'anchor')
    add(pin, 'b0Pin')
    for seg in verified['segments']:
        if seg['planSha256'] == spec['b0']['planSha256']:
            continue
        plan = h._load(root, seg['planFile'], seg['planSha256'], 'HISTORY_PLAN_HASH_REQUIRED')
        plans.append(plan)
        add(seg['end'], 'planEnd')
        if scope == 'H1' or (scope == 'H2' and seg['start'] > pin):
            for _, end in cp.logscan.windows(seg['start'], seg['end'], cp.WINDOW):
                add(end, 'windowEnd')
    for row in verified['rows']:
        if is_b0_row(row, spec):
            if spec['b0EventBlockRecheck']:
                add(_number(row), 'b0EventBlock')
        else:
            add(_number(row), 'eventBlock')
    return need, plans


def verify(verified, headers_doc, spec, root):
    """Returns {scope, entries, eventBlockTimestamps, ...}; raises HistoryRefused (REFUSED / INCOMPLETE)."""
    scope = spec['headerScope']
    _require(scope in cp.APPROVED_HEADER_SCOPES, 'HEADER_SCOPE_NOT_APPROVED', str(scope))
    d = headers_doc
    _require(isinstance(d, dict) and set(d) == KEYS and d['schema'] == SCHEMA, 'HEADER_KEYS')
    _require(d['chain'] == spec['chain'] == verified['chain'] and d['chainId'] == spec['chainId'], 'HEADER_CHAIN')
    _require(d['scope'] in cp.HEADER_SCOPES
             and cp.HEADER_SCOPES.index(d['scope']) <= cp.HEADER_SCOPES.index(scope), 'HEADER_SCOPE_DOWNGRADE',
             f'sidecar scope {d["scope"]!r} is weaker than the reviewed spec scope {scope}')
    _require(verified['blockNumber'] == spec['anchor']['block'] and verified['blockHash'] == spec['anchor']['hash'],
             'HEADER_ANCHOR', 'manifest anchor differs from the spec')
    _require(isinstance(d['headers'], list) and isinstance(d['rechecks'], list), 'HEADER_KEYS')
    by_number = {}
    for entry in d['headers']:
        _require(isinstance(entry, dict) and set(entry) == ENTRY_KEYS, 'HEADER_KEYS', 'entry')
        n = entry['number']
        _require(type(n) is int and n > 0 and n not in by_number, 'HEADER_ENTRY', f'number {n!r}')
        _require(h.full_hash(entry['hash']) and h.full_hash(entry['parentHash'])
                 and type(entry['timestamp']) is int and entry['timestamp'] >= 0, 'HEADER_ENTRY', str(n))
        _require(isinstance(entry['reason'], list) and entry['reason']
                 and len(set(entry['reason'])) == len(entry['reason'])
                 and all(r in REASONS for r in entry['reason']), 'HEADER_REASON', str(n))
        by_number[n] = entry
    _require([e['number'] for e in d['headers']] == sorted(by_number), 'HEADER_ORDER')
    need, plans = required(verified, spec, root)
    for number, reasons in need.items():
        entry = by_number.get(number)
        _require(entry is not None and reasons <= set(entry['reason']), 'HEADER_MISSING',
                 f'block {number}: {sorted(reasons)}', 'INCOMPLETE')
    for number, entry in by_number.items():
        _require(set(entry['reason']) <= need.get(number, set()), 'HEADER_REASON',
                 f'block {number} claims {sorted(set(entry["reason"]) - need.get(number, set()))}')
    anchor = by_number[spec['anchor']['block']]
    _require(anchor['hash'] == spec['anchor']['hash'], 'HISTORY_REORG', 'anchor hash changed')
    _require(by_number[spec['pin']['block']]['hash'] == spec['pin']['hash'], 'HISTORY_ANCHOR_MISMATCH', 'B0 pin hash')
    for plan in plans:
        entry = by_number[plan['end']]
        _require(entry['hash'] == plan['end_block_hash'] and entry['timestamp'] == plan['end_timestamp'],
                 'HEADER_PLAN_END', f'plan end {plan["end"]}')
    for row in verified['rows']:
        entry = by_number.get(_number(row))
        if entry is not None and ('eventBlock' in entry['reason'] or 'b0EventBlock' in entry['reason']):
            _require(row['blockHash'] == entry['hash'], 'HISTORY_EVENT_REORG', f'block {_number(row)}')
    for number, entry in by_number.items():
        parent = by_number.get(number - 1)
        if parent is not None:
            _require(entry['parentHash'] == parent['hash'], 'HEADER_CHAIN', f'{number - 1} -> {number}')
    rechecked = {}
    for item in d['rechecks']:
        _require(isinstance(item, dict) and set(item) == {'number', 'hash'} and type(item['number']) is int
                 and h.full_hash(item['hash']) and item['number'] not in rechecked, 'HEADER_RECHECK')
        rechecked[item['number']] = item['hash']
    for number in [spec['anchor']['block'], *(p['end'] for p in plans)]:
        _require(number in rechecked, 'HEADER_RECHECK_MISSING', str(number), 'INCOMPLETE')
        _require(rechecked[number] == by_number[number]['hash'], 'HISTORY_REORG', f'block {number} changed at recheck')
    _require(isinstance(d['imported'], list) and all(
        isinstance(i, dict) and set(i) == {'headersSha256', 'numbers'} for i in d['imported']), 'HEADER_KEYS', 'imported')
    approved = {s['checkpoint']['headersSha256'] for s in spec['segments'] if s['checkpoint'] is not None}
    _require({i['headersSha256'] for i in d['imported']} <= approved, 'HEADER_IMPORT',
             'entries imported from a sidecar that is not an approved checkpoint')
    return {'status': 'VERIFIED', 'scope': scope, 'sidecarScope': d['scope'], 'entries': len(by_number),
            'imported': sum(len(i['numbers']) for i in d['imported']),
            'required': sum(len(r) for r in need.values()), 'statement': STATEMENT,
            'timestamps': {n: e['timestamp'] for n, e in by_number.items()}}
