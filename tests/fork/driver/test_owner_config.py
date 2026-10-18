"""Tests of the owner-confirmed configuration gate (``owner_config``). Test-only, offline.

The archived-evidence test runs only when FAROO_OWNER_CONFIG_RUNS names a history root that holds the archived
``s1-testnet-r1`` / ``s2-testnet-r1`` / ``s1-mainnet-r1`` run roots with their staged B0 prefixes; otherwise it is
NOT RUN.
"""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import fr_d1_scope as scope  # noqa: E402
import history as h  # noqa: E402
import owner_config as oc  # noqa: E402

ANCHOR = dict(oc.TESTNET_ANCHOR)
ENTRIES = {e['id']: e for e in oc.CHANGES}


def word(address):
    return '0x' + address[2:].rjust(64, '0')


def row(entry, *, block=None, log_index=None, tx=None, block_hash=None, old=None, new=None):
    e = entry['event']
    return {'address': entry['contract'], 'blockNumber': hex(block or e['block']), 'blockHash': block_hash or e['blockHash'],
            'logIndex': hex(e['logIndex'] if log_index is None else log_index),
            'transactionHash': tx or e['transactionHash'], 'data': '0x',
            'topics': [e['topic0'], word(old or entry['old']), word(new or entry['new'])]}


def history():
    return [row(e) for e in oc.CHANGES]


def observed(overrides=None):
    values = {(e['section'], e['field']): e['new'] for e in oc.CHANGES}
    values.update(overrides or {})
    return values


class GateTest(unittest.TestCase):
    def refused(self, fn, code, status='FAIL'):
        with self.assertRaises(oc.OwnerConfigRefused) as ctx:
            fn()
        self.assertEqual((ctx.exception.code, ctx.exception.status), (code, status), str(ctx.exception))

    def test_topics_are_the_recorded_event_signatures(self):
        self.assertEqual(oc.SLP_CHANGED, '0x1b00dbfad7f4f72226b1de1cda6529fe1166b91b4a2bf3c9e65a876fb06bafaa')
        self.assertEqual(oc.KEEPER_CHANGED, '0x068b48a2fe7f498b57ff6da64f075ae658fde8d77124b092e62b3dc58d91ce35')
        self.assertEqual({e['standing'] for e in oc.CHANGES} <= set(oc.STANDINGS), True)
        self.assertEqual([e['standing'] for e in oc.CHANGES], ['OWNER_CONFIRMED', 'UPGRADE_TARGET', 'OWNER_DECISION_PENDING'])

    def test_confirmed_reporter_is_the_expectation_only_when_all_three_agree(self):
        results = oc.check('testnet', ANCHOR, history(), observed())
        self.assertEqual([r['result'] for r in results], ['MATCH'] * 3)
        self.assertEqual(oc.expected('testnet-oracle-reporter', 'testnet', ANCHOR, history(), observed()),
                         '0xe45493245c4bc3a573d35b191d8674f861e1ef45')

    def test_old_value_and_unknown_new_value_are_drift(self):
        for value in ('0x464017cdc3c2af2b5b525fde03ac93f15172db43', '0x' + '77' * 20, None):
            with self.subTest(value):
                obs = observed({('oracle', 'slp'): value})
                self.refused(lambda: oc.expected('testnet-oracle-reporter', 'testnet', ANCHOR, history(), obs), 'DRIFT')

    def test_wrong_chain_and_wrong_anchor_are_refused(self):
        self.assertEqual(oc.check('mainnet', ANCHOR, history(), observed()), [])
        self.refused(lambda: oc.expected('testnet-oracle-reporter', 'mainnet', ANCHOR, history(), observed()),
                     'CHAIN_MISMATCH')
        for anchor in ({**ANCHOR, 'block': ANCHOR['block'] + 1}, {**ANCHOR, 'hash': '0x' + '11' * 32}, {}):
            with self.subTest(anchor):
                self.refused(lambda: oc.expected('testnet-oracle-reporter', 'testnet', anchor, history(), observed()),
                             'ANCHOR_MISMATCH')

    def test_a_later_change_or_another_event_is_never_accepted(self):
        entry = ENTRIES['testnet-oracle-reporter']
        # changed away and back after the confirmed event: the getter matches, the history does not
        away = row(entry, block=entry['event']['block'] + 10, tx='0x' + '21' * 32, block_hash='0x' + '31' * 32,
                   old=entry['new'], new='0x' + '77' * 20)
        back = row(entry, block=entry['event']['block'] + 20, tx='0x' + '22' * 32, block_hash='0x' + '32' * 32,
                   old='0x' + '77' * 20, new=entry['new'])
        self.refused(lambda: oc.expected(entry['id'], 'testnet', ANCHOR, history() + [away, back], observed()),
                     'LATER_CHANGE')
        for name, edit in (('tx', {'tx': '0x' + '23' * 32}), ('block hash', {'block_hash': '0x' + '33' * 32}),
                           ('old value', {'old': '0x' + '44' * 20}), ('log index', {'log_index': 1})):
            with self.subTest(name):
                rows = [r for r in history() if r['address'] != entry['contract']] + [row(entry, **edit)]
                self.refused(lambda: oc.expected(entry['id'], 'testnet', ANCHOR, rows, observed()), 'EVENT_MISMATCH')
        rows = [r for r in history() if r['address'] != entry['contract']]
        self.refused(lambda: oc.expected(entry['id'], 'testnet', ANCHOR, rows, observed()), 'EVENT_MISMATCH')
        # an event after the anchor is outside the observation and cannot match it
        late = row(entry, block=ANCHOR['block'] + 1)
        rows = [r for r in history() if r['address'] != entry['contract']] + [late]
        self.refused(lambda: oc.expected(entry['id'], 'testnet', ANCHOR, rows, observed()), 'EVENT_MISMATCH')

    def test_pending_keeper_is_blocked_and_the_sink_is_the_upgrade_target(self):
        self.refused(lambda: oc.expected('testnet-distributor-keeper', 'testnet', ANCHOR, history(), observed()),
                     'OWNER_DECISION_PENDING', 'BLOCKED')
        sink = oc.expected('testnet-stpros-sink', 'testnet', ANCHOR, history(), observed())
        distributor = next(a for a, x in h.registry('testnet')['expected_proxies'].items() if x['name'] == 'SlpDistributor')
        self.assertEqual((sink, ENTRIES['testnet-distributor-keeper']['contract']), (distributor, distributor))
        self.refused(lambda: oc.expected('unknown', 'testnet', ANCHOR, history(), observed()), 'NOT_RECORDED')

    def test_dispositions_annotate_without_changing_the_report(self):
        report = {'sections': {'oracle': {'issues': [{'rule': 'REPORTER_REGISTRY', 'severity': 'FAIL', 'detail': 'd'},
                                                     {'rule': 'OTHER', 'severity': 'INCOMPLETE', 'detail': 'x'}]}}}
        before = copy.deepcopy(report)
        resolved = oc.dispositions(report, oc.check('testnet', ANCHOR, history(), observed()))
        self.assertEqual(report, before)
        self.assertEqual([(d['originalSeverity'], d['disposition']) for d in resolved],
                         [('FAIL', 'OWNER-CONFIRMED CONFIGURATION CHANGE')])
        drift = oc.check('testnet', ANCHOR, history(), observed({('oracle', 'slp'): '0x' + '77' * 20}))
        self.assertEqual(oc.dispositions(report, drift)[0]['disposition'], 'UNRESOLVED')

    def test_outside_every_source_binding(self):
        self.assertNotIn('tests/fork/driver/owner_config.py', scope.SOURCE_BINDING_FILES)

    def test_archived_evidence(self):
        root = os.environ.get('FAROO_OWNER_CONFIG_RUNS')
        if not root:
            self.skipTest('NOT RUN: set FAROO_OWNER_CONFIG_RUNS')
        import collector as col
        for chain, run_id in (('testnet', 's1-testnet-r1'), ('testnet', 's2-testnet-r1'), ('mainnet', 's1-mainnet-r1')):
            base = Path(root) / run_id
            spec = json.loads((base / run_id / 'spec.json').read_bytes())
            _, verified, _ = col.content_verify(base, run_id, spec)
            state = json.loads((base / run_id / 'state-observation.json').read_bytes())
            anchor = {'block': state['block'], 'hash': state['blockHash']}
            results = oc.check(chain, anchor, verified['rows'], oc.observed_values(state))
            if chain == 'mainnet':
                self.assertEqual(results, [])
            else:
                self.assertEqual([r['result'] for r in results], ['MATCH'] * 3, run_id)


if __name__ == '__main__':
    unittest.main()
