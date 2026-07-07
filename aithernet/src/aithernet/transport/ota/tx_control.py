"""beta.2: operator-controlled physical-TX policy + plans. Local only — no vendor/cloud gate.

Physical SDR transmission is a real, supported capability. Whether/where/how to transmit is the
LOCAL operator's decision. There is NO vendor approval, cloud authorization, subscription, hidden
allowlist, release key, or external service in this path. What remains are TECHNICAL CORRECTNESS
controls that keep the radio and the network sound:

  * the operator must enable TX locally (``rf tx enable``);
  * a TX-capable device must be present and report TX capability;
  * the plan's parameters must be within the DEVICE's actual supported ranges;
  * the plan must be within the operator's own configured ceilings;
  * the peer message must be authenticated;
  * the operator must confirm the exact plan (interactively, or via a local pre-authorization
    envelope the operator created).

None of these are feature-withholding; all are correctness/safety the operator asked Aithernet to
enforce. Aithernet never independently chooses an operating frequency.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aithernet.transport.ota.authorization import RFTxCapability, RFTxPlan, TxGateResult


@dataclass(frozen=True)
class OperatorTxConfig:
    """The LOCAL operator's physical-TX configuration for this node (stored in node config)."""

    enabled: bool = False                       # rf tx enable / disable — a local toggle
    device_id: str | None = None
    uri: str | None = None
    tx_channel: int = 0
    antenna: str | None = None
    preferred_profiles: tuple[str, ...] = field(default_factory=tuple)
    operating_region_label: str = ""            # documentation/audit only; never selects frequency
    max_duration_seconds: float = 5.0
    max_message_bytes: int = 65536
    max_frame_count: int = 1000
    gain_ceiling: float = 0.0                   # operator-set ceiling (0 = use device max)
    allow_ip_fallback: bool = False
    require_interactive_confirmation: bool = True
    allow_preauthorized_noninteractive: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PreAuthEnvelope:
    """A LOCAL, operator-created pre-authorization permitting bounded non-interactive plans."""

    envelope_id: str
    device_id: str
    freq_min_hz: int
    freq_max_hz: int
    max_occupied_bandwidth_hz: int
    gain_ceiling: float
    max_duration_seconds: float
    allowed_profiles: tuple[str, ...]
    max_payload_bytes: int
    max_retries: int
    valid_until: int                            # unix seconds

    def to_dict(self) -> dict:
        return asdict(self)


def tx_config_path(state_root: Path | None = None) -> Path:
    """Local path for the operator TX config (never an external service)."""
    if state_root is not None:
        return Path(state_root) / "config" / "tx.json"
    base = os.environ.get("AITHERNET_STATE_ROOT") or str(
        Path.home() / ".local" / "share" / "aithernet")
    return Path(base) / "config" / "tx.json"


def load_tx_config(state_root: Path | None = None) -> OperatorTxConfig:
    """Load the LOCAL operator TX config (defaults: disabled). No network call."""
    path = tx_config_path(state_root)
    if not path.is_file():
        return OperatorTxConfig()
    try:
        data = json.loads(path.read_text())
        fields = {f for f in OperatorTxConfig.__dataclass_fields__}
        kw = {k: v for k, v in data.items() if k in fields}
        if "preferred_profiles" in kw:
            kw["preferred_profiles"] = tuple(kw["preferred_profiles"])
        return OperatorTxConfig(**kw)
    except (OSError, ValueError, TypeError):
        return OperatorTxConfig()


def save_tx_config(config: OperatorTxConfig, state_root: Path | None = None) -> Path:
    """Persist the operator TX config locally (0600). Returns the path."""
    path = tx_config_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(config.to_dict(), fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def plan_within_envelope(plan: RFTxPlan, env: PreAuthEnvelope, now: int) -> bool:
    """Whether a plan fits a local pre-authorization envelope (ranges + validity)."""
    if env.valid_until <= now or plan.device_id != env.device_id:
        return False
    lo, hi = plan.center_frequency_hz - plan.occupied_bandwidth_hz // 2, \
        plan.center_frequency_hz + plan.occupied_bandwidth_hz // 2
    return (env.freq_min_hz <= lo and hi <= env.freq_max_hz
            and plan.occupied_bandwidth_hz <= env.max_occupied_bandwidth_hz
            and plan.gain <= env.gain_ceiling
            and plan.duration_seconds <= env.max_duration_seconds
            and plan.profile_id in env.allowed_profiles)


def evaluate_operator_tx(*, plan: RFTxPlan, config: OperatorTxConfig, capability: RFTxCapability,
                         device_present: bool, peer_authenticated: bool,
                         interactive_confirmed: bool, envelope: PreAuthEnvelope | None = None,
                         conflicting_active_tx: bool = False, now: int) -> TxGateResult:
    """Decide whether a PHYSICAL transmission is permitted — entirely from LOCAL state.

    Returns allowed=True only when the operator has enabled TX, a capable device is present, the
    plan is within device + operator ranges, the peer is authenticated, no conflict is active, and
    the operator has confirmed (interactively or via a matching local pre-authorization envelope).
    No vendor/cloud authorization is consulted. Reasons are concrete + technical."""
    reasons: list[str] = []
    # 1) local operator enablement (a local toggle, not a vendor lock)
    if not config.enabled:
        reasons.append("physical TX is not enabled on this node (run `aithernet rf tx enable`)")
    # 2) hardware capability (technical)
    if not capability.adapter_supports_tx:
        reasons.append("the adapter does not report TX capability")
    if not capability.device_reports_tx:
        reasons.append("the device does not report TX capability")
    if not device_present:
        reasons.append("the configured TX device is not currently present")
    # 3) peer authentication (security — never relaxed)
    if not peer_authenticated:
        reasons.append("the peer message is not authenticated")
    # 4) DEVICE range enforcement (prevent out-of-range hardware parameters)
    if not (capability.min_freq_hz <= plan.center_frequency_hz <= capability.max_freq_hz):
        reasons.append("center frequency is outside the device's supported range")
    if plan.sample_rate > capability.max_sample_rate:
        reasons.append("sample rate is outside the device's supported range")
    if plan.gain > capability.max_gain:
        reasons.append("gain is outside the device's supported range")
    if capability.channels and plan.channel not in capability.channels:
        reasons.append("the selected channel is not supported by the device")
    # 5) OPERATOR-configured ceilings (the operator's own bounds)
    if plan.duration_seconds > config.max_duration_seconds:
        reasons.append("duration exceeds the operator-configured ceiling")
    if config.gain_ceiling > 0 and plan.gain > config.gain_ceiling:
        reasons.append("gain exceeds the operator-configured ceiling")
    if plan.frame_count > config.max_frame_count:
        reasons.append("frame count exceeds the operator-configured ceiling")
    # 6) no conflicting active transmission
    if conflicting_active_tx:
        reasons.append("a conflicting transmission is already active on this device")
    # 7) LOCAL operator confirmation (interactive OR a matching local pre-authorization envelope)
    confirmed = interactive_confirmed or (
        config.allow_preauthorized_noninteractive and envelope is not None
        and plan_within_envelope(plan, envelope, now))
    if not confirmed:
        reasons.append("no local operator confirmation for this exact plan "
                       "(confirm interactively, or create a pre-authorization envelope)")
    return TxGateResult(allowed=not reasons, reasons=reasons)
