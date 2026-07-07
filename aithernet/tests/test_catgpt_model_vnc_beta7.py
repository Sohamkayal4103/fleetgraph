"""beta.7 — CatGPT managed-model auto-persistence (FIX 1), provider-status clarity (FIX 2), and
the noVNC password/open UX (FIX 3). All self-contained (no Docker, isolated state root)."""
from __future__ import annotations

import aithernet.agents.providers as prov
from aithernet.catgpt.manager import CatGptGatewayManager, GatewayConfig


class _NoDocker:
    """A docker runner that reports Docker absent (setup/status never shell out)."""

    def available(self) -> bool:
        return False

    def accessible(self) -> bool:
        return False

    def image_present(self, tag: str, *, timeout: float = 20.0) -> bool:
        return False


def _configure_managed(tmp_path, model: str = "") -> None:
    prov.configure(prov.COORDINATOR, "catgpt_gateway",
                   base_url="http://127.0.0.1:8000/v1", api_key_ref="CATGPT_GATEWAY_API_KEY",
                   model=model, state_root=tmp_path)


# ── FIX 1: auto model persistence ────────────────────────────────────────────
def test_single_discovered_model_is_persisted(tmp_path, monkeypatch):
    _configure_managed(tmp_path)
    monkeypatch.setattr(prov, "discover_models", lambda *a, **k: ["catgpt-browser"])
    r = prov.ensure_catgpt_coordinator_model(tmp_path)
    assert r["persisted"] is True and r["model"] == "catgpt-browser"
    cfg = prov._role_provider_config(prov.COORDINATOR, tmp_path)
    assert getattr(cfg, "model", None) == "catgpt-browser"  # persisted to node config


def test_multiple_models_require_explicit_choice(tmp_path, monkeypatch):
    _configure_managed(tmp_path)
    monkeypatch.setattr(prov, "discover_models", lambda *a, **k: ["catgpt-browser", "other"])
    r = prov.ensure_catgpt_coordinator_model(tmp_path)
    assert r["persisted"] is False and r["reason"] == "multiple_models"
    assert prov._role_provider_config(prov.COORDINATOR, tmp_path).model in (None, "")
    # an explicit choice persists
    r2 = prov.ensure_catgpt_coordinator_model(tmp_path, chosen="other")
    assert r2["persisted"] and r2["model"] == "other"


def test_no_model_discovered_persists_nothing(tmp_path, monkeypatch):
    _configure_managed(tmp_path)
    monkeypatch.setattr(prov, "discover_models", lambda *a, **k: [])
    r = prov.ensure_catgpt_coordinator_model(tmp_path)
    assert r["persisted"] is False and r["reason"] == "no_model_discovered"


def test_already_configured_is_a_noop(tmp_path, monkeypatch):
    _configure_managed(tmp_path, model="catgpt-browser")
    monkeypatch.setattr(prov, "discover_models",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not discover")))
    r = prov.ensure_catgpt_coordinator_model(tmp_path)
    assert r["persisted"] is False and r["reason"] == "already_configured"


def test_non_managed_coordinator_is_left_alone(tmp_path):
    prov.configure(prov.COORDINATOR, "gemini_cli", executable="gemini", state_root=tmp_path)
    r = prov.ensure_catgpt_coordinator_model(tmp_path)
    assert r["managed"] is False and r["persisted"] is False


# ── FIX 2: provider-status clarity ───────────────────────────────────────────
def test_provider_status_distinguishes_configured_vs_discovered(tmp_path, monkeypatch):
    _configure_managed(tmp_path)  # no model configured yet
    monkeypatch.setattr(prov, "_catgpt_managed_status",
                        lambda sr=None: (True, "ready", "catgpt-browser"))
    monkeypatch.setattr(prov, "_endpoint_reachable", lambda *a, **k: True)
    s = prov.status(prov.COORDINATOR, state_root=tmp_path)
    assert s["configured_model"] is None
    assert s["discovered_gateway_model"] == "catgpt-browser"
    assert s["effective_model"] == "catgpt-browser"
    assert s["managed_gateway"] is True
    assert s["repair_available"] is True
    assert s["repair_action"]  # a concrete command
    # never over-claim mission_ready without an effective CONFIGURED model
    assert s["readiness"]["mission_ready"] is False


def test_provider_status_mission_ready_needs_configured_model(tmp_path, monkeypatch):
    _configure_managed(tmp_path, model="catgpt-browser")
    monkeypatch.setattr(prov, "_catgpt_managed_status",
                        lambda sr=None: (True, "ready", "catgpt-browser"))
    monkeypatch.setattr(prov, "_endpoint_reachable", lambda *a, **k: True)
    s = prov.status(prov.COORDINATOR, state_root=tmp_path)
    assert s["configured_model"] == "catgpt-browser"
    assert s["repair_available"] is False  # already configured → nothing to repair


# ── FIX 3: noVNC password / open ─────────────────────────────────────────────
def test_novnc_open_url_uses_vnc_html_autoconnect():
    cfg = GatewayConfig(api_host="127.0.0.1", novnc_port=6080)
    assert cfg.novnc_open_url == "http://127.0.0.1:6080/vnc.html?autoconnect=1"


def test_read_vnc_password_returns_only_the_password(tmp_path):
    secret = tmp_path / "aithernet.env"
    mgr = CatGptGatewayManager(state_root=tmp_path, runner=_NoDocker(), secret_env_file=secret)
    summary = mgr.setup(provider="chatgpt", vnc_password="local-vnc-pw-123",
                        configure_coordinator=False)
    assert mgr.read_vnc_password() == "local-vnc-pw-123"
    # the value is NEVER surfaced in setup output
    assert "local-vnc-pw-123" not in str(summary)
    assert summary["novnc_open_url"].endswith("/vnc.html?autoconnect=1")


def test_vnc_password_is_not_the_api_token(tmp_path):
    secret = tmp_path / "aithernet.env"
    mgr = CatGptGatewayManager(state_root=tmp_path, runner=_NoDocker(), secret_env_file=secret)
    mgr.setup(provider="chatgpt", api_token="api-token-XYZ", vnc_password="vnc-pw-ABC",
              configure_coordinator=False)
    assert mgr.read_vnc_password() == "vnc-pw-ABC"
    assert mgr.read_vnc_password() != "api-token-XYZ"
    # the compose/config never inline the VNC password or the API token
    compose = mgr.compose_path.read_text()
    assert "vnc-pw-ABC" not in compose and "api-token-XYZ" not in compose
