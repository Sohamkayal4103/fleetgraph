"""beta.10 Defect 2: OpenAI-compatible guided configuration + tested profiles.

Proves a base URL + model are required and URL-validated before saving, that switching providers
does not leave stale config, that the Z.AI tested profile exposes both general + coding-plan base
URLs, and that the guided flow never suggests GEMINI_API_KEY for a non-Gemini provider.
"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from aithernet.agents import providers as prov
from aithernet.cli import app


def _node_yaml(state_root):
    return yaml.safe_load((state_root / "config" / "node.yaml").read_text())


def test_openai_compatible_requires_base_url_and_model(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="base-url"):
        prov.configure("coordinator", "openai_compatible", model="m", state_root=tmp_path)
    with pytest.raises(ValueError, match="model"):
        prov.configure("coordinator", "openai_compatible",
                       base_url="https://api.z.ai/api/paas/v4/", state_root=tmp_path)


def test_base_url_format_is_validated(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="http"):
        prov.configure("coordinator", "openai_compatible", base_url="not-a-url",
                       model="glm-4.6", state_root=tmp_path)
    prov.validate_base_url("https://api.z.ai/api/paas/v4/")  # no raise
    with pytest.raises(ValueError):
        prov.validate_base_url("ftp://x/y")


def test_configure_persists_and_switch_clears_stale(tmp_path):
    prov.configure("coordinator", "openai_compatible", base_url="https://api.z.ai/api/paas/v4/",
                   model="glm-4.6", api_key_ref="ZAI_API_KEY", state_root=tmp_path)
    sec = _node_yaml(tmp_path)["coordinator"]
    assert sec["base_url"] == "https://api.z.ai/api/paas/v4/"
    assert sec["model"] == "glm-4.6"
    assert sec["api_key"] == "env:ZAI_API_KEY"
    # Switching to a local endpoint must replace the section (no stale hosted key/url left behind).
    prov.configure("coordinator", "openai_compatible_local", base_url="http://127.0.0.1:9099",
                   model="stub-1", state_root=tmp_path)
    sec2 = _node_yaml(tmp_path)["coordinator"]
    assert sec2["provider"] == "openai_compatible_local"
    assert "api_key" not in sec2  # stale hosted credential reference is gone
    assert sec2["allow_unauthenticated"] is True


def test_zai_profile_has_both_base_urls():
    p = prov.OPENAI_COMPATIBLE_PROFILES["zai-glm"]
    assert p["base_url"] == "https://api.z.ai/api/paas/v4/"
    assert p["coding_base_url"] == "https://api.z.ai/api/coding/paas/v4/"
    assert p["suggested_secret"] == "ZAI_API_KEY"


def test_suggested_secret_name_is_provider_appropriate():
    assert prov.suggested_secret_name("openai_compatible") == "OPENAI_COMPATIBLE_API_KEY"
    assert prov.suggested_secret_name("anthropic") == "ANTHROPIC_API_KEY"
    assert prov.suggested_secret_name("gemini_api") == "GEMINI_API_KEY"
    # Never Gemini for a non-Gemini provider.
    assert prov.suggested_secret_name("openai_compatible") != "GEMINI_API_KEY"


def test_connect_openai_compatible_noninteractive(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    from aithernet.provisioning import services
    monkeypatch.setattr(services, "installed_scope", lambda: None)
    runner = CliRunner()
    out = runner.invoke(app, [
        "agents", "connect", "coordinator", "--provider", "openai_compatible",
        "--base-url", "https://api.z.ai/api/paas/v4/", "--model", "glm-4.6",
        "--api-key-ref", "ZAI_API_KEY", "--no-verify", "--no-restart"])
    assert out.exit_code == 0, out.output
    sec = _node_yaml(tmp_path)["coordinator"]
    assert sec["base_url"] == "https://api.z.ai/api/paas/v4/" and sec["model"] == "glm-4.6"
    assert sec["api_key"] == "env:ZAI_API_KEY"


def test_connect_anthropic_without_key_does_not_suggest_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    runner = CliRunner()
    out = runner.invoke(app, ["agents", "connect", "coordinator", "--provider", "anthropic"])
    # Exits asking for a key; the suggestion must be ANTHROPIC_API_KEY, never GEMINI_API_KEY.
    assert "ANTHROPIC_API_KEY" in out.output
    assert "GEMINI_API_KEY" not in out.output
