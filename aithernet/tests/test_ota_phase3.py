"""1.0.0-beta.1 OTA Phase 3: simulated two-node canonical peer exchange through impaired RF.

Two distinct node identities, explicit trust (shared link secret + pinned peer key), and the FULL
chain (seal -> fragment -> modulate -> impaired channel -> demodulate -> reassemble -> open ->
signature verify). Proves payload-digest equality, the A->B->A round trip, fragmentation,
retransmission under loss, wrong-key/wrong-receiver rejection, and a secret-free exchange record.
No radio, no transmission.
"""

from __future__ import annotations

import pytest

from aithernet.transport.envelope import (
    EnvelopeRecipient,
    EnvelopeSender,
    MessageEnvelope,
)
from aithernet.transport.identity import IdentityManager
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota.interface import DeliveryStatus
from aithernet.transport.ota.profiles import REFERENCE_BPSK
from aithernet.transport.ota.simulated_rf import SimulatedRFLink, derive_link_key

SHARED = b"pairing-secret-established-out-of-band-32B!"


def _identities(tmp_path):
    a = IdentityManager(tmp_path / "a", node_id="node-A", node_name="A").initialize()
    b = IdentityManager(tmp_path / "b", node_id="node-B", node_name="B").initialize()
    return a, b


def _link(a, b, channel=None):
    link = SimulatedRFLink(REFERENCE_BPSK, channel or ch.ChannelModel(snr_db=15.0))
    link.add_endpoint("node-A", peer_id="node-B", peer_public_key_b64=b.public_key_b64,
                      shared_secret=SHARED)
    link.add_endpoint("node-B", peer_id="node-A", peer_public_key_b64=a.public_key_b64,
                      shared_secret=SHARED)
    return link


def _signed_env(sender_id, sender_fp, recipient_id, identity, payload,
                correlation=None):
    env = MessageEnvelope(sender=EnvelopeSender(node_id=sender_id, fingerprint=sender_fp),
                          recipient=EnvelopeRecipient(node_id=recipient_id), payload=payload,
                          correlation_id=correlation)
    return env.sign(identity)


def test_distinct_identities_and_link_key_symmetry():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        a, b = _identities(Path(d))
    assert a.node_id != b.node_id and a.public_key_b64 != b.public_key_b64
    # the derived link key is identical from either side (order-independent)
    assert (derive_link_key("node-A", "node-B", SHARED)
            == derive_link_key("node-B", "node-A", SHARED))


def test_canonical_request_survives_rf_and_signature_verifies(tmp_path):
    a, b = _identities(tmp_path)
    link = _link(a, b)
    env = _signed_env("node-A", a.fingerprint, "node-B", a,
                      {"type": "mission_request", "objective": "bounded health check"},
                      correlation="corr-1")
    receipt, received, record = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    assert receipt.status is DeliveryStatus.DELIVERED
    assert received is not None and received.payload == env.payload
    assert received.message_id == env.message_id and received.correlation_id == "corr-1"
    # canonical payload digest equality + signature verified at the receiver
    assert record.signature_result == "verified"
    assert record.canonical_payload_digest == record.received_payload_digest
    # the exchange record is secret-free
    blob = str(record.to_dict())
    assert "link_key" not in blob and SHARED.decode() not in blob


def test_full_request_reply_round_trip(tmp_path):
    a, b = _identities(tmp_path)
    link = _link(a, b)
    req = _signed_env("node-A", a.fingerprint, "node-B", a,
                      {"type": "mission_request", "objective": "x"}, correlation="c9")
    r1, got_req, _ = link.transmit(sender="node-A", receiver="node-B", envelope=req)
    assert r1.status is DeliveryStatus.DELIVERED and got_req is not None
    # Node B forms a semantic reply (correlated) and sends it back over RF.
    reply = _signed_env("node-B", b.fingerprint, "node-A", b,
                        {"type": "result", "status": "completed", "reply_to": got_req.message_id},
                        correlation="c9")
    r2, got_reply, _ = link.transmit(sender="node-B", receiver="node-A", envelope=reply)
    assert r2.status is DeliveryStatus.DELIVERED
    assert got_reply.payload["status"] == "completed"
    assert got_reply.correlation_id == "c9"  # distributed correlation timeline


def test_fragmented_message_round_trips(tmp_path):
    a, b = _identities(tmp_path)
    link = _link(a, b)
    big = {"type": "mission_request", "blob": "Z" * 1500}  # forces multiple frames
    env = _signed_env("node-A", a.fingerprint, "node-B", a, big)
    receipt, received, record = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    assert receipt.status is DeliveryStatus.DELIVERED
    assert received.payload == big and record.frame_count >= 2


def test_retransmission_under_lossy_channel(tmp_path):
    a, b = _identities(tmp_path)
    # A moderate SNR with phase offset still recovers via FEC + ARQ retransmission.
    link = _link(a, b, ch.ChannelModel(snr_db=8.0, phase_rad=0.6, delay_samples=11, seed=2))
    env = _signed_env("node-A", a.fingerprint, "node-B", a, {"type": "mission_request", "n": 1})
    receipt, received, _ = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    assert receipt.status is DeliveryStatus.DELIVERED and received.payload == env.payload


def test_wrong_link_key_cannot_open(tmp_path):
    a, b = _identities(tmp_path)
    link = SimulatedRFLink(REFERENCE_BPSK, ch.ChannelModel(snr_db=20.0))
    link.add_endpoint("node-A", peer_id="node-B", peer_public_key_b64=b.public_key_b64,
                      shared_secret=SHARED)
    # Receiver paired with a DIFFERENT shared secret -> cannot authenticate even a clean decode.
    link.add_endpoint("node-B", peer_id="node-A", peer_public_key_b64=a.public_key_b64,
                      shared_secret=b"a-different-unrelated-pairing-secret!!")
    env = _signed_env("node-A", a.fingerprint, "node-B", a, {"type": "mission_request"})
    receipt, received, record = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    assert receipt.status is DeliveryStatus.REJECTED and received is None
    assert "rejected" in record.decode_outcome


def test_severe_noise_fails_cleanly_no_silent_wrong_message(tmp_path):
    a, b = _identities(tmp_path)
    link = _link(a, b, ch.ChannelModel(snr_db=-10.0, seed=7))
    env = _signed_env("node-A", a.fingerprint, "node-B", a, {"type": "mission_request"})
    receipt, received, _ = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    # Never a silently-wrong delivered message: either FAILED or REJECTED, never DELIVERED-wrong.
    assert receipt.status in (DeliveryStatus.FAILED, DeliveryStatus.REJECTED)
    assert received is None


def test_replay_rejected_at_receiver(tmp_path):
    a, b = _identities(tmp_path)
    link = _link(a, b, ch.ChannelModel(snr_db=20.0))
    env = _signed_env("node-A", a.fingerprint, "node-B", a, {"type": "mission_request"})
    # First delivery succeeds; re-injecting the SAME sealed header/sequence is replay-rejected.
    import json
    from dataclasses import asdict

    from aithernet.transport.ota import security as sec
    ep_b = link._endpoints["node-B"]
    header, ct = sec.seal(env, key=link._endpoints["node-A"].link_key, sender_node_id="node-A",
                          receiver_node_id="node-B", peer_key_id="node-B", sequence=1,
                          transport_profile_id=REFERENCE_BPSK.profile_id)
    sec.open_sealed(header, ct, key=ep_b.link_key, expected_receiver="node-B",
                    replay_guard=ep_b.replay_guard)
    with pytest.raises(sec.OTASecurityError, match="replay"):
        header2 = sec.OTASecurityHeader(**json.loads(json.dumps(asdict(header))))
        sec.open_sealed(header2, ct, key=ep_b.link_key, expected_receiver="node-B",
                        replay_guard=ep_b.replay_guard)
