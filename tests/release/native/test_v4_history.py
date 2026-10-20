"""V4 mainnet release history: targets created after the archive start, and the identity-only asset (offline).

``authenticate_history`` / ``HistorySession.create`` are the production functions; the plan and chunk files are crafted
in a temporary directory. A target listed in ``createdTargets`` (V4 mainnet scope only) may be covered from its
``coverageStart`` when CREATE(deployer, nonce) is that address, its code is absent at ``coverageStart - 1`` and the
deployer is an EOA at the reviewed block. Every other target keeps the frozen rule: covered from ``fromBlock``.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
from test_v4_delay_policy import approved_governance, corpus  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import history  # noqa: E402
from tools.release.native.distributor_predeploy import create_address  # noqa: E402

FROM, PIN, ANCHOR = 100, 5_000, 9_000
DEPLOYER = '0x' + '8c' * 20
ANCHOR_HASH, PIN_HASH = '0x' + 'a1' * 32, '0x' + 'b2' * 32
CREATED = {'timelock': 40, 'distributor': 41, 'bridge': 42, 'assertions': 43}   # name -> deployer nonce


class History:
    """One crafted archive: segment A [FROM, PIN] over the legacy targets, segment B [PIN + 1, ANCHOR] over all."""

    def __init__(self, directory, legacy, created, *, b_start=PIN + 1, logs=()):
        self.dir = Path(directory)
        self.segments = [self.segment('a', FROM, PIN, legacy, PIN_HASH, []),
                         self.segment('b', b_start, ANCHOR, legacy + created, ANCHOR_HASH, list(logs))]

    def write(self, name, doc):
        path = self.dir / f'{name}.json'
        path.write_text(json.dumps(doc))
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def segment(self, name, start, end, addresses, end_hash, logs):
        addresses = sorted(addresses)
        plan = {'chain': 'mainnet', 'chain_id': 1672, 'window': 1000, 'start': start, 'end': end,
                'addresses': [{'address': a} for a in addresses], 'end_block_hash': end_hash,
                'filter_id': j.historical_filter_id(addresses)}
        windows, w = [], start
        while w <= end:
            e = min(w + 999, end)
            windows.append([w, e, sum(w <= int(r['blockNumber'], 16) <= e for r in logs)])
            w = e + 1
        chunk = {'chain': 'mainnet', 'filter_id': plan['filter_id'], 'from': start, 'to': end, 'windows': windows,
                 'logs': logs}
        p = self.write(f'plan-{name}', plan)
        return {'planFile': p['path'], 'planSha256': p['sha256'], 'chunks': [self.write(f'chunk-{name}', chunk)]}


def log(address, block, index=0):
    return {'address': address, 'blockNumber': hex(block), 'blockHash': '0x' + format(block, '064x'),
            'transactionHash': '0x' + format(block * 10 + index, '064x'), 'logIndex': hex(index),
            'topics': ['0x' + 'cd' * 32], 'data': '0x', 'removed': False}


class V4HistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, declared = corpus()
        release = next(r for r in rows if r['id'] in declared and r['function'] == 'core.mjs.preflight'
                       and r['outcome'] == 'return')
        cls.base = gate.revive(release['arguments'])[0]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT / 'cache') if (ROOT / 'cache').is_dir() else None)
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, v4=True):
        c = copy.deepcopy(self.base)
        if v4:
            c['policy'] = {**c['policy'], 'timelockDelayPolicy': 'V4-CONFIG-600', 'minDelay': 600}
            approved_governance(c)
        for name, nonce in CREATED.items():
            c['addresses'][name] = create_address(DEPLOYER, nonce)
        c['observed'] = {**c['observed'], 'blockNumber': ANCHOR, 'blockHash': ANCHOR_HASH}
        return c

    def proof(self, c, *, created=None, logs=(), b_start=PIN + 1, legacy=None, targets=None):
        targets = history.history_targets(c) if targets is None else targets
        new = [c['addresses'][n] for n in CREATED]
        legacy = [t for t in targets if t not in new] if legacy is None else legacy
        archive = History(self.dir, legacy, [t for t in new if t in targets], b_start=b_start, logs=logs)
        doc = {'schema': 'faroo-history-v1', 'chainId': 1672, 'blockNumber': ANCHOR, 'blockHash': ANCHOR_HASH,
               'fromBlock': FROM, 'inventorySha256': 'f' * 64, 'targets': targets, 'segments': archive.segments}
        if created is not False:
            doc['createdTargets'] = created if created is not None else [
                {'address': c['addresses'][n], 'deployer': DEPLOYER, 'nonce': nonce, 'coverageStart': PIN + 1}
                for n, nonce in CREATED.items()]
        ref = archive.write('proof', doc)
        c['historyFile'] = ref['path']
        c['review'] = {**c['review'], 'historySha256': ref['sha256'], 'inventorySha256': 'f' * 64}
        return {'fromBlock': FROM}

    def refused(self, code, fn, *args):
        with self.assertRaises(j.ReleaseError) as raised:
            fn(*args)
        self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    def session(self, c, inventory, code_at=None):
        reads = []

        async def rpc(method, params):
            reads.append((method, params))
            if method == 'eth_getBlockByNumber':
                n = int(params[0], 16)
                return {'number': params[0], 'hash': {PIN: PIN_HASH, ANCHOR: ANCHOR_HASH}[n]}
            if method == 'eth_getCode':
                return (code_at or {}).get((params[0], int(params[1], 16)), '0x')
            raise AssertionError(method)
        return asyncio.run(history.HistorySession.create(c, inventory, rpc)), reads

    # ------------------------------------------------------------------------------------------------ targets
    def test_asset_is_identity_only_in_scope(self):
        c = self.config()
        self.assertNotIn(c['addresses']['asset'], history.history_targets(c))
        self.assertIn(c['addresses']['timelock'], history.history_targets(c))
        frozen = self.config(v4=False)
        self.assertIn(frozen['addresses']['asset'], history.history_targets(frozen))   # frozen R4 rule unchanged

    # ------------------------------------------------------------------------------------------------ created
    def test_created_targets_are_covered_from_their_creation_window(self):
        c = self.config()
        new = [c['addresses'][n] for n in CREATED]
        inv = self.proof(c, logs=[log(new[0], PIN + 7), log(c['addresses']['stpros'], ANCHOR - 1, 1)])
        result = history.authenticate_history(c, inv)
        self.assertEqual(sorted(result['created']), sorted(new))
        session, reads = self.session(c, inv)
        codes = sorted((p[0], int(p[1], 16)) for m, p in reads if m == 'eth_getCode')
        legacy = [t for t in session.targets if t not in new]
        self.assertEqual(codes, sorted([(t, FROM - 1) for t in legacy] + [(t, PIN) for t in new]
                                       + [(DEPLOYER, ANCHOR)] * len(new)))
        self.assertEqual(len(session.history_logs(new[0], FROM, ANCHOR, ['0x' + 'cd' * 32])), 1)

    def test_created_target_refusals(self):
        c = self.config(); t = c['addresses']['timelock']
        entry = lambda **kw: [{'address': t, 'deployer': DEPLOYER, 'nonce': 40, 'coverageStart': PIN + 1, **kw}]
        for created, why in ((entry(nonce=39), 'not CREATE(deployer, nonce)'),
                             (entry(deployer='0x' + '8d' * 20), 'another deployer'),
                             (entry(coverageStart=FROM), 'starts at fromBlock'),
                             (entry(coverageStart=ANCHOR + 1), 'after the anchor'),
                             (entry() + entry(), 'duplicate'),
                             ([{'address': c['addresses']['oracle'], 'deployer': DEPLOYER, 'nonce': 1,
                                'coverageStart': PIN + 1}], 'a legacy target'),
                             ([], 'empty list')):
            with self.subTest(why=why):
                c = self.config()
                inv = self.proof(c, created=created)
                self.refused('HISTORY_CREATED_TARGETS', history.authenticate_history, c, inv)

    def test_without_a_creation_entry_the_frozen_rule_applies(self):
        c = self.config()
        inv = self.proof(c, created=False)              # new targets covered only from PIN + 1
        self.refused('HISTORY_TARGET_GAP_OR_OVERLAP', history.authenticate_history, c, inv)
        c = self.config()
        t = c['addresses']['timelock']
        inv = self.proof(c, created=[{'address': t, 'deployer': DEPLOYER, 'nonce': 40, 'coverageStart': PIN + 1}])
        self.refused('HISTORY_TARGET_GAP_OR_OVERLAP', history.authenticate_history, c, inv)   # the other three

    def test_coverage_must_start_exactly_at_the_declared_start(self):
        c = self.config()
        inv = self.proof(c, created=[{'address': c['addresses'][n], 'deployer': DEPLOYER, 'nonce': nonce,
                                      'coverageStart': PIN} for n, nonce in CREATED.items()])
        self.refused('HISTORY_TARGET_GAP_OR_OVERLAP', history.authenticate_history, c, inv)

    def test_code_before_start_or_a_contract_deployer_is_refused(self):
        c = self.config(); t = c['addresses']['bridge']
        inv = self.proof(c)
        self.refused('INVENTORY_START_HAS_CODE', self.session, c, inv, {(t, PIN): '0x60'})
        c = self.config(); inv = self.proof(c)
        self.refused('HISTORY_CREATED_TARGETS', self.session, c, inv, {(DEPLOYER, ANCHOR): '0xef0100' + '11' * 20})

    def test_created_targets_are_refused_outside_the_v4_scope(self):
        c = self.config(v4=False)
        inv = self.proof(c)
        self.refused('HISTORY_CREATED_TARGETS', history.authenticate_history, c, inv)


if __name__ == '__main__':
    unittest.main()
