"""External-agent connection endpoints (Stage 7).

These routes let an external agent connect, be listed/inspected, change status, and send
messages that the node persists (optionally creating a mission and running one explicit
mission step). Handlers stay thin: all persistence and event logic lives in the
communication runtime. The node never calls ``endpoint_url`` — it is stored for later.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.communication.contracts import (
    ExternalAgentCreate,
    ExternalAgentDisabledError,
    ExternalAgentMessageCreate,
    ExternalAgentMessageRead,
    ExternalAgentMessageResponse,
    ExternalAgentNotFoundError,
    ExternalAgentRead,
    ExternalAgentStatus,
    ExternalAgentStatusUpdate,
    ExternalAgentStatusValue,
    ExternalAgentValidationError,
)
from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorProviderError,
)
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/agents", tags=["agents"])

#: A second router for the flat ``/agent-messages`` collection (no ``/agents`` prefix).
messages_router = APIRouter(tags=["agents"])


def _require_agent(runtime: NodeRuntime, agent_id: str) -> ExternalAgentRead:
    """Return an agent or raise a clean 404."""
    agent = runtime.get_external_agent(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"External agent {agent_id} not found"
        )
    return agent


@router.post("/connect", response_model=ExternalAgentRead, status_code=status.HTTP_201_CREATED)
async def connect_agent(
    payload: ExternalAgentCreate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentRead:
    """Connect an external agent (status ``connected``) and persist it.

    A raw secret smuggled into ``metadata`` is rejected with 400 by key name only (the
    value is never echoed) — Stage 7 stores no credentials.
    """
    try:
        return await runtime.create_external_agent(payload)
    except ExternalAgentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=list[ExternalAgentRead])
def list_agents(
    status_filter: ExternalAgentStatusValue | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[ExternalAgentRead]:
    """List external agents, newest first, optionally filtered by status."""
    return runtime.list_external_agents(status=status_filter, limit=limit, offset=offset)


@router.get("/status", response_model=ExternalAgentStatus)
def agents_status(runtime: NodeRuntime = Depends(get_runtime)) -> ExternalAgentStatus:
    """Return a roster snapshot: connected count, total count, and the agents."""
    return runtime.external_agent_status()


@router.get("/{agent_id}", response_model=ExternalAgentRead)
def get_agent(
    agent_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentRead:
    """Return a single external agent or 404 if it does not exist."""
    return _require_agent(runtime, agent_id)


@router.patch("/{agent_id}/status", response_model=ExternalAgentRead)
async def set_agent_status(
    agent_id: str,
    payload: ExternalAgentStatusUpdate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentRead:
    """Set an external agent's connection status (connected/disconnected/disabled)."""
    try:
        return await runtime.update_external_agent_status(agent_id, payload.status)
    except ExternalAgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{agent_id}/disconnect", response_model=ExternalAgentRead)
async def disconnect_agent(
    agent_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentRead:
    """Mark an external agent ``disconnected``."""
    try:
        return await runtime.update_external_agent_status(
            agent_id, ExternalAgentStatusValue.DISCONNECTED
        )
    except ExternalAgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{agent_id}/disable", response_model=ExternalAgentRead)
async def disable_agent(
    agent_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentRead:
    """Mark an external agent ``disabled`` (preferred over hard deletion)."""
    try:
        return await runtime.disable_external_agent(agent_id)
    except ExternalAgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{agent_id}/message", response_model=ExternalAgentMessageResponse)
async def post_agent_message(
    agent_id: str,
    payload: ExternalAgentMessageCreate,
    runtime: NodeRuntime = Depends(get_runtime),
) -> ExternalAgentMessageResponse:
    """Record an inbound message; optionally create a mission and run one mission step.

    Errors map cleanly: missing agent → 404; disabled agent or invalid
    run_step/create_mission combination → 400; unconfigured coordinator (when a step is
    requested) → 503; coordinator provider failure during the step → 502.
    """
    try:
        return await runtime.receive_external_agent_message(agent_id, payload)
    except ExternalAgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ExternalAgentDisabledError, ExternalAgentValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CoordinatorConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CoordinatorProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/{agent_id}/messages", response_model=list[ExternalAgentMessageRead])
def list_agent_messages(
    agent_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[ExternalAgentMessageRead]:
    """List messages for one external agent, newest first (404 if the agent is unknown)."""
    _require_agent(runtime, agent_id)
    return runtime.list_external_agent_messages(agent_id=agent_id, limit=limit, offset=offset)


@messages_router.get("/agent-messages", response_model=list[ExternalAgentMessageRead])
def list_all_agent_messages(
    agent_id: str | None = Query(default=None),
    mission_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[ExternalAgentMessageRead]:
    """List external-agent messages across all agents, optionally filtered."""
    return runtime.list_external_agent_messages(
        agent_id=agent_id, mission_id=mission_id, limit=limit, offset=offset
    )
