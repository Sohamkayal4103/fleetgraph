"""The autonomous mission worker manager (Stage 12, Parts E/D/L/M).

A single :class:`MissionWorkerManager` polls for queued execution runs, acquires a real
DB-backed lease for each, and drives :class:`MissionEngine` iterations in a bounded pool of
background tasks (default one). It owns lease heartbeats, pause/resume/cancel operations,
crash recovery on startup, and a clean shutdown that stops at atomic boundaries and leaves
no orphaned work. It NEVER fabricates results and never runs two tasks for one mission.

Polling/status reads never start a worker, execute a route, or call a model — only the
worker loop (or an explicit ``run_one_step``) does work.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.coordinator.contracts import MissionNotFoundError
from aithernet.missions import events as ev
from aithernet.missions.budgets import remaining as budget_remaining
from aithernet.missions.budgets import resolve_budgets
from aithernet.missions.engine import TERMINAL_OUTCOMES, MissionEngine
from aithernet.missions.lifecycle import MissionTransitionError
from aithernet.schemas.mission_runs import (
    MissionExecutionRunRead,
    MissionExecutionStatus,
    MissionWorkerStatus,
)
from aithernet.schemas.missions import (
    TERMINAL_MISSION_STATES,
    MissionRead,
    MissionStatus,
)
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    MissionExecutionRunRepository,
    MissionRepository,
    MissionStepRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

#: Emit a lease.renewed event only every Nth heartbeat to avoid flooding the event log.
_RENEW_EVENT_EVERY = 6


class MissionConflictError(Exception):
    """Raised when a mission operation conflicts with the current lifecycle state."""


class _RunState:
    """Mutable coordination flags for one in-flight run task."""

    __slots__ = ("stop", "lease_lost")

    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.lease_lost = False


class MissionWorkerManager:
    """Owns the autonomous worker pool, leasing, recovery, and run operations."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.mission_execution
        self.engine = MissionEngine(runtime)
        self.node_id = runtime.config.node_id
        # A stable owner identity for every lease this manager acquires.
        self.owner_id = f"worker-{runtime.config.node_id}-{new_uuid()[:8]}"
        self._tasks: dict[str, asyncio.Task] = {}  # mission_id -> run task
        self._states: dict[str, _RunState] = {}  # run_id -> state
        self._poll_task: asyncio.Task | None = None
        self._semaphore = asyncio.Semaphore(max(1, self.config.worker_count))
        self._stopping = False
        self._started = False
        self._degraded = False
        self._last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Recover interrupted runs, then start the background poll loop (idempotent)."""
        if self._started or not self.config.enabled:
            return
        self._started = True
        self._stopping = False
        try:
            await self.recover()
        except Exception as exc:  # recovery must never block startup
            self._degraded = True
            self._last_error = f"recovery_error: {type(exc).__name__}"
        self._poll_task = asyncio.create_task(self._poll_loop(), name="mission-worker-poll")
        # Worker lifecycle is operational telemetry (live stream only, not persisted audit).
        await self.runtime.publish_ephemeral_event(
            event_type=ev.EVENT_WORKER_STARTED,
            message=f"Mission worker started (pool={self.config.worker_count}).",
            payload={"node_id": self.node_id, "worker_count": self.config.worker_count},
        )

    async def shutdown(self) -> None:
        """Stop new work, let in-flight iterations finish at the atomic boundary, release leases."""
        if not self._started:
            return
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        # Signal every in-flight run to stop after its current atomic step.
        for state in list(self._states.values()):
            state.stop.set()
        if self._tasks:
            grace = max(1, self.config.shutdown_grace_seconds)
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._tasks.values()), timeout=grace)
        # Hard-cancel anything that overran the grace window (still an atomic boundary —
        # the run row stays active with a released lease, so recovery requeues it).
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._started = False
        await self.runtime.publish_ephemeral_event(
            event_type=ev.EVENT_WORKER_STOPPED,
            message="Mission worker stopped.",
            payload={"node_id": self.node_id},
        )

    # -- poll loop ---------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._degraded = True
                self._last_error = f"poll_error: {type(exc).__name__}"
                await self.runtime.publish_ephemeral_event(
                    event_type=ev.EVENT_WORKER_DEGRADED,
                    message=f"Mission worker poll error: {type(exc).__name__}.",
                    payload={"node_id": self.node_id},
                )
            await asyncio.sleep(max(0.05, self.config.poll_interval_seconds))

    async def _poll_once(self) -> None:
        """Pick up queued runs up to free pool capacity and launch tasks for them."""
        self._reap_finished()
        # Stage 13B: the existing mission-worker loop owns reply-wait timeouts too (no second
        # polling architecture). Due pending waits are timed out + their missions requeued once.
        comms = getattr(self.runtime, "comms", None)
        if comms is not None:
            with contextlib.suppress(Exception):
                await comms.sweep_timeouts()
        free = self._semaphore._value  # available pool slots
        if free <= 0:
            return
        with self.runtime.session_scope() as session:
            queued = MissionExecutionRunRepository(session).queued_run_ids(self.node_id, limit=free)
        for run_id in queued:
            with self.runtime.session_scope() as session:
                run = MissionExecutionRunRepository(session).get(run_id)
                mission_id = run.mission_id if run is not None else None
            if mission_id is None or mission_id in self._tasks:
                continue  # one task per mission
            self._tasks[mission_id] = asyncio.create_task(
                self._run_mission(run_id, mission_id), name=f"mission-run-{run_id}"
            )

    def _reap_finished(self) -> None:
        for mission_id, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(mission_id, None)

    # -- a single mission run task ------------------------------------------------

    async def _run_mission(self, run_id: str, mission_id: str) -> None:
        async with self._semaphore:
            token = await self._acquire(run_id, mission_id)
            if token is None:
                self._tasks.pop(mission_id, None)
                return
            state = _RunState()
            self._states[run_id] = state
            heartbeat = asyncio.create_task(self._heartbeat(run_id, mission_id, token, state))
            await self.runtime._emit_event(
                event_type=ev.EVENT_RUN_STARTED,
                mission_id=mission_id,
                source="mission_worker",
                message=f"Mission run {run_id} started.",
                payload={"run_id": run_id},
            )
            try:
                while not state.stop.is_set() and not self._stopping:
                    outcome = await self.engine.run_iteration(run_id)
                    if outcome in TERMINAL_OUTCOMES:
                        break
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._last_error = f"iteration_error: {type(exc).__name__}"
            finally:
                state.stop.set()
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
                # Release the lease only if we still hold it (not lost mid-run). A still-active
                # run (shutdown/cancel mid-flight) becomes recoverable; a terminal run is done.
                if not state.lease_lost:
                    self._release(run_id, token)
                self._states.pop(run_id, None)
                self._tasks.pop(mission_id, None)
                await self.runtime._emit_event(
                    event_type=ev.EVENT_RUN_STOPPED,
                    mission_id=mission_id,
                    source="mission_worker",
                    message=f"Mission run {run_id} stopped.",
                    payload={"run_id": run_id},
                )

    async def _heartbeat(
        self, run_id: str, mission_id: str, token: str, state: _RunState
    ) -> None:
        """Renew the lease on an interval; stop the run if renewal is ever lost."""
        interval = max(0.05, self.config.lease_renew_interval_seconds)
        count = 0
        while not state.stop.is_set():
            await asyncio.sleep(interval)
            if state.stop.is_set():
                return
            now = utcnow()
            expiry = now + timedelta(seconds=self.config.lease_duration_seconds)
            with self.runtime.session_scope() as session:
                rc = MissionExecutionRunRepository(session).renew_lease(
                    run_id, owner_id=self.owner_id, token=token, now=now, expiry=expiry
                )
                session.commit()
            if rc == 0:
                state.lease_lost = True
                state.stop.set()
                await self.runtime._emit_event(
                    event_type=ev.EVENT_LEASE_LOST,
                    mission_id=mission_id,
                    source="mission_worker",
                    message=f"Mission lease lost for run {run_id}; stopping worker.",
                    payload={"run_id": run_id},
                )
                return
            count += 1
            if count % _RENEW_EVENT_EVERY == 0:  # avoid renewal flooding
                await self.runtime._emit_event(
                    event_type=ev.EVENT_LEASE_RENEWED,
                    mission_id=mission_id,
                    source="mission_worker",
                    message=f"Mission lease renewed for run {run_id}.",
                    payload={"run_id": run_id},
                )

    async def _acquire(self, run_id: str, mission_id: str) -> str | None:
        """Atomically acquire the DB lease and set mission/run active. Returns the token."""
        token = new_uuid()
        now = utcnow()
        expiry = now + timedelta(seconds=self.config.lease_duration_seconds)
        with self.runtime.session_scope() as session:
            rc = MissionExecutionRunRepository(session).acquire_lease(
                run_id, owner_id=self.owner_id, token=token, now=now, expiry=expiry
            )
            session.commit()
        if rc != 1:
            return None
        # Mission active (run row is already active from the atomic acquire).
        with contextlib.suppress(MissionTransitionError):
            self.engine._apply_transition(run_id, mission_id, MissionStatus.ACTIVE)
        await self.runtime._emit_event(
            event_type=ev.EVENT_LEASE_ACQUIRED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission lease acquired for run {run_id}.",
            payload={"run_id": run_id, "owner_id": self.owner_id},
        )
        return token

    def _release(self, run_id: str, token: str) -> None:
        with self.runtime.session_scope() as session:
            MissionExecutionRunRepository(session).release_lease(run_id, token=token)
            session.commit()

    # -- recovery ----------------------------------------------------------------

    async def recover(self) -> None:
        """Reconcile interrupted runs after a node restart (Part M).

        Active runs with an expired/cleared lease are requeued for a FRESH coordinator
        decision; their interrupted ``running`` steps are marked failed (never assumed
        completed, never replayed). Terminal/paused/waiting runs are left untouched.
        """
        if not self.config.recovery_enabled:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            recoverable = MissionExecutionRunRepository(session).recoverable_active_ids(
                self.node_id, now
            )
        for run_id in recoverable:
            with self.runtime.session_scope() as session:
                runs = MissionExecutionRunRepository(session)
                steps = MissionStepRepository(session)
                run = runs.get(run_id)
                if run is None:
                    continue
                mission_id = run.mission_id
                # Mark interrupted (running) steps as failed/interrupted — honest accounting.
                for step in steps.list(mission_id=mission_id, limit=200):
                    if step.status == "running":
                        steps.update(
                            step.id,
                            status="failed",
                            error="Interrupted by node restart; not assumed completed.",
                            completed_at=now,
                        )
                # Requeue with a cleared lease and an incremented recovery counter.
                runs.update(
                    run_id,
                    fields={
                        "status": MissionStatus.QUEUED.value,
                        "recovery_count": run.recovery_count + 1,
                        "lease_token": None,
                        "execution_owner_id": None,
                        "lease_acquired_at": None,
                        "lease_renewed_at": None,
                        "lease_expiry": None,
                        "pause_requested": False,
                        "queued_at": now,
                    },
                )
                MissionRepository(session).update_status(mission_id, MissionStatus.QUEUED.value)
                session.commit()
            await self.runtime._emit_event(
                event_type=ev.EVENT_RECOVERED,
                mission_id=mission_id,
                source="mission_worker",
                message=f"Recovered interrupted mission run {run_id}; requeued for reassessment.",
                payload={"run_id": run_id},
            )

    # -- operations (start / pause / resume / cancel / one-step) -----------------

    async def enqueue_mission(self, mission_id: str) -> tuple[MissionExecutionRunRead, bool]:
        """Create (or reuse) a queued run and queue the mission. Returns (run, created).

        Idempotent: a mission that already has a non-terminal run returns that run. A
        terminal mission is rejected (a fresh run is an explicit, out-of-scope re-run op).
        """
        with self.runtime.session_scope() as session:
            mission_row = MissionRepository(session).get(mission_id)
            if mission_row is None:
                raise MissionNotFoundError(f"Mission {mission_id} not found.")
            mission_status = MissionStatus(mission_row.status)
            runs = MissionExecutionRunRepository(session)
            existing = runs.current_for_mission(mission_id)
            if existing is not None:
                run_read = MissionExecutionRunRead.model_validate(existing)
                run_id = existing.id
                created = False
            else:
                if mission_status in TERMINAL_MISSION_STATES:
                    raise MissionConflictError(
                        f"Mission {mission_id} is {mission_status.value}; cannot start a new run."
                    )
                budgets = resolve_budgets(self.config.default_budgets, None)
                run = runs.create(mission_id=mission_id, node_id=self.node_id, budgets=budgets)
                session.commit()
                run_read = MissionExecutionRunRead.model_validate(run)
                run_id = run.id
                created = True
        # Queue the mission (idempotent if already queued).
        with contextlib.suppress(MissionTransitionError):
            self.engine._apply_transition(
                run_id, mission_id, MissionStatus.QUEUED, run_fields={"queued_at": utcnow()}
            )
        if created:
            await self.runtime._emit_event(
                event_type=ev.EVENT_RUN_CREATED,
                mission_id=mission_id,
                source="mission_worker",
                message=f"Mission execution run {run_id} created.",
                payload={"run_id": run_id},
            )
        await self.runtime._emit_event(
            event_type=ev.EVENT_QUEUED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission {mission_id} queued for autonomous execution.",
            payload={"run_id": run_id},
        )
        return self._run_read(run_id) or run_read, created

    async def retry_mission(self, mission_id: str) -> tuple[MissionExecutionRunRead, bool]:
        """Retry a BLOCKED/FAILED mission after the customer repaired the cause (provider config,
        runtime PATH, auth). Creates a FRESH run — resetting the transient failure streak — and
        requeues the SAME mission; it never creates a duplicate mission (beta.9 Defect 4).

        Idempotent and safe:

        * BLOCKED / FAILED      -> reset to a fresh queued run (the retry).
        * any non-terminal run  -> already running; returns the current run (created=False).
        * COMPLETED / CANCELLED -> conflict (nothing to retry; re-run via a new mission).
        """
        retriable = {MissionStatus.BLOCKED, MissionStatus.FAILED}
        with self.runtime.session_scope() as session:
            missions = MissionRepository(session)
            mission_row = missions.get(mission_id)
            if mission_row is None:
                raise MissionNotFoundError(f"Mission {mission_id} not found.")
            status = MissionStatus(mission_row.status)
            existing = MissionExecutionRunRepository(session).current_for_mission(mission_id)
            if existing is not None:
                # Already has a live run — return it idempotently; never spawn a duplicate.
                return MissionExecutionRunRead.model_validate(existing), False
            if status not in retriable:
                raise MissionConflictError(
                    f"Mission {mission_id} is {status.value}; only blocked or failed missions "
                    f"can be retried (start a new mission to re-run a completed one)."
                )
            # Reset the mission out of its terminal state so a fresh run can be queued. This is the
            # one legitimate place a terminal mission is requeued (mirroring crash recovery).
            missions.update_status(mission_id, MissionStatus.QUEUED.value)
            session.commit()
        # beta.5: re-probe coding-agent readiness so a locally-repaired environment (a fixed
        # bubblewrap sandbox, a newly-installed executable) is re-detected on the fresh run instead
        # of a stale per-process "unavailable" being carried over — the beta.4 retry complaint.
        try:
            self.runtime.coding_agent.refresh_readiness()
        except Exception:  # noqa: BLE001 — readiness refresh is best-effort, never blocks a retry
            pass
        # The blocked run is terminal, so enqueue_mission creates a NEW run (failures reset to 0).
        return await self.enqueue_mission(mission_id)

    async def pause_mission(self, mission_id: str) -> MissionExecutionRunRead:
        """Request a pause. A running mission pauses at its next atomic boundary."""
        run = self._current_run(mission_id)
        if run is None:
            raise MissionConflictError(f"Mission {mission_id} has no active run to pause.")
        self._update_run(run.id, pause_requested=True)
        # If it is not actively iterating, pause it directly (queued/waiting -> paused).
        if mission_id not in self._tasks:
            with contextlib.suppress(MissionTransitionError):
                self.engine._apply_transition(
                    run.id, mission_id, MissionStatus.PAUSED, run_fields={"paused_at": utcnow()}
                )
                await self.runtime._emit_event(
                    event_type=ev.EVENT_PAUSED,
                    mission_id=mission_id,
                    source="mission_worker",
                    message=f"Mission {mission_id} paused.",
                    payload={"run_id": run.id},
                )
        return self._run_read(run.id)

    async def resume_mission(self, mission_id: str) -> MissionExecutionRunRead:
        """Resume a paused/waiting mission: requeue, preserving steps/usage. No replay."""
        run = self._latest_run(mission_id)
        if run is None or run.status not in ("paused", "waiting"):
            raise MissionConflictError(
                f"Mission {mission_id} is not paused or waiting; cannot resume."
            )
        with contextlib.suppress(MissionTransitionError):
            self.engine._apply_transition(
                run.id,
                mission_id,
                MissionStatus.QUEUED,
                run_fields={"pause_requested": False, "waiting_json": None, "queued_at": utcnow()},
            )
        await self.runtime._emit_event(
            event_type=ev.EVENT_RESUMED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Mission {mission_id} resumed; requeued for reassessment.",
            payload={"run_id": run.id},
        )
        return self._run_read(run.id)

    async def cancel_mission(self, mission_id: str) -> MissionExecutionRunRead:
        """Request cancellation. A running mission cancels at its next atomic boundary."""
        run = self._current_run(mission_id)
        if run is None:
            raise MissionConflictError(f"Mission {mission_id} has no active run to cancel.")
        now = utcnow()
        self._update_run(run.id, cancel_requested_at=now)
        await self.runtime._emit_event(
            event_type=ev.EVENT_CANCEL_REQUESTED,
            mission_id=mission_id,
            source="mission_worker",
            message=f"Cancellation requested for mission {mission_id}.",
            payload={"run_id": run.id},
        )
        # If no task is actively iterating, cancel directly (acknowledge + transition).
        if mission_id not in self._tasks:
            self._update_run(run.id, cancel_acknowledged_at=utcnow())
            with contextlib.suppress(MissionTransitionError):
                self.engine._apply_transition(
                    run.id,
                    mission_id,
                    MissionStatus.CANCELLED,
                    run_fields={"cancelled_at": utcnow()},
                )
                # Stage 13B: cancel any pending reply waits (prevents a later resume).
                _comms = getattr(self.runtime, "comms", None)
                if _comms is not None:
                    with contextlib.suppress(Exception):
                        _comms.cancel_mission_waits(mission_id)
                await self.runtime._emit_event(
                    event_type=ev.EVENT_CANCELLED,
                    mission_id=mission_id,
                    source="mission_worker",
                    message=f"Mission {mission_id} cancelled.",
                    payload={"run_id": run.id},
                )
        return self._run_read(run.id)

    async def run_one_step(self, mission_id: str) -> MissionExecutionStatus:
        """Run exactly ONE autonomous iteration synchronously (no background loop).

        Acquires the lease, runs a single :meth:`MissionEngine.run_iteration`, releases the
        lease, and returns the resulting execution status. Conflicts if a worker already
        holds the mission.
        """
        if mission_id in self._tasks:
            raise MissionConflictError(
                f"Mission {mission_id} is being executed by a worker; cannot step manually."
            )
        run = self._current_run(mission_id)
        if run is None:
            run_read, _ = await self.enqueue_mission(mission_id)
            run_id = run_read.id
        else:
            run_id = run.id
        token = await self._acquire(run_id, mission_id)
        if token is None:
            raise MissionConflictError(
                f"Could not acquire a lease for mission {mission_id} (already leased)."
            )
        try:
            await self.engine.run_iteration(run_id)
        finally:
            self._release(run_id, token)
        return self.get_execution_status(mission_id)

    # -- status reads (never start work) -----------------------------------------

    def get_execution_status(self, mission_id: str) -> MissionExecutionStatus | None:
        with self.runtime.session_scope() as session:
            mission_row = MissionRepository(session).get(mission_id)
            if mission_row is None:
                return None
            mission = MissionRead.model_validate(mission_row)
            run = MissionExecutionRunRepository(session).latest_for_mission(mission_id)
            run_read = MissionExecutionRunRead.model_validate(run) if run is not None else None
            budget = {}
            if run is not None:
                budgets = resolve_budgets(self.config.default_budgets, run.budgets_json)
                budget = budget_remaining(run, budgets)
        return MissionExecutionStatus(
            mission=mission,
            run=run_read,
            budget_remaining=budget,
            has_active_run=run_read is not None
            and run_read.status not in ("completed", "blocked", "failed", "cancelled"),
        )

    def worker_status(self) -> MissionWorkerStatus:
        with self.runtime.session_scope() as session:
            queued = len(
                MissionExecutionRunRepository(session).queued_run_ids(self.node_id, limit=1000)
            )
        return MissionWorkerStatus(
            enabled=self.config.enabled,
            running=self._started and not self._stopping,
            degraded=self._degraded,
            node_id=self.node_id,
            worker_count=self.config.worker_count,
            active_missions=list(self._tasks.keys()),
            active_count=len(self._tasks),
            queued_count=queued,
            last_error=self._last_error,
        )

    # -- small internal helpers --------------------------------------------------

    def _current_run(self, mission_id: str):
        with self.runtime.session_scope() as session:
            return MissionExecutionRunRepository(session).current_for_mission(mission_id)

    def _latest_run(self, mission_id: str):
        with self.runtime.session_scope() as session:
            return MissionExecutionRunRepository(session).latest_for_mission(mission_id)

    def _run_read(self, run_id: str) -> MissionExecutionRunRead | None:
        with self.runtime.session_scope() as session:
            run = MissionExecutionRunRepository(session).get(run_id)
            return MissionExecutionRunRead.model_validate(run) if run is not None else None

    def _update_run(self, run_id: str, **fields) -> None:
        with self.runtime.session_scope() as session:
            MissionExecutionRunRepository(session).update(run_id, fields=fields)
            session.commit()
