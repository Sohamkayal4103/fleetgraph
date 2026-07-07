"""Event-type constants for the Stage 14D external-agent gateway.

Two families live here:

* ``EXT_*`` — the canonical *external* event types delivered to agents over webhooks /
  WebSocket (the ``agent.*`` envelope event_type values).
* ``EVENT_*`` — *internal* sanitized runtime events emitted to the node event bus / audit
  log for operators. They never carry secrets, signatures, raw callback bodies, or URLs.
"""

from __future__ import annotations

# --- External event envelope types (delivered to agents) -------------------
EXT_MISSION_ACCEPTED = "agent.mission.accepted"
EXT_MISSION_STARTED = "agent.mission.started"
EXT_MISSION_STATUS = "agent.mission.status"
EXT_MISSION_COMPLETED = "agent.mission.completed"
EXT_MISSION_FAILED = "agent.mission.failed"
EXT_MISSION_CANCELLED = "agent.mission.cancelled"
EXT_ARTIFACT_READY = "agent.artifact.ready"
EXT_ARTIFACT_TRANSFER_FAILED = "agent.artifact.transfer_failed"
EXT_CONVERSATION_REPLY = "agent.conversation.reply"
EXT_DELIVERY_RETRYING = "agent.delivery.retrying"
EXT_DELIVERY_DEAD_LETTERED = "agent.delivery.dead_lettered"
EXT_SUBSCRIPTION_PAUSED = "agent.subscription.paused"

#: Terminal external event types — these must never be collapsed and get full retention.
EXT_TERMINAL = frozenset(
    {EXT_MISSION_COMPLETED, EXT_MISSION_FAILED, EXT_MISSION_CANCELLED}
)

#: Every external event type an agent may subscribe to.
EXT_ALL = frozenset(
    {
        EXT_MISSION_ACCEPTED,
        EXT_MISSION_STARTED,
        EXT_MISSION_STATUS,
        EXT_MISSION_COMPLETED,
        EXT_MISSION_FAILED,
        EXT_MISSION_CANCELLED,
        EXT_ARTIFACT_READY,
        EXT_ARTIFACT_TRANSFER_FAILED,
        EXT_CONVERSATION_REPLY,
    }
)

# --- Internal sanitized runtime / audit events -----------------------------
EVENT_AGENT_CREATED = "agent.created"
EVENT_AGENT_ENABLED = "agent.enabled"
EVENT_AGENT_DISABLED = "agent.disabled"
EVENT_CREDENTIAL_CREATED = "agent.credential.created"
EVENT_CREDENTIAL_REVOKED = "agent.credential.revoked"
EVENT_AUTH_FAILED = "agent.authentication.failed"
EVENT_ENDPOINT_REGISTERED = "agent.endpoint.registered"
EVENT_ENDPOINT_VERIFICATION_STARTED = "agent.endpoint.verification_started"
EVENT_ENDPOINT_VERIFIED = "agent.endpoint.verified"
EVENT_ENDPOINT_REJECTED = "agent.endpoint.rejected"
EVENT_SUBSCRIPTION_CREATED = "agent.subscription.created"
EVENT_SUBSCRIPTION_PAUSED = "agent.subscription.paused"
EVENT_SUBSCRIPTION_RESUMED = "agent.subscription.resumed"
EVENT_MESSAGE_ENQUEUED = "agent.message.enqueued"
EVENT_DELIVERY_STARTED = "agent.delivery.started"
EVENT_DELIVERY_SUCCEEDED = "agent.delivery.succeeded"
EVENT_DELIVERY_RETRY_SCHEDULED = "agent.delivery.retry_scheduled"
EVENT_DELIVERY_DEAD_LETTERED = "agent.delivery.dead_lettered"
EVENT_RECEIPT_ACCEPTED = "agent.receipt.accepted"
EVENT_RECEIPT_REJECTED = "agent.receipt.rejected"
EVENT_WS_CONNECTED = "agent.websocket.connected"
EVENT_WS_DISCONNECTED = "agent.websocket.disconnected"
EVENT_WS_REPLAY_STARTED = "agent.websocket.replay_started"
EVENT_WS_REPLAY_COMPLETED = "agent.websocket.replay_completed"
EVENT_RATE_LIMIT_EXCEEDED = "agent.rate_limit.exceeded"

#: Maps a mission lifecycle event_type (from the mission engine) to an external event type.
MISSION_EVENT_MAP = {
    "mission.run.started": EXT_MISSION_STARTED,
    "mission.queued": EXT_MISSION_STATUS,
    "mission.waiting": EXT_MISSION_STATUS,
    "mission.paused": EXT_MISSION_STATUS,
    "mission.resumed": EXT_MISSION_STATUS,
    "mission.completed": EXT_MISSION_COMPLETED,
    "mission.failed": EXT_MISSION_FAILED,
    "mission.blocked": EXT_MISSION_FAILED,
    "mission.cancelled": EXT_MISSION_CANCELLED,
}
