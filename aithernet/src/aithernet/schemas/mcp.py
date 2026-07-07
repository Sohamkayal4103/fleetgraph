"""MCP tool-call request/response schemas (API and persistence surface)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MCPToolCallStatus(str, Enum):
    """Lifecycle states an MCP tool call can occupy."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class MCPToolCallCreate(BaseModel):
    """A request to call an MCP tool (tool_name + targeting metadata + arguments).

    Mission/step ids are attached by the orchestrator from execution context — never
    supplied by the coordinator model.
    """

    tool_name: str = Field(min_length=1, description="The MCP tool to invoke.")
    mission_id: str | None = Field(default=None, description="Optional owning mission.")
    mission_step_id: str | None = Field(default=None, description="Optional owning mission step.")
    task_id: str | None = Field(default=None, description="Optional owning coding task.")
    caller: str = Field(
        default="user",
        description="Who initiated the call (user, coordinator, coding_agent, runtime).",
    )
    arguments: dict = Field(
        default_factory=dict, description="Arguments passed to the MCP tool."
    )


class MCPToolCallBody(BaseModel):
    """Body for ``POST /mcp/tools/{tool_name}/call`` (tool_name comes from the path)."""

    mission_id: str | None = Field(default=None, description="Optional owning mission.")
    task_id: str | None = Field(default=None, description="Optional owning coding task.")
    caller: str = Field(default="user", description="Who initiated the call.")
    arguments: dict = Field(
        default_factory=dict, description="Arguments passed to the MCP tool."
    )


class MCPToolCallRead(BaseModel):
    """A persisted MCP tool call as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    mission_id: str | None
    mission_step_id: str | None = None
    task_id: str | None
    caller: str
    tool_name: str
    arguments: dict = Field(validation_alias="arguments_json", serialization_alias="arguments")
    status: MCPToolCallStatus
    result: dict = Field(validation_alias="result_json", serialization_alias="result")
    error: str | None
    error_type: str | None = None
    session_id: str | None = None
    session_generation: int | None = None
    tool_catalog_generation: int | None = None
    duration_ms: int | None = None
    # Stage 13A.5: which RF backend executed this call (existing rows default to legacy).
    backend_id: str = "legacy_gr_mcp"
    backend_kind: str | None = None
    backend_version: str | None = None
    backend_source_revision: str | None = None
    result_summary_type: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
