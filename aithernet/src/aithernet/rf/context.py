"""Generic per-(node, backend) RF workspace context (Stage 13A.5, Part H).

Maintains one compact context row per node+backend for higher-level backends (e.g. Marconi):
devices/captures/signals/measurements/scenes/pipelines/runs/artifacts summaries. The legacy
GNU Radio backend keeps its own dedicated context (Stage 11B) and is exposed through a view,
never duplicated. Summaries are compact — never full captures, plots, schemas, or raw results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aithernet.state.models import utcnow
from aithernet.state.repositories import RFArtifactRepository, RFWorkspaceContextRepository

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime


class RFWorkspaceContextService:
    """Reads/updates the generic RF workspace context for non-legacy backends."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id

    def current(self, backend_id: str) -> dict:
        """Return the full context for a backend as a plain dict (honest defaults if none)."""
        with self.runtime.session_scope() as session:
            record = RFWorkspaceContextRepository(session).get(self.node_id, backend_id)
            artifact_count = RFArtifactRepository(session).count(backend_id=backend_id)
            if record is None:
                return {
                    "node_id": self.node_id,
                    "backend_id": backend_id,
                    "status": "unknown",
                    "stale": True,
                    "stale_reason": "never refreshed",
                    "context_version": 0,
                    "artifact_count": artifact_count,
                }
            return {
                "node_id": record.node_id,
                "backend_id": record.backend_id,
                "backend_version": record.backend_version,
                "backend_source_revision": record.backend_source_revision,
                "status": record.status,
                "stale": record.stale,
                "stale_reason": record.stale_reason,
                "devices_summary": record.devices_summary_json,
                "captures_summary": record.captures_summary_json,
                "signals_summary": record.signals_summary_json,
                "measurements_summary": record.measurements_summary_json,
                "active_scene_summary": record.active_scene_summary_json,
                "active_pipeline_summary": record.active_pipeline_summary_json,
                "pipeline_validation_summary": record.pipeline_validation_summary_json,
                "recent_runs_summary": record.recent_runs_summary_json,
                "artifacts_summary": record.artifacts_summary_json,
                "exported_flowgraphs_summary": record.exported_flowgraphs_summary_json,
                "latest_errors": record.latest_errors_json,
                "related_mission_id": record.related_mission_id,
                "source_mcp_call_ids": record.source_mcp_call_ids_json,
                "source_session_id": record.source_session_id,
                "source_session_generation": record.source_session_generation,
                "context_version": record.context_version,
                "last_refreshed_at": (
                    record.last_refreshed_at.isoformat() if record.last_refreshed_at else None
                ),
                "artifact_count": artifact_count,
            }

    def compact(self, backend_id: str) -> dict:
        """A small, prompt-safe summary of a backend's RF context (counts/status only)."""
        ctx = self.current(backend_id)

        def _count(summary):
            if isinstance(summary, dict):
                return summary.get("count")
            return None

        return {
            "backend_id": backend_id,
            "status": ctx.get("status"),
            "stale": ctx.get("stale"),
            "devices": _count(ctx.get("devices_summary")),
            "captures": _count(ctx.get("captures_summary")),
            "signals": _count(ctx.get("signals_summary")),
            "runs": _count(ctx.get("recent_runs_summary")),
            "artifacts": ctx.get("artifact_count"),
            "last_refreshed_at": ctx.get("last_refreshed_at"),
        }

    def mark_stale(self, backend_id: str, reason: str) -> None:
        """Mark a backend's derived context stale (after a mutation or session restart)."""
        with self.runtime.session_scope() as session:
            RFWorkspaceContextRepository(session).update(
                self.node_id, backend_id, fields={"stale": True, "stale_reason": reason}
            )
            session.commit()

    def apply_fields(
        self,
        backend_id: str,
        *,
        fields: dict,
        call_id: str | None,
        session_id: str | None = None,
        session_generation: int | None = None,
        backend_version: str | None = None,
        backend_source_revision: str | None = None,
        mission_id: str | None = None,
        mission_run_id: str | None = None,
        mission_step_id: str | None = None,
        refreshed: bool = False,
    ) -> None:
        """Apply summary fields to a backend's context and link the source MCP call."""
        with self.runtime.session_scope() as session:
            repo = RFWorkspaceContextRepository(session)
            record = repo.get_or_create(self.node_id, backend_id)
            merged = dict(fields)
            if call_id is not None:
                ids = list(record.source_mcp_call_ids_json or [])
                if call_id not in ids:
                    ids = ([*ids, call_id])[-25:]
                merged["source_mcp_call_ids_json"] = ids
            if session_id is not None:
                merged["source_session_id"] = session_id
            if session_generation is not None:
                merged["source_session_generation"] = session_generation
            if backend_version is not None:
                merged["backend_version"] = backend_version
            if backend_source_revision is not None:
                merged["backend_source_revision"] = backend_source_revision
            for key, value in (
                ("related_mission_id", mission_id),
                ("related_mission_run_id", mission_run_id),
                ("related_mission_step_id", mission_step_id),
            ):
                if value is not None:
                    merged[key] = value
            if refreshed:
                merged["last_refreshed_at"] = utcnow()
                merged.setdefault("stale", False)
                merged.setdefault("stale_reason", None)
            repo.update(self.node_id, backend_id, fields=merged)
            session.commit()
