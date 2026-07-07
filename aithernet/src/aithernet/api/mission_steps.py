"""Mission-step query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.mission_steps import MissionStepRead

router = APIRouter(prefix="/mission-steps", tags=["mission-steps"])


@router.get("", response_model=list[MissionStepRead])
def list_mission_steps(
    mission_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[MissionStepRead]:
    """List persisted mission steps, newest first, optionally filtered by mission."""
    return runtime.list_mission_steps(mission_id=mission_id, limit=limit, offset=offset)


@router.get("/{step_id}", response_model=MissionStepRead)
def get_mission_step(
    step_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> MissionStepRead:
    """Return a single mission step or 404 if it does not exist."""
    step = runtime.get_mission_step(step_id)
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission step not found")
    return step
