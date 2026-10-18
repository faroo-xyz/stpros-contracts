"""FR-D1 scope binding: one authorization per stage and chain, its campaign envelope, and the run pins. Test-only.

The v1 authorization schemas bind a chain, a method list and a budget. The v2 schemas (this module) add the
campaign envelope, and every value of it that the code can derive is checked for exact equality:

- ``check_collection_authorization`` / ``check_fork_authorization``: stage, single chain, exact endpoint and chain
  id, method list, transport limits, the owner ceilings (``OWNER_CEILINGS``, never exceeded even when pinned), the
  immutable B0 pins, ``fromBlock``, the registry targets and the (empty) extra targets and pre-pin segment, the new-vault
  creation rule, H1 headers with the B0 event-block recheck, no checkpoint, the anchor policy, the validity window
  and the source binding (raw SHA-256 of every enforcing source, ``SOURCE_BINDING_FILES``);
- ``select_anchor`` (S1): the single selection invocation reads the chain id and the head through the budgeted
  gate, takes ``head - depthBlocks``, refuses an anchor outside the policy bounds and pins block, hash and how it
  was chosen in the hash-chained budget ledger before any collection invocation exists;
- ``check_spec`` and ``bind_run``: a spec is accepted only inside the envelope, at the pinned anchor (S1: its own
  selection; S2: the S1 ledger's anchor and final spec, never S1's outputs), with a vault set that only grows,
  and only if the budget left can cover the run's lower bound. Its SHA-256 is pinned in the ledger before the
  run's first read, so a recomputed spec hash is never an approval;
- ``bind_fork_anchor`` (S3): the fork block and hash come from the S1 and S2 ledgers and an Agent A
  reconciliation record pinned in ``fr_d1_approvals.RECONCILIATIONS`` (empty: every real-state Fork is BLOCKED);
  ``check_fork_request`` / ``check_fork_response`` keep every forwarded read at that block and stop on a chain id
  or fork-header mismatch.

Nothing here signs, sends a transaction or reads the environment or a secret.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collection_plan as cp  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import history as h  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release.native import rpc as native_rpc  # noqa: E402

ROOT = h.ROOT
COLLECTION_SCHEMA = 'faroo-fork-collection-authorization-v2'
FORK_SCHEMA = 'faroo-fork-execution-authorization-v2'
RECONCILIATION_SCHEMA = 'faroo-fork-s1-s2-reconciliation-v1'
COLLECTION_STAGES = ('S1', 'S2')
# Owner FR-D1 decision of 2026-10-18: one budget per stage and chain, at most 3 restarts. A record above these is
# refused even if its SHA-256 is pinned; a larger budget needs a new owner decision and a change here.
OWNER_CEILINGS = {'S1': {'mainnet': 2000, 'testnet': 400}, 'S2': {'mainnet': 1000, 'testnet': 200},
                  'S3': {'mainnet': 20000, 'testnet': 20000}}
OWNER_MAX_RESTARTS = 3
MAX_RETRIES = 2                          # ReleaseRpc: three attempts per POST
COLLECTION_METHODS = tuple(sorted(tp.COLLECTION_METHODS | {tp.BALANCE_METHOD}))
# The reads a forked anvil was measured to send (execution-readiness, SIMULATION). The margin methods of the v1
# list are not authorized for S3.
FORK_STATE_METHODS = ('eth_chainId', 'eth_getBlockByNumber', 'eth_getBalance', 'eth_getTransactionCount',
                      'eth_getCode', 'eth_getStorageAt')
# Sources whose behaviour defines the authorized scope. ``fr_d1_approvals.py`` is excluded on purpose.
SOURCE_BINDING_FILES = (
    'tests/fork/driver/collection_plan.py', 'tests/fork/driver/collector.py', 'tests/fork/driver/fork_mode.py',
    'tests/fork/driver/fr_d1_collect.py', 'tests/fork/driver/fr_d1_scope.py', 'tests/fork/driver/headers.py',
    'tests/fork/driver/history.py', 'tests/fork/driver/history_inventory.py', 'tests/fork/driver/inventory.py',
    'tests/fork/driver/observe.py', 'tests/fork/driver/reproduce.py', 'tests/fork/driver/state_observation.py',
    'tests/fork/driver/transport.py', 'tools/lib/rpc.py', 'tools/release/logscan.py',
    'tools/release/native/history.py', 'tools/release/native/rpc.py', 'tools/release/registry.json')
COLLECTION_KEYS = frozenset({'schema', 'authorizationId', 'mode', 'stage', 'chains', 'methods', 'maxHttpPosts',
                             'maxRestarts', 'statement', 'scope'})
COLLECTION_SCOPE_KEYS = frozenset({'chainId', 'endpoint', 'transport', 'methodRestrictions', 'b0', 'fromBlock',
                                   'targets', 'newVaults', 'headerScope', 'b0EventBlockRecheck', 'checkpoints',
                                   'anchorPolicy', 'validity', 'retention', 'owner', 'sourceBinding'})
FORK_KEYS = frozenset({'schema', 'authorizationId', 'mode', 'stage', 'chain', 'endpoint', 'methods', 'forkBlock',
                       'forkBlockHash', 'maxHttpPosts', 'maxRestarts', 'pacingMs', 'retention', 'statement', 'scope'})
FORK_SCOPE_KEYS = frozenset({'chainId', 'endpoint', 'transport', 'paramPolicy', 'forkAnchor', 'validity', 'owner',
                             'prerequisites', 'sourceBinding'})
RECONCILIATION_KEYS = frozenset({'schema', 'chain', 'chainId', 'anchorBlock', 'anchorHash', 'specSha256',
                                 's1AuthorizationSha256', 's2AuthorizationSha256', 's1ManifestSha256',
                                 's2ManifestSha256', 'comparisonSha256', 'verdict', 'reviewer', 'statement'})
_SHA = re.compile(r'[0-9a-f]{64}')
_COMMIT = re.compile(r'[0-9a-f]{40}')
_HASH = re.compile(r'0x[0-9a-f]{64}')
_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
_QUANTITY = re.compile(r'0x(0|[1-9a-f][0-9a-f]*)')
_UTC = re.compile(r'20[0-9]{2}-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z')


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise CollectionStop(code, detail, status)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def endpoint(chain):
    """The resolved allowlist entry of ``chain`` (the URL the gate actually posts to)."""
    return native_rpc._endpoint(PUBLIC_RPC[chain], False, False)[1]


def read_record(path, expected_sha256, code):
    """Raw bytes of an evidence record, by raw SHA-256. A missing or changed record is REFUSED."""
    _require(isinstance(expected_sha256, str) and _SHA.fullmatch(expected_sha256) is not None, code, 'raw SHA-256')
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise CollectionStop(code.replace('_HASH', '_MISSING'), str(path), 'REFUSED') from None
    _require(sha256(raw) == expected_sha256, code, str(path))
    try:
        return raw, json.loads(raw)
    except ValueError:
        raise CollectionStop(code.replace('_HASH', '_KEYS'), f'{path}: not JSON', 'REFUSED') from None


def source_files():
    return {name: sha256((ROOT / name).read_bytes()) for name in SOURCE_BINDING_FILES}


def _texts(value):
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_texts(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _texts(v) for k, v in value.items())
    return False


def _utc_ok(text):
    return isinstance(text, str) and _UTC.fullmatch(text) is not None


# --------------------------------------------------------------------------------------- expected envelope
def expected_transport():
    return {'pacingMsMin': tp.PACING_MS, 'maxItemsPerPost': tp.GROUP_LIMIT, 'maxRetriesPerPost': MAX_RETRIES,
            'logRangeBlocks': tp.WINDOW_LIMIT, 'postsInFlight': 1, 'redirects': 'REFUSED',
            'environmentProxy': 'REFUSED'}


def expected_method_restrictions(chain):
    return {'eth_getBalance': {'address': h.registry(chain)['stpros'], 'block': 'ANCHOR'},
            'eth_getLogs': {'addresses': 'SEGMENT_TARGETS', 'maxRangeBlocks': tp.WINDOW_LIMIT},
            'eth_getBlockByNumber': {'blocks': "'latest' (anchor checks), anchor, B0 pin, plan ends, H1 window ends,"
                                               ' event blocks, B0 event blocks (incl. the gas-envelope samples)'},
            'eth_getCode': {'blocks': 'segment start - 1, anchor'},
            'eth_getStorageAt': {'blocks': 'anchor'}, 'eth_call': {'blocks': 'anchor'},
            'eth_getTransactionByHash': {'scope': 'transactions of collected rows: the gas-envelope sample '
                                                   'deployments (collection_plan.GAS_ENVELOPE, B0 rows) only'},
            'eth_chainId': {'expected': h.CHAINS[chain]}}


def expected_b0(chain):
    p = h.B0_PREFIX[chain]
    return {'start': p['start'], 'end': p['end'], 'endBlockHash': p['endBlockHash'], 'planSha256': p['planSha256'],
            'chunkCount': p['chunkCount'], 'chunkListSha256': p['chunkListSha256'], 'filterId': p['filterId']}


def expected_extra_segment(chain):
    extra = list(h.EXTRA_TARGETS[chain])
    if not extra:
        return None
    p = h.B0_PREFIX[chain]
    return {'id': 'S2', 'kind': 'extra-pre-pin', 'start': p['start'], 'end': p['end'], 'addresses': extra}


def expected_new_vaults(chain):
    return {'factories': list(h.registry(chain)['factories']), 'nonceOffset': cp.VAULT_NONCE_OFFSET,
            'creation': 'CREATE(factory, nonceOffset + index)',
            'indices': 'contiguous after the B0 vaults of the same factory',
            'discovery': 'YieldVaultCreated of a registry factory in the collected history; a mismatch stops'}


def expected_param_policy():
    return {'block': 'FORK_BLOCK_ONLY',
            'blockForms': ['hex(forkBlock)', '{"blockNumber": hex(forkBlock)}', '{"blockHash": forkBlockHash}'],
            'responseChecks': ['eth_chainId result == chainId', 'eth_getBlockByNumber(forkBlock) hash == forkBlockHash']}


def expected_fork_transport():
    return {'pacingMsMin': tp.PACING_MS, 'maxItemsPerPost': tp.GROUP_LIMIT, 'upstreamRetries': 0,
            'requestsInFlight': 1, 'redirects': 'REFUSED', 'environmentProxy': 'REFUSED'}


# --------------------------------------------------------------------------------------- record builders
def collection_record(stage, chain, *, mode, ident, ceiling, restarts, b0_targets, anchor_policy, validity, retention,
                      owner, statement, bases):
    """An S1/S2 record whose derivable envelope is the code's; ``bases`` = (production, research) commits."""
    return {'schema': COLLECTION_SCHEMA, 'authorizationId': ident, 'mode': mode, 'stage': stage, 'chains': [chain],
            'methods': list(COLLECTION_METHODS), 'maxHttpPosts': ceiling, 'maxRestarts': restarts,
            'statement': statement,
            'scope': {'chainId': h.CHAINS[chain], 'endpoint': endpoint(chain), 'transport': expected_transport(),
                      'methodRestrictions': expected_method_restrictions(chain), 'b0': expected_b0(chain),
                      'fromBlock': h.B0_PREFIX[chain]['start'],
                      'targets': {'b0Targets': sorted(b0_targets), 'extraTargets': list(h.EXTRA_TARGETS[chain]),
                                  'extraSegment': expected_extra_segment(chain)},
                      'newVaults': expected_new_vaults(chain), 'headerScope': 'H1', 'b0EventBlockRecheck': True,
                      'checkpoints': [], 'anchorPolicy': anchor_policy, 'validity': validity, 'retention': retention,
                      'owner': owner, 'sourceBinding': {'productionBase': bases[0], 'researchBase': bases[1],
                                                        'files': source_files()}}}


def fork_record(chain, *, mode, ident, upstream, ceiling, restarts, s1_sha256, s2_sha256, validity, retention, owner,
                prerequisites, statement, bases):
    return {'schema': FORK_SCHEMA, 'authorizationId': ident, 'mode': mode, 'stage': 'S3', 'chain': chain,
            'endpoint': upstream, 'methods': list(FORK_STATE_METHODS), 'forkBlock': None, 'forkBlockHash': None,
            'maxHttpPosts': ceiling, 'maxRestarts': restarts, 'pacingMs': tp.PACING_MS, 'retention': retention,
            'statement': statement,
            'scope': {'chainId': h.CHAINS[chain], 'endpoint': upstream, 'transport': expected_fork_transport(),
                      'paramPolicy': expected_param_policy(),
                      'forkAnchor': {'rule': 'S1_S2_RECONCILED', 's1AuthorizationSha256': s1_sha256,
                                     's2AuthorizationSha256': s2_sha256, 'reconciliationSchema': RECONCILIATION_SCHEMA},
                      'validity': validity, 'owner': owner, 'prerequisites': prerequisites,
                      'sourceBinding': {'productionBase': bases[0], 'researchBase': bases[1], 'files': source_files()}}}


def record_bytes(doc):
    """The on-disk form of a record: sorted keys, two-space indent, one trailing newline."""
    return (json.dumps(doc, indent=2, sort_keys=True) + '\n').encode()


# --------------------------------------------------------------------------------------- authorization checks
def _common(doc, stage, chain, ceiling, restarts):
    _require(_texts(doc['statement']) and doc['mode'] in ('SIMULATION',) + (
        ('PUBLIC_READ_ONLY',) if stage in COLLECTION_STAGES else ('LOCAL_FORK_PUBLIC_READ_ONLY',)),
        'AUTHORIZATION_KEYS', 'mode')
    prefix = 'fr-d1' if doc['mode'] != 'SIMULATION' else 'sim'
    _require(re.fullmatch(rf'{prefix}-{stage.lower()}-{chain}-[0-9a-z]+', doc['authorizationId']) is not None,
             'AUTHORIZATION_ID', f'{doc["authorizationId"]!r} must name stage {stage} and chain {chain}')
    if doc['mode'] != 'SIMULATION':
        _require(ceiling <= OWNER_CEILINGS[stage][chain], 'AUTHORIZATION_CEILING',
                 f'{ceiling} > owner ceiling {OWNER_CEILINGS[stage][chain]} for {stage} {chain}')
        _require(restarts <= OWNER_MAX_RESTARTS, 'AUTHORIZATION_RESTARTS', str(restarts))


def _validity(scope):
    v = scope['validity']
    _require(isinstance(v, dict) and set(v) == {'notBeforeUtc', 'notAfterUtc', 'stopConditions'}
             and _utc_ok(v['notBeforeUtc']) and _utc_ok(v['notAfterUtc']) and v['notBeforeUtc'] < v['notAfterUtc']
             and isinstance(v['stopConditions'], list) and v['stopConditions'] and _texts(v['stopConditions']),
             'AUTHORIZATION_VALIDITY')


def _source_binding(scope):
    b = scope['sourceBinding']
    _require(isinstance(b, dict) and set(b) == {'productionBase', 'researchBase', 'files'}
             and isinstance(b['productionBase'], str) and _COMMIT.fullmatch(b['productionBase']) is not None
             and isinstance(b['researchBase'], str) and _COMMIT.fullmatch(b['researchBase']) is not None
             and isinstance(b['files'], dict) and set(b['files']) == set(SOURCE_BINDING_FILES),
             'AUTHORIZATION_SOURCE_BINDING', 'binding keys or file set')
    actual = source_files()
    changed = sorted(name for name in SOURCE_BINDING_FILES if b['files'][name] != actual[name])
    _require(not changed, 'AUTHORIZATION_SOURCE_BINDING', f'source differs from the authorized version: {changed}')


def check_collection_authorization(doc, *, bind_sources=True):
    """Every derivable envelope value of an S1/S2 record equals the code's; refuses anything else. The source
    binding is checked for the record that drives a run; a referenced earlier-stage record (``bind_sources=False``)
    was bound when it ran and is identified by its raw SHA-256 and approval pin."""
    _require(isinstance(doc, dict) and set(doc) == COLLECTION_KEYS and doc['schema'] == COLLECTION_SCHEMA
             and doc['stage'] in COLLECTION_STAGES and isinstance(doc['chains'], list) and len(doc['chains']) == 1
             and doc['chains'][0] in h.CHAINS and isinstance(doc['scope'], dict)
             and set(doc['scope']) == COLLECTION_SCOPE_KEYS, 'AUTHORIZATION_KEYS', 'v2 collection keys')
    stage, chain, s = doc['stage'], doc['chains'][0], doc['scope']
    _common(doc, stage, chain, doc['maxHttpPosts'], doc['maxRestarts'])
    _require(doc['methods'] == list(COLLECTION_METHODS), 'AUTHORIZATION_METHODS', 'the reviewed read methods exactly')
    _require(s['chainId'] == h.CHAINS[chain], 'AUTHORIZATION_CHAIN', str(s['chainId']))
    _require(s['endpoint'] == endpoint(chain), 'AUTHORIZATION_ENDPOINT', repr(s['endpoint']))
    _require(s['transport'] == expected_transport(), 'AUTHORIZATION_TRANSPORT')
    _require(s['methodRestrictions'] == expected_method_restrictions(chain), 'AUTHORIZATION_METHODS', 'restrictions')
    _require(s['b0'] == expected_b0(chain), 'AUTHORIZATION_B0', 'B0 pins differ from the immutable archive')
    _require(s['fromBlock'] == h.B0_PREFIX[chain]['start'], 'AUTHORIZATION_RANGE', 'fromBlock')
    t = s['targets']
    _require(isinstance(t, dict) and set(t) == {'b0Targets', 'extraTargets', 'extraSegment'}
             and isinstance(t['b0Targets'], list) and t['b0Targets'] == sorted(set(t['b0Targets']))
             and all(isinstance(a, str) and _ADDRESS.fullmatch(a) for a in t['b0Targets']), 'AUTHORIZATION_TARGETS')
    reg = h.registry(chain)
    _require({reg['stpros'], *reg['factories']} <= set(t['b0Targets']), 'AUTHORIZATION_TARGETS',
             'the registry set is not in the B0 targets')
    _require(t['extraTargets'] == list(h.EXTRA_TARGETS[chain]), 'AUTHORIZATION_TARGETS', 'extra targets (GOV Safe)')
    _require(t['extraSegment'] == expected_extra_segment(chain), 'AUTHORIZATION_TARGETS', 'Safe pre-pin segment')
    _require(s['newVaults'] == expected_new_vaults(chain), 'AUTHORIZATION_NEW_VAULTS')
    _require(s['headerScope'] == 'H1' and s['headerScope'] in cp.APPROVED_HEADER_SCOPES, 'AUTHORIZATION_HEADERS')
    _require(s['b0EventBlockRecheck'] is True, 'AUTHORIZATION_HEADERS', 'B0 event-block recheck')
    _require(s['checkpoints'] == [], 'AUTHORIZATION_CHECKPOINTS', 'no checkpoint reuse is authorized')
    a, pin = s['anchorPolicy'], h.B0_PREFIX[chain]['end']
    if stage == 'S1':
        _require(isinstance(a, dict) and set(a) == {'rule', 'depthBlocks', 'minBlock', 'maxBlock', 'notAfterUtc',
                                                    'notAfterTimestamp', 'selection'}
                 and a['rule'] == 'HEAD_MINUS_DEPTH' and a['depthBlocks'] == cp.FINALITY_DEPTH_ASSUMPTION
                 and a['minBlock'] == pin + 1 and type(a['maxBlock']) is int and a['maxBlock'] >= a['minBlock']
                 and _utc_ok(a['notAfterUtc']) and type(a['notAfterTimestamp']) is int and a['notAfterTimestamp'] > 0
                 and a['selection'] == 'ONCE_BEFORE_COLLECTION', 'AUTHORIZATION_ANCHOR_POLICY')
    else:
        _require(isinstance(a, dict) and set(a) == {'rule', 's1AuthorizationId', 's1AuthorizationSha256'}
                 and a['rule'] == 'S1_PINNED_SPEC' and isinstance(a['s1AuthorizationSha256'], str)
                 and _SHA.fullmatch(a['s1AuthorizationSha256']) is not None
                 and isinstance(a['s1AuthorizationId'], str), 'AUTHORIZATION_ANCHOR_POLICY')
    _validity(s)
    _require(isinstance(s['retention'], dict) and s['retention'] and _texts(s['retention']), 'AUTHORIZATION_KEYS',
             'retention')
    _require(isinstance(s['owner'], dict) and s['owner'] and _texts(s['owner']), 'AUTHORIZATION_KEYS', 'owner')
    if bind_sources:
        _source_binding(s)
    else:
        _require(isinstance(s['sourceBinding'], dict) and set(s['sourceBinding']) == {'productionBase', 'researchBase',
                                                                                      'files'},
                 'AUTHORIZATION_SOURCE_BINDING', 'binding keys')


def check_fork_authorization(doc):
    _require(isinstance(doc, dict) and set(doc) == FORK_KEYS and doc['schema'] == FORK_SCHEMA and doc['stage'] == 'S3'
             and doc['chain'] in h.CHAINS and isinstance(doc['scope'], dict) and set(doc['scope']) == FORK_SCOPE_KEYS
             and doc['forkBlock'] is None and doc['forkBlockHash'] is None, 'AUTHORIZATION_KEYS', 'v2 fork keys')
    chain, s = doc['chain'], doc['scope']
    _common(doc, 'S3', chain, doc['maxHttpPosts'], doc['maxRestarts'])
    _require(doc['methods'] == list(FORK_STATE_METHODS), 'AUTHORIZATION_METHODS', 'the measured anvil reads exactly')
    _require(s['chainId'] == h.CHAINS[chain], 'AUTHORIZATION_CHAIN', str(s['chainId']))
    if doc['mode'] == 'SIMULATION':
        _require(native_rpc._endpoint(doc['endpoint'], True, True)[1] == doc['endpoint'] == s['endpoint'],
                 'FORK_ENDPOINT', 'a simulation upstream is loopback only')
    else:
        _require(doc['endpoint'] == s['endpoint'] == endpoint(chain), 'FORK_ENDPOINT', repr(doc['endpoint']))
    _require(s['transport'] == expected_fork_transport() and doc['pacingMs'] >= tp.PACING_MS,
             'AUTHORIZATION_TRANSPORT')
    _require(s['paramPolicy'] == expected_param_policy(), 'AUTHORIZATION_PARAMS')
    f = s['forkAnchor']
    _require(isinstance(f, dict) and set(f) == {'rule', 's1AuthorizationSha256', 's2AuthorizationSha256',
                                                'reconciliationSchema'}
             and f['rule'] == 'S1_S2_RECONCILED' and f['reconciliationSchema'] == RECONCILIATION_SCHEMA
             and all(isinstance(f[k], str) and _SHA.fullmatch(f[k]) for k in ('s1AuthorizationSha256',
                                                                            's2AuthorizationSha256'))
             and f['s1AuthorizationSha256'] != f['s2AuthorizationSha256'], 'AUTHORIZATION_ANCHOR_POLICY')
    _validity(s)
    _require(isinstance(s['owner'], dict) and s['owner'] and _texts(s['owner']), 'AUTHORIZATION_KEYS', 'owner')
    _require(isinstance(s['prerequisites'], list) and s['prerequisites'] and _texts(s['prerequisites']),
             'AUTHORIZATION_KEYS', 'prerequisites')
    _require(_texts(doc['retention']), 'AUTHORIZATION_KEYS', 'retention')
    _source_binding(s)


def check_validity(auth, now_utc):
    v = auth['scope']['validity']
    _require(_utc_ok(now_utc) and v['notBeforeUtc'] <= now_utc <= v['notAfterUtc'], 'AUTHORIZATION_EXPIRED',
             f'{now_utc} is outside [{v["notBeforeUtc"]}, {v["notAfterUtc"]}]')


# --------------------------------------------------------------------------------------- spec envelope
def check_spec(spec, auth):
    """A derived spec (``collection_plan.check_spec`` passed) must lie inside the authorization's envelope."""
    s, chain = auth['scope'], auth['chains'][0]
    _require(spec['chain'] == chain and spec['chainId'] == s['chainId'], 'SPEC_OUT_OF_SCOPE', 'chain')
    _require(native_rpc._endpoint(spec['rpc'], False, False)[1] == s['endpoint'], 'SPEC_OUT_OF_SCOPE', 'endpoint')
    _require(spec['fromBlock'] == s['fromBlock'] and spec['pin'] == {'block': s['b0']['end'],
                                                                     'hash': s['b0']['endBlockHash']},
             'SPEC_OUT_OF_SCOPE', 'fromBlock or B0 pin')
    _require({k: spec['b0'][k] for k in ('planSha256', 'chunkListSha256', 'chunkCount', 'filterId')}
             == {k: s['b0'][k] for k in ('planSha256', 'chunkListSha256', 'chunkCount', 'filterId')},
             'SPEC_OUT_OF_SCOPE', 'B0 archive')
    _require(spec['b0']['targets'] == s['targets']['b0Targets'], 'SPEC_OUT_OF_SCOPE', 'registry target set')
    _require(spec['extraTargets'] == s['targets']['extraTargets'], 'SPEC_OUT_OF_SCOPE', 'extra targets')
    _require(spec['headerScope'] == s['headerScope'] and spec['b0EventBlockRecheck'] is s['b0EventBlockRecheck'],
             'SPEC_OUT_OF_SCOPE', 'header scope')
    _require(all(seg['checkpoint'] is None for seg in spec['segments']), 'SPEC_OUT_OF_SCOPE', 'checkpoint reuse')
    anchor, pin = spec['anchor']['block'], s['b0']['end']
    _require(anchor > pin, 'SPEC_OUT_OF_SCOPE', 'the anchor must follow the B0 pin')
    policy = s['anchorPolicy']
    if policy['rule'] == 'HEAD_MINUS_DEPTH':
        _require(policy['minBlock'] <= anchor <= policy['maxBlock'], 'SPEC_OUT_OF_SCOPE', 'anchor bounds')
    ids = [seg['id'] for seg in spec['segments']]
    extra = s['targets']['extraSegment']
    _require(ids[:1] == ['S1'] and ('S2' in ids) == (extra is not None), 'SPEC_OUT_OF_SCOPE', f'segments {ids}')
    if extra is not None:
        seg = cp.segment(spec, 'S2')
        _require([seg['start'], seg['end'], [e['address'] for e in seg['addresses']]]
                 == [extra['start'], extra['end'], extra['addresses']], 'SPEC_OUT_OF_SCOPE', 'Safe pre-pin segment')
    factories = s['newVaults']['factories']
    _require(all(v['factory'] in factories for v in spec['newVaults']), 'SPEC_OUT_OF_SCOPE', 'new-vault factory')
    _require(spec['balance'] == {'address': s['methodRestrictions']['eth_getBalance']['address'], 'block': anchor},
             'SPEC_OUT_OF_SCOPE', 'eth_getBalance scope')


def required_posts_lower_bound(spec, b0_event_blocks):
    """POSTs a fresh run of ``spec`` sends at least (no retry, no fallback, no new event block, no observation)."""
    anchor, pin = spec['anchor']['block'], spec['pin']['block']
    posts = 3                                    # chain id, head, anchor and pin headers
    if spec['chain'] == 'mainnet' and anchor > pin:
        posts += 1                               # GC-01
    need = {anchor, pin} | (set(b0_event_blocks) if spec['b0EventBlockRecheck'] else set())
    for seg in spec['segments']:
        if seg['checkpoint'] is not None:
            continue
        posts += 1 + -(-len(seg['addresses']) // tp.GROUP_LIMIT) + len(cp.chunk_grid(seg))
        need.add(seg['end'])
        if spec['headerScope'] == 'H1':
            need |= {end for _, end in cp.logscan.windows(seg['start'], seg['end'], cp.WINDOW)}
    posts += 1 if cp.GAS_ENVELOPE.get(spec['chain']) else 0     # gas-envelope read
    return posts + -(-len(need) // tp.GROUP_LIMIT) + 1          # sidecar headers, end-of-collection recheck


# --------------------------------------------------------------------------------------- S1 anchor selection
def _header(raw, number=None):
    ok = (isinstance(raw, dict) and isinstance(raw.get('number'), str) and _QUANTITY.fullmatch(raw['number'])
          and isinstance(raw.get('timestamp'), str) and _QUANTITY.fullmatch(raw['timestamp'])
          and isinstance(raw.get('hash'), str) and _HASH.fullmatch(raw['hash'])
          and isinstance(raw.get('parentHash'), str) and _HASH.fullmatch(raw['parentHash']))
    _require(ok and (number is None or int(raw['number'], 16) == number), 'HEADER_MISSING_OR_MISMATCH',
             f'block {number}', 'STOP')
    return {'number': int(raw['number'], 16), 'hash': raw['hash'], 'parentHash': raw['parentHash'],
            'timestamp': int(raw['timestamp'], 16)}


def _public_roots(auth, *paths):
    if auth['mode'] != 'SIMULATION':
        import fork_mode
        for path in paths:
            fork_mode.require_export_root(path, 'collection')


async def select_anchor(gate, now_utc):
    """S1 only. Opens the ledger's single selection invocation, reads chain id, head and ``head - depth`` through
    the gate (budgeted, paced) and pins the anchor. Returns the pinned record."""
    ledger = gate.ledger
    auth = ledger.authorization
    _require('scope' in auth and auth['stage'] == 'S1', 'ANCHOR_SELECTION_STAGE', 'only an S1 v2 authorization selects')
    _require(gate.chain == auth['chains'][0], 'AUTHORIZATION_SCOPE', f'{gate.chain} not authorized')
    _require(gate.simulation == (auth['mode'] == 'SIMULATION'), 'FR_D1_NOT_AUTHORIZED',
             'a simulation record drives only an injected transport, a public record only the native one')
    check_validity(auth, now_utc)
    _public_roots(auth, ledger.directory)
    policy, chain = auth['scope']['anchorPolicy'], gate.chain
    ledger.open(selection=True)
    try:
        lane = tp.Lane(tp.release_rpc(gate), gate)
        chain_id = await lane.call('eth_chainId', [])
        _require(isinstance(chain_id, str) and _QUANTITY.fullmatch(chain_id) is not None
                 and int(chain_id, 16) == auth['scope']['chainId'], 'RPC_CHAIN', repr(chain_id))
        head = _header(await lane.call('eth_getBlockByNumber', ['latest', False]))['number']
        block = head - policy['depthBlocks']
        _require(policy['minBlock'] <= block <= policy['maxBlock'], 'ANCHOR_OUT_OF_BOUNDS',
                 f'head {head} - {policy["depthBlocks"]} = {block} outside [{policy["minBlock"]}, {policy["maxBlock"]}]')
        anchor = _header(await lane.call('eth_getBlockByNumber', [hex(block), False]), block)
        _require(anchor['timestamp'] <= policy['notAfterTimestamp'], 'ANCHOR_OUT_OF_BOUNDS',
                 f'anchor timestamp {anchor["timestamp"]} after {policy["notAfterUtc"]}')
        return ledger.pin_anchor({'chain': chain, 'chainId': auth['scope']['chainId'], 'block': block,
                                  'hash': anchor['hash'], 'parentHash': anchor['parentHash'],
                                  'timestamp': anchor['timestamp'], 'head': head, 'rule': policy['rule'],
                                  'depthBlocks': policy['depthBlocks'], 'source': 'SELECTED',
                                  'selectedUtc': now_utc})
    finally:
        ledger.close()


# --------------------------------------------------------------------------------------- stage references
def inspect_ledger(directory, authorization_path, authorization_sha256, *, stage, chain, load):
    """Loads a referenced stage's authorization (``load`` = the collector loader, without the source binding of the
    referencing run's code) and verifies its finished ledger."""
    try:
        auth = load(authorization_path, authorization_sha256, bind_sources=False)
    except CollectionStop as stop:
        raise CollectionStop(f'{stage}_{stop.code}', stop.detail, 'REFUSED') from None
    _require('scope' in auth and auth['stage'] == stage and auth['chains'] == [chain], f'{stage}_AUTHORIZATION_STAGE',
             f'{auth.get("stage")} {auth.get("chains")}')
    ledger = tp.BudgetLedger(directory, auth, invocation=f'{stage.lower()}-reference').inspect()
    return auth, ledger


def s1_reference(s2_auth, s1_authorization_path, s1_authorization_sha256, s1_ledger_dir, *, load):
    """What an S2 run reproduces: the S1 ledger's pinned anchor and final spec (identity only, no S1 output)."""
    _require('scope' in s2_auth and s2_auth['stage'] == 'S2', 'S1_REFERENCE_STAGE', 'an S2 authorization is required')
    policy = s2_auth['scope']['anchorPolicy']
    _require(s1_authorization_sha256 == policy['s1AuthorizationSha256'], 'S1_REFERENCE_FOREIGN',
             'the S1 authorization is not the one this S2 authorization names')
    _require(s1_authorization_sha256 != s2_auth['sha256'], 'S1_REFERENCE_FOREIGN', 'S2 cannot reuse its own record')
    s1_auth, ledger = inspect_ledger(s1_ledger_dir, s1_authorization_path, s1_authorization_sha256, stage='S1',
                                     chain=s2_auth['chains'][0], load=load)
    _require(s1_auth['authorizationId'] == policy['s1AuthorizationId'], 'S1_REFERENCE_FOREIGN', 'S1 authorization id')
    _require(s2_auth['mode'] == 'SIMULATION' or s1_auth['mode'] == 'PUBLIC_READ_ONLY', 'S1_REFERENCE_FOREIGN',
             'a public S2 reproduces only a public S1 collection')
    _require(ledger.anchor is not None and ledger.specs, 'S1_EVIDENCE_INCOMPLETE', 'the S1 ledger pins no run',
             'BLOCKED')
    last = ledger.specs[-1]
    return {'authorizationSha256': s1_authorization_sha256, 'authorizationId': s1_auth['authorizationId'],
            'ledgerPath': str(ledger.path.resolve()), 'ledgerSeq': ledger.seq, 'ledgerHead': ledger.head,
            'anchor': ledger.anchor, 'specSha256': last['specSha256'], 'newVaults': last['newVaults']}


# --------------------------------------------------------------------------------------- run binding
def bind_run(collector):
    """Before the run's first read: the spec is at the pinned anchor, inside the envelope, never a narrowing of an
    earlier spec of this authorization, affordable, and its SHA-256 is pinned (or equals this run id's pin)."""
    ledger = collector.gate.ledger
    auth = ledger.authorization
    if 'scope' not in auth:
        return None
    check_validity(auth, collector.utc())
    spec, sha = collector.spec, collector.spec_sha256
    if auth['stage'] == 'S1':
        _require(collector.reference is None, 'S1_REFERENCE_UNEXPECTED', 'an S1 run selects its own anchor')
        _require(ledger.anchor is not None, 'ANCHOR_NOT_PINNED', 'run the S1 anchor selection first')
    else:
        ref = collector.reference
        _require(ref is not None and ref['authorizationSha256'] == auth['scope']['anchorPolicy']['s1AuthorizationSha256'],
                 'S1_REFERENCE_REQUIRED', 'an S2 run needs the verified S1 reference of this authorization')
        _require(ref['ledgerPath'] != str(ledger.path.resolve()), 'S1_REFERENCE_FOREIGN', 'S2 needs its own ledger')
        _require(sha == ref['specSha256'], 'S2_SPEC_NOT_S1_SPEC', 'S2 reproduces exactly the final S1 spec')
        if ledger.anchor is None:
            ledger.pin_anchor({**{k: ref['anchor'][k] for k in ('chain', 'chainId', 'block', 'hash', 'parentHash',
                                                                 'timestamp')},
                               'source': 'S1_LEDGER', 's1AuthorizationSha256': ref['authorizationSha256'],
                               's1LedgerSeq': ref['ledgerSeq'], 's1LedgerHead': ref['ledgerHead']})
        _require(all(ledger.anchor[k] == ref['anchor'][k] for k in ('chain', 'block', 'hash')), 'S1_REFERENCE_FOREIGN',
                 'the S2 ledger is pinned to another anchor')
    anchor = ledger.anchor
    _require(anchor['chain'] == spec['chain'] and spec['anchor'] == {'block': anchor['block'], 'hash': anchor['hash']},
             'SPEC_ANCHOR_NOT_PINNED', f'spec anchor {spec["anchor"]} is not the pinned anchor {anchor["block"]}')
    mine = [p for p in ledger.specs if p['runId'] == collector.run_id]
    if mine:
        _require(all(p['specSha256'] == sha for p in mine), 'SPEC_PIN_MISMATCH', f'{collector.run_id} is pinned to '
                 'another spec')
        return {'status': 'RESUMED', 'specSha256': sha}
    vaults = {tuple(v) for v in [[x['factory'], x['index'], x['vault']] for x in spec['newVaults']]}
    for prior in ledger.specs:
        _require({tuple(v) for v in prior['newVaults']} <= vaults, 'SPEC_REVISION_NOT_MONOTONE',
                 f'{prior["runId"]} pinned vaults this spec drops')
    lower = required_posts_lower_bound(spec, {int(r['blockNumber'], 16) for r in collector._b0_rows()})
    _require(ledger.used + lower <= ledger.ceiling, 'BUDGET_INFEASIBLE',
             f'{ledger.used} used + at least {lower} needed > {ledger.ceiling}')
    record = {'runId': collector.run_id, 'specSha256': sha, 'chain': spec['chain'], 'anchorBlock': anchor['block'],
              'anchorHash': anchor['hash'], 'newVaults': sorted([list(v) for v in vaults]),
              'lowerBoundPosts': lower, 'collector': collector.collector}
    ledger.pin_spec(record)
    return {'status': 'PINNED', **record}


# --------------------------------------------------------------------------------------- S3 fork anchor
def check_reconciliation(doc, raw_sha256, *, accepted):
    _require(isinstance(doc, dict) and set(doc) == RECONCILIATION_KEYS and doc['schema'] == RECONCILIATION_SCHEMA,
             'RECONCILIATION_KEYS')
    _require(raw_sha256 in accepted, 'RECONCILIATION_NOT_ACCEPTED',
             'no Agent A reconciliation of the S1/S2 evidence is accepted for this anchor', 'BLOCKED')
    _require(doc['verdict'] == 'RECONCILED' and doc['reviewer'] == 'Agent A', 'RECONCILIATION_VERDICT',
             f'{doc["verdict"]!r} by {doc["reviewer"]!r}', 'BLOCKED')


def bind_fork_anchor(auth, *, s1, s2, reconciliation, load, now_utc, accepted=None):
    """S3: the fork block and hash are the S1 ledger anchor, reproduced by S2 at the same final spec and reconciled
    by an accepted Agent A record. ``s1``/``s2`` are (authorization path, SHA-256, ledger directory);
    ``reconciliation`` is (path, SHA-256). Returns the authorization with the bound fork anchor."""
    _require('scope' in auth and auth['stage'] == 'S3', 'FORK_ANCHOR_STAGE', 'an S3 v2 authorization is required')
    check_validity(auth, now_utc)
    chain, policy = auth['chain'], auth['scope']['forkAnchor']
    _require(s1[1] == policy['s1AuthorizationSha256'] and s2[1] == policy['s2AuthorizationSha256'],
             'FORK_ANCHOR_FOREIGN', 'the S1/S2 authorizations are not the ones this S3 authorization names')
    a1, l1 = inspect_ledger(s1[2], s1[0], s1[1], stage='S1', chain=chain, load=load)
    a2, l2 = inspect_ledger(s2[2], s2[0], s2[1], stage='S2', chain=chain, load=load)
    if auth['mode'] != 'SIMULATION':
        _require(a1['mode'] == a2['mode'] == 'PUBLIC_READ_ONLY', 'FORK_ANCHOR_FOREIGN', 'public evidence required')
    _require(a2['scope']['anchorPolicy']['s1AuthorizationSha256'] == s1[1], 'FORK_ANCHOR_FOREIGN', 'S2 names another S1')
    _require(l1.anchor is not None and l1.specs and l2.anchor is not None and l2.specs, 'FORK_EVIDENCE_INCOMPLETE',
             'S1 and S2 must each pin an anchor and a spec', 'BLOCKED')
    final = l1.specs[-1]['specSha256']
    _require(all(l1.anchor[k] == l2.anchor[k] for k in ('chain', 'block', 'hash')) and l2.anchor['source'] == 'S1_LEDGER'
             and l2.anchor.get('s1AuthorizationSha256') == s1[1]
             and any(p['specSha256'] == final for p in l2.specs), 'FORK_ANCHOR_MISMATCH',
             'S2 did not reproduce the final S1 spec at the S1 anchor')
    raw, doc = read_record(reconciliation[0], reconciliation[1], 'RECONCILIATION_HASH')
    check_reconciliation(doc, reconciliation[1], accepted=approvals.RECONCILIATIONS if accepted is None else accepted)
    _require([doc['chain'], doc['chainId'], doc['anchorBlock'], doc['anchorHash'], doc['specSha256'],
              doc['s1AuthorizationSha256'], doc['s2AuthorizationSha256']]
             == [chain, h.CHAINS[chain], l1.anchor['block'], l1.anchor['hash'], final, s1[1], s2[1]],
             'FORK_ANCHOR_MISMATCH', 'the reconciliation record names other evidence')
    return {**auth, 'forkBlock': l1.anchor['block'], 'forkBlockHash': l1.anchor['hash'],
            'anchorBinding': {'s1LedgerHead': l1.head, 's2LedgerHead': l2.head, 'specSha256': final,
                              'reconciliationSha256': reconciliation[1]}}


def _block_ok(tag, block, block_hash):
    if isinstance(tag, str):
        return tag == hex(block)
    if isinstance(tag, dict):
        if set(tag) == {'blockNumber'}:
            return tag['blockNumber'] == hex(block)
        return set(tag) <= {'blockHash', 'requireCanonical'} and tag.get('blockHash') == block_hash and \
            isinstance(tag.get('requireCanonical', False), bool)
    return False


def check_fork_request(item, block, block_hash):
    """The parameter rule of a scoped S3 proxy: every state read is at the bound fork block."""
    method, params = item['method'], item.get('params', [])
    _require(isinstance(params, list), 'FORK_PARAMS', method)
    address = lambda v: isinstance(v, str) and _ADDRESS.fullmatch(v.lower()) is not None  # noqa: E731
    if method == 'eth_chainId':
        ok = params == []
    elif method == 'eth_getBlockByNumber':
        ok = len(params) == 2 and params[0] == hex(block) and isinstance(params[1], bool)
    elif method in ('eth_getBalance', 'eth_getTransactionCount', 'eth_getCode'):
        ok = len(params) == 2 and address(params[0]) and _block_ok(params[1], block, block_hash)
    elif method == 'eth_getStorageAt':
        ok = (len(params) == 3 and address(params[0]) and isinstance(params[1], str)
              and re.fullmatch(r'0x[0-9a-fA-F]{1,64}', params[1]) is not None and _block_ok(params[2], block, block_hash))
    else:
        ok = False
    _require(ok, 'FORK_PARAMS', f'{method} {json.dumps(params)[:160]} is not a read at fork block {block}')


def check_fork_response(items, body, chain_id, block_hash):
    """Raises on a chain id or fork-header answer that contradicts the bound anchor (the proxy then stops)."""
    try:
        out = json.loads(body)
    except ValueError:
        return
    answers = {a.get('id'): a for a in (out if isinstance(out, list) else [out]) if isinstance(a, dict)}
    for item in items:
        result = answers.get(item.get('id'), {}).get('result')
        if result is None:
            continue
        if item['method'] == 'eth_chainId':
            _require(result == hex(chain_id), 'FORK_CHAIN_MISMATCH', repr(result), 'STOP')
        elif item['method'] == 'eth_getBlockByNumber':
            _require(isinstance(result, dict) and result.get('hash') == block_hash, 'FORK_ANCHOR_MISMATCH',
                     f'upstream header {result.get("hash") if isinstance(result, dict) else result!r}', 'STOP')
