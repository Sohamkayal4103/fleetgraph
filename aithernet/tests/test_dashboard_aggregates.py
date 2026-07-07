"""Stage 13C dashboard aggregate read-model + operator-composer tests (Part S, backend).

Uses two in-process comms-enabled nodes wired with the routing fake transport (no network,
Gemini, Codex, GNU Radio, or Marconi). Asserts the aggregate endpoints are bounded, sanitized,
read-only, preserve the ACK-vs-semantic-reply and trust-vs-authorization distinctions, derive
topology only from persisted correlations, and never expose keys/signatures/envelopes.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from _comms_util import active_mission, deliver, set_perms, two_nodes
from aithernet.api.app import create_app

run = asyncio.run

_SECRET_MARKERS = (
    "signature", "public_key", "-----begin", "envelope_json", "private", "authorization",
)


def _no_secrets(*texts: str) -> bool:
    blob = " ".join(texts).lower()
    return not any(marker in blob for marker in _SECRET_MARKERS)


def _outbound_request(a, pb, *, expects_reply=True, with_wait=True, text="What can you do?"):
    async def scenario():
        mid, rid = await active_mission(a)
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text=text, data={"k": 1},
            expects_reply=expects_reply, conversation_id=None, reply_to_message_id=None,
            reply_to_request_id=None, response_deadline=None, mission_id=mid,
            mission_run_id=rid, mission_step_id=None, causation_id=None,
        )
        await deliver(a, res["outbox_record_id"])
        if with_wait:
            await a.comms.prepare_peer_reply_wait(
                mission_id=mid, mission_run_id=rid, mission_step_id=None,
                outbound_message_id=res["message_id"], deadline=None,
            )
        return mid, res
    return run(scenario())


# == 1. overview aggregate ===================================================================


def test_overview_aggregate(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        r = client.get("/overview")
        assert r.status_code == 200
        data = r.json()
        # Health comes from worker/runtime state, not a configured flag.
        assert "running" in data["workers"]["mission"]
        assert "running" in data["workers"]["transport"]
        assert data["peers"]["total"] == 1 and data["peers"]["trusted"] == 1
        assert data["reply_waits"]["pending"] == 1
        assert data["outbox"]["acknowledged"] == 1  # routing transport ACKs delivery
        fp = data["node"]["fingerprint"]
        assert fp is None or fp.startswith("sha256:")  # abbreviated, never a full key
        assert _no_secrets(r.text)


# == 2. fleet aggregate ======================================================================


def test_fleet_separates_trust_from_authorization(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        peers = client.get("/fleet").json()["peers"]
        assert len(peers) == 1
        peer = peers[0]
        assert peer["trust_state"] == "trusted"  # trust is its own field
        assert "permissions" in peer and "may_send_requests" in peer["permissions"]  # auth separate
        assert peer["transport_health"] in ("healthy", "unknown", "failing", "disabled")
        assert peer["pending_reply_waits"] == 1
        assert "public_key" not in peer  # never a full key
        assert peer["fingerprint"] is None or peer["fingerprint"].startswith("sha256:")
        assert _no_secrets(client.get("/fleet").text)


# == 3. conversation timeline (ACK distinct from semantic reply) =============================


def test_conversation_timeline_distinguishes_ack_from_reply(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _mid, res = _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        tl = client.get(f"/conversations/{res['conversation_id']}/timeline")
        assert tl.status_code == 200
        data = tl.json()
        out = [e for e in data["entries"] if e.get("direction") == "outbound"]
        assert len(out) == 1
        entry = out[0]
        # ACK is delivery, NOT a semantic answer — separate booleans.
        assert entry["transport_acknowledged"] is True
        assert entry["semantic_reply"] is False
        assert entry["text_preview"] == "What can you do?"  # bounded preview present
        assert "transport_acknowledged" in data["legend"] and "semantic_reply" in data["legend"]
        assert any(e["kind"].startswith("reply_wait") for e in data["entries"])
        assert _no_secrets(tl.text)


def test_conversation_timeline_404(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        assert client.get("/conversations/nope/timeline").status_code == 404


# == 4. distributed mission timeline (topology from persisted correlations) ==================


def test_distributed_mission_timeline_topology(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, _res = _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        dt = client.get(f"/missions/{mid}/distributed-timeline")
        assert dt.status_code == 200
        data = dt.json()
        assert data["mission_status"]
        assert len(data["outbound_messages"]) == 1
        assert len(data["reply_waits"]) == 1
        node_kinds = {n["kind"] for n in data["topology"]["nodes"]}
        assert "local_mission" in node_kinds and "peer" in node_kinds
        peer_nodes = [n for n in data["topology"]["nodes"] if n["kind"] == "peer"]
        assert all(n["status"] == "remote_unknown" for n in peer_nodes)  # never fabricated
        assert _no_secrets(dt.text)


def test_distributed_inbound_mission_shows_obligation_and_source(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(b, pa, may_send_requests=True, may_request_response=True)

    async def scenario():
        mid, rid = await active_mission(a)
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text="please answer", data={},
            expects_reply=True, conversation_id=None, reply_to_message_id=None,
            reply_to_request_id=None, response_deadline=None, mission_id=mid,
            mission_run_id=rid, mission_step_id=None, causation_id=None,
        )
        await deliver(a, res["outbox_record_id"])  # B creates an inbound mission
        return res
    res = run(scenario())

    with TestClient(create_app(runtime=b)) as client:
        irs = client.get("/inbound-requests").json()
        assert len(irs) == 1 and irs[0]["mission_id"]
        bmid = irs[0]["mission_id"]
        dt = client.get(f"/missions/{bmid}/distributed-timeline").json()
        assert dt["is_inbound_mission"] is True
        assert dt["response_obligation"] is not None
        assert dt["response_obligation"]["response_required"] is True
        assert dt["inbound_request"]["request_id"] == res["request_id"]
        assert any(e["kind"] == "inbound_request" for e in dt["topology"]["edges"])


# == 5. communications summary ===============================================================


def test_communications_summary(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        cs = client.get("/communications/summary")
        assert cs.status_code == 200
        data = cs.json()
        assert data["reply_waits"].get("pending") == 1
        assert data["peers"]["trusted"] == 1
        assert "outbox_counts" in data and "inbox_count" in data
        assert _no_secrets(cs.text)


# == 6. operator composer ====================================================================


def test_operator_send_requires_trusted_authorized_peer(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        before = len(a.transport.list_outbox())
        r = client.post("/communications/send", json={
            "peer_id": pb, "message_type": "request", "text": "hi", "expects_reply": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["origin"] == "operator" and body["status"] == "pending"
        assert body["expects_reply"] is True
        assert len(a.transport.list_outbox()) == before + 1  # exactly one outbox record
        assert client.post(
            "/communications/send", json={"peer_id": "ghost", "text": "x"}
        ).status_code == 404
        client.post(f"/peers/{pb}/revoke")
        assert client.post(
            "/communications/send", json={"peer_id": pb, "text": "x"}
        ).status_code == 403


def test_operator_send_blocked_without_receive_permission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    set_perms(a, pb, may_receive_messages=False)
    with TestClient(create_app(runtime=a)) as client:
        r = client.post("/communications/send", json={"peer_id": pb, "text": "x"})
        assert r.status_code == 403


def test_operator_send_rejects_bad_json_data(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        # data must be an object — a list is rejected by schema validation (422).
        r = client.post("/communications/send", json={"peer_id": pb, "text": "x", "data": [1, 2]})
        assert r.status_code == 422


def test_operator_send_does_not_create_a_mission(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        before = len(client.get("/missions").json())
        client.post("/communications/send", json={"peer_id": pb, "text": "hi"})
        assert len(client.get("/missions").json()) == before  # never creates a local mission


def test_operator_message_distinguishable_from_coordinator(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        sent = client.post("/communications/send", json={"peer_id": pb, "text": "manual"}).json()
        tl = client.get(f"/conversations/{sent['conversation_id']}/timeline").json()
        out = [e for e in tl["entries"] if e.get("direction") == "outbound"]
        # No mission_id ⇒ operator-originated (coordinator messages always carry a mission_id).
        assert out and all(e.get("mission_id") in (None, "") for e in out)


# == 7. read endpoints create no side effects ================================================


def test_reads_create_no_side_effects(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    mid, res = _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        before_outbox = len(a.transport.list_outbox())
        before_waits = len(client.get("/reply-waits").json())
        for _ in range(5):
            client.get("/overview")
            client.get("/fleet")
            client.get(f"/conversations/{res['conversation_id']}/timeline")
            client.get(f"/missions/{mid}/distributed-timeline")
            client.get("/communications/summary")
            client.get("/reply-waits")
        assert len(a.transport.list_outbox()) == before_outbox  # nothing sent
        waits = client.get("/reply-waits").json()
        assert len(waits) == before_waits and all(w["state"] == "pending" for w in waits)


# == 8/9. global reply-waits + state filter ==================================================


def test_global_reply_waits_and_state_filter(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    _outbound_request(a, pb)
    with TestClient(create_app(runtime=a)) as client:
        allw = client.get("/reply-waits").json()
        assert len(allw) == 1 and allw[0]["state"] == "pending"
        assert len(client.get("/reply-waits?state=pending").json()) == 1
        assert client.get("/reply-waits?state=satisfied").json() == []


# == 10. pagination/limit bounds enforced ====================================================


def test_aggregate_limit_bounds(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        assert client.get("/fleet?limit=0").status_code == 422  # below min
        assert client.get("/fleet?limit=5").status_code == 200
        assert client.get("/reply-waits?limit=99999").status_code == 422  # above max


# == regression: existing comms endpoints unchanged ==========================================


def test_existing_comms_endpoints_unchanged(tmp_path) -> None:
    a, b, pb, pa = two_nodes(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        assert client.get("/conversations").status_code == 200
        assert client.get("/inbound-requests").status_code == 200
        assert client.get(f"/peers/{pb}/permissions").status_code == 200


# == CLI parity (Part P) =====================================================================


def _serve(runtime, port):
    import threading
    import time

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(create_app(runtime=runtime), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server start timeout")
        time.sleep(0.02)
    return server, thread


def test_cli_stage13c_inspection_commands(tmp_path) -> None:
    from typer.testing import CliRunner

    from aithernet.cli import app as cli_app

    a, b, pb, pa = two_nodes(tmp_path)
    mid, _res = _outbound_request(a, pb)
    server, thread = _serve(a, 8351)
    runner = CliRunner()
    url = "http://127.0.0.1:8351"
    try:
        overview = runner.invoke(cli_app, ["overview", "--url", url])
        assert overview.exit_code == 0 and "Node:" in overview.stdout
        fleet = runner.invoke(cli_app, ["fleet", "list", "--url", url])
        assert fleet.exit_code == 0 and "trust=" in fleet.stdout
        show = runner.invoke(cli_app, ["fleet", "show", pb, "--url", url])
        assert show.exit_code == 0 and "authorization" in show.stdout.lower()
        summ = runner.invoke(cli_app, ["communication", "summary", "--url", url])
        assert summ.exit_code == 0
        waits = runner.invoke(cli_app, ["communication", "waits", "--url", url])
        assert waits.exit_code == 0
        dist = runner.invoke(cli_app, ["mission", "distributed-timeline", mid, "--url", url])
        assert dist.exit_code == 0 and "topology" in dist.stdout.lower()
        # operator send via CLI queues exactly one message
        send = runner.invoke(cli_app, ["conversation", "send", pb, "--text", "hi", "--url", url])
        assert send.exit_code == 0 and "origin=operator" in send.stdout
        # No secrets in any CLI output (ACK vs reply distinction is preserved as text).
        combined = (overview.stdout + fleet.stdout + show.stdout + dist.stdout).lower()
        assert "signature" not in combined and "-----begin" not in combined
        assert "private" not in combined
    finally:
        server.should_exit = True
        thread.join(timeout=5)
