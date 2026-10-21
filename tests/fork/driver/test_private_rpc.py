"""Private upstream of a real-state Fork (schema v4-private): delivery, URL policy, redaction, record checks, the proxy's
budgeted bounded retry. Test-only; no network: every URL and key here is fictitious and every upstream is a fake.

The launcher round trip runs ``tools/verify/run_private_rpc.py`` -> ``tools/verify/run.sh`` against a scratch export
(manifest only) with a fake ``PHAROS_RPC_URL``; the real variable is never read by these tests.
"""
import copy
import http.client
import io
import json
import os
from pathlib import Path
import pickle
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import fork_mode as fm  # noqa: E402
import fr_d1_approvals as approvals  # noqa: E402
import fr_d1_scope as scope  # noqa: E402
import private_rpc as prpc  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402

MARK = 'FAKEKEY-7c1d0e5b'                                   # must never appear in any output
URL = 'https' + '://rpc.example.invalid/v1/' + MARK
OTHER = 'https' + '://rpc.example.invalid/v1/OTHERKEY'
BLOCK, BLOCK_HASH = 20_788_614, '0x' + '2b' * 32
TX = '0x' + 'ab' * 32
READS = [{'method': 'eth_getBlockByNumber', 'params': ['0x5', True], 'expect': {'hash': '0x' + 'cd' * 32}, 'purpose': 'pin'},
         {'method': 'eth_getCode', 'params': ['0x' + '9c' * 20, '0x4'], 'expect': {'code': '0x'}, 'purpose': 'absence'}]
VALIDITY = {'notBeforeUtc': '2026-10-01T00:00:00Z', 'notAfterUtc': '2026-12-31T00:00:00Z', 'stopConditions': ['test']}


def private_record(mode=scope.PRIVATE_DIAGNOSTIC, ident='diag-s3-mainnet-test1', commitment=None, reads=READS):
    return scope.private_fork_record(
        'mainnet', mode=mode, ident=ident, commitment=commitment or prpc.commitment(URL), ceiling=15000, restarts=3,
        s1_sha256='1' * 64, s2_sha256='2' * 64, validity=VALIDITY, retention='test', owner={'decision': 'test'},
        prerequisites=['test'], statement='private upstream test record', bases=('0' * 40, '0' * 40),
        historical_reads=copy.deepcopy(reads))


def reviewed(reads=READS):
    return mock.patch.dict(scope.REVIEWED_HISTORICAL_READS, {'mainnet': scope.historical_reads_digest(reads)})


class Refusals(unittest.TestCase):
    def refused(self, fn, code):
        with self.assertRaises(CollectionStop) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        self.assertNotIn(MARK, str(ctx.exception))
        return ctx.exception

    def category(self, fn, category):
        with self.assertRaises(prpc.PrivateRpcError) as ctx:
            fn()
        self.assertEqual(ctx.exception.category, category)
        self.assertNotIn(MARK, str(ctx.exception))
        self.assertNotIn('example.invalid', str(ctx.exception))


class UrlPolicyTest(Refusals):
    def test_policy(self):
        prpc.validate(URL)
        h = 'https' + '://'
        for url, category in ((None, 'URL_FORM'), ('', 'URL_FORM'), (URL + ' x', 'URL_FORM'), (URL + '\n', 'URL_FORM'),
                              ('http' + '://rpc.example.invalid/' + MARK, 'SCHEME'), ('wss://rpc.example.invalid/', 'SCHEME'),
                              (h + 'user:' + MARK + '@rpc.example.invalid/', 'USERINFO'),
                              (h + 'rpc.example.invalid/' + MARK + '#x', 'FRAGMENT'),
                              (h + 'rpc.example.invalid:8545/' + MARK, 'PORT'),
                              (h + 'localhost/' + MARK, 'HOST'), (h + '127.0.0.1/' + MARK, 'HOST'),
                              (h + '10.1.2.3/' + MARK, 'HOST'), (h + '[::1]/' + MARK, 'HOST'), (h + 'rpc/' + MARK, 'HOST'),
                              (h + '8.8.8.8/' + MARK, 'HOST'), ('x' * 3000, 'URL_FORM')):
            self.category(lambda: prpc.validate(url), category)

    def test_commitment_is_domain_separated_and_never_the_url(self):
        c = prpc.commitment(URL)
        self.assertEqual(len(c), 64)
        self.assertNotEqual(c, __import__('hashlib').sha256(URL.encode()).hexdigest())
        self.assertNotEqual(c, prpc.commitment(OTHER))


class DeliveryTest(Refusals):
    def test_no_delivery_is_refused_without_fallback(self):
        env = {prpc.VARIABLE: URL}                                  # a bare variable is not a delivery
        self.category(lambda: prpc.take(env), 'MISSING')
        self.assertNotIn(prpc.VARIABLE, env)                        # and is removed from the environment anyway

    def test_pipe_is_read_once_and_closed(self):
        r, w = os.pipe()
        os.write(w, URL.encode())
        os.close(w)
        env = {prpc.FD_VARIABLE: str(r)}
        self.assertEqual(prpc.take(env), URL)
        self.assertNotIn(prpc.FD_VARIABLE, env)
        with self.assertRaises(OSError):
            os.fstat(r)

    @staticmethod
    def launcher():
        import importlib.util
        spec = importlib.util.spec_from_file_location('run_private_rpc', ROOT / 'tools/verify/run_private_rpc.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_launcher_hands_over_a_pipe_never_the_url(self):
        """The launcher execs run.sh with the descriptor number only; the URL is in the pipe, not in argv or env."""
        mod = self.launcher()
        with mock.patch.dict(os.environ, {prpc.VARIABLE: URL}), mock.patch.object(mod.os, 'execve') as execve:
            mod.main(['run_private_rpc.py', '/Volumes/project/tmp/x/export', 'python3', '-c', 'pass'])
        path, argv, env = execve.call_args[0]
        self.assertEqual((path, argv[0], Path(argv[1]).name, argv[2]), ('/bin/bash', 'bash', 'run.sh', '/Volumes/project/tmp/x/export'))
        self.assertEqual(argv[4:], ['python3', '-c', 'pass'])
        self.assertNotIn(MARK, json.dumps([argv, env]))
        self.assertLessEqual(set(env), {'PATH', 'HOME', 'TERM'})
        name, fd = argv[3].split('=')
        self.assertEqual(name, prpc.FD_VARIABLE)
        fd = int(fd)
        self.assertTrue(os.get_inheritable(fd))
        self.assertEqual(prpc.take({prpc.FD_VARIABLE: str(fd)}), URL)

    def test_the_descriptor_survives_an_empty_environment_exec(self):
        """run.sh ends in ``exec env -i ...``: an inherited pipe descriptor stays readable for the command."""
        r, w = os.pipe()
        os.write(w, URL.encode())
        os.close(w)
        child = (f'import os,sys,json; sys.path.insert(0, {str(HERE)!r}); import private_rpc as p; '
                 'url=p.take(); print(json.dumps({"c": p.commitment(url), "env": sorted(os.environ)}))')
        out = subprocess.run(['/bin/bash', '-c', f'exec env -i PATH="$PATH" {prpc.FD_VARIABLE}={r} python3 -c "$1"', 'bash', child],
                             pass_fds=(r,), env={'PATH': os.environ.get('PATH', '')}, capture_output=True, text=True,
                             timeout=60)
        os.close(r)
        self.assertEqual(out.returncode, 0, out.stderr[-1000:].replace(MARK, '<MARK>'))
        doc = json.loads(out.stdout)
        self.assertEqual(doc['c'], prpc.commitment(URL))
        self.assertNotIn(prpc.VARIABLE, doc['env'])
        self.assertNotIn(MARK, out.stdout + out.stderr)

    def test_launcher_refuses_without_the_variable_or_with_it_as_an_argument(self):
        launcher = ROOT / 'tools/verify/run_private_rpc.py'
        base = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '')}
        out = subprocess.run([sys.executable, str(launcher), '/Volumes/project/tmp/x', 'true'], env=base,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 2)
        self.assertIn('no fallback endpoint', out.stderr)
        out = subprocess.run([sys.executable, str(launcher), '/Volumes/project/tmp/x', f'{prpc.VARIABLE}={URL}', 'true'],
                             env={**base, prpc.VARIABLE: URL}, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 2)
        self.assertNotIn(MARK, out.stdout + out.stderr)


class _Opener:
    def __init__(self, error=None, status=200, body=b'{}'):
        self.error, self.status, self.body, self.requests = error, status, body, []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout, request.get_method()))
        if self.error is not None:
            raise self.error
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, f'status {request.full_url}', {}, io.BytesIO(self.body))
        response = mock.MagicMock()
        response.status, response.read.return_value = 200, self.body
        response.__enter__.return_value = response
        return response


class UpstreamTest(Refusals):
    def upstream(self, opener, url=URL, commitment=None):
        return prpc.PrivateUpstream(url, commitment or prpc.commitment(URL), opener=lambda: opener)

    def test_commitment_mismatch_is_refused(self):
        self.category(lambda: prpc.PrivateUpstream(OTHER, prpc.commitment(URL)), 'COMMITMENT')

    def test_classification_and_redaction(self):
        cases = ((urllib.error.URLError(TimeoutError(f'timed out {URL}')), 'TIMEOUT'),
                 (urllib.error.URLError(socket.timeout()), 'TIMEOUT'),
                 (urllib.error.URLError(ConnectionResetError(54, f'reset {URL}')), 'CONNECTION_RESET'),
                 (http.client.RemoteDisconnected(URL), 'CONNECTION_RESET'),
                 (urllib.error.URLError(ssl.SSLError(1, f'handshake {URL}')), 'TLS'),
                 (urllib.error.URLError(socket.gaierror(8, URL)), 'DNS'),
                 (urllib.error.URLError(OSError(f'other {URL}')), 'NETWORK'),
                 (RuntimeError(f'boom {URL}'), 'UNEXPECTED'))
        for error, category in cases:
            self.category(lambda: self.upstream(_Opener(error)).post(b'{}', 5), category)

    def test_redirect_and_http_errors(self):
        import gc
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.category(lambda: self.upstream(_Opener(status=302)).post(b'{}', 5), 'REDIRECT')
            self.assertEqual(self.upstream(_Opener(status=429, body=b'busy')).post(b'{}', 5), (429, b'busy'))
            gc.collect()
        self.assertFalse([str(w.message) for w in caught if MARK in str(w.message)])   # error objects are closed

    def test_post_uses_the_committed_url_and_is_redacted(self):
        opener = _Opener(body=b'{"ok":1}')
        up = self.upstream(opener)
        self.assertEqual(up.post(b'{"x":1}', 7), (200, b'{"ok":1}'))
        self.assertEqual(opener.requests, [(URL, 7, 'POST')])
        self.assertNotIn(MARK, repr(up) + str(up))
        self.assertFalse(any(MARK in str(v) for v in vars(up).values()))
        with self.assertRaises(TypeError):
            pickle.dumps(up)


class _Connection:
    """A fake ``http.client.HTTPSConnection``: scripted answers, records requests."""
    made = []

    def __init__(self, host, port, timeout, script):
        self.host, self.port, self.timeout, self.script, self.sock, self.closed, self.requests = host, port, timeout, script, None, False, []
        _Connection.made.append(self)

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, self.timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.pending = step

    def getresponse(self):
        status, body, will_close = self.pending
        r = mock.MagicMock()
        r.status, r.will_close, r.read.return_value = status, will_close, body
        return r

    def close(self):
        self.closed = True


class KeepAliveTest(Refusals):
    def upstream(self, script):
        _Connection.made = []
        return prpc.PrivateUpstream(URL + '?k=1', prpc.commitment(URL + '?k=1'),
                                    connection=lambda h, p, t: _Connection(h, p, t, script))

    def test_one_connection_is_reused(self):
        up = self.upstream([(200, b'a', False), (200, b'b', False)])
        self.assertEqual([up.post(b'1', 5), up.post(b'2', 7)], [(200, b'a'), (200, b'b')])
        self.assertEqual(len(_Connection.made), 1)
        conn = _Connection.made[0]
        self.assertEqual((conn.host, conn.port), ('rpc.example.invalid', 443))
        self.assertEqual([r[:2] + (r[3],) for r in conn.requests], [('POST', f'/v1/{MARK}?k=1', 5), ('POST', f'/v1/{MARK}?k=1', 7)])
        up.close()
        self.assertTrue(conn.closed)

    def test_failure_drops_the_connection_and_the_next_attempt_reconnects(self):
        script = [ConnectionResetError(54, f'reset {URL}'), (200, b'ok', False)]
        up = self.upstream(script)
        self.category(lambda: up.post(b'1', 5), 'CONNECTION_RESET')
        self.assertTrue(_Connection.made[0].closed)
        self.assertEqual(up.post(b'2', 5), (200, b'ok'))
        self.assertEqual(len(_Connection.made), 2)

    def test_server_close_and_redirect(self):
        up = self.upstream([(200, b'a', True), (302, b'', False)])
        self.assertEqual(up.post(b'1', 5), (200, b'a'))
        self.assertTrue(_Connection.made[0].closed)                   # will_close: the next POST reconnects
        self.category(lambda: up.post(b'2', 5), 'REDIRECT')
        self.assertEqual(len(_Connection.made), 2)
        self.assertTrue(_Connection.made[1].closed)
        self.category(lambda: self.upstream([TimeoutError(URL)]).post(b'3', 5), 'TIMEOUT')
        self.category(lambda: self.upstream([ssl.SSLError(1, URL)]).post(b'3', 5), 'TLS')


class RecordTest(Refusals):
    def test_builder_record_passes_and_widenings_are_refused(self):
        with reviewed():
            doc = private_record()
            scope.check_private_fork_authorization(doc)
            self.assertEqual(doc['endpoint'], scope.PRIVATE_ENDPOINT)
            self.assertNotIn(MARK, json.dumps(doc))
            mutations = {
                'AUTHORIZATION_KEYS': [lambda d: d.__setitem__('chain', 'testnet'), lambda d: d.__setitem__('mode', 'LOCAL_FORK_PUBLIC_READ_ONLY'),
                                       lambda d: d['scope'].pop('privateEndpoint')],
                'AUTHORIZATION_ID': [lambda d: d.__setitem__('authorizationId', 'fr-d1-s3-mainnet-test1')],
                'AUTHORIZATION_CEILING': [lambda d: d.__setitem__('maxHttpPosts', 20001)],
                'AUTHORIZATION_RESTARTS': [lambda d: d.__setitem__('maxRestarts', 4)],
                'AUTHORIZATION_METHODS': [lambda d: d['methods'].append('eth_sendRawTransaction'),
                                          lambda d: d['methods'].append('eth_call')],
                'AUTHORIZATION_HISTORICAL_READS': [lambda d: d['scope']['historicalReads'].append(
                    {'method': 'eth_getCode', 'params': ['0x' + '9c' * 20, '0x3'], 'expect': {'code': '0x'}, 'purpose': 'extra'})],
                'FORK_ENDPOINT': [lambda d: (d.__setitem__('endpoint', 'https' + '://rpc.pharos.xyz/'),
                                             d['scope'].__setitem__('endpoint', 'https' + '://rpc.pharos.xyz/')),
                                  lambda d: (d.__setitem__('endpoint', URL), d['scope'].__setitem__('endpoint', URL)),
                                  lambda d: d['scope']['privateEndpoint'].__setitem__('commitment', 'x'),
                                  lambda d: d['scope']['privateEndpoint'].__setitem__('variable', 'OTHER_RPC_URL')],
                'AUTHORIZATION_TRANSPORT': [lambda d: d['scope']['transport'].__setitem__('upstreamRetries', 5),
                                            lambda d: d['scope']['transport'].__setitem__('deadlineS', 60),
                                            lambda d: d['scope']['transport'].__setitem__('redirects', 'FOLLOW'),
                                            lambda d: d.__setitem__('pacingMs', 10)],
                'AUTHORIZATION_PARAMS': [lambda d: d['scope']['paramPolicy'].__setitem__('block', 'ANY')],
                'AUTHORIZATION_SOURCE_BINDING': [lambda d: d['scope']['sourceBinding']['files'].__setitem__(
                    'tests/fork/driver/private_rpc.py', '0' * 64),
                    lambda d: d['scope']['sourceBinding']['files'].pop('tools/verify/run_private_rpc.py')],
            }
            for code, fns in mutations.items():
                for fn in fns:
                    d = copy.deepcopy(doc)
                    fn(d)
                    self.refused(lambda: scope.check_private_fork_authorization(d), code)
        self.refused(lambda: scope.check_private_fork_authorization(private_record()), 'AUTHORIZATION_HISTORICAL_READS')

    def test_loader_pins_by_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix='faroo-private-record-'))
        with reviewed():
            for mode, ident in ((scope.PRIVATE_DIAGNOSTIC, 'diag-s3-mainnet-test1'), (scope.PRIVATE_FORMAL, 'fr-d1-s3-mainnet-test1')):
                raw = scope.record_bytes(private_record(mode, ident))
                path = tmp / f'{ident}.json'
                path.write_bytes(raw)
                sha = scope.sha256(raw)
                self.refused(lambda: fm.load_authorization(path, sha, approved=frozenset(), diagnostics=frozenset()),
                             'FR_D1_NOT_AUTHORIZED')
                right = (dict(diagnostics=frozenset({sha}), approved=frozenset()) if mode == scope.PRIVATE_DIAGNOSTIC
                         else dict(approved=frozenset({sha}), diagnostics=frozenset()))
                self.assertEqual(fm.load_authorization(path, sha, **right)['sha256'], sha)
                swapped = {'approved': right['diagnostics'], 'diagnostics': right['approved']}
                self.refused(lambda: fm.load_authorization(path, sha, **swapped), 'FR_D1_NOT_AUTHORIZED')

    def test_public_records_keep_their_path(self):
        doc = scope.fork_record('mainnet', mode='LOCAL_FORK_PUBLIC_READ_ONLY', ident='fr-d1-s3-mainnet-pub1',
                                upstream=scope.endpoint('mainnet'), ceiling=100, restarts=1, s1_sha256='1' * 64,
                                s2_sha256='2' * 64, validity=VALIDITY, retention='t', owner={'d': 't'}, prerequisites=['t'],
                                statement='t', bases=('0' * 40, '0' * 40), historical_reads=READS)
        scope.check_fork_authorization(doc)
        self.assertEqual(doc['scope']['transport']['upstreamRetries'], 0)
        self.assertEqual(set(doc['scope']['sourceBinding']['files']), set(scope.SOURCE_BINDING_FILES))
        d = copy.deepcopy(doc)
        d['endpoint'] = d['scope']['endpoint'] = scope.PRIVATE_ENDPOINT
        self.refused(lambda: scope.check_fork_authorization(d), 'FORK_ENDPOINT')


class ProxyTest(Refusals):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='faroo-private-proxy-'))
        with reviewed():
            doc = private_record()
        self.sha = scope.sha256(scope.record_bytes(doc))
        self.auth = {**doc, 'sha256': self.sha, 'forkBlock': BLOCK, 'forkBlockHash': BLOCK_HASH,
                     'anchorBinding': {'reconciliationSha256': sorted(approvals.RECONCILIATIONS)[0]}}
        self.pins = mock.patch.object(fm, 'APPROVED_DIAGNOSTICS', frozenset({self.sha}))
        self.pins.start()
        self.addCleanup(self.pins.stop)
        self.clock = [0.0]

    def proxy(self, answers, ceiling=50, source=lambda: URL):
        auth = dict(self.auth, maxHttpPosts=ceiling)
        ledger = tp.BudgetLedger(self.tmp / f'l{len(list(self.tmp.iterdir()))}', auth, invocation='test')
        ledger.open()
        self.addCleanup(ledger.close)
        answers = list(answers)

        def forward(raw, timeout_s):
            self.timeouts.append(timeout_s)
            self.clock[0] += 0.1
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        self.timeouts = []
        p = fm.ForkBudgetProxy(auth, ledger, forward=forward, private_source=source,
                               clock_s=lambda: self.clock[0], sleep=lambda s: self.clock.__setitem__(0, self.clock[0] + s))
        self.addCleanup(p.server.server_close)
        return p, ledger

    @staticmethod
    def body(method, params, rid=1):
        return json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}).encode()

    def ok(self, result, rid=1):
        return 200, json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result}).encode()

    def test_reads_at_the_anchor_pass_and_are_budgeted(self):
        p, ledger = self.proxy([self.ok('0x688'), self.ok({'hash': BLOCK_HASH, 'number': hex(BLOCK)})])
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 200)
        self.assertEqual(p.handle(self.body('eth_getBlockByNumber', [hex(BLOCK), False]))[0], 200)
        self.assertEqual((ledger.used, p.attempts, p.forwarded), (2, 2, 2))

    def test_writes_other_blocks_and_unlisted_history_are_refused_before_budget(self):
        p, ledger = self.proxy([])
        for raw, code in ((self.body('eth_sendRawTransaction', ['0x00']), 'FORK_METHOD'),
                          (self.body('eth_call', [{}, hex(BLOCK)]), 'FORK_METHOD'),
                          (self.body('eth_getStorageAt', ['0x' + '11' * 20, '0x0', hex(BLOCK - 1)]), 'FORK_PARAMS'),
                          (self.body('eth_getCode', ['0x' + '9c' * 20, '0x3']), 'FORK_PARAMS'),
                          (self.body('eth_getTransactionByHash', [TX]), 'FORK_PARAMS')):
            status, out = p.handle(raw)
            self.assertEqual(status, 403)
            self.assertIn(code, out.decode())
        self.assertEqual((ledger.used, p.attempts), (0, 0))

    def test_anchor_or_chain_contradiction_stops(self):
        p, _ = self.proxy([self.ok('0x1')])
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 403)
        self.assertEqual(p.stopped.code, 'FORK_CHAIN_MISMATCH')
        p, _ = self.proxy([self.ok({'hash': '0x' + '00' * 32})])
        p.handle(self.body('eth_getBlockByNumber', [hex(BLOCK), False]))
        self.assertEqual(p.stopped.code, 'FORK_ANCHOR_MISMATCH')

    def test_retries_are_bounded_and_every_attempt_is_budgeted(self):
        p, ledger = self.proxy([prpc.PrivateRpcError('TIMEOUT'), prpc.PrivateRpcError('TLS'), self.ok('0x688')])
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 200)
        self.assertEqual((ledger.used, p.attempts), (3, 3))
        self.assertEqual([r['outcome'] for r in p.network], ['TIMEOUT', 'TLS', 'OK'])
        self.assertTrue(all(t <= prpc.TRANSPORT['attemptTimeoutS'] for t in self.timeouts))
        p, ledger = self.proxy([(429, b'busy'), self.ok('0x688')])
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 200)
        self.assertEqual(ledger.used, 2)

    def test_final_failure_is_categorized_and_redacted(self):
        p, ledger = self.proxy([ConnectionResetError(f'reset {URL}')] * 3)
        status, out = p.handle(self.body('eth_chainId', []))
        self.assertEqual(status, 502)
        self.assertIn('PRIVATE_RPC_CONNECTION_RESET', out.decode())
        self.assertEqual((ledger.used, p.forwarded, p.stopped), (3, 0, None))
        p2, ledger2 = self.proxy([RuntimeError(f'boom {URL}')])
        status, out = p2.handle(self.body('eth_chainId', []))
        self.assertEqual((status, ledger2.used), (502, 1))                     # not retryable: one attempt
        for proxy in (p, p2):
            text = json.dumps({'refused': proxy.refused, 'network': proxy.network}) + out.decode()
            self.assertNotIn(MARK, text)
            self.assertNotIn('example.invalid', text)

    def test_deadline_caps_the_retries(self):
        p, ledger = self.proxy([prpc.PrivateRpcError('TIMEOUT')] * 3)
        original = p.forward

        def slow(raw, timeout_s):
            self.clock[0] += timeout_s                  # each attempt uses its whole timeout
            return original(raw, timeout_s)
        p.forward = slow
        p.handle(self.body('eth_chainId', []))
        spent = sum(self.timeouts) + sum(prpc.TRANSPORT['backoffS'][:p.attempts - 1])
        self.assertLessEqual(spent, prpc.TRANSPORT['deadlineS'])
        self.assertEqual(ledger.used, p.attempts)

    def test_redirect_stops_the_session(self):
        p, ledger = self.proxy([prpc.PrivateRpcError('REDIRECT'), self.ok('0x688')])
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 502)
        self.assertEqual(p.stopped.code, 'PRIVATE_RPC_REDIRECT')
        self.assertEqual(p.handle(self.body('eth_chainId', []))[0], 403)
        self.assertEqual(ledger.used, 1)

    def test_budget_exhaustion_during_a_retry_stops(self):
        p, ledger = self.proxy([prpc.PrivateRpcError('TIMEOUT')] * 3, ceiling=2)
        status, _ = p.handle(self.body('eth_chainId', []))
        self.assertEqual((status, ledger.used), (429, 2))
        self.assertIsInstance(p.stopped, tp.BudgetExhausted)

    def test_construction_refusals(self):
        self.refused(lambda: self.proxy([], source=lambda: OTHER), 'PRIVATE_RPC_COMMITMENT')
        self.refused(lambda: self.proxy([], source=lambda: 'http' + '://rpc.example.invalid/' + MARK), 'PRIVATE_RPC_SCHEME')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(prpc.FD_VARIABLE, None)
            auth = dict(self.auth)
            ledger = tp.BudgetLedger(self.tmp / 'none', auth, invocation='t')
            self.refused(lambda: fm.ForkBudgetProxy(auth, ledger), 'PRIVATE_RPC_MISSING')
        with mock.patch.object(fm, 'APPROVED_DIAGNOSTICS', frozenset()):
            self.refused(lambda: self.proxy([]), 'FR_D1_NOT_AUTHORIZED')
        unbound = {k: v for k, v in self.auth.items() if k != 'anchorBinding'}
        self.refused(lambda: fm.ForkBudgetProxy(unbound, None, private_source=lambda: URL), 'FORK_ANCHOR_NOT_BOUND')


if __name__ == '__main__':
    unittest.main()
