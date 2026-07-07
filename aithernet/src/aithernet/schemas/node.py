"""Node health and status schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HealthStatus(BaseModel):
    """Lightweight liveness response for ``GET /health``."""

    status: str
    service: str
    version: str


class NodeStatus(BaseModel):
    """Operational snapshot of the node for ``GET /node/status``."""

    node_id: str
    node_name: str
    runtime_status: str
    database_path: str
    started_at: datetime
    version: str
    mission_count: int
    event_count: int
