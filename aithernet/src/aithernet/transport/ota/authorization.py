"""Phase 1/5: the RF transmit authorization model — physical TX is CLOSED by default.

Receive-only remains the default. A physical TX action is rejected unless EVERY gate is satisfied
and the operator approval binds to the exact plan digest. Any change to frequency, sample rate,
gain, bandwidth, duration, payload, hardware, URI, modem profile, or generated flowgraph invalidates
prior approval. This module is pure policy/schema; it never touches a radio. No frequency is chosen
here — the operator supplies authorized parameters.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class RFTxCapability:
    """What a discovered device + adapter explicitly report they can do for TX."""

    adapter_supports_tx: bool
    device_reports_tx: bool
    min_freq_hz: int
    max_freq_hz: int
    max_sample_rate: int
    max_gain: float
    channels: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RFTxPolicy:
    """The node's TX policy. Default-deny; an operator must opt in with explicit bounds."""

    tx_allowed: bool = False
    allowed_freq_ranges_hz: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    max_sample_rate: int = 0
    max_occupied_bandwidth_hz: int = 0
    max_gain: float = 0.0
    max_duration_seconds: float = 0.0
    max_messages: int = 0
    max_frames: int = 0
    approved_profiles: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RFTxPlan:
    """The EXACT transmission plan. Its digest is what an authorization binds to."""

    device_id: str
    uri: str
    profile_id: str
    profile_digest: str
    flowgraph_digest: str
    implementation_digest: str
    frame_artifact_digest: str
    center_frequency_hz: int
    sample_rate: int
    occupied_bandwidth_hz: int
    gain: float
    channel: int
    duration_seconds: float
    message_count: int
    frame_count: int

    def digest(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class RFTxLease:
    lease_id: str
    device_id: str
    expires_at: int            # unix seconds
    active: bool = True


@dataclass(frozen=True)
class RFTxAuthorization:
    """An explicit operator approval bound to ONE plan digest."""

    authorization_id: str
    plan_digest: str
    operator: str
    approved: bool
    expires_at: int


@dataclass(frozen=True)
class TxGateResult:
    allowed: bool
    reasons: list           # the gate(s) that failed (empty when allowed)


def evaluate_tx_gates(*, plan: RFTxPlan, policy: RFTxPolicy, capability: RFTxCapability,
                      lease: RFTxLease | None, authorization: RFTxAuthorization | None,
                      profile_simulation_only: bool, validation_passed: bool,
                      conflicting_active_tx: bool, now: int) -> TxGateResult:
    """Return whether a PHYSICAL TX is permitted. Default-deny; ALL gates must pass.

    This is exercised by tests to prove TX is denied by default and on any plan/authorization
    drift. Nothing here transmits; a True result only means the plan *would* be permitted once a
    managed execution path is separately qualified."""
    reasons: list = []
    if not capability.adapter_supports_tx:
        reasons.append("adapter does not support TX")
    if not capability.device_reports_tx:
        reasons.append("device does not report TX capability")
    if not policy.tx_allowed:
        reasons.append("physical TX is not enabled on this node (local operator toggle)")
    # beta.2: `profile_simulation_only` is INFORMATIONAL only — a profile that has not yet been
    # hardware-tested is NOT prohibited from physical use (recorded in the plan, never a lock).
    if plan.profile_id not in policy.approved_profiles:
        reasons.append("profile not in the operator's locally configured approved_profiles")
    if not _freq_authorized(plan.center_frequency_hz, plan.occupied_bandwidth_hz,
                            policy.allowed_freq_ranges_hz):
        reasons.append("frequency/bandwidth not authorized")
    if not (capability.min_freq_hz <= plan.center_frequency_hz <= capability.max_freq_hz):
        reasons.append("frequency outside device capability")
    if plan.sample_rate > policy.max_sample_rate or plan.sample_rate > capability.max_sample_rate:
        reasons.append("sample rate not authorized")
    if plan.occupied_bandwidth_hz > policy.max_occupied_bandwidth_hz:
        reasons.append("occupied bandwidth not authorized")
    if plan.gain > policy.max_gain or plan.gain > capability.max_gain:
        reasons.append("gain not authorized")
    if plan.duration_seconds > policy.max_duration_seconds:
        reasons.append("duration not authorized")
    if plan.message_count > policy.max_messages or plan.frame_count > policy.max_frames:
        reasons.append("message/frame count not authorized")
    if plan.channel not in capability.channels:
        reasons.append("channel/antenna not authorized")
    if not validation_passed:
        reasons.append("generated implementation did not pass validation")
    if conflicting_active_tx:
        reasons.append("a conflicting lease or active transmission exists")
    if (lease is None or not lease.active or lease.expires_at <= now
            or lease.device_id != plan.device_id):
        reasons.append("no valid bounded TX lease for the device")
    if authorization is None or not authorization.approved or authorization.expires_at <= now:
        reasons.append("no valid operator authorization")
    elif authorization.plan_digest != plan.digest():
        reasons.append("authorization does not bind to this exact plan digest")
    return TxGateResult(allowed=not reasons, reasons=reasons)


def _freq_authorized(center: int, bw: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    lo, hi = center - bw // 2, center + bw // 2
    return any(r_lo <= lo and hi <= r_hi for (r_lo, r_hi) in ranges)
