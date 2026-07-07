"""Part J integration: Gemini coordinator through the runtime, API, and config loader.

A fake ``gemini`` script (answers ``--version`` and prints the observed headless JSON shape)
drives real mission-step state transitions, the API status/diagnostics surfaces, and
``.env``-based provider selection. No real Gemini CLI, network, or Google account is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.loader import load_config
from aithernet.config.settings import CoordinatorConfig, NodeConfig
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.orchestrator.runtime import NodeRuntime

_DECISION = {
    "summary": "Reviewed the node state.",
    "next_target": "respond",
    "action": "acknowledge",
    "message": "Node status summarized.",
    "structured_payload": {},
    "expected_result": "The mission source is informed.",
    "confidence": 0.9,
}


def _fake_gemini(
    path: Path,
    *,
    version: str = "0.46.0-fake",
    response: str | None = None,
    exit_code: int = 0,
    raw_stdout: str | None = None,
) -> Path:
    payload = {
        "session_id": "fake",
        "response": response if response is not None else json.dumps(_DECISION),
        "stats": {
            "models": {"gemini-3-flash-preview": {}},
            "tools": {"totalCalls": 0, "byName": {}},
        },
    }
    out = raw_stdout if raw_stdout is not None else json.dumps(payload)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv[1:]:\n"
        f"    print({version!r}); sys.exit(0)\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.exit({exit_code})\n"
    )
    path.chmod(0o755)
    return path


def _gemini_runtime(config: NodeConfig, fake: Path, tmp_path: Path) -> NodeRuntime:
    coordinator = CoordinatorRuntime.from_config(
        CoordinatorConfig(
            provider="gemini_cli",
            executable=str(fake),
            working_directory=str(tmp_path / "rt"),
        )
    )
    return NodeRuntime.from_config(config, coordinator=coordinator)


# -- API status / diagnostics (Part J #24) ---------------------------------------


def test_api_coordinator_status_for_gemini(config: NodeConfig, tmp_path) -> None:
    fake = _fake_gemini(tmp_path / "gemini")
    runtime = _gemini_runtime(config, fake, tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        body = client.get("/coordinator/status").json()
        assert body["provider"] == "gemini_cli"
        assert body["configured"] is True
        assert body["executable"] == str(fake)
        assert body["cli_version"] == "0.46.0-fake"
        assert body["model"] is None  # default model


def test_api_coordinator_diagnostics_probe(config: NodeConfig, tmp_path) -> None:
    # Probe healthcheck expects a response containing "ok".
    fake = _fake_gemini(tmp_path / "gemini", response=json.dumps({"ok": True}))
    runtime = _gemini_runtime(config, fake, tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        non_probe = client.get("/coordinator/diagnostics").json()
        assert non_probe["provider"] == "gemini_cli"
        assert non_probe["executable_resolves"] is True
        assert non_probe["probe"]["attempted"] is False

        probed = client.get("/coordinator/diagnostics", params={"probe": "true"}).json()
        assert probed["probe"]["attempted"] is True
        assert probed["probe"]["succeeded"] is True
        assert probed["probe"]["tool_calls"] == 0
        assert "gemini-3-flash-preview" in (probed["probe"]["model_reported"] or [])


def test_api_diagnostics_does_not_leak_env(config: NodeConfig, tmp_path) -> None:
    fake = _fake_gemini(tmp_path / "gemini", response=json.dumps({"ok": True}))
    runtime = _gemini_runtime(config, fake, tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        text = client.get("/coordinator/diagnostics", params={"probe": "true"}).text
        # The probe must not echo environment or auth material.
        assert "PATH" not in text or "/usr/bin" not in text
        assert "HOME" not in text


# -- mission-step routing on success (Part J #22) --------------------------------


def test_successful_decision_routes_through_router(config: NodeConfig, tmp_path) -> None:
    fake = _fake_gemini(tmp_path / "gemini")  # returns next_target "respond"
    runtime = _gemini_runtime(config, fake, tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        mission = client.post("/missions", json={"content": "Summarize node"}).json()
        response = client.post(f"/missions/{mission['id']}/step")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"]["next_target"] == "respond"
        assert body["step"]["route_target"] == "respond"
        assert body["step"]["status"] == "completed"

        # The persisted coordinator.decision event must not contain the raw prompt.
        events = client.get("/events").json()
        decision_events = [e for e in events if e["event_type"] == "coordinator.decision"]
        assert decision_events
        dumped = json.dumps(decision_events[0])
        assert "Coordinator Agent" not in dumped  # no system prompt leaked
        assert "do NOT call" not in dumped


# -- mission-step failure on provider error (Part J #21) -------------------------


def test_mission_step_fails_on_gemini_error(config: NodeConfig, tmp_path) -> None:
    # A non-zero exit with no JSON -> CoordinatorProviderError -> 502.
    fake = _fake_gemini(tmp_path / "gemini", raw_stdout="", exit_code=9)
    runtime = _gemini_runtime(config, fake, tmp_path)
    with TestClient(create_app(runtime=runtime)) as client:
        mission = client.post("/missions", json={"content": "do x"}).json()
        response = client.post(f"/missions/{mission['id']}/step")
        assert response.status_code == 502

        after = client.get(f"/missions/{mission['id']}").json()
        assert after["status"] == "failed"  # never stuck "running"

        events = client.get("/events").json()
        types = [e["event_type"] for e in events]
        assert "coordinator.failed" in types
        assert "mission_step.failed" in types
        # The failure event identifies the provider.
        failed = next(e for e in events if e["event_type"] == "coordinator.failed")
        assert failed["payload"]["provider"] == "gemini_cli"


# -- .env provider selection (Part J #25) ----------------------------------------


def test_env_selects_gemini_provider(tmp_path) -> None:
    cfg = tmp_path / "node.yaml"
    cfg.write_text(
        "node_id: t\n"
        "node_name: t\n"
        "coordinator:\n"
        "  provider: env:AITHERNET_COORDINATOR_PROVIDER\n"
        "  executable: env:AITHERNET_COORDINATOR_EXECUTABLE\n"
        "  model: env:AITHERNET_COORDINATOR_MODEL\n"
        "  timeout_seconds: env:AITHERNET_COORDINATOR_TIMEOUT_SECONDS\n"
    )
    saved = {k: os.environ.get(k) for k in (
        "AITHERNET_COORDINATOR_PROVIDER",
        "AITHERNET_COORDINATOR_EXECUTABLE",
        "AITHERNET_COORDINATOR_MODEL",
        "AITHERNET_COORDINATOR_TIMEOUT_SECONDS",
    )}
    os.environ["AITHERNET_COORDINATOR_PROVIDER"] = "gemini_cli"
    os.environ["AITHERNET_COORDINATOR_EXECUTABLE"] = "gemini"
    os.environ["AITHERNET_COORDINATOR_MODEL"] = ""  # blank -> default model
    os.environ["AITHERNET_COORDINATOR_TIMEOUT_SECONDS"] = "300"
    try:
        loaded = load_config(cfg, env_file=tmp_path / "absent.env")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert loaded.coordinator.provider == "gemini_cli"
    assert loaded.coordinator.executable == "gemini"
    assert loaded.coordinator.model is None  # blank -> unset (provider default)
    assert loaded.coordinator.timeout_seconds == 300.0
