"""Offline tests of the FR-D1 scope binding (``fr_d1_scope``) and its hooks in the collector, ledger and proxy.

Run inside an approved export:
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_fr_d1.py' -v

Every chain is the FAKE chain of ``test_collector`` injected as the gate's ``post`` (SIMULATION ONLY). Public records
are only loaded, never run: a test that needs the native transport replaces ``native_rpc._post`` by a guard that
fails the test if it is ever called. The optional record-file test checks the prepared research records when
FAROO_FR_D1_RECORDS and FAROO_B0_INVENTORY_DIR are supplied; otherwise it is NOT RUN.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collection_plan as cp  # noqa: E402
import collector as col  # noqa: E402
import fork_mode as fm  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import fr_d1_collect as runner  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import history as h  # noqa: E402
import transport as tp  # noqa: E402
from test_collector import ANCHOR, FROM, Clock, HEAD, PIN, World, run  # noqa: E402
from transport import BudgetExhausted, CollectionStop  # noqa: E402
from tools.release.native import rpc as native_rpc  # noqa: E402

NOW = '2026-10-20T00:00:00Z'
VALIDITY = {'notBeforeUtc': '2026-10-18T00:00:00Z', 'notAfterUtc': '2026-12-31T23:59:59Z',
            'stopConditions': ['offline test']}
BASES = ('c' * 40, 'b' * 40)
SELECTED = HEAD - cp.FINALITY_DEPTH_ASSUMPTION
UPSTREAM = 'http://127.0.0.1:9/'


def s1_policy(**kw):
    return {'rule': 'HEAD_MINUS_DEPTH', 'depthBlocks': cp.FINALITY_DEPTH_ASSUMPTION, 'minBlock': PIN + 1,
            'maxBlock': 10_000, 'notAfterUtc': '2026-12-26T00:00:00Z', 'notAfterTimestamp': 1_800_000_000, **kw}


def write(root, doc):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'{doc["authorizationId"]}-{len(list(root.iterdir()))}.json'
    path.write_bytes(scope.record_bytes(doc))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def record(root, world, stage, *, mode='SIMULATION', ceiling=100_000, restarts=3, s1=None, edit=None, ident=None):
    """A v2 collection record for ``world`` (call under ``world.patch()``). ``s1`` = (id, sha) for S2."""
    chain = world.chain
    prefix = 'sim' if mode == 'SIMULATION' else 'fr-d1'
    policy = ({**s1_policy(), 'selection': 'ONCE_BEFORE_COLLECTION'} if stage == 'S1' else
              {'rule': 'S1_PINNED_SPEC', 's1AuthorizationId': s1[0], 's1AuthorizationSha256': s1[1]})
    doc = scope.collection_record(
        stage, chain, mode=mode, ident=ident or f'{prefix}-{stage.lower()}-{chain}-t', ceiling=ceiling,
        restarts=restarts, b0_targets=[t['address'] for t in cp.b0_targets(world.b0_plan)], anchor_policy=policy,
        validity=copy.deepcopy(VALIDITY), retention={'local': 'test directory'}, owner={'source': 'offline test'},
        statement='SIMULATION ONLY: offline test record' if mode == 'SIMULATION' else 'test record', bases=BASES)
    if edit:
        edit(doc)
    return write(Path(root) / 'records', doc)


def fork_record(root, chain, s1_sha, s2_sha, *, mode='SIMULATION', upstream=UPSTREAM, ceiling=20, edit=None):
    prefix = 'sim' if mode == 'SIMULATION' else 'fr-d1'
    doc = scope.fork_record(chain, mode=mode, ident=f'{prefix}-s3-{chain}-t', upstream=upstream, ceiling=ceiling,
                            restarts=1, s1_sha256=s1_sha, s2_sha256=s2_sha, validity=copy.deepcopy(VALIDITY),
                            retention='test directory', owner={'source': 'offline test'}, prerequisites=['test'],
                            statement='SIMULATION ONLY: offline test record' if mode == 'SIMULATION' else 'test',
                            bases=BASES)
    if edit:
        edit(doc)
    return write(Path(root) / 'records', doc)


def gate_for(path, sha, ledger_dir, fake, clock, invocation='inv'):
    auth = col.load_authorization(path, sha)
    ledger = tp.BudgetLedger(ledger_dir, auth, invocation=invocation, clock_ms=clock.ms)
    return tp.HttpGate(auth['chains'][0], ledger, clock_ms=clock.ms, sleep=clock.sleep, post=fake.post)


def kinds(ledger_dir):
    path = next(Path(ledger_dir).glob('*.jsonl'))
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def reply(match, result):
    """A fault answering every item that ``match``es with ``result`` (single or batched bodies)."""
    def fault(chain, body):
        items = body if isinstance(body, list) else [body]
        if not any(match(i) for i in items):
            return None
        out = [{'jsonrpc': '2.0', 'id': i['id'], 'result': result} if match(i) else chain.answer(i) for i in items]
        return 200, out if isinstance(body, list) else out[0]
    return fault


def guard(*args, **kwargs):
    raise AssertionError('the native transport was reached in an offline test')


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def stop(self, fn, code, status=None):
        with self.assertRaises(CollectionStop) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        if status:
            self.assertEqual(ctx.exception.status, status, str(ctx.exception))
        return ctx.exception

    def world(self, chain, sub='w', **kw):
        w = World(self.root / sub / chain, chain, **kw)
        patch = w.patch()
        patch.start()
        self.addCleanup(patch.stop)
        return w

    def select(self, w, path, sha, ledger_dir=None, clock=None):
        g = gate_for(path, sha, ledger_dir or self.root / 'budget-s1', w.fake, clock or Clock(), 'select')
        return run(scope.select_anchor(g, NOW))

    def collect(self, w, path, sha, *, ledger_dir=None, run_id='run-s1', anchor=SELECTED, new=None, reference=None,
                collector='S1', spec=None, invocation='collect'):
        clock = Clock()
        g = gate_for(path, sha, ledger_dir or self.root / 'budget-s1', w.fake, clock, invocation)
        spec = spec or w.spec(anchor=anchor, **({} if new is None else {'new': new}))
        c = w.collector(spec, g, clock, run_id=run_id, utc=NOW, collector=collector, reference=reference)
        return run(c.run(invocation)), g, spec

    def s1(self, w, **kw):
        path, sha = record(self.root, w, 'S1', **kw)
        self.select(w, path, sha)
        summary, g, spec = self.collect(w, path, sha)
        self.assertEqual(summary['status'], 'COLLECTED')
        return path, sha, spec

    def s2(self, w, s1_path, s1_sha, **kw):
        s1_auth = col.load_authorization(s1_path, s1_sha)
        path, sha = record(self.root, w, 'S2', s1=(s1_auth['authorizationId'], s1_sha), **kw)
        auth = col.load_authorization(path, sha)
        ref = scope.s1_reference(auth, s1_path, s1_sha, self.root / 'budget-s1', load=col.load_authorization)
        summary, g, spec = self.collect(w, path, sha, ledger_dir=self.root / 'budget-s2', run_id='run-s2',
                                        anchor=ref['anchor']['block'], new=[tuple(v) for v in ref['newVaults']],
                                        reference=ref, collector='S2')
        return path, sha, summary, spec


# ------------------------------------------------------------------------------------------------ records
class RecordTest(Base):
    def test_valid_records_load_for_every_stage_and_chain(self):
        for chain in ('testnet', 'mainnet'):
            w = self.world(chain)
            p1, s1 = record(self.root, w, 'S1')
            a1 = col.load_authorization(p1, s1)
            self.assertEqual((a1['stage'], a1['chains'], a1['scope']['endpoint']), ('S1', [chain], scope.endpoint(chain)))
            p2, s2 = record(self.root, w, 'S2', s1=(a1['authorizationId'], s1))
            self.assertEqual(col.load_authorization(p2, s2)['stage'], 'S2')
            p3, s3 = fork_record(self.root, chain, s1, s2)
            self.assertEqual(fm.load_authorization(p3, s3)['stage'], 'S3')
        self.assertNotIn('tests/fork/driver/fr_d1_approvals.py', scope.SOURCE_BINDING_FILES)
        self.assertEqual((approvals.COLLECTION, approvals.FORK, approvals.RECONCILIATIONS),
                         (frozenset({
                            '6963e58be871b15ff3be92bbd0654cbd784b183fecd1c9050379a6b1a10782c1',  # fr-d1-s1-testnet-sl20261018
                            'f4ca9cf3ccb8a5241aefaaa51497171e877f8f7e25f701bd6cdac19f2a4c28cf',  # fr-d1-s2-testnet-sl20261018
                            '00c6ab29bf49d545608306a1543b309b555e04e943ad3cd4358018227512b669',  # fr-d1-s1-mainnet-sl20261018
                            '71357ba54d74ccaa04aad6acb39dc02cacdda9993effd3fefa2e5618c05efe74',  # fr-d1-s2-mainnet-sl20261018
                        }),
                          frozenset({
                            '3dd3ab07248c6e74d156d93978d00ea63c5d2cd6767d867f53f610ae560fd96c',  # fr-d1-s3-testnet-sl20261018
                            '19442bc39ee398f7e9daa9bc71818bf78019ee196ed239cb07bd05e37a77599f',  # fr-d1-s3-mainnet-sl20261018
                        }), frozenset({
                            'a27ffd60d7105aa6aebfcc0d638da8b3488535b973e75f79245200d15888cf81',  # testnet S1/S2
                            '158afc230f468be8fb2ad5a68b4eb2036322c1c048fd8ef7ee68a106185bfd61',  # mainnet S1/S2
                        })))
        self.assertIs(col.APPROVED_COLLECTION_AUTHORIZATIONS, approvals.COLLECTION)
        self.assertIs(fm.APPROVED_FORK_AUTHORIZATIONS, approvals.FORK)

    def test_every_envelope_change_is_refused(self):
        w = self.world('mainnet')
        safe, stpros = h.GOV_SAFE, h.registry('mainnet')['stpros']
        cases = {
            'AUTHORIZATION_ENDPOINT': lambda d: d['scope'].update(endpoint=scope.endpoint('testnet')),
            'AUTHORIZATION_CHAIN': lambda d: d['scope'].update(chainId=688689),
            'AUTHORIZATION_B0': lambda d: d['scope']['b0'].update(endBlockHash='0x' + '11' * 32),
            'AUTHORIZATION_RANGE': lambda d: d['scope'].update(fromBlock=d['scope']['fromBlock'] - 1),
            'AUTHORIZATION_TARGETS': lambda d: d['scope']['targets'].update(extraTargets=[safe]),
            'AUTHORIZATION_TARGETS ': lambda d: d['scope']['targets'].update(extraSegment={
                'id': 'S2', 'kind': 'extra-pre-pin', 'start': FROM, 'end': PIN, 'addresses': [safe]}),
            'AUTHORIZATION_TARGETS  ': lambda d: d['scope']['targets']['b0Targets'].remove(stpros),
            'AUTHORIZATION_NEW_VAULTS': lambda d: d['scope']['newVaults'].update(nonceOffset=4),
            'AUTHORIZATION_HEADERS': lambda d: d['scope'].update(headerScope='H2'),
            'AUTHORIZATION_HEADERS ': lambda d: d['scope'].update(b0EventBlockRecheck=False),
            'AUTHORIZATION_CHECKPOINTS': lambda d: d['scope'].update(checkpoints=['S2']),
            'AUTHORIZATION_METHODS': lambda d: d['methods'].remove('eth_getBalance'),
            'AUTHORIZATION_METHODS ': lambda d: d['scope']['methodRestrictions']['eth_getBalance'].update(address=safe),
            'AUTHORIZATION_KEYS': lambda d: d['methods'].append('eth_sendRawTransaction'),
            'AUTHORIZATION_KEYS ': lambda d: d.update(chains=['mainnet', 'testnet']),
            'AUTHORIZATION_KEYS  ': lambda d: d.update(stage='S3'),
            'AUTHORIZATION_KEYS   ': lambda d: d['scope'].pop('owner'),
            'AUTHORIZATION_TRANSPORT': lambda d: d['scope']['transport'].update(pacingMsMin=100),
            'AUTHORIZATION_TRANSPORT ': lambda d: d['scope']['transport'].update(maxRetriesPerPost=5),
            'AUTHORIZATION_ANCHOR_POLICY': lambda d: d['scope']['anchorPolicy'].update(depthBlocks=10),
            'AUTHORIZATION_ANCHOR_POLICY ': lambda d: d['scope']['anchorPolicy'].update(minBlock=PIN),
            'AUTHORIZATION_ANCHOR_POLICY  ': lambda d: d['scope']['anchorPolicy'].update(rule='OPERATOR_CHOICE'),
            'AUTHORIZATION_VALIDITY': lambda d: d['scope']['validity'].update(notAfterUtc='2026-10-01T00:00:00Z'),
            'AUTHORIZATION_SOURCE_BINDING': lambda d: d['scope']['sourceBinding']['files'].update(
                {'tests/fork/driver/transport.py': '0' * 64}),
            'AUTHORIZATION_SOURCE_BINDING ': lambda d: d['scope']['sourceBinding']['files'].pop('tools/lib/rpc.py'),
            'AUTHORIZATION_ID': lambda d: d.update(authorizationId='sim-s2-mainnet-t'),
        }
        for code, edit in cases.items():
            path, sha = record(self.root, w, 'S1', edit=edit)
            self.stop(lambda: col.load_authorization(path, sha), code.strip(), 'REFUSED')

    def test_owner_ceilings_and_restarts_cannot_be_raised(self):
        w = self.world('mainnet')
        for stage, ceiling, restarts, code in (('S1', 2001, 3, 'AUTHORIZATION_CEILING'),
                                               ('S1', 2000, 4, 'AUTHORIZATION_RESTARTS'),
                                               ('S2', 1001, 3, 'AUTHORIZATION_CEILING')):
            path, sha = record(self.root, w, stage, mode='PUBLIC_READ_ONLY', ceiling=ceiling, restarts=restarts,
                               s1=('fr-d1-s1-mainnet-t', 'a' * 64))
            self.stop(lambda: col.load_authorization(path, sha), code, 'REFUSED')
        path, sha = record(self.root, w, 'S1', mode='PUBLIC_READ_ONLY', ceiling=2000, restarts=3)
        self.stop(lambda: col.load_authorization(path, sha), 'FR_D1_NOT_AUTHORIZED', 'REFUSED')
        p3, s3 = fork_record(self.root, 'mainnet', 'a' * 64, 'b' * 64, mode='LOCAL_FORK_PUBLIC_READ_ONLY',
                             upstream=scope.endpoint('mainnet'), ceiling=20001)
        self.stop(lambda: fm.load_authorization(p3, s3), 'AUTHORIZATION_CEILING')
        p3, s3 = fork_record(self.root, 'mainnet', 'a' * 64, 'b' * 64, mode='LOCAL_FORK_PUBLIC_READ_ONLY',
                             upstream=scope.endpoint('mainnet'), ceiling=20000)
        self.stop(lambda: fm.load_authorization(p3, s3), 'FR_D1_NOT_AUTHORIZED')

    def test_missing_or_modified_records_fail_closed(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1')
        self.stop(lambda: col.load_authorization(self.root / 'absent.json', sha), 'AUTHORIZATION_MISSING')
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"maxHttpPosts": 100000', b'"maxHttpPosts": 100001'))
        self.stop(lambda: col.load_authorization(path, sha), 'AUTHORIZATION_HASH')
        path.write_bytes(b'not json')
        self.stop(lambda: col.load_authorization(path, hashlib.sha256(b'not json').hexdigest()), 'AUTHORIZATION_KEYS')
        p3, s3 = fork_record(self.root, 'testnet', 'a' * 64, 'b' * 64)
        self.stop(lambda: col.load_authorization(p3, s3), 'AUTHORIZATION_KEYS')        # S3 record in a collector
        p1, s1 = record(self.root, w, 'S1')
        self.stop(lambda: fm.load_authorization(p1, s1), 'AUTHORIZATION_KEYS')         # S1 record in the proxy

    def test_code_drift_of_the_b0_pin_or_an_extra_target_voids_the_record(self):
        w = self.world('mainnet')
        path, sha = record(self.root, w, 'S1')
        with mock.patch.dict(h.B0_PREFIX, {'mainnet': {**h.B0_PREFIX['mainnet'], 'chunkCount': 1}}):
            self.stop(lambda: col.load_authorization(path, sha), 'AUTHORIZATION_B0')
        self.assertEqual(json.loads(path.read_bytes())['scope']['targets']['extraSegment'], None)
        with mock.patch.dict(h.EXTRA_TARGETS, {'mainnet': (h.GOV_SAFE,)}):      # restoring the Safe history scan
            self.stop(lambda: col.load_authorization(path, sha), 'AUTHORIZATION_TARGETS')
        real = scope.source_files()
        with mock.patch.object(scope, 'source_files', lambda: {**real, 'tools/release/registry.json': '1' * 64}):
            self.stop(lambda: col.load_authorization(path, sha), 'AUTHORIZATION_SOURCE_BINDING')
            # a referenced earlier-stage record stays identifiable by its raw SHA-256 after a later source change
            self.assertEqual(col.load_authorization(path, sha, bind_sources=False)['sha256'], sha)


# ------------------------------------------------------------------------------------------------ S1 anchor
class AnchorSelectionTest(Base):
    def test_selection_pins_head_minus_depth_inside_the_budget(self):
        for chain in ('testnet', 'mainnet'):
            w = self.world(chain)
            path, sha = record(self.root, w, 'S1', ident=f'sim-s1-{chain}-sel')
            ledger_dir = self.root / f'budget-{chain}'
            anchor = self.select(w, path, sha, ledger_dir)
            self.assertEqual((anchor['block'], anchor['hash'], anchor['head']), (SELECTED, w.fake.hash(SELECTED), HEAD))
            self.assertEqual(anchor['source'], 'SELECTED')
            records = kinds(ledger_dir)
            self.assertEqual([r['kind'] for r in records], ['genesis', 'select', 'attempt', 'attempt', 'attempt',
                                                            'anchor', 'close'])
            self.assertEqual(w.fake.posts, 3)
            # a second selection is refused without any request, and the pin survives
            self.stop(lambda: self.select(w, path, sha, ledger_dir), 'ANCHOR_ALREADY_PINNED', 'REFUSED')
            self.assertEqual(w.fake.posts, 3)
            self.assertEqual(tp.BudgetLedger(ledger_dir, col.load_authorization(path, sha), invocation='i').inspect()
                             .anchor['block'], SELECTED)

    def test_anchor_bounds_chain_id_and_validity(self):
        w = self.world('testnet')
        cases = (('ANCHOR_OUT_OF_BOUNDS', lambda d: d['scope']['anchorPolicy'].update(maxBlock=SELECTED - 1), None),
                 ('ANCHOR_OUT_OF_BOUNDS', lambda d: d['scope']['anchorPolicy'].update(notAfterTimestamp=1_700_000_000),
                  None),
                 ('ANCHOR_OUT_OF_BOUNDS', None, lambda: setattr(w.fake, 'head', PIN + 100)),
                 ('RPC_CHAIN', None, lambda: w.fake.faults.append(reply(lambda i: i['method'] == 'eth_chainId', '0x1'))))
        for i, (code, edit, fault) in enumerate(cases):
            w.fake.head, w.fake.faults[:] = HEAD, []
            if fault:
                fault()
            path, sha = record(self.root, w, 'S1', edit=edit)
            ledger_dir = self.root / f'b{i}'
            self.stop(lambda: self.select(w, path, sha, ledger_dir), code)
            self.assertIsNone(tp.BudgetLedger(ledger_dir, col.load_authorization(path, sha), invocation='i')
                              .inspect().anchor)
        path, sha = record(self.root, w, 'S1')
        g = gate_for(path, sha, self.root / 'late', w.fake, Clock())
        self.stop(lambda: run(scope.select_anchor(g, '2027-01-01T00:00:00Z')), 'AUTHORIZATION_EXPIRED')

    def test_selection_is_charged_and_stops_at_the_ceiling(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1', ceiling=2)
        with self.assertRaises(BudgetExhausted):
            self.select(w, path, sha)
        self.assertEqual(w.fake.posts, 2)
        self.assertIsNone(tp.BudgetLedger(self.root / 'budget-s1', col.load_authorization(path, sha),
                                          invocation='i').inspect().anchor)

    def test_selection_order_stage_and_transport_mode(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1')
        self.stop(lambda: self.collect(w, path, sha), 'ANCHOR_NOT_PINNED', 'REFUSED')       # collection opened first
        self.assertEqual(w.fake.posts, 0)
        self.stop(lambda: self.select(w, path, sha), 'ANCHOR_SELECTION_ORDER', 'REFUSED')
        a1 = col.load_authorization(path, sha)
        p2, s2 = record(self.root, w, 'S2', s1=(a1['authorizationId'], sha))
        self.stop(lambda: self.select(w, p2, s2, self.root / 'b2'), 'ANCHOR_SELECTION_STAGE')
        p1, s1 = record(self.root, w, 'S1', mode='PUBLIC_READ_ONLY', ceiling=400)
        with mock.patch.object(col, 'APPROVED_COLLECTION_AUTHORIZATIONS', frozenset({s1})):
            auth = col.load_authorization(p1, s1)
        ledger = tp.BudgetLedger(self.root / 'b3', auth, invocation='i')
        g = tp.HttpGate('testnet', ledger, post=w.fake.post)                  # public record, injected transport
        self.stop(lambda: run(scope.select_anchor(g, NOW)), 'FR_D1_NOT_AUTHORIZED')
        with mock.patch.object(native_rpc, '_post', guard):                  # public record, native transport
            g = tp.HttpGate('testnet', tp.BudgetLedger(self.root / 'b4', auth, invocation='i'), approved={s1})
            self.stop(lambda: run(scope.select_anchor(g, NOW)), 'EXPORT_ROOT')
        self.assertEqual(w.fake.posts, 0)

    def test_selection_is_not_a_restart_but_a_repeat_is(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1', restarts=1)
        w.fake.faults.append(reply(lambda i: i['method'] == 'eth_getBlockByNumber' and i['params'][0] == hex(SELECTED),
                                   None))
        self.stop(lambda: self.select(w, path, sha), 'HEADER_MISSING_OR_MISMATCH')   # selection 1: no anchor
        w.fake.faults[:] = []
        self.select(w, path, sha)                                                    # selection 2: counts as a restart
        self.collect(w, path, sha)                                                   # first collection invocation
        self.stop(lambda: self.collect(w, path, sha, invocation='again'), 'SUPERVISOR_BOUND', 'REFUSED')


# ------------------------------------------------------------------------------------------------ S1 runs
class ScopedCollectionTest(Base):
    def test_the_lower_bound_counts_the_gas_envelope_read(self):
        for chain in ('testnet', 'mainnet'):
            spec = self.world(chain, sub=chain).spec(anchor=SELECTED)
            self.assertEqual(spec['extraTargets'], [])
            with_read = scope.required_posts_lower_bound(spec, [])
            with mock.patch.dict(cp.GAS_ENVELOPE, {chain: ()}):
                self.assertEqual(scope.required_posts_lower_bound(spec, []), with_read - 1)

    def test_s1_collects_at_the_pinned_anchor_and_pins_the_spec_first(self):
        for chain in ('testnet', 'mainnet'):
            w = self.world(chain, sub=chain)
            path, sha = record(self.root / chain, w, 'S1')
            ledger_dir = self.root / chain / 'budget'
            self.select(w, path, sha, ledger_dir)
            summary, g, spec = self.collect(w, path, sha, ledger_dir=ledger_dir)
            self.assertEqual(summary['status'], 'COLLECTED')
            self.assertEqual(col.content_verify(w.root, 'run-s1', spec)[0]['status'], 'CONTENT_VERIFIED')
            records = kinds(ledger_dir)
            order = [r['kind'] for r in records]
            first_open = order.index('open')
            self.assertLess(order.index('anchor'), first_open)
            self.assertEqual(order[first_open + 1], 'spec')                  # pinned before any collection POST
            pinned = records[first_open + 1]
            self.assertEqual(pinned['specSha256'], hashlib.sha256(cp.canonical_bytes(spec)).hexdigest())
            self.assertEqual((pinned['anchorBlock'], pinned['anchorHash']), (SELECTED, w.fake.hash(SELECTED)))
            self.assertLessEqual(pinned['lowerBoundPosts'], g.ledger.used - 3)
            self.assertEqual(g.ledger.used, w.fake.posts)
            journal = [json.loads(x) for x in (w.root / 'run-s1/journal.jsonl').read_bytes().splitlines()]
            self.assertEqual(journal[1]['event'], 'scopePin')
            # a resume of the same run id re-checks the pin and sends nothing new for the cached work
            again, _, _ = self.collect(w, path, sha, ledger_dir=ledger_dir, invocation='resume')
            self.assertEqual(again['files'], summary['files'])

    def test_a_recomputed_spec_hash_never_changes_the_scope(self):
        w = self.world('mainnet')
        path, sha = record(self.root, w, 'S1')
        self.select(w, path, sha)
        posts = w.fake.posts
        # another anchor: a perfectly derived spec with its own SHA-256, refused before any request
        self.stop(lambda: self.collect(w, path, sha, anchor=ANCHOR), 'SPEC_ANCHOR_NOT_PINNED')
        self.stop(lambda: self.collect(w, path, sha, run_id='r2', spec=w.spec(anchor=SELECTED, b0_event_block_recheck=False)),
                  'SPEC_OUT_OF_SCOPE')
        with mock.patch.object(cp, 'APPROVED_HEADER_SCOPES', frozenset({'H1', 'H2'})):
            self.stop(lambda: self.collect(w, path, sha, run_id='r3', spec=w.spec(anchor=SELECTED, header_scope='H2')),
                      'SPEC_OUT_OF_SCOPE')
        extra = '0x' + '5f' * 20
        with mock.patch.dict(h.EXTRA_TARGETS, {'mainnet': (extra,)}):
            self.stop(lambda: self.collect(w, path, sha, run_id='r4', spec=w.spec(anchor=SELECTED)),
                      'AUTHORIZATION_TARGETS')
        self.assertEqual(w.fake.posts, posts)

    def test_the_run_pin_is_frozen_and_survives_a_deleted_run_root(self):
        w = self.world('testnet', new_vault=True)
        path, sha = record(self.root, w, 'S1')
        self.select(w, path, sha)
        self.collect(w, path, sha)
        shutil.rmtree(w.root / 'run-s1')
        self.stop(lambda: self.collect(w, path, sha, new=[]), 'SPEC_PIN_MISMATCH', 'REFUSED')

    def test_new_vaults_follow_the_creation_rule_and_only_grow(self):
        w = self.world('testnet', new_vault=True)
        path, sha = record(self.root, w, 'S1')
        self.select(w, path, sha)
        self.stop(lambda: self.collect(w, path, sha, run_id='r0', new=[]), 'NEW_VAULT_REQUIRES_SPEC', 'INCOMPLETE')
        summary, _, _ = self.collect(w, path, sha, run_id='r1')                  # the discovered CREATE vault
        self.assertEqual(summary['status'], 'COLLECTED')
        self.stop(lambda: self.collect(w, path, sha, run_id='r2', new=[]), 'SPEC_REVISION_NOT_MONOTONE')
        factory = w.factories[0]
        fake_vault = (factory, 2, '0x' + '99' * 20)
        with self.assertRaises(h.HistoryRefused) as ctx:
            w.spec(anchor=SELECTED, new=[w.new_vault, fake_vault])
        self.assertEqual(ctx.exception.code, 'UNKNOWN_CREATION_PATH')
        with mock.patch.object(scope, 'expected_new_vaults', lambda chain: None):
            self.stop(lambda: col.load_authorization(path, sha), 'AUTHORIZATION_NEW_VAULTS')

    def test_an_unaffordable_run_is_refused_before_its_first_read(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1', ceiling=5)
        self.select(w, path, sha)
        self.stop(lambda: self.collect(w, path, sha), 'BUDGET_INFEASIBLE', 'REFUSED')
        self.assertEqual(w.fake.posts, 3)
        self.assertNotIn('spec', [r['kind'] for r in kinds(self.root / 'budget-s1')])

    def test_public_v1_records_need_the_v2_envelope_even_when_pinned(self):
        w = self.world('testnet')
        export = self.root / 'export'
        area = export / 'cache_forge/rehearsal/fork-campaign/collection'
        area.mkdir(parents=True)
        (export / 'EXPORT-MANIFEST.txt').write_text('{}')
        doc = {'schema': col.AUTHORIZATION_SCHEMA, 'authorizationId': 'v1-public', 'mode': 'PUBLIC_READ_ONLY',
               'chains': ['testnet'], 'methods': list(col.AUTHORIZED_METHODS), 'maxHttpPosts': 400, 'maxRestarts': 3,
               'statement': 'v1 record'}
        p = self.root / 'v1.json'
        p.write_text(json.dumps(doc))
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        w2 = World(area / 'history', 'testnet')
        with mock.patch.object(col, 'APPROVED_COLLECTION_AUTHORIZATIONS', frozenset({sha})), \
                mock.patch.object(native_rpc, '_post', guard), w2.patch():
            auth = col.load_authorization(p, sha)
            g = tp.HttpGate('testnet', tp.BudgetLedger(area / 'budget', auth, invocation='i'), approved={sha})
            c = w2.collector(w2.spec(), g, Clock())
            self.stop(c.preflight, 'AUTHORIZATION_SCOPE_REQUIRED', 'REFUSED')
        self.assertEqual(w.fake.posts, 0)


# ------------------------------------------------------------------------------------------------ S2 and S3
class StageSeparationTest(Base):
    def test_s2_reproduces_the_final_s1_spec_under_its_own_ledger(self):
        w = self.world('mainnet')
        p1, s1, spec1 = self.s1(w)
        p2, s2, summary, spec2 = self.s2(w, p1, s1)
        self.assertEqual(summary['status'], 'COLLECTED')
        self.assertEqual(spec1, spec2)
        records = kinds(self.root / 'budget-s2')
        anchor = next(r for r in records if r['kind'] == 'anchor')
        self.assertEqual((anchor['source'], anchor['s1AuthorizationSha256'], anchor['block']), ('S1_LEDGER', s1, SELECTED))
        self.assertNotIn('select', [r['kind'] for r in records])
        # the S1 record stays a valid reference after a later change of the enforcing sources
        a2 = col.load_authorization(p2, s2)
        real = scope.source_files()
        with mock.patch.object(scope, 'source_files', lambda: {**real, 'tests/fork/driver/collector.py': '2' * 64}):
            ref = scope.s1_reference(a2, p1, s1, self.root / 'budget-s1', load=col.load_authorization)
            self.assertEqual(ref['specSha256'], hashlib.sha256(cp.canonical_bytes(spec1)).hexdigest())
            self.stop(lambda: col.load_authorization(p2, s2), 'AUTHORIZATION_SOURCE_BINDING')
        self.assertEqual(records[0]['authorizationSha256'], s2)
        self.assertEqual(kinds(self.root / 'budget-s1')[0]['authorizationSha256'], s1)

    def test_s2_refuses_any_other_s1_evidence(self):
        w = self.world('testnet')
        p1, s1, _ = self.s1(w)
        a1 = col.load_authorization(p1, s1)
        p2, s2 = record(self.root, w, 'S2', s1=(a1['authorizationId'], s1))
        a2 = col.load_authorization(p2, s2)
        load = col.load_authorization
        ref = scope.s1_reference(a2, p1, s1, self.root / 'budget-s1', load=load)
        # no reference, a reference from another S1 record, an S1 run carrying a reference
        self.stop(lambda: self.collect(w, p2, s2, ledger_dir=self.root / 'b2', run_id='x1'), 'S1_REFERENCE_REQUIRED')
        po, so = record(self.root, w, 'S1', ident='sim-s1-testnet-other')
        self.stop(lambda: scope.s1_reference(a2, po, so, self.root / 'budget-s1', load=load), 'S1_REFERENCE_FOREIGN')
        self.stop(lambda: self.collect(w, p1, s1, run_id='x2', reference=ref), 'S1_REFERENCE_UNEXPECTED')
        # S2 cannot run S1's ledger, nor another spec than the final S1 spec
        bad = {**ref, 'ledgerPath': str((self.root / 'b3' / f'{a2["authorizationId"]}.jsonl').resolve())}
        self.stop(lambda: self.collect(w, p2, s2, ledger_dir=self.root / 'b3', run_id='x3', reference=bad),
                  'S1_REFERENCE_FOREIGN')
        self.stop(lambda: self.collect(w, p2, s2, ledger_dir=self.root / 'b4', run_id='x4', reference=ref, anchor=ANCHOR),
                  'S2_SPEC_NOT_S1_SPEC')
        # missing, tampered, in-use or empty S1 evidence
        self.stop(lambda: scope.s1_reference(a2, p1, s1, self.root / 'nowhere', load=load), 'BUDGET_LEDGER_MISSING')
        self.stop(lambda: scope.s1_reference(a2, self.root / 'gone.json', s1, self.root / 'budget-s1', load=load),
                  'S1_AUTHORIZATION_MISSING')
        ledger = self.root / 'budget-s1' / f'{a1["authorizationId"]}.jsonl'
        raw = ledger.read_bytes()
        ledger.write_bytes(raw.replace(b'"kind":"anchor"', b'"kind":"anchor" '))
        self.stop(lambda: scope.s1_reference(a2, p1, s1, self.root / 'budget-s1', load=load), 'BUDGET_LEDGER_CORRUPT')
        ledger.write_bytes(raw)
        lock = ledger.with_suffix('.lock')
        lock.write_text('{}')
        self.stop(lambda: scope.s1_reference(a2, p1, s1, self.root / 'budget-s1', load=load), 'BUDGET_LEDGER_LOCKED')
        lock.unlink()
        pe, se = record(self.root, w, 'S1', ident='sim-s1-testnet-empty')
        pe2, se2 = record(self.root, w, 'S2', s1=('sim-s1-testnet-empty', se))
        self.select(w, pe, se, self.root / 'be')
        self.stop(lambda: scope.s1_reference(load(pe2, se2), pe, se, self.root / 'be', load=load),
                  'S1_EVIDENCE_INCOMPLETE', 'BLOCKED')

    def test_a_raised_ceiling_under_the_same_id_is_a_foreign_ledger(self):
        w = self.world('testnet')
        p1, s1 = record(self.root, w, 'S1', ceiling=100)
        self.select(w, p1, s1)
        p1b, s1b = record(self.root, w, 'S1', ceiling=200)
        self.stop(lambda: self.select(w, p1b, s1b), 'BUDGET_LEDGER_FOREIGN', 'REFUSED')

    def fork_evidence(self, chain='mainnet'):
        w = self.world(chain)
        p1, s1, spec = self.s1(w)
        p2, s2, _, _ = self.s2(w, p1, s1)
        p3, s3 = fork_record(self.root, chain, s1, s2)
        rec = {'schema': scope.RECONCILIATION_SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain],
               'anchorBlock': SELECTED, 'anchorHash': w.fake.hash(SELECTED),
               'specSha256': hashlib.sha256(cp.canonical_bytes(spec)).hexdigest(), 's1AuthorizationSha256': s1,
               's2AuthorizationSha256': s2, 's1ManifestSha256': '1' * 64, 's2ManifestSha256': '2' * 64,
               'comparisonSha256': '3' * 64, 'verdict': 'RECONCILED', 'reviewer': 'Agent A', 'statement': 'test'}
        rp = self.root / 'reconciliation.json'
        rp.write_bytes(scope.record_bytes(rec))
        rsha = hashlib.sha256(rp.read_bytes()).hexdigest()
        args = {'s1': (p1, s1, self.root / 'budget-s1'), 's2': (p2, s2, self.root / 'budget-s2'),
                'reconciliation': (rp, rsha), 'load': col.load_authorization, 'now_utc': NOW}
        return w, fm.load_authorization(p3, s3), args, rec, rsha

    def test_s3_fork_anchor_binds_only_to_reconciled_s1_s2_evidence(self):
        w, auth, args, rec, rsha = self.fork_evidence()
        self.stop(lambda: scope.bind_fork_anchor(auth, **args), 'RECONCILIATION_NOT_ACCEPTED', 'BLOCKED')
        self.stop(lambda: scope.bind_fork_anchor(auth, **{**args, 'now_utc': '2027-01-01T00:00:00Z'}, accepted={rsha}),
                  'AUTHORIZATION_EXPIRED')
        bound = scope.bind_fork_anchor(auth, **args, accepted={rsha})
        self.assertEqual((bound['forkBlock'], bound['forkBlockHash']), (SELECTED, w.fake.hash(SELECTED)))
        self.stop(lambda: scope.bind_fork_anchor(auth, **{**args, 's1': args['s2'], 's2': args['s1']}, accepted={rsha}),
                  'FORK_ANCHOR_FOREIGN')
        self.stop(lambda: scope.bind_fork_anchor(auth, **{**args, 'reconciliation': (self.root / 'none', rsha)},
                                                 accepted={rsha}), 'RECONCILIATION_MISSING')
        other = self.root / 'other.json'
        other.write_bytes(scope.record_bytes({**rec, 'anchorBlock': SELECTED - 1}))
        osha = hashlib.sha256(other.read_bytes()).hexdigest()
        self.stop(lambda: scope.bind_fork_anchor(auth, **{**args, 'reconciliation': (other, osha)}, accepted={osha}),
                  'FORK_ANCHOR_MISMATCH')
        # a proxy needs the binding, and a ledger of its own authorization
        self.stop(lambda: fm.ForkBudgetProxy(auth, None), 'FORK_ANCHOR_NOT_BOUND', 'BLOCKED')
        s1_auth = col.load_authorization(args['s1'][0], args['s1'][1])
        foreign = tp.BudgetLedger(self.root / 'budget-s1', s1_auth, invocation='f')
        self.stop(lambda: fm.ForkBudgetProxy(bound, foreign), 'FORK_LEDGER_FOREIGN')

    def test_s3_proxy_forwards_only_reads_at_the_fork_block_and_stops_on_a_contradiction(self):
        w, auth, args, rec, rsha = self.fork_evidence()
        bound = scope.bind_fork_anchor(auth, **args, accepted={rsha})
        block, fork_hash = hex(bound['forkBlock']), bound['forkBlockHash']
        answers = {'hash': fork_hash, 'chain': hex(1672)}

        def forward(url, raw, timeout):
            item = json.loads(raw)
            result = {'eth_chainId': answers['chain'], 'eth_getBlockByNumber': {'hash': answers['hash']}}.get(
                item['method'], '0x0')
            return 200, json.dumps({'jsonrpc': '2.0', 'id': item['id'], 'result': result}).encode()
        ledger = tp.BudgetLedger(self.root / 'budget-s3', bound, invocation='p').open()
        self.addCleanup(ledger.close)
        proxy = fm.ForkBudgetProxy(bound, ledger, forward=forward, clock_s=lambda: 0.0, sleep=lambda s: None)
        self.addCleanup(proxy.server.server_close)
        a = '0x' + '12' * 20

        def call(method, params):
            return proxy.handle(json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': method, 'params': params}).encode())[0]
        self.assertEqual(call('eth_chainId', []), 200)
        self.assertEqual(call('eth_getBlockByNumber', [block, False]), 200)
        self.assertEqual(call('eth_getBalance', [a, block]), 200)
        self.assertEqual(call('eth_getStorageAt', [a, '0x0', {'blockHash': fork_hash}]), 200)
        self.assertEqual(call('eth_getCode', [a, {'blockNumber': block}]), 200)
        used = ledger.used
        for method, params in (('eth_getBalance', [a, 'latest']), ('eth_getCode', [a, hex(bound['forkBlock'] + 1)]),
                               ('eth_getBlockByNumber', ['latest', False]), ('eth_getStorageAt', [a, '0x0', 'pending']),
                               ('eth_getProof', [a, [], block]), ('eth_getLogs', [{}]), ('eth_sendRawTransaction', ['0x'])):
            self.assertEqual(call(method, params), 403, method)
        self.assertEqual(ledger.used, used)                                   # refused before it is charged
        answers['hash'] = '0x' + 'ee' * 32                                     # the upstream contradicts the anchor
        self.assertEqual(call('eth_getBlockByNumber', [block, False]), 403)
        self.assertEqual(proxy.stopped.code, 'FORK_ANCHOR_MISMATCH')
        self.assertEqual(call('eth_chainId', []), 403)                        # final: nothing further is forwarded
        self.assertEqual(ledger.used, used + 1)

    def test_public_fork_records_need_v2_and_the_measured_methods_only(self):
        p, s = fork_record(self.root, 'testnet', 'a' * 64, 'b' * 64, mode='LOCAL_FORK_PUBLIC_READ_ONLY',
                           upstream=scope.endpoint('testnet'), edit=lambda d: d['methods'].append('eth_getProof'))
        self.stop(lambda: fm.load_authorization(p, s), 'AUTHORIZATION_METHODS')
        doc = {'schema': fm.SCHEMA, 'authorizationId': 'v1-public', 'mode': 'LOCAL_FORK_PUBLIC_READ_ONLY',
               'chain': 'testnet', 'endpoint': scope.endpoint('testnet'), 'methods': list(fm.FORK_METHODS),
               'forkBlock': 10, 'forkBlockHash': '0x' + 'ab' * 32, 'maxHttpPosts': 3, 'maxRestarts': 1,
               'pacingMs': 350, 'retention': 'test', 'statement': 'v1'}
        p = self.root / 'v1-fork.json'
        p.write_text(json.dumps(doc))
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        auth = fm.load_authorization(p, sha, approved={sha})
        with mock.patch.object(fm, 'APPROVED_FORK_AUTHORIZATIONS', frozenset({sha})):
            self.stop(lambda: fm.ForkBudgetProxy(auth, None), 'FORK_SCOPE_REQUIRED')
        # a hand-made binding of a pinned public record is refused without an accepted reconciliation
        p, s = fork_record(self.root, 'testnet', 'a' * 64, 'b' * 64, mode='LOCAL_FORK_PUBLIC_READ_ONLY',
                           upstream=scope.endpoint('testnet'))
        made = {**fm.load_authorization(p, s, approved={s}), 'forkBlock': 10, 'forkBlockHash': '0x' + 'ab' * 32,
                'anchorBinding': {'reconciliationSha256': 'c' * 64}}
        with mock.patch.object(fm, 'APPROVED_FORK_AUTHORIZATIONS', frozenset({s})):
            self.stop(lambda: fm.ForkBudgetProxy(made, None), 'FORK_ANCHOR_NOT_BOUND', 'BLOCKED')


class RunnerTest(Base):
    def test_check_reports_and_refusals_are_structured(self):
        w = self.world('testnet')
        path, sha = record(self.root, w, 'S1', mode='PUBLIC_READ_ONLY', ceiling=400)
        out = []
        with mock.patch('builtins.print', lambda text: out.append(json.loads(text))):
            self.assertEqual(runner.main(['check', '--authorization', str(path), '--sha256', sha]), 1)
            self.assertEqual(out[-1]['code'], 'FR_D1_NOT_AUTHORIZED')
            path2, sha2 = record(self.root, w, 'S1')
            self.assertEqual(runner.main(['check', '--authorization', str(path2), '--sha256', sha2]), 0)
            self.assertEqual((out[-1]['status'], out[-1]['stage']), ('LOADED', 'S1'))


# ------------------------------------------------------------------------------------------------ prepared records
class PreparedRecordsTest(unittest.TestCase):
    """The research FR-D1 records: every envelope check passes; each record loads exactly when it is pinned."""

    def test_prepared_records(self):
        directory, b0 = os.environ.get('FAROO_FR_D1_RECORDS'), os.environ.get('FAROO_B0_INVENTORY_DIR')
        if not directory or not b0:
            self.skipTest('NOT RUN: set FAROO_FR_D1_RECORDS and FAROO_B0_INVENTORY_DIR')
        manifest = json.loads((Path(directory) / 'records.json').read_text())
        loaded = {}
        for entry in manifest['records']:
            path, sha = Path(directory) / entry['file'], entry['sha256']
            loader = fm.load_authorization if entry['stage'] == 'S3' else col.load_authorization
            try:
                auth = loader(path, sha)
                state = 'APPROVED'
            except CollectionStop as stop:
                self.assertEqual(stop.code, 'FR_D1_NOT_AUTHORIZED', f'{entry["file"]}: {stop}')
                auth, state = json.loads(path.read_bytes()), 'NOT_APPROVED'
            pinned = approvals.FORK if entry['stage'] == 'S3' else approvals.COLLECTION
            self.assertEqual(state, 'APPROVED' if sha in pinned else 'NOT_APPROVED', entry['file'])
            loaded[(entry['stage'], entry['chain'])] = (auth, sha)
        for chain in h.CHAINS:
            s1, s1sha = loaded[('S1', chain)]
            s2, s2sha = loaded[('S2', chain)]
            s3, _ = loaded[('S3', chain)]
            plan = json.loads((Path(b0) / chain / 'plan.json').read_bytes())
            self.assertEqual(hashlib.sha256((Path(b0) / chain / 'plan.json').read_bytes()).hexdigest(),
                             h.B0_PREFIX[chain]['planSha256'])
            targets = [t['address'] for t in cp.b0_targets(plan)]
            self.assertEqual(s1['scope']['targets']['b0Targets'], targets)
            self.assertEqual(s2['scope']['targets']['b0Targets'], targets)
            self.assertEqual(s2['scope']['anchorPolicy']['s1AuthorizationSha256'], s1sha)
            self.assertEqual(s2['scope']['anchorPolicy']['s1AuthorizationId'], s1['authorizationId'])
            self.assertEqual([s3['scope']['forkAnchor'][k] for k in ('s1AuthorizationSha256', 's2AuthorizationSha256')],
                             [s1sha, s2sha])
            self.assertEqual(len({s1['authorizationId'], s2['authorizationId'], s3['authorizationId']}), 3)


if __name__ == '__main__':
    unittest.main()
