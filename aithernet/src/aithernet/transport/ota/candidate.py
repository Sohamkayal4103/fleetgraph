"""Phase 4: deterministic validation + promotion gate for a generated modem candidate.

A coding agent may PROPOSE a new/adapted modem (an ``RFLinkProfile`` + generated flowgraph source),
but it may never approve transmission. The canonical, deterministic system validates the candidate
(profile structure, flowgraph safety, framing/security interop, and simulated two-node delivery over
impaired channels) and only then promotes it from ``unapproved`` to ``approved_simulation``. A
candidate is NEVER promoted to physical-approved here; physical TX stays closed. This gate runs no
model and transmits nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from aithernet.transport.envelope import (
    EnvelopeRecipient,
    EnvelopeSender,
    MessageEnvelope,
)
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota import flowgraph as fg
from aithernet.transport.ota.interface import DeliveryStatus
from aithernet.transport.ota.profiles import (
    PROFILE_APPROVED_SIMULATION,
    ProfileRegistry,
    ProfileValidationError,
    RFLinkProfile,
    validate_profile,
)
from aithernet.transport.ota.simulated_rf import SimulatedRFLink

#: The impaired channels every candidate must survive (mission-grouped, deterministic seeds).
_VALIDATION_CHANNELS = (
    ch.ChannelModel(snr_db=20.0, seed=101),
    ch.ChannelModel(snr_db=12.0, phase_rad=0.7, delay_samples=13, seed=102),
    ch.ChannelModel(snr_db=8.0, phase_rad=0.3, delay_samples=29, seed=103),
)


@dataclass
class CandidateValidationReport:
    profile_id: str
    profile_digest: str
    implementation_digest: str
    flowgraph_digest: str
    profile_structurally_valid: bool = False
    flowgraph_safe: bool = False
    framing_interop_ok: bool = False
    simulated_deliveries: list = field(default_factory=list)   # per-channel pass/fail
    promoted: bool = False
    problems: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (self.profile_structurally_valid and self.flowgraph_safe
                and self.framing_interop_ok and bool(self.simulated_deliveries)
                and all(self.simulated_deliveries))

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["passed"] = self.passed
        return d


def validate_and_promote(profile: RFLinkProfile, *, implementation_source: str,
                         registry: ProfileRegistry, peer_keys, channels=_VALIDATION_CHANNELS
                         ) -> CandidateValidationReport:
    """Validate a candidate modem and promote it to simulation-approved only if EVERY gate passes.

    ``peer_keys`` is (node_a_id, node_b_id, pub_a_b64, pub_b_b64, shared_secret). ``registry`` is
    mutated only on a full pass. Returns a structured report (with digests) for research + audit."""
    impl_digest = "sha256:" + hashlib.sha256(implementation_source.encode()).hexdigest()
    tx = fg.generate_tx_flowgraph(profile)
    rx = fg.generate_rx_flowgraph(profile)
    report = CandidateValidationReport(
        profile_id=profile.profile_id, profile_digest=profile.digest(),
        implementation_digest=impl_digest, flowgraph_digest=fg.flowgraph_digest(tx, rx))

    # 1) structural profile validation + register as UNAPPROVED (never directly transmittable)
    try:
        validate_profile(profile)
        registry.register_candidate(profile)
        report.profile_structurally_valid = True
    except ProfileValidationError as exc:
        report.problems.append(f"profile invalid: {exc}")
        return report

    # 2) flowgraph + implementation safety (bounded blocks; no arbitrary/shell/network code)
    fg_ok, fg_probs = fg.validate_flowgraph_source(tx)
    rx_ok, rx_probs = fg.validate_flowgraph_source(rx)
    impl_ok, impl_probs = fg.validate_flowgraph_source(implementation_source)
    report.flowgraph_safe = fg_ok and rx_ok and impl_ok
    report.problems.extend(fg_probs + rx_probs + impl_probs)
    if not report.flowgraph_safe:
        return report

    # 3) framing/security interop + simulated two-node delivery across impaired channels
    a_id, b_id, pub_a, pub_b, secret = peer_keys
    deliveries = []
    for cm in channels:
        link = SimulatedRFLink(profile, cm)
        link.add_endpoint(a_id, peer_id=b_id, peer_public_key_b64=pub_b, shared_secret=secret)
        link.add_endpoint(b_id, peer_id=a_id, peer_public_key_b64=pub_a, shared_secret=secret)
        env = MessageEnvelope(sender=EnvelopeSender(node_id=a_id, fingerprint="fp"),
                              recipient=EnvelopeRecipient(node_id=b_id),
                              payload={"type": "candidate_probe", "ch": cm.snr_db})
        receipt, received, _rec = link.transmit(sender=a_id, receiver=b_id, envelope=env)
        ok = (receipt.status is DeliveryStatus.DELIVERED and received is not None
              and received.payload == env.payload)
        deliveries.append(ok)
    report.simulated_deliveries = deliveries
    report.framing_interop_ok = bool(deliveries) and all(deliveries)

    # 4) promote to SIMULATION-approved only on a full pass (never physical-approved)
    if report.passed:
        registry.promote_to_simulation(profile.profile_id)
        report.promoted = registry.state(profile.profile_id) == PROFILE_APPROVED_SIMULATION
    return report
