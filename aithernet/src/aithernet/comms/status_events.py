"""Sanitized remote mission-status event types (Stage 13D.3, Part M).

Payloads carry ids, state names, sequence numbers, and counts ONLY — never prompts, mission
instructions, tool contents, raw envelopes, signatures, keys, paths, credentials, or
environments. Stale notification is persisted (a `stale_notified` flag) so reads do not re-emit
a stale event on every poll.
"""

from __future__ import annotations

# Local publication (this node reporting its own mission to a peer).
EVENT_STATUS_QUEUED = "remote_mission.status.queued"
EVENT_STATUS_SENT = "remote_mission.status.sent"
EVENT_STATUS_QUERY_QUEUED = "remote_mission.status.query_queued"
EVENT_STATUS_RESPONSE_QUEUED = "remote_mission.status.response_queued"

# Inbound (this node receiving a peer's status about the peer's mission).
EVENT_STATUS_RECEIVED = "remote_mission.status.received"
EVENT_STATUS_ACCEPTED = "remote_mission.status.accepted"
EVENT_STATUS_DUPLICATE = "remote_mission.status.duplicate"
EVENT_STATUS_OLDER_IGNORED = "remote_mission.status.older_ignored"
EVENT_STATUS_REJECTED = "remote_mission.status.rejected"
EVENT_STATUS_QUERY_RECEIVED = "remote_mission.status.query_received"

# Snapshot lifecycle.
EVENT_SNAPSHOT_CREATED = "remote_mission.snapshot.created"
EVENT_SNAPSHOT_UPDATED = "remote_mission.snapshot.updated"
EVENT_SNAPSHOT_STALE = "remote_mission.snapshot.stale"
