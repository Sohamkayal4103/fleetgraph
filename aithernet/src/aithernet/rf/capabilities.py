"""Hardware-neutral RF capability types and adapter conformance (Stage 14G, PHASE 8 #2).

Device-specific RF limits (e.g. the ADALM-PLUTO 70 MHz–6 GHz / 61.44 MS/s envelope) belong to a
hardware **adapter**, NOT the generic mission layer. An adapter declares :class:`RFCapabilities`;
the effective authorized envelope for a mission is the INTERSECTION of:

    global receive-only policy
    ∩ selected hardware adapter capabilities
    ∩ discovered device capabilities
    ∩ operator-configured policy
    ∩ requested mission limits

This module owns the capability type, its intersection algebra, and the per-adapter declarations.
Nothing here is claimed physically qualified — ``physically_qualified`` is ``False`` for every
adapter until that exact adapter has actually passed on real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Adapter names align with the customer hardware profiles (aithernet.provisioning.profiles).
SOFTWARE_ONLY = "software-only"
SIMULATION = "simulation"
PLUTOSDR = "plutosdr"
USRP = "usrp"
GENERIC_SOAPY = "generic-soapy"
FULL_LAB = "full-lab"


def _tighter_min(a: float | None, b: float | None) -> float | None:
    """The more restrictive lower bound (the larger min). None means 'unconstrained'."""
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def _tighter_max(a: float | None, b: float | None) -> float | None:
    """The more restrictive upper bound (the smaller max)."""
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


@dataclass(frozen=True)
class RFCapabilities:
    """A hardware-neutral capability envelope. ``None`` bounds mean 'this source does not constrain
    that axis' (e.g. a generic adapter defers the band to the discovered device)."""

    source: str                       # where these caps came from (adapter/device/operator name)
    rx_capable: bool = True
    requires_hardware: bool = False
    freq_min_hz: float | None = None
    freq_max_hz: float | None = None
    sample_rate_min: float | None = None
    sample_rate_max: float | None = None
    gain_min_db: float | None = None
    gain_max_db: float | None = None
    physically_qualified: bool = False
    notes: str = ""

    def intersect(self, other: RFCapabilities | None) -> RFCapabilities:
        """Narrow to the more restrictive of each axis. ``rx_capable`` is AND; ``requires_hardware``
        is OR; ``physically_qualified`` is AND (a chain is only qualified if every part is)."""
        if other is None:
            return self
        return RFCapabilities(
            source=f"{self.source}∩{other.source}",
            rx_capable=self.rx_capable and other.rx_capable,
            requires_hardware=self.requires_hardware or other.requires_hardware,
            freq_min_hz=_tighter_min(self.freq_min_hz, other.freq_min_hz),
            freq_max_hz=_tighter_max(self.freq_max_hz, other.freq_max_hz),
            sample_rate_min=_tighter_min(self.sample_rate_min, other.sample_rate_min),
            sample_rate_max=_tighter_max(self.sample_rate_max, other.sample_rate_max),
            gain_min_db=_tighter_min(self.gain_min_db, other.gain_min_db),
            gain_max_db=_tighter_max(self.gain_max_db, other.gain_max_db),
            physically_qualified=self.physically_qualified and other.physically_qualified,
            notes=(self.notes or other.notes),
        )

    def with_source(self, name: str) -> RFCapabilities:
        return replace(self, source=name)

    def to_dict(self) -> dict:
        return {
            "source": self.source, "rx_capable": self.rx_capable,
            "requires_hardware": self.requires_hardware,
            "freq_min_hz": self.freq_min_hz, "freq_max_hz": self.freq_max_hz,
            "sample_rate_min": self.sample_rate_min, "sample_rate_max": self.sample_rate_max,
            "gain_min_db": self.gain_min_db, "gain_max_db": self.gain_max_db,
            "physically_qualified": self.physically_qualified, "notes": self.notes,
        }


# Per-adapter capability declarations. NONE are physically_qualified yet.
ADAPTERS: dict[str, RFCapabilities] = {
    SOFTWARE_ONLY: RFCapabilities(
        SOFTWARE_ONLY, rx_capable=False, requires_hardware=False,
        notes="no RF front-end; software/coding/coordination only"),
    SIMULATION: RFCapabilities(
        SIMULATION, rx_capable=True, requires_hardware=False,
        freq_min_hz=1.0, freq_max_hz=6_000_000_000.0,
        sample_rate_min=1_000.0, sample_rate_max=61_440_000.0,
        gain_min_db=0.0, gain_max_db=70.0,
        notes="synthetic IQ source; bounds are nominal, no physical device"),
    PLUTOSDR: RFCapabilities(
        PLUTOSDR, rx_capable=True, requires_hardware=True,
        freq_min_hz=70_000_000.0, freq_max_hz=6_000_000_000.0,
        sample_rate_min=521_000.0, sample_rate_max=61_440_000.0,
        gain_min_db=0.0, gain_max_db=73.0,
        notes="ADALM-PLUTO (AD9363/AD9364) receive envelope"),
    USRP: RFCapabilities(
        USRP, rx_capable=True, requires_hardware=True,
        freq_min_hz=0.0, freq_max_hz=6_000_000_000.0,
        sample_rate_min=200_000.0, sample_rate_max=61_440_000.0,
        gain_min_db=0.0, gain_max_db=90.0,
        notes="Ettus USRP nominal envelope; exact range is device/daughterboard specific"),
    GENERIC_SOAPY: RFCapabilities(
        GENERIC_SOAPY, rx_capable=True, requires_hardware=True,
        notes="device-defined; band/rate/gain come from the discovered device's SoapySDR ranges"),
    FULL_LAB: RFCapabilities(
        FULL_LAB, rx_capable=True, requires_hardware=True,
        notes="multi-device lab; effective envelope comes from the selected device"),
}


def adapter_capabilities(name: str) -> RFCapabilities:
    """Resolve an adapter name (customer profile name or its alias) to its capabilities."""
    key = (name or "").strip().lower()
    if key in ADAPTERS:
        return ADAPTERS[key]
    # accept the internal/alias profile names too
    try:
        from aithernet.provisioning import profiles
        prof = profiles.resolve(key)
        if prof.name in ADAPTERS:
            return ADAPTERS[prof.name]
    except Exception:  # noqa: BLE001
        pass
    raise KeyError(f"unknown RF adapter '{name}' (choose: {', '.join(ADAPTERS)})")


#: Map a discovered device driver to its hardware adapter (for envelope resolution).
_DRIVER_ADAPTER = {
    "plutosdr": PLUTOSDR, "pluto": PLUTOSDR, "ad9361": PLUTOSDR, "ad9363": PLUTOSDR,
    "ad9363a": PLUTOSDR, "uhd": USRP, "usrp": USRP,
}


def adapter_for_driver(driver: str | None) -> str:
    """Resolve a device driver string to its adapter name. Unknown drivers → generic-soapy."""
    return _DRIVER_ADAPTER.get((driver or "").strip().lower(), GENERIC_SOAPY)


def device_capabilities_from_contract(caps, *, source: str = "device") -> RFCapabilities | None:
    """Build RFCapabilities from a canonical hardware DeviceCapabilities (best-effort, safe).

    Returns ``None`` when the device declares no usable ranges (so the adapter envelope applies
    unchanged — the existing working capture path is preserved).
    """
    if caps is None:
        return None
    freqs = getattr(caps, "frequency_ranges", None) or []
    rates = getattr(caps, "sample_rate_ranges", None) or []
    fmin = min((r.min_hz for r in freqs), default=None)
    fmax = max((r.max_hz for r in freqs), default=None)
    smin = min((r.min for r in rates), default=None)
    smax = max((r.max for r in rates), default=None)
    if fmin is None and fmax is None and smin is None and smax is None:
        # rx_supported may still be informative, but with no ranges there is nothing to narrow
        rx = getattr(caps, "rx_supported", None)
        if rx is False:
            return RFCapabilities(source=source, rx_capable=False, requires_hardware=True)
        return None
    return RFCapabilities(
        source=source, rx_capable=getattr(caps, "rx_supported", True) is not False,
        requires_hardware=True,
        freq_min_hz=fmin, freq_max_hz=fmax, sample_rate_min=smin, sample_rate_max=smax)


def device_capabilities_from_soapy(probe: dict, *, source: str = "device") -> RFCapabilities:
    """Build device capabilities from a SoapySDR probe dict (frequency/rate/gain ranges).

    Best-effort + conservative: only sets a bound when the probe actually reports it. Used to narrow
    the adapter envelope to the specific discovered device.
    """
    def _rng(key):
        r = probe.get(key) or {}
        lo = r.get("min")
        hi = r.get("max")
        return (float(lo) if lo is not None else None, float(hi) if hi is not None else None)

    fmin, fmax = _rng("frequency_range")
    smin, smax = _rng("sample_rate_range")
    gmin, gmax = _rng("gain_range")
    return RFCapabilities(
        source=source, rx_capable=True, requires_hardware=True,
        freq_min_hz=fmin, freq_max_hz=fmax,
        sample_rate_min=smin, sample_rate_max=smax,
        gain_min_db=gmin, gain_max_db=gmax,
        notes="discovered-device capabilities",
    )
