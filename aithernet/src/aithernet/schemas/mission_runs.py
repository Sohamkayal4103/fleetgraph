"""Mission execution-run schemas (Stage 12). Lease tokens are NEVER exposed."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from aithernet.schemas.missions import MissionRead


class MissionRunStatus(str, Enum):
    """Lifecycle states of an autonomous execution run (mirrors the mission lifecycle)."""

    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MissionExecutionRunRead(BaseModel):
    """A persisted execution run as returned by the API (secret-free; no lease token)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    mission_id: str
    node_id: str
    status: MissionRunStatus
    iteration_count: int
    coordinator_call_count: int
    node_state_action_count: int
    coding_agent_action_count: int
    mcp_action_count: int
    response_action_count: int
    consecutive_failures: int
    elapsed_seconds: float
    budgets: dict = Field(validation_alias="budgets_json", serialization_alias="budgets")
    last_mission_step_id: str | None = None
    last_decision_id: str | None = None
    last_route_target: str | None = None
    waiting: dict | None = Field(
        default=None, validation_alias="waiting_json", serialization_alias="waiting"
    )
    blocked_reason: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    final_response: str | None = None
    pause_requested: bool = False
    recovery_count: int = 0
    version: int = 0
    # Lease — sanitized: the owner id (a worker uuid) and timing, never the token.
    execution_owner_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_renewed_at: datetime | None = None
    lease_expiry: datetime | None = None
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    waiting_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_acknowledged_at: datetime | None = None


class MissionExecutionStatus(BaseModel):
    """Combined mission + current run view for ``GET /missions/{id}/execution``."""

    mission: MissionRead
    run: MissionExecutionRunRead | None = None
    budget_remaining: dict = Field(default_factory=dict)
    has_active_run: bool = False


class MissionTimelineEntry(BaseModel):
    """One chronological mission timeline entry (events + step summaries)."""

    at: datetime
    kind: str  # "event" | "step"
    event_type: str | None = None
    source: str | None = None
    message: str
    step_id: str | None = None
    route_target: str | None = None
    status: str | None = None
    coding_task_id: str | None = None  # beta.7 (FIX 9): surface the coding task id for inspection


class MissionTimeline(BaseModel):
    mission_id: str
    entries: list[MissionTimelineEntry] = Field(default_factory=list)


class MissionWorkerStatus(BaseModel):
    """Status of the autonomous mission worker manager (Stage 12)."""

    enabled: bool
    running: bool
    degraded: bool = False
    node_id: str
    worker_count: int
    active_missions: list[str] = Field(default_factory=list)
    active_count: int = 0
    queued_count: int = 0
    last_error: str | None = None


class MissionStartResponse(BaseModel):
    """Response for start/resume — returns after queueing, not after completion."""

    mission: MissionRead
    run: MissionExecutionRunRead
    queued: bool
    detail: str
