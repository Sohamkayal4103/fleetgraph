"""Pydantic request/response schemas for the node API and CLI."""

from aithernet.schemas.coding import (
    CodingTaskCreate,
    CodingTaskRead,
    CodingTaskResultRead,
    CodingTaskRunResponse,
    CodingTaskStatus,
)
from aithernet.schemas.events import EventRead
from aithernet.schemas.mcp import (
    MCPToolCallBody,
    MCPToolCallCreate,
    MCPToolCallRead,
    MCPToolCallStatus,
)
from aithernet.schemas.missions import MissionCreate, MissionRead, MissionStatus
from aithernet.schemas.node import HealthStatus, NodeStatus

# NOTE: ``mission_steps`` is intentionally NOT re-exported here. It depends on
# ``coordinator.contracts`` (which in turn imports the schemas above), so importing it
# from this package ``__init__`` could re-enter a partially-initialized module. Import it
# directly from ``aithernet.schemas.mission_steps`` instead.

__all__ = [
    "MissionCreate",
    "MissionRead",
    "MissionStatus",
    "EventRead",
    "NodeStatus",
    "HealthStatus",
    "CodingTaskCreate",
    "CodingTaskRead",
    "CodingTaskResultRead",
    "CodingTaskRunResponse",
    "CodingTaskStatus",
    "MCPToolCallBody",
    "MCPToolCallCreate",
    "MCPToolCallRead",
    "MCPToolCallStatus",
]
