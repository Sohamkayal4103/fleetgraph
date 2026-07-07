"""Aggregate read-model builder for the Stage 13C operator dashboard.

Every method here is READ-ONLY. It opens at most a couple of bounded sessions, reuses the
existing summary builders (so the ACK-vs-semantic-reply and trust-vs-authorization
distinctions are preserved exactly), and returns plain JSON-able dicts that the typed API
schemas validate. Message *content* is surfaced only as a bounded, escaped preview extracted
from the application payload — never the raw signed envelope, signature, keys, or headers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aithernet.comms import events as ev
from aithernet.state.repositories import (
    AgentInboxRepository,
    AgentOutboxRepository,
    ArtifactTransferRepository,
    EventRepository,
    InboundRequestRepository,
    MissionExecutionRunRepository,
    MissionReplyWaitRepository,
    MissionRepository,
    MissionStepRepository,
    PeerRepository,
    RFArtifactRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

# Bounded preview limits (sanitized application content only — never the full payload).
TEXT_PREVIEW_CHARS = 280
DATA_PREVIEW_CHARS = 600
RECENT_EVENT_WINDOW = 200
RECENT_FAILURE_LIMIT = 10

# Event types that represent a failure/degradation in each category (for recent-failure rollups).
_COMMS_FAILURE_EVENTS = {
    ev.EVENT_REPLY_WAIT_TIMED_OUT,
    ev.EVENT_PEER_REPLY_IGNORED,
    ev.EVENT_INBOUND_REQUEST_REJECTED,
    ev.EVENT_INBOUND_REQUEST_DUPLICATE,
    ev.EVENT_RESUME_SKIPPED,
}


def _preview_text(value: object) -> str | None:
    """Return a bounded, single-line preview of an application text field."""
    if not isinstance(value, str) or not value:
        return None
    flat = " ".join(value.split())
    return flat if len(flat) <= TEXT_PREVIEW_CHARS else flat[:TEXT_PREVIEW_CHARS] + "…"


def _preview_data(value: object) -> str | None:
    """Return a bounded JSON preview of an application structured-data field (escaped on render)."""
    if not isinstance(value, dict) or not value:
        return None
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return encoded if len(encoded) <= DATA_PREVIEW_CHARS else encoded[:DATA_PREVIEW_CHARS] + "…"


def _out_payload(record) -> dict:
    """The application payload of an outbound message (inside the signed envelope), or {}."""
    envelope = record.envelope_json if isinstance(record.envelope_json, dict) else {}
    payload = envelope.get("payload")
    return payload if isinstance(payload, dict) else {}


def _in_payload(record) -> dict:
    """The application payload of an inbound message (already extracted), or {}."""
    return record.payload_json if isinstance(record.payload_json, dict) else {}


def _abbrev_fingerprint(fingerprint: str | None) -> str | None:
    """Abbreviate a fingerprint to ``sha256:<first 16>`` — never the full value, never a key."""
    if not fingerprint:
        return None
    body = fingerprint.split(":", 1)[-1]
    return f"sha256:{body[:16]}" if body else None


def _peer_transport_health(peer) -> str:
    """Derive a peer's transport health from persisted contact records (never fabricated)."""
    if peer.trust_state == "revoked" or not peer.enabled:
        return "disabled"
    if peer.last_success_at is None and peer.last_failure_at is None:
        return "unknown"
    if peer.last_failure_at is not None and (
        peer.last_success_at is None or peer.last_failure_at > peer.last_success_at
    ):
        return "failing"
    return "healthy"


class DashboardService:
    """Builds bounded, sanitized aggregate read models from persisted state."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime

    # -- overview ----------------------------------------------------------------

    async def overview(self) -> dict:
        """Node identity + health + worker/coordinator/RF/comms rollup (Part C)."""
        rt = self.runtime
        node = rt.status().model_dump(mode="json")
        coordinator = rt.coordinator.status().model_dump(mode="json")
        coding = rt.coding_agent_status().model_dump(mode="json")
        worker = rt.mission_worker_status().model_dump(mode="json")
        transport = rt.transport.transport_status()
        identity = transport.get("identity") or {}
        rf_statuses = await rt.rf.list_statuses()

        with rt.session_scope() as session:
            mission_counts = MissionRepository(session).counts_by_status()
            run_counts = MissionExecutionRunRepository(session).counts_by_status()
            wait_counts = MissionReplyWaitRepository(session).counts_by_state()
            inbound_counts = InboundRequestRepository(session).counts_by_state()
            recent_events = EventRepository(session).list(limit=RECENT_EVENT_WINDOW)
            failures = self._recent_failures(recent_events)

        outbox_counts = transport.get("outbox_counts") or {}
        return {
            "node": {
                "node_id": node.get("node_id"),
                "node_name": node.get("node_name"),
                "runtime_status": node.get("runtime_status"),
                "version": node.get("version"),
                "started_at": node.get("started_at"),
                "mission_count": node.get("mission_count"),
                "event_count": node.get("event_count"),
                "identity_initialized": bool(identity.get("initialized")),
                "fingerprint": _abbrev_fingerprint(identity.get("fingerprint")),
            },
            "workers": {
                "mission": {
                    "enabled": worker.get("enabled"), "running": worker.get("running"),
                    "degraded": worker.get("degraded"),
                    "active_count": worker.get("active_count"),
                    "queued_count": worker.get("queued_count"),
                },
                "transport": {
                    "enabled": (transport.get("worker") or {}).get("enabled"),
                    "running": (transport.get("worker") or {}).get("running"),
                    "degraded": (transport.get("worker") or {}).get("degraded"),
                    "in_flight": (transport.get("worker") or {}).get("in_flight"),
                },
            },
            "coordinator": {
                "provider": coordinator.get("provider"),
                "configured": coordinator.get("configured"),
                "missing_configuration": coordinator.get("missing_configuration") or [],
            },
            "coding_agent": {
                "provider": coding.get("provider"),
                "configured": coding.get("configured"),
                "missing_configuration": coding.get("missing_configuration") or [],
            },
            "rf_backends": [
                {
                    "backend_id": s.backend_id, "state": getattr(s.state, "value", s.state),
                    "tool_count": s.tool_count, "experimental": s.experimental,
                    "version": s.version, "last_error_type": s.last_error_type,
                }
                for s in rf_statuses
            ],
            "peers": {
                "total": transport.get("peer_count", 0),
                "trusted": transport.get("trusted_peer_count", 0),
            },
            "outbox": {
                "pending": int(outbox_counts.get("pending", 0)),
                "in_flight": int(outbox_counts.get("in_flight", 0)),
                "delivered": int(outbox_counts.get("delivered", 0)),
                "acknowledged": int(outbox_counts.get("acknowledged", 0)),
                "failed": int(outbox_counts.get("failed", 0)),
                "dead_letter": int(outbox_counts.get("dead_letter", 0)),
            },
            "inbox_count": transport.get("inbox_count", 0),
            "missions": {
                "by_status": mission_counts,
                "active": int(run_counts.get("active", 0)),
                "waiting": int(run_counts.get("waiting", 0)),
                "queued": int(run_counts.get("queued", 0)),
            },
            "reply_waits": {
                "pending": int(wait_counts.get("pending", 0)),
                "by_state": wait_counts,
            },
            "inbound_requests": {
                "total": sum(inbound_counts.values()),
                "by_state": inbound_counts,
            },
            "recent_failures": failures,
        }

    def _recent_failures(self, events) -> dict:
        comms, rf, mission = [], [], []
        for e in events:
            entry = {
                "event_type": e.event_type, "source": e.source,
                "message": _preview_text(e.message), "mission_id": e.mission_id,
                "at": e.created_at.isoformat() if e.created_at else None,
            }
            etype = e.event_type
            if e.event_type in _COMMS_FAILURE_EVENTS or etype.startswith("inbound_request."):
                if "fail" in etype or "reject" in etype or "timed_out" in etype \
                        or "duplicate" in etype or "ignored" in etype or "skipped" in etype:
                    comms.append(entry)
            elif e.source == "rf" or etype.startswith("rf."):
                if "fail" in etype or "error" in etype or "degraded" in etype:
                    rf.append(entry)
            elif etype.startswith("mission.") and ("fail" in etype or "blocked" in etype):
                mission.append(entry)
        return {
            "communication": comms[:RECENT_FAILURE_LIMIT],
            "rf": rf[:RECENT_FAILURE_LIMIT],
            "mission": mission[:RECENT_FAILURE_LIMIT],
        }

    # -- fleet -------------------------------------------------------------------

    def fleet(self, *, limit: int = 200) -> dict:
        """Per-peer aggregate: trust, permissions (separate), health, manifest, linkage (Part D)."""
        with self.runtime.session_scope() as session:
            peers = PeerRepository(session).list(limit=limit)
            outbox = AgentOutboxRepository(session).list(limit=1000)
            inbox = AgentInboxRepository(session).list(limit=1000)
            waits = MissionReplyWaitRepository(session).list(state="pending", limit=1000)

        # Aggregate per-peer in memory (no N+1).
        conv_by_peer: dict[str, set[str]] = {}
        outstanding_by_peer: dict[str, int] = {}
        for o in outbox:
            if o.conversation_id:
                conv_by_peer.setdefault(o.peer_id, set()).add(o.conversation_id)
            if o.mission_id and o.acknowledged_at is None and o.status not in ("dead_letter",):
                outstanding_by_peer[o.peer_id] = outstanding_by_peer.get(o.peer_id, 0) + 1
        for i in inbox:
            if i.peer_id and i.conversation_id:
                conv_by_peer.setdefault(i.peer_id, set()).add(i.conversation_id)
        waits_by_peer: dict[str, int] = {}
        for w in waits:
            waits_by_peer[w.expected_peer_id] = waits_by_peer.get(w.expected_peer_id, 0) + 1

        entries = []
        for p in peers:
            caps = None
            if p.capability_snapshot_json:
                caps = (p.capability_snapshot_json or {}).get("capabilities")
            entries.append({
                "peer_id": p.id, "name": p.name, "role": p.role,
                "trust_state": p.trust_state, "enabled": p.enabled,
                "transport_health": _peer_transport_health(p),
                "last_success_at": p.last_success_at.isoformat() if p.last_success_at else None,
                "last_failure_at": p.last_failure_at.isoformat() if p.last_failure_at else None,
                "consecutive_failures": p.consecutive_failures,
                "fingerprint": _abbrev_fingerprint(p.fingerprint),
                "manifest_at": (
                    p.capability_snapshot_at.isoformat() if p.capability_snapshot_at else None
                ),
                "capabilities": caps,
                "permissions": {
                    "may_send_requests": p.may_send_requests,
                    "may_send_replies": p.may_send_replies,
                    "may_receive_messages": p.may_receive_messages,
                    "may_request_response": p.may_request_response,
                    "max_inbound_request_bytes": p.max_inbound_request_bytes,
                    "max_concurrent_inbound_missions": p.max_concurrent_inbound_missions,
                },
                "conversation_count": len(conv_by_peer.get(p.id, ())),
                "outstanding_messages": outstanding_by_peer.get(p.id, 0),
                "pending_reply_waits": waits_by_peer.get(p.id, 0),
            })
        return {"peers": entries}

    # -- conversation timeline ---------------------------------------------------

    def conversation_timeline(self, conversation_id: str, *, limit: int = 200) -> dict | None:
        """Ordered, typed timeline for one conversation with bounded content (Parts E/F)."""
        with self.runtime.session_scope() as session:
            outbox = [
                o for o in AgentOutboxRepository(session).list(limit=1000)
                if o.conversation_id == conversation_id
            ]
            inbox = [
                i for i in AgentInboxRepository(session).list(limit=1000)
                if i.conversation_id == conversation_id
            ]
            waits = MissionReplyWaitRepository(session).list(
                conversation_id=conversation_id, limit=1000
            )
        if not outbox and not inbox and not waits:
            return None

        entries: list[dict] = []
        peers: set[str] = set()
        for o in outbox:
            payload = _out_payload(o)
            peers.add(o.peer_id)
            entries.append({
                "kind": f"outbound_{o.application_type or 'message'}",
                "message_id": o.message_id, "direction": "outbound", "peer_id": o.peer_id,
                "message_type": o.application_type,
                "text_preview": _preview_text(payload.get("text")),
                "data_preview": _preview_data(payload.get("data")),
                "request_id": o.application_request_id,
                "reply_to_request_id": o.reply_to_request_id,
                "expects_reply": o.expects_reply,
                "delivery_status": o.status,
                "transport_acknowledged": o.acknowledged_at is not None,
                "semantic_reply": False,
                "attempt_count": o.attempt_count, "error_type": o.error_type,
                "mission_id": o.mission_id, "mission_run_id": o.mission_run_id,
                "mission_step_id": o.mission_step_id,
                "at": o.created_at.isoformat() if o.created_at else None,
            })
        for i in inbox:
            payload = _in_payload(i)
            if i.peer_id:
                peers.add(i.peer_id)
            entries.append({
                "kind": f"inbound_{i.application_type or 'message'}",
                "message_id": i.message_id, "direction": "inbound", "peer_id": i.peer_id,
                "message_type": i.application_type,
                "text_preview": _preview_text(payload.get("text")),
                "data_preview": _preview_data(payload.get("data")),
                "request_id": i.application_request_id,
                "reply_to_request_id": i.reply_to_request_id,
                "expects_reply": None,
                "delivery_status": "received",
                "transport_acknowledged": True,
                "semantic_reply": i.application_type == "reply",
                "application_state": i.application_state, "duplicate_count": i.duplicate_count,
                "mission_id": i.mission_id,
                "at": i.received_at.isoformat() if i.received_at else None,
            })
        for w in waits:
            entries.append({
                "kind": f"reply_wait_{w.state}",
                "wait_id": w.id, "direction": "system", "message_type": "reply_wait",
                "request_id": w.request_id, "outbound_message_id": w.outbound_message_id,
                "expected_peer_id": w.expected_peer_id, "state": w.state,
                "satisfying_inbox_message_id": w.satisfying_inbox_message_id,
                "deadline": w.deadline.isoformat() if w.deadline else None,
                "mission_id": w.mission_id,
                "at": w.created_at.isoformat() if w.created_at else None,
            })
        entries.sort(key=lambda e: e.get("at") or "")
        return {
            "conversation_id": conversation_id,
            "peers": sorted(p for p in peers if p),
            "message_count": len(outbox) + len(inbox),
            "entries": entries[:limit],
            "legend": {
                "transport_acknowledged": "peer node accepted delivery — NOT a semantic answer",
                "semantic_reply": "authenticated correlated reply that satisfied a wait",
            },
        }

    # -- distributed mission timeline --------------------------------------------

    def distributed_mission_timeline(self, mission_id: str) -> dict | None:
        """Distributed correlation timeline + topology for one mission (Part H).

        Derives only from persisted correlation records. Remote mission status is reported as
        ``unknown`` because the remote node never supplies it to us.
        """
        comms = self.runtime.comms.mission_communication_context(mission_id)
        with self.runtime.session_scope() as session:
            mission = MissionRepository(session).get(mission_id)
            if mission is None:
                return None
            run = MissionExecutionRunRepository(session).latest_for_mission(mission_id)
            steps = MissionStepRepository(session).list(mission_id=mission_id, limit=500)
            events = EventRepository(session).list(mission_id=mission_id, limit=500)
            inbound = None
            request_id = (mission.metadata_json or {}).get("request_id")
            if request_id:
                ir = InboundRequestRepository(session).get_by_request_id(request_id)
                inbound = self.runtime.comms._inbound_summary(ir) if ir is not None else None

        entries: list[dict] = []
        for s in steps:
            if s.route_target == "peer_message" or s.route_target:
                entries.append({
                    "kind": "step", "route_target": s.route_target,
                    "route_action": s.route_action, "status": s.status,
                    "step_id": s.id,
                    "at": s.created_at.isoformat() if s.created_at else None,
                })
        comm_event_prefixes = ("mission.peer_", "inbound_request.", "mission.communication.")
        for e in events:
            if e.event_type.startswith(comm_event_prefixes):
                entries.append({
                    "kind": "event", "event_type": e.event_type, "source": e.source,
                    "message": _preview_text(e.message),
                    "at": e.created_at.isoformat() if e.created_at else None,
                })
        entries.sort(key=lambda x: x.get("at") or "")

        # Stage 13D.3: authenticated remote snapshots + artifact transfers + imported artifacts
        # linked to THIS local mission (persisted facts only).
        remote_missions = self.runtime.mission_status.list_snapshots(local_mission_id=mission_id)
        with self.runtime.session_scope() as session:
            transfers = [
                self._transfer_topo(t)
                for t in ArtifactTransferRepository(session).list(limit=200)
                if t.mission_id == mission_id
            ]
            imported = [
                self._artifact_topo(a)
                for a in RFArtifactRepository(session).list(mission_id=mission_id, limit=100)
                if a.availability_state == "imported" or a.origin_node_id
            ]

        peer_ids = sorted({m["peer_id"] for m in comms["outbound_messages"] if m.get("peer_id")})
        is_inbound_mission = mission.source_type == "peer_request"
        topology = self._mission_topology(
            mission, peer_ids, comms, inbound, is_inbound_mission,
            remote_missions=remote_missions, transfers=transfers, imported=imported,
        )
        return {
            "mission_id": mission_id,
            "mission_status": mission.status,
            "mission_run_id": run.id if run is not None else None,
            "iteration_count": run.iteration_count if run is not None else None,
            "source_type": mission.source_type,
            "is_inbound_mission": is_inbound_mission,
            "outbound_messages": comms["outbound_messages"],
            "reply_waits": comms["reply_waits"],
            "response_obligation": comms["response_obligation"],
            "inbound_request": inbound,
            "remote_missions": remote_missions,
            "artifact_transfers": transfers,
            "imported_artifacts": imported,
            "entries": entries,
            "topology": topology,
            "legend": comms["legend"],
        }

    @staticmethod
    def _transfer_topo(t) -> dict:
        return {
            "transfer_id": t.transfer_id, "direction": t.direction, "peer_id": t.peer_id,
            "state": t.state, "received_bytes": t.received_bytes,
            "expected_size": t.expected_size, "local_artifact_id": t.local_artifact_id,
            "conversation_id": t.conversation_id, "digest": t.expected_digest,
        }

    @staticmethod
    def _artifact_topo(a) -> dict:
        return {
            "artifact_id": a.id, "origin_node_id": a.origin_node_id,
            "origin_artifact_id": a.origin_artifact_id, "kind": a.artifact_kind,
            "availability_state": a.availability_state, "conversation_id": a.conversation_id,
            "digest": a.digest, "size_bytes": a.size_bytes, "pinned": a.pinned,
        }

    def _mission_topology(self, mission, peer_ids, comms, inbound, is_inbound_mission,
                          *, remote_missions=None, transfers=None, imported=None) -> dict:
        """Node-and-edge topology derived ONLY from persisted correlation records."""
        nodes = [{
            "id": f"mission:{mission.id}", "kind": "local_mission",
            "label": "Local mission", "status": mission.status,
        }]
        edges: list[dict] = []
        for peer_id in peer_ids:
            nodes.append({
                "id": f"peer:{peer_id}", "kind": "peer", "label": "Peer node",
                "status": "remote_unknown",
            })
        # Outbound request → peer edges, keyed by correlation request_id.
        for m in comms["outbound_messages"]:
            if m.get("peer_id"):
                edges.append({
                    "from": f"mission:{mission.id}", "to": f"peer:{m['peer_id']}",
                    "kind": f"outbound_{m.get('message_type') or 'message'}",
                    "request_id": m.get("request_id"),
                    "acknowledged": m.get("transport_acknowledged"),
                })
        # Reply waits → satisfied edges (semantic reply distinct from ACK).
        for w in comms["reply_waits"]:
            edges.append({
                "from": f"peer:{w['expected_peer_id']}", "to": f"mission:{mission.id}",
                "kind": "reply", "state": w["state"], "request_id": w["request_id"],
                "satisfied": w["state"] == "satisfied",
            })
        # If THIS is an inbound mission, show the originating remote peer as the source.
        if is_inbound_mission and inbound is not None:
            nodes.append({
                "id": f"peer:{inbound['peer_id']}", "kind": "peer",
                "label": "Requesting peer", "status": "remote_unknown",
            })
            edges.append({
                "from": f"peer:{inbound['peer_id']}", "to": f"mission:{mission.id}",
                "kind": "inbound_request", "request_id": inbound["request_id"],
                "response_required": inbound["response_required"],
                "response_queued": inbound["response_queued"],
            })
        # Stage 13D.3: remote mission + authenticated status nodes/edges (persisted snapshots
        # only). State + freshness are shown; `unknown` only appears when no snapshot exists and
        # terminal state is NEVER inferred (it comes from an accepted terminal update).
        for snap in remote_missions or []:
            rm_id = f"remote_mission:{snap['peer_id']}:{snap['remote_mission_ref']}"
            nodes.append({
                "id": rm_id, "kind": "remote_mission", "label": "Remote mission",
                "status": snap.get("state") or "unknown", "freshness": snap.get("freshness"),
                "sequence": snap.get("latest_sequence"),
                "terminal": bool(snap.get("terminal_category")),
            })
            edges.append({
                "from": f"peer:{snap['peer_id']}", "to": rm_id,
                "kind": "request_created_remote_mission", "request_id": snap.get("request_id"),
            })
            edges.append({
                "from": rm_id, "to": f"mission:{mission.id}",
                "kind": "remote_status_synced", "sequence": snap.get("latest_sequence"),
                "state": snap.get("state"), "freshness": snap.get("freshness"),
            })
        # Stage 13D.3: artifact-transfer + imported-artifact nodes/edges (persisted only).
        for t in transfers or []:
            tid = f"transfer:{t['transfer_id']}"
            nodes.append({
                "id": tid, "kind": "artifact_transfer", "label": "Artifact transfer",
                "status": t.get("state"), "direction": t.get("direction"),
                "received_bytes": t.get("received_bytes"), "expected_size": t.get("expected_size"),
            })
            edges.append({
                "from": f"mission:{mission.id}", "to": tid, "kind": "mission_requested_artifact",
                "peer_id": t.get("peer_id"),
            })
        for a in imported or []:
            aid = f"artifact:{a['artifact_id']}"
            nodes.append({
                "id": aid, "kind": "imported_artifact", "label": "Imported artifact",
                "status": a.get("availability_state"), "origin_node_id": a.get("origin_node_id"),
            })
            edges.append({
                "from": f"mission:{mission.id}", "to": aid, "kind": "transfer_produced_artifact",
                "conversation_id": a.get("conversation_id"),
            })
        # De-duplicate nodes by id (a peer/remote mission may appear from multiple sources).
        unique: dict[str, dict] = {}
        for n in nodes:
            unique.setdefault(n["id"], n)
        return {"nodes": list(unique.values()), "edges": edges}

    # -- reply waits (global, filterable) ----------------------------------------

    def reply_waits(self, *, state: str | None = None, limit: int = 200) -> list[dict]:
        """All reply waits across missions, newest first, optionally filtered by state (Part I)."""
        with self.runtime.session_scope() as session:
            rows = MissionReplyWaitRepository(session).list(state=state, limit=limit)
            return [self.runtime.comms._wait_summary(w) for w in rows]

    # -- communication summary ---------------------------------------------------

    def communications_summary(self) -> dict:
        """Fleet-wide communication rollup: outbox/inbox/waits/inbound + failures (Part K)."""
        transport = self.runtime.transport.transport_status()
        with self.runtime.session_scope() as session:
            wait_counts = MissionReplyWaitRepository(session).counts_by_state()
            inbound_counts = InboundRequestRepository(session).counts_by_state()
            inbound_total = InboundRequestRepository(session).count()
            recent_events = EventRepository(session).list(limit=RECENT_EVENT_WINDOW)
            failures = self._recent_failures(recent_events)["communication"]
        return {
            "outbox_counts": transport.get("outbox_counts") or {},
            "inbox_count": transport.get("inbox_count", 0),
            "reply_waits": wait_counts,
            "inbound_requests": {"total": inbound_total, "by_state": inbound_counts},
            "peers": {
                "total": transport.get("peer_count", 0),
                "trusted": transport.get("trusted_peer_count", 0),
            },
            "recent_failures": failures,
        }
