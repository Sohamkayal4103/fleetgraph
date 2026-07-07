"""The autonomous atomic-iteration engine (Stage 12, Parts F/J/K/L/M).

One call to :meth:`MissionEngine.run_iteration` performs EXACTLY one atomic mission step:

    check budgets -> assemble compact context -> coordinator decides ONE thing ->
    validate mission_control -> persist exactly ONE MissionStep
    (an action step that runs at most one route, OR a control-only step) ->
    persist the real result -> update run usage + mission/run lifecycle.

The worker calls this repeatedly, feeding each real result into the next context, until the
coordinator concludes (complete/wait/blocked) or infrastructure stops the run (budget
exhaustion, pause, cancel). This is NOT a predetermined workflow and NEVER executes more
than one external route per step. Completed actions are never replayed; failed actions are
never silently retried — the coordinator reassesses from the persisted result.
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from enum import Enum
from typing import TYPE_CHECKING

from aithernet.coordinator.contracts import (
    CoordinatorDecision,
    CoordinatorError,
    MissionControlDisposition,
    validate_mission_control,
)
from aithernet.missions import events as ev
from aithernet.missions.budgets import (
    REASON_BUDGET_EXHAUSTED,
    ROUTE_BUDGET,
    fallback_budget_block_reason,
    iteration_block_reason,
    resolve_budgets,
    route_block_reason,
)
from aithernet.missions.context import build_mission_run_context
from aithernet.missions.lifecycle import MissionTransitionError, validate_transition
from aithernet.orchestrator.decision_router import normalize_target
from aithernet.sanitize import redact_secrets
from aithernet.schemas.mission_steps import MissionStepStatus
from aithernet.schemas.missions import MissionRead, MissionStatus
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    MissionExecutionRunRepository,
    MissionRepository,
    MissionStepRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

#: How much free-text to retain in a persisted control-step result.
_REASON_PREVIEW = 2000

#: Synthetic route target recorded for control-only steps (no external route ran).
ROUTE_CONTROL = "mission_control"


class IterationOutcome(str, Enum):
    """Result of one atomic iteration — tells the worker whether to keep looping."""

    CONTINUED = "continued"  # an action step ran (or a recoverable failure); loop again
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    STOPPED = "stopped"  # nothing to do (run terminal / not active)


#: Outcomes that stop the worker loop for this run.
TERMINAL_OUTCOMES = frozenset(
    {
        IterationOutcome.COMPLETED,
        IterationOutcome.WAITING,
        IterationOutcome.BLOCKED,
        IterationOutcome.FAILED,
        IterationOutcome.CANCELLED,
        IterationOutcome.PAUSED,
        IterationOutcome.STOPPED,
    }
)


def _elapsed_since(started) -> float:
    """Seconds elapsed since ``started`` (SQLite returns naive datetimes — assume UTC)."""
    if started is None:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (utcnow() - started).total_seconds())


def _short(text: str | None) -> str | None:
    if not text:
        return text
    text = redact_secrets(text)
    return text if len(text) <= _REASON_PREVIEW else text[:_REASON_PREVIEW] + "…[truncated]"


def _is_coding_sandbox_failure(error: str | None) -> bool:
    """beta.5: True if a coding-agent route error is a bubblewrap userns/loopback failure."""
    if not error:
        return False
    try:
        from aithernet.coding_agent.sandbox_preflight import matches_sandbox_failure
        return matches_sandbox_failure(error)
    except Exception:  # noqa: BLE001 — classification must never break the engine
        return False


class MissionEngine:
    """Executes atomic mission iterations against a :class:`NodeRuntime`."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.mission_execution

    # -- run-row helpers ---------------------------------------------------------

    def _get_run(self, run_id: str):
        with self.runtime.session_scope() as session:
            return MissionExecutionRunRepository(session).get(run_id)

    def _update_run(self, run_id: str, **fields) -> None:
        with self.runtime.session_scope() as session:
            MissionExecutionRunRepository(session).update(run_id, fields=fields)
            session.commit()

    def _apply_transition(
        self,
        run_id: str,
        mission_id: str,
        target: MissionStatus,
        *,
        run_fields: dict | None = None,
    ) -> None:
        """Centralized, validated transition applied to BOTH the mission and run rows.

        Terminal states are never silently reopened (validated by the lifecycle machine).
        """
        with self.runtime.session_scope() as session:
            missions = MissionRepository(session)
            runs = MissionExecutionRunRepository(session)
            mission_row = missions.get(mission_id)
            run = runs.get(run_id)
            if mission_row is None or run is None:
                return
            current_mission = MissionStatus(mission_row.status)
            current_run = MissionStatus(run.status)
            validate_transition(current_mission, target)
            validate_transition(current_run, target)
            missions.update_status(mission_id, target.value)
            fields = dict(run_fields or {})
            fields["status"] = target.value
            fields["last_activity_at"] = utcnow()
            runs.update(run_id, fields=fields)
            session.commit()

    def _persist_control_step(
        self,
        mission_id: str,
        run_id: str,
        *,
        disposition: str,
        status: str,
        reason: str | None = None,
        result_extra: dict | None = None,
        decision: CoordinatorDecision | None = None,
    ) -> str:
        """Persist a control-only MissionStep (no external route executed).

        Honest accounting: the step records that a lifecycle decision was made, not that
        any external action ran. ``decision`` is attached when one came from the model;
        engine-originated control steps (budget/cancel/pause) synthesize a minimal record.
        """
        decision_id = decision.decision_id if decision is not None else new_uuid()
        decision_json = (
            decision.model_dump(mode="json")
            if decision is not None
            else {
                "decision_id": decision_id,
                "mission_id": mission_id,
                "summary": _short(reason) or disposition,
                "next_target": ROUTE_CONTROL,
                "action": disposition,
                "source": "engine",
                "mission_control": {"disposition": disposition},
            }
        )
        result = {"type": "mission_control", "disposition": disposition}
        if reason:
            result["reason"] = _short(reason)
        if result_extra:
            result.update(result_extra)
        now = utcnow()
        with self.runtime.session_scope() as session:
            step = MissionStepRepository(session).create(
                mission_id=mission_id,
                decision_id=decision_id,
                decision=decision_json,
                route_target=ROUTE_CONTROL,
                route_action=disposition,
                status=status,
                started_at=now,
            )
            MissionStepRepository(session).update(
                step.id, result=result, completed_at=now
            )
            session.commit()
            step_id = step.id
        self._update_run(run_id, last_mission_step_id=step_id, last_activity_at=now)
        return step_id

    # -- the atomic iteration ----------------------------------------------------

    async def run_iteration(self, run_id: str) -> IterationOutcome:
        """Perform exactly one atomic iteration for ``run_id``. See module docstring."""
        run = self._get_run(run_id)
        if run is None or run.status != "active":
            return IterationOutcome.STOPPED
        mission_id = run.mission_id

        # 1. Cancellation requested -> acknowledge honestly and stop. No coordinator call
        #    is made after acknowledgement.
        if run.cancel_requested_at is not None and run.cancel_acknowledged_at is None:
            return await self._handle_cancel(run_id, mission_id)

        # 2. Pause requested -> finish here (we are at an atomic boundary) and pause.
        if run.pause_requested:
            return await self._handle_pause(run_id, mission_id)

        budgets = resolve_budgets(self.config.default_budgets, run.budgets_json)

        # 3. Budget gate BEFORE any coordinator call. Exhaustion -> blocked (never completed),
        #    and we do NOT call the coordinator merely to announce exhaustion.
        block_reason = iteration_block_reason(run, budgets)
        if block_reason is not None:
            return await self._handle_budget_block(run_id, mission_id, block_reason)

        # 4. Account for this iteration + emit iteration.started.
        elapsed = _elapsed_since(run.started_at)
        iteration_no = run.iteration_count + 1
        self._update_run(
            run_id,
            iteration_count=iteration_no,
            elapsed_seconds=elapsed,
            last_activity_at=utcnow(),
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_ITERATION_STARTED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission iteration {iteration_no} started for mission {mission_id}.",
            payload={"run_id": run_id, "iteration": iteration_no},
        )

        # 5. Assemble compact context and ask the coordinator for ONE decision.
        mission = self.runtime.get_mission(mission_id)
        if mission is None:
            return IterationOutcome.STOPPED
        run = self._get_run(run_id)
        context = build_mission_run_context(
            self.runtime,
            mission=mission,
            run=run,
            budgets=budgets,
            limits=self.config.context,
        )

        try:
            decision = await self.runtime.invoke_coordinator(mission_id, extra_context=context)
        except CoordinatorError as exc:
            return await self._handle_coordinator_failure(
                run_id, mission_id, budgets, str(exc), exc=exc)

        # coordinator call succeeded -> count it.
        self._update_run(
            run_id,
            coordinator_call_count=run.coordinator_call_count + 1,
            last_decision_id=decision.decision_id,
            last_activity_at=utcnow(),
        )

        # 6. Validate the mission_control contract. Reject + persist a clear failure.
        validation_error = validate_mission_control(decision)
        if validation_error is not None:
            return await self._handle_invalid_decision(
                run_id, mission_id, budgets, decision, validation_error
            )

        await self.runtime._emit_event(
            event_type=ev.EVENT_DECISION_COMPLETED,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(decision.summary) or "Coordinator decision recorded.",
            payload={
                "run_id": run_id,
                "decision_id": decision.decision_id,
                "disposition": decision.mission_control.disposition.value,
                "next_target": decision.next_target,
            },
        )

        # 7. Dispatch on the lifecycle disposition.
        disp = decision.mission_control.disposition
        if disp is MissionControlDisposition.COMPLETE:
            return await self._handle_complete(run_id, mission_id, decision)
        if disp is MissionControlDisposition.WAIT:
            return await self._handle_wait(run_id, mission_id, decision)
        if disp is MissionControlDisposition.BLOCKED:
            return await self._handle_blocked(run_id, mission_id, decision)
        return await self._handle_continue(run_id, mission_id, budgets, decision)

    # -- disposition handlers ----------------------------------------------------

    async def _handle_continue(
        self, run_id: str, mission_id: str, budgets: dict, decision: CoordinatorDecision
    ) -> IterationOutcome:
        """Execute AT MOST ONE external route, persist the real result, then reassess."""
        route_target = normalize_target(decision.next_target)

        # Per-route budget gate.
        rb = route_block_reason(self._get_run(run_id), budgets, route_target)
        if rb is not None:
            return await self._handle_budget_block(run_id, mission_id, rb)

        # beta.5 fallback budget protection: before STARTING a gnuradio_mcp flowgraph build, ensure
        # enough actions remain to build + validate + execute it. Blocking early with a clear reason
        # beats spending the remaining budget on doomed partial work (a real beta.4 failure).
        if route_target == "gnuradio_mcp":
            fb = fallback_budget_block_reason(self._get_run(run_id), budgets)
            if fb is not None:
                return await self._handle_budget_block(run_id, mission_id, fb)

        # beta.7 (FIX 6): before ENTERING the coding agent, verify the bubblewrap sandbox can
        # initialize. On a host with the AppArmor userns restriction every coding action fails, so
        # block early (before spending the coding-route budget) with a guided repair rather than
        # dispatching a route that is known to fail.
        if route_target == "coding_agent":
            preflight = self._coding_sandbox_preflight_block(run_id)
            if preflight is not None:
                return await self._handle_coding_sandbox_block(run_id, mission_id, preflight)

        now = utcnow()
        with self.runtime.session_scope() as session:
            step = MissionStepRepository(session).create(
                mission_id=mission_id,
                decision_id=decision.decision_id,
                decision=decision.model_dump(mode="json"),
                route_target=route_target,
                route_action=decision.action,
                status=MissionStepStatus.RUNNING.value,
                started_at=now,
            )
            session.commit()
            step_id = step.id

        await self.runtime._emit_event(
            event_type=ev.EVENT_ACTION_STARTED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission action started (target={route_target}, action={decision.action}).",
            payload={"run_id": run_id, "step_id": step_id, "route_target": route_target},
        )

        try:
            route = await self.runtime._route_decision(
                mission_id, decision, route_target, step_id
            )
        except Exception as exc:  # never strand the step in `running`
            self.runtime._update_mission_step(
                step_id,
                status=MissionStepStatus.FAILED.value,
                error=_short(f"Unexpected routing error [{type(exc).__name__}]: {exc}"),
                completed_at=utcnow(),
            )
            await self._after_route(run_id, mission_id, route_target, step_id, succeeded=False)
            await self.runtime._emit_event(
                event_type=ev.EVENT_ACTION_FAILED,
                mission_id=mission_id,
                source="mission_worker",
                message=f"Mission action failed unexpectedly: {type(exc).__name__}.",
                payload={"run_id": run_id, "step_id": step_id, "route_target": route_target},
            )
            return IterationOutcome.CONTINUED

        self.runtime._update_mission_step(
            step_id,
            status=route.status,
            result=route.result,
            error=_short(route.error),
            completed_at=utcnow(),
        )
        succeeded = route.status == MissionStepStatus.COMPLETED.value
        # beta.5: a correctable MCP schema rejection returns a structured correction to the
        # coordinator WITHOUT consuming the scarce gnuradio_mcp action budget or extending the
        # failure streak — the coordinator simply re-issues a valid call. The iteration /
        # coordinator-call budgets still bound how long it may keep trying.
        schema_rejected = isinstance(route.result, dict) and route.result.get("schema_rejected")
        await self._after_route(run_id, mission_id, route_target, step_id,
                                succeeded=succeeded, consume_budget=not schema_rejected)
        await self.runtime._emit_event(
            event_type=ev.EVENT_ACTION_COMPLETED if succeeded else ev.EVENT_ACTION_FAILED,
            mission_id=mission_id,
            source="mission_worker",
            message=(
                f"Mission action {route.status} (target={route_target}, action={decision.action})."
            ),
            payload={
                "run_id": run_id,
                "step_id": step_id,
                "route_target": route_target,
                "status": route.status,
            },
        )
        # beta.5: a coding-agent bubblewrap sandbox failure (uid-map / loopback) will not resolve by
        # looping — block early with actionable remediation instead of burning further iterations.
        # Recoverable via `aithernet mission retry` once the sandbox is fixed (retry re-runs it).
        if not succeeded and route_target == "coding_agent" and _is_coding_sandbox_failure(
                route.error):
            return await self._handle_coding_sandbox_block(run_id, mission_id, route.error or "")
        return IterationOutcome.CONTINUED

    async def _after_route(
        self, run_id: str, mission_id: str, route_target: str, step_id: str, *, succeeded: bool,
        consume_budget: bool = True,
    ) -> None:
        """Update run usage counters + failure tracking after one route executes.

        ``consume_budget=False`` (beta.5) marks a correctable, pre-dispatch rejection (e.g. an MCP
        schema-guard correction): it neither consumes the route's action budget nor extends/resets
        the failure streak, so a fixable mistake does not spend a scarce action."""
        run = self._get_run(run_id)
        fields: dict = {
            "last_mission_step_id": step_id,
            "last_route_target": route_target,
            "last_activity_at": utcnow(),
        }
        if not consume_budget:
            self._update_run(run_id, **fields)
            return
        mapping = ROUTE_BUDGET.get(route_target)
        if mapping is not None:
            count_attr = mapping[1]
            fields[count_attr] = getattr(run, count_attr, 0) + 1
        # A completed route resets the consecutive-failure streak; a failed/blocked route
        # extends it (the route is NEVER auto-retried — the coordinator reassesses).
        fields["consecutive_failures"] = 0 if succeeded else run.consecutive_failures + 1
        self._update_run(run_id, **fields)

    def _mission_content(self, mission_id: str) -> str:
        """The original mission prompt (for deterministic tool-requirement detection)."""
        try:
            from aithernet.state.repositories import MissionRepository
            with self.runtime.session_scope() as s:
                m = MissionRepository(s).get(mission_id)
                return getattr(m, "content", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _tool_accountability(self, mission_id: str, run_id: str) -> dict:
        """beta.7 (FIX 7): an honest, structured record of which tools a mission actually used."""
        run = self._get_run(run_id)
        coordinator = coding = None
        try:
            coordinator = self.runtime.coordinator.config.provider
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.runtime.coding_agent.is_configured():
                coding = self.runtime.coding_agent.config.provider
        except Exception:  # noqa: BLE001
            pass
        tools: list[str] = []
        try:
            tools = [c.tool_name for c in self.runtime.list_mcp_tool_calls(mission_id=mission_id)
                     if getattr(c, "tool_name", None)]
        except Exception:  # noqa: BLE001
            pass
        mcp_used = bool(tools) or bool(getattr(run, "mcp_action_count", 0))
        return {
            "coordinator_provider": coordinator,
            "coding_provider": coding,
            "mcp_used": mcp_used,
            "mcp_tools_called": tools,
            "gnuradio_mcp_used": mcp_used,   # the only MCP backend is GNU Radio MCP
            "gnuradio_runtime_used_by_coding": "unknown",  # coding agent has no GNU Radio MCP yet
            "fallback_used": False,          # no automatic provider fallback is wired at run time
            "fallback_reason": None,
            "required_tool_missing": False,
        }

    async def _emit_tool_accountability(self, mission_id: str, run_id: str, acct: dict) -> None:
        msg = ("Tool accountability: "
               f"coordinator={acct['coordinator_provider']} coding={acct['coding_provider']} "
               f"mcp_used={acct['mcp_used']} gnuradio_mcp_used={acct['gnuradio_mcp_used']} "
               f"gnuradio_runtime_used_by_coding={acct['gnuradio_runtime_used_by_coding']} "
               f"fallback_used={acct['fallback_used']} "
               f"required_tool_missing={acct['required_tool_missing']}")
        await self.runtime._emit_event(
            event_type=ev.EVENT_TOOL_ACCOUNTABILITY, mission_id=mission_id,
            source="mission_worker", message=msg, payload={"run_id": run_id, **acct})

    async def _handle_mcp_requirement_block(
        self, run_id: str, mission_id: str, req: dict, acct: dict
    ) -> IterationOutcome:
        """beta.7 (FIX 8): the prompt REQUIRES MCP/GNU-Radio but no MCP call happened — refuse to
        complete through a fallback; block with a structured, actionable reason."""
        acct = {**acct, "required_tool_missing": True}
        reason = (
            "blocked_required_tool[mcp_required]: this mission requires GNU Radio / RF-MCP "
            + ("validation before implementation" if req.get("mcp_before_implementation")
               else "validation")
            + ", but no MCP tool call was made — not completing through a fallback. Ensure the GNU "
            "Radio MCP is configured (`aithernet doctor` → mcp_wiring) and re-run, or relax the "
            "requirement in the mission prompt.")
        self._persist_control_step(
            mission_id, run_id, disposition="blocked",
            status=MissionStepStatus.BLOCKED.value, reason=reason,
            result_extra={"failure_type": "required_tool_missing",
                          "failure_category": "required_tool_missing",
                          "missing_capability": "gnuradio_mcp", "tool_accountability": acct},
        )
        self._apply_transition(run_id, mission_id, MissionStatus.BLOCKED,
                               run_fields={"blocked_reason": _short(reason)})
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED, mission_id=mission_id, source="mission_worker",
            message=_short(reason), payload={"run_id": run_id,
                                             "failure_category": "required_tool_missing",
                                             "missing_capability": "gnuradio_mcp",
                                             "tool_accountability": acct})
        await self._emit_tool_accountability(mission_id, run_id, acct)
        self._record_owner_research(mission_id, run_id, outcome="blocked", acct=acct,
                                    blocker_reason=reason)
        return IterationOutcome.BLOCKED

    async def _handle_complete(
        self, run_id: str, mission_id: str, decision: CoordinatorDecision
    ) -> IterationOutcome:
        final = decision.mission_control.final_response
        # Stage 13B response obligation: a response-required inbound-request mission may only
        # complete after a correlated reply has been durably queued. If unmet, reject the
        # completion cleanly (NOT a failure) and loop so the coordinator can queue the reply.
        obligation = self._response_obligation_unmet(mission_id)
        if obligation is not None:
            return await self._handle_obligation_block(run_id, mission_id, decision, obligation)
        # beta.7 (FIX 7/8): compute tool accountability and enforce an explicit MCP/GNU-Radio
        # requirement. If the prompt REQUIRES MCP but no MCP tool was called, refuse to complete
        # through a fallback — block with a structured reason instead.
        from aithernet.missions.requirements import parse_tool_requirements
        acct = self._tool_accountability(mission_id, run_id)
        req = parse_tool_requirements(self._mission_content(mission_id))
        if req.get("mcp_required") and not acct["mcp_used"]:
            return await self._handle_mcp_requirement_block(run_id, mission_id, req, acct)
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="complete",
            status=MissionStepStatus.COMPLETED.value,
            reason=decision.mission_control.reason,
            result_extra={"final_response": _short(final), "tool_accountability": acct},
            decision=decision,
        )
        self._apply_transition(
            run_id,
            mission_id,
            MissionStatus.COMPLETED,
            run_fields={
                "final_response": _short(final),
                "consecutive_failures": 0,
                "completed_at": utcnow(),
            },
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_COMPLETED,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(final) or f"Mission {mission_id} completed.",
            payload={"run_id": run_id, "tool_accountability": acct},
        )
        await self._emit_tool_accountability(mission_id, run_id, acct)
        self._record_owner_research(mission_id, run_id, outcome="completed", acct=acct)
        return IterationOutcome.COMPLETED

    def _record_owner_research(self, mission_id: str, run_id: str, *, outcome: str = "completed",
                               acct: dict | None = None, blocker_reason: str | None = None) -> None:
        """beta.9: when owner_full recording is enabled, write one sanitized research record for the
        mission OUTCOME (completed / blocked / failed) into the local spool, then best-effort upload
        to the hosted OWNER ARCHIVE (provider-agnostic; hooks generic MissionEngine layers only).

        Fully guarded — a data-collection problem must NEVER fail or block a mission. Works for any
        coordinator/coding provider (CatGPT, API providers, local) — no CatGPT/Codex dependency."""
        try:
            from aithernet.data.research_recorder import (
                owner_recording_enabled,
                record_mission_outcome,
            )
            if not owner_recording_enabled(self.runtime):
                return
            if acct is None:
                acct = self._tool_accountability(mission_id, run_id)
            record_mission_outcome(self.runtime, mission_id, run_id, outcome=outcome,
                                   accountability=acct, blocker_reason=blocker_reason)
            # Default hosted-client path: upload to the Aithernet owner archive (no client Drive).
            from aithernet.data.research_upload import maybe_upload_owner_archive
            maybe_upload_owner_archive(self.runtime)
            # Advanced standalone/local-archive mode only: legacy client Drive sync.
            from aithernet.data.research_sync import maybe_auto_sync
            maybe_auto_sync(self.runtime)
        except Exception:  # noqa: BLE001 — recording/upload are best-effort, never mission-fatal
            pass

    async def _handle_wait(
        self, run_id: str, mission_id: str, decision: CoordinatorDecision
    ) -> IterationOutcome:
        waiting = decision.mission_control.waiting
        waiting_json = waiting.model_dump(mode="json") if waiting is not None else {}

        # Stage 13B: a structured peer-reply wait creates a durable MissionReplyWait BEFORE the
        # mission becomes waiting. If a correlated reply already arrived (reply-before-wait
        # race), the wait is satisfied inline and the mission CONTINUES (it does not get stuck).
        condition = waiting.wait_condition if waiting is not None else None
        if condition is not None and condition.type == "peer_reply":
            outcome = await self._handle_peer_reply_wait(run_id, mission_id, decision, condition)
            if outcome is not None:
                return outcome  # error/continue handled; otherwise fall through to WAITING below

        self._persist_control_step(
            mission_id,
            run_id,
            disposition="wait",
            status=MissionStepStatus.COMPLETED.value,
            reason=waiting.reason if waiting else None,
            result_extra={"waiting": waiting_json},
            decision=decision,
        )
        self._apply_transition(
            run_id,
            mission_id,
            MissionStatus.WAITING,
            run_fields={"waiting_json": waiting_json, "waiting_at": utcnow()},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_WAITING,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(waiting.reason) if waiting else f"Mission {mission_id} waiting.",
            payload={"run_id": run_id, "waiting": waiting_json},
        )
        return IterationOutcome.WAITING

    async def _handle_peer_reply_wait(
        self, run_id: str, mission_id: str, decision: CoordinatorDecision, condition
    ) -> IterationOutcome | None:
        """Create/resolve a durable peer-reply wait. Returns an outcome to short-circuit, or
        ``None`` to proceed into the normal WAITING transition."""
        from aithernet.comms.service import CommunicationError

        if not condition.outbound_message_id:
            return await self._handle_invalid_decision(
                run_id, mission_id,
                resolve_budgets(self.config.default_budgets, self._get_run(run_id).budgets_json),
                decision,
                "wait_condition.peer_reply requires an outbound_message_id.",
            )
        try:
            result = await self.runtime.comms.prepare_peer_reply_wait(
                mission_id=mission_id,
                mission_run_id=run_id,
                mission_step_id=self._get_run(run_id).last_mission_step_id,
                outbound_message_id=condition.outbound_message_id,
                deadline=condition.deadline,
            )
        except CommunicationError as exc:
            return await self._handle_invalid_decision(
                run_id, mission_id,
                resolve_budgets(self.config.default_budgets, self._get_run(run_id).budgets_json),
                decision, f"wait_condition invalid: {exc}",
            )
        if result["kind"] == "already_satisfied":
            # The correlated reply is already present — record a control step and CONTINUE so
            # the coordinator reassesses with the reply in context (it never gets stuck).
            self._persist_control_step(
                mission_id, run_id, disposition="continue",
                status=MissionStepStatus.COMPLETED.value,
                reason="peer reply already present; resuming without waiting",
                result_extra={"wait": result}, decision=decision,
            )
            return IterationOutcome.CONTINUED
        return None  # proceed to the WAITING transition below

    async def _handle_obligation_block(
        self, run_id: str, mission_id: str, decision: CoordinatorDecision, obligation: str
    ) -> IterationOutcome:
        """Reject a premature completion of a response-required inbound mission (NOT a failure)."""
        self._persist_control_step(
            mission_id, run_id, disposition="continue",
            status=MissionStepStatus.BLOCKED.value,
            reason=f"response obligation unmet: {obligation}",
            decision=decision,
        )
        self._update_run(run_id, last_activity_at=utcnow())
        await self.runtime._emit_event(
            event_type=ev.EVENT_DECISION_COMPLETED,
            mission_id=mission_id, source="mission_worker",
            message="Completion rejected: a correlated reply must be queued first.",
            payload={"run_id": run_id, "obligation": _short(obligation)},
        )
        return IterationOutcome.CONTINUED

    def _response_obligation_unmet(self, mission_id: str) -> str | None:
        comms = getattr(self.runtime, "comms", None)
        if comms is None:
            return None
        try:
            return comms.response_obligation_unmet(mission_id)
        except Exception:
            return None

    async def _handle_blocked(
        self, run_id: str, mission_id: str, decision: CoordinatorDecision
    ) -> IterationOutcome:
        blocked = decision.mission_control.blocked
        reason = blocked.reason if blocked else (decision.mission_control.reason or "blocked")
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="blocked",
            status=MissionStepStatus.BLOCKED.value,
            reason=reason,
            result_extra={
                "missing_capability": blocked.missing_capability if blocked else None
            },
            decision=decision,
        )
        self._apply_transition(
            run_id,
            mission_id,
            MissionStatus.BLOCKED,
            run_fields={"blocked_reason": _short(reason)},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(reason) or f"Mission {mission_id} blocked.",
            payload={"run_id": run_id},
        )
        return IterationOutcome.BLOCKED

    # -- failure / budget / lifecycle-interrupt handlers -------------------------

    async def _handle_budget_block(
        self, run_id: str, mission_id: str, reason: str
    ) -> IterationOutcome:
        """Persist a control step + block the mission on resource exhaustion (never completed)."""
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="blocked",
            status=MissionStepStatus.BLOCKED.value,
            reason=reason,
        )
        self._apply_transition(
            run_id,
            mission_id,
            MissionStatus.BLOCKED,
            run_fields={"blocked_reason": _short(reason)},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BUDGET_EXHAUSTED,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(reason) or "Mission resource budget exhausted.",
            payload={"run_id": run_id, "reason": REASON_BUDGET_EXHAUSTED},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED,
            mission_id=mission_id,
            source="mission_worker",
            message=_short(reason) or "Mission blocked.",
            payload={"run_id": run_id},
        )
        return IterationOutcome.BLOCKED

    async def _handle_permanent_block(
        self, run_id: str, mission_id: str, error: str, *, failure_category: str = "configuration"
    ) -> IterationOutcome:
        """Block immediately (ONE attempt) on a non-retryable provider configuration/auth error.

        Names the failing coordinator provider + the required customer action; never increments the
        failure budget and is never relabeled as resource_budget_exhausted. The fine-grained
        ``failure_category`` (configuration/authentication/executable) is recorded for telemetry so
        ``aithernet doctor`` / ``mission retry`` can point at the exact repair (beta.9 Defect 3)."""
        provider = "?"
        try:
            provider = self.runtime.coordinator.config.provider
        except Exception:  # noqa: BLE001
            pass
        reason = (f"provider_configuration_error[{failure_category}]: coordinator provider "
                  f"'{provider}' is not usable: {error}. Repair it (aithernet doctor --repair, or "
                  f"aithernet agents connect coordinator), then `aithernet mission retry "
                  f"{mission_id}`.")
        self._persist_control_step(
            mission_id, run_id, disposition="blocked",
            status=MissionStepStatus.BLOCKED.value, reason=reason,
            result_extra={"failure_type": "permanent", "failure_category": failure_category,
                          "provider": provider},
        )
        self._apply_transition(
            run_id, mission_id, MissionStatus.BLOCKED,
            run_fields={"blocked_reason": _short(reason)},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED, mission_id=mission_id, source="mission_worker",
            message=_short(reason), payload={"run_id": run_id, "failure_type": "permanent",
                                             "failure_category": failure_category,
                                             "provider": provider},
        )
        return IterationOutcome.BLOCKED

    async def _handle_quota_block(
        self, run_id: str, mission_id: str, error: str
    ) -> IterationOutcome:
        """beta.10 Defect 4: park a mission whose coordinator provider quota is exhausted.

        Records ``failure_category=quota_exhausted`` and blocks with the recoverable reason
        ``blocked_provider_quota`` WITHOUT incrementing the consecutive-failure budget (so the
        mission is never mislabeled ``resource_budget_exhausted``). Names the exhausted provider and
        whether a fallback coordinator is configured, and is recoverable via ``mission retry``."""
        provider = "?"
        fallbacks: list[str] = []
        try:
            cfg = self.runtime.coordinator.config
            provider = cfg.provider
            fallbacks = list(getattr(cfg, "fallback_providers", []) or [])
        except Exception:  # noqa: BLE001
            pass
        if fallbacks:
            hint = (f" A fallback coordinator is configured ({', '.join(fallbacks)}); connect it "
                    f"with `aithernet agents connect coordinator`, then ")
        else:
            # beta.5: point at a concrete, subscription-backed local alternative (CatGPT Gateway),
            # so the operator has an immediate path off the exhausted provider.
            hint = (" Connect a different coordinator with `aithernet agents connect coordinator` "
                    "(for example the local CatGPT Gateway: `aithernet agents connect coordinator "
                    "--provider catgpt_gateway`), or wait for the quota to reset, then ")
        reason = (f"blocked_provider_quota[quota_exhausted]: coordinator provider '{provider}' "
                  f"quota is exhausted: {error}.{hint}`aithernet mission retry {mission_id}`.")
        # beta.5: reflect the fresh quota failure in provider-status so `mission_ready` no longer
        # reads true off a stale earlier live-verification (best-effort; never writes a secret and
        # is a no-op when there is no resolvable node config (e.g. unit tests with a fake runtime).
        if getattr(self.runtime, "config", None) is not None:
            try:
                from aithernet.agents import providers as _prov
                _prov.record_provider_readiness("coordinator", "quota_exhausted", live=False)
            except Exception:  # noqa: BLE001 — status refresh is best-effort
                pass
        self._persist_control_step(
            mission_id, run_id, disposition="blocked",
            status=MissionStepStatus.BLOCKED.value, reason=reason,
            result_extra={"failure_type": "quota", "failure_category": "quota_exhausted",
                          "provider": provider, "fallback_providers": fallbacks},
        )
        self._apply_transition(
            run_id, mission_id, MissionStatus.BLOCKED,
            run_fields={"blocked_reason": _short(reason)},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED, mission_id=mission_id, source="mission_worker",
            message=_short(reason), payload={"run_id": run_id, "failure_type": "quota",
                                             "failure_category": "quota_exhausted",
                                             "provider": provider,
                                             "fallback_providers": fallbacks},
        )
        return IterationOutcome.BLOCKED

    def _coding_sandbox_preflight_block(self, run_id: str) -> str | None:
        """beta.7 (FIX 6): return a blocker string if the coding sandbox is known-degraded, else
        None. Probes ``preflight_sandbox()`` at most once per run when healthy (cached); while
        blocked it re-probes each dispatch (which immediately blocks), so a `mission retry` after a
        `coding-sandbox repair` picks up the fix. Never raises."""
        ok = getattr(self, "_sandbox_ok_runs", None)
        if ok is None:
            ok = self._sandbox_ok_runs = set()
        if run_id in ok:
            return None
        from aithernet.coding_agent import sandbox_preflight as sp
        try:
            probe = sp.preflight_sandbox()
        except Exception:  # noqa: BLE001 — a broken preflight must never block a mission itself
            ok.add(run_id)
            return None
        if probe.severity == "blocked":
            return ("this mission requires coding-agent filesystem operations, but the coding "
                    "sandbox is blocked by Ubuntu AppArmor user-namespace restrictions "
                    f"[{sp.SYSCTL_APPARMOR_USERNS}="
                    f"{sp.apparmor_restrict_value()}]. {probe.detail}")
        ok.add(run_id)  # healthy → do not re-probe this run
        return None

    async def _handle_coding_sandbox_block(
        self, run_id: str, mission_id: str, error: str
    ) -> IterationOutcome:
        """beta.5: block a mission whose coding-agent bubblewrap sandbox cannot initialize.

        The uid-map / loopback failure (typically Ubuntu's
        ``kernel.apparmor_restrict_unprivileged_userns=1``) will not resolve by retrying the same
        route, so we block ONCE with the exact, security-annotated remediation and point at
        ``aithernet doctor`` — instead of burning the remaining iteration budget re-selecting a
        coding agent that is known to be impossible on this host. Recoverable via ``mission retry``
        after the host is fixed (the fresh run re-attempts the coding route)."""
        from aithernet.coding_agent.sandbox_preflight import remediation_text
        provider = "?"
        try:
            provider = self.runtime.coding_agent.config.provider
        except Exception:  # noqa: BLE001
            pass
        reason = (
            f"blocked_coding_sandbox[coding_sandbox_unavailable]: the coding agent's bubblewrap "
            f"sandbox could not initialize on this host ({_short(error)}). {remediation_text()} "
            f"Verify with `aithernet doctor` (coding_sandbox check); once the host is fixed, "
            f"`aithernet mission retry {mission_id}`.")
        self._persist_control_step(
            mission_id, run_id, disposition="blocked",
            status=MissionStepStatus.BLOCKED.value, reason=reason,
            result_extra={"failure_type": "coding_sandbox",
                          "failure_category": "coding_sandbox_unavailable", "provider": provider},
        )
        self._apply_transition(
            run_id, mission_id, MissionStatus.BLOCKED,
            run_fields={"blocked_reason": _short(reason)},
        )
        await self.runtime._emit_event(
            event_type=ev.EVENT_BLOCKED, mission_id=mission_id, source="mission_worker",
            message=_short(reason), payload={"run_id": run_id, "failure_type": "coding_sandbox",
                                             "failure_category": "coding_sandbox_unavailable",
                                             "provider": provider},
        )
        return IterationOutcome.BLOCKED

    async def _handle_coordinator_failure(
        self, run_id: str, mission_id: str, budgets: dict, error: str,
        exc: Exception | None = None,
    ) -> IterationOutcome:
        """Record a coordinator failure. PERMANENT (config/auth/executable) errors block after ONE
        attempt and never burn the failure budget; TRANSIENT errors increment the bounded failure
        streak and are spaced by exponential backoff (beta.9 Defect 3)."""
        from aithernet.missions.failure_classification import (
            PERMANENT,
            QUOTA,
            category,
            disposition_for,
        )
        cat = category(exc, error)
        disp = disposition_for(cat)
        if disp == PERMANENT:
            return await self._handle_permanent_block(
                run_id, mission_id, error, failure_category=cat)
        if disp == QUOTA:
            # beta.10 Defect 4: quota exhaustion is NOT a generic budget failure. Park the mission
            # in a recoverable quota state WITHOUT burning the consecutive-failure budget; name the
            # exhausted provider and whether a fallback coordinator is configured.
            return await self._handle_quota_block(run_id, mission_id, error)
        run = self._get_run(run_id)
        failures = run.consecutive_failures + 1
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="blocked",
            status=MissionStepStatus.FAILED.value,
            reason=f"coordinator_error[{cat}]: {error}",
            result_extra={"failure_type": "transient", "failure_category": cat},
        )
        self._update_run(
            run_id,
            coordinator_call_count=run.coordinator_call_count + 1,
            consecutive_failures=failures,
            last_activity_at=utcnow(),
        )
        # If the failure streak is now exhausted, block immediately rather than spinning.
        if failures >= budgets.get("max_consecutive_failures", 1):
            return await self._handle_budget_block(
                run_id,
                mission_id,
                f"{REASON_BUDGET_EXHAUSTED}: max_consecutive_failures "
                f"({budgets.get('max_consecutive_failures')}) reached after {cat}",
            )
        # Bounded exponential backoff before the next attempt (NOT three immediate identical
        # retries). The heartbeat renews the lease concurrently, so a long backoff never drops it.
        await self._coordinator_retry_backoff(budgets, failures, mission_id, cat)
        return IterationOutcome.CONTINUED

    async def _coordinator_retry_backoff(
        self, budgets: dict, failures: int, mission_id: str, cat: str
    ) -> None:
        """Sleep an exponentially-growing, capped delay between transient coordinator retries."""
        import asyncio

        initial = float(budgets.get("coordinator_retry_initial_backoff_seconds", 2.0))
        cap = float(budgets.get("coordinator_retry_max_backoff_seconds", 30.0))
        if initial <= 0 or cap <= 0:
            return
        delay = min(initial * (2 ** (failures - 1)), cap)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _handle_invalid_decision(
        self,
        run_id: str,
        mission_id: str,
        budgets: dict,
        decision: CoordinatorDecision,
        error: str,
    ) -> IterationOutcome:
        run = self._get_run(run_id)
        failures = run.consecutive_failures + 1
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="blocked",
            status=MissionStepStatus.FAILED.value,
            reason=f"invalid_decision: {error}",
            decision=decision,
        )
        self._update_run(
            run_id,
            coordinator_call_count=run.coordinator_call_count + 1,
            last_decision_id=decision.decision_id,
            consecutive_failures=failures,
            last_activity_at=utcnow(),
        )
        if failures >= budgets.get("max_consecutive_failures", 1):
            return await self._handle_budget_block(
                run_id,
                mission_id,
                f"{REASON_BUDGET_EXHAUSTED}: max_consecutive_failures "
                f"({budgets.get('max_consecutive_failures')}) reached",
            )
        return IterationOutcome.CONTINUED

    async def _handle_pause(self, run_id: str, mission_id: str) -> IterationOutcome:
        try:
            self._apply_transition(
                run_id, mission_id, MissionStatus.PAUSED, run_fields={"paused_at": utcnow()}
            )
        except MissionTransitionError:
            return IterationOutcome.STOPPED
        await self.runtime._emit_event(
            event_type=ev.EVENT_PAUSED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission {mission_id} paused at an atomic boundary.",
            payload={"run_id": run_id},
        )
        return IterationOutcome.PAUSED

    async def _handle_cancel(self, run_id: str, mission_id: str) -> IterationOutcome:
        now = utcnow()
        # Acknowledge first so no coordinator call can follow.
        self._update_run(run_id, cancel_acknowledged_at=now, last_activity_at=now)
        self._persist_control_step(
            mission_id,
            run_id,
            disposition="cancelled",
            status=MissionStepStatus.COMPLETED.value,
            reason="cancellation acknowledged",
        )
        try:
            self._apply_transition(
                run_id, mission_id, MissionStatus.CANCELLED, run_fields={"cancelled_at": now}
            )
        except MissionTransitionError:
            return IterationOutcome.STOPPED
        # Stage 13B: cancelling a mission cancels its pending reply waits (no later resume).
        comms = getattr(self.runtime, "comms", None)
        if comms is not None:
            with contextlib.suppress(Exception):
                comms.cancel_mission_waits(mission_id)
        await self.runtime._emit_event(
            event_type=ev.EVENT_CANCELLED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission {mission_id} cancelled.",
            payload={"run_id": run_id},
        )
        return IterationOutcome.CANCELLED


def mission_read_status(mission: MissionRead) -> MissionStatus:
    """Helper: a mission's status as a :class:`MissionStatus` (tolerates raw strings)."""
    return mission.status if isinstance(mission.status, MissionStatus) else MissionStatus(
        mission.status
    )
