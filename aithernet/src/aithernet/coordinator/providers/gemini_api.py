"""Gemini API coordinator provider (native ``generateContent`` REST API).

This is the FIRST-CLASS Gemini integration for the intended coordinator qualification path — it is
distinct from the optional Gemini CLI provider (``gemini_cli``), which shells out to an
interactively-authenticated CLI. ``gemini_api`` calls Google's Generative Language API directly
with ``httpx.AsyncClient`` and an API key supplied as an ``env:VAR`` reference (never inline).

Supported model + endpoint rules live HERE (one canonical place) so they are not duplicated across
config, docs and tests. The system prompt instructs the model to return one JSON object and
``responseMimeType: application/json`` enforces JSON output where supported.
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

#: Default Generative Language API base; overridable via config for gateways/proxies.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

#: API version path segment.
_API_VERSION = "v1beta"

#: Recognized Gemini model families (validation is advisory: unknown but plausible future models
#: are allowed, but a clearly non-Gemini model id is flagged). Kept here as the single source.
_SUPPORTED_MODEL_PREFIXES = ("gemini-",)

#: The canonical default model for a new beta.8 node (current, fast, low-cost). The exact value the
#: customer journey + handoff use, so there is no unexplained ``<model>`` placeholder.
DEFAULT_MODEL = "gemini-2.0-flash"

#: Recommended, currently-supported Gemini models (single source for docs/CLI/handoff).
RECOMMENDED_MODELS: tuple[str, ...] = (
    "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-flash", "gemini-1.5-pro",
)


def is_supported_model(model: str | None) -> bool:
    return bool(model) and any(model.startswith(p) for p in _SUPPORTED_MODEL_PREFIXES)


class GeminiAPIProvider(CoordinatorProvider):
    """Call the Gemini ``generateContent`` REST API via ``httpx.AsyncClient``."""

    name: ClassVar[str] = "gemini_api"

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        elif not is_supported_model(self.config.model):
            # Endpoint/model rule: this provider speaks the Gemini API; a non-gemini model id is a
            # misconfiguration (use openai_compatible/anthropic for those).
            missing.append(f"model (not a Gemini model: '{self.config.model}')")
        if not self.config.api_key:
            missing.append("api_key")
        return missing

    def _endpoint(self) -> str:
        base = (self.config.base_url or DEFAULT_BASE_URL).rstrip("/")
        return f"{base}/{_API_VERSION}/models/{self.config.model}:generateContent"

    def _headers(self) -> dict[str, str]:
        # Send the key as a header (never in the URL/query, which can be logged).
        return {"Content-Type": "application/json", "x-goog-api-key": self.config.api_key or ""}

    def _body(self, payload: CoordinatorInput) -> dict:
        gen: dict = {"responseMimeType": "application/json",
                     "maxOutputTokens": self.config.max_tokens}
        if self.config.temperature is not None:
            gen["temperature"] = self.config.temperature
        return {
            "systemInstruction": {"parts": [{"text": build_system_prompt()}]},
            "contents": [{"role": "user", "parts": [{"text": build_user_prompt(payload)}]}],
            "generationConfig": gen,
        }

    @staticmethod
    def _text_from_response(data: dict) -> str:
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        missing = self.check_ready()
        if missing:
            raise CoordinatorConfigurationError(
                "Gemini API provider is missing required configuration: " + ", ".join(missing)
            )
        try:
            from aithernet.tls import verify_option
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds,
                                         verify=verify_option()) as client:
                response = await client.post(
                    self._endpoint(), json=self._body(payload), headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise CoordinatorProviderError(f"Gemini API request failed: {exc}") from exc

        if response.status_code >= 400:
            raise CoordinatorProviderError(
                f"Gemini API returned HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            content = self._text_from_response(response.json())
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CoordinatorProviderError(f"Unexpected Gemini API response shape: {exc}") from exc

        raw_decision = extract_json_object(content)
        return CoordinatorDecision.from_model_json(raw_decision, mission_id=payload.mission_id)

    async def healthcheck(self) -> dict:
        """One minimal real generateContent call; returns sanitized metadata (never the key, the
        full prompt, or generated content)."""
        missing = self.check_ready()
        if missing:
            raise CoordinatorConfigurationError(
                "Gemini API provider is not configured: " + ", ".join(missing))
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with the single word: ok"}]}],
            "generationConfig": {"maxOutputTokens": 8},
        }
        try:
            from aithernet.tls import verify_option
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds,
                                         verify=verify_option()) as client:
                response = await client.post(self._endpoint(), json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise CoordinatorProviderError(f"Gemini API probe failed: {exc}") from exc
        if response.status_code >= 400:
            raise CoordinatorProviderError(
                f"Gemini API probe returned HTTP {response.status_code}")
        data = response.json()
        finish = None
        try:
            finish = data["candidates"][0].get("finishReason")
        except (KeyError, IndexError, TypeError):
            pass
        return {"provider": self.name, "model": self.config.model, "ok": True,
                "finish_reason": finish}
