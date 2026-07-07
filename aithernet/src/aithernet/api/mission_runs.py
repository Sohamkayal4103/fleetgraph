"""Mission execution-run and worker endpoints (Stage 12).

Top-level read endpoints for execution runs and the autonomous worker manager. All
responses are secret-free — lease tokens are never serialized. These reads never start a
worker, execute a route, or call a model.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.mission_runs import MissionExecutionRunRead, MissionWorkerStatus

router = APIRouter(tags=["mission-execution"])


@router.get("/mission-runs", response_model=list[MissionExecutionRunRead])
def list_mission_runs(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MissionExecutionRunRead]:
    """List execution runs, newest first (no lease tokens)."""
    return runtime.list_mission_runs(limit=limit, offset=offset)


@router.get("/mission-runs/{run_id}", response_model=MissionExecutionRunRead)
def get_mission_run(
    run_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionExecutionRunRead:
    """Return a single execution run or 404 (no lease token)."""
    run = runtime.get_mission_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/mission-worker/status", response_model=MissionWorkerStatus)
def get_mission_worker_status(
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionWorkerStatus:
    """Return the autonomous worker manager status (sanitized)."""
    return runtime.mission_worker_status()
