"""Compact mission-run context assembly for coordinator reassessment (Stage 12).

Each autonomous iteration feeds the coordinator a small, structured snapshot of the run:
the objective, where the run stands, what was already tried and what it produced, the
surrounding node/MCP/coding context, and the remaining resource budgets. It is descriptive
history, never a plan. The assembler is deliberately bounded and self-censoring:

  * limits are configurable (:class:`MissionContextLimits`);
  * truncation is marked explicitly so the model knows context was elided;
  * it NEVER includes full DB dumps, raw tool catalogs, complete MCP/Codex bodies, JSONL,
    prompts, or secrets — long free-text is previewed and secret-redacted.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from aithernet.config.settings import MissionContextLimits
from aithernet.missions.budgets import remaining as budget_remaining
from aithernet.sanitize import redact_secrets
from aithernet.schemas.missions import MissionRead
from aithernet.state.models import MissionExecutionRun, utcnow

if TYPE_CHECKING:  # avoid a runtime import cycle with the node runtime
    from aithernet.orchestrator.runtime import NodeRuntime


def _preview(value: object, limit: int) -> str:
    """Render any value to a bounded, secret-redacted preview string."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = redact_secrets(text)
    if len(text) > limit:
        return text[:limit] + "…[truncated]"
    return text


def _step_summary(step, limit: int) -> dict:
    """Compact one persisted mission step (decision summary + bounded result/error)."""
    decision = step.decision or {}
    return {
        "step_id": step.id,
        "route_target": step.route_target,
        "action": step.route_action,
        "status": step.status.value if hasattr(step.status, "value") else step.status,
        "decision_summary": _preview(decision.get("summary", ""), limit),
        "disposition": (decision.get("mission_control") or {}).get("disposition"),
        "result_preview": _preview(step.result, limit) if step.result else "",
        "error": _preview(step.error, limit) if step.error else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
    }


def build_mission_run_context(
    runtime: NodeRuntime,
    *,
    mission: MissionRead,
    run: MissionExecutionRun,
    budgets: dict,
    limits: MissionContextLimits,
) -> dict:
    """Assemble the compact, structured run context handed to the coordinator.

    Reads recent persisted state through the runtime's own (sanitized) accessors and
    bounds every list/preview to ``limits``. Truncation is recorded under ``_truncation``.
    """
    truncation: dict[str, bool] = {}
    preview_limit = limits.result_preview_characters

    # Recent mission steps (newest-first from the repo); surface oldest-first so the model
    # reads them as a progression. Failed actions are highlighted so they are not retried.
    steps = runtime.list_mission_steps(mission_id=mission.id, limit=limits.recent_steps + 1)
    if len(steps) > limits.recent_steps:
        truncation["recent_steps"] = True
        steps = steps[: limits.recent_steps]
    step_summaries = [_step_summary(s, preview_limit) for s in reversed(steps)]
    failed_actions = [
        {
            "step_id": s["step_id"],
            "route_target": s["route_target"],
            "action": s["action"],
            "status": s["status"],
            "error": s["error"],
        }
        for s in step_summaries
        if s["status"] in ("failed", "blocked")
    ]

    # Recent mission-linked MCP calls (names/status only — never full result bodies).
    mcp_calls = runtime.list_mcp_tool_calls(
        mission_id=mission.id, limit=limits.recent_mcp_calls + 1
    )
    if len(mcp_calls) > limits.recent_mcp_calls:
        truncation["recent_mcp_calls"] = True
        mcp_calls = mcp_calls[: limits.recent_mcp_calls]
    mcp_summaries = [
        {
            "call_id": c.id,
            "tool_name": c.tool_name,
            "status": c.status.value if hasattr(c.status, "value") else c.status,
            "error": _preview(c.error, preview_limit) if c.error else None,
        }
        for c in mcp_calls
    ]

    # Recent mission-linked coding tasks (objective preview + status; never full stdout).
    coding_tasks = [
        t
        for t in runtime.list_coding_tasks(limit=200)
        if getattr(t, "mission_id", None) == mission.id
    ]
    if len(coding_tasks) > limits.recent_coding_tasks:
        truncation["recent_coding_tasks"] = True
        coding_tasks = coding_tasks[: limits.recent_coding_tasks]
    coding_summaries = [
        {
            "task_id": t.id,
            "objective_preview": _preview(t.objective, preview_limit),
            "status": t.status.value if hasattr(t.status, "value") else t.status,
        }
        for t in coding_tasks
    ]

    # Recent mission-linked events.
    events = runtime.list_events(mission_id=mission.id, limit=limits.recent_events)
    event_summaries = [
        {
            "event_type": e.event_type,
            "source": e.source,
            "message": _preview(e.message, preview_limit),
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]

    # External-agent messages linked to this mission (resume/inbound signals).
    try:
        agent_messages = runtime.list_external_agent_messages(
            mission_id=mission.id, limit=limits.recent_events
        )
        agent_summaries = [
            {
                "agent_id": m.agent_id,
                "direction": getattr(m, "direction", None),
                "content_preview": _preview(getattr(m, "content", ""), preview_limit),
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in agent_messages
        ]
    except Exception:
        agent_summaries = []

    context: dict = {
        "objective": _preview(mission.content, limits.max_total_characters // 4),
        "mission_status": mission.status.value if hasattr(mission.status, "value")
        else mission.status,
        "run": {
            "run_id": run.id,
            "status": run.status,
            "iteration": run.iteration_count,
            "coordinator_calls": run.coordinator_call_count,
            "consecutive_failures": run.consecutive_failures,
            "recovery_count": run.recovery_count,
            "last_route_target": run.last_route_target,
        },
        "remaining_budgets": budget_remaining(run, budgets),
        "recent_steps": step_summaries,
        "failed_actions": failed_actions,
        "recent_mcp_calls": mcp_summaries,
        "recent_coding_tasks": coding_summaries,
        "recent_events": event_summaries,
        "external_agent_messages": agent_summaries,
        "waiting": run.waiting_json or None,
        "blocked_reason": run.blocked_reason,
        "current_time": utcnow().isoformat(),
    }

    # Stage 13B: compact peer-communication context (outbound messages + delivery/ack state,
    # reply waits + deadlines, bounded conversation, response obligation). It distinguishes a
    # TRANSPORT acknowledgement from a SEMANTIC reply. Never includes signatures/envelopes.
    comms = getattr(runtime, "comms", None)
    if comms is not None:
        with contextlib.suppress(Exception):
            comm_ctx = comms.mission_communication_context(mission.id)
            if any(
                comm_ctx.get(k) for k in
                ("outbound_messages", "reply_waits", "recent_conversation", "response_obligation")
            ):
                context["communications"] = comm_ctx

    # Stage 13D.2: compact artifact context (local metadata, remote offers, transfer states,
    # imported artifacts, quota) — never bytes, absolute paths, or unbounded manifests.
    artifacts = getattr(runtime, "artifacts", None)
    if artifacts is not None:
        with contextlib.suppress(Exception):
            art_ctx = artifacts.coordinator_context()
            if any(art_ctx.get(k) for k in ("local_artifacts", "remote_offers", "transfers")):
                context["artifacts"] = art_ctx

    # Stage 13D.3: compact remote-mission facts (peer, correlated request/conversation, latest
    # AUTHENTICATED state + freshness, sequence, response obligation, artifact/transfer links,
    # last-received time, pending-query flag). The coordinator may reason about stale/unknown
    # status but never writes a snapshot. No prompts/envelopes/signatures/tool internals.
    mission_status = getattr(runtime, "mission_status", None)
    if mission_status is not None:
        with contextlib.suppress(Exception):
            status_ctx = mission_status.coordinator_context()
            if status_ctx.get("remote_missions"):
                context["remote_missions"] = status_ctx["remote_missions"]

    # Stage 14B: compact managed-hardware inventory (known device ids, present/missing,
    # enabled/disabled, health, RX/TX, channels, concise frequency/sample-rate, lease availability,
    # compatible RF backends). NEVER raw driver output, device paths, secrets, environment, or
    # arbitrary provider metadata. The coordinator may select ONLY a known device id + bounded
    # purpose for an rf_device lease action; a model cannot bypass leasing or inject device args.
    if getattr(runtime, "config", None) is not None and runtime.config.hardware.enabled:
        with contextlib.suppress(Exception):
            hw_ctx = runtime.hardware_compact_inventory()
            if hw_ctx.get("devices"):
                context["hardware"] = hw_ctx

    # Enforce a hard character ceiling: drop the lowest-priority sections first, recording
    # what was elided, so the prompt never blows past the configured budget.
    _drop_order = [
        "external_agent_messages",
        "recent_events",
        "recent_coding_tasks",
        "recent_mcp_calls",
    ]
    for key in _drop_order:
        if len(json.dumps(context, default=str)) <= limits.max_total_characters:
            break
        if context.get(key):
            context[key] = []
            truncation[key] = True

    if truncation:
        context["_truncation"] = truncation
    return context
