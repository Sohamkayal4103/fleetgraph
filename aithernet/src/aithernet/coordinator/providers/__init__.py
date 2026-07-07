"""Coordinator model providers and the provider factory.

A provider turns a :class:`~aithernet.coordinator.contracts.CoordinatorInput` into a
:class:`~aithernet.coordinator.contracts.CoordinatorDecision` by calling a real model
API. Providers contain no database or FastAPI code.
"""

from __future__ import annotations

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.providers.anthropic import AnthropicProvider
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.providers.catgpt_gateway import CatGPTGatewayProvider
from aithernet.coordinator.providers.claude_cli import ClaudeCLIProvider
from aithernet.coordinator.providers.cloud import (
    AnthropicBedrockProvider,
    AnthropicVertexProvider,
    GeminiVertexProvider,
)
from aithernet.coordinator.providers.gemini_api import GeminiAPIProvider
from aithernet.coordinator.providers.gemini_cli import GeminiCLIProvider
from aithernet.coordinator.providers.openai_compatible import OpenAICompatibleProvider

#: Registry of provider name -> implementation. ``openai_compatible_local`` and ``openai_api`` are
#: the same OpenAI-compatible adapter pointed at a local / the OpenAI endpoint (agent-agnostic: a
#: catalogue option maps to a role adapter, so each customer-facing choice is runnable). The cloud
#: adapters (Bedrock / Vertex) change only endpoint + auth on the Anthropic/Gemini adapters.
PROVIDERS: dict[str, type[CoordinatorProvider]] = {
    OpenAICompatibleProvider.name: OpenAICompatibleProvider,
    "openai_compatible_local": OpenAICompatibleProvider,
    "openai_api": OpenAICompatibleProvider,
    # beta.5: a local, subscription-backed OpenAI-compatible browser gateway (CatGPT-Gateway).
    # Its own adapter subclass applies local-gateway defaults + env overrides; the transport,
    # structured-JSON decision path, and redaction are the OpenAI-compatible ones.
    CatGPTGatewayProvider.name: CatGPTGatewayProvider,
    AnthropicProvider.name: AnthropicProvider,
    AnthropicBedrockProvider.name: AnthropicBedrockProvider,
    AnthropicVertexProvider.name: AnthropicVertexProvider,
    ClaudeCLIProvider.name: ClaudeCLIProvider,
    GeminiAPIProvider.name: GeminiAPIProvider,
    GeminiVertexProvider.name: GeminiVertexProvider,
    GeminiCLIProvider.name: GeminiCLIProvider,
}


def build_provider(config: CoordinatorConfig) -> CoordinatorProvider | None:
    """Construct the provider named by ``config``, or ``None`` if the name is unknown.

    An unknown provider name is reported by the runtime as a clear configuration error
    when reasoning is attempted; constructing one here never raises so the node can
    still start and report its status.
    """
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        return None
    return provider_cls(config)


__all__ = [
    "CoordinatorProvider",
    "OpenAICompatibleProvider",
    "CatGPTGatewayProvider",
    "AnthropicProvider",
    "GeminiCLIProvider",
    "PROVIDERS",
    "build_provider",
]
