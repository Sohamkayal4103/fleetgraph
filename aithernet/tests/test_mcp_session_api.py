"""Stage 11B: managed MCP session through the API, persistence linkage, and shutdown.

Uses the persistent fake stdio MCP server via the app lifespan (autostart). No gr-mcp,
GNU Radio, or network is required.
"""

from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.settings import (
    CoordinatorConfig,
    MCPServerConfig,
    MCPSessionConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import CoordinatorDecision, CoordinatorInput
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mcp(server, **session) -> MCPRuntime:
    return MCPRuntime.from_config(
        MCPServerConfig(
            command=sys.executable,
            args=[str(server)],
            session=MCPSessionConfig(autostart=True, **session),
        )
    )


def test_session_autostarts_and_status_endpoint(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)) as client:
        body = client.get("/mcp/session").json()
        assert body["state"] == "ready"
        assert body["generation"] >= 1
        assert body["tool_count"] == 7
        assert body["pid"] is not None
        pid = body["pid"]
    # Clean shutdown leaves no orphan process.
    assert not _pid_alive(pid)


def test_tool_calls_reuse_one_process(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)) as client:
        before = client.get("/mcp/session").json()
        # Several tool calls + tool lists.
        assert client.get("/mcp/tools").status_code == 200
        c1 = client.post("/mcp/tools/echo/call", json={"arguments": {"v": 1}}).json()
        c2 = client.post("/mcp/tools/echo/call", json={"arguments": {"v": 2}}).json()
        assert client.get("/mcp/tools").status_code == 200
        after = client.get("/mcp/session").json()
        # Same session, same generation, same process.
        assert before["session_id"] == after["session_id"]
        assert before["generation"] == after["generation"]
        assert before["pid"] == after["pid"]
        # Persisted calls record the session/generation; manual calls have null mission ids.
        assert c1["session_id"] == before["session_id"]
        assert c1["session_generation"] == before["generation"]
        assert c1["mission_id"] is None and c1["mission_step_id"] is None
        assert c1["status"] == "completed" and c2["status"] == "completed"


def test_dashboard_polling_does_not_spawn_processes(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)) as client:
        pids = {client.get("/mcp/session").json()["pid"] for _ in range(8)}
        assert len(pids) == 1  # polling reuses the one managed process


def test_restart_endpoint_increments_generation(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)) as client:
        before = client.get("/mcp/session").json()
        restarted = client.post("/mcp/session/restart").json()
        assert restarted["state"] == "ready"
        assert restarted["generation"] == before["generation"] + 1
        assert restarted["pid"] != before["pid"]
        assert not _pid_alive(before["pid"])  # old process gone


def test_stop_and_start_endpoints(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)) as client:
        stopped = client.post("/mcp/session/stop").json()
        assert stopped["state"] == "stopped"
        started = client.post("/mcp/session/start").json()
        assert started["state"] == "ready"


def test_session_status_no_secret_leak(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    mcp = MCPRuntime.from_config(
        MCPServerConfig(
            command=sys.executable,
            args=[str(server)],
            env={"GR_SECRET": "sk-super-secret-session-value"},
            session=MCPSessionConfig(autostart=True),
        )
    )
    runtime = NodeRuntime.from_config(config, mcp=mcp)
    with TestClient(create_app(runtime=runtime)) as client:
        text = client.get("/mcp/session").text
        assert "sk-super-secret-session-value" not in text
        assert "env" not in client.get("/mcp/session").json()


# -- mission-routed linkage (scripted coordinator + persistent server) -----------


class _ScriptedCoordinator(CoordinatorProvider):
    """Returns a fixed gnuradio_mcp decision so a mission step routes to the live tool."""

    name = "scripted"

    def __init__(self, tool_name: str) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self._tool = tool_name

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json(
            {
                "summary": "Inspect the radio.",
                "next_target": "gnuradio_mcp",
                "action": "inspect",
                "message": "Calling a GNU Radio tool.",
                "structured_payload": {"tool_name": self._tool, "arguments": {}},
                "expected_result": "Tool result recorded.",
            },
            mission_id=payload.mission_id,
        )


def test_mission_routed_call_links_mission_and_step(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(
        config,
        coordinator=CoordinatorRuntime(
            CoordinatorConfig(provider="scripted"), _ScriptedCoordinator("get_blocks")
        ),
        mcp=_mcp(server),
    )
    with TestClient(create_app(runtime=runtime)) as client:
        mission = client.post("/missions", json={"content": "inspect radio"}).json()
        step = client.post(f"/missions/{mission['id']}/step")
        assert step.status_code == 200, step.text
        body = step.json()
        assert body["step"]["route_target"] == "gnuradio_mcp"
        assert body["step"]["status"] == "completed"

        # The routed MCP call is auto-linked to the mission + step (coordinator gave no ids).
        calls = client.get("/mcp/tool-calls", params={"mission_id": mission["id"]}).json()
        routed = [c for c in calls if c["tool_name"] == "get_blocks"]
        assert routed, "expected a mission-routed get_blocks call"
        call = routed[0]
        assert call["mission_id"] == mission["id"]
        assert call["mission_step_id"] == body["step"]["id"]
        assert call["session_id"] is not None
        assert call["session_generation"] >= 1
        assert call["status"] == "completed"
