"""MCP runtime, stdio client, API, and CLI tests.

No test requires a real gr-mcp install or GNU Radio. Most tests inject a subprocess-free
MCP client double; the stdio-client tests launch a tiny temp MCP server (created under a
pytest temp path) and exercise the real JSON-RPC subprocess I/O.
"""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aithernet.cli import app as cli_app
from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.clients.stdio import StdioMCPClient
from aithernet.mcp.contracts import ACTIVE_MCP_CAPABILITIES
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime

runner = CliRunner()


# A minimal MCP server: newline-delimited JSON-RPC over stdin/stdout. It answers
# initialize, accepts notifications/initialized, lists one `echo` tool, and echoes the
# call arguments back. This lives only in tests (written to a temp path), never in src.
TINY_MCP_SERVER = '''\
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tiny-test-mcp", "version": "0.0.1"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "Echo arguments back",
             "inputSchema": {"type": "object"}},
        ]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments", {})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(args)}],
            "isError": False,
        }})
    elif method == "exit":
        break
'''


def _write_tiny_server(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "main.py"
    script.write_text(TINY_MCP_SERVER)
    return script


def _write_fake_uv(path: Path) -> Path:
    """Write an executable that emulates `uv --directory DIR run main.py`."""
    source = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "directory = args[args.index('--directory') + 1] if '--directory' in args else '.'\n"
        "script = args[args.index('run') + 1] if 'run' in args else 'main.py'\n"
        "os.execv(sys.executable, [sys.executable, os.path.join(directory, script)])\n"
    )
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# -- runtime / status ------------------------------------------------------------


def test_status_unconfigured_without_command() -> None:
    runtime = MCPRuntime.from_config(MCPServerConfig())  # no command
    status = runtime.status()
    assert status.configured is False
    assert "command" in status.missing_configuration
    assert status.command is None


def test_status_unknown_provider() -> None:
    runtime = MCPRuntime.from_config(MCPServerConfig(provider="does-not-exist", command="x"))
    status = runtime.status()
    assert status.configured is False
    assert any("unknown" in item for item in status.missing_configuration)


def test_status_does_not_leak_env(mcp_client: TestClient) -> None:
    # Replace the runtime's MCP with one carrying a secret env value.
    mcp_client.app.state.runtime.mcp = MCPRuntime.from_config(
        MCPServerConfig(
            command="definitely-not-a-real-binary-xyz",
            args=["--token", "x"],
            env={"GR_SECRET": "sk-super-secret-mcp-value"},
        )
    )
    response = mcp_client.get("/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert "env" not in body
    assert "sk-super-secret-mcp-value" not in response.text
    assert set(body["active_capabilities"]) == set(ACTIVE_MCP_CAPABILITIES)


# -- tool listing / calling via injected stub client -----------------------------


def test_list_tools_with_stub(mcp_client: TestClient) -> None:
    response = mcp_client.get("/mcp/tools")
    assert response.status_code == 200
    tools = response.json()
    assert [t["name"] for t in tools] == ["echo"]
    # Listing emits requested + completed audit events.
    types = [e["event_type"] for e in mcp_client.get("/events").json()]
    assert "mcp.tool_list.requested" in types
    assert "mcp.tool_list.completed" in types


def test_call_tool_persists_events_and_result(mcp_client: TestClient) -> None:
    response = mcp_client.post(
        "/mcp/tools/echo/call", json={"caller": "user", "arguments": {"x": 1}}
    )
    assert response.status_code == 200
    call = response.json()
    assert call["tool_name"] == "echo"
    assert call["status"] == "completed"
    assert call["result"]["echo"] == {"x": 1}
    assert call["started_at"] is not None
    assert call["completed_at"] is not None

    # The call is auditable via the tool-calls endpoints.
    assert mcp_client.get(f"/mcp/tool-calls/{call['id']}").json()["id"] == call["id"]
    assert any(c["id"] == call["id"] for c in mcp_client.get("/mcp/tool-calls").json())

    types = [e["event_type"] for e in mcp_client.get("/events").json()]
    assert "mcp.tool_call.created" in types
    assert "mcp.tool_call.started" in types
    assert "mcp.tool_call.completed" in types


def test_call_tool_provider_failure(failing_mcp_client: TestClient) -> None:
    response = failing_mcp_client.post("/mcp/tools/echo/call", json={"arguments": {}})
    assert response.status_code == 502
    assert "simulated MCP failure" in response.json()["detail"]

    # A failed call is still persisted with a failure event.
    calls = failing_mcp_client.get("/mcp/tool-calls").json()
    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    types = [e["event_type"] for e in failing_mcp_client.get("/events").json()]
    assert "mcp.tool_call.failed" in types


def test_list_tools_unconfigured_returns_503(client: TestClient) -> None:
    response = client.get("/mcp/tools")  # default app: MCP unconfigured
    assert response.status_code == 503


def test_call_unconfigured_returns_503(client: TestClient) -> None:
    response = client.post("/mcp/tools/echo/call", json={"arguments": {}})
    assert response.status_code == 503


def test_get_missing_call_returns_404(mcp_client: TestClient) -> None:
    assert mcp_client.get("/mcp/tool-calls/nope").status_code == 404


# -- real stdio client against a tiny temp MCP server ----------------------------


def test_stdio_client_lists_and_calls_real_subprocess(tmp_path) -> None:
    script = _write_tiny_server(tmp_path / "server")
    config = MCPServerConfig(command=sys.executable, args=[str(script)])
    client = StdioMCPClient(config)
    assert client.check_ready() == []

    async def scenario():
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"hello": "world"})
        return tools, result

    tools, result = asyncio.run(scenario())
    assert [t.name for t in tools] == ["echo"]
    assert tools[0].input_schema == {"type": "object"}
    assert result.status == "completed"
    # The server echoes arguments back inside the content block.
    text = result.result["content"][0]["text"]
    assert "world" in text


def test_stdio_client_supports_gr_mcp_command_shape(tmp_path) -> None:
    """Validate the exact gr-mcp shape: uv --directory /path/to/gr-mcp run main.py."""
    server_dir = tmp_path / "gr-mcp"
    _write_tiny_server(server_dir)
    fake_uv = _write_fake_uv(tmp_path / "uv")

    config = MCPServerConfig(
        command=str(fake_uv),
        args=["--directory", str(server_dir), "run", "main.py"],
    )
    client = StdioMCPClient(config)
    assert client.check_ready() == []

    tools = asyncio.run(client.list_tools())
    assert [t.name for t in tools] == ["echo"]


def test_stdio_client_unresolved_command_not_ready() -> None:
    client = StdioMCPClient(MCPServerConfig(command="definitely-not-a-real-binary-xyz"))
    assert "command" in client.check_ready()


def test_stdio_client_unresolved_args_not_ready() -> None:
    client = StdioMCPClient(MCPServerConfig(command=sys.executable, args=["ok", None]))
    assert "args" in client.check_ready()


# -- coordinator capability integration ------------------------------------------


def test_coordinator_capabilities_include_mcp_when_configured(mcp_runtime: NodeRuntime) -> None:
    active, future = mcp_runtime.coordinator_capabilities()
    assert "gnuradio_mcp" in active
    assert "gnuradio_mcp" not in future


def test_coordinator_capabilities_exclude_mcp_when_unconfigured(config) -> None:
    runtime = NodeRuntime.from_config(
        config, mcp=MCPRuntime.from_config(MCPServerConfig())  # no command
    )
    active, future = runtime.coordinator_capabilities()
    assert "gnuradio_mcp" not in active
    assert "gnuradio_mcp" in future


def test_coding_agent_status_reflects_mcp_when_configured(mcp_client: TestClient) -> None:
    body = mcp_client.get("/coding-agent/status").json()
    # MCP is configured (stub), so gnuradio_mcp moves from future to active node capability.
    assert "gnuradio_mcp" in body["active_capabilities"]
    assert "gnuradio_mcp" not in body["future_capabilities"]


def test_coding_agent_status_keeps_mcp_future_when_unconfigured(client: TestClient) -> None:
    body = client.get("/coding-agent/status").json()  # default app: MCP unconfigured
    assert "gnuradio_mcp" in body["future_capabilities"]
    assert "gnuradio_mcp" not in body["active_capabilities"]


# -- CLI wiring (against a real server with the stub MCP client) ------------------


def test_cli_mcp_status(mcp_live_server: str) -> None:
    result = runner.invoke(cli_app, ["mcp", "status", "--url", mcp_live_server])
    assert result.exit_code == 0
    assert "stub_mcp" in result.stdout
    assert "Configured: yes" in result.stdout


def test_cli_mcp_tools(mcp_live_server: str) -> None:
    result = runner.invoke(cli_app, ["mcp", "tools", "--url", mcp_live_server])
    assert result.exit_code == 0
    assert "echo" in result.stdout


def test_cli_mcp_call(mcp_live_server: str) -> None:
    result = runner.invoke(
        cli_app,
        ["mcp", "call", "echo", "--args-json", '{"x": 5}', "--url", mcp_live_server],
    )
    assert result.exit_code == 0
    assert "Status:    completed" in result.stdout
    assert "Tool:      echo" in result.stdout
