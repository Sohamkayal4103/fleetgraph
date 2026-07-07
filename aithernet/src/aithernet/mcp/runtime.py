"""The MCP runtime: a transport-agnostic facade over the managed MCP session.

``MCPRuntime`` holds the persistent :class:`~aithernet.mcp.session.MCPSessionManager` for
the real stdio path (Stage 11B), or an injected :class:`~aithernet.mcp.clients.base.MCPClient`
double for tests. It exposes :meth:`list_tools`/:meth:`call_tool` plus session lifecycle
delegators. Production endpoints/CLI go through the single managed session; direct,
unmanaged calls (unit tests, a config-level diagnostics probe before the lifespan started
the session) fall back to an ephemeral one-shot connection.
"""

from __future__ import annotations

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.contracts import (
    ACTIVE_MCP_CAPABILITIES,
    FUTURE_MCP_CAPABILITIES,
    MCPConfigurationError,
    MCPServerStatus,
    MCPSessionState,
    MCPSessionStatus,
    MCPToolCallResult,
    MCPToolInfo,
)
from aithernet.mcp.session import EventEmitter, MCPSessionManager


class MCPRuntime:
    """Owns the managed MCP session (or an injected client double) for a node."""

    def __init__(
        self,
        config: MCPServerConfig,
        client=None,
        *,
        session: MCPSessionManager | None = None,
    ) -> None:
        self.config = config
        self.client = client  # legacy/test-double client path
        self.session = session  # managed persistent session (real path)

    @classmethod
    def from_config(
        cls,
        config: MCPServerConfig,
        *,
        node_id: str | None = None,
        on_event: EventEmitter | None = None,
    ) -> MCPRuntime:
        """Build a runtime with a managed session for the configured stdio server."""
        session = MCPSessionManager.from_config(config, node_id=node_id, on_event=on_event)
        return cls(config, client=None, session=session)

    # -- readiness ---------------------------------------------------------------

    def is_configured(self) -> bool:
        if self.client is not None:
            return not self.client.check_ready()
        if self.session is not None:
            return self.session.is_configured()
        return False

    def _missing(self) -> list[str]:
        if self.client is not None:
            return self.client.check_ready()
        if self.session is not None:
            return self.session.missing_configuration()
        return [f"provider (unknown: '{self.config.provider}')"]

    # -- tool operations (delegated to the managed session or stub) --------------

    async def list_tools(self, *, refresh: bool = False) -> list[MCPToolInfo]:
        if self.client is not None:
            return await self.client.list_tools()
        if self.session is None:
            raise MCPConfigurationError("No MCP client/session is configured.")
        if self.session.is_managed():
            return await self.session.list_tools(refresh=refresh)
        return await self.session.ephemeral_list_tools()

    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        if self.client is not None:
            return await self.client.call_tool(tool_name, arguments)
        if self.session is None:
            raise MCPConfigurationError("No MCP client/session is configured.")
        if self.session.is_managed():
            return await self.session.call_tool(tool_name, arguments)
        return await self.session.ephemeral_call_tool(tool_name, arguments)

    # -- session lifecycle (delegated) -------------------------------------------

    async def start_session(self) -> MCPSessionStatus:
        if self.session is not None:
            return await self.session.start()
        return self._synthetic_session_status()

    async def stop_session(self) -> MCPSessionStatus:
        if self.session is not None:
            return await self.session.stop()
        return self._synthetic_session_status()

    async def restart_session(self) -> MCPSessionStatus:
        if self.session is not None:
            return await self.session.restart()
        return self._synthetic_session_status()

    async def session_status(self) -> MCPSessionStatus:
        if self.session is not None:
            return await self.session.get_status()
        return self._synthetic_session_status()

    async def close_session(self) -> None:
        if self.session is not None:
            await self.session.close()

    def _synthetic_session_status(self) -> MCPSessionStatus:
        """A stopped-session snapshot for the stub/no-session path (e.g. unit-test doubles)."""
        return MCPSessionStatus(
            session_id="-",
            provider=self.config.provider,
            state=MCPSessionState.STOPPED,
            configured=self.is_configured(),
            command=self.command_summary(),
            missing_configuration=self._missing(),
            max_in_flight_requests=self.config.session.max_in_flight_requests,
            autostart=self.config.session.autostart,
            auto_restart=self.config.session.auto_restart,
        )

    # -- config snapshot ---------------------------------------------------------

    def command_summary(self) -> str | None:
        """A secret-free, human-readable launch-command summary (no env values)."""
        if not self.config.command:
            return None
        parts = [self.config.command]
        parts.extend(arg if arg is not None else "<unset>" for arg in self.config.args)
        return " ".join(parts)

    def status(self) -> MCPServerStatus:
        """Return a secret-free configuration snapshot for the status endpoint."""
        return MCPServerStatus(
            provider=self.config.provider,
            configured=self.is_configured(),
            command=self.command_summary(),
            server_name=None,
            missing_configuration=self._missing(),
            active_capabilities=list(ACTIVE_MCP_CAPABILITIES),
            available_tools=None,
            future_capabilities=list(FUTURE_MCP_CAPABILITIES),
        )
