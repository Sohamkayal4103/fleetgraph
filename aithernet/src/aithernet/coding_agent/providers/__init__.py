"""Coding-agent providers and the provider factory.

A provider turns a :class:`~aithernet.coding_agent.contracts.CodingTaskExecutionInput`
into a :class:`~aithernet.coding_agent.contracts.CodingTaskExecutionResult` by running a
real coding agent. Providers contain no database or FastAPI code.
"""

from __future__ import annotations

from aithernet.coding_agent.providers.base import CodingAgentProvider
from aithernet.coding_agent.providers.claude_code import ClaudeCodeProvider
from aithernet.coding_agent.providers.codex_cli import CodexCliProvider
from aithernet.config.settings import CodingAgentConfig

#: Registry of provider name -> implementation. ``codex_api`` is the Codex CLI driven by an OpenAI
#: API key (separate billing) instead of a ChatGPT subscription; ``local_coding`` is the Codex CLI
#: pointed at a local OpenAI-compatible endpoint (local compute). Both are the same runnable adapter
#: with different auth/endpoint config, so each customer-facing choice is executable by the engine.
PROVIDERS: dict[str, type[CodingAgentProvider]] = {
    ClaudeCodeProvider.name: ClaudeCodeProvider,
    CodexCliProvider.name: CodexCliProvider,
    "codex_api": CodexCliProvider,
    "local_coding": CodexCliProvider,
}


def build_provider(config: CodingAgentConfig) -> CodingAgentProvider | None:
    """Construct the provider named by ``config``, or ``None`` if the name is unknown.

    An unknown provider name is reported as a clear configuration error when execution is
    attempted; constructing one here never raises so the node can still start and report
    its status.
    """
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        return None
    return provider_cls(config)


__all__ = [
    "CodingAgentProvider",
    "ClaudeCodeProvider",
    "CodexCliProvider",
    "PROVIDERS",
    "build_provider",
]
