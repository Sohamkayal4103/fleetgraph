"""beta.5: coding-agent bubblewrap sandbox readiness preflight.

Detects the beta.4 failures — `bwrap: setting up uid map: Permission denied` and
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` — using non-destructive live probes,
with the Ubuntu AppArmor `kernel.apparmor_restrict_unprivileged_userns` sysctl as context, and maps
the result into `aithernet doctor` truthfully (blocked => not READY, with exact remediation).
"""

from __future__ import annotations

from aithernet import doctor
from aithernet.coding_agent import sandbox_preflight as sp

# -- signature matching -----------------------------------------------------------------------

def test_matches_the_two_beta4_signatures():
    assert sp.matches_sandbox_failure(
        "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted")
    assert sp.matches_sandbox_failure("bwrap: setting up uid map: Permission denied")
    assert not sp.matches_sandbox_failure("codex exited with status 1: syntax error")
    assert not sp.matches_sandbox_failure("")


# -- preflight severity classification (live probe is authoritative) --------------------------

def _patch(monkeypatch, *, which="/usr/bin/bwrap", sysctls=None, probe_results=None):
    sysctls = sysctls or {}
    monkeypatch.setattr(sp.shutil, "which", lambda name: which)
    monkeypatch.setattr(sp, "read_sysctl", lambda name: sysctls.get(name))
    monkeypatch.setattr(sp, "_bwrap_version", lambda b: "0.9.0")
    results = list(probe_results or [])

    def fake_run(bwrap, args, timeout=12.0):
        return results.pop(0) if results else (0, "")

    monkeypatch.setattr(sp, "_run_bwrap", fake_run)


def test_ready_when_both_probes_pass_even_with_apparmor_restricted(monkeypatch):
    _patch(monkeypatch,
           sysctls={"kernel.apparmor_restrict_unprivileged_userns": "1"},
           probe_results=[(0, ""), (0, "")])
    probe = sp.preflight_sandbox()
    assert probe.severity == "ready" and probe.ready is True
    assert probe.apparmor_restricted is True   # reported for context, but does not block


def test_blocked_on_uid_map_failure_with_apparmor_remediation(monkeypatch):
    _patch(monkeypatch,
           sysctls={"kernel.apparmor_restrict_unprivileged_userns": "1"},
           probe_results=[(1, "bwrap: setting up uid map: Permission denied")])
    probe = sp.preflight_sandbox()
    assert probe.severity == "blocked"
    assert "apparmor_restrict_unprivileged_userns=0" in probe.remediation
    assert "hardening" in probe.remediation.lower()   # security warning present


def test_blocked_on_loopback_failure(monkeypatch):
    _patch(monkeypatch,
           sysctls={"kernel.apparmor_restrict_unprivileged_userns": "1"},
           probe_results=[(0, ""),  # uid-map ok
                          (1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted")])
    probe = sp.preflight_sandbox()
    assert probe.severity == "blocked"
    assert "loopback" in probe.detail.lower()


def test_absent_when_bwrap_missing(monkeypatch):
    _patch(monkeypatch, which=None)
    probe = sp.preflight_sandbox()
    assert probe.severity == "absent"
    assert "bubblewrap" in probe.remediation.lower()


def test_signals_include_sysctls_read(monkeypatch):
    _patch(monkeypatch,
           sysctls={"kernel.apparmor_restrict_unprivileged_userns": "1",
                    "user.max_user_namespaces": "1000"},
           probe_results=[(0, ""), (0, "")])
    probe = sp.preflight_sandbox()
    assert probe.signals["kernel.apparmor_restrict_unprivileged_userns"] == "1"
    assert probe.signals["user.max_user_namespaces"] == "1000"


# -- doctor integration (truthful readiness) --------------------------------------------------

def test_doctor_reports_ready_when_probe_ready(monkeypatch):
    monkeypatch.setattr(
        doctor, "_check_coding_sandbox", doctor._check_coding_sandbox)  # keep real
    monkeypatch.setattr(
        "aithernet.coding_agent.sandbox_preflight.preflight_sandbox",
        lambda **k: sp.SandboxProbe(severity="ready", detail="ok"))
    r = doctor.DoctorReport()
    doctor._check_coding_sandbox(r)
    c = r.checks[0]
    assert c.name == "coding_sandbox" and c.category == "coding" and c.status == doctor.READY


def test_doctor_degraded_and_not_ok_when_blocked(monkeypatch):
    monkeypatch.setattr(
        "aithernet.coding_agent.sandbox_preflight.preflight_sandbox",
        lambda **k: sp.SandboxProbe(severity="blocked", detail="uid map denied",
                                    remediation="sudo sysctl ..."))
    r = doctor.DoctorReport()
    doctor._check_coding_sandbox(r)
    c = r.checks[0]
    assert c.status == doctor.DEGRADED
    assert "coding agent may fail" in c.detail  # makes the mission impact clear
    assert r.ok is False                        # a blocked sandbox blocks overall readiness


def test_doctor_coding_sandbox_has_its_own_domain(monkeypatch):
    monkeypatch.setattr(
        "aithernet.coding_agent.sandbox_preflight.preflight_sandbox",
        lambda **k: sp.SandboxProbe(severity="blocked", detail="x", remediation="y"))
    r = doctor.DoctorReport()
    doctor._check_coding_sandbox(r)
    domains = r.domains()
    assert "coding_sandbox" in domains
    assert domains["coding_sandbox"]["status"] == "unavailable"
