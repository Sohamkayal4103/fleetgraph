"""Mission request/response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MissionStatus(str, Enum):
    """Lifecycle states a mission can occupy.

    Stage 12 adds the autonomous-execution states (``queued``/``waiting``/``paused``/
    ``blocked``) additively; manual one-step execution still uses ``received``/``active``.
    """

    RECEIVED = "received"
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Terminal mission states — a worker never reopens these (only an explicit new run does).
TERMINAL_MISSION_STATES: frozenset[MissionStatus] = frozenset(
    {
        MissionStatus.COMPLETED,
        MissionStatus.BLOCKED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
    }
)


class MissionCreate(BaseModel):
    """Body for ``POST /missions``."""

    content: str = Field(min_length=1, description="The mission instruction/text.")
    source_type: str = Field(
        default="user", description="Origin class of the mission (e.g. user, peer)."
    )
    source_id: str | None = Field(
        default=None, description="Optional identifier of the originating source."
    )
    metadata: dict = Field(
        default_factory=dict, description="Arbitrary structured metadata for the mission."
    )


class MissionRead(BaseModel):
    """Mission as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source_type: str
    source_id: str | None
    content: str
    status: MissionStatus
    created_at: datetime
    updated_at: datetime
    metadata: dict = Field(validation_alias="metadata_json", serialization_alias="metadata")
