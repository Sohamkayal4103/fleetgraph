"""MCP runtime and generic client layer for the node.

Stage 4 implements the GNU Radio MCP boundary: a generic stdio MCP client plus a
transport-agnostic :class:`MCPRuntime`. The intended/default external server is the
ready-made ``yoelbassin/gr-mcp`` GNU Radio MCP server, connected over stdio. This package
is the Aithernet MCP client/runtime — it is NOT a copy of gr-mcp.

Tool names are discovered at runtime via ``tools/list``; no GNU Radio workflows or tool
names are hardcoded. The coordinator and coding agent do not yet route to MCP
automatically — that is a future stage. This package knows nothing about FastAPI or the
database; :class:`NodeRuntime` persists tool calls and bridges them.
"""

from aithernet.mcp.contracts import (
    ACTIVE_MCP_CAPABILITIES,
    FUTURE_MCP_CAPABILITIES,
    MCPClientError,
    MCPConfigurationError,
    MCPError,
    MCPServerStatus,
    MCPToolCallNotFoundError,
    MCPToolCallResult,
    MCPToolInfo,
)
from aithernet.mcp.diagnostics import MCPDiagnostics, MCPProbeResult, build_diagnostics
from aithernet.mcp.runtime import MCPRuntime

__all__ = [
    "ACTIVE_MCP_CAPABILITIES",
    "FUTURE_MCP_CAPABILITIES",
    "MCPToolInfo",
    "MCPToolCallResult",
    "MCPServerStatus",
    "MCPError",
    "MCPConfigurationError",
    "MCPClientError",
    "MCPToolCallNotFoundError",
    "MCPRuntime",
    "MCPDiagnostics",
    "MCPProbeResult",
    "build_diagnostics",
]
