"""beta.9: the customer-facing 'Local OpenAI-compatible' coordinator option is actually runnable —
the catalogue key maps to the registered adapter and configure writes the correct unauth field."""

from __future__ import annotations

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.providers import build_provider
from aithernet.coordinator.providers.openai_compatible import OpenAICompatibleProvider


def test_openai_compatible_local_builds_the_adapter():
    cfg = CoordinatorConfig(provider="openai_compatible_local", model="stub-1",
                            base_url="http://127.0.0.1:9099", allow_unauthenticated=True)
    prov = build_provider(cfg)
    assert isinstance(prov, OpenAICompatibleProvider)
    assert prov.check_ready() == []  # unauthenticated local endpoint is ready without a key


def test_configure_local_writes_allow_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    from aithernet.agents import providers as prov
    prov.configure(prov.COORDINATOR, "openai_compatible_local",
                   base_url="http://127.0.0.1:9099", model="stub-1", state_root=tmp_path)
    import yaml
    data = yaml.safe_load((tmp_path / "config" / "node.yaml").read_text())
    assert data["coordinator"]["allow_unauthenticated"] is True
    assert "allow_missing_api_key" not in data["coordinator"]
