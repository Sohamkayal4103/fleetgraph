"""Gemini API coordinator provider + secure secret store (beta.8 phase 3).

gemini_api is a first-class API coordinator (distinct from the optional Gemini CLI). Covered with a
bounded fake-server integration test (no external credential); a real bounded test is operator-gated
via the GEMINI_API_KEY env var. Secrets are never persisted in node.yaml/logs/status.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from aithernet.agents import providers as agents
from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import CoordinatorConfigurationError, CoordinatorInput
from aithernet.coordinator.providers import PROVIDERS
from aithernet.coordinator.providers.gemini_api import GeminiAPIProvider, is_supported_model


def _input():
    from datetime import UTC, datetime
    return CoordinatorInput(mission_id="m1", mission_content="say hi", mission_source_type="user",
                            mission_status="received", current_time=datetime.now(UTC))


def test_gemini_api_is_registered_and_first_class():
    assert PROVIDERS.get("gemini_api") is GeminiAPIProvider
    keys = [p.key for p in agents.providers_for("coordinator")]
    assert "gemini_api" in keys and "gemini_cli" in keys
    info = next(p for p in agents.providers_for("coordinator") if p.key == "gemini_api")
    assert info.kind == "api" and "Gemini API" in info.display


def test_model_validation_rules():
    assert is_supported_model("gemini-2.0-flash")
    assert not is_supported_model("gpt-4o")
    assert not is_supported_model(None)
    # check_ready flags a non-gemini model + missing key
    p = GeminiAPIProvider(CoordinatorConfig(provider="gemini_api", model="gpt-4o"))
    missing = p.check_ready()
    assert any("model" in m for m in missing) and "api_key" in missing


def test_missing_config_raises_configuration_error():
    p = GeminiAPIProvider(CoordinatorConfig(provider="gemini_api"))  # no model/key
    with pytest.raises(CoordinatorConfigurationError):
        import asyncio
        asyncio.run(p.decide(_input()))


def test_fake_server_inference(monkeypatch):
    """Bounded fake-server integration: a stub generateContent endpoint returns a JSON decision;
    the provider parses it into a CoordinatorDecision. No external credentials."""
    decision = {"summary": "greet", "next_target": "respond", "action": "acknowledge",
                "message": "done", "expected_result": "greeted",
                "mission_control": {"disposition": "complete", "final_response": "hi"}}

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key_header"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(decision)}]},
                            "finishReason": "STOP"}]})

    transport = httpx.MockTransport(handler)

    # Patch AsyncClient to use the mock transport regardless of verify=.
    real_async = httpx.AsyncClient

    def fake_async(*a, **k):
        k.pop("verify", None)
        k["transport"] = transport
        return real_async(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", fake_async)

    p = GeminiAPIProvider(CoordinatorConfig(
        provider="gemini_api", model="gemini-2.0-flash", api_key="FAKE-KEY"))
    import asyncio
    result = asyncio.run(p.decide(_input()))
    assert result.mission_id == "m1"
    # endpoint shape + key sent as header, never in the URL
    assert "models/gemini-2.0-flash:generateContent" in captured["url"]
    assert "FAKE-KEY" not in captured["url"]
    assert captured["key_header"] == "FAKE-KEY"


def test_status_never_leaks_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    agents.configure("coordinator", "gemini_api", model="gemini-2.0-flash",
                     api_key_ref="GEMINI_API_KEY", state_root=tmp_path)
    # node.yaml records only the env ref, never the value
    raw = (tmp_path / "config" / "node.yaml").read_text()
    assert "super-secret-value" not in raw
    assert "env:GEMINI_API_KEY" in raw
    st = agents.status("coordinator", state_root=tmp_path)
    assert json.dumps(st).find("super-secret-value") == -1
    assert st.get("api_key_ref") == "GEMINI_API_KEY"
    assert st.get("api_key_present") is True


def test_secret_store_roundtrip_no_leak(tmp_path):
    env_file = tmp_path / "aithernet.env"
    agents.set_secret("GEMINI_API_KEY", "abc123", env_file=env_file)
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    assert agents.list_secret_names(env_file=env_file) == ["GEMINI_API_KEY"]
    # rotation replaces, does not duplicate
    agents.set_secret("GEMINI_API_KEY", "rotated", env_file=env_file)
    body = env_file.read_text()
    assert body.count("GEMINI_API_KEY=") == 1 and "rotated" in body
    # removal
    assert agents.remove_secret("GEMINI_API_KEY", env_file=env_file) is True
    assert agents.list_secret_names(env_file=env_file) == []


def test_secret_name_validation(tmp_path):
    with pytest.raises(ValueError):
        agents.set_secret("not-a-valid-name", "x", env_file=tmp_path / "e.env")


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"),
                    reason="operator-gated: set GEMINI_API_KEY for a real bounded Gemini API test")
def test_real_gemini_api_bounded_probe():
    p = GeminiAPIProvider(CoordinatorConfig(
        provider="gemini_api", model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        api_key=os.environ["GEMINI_API_KEY"], max_tokens=16))
    import asyncio
    meta = asyncio.run(p.healthcheck())
    assert meta["ok"] is True and meta["provider"] == "gemini_api"
