"""Shared pytest fixtures.

Every test runs against an isolated, temporary SQLite database so results never
depend on (or pollute) a developer's local node state.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.coding_agent.contracts import (
    CodingAgentProviderError,
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
)
from aithernet.coding_agent.providers.base import CodingAgentProvider
from aithernet.coding_agent.runtime import CodingAgentRuntime
from aithernet.config.settings import (
    CodingAgentConfig,
    CoordinatorConfig,
    MCPServerConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import (
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.mcp.clients.base import MCPClient
from aithernet.mcp.contracts import MCPClientError, MCPToolCallResult, MCPToolInfo
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def _free_port() -> int:
    """Reserve and release an ephemeral local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- tiny temp MCP server (test-only) --------------------------------------------
#
# A minimal newline-delimited JSON-RPC MCP server, written to a pytest temp path. It
# answers initialize, accepts notifications/initialized, lists one `echo` tool, and
# echoes the call arguments back. This is a TEST double only — never production code,
# never a fake GNU Radio. The ``marker_path`` variant touches a file on launch so a test
# can prove a non-probe diagnostic did NOT start the server.

_TINY_MCP_SERVER_BODY = '''\
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
        size = args.get("size")
        if isinstance(size, int) and size > 0:
            # Emit a single JSON-RPC frame whose text block is `size` bytes long, so a
            # test can drive a response larger than asyncio's default 64 KiB line limit.
            text = "x" * size
        else:
            text = json.dumps(args)
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }})
    elif method == "exit":
        break
'''

_TINY_MCP_SERVER_HEADER = '''\
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


'''


def write_tiny_mcp_server(directory: Path, *, marker_path: Path | None = None) -> Path:
    """Write the tiny MCP server to ``directory/main.py`` and return its path.

    If ``marker_path`` is given, the server touches that file at launch (before reading
    any input) so a test can assert whether the server was actually started.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "main.py"
    marker_line = ""
    if marker_path is not None:
        marker_line = f'open(r"{marker_path}", "w").write("launched")\n\n'
    script.write_text(_TINY_MCP_SERVER_HEADER + marker_line + _TINY_MCP_SERVER_BODY)
    return script


@pytest.fixture()
def write_mcp_server():
    """Return the tiny-MCP-server writer (call with a dir and optional ``marker_path``)."""
    return write_tiny_mcp_server


# -- persistent stdio MCP server (test-only, Stage 11B) --------------------------
#
# A long-lived JSON-RPC stdio server with GNU Radio-like read tools (get_blocks,
# get_connections, validate_flowgraph, get_all_errors) plus a mutating make_block and a
# crash tool. Used to exercise the persistent session + GNU Radio context. A TEST double
# only — never a fake GNU Radio, never production code.

_PERSISTENT_MCP_SERVER = r'''
import sys, json, os
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def result(mid, text, is_error=False):
    send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":text}],"isError":is_error}})
TOOLS = [
    {"name":"echo","description":"echo","inputSchema":{"type":"object"}},
    {"name":"crash","description":"crash","inputSchema":{"type":"object"}},
    {"name":"get_blocks","description":"blocks","inputSchema":{"type":"object"}},
    {"name":"get_connections","description":"conns","inputSchema":{"type":"object"}},
    {"name":"validate_flowgraph","description":"validate","inputSchema":{"type":"object"}},
    {"name":"get_all_errors","description":"errors","inputSchema":{"type":"object"}},
    {"name":"make_block","description":"mutate","inputSchema":{"type":"object"}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method"); mid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
            "capabilities":{"tools":{}},"serverInfo":{"name":"fake-gr","version":"1.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name"); args = params.get("arguments") or {}
        if name == "crash":
            os._exit(7)
        elif name == "echo":
            result(mid, json.dumps(args) if "size" not in args else "x" * int(args["size"]))
        elif name == "get_blocks":
            result(mid, json.dumps(args.get("blocks", [])))
        elif name == "get_connections":
            result(mid, json.dumps(args.get("connections", [])))
        elif name == "validate_flowgraph":
            result(mid, json.dumps({"valid": True}))
        elif name == "get_all_errors":
            result(mid, json.dumps([]))
        elif name == "make_block":
            result(mid, json.dumps({"ok": True}))
        elif name == "fail_tool":
            result(mid, "tool failed", is_error=True)
        else:
            result(mid, json.dumps(args))
    elif method == "exit":
        break
'''


def write_persistent_mcp_server(directory: Path, name: str = "server.py") -> Path:
    """Write the persistent GNU Radio-like stdio MCP server and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(_PERSISTENT_MCP_SERVER)
    return script


@pytest.fixture()
def persistent_mcp_server():
    """Return the persistent-MCP-server writer (call with a directory)."""
    return write_persistent_mcp_server


# -- test-only coordinator providers ---------------------------------------------
#
# These doubles live only in the test suite. They produce a real CoordinatorDecision
# derived from the input (no network) so the runtime can be exercised without an API
# key, exactly as the spec requires.


class StubCoordinatorProvider(CoordinatorProvider):
    """A deterministic provider that derives a decision from the mission content."""

    name = "stub"

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json(
            {
                "summary": f"Reviewed mission: {payload.mission_content}",
                "next_target": "respond",
                "action": "acknowledge_mission",
                "message": f"Acknowledged '{payload.mission_content}'.",
                "structured_payload": {"capabilities": payload.available_capabilities},
                "expected_result": "The mission source is informed the node received the task.",
                "confidence": 0.9,
            },
            mission_id=payload.mission_id,
        )


class FailingCoordinatorProvider(CoordinatorProvider):
    """A provider that always fails, to exercise the failure path."""

    name = "failing"

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        raise CoordinatorProviderError("simulated provider failure")


class ScriptedCoordinatorProvider(CoordinatorProvider):
    """Returns a fixed, caller-supplied decision (for mission-step routing tests)."""

    name = "scripted"

    def __init__(self, decision_fields: dict) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self._decision_fields = decision_fields

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        return CoordinatorDecision.from_model_json(
            self._decision_fields, mission_id=payload.mission_id
        )


def _coordinator_runtime(provider: CoordinatorProvider) -> CoordinatorRuntime:
    return CoordinatorRuntime(CoordinatorConfig(provider=provider.name), provider)


# -- test-only coding-agent providers --------------------------------------------
#
# These doubles never spawn a subprocess; they return a real CodingTaskExecutionResult
# (or raise) so the runtime can be exercised without Claude Code installed.


class StubCodingAgentProvider(CodingAgentProvider):
    """A deterministic provider that reports a successful run derived from the task."""

    name = "stub_coding"

    def check_ready(self) -> list[str]:
        return []

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        return CodingTaskExecutionResult(
            status="completed",
            summary=f"Implemented objective: {task.objective}",
            stdout=f"Worked on '{task.objective}' in {task.workspace}.",
            stderr="",
            exit_code=0,
            files_changed=["NOTES.md"],
            commands_run=["echo done"],
            payload={"objective": task.objective},
        )


class FailingCodingAgentProvider(CodingAgentProvider):
    """A provider that always raises, to exercise the infrastructure-failure path."""

    name = "failing_coding"

    def check_ready(self) -> list[str]:
        return []

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        raise CodingAgentProviderError("simulated coding-agent failure")


class NonZeroExitCodingAgentProvider(CodingAgentProvider):
    """A provider whose process runs but exits non-zero (a failed result, not an error)."""

    name = "nonzero_coding"

    def check_ready(self) -> list[str]:
        return []

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        return CodingTaskExecutionResult(
            status="failed",
            summary="Agent exited with code 2.",
            stdout="partial work",
            stderr="boom",
            exit_code=2,
        )


def _coding_agent_runtime(provider: CodingAgentProvider) -> CodingAgentRuntime:
    return CodingAgentRuntime(CodingAgentConfig(provider=provider.name), provider)


# -- test-only MCP clients -------------------------------------------------------
#
# These doubles never spawn a subprocess; they return real MCP contracts (or raise) so
# the runtime can be exercised without gr-mcp or GNU Radio installed.


class StubMCPClient(MCPClient):
    """A deterministic MCP client exposing a single ``echo`` tool."""

    name = "stub_mcp"

    def check_ready(self) -> list[str]:
        return []

    async def list_tools(self) -> list[MCPToolInfo]:
        return [
            MCPToolInfo(
                name="echo",
                description="Echo the arguments back.",
                input_schema={"type": "object"},
            )
        ]

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        return MCPToolCallResult(
            tool_name=tool_name,
            status="completed",
            result={"content": [{"type": "text", "text": "ok"}], "echo": arguments},
        )


class FailingMCPClient(MCPClient):
    """An MCP client that always raises, to exercise the failure path."""

    name = "failing_mcp"

    def check_ready(self) -> list[str]:
        return []

    async def list_tools(self) -> list[MCPToolInfo]:
        raise MCPClientError("simulated MCP failure")

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        raise MCPClientError("simulated MCP failure")


def _mcp_runtime(client: MCPClient) -> MCPRuntime:
    return MCPRuntime(MCPServerConfig(provider=client.name, command="stub"), client)


@pytest.fixture()
def config(tmp_path) -> NodeConfig:
    """A node config pointing at a throwaway database under ``tmp_path``."""
    db_path = tmp_path / "test_aithernet.db"
    return NodeConfig(
        node_id="test-node-id",
        node_name="aithernet-test-node",
        database_url=f"sqlite:///{db_path}",
        host="127.0.0.1",
        port=8080,
        log_level="warning",
    )


@pytest.fixture()
def client(config: NodeConfig) -> Iterator[TestClient]:
    """A FastAPI TestClient bound to a freshly-built app for each test."""
    app = create_app(config)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def coordinator_runtime(config: NodeConfig) -> NodeRuntime:
    """A node runtime wired to a deterministic, network-free coordinator provider."""
    return NodeRuntime.from_config(
        config, coordinator=_coordinator_runtime(StubCoordinatorProvider(CoordinatorConfig()))
    )


@pytest.fixture()
def coordinator_client(coordinator_runtime: NodeRuntime) -> Iterator[TestClient]:
    """TestClient whose coordinator uses the stub provider."""
    app = create_app(runtime=coordinator_runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def failing_coordinator_client(config: NodeConfig) -> Iterator[TestClient]:
    """TestClient whose coordinator always fails, for the failure path."""
    runtime = NodeRuntime.from_config(
        config, coordinator=_coordinator_runtime(FailingCoordinatorProvider(CoordinatorConfig()))
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def coding_agent_runtime(config: NodeConfig) -> NodeRuntime:
    """A node runtime wired to a deterministic, network-free coding-agent provider."""
    return NodeRuntime.from_config(
        config, coding_agent=_coding_agent_runtime(StubCodingAgentProvider(CodingAgentConfig()))
    )


@pytest.fixture()
def coding_agent_client(coding_agent_runtime: NodeRuntime) -> Iterator[TestClient]:
    """TestClient whose coding agent uses the stub provider."""
    app = create_app(runtime=coding_agent_runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def failing_coding_agent_client(config: NodeConfig) -> Iterator[TestClient]:
    """TestClient whose coding agent always raises, for the failure path."""
    runtime = NodeRuntime.from_config(
        config, coding_agent=_coding_agent_runtime(FailingCodingAgentProvider(CodingAgentConfig()))
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def nonzero_coding_agent_client(config: NodeConfig) -> Iterator[TestClient]:
    """TestClient whose coding agent runs but exits non-zero (failed result, HTTP 200)."""
    runtime = NodeRuntime.from_config(
        config,
        coding_agent=_coding_agent_runtime(NonZeroExitCodingAgentProvider(CodingAgentConfig())),
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def full_agent_client(config: NodeConfig) -> Iterator[TestClient]:
    """TestClient with both coordinator and coding-agent stubs (configured)."""
    runtime = NodeRuntime.from_config(
        config,
        coordinator=_coordinator_runtime(StubCoordinatorProvider(CoordinatorConfig())),
        coding_agent=_coding_agent_runtime(StubCodingAgentProvider(CodingAgentConfig())),
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def make_step_client(config: NodeConfig):
    """Factory building a TestClient whose coordinator returns a scripted decision.

    ``coding_agent``/``mcp`` toggle whether those capabilities are configured (stub) or
    explicitly unconfigured — deterministic regardless of what is installed locally.
    """
    open_clients: list[TestClient] = []

    def _build(
        decision_fields: dict, *, coding_agent: bool = False, mcp: bool = False
    ) -> TestClient:
        coordinator = _coordinator_runtime(ScriptedCoordinatorProvider(decision_fields))
        if coding_agent:
            coding = _coding_agent_runtime(StubCodingAgentProvider(CodingAgentConfig()))
        else:
            coding = CodingAgentRuntime.from_config(
                CodingAgentConfig(executable="definitely-not-a-real-binary-xyz")
            )
        mcp_rt = (
            _mcp_runtime(StubMCPClient(MCPServerConfig()))
            if mcp
            else MCPRuntime.from_config(MCPServerConfig())  # command None -> unconfigured
        )
        runtime = NodeRuntime.from_config(
            config, coordinator=coordinator, coding_agent=coding, mcp=mcp_rt
        )
        test_client = TestClient(create_app(runtime=runtime))
        test_client.__enter__()
        open_clients.append(test_client)
        return test_client

    yield _build
    for test_client in open_clients:
        test_client.__exit__(None, None, None)


@pytest.fixture()
def step_live_server(coordinator_runtime: NodeRuntime) -> Iterator[str]:
    """Live server whose coordinator returns a respond decision (for CLI step tests)."""
    with _serve(create_app(runtime=coordinator_runtime)) as base_url:
        yield base_url


@pytest.fixture()
def mcp_runtime(config: NodeConfig) -> NodeRuntime:
    """A node runtime wired to a deterministic, subprocess-free MCP client."""
    return NodeRuntime.from_config(config, mcp=_mcp_runtime(StubMCPClient(MCPServerConfig())))


@pytest.fixture()
def mcp_client(mcp_runtime: NodeRuntime) -> Iterator[TestClient]:
    """TestClient whose MCP runtime uses the stub client."""
    app = create_app(runtime=mcp_runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def failing_mcp_client(config: NodeConfig) -> Iterator[TestClient]:
    """TestClient whose MCP client always fails, for the failure path."""
    runtime = NodeRuntime.from_config(config, mcp=_mcp_runtime(FailingMCPClient(MCPServerConfig())))
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def mcp_live_server(mcp_runtime: NodeRuntime) -> Iterator[str]:
    """Live server whose MCP runtime uses the stub client (for CLI tests)."""
    with _serve(create_app(runtime=mcp_runtime)) as base_url:
        yield base_url


@contextmanager
def _serve(app) -> Iterator[str]:
    """Run ``app`` on a real uvicorn server in a background thread; yield its base URL."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Live server failed to start in time.")
        time.sleep(0.02)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def live_server(config: NodeConfig) -> Iterator[str]:
    """Run a real uvicorn server in a background thread; yield its base URL.

    Used for tests that exercise streaming HTTP (SSE), which the in-process
    ``TestClient`` cannot drive concurrently with a second request.
    """
    with _serve(create_app(config)) as base_url:
        yield base_url


@pytest.fixture()
def coordinator_live_server(coordinator_runtime: NodeRuntime) -> Iterator[str]:
    """Live server whose coordinator uses the stub provider (for CLI tests)."""
    with _serve(create_app(runtime=coordinator_runtime)) as base_url:
        yield base_url


# -- Stage 13A agent-transport fixtures (temp identity dir; no real XDG state) ----


def _transport_config(tmp_path: Path, *, node_id: str = "test-node-id") -> NodeConfig:
    """A node config with an isolated temp identity directory + database."""
    from aithernet.config.settings import (
        AgentTransportConfig,
        IdentityConfig,
        TransportLocalDevelopmentConfig,
    )

    base = tmp_path / node_id
    base.mkdir(parents=True, exist_ok=True)
    return NodeConfig(
        node_id=node_id,
        node_name="aithernet-transport-test",
        database_url=f"sqlite:///{base / 'node.db'}",
        host="127.0.0.1",
        port=8080,
        log_level="warning",
        agent_transport=AgentTransportConfig(
            identity=IdentityConfig(state_directory=str(base / "identity")),
            local_development=TransportLocalDevelopmentConfig(allow_insecure_http=True),
        ),
    )


@pytest.fixture()
def transport_runtime(tmp_path) -> NodeRuntime:
    """A node runtime with an isolated temp identity directory (transport enabled)."""
    return NodeRuntime.from_config(_transport_config(tmp_path))


@pytest.fixture()
def transport_client(transport_runtime: NodeRuntime) -> Iterator[TestClient]:
    """TestClient for a transport-enabled node with an isolated temp identity dir."""
    app = create_app(runtime=transport_runtime)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def transport_live_server(transport_runtime: NodeRuntime) -> Iterator[str]:
    """Live uvicorn server for a transport-enabled node (for CLI tests)."""
    with _serve(create_app(runtime=transport_runtime)) as base_url:
        yield base_url


@pytest.fixture()
def coding_agent_live_server(coding_agent_runtime: NodeRuntime) -> Iterator[str]:
    """Live server whose coding agent uses the stub provider (for CLI tests)."""
    with _serve(create_app(runtime=coding_agent_runtime)) as base_url:
        yield base_url
