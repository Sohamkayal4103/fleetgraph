"""Health endpoints — liveness and readiness (Stage 8 + Stage 14A area 5).

``/health`` and ``/health/live`` are cheap liveness probes: they return immediately without
touching the database or any worker, so they stay true while the process responds.
``/health/ready`` reports success ONLY after the required dependencies initialize (schema
migrations, repositories, the mission/transport workers when enabled, and the default RF backend
when required). It returns HTTP 503 while starting/migrating/shutting-down or when a required
component is not ready, so a readiness check never routes work to a half-initialized node.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from aithernet import __version__
from aithernet.api.app import get_runtime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.node import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    """Liveness check — returns immediately without touching the database."""
    return HealthStatus(status="ok", service="aithernet-node", version=__version__)


@router.get("/health/live", response_model=HealthStatus)
def health_live() -> HealthStatus:
    """Liveness probe — the process is alive and the event loop is responsive."""
    return HealthStatus(status="ok", service="aithernet-node", version=__version__)


@router.get("/health/ready")
def health_ready(response: Response, runtime: NodeRuntime = Depends(get_runtime)) -> dict:
    """Readiness probe — 200 only when every required component is initialized; else 503."""
    snapshot = runtime.readiness.snapshot()
    if not snapshot["ready"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if snapshot["ready"] else "not_ready",
        "phase": snapshot["phase"],
        "version": __version__,
        "components": snapshot["components"],
    }
