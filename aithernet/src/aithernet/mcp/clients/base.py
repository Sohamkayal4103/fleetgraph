"""Abstract MCP client interface.

A client is the transport-specific implementation of the Model Context Protocol: it
lists tools and calls them on an external server. The runtime depends on this abstraction
so gr-mcp (stdio) works now and other transports can be added later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.contracts import MCPToolCallResult, MCPToolInfo


class MCPClient(ABC):
    """Contract every MCP client implements.

    Constructed with an :class:`MCPServerConfig`; exposes async :meth:`list_tools` and
    :meth:`call_tool`. It knows nothing about FastAPI or the database.
    """

    #: Stable registry name for this client (matches ``MCPServerConfig.provider``).
    name: ClassVar[str]

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config

    @abstractmethod
    def check_ready(self) -> list[str]:
        """Return the names of missing/unresolved required config (empty when ready)."""

    @abstractmethod
    async def list_tools(self) -> list[MCPToolInfo]:
        """List the tools the configured server exposes (via ``tools/list``).

        Raises :class:`~aithernet.mcp.contracts.MCPConfigurationError` if not configured,
        or :class:`~aithernet.mcp.contracts.MCPClientError` on a transport/protocol fault.
        """

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict) -> MCPToolCallResult:
        """Call ``tool_name`` with ``arguments`` (via ``tools/call``).

        Raises :class:`~aithernet.mcp.contracts.MCPConfigurationError` if not configured,
        or :class:`~aithernet.mcp.contracts.MCPClientError` on a transport/protocol fault.
        A tool that runs but reports ``isError`` is returned with ``status="failed"``.
        """
