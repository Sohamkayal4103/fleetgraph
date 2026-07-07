"""Autonomous mission-execution event types (Stage 12, Part R).

Event payloads carry only sanitized, structural facts (ids, statuses, counts, sanitized
reasons). They NEVER contain full prompts, raw Gemini/Codex output, complete MCP responses,
JSONL, credentials, environment maps, or lease tokens. Lease-renewal events are emitted
sparingly (not every heartbeat) to avoid flooding the event log.
"""

from __future__ import annotations

# Run lifecycle.
EVENT_RUN_CREATED = "mission.run.created"
EVENT_QUEUED = "mission.queued"
EVENT_RUN_STARTED = "mission.run.started"
EVENT_RUN_STOPPED = "mission.run.stopped"

# Leasing.
EVENT_LEASE_ACQUIRED = "mission.lease.acquired"
EVENT_LEASE_RENEWED = "mission.lease.renewed"
EVENT_LEASE_LOST = "mission.lease.lost"

# Iteration / decision / action.
EVENT_ITERATION_STARTED = "mission.iteration.started"
EVENT_DECISION_COMPLETED = "mission.decision.completed"
EVENT_ACTION_STARTED = "mission.action.started"
EVENT_ACTION_COMPLETED = "mission.action.completed"
EVENT_ACTION_FAILED = "mission.action.failed"

# Lifecycle outcomes.
EVENT_WAITING = "mission.waiting"
EVENT_PAUSED = "mission.paused"
EVENT_RESUMED = "mission.resumed"
EVENT_CANCEL_REQUESTED = "mission.cancel.requested"
EVENT_CANCELLED = "mission.cancelled"
EVENT_COMPLETED = "mission.completed"
EVENT_BLOCKED = "mission.blocked"
EVENT_FAILED = "mission.failed"
EVENT_RECOVERED = "mission.recovered"
EVENT_BUDGET_EXHAUSTED = "mission.budget.exhausted"
EVENT_TOOL_ACCOUNTABILITY = "mission.tool_accountability"  # beta.7 (FIX 7)

# Worker manager.
EVENT_WORKER_STARTED = "worker.started"
EVENT_WORKER_STOPPED = "worker.stopped"
EVENT_WORKER_DEGRADED = "worker.degraded"
