"""Authenticated remote mission-status synchronization (Stage 13D.3, Parts D–H, M, N).

A node reports its OWN local mission lifecycle to the peer that requested the mission, using the
existing signed Stage 13A durable outbox (no second transport, inbox, outbox, or mission engine).
The receiving node authenticates the sender, correlates the update to the local initiating
mission by request id, and keeps ONE current snapshot per remote relationship — updated only by
authenticated, in-order messages, never by inference. A remote update NEVER mutates a local
mission, never satisfies a reply wait, and never creates a MissionStep.

Determinism:
* sequence numbers are infrastructure-allocated, monotonic per (local mission, requesting peer),
  and persisted so they survive restart;
* re-publishing the same committed state is idempotent (no new sequence, no duplicate update);
* a duplicate received update is idempotent; an older sequence is ignored; a same-sequence
  message with a different payload is rejected and recorded;
* publication is triggered from committed mission transitions and reconciled on startup, so a
  restart never loses a status and never lets an older outbox retry replace newer accepted state.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aithernet.comms import status_events as sev
from aithernet.comms.status_protocol import (
    MISSION_STATUS_VERSION,
    MissionStatusError,
    MissionStatusMessage,
    MissionStatusType,
    classify_terminal,
    is_mission_status,
    parse_mission_status,
)
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    AgentOutboxRepository,
    ArtifactTransferRepository,
    InboundRequestRepository,
    LocalMissionStatusPublicationRepository,
    MissionRepository,
    PeerRepository,
    RemoteMissionSnapshotRepository,
    RemoteMissionStatusEventRepository,
    RFArtifactRepository,
)
from aithernet.transport.envelope import MessageKind, build_envelope

if TYPE_CHECKING:
    from datetime import datetime

    from aithernet.orchestrator.runtime import NodeRuntime

INBOUND_SOURCE_TYPE = "peer_request"
# Mission lifecycle event-type prefix that triggers a (reconciling, idempotent) publish. Status
# events use the "remote_mission." prefix, so publication is never triggered recursively.
_MISSION_EVENT_PREFIX = "mission."


def _short(value: str | None, limit: int = 240) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import UTC
        return dt.replace(tzinfo=UTC)
    return dt


class MissionStatusService:
    """Publishes local mission status to peers and persists authenticated remote snapshots."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.config = runtime.config.communication
        self.node_id = runtime.config.node_id

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.config.mission_status_enabled

    # == PUBLISH (this node reports its own inbound mission) =====================

    async def maybe_publish_from_event(self, event_type: str, mission_id: str | None) -> None:
        """Trigger hook from ``runtime._emit_event``: reconcile + publish on a mission transition.

        Idempotent and cheap for the common case — a non-peer-request mission returns after one
        lookup. Status events do not match the ``mission.`` prefix, so this never recurses.
        """
        if not self.enabled or not mission_id:
            return
        if not event_type.startswith(_MISSION_EVENT_PREFIX):
            return
        with contextlib.suppress(Exception):
            await self.publish_for_mission(mission_id)

    async def publish_for_mission(self, mission_id: str) -> str | None:
        """Publish the current committed state of a reportable inbound mission (Parts D/E).

        Reportable = a ``peer_request`` mission whose originating peer is trusted, enabled, and
        authorized to receive our status (``may_receive_mission_status``). Returns the queued
        outbox message id, or None when nothing was published (not reportable / unchanged).
        """
        if not self.enabled:
            return None
        message_id = None
        with self.runtime.session_scope() as session:
            mission = MissionRepository(session).get(mission_id)
            if mission is None or mission.source_type != INBOUND_SOURCE_TYPE:
                return None
            meta = mission.metadata_json or {}
            peer_id = meta.get("peer_id")
            request_id = meta.get("request_id")
            conversation_id = meta.get("conversation_id")
            if not peer_id:
                return None
            peer = PeerRepository(session).get(peer_id)
            if (
                peer is None or peer.trust_state != "trusted" or not peer.enabled
                or not peer.may_receive_mission_status
            ):
                return None
            state = mission.status
            pub = LocalMissionStatusPublicationRepository(session).get_or_create(
                mission_id=mission_id, peer_id=peer_id, request_id=request_id,
                conversation_id=conversation_id,
            )
            if pub.last_state == state:
                return None  # idempotent: this state is already published
            sequence = pub.last_sequence + 1

            obligation = self._obligation_for(session, request_id)
            artifact_ids, transfer_ids = self._links_for(session, mission_id)
            message = MissionStatusMessage(
                application_version=MISSION_STATUS_VERSION,
                message_type=MissionStatusType.UPDATE,
                origin_node_id=self.node_id,
                remote_mission_id=mission_id,
                inbound_request_id=request_id,
                parent_request_id=meta.get("parent_request_id"),
                conversation_id=conversation_id,
                state=state,
                response_obligation=obligation,
                sequence=sequence,
                mission_updated_at=_aware(mission.updated_at).isoformat()
                if mission.updated_at else None,
                emitted_at=utcnow().isoformat(),
                progress_summary=_short(meta.get("progress_summary"), 280),
                terminal_category=classify_terminal(state),
                artifact_ids=artifact_ids,
                artifact_count=len(artifact_ids),
                transfer_ids=transfer_ids,
                transfer_count=len(transfer_ids),
            )
            record_id = self._queue_outbox(session, peer, message, conversation_id, request_id)
            message_id = record_id
            pub.last_sequence = sequence
            pub.last_state = state
            pub.last_message_id = record_id
            session.flush()
            session.commit()

        await self._emit(
            sev.EVENT_STATUS_QUEUED,
            f"Queued mission-status update seq={sequence} state={state}.",
            mission_id=mission_id,
            payload={"peer_id": peer_id, "state": state, "sequence": sequence,
                     "request_id": request_id},
        )
        return message_id

    def _obligation_for(self, session, request_id: str | None) -> str:
        if not request_id:
            return "none"
        ir = InboundRequestRepository(session).get_by_request_id(request_id)
        if ir is None:
            return "none"
        if ir.response_message_id:
            return "queued"
        return "required" if ir.response_required else "none"

    def _links_for(self, session, mission_id: str) -> tuple[list[str], list[str]]:
        artifacts = RFArtifactRepository(session).list(mission_id=mission_id, limit=50)
        artifact_ids = [a.id for a in artifacts][:50]
        transfers = [
            t for t in ArtifactTransferRepository(session).list(limit=200)
            if t.mission_id == mission_id
        ]
        transfer_ids = [t.transfer_id for t in transfers][:50]
        return artifact_ids, transfer_ids

    def _queue_outbox(self, session, peer, message: MissionStatusMessage,
                      conversation_id: str | None, request_id: str | None) -> str:
        """Build a SIGNED envelope and persist ONE durable outbox record (no mission step)."""
        identity = self.runtime.transport.require_identity()
        envelope = build_envelope(
            identity=identity, recipient_node_id=peer.expected_node_id, recipient_agent_id=None,
            kind=MessageKind.AGENT_MESSAGE.value, payload=message.to_payload(),
            conversation_id=conversation_id, correlation_id=request_id,
            content_type="application/json",
        )
        record = AgentOutboxRepository(session).create(
            message_id=envelope.message_id, peer_id=peer.id,
            kind=MessageKind.AGENT_MESSAGE.value, envelope=envelope.authenticated_dict(),
            envelope_hash=envelope.canonical_hash(), conversation_id=conversation_id,
            correlation_id=request_id, application_type="mission_status",
            max_attempts=self.runtime.config.agent_transport.outbound.max_attempts,
        )
        return record.id

    # == RECEIVE (this node receives a peer's status about the peer's mission) ====

    async def on_inbound_status(self, record, peer_snapshot: dict) -> bool:
        """Process one inbound mission-status message. Returns True if it was a status message.

        Idempotency is guaranteed by Stage 13A (duplicate envelopes never reach here). This never
        touches a local mission, never satisfies a reply wait, and never creates a MissionStep.
        """
        if not self.enabled:
            return False
        payload = dict(record.payload_json or {})
        if not is_mission_status(payload):
            return False
        try:
            message = parse_mission_status(payload)
        except MissionStatusError as exc:
            await self._record_event(
                None, peer_snapshot, message=None, disposition="malformed_rejected",
                reason=exc.code, inbox_id=record.id, sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return True

        if message.message_type is MissionStatusType.QUERY:
            await self._on_query(record, message, peer_snapshot)
        else:  # UPDATE or RESPONSE both update the snapshot
            await self._on_update(record, message, peer_snapshot)
        return True

    async def _on_update(self, record, message: MissionStatusMessage, peer: dict) -> None:
        peer_id = peer.get("id")
        # Authorization: this peer must be allowed to publish status to us (separate from trust).
        if not peer.get("may_publish_mission_status"):
            await self._record_event(
                None, peer, message, disposition="unauthorized_rejected",
                reason="peer not authorized to publish status", inbox_id=record.id,
                sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return
        # A node may only report its OWN mission: the payload origin must be the sender's node.
        if message.origin_node_id != peer.get("expected_node_id"):
            await self._record_event(
                None, peer, message, disposition="unauthorized_rejected",
                reason="origin node mismatch", inbox_id=record.id,
                sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return
        remote_ref = message.remote_mission_id
        if not remote_ref or not message.state:
            await self._record_event(
                None, peer, message, disposition="malformed_rejected",
                reason="missing remote mission id or state", inbox_id=record.id,
                sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return

        now = utcnow()
        deadline = self._freshness_deadline(message.state, now)
        terminal = classify_terminal(message.state)
        disposition = "accepted"
        reason = None
        created = False
        sev_type = sev.EVENT_STATUS_ACCEPTED
        snapshot_id = None
        with self.runtime.session_scope() as session:
            local_mission_id, conversation_id = self._correlate_local(
                session, message, peer_id
            )
            snap_repo = RemoteMissionSnapshotRepository(session)
            snap = snap_repo.find(peer_id=peer_id, remote_mission_ref=remote_ref)
            if snap is None:
                snap = snap_repo.create(
                    origin_node_id=message.origin_node_id, peer_id=peer_id,
                    remote_mission_ref=remote_ref, request_id=message.inbound_request_id,
                    parent_request_id=message.parent_request_id,
                    conversation_id=conversation_id, local_mission_id=local_mission_id,
                    latest_sequence=message.sequence, latest_state=message.state,
                    response_obligation=message.response_obligation,
                    progress_summary=_short(message.progress_summary, 320),
                    terminal_category=terminal,
                    artifact_count=message.artifact_count,
                    transfer_count=message.transfer_count,
                    artifact_ids_json=message.artifact_ids or None,
                    transfer_ids_json=message.transfer_ids or None,
                    remote_updated_at=_parse_iso(message.mission_updated_at),
                    received_at=now, freshness_deadline=deadline,
                    freshness="terminal" if terminal else "fresh",
                    protocol_version=message.application_version,
                )
                created = True
                sev_type = sev.EVENT_SNAPSHOT_CREATED
            elif message.sequence < snap.latest_sequence:
                disposition = "older_sequence_ignored"
                reason = f"seq {message.sequence} < {snap.latest_sequence}"
                sev_type = sev.EVENT_STATUS_OLDER_IGNORED
            elif message.sequence == snap.latest_sequence:
                if message.state == snap.latest_state:
                    disposition = "duplicate"
                    sev_type = sev.EVENT_STATUS_DUPLICATE
                    # A query RESPONSE confirming the current state answers a pending query.
                    if message.message_type is MissionStatusType.RESPONSE:
                        snap.query_pending = False
                else:
                    disposition = "invalid_transition_rejected"
                    reason = "same sequence, conflicting payload"
                    sev_type = sev.EVENT_STATUS_REJECTED
            else:  # strictly newer sequence -> accept
                snap.latest_sequence = message.sequence
                snap.latest_state = message.state
                snap.response_obligation = message.response_obligation
                snap.progress_summary = _short(message.progress_summary, 320)
                snap.terminal_category = terminal
                snap.artifact_count = message.artifact_count
                snap.transfer_count = message.transfer_count
                snap.artifact_ids_json = message.artifact_ids or None
                snap.transfer_ids_json = message.transfer_ids or None
                snap.remote_updated_at = _parse_iso(message.mission_updated_at)
                snap.received_at = now
                snap.freshness_deadline = deadline
                snap.freshness = "terminal" if terminal else "fresh"
                snap.stale_notified = False
                snap.query_pending = False
                if local_mission_id and not snap.local_mission_id:
                    snap.local_mission_id = local_mission_id
                if conversation_id and not snap.conversation_id:
                    snap.conversation_id = conversation_id
                sev_type = sev.EVENT_SNAPSHOT_UPDATED
            snapshot_id = snap.id
            RemoteMissionStatusEventRepository(session).create(
                snapshot_id=snapshot_id, peer_id=peer_id, remote_mission_ref=remote_ref,
                request_id=message.inbound_request_id, sequence=message.sequence,
                state=message.state, message_type=message.message_type.value,
                disposition=disposition, reason=_short(reason), inbox_message_id=record.id,
                received_at=now,
            )
            session.commit()

        # Emit the disposition-specific event first, then snapshot.created/updated when accepted.
        if disposition not in ("accepted",):
            await self._emit(
                sev_type, f"Remote status {disposition}.",
                mission_id=local_mission_id,
                payload={"snapshot_id": snapshot_id, "peer_id": peer_id,
                         "sequence": message.sequence, "state": message.state,
                         "disposition": disposition},
            )
        else:
            await self._emit(
                sev.EVENT_STATUS_ACCEPTED, "Remote status accepted.",
                mission_id=local_mission_id,
                payload={"snapshot_id": snapshot_id, "peer_id": peer_id,
                         "sequence": message.sequence, "state": message.state},
            )
            await self._emit(
                sev.EVENT_SNAPSHOT_CREATED if created else sev.EVENT_SNAPSHOT_UPDATED,
                "Remote snapshot created." if created else "Remote snapshot updated.",
                mission_id=local_mission_id,
                payload={"snapshot_id": snapshot_id, "state": message.state,
                         "terminal": bool(terminal)},
            )

    def _correlate_local(self, session, message: MissionStatusMessage,
                         peer_id: str) -> tuple[str | None, str | None]:
        """Correlate the remote update to OUR initiating mission by request id (provable only).

        An unrelated request id (or one belonging to a different peer) does not attach to a
        local mission/conversation — it is left uncorrelated rather than mis-merged.
        """
        request_id = message.inbound_request_id
        if not request_id:
            return None, message.conversation_id
        out = AgentOutboxRepository(session).first_by_application_request_id(request_id)
        if out is not None and out.peer_id == peer_id:
            return out.mission_id, out.conversation_id or message.conversation_id
        return None, message.conversation_id

    # == QUERY (peer asks us; we respond from persisted local fact) ==============

    async def _on_query(self, record, message: MissionStatusMessage, peer: dict) -> None:
        peer_id = peer.get("id")
        if not peer.get("may_query_mission_status"):
            await self._record_event(
                None, peer, message, disposition="unauthorized_rejected",
                reason="peer not authorized to query status", inbox_id=record.id,
                sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return
        await self._emit(
            sev.EVENT_STATUS_QUERY_RECEIVED, "Mission-status query received.",
            payload={"peer_id": peer_id, "request_id": message.inbound_request_id},
        )
        # Resolve the local mission this query refers to via the inbound request id only.
        with self.runtime.session_scope() as session:
            ir = (
                InboundRequestRepository(session).get_by_request_id(message.inbound_request_id)
                if message.inbound_request_id else None
            )
            mission_id = ir.mission_id if ir is not None and ir.peer_id == peer_id else None
        if mission_id is None:
            # Unknown/unrelated mission -> bounded factual rejection, no enumeration possible.
            await self._record_event(
                None, peer, message, disposition="unknown_mission_rejected",
                reason="no local mission for this request", inbox_id=record.id,
                sev_type=sev.EVENT_STATUS_REJECTED,
            )
            return
        # Respond from the current published fact (re-query after completion creates no
        # transition: it forces a fresh RESPONSE with the current sequence/state).
        await self._respond_to_query(mission_id)

    async def _respond_to_query(self, mission_id: str) -> None:
        if not self.enabled:
            return
        with self.runtime.session_scope() as session:
            mission = MissionRepository(session).get(mission_id)
            if mission is None or mission.source_type != INBOUND_SOURCE_TYPE:
                return
            meta = mission.metadata_json or {}
            peer_id = meta.get("peer_id")
            request_id = meta.get("request_id")
            conversation_id = meta.get("conversation_id")
            peer = PeerRepository(session).get(peer_id) if peer_id else None
            if peer is None or not peer.may_receive_mission_status:
                return
            pub = LocalMissionStatusPublicationRepository(session).get_or_create(
                mission_id=mission_id, peer_id=peer_id, request_id=request_id,
                conversation_id=conversation_id,
            )
            # A response reflects the current committed state with the latest published sequence;
            # if nothing has been published yet, allocate the first sequence for this state.
            if pub.last_state != mission.status:
                pub.last_sequence += 1
                pub.last_state = mission.status
            sequence = pub.last_sequence or 1
            obligation = self._obligation_for(session, request_id)
            artifact_ids, transfer_ids = self._links_for(session, mission_id)
            message = MissionStatusMessage(
                application_version=MISSION_STATUS_VERSION,
                message_type=MissionStatusType.RESPONSE,
                origin_node_id=self.node_id, remote_mission_id=mission_id,
                inbound_request_id=request_id, conversation_id=conversation_id,
                state=mission.status, response_obligation=obligation, sequence=sequence,
                mission_updated_at=_aware(mission.updated_at).isoformat()
                if mission.updated_at else None,
                emitted_at=utcnow().isoformat(),
                terminal_category=classify_terminal(mission.status),
                artifact_ids=artifact_ids, artifact_count=len(artifact_ids),
                transfer_ids=transfer_ids, transfer_count=len(transfer_ids),
            )
            record_id = self._queue_outbox(session, peer, message, conversation_id, request_id)
            pub.last_message_id = record_id
            session.flush()
            session.commit()
        await self._emit(
            sev.EVENT_STATUS_RESPONSE_QUEUED, "Mission-status response queued.",
            mission_id=mission_id,
            payload={"peer_id": peer_id, "state": message.state, "sequence": sequence},
        )

    async def query_snapshot(self, snapshot_id: str) -> bool:
        """Operator/coordinator action: ask the peer for the latest status of a known snapshot."""
        if not self.enabled:
            return False
        with self.runtime.session_scope() as session:
            snap = RemoteMissionSnapshotRepository(session).get(snapshot_id)
            if snap is None:
                return False
            peer = PeerRepository(session).get(snap.peer_id)
            if peer is None or peer.trust_state != "trusted" or not peer.enabled:
                return False
            # We must be authorized to receive status from this peer to make use of a response.
            message = MissionStatusMessage(
                application_version=MISSION_STATUS_VERSION,
                message_type=MissionStatusType.QUERY, origin_node_id=self.node_id,
                remote_mission_id=snap.remote_mission_ref, inbound_request_id=snap.request_id,
                conversation_id=snap.conversation_id,
            )
            self._queue_outbox(session, peer, message, snap.conversation_id, snap.request_id)
            snap.query_pending = True
            session.flush()
            session.commit()
            peer_id = snap.peer_id
        await self._emit(
            sev.EVENT_STATUS_QUERY_QUEUED, "Mission-status query queued.",
            payload={"snapshot_id": snapshot_id, "peer_id": peer_id},
        )
        return True

    # == FRESHNESS (Part G) ======================================================

    def _freshness_deadline(self, state: str, now: datetime) -> datetime | None:
        from datetime import timedelta
        if classify_terminal(state):
            return None
        return now + timedelta(seconds=self.config.status_freshness_seconds)

    def classify_freshness(self, snap) -> str:
        """unknown is for a missing snapshot; here we return fresh/stale/terminal from persisted
        timestamps (never inferring terminal/completed/failed from staleness)."""
        if snap.terminal_category:
            return "terminal"
        deadline = _aware(snap.freshness_deadline)
        if deadline is not None and utcnow() > deadline:
            return "stale"
        return "fresh"

    # == RECOVERY (Part N) =======================================================

    async def recover(self) -> None:
        """Reconcile unsent status on startup. Unsent updates already sit in the durable outbox
        (the delivery worker redelivers them); here we re-publish the CURRENT state of every
        reportable mission so a transition committed just before a crash is not lost. Idempotent —
        a mission already published at its current state is skipped, and an older outbox retry can
        never replace newer accepted remote state (the receiver enforces sequence order)."""
        if not self.enabled:
            return
        with self.runtime.session_scope() as session:
            missions = [
                m for m in MissionRepository(session).list(limit=1000)
                if m.source_type == INBOUND_SOURCE_TYPE
            ]
        for mission in missions:
            with contextlib.suppress(Exception):
                await self.publish_for_mission(mission.id)

    # == READ HELPERS (API/CLI/dashboard/coordinator) ============================

    def list_snapshots(self, *, peer_id: str | None = None, local_mission_id: str | None = None,
                       limit: int = 100) -> list[dict]:
        out = []
        with self.runtime.session_scope() as session:
            rows = RemoteMissionSnapshotRepository(session).list(
                peer_id=peer_id, local_mission_id=local_mission_id, limit=limit
            )
            for snap in rows:
                out.append(self._snapshot_summary(snap))
        return out

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            snap = RemoteMissionSnapshotRepository(session).get(snapshot_id)
            return self._snapshot_summary(snap) if snap is not None else None

    def snapshot_events(self, snapshot_id: str, *, limit: int = 200) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = RemoteMissionStatusEventRepository(session).list_for_snapshot(
                snapshot_id, limit=limit
            )
            return [{
                "event_id": e.id, "sequence": e.sequence, "state": e.state,
                "message_type": e.message_type, "disposition": e.disposition,
                "reason": _short(e.reason), "peer_id": e.peer_id,
                "received_at": e.received_at.isoformat() if e.received_at else None,
            } for e in rows]

    def list_publications(self, mission_id: str) -> list[dict]:
        with self.runtime.session_scope() as session:
            rows = LocalMissionStatusPublicationRepository(session).list_for_mission(mission_id)
            return [{
                "publication_id": p.id, "mission_id": p.mission_id, "peer_id": p.peer_id,
                "request_id": p.request_id, "last_sequence": p.last_sequence,
                "last_state": p.last_state,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            } for p in rows]

    def coordinator_context(self, *, limit: int = 20) -> dict:
        """Compact, sanitized remote-mission facts for the coordinator (Part I)."""
        snapshots = []
        with self.runtime.session_scope() as session:
            rows = RemoteMissionSnapshotRepository(session).list(limit=limit)
            for snap in rows:
                snapshots.append({
                    "peer_id": snap.peer_id,
                    "remote_mission_ref": snap.remote_mission_ref,
                    "request_id": snap.request_id,
                    "conversation_id": snap.conversation_id,
                    "local_mission_id": snap.local_mission_id,
                    "state": snap.latest_state,
                    "freshness": self.classify_freshness(snap),
                    "latest_sequence": snap.latest_sequence,
                    "response_obligation": snap.response_obligation,
                    "artifact_count": snap.artifact_count,
                    "transfer_count": snap.transfer_count,
                    "last_received_at": snap.received_at.isoformat() if snap.received_at else None,
                    "query_pending": snap.query_pending,
                })
        return {"remote_missions": snapshots}

    def peer_permissions(self, peer_id: str) -> dict | None:
        with self.runtime.session_scope() as session:
            peer = PeerRepository(session).get(peer_id)
            if peer is None:
                return None
            return {
                "peer_id": peer.id, "name": peer.name, "trust_state": peer.trust_state,
                "enabled": peer.enabled,
                "may_publish_mission_status": peer.may_publish_mission_status,
                "may_query_mission_status": peer.may_query_mission_status,
                "may_receive_mission_status": peer.may_receive_mission_status,
                "max_active_remote_snapshots": peer.max_active_remote_snapshots,
            }

    async def set_peer_permissions(self, peer_id: str, fields: dict) -> dict | None:
        from aithernet.state.repositories import MissionStatusPermissionRepository
        update = {k: v for k, v in fields.items() if v is not None}
        with self.runtime.session_scope() as session:
            peer = MissionStatusPermissionRepository(session).update(peer_id, fields=update)
            session.commit()
            if peer is None:
                return None
        return self.peer_permissions(peer_id)

    def _snapshot_summary(self, snap) -> dict:
        freshness = self.classify_freshness(snap)
        return {
            "snapshot_id": snap.id, "origin_node_id": snap.origin_node_id,
            "peer_id": snap.peer_id, "remote_mission_ref": snap.remote_mission_ref,
            "request_id": snap.request_id, "parent_request_id": snap.parent_request_id,
            "conversation_id": snap.conversation_id, "local_mission_id": snap.local_mission_id,
            "latest_sequence": snap.latest_sequence, "state": snap.latest_state,
            "response_obligation": snap.response_obligation,
            "progress_summary": _short(snap.progress_summary, 320),
            "terminal_category": snap.terminal_category, "freshness": freshness,
            "artifact_count": snap.artifact_count, "transfer_count": snap.transfer_count,
            "artifact_ids": snap.artifact_ids_json or [],
            "transfer_ids": snap.transfer_ids_json or [],
            "remote_updated_at": snap.remote_updated_at.isoformat()
            if snap.remote_updated_at else None,
            "received_at": snap.received_at.isoformat() if snap.received_at else None,
            "freshness_deadline": snap.freshness_deadline.isoformat()
            if snap.freshness_deadline else None,
            "query_pending": snap.query_pending,
            "protocol_version": snap.protocol_version,
        }

    # == internal ================================================================

    async def _record_event(self, snapshot_id, peer, message, *, disposition, reason,
                            inbox_id, sev_type) -> None:
        with self.runtime.session_scope() as session:
            RemoteMissionStatusEventRepository(session).create(
                snapshot_id=snapshot_id, peer_id=peer.get("id"),
                remote_mission_ref=message.remote_mission_id if message else None,
                request_id=message.inbound_request_id if message else None,
                sequence=message.sequence if message else None,
                state=message.state if message else None,
                message_type=message.message_type.value if message else None,
                disposition=disposition, reason=_short(reason), inbox_message_id=inbox_id,
            )
            session.commit()
        await self._emit(
            sev_type, f"Remote status {disposition}.",
            payload={"peer_id": peer.get("id"), "disposition": disposition,
                     "reason": _short(reason)},
        )

    async def _emit(self, event_type: str, message: str, *, mission_id: str | None = None,
                    payload: dict | None = None) -> None:
        with contextlib.suppress(Exception):
            await self.runtime._emit_event(
                event_type=event_type, mission_id=mission_id, source="mission_status",
                message=_short(message) or event_type, payload=payload or {},
            )


def _parse_iso(value: str | None):
    if not value:
        return None
    from datetime import datetime
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value)
    return None
