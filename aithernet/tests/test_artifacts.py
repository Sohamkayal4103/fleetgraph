"""Stage 13D.2 artifact control-plane, protocol, permissions, and migration tests (Part L).

Two in-process comms nodes wired with the routing fake transport carry the signed control
messages (offer/request/grant/reject) without a network. The BYTE plane (streaming download,
resume) is exercised by the real two-node test in test_artifact_two_node.py; the store byte
mechanics are covered in test_artifact_store.py. Here we verify identity, permissions
(conservative + separate from trust), the signed protocol, grant binding, idempotency, the
coordinator route restrictions, that bytes never enter SQLite, and the migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from _comms_util import deliver, two_nodes
from aithernet.artifacts.protocol import (
    ArtifactControlError,
    ArtifactControlMessage,
    ArtifactControlType,
    parse_artifact_control,
)
from aithernet.artifacts.service import ArtifactError
from aithernet.state.repositories import ArtifactPermissionRepository, ArtifactTransferRepository

run = asyncio.run


def _set_artifact_perms(node, peer_id, **fields):
    with node.session_scope() as session:
        ArtifactPermissionRepository(session).update(peer_id, fields=fields)
        session.commit()


def _index(node, tmp_path, name="cap.bin", data=b"RFDATA" * 5000):
    src = tmp_path / name
    src.write_bytes(data)
    return node.artifacts.index_local_artifact(
        source_path=str(src), backend_id="marconi", artifact_kind="capture", display_name=name,
    ), data


# == protocol ================================================================================


def test_protocol_roundtrip_and_detect():
    msg = ArtifactControlMessage(
        message_type=ArtifactControlType.GRANT, transfer_id="t1",
        digest="sha256:" + "a" * 64, size=123,
    )
    parsed = parse_artifact_control(msg.to_payload())
    assert parsed.message_type is ArtifactControlType.GRANT and parsed.size == 123


def test_protocol_rejects_bad_digest_and_application():
    with pytest.raises(ArtifactControlError):
        parse_artifact_control({"application": "evil"})
    with pytest.raises(ArtifactControlError):
        parse_artifact_control({
            "application": "aithernet.artifact-control", "application_version": "1",
            "message_type": "artifact_grant", "transfer_id": "t", "digest": "md5:x",
        })


def test_protocol_carries_no_bytes_or_paths():
    # The schema simply has no field for bytes/URL/path/keys/headers.
    fields = set(ArtifactControlMessage.model_fields)
    for forbidden in ("bytes", "url", "path", "signature", "key", "headers", "retry"):
        assert not any(forbidden in f for f in fields)


# == artifact identity + store-not-in-sqlite =================================================


def test_index_creates_transferable_identity(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, data = _index(a, tmp_path)
    assert art["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert art["size_bytes"] == len(data) and art["availability_state"] == "local"
    assert art["origin_node_id"] == a.config.node_id


def test_bytes_never_enter_sqlite(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, data = _index(a, tmp_path, data=os.urandom(20000))
    # The raw bytes must not appear anywhere in the SQLite file.
    db_path = a.config.database_url.replace("sqlite:///", "")
    raw = open(db_path, "rb").read()
    assert data[:64] not in raw
    # The digest (hex, safe metadata) may appear; the bytes must not.


# == permissions: conservative + separate from trust ========================================


def test_artifact_permissions_default_conservative(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)  # peers are TRUSTED by default
    perms = a.artifacts.peer_artifact_permissions(pb)
    assert perms["may_offer_artifacts"] is False
    assert perms["may_request_artifacts"] is False
    assert perms["may_receive_artifacts"] is False


def test_artifact_permission_update_is_separate_from_message_perms(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    run(a.artifacts.set_peer_artifact_permissions(pb, {"may_request_artifacts": True}))
    perms = a.artifacts.peer_artifact_permissions(pb)
    assert perms["may_request_artifacts"] is True
    # Trust + message perms are untouched fields.
    assert perms["trust_state"] == "trusted"


# == control plane: offer recorded only when authorized ======================================


def test_offer_recorded_only_when_peer_may_offer(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, _ = _index(b, tmp_path)

    async def main():
        res = await b.artifacts.offer_artifact(artifact_id=art["artifact_id"], peer_id=pa)
        await deliver(b, _outbox_id(b, res["transfer_id"]))
    # A has NOT granted may_offer_artifacts to B -> offer ignored.
    run(main())
    assert a.artifacts.list_remote_offers() == []
    # Authorize and re-offer.
    _set_artifact_perms(a, pb, may_offer_artifacts=True)

    async def main2():
        res = await b.artifacts.offer_artifact(artifact_id=art["artifact_id"], peer_id=pa)
        await deliver(b, _outbox_id(b, res["transfer_id"]))
    run(main2())
    offers = a.artifacts.list_remote_offers()
    assert len(offers) == 1 and offers[0]["digest"] == art["digest"]


# == control plane: request -> grant (authorized) / reject (unauthorized) ====================


def test_request_unauthorized_is_rejected_no_grant(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, _ = _index(b, tmp_path)

    async def main():
        # A requests without B authorizing -> B rejects; A's transfer becomes rejected.
        res = await a.artifacts.request_artifact(
            peer_id=pb, origin_artifact_id=art["artifact_id"], digest=art["digest"],
            size=art["size_bytes"],
        )
        await deliver(a, _outbox_id(a, res["transfer_id"]))  # request -> B
        # B's reject goes back to A.
        reject_id = _latest_outbox(b)
        if reject_id:
            await deliver(b, reject_id)
        return res["transfer_id"]
    tid = run(main())
    t = a.artifacts.get_transfer(tid)
    assert t["state"] == "rejected"
    # B created no outbound grant transfer.
    assert all(x["state"] == "offered" or x["direction"] != "outbound"
               for x in b.artifacts.list_transfers())


def test_request_authorized_produces_bound_grant(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, _ = _index(b, tmp_path)
    _set_artifact_perms(b, pa, may_request_artifacts=True, may_receive_artifacts=True)

    async def main():
        res = await a.artifacts.request_artifact(
            peer_id=pb, origin_artifact_id=art["artifact_id"], digest=art["digest"],
            size=art["size_bytes"],
        )
        await deliver(a, _outbox_id(a, res["transfer_id"]))  # request -> B (creates grant)
        await deliver(b, _latest_outbox(b))  # grant -> A
        return res["transfer_id"]
    tid = run(main())
    # A's inbound transfer is authorized; B's outbound grant binds the exact receiver + digest.
    ta = a.artifacts.get_transfer(tid)
    assert ta["state"] == "authorized"
    with b.session_scope() as session:
        grant = ArtifactTransferRepository(session).get_by_transfer_id(tid)
        assert grant.direction == "outbound"
        assert grant.expected_receiver_node_id == a.config.node_id
        assert grant.expected_digest == art["digest"]


def test_duplicate_request_creates_one_grant(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, _ = _index(b, tmp_path)
    _set_artifact_perms(b, pa, may_request_artifacts=True, may_receive_artifacts=True)

    async def main():
        res = await a.artifacts.request_artifact(
            peer_id=pb, origin_artifact_id=art["artifact_id"], digest=art["digest"],
            size=art["size_bytes"],
        )
        rid = _outbox_id(a, res["transfer_id"])
        await deliver(a, rid)
        await deliver(a, rid)  # re-deliver same request -> idempotent
        return res["transfer_id"]
    tid = run(main())
    with b.session_scope() as session:
        rows = [x for x in ArtifactTransferRepository(session).list()
                if x.transfer_id == tid and x.direction == "outbound"]
        assert len(rows) == 1  # one grant, not two


# == grant binding: serve_range refuses the wrong receiver / expired =========================


def test_serve_range_refuses_wrong_receiver(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)
    art, data = _index(b, tmp_path)
    _set_artifact_perms(b, pa, may_request_artifacts=True, may_receive_artifacts=True)

    async def main():
        res = await a.artifacts.request_artifact(
            peer_id=pb, origin_artifact_id=art["artifact_id"], digest=art["digest"],
            size=art["size_bytes"],
        )
        await deliver(a, _outbox_id(a, res["transfer_id"]))
        return res["transfer_id"]
    tid = run(main())
    # The correct receiver may pull; a different node id is refused (grant binding).
    chunk = b.artifacts.serve_range(sender_node_id=a.config.node_id, transfer_id=tid,
                                    offset=0, length=10)
    assert chunk == data[:10]
    with pytest.raises(ArtifactError) as exc:
        b.artifacts.serve_range(sender_node_id="someone-else", transfer_id=tid, offset=0, length=10)
    assert exc.value.code in ("grant_mismatch", "grant_not_found")


# == coordinator route restrictions ==========================================================


def test_coordinator_route_cannot_specify_url_or_path():
    from aithernet.coordinator.contracts import CoordinatorDecision
    from aithernet.orchestrator.decision_router import (
        RouteValidationError,
        peer_artifact_from_decision,
    )

    def mk(payload):
        return CoordinatorDecision.from_model_json({
            "summary": "s", "next_target": "peer_artifact", "action": "x", "message": "m",
            "structured_payload": payload, "expected_result": "r",
        }, mission_id="m1")

    spec = peer_artifact_from_decision(mk({
        "action": "request", "peer_id": "p1", "artifact_id": "a1", "purpose": "compare",
        # malicious extras the extractor must IGNORE:
        "url": "http://evil", "path": "/etc/passwd", "digest": "sha256:0", "destination": "/x",
        "http_range": "0-100", "headers": {"x": "y"}, "signing_identity": "k",
    }))
    assert set(spec) == {"action", "peer_id", "artifact_id", "purpose", "conversation_id"}
    assert "url" not in spec and "path" not in spec
    with pytest.raises(RouteValidationError):
        peer_artifact_from_decision(mk({"action": "request", "peer_id": "p1"}))  # no artifact id


# == migration from the Stage 13D-foundation database ========================================


def test_migration_adds_artifact_columns_and_tables(tmp_path):
    from sqlalchemy import inspect

    from aithernet.state.db import create_db_engine
    from aithernet.state.migrations import run_migrations, validate_schema

    path = str(tmp_path / "n.db")
    engine = create_db_engine(f"sqlite:///{path}")
    run_migrations(engine)
    assert validate_schema(engine).ok
    insp = inspect(engine)
    ea_cols = {c["name"] for c in insp.get_columns("external_agents")}
    assert {"may_offer_artifacts", "may_request_artifacts", "may_receive_artifacts"} <= ea_cols
    rf_cols = {c["name"] for c in insp.get_columns("rf_artifacts")}
    assert {"digest", "object_ref", "availability_state", "pinned"} <= rf_cols
    tables = set(insp.get_table_names())
    assert {"artifact_transfers", "artifact_transfer_attempts",
            "remote_artifact_references"} <= tables


# -- helpers -----------------------------------------------------------------


def _outbox_id(node, transfer_id):
    """The outbox record id for the control message correlated to a transfer id."""
    from aithernet.state.repositories import AgentOutboxRepository
    with node.session_scope() as session:
        rows = AgentOutboxRepository(session).list(limit=50)
        for r in rows:
            if r.correlation_id == transfer_id:
                return r.id
    return None


def _latest_outbox(node):
    from aithernet.state.repositories import AgentOutboxRepository
    with node.session_scope() as session:
        rows = AgentOutboxRepository(session).list(limit=1)
        return rows[0].id if rows else None
