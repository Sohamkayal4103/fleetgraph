"""Real SoapySDR / UHD provider probing parsers (Stage 14C.1, Part B).

Pure, deterministic, bounded parsers that turn a discovery/probe tool's text output into
vendor-neutral facts. They treat tool output as UNTRUSTED, parse only bounded structured fields,
never expose raw full output, never infer an unsupported capability from a product name, and leave
unknown facts unknown. Identity uses stable facts (provider/driver/URI/serial) — never a
``#N`` enumeration label or list index — so a device a tool lists twice (e.g. SoapyPlutoSDR
reporting ``PlutoSDR #0`` and ``#1`` for one board at the same ``uri``) collapses to ONE device.
"""

from __future__ import annotations

import re

from aithernet.hardware.contracts import (
    DEVICE_KIND_SDR,
    CapabilitySource,
    DeviceCapabilities,
    FrequencyRange,
    ValueRange,
)

# -- SoapySDR --find parsing -----------------------------------------------------------------

_FOUND_RE = re.compile(r"^Found device\b")

#: SoapySDR factory drivers that are NOT physical RF SDRs and must never be counted as RF hardware
#: (the canonical exclusion every discovery surface uses). `audio` covers the WSL/RDP endpoints
#: (RDP Source, Monitor of RDP Sink) misreported as SDRs in beta.7.
NON_RF_SOAPY_DRIVERS = frozenset({"audio", "null", "none", "file", "csv", "loopback", "remote"})


def is_rf_driver(driver: str | None) -> bool:
    """True when a SoapySDR driver represents physical RF hardware (not audio/null/file/etc.)."""
    return bool(driver) and driver.strip().lower() not in NON_RF_SOAPY_DRIVERS


def parse_soapy_find(text: str) -> list[dict]:
    """Parse ``SoapySDRUtil --find`` output into bounded ``key = value`` blocks (one per device)."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _FOUND_RE.match(line):
            current = {}
            blocks.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key:
            current[key] = value.strip()[:160]
    return [b for b in blocks if b]


def soapy_find_to_descriptors(provider_id: str, text: str) -> list[dict]:
    """Map ``--find`` blocks to device descriptors keyed on STABLE facts (never the ``#N`` label).

    ``device`` (e.g. ``PlutoSDR``) is the product, ``uri`` is the stable transport/address, and a
    ``serial`` is used only when the tool actually reports one. The ``label`` (which carries the
    transient ``#0``/``#1`` index) is deliberately NOT used for product or identity.
    """
    out: list[dict] = []
    for b in parse_soapy_find(text):
        if not is_rf_driver(b.get("driver")):
            continue  # skip non-RF Soapy factories (audio/null/file/...) — never an SDR
        out.append({
            "driver": b.get("driver"),
            "vendor": b.get("manufacturer"),
            # product comes from the stable `device` field, NOT the `#N` label.
            "product": b.get("device") or b.get("product"),
            "serial": b.get("serial"),
            "transport": b.get("uri"),       # stable address (e.g. ip:pluto.local) — dedup key
            "device_kind": DEVICE_KIND_SDR,
            "display_name": b.get("device") or b.get("driver"),
        })
    return out


# -- IIO context identity (libiio) -----------------------------------------------------------
#
# SoapySDR ``--find`` does not surface a PlutoSDR's per-unit serial, so the canonical key falls
# back to transport facts and the SAME physical board reachable over both ``usb:`` and ``ip:``
# appears as two identity-limited devices. ``iio_attr -u <uri> -C`` / ``iio_info -u <uri>`` DO
# expose the board's stable ``hw_serial``. These parsers turn that read-only context-attribute
# output into the stable serial used to merge transports of one physical device.

_IIO_KV_RE = re.compile(r"^\s*([A-Za-z0-9_][\w,.\-]*)\s*[:=]\s*(.+?)\s*$")


def parse_iio_context_attrs(text: str) -> dict:
    """Parse ``iio_attr -C`` / ``iio_info`` context-attribute lines into a bounded ``{key: value}``.

    Tolerates both ``key: value`` and ``key = value`` forms and ignores headers/noise. Treats the
    output as UNTRUSTED: keys/values are bounded and only the first occurrence of a key is kept.
    """
    attrs: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("iio context"):
            continue
        m = _IIO_KV_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip().lower()[:64]
        if key and key not in attrs:
            attrs[key] = m.group(2).strip()[:160]
    return attrs


def iio_serial_from_attrs(attrs: dict) -> str | None:
    """Return the stable per-unit serial from parsed IIO context attributes, or ``None``.

    Prefers ``hw_serial`` (the board's burned-in identity) and falls back to ``usb,serial`` —
    never a model, hostname, or firmware string.
    """
    if not isinstance(attrs, dict):
        return None
    return attrs.get("hw_serial") or attrs.get("usb,serial") or None


# -- SoapySDR --probe parsing ----------------------------------------------------------------

_SECTION_RE = re.compile(r"^--\s*(.+?)\s*$")
_BRACKET_RANGE_RE = re.compile(r"\[\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\]")


def _split_sections(text: str) -> dict[str, list[str]]:
    """Split probe output into ``title -> [lines]`` sections by the ``-- Title --`` rows."""
    sections: dict[str, list[str]] = {}
    title = "_head"
    sections[title] = []
    lines = (text or "").splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if set(line) == {"-"} and line:  # a separator rule line
            # the title is the NEXT non-rule line
            continue
        m = _SECTION_RE.match(line)
        # A section header looks like "-- Device identification" with rule lines around it.
        if line.startswith("-- ") and i + 1 < len(lines):
            title = line[3:].strip()
            sections.setdefault(title, [])
            continue
        if m and line.startswith("--"):
            title = m.group(1).strip()
            sections.setdefault(title, [])
            continue
        sections.setdefault(title, []).append(line)
    return sections


def _to_hz(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit.startswith("ghz"):
        return value * 1e9
    if unit.startswith("mhz") or unit.startswith("msps"):
        return value * 1e6
    if unit.startswith("khz") or unit.startswith("ksps"):
        return value * 1e3
    return value


def _parse_range_line(line: str) -> tuple[float, float] | None:
    m = _BRACKET_RANGE_RE.search(line)
    if not m:
        return None
    unit = line.rsplit("]", 1)[-1].strip()
    return _to_hz(float(m.group(1)), unit), _to_hz(float(m.group(2)), unit)


def _parse_list_after_colon(line: str) -> list[str]:
    value = line.split(":", 1)[1].strip() if ":" in line else ""
    return [p.strip() for p in value.split(",") if p.strip()]


def parse_soapy_probe(text: str) -> dict:
    """Parse ``SoapySDRUtil --probe`` into bounded structured facts (no raw output retained)."""
    sections = _split_sections(text)
    info: dict = {"identification": {}, "rx": None, "tx": None}

    for line in sections.get("Device identification", []):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        info["identification"][key.strip().lower()] = value.strip()[:160]

    periph = sections.get("Peripheral summary", [])
    rx_count = tx_count = 0
    timestamps = None
    for line in periph:
        low = line.lower()
        if low.startswith("channels:"):
            mrx = re.search(r"(\d+)\s*rx", low)
            mtx = re.search(r"(\d+)\s*tx", low)
            rx_count = int(mrx.group(1)) if mrx else 0
            tx_count = int(mtx.group(1)) if mtx else 0
        elif low.startswith("timestamps:"):
            timestamps = "yes" in low
    info["rx_channels"] = rx_count
    info["tx_channels"] = tx_count
    info["hardware_timestamp"] = timestamps

    for direction, title in (("rx", "RX Channel 0"), ("tx", "TX Channel 0")):
        chan_lines = sections.get(title)
        if not chan_lines:
            continue
        chan: dict = {}
        for line in chan_lines:
            low = line.lower()
            if low.startswith("full-duplex:"):
                chan["full_duplex"] = "yes" in low
            elif low.startswith("stream formats:"):
                chan["stream_formats"] = _parse_list_after_colon(line)
            elif low.startswith("antennas:"):
                chan["antennas"] = _parse_list_after_colon(line)
            elif "full gain range:" in low:
                rng = _parse_range_line(line)
                if rng:
                    chan["gain_range"] = list(rng)
            elif "full freq range:" in low:
                rng = _parse_range_line(line)
                if rng:
                    chan["frequency_range"] = list(rng)
            elif low.startswith("sample rates:"):
                rng = _parse_range_line(line)
                if rng:
                    chan["sample_rate_range"] = list(rng)
                else:
                    unit = line.rsplit(" ", 1)[-1]
                    vals = []
                    for tok in _parse_list_after_colon(line):
                        num = re.findall(r"[-\d.eE]+", tok)
                        if num:
                            vals.append(_to_hz(float(num[0]), unit))
                    chan["sample_rates"] = vals
            elif low.startswith("filter bandwidths:"):
                unit = line.rsplit(" ", 1)[-1]
                bws = []
                for tok in _parse_list_after_colon(line):
                    num = re.findall(r"[-\d.eE]+", tok)
                    if num:
                        bws.append(_to_hz(float(num[0]), unit))
                chan["bandwidths"] = bws
        info[direction] = chan
    return info


def soapy_probe_serial(probe: dict) -> str | None:
    ident = probe.get("identification", {})
    return ident.get("hw_serial") or ident.get("serial") or None


def soapy_probe_to_capabilities(probe: dict, *, driver: str | None = None) -> DeviceCapabilities:
    """Build a source-attributed :class:`DeviceCapabilities` from parsed probe facts.

    Represents RX and TX as directions of the SAME physical device. Shared facts (frequency,
    sample-rate range) populate the top-level fields used by lease compatibility; per-direction
    detail (RX vs TX gain/antennas) is preserved in ``extra`` for the report/UI. Unknown stays
    unknown — nothing is inferred from the product name.
    """
    rx = probe.get("rx") or {}
    tx = probe.get("tx") or {}
    rx_count = probe.get("rx_channels") or 0
    tx_count = probe.get("tx_channels") or 0
    directions = []
    if rx_count:
        directions.append("rx")
    if tx_count:
        directions.append("tx")

    # shared frequency / sample-rate (prefer RX, fall back to TX)
    freq = rx.get("frequency_range") or tx.get("frequency_range")
    sr = rx.get("sample_rate_range") or tx.get("sample_rate_range")
    sr_values = rx.get("sample_rates") or tx.get("sample_rates") or []
    antennas = list(dict.fromkeys((rx.get("antennas") or []) + (tx.get("antennas") or [])))
    formats = list(dict.fromkeys(
        (rx.get("stream_formats") or []) + (tx.get("stream_formats") or [])))
    full_duplex = rx.get("full_duplex") if rx.get("full_duplex") is not None else tx.get(
        "full_duplex")

    ident = probe.get("identification", {})
    extra = {
        "serial": soapy_probe_serial(probe),
        "uri": ident.get("uri"),
        "hardware": ident.get("hardware"),
        "hw_model": ident.get("hw_model"),
        "firmware": ident.get("fw_version"),
        "rx": {"gain_range": rx.get("gain_range"), "antennas": rx.get("antennas"),
               "bandwidths": rx.get("bandwidths"), "full_duplex": rx.get("full_duplex")},
        "tx": {"gain_range": tx.get("gain_range"), "antennas": tx.get("antennas"),
               "bandwidths": tx.get("bandwidths"), "full_duplex": tx.get("full_duplex")},
    }
    device_args = {"driver": driver or ident.get("driver", "").lower() or None}
    if ident.get("uri"):
        device_args["uri"] = ident["uri"]
    device_args = {k: v for k, v in device_args.items() if v}

    return DeviceCapabilities(
        rx_supported=bool(rx_count) if (rx_count or tx_count) else None,
        tx_supported=bool(tx_count) if (rx_count or tx_count) else None,
        full_duplex=full_duplex,
        channel_count=max(rx_count, tx_count) or None,
        channel_directions=directions,
        frequency_ranges=[FrequencyRange(freq[0], freq[1])] if freq else [],
        sample_rate_ranges=[ValueRange(sr[0], sr[1])] if sr else [],
        sample_rates=[float(v) for v in sr_values],
        gain_ranges=[ValueRange(*rx["gain_range"])] if rx.get("gain_range") else (
            [ValueRange(*tx["gain_range"])] if tx.get("gain_range") else []),
        gain_stages=[],
        antennas=antennas,
        bandwidth_ranges=[],
        clock_sources=[],
        time_sources=[],
        hardware_timestamp=probe.get("hardware_timestamp"),
        stream_formats=formats,
        driver=driver or (ident.get("driver", "").lower() or None),
        device_args=device_args,
        shared_receive_safe=False,  # never assume safe; an operator declares shared-RX safety
        source=CapabilitySource.PROBED,
        confidence="high",
        extra={k: v for k, v in extra.items() if v},
    )


# -- UHD parsing (uhd_find_devices / uhd_usrp_probe) -----------------------------------------


def parse_uhd_find(text: str) -> list[dict]:
    """Parse ``uhd_find_devices`` ``key: value`` device-address blocks."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("--") and "UHD Device" in line:
            current = {}
            blocks.append(current)
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("serial", "name", "product", "type", "addr", "fpga", "mgmt_addr"):
            current[key] = value.strip()[:160]
    return [b for b in blocks if b]


def uhd_find_to_descriptors(provider_id: str, text: str) -> list[dict]:
    out: list[dict] = []
    for b in parse_uhd_find(text):
        out.append({
            "driver": b.get("type"),
            "vendor": "Ettus",
            "product": b.get("product") or b.get("name"),
            "serial": b.get("serial"),
            "transport": b.get("addr"),
            "device_kind": DEVICE_KIND_SDR,
            "display_name": b.get("name") or b.get("product"),
        })
    return out
