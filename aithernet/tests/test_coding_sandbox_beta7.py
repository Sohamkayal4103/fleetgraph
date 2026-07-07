"""beta.7 — coding-sandbox guided repair/restore UX (FIX 5) and the mission preflight blocker for
a degraded sandbox (FIX 6). No sysctl is ever mutated (subprocess.run is monkeypatched)."""
from __future__ import annotations

import subprocess

from typer.testing import CliRunner

from aithernet.cli import app
from aithernet.coding_agent import sandbox_preflight as sp
from aithernet.doctor import DoctorReport, _check_coding_sandbox

runner = CliRunner()


class _Ok:
    returncode = 0


# ── FIX 5: sandbox repair/restore helpers + CLI ──────────────────────────────
def test_relax_and_restore_command_argv():
    assert sp.relax_userns_command() == [
        "sudo", "sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=0"]
    assert sp.restore_userns_command() == [
        "sudo", "sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=1"]


def test_host_hardening_relaxed_detection(monkeypatch):
    monkeypatch.setattr(sp, "read_sysctl", lambda name: "0")
    assert sp.host_hardening_relaxed() is True
    monkeypatch.setattr(sp, "read_sysctl", lambda name: "1")
    assert sp.host_hardening_relaxed() is False


def test_repair_never_runs_sudo_without_confirmation(monkeypatch):
    monkeypatch.setattr(sp, "host_hardening_relaxed", lambda: False)
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a[0]) or _Ok())
    result = runner.invoke(app, ["coding-sandbox", "repair"], input="n\n")
    assert result.exit_code == 1
    assert ran == [], "sudo must NOT run when the user declines"
    assert "RELAX" in result.output.upper()
    assert "restore" in result.output.lower()  # restore command shown up-front


def test_repair_runs_sudo_only_after_confirmation(monkeypatch):
    monkeypatch.setattr(sp, "host_hardening_relaxed", lambda: False)
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a[0]) or _Ok())
    result = runner.invoke(app, ["coding-sandbox", "repair", "--yes"])
    assert result.exit_code == 0
    assert ran == [sp.relax_userns_command()]
    assert "restore" in result.output.lower()


def test_repair_persistent_is_rejected(monkeypatch):
    monkeypatch.setattr(sp, "host_hardening_relaxed", lambda: False)
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a[0]) or _Ok())
    result = runner.invoke(app, ["coding-sandbox", "repair", "--persistent", "--yes"])
    assert result.exit_code == 2
    assert ran == []  # never mutates the host for an unsupported persistent request


def test_restore_runs_the_restore_command(monkeypatch):
    ran: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a[0]) or _Ok())
    result = runner.invoke(app, ["coding-sandbox", "restore", "--yes"])
    assert result.exit_code == 0
    assert ran == [sp.restore_userns_command()]


def test_doctor_reports_relaxed_host_hardening(monkeypatch):
    monkeypatch.setattr(sp, "preflight_sandbox",
                        lambda **k: sp.SandboxProbe(severity="ready", detail="bwrap ok"))
    monkeypatch.setattr(sp, "host_hardening_relaxed", lambda: True)
    r = DoctorReport()
    _check_coding_sandbox(r)
    by_name = {c.name: c for c in r.checks}
    assert "coding_sandbox" in by_name  # sandbox itself READY
    assert "host_hardening" in by_name
    hh = by_name["host_hardening"]
    assert "relaxed" in hh.detail
    assert "coding-sandbox restore" in hh.remediation


def test_doctor_omits_hardening_note_when_not_relaxed(monkeypatch):
    monkeypatch.setattr(sp, "preflight_sandbox",
                        lambda **k: sp.SandboxProbe(severity="ready", detail="bwrap ok"))
    monkeypatch.setattr(sp, "host_hardening_relaxed", lambda: False)
    r = DoctorReport()
    _check_coding_sandbox(r)
    assert "host_hardening" not in {c.name for c in r.checks}  # quiet when healthy


# ── FIX 6: mission preflight blocker ─────────────────────────────────────────
def test_mission_preflight_blocks_a_degraded_sandbox(monkeypatch):
    from aithernet.missions.engine import MissionEngine
    monkeypatch.setattr(sp, "preflight_sandbox",
                        lambda **k: sp.SandboxProbe(severity="blocked",
                                                    detail="bubblewrap uid-map probe failed"))
    monkeypatch.setattr(sp, "apparmor_restrict_value", lambda: "1")
    eng = MissionEngine.__new__(MissionEngine)   # bypass __init__; helper only needs the cache attr
    block = eng._coding_sandbox_preflight_block("run-A")
    assert block is not None
    assert "AppArmor user-namespace" in block
    assert "coding-agent filesystem operations" in block


def test_mission_preflight_allows_a_ready_sandbox_and_caches(monkeypatch):
    from aithernet.missions.engine import MissionEngine
    calls = {"n": 0}

    def _probe(**k):
        calls["n"] += 1
        return sp.SandboxProbe(severity="ready", detail="ok")

    monkeypatch.setattr(sp, "preflight_sandbox", _probe)
    eng = MissionEngine.__new__(MissionEngine)
    assert eng._coding_sandbox_preflight_block("run-B") is None
    assert eng._coding_sandbox_preflight_block("run-B") is None  # cached — no re-probe
    assert calls["n"] == 1
