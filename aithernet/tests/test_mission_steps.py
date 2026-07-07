"""Mission-step (coordinator decision routing) tests.

No test requires a live model/API, Claude Code, gr-mcp, or GNU Radio. A scripted
coordinator returns a fixed decision, and stub coding-agent/MCP runtimes are injected via
the ``make_step_client`` factory (see ``conftest``).
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.cli import app as cli_app
from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.orchestrator.decision_router import (
    ROUTE_CODING_AGENT,
    ROUTE_MCP,
    ROUTE_RESPOND,
    ROUTE_UNKNOWN,
    RouteValidationError,
    coding_task_payload_from_decision,
    mcp_call_payload_from_decision,
    normalize_target,
)

runner = CliRunner()


def _decision(next_target: str, **extra) -> dict:
    """Build the model-owned decision fields for a scripted coordinator."""
    fields = {
        "summary": "Considered the mission.",
        "next_target": next_target,
        "action": "act",
        "message": "Acknowledged the mission.",
        "expected_result": "A routed step is executed.",
    }
    fields.update(extra)
    return fields


def _create_mission(client: TestClient, content: str = "do something") -> str:
    return client.post("/missions", json={"content": content}).json()["id"]


# -- pure router unit tests ------------------------------------------------------


def test_normalize_target_aliases() -> None:
    assert normalize_target("respond") == ROUTE_RESPOND
    assert normalize_target("user") == ROUTE_RESPOND
    assert normalize_target("mission_source") == ROUTE_RESPOND
    assert normalize_target("coding_agent") == ROUTE_CODING_AGENT
    assert normalize_target("future_coding_agent") == ROUTE_CODING_AGENT
    assert normalize_target("gnuradio_mcp") == ROUTE_MCP
    assert normalize_target("MCP") == ROUTE_MCP
    assert normalize_target("future_gnuradio_mcp") == ROUTE_MCP
    assert normalize_target("node_state") == "node_state"
    assert normalize_target("status") == "node_state"
    assert normalize_target("continue_reasoning") == "continue_reasoning"
    assert normalize_target("peer_agent") == ROUTE_UNKNOWN
    assert normalize_target(None) == ROUTE_UNKNOWN


def test_coding_task_payload_falls_back_to_message() -> None:
    decision = CoordinatorDecision.from_model_json(
        _decision("coding_agent", message="implement FM demod", structured_payload={}),
        mission_id="m-1",
    )
    payload = coding_task_payload_from_decision(decision, mission_id="m-1")
    assert payload.objective == "implement FM demod"
    assert payload.mission_id == "m-1"


def test_mcp_payload_requires_tool_name() -> None:
    decision = CoordinatorDecision.from_model_json(
        _decision("gnuradio_mcp", structured_payload={}), mission_id="m-1"
    )
    try:
        mcp_call_payload_from_decision(decision, mission_id="m-1")
    except RouteValidationError as exc:
        assert "tool_name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RouteValidationError")


# -- respond route ---------------------------------------------------------------


def test_respond_route_completes(make_step_client) -> None:
    client = make_step_client(_decision("respond", message="Here is your answer."))
    mission_id = _create_mission(client)

    response = client.post(f"/missions/{mission_id}/step")
    assert response.status_code == 200
    body = response.json()
    assert body["mission"]["id"] == mission_id
    assert body["decision"]["next_target"] == "respond"

    step = body["step"]
    assert step["route_target"] == "respond"
    assert step["status"] == "completed"
    assert step["result"]["type"] == "response"
    assert step["result"]["message"] == "Here is your answer."


def test_respond_alias_user(make_step_client) -> None:
    client = make_step_client(_decision("user"))
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["route_target"] == "respond"
    assert step["status"] == "completed"


# -- node_state route ------------------------------------------------------------


def test_node_state_route_returns_summary(make_step_client) -> None:
    client = make_step_client(_decision("node_state"))
    mission_id = _create_mission(client)

    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["status"] == "completed"
    result = step["result"]
    assert result["type"] == "node_state"
    assert "node_status" in result
    assert "coordinator_status" in result
    assert "coding_agent_status" in result
    assert "mcp_status" in result
    assert isinstance(result["recent_events"], list)


# -- coding_agent route ----------------------------------------------------------


def test_coding_agent_route_creates_and_runs_task(make_step_client) -> None:
    client = make_step_client(
        _decision(
            "coding_agent",
            structured_payload={
                "objective": "Build a low-pass filter block",
                "expected_outputs": ["filter.py"],
            },
        ),
        coding_agent=True,
    )
    mission_id = _create_mission(client)

    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["route_target"] == "coding_agent"
    assert step["status"] == "completed"
    result = step["result"]
    assert result["type"] == "coding_task"
    assert result["result_status"] == "completed"

    # A coding task was really created and linked to the mission.
    tasks = client.get("/coding-tasks").json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == result["task_id"]
    assert tasks[0]["objective"] == "Build a low-pass filter block"
    assert tasks[0]["mission_id"] == mission_id


def test_coding_agent_alias_future(make_step_client) -> None:
    client = make_step_client(
        _decision("future_coding_agent", structured_payload={"objective": "x"}),
        coding_agent=True,
    )
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["route_target"] == "coding_agent"
    assert step["status"] == "completed"


def test_coding_agent_route_blocked_when_unconfigured(make_step_client) -> None:
    client = make_step_client(
        _decision("coding_agent", structured_payload={"objective": "x"}),
        coding_agent=False,
    )
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["status"] == "blocked"
    assert "coding agent" in step["error"].lower()
    # No coding task created when blocked.
    assert client.get("/coding-tasks").json() == []


# -- gnuradio_mcp route ----------------------------------------------------------


def test_mcp_route_calls_tool(make_step_client) -> None:
    client = make_step_client(
        _decision(
            "gnuradio_mcp",
            structured_payload={"tool_name": "echo", "arguments": {"freq": 433}},
        ),
        mcp=True,
    )
    mission_id = _create_mission(client)

    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["route_target"] == "gnuradio_mcp"
    assert step["status"] == "completed"
    result = step["result"]
    assert result["type"] == "mcp_tool_call"
    assert result["tool_name"] == "echo"
    assert result["status"] == "completed"

    # A persisted MCP tool call exists, linked to the mission, with caller=coordinator.
    calls = client.get("/mcp/tool-calls").json()
    assert len(calls) == 1
    assert calls[0]["id"] == result["call_id"]
    assert calls[0]["caller"] == "coordinator"
    assert calls[0]["mission_id"] == mission_id


def test_mcp_route_blocked_when_unconfigured(make_step_client) -> None:
    client = make_step_client(
        _decision("gnuradio_mcp", structured_payload={"tool_name": "echo"}),
        mcp=False,
    )
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["status"] == "blocked"
    assert "mcp" in step["error"].lower()


def test_mcp_route_fails_when_tool_name_missing(make_step_client) -> None:
    client = make_step_client(
        _decision("gnuradio_mcp", structured_payload={}),  # no tool_name
        mcp=True,
    )
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    # Capability available, payload malformed -> failed (not blocked), with clear reason.
    assert step["status"] == "failed"
    assert "tool_name" in step["error"]


# -- blocked / unknown routes ----------------------------------------------------


def test_continue_reasoning_blocked(make_step_client) -> None:
    client = make_step_client(_decision("continue_reasoning"))
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["status"] == "blocked"
    assert "autonomous loops are not enabled" in step["error"].lower()


def test_unknown_target_blocked(make_step_client) -> None:
    client = make_step_client(_decision("peer_agent"))
    mission_id = _create_mission(client)
    step = client.post(f"/missions/{mission_id}/step").json()["step"]
    assert step["status"] == "blocked"
    assert "unsupported route target" in step["error"].lower()


# -- events, querying, and missing mission ---------------------------------------


def test_mission_step_emits_events(make_step_client) -> None:
    client = make_step_client(_decision("respond"))
    mission_id = _create_mission(client)
    client.post(f"/missions/{mission_id}/step")

    events = client.get("/events", params={"mission_id": mission_id}).json()
    types = [e["event_type"] for e in events]
    assert "mission_step.started" in types
    assert "mission_step.completed" in types
    # The coordinator events are reused, not duplicated.
    assert "coordinator.decision" in types


def test_mission_steps_listing_and_get(make_step_client) -> None:
    client = make_step_client(_decision("respond"))
    mission_id = _create_mission(client)
    step_id = client.post(f"/missions/{mission_id}/step").json()["step"]["id"]

    listed = client.get("/mission-steps", params={"mission_id": mission_id}).json()
    assert [s["id"] for s in listed] == [step_id]
    assert client.get(f"/mission-steps/{step_id}").json()["id"] == step_id
    assert client.get("/mission-steps/nope").status_code == 404


def test_step_missing_mission_returns_404(make_step_client) -> None:
    client = make_step_client(_decision("respond"))
    assert client.post("/missions/nope/step").status_code == 404


# -- CLI (against a live server with injected stub coordinator) ------------------


def test_cli_mission_step(step_live_server: str) -> None:
    mission_id = httpx.post(
        f"{step_live_server}/missions", json={"content": "scan band"}, timeout=10
    ).json()["id"]

    result = runner.invoke(cli_app, ["mission", "step", mission_id, "--url", step_live_server])
    assert result.exit_code == 0
    assert "Target:   respond" in result.stdout
    assert "Status:   completed" in result.stdout


def test_cli_mission_steps_and_show(step_live_server: str) -> None:
    mission_id = httpx.post(
        f"{step_live_server}/missions", json={"content": "scan band"}, timeout=10
    ).json()["id"]
    runner.invoke(cli_app, ["mission", "step", mission_id, "--url", step_live_server])

    listed = runner.invoke(cli_app, ["mission", "steps", "--url", step_live_server])
    assert listed.exit_code == 0
    step_id = listed.stdout.split()[0]

    shown = runner.invoke(cli_app, ["mission", "step-show", step_id, "--url", step_live_server])
    assert shown.exit_code == 0
    assert step_id in shown.stdout
