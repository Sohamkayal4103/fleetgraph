#!/usr/bin/env python3
"""Bounded RF capture analysis (Stage 14C.1).

Runs in the GNU Radio / system interpreter (which has numpy) — Aithernet's venv has no numpy.
Reads an interleaved CF32 IQ file and emits BOUNDED power statistics + an optional averaged PSD
as JSON on stdout (and a PSD CSV when requested). It does NOT classify emitters — an energy scan
measures aggregate activity only. No raw samples are echoed.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--threshold-db", type=float, default=-70.0)
    ap.add_argument("--psd-bins", type=int, default=1024)
    ap.add_argument("--psd-csv", default="")
    ap.add_argument("--max-samples", type=int, default=8_000_000)
    args = ap.parse_args()

    try:
        iq = np.fromfile(args.input, dtype=np.complex64, count=args.max_samples)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 2
    n = int(iq.size)
    if n == 0:
        print(json.dumps({"status": "error", "error": "empty capture"}))
        return 3

    power = (iq.real.astype(np.float64) ** 2 + iq.imag.astype(np.float64) ** 2)
    # dBFS relative to a full-scale magnitude of 1.0 (CF32 is normalized to [-1, 1]).
    eps = 1e-12
    power_db = 10.0 * np.log10(power + eps)
    median_db = float(np.median(power_db))
    p95_db = float(np.percentile(power_db, 95))
    mean_db = float(10.0 * np.log10(float(np.mean(power)) + eps))
    peak_db = float(np.max(power_db))
    occupancy = float(np.mean(power_db > args.threshold_db))
    dc = complex(np.mean(iq))

    # Averaged PSD (Welch-style, numpy only): segment, window, FFT, average magnitude^2.
    bins = max(64, min(args.psd_bins, 8192))
    psd = None
    if n >= bins:
        nseg = n // bins
        seg = iq[: nseg * bins].reshape(nseg, bins)
        win = np.hanning(bins).astype(np.float32)
        spec = np.fft.fftshift(np.fft.fft(seg * win, axis=1), axes=1)
        psd_lin = np.mean(np.abs(spec) ** 2, axis=0) / (bins * float(np.sum(win ** 2)))
        psd_db = 10.0 * np.log10(psd_lin + eps)
        freqs = np.fft.fftshift(np.fft.fftfreq(bins, d=1.0 / args.rate))
        psd = (freqs, psd_db)
        if args.psd_csv:
            with open(args.psd_csv, "w") as f:
                f.write("freq_offset_hz,power_db\n")
                for fr, pw in zip(freqs, psd_db, strict=False):
                    f.write(f"{fr:.1f},{pw:.3f}\n")

    out = {
        "status": "ok",
        "sample_count": n,
        "median_power_db": round(median_db, 3),
        "p95_power_db": round(p95_db, 3),
        "mean_power_db": round(mean_db, 3),
        "peak_power_db": round(peak_db, 3),
        "occupancy_fraction": round(occupancy, 4),
        "threshold_db": args.threshold_db,
        "dc_offset_mag": round(abs(dc), 5),
    }
    if psd is not None:
        out["psd_peak_db"] = round(float(np.max(psd[1])), 3)
        out["psd_bins"] = bins
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
