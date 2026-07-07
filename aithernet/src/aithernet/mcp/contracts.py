"""MCP domain contracts: tool info, call results, capabilities, status, and errors.

These models are the stable boundary between the node runtime and any MCP client. A
client lists tools and calls them; it never touches FastAPI or the database. Tool names
are discovered at runtime via ``tools/list`` and are never hardcoded into node behavior.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

#: MCP capabilities that are real and usable in Stage 4.
ACTIVE_MCP_CAPABILITIES: tuple[str, ...] = (
    "mcp_client",
    "mcp_tool_listing",
    "mcp_tool_calling",
    "mcp_tool_call_store",
    "event_log",
)

#: MCP capabilities planned for later stages — advertised as context only, never invoked.
FUTURE_MCP_CAPABILITIES: tuple[str, ...] = (
    "long_lived_mcp_sessions",
    "gnuradio_flowgraph_summary",
    "gnuradio_domain_tools",
    "coordinator_direct_tool_routing",
    "coding_agent_direct_tool_routing",
)


# -- errors ----------------------------------------------------------------------


class MCPError(Exception):
    """Base class for all MCP-related failures."""


class MCPConfigurationError(MCPError):
    """Raised when an MCP operation is attempted without valid configuration."""


class MCPClientError(MCPError):
    """Raised on MCP transport/protocol failures (spawn, JSON-RPC, timeout)."""


class MCPToolCallNotFoundError(Exception):
    """Raised when an operation references a tool-call id that does not exist."""


class MCPSchemaValidationError(MCPError):
    """beta.10 Defect 5: a proposed tool call failed authoritative-schema preflight.

    Carries a normalized, coordinator-facing ``correction`` so the model can re-issue a valid
    call. This is distinct from a runtime MCP tool failure (the schema rejection happens BEFORE
    any server dispatch), so the action record can tell a model-generated invalid call apart from
    an adapter mapping error or a server-side runtime failure."""

    def __init__(self, correction: str, *, tool_name: str = "", schema_digest: str = "") -> None:
        super().__init__(correction)
        self.correction = correction
        self.tool_name = tool_name
        self.schema_digest = schema_digest


# -- tool info -------------------------------------------------------------------


class MCPToolInfo(BaseModel):
    """A tool advertised by an MCP server (from ``tools/list``)."""

    name: str
    description: str | None = None
    input_schema: dict | None = None


# -- call result -----------------------------------------------------------------


class MCPToolCallResult(BaseModel):
    """A client's report of invoking a tool.

    ``status`` is ``"completed"`` for a successful round trip and ``"failed"`` when the
    tool itself reported an error (``isError``). Transport/protocol failures are raised as
    :class:`MCPClientError` instead. ``call_id`` is filled by the runtime after the call
    is persisted; the client leaves it ``None``.
    """

    call_id: str | None = None
    tool_name: str
    status: str
    result: dict = Field(default_factory=dict)
    error: str | None = None


# -- status ----------------------------------------------------------------------


class MCPServerStatus(BaseModel):
    """MCP server configuration snapshot for ``GET /mcp/status``.

    Reports the provider, a launch-command summary, and readiness — but never the
    subprocess ``env`` map, so secrets are not exposed. ``available_tools`` is left unset
    here (listing tools launches the server); use ``GET /mcp/tools`` to enumerate them.
    """

    provider: str
    configured: bool
    command: str | None = None
    server_name: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)
    active_capabilities: list[str] = Field(default_factory=list)
    available_tools: list[str] | None = None
    future_capabilities: list[str] = Field(default_factory=list)


# -- persistent session ----------------------------------------------------------


class MCPSessionState(str, Enum):
    """Lifecycle states of the persistent, long-lived MCP session.

    ``stopped``    — no subprocess running (initial / after a clean stop).
    ``starting``   — launching + initialize + initial tool discovery in progress.
    ``ready``      — process up, initialized, tools discovered; serving calls.
    ``degraded``   — the process died unexpectedly; not serving until recovered.
    ``restarting`` — a stop+start cycle is in progress (generation will increment).
    ``stopping``   — a clean shutdown is in progress.
    ``failed``     — start/recovery failed (e.g. unconfigured, spawn error).
    """

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    FAILED = "failed"


class MCPSessionStatus(BaseModel):
    """Secret-free status of the managed MCP session for ``GET /mcp/session``.

    Never includes the subprocess environment, secrets in arguments (the ``command``
    summary is redacted), authentication material, or raw tool results.

    ``session_id`` identifies the managed session object for the current node runtime
    (stable for the runtime's lifetime). ``generation`` counts how many MCP subprocesses
    have successfully reached the ``ready`` state in this session's lifetime — it
    increments by one each time a new process is launched, initialized, and its tools
    rediscovered (a *failed* start does not increment it).
    """

    session_id: str
    node_id: str | None = None
    provider: str
    state: MCPSessionState
    configured: bool
    command: str | None = None
    executable_resolves: bool | None = None
    working_directory: str | None = None
    pid: int | None = None
    generation: int = 0
    restart_count: int = 0
    started_at: datetime | None = None
    ready_at: datetime | None = None
    stopped_at: datetime | None = None
    last_activity_at: datetime | None = None
    uptime_seconds: float | None = None
    tool_count: int | None = None
    tool_catalog_generation: int | None = None
    active_requests: int = 0
    max_in_flight_requests: int = 1
    last_error_type: str | None = None
    last_error: str | None = None
    autostart: bool = True
    auto_restart: bool = True
    missing_configuration: list[str] = Field(default_factory=list)
