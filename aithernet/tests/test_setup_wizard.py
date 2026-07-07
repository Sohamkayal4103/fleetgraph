"""Tests for the guided setup wizard (Stage 14G, PHASE 4A).

Covers profile resolution + aliases, resumable/idempotent state, dry-run purity (no side effects),
plan aggregation, and the run modes (full / resume / repair / only / failure isolation). Hardware-
touching steps (discovery/doctor) are exercised via monkeypatch so the suite is hermetic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aithernet import __version__
from aithernet.provisioning import profiles, services, state, wizard


@pytest.fixture(autouse=True)
def _hermetic_hardware(monkeypatch):
    """Make the hardware-touching steps deterministic + fast (no real SDR / subprocesses)."""
    from aithernet import components as comp
    from aithernet.hardware import setup as hw

    monkeypatch.setattr(hw, "discover_sdrs", lambda: [])
    monkeypatch.setattr(hw, "doctor", lambda: hw.DoctorReport())  # no checks -> ok=True
    monkeypatch.setattr(
        comp, "component_status",
        lambda name: comp.ComponentStatus(name=name, installed=False, issues=["not installed"]))


# --- version derivation ----------------------------------------------------
def test_version_is_not_the_stale_hardcoded_string():
    assert __version__ not in ("0.1.0", "0.8.0-beta.1", "0.8.0-beta.3")
    # derives from the installed package metadata (the 1.0.0-beta.X release line; PEP 440
    # normalizes 1.0.0-beta.5 -> 1.0.0b5, so match the release-series prefix, not an exact string)
    assert __version__.startswith("1.0.0")


# --- profiles --------------------------------------------------------------
def test_profile_names_are_customer_facing():
    assert profiles.profile_names() == [
        "software-only", "simulation", "plutosdr", "usrp", "generic-soapy", "full-lab"]


@pytest.mark.parametrize("alias,internal", [
    ("core", "core"), ("pluto", "pluto"), ("soapy-generic", "soapy-generic"),
    ("full", "full"), ("software-only", "core"), ("plutosdr", "pluto"),
    ("generic-soapy", "soapy-generic"), ("full-lab", "full"),
])
def test_compatibility_aliases_resolve(alias, internal):
    assert profiles.internal_name(alias) == internal


def test_internal_names_are_valid_hardware_profiles():
    from aithernet.hardware import setup as hw
    for p in profiles.PROFILES:
        assert p.internal in hw.PROFILES


def test_unknown_profile_is_actionable():
    with pytest.raises(KeyError) as exc:
        profiles.resolve("totally-bogus")
    assert "software-only" in str(exc.value)


# --- state persistence -----------------------------------------------------
def test_state_roundtrip(tmp_path):
    st = state.SetupState(node_name="n1", hardware_profile="plutosdr", created_at="t0")
    st.record("deployment_mode", state.DONE, "ok", now="t1")
    path = state.save_state(st, tmp_path)
    assert path.is_file()
    # file is 0600, dir 0700
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = state.load_state(tmp_path)
    assert loaded.node_name == "n1"
    assert loaded.hardware_profile == "plutosdr"
    assert loaded.status_of("deployment_mode") == state.DONE


def test_load_state_missing_returns_none(tmp_path):
    assert state.load_state(tmp_path) is None


def test_load_state_corrupt_returns_none(tmp_path):
    p = state.state_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    assert state.load_state(tmp_path) is None


# --- dry-run purity --------------------------------------------------------
def test_build_plan_makes_no_changes(tmp_path):
    st = wizard.load_or_new(tmp_path)
    plans = wizard.build_plan(st, state_root=tmp_path, opts={"profile": "plutosdr"})
    assert len(plans) == len(wizard.STEPS)
    # nothing written, no dirs created
    assert state.load_state(tmp_path) is None
    assert not (tmp_path / "identity").exists()
    # the plutosdr step surfaces apt packages + sudo
    sdr = next(p for p in plans if p.step_id == "sdr_strategy")
    assert sdr.requires_sudo and sdr.os_packages
    assert sdr.est_download_mb > 0


def test_plan_totals_aggregate(tmp_path):
    st = wizard.load_or_new(tmp_path)
    plans = wizard.build_plan(st, state_root=tmp_path, opts={"profile": "full-lab"})
    totals = wizard.plan_totals(plans)
    assert "gnuradio" in totals["os_packages"]
    assert totals["est_installed_mb"] > 0
    assert "sdr_strategy" in totals["privileged_steps"]
    assert "rf-mcp" in totals["components"]


# --- run modes (hermetic: only config/service steps) -----------------------
_SAFE = ["deployment_mode", "node_identity", "directories", "service_mode", "service_install",
         "hardware_profile", "sdr_strategy", "coordinator_provider", "coding_provider"]


def _run_safe(tmp_path, **opts):
    st = wizard.load_or_new(tmp_path)
    base = {"deployment_mode": "standalone", "service_mode": "manual",
            "profile": "software-only", "node_name": "t"}
    base.update(opts)
    return st, wizard.run(st, state_root=tmp_path, opts=base, only=_SAFE)


def test_full_run_then_idempotent(tmp_path):
    """A FULL run (all steps, hardware monkeypatched) then a plain re-run executes nothing."""
    st = wizard.load_or_new(tmp_path)
    opts = {"deployment_mode": "standalone", "service_mode": "manual",
            "profile": "software-only", "node_name": "t"}
    results = wizard.run(st, state_root=tmp_path, opts=opts)
    assert len(results) == len(wizard.STEPS)
    assert all(r.status != state.FAILED for r in results)
    assert (tmp_path / "identity").exists()
    # re-run without flags: every step already complete -> nothing executes (idempotent)
    again = wizard.run(st, state_root=tmp_path, opts=opts)
    assert again == []


def test_resume_only_runs_incomplete(tmp_path):
    st = wizard.load_or_new(tmp_path)
    wizard.run(st, state_root=tmp_path, opts={"deployment_mode": "standalone"},
               only=["deployment_mode"])
    # resume the rest (real resume: no `only`, skips complete steps)
    res = wizard.run(st, state_root=tmp_path,
                     opts={"service_mode": "manual", "profile": "software-only", "node_name": "t"})
    ran = {r.step_id for r in res}
    assert "deployment_mode" not in ran          # already complete
    assert "node_identity" in ran


def test_repair_reruns_deferred(tmp_path):
    st, _ = _run_safe(tmp_path, profile="plutosdr")
    assert st.status_of("sdr_strategy") == state.DEFERRED
    res = wizard.run(st, state_root=tmp_path,
                     opts={"profile": "plutosdr", "service_mode": "manual"},
                     repair=True)
    ran = {r.step_id for r in res}
    assert "sdr_strategy" in ran                  # deferred is re-run by repair
    assert "deployment_mode" not in ran           # done is not


def test_failed_step_does_not_abort_run(tmp_path, monkeypatch):
    st = wizard.load_or_new(tmp_path)

    def boom(ctx):
        raise RuntimeError("kaboom")

    target = wizard.get_step("hardware_profile")
    monkeypatch.setattr(target, "apply", boom)
    results = wizard.run(st, state_root=tmp_path,
                         opts={"deployment_mode": "standalone", "service_mode": "manual"},
                         only=["deployment_mode", "hardware_profile", "coding_provider"])
    by_id = {r.step_id: r for r in results}
    assert by_id["hardware_profile"].status == state.FAILED
    # later step still ran despite the earlier failure
    assert by_id["coding_provider"].status == state.DONE


def test_non_interactive_rejects_no_prompt(tmp_path):
    # with interactive False and no ask, answers come from opts/defaults only (never blocks)
    st = wizard.load_or_new(tmp_path)
    res = wizard.run(st, state_root=tmp_path, opts={}, only=["deployment_mode"],
                     interactive=False, ask=None)
    assert res[0].status == state.DONE
    assert st.deployment_mode == "standalone"  # default


# --- service planning ------------------------------------------------------
@pytest.mark.parametrize("mode,priv", [
    (services.USER, False), (services.SYSTEM, True), (services.MANUAL, False)])
def test_service_plan_privilege(mode, priv):
    p = services.plan(mode)
    assert p["privileged"] is priv
    assert p["install_commands"]


def test_user_unit_install(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = services.install_user_unit(state_root=tmp_path)
    assert path.is_file()
    txt = path.read_text()
    assert "WantedBy=default.target" in txt and "ExecStart=" in txt


def test_system_unit_is_hardened():
    txt = services.system_unit_text()
    assert "User=aithernet" in txt
    assert "NoNewPrivileges=true" in txt
    assert "ProtectSystem=strict" in txt


# --- packaging: works from an installed entrypoint, outside the source tree ----
def test_setup_dry_run_from_installed_cli_outside_source(tmp_path):
    """ACCEPTANCE #1/#2: `aithernet setup --dry-run` runs from the installed console script with
    cwd OUTSIDE the repo and produces a complete plan, importing nothing from the source tree."""
    exe = shutil.which("aithernet") or str(Path(sys.executable).parent / "aithernet")
    if not Path(exe).exists():
        pytest.skip("aithernet console script not installed")
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    proc = subprocess.run(
        [exe, "setup", "--dry-run", "--json", "--profile", "plutosdr",
         "--state-root", str(tmp_path / "state")],
        cwd=str(workdir), capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert len(data["plan"]) == len(wizard.STEPS)
    assert "gnuradio" in data["totals"]["os_packages"]
    # dry-run wrote no state file
    assert not (tmp_path / "state" / "setup" / "state.json").exists()


def test_module_import_does_not_require_repo(tmp_path):
    """The provisioning package imports purely from the installed distribution."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import aithernet.provisioning.wizard as w; print(len(w.STEPS))"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(len(wizard.STEPS))
