"""GNU Radio workspace context contracts (Stage 11B)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FlowgraphStatus(str, Enum):
    """Known flowgraph status states (never fabricated; ``unknown`` when unavailable)."""

    UNKNOWN = "unknown"  # not yet inspected / server did not expose it
    NONE = "none"  # no active flowgraph exists
    LOADED = "loaded"  # a flowgraph is present (blocks discovered)
    INVALID = "invalid"  # validation reported problems
    RUNNING = "running"  # execution reported the flowgraph running
    ERROR = "error"  # the last relevant tool call errored


class GNURadioContext(BaseModel):
    """The node's current structured GNU Radio context (compact, never fabricated).

    Large raw tool results are NOT duplicated here — they live in the MCP-call records
    referenced by ``source_mcp_call_ids``. Unavailable fields are ``null``/``unknown``.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    context_id: str = Field(validation_alias="id", serialization_alias="context_id")
    node_id: str
    active_flowgraph_path: str | None = None
    active_flowgraph_name: str | None = None
    active_flowgraph_hash: str | None = None
    flowgraph_status: FlowgraphStatus = FlowgraphStatus.UNKNOWN
    block_summary: dict | None = Field(
        default=None, validation_alias="block_summary_json", serialization_alias="block_summary"
    )
    connection_summary: dict | None = Field(
        default=None,
        validation_alias="connection_summary_json",
        serialization_alias="connection_summary",
    )
    validation_summary: dict | None = Field(
        default=None,
        validation_alias="validation_summary_json",
        serialization_alias="validation_summary",
    )
    latest_error_summary: dict | None = Field(
        default=None,
        validation_alias="latest_error_summary_json",
        serialization_alias="latest_error_summary",
    )
    execution_summary: dict | None = Field(
        default=None,
        validation_alias="execution_summary_json",
        serialization_alias="execution_summary",
    )
    related_mission_id: str | None = None
    related_mission_step_id: str | None = None
    source_mcp_call_ids: list = Field(
        default_factory=list,
        validation_alias="source_mcp_call_ids_json",
        serialization_alias="source_mcp_call_ids",
    )
    source_session_id: str | None = None
    source_session_generation: int | None = None
    stale: bool = True
    stale_reason: str | None = None
    context_version: int = 0
    last_refreshed_at: datetime | None = None
    last_changed_at: datetime | None = None

    def compact(self) -> dict:
        """A small, prompt-safe summary for the coordinator (no large catalogs/raw results)."""
        return {
            "active_flowgraph": self.active_flowgraph_name or self.active_flowgraph_path,
            "flowgraph_status": self.flowgraph_status.value,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "block_count": (self.block_summary or {}).get("count"),
            "connection_count": (self.connection_summary or {}).get("count"),
            "validation": (self.validation_summary or {}).get("summary"),
            "latest_errors": (self.latest_error_summary or {}).get("summary"),
            "execution_status": (self.execution_summary or {}).get("summary"),
            "source_session_generation": self.source_session_generation,
            "last_refreshed_at": (
                self.last_refreshed_at.isoformat() if self.last_refreshed_at else None
            ),
        }


class GNURadioContextRefresh(BaseModel):
    """Body for ``POST /gnuradio/context/refresh`` (optional mission/step linkage)."""

    mission_id: str | None = Field(default=None, description="Optional owning mission.")
    mission_step_id: str | None = Field(default=None, description="Optional owning mission step.")
