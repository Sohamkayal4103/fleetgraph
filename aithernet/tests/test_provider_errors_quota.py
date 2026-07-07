"""beta.10 Defect 3 + 4: normalized redacted provider errors, and quota handling/fallback.

Defect 3: provider failures normalize to a structured, secret-free record (category, retry-after,
user action, redacted excerpt, diagnostic digest). Defect 4: quota exhaustion is a distinct,
recoverable category that parks the mission in `blocked_provider_quota` WITHOUT burning the
consecutive-failure budget or being mislabeled `resource_budget_exhausted`, and names any
configured fallback coordinator.
"""

from __future__ import annotations

import asyncio
import types

from aithernet.coordinator.contracts import CoordinatorProviderError
from aithernet.coordinator.provider_errors import normalize_provider_error
from aithernet.missions import failure_classification as fc
from aithernet.missions.engine import IterationOutcome, MissionEngine

# -- Defect 3: normalized, redacted provider-error record -------------------------------------

def test_normalize_redacts_secrets_and_strips_host_query():
    rec = normalize_provider_error(
        provider="openai_compatible", model="glm-4.6",
        host="api.z.ai?access_token=sk-supersecretvalue",
        status_code=401, body="Authorization: Bearer sk-abcdef123456 was rejected",
    )
    blob = str(rec.to_dict())
    assert "sk-abcdef123456" not in blob and "sk-supersecretvalue" not in blob
    assert "access_token" not in rec.host  # query string with secret stripped from host
    assert rec.host == "api.z.ai"
    assert rec.category == fc.AUTHENTICATION and rec.disposition == fc.PERMANENT
    assert rec.retryable is False
    assert rec.diagnostic_digest.startswith("sha256:")
    assert "set-secret" in rec.action or "credential" in rec.action


def test_normalize_extracts_retry_after_and_quota():
    rec = normalize_provider_error(
        provider="gemini_cli", model="gemini-2.0", status_code=429,
        retry_after="42", body="RESOURCE_EXHAUSTED: quota exceeded for the day",
    )
    assert rec.category == fc.QUOTA_EXHAUSTED
    assert rec.disposition == fc.QUOTA
    assert rec.retry_after_seconds == 42


def test_payment_and_rate_limit_are_distinct_from_quota():
    pay = normalize_provider_error(provider="openai_compatible", status_code=402,
                                   body="insufficient credit balance")
    assert pay.category == fc.PAYMENT_REQUIRED and pay.disposition == fc.PERMANENT
    rl = normalize_provider_error(provider="openai_compatible", status_code=429,
                                  body="too many requests, slow down")
    assert rl.category == fc.RATE_LIMIT and rl.disposition == fc.TRANSIENT


def test_model_and_tls_categories():
    m = normalize_provider_error(provider="openai_compatible", status_code=404,
                                 body="model not found: glm-9")
    assert m.category == fc.MODEL_UNAVAILABLE and m.retryable is False
    t = normalize_provider_error(provider="openai_compatible",
                                 body="SSL: CERTIFICATE_VERIFY_FAILED self-signed certificate")
    assert t.category == fc.TLS


# -- Defect 4: quota classification + recoverable engine state --------------------------------

def test_quota_disposition_is_distinct():
    assert fc.disposition_for(fc.QUOTA_EXHAUSTED) == fc.QUOTA
    assert fc.disposition_for(fc.RATE_LIMIT) == fc.TRANSIENT
    assert fc.category(None, "TerminalQuotaError: quota exhausted") == fc.QUOTA_EXHAUSTED


def _quota_fake(fallbacks):
    fake = types.SimpleNamespace()
    fake.runtime = types.SimpleNamespace(
        coordinator=types.SimpleNamespace(
            config=types.SimpleNamespace(provider="gemini_cli", fallback_providers=fallbacks)))
    fake.persisted = []
    fake.transitions = []
    fake.events = []
    fake.update_calls = []
    fake._persist_control_step = lambda *a, **k: fake.persisted.append(k)
    fake._apply_transition = lambda *a, **k: fake.transitions.append((a, k))
    fake._update_run = lambda *a, **k: fake.update_calls.append(k)

    async def _emit(**k):
        fake.events.append(k)

    fake.runtime._emit_event = _emit
    # Exercise the REAL quota handler bound to the fake engine state.
    fake._handle_quota_block = types.MethodType(MissionEngine._handle_quota_block, fake)
    return fake


def test_quota_error_routes_to_recoverable_block_without_budget_burn():
    fake = _quota_fake(["openai_compatible:zai-glm"])
    outcome = asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3},
        "TerminalQuotaError: gemini quota exhausted",
        exc=CoordinatorProviderError("TerminalQuotaError: gemini quota exhausted")))
    assert outcome is IterationOutcome.BLOCKED
    # Recoverable quota block: never incremented the failure budget...
    assert fake.update_calls == []
    # ...and the reason is blocked_provider_quota, not resource_budget_exhausted.
    reason = fake.persisted[0]["reason"]
    assert "blocked_provider_quota" in reason
    assert "resource_budget_exhausted" not in reason
    assert fake.persisted[0]["result_extra"]["failure_category"] == "quota_exhausted"
    # Names the configured fallback coordinator.
    assert "openai_compatible:zai-glm" in reason


def test_quota_block_without_fallback_suggests_connecting_one():
    fake = _quota_fake([])
    asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3},
        "quota exceeded", exc=CoordinatorProviderError("quota exceeded")))
    reason = fake.persisted[0]["reason"]
    assert "agents connect coordinator" in reason
    assert fake.persisted[0]["result_extra"]["fallback_providers"] == []
