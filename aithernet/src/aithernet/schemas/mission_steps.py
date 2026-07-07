"""Mission-step request/response schemas (API and persistence surface).

A mission step is the auditable record of executing one coordinator decision through the
routing contract. ``MissionStepRunResponse`` composes the mission, the coordinator
decision, and the resulting step.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.schemas.missions import MissionRead


class MissionStepStatus(str, Enum):
    """Lifecycle/outcome states a mission step can occupy.

    ``blocked`` means the decision could not be routed (unsupported/unavailable target),
    distinct from ``failed`` (an available route whose execution or payload errored).
    """

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MissionStepRead(BaseModel):
    """A persisted mission step as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    mission_id: str
    decision_id: str
    decision: dict = Field(validation_alias="decision_json", serialization_alias="decision")
    route_target: str
    route_action: str
    status: MissionStepStatus
    result: dict = Field(validation_alias="result_json", serialization_alias="result")
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class MissionStepRunResponse(BaseModel):
    """Response for ``POST /missions/{id}/step``: mission + decision + executed step."""

    mission: MissionRead
    decision: CoordinatorDecision
    step: MissionStepRead
