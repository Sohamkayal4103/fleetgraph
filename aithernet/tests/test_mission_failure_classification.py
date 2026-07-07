"""Mission failure classification + permanent-config blocking (beta.8 phase 5).

Permanent provider configuration/auth errors must block after ONE attempt (named, not relabeled as
budget exhaustion); transient errors keep bounded retry.
"""

from __future__ import annotations

import asyncio
import types

from aithernet.coding_agent.contracts import CodingAgentConfigurationError
from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorProviderError,
)
from aithernet.missions.engine import IterationOutcome, MissionEngine
from aithernet.missions.failure_classification import PERMANENT, TRANSIENT, classify


def test_configuration_errors_are_permanent():
    assert classify(CoordinatorConfigurationError("missing model")) == PERMANENT
    assert classify(CodingAgentConfigurationError("no executable")) == PERMANENT


def test_transient_markers_win():
    assert classify(CoordinatorProviderError("connection timed out")) == TRANSIENT
    assert classify(CoordinatorProviderError("HTTP 503 unavailable")) == TRANSIENT
    assert classify(None, "rate limit exceeded (429)") == TRANSIENT


def test_text_only_permanent_markers():
    assert classify(None, "provider is missing required configuration: model") == PERMANENT
    assert classify(None, "executable not found on PATH") == PERMANENT
    assert classify(None, "unknown provider 'foo'") == PERMANENT


def test_unknown_defaults_transient():
    assert classify(None, "some unexpected hiccup") == TRANSIENT
    assert classify(None, "") == TRANSIENT


class _FakeRun:
    consecutive_failures = 0
    coordinator_call_count = 0


def _fake_engine():
    fake = types.SimpleNamespace()
    fake.runtime = types.SimpleNamespace(
        coordinator=types.SimpleNamespace(config=types.SimpleNamespace(provider="gemini_api")))
    fake._permanent_called = []
    fake._transient_persisted = []

    async def _perm(run_id, mission_id, error, *, failure_category="configuration"):
        fake._permanent_called.append(error)
        fake._permanent_category = failure_category
        return IterationOutcome.BLOCKED

    fake._handle_permanent_block = _perm
    fake._get_run = lambda run_id: _FakeRun()
    fake._persist_control_step = lambda *a, **k: fake._transient_persisted.append(k.get("reason"))
    fake._update_run = lambda *a, **k: None

    async def _budget(run_id, mission_id, reason):
        fake._transient_persisted.append(reason)
        return IterationOutcome.BLOCKED

    fake._handle_budget_block = _budget

    fake._backoffs = []

    async def _backoff(budgets, failures, mission_id, cat):
        fake._backoffs.append((failures, cat))

    fake._coordinator_retry_backoff = _backoff
    return fake


def test_permanent_config_error_blocks_after_one_attempt():
    fake = _fake_engine()
    outcome = asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3},
        "Gemini API provider is missing required configuration: api_key",
        exc=CoordinatorConfigurationError("missing api_key")))
    assert outcome is IterationOutcome.BLOCKED
    assert len(fake._permanent_called) == 1            # exactly one attempt, then blocked
    assert not fake._transient_persisted               # never entered the budget/retry path


def test_transient_error_uses_bounded_retry_path():
    fake = _fake_engine()
    outcome = asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3},
        "connection reset by peer", exc=CoordinatorProviderError("connection reset by peer")))
    # transient: NOT a permanent block; continues (1 of 3 failures) without budget block yet
    assert not fake._permanent_called
    assert outcome is IterationOutcome.CONTINUED
    # and it spaced the retry with bounded backoff rather than spinning immediately
    assert fake._backoffs == [(1, "network")]


# -- beta.9 Defect 3: fine-grained categories + disposition + backoff ------------------------

def test_fine_grained_categories():
    from aithernet.missions import failure_classification as fc
    assert fc.category(None, "request timed out after 60s") == fc.TIMEOUT
    assert fc.category(None, "HTTP 401 unauthorized") == fc.AUTHENTICATION
    assert fc.category(None, "rate limit exceeded (429)") == fc.RATE_LIMIT
    assert fc.category(None, "connection reset by peer") == fc.NETWORK
    assert fc.category(None, "could not parse: expecting value") == fc.MALFORMED_OUTPUT
    assert fc.category(None, "decision invalid: field required") == fc.SCHEMA_INVALID
    assert fc.category(None, "executable not found on PATH") == fc.EXECUTABLE
    assert fc.category(None, "missing required configuration: model") == fc.CONFIGURATION
    assert fc.category(None, "some unexpected hiccup") == fc.UNKNOWN


def test_category_disposition_keeps_budget_for_provider_config():
    from aithernet.missions import failure_classification as fc
    # auth / executable / configuration are permanent — they must NOT burn the failure budget
    for cat in (fc.AUTHENTICATION, fc.EXECUTABLE, fc.CONFIGURATION):
        assert fc.disposition_for(cat) == fc.PERMANENT
    # timeouts / network / rate-limit / malformed / schema are recoverable transients
    for cat in (fc.TIMEOUT, fc.NETWORK, fc.RATE_LIMIT, fc.MALFORMED_OUTPUT, fc.SCHEMA_INVALID):
        assert fc.disposition_for(cat) == fc.TRANSIENT


def test_bare_timeout_error_is_timeout_transient():
    from aithernet.missions import failure_classification as fc
    assert fc.category(TimeoutError(), "") == fc.TIMEOUT
    assert fc.classify(TimeoutError(), "") == fc.TRANSIENT


def test_auth_failure_blocks_after_one_attempt_without_budget():
    # A 403 auth rejection is permanent: one attempt, named, never relabeled budget-exhausted.
    fake = _fake_engine()
    outcome = asyncio.run(MissionEngine._handle_coordinator_failure(
        fake, "run1", "m1", {"max_consecutive_failures": 3},
        "HTTP 403 forbidden: invalid api key",
        exc=CoordinatorProviderError("HTTP 403 forbidden: invalid api key")))
    assert outcome is IterationOutcome.BLOCKED
    assert len(fake._permanent_called) == 1
    # beta.10 refines "invalid api key" to the more specific (still PERMANENT) invalid_secret.
    assert fake._permanent_category in ("authentication", "invalid_secret")
    assert not fake._transient_persisted


def test_backoff_is_bounded_and_exponential():
    import asyncio as _aio

    from aithernet.missions.engine import MissionEngine

    slept: list[float] = []

    async def _fake_sleep(d):
        slept.append(d)

    fake = types.SimpleNamespace()
    budgets = {"coordinator_retry_initial_backoff_seconds": 2.0,
               "coordinator_retry_max_backoff_seconds": 30.0}
    orig = _aio.sleep
    _aio.sleep = _fake_sleep
    try:
        for failures in (1, 2, 3, 4, 10):
            _aio.run(MissionEngine._coordinator_retry_backoff(
                fake, budgets, failures, "m1", "timeout"))
    finally:
        _aio.sleep = orig
    assert slept == [2.0, 4.0, 8.0, 16.0, 30.0]  # exponential, capped at 30s


def test_zero_backoff_disables_sleep():
    import asyncio as _aio

    from aithernet.missions.engine import MissionEngine

    slept: list[float] = []

    async def _fake_sleep(d):  # pragma: no cover - must never be called
        slept.append(d)

    fake = types.SimpleNamespace()
    orig = _aio.sleep
    _aio.sleep = _fake_sleep
    try:
        _aio.run(MissionEngine._coordinator_retry_backoff(
            fake, {"coordinator_retry_initial_backoff_seconds": 0}, 3, "m1", "timeout"))
    finally:
        _aio.sleep = orig
    assert slept == []


def test_coordinator_timeout_default_exceeds_60s():
    from aithernet.config.settings import CoordinatorConfig
    assert CoordinatorConfig().timeout_seconds > 60.0
