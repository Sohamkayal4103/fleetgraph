"""The coding-agent runtime: a thin, provider-agnostic execution facade.

``CodingAgentRuntime`` holds the configured provider and exposes :meth:`execute`. It is
the seam the node runtime depends on, and the unit tests inject a test-only provider
here. It contains no database or HTTP-framework code.
"""

from __future__ import annotations

from aithernet.coding_agent.contracts import (
    ACTIVE_CODING_CAPABILITIES,
    FUTURE_CODING_CAPABILITIES,
    CodingAgentConfigurationError,
    CodingAgentStatus,
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
)
from aithernet.coding_agent.providers import build_provider
from aithernet.coding_agent.providers.base import CodingAgentProvider
from aithernet.config.settings import CodingAgentConfig


def bounded_task_workspace(workspace_root: str, task_id: str) -> str:
    """An absolute, per-task coding workspace beneath ``workspace_root``, fail-closed on any path
    that would escape the root (the task id must be a single, separator-free segment)."""
    from pathlib import Path
    root = Path(workspace_root).expanduser().resolve()
    candidate = (root / task_id).resolve()
    if root != candidate.parent:
        raise CodingAgentConfigurationError(
            f"coding workspace containment violation for task id {task_id!r}")
    return str(candidate)


class CodingAgentRuntime:
    """Owns the configured coding-agent provider and mediates execution calls."""

    def __init__(self, config: CodingAgentConfig, provider: CodingAgentProvider | None) -> None:
        self.config = config
        self.provider = provider

    @classmethod
    def from_config(cls, config: CodingAgentConfig) -> CodingAgentRuntime:
        """Build a runtime, resolving the provider named in ``config``.

        Construction never fails: an unknown or unavailable provider yields a runtime
        that reports itself as not configured and raises a clear error only if asked to
        execute.
        """
        return cls(config, build_provider(config))

    def is_configured(self) -> bool:
        """True when a known provider is selected and ready (executable resolvable)."""
        return self.provider is not None and not self.provider.check_ready()

    def refresh_readiness(self) -> None:
        """beta.5: re-probe provider readiness (drop the per-process cache). Called at retry so a
        locally-repaired coding environment/sandbox is re-detected, not assumed unavailable."""
        if self.provider is not None:
            self.provider.reset_readiness_cache()

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        """Delegate execution to the configured provider.

        Raises :class:`CodingAgentConfigurationError` when no usable provider is
        configured, rather than pretending the task ran.
        """
        if self.provider is None:
            raise CodingAgentConfigurationError(
                f"No coding-agent provider is configured for '{self.config.provider}'. "
                "Set a known provider and ensure its executable is available."
            )
        return await self.provider.execute(task)

    def status(self) -> CodingAgentStatus:
        """Return a secret-free configuration snapshot for the status endpoint."""
        if self.provider is None:
            missing = [f"provider (unknown: '{self.config.provider}')"]
        else:
            missing = self.provider.check_ready()
        return CodingAgentStatus(
            provider=self.config.provider,
            configured=self.is_configured(),
            executable=self.config.executable,
            workspace=self.config.workspace,
            missing_configuration=missing,
            active_capabilities=list(ACTIVE_CODING_CAPABILITIES),
            future_capabilities=list(FUTURE_CODING_CAPABILITIES),
        )
