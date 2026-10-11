"""Minimal ABI encoding/decoding for the read-only tools (static types only,
plus dynamic arrays of static tuples as returned by `getWithdrawals`)."""

try:
    from .keccak import keccak256, selector
except ImportError:  # Backwards-compatible standalone B0 entry points.
    from keccak import keccak256, selector


def enc_word(v):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, str):
        h = v[2:] if v.startswith("0x") else v
        if len(h) <= 40:  # address
            return h.lower().rjust(64, "0")
        return h.lower().rjust(64, "0")  # bytes32
    if isinstance(v, int):
        if v < 0:
            v += 1 << 256
        return format(v, "064x")
    raise TypeError(f"unsupported ABI value {v!r}")


def calldata(signature, *args):
    return selector(signature) + "".join(enc_word(a) for a in args)


def words(hexdata):
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def dec_uint(hexdata, i=0):
    w = words(hexdata)
    if len(w) <= i:
        raise ValueError(f"short return data: {hexdata!r}")
    return int(w[i], 16)


def dec_addr(hexdata, i=0):
    w = words(hexdata)
    if len(w) <= i:
        raise ValueError(f"short return data: {hexdata!r}")
    return "0x" + w[i][24:]


def dec_bool(hexdata, i=0):
    return dec_uint(hexdata, i) != 0


def dec_tuple_array(hexdata):
    """Decode `T[]` where T is a static tuple of N words; returns list of int lists."""
    w = words(hexdata)
    if not w:
        raise ValueError("empty return data")
    off = int(w[0], 16) // 32
    n = int(w[off], 16)
    body = w[off + 1:]
    if n == 0:
        return []
    if len(body) % n:
        raise ValueError("tuple array body not divisible by length")
    k = len(body) // n
    return [[int(x, 16) for x in body[j * k:(j + 1) * k]] for j in range(n)]


def topic_addr(topic):
    return "0x" + topic[-40:].lower()


def event_topic(signature):
    return keccak256(signature.encode())


def _selftest():
    assert calldata("balanceOf(address)", "0x00000000000000000000000000000000000000aa") == \
        "0x70a08231" + "0" * 62 + "aa"
    # [ (1,2), (3,4) ] encoded as dynamic array of 2-word tuples
    enc = "0x" + enc_word(32) + enc_word(2) + enc_word(1) + enc_word(2) + enc_word(3) + enc_word(4)
    assert dec_tuple_array(enc) == [[1, 2], [3, 4]]
    assert dec_tuple_array("0x" + enc_word(32) + enc_word(0)) == []


if __name__ == "__main__":
    _selftest()
    print("abi selftest ok")

# Typed codec: additive API. The historical static functions above remain unchanged.
# Decoding preserves the pinned viem reader's admission (including noncanonical
# padding/offsets); callers must not mistake decoding for artifact/state attestation.
import re as _re


class AbiError(ValueError):
    """Stable reference failure category; diagnostic formatting is not a hash domain."""
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(detail or code)


class BigInt(int):
    """ABI uint >48 bits, preserving viem's BigInt versus Number result distinction."""


def _fail(code, detail=''):
    raise AbiError(code, detail)


def _array(param):
    match = _re.fullmatch(r'(.+)\[(\d*)\]', param['type'])
    if not match:
        return None
    return dict(param, type=match[1]), (int(match[2]) if match[2] else None)


def _dynamic(p):
    a = _array(p)
    if a:
        return a[1] is None or _dynamic(a[0])
    return p['type'] in ('bytes', 'string') or (p['type'] == 'tuple' and any(map(_dynamic, p['components'])))


def _static_size(p):
    if _dynamic(p):
        return 32
    a = _array(p)
    if a:
        return a[1] * _static_size(a[0])
    return sum(map(_static_size, p['components'])) if p['type'] == 'tuple' else 32


def _hex(value, malformed='InvalidBytesError'):
    if not isinstance(value, str):
        _fail('TypeError')
    if not _re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', value):
        _fail(malformed)
    return bytes.fromhex(value[2:])


def _word(n):
    return n.to_bytes(32, 'big')


def _checksum(value):
    low = value.lower().removeprefix('0x')
    digest = keccak256(low.encode())[2:]
    return '0x' + ''.join(c.upper() if int(h, 16) >= 8 else c for c, h in zip(low, digest))


def _utf8(value):
    # JS TextEncoder replaces unpaired UTF-16 surrogates with U+FFFD.
    return value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'replace').encode('utf-8')


def _encode_sequence(params, values):
    # Encoding fragments are even-length hex text without 0x. Byte-value leaves
    # retain their original spelling; ABI sizes and offsets remain in octets.
    if len(params) != len(values):
        _fail('AbiEncodingLengthMismatchError')
    encoded = [_encode(p, v) for p, v in zip(params, values)]
    offset = sum(_static_size(p) for p in params)
    head, tail = [], []
    for p, data in zip(params, encoded):
        if _dynamic(p):
            head.append(_word(offset).hex()); tail.append(data); offset += len(data) // 2
        else:
            head.append(data)
    return ''.join(head + tail)


def _encode(p, value):
    t = p['type']; a = _array(p)
    if a:
        child, count = a
        if not isinstance(value, (list, tuple)):
            _fail('InvalidArrayError')
        if count is not None and len(value) != count:
            _fail('AbiEncodingArrayLengthMismatchError')
        data = _encode_sequence([child] * len(value), value)
        return (_word(len(value)).hex() if count is None else '') + data
    if t == 'tuple':
        components = p['components']
        vals = [value.get(c['name']) for c in components] if isinstance(value, dict) else value
        return _encode_sequence(components, vals)
    if t == 'address':
        if not isinstance(value, str) or not _re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
            _fail('InvalidAddressError')
        if value != value.lower() and value != _checksum(value):
            _fail('InvalidAddressError')
        return (bytes(12) + bytes.fromhex(value[2:])).hex()
    if t == 'bool':
        if type(value) is not bool:
            _fail('BaseError', 'Invalid boolean value')
        return _word(int(value)).hex()
    match = _re.fullmatch(r'uint(\d*)', t)
    if match:
        width = int(match[1] or 256)
        if width < 8 or width > 256 or width % 8:
            _fail('InvalidAbiEncodingTypeError')
        try:
            n = int(value, 0) if isinstance(value, str) and _re.match(r'^0[xXbBoO]', value) else int(value)
        except (ValueError, TypeError):
            _fail('TypeError')
        if n < 0 or n >= 2**width:
            _fail('IntegerOutOfRangeError')
        if isinstance(value, float) and value != n:
            _fail('RangeError')
        return _word(n).hex()
    if t == 'string':
        data = _utf8(value).hex()
    elif t == 'bytes':
        _hex(value)  # Validate whole octets without replacing their hex spelling.
        data = value[2:]
    else:
        match = _re.fullmatch(r'bytes(\d+)', t)
        if not match or not 1 <= int(match[1]) <= 32:
            _fail('InvalidAbiEncodingTypeError')
        data = _hex(value)
        if len(data) != int(match[1]):
            _fail('AbiEncodingBytesSizeMismatchError')
        return value[2:].ljust(64, '0')
    return _word(len(data) // 2).hex() + data + '0' * (-len(data) % 64)


def encode_parameters(params, values):
    """Encode JSON ABI schemas, not a human-signature language or wallet request."""
    return '0x' + _encode_sequence(params, values)


def _read(data, start, count):
    if start < 0 or start >= len(data) or start + count > len(data):
        _fail('PositionOutOfBoundsError')
    return data[start:start + count]


def _number(data, pos):
    value = int.from_bytes(_read(data, pos, 32), 'big')
    if value > 2**53 - 1:
        _fail('IntegerOutOfRangeError')
    return value


def _decode_sequence(params, data, start):
    values, pos = [], start
    for p in params:
        values.append(_decode(p, data, pos, start))
        pos += _static_size(p)
    return values


def _decode(p, data, pos, base):
    t = p['type']; a = _array(p)
    if a:
        child, count = a
        start = base + _number(data, pos) if _dynamic(p) else pos
        if count is None:
            count = _number(data, start); start += 32
        size = _static_size(child)
        # Bound resource use by actual accessible slots before allocating the result.
        if size and count > max(0, len(data) - start) // size:
            _fail('PositionOutOfBoundsError')
        return [_decode(child, data, start + i * size, start) for i in range(count)]
    if t == 'tuple':
        start = base + _number(data, pos) if _dynamic(p) else pos
        vals = _decode_sequence(p['components'], data, start)
        if p['components'] and all(x.get('name') for x in p['components']):
            return {x['name']: v for x, v in zip(p['components'], vals)}
        return vals
    if t in ('bytes', 'string'):
        start = base + _number(data, pos)
        length = _number(data, start)
        # viem still moves its cursor to base+32 for a zero-length value.
        if not length:
            if base + 32 >= len(data):
                _fail('PositionOutOfBoundsError')
            return '' if t == 'string' else '0x'
        body = _read(data, start + 32, length)
        if t == 'string':
            # Pinned viem decodeString trims leading NULs and uses TextDecoder.
            body = body.lstrip(b'\0') or b'\0'
            return body.decode('utf-8-sig', 'replace')
        return '0x' + body.hex()
    word = _read(data, pos, 32)
    if t == 'address':
        return _checksum(word[-20:].hex())
    if t == 'bool':
        n = int.from_bytes(word, 'big')
        if n not in (0, 1):
            _fail('InvalidBytesBooleanError')
        return bool(n)
    match = _re.fullmatch(r'uint(\d*)', t)
    if match:
        n = int.from_bytes(word, 'big'); width = int(match[1] or 256)
        if width <= 48:
            if n > 2**53 - 1:
                _fail('IntegerOutOfRangeError')
            return n
        return BigInt(n)
    match = _re.fullmatch(r'bytes(\d+)', t)
    if match and 1 <= int(match[1]) <= 32:
        return '0x' + word[:int(match[1])].hex()
    _fail('InvalidAbiDecodingTypeError')


def decode_parameters(params, value):
    # viem's hex-to-byte decoder reports BaseError for malformed text. Keep
    # validation fail-closed and non-string TypeError; do not catch other errors.
    data = _hex(value, malformed='BaseError')
    if not data and params:
        _fail('AbiDecodingZeroDataError')
    if data and len(data) < 32:
        _fail('AbiDecodingDataSizeTooSmallError')
    return _decode_sequence(params, data, 0)


def canonical_type(p):
    a = _array(p)
    if a:
        return canonical_type(a[0]) + ('[]' if a[1] is None else f'[{a[1]}]')
    if p['type'] == 'tuple':
        return '(' + ','.join(map(canonical_type, p['components'])) + ')'
    return 'uint256' if p['type'] == 'uint' else p['type']


def item_signature(item):
    return item['name'] + '(' + ','.join(map(canonical_type, item['inputs'])) + ')'


def encode_function(item, values):
    return selector(item_signature(item)) + encode_parameters(item['inputs'], values)[2:]


def decode_event(item, topics, data):
    if not topics:
        _fail('AbiEventSignatureEmptyTopicsError')
    if topics[0] != keccak256(item_signature(item).encode()):
        _fail('AbiEventSignatureNotFoundError')
    inputs = item['inputs']; values = {}; i = 1
    for index, p in enumerate(inputs):
        if p.get('indexed'):
            if i >= len(topics):
                _fail('DecodeLogTopicsMismatch')
            values[index] = topics[i] if p['type'] in ('string', 'bytes', 'tuple') or _array(p) else decode_parameters([p], topics[i])[0]
            i += 1
    nonindexed = [(i, p) for i, p in enumerate(inputs) if not p.get('indexed')]
    if nonindexed:
        if not data or data == '0x':
            _fail('DecodeLogDataMismatch')
        try:
            decoded = decode_parameters([p for _, p in nonindexed], data)
        except AbiError as exc:
            if exc.code in ('AbiDecodingDataSizeTooSmallError', 'PositionOutOfBoundsError'):
                _fail('DecodeLogDataMismatch')
            raise
        values.update({i: v for (i, _), v in zip(nonindexed, decoded)})
    args = [values[i] for i in range(len(inputs))] if any(not p.get('name') for p in inputs) else {inputs[i]['name']: v for i, v in values.items()}
    return {'eventName': item['name'], 'args': args}


class _Undefined:
    def __bool__(self):
        return False


UNDEFINED = _Undefined()


def decode_function(items, data):
    """Resolve the exact selector in a reviewed JSON ABI; no guessed overload."""
    signature = data[:10]
    item = next((i for i in items if i.get('type') == 'function' and selector(item_signature(i)) == signature), None)
    if item is None:
        _fail('AbiFunctionSignatureNotFoundError')
    return {'functionName': item['name'],
            'args': decode_parameters(item['inputs'], '0x' + data[10:]) if item.get('inputs') else UNDEFINED}
