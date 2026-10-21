"""Private upstream of a real-state Fork (schema ``faroo-fork-execution-authorization-v4-private``). Test-only.

The owner's private endpoint is the caller's ``PHAROS_RPC_URL``. It is never part of a record, a log, a command line or
a child environment:

- ``tools/verify/run_private_rpc.py`` (outside the export) reads the variable in memory and hands its bytes to the
  export process through an inherited pipe; only the descriptor number (``FAROO_PRIVATE_RPC_FD``) passes through
  ``tools/verify/run.sh``, whose empty-environment rule is unchanged;
- ``take`` reads that pipe once, closes it and removes the descriptor variable; there is no fallback endpoint;
- ``validate`` admits an https URL with a DNS host (no IP literal, no ``localhost``), port 443, no user info and no
  fragment; anything else is refused by category only;
- the record binds ``commitment(url)`` (domain-separated SHA-256), so a run uses exactly the endpoint whose commitment
  was reviewed; the URL itself is never written;
- ``PrivateUpstream.post`` sends one POST over a reused TLS connection (dropped on any failure) with no environment
  proxy and no redirect, and maps every failure to a
  ``PrivateRpcError`` whose text is a fixed category (never the URL, a host name, a response body or an exception
  message). ``__repr__`` is redacted and the object cannot be pickled.

Retries are not here: ``fork_mode.ForkBudgetProxy`` retries within ``TRANSPORT`` and reserves every attempt in the
budget ledger before it is sent.
"""
import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

VARIABLE = 'PHAROS_RPC_URL'
FD_VARIABLE = 'FAROO_PRIVATE_RPC_FD'
SCHEME = 'https'
PORTS = (None, 443)
MAX_URL_BYTES = 2048
COMMITMENT_RULE = 'sha256("faroo-private-rpc-v1" || 0x00 || utf8(url))'
_DOMAIN = b'faroo-private-rpc-v1\x00'
# Bounded retry of one proxied POST: every attempt is budgeted before it is sent; the whole POST ends within
# ``deadlineS`` (below anvil's and the release reader's 30 s request limits).
TRANSPORT = {'attempts': 3, 'attemptTimeoutS': 8, 'deadlineS': 20, 'backoffS': [0.5, 1.0],
             'retryOn': ['TIMEOUT', 'CONNECTION_RESET', 'TLS', 'HTTP_429', 'HTTP_502', 'HTTP_503', 'HTTP_504']}
RETRY_HTTP = (429, 502, 503, 504)
CATEGORIES = ('MISSING', 'URL_FORM', 'SCHEME', 'USERINFO', 'HOST', 'PORT', 'FRAGMENT', 'COMMITMENT', 'TIMEOUT',
              'CONNECTION_RESET', 'TLS', 'DNS', 'REDIRECT', 'NETWORK', 'UNEXPECTED')
RETRYABLE = frozenset({'TIMEOUT', 'CONNECTION_RESET', 'TLS'})


class PrivateRpcError(Exception):
    """A redacted failure: only ``category`` (one of ``CATEGORIES``) and a fixed text."""

    def __init__(self, category, note=''):
        assert category in CATEGORIES, category
        self.category, self.retryable = category, category in RETRYABLE
        super().__init__(f'PRIVATE_RPC_{category}' + (f': {note}' if note else ''))


def commitment(url):
    return hashlib.sha256(_DOMAIN + url.encode()).hexdigest()


def validate(url):
    """Refuses (by category) anything but scheme https, a DNS host, port 443, a path and an optional query."""
    if not isinstance(url, str) or not url or len(url.encode()) > MAX_URL_BYTES or any(c.isspace() or ord(c) < 32
                                                                                        for c in url):
        raise PrivateRpcError('URL_FORM')
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise PrivateRpcError('URL_FORM') from None
    if parts.scheme != SCHEME:
        raise PrivateRpcError('SCHEME')
    if parts.username is not None or parts.password is not None or '@' in parts.netloc:
        raise PrivateRpcError('USERINFO')
    if parts.fragment or url.endswith('#'):
        raise PrivateRpcError('FRAGMENT')
    if port not in PORTS:
        raise PrivateRpcError('PORT')
    host = (parts.hostname or '').rstrip('.')
    if not host or host == 'localhost' or host.endswith('.localhost') or '.' not in host:
        raise PrivateRpcError('HOST')
    try:
        ipaddress.ip_address(host)
        raise PrivateRpcError('HOST')                   # IP literals are refused: a DNS name only
    except ValueError:
        pass
    return url


def take(environ=None):
    """The URL handed over by ``tools/verify/run_private_rpc.py``: read once from the inherited pipe, which is then
    closed. No pipe, no URL: ``PrivateRpcError('MISSING')`` (no fallback endpoint)."""
    environ = os.environ if environ is None else environ
    fd = environ.pop(FD_VARIABLE, None)
    environ.pop(VARIABLE, None)                         # never inherited by anything this process starts
    if fd is None or not fd.isdigit():
        raise PrivateRpcError('MISSING', f'{VARIABLE} was not delivered (run through tools/verify/run_private_rpc.py)')
    chunks = []
    try:
        while True:
            chunk = os.read(int(fd), 4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > MAX_URL_BYTES:
                raise PrivateRpcError('URL_FORM')
    except OSError:
        raise PrivateRpcError('MISSING', 'the delivery descriptor is not readable') from None
    finally:
        try:
            os.close(int(fd))
        except OSError:
            pass
    try:
        return b''.join(chunks).decode()
    except UnicodeDecodeError:
        raise PrivateRpcError('URL_FORM') from None


def classify(error):
    """A fixed category for a transport failure; the exception text is never used."""
    if isinstance(error, PrivateRpcError):
        return error.category
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'TIMEOUT'
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                           http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return 'CONNECTION_RESET'
    if isinstance(reason, ssl.SSLError):
        return 'TLS'
    if isinstance(reason, socket.gaierror):
        return 'DNS'
    if isinstance(reason, (OSError, http.client.HTTPException)):
        return 'NETWORK'
    return 'UNEXPECTED'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PrivateRpcError('REDIRECT')


def _https_connection(host, port, timeout_s):
    return http.client.HTTPSConnection(host, port, timeout=timeout_s, context=ssl.create_default_context())


def _keepalive_sender(url, factory):
    """POSTs over one reused TLS connection (no environment proxy, redirects never followed). Any failure drops the
    connection; the next attempt opens a new one."""
    parts = urllib.parse.urlsplit(url)
    target = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    host, port = parts.hostname, parts.port or 443
    state = {'conn': None}

    def close():
        conn, state['conn'] = state['conn'], None
        if conn is not None:
            try:
                conn.close()
            except Exception:                   # noqa: BLE001 - closing is best effort
                pass

    def send(raw, timeout_s):
        try:
            if state['conn'] is None:
                state['conn'] = factory(host, port, timeout_s)
            conn = state['conn']
            conn.timeout = timeout_s
            if getattr(conn, 'sock', None) is not None:
                conn.sock.settimeout(timeout_s)
            conn.request('POST', target, body=raw, headers={'content-type': 'application/json'})
            response = conn.getresponse()
            status, body = response.status, response.read()
            if response.will_close:
                close()
        except Exception as error:              # noqa: BLE001 - every failure leaves as a category only
            close()
            raise PrivateRpcError(classify(error)) from None
        if 300 <= status < 400:
            close()
            raise PrivateRpcError('REDIRECT')
        return status, body
    return send, close


class PrivateUpstream:
    """One reviewed private endpoint, held in a closure. ``post(raw, timeout_s)`` -> (status, body bytes). By default one
    TLS connection is reused (``_keepalive_sender``); ``opener`` (tests) selects the urllib path instead."""

    def __init__(self, url, expected_commitment, *, opener=None, connection=None, clock_s=time.monotonic):
        validate(url)
        if commitment(url) != expected_commitment:
            raise PrivateRpcError('COMMITMENT', 'the delivered endpoint is not the committed one')
        self.commitment = expected_commitment
        self.clock = clock_s
        self._close = lambda: None
        if opener is None:
            self._send, self._close = _keepalive_sender(url, connection or _https_connection)
            return
        build = opener

        def send(raw, timeout_s):
            request = urllib.request.Request(url, data=raw, headers={'content-type': 'application/json'}, method='POST')
            try:
                with build().open(request, timeout=timeout_s) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                try:
                    if 300 <= status < 400:
                        raise PrivateRpcError('REDIRECT') from None
                    try:
                        body = error.read()
                    except Exception:           # noqa: BLE001 - the body of a failed answer is optional
                        body = b''
                    return status, body
                finally:
                    error.close()               # an unclosed error object would be reported (with its repr) at exit
            except PrivateRpcError:
                raise
            except Exception as error:          # noqa: BLE001 - every failure leaves as a category only
                raise PrivateRpcError(classify(error)) from None
        self._send = send

    def post(self, raw, timeout_s):
        return self._send(raw, timeout_s)

    def close(self):
        self._close()

    def __repr__(self):
        return f'<PrivateUpstream {VARIABLE} commitment={self.commitment[:12]}… (redacted)>'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('a private upstream is not serializable')


def resolve(expected_commitment, *, environ=None):
    """The committed private upstream of this process (``take`` + ``validate`` + commitment check)."""
    return PrivateUpstream(take(environ), expected_commitment)
