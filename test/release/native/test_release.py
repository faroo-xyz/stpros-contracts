"""Focused native Release regressions and deliberate wrong-rule controls."""
import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate
from tools.lib import abi, release_json as j
from tools.release.native import inputs, plans, history, checkpoint, timelock, economics, residual
from tools.release.native import rpc as transport
from tools.release.native import __main__ as cli

CONTROL_RESULTS = []
CONTROL_CASES = {
    'ARTIFACT': 'reference-supplement.mjs:0032',
    'CONFIG': 'remediation.test.mjs:0003',
    'MAIN-P1': 'reference-rich-plans.mjs:0002',
    'MAIN-HU': 'release-tooling.test.mjs:0001',
    'TESTNET': 'reference-rich-plans.mjs:0003',
    'SAFE-STRUCT': 'read-only.integration.test.mjs:0009',
    'SAFE-NONCE': 'read-only.integration.test.mjs:0011',
    'GC01': 'governance-closure.test.mjs:0001',
    'GC03-LIFECYCLE': 'governance-closure.test.mjs:0046',
    'GC03-COVERAGE': 'remediation.test.mjs:0092',
    'ARCHIVE': 'reference-supplement.mjs:0039',
    'JOURNAL': 'history.test.mjs:0011',
    'FRESHNESS': 'read-only.integration.test.mjs:0017',
    'ECON-HOLDERS': 'release-tooling.test.mjs:0057',
    'ECON-QUEUE': 'read-only.integration.test.mjs:0113',
    'ECON-POOL': 'remediation.test.mjs:0031',
    'RESIDUAL': 'remediation.test.mjs:0113',
    'POST-GAS': 'reference-cli.mjs:0036',
}
REQUIRED_CONTROL_FAMILIES = frozenset((*CONTROL_CASES, 'AUTHORITY-SURFACE'))


@contextmanager
def wrong_rule(family):
    if family in ('ARTIFACT', 'CONFIG', 'SAFE-NONCE'):
        module, code = {'ARTIFACT': (inputs, 'RUNTIME_CODEHASH'), 'CONFIG': (inputs, 'OPERATIONAL_TIMELOCK_AUTHORITY'), 'SAFE-NONCE': (checkpoint, 'SAFE_NONCE')}[family]
        original = module.require
        def ignore(condition, name, detail=''):
            if name != code:
                original(condition, name, detail)
        target = patch.object(module, 'require', ignore)
    elif family == 'SAFE-STRUCT':
        original = checkpoint.exact_set
        target = patch.object(checkpoint, 'exact_set', lambda a, b, code: None if code == 'SAFE_OWNERS' else original(a, b, code))
    elif family in ('MAIN-P1', 'TESTNET'):
        original = plans.cleanup
        target = patch.object(plans, 'cleanup', lambda *a, **kw: list(reversed(original(*a, **kw))))
    elif family == 'MAIN-HU':
        original = plans.operation
        def swap(transactions, *args):
            transactions[0], transactions[1] = transactions[1], transactions[0]
            return original(transactions, *args)
        target = patch.object(plans, 'operation', swap)
    elif family == 'GC01':
        target = patch.object(inputs, 'require_mainnet_phase', lambda _: None)
    elif family == 'GC03-LIFECYCLE':
        target = patch.object(timelock, 'require_dependency_done', lambda *a: None)
    elif family == 'GC03-COVERAGE':
        target = patch.object(timelock, 'require_action_bearing_coverage', lambda *a: abi.UNDEFINED)
    elif family == 'ARCHIVE':
        original = j.checked_json
        target = patch.object(j, 'checked_json', lambda path, expected=None: original(path))
    elif family == 'JOURNAL':
        original = history.HistorySession.extend
        async def missing_as_empty(session, head):
            raw = session.raw_rpc
            async def read(method, params):
                result = await raw(method, params)
                return [] if method == 'eth_getLogs' and result is abi.UNDEFINED else result
            session.raw_rpc = read
            try:
                return await original(session, head)
            finally:
                session.raw_rpc = raw
        target = patch.object(history.HistorySession, 'extend', missing_as_empty)
    elif family == 'FRESHNESS':
        target = patch.object(checkpoint, 'require_fresh_head', lambda *a: None)
    elif family == 'ECON-HOLDERS':
        original = economics.exact_set
        target = patch.object(economics, 'exact_set', lambda a, b, code: None if code == 'HOLDER_TRUNCATION' else original(a, b, code))
    elif family == 'ECON-QUEUE':
        target = patch.object(economics, 'request_receiver', lambda receiver, owner, migrated: receiver)
    elif family == 'ECON-POOL':
        target = patch.object(economics, 'empty_pool_nav', lambda tokens, shares: tokens)
    elif family == 'RESIDUAL':
        target = patch.object(residual, 'require_direct_call', lambda *a: None)
    elif family == 'POST-GAS':
        original = checkpoint.estimate_gas
        async def promote(*args):
            result = await original(*args); result['atomicBatch'] = True
            return result
        target = patch.object(checkpoint, 'estimate_gas', promote)
    else:
        raise AssertionError('Unknown wrong-rule family')
    with target:
        yield


class NativeReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wanted = set(CONTROL_CASES.values()) | {'reference-cli.mjs:0001'}
        cls.cases = {rec['id']: rec for rec in gate.records() if rec['id'] in wanted}
        if set(cls.cases) != wanted:
            raise AssertionError('Required wrong-rule witness missing')

    def test_frozen_corpus_and_all_required_actions(self):
        lock, index = gate.verify_corpus()
        self.assertEqual(len(index), lock['caseCount'])
        actions = {row['function'].split('release:')[1] for row in index if row['function'].startswith('index.ts.release:')}
        self.assertEqual(actions, set(cli.RELEASE_ACTIONS))
        self.assertEqual(len({row['id'] for row in index}), len(index))
        self.assertTrue(any(row['outcome'] == 'return' for row in index))
        self.assertTrue(any(row['outcome'] == 'throw' for row in index))
        import witness_inputs
        witness = gate.ROOT/'test/fixtures/native-release/plans.json'
        self.assertEqual(json.loads(witness.read_text()), witness_inputs.extract())

    def test_parser_rejects_unapproved_authority_before_input_io(self):
        for args in [('release', 'sign'), ('release', 'send'), ('release', 'deploy'), ('loss', 'contingency'),
                     ('ops', 'prepare-round'), ('artifact', 'submit'), ('release', 'build-u', '--send'),
                     ('release', 'preflight', '--private-key', 'SYNTHETIC-UNUSED')]:
            with self.subTest(args=args), patch.object(j, 'checked_json', side_effect=AssertionError('Unexpected input access')):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                    cli.main(list(args))
                self.assertEqual(result.exception.code, 2)
        groups = cli.parser()._subparsers._group_actions[0].choices
        self.assertEqual(set(groups), {'release', 'artifact'})
        self.assertEqual(set(groups['release']._subparsers._group_actions[0].choices), set(cli.RELEASE_ACTIONS))
        self.assertEqual(set(groups['artifact']._subparsers._group_actions[0].choices), {'normalize'})

    def test_missing_approval_fails_before_file_and_rpc_access(self):
        with patch.object(j, 'checked_json', side_effect=AssertionError('Unexpected input access')):
            with self.assertRaisesRegex(j.ReleaseError, 'INCOMPLETE: config and independent review hash required'):
                asyncio.run(cli.run_release('preflight', release_config='/private/tmp/harmless-unread.json', review_hash='invalid'))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(cli.main(['release', 'preflight']), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), 'INCOMPLETE: config and independent review hash required\n')

    def test_standalone_python_cli_exact_stdout(self):
        rec = self.cases['reference-cli.mjs:0001']; gate.prepare(rec); args = rec['arguments'][0]
        result = subprocess.run([sys.executable, '-m', 'tools.release.native', 'release', 'preflight',
                                 '--release-config', args['releaseConfig'], '--review-hash', args['reviewHash']],
                                cwd=gate.ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, rec['value']['stdout'])
        self.assertEqual(result.stderr, '')

    def test_control_epoch_and_clock_boundaries(self):
        rec = {'callbacks': [
            {'label': '0', 'args': ['eth_getCode', ['0x'+'12'*20, '0x1']], 'order': 0, 'outcome': 'return', 'value': '0x'},
            {'label': '0', 'args': ['eth_getBlockByNumber', ['latest', False]], 'order': 1, 'outcome': 'return', 'value': None}],
            'clocks': [1000], 'events': [{'kind': 'callback-start', 'index': 0}, {'kind': 'callback-end', 'index': 0},
                                       {'kind': 'clock', 'index': 0}, {'kind': 'callback-start', 'index': 1}, {'kind': 'callback-end', 'index': 1}]}
        tape = gate.Tape(rec)
        with self.assertRaisesRegex(AssertionError, 'epoch'):
            asyncio.run(tape.callback('0')('eth_getBlockByNumber', ['latest', False]))
        tape = gate.Tape(rec)
        with self.assertRaisesRegex(AssertionError, 'phase boundary'):
            tape.now()
        rec['events'].insert(2, {'kind': 'read', 'path': '/private/tmp/synthetic.json'})
        tape = gate.Tape(rec)
        with self.assertRaisesRegex(AssertionError, 'phase boundary'):
            tape.record_io({'kind': 'read', 'path': '/private/tmp/synthetic.json'})

    def test_changed_corpus_lock_is_rejected(self):
        original = Path.read_bytes
        def changed(path):
            return b'changed' if path == gate.HERE/'corpus/reference-lock.json' else original(path)
        with patch.object(Path, 'read_bytes', changed), self.assertRaisesRegex(ValueError, 'reference lock changed'):
            gate.verify_corpus()

    def test_wrong_rule_AUTHORITY_SURFACE(self):
        calls = []
        async def forbidden_post(url, body, timeout):
            calls.append(body)
            raise AssertionError('Write method reached mocked transport')
        rpc = transport.ReleaseRpc('http://127.0.0.1:8545', True, post=forbidden_post)
        with self.assertRaisesRegex(j.ReleaseError, 'RPC_METHOD_REFUSED'):
            asyncio.run(rpc('eth_sendRawTransaction', ['0x']))
        self.assertEqual(calls, [])
        with patch.object(transport, 'METHODS', transport.METHODS | {'eth_sendRawTransaction'}):
            with self.assertRaisesRegex(AssertionError, 'Write method reached mocked transport'):
                asyncio.run(rpc('eth_sendRawTransaction', ['0x']))
        self.assertEqual(len(calls), 1)
        CONTROL_RESULTS.append({'family': 'AUTHORITY-SURFACE', 'detected': True, 'boundary': 'refusal before mocked transport'})


def wrong_rule_test(family, case_id):
    def test(self):
        rec = self.cases[case_id]
        baseline = asyncio.run(gate.run_case(rec))
        self.assertTrue(baseline['pass'], json.dumps(baseline, default=str)[:800])
        with wrong_rule(family):
            changed = asyncio.run(gate.run_case(rec))
        self.assertFalse(changed['pass'], 'The intentional wrong rule was not detected')
        CONTROL_RESULTS.append({'family': family, 'caseId': case_id, 'baselinePass': True, 'detected': True,
                                'outcomeMismatch': not changed['outcomeMatch'], 'ioMismatch': not changed['ioMatch'],
                                'transcriptError': changed['transcriptError'], 'candidateOutcome': changed['actual']['outcome'],
                                'candidateError': changed['actual'].get('error')})
    return test


for family, case_id in CONTROL_CASES.items():
    setattr(NativeReleaseTests, 'test_wrong_rule_'+family.replace('-', '_'), wrong_rule_test(family, case_id))


if __name__ == '__main__':
    unittest.main()
