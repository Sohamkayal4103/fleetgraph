"""Coding-agent domain contracts: execution input/result, capabilities, errors.

These models are the stable boundary between the node runtime and any coding-agent
provider. A provider consumes a :class:`CodingTaskExecutionInput` and returns a
:class:`CodingTaskExecutionResult`; it never touches FastAPI or the database.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from aithernet.schemas.coding import CodingTaskRead
from aithernet.state.models import utcnow

#: Coding-agent capabilities that are real and usable in Stage 3.
ACTIVE_CODING_CAPABILITIES: tuple[str, ...] = (
    "workspace",
    "subprocess_execution",
    "coding_agent_task_execution",
    "event_log",
    "task_store",
)

#: Coding-agent capabilities planned for later stages — advertised as context only and
#: never invoked. In particular, GNU Radio MCP is NOT available to the coding agent yet.
FUTURE_CODING_CAPABILITIES: tuple[str, ...] = (
    "gnuradio_mcp",
    "repo_graph_retrieval",
    "peer_coordination",
    "manager_coordination",
)


# -- errors ----------------------------------------------------------------------


class CodingAgentError(Exception):
    """Base class for all coding-agent failures."""


class CodingAgentConfigurationError(CodingAgentError):
    """Raised when the coding agent is asked to run without valid configuration.

    Surfaced as a clear configuration error rather than pretending work was done.
    """


class CodingAgentProviderError(CodingAgentError):
    """Raised when a provider cannot run (missing executable, timeout, spawn failure)."""


class CodingTaskNotFoundError(Exception):
    """Raised when an operation references a coding-task id that does not exist."""


# -- execution input -------------------------------------------------------------


class CodingTaskExecutionInput(BaseModel):
    """Everything a provider needs to execute a coding task.

    Built by the node runtime from a persisted task via :meth:`from_task`. It carries
    the task contract plus the resolved workspace; it never prescribes a fixed workflow.
    """

    task_id: str
    mission_id: str | None = None
    objective: str
    context: dict = Field(default_factory=dict)
    available_tools: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    reporting_requirements: list[str] = Field(default_factory=list)
    workspace: str
    current_time: datetime

    @classmethod
    def from_task(
        cls,
        task: CodingTaskRead,
        *,
        workspace: str,
        current_time: datetime | None = None,
    ) -> CodingTaskExecutionInput:
        """Assemble an execution input from a persisted task and resolved workspace."""
        return cls(
            task_id=task.id,
            mission_id=task.mission_id,
            objective=task.objective,
            context=task.context,
            available_tools=task.available_tools,
            expected_outputs=task.expected_outputs,
            reporting_requirements=task.reporting_requirements,
            workspace=workspace,
            current_time=current_time or utcnow(),
        )


# -- execution result ------------------------------------------------------------


class CodingTaskExecutionResult(BaseModel):
    """A provider's report of executing a coding task.

    ``status`` is ``"completed"`` when the agent ran successfully and ``"failed"`` when
    it ran but returned a non-zero exit code. Infrastructure failures (missing
    executable, timeout) are raised as :class:`CodingAgentProviderError` instead, so the
    runtime can record them distinctly. Output streams are captured verbatim — providers
    must not fabricate files-changed or commands-run they cannot observe.
    """

    status: str
    summary: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    artifacts: list = Field(default_factory=list)
    files_changed: list = Field(default_factory=list)
    commands_run: list = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)


# -- status ----------------------------------------------------------------------


class CodingAgentStatus(BaseModel):
    """Coding-agent configuration snapshot for ``GET /coding-agent/status``.

    Reports the provider, executable, and workspace, plus whether the agent is ready.
    It deliberately omits the subprocess ``env`` map so secrets are never exposed.
    """

    provider: str
    configured: bool
    executable: str
    workspace: str
    missing_configuration: list[str] = Field(default_factory=list)
    active_capabilities: list[str] = Field(default_factory=list)
    future_capabilities: list[str] = Field(default_factory=list)
