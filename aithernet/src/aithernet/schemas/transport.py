"""Agent-transport request/response schemas (Stage 13A).

These are the secret-free API surface for identity, peers, outbox/inbox, and worker status.
They NEVER expose private keys, raw signatures, authentication headers, or environment. A
peer's *public* key and fingerprint are shown (they are public); stored envelope signatures
are not surfaced through these read models.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TrustState(str, Enum):
    UNTRUSTED = "untrusted"
    PENDING = "pending"
    TRUSTED = "trusted"
    REVOKED = "revoked"


class PeerRole(str, Enum):
    NODE = "node"
    MANAGER = "manager"
    EXTERNAL_AGENT = "external_agent"
    UNKNOWN = "unknown"


# -- identity --------------------------------------------------------------------


class IdentityStatus(BaseModel):
    """Local identity status (public metadata only — never the private key)."""

    initialized: bool
    node_id: str | None = None
    node_name: str | None = None
    identity_version: int | None = None
    fingerprint: str | None = None
    fingerprint_short: str | None = None
    public_key: str | None = None
    created_at: datetime | None = None
    rotated_at: datetime | None = None
    enabled: bool | None = None
    state_directory: str | None = None


class PublicIdentityDocument(BaseModel):
    """The shareable public identity document (Part B `identity public`)."""

    scheme: str
    identity_version: int
    node_id: str
    node_name: str
    public_key: str
    fingerprint: str
    created_at: str
    enabled: bool
    rotated_at: str | None = None


# -- peers -----------------------------------------------------------------------


class PeerCreate(BaseModel):
    name: str = Field(min_length=1)
    role: PeerRole = PeerRole.NODE
    endpoint_url: str | None = None
    expected_node_id: str | None = None
    public_key: str | None = None
    fingerprint: str | None = None
    transport: str = "http"
    tls_verify: bool = True
    metadata: dict = Field(default_factory=dict)


class PeerUpdate(BaseModel):
    name: str | None = None
    role: PeerRole | None = None
    endpoint_url: str | None = None
    expected_node_id: str | None = None
    tls_verify: bool | None = None
    enabled: bool | None = None
    metadata: dict | None = None


class PeerTrustRequest(BaseModel):
    """Explicitly trust a peer, pinning its public key (Part C `peer trust`)."""

    public_key: str | None = Field(
        default=None, description="Public key to pin; must match an already-stored key if set."
    )
    fingerprint: str | None = Field(
        default=None, description="Expected fingerprint to confirm the pinned key."
    )


class PeerRead(BaseModel):
    """A peer (trust + transport view). Public key/fingerprint are public; no private data."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role: str
    endpoint_url: str | None
    expected_node_id: str | None
    public_key: str | None
    fingerprint: str | None
    trust_state: str
    enabled: bool
    transport: str
    tls_verify: bool
    status: str
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    capability_snapshot: dict | None = Field(
        default=None, validation_alias="capability_snapshot_json",
        serialization_alias="capability_snapshot",
    )
    capability_snapshot_at: datetime | None
    last_manifest_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None


# -- outbox / inbox --------------------------------------------------------------


class OutboxMessageRead(BaseModel):
    """An outbound delivery record (no raw signature/envelope is exposed)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    message_id: str
    peer_id: str
    kind: str
    protocol_version: str
    status: str
    attempt_count: int
    max_attempts: int
    conversation_id: str | None
    correlation_id: str | None
    mission_id: str | None
    envelope_hash: str | None
    next_attempt_at: datetime | None
    first_attempted_at: datetime | None
    last_attempted_at: datetime | None
    delivered_at: datetime | None
    acknowledged_at: datetime | None
    remote_ack_id: str | None
    http_status: int | None
    error_type: str | None
    last_error: str | None
    dead_letter_reason: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InboxMessageRead(BaseModel):
    """An inbound delivery record. ``payload`` is operator data; the signature is not shown."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    message_id: str
    peer_id: str | None
    sender_node_id: str
    sender_fingerprint: str
    kind: str
    protocol_version: str
    conversation_id: str | None
    correlation_id: str | None
    reply_to_message_id: str | None
    mission_id: str | None
    processing_state: str
    ack_status: str
    ack_id: str | None
    duplicate_count: int
    rejection_reason: str | None
    envelope_hash: str
    payload: dict = Field(
        validation_alias="payload_json", serialization_alias="payload"
    )
    received_at: datetime
    authenticated_at: datetime | None


# -- transport status / worker ---------------------------------------------------


class TransportWorkerStatus(BaseModel):
    enabled: bool
    running: bool
    degraded: bool = False
    worker_count: int
    in_flight: int = 0
    last_error: str | None = None


class TransportStatus(BaseModel):
    """Combined transport status: identity + worker + outbox/inbox counts."""

    enabled: bool
    identity: IdentityStatus
    worker: TransportWorkerStatus
    outbox_counts: dict = Field(default_factory=dict)
    inbox_count: int = 0
    peer_count: int = 0
    trusted_peer_count: int = 0


# -- operator send ---------------------------------------------------------------


class AgentMessageSendRequest(BaseModel):
    """Create an outbound operator message to a peer (Part M)."""

    peer_id: str
    subject: str | None = None
    text: str | None = None
    data: dict = Field(default_factory=dict)
    content_type: str = "application/json"
    kind: str = "agent_message"
    conversation_id: str | None = None
    correlation_id: str | None = None
    reply_to_message_id: str | None = None
    mission_id: str | None = None
    expires_in_seconds: float | None = None
    send_now: bool = Field(
        default=False,
        description="Attempt one delivery synchronously after persisting the outbox record.",
    )


class AgentMessageSendResponse(BaseModel):
    message: OutboxMessageRead
    queued: bool
    detail: str
