"""OpenAI-compatible Chat Completions coordinator provider.

Targets any service exposing the OpenAI ``/chat/completions`` API (OpenAI itself,
Azure-style gateways, vLLM, llama.cpp servers, Ollama's OpenAI shim, etc.). This is the
first real provider for Stage 2.

Failures are reported with actionable, *sanitized* detail: the exception class, a
non-empty description (even when ``str(exc)`` is empty, as some httpx timeouts are), the
sanitized endpoint, and — for HTTP errors — the status, content type, and a bounded,
secret-redacted slice of the response body. Request headers, bearer tokens, and API keys
are never included.
"""

from __future__ import annotations

import json
import re
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

#: Max chars of an upstream response body to include in an error (bounded, sanitized).
_ERROR_BODY_LIMIT = 600

#: Max chars of model content to preview in a parse-failure error.
_CONTENT_PREVIEW_LIMIT = 400


def _redact(text: str) -> str:
    """Mask common secret/token patterns so they never reach errors, logs, or the UI."""
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{4,}", "Bearer [REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9._\-]{4,}", "[REDACTED]", text)
    text = re.sub(
        r'(?i)("?(?:api[_-]?key|authorization|access[_-]?token|token|password)"?\s*[:=]\s*"?)'
        r"[^\"\s,}]{4,}",
        r"\1[REDACTED]",
        text,
    )
    return text


class OpenAICompatibleProvider(CoordinatorProvider):
    """Call an OpenAI-compatible Chat Completions endpoint via ``httpx.AsyncClient``."""

    name: ClassVar[str] = "openai_compatible"

    def __init__(self, config, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(config)
        # Tests inject an httpx MockTransport here; production leaves it None.
        self._transport = transport

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        if not self.config.base_url:
            missing.append("base_url")
        if not self.config.api_key and not self.config.allow_unauthenticated:
            missing.append("api_key")
        return missing

    def _endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _sanitized_endpoint(self) -> str:
        """Endpoint without any userinfo/query/fragment that could carry a secret."""
        try:
            url = httpx.URL(self._endpoint())
            netloc = url.host
            if url.port:
                netloc = f"{url.host}:{url.port}"
            return f"{url.scheme}://{netloc}{url.path}"
        except Exception:
            return "<endpoint>"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _body(self, payload: CoordinatorInput, *, include_response_format: bool) -> dict:
        body: dict = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(payload)},
            ],
        }
        if include_response_format:
            # Broadly supported; servers that don't understand it usually ignore it, and
            # the prompt still constrains output to a JSON object. A server that *rejects*
            # it triggers a single, narrow retry without the field (see ``decide``).
            body["response_format"] = {"type": "json_object"}
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        return body

    async def _post(self, endpoint: str, body: dict) -> httpx.Response:
        """POST to the endpoint, raising a sanitized provider error on transport failure."""
        try:
            from aithernet.tls import verify_option
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport,
                verify=verify_option(),
            ) as client:
                return await client.post(endpoint, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise CoordinatorProviderError(self._transport_error(exc)) from exc

    def _transport_error(self, exc: httpx.HTTPError) -> str:
        # str(exc) is empty for several httpx timeout types; fall back to repr so the
        # message is never blank (the original empty-"502" bug).
        detail = str(exc).strip() or repr(exc)
        category = ""
        if isinstance(exc, httpx.TimeoutException):
            category = f" (timeout after {self.config.timeout_seconds}s)"
        return (
            f"OpenAI-compatible request failed [{type(exc).__name__}]{category} "
            f"at {self._sanitized_endpoint()}: {_redact(detail)}"
        )

    def _http_error(self, response: httpx.Response) -> str:
        content_type = response.headers.get("content-type", "unknown")
        text = response.text or ""
        body = _redact(text)[:_ERROR_BODY_LIMIT] if text.strip() else "<empty response body>"
        return (
            f"OpenAI-compatible API returned HTTP {response.status_code} "
            f"at {self._sanitized_endpoint()} (content-type: {content_type}): {body}"
        )

    @staticmethod
    def _response_format_unsupported(response: httpx.Response) -> bool:
        """True only when the body explicitly blames the ``response_format`` field.

        Narrow on purpose: a generic 400/500 must NOT trigger a retry — only a server that
        names ``response_format``/``json_object`` as unsupported.
        """
        text = (response.text or "").lower()
        return "response_format" in text or "json_object" in text

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        missing = self.check_ready()
        if missing:
            raise CoordinatorConfigurationError(
                "OpenAI-compatible provider is missing required configuration: "
                + ", ".join(missing)
            )

        endpoint = self._endpoint()
        response = await self._post(endpoint, self._body(payload, include_response_format=True))

        # Narrow Ollama/llama.cpp compatibility: retry once WITHOUT response_format only
        # when the server explicitly rejects that field. Never retry arbitrary failures.
        if response.status_code in (400, 422) and self._response_format_unsupported(response):
            response = await self._post(
                endpoint, self._body(payload, include_response_format=False)
            )

        if response.status_code >= 400:
            raise CoordinatorProviderError(self._http_error(response))

        content = self._extract_content(response)
        return self._parse_decision(content, payload)

    async def healthcheck(self) -> dict:
        """One minimal real chat-completion for the ``--probe`` diagnostics (no secrets)."""
        missing = self.check_ready()
        if missing:
            raise CoordinatorConfigurationError(
                "OpenAI-compatible provider is missing required configuration: "
                + ", ".join(missing)
            )
        endpoint = self._endpoint()
        body = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        response = await self._post(endpoint, body)
        if response.status_code >= 400:
            raise CoordinatorProviderError(self._http_error(response))
        content = self._extract_content(response)
        return {
            "provider": self.name,
            "model_reported": [self.config.model] if self.config.model else [],
            "response_received": bool(content.strip()),
            "status_code": response.status_code,
        }

    def _extract_content(self, response: httpx.Response) -> str:
        """Pull the assistant message text, accepting string or content-block forms."""
        try:
            data = response.json()
        except ValueError as exc:
            preview = _redact(response.text or "")[:_CONTENT_PREVIEW_LIMIT]
            raise CoordinatorProviderError(
                f"OpenAI-compatible response at {self._sanitized_endpoint()} was not valid "
                f"JSON [{type(exc).__name__}]: {preview!r}"
            ) from exc

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            preview = _redact(json.dumps(data, default=str))[:_CONTENT_PREVIEW_LIMIT]
            raise CoordinatorProviderError(
                f"Unexpected OpenAI-compatible response shape at {self._sanitized_endpoint()} "
                f"(missing choices[0].message) [{type(exc).__name__}]: {preview}"
            ) from exc

        text = self._content_to_text(message.get("content"))
        if not text or not text.strip():
            raise CoordinatorProviderError(
                f"OpenAI-compatible response at {self._sanitized_endpoint()} contained no "
                f"usable message content (content type: {type(message.get('content')).__name__})."
            )
        return text

    @staticmethod
    def _content_to_text(content: object) -> str | None:
        """Normalize OpenAI 'content' (a string, or a list of text blocks) to text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            return "".join(parts) if parts else None
        return None

    def _parse_decision(self, content: str, payload: CoordinatorInput) -> CoordinatorDecision:
        """Parse a decision, reporting the failing stage and a sanitized content preview."""
        preview = _redact(content)[:_CONTENT_PREVIEW_LIMIT]
        try:
            raw_decision = extract_json_object(content)
        except CoordinatorProviderError as exc:
            raise CoordinatorProviderError(
                "OpenAI-compatible transport succeeded but the model output was not a JSON "
                f"object (extraction stage) at {self._sanitized_endpoint()}: {exc} "
                f"| content preview: {preview!r}"
            ) from exc
        try:
            return CoordinatorDecision.from_model_json(
                raw_decision, mission_id=payload.mission_id
            )
        except CoordinatorProviderError as exc:
            raise CoordinatorProviderError(
                "OpenAI-compatible transport succeeded but the decision JSON did not match "
                f"the schema (validation stage) at {self._sanitized_endpoint()}: {exc} "
                f"| content preview: {preview!r}"
            ) from exc
