"""Abstract coordinator provider interface and shared parsing helpers."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import ClassVar

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import (
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.sanitize import redact_secrets

__all__ = ["CoordinatorProvider", "extract_json_object", "redact_secrets"]


class CoordinatorProvider(ABC):
    """Contract every coordinator model provider implements.

    A provider is constructed with a :class:`CoordinatorConfig` and exposes an async
    :meth:`decide`. It knows nothing about FastAPI or the database.
    """

    #: Stable registry name for this provider.
    name: ClassVar[str]

    def __init__(self, config: CoordinatorConfig) -> None:
        self.config = config

    @abstractmethod
    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        """Reason over ``payload`` and return a structured decision.

        Implementations raise
        :class:`~aithernet.coordinator.contracts.CoordinatorConfigurationError` if
        required configuration is missing, or
        :class:`~aithernet.coordinator.contracts.CoordinatorProviderError` if the model
        call fails or returns an unusable response.
        """

    @abstractmethod
    def check_ready(self) -> list[str]:
        """Return the names of missing required config fields (empty when ready)."""

    async def healthcheck(self) -> dict:
        """Make one minimal real inference and return sanitized, non-secret metadata.

        Used only by the explicit coordinator *probe* diagnostics (never by status
        polling). The default raises so a provider without a cheap probe is reported
        honestly rather than faking success. Implementations must never include
        credentials, environment, or full prompts in the returned metadata.
        """
        raise CoordinatorProviderError(
            f"Provider '{self.name}' does not support a live probe."
        )


def extract_json_object(text: str) -> dict:
    """Parse a JSON object from raw model output.

    Tolerates a single Markdown code fence (```json ... ```) around the object, since
    some models wrap output despite instructions. Raises
    :class:`CoordinatorProviderError` if no JSON object can be parsed.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        # Drop the opening fence (with optional language tag) and the closing fence.
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else ""
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[: -len("```")]
        candidate = candidate.strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise CoordinatorProviderError(
            f"Coordinator returned non-JSON content: {text[:300]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise CoordinatorProviderError(
            f"Coordinator returned JSON of type {type(parsed).__name__}, expected an object."
        )
    return parsed
