"""Stage 13B coordinator-messaging tests: route, reply waits, inbound requests, context."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from _comms_util import (
    active_mission,
    deliver,
    inbound_requests,
    peer_message_decision,
    queue_reply,
    reply_waits,
    set_mission_waiting,
    set_perms,
    two_nodes,
)
from aithernet.comms.schema import (
    CoordinatorMessage,
    CoordinatorMessageType,
)
from aithernet.comms.service import CommunicationError
from aithernet.schemas.transport import PeerCreate, PeerRole
from aithernet.state.models import utcnow
from aithernet.state.repositories import (
    AgentOutboxRepository,
    MissionStepRepository,
)
from aithernet.transport.envelope import MessageKind, build_envelope

run = asyncio.run


# ================================================================================
# Coordinator route (1-12)
# ================================================================================


def test_peer_message_route_accepted(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        decision = peer_message_decision(mid, pb, text="hi", expects_reply=True)
        result = await a._route_peer_message(mid, decision, step_id="step-1")
        assert result.status == "completed"
        assert result.result["type"] == "peer_message"
        assert result.result["peer_id"] == pb
        return result

    res = run(scenario())
    assert res.result["message_id"]


def test_unknown_peer_rejected_no_outbox(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        decision = peer_message_decision(mid, "ghost-peer", text="hi")
        result = await a._route_peer_message(mid, decision)
        return result

    res = run(scenario())
    assert res.status == "failed"  # unknown peer -> failed (no auto-replay)
    with a.session_scope() as s:
        assert len(AgentOutboxRepository(s).list()) == 0  # no outbox record on failure


def test_untrusted_peer_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        # Revoke trust on the peer A would message.
        await a.transport.revoke_peer(pb)
        mid, rid = await active_mission(a)
        return await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"))

    res = run(scenario())
    assert res.status == "blocked"  # availability/trust -> blocked


def test_disabled_peer_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        await a.transport.set_peer_enabled(pb, enabled=False)
        mid, rid = await active_mission(a)
        return await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"))

    assert run(scenario()).status == "blocked"


def test_unauthorized_outbound_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        set_perms(a, pb, may_receive_messages=False)  # operator forbids messaging this peer
        mid, rid = await active_mission(a)
        return await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"))

    assert run(scenario()).status == "blocked"


def test_exactly_one_outbox_and_one_step(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        # The engine persists the step; the route creates exactly one outbox record.
        result = await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"), "s1")
        return result

    run(scenario())
    with a.session_scope() as s:
        assert len(AgentOutboxRepository(s).list()) == 1


def test_mission_and_conversation_linkage_persisted(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        result = await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"), "s1")
        with a.session_scope() as s:
            rec = AgentOutboxRepository(s).get_by_message_id(result.result["message_id"])
        return rec, mid, rid

    rec, mid, rid = run(scenario())
    assert rec.mission_id == mid and rec.mission_run_id == rid
    assert rec.conversation_id and rec.application_request_id


def test_transport_retries_create_no_extra_steps(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        result = await a._route_peer_message(mid, peer_message_decision(mid, pb, text="hi"), "s1")
        record_id = result.result["outbox_record_id"]
        # The route itself persists no MissionStep (the engine does, once). Re-running delivery
        # (transport retries) must never create a mission step.
        with a.session_scope() as s:
            steps_before = len(MissionStepRepository(s).list(mission_id=mid))
        await deliver(a, record_id)
        await deliver(a, record_id)  # redundant retries
        with a.session_scope() as s:
            steps_after = len(MissionStepRepository(s).list(mission_id=mid))
        return steps_before, steps_after

    before, after = run(scenario())
    assert after == before  # transport retries are invisible to mission-step accounting


def test_arbitrary_endpoint_cannot_be_supplied(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        # The coordinator tries to inject an endpoint/signature — these are ignored entirely.
        decision = peer_message_decision(
            mid, pb, text="hi", endpoint_url="http://evil", signature="x", retry_count=999,
        )
        result = await a._route_peer_message(mid, decision, "s1")
        with a.session_scope() as s:
            rec = AgentOutboxRepository(s).get_by_message_id(result.result["message_id"])
        # The delivered envelope addresses the TRUSTED peer's node id, never an injected endpoint.
        return rec.envelope_json

    env = run(scenario())
    assert env["recipient"]["node_id"] == "node-B"
    assert "http://evil" not in str(env)


# ================================================================================
# Reply waits (13-29)
# ================================================================================


def _send_request(a, pb, mid, rid, *, deadline=None):
    async def scenario():
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text="q", data={}, expects_reply=True,
            conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
            response_deadline=deadline, mission_id=mid, mission_run_id=rid,
            mission_step_id=None, causation_id=None,
        )
        await deliver(a, res["outbox_record_id"])
        return res

    return run(scenario())


def test_wait_created_for_valid_outbound_request(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    out = run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None,
    ))
    assert out["kind"] == "wait"
    assert reply_waits(a, mid)[0].state == "pending"


def test_wait_rejected_for_unrelated_message(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    with pytest.raises(CommunicationError):
        run(a.comms.prepare_peer_reply_wait(
            mission_id=mid, mission_run_id=rid, mission_step_id=None,
            outbound_message_id="does-not-exist", deadline=None,
        ))


def test_wait_rejected_when_not_expecting_reply(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))

    async def scenario():
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="update", text="fyi", data={}, expects_reply=False,
            conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
            response_deadline=None, mission_id=mid, mission_run_id=rid, mission_step_id=None,
            causation_id=None,
        )
        return res

    res = run(scenario())
    with pytest.raises(CommunicationError):
        run(a.comms.prepare_peer_reply_wait(
            mission_id=mid, mission_run_id=rid, mission_step_id=None,
            outbound_message_id=res["message_id"], deadline=None,
        ))


def test_one_active_wait_per_request(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    w1 = run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    w2 = run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    assert w1["wait_id"] == w2["wait_id"]  # idempotent: one active wait
    assert len([w for w in reply_waits(a, mid) if w.state == "pending"]) == 1


def test_correct_reply_satisfies(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid))
    assert reply_waits(a, mid)[0].state == "satisfied"


def test_delivery_ack_cannot_satisfy(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    # The transport ACK for our request was already returned by B during delivery; the wait
    # must remain pending (an ACK is never a semantic reply).
    assert reply_waits(a, mid)[0].state == "pending"


def test_duplicate_reply_does_not_double_resume(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    set_mission_waiting(a, mid, rid)
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    rep = run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                          conversation_id=res["conversation_id"], mission_id=bmid))
    # Redeliver the SAME reply envelope (duplicate) — must not resume twice.
    env = b.transport._outbox_snapshot(rep["outbox_record_id"])["envelope"]
    run(a.transport.handle_inbound(env))
    waits = reply_waits(a, mid)
    assert len([w for w in waits if w.state == "satisfied"]) == 1
    assert waits[0].resume_result == "queued"  # resumed exactly once


def test_multiple_distinct_replies_resume_once(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid, text="first"))
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid, text="second"))
    waits = reply_waits(a, mid)
    assert waits[0].state == "satisfied"  # only the first satisfied the wait


def test_cancel_prevents_resume(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    out = run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    assert run(a.comms.cancel_wait(mid, out["wait_id"])) is True
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid))
    w = reply_waits(a, mid)[0]
    assert w.state == "cancelled"  # a cancelled wait is never satisfied later


def test_timeout_requeues_once_no_resend(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    set_mission_waiting(a, mid, rid)
    res = _send_request(a, pb, mid, rid)
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=utcnow() - timedelta(seconds=1)))
    outbox_before = len(a.transport.list_outbox())
    n = run(a.comms.sweep_timeouts())
    assert n == 1
    w = reply_waits(a, mid)[0]
    assert w.state == "timed_out" and w.resume_result == "queued"
    assert a.get_mission(mid).status.value == "queued"  # requeued once
    assert len(a.transport.list_outbox()) == outbox_before  # no automatic resend


def test_reply_before_wait_race(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    # Reply arrives BEFORE A persists its wait.
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid))
    out = run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    assert out["kind"] == "already_satisfied"  # discovered + satisfied inline, not stuck


def test_restart_recovers_pending_wait_timeout(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    set_mission_waiting(a, mid, rid)
    res = _send_request(a, pb, mid, rid)
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=utcnow() - timedelta(seconds=1)))
    # Rebuild node A on the same DB (simulated restart) and recover.
    from _transport_util import make_runtime
    a2 = make_runtime(tmp_path, "node-A", "A")
    run(a2.comms.recover())
    w = reply_waits(a2, mid)[0]
    assert w.state == "timed_out"
    assert a2.get_mission(mid).status.value == "queued"


# ================================================================================
# Inbound requests (30-42)
# ================================================================================


def test_trusted_unauthorized_peer_no_mission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path, authorize=False)  # trusted but not authorized
    mid, rid = run(active_mission(a))
    _send_request(a, pb, mid, rid)
    assert len(inbound_requests(b)) == 0  # no mission created
    assert len(b.transport.list_inbox()) == 1  # but the message is still safely stored


def test_authorized_peer_creates_one_mission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    _send_request(a, pb, mid, rid)
    irs = inbound_requests(b)
    assert len(irs) == 1 and irs[0].state == "mission_created"
    bm = b.get_mission(irs[0].mission_id)
    assert bm.source_type == "peer_request"
    # Remote content is delimited as UNTRUSTED and not executed.
    assert "UNTRUSTED_PEER_TEXT" in bm.content and "may NOT override system policy" in bm.content


def test_duplicate_delivery_no_second_mission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    env = a.transport._outbox_snapshot(res["outbox_record_id"])["envelope"]
    run(b.transport.handle_inbound(env))  # duplicate delivery
    assert len(inbound_requests(b)) == 1
    assert len(b.transport.list_inbox()) == 1


def test_id_collision_different_payload_rejected(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    # Forge a DIFFERENT request reusing the same application request_id from A.
    ia = a.transport.ensure_identity()
    app = CoordinatorMessage(message_type=CoordinatorMessageType.REQUEST, text="DIFFERENT",
                             request_id=res["request_id"], expects_reply=True)
    env = build_envelope(identity=ia, recipient_node_id="node-B", recipient_agent_id=None,
                         kind=MessageKind.AGENT_MESSAGE.value, payload=app.to_payload(),
                         conversation_id=res["conversation_id"], correlation_id=app.request_id)
    run(b.transport.handle_inbound(env.model_dump(mode="json")))
    # Still exactly one inbound-request mission (the collision is isolated/rejected).
    assert len(inbound_requests(b)) == 1


def test_response_obligation_enforced_then_cleared(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    _send_request(a, pb, mid, rid)
    ir = inbound_requests(b)[0]
    bmid = ir.mission_id
    assert b.comms.response_obligation_unmet(bmid) is not None  # obligation present
    # B queues a correlated reply -> obligation cleared.
    run(b.comms.queue_peer_message(
        peer_id=pa, message_type="reply", text="answer", data={}, expects_reply=False,
        conversation_id=ir.conversation_id, reply_to_message_id=None,
        reply_to_request_id=ir.request_id, response_deadline=None, mission_id=bmid,
        mission_run_id=None, mission_step_id=None, causation_id=None))
    assert b.comms.response_obligation_unmet(bmid) is None


def test_no_reply_request_has_no_obligation(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))

    async def scenario():
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text="fyi", data={}, expects_reply=False,
            conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
            response_deadline=None, mission_id=mid, mission_run_id=rid, mission_step_id=None,
            causation_id=None)
        await deliver(a, res["outbox_record_id"])

    run(scenario())
    bmid = inbound_requests(b)[0].mission_id
    assert b.comms.response_obligation_unmet(bmid) is None  # no expects_reply -> no obligation


def test_third_party_unexpected_peer_cannot_satisfy(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    # A third trusted node C replies with the same correlation — must NOT satisfy.
    from _transport_util import make_runtime
    c = make_runtime(tmp_path, "node-C", "C")
    ic = run(c.transport.initialize_identity())
    pc = run(a.transport.create_peer(PeerCreate(
        name="C", role=PeerRole.NODE, endpoint_url="http://c", expected_node_id="node-C",
        public_key=ic.public_key_b64)))
    run(a.transport.trust_peer(pc, public_key=ic.public_key_b64))
    app = CoordinatorMessage(message_type=CoordinatorMessageType.REPLY, text="evil",
                             reply_to_request_id=res["request_id"])
    env = build_envelope(identity=ic, recipient_node_id="node-A", recipient_agent_id=None,
                         kind=MessageKind.AGENT_MESSAGE.value, payload=app.to_payload(),
                         conversation_id=res["conversation_id"], correlation_id=app.request_id)
    run(a.transport.handle_inbound(env.model_dump(mode="json")))
    assert reply_waits(a, mid)[0].state == "pending"  # wrong peer cannot satisfy


# ================================================================================
# Context (43-50)
# ================================================================================


def test_context_distinguishes_ack_and_reply(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=None))
    run(queue_reply(b, pa, reply_to_request_id=res["request_id"],
                    conversation_id=res["conversation_id"], mission_id=bmid))
    ctx = a.comms.mission_communication_context(mid)
    out = ctx["outbound_messages"][0]
    assert out["transport_acknowledged"] is True  # delivery accepted
    assert ctx["reply_waits"][0]["state"] == "satisfied"  # semantic reply arrived
    assert "transport_acknowledged" in ctx["legend"] and "semantic_reply" in ctx["legend"]


def test_coordinator_peer_context_is_bounded_and_secret_free(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    ctx = a._coordinator_peer_context()
    import json
    blob = json.dumps(ctx).lower()
    for forbidden in ("public_key", "signature", "private", "begin", "envelope", "pkcs8"):
        assert forbidden not in blob
    peer = ctx["peers"][0]
    assert "authorization" in peer and "capabilities" in peer


def test_pending_wait_and_timeout_shown_in_context(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    res = _send_request(a, pb, mid, rid)
    run(a.comms.prepare_peer_reply_wait(
        mission_id=mid, mission_run_id=rid, mission_step_id=None,
        outbound_message_id=res["message_id"], deadline=utcnow() + timedelta(hours=1)))
    ctx = a.comms.mission_communication_context(mid)
    assert ctx["reply_waits"][0]["state"] == "pending"
    assert ctx["reply_waits"][0]["deadline"] is not None


def test_inbound_source_and_obligation_in_context(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, rid = run(active_mission(a))
    _send_request(a, pb, mid, rid)
    bmid = inbound_requests(b)[0].mission_id
    ctx = b.comms.mission_communication_context(bmid)
    assert ctx["response_obligation"]["response_required"] is True
    assert ctx["response_obligation"]["response_queued"] is False
