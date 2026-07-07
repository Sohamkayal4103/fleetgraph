"""Coding Agent Runtime — the implementation/execution worker for the node.

Stage 3 implements the coding-agent boundary and the first real provider: Claude Code
via its CLI in non-interactive mode. The coordinator does not yet route to the coding
agent automatically, and GNU Radio MCP is not available to it; those are later stages.

This package knows nothing about FastAPI or the database; :class:`NodeRuntime` persists
tasks/results and bridges them into a :class:`CodingTaskExecutionInput`.
"""

from aithernet.coding_agent.contracts import (
    ACTIVE_CODING_CAPABILITIES,
    FUTURE_CODING_CAPABILITIES,
    CodingAgentConfigurationError,
    CodingAgentError,
    CodingAgentProviderError,
    CodingAgentStatus,
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
    CodingTaskNotFoundError,
)
from aithernet.coding_agent.runtime import CodingAgentRuntime

__all__ = [
    "ACTIVE_CODING_CAPABILITIES",
    "FUTURE_CODING_CAPABILITIES",
    "CodingTaskExecutionInput",
    "CodingTaskExecutionResult",
    "CodingAgentStatus",
    "CodingAgentError",
    "CodingAgentConfigurationError",
    "CodingAgentProviderError",
    "CodingTaskNotFoundError",
    "CodingAgentRuntime",
]
