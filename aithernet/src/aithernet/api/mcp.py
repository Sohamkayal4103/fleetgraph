"""GNU Radio MCP endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.mcp.contracts import (
    MCPClientError,
    MCPConfigurationError,
    MCPServerStatus,
    MCPSessionStatus,
    MCPToolInfo,
)
from aithernet.mcp.diagnostics import MCPDiagnostics, build_diagnostics
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.mcp import MCPToolCallBody, MCPToolCallCreate, MCPToolCallRead

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _raise_for_mcp_error(exc: Exception) -> None:
    """Translate MCP domain errors into clean HTTP errors."""
    if isinstance(exc, MCPConfigurationError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if isinstance(exc, MCPClientError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    raise exc


@router.get("/status", response_model=MCPServerStatus)
def mcp_status(runtime: NodeRuntime = Depends(get_runtime)) -> MCPServerStatus:
    """Report the configured MCP server and readiness, without secrets."""
    return runtime.mcp.status()


# -- persistent session lifecycle (Stage 11B) ------------------------------------


@router.get("/session", response_model=MCPSessionStatus)
async def mcp_session(runtime: NodeRuntime = Depends(get_runtime)) -> MCPSessionStatus:
    """Report the managed MCP session status (secret-free). Polling spawns no process."""
    return await runtime.mcp_session_status()


@router.post("/session/start", response_model=MCPSessionStatus)
async def mcp_session_start(runtime: NodeRuntime = Depends(get_runtime)) -> MCPSessionStatus:
    """Start the managed session (idempotent). Returns the status AFTER the transition.

    A start failure is reported in the body (``state=failed`` + sanitized error), not an
    HTTP error — the rest of the API stays up.
    """
    return await runtime.start_mcp_session()


@router.post("/session/restart", response_model=MCPSessionStatus)
async def mcp_session_restart(runtime: NodeRuntime = Depends(get_runtime)) -> MCPSessionStatus:
    """Restart the managed session (new generation, rediscovered tools); awaits completion."""
    return await runtime.restart_mcp_session()


@router.post("/session/stop", response_model=MCPSessionStatus)
async def mcp_session_stop(runtime: NodeRuntime = Depends(get_runtime)) -> MCPSessionStatus:
    """Stop the managed session and clean up the subprocess; awaits completion (idempotent)."""
    return await runtime.stop_mcp_session()


@router.get("/diagnostics", response_model=MCPDiagnostics)
async def mcp_diagnostics(
    probe: bool = Query(
        default=False,
        description="Actually launch the server and run tools/list (default: inspect only).",
    ),
    runtime: NodeRuntime = Depends(get_runtime),
) -> MCPDiagnostics:
    """Return a structured MCP diagnostic report (no secrets).

    With ``probe=false`` (default) only configuration/readiness is inspected — the server
    is never launched. With ``probe=true`` the real MCP path is exercised, capturing tool
    count/names on success or a structured error on failure (always HTTP 200).
    """
    return await build_diagnostics(runtime.mcp, probe=probe)


@router.get("/tools", response_model=list[MCPToolInfo])
async def list_mcp_tools(
    refresh: bool = Query(default=False, description="Re-query the live server (replace cache)."),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MCPToolInfo]:
    """List tools from the managed MCP session (503 unconfigured, 502 failure).

    Uses the persistent session's cached catalog; ``?refresh=true`` re-queries the server.
    """
    try:
        return await runtime.list_mcp_tools(refresh=refresh)
    except (MCPConfigurationError, MCPClientError) as exc:
        _raise_for_mcp_error(exc)


@router.post("/tools/{tool_name}/call", response_model=MCPToolCallRead)
async def call_mcp_tool(
    tool_name: str,
    body: MCPToolCallBody,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MCPToolCallRead:
    """Call an MCP tool and return the persisted, audited call record."""
    payload = MCPToolCallCreate(
        tool_name=tool_name,
        mission_id=body.mission_id,
        task_id=body.task_id,
        caller=body.caller,
        arguments=body.arguments,
    )
    try:
        return await runtime.call_mcp_tool(payload)
    except (MCPConfigurationError, MCPClientError) as exc:
        _raise_for_mcp_error(exc)


@router.get("/tool-calls", response_model=list[MCPToolCallRead])
def list_mcp_tool_calls(
    mission_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MCPToolCallRead]:
    """List persisted MCP tool calls, newest first."""
    return runtime.list_mcp_tool_calls(
        mission_id=mission_id, task_id=task_id, limit=limit, offset=offset
    )


@router.get("/tool-calls/{call_id}", response_model=MCPToolCallRead)
def get_mcp_tool_call(
    call_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MCPToolCallRead:
    """Return a single persisted MCP tool call or 404 if it does not exist."""
    call = runtime.get_mcp_tool_call(call_id)
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool call not found")
    return call
