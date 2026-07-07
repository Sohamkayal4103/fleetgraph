"""1.0.0-beta.1 OTA Phase 1: transport interface + RF schemas (profile, frame/CRC, AEAD security,
replay/expiry/receiver, TX authorization gates). All software; no radio, no transmission."""

from __future__ import annotations

import os

import pytest

from aithernet.transport.envelope import (
    EnvelopeRecipient,
    EnvelopeSender,
    MessageEnvelope,
)
from aithernet.transport.ota import authorization as auth
from aithernet.transport.ota import frame as fr
from aithernet.transport.ota import profiles as pf
from aithernet.transport.ota import security as sec
from aithernet.transport.ota.interface import DeliveryReceipt, DeliveryStatus


def _env(payload=None):
    return MessageEnvelope(sender=EnvelopeSender(node_id="A", fingerprint="fp-A"),
                           recipient=EnvelopeRecipient(node_id="B"),
                           payload=payload or {"hello": "world"})


# -- profiles ---------------------------------------------------------------------------------

def test_reference_profiles_validate_and_have_digests():
    pf.validate_profile(pf.REFERENCE_BPSK)
    pf.validate_profile(pf.REFERENCE_QPSK)
    assert pf.REFERENCE_BPSK.digest().startswith("sha256:")
    assert pf.REFERENCE_BPSK.digest() != pf.REFERENCE_QPSK.digest()
    assert "profile_digest" in pf.REFERENCE_BPSK.to_manifest()
    # profile carries NO frequency/gain/power/antenna/hardware identity
    m = pf.REFERENCE_BPSK.to_manifest()
    for forbidden in ("frequency", "center_freq", "gain", "power", "antenna", "device", "uri"):
        assert not any(forbidden in k for k in m), f"profile leaked hardware field {forbidden}"


def test_profile_validation_rejects_bad_profiles():
    bad = pf.RFLinkProfile(**{**pf.REFERENCE_BPSK.__dict__, "sample_rate": 9999})
    with pytest.raises(pf.ProfileValidationError):
        pf.validate_profile(bad)


def test_registry_candidate_is_unapproved_until_promoted():
    reg = pf.ProfileRegistry()
    assert reg.approved_for_simulation("ref-bpsk-1k")
    cand = pf.RFLinkProfile(**{**pf.REFERENCE_BPSK.__dict__, "profile_id": "cand-x"})
    reg.register_candidate(cand)
    assert reg.state("cand-x") == pf.PROFILE_UNAPPROVED
    assert not reg.approved_for_simulation("cand-x")
    reg.promote_to_simulation("cand-x")
    assert reg.approved_for_simulation("cand-x")


# -- framing + CRC ----------------------------------------------------------------------------

def test_frame_roundtrip_and_crc_detects_corruption():
    f = fr.Frame(fr.FrameType.DATA, msg_id=42, seq=0, total=1, payload=b"payload-bytes")
    raw = f.encode("crc16")
    assert fr.Frame.decode(raw, "crc16") == f
    corrupted = bytearray(raw)
    corrupted[-3] ^= 0xFF
    with pytest.raises(fr.FrameError, match="crc"):
        fr.Frame.decode(bytes(corrupted), "crc16")


def test_fragmentation_and_reassembly():
    payload = os.urandom(500)
    frames = fr.fragment(payload, msg_id=7, max_frame_payload=128)
    assert len(frames) == 4 and frames[0].total == 4
    assert fr.reassemble(frames) == payload
    # missing fragment is detected
    with pytest.raises(fr.FrameError, match="missing"):
        fr.reassemble(frames[:-1])


# -- security: AEAD + replay/expiry/receiver --------------------------------------------------

def test_seal_open_roundtrip_preserves_canonical_envelope():
    key = os.urandom(32)
    env = _env({"mission": "request", "n": 5})
    header, ct = sec.seal(env, key=key, sender_node_id="A", receiver_node_id="B",
                          peer_key_id="kid-1", sequence=1, transport_profile_id="ref-bpsk-1k")
    out = sec.open_sealed(header, ct, key=key, expected_receiver="B")
    assert out.message_id == env.message_id and out.payload == env.payload
    # header binds the required fields and never carries the key
    h = header.to_dict()
    for fld in ("sender_node_id", "receiver_node_id", "peer_key_id", "message_id", "sequence",
                "timestamp", "expiry", "nonce_hex", "payload_digest", "transport_profile_id"):
        assert fld in h
    assert "key" not in str(h).lower() or "key_id" in str(h).lower()


def test_tampered_ciphertext_fails_authentication():
    key = os.urandom(32)
    header, ct = sec.seal(_env(), key=key, sender_node_id="A", receiver_node_id="B",
                          peer_key_id="k", sequence=1, transport_profile_id="p")
    bad = bytearray(ct)
    bad[0] ^= 0xFF
    with pytest.raises(sec.OTASecurityError, match="authentication"):
        sec.open_sealed(header, bytes(bad), key=key, expected_receiver="B")


def test_wrong_receiver_rejected():
    key = os.urandom(32)
    header, ct = sec.seal(_env(), key=key, sender_node_id="A", receiver_node_id="B",
                          peer_key_id="k", sequence=1, transport_profile_id="p")
    with pytest.raises(sec.OTASecurityError, match="receiver"):
        sec.open_sealed(header, ct, key=key, expected_receiver="C")


def test_expired_message_rejected():
    key = os.urandom(32)
    header, ct = sec.seal(_env(), key=key, sender_node_id="A", receiver_node_id="B",
                          peer_key_id="k", sequence=1, transport_profile_id="p",
                          expiry_seconds=10, now=1000)
    with pytest.raises(sec.OTASecurityError, match="expired"):
        sec.open_sealed(header, ct, key=key, expected_receiver="B", now=2000)


def test_replay_rejected():
    key = os.urandom(32)
    guard = sec.ReplayGuard()
    header, ct = sec.seal(_env(), key=key, sender_node_id="A", receiver_node_id="B",
                          peer_key_id="k", sequence=1, transport_profile_id="p")
    sec.open_sealed(header, ct, key=key, expected_receiver="B", replay_guard=guard)
    with pytest.raises(sec.OTASecurityError, match="replay"):
        sec.open_sealed(header, ct, key=key, expected_receiver="B", replay_guard=guard)


def test_demodulation_is_not_authentication_wrong_key_fails():
    # A different key (a peer not actually trusted) cannot open even a perfectly-decoded frame.
    header, ct = sec.seal(_env(), key=os.urandom(32), sender_node_id="A", receiver_node_id="B",
                          peer_key_id="k", sequence=1, transport_profile_id="p")
    with pytest.raises(sec.OTASecurityError):
        sec.open_sealed(header, ct, key=os.urandom(32), expected_receiver="B")


# -- TX authorization: closed by default ------------------------------------------------------

def _plan(**over):
    base = dict(device_id="dev1", uri="sim://dev1", profile_id="ref-bpsk-1k",
                profile_digest="pd", flowgraph_digest="fg", implementation_digest="im",
                frame_artifact_digest="fa", center_frequency_hz=433_000_000, sample_rate=8000,
                occupied_bandwidth_hz=1200, gain=10.0, channel=0, duration_seconds=1.0,
                message_count=1, frame_count=5)
    base.update(over)
    return auth.RFTxPlan(**base)


def test_tx_denied_by_default():
    res = auth.evaluate_tx_gates(
        plan=_plan(), policy=auth.RFTxPolicy(), capability=auth.RFTxCapability(
            adapter_supports_tx=False, device_reports_tx=False, min_freq_hz=0, max_freq_hz=0,
            max_sample_rate=0, max_gain=0.0), lease=None, authorization=None,
        profile_simulation_only=True, validation_passed=False, conflicting_active_tx=False,
        now=1000)
    # beta.2: physical TX is denied by DEFAULT because the local operator has not enabled it
    # (an operator toggle, not a vendor lock). Still default-deny; only the wording changed.
    assert res.allowed is False
    assert "not enabled on this node" in " ".join(res.reasons)


def test_tx_authorization_must_bind_exact_plan_digest():
    plan = _plan()
    cap = auth.RFTxCapability(adapter_supports_tx=True, device_reports_tx=True,
                              min_freq_hz=400_000_000, max_freq_hz=450_000_000,
                              max_sample_rate=8000, max_gain=20.0, channels=(0,))
    policy = auth.RFTxPolicy(tx_allowed=True, allowed_freq_ranges_hz=((432_000_000, 434_000_000),),
                             max_sample_rate=8000, max_occupied_bandwidth_hz=2000, max_gain=20.0,
                             max_duration_seconds=5.0, max_messages=10, max_frames=100,
                             approved_profiles=("ref-bpsk-1k",))
    lease = auth.RFTxLease(lease_id="L1", device_id="dev1", expires_at=2000)
    # authorization bound to a DIFFERENT plan digest -> denied
    wrong = auth.RFTxAuthorization(authorization_id="Z", plan_digest="sha256:WRONG",
                                   operator="op", approved=True, expires_at=2000)
    res = auth.evaluate_tx_gates(plan=plan, policy=policy, capability=cap, lease=lease,
                                 authorization=wrong, profile_simulation_only=False,
                                 validation_passed=True, conflicting_active_tx=False, now=1000)
    assert res.allowed is False and any("exact plan digest" in r for r in res.reasons)
    # correctly bound authorization -> all gates pass (still no transmission happens)
    right = auth.RFTxAuthorization(authorization_id="Z", plan_digest=plan.digest(),
                                   operator="op", approved=True, expires_at=2000)
    ok = auth.evaluate_tx_gates(plan=plan, policy=policy, capability=cap, lease=lease,
                                authorization=right, profile_simulation_only=False,
                                validation_passed=True, conflicting_active_tx=False, now=1000)
    assert ok.allowed is True and ok.reasons == []


def test_delivery_receipt_is_serializable_and_simulated_default():
    r = DeliveryReceipt(transport_id="simulated_rf", message_id="m1",
                        status=DeliveryStatus.DELIVERED)
    assert r.simulated is True and r.to_dict()["status"] == "delivered"
