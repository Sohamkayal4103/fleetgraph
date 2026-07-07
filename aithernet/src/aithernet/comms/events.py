"""Sanitized coordinator-communication event types (Stage 13B, Part R).

Payloads carry ids, states, and bounded reasons only — never full message text, structured
data, prompts, keys, signatures, headers, environments, or HTTP bodies.
"""

from __future__ import annotations

# Outbound peer messaging + reply waits (mission-linked).
EVENT_PEER_MESSAGE_QUEUED = "mission.peer_message.queued"
EVENT_PEER_MESSAGE_DELIVERY_UPDATED = "mission.peer_message.delivery_updated"
EVENT_REPLY_WAIT_CREATED = "mission.peer_reply_wait.created"
EVENT_REPLY_WAIT_SATISFIED = "mission.peer_reply_wait.satisfied"
EVENT_REPLY_WAIT_TIMED_OUT = "mission.peer_reply_wait.timed_out"
EVENT_REPLY_WAIT_CANCELLED = "mission.peer_reply_wait.cancelled"
EVENT_PEER_REPLY_RECEIVED = "mission.peer_reply.received"
EVENT_PEER_REPLY_IGNORED = "mission.peer_reply.ignored"

# Inbound coordinator requests.
EVENT_INBOUND_REQUEST_RECEIVED = "inbound_request.received"
EVENT_INBOUND_REQUEST_MISSION_CREATED = "inbound_request.mission_created"
EVENT_INBOUND_REQUEST_DUPLICATE = "inbound_request.duplicate"
EVENT_INBOUND_REQUEST_REJECTED = "inbound_request.rejected"
EVENT_INBOUND_REQUEST_RESPONSE_REQUIRED = "inbound_request.response_required"
EVENT_INBOUND_REQUEST_RESPONSE_QUEUED = "inbound_request.response_queued"

# Resume.
EVENT_RESUME_QUEUED = "mission.communication.resume_queued"
EVENT_RESUME_SKIPPED = "mission.communication.resume_skipped"

# Canonical conversation resolution (Stage 13D).
EVENT_CONVERSATION_CANONICALIZED = "conversation.canonicalized"
EVENT_CONVERSATION_REPAIR_COMPLETED = "conversation.repair.completed"
