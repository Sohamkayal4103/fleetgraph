"""Mission endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorDecision,
    CoordinatorProviderError,
    MissionNotFoundError,
)
from aithernet.missions.worker import MissionConflictError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.mission_runs import (
    MissionExecutionRunRead,
    MissionExecutionStatus,
    MissionStartResponse,
    MissionTimeline,
)
from aithernet.schemas.mission_steps import MissionStepRunResponse
from aithernet.schemas.missions import MissionCreate, MissionRead

router = APIRouter(prefix="/missions", tags=["missions"])


@router.post("", response_model=MissionRead, status_code=status.HTTP_201_CREATED)
async def create_mission(
    payload: MissionCreate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionRead:
    """Persist a mission and emit a ``mission.received`` event."""
    return await runtime.create_mission(payload)


@router.get("", response_model=list[MissionRead])
def list_missions(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MissionRead]:
    """List missions, newest first."""
    return runtime.list_missions(limit=limit, offset=offset)


@router.get("/{mission_id}", response_model=MissionRead)
def get_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionRead:
    """Return a single mission or 404 if it does not exist."""
    mission = runtime.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return mission


@router.post("/{mission_id}/process", response_model=CoordinatorDecision)
async def process_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CoordinatorDecision:
    """Run one coordinator reasoning step for a mission and return the decision.

    Maps coordinator failures to clear HTTP errors: a missing mission to 404, an
    unconfigured coordinator to 503, and an upstream provider failure to 502.
    """
    try:
        return await runtime.process_mission_with_coordinator(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CoordinatorConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CoordinatorProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{mission_id}/step", response_model=MissionStepRunResponse)
async def run_mission_step(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionStepRunResponse:
    """Run one coordinator decision and execute its routed step.

    Returns the mission, the coordinator decision, and the persisted step. A blocked or
    failed route is reported in the step (HTTP 200), not as an error. Coordinator-level
    failures map to 404 (missing mission), 503 (unconfigured), or 502 (provider failure).
    """
    try:
        return await runtime.run_mission_step(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CoordinatorConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CoordinatorProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


# -- autonomous execution (Stage 12) ---------------------------------------------


@router.post("/{mission_id}/start", response_model=MissionStartResponse)
async def start_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionStartResponse:
    """Queue a mission for autonomous execution and return AFTER queueing (not completion).

    Idempotent: a mission that already has a non-terminal run returns that run. A terminal
    mission yields a structured 409 conflict.
    """
    try:
        run, created = await runtime.start_mission(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    mission = runtime.get_mission(mission_id)
    if mission is None:  # pragma: no cover - just queued above
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return MissionStartResponse(
        mission=mission,
        run=run,
        queued=True,
        detail="Mission queued for autonomous execution."
        if created
        else "Mission already had an active run; returning it.",
    )


@router.post("/{mission_id}/retry", response_model=MissionStartResponse)
async def retry_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionStartResponse:
    """Retry a blocked/failed mission with a fresh run after the cause was repaired.

    Idempotent: a mission with a live run returns it; a completed/cancelled mission yields a 409.
    Never creates a duplicate mission (beta.9 Defect 4).
    """
    try:
        run, created = await runtime.retry_mission_run(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    mission = runtime.get_mission(mission_id)
    if mission is None:  # pragma: no cover - just requeued above
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return MissionStartResponse(
        mission=mission, run=run, queued=True,
        detail="Mission re-queued (retry)." if created
        else "Mission already had a live run; returning it.",
    )


@router.post("/{mission_id}/pause", response_model=MissionExecutionRunRead)
async def pause_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionRunRead:
    """Request a pause; a running mission pauses at its next atomic boundary."""
    return await _run_op(runtime.pause_mission_run, mission_id)


@router.post("/{mission_id}/resume", response_model=MissionExecutionRunRead)
async def resume_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionRunRead:
    """Resume a paused/waiting mission (requeue for reassessment; no replay)."""
    return await _run_op(runtime.resume_mission_run, mission_id)


@router.post("/{mission_id}/cancel", response_model=MissionExecutionRunRead)
async def cancel_mission(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionRunRead:
    """Request cancellation; a running mission cancels at its next atomic boundary."""
    return await _run_op(runtime.cancel_mission_run, mission_id)


@router.post("/{mission_id}/run-step", response_model=MissionExecutionStatus)
async def run_one_autonomous_step(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionStatus:
    """Run exactly one autonomous iteration (no background loop). Conflict if a worker holds it."""
    try:
        return await runtime.run_one_autonomous_step(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{mission_id}/execution", response_model=MissionExecutionStatus)
def get_mission_execution(
    mission_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionStatus:
    """Return the combined mission + current run + remaining-budget view (no lease token)."""
    execution = runtime.mission_execution_status(mission_id)
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return execution


@router.get("/{mission_id}/timeline", response_model=MissionTimeline)
def get_mission_timeline(
    mission_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionTimeline:
    """Return a chronological mission timeline (events + step summaries)."""
    timeline = runtime.mission_timeline(mission_id, limit=limit)
    if timeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return timeline


async def _run_op(op, mission_id: str) -> MissionExecutionRunRead:
    """Shared error mapping for pause/resume/cancel operations."""
    try:
        return await op(mission_id)
    except MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MissionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
