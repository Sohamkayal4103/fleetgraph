"""Managed artifact-transfer worker (Stage 13D.2, Part F).

Downloads authorized inbound transfers by streaming validated byte ranges from the peer's
authenticated artifact endpoint into the managed store's partial buffer, resuming from the
persisted offset, then verifying the exact SHA-256 + size before an atomic commit and import.
It is INDEPENDENT of the mission worker and the RF MCP sessions — a transfer failure or restart
never restarts an RF backend, and infrastructure retries never create a MissionStep.

Retry policy: transient failures (network/timeout/HTTP 5xx) back off exponentially up to a
ceiling; authorization, digest, size, traversal, and permanent protocol failures are NOT
retried. Cancelled transfers never resume; completed objects are never re-downloaded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

from aithernet.artifacts import events as ev
from aithernet.artifacts.service import PERMANENT_CODES, ArtifactError, aware
from aithernet.artifacts.store import ArtifactStoreError
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    ArtifactTransferAttemptRepository,
    ArtifactTransferRepository,
    PeerRepository,
    RFArtifactRepository,
)
from aithernet.transport.envelope import MessageKind, build_envelope

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

_TRANSIENT_PROGRESS_EVERY = 4  # rate-limit progress events to every Nth chunk


class ArtifactTransferWorker:
    """Polls for authorized inbound transfers and drives resumable downloads."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.artifacts
        self.owner_id = f"artifact-{runtime.config.node_id}-{new_uuid()[:8]}"
        self._poll_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._stopping = False
        self._started = False
        self._degraded = False
        self._last_error: str | None = None

    def is_running(self) -> bool:
        return self._started and not self._stopping

    def is_degraded(self) -> bool:
        return self._degraded

    def schedule(self) -> None:
        with contextlib.suppress(Exception):
            self._wake.set()

    async def start(self) -> None:
        if self._started or not (self.config.enabled and self.config.worker_enabled):
            return
        self._started = True
        self._stopping = False
        with contextlib.suppress(Exception):
            await self.runtime.artifacts.recover()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="artifact-transfer-poll")

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._stopping = True
        self._wake.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
        if self._inflight:
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._inflight), timeout=5.0)
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
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
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=max(0.1, self.config.worker_poll_interval_seconds)
                )
            self._wake.clear()

    async def _poll_once(self) -> None:
        self._inflight = {t for t in self._inflight if not t.done()}
        free = max(0, self.config.max_concurrent_transfers - len(self._inflight))
        if free <= 0:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            candidates = ArtifactTransferRepository(session).claimable_inbound(now, limit=free)
        for transfer in candidates:
            lease = now + timedelta(seconds=120)
            with self.runtime.session_scope() as session:
                claimed = ArtifactTransferRepository(session).claim(
                    transfer.transfer_id, owner=self.owner_id, lease_until=lease
                )
                session.commit()
            if claimed == 1:
                task = asyncio.create_task(
                    self._run_transfer(transfer.transfer_id),
                    name=f"artifact-{transfer.transfer_id[:8]}",
                )
                self._inflight.add(task)

    # -- one transfer ------------------------------------------------------------

    async def _run_transfer(self, transfer_id: str) -> None:
        try:
            await self._download(transfer_id)
        except (ArtifactError, ArtifactStoreError) as exc:
            await self._handle_failure(transfer_id, type(exc).__name__, getattr(exc, "code", None),
                                       str(exc))
        except (httpx.HTTPError, OSError) as exc:
            await self._handle_failure(transfer_id, "transient", "transient", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            await self._handle_failure(transfer_id, "error", "error", type(exc).__name__)
        finally:
            with self.runtime.session_scope() as session:
                ArtifactTransferRepository(session).update(
                    transfer_id, fields={"claim_owner": None, "claim_expires_at": None}
                )
                session.commit()

    async def _download(self, transfer_id: str) -> None:
        store = self.runtime.artifacts.store
        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            if t is None or t.state not in ("authorized", "transferring", "paused"):
                return
            peer = PeerRepository(session).get(t.peer_id)
            peer_d = self.runtime.transport._peer_snapshot(peer) if peer is not None else None
            expected_size = t.expected_size
            expected_digest = t.expected_digest
            grant_expiry = t.grant_expires_at
            attempt_no = (t.attempt_count or 0) + 1
        if peer_d is None:
            raise ArtifactError("Peer not found for transfer.", code="unknown_peer")
        if grant_expiry is not None and aware(grant_expiry) <= utcnow():
            raise ArtifactError("Grant expired before transfer.", code="grant_expired")

        # Resume from the PERSISTED partial length on disk — this is the source of truth and
        # what makes a post-restart attempt begin at the saved offset, not byte zero. The
        # attempt records that start_offset so the restart/resume is provable from persisted data.
        start_offset = store.partial_size(transfer_id)
        resumed = start_offset > 0
        with self.runtime.session_scope() as session:
            ArtifactTransferRepository(session).update(
                transfer_id, fields={"state": "transferring", "attempt_count": attempt_no,
                                    "received_bytes": start_offset}
            )
            attempt = ArtifactTransferAttemptRepository(session).create(
                transfer_id=transfer_id, attempt_number=attempt_no, outcome="started",
                start_offset=start_offset,
            )
            session.commit()
            attempt_id = attempt.id
        await self._emit(
            ev.EVENT_ARTIFACT_TRANSFER_RESUMED if resumed else ev.EVENT_ARTIFACT_TRANSFER_STARTED,
            "Artifact download resumed." if resumed else "Artifact download started.",
            transfer_id=transfer_id, extra={"start_offset": start_offset},
        )

        offset = start_offset
        chunk = self.config.chunk_bytes
        delay = self.config.worker_chunk_delay_seconds
        progress_tick = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.request_timeout_seconds,
                                  connect=self.config.connect_timeout_seconds),
            verify=peer_d.get("tls_verify", True),
        ) as client:
            while offset < expected_size:
                if await self._is_cancelled(transfer_id):
                    return
                length = min(chunk, expected_size - offset)
                data = await self._pull(client, peer_d, transfer_id, offset, length)
                if not data:
                    raise ArtifactError("Empty range response.", code="transient")
                store.append_chunk(transfer_id, offset=offset, data=data,
                                   max_total_bytes=expected_size)
                offset += len(data)
                with self.runtime.session_scope() as session:
                    ArtifactTransferRepository(session).update(
                        transfer_id, fields={"received_bytes": offset, "partial_ref":
                                            f"partial/{transfer_id}"}
                    )
                    session.commit()
                progress_tick += 1
                if progress_tick % _TRANSIENT_PROGRESS_EVERY == 0:
                    await self._emit(ev.EVENT_ARTIFACT_TRANSFER_PROGRESS, "Transfer progress.",
                                     transfer_id=transfer_id,
                                     extra={"received_bytes": offset, "expected": expected_size})
                # Test-only deterministic pacing so a transfer can be interrupted mid-flight.
                # PRODUCTION default is 0 (no delay).
                if delay > 0:
                    await asyncio.sleep(delay)

        with self.runtime.session_scope() as session:
            ArtifactTransferAttemptRepository(session).update(
                attempt_id, fields={"outcome": "downloaded",
                                   "bytes_transferred": expected_size - start_offset,
                                   "finished_at": utcnow()}
            )
            session.commit()

        # Verify the exact digest + size, then atomically commit. A mismatch quarantines and
        # NEVER publishes — it is a permanent failure.
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_VERIFYING, "Verifying artifact.",
                         transfer_id=transfer_id)
        with self.runtime.session_scope() as session:
            ArtifactTransferRepository(session).update(transfer_id, fields={"state": "verifying"})
            session.commit()
        stored = store.verify_and_commit_partial(
            transfer_id, expected_digest=expected_digest, expected_size=expected_size
        )
        await self._finalize(transfer_id, stored)

    async def _pull(self, client: httpx.AsyncClient, peer_d: dict, transfer_id: str,
                    offset: int, length: int) -> bytes:
        """POST a signed pull envelope and return the raw byte range (bounded)."""
        identity = self.runtime.transport.require_identity()
        envelope = build_envelope(
            identity=identity, recipient_node_id=peer_d["expected_node_id"],
            recipient_agent_id=None,
            kind=MessageKind.ARTIFACT_PULL.value,
            payload={"transfer_id": transfer_id, "offset": offset, "length": length},
            correlation_id=transfer_id, content_type="application/octet-stream",
        )
        url = peer_d["endpoint_url"].rstrip("/") + "/agent/v1/artifact"
        resp = await client.post(url, content=json.dumps(envelope.authenticated_dict()),
                                 headers={"content-type": "application/json"})
        if resp.status_code in (401, 403, 404, 409, 410):
            raise ArtifactError(f"Peer refused the range ({resp.status_code}).",
                                code="grant_mismatch")
        resp.raise_for_status()
        return resp.content

    async def _finalize(self, transfer_id: str, stored) -> None:
        now = utcnow()
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(transfer_id)
            if t is None or t.state == "completed":
                return
            artifacts = RFArtifactRepository(session)
            peer = PeerRepository(session).get(t.peer_id)
            origin_node = peer.expected_node_id if peer is not None else None
            artifact, _created = artifacts.upsert(
                node_id=self.runtime.config.node_id, backend_id="imported",
                relative_path=f"imported/{stored.digest.split(':', 1)[1][:16]}",
                artifact_kind=t.artifact_kind or "unknown", size_bytes=stored.size_bytes,
                content_hash=stored.digest, mission_id=t.mission_id,
                mission_run_id=t.mission_run_id, mission_step_id=t.mission_step_id,
            )
            artifacts.update(artifact.id, fields={
                "digest": stored.digest, "object_ref": stored.relative_object_path,
                "size_bytes": stored.size_bytes, "availability_state": "imported",
                "origin_node_id": origin_node, "origin_artifact_id": t.origin_artifact_id,
                "display_name": t.display_name, "conversation_id": t.conversation_id,
                "request_id": t.request_id,
            })
            repo.update(transfer_id, fields={
                "state": "completed", "completed_at": now, "received_bytes": stored.size_bytes,
                "object_ref": stored.relative_object_path, "local_artifact_id": artifact.id,
                "error_type": None, "last_error": None,
            })
            session.commit()
            local_artifact_id = artifact.id
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_COMPLETED, "Artifact transfer completed.",
                         transfer_id=transfer_id, extra={"local_artifact_id": local_artifact_id})
        with contextlib.suppress(Exception):
            await self.runtime.artifacts.send_completion(transfer_id, ok=True)

    async def _handle_failure(self, transfer_id: str, error_type: str, code: str | None,
                              message: str) -> None:
        permanent = code in PERMANENT_CODES
        now = utcnow()
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(transfer_id)
            if t is None or t.state in ("completed", "cancelled"):
                return
            attempts = (t.attempt_count or 0)
            give_up = permanent or attempts >= self.config.max_attempts
            if give_up:
                fields = {"state": "failed", "error_type": code or error_type,
                          "last_error": message[:240]}
            else:
                backoff = min(self.config.maximum_backoff_seconds,
                             self.config.initial_backoff_seconds * (2 ** max(0, attempts - 1)))
                fields = {"state": "authorized", "error_type": code or error_type,
                          "last_error": message[:240],
                          "next_attempt_at": now + timedelta(seconds=backoff)}
            repo.update(transfer_id, fields=fields)
            ArtifactTransferAttemptRepository(session).create(
                transfer_id=transfer_id, attempt_number=attempts, outcome="failed",
                error_type=code or error_type, error=message[:240], finished_at=now,
            )
            session.commit()
            failed_now = give_up
        if failed_now:
            await self._emit(ev.EVENT_ARTIFACT_TRANSFER_FAILED, "Artifact transfer failed.",
                             transfer_id=transfer_id, extra={"error_type": code or error_type})
            with contextlib.suppress(Exception):
                await self.runtime.artifacts.send_completion(transfer_id, ok=False, reason=code)

    async def _is_cancelled(self, transfer_id: str) -> bool:
        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            return t is None or t.state in ("cancelled", "expired", "rejected")

    async def _emit(self, event_type: str, message: str, *, transfer_id: str,
                    extra: dict | None = None) -> None:
        payload = {"transfer_id": transfer_id}
        if extra:
            payload.update(extra)
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, source="artifacts", message=message, payload=payload
            )
