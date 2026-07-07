"""RF backend API request/response schemas (Stage 13A.5). Secret-free; no env or private data."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RFToolCallRequest(BaseModel):
    """Body for ``POST /rf/backends/{backend_id}/call`` — one atomic tool call."""

    tool_name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    caller: str = Field(default="user")
    mission_id: str | None = None
    mission_step_id: str | None = None
    task_id: str | None = None


class RFArtifactRead(BaseModel):
    """A safe, workspace-relative artifact reference (never an absolute path or bytes)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    backend_id: str
    relative_path: str
    artifact_kind: str
    media_type: str | None
    size_bytes: int | None
    content_hash: str | None
    mcp_call_id: str | None
    mission_id: str | None
    mission_step_id: str | None = None
    created_at: datetime
    observed_at: datetime
