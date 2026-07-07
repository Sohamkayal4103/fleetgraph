"""Stage 13D.3 remote-mission API + CLI tests (TestClient + in-thread server).

Verifies the bounded, sanitized API surface, that reads create no side effects, that the
status-query action is reachable, that mission-status permissions are managed separately from
trust, and that the CLI mirrors the API without printing secrets.
"""

from __future__ import annotations

import asyncio
import threading
import time

import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _comms_util import deliver, set_perms, two_nodes
from aithernet.api.app import create_app
from aithernet.cli import app as cli_app
from aithernet.comms.status_protocol import MissionStatusMessage, MissionStatusType
from aithernet.schemas.missions import MissionCreate
from aithernet.state.repositories import MissionExecutionRunRepository, MissionRepository
from aithernet.transport.envelope import MessageKind, build_envelope

run = asyncio.run
runner = CliRunner()


def _seed_snapshot(tmp_path):
    """Build A with one authenticated remote snapshot from B (state=waiting, seq=2)."""
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_receive_mission_status=True, may_query_mission_status=True)
    set_perms(a, pb, may_publish_mission_status=True)

    async def scenario():
        mission = await a.create_mission(MissionCreate(content="local", source_type="user"))
        run_read, _ = await a.worker_manager.enqueue_mission(mission.id)
        with a.session_scope() as session:
            MissionRepository(session).update_status(mission.id, "active")
            MissionExecutionRunRepository(session).update(run_read.id, fields={"status": "active"})
            session.commit()
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text="work", data={}, expects_reply=True,
            conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
            response_deadline=None, mission_id=mission.id, mission_run_id=None,
            mission_step_id=None, causation_id=None,
        )
        await deliver(a, res["outbox_record_id"])
        identity = b.transport.ensure_identity()
        msg = MissionStatusMessage(
            message_type=MissionStatusType.UPDATE, origin_node_id=b.config.node_id,
            remote_mission_id="remote-1", inbound_request_id=res["request_id"],
            state="waiting", sequence=2,
        )
        env = build_envelope(
            identity=identity, recipient_node_id=a.config.node_id, recipient_agent_id=None,
            kind=MessageKind.AGENT_MESSAGE.value, payload=msg.to_payload(),
            conversation_id=None, correlation_id=res["request_id"], content_type="application/json",
        )
        await a.transport.handle_inbound(env.authenticated_dict())
        return mission.id

    mid = run(scenario())
    return a, b, pb, pa, mid


def test_remote_mission_api_list_detail_events(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        rows = client.get("/remote-missions").json()
        assert len(rows) == 1
        sid = rows[0]["snapshot_id"]
        assert rows[0]["state"] == "waiting" and rows[0]["latest_sequence"] == 2
        assert rows[0]["freshness"] in ("fresh", "stale", "terminal")
        detail = client.get(f"/remote-missions/{sid}").json()
        assert detail["snapshot_id"] == sid
        events = client.get(f"/remote-missions/{sid}/events").json()
        assert any(e["disposition"] == "accepted" for e in events)
        assert client.get("/remote-missions/ghost").status_code == 404
        # no secrets in any response
        blob = (client.get("/remote-missions").text + detail.__str__()).lower()
        for secret in ("signature", "-----begin", "private", "envelope", "prompt"):
            assert secret not in blob


def test_remote_mission_query_action(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        sid = client.get("/remote-missions").json()[0]["snapshot_id"]
        before = len(a.transport.list_outbox())
        res = client.post(f"/remote-missions/{sid}/query").json()
        assert res["queued"] is True
        # one query message queued through the durable outbox
        assert len(a.transport.list_outbox()) == before + 1


def test_reads_create_no_side_effects(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        before = len(a.transport.list_outbox())
        sid = client.get("/remote-missions").json()[0]["snapshot_id"]
        for _ in range(5):
            client.get("/remote-missions")
            client.get(f"/remote-missions/{sid}")
            client.get(f"/remote-missions/{sid}/events")
        assert len(a.transport.list_outbox()) == before  # pure reads send nothing


def test_status_permissions_separate_from_trust(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        perms = client.get(f"/peers/{pb}/mission-status-permissions").json()
        assert perms["may_publish_mission_status"] is True  # granted in seed
        assert perms["may_query_mission_status"] is False
        # trust is reported but is a distinct field
        assert perms["trust_state"] == "trusted"
        updated = client.patch(
            f"/peers/{pb}/mission-status-permissions",
            json={"may_query_mission_status": True},
        ).json()
        assert updated["may_query_mission_status"] is True
        assert client.get("/peers/ghost/mission-status-permissions").status_code == 404


def test_status_publications_endpoint(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    # B's inbound mission has publications; A's local initiating mission does not.
    with b.session_scope() as session:
        bmid = [m for m in MissionRepository(session).list(limit=20)
                if m.source_type == "peer_request"][0].id
    with TestClient(create_app(runtime=b)) as client:
        pubs = client.get(f"/missions/{bmid}/status-publications").json()
        assert pubs and pubs[0]["last_sequence"] >= 1


def test_distributed_timeline_has_remote_status_and_topology(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        data = client.get(f"/missions/{mid}/distributed-timeline").json()
        assert data["remote_missions"], "remote snapshot should appear on the initiating mission"
        kinds = {n["kind"] for n in data["topology"]["nodes"]}
        assert "remote_mission" in kinds
        edge_kinds = {e["kind"] for e in data["topology"]["edges"]}
        assert "remote_status_synced" in edge_kinds
        # remote node carries authenticated state + freshness, never inferred terminal
        rm_node = next(n for n in data["topology"]["nodes"] if n["kind"] == "remote_mission")
        assert rm_node["status"] == "waiting" and "freshness" in rm_node


def test_cli_remote_mission_commands(tmp_path) -> None:
    a, b, pb, pa, mid = _seed_snapshot(tmp_path)
    server = uvicorn.Server(uvicorn.Config(create_app(runtime=a), host="127.0.0.1", port=8261,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server start timeout")
        time.sleep(0.02)
    try:
        url = "http://127.0.0.1:8261"
        listing = runner.invoke(cli_app, ["remote-mission", "list", "--url", url])
        assert listing.exit_code == 0 and "freshness" in listing.stdout
        perms = runner.invoke(
            cli_app, ["peer", "mission-status-permissions", "show", pb, "--url", url]
        )
        assert perms.exit_code == 0 and "may_publish_mission_status" in perms.stdout
        setp = runner.invoke(cli_app, [
            "peer", "mission-status-permissions", "set", pb, "--may-query", "--url", url
        ])
        assert setp.exit_code == 0
        assert "signature" not in (listing.stdout + perms.stdout).lower()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
