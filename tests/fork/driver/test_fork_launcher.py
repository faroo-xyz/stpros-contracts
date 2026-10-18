"""Tests of the real-state Fork launcher (``fork_launcher``). Test-only; never reaches a public endpoint.

Fork round trips use a loopback upstream anvil and a SIMULATION authorization (loopback upstreams only); they need
anvil inside an approved export, otherwise NOT RUN. The real-record tests need FAROO_LAUNCHER_EVIDENCE: a directory
with the streamlined ``records/``, the archived ``runs/<stage>-<chain>/`` budget ledgers of the S1/S2 collections and
``reconciliation/`` (the two final records and the two candidates). They bind and refuse fork anchors offline; a
public launch is always refused before any proxy or process exists (isolation), so no request can leave the host.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import collector as col  # noqa: E402
import fork_launcher as fl  # noqa: E402
import fork_mode as fm  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402
import test_owner_config as toc  # noqa: E402

NOW = '2026-10-19T00:00:00Z'
SENDER = '0x' + '5e' * 20
ANCHORS = {'testnet': (31_580_881, '0x8727295552e06aa735c8f7de00410a8f9bdac627e333136ef33d58f584b76555'),
           'mainnet': (20_582_384, '0x42090507f18a3b7d8646d39c7a8dbceff2adf7fd158751b1fefc6cfde418aea9')}
FINAL = {'testnet': 'a27ffd60d7105aa6aebfcc0d638da8b3488535b973e75f79245200d15888cf81',
         'mainnet': '158afc230f468be8fb2ad5a68b4eb2036322c1c048fd8ef7ee68a106185bfd61'}


def loopback_ready():
    if shutil.which('anvil') is None:
        return 'NOT RUN: anvil is not available in this environment'
    if not (ROOT / 'EXPORT-MANIFEST.txt').is_file():
        return 'NOT RUN: Foundry binaries run only inside an approved export'
    return None


class Base(unittest.TestCase):
    def stop(self, fn, code):
        with self.assertRaises(CollectionStop) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception


class FakeLedger:
    def __init__(self, directory):
        self.directory = Path(directory)


class StaticTest(Base):
    def test_write_methods_are_refused_before_anything_starts(self):
        auth = {'methods': ['eth_chainId', 'eth_sendRawTransaction'], 'mode': 'SIMULATION', 'chain': 'mainnet'}
        self.stop(lambda: fl.launch(auth, None, ROOT), 'FORK_WRITE_METHOD')
        self.assertFalse(fl.WRITE_METHODS & set(scope.FORK_STATE_METHODS))

    def test_node_requires_a_running_loopback_proxy(self):
        class Fake:
            url, auth = 'http://10.0.0.1:8545/', {}

            class thread:
                @staticmethod
                def is_alive():
                    return True
        self.stop(lambda: fl.ForkNode(Fake(), ROOT), 'FORK_PROXY')

    def test_the_two_pinned_reconciliations_are_exactly_the_accepted_records(self):
        self.assertEqual(approvals.RECONCILIATIONS, frozenset(FINAL.values()))
        self.assertNotIn('tests/fork/driver/fr_d1_approvals.py', scope.SOURCE_BINDING_FILES)


class IsolationTest(Base):
    """A public run is confined to an approved export below the approved parent, ledger in its Fork area."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.parent = Path(self._tmp.name).resolve()
        self.export = self.parent / 'faroo-s3-export'
        self.fork = self.export / 'cache_forge/rehearsal/fork-campaign/fork'
        self.fork.mkdir(parents=True)
        (self.export / 'EXPORT-MANIFEST.txt').write_text('{}')

    def ok(self, cwd, ledger_dir, parent=None):
        return fl.require_isolation(cwd, FakeLedger(ledger_dir), parent or self.parent)

    def test_the_approved_layout_passes(self):
        self.assertEqual(self.ok(self.export, self.fork / 'budget'), self.export)
        self.assertEqual(fl.APPROVED_EXPORT_PARENT, Path('/Volumes/project/tmp'))

    def test_every_escape_is_refused(self):
        self.stop(lambda: fl.require_isolation(self.export, None, self.parent), 'FORK_ISOLATION')
        self.stop(lambda: self.ok(self.export, self.export / 'cache_forge/rehearsal/elsewhere'), 'EXPORT_ROOT')
        self.stop(lambda: self.ok(self.parent, self.fork), 'FORK_ISOLATION')                     # cwd is not the export
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, other)
        self.stop(lambda: self.ok(self.export, self.fork, parent=other), 'FORK_ISOLATION')       # outside the parent
        (self.export / '.git').mkdir()
        self.stop(lambda: self.ok(self.export, self.fork), 'EXPORT_ROOT')                       # a repository checkout
        (self.export / '.git').rmdir()
        link = self.parent / 'link'
        os.symlink(self.export, link)
        self.stop(lambda: self.ok(link, link / 'cache_forge/rehearsal/fork-campaign/fork'), 'FORK_ISOLATION')
        self.stop(lambda: self.ok(self.export, link / 'cache_forge/rehearsal/fork-campaign/fork'), 'EXPORT_ROOT_SYMLINK')
        self.stop(lambda: self.ok(Path('relative'), self.fork), 'FORK_ISOLATION')


class PreflightTest(Base):
    def auth(self, chain, *, reconciliation='r' * 64, block=None, block_hash=None, spec='s' * 64):
        b, bh = ANCHORS[chain]
        return {'chain': chain, 'forkBlock': block or b, 'forkBlockHash': block_hash or bh,
                'anchorBinding': {'reconciliationSha256': reconciliation, 'specSha256': spec},
                'scope': {'forkAnchor': {'s1AuthorizationSha256': '1' * 64, 's2AuthorizationSha256': '2' * 64}}}

    def record(self, chain, **edit):
        b, bh = ANCHORS[chain]
        doc = {'chain': chain, 'chainId': {'testnet': 688689, 'mainnet': 1672}[chain], 'anchorBlock': b, 'anchorHash': bh,
               's1AuthorizationSha256': '1' * 64, 's2AuthorizationSha256': '2' * 64, 'specSha256': 's' * 64,
               'verdict': 'RECONCILED', 'reviewer': 'Agent A', **edit}
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        path = d / 'r.json'
        path.write_bytes(scope.record_bytes(doc))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_an_unpinned_or_foreign_reconciliation_is_never_present(self):
        path, sha = self.record('testnet')
        gate = fl.reconciliation_gate('testnet', self.auth('testnet', reconciliation=sha), (path, sha))
        self.assertEqual(gate['state'], 'BLOCKED')                       # not pinned, although otherwise exact
        with mock.patch.object(approvals, 'RECONCILIATIONS', frozenset({sha})):
            self.assertEqual(fl.reconciliation_gate('testnet', self.auth('testnet', reconciliation=sha), (path, sha))['state'],
                             'PRESENT')
            for name, auth, chain in (('other chain', self.auth('testnet', reconciliation=sha), 'mainnet'),
                                      ('other block', self.auth('testnet', reconciliation=sha, block=1), 'testnet'),
                                      ('other hash', self.auth('testnet', reconciliation=sha, block_hash='0x' + '11' * 32),
                                       'testnet'),
                                      ('other binding', self.auth('testnet', reconciliation='b' * 64), 'testnet'),
                                      ('other spec', self.auth('testnet', reconciliation=sha, spec='t' * 64), 'testnet')):
                with self.subTest(name):
                    self.assertEqual(fl.reconciliation_gate(chain, auth, (path, sha))['state'], 'BLOCKED')
            path.write_bytes(path.read_bytes().replace(b'Agent A', b'Agent X'))
            self.assertEqual(fl.reconciliation_gate('testnet', self.auth('testnet', reconciliation=sha), (path, sha))['state'],
                             'BLOCKED')                             # modified bytes are not the pinned record
        pending, psha = self.record('testnet', verdict='PENDING AGENT A')
        with mock.patch.object(approvals, 'RECONCILIATIONS', frozenset({psha})):
            self.assertEqual(fl.reconciliation_gate('testnet', self.auth('testnet', reconciliation=psha), (pending, psha))['state'],
                             'BLOCKED')

    def test_a_safe_manifest_is_present_only_for_this_safe_anchor_and_official_entries(self):
        import campaign_input as ci
        import history as h
        b, bh = ANCHORS['mainnet']
        doc = {'chainId': 1672, 'safe': h.GOV_SAFE, 'block': b, 'blockHash': bh,
               'officialRegistry': {'safe_l2': {'networkEntry': 'canonical'}, 'multi_send_call_only': {'networkEntry': 'canonical'}}}
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)

        def manifest(**edit):
            path = d / f'm{len(list(d.iterdir()))}.json'
            path.write_text(json.dumps({**doc, **edit}))
            return path, hashlib.sha256(path.read_bytes()).hexdigest()
        good = manifest()
        auth = self.auth('mainnet')
        self.assertEqual(fl.safe_manifest_gate(auth, good)['state'], 'BLOCKED')          # FR-X8 not approved
        self.assertEqual(ci.APPROVED_SAFE_IDENTITY_MANIFESTS, frozenset())
        for name, (path, sha), a in (('approved', good, auth), ('other anchor', good, self.auth('mainnet', block=1)),
                                     ('other chain', good, self.auth('testnet')),
                                     ('other safe', manifest(safe='0x' + '12' * 20), auth),
                                     ('other chain id', manifest(chainId=688689), auth),
                                     ('not official', manifest(officialRegistry={}), auth)):
            with self.subTest(name), mock.patch.object(ci, 'APPROVED_SAFE_IDENTITY_MANIFESTS', frozenset({sha})):
                self.assertEqual(fl.safe_manifest_gate(a, (path, sha))['state'], 'PRESENT' if name == 'approved' else 'BLOCKED')

    def test_testnet_identities_are_checked_at_the_fork_anchor(self):
        auth = self.auth('testnet')
        gates = fl.preflight('testnet', auth=auth, rows=toc.history(), observed=toc.observed())
        self.assertEqual([g['state'] for g in gates[1:]], ['PRESENT', 'PRESENT', 'BLOCKED'])  # reporter, sink, keeper
        drift = fl.preflight('testnet', auth=auth, rows=toc.history(), observed=toc.observed({('oracle', 'slp'): '0x' + '77' * 20}))
        self.assertEqual(drift[1]['state'], 'FAIL')
        moved = fl.preflight('testnet', auth=self.auth('testnet', block=ANCHORS['testnet'][0] + 1), rows=toc.history(),
                             observed=toc.observed())
        self.assertEqual(moved[1]['state'], 'FAIL')                                      # ANCHOR_MISMATCH
        self.assertEqual([g['state'] for g in fl.preflight('testnet')[1:]], ['BLOCKED'] * 3)
        self.assertEqual([g['state'] for g in fl.preflight('mainnet')], ['BLOCKED', 'BLOCKED'])


class LaunchTest(Base):
    @classmethod
    def setUpClass(cls):
        reason = loopback_ready()
        if reason:
            raise unittest.SkipTest(reason)

    def setUp(self):
        import localchain as lc
        (ROOT / 'cache_forge/rehearsal').mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / 'cache_forge/rehearsal')
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.upstream = lc.LocalChain(1672, 1_760_000_000)
        self.addCleanup(self.upstream.close)
        self.upstream.warp(12)
        self.head = self.upstream.block()
        self.started = []
        real = subprocess.Popen

        def record(*args, **kwargs):
            proc = real(*args, **kwargs)
            self.started.append(proc)
            return proc
        patch = mock.patch.object(fl.subprocess, 'Popen', side_effect=record)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        for proc in self.started:
            self.assertIsNotNone(proc.poll(), 'the launcher left an anvil process running')

    def auth(self, *, chain='mainnet', block_hash=None, ceiling=100_000, name='a'):
        return fm.simulation_authorization(self.dir / name, chain, self.upstream.node.url + '/',
                                           int(self.head['number'], 16), block_hash or self.head['hash'], ceiling=ceiling)

    def ledger(self, auth, name='budget'):
        ledger = tp.BudgetLedger(self.dir / name, auth, invocation='launcher-test').open()
        self.addCleanup(ledger.close)
        return ledger

    def test_fork_at_the_bound_anchor_executes_locally_only(self):
        auth = self.auth()
        ledger = self.ledger(auth)
        before = (self.upstream.block()['number'], self.upstream.node.rpc('eth_getTransactionCount', [SENDER, 'latest']))

        def flow(node):
            self.assertEqual(node.chain_id, 1672)
            receipt = node.send_alone(SENDER, '0x' + '7a' * 20, '0x')
            return {'status': receipt['status'], 'localNonce': node.rpc('eth_getTransactionCount', [SENDER, 'latest'])}
        out = fl.run(auth, ledger, ROOT, flow)
        self.assertEqual(out['result'], {'status': '0x1', 'localNonce': '0x1'})
        after = (self.upstream.block()['number'], self.upstream.node.rpc('eth_getTransactionCount', [SENDER, 'latest']))
        self.assertEqual(after, before)                                   # nothing reached the upstream
        self.assertEqual(out['forwardedPosts'], out['ledgerUsed'])
        self.assertTrue(set(out['methods']) <= set(auth['methods']))
        self.assertNotIn('eth_sendRawTransaction', out['methods'])
        self.assertIsNone(out['stopped'])
        self.assertEqual(len(self.started), 1)

    def test_every_startup_failure_stops_the_node_and_the_proxy(self):
        cases = (('hash', dict(block_hash='0x' + '66' * 32), 'FORK_ANCHOR_MISMATCH'),
                 ('chain', dict(chain='testnet'), 'FORK_CHAIN_MISMATCH'))
        for name, kw, code in cases:
            with self.subTest(name):
                auth = self.auth(name=name, **kw)
                proxies = []
                real = fm.ForkBudgetProxy

                def keep(*a, **k):
                    p = real(*a, **k)
                    proxies.append(p)
                    return p
                with mock.patch.object(fl.fm, 'ForkBudgetProxy', side_effect=keep):
                    self.stop(lambda: fl.launch(auth, self.ledger(auth, f'b-{name}'), ROOT), code)
                self.assertFalse(proxies[0].thread.is_alive(), 'the proxy kept serving after a failed launch')
        with mock.patch.object(fl, 'STARTUP_S', 0):
            auth = self.auth(name='timeout')
            self.stop(lambda: fl.launch(auth, self.ledger(auth, 'b-timeout'), ROOT), 'FORK_NODE_TIMEOUT')
        self.assertEqual(len(self.started), 3)

    def test_budget_exhaustion_stops_the_fork_and_leaves_no_process(self):
        auth = self.auth(ceiling=2, name='ceiling')
        ledger = self.ledger(auth, 'b3')
        with self.assertRaises(CollectionStop):
            fl.launch(auth, ledger, ROOT)
        self.assertLessEqual(ledger.used, 2)

    def test_a_proxy_refusal_starts_no_process(self):
        public = {**self.auth(name='public'), 'mode': 'LOCAL_FORK_PUBLIC_READ_ONLY', 'sha256': 'f' * 64}
        with mock.patch.object(fl, 'require_isolation', lambda cwd, ledger: None):
            self.stop(lambda: fl.launch(public, None, ROOT), 'FR_D1_NOT_AUTHORIZED')
        self.assertEqual(self.started, [])

    def test_code_identities_compare_with_supplied_official_hashes(self):
        from tools.lib.keccak import keccak256_bytes
        code = '0x6001600055'
        target = '0x' + '9c' * 20
        self.upstream.node.rpc('anvil_setCode', [target, code])
        self.upstream.warp(12)
        self.head = self.upstream.block()
        auth = self.auth(name='code')
        official = '0x' + keccak256_bytes(bytes.fromhex(code[2:])).hex()
        out = fl.run(auth, self.ledger(auth, 'b4'), ROOT, lambda node: fl.code_identities(
            node, auth['forkBlock'], {target: official, '0x' + '9d' * 20: official}))
        self.assertEqual([(r['address'], r['result']) for r in out['result']],
                         [(target, 'MATCH'), ('0x' + '9d' * 20, 'MISMATCH')])


class RealRecordTest(Base):
    """The approved streamlined S3 records, the archived S1/S2 ledgers and the two pinned reconciliations, offline."""

    def setUp(self):
        base = os.environ.get('FAROO_LAUNCHER_EVIDENCE')
        if not base:
            self.skipTest('NOT RUN: set FAROO_LAUNCHER_EVIDENCE')
        self.base = Path(base)
        self.records = json.loads((self.base / 'records/records.json').read_text())['records']
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def record(self, stage, chain):
        entry = next(e for e in self.records if (e['stage'], e['chain']) == (stage, chain))
        return self.base / 'records' / entry['file'], entry['sha256']

    def refs(self, chain):
        (p1, h1), (p2, h2) = self.record('S1', chain), self.record('S2', chain)
        return (p1, h1, self.base / f'runs/s1-{chain}/budget'), (p2, h2, self.base / f'runs/s2-{chain}/budget')

    def bind(self, chain, reconciliation, accepted=None, refs=None):
        p3, s3 = self.record('S3', chain)
        auth = fm.load_authorization(p3, s3)
        s1, s2 = refs or self.refs(chain)
        return scope.bind_fork_anchor(auth, s1=s1, s2=s2, reconciliation=reconciliation, load=col.load_authorization,
                                      now_utc=NOW, accepted=accepted)

    def final(self, chain):
        path = self.base / f'reconciliation/{chain}.reconciliation.json'
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def edited(self, chain, **edit):
        doc = json.loads(self.final(chain)[0].read_bytes())
        path = self.tmp / f'{chain}-{len(list(self.tmp.iterdir()))}.json'
        path.write_bytes(scope.record_bytes({**doc, **edit}))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_the_final_records_bind_their_own_chain_anchor_and_spec(self):
        for chain in ('testnet', 'mainnet'):
            with self.subTest(chain):
                path, sha = self.final(chain)
                self.assertEqual(sha, FINAL[chain])
                doc = json.loads(path.read_bytes())
                candidate = json.loads((self.base / f'reconciliation/{chain}.candidate.json').read_bytes())
                self.assertEqual({k for k in doc if doc[k] != candidate[k]}, {'verdict', 'reviewer'})
                bound = self.bind(chain, (path, sha))
                self.assertEqual((bound['forkBlock'], bound['forkBlockHash']), ANCHORS[chain])
                self.assertEqual(bound['anchorBinding']['reconciliationSha256'], sha)
                gate = fl.reconciliation_gate(chain, bound, (path, sha))
                self.assertEqual(gate['state'], 'PRESENT', gate['problems'])
                # a public launch from outside an approved export is refused before any proxy or process exists
                self.stop(lambda: fl.launch(bound, None, ROOT), 'FORK_ISOLATION')

    def test_wrong_chain_hash_spec_unapproved_or_modified_records_are_refused(self):
        tpath, tsha = self.final('testnet')
        mpath, msha = self.final('mainnet')
        self.stop(lambda: self.bind('testnet', (mpath, msha)), 'FORK_ANCHOR_MISMATCH')            # the other chain's record
        hp, hs = self.edited('testnet', anchorHash='0x' + '11' * 32)
        self.stop(lambda: self.bind('testnet', (hp, hs)), 'RECONCILIATION_NOT_ACCEPTED')          # not pinned
        self.stop(lambda: self.bind('testnet', (hp, hs), accepted=frozenset({hs})), 'FORK_ANCHOR_MISMATCH')
        sp, ss = self.edited('testnet', specSha256='0' * 64)
        self.stop(lambda: self.bind('testnet', (sp, ss), accepted=frozenset({ss})), 'FORK_ANCHOR_MISMATCH')
        candidate = self.base / 'reconciliation/testnet.candidate.json'
        csha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        self.stop(lambda: self.bind('testnet', (candidate, csha)), 'RECONCILIATION_NOT_ACCEPTED')
        copy_path = self.tmp / 'modified.json'
        copy_path.write_bytes(tpath.read_bytes().replace(b'"verdict":', b'"verdict" :'))
        self.stop(lambda: self.bind('testnet', (copy_path, tsha)), 'RECONCILIATION_HASH')         # bytes differ from the pin
        # S1/S2 evidence that does not reproduce the final spec: an S2 ledger of the other chain
        s1, _ = self.refs('testnet')
        _, s2_main = self.refs('mainnet')
        with self.assertRaises(CollectionStop):
            self.bind('testnet', (tpath, tsha), refs=(s1, s2_main))
        self.assertEqual(copy.deepcopy(approvals.RECONCILIATIONS), frozenset(FINAL.values()))


if __name__ == '__main__':
    unittest.main()
