"""Coding-task endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.coding_agent.contracts import (
    CodingAgentConfigurationError,
    CodingAgentProviderError,
    CodingTaskNotFoundError,
)
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.coding import (
    CodingTaskCreate,
    CodingTaskRead,
    CodingTaskResultRead,
    CodingTaskRunResponse,
)

router = APIRouter(prefix="/coding-tasks", tags=["coding-tasks"])


def _raise_for_coding_error(exc: Exception) -> None:
    """Translate coding-agent domain errors into clean HTTP errors."""
    if isinstance(exc, CodingTaskNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, CodingAgentConfigurationError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    if isinstance(exc, CodingAgentProviderError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    raise exc


@router.post("", response_model=CodingTaskRead, status_code=status.HTTP_201_CREATED)
async def create_coding_task(
    payload: CodingTaskCreate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CodingTaskRead:
    """Create a coding task without running it; emits ``coding_task.created``."""
    return await runtime.create_coding_task(payload)


@router.post("/run", response_model=CodingTaskRunResponse)
async def create_and_run_coding_task(
    payload: CodingTaskCreate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CodingTaskRunResponse:
    """Create a coding task and immediately run it through the coding agent."""
    try:
        task, result = await runtime.create_and_run_coding_task(payload)
        return CodingTaskRunResponse(task=task, result=result)
    except (CodingAgentConfigurationError, CodingAgentProviderError) as exc:
        _raise_for_coding_error(exc)


@router.get("", response_model=list[CodingTaskRead])
def list_coding_tasks(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[CodingTaskRead]:
    """List coding tasks, newest first."""
    return runtime.list_coding_tasks(limit=limit, offset=offset)


@router.get("/{task_id}", response_model=CodingTaskRead)
def get_coding_task(
    task_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CodingTaskRead:
    """Return a single coding task or 404 if it does not exist."""
    task = runtime.get_coding_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coding task not found")
    return task


@router.post("/{task_id}/run", response_model=CodingTaskResultRead)
async def run_coding_task(
    task_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CodingTaskResultRead:
    """Run one coding task and return its result.

    Maps a missing task to 404, an unconfigured coding agent to 503, and an execution
    (provider) failure to 502.
    """
    try:
        return await runtime.run_coding_task(task_id)
    except (
        CodingTaskNotFoundError,
        CodingAgentConfigurationError,
        CodingAgentProviderError,
    ) as exc:
        _raise_for_coding_error(exc)


@router.get("/{task_id}/result", response_model=CodingTaskResultRead)
def get_coding_task_result(
    task_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> CodingTaskResultRead:
    """Return the latest result for a task, or 404 if the task or result is absent."""
    try:
        result = runtime.get_coding_task_result(task_id)
    except CodingTaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No result for this task yet"
        )
    return result
