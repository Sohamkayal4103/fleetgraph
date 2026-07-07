#!/usr/bin/env python3
"""Bounded GNU Radio RX capture runner (Stage 14C.1).

Executed as a SUBPROCESS by Aithernet using a separately-configured GNU Radio interpreter (e.g.
/usr/bin/python3) — Aithernet's own venv never imports gnuradio. Builds a minimal RX flowgraph
(source -> head(N) -> file sink) that captures exactly ``rate * duration`` complex samples to
``--output`` as interleaved CF32 IQ, then prints a single JSON line of facts to stdout.

RX ONLY — it never creates a transmit block. The head block guarantees a finite capture. Device
addressing comes ONLY from bounded args the deterministic backend binding produced (a Soapy
``driver=...`` string or an IIO ``uri``), never a shell fragment. Two real source backends:

* ``iio_fmcomms2`` — the native ADALM-PLUTO / FMCOMMS2 source addressed by ``--uri`` (the robust
  path for PlutoSDR; avoids the SoapyPlutoSDR duplicate-enumeration ``make`` ambiguity).
* ``soapy`` — the generic SoapySDR source addressed by ``--device-args``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _build_iio(uri, freq, rate, gain, gr, iio, blocks, nsamples, output):
    src = iio.fmcomms2_source_fc32(uri, [True, True], 0x8000)
    src.set_frequency(int(freq))
    src.set_samplerate(int(rate))
    try:
        src.set_gain_mode(0, "manual")
        src.set_gain(0, int(gain))
    except Exception:  # noqa: BLE001 — optional per build
        pass
    head = blocks.head(gr.sizeof_gr_complex, nsamples)
    sink = blocks.file_sink(gr.sizeof_gr_complex, output, False)
    sink.set_unbuffered(False)
    tb = gr.top_block("aithernet_rx_capture", catch_exceptions=True)
    tb.connect((src, 0), head, sink)
    return tb


def _build_soapy(device_args, antenna, freq, rate, gain, gr, soapy, blocks, nsamples, output):
    src = soapy.source(device_args, "fc32", 1, "", "", [""], [""])
    src.set_sample_rate(0, rate)
    try:
        src.set_gain_mode(0, False)
    except Exception:  # noqa: BLE001
        pass
    src.set_frequency(0, freq)
    src.set_gain(0, gain)
    if antenna:
        try:
            src.set_antenna(0, antenna)
        except Exception:  # noqa: BLE001
            pass
    head = blocks.head(gr.sizeof_gr_complex, nsamples)
    sink = blocks.file_sink(gr.sizeof_gr_complex, output, False)
    sink.set_unbuffered(False)
    tb = gr.top_block("aithernet_rx_capture", catch_exceptions=True)
    tb.connect(src, head, sink)
    return tb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("iio_fmcomms2", "soapy"), default="iio_fmcomms2")
    ap.add_argument("--uri", default="", help="IIO context uri, e.g. ip:pluto.local")
    ap.add_argument("--device-args", default="", help="Soapy device args, e.g. driver=plutosdr")
    ap.add_argument("--freq", type=float, required=True, help="Center frequency (Hz)")
    ap.add_argument("--rate", type=float, required=True, help="Sample rate (S/s)")
    ap.add_argument("--gain", type=float, required=True, help="Manual RX gain (dB)")
    ap.add_argument("--duration", type=float, required=True, help="Capture duration (s)")
    ap.add_argument("--output", required=True, help="Output IQ file path (CF32)")
    ap.add_argument("--antenna", default="", help="RX antenna name (Soapy; optional)")
    args = ap.parse_args()

    try:
        from gnuradio import blocks, gr
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "error": "gnuradio import failed"}))
        return 3

    nsamples = int(args.rate * args.duration)
    if nsamples <= 0:
        print(json.dumps({"status": "error", "error": "non-positive sample count"}))
        return 2

    started = time.time()
    try:
        if args.source == "iio_fmcomms2":
            from gnuradio import iio
            uri = args.uri or "ip:pluto.local"
            tb = _build_iio(uri, args.freq, args.rate, args.gain, gr, iio, blocks, nsamples,
                            args.output)
        else:
            from gnuradio import soapy
            tb = _build_soapy(args.device_args, args.antenna, args.freq, args.rate, args.gain,
                              gr, soapy, blocks, nsamples, args.output)
        tb.start()
        tb.wait()
        tb.stop()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "error": "flowgraph failed"}))
        return 4
    elapsed = time.time() - started

    size = os.path.getsize(args.output) if os.path.exists(args.output) else 0
    print(json.dumps({
        "status": "ok",
        "source": args.source,
        "expected_samples": nsamples,
        "bytes": size,
        "sample_count": size // 8,  # CF32 = 8 bytes/sample
        "elapsed_seconds": round(elapsed, 3),
        "sample_format": "CF32",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
