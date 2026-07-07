"""Durable delivery worker (Stage 13A, Part K).

A managed background worker, owned by :class:`TransportService`, that drives the durable
outbox: it recovers expired in-flight claims on startup, then repeatedly claims due records
with a DB-backed lease (so two workers can never own the same record), delivers them via the
configured transport, and persists the resulting terminal/retry state. A disabled worker
sends nothing; cancelled/expired/terminal records are never claimed. Shutdown stops claiming
new work and lets active attempts finish within a bounded grace window, leaving no record
claimed indefinitely (recovery requeues any that overran).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import AgentOutboxRepository
from aithernet.transport import events as ev

if TYPE_CHECKING:
    from aithernet.transport.service import TransportService


class DeliveryWorker:
    """Polls the durable outbox and delivers due messages within a bounded worker pool."""

    def __init__(self, service: TransportService) -> None:
        self.service = service
        self.runtime = service.runtime
        self.config = service.config.outbound
        self.owner_id = f"delivery-{service.node_id}-{new_uuid()[:8]}"
        self._poll_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max(1, self.config.worker_count))
        self._stopping = False
        self._started = False
        self._degraded = False
        self._last_error: str | None = None

    # -- status ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._started and not self._stopping

    def is_degraded(self) -> bool:
        return self._degraded

    def last_error(self) -> str | None:
        return self._last_error

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        if self._started or not self.config.enabled:
            return
        self._started = True
        self._stopping = False
        if getattr(self.config, "recovery_enabled", True):
            with contextlib.suppress(Exception):
                self._recover()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="transport-delivery-poll")
        await self.runtime.publish_ephemeral_event(
            event_type=ev.EVENT_WORKER_STARTED,
            source="transport",
            message="Transport delivery worker started.",
            payload={"worker_count": self.config.worker_count},
        )

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        if self._inflight:
            grace = max(1.0, self.config.shutdown_grace_seconds)
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._inflight), timeout=grace)
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Any record still claimed by an overrun attempt is requeued by recovery; reset now
        # so nothing is left claimed indefinitely.
        with contextlib.suppress(Exception):
            self._recover()
        self._started = False
        await self.runtime.publish_ephemeral_event(
            event_type=ev.EVENT_WORKER_STOPPED,
            source="transport",
            message="Transport delivery worker stopped.",
            payload={},
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
                    source="transport",
                    message=f"Transport worker poll error: {type(exc).__name__}.",
                    payload={},
                )
            await asyncio.sleep(max(0.05, self.config.poll_interval_seconds))

    async def _poll_once(self) -> None:
        self._reap()
        free = self._semaphore._value
        if free <= 0:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            due = AgentOutboxRepository(session).due_ids(now, limit=free)
        for record_id in due:
            claim_expiry = now + timedelta(seconds=self.config.delivery_lease_seconds)
            with self.runtime.session_scope() as session:
                claimed = AgentOutboxRepository(session).claim(
                    record_id, owner=self.owner_id, now=now, claim_expiry=claim_expiry
                )
                session.commit()
            if claimed == 1:
                task = asyncio.create_task(self._deliver(record_id), name=f"deliver-{record_id}")
                self._inflight.add(task)

    def _reap(self) -> None:
        for task in list(self._inflight):
            if task.done():
                self._inflight.discard(task)

    async def _deliver(self, record_id: str) -> None:
        async with self._semaphore:
            try:
                await self.service.deliver_record(record_id, owner=self.owner_id)
            except Exception as exc:
                self._last_error = f"deliver_error: {type(exc).__name__}"
            finally:
                self._reap()

    def _recover(self) -> int:
        with self.runtime.session_scope() as session:
            count = AgentOutboxRepository(session).recover_expired_claims(utcnow())
            session.commit()
        return count
