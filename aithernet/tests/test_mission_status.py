"""Stage 13D.3 remote mission-status synchronization tests (temp DBs, routing fake transport).

A node reports its OWN local mission lifecycle to the requesting peer over the existing signed
durable outbox; the receiver authenticates the sender, correlates by request id, and keeps one
ordered snapshot per relationship. These tests prove conservative permission defaults, sequence
ordering (newer/duplicate/older/same-sequence-conflict), authorization isolation, freshness, the
query path, that a remote update never mutates a local mission or creates a MissionStep, and that
no prompts/keys/signatures/envelopes are exposed.
"""

from __future__ import annotations

import asyncio

from _comms_util import deliver, set_perms, two_nodes
from aithernet.comms.status_protocol import (
    MISSION_STATUS_APPLICATION,
    MissionStatusMessage,
    MissionStatusType,
)
from aithernet.state.repositories import (
    MissionRepository,
    MissionStepRepository,
)
from aithernet.transport.envelope import MessageKind, build_envelope

run = asyncio.run


# -- helpers ---------------------------------------------------------------------------------


async def _make_inbound_mission(a, b, pb, *, text="do RF work"):
    """A sends a request to B -> B creates an inbound peer mission. Returns (request_id, B mission
    id, A outbox record). A's outbound request carries a real local mission so correlation works."""
    mid_a, _ = await _active_mission(a)
    res = await a.comms.queue_peer_message(
        peer_id=pb, message_type="request", text=text, data={}, expects_reply=True,
        conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
        response_deadline=None, mission_id=mid_a, mission_run_id=None, mission_step_id=None,
        causation_id=None,
    )
    await deliver(a, res["outbox_record_id"])
    with b.session_scope() as session:
        bm = [m for m in MissionRepository(session).list(limit=20)
              if m.source_type == "peer_request"][0]
    return res["request_id"], bm.id, mid_a


async def _active_mission(node, content="local work"):
    from aithernet.schemas.missions import MissionCreate
    from aithernet.state.repositories import MissionExecutionRunRepository
    mission = await node.create_mission(MissionCreate(content=content, source_type="user"))
    run_read, _ = await node.worker_manager.enqueue_mission(mission.id)
    with node.session_scope() as session:
        MissionRepository(session).update_status(mission.id, "active")
        MissionExecutionRunRepository(session).update(run_read.id, fields={"status": "active"})
        session.commit()
    return mission.id, run_read.id


async def _flush_status(sender):
    """Deliver every queued mission-status outbox record (sender -> peer)."""
    from aithernet.state.repositories import AgentOutboxRepository
    with sender.session_scope() as session:
        ids = [r.id for r in AgentOutboxRepository(session).list(limit=100)
               if r.application_type == "mission_status" and r.status in ("pending", "failed")]
    for rid in ids:
        await deliver(sender, rid)


async def _inject(sender, target, message: MissionStatusMessage):
    """Build a SIGNED envelope from sender to target and deliver it through the full inbound
    pipeline (auth + store + dispatch). Lets a test craft arbitrary sequences/states."""
    identity = sender.transport.ensure_identity()
    envelope = build_envelope(
        identity=identity, recipient_node_id=target.config.node_id, recipient_agent_id=None,
        kind=MessageKind.AGENT_MESSAGE.value, payload=message.to_payload(),
        conversation_id=message.conversation_id, correlation_id=message.inbound_request_id,
        content_type="application/json",
    )
    return await target.transport.handle_inbound(envelope.authenticated_dict())


def _status_perms(a, b, pb, pa):
    """B may send status to A and answer A's queries (pa on B); A may accept B's status (pb on A).

    Permissions are evaluated by the RECEIVER of each message: A authorizes B to publish; B
    authorizes A to query (B receives the query). Trust is separate from all of these.
    """
    set_perms(b, pa, may_receive_mission_status=True, may_query_mission_status=True)
    set_perms(a, pb, may_publish_mission_status=True)


def _update(origin_node_id, request_id, *, state, sequence, summary=None):
    return MissionStatusMessage(
        message_type=MissionStatusType.UPDATE, origin_node_id=origin_node_id,
        remote_mission_id="remote-mission-1", inbound_request_id=request_id, state=state,
        sequence=sequence, progress_summary=summary,
    )


# == protocol + permissions ==================================================================


def test_conservative_permission_defaults(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    perms = a.mission_status.peer_permissions(pb)
    assert perms["may_publish_mission_status"] is False
    assert perms["may_query_mission_status"] is False
    assert perms["may_receive_mission_status"] is False


def test_authorized_update_accepted(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, mid_a = run(_make_inbound_mission(a, b, pb))
    run(_inject(b, a, _update(b.config.node_id, req, state="active", sequence=1)))
    snaps = a.mission_status.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0]["state"] == "active" and snaps[0]["latest_sequence"] == 1
    # Correlated to A's initiating local mission by request id.
    assert snaps[0]["local_mission_id"] == mid_a


def test_unauthorized_update_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    # B may send (pa), but A did NOT grant may_publish for B -> A rejects.
    set_perms(b, pa, may_receive_mission_status=True)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    run(_inject(b, a, _update(b.config.node_id, req, state="active", sequence=1)))
    assert a.mission_status.list_snapshots() == []


def test_wrong_origin_node_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    # B is the authenticated sender but claims a different origin node -> rejected.
    run(_inject(b, a, _update("some-other-node", req, state="active", sequence=1)))
    assert a.mission_status.list_snapshots() == []


def test_malformed_status_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    # Missing state -> malformed/rejected, no snapshot.
    run(_inject(b, a, _update(b.config.node_id, req, state=None, sequence=1)))
    assert a.mission_status.list_snapshots() == []


def test_status_payload_carries_no_prohibited_fields(tmp_path) -> None:
    msg = _update("node-b", "req-1", state="active", sequence=1, summary="bounded summary")
    payload = msg.to_payload()
    assert payload["application"] == MISSION_STATUS_APPLICATION
    for forbidden in ("prompt", "signature", "private_key", "envelope", "tool_result",
                      "arguments", "environment", "path"):
        assert forbidden not in payload


# == sequencing ==============================================================================


def test_sequence_ordering_and_dedup(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    node_b = b.config.node_id

    run(_inject(b, a, _update(node_b, req, state="active", sequence=1)))
    run(_inject(b, a, _update(node_b, req, state="waiting", sequence=2)))  # newer accepted
    snap = a.mission_status.list_snapshots()[0]
    assert snap["state"] == "waiting" and snap["latest_sequence"] == 2

    # duplicate (same seq, same state) -> idempotent, snapshot unchanged
    run(_inject(b, a, _update(node_b, req, state="waiting", sequence=2)))
    snap = a.mission_status.list_snapshots()[0]
    assert snap["latest_sequence"] == 2

    # older sequence -> ignored, newer state retained
    run(_inject(b, a, _update(node_b, req, state="active", sequence=1)))
    snap = a.mission_status.list_snapshots()[0]
    assert snap["state"] == "waiting" and snap["latest_sequence"] == 2

    events = a.mission_status.snapshot_events(snap["snapshot_id"])
    dispositions = [e["disposition"] for e in events]
    assert "accepted" in dispositions and "duplicate" in dispositions
    assert "older_sequence_ignored" in dispositions


def test_same_sequence_conflicting_payload_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    node_b = b.config.node_id
    run(_inject(b, a, _update(node_b, req, state="active", sequence=1)))
    # same sequence, DIFFERENT state -> rejected, snapshot keeps the original.
    run(_inject(b, a, _update(node_b, req, state="completed", sequence=1)))
    snap = a.mission_status.list_snapshots()[0]
    assert snap["state"] == "active"
    events = a.mission_status.snapshot_events(snap["snapshot_id"])
    assert any(e["disposition"] == "invalid_transition_rejected" for e in events)


def test_publication_sequence_monotonic_and_restart_safe(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    _, bmid, _ = run(_make_inbound_mission(a, b, pb))

    async def transition_and_publish(state):
        with b.session_scope() as session:
            MissionRepository(session).update_status(bmid, state)
            session.commit()
        return await b.mission_status.publish_for_mission(bmid)

    seqs = []
    for state in ("queued", "active", "waiting"):
        run(transition_and_publish(state))
        with b.session_scope() as session:
            from aithernet.state.repositories import LocalMissionStatusPublicationRepository
            pub = LocalMissionStatusPublicationRepository(session).list_for_mission(bmid)[0]
            seqs.append(pub.last_sequence)
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # strictly increasing

    # Re-publishing the same (current) state allocates NO new sequence (idempotent + retry-safe).
    assert run(b.mission_status.publish_for_mission(bmid)) is None


# == mission integration =====================================================================


def test_inbound_mission_creation_publishes_initial_status(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, bmid, _ = run(_make_inbound_mission(a, b, pb))
    # The mission-creation transition already queued an initial status update on B.
    pubs = b.mission_status.list_publications(bmid)
    assert pubs and pubs[0]["last_sequence"] >= 1


def test_remote_update_creates_no_mission_step_and_never_mutates_local_mission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, mid_a = run(_make_inbound_mission(a, b, pb))
    with a.session_scope() as session:
        before_status = MissionRepository(session).get(mid_a).status
        before_steps = len(MissionStepRepository(session).list(mission_id=mid_a, limit=100))
    run(_inject(b, a, _update(b.config.node_id, req, state="completed", sequence=1)))
    with a.session_scope() as session:
        after_status = MissionRepository(session).get(mid_a).status
        after_steps = len(MissionStepRepository(session).list(mission_id=mid_a, limit=100))
    # A remote "completed" never completes A's local mission and never adds a step.
    assert after_status == before_status
    assert after_steps == before_steps


def test_publish_skips_non_reportable_and_unauthorized(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    # No status perms granted -> B does not publish even though the inbound mission exists.
    _, bmid, _ = run(_make_inbound_mission(a, b, pb))
    assert run(b.mission_status.publish_for_mission(bmid)) is None
    # A local (non peer_request) mission is never reportable.
    mid_local, _ = run(_active_mission(b))
    assert run(b.mission_status.publish_for_mission(mid_local)) is None


# == snapshot + freshness ====================================================================


def test_wrong_peer_cannot_update_another_snapshot(tmp_path) -> None:
    # Three nodes: A (receiver), B (real owner), C (impostor). C is trusted+authorized on A but
    # reports a remote mission with origin B -> rejected (origin mismatch); B's snapshot is safe.
    a, b, pb, pa = two_nodes(tmp_path)
    c = __import__("_transport_util", fromlist=["make_runtime"]).make_runtime(
        tmp_path, "node-C", "C"
    )
    from _transport_util import init_and_peer
    pc_on_a, pa_on_c = run(init_and_peer(a, c))
    _status_perms(a, b, pb, pa)
    set_perms(a, pc_on_a, may_publish_mission_status=True)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    run(_inject(b, a, _update(b.config.node_id, req, state="active", sequence=1)))
    # C tries to claim B's mission update (origin = B's node) -> rejected.
    run(_inject(c, a, _update(b.config.node_id, req, state="completed", sequence=9)))
    snap = a.mission_status.list_snapshots(peer_id=pb)[0]
    assert snap["state"] == "active" and snap["latest_sequence"] == 1


def test_freshness_unknown_fresh_stale_terminal(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    a.config.communication.status_freshness_seconds = 0.001  # immediate staleness for non-terminal
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    # unknown: no snapshot
    assert a.mission_status.list_snapshots(peer_id="ghost") == []
    # non-terminal update -> becomes stale after the (tiny) freshness window
    run(_inject(b, a, _update(b.config.node_id, req, state="active", sequence=1)))
    import time
    time.sleep(0.02)
    snap = a.mission_status.list_snapshots()[0]
    assert snap["freshness"] == "stale"  # never inferred as failed/completed
    # terminal update -> classified terminal, never stale
    run(_inject(b, a, _update(b.config.node_id, req, state="completed", sequence=2)))
    snap = a.mission_status.list_snapshots()[0]
    assert snap["freshness"] == "terminal" and snap["terminal_category"] == "completed"


def test_snapshot_and_events_persist_and_have_no_secrets(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    run(_inject(b, a, _update(b.config.node_id, req, state="active", sequence=1)))
    snap = a.mission_status.list_snapshots()[0]
    blob = str(snap) + str(a.mission_status.snapshot_events(snap["snapshot_id"]))
    low = blob.lower()
    for secret in ("signature", "-----begin", "private", "envelope", "prompt"):
        assert secret not in low


# == query ===================================================================================


def test_query_known_mission_gets_authenticated_response(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, bmid, _ = run(_make_inbound_mission(a, b, pb))
    # B advances and publishes; A receives an initial snapshot.
    with b.session_scope() as session:
        MissionRepository(session).update_status(bmid, "active")
        session.commit()
    run(b.mission_status.publish_for_mission(bmid))
    run(_flush_status(b))
    snap = a.mission_status.list_snapshots()[0]

    # A queries B for the latest status; B responds from persisted fact; A's snapshot refreshes.
    assert run(a.mission_status.query_snapshot(snap["snapshot_id"])) is True
    run(_flush_status(a))   # deliver A's query to B
    run(_flush_status(b))   # deliver B's response to A
    refreshed = a.mission_status.get_snapshot(snap["snapshot_id"])
    assert refreshed["state"] == "active"
    assert refreshed["query_pending"] is False  # response cleared the pending flag


def test_coordinator_context_is_bounded_and_factual(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _status_perms(a, b, pb, pa)
    req, _, _ = run(_make_inbound_mission(a, b, pb))
    run(_inject(b, a, _update(b.config.node_id, req, state="waiting", sequence=3)))
    ctx = a.mission_status.coordinator_context()
    assert ctx["remote_missions"]
    rm = ctx["remote_missions"][0]
    # Only factual fields — no envelope/signature/prompt keys.
    assert set(rm) >= {"peer_id", "state", "freshness", "latest_sequence", "response_obligation"}
    assert rm["state"] == "waiting" and rm["freshness"] in ("fresh", "stale", "terminal")
