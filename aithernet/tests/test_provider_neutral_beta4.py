"""beta.4 provider-neutral agent-runtime: capabilities, capacity pools, fallback, presets, adapters.

Deterministic (no live providers). Verifies the capability spine that makes provider selection
role-aware, the non-secret capacity-pool identity + shared-pool detection, explicit fallback rules,
the recommended presets, the expanded provider catalogue + registry, and the canonical error
vocabulary — including the security invariant that no secret appears in any record.
"""

from __future__ import annotations

import pytest

from aithernet.agents import capabilities as C
from aithernet.agents import capacity as Q
from aithernet.agents import fallback as F
from aithernet.agents import presets as PR
from aithernet.agents import providers as P
from aithernet.coding_agent.providers import build_provider as build_coding
from aithernet.config.settings import CodingAgentConfig, CoordinatorConfig
from aithernet.coordinator.providers import build_provider as build_coord

# -- capability gating -----------------------------------------------------------------------


def test_every_catalogue_provider_satisfies_its_role():
    for info in P.CATALOGUE:
        if info.key == "disabled":
            assert not info.satisfies_role()
        else:
            assert info.satisfies_role(), f"{info.key} does not satisfy role {info.role}"


def test_coding_is_not_inferred_from_reasoning():
    # A coordinator (text/reasoning) is NOT eligible for coding; a coder is not a coordinator.
    assert C.satisfies_role("claude_cli", "coordinator")
    assert not C.satisfies_role("claude_cli", "coding")
    assert C.satisfies_role("codex_cli", "coding")
    assert not C.satisfies_role("codex_cli", "coordinator")


def test_disabled_never_satisfies_a_role():
    assert not C.satisfies_role("disabled", "coordinator")
    assert not C.satisfies_role("disabled", "coding")


def test_auth_and_billing_labels_are_accurate():
    # Subscriptions are subscription-billed; APIs are separately billed; local is local compute.
    assert C.auth_category("claude_cli") == C.AUTH_SUBSCRIPTION
    assert C.billing_model("claude_cli") == C.BILL_SUBSCRIPTION
    assert C.auth_category("anthropic") == C.AUTH_API
    assert C.billing_model("anthropic") == C.BILL_API
    assert C.auth_category("anthropic_bedrock") == C.AUTH_CLOUD
    assert C.billing_model("openai_compatible_local") == C.BILL_LOCAL


def test_catalogue_has_all_named_providers():
    coord = {p.key for p in P.providers_for("coordinator")}
    coding = {p.key for p in P.providers_for("coding")}
    assert {"claude_cli", "anthropic", "anthropic_bedrock", "anthropic_vertex", "openai_api",
            "openai_compatible", "openai_compatible_local", "gemini_api", "gemini_vertex",
            "gemini_cli"} <= coord
    assert {"codex_cli", "codex_api", "claude_code", "local_coding"} <= coding


def test_every_named_provider_resolves_to_a_runtime_class():
    for info in P.providers_for("coordinator"):
        if info.key == "disabled":
            continue
        assert build_coord(CoordinatorConfig(provider=info.key)) is not None, info.key
    for info in P.providers_for("coding"):
        if info.key == "disabled":
            continue
        assert build_coding(CodingAgentConfig(provider=info.key)) is not None, info.key


# -- capacity pools --------------------------------------------------------------------------


def test_shared_pool_when_claude_coordinator_and_claude_code_coding():
    pools = Q.build_pools(
        {"coordinator": {"provider": "claude_cli"}, "coding": {"provider": "claude_code"}},
        runtime_user="op")
    assert len(pools) == 1
    (state,) = pools.values()
    assert state.roles == {"coordinator", "coding"}
    assert state.family == "claude_subscription"


def test_independent_pools_for_provider_diverse():
    pools = Q.build_pools(
        {"coordinator": {"provider": "claude_cli"}, "coding": {"provider": "codex_cli"}},
        runtime_user="op")
    assert len(pools) == 2


def test_pool_id_is_non_secret():
    pid = Q.pool_id("anthropic", api_key_ref="env:ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" not in pid and "key" not in pid.lower()
    assert pid.startswith("anthropic_api:")
    local = Q.pool_id("openai_compatible_local", base_url="http://127.0.0.1:11434/v1")
    assert "127.0.0.1" not in local and local.startswith("local_runtime:")


def test_pool_state_view_is_bounded_and_quota_aware():
    s = Q.PoolState("anthropic_api:abc", "anthropic_api", concurrency_limit=2)
    s.quota_exhausted = True
    s.retry_after_seconds = 60
    assert s.available is False
    v = s.view()
    assert v["quota_exhausted"] is True and v["retry_after_seconds"] == 60


# -- fallback --------------------------------------------------------------------------------


def test_chain_drops_role_incapable_disabled_and_duplicates():
    chain = F.resolve_chain("coordinator", "claude_cli",
                            ["openai_api", "codex_cli", "disabled", "openai_api", ""])
    assert chain == ["claude_cli", "openai_api"]  # codex_cli (coding-only) + disabled + dup removed


def test_fallback_only_on_provider_availability_conditions():
    assert F.should_fallback("quota_exhausted")
    assert F.should_fallback("subscription_unavailable")
    assert not F.should_fallback("malformed_response")
    assert not F.should_fallback("workspace_violation")
    assert not F.should_fallback(None)


def test_fallback_transition_is_recorded_and_not_silent():
    rec = F.FallbackTransition("coordinator", "claude_cli", "openai_api:fast",
                               "quota_exhausted", "inv-9").record()
    assert rec["silent"] is False
    assert rec["from_provider"] == "claude_cli" and rec["to_provider"] == "openai_api"
    assert rec["to_profile"] == "fast"
    assert rec["reason_category"] == "quota_exhausted"


# -- presets ---------------------------------------------------------------------------------


def test_presets_exist_and_are_role_valid():
    names = {p.name for p in PR.list_presets()}
    assert {"reasoning_first", "coding_first", "lowest_cost", "fully_local", "provider_diverse",
            "single_provider_claude"} == names
    for preset in PR.list_presets():
        assert C.satisfies_role(preset.coordinator, "coordinator"), preset.name
        assert C.satisfies_role(preset.coding, "coding"), preset.name


def test_single_provider_claude_is_shared_pool():
    preset = PR.get_preset("single_provider_claude")
    assert preset.shared_pool is True
    assert preset.coordinator == "claude_cli" and preset.coding == "claude_code"
    bs = preset.billing_summary()
    assert bs["coordinator"]["billing"] == C.BILL_SUBSCRIPTION


def test_provider_diverse_is_two_subscriptions_not_shared():
    preset = PR.get_preset("provider_diverse")
    assert preset.shared_pool is False
    assert preset.coordinator == "claude_cli" and preset.coding == "codex_cli"


# -- error vocabulary ------------------------------------------------------------------------


def test_canonical_error_vocabulary_is_complete():
    from aithernet.coordinator import provider_errors as PE
    required = {
        "executable_missing", "unsupported_version", "unauthenticated", "authentication_expired",
        "invalid_credential", "permission_denied", "subscription_unavailable",
        "api_billing_required", "credit_exhausted", "quota_exhausted", "rate_limited",
        "model_unavailable", "model_incompatible", "endpoint_unavailable", "network_failure",
        "tls_failure", "timeout", "cancelled", "malformed_response", "structured_output_invalid",
        "tool_call_invalid", "workspace_violation", "provider_internal_error",
    }
    assert required <= PE.CANONICAL_CATEGORIES


@pytest.mark.parametrize("status,expected", [
    (402, "api_billing_required"), (429, "rate_limited"), (401, "unauthenticated"),
])
def test_http_status_maps_to_canonical(status, expected):
    from aithernet.coordinator import provider_errors as PE
    rec = PE.normalize_provider_error(provider="anthropic", role="coordinator", adapter="api",
                                      status_code=status, body="error")
    assert rec.canonical_category == expected
    assert rec.role == "coordinator" and rec.adapter == "api"


def test_runtime_categories_via_override():
    from aithernet.coordinator import provider_errors as PE
    rec = PE.normalize_provider_error(provider="codex_cli", role="coding", adapter="cli",
                                      category_override="workspace_violation",
                                      body="attempted to write outside workspace")
    assert rec.canonical_category == "workspace_violation"
    assert rec.retryable is False


def test_error_record_never_leaks_secrets():
    from aithernet.coordinator import provider_errors as PE
    leaky = "Authorization: Bearer sk-secret123456 x-api-key: AIzaSECRETKEY1234567890"
    rec = PE.normalize_provider_error(provider="openai_api", role="coordinator", adapter="api",
                                      status_code=401, body=leaky)
    blob = str(rec.to_dict())
    assert "sk-secret123456" not in blob and "AIzaSECRETKEY1234567890" not in blob
