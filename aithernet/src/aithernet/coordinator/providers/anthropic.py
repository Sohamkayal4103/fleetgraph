"""Anthropic Messages API coordinator provider.

A second real provider, implemented directly against the Anthropic Messages API with
``httpx.AsyncClient``. The system prompt instructs the model to return a single JSON
object (the Messages API has no JSON response-format flag, so the constraint is enforced
by the prompt and verified on parse).
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.prompts import build_system_prompt, build_user_prompt
from aithernet.coordinator.providers.base import CoordinatorProvider, extract_json_object

#: Default Anthropic API base; overridable via config for gateways/proxies.
_DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Messages API version header value.
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(CoordinatorProvider):
    """Call the Anthropic Messages API via ``httpx.AsyncClient``."""

    name: ClassVar[str] = "anthropic"

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        if not self.config.api_key:
            missing.append("api_key")
        return missing

    def _endpoint(self) -> str:
        base = (self.config.base_url or _DEFAULT_BASE_URL).rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        return f"{base}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key or "",
            "anthropic-version": _ANTHROPIC_VERSION,
        }

    def _body(self, payload: CoordinatorInput) -> dict:
        body: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": build_system_prompt(),
            "messages": [{"role": "user", "content": build_user_prompt(payload)}],
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        return body

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        missing = self.check_ready()
        if missing:
            raise CoordinatorConfigurationError(
                "Anthropic provider is missing required configuration: " + ", ".join(missing)
            )

        try:
            from aithernet.tls import verify_option
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds,
                                         verify=verify_option()) as client:
                response = await client.post(
                    self._endpoint(), json=self._body(payload), headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise CoordinatorProviderError(f"Anthropic request failed: {exc}") from exc

        if response.status_code >= 400:
            raise CoordinatorProviderError(
                f"Anthropic API returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            data = response.json()
            content = data["content"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CoordinatorProviderError(
                f"Unexpected Anthropic response shape: {exc}"
            ) from exc

        raw_decision = extract_json_object(content)
        return CoordinatorDecision.from_model_json(raw_decision, mission_id=payload.mission_id)
