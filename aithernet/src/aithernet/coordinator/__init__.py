"""Coordinator Agent Runtime — mission-level reasoning for the node.

The coordinator receives mission context plus node state and decides the next
appropriate action. Stage 2 implements the runtime, the model-provider abstraction,
and the OpenAI-compatible and Anthropic providers. It does **not** execute the chosen
action: delegating to a coding agent, calling GNU Radio MCP, or contacting peer/manager
agents are later stages. Those appear only as documented ``next_target`` values and
``future_capabilities`` — never as active behavior.

This package knows nothing about FastAPI or the database; :class:`NodeRuntime` bridges
node state into a :class:`CoordinatorInput` and persists the returned decision.
"""

from aithernet.coordinator.contracts import (
    ACTIVE_CAPABILITIES,
    FUTURE_CAPABILITIES,
    CoordinatorConfigurationError,
    CoordinatorDecision,
    CoordinatorError,
    CoordinatorInput,
    CoordinatorProviderError,
    CoordinatorStatus,
    MissionNotFoundError,
)
from aithernet.coordinator.runtime import CoordinatorRuntime

__all__ = [
    "ACTIVE_CAPABILITIES",
    "FUTURE_CAPABILITIES",
    "CoordinatorInput",
    "CoordinatorDecision",
    "CoordinatorStatus",
    "CoordinatorError",
    "CoordinatorConfigurationError",
    "CoordinatorProviderError",
    "MissionNotFoundError",
    "CoordinatorRuntime",
]
