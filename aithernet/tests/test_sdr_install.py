"""Tests for validated SDR dependency installation (Stage 14G, PHASE 5).

Software-only: asserts the PLAN logic (mode resolution, profile scoping, estimates, runtime probes)
and the read-only runtime verification — without running apt, building from source, or touching a
device. The central guarantee is that the Pluto plan is scoped to the GNU Radio IIO + SoapyPlutoSDR
path and NOT the whole Soapy/Pothosware/UHD ecosystem.
"""

from __future__ import annotations

import pytest

from aithernet.hardware import sdr


# --- mode + profile resolution --------------------------------------------
def test_modes_and_unknown_rejected():
    with pytest.raises(sdr.SdrError):
        sdr.plan("plutosdr", "bogus-mode")
    with pytest.raises(sdr.SdrError):
        sdr.plan("not-a-profile", "recommended")


def test_alias_resolves_to_internal():
    assert sdr.plan("plutosdr").internal == "pluto"
    assert sdr.plan("pluto").internal == "pluto"          # compatibility alias
    assert sdr.plan("software-only").internal == "core"


# --- the central scoping guarantee ----------------------------------------
def test_pluto_plan_is_scoped_to_iio_path_not_whole_ecosystem():
    p = sdr.plan("plutosdr", "recommended")
    pkgs = set(p.apt_packages)
    # required Pluto path: GNU Radio IIO (capture) + SoapyPlutoSDR (discovery) + libiio
    assert {"gnuradio", "gr-iio", "libiio-utils", "soapysdr-module-plutosdr",
            "soapysdr-tools"} <= pkgs
    # explicitly NOT the whole ecosystem
    assert "soapysdr-module-all" not in pkgs
    assert "uhd-host" not in pkgs and "libuhd-dev" not in pkgs
    assert "gr-osmosdr" not in pkgs


def test_pluto_runtime_probe_is_the_actual_capture_import():
    p = sdr.plan("plutosdr")
    # the capture path is `from gnuradio import iio` — the probe must assert exactly that
    assert ["python3", "-c", "from gnuradio import gr, iio"] in p.runtime_probes
    assert ["SoapySDRUtil", "--find"] in p.runtime_probes


def test_pluto_source_mode_pins_only_what_it_needs():
    p = sdr.plan("plutosdr", "source")
    names = {c["name"] for c in p.source_components}
    assert names == {"gnuradio", "SoapySDR"}              # no UHD for Pluto
    assert p.source_prefix.startswith("/opt/aithernet")   # Aithernet-owned prefix, never /usr/local
    assert all(c["pin"] and not c["pin"].endswith(("main", "master")) for c in p.source_components)


# --- modes -----------------------------------------------------------------
def test_recommended_uses_apt_network():
    p = sdr.plan("plutosdr", "recommended")
    assert any("apt-get install -y" in op for op in p.privileged_ops)
    assert any("apt" in s.lower() for s in p.download_sources)  # validated apt repositories
    assert p.est_download_mb > 0 and p.est_installed_mb > 0


def test_offline_never_uses_apt():
    # Finding 2B: offline must make ZERO apt/network calls. Without pre-staged assets it reports
    # unavailable; with pre-staged .deb payloads it installs via dpkg — never apt.
    p = sdr.plan("plutosdr", "offline")
    assert p.offline_unavailable
    assert not any("apt" in op for op in p.privileged_ops)
    assert p.download_sources == []


def test_offline_with_staged_uses_dpkg(tmp_path):
    osp = tmp_path / "os-packages"
    osp.mkdir()
    (osp / "gnuradio.deb").write_bytes(b"x")
    p = sdr.plan("plutosdr", "offline", staged_dir=str(tmp_path))
    assert not p.offline_unavailable
    assert any("dpkg -i" in op for op in p.privileged_ops)
    assert not any("apt" in op for op in p.privileged_ops)


def test_software_only_needs_no_vendor_packages_or_groups():
    p = sdr.plan("software-only")
    assert "gnuradio" in p.apt_packages
    assert "soapysdr-module-plutosdr" not in p.apt_packages
    assert p.groups == []                                 # no USB device groups for software-only


def test_full_lab_includes_groups_and_logout():
    p = sdr.plan("full-lab")
    assert p.groups                                       # plugdev/dialout
    assert p.requires_logout is True


def test_plan_to_dict_serialisable():
    import json
    json.dumps(sdr.plan("usrp", "source").to_dict())


# --- runtime verification (read-only, monkeypatched) -----------------------
def test_verify_runtime_all_ok(monkeypatch):
    monkeypatch.setattr(sdr.shutil, "which", lambda e: "/usr/bin/" + e)
    monkeypatch.setattr(sdr, "_probe", lambda cmd, timeout=20.0: (0, "ok"))
    out = sdr.verify_runtime("plutosdr")
    assert out["ok"] is True
    assert all(p["status"] == "ok" for p in out["probes"])


def test_verify_runtime_reports_missing_tool(monkeypatch):
    monkeypatch.setattr(sdr.shutil, "which", lambda e: None)       # SoapySDRUtil absent
    monkeypatch.setattr(sdr, "_probe", lambda cmd, timeout=20.0: (0, "ok"))
    out = sdr.verify_runtime("plutosdr")
    assert out["ok"] is False
    statuses = {p["probe"]: p["status"] for p in out["probes"]}
    assert statuses["SoapySDRUtil --find"] == "tool-absent"


def test_verify_runtime_reports_failed_import(monkeypatch):
    monkeypatch.setattr(sdr.shutil, "which", lambda e: "/usr/bin/" + e)
    monkeypatch.setattr(sdr, "_probe",
                        lambda cmd, timeout=20.0: (1, "ImportError") if "gnuradio" in cmd[-1]
                        else (0, "ok"))
    out = sdr.verify_runtime("plutosdr")
    assert out["ok"] is False


def test_status_reports_presence(monkeypatch):
    monkeypatch.setattr(sdr.shutil, "which", lambda e: "/usr/bin/" + e)
    monkeypatch.setattr(sdr, "_probe", lambda cmd, timeout=20.0: (0, "x"))
    st = sdr.status("plutosdr")
    assert st["present"]["gnuradio"] and st["present"]["gr_iio"]
    assert st["runtime"]["ok"] is True
