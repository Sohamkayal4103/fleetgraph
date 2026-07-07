"""beta.9 Defect 5: readiness is consistent and truthful across surfaces.

* doctor labels a live-verified coordinator READY (not UNVERIFIED) — it previously only matched
  the string "ready" while providers report "live-verified".
* setup qualification never claims mission readiness while the coordinator is configured but not
  live-verified; it reports RF-MCP readiness separately from coordinator/mission readiness.
"""

from __future__ import annotations

from types import SimpleNamespace

from aithernet import doctor as doc
from aithernet.provisioning import wizard
from aithernet.provisioning.state import CONFIGURED, DONE


def test_doctor_marks_live_verified_provider_ready(monkeypatch):
    from aithernet.agents import providers as prov

    def fake_status(role, *, state_root=None):
        return {"configured": True, "display": f"{role}-X", "provider": "gemini_api",
                "auth_readiness": "live-verified", "readiness": {"live_verified": True}}

    monkeypatch.setattr(prov, "status", fake_status)
    monkeypatch.setattr(prov, "ROLES", (prov.COORDINATOR,))
    report = doc.DoctorReport()
    doc._check_providers(report)
    check = next(c for c in report.checks if c.name == "coordinator_provider")
    assert check.status == doc.READY
    assert "live-verified" in check.detail


def _qual_ctx(tmp_path):
    state = wizard.load_or_new(tmp_path)
    state.hardware_profile = "software-only"
    # managed_components must read DONE for the RF-MCP gate to pass
    state.record("managed_components", DONE)
    return SimpleNamespace(state=state, state_root=tmp_path)


def test_qualification_not_mission_ready_when_coordinator_unverified(tmp_path, monkeypatch):
    from aithernet.agents import providers as prov
    monkeypatch.setattr(prov, "status", lambda role, *, state_root=None: {
        "provider": "gemini_cli", "readiness": {"live_verified": False, "mission_ready": False}})
    res = wizard._apply_qualification(_qual_ctx(tmp_path))
    assert res.status == CONFIGURED
    assert "not yet verified" in res.detail
    assert "ready for a first bounded receive-only mission" not in res.detail


def test_qualification_mission_ready_when_coordinator_live_verified(tmp_path, monkeypatch):
    from aithernet.agents import providers as prov
    monkeypatch.setattr(prov, "status", lambda role, *, state_root=None: {
        "provider": "gemini_api", "readiness": {"live_verified": True, "mission_ready": True}})
    res = wizard._apply_qualification(_qual_ctx(tmp_path))
    assert res.status == DONE
    assert "ready for a first bounded receive-only mission" in res.detail
    assert "live-verified" in res.detail


def test_qualification_disabled_coordinator_is_software_only(tmp_path, monkeypatch):
    from aithernet.agents import providers as prov
    monkeypatch.setattr(prov, "status", lambda role, *, state_root=None: {
        "provider": prov.DISABLED, "readiness": {}})
    res = wizard._apply_qualification(_qual_ctx(tmp_path))
    assert res.status == DONE
    assert "coordinator disabled" in res.detail
