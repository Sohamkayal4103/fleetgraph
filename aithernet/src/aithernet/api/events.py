"""Event endpoints, including a Server-Sent Events live stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.events import EventRead

router = APIRouter(prefix="/events", tags=["events"])

#: Seconds between SSE keepalive comments when no events are flowing.
_KEEPALIVE_INTERVAL = 15.0


@router.get("", response_model=list[EventRead])
def list_events(
    mission_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    runtime: NodeRuntime = Depends(get_runtime),
) -> list[EventRead]:
    """List events, newest first, optionally filtered by ``mission_id``."""
    return runtime.list_events(mission_id=mission_id, limit=limit, offset=offset)


@router.get("/stream")
async def stream_events(
    request: Request,
    runtime: NodeRuntime = Depends(get_runtime),
) -> StreamingResponse:
    """Stream runtime events as they are published, using Server-Sent Events.

    Only events produced *after* the connection is established are delivered (this is a
    live tail, not a replay of history — use ``GET /events`` for history). The stream
    emits ``: keepalive`` comments during idle periods and terminates when the client
    disconnects.

    Example:
        curl -N http://127.0.0.1:8080/events/stream
    """

    async def event_generator() -> AsyncIterator[str]:
        async with runtime.event_bus.subscribe() as queue:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event['event_type']}\ndata: {json.dumps(event)}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=headers
    )
