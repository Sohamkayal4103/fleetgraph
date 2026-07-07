"""Automatic owner-research recording: turn a completed mission into a local research record.

When ``data_platform.collection.data_collection_mode == "owner_full"`` the mission engine calls
:func:`record_mission_completion` right after a mission completes. It assembles an honest,
sanitized record of what actually happened — mission + run metadata, provider roles, tool
accountability, bounded event summaries, coding-task summaries, MCP tool calls, and RF-artifact
manifest paths/hashes — and writes it into the local append-only research spool.

Everything is best-effort and fail-open for the *mission*: recording NEVER raises into the engine
(a spool problem must not fail a completed mission). The spool itself secret-scans every record
before it lands in ``raw/`` and diverts anything with a residual credential to ``quarantine/`` — so
API tokens, VNC/OAuth tokens, cookies, passwords, and hosted secrets are removed or quarantined,
never written to ``raw/`` and never uploaded.

Hidden chain-of-thought is not captured — only provider-exposed messages and the structured
metadata the runtime already persists.
"""

from __future__ import annotations

from datetime import UTC, datetime

RESEARCH_RECORD_SCHEMA = "aithernet.research.mission.v1"

#: Bound the record so a chatty mission never writes an unbounded blob (redactor also truncates).
_MAX_EVENTS = 200
_MAX_MCP_CALLS = 200
_MAX_ARTIFACTS = 200
_MAX_CODING_TASKS = 50


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(value, "strftime") else str(value)
    except Exception:  # noqa: BLE001
        return None


def collection_mode(runtime) -> str:
    """The live research-collection mode from the runtime config (``off`` on any error)."""
    try:
        return str(runtime.config.data_platform.collection.data_collection_mode or "off")
    except Exception:  # noqa: BLE001
        return "off"


def owner_recording_enabled(runtime) -> bool:
    return collection_mode(runtime) == "owner_full"


def _mission_and_run(runtime, mission_id: str, run_id: str) -> tuple[dict, dict]:
    """Core mission + run metadata (best-effort; each field guarded)."""
    mission: dict = {"id": mission_id}
    run: dict = {"id": run_id}
    try:
        from aithernet.state.repositories import MissionExecutionRunRepository, MissionRepository
        with runtime.session_scope() as s:
            m = MissionRepository(s).get(mission_id)
            if m is not None:
                mission.update({
                    "source_type": getattr(m, "source_type", None),
                    "status": getattr(m, "status", None),
                    "created_at": _iso(getattr(m, "created_at", None)),
                    "content": getattr(m, "content", None),
                })
            r = MissionExecutionRunRepository(s).get(run_id)
            if r is not None:
                run.update({
                    "status": getattr(r, "status", None),
                    "iteration_count": getattr(r, "iteration_count", None),
                    "coordinator_call_count": getattr(r, "coordinator_call_count", None),
                    "coding_agent_action_count": getattr(r, "coding_agent_action_count", None),
                    "mcp_action_count": getattr(r, "mcp_action_count", None),
                    "elapsed_seconds": getattr(r, "elapsed_seconds", None),
                    "started_at": _iso(getattr(r, "started_at", None)),
                    "completed_at": _iso(getattr(r, "completed_at", None)),
                    "final_response": getattr(r, "final_response", None),
                })
    except Exception:  # noqa: BLE001 — core record still gets written with what we have
        pass
    return mission, run


def _event_summaries(runtime, mission_id: str) -> tuple[list[dict], list[str]]:
    """Bounded, sanitized event summaries + the distinct coding-task ids seen in payloads."""
    events: list[dict] = []
    coding_task_ids: list[str] = []
    try:
        rows = runtime.list_events(mission_id=mission_id, limit=_MAX_EVENTS)
        for e in rows:
            payload = e.payload if isinstance(getattr(e, "payload", None), dict) else {}
            tid = payload.get("task_id")
            if tid and tid not in coding_task_ids:
                coding_task_ids.append(tid)
            events.append({
                "at": _iso(getattr(e, "created_at", None)),
                "event_type": getattr(e, "event_type", None),
                "source": getattr(e, "source", None),
                "message": getattr(e, "message", None),
            })
    except Exception:  # noqa: BLE001
        pass
    return events, coding_task_ids


def _coding_task_summaries(runtime, task_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for tid in task_ids[:_MAX_CODING_TASKS]:
        entry: dict = {"task_id": tid}
        try:
            res = runtime.get_coding_task_result(tid)
        except Exception:  # noqa: BLE001
            res = None
        if res is not None:
            def _len(obj, name):
                v = getattr(obj, name, None)
                return len(v) if isinstance(v, (list, tuple)) else None
            entry.update({
                "status": getattr(res, "status", None),
                "summary": getattr(res, "summary", None),
                "commands_run": _len(res, "commands_run"),
                "files_changed": _len(res, "files_changed"),
                "artifacts": _len(res, "artifacts"),
            })
        out.append(entry)
    return out


def _mcp_calls(runtime, mission_id: str) -> list[dict]:
    out: list[dict] = []
    try:
        for c in runtime.list_mcp_tool_calls(mission_id=mission_id, limit=_MAX_MCP_CALLS):
            out.append({
                "tool_name": getattr(c, "tool_name", None),
                "status": getattr(c, "status", None),
                "backend_id": getattr(c, "backend_id", None),
                "at": _iso(getattr(c, "created_at", None)),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def _artifact_manifest(runtime, mission_id: str) -> list[dict]:
    """RF-artifact manifest entries (workspace-relative path + hash only; never bytes)."""
    out: list[dict] = []
    try:
        from aithernet.state.repositories import RFArtifactRepository
        with runtime.session_scope() as s:
            for a in RFArtifactRepository(s).list(mission_id=mission_id, limit=_MAX_ARTIFACTS):
                out.append({
                    "relative_path": getattr(a, "relative_path", None),
                    "content_hash": getattr(a, "content_hash", None),
                    "digest": getattr(a, "digest", None),
                    "size_bytes": getattr(a, "size_bytes", None),
                    "media_type": getattr(a, "media_type", None),
                    "artifact_kind": getattr(a, "artifact_kind", None),
                })
    except Exception:  # noqa: BLE001
        pass
    return out


def _environment(runtime) -> dict:
    """Provider-agnostic environment/context (secret-free, best-effort, cheap)."""
    import platform as _pf
    env: dict = {"python_version": _pf.python_version()}
    try:
        from aithernet import __version__ as _v
        env["aithernet_version"] = _v
    except Exception:  # noqa: BLE001
        pass
    try:
        env["os"] = f"{_pf.system()} {_pf.release()}"
    except Exception:  # noqa: BLE001
        pass
    try:
        env["hardware_profile"] = getattr(runtime.config, "hardware_profile", None) or \
            getattr(getattr(runtime.config, "profile", None), "name", None)
    except Exception:  # noqa: BLE001
        pass
    return env


def _provider_block(runtime, acct: dict) -> dict:
    """OPTIONAL, sanitized provider metadata — never a provider secret. Provider-agnostic: works for
    CatGPT, Claude/OpenAI/Gemini API, local/OpenAI-compatible coordinators, and any coding provider.
    """
    acct = acct or {}
    tool_providers = []
    if acct.get("gnuradio_mcp_used") or acct.get("mcp_used"):
        tool_providers.append("gnuradio_mcp")
    effective_model = None
    try:
        effective_model = getattr(runtime.coordinator.config, "model", None)
    except Exception:  # noqa: BLE001
        pass
    return {
        "coordinator_provider": acct.get("coordinator_provider"),
        "coding_provider": acct.get("coding_provider"),
        "tool_providers": tool_providers,
        "effective_model": effective_model,
        "provider_support_level": acct.get("provider_support_level", "unknown"),
        "live_verified": acct.get("live_verified", "unknown"),
    }


def _quality_block(outcome: str, blocker_reason: str | None, acct: dict) -> dict:
    acct = acct or {}
    category = None
    if blocker_reason:
        low = blocker_reason.lower()
        if "required_tool" in low or "mcp" in low:
            category = "required_tool_missing"
        elif "sandbox" in low or "apparmor" in low:
            category = "sandbox_failure"
        elif "provider" in low or "auth" in low:
            category = "provider_or_auth"
        else:
            category = "other"
    return {
        "outcome": outcome,
        "pass_fail": "pass" if outcome == "completed" else "fail",
        "satisfied_objective": ("yes" if outcome == "completed"
                                else ("no" if outcome in ("failed", "blocked") else "unknown")),
        "blocker_category": category,
        "required_tool_missing": bool(acct.get("required_tool_missing")),
        "fallback_used": bool(acct.get("fallback_used")),
    }


def build_mission_record(runtime, mission_id: str, run_id: str,
                         accountability: dict | None = None, *, outcome: str = "completed",
                         blocker_reason: str | None = None) -> dict:
    """Assemble the sanitized-but-pre-redaction research record for a mission OUTCOME.

    Records the Aithernet MISSION ARCHITECTURE (mission input, coordinator trajectory via events +
    accountability, tool-use, coding-agent, outcome, environment, quality) — provider-agnostic, not
    a specific provider implementation. The returned dict is passed to
    :meth:`ResearchSpool.write_raw_record`, which redacts + secret-scans it. Must not raise."""
    acct = accountability or {}
    mission, run = _mission_and_run(runtime, mission_id, run_id)
    events, coding_task_ids = _event_summaries(runtime, mission_id)
    return {
        "schema": RESEARCH_RECORD_SCHEMA,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "mission_completion" if outcome == "completed" else "mission_outcome",
        "outcome": outcome,
        "blocker_reason": blocker_reason,
        "mission": mission,
        "run": run,
        "provider": _provider_block(runtime, acct),
        "tool_accountability": acct,
        "coding_task_ids": coding_task_ids,
        "coding_tasks": _coding_task_summaries(runtime, coding_task_ids),
        "mcp_tool_calls": _mcp_calls(runtime, mission_id),
        "artifacts": _artifact_manifest(runtime, mission_id),
        "events": events,
        "environment": _environment(runtime),
        "quality": _quality_block(outcome, blocker_reason, acct),
    }


def record_mission_outcome(runtime, mission_id: str, run_id: str, *, outcome: str = "completed",
                           accountability: dict | None = None,
                           blocker_reason: str | None = None) -> dict | None:
    """Write one sanitized research record for a mission OUTCOME (completed/blocked/failed) into the
    local spool. Returns the spool receipt, or ``None`` when disabled/failed. NEVER raises."""
    try:
        if not owner_recording_enabled(runtime):
            return None
        from aithernet.data.research_spool import ResearchSpool
        spool = ResearchSpool()
        record = build_mission_record(runtime, mission_id, run_id, accountability,
                                      outcome=outcome, blocker_reason=blocker_reason)
        receipt = spool.write_raw_record(record)
        spool.mark_recorded(mission_id=mission_id, run_id=run_id,
                            digest=receipt.get("digest"), dest=receipt.get("dest"))
        return receipt
    except Exception:  # noqa: BLE001 — recording is best-effort; never fail a mission
        return None


def record_mission_completion(runtime, mission_id: str, run_id: str,
                              accountability: dict | None = None) -> dict | None:
    """Backward-compatible wrapper (outcome='completed')."""
    return record_mission_outcome(runtime, mission_id, run_id, outcome="completed",
                                  accountability=accountability)
