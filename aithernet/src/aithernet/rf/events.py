"""Sanitized RF-backend event types (Stage 13A.5, Part P).

Event payloads carry only ids, counts, states, and sanitized reasons — never full MCP
results, full tool schemas, environment variables, binary artifacts, prompts, or unnecessary
absolute private paths (artifact references are workspace-relative).
"""

from __future__ import annotations

EVENT_BACKEND_STARTING = "rf.backend.starting"
EVENT_BACKEND_READY = "rf.backend.ready"
EVENT_BACKEND_STOPPED = "rf.backend.stopped"
EVENT_BACKEND_DEGRADED = "rf.backend.degraded"
EVENT_BACKEND_FAILED = "rf.backend.failed"
EVENT_BACKEND_RESTARTED = "rf.backend.restarted"

EVENT_TOOLS_REFRESHED = "rf.tools.refreshed"

EVENT_CALL_STARTED = "rf.call.started"
EVENT_CALL_COMPLETED = "rf.call.completed"
EVENT_CALL_FAILED = "rf.call.failed"

EVENT_CONTEXT_REFRESHED = "rf.context.refreshed"
EVENT_CONTEXT_STALE = "rf.context.stale"

EVENT_ARTIFACT_INDEXED = "rf.artifact.indexed"
EVENT_ARTIFACT_REJECTED = "rf.artifact.rejected"
