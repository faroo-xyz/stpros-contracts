"""Release JSON identity, deliberately distinct from B0/native pretty JSON.

Numbers model JSON.parse's IEEE-754 Number; BigInt is explicit. No approval is
created here. checked_json preserves the existing helper's optional hash;
approved_json requires an externally supplied digest for authorization inputs.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

try:
    from .abi import BigInt, UNDEFINED
except ImportError:  # Existing standalone tools put tools/lib on sys.path.
    from abi import BigInt, UNDEFINED


class ReleaseError(ValueError):
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(code + (': ' + detail if detail else ''))


def truthy(value):
    # Objects and arrays are truthy in JS even when empty.
    return not (value is UNDEFINED or value is None or value is False or (isinstance(value, (int, float)) and
                (value == 0 or (isinstance(value, float) and math.isnan(value)))) or value == '')


def number_text(value):
    try:
        x = float(value)
    except OverflowError:
        return 'null'
    if not math.isfinite(x):
        return 'null'
    if x == 0:
        return '0'
    sign = '-' if x < 0 else ''
    text = repr(abs(x)).lower()
    if 'e' in text:
        mantissa, exponent = text.split('e'); exponent = int(exponent)
    else:
        mantissa, exponent = text, 0
    whole, _, fraction = mantissa.partition('.')
    digits = (whole + fraction).lstrip('0')
    decimal = len(whole) + exponent
    if whole == '0':
        leading = len(fraction) - len(fraction.lstrip('0'))
        decimal = -leading + exponent
    digits = digits.rstrip('0') or '0'
    if -6 < decimal <= 21:
        if decimal <= 0:
            return sign + '0.' + '0' * -decimal + digits
        if decimal >= len(digits):
            return sign + digits + '0' * (decimal - len(digits))
        return sign + digits[:decimal] + '.' + digits[decimal:]
    exp = decimal - 1
    return sign + digits[0] + ('.' + digits[1:] if len(digits) > 1 else '') + 'e' + ('+' if exp >= 0 else '') + str(exp)


def quote(value):
    # JSON.stringify is well-formed: lone surrogates escape, pairs are UTF-8.
    value = value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass')
    result = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return ''.join(f'\\u{ord(c):04x}' if 0xd800 <= ord(c) <= 0xdfff else c for c in result)


def canonical(value):
    if value is UNDEFINED:
        return UNDEFINED
    if isinstance(value, BigInt):
        return quote(str(value))
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return number_text(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, list):
        return '[' + ','.join('' if x is UNDEFINED else canonical(x) for x in value) + ']'
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError('release JSON object keys must be strings')
        keys = sorted(value, key=lambda k: k.encode('utf-16-be', 'surrogatepass'))
        return '{' + ','.join(quote(k) + ':' + ('undefined' if value[k] is UNDEFINED else canonical(value[k])) for k in keys) + '}'
    raise TypeError('unsupported release JSON value')


def parse_json(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')  # Node Buffer.toString behavior
    def invalid(_):
        raise json.JSONDecodeError('Invalid JSON constant', _, 0)
    return json.loads(raw, parse_int=float, parse_float=float, parse_constant=invalid)


def canonical_sha256(value):
    text = canonical(value)
    if text is UNDEFINED: raise TypeError('cannot hash undefined')
    return hashlib.sha256(text.encode()).hexdigest()


def raw_sha256(value):
    return hashlib.sha256(value).hexdigest()


def historical_filter_id(addresses):
    """B0 wire format includes separator spaces; never use release canonical here."""
    return raw_sha256(json.dumps(sorted(addresses)).encode())[:16]


def checked_json(path, expected_hash=None):
    """Match core.checkedJson; the optional digest is not an approval assertion."""
    absolute = os.path.abspath(path)
    cursor = Path(absolute).anchor
    for part in Path(absolute).parts[1:]:
        if part.startswith('.') or re.search(r'(^keys?$|credentials?|secrets?|keystores?|private[-_]?keys?|id_rsa|id_ed25519)', part, re.I):
            raise ReleaseError('UNSAFE_INPUT_PATH')
        cursor = os.path.join(cursor, part)
        if stat.S_ISLNK(os.lstat(cursor).st_mode):
            raise ReleaseError('SYMLINK_INPUT')
    if not absolute.endswith('.json'):
        raise ReleaseError('JSON_INPUT_REQUIRED')
    data = Path(absolute).read_bytes()
    if expected_hash and raw_sha256(data) != expected_hash:
        raise ReleaseError('REVIEWED_FILE_HASH', absolute)
    return parse_json(data)


def approved_json(path, expected_hash):
    """Mandatory externally approved raw SHA, for future CLI input boundaries."""
    if not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
        raise ReleaseError('INDEPENDENT_REVIEW_HASH_REQUIRED')
    return checked_json(path, expected_hash)


def wire_json(value):
    """JSON.stringify wire format, preserving insertion order except integer keys."""
    if value is UNDEFINED:
        return UNDEFINED
    if isinstance(value, BigInt):
        raise TypeError('Do not know how to serialize a BigInt')
    if isinstance(value, list):
        return '[' + ','.join('null' if v is UNDEFINED else wire_json(v) for v in value) + ']'
    if isinstance(value, dict):
        numeric = sorted((k for k in value if re.fullmatch(r'0|[1-9][0-9]*', k) and int(k) < 2**32-1), key=int)
        keys = numeric + [k for k in value if k not in numeric]
        return '{' + ','.join(quote(k) + ':' + wire_json(value[k]) for k in keys if value[k] is not UNDEFINED) + '}'
    return canonical(value)
