"""Classify mission provider failures as PERMANENT (non-retryable) or TRANSIENT, and into a
fine-grained category for telemetry and bounded-backoff behaviour.

A permanent failure — missing API key/model/endpoint, an absent executable, an unsupported/unknown
provider, an authentication rejection, or an invalid canonical state path — must stop after ONE
attempt and name the required customer action, NOT be retried until ``resource_budget_exhausted``.
Transient failures (network, timeout, 5xx, rate limit, a one-off malformed/schema-invalid response)
keep their bounded-retry behaviour, now spaced by exponential backoff rather than three immediate
identical retries (beta.9 Defect 3).

Classification is primarily by exception TYPE (configuration errors are permanent); text markers are
a conservative fallback when only an error string is available. Unknown failures default to
TRANSIENT so we never guess a config error and strand a recoverable mission.
"""

from __future__ import annotations

PERMANENT = "permanent"
TRANSIENT = "transient"
QUOTA = "quota"  # beta.10 Defect 4: recoverable, but must NOT burn the failure budget

# -- fine-grained categories (beta.9 Defect 3, extended in beta.10 Defect 3/4) ----------------
TIMEOUT = "timeout"
NETWORK = "network"
RATE_LIMIT = "rate_limit"
QUOTA_EXHAUSTED = "quota_exhausted"
PAYMENT_REQUIRED = "payment_required"
AUTHENTICATION = "authentication"
MISSING_SECRET = "missing_secret"
INVALID_SECRET = "invalid_secret"
EXECUTABLE = "executable"
CONFIGURATION = "configuration"
MODEL_UNAVAILABLE = "model_unavailable"
ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
MALFORMED_ENDPOINT = "malformed_endpoint"
UNSUPPORTED_API = "unsupported_api"
TLS = "tls"
MALFORMED_OUTPUT = "malformed_output"
SCHEMA_INVALID = "schema_invalid"
PROVIDER_INTERNAL = "provider_internal"
UNKNOWN = "unknown"

#: Disposition of each category. Auth / executable / configuration / payment / bad-model are
#: customer-fixable — they block after ONE attempt and must NOT exhaust the general failure budget.
#: QUOTA_EXHAUSTED is recoverable but special: the mission parks in a recoverable quota state
#: without burning the failure budget (beta.10 Defect 4). The rest get bounded-backoff retries.
CATEGORY_DISPOSITION: dict[str, str] = {
    TIMEOUT: TRANSIENT,
    NETWORK: TRANSIENT,
    RATE_LIMIT: TRANSIENT,
    ENDPOINT_UNAVAILABLE: TRANSIENT,
    TLS: TRANSIENT,
    MALFORMED_OUTPUT: TRANSIENT,
    SCHEMA_INVALID: TRANSIENT,
    PROVIDER_INTERNAL: TRANSIENT,
    QUOTA_EXHAUSTED: QUOTA,
    AUTHENTICATION: PERMANENT,
    MISSING_SECRET: PERMANENT,
    INVALID_SECRET: PERMANENT,
    PAYMENT_REQUIRED: PERMANENT,
    EXECUTABLE: PERMANENT,
    CONFIGURATION: PERMANENT,
    MODEL_UNAVAILABLE: PERMANENT,
    MALFORMED_ENDPOINT: PERMANENT,
    UNSUPPORTED_API: PERMANENT,
    UNKNOWN: TRANSIENT,
}

# Ordered (category, markers) — checked top to bottom; the first match wins. More specific /
# higher-severity categories come first so e.g. an auth 403 is not mislabelled "network".
_CATEGORY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (EXECUTABLE, ("executable", "not found on path", "not runnable", "no such file",
                  "enoent", "command not found")),
    (PAYMENT_REQUIRED, ("payment required", "payment_required", "insufficient credit",
                        "insufficient_quota", "insufficient funds", "billing", "add credit",
                        "402", "credit balance")),
    (QUOTA_EXHAUSTED, ("quota exceeded", "quota_exceeded", "quota exhausted", "out of quota",
                       "daily limit", "usage limit", "terminalquota", "resource exhausted",
                       "resource_exhausted")),
    (MISSING_SECRET, ("missing api key", "api key not", "no api key", "api_key not set",
                      "credential not set", "missing credential")),
    (INVALID_SECRET, ("invalid api key", "invalid_api_key", "incorrect api key",
                      "api key invalid")),
    (AUTHENTICATION, ("unauthor", "authentication", "permission denied", "forbidden",
                      "401", "403", "credential")),
    (RATE_LIMIT, ("rate limit", "rate_limit", "ratelimit", "429", "too many requests")),
    (TLS, ("tls", "ssl", "certificate", "cert verify", "handshake", "self-signed")),
    (TIMEOUT, ("timeout", "timed out", "deadline exceeded")),
    (MODEL_UNAVAILABLE, ("model not found", "no such model", "unknown model", "model_not_found",
                         "model does not exist", "unsupported model")),
    (UNSUPPORTED_API, ("unsupported api", "not supported", "unexpected response shape",
                       "unsupported response", "incompatible api")),
    (ENDPOINT_UNAVAILABLE, ("endpoint unavailable", "service unavailable", "503", "502",
                            "bad gateway", "no healthy upstream")),
    (NETWORK, ("connection", "network", "dns", "reset by peer", "unreachable",
               "temporarily", "unavailable", "500")),
    (PROVIDER_INTERNAL, ("internal server error", "internal error", "server_error")),
    (SCHEMA_INVALID, ("schema", "validation error", "validationerror", "missing field",
                      "field required", "decision invalid", "invalid decision")),
    (MALFORMED_OUTPUT, ("malformed", "no json", "not valid json", "invalid json",
                        "could not parse", "expecting value", "no decision json",
                        "non-json", "empty response")),
    (MALFORMED_ENDPOINT, ("malformed endpoint", "invalid url", "could not parse url",
                          "no host", "invalid base_url")),
    (CONFIGURATION, ("missing required configuration", "missing configuration", "not configured",
                     "unknown provider", "not a known", "no implementation", "no model",
                     "no endpoint", "base_url", "unsupported provider", "invalid canonical",
                     "invalid state path")),
)


def category(exc: Exception | None, error_text: str = "") -> str:
    """Return the fine-grained failure category for a coordinator/coding failure."""
    # Configuration errors are configuration by type, regardless of message.
    try:
        from aithernet.coding_agent.contracts import CodingAgentConfigurationError
        from aithernet.coordinator.contracts import CoordinatorConfigurationError
        if isinstance(exc, (CoordinatorConfigurationError, CodingAgentConfigurationError)):
            return CONFIGURATION
    except Exception:  # noqa: BLE001 — contracts import must never break classification
        pass
    # A bare asyncio.TimeoutError carries no useful message — recognise it by type.
    if isinstance(exc, TimeoutError):
        return TIMEOUT

    text = (error_text or (str(exc) if exc else "")).lower()
    for cat, markers in _CATEGORY_MARKERS:
        if any(m in text for m in markers):
            return cat
    return UNKNOWN


def disposition_for(cat: str) -> str:
    """PERMANENT or TRANSIENT for a fine-grained category."""
    return CATEGORY_DISPOSITION.get(cat, TRANSIENT)


def classify(exc: Exception | None, error_text: str = "") -> str:
    """Return PERMANENT or TRANSIENT for a coordinator/coding failure (back-compat wrapper)."""
    return disposition_for(category(exc, error_text))
