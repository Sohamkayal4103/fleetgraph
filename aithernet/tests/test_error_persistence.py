"""Part H: failure state-transition integrity.

A failed coordinator/MCP/coding operation must (a) persist the original sanitized error,
(b) emit a failure event, (c) never remain stuck in ``running``, and (d) never be reported
as completed. These tests inject failing doubles via the existing fixtures and also force
an *unexpected* (non-domain) exception to prove the runtime still records a terminal state.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.settings import MCPServerConfig, NodeConfig
from aithernet.mcp.contracts import MCPClientError, MCPToolCallResult, MCPToolInfo
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def test_failed_mcp_call_persists_error_and_is_terminal(failing_mcp_client: TestClient) -> None:
    response = failing_mcp_client.post("/mcp/tools/echo/call", json={"arguments": {}})
    assert response.status_code == 502  # structured transport failure
    assert "simulated MCP failure" in response.json()["detail"]

    call = failing_mcp_client.get("/mcp/tool-calls").json()[0]
    assert call["status"] == "failed"  # terminal, not "running"
    assert "simulated MCP failure" in (call["error"] or "")

    types = [e["event_type"] for e in failing_mcp_client.get("/events").json()]
    assert "mcp.tool_call.failed" in types
    assert "mcp.tool_call.completed" not in types


def test_failed_coding_task_is_terminal(failing_coding_agent_client: TestClient) -> None:
    created = failing_coding_agent_client.post(
        "/coding-tasks", json={"objective": "boom"}
    ).json()
    run = failing_coding_agent_client.post(f"/coding-tasks/{created['id']}/run")
    assert run.status_code == 502

    after = failing_coding_agent_client.get(f"/coding-tasks/{created['id']}").json()
    assert after["status"] == "failed"  # never stuck "running", never "completed"
    assert after["completed_at"] is not None


class _ExplodingMCPClient:
    """A client whose call_tool raises an UNEXPECTED (non-MCPError) exception."""

    name = "exploding_mcp"

    def check_ready(self) -> list[str]:
        return []

    async def list_tools(self) -> list[MCPToolInfo]:
        return [MCPToolInfo(name="echo", description="x", input_schema={})]

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        raise RuntimeError("unexpected non-domain explosion")


def test_unexpected_mcp_exception_still_marks_call_failed(config: NodeConfig) -> None:
    mcp = MCPRuntime(
        MCPServerConfig(provider="exploding_mcp", command="stub"), _ExplodingMCPClient()
    )
    runtime = NodeRuntime.from_config(config, mcp=mcp)
    with TestClient(create_app(runtime=runtime)) as client:
        # An unexpected (non-domain) exception is re-raised by the API (TestClient
        # propagates it). The point is that the persisted call is already terminal.
        with pytest.raises(RuntimeError, match="explosion"):
            client.post("/mcp/tools/echo/call", json={"arguments": {}})
        calls = client.get("/mcp/tool-calls").json()
        assert len(calls) == 1
        assert calls[0]["status"] == "failed"  # never stuck "running"
        assert "explosion" in (calls[0]["error"] or "")


def test_sanity_mcperror_is_a_real_exception() -> None:
    # Guard: MCPClientError is the domain error the oversized-frame path raises.
    assert issubclass(MCPClientError, Exception)
