"""Sanitized agent-transport event types (Stage 13A, Part P).

Event payloads carry only ids, fingerprints, statuses, counts, and sanitized reasons. They
NEVER contain private keys, raw signatures, full payloads, auth headers, entire HTTP
responses, or environment maps.
"""

from __future__ import annotations

# Identity / peers.
EVENT_IDENTITY_INITIALIZED = "identity.initialized"
EVENT_PEER_CREATED = "peer.created"
EVENT_PEER_TRUSTED = "peer.trusted"
EVENT_PEER_REVOKED = "peer.revoked"
EVENT_PEER_KEY_MISMATCH = "peer.key_mismatch"
EVENT_PEER_HEALTH_SUCCEEDED = "peer.health.succeeded"
EVENT_PEER_HEALTH_FAILED = "peer.health.failed"
EVENT_PEER_MANIFEST_REFRESHED = "peer.manifest.refreshed"

# Outbound delivery lifecycle.
EVENT_MESSAGE_QUEUED = "agent.message.queued"
EVENT_DELIVERY_STARTED = "agent.message.delivery.started"
EVENT_MESSAGE_DELIVERED = "agent.message.delivered"
EVENT_MESSAGE_ACKNOWLEDGED = "agent.message.acknowledged"
EVENT_MESSAGE_RETRY_SCHEDULED = "agent.message.retry_scheduled"
EVENT_MESSAGE_FAILED = "agent.message.failed"
EVENT_MESSAGE_DEAD_LETTER = "agent.message.dead_letter"
EVENT_MESSAGE_CANCELLED = "agent.message.cancelled"

# Inbound.
EVENT_MESSAGE_RECEIVED = "agent.message.received"
EVENT_MESSAGE_DUPLICATE = "agent.message.duplicate"
EVENT_MESSAGE_REJECTED = "agent.message.rejected"

# Worker.
EVENT_WORKER_STARTED = "transport.worker.started"
EVENT_WORKER_STOPPED = "transport.worker.stopped"
EVENT_WORKER_DEGRADED = "transport.worker.degraded"
