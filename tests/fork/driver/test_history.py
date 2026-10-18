"""Offline tests of history content verification, inventory derivation and the observation check.

Run inside an approved export:
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_*.py' -v

Synthetic archives below use registry addresses with fabricated logs, hashes and blocks; they are
SIMULATION ONLY. The optional real-archive test reads the immutable B0 archive only when
FAROO_B0_ARCHIVE (the tarball) and FAROO_B0_INVENTORY_DIR are supplied; otherwise it is NOT RUN.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import campaign_input as ci  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import inventory as inv  # noqa: E402
import scenarios  # noqa: E402
from history import HistoryRefused  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.lib.rpc import PUBLIC_RPC  # noqa: E402
from tools.release.native.history import authenticate_history, history_targets  # noqa: E402

T = h.logscan.T
ZERO32 = '0x' + '00' * 32
ADMIN = hi.DEFAULT_ADMIN_ROLE
INV_SHA = '0x' + '5a' * 32
REG = {c: h.registry(c) for c in h.CHAINS}
E2 = '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1'
ALICE, BOB = '0x' + 'a1' * 20, '0x' + 'b2' * 20


def word(address):
    return '0x' + address[2:].rjust(64, '0')


def amount(value):
    return '0x' + format(value, '064x')


def block_hash(block):
    return '0x' + hashlib.sha256(f'block {block}'.encode()).hexdigest()


def log(address, topics, block, index, data='0x', tx=None):
    return {'address': address, 'topics': topics, 'data': data, 'blockNumber': hex(block), 'blockHash': block_hash(block),
            'transactionHash': '0x' + hashlib.sha256((tx or f'tx {block} {index}').encode()).hexdigest(),
            'transactionIndex': '0x0', 'logIndex': hex(index), 'removed': False}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, doc):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc))
    return sha(path)


class Archive:
    """Writes B0-schema plans/chunks and a campaign manifest below `root` (SIMULATION ONLY)."""

    def __init__(self, root, chain):
        self.root, self.chain, self.segments = Path(root), chain, []

    def segment(self, name, addresses, start, end, logs, code_before=(), chunk_windows=2, end_hash=None):
        addresses = sorted(addresses)
        fid = j.historical_filter_id(addresses)
        windows = [(s, min(s + 999, end)) for s in range(start, end + 1, 1000)]
        refs = []
        for i in range(0, len(windows), chunk_windows):
            group = windows[i:i + chunk_windows]
            a, b = group[0][0], group[-1][1]
            rows = [r for r in logs if a <= int(r['blockNumber'], 16) <= b]
            chunk = {'chain': self.chain, 'fetched_utc': '2026-10-13T00:00:00Z', 'filter_id': fid, 'from': a, 'to': b,
                     'logs': rows, 'windows': [[x, y, sum(x <= int(r['blockNumber'], 16) <= y for r in rows)]
                                               for x, y in group]}
            rel = f'{name}/chunks/{a:010d}-{b:010d}.json'
            refs.append({'path': rel, 'sha256': write(self.root / rel, chunk)})
        plan = {'addresses': [{'address': a, 'labels': ['synthetic']} for a in addresses],
                'addresses_with_code_before_start': list(code_before), 'chain': self.chain,
                'chain_id': h.CHAINS[self.chain], 'chunk_windows': chunk_windows, 'created_utc': '2026-10-13T00:00:00Z',
                'end': end, 'end_block_hash': end_hash or block_hash(end), 'end_timestamp': 1791527275 + end,
                'filter_id': fid, 'head_at_plan': end + 120, 'rpc': PUBLIC_RPC[self.chain], 'start': start,
                'start_basis': 'synthetic', 'tool_version': '1', 'vaults': [], 'window': 1000}
        segment = {'planFile': f'{name}/plan.json', 'planSha256': write(self.root / f'{name}/plan.json', plan),
                   'chunks': refs}
        self.segments.append(segment)
        return segment

    def manifest(self, from_block, anchor, targets, anchor_hash=None, inventory_sha=INV_SHA, name='manifest.json'):
        doc = {'schema': h.SCHEMA, 'chain': self.chain, 'chainId': h.CHAINS[self.chain], 'fromBlock': from_block,
               'blockNumber': anchor, 'blockHash': anchor_hash or block_hash(anchor), 'inventorySha256': inventory_sha,
               'targets': sorted(targets), 'segments': self.segments}
        return name, write(self.root / name, doc), doc


def prefix_of(segment, root, start, end):
    plan = json.loads((Path(root) / segment['planFile']).read_text())
    return {'start': start, 'end': end, 'filterId': plan['filter_id'], 'endBlockHash': plan['end_block_hash'],
            'planSha256': segment['planSha256'], 'chunkCount': len(segment['chunks']),
            'chunkListSha256': h.chunk_list_sha256(c['sha256'] for c in segment['chunks'])}


# Synthetic campaign layout: "B0" prefix [100, 2999], pin 2999, incremental [3000, 4500].
FROM, PIN, ANCHOR = 100, 2999, 4500
ZERO20 = '0x' + '00' * 20
BEACON_T, VAULT_IMPL, STPROS_IMPL = '0x' + 'be' * 20, '0x' + '1f' * 20, '0x' + '5e' * 20


def vault_creation(factory, vault, beacon, admin, block, tx_tag):
    """YieldVaultCreated, the BeaconProxy constructor's BeaconUpgraded and the ADMIN grant, one transaction."""
    tx = f'create {vault} {tx_tag}'
    return [log(factory, [T['YieldVaultCreated'], word(vault), word(admin)], block, 0, tx=tx),
            log(vault, [T['RoleGranted'], ADMIN, word(admin), word(factory)], block, 1, tx=tx),
            log(vault, [T['ProxyBeaconUpgraded'], word(beacon)], block, 2, tx=tx)]


def runtime_hash(vault):
    return '0x' + hashlib.sha256(f'synthetic runtime {vault}'.encode()).hexdigest()


def reviewed_for(derived):
    """Synthetic reviewed runtime identities for every discovered vault (SIMULATION ONLY)."""
    return {c['vault']: {'runtimeKeccak': runtime_hash(c['vault']), 'factory': c['factory'],
                         'beacon': derived['factoryBeacons'][c['factory']][0]} for c in derived['vaultCreation']}


def testnet_case(root, *, new_vault=True, drop_new_vault=False, extra_rows=(), new_prefix_logs=(),
                 extra_prefix_rows=(), v2_beacon=BEACON_T):
    """Testnet: factory, its beacon, StPROS and one vault in the prefix; a second vault created after
    the pin."""
    reg = REG['testnet']
    factory, stpros = reg['factories'][0], reg['stpros']
    v1, v2 = '0x' + 'c1' * 20, '0x' + 'c2' * 20
    base = [factory, stpros, v1, BEACON_T]
    prefix_logs = [
        log(stpros, [T['Upgraded'], word(STPROS_IMPL)], 105, 0),
        log(stpros, [T['AdminChanged']], 105, 1, '0x' + '00' * 32 + word(reg['proxy_admin'])[2:]),
        log(factory, [T['OwnershipTransferred'], word(ZERO20), word(E2)], 110, 0),
        log(factory, [T['BeaconCreated'], word(BEACON_T), word(VAULT_IMPL)], 120, 0),
        log(BEACON_T, [T['Upgraded'], word(VAULT_IMPL)], 120, 1),
        log(BEACON_T, [T['OwnershipTransferred'], word(ZERO20), word(factory)], 120, 2),
        *vault_creation(factory, v1, BEACON_T, E2, 150, 'v1'),
        *extra_prefix_rows,
        log(stpros, [T['Transfer'], word('0x' + '00' * 20), word(ALICE)], 200, 0, amount(30)),
        log(stpros, [T['Withdraw'], word(ALICE), word(ALICE), word(ALICE)], 1500, 0, amount(5) + '0' * 64),
        log(stpros, [T['Transfer'], word(ALICE), word('0x' + '00' * 20)], 1500, 1, amount(5)),
        log(v1, [T['Transfer'], word('0x' + '00' * 20), word(BOB)], 2500, 0, amount(7)),
    ]
    later = [log(stpros, [T['Transfer'], word(ALICE), word(BOB)], 3100, 0, amount(10)), *extra_rows]
    if new_vault:
        created = vault_creation(factory, v2, v2_beacon, E2, 3200, 'v2')
        # A collector filtering by the target set never fetches v2's own logs when v2 is omitted.
        later += created[:1] if drop_new_vault else created
    a = Archive(root, 'testnet')
    b0 = a.segment('b0', base, FROM, PIN, prefix_logs)
    targets = base + ([v2] if new_vault and not drop_new_vault else [])
    if new_vault and not drop_new_vault:
        a.segment('new-targets-prefix', [v2], FROM, PIN, list(new_prefix_logs))
    a.segment('incremental', targets, PIN + 1, ANCHOR, later)
    return a, prefix_of(b0, root, FROM, PIN), targets, {'v1': v1, 'v2': v2, 'factory': factory, 'stpros': stpros}


def mainnet_case(root, *, safe_segment=True, safe_target=True, safe_from=FROM, safe_code_before=()):
    reg = REG['mainnet']
    stpros, safe = reg['stpros'], h.GOV_SAFE
    v1 = '0x' + 'd1' * 20
    base = [*reg['factories'], stpros, v1]
    a = Archive(root, 'mainnet')
    beacons = ['0x' + 'b0' * 20, '0x' + 'b1' * 20]
    b0 = a.segment('b0', base, FROM, PIN, [
        *[log(f, [T['BeaconCreated'], word(b), word(VAULT_IMPL)], 115, i) for i, (f, b) in enumerate(zip(reg['factories'], beacons))],
        *vault_creation(reg['factories'][0], v1, beacons[0], safe, 120, 'v1')])
    safe_setup = '0x' + hashlib.sha256(b'synthetic SafeSetup').hexdigest()
    if safe_segment and safe_target:
        a.segment('safe', [safe], safe_from, PIN, [log(safe, [safe_setup], 110, 0)], code_before=safe_code_before)
    targets = [*base, safe] if safe_target else base
    a.segment('incremental', targets, PIN + 1, ANCHOR, [])
    return a, prefix_of(b0, root, FROM, PIN), sorted(targets)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()   # native reader refuses symlinked components

    def tearDown(self):
        self._tmp.cleanup()

    def refused(self, fn, code, status=None):
        with self.assertRaises(HistoryRefused) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        if status:
            self.assertEqual(ctx.exception.status, status, str(ctx.exception))
        return ctx.exception

    def auth(self, archive, prefix, name, digest, **kw):
        args = dict(chain=archive.chain, anchor_block=ANCHOR, anchor_hash=block_hash(ANCHOR), inventory_sha256=INV_SHA,
                    prefix=prefix)
        args.update(kw)
        return h.authenticate(name, digest, archive.root, **args)


class ContentVerificationTest(Tmp):
    def test_valid_archive_is_content_verified_never_authenticated(self):
        a, prefix, targets, ids = testnet_case(self.root)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        result = self.auth(a, prefix, name, digest)
        self.assertEqual(result['provenance']['authenticated'], False)
        self.assertEqual(result['provenance']['canonicalAnchorRecheck'], 'NOT RUN')
        self.assertEqual(result['provenance']['independentApproval'], 'ABSENT')
        self.assertEqual(len(result['rows']), 17)
        self.assertEqual(result['b0Prefix']['planSha256'], prefix['planSha256'])
        self.assertEqual(h.provenance()['authenticated'], False)
        # The final FS gate is unaffected by content verification.
        self.assertFalse(scenarios.FINAL_GATE_PROVENANCE_IMPLEMENTED)

    def test_missing_or_modified_chunks_refused(self):
        a, prefix, targets, _ = testnet_case(self.root)
        a.segments[0]['chunks'].pop()
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_CHUNK_GAP')
        a, prefix, targets, _ = testnet_case(self.root / 'm')
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        chunk = a.root / a.segments[2]['chunks'][0]['path']
        doc = json.loads(chunk.read_text()); doc['logs'][0]['data'] = amount(11)
        chunk.write_text(json.dumps(doc))
        error = self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_CHUNK_HASH_REQUIRED')
        self.assertIn('REVIEWED_FILE_HASH', str(error))

    def test_modified_b0_prefix_refused_even_when_rehashed(self):
        a, prefix, targets, _ = testnet_case(self.root)
        ref = a.segments[0]['chunks'][1]
        chunk = a.root / ref['path']
        doc = json.loads(chunk.read_text()); doc['logs'] = doc['logs'][:-1]; doc['windows'][-1][2] -= 1
        ref['sha256'] = write(chunk, doc)      # self-consistent rehash of a B0 chunk
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'B0_PREFIX_MODIFIED')

    def test_raw_sha256_bindings(self):
        a, prefix, targets, _ = testnet_case(self.root)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, 'ab' * 32), 'HISTORY_PROOF_REQUIRED')
        self.refused(lambda: self.auth(a, prefix, name, '0x' + digest), 'HISTORY_PROOF_REQUIRED')
        self.refused(lambda: self.auth(a, prefix, name, digest.upper()), 'HISTORY_PROOF_REQUIRED')
        for mutate, code in ((lambda s: s.update(planSha256='0' * 64), 'HISTORY_PLAN_HASH_REQUIRED'),
                             (lambda s: s['chunks'][0].update(sha256='0x' + s['chunks'][0]['sha256']),
                              'HISTORY_CHUNK_HASH_REQUIRED')):
            b, prefix, targets, _ = testnet_case(self.root / code)
            mutate(b.segments[2])
            name, digest, _ = b.manifest(FROM, ANCHOR, targets)
            self.refused(lambda: self.auth(b, prefix, name, digest), code)

    def test_target_coverage_gaps_refused(self):
        a, prefix, targets, ids = testnet_case(self.root, drop_new_vault=True)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        # Vault created after the pin but absent from the target set: discovery is not closed.
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_MISSING', 'INCOMPLETE')
        a, prefix, targets, ids = testnet_case(self.root / 'gap')
        a.segments.pop(1)                       # new vault covered only after the pin
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_GAP_OR_OVERLAP', 'INCOMPLETE')
        a, prefix, targets, ids = testnet_case(self.root / 'extra')
        a.segment('stray', ['0x' + 'ee' * 20], FROM, ANCHOR, [])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets + ['0x' + 'ee' * 20])
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_UNEXPECTED')
        a, prefix, targets, ids = testnet_case(self.root / 'short')
        name, digest, _ = a.manifest(FROM, ANCHOR + 10, targets, anchor_hash=block_hash(ANCHOR + 10))
        self.refused(lambda: self.auth(a, prefix, name, digest, anchor_block=ANCHOR + 10,
                                       anchor_hash=block_hash(ANCHOR + 10)), 'HISTORY_TARGET_GAP_OR_OVERLAP')

    def test_duplicate_and_overlapping_windows_refused(self):
        a, prefix, targets, ids = testnet_case(self.root)
        a.segment('overlap', [ids['stpros']], 4000, ANCHOR, [])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_GAP_OR_OVERLAP', 'REFUSED')
        stpros = REG['testnet']['stpros']
        dup = log(stpros, [T['Transfer'], word(BOB), word(ALICE)], 3100, 0, amount(1))
        a, prefix, targets, ids = testnet_case(self.root / 'dup', extra_rows=[dup])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_DUPLICATE_LOG')
        # Window count and window tiling inside a chunk.
        for mutate, code in ((lambda d: d['windows'][0].__setitem__(2, d['windows'][0][2] + 1), 'HISTORY_WINDOW_COUNT'),
                             (lambda d: d['windows'][1].__setitem__(0, d['windows'][1][0] - 1), 'HISTORY_WINDOW_GAP')):
            a, prefix, targets, ids = testnet_case(self.root / code)
            ref = a.segments[2]['chunks'][0]
            doc = json.loads((a.root / ref['path']).read_text()); mutate(doc)
            ref['sha256'] = write(a.root / ref['path'], doc)
            name, digest, _ = a.manifest(FROM, ANCHOR, targets)
            self.refused(lambda: self.auth(a, prefix, name, digest), code)

    def test_duplicate_log_across_segments_refused(self):
        # Same (block, logIndex) as a prefix row, attributed to the new vault's prefix segment.
        v2 = '0x' + 'c2' * 20
        dup = log(v2, [T['Transfer'], word(ALICE), word(BOB)], 150, 0, amount(1))
        a, prefix, targets, ids = testnet_case(self.root, new_prefix_logs=[dup])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_DUPLICATE_LOG')

    def test_block_and_chain_identity(self):
        a, prefix, targets, _ = testnet_case(self.root)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest, anchor_hash='0x' + '77' * 32), 'HISTORY_BINDING')
        self.refused(lambda: self.auth(a, prefix, name, digest, anchor_block=ANCHOR - 1), 'HISTORY_BINDING')
        self.refused(lambda: self.auth(a, prefix, name, digest, inventory_sha256='0x' + '66' * 32), 'HISTORY_BINDING')
        self.refused(lambda: self.auth(a, prefix, name, digest, chain='mainnet'), 'HISTORY_CHAIN')
        # Manifest anchor hash that the anchor plan does not carry.
        name2, digest2, _ = a.manifest(FROM, ANCHOR, targets, anchor_hash='0x' + '77' * 32, name='m2.json')
        self.refused(lambda: self.auth(a, prefix, name2, digest2, anchor_hash='0x' + '77' * 32), 'HISTORY_ANCHOR_MISMATCH')
        for field, value, code in (('chain_id', 1672, 'HISTORY_PLAN_RANGE'), ('rpc', 'http://127.0.0.1:8545', 'HISTORY_PLAN_RANGE'),
                                   ('chain', 'mainnet', 'HISTORY_PLAN_RANGE'), ('window', 2000, 'HISTORY_PLAN_RANGE'),
                                   ('filter_id', '0' * 16, 'HISTORY_FILTER'), ('extra', 1, 'HISTORY_PLAN_KEYS')):
            b, prefix, targets, _ = testnet_case(self.root / field)
            seg = b.segments[2]
            plan = json.loads((b.root / seg['planFile']).read_text()); plan[field] = value
            seg['planSha256'] = write(b.root / seg['planFile'], plan)
            name, digest, _ = b.manifest(FROM, ANCHOR, targets)
            self.refused(lambda: self.auth(b, prefix, name, digest), code)
        # A row whose block hash disagrees with another row of the same block (reorg mix).
        stpros = REG['testnet']['stpros']
        row = log(stpros, [T['Transfer'], word(BOB), word(ALICE)], 3100, 5, amount(1))
        row['blockHash'] = '0x' + '99' * 32
        c, prefix, targets, _ = testnet_case(self.root / 'reorg', extra_rows=[row])
        name, digest, _ = c.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(c, prefix, name, digest), 'HISTORY_BLOCK_HASH_CONFLICT')
        removed = log(stpros, [T['Transfer'], word(BOB), word(ALICE)], 3101, 0, amount(1)); removed['removed'] = True
        d, prefix, targets, _ = testnet_case(self.root / 'removed', extra_rows=[removed])
        name, digest, _ = d.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(d, prefix, name, digest), 'HISTORY_LOG_IDENTITY')

    def test_paths_must_stay_below_the_history_root(self):
        a, prefix, targets, _ = testnet_case(self.root / 'inner')
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        for bad in ('../inner/manifest.json', str(a.root / name), 'sub\\manifest.json'):
            self.refused(lambda: h.authenticate(bad, digest, a.root, chain='testnet', anchor_block=ANCHOR,
                                                anchor_hash=block_hash(ANCHOR), inventory_sha256=INV_SHA, prefix=prefix),
                         'HISTORY_PROOF_REQUIRED')
        os.symlink(a.root / name, a.root / 'link.json')
        self.refused(lambda: self.auth(a, prefix, 'link.json', digest), 'HISTORY_PROOF_REQUIRED')

    def test_self_consistent_unpinned_archive_refused(self):
        # Fully self-consistent synthetic history: the pinned B0 archive is absent, so it is refused.
        a, prefix, targets, _ = testnet_case(self.root)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, None, name, digest), 'B0_PREFIX_REQUIRED', 'INCOMPLETE')
        # A segment shaped like B0 (same range and filter) but with other bytes.
        shaped = dict(prefix, planSha256='1' * 64)
        self.refused(lambda: self.auth(a, shaped, name, digest), 'B0_PREFIX_MODIFIED')


def with_safe_target():
    """The retained generic extra-target mechanism, exercised with the GOV Safe as an explicit extra target. The
    streamlined scope declares none (``StreamlinedSafeScopeTest``)."""
    return mock.patch.dict(h.EXTRA_TARGETS, {'mainnet': (h.GOV_SAFE,)})


class StreamlinedSafeScopeTest(Tmp):
    def test_no_default_extra_target_and_a_safe_target_is_unexpected(self):
        self.assertEqual(h.EXTRA_TARGETS, {'mainnet': (), 'testnet': ()})
        a, prefix, targets = mainnet_case(self.root, safe_target=False)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        derived = hi.derive(self.auth(a, prefix, name, digest), 'mainnet')
        self.assertEqual(derived['safe'], {})
        a, prefix, targets = mainnet_case(self.root / 'with-safe')
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_UNEXPECTED')


class SafeHistoryTest(Tmp):
    def setUp(self):
        super().setUp()
        patch = with_safe_target()
        patch.start()
        self.addCleanup(patch.stop)

    def test_safe_complete_history_required(self):
        a, prefix, targets = mainnet_case(self.root)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        result = self.auth(a, prefix, name, digest)
        derived = hi.derive(result, 'mainnet')
        safe = h.GOV_SAFE
        self.assertTrue(derived['safe'][safe]['replay'].startswith('BLOCKED'))
        self.assertEqual(derived['safe'][safe]['firstLogBlock'], 110)

    def test_missing_safe_history_refused(self):
        a, prefix, targets = mainnet_case(self.root, safe_segment=False)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_GAP_OR_OVERLAP', 'INCOMPLETE')
        a, prefix, targets = mainnet_case(self.root / 'no-target', safe_target=False)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_TARGET_MISSING', 'INCOMPLETE')

    def test_safe_history_starting_after_creation_refused(self):
        safe = h.GOV_SAFE
        a, prefix, targets = mainnet_case(self.root, safe_code_before=[safe])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(a, prefix, name, digest), 'HISTORY_START_HAS_CODE', 'INCOMPLETE')
        b, prefix, targets = mainnet_case(self.root / 'late', safe_from=FROM + 1000)
        name, digest, _ = b.manifest(FROM, ANCHOR, targets)
        self.refused(lambda: self.auth(b, prefix, name, digest), 'HISTORY_TARGET_GAP_OR_OVERLAP', 'INCOMPLETE')

    def test_safe_target_is_the_reviewed_gov_identity(self):
        safe = h.GOV_SAFE
        self.assertEqual(safe, ci.REAL_IDENTITIES['mainnet']['GOV'])
        self.assertIn(safe, REG['mainnet']['accounts'])
        self.assertEqual(h.EXTRA_TARGETS['testnet'], ())


def observation_for(derived, chain, overrides=None):
    """A consistent synthetic observation of the derived enumeration (SIMULATION ONLY)."""
    o = {'schema': hi.OBSERVATION_SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'block': derived['anchorBlock'],
         'blockHash': derived['anchorBlockHash'],
         'tokens': {t: {'totalSupply': x['replaySupply'], 'balanceOf': dict(x['balances'])} for t, x in derived['holders'].items()},
         'roles': {}, 'owners': dict(derived['owners']), 'implementations': dict(derived['implementations']),
         'admins': dict(derived['admins']), 'factories': copy.deepcopy(derived['factories']),
         'withdrawals': {'stpros': derived['stpros'], 'queued': '15', 'completed': '10', 'reserve': '5',
                         'owners': {a: {'head': 0, 'tail': 1, 'records': [{'queued': '0', 'pending': '5', 'createdAt': '1',
                                                                              'unbondingPeriod': '0'}]}
                                    for a in derived['withdrawOwners']}},
         'vaultRuntimeKeccak': {v: runtime_hash(v) for vs in derived['factories'].values() for v in vs},
         'pendingOwners': dict(derived['pendingOwners'])}
    for vault in hi.role_targets(derived):
        holders = derived['roles'].get(vault, {'holders': {}})['holders']
        for role, account in hi.role_probes(derived, chain, vault):
            o['roles'].setdefault(vault, {}).setdefault(role, {})[account] = account in holders.get(role, [])
    return o


class InventoryDerivationTest(Tmp):
    def derived(self, **kw):
        a, prefix, targets, ids = testnet_case(self.root / f'case{len(list(self.root.iterdir()))}', **kw)
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        return hi.derive(self.auth(a, prefix, name, digest), 'testnet'), ids

    def check(self, d, o, reviewed=None):
        return hi.check_observation(d, o, 'testnet', reviewed_for(d) if reviewed is None else reviewed)

    def test_enumeration_and_observation_feed_the_existing_normalizer(self):
        d, ids = self.derived()
        self.assertEqual(d['targetClosure'], 'COMPLETE')
        self.assertEqual(d['holders'][ids['stpros']]['balances'], {ALICE: '15', BOB: '10'})
        self.assertEqual(d['factories'][ids['factory']], [ids['v1'], ids['v2']])
        self.assertEqual(d['withdrawOwners'], [ALICE])
        self.assertEqual([e['event'] for e in d['authorityEventsAfterPin']],
                         ['YieldVaultCreated', 'RoleGranted', 'ProxyBeaconUpgraded'])
        # Post-pin authority events are drift until reviewed: never an unqualified PASS.
        docs, mism = self.check(d, observation_for(d, 'testnet'))
        self.assertFalse(any(mism.values()))
        self.assertEqual(docs['summary.json']['checks']['post_pin_authority_review']['status'], 'INCOMPLETE')
        self.assertEqual(docs['summary.json']['authorityEventsAfterPin'],
                         {'count': 3, 'disposition': 'REVIEW REQUIRED (FR-X6); not approved'})
        self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
        with self.assertRaisesRegex(inv.InventoryRefused, 'summary checks not all PASS'):
            inv.normalize(docs, 'testnet', ids['stpros'])
        # Without post-pin authority events the same pipeline normalizes.
        d, ids = self.derived(new_vault=False)
        self.assertEqual(d['authorityEventsAfterPin'], [])
        docs, mism = self.check(d, observation_for(d, 'testnet'))
        self.assertFalse(any(mism.values()))
        self.assertEqual({k: c['status'] for k, c in docs['summary.json']['checks'].items()},
                         dict.fromkeys(('vault_discovery', 'role_inventory', 'ownership_histories', 'upgrade_histories',
                                        'share_holders', 'withdrawal_records', 'beacon_identity',
                                        'pending_owner_evidence', 'post_pin_authority_review',
                                        'post_pin_configuration_review', 'post_pin_unclassified_review',
                                        'unclassified_history_review'), 'PASS'))
        self.assertEqual(docs['summary.json']['overall'], 'PASS')
        self.assertFalse(docs['summary.json']['provenance']['authenticated'])
        normalized = inv.normalize(docs, 'testnet', ids['stpros'])
        self.assertEqual(normalized['anchorBlock'], ANCHOR)
        self.assertEqual(normalized['stpros']['holders'], [ALICE, BOB])
        self.assertEqual(normalized['stpros']['recordOwners'], [ALICE])

    def test_incomplete_enumeration_is_incomplete(self):
        d, ids = self.derived()
        cases = (
            (lambda o: o['tokens'][ids['stpros']]['balanceOf'].pop(BOB), 'HOLDER_ENUMERATION_INCOMPLETE'),
            (lambda o: o['tokens'].pop(ids['v2']), 'HOLDER_ENUMERATION_INCOMPLETE'),
            (lambda o: o['withdrawals']['owners'].pop(ALICE), 'WITHDRAWAL_ENUMERATION_INCOMPLETE'),
            (lambda o: o['roles'][ids['v2']][ADMIN].pop(E2), 'ROLE_ENUMERATION_INCOMPLETE'),
            (lambda o: o['roles'].pop(ids['v1']), 'ROLE_ENUMERATION_INCOMPLETE'),
            (lambda o: o['factories'].pop(ids['factory']), 'VAULT_ENUMERATION_INCOMPLETE'),
        )
        for mutate, code in cases:
            o = observation_for(d, 'testnet'); mutate(o)
            self.refused(lambda: self.check(d, o), code, 'INCOMPLETE')
        o = observation_for(d, 'testnet'); o['tokens'][ids['stpros']]['balanceOf']['0x' + 'ef' * 20] = '0'
        self.refused(lambda: self.check(d, o), 'HOLDER_ENUMERATION_INCOMPLETE', 'REFUSED')
        o = observation_for(d, 'testnet'); o['withdrawals']['owners'][BOB] = {'head': 0, 'tail': 0, 'records': []}
        self.refused(lambda: self.check(d, o), 'WITHDRAWAL_ENUMERATION_INCOMPLETE', 'REFUSED')

    def test_disagreeing_values_fail_and_are_refused_by_the_normalizer(self):
        d, ids = self.derived()
        for mutate, check in (
                (lambda o: o['tokens'][ids['stpros']]['balanceOf'].__setitem__(BOB, '9'), 'share_holders'),
                (lambda o: o['tokens'][ids['stpros']].__setitem__('totalSupply', '26'), 'share_holders'),
                (lambda o: o['roles'][ids['v1']][ADMIN].__setitem__(REG['testnet']['proxy_admin'], True), 'role_inventory'),
                (lambda o: o['withdrawals'].__setitem__('completed', '11'), 'withdrawal_records'),
                (lambda o: o['factories'][ids['factory']].reverse(), 'vault_discovery')):
            o = observation_for(d, 'testnet'); mutate(o)
            docs, _ = self.check(d, o)
            self.assertEqual(docs['summary.json']['checks'][check]['status'], 'FAIL')
            self.assertEqual(docs['summary.json']['overall'], 'FAIL')
            with self.assertRaises(inv.InventoryRefused):
                inv.normalize(docs, 'testnet', ids['stpros'])

    def test_stale_or_foreign_observation_refused(self):
        d, ids = self.derived()
        for field, value, code in (('block', ANCHOR - 1, 'OBSERVATION_ANCHOR'), ('blockHash', ZERO32, 'OBSERVATION_ANCHOR'),
                                   ('chainId', 1672, 'OBSERVATION_CHAIN')):
            o = observation_for(d, 'testnet'); o[field] = value
            self.refused(lambda: self.check(d, o), code)

    def test_unclosed_history_never_normalizes(self):
        a, prefix, targets, ids = testnet_case(self.root)
        name, digest, manifest = a.manifest(FROM, ANCHOR, targets)
        d = hi.derive(h.verify_archive(manifest, a.root, 'testnet'), 'testnet')
        self.assertEqual(d['targetClosure'], 'NOT VERIFIED')
        docs, mism = self.check(d, observation_for(d, 'testnet'))
        self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
        with self.assertRaisesRegex(inv.InventoryRefused, 'INCOMPLETE: log coverage'):
            inv.normalize(docs, 'testnet', ids['stpros'])

    def test_missing_mint_history_is_incomplete(self):
        stpros = REG['testnet']['stpros']
        burn = log(stpros, [T['Transfer'], word(BOB), word('0x' + '00' * 20)], 3150, 0, amount(99))
        a, prefix, targets, _ = testnet_case(self.root, extra_rows=[burn])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        result = self.auth(a, prefix, name, digest)
        self.refused(lambda: hi.derive(result, 'testnet'), 'NEGATIVE_REPLAY_BALANCE', 'INCOMPLETE')


class ObservationReconciliationTest(Tmp):
    """OBS-01 (owner/implementation/admin universes), OBS-02 (role pairs) and OBS-03 (beacon identity)."""
    derived = InventoryDerivationTest.derived
    check = InventoryDerivationTest.check

    def fails(self, d, o, check, reviewed=None):
        docs, mism = self.check(d, o, reviewed)
        self.assertEqual(docs['summary.json']['checks'][check]['status'], 'FAIL')
        self.assertEqual(docs['summary.json']['overall'], 'FAIL')
        with self.assertRaises(inv.InventoryRefused):
            inv.normalize(docs, 'testnet', REG['testnet']['stpros'])
        return docs, mism

    def test_obs01_owner_implementation_admin_universes_are_exact(self):
        d, ids = self.derived(new_vault=False)
        admin = REG['testnet']['proxy_admin']
        self.assertEqual(d['owners'], {ids['factory']: E2, BEACON_T: ids['factory']})
        self.assertEqual(d['implementations'], {ids['stpros']: STPROS_IMPL, BEACON_T: VAULT_IMPL})
        self.assertEqual(d['admins'], {ids['stpros']: admin})   # a beacon has no admin, a vault proxy neither
        stranger = '0x' + 'ef' * 20
        for name, mutate in (('owners', lambda o: o['owners'].__setitem__(stranger, E2)),
                             ('owners', lambda o: o['owners'].__setitem__(ids['v1'], E2)),
                             ('implementations', lambda o: o['implementations'].__setitem__(ids['v1'], VAULT_IMPL)),
                             ('admins', lambda o: o['admins'].__setitem__(BEACON_T, admin))):
            o = observation_for(d, 'testnet'); mutate(o)
            error = self.refused(lambda: self.check(d, o), 'OWNER_OR_IMPLEMENTATION_ENUMERATION_INCOMPLETE', 'REFUSED')
            self.assertIn(f'{name}: not derived from history', str(error))
        for name, key in (('owners', BEACON_T), ('implementations', ids['stpros']), ('admins', ids['stpros'])):
            o = observation_for(d, 'testnet'); o[name].pop(key)
            self.refused(lambda: self.check(d, o), 'OWNER_OR_IMPLEMENTATION_ENUMERATION_INCOMPLETE', 'INCOMPLETE')
        for name, key, value, check in (('owners', ids['factory'], BOB, 'ownership_histories'),
                                        ('implementations', BEACON_T, STPROS_IMPL, 'upgrade_histories'),
                                        ('admins', ids['stpros'], BOB, 'upgrade_histories')):
            o = observation_for(d, 'testnet'); o[name][key] = value
            self.fails(d, o, check)
        o = observation_for(d, 'testnet'); o['owners'][ids['factory']] = 7
        self.refused(lambda: self.check(d, o), 'OBSERVATION_VALUE')

    def test_obs02_role_observation_universe_is_exact(self):
        d, ids = self.derived(new_vault=False)
        v1 = ids['v1']
        o = observation_for(d, 'testnet')
        self.assertEqual(sorted((r, a) for r, accounts in o['roles'][v1].items() for a in accounts),
                         hi.role_probes(d, 'testnet', v1))
        docs, mism = self.check(d, o)
        self.assertEqual(docs['summary.json']['checks']['role_inventory']['status'], 'PASS')
        stranger, other_role = '0x' + 'ef' * 20, '0x' + '99' * 32
        for mutate in (lambda o: o['roles'][v1].__setitem__(other_role, {E2: False}),              # extra role id
                       lambda o: o['roles'][v1].__setitem__(other_role, {E2: True}),
                       lambda o: o['roles'][v1].__setitem__(other_role, {}),                       # IVH-03: empty map
                       lambda o: o['roles'][v1][ADMIN].__setitem__(stranger, False),               # extra account
                       lambda o: o['roles'][v1][hi.OPERATOR_ROLE].__setitem__(stranger, True)):
            o = observation_for(d, 'testnet'); mutate(o)
            self.refused(lambda: self.check(d, o), 'ROLE_ENUMERATION_INCOMPLETE', 'REFUSED')
        o = observation_for(d, 'testnet'); o['roles'][v1][other_role] = {}
        self.assertIn(f'{v1}: role ids: not derived from history', str(
            self.refused(lambda: self.check(d, o), 'ROLE_ENUMERATION_INCOMPLETE', 'REFUSED')))
        for mutate in (lambda o: o['roles'][v1][hi.OPERATOR_ROLE].pop(REG['testnet']['proxy_admin']),  # missing pair
                       lambda o: o['roles'][v1].pop(hi.OPERATOR_ROLE),                               # missing role
                       lambda o: o['roles'][v1].__setitem__(hi.OPERATOR_ROLE, {})):                  # emptied role
            o = observation_for(d, 'testnet'); mutate(o)
            self.refused(lambda: self.check(d, o), 'ROLE_ENUMERATION_INCOMPLETE', 'INCOMPLETE')
        o = observation_for(d, 'testnet'); o['roles'][v1][ADMIN][E2] = False                     # wrong permission
        _, mism = self.fails(d, o, 'role_inventory')
        self.assertEqual(mism['roles'], [{'vault': v1, 'role': ADMIN, 'account': E2, 'replay': True, 'live_hasRole': False}])
        o = observation_for(d, 'testnet'); o['roles'][v1][ADMIN][E2] = 'true'
        self.refused(lambda: self.check(d, o), 'OBSERVATION_VALUE')
        o = observation_for(d, 'testnet'); o['roles'][v1][ADMIN] = [E2]
        self.refused(lambda: self.check(d, o), 'OBSERVATION_KEYS')

    def test_obs02_role_history_outside_vaults_must_be_observed(self):
        stpros = REG['testnet']['stpros']
        grant = log(stpros, [T['RoleGranted'], hi.OPERATOR_ROLE, word(BOB), word(E2)], 130, 0)
        d, ids = self.derived(new_vault=False, extra_prefix_rows=[grant])
        self.assertIn(stpros, hi.role_targets(d))
        docs, _ = self.check(d, observation_for(d, 'testnet'))
        self.assertEqual(docs['summary.json']['checks']['role_inventory']['status'], 'PASS')
        self.assertIn(stpros, docs['roles.json'])
        o = observation_for(d, 'testnet'); o['roles'].pop(stpros)
        self.refused(lambda: self.check(d, o), 'ROLE_ENUMERATION_INCOMPLETE', 'INCOMPLETE')

    def test_obs03_vault_runtime_identity(self):
        d, ids = self.derived(new_vault=False)
        v1 = ids['v1']
        self.assertEqual(d['factoryBeacons'][ids['factory']], [BEACON_T])
        self.assertEqual([e['value'] for e in d['beaconHistory'][v1]], [BEACON_T])
        docs, _ = self.check(d, observation_for(d, 'testnet'))
        self.assertEqual(docs['summary.json']['checks']['beacon_identity']['status'], 'PASS')
        self.assertEqual(docs['summary.json']['beaconIdentityNotVerified'], [])
        o = observation_for(d, 'testnet'); o['vaultRuntimeKeccak'][v1] = '0x' + 'ab' * 32
        self.fails(d, o, 'beacon_identity')
        o = observation_for(d, 'testnet'); o['vaultRuntimeKeccak'].pop(v1)
        self.refused(lambda: self.check(d, o), 'VAULT_RUNTIME_ENUMERATION_INCOMPLETE', 'INCOMPLETE')
        o = observation_for(d, 'testnet'); o['vaultRuntimeKeccak']['0x' + 'ef' * 20] = '0x' + 'ab' * 32
        self.refused(lambda: self.check(d, o), 'VAULT_RUNTIME_ENUMERATION_INCOMPLETE', 'REFUSED')
        o = observation_for(d, 'testnet'); o['vaultRuntimeKeccak'][v1] = 'abc'
        self.refused(lambda: self.check(d, o), 'OBSERVATION_VALUE')
        for field, value in (('beacon', '0x' + 'aa' * 20), ('factory', '0x' + 'aa' * 20)):
            reviewed = reviewed_for(d); reviewed[v1][field] = value
            self.fails(d, observation_for(d, 'testnet'), 'beacon_identity', reviewed)
        # No reviewed runtime: beacon identity NOT VERIFIED, never PASS.
        docs, mism = self.check(d, observation_for(d, 'testnet'), reviewed={})
        self.assertFalse(mism['beacons'])
        self.assertEqual(docs['summary.json']['checks']['beacon_identity']['status'], 'INCOMPLETE')
        self.assertEqual(docs['summary.json']['beaconIdentityNotVerified'], [v1])
        self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
        with self.assertRaises(inv.InventoryRefused):
            inv.normalize(docs, 'testnet', ids['stpros'])
        # The default reviewed source is the pinned deployed-state snapshot: synthetic vaults are not in it.
        docs, _ = hi.check_observation(d, observation_for(d, 'testnet'), 'testnet')
        self.assertEqual(docs['summary.json']['beaconIdentityNotVerified'], [v1])

    def test_obs03_beacon_history_inconsistency_fails(self):
        d, ids = self.derived(v2_beacon='0x' + 'bb' * 20)          # v2 names a beacon its factory never created
        _, mism = self.fails(d, observation_for(d, 'testnet'), 'beacon_identity')
        # Both the history inconsistency and the reviewed-beacon disagreement are reported for v2 only.
        self.assertEqual(sorted((m['vault'], sorted(m)) for m in mism['beacons']),
                         [(ids['v2'], ['beaconUpgraded', 'factoryBeaconCreated', 'vault']),
                          (ids['v2'], ['reviewedBeacon', 'reviewedFactory', 'vault'])])
        v1 = '0x' + 'c1' * 20
        late = log(v1, [T['ProxyBeaconUpgraded'], word(BEACON_T)], 160, 0)   # outside the creation transaction
        d, ids = self.derived(new_vault=False, extra_prefix_rows=[late])
        self.fails(d, observation_for(d, 'testnet'), 'beacon_identity')
        stray = log(REG['testnet']['stpros'], [T['ProxyBeaconUpgraded'], word(BEACON_T)], 160, 0)
        d, ids = self.derived(new_vault=False, extra_prefix_rows=[stray])
        _, mism = self.fails(d, observation_for(d, 'testnet'), 'beacon_identity')
        self.assertEqual(mism['beacons'], [{'target': REG['testnet']['stpros'], 'beaconUpgraded': 1, 'expected': 'vaults only'}])

    def test_reviewed_runtime_source_is_the_pinned_snapshot(self):
        for chain, count in (('mainnet', 2), ('testnet', 15)):
            reviewed = hi.reviewed_vault_runtimes(chain)
            self.assertEqual(len(reviewed), count)
            self.assertTrue({r['beacon'] for r in reviewed.values()} <= set(REG[chain]['expected_beacons']))
            self.assertTrue({r['factory'] for r in reviewed.values()} <= set(REG[chain]['factories']))
        with mock.patch.dict(h.B0_PREFIX, {'testnet': dict(h.B0_PREFIX['testnet'], end=1)}):
            self.refused(lambda: hi.reviewed_vault_runtimes('testnet'), 'REVIEWED_RUNTIME_SOURCE')


class WithdrawalRecordTest(Tmp):
    """IVH-02: every queue value is validated, and the VToken queue rules hold, before withdrawal_records PASS.

    Valid state used below (Q 20, C 6): Alice requested 2, then 10; Bob 5; Alice 3; Alice then claimed 6,
    the first record fully and 4 of the second (head 1, baseline 2 + 4 = 6, pending 6). Bob's own later
    claim of 5 raises C to 11, above Alice's baseline, which is legitimate."""
    derived = InventoryDerivationTest.derived
    check = InventoryDerivationTest.check
    STPROS = REG['testnet']['stpros']

    def setUp(self):
        super().setUp()
        bob = log(self.STPROS, [T['Withdraw'], word(BOB), word(BOB), word(BOB)], 170, 0, amount(5) + '0' * 64)
        self.d, _ = self.derived(new_vault=False, extra_prefix_rows=[bob])
        self.assertEqual(self.d['withdrawOwners'], [ALICE, BOB])

    def observe(self, queued=20, completed=6, alice=(1, [(6, 6, ZERO20), (17, 3, ALICE)]), bob=(0, [(12, 5, None)])):
        o = observation_for(self.d, 'testnet')
        owners = {}
        for owner, (head, records) in ((ALICE, alice), (BOB, bob)):
            owners[owner] = {'head': head, 'tail': head + len(records), 'records': [
                {'queued': str(b), 'pending': str(p), 'createdAt': '1000', 'unbondingPeriod': '600',
                 **({} if receiver is None else {'receiver': receiver})} for b, p, receiver in records]}
        o['withdrawals'].update(queued=str(queued), completed=str(completed), reserve=str(max(queued - completed, 0)),
                                owners=owners)
        return o

    def outcome(self, o):
        docs, mism = self.check(self.d, o)
        return docs['summary.json']['checks']['withdrawal_records']['status'], docs, mism['withdrawals']

    def test_valid_partial_claims_and_receiver_conventions_pass(self):
        for o in (self.observe(), self.observe(completed=11, bob=(1, []))):     # baseline below C is legitimate
            status, docs, mism = self.outcome(o)
            self.assertEqual((status, mism, docs['summary.json']['overall']), ('PASS', [], 'PASS'))
            normalized = inv.normalize(docs, 'testnet', self.STPROS)
        self.assertEqual(normalized['stpros']['recordQueued'], ['6', '17'])
        self.assertEqual(normalized['stpros']['recordReceivers'], [ZERO20, ALICE])
        status, _, _ = self.outcome(self.observe(bob=(0, [(12, 5, BOB.upper().replace('0X', '0x'))])))
        self.assertEqual(status, 'PASS')

    def test_impossible_queue_states_fail(self):
        for o, rule in (
                (self.observe(bob=(0, [(21, 5, None)])), 'record.queued + record.pending <= Q'),    # baseline above Q
                (self.observe(bob=(0, [(16, 5, None)])), 'record.queued + record.pending <= Q'),
                (self.observe(alice=(1, [(6, 6, None), (10, 3, None)])), 'FIFO: record.queued >= previous record.queued + pending'),
                (self.observe(queued=20, completed=21), 'Q >= C')):
            status, docs, mism = self.outcome(o)
            self.assertEqual(status, 'FAIL')
            self.assertIn(rule, [m.get('rule') for m in mism])
            self.assertEqual(docs['summary.json']['overall'], 'FAIL')
            with self.assertRaises(inv.InventoryRefused):
                inv.normalize(docs, 'testnet', self.STPROS)
        o = self.observe(); o['withdrawals']['owners'][ALICE]['tail'] += 1
        self.assertEqual(self.outcome(o)[2], [{'owner': ALICE, 'head': 1, 'tail': 4, 'records': 2}])

    def test_cross_owner_intervals_must_be_disjoint(self):
        """IVH-04: every request takes the fresh range [Q, Q + assets) and a claim only removes a prefix of one
        range, so outstanding ranges never overlap, across owners as well as within one. Agent C's witness moves
        Bob's baseline 12 to 6: every per-owner and aggregate rule still holds, but [6, 11) overlaps Alice's [6, 12)."""
        rule = 'outstanding [queued, queued + pending) disjoint across all owners'
        valid = (1, [(6, 6, None), (17, 3, None)])
        for alice, bob, expected in (
                # Equal starts: the shorter range sorts first, so Alice's [6, 12) is reported against Bob's [6, 11).
                (valid, (0, [(6, 5, None)]), [(ALICE, 1, ['6', '12'], BOB, 0, ['6', '11'])]),
                # Both of Bob's ranges lie inside Alice's [6, 12); each is reported against it.
                (valid, (0, [(7, 2, None), (10, 3, None)]), [(BOB, 0, ['7', '9'], ALICE, 1, ['6', '12']),
                                                             (BOB, 1, ['10', '13'], ALICE, 1, ['6', '12'])]),
                # Bob's [12, 18) runs into Alice's [17, 20); Alice's first range is shortened to keep the sum.
                ((1, [(6, 5, None), (17, 3, None)]), (0, [(12, 6, None)]),
                 [(ALICE, 2, ['17', '20'], BOB, 0, ['12', '18'])])):
            status, docs, mism = self.outcome(self.observe(alice=alice, bob=bob))
            self.assertEqual(status, 'FAIL')
            self.assertEqual([(m['owner'], m['index'], m['interval'], m['overlapsOwner'], m['overlapsIndex'],
                               m['overlapsInterval']) for m in mism], expected)
            self.assertEqual({m['rule'] for m in mism}, {rule})
            self.assertEqual(docs['summary.json']['overall'], 'FAIL')
            with self.assertRaises(inv.InventoryRefused):
                inv.normalize(docs, 'testnet', self.STPROS)

    def test_disjoint_interleaved_owners_and_touching_ranges_pass(self):
        """Interleaved owners with touching ends, gaps left by completed and partial claims, and a baseline below
        C are all reachable and pass."""
        for queued, completed, alice, bob in (
                (14, 0, (0, [(0, 2, None), (7, 3, None)]), (0, [(2, 5, None), (10, 4, None)])),   # requests only
                (14, 5, (1, [(7, 3, None)]), (0, [(5, 2, None), (10, 4, None)])),     # Alice claimed 2, Bob 3 of 5
                (20, 6, (1, [(6, 6, None), (17, 3, None)]), (0, [(12, 5, None)])),   # [6,12) [12,17) [17,20)
                (20, 11, (1, [(6, 6, None), (17, 3, None)]), (1, []))):              # baseline 6 below C 11
            status, docs, mism = self.outcome(self.observe(queued, completed, alice, bob))
            self.assertEqual((status, mism, docs['summary.json']['overall']), ('PASS', [], 'PASS'))
            inv.normalize(docs, 'testnet', self.STPROS)

    def test_oversized_and_boundary_decimals(self):
        """IVH-05: an oversized decimal string is refused before int(), never by Python's integer-string
        conversion limit, which this checker neither raises nor disables. 2**256 - 1 is the largest encoding."""
        self.assertEqual(hi.UINT256_DIGITS, len(str(2 ** 256 - 1)))
        self.assertNotIn('set_int_max_str_digits', (HERE / 'history_inventory.py').read_text())
        limit = sys.get_int_max_str_digits()
        token = next(t for t, x in self.observe()['tokens'].items() if x['balanceOf'])
        holder = next(iter(self.observe()['tokens'][token]['balanceOf']))
        places = [*((f, lambda o, v, f=f: o['withdrawals']['owners'][BOB]['records'][0].__setitem__(f, v))
                    for f in hi.RECORD_FIELDS),
                  *((k, lambda o, v, k=k: o['withdrawals'].__setitem__(k, v)) for k in ('queued', 'completed', 'reserve')),
                  ('totalSupply', lambda o, v: o['tokens'][token].__setitem__('totalSupply', v)),
                  ('balanceOf', lambda o, v: o['tokens'][token]['balanceOf'].__setitem__(holder, v))]
        for name, place in places:
            for value in ('9' * 5000, '1' + '0' * 4300, '0' * 5000, '1' * 79, '9' * 78, str(2 ** 256)):
                o = self.observe(); place(o, value)
                try:
                    self.check(self.d, o)
                except HistoryRefused as error:
                    self.assertEqual((error.code, error.status), ('OBSERVATION_VALUE', 'REFUSED'), name)
                    self.assertIn('not a uint256 decimal string', str(error), name)
                except ValueError as error:          # e.g. Python's conversion limit: not a structured refusal
                    self.fail(f'{name}: unstructured {type(error).__name__}: {str(error)[:80]}')
                else:
                    self.fail(f'{name}: {len(value)}-character value accepted')
        self.assertEqual(sys.get_int_max_str_digits(), limit)
        top = str(2 ** 256 - 1)
        for value in (top, '0'):
            try:
                self.assertEqual(hi._uint(value, 'x'), int(value))
            except HistoryRefused as error:
                self.fail(f'{len(value)}-digit uint256 refused: {error}')
        for field, value, status in (('createdAt', top, 'PASS'), ('unbondingPeriod', top, 'PASS'),
                                     ('createdAt', '0', 'PASS'), ('unbondingPeriod', '0', 'PASS'),
                                     ('queued', top, 'FAIL'), ('pending', top, 'FAIL')):   # parsed, then bounded by Q
            o = self.observe(); o['withdrawals']['owners'][BOB]['records'][0][field] = value
            self.assertEqual(self.outcome(o)[0], status, (field, value))
        o = self.observe(); o['withdrawals'].update(queued=top, reserve='0')
        self.assertIn({'sum_pending': '14', 'Q_minus_C': str(2 ** 256 - 7)}, self.outcome(o)[2])

    def test_zero_pending_record_requires_review(self):
        # An empty range takes no part in the disjointness rule; the record stays INCOMPLETE, never PASS.
        for alice, bob, at in (((1, [(6, 6, None), (17, 3, None), (20, 0, None)]), (0, [(12, 5, None)]), (ALICE, 3)),
                               ((1, [(6, 6, None), (17, 3, None)]), (0, [(8, 0, None), (12, 5, None)]), (BOB, 0))):
            status, docs, mism = self.outcome(self.observe(alice=alice, bob=bob))
            self.assertEqual((status, mism), ('INCOMPLETE', []))
            self.assertEqual([(r['owner'], r['index']) for r in docs['withdrawals.json']['review']], [at])
            self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
            with self.assertRaisesRegex(inv.InventoryRefused, 'summary checks not all PASS'):
                inv.normalize(docs, 'testnet', self.STPROS)

    def test_malformed_values_are_refused(self):
        for head, tail in ((-2, 0), (3, 1), (True, 3), ('1', 3), (1.0, 3), (2 ** 256, 2 ** 256 + 2), (1, None)):
            o = self.observe(); o['withdrawals']['owners'][ALICE].update(head=head, tail=tail)
            self.refused(lambda: self.check(self.d, o), 'OBSERVATION_VALUE', 'REFUSED')
        for field in hi.RECORD_FIELDS:
            for value in (None, 1000, '1.0', '-1', '٣', '01', '', ' 1', str(2 ** 256)):
                o = self.observe(); o['withdrawals']['owners'][BOB]['records'][0][field] = value
                self.refused(lambda: self.check(self.d, o), 'OBSERVATION_VALUE', 'REFUSED')
        for value in ('0x123', 5, None, '0x' + 'g' * 40, ALICE + '00'):
            o = self.observe(); o['withdrawals']['owners'][BOB]['records'][0]['receiver'] = value
            self.refused(lambda: self.check(self.d, o), 'OBSERVATION_VALUE', 'REFUSED')
        for key, value in (('queued', '020'), ('completed', 6), ('reserve', str(2 ** 256))):
            o = self.observe(); o['withdrawals'][key] = value
            self.refused(lambda: self.check(self.d, o), 'OBSERVATION_VALUE', 'REFUSED')
        for mutate in (lambda r: r.pop('createdAt'), lambda r: r.__setitem__('note', '1')):
            o = self.observe(); mutate(o['withdrawals']['owners'][BOB]['records'][0])
            self.refused(lambda: self.check(self.d, o), 'OBSERVATION_KEYS', 'REFUSED')
        o = self.observe(); o['withdrawals']['owners'][BOB]['records'][0] = ['12', '5']
        self.refused(lambda: self.check(self.d, o), 'OBSERVATION_KEYS', 'REFUSED')


def topic(signature):
    return h.logscan.keccak256(signature.encode())


# YieldVault round/settlement life-cycle events (IVH-01): phase, round terms and settlement are not observed.
LIFE_CYCLE = ('RoundOpened(uint256,uint256,uint256,uint256)', 'OpenPeriodExtended(uint256,uint256,uint256)',
              'RoundLocked(uint256,uint256,uint256)', 'SettlementProposed(uint256,uint8,uint256,uint256,bool,address)',
              'SettlementCancelled(uint256,uint256)', 'SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)',
              'ParSettlementForced(uint256,uint256)', 'ProfitFunded(address,uint256)', 'ProfitRefunded(address,uint256)',
              'CounterpartyProceedsPaid(address,uint256)')


def source_event_signatures():
    """Canonical event signatures of production `contracts/` and every OpenZeppelin file it imports
    (Foundry remappings), with enum parameters encoded as uint8."""
    remap = {'@openzeppelin/contracts-upgradeable/': 'lib/openzeppelin-contracts-upgradeable/',
             '@openzeppelin/contracts/': 'lib/openzeppelin-contracts/'}

    def resolve(base, imported):
        for prefix, target in remap.items():
            if imported.startswith(prefix):
                return (ROOT / target / imported[len(prefix):]).resolve()
        return (base.parent / imported).resolve()
    todo, seen = list((ROOT / 'contracts').rglob('*.sol')), set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        todo += [resolve(path, i) for i in re.findall(r'import\s+(?:\{[^}]*\}\s+from\s+)?"([^"]+)"', path.read_text())]
    enums = {e for path in seen for e in re.findall(r'\benum\s+(\w+)', path.read_text())}
    signatures = set()
    for path in seen:
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', path.read_text(), flags=re.S)
        for name, params in re.findall(r'\bevent\s+(\w+)\s*\(([^)]*)\)', text):
            types = [p.split()[0] for p in (x.strip() for x in params.split(',')) if p]
            signatures.add(f"{name}({','.join('uint8' if t in enums else t for t in types)})")
    return signatures


def legacy_event_signatures():
    out = set()
    for path in sorted((ROOT / 'deployments').glob('*/*.json')):
        for item in json.loads(path.read_text()).get('abi', []):
            if item.get('type') == 'event':
                out.add(item['name'] + '(' + ','.join(i['type'] for i in item['inputs']) + ')')
    return out


class EventClassificationTest(unittest.TestCase):
    """The authority gate covers every event the reviewed legacy and production sources can emit."""

    def test_every_source_event_is_classified_once(self):
        table = {sig for sigs in hi.EVENT_CLASSES.values() for sig in sigs}
        self.assertEqual(sum(len(v) for v in hi.EVENT_CLASSES.values()), len(table), 'duplicate signature')
        self.assertEqual(len(hi.EVENTS), len(table), 'topic collision')
        legacy, production = legacy_event_signatures(), source_event_signatures()
        self.assertEqual(len(legacy), 55)
        self.assertEqual(sorted(legacy - table), [], 'legacy deployment ABI event not classified')
        self.assertEqual(sorted(production - table), [], 'production source event not classified')
        self.assertEqual(sorted(table - legacy - production), [], 'classified event absent from reviewed sources')
        for signature in ('OwnershipTransferStarted(address,address)', 'GuardianChanged(address,address)',
                          'TimelockChanged(address,address)', 'ApprovedPerformanceFeeChanged(uint16,uint16)'):
            self.assertIn(signature, production - legacy, 'R4-only event')

    def test_required_families_and_native_release_topics_are_gated(self):
        cls = {sig: c for c, sigs in hi.EVENT_CLASSES.items() for sig in sigs}
        for signature in ('OwnershipTransferStarted(address,address)', 'OwnershipTransferred(address,address)',
                          'GuardianChanged(address,address)', 'TimelockChanged(address,address)',
                          'KeeperChanged(address,address)', 'RoleGranted(bytes32,address,address)',
                          'RoleRevoked(bytes32,address,address)', 'RoleAdminChanged(bytes32,bytes32,bytes32)',
                          'CounterpartyWhitelistUpdated(address,bool)', 'SlpWhitelistUpdated(address,bool)',
                          'Upgraded(address)', 'AdminChanged(address,address)', 'BeaconUpgraded(address)',
                          'BeaconUpgraded(address,address)', 'Paused(address)', 'Unpaused(address)'):
            self.assertEqual(cls[signature], hi.AUTHORITY, signature)
        from tools.release.native.checkpoint import CONFIG_TOPICS
        gated = {t for t, (_, _, c) in hi.EVENTS.items() if c in (hi.AUTHORITY, hi.CONFIGURATION)}
        # MinDelayChange is a Timelock event: no Timelock exists before release and none is imported by
        # production contracts, so it is outside the campaign history (native GC-03 covers it).
        self.assertEqual(sorted(set(CONFIG_TOPICS) - gated), [topic('MinDelayChange(uint256,uint256)')])
        for name, value in T.items():   # B0 replay topics keep their meaning
            self.assertEqual(hi.EVENTS[value][2], hi.OPERATIONAL if name in ('Transfer', 'Withdraw') else hi.AUTHORITY, name)

    def test_pool_correction_is_configuration_and_routine_accounting_is_operational(self):
        cls = {sig: c for c, sigs in hi.EVENT_CLASSES.items() for sig in sigs}
        # Production Oracle emits SetTokenAmount from initializePool and from the onlyOwner
        # compare-and-set setTokenAmount correction; the signature cannot tell them apart.
        oracle = (ROOT / 'contracts/Oracle.sol').read_text()
        self.assertEqual(oracle.count('emit SetTokenAmount('), 2)
        self.assertIn('function setTokenAmount(address token, uint256 expectedOldT, uint256 newT) external onlyOwner', oracle)
        self.assertEqual(cls['SetTokenAmount(address,uint256,uint256)'], hi.CONFIGURATION)
        for signature in ('Transfer(address,address,uint256)', 'Deposit(address,address,uint256,uint256)',
                          'Withdraw(address,address,address,uint256,uint256)', 'PoolUpdated(address,uint256,uint256,bool)',
                          'TokenAmountUpdated(address,uint256,uint256)',
                          'CommissionMinted(address,address,address,uint256,uint256)',
                          'CommissionMinted(address,address,uint256)'):
            self.assertEqual(cls[signature], hi.OPERATIONAL, signature)
        self.assertEqual({c: len(v) for c, v in hi.EVENT_CLASSES.items()},
                         {hi.AUTHORITY: 24, hi.CONFIGURATION: 27, hi.OPERATIONAL: 13})

    def test_vault_life_cycle_events_require_review(self):
        """IVH-01: from the exact recorded legacy source, bound to the reviewed runtime identity."""
        record = json.loads((ROOT / 'deployments/testnet/YieldVault.json').read_text())
        runtime = h.logscan.keccak256(bytes.fromhex(record['deployedBytecode'][2:]))
        state = json.loads((ROOT / 'snapshots/deployed/testnet/state.json').read_text())
        beacons = [b for b in state['beacons'].values() if b['implementation_code']['keccak256'] == runtime]
        self.assertEqual(len(beacons), 1)
        self.assertIn('deployments/testnet/YieldVault.json', beacons[0]['implementation_code_matches_records'])
        metadata = record['metadata'] if isinstance(record['metadata'], dict) else json.loads(record['metadata'])
        source = metadata['sources']['project/contracts/YieldVault.sol']
        self.assertEqual(h.logscan.keccak256(source['content'].encode()), source['keccak256'])

        def body(text, signature):
            return text.split(signature, 1)[1].split('\n    function ', 1)[0]
        legacy = source['content']
        reset = body(legacy, 'function setOpenPeriodRemaining(uint256 remaining) external {')   # no access check
        self.assertIn('_phase = Phase.OPEN;', reset)
        self.assertIn('emit OpenPeriodExtended(', reset)
        self.assertIn('function extendOpenPeriod(uint256 extension) external onlyRole(DEFAULT_ADMIN_ROLE)', legacy)
        for setter in ('setRoundCap(uint256 newRoundCap)', 'setLockDuration(uint256 newLockDuration)',
                       'setMaxLossBps(uint256 newMaxLossBps)'):                                   # eventless
            self.assertNotIn('emit ', body(legacy, f'function {setter} external {{'))
        for text in (legacy, (ROOT / 'contracts/YieldVault.sol').read_text()):
            terms = body(text, 'function _openRound(RoundParams calldata params) internal {')
            self.assertIn('performanceFeeBps = params.performanceFeeBps;', terms)
            self.assertIn('maxLossBps = params.maxLossBps;', terms)
            self.assertIn('emit RoundOpened(roundIndex, openedAt, openDeadline, roundCap);', terms)
        cls = {sig: c for c, sigs in hi.EVENT_CLASSES.items() for sig in sigs}
        for signature in LIFE_CYCLE:
            self.assertEqual(cls[signature], hi.CONFIGURATION, signature)
        for signature in ('Transfer(address,address,uint256)', 'Deposit(address,address,uint256,uint256)',
                          'Withdraw(address,address,address,uint256,uint256)'):
            self.assertEqual(cls[signature], hi.OPERATIONAL, signature)

    def test_real_b0_history_has_no_unclassified_event(self):
        archive, directory = os.environ.get('FAROO_B0_ARCHIVE'), os.environ.get('FAROO_B0_INVENTORY_DIR')
        if not archive or not directory:
            self.skipTest('NOT RUN: set FAROO_B0_ARCHIVE and FAROO_B0_INVENTORY_DIR')
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.isfile() and re.fullmatch(r'logscan/(mainnet|testnet)/chunks/[0-9]+-[0-9]+\.json', member.name):
                    chain = member.name.split('/')[1]
                    for row in json.loads(tar.extractfile(member).read())['logs']:
                        self.assertNotEqual(hi.classify(row, chain)[2], hi.UNCLASSIFIED, (chain, row['topics'][0]))


class AuthorityGateTest(Tmp):
    """Post-pin authority, configuration and unclassified events stay REVIEW REQUIRED; operational ones do not."""
    derived = InventoryDerivationTest.derived
    check = InventoryDerivationTest.check
    FACTORY = REG['testnet']['factories'][0]
    V1 = '0x' + 'c1' * 20

    def statuses(self, *rows, observe=None):
        d, ids = self.derived(new_vault=False, extra_rows=list(rows))
        o = observation_for(d, 'testnet')
        if observe:
            observe(o)
        docs, mism = self.check(d, o)
        return d, docs, mism, ids

    def assert_gated(self, docs, gate):
        checks = {k: c['status'] for k, c in docs['summary.json']['checks'].items()}
        self.assertEqual(checks[gate], 'INCOMPLETE', checks)
        self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
        with self.assertRaisesRegex(inv.InventoryRefused, 'summary checks not all PASS'):
            inv.normalize(docs, 'testnet', REG['testnet']['stpros'])

    def test_isolated_ownership_transfer_started_is_never_an_unqualified_pass(self):
        started = log(self.FACTORY, [topic('OwnershipTransferStarted(address,address)'), word(E2), word(BOB)], 3300, 0)
        d, docs, mism, ids = self.statuses(started)
        self.assertEqual(d['owners'][self.FACTORY], E2)                   # owner() replay unchanged
        self.assertEqual(d['pendingOwners'], {self.FACTORY: BOB})
        self.assertEqual([e['event'] for e in d['authorityEventsAfterPin']], ['OwnershipTransferStarted'])
        self.assertFalse(any(mism.values()))
        self.assert_gated(docs, 'post_pin_authority_review')
        self.assertEqual(docs['summary.json']['checks']['pending_owner_evidence']['status'], 'INCOMPLETE')
        self.assertEqual(docs['summary.json']['checks']['ownership_histories']['status'], 'PASS')
        self.assertEqual(docs['summary.json']['pendingOwnerEvidence']['nonzero'], [self.FACTORY])

    def test_missing_pending_owner_evidence_is_not_zero(self):
        started = log(self.FACTORY, [topic('OwnershipTransferStarted(address,address)'), word(E2), word(BOB)], 3300, 0)
        d, ids = self.derived(new_vault=False, extra_rows=[started])
        o = observation_for(d, 'testnet'); o['pendingOwners'].pop(self.FACTORY)
        self.refused(lambda: self.check(d, o), 'PENDING_OWNER_EVIDENCE_INCOMPLETE', 'INCOMPLETE')
        o = observation_for(d, 'testnet'); del o['pendingOwners']
        self.refused(lambda: self.check(d, o), 'OBSERVATION_KEYS')
        o = observation_for(d, 'testnet'); o['pendingOwners'][self.FACTORY] = ZERO20     # zero is a claim, not a default
        docs, mism = self.check(d, o)
        self.assertEqual(mism['pendingOwners'], [{'target': self.FACTORY, 'replay': BOB, 'live': ZERO20}])
        self.assertEqual(docs['summary.json']['checks']['pending_owner_evidence']['status'], 'FAIL')
        o = observation_for(d, 'testnet'); o['pendingOwners'][BEACON_T] = ZERO20         # target without Ownable2Step
        self.refused(lambda: self.check(d, o), 'PENDING_OWNER_EVIDENCE_INCOMPLETE', 'REFUSED')
        o = observation_for(d, 'testnet'); o['pendingOwners'][self.FACTORY] = None
        self.refused(lambda: self.check(d, o), 'OBSERVATION_VALUE')

    def test_completed_or_cancelled_two_step_transfer_still_requires_review(self):
        started = log(self.FACTORY, [topic('OwnershipTransferStarted(address,address)'), word(E2), word(BOB)], 3300, 0)
        accepted = log(self.FACTORY, [T['OwnershipTransferred'], word(E2), word(BOB)], 3301, 0)
        d, docs, mism, _ = self.statuses(started, accepted)
        self.assertEqual((d['owners'][self.FACTORY], d['pendingOwners'][self.FACTORY]), (BOB, ZERO20))
        self.assertEqual(docs['summary.json']['checks']['pending_owner_evidence']['status'], 'PASS')
        self.assert_gated(docs, 'post_pin_authority_review')
        cancelled = log(self.FACTORY, [topic('OwnershipTransferStarted(address,address)'), word(E2), word(ZERO20)], 3302, 0)
        d, docs, mism, _ = self.statuses(started, cancelled)
        self.assertEqual(d['pendingOwners'][self.FACTORY], ZERO20)
        self.assert_gated(docs, 'post_pin_authority_review')

    def test_guardian_timelock_keeper_and_trigger_changes_require_review(self):
        for signature in ('GuardianChanged(address,address)', 'TimelockChanged(address,address)',
                          'KeeperChanged(address,address)', 'TriggerAddressChanged(address,address)',
                          'Paused(address)', 'CounterpartyWhitelistUpdated(address,bool)',
                          'BeaconUpgraded(address,address)', 'Initialized(uint64)'):
            row = log(self.FACTORY, [topic(signature), *([word(E2), word(BOB)] if signature.count(',') == 1 and
                                                          'bool' not in signature else [])], 3300, 0)
            d, docs, mism, _ = self.statuses(row)
            self.assertEqual([e['signature'] for e in d['authorityEventsAfterPin']], [signature])
            self.assert_gated(docs, 'post_pin_authority_review')
            self.assertEqual(docs['summary.json']['checks']['post_pin_configuration_review']['status'], 'PASS')

    def test_role_privilege_changes_stay_fail_closed(self):
        grant = log(self.V1, [T['RoleGranted'], hi.OPERATOR_ROLE, word(BOB), word(E2)], 3300, 0)
        d, docs, mism, _ = self.statuses(grant)
        self.assertEqual(docs['summary.json']['checks']['role_inventory']['status'], 'PASS')   # observation agrees
        self.assert_gated(docs, 'post_pin_authority_review')
        revoke = log(self.V1, [T['RoleRevoked'], ADMIN, word(E2), word(E2)], 3301, 0)
        d, docs, mism, _ = self.statuses(grant, revoke)
        self.assert_gated(docs, 'post_pin_authority_review')
        admin_change = log(self.V1, [T['RoleAdminChanged'], hi.OPERATOR_ROLE, ADMIN, '0x' + '77' * 32], 3300, 0)
        d, docs, mism, _ = self.statuses(admin_change)
        self.assertEqual(docs['summary.json']['checks']['role_inventory']['status'], 'FAIL')
        self.assertEqual(docs['summary.json']['checks']['post_pin_authority_review']['status'], 'INCOMPLETE')

    def test_configuration_changes_require_review(self):
        for signature in ('UnbondingPeriodChanged(uint256,uint256)', 'SlpChanged(address,address)',
                          'MaxAprPpmChanged(uint256)', 'PhaseChanged(uint256,uint8,uint8)',
                          'EmergencyCancelled(address,address)', 'ApprovedPerformanceFeeChanged(uint16,uint16)'):
            d, docs, mism, _ = self.statuses(log(self.FACTORY, [topic(signature)], 3300, 0))
            self.assertEqual([e['signature'] for e in d['configurationEventsAfterPin']], [signature])
            self.assertEqual(d['authorityEventsAfterPin'], [])
            self.assert_gated(docs, 'post_pin_configuration_review')

    def test_post_pin_pool_correction_requires_configuration_review(self):
        stpros = REG['testnet']['stpros']
        correction = log(stpros, [topic('SetTokenAmount(address,uint256,uint256)'), word(stpros)], 3300, 0,
                         amount(40) + amount(25)[2:])
        d, docs, mism, ids = self.statuses(correction)
        self.assertEqual(d['owners'][self.FACTORY], E2)                   # ownership unchanged
        self.assertEqual((d['authorityEventsAfterPin'], d['unclassifiedEventsAfterPin']), ([], []))
        self.assertEqual([e['signature'] for e in d['configurationEventsAfterPin']],
                         ['SetTokenAmount(address,uint256,uint256)'])
        self.assertNotIn('SetTokenAmount', d['operationalEventCountsAfterPin'])
        self.assertFalse(any(mism.values()))                              # inventory otherwise consistent
        self.assertEqual(docs['summary.json']['checks']['post_pin_authority_review']['status'], 'PASS')
        self.assert_gated(docs, 'post_pin_configuration_review')
        # Routine mint/redeem pool accounting and commission accounting stay operational.
        routine = [log(stpros, [topic('PoolUpdated(address,uint256,uint256,bool)'), word(stpros)], 3301, 0),
                   log(stpros, [topic('CommissionMinted(address,address,uint256)'), word(BOB), word(ALICE)], 3301, 1)]
        d, docs, mism, _ = self.statuses(*routine)
        self.assertEqual(d['configurationEventsAfterPin'], [])
        self.assertEqual(docs['summary.json']['overall'], 'PASS')

    def test_post_pin_vault_life_cycle_events_require_review(self):
        """IVH-01: a phase reset or round-term change with every observed field consistent is never a PASS."""
        for signature in LIFE_CYCLE:
            d, docs, mism, _ = self.statuses(log(self.V1, [topic(signature), amount(1)], 3300, 0, amount(100) + amount(200)[2:]))
            self.assertEqual([e['signature'] for e in d['configurationEventsAfterPin']], [signature])
            # Only the fixture's own post-pin Transfer is counted as operational.
            self.assertEqual((d['authorityEventsAfterPin'], d['operationalEventCountsAfterPin']), ([], {'Transfer': 1}))
            self.assertFalse(any(mism.values()), signature)
            self.assertEqual(docs['summary.json']['checks']['post_pin_authority_review']['status'], 'PASS')
            self.assert_gated(docs, 'post_pin_configuration_review')
        # The same pipeline without a life-cycle event is a PASS of limited meaning only.
        d, docs, mism, _ = self.statuses(log(self.V1, [T['Transfer'], word(BOB), word(ALICE)], 3300, 0, amount(0)))
        summary = docs['summary.json']
        self.assertEqual(summary['overall'], 'PASS')
        self.assertEqual((summary.get('overallMeaning'), summary.get('notObserved')),
                         (hi.OVERALL_MEANING, list(hi.NOT_OBSERVED)))
        self.assertIn('not migration readiness', hi.OVERALL_MEANING)
        self.assertTrue(hi.NOT_OBSERVED[0].startswith('YieldVault phase, round terms'))
        self.assertFalse(summary['provenance']['authenticated'])

    def test_ordinary_operational_events_are_not_authority_changes(self):
        stpros = REG['testnet']['stpros']
        rows = [log(stpros, [T['Transfer'], word(BOB), word(ALICE)], 3300, 0, amount(1)),
                log(stpros, [topic('Approval(address,address,uint256)'), word(BOB), word(ALICE)], 3300, 1, amount(5)),
                log(stpros, [topic('Deposit(address,address,uint256,uint256)'), word(BOB), word(BOB)], 3301, 0)]
        d, docs, mism, _ = self.statuses(*rows)
        self.assertEqual((d['authorityEventsAfterPin'], d['configurationEventsAfterPin'], d['unclassifiedEventsAfterPin']),
                         ([], [], []))
        self.assertEqual(d['operationalEventCountsAfterPin'], {'Approval': 1, 'Deposit': 1, 'Transfer': 2})
        self.assertFalse(any(mism.values()))
        self.assertEqual(docs['summary.json']['overall'], 'PASS')
        inv.normalize(docs, 'testnet', stpros)
        # A Transfer that disagrees with the observation fails the holder section, not an authority gate.
        d, docs, mism, _ = self.statuses(*rows, observe=lambda o: o['tokens'][stpros]['balanceOf'].__setitem__(BOB, '0'))
        self.assertEqual(docs['summary.json']['checks']['share_holders']['status'], 'FAIL')
        self.assertEqual(docs['summary.json']['checks']['post_pin_authority_review']['status'], 'PASS')

    def test_unclassified_events_are_never_harmless(self):
        unknown = '0x' + hashlib.sha256(b'unreviewed event').hexdigest()
        d, docs, mism, _ = self.statuses(log(self.FACTORY, [unknown], 3300, 0))
        self.assertEqual([(e['event'], e['class']) for e in d['unclassifiedEventsAfterPin']], [('UnknownEvent', hi.UNCLASSIFIED)])
        self.assert_gated(docs, 'post_pin_unclassified_review')
        self.assertEqual(docs['summary.json']['checks']['unclassified_history_review']['status'], 'INCOMPLETE')
        d, ids = self.derived(new_vault=False, extra_prefix_rows=[log(self.FACTORY, [unknown], 130, 0)])
        docs, _ = self.check(d, observation_for(d, 'testnet'))
        self.assertEqual(docs['summary.json']['checks']['post_pin_unclassified_review']['status'], 'PASS')
        self.assert_gated(docs, 'unclassified_history_review')

    @with_safe_target()
    def test_post_pin_safe_events_are_unclassified(self):
        a, prefix, targets = mainnet_case(self.root)
        safe = h.GOV_SAFE
        a.segments.pop()
        a.segment('incremental', targets, PIN + 1, ANCHOR, [log(safe, [topic('ChangedThreshold(uint256)')], 3300, 0)])
        name, digest, _ = a.manifest(FROM, ANCHOR, targets)
        d = hi.derive(self.auth(a, prefix, name, digest), 'mainnet')
        self.assertEqual([(e['address'], e['event']) for e in d['unclassifiedEventsAfterPin']], [(safe, 'SafeEvent')])
        self.assertEqual(d['unclassifiedTopics'], {})          # Safe history stays under the FR-X8 BLOCKED replay
        self.assertTrue(d['safe'][safe]['replay'].startswith('BLOCKED'))


class CampaignBindingTest(Tmp):
    """`campaign_input.load` runs content verification for a declared manifest (fake export skeleton)."""

    def export(self):
        root = self.root / 'export'
        (root / 'tools/ci').mkdir(parents=True)
        (root / 'tools/ci/r4-reference.json').write_text('{}\n')
        ref = sha(root / 'tools/ci/r4-reference.json')
        (root / 'cache/native-r4').mkdir(parents=True)
        (root / 'cache/native-r4/manifest.json').write_text(json.dumps({'referenceSha256': ref}))
        (root / 'snapshots/deployed').mkdir(parents=True)
        (root / 'snapshots/deployed/pins.json').write_text(json.dumps({'mainnet': 1, 'testnet': PIN}))
        (root / 'EXPORT-MANIFEST.txt').write_text(json.dumps({'sourceHead': 'a' * 40}))
        return root

    def input_for(self, root, history_sha, inventory_sha):
        actors = {}
        for i, role in enumerate(ci.REQUIRED_ACTORS['testnet']):
            real = ci.REAL_IDENTITIES['testnet'].get(role)
            actors[role] = {'address': real or '0x' + format(0xc0 + i, '040x'),
                            'kind': 'REAL' if real else 'LABELLED_SYNTHETIC', 'label': role}
        return {'schema': ci.SCHEMA, 'campaign': 'T', 'chain': 'testnet', 'chainId': 688689, 'evidenceClass': 'E_FRESH',
                'fork': {'block': ANCHOR, 'blockHash': block_hash(ANCHOR)},
                'source': {'productionHead': 'a' * 40, 'exportManifestSha256': '0x' + sha(root / 'EXPORT-MANIFEST.txt'),
                           'r4ReferenceSha256': '0x' + sha(root / 'tools/ci/r4-reference.json'),
                           'nativeR4ManifestSha256': '0x' + sha(root / 'cache/native-r4/manifest.json')},
                'inventory': {'path': 'inputs/inventory.json', 'sha256': inventory_sha, 'anchorBlock': ANCHOR,
                              'complete': True, 'historyManifestSha256': history_sha},
                'actors': actors, 'safe': {'mode': 'ABSENT', 'identityManifestSha256': ZERO32},
                'decisions': {k: 'UNDECIDED' for k in ci.DECISIONS}}

    def stage(self, root):
        cache = root / ci.INPUT_ROOT
        (cache / 'inputs').mkdir(parents=True)
        (cache / 'inputs/inventory.json').write_text('{"schema":"faroo-fork-inventory-v1"}\n')
        inventory_sha = '0x' + sha(cache / 'inputs/inventory.json')
        history_root = cache / 'history'
        a, prefix, targets, _ = testnet_case(history_root)
        _, digest, _ = a.manifest(FROM, ANCHOR, targets, inventory_sha=inventory_sha, name='staging.json')
        (history_root / 'staging.json').rename(history_root / f'0x{digest}.json')
        return a, prefix, inventory_sha, '0x' + digest

    def test_self_consistent_history_is_refused_without_the_pinned_b0_archive(self):
        root = self.export()
        _, _, inventory_sha, history_sha = self.stage(root)
        path = self.root / 'input.json'
        path.write_text(json.dumps(self.input_for(root, history_sha, inventory_sha)))
        with self.assertRaisesRegex(ci.InputRefused, 'history manifest: INCOMPLETE: B0_PREFIX_REQUIRED'):
            ci.load(path, root)

    def test_content_verified_history_stays_incomplete(self):
        root = self.export()
        _, prefix, inventory_sha, history_sha = self.stage(root)
        path = self.root / 'input.json'
        doc = self.input_for(root, history_sha, inventory_sha)
        path.write_text(json.dumps(doc))
        with mock.patch.dict(h.B0_PREFIX, {'testnet': prefix}):
            _, _, blockers = ci.load(path, root)
            self.assertIn(('INCOMPLETE', ci.HISTORY_CONTENT_ONLY), blockers)
            self.assertIn(('INCOMPLETE', 'authenticated history not verified: the native history verifier did not run'),
                          blockers)
            self.assertIn(('INCOMPLETE', 'final FS acceptance gate not implemented (fail-closed)'), blockers)
            bad = copy.deepcopy(doc); bad['fork']['blockHash'] = '0x' + '77' * 32
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ci.InputRefused, 'HISTORY_BINDING'):
                ci.load(path, root)


class NativeDifferentialTest(Tmp):
    """The segment/chunk/window walk agrees with the native release verifier on release-shaped input."""

    def setup_release(self, root, mutate=None):
        names = ('oracle', 'stpros', 'bridge', 'distributor', 'proxyAdmin', 'timelock', 'assertions', 'asset')
        addresses = {n: '0x' + format(0x1000 + i, '040x') for i, n in enumerate(names)}
        factory = {'address': '0x' + format(0x2000, '040x'), 'beacon': '0x' + format(0x2001, '040x'),
                   'vaults': [{'address': '0x' + format(0x2002, '040x')}]}
        config = {'chainId': 688689, 'addresses': addresses, 'factories': [factory]}
        targets = history_targets(config)
        rows = [log(addresses['stpros'], [T['Transfer'], word(ALICE), word(BOB)], 500, 0, amount(1)),
                log(factory['vaults'][0]['address'], [T['RoleGranted'], ADMIN, word(E2), word(E2)], 1700, 2)]
        a = Archive(root, 'testnet')
        a.segment('a', targets, FROM, 2099, rows)
        a.segment('b', targets, 2100, ANCHOR, [log(addresses['oracle'], [T['Upgraded'], word(BOB)], 3000, 0)])
        if mutate:
            mutate(a, targets)
        name, digest, manifest = a.manifest(FROM, ANCHOR, targets)
        native = {k: v for k, v in manifest.items() if k not in ('chain',)}
        native['schema'], native['inventorySha256'] = 'faroo-history-v1', 'f' * 64
        native['segments'] = [{'planFile': str(a.root / s['planFile']), 'planSha256': s['planSha256'],
                               'chunks': [{'path': str(a.root / c['path']), 'sha256': c['sha256']} for c in s['chunks']]}
                              for s in manifest['segments']]
        native_sha = write(a.root / 'native.json', native)
        config.update(historyFile=str(a.root / 'native.json'), observed={'blockNumber': ANCHOR, 'blockHash': block_hash(ANCHOR)},
                      review={'historySha256': native_sha, 'inventorySha256': 'f' * 64})
        return a, manifest, config

    def run_both(self, root, mutate=None):
        a, manifest, config = self.setup_release(root, mutate)
        native = adapter = None
        try:
            native = authenticate_history(config, {'fromBlock': FROM})['rows']
        except j.ReleaseError as error:
            native = error.code
        try:
            adapter = h.verify_archive(manifest, a.root, 'testnet')['rows']
        except HistoryRefused as error:
            adapter = error.code
        return native, adapter

    def test_agreement_on_valid_and_defective_archives(self):
        native, adapter = self.run_both(self.root / 'ok')
        self.assertIsInstance(native, list)
        self.assertEqual(native, adapter)

        def drop_chunk(a, _t):
            a.segments[1]['chunks'].pop()

        def wrong_count(a, _t):
            ref = a.segments[1]['chunks'][0]
            doc = json.loads((a.root / ref['path']).read_text()); doc['windows'][0][2] += 1
            ref['sha256'] = write(a.root / ref['path'], doc)

        def duplicate(a, _t):
            ref = a.segments[0]['chunks'][0]
            doc = json.loads((a.root / ref['path']).read_text()); doc['logs'].append(copy.deepcopy(doc['logs'][0]))
            doc['windows'][0][2] += 1
            ref['sha256'] = write(a.root / ref['path'], doc)

        def target_gap(a, targets):
            a.segments.pop(1)
            a.segment('b', targets[1:], 2100, ANCHOR, [])
            a.segment('c', targets[:1], 2200, ANCHOR, [])

        def overlap(a, targets):
            a.segment('c', targets[:1], 4000, ANCHOR, [])

        def wrong_filter(a, _t):
            seg = a.segments[1]
            plan = json.loads((a.root / seg['planFile']).read_text()); plan['filter_id'] = '0' * 16
            seg['planSha256'] = write(a.root / seg['planFile'], plan)

        def tampered(a, _t):
            ref = a.segments[1]['chunks'][0]
            (a.root / ref['path']).write_text((a.root / ref['path']).read_text().replace('"fetched_utc"', '"fetched_utc" '))

        def outside(a, _t):
            ref = a.segments[1]['chunks'][-1]
            doc = json.loads((a.root / ref['path']).read_text())
            doc['logs'].append(log('0x' + format(0x1000, '040x'), [T['Upgraded'], word(ALICE)], ANCHOR + 1, 0))
            ref['sha256'] = write(a.root / ref['path'], doc)

        for mutate, code in ((drop_chunk, 'HISTORY_CHUNK_GAP'), (wrong_count, 'HISTORY_WINDOW_COUNT'),
                             (duplicate, 'HISTORY_DUPLICATE_LOG'), (target_gap, 'HISTORY_TARGET_GAP_OR_OVERLAP'),
                             (overlap, 'HISTORY_TARGET_GAP_OR_OVERLAP'), (wrong_filter, 'HISTORY_FILTER'),
                             (outside, 'HISTORY_LOG_RANGE')):
            native, adapter = self.run_both(self.root / code / mutate.__name__, mutate)
            self.assertEqual((native, adapter), (code, code), mutate.__name__)
        native, adapter = self.run_both(self.root / 'tampered', tampered)
        self.assertEqual(native, 'REVIEWED_FILE_HASH')
        self.assertEqual(adapter, 'HISTORY_CHUNK_HASH_REQUIRED')


class RealB0ArchiveTest(unittest.TestCase):
    """Historical re-derivation from the immutable B0 archive (E-hist content only, never E-fresh)."""

    def setUp(self):
        archive, directory = os.environ.get('FAROO_B0_ARCHIVE'), os.environ.get('FAROO_B0_INVENTORY_DIR')
        if not archive or not directory:
            self.skipTest('NOT RUN: set FAROO_B0_ARCHIVE and FAROO_B0_INVENTORY_DIR to the archived B0 evidence')
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()   # native reader refuses symlinked components
        self.directory = Path(directory)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.isfile() and re.fullmatch(r'logscan/(mainnet|testnet)/chunks/[0-9]+-[0-9]+\.json', member.name):
                    target = self.root / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())

    def tearDown(self):
        self._tmp.cleanup()

    def manifest(self, chain):
        plan = self.root / f'logscan/{chain}/plan.json'
        plan.write_bytes((self.directory / chain / 'plan.json').read_bytes())
        p = json.loads(plan.read_text())
        chunks = sorted((self.root / f'logscan/{chain}/chunks').glob('*.json'))
        return {'schema': h.SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'fromBlock': p['start'],
                'blockNumber': p['end'], 'blockHash': p['end_block_hash'], 'inventorySha256': INV_SHA,
                'targets': sorted(a['address'] for a in p['addresses']),
                'segments': [{'planFile': f'{chain}/plan.json', 'planSha256': sha(plan),
                              'chunks': [{'path': str(c.relative_to(self.root / 'logscan')), 'sha256': sha(c)}
                                         for c in chunks]}]}

    def b0(self, chain):
        return {n: json.loads((self.directory / chain / n).read_text())
                for n in ('summary.json', 'holders.json', 'withdrawals.json', 'roles.json', 'owners.json', 'upgrades.json',
                          'plan.json')}

    def observation(self, derived, chain, b0):
        """Observation reconstructed from B0's recorded live cross-check at the pin (B0 PASS:
        live == replay for every touched account); SIMULATION of a collection, not new evidence."""
        reg = REG[chain]
        o = observation_for(derived, chain)
        for token, x in derived['holders'].items():
            o['tokens'][token] = {'totalSupply': b0['holders.json'][token]['totalSupply_at_pin'],
                                  'balanceOf': {a: b0['holders.json'][token]['holders'].get(a, '0') for a in x['balances']}}
        for vault, roles in o['roles'].items():
            for role, accounts in roles.items():
                for account in accounts:
                    accounts[account] = account in b0['roles.json'][vault]['holders_at_pin'].get(role, [])
        o['owners'] = {a: x['owner_at_pin'] for a, x in b0['owners.json'].items()}
        o['implementations'] = {a: x['implementation_at_pin'] for a, x in b0['upgrades.json'].items()}
        o['admins'] = {a: x['admin_at_pin'] for a, x in b0['upgrades.json'].items() if x['admin_at_pin']}
        o['factories'] = {f: [v['vault'] for v in sorted(b0['plan.json']['vaults'], key=lambda v: v['index'])
                              if v['factory'] == f] for f in reg['factories']}
        rel, recs = b0['withdrawals.json']['relation'], b0['withdrawals.json']['records']
        reviewed = hi.reviewed_vault_runtimes(chain)
        o['vaultRuntimeKeccak'] = {v: reviewed[v]['runtimeKeccak'] for vs in derived['factories'].values() for v in vs}
        o['withdrawals'] = {'stpros': reg['stpros'], 'queued': rel['Q_queuedWithdrawal'],
                            'completed': rel['C_completedWithdrawal'], 'reserve': rel['R_totalCanWithdrawAmount'],
                            'owners': {a: recs.get(a, {'head': 0, 'tail': 0, 'records': []}) for a in derived['withdrawOwners']}}
        return o

    def test_b0_prefix_constants_and_rederivation(self):
        for chain in ('testnet', 'mainnet'):
            manifest = self.manifest(chain)
            b0 = self.b0(chain)
            # No extra log target (streamlined scope): the B0 prefix alone closes the target set on both chains.
            verified = h.verify_manifest(manifest, self.root / 'logscan', chain)
            self.assertEqual(verified['b0Prefix']['chunkListSha256'], h.B0_PREFIX[chain]['chunkListSha256'])
            if chain == 'mainnet':
                with with_safe_target(), self.assertRaises(HistoryRefused) as ctx:
                    h.verify_manifest(manifest, self.root / 'logscan', chain)
                self.assertEqual((ctx.exception.code, ctx.exception.status), ('HISTORY_TARGET_MISSING', 'INCOMPLETE'))
            derived = hi.derive(verified, chain)
            self.assertEqual(derived['authorityEventsAfterPin'], [])
            docs, mism = hi.check_observation(derived, self.observation(derived, chain, b0), chain)
            self.assertFalse(any(mism.values()), chain)
            self.assertEqual(docs['summary.json']['beaconIdentityNotVerified'], [], chain)
            stpros = REG[chain]['stpros']
            # Both chains now re-derive the B0 inventory exactly (mainnet no longer waits for a Safe history).
            self.assertEqual(inv.normalize(docs, chain, stpros), inv.normalize(b0, chain, stpros))
            for token, x in b0['holders.json'].items():
                self.assertEqual({a: v for a, v in derived['holders'][token]['balances'].items() if int(v)}, x['holders'])
            self.assertEqual(len(derived['withdrawOwners']), b0['withdrawals.json']['request_owners'])

    def test_real_b0_independent_verification_witnesses(self):
        """IVH-01/02/03/04 witnesses: the real, unmodified B0 prefix with an explicitly synthetic suffix or
        an edited observation field (SIMULATION ONLY; never E-fresh evidence)."""
        chain, stpros = 'testnet', REG['testnet']['stpros']
        manifest, b0, root = self.manifest(chain), self.b0(chain), self.root / 'logscan'
        d = hi.derive(h.verify_manifest(manifest, root, chain), chain)
        o = self.observation(d, chain, b0)
        docs, mism = hi.check_observation(d, o, chain)                       # positive control
        self.assertEqual((docs['summary.json']['overall'], any(mism.values())), ('PASS', False))
        vault, anchor = next(iter(hi.reviewed_vault_runtimes(chain))), manifest['blockNumber'] + 1
        for label, signature, data, topics in (
                ('open-period-reset', 'OpenPeriodExtended(uint256,uint256,uint256)', amount(100) + amount(200)[2:],
                 [amount(1)]),
                ('round-terms-change', 'RoundOpened(uint256,uint256,uint256,uint256)',
                 amount(100) + amount(200)[2:] + amount(999)[2:], [amount(9)])):
            m = copy.deepcopy(manifest)
            row = log(vault, [topic(signature), *topics], anchor, 0, data)
            m['segments'].append(Archive(root, chain).segment(label, m['targets'], anchor, anchor, [row]))
            m['blockNumber'], m['blockHash'] = anchor, block_hash(anchor)
            ds = hi.derive(h.verify_manifest(m, root, chain), chain)
            docs, mism = hi.check_observation(ds, self.observation(ds, chain, b0), chain)
            self.assertFalse(any(mism.values()), label)
            self.assertEqual([k for k, c in docs['summary.json']['checks'].items() if c['status'] != 'PASS'],
                             ['post_pin_configuration_review'], label)
            self.assertEqual(docs['summary.json']['overall'], 'INCOMPLETE')
            with self.assertRaisesRegex(inv.InventoryRefused, 'summary checks not all PASS'):
                inv.normalize(docs, chain, stpros)
        owner = next(a for a, x in o['withdrawals']['owners'].items() if x['records'])
        q, n = int(o['withdrawals']['queued']), len(o['withdrawals']['owners'][owner]['records'])

        def edited(change):
            x = copy.deepcopy(o); change(x['withdrawals']['owners'][owner], x)
            return x
        docs, mism = hi.check_observation(d, edited(lambda e, x: e['records'][0].__setitem__('queued', str(q + 1))), chain)
        self.assertIn('record.queued + record.pending <= Q', [m.get('rule') for m in mism['withdrawals']])
        self.assertEqual(docs['summary.json']['checks']['withdrawal_records']['status'], 'FAIL')
        for change, code in ((lambda e, x: e.update(head=-n, tail=0), 'OBSERVATION_VALUE'),
                             (lambda e, x: e['records'][0].__setitem__('createdAt', None), 'OBSERVATION_VALUE'),
                             (lambda e, x: next(iter(x['roles'].values())).__setitem__('0x' + '99' * 32, {}),
                              'ROLE_ENUMERATION_INCOMPLETE')):
            with self.assertRaises(HistoryRefused) as ctx:
                hi.check_observation(d, edited(change), chain)
            self.assertEqual((ctx.exception.code, ctx.exception.status), (code, 'REFUSED'))
        # A valid partial claim of the head record on the real queue still passes and normalizes.
        record = o['withdrawals']['owners'][owner]['records'][0]
        paid = int(record['pending']) // 2
        self.assertGreater(paid, 0)

        def claim(e, x):
            e['records'][0].update(queued=str(int(record['queued']) + paid), pending=str(int(record['pending']) - paid))
            x['withdrawals']['completed'] = str(int(x['withdrawals']['completed']) + paid)
        docs, mism = hi.check_observation(d, edited(claim), chain)
        self.assertEqual((docs['summary.json']['overall'], any(mism.values())), ('PASS', False))
        inv.normalize(docs, chain, stpros)
        # IVH-04 (Agent C's exact edit): the lowest real baseline copied onto another owner's single record.
        # The unedited real ranges (positive control above) touch end to end and tile [C, Q) exactly.
        ranges = sorted((int(r['queued']), int(r['queued']) + int(r['pending']))
                        for e in o['withdrawals']['owners'].values() for r in e['records'])
        self.assertEqual((len(ranges), ranges[0][0], ranges[-1][1]),
                         (12, int(o['withdrawals']['completed']), int(o['withdrawals']['queued'])))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])))
        x = copy.deepcopy(o)
        records = [(a, r) for a, e in x['withdrawals']['owners'].items() for r in e['records']]
        low_owner, low = min(records, key=lambda ar: int(ar[1]['queued']))
        target = next(a for a, e in x['withdrawals']['owners'].items() if a != low_owner and len(e['records']) == 1)
        self.assertEqual((target, x['withdrawals']['owners'][target]['records'][0]['queued'], low['queued']),
                         ('0x4a3e99063fd6a90d9de955717fd79ed3371a2be3', '22182598764222222218', '6000000000000000000'))
        x['withdrawals']['owners'][target]['records'][0]['queued'] = low['queued']
        docs, mism = hi.check_observation(d, x, chain)
        # The moved range [6e18, 8.02e18) covers the low owner's [6e18, 8e18) and reaches into its [8e18, 10e18).
        self.assertEqual([(m['owner'], m['index'], m['overlapsOwner'], m['overlapsIndex']) for m in mism['withdrawals']],
                         [(target, 0, low_owner, 4), (low_owner, 5, target, 0)])
        self.assertEqual({m['rule'] for m in mism['withdrawals']},
                         {'outstanding [queued, queued + pending) disjoint across all owners'})
        self.assertFalse(any(v for k, v in mism.items() if k != 'withdrawals'))
        self.assertEqual(docs['summary.json']['checks']['withdrawal_records']['status'], 'FAIL')
        with self.assertRaises(inv.InventoryRefused):
            inv.normalize(docs, chain, stpros)

    def test_modified_real_chunk_refused(self):
        manifest = self.manifest('testnet')
        ref = manifest['segments'][0]['chunks'][5]
        path = self.root / 'logscan' / ref['path']
        doc = json.loads(path.read_text()); doc['fetched_utc'] = '2026-10-13T00:00:00Z'
        ref['sha256'] = write(path, doc)    # rehashed: content integrity holds, B0 identity does not
        with self.assertRaises(HistoryRefused) as ctx:
            h.verify_manifest(manifest, self.root / 'logscan', 'testnet')
        self.assertEqual(ctx.exception.code, 'B0_PREFIX_MODIFIED')


if __name__ == '__main__':
    unittest.main()
