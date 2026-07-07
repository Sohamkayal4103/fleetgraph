"""Stage 13B schema-bounds + API/CLI tests (Part U items 51-61 + Part B bounds)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from _comms_util import (
    active_mission,
    deliver,
    two_nodes,
)
from aithernet.api.app import create_app
from aithernet.cli import app as cli_app
from aithernet.comms.schema import (
    CoordinatorMessage,
    CoordinatorMessageError,
    is_coordinator_message,
    parse_coordinator_message,
)

run = asyncio.run


# -- application schema bounds (Part B) ------------------------------------------


def test_schema_detect_and_roundtrip() -> None:
    msg = CoordinatorMessage(message_type="request", text="hi", data={"k": 1}, expects_reply=True)
    payload = msg.to_payload()
    assert is_coordinator_message(payload)
    parsed = parse_coordinator_message(payload, max_text_characters=100, max_data_bytes=1000,
                                       max_data_depth=5)
    assert parsed.message_type.value == "request" and parsed.expects_reply


def test_schema_bounds_enforced() -> None:
    base = {"application": "aithernet.coordinator-message", "application_version": "1"}
    with pytest.raises(CoordinatorMessageError):  # text too large
        parse_coordinator_message({**base, "text": "x" * 200}, max_text_characters=100,
                                  max_data_bytes=1000, max_data_depth=5)
    with pytest.raises(CoordinatorMessageError):  # data too large
        parse_coordinator_message({**base, "data": {"x": "y" * 5000}}, max_text_characters=100,
                                  max_data_bytes=100, max_data_depth=5)
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    with pytest.raises(CoordinatorMessageError):  # nesting too deep
        parse_coordinator_message({**base, "data": deep}, max_text_characters=100,
                                  max_data_bytes=10000, max_data_depth=2)


def test_schema_rejects_unknown_application_and_version() -> None:
    with pytest.raises(CoordinatorMessageError):
        parse_coordinator_message({"application": "evil"}, max_text_characters=100,
                                  max_data_bytes=1000, max_data_depth=5)
    with pytest.raises(CoordinatorMessageError):
        parse_coordinator_message(
            {"application": "aithernet.coordinator-message", "application_version": "99"},
            max_text_characters=100, max_data_bytes=1000, max_data_depth=5,
        )


# -- API endpoints (51-56, 61) ---------------------------------------------------


def _api_setup(tmp_path):
    a, b, pb, pa = two_nodes(tmp_path)

    async def scenario():
        mid, rid = await active_mission(a)
        res = await a.comms.queue_peer_message(
            peer_id=pb, message_type="request", text="q", data={}, expects_reply=True,
            conversation_id=None, reply_to_message_id=None, reply_to_request_id=None,
            response_deadline=None, mission_id=mid, mission_run_id=rid, mission_step_id=None,
            causation_id=None)
        await deliver(a, res["outbox_record_id"])
        await a.comms.prepare_peer_reply_wait(
            mission_id=mid, mission_run_id=rid, mission_step_id=None,
            outbound_message_id=res["message_id"], deadline=None)
        return mid, res

    mid, res = run(scenario())
    return a, b, pb, pa, mid, res


def test_api_communications_and_reply_waits(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        comm = client.get(f"/missions/{mid}/communications").json()
        assert comm["outbound_messages"][0]["message_id"] == res["message_id"]
        assert comm["reply_waits"][0]["state"] == "pending"
        waits = client.get(f"/missions/{mid}/reply-waits").json()
        assert len(waits) == 1
        # cancel-wait endpoint
        wait_id = waits[0]["wait_id"]
        cancelled = client.post(f"/missions/{mid}/reply-waits/{wait_id}/cancel").json()
        assert cancelled["cancelled"] is True
        # no secrets in any comms response
        for path in (f"/missions/{mid}/communications", f"/missions/{mid}/reply-waits"):
            blob = client.get(path).text.lower()
            assert "signature" not in blob and "public_key" not in blob and "envelope" not in blob


def test_api_conversations_and_inbound_requests(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    with TestClient(create_app(runtime=b)) as client:
        convs = client.get("/conversations").json()
        assert any(c["conversation_id"] == res["conversation_id"] for c in convs)
        msgs = client.get(f"/conversations/{res['conversation_id']}/messages").json()
        assert len(msgs) >= 1
        irs = client.get("/inbound-requests").json()
        assert len(irs) == 1 and irs[0]["response_required"] is True
        one = client.get(f"/inbound-requests/{irs[0]['request_id']}").json()
        assert one["request_id"] == irs[0]["request_id"]


def test_api_peer_permissions(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        perms = client.get(f"/peers/{pb}/permissions").json()
        assert perms["may_receive_messages"] is True
        # PATCH permissions (separate from key trust)
        updated = client.patch(f"/peers/{pb}/permissions", json={"may_send_requests": True}).json()
        assert updated["may_send_requests"] is True
        assert client.get("/peers/ghost/permissions").status_code == 404


def test_api_polling_creates_no_messages_or_resumes(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        before = len(a.transport.list_outbox())
        for _ in range(5):
            client.get(f"/missions/{mid}/communications")
            client.get(f"/missions/{mid}/reply-waits")
            client.get("/conversations")
        after = len(a.transport.list_outbox())
        assert after == before  # read-only polling sends nothing
        # the pending wait is unchanged (no resume from polling)
        assert client.get(f"/missions/{mid}/reply-waits").json()[0]["state"] == "pending"


# -- CLI (57-58) -----------------------------------------------------------------

runner = CliRunner()


def test_cli_communication_and_permission_views(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    import threading
    import time

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(create_app(runtime=a), host="127.0.0.1", port=8242,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server start timeout")
        time.sleep(0.02)
    try:
        url = "http://127.0.0.1:8242"
        comm = runner.invoke(cli_app, ["mission", "communications", mid, "--url", url])
        assert comm.exit_code == 0 and "Reply waits" in comm.stdout
        waits = runner.invoke(cli_app, ["mission", "reply-waits", mid, "--url", url])
        assert waits.exit_code == 0
        perms = runner.invoke(cli_app, ["peer", "permissions", "show", pb, "--url", url])
        assert perms.exit_code == 0 and "may_send_requests" in perms.stdout
        # CLI permission update
        setp = runner.invoke(cli_app, [
            "peer", "permissions", "set", pb, "--may-send-requests", "--url", url
        ])
        assert setp.exit_code == 0
        convs = runner.invoke(cli_app, ["conversation", "list", "--url", url])
        assert convs.exit_code == 0
        # no secrets in CLI output
        assert "signature" not in (comm.stdout + perms.stdout).lower()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# -- regression: legacy external-agent + transport APIs unchanged (70) -----------


def test_legacy_apis_backward_compatible(tmp_path) -> None:
    a, b, pb, pa, mid, res = _api_setup(tmp_path)
    with TestClient(create_app(runtime=a)) as client:
        assert client.get("/agents").status_code == 200
        assert client.get("/agent-messages").status_code == 200
        assert client.get("/peers").status_code == 200
        assert client.get("/agent-transport/status").status_code == 200
        assert client.get("/missions").status_code == 200
