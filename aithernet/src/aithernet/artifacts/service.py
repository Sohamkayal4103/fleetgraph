"""Artifact transfer orchestration (Stage 13D.2).

Owns indexing local RF artifacts into the managed store, the signed offer/request/grant/reject/
complete/failed control exchange (carried inside the existing signed transport envelope), the
grant binding, and the read models the API/CLI/dashboard render. Binary bytes move ONLY through
the dedicated authenticated streaming endpoint + the managed store on disk — never through this
service's JSON, SQLite, prompts, or events.

Permissions (enforced at the RECEIVING node, separate from transport trust):
  * a peer's ``may_offer_artifacts`` gates an inbound *offer* we record;
  * a peer's ``may_request_artifacts`` + ``may_receive_artifacts`` gate a *grant* we issue.
Defaults are conservative — a trusted peer has no artifact permission until granted.
"""

from __future__ import annotations

import contextlib
import os
from datetime import timedelta
from typing import TYPE_CHECKING

from aithernet.artifacts import events as ev
from aithernet.artifacts.protocol import (
    ArtifactControlError,
    ArtifactControlMessage,
    ArtifactControlType,
    is_artifact_control,
    parse_artifact_control,
)
from aithernet.artifacts.store import ArtifactStore, ArtifactStoreError
from aithernet.rf.artifacts import safe_relative_path
from aithernet.sanitize import redact_secrets
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    AgentInboxRepository,
    AgentOutboxRepository,
    ArtifactTransferRepository,
    PeerRepository,
    RemoteArtifactReferenceRepository,
    RFArtifactRepository,
)
from aithernet.transport.envelope import MessageKind, build_envelope

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

# Non-retryable control/transfer failure codes (authorization/digest/size/path/protocol).
PERMANENT_CODES = frozenset({
    "not_authorized", "peer_not_trusted", "peer_disabled", "unknown_peer", "bad_digest",
    "digest_mismatch", "size_mismatch", "too_large", "store_quota", "path_escape",
    "unsafe_source", "grant_expired", "grant_not_found", "grant_mismatch", "kind_not_allowed",
    "no_endpoint", "expired",
})


class ArtifactError(Exception):
    code: str = "artifact_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _short(text: str | None, limit: int = 240) -> str | None:
    if not text:
        return None
    cleaned = redact_secrets(str(text))
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def aware(dt):
    """Normalize a possibly-naive SQLite datetime to UTC-aware for safe comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import UTC
        return dt.replace(tzinfo=UTC)
    return dt


class ArtifactService:
    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.artifacts
        self.node_id = runtime.config.node_id
        self._store: ArtifactStore | None = None

    # -- managed store -----------------------------------------------------------

    @property
    def store(self) -> ArtifactStore:
        if self._store is None:
            root = self.config.state_directory or os.path.join(
                os.path.expanduser("~"), ".local", "state", "aithernet", "artifact-store"
            )
            self._store = ArtifactStore(
                root,
                total_quota_bytes=self.config.total_store_quota_bytes,
                max_artifact_bytes=self.config.max_artifact_bytes,
                minimum_free_bytes=self.config.minimum_free_bytes,
            )
        return self._store

    # -- indexing (import a local backend artifact into the managed store) -------

    def index_local_artifact(
        self, *, source_path: str, workspace: str | None = None, backend_id: str = "legacy_gr_mcp",
        artifact_kind: str = "unknown", display_name: str | None = None,
        backend_version: str | None = None, backend_source_revision: str | None = None,
        mission_id: str | None = None, mission_run_id: str | None = None,
        mission_step_id: str | None = None, mcp_call_id: str | None = None,
        conversation_id: str | None = None, request_id: str | None = None,
    ) -> dict:
        """Stream-import a regular local file into the store and upsert a transferable record.

        ``workspace`` (when given) confines ``source_path`` to the backend workspace before
        import; the original backend file is left in place. Bytes go to the content-addressed
        store; this row records only digest/size/object reference + identity/evidence.
        """
        if workspace is not None:
            rel = safe_relative_path(workspace, source_path)
            abs_path = os.path.join(os.path.realpath(workspace), rel)
        else:
            rel = os.path.basename(source_path)
            abs_path = source_path
        try:
            stored = self.store.import_file(abs_path)
        except ArtifactStoreError as exc:
            raise ArtifactError(str(exc), code=exc.code) from exc
        with self.runtime.session_scope() as session:
            repo = RFArtifactRepository(session)
            artifact, _created = repo.upsert(
                node_id=self.node_id, backend_id=backend_id, relative_path=rel,
                artifact_kind=artifact_kind, size_bytes=stored.size_bytes,
                content_hash=stored.digest, mcp_call_id=mcp_call_id, mission_id=mission_id,
                mission_run_id=mission_run_id, mission_step_id=mission_step_id,
            )
            repo.update(artifact.id, fields={
                "digest": stored.digest, "object_ref": stored.relative_object_path,
                "size_bytes": stored.size_bytes, "availability_state": "local",
                "origin_node_id": self.node_id, "backend_version": backend_version,
                "backend_source_revision": backend_source_revision,
                "display_name": display_name or os.path.basename(rel),
                "conversation_id": conversation_id, "request_id": request_id,
            })
            session.commit()
            result = self._artifact_summary(repo.get(artifact.id))
        return result

    # -- outbound control: offer / request / cancel / retry ----------------------

    async def offer_artifact(
        self, *, artifact_id: str, peer_id: str,
        conversation_id: str | None = None, purpose: str | None = None,
    ) -> dict:
        """Offer a locally-stored artifact to a trusted peer (sends a signed artifact_offer)."""
        peer = self._peer_or_raise(peer_id)
        with self.runtime.session_scope() as session:
            art = RFArtifactRepository(session).get(artifact_id)
            art_meta = None
            if art is not None and art.digest and art.object_ref:
                art_meta = {
                    "digest": art.digest, "size_bytes": art.size_bytes,
                    "artifact_kind": art.artifact_kind, "display_name": art.display_name,
                    "object_ref": art.object_ref,
                }
        if art_meta is None:
            raise ArtifactError("Artifact is not in the managed store.", code="not_storable")
        transfer_id = new_uuid()
        control = ArtifactControlMessage(
            message_type=ArtifactControlType.OFFER, transfer_id=transfer_id,
            origin_artifact_id=artifact_id, digest=art_meta["digest"], size=art_meta["size_bytes"],
            artifact_kind=art_meta["artifact_kind"], display_name=art_meta["display_name"],
            conversation_id=conversation_id, state="offered",
            expected_sender=self.node_id, expected_receiver=peer["expected_node_id"],
        )
        with self.runtime.session_scope() as session:
            ArtifactTransferRepository(session).create(
                transfer_id=transfer_id, direction="outbound", peer_id=peer_id,
                local_artifact_id=artifact_id, state="offered",
                expected_digest=art_meta["digest"], expected_size=art_meta["size_bytes"],
                artifact_kind=art_meta["artifact_kind"], display_name=art_meta["display_name"],
                conversation_id=conversation_id, object_ref=art_meta["object_ref"],
                expected_sender_node_id=self.node_id,
                expected_receiver_node_id=peer["expected_node_id"],
            )
            session.commit()
        await self._send_control(peer, control, conversation_id=conversation_id)
        await self._emit(ev.EVENT_ARTIFACT_OFFER_QUEUED,
                         f"Offered artifact to peer {peer_id}.",
                         payload={"transfer_id": transfer_id, "peer_id": peer_id})
        return {"transfer_id": transfer_id, "state": "offered", "peer_id": peer_id}

    async def request_artifact(self, *, peer_id: str, origin_artifact_id: str,
                               digest: str | None = None, size: int | None = None,
                               conversation_id: str | None = None, purpose: str | None = None,
                               mission_id: str | None = None) -> dict:
        """Request a remote artifact from a trusted peer (sends a signed artifact_request)."""
        if not self.config.enabled:
            raise ArtifactError("Artifact transfer is disabled.", code="disabled")
        peer = self._peer_or_raise(peer_id)
        with self.runtime.session_scope() as session:
            ref = RemoteArtifactReferenceRepository(session).find(
                peer_id=peer_id, origin_artifact_id=origin_artifact_id
            )
            if ref is not None:
                digest = digest or ref.digest
                size = size if size is not None else ref.size_bytes
                conversation_id = conversation_id or ref.conversation_id
        if not digest or size is None:
            raise ArtifactError("Unknown artifact digest/size to request.", code="unknown_artifact")
        # Local capacity gate (we will be the receiver/downloader).
        try:
            self.store.precheck_capacity(size)
        except ArtifactStoreError as exc:
            raise ArtifactError(str(exc), code=exc.code) from exc
        transfer_id = new_uuid()
        request_id = new_uuid()
        with self.runtime.session_scope() as session:
            ArtifactTransferRepository(session).create(
                transfer_id=transfer_id, direction="inbound", peer_id=peer_id,
                origin_artifact_id=origin_artifact_id, state="requested",
                expected_digest=digest, expected_size=size, conversation_id=conversation_id,
                request_id=request_id, mission_id=mission_id,
                expected_sender_node_id=peer["expected_node_id"],
                expected_receiver_node_id=self.node_id,
            )
            session.commit()
        control = ArtifactControlMessage(
            message_type=ArtifactControlType.REQUEST, transfer_id=transfer_id,
            origin_artifact_id=origin_artifact_id, digest=digest, size=size,
            conversation_id=conversation_id, request_id=request_id, state="requested",
            expected_sender=peer["expected_node_id"], expected_receiver=self.node_id,
        )
        await self._send_control(peer, control, conversation_id=conversation_id)
        await self._emit(ev.EVENT_ARTIFACT_REQUEST_QUEUED,
                         f"Requested artifact from peer {peer_id}.",
                         payload={"transfer_id": transfer_id, "peer_id": peer_id})
        return {"transfer_id": transfer_id, "state": "requested", "peer_id": peer_id}

    async def cancel_transfer(self, transfer_id: str) -> bool:
        now = utcnow()
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(transfer_id)
            if t is None or t.state in ("completed", "cancelled", "rejected", "expired"):
                return False
            repo.update(transfer_id, fields={"state": "cancelled", "cancelled_at": now,
                                             "claim_owner": None, "claim_expires_at": None})
            session.commit()
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_CANCELLED, "Transfer cancelled by operator.",
                         payload={"transfer_id": transfer_id})
        return True

    async def retry_transfer(self, transfer_id: str) -> bool:
        """Re-queue a failed inbound transfer (only if the failure was transient)."""
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(transfer_id)
            if t is None or t.direction != "inbound" or t.state != "failed":
                return False
            if t.error_type in PERMANENT_CODES:
                return False
            repo.update(transfer_id, fields={"state": "authorized", "next_attempt_at": utcnow(),
                                             "error_type": None, "last_error": None})
            session.commit()
        return True

    # -- inbound control dispatch ------------------------------------------------

    async def on_inbound_control(self, inbox_record_id: str, peer_snapshot: dict) -> None:
        if not self.config.enabled:
            return
        with self.runtime.session_scope() as session:
            record = AgentInboxRepository(session).get(inbox_record_id)
            payload = dict(record.payload_json or {}) if record is not None else {}
            peer = PeerRepository(session).get(record.peer_id) if (
                record is not None and record.peer_id
            ) else None
            peer_auth = self._peer_auth(peer) if peer is not None else None
        if record is None or not is_artifact_control(payload):
            return
        try:
            ctrl = parse_artifact_control(payload)
        except ArtifactControlError:
            return
        if peer_auth is None:
            return
        handler = {
            ArtifactControlType.OFFER: self._on_offer,
            ArtifactControlType.REQUEST: self._on_request,
            ArtifactControlType.GRANT: self._on_grant,
            ArtifactControlType.REJECT: self._on_reject,
            ArtifactControlType.COMPLETE: self._on_complete,
            ArtifactControlType.FAILED: self._on_failed,
        }.get(ctrl.message_type)
        if handler is not None:
            await handler(record, ctrl, peer_auth)

    async def _on_offer(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        if not peer.get("may_offer_artifacts"):
            await self._emit(ev.EVENT_ARTIFACT_TRANSFER_REJECTED,
                             "Offer from peer not authorized to offer; ignored.",
                             payload={"peer_id": peer["id"], "transfer_id": ctrl.transfer_id})
            return
        with self.runtime.session_scope() as session:
            RemoteArtifactReferenceRepository(session).upsert(
                peer_id=peer["id"], origin_node_id=record.sender_node_id,
                origin_artifact_id=ctrl.origin_artifact_id or ctrl.transfer_id,
                digest=ctrl.digest or "", size_bytes=ctrl.size or 0,
                artifact_kind=ctrl.artifact_kind, display_name=ctrl.display_name,
                conversation_id=record.conversation_id, request_id=ctrl.request_id,
            )
            session.commit()
        await self._emit(
            ev.EVENT_ARTIFACT_OFFER_RECEIVED, "Recorded a remote artifact offer.",
            payload={"peer_id": peer["id"], "origin_artifact_id": ctrl.origin_artifact_id},
        )

    async def _on_request(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        await self._emit(ev.EVENT_ARTIFACT_REQUEST_RECEIVED, "Inbound artifact request.",
                         payload={"peer_id": peer["id"], "transfer_id": ctrl.transfer_id})
        reason = self._authorize_request(ctrl, peer)
        if reason is not None:
            await self._reject(peer, ctrl, reason)
            return
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            existing = repo.get_by_transfer_id(ctrl.transfer_id)
            if existing is not None:  # duplicate request -> idempotent, re-send the grant
                grant_present = existing.state in ("authorized", "transferring", "completed")
                session.rollback()
                if grant_present:
                    await self._send_grant(peer, existing)
                return
            art = RFArtifactRepository(session).by_digest(ctrl.digest or "__none__")
            art = art[0] if art else None
            if art is None or not art.object_ref:
                session.rollback()
                await self._reject(peer, ctrl, "grant_not_found")
                return
            expiry = utcnow() + timedelta(seconds=self.config.grant_ttl_seconds)
            transfer = repo.create(
                transfer_id=ctrl.transfer_id, direction="outbound", peer_id=peer["id"],
                local_artifact_id=art.id, origin_artifact_id=ctrl.origin_artifact_id,
                state="authorized", expected_digest=art.digest, expected_size=art.size_bytes or 0,
                artifact_kind=art.artifact_kind, display_name=art.display_name,
                object_ref=art.object_ref, conversation_id=record.conversation_id,
                request_id=ctrl.request_id, grant_expires_at=expiry,
                expected_sender_node_id=self.node_id,
                expected_receiver_node_id=record.sender_node_id,
            )
            session.commit()
            transfer_snapshot = self._transfer_summary(transfer)
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_AUTHORIZED, "Authorized + granted a transfer.",
                         payload={"transfer_id": ctrl.transfer_id, "peer_id": peer["id"]})
        await self._send_grant(peer, None, snapshot=transfer_snapshot)

    def _authorize_request(self, ctrl: ArtifactControlMessage, peer: dict) -> str | None:
        if not self.config.enabled:
            return "disabled"
        if not peer.get("may_request_artifacts") or not peer.get("may_receive_artifacts"):
            return "not_authorized"
        cap = peer.get("max_artifact_bytes")
        if cap is not None and (ctrl.size or 0) > cap:
            return "too_large"
        kinds = peer.get("allowed_artifact_kinds_json")
        if kinds and ctrl.artifact_kind and ctrl.artifact_kind not in kinds:
            return "kind_not_allowed"
        max_conc = peer.get("max_concurrent_transfers") or (
            self.config.default_max_concurrent_transfers_per_peer
        )
        with self.runtime.session_scope() as session:
            active = ArtifactTransferRepository(session).active_count_for_peer(peer["id"])
        if active >= max_conc:
            return "too_many_transfers"
        return None

    async def _on_grant(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(ctrl.transfer_id)
            if t is None or t.direction != "inbound":
                return
            if t.state not in ("requested", "authorized"):
                return  # idempotent — already progressing
            repo.update(ctrl.transfer_id, fields={
                "state": "authorized", "expected_digest": ctrl.digest or t.expected_digest,
                "expected_size": ctrl.size if ctrl.size is not None else t.expected_size,
                "grant_expires_at": _parse_iso(ctrl.expiry),
                "expected_sender_node_id": record.sender_node_id,
                "expected_receiver_node_id": self.node_id,
                "next_attempt_at": utcnow(), "error_type": None, "last_error": None,
            })
            session.commit()
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_AUTHORIZED, "Transfer authorized by peer.",
                         payload={"transfer_id": ctrl.transfer_id})
        self.runtime.schedule_artifact_worker()

    async def _on_reject(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(ctrl.transfer_id)
            if (t is not None and t.direction == "inbound"
                    and t.state in ("requested", "authorized")):
                repo.update(ctrl.transfer_id, fields={
                    "state": "rejected", "error_type": "rejected",
                    "last_error": _short(ctrl.reason) or "peer rejected the request",
                })
                session.commit()
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_REJECTED, "Peer rejected the transfer.",
                         payload={"transfer_id": ctrl.transfer_id})

    async def _on_complete(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(ctrl.transfer_id)
            if t is not None and t.direction == "outbound" and t.state != "completed":
                repo.update(ctrl.transfer_id, fields={"state": "completed",
                                                     "completed_at": utcnow()})
                session.commit()

    async def _on_failed(self, record, ctrl: ArtifactControlMessage, peer: dict) -> None:
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            t = repo.get_by_transfer_id(ctrl.transfer_id)
            if (t is not None and t.direction == "outbound"
                    and t.state not in ("completed", "failed")):
                repo.update(ctrl.transfer_id, fields={
                    "state": "failed", "error_type": "remote_failed",
                    "last_error": _short(ctrl.reason),
                })
                session.commit()

    # -- serve (used by the authenticated binary endpoint) -----------------------

    def serve_range(self, *, sender_node_id: str, transfer_id: str, offset: int,
                    length: int) -> bytes:
        """Validate the outbound grant bound to ``sender_node_id`` and return a byte range.

        Raises :class:`ArtifactError` on any grant violation (mismatch/expiry/cancel/missing).
        Never serves anything but the exact granted object; never exposes a path.
        """
        now = utcnow()
        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            if t is None or t.direction != "outbound":
                raise ArtifactError("No such grant.", code="grant_not_found")
            if t.state in ("cancelled", "rejected", "expired", "failed"):
                raise ArtifactError("Grant is not usable.", code="grant_mismatch")
            if t.expected_receiver_node_id != sender_node_id:
                raise ArtifactError(
                    "Grant is bound to a different receiver.", code="grant_mismatch"
                )
            if t.grant_expires_at is not None and aware(t.grant_expires_at) <= now:
                raise ArtifactError("Grant has expired.", code="grant_expired")
            digest = t.expected_digest
        try:
            data = self.store.read_range(digest, offset=offset, length=length)
        except ArtifactStoreError as exc:
            raise ArtifactError(str(exc), code=exc.code) from exc
        with self.runtime.session_scope() as session:
            if t.state == "authorized":
                ArtifactTransferRepository(session).update(
                    transfer_id, fields={"state": "transferring"}
                )
                session.commit()
        return data

    def grant_total_size(self, transfer_id: str, sender_node_id: str) -> int:
        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            if t is None or t.expected_receiver_node_id != sender_node_id:
                raise ArtifactError("No such grant.", code="grant_not_found")
            return t.expected_size

    # -- helpers -----------------------------------------------------------------

    async def _send_grant(self, peer: dict, transfer, *, snapshot: dict | None = None) -> None:
        snap = snapshot or self._transfer_summary(transfer)
        control = ArtifactControlMessage(
            message_type=ArtifactControlType.GRANT, transfer_id=snap["transfer_id"],
            origin_artifact_id=snap.get("origin_artifact_id"), digest=snap["expected_digest"],
            size=snap["expected_size"], artifact_kind=snap.get("artifact_kind"),
            display_name=snap.get("display_name"), conversation_id=snap.get("conversation_id"),
            request_id=snap.get("request_id"), expiry=snap.get("grant_expires_at"),
            state="authorized", expected_sender=self.node_id,
            expected_receiver=peer["expected_node_id"],
        )
        await self._send_control(peer, control, conversation_id=snap.get("conversation_id"))

    async def _reject(self, peer: dict, ctrl: ArtifactControlMessage, reason: str) -> None:
        control = ArtifactControlMessage(
            message_type=ArtifactControlType.REJECT, transfer_id=ctrl.transfer_id,
            request_id=ctrl.request_id, state="rejected", reason=reason,
            expected_sender=self.node_id, expected_receiver=peer["expected_node_id"],
        )
        with contextlib.suppress(Exception):
            await self._send_control(peer, control, conversation_id=ctrl.conversation_id)
        await self._emit(ev.EVENT_ARTIFACT_TRANSFER_REJECTED,
                         f"Rejected inbound artifact request: {reason}.",
                         payload={"transfer_id": ctrl.transfer_id, "reason": reason})

    async def send_completion(
        self, transfer_id: str, *, ok: bool, reason: str | None = None
    ) -> None:
        """Notify the granting peer that an inbound transfer completed or failed."""
        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            peer = PeerRepository(session).get(t.peer_id) if t is not None else None
            peer_d = self.runtime.transport._peer_snapshot(peer) if peer is not None else None
            req_id = t.request_id if t is not None else None
        if peer_d is None:
            return
        control = ArtifactControlMessage(
            message_type=ArtifactControlType.COMPLETE if ok else ArtifactControlType.FAILED,
            transfer_id=transfer_id, request_id=req_id,
            state="completed" if ok else "failed", reason=_short(reason),
            expected_sender=self.node_id, expected_receiver=peer_d["expected_node_id"],
        )
        with contextlib.suppress(Exception):
            await self._send_control(peer_d, control)

    async def _send_control(self, peer: dict, control: ArtifactControlMessage,
                            *, conversation_id: str | None = None) -> str:
        identity = self.runtime.transport.require_identity()
        envelope = build_envelope(
            identity=identity, recipient_node_id=peer["expected_node_id"], recipient_agent_id=None,
            kind=MessageKind.AGENT_MESSAGE.value, payload=control.to_payload(),
            conversation_id=conversation_id, correlation_id=control.transfer_id,
            content_type="application/json",
        )
        with self.runtime.session_scope() as session:
            record = AgentOutboxRepository(session).create(
                message_id=envelope.message_id, peer_id=peer["id"],
                kind=MessageKind.AGENT_MESSAGE.value, envelope=envelope.authenticated_dict(),
                envelope_hash=envelope.canonical_hash(), conversation_id=conversation_id,
                correlation_id=control.transfer_id,
                max_attempts=self.runtime.config.agent_transport.outbound.max_attempts,
            )
            session.commit()
            record_id = record.id
        return record_id

    def _peer_or_raise(self, peer_id: str) -> dict:
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).get(peer_id)
            snap = self.runtime.transport._peer_snapshot(peer) if peer is not None else None
        if snap is None:
            raise ArtifactError(f"Unknown peer '{peer_id}'.", code="unknown_peer")
        if snap["trust_state"] != "trusted" or not snap["enabled"]:
            raise ArtifactError("Peer is not trusted or is disabled.", code="peer_not_trusted")
        if not snap["expected_node_id"] or not snap["endpoint_url"]:
            raise ArtifactError("Peer has no endpoint/node id.", code="no_endpoint")
        return snap

    @staticmethod
    def _peer_auth(peer) -> dict:
        return {
            "id": peer.id, "expected_node_id": peer.expected_node_id,
            "trust_state": peer.trust_state, "enabled": peer.enabled,
            "may_offer_artifacts": peer.may_offer_artifacts,
            "may_request_artifacts": peer.may_request_artifacts,
            "may_receive_artifacts": peer.may_receive_artifacts,
            "max_artifact_bytes": peer.max_artifact_bytes,
            "max_concurrent_transfers": peer.max_concurrent_transfers,
            "allowed_artifact_kinds_json": peer.allowed_artifact_kinds_json,
        }

    # -- read models -------------------------------------------------------------

    @staticmethod
    def _artifact_summary(a) -> dict:
        return {
            "artifact_id": a.id, "node_id": a.node_id, "origin_node_id": a.origin_node_id,
            "origin_artifact_id": a.origin_artifact_id, "backend_id": a.backend_id,
            "backend_version": a.backend_version,
            "backend_source_revision": a.backend_source_revision,
            "artifact_kind": a.artifact_kind, "media_type": a.media_type,
            "display_name": a.display_name, "size_bytes": a.size_bytes,
            "digest": a.digest, "relative_path": a.relative_path,
            "availability_state": a.availability_state, "pinned": a.pinned,
            "retention_state": a.retention_state, "conversation_id": a.conversation_id,
            "request_id": a.request_id, "mission_id": a.mission_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }

    @staticmethod
    def _transfer_summary(t) -> dict:
        return {
            "transfer_id": t.transfer_id, "direction": t.direction, "peer_id": t.peer_id,
            "state": t.state, "local_artifact_id": t.local_artifact_id,
            "origin_artifact_id": t.origin_artifact_id, "expected_digest": t.expected_digest,
            "expected_size": t.expected_size, "received_bytes": t.received_bytes,
            "artifact_kind": t.artifact_kind, "display_name": t.display_name,
            "conversation_id": t.conversation_id, "request_id": t.request_id,
            "mission_id": t.mission_id, "attempt_count": t.attempt_count,
            "error_type": t.error_type, "last_error": _short(t.last_error),
            "grant_expires_at": t.grant_expires_at.isoformat() if t.grant_expires_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    def list_artifacts(self, *, limit: int = 100) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = RFArtifactRepository(session).list(limit=limit)
            return [self._artifact_summary(a) for a in rows]

    def get_artifact(self, artifact_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            a = RFArtifactRepository(session).get(artifact_id)
            return self._artifact_summary(a) if a is not None else None

    def list_transfers(self, *, direction: str | None = None, state: str | None = None,
                       limit: int = 100) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = ArtifactTransferRepository(session).list(
                direction=direction, state=state, limit=limit
            )
            return [self._transfer_summary(t) for t in rows]

    def get_transfer(self, transfer_id: str) -> dict | None:
        from aithernet.state.repositories import ArtifactTransferAttemptRepository

        with self.runtime.session_scope() as session:
            t = ArtifactTransferRepository(session).get_by_transfer_id(transfer_id)
            if t is None:
                return None
            summary = self._transfer_summary(t)
            attempts = ArtifactTransferAttemptRepository(session).list_for_transfer(transfer_id)
            summary["attempts"] = [{
                "attempt_number": at.attempt_number, "start_offset": at.start_offset,
                "bytes_transferred": at.bytes_transferred, "outcome": at.outcome,
                "error_type": at.error_type,
                "started_at": at.started_at.isoformat() if at.started_at else None,
                "finished_at": at.finished_at.isoformat() if at.finished_at else None,
            } for at in attempts]
            return summary

    def list_remote_offers(self, *, limit: int = 100) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = RemoteArtifactReferenceRepository(session).list(limit=limit)
            return [{
                "id": r.id, "peer_id": r.peer_id, "origin_node_id": r.origin_node_id,
                "origin_artifact_id": r.origin_artifact_id, "digest": r.digest,
                "size_bytes": r.size_bytes, "artifact_kind": r.artifact_kind,
                "display_name": r.display_name, "state": r.state,
                "conversation_id": r.conversation_id,
                "offered_at": r.offered_at.isoformat() if r.offered_at else None,
            } for r in rows]

    async def set_pinned(self, artifact_id: str, pinned: bool) -> dict | None:
        with self.runtime.session_scope() as session:
            repo = RFArtifactRepository(session)
            repo.update(artifact_id, fields={"pinned": pinned,
                                            "retention_state": "pinned" if pinned else "default"})
            session.commit()
            a = repo.get(artifact_id)
            return self._artifact_summary(a) if a is not None else None

    def store_status(self) -> dict:
        return self.store.status()

    def coordinator_context(self, *, limit: int = 8) -> dict:
        """Compact artifact context for the coordinator (Part G). No bytes/paths/manifests."""
        status = self.store.status()
        with self.runtime.session_scope() as session:
            local = RFArtifactRepository(session).list(limit=limit)
            offers = RemoteArtifactReferenceRepository(session).list(limit=limit)
            transfers = ArtifactTransferRepository(session).list(limit=limit)
        return {
            "local_artifacts": [{
                "artifact_id": a.id, "kind": a.artifact_kind, "size_bytes": a.size_bytes,
                "availability": a.availability_state, "display_name": a.display_name,
            } for a in local],
            "remote_offers": [{
                "peer_id": r.peer_id, "origin_artifact_id": r.origin_artifact_id,
                "kind": r.artifact_kind, "size_bytes": r.size_bytes, "state": r.state,
            } for r in offers],
            "transfers": [{
                "transfer_id": t.transfer_id, "direction": t.direction, "state": t.state,
                "peer_id": t.peer_id, "received_bytes": t.received_bytes,
                "expected_size": t.expected_size,
            } for t in transfers],
            "store": {
                "used_bytes": status["used_bytes"],
                "available_in_quota_bytes": status["available_in_quota_bytes"],
                "free_disk_bytes": status["free_disk_bytes"],
            },
            "legend": {
                "note": "Artifact bytes are never shown here; request/offer by id only.",
            },
        }

    async def gc(self, *, dry_run: bool) -> dict:
        with self.runtime.session_scope() as session:
            keep = RFArtifactRepository(session).referenced_digests()
        result = self.store.gc(pinned_digests=keep, dry_run=dry_run)
        if not dry_run:
            await self._emit(ev.EVENT_ARTIFACT_STORE_GC, "Artifact store GC applied.",
                             payload={"removed": result["removed_objects"]})
        return result

    async def set_peer_artifact_permissions(self, peer_id: str, fields: dict) -> dict | None:
        from aithernet.state.repositories import ArtifactPermissionRepository

        with self.runtime.session_scope() as session:
            peer = ArtifactPermissionRepository(session).update(peer_id, fields=fields)
            session.commit()
            if peer is None:
                return None
        return self.peer_artifact_permissions(peer_id)

    def peer_artifact_permissions(self, peer_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            p = PeerRepository(session).get(peer_id)
            if p is None:
                return None
            return {
                "peer_id": p.id, "name": p.name, "trust_state": p.trust_state,
                "may_offer_artifacts": p.may_offer_artifacts,
                "may_request_artifacts": p.may_request_artifacts,
                "may_receive_artifacts": p.may_receive_artifacts,
                "max_artifact_bytes": p.max_artifact_bytes,
                "max_concurrent_transfers": p.max_concurrent_transfers,
                "allowed_artifact_kinds": p.allowed_artifact_kinds_json,
            }

    # -- recovery ----------------------------------------------------------------

    async def recover(self) -> None:
        """Expire stale grants and re-arm interrupted inbound transfers (idempotent)."""
        if not self.config.enabled:
            return
        now = utcnow()
        with self.runtime.session_scope() as session:
            repo = ArtifactTransferRepository(session)
            for t in repo.list(limit=1000):
                if t.state in ("authorized", "transferring", "paused") and (
                    t.grant_expires_at is not None and aware(t.grant_expires_at) <= now
                ):
                    repo.update(t.transfer_id, fields={"state": "expired",
                                                      "error_type": "grant_expired"})
                elif t.direction == "inbound" and t.state in ("transferring", "paused"):
                    repo.update(t.transfer_id, fields={"state": "authorized",
                                                      "claim_owner": None, "claim_expires_at": None,
                                                      "next_attempt_at": now})
            session.commit()
        self.runtime.schedule_artifact_worker()

    # -- events ------------------------------------------------------------------

    async def _emit(self, event_type: str, message: str, *, payload: dict | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=(payload or {}).get("mission_id"),
                source="artifacts", message=_short(message) or event_type, payload=payload or {},
            )


def _parse_iso(value: str | None):
    if not value:
        return None
    from datetime import datetime
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            from datetime import UTC
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None
