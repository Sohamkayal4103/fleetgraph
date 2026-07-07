"""Stage 11B: persistent MCP session manager lifecycle, JSON-RPC, and recovery.

A fake **persistent** stdio MCP server (a real subprocess) backs these tests; no gr-mcp,
GNU Radio, or network is needed. Each test runs its whole lifecycle inside ONE event loop
(start → calls → restart → stop) so the persistent connection's tasks stay valid.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from aithernet.config.settings import MCPServerConfig, MCPSessionConfig
from aithernet.mcp.contracts import MCPClientError, MCPError, MCPSessionState
from aithernet.mcp.session import MCPSessionManager

# A persistent JSON-RPC stdio server: loops reading requests, supports echo/slow/crash/
# get_blocks, and (via threads) delayed/out-of-order responses.
_FAKE_SERVER = r'''
import sys, json, os, time, threading
_lock = threading.Lock()
def send(o):
    with _lock:
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def result(mid, text, is_error=False):
    send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":text}],"isError":is_error}})
TOOLS = [
    {"name":"echo","description":"echo","inputSchema":{"type":"object"}},
    {"name":"slow","description":"slow","inputSchema":{"type":"object"}},
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
            "capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1.0"}}})
        # Emit a stray notification to prove notifications never resolve a request future.
        send({"jsonrpc":"2.0","method":"server/notice","params":{"hello":True}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name"); args = params.get("arguments") or {}
        if name == "crash":
            os._exit(7)                      # die WITHOUT responding
        elif name == "slow":
            time.sleep(args.get("seconds", 2)); result(mid, "slow-done")
        elif name == "echo":
            delay = args.get("delay", 0)
            if "size" in args:
                result(mid, "x" * int(args["size"]))
            elif delay:
                threading.Timer(delay, lambda m=mid, a=args: result(m, json.dumps(a))).start()
            else:
                result(mid, json.dumps(args))
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
        else:
            result(mid, json.dumps(args))
    elif method == "exit":
        break
'''


def _write_server(path: Path) -> Path:
    path.write_text(_FAKE_SERVER)
    return path


def _manager(script: Path, **session_overrides) -> MCPSessionManager:
    return MCPSessionManager.from_config(
        MCPServerConfig(
            command=sys.executable,
            args=[str(script)],
            timeout_seconds=session_overrides.pop("timeout_seconds", 10.0),
            session=MCPSessionConfig(**session_overrides),
        )
    )


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False  # PermissionError is rare here; treat as not-ours/gone
    except OSError:
        return False


# -- session lifecycle -----------------------------------------------------------


def test_start_creates_one_process_and_discovers_tools(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        status = await mgr.start()
        assert status.state is MCPSessionState.READY
        assert status.generation == 1
        assert status.pid is not None and _pid_alive(status.pid)
        assert status.tool_count == 8
        tools = await mgr.list_tools()
        assert {"echo", "get_blocks"} <= {t.name for t in tools}
        await mgr.close()
        assert not _pid_alive(status.pid)

    asyncio.run(body())


def test_repeated_start_is_idempotent(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        s1 = await mgr.start()
        s2 = await mgr.start()  # already ready
        assert s1.pid == s2.pid and s1.generation == s2.generation == 1
        await mgr.close()

    asyncio.run(body())


def test_multiple_calls_reuse_the_same_process(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        await mgr.start()
        pid = (await mgr.get_status()).pid
        for _ in range(4):
            r = await mgr.call_tool("echo", {"v": 1})
            assert r.status == "completed"
        status = await mgr.get_status()
        assert status.pid == pid and status.generation == 1  # same process, same generation
        await mgr.close()

    asyncio.run(body())


def test_stop_is_idempotent_and_cleans_up(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        status = await mgr.start()
        pid = status.pid
        await mgr.stop()
        assert (await mgr.get_status()).state is MCPSessionState.STOPPED
        assert not _pid_alive(pid)
        again = await mgr.stop()  # idempotent
        assert again.state is MCPSessionState.STOPPED

    asyncio.run(body())


def test_restart_increments_generation_and_refreshes_tools(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        s1 = await mgr.start()
        s2 = await mgr.restart()
        assert s2.generation == s1.generation + 1
        assert s2.pid != s1.pid and not _pid_alive(s1.pid)
        assert s2.tool_count == 8  # rediscovered
        assert s2.restart_count == 1
        await mgr.close()

    asyncio.run(body())


def test_simultaneous_starts_create_one_process(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        results = await asyncio.gather(mgr.start(), mgr.start(), mgr.start())
        pids = {r.pid for r in results}
        assert len(pids) == 1  # exactly one process
        assert all(r.generation == 1 for r in results)
        await mgr.close()

    asyncio.run(body())


# -- process death + recovery ----------------------------------------------------


def test_unexpected_death_marks_degraded_and_fails_pending(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"), auto_restart=False)
        await mgr.start()
        # 'crash' kills the server mid-call WITHOUT responding -> the call must FAIL.
        with pytest.raises(MCPClientError):
            await mgr.call_tool("crash", {})
        # Give the watcher a moment to observe the exit.
        for _ in range(50):
            if (await mgr.get_status()).state is MCPSessionState.DEGRADED:
                break
            await asyncio.sleep(0.05)
        status = await mgr.get_status()
        assert status.state is MCPSessionState.DEGRADED
        assert status.last_error_type == "MCPProcessExited"
        await mgr.close()

    asyncio.run(body())


def test_auto_restart_is_bounded(tmp_path) -> None:
    async def body() -> None:
        # A server that exits immediately (start always fails); bounded auto-restart must
        # give up after max_restart_attempts rather than retrying forever.
        broken = tmp_path / "broken.py"
        broken.write_text("import sys\nsys.exit(1)\n")
        mgr = _manager(
            broken,
            auto_restart=True,
            max_restart_attempts=2,
            restart_initial_backoff_seconds=0.05,
            restart_max_backoff_seconds=0.1,
        )
        status = await mgr.start()
        assert status.state is MCPSessionState.FAILED  # could not initialize
        # Let the bounded recovery loop exhaust its attempts (backoff is tiny).
        await asyncio.sleep(1.0)
        final = await mgr.get_status()
        assert final.state is MCPSessionState.FAILED
        assert final.restart_count <= 2  # bounded by max_restart_attempts
        await mgr.close()

    asyncio.run(body())


# -- JSON-RPC & concurrency ------------------------------------------------------


def test_response_ids_correlate(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        await mgr.start()
        r1 = await mgr.call_tool("echo", {"tag": "a"})
        r2 = await mgr.call_tool("echo", {"tag": "b"})
        assert "a" in r1.result["content"][0]["text"]
        assert "b" in r2.result["content"][0]["text"]
        await mgr.close()

    asyncio.run(body())


def test_out_of_order_responses_correlate_with_concurrency(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"), max_in_flight_requests=2)
        await mgr.start()
        # 'a' responds after 0.3s, 'b' immediately -> b's frame arrives before a's.
        slow = mgr.call_tool("echo", {"tag": "a", "delay": 0.3})
        fast = mgr.call_tool("echo", {"tag": "b", "delay": 0})
        ra, rb = await asyncio.gather(slow, fast)
        assert "a" in ra.result["content"][0]["text"]  # correlated by id despite order
        assert "b" in rb.result["content"][0]["text"]
        await mgr.close()

    asyncio.run(body())


def test_request_timeout_does_not_corrupt_session(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"), timeout_seconds=0.3)
        await mgr.start()
        with pytest.raises(MCPClientError):
            await mgr.call_tool("slow", {"seconds": 2})  # exceeds the 0.3s timeout
        # A later valid call still works (the pending map was cleaned up).
        await asyncio.sleep(2.2)  # let the stale slow response drain and be ignored
        r = await mgr.call_tool("echo", {"ok": True})
        assert r.status == "completed"
        await mgr.close()

    asyncio.run(body())


def test_large_response_still_works(tmp_path) -> None:
    async def body() -> None:
        mgr = _manager(_write_server(tmp_path / "srv.py"))
        await mgr.start()
        r = await mgr.call_tool("echo", {"size": 200_000})
        assert len(r.result["content"][0]["text"]) == 200_000
        await mgr.close()

    asyncio.run(body())


def test_unconfigured_session_reports_failed(tmp_path) -> None:
    async def body() -> None:
        mgr = MCPSessionManager.from_config(MCPServerConfig())  # no command
        status = await mgr.start()
        assert status.state is MCPSessionState.FAILED
        assert status.configured is False
        with pytest.raises(MCPError):
            await mgr.call_tool("echo", {})

    asyncio.run(body())
