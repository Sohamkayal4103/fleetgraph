"""Canonical external-agent event envelope (Stage 14D).

Every webhook and WebSocket event uses the SAME envelope shape so an agent verifies and
correlates messages identically across transports. Payloads use explicit, bounded schemas;
internal models are never serialized verbatim.
"""

from __future__ import annotations

from datetime import datetime

from aithernet.state.models import InteropMessage
from aithernet.transport.canonical import canonical_hash


def payload_digest(payload: dict) -> str:
    """Deterministic digest of an event payload (``sha256:<hex>``)."""
    return canonical_hash(payload)


def envelope_from_message(message: InteropMessage) -> dict:
    """Build the canonical wire envelope for a persisted :class:`InteropMessage`."""
    return {
        "message_id": message.message_id,
        "schema_version": message.schema_version,
        "agent_id": message.agent_id,
        "subscription_id": message.subscription_id,
        "sequence": message.sequence,
        "event_type": message.event_type,
        "created_at": _iso(message.created_at),
        "mission_id": message.mission_id,
        "run_id": message.run_id,
        "conversation_id": message.conversation_id,
        "artifact_id": message.artifact_id,
        "correlation_id": message.correlation_id,
        "causation_id": message.causation_id,
        "status_revision": message.status_revision,
        "payload": message.payload_json or {},
        "payload_digest": message.payload_digest,
        "expires_at": _iso(message.expires_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
