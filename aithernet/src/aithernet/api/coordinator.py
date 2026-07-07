"""Coordinator status and diagnostics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aithernet.api.app import get_runtime
from aithernet.coordinator.contracts import CoordinatorStatus
from aithernet.coordinator.diagnostics import (
    CoordinatorDiagnostics,
    build_coordinator_diagnostics,
)
from aithernet.orchestrator.runtime import NodeRuntime

router = APIRouter(prefix="/coordinator", tags=["coordinator"])


@router.get("/status", response_model=CoordinatorStatus)
def coordinator_status(runtime: NodeRuntime = Depends(get_runtime)) -> CoordinatorStatus:
    """Report the configured coordinator provider and readiness, without secrets."""
    return runtime.coordinator.status()


@router.get("/diagnostics", response_model=CoordinatorDiagnostics)
async def coordinator_diagnostics(
    probe: bool = Query(
        default=False,
        description="Make one minimal authenticated inference (default: inspect config only).",
    ),
    runtime: NodeRuntime = Depends(get_runtime),
) -> CoordinatorDiagnostics:
    """Report coordinator readiness; ``?probe=true`` runs one minimal real inference.

    Always HTTP 200 — a failed probe is reported in the body (never an unhandled error), and
    the report never exposes auth files, tokens, environment, or the full prompt.
    """
    return await build_coordinator_diagnostics(runtime.coordinator, probe=probe)
