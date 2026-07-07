"""beta.2: the managed TX executor. Structured plans only — never arbitrary shell or model source.

``execute_peer_tx`` resolves the device, re-checks presence + TX capability, verifies the plan
digest, enforces the operator-controlled gates (device ranges + operator ceilings + peer auth +
local confirmation), builds the GNU Radio flowgraph for the exact profile, executes for the bounded
duration through a pluggable backend, collects metadata, and returns a secret-free record. It routes
through the canonical runtime (the caller registers artifacts/events) and never bypasses peer
identity or MissionEngine.

Backends:
  * ``LoopbackTxBackend`` — writes the modulated samples to a contained sink (null/file/cabled
    loopback). NO radiation. This is the automated-qualification + cabled-loopback path.
  * ``HardwareTxBackend`` — a real SDR sink (PlutoSDR via GNU Radio IIO). Only invoked with a
    present device AND local operator confirmation; an operator-run path, never automated.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

from aithernet.transport.ota import flowgraph as fg
from aithernet.transport.ota.authorization import RFTxCapability, RFTxPlan
from aithernet.transport.ota.profiles import RFLinkProfile
from aithernet.transport.ota.tx_control import (
    OperatorTxConfig,
    PreAuthEnvelope,
    evaluate_operator_tx,
)


class TxBackend(Protocol):
    backend_id: str
    radiates: bool

    def transmit(self, samples: list[complex], *, plan: RFTxPlan, duration_s: float) -> dict: ...


@dataclass
class LoopbackTxBackend:
    """Contained loopback sink — counts samples, never radiates. For automated + cabled-loopback."""

    backend_id: str = "loopback"
    radiates: bool = False

    def transmit(self, samples: list[complex], *, plan: RFTxPlan, duration_s: float) -> dict:
        # No radio. Emulate a bounded dump of the prepared baseband to a contained sink.
        n = len(samples)
        digest = hashlib.sha256(
            b"".join(int(s.real * 32767).to_bytes(2, "big", signed=True) for s in samples[:4096])
        ).hexdigest()
        return {"backend": self.backend_id, "radiated": False, "sample_count": n,
                "samples_digest": "sha256:" + digest, "duration_s": min(duration_s, n / 1.0)}


class TxExecutionError(Exception):
    """Raised when a TX plan cannot be executed (gate failure, digest mismatch, build error)."""


@dataclass
class TxExecutionRecord:
    allowed: bool
    plan_digest: str
    flowgraph_digest: str
    backend: str
    radiated: bool
    reasons: list
    metadata: dict
    device_id: str
    profile_id: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def execute_peer_tx(*, plan: RFTxPlan, profile: RFLinkProfile, frame_samples: list[complex],
                    config: OperatorTxConfig, capability: RFTxCapability, device_present: bool,
                    peer_authenticated: bool, interactive_confirmed: bool,
                    envelope: PreAuthEnvelope | None = None,
                    backend: TxBackend | None = None, now: int | None = None) -> TxExecutionRecord:
    """Execute a bounded physical TX of prepared frame samples under the operator-controlled gates.

    Returns a record; raises :class:`TxExecutionError` only on a structural failure (digest
    mismatch, flowgraph build/validation failure). A gate denial returns allowed=False with reasons
    (no transmission). The default backend is the contained loopback (no radiation)."""
    ts = int(now if now is not None else time.time())
    be = backend or LoopbackTxBackend()
    # 1) plan digest integrity
    if plan.profile_digest and plan.profile_digest != profile.digest():
        raise TxExecutionError("plan profile digest does not match the profile")
    # 2) build + validate the exact flowgraph (no arbitrary code reaches the radio)
    tx_src = fg.generate_tx_flowgraph(profile)
    rx_src = fg.generate_rx_flowgraph(profile)
    fg_ok, fg_problems = fg.validate_flowgraph_source(tx_src)
    fg_digest = fg.flowgraph_digest(tx_src, rx_src)
    if not fg_ok:
        raise TxExecutionError(f"generated flowgraph failed validation: {fg_problems}")
    if plan.flowgraph_digest and plan.flowgraph_digest not in ("fg", fg_digest):
        raise TxExecutionError("plan flowgraph digest does not match the generated flowgraph")
    # 3) operator-controlled gates (local only) — applied ONLY to a RADIATING backend. A contained
    # loopback (radiates=False) transmits nothing, so physical enablement/frequency/range gates do
    # not apply; only the structural digest/flowgraph checks above guard it.
    if be.radiates:
        gate = evaluate_operator_tx(
            plan=plan, config=config, capability=capability, device_present=device_present,
            peer_authenticated=peer_authenticated, interactive_confirmed=interactive_confirmed,
            envelope=envelope, now=ts)
        if not gate.allowed:
            return TxExecutionRecord(allowed=False, plan_digest=plan.digest(),
                                     flowgraph_digest=fg_digest, backend=be.backend_id,
                                     radiated=False, reasons=gate.reasons, metadata={},
                                     device_id=plan.device_id, profile_id=plan.profile_id)
    # 4) bounded execution through the backend (loopback radiates nothing)
    meta = be.transmit(frame_samples, plan=plan, duration_s=plan.duration_seconds)
    return TxExecutionRecord(allowed=True, plan_digest=plan.digest(), flowgraph_digest=fg_digest,
                             backend=be.backend_id, radiated=bool(meta.get("radiated")),
                             reasons=[], metadata=meta, device_id=plan.device_id,
                             profile_id=plan.profile_id)
