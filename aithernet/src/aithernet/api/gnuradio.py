"""GNU Radio workspace context endpoints (Stage 11B)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from aithernet.api.app import get_runtime
from aithernet.gnuradio.contracts import GNURadioContext, GNURadioContextRefresh
from aithernet.mcp.contracts import MCPClientError, MCPConfigurationError
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/gnuradio", tags=["gnuradio"])


@router.get("/context", response_model=GNURadioContext)
def gnuradio_context(runtime: NodeRuntime = Depends(get_runtime)) -> GNURadioContext:
    """Return the node's current structured GNU Radio context (compact, never fabricated)."""
    return runtime.gnuradio.current()


@router.post("/context/refresh", response_model=GNURadioContext)
async def refresh_gnuradio_context(
    body: GNURadioContextRefresh | None = None,
    runtime: NodeRuntime = Depends(get_runtime),
) -> GNURadioContext:
    """Refresh context via the available read-only tools (no execution/mutation).

    Honestly represents "no active flowgraph". 503 when MCP is unconfigured; 502 on a
    transport failure.
    """
    body = body or GNURadioContextRefresh()
    try:
        return await runtime.gnuradio.refresh(
            mission_id=body.mission_id, mission_step_id=body.mission_step_id
        )
    except MCPConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except MCPClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
