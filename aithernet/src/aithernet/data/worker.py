"""Durable export delivery worker (Stage 14E, Part 10).

A restart-safe worker, separate from the peer outbox, external-agent outbox, and mission steps.
It recovers expired claims on startup, claims due *sealed* batches with a DB lease, delivers
them to their destination, verifies the receipt, and persists terminal/retry/dead-letter state.
Retries are deterministic infrastructure (bounded exponential backoff + jitter). It honours the
operator pause switch and disk-space floor, and creates NO MissionStep.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import DataExportBatchRepository

if TYPE_CHECKING:
    from aithernet.data.service import DataPlatformService


class DataExportWorker:
    """Polls the export outbox and delivers due batches within a bounded pool."""

    def __init__(self, service: DataPlatformService) -> None:
        self.service = service
        self.runtime = service.runtime
        self.config = service.config.export
        self.owner_id = f"export-{service.node_id}-{new_uuid()[:8]}"
        self._poll_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max(1, self.config.per_destination_concurrency))
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
        self._poll_task = asyncio.create_task(self._poll_loop(), name="data-export-poll")

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
            except Exception as exc:  # noqa: BLE001
                self._degraded = True
                self._last_error = f"poll_error: {type(exc).__name__}"
            await asyncio.sleep(max(0.05, self.config.poll_interval_seconds))

    async def _poll_once(self) -> None:
        self._reap()
        # Honour the operator pause switch — collection continues, delivery halts.
        if self.config.paused:
            return
        free = self._semaphore._value
        if free <= 0:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            due = DataExportBatchRepository(session).due_ids(now, limit=free)
        for batch_id in due:
            claim_expiry = now + timedelta(seconds=self.config.claim_seconds)
            with self.runtime.session_scope() as session:
                claimed = DataExportBatchRepository(session).claim(
                    batch_id, owner=self.owner_id, now=now, claim_expiry=claim_expiry
                )
                session.commit()
            if claimed == 1:
                task = asyncio.create_task(self._deliver(batch_id), name=f"export-{batch_id}")
                self._inflight.add(task)

    def _reap(self) -> None:
        for task in list(self._inflight):
            if task.done():
                self._inflight.discard(task)

    async def _deliver(self, batch_id: str) -> None:
        async with self._semaphore:
            try:
                await self.service.deliver_batch(batch_id, owner=self.owner_id)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"deliver_error: {type(exc).__name__}"
            finally:
                self._reap()

    def _recover(self) -> int:
        with self.runtime.session_scope() as session:
            count = DataExportBatchRepository(session).recover_expired_claims(utcnow())
            session.commit()
        return count
