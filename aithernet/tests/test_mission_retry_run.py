"""beta.9 Defect 4: `aithernet mission retry` re-runs a blocked/failed mission with a FRESH run
after the cause is repaired, resetting the transient failure streak and never creating a duplicate
mission. Deterministic, network-free (a fake coordinator), backoff disabled for speed.
"""

from __future__ import annotations

import asyncio

import pytest

from aithernet.config.settings import (
    CoordinatorConfig,
    MissionBudgets,
    MissionExecutionConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import CoordinatorDecision, CoordinatorProviderError
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.missions.worker import MissionConflictError
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.missions import MissionCreate
from aithernet.state.repositories import MissionExecutionRunRepository, MissionRepository

TERMINAL = {"completed", "blocked", "failed", "cancelled"}


def _run(coro):
    return asyncio.run(coro)


def _complete(mid):
    return CoordinatorDecision.from_model_json(
        {"summary": "done", "next_target": "respond", "action": "act", "message": "m",
         "structured_payload": {}, "expected_result": "ok",
         "mission_control": {"disposition": "complete", "final_response": "done"}},
        mission_id=mid)


class RepairableProvider(CoordinatorProvider):
    """Raises a transient error until ``repaired`` is set, then completes — models a provider that
    is misconfigured/unreachable and then fixed by the customer."""

    name = "repairable"

    def __init__(self) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self.repaired = False
        self.calls = 0

    def check_ready(self):
        return []

    async def decide(self, payload):
        self.calls += 1
        if not self.repaired:
            raise CoordinatorProviderError("connection reset by peer")  # transient
        return _complete(payload.mission_id)


def _runtime(tmp_path, provider):
    me = MissionExecutionConfig(
        enabled=False, lease_duration_seconds=30, lease_renew_interval_seconds=10,
        poll_interval_seconds=0.1,
        default_budgets=MissionBudgets(
            max_consecutive_failures=2,
            coordinator_retry_initial_backoff_seconds=0,
            coordinator_retry_max_backoff_seconds=0))
    cfg = NodeConfig(node_id="t", node_name="t",
                     database_url=f"sqlite:///{tmp_path / 'r.db'}", mission_execution=me)
    return NodeRuntime.from_config(
        cfg, coordinator=CoordinatorRuntime(CoordinatorConfig(provider=provider.name), provider))


async def _drive(mgr, mission_id, max_iters=30):
    last = None
    for _ in range(max_iters):
        status = await mgr.run_one_step(mission_id)
        last = status.run.status
        if last in TERMINAL:
            break
    return last


def _new_mission(rt):
    return _run(rt.create_mission(MissionCreate(content="do the thing", source_type="user")))


def test_retry_reruns_blocked_without_duplicate_mission(tmp_path):
    prov = RepairableProvider()
    rt = _runtime(tmp_path, prov)
    mgr = rt.worker_manager
    mission = _new_mission(rt)
    first_run, _ = _run(mgr.enqueue_mission(mission.id))
    assert _run(_drive(mgr, mission.id)) == "blocked"

    # The customer "repairs" the provider, then retries — no new mission is created.
    prov.repaired = True
    retry_run, created = _run(mgr.retry_mission(mission.id))
    assert created is True
    assert retry_run.id != first_run.id          # a FRESH run
    with rt.session_scope() as s:
        assert len(MissionRepository(s).list()) == 1   # exactly one mission, never duplicated
        run_row = MissionExecutionRunRepository(s).get(retry_run.id)
        assert run_row.consecutive_failures == 0       # transient failure streak reset

    # The retried run now completes.
    assert _run(_drive(mgr, mission.id)) == "completed"


def test_retry_completed_mission_is_conflict(tmp_path):
    prov = RepairableProvider()
    prov.repaired = True
    rt = _runtime(tmp_path, prov)
    mgr = rt.worker_manager
    mission = _new_mission(rt)
    _run(mgr.enqueue_mission(mission.id))
    assert _run(_drive(mgr, mission.id)) == "completed"
    with pytest.raises(MissionConflictError):
        _run(mgr.retry_mission(mission.id))


def test_retry_running_mission_returns_existing_run(tmp_path):
    prov = RepairableProvider()
    prov.repaired = True
    rt = _runtime(tmp_path, prov)
    mgr = rt.worker_manager
    mission = _new_mission(rt)
    queued, _ = _run(mgr.enqueue_mission(mission.id))
    again, created = _run(mgr.retry_mission(mission.id))
    assert created is False          # idempotent: no duplicate run while one is live
    assert again.id == queued.id
