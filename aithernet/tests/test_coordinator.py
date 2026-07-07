"""Coordinator runtime, API, and CLI tests.

None of these tests require an external API key: a deterministic, network-free provider
is injected via fixtures (see ``conftest``).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.cli import app as cli_app
from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import (
    ACTIVE_CAPABILITIES,
    CoordinatorConfigurationError,
    CoordinatorInput,
)
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.schemas.events import EventRead
from aithernet.schemas.missions import MissionRead, MissionStatus
from aithernet.schemas.node import NodeStatus
from aithernet.state.models import utcnow

runner = CliRunner()


# -- input construction ----------------------------------------------------------


def test_coordinator_input_built_from_mission() -> None:
    mission = MissionRead(
        id="m-1",
        source_type="user",
        source_id="op-7",
        content="Survey the 2.4 GHz band",
        status=MissionStatus.RECEIVED,
        created_at=utcnow(),
        updated_at=utcnow(),
        metadata_json={},
    )
    node_status = NodeStatus(
        node_id="n-1",
        node_name="node",
        runtime_status="running",
        database_path="/tmp/x.db",
        started_at=utcnow(),
        version="0.1.0",
        mission_count=1,
        event_count=2,
    )
    events = [
        EventRead(
            id="e-1",
            mission_id="m-1",
            event_type="mission.received",
            source="runtime",
            message="received",
            payload_json={},
            created_at=utcnow(),
        )
    ]

    payload = CoordinatorInput.from_node_context(
        mission=mission, node_status=node_status, recent_events=events
    )

    assert payload.mission_id == "m-1"
    assert payload.mission_content == "Survey the 2.4 GHz band"
    assert payload.mission_source_type == "user"
    assert payload.mission_source_id == "op-7"
    assert payload.mission_status == "received"
    assert payload.node_status_summary["mission_count"] == 1
    assert payload.recent_events[0]["event_type"] == "mission.received"
    assert list(ACTIVE_CAPABILITIES) == payload.available_capabilities
    # Future capabilities are advertised but kept distinct from active ones.
    assert "coding_agent" in payload.future_capabilities
    assert "coding_agent" not in payload.available_capabilities


# -- runtime configuration -------------------------------------------------------


def test_unconfigured_coordinator_raises_clean_error() -> None:
    """An unconfigured coordinator fails clearly instead of fabricating a decision."""
    runtime = CoordinatorRuntime.from_config(CoordinatorConfig())  # no model/base_url/key
    assert runtime.is_configured() is False

    payload = CoordinatorInput(
        mission_id="m",
        mission_content="x",
        mission_source_type="user",
        mission_status="received",
        current_time=utcnow(),
    )
    try:
        asyncio.run(runtime.decide(payload))
    except CoordinatorConfigurationError:
        pass
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected CoordinatorConfigurationError")


def test_unknown_provider_reports_unconfigured() -> None:
    runtime = CoordinatorRuntime.from_config(CoordinatorConfig(provider="does-not-exist"))
    status = runtime.status()
    assert status.configured is False
    assert status.provider == "does-not-exist"
    assert any("unknown" in item for item in status.missing_configuration)


# -- end-to-end processing via the runtime ---------------------------------------


def test_process_persists_started_and_decision_events(coordinator_client: TestClient) -> None:
    created = coordinator_client.post("/missions", json={"content": "Scan 868 MHz"}).json()

    decision = coordinator_client.post(f"/missions/{created['id']}/process").json()
    assert decision["mission_id"] == created["id"]
    assert decision["next_target"] == "respond"

    events = coordinator_client.get("/events", params={"mission_id": created["id"]}).json()
    types = [event["event_type"] for event in events]
    assert "coordinator.processing_started" in types
    assert "coordinator.decision" in types

    decision_event = next(e for e in events if e["event_type"] == "coordinator.decision")
    assert decision_event["payload"]["decision_id"] == decision["decision_id"]


def test_status_transitions_received_to_active(coordinator_client: TestClient) -> None:
    created = coordinator_client.post("/missions", json={"content": "Listen 915 MHz"}).json()
    assert created["status"] == "received"

    coordinator_client.post(f"/missions/{created['id']}/process")

    after = coordinator_client.get(f"/missions/{created['id']}").json()
    assert after["status"] == "active"


def test_process_endpoint_returns_decision(coordinator_client: TestClient) -> None:
    created = coordinator_client.post("/missions", json={"content": "Decode ADS-B"}).json()
    response = coordinator_client.post(f"/missions/{created['id']}/process")
    assert response.status_code == 200
    body = response.json()
    assert {"decision_id", "mission_id", "summary", "next_target", "action", "message"} <= set(
        body
    )


def test_process_missing_mission_returns_404(coordinator_client: TestClient) -> None:
    response = coordinator_client.post("/missions/nope/process")
    assert response.status_code == 404


def test_provider_failure_records_event_and_clean_error(
    failing_coordinator_client: TestClient,
) -> None:
    created = failing_coordinator_client.post("/missions", json={"content": "boom"}).json()

    response = failing_coordinator_client.post(f"/missions/{created['id']}/process")
    assert response.status_code == 502
    assert "simulated provider failure" in response.json()["detail"]

    events = failing_coordinator_client.get(
        "/events", params={"mission_id": created["id"]}
    ).json()
    types = [event["event_type"] for event in events]
    assert "coordinator.processing_started" in types
    assert "coordinator.failed" in types

    after = failing_coordinator_client.get(f"/missions/{created['id']}").json()
    assert after["status"] == "failed"


# -- coordinator status endpoint -------------------------------------------------


def test_coordinator_status_does_not_leak_secrets(coordinator_client: TestClient) -> None:
    response = coordinator_client.get("/coordinator/status")
    assert response.status_code == 200
    body = response.json()

    assert body["provider"] == "stub"
    assert body["configured"] is True
    assert set(body["active_capabilities"]) == set(ACTIVE_CAPABILITIES)
    # No secret material is present anywhere in the response.
    assert "api_key" not in body
    assert all(key != "api_key" for key in body)
    serialized = response.text.lower()
    assert "secret" not in serialized
    assert "bearer" not in serialized


def test_unconfigured_status_lists_missing_via_api(client: TestClient) -> None:
    """The default app (no coordinator env vars) reports what configuration is missing."""
    body = client.get("/coordinator/status").json()
    assert body["configured"] is False
    assert "api_key_configured" in body and body["api_key_configured"] is False


# -- CLI wiring (against a real server with the stub provider) --------------------


def test_cli_coordinator_status(coordinator_live_server: str) -> None:
    result = runner.invoke(cli_app, ["coordinator", "status", "--url", coordinator_live_server])
    assert result.exit_code == 0
    assert "stub" in result.stdout
    assert "Configured: yes" in result.stdout


def test_cli_mission_process(coordinator_live_server: str) -> None:
    submit = runner.invoke(
        cli_app, ["mission", "submit", "Scan 433 MHz", "--url", coordinator_live_server]
    )
    assert submit.exit_code == 0

    list_result = runner.invoke(
        cli_app, ["mission", "list", "--url", coordinator_live_server]
    )
    mission_id = list_result.stdout.split()[0]

    result = runner.invoke(
        cli_app, ["mission", "process", mission_id, "--url", coordinator_live_server]
    )
    assert result.exit_code == 0
    assert "Next target:" in result.stdout
    assert "respond" in result.stdout


def test_cli_submit_with_process_flag(coordinator_live_server: str) -> None:
    result = runner.invoke(
        cli_app,
        ["mission", "submit", "Triangulate beacon", "--process", "--url", coordinator_live_server],
    )
    assert result.exit_code == 0
    assert "Mission submitted:" in result.stdout
    assert "Decision:" in result.stdout
