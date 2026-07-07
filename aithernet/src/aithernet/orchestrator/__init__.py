"""Runtime orchestration: the in-process event bus and the node runtime.

The runtime owns node-level lifecycle and is the seam where later stages plug in the
coordinator agent, coding agent, and GNU Radio MCP access.
"""

from aithernet.orchestrator.event_bus import EventBus
from aithernet.orchestrator.runtime import NodeRuntime

__all__ = ["EventBus", "NodeRuntime"]
