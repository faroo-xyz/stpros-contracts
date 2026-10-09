"""Pure-Python Keccak-256 (Ethereum variant, pre-NIST padding).

Promoted from the research evidence tool `evidence/tools/keccak.py`. No third-
party dependency, so every tool in this repository can hash without network or
package installation. Throughput is low (~1 MB/s); it is only used for source
files, selectors, slots and bytecode identities.
"""

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256_bytes(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data)
    p.append(1)
    while len(p) % rate:
        p.append(0)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        blk = p[off:off + rate]
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            a[x][y] ^= int.from_bytes(blk[8 * i:8 * i + 8], "little")
        a = _f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def keccak256(data: bytes) -> str:
    """Return the 0x-prefixed lowercase hex Keccak-256 of `data`."""
    return "0x" + keccak256_bytes(data).hex()


def selector(signature: str) -> str:
    """4-byte function/error selector, 0x-prefixed."""
    return keccak256(signature.encode())[:10]


def erc7201_slot(namespace: str) -> str:
    """ERC-7201 root: keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~0xff."""
    inner = int(keccak256(namespace.encode()), 16) - 1
    outer = int(keccak256(inner.to_bytes(32, "big")), 16) & ~0xFF
    return "0x" + outer.to_bytes(32, "big").hex()


def _selftest():
    assert keccak256(b"") == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc") == "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    # A 200-byte message crosses the 136-byte rate boundary.
    assert keccak256(b"a" * 200) == "0x" + keccak256_bytes(b"a" * 200).hex()
    assert erc7201_slot("openzeppelin.storage.Initializable") == \
        "0xf0c57e16840df040f15088dc2f81fe391c3923bec73e23a9662efc9c229c6a00"
    assert erc7201_slot("openzeppelin.storage.Ownable") == \
        "0x9016d09d72d40fdae2fd8ceac6b6234c7706214fd39c1cd1e609a0528c199300"


if __name__ == "__main__":
    _selftest()
    print("keccak selftest ok")
