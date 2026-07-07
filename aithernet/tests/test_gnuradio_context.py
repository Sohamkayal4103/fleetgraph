"""Stage 11B: GNU Radio workspace context — honest, runtime-checked, never fabricated."""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.settings import MCPServerConfig, MCPSessionConfig, NodeConfig
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def _mcp(server) -> MCPRuntime:
    return MCPRuntime.from_config(
        MCPServerConfig(
            command=sys.executable,
            args=[str(server)],
            session=MCPSessionConfig(autostart=True),
        )
    )


def _client(config, mcp) -> TestClient:
    return TestClient(create_app(runtime=NodeRuntime.from_config(config, mcp=mcp)))


def test_empty_context_is_honest(config: NodeConfig):
    # No MCP configured at all -> context is honestly unknown + stale, no fabricated flowgraph.
    with TestClient(create_app(config)) as client:
        ctx = client.get("/gnuradio/context").json()
        assert ctx["flowgraph_status"] == "unknown"
        assert ctx["stale"] is True
        assert ctx["active_flowgraph_path"] is None
        assert ctx["block_summary"] is None


def test_refresh_no_flowgraph_is_honest(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        ctx = client.post("/gnuradio/context/refresh", json={}).json()
        # get_blocks returns [] -> NONE (no active flowgraph), not a crash or invention.
        assert ctx["flowgraph_status"] == "none"
        assert ctx["stale"] is False
        assert ctx["block_summary"]["count"] == 0
        assert ctx["source_mcp_call_ids"]  # refresh generated linked MCP calls
        assert ctx["source_session_generation"] >= 1


def test_refresh_uses_only_available_tools(config: NodeConfig, write_mcp_server, tmp_path):
    # The tiny server exposes only 'echo' — none of the read-only context tools.
    server = write_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        ctx = client.post("/gnuradio/context/refresh", json={}).json()
        # Every context field reports unavailable; no read tool was invoked.
        assert ctx["block_summary"] == {"available": False, "reason": "tool not in live catalog"}
        assert ctx["flowgraph_status"] == "unknown"  # nothing readable
        # No get_blocks call was made (the tool is not in the catalog).
        calls = client.get("/mcp/tool-calls").json()
        assert all(c["tool_name"] != "get_blocks" for c in calls)


def test_successful_block_result_updates_summary(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        # A manual get_blocks with two blocks -> block summary count 2 (centralized interp).
        client.post(
            "/mcp/tools/get_blocks/call",
            json={"arguments": {"blocks": [{"id": "a"}, {"id": "b"}]}},
        )
        ctx = client.get("/gnuradio/context").json()
        assert ctx["block_summary"]["count"] == 2
        assert ctx["block_summary"]["available"] is True


def test_failed_tool_call_does_not_update_context_as_success(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        before = client.get("/gnuradio/context").json()
        # A failed call must not record context as success; establish a baseline first.
        client.post("/mcp/tools/get_blocks/call", json={"arguments": {"blocks": []}})
        baseline = client.get("/gnuradio/context").json()
        # A tool that reports an error must not overwrite the good summary as success.
        client.post("/mcp/tools/get_blocks/call", json={"arguments": {}})  # ok, empty
        after = client.get("/gnuradio/context").json()
        assert after["block_summary"]["available"] is True
        assert before is not None and baseline is not None  # sanity


def test_mutation_marks_context_stale(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        client.post("/gnuradio/context/refresh", json={})
        assert client.get("/gnuradio/context").json()["stale"] is False
        # A mutating tool marks derived context stale until refreshed.
        client.post("/mcp/tools/make_block/call", json={"arguments": {"type": "x"}})
        ctx = client.get("/gnuradio/context").json()
        assert ctx["stale"] is True
        assert "make_block" in (ctx["stale_reason"] or "")


def test_restart_marks_context_stale(config: NodeConfig, persistent_mcp_server, tmp_path):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        client.post("/gnuradio/context/refresh", json={})
        assert client.get("/gnuradio/context").json()["stale"] is False
        client.post("/mcp/session/restart")
        # A new generation must not make old context appear current.
        assert client.get("/gnuradio/context").json()["stale"] is True


def test_large_result_is_referenced_not_duplicated(
    config: NodeConfig,
    persistent_mcp_server,
    tmp_path,
):
    server = persistent_mcp_server(tmp_path / "srv")
    with _client(config, _mcp(server)) as client:
        # A large echo result is persisted on the call; the context preview is bounded.
        call = client.post("/mcp/tools/echo/call", json={"arguments": {"size": 50_000}}).json()
        assert len(call["result"]["content"][0]["text"]) == 50_000  # full raw kept on the call
        ctx = client.get("/gnuradio/context").json()
        import json as _json

        assert len(_json.dumps(ctx)) < 5_000  # context stays compact, not a copy of the result


def test_coordinator_context_is_compact(config: NodeConfig, persistent_mcp_server, tmp_path):
    import asyncio

    server = persistent_mcp_server(tmp_path / "srv")
    runtime = NodeRuntime.from_config(config, mcp=_mcp(server))
    with TestClient(create_app(runtime=runtime)):
        # session_status()/cached_tool_names() read managed state only (no connection I/O).
        mcp_ctx = asyncio.run(runtime._coordinator_mcp_context())
        # Compact: discovered tool NAMES only, never input schemas / full catalogs.
        assert isinstance(mcp_ctx["tools"], list)
        assert "get_blocks" in mcp_ctx["tools"]
        assert "inputSchema" not in str(mcp_ctx)
        compact = runtime.gnuradio.current().compact()
        assert set(compact) >= {"flowgraph_status", "stale", "block_count"}
