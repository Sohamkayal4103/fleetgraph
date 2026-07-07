"""Persistent MCP session manager (Stage 11B).

Replaces per-operation subprocess usage with ONE managed, long-lived MCP session per
running node. It launches the gr-mcp subprocess once, performs the initialize handshake
once, discovers and caches tools, reuses the same connection for many atomic calls,
watches process health, recovers within bounded limits, and shuts down cleanly.

This is infrastructure, not autonomy: a persistent session may execute many atomic calls,
but it NEVER replays a failed call and never executes an unrequested sequence of mission
actions. Session recovery and call retry are separate decisions — recovery never re-issues
the caller's in-flight request.

Generation semantics (documented and tested): ``generation`` counts how many MCP
subprocesses have successfully reached the ``ready`` state in this session's lifetime. It
increments by one only when a new process is launched, initialized, AND its tools
rediscovered. A *failed* start does not increment it. The cached tool catalog records the
``tool_catalog_generation`` that produced it; a restart clears the catalog and rediscovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.clients.stdio import (
    StdioConnection,
    check_stdio_ready,
    resolve_command,
)
from aithernet.mcp.contracts import (
    MCPClientError,
    MCPConfigurationError,
    MCPError,
    MCPSessionState,
    MCPSessionStatus,
    MCPToolCallResult,
    MCPToolInfo,
)
from aithernet.sanitize import redact_secrets
from aithernet.state.models import new_uuid, utcnow

# -- session lifecycle / context event types (sanitized; never carry env or secrets) --
EVENT_SESSION_STARTING = "mcp.session.starting"
EVENT_SESSION_READY = "mcp.session.ready"
EVENT_SESSION_DEGRADED = "mcp.session.degraded"
EVENT_SESSION_RESTARTING = "mcp.session.restarting"
EVENT_SESSION_STOPPED = "mcp.session.stopped"
EVENT_SESSION_FAILED = "mcp.session.failed"
EVENT_TOOLS_REFRESHED = "mcp.tools.refreshed"

#: Async callback the runtime wires to publish a sanitized session event.
EventEmitter = Callable[[str, str, dict], Awaitable[None]]

#: Builds a fresh connection for the next generation (injectable for tests).
ConnectionFactory = Callable[[], StdioConnection]


def _sanitize(text: str, limit: int = 600) -> str:
    return redact_secrets(text)[:limit]


class MCPSessionManager:
    """Owns the single long-lived MCP connection for a node and its lifecycle."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        node_id: str | None = None,
        connection_factory: ConnectionFactory | None = None,
        on_event: EventEmitter | None = None,
    ) -> None:
        self.config = config
        self.node_id = node_id
        self._factory = connection_factory
        self._on_event = on_event

        self.session_id = new_uuid()
        self._state = MCPSessionState.STOPPED
        self._generation = 0
        self._restart_count = 0
        self._consecutive_failures = 0
        self._gave_up = False  # set once the restart cap is exceeded (emit ONE summary, then stop)

        self._connection: StdioConnection | None = None
        self._tools: list[MCPToolInfo] = []
        self._tool_catalog_generation: int | None = None

        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task | None = None
        self._recover_task: asyncio.Task | None = None
        self._expecting_exit = False
        self._lifecycle_active = False  # True once a managed start has happened
        self._active_requests = 0

        self._started_at: datetime | None = None
        self._ready_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_activity_at: datetime | None = None
        self._last_error_type: str | None = None
        self._last_error: str | None = None

    # -- construction ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: MCPServerConfig,
        *,
        node_id: str | None = None,
        on_event: EventEmitter | None = None,
    ) -> MCPSessionManager:
        """Build a manager whose default factory launches the configured stdio server."""
        return cls(config, node_id=node_id, on_event=on_event)

    def _default_factory(self) -> StdioConnection:
        resolved = resolve_command(self.config.command)
        if resolved is None:
            raise MCPConfigurationError("MCP command does not resolve to a runnable path.")
        session = self.config.session
        return StdioConnection(
            command=resolved,
            args=[arg for arg in self.config.args if arg is not None],
            cwd=self.config.cwd,
            env={**os.environ, **self.config.env},
            timeout=self.config.timeout_seconds,
            stream_limit=self.config.stdio_stream_limit_bytes,
            max_in_flight=session.max_in_flight_requests,
            stderr_buffer_bytes=session.stderr_buffer_bytes,
        )

    def _build_connection(self) -> StdioConnection:
        if self._factory is not None:
            return self._factory()
        return self._default_factory()

    # -- readiness ---------------------------------------------------------------

    def is_configured(self) -> bool:
        if self.config.provider != "stdio" and self._factory is None:
            return False
        return not self.missing_configuration()

    def missing_configuration(self) -> list[str]:
        if self._factory is not None:
            return []
        if self.config.provider != "stdio":
            return [f"provider (unknown: '{self.config.provider}')"]
        return check_stdio_ready(self.config)

    def is_managed(self) -> bool:
        """True when a managed lifecycle is active (so callers should use this session)."""
        return self._lifecycle_active

    def set_event_emitter(self, emitter: EventEmitter | None) -> None:
        """Wire the runtime's sanitized event publisher (set after the runtime is built)."""
        self._on_event = emitter

    def cached_tool_names(self) -> set[str]:
        """Names from the currently cached tool catalog (no server call). Empty if none."""
        return {tool.name for tool in self._tools}

    def server_info(self) -> dict:
        """The live server's ``serverInfo`` (name/version) from the initialize handshake.

        Empty until the session has reached READY at least once. Used by the RF registry to
        report a backend's reported version (Stage 13A.5); never contains secrets.
        """
        return dict(self._connection.server_info) if self._connection is not None else {}

    @property
    def generation(self) -> int:
        return self._generation

    # -- event helper ------------------------------------------------------------

    async def _emit(self, event_type: str, message: str, payload: dict) -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                await self._on_event(event_type, message, payload)

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> MCPSessionStatus:
        """Start the session (idempotent when already ready). Never raises on failure.

        A failure to start marks the session ``failed`` and records the sanitized error so
        the rest of the API stays up — the caller reads the returned status.
        """
        async with self._lock:
            if self._state is MCPSessionState.READY and self._alive():
                return self._status()
            # An explicit start clears the permanent-failure latch + failure streak so a manual
            # retry (e.g. after the operator fixes the cause) gets a fresh bounded set of attempts.
            self._gave_up = False
            self._consecutive_failures = 0
            if not self.is_configured():
                self._mark_failed(
                    "MCPConfigurationError",
                    "MCP session is not configured: " + ", ".join(self.missing_configuration()),
                )
                await self._emit(EVENT_SESSION_FAILED, "MCP session not configured.", {})
                return self._status()
            await self._start_locked()
            return self._status()

    async def stop(self) -> MCPSessionStatus:
        """Stop the session and clean up the subprocess (idempotent when stopped)."""
        async with self._lock:
            if self._state is MCPSessionState.STOPPED:
                return self._status()
            await self._stop_locked()
            await self._emit(
                EVENT_SESSION_STOPPED,
                "MCP session stopped.",
                {"session_id": self.session_id, "generation": self._generation},
            )
            return self._status()

    async def restart(self) -> MCPSessionStatus:
        """Stop the current process and start a fresh one (generation increments).

        Policy: in-flight calls FAIL (they are neither drained nor replayed); the old
        process is stopped, a new one is started + initialized, and tools are rediscovered.
        """
        async with self._lock:
            self._restart_count += 1
            self._state = MCPSessionState.RESTARTING
            await self._emit(
                EVENT_SESSION_RESTARTING,
                "MCP session restarting.",
                {"session_id": self.session_id, "restart_count": self._restart_count},
            )
            await self._teardown_connection()
            await self._start_locked()
            return self._status()

    async def ensure_ready(self) -> MCPSessionStatus:
        """Ensure the session is ready, starting a configured stopped/degraded one.

        Never replays a caller's failed request — it only (re)establishes the session.
        """
        async with self._lock:
            if self._state is MCPSessionState.READY and self._alive():
                return self._status()
            if not self.is_configured():
                self._mark_failed(
                    "MCPConfigurationError",
                    "MCP session is not configured: " + ", ".join(self.missing_configuration()),
                )
                return self._status()
            await self._teardown_connection()
            await self._start_locked()
            return self._status()

    async def get_status(self) -> MCPSessionStatus:
        return self._status()

    async def close(self) -> None:
        """Clean shutdown (used by the application lifespan): leave no orphan process."""
        async with self._lock:
            if self._state is not MCPSessionState.STOPPED:
                await self._stop_locked()
        if self._recover_task is not None:
            self._recover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recover_task

    # -- internal lifecycle (lock held) ------------------------------------------

    def _alive(self) -> bool:
        return self._connection is not None and self._connection.is_alive()

    async def _start_locked(self) -> None:
        self._state = MCPSessionState.STARTING
        self._lifecycle_active = True
        self._started_at = utcnow()
        await self._emit(
            EVENT_SESSION_STARTING,
            "MCP session starting.",
            {"session_id": self.session_id},
        )
        connection: StdioConnection | None = None
        try:
            connection = self._build_connection()
            await connection.open()
            tools = await connection.list_tools()
        except Exception as exc:  # spawn/initialize/discovery failure
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close()
            self._connection = None
            self._mark_failed(type(exc).__name__, _sanitize(str(exc)))
            await self._emit(
                EVENT_SESSION_FAILED,
                f"MCP session failed to start: {self._last_error}",
                {"session_id": self.session_id, "error_type": self._last_error_type},
            )
            self._maybe_schedule_recovery()
            return

        self._connection = connection
        self._tools = tools
        self._generation += 1
        self._tool_catalog_generation = self._generation
        self._ready_at = utcnow()
        self._last_activity_at = self._ready_at
        self._consecutive_failures = 0
        self._gave_up = False  # a successful start clears the permanent-failure latch
        self._last_error_type = None
        self._last_error = None
        self._expecting_exit = False
        self._state = MCPSessionState.READY
        self._start_watcher(connection)
        await self._emit(
            EVENT_SESSION_READY,
            f"MCP session ready (generation {self._generation}, {len(tools)} tool(s)).",
            {
                "session_id": self.session_id,
                "generation": self._generation,
                "pid": connection.pid,
                "tool_count": len(tools),
            },
        )

    async def _stop_locked(self) -> None:
        self._state = MCPSessionState.STOPPING
        await self._teardown_connection()
        self._lifecycle_active = False
        self._stopped_at = utcnow()
        self._state = MCPSessionState.STOPPED

    async def _teardown_connection(self) -> None:
        """Cancel the watcher, close the connection, and clear the tool cache."""
        self._expecting_exit = True
        self._cancel_watcher()
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close(self.config.session.shutdown_grace_seconds)
        self._connection = None
        self._tools = []
        self._tool_catalog_generation = None

    def _mark_failed(self, error_type: str, message: str) -> None:
        self._state = MCPSessionState.FAILED
        self._lifecycle_active = False
        self._last_error_type = error_type
        self._last_error = message
        self._consecutive_failures += 1

    # -- process watcher + bounded auto-restart ----------------------------------

    def _start_watcher(self, connection: StdioConnection) -> None:
        self._watcher = asyncio.create_task(self._watch(connection))

    def _cancel_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None

    async def _watch(self, connection: StdioConnection) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            code = await connection.wait_exit()
            if self._expecting_exit or self._connection is not connection:
                return  # planned stop/restart already handled this exit
            async with self._lock:
                if self._expecting_exit or self._connection is not connection:
                    return
                self._state = MCPSessionState.DEGRADED
                self._consecutive_failures += 1
                self._last_error_type = "MCPProcessExited"
                self._last_error = _sanitize(
                    f"gr-mcp process exited unexpectedly (code {code}). "
                    f"stderr: {connection.stderr_tail(300)!r}"
                )
                self._connection = None
                self._tools = []
                self._tool_catalog_generation = None
                await self._emit(
                    EVENT_SESSION_DEGRADED,
                    f"MCP session degraded: {self._last_error}",
                    {
                        "session_id": self.session_id,
                        "exit_code": code,
                        "generation": self._generation,
                    },
                )
                self._maybe_schedule_recovery()

    def _maybe_schedule_recovery(self) -> None:
        session = self.config.session
        if not session.auto_restart:
            return
        if self._consecutive_failures > session.max_restart_attempts:
            # Permanent failure: stop restarting, settle into a STABLE failed state, and emit ONE
            # summarized diagnostic — never an unbounded restart/degraded event storm.
            if not self._gave_up:
                self._gave_up = True
                self._state = MCPSessionState.FAILED
                self._recover_task = asyncio.create_task(self._emit(
                    EVENT_SESSION_FAILED,
                    f"MCP session failed permanently after {session.max_restart_attempts} restart "
                    f"attempt(s); not retrying. Last error: {self._last_error}",
                    {"session_id": self.session_id, "attempts": self._restart_count,
                     "max_restart_attempts": session.max_restart_attempts}))
            return
        backoff = min(
            session.restart_initial_backoff_seconds * (2 ** (self._consecutive_failures - 1)),
            session.restart_max_backoff_seconds,
        )
        self._recover_task = asyncio.create_task(self._auto_recover(backoff))

    async def _auto_recover(self, backoff: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(backoff)
            async with self._lock:
                if self._state not in (MCPSessionState.DEGRADED, MCPSessionState.FAILED):
                    return  # a manual stop/start took over
                if not self.config.session.auto_restart:
                    return
                self._restart_count += 1
                self._state = MCPSessionState.RESTARTING
                await self._emit(
                    EVENT_SESSION_RESTARTING,
                    "MCP session auto-restarting after unexpected exit.",
                    {"session_id": self.session_id, "auto": True},
                )
                await self._teardown_connection()
                await self._start_locked()
                # _start_locked schedules another bounded recovery on failure.

    # -- tools -------------------------------------------------------------------

    async def list_tools(self, *, refresh: bool = False) -> list[MCPToolInfo]:
        await self._require_ready()
        if refresh or not self._tools:
            connection = self._connection
            if connection is None:
                raise MCPClientError("MCP session is not ready.")
            self._active_requests += 1
            try:
                self._tools = await connection.list_tools()
            finally:
                self._active_requests -= 1
            self._tool_catalog_generation = self._generation
            self._last_activity_at = utcnow()
            await self._emit(
                EVENT_TOOLS_REFRESHED,
                f"MCP tools refreshed: {len(self._tools)} tool(s).",
                {
                    "session_id": self.session_id,
                    "generation": self._generation,
                    "tool_count": len(self._tools),
                },
            )
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolCallResult:
        await self._require_ready()
        connection = self._connection
        if connection is None or not connection.is_alive():
            raise MCPClientError("MCP session is not ready (no live connection).")
        self._active_requests += 1
        try:
            result = await connection.call_tool(tool_name, arguments)
            self._last_activity_at = utcnow()
            return result
        except MCPError:
            self._last_activity_at = utcnow()
            raise  # never retried/replayed here — recovery is a separate decision
        finally:
            self._active_requests -= 1

    async def _require_ready(self) -> None:
        status = await self.ensure_ready()
        if status.state is not MCPSessionState.READY:
            if not status.configured:
                raise MCPConfigurationError(
                    "MCP session is not configured: "
                    + ", ".join(status.missing_configuration)
                )
            raise MCPClientError(
                f"MCP session is not ready (state={status.state.value}; "
                f"{status.last_error or 'startup failed'})."
            )

    # -- status ------------------------------------------------------------------

    def _command_summary(self) -> str | None:
        if not self.config.command:
            return None
        parts = [self.config.command]
        parts.extend(arg if arg is not None else "<unset>" for arg in self.config.args)
        return _sanitize(" ".join(parts), 400)

    def _status(self) -> MCPSessionStatus:
        uptime = None
        if self._state is MCPSessionState.READY and self._ready_at is not None:
            uptime = (utcnow() - self._ready_at).total_seconds()
        resolved = resolve_command(self.config.command) if self._factory is None else None
        tool_count = len(self._tools) if self._state is MCPSessionState.READY else None
        return MCPSessionStatus(
            session_id=self.session_id,
            node_id=self.node_id,
            provider=self.config.provider,
            state=self._state,
            configured=self.is_configured(),
            command=self._command_summary(),
            executable_resolves=(resolved is not None) if self._factory is None else None,
            working_directory=self.config.cwd,
            pid=self._connection.pid if self._connection is not None else None,
            generation=self._generation,
            restart_count=self._restart_count,
            started_at=self._started_at,
            ready_at=self._ready_at,
            stopped_at=self._stopped_at,
            last_activity_at=self._last_activity_at,
            uptime_seconds=uptime,
            tool_count=tool_count,
            tool_catalog_generation=self._tool_catalog_generation,
            active_requests=self._active_requests,
            max_in_flight_requests=self.config.session.max_in_flight_requests,
            last_error_type=self._last_error_type,
            last_error=self._last_error,
            autostart=self.config.session.autostart,
            auto_restart=self.config.session.auto_restart,
            missing_configuration=self.missing_configuration(),
        )

    # -- ephemeral (unmanaged) one-shots -----------------------------------------
    #
    # Used by MCPRuntime when no managed session is active (e.g. a direct unit-test call or
    # a config-level diagnostics probe before the lifespan started the session). These open
    # and close their own short-lived connection and never touch the managed state.

    async def ephemeral_list_tools(self) -> list[MCPToolInfo]:
        if not self.is_configured():
            raise MCPConfigurationError(
                "MCP is not configured: " + ", ".join(self.missing_configuration())
            )
        connection = self._build_connection()
        await connection.open()
        try:
            return await connection.list_tools()
        finally:
            await connection.close(self.config.session.shutdown_grace_seconds)

    async def ephemeral_call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> MCPToolCallResult:
        if not self.is_configured():
            raise MCPConfigurationError(
                "MCP is not configured: " + ", ".join(self.missing_configuration())
            )
        connection = self._build_connection()
        await connection.open()
        try:
            return await connection.call_tool(tool_name, arguments)
        finally:
            await connection.close(self.config.session.shutdown_grace_seconds)
