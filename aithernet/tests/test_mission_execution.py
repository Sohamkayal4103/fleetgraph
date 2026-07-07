"""Stage 12 — autonomous mission execution loop tests.

Every test runs against an isolated temp SQLite database with deterministic, network-free
fakes (no Gemini/Codex/GNU Radio/gr-mcp). The background worker is disabled by default so
iterations are driven synchronously and deterministically; the few tests that exercise the
real background poll loop start it explicitly and poll for the terminal state.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
import uvicorn
from fastapi.testclient import TestClient

from aithernet.config.settings import (
    CoordinatorConfig,
    MissionBudgets,
    MissionExecutionConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import (
    CoordinatorDecision,
    CoordinatorProviderError,
    MissionControlDisposition,
    validate_mission_control,
)
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.missions import budgets as budget_mod
from aithernet.missions.context import build_mission_run_context
from aithernet.missions.lifecycle import (
    MissionTransitionError,
    can_transition,
    is_terminal,
    validate_transition,
)
from aithernet.missions.worker import MissionConflictError, MissionWorkerManager
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.missions import (
    TERMINAL_MISSION_STATES,
    MissionCreate,
    MissionStatus,
)
from aithernet.state.models import utcnow
from aithernet.state.repositories import MissionExecutionRunRepository

TERMINAL_RUN = {"completed", "blocked", "failed", "cancelled"}


# -- deterministic coordinator providers -----------------------------------------


def _decision(disposition="continue", *, target="respond", **mc):
    fields = {
        "summary": f"step-{disposition}",
        "next_target": target,
        "action": "act",
        "message": "msg",
        "structured_payload": {},
        "expected_result": "ok",
        "mission_control": {"disposition": disposition, **mc},
    }
    return fields


class SequencedProvider(CoordinatorProvider):
    """Yields scripted decision dicts in order; repeats the last one when exhausted."""

    name = "sequenced"

    def __init__(self, sequence: list[dict]) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self.sequence = sequence
        self.calls = 0

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload) -> CoordinatorDecision:
        fields = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return CoordinatorDecision.from_model_json(fields, mission_id=payload.mission_id)


class FlakyThenCompleteProvider(CoordinatorProvider):
    """Raises a provider error ``fail_times`` times, then completes."""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self.fail_times = fail_times
        self.calls = 0

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload) -> CoordinatorDecision:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise CoordinatorProviderError("simulated provider failure")
        return CoordinatorDecision.from_model_json(
            _decision("complete", final_response="done"), mission_id=payload.mission_id
        )


# -- runtime/worker construction helpers -----------------------------------------


def _config(tmp_path, *, enabled=False, budgets=None, lease=30, renew=10):
    # Disable the beta.9 transient-retry backoff so these deterministic tests never sleep.
    base = (budgets or MissionBudgets()).model_copy(
        update={"coordinator_retry_initial_backoff_seconds": 0,
                "coordinator_retry_max_backoff_seconds": 0})
    me = MissionExecutionConfig(
        enabled=enabled,
        lease_duration_seconds=lease,
        lease_renew_interval_seconds=renew,
        poll_interval_seconds=0.1,
        default_budgets=base,
    )
    return NodeConfig(
        node_id="test-node",
        node_name="t",
        database_url=f"sqlite:///{tmp_path / 'exec.db'}",
        mission_execution=me,
    )


def _runtime(tmp_path, sequence, **kw):
    config = _config(tmp_path, **kw)
    provider = SequencedProvider(sequence)
    return NodeRuntime.from_config(
        config, coordinator=CoordinatorRuntime(CoordinatorConfig(provider="sequenced"), provider)
    )


def _runtime_with_provider(tmp_path, provider, **kw):
    config = _config(tmp_path, **kw)
    return NodeRuntime.from_config(
        config, coordinator=CoordinatorRuntime(CoordinatorConfig(provider=provider.name), provider)
    )


async def _new_mission(runtime, content="do the thing"):
    mission = await runtime.create_mission(MissionCreate(content=content, source_type="user"))
    return mission


async def _drive(mgr: MissionWorkerManager, mission_id, max_iters=40):
    """Drive the loop synchronously via run_one_step until terminal; return run statuses."""
    statuses = []
    for _ in range(max_iters):
        status = await mgr.run_one_step(mission_id)
        statuses.append(status.run.status)
        if status.run.status in TERMINAL_RUN:
            break
    return statuses


def _run(coro):
    return asyncio.run(coro)


@contextmanager
def _serve(app):
    """Run ``app`` on a real uvicorn server in a background thread; yield its base URL."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Live server failed to start in time.")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ================================================================================
# Lifecycle transition machine
# ================================================================================


def test_terminal_states_frozen():
    assert TERMINAL_MISSION_STATES == frozenset(
        {
            MissionStatus.COMPLETED,
            MissionStatus.BLOCKED,
            MissionStatus.FAILED,
            MissionStatus.CANCELLED,
        }
    )


@pytest.mark.parametrize(
    "src,dst",
    [
        (MissionStatus.RECEIVED, MissionStatus.QUEUED),
        (MissionStatus.QUEUED, MissionStatus.ACTIVE),
        (MissionStatus.ACTIVE, MissionStatus.WAITING),
        (MissionStatus.ACTIVE, MissionStatus.COMPLETED),
        (MissionStatus.ACTIVE, MissionStatus.BLOCKED),
        (MissionStatus.ACTIVE, MissionStatus.PAUSED),
        (MissionStatus.WAITING, MissionStatus.QUEUED),
        (MissionStatus.PAUSED, MissionStatus.QUEUED),
        (MissionStatus.ACTIVE, MissionStatus.QUEUED),
    ],
)
def test_allowed_transitions(src, dst):
    validate_transition(src, dst)  # no raise
    assert can_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        (MissionStatus.COMPLETED, MissionStatus.ACTIVE),
        (MissionStatus.FAILED, MissionStatus.QUEUED),
        (MissionStatus.CANCELLED, MissionStatus.ACTIVE),
        (MissionStatus.BLOCKED, MissionStatus.ACTIVE),
        (MissionStatus.RECEIVED, MissionStatus.COMPLETED),
        (MissionStatus.QUEUED, MissionStatus.COMPLETED),
    ],
)
def test_forbidden_transitions(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(MissionTransitionError):
        validate_transition(src, dst)


def test_idempotent_same_state_allowed():
    for s in MissionStatus:
        assert can_transition(s, s)


def test_is_terminal():
    assert is_terminal(MissionStatus.COMPLETED)
    assert is_terminal(MissionStatus.CANCELLED)
    assert not is_terminal(MissionStatus.ACTIVE)
    assert not is_terminal(MissionStatus.WAITING)


# ================================================================================
# Budgets
# ================================================================================


def test_resolve_budgets_defaults():
    base = MissionBudgets()
    resolved = budget_mod.resolve_budgets(base, None)
    assert resolved["max_iterations"] == base.max_iterations


def test_resolve_budgets_override_clamped_to_max():
    base = MissionBudgets(max_iterations=10)
    # An override above the max is clamped down; below the max is honored.
    assert budget_mod.resolve_budgets(base, {"max_iterations": 99})["max_iterations"] == 10
    assert budget_mod.resolve_budgets(base, {"max_iterations": 4})["max_iterations"] == 4


def test_resolve_budgets_ignores_unknown_and_nonpositive():
    base = MissionBudgets(max_iterations=10)
    out = budget_mod.resolve_budgets(base, {"unknown": 5, "max_iterations": 0})
    assert "unknown" not in out
    assert out["max_iterations"] == 10  # 0 ignored


class _FakeRun:
    def __init__(self, **kw):
        self.iteration_count = kw.get("iteration_count", 0)
        self.coordinator_call_count = kw.get("coordinator_call_count", 0)
        self.elapsed_seconds = kw.get("elapsed_seconds", 0.0)
        self.consecutive_failures = kw.get("consecutive_failures", 0)
        self.node_state_action_count = kw.get("node_state_action_count", 0)
        self.coding_agent_action_count = kw.get("coding_agent_action_count", 0)
        self.mcp_action_count = kw.get("mcp_action_count", 0)
        self.response_action_count = kw.get("response_action_count", 0)


def test_iteration_block_reason_iterations():
    b = budget_mod.resolve_budgets(MissionBudgets(max_iterations=3), None)
    assert budget_mod.iteration_block_reason(_FakeRun(iteration_count=3), b) is not None
    assert budget_mod.iteration_block_reason(_FakeRun(iteration_count=2), b) is None


def test_iteration_block_reason_consecutive_failures():
    b = budget_mod.resolve_budgets(MissionBudgets(max_consecutive_failures=2), None)
    reason = budget_mod.iteration_block_reason(_FakeRun(consecutive_failures=2), b)
    assert reason is not None and "max_consecutive_failures" in reason


def test_iteration_block_reason_elapsed_and_calls():
    b = budget_mod.resolve_budgets(
        MissionBudgets(max_elapsed_seconds=10, max_coordinator_calls=2), None
    )
    assert budget_mod.iteration_block_reason(_FakeRun(elapsed_seconds=10.0), b) is not None
    assert budget_mod.iteration_block_reason(_FakeRun(coordinator_call_count=2), b) is not None


def test_route_block_reason():
    b = budget_mod.resolve_budgets(MissionBudgets(max_response_actions=1), None)
    blocked = budget_mod.route_block_reason(_FakeRun(response_action_count=1), b, "respond")
    assert blocked is not None
    assert budget_mod.route_block_reason(_FakeRun(response_action_count=0), b, "respond") is None
    # An unknown/control target has no per-route budget.
    assert budget_mod.route_block_reason(_FakeRun(), b, "mission_control") is None


def test_remaining_never_negative():
    b = budget_mod.resolve_budgets(MissionBudgets(max_iterations=2), None)
    rem = budget_mod.remaining(_FakeRun(iteration_count=5), b)
    assert rem["iterations"] == 0
    assert all(v >= 0 for v in rem.values())


# ================================================================================
# mission_control validation
# ================================================================================


def _build_decision(fields):
    return CoordinatorDecision.from_model_json(fields, mission_id="m1")


def test_validate_continue_ok():
    d = _build_decision(_decision("continue", target="respond"))
    assert validate_mission_control(d) is None


def test_validate_continue_requires_target():
    d = _build_decision(_decision("continue", target=""))
    assert validate_mission_control(d) is not None


def test_validate_complete_requires_final_response():
    assert validate_mission_control(_build_decision(_decision("complete"))) is not None
    assert (
        validate_mission_control(_build_decision(_decision("complete", final_response="x"))) is None
    )


def test_validate_wait_requires_reason():
    assert validate_mission_control(_build_decision(_decision("wait"))) is not None
    ok = _build_decision(_decision("wait", waiting={"reason": "for input"}))
    assert validate_mission_control(ok) is None


def test_validate_blocked_requires_reason():
    assert validate_mission_control(_build_decision(_decision("blocked"))) is not None
    ok = _build_decision(_decision("blocked", blocked={"reason": "no tool"}))
    assert validate_mission_control(ok) is None


def test_decision_defaults_to_continue_when_mission_control_absent():
    fields = {
        "summary": "s",
        "next_target": "respond",
        "action": "a",
        "message": "m",
        "structured_payload": {},
        "expected_result": "e",
    }
    d = _build_decision(fields)
    assert d.mission_control.disposition is MissionControlDisposition.CONTINUE


# ================================================================================
# DB-backed leasing
# ================================================================================


def _make_run(runtime, mission_id):
    with runtime.session_scope() as session:
        run = MissionExecutionRunRepository(session).create(
            mission_id=mission_id, node_id=runtime.config.node_id, budgets={}
        )
        session.commit()
        return run.id


def test_acquire_lease_atomic_single_winner(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_id = _make_run(runtime, mission.id)
    now = utcnow()
    expiry = now + timedelta(seconds=30)
    with runtime.session_scope() as s:
        rc1 = MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w1", token="t1", now=now, expiry=expiry
        )
        s.commit()
    # A second acquire on the now-active, unexpired lease fails.
    with runtime.session_scope() as s:
        rc2 = MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w2", token="t2", now=now, expiry=expiry
        )
        s.commit()
    assert rc1 == 1
    assert rc2 == 0


def test_renew_requires_matching_token(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_id = _make_run(runtime, mission.id)
    now = utcnow()
    exp = now + timedelta(seconds=30)
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w1", token="t1", now=now, expiry=exp
        )
        s.commit()
    with runtime.session_scope() as s:
        good = MissionExecutionRunRepository(s).renew_lease(
            run_id, owner_id="w1", token="t1", now=now, expiry=exp
        )
        bad = MissionExecutionRunRepository(s).renew_lease(
            run_id, owner_id="w1", token="WRONG", now=now, expiry=exp
        )
        s.commit()
    assert good == 1
    assert bad == 0


def test_expired_lease_is_recoverable(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_id = _make_run(runtime, mission.id)
    past = utcnow() - timedelta(seconds=60)
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w1", token="t1", now=past, expiry=past
        )
        s.commit()
    with runtime.session_scope() as s:
        ids = MissionExecutionRunRepository(s).recoverable_active_ids(
            runtime.config.node_id, utcnow()
        )
    assert run_id in ids
    # And a fresh worker can re-acquire the expired lease.
    with runtime.session_scope() as s:
        rc = MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w2", token="t2", now=utcnow(),
            expiry=utcnow() + timedelta(seconds=30),
        )
        s.commit()
    assert rc == 1


def test_release_requires_token(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_id = _make_run(runtime, mission.id)
    now = utcnow()
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="w1", token="t1", now=now, expiry=now + timedelta(seconds=30)
        )
        s.commit()
    with runtime.session_scope() as s:
        assert MissionExecutionRunRepository(s).release_lease(run_id, token="WRONG") == 0
        assert MissionExecutionRunRepository(s).release_lease(run_id, token="t1") == 1
        s.commit()


def test_run_read_never_exposes_lease_token(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    # The Read schema has no lease_token field at all.
    assert "lease_token" not in run_read.model_dump()


# ================================================================================
# Engine atomic iteration
# ================================================================================


def test_continue_then_complete(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="all done")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "completed"
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.final_response == "all done"
    # Exactly one action step + one control step, in order.
    steps = list(reversed(runtime.list_mission_steps(mission_id=mission.id)))
    assert [s.route_target for s in steps] == ["respond", "mission_control"]


def test_complete_immediately(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="instant")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses == ["completed"]
    assert runtime.get_mission(mission.id).status is MissionStatus.COMPLETED


def test_wait_disposition(tmp_path):
    runtime = _runtime(
        tmp_path, [_decision("wait", waiting={"reason": "awaiting operator input"})]
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.status == "waiting"
    assert execution.mission.status is MissionStatus.WAITING
    assert execution.run.waiting["reason"] == "awaiting operator input"


def test_blocked_disposition(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("blocked", blocked={"reason": "needs a tool", "missing_capability": "x"})],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.status == "blocked"
    assert "needs a tool" in execution.run.blocked_reason


def test_one_route_per_action_step(tmp_path):
    # node_state then complete — verify exactly one route executed per step.
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="node_state"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.node_state_action_count == 1
    assert execution.run.response_action_count == 0


def test_iteration_budget_exhaustion_blocks_not_completes(tmp_path):
    # Never completes (always continue) with max_iterations=2 -> blocked on exhaustion.
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond")],
        budgets=MissionBudgets(max_iterations=2),
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "blocked"
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.status == "blocked"
    assert budget_mod.REASON_BUDGET_EXHAUSTED in (execution.run.blocked_reason or "")
    # Budget exhaustion must NOT mark completed.
    assert execution.mission.status is MissionStatus.BLOCKED


def test_response_route_budget_exhaustion(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond")],
        budgets=MissionBudgets(max_response_actions=1, max_iterations=10),
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "blocked"


def test_coordinator_failures_count_toward_consecutive(tmp_path):
    provider = FlakyThenCompleteProvider(fail_times=5)
    runtime = _runtime_with_provider(
        tmp_path, provider, budgets=MissionBudgets(max_consecutive_failures=3)
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    # 3 consecutive coordinator failures -> blocked before reaching the completing decision.
    assert statuses[-1] == "blocked"


def test_coordinator_recovers_before_limit(tmp_path):
    provider = FlakyThenCompleteProvider(fail_times=2)
    runtime = _runtime_with_provider(
        tmp_path, provider, budgets=MissionBudgets(max_consecutive_failures=5)
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "completed"


def test_invalid_decision_persisted_as_failure(tmp_path):
    # complete with no final_response is invalid -> failure step; with max=1 -> blocked.
    runtime = _runtime(
        tmp_path, [_decision("complete")], budgets=MissionBudgets(max_consecutive_failures=1)
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "blocked"
    steps = runtime.list_mission_steps(mission_id=mission.id)
    assert any(s.status.value == "failed" for s in steps)


def test_failed_route_increments_failures_and_no_retry(tmp_path):
    # coding_agent unconfigured -> blocked route; verify consecutive_failures increments and
    # the same action is never auto-retried (the next coordinator call is a NEW decision).
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="coding_agent"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    # First step: coding_agent route blocked (unconfigured).
    _run(runtime.worker_manager.run_one_step(mission.id))
    after_first = runtime.mission_execution_status(mission.id)
    assert after_first.run.consecutive_failures == 1
    # Second step completes (reassessed); failures reset.
    _run(runtime.worker_manager.run_one_step(mission.id))
    final = runtime.mission_execution_status(mission.id)
    assert final.run.status == "completed"


# ================================================================================
# Worker operations: enqueue / conflict / pause / resume / cancel / run-one-step
# ================================================================================


def test_enqueue_idempotent(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run1, created1 = _run(runtime.worker_manager.enqueue_mission(mission.id))
    run2, created2 = _run(runtime.worker_manager.enqueue_mission(mission.id))
    assert created1 is True
    assert created2 is False
    assert run1.id == run2.id


def test_start_terminal_mission_conflict(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    # Mission is completed; starting a fresh run conflicts.
    with pytest.raises(MissionConflictError):
        _run(runtime.worker_manager.enqueue_mission(mission.id))


def test_pause_then_resume(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    # Pause while queued (no task running) -> paused directly.
    _run(runtime.worker_manager.pause_mission(mission.id))
    assert runtime.mission_execution_status(mission.id).run.status == "paused"
    # Resume -> requeued, then drive to completion (steps preserved, no replay).
    _run(runtime.worker_manager.resume_mission(mission.id))
    assert runtime.mission_execution_status(mission.id).run.status == "queued"
    statuses = _run(_drive(runtime.worker_manager, mission.id))
    assert statuses[-1] == "completed"


def test_resume_requires_paused_or_waiting(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    with pytest.raises(MissionConflictError):
        _run(runtime.worker_manager.resume_mission(mission.id))


def test_cancel_queued_directly(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.cancel_mission(mission.id))
    execution = runtime.mission_execution_status(mission.id)
    assert execution.run.status == "cancelled"
    assert execution.run.cancel_acknowledged_at is not None
    assert execution.mission.status is MissionStatus.CANCELLED


def test_cancel_no_active_run_conflict(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    with pytest.raises(MissionConflictError):
        _run(runtime.worker_manager.cancel_mission(mission.id))


def test_run_one_step_no_background_loop(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    status = _run(runtime.worker_manager.run_one_step(mission.id))
    # Exactly one iteration ran; not yet complete (it continued).
    assert status.run.iteration_count == 1
    assert status.run.status == "active"


def test_waiting_does_not_consume_worker_slot(tmp_path):
    runtime = _runtime(tmp_path, [_decision("wait", waiting={"reason": "later"})])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    # A waiting run is not in the active task map.
    assert mission.id not in runtime.worker_manager._tasks


# ================================================================================
# Recovery after restart
# ================================================================================


def test_recovery_requeues_interrupted_run(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    run_id = run_read.id
    # Simulate an interrupted active run with an expired lease + a running step.
    past = utcnow() - timedelta(seconds=120)
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_id, owner_id="dead", token="t", now=past, expiry=past
        )
        s.commit()
    from aithernet.state.repositories import MissionStepRepository

    with runtime.session_scope() as s:
        step = MissionStepRepository(s).create(
            mission_id=mission.id,
            decision_id="d1",
            decision={"summary": "x"},
            route_target="respond",
            status="running",
            started_at=past,
        )
        s.commit()
        step_id = step.id

    _run(runtime.worker_manager.recover())

    after = runtime.mission_execution_status(mission.id)
    assert after.run.status == "queued"
    assert after.run.recovery_count == 1
    # The interrupted running step is marked failed/interrupted, never assumed completed.
    step = runtime.get_mission_step(step_id)
    assert step.status.value == "failed"
    assert "Interrupted" in (step.error or "")


def test_recovery_preserves_terminal_runs(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    _run(runtime.worker_manager.recover())
    # A completed mission stays completed after recovery.
    assert runtime.get_mission(mission.id).status is MissionStatus.COMPLETED


# ================================================================================
# Context assembler
# ================================================================================


def test_context_includes_budgets_and_objective(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime, content="survey 2.4GHz"))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    with runtime.session_scope() as s:
        run = MissionExecutionRunRepository(s).get(run_read.id)
        budgets = budget_mod.resolve_budgets(runtime.config.mission_execution.default_budgets, {})
        ctx = build_mission_run_context(
            runtime,
            mission=runtime.get_mission(mission.id),
            run=run,
            budgets=budgets,
            limits=runtime.config.mission_execution.context,
        )
    assert "survey 2.4GHz" in ctx["objective"]
    assert "remaining_budgets" in ctx
    assert ctx["run"]["run_id"] == run_read.id


def test_context_marks_truncation(tmp_path):
    from aithernet.config.settings import MissionContextLimits

    runtime = _runtime(tmp_path, [_decision("continue", target="respond")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    # Produce several steps.
    for _ in range(3):
        _run(runtime.worker_manager.run_one_step(mission.id))
    with runtime.session_scope() as s:
        run = MissionExecutionRunRepository(s).current_for_mission(mission.id)
        budgets = budget_mod.resolve_budgets(runtime.config.mission_execution.default_budgets, {})
        ctx = build_mission_run_context(
            runtime,
            mission=runtime.get_mission(mission.id),
            run=run,
            budgets=budgets,
            limits=MissionContextLimits(recent_steps=1),
        )
    assert ctx.get("_truncation", {}).get("recent_steps") is True


def test_context_redacts_secrets(tmp_path):
    from aithernet.config.settings import MissionContextLimits

    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime, content="token=sk-abcdefgh1234 secret"))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    with runtime.session_scope() as s:
        run = MissionExecutionRunRepository(s).get(run_read.id)
        ctx = build_mission_run_context(
            runtime,
            mission=runtime.get_mission(mission.id),
            run=run,
            budgets={},
            limits=MissionContextLimits(),
        )
    assert "sk-abcdefgh1234" not in ctx["objective"]


# ================================================================================
# Background worker (real poll loop)
# ================================================================================


def test_background_worker_completes(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="bg done")],
        enabled=True,
    )

    async def scenario():
        mission = await _new_mission(runtime)
        await runtime.worker_manager.start()
        await runtime.worker_manager.enqueue_mission(mission.id)
        for _ in range(60):
            await asyncio.sleep(0.1)
            st = runtime.mission_execution_status(mission.id)
            if st.run and st.run.status in TERMINAL_RUN:
                break
        await runtime.worker_manager.shutdown()
        return runtime.mission_execution_status(mission.id)

    execution = _run(scenario())
    assert execution.run.status == "completed"
    assert execution.run.final_response == "bg done"


def test_disabled_worker_does_not_start(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")], enabled=False)
    _run(runtime.worker_manager.start())
    assert runtime.worker_manager.worker_status().running is False


# ================================================================================
# API endpoints
# ================================================================================


@pytest.fixture()
def exec_client(tmp_path):
    """A TestClient with a sequenced coordinator and the background worker DISABLED.

    Disabling the loop keeps API tests deterministic — iterations are driven via the
    synchronous run-step endpoint.
    """
    from aithernet.api.app import create_app

    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="api done")],
        enabled=False,
    )
    with TestClient(create_app(runtime=runtime)) as c:
        yield c


def _create_mission(client):
    return client.post("/missions", json={"content": "api mission"}).json()["id"]


def test_api_start_returns_queued(exec_client):
    mid = _create_mission(exec_client)
    r = exec_client.post(f"/missions/{mid}/start")
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] is True
    assert body["run"]["status"] == "queued"
    assert "lease_token" not in body["run"]


def test_api_start_idempotent(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    r2 = exec_client.post(f"/missions/{mid}/start")
    assert r2.status_code == 200
    assert "already had an active run" in r2.json()["detail"]


def test_api_run_step_and_execution(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    step1 = exec_client.post(f"/missions/{mid}/run-step")
    assert step1.status_code == 200
    assert step1.json()["run"]["iteration_count"] == 1
    exec_client.post(f"/missions/{mid}/run-step")
    execution = exec_client.get(f"/missions/{mid}/execution").json()
    assert execution["run"]["status"] == "completed"
    assert execution["run"]["final_response"] == "api done"


def test_api_execution_404(exec_client):
    assert exec_client.get("/missions/nope/execution").status_code == 404


def test_api_timeline(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    exec_client.post(f"/missions/{mid}/run-step")
    timeline = exec_client.get(f"/missions/{mid}/timeline").json()
    assert timeline["mission_id"] == mid
    assert len(timeline["entries"]) >= 1


def test_api_pause_resume(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    paused = exec_client.post(f"/missions/{mid}/pause").json()
    assert paused["status"] == "paused"
    resumed = exec_client.post(f"/missions/{mid}/resume").json()
    assert resumed["status"] == "queued"


def test_api_cancel(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    cancelled = exec_client.post(f"/missions/{mid}/cancel").json()
    assert cancelled["status"] == "cancelled"


def test_api_cancel_no_run_conflict(exec_client):
    mid = _create_mission(exec_client)
    r = exec_client.post(f"/missions/{mid}/cancel")
    assert r.status_code == 409


def test_api_mission_runs_list(exec_client):
    mid = _create_mission(exec_client)
    exec_client.post(f"/missions/{mid}/start")
    runs = exec_client.get("/mission-runs").json()
    assert len(runs) == 1
    assert "lease_token" not in runs[0]
    run = exec_client.get(f"/mission-runs/{runs[0]['id']}").json()
    assert run["id"] == runs[0]["id"]


def test_api_mission_run_404(exec_client):
    assert exec_client.get("/mission-runs/nope").status_code == 404


def test_api_worker_status(exec_client):
    status = exec_client.get("/mission-worker/status").json()
    assert status["enabled"] is False
    assert "node_id" in status
    assert status["worker_count"] == 1


def test_api_manual_step_still_works_and_does_not_loop(exec_client):
    # Regression: the legacy manual /step endpoint runs exactly one step (no autonomous loop).
    mid = _create_mission(exec_client)
    r = exec_client.post(f"/missions/{mid}/step")
    assert r.status_code == 200
    body = r.json()
    assert "decision" in body and "step" in body
    # It did not create an execution run.
    execution = exec_client.get(f"/missions/{mid}/execution").json()
    assert execution["run"] is None


# ================================================================================
# CLI
# ================================================================================


def test_cli_worker_status(tmp_path):
    from typer.testing import CliRunner

    from aithernet.api.app import create_app
    from aithernet.cli import app as cli_app

    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")], enabled=False)
    with _serve(create_app(runtime=runtime)) as base_url:
        result = CliRunner().invoke(cli_app, ["worker", "status", "--url", base_url])
    assert result.exit_code == 0
    assert "Worker" in result.stdout


def test_cli_mission_start_and_execution(tmp_path):
    from typer.testing import CliRunner

    from aithernet.api.app import create_app
    from aithernet.cli import app as cli_app

    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="cli done")],
        enabled=False,
    )
    runner = CliRunner()
    with _serve(create_app(runtime=runtime)) as base_url:
        mission = _run(_new_mission(runtime))
        start = runner.invoke(cli_app, ["mission", "start", mission.id, "--url", base_url])
        assert start.exit_code == 0
        execution = runner.invoke(
            cli_app, ["mission", "execution", mission.id, "--url", base_url]
        )
        assert execution.exit_code == 0
        assert "status=queued" in execution.stdout


# ================================================================================
# Emitted events (persisted mission-audit events)
# ================================================================================


def _event_types(runtime, mission_id):
    return {e.event_type for e in runtime.list_events(mission_id=mission_id, limit=200)}


def test_events_run_created_and_queued(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    types = _event_types(runtime, mission.id)
    assert "mission.run.created" in types
    assert "mission.queued" in types


def test_events_lease_and_completion(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    types = _event_types(runtime, mission.id)
    assert "mission.lease.acquired" in types
    assert "mission.completed" in types
    assert "mission.decision.completed" in types


def test_events_action_completed(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    types = _event_types(runtime, mission.id)
    assert "mission.action.started" in types
    assert "mission.action.completed" in types
    assert "mission.iteration.started" in types


def test_events_budget_exhausted(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond")],
        budgets=MissionBudgets(max_iterations=1),
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    types = _event_types(runtime, mission.id)
    assert "mission.budget.exhausted" in types
    assert "mission.blocked" in types


def test_events_waiting(tmp_path):
    runtime = _runtime(tmp_path, [_decision("wait", waiting={"reason": "later"})])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    assert "mission.waiting" in _event_types(runtime, mission.id)


def test_events_cancel(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.cancel_mission(mission.id))
    types = _event_types(runtime, mission.id)
    assert "mission.cancel.requested" in types
    assert "mission.cancelled" in types


def test_events_recovered(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    past = utcnow() - timedelta(seconds=120)
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_read.id, owner_id="dead", token="t", now=past, expiry=past
        )
        s.commit()
    _run(runtime.worker_manager.recover())
    assert "mission.recovered" in _event_types(runtime, mission.id)


def test_no_lease_token_in_events(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    for e in runtime.list_events(mission_id=mission.id, limit=200):
        assert "lease_token" not in (e.payload or {})


# ================================================================================
# Invariants
# ================================================================================


def test_one_step_persisted_per_iteration(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    before = len(runtime.list_mission_steps(mission_id=mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    after = len(runtime.list_mission_steps(mission_id=mission.id))
    assert after - before == 1


def test_completed_run_not_replayed(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(_drive(runtime.worker_manager, mission.id))
    steps_before = len(runtime.list_mission_steps(mission_id=mission.id))
    # A further run_iteration on the terminal run is a no-op (STOPPED), persists nothing.
    from aithernet.missions.engine import IterationOutcome as IO

    run = runtime.worker_manager._current_run(mission.id)
    # current_for_mission returns None for terminal -> use latest
    latest = runtime.worker_manager._latest_run(mission.id)
    out = _run(runtime.worker_manager.engine.run_iteration(latest.id))
    assert out is IO.STOPPED
    assert len(runtime.list_mission_steps(mission_id=mission.id)) == steps_before
    assert run is None


def test_cancel_ack_prevents_future_coordinator_calls(tmp_path):
    # A run with cancel requested acknowledges + cancels on the next iteration; the
    # coordinator is never called again afterward.
    runtime = _runtime(tmp_path, [_decision("continue", target="respond")])
    mission = _run(_new_mission(runtime))
    run_read, _ = _run(runtime.worker_manager.enqueue_mission(mission.id))
    # Request cancel while queued -> cancelled directly.
    _run(runtime.worker_manager.cancel_mission(mission.id))
    calls_after = runtime.coordinator.provider.calls
    assert runtime.mission_execution_status(mission.id).run.status == "cancelled"
    # Coordinator was never invoked for this mission.
    assert calls_after == 0


def test_result_persisted_before_reassessment(tmp_path):
    # After an action step, the step result is persisted (completed) before the next
    # coordinator call would see it.
    runtime = _runtime(
        tmp_path,
        [_decision("continue", target="respond"), _decision("complete", final_response="d")],
    )
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    _run(runtime.worker_manager.run_one_step(mission.id))
    steps = runtime.list_mission_steps(mission_id=mission.id)
    assert steps[0].status.value == "completed"
    assert steps[0].completed_at is not None


def test_run_one_step_conflict_when_worker_holds_mission(tmp_path):
    runtime = _runtime(tmp_path, [_decision("continue", target="respond")])
    mission = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(mission.id))
    # Simulate the background worker holding this mission.
    runtime.worker_manager._tasks[mission.id] = object()  # type: ignore[assignment]
    with pytest.raises(MissionConflictError):
        _run(runtime.worker_manager.run_one_step(mission.id))
    runtime.worker_manager._tasks.pop(mission.id, None)


def test_worker_status_counts_queued(tmp_path):
    runtime = _runtime(tmp_path, [_decision("complete", final_response="x")])
    m1 = _run(_new_mission(runtime))
    m2 = _run(_new_mission(runtime))
    _run(runtime.worker_manager.enqueue_mission(m1.id))
    _run(runtime.worker_manager.enqueue_mission(m2.id))
    status = runtime.worker_manager.worker_status()
    assert status.queued_count == 2
