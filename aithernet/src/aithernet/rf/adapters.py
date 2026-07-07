"""Backend semantic adapters (Stage 13A.5, Part I).

An adapter recognizes backend-specific tool semantics ONLY when (1) the backend is live,
(2) the tool is in the dynamically discovered catalog, (3) the call completed successfully,
and (4) the returned result actually contains the claimed information. Success is never
inferred from a tool name alone. A mutating operation whose final state is unknown marks the
affected derived context stale rather than fabricating it.

  * ``LegacyGrMcpSemanticAdapter`` delegates to the existing Stage 11B GNU Radio context
    service (no duplication, identical behavior).
  * ``MarconiSemanticAdapter`` interprets the dynamically discovered Marconi tool results
    (devices/captures/signals/measurements/scenes/pipelines/runs/plots/exports) and indexes
    workspace-contained artifacts.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from aithernet.rf import events as ev
from aithernet.rf.artifacts import classify, safe_relative_path, stat_and_hash
from aithernet.rf.contracts import RFArtifactError
from aithernet.schemas.mcp import MCPToolCallRead
from aithernet.state.repositories import RFArtifactRepository

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime
    from aithernet.rf.context import RFWorkspaceContextService


class RFSemanticAdapter:
    """Base adapter: by default it records nothing (a backend with unknown semantics)."""

    backend_id: str = "generic"

    def refresh_tools(self, discovered: set[str]) -> list[str]:
        """Discovered read-only tools (no required args) to call during a context refresh."""
        return []

    async def on_call(self, call: MCPToolCallRead, *, meta: dict) -> str | None:
        """Interpret one persisted call; return a result_summary_type. No-op by default."""
        return None


class LegacyGrMcpSemanticAdapter(RFSemanticAdapter):
    """Delegates to the existing GNU Radio context service (Stage 11B), unchanged."""

    backend_id = "legacy_gr_mcp"
    _REFRESH = ("get_blocks", "get_connections", "validate_flowgraph", "get_all_errors")

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime

    def refresh_tools(self, discovered: set[str]) -> list[str]:
        return [name for name in self._REFRESH if name in discovered]

    async def on_call(self, call: MCPToolCallRead, *, meta: dict) -> str | None:
        # The GNU Radio context service interprets recognized tools (only on success) exactly
        # as in Stage 11B. The 'gnuradio_context' caller updates context itself, so skip it.
        if call.caller != "gnuradio_context":
            with contextlib.suppress(Exception):
                await self.runtime.gnuradio.apply_tool_call(call)
        return "gnuradio_flowgraph"


# -- Marconi ---------------------------------------------------------------------

#: Marconi read-only tools with no required arguments (safe for a context refresh).
_MARCONI_REFRESH = ("list_devices", "list_runs", "list_blocks")

#: Keys whose string values may be workspace artifact paths.
_PATH_KEYS = frozenset(
    {"path", "plot_path", "image_path", "output", "output_path", "grc_path", "file", "filepath"}
)


def marconi_payload(result: dict) -> object:
    """Extract Marconi's actual payload from a FastMCP-wrapped tool result."""
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                with contextlib.suppress(Exception):
                    return json.loads(text)
                return text
    return None


def _collect_paths(payload: object, out: list[str], depth: int = 0) -> None:
    """Recursively collect candidate artifact path strings from a payload (bounded depth)."""
    if depth > 4 or len(out) > 50:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _PATH_KEYS and isinstance(value, str) and value.strip():
                out.append(value)
            elif key in ("files", "artifacts", "plots") and isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, dict):
                        _collect_paths(item, out, depth + 1)
            else:
                _collect_paths(value, out, depth + 1)
    elif isinstance(payload, list):
        for item in payload[:50]:
            _collect_paths(item, out, depth + 1)


class MarconiSemanticAdapter(RFSemanticAdapter):
    """Interprets dynamically discovered Marconi tool results into RF workspace context."""

    backend_id = "marconi"

    #: tool -> the context summary field it populates on success (when the result has data).
    _SUMMARY_FIELD = {
        "list_devices": "devices_summary_json",
        "list_runs": "recent_runs_summary_json",
        "find_signals": "signals_summary_json",
        "detect_bursts": "signals_summary_json",
        "measure": "measurements_summary_json",
        "validate_pipeline": "pipeline_validation_summary_json",
        "save_pipeline": "active_pipeline_summary_json",
        "export_grc": "exported_flowgraphs_summary_json",
        "psd": "captures_summary_json",
    }
    #: Mutating tools whose final state is not fully known from the result -> mark stale.
    _MUTATING = frozenset(
        {"simulate_scene", "capture", "load_capture", "render_scene", "run_pipeline",
         "transmit_capture"}
    )

    def __init__(self, runtime: NodeRuntime, *, workspace: str | None,
                 context: RFWorkspaceContextService) -> None:
        self.runtime = runtime
        self.workspace = workspace
        self.context = context
        self.node_id = runtime.config.node_id

    def refresh_tools(self, discovered: set[str]) -> list[str]:
        return [name for name in _MARCONI_REFRESH if name in discovered]

    async def on_call(self, call: MCPToolCallRead, *, meta: dict) -> str | None:
        tool = call.tool_name
        succeeded = call.status.value == "completed" if hasattr(call.status, "value") else (
            call.status == "completed"
        )
        if not succeeded:
            # Record the failure honestly; never update success-derived summaries.
            self.context.apply_fields(
                self.backend_id,
                fields={"latest_errors_json": {"tool": tool, "error": (call.error or "")[:300]}},
                call_id=call.id,
                **self._meta_kwargs(meta),
            )
            return "error"

        payload = marconi_payload(call.result or {})
        fields: dict = {}
        summary_type = "marconi_result"

        # Only record a summary when the result actually carries the claimed data.
        field = self._SUMMARY_FIELD.get(tool)
        if field is not None and payload is not None:
            fields[field] = self._summarize(tool, payload)
            summary_type = tool

        # Index any workspace-contained artifact paths the result actually reported.
        await self._index_artifacts(call, payload, meta)

        # A mutating op whose final state is not fully known marks derived context stale.
        if tool in self._MUTATING:
            fields["stale"] = True
            fields["stale_reason"] = f"after {tool}"
            with contextlib.suppress(Exception):
                await self._emit(ev.EVENT_CONTEXT_STALE, f"RF context stale after {tool}.",
                                 {"backend_id": self.backend_id, "tool": tool})

        if fields:
            self.context.apply_fields(
                self.backend_id, fields=fields, call_id=call.id, **self._meta_kwargs(meta)
            )
        return summary_type

    def _summarize(self, tool: str, payload: object) -> dict:
        """Compact, bounded summary of a Marconi payload (counts + a few sample ids)."""
        if isinstance(payload, list):
            sample = []
            for item in payload[:5]:
                if isinstance(item, dict):
                    sample.append({k: item[k] for k in ("id", "name", "kind") if k in item})
                else:
                    sample.append(item)
            return {"count": len(payload), "sample": sample}
        if isinstance(payload, dict):
            out = {"count": 1}
            for key in ("valid", "summary", "status", "id", "name", "count", "signals", "bursts"):
                if key in payload and not isinstance(payload[key], (list, dict)):
                    out[key] = payload[key]
            if isinstance(payload.get("signals"), list):
                out["count"] = len(payload["signals"])
            return out
        return {"count": 0, "value": str(payload)[:120]}

    async def _index_artifacts(self, call: MCPToolCallRead, payload: object, meta: dict) -> None:
        paths: list[str] = []
        _collect_paths(payload, paths)
        for candidate in paths:
            try:
                rel = safe_relative_path(self.workspace, candidate)
            except RFArtifactError:
                with contextlib.suppress(Exception):
                    await self._emit(ev.EVENT_ARTIFACT_REJECTED,
                                     "Rejected artifact path outside workspace.",
                                     {"backend_id": self.backend_id, "call_id": call.id})
                continue
            kind, media = classify(rel)
            size, content_hash = stat_and_hash(self.workspace, rel)
            with self.runtime.session_scope() as session:
                _, created = RFArtifactRepository(session).upsert(
                    node_id=self.node_id, backend_id=self.backend_id, relative_path=rel,
                    artifact_kind=kind, media_type=media, size_bytes=size,
                    content_hash=content_hash, mcp_call_id=call.id,
                    mission_id=call.mission_id, mission_step_id=call.mission_step_id,
                )
                session.commit()
            if created:
                with contextlib.suppress(Exception):
                    await self._emit(ev.EVENT_ARTIFACT_INDEXED, f"Indexed artifact {rel}.",
                                     {"backend_id": self.backend_id, "relative_path": rel,
                                      "kind": kind, "call_id": call.id})

    def _meta_kwargs(self, meta: dict) -> dict:
        return {
            "session_id": meta.get("session_id"),
            "session_generation": meta.get("session_generation"),
            "backend_version": meta.get("backend_version"),
            "backend_source_revision": meta.get("backend_source_revision"),
            "mission_id": meta.get("mission_id"),
            "mission_run_id": meta.get("mission_run_id"),
            "mission_step_id": meta.get("mission_step_id"),
        }

    async def _emit(self, event_type: str, message: str, payload: dict) -> None:
        await self.runtime._emit_event(
            event_type=event_type, mission_id=payload.get("mission_id"), source="rf",
            message=message, payload=payload,
        )
