"""Event response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EventRead(BaseModel):
    """Runtime event as returned by the API and streamed over the event bus."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    mission_id: str | None
    event_type: str
    source: str
    message: str
    payload: dict = Field(validation_alias="payload_json", serialization_alias="payload")
    created_at: datetime
