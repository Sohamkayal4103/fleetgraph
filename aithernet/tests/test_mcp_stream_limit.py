"""Part A regression: large MCP responses must not crash the backend.

A real temporary stdio MCP subprocess returns a single JSON-RPC frame larger than
asyncio's default 64 KiB line limit (the size that triggered
``ValueError: Separator is found, but chunk is longer than limit`` and an HTTP 500). These
tests prove the configurable ``stdio_stream_limit_bytes`` accepts large frames, an
undersized limit raises a clean :class:`MCPClientError` (never a raw ``ValueError``), and
the API returns a structured 502 with the call persisted as failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aithernet.api.app import create_app
from aithernet.config.settings import MCPServerConfig, NodeConfig
from aithernet.mcp.contracts import MCPClientError
from aithernet.mcp.runtime import MCPRuntime
from aithernet.orchestrator.runtime import NodeRuntime

#: A response comfortably larger than asyncio's 64 KiB (65536) default line limit.
_BIG = 200_000

#: The old default that used to crash: asyncio's 64 KiB stream limit.
_OLD_DEFAULT_LIMIT = 64 * 1024


def _runtime(script: Path, limit: int) -> MCPRuntime:
    return MCPRuntime.from_config(
        MCPServerConfig(
            command=sys.executable,
            args=[str(script)],
            stdio_stream_limit_bytes=limit,
        )
    )


def test_large_response_accepted_with_configured_limit(tmp_path, write_mcp_server) -> None:
    script = write_mcp_server(tmp_path / "server")
    runtime = _runtime(script, 16 * 1024 * 1024)

    result = asyncio.run(runtime.call_tool("echo", {"size": _BIG}))

    assert result.status == "completed"
    # Content is parsed correctly and not truncated.
    text = result.result["content"][0]["text"]
    assert len(text) == _BIG
    assert set(text) == {"x"}


def test_old_default_limit_raises_clean_mcp_error(tmp_path, write_mcp_server) -> None:
    # Reproduces the original failure: a >64 KiB frame under the old 64 KiB limit. It must
    # surface as MCPClientError, NOT a raw ValueError/LimitOverrunError.
    script = write_mcp_server(tmp_path / "server")
    runtime = _runtime(script, _OLD_DEFAULT_LIMIT)

    with pytest.raises(MCPClientError) as excinfo:
        asyncio.run(runtime.call_tool("echo", {"size": _BIG}))

    message = str(excinfo.value)
    assert "stdio stream limit" in message
    assert str(_OLD_DEFAULT_LIMIT) in message


def test_no_unhandled_valueerror_escapes(tmp_path, write_mcp_server) -> None:
    script = write_mcp_server(tmp_path / "server")
    runtime = _runtime(script, _OLD_DEFAULT_LIMIT)
    try:
        asyncio.run(runtime.call_tool("echo", {"size": _BIG}))
    except MCPClientError:
        pass  # expected, handled domain error
    except ValueError as exc:  # pragma: no cover - this is the bug we are fixing
        pytest.fail(f"raw ValueError escaped the MCP boundary: {exc!r}")


def test_api_returns_structured_502_when_limit_exceeded(
    config: NodeConfig, tmp_path, write_mcp_server
) -> None:
    script = write_mcp_server(tmp_path / "server")
    runtime = NodeRuntime.from_config(config, mcp=_runtime(script, _OLD_DEFAULT_LIMIT))
    with TestClient(create_app(runtime=runtime)) as client:
        response = client.post("/mcp/tools/echo/call", json={"arguments": {"size": _BIG}})
        assert response.status_code == 502  # structured transport failure, not 500
        assert "stdio stream limit" in response.json()["detail"]

        # The call is persisted as failed with the error recorded.
        calls = client.get("/mcp/tool-calls").json()
        assert len(calls) == 1
        assert calls[0]["status"] == "failed"
        assert "stdio stream limit" in (calls[0]["error"] or "")


def test_api_large_response_succeeds_with_default_limit(
    config: NodeConfig, tmp_path, write_mcp_server
) -> None:
    # The shipped default (16 MiB) comfortably accepts a large gr-mcp-style catalog frame.
    script = write_mcp_server(tmp_path / "server")
    runtime = NodeRuntime.from_config(config, mcp=_runtime(script, 16 * 1024 * 1024))
    with TestClient(create_app(runtime=runtime)) as client:
        response = client.post("/mcp/tools/echo/call", json={"arguments": {"size": _BIG}})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert len(body["result"]["content"][0]["text"]) == _BIG
