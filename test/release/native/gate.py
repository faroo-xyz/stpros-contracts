"""Frozen original-engine Release conformance; never updates expected outputs."""
import argparse
import asyncio
from collections import deque
import errno
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tools.lib import abi, release_json as j
from tools.release.native import inputs, plans, history, timelock, economics, residual, checkpoint
from tools.release.native import __main__ as cli
from tools.release.native.rpc import ReleaseRpc, TransportError
from tools.release.native._shared import pretty_json

LOCK_SHA256 = 'bf0049a12b14a4a10f11705a32d21552f2d5da5de1000c67de4aba86e707d4cc'
INPUT_ROOT = Path('/private/tmp/faroo-native-release/inputs')
OWNER = 'FAROO NATIVE RELEASE SYNTHETIC INPUTS; reference-lock-bound replay only\n'
REFERENCE_ROOT = '/private/tmp/faroo-native-release/reference'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pack(value):
    if value is abi.UNDEFINED:
        return {'$undefined': True}
    if isinstance(value, abi.BigInt):
        return {'$bigint': str(value)}
    if callable(value):
        return {'$function': True}
    if isinstance(value, list):
        return [pack(x) for x in value]
    if isinstance(value, dict):
        return {key: pack(x) for key, x in value.items()}
    return value


def revive(value):
    if isinstance(value, dict):
        if set(value) == {'$bigint'}:
            return abi.BigInt(value['$bigint'])
        if set(value) == {'$undefined'}:
            return abi.UNDEFINED
        if set(value) == {'$number'}:
            return float(value['$number'])
        return {k: revive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [revive(x) for x in value]
    return value


def canonical(value):
    return j.canonical(value)


def verify_corpus():
    raw = (HERE/'corpus/reference-lock.json').read_bytes()
    if sha(raw) != LOCK_SHA256:
        raise ValueError('Frozen release reference lock changed')
    lock = json.loads(raw)
    for name, expected in lock['files'].items():
        if sha((HERE/name).read_bytes()) != expected:
            raise ValueError('Frozen release reference file changed: '+name)
    index = json.loads((HERE/'corpus/index.json').read_text())
    if len(index) != lock['caseCount'] or len({x['id'] for x in index}) != len(index):
        raise ValueError('Missing/duplicate frozen case')
    return lock, index


def records():
    lock, index = verify_corpus()
    expected = {x['id']: x for x in index}; observed = set()
    for name in lock['files']:
        if not name.endswith('.json.gz'):
            continue
        rows = json.loads(gzip.decompress((HERE/name).read_bytes()))['records']
        for rec in rows:
            identity = sha(json.dumps(rec, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode())
            if rec['id'] in observed or rec['id'] not in expected or expected[rec['id']]['sha256'] != identity:
                raise ValueError('Frozen case identity changed')
            observed.add(rec['id']); yield rec
    if observed != set(expected):
        raise ValueError('Incomplete frozen corpus')


def safe_path(name):
    path = Path(name)
    if not path.is_absolute() or str(path) != name or not path.is_relative_to(INPUT_ROOT) or path.suffix != '.json':
        raise ValueError('Unsafe replay path')
    for part in path.parts:
        if part.startswith('.') or part.lower() in ('key', 'keys', 'credential', 'credentials', 'secret', 'secrets', 'keystore', 'keystores'):
            raise ValueError('Unsafe replay path component')
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Symlink replay path')
    return path


def prepare(rec):
    import base64
    # Validate ancestors before even reading the ownership marker.
    for path in [INPUT_ROOT/'OWNER.txt', INPUT_ROOT, *INPUT_ROOT.parents]:
        if path.is_symlink():
            raise ValueError('Symlink synthetic fixture root')
    if not INPUT_ROOT.exists():
        INPUT_ROOT.mkdir(parents=True); (INPUT_ROOT/'OWNER.txt').write_text(OWNER)
    if (INPUT_ROOT/'OWNER.txt').read_text() != OWNER:
        raise ValueError('Unowned synthetic fixture directory')
    for name, entry in rec['files'].items():
        path = safe_path(name); raw = base64.b64decode(entry['raw'])
        if sha(raw) != entry['sha256']:
            raise ValueError('Frozen input digest changed')
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    for e in rec['events']:
        if e['kind'] == 'lstat' and e.get('error', {}).get('code') == 'ENOENT':
            safe_path(e['path']).unlink(missing_ok=True)


def error_value(error):
    if isinstance(error, j.ReleaseError):
        return {'name': 'Error', 'message': str(error)}
    if isinstance(error, abi.AbiError):
        return {'name': error.code, 'message': str(error)}
    if isinstance(error, TransportError):
        return {'name': error.name, 'message': str(error)}
    if isinstance(error, OSError):
        return {'name': 'Error', 'message': str(error), 'code': errno.errorcode.get(error.errno, str(error.errno)), 'syscall': getattr(error, 'release_syscall', 'lstat'), 'path': str(error.filename)}
    return {'name': type(error).__name__, 'message': str(error)}


def normalize_io(value):
    if isinstance(value, list):
        return [normalize_io(x) for x in value]
    if isinstance(value, dict):
        result = {k: normalize_io(v) for k, v in value.items()}
        if result.get('code') == 'ENOENT':
            # The approved E3 normalization is OS diagnostic wording only.
            result.pop('message', None)
        return result
    if isinstance(value, str):
        if value == REFERENCE_ROOT or value.startswith(REFERENCE_ROOT+'/'):
            return '$REPO'+value[len(REFERENCE_ROOT):]
        if value == str(ROOT) or value.startswith(str(ROOT)+'/'):
            return '$REPO'+value[len(str(ROOT)):]
    return value


class Tape:
    """Replay the recorded call/settlement frontier; no response guessing.

    Requests in the same recorded start frontier may arrive in another order.
    Responses settle only in the original causal order. Every required call,
    clock read and input-file access must be accounted for.
    """
    def __init__(self, rec):
        self.rec = rec; self.cursor = 0; self.clock = 0
        self.events = [e for e in rec['events'] if e['kind'].startswith('callback-')]
        self.taken = set(); self.pending = {}; self.failure = None; self.io = []; self.starts = []; self.trace = []
        self.queues = {}
        self.epochs = {}; self.epoch_remaining = {}; self.epoch_order = deque()
        self.clock_frontiers = []; self.io_frontiers = []; started, settled = set(), set()
        epoch, current = 0, None
        for event in rec['events']:
            if event['kind'] == 'callback-start':
                entry = rec['callbacks'][event['index']]; arguments = entry['args']
                key = None
                if len(arguments) == 2 and arguments[0] in ('eth_call', 'eth_getCode', 'eth_getStorageAt') and isinstance(arguments[1], list) and arguments[1] and isinstance(arguments[1][-1], str) and arguments[1][-1].startswith('0x'):
                    key = (entry['label'], arguments[1][-1])
                if key is None or key != current:
                    epoch += 1; self.epoch_order.append(epoch)
                current = key; self.epochs[entry['order']] = epoch
                self.epoch_remaining[epoch] = self.epoch_remaining.get(epoch, 0) + 1
                started.add(entry['order'])
            elif event['kind'] == 'callback-end':
                settled.add(event['index'])
            elif event['kind'] == 'clock':
                self.clock_frontiers.append((set(started), set(settled))); current = None
            elif event['kind'] in ('read', 'lstat', 'write'):
                self.io_frontiers.append((set(started), set(settled), len(self.clock_frontiers)))
                current = None
        self.settled = set()
        for entry in rec['callbacks']:
            key = (entry['label'], canonical(entry['args']))
            self.queues.setdefault(key, deque()).append(entry)

    def callback(self, label):
        async def read(*args):
            key = (label, canonical(pack(list(args))))
            queue = self.queues.get(key)
            if not queue:
                self.failure = 'Unexpected callback '+label+' '+key[1][:180]
                raise AssertionError(self.failure)
            entry = queue[0]; index = entry['order']; epoch = self.epochs[index]
            if not self.epoch_order or epoch != self.epoch_order[0]:
                self.failure = 'Callback crossed a pinned read/control epoch'
                raise AssertionError(self.failure)
            queue.popleft(); self.epoch_remaining[epoch] -= 1
            if not self.epoch_remaining[epoch]:
                self.epoch_order.popleft()
            self.taken.add(index); self.starts.append(index)
            self.trace.append({'kind': 'callback-start', 'index': index, 'label': label, 'args': pack(list(args))})
            future = asyncio.get_running_loop().create_future()
            self.pending[index] = (future, entry); self.drain()
            return await future
        return read

    def drain(self):
        while self.cursor < len(self.events):
            event = self.events[self.cursor]; index = event['index']
            if event['kind'] == 'callback-start':
                if index not in self.taken:
                    return
                self.cursor += 1
            else:
                if index not in self.pending:
                    return
                future, entry = self.pending.pop(index); self.cursor += 1; self.settled.add(index)
                self.trace.append({'kind': 'callback-end', 'index': index, 'outcome': entry['outcome']})
                if future.done():
                    self.failure = 'Callback cancelled before recorded settlement'; continue
                if entry['outcome'] == 'throw':
                    e = entry['error']
                    future.set_exception(TransportError(e['name'], e['message']))
                else:
                    future.set_result(revive(entry['value']))

    def now(self):
        if self.clock >= len(self.rec['clocks']):
            self.failure = 'Unexpected clock read'; raise AssertionError(self.failure)
        started, settled = self.clock_frontiers[self.clock]
        if self.taken != started or self.settled != settled:
            self.failure = 'Clock crossed a callback phase boundary'; raise AssertionError(self.failure)
        value = self.rec['clocks'][self.clock]; self.clock += 1
        self.trace.append({'kind': 'clock', 'value': value})
        return value

    def record_io(self, event):
        index = len(self.io)
        if index >= len(self.io_frontiers):
            self.failure = 'Unexpected file access'; raise AssertionError(self.failure)
        started, settled, clock = self.io_frontiers[index]
        if self.taken != started or self.settled != settled or self.clock != clock:
            self.failure = 'File access crossed a callback/clock phase boundary'
            raise AssertionError(self.failure)
        self.io.append(event)
        self.trace.append(event)

    async def finish(self):
        for _ in range(100):
            if self.cursor == len(self.events) and not self.pending:
                break
            await asyncio.sleep(0)
        if self.failure:
            raise AssertionError(self.failure)
        if self.cursor != len(self.events) or len(self.taken) != len(self.rec['callbacks']):
            raise AssertionError('Incomplete callback transcript')
        if self.clock != len(self.rec['clocks']):
            raise AssertionError('Clock coverage mismatch')


def context(args):
    return inputs.ReleaseContext(args[0], args[1], args[2] if len(args) > 2 else None)


async def invoke(rec, args, tape):
    module, name = rec['function'].rsplit('.', 1)
    if module == 'index.ts':
        flags, rpc, trace = args
        def factory(url, simulation=False, local_only=False):
            trace['factories'].append({'url': url, 'simulation': simulation, 'localOnly': local_only})
            ReleaseRpc(url, simulation, local_only)
            return rpc
        value = await cli.run_release(name.removeprefix('release:'), release_config=flags['releaseConfig'], review_hash=flags['reviewHash'], rpc_url=flags['rpc'], checkpoint=flags['checkpoint'], checkpoint_review=flags['checkpointReview'], checkpoint_review_hash=flags['checkpointReviewHash'], rpc_factory=factory, clock_ms=tape.now)
        text = pretty_json(value)+'\n'; trace['stdout'].append(text)
        return {'stdout': ''.join(trace['stdout'])}
    functions = {
        'core.mjs': {'verifyRuntime': inputs.verify_runtime, 'validateInventory': inputs.validate_inventory, 'validateConfig': inputs.validate_config, 'validateRecordedTopology': inputs.validate_recorded_topology, 'preflight': inputs.preflight, 'operation': plans.operation, 'verifyOperation': plans.verify_operation, 'roleDigest': plans.role_digest, 'coreExpectation': plans.core_expectation, 'requireP1Complete': plans.require_p1_complete, 'safeBuilder': plans.safe_builder, 'deploymentDefinitions': plans.deployment_definitions},
        'artifacts.mjs': {'freezeArtifact': inputs.freeze_artifact},
        'history.mjs': {'authenticateHistory': history.authenticate_history, 'historyTargets': history.history_targets},
        'timelock-history.mjs': {'replayOperations': timelock.replay_operations, 'checkOperationPlan': timelock.check_operation_plan, 'requireActionBearingCoverage': timelock.require_action_bearing_coverage},
        'pool-replay.mjs': {'replayPool': economics.replay_pool},
        'residual-replay.mjs': {'replayResidual': residual.replay_residual, 'termsHash': residual.terms_hash},
    }
    if module == 'core.mjs' and name in ('buildU', 'buildHandoff', 'p1Runbook', 'testnetHandoff', 'expectations'):
        fn = {'buildU': plans.build_u, 'buildHandoff': plans.build_handoff, 'p1Runbook': plans.p1_runbook, 'testnetHandoff': plans.testnet_handoff, 'expectations': plans.expectations}[name]
        return fn(context(args), *args[3:])
    if module == 'read-only.mjs':
        if name == 'recheckObserved':
            return await checkpoint.recheck_observed(inputs.ReleaseContext(args[0], args[1]), *args[2:], clock_ms=tape.now)
        if name == 'verifyCheckpoint':
            return await checkpoint.verify_checkpoint(context(args), *args[3:], clock_ms=tape.now)
        if name == 'reviewedContext':
            return await checkpoint.reviewed_context(*args, clock_ms=tape.now)
        fn = {'scanRoles': checkpoint.scan_roles, 'scanEconomics': economics.scan_economics, 'estimateGas': checkpoint.estimate_gas}[name]
    else:
        fn = functions[module][name]
    value = fn(*args)
    return await value if inspect.isawaitable(value) else value


async def run_case(rec):
    prepare(rec); tape = Tape(rec)
    args = [tape.callback(x['$callback']) if isinstance(x, dict) and '$callback' in x else revive(x) for x in rec['arguments']]
    original_lstat, original_read = os.lstat, Path.read_bytes
    def lstat(path, *a, **kw):
        try:
            value = original_lstat(path, *a, **kw)
            tape.record_io({'kind': 'lstat', 'path': str(path), 'type': 'symlink' if stat.S_ISLNK(value.st_mode) else 'directory' if stat.S_ISDIR(value.st_mode) else 'file'})
            return value
        except OSError as error:
            error.release_syscall = 'lstat'; tape.record_io({'kind': 'lstat', 'path': str(path), 'error': error_value(error)}); raise
    def read(path):
        raw = original_read(path)
        if str(path).startswith(str(INPUT_ROOT)+'/'):
            tape.record_io({'kind': 'read', 'path': str(path), 'sha256': sha(raw)})
        return raw
    with patch.object(os, 'lstat', lstat), patch.object(Path, 'read_bytes', read):
        try:
            value = await asyncio.wait_for(invoke(rec, args, tape), 30)
            actual = {'outcome': 'return', 'value': pack(value)}
        except Exception as error:
            actual = {'outcome': 'throw', 'error': error_value(error)}
        transcript_error = None
        try:
            await tape.finish()
        except Exception as error:
            transcript_error = str(error)
    expected = {'outcome': rec['outcome'], 'value': rec['value']} if rec['outcome'] == 'return' else {'outcome': 'throw', 'error': rec['error']}
    expected_io = [e for e in rec['events'] if e['kind'] in ('read', 'lstat')]
    after = [({'$function': True} if isinstance(x, dict) and '$callback' in x else x) for x in rec['argumentsAfter']]
    if expected.get('error', {}).get('code') == 'ENOENT':
        outcome_match = canonical(normalize_io(actual)) == canonical(normalize_io(expected))
    else:
        # Successful values, calldata, hashes and source paths are never normalized.
        outcome_match = canonical(actual) == canonical(expected)
    io_match = canonical(normalize_io(tape.io)) == canonical(normalize_io(expected_io))
    mutation_match = canonical(pack(args)) == canonical(after)
    return {'id': rec['id'], 'function': rec['function'], 'test': rec['test'], 'pass': outcome_match and io_match and mutation_match and not transcript_error,
            'outcomeMatch': outcome_match, 'ioMatch': io_match, 'argumentMutationMatch': mutation_match,
            'callbacks': len(tape.taken), 'expectedCallbacks': len(rec['callbacks']), 'clockReads': tape.clock,
            'sideEffectTrace': tape.trace,
            'transcriptError': transcript_error, 'actual': actual, **({} if io_match else {'actualIo': tape.io, 'expectedIo': expected_io})}


async def compare(filter_text=None):
    result = []
    for rec in records():
        if filter_text and filter_text not in rec['id']:
            continue
        row = await run_case(rec); result.append(row)
        if not row['pass']:
            print(json.dumps({k: row[k] for k in ('id', 'function', 'outcomeMatch', 'ioMatch', 'argumentMutationMatch', 'transcriptError')}), flush=True)
        if len(result) % 25 == 0:
            print('Compared', len(result), 'cases', flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=['verify', 'compare', 'test']); ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--reference-proof', type=Path); ap.add_argument('--filter')
    args = ap.parse_args()
    if args.mode == 'test' and args.filter:
        raise ValueError('The integrated gate requires every frozen case')
    if not (ROOT/'EXPORT-MANIFEST.txt').is_file():
        raise ValueError('Approved export required')
    for part in args.out.parts:
        if part.lower().startswith('.env') or any(word in part.lower() for word in ('credential', 'keystore', 'secret', 'private_key', 'mnemonic')):
            raise ValueError('Unsafe evidence path')
    for path in [args.out, *args.out.parents]:
        if path.is_symlink():
            raise ValueError('Symlink evidence path')
    if args.out.exists():
        raise ValueError('Refusing existing evidence')
    lock, index = verify_corpus()
    if args.mode == 'verify':
        report = {'status': 'PASS', 'cases': len(index), 'lockSha256': LOCK_SHA256}
    else:
        if not args.reference_proof:
            raise ValueError('Complete original reference replay is required first')
        proof = j.checked_json(str(args.reference_proof))
        if proof.get('status') != 'PASS' or proof.get('scope') != 'complete' or proof.get('cases') != lock['caseCount'] or proof.get('referenceLockSha256') != LOCK_SHA256:
            raise ValueError('Original reference replay is incomplete or mismatched')
        if proof.get('runnerSha256') != sha((HERE/'reference/replay.mjs').read_bytes()) or proof.get('taskAdapterSha256') != sha((HERE/'reference/task-adapter.mjs').read_bytes()):
            raise ValueError('Original reference runner identity differs')
        units = None
        if args.mode == 'test':
            units = strict_tests()
        results = asyncio.run(compare(args.filter))
        passed = all(x['pass'] for x in results) and (bool(args.filter) or len(results) == len(index))
        report = {'status': 'PASS' if passed else 'FAIL', 'scope': 'subset' if args.filter else 'complete', 'cases': len(results), 'matches': sum(bool(x['pass']) for x in results), 'mismatches': sum(not x['pass'] for x in results), 'lockSha256': LOCK_SHA256, 'referenceProofSha256': sha(args.reference_proof.read_bytes()), 'results': results}
        if units is not None:
            report['focusedTests'] = units
            if units['status'] != 'PASS':
                report['status'] = 'FAIL'
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, indent=2, ensure_ascii=True)+'\n').encode()
    args.out.write_bytes(gzip.compress(raw, mtime=0) if args.out.suffix == '.gz' else raw)
    print(report['status'], report['cases'], 'cases')
    return 0 if report['status'] == 'PASS' else 1


def strict_tests():
    import unittest
    sys.modules.setdefault('gate', sys.modules[__name__])
    import test_release
    required = {
        'test_frozen_corpus_and_all_required_actions',
        'test_parser_rejects_unapproved_authority_before_input_io',
        'test_missing_approval_fails_before_file_and_rpc_access',
        'test_standalone_python_cli_exact_stdout',
        'test_control_epoch_and_clock_boundaries',
        'test_changed_corpus_lock_is_rejected',
        'test_wrong_rule_AUTHORITY_SURFACE',
        *('test_wrong_rule_'+name.replace('-', '_') for name in test_release.CONTROL_CASES),
    }
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_release.NativeReleaseTests)
    found = [test.id().rsplit('.', 1)[-1] for test in suite]
    if len(found) != len(required) or set(found) != required:
        raise ValueError('Required Release regression missing or duplicated')
    test_release.CONTROL_RESULTS.clear()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    controls = test_release.CONTROL_RESULTS
    families = [row['family'] for row in controls]
    passed = result.wasSuccessful() and not result.skipped and not result.expectedFailures
    passed = passed and len(families) == len(test_release.REQUIRED_CONTROL_FAMILIES) and set(families) == test_release.REQUIRED_CONTROL_FAMILIES
    return {'status': 'PASS' if passed else 'FAIL', 'testsRun': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'expectedFailures': len(result.expectedFailures),
            'requiredTests': sorted(required), 'wrongRuleControls': controls}


if __name__ == '__main__':
    raise SystemExit(main())
