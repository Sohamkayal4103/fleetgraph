"""Stage 13C operator-dashboard aggregate endpoints (Part O).

Read-only, bounded, sanitized aggregate views that let the dashboard render the whole
distributed picture with a few requests instead of many. Every handler is read-only: it never
sends a message, never resumes a mission, never mutates state. Responses never contain private
keys, signatures, raw envelopes, auth headers, prompts, environment, or unbounded payloads.
These endpoints are additive — they do not replace the existing Stage 13A/13B APIs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aithernet.api.app import get_runtime
from aithernet.dashboard import DashboardService
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.dashboard import (
    CommunicationsSummaryResponse,
    ConversationTimelineResponse,
    DistributedMissionTimelineResponse,
    FleetResponse,
    OverviewResponse,
)

router = APIRouter(tags=["dashboard"])


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(runtime: NodeRuntime = Depends(get_runtime)) -> OverviewResponse:
    """Node identity + runtime/worker/coordinator/RF/comms health rollup (Part C)."""
    data = await DashboardService(runtime).overview()
    return OverviewResponse.model_validate(data)


@router.get("/fleet", response_model=FleetResponse)
def get_fleet(
    runtime: NodeRuntime = Depends(get_runtime),
    limit: int = Query(default=200, ge=1, le=1000),
) -> FleetResponse:
    """Per-peer fleet aggregate: trust, permissions, health, manifest, linkage (Part D)."""
    return FleetResponse.model_validate(DashboardService(runtime).fleet(limit=limit))


@router.get(
    "/conversations/{conversation_id}/timeline",
    response_model=ConversationTimelineResponse,
)
def get_conversation_timeline(
    conversation_id: str,
    runtime: NodeRuntime = Depends(get_runtime),
    limit: int = Query(default=200, ge=1, le=1000),
) -> ConversationTimelineResponse:
    """Ordered conversation timeline with bounded content + ACK/reply legend (Parts E/F)."""
    data = DashboardService(runtime).conversation_timeline(conversation_id, limit=limit)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return ConversationTimelineResponse.model_validate(data)


@router.get(
    "/missions/{mission_id}/distributed-timeline",
    response_model=DistributedMissionTimelineResponse,
)
def get_distributed_mission_timeline(
    mission_id: str, runtime: NodeRuntime = Depends(get_runtime)
) -> DistributedMissionTimelineResponse:
    """Distributed correlation timeline + persisted-only topology for one mission (Part H)."""
    data = DashboardService(runtime).distributed_mission_timeline(mission_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
    return DistributedMissionTimelineResponse.model_validate(data)


@router.get("/reply-waits")
def list_reply_waits(
    runtime: NodeRuntime = Depends(get_runtime),
    state: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict]:
    """All reply waits across missions, filterable by state (Part I)."""
    return DashboardService(runtime).reply_waits(state=state, limit=limit)


@router.get("/communications/summary", response_model=CommunicationsSummaryResponse)
def get_communications_summary(
    runtime: NodeRuntime = Depends(get_runtime),
) -> CommunicationsSummaryResponse:
    """Fleet-wide communication rollup: outbox/inbox/waits/inbound + recent failures (Part K)."""
    return CommunicationsSummaryResponse.model_validate(
        DashboardService(runtime).communications_summary()
    )
