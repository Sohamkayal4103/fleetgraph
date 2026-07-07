"""Hardware discovery providers (Stage 14B, Part B).

Each provider implements :class:`HardwareDiscoveryBackend`. Production providers shell out to a
real discovery tool and map its output onto vendor-neutral value objects, or surface only what an
operator explicitly declared — they NEVER invent devices, and an absent tool reports
``available() == False`` instead of crashing. Discovery output is untrusted input: every field is
bounded and sanitized before it leaves this module.

Provider kinds:

* ``static``        — operator-declared device descriptors inline in config.
* ``static_file``   — operator-declared descriptors in a JSON file (re-read each cycle).
* ``soapy``         — SoapySDR command discovery (``SoapySDRUtil --find``).
* ``uhd``           — UHD/USRP discovery (``uhd_find_devices``).
* ``command``       — generic custom discovery command emitting a strict JSON schema on stdout.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from pathlib import Path

from aithernet.config.settings import HardwareDiscoveryProviderConfig
from aithernet.hardware.contracts import (
    DEVICE_KIND_SDR,
    CapabilitySource,
    DeviceCapabilities,
    DeviceHealth,
    DiscoveredDevice,
    FrequencyRange,
    HealthState,
    ValueRange,
    sanitize_metadata,
)
from aithernet.hardware.identity import compute_hardware_key, is_usable_serial, normalize_serial

_FORBIDDEN_ARG = ("path", "usb", "env", "secret", "token", "password", "credential", "endpoint")
_MAX_ARGS = 16

#: Soapy driver names that identify an ADALM-Pluto (one board reachable over usb: AND ip:).
_PLUTO_DRIVERS = frozenset({"plutosdr", "pluto"})


def _preferred_transport(transports: list[str]) -> str | None:
    """Pick the preferred transport/URI deterministically: usable local USB first, then a
    reachable network URI, else the first preserved alternative. Never reorders the preserved set.
    """
    if not transports:
        return None
    for t in transports:                      # 1) usable local USB on this workstation
        if t.lower().startswith("usb"):
            return t
    for t in transports:                      # 2) otherwise a reachable configured network URI
        if t.lower().startswith(("ip", "tcp")):
            return t
    return transports[0]                      # 3) fall back to the first preserved alternative


def merge_discovered_devices(devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """Collapse devices that share a stable ``hardware_key`` into ONE record per physical device.

    Two views merge ONLY because they computed the SAME stable key (same provider/driver/serial, or
    — when serial-less — the same stable transport facts). Differing serials produce different keys
    and stay separate. Every discovered transport/URI is preserved in ``metadata.transports`` and
    the deterministic preferred URI in ``metadata.preferred_transport``; the record's primary
    ``transport`` is set to the preferred URI. Order of first appearance is preserved (so the stable
    id and the merged record do not depend on discovery/enumeration order).
    """
    groups: dict[str, list[DiscoveredDevice]] = {}
    order: list[str] = []
    for dev in devices:
        if dev.hardware_key not in groups:
            groups[dev.hardware_key] = []
            order.append(dev.hardware_key)
        groups[dev.hardware_key].append(dev)

    out: list[DiscoveredDevice] = []
    for key in order:
        group = groups[key]
        base = group[0]
        transports: list[str] = []
        for d in group:
            if d.transport and d.transport not in transports:
                transports.append(d.transport)
        preferred = _preferred_transport(transports) or base.transport
        meta = dict(base.metadata or {})
        if transports:
            # Sanitizer-safe keys (no "uri"/"usb"/"address" substring); values are URI strings.
            meta["transports"] = ",".join(transports)
            meta["preferred_transport"] = preferred or ""
            meta["merged_views"] = str(len(group))
        out.append(dataclasses.replace(base, transport=preferred, metadata=meta))
    return out


def _s(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:128] or None


def sanitize_device_args(raw: dict | None) -> dict:
    """Bound + sanitize backend device-arguments (e.g. ``{"driver": "uhd", "serial": "30AD"}``).

    Drops forbidden keys (paths, environment, credentials, endpoints), caps count and value
    length, and keeps only scalar values — so a backend binding never carries a raw path or secret.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_ARGS or not isinstance(key, str):
            continue
        if any(bad in key.lower() for bad in _FORBIDDEN_ARG):
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key[:32]] = str(value)[:128]
    return out


def _ranges(value) -> list[ValueRange]:
    out: list[ValueRange] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append(ValueRange(float(item[0]), float(item[1])))
            elif isinstance(item, dict) and "min" in item and "max" in item:
                out.append(ValueRange(float(item["min"]), float(item["max"])))
    return out


def _freq_ranges(value) -> list[FrequencyRange]:
    return [FrequencyRange(r.min, r.max) for r in _ranges(value)]


def _str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v)[:48] for v in value][:32]
    return []


def descriptor_to_discovered(provider_id: str, d: dict) -> DiscoveredDevice:
    """Map one operator/tool descriptor dict to a :class:`DiscoveredDevice` with a stable key."""
    vendor = _s(d.get("vendor") or d.get("manufacturer"))
    product = _s(d.get("product") or d.get("name") or d.get("label"))
    serial = _s(d.get("serial"))
    driver = _s(d.get("driver") or d.get("type"))
    transport = _s(d.get("transport") or d.get("bus"))
    channel = _s(d.get("channel"))
    kind = _s(d.get("device_kind") or d.get("kind")) or DEVICE_KIND_SDR
    key, limited = compute_hardware_key(
        provider_id=provider_id, driver=driver, vendor=vendor, product=product,
        serial=serial, channel=channel, transport=transport,
    )
    display = _s(d.get("display_name")) or product or vendor or driver or "device"
    return DiscoveredDevice(
        provider_id=provider_id, hardware_key=key, device_kind=kind, vendor=vendor,
        product=product, serial=serial, driver=driver, transport=transport, channel=channel,
        display_name=display, identity_limited=limited,
        metadata=sanitize_metadata(d.get("metadata")),
    )


def descriptor_to_caps(
    d: dict, *, source: str = CapabilitySource.DECLARED, confidence: str = "high"
) -> DeviceCapabilities:
    """Map a descriptor's declared/probed capability facts to :class:`DeviceCapabilities`."""
    c = d.get("capabilities") if isinstance(d.get("capabilities"), dict) else {}
    args = sanitize_device_args(d.get("device_args") or ({"driver": d.get("driver")} if d.get(
        "driver") else {}))
    if d.get("serial") and "serial" not in args:
        args["serial"] = str(d["serial"])[:128]
    return DeviceCapabilities(
        rx_supported=c.get("rx") if isinstance(c.get("rx"), bool) else c.get("rx_supported"),
        tx_supported=c.get("tx") if isinstance(c.get("tx"), bool) else c.get("tx_supported"),
        full_duplex=c.get("full_duplex") if isinstance(c.get("full_duplex"), bool) else None,
        channel_count=c.get("channels") if isinstance(c.get("channels"), int)
        else c.get("channel_count"),
        channel_directions=_str_list(c.get("channel_directions")),
        frequency_ranges=_freq_ranges(c.get("frequency_ranges") or c.get("frequency_hz")),
        sample_rate_ranges=_ranges(c.get("sample_rate_ranges")),
        sample_rates=[float(v) for v in c.get("sample_rates", []) if isinstance(v, (int, float))],
        gain_ranges=_ranges(c.get("gain_ranges")),
        gain_stages=_str_list(c.get("gain_stages")),
        antennas=_str_list(c.get("antennas")),
        bandwidth_ranges=_ranges(c.get("bandwidth_ranges")),
        clock_sources=_str_list(c.get("clock_sources")),
        time_sources=_str_list(c.get("time_sources")),
        hardware_timestamp=c.get("hardware_timestamp")
        if isinstance(c.get("hardware_timestamp"), bool) else None,
        stream_formats=_str_list(c.get("stream_formats")),
        driver=_s(d.get("driver") or d.get("type")),
        device_args=args,
        shared_receive_safe=bool(c.get("shared_receive_safe", False)),
        source=source,
        confidence=confidence,
    )


# -- descriptor-based providers --------------------------------------------------------------


class _DescriptorProvider:
    """Shared behaviour for providers whose devices come from descriptor dicts."""

    def __init__(self, backend_id: str, config: HardwareDiscoveryProviderConfig) -> None:
        self.backend_id = backend_id
        self.config = config
        self._descriptors: dict[str, dict] = {}

    async def _load_descriptors(self) -> list[dict]:  # pragma: no cover - overridden
        return []

    async def discover(self) -> list[DiscoveredDevice]:
        descriptors = await self._load_descriptors()
        self._descriptors = {}
        out: list[DiscoveredDevice] = []
        for d in descriptors:
            if not isinstance(d, dict):
                continue
            device = descriptor_to_discovered(self.backend_id, d)
            self._descriptors[device.hardware_key] = d
            out.append(device)
        return out

    async def probe(self, device: DiscoveredDevice) -> DeviceCapabilities:
        d = self._descriptors.get(device.hardware_key, {})
        return descriptor_to_caps(d, source=CapabilitySource.DECLARED)

    async def health(self, device: DiscoveredDevice) -> DeviceHealth:
        d = self._descriptors.get(device.hardware_key)
        if d is None:
            return DeviceHealth(state=HealthState.MISSING, detail="not in current descriptors")
        declared = _s(d.get("health"))
        if declared in (HealthState.OK, HealthState.DEGRADED, HealthState.ERROR):
            return DeviceHealth(state=declared)
        return DeviceHealth(state=HealthState.OK)


class StaticDiscoveryBackend(_DescriptorProvider):
    """Devices declared inline in config (operator-declared facts; never invented)."""

    async def available(self) -> bool:
        return True

    async def _load_descriptors(self) -> list[dict]:
        return list(self.config.devices or [])


class StaticFileDiscoveryBackend(_DescriptorProvider):
    """Devices declared in a JSON descriptors file (re-read each cycle).

    The file may hold a list of descriptors or ``{"devices": [...]}``. A missing/empty/invalid
    file yields no devices (the provider stays available but reports nothing) — never an invention.
    """

    async def available(self) -> bool:
        return bool(self.config.descriptors_file)

    async def _load_descriptors(self) -> list[dict]:
        path = self.config.descriptors_file
        if not path or not Path(path).expanduser().is_file():
            return []
        try:
            data = json.loads(Path(path).expanduser().read_text())
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(data, dict):
            data = data.get("devices", [])
        return data if isinstance(data, list) else []


class CommandDiscoveryBackend(_DescriptorProvider):
    """Generic custom discovery command emitting a strict JSON schema on stdout (untrusted)."""

    async def available(self) -> bool:
        cmd = self.config.command
        return bool(cmd) and (
            shutil.which(cmd) is not None or Path(cmd).expanduser().exists()
        )

    async def _run(self) -> str | None:
        cmd = self.config.command
        if not cmd:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, *self.config.args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.timeout_seconds
            )
        except (TimeoutError, OSError):
            return None
        return out.decode("utf-8", "replace") if out else None

    async def _load_descriptors(self) -> list[dict]:
        raw = await self._run()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            data = data.get("devices", [])
        return data if isinstance(data, list) else []


class _RealToolProvider:
    """Base for real soapy/uhd providers: a cheap find pass + a deep per-device probe.

    Discovery (find) is cheap and runs every cycle; probing opens the hardware and is expensive,
    so ``probe_on_every_discovery`` is False — the inventory service probes only when a device has
    no capabilities yet or qualification forces it, and never disturbs a device that holds a lease.
    Duplicate tool listings for one physical board (e.g. SoapyPlutoSDR ``#0``/``#1`` at the same
    ``uri``) collapse to ONE device because identity uses stable facts, not the ``#N`` label.
    """

    FIND_COMMAND = ""
    FIND_ARGS: list[str] = []
    PROBE_COMMAND = ""
    probe_on_every_discovery = False

    def __init__(self, backend_id: str, config: HardwareDiscoveryProviderConfig) -> None:
        self.backend_id = backend_id
        self.config = config
        self._present: set[str] = set()

    def _find_command(self) -> str:
        return self.config.command or self.FIND_COMMAND

    def _probe_command(self) -> str:
        return self.PROBE_COMMAND

    async def available(self) -> bool:
        return shutil.which(self._find_command()) is not None

    async def _run(self, cmd: str, args: list[str], timeout: float) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, OSError):
            return None
        return out.decode("utf-8", "replace") if out else None

    def _descriptors_from_find(self, text: str) -> list[dict]:  # pragma: no cover - overridden
        return []

    async def _enrich_descriptors(self, descriptors: list[dict]) -> None:
        """Hook: enrich raw descriptors with extra STABLE identity before keying. Default: no-op.

        Read-only and additive — it may only fill stable identity facts (e.g. a hardware serial)
        on descriptors that lack them; it never starts an RF capture/probe and never invents data.
        """
        return

    async def discover(self) -> list[DiscoveredDevice]:
        args = self.config.args or self.FIND_ARGS
        text = await self._run(self._find_command(), args, self.config.timeout_seconds)
        self._present = set()
        if not text:
            return []
        descriptors = self._descriptors_from_find(text)
        await self._enrich_descriptors(descriptors)
        devices = [descriptor_to_discovered(self.backend_id, d) for d in descriptors]
        # Merge views that resolved to the SAME stable identity (e.g. one Pluto on usb: + ip:),
        # preserving every transport/URI; duplicates of one physical device collapse to ONE record.
        merged = merge_discovered_devices(devices)
        self._present = {d.hardware_key for d in merged}
        return merged

    def _probe_args(self, device: DiscoveredDevice) -> list[str]:  # pragma: no cover - overridden
        return []

    def _caps_from_probe(
        self, text: str, device: DiscoveredDevice
    ) -> DeviceCapabilities | None:  # pragma: no cover - overridden
        return None

    async def probe(self, device: DiscoveredDevice) -> DeviceCapabilities | None:
        """Open the device read-only and return real capabilities, or None on failure.

        Returning None (never a fabricated/partial capability set) means the inventory keeps the
        device's existing capabilities intact — a probe failure must not corrupt the record.
        """
        cmd = self._probe_command()
        if not cmd or shutil.which(cmd) is None:
            return None
        timeout = max(self.config.timeout_seconds * 2, 30.0)
        text = await self._run(cmd, self._probe_args(device), timeout)
        if not text:
            return None
        try:
            return self._caps_from_probe(text, device)
        except (ValueError, KeyError, IndexError):
            return None

    async def health(self, device: DiscoveredDevice) -> DeviceHealth:
        return DeviceHealth(
            state=HealthState.OK if device.hardware_key in self._present else HealthState.MISSING
        )


class SoapyDiscoveryBackend(_RealToolProvider):
    """SoapySDR discovery + probe via ``SoapySDRUtil --find`` / ``--probe`` (real tool)."""

    FIND_COMMAND = "SoapySDRUtil"
    FIND_ARGS = ["--find"]
    PROBE_COMMAND = "SoapySDRUtil"

    def _descriptors_from_find(self, text: str) -> list[dict]:
        from aithernet.hardware.probing import soapy_find_to_descriptors
        return soapy_find_to_descriptors(self.backend_id, text)

    async def _resolve_iio_serial(self, uri: str) -> str | None:
        """Resolve a Pluto's stable IIO ``hw_serial`` for one URI, read-only, or ``None``.

        Uses ``iio_attr -u <uri> -C`` (falling back to ``iio_info -u <uri>``) — context-attribute
        reads only. It opens NO RF stream, captures NO samples, and changes NO device setting.
        Returns the normalized serial, or ``None`` when no tool/serial is available (stay
        identity-limited rather than guess).
        """
        from aithernet.hardware.probing import iio_serial_from_attrs, parse_iio_context_attrs
        timeout = min(self.config.timeout_seconds, 10.0)
        for cmd, cmd_args in (("iio_attr", ["-u", uri, "-C"]), ("iio_info", ["-u", uri])):
            if shutil.which(cmd) is None:
                continue
            text = await self._run(cmd, cmd_args, timeout)
            if not text:
                continue
            serial = iio_serial_from_attrs(parse_iio_context_attrs(text))
            if serial:
                return normalize_serial(serial)
        return None

    async def _enrich_descriptors(self, descriptors: list[dict]) -> None:
        """Fill the stable IIO serial on serial-less Pluto descriptors so usb:/ip: views of one
        physical board key identically and merge. Conflicting stable evidence is kept separate.
        """
        cache: dict[str, str | None] = {}
        for d in descriptors:
            if (d.get("driver") or "").strip().lower() not in _PLUTO_DRIVERS:
                continue
            uri = d.get("transport")
            if not uri:
                continue
            if uri not in cache:
                cache[uri] = await self._resolve_iio_serial(uri)
            iio_serial = cache[uri]
            if not iio_serial:
                continue  # no stable serial resolvable -> stay identity-limited (fail closed later)
            existing = d.get("serial")
            md = dict(d.get("metadata") or {})
            if is_usable_serial(existing) and normalize_serial(existing) != iio_serial:
                # Conflicting stable evidence: never merge on a guess — keep the reported serial,
                # flag the conflict, and let the records stay separate.
                md["serial_conflict"] = True
                d["metadata"] = md
                continue
            d["serial"] = iio_serial
            md["identity_source"] = "iio"
            d["metadata"] = md

    def _device_args_string(self, device: DiscoveredDevice) -> str:
        parts = [f"driver={device.driver}"] if device.driver else []
        if device.serial:
            parts.append(f"serial={device.serial}")
        elif device.transport:
            parts.append(f"uri={device.transport}")
        return ",".join(parts)

    def _probe_args(self, device: DiscoveredDevice) -> list[str]:
        return [f"--probe={self._device_args_string(device)}"]

    def _caps_from_probe(self, text: str, device: DiscoveredDevice):
        from aithernet.hardware.probing import parse_soapy_probe, soapy_probe_to_capabilities
        return soapy_probe_to_capabilities(parse_soapy_probe(text), driver=device.driver)


class UhdDiscoveryBackend(_RealToolProvider):
    """UHD/USRP discovery + probe: ``uhd_find_devices`` / ``uhd_usrp_probe`` (real tool)."""

    FIND_COMMAND = "uhd_find_devices"
    FIND_ARGS: list[str] = []
    PROBE_COMMAND = "uhd_usrp_probe"

    def _descriptors_from_find(self, text: str) -> list[dict]:
        from aithernet.hardware.probing import uhd_find_to_descriptors
        return uhd_find_to_descriptors(self.backend_id, text)

    def _probe_args(self, device: DiscoveredDevice) -> list[str]:
        if device.serial:
            return [f"--args=serial={device.serial}"]
        if device.transport:
            return [f"--args=addr={device.transport}"]
        return []

    def _caps_from_probe(self, text: str, device: DiscoveredDevice):
        # A deep, validated UHD probe parser requires a real USRP (none attached during this
        # qualification). Until then UHD capabilities stay at find-level (driver/serial), confidence
        # low — never inferred from the model name. This is recorded honestly as not physically
        # qualified for UHD.
        from aithernet.hardware.contracts import CapabilitySource, DeviceCapabilities
        args = {"driver": device.driver} if device.driver else {}
        if device.serial:
            args["serial"] = device.serial
        return DeviceCapabilities(
            driver=device.driver, device_args=args,
            source=CapabilitySource.PROBED, confidence="low",
        )


_PROVIDER_KINDS = {
    "static": StaticDiscoveryBackend,
    "static_file": StaticFileDiscoveryBackend,
    "command": CommandDiscoveryBackend,
    "soapy": SoapyDiscoveryBackend,
    "uhd": UhdDiscoveryBackend,
}


def build_provider(backend_id: str, config: HardwareDiscoveryProviderConfig):
    """Construct the provider implementation for a configured discovery backend (or None)."""
    cls = _PROVIDER_KINDS.get(config.kind)
    if cls is None:
        return None
    return cls(backend_id, config)
