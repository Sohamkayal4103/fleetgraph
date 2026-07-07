"""Durable webhook delivery worker for external agents (Stage 14D).

Mirrors the Stage 13A peer delivery worker: recover expired claims on startup, then claim
due *webhook-mode* messages with a DB-backed lease, deliver them, and persist the resulting
terminal/retry/dead-letter state. WebSocket-mode messages are pushed by the live session and
are never claimed here. Retries are deterministic infrastructure (bounded exponential backoff
with jitter), never coordinator decisions. Shutdown stops claiming and lets in-flight
attempts finish within a grace window; any overrun is requeued by recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import InteropMessageRepository

if TYPE_CHECKING:
    from aithernet.interop.service import InteropService


class InteropDeliveryWorker:
    """Polls the interop outbox and delivers due webhook messages within a bounded pool."""

    def __init__(self, service: InteropService) -> None:
        self.service = service
        self.runtime = service.runtime
        self.config = service.config.delivery
        self.owner_id = f"interop-{service.node_id}-{new_uuid()[:8]}"
        self._poll_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        self._stopping = False
        self._started = False
        self._degraded = False
        self._last_error: str | None = None

    def is_running(self) -> bool:
        return self._started and not self._stopping

    def is_degraded(self) -> bool:
        return self._degraded

    def last_error(self) -> str | None:
        return self._last_error

    async def start(self) -> None:
        if self._started or not (self.service.enabled and self.config.worker_enabled):
            return
        self._started = True
        self._stopping = False
        with contextlib.suppress(Exception):
            self._recover()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="interop-delivery-poll")

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        if self._inflight:
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._inflight), timeout=10.0)
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        with contextlib.suppress(Exception):
            self._recover()
        self._started = False

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a poll error never crashes the node
                self._degraded = True
                self._last_error = f"poll_error: {type(exc).__name__}"
            await asyncio.sleep(max(0.05, self.config.poll_interval_seconds))

    async def _poll_once(self) -> None:
        self._reap()
        free = self._semaphore._value
        if free <= 0:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            repo = InteropMessageRepository(session)
            repo.expire_old(now)
            session.commit()
            due = repo.due_ids(now, mode="webhook", limit=free)
        for record_id in due:
            claim_expiry = now + timedelta(seconds=self.config.claim_seconds)
            with self.runtime.session_scope() as session:
                claimed = InteropMessageRepository(session).claim(
                    record_id, owner=self.owner_id, now=now, claim_expiry=claim_expiry
                )
                session.commit()
            if claimed == 1:
                task = asyncio.create_task(
                    self._deliver(record_id), name=f"interop-deliver-{record_id}"
                )
                self._inflight.add(task)

    def _reap(self) -> None:
        for task in list(self._inflight):
            if task.done():
                self._inflight.discard(task)

    async def _deliver(self, record_id: str) -> None:
        async with self._semaphore:
            try:
                await self.service.deliver_message(record_id, owner=self.owner_id)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"deliver_error: {type(exc).__name__}"
            finally:
                self._reap()

    def _recover(self) -> int:
        with self.runtime.session_scope() as session:
            count = InteropMessageRepository(session).recover_expired_claims(utcnow())
            session.commit()
        return count
