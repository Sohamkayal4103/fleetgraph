"""External-agent connection interface tests (Stage 7).

No test makes a real outbound network call: external agents are message/mission *sources*,
and the node never calls ``endpoint_url``. Mission/step execution uses the injected
network-free coordinator/coding-agent stubs (see ``conftest``). A live in-process server is
used only for the CLI test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.cli import app as cli_app

runner = CliRunner()


def _connect(client: TestClient, **overrides) -> dict:
    body = {"name": "manager-1", "agent_type": "manager", "transport": "local"}
    body.update(overrides)
    response = client.post("/agents/connect", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# -- connection lifecycle --------------------------------------------------------


def test_agent_connect_is_persisted(client: TestClient) -> None:
    agent = _connect(client, name="user-agent", endpoint_url="https://example.invalid/agent")
    assert agent["status"] == "connected"
    assert agent["name"] == "user-agent"
    assert agent["endpoint_url"] == "https://example.invalid/agent"
    assert agent["last_seen_at"] is not None

    # It is retrievable after connecting.
    fetched = client.get(f"/agents/{agent['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == agent["id"]


def test_agent_status_list_and_show(client: TestClient) -> None:
    a1 = _connect(client, name="a-one")
    a2 = _connect(client, name="a-two")

    listed = client.get("/agents").json()
    ids = {a["id"] for a in listed}
    assert {a1["id"], a2["id"]} <= ids

    status = client.get("/agents/status").json()
    assert status["total_count"] == 2
    assert status["connected_count"] == 2
    assert len(status["agents"]) == 2

    # Disabling one is reflected in the roster counts.
    client.post(f"/agents/{a2['id']}/disable")
    status2 = client.get("/agents/status").json()
    assert status2["total_count"] == 2
    assert status2["connected_count"] == 1


def test_no_secret_is_stored_or_returned(client: TestClient) -> None:
    # An auth_type hint is accepted; no credential field exists to store.
    agent = _connect(client, name="bearer-agent", auth_type="bearer")
    assert agent["auth_type"] == "bearer"
    assert "token" not in agent
    assert "secret" not in agent

    # A raw secret smuggled into metadata is rejected, not silently persisted.
    rejected = client.post(
        "/agents/connect",
        json={"name": "leaky", "metadata": {"token": "sk-super-secret-agent-value"}},
    )
    assert rejected.status_code == 400  # rejected by key name; value never echoed
    assert "sk-super-secret-agent-value" not in rejected.text
    assert "token" in rejected.json()["detail"]
    # And nothing was stored for it.
    assert all(a["name"] != "leaky" for a in client.get("/agents").json())


def test_set_status_transitions(client: TestClient) -> None:
    agent = _connect(client)
    disconnected = client.patch(f"/agents/{agent['id']}/status", json={"status": "disconnected"})
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "disconnected"

    reconnected = client.patch(f"/agents/{agent['id']}/status", json={"status": "connected"})
    assert reconnected.json()["status"] == "connected"

    bad = client.patch(f"/agents/{agent['id']}/status", json={"status": "bogus"})
    assert bad.status_code == 422  # not a valid status value


# -- messaging -------------------------------------------------------------------


def test_inbound_message_is_persisted(client: TestClient) -> None:
    agent = _connect(client)
    response = client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "scan 2.4GHz", "create_mission": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"]["direction"] == "inbound"
    assert body["message"]["content"] == "scan 2.4GHz"
    assert body["mission"] is None

    # Both the inbound message and the outbound reply are persisted.
    messages = client.get(f"/agents/{agent['id']}/messages").json()
    directions = sorted(m["direction"] for m in messages)
    assert directions == ["inbound", "outbound"]
    reply = next(m for m in messages if m["direction"] == "outbound")
    assert "recorded" in reply["content"].lower()


def test_mission_request_creates_mission_with_external_source(client: TestClient) -> None:
    agent = _connect(client)
    response = client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "demodulate FM", "payload": {"freq": 100.1}},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Response includes the created mission (test scenario 6).
    assert body["mission"] is not None
    mission_id = body["mission"]["id"]

    mission = client.get(f"/missions/{mission_id}").json()
    assert mission["source_type"] == "external_agent"
    assert mission["source_id"] == agent["id"]
    assert mission["content"] == "demodulate FM"

    # The inbound message is linked to the mission.
    msg = client.get("/agent-messages", params={"mission_id": mission_id}).json()
    assert any(m["direction"] == "inbound" and m["mission_id"] == mission_id for m in msg)


def test_run_step_runs_exactly_one_step(full_agent_client: TestClient) -> None:
    agent = _connect(full_agent_client)
    response = full_agent_client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "acknowledge me", "create_mission": True, "run_step": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mission"] is not None
    assert body["step_response"] is not None
    step = body["step_response"]["step"]
    assert step["status"] == "completed"
    assert step["route_target"] == "respond"

    # Exactly one step was run for that mission.
    steps = full_agent_client.get(
        "/mission-steps", params={"mission_id": body["mission"]["id"]}
    ).json()
    assert len(steps) == 1


def test_run_step_without_create_mission_is_400(client: TestClient) -> None:
    agent = _connect(client)
    response = client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "go", "create_mission": False, "run_step": True},
    )
    assert response.status_code == 400
    assert "create_mission" in response.json()["detail"]


def test_run_step_without_coordinator_is_503(client: TestClient) -> None:
    # The default client has no configured coordinator.
    agent = _connect(client)
    response = client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "go", "create_mission": True, "run_step": True},
    )
    assert response.status_code == 503


def test_message_to_missing_agent_is_404(client: TestClient) -> None:
    response = client.post(
        "/agents/does-not-exist/message", json={"content": "hello"}
    )
    assert response.status_code == 404


def test_disabled_agent_cannot_send_messages(client: TestClient) -> None:
    agent = _connect(client)
    client.post(f"/agents/{agent['id']}/disable")
    response = client.post(f"/agents/{agent['id']}/message", json={"content": "hi"})
    assert response.status_code == 400
    assert "disabled" in response.json()["detail"].lower()


def test_events_are_emitted(full_agent_client: TestClient) -> None:
    agent = _connect(full_agent_client)
    full_agent_client.post(
        f"/agents/{agent['id']}/message",
        json={"content": "do it", "create_mission": True, "run_step": True},
    )
    events = full_agent_client.get("/events").json()
    types = {e["event_type"] for e in events}
    assert "external_agent.connected" in types
    assert "external_agent.message_received" in types
    assert "external_agent.mission_created" in types
    assert "external_agent.step_ran" in types
    assert "external_agent.message_replied" in types


# -- CLI -------------------------------------------------------------------------


def test_cli_agents_connect_list_message(live_server: str) -> None:
    connect = runner.invoke(
        cli_app,
        ["agents", "connect", "--name", "cli-agent", "--type", "manager", "--url", live_server],
    )
    assert connect.exit_code == 0, connect.output
    assert "cli-agent" in connect.output
    agent_id = next(
        line.split()[-1] for line in connect.output.splitlines() if line.startswith("Agent:")
    )

    listed = runner.invoke(cli_app, ["agents", "list", "--url", live_server])
    assert listed.exit_code == 0
    assert agent_id in listed.output

    message = runner.invoke(
        cli_app,
        ["agents", "message", agent_id, "scan the band", "--url", live_server],
    )
    assert message.exit_code == 0, message.output
    assert "Message:" in message.output
    assert "Mission:" in message.output
