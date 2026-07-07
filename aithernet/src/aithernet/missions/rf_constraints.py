"""Deterministic receive-only RF mission constraints (Stage 14G, PHASE 8).

The AI coordinator may PROPOSE or refine an RF mission, but it can never widen these bounds: an RF
mission request is validated here, deterministically, into a frozen :class:`AuthorizedCapturePlan`
that the executor consumes. This module is the single gate that enforces:

* **receive-only** — any transmit direction/intent is rejected; there is no TX command path;
* frequency within the authorized band;
* sample-rate, duration, gain within bounds;
* device authorization (optional allowlist);
* artifact size/count limits (clamped down, never widened).

It is pure (no I/O, no hardware, no model calls) so the safety guarantee is unit-testable and holds
independently of the coordinator. The returned plan carries a deterministic digest for audit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

# Receive-only is the ONLY accepted direction. These are explicitly rejected.
_TX_DIRECTIONS = {"tx", "rx_tx", "txrx", "transmit", "both"}
RX = "rx"


class ConstraintViolation(ValueError):
    """A deterministic constraint was violated. ``category`` is a stable machine string."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class GlobalRxPolicy:
    """Hardware-NEUTRAL node/operator policy. Carries no device-specific RF band/rate — those come
    from the hardware adapter + the discovered device. Receive-only is always implied.
    """

    duration_max_s: float = 10.0                # matches CaptureConfig.max_duration_seconds
    artifact_max_bytes: int = 256 * 1024 * 1024  # 256 MiB hard ceiling per mission
    artifact_max_count: int = 8
    allowed_devices: tuple[str, ...] = ()       # empty = any discovered device is allowed


@dataclass(frozen=True)
class ReceiveOnlyBounds:
    """The RESOLVED effective envelope for one mission — the intersection of the global policy, the
    selected hardware adapter, the discovered device, and any operator policy. It is built
    by :func:`resolve_bounds`, NEVER hand-defaulted to a specific device. ``None`` frequency/rate
    bounds mean the envelope is undetermined (no adapter/device caps) and capture is refused.
    """

    adapter: str = ""
    rx_capable: bool = False
    requires_hardware: bool = False
    physically_qualified: bool = False
    freq_min_hz: float | None = None
    freq_max_hz: float | None = None
    sample_rate_min: float | None = None
    sample_rate_max: float | None = None
    gain_min_db: float | None = None
    gain_max_db: float | None = None
    duration_max_s: float = 10.0
    artifact_max_bytes: int = 256 * 1024 * 1024
    artifact_max_count: int = 8
    allowed_devices: tuple[str, ...] = ()


def resolve_bounds(adapter, *, device_caps=None, operator_caps=None,
                   policy: GlobalRxPolicy | None = None) -> ReceiveOnlyBounds:
    """Compute the effective receive envelope:

        global policy ∩ adapter capabilities ∩ device capabilities ∩ operator policy

    ``adapter`` is an adapter name or an :class:`~aithernet.rf.capabilities.RFCapabilities`.
    ``device_caps``/``operator_caps`` are optional ``RFCapabilities`` that further narrow the band.
    """
    from aithernet.rf import capabilities as caps
    pol = policy or GlobalRxPolicy()
    adapter_caps = (adapter if isinstance(adapter, caps.RFCapabilities)
                    else caps.adapter_capabilities(adapter))
    eff = adapter_caps.intersect(device_caps).intersect(operator_caps)
    return ReceiveOnlyBounds(
        adapter=adapter_caps.source,
        rx_capable=eff.rx_capable,
        requires_hardware=eff.requires_hardware,
        physically_qualified=eff.physically_qualified,
        freq_min_hz=eff.freq_min_hz, freq_max_hz=eff.freq_max_hz,
        sample_rate_min=eff.sample_rate_min, sample_rate_max=eff.sample_rate_max,
        gain_min_db=eff.gain_min_db, gain_max_db=eff.gain_max_db,
        duration_max_s=pol.duration_max_s,
        artifact_max_bytes=pol.artifact_max_bytes, artifact_max_count=pol.artifact_max_count,
        allowed_devices=pol.allowed_devices,
    )


@dataclass(frozen=True)
class RFMissionRequest:
    """A requested receive-only RF mission. ``objective`` is free natural language; the rest are
    bounded structured parameters. ``direction`` MUST be receive-only — anything else is rejected.
    The coordinator/coding selections and authorization boundary are recorded for audit only.
    """

    objective: str
    device_id: str
    center_frequency_hz: float
    sample_rate: float
    duration_s: float
    gain_db: float = 40.0
    direction: str = RX
    artifact_max_bytes: int | None = None       # may ask for LESS than the bound, never more
    artifact_max_count: int | None = None
    coordinator: str = ""
    coding: str = ""
    authorization_boundary: str = ""            # operator-stated scope (recorded, not interpreted)
    antenna: str | None = None


@dataclass(frozen=True)
class AuthorizedCapturePlan:
    """The frozen, validated capture plan the executor runs. Receive-only by construction."""

    objective: str
    device_id: str
    center_frequency_hz: float
    sample_rate: float
    duration_s: float
    gain_db: float
    direction: str                              # always "rx"
    artifact_max_bytes: int
    artifact_max_count: int
    coordinator: str
    coding: str
    authorization_boundary: str
    antenna: str | None
    adapter: str = ""                           # the resolved hardware adapter
    physically_qualified: bool = False          # True only if the adapter was tested on real hw
    clamped: tuple[str, ...] = field(default_factory=tuple)   # which fields were reduced to bounds
    digest: str = ""

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "objective", "device_id", "center_frequency_hz", "sample_rate", "duration_s",
            "gain_db", "direction", "artifact_max_bytes", "artifact_max_count", "coordinator",
            "coding", "authorization_boundary", "antenna", "adapter", "physically_qualified",
            "clamped")}
        d["digest"] = self.digest
        return d


def _digest(plan: AuthorizedCapturePlan) -> str:
    payload = {k: getattr(plan, k) for k in (
        "adapter", "device_id", "center_frequency_hz", "sample_rate", "duration_s", "gain_db",
        "direction", "artifact_max_bytes", "artifact_max_count")}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def validate(request: RFMissionRequest,
             bounds: ReceiveOnlyBounds | None = None) -> AuthorizedCapturePlan:
    """Validate + normalise a request into a frozen, receive-only :class:`AuthorizedCapturePlan`.

    Hard rejections raise :class:`ConstraintViolation`. Artifact limits and gain are CLAMPED down to
    the bounds (recorded in ``clamped``); frequency, sample-rate and duration are rejected if out of
    range rather than silently moved (so the operator/coordinator sees the real envelope).
    """
    if bounds is None:
        raise ConstraintViolation(
            "no_envelope",
            "an effective receive envelope is required — call resolve_bounds(adapter, …) first "
            "(the generic layer carries no device-specific defaults)")
    b = bounds

    # 1) RECEIVE-ONLY — the non-negotiable gate. No transmit command path exists.
    direction = (request.direction or "").strip().lower()
    if direction in _TX_DIRECTIONS or direction != RX:
        raise ConstraintViolation(
            "transmit_forbidden",
            f"receive-only: direction must be 'rx' (got {request.direction!r}); "
            "Aithernet has no transmit path")

    # 2) the selected adapter must be able to receive (software-only cannot)
    if not b.rx_capable:
        raise ConstraintViolation(
            "adapter_cannot_receive",
            f"adapter '{b.adapter}' has no receive capability (use simulation or a hardware "
            "profile)")

    # 3) objective must be present and bounded (defence against empty/oversized blobs)
    if not (request.objective or "").strip():
        raise ConstraintViolation("missing_objective", "an RF mission needs an objective")
    if len(request.objective) > 2000:
        raise ConstraintViolation("objective_too_long", "objective exceeds 2000 chars")

    # 4) device authorization
    if b.allowed_devices and request.device_id not in b.allowed_devices:
        raise ConstraintViolation("device_unauthorized",
                                  f"device '{request.device_id}' is not in the authorized set")
    if not request.device_id:
        raise ConstraintViolation("missing_device", "an RF mission needs a device")

    # 5) frequency band — the envelope must be DETERMINED (adapter/device caps), then in-range
    f = float(request.center_frequency_hz)
    if b.freq_min_hz is None or b.freq_max_hz is None:
        raise ConstraintViolation(
            "envelope_undetermined",
            f"frequency envelope unknown for adapter '{b.adapter}'; discover the device first")
    if not (b.freq_min_hz <= f <= b.freq_max_hz):
        raise ConstraintViolation(
            "frequency_out_of_band",
            f"center frequency {f:.0f} Hz outside [{b.freq_min_hz:.0f}, {b.freq_max_hz:.0f}]")

    # 6) sample rate (reject out-of-range; envelope must be determined)
    sr = float(request.sample_rate)
    if b.sample_rate_min is None or b.sample_rate_max is None:
        raise ConstraintViolation(
            "envelope_undetermined",
            f"sample-rate envelope unknown for adapter '{b.adapter}'; discover the device first")
    if not (b.sample_rate_min <= sr <= b.sample_rate_max):
        raise ConstraintViolation(
            "sample_rate_out_of_range",
            f"sample rate {sr:.0f} outside [{b.sample_rate_min:.0f}, {b.sample_rate_max:.0f}]")

    # 7) duration (reject non-positive; reject over the hard ceiling)
    dur = float(request.duration_s)
    if dur <= 0:
        raise ConstraintViolation("invalid_duration", "duration must be > 0")
    if dur > b.duration_max_s:
        raise ConstraintViolation(
            "duration_too_long",
            f"duration {dur}s exceeds the {b.duration_max_s}s receive ceiling")

    clamped: list[str] = []

    # 8) gain — clamp into range when the adapter/device defines one (else pass through, >= 0)
    gain = float(request.gain_db)
    lo = b.gain_min_db if b.gain_min_db is not None else 0.0
    hi = b.gain_max_db if b.gain_max_db is not None else gain
    cg = min(max(gain, lo), hi)
    if cg != gain:
        clamped.append("gain_db")

    # 8) artifact limits — clamp DOWN to the bounds (a request may ask for less, never more)
    amax_bytes = b.artifact_max_bytes if request.artifact_max_bytes is None else min(
        int(request.artifact_max_bytes), b.artifact_max_bytes)
    if request.artifact_max_bytes is not None and amax_bytes != int(request.artifact_max_bytes):
        clamped.append("artifact_max_bytes")
    amax_count = b.artifact_max_count if request.artifact_max_count is None else min(
        int(request.artifact_max_count), b.artifact_max_count)
    if request.artifact_max_count is not None and amax_count != int(request.artifact_max_count):
        clamped.append("artifact_max_count")
    if amax_bytes <= 0 or amax_count <= 0:
        raise ConstraintViolation("invalid_artifact_limits",
                                  "artifact byte/count limits must be > 0")

    plan = AuthorizedCapturePlan(
        objective=request.objective.strip(), device_id=request.device_id,
        center_frequency_hz=f, sample_rate=sr, duration_s=dur, gain_db=cg, direction=RX,
        artifact_max_bytes=amax_bytes, artifact_max_count=amax_count,
        coordinator=request.coordinator, coding=request.coding,
        authorization_boundary=request.authorization_boundary, antenna=request.antenna,
        adapter=b.adapter, physically_qualified=b.physically_qualified,
        clamped=tuple(clamped),
    )
    return replace(plan, digest=_digest(plan))


def assert_receive_only(direction: str) -> None:
    """The receive-only authorization gate for the canonical RF route (lease acquire).

    Aithernet acquires only RX leases — any transmit direction is rejected here, before the lease
    (and therefore any device use) is granted. Raises :class:`ConstraintViolation`.
    """
    d = (direction or "").strip().lower()
    if d in _TX_DIRECTIONS or d != RX:
        raise ConstraintViolation(
            "transmit_forbidden",
            f"receive-only: lease direction must be 'rx' (got {direction!r}); "
            "Aithernet has no transmit path")


def validate_capture(*, device_id: str, freq_hz: float, sample_rate: float, duration_s: float,
                     gain_db: float, adapter, device_caps=None, operator_caps=None,
                     policy: GlobalRxPolicy | None = None,
                     objective: str = "bounded receive capture") -> AuthorizedCapturePlan:
    """The deterministic gate at the canonical RX EXECUTION boundary (hardware capture).

    FAILS CLOSED for a physical SDR: if a safety-critical capability needed to authorize the request
    is unknown, the capture is rejected (before the subprocess) with a stable error —
    ``device_identity_unresolved`` / ``frequency_capability_unknown`` /
    ``sample_rate_capability_unknown`` / ``gain_capability_unknown`` / ``rf_capabilities_unknown``.
    A real capture is NEVER silently permitted because an adapter failed to report bounds.

    For non-physical adapters the explicit synthetic capabilities apply (``simulation``), or the
    adapter cannot receive at all (``software-only``). Receive-only is always enforced; duration is
    always bounded. ``device_caps``/``operator_caps`` may NARROW (or, for an explicitly-configured
    operator override, supply) the envelope — they never come from the coordinator model. Returns
    the (gain-clamped) :class:`AuthorizedCapturePlan`.
    """
    try:
        bounds = resolve_bounds(adapter, device_caps=device_caps, operator_caps=operator_caps,
                                policy=policy)
    except KeyError as exc:
        raise ConstraintViolation("rf_capabilities_unknown", str(exc)) from exc
    if not bounds.rx_capable:
        raise ConstraintViolation(
            "adapter_cannot_receive", f"adapter '{bounds.adapter}' cannot receive")
    if bounds.allowed_devices and device_id not in bounds.allowed_devices:
        raise ConstraintViolation("device_unauthorized",
                                  f"device '{device_id}' is not in the authorized set")
    if duration_s <= 0:
        raise ConstraintViolation("invalid_duration", "duration must be > 0")
    if duration_s > bounds.duration_max_s:
        raise ConstraintViolation("duration_too_long",
                                  f"duration {duration_s}s exceeds the {bounds.duration_max_s}s "
                                  "receive ceiling")

    # FAIL CLOSED for physical hardware: every safety-critical capability must be DETERMINED
    # (from the adapter, the discovered device, or an explicit bounded operator override).
    if bounds.requires_hardware:
        if not (device_id or "").strip():
            raise ConstraintViolation("device_identity_unresolved",
                                      "physical capture requires a resolved device identity")
        if bounds.freq_min_hz is None or bounds.freq_max_hz is None:
            raise ConstraintViolation(
                "frequency_capability_unknown",
                f"adapter '{bounds.adapter}' did not report a frequency range for device "
                f"'{device_id}' — refusing physical capture")
        if bounds.sample_rate_min is None or bounds.sample_rate_max is None:
            raise ConstraintViolation(
                "sample_rate_capability_unknown",
                f"adapter '{bounds.adapter}' did not report a sample-rate range for device "
                f"'{device_id}' — refusing physical capture")
        if bounds.gain_min_db is None or bounds.gain_max_db is None:
            raise ConstraintViolation(
                "gain_capability_unknown",
                f"adapter '{bounds.adapter}' did not report a gain range for device "
                f"'{device_id}' — refusing physical capture")

    f = float(freq_hz)
    if bounds.freq_min_hz is not None and bounds.freq_max_hz is not None:
        if not (bounds.freq_min_hz <= f <= bounds.freq_max_hz):
            raise ConstraintViolation(
                "frequency_out_of_band",
                f"{f:.0f} Hz outside [{bounds.freq_min_hz:.0f}, {bounds.freq_max_hz:.0f}]")
    sr = float(sample_rate)
    if bounds.sample_rate_min is not None and bounds.sample_rate_max is not None:
        if not (bounds.sample_rate_min <= sr <= bounds.sample_rate_max):
            raise ConstraintViolation(
                "sample_rate_out_of_range",
                f"{sr:.0f} outside [{bounds.sample_rate_min:.0f}, {bounds.sample_rate_max:.0f}]")
    clamped: list[str] = []
    gain = float(gain_db)
    lo = bounds.gain_min_db if bounds.gain_min_db is not None else 0.0
    hi = bounds.gain_max_db if bounds.gain_max_db is not None else gain
    cg = min(max(gain, lo), hi)
    if cg != gain:
        clamped.append("gain_db")
    plan = AuthorizedCapturePlan(
        objective=objective, device_id=device_id, center_frequency_hz=f, sample_rate=sr,
        duration_s=float(duration_s), gain_db=cg, direction=RX,
        artifact_max_bytes=bounds.artifact_max_bytes, artifact_max_count=bounds.artifact_max_count,
        coordinator="", coding="", authorization_boundary="", antenna=None,
        adapter=bounds.adapter, physically_qualified=bounds.physically_qualified,
        clamped=tuple(clamped))
    return replace(plan, digest=_digest(plan))


def explain_bounds(bounds: ReceiveOnlyBounds) -> dict:
    """The resolved effective envelope, for `mission rf-plan` display (no secrets)."""
    b = bounds
    return {
        "adapter": b.adapter,
        "rx_capable": b.rx_capable,
        "physically_qualified": b.physically_qualified,
        "direction": "rx (receive-only; no transmit path)",
        "frequency_hz": [b.freq_min_hz, b.freq_max_hz],
        "sample_rate": [b.sample_rate_min, b.sample_rate_max],
        "duration_s_max": b.duration_max_s,
        "gain_db": [b.gain_min_db, b.gain_max_db],
        "artifact_max_bytes": b.artifact_max_bytes,
        "artifact_max_count": b.artifact_max_count,
        "allowed_devices": list(b.allowed_devices) or "any discovered device",
    }
