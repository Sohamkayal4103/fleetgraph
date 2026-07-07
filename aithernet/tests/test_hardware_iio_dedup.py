"""IIO-identity deduplication for Pluto discovery (software-only, no hardware).

A single physical ADALM-Pluto is reachable over BOTH ``usb:`` and ``ip:`` transports, but
``SoapySDRUtil --find`` does not surface its serial — so the canonical key fell back to transport
facts and ONE board appeared as TWO identity-limited devices. This fix resolves the stable IIO
``hw_serial`` per URI (read-only) and merges transports of one physical board, while never merging
on a guess. These tests drive the canonical ``SoapyDiscoveryBackend`` with an injected fake command
runner (no SoapySDR/libiio/RF subprocess is ever started) and a sanitized serial fixture.
"""

from __future__ import annotations

import asyncio

import pytest

from aithernet.config.settings import HardwareDiscoveryProviderConfig
from aithernet.hardware import discovery as discovery_mod
from aithernet.hardware.discovery import (
    SoapyDiscoveryBackend,
    _preferred_transport,
    merge_discovered_devices,
)
from aithernet.hardware.probing import iio_serial_from_attrs, parse_iio_context_attrs

run = asyncio.run

# -- sanitized fixtures --------------------------------------------------------------------------

_FIND_USB_AND_IP = """\
Found device 0
  driver = plutosdr
  device = PlutoSDR
  label = PlutoSDR #0 usb:3.5.5
  uri = usb:3.5.5

Found device 1
  driver = plutosdr
  device = PlutoSDR
  label = PlutoSDR #0 ip:pluto.local
  uri = ip:pluto.local

Found device 2
  driver = plutosdr
  device = PlutoSDR
  label = PlutoSDR #1 ip:pluto.local
  uri = ip:pluto.local
"""

# real iio_attr -C shape (sanitized serials)
_IIO_SERIAL_A = "03df62aa27d3142b33d76aa22fcb0c332b"
_IIO_SERIAL_B = "ffaa11bb22cc33dd44ee55ff66001122ab"


def _iio_text(serial: str) -> str:
    return (
        "IIO context with 15 attributes:\n"
        "hw_model: Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)\n"
        f"hw_serial: {serial}\n"
        "fw_version: v0.39-dirty\n"
        "ad9361-phy,model: ad9363a\n"
    )


class _FakeSoapy(SoapyDiscoveryBackend):
    """A SoapyDiscoveryBackend whose subprocess layer is replaced by canned text.

    ``serial_for`` maps a URI -> serial (or None) for the IIO identity step. Every command the
    backend would run is recorded in ``self.calls`` so a test can prove NO RF probe/capture runs.
    """

    def __init__(self, find_text, serial_for, **kw):
        super().__init__("pluto-soapy", HardwareDiscoveryProviderConfig(kind="soapy"))
        self._find_text = find_text
        self._serial_for = serial_for
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def _run(self, cmd, args, timeout):  # type: ignore[override]
        self.calls.append((cmd, tuple(args)))
        if cmd == "SoapySDRUtil":
            return self._find_text
        if cmd in ("iio_attr", "iio_info"):
            uri = args[args.index("-u") + 1]
            serial = self._serial_for.get(uri)
            return _iio_text(serial) if serial else "IIO context with 0 attributes:\n"
        return None


@pytest.fixture(autouse=True)
def _iio_tools_present(monkeypatch):
    # Pretend iio_attr/iio_info exist; SoapySDRUtil too. No real binary is invoked (._run is faked).
    monkeypatch.setattr(discovery_mod.shutil, "which", lambda c: f"/usr/bin/{c}")


def _discover(find_text, serial_for):
    be = _FakeSoapy(find_text, serial_for)
    devices = run(be.discover())
    return be, devices


# -- pure parsers --------------------------------------------------------------------------------

def test_parse_iio_context_attrs_and_serial():
    attrs = parse_iio_context_attrs(_iio_text(_IIO_SERIAL_A))
    assert attrs["hw_serial"] == _IIO_SERIAL_A
    assert iio_serial_from_attrs(attrs) == _IIO_SERIAL_A
    assert iio_serial_from_attrs({}) is None


# -- merge behavior ------------------------------------------------------------------------------

def test_usb_and_ip_same_serial_merge_into_one_device():
    _, devices = _discover(_FIND_USB_AND_IP,
                           {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    assert len(devices) == 1
    dev = devices[0]
    assert dev.identity_limited is False
    assert dev.serial == _IIO_SERIAL_A
    assert dev.metadata.get("identity_source") == "iio"


def test_duplicate_ip_views_merge():
    # both ip views (#0/#1) collapse even with no serial, by stable transport identity
    find = "\n".join(_FIND_USB_AND_IP.splitlines()[5:])  # drop the usb block, keep the two ip views
    _, devices = _discover(find, {"ip:pluto.local": None})
    assert len(devices) == 1
    assert devices[0].metadata.get("transports") == "ip:pluto.local"


def test_different_serials_stay_separate():
    _, devices = _discover(_FIND_USB_AND_IP,
                           {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_B})
    serials = {d.serial for d in devices}
    assert len(devices) == 2
    assert serials == {_IIO_SERIAL_A, _IIO_SERIAL_B}


def test_missing_serial_does_not_merge_across_transports():
    _, devices = _discover(_FIND_USB_AND_IP, {"usb:3.5.5": None, "ip:pluto.local": None})
    # usb stays separate from ip when neither resolves a serial; ip #0/#1 still collapse
    transports = sorted(d.transport for d in devices)
    assert len(devices) == 2
    assert transports == ["ip:pluto.local", "usb:3.5.5"]
    assert all(d.identity_limited for d in devices)


def test_conflicting_serial_evidence_fails_safe():
    # soapy reports a serial; IIO reports a DIFFERENT one for the same URI -> never merge on a guess
    find = (
        "Found device 0\n  driver = plutosdr\n  device = PlutoSDR\n"
        "  serial = soapyreportedserial0001\n  uri = usb:3.5.5\n\n"
        "Found device 1\n  driver = plutosdr\n  device = PlutoSDR\n  uri = ip:pluto.local\n"
    )
    _, devices = _discover(find, {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    usb = [d for d in devices if d.transport == "usb:3.5.5"]
    assert len(usb) == 1
    assert usb[0].serial == "soapyreportedserial0001"  # original kept, not overwritten
    assert usb[0].metadata.get("serial_conflict") == "True"
    # the conflicting usb record is NOT merged with the ip record
    assert len(devices) == 2


def test_all_uris_retained_in_merged_record():
    _, devices = _discover(_FIND_USB_AND_IP,
                           {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    transports = devices[0].metadata["transports"].split(",")
    assert "usb:3.5.5" in transports
    assert "ip:pluto.local" in transports


def test_usb_transport_preferred_by_policy():
    _, devices = _discover(_FIND_USB_AND_IP,
                           {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    assert devices[0].transport == "usb:3.5.5"
    assert devices[0].metadata["preferred_transport"] == "usb:3.5.5"
    # policy unit check: network only -> first network URI; nothing -> first alternative
    assert _preferred_transport(["ip:pluto.local", "usb:3.5.5"]) == "usb:3.5.5"
    assert _preferred_transport(["ip:pluto.local", "tcp:10.0.0.5"]) == "ip:pluto.local"
    assert _preferred_transport(["serial:/x"]) == "serial:/x"


def test_discovery_order_does_not_change_stable_id():
    _, a = _discover(_FIND_USB_AND_IP,
                     {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    # reversed enumeration order (ip blocks first, usb last)
    reordered = "\n".join([
        "Found device 0\n  driver = plutosdr\n  device = PlutoSDR\n  uri = ip:pluto.local\n",
        "Found device 1\n  driver = plutosdr\n  device = PlutoSDR\n  uri = usb:3.5.5\n",
    ])
    _, b = _discover(reordered, {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    assert a[0].hardware_key == b[0].hardware_key


def test_reconnect_produces_same_stable_id():
    _, first = _discover(_FIND_USB_AND_IP,
                         {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    _, second = _discover(_FIND_USB_AND_IP,
                          {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    assert first[0].hardware_key == second[0].hardware_key


def test_simulation_cannot_merge_with_physical_pluto():
    from aithernet.hardware.discovery import descriptor_to_discovered
    pluto = descriptor_to_discovered(
        "pluto-soapy",
        {"driver": "plutosdr", "product": "PlutoSDR", "serial": _IIO_SERIAL_A,
         "transport": "usb:3.5.5"})
    sim = descriptor_to_discovered(
        "sim", {"driver": "simulation", "product": "PlutoSDR", "serial": _IIO_SERIAL_A,
                "transport": "usb:3.5.5"})
    assert pluto.hardware_key != sim.hardware_key
    merged = merge_discovered_devices([pluto, sim])
    assert len(merged) == 2  # different provider/driver -> never merged


def test_no_rf_subprocess_is_started_during_discovery():
    be, _ = _discover(_FIND_USB_AND_IP,
                      {"usb:3.5.5": _IIO_SERIAL_A, "ip:pluto.local": _IIO_SERIAL_A})
    cmds = [c for c, _ in be.calls]
    # only --find and IIO identity reads; never a SoapySDR --probe or any gr/capture subprocess
    assert "SoapySDRUtil" in cmds
    assert any(c in ("iio_attr", "iio_info") for c in cmds)
    for cmd, args in be.calls:
        joined = " ".join(args)
        assert "--probe" not in joined, f"discovery must not probe RF: {cmd} {joined}"
        assert "capture" not in joined.lower()
        assert cmd not in ("python", "python3", "/usr/bin/python3")
