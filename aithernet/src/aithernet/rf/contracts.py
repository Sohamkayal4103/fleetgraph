"""Generic RF-backend contracts (Stage 13A.5, Part B).

These model a *generic* RF backend reached over an MCP stdio subprocess, independent of the
specific backend (legacy GNU Radio, Marconi, or future ones). Backend ids are stable
identifiers (``legacy_gr_mcp``, ``marconi``) — never filesystem paths. Status never includes
subprocess environments, secrets, or private paths beyond the configured public fields.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class RFBackendState(str, Enum):
    """Lifecycle state of one RF backend (a superset view over the MCP session state)."""

    DISABLED = "disabled"
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"


#: Backend kinds.
BACKEND_KIND_MCP_STDIO = "mcp_stdio"


class RFBackendStatus(BaseModel):
    """Secret-free status snapshot for one RF backend."""

    backend_id: str
    display_name: str
    backend_kind: str
    experimental: bool
    enabled: bool
    default: bool
    state: RFBackendState
    provider: str
    command: str | None = None
    cwd: str | None = None
    version: str | None = None
    source_revision: str | None = None
    session_id: str | None = None
    session_generation: int | None = None
    pid: int | None = None
    tool_count: int = 0
    active_requests: int = 0
    max_in_flight_requests: int = 1
    workspace: str | None = None
    last_activity_at: datetime | None = None
    last_error_type: str | None = None
    last_error: str | None = None


class RFToolInfo(BaseModel):
    """A tool advertised by a backend's live catalog (name + description; schema optional)."""

    backend_id: str
    name: str
    description: str | None = None
    input_schema: dict | None = None


# -- errors ----------------------------------------------------------------------


class RFBackendError(Exception):
    """Base class for RF-backend errors. ``code`` is a stable machine string."""

    code: str = "rf_backend_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class RFBackendNotFoundError(RFBackendError):
    code = "unknown_backend"


class RFBackendDisabledError(RFBackendError):
    code = "backend_disabled"


class RFBackendNotReadyError(RFBackendError):
    code = "backend_not_ready"


class RFBackendUnavailableError(RFBackendError):
    code = "executable_unavailable"


class RFToolNotFoundError(RFBackendError):
    code = "tool_not_found"


class RFArtifactError(RFBackendError):
    code = "unsafe_artifact_path"


class RFContextUnavailableError(RFBackendError):
    code = "context_unavailable"


class RFCallExecutionContext(BaseModel):
    """Mission/run/step linkage + caller attached to an RF tool call by Aithernet."""

    caller: str = "user"
    mission_id: str | None = None
    mission_run_id: str | None = None
    mission_step_id: str | None = None
    task_id: str | None = None
