"""Offline unit tests of the Fork-campaign driver, schemas, catalog and receipt-frame oracle.

Run inside an approved export:
  tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/fork/driver -p 'test_*.py' -v

Tests that need export-local artifacts (canonical build, fixture outputs, an explicitly supplied
B0 inventory) skip with a reason when the artifact is absent; a skip is NOT RUN, never PASS.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import anvil  # noqa: E402
import campaign_input as ci  # noqa: E402
import inventory as inv  # noqa: E402
import oracles  # noqa: E402
import safe_route  # noqa: E402
from safe_route import ReplayValidationError  # noqa: E402
import scenarios  # noqa: E402
from tools.lib import abi  # noqa: E402

PINS = {'mainnet': 19774476, 'testnet': 31190661}
Z = ci.ZERO_HASH
ZA = ci.ZERO_ADDRESS
H1 = '0x' + '11' * 32
H2 = '0x' + '22' * 32


def actors(chain='mainnet'):
    out = {}
    for i, role in enumerate(ci.REQUIRED_ACTORS[chain]):
        real = ci.REAL_IDENTITIES[chain].get(role)
        out[role] = {'address': real or '0x' + format(0xc0 + i, '040x'),
                     'kind': 'REAL' if real else 'LABELLED_SYNTHETIC', 'label': role}
    return out


def base_input(cls='SYNTHETIC', block=0, block_hash=Z, anchor=0, complete=False, history=Z):
    return {
        'schema': ci.SCHEMA, 'campaign': 'M', 'chain': 'mainnet', 'chainId': 1672, 'evidenceClass': cls,
        'fork': {'block': block, 'blockHash': block_hash},
        'source': {'productionHead': '62c7b5ad4cebd81ca7e510358cf18aa5771a923d', 'exportManifestSha256': H1,
                   'r4ReferenceSha256': H1, 'nativeR4ManifestSha256': H1},
        'inventory': {'path': 'inputs/inventory.json', 'sha256': H2, 'anchorBlock': anchor, 'complete': complete,
                      'historyManifestSha256': history},
        'actors': actors(),
        'safe': {'mode': 'ABSENT', 'identityManifestSha256': Z},
        'decisions': {k: 'UNDECIDED' for k in ci.DECISIONS},
    }


class CampaignInputTest(unittest.TestCase):
    def refused(self, doc, fragment):
        with self.assertRaises(ci.InputRefused) as ctx:
            ci.validate(doc, PINS)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_synthetic_reports_blockers_explicitly(self):
        blockers = ci.validate(base_input(), PINS)
        kinds = {(k, m.split(':')[0]) for k, m in blockers}
        self.assertIn(('BLOCKED', 'Safe path'), kinds)
        self.assertIn(('BLOCKED', 'FR-D2 undecided'), {(k, m) for k, m in blockers})
        self.assertIn(('BLOCKED', 'FR-D3 undecided'), {(k, m) for k, m in blockers})
        self.assertIn(('INCOMPLETE', 'evidence class SYNTHETIC cannot close an FS row'), {(k, m) for k, m in blockers})
        self.assertTrue(any(k == 'INCOMPLETE' and 'labelled synthetic actor' in m for k, m in blockers))

    def test_valid_e_hist_and_e_fresh(self):
        ci.validate(base_input('E_HIST', PINS['mainnet'], H1, PINS['mainnet'], True), PINS)
        blockers = ci.validate(base_input('E_FRESH', PINS['mainnet'] + 5, H1, PINS['mainnet'] + 5, True, H2), PINS)
        self.assertNotIn(('INCOMPLETE', 'evidence class E_FRESH cannot close an FS row'), blockers)

    def test_schema_violations_refused(self):
        doc = base_input(); doc['extra'] = 1
        self.refused(doc, 'unexpected key set at $')
        doc = base_input(); del doc['decisions']
        self.refused(doc, 'unexpected key set at $')
        doc = base_input(); doc['chainId'] = 688689
        self.refused(doc, 'chain, chainId and campaign disagree')
        doc = base_input(); doc['fork']['block'] = True
        self.refused(doc, '.fork.block must be a non-negative integer')
        doc = base_input(); doc['source']['r4ReferenceSha256'] = '0x' + 'AB' * 32
        self.refused(doc, 'lowercase 32-byte hex')
        doc = base_input(); doc['inventory']['path'] = '../../research/inventory.json'
        self.refused(doc, 'relative to the rehearsal cache')
        doc = base_input(); doc['decisions']['FR-D2'] = 'd'
        self.refused(doc, '.decisions.FR-D2')
        doc = base_input(); del doc['actors']['KEEPER']
        self.refused(doc, 'actor mapping must be exactly')
        doc = base_input(); doc['actors']['CAN']['address'] = doc['actors']['GRD']['address']
        self.refused(doc, 'actors must be distinct')
        doc = base_input(); doc['actors']['E1']['address'] = '0x' + '45' * 20
        self.refused(doc, 'REAL E1 differs from the reviewed identity')
        doc = base_input(); doc['actors']['CAN']['label'] = 'bad"label'
        self.refused(doc, '.actors.CAN.label')

    def test_missing_evidence_refused(self):
        self.refused(base_input('SYNTHETIC', 5, H1), 'SYNTHETIC input must not claim a fork block')
        self.refused(base_input('E_HIST', PINS['mainnet'] + 1, H1, PINS['mainnet'] + 1, True), 'E-hist must fork at the B0 pin')
        self.refused(base_input('E_HIST', PINS['mainnet'], Z, PINS['mainnet'], True), 'fork block hash missing')
        self.refused(base_input('E_HIST', PINS['mainnet'], H1, PINS['mainnet'], False), 'INCOMPLETE: inventory history')
        self.refused(base_input('E_FRESH', PINS['mainnet'] + 5, H1, PINS['mainnet'] + 5, True, Z), 'authenticated history manifest')
        self.refused(base_input('E_FRESH', PINS['mainnet'] + 5, H1, PINS['mainnet'], True, H2), 'inventory anchor differs')
        doc = base_input('E_HIST', PINS['mainnet'], H1, PINS['mainnet'], True)
        doc['actors']['E2']['kind'] = 'LABELLED_SYNTHETIC'
        self.refused(doc, 'E2 must be the real identity in Fork evidence')

    def test_safe_identity_rules(self):
        # Review finding 1: no caller-supplied Safe identity, flag or review reference is trusted.
        self.assertEqual(ci.APPROVED_SAFE_IDENTITY_MANIFESTS, frozenset(), 'no approved manifest exists')
        doc = base_input('E_HIST', PINS['mainnet'], H1, PINS['mainnet'], True)
        doc['safe'] = {'mode': 'APPROVED_MANIFEST', 'identityManifestSha256': H1}
        self.refused(doc, 'BLOCKED: Safe identity manifest is not independently approved')
        doc['safe'] = {'mode': 'ABSENT', 'identityManifestSha256': H1}
        self.refused(doc, 'Safe identity manifest given without APPROVED_MANIFEST mode')
        doc['safe'] = {'mode': 'SYNTHETIC', 'identityManifestSha256': Z}
        self.refused(doc, 'a synthetic Safe is only valid for SYNTHETIC evidence')
        # The retired self-attesting shape is refused outright.
        doc['safe'] = {'proxy': '0x' + 'aa' * 20, 'proxyRuntimeKeccak': H1, 'singleton': '0x' + 'bb' * 20,
                       'singletonRuntimeKeccak': H1, 'multiSendCallOnly': '0x' + 'cc' * 20,
                       'multiSendCallOnlyRuntimeKeccak': H1, 'reviewRef': 'approved-by-agent-A', 'synthetic': False}
        self.refused(doc, 'unexpected key set at .safe')
        blockers = ci.validate(base_input(), PINS)
        self.assertIn(('BLOCKED', 'Safe path: no independently approved Safe identity manifest (FR-X8)'), blockers)
        doc = base_input()
        doc['safe'] = {'mode': 'SYNTHETIC', 'identityManifestSha256': Z}
        self.assertIn(('INCOMPLETE', 'Safe path uses synthetic models, not real Safe bytecode'), ci.validate(doc, PINS))

    def test_e_fresh_history_and_final_gate_stay_fail_closed(self):
        # Review finding 2: a declared (invented) history hash with complete=true is not provenance.
        blockers = ci.validate(base_input('E_FRESH', PINS['mainnet'] + 5, H1, PINS['mainnet'] + 5, True, '0x' + 'de' * 32), PINS)
        self.assertIn(('INCOMPLETE', 'authenticated history not verified: the native history verifier did not run'), blockers)
        self.assertIn(('INCOMPLETE', 'final FS acceptance gate not implemented (fail-closed)'), blockers)

    def test_bind_to_export_checks_every_identity(self):
        native = ROOT / 'cache/native-r4/manifest.json'
        if not (ROOT / 'EXPORT-MANIFEST.txt').is_file() or not native.is_file():
            self.skipTest('NOT RUN: needs an export with tools/ci/canonical.py output')
        out = ROOT / ci.INPUT_ROOT / 'inputs'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'inventory.json').write_text('{"schema":"faroo-fork-inventory-v1"}\n')
        doc = base_input()
        manifest = json.loads((ROOT / 'EXPORT-MANIFEST.txt').read_text())
        doc['source'] = {'productionHead': manifest['sourceHead'],
                         'exportManifestSha256': ci.sha256_file(ROOT / 'EXPORT-MANIFEST.txt'),
                         'r4ReferenceSha256': ci.sha256_file(ROOT / 'tools/ci/r4-reference.json'),
                         'nativeR4ManifestSha256': ci.sha256_file(native)}
        doc['inventory']['sha256'] = ci.sha256_file(out / 'inventory.json')
        path = out / 'input.json'
        path.write_text(json.dumps(doc))
        loaded, sha, _ = ci.load(path, ROOT)
        self.assertEqual(sha, ci.sha256_file(path))
        for key in ('exportManifestSha256', 'r4ReferenceSha256', 'nativeR4ManifestSha256'):
            bad = copy.deepcopy(doc)
            bad['source'][key] = H1
            path.write_text(json.dumps(bad))
            with self.assertRaises(ci.InputRefused):
                ci.load(path, ROOT)
        bad = copy.deepcopy(doc); bad['inventory']['sha256'] = H1
        path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ci.InputRefused, 'inventory SHA-256 mismatch'):
            ci.load(path, ROOT)
        # E-fresh with an invented history hash and no history file is refused at the export.
        fresh = copy.deepcopy(doc)
        fresh.update(evidenceClass='E_FRESH', fork={'block': PINS['mainnet'] + 5, 'blockHash': H1})
        fresh['inventory'].update(anchorBlock=PINS['mainnet'] + 5, complete=True, historyManifestSha256='0x' + 'de' * 32)
        fresh['actors']['GOV']['kind'] = 'REAL'
        path.write_text(json.dumps(fresh))
        with self.assertRaisesRegex(ci.InputRefused, 'history manifest file missing'):
            ci.load(path, ROOT)


# --------------------------------------------------------------------------- inventory

def b0_docs():
    owner, holder2 = '0x' + 'a1' * 20, '0x' + 'a2' * 20
    st = '0x6b0a44c64190279f7034b77c13a566e914fe5ec4'
    return {
        'summary.json': {'chain_id': 1672, 'coverage': 'COMPLETE', 'coverage_problems': [], 'pinned_block': 100,
                         'pinned_block_hash': H1, 'checks': {'a': {'status': 'PASS'}}},
        'holders.json': {st: {'status': 'PASS', 'mismatch_count': 0, 'holder_count': 2,
                              'holders': {owner: '20', holder2: '10'}, 'totalSupply_at_pin': '30'}},
        'withdrawals.json': {'status': 'PASS', 'mismatches': [], 'stpros': st, 'owners_with_outstanding_records': 1,
                             'relation': {'Q_queuedWithdrawal': '15', 'C_completedWithdrawal': '5',
                                          'R_totalCanWithdrawAmount': '10', 'sum_pending': '10'},
                             'records': {owner: {'head': 0, 'tail': 2, 'records': [
                                 {'queued': '0', 'pending': '5', 'createdAt': '1', 'unbondingPeriod': '604800'},
                                 {'queued': '5', 'pending': '5', 'createdAt': '2', 'unbondingPeriod': '604800'}]}}},
        'roles.json': {'0x' + 'b1' * 20: {'mismatches': [], 'holders_at_pin': {'0x' + '00' * 32: [owner]}}},
        'owners.json': {st: {'status': 'PASS', 'mismatches': [], 'owner_at_pin': owner}},
    }


class InventoryTest(unittest.TestCase):
    ST = '0x6b0a44c64190279f7034b77c13a566e914fe5ec4'

    def test_normalize_recomputes_relations(self):
        doc = inv.normalize(b0_docs(), 'mainnet', self.ST)
        self.assertEqual(doc['stpros']['recordPending'], ['5', '5'])
        self.assertEqual(doc['stpros']['recordReceivers'], [ZA, ZA])
        self.assertEqual(doc['anchorBlock'], 100)
        self.assertEqual(len(doc['roles']), 1)

    def test_inconsistent_inventory_refused(self):
        cases = [
            (lambda d: d['withdrawals.json']['records']['0x' + 'a1' * 20]['records'][1].update(pending='4'), 'Σ pending'),
            (lambda d: d['holders.json'][self.ST]['holders'].update({'0x' + 'a2' * 20: '11'}), 'Σ holder balances'),
            (lambda d: d['summary.json'].update(coverage='PARTIAL'), 'INCOMPLETE: log coverage'),
            (lambda d: d['withdrawals.json']['relation'].update(R_totalCanWithdrawAmount='9'), 'mainnet R != Q'),
            (lambda d: d['withdrawals.json'].update(stpros='0x' + 'ee' * 20), 'another StPROS'),
        ]
        for mutate, fragment in cases:
            docs = b0_docs()
            mutate(docs)
            with self.assertRaises(inv.InventoryRefused) as ctx:
                inv.normalize(docs, 'mainnet', self.ST)
            self.assertIn(fragment, str(ctx.exception))

    def test_unauthenticated_files_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'cache_forge') if (ROOT / 'cache_forge').is_dir() else tempfile.TemporaryDirectory() as tmp:
            expected = {}
            for name, doc in b0_docs().items():
                data = json.dumps(doc).encode()
                (Path(tmp) / name).write_bytes(data)
                expected[name] = hashlib.sha256(data).hexdigest()
            docs = inv.read_authenticated(tmp, expected)
            self.assertEqual(inv.normalize(docs, 'mainnet', self.ST)['chainId'], 1672)
            tampered = dict(expected, **{'holders.json': '0' * 64})
            with self.assertRaisesRegex(inv.InventoryRefused, 'SHA-256 mismatch'):
                inv.read_authenticated(tmp, tampered)
            with self.assertRaisesRegex(inv.InventoryRefused, 'cover exactly'):
                inv.read_authenticated(tmp, {k: v for k, v in expected.items() if k != 'roles.json'})

    def test_real_b0_inventory_when_supplied(self):
        directory = os.environ.get('FAROO_B0_INVENTORY_DIR')
        if not directory:
            self.skipTest('NOT RUN: FAROO_B0_INVENTORY_DIR not supplied (historical inventory is an explicit input)')
        expected = json.loads(Path(os.environ['FAROO_B0_INVENTORY_EXPECT']).read_text())
        for chain, st in (('mainnet', '0x6b0a44c64190279f7034b77c13a566e914fe5ec4'),
                          ('testnet', '0xb1437aeea18189eb6d02dc46cd4d28613d582e9a')):
            doc = inv.normalize(inv.read_authenticated(Path(directory) / chain, expected[chain]), chain, st)
            s = doc['stpros']
            pending = sum(int(x) for x in s['recordPending'])
            self.assertEqual(pending, int(s['queued']) - int(s['completed']))
            self.assertEqual(doc['anchorBlock'], PINS[chain])
            if chain == 'mainnet':
                self.assertEqual(pending, 673309794035648617669)
                self.assertEqual(len(set(s['recordOwners'])), 13)
                self.assertEqual(len(s['holders']), 42)
            else:
                self.assertEqual(pending, 1505098291851011388640)
                self.assertEqual(s['reserve'], '0', 'testnet V3 design: R = 0, accepted state')


# --------------------------------------------------------------------------- catalog and cases

class ScenarioTest(unittest.TestCase):
    def test_catalog_lists_every_fs_row_and_nothing_runs(self):
        ids = scenarios.catalog_ids()
        self.assertEqual(len(ids), len(set(ids)))
        for required in ['FS-1.A', 'FS-1.B', 'FS-1.C', 'FS-1.D', 'FS-1.ALT-A',
                         *[f'FS-1a.{v}' for v in ('i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii')],
                         'FS-2.normal', *[f'FS-2.{r}' for r in 'abcdefg'],
                         *[f'FS-3{c}.{n}' for c in 'mt' for n in range(1, 9)],
                         *[f'FS-4{c}.{n}' for c in 'mt' for n in ('R0', 'R1', 'R2', 'R3', 'R4', 'failure')],
                         'FS-5m.pre', 'FS-5m.uFrame', 'FS-5m.post', 'FS-5m.permutations',
                         'FS-5t.relations', 'FS-5t.partialClaims', 'FS-5t.vaults',
                         *[f'FS-6m.{n}' for n in ('P-1', 'H.A', 'H.B', 'H.C', 'H.D', 'U.A', 'U.B', 'U.C', 'U.D', 'ALT-A', 'P-0')],
                         *[f'FS-6t.{n}' for n in ('T-5', 'U_t.normal', 'U_t.b', 'U_t.c', 'U_t.d')],
                         *[f'FS-7.{c}' for c in 'ABCD']]:
            self.assertIn(required, ids)
        status = scenarios.report()
        self.assertEqual(status['counts']['NOT RUN'], len(ids))
        self.assertEqual(status['counts']['PASS'], 0)
        self.assertTrue(all(not s['implemented'] for s in scenarios.CATALOG))

    def test_ledger_schema_rejects_inconsistent_results(self):
        good = {'schema': 'faroo-fork-ledger-v1', 'scenario': 's', 'evidenceClass': 'SYNTHETIC (class C)',
                'aggregate': 'PASS', 'verdict': 'INCOMPLETE', 'verdictReason': 'SYNTHETIC (class C) evidence cannot close an FS row',
                'checks': [{'id': 'a', 'verdict': 'PASS', 'expected': '1', 'actual': '1', 'detail': ''}], 'gas': []}
        scenarios.require_ledger(good)
        for mutate in (lambda d: d['checks'][0].update(actual='2'),
                       lambda d: d.update(aggregate='FAIL'),
                       lambda d: d.update(verdict='PASS'),
                       lambda d: d.update(checks=[]),
                       lambda d: d['gas'].append({'frame': 'WARM'}),
                       lambda d: d.pop('verdictReason')):
            doc = copy.deepcopy(good)
            mutate(doc)
            with self.assertRaises(scenarios.LedgerRefused):
                scenarios.require_ledger(doc)

    def test_final_fs_pass_is_fail_closed(self):
        # Review finding 2, counterexamples 1 and 4: a one-check E-fresh ledger claiming PASS, or any
        # class claiming PASS, is rejected; failures carry through unchanged.
        self.assertFalse(scenarios.FINAL_GATE_PROVENANCE_IMPLEMENTED)
        for cls in ('E-fresh', 'E-hist', 'SYNTHETIC (class C)'):
            doc = {'schema': 'faroo-fork-ledger-v1', 'scenario': 'FS-1.A', 'evidenceClass': cls, 'aggregate': 'PASS',
                   'verdict': 'PASS', 'verdictReason': '', 'gas': [],
                   'checks': [{'id': 'a', 'verdict': 'PASS', 'expected': '1', 'actual': '1', 'detail': ''}]}
            with self.assertRaises(scenarios.LedgerRefused):
                scenarios.require_ledger(doc)
        failing = {'schema': 'faroo-fork-ledger-v1', 'scenario': 'x', 'evidenceClass': 'E-fresh', 'aggregate': 'FAIL',
                   'verdict': 'INCOMPLETE', 'verdictReason': 'x', 'gas': [],
                   'checks': [{'id': 'a', 'verdict': 'FAIL', 'expected': '1', 'actual': '2', 'detail': ''}]}
        with self.assertRaises(scenarios.LedgerRefused):
            scenarios.require_ledger(failing)

    def test_signatures_match_protected_artifacts(self):
        self.assertEqual(scenarios.SIGS['migrateFromV3'], '0x7f3daf92', 'approved Oracle V3 ABI')
        native = ROOT / 'cache/native-r4'
        if not (native / 'manifest.json').is_file():
            self.skipTest('NOT RUN: needs tools/ci/canonical.py output in the export')
        selectors = {}
        for name in ('Oracle', 'StPROS', 'YieldVaultFactory', 'YieldVault', 'TimelockController', 'ProxyAdmin',
                     'BridgeVault', 'SlpDistributor'):
            selectors.update({'0x' + v: k for k, v in json.loads((native / name / 'artifact.json').read_text())['selectors'].items()})
        for key, sel in scenarios.SIGS.items():
            if key in ('revokeRole', 'renounceRole', 'emergencyCancel'):
                continue  # legacy V1 vault / factory functions; checked below against R4 where present
            self.assertIn(sel, selectors, key)
        self.assertIn(scenarios.SIGS['revokeRole'], selectors)
        self.assertIn(scenarios.SIGS['emergencyCancel'], selectors)

    def test_mainnet_case_definitions(self):
        a, b, c, d = (scenarios.mainnet_case(x) for x in 'ABCD')
        self.assertEqual(len(a['u']), 10)
        self.assertEqual(len(b['u']), 9, 'B omits the Factory_test migration in U')
        self.assertEqual(b['h'][0][2], 'upgradeAndCall:factoryTest:migrateToR4', 'B fallback item 0')
        self.assertEqual(d['h'], b['h'])
        self.assertEqual(d['p1'], [])
        self.assertEqual([x[0] for x in c['p1']], ['E1', 'E1'])
        self.assertTrue(c['residualRecovery'] and d['residualRecovery'] and not a['residualRecovery'])
        self.assertEqual(scenarios.testnet_case()['u'][6 + 14], ('TL', 'vault14', 'grantRole'))
        self.assertEqual(scenarios.testnet_case(e2_available=False)['handoffCheck'], 'assertTestnetRecoveryHandoff')

    def _labels(self):
        names = ['proxyAdmin', 'oracle', 'stpros', 'factory', 'factoryTest', 'vault001', 'testVault', 'assertions', 'timelock']
        labels = {('0x' + format(i + 1, '040x')): n for i, n in enumerate(names)}
        labels['0x' + 'e1' * 20] = 'E1'
        labels['0x' + 'e2' * 20] = 'E2'
        labels['selector:0xaaaaaaaa'] = 'assertR4'
        return labels, {n: a for a, n in labels.items() if not a.startswith('selector')}

    def _upgrade(self, a, proxy, inner_sig):
        inner = scenarios.SIGS[inner_sig][2:] + '00' * 32
        body = abi.enc_word(a[proxy]) + abi.enc_word('0x' + '99' * 20) + abi.enc_word(96) + abi.enc_word(len(inner) // 2) + inner.ljust(64 * ((len(inner) + 63) // 64), '0')
        return {'to': a['proxyAdmin'], 'data': scenarios.SIGS['upgradeAndCall'] + body}

    def _u(self, a, case):
        call = lambda t, s: {'to': a[t], 'data': scenarios.SIGS[s] + abi.enc_word('0x' + '77' * 20)}
        txs = [self._upgrade(a, 'oracle', 'migrateFromV1.oracle'), self._upgrade(a, 'stpros', 'migrateFromV1.stpros'),
               self._upgrade(a, 'factory', 'migrateToR4'), call('factory', 'upgradeBeaconTo'), call('vault001', 'grantRole')]
        if case in 'AC':
            txs.append(self._upgrade(a, 'factoryTest', 'migrateToR4'))
        txs += [call('factoryTest', 'upgradeBeaconTo'), call('factoryTest', 'retireVault'),
                {'to': a['factoryTest'], 'data': scenarios.SIGS['closeCreation']}, {'to': a['assertions'], 'data': '0xaaaaaaaa'}]
        return txs

    def test_bundle_shape_conformance_and_adverse_orderings(self):
        labels, a = self._labels()
        for case in 'ABCD':
            self.assertEqual(scenarios.shape_mismatches(scenarios.mainnet_case(case)['u'], self._u(a, case), labels), [], case)
        # FS-1a viii: beacon upgrade before the factory migration is detected.
        swapped = self._u(a, 'A')
        swapped[2], swapped[3] = swapped[3], swapped[2]
        self.assertTrue(scenarios.shape_mismatches(scenarios.mainnet_case('A')['u'], swapped, labels))
        # A case-A bundle used for case B (item 6 present) is detected.
        self.assertTrue(scenarios.shape_mismatches(scenarios.mainnet_case('B')['u'], self._u(a, 'A'), labels))
        # P-1 actor check: E1 must send the Factory_test steps.
        p1 = [{'actor': a['E1'], 'to': a['factoryTest'], 'data': scenarios.SIGS['emergencyCancel'] + abi.enc_word(a['testVault'])},
              {'actor': a['E2'], 'to': a['testVault'], 'data': scenarios.SIGS['revokeRole'] + '00' * 64},
              {'actor': a['E2'], 'to': a['testVault'], 'data': scenarios.SIGS['renounceRole'] + '00' * 64},
              {'actor': a['E1'], 'to': a['factoryTest'], 'data': scenarios.SIGS['transferOwnership'] + '00' * 32}]
        self.assertEqual(scenarios.shape_mismatches(scenarios.mainnet_case('A')['p1'], p1, labels), [])
        p1[3]['actor'] = a['E2']
        self.assertIn('sent by E2, expected E1', ' '.join(scenarios.shape_mismatches(scenarios.mainnet_case('A')['p1'], p1, labels)))


# --------------------------------------------------------------------------- receipt-frame oracle

def frames(donation):
    d = 7 if donation else 0
    pre = {'bridgeNative': d, 'slpNative': 0, 'stNative': 3 if donation else 0, 'wprosAtSt': 150 + (1 if donation else 0),
           'reserve': 38, 'queued': 68, 'completed': 30, 'supply': 150, 'escrow': 38 + (1 if donation else 0),
           'poolT': 0, 'poolV': 0}
    s = pre['supply'] - pre['escrow']
    post = dict(pre, bridgeNative=d + 38, slpNative=pre['wprosAtSt'] - 38, wprosAtSt=0, reserve=0, supply=s,
                escrow=0, poolT=s, poolV=s)
    return pre, post


class OracleTest(unittest.TestCase):
    def verdicts(self, rows):
        return {r['id']: r['verdict'] for r in rows}

    def test_correct_rule_exact_with_and_without_donation(self):
        for donation in (False, True):
            pre, post = frames(donation)
            self.assertTrue(all(v == 'PASS' for v in self.verdicts(oracles.u_frame(pre, post, 38)).values()), donation)

    def test_wrong_rules_rejected_on_donation_state(self):
        pre, post = frames(True)
        failing = {'WRONG_ABSOLUTE_BRIDGE': 'C-1.bridgeNativeAfter', 'WRONG_SLP_SUPPLY_BACKED': 'C-2.slpNativeAfter',
                   'WRONG_ST_NATIVE_ZERO': 'C-3.stNativeAfter', 'WRONG_NO_ESCROW_BURN': "POST-3.S'==S-E"}
        for rule, check_id in failing.items():
            self.assertEqual(self.verdicts(oracles.u_frame(pre, post, 38, rule))[check_id], 'FAIL', rule)
        pre0, post0 = frames(False)
        self.assertEqual(self.verdicts(oracles.u_frame(pre0, post0, 38, 'WRONG_ABSOLUTE_BRIDGE'))['C-1.bridgeNativeAfter'],
                         'PASS', 'undetectable without a pre-existing BridgeVault balance')

    def test_custody_loss_and_ghost_mismatch_rejected(self):
        pre, post = frames(True)
        post['bridgeNative'] -= 1
        v = self.verdicts(oracles.u_frame(pre, post, 38))
        self.assertEqual(v['C-1.bridgeNativeAfter'], 'FAIL')
        self.assertEqual(v['C-5.M-1.wprosBefore==dBridge+dSlp'], 'FAIL')
        # A ghost missing one wei of pending (an incomplete inventory) is rejected.
        self.assertEqual(self.verdicts(oracles.u_frame(*frames(False), 37))['C-0.reserveTransferred==ghostPending'], 'FAIL')
        # A decreasing custody balance is never accepted, even if totals add up elsewhere.
        pre, post = frames(False)
        post['slpNative'] = pre['slpNative'] + pre['wprosAtSt'] + 5
        post['bridgeNative'] = pre['bridgeNative']
        v = self.verdicts(oracles.u_frame(pre, post, 38))
        self.assertEqual(v['C-1.bridgeNativeAfter'], 'FAIL')


    def test_c0_reserve_matching_ghost_but_not_q_minus_c_rejected(self):
        """Review finding 4: R_pre == ghost but R_pre != Q_pre - C_pre fails inside u_frame itself."""
        pre, post = frames(False)
        pre['queued'] += 32  # Q - C = 70 while R = ghost = 38
        post['queued'] = pre['queued']
        v = self.verdicts(oracles.u_frame(pre, post, 38))
        self.assertEqual(v['C-0.reserveTransferred==ghostPending'], 'PASS')
        self.assertEqual(v['C-0.reserveTransferred==Q_pre-C_pre'], 'FAIL')
        pre, post = frames(False)
        pre['completed'] = pre['queued'] + 1  # Q < C
        post['completed'] = pre['completed']
        v = self.verdicts(oracles.u_frame(pre, post, 38))
        self.assertEqual(v['C-0.Q_pre>=C_pre'], 'FAIL')
        self.assertEqual(v['C-0.reserveTransferred==Q_pre-C_pre'], 'FAIL')


# --------------------------------------------------------------------------- isolation and driver

class IsolationTest(unittest.TestCase):
    def test_fork_mode_is_refused(self):
        with self.assertRaises(anvil.ForkNotAuthorized):
            anvil.LoopbackNode(1672, 0, ROOT, fork_url='https://rpc.pharos.xyz')

    def test_driver_sources_have_no_remote_endpoint_or_secret_access(self):
        for path in HERE.glob('*.py'):
            if path.name == Path(__file__).name:
                continue
            text = path.read_text()
            self.assertNotIn('https://', text, path.name)
            if path.name == 'fork_launcher.py':
                # XR-I09: the single launcher may fork only through the budget proxy's loopback URL.
                self.assertEqual(text.count('--fork-url'), 1, path.name)
                self.assertIn("'--fork-url', proxy.url,", text)
                self.assertIn("LOOPBACK_PREFIX = 'http://127.0.0.1:'", text)
                self.assertIn('fm.ForkBudgetProxy(auth, ledger)', text)
            else:
                self.assertNotIn('--fork-url', text, path.name)
            # `.env` files (but not `os.environ`) are never referenced, except by the name guard.
            self.assertIsNone(re.search(r'\.env(?!iron)', text.replace("startswith('.env')", '')), path.name)
            self.assertNotIn('private_key', text.lower(), path.name)
        for name in ('anvil.py', 'fork_launcher.py'):
            command = (HERE / name).read_text()
            for flag in ("'--accounts', '0'", "'--host', '127.0.0.1'", "'--block-base-fee-per-gas', '0'"):
                self.assertIn(flag, command, name)



FIXTURE_OUT = ROOT / 'cache_forge/rehearsal/fork-campaign'
ZERO_CASE = 'anvil-synthetic-zero-donation'


def _exec_steps(cfg):
    return [s for s in cfg['sequence']['steps'] if s['data'].lower().startswith(safe_route.EXEC_SELECTOR)]


def _approval_steps(cfg):
    return [s for s in cfg['sequence']['steps'] if s['data'].lower().startswith(safe_route.APPROVE_HASH_SELECTOR)]


def _set_tx(cfg, **fields):
    """Re-encodes every execTransaction of the case with changed Safe transaction fields."""
    for step in _exec_steps(cfg):
        tx, signatures = safe_route.decode_exec(step['data'])
        tx.update(fields)
        step['data'] = abi.encode_function(safe_route.EXEC_TRANSACTION,
                                           [*(tx[f] for f in safe_route.SAFE_TX_FIELDS), signatures])


def _rehash(cfg, declared=True, approvals=True):
    """Recomputes the Safe hash of the (changed) transaction and, optionally, the declared hash
    and the approvals that reference it, as an altered export would."""
    tx, _ = safe_route.decode_exec(_exec_steps(cfg)[-1]['data'])
    tx_hash = safe_route.safe_tx_hash(cfg['chainId'], cfg['safe'], tx, cfg['safeNonce'])
    if declared:
        cfg['safeTxHash'] = tx_hash
    if approvals:
        for step in _approval_steps(cfg):
            step['data'] = safe_route.approve_hash_calldata(tx_hash)
    return tx_hash


def _redigest(cfg):
    cfg['sequence']['digest'] = safe_route.sequence_digest(cfg['sequence']['steps'])


class ReplayAdmissionTest(unittest.TestCase):
    """Review finding IV-01: the receipt replay admits only the approved Safe route, recomputed
    from the exported bytes. Every refusal is a ``ReplayValidationError`` raised before a node starts."""

    def setUp(self):
        path = FIXTURE_OUT / f'{ZERO_CASE}.json'
        if not path.is_file():
            self.skipTest('NOT RUN: run the CampaignAnvilFixture tests in this export first')
        self.original = json.loads(path.read_text())

    def case(self):
        return copy.deepcopy(self.original)

    def refused(self, cfg, message):
        with self.assertRaises(ReplayValidationError) as raised:
            safe_route.validate_case(cfg, ZERO_CASE)
        self.assertIn(message, str(raised.exception))

    def test_unmodified_export_admitted(self):
        route = safe_route.validate_case(self.case(), ZERO_CASE)
        self.assertEqual(route['txHash'], self.original['safeTxHash'].lower())
        # The Python digest reproduces TxSequence.digest (Solidity abi.encode(Step[])) exactly.
        self.assertEqual(safe_route.sequence_digest(self.original['sequence']['steps']),
                         self.original['sequence']['digest'].lower())
        self.assertEqual(route['tx']['safeTxGas'], 0)
        self.assertEqual(len(route['approvals']), route['threshold'] - 1)
        self.assertNotIn(route['executor'], route['approvals'].values())
        self.assertEqual(route['executions'], ['safe.execTransaction.withoutApprovals', 'safe.execTransaction'])

    def test_reviewer_nonzero_safe_tx_gas_case_refused(self):
        # Agent C's reproduction: safeTxGas = 500000, approvals recalculated, declared hash and digest stale.
        cfg = self.case()
        _set_tx(cfg, safeTxGas=500000)
        new_hash = _rehash(cfg, declared=False)
        self.assertNotEqual(new_hash, cfg['safeTxHash'].lower())
        self.refused(cfg, 'stale sequence digest')

    def test_changed_transaction_parameters_refused_even_when_rebound(self):
        # Every binding recomputed consistently: only the route rule itself can refuse.
        for fields, message in (({'safeTxGas': 500000}, 'safeTxGas == 0'),
                                ({'baseGas': 1}, 'carries no gas refund'),
                                ({'gasPrice': 1}, 'carries no gas refund'),
                                ({'refundReceiver': '0x' + '77' * 20}, 'carries no gas refund'),
                                ({'operation': 0}, 'CALL to a MultiSend or singleton library'),
                                ({'to': '0x' + '77' * 20}, 'DELEGATECALL to an unreviewed target'),
                                ({'operation': 2}, 'invalid operation')):
            with self.subTest(fields=fields):
                cfg = self.case()
                _set_tx(cfg, **fields)
                _rehash(cfg)
                _redigest(cfg)
                self.refused(cfg, message)

    def test_changed_transaction_with_stale_hash_refused(self):
        # One byte of the batch changed, digest recomputed, declared hash and approvals left stale.
        cfg = self.case()
        tx, _ = safe_route.decode_exec(_exec_steps(cfg)[-1]['data'])
        _set_tx(cfg, data=tx['data'][:-2] + ('00' if tx['data'][-2:] != '00' else '01'))
        _redigest(cfg)
        self.refused(cfg, 'stale declared safeTxHash')

    def test_stale_declared_hash_refused(self):
        cfg = self.case()
        cfg['safeTxHash'] = '0x' + 'ab' * 32
        self.refused(cfg, 'stale declared safeTxHash')

    def test_stale_sequence_digest_refused(self):
        cfg = self.case()
        cfg['sequence']['digest'] = '0x' + 'cd' * 32
        self.refused(cfg, 'stale sequence digest')
        cfg = self.case()
        cfg['sequence']['steps'][1]['label'] += '.renamed'
        self.refused(cfg, 'stale sequence digest')

    def test_inconsistent_approval_hash_refused(self):
        cfg = self.case()
        _approval_steps(cfg)[0]['data'] = safe_route.approve_hash_calldata('0x' + 'ab' * 32)
        _redigest(cfg)
        self.refused(cfg, 'approval does not reference the Safe transaction hash')
        cfg = self.case()
        cfg['sequence']['steps'].remove(_approval_steps(cfg)[0])
        _redigest(cfg)
        self.refused(cfg, 'approvals must cover exactly the signers other than the executor')

    def test_signature_and_route_shape_violations_refused(self):
        cfg = self.case()
        tx, signatures = safe_route.decode_exec(_exec_steps(cfg)[-1]['data'])
        for step in _exec_steps(cfg):
            step['data'] = abi.encode_function(safe_route.EXEC_TRANSACTION,
                                               [*(tx[f] for f in safe_route.SAFE_TX_FIELDS), signatures[:-130]])
        _redigest(cfg)
        self.refused(cfg, 'exactly threshold approved-hash signatures')
        cfg = self.case()
        cfg['sequence']['steps'][-1]['value'] = '1'
        _redigest(cfg)
        self.refused(cfg, 'sends no value')
        cfg = self.case()
        cfg['sequence']['steps'][-1]['from'] = '0x' + '77' * 20
        _redigest(cfg)
        self.refused(cfg, 'executor is not a Safe owner')
        cfg = self.case()
        cfg['chainId'] = 1672
        self.refused(cfg, 'sequence chainId differs')
        cfg = self.case()
        cfg['extra'] = 1
        self.refused(cfg, 'case keys')

    def test_refusal_does_not_depend_on_assert(self):
        # Under python -O every assert statement is removed; admission and the ledger gate still refuse.
        cfg = self.case()
        cfg['safeTxHash'] = '0x' + 'ab' * 32
        program = ('import json,sys; sys.path[:0]=sys.argv[1:3]; import safe_route, scenarios\n'
                   'cfg=json.loads(sys.argv[3])\n'
                   'try:\n    safe_route.validate_case(cfg)\n    print("ADMITTED")\nexcept safe_route.ReplayValidationError:\n    print("REFUSED")\n'
                   'doc={"schema":"faroo-fork-ledger-v1","scenario":"x","evidenceClass":"E-fresh","aggregate":"PASS",'
                   '"verdict":"PASS","verdictReason":"","gas":[],"checks":[{"id":"a","verdict":"PASS","expected":"1","actual":"1","detail":""}]}\n'
                   'try:\n    scenarios.require_ledger(doc)\n    print("LEDGER-ACCEPTED")\nexcept scenarios.LedgerRefused:\n    print("LEDGER-REFUSED")\n')
        result = subprocess.run([sys.executable, '-O', '-c', program, str(HERE), str(ROOT), json.dumps(cfg)],
                                capture_output=True, text=True, env={}, check=True)
        self.assertEqual(result.stdout.split(), ['REFUSED', 'LEDGER-REFUSED'])


class ExecutionEventTest(unittest.TestCase):
    """The Safe's execution result is read from its own event, bound to the admitted hash."""
    SAFE = '0x' + '5a' * 20
    HASH = '0x' + '9a' * 32

    def receipt(self, *logs, status='0x1'):
        return {'status': status, 'logs': list(logs)}

    def event(self, topic=safe_route.EXECUTION_SUCCESS, tx_hash=HASH, payment=0, address=SAFE, extra=()):
        return {'address': address, 'topics': [topic, *extra], 'data': '0x' + tx_hash[2:] + format(payment, '064x')}

    def test_bound_success_only(self):
        ok = self.receipt(self.event())
        self.assertTrue(safe_route.inner_success(ok, self.SAFE, self.HASH))
        self.assertEqual(safe_route.execution_events(ok, self.SAFE), [('SUCCESS', self.HASH, 0)])
        for label, receipt in (
                ('wrong hash', self.receipt(self.event(tx_hash='0x' + 'ff' * 32))),
                ('non-zero payment', self.receipt(self.event(payment=1))),
                ('failure event', self.receipt(self.event(topic=safe_route.EXECUTION_FAILURE))),
                ('success and failure', self.receipt(self.event(), self.event(topic=safe_route.EXECUTION_FAILURE))),
                ('duplicate success', self.receipt(self.event(), self.event())),
                ('malformed topics', self.receipt(self.event(extra=('0x' + '00' * 32,)))),
                ('other emitter', self.receipt(self.event(address='0x' + '77' * 20))),
                ('no event', self.receipt()),
                ('reverted outer call', self.receipt(self.event(), status='0x0'))):
            with self.subTest(label):
                self.assertFalse(safe_route.inner_success(receipt, self.SAFE, self.HASH))
        malformed = safe_route.execution_events(self.receipt(self.event(extra=('0x' + '00' * 32,))), self.SAFE)
        self.assertEqual(malformed, [('MALFORMED', None, None)])

    def indexed(self, topic=safe_route.EXECUTION_SUCCESS, tx_hash=HASH, payment=0, data=None):
        """Safe 1.4.x: ``ExecutionSuccess(bytes32 indexed txHash, uint256 payment)``."""
        return {'address': self.SAFE, 'topics': [topic, tx_hash], 'data': data or '0x' + format(payment, '064x')}

    def test_safe_1_4_1_indexed_encoding(self):
        ok = self.receipt(self.indexed())
        self.assertTrue(safe_route.inner_success(ok, self.SAFE, self.HASH))
        self.assertEqual(safe_route.execution_events(ok, self.SAFE), [('SUCCESS', self.HASH, 0)])
        for label, receipt in (('wrong hash', self.receipt(self.indexed(tx_hash='0x' + 'ff' * 32))),
                               ('non-zero payment', self.receipt(self.indexed(payment=1))),
                               ('failure event', self.receipt(self.indexed(topic=safe_route.EXECUTION_FAILURE))),
                               ('hash also in data', self.receipt(self.indexed(data='0x' + self.HASH[2:] + '00' * 32))),
                               ('short topic', self.receipt(self.indexed(tx_hash='0x9a'))),
                               ('reverted outer call', self.receipt(self.indexed(), status='0x0'))):
            with self.subTest(label):
                self.assertFalse(safe_route.inner_success(receipt, self.SAFE, self.HASH))


class SyntheticDriverTest(unittest.TestCase):
    def setUp(self):
        out = ROOT / 'cache_forge/rehearsal/fork-campaign'
        if not (out / 'anvil-synthetic-positive-donation-state.json').is_file():
            self.skipTest('NOT RUN: run the CampaignAnvilFixture tests in this export first')
        import run_synthetic
        self.run_synthetic = run_synthetic

    def test_receipt_frame_run_passes_and_is_deterministic(self):
        for name in self.run_synthetic.CASES:
            first, d1, r1 = self.run_synthetic.run_case(name)
            second, d2, r2 = self.run_synthetic.run_case(name)
            self.assertEqual(first['aggregate'], 'PASS', name)
            self.assertEqual(first['verdict'], 'INCOMPLETE', 'synthetic evidence never closes a scenario')
            self.assertEqual((d1, r1), (d2, r2))
            self.assertEqual([s for _, s, _ in r1], [0, 1, 1, 1], 'rejection, two approvals, execution')
            self.assertTrue(all(g['frame'] in ('RECEIPT', 'ESTIMATE') for g in first['gas']))

    def test_receipt_frame_wrong_rule_fails(self):
        ledger, _, _ = self.run_synthetic.run_case('anvil-synthetic-positive-donation', 'WRONG_ABSOLUTE_BRIDGE')
        self.assertEqual(ledger['aggregate'], 'FAIL')

    def verdicts(self, ledger):
        return {c['id']: c['verdict'] for c in ledger['checks']}

    def test_execution_outcome_inner_result_and_nonce_recorded(self):
        ledger, _, _ = self.run_synthetic.run_case('anvil-synthetic-zero-donation')
        v = self.verdicts(ledger)
        for check in ('safe.execTransaction.outcome.outerCall', 'safe.execTransaction.outcome.innerExecution',
                      'safe.execTransaction.nonce.expectedProgression',
                      'safe.execTransaction.withoutApprovals.outcome.outerCall',
                      'safe.execTransaction.withoutApprovals.nonce.expectedProgression'):
            self.assertEqual(v[check], 'PASS', check)

    def test_unexpected_execution_failure_fails(self):
        # Review finding 3: without one approval the execution reverts; nonce and ownership stay
        # consistent, yet the run is a FAIL.
        cfg = json.loads((ROOT / 'cache_forge/rehearsal/fork-campaign/anvil-synthetic-zero-donation.json').read_text())
        approval = next(s['label'] for s in cfg['sequence']['steps'] if s['label'].startswith('safe.approveHash'))
        ledger, _, receipts = self.run_synthetic.run_case('anvil-synthetic-zero-donation', drop=(approval,))
        v = self.verdicts(ledger)
        self.assertEqual(v['safe.execTransaction.outcome.outerCall'], 'FAIL')
        self.assertEqual(v['safe.execTransaction.outcome.innerExecution'], 'FAIL')
        self.assertEqual(v['safe.execTransaction.nonce.consistentWithOuterCall'], 'PASS')
        self.assertEqual(v['safe.ownersUnchanged'], 'PASS')
        self.assertEqual(ledger['aggregate'], 'FAIL')
        self.assertNotEqual(ledger['verdict'], 'PASS')

    def test_admitted_route_checks_recorded(self):
        ledger, _, _ = self.run_synthetic.run_case('anvil-synthetic-zero-donation')
        v = self.verdicts(ledger)
        for check in ('chain.id', 'safe.nonceBefore', 'safe.thresholdUnchanged',
                      'safe.execTransaction.safeTxHash.onChainEqualsIndependent',
                      'safe.execTransaction.withoutApprovals.safeTxHash.onChainEqualsIndependent',
                      'safe.execTransaction.executionEvent.count', 'safe.execTransaction.executionEvent.txHash',
                      'safe.execTransaction.executionEvent.payment', 'safe.execTransaction.executionEvent.success',
                      'safe.execTransaction.withoutApprovals.executionEvent.count'):
            self.assertEqual(v[check], 'PASS', check)
        approvals = [c for c in v if c.startswith('safe.approveHash.') and c.endswith('.approvedHash')]
        self.assertEqual(len(approvals), 2)
        self.assertTrue(all(v[c] == 'PASS' for c in approvals))

    def test_altered_export_is_never_executed(self):
        # Agent C's stale-binding reproductions are refused at admission, before any node starts.
        cfg = json.loads((ROOT / 'cache_forge/rehearsal/fork-campaign/anvil-synthetic-zero-donation.json').read_text())
        stale = copy.deepcopy(cfg)
        stale['safeTxHash'] = '0x' + 'ab' * 32
        stale['sequence']['digest'] = '0x' + 'cd' * 32
        altered = copy.deepcopy(cfg)
        _set_tx(altered, safeTxGas=500000)
        _rehash(altered, declared=False)
        for case in (stale, altered):
            with self.assertRaises(ReplayValidationError):
                self.run_synthetic.run_case('anvil-synthetic-zero-donation', case=case)
        with self.assertRaises(ReplayValidationError):
            self.run_synthetic.run_case('anvil-synthetic-zero-donation', drop=('no.such.step',))

    def test_expected_rejection_that_succeeds_fails(self):
        cfg = json.loads((ROOT / 'cache_forge/rehearsal/fork-campaign/anvil-synthetic-zero-donation.json').read_text())
        approvals = tuple(s['label'] for s in cfg['sequence']['steps'] if s['label'].startswith('safe.approveHash'))
        # The final execution declared as a required revert, although it succeeds.
        ledger, _, _ = self.run_synthetic.run_case('anvil-synthetic-zero-donation', expect={'safe.execTransaction': False})
        v = self.verdicts(ledger)
        self.assertEqual(v['safe.execTransaction.outcome.outerCall'], 'FAIL')
        self.assertEqual(ledger['aggregate'], 'FAIL')
        self.assertTrue(approvals)


if __name__ == '__main__':
    unittest.main()
