"""Value, ABI and output helpers shared by the release policy modules.

Only code-owned ABI descriptors are loaded here. Approval and chain state never
live in module globals. The four existing JSON/hash domains stay separate.
"""
from functools import lru_cache
from pathlib import Path
import json
import math
import re

from ...lib import abi
from ...lib.keccak import keccak256, selector
from ...lib import release_json as j
from .rpc import TransportError, _bigint

UNDEFINED = abi.UNDEFINED
BigInt = abi.BigInt
ROOT = Path(__file__).resolve().parents[3]
ZERO = '0x' + '00' * 20
ZERO32 = '0x' + '00' * 32
SCHEMAS = json.loads((Path(__file__).with_name('abi-schemas.json')).read_text())['schemas']


def field(value, key, default=UNDEFINED):
    """JSON object property access, retaining missing versus null."""
    if value is None or value is UNDEFINED:
        raise TypeError('Cannot read properties of null or undefined')
    if isinstance(value, dict):
        return value.get(key, default)
    if key == 'length' and isinstance(value, (list, str)):
        return len(value.encode('utf-16-le', 'surrogatepass')) // 2 if isinstance(value, str) else len(value)
    return default


def optional(value, *keys):
    for key in keys:
        if value is None or value is UNDEFINED:
            return UNDEFINED
        value = field(value, key)
    return value


def coalesce(value, fallback):
    return fallback if value is None or value is UNDEFINED else value


def equal(a, b):
    if isinstance(a, BigInt) or isinstance(b, BigInt):
        return isinstance(a, BigInt) and isinstance(b, BigInt) and int(a) == int(b)
    if type(a) is bool or type(b) is bool:
        return type(a) is type(b) and a == b
    if type(a) in (int, float) and type(b) in (int, float):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return type(a) is type(b) and a == b


def string(value):
    if value is UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'true' if value else 'false'
    if isinstance(value, BigInt):
        return str(int(value))
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return 'NaN' if math.isnan(value) else '-Infinity' if value < 0 else 'Infinity'
        return j.number_text(value)
    if isinstance(value, list):
        return ','.join('' if x is None or x is UNDEFINED else string(x) for x in value)
    return '[object Object]'


def require(ok, code, detail=''):
    if not j.truthy(ok):
        raise j.ReleaseError(code, string(detail) if j.truthy(detail) else '')


def bigint(value):
    if value is UNDEFINED:
        raise TransportError('TypeError', 'Cannot convert undefined to a BigInt')
    return BigInt(_bigint(value))


def integer_divide(left, right):
    """BigInt division truncates toward zero and names division-by-zero failures."""
    if right == 0:
        raise TransportError('RangeError', 'Division by zero')
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


def number(value):
    if value is None:
        return 0
    if value is UNDEFINED:
        return math.nan
    if isinstance(value, str):
        text = value.strip()
        try:
            value = _bigint(text) if re.match(r'^0[xXoObB]', text) else float(text or '0')
        except (ValueError, TransportError):
            return math.nan
    try:
        result = float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf
    return int(result) if math.isfinite(result) and result.is_integer() else result


def safe_integer(value):
    return type(value) in (int, float) and -2**53 + 1 <= value <= 2**53 - 1 and value == int(value)


def array(value):
    if not isinstance(value, list):
        raise TypeError('Array operation requires an array')
    return value


def unique(values):
    """Insertion-preserving set for the source's address/role strings."""
    return list(dict.fromkeys(values))


def exact_set(a, b, code):
    if not isinstance(a, (list, str)) or not isinstance(b, (list, str)):
        raise TypeError('Value is not iterable')
    order = lambda value: string(value).encode('utf-16-be', 'surrogatepass')
    require(field(a, 'length') == len(set(a)) and j.canonical(sorted(a, key=order)) == j.canonical(sorted(b, key=order)), code)


def address(value, label='address'):
    require(isinstance(value, str) and re.fullmatch(r'0x[0-9a-f]{40}', value) and value != ZERO, 'ADDRESS', label)
    return value


def hex_number(value):
    return '0x' + format(bigint(value), 'x')


def hash_hex(value):
    """Viem hashing admission is distinct from typed ABI byte-value admission.

    Hex hashing left-pads an odd nibble; it never changes the typed encoder's
    reviewed refusal of odd or nonhex byte-value strings.
    """
    if not isinstance(value, str) or not value.startswith('0x'):
        raise j.ReleaseError('Uint8Array expected')
    digits = value[2:]
    if len(digits) % 2:
        digits = '0' + digits
    if not re.fullmatch(r'[0-9a-fA-F]*', digits):
        raise abi.AbiError('BaseError', 'Invalid byte sequence')
    return keccak256(bytes.fromhex(digits))


def hash_text(value):
    return keccak256(abi._utf8(value))


ROLES = dict(admin=ZERO32, proposer=hash_text('PROPOSER_ROLE'), executor=hash_text('EXECUTOR_ROLE'),
             canceller=hash_text('CANCELLER_ROLE'), operator=hash_text('OPERATOR_ROLE'))
SLOTS = {'admin': hex_number(bigint(hash_text('eip1967.proxy.admin')) - 1),
         'implementation': '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc',
         'initialized': '0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00'}


@lru_cache(maxsize=256)
def function_selector(signature):
    return selector(signature)


def function_item(items, name):
    item = next((x for x in items if x.get('type') == 'function' and x.get('name') == name), None)
    if item is None:
        raise abi.AbiError('AbiFunctionNotFoundError')
    return item


def call_data(items, name, args=()):
    item = function_item(items, name)
    return function_selector(abi.item_signature(item)) + abi.encode_parameters(item['inputs'], args)[2:]


def decode_result(items, name, data):
    outputs = function_item(items, name)['outputs']
    values = abi.decode_parameters(outputs, data)
    return values[0] if len(outputs) == 1 else values if outputs else UNDEFINED


def event(items, row):
    topics = row['topics']
    if not topics:
        raise abi.AbiError('AbiEventSignatureEmptyTopicsError')
    item = next((x for x in items if hash_text(abi.item_signature(x)) == topics[0]), None)
    if item is None:
        raise abi.AbiError('AbiEventSignatureNotFoundError')
    return abi.decode_event(item, topics, row['data'])


def output_value(value):
    if isinstance(value, BigInt):
        return str(int(value))
    if isinstance(value, list):
        return [output_value(x) for x in value]
    if isinstance(value, dict):
        return {k: output_value(v) for k, v in value.items()}
    return value


def pretty_json(value):
    """Indent exact JSON.stringify-compatible tokens without reserializing numbers."""
    compact = j.wire_json(output_value(value))
    if compact is UNDEFINED:
        return UNDEFINED
    out, depth, quoted, escaped = [], 0, False, False
    for index, char in enumerate(compact):
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True; out.append(char)
        elif char in '{[':
            out.append(char); depth += 1
            if compact[index + 1] not in '}]':
                out.append('\n' + '  ' * depth)
        elif char in '}]':
            depth -= 1
            if compact[index - 1] not in '{[':
                out.append('\n' + '  ' * depth)
            out.append(char)
        elif char == ',':
            out.append(',\n' + '  ' * depth)
        elif char == ':':
            out.append(': ')
        else:
            out.append(char)
    return ''.join(out)
