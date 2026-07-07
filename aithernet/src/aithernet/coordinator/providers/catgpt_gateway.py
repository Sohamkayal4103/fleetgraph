"""CatGPT-Gateway coordinator provider (1.0.0-beta.5).

CatGPT-Gateway is a *local*, browser-session-backed gateway: the user runs it on their own
workstation, signs into ChatGPT/Claude in the gateway's own browser/session flow, and the gateway
re-exposes that subscription through an **OpenAI-compatible** local HTTP API (``/v1``). Aithernet
uses it as an OPTIONAL COORDINATOR provider so subscription-backed reasoning can drive mission
strategy while the coding agent (Codex CLI / Claude Code / …) stays a separate executor.

This adapter is deliberately a thin specialisation of :class:`OpenAICompatibleProvider`:

* it reuses the exact Chat Completions transport, the ``response_format=json_object`` structured
  path, the two-stage decision parsing, and the secret-redaction used by every OpenAI-compatible
  coordinator, so the mission engine sees an identical :class:`CoordinatorDecision`;
* it supplies **local-gateway defaults** — a ``127.0.0.1`` base URL and an optional API key — so a
  user who just started the gateway can connect with no cloud credential; and
* it honours per-provider environment overrides (base URL / model / api-key reference).

Security posture (local-only):

* Aithernet NEVER receives or stores the gateway's browser cookies/session tokens — it only speaks
  the OpenAI HTTP API to ``base_url``.
* No credential value is ever logged; the inherited ``_redact`` masks any that appear in an error.
* A *dummy* local key is permitted only because some gateways require a non-empty ``Authorization``
  header — it is clearly non-secret and local-only, and the provider works with no key at all.
"""

from __future__ import annotations

import os
from typing import ClassVar

import httpx

from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorProviderError,
)
from aithernet.coordinator.providers.base import extract_json_object
from aithernet.coordinator.providers.openai_compatible import OpenAICompatibleProvider, _redact

#: Default local endpoint for a freshly-started CatGPT-Gateway (OpenAI-compatible ``/v1`` root).
DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"

#: Lower-cased body markers that mean the gateway's own browser/session auth (ChatGPT/Claude web
#: login) is not ready — distinct from a wrong Aithernet-side bearer token. Never a secret.
_SESSION_MARKERS: tuple[str, ...] = (
    "login required", "please log in", "please sign in", "not logged in", "session expired",
    "browser session", "provider auth", "provider authentication", "authenticate in the browser",
    "sign in to", "re-authenticate", "session not ready",
)

# There is deliberately NO default model. The model is never fabricated: it comes only from what
# the operator explicitly configured (node.yaml / AITHERNET_CATGPT_GATEWAY_MODEL) or from a value
# the gateway actually reports via ``GET /models`` that the user chose. When neither is available,
# setup and the live test fail clearly rather than claiming a model the gateway may not serve.

#: Per-provider environment overrides (never a credential value for base_url/model; the api-key one
#: names an env var, matching the ``env:NAME`` reference model used everywhere else).
ENV_BASE_URL = "AITHERNET_CATGPT_GATEWAY_BASE_URL"
ENV_MODEL = "AITHERNET_CATGPT_GATEWAY_MODEL"
ENV_API_KEY_REF = "AITHERNET_CATGPT_GATEWAY_API_KEY_REF"


class CatGPTGatewayProvider(OpenAICompatibleProvider):
    """Call a local CatGPT-Gateway OpenAI-compatible endpoint as the coordinator."""

    name: ClassVar[str] = "catgpt_gateway"

    def __init__(self, config, *, transport: httpx.BaseTransport | None = None) -> None:
        # Resolve local-gateway defaults + env overrides ONCE, then hand the effective config to the
        # OpenAI-compatible base so every inherited method (decide/healthcheck/_body) uses them.
        super().__init__(self._effective_config(config), transport=transport)

    @classmethod
    def _effective_config(cls, config):
        """Config with local-gateway defaults + env overrides applied (base_url/model/api_key)."""
        base_url = (config.base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL)
        # No fabricated fallback: an unset model stays unset (surfaced as a clear, honest failure).
        model = (config.model or os.environ.get(ENV_MODEL) or None)
        api_key = config.api_key
        if not api_key:
            # AITHERNET_CATGPT_GATEWAY_API_KEY_REF names an env var holding the key (a reference,
            # never the key itself) — mirrors node.yaml's `api_key: env:NAME` model. Resolve it from
            # the process env OR the managed 0600 secret store, so a CLI process (which lacks the
            # service's EnvironmentFile) sends the same Authorization the running service would.
            ref = os.environ.get(ENV_API_KEY_REF)
            if ref:
                from aithernet.agents.providers import resolve_secret_value
                api_key = resolve_secret_value(ref)
        # A local gateway usually needs no external key; only require one if the operator set it.
        allow_unauthenticated = True if not api_key else config.allow_unauthenticated
        return config.model_copy(update={
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "allow_unauthenticated": allow_unauthenticated,
        })

    def check_ready(self) -> list[str]:
        """Missing readiness reasons. ``base_url`` defaults to the local gateway; the API key is
        OPTIONAL (local). A ``model`` is REQUIRED and never fabricated — it must be configured or
        chosen from what the gateway reports, so an unset model reads as not-ready here (the connect
        flow / live test then explain how to pick one)."""
        missing: list[str] = []
        if not self.config.base_url:
            missing.append("base_url")
        if not self.config.model:
            missing.append("model")
        return missing

    def models_url(self) -> str:
        """The ``GET {base_url}/models`` discovery URL for status/model listing."""
        return (self.config.base_url or "").rstrip("/") + "/models"

    async def list_models(self) -> list[str]:
        """Best-effort model discovery via ``GET /models`` (no token spend). Empty on any failure.

        Used only for status/connect UX — the provider never *fails* setup because discovery is
        unavailable (some gateways don't implement ``/models``); only live inference can fail.
        """
        try:
            from aithernet.tls import verify_option
            async with httpx.AsyncClient(
                timeout=min(self.config.timeout_seconds, 8.0), transport=self._transport,
                verify=verify_option(),
            ) as client:
                resp = await client.get(self.models_url(), headers=self._headers())
            if resp.status_code >= 400:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [m.get("id") for m in entries if isinstance(m, dict) and m.get("id")]

    async def healthcheck(self) -> dict:
        """One bounded, real live probe — but NEVER against a fabricated model.

        When no model is configured, resolve one from what the gateway actually reports (``GET
        /models``) or fail clearly. A configured model is used as-is; the gateway may still reject
        it with a real ``model_unavailable`` provider error, which is surfaced honestly.
        """
        if not self.config.model:
            models = await self.list_models()
            if models:
                raise CoordinatorConfigurationError(
                    "CatGPT-Gateway model not configured. The gateway offers: "
                    + ", ".join(models[:20])
                    + " — choose one with `aithernet agents connect coordinator` "
                    "(or set AITHERNET_CATGPT_GATEWAY_MODEL).")
            raise CoordinatorConfigurationError(
                "CatGPT-Gateway model not configured and model discovery is unavailable.")
        return await super().healthcheck()

    def _body(self, payload, *, include_response_format: bool) -> dict:
        """Use the SIMPLEST OpenAI-compatible request for CatGPT-Gateway coordinator calls.

        The gateway proxies a browser session and may not support ``response_format`` /
        ``json_object`` / ``tools`` / ``json_schema``. We therefore NEVER send those — the system
        prompt already constrains the model to emit a single JSON object, which the inherited
        two-stage parser reads from the assistant text into the identical ``CoordinatorDecision``
        the mission engine expects. (Free-form non-JSON never passes through as a decision: the
        parser raises, and the mission engine records a structured-output failure.)"""
        return super()._body(payload, include_response_format=False)

    async def live_probe(self) -> dict:
        """A bounded, real live test classified into an HONEST category (never "unreachable" for a
        reachable endpoint). Uses only the simplest OpenAI-compatible path: GET /models for
        reachability+credential, then a plain POST /chat/completions (model + messages, NO
        response_format/tools) asking for JSON, and parses JSON from the assistant text.

        Returns a secret-free dict: ``ok``, ``category``, ``endpoint_reachable``, ``status_code``,
        ``model_reported``, ``content_parseable``, ``detail``. Categories:
        ``catgpt_gateway_unreachable`` / ``credential_rejected`` / ``browser_session_not_ready`` /
        ``unsupported_gateway_feature`` / ``openai_compatibility_error`` / ``model_unavailable`` /
        ``quota_exhausted`` / ``rate_limited`` / ``structured_output_incompatible`` /
        ``gateway_http_error`` / ``ok``. Never includes headers, tokens, cookies, or session data.
        """
        from aithernet.tls import verify_option
        host = self._sanitized_endpoint()
        out: dict = {
            "ok": False, "category": "gateway_http_error", "endpoint_reachable": False,
            "status_code": None, "provider": self.name,
            "model_reported": [self.config.model] if self.config.model else [],
            "content_parseable": False, "detail": "",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport,
                verify=verify_option(),
            ) as client:
                # 1) reachability + credential via GET /models (a plain, low-cost request).
                try:
                    mresp = await client.get(self.models_url(), headers=self._headers())
                except httpx.HTTPError as exc:
                    out.update(
                        category="catgpt_gateway_unreachable",
                        detail=f"CatGPT-Gateway not reachable at {host} [{type(exc).__name__}] — "
                               "is the local gateway running?")
                    return out
                out["endpoint_reachable"] = True
                if mresp.status_code in (401, 403):
                    return self._classify_http(mresp, out, host)

                # 2) simplest possible chat completion: model + messages, no advanced features.
                body: dict = {
                    "model": self.config.model,
                    "messages": [{
                        "role": "user",
                        "content": ("Return only valid JSON with keys status and message. "
                                    "Use status=ready and message=ok."),
                    }],
                    "max_tokens": max(16, min(int(self.config.max_tokens), 256)),
                }
                if self.config.temperature is not None:
                    body["temperature"] = self.config.temperature
                try:
                    cr = await client.post(self._endpoint(), json=body, headers=self._headers())
                except httpx.HTTPError as exc:
                    out.update(
                        category="catgpt_gateway_unreachable",
                        detail=f"CatGPT-Gateway chat endpoint not reachable at {host} "
                               f"[{type(exc).__name__}].")
                    return out
        except httpx.HTTPError as exc:  # client construction / TLS
            out.update(category="catgpt_gateway_unreachable",
                       detail=f"CatGPT-Gateway not reachable at {host} [{type(exc).__name__}].")
            return out

        out["status_code"] = cr.status_code
        if cr.status_code >= 400:
            return self._classify_http(cr, out, host)

        # 3) 200 OK — parse structured JSON from the assistant text (no strict-mode assumption).
        try:
            content = self._extract_content(cr)
            extract_json_object(content)  # tolerates a single ```json fence; raises on non-JSON
        except CoordinatorProviderError:
            out.update(
                category="structured_output_incompatible",
                detail="the gateway responded but the assistant content was not parseable JSON — "
                       "the coordinator needs a model/gateway that returns a JSON object.")
            return out
        out.update(ok=True, category="ok", content_parseable=True,
                   detail="live bounded inference succeeded")
        return out

    def _classify_http(self, resp: httpx.Response, out: dict, host: str) -> dict:
        """Classify an HTTP error response into an honest, secret-free category."""
        status = resp.status_code
        out["status_code"] = status
        low = (resp.text or "").lower()
        body = _redact(resp.text or "")[:200]
        if status in (401, 403):
            if any(m in low for m in _SESSION_MARKERS):
                out.update(category="browser_session_not_ready",
                           detail=f"CatGPT-Gateway browser session/login not ready ({status}) — "
                                  "sign in through the gateway, then retry.")
            else:
                out.update(category="credential_rejected",
                           detail=f"CatGPT-Gateway rejected the local bearer token ({status}) — "
                                  "check the token via `aithernet agents set-secret`.")
            return out
        if any(m in low for m in _SESSION_MARKERS):
            out.update(category="browser_session_not_ready",
                       detail=f"CatGPT-Gateway browser session/login not ready ({status}).")
            return out
        if "quota" in low or "resource_exhausted" in low or "daily limit" in low:
            out.update(category="quota_exhausted",
                       detail=f"CatGPT-Gateway quota exhausted ({status}).")
            return out
        if status == 429 or "rate limit" in low or "too many requests" in low:
            out.update(category="rate_limited",
                       detail=f"CatGPT-Gateway rate-limited the request ({status}).")
            return out
        if ("model" in low and ("not found" in low or "does not exist" in low
                                or "unknown" in low)) or status == 404:
            out.update(category="model_unavailable",
                       detail=f"the gateway does not offer model {self.config.model!r} ({status}).")
            return out
        if status in (400, 422) and any(f in low for f in (
                "response_format", "json_schema", "tool_choice", "function_call", "tools")):
            out.update(category="unsupported_gateway_feature",
                       detail=f"the gateway rejected an advanced OpenAI feature ({status}): {body}")
            return out
        if status in (400, 422):
            out.update(category="openai_compatibility_error",
                       detail=f"the gateway rejected the request as OpenAI-incompatible "
                              f"({status}): {body}")
            return out
        out.update(category="gateway_http_error", detail=f"gateway HTTP {status} at {host}: {body}")
        return out
