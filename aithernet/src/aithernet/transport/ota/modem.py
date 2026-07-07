"""Phase 2: the reference software modem (pure Python complex baseband; no numpy in core).

Implements BPSK/QPSK modulation + demodulation with a known preamble+sync prefix used for frame
acquisition (cross-correlation) and phase recovery. This is the software/simulation realization of
the link; the GNU Radio flowgraph (see ``flowgraph.py``) is the hardware realization. The modem
never transmits — it only turns bytes into baseband samples and back.
"""

from __future__ import annotations

import cmath
from dataclasses import dataclass

from aithernet.transport.ota.fec import (
    bits_to_bytes,
    bytes_to_bits,
    fec_decode,
    fec_encode,
)
from aithernet.transport.ota.profiles import RFLinkProfile

_INV_SQRT2 = 2 ** -0.5

# Gray-mapped QPSK constellation for bit-pairs.
_QPSK = {
    (0, 0): complex(_INV_SQRT2, _INV_SQRT2), (0, 1): complex(-_INV_SQRT2, _INV_SQRT2),
    (1, 1): complex(-_INV_SQRT2, -_INV_SQRT2), (1, 0): complex(_INV_SQRT2, -_INV_SQRT2),
}
_QPSK_INV = {v: k for k, v in _QPSK.items()}


def _bits_per_symbol(modulation: str) -> int:
    return {"bpsk": 1, "qpsk": 2}[modulation]


def _map_symbols(bits: list[int], modulation: str) -> list[complex]:
    if modulation == "bpsk":
        return [complex(1.0 if b == 0 else -1.0, 0.0) for b in bits]
    pad = (-len(bits)) % 2
    bits = bits + [0] * pad
    return [_QPSK[(bits[i], bits[i + 1])] for i in range(0, len(bits), 2)]


def _demap_symbols(symbols: list[complex], modulation: str) -> list[int]:
    if modulation == "bpsk":
        return [0 if s.real >= 0 else 1 for s in symbols]
    bits: list[int] = []
    for s in symbols:
        nearest = min(_QPSK_INV, key=lambda c: abs(c - s))
        bits.extend(_QPSK_INV[nearest])
    return bits


def _prefix_symbols(profile: RFLinkProfile) -> list[complex]:
    """Known preamble+sync symbols used for acquisition + phase estimation (BPSK-mapped bytes)."""
    bits = bytes_to_bits(profile.preamble + profile.sync_word)
    return [complex(1.0 if b == 0 else -1.0, 0.0) for b in bits]


def _upsample(symbols: list[complex], sps: int) -> list[complex]:
    out: list[complex] = []
    for s in symbols:
        out.extend([s] * sps)
    return out


@dataclass(frozen=True)
class DemodResult:
    data: bytes
    start_index: int
    phase_estimate: float
    prefix_correlation: float


def modulate(frame_bytes: bytes, profile: RFLinkProfile) -> list[complex]:
    """Bytes -> FEC -> symbols (preamble+sync prefix) -> rectangular-pulse baseband samples."""
    coded = fec_encode(bytes_to_bits(frame_bytes), profile.fec_scheme)
    symbols = _prefix_symbols(profile) + _map_symbols(coded, profile.modulation)
    return _upsample(symbols, profile.samples_per_symbol)


def _matched_filter(samples: list[complex], sps: int) -> list[complex]:
    """Integrate-and-dump over each symbol (matched filter for a rectangular pulse)."""
    n = len(samples) // sps
    return [sum(samples[i * sps:(i + 1) * sps]) / sps for i in range(n)]


def demodulate(samples: list[complex], profile: RFLinkProfile) -> DemodResult:
    """Acquire the prefix, estimate phase, then demap payload symbols -> FEC -> bytes.

    Robust to a leading integer sample delay, a constant phase rotation, and AWGN (the channel must
    preserve the signal length — no sample insertion/deletion)."""
    sps = profile.samples_per_symbol
    prefix = _prefix_symbols(profile)
    plen = len(prefix)
    # Symbol-rate matched filter over candidate symbol-timing offsets; pick the offset whose
    # prefix cross-correlation magnitude is largest (also yields the phase estimate).
    best = None
    for offset in range(sps):
        syms = _matched_filter(samples[offset:], sps)
        if len(syms) < plen:
            continue
        # slide the known prefix across the symbol stream
        limit = min(len(syms) - plen, 2 * (len(prefix) + 8))
        for start in range(limit + 1):
            corr = sum(syms[start + k] * prefix[k].conjugate() for k in range(plen))
            mag = abs(corr)
            if best is None or mag > best[0]:
                best = (mag, offset, start, corr, syms)
    if best is None:
        raise ValueError("no symbols to demodulate")
    mag, offset, start, corr, syms = best
    phase = cmath.phase(corr)
    derot = cmath.exp(-1j * phase)
    payload_syms = [s * derot for s in syms[start + plen:]]
    coded_bits = _demap_symbols(payload_syms, profile.modulation)
    # Trim to a length the FEC scheme + byte packing accept (drop trailing partial symbol bits).
    decoded = fec_decode(coded_bits, profile.fec_scheme)
    decoded = decoded[: (len(decoded) // 8) * 8]
    return DemodResult(data=bits_to_bytes(decoded), start_index=offset + start * sps,
                       phase_estimate=phase, prefix_correlation=mag)
