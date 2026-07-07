"""Hardware setup/profiles/doctor tests (Stage 14F).

Profile plans and the doctor's report shape are validated deterministically. Live device detection
is host-dependent and exercised separately on a real RF host; here we assert the read-only,
no-compile contract.
"""

from __future__ import annotations

import pytest

from aithernet.hardware import setup as hw


def test_all_required_profiles_exist():
    for name in ("core", "simulation", "pluto", "usrp", "soapy-generic", "full"):
        assert name in hw.PROFILES
        assert hw.PROFILES[name]["apt"], f"{name} must list packages"


def test_install_plan_uses_signed_distro_packages_no_source_build():
    plan = hw.install_plan("pluto")
    assert plan["apt_command"][:3] == ["sudo", "apt-get", "install"]
    assert "soapysdr-module-plutosdr" in plan["apt_packages"]
    # standard mode never compiles GNU Radio/UHD/SoapySDR from source
    assert "no source compilation" in plan["note"].lower()


def test_unknown_profile_rejected():
    with pytest.raises(KeyError):
        hw.install_plan("does-not-exist")


def test_source_build_pins_are_explicit_and_outside_usr_local():
    pins = hw.SOURCE_BUILD_PINS
    assert pins["prefix"].startswith("/opt/aithernet")  # Aithernet-owned, never /usr/local
    assert "/usr/local" not in pins["prefix"]
    for c in pins["components"]:
        assert c["url"] and c["pin"], "every source component must pin a revision"


def test_doctor_report_shape_is_serializable():
    # doctor() shells out to detection tools; on a host without them it still returns a valid,
    # serializable report (checks present, ok is a bool). No exceptions, no privilege.
    rep = hw.doctor()
    d = rep.to_dict()
    assert isinstance(d["ok"], bool)
    assert isinstance(d["checks"], list) and d["checks"]
    assert all({"name", "status"} <= set(c) for c in d["checks"])
