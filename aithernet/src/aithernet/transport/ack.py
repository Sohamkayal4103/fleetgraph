"""Signed delivery acknowledgements (Stage 13A, Part I).

The receiver returns a signed acknowledgement for every authenticated delivery. The sender
marks its outbox record ``acknowledged`` ONLY after verifying this ACK — never on a bare
HTTP 200. The ACK signature covers the canonical serialization of all fields except the
signature, exactly like the envelope.
"""

from __future__ import annotations

from enum import Enum

from aithernet.state.models import new_uuid, utcnow
from aithernet.transport.canonical import canonical_bytes
from aithernet.transport.envelope import PROTOCOL, PROTOCOL_VERSION
from aithernet.transport.identity import NodeIdentity, verify_signature


class AckStatus(str, Enum):
    """Outcome the receiver reports for a delivery."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


#: Cap on the optional human reason carried in an ACK.
_MAX_REASON = 300


def build_ack(
    identity: NodeIdentity,
    *,
    original_message_id: str,
    status: AckStatus,
    inbox_record_id: str | None,
    duplicate: bool,
    reason: str | None = None,
) -> dict:
    """Construct and SIGN a delivery acknowledgement from this node's identity."""
    ack = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "acknowledgement_id": new_uuid(),
        "original_message_id": original_message_id,
        "accepting_node_id": identity.node_id,
        "accepting_fingerprint": identity.fingerprint,
        "status": status.value,
        "received_at": utcnow().isoformat(),
        "inbox_record_id": inbox_record_id,
        "duplicate": duplicate,
        "reason": (reason or "")[:_MAX_REASON] or None,
    }
    ack["signature"] = identity.sign(canonical_bytes(ack))
    return ack


def verify_ack(
    ack: object,
    *,
    public_key_b64: str,
    expected_message_id: str,
    expected_node_id: str,
) -> tuple[bool, str]:
    """Verify a received ACK. Returns ``(ok, reason)``; ``ok=False`` => delivery failure.

    Checks (all required): well-formed object, supported protocol/version, signature valid
    against the trusted peer key, the accepting node id matches the expected peer, and the
    original message id matches the delivered message. A bare HTTP 200 with an invalid or
    unsigned ACK is therefore treated as a failed delivery, never as acknowledgement.
    """
    if not isinstance(ack, dict):
        return False, "ack_not_object"
    if ack.get("protocol") != PROTOCOL or str(ack.get("protocol_version")) != PROTOCOL_VERSION:
        return False, "ack_protocol_mismatch"
    signature = ack.get("signature")
    if not isinstance(signature, str) or not signature:
        return False, "ack_unsigned"
    if ack.get("original_message_id") != expected_message_id:
        return False, "ack_wrong_message"
    if ack.get("accepting_node_id") != expected_node_id:
        return False, "ack_wrong_peer"
    if not verify_signature(public_key_b64, canonical_bytes(ack), signature):
        return False, "ack_bad_signature"
    return True, ack.get("status", AckStatus.ACCEPTED.value)
