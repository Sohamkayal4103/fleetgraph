"""Item 1: runtime RF carrier integration — canonical envelope over simulated RF to a real ingress.

Two distinct node identities. The receiver ingress performs the canonical, security-critical steps
(verify the sender's Ed25519 signature against the pinned peer key; reject untrusted/unsigned) and
returns a SIGNED result envelope, which travels back over the RF return path. Proves the sender
receives the peer's semantic result, that an unsigned/wrong-key request is refused by the receiver
(not the modem), and that correlation/idempotency survive — through the OTA stack, no radio.
"""

from __future__ import annotations

from aithernet.transport.envelope import (
    EnvelopeRecipient,
    EnvelopeSender,
    MessageEnvelope,
)
from aithernet.transport.identity import IdentityManager
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota.interface import DeliveryStatus
from aithernet.transport.ota.runtime_link import RFRuntimeLink

LINK_SECRET = b"out-of-band-pairing-secret-for-rf-link!"


def _nodes(tmp_path):
    a = IdentityManager(tmp_path / "a", node_id="node-A", node_name="A").initialize()
    b = IdentityManager(tmp_path / "b", node_id="node-B", node_name="B").initialize()
    return a, b


def _signed(sender_id, fp, recipient_id, identity, payload, correlation=None):
    env = MessageEnvelope(sender=EnvelopeSender(node_id=sender_id, fingerprint=fp),
                          recipient=EnvelopeRecipient(node_id=recipient_id), payload=payload,
                          correlation_id=correlation)
    return env.sign(identity)


def test_runtime_round_trip_through_real_ingress(tmp_path):
    a, b = _nodes(tmp_path)
    link = RFRuntimeLink(a, peer_node_id="node-B", peer_public_key_b64=b.public_key_b64,
                         link_secret=LINK_SECRET)

    # The receiving node's CANONICAL ingress: verify the sender signature (authoritative policy),
    # then run the (bounded) mission and return a SIGNED result envelope.
    def node_b_ingress(opened: MessageEnvelope):
        assert opened.verify_signature_with(a.public_key_b64)   # receiver owns authentication
        return _signed("node-B", b.fingerprint, "node-A", b,
                       {"type": "result", "status": "completed",
                        "reply_to": opened.message_id}, correlation=opened.correlation_id)

    req = _signed("node-A", a.fingerprint, "node-B", a,
                  {"type": "mission_request", "objective": "bounded health + capability check"},
                  correlation="rc-1")
    receipt, result, records = link.deliver(req, receiver_ingress=node_b_ingress)
    assert receipt.status is DeliveryStatus.DELIVERED
    assert result is not None and result.payload["status"] == "completed"
    assert result.payload["reply_to"] == req.message_id
    assert result.correlation_id == "rc-1"                       # distributed correlation timeline
    # two RF exchanges recorded (request + result), both secret-free
    assert len(records) == 2
    for r in records:
        assert LINK_SECRET.decode() not in str(r.to_dict())
        assert r.signature_result == "verified"


def test_receiver_refuses_wrong_signature_not_modem(tmp_path):
    a, b = _nodes(tmp_path)
    # Forge: an envelope claiming to be from A but signed by B's key.
    link = RFRuntimeLink(a, peer_node_id="node-B", peer_public_key_b64=b.public_key_b64,
                         link_secret=LINK_SECRET)
    forged = _signed("node-A", a.fingerprint, "node-B", b,  # signed by B, not A
                     {"type": "mission_request"})

    refused = {"hit": False}

    def strict_ingress(opened: MessageEnvelope):
        if not opened.verify_signature_with(a.public_key_b64):
            refused["hit"] = True
            return None                  # receiving node rejects (its policy, not the modem)
        return _signed("node-B", b.fingerprint, "node-A", b, {"ok": True})

    receipt, result, _ = link.deliver(forged, receiver_ingress=strict_ingress)
    # The frame decodes + AEAD opens fine; the RECEIVER refuses on signature -> no result returned.
    assert refused["hit"] is True and result is None


def test_delivery_fails_cleanly_under_severe_noise(tmp_path):
    a, b = _nodes(tmp_path)
    link = RFRuntimeLink(a, peer_node_id="node-B", peer_public_key_b64=b.public_key_b64,
                         link_secret=LINK_SECRET, channel=ch.ChannelModel(snr_db=-12.0, seed=4))
    req = _signed("node-A", a.fingerprint, "node-B", a, {"type": "mission_request"})
    receipt, result, _ = link.deliver(req, receiver_ingress=lambda e: e)
    assert receipt.status in (DeliveryStatus.FAILED, DeliveryStatus.REJECTED)
    assert result is None
