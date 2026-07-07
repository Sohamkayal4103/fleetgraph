"""MCP clients and the client factory.

A client lists and calls tools on an external MCP server. The Stage 4 client is a generic
stdio JSON-RPC client; it contains no database or FastAPI code.
"""

from __future__ import annotations

from aithernet.config.settings import MCPServerConfig
from aithernet.mcp.clients.base import MCPClient
from aithernet.mcp.clients.stdio import StdioMCPClient

#: Registry of provider name -> client implementation.
CLIENTS: dict[str, type[MCPClient]] = {
    StdioMCPClient.name: StdioMCPClient,
}


def build_client(config: MCPServerConfig) -> MCPClient | None:
    """Construct the client named by ``config.provider``, or ``None`` if unknown.

    An unknown provider is reported as a clear configuration error when an operation is
    attempted; constructing one here never raises so the node can still start and report
    its status.
    """
    client_cls = CLIENTS.get(config.provider)
    if client_cls is None:
        return None
    return client_cls(config)


__all__ = ["MCPClient", "StdioMCPClient", "CLIENTS", "build_client"]
