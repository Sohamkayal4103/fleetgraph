"""Provider->service propagation + live-readiness semantics (beta.8 phase 4).

A non-live config/executable probe must never be recorded as live auth verification; only a real
--live test grants end-to-end (mission) readiness. Configured providers must reach the config the
runtime loads.
"""

from __future__ import annotations

from aithernet.agents import providers as agents
from aithernet.config.loader import load_config
from aithernet.coordinator.providers.gemini_api import GeminiAPIProvider


def _configure(tmp_path):
    agents.configure("coordinator", "gemini_api", model="gemini-2.0-flash",
                     api_key_ref="GEMINI_API_KEY", state_root=tmp_path)


def test_nonlive_probe_does_not_grant_live_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _configure(tmp_path)
    r = agents.test("coordinator", live=False, state_root=tmp_path)
    assert r["ready"] is True and r["live"] is False
    st = agents.status("coordinator", state_root=tmp_path)
    assert st["readiness"]["live_verified"] is False
    assert st["auth_readiness"] != "live-verified"
    assert st["last_test"]["live"] is False


def test_live_pass_grants_live_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _configure(tmp_path)

    async def fake_hc(self):
        return {"provider": "gemini_api", "model": "gemini-2.0-flash", "ok": True}

    monkeypatch.setattr(GeminiAPIProvider, "healthcheck", fake_hc)
    r = agents.test("coordinator", live=True, state_root=tmp_path)
    assert r["ready"] is True and r["live"] is True
    st = agents.status("coordinator", state_root=tmp_path)
    assert st["readiness"]["live_verified"] is True
    assert st["auth_readiness"] == "live-verified"
    assert st["last_test"]["live"] is True


def test_readiness_object_states(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    st0 = agents.status("coordinator", state_root=tmp_path)
    assert st0["readiness"]["selected"] is False  # unconfigured
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _configure(tmp_path)
    st = agents.status("coordinator", state_root=tmp_path)
    assert st["readiness"]["selected"] is True
    assert st["readiness"]["credential_reference"] is True
    assert st["readiness"]["credential_available"] is False  # env var not set
    assert st["readiness"]["mission_ready"] is False          # no live test yet


def test_configure_reaches_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    _configure(tmp_path)
    cfg = load_config(tmp_path / "config" / "node.yaml")  # exactly what the runtime loads
    assert cfg.coordinator.provider == "gemini_api"
    assert cfg.coordinator.model == "gemini-2.0-flash"
