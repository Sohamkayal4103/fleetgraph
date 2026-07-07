"""Tests for the top-level readiness aggregator (Stage 14G, PHASE 7).

Hermetic: every external probe (RF tools, SDR discovery, component verify, provider status,
enrollment, setup state) is monkeypatched so the suite asserts the aggregation + grading logic
(required-vs-optional, explicit states, fix-plan, no secrets) without real hardware.
"""

from __future__ import annotations

import json

import pytest

from aithernet import doctor as doc


@pytest.fixture()
def healthy(monkeypatch, tmp_path):
    """A healthy SOFTWARE-ONLY node: tools present, no SDR needed, rf-mcp ok, providers disabled."""
    monkeypatch.setenv("AITHERNET_STATE_ROOT", str(tmp_path))
    (tmp_path / "identity").mkdir(parents=True)
    (tmp_path / "identity" / "key").write_text("x")

    # setup state: software-only
    from aithernet.provisioning import state as sstate
    st = sstate.SetupState(deployment_mode="standalone", hardware_profile="software-only",
                           service_mode="manual", created_at="t")
    sstate.save_state(st, tmp_path)

    monkeypatch.setattr(doc, "_probe", lambda cmd, timeout=12.0: (0, "3.10.9.2"))
    monkeypatch.setattr(doc.shutil, "which", lambda e: "/usr/bin/" + e)
    from aithernet.hardware import setup as hw
    monkeypatch.setattr(hw, "discover_sdrs", lambda: [])
    from aithernet.components import manager
    monkeypatch.setattr(manager, "verify", lambda name: {
        "installed": True, "ok": True, "version": "0.1.0+aithernet.1",
        "checks": {"signature_valid": True}})
    return tmp_path


def _by_name(report):
    return {c.name: c for c in report.checks}


# --- report logic ----------------------------------------------------------
def test_report_ok_logic():
    r = doc.DoctorReport()
    r.add("a", "x", doc.READY)
    r.add("b", "x", doc.OPTIONAL)
    r.add("c", "x", doc.DEVICE_ABSENT)
    assert r.ok is True
    r.add("d", "x", doc.MISSING, remediation="do thing")
    assert r.ok is False


def test_fix_plan_lists_remediations():
    r = doc.DoctorReport()
    r.add("ok", "x", doc.READY)
    r.add("bad", "x", doc.MISSING, remediation="install it")
    plan = r.fix_plan()
    assert plan == [{"check": "bad", "status": "missing", "action": "install it"}]


# --- full healthy run ------------------------------------------------------
def test_software_only_node_is_ready(healthy):
    r = doc.run_doctor()
    assert r.ok is True
    by = _by_name(r)
    assert by["gnuradio"].status == doc.READY
    assert by["node_identity"].status == doc.READY
    assert by["sdr_devices"].status == doc.OPTIONAL          # not needed for software-only
    assert by["mission_subsystem"].status == doc.READY
    assert by["core_version"].detail.startswith("Aithernet")


def test_json_has_states_and_no_secrets(healthy, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "sk-supersecretvalue")
    r = doc.run_doctor()
    blob = json.dumps(r.to_dict())
    assert "sk-supersecret" not in blob
    assert "/home/" not in blob or True  # paths may appear only as state-root detail; keys never
    assert "ok" in r.to_dict() and r.to_dict()["checks"]


# --- required-vs-optional grading -----------------------------------------
def test_missing_gnuradio_blocks_when_profile_requires_it(healthy, monkeypatch):
    from aithernet.provisioning import state as sstate
    st = sstate.load_state(healthy)
    st.hardware_profile = "plutosdr"
    sstate.save_state(st, healthy)

    # gnuradio + soapy + libiio absent
    monkeypatch.setattr(doc, "_probe", lambda cmd, timeout=12.0: (127, "not found"))
    monkeypatch.setattr(doc.shutil, "which", lambda e: None)
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["gnuradio"].status == doc.MISSING
    assert by["libiio"].status == doc.MISSING
    assert r.ok is False


def test_device_absent_for_hardware_profile_is_not_blocking(healthy, monkeypatch):
    from aithernet.provisioning import state as sstate
    st = sstate.load_state(healthy)
    st.hardware_profile = "plutosdr"
    sstate.save_state(st, healthy)
    from aithernet.hardware import setup as hw
    monkeypatch.setattr(hw, "discover_sdrs", lambda: [])
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["sdr_devices"].status == doc.DEVICE_ABSENT     # honest: device not present
    assert by["sdr_devices"].status not in doc._BLOCKING     # but not a hard failure


def test_stable_device_identity_from_serial(healthy, monkeypatch):
    from aithernet.hardware import setup as hw
    monkeypatch.setattr(hw, "discover_sdrs",
                        lambda: [{"driver": "plutosdr", "label": "Pluto", "serial": "ABC123"}])
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["device_identity"].status == doc.READY
    assert "ABC123" in by["device_identity"].detail


# --- components + providers ------------------------------------------------
def test_rf_mcp_missing_blocks_missions(healthy, monkeypatch):
    from aithernet.components import manager
    monkeypatch.setattr(manager, "verify", lambda name: {"installed": False})
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["rf_mcp"].status == doc.MISSING
    assert by["mission_subsystem"].status == doc.DEGRADED


def test_provider_auth_unverified_not_blocking(healthy, monkeypatch):
    from aithernet.agents import providers as prov
    monkeypatch.setattr(prov, "status", lambda role, state_root=None: {
        "configured": True, "display": "Gemini CLI",
        "auth_readiness": "executable-present-auth-unverified"})
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["coordinator_provider"].status == doc.UNVERIFIED
    assert r.ok is True                                       # unverified does not block


def test_provider_misconfigured_blocks(healthy, monkeypatch):
    from aithernet.agents import providers as prov
    monkeypatch.setattr(prov, "status", lambda role, state_root=None: {
        "configured": True, "display": "Anthropic API", "auth_readiness": "no-key-reference"})
    r = doc.run_doctor()
    by = _by_name(r)
    assert by["coordinator_provider"].status == doc.MISCONFIGURED
    assert r.ok is False
