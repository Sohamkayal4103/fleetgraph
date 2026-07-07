"""Managed SDR hardware contracts: discovery interface, value objects, states, errors (Stage 14B).

These are the deterministic, vendor-neutral data types that flow between the discovery
providers, the inventory service, the lease service, and the backend-binding layer. They are
NOT ORM models (those live in :mod:`aithernet.state.models`) — they are the factual,
source-attributed value objects a provider returns and the service persists.

Design invariants:

* Discovery output is **untrusted input** — providers return only bounded, normalized facts;
  ``sanitize_metadata`` caps key/value sizes and count, and raw USB paths / environment maps /
  credentials never enter the coordinator-facing surface.
* Capabilities are **factual and source-attributed** — unknown stays ``None``/empty; a feature
  is never inferred from a vendor or product name.
* No type here imports a vendor-specific Python library; a provider shells out to a real
  discovery tool (or reads operator-declared descriptors) and maps the result onto these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# -- device kinds (extensible) ---------------------------------------------------------------
# A free-form, extensible string — NOT a closed enum — so a future optical/LiFi or replay
# frontend is representable without redesigning the core tables. These are the known constants.
DEVICE_KIND_SDR = "sdr"                 # ordinary RX/TX SDR transceiver
DEVICE_KIND_RX_ONLY = "rx_sdr"          # receive-only SDR
DEVICE_KIND_TX_ONLY = "tx_sdr"          # transmit-only device
DEVICE_KIND_CUSTOM_IQ = "custom_iq"     # custom IQ frontend
DEVICE_KIND_OPTICAL = "optical"         # optical / LiFi sample frontend
DEVICE_KIND_REPLAY = "replay"           # replay / file device
DEVICE_KIND_UNKNOWN = "unknown"


# -- presence / health / lifecycle states ----------------------------------------------------


class PresenceState:
    """Whether a discovered device is currently observed by its provider."""

    PRESENT = "present"
    MISSING = "missing"
    UNKNOWN = "unknown"


class DeviceStatus:
    """The inventory service's composite per-device status (Part F)."""

    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"  # provider unavailable / not installed
    DEGRADED = "degraded"
    ERROR = "error"
    DISABLED = "disabled"        # administratively disabled


class HealthState:
    """Factual device health classification (never inferred from a name)."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    MISSING = "missing"
    UNKNOWN = "unknown"


class AdminState:
    ENABLED = "enabled"
    DISABLED = "disabled"


class CapabilitySource:
    """Where a capability fact came from — for confidence/attribution."""

    PROBED = "probed"        # read from the device/provider at probe time
    DECLARED = "declared"    # operator-declared in static config
    UNKNOWN = "unknown"


# -- lease states / modes / direction --------------------------------------------------------


class LeaseState:
    PENDING = "pending"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"
    ORPHANED = "orphaned"


#: Terminal lease states — a lease in one of these never holds the device.
TERMINAL_LEASE_STATES = frozenset(
    {LeaseState.RELEASED, LeaseState.EXPIRED, LeaseState.REVOKED, LeaseState.FAILED}
)
#: States that hold (or may still hold) the device and so conflict with a new exclusive lease.
HOLDING_LEASE_STATES = frozenset({LeaseState.PENDING, LeaseState.ACTIVE, LeaseState.ORPHANED})


class LeaseMode:
    EXCLUSIVE = "exclusive"
    SHARED_RECEIVE = "shared_receive"


class Direction:
    RX = "rx"
    TX = "tx"
    RX_TX = "rx_tx"


# -- discovery value objects -----------------------------------------------------------------

_MAX_META_KEYS = 24
_MAX_META_KEY_LEN = 48
_MAX_META_VALUE_LEN = 256
#: metadata keys that must never be surfaced (raw paths / env / credentials / secrets).
_FORBIDDEN_META = (
    "path", "device_path", "usb", "bus", "environment", "env", "secret", "token",
    "password", "credential", "api_key", "auth", "uri", "endpoint", "address",
)


def sanitize_metadata(raw: dict | None) -> dict:
    """Bound and sanitize provider-supplied metadata before it is persisted/surfaced.

    Drops forbidden keys (raw paths, environment, credentials), caps key/value length, and
    caps the number of entries. Discovery output is untrusted; this is the choke point.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_META_KEYS:
            break
        if not isinstance(key, str):
            continue
        low = key.lower()
        if any(bad in low for bad in _FORBIDDEN_META):
            continue
        skey = key[:_MAX_META_KEY_LEN]
        if isinstance(value, bool) or value is None:
            out[skey] = str(value)
        elif isinstance(value, (int, float)):
            out[skey] = str(value)
        elif isinstance(value, str):
            out[skey] = value[:_MAX_META_VALUE_LEN]
        # non-scalar values are dropped (bounded, normalized data only)
    return out


@dataclass(frozen=True)
class DiscoveredDevice:
    """One device a discovery provider observed — only stable, bounded facts.

    ``hardware_key`` is the deterministic stable identity (see :mod:`aithernet.hardware.identity`)
    and is computed by the provider/service from the stable fields below, never from a transient
    enumeration index. ``identity_limited`` marks a device whose identity could not be pinned to a
    serial (so rediscovery may be less certain).
    """

    provider_id: str
    hardware_key: str
    device_kind: str = DEVICE_KIND_UNKNOWN
    vendor: str | None = None
    product: str | None = None
    serial: str | None = None
    driver: str | None = None
    transport: str | None = None      # bounded bus/transport DESCRIPTION (e.g. "usb", "pcie")
    channel: str | None = None        # channel / interface identity, when the device is per-channel
    display_name: str | None = None
    identity_limited: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FrequencyRange:
    min_hz: float
    max_hz: float


@dataclass(frozen=True)
class ValueRange:
    min: float
    max: float


@dataclass
class DeviceCapabilities:
    """Normalized, source-attributed device capabilities. Unknown stays unknown."""

    rx_supported: bool | None = None
    tx_supported: bool | None = None
    full_duplex: bool | None = None
    channel_count: int | None = None
    channel_directions: list[str] = field(default_factory=list)
    frequency_ranges: list[FrequencyRange] = field(default_factory=list)
    sample_rate_ranges: list[ValueRange] = field(default_factory=list)
    sample_rates: list[float] = field(default_factory=list)
    gain_ranges: list[ValueRange] = field(default_factory=list)
    gain_stages: list[str] = field(default_factory=list)
    antennas: list[str] = field(default_factory=list)
    bandwidth_ranges: list[ValueRange] = field(default_factory=list)
    clock_sources: list[str] = field(default_factory=list)
    time_sources: list[str] = field(default_factory=list)
    hardware_timestamp: bool | None = None
    stream_formats: list[str] = field(default_factory=list)
    driver: str | None = None
    #: Bounded, sanitized backend device-arguments an RF backend needs (e.g. {"driver": "uhd"}).
    device_args: dict = field(default_factory=dict)
    shared_receive_safe: bool = False
    source: str = CapabilitySource.UNKNOWN
    confidence: str = "unknown"  # high | medium | low | unknown
    #: Bounded extra normalized facts (e.g. per-direction RX/TX gain/antennas, uri, firmware).
    #: Merged into the persisted capabilities_json; never raw command output.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceHealth:
    """A factual health reading for a device at a point in time."""

    state: str = HealthState.UNKNOWN
    detail: str | None = None


# -- discovery backend interface -------------------------------------------------------------


@runtime_checkable
class HardwareDiscoveryBackend(Protocol):
    """A registered hardware discovery/capability provider (Part B).

    A provider is generic and vendor-neutral at this layer: it shells out to a real discovery
    tool (SoapySDR/UHD/a custom command) or reads operator-declared descriptors, and maps the
    result onto the value objects above. It never invents devices and never crashes node startup
    when its tool is absent — :meth:`available` returns ``False`` instead.
    """

    backend_id: str

    async def available(self) -> bool:
        """True when this provider's tool/API is usable in this environment."""
        ...

    async def discover(self) -> list[DiscoveredDevice]:
        """Return the devices currently observed (possibly empty); never raise on 'none found'."""
        ...

    async def probe(self, device: DiscoveredDevice) -> DeviceCapabilities:
        """Return factual capabilities for one device (unknown fields stay unknown)."""
        ...

    async def health(self, device: DiscoveredDevice) -> DeviceHealth:
        """Return a factual health reading for one device."""
        ...


# -- errors ----------------------------------------------------------------------------------


class HardwareError(Exception):
    """Base class for managed-hardware errors. Messages are operator-facing and sanitized."""


class DeviceNotFoundError(HardwareError):
    """A referenced device id is not in the persisted inventory."""


class DeviceUnavailableError(HardwareError):
    """A device exists but cannot be used now (disabled/missing/unhealthy) — a BLOCKED outcome."""


class LeaseNotFoundError(HardwareError):
    """A referenced lease id does not exist."""


class LeaseConflictError(HardwareError):
    """An incompatible/conflicting lease already holds the device — a deterministic refusal."""


class LeaseCompatibilityError(HardwareError):
    """The lease request is incompatible with the device's capabilities — a factual failure."""


class BindingNotFoundError(HardwareError):
    """No RF-backend binding exists for the (device, backend) pair."""


class ProviderError(HardwareError):
    """A discovery provider failed; surfaced sanitized (type only), never crashing the service."""
