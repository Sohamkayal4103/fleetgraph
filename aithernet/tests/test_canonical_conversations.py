"""Stage 13D canonical-conversation resolution tests (Part Q — canonical conversations).

Two in-process comms nodes over the routing fake transport. Verifies that a reply which omits
``conversation_id`` inherits the original request/reply chain by PROVABLE correlation only
(never similarity), restricted to the expected peer; that duplicates/wrong-peer/collisions never
merge conversations; and that the historical repair is bounded, idempotent, and leaves ambiguous
rows unchanged.
"""

from __future__ import annotations

import asyncio

from _comms_util import active_mission, deliver, set_perms, two_nodes
from aithernet.comms.conversation import resolve_canonical_conversation

run = asyncio.run


async def _request(a, pb, *, conversation_id, mid, rid, text="ping"):
    res = await a.comms.queue_peer_message(
        peer_id=pb, message_type="request", text=text, data={}, expects_reply=True,
        conversation_id=conversation_id, reply_to_message_id=None, reply_to_request_id=None,
        response_deadline=None, mission_id=mid, mission_run_id=rid, mission_step_id=None,
        causation_id=None,
    )
    await deliver(a, res["outbox_record_id"])
    return res


async def _reply(b, pa, *, request_id, conversation_id=None, text="pong"):
    res = await b.comms.queue_peer_message(
        peer_id=pa, message_type="reply", text=text, data={}, expects_reply=False,
        conversation_id=conversation_id, reply_to_message_id=None,
        reply_to_request_id=request_id, response_deadline=None, mission_id=None,
        mission_run_id=None, mission_step_id=None, causation_id=None,
    )
    await deliver(b, res["outbox_record_id"])
    return res


def _convs(node):
    return node.comms.list_conversations()


# == explicit id retained ====================================================================


def test_explicit_conversation_id_retained(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True, may_request_response=True)

    async def main():
        mid, rid = await active_mission(a)
        res = await _request(a, pb, conversation_id="EXPLICIT", mid=mid, rid=rid)
        return res["conversation_id"]
    assert run(main()) == "EXPLICIT"


# == omitted reply conversation resolved by correlation ======================================


def test_reply_without_conversation_inherits_via_inbound_request(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True, may_request_response=True)

    async def main():
        mid, rid = await active_mission(a)
        req = await _request(a, pb, conversation_id="CONV-A", mid=mid, rid=rid)
        # B replies with NO conversation_id; it must inherit CONV-A (the inbound request's conv).
        rep = await _reply(b, pa, request_id=req["request_id"], conversation_id=None)
        return rep["conversation_id"]
    assert run(main()) == "CONV-A"


def test_one_chain_stays_one_conversation_on_both_sides(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True, may_request_response=True)

    async def main():
        mid, rid = await active_mission(a)
        req = await _request(a, pb, conversation_id="CHAIN", mid=mid, rid=rid)
        await _reply(b, pa, request_id=req["request_id"], conversation_id=None)
        return _convs(a), _convs(b)
    a_convs, b_convs = run(main())
    # A sees exactly one conversation CHAIN with one outbound + one inbound.
    assert len(a_convs) == 1 and a_convs[0]["conversation_id"] == "CHAIN"
    assert a_convs[0]["outbound"] == 1 and a_convs[0]["inbound"] == 1


def test_reply_before_wait_resolves(tmp_path):
    # A reply arriving before any wait still inherits via the inbound-request message on B.
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True)

    async def main():
        mid, rid = await active_mission(a)
        req = await _request(a, pb, conversation_id="C1", mid=mid, rid=rid)
        rep = await _reply(b, pa, request_id=req["request_id"], conversation_id=None)
        return rep["conversation_id"]
    assert run(main()) == "C1"


def test_duplicate_reply_creates_no_extra_conversation(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True)

    async def main():
        mid, rid = await active_mission(a)
        req = await _request(a, pb, conversation_id="DUP", mid=mid, rid=rid)
        r1 = await _reply(b, pa, request_id=req["request_id"], conversation_id=None)
        # Re-deliver the SAME reply (transport duplicate) — idempotent, no new conversation.
        await deliver(b, r1["outbox_record_id"])
        return _convs(a)
    convs = run(main())
    assert len(convs) == 1 and convs[0]["conversation_id"] == "DUP"


# == isolation: wrong peer / collision never merges ==========================================


def test_wrong_peer_request_id_does_not_merge(tmp_path):
    # A third peer reusing the same request id must NOT inherit the other peer's conversation.
    a, b, pb, pa = two_nodes(tmp_path)
    with a.session_scope() as session:
        from aithernet.state.repositories import AgentOutboxRepository
        out = AgentOutboxRepository(session).create(
            message_id="m-x", peer_id=pb, kind="agent_message", envelope={}, envelope_hash="h",
            conversation_id="OWN", application_type="request", application_request_id="shared-req",
        )
        session.commit()
        del out
    with a.session_scope() as session:
        # Resolve as if a DIFFERENT peer replied to 'shared-req'.
        conv, source = resolve_canonical_conversation(
            session, explicit_conversation_id=None, reply_to_request_id="shared-req",
            expected_peer_id="some-other-peer",
        )
    assert source == "generated" and conv != "OWN"


def test_resolver_precedence_outbound_then_generated(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    with a.session_scope() as session:
        from aithernet.state.repositories import AgentOutboxRepository
        AgentOutboxRepository(session).create(
            message_id="m-1", peer_id=pb, kind="agent_message", envelope={}, envelope_hash="h",
            conversation_id="OUTCONV", application_type="request", application_request_id="r-1",
        )
        session.commit()
    with a.session_scope() as session:
        conv, source = resolve_canonical_conversation(
            session, explicit_conversation_id=None, reply_to_request_id="r-1", expected_peer_id=pb,
        )
        assert conv == "OUTCONV" and source == "outbound_request"
        # An unknown request id falls back to a fresh, generated conversation.
        conv2, source2 = resolve_canonical_conversation(
            session, explicit_conversation_id=None, reply_to_request_id="unknown",
            expected_peer_id=pb,
        )
        assert source2 == "generated" and conv2 not in ("OUTCONV",)


# == repair: dry-run / apply / idempotent / ambiguous unchanged ==============================


def _force_split_reply(node, *, request_id, conv_split, peer_id):
    """Insert an inbound reply row whose conversation_id is a wrong split (simulating legacy)."""
    from aithernet.state.repositories import AgentInboxRepository
    with node.session_scope() as session:
        rec = AgentInboxRepository(session).create(
            message_id=f"in-{request_id}", peer_id=peer_id, sender_node_id="node-B",
            sender_fingerprint="fp", kind="agent_message", envelope_hash=f"h-{request_id}",
            envelope={}, payload={}, conversation_id=conv_split,
        )
        rec.application_type = "reply"
        rec.reply_to_request_id = request_id
        session.flush()
        session.commit()


def test_repair_dry_run_then_apply_then_idempotent(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)

    async def main():
        # A has an outbound request in conversation CANON with request id R.
        with a.session_scope() as session:
            from aithernet.state.repositories import AgentOutboxRepository
            AgentOutboxRepository(session).create(
                message_id="out-R", peer_id=pb, kind="agent_message", envelope={},
                envelope_hash="h", conversation_id="CANON", application_type="request",
                application_request_id="R",
            )
            session.commit()
        # A legacy reply row landed in a WRONG split conversation.
        _force_split_reply(a, request_id="R", conv_split="WRONG-SPLIT", peer_id=pb)

        dry = await a.comms.repair_conversations(dry_run=True)
        applied = await a.comms.repair_conversations(dry_run=False)
        again = await a.comms.repair_conversations(dry_run=False)
        return dry, applied, again
    dry, applied, again = run(main())
    assert dry["repairable"] == 1 and dry["repaired"] == 0  # dry-run changes nothing
    assert applied["repaired"] == 1
    assert again["repairable"] == 0 and again["repaired"] == 0  # idempotent


def test_repair_leaves_ambiguous_rows_unchanged(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)

    async def main():
        # A reply correlated to an UNKNOWN request id (no outbound/inbound/wait/IR) is ambiguous.
        _force_split_reply(a, request_id="UNRESOLVABLE", conv_split="SOLO", peer_id=pb)
        return await a.comms.repair_conversations(dry_run=False)
    res = run(main())
    assert res["repairable"] == 0 and res["repaired"] == 0  # never merged
