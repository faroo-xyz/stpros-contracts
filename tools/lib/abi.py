"""Minimal ABI encoding/decoding for the read-only tools (static types only,
plus dynamic arrays of static tuples as returned by `getWithdrawals`)."""

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
