"""Stage 13A peer-trust, inbox dedup, ACK, and delivery-worker tests (Part T 17-51, 57)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from _transport_util import (
    FailingTransport,
    RoutingTransport,
    conn_error,
    http_4xx,
    http_5xx,
    init_and_peer,
    make_runtime,
    send,
    timeout_error,
)
from aithernet.state.models import utcnow
from aithernet.state.repositories import AgentOutboxRepository, PeerRepository
from aithernet.transport.ack import AckStatus, build_ack, verify_ack
from aithernet.transport.errors import (
    KeyMismatchError,
    MessageIdCollisionError,
    PeerTrustError,
)
from aithernet.transport.identity import IdentityManager

run = asyncio.run


def _two(tmp_path, **outbound):
    a = make_runtime(tmp_path, "node-A", "A", **outbound)
    b = make_runtime(tmp_path, "node-B", "B")
    return a, b


def _claim_deliver(a, record_id, transport):
    """Deterministically claim + deliver one record once; return the resulting status."""
    a.transport._transport = transport
    owner = "test-owner"
    now = utcnow()
    with a.session_scope() as s:
        claimed = AgentOutboxRepository(s).claim(
            record_id, owner=owner, now=now, claim_expiry=now + timedelta(seconds=30)
        )
        s.commit()
    assert claimed == 1
    return run(a.transport.deliver_record(record_id, owner=owner))


def _force_due(a, record_id):
    with a.session_scope() as s:
        AgentOutboxRepository(s).update(
            record_id, fields={"next_attempt_at": utcnow() - timedelta(seconds=1)}
        )
        s.commit()


def _envelope_of(a, record_id) -> dict:
    return a.transport._outbox_snapshot(record_id)["envelope"]


def _raw_outbox(a, record_id):
    """Read the raw outbox ORM row (for internal fields not on the public read model)."""
    with a.session_scope() as s:
        rec = AgentOutboxRepository(s).get(record_id)
        return {
            "status": rec.status,
            "claim_owner": rec.claim_owner,
            "message_id": rec.message_id,
            "next_attempt_at": rec.next_attempt_at,
        }


def _aware(dt):
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# -- peer trust (17-22) ----------------------------------------------------------


def test_untrusted_peer_rejected(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b, trust=False))
    rid = run(send(a, pb))
    env = _envelope_of(a, rid)
    with pytest.raises(PeerTrustError):
        run(b.transport.handle_inbound(env))


def test_trusted_peer_accepted(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    ack = run(b.transport.handle_inbound(_envelope_of(a, rid)))
    assert ack["status"] == AckStatus.ACCEPTED.value


def test_revoked_peer_rejected(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, pa = run(init_and_peer(a, b))
    run(b.transport.revoke_peer(pa))
    rid = run(send(a, pb))
    with pytest.raises(PeerTrustError):
        run(b.transport.handle_inbound(_envelope_of(a, rid)))


def test_disabled_peer_rejected(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, pa = run(init_and_peer(a, b))
    run(b.transport.set_peer_enabled(pa, enabled=False))
    rid = run(send(a, pb))
    with pytest.raises(PeerTrustError):
        run(b.transport.handle_inbound(_envelope_of(a, rid)))


def test_key_mismatch_detected(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, pa = run(init_and_peer(a, b))
    # B's stored key for A is changed to a different key -> inbound fingerprint mismatch.
    other = IdentityManager(tmp_path / "other", node_id="node-A", node_name="A").initialize()
    with b.session_scope() as s:
        from aithernet.transport.identity import fingerprint_for_public_key

        PeerRepository(s).update(pa, fields={
            "public_key": other.public_key_b64,
            "fingerprint": fingerprint_for_public_key(other.public_key_b64),
        })
        s.commit()
    rid = run(send(a, pb))
    with pytest.raises(KeyMismatchError):
        run(b.transport.handle_inbound(_envelope_of(a, rid)))


def test_manifest_key_cannot_replace_trusted_key(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    pinned = a.transport.get_peer_read(pb).public_key
    # A different identity signs the manifest A fetches for peer B.
    rogue = IdentityManager(tmp_path / "rogue", node_id="node-B", node_name="B").initialize()

    class RogueManifest:
        async def fetch_manifest(self, **_):
            from aithernet.transport.manifest import build_manifest
            return build_manifest(rogue, node_name="B", endpoint="http://b", capabilities=[])
        async def deliver(self, **_):  # pragma: no cover
            raise AssertionError
        async def healthcheck(self, **_):  # pragma: no cover
            return {}

    a.transport._transport = RogueManifest()
    with pytest.raises(KeyMismatchError):
        run(a.transport.refresh_manifest(pb))
    # The pinned key is untouched.
    assert a.transport.get_peer_read(pb).public_key == pinned


# -- inbox dedup / idempotency (23-28) ------------------------------------------


def test_first_delivery_creates_one_record(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    run(b.transport.handle_inbound(_envelope_of(a, run(send(a, pb)))))
    assert len(b.transport.list_inbox()) == 1


def test_duplicate_identical_creates_no_second_record(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    env = _envelope_of(a, run(send(a, pb)))
    run(b.transport.handle_inbound(env))
    run(b.transport.handle_inbound(env))
    run(b.transport.handle_inbound(env))
    assert len(b.transport.list_inbox()) == 1


def test_duplicate_returns_equivalent_valid_ack(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    ib = b.transport.ensure_identity()
    env = _envelope_of(a, run(send(a, pb)))
    ack1 = run(b.transport.handle_inbound(env))
    ack2 = run(b.transport.handle_inbound(env))
    assert ack2["duplicate"] is True
    assert ack2["original_message_id"] == ack1["original_message_id"]
    ok, _ = verify_ack(ack2, public_key_b64=ib.public_key_b64,
                       expected_message_id=env["message_id"], expected_node_id="node-B")
    assert ok


def test_same_id_different_hash_rejected(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    ia = a.transport.ensure_identity()
    env = _envelope_of(a, run(send(a, pb)))
    run(b.transport.handle_inbound(env))
    # Re-sign a DIFFERENT payload under the SAME message id (validly signed by A).
    from aithernet.transport.envelope import MessageEnvelope

    forged = MessageEnvelope.model_validate(env).model_copy(
        update={"payload": {"subject": "s", "text": "DIFFERENT"}, "signature": None}
    ).sign(ia)
    with pytest.raises(MessageIdCollisionError):
        run(b.transport.handle_inbound(forged.model_dump(mode="json")))
    assert len(b.transport.list_inbox()) == 1  # original untouched


def test_duplicate_count_tracked(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    env = _envelope_of(a, run(send(a, pb)))
    run(b.transport.handle_inbound(env))
    run(b.transport.handle_inbound(env))
    run(b.transport.handle_inbound(env))
    assert b.transport.list_inbox()[0].duplicate_count == 2


def test_inbox_survives_restart(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    env = _envelope_of(a, run(send(a, pb)))
    run(b.transport.handle_inbound(env))
    # Rebuild node B over the same database + identity dir (simulated restart).
    b2 = make_runtime(tmp_path, "node-B", "B")
    assert len(b2.transport.list_inbox()) == 1


# -- acknowledgement verification (29-34) ----------------------------------------


def _ack_setup(tmp_path):
    ia = IdentityManager(tmp_path / "A", node_id="node-A", node_name="A").initialize()
    ib = IdentityManager(tmp_path / "B", node_id="node-B", node_name="B").initialize()
    ack = build_ack(ib, original_message_id="m-1", status=AckStatus.ACCEPTED,
                    inbox_record_id="r1", duplicate=False)
    return ia, ib, ack


def test_valid_signed_ack_verified(tmp_path) -> None:
    _, ib, ack = _ack_setup(tmp_path)
    ok, _ = verify_ack(ack, public_key_b64=ib.public_key_b64,
                       expected_message_id="m-1", expected_node_id="node-B")
    assert ok


def test_unsigned_ack_rejected(tmp_path) -> None:
    _, ib, ack = _ack_setup(tmp_path)
    ack["signature"] = None
    ok, reason = verify_ack(ack, public_key_b64=ib.public_key_b64,
                            expected_message_id="m-1", expected_node_id="node-B")
    assert not ok and reason == "ack_unsigned"


def test_wrong_peer_ack_rejected(tmp_path) -> None:
    ia, ib, ack = _ack_setup(tmp_path)
    ok, _ = verify_ack(ack, public_key_b64=ia.public_key_b64,  # wrong key
                       expected_message_id="m-1", expected_node_id="node-B")
    assert not ok


def test_wrong_message_ack_rejected(tmp_path) -> None:
    _, ib, ack = _ack_setup(tmp_path)
    ok, reason = verify_ack(ack, public_key_b64=ib.public_key_b64,
                            expected_message_id="other", expected_node_id="node-B")
    assert not ok and reason == "ack_wrong_message"


def test_tampered_ack_rejected(tmp_path) -> None:
    _, ib, ack = _ack_setup(tmp_path)
    ack["status"] = "rejected"  # mutate a signed field
    ok, reason = verify_ack(ack, public_key_b64=ib.public_key_b64,
                            expected_message_id="m-1", expected_node_id="node-B")
    assert not ok and reason == "ack_bad_signature"


def test_http_success_without_valid_ack_not_acknowledged(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    # Routing transport returns HTTP 200 but a forged/unsigned ack body.
    bad = RoutingTransport(b, ack_override={"status": "accepted", "signature": "bm8="})
    status = _claim_deliver(a, rid, bad)
    assert status != "acknowledged"
    assert a.transport.get_outbox_read(rid).acknowledged_at is None


# -- outbox / delivery worker (35-51) --------------------------------------------


def test_queue_and_claim(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    rec = a.transport.get_outbox_read(rid)
    assert rec.status == "pending" and rec.attempt_count == 0


def test_two_workers_cannot_claim_same_record(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    now = utcnow()
    with a.session_scope() as s1, a.session_scope() as s2:
        r1 = AgentOutboxRepository(s1).claim(rid, owner="w1", now=now,
                                             claim_expiry=now + timedelta(seconds=30))
        s1.commit()
        r2 = AgentOutboxRepository(s2).claim(rid, owner="w2", now=now,
                                             claim_expiry=now + timedelta(seconds=30))
        s2.commit()
    assert (r1, r2) == (1, 0)


def test_successful_delivery_acknowledged(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, RoutingTransport(b)) == "acknowledged"
    rec = a.transport.get_outbox_read(rid)
    assert rec.status == "acknowledged" and rec.acknowledged_at is not None


def test_connection_error_schedules_retry(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, FailingTransport(conn_error())) == "retry_scheduled"


def test_timeout_schedules_retry(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, FailingTransport(timeout_error())) == "retry_scheduled"


def test_5xx_schedules_retry(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, FailingTransport(http_5xx())) == "retry_scheduled"


def test_permanent_4xx_does_not_retry(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, FailingTransport(http_4xx())) == "failed"


def test_invalid_signature_does_not_retry(tmp_path) -> None:
    # Peer B does not trust A -> inbound rejects with 401 invalid/უntrusted -> permanent.
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b, trust=False))
    rid = run(send(a, pb))
    status = _claim_deliver(a, rid, RoutingTransport(b))
    assert status == "failed"  # 403/401 permanent, never retried


def test_bounded_exponential_backoff(tmp_path) -> None:
    a, b = _two(tmp_path, max_attempts=5, initial_backoff_seconds=1.0,
                maximum_backoff_seconds=60.0, jitter_seconds=0.0)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    delays = []
    for _ in range(3):
        _force_due(a, rid)
        before = utcnow()
        _claim_deliver(a, rid, FailingTransport(conn_error()))
        nxt = _aware(_raw_outbox(a, rid)["next_attempt_at"])
        delays.append((nxt - before).total_seconds())
    # Strictly increasing (1, 2, 4 ...) and bounded by the maximum.
    assert delays[0] < delays[1] < delays[2]
    assert all(d <= 60.0 + 1 for d in delays)


def test_max_attempts_leads_to_dead_letter(tmp_path) -> None:
    a, b = _two(tmp_path, max_attempts=2)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert _claim_deliver(a, rid, FailingTransport(http_5xx())) == "retry_scheduled"
    _force_due(a, rid)
    assert _claim_deliver(a, rid, FailingTransport(http_5xx())) == "dead_letter"


def test_retry_uses_same_message_id(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    original = a.transport.get_outbox_read(rid).message_id
    _claim_deliver(a, rid, FailingTransport(conn_error()))
    assert a.transport.get_outbox_read(rid).message_id == original


def test_restart_recovers_expired_in_flight_claim(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    # Simulate an in-flight claim whose lease expired (worker crashed).
    with a.session_scope() as s:
        AgentOutboxRepository(s).update(rid, fields={
            "status": "in_flight", "claim_owner": "dead",
            "claim_expires_at": utcnow() - timedelta(seconds=60),
        })
        s.commit()
    original = a.transport.get_outbox_read(rid).message_id
    with a.session_scope() as s:
        recovered = AgentOutboxRepository(s).recover_expired_claims(utcnow())
        s.commit()
    rec = _raw_outbox(a, rid)
    assert recovered == 1 and rec["status"] == "retry_scheduled"
    assert rec["claim_owner"] is None and rec["message_id"] == original


def test_cancellation_prevents_delivery(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))
    assert run(a.transport.cancel_message(rid)) is True
    # A cancelled record is terminal and not claimable.
    now = utcnow()
    with a.session_scope() as s:
        claimed = AgentOutboxRepository(s).claim(rid, owner="w", now=now,
                                                 claim_expiry=now + timedelta(seconds=30))
        s.commit()
    assert claimed == 0
    assert a.transport.get_outbox_read(rid).status == "cancelled"


def test_expired_message_is_not_sent(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb, expires_in_seconds=-5))  # already expired
    transport = RoutingTransport(b)
    assert _claim_deliver(a, rid, transport) == "failed"
    assert transport.calls == 0  # never put on the wire
    assert len(b.transport.list_inbox()) == 0


def test_disabled_worker_sends_nothing(tmp_path) -> None:
    a = make_runtime(tmp_path, "node-A", "A")  # worker_enabled defaults to False
    b = make_runtime(tmp_path, "node-B", "B")
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))

    async def scenario():
        await a.transport.start()  # outbound.enabled is False -> no worker
        a.transport._transport = RoutingTransport(b)
        await asyncio.sleep(0.3)
        await a.transport.shutdown()

    run(scenario())
    assert a.transport.get_outbox_read(rid).status == "pending"
    assert len(b.transport.list_inbox()) == 0


def test_worker_delivers_and_shutdown_leaves_no_claim(tmp_path) -> None:
    a = make_runtime(tmp_path, "node-A", "A", worker_enabled=True, poll_interval_seconds=0.1)
    b = make_runtime(tmp_path, "node-B", "B")
    pb, _ = run(init_and_peer(a, b))
    rid = run(send(a, pb))

    async def scenario():
        await a.transport.start()
        a.transport._transport = RoutingTransport(b)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if a.transport.get_outbox_read(rid).status == "acknowledged":
                break
        await a.transport.shutdown()

    run(scenario())
    rec = _raw_outbox(a, rid)
    assert rec["status"] == "acknowledged"
    assert rec["claim_owner"] is None  # no record left claimed after shutdown


# -- manifest persistence (57) ---------------------------------------------------


def test_peer_manifest_persistence(tmp_path) -> None:
    a, b = _two(tmp_path)
    pb, _ = run(init_and_peer(a, b))
    a.transport._transport = RoutingTransport(b)
    manifest = run(a.transport.refresh_manifest(pb))
    peer = a.transport.get_peer_read(pb)
    assert peer.capability_snapshot is not None
    assert peer.capability_snapshot_at is not None
    assert manifest["node_id"] == "node-B"
