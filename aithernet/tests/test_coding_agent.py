"""Coding-agent runtime, API, and CLI tests.

None of these tests require Claude Code installed: deterministic, subprocess-free provider
doubles are injected via fixtures (see ``conftest``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.api.app import create_app
from aithernet.cli import app as cli_app
from aithernet.coding_agent.contracts import (
    ACTIVE_CODING_CAPABILITIES,
    CodingTaskExecutionInput,
)
from aithernet.coding_agent.runtime import CodingAgentRuntime
from aithernet.config.settings import CodingAgentConfig
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.coding import CodingTaskRead
from aithernet.state.models import utcnow

runner = CliRunner()


# -- execution-input construction ------------------------------------------------


def test_execution_input_built_from_task() -> None:
    task = CodingTaskRead(
        id="t-1",
        mission_id="m-1",
        objective="Add a tone generator block",
        context_json={"band": "433MHz"},
        available_tools_json=["filesystem"],
        expected_outputs_json=["flowgraph.grc"],
        reporting_requirements_json=["list files changed"],
        status="created",
        provider="claude_code",
        created_at=utcnow(),
        updated_at=utcnow(),
        started_at=None,
        completed_at=None,
    )

    payload = CodingTaskExecutionInput.from_task(task, workspace="/tmp/ws")

    assert payload.task_id == "t-1"
    assert payload.mission_id == "m-1"
    assert payload.objective == "Add a tone generator block"
    assert payload.context == {"band": "433MHz"}
    assert payload.available_tools == ["filesystem"]
    assert payload.expected_outputs == ["flowgraph.grc"]
    assert payload.reporting_requirements == ["list files changed"]
    assert payload.workspace == "/tmp/ws"


# -- runtime configuration -------------------------------------------------------


def test_unconfigured_coding_agent_not_ready() -> None:
    """A missing executable means the agent reports not-configured (no fabrication)."""
    runtime = CodingAgentRuntime.from_config(
        CodingAgentConfig(executable="definitely-not-a-real-binary-xyz")
    )
    assert runtime.is_configured() is False
    status = runtime.status()
    assert status.configured is False
    assert "executable" in status.missing_configuration


def test_unknown_provider_reports_unconfigured() -> None:
    runtime = CodingAgentRuntime.from_config(CodingAgentConfig(provider="does-not-exist"))
    status = runtime.status()
    assert status.configured is False
    assert status.provider == "does-not-exist"
    assert any("unknown" in item for item in status.missing_configuration)


# -- task creation and persistence -----------------------------------------------


def test_create_coding_task_persists(coding_agent_client: TestClient) -> None:
    response = coding_agent_client.post(
        "/coding-tasks",
        json={
            "objective": "Implement an FM demodulator",
            "expected_outputs": ["demod.py"],
            "reporting_requirements": ["summary"],
        },
    )
    assert response.status_code == 201
    task = response.json()
    assert task["id"]
    assert task["objective"] == "Implement an FM demodulator"
    assert task["status"] == "created"
    assert task["expected_outputs"] == ["demod.py"]
    assert task["provider"] == "stub_coding"

    got = coding_agent_client.get(f"/coding-tasks/{task['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == task["id"]


def test_create_coding_task_emits_created_event(coding_agent_client: TestClient) -> None:
    created = coding_agent_client.post("/coding-tasks", json={"objective": "x"}).json()
    events = coding_agent_client.get("/events").json()
    created_events = [e for e in events if e["event_type"] == "coding_task.created"]
    assert len(created_events) == 1
    assert created_events[0]["payload"]["task_id"] == created["id"]


def test_list_coding_tasks(coding_agent_client: TestClient) -> None:
    a = coding_agent_client.post("/coding-tasks", json={"objective": "a"}).json()
    b = coding_agent_client.post("/coding-tasks", json={"objective": "b"}).json()
    tasks = coding_agent_client.get("/coding-tasks").json()
    assert {t["id"] for t in tasks} == {a["id"], b["id"]}


def test_get_missing_task_returns_404(coding_agent_client: TestClient) -> None:
    assert coding_agent_client.get("/coding-tasks/nope").status_code == 404


def test_create_rejects_empty_objective(coding_agent_client: TestClient) -> None:
    assert coding_agent_client.post("/coding-tasks", json={"objective": ""}).status_code == 422


# -- task execution --------------------------------------------------------------


def test_run_task_persists_result_and_transitions_status(
    coding_agent_client: TestClient,
) -> None:
    created = coding_agent_client.post("/coding-tasks", json={"objective": "build x"}).json()
    assert created["status"] == "created"

    run = coding_agent_client.post(f"/coding-tasks/{created['id']}/run")
    assert run.status_code == 200
    result = run.json()
    assert result["task_id"] == created["id"]
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "build x" in result["summary"]

    after = coding_agent_client.get(f"/coding-tasks/{created['id']}").json()
    assert after["status"] == "completed"
    assert after["started_at"] is not None
    assert after["completed_at"] is not None

    # The result is retrievable via the dedicated endpoint.
    latest = coding_agent_client.get(f"/coding-tasks/{created['id']}/result").json()
    assert latest["id"] == result["id"]


def test_run_emits_started_and_completed_events(coding_agent_client: TestClient) -> None:
    created = coding_agent_client.post("/coding-tasks", json={"objective": "y"}).json()
    coding_agent_client.post(f"/coding-tasks/{created['id']}/run")

    events = coding_agent_client.get("/events").json()
    types = [e["event_type"] for e in events]
    assert "coding_task.started" in types
    assert "coding_task.completed" in types


def test_create_and_run_endpoint(coding_agent_client: TestClient) -> None:
    response = coding_agent_client.post(
        "/coding-tasks/run", json={"objective": "scan and report"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "completed"
    assert body["result"]["status"] == "completed"
    assert body["result"]["task_id"] == body["task"]["id"]


def test_provider_failure_marks_failed_and_returns_502(
    failing_coding_agent_client: TestClient,
) -> None:
    created = failing_coding_agent_client.post("/coding-tasks", json={"objective": "boom"}).json()

    run = failing_coding_agent_client.post(f"/coding-tasks/{created['id']}/run")
    assert run.status_code == 502
    assert "simulated coding-agent failure" in run.json()["detail"]

    events = failing_coding_agent_client.get("/events").json()
    types = [e["event_type"] for e in events]
    assert "coding_task.started" in types
    assert "coding_task.failed" in types

    after = failing_coding_agent_client.get(f"/coding-tasks/{created['id']}").json()
    assert after["status"] == "failed"
    # A failure result was still persisted, capturing the error.
    result = failing_coding_agent_client.get(f"/coding-tasks/{created['id']}/result").json()
    assert result["status"] == "failed"


def test_nonzero_exit_is_failed_result_not_error(
    nonzero_coding_agent_client: TestClient,
) -> None:
    """A process that runs but exits non-zero is a normal failed result (HTTP 200)."""
    created = nonzero_coding_agent_client.post("/coding-tasks", json={"objective": "z"}).json()
    run = nonzero_coding_agent_client.post(f"/coding-tasks/{created['id']}/run")
    assert run.status_code == 200
    result = run.json()
    assert result["status"] == "failed"
    assert result["exit_code"] == 2

    after = nonzero_coding_agent_client.get(f"/coding-tasks/{created['id']}").json()
    assert after["status"] == "failed"


def test_run_missing_task_returns_404(coding_agent_client: TestClient) -> None:
    assert coding_agent_client.post("/coding-tasks/nope/run").status_code == 404


def test_result_404_when_never_run(coding_agent_client: TestClient) -> None:
    created = coding_agent_client.post("/coding-tasks", json={"objective": "later"}).json()
    assert coding_agent_client.get(f"/coding-tasks/{created['id']}/result").status_code == 404


# -- unconfigured coding agent (default app) -------------------------------------


def test_run_unconfigured_agent_returns_503(client: TestClient) -> None:
    """The default app uses a real provider; with no executable it fails clearly."""
    # Force an unmistakably-absent executable so the result is deterministic.
    client.app.state.runtime.coding_agent = CodingAgentRuntime.from_config(
        CodingAgentConfig(executable="definitely-not-a-real-binary-xyz")
    )
    created = client.post("/coding-tasks", json={"objective": "noop"}).json()
    run = client.post(f"/coding-tasks/{created['id']}/run")
    assert run.status_code == 503

    after = client.get(f"/coding-tasks/{created['id']}").json()
    assert after["status"] == "failed"
    types = [e["event_type"] for e in client.get("/events").json()]
    assert "coding_task.failed" in types


# -- status endpoint -------------------------------------------------------------


def test_coding_agent_status_does_not_leak_env(config) -> None:
    """The status endpoint never exposes the subprocess env map or its values."""
    runtime = NodeRuntime.from_config(
        config,
        coding_agent=CodingAgentRuntime.from_config(
            CodingAgentConfig(
                executable="definitely-not-a-real-binary-xyz",
                env={"SECRET_TOKEN": "sk-super-secret-value"},
            )
        ),
    )
    with TestClient(create_app(runtime=runtime)) as client:
        response = client.get("/coding-agent/status")
        assert response.status_code == 200
        body = response.json()
        assert "env" not in body
        assert "sk-super-secret-value" not in response.text
        assert set(body["active_capabilities"]) == set(ACTIVE_CODING_CAPABILITIES)
        assert body["configured"] is False


# -- coordinator capability integration ------------------------------------------


def test_capabilities_include_coding_agent_when_configured(
    coding_agent_runtime: NodeRuntime,
) -> None:
    active, future = coding_agent_runtime.coordinator_capabilities()
    assert "coding_agent" in active
    assert "coding_agent" not in future


def test_capabilities_exclude_coding_agent_when_unconfigured(config) -> None:
    runtime = NodeRuntime.from_config(
        config,
        coding_agent=CodingAgentRuntime.from_config(
            CodingAgentConfig(executable="definitely-not-a-real-binary-xyz")
        ),
    )
    active, future = runtime.coordinator_capabilities()
    assert "coding_agent" not in active
    assert "coding_agent" in future


def test_coordinator_sees_coding_agent_when_configured(full_agent_client: TestClient) -> None:
    """End-to-end: a configured coding agent surfaces in the coordinator's input."""
    mission = full_agent_client.post("/missions", json={"content": "do work"}).json()
    decision = full_agent_client.post(f"/missions/{mission['id']}/process").json()
    # The stub coordinator echoes the capabilities it was given into structured_payload.
    assert "coding_agent" in decision["structured_payload"]["capabilities"]


# -- CLI wiring (against a real server with the stub provider) --------------------


def test_cli_coding_agent_status(coding_agent_live_server: str) -> None:
    result = runner.invoke(
        cli_app, ["coding-agent", "status", "--url", coding_agent_live_server]
    )
    assert result.exit_code == 0
    assert "stub_coding" in result.stdout
    assert "Configured: yes" in result.stdout


def test_cli_coding_task_submit(coding_agent_live_server: str) -> None:
    result = runner.invoke(
        cli_app,
        [
            "coding-task",
            "submit",
            "Generate a constellation plot",
            "--expected-output",
            "plot.png",
            "--url",
            coding_agent_live_server,
        ],
    )
    assert result.exit_code == 0
    assert "Task:" in result.stdout
    assert "Status:    completed" in result.stdout


def test_cli_coding_task_create_then_run(coding_agent_live_server: str) -> None:
    create = runner.invoke(
        cli_app,
        ["coding-task", "create", "Build a filter", "--url", coding_agent_live_server],
    )
    assert create.exit_code == 0

    list_result = runner.invoke(
        cli_app, ["coding-task", "list", "--url", coding_agent_live_server]
    )
    task_id = list_result.stdout.split()[0]

    run = runner.invoke(
        cli_app, ["coding-task", "run", task_id, "--url", coding_agent_live_server]
    )
    assert run.exit_code == 0
    assert "Status:    completed" in run.stdout
