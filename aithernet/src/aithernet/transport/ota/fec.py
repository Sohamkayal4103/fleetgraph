"""Phase 2: forward error correction + bit/byte helpers (pure Python, no numpy in core).

Two reference schemes: ``repetition3`` (triple-repeat + majority vote, rate 1/3) and ``hamming74``
(7,4 single-error-correcting, rate 4/7). FEC operates on bit lists so it composes with any modem.
"""

from __future__ import annotations


def bytes_to_bits(data: bytes) -> list[int]:
    bits: list[int] = []
    for byte in data:
        bits.extend((byte >> (7 - i)) & 1 for i in range(8))
    return bits


def bits_to_bytes(bits: list[int]) -> bytes:
    if len(bits) % 8:
        raise ValueError("bit count must be a multiple of 8")
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | (b & 1)
        out.append(byte)
    return bytes(out)


# -- repetition-3 -----------------------------------------------------------------------------

def _rep3_encode(bits: list[int]) -> list[int]:
    out: list[int] = []
    for b in bits:
        out.extend((b, b, b))
    return out


def _rep3_decode(bits: list[int]) -> list[int]:
    if len(bits) % 3:
        raise ValueError("repetition3 stream length must be a multiple of 3")
    return [1 if (bits[i] + bits[i + 1] + bits[i + 2]) >= 2 else 0
            for i in range(0, len(bits), 3)]


# -- Hamming(7,4) -----------------------------------------------------------------------------
# Generator: parity bits p1,p2,p3 over data d1..d4; corrects any single-bit error per 7-bit block.

def _ham74_encode(bits: list[int]) -> list[int]:
    pad = (-len(bits)) % 4
    bits = bits + [0] * pad
    out: list[int] = []
    for i in range(0, len(bits), 4):
        d1, d2, d3, d4 = bits[i:i + 4]
        p1 = d1 ^ d2 ^ d4
        p2 = d1 ^ d3 ^ d4
        p3 = d2 ^ d3 ^ d4
        out.extend((p1, p2, d1, p3, d2, d3, d4))
    return out


def _ham74_decode(bits: list[int]) -> list[int]:
    if len(bits) % 7:
        raise ValueError("hamming74 stream length must be a multiple of 7")
    out: list[int] = []
    for i in range(0, len(bits), 7):
        b = list(bits[i:i + 7])
        p1, p2, d1, p3, d2, d3, d4 = b
        s1 = p1 ^ d1 ^ d2 ^ d4
        s2 = p2 ^ d1 ^ d3 ^ d4
        s3 = p3 ^ d2 ^ d3 ^ d4
        syndrome = (s3 << 2) | (s2 << 1) | s1
        if syndrome:                     # correct the single bit at position `syndrome` (1-based)
            b[syndrome - 1] ^= 1
        out.extend((b[2], b[4], b[5], b[6]))   # d1,d2,d3,d4
    return out


_SCHEMES = {
    "none": (lambda b: list(b), lambda b: list(b)),
    "repetition3": (_rep3_encode, _rep3_decode),
    "hamming74": (_ham74_encode, _ham74_decode),
}


def fec_encode(bits: list[int], scheme: str) -> list[int]:
    if scheme not in _SCHEMES:
        raise ValueError(f"unknown fec scheme {scheme!r}")
    return _SCHEMES[scheme][0](bits)


def fec_decode(bits: list[int], scheme: str) -> list[int]:
    if scheme not in _SCHEMES:
        raise ValueError(f"unknown fec scheme {scheme!r}")
    return _SCHEMES[scheme][1](bits)
