"""Coding-agent status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aithernet.api.app import get_runtime
from aithernet.coding_agent.contracts import CodingAgentStatus
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/coding-agent", tags=["coding-agent"])


@router.get("/status", response_model=CodingAgentStatus)
def coding_agent_status(runtime: NodeRuntime = Depends(get_runtime)) -> CodingAgentStatus:
    """Report the configured coding-agent provider and readiness, without secrets.

    Reflects GNU Radio MCP as an active node capability when the MCP runtime is configured.
    """
    return runtime.coding_agent_status()
