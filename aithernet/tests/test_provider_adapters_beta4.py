"""beta.4 adapter-level tests: Claude CLI coordinator, cloud adapters, observability records.

Deterministic (no live providers, no subprocess). Verifies the Claude CLI coordinator's safe
envelope handling (tool-use rejection, error rejection, text extraction), the cloud adapters'
endpoint/auth construction + readiness, and that observability records are credential-free.
"""

from __future__ import annotations

import pytest

from aithernet.agents.observability import ProviderInvocation, quota_event_record
from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import CoordinatorProviderError
from aithernet.coordinator.providers.claude_cli import ClaudeCLIProvider
from aithernet.coordinator.providers.cloud import (
    AnthropicBedrockProvider,
    AnthropicVertexProvider,
    GeminiVertexProvider,
)

# -- Claude CLI coordinator (no subprocess) --------------------------------------------------


def _claude(**kw) -> ClaudeCLIProvider:
    return ClaudeCLIProvider(CoordinatorConfig(provider="claude_cli", executable="claude", **kw))


def test_claude_cli_rejects_tool_use():
    p = _claude()
    with pytest.raises(CoordinatorProviderError, match="used a tool"):
        p._parse_envelope(b'{"result": "{}", "num_tool_uses": 1}')


def test_claude_cli_rejects_error_result():
    p = _claude()
    with pytest.raises(CoordinatorProviderError, match="error"):
        p._parse_envelope(b'{"result": "x", "is_error": true}')


def test_claude_cli_extracts_assistant_text():
    p = _claude()
    env = p._parse_envelope(b'{"result": "{\\"summary\\": \\"ok\\"}", "num_tool_uses": 0}')
    assert p._assistant_text(env) == '{"summary": "ok"}'


def test_claude_cli_extracts_content_block_array():
    p = _claude()
    env = {"content": [{"text": "{\"a\":"}, {"text": "1}"}]}
    assert p._assistant_text(env) == '{"a":1}'


def test_claude_cli_rejects_non_json_envelope():
    p = _claude()
    with pytest.raises(CoordinatorProviderError, match="JSON envelope"):
        p._parse_envelope(b"not json at all")


def test_claude_cli_not_ready_without_executable():
    p = ClaudeCLIProvider(CoordinatorConfig(provider="claude_cli",
                                            executable="definitely-not-a-real-binary-xyz"))
    assert "executable" in p.check_ready()


def test_claude_cli_runtime_dir_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    d = _claude().runtime_dir()
    assert d.is_dir()
    assert str(tmp_path) in str(d) and "coordinator-runtime" in str(d)


# -- cloud adapters --------------------------------------------------------------------------


def test_bedrock_endpoint_and_auth():
    p = AnthropicBedrockProvider(CoordinatorConfig(
        provider="anthropic_bedrock", model="anthropic.claude-3-5-sonnet", region="us-east-1",
        api_key="bedrock-token"))
    assert p.check_ready() == []
    ep = p._endpoint()
    assert ep == ("https://bedrock-runtime.us-east-1.amazonaws.com"
                  "/model/anthropic.claude-3-5-sonnet/invoke")
    assert p._headers()["Authorization"] == "Bearer bedrock-token"


def test_bedrock_missing_region_and_credential():
    p = AnthropicBedrockProvider(CoordinatorConfig(provider="anthropic_bedrock", model="m"))
    assert set(p.check_ready()) == {"region", "api_key"}


def test_vertex_claude_endpoint():
    p = AnthropicVertexProvider(CoordinatorConfig(
        provider="anthropic_vertex", model="claude-3-5-sonnet", project="proj-1",
        region="us-east5", api_key="gcp-token"))
    assert p.check_ready() == []
    assert ("https://us-east5-aiplatform.googleapis.com/v1/projects/proj-1/locations/us-east5/"
            "publishers/anthropic/models/claude-3-5-sonnet:rawPredict") == p._endpoint()
    # body() injects the Vertex anthropic_version and drops the model (model is in the URL).
    b = p._body(_payload())
    assert b.get("anthropic_version") == "vertex-2023-10-16" and "model" not in b


def test_gemini_vertex_endpoint_and_readiness():
    p = GeminiVertexProvider(CoordinatorConfig(
        provider="gemini_vertex", model="gemini-2.0-flash", project="proj-9",
        region="us-central1", api_key="gcp-token"))
    assert p.check_ready() == []
    assert ("https://us-central1-aiplatform.googleapis.com/v1/projects/proj-9/locations/"
            "us-central1/publishers/google/models/gemini-2.0-flash:generateContent") == p._endpoint()
    p2 = GeminiVertexProvider(CoordinatorConfig(provider="gemini_vertex", model="gemini-2.0-flash"))
    assert set(p2.check_ready()) == {"project", "region", "api_key"}


def _payload():
    """A minimal real CoordinatorInput for body construction."""
    from datetime import UTC, datetime

    from aithernet.coordinator.contracts import CoordinatorInput
    return CoordinatorInput(
        mission_id="m-1", mission_content="do x", mission_source_type="user",
        mission_status="received", current_time=datetime.now(UTC))


# -- observability ---------------------------------------------------------------------------


def test_provider_invocation_record_is_secret_free():
    inv = ProviderInvocation(
        invocation_id="inv-1", role="coordinator", provider="claude_cli", adapter="cli",
        auth_category="subscription", capacity_pool_id="claude_subscription:abc",
        requested_model="claude", effective_model="claude-x", input_tokens=10, output_tokens=20,
        finish_reason="stop", mission_id="m-1", mission_result="completed",
        extra={"api_key": "sk-should-be-dropped", "note": "kept"})
    rec = inv.to_record()
    assert rec["record_type"] == "provider_invocation"
    assert rec["auth_category"] == "subscription"
    assert rec["capacity_pool_id"] == "claude_subscription:abc"
    assert "api_key" not in rec and "sk-should-be-dropped" not in str(rec)
    assert rec["note"] == "kept"
    # Unavailable usage stays None (never fabricated).
    assert rec["cached_tokens"] is None and rec["reasoning_tokens"] is None


def test_quota_event_record():
    rec = quota_event_record(role="coding", provider="codex_cli",
                             capacity_pool_id="chatgpt_codex_subscription:x",
                             category="quota_exhausted", retry_after_seconds=120,
                             limit_scope="session")
    assert rec["record_type"] == "provider_quota_event"
    assert rec["category"] == "quota_exhausted" and rec["retry_after_seconds"] == 120
