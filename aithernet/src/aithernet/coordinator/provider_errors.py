"""beta.10 Defect 3: a normalized, redacted provider-error record.

Replaces opaque ``CoordinatorProviderError`` strings with a structured, secret-free record that
classifies the failure, extracts retry-after, names a concise user action, and carries a bounded
redacted response excerpt plus a complete internal diagnostic digest. API keys, bearer headers,
cookies, OAuth tokens, and secret-bearing query strings are NEVER present in any field.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from aithernet.missions import failure_classification as fc

#: Substrings that, if found in a header NAME or token, mark a value we must never echo.
_REDACT = re.compile(
    r"(?i)(authorization|bearer\s+\S+|api[_-]?key|x-api-key|cookie|set-cookie|token|secret|"
    r"refresh_token|access_token|password)\s*[:=]\s*\S+")
_KEYISH = re.compile(r"(?i)\b(sk-[a-z0-9]{6,}|AIza[0-9A-Za-z_\-]{10,}|ghp_[0-9A-Za-z]{10,})\b")

#: Per-category concise, user-facing remediation. Adapter-agnostic.
_ACTION = {
    fc.MISSING_SECRET: "Store the API key: `aithernet agents set-secret <NAME>`.",
    fc.INVALID_SECRET: "The API key was rejected — rotate it with `aithernet agents set-secret`.",
    fc.AUTHENTICATION: "Authentication was rejected — re-check the provider credential.",
    fc.PAYMENT_REQUIRED: "The provider needs credit/billing — top up, then retry.",
    fc.QUOTA_EXHAUSTED: "Provider quota is exhausted — retry after reset or connect a fallback "
                        "coordinator.",
    fc.RATE_LIMIT: "Rate limited — the node retries with backoff automatically.",
    fc.MODEL_UNAVAILABLE: "The model is not offered by this endpoint — set a valid `--model`.",
    fc.ENDPOINT_UNAVAILABLE: "The endpoint is temporarily unavailable — the node retries.",
    fc.MALFORMED_ENDPOINT: "The base URL is malformed — fix it with `aithernet agents connect`.",
    fc.UNSUPPORTED_API: "The endpoint is not OpenAI-compatible as configured — check the base URL.",
    fc.TLS: "TLS/certificate verification failed — check the endpoint's certificate.",
    fc.TIMEOUT: "The provider timed out — the node retries with backoff.",
    fc.NETWORK: "Network error reaching the provider — the node retries.",
    fc.MALFORMED_OUTPUT: "The provider returned malformed output — the node retries.",
    fc.SCHEMA_INVALID: "The provider returned a schema-invalid decision — the node retries.",
    fc.PROVIDER_INTERNAL: "The provider had an internal error — the node retries.",
    fc.CONFIGURATION: "Coordinator configuration is incomplete — run `aithernet agents connect`.",
    fc.UNKNOWN: "The provider call failed — see diagnostics.",
}


#: The canonical, adapter-agnostic error vocabulary exposed to operators + research (beta.4).
CANONICAL_CATEGORIES = frozenset({
    "executable_missing", "unsupported_version", "unauthenticated", "authentication_expired",
    "invalid_credential", "permission_denied", "subscription_unavailable", "api_billing_required",
    "credit_exhausted", "quota_exhausted", "rate_limited", "model_unavailable",
    "model_incompatible", "endpoint_unavailable", "network_failure", "tls_failure", "timeout",
    "cancelled", "malformed_response", "structured_output_invalid", "tool_call_invalid",
    "workspace_violation", "provider_internal_error",
})

#: Map the internal fine-grained classifier categories onto the canonical vocabulary.
_CANONICAL: dict[str, str] = {
    fc.EXECUTABLE: "executable_missing",
    fc.AUTHENTICATION: "unauthenticated",
    fc.MISSING_SECRET: "unauthenticated",
    fc.INVALID_SECRET: "invalid_credential",
    fc.PAYMENT_REQUIRED: "api_billing_required",
    fc.QUOTA_EXHAUSTED: "quota_exhausted",
    fc.RATE_LIMIT: "rate_limited",
    fc.MODEL_UNAVAILABLE: "model_unavailable",
    fc.ENDPOINT_UNAVAILABLE: "endpoint_unavailable",
    fc.MALFORMED_ENDPOINT: "endpoint_unavailable",
    fc.UNSUPPORTED_API: "endpoint_unavailable",
    fc.NETWORK: "network_failure",
    fc.TLS: "tls_failure",
    fc.TIMEOUT: "timeout",
    fc.MALFORMED_OUTPUT: "malformed_response",
    fc.SCHEMA_INVALID: "structured_output_invalid",
    fc.PROVIDER_INTERNAL: "provider_internal_error",
    fc.CONFIGURATION: "permission_denied",
    fc.UNKNOWN: "provider_internal_error",
}

#: Canonical categories the coding/coordinator runtimes raise directly (not HTTP-derived).
_DIRECT_CANONICAL = frozenset({
    "executable_missing", "unsupported_version", "authentication_expired", "permission_denied",
    "subscription_unavailable", "credit_exhausted", "model_incompatible", "cancelled",
    "tool_call_invalid", "workspace_violation",
})


def canonical_category(internal_or_canonical: str) -> str:
    """Return the canonical category for an fc category (pass-through if already canonical)."""
    if internal_or_canonical in CANONICAL_CATEGORIES:
        return internal_or_canonical
    return _CANONICAL.get(internal_or_canonical, "provider_internal_error")


def _redact(text: str, limit: int = 280) -> str:
    """Strip secret-bearing fragments and bound the length of a response excerpt."""
    if not text:
        return ""
    cleaned = _KEYISH.sub("[redacted]", _REDACT.sub("[redacted]", text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _safe_host(host: str) -> str:
    """A host[:port] with any secret-bearing query string or userinfo stripped."""
    if not host:
        return ""
    host = host.split("?", 1)[0].split("#", 1)[0]
    return host.rsplit("@", 1)[-1]  # drop any user:pass@ prefix


@dataclass
class ProviderErrorRecord:
    provider: str
    host: str
    model: str
    category: str
    disposition: str          # permanent | transient | quota
    retryable: bool
    status_code: int | None = None
    provider_error_code: str | None = None
    retry_after_seconds: int | None = None
    action: str = ""
    excerpt: str = ""          # bounded, redacted response excerpt
    diagnostic_digest: str = ""  # sha256 over the full (un-truncated) internal detail
    # -- beta.4 additions: canonical vocabulary + adapter/role/effective-model provenance --
    canonical_category: str = ""   # one of CANONICAL_CATEGORIES
    role: str = ""                 # coordinator | coding
    adapter: str = ""              # the adapter kind (cli | api | local | cloud)
    effective_model: str = ""      # the model the provider actually used, when returned

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_quota(self) -> bool:
        return self.category == fc.QUOTA_EXHAUSTED


#: Concise operator action for canonical categories the runtimes raise directly.
_CANONICAL_ACTION = {
    "unsupported_version": "The provider CLI version is unsupported — upgrade it, then retry.",
    "authentication_expired": "The provider login expired — sign in again as the runtime user.",
    "permission_denied": "The provider denied permission — re-check the credential/scope.",
    "subscription_unavailable": "The subscription is unavailable or exhausted — retry after reset "
                                "or connect a fallback.",
    "credit_exhausted": "Provider credit is exhausted — top up the account, then retry.",
    "model_incompatible": "The model is incompatible with this role — choose a compatible model.",
    "cancelled": "The invocation was cancelled.",
    "tool_call_invalid": "The provider produced an invalid tool call — the node retries.",
    "workspace_violation": "The provider attempted to act outside its authorized workspace — "
                           "blocked.",
}


def normalize_provider_error(
    *, provider: str, model: str = "", host: str = "", status_code: int | None = None,
    retry_after: int | str | None = None, provider_error_code: str | None = None,
    body: str = "", exc: Exception | None = None,
    role: str = "", adapter: str = "", effective_model: str = "",
    category_override: str | None = None,
) -> ProviderErrorRecord:
    """Build a normalized, secret-free :class:`ProviderErrorRecord` from raw provider failure.

    ``category_override`` lets a runtime supply a canonical category directly (e.g.
    ``workspace_violation``, ``cancelled``, ``tool_call_invalid``) that no HTTP status implies.
    """
    if category_override in CANONICAL_CATEGORIES:
        canonical = category_override
        cat = canonical  # store the canonical name as the primary category too
        disp = fc.QUOTA if canonical == "quota_exhausted" else (
            fc.PERMANENT if canonical in (
                "workspace_violation", "permission_denied", "subscription_unavailable",
                "credit_exhausted", "model_incompatible", "authentication_expired",
                "unsupported_version", "executable_missing", "invalid_credential", "cancelled",
            ) else fc.TRANSIENT)
    else:
        text_for_class = " ".join(filter(None, [
            f"http {status_code}" if status_code else "", provider_error_code or "", body,
            str(exc) if exc else ""]))
        cat = fc.category(exc, text_for_class)
        # HTTP status is a strong signal that text markers can miss.
        if status_code == 402:
            cat = fc.PAYMENT_REQUIRED
        elif status_code == 429 and cat not in (fc.QUOTA_EXHAUSTED, fc.PAYMENT_REQUIRED):
            cat = fc.RATE_LIMIT
        elif status_code in (401, 403) and cat == fc.UNKNOWN:
            cat = fc.AUTHENTICATION
        disp = fc.disposition_for(cat)
        canonical = canonical_category(cat)
    ra: int | None = None
    if retry_after is not None:
        try:
            ra = int(float(str(retry_after)))
        except (TypeError, ValueError):
            ra = None
    full_detail = "\n".join(filter(None, [
        f"provider={provider}", f"role={role}", f"model={model}",
        f"effective_model={effective_model}", f"host={_safe_host(host)}",
        f"status={status_code}", f"code={provider_error_code}", body or "",
        repr(exc) if exc else ""]))
    action = _ACTION.get(cat) or _CANONICAL_ACTION.get(canonical) or _ACTION[fc.UNKNOWN]
    return ProviderErrorRecord(
        provider=provider, host=_safe_host(host), model=model, category=cat, disposition=disp,
        retryable=disp != fc.PERMANENT, status_code=status_code,
        provider_error_code=provider_error_code, retry_after_seconds=ra,
        action=action, excerpt=_redact(body),
        diagnostic_digest="sha256:" + hashlib.sha256(_redact(full_detail, 4000).encode()
                                                      ).hexdigest(),
        canonical_category=canonical, role=role, adapter=adapter,
        effective_model=effective_model or model)
