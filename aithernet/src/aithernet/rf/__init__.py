"""Multi-RF-backend foundation (Stage 13A.5).

Aithernet operates one or more external RF backends — the stable low-level GNU Radio MCP
server and the experimental higher-level Marconi MCP server — each as an independent MCP
subprocess. Aithernet remains the mission/autonomy/persistence/recovery/distributed-node
runtime; it never imports a backend's Python modules, never runs Marconi's Claude plugin or
skills, and operates every backend only through audited, atomic MCP tool calls.
"""

from __future__ import annotations

from aithernet.rf.context import RFWorkspaceContextService
from aithernet.rf.registry import RFBackendRegistry

__all__ = ["RFBackendRegistry", "RFWorkspaceContextService"]
