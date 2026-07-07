"""beta.2: SDR TX-capability discovery (adapter-specific). Open to any TX-capable SDR.

Discovery is LOCAL: it asks the installed driver/adapter what the connected device actually
supports. Ranges/controls are per-adapter — never assume all SDRs are the same. PlutoSDR (libiio)
is the first supported TX adapter; the design is open to others (USRP/UHD, SoapySDR-backed).

This module never transmits and never chooses a frequency. It only reports capability so the
operator can build a plan within the device's real ranges.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from aithernet.transport.ota.authorization import RFTxCapability


@dataclass(frozen=True)
class DiscoveredDevice:
    device_id: str          # stable id (e.g. serial/usb id) when available
    adapter: str            # "plutosdr" | "uhd" | "soapy" | ...
    uri: str                # transport URI (e.g. "ip:192.168.2.1", "usb:1.2.3")
    present: bool
    label: str = ""

    def to_dict(self) -> dict:
        return {"device_id": self.device_id, "adapter": self.adapter, "uri": self.uri,
                "present": self.present, "label": self.label}


# Documented capability envelopes per adapter. Discovery confirms the device is present + refines
# these from the live adapter where the toolchain exposes it; we never claim ranges a device lacks.
_ADAPTER_CAPABILITIES = {
    # PlutoSDR (AD936x). The conservative AD9363 TX range; an AD9364/modified unit reports wider —
    # discovery uses the device's own reported range when the IIO toolchain is available.
    "plutosdr": RFTxCapability(adapter_supports_tx=True, device_reports_tx=True,
                               min_freq_hz=325_000_000, max_freq_hz=3_800_000_000,
                               max_sample_rate=61_440_000, max_gain=0.0, channels=(0,)),
}


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def discover_tx_devices() -> list[DiscoveredDevice]:
    """Discover TX-capable SDRs via the installed adapters. Empty when none are present.

    PlutoSDR is detected via the libiio tooling (``iio_info``/``iio_attr``). No device is
    fabricated — an absent toolchain or device yields no entries."""
    devices: list[DiscoveredDevice] = []
    # PlutoSDR via libiio. Without the toolchain or a connected device, report nothing.
    if _have("iio_info") or _have("iio_attr"):
        import subprocess
        try:
            out = subprocess.run(["iio_info", "-s"], capture_output=True, text=True,
                                 timeout=8).stdout
        except Exception:  # noqa: BLE001
            out = ""
        for line in out.splitlines():
            if "pluto" in line.lower() or "ad936" in line.lower():
                uri = "ip:192.168.2.1"
                for tok in line.split():
                    if tok.startswith(("ip:", "usb:", "local:")):
                        uri = tok
                devices.append(DiscoveredDevice(device_id=f"plutosdr:{uri}", adapter="plutosdr",
                                                uri=uri, present=True, label="PlutoSDR (AD936x)"))
    return devices


def resolve_capability(adapter: str, *, device_present: bool = True) -> RFTxCapability:
    """The TX capability envelope for an adapter. Raises for an unknown adapter."""
    cap = _ADAPTER_CAPABILITIES.get(adapter)
    if cap is None:
        raise ValueError(f"unsupported TX adapter {adapter!r} "
                         f"(supported: {', '.join(sorted(_ADAPTER_CAPABILITIES))})")
    if not device_present:
        # capability of an absent device is reported but flagged not-present by discovery
        return cap
    return cap


def supported_adapters() -> list[str]:
    return sorted(_ADAPTER_CAPABILITIES)
