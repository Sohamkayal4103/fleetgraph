"""Multi-RF-backend API (Stage 13A.5, Part L).

Operator-facing endpoints to inspect and operate RF backends, their live tools, per-backend
contexts, and indexed artifacts. Errors are sanitized and categorized (unknown/disabled/
stopped backend, unavailable executable, missing live tool, invalid arguments, backend call
failure, unsafe artifact path). The legacy ``/mcp/*`` + ``/gnuradio/context`` endpoints are
unchanged and continue targeting the legacy backend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.mcp.contracts import MCPConfigurationError, MCPError, MCPToolInfo
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.rf.contracts import (
    RFBackendDisabledError,
    RFBackendNotFoundError,
    RFBackendNotReadyError,
    RFBackendStatus,
    RFBackendUnavailableError,
    RFCallExecutionContext,
    RFContextUnavailableError,
    RFToolNotFoundError,
)
from aithernet.schemas.mcp import MCPToolCallRead
from aithernet.schemas.rf import RFArtifactRead, RFToolCallRequest
from aithernet.state.repositories import RFArtifactRepository

router = APIRouter(prefix="/rf", tags=["rf"])

#: RF error code -> HTTP status (sanitized + categorized).
_STATUS_BY_CODE = {
    "unknown_backend": status.HTTP_404_NOT_FOUND,
    "backend_disabled": status.HTTP_409_CONFLICT,
    "backend_not_ready": status.HTTP_409_CONFLICT,
    "executable_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "tool_not_found": status.HTTP_404_NOT_FOUND,
    "unsafe_artifact_path": status.HTTP_400_BAD_REQUEST,
    "context_unavailable": status.HTTP_409_CONFLICT,
}


def _raise(exc) -> None:
    code = getattr(exc, "code", "rf_backend_error")
    http_status = _STATUS_BY_CODE.get(code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=http_status, detail=str(exc)) from exc


# -- backends --------------------------------------------------------------------


@router.get("/backends", response_model=list[RFBackendStatus])
async def list_backends(runtime: NodeRuntime = Depends(get_runtime)) -> list[RFBackendStatus]:
    """List every configured RF backend with its (secret-free) status."""
    return await runtime.rf.list_statuses()


@router.get("/backends/{backend_id}", response_model=RFBackendStatus)
async def get_backend(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> RFBackendStatus:
    try:
        return await runtime.rf.get_status(backend_id)
    except RFBackendNotFoundError as exc:
        _raise(exc)


@router.post("/backends/{backend_id}/start", response_model=RFBackendStatus)
async def start_backend(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> RFBackendStatus:
    """Start a backend session. A failure is reported in the returned state, not as a 5xx."""
    try:
        return await runtime.rf.start_backend(backend_id)
    except (RFBackendNotFoundError, RFBackendDisabledError, RFBackendUnavailableError) as exc:
        _raise(exc)


@router.post("/backends/{backend_id}/stop", response_model=RFBackendStatus)
async def stop_backend(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> RFBackendStatus:
    try:
        return await runtime.rf.stop_backend(backend_id)
    except RFBackendNotFoundError as exc:
        _raise(exc)


@router.post("/backends/{backend_id}/restart", response_model=RFBackendStatus)
async def restart_backend(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> RFBackendStatus:
    try:
        return await runtime.rf.restart_backend(backend_id)
    except RFBackendNotFoundError as exc:
        _raise(exc)


# -- tools -----------------------------------------------------------------------


@router.get("/backends/{backend_id}/tools", response_model=list[MCPToolInfo])
async def list_tools(
    backend_id: str,
    refresh: bool = Query(default=False),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MCPToolInfo]:
    try:
        return await runtime.rf.list_tools(backend_id, refresh=refresh)
    except (RFBackendNotFoundError, RFBackendDisabledError) as exc:
        _raise(exc)
    except MCPConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except MCPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/backends/{backend_id}/tools/refresh", response_model=list[MCPToolInfo])
async def refresh_tools(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> list[MCPToolInfo]:
    try:
        return await runtime.rf.list_tools(backend_id, refresh=True)
    except (RFBackendNotFoundError, RFBackendDisabledError) as exc:
        _raise(exc)
    except MCPConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except MCPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/backends/{backend_id}/call", response_model=MCPToolCallRead)
async def call_tool(
    backend_id: str,
    body: RFToolCallRequest,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MCPToolCallRead:
    """Execute exactly one MCP tool call on a backend and return the audited call record."""
    ctx = RFCallExecutionContext(
        caller=body.caller, mission_id=body.mission_id,
        mission_step_id=body.mission_step_id, task_id=body.task_id,
    )
    try:
        return await runtime.rf.call_tool(
            backend_id, body.tool_name, body.arguments, execution_context=ctx
        )
    except (
        RFBackendNotFoundError, RFBackendDisabledError, RFBackendNotReadyError,
        RFBackendUnavailableError, RFToolNotFoundError,
    ) as exc:
        _raise(exc)
    except MCPConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except MCPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# -- contexts --------------------------------------------------------------------


@router.get("/contexts")
def list_contexts(runtime: NodeRuntime = Depends(get_runtime)) -> list[dict]:
    return runtime.rf.list_contexts()


@router.get("/contexts/{backend_id}")
def get_context(backend_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.rf.context(backend_id)
    except RFBackendNotFoundError as exc:
        _raise(exc)


@router.post("/contexts/{backend_id}/refresh")
async def refresh_context(
    backend_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> dict:
    """Refresh a backend's RF context by calling ONLY its discovered read-only tools."""
    try:
        return await runtime.rf.refresh_context(backend_id)
    except (
        RFBackendNotFoundError, RFBackendDisabledError, RFBackendNotReadyError,
        RFContextUnavailableError,
    ) as exc:
        _raise(exc)
    except MCPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# -- artifacts (metadata only; never serves raw bytes) ---------------------------


@router.get("/artifacts", response_model=list[RFArtifactRead])
def list_artifacts(
    backend_id: str | None = Query(default=None),
    mission_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[RFArtifactRead]:
    with runtime.session_scope() as session:
        rows = RFArtifactRepository(session).list(
            backend_id=backend_id, mission_id=mission_id, limit=limit, offset=offset
        )
        return [RFArtifactRead.model_validate(row) for row in rows]


@router.get("/artifacts/{artifact_id}", response_model=RFArtifactRead)
def get_artifact(artifact_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> RFArtifactRead:
    with runtime.session_scope() as session:
        row = RFArtifactRepository(session).get(artifact_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        return RFArtifactRead.model_validate(row)


# -- benchmarks (Part Q; operator-run evidence, never changes the default backend) -----


@router.get("/benchmarks/scenarios")
def list_benchmark_scenarios() -> list[dict]:
    from aithernet.rf.benchmark import list_scenarios

    return list_scenarios()


@router.get("/benchmarks")
def list_benchmarks(
    limit: int = Query(default=100, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    return runtime.rf_benchmark.list(limit=limit)


@router.get("/benchmarks/{run_id}")
def get_benchmark(run_id: str, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    record = runtime.rf_benchmark.get(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Benchmark not found")
    return record


@router.post("/benchmarks/run")
async def run_benchmark(
    scenario_id: str = Query(...),
    backend_id: str | None = Query(default=None, description="A backend id, or omit for all."),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[dict]:
    """Run a scenario against one backend (or every backend that can express it)."""
    try:
        if backend_id:
            return [await runtime.rf_benchmark.run(scenario_id, backend_id)]
        return await runtime.rf_benchmark.run_all(scenario_id)
    except Exception as exc:
        code = getattr(exc, "code", "benchmark_error")
        http_status = (
            status.HTTP_404_NOT_FOUND if code in ("unknown_scenario", "scenario_not_supported")
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=http_status, detail=str(exc)) from exc
