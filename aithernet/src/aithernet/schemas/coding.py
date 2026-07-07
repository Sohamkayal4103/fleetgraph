"""Coding-task request/response schemas (API and persistence surface)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CodingTaskStatus(str, Enum):
    """Lifecycle states a coding task can occupy."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CodingTaskCreate(BaseModel):
    """Body for ``POST /coding-tasks`` (and the create-and-run endpoint)."""

    objective: str = Field(min_length=1, description="What the coding agent should implement.")
    mission_id: str | None = Field(
        default=None, description="Optional mission this task belongs to."
    )
    context: dict = Field(
        default_factory=dict, description="Structured context handed to the coding agent."
    )
    available_tools: list[str] = Field(
        default_factory=list, description="Tools the task may rely on."
    )
    expected_outputs: list[str] = Field(
        default_factory=list, description="Outputs the task is expected to produce."
    )
    reporting_requirements: list[str] = Field(
        default_factory=list, description="What the agent must report back."
    )


class CodingTaskRead(BaseModel):
    """A coding task as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    mission_id: str | None
    objective: str
    context: dict = Field(validation_alias="context_json", serialization_alias="context")
    available_tools: list[str] = Field(
        validation_alias="available_tools_json", serialization_alias="available_tools"
    )
    expected_outputs: list[str] = Field(
        validation_alias="expected_outputs_json", serialization_alias="expected_outputs"
    )
    reporting_requirements: list[str] = Field(
        validation_alias="reporting_requirements_json",
        serialization_alias="reporting_requirements",
    )
    status: CodingTaskStatus
    provider: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CodingTaskResultRead(BaseModel):
    """The outcome of executing a coding task, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    status: CodingTaskStatus
    summary: str
    stdout: str
    stderr: str
    exit_code: int | None
    artifacts: list = Field(validation_alias="artifacts_json", serialization_alias="artifacts")
    files_changed: list = Field(
        validation_alias="files_changed_json", serialization_alias="files_changed"
    )
    commands_run: list = Field(
        validation_alias="commands_run_json", serialization_alias="commands_run"
    )
    payload: dict = Field(validation_alias="payload_json", serialization_alias="payload")
    created_at: datetime


class CodingTaskRunResponse(BaseModel):
    """Response for the create-and-run endpoint: the task plus its first result."""

    task: CodingTaskRead
    result: CodingTaskResultRead
