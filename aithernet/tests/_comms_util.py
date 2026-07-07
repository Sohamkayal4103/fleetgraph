"""Shared helpers for Stage 13B coordinator-communication tests (fake peers, temp DBs).

Builds two comms-enabled nodes wired with the Stage 13A routing fake transport (no real
network/Gemini/Codex/RF). Provides authorization + delivery helpers and a peer_message
decision builder.
"""

from __future__ import annotations

import asyncio

from _transport_util import RoutingTransport, init_and_peer, make_runtime
from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.schemas.missions import MissionCreate
from aithernet.state.repositories import (
    InboundRequestRepository,
    MissionExecutionRunRepository,
    MissionReplyWaitRepository,
    MissionRepository,
    PeerPermissionRepository,
)

run = asyncio.run


def two_nodes(tmp_path, *, authorize: bool = True):
    """Return (A, B, peer_b_on_a, peer_a_on_b) with mutual trust + routing transports.

    When ``authorize`` is True, B authorizes A to send coordinator requests AND to request a
    response (so a response obligation applies). Reply authorization (A may accept B's replies)
    is on by default.
    """
    a = make_runtime(tmp_path, "node-A", "A")
    b = make_runtime(tmp_path, "node-B", "B")
    pb, pa = run(init_and_peer(a, b))
    if authorize:
        with b.session_scope() as session:
            PeerPermissionRepository(session).update(
                pa, fields={"may_send_requests": True, "may_request_response": True}
            )
            session.commit()
    a.transport._transport = RoutingTransport(b)
    b.transport._transport = RoutingTransport(a)
    return a, b, pb, pa


def set_perms(node, peer_id, **fields):
    with node.session_scope() as session:
        PeerPermissionRepository(session).update(peer_id, fields=fields)
        session.commit()


async def deliver(node, record_id):
    """Deterministically deliver one outbox record (no background worker needed)."""
    return await node.transport.send_now(record_id)


def peer_message_decision(mission_id, peer_id, **payload):
    """Build a coordinator decision selecting the peer_message route."""
    return CoordinatorDecision.from_model_json(
        {
            "summary": "talk to peer", "next_target": "peer_message", "action": "msg",
            "message": "m", "structured_payload": {"peer_id": peer_id, **payload},
            "expected_result": "a reply",
        },
        mission_id=mission_id,
    )


async def active_mission(node, content="do work"):
    """Create a mission + an active execution run, return (mission_id, run_id)."""
    mission = await node.create_mission(MissionCreate(content=content, source_type="user"))
    run_read, _ = await node.worker_manager.enqueue_mission(mission.id)
    with node.session_scope() as session:
        MissionRepository(session).update_status(mission.id, "active")
        MissionExecutionRunRepository(session).update(run_read.id, fields={"status": "active"})
        session.commit()
    return mission.id, run_read.id


def set_mission_waiting(node, mission_id, run_id):
    with node.session_scope() as session:
        MissionRepository(session).update_status(mission_id, "waiting")
        MissionExecutionRunRepository(session).update(run_id, fields={"status": "waiting"})
        session.commit()


def reply_waits(node, mission_id):
    with node.session_scope() as session:
        return MissionReplyWaitRepository(session).list_for_mission(mission_id)


def inbound_requests(node):
    with node.session_scope() as session:
        return InboundRequestRepository(session).list()


async def queue_reply(node, peer_id, *, reply_to_request_id, conversation_id, mission_id=None,
                      text="here is the answer", data=None):
    """B-side helper: queue a correlated reply to an inbound request and deliver it."""
    res = await node.comms.queue_peer_message(
        peer_id=peer_id, message_type="reply", text=text, data=data or {}, expects_reply=False,
        conversation_id=conversation_id, reply_to_message_id=None,
        reply_to_request_id=reply_to_request_id, response_deadline=None, mission_id=mission_id,
        mission_run_id=None, mission_step_id=None, causation_id=None,
    )
    await deliver(node, res["outbox_record_id"])
    return res
