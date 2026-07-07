"""The GNU Radio context service: the ONE place GNU Radio tool semantics are interpreted.

It updates compact context summaries only from successful, parsed tool results, and only
recognizes a known tool's semantics after confirming that tool is present in the live,
dynamically discovered catalog. It never fabricates flowgraph state, never executes or
mutates a flowgraph during a refresh, and represents "no active flowgraph" honestly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aithernet.gnuradio.contracts import FlowgraphStatus, GNURadioContext
from aithernet.gnuradio.repositories import GNURadioContextRepository
from aithernet.mcp.contracts import MCPError
from aithernet.schemas.mcp import MCPToolCallCreate, MCPToolCallRead
from aithernet.state.models import utcnow

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

# -- context lifecycle event types (sanitized) -----------------------------------
EVENT_CONTEXT_REFRESHING = "gnuradio.context.refreshing"
EVENT_CONTEXT_REFRESHED = "gnuradio.context.refreshed"
EVENT_CONTEXT_FAILED = "gnuradio.context.failed"

#: Known gr-mcp read-only context tools -> the context field they populate. These are only
#: ACTED ON when the tool is present in the live discovered catalog (runtime-checked
#: semantics, never inferred from the name alone).
_READ_TOOLS: dict[str, str] = {
    "get_blocks": "block_summary_json",
    "get_connections": "connection_summary_json",
    "validate_flowgraph": "validation_summary_json",
    "get_all_errors": "latest_error_summary_json",
}

#: Known mutating tools — a successful one marks derived context stale until refreshed.
_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "make_block",
        "remove_block",
        "set_block_params",
        "connect_blocks",
        "disconnect_blocks",
        "clear_flowgraph",
        "load_flowgraph",
    }
)
_SAVE_TOOLS: frozenset[str] = frozenset({"save_flowgraph"})
_EXECUTE_TOOLS: frozenset[str] = frozenset({"execute_flowgraph"})

#: The read-only tools used by a refresh, in order.
_REFRESH_TOOLS: tuple[str, ...] = (
    "get_blocks",
    "get_connections",
    "validate_flowgraph",
    "get_all_errors",
)

_PREVIEW = 600


def _extract_text(result: dict) -> str:
    """Join the text content blocks of an MCP tool result (bounded by the caller)."""
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        joined = "\n".join(p for p in parts if p).strip()
        if joined:
            return joined
    return json.dumps(result, default=str)


def _maybe_count(text: str) -> int | None:
    """Best-effort element count from a tool result text; ``None`` if not determinable."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("blocks", "connections", "errors", "items", "results"):
            value = parsed.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _summary(call_id: str, text: str, *, count_keyed: bool = False) -> dict:
    summary = {"available": True, "source_call_id": call_id, "preview": text[:_PREVIEW]}
    if count_keyed:
        summary["count"] = _maybe_count(text)
    summary["summary"] = text[:200]
    return summary


class GNURadioContextService:
    """Owns interpretation and persistence of the node's GNU Radio workspace context."""

    def __init__(self, node: NodeRuntime) -> None:
        self._node = node

    # -- read --------------------------------------------------------------------

    def current(self) -> GNURadioContext:
        """Load the node's current context (a fresh, honest default if none persisted)."""
        node_id = self._node.config.node_id
        with self._node.session_scope() as session:
            record = GNURadioContextRepository(session).get(node_id)
            if record is None:
                return GNURadioContext(context_id="-", node_id=node_id)
            return GNURadioContext.model_validate(record)

    # -- write helpers -----------------------------------------------------------

    def _persist(self, fields: dict) -> GNURadioContext:
        node_id = self._node.config.node_id
        with self._node.session_scope() as session:
            record = GNURadioContextRepository(session).update(node_id, fields=fields)
            session.commit()
            return GNURadioContext.model_validate(record)

    def mark_stale(self, reason: str) -> None:
        """Mark the derived context stale (e.g. after a mutation or a session restart)."""
        self._persist({"stale": True, "stale_reason": reason})

    # -- interpretation of a single tool result ----------------------------------

    async def apply_tool_call(self, call: MCPToolCallRead) -> None:
        """Update context from ONE persisted tool call (only on success, only known tools).

        Recognizes a tool's semantics only if it is in the live discovered catalog. A failed
        tool call never updates context as success.
        """
        if self._node.mcp.session is None:
            return
        available = self._node.mcp.session.cached_tool_names()
        tool = call.tool_name
        if tool not in available:
            return  # not a live tool — do not infer semantics from the name
        if call.status.value != "completed":
            return  # failed tool call must not update context as success

        if tool in _READ_TOOLS:
            field = _READ_TOOLS[tool]
            text = _extract_text(call.result)
            count_keyed = tool in ("get_blocks", "get_connections", "get_all_errors")
            self._persist(
                {
                    field: _summary(call.id, text, count_keyed=count_keyed),
                    "source_session_id": call.session_id,
                    "source_session_generation": call.session_generation,
                    "related_mission_id": call.mission_id,
                    "related_mission_step_id": call.mission_step_id,
                }
            )
        elif tool in _MUTATION_TOOLS:
            self.mark_stale(f"mutated by {tool}")
        elif tool in _SAVE_TOOLS:
            path = self._extract_path(call.result)
            fields: dict = {"related_mission_id": call.mission_id}
            if path:
                fields["active_flowgraph_path"] = path
            self._persist(fields)
        elif tool in _EXECUTE_TOOLS:
            text = _extract_text(call.result)
            self._persist(
                {
                    "execution_summary_json": _summary(call.id, text),
                    "flowgraph_status": FlowgraphStatus.RUNNING.value,
                }
            )

    @staticmethod
    def _extract_path(result: dict) -> str | None:
        """Pull a flowgraph path from a save result only if it actually contains one."""
        text = _extract_text(result)
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            for key in ("path", "filepath", "file", "saved_to"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    # -- refresh -----------------------------------------------------------------

    async def refresh(
        self, *, mission_id: str | None = None, mission_step_id: str | None = None
    ) -> GNURadioContext:
        """Refresh context by calling the available read-only tools (no execution/mutation).

        Inspects the live catalog, calls only the read-only context tools that are actually
        available, links the generated MCP calls to the optional mission/step, and updates
        only the fields it could read. Honestly represents "no active flowgraph".
        """
        node_id = self._node.config.node_id
        await self._node._emit_event(
            event_type=EVENT_CONTEXT_REFRESHING,
            mission_id=mission_id,
            source="gnuradio",
            message="GNU Radio context refresh started.",
            payload={"node_id": node_id},
        )
        try:
            tools = await self._node.list_mcp_tools()
        except MCPError as exc:
            await self._node._emit_event(
                event_type=EVENT_CONTEXT_FAILED,
                mission_id=mission_id,
                source="gnuradio",
                message=f"GNU Radio context refresh failed: {exc}",
                payload={"node_id": node_id, "error_type": type(exc).__name__},
            )
            raise

        available = {tool.name for tool in tools}
        session_status = await self._node.mcp.session_status()
        fields: dict = {
            "source_session_id": (
                session_status.session_id if session_status.session_id != "-" else None
            ),
            "source_session_generation": session_status.generation or None,
            "source_mcp_call_ids_json": [],
            "related_mission_id": mission_id,
            "related_mission_step_id": mission_step_id,
            "stale": False,
            "stale_reason": None,
            "last_refreshed_at": utcnow(),
        }
        call_ids: list[str] = []
        block_count: int | None = None
        any_read = False
        errored = False

        for tool in _REFRESH_TOOLS:
            field = _READ_TOOLS[tool]
            if tool not in available:
                fields[field] = {"available": False, "reason": "tool not in live catalog"}
                continue
            any_read = True
            call = await self._call_read_tool(tool, mission_id, mission_step_id)
            if call is None or call.status.value != "completed":
                err = (call.error or "call failed")[:200] if call is not None else "call failed"
                fields[field] = {"available": True, "called": True, "ok": False, "error": err}
                if call is not None:
                    call_ids.append(call.id)
                errored = True
                continue
            call_ids.append(call.id)
            text = _extract_text(call.result)
            count_keyed = tool in ("get_blocks", "get_connections", "get_all_errors")
            summary = _summary(call.id, text, count_keyed=count_keyed)
            fields[field] = summary
            if tool == "get_blocks":
                block_count = summary.get("count")

        fields["source_mcp_call_ids_json"] = call_ids
        fields["flowgraph_status"] = self._derive_status(
            any_read=any_read, errored=errored, block_count=block_count
        )
        context = self._persist(fields)

        await self._node._emit_event(
            event_type=EVENT_CONTEXT_REFRESHED,
            mission_id=mission_id,
            source="gnuradio",
            message=(
                f"GNU Radio context refreshed (status={context.flowgraph_status.value}, "
                f"{len(call_ids)} call(s), generation {context.source_session_generation})."
            ),
            payload={
                "node_id": node_id,
                "flowgraph_status": context.flowgraph_status.value,
                "source_session_generation": context.source_session_generation,
                "source_mcp_call_ids": call_ids,
            },
        )
        return context

    async def _call_read_tool(
        self, tool: str, mission_id: str | None, step_id: str | None
    ) -> MCPToolCallRead | None:
        """Call a read-only context tool through the managed session, linked + persisted."""
        try:
            return await self._node.call_mcp_tool(
                MCPToolCallCreate(
                    tool_name=tool,
                    arguments={},
                    caller="gnuradio_context",
                    mission_id=mission_id,
                    mission_step_id=step_id,
                )
            )
        except MCPError:
            return None  # transport/config failure for this tool; recorded by call_mcp_tool

    @staticmethod
    def _derive_status(*, any_read: bool, errored: bool, block_count: int | None) -> str:
        if not any_read:
            return FlowgraphStatus.UNKNOWN.value
        if block_count is not None:
            return FlowgraphStatus.LOADED.value if block_count > 0 else FlowgraphStatus.NONE.value
        if errored:
            return FlowgraphStatus.ERROR.value
        return FlowgraphStatus.UNKNOWN.value
