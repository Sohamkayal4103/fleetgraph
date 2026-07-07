"""Tests for hardware-neutral RF capabilities + effective-envelope resolution (PHASE 8 #2).

Proves the generic mission layer carries NO device-specific defaults: the Pluto envelope lives in
the plutosdr adapter, the effective envelope is an intersection (global ∩ adapter ∩ device ∩
operator), software-only cannot receive, generic-soapy is device-derived (undetermined without a
device), and no adapter is claimed physically qualified.
"""

from __future__ import annotations

import pytest

from aithernet.missions import rf_constraints as rfc
from aithernet.rf import capabilities as caps


def test_all_required_adapters_present():
    for name in ("software-only", "simulation", "plutosdr", "usrp", "generic-soapy"):
        assert name in caps.ADAPTERS


def test_no_adapter_is_physically_qualified():
    assert all(not c.physically_qualified for c in caps.ADAPTERS.values())


def test_pluto_envelope_lives_in_the_adapter_not_the_generic_layer():
    pluto = caps.adapter_capabilities("plutosdr")
    assert pluto.freq_min_hz == 70_000_000.0 and pluto.freq_max_hz == 6_000_000_000.0
    assert pluto.sample_rate_max == 61_440_000.0
    # the generic resolved bounds default carries NO device band (None) until an adapter is applied
    assert rfc.ReceiveOnlyBounds().freq_min_hz is None
    assert rfc.ReceiveOnlyBounds().freq_max_hz is None


def test_software_only_cannot_receive():
    bounds = rfc.resolve_bounds("software-only")
    assert bounds.rx_capable is False
    req = rfc.RFMissionRequest("x", "dev", 2.4e9, 2e6, 3)
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate(req, bounds)
    assert exc.value.category == "adapter_cannot_receive"


def test_generic_soapy_envelope_undetermined_without_device():
    bounds = rfc.resolve_bounds("generic-soapy")
    assert bounds.freq_min_hz is None
    req = rfc.RFMissionRequest("x", "dev", 2.4e9, 2e6, 3)
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate(req, bounds)
    assert exc.value.category == "envelope_undetermined"


def test_device_caps_narrow_the_adapter_envelope():
    # a device that only tunes 2.40–2.50 GHz narrows the Pluto 70 MHz–6 GHz envelope
    dev = caps.RFCapabilities("device", freq_min_hz=2.40e9, freq_max_hz=2.50e9,
                              sample_rate_min=1e6, sample_rate_max=20e6,
                              gain_min_db=0, gain_max_db=60)
    bounds = rfc.resolve_bounds("plutosdr", device_caps=dev)
    assert bounds.freq_min_hz == 2.40e9 and bounds.freq_max_hz == 2.50e9
    assert bounds.sample_rate_max == 20e6
    # a request outside the narrowed device band is rejected even though the adapter allows it
    req = rfc.RFMissionRequest("x", "dev", 5.0e9, 2e6, 3)  # within Pluto, outside the device
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate(req, bounds)
    assert exc.value.category == "frequency_out_of_band"


def test_intersection_takes_the_more_restrictive_bound():
    a = caps.RFCapabilities("a", freq_min_hz=70e6, freq_max_hz=6e9, gain_max_db=73)
    b = caps.RFCapabilities("b", freq_min_hz=100e6, freq_max_hz=3e9, gain_max_db=50)
    eff = a.intersect(b)
    assert eff.freq_min_hz == 100e6      # larger min
    assert eff.freq_max_hz == 3e9        # smaller max
    assert eff.gain_max_db == 50         # smaller max
    assert eff.rx_capable is True


def test_intersection_rx_capable_is_and():
    sw = caps.adapter_capabilities("software-only")   # rx_capable=False
    pluto = caps.adapter_capabilities("plutosdr")
    assert sw.intersect(pluto).rx_capable is False


def test_simulation_allows_capture_without_hardware():
    bounds = rfc.resolve_bounds("simulation")
    assert bounds.rx_capable is True and bounds.physically_qualified is False
    plan = rfc.validate(rfc.RFMissionRequest("sim survey", "sim-0", 2.4e9, 2e6, 3), bounds)
    assert plan.adapter == "simulation" and plan.physically_qualified is False


def test_device_caps_from_soapy_probe():
    probe = {"frequency_range": {"min": 7e7, "max": 6e9},
             "sample_rate_range": {"min": 5.21e5, "max": 6.144e7}}
    dc = caps.device_capabilities_from_soapy(probe)
    assert dc.freq_min_hz == 7e7 and dc.sample_rate_max == 6.144e7
    assert dc.rx_capable is True
