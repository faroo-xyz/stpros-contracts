"""EVM bytecode helpers: CBOR metadata split, disassembly, opcode and
selector scans. Used by provenance (identity), snapshots (capabilities,
bytecode) and fixtures."""

from keccak import keccak256

OPNAMES = {
    0x00: "STOP", 0x01: "ADD", 0x02: "MUL", 0x03: "SUB", 0x04: "DIV", 0x05: "SDIV", 0x06: "MOD",
    0x07: "SMOD", 0x08: "ADDMOD", 0x09: "MULMOD", 0x0A: "EXP", 0x0B: "SIGNEXTEND",
    0x10: "LT", 0x11: "GT", 0x12: "SLT", 0x13: "SGT", 0x14: "EQ", 0x15: "ISZERO", 0x16: "AND",
    0x17: "OR", 0x18: "XOR", 0x19: "NOT", 0x1A: "BYTE", 0x1B: "SHL", 0x1C: "SHR", 0x1D: "SAR",
    0x20: "KECCAK256", 0x30: "ADDRESS", 0x31: "BALANCE", 0x32: "ORIGIN", 0x33: "CALLER",
    0x34: "CALLVALUE", 0x35: "CALLDATALOAD", 0x36: "CALLDATASIZE", 0x37: "CALLDATACOPY",
    0x38: "CODESIZE", 0x39: "CODECOPY", 0x3A: "GASPRICE", 0x3B: "EXTCODESIZE", 0x3C: "EXTCODECOPY",
    0x3D: "RETURNDATASIZE", 0x3E: "RETURNDATACOPY", 0x3F: "EXTCODEHASH", 0x40: "BLOCKHASH",
    0x41: "COINBASE", 0x42: "TIMESTAMP", 0x43: "NUMBER", 0x44: "PREVRANDAO", 0x45: "GASLIMIT",
    0x46: "CHAINID", 0x47: "SELFBALANCE", 0x48: "BASEFEE", 0x49: "BLOBHASH", 0x4A: "BLOBBASEFEE",
    0x50: "POP", 0x51: "MLOAD", 0x52: "MSTORE", 0x53: "MSTORE8", 0x54: "SLOAD", 0x55: "SSTORE",
    0x56: "JUMP", 0x57: "JUMPI", 0x58: "PC", 0x59: "MSIZE", 0x5A: "GAS", 0x5B: "JUMPDEST",
    0x5C: "TLOAD", 0x5D: "TSTORE", 0x5E: "MCOPY", 0x5F: "PUSH0",
    0xF0: "CREATE", 0xF1: "CALL", 0xF2: "CALLCODE", 0xF3: "RETURN", 0xF4: "DELEGATECALL",
    0xF5: "CREATE2", 0xFA: "STATICCALL", 0xFD: "REVERT", 0xFE: "INVALID", 0xFF: "SELFDESTRUCT",
}
for _i in range(32):
    OPNAMES[0x60 + _i] = f"PUSH{_i + 1}"
for _i in range(16):
    OPNAMES[0x80 + _i] = f"DUP{_i + 1}"
    OPNAMES[0x90 + _i] = f"SWAP{_i + 1}"
for _i in range(5):
    OPNAMES[0xA0 + _i] = f"LOG{_i}"

FORBIDDEN_OPCODES = ("DELEGATECALL", "CALLCODE", "SELFDESTRUCT")


def split_metadata(code: bytes):
    """Split solc runtime/creation code into (body, cbor). The CBOR length is
    the trailing 2 bytes; returns (code, b"") when no valid CBOR map trails."""
    if len(code) < 2:
        return code, b""
    n = int.from_bytes(code[-2:], "big")
    if n == 0 or n + 2 > len(code):
        return code, b""
    cbor = code[-(n + 2):]
    if cbor[0] & 0xE0 != 0xA0:  # CBOR map major type
        return code, b""
    return code[:-(n + 2)], cbor


def parse_cbor_metadata(cbor: bytes):
    """Decode the tiny solc CBOR map: {"ipfs": bytes, "solc": bytes|str, ...}."""
    out = {}
    if not cbor:
        return out
    data = cbor[:-2]
    pos = 1
    count = data[0] & 0x1F

    def read_item(p):
        b = data[p]
        major, info = b >> 5, b & 0x1F
        p += 1
        if info < 24:
            length = info
        elif info == 24:
            length = data[p]; p += 1
        elif info == 25:
            length = int.from_bytes(data[p:p + 2], "big"); p += 2
        else:
            raise ValueError("unsupported CBOR length")
        if major == 2:
            return data[p:p + length], p + length
        if major == 3:
            return data[p:p + length].decode(), p + length
        if major == 7:
            return {20: False, 21: True}.get(info), p
        if major == 0:
            return length, p
        raise ValueError(f"unsupported CBOR major type {major}")

    for _ in range(count):
        k, pos = read_item(pos)
        v, pos = read_item(pos)
        out[k] = v.hex() if isinstance(v, (bytes, bytearray)) else v
    return out


def instructions(code: bytes):
    """Yield (pc, opname, push_data) over executable code (metadata stripped)."""
    body, _ = split_metadata(code)
    pc = 0
    while pc < len(body):
        op = body[pc]
        name = OPNAMES.get(op, f"UNKNOWN_0x{op:02x}")
        if 0x60 <= op <= 0x7F:
            n = op - 0x5F
            yield pc, name, body[pc + 1:pc + 1 + n]
            pc += 1 + n
        else:
            yield pc, name, b""
            pc += 1


def opcode_presence(code: bytes, names=FORBIDDEN_OPCODES):
    """Opcodes present as instructions. Data sections after the dispatcher
    can decode as spurious opcodes, so callers report this as a structural
    (ST) scan, never as a semantic proof."""
    found = {n: 0 for n in names}
    for _, name, _ in instructions(code):
        if name in found:
            found[name] += 1
    return found


def push4_selectors(code: bytes):
    """Candidate 4-byte selectors pushed in the dispatcher (PUSH4 x; EQ)."""
    sels = set()
    prev = None
    for _, name, data in instructions(code):
        if prev is not None and name in ("EQ", "DUP2", "GT", "LT"):
            sels.add("0x" + prev.hex())
        prev = data if name == "PUSH4" else None
    return sorted(sels)


def code_identity(code_hex: str):
    code = bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)
    body, cbor = split_metadata(code)
    meta = parse_cbor_metadata(cbor) if cbor else {}
    return {
        "size": len(code),
        "keccak256": keccak256(code),
        "keccak256_without_metadata": keccak256(body),
        "cbor_metadata": meta,
    }


def mask_immutables(code: bytes, immutable_refs):
    """Zero the byte ranges of immutable references (solc `immutableReferences`)."""
    b = bytearray(code)
    for refs in (immutable_refs or {}).values():
        for r in refs:
            s, ln = r["start"], r["length"]
            b[s:s + ln] = b"\x00" * ln
    return bytes(b)
