"""Readiness domains + concise customer errors (beta.8 phase 9)."""

from __future__ import annotations

import subprocess
import sys

from aithernet import doctor as doc
from aithernet.doctor import DEVICE_ABSENT, MISSING, READY, DoctorReport


def test_domains_partition_checks_and_are_truthful():
    r = DoctorReport()
    r.add("identity", "identity", READY)
    r.add("node_db", "core", READY)
    r.add("coordinator", "agents", READY)
    r.add("sdr_devices", "hardware", DEVICE_ABSENT)  # no SDR connected
    r.add("rf_mcp", "components", MISSING)            # MCP not ready
    d = r.domains()
    assert d["core_node"]["status"] == "available"
    assert d["providers"]["status"] == "available"
    assert d["physical_rf"]["status"] == "unavailable"   # device absent -> not available
    assert d["mcp"]["status"] == "unavailable"            # blocking
    assert d["hosted_enrollment"]["status"] == "not-applicable"  # no hosted checks


def test_software_only_core_ready_while_physical_rf_unavailable():
    r = DoctorReport()
    r.add("identity", "identity", READY)
    r.add("sdr_devices", "hardware", DEVICE_ABSENT)
    d = r.domains()
    # core can be ready even though physical RF is unavailable; the top-level must not imply RF.
    assert d["core_node"]["status"] == "available"
    assert d["physical_rf"]["status"] == "unavailable"


def test_to_dict_includes_domains():
    r = DoctorReport()
    r.add("identity", "identity", READY)
    assert "domains" in r.to_dict()


def test_run_doctor_exposes_domains():
    report = doc.run_doctor()
    domains = report.domains()
    assert set(domains) >= {"core_node", "providers", "mcp", "physical_rf", "hosted_enrollment"}


def test_cli_concise_error_no_traceback(tmp_path):
    """A command pointed at a non-existent config surfaces a concise error, not a Rich traceback."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    # `database status` loads config from the given path; a bogus path is an expected failure.
    res = subprocess.run(
        [sys.executable, "-m", "aithernet.cli", "status", "--config", str(tmp_path / "nope.yaml")],
        capture_output=True, text=True, env={**env}, timeout=60)
    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "Traceback (most recent call last)" not in combined
