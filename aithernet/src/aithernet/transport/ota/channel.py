"""Phase 2/3: synthetic impaired RF channel (pure Python, deterministic).

Models the impairments the demod must survive: a leading integer sample delay, a constant phase
rotation, an optional small carrier-frequency offset, and additive white Gaussian noise at a chosen
SNR. The channel PRESERVES the signal length apart from the explicit leading delay (no sample
insertion/deletion), which the correlation-based demod relies on. Used only for simulation.
"""

from __future__ import annotations

import cmath
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelModel:
    snr_db: float = 20.0
    phase_rad: float = 0.0
    delay_samples: int = 0
    freq_offset_cycles_per_sample: float = 0.0
    seed: int = 0

    def to_dict(self) -> dict:
        return {"snr_db": self.snr_db, "phase_rad": self.phase_rad,
                "delay_samples": self.delay_samples,
                "freq_offset_cycles_per_sample": self.freq_offset_cycles_per_sample,
                "seed": self.seed}


def _signal_power(samples: list[complex]) -> float:
    if not samples:
        return 0.0
    return sum((s.real * s.real + s.imag * s.imag) for s in samples) / len(samples)


def apply_channel(samples: list[complex], model: ChannelModel) -> list[complex]:
    """Return the impaired samples (leading delay, phase, freq offset, AWGN)."""
    rng = random.Random(model.seed)
    rot = cmath.exp(1j * model.phase_rad)
    out: list[complex] = [complex(0.0, 0.0)] * model.delay_samples
    w = 2 * math.pi * model.freq_offset_cycles_per_sample
    for n, s in enumerate(samples):
        out.append(s * rot * cmath.exp(1j * w * n))
    power = _signal_power(samples) or 1.0
    noise_var = power / (10 ** (model.snr_db / 10.0))
    sigma = math.sqrt(noise_var / 2.0)
    return [c + complex(rng.gauss(0.0, sigma), rng.gauss(0.0, sigma)) for c in out]


def measure_snr_db(clean: list[complex], noisy: list[complex]) -> float:
    """Estimate post-channel SNR from aligned clean vs noisy samples (analysis/reporting only)."""
    n = min(len(clean), len(noisy))
    if n == 0:
        return float("nan")
    sig = _signal_power(clean[:n]) or 1e-12
    err = sum(abs(noisy[i] - clean[i]) ** 2 for i in range(n)) / n or 1e-12
    return 10.0 * math.log10(sig / err)
