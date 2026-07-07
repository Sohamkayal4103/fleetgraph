"""Setup-mode honesty tests (beta.6 acceptance findings 2A/2B/2C).

The dry-run plan and the executed steps must reflect the SELECTED modes:
  * standalone omits hosted enrollment and makes zero control-plane requests; hosted includes it;
  * offline makes zero APT/network calls and fails clearly when pre-staged OS assets are absent;
    recommended may use validated APT; none skips SDR dependency installation;
  * the signed component bundle directory is propagated to the verifying installer;
  * dry-run and execution resolve the same strategy/steps;
  * personal-workstation state lives under the invoking user, never root.
"""

from __future__ import annotations

import pytest

from aithernet.hardware import sdr
from aithernet.provisioning import state, wizard


def _by_id(plans, step_id):
    return next(p for p in plans if p.step_id == step_id)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """rf-mcp reports not-installed so the components step exercises the bundle path."""
    from aithernet import components as comp
    monkeypatch.setattr(
        comp, "component_status",
        lambda name: comp.ComponentStatus(name=name, installed=False, issues=["not installed"]))


# --- 2A: standalone vs hosted enrollment -----------------------------------
def test_standalone_plan_omits_enrollment():
    plans = wizard.build_plan(state.SetupState(), opts={"deployment_mode": "standalone"})
    enr = _by_id(plans, "hosted_enrollment")
    assert enr.download_sources == []
    assert enr.deferred_to == ""
    assert "skipped" in " ".join(enr.notes).lower()


def test_hosted_plan_includes_enrollment():
    plans = wizard.build_plan(state.SetupState(), opts={"deployment_mode": "hosted"})
    enr = _by_id(plans, "hosted_enrollment")
    assert "hosted control plane (HTTPS)" in enr.download_sources
    assert enr.deferred_to.startswith("aithernet enroll")


def test_standalone_makes_zero_hosted_requests(tmp_path, monkeypatch):
    # If enrollment were attempted in standalone, _top_enroll would run; make that an error.
    import aithernet.hosted.cli as hosted_cli

    def _boom(**_kw):
        raise AssertionError("standalone must not contact the hosted control plane")

    monkeypatch.setattr(hosted_cli, "_top_enroll", _boom, raising=False)
    st = state.SetupState(created_at="t0", deployment_mode="standalone")
    results = wizard.run(st, state_root=tmp_path, opts={"deployment_mode": "standalone"},
                         only=["hosted_enrollment"])
    assert results[0].status == state.SKIPPED


# --- 2B: SDR strategy ------------------------------------------------------
def test_offline_plan_makes_zero_apt_or_network():
    plans = wizard.build_plan(
        state.SetupState(), opts={"profile": "plutosdr", "sdr_strategy": "offline"})
    sdr_plan = _by_id(plans, "sdr_strategy")
    assert sdr_plan.download_sources == []
    assert sdr_plan.privileged_ops == []
    assert not any("apt" in op for op in sdr_plan.privileged_ops)
    assert "offline assets unavailable/incomplete" in " ".join(sdr_plan.notes).lower()


def test_offline_apply_fails_clearly_when_assets_unavailable(tmp_path):
    st = state.SetupState(created_at="t0", hardware_profile="plutosdr")
    res = wizard.run(st, state_root=tmp_path,
                     opts={"sdr_strategy": "offline", "profile": "plutosdr"},
                     only=["sdr_strategy"])[0]
    assert res.status == state.FAILED
    assert "offline assets unavailable/incomplete" in res.detail
    assert "apt-get" not in (res.remediation or "").lower()


def test_offline_with_staged_assets_uses_dpkg_not_apt(tmp_path):
    osp = tmp_path / "bundle" / "os-packages"
    osp.mkdir(parents=True)
    (osp / "gnuradio.deb").write_bytes(b"x")
    st = state.SetupState(created_at="t0", hardware_profile="plutosdr")
    res = wizard.run(st, state_root=tmp_path,
                     opts={"sdr_strategy": "offline", "profile": "plutosdr",
                           "components_bundle_dir": str(tmp_path / "bundle")},
                     only=["sdr_strategy"])[0]
    assert res.status == state.DEFERRED
    assert "dpkg" in (res.remediation or "") and "apt" not in (res.remediation or "")


def test_recommended_plan_uses_validated_apt():
    plans = wizard.build_plan(
        state.SetupState(), opts={"profile": "plutosdr", "sdr_strategy": "recommended"})
    sdr_plan = _by_id(plans, "sdr_strategy")
    assert any("apt-get install" in op for op in sdr_plan.privileged_ops)
    assert any("apt" in s.lower() for s in sdr_plan.download_sources)


def test_none_skips_sdr_install(tmp_path):
    st = state.SetupState(created_at="t0", hardware_profile="plutosdr")
    res = wizard.run(st, state_root=tmp_path,
                     opts={"sdr_strategy": "none", "profile": "plutosdr"},
                     only=["sdr_strategy"])[0]
    assert res.status == state.DONE
    assert "none" in res.detail and "no SDR dependencies" in res.detail


def test_dry_run_and_execution_select_same_offline_strategy(tmp_path):
    opts = {"profile": "plutosdr", "sdr_strategy": "offline"}
    plan = _by_id(wizard.build_plan(state.SetupState(), opts=opts), "sdr_strategy")
    assert not any("apt" in op for op in plan.privileged_ops) and plan.download_sources == []
    st = state.SetupState(created_at="t0")
    results = wizard.run(st, state_root=tmp_path, opts=opts,
                         only=["hardware_profile", "sdr_strategy"])
    res = next(r for r in results if r.step_id == "sdr_strategy")
    # Plan said offline-unavailable; execution agrees (same resolved step graph), never apt.
    assert "offline assets unavailable/incomplete" in " ".join(plan.notes).lower()
    assert res.status == state.FAILED and "apt-get" not in (res.remediation or "").lower()


# --- sdr module: offline never emits apt -----------------------------------
def test_sdr_plan_offline_unavailable_without_staged():
    p = sdr.plan("plutosdr", "offline")
    assert p.offline_unavailable
    assert p.privileged_ops == [] and p.download_sources == []


def test_sdr_plan_offline_uses_dpkg_with_staged(tmp_path):
    osp = tmp_path / "os-packages"
    osp.mkdir()
    (osp / "a.deb").write_bytes(b"x")
    p = sdr.plan("plutosdr", "offline", staged_dir=str(tmp_path))
    assert not p.offline_unavailable
    assert any("dpkg" in op for op in p.privileged_ops)
    assert not any("apt" in op for op in p.privileged_ops)
    assert p.download_sources == []


def test_sdr_plan_recommended_uses_apt():
    p = sdr.plan("plutosdr", "recommended")
    assert any("apt-get install" in op for op in p.privileged_ops)


# --- 2C: signed component bundle -------------------------------------------
def test_component_bundle_dir_propagated_to_installer(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    (bundle / "lock.json").write_text("{}")
    captured = {}
    from aithernet.components import manager

    def _fake_install(name, *, bundle=None, force=False, **_kw):
        captured.update(name=name, bundle=str(bundle), force=force)
        return {"version": "0.1.0+aithernet.2"}

    monkeypatch.setattr(manager, "install", _fake_install)
    st = state.SetupState(created_at="t0")
    res = wizard.run(st, state_root=tmp_path,
                     opts={"components_bundle_dir": str(bundle)},
                     only=["managed_components"])[0]
    assert captured["name"] == "rf-mcp"
    assert captured["bundle"] == str(bundle)
    assert res.status == state.DONE
    assert "signed bundle" in res.detail


def test_component_apply_reports_verification_failure(tmp_path, monkeypatch):
    bundle = tmp_path / "release"
    bundle.mkdir()
    (bundle / "lock.json").write_text("{}")
    from aithernet.components import manager
    from aithernet.components.manager import ComponentError

    def _bad(*_a, **_k):
        raise ComponentError("untrusted_key", "bundle public key does not match the pinned key")

    monkeypatch.setattr(manager, "install", _bad)
    st = state.SetupState(created_at="t0")
    res = wizard.run(st, state_root=tmp_path,
                     opts={"components_bundle_dir": str(bundle)},
                     only=["managed_components"])[0]
    assert res.status == state.FAILED
    assert "bundle install failed" in res.detail


def test_component_plan_states_pinned_key_and_no_clone():
    plans = wizard.build_plan(state.SetupState(), opts={"components_bundle_dir": "/tmp/rel"})
    comp_plan = _by_id(plans, "managed_components")
    text = " ".join(comp_plan.notes).lower()
    assert "pinned" in text
    assert "no git clone" in text and "no network fallback" in text
    assert comp_plan.deferred_to == "aithernet components install rf-mcp --bundle /tmp/rel"


# --- state ownership -------------------------------------------------------
def test_personal_workstation_state_under_invoking_user(monkeypatch, tmp_path):
    fake_home = tmp_path / "home" / "operator"
    fake_home.mkdir(parents=True)
    monkeypatch.delenv("AITHERNET_STATE_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    from aithernet.provisioning.state import default_state_root
    root = default_state_root()
    assert str(root).startswith(str(fake_home))
    assert not str(root).startswith("/root")
    assert not str(root).startswith("/var/lib")
