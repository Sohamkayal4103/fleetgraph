"""Node status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.node import NodeStatus

router = APIRouter(prefix="/node", tags=["node"])


@router.get("/status", response_model=NodeStatus)
def node_status(runtime: NodeRuntime = Depends(get_runtime)) -> NodeStatus:
    """Return an operational snapshot of the node, including live counts."""
    return runtime.status()
