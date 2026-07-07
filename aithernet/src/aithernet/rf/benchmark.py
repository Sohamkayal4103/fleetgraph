"""Operator-run comparative RF benchmark harness (Stage 13A.5, Part Q).

A benchmark is EVIDENCE, not a routing rule: running it never changes the default backend and
never declares a winner across non-equivalent scenarios. Each scenario step is exactly ONE
atomic MCP tool call (no hidden multi-tool MissionStep). Scenarios are repository-defined and
restricted to simulation / file-analysis — never live radio transmission. A scenario declares
the read-only/safe tool steps PER backend (where genuinely expressible) plus a validation
predicate; backends without an expressible mapping are skipped honestly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aithernet.rf.adapters import marconi_payload
from aithernet.rf.contracts import RFBackendError, RFCallExecutionContext
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    RFArtifactRepository,
    RFBenchmarkRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime


def _ok_completed(call) -> bool:
    status = call.status.value if hasattr(call.status, "value") else call.status
    return status == "completed"


def _ok_non_error_list(call) -> bool:
    """Validation: the call completed and returned a (possibly empty) list payload."""
    if not _ok_completed(call):
        return False
    payload = marconi_payload(call.result or {})
    # Legacy gr-mcp returns content/text; treat any completed call with no isError as valid.
    return payload is None or isinstance(payload, (list, dict))


#: Repository-defined scenarios. Each maps a backend_id -> ordered list of (tool, args) steps.
#: Only read-only / simulation steps are used; ``validate`` runs on the last step's call.
SCENARIOS: dict[str, dict] = {
    "tool_discovery": {
        "version": "1",
        "description": "List the backend's available building blocks (read-only).",
        "backends": {
            "marconi": [("list_blocks", {})],
            "legacy_gr_mcp": [("get_all_available_blocks", {})],
        },
        "validate": _ok_non_error_list,
    },
    "workspace_state": {
        "version": "1",
        "description": "Read the backend's current state (devices/runs or flowgraph/errors).",
        "backends": {
            "marconi": [("list_devices", {}), ("list_runs", {})],
            "legacy_gr_mcp": [("get_blocks", {}), ("get_all_errors", {})],
        },
        "validate": _ok_non_error_list,
    },
    "simulated_capture_analysis": {
        "version": "1",
        "description": (
            "Marconi-only: simulate a scene, capture IQ, and compute a PSD "
            "(simulation/file-analysis; no live radio)."
        ),
        "backends": {
            "marconi": [
                ("simulate_scene", {"device_id": "bench-sim",
                                    "elements": [{"kind": "tone", "freq": 100_000, "amp": 1.0}]}),
                ("capture", {"device_id": "bench-sim", "center_freq": 0.0,
                             "sample_rate": 1_000_000.0, "duration": 0.05, "name": "bench"}),
            ],
        },
        "validate": _ok_completed,
    },
}


def list_scenarios() -> list[dict]:
    return [
        {
            "scenario_id": sid,
            "version": s["version"],
            "description": s["description"],
            "backends": sorted(s["backends"]),
        }
        for sid, s in SCENARIOS.items()
    ]


class RFBenchmarkRunner:
    """Runs scenarios against backends and records factual metrics (never changes defaults)."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id

    async def run(self, scenario_id: str, backend_id: str, *, repeat: bool = False) -> dict:
        """Run one scenario against one backend, persist a benchmark record, return it."""
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise RFBackendError(
                f"Unknown benchmark scenario {scenario_id!r}.", code="unknown_scenario"
            )
        steps = scenario["backends"].get(backend_id)
        if steps is None:
            raise RFBackendError(
                f"Scenario '{scenario_id}' is not expressible on backend '{backend_id}'.",
                code="scenario_not_supported",
            )

        backend = self.runtime.rf.get_backend(backend_id)
        version = backend.session.server_info().get("version")
        started = utcnow()
        ctx = RFCallExecutionContext(caller="benchmark")
        executed: list[dict] = []
        mcp_calls = 0
        last_call = None
        error_type = None
        success = True
        artifacts_before = self._artifact_count(backend_id)

        for tool, args in steps:
            try:
                last_call = await self.runtime.rf.call_tool(
                    backend_id, tool, args, execution_context=ctx
                )
                mcp_calls += 1
                status = (
                    last_call.status.value if hasattr(last_call.status, "value")
                    else last_call.status
                )
                executed.append({"tool": tool, "status": status})
                if status != "completed":
                    success = False
                    error_type = last_call.error_type or "MCPToolError"
                    break
            except Exception as exc:  # availability / tool errors recorded honestly
                success = False
                error_type = type(exc).__name__
                executed.append({"tool": tool, "status": "error", "error_type": error_type})
                break

        elapsed = (utcnow() - started).total_seconds()
        validate = scenario.get("validate")
        if not success or last_call is None:
            validation_outcome = "failed"
        else:
            try:
                validation_outcome = "passed" if validate(last_call) else "failed"
            except Exception:
                validation_outcome = "unknown"

        artifact_types = self._artifact_types(backend_id)
        artifact_count = self._artifact_count(backend_id) - artifacts_before

        repeatability = None
        if repeat and success:
            # Re-run once for a coarse repeatability signal (no winner declared).
            second = await self.run(scenario_id, backend_id, repeat=False)
            repeatability = "repeatable" if second.get("success") else "not_repeatable"

        with self.runtime.session_scope() as session:
            record = RFBenchmarkRepository(session).create(
                node_id=self.node_id, scenario_id=scenario_id,
                scenario_version=scenario["version"], backend_id=backend_id,
                backend_version=version,
                backend_source_revision=backend.config.source_revision,
                success=success, mission_iterations=0, coordinator_calls=0,
                mcp_calls=mcp_calls, elapsed_seconds=elapsed,
                validation_outcome=validation_outcome,
                artifact_count=max(0, artifact_count), artifact_types_json=artifact_types,
                result_summary=scenario["description"], error_type=error_type,
                repeatability_result=repeatability, steps_json=executed,
            )
            session.commit()
            record_id = record.id
        return self._read(record_id)

    async def run_all(self, scenario_id: str) -> list[dict]:
        """Run a scenario against every backend that can express it (both where practical)."""
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise RFBackendError(
                f"Unknown benchmark scenario {scenario_id!r}.", code="unknown_scenario"
            )
        results = []
        for backend_id in scenario["backends"]:
            if backend_id in self.runtime.rf.backend_ids():
                with __import__("contextlib").suppress(Exception):
                    results.append(await self.run(scenario_id, backend_id))
        return results

    # -- reads -------------------------------------------------------------------

    def list(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = RFBenchmarkRepository(session).list(limit=limit, offset=offset)
            return [self._to_dict(r) for r in rows]

    def get(self, run_id: str) -> dict | None:
        return self._read(run_id)

    def _read(self, run_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            row = RFBenchmarkRepository(session).get(run_id)
            return self._to_dict(row) if row is not None else None

    @staticmethod
    def _to_dict(row) -> dict:
        return {
            "id": row.id, "scenario_id": row.scenario_id, "scenario_version": row.scenario_version,
            "backend_id": row.backend_id, "backend_version": row.backend_version,
            "backend_source_revision": row.backend_source_revision, "success": row.success,
            "mission_iterations": row.mission_iterations,
            "coordinator_calls": row.coordinator_calls,
            "mcp_calls": row.mcp_calls, "elapsed_seconds": row.elapsed_seconds,
            "validation_outcome": row.validation_outcome, "artifact_count": row.artifact_count,
            "artifact_types": row.artifact_types_json, "result_summary": row.result_summary,
            "error_type": row.error_type, "repeatability_result": row.repeatability_result,
            "steps": row.steps_json, "created_at": row.created_at.isoformat(),
        }

    def _artifact_count(self, backend_id: str) -> int:
        with self.runtime.session_scope() as session:
            return RFArtifactRepository(session).count(backend_id=backend_id)

    def _artifact_types(self, backend_id: str) -> list[str]:
        with self.runtime.session_scope() as session:
            arts = RFArtifactRepository(session).list(backend_id=backend_id, limit=200)
            return sorted({a.artifact_kind for a in arts})
