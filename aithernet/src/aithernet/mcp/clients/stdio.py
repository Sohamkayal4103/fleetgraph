"""Generic stdio MCP transport — a reusable long-lived JSON-RPC connection.

Speaks JSON-RPC 2.0 over a child process's stdin/stdout, implementing the MCP stdio
lifecycle: ``initialize`` -> ``notifications/initialized`` -> many ``tools/list`` /
``tools/call`` -> terminate. Messages are newline-delimited JSON.

:class:`StdioConnection` is a **persistent** connection (Stage 11B): one subprocess, one
initialize handshake, one request-id sequence, one stdout reader loop, one stderr reader,
and a pending-request map keyed by JSON-RPC id. Writes to stdin are serialized by an async
lock and concurrency is bounded by a semaphore (``max_in_flight``). The legacy
:class:`StdioMCPClient` keeps the original open-per-call shape for any direct/ephemeral use
and for the client registry; the managed session (see :mod:`aithernet.mcp.session`) drives
:class:`StdioConnection` directly.

This module is transport-generic and supports the gr-mcp command shape. Tool names are
never assumed — callers discover them via ``tools/list``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from pathlib import Path
from typing import ClassVar

from aithernet import __version__
from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.clients.base import MCPClient
from aithernet.mcp.contracts import (
    MCPClientError,
    MCPConfigurationError,
    MCPToolCallResult,
    MCPToolInfo,
)

#: MCP protocol version advertised during the initialize handshake.
_PROTOCOL_VERSION = "2024-11-05"

#: Default grace period (seconds) to wait for the process to exit after closing stdin.
_TERMINATE_GRACE = 5.0


def resolve_command(command: str | None) -> str | None:
    """Return a runnable path for ``command``, or ``None`` if it cannot be resolved.

    Accepts an absolute/relative path to an existing executable file, or a bare name to
    look up on ``PATH``. Shared by the stdio transport and the MCP diagnostics so both
    report command resolution identically.
    """
    if not command:
        return None
    candidate = Path(command)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(command)


def check_stdio_ready(config: MCPServerConfig) -> list[str]:
    """Return missing/unresolved required stdio config (empty when ready)."""
    missing: list[str] = []
    if not config.command or resolve_command(config.command) is None:
        missing.append("command")
    if any(arg is None for arg in config.args):
        missing.append("args")
    if config.cwd is not None and not Path(config.cwd).is_dir():
        missing.append("cwd")
    return missing


def tool_error_text(result: dict) -> str:
    """Extract a human-readable error message from a tool result's content blocks."""
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = " ".join(part for part in parts if part).strip()
        if text:
            return text
    return "The MCP tool reported an error."


def to_tool_result(tool_name: str, raw: dict) -> MCPToolCallResult:
    """Wrap a raw ``tools/call`` result, honoring the server's ``isError`` flag."""
    is_error = bool(raw.get("isError"))
    return MCPToolCallResult(
        tool_name=tool_name,
        status="failed" if is_error else "completed",
        result=raw,
        error=tool_error_text(raw) if is_error else None,
    )


def parse_tool_infos(result: dict) -> list[MCPToolInfo]:
    """Parse a ``tools/list`` result into :class:`MCPToolInfo` objects."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return []
    infos: list[MCPToolInfo] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        infos.append(
            MCPToolInfo(
                name=str(tool.get("name", "")),
                description=tool.get("description"),
                input_schema=tool.get("inputSchema"),
            )
        )
    return infos


class StdioConnection:
    """A persistent JSON-RPC 2.0 connection to a stdio MCP subprocess.

    One subprocess, one initialize, one stdout reader loop, one stderr reader, one
    pending-request map. Concurrency is bounded by ``max_in_flight``; writes are serialized.
    A reader that hits EOF or a stream-limit overrun fails all pending requests honestly
    (never silently dropped, never replayed).
    """

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str | None,
        env: dict[str, str],
        timeout: float,
        stream_limit: int,
        max_in_flight: int = 1,
        stderr_buffer_bytes: int = 256 * 1024,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._stream_limit = stream_limit
        self._stderr_limit = stderr_buffer_bytes
        self._counter = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._write_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._stderr_buf = bytearray()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False
        self.server_info: dict = {}

    # -- properties --------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    @property
    def exit_code(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    def stderr_tail(self, limit: int = 2000) -> str:
        return self._stderr_buf[-limit:].decode("utf-8", errors="replace")

    async def wait_exit(self) -> int | None:
        """Await subprocess exit and return its code (used by the session watcher)."""
        if self._proc is None:
            return None
        return await self._proc.wait()

    # -- lifecycle ---------------------------------------------------------------

    async def open(self) -> dict:
        """Launch, start the reader loops, and run the MCP initialize handshake."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
                # Raise the per-line buffer cap above asyncio's 64 KiB default so a large
                # legitimate JSON-RPC frame (e.g. gr-mcp's block catalog) is not rejected.
                limit=self._stream_limit,
            )
        except (OSError, ValueError) as exc:
            raise MCPClientError(f"Failed to launch MCP server: {exc}") from exc

        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "aithernet", "version": __version__},
                },
            )
            info = result.get("serverInfo")
            self.server_info = info if isinstance(info, dict) else {}
            await self._notify("notifications/initialized")
        except BaseException:
            await self.close()
            raise
        return self.server_info

    async def close(self, grace: float = _TERMINATE_GRACE) -> None:
        """Stop accepting requests, terminate the subprocess, and fail any pending calls."""
        self._closed = True
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except (TimeoutError, Exception):
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_all(MCPClientError("MCP session was closed."))

    # -- reader loops ------------------------------------------------------------

    async def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # A single frame exceeded the buffer limit; the stream is now unframed and
                # cannot be recovered. Fail every pending request with an actionable error.
                self._fail_all(
                    MCPClientError(
                        "MCP response exceeded the configured stdio stream limit of "
                        f"{self._stream_limit} bytes. Increase "
                        "gnuradio_mcp.stdio_stream_limit_bytes if the server legitimately "
                        f"returns larger frames. ({type(exc).__name__})"
                    )
                )
                return
            if not line:  # EOF: the server closed stdout (process exiting).
                self._fail_all(
                    MCPClientError(
                        "MCP server closed the connection. "
                        f"stderr: {self.stderr_tail(500)!r}"
                    )
                )
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # A malformed frame cannot be correlated; skip it without corrupting the
                # pending map or resolving the wrong request.
                continue
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if message_id is None:
                continue  # a notification or server-initiated request — not our response
            future = self._pending.pop(message_id, None)
            if future is None or future.done():
                continue  # unknown/stale id — ignore, do not corrupt the session
            error = message.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                msg = error.get("message") if isinstance(error, dict) else str(error)
                future.set_exception(MCPClientError(f"MCP error {code}: {msg}"))
            else:
                result = message.get("result")
                future.set_result(result if isinstance(result, dict) else {})

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        with contextlib.suppress(Exception):
            while True:
                line = await stderr.readline()
                if not line:
                    break
                self._stderr_buf.extend(line)
                if len(self._stderr_buf) > self._stderr_limit:
                    # Bounded ring: keep only the most recent bytes.
                    del self._stderr_buf[: len(self._stderr_buf) - self._stderr_limit]

    def _fail_all(self, exc: MCPClientError) -> None:
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -- JSON-RPC ----------------------------------------------------------------

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    async def _notify(self, method: str, params: dict | None = None) -> None:
        async with self._write_lock:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
                .encode("utf-8")
            )
            await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict) -> dict:
        async with self._semaphore:
            if self._closed or self._proc is None or self._proc.stdin is None:
                raise MCPClientError("MCP session is not connected.")
            request_id = self._next_id()
            future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                async with self._write_lock:
                    self._proc.stdin.write(
                        (
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": method,
                                    "params": params,
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    await self._proc.stdin.drain()
                return await asyncio.wait_for(future, timeout=self._timeout)
            except TimeoutError as exc:
                raise MCPClientError(
                    f"MCP request '{method}' timed out after {self._timeout}s."
                ) from exc
            finally:
                # Always drop the pending future so a slow/late response cannot corrupt the
                # pending map (the reader simply ignores an unknown id afterwards).
                self._pending.pop(request_id, None)

    # -- MCP operations ----------------------------------------------------------

    async def list_tools(self) -> list[MCPToolInfo]:
        return parse_tool_infos(await self._request("tools/list", {}))

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        raw = await self._request("tools/call", {"name": tool_name, "arguments": arguments})
        return to_tool_result(tool_name, raw)


class StdioMCPClient(MCPClient):
    """Legacy per-call stdio client (open → operate → close), kept for the registry.

    The managed persistent session (:mod:`aithernet.mcp.session`) is the production path;
    this client is retained for the client registry and ephemeral/direct one-shot use.
    """

    name: ClassVar[str] = "stdio"

    def check_ready(self) -> list[str]:
        return check_stdio_ready(self.config)

    def _connection(self) -> StdioConnection:
        resolved = resolve_command(self.config.command)
        assert resolved is not None  # guarded by check_ready
        return StdioConnection(
            command=resolved,
            args=[arg for arg in self.config.args if arg is not None],
            cwd=self.config.cwd,
            env={**os.environ, **self.config.env},
            timeout=self.config.timeout_seconds,
            stream_limit=self.config.stdio_stream_limit_bytes,
            max_in_flight=self.config.session.max_in_flight_requests,
            stderr_buffer_bytes=self.config.session.stderr_buffer_bytes,
        )

    def _ensure_ready(self) -> None:
        missing = self.check_ready()
        if missing:
            raise MCPConfigurationError(
                "stdio MCP client is missing required configuration: " + ", ".join(missing)
            )

    async def list_tools(self) -> list[MCPToolInfo]:
        self._ensure_ready()
        conn = self._connection()
        await conn.open()
        try:
            return await conn.list_tools()
        finally:
            await conn.close()

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        self._ensure_ready()
        conn = self._connection()
        await conn.open()
        try:
            return await conn.call_tool(tool_name, arguments)
        finally:
            await conn.close()
