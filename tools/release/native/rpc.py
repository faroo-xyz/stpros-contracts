"""Read-only release transport matching the pinned Node reader's policy.

No default endpoint, environment credentials, proxy discovery, signer or write RPC.
The archive Client is deliberately unchanged. Injectable transport/time are for
bounded offline tests; every request still passes the same policy and correlation.
"""
import asyncio
import json
import ipaddress
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ...lib.release_json import ReleaseError, truthy, parse_json, wire_json, number_text

METHODS = frozenset(('eth_chainId', 'eth_getBlockByNumber', 'eth_getCode', 'eth_getStorageAt',
                     'eth_call', 'eth_getLogs', 'eth_estimateGas', 'eth_getTransactionByHash'))
PUBLIC = frozenset(('https://rpc.pharos.xyz/', 'https://atlantic.dplabs-internal.com/'))
LOCAL = frozenset(('localhost', '127.0.0.1', '::1'))
RETRY_HTTP = frozenset((429, 500, 502, 503, 504))
RETRY_RPC = frozenset((-32011, -32603))
_MISSING = object()


class TransportError(Exception):
    def __init__(self, name, message):
        self.name = name
        super().__init__(message)


def _ipv4(host):
    parts = host.rstrip('.').split('.')
    last = parts[-1]
    if not (re.fullmatch(r'[0-9]+', last) or re.fullmatch(r'0[xX][0-9a-fA-F]*', last)):
        return host
    def number(part):
        if re.fullmatch(r'0[xX][a-fA-F0-9]*', part): return int(part[2:] or '0', 16)
        if not re.fullmatch(r'[0-9]+', part): raise ValueError()
        return int(part, 8) if len(part)>1 and part[0]=='0' else int(part, 10)
    try:
        nums = list(map(number, parts))
    except ValueError:
        raise TransportError('TypeError', 'Invalid URL')
    if not 1 <= len(nums) <= 4 or any(x > 255 for x in nums[:-1]) or not nums[-1] < 256**(5-len(nums)):
        raise TransportError('TypeError', 'Invalid URL')
    value = sum(x * 256**(3-i) for i,x in enumerate(nums[:-1])) + nums[-1]
    return '.'.join(str((value >> shift) & 255) for shift in (24,16,8,0))


def _endpoint(url, simulation, local_only):
    if not isinstance(url, str):
        raise TransportError('TypeError', 'Invalid URL')
    text = url.strip(''.join(chr(n) for n in range(33))).replace('\t','').replace('\r','').replace('\n','')
    m = re.match(r'^(https?):[\\/]*(.*)$', text, re.I)
    if not m:
        # Other syntactically valid schemes still fail the allowlist.
        if '://' in text:
            raise ReleaseError('RPC_NOT_ALLOWLISTED')
        raise TransportError('TypeError', 'Invalid URL')
    text = m[1].lower() + '://' + m[2].replace('\\','/')
    try:
        p = urllib.parse.urlsplit(text)
        port = p.port
        host = urllib.parse.unquote(p.hostname or '').lower()
        if not host or any(c.isspace() for c in host):
            raise ValueError()
    except ValueError:
        raise TransportError('TypeError', 'Invalid URL')
    if p.username or p.password:
        raise ReleaseError('RPC_CREDENTIALS_FORBIDDEN')
    if ':' not in host:
        host = _ipv4(host)
    else:
        try: host = ipaddress.IPv6Address(host).compressed
        except ipaddress.AddressValueError: raise TransportError('TypeError', 'Invalid URL')
    parts = []
    for item in (p.path or '/').split('/'):
        dot = re.sub('%2e', '.', item, flags=re.I)
        if dot == '..':
            if parts: parts.pop()
        elif dot != '.':
            parts.append(item)
    pathname = '/'.join(parts) or '/'
    if not pathname.startswith('/'): pathname = '/' + pathname
    authority = '[' + host + ']' if ':' in host else host
    if port is not None and port != (443 if p.scheme=='https' else 80):
        authority += ':' + str(port)
    href = p.scheme + '://' + authority + pathname
    if '?' in text.split('#')[0]: href += '?' + p.query
    if '#' in text: href += '#' + p.fragment
    public = href in PUBLIC
    local = simulation and host in LOCAL and pathname == '/' and not p.query and not p.fragment
    if not (public or local):
        raise ReleaseError('RPC_NOT_ALLOWLISTED')
    if local_only and not local:
        raise ReleaseError('LOCAL_RPC_REQUIRED')
    return public, href


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError('TypeError', 'fetch failed')


async def _post(url, body, timeout_ms):
    def send():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(url, data=wire_json(body).encode(),
                                         headers={'content-type':'application/json'}, method='POST')
        try:
            with opener.open(request, timeout=timeout_ms/1000) as response:
                return response.status, parse_json(response.read())
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise TransportError('TypeError', 'fetch failed')
            return error.code, None
        except (TimeoutError, asyncio.TimeoutError):
            raise TransportError('TimeoutError', 'The operation was aborted due to timeout')
        except urllib.error.URLError:
            raise TransportError('TypeError', 'fetch failed')
        except json.JSONDecodeError as error:
            raise TransportError('SyntaxError', str(error))
    return await asyncio.to_thread(send)


def _same_id(value, expected):
    return type(value) in (int,float) and value == expected


def _id_key(value):
    if isinstance(value, (dict,list)):
        return ('object', id(value))
    if type(value) in (int,float):
        return ('number', value)
    return (type(value), value)


def _bigint(value):
    # JSON arrays use JS Array.toString before BigInt conversion.
    if isinstance(value, list):
        def string(v):
            if v is None: return ''
            if isinstance(v, list): return ','.join(map(string, v))
            if isinstance(v, dict): return '[object Object]'
            if type(v) is bool: return 'true' if v else 'false'
            if isinstance(v, (int, float)):
                if isinstance(v, float) and not math.isfinite(v): return str(v).replace('inf','Infinity').replace('nan','NaN')
                return number_text(v)
            return v
        return _bigint(','.join(map(string, value)))
    if isinstance(value, dict):
        if 'toString' in value: raise TransportError('TypeError','Cannot convert object to primitive value')
        return _bigint('[object Object]')
    if isinstance(value, str):
        whitespace = '\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff' + ''.join(chr(n) for n in range(0x2000, 0x200b))
        text = value.strip(whitespace)
        if not text: return 0
        if re.fullmatch(r'[+-]?[0-9]+', text): return int(text)
        for prefix, digits, base in [('x','a-fA-F0-9',16),('o','0-7',8),('b','01',2)]:
            if re.fullmatch('0['+prefix+prefix.upper()+']['+digits+']+',text): return int(text[2:],base)
        raise TransportError('SyntaxError', 'Cannot convert '+value+' to a BigInt')
    if value is None: raise TransportError('TypeError','Cannot convert null to a BigInt')
    if isinstance(value,float) and (not math.isfinite(value) or value != int(value)):
        raise TransportError('RangeError','The number cannot be converted to a BigInt because it is not an integer')
    return int(value)


class ReleaseRpc:
    def __init__(self, url, simulation=False, local_only=False, *, post=None, clock_ms=None, sleep=None):
        self.public, resolved = _endpoint(url, simulation, local_only)
        self.url=url; self._post=post or (lambda _url, body, timeout: _post(resolved, body, timeout))
        self._clock=clock_ms or (lambda: time.time()*1000); self._sleep=sleep or asyncio.sleep
        self._id=0; self._last=0; self._queue=[]; self._running=False

    async def __call__(self, method, params):
        if not isinstance(method, str) or method not in METHODS:
            raise ReleaseError('RPC_METHOD_REFUSED', method)
        if method == 'eth_getLogs':
            a,b=_bigint(params[0]['fromBlock']),_bigint(params[0]['toBlock'])
            if b<a or b-a>=1000:
                raise ReleaseError('LOG_RANGE')
        loop=asyncio.get_running_loop(); future=loop.create_future();self._id+=1
        request={'jsonrpc':'2.0','id':self._id,'method':method,'params':params}
        self._queue.append((request,future))
        if not self._running:
            self._running=True;loop.call_soon(lambda:asyncio.create_task(self._drain()))
        return await future

    async def _drain(self):
        try:
            while self._queue:
                batch=self._queue[:20];del self._queue[:20]
                requests=[r for r,_ in batch]; body=requests[0] if len(batch)==1 else requests
                try:
                    for attempt in range(3):
                        wait=max(0,350-(self._clock()-self._last)) if self.public else 0
                        if wait: await self._sleep(wait/1000)
                        self._last=self._clock()
                        try:
                            status,response=await asyncio.wait_for(self._post(self.url,parse_json(wire_json(body)),30000),30)
                        except asyncio.TimeoutError:
                            raise TransportError('TimeoutError','The operation was aborted due to timeout')
                        bodies=[response] if len(batch)==1 else response
                        retry=status in RETRY_HTTP or (isinstance(bodies,list) and any(isinstance(b,dict) and isinstance(b.get('error'),dict) and b['error'].get('code') in RETRY_RPC for b in bodies))
                        if retry and attempt<2:
                            await self._sleep(2**attempt);continue
                        if not 200<=status<300: raise ReleaseError('RPC_HTTP',str(status))
                        break
                    if not isinstance(bodies,list) or len(bodies)!=len(batch) or len({_id_key(b.get('id',_MISSING) if isinstance(b,dict) else _MISSING) for b in bodies})!=len(batch):
                        raise ReleaseError('RPC_BATCH_INCOMPLETE')
                    results=[]
                    for request,_ in batch:
                        entry=next((b for b in bodies if isinstance(b,dict) and _same_id(b.get('id'),request['id'])),None)
                        if not entry or entry.get('jsonrpc')!='2.0':raise ReleaseError('RPC_RESPONSE_ID')
                        if truthy(entry.get('error')):
                            error=entry['error'];detail=error.get('message','') if isinstance(error,dict) else ''
                            raise ReleaseError('RPC_ERROR',detail)
                        if 'result' not in entry:raise ReleaseError('RPC_MISSING_RESULT')
                        results.append(entry['result'])
                    for (_,future),value in zip(batch,results):
                        if not future.done():future.set_result(value)
                except Exception as error:
                    for _,future in batch:
                        if not future.done():future.set_exception(error)
        finally:
            self._running=False


async def pinned(chain_id, rpc, tag='latest'):
    """The existing reader's chain/header pin primitive, without release policy."""
    observed = _bigint(await rpc('eth_chainId', []))
    try: observed_number = float(observed)
    except OverflowError: observed_number = math.inf if observed > 0 else -math.inf
    if type(chain_id) not in (int, float) or observed_number != chain_id:
        raise ReleaseError('RPC_CHAIN')
    header = await rpc('eth_getBlockByNumber', [tag, False])
    if not isinstance(header, dict) or not all(truthy(header.get(k)) for k in ('hash', 'number', 'timestamp')):
        raise ReleaseError('BLOCK_MISSING')
    number = _bigint(header['number'])
    if tag != 'latest' and number != _bigint(tag):
        raise ReleaseError('BLOCK_NUMBER_MISMATCH')
    return {'header': header, 'block': '0x' + format(number, 'x')}
