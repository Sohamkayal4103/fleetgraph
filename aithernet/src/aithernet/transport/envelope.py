"""Canonical signed transport envelope + message kinds (Stage 13A, Parts D/E).

One versioned envelope carries every transport message between nodes. Its ``signature``
covers the canonical serialization (see :mod:`aithernet.transport.canonical`) of every
other field, so any change to an authenticated field invalidates the signature. The
envelope is transport-level only — its payload is stored as data and NEVER executed as a
mission, route, tool call, or shell command in Stage 13A.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field

from aithernet.state.models import new_uuid, utcnow
from aithernet.transport.canonical import canonical_bytes, canonical_hash
from aithernet.transport.errors import (
    EnvelopeError,
    ExpiredMessageError,
    PayloadTooLargeError,
    ProtocolVersionError,
    RecipientMismatchError,
)
from aithernet.transport.identity import NodeIdentity, verify_signature

#: Protocol identity + supported versions.
PROTOCOL = "aithernet-agent"
PROTOCOL_VERSION = "1"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"1"})


class MessageKind(str, Enum):
    """The small transport-level message set supported in Stage 13A.

    These are protocol messages, NOT mission workflows — there is no delegation semantics.
    """

    AGENT_MESSAGE = "agent_message"
    DELIVERY_ACK = "delivery_ack"
    CAPABILITY_REQUEST = "capability_request"
    CAPABILITY_MANIFEST = "capability_manifest"
    PING = "ping"
    PONG = "pong"
    # Stage 13D.2: an authenticated artifact byte-range pull. NOT a deliverable message — it is
    # verified by the dedicated artifact endpoint, never stored in the inbox or executed.
    ARTIFACT_PULL = "artifact_pull"


#: Kinds a peer may DELIVER to ``POST /agent/v1/messages`` (ack is a response, not delivered).
DELIVERABLE_KINDS = frozenset(
    {
        MessageKind.AGENT_MESSAGE.value,
        MessageKind.CAPABILITY_REQUEST.value,
        MessageKind.CAPABILITY_MANIFEST.value,
        MessageKind.PING.value,
        MessageKind.PONG.value,
    }
)


class EnvelopeSender(BaseModel):
    node_id: str
    fingerprint: str


class EnvelopeRecipient(BaseModel):
    node_id: str | None = None
    agent_id: str | None = None


class MessageEnvelope(BaseModel):
    """A signed transport envelope. ``signature`` authenticates all other fields."""

    protocol: str = PROTOCOL
    protocol_version: str = PROTOCOL_VERSION
    message_id: str = Field(default_factory=new_uuid)
    conversation_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to_message_id: str | None = None

    sender: EnvelopeSender
    recipient: EnvelopeRecipient

    # beta.3 mesh binding (additive, optional for backward compatibility with beta.2 peers). When
    # present these are part of the canonical signed body, so the receiving node can authorize the
    # message against its OWN local mesh membership before any MissionEngine ingress.
    # ``authority_id`` is the mesh's tenant id (hosted) or standalone owner fingerprint — a foreign
    # authority never matches a local mesh, which makes cross-tenant isolation cryptographic.
    mesh_id: str | None = None
    authority_id: str | None = None

    kind: str = MessageKind.AGENT_MESSAGE.value
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    expires_at: str | None = None
    nonce: str = Field(default_factory=new_uuid)

    mission_id: str | None = None
    mission_run_id: str | None = None
    mission_step_id: str | None = None

    content_type: str = "application/json"
    payload: dict = Field(default_factory=dict)
    signature: str | None = None

    # -- canonicalization / signing ---------------------------------------------

    def authenticated_dict(self) -> dict:
        """The full envelope as a plain dict (the signature is excluded by canonicalization)."""
        return self.model_dump(mode="json")

    def canonical_hash(self) -> str:
        """Stable ``sha256:<hex>`` over the authenticated (signature-excluded) envelope."""
        return canonical_hash(self.authenticated_dict())

    def sign(self, identity: NodeIdentity) -> MessageEnvelope:
        """Return a copy signed by ``identity`` (sets ``signature``)."""
        body = canonical_bytes(self.authenticated_dict())
        return self.model_copy(update={"signature": identity.sign(body)})

    def verify_signature_with(self, public_key_b64: str) -> bool:
        """Verify the envelope signature against ``public_key_b64``."""
        if not self.signature:
            return False
        body = canonical_bytes(self.authenticated_dict())
        return verify_signature(public_key_b64, body, self.signature)


def build_envelope(
    *,
    identity: NodeIdentity,
    recipient_node_id: str | None,
    recipient_agent_id: str | None,
    kind: str,
    payload: dict,
    conversation_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    reply_to_message_id: str | None = None,
    mission_id: str | None = None,
    mission_run_id: str | None = None,
    mission_step_id: str | None = None,
    content_type: str = "application/json",
    expires_in_seconds: float | None = None,
    message_id: str | None = None,
    mesh_id: str | None = None,
    authority_id: str | None = None,
) -> MessageEnvelope:
    """Construct and SIGN an outbound envelope from this node's identity."""
    expires_at = None
    if expires_in_seconds is not None:
        expires_at = (utcnow() + timedelta(seconds=expires_in_seconds)).isoformat()
    envelope = MessageEnvelope(
        message_id=message_id or new_uuid(),
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        reply_to_message_id=reply_to_message_id,
        sender=EnvelopeSender(node_id=identity.node_id, fingerprint=identity.fingerprint),
        recipient=EnvelopeRecipient(node_id=recipient_node_id, agent_id=recipient_agent_id),
        mesh_id=mesh_id,
        authority_id=authority_id,
        kind=kind,
        expires_at=expires_at,
        mission_id=mission_id,
        mission_run_id=mission_run_id,
        mission_step_id=mission_step_id,
        content_type=content_type,
        payload=payload,
    )
    return envelope.sign(identity)


# -- inbound validation ----------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EnvelopeError(f"Invalid timestamp '{value}'.") from exc
    # SQLite round-trips drop tzinfo; treat a naive timestamp as UTC so comparisons with the
    # timezone-aware ``utcnow()`` never raise.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_envelope(raw: object, *, maximum_payload_bytes: int) -> MessageEnvelope:
    """Parse + structurally validate an inbound envelope (no trust/signature checks yet).

    Enforces protocol/version, required structure, and a bounded payload size. Raises a
    specific :class:`TransportError` subclass on each failure so the API can map it to a
    sanitized response.
    """
    if not isinstance(raw, dict):
        raise EnvelopeError("Envelope must be a JSON object.")
    if raw.get("protocol") != PROTOCOL:
        raise ProtocolVersionError(f"Unsupported protocol '{raw.get('protocol')}'.")
    if str(raw.get("protocol_version")) not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolVersionError(
            f"Unsupported protocol version '{raw.get('protocol_version')}'."
        )
    try:
        envelope = MessageEnvelope.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> sanitized malformed-envelope
        raise EnvelopeError(f"Malformed envelope: {type(exc).__name__}.") from exc

    if not envelope.signature:
        raise EnvelopeError("Envelope is missing a signature.")
    # Bound the payload size on the canonical form (defends against deep/large payloads).
    if len(canonical_bytes({"payload": envelope.payload})) > maximum_payload_bytes:
        raise PayloadTooLargeError("Envelope payload exceeds the configured size limit.")
    return envelope


def validate_recipient(envelope: MessageEnvelope, *, local_node_id: str) -> None:
    """Ensure the envelope is addressed to this node (when a recipient node id is given)."""
    recipient_node = envelope.recipient.node_id
    if recipient_node is not None and recipient_node != local_node_id:
        raise RecipientMismatchError("Envelope recipient does not match this node.")


def validate_timing(
    envelope: MessageEnvelope, *, accepted_clock_skew_seconds: float, now: datetime | None = None
) -> None:
    """Validate ``created_at`` skew and ``expires_at`` against the accepted clock skew."""
    now = now or utcnow()
    skew = timedelta(seconds=accepted_clock_skew_seconds)
    created = _parse_dt(envelope.created_at)
    if created is None:
        raise EnvelopeError("Envelope is missing a valid created_at timestamp.")
    if created > now + skew:
        raise ExpiredMessageError("Envelope created_at is too far in the future (clock skew).")
    expires = _parse_dt(envelope.expires_at)
    if expires is not None and now > expires + skew:
        raise ExpiredMessageError("Envelope has expired.")


def is_expired(envelope_expires_at: str | None, *, now: datetime | None = None) -> bool:
    """True if an outbound envelope's ``expires_at`` is in the past (do not send it)."""
    expires = _parse_dt(envelope_expires_at)
    if expires is None:
        return False
    return (now or utcnow()) > expires
