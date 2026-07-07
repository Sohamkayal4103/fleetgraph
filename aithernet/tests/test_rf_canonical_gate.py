"""Receive-only gate integrated into the CANONICAL RF path (Stage 14G corrective COMMIT 2).

Proves the deterministic gate runs at the canonical boundaries — the lease-acquire authorization
(`assert_receive_only`, called by orchestrator `_route_rf_device`) and the RX execution boundary
(`validate_capture`, called by `hardware/capture.py CaptureService.capture`). The capture wiring
test confirms an out-of-band request is rejected BEFORE the capture subprocess runs. Pure +
lightweight; no real hardware, no full runtime.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from aithernet.missions import rf_constraints as rfc
from aithernet.rf import capabilities as caps


# --- receive-only authorization gate (lease acquire) -----------------------
@pytest.mark.parametrize("direction", ["tx", "rx_tx", "txrx", "transmit", "both", ""])
def test_assert_receive_only_rejects_transmit(direction):
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.assert_receive_only(direction)
    assert exc.value.category == "transmit_forbidden"


def test_assert_receive_only_allows_rx():
    rfc.assert_receive_only("rx")   # no raise


# --- adapter/device capability helpers -------------------------------------
@pytest.mark.parametrize("driver,adapter", [
    ("plutosdr", "plutosdr"), ("pluto", "plutosdr"), ("ad9361", "plutosdr"),
    ("uhd", "usrp"), ("usrp", "usrp"), ("lime", "generic-soapy"), (None, "generic-soapy")])
def test_adapter_for_driver(driver, adapter):
    assert caps.adapter_for_driver(driver) == adapter


def test_device_capabilities_from_contract_ranges():
    fr = types.SimpleNamespace(min_hz=2.40e9, max_hz=2.50e9)
    sr = types.SimpleNamespace(min=1e6, max=20e6)
    contract = types.SimpleNamespace(frequency_ranges=[fr], sample_rate_ranges=[sr],
                                     rx_supported=True)
    dc = caps.device_capabilities_from_contract(contract)
    assert dc.freq_min_hz == 2.40e9 and dc.freq_max_hz == 2.50e9 and dc.sample_rate_max == 20e6


def test_device_capabilities_none_when_no_ranges():
    contract = types.SimpleNamespace(frequency_ranges=[], sample_rate_ranges=[], rx_supported=True)
    assert caps.device_capabilities_from_contract(contract) is None
    assert caps.device_capabilities_from_contract(None) is None


# --- validate_capture: the RX execution boundary gate ----------------------
def _cap(**kw):
    base = dict(device_id="pluto-0", freq_hz=2.437e9, sample_rate=2e6, duration_s=3.0,
                gain_db=40.0, adapter="plutosdr")
    base.update(kw)
    return rfc.validate_capture(**base)


def test_validate_capture_valid_pluto():
    plan = _cap()
    assert plan.direction == "rx" and plan.adapter == "plutosdr" and plan.digest


def test_validate_capture_out_of_band():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _cap(freq_hz=10e9)
    assert exc.value.category == "frequency_out_of_band"


def test_validate_capture_rate_out_of_range():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _cap(sample_rate=200e6)
    assert exc.value.category == "sample_rate_out_of_range"


def test_validate_capture_duration_too_long():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _cap(duration_s=999.0)
    assert exc.value.category == "duration_too_long"


def test_validate_capture_software_only_cannot_receive():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _cap(adapter="software-only")
    assert exc.value.category == "adapter_cannot_receive"


def test_validate_capture_device_allowlist():
    pol = rfc.GlobalRxPolicy(allowed_devices=("pluto-A",))
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _cap(device_id="pluto-B", policy=pol)
    assert exc.value.category == "device_unauthorized"


def test_validate_capture_gain_clamped():
    plan = _cap(gain_db=500)
    assert plan.gain_db == 73.0 and "gain_db" in plan.clamped


# --- FAIL CLOSED for physical hardware when capabilities are unknown -------
def test_physical_generic_fails_closed_when_envelope_unknown():
    # generic-soapy with NO declared device ranges: a real capture must be REFUSED, not permitted.
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="dev-1", freq_hz=2.4e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="generic-soapy")
    assert exc.value.category == "frequency_capability_unknown"


def test_physical_missing_device_identity_fails_closed():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="", freq_hz=2.4e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="plutosdr")
    assert exc.value.category == "device_identity_unresolved"


def test_physical_partial_caps_fail_closed_per_axis():
    # device reports frequency but NOT sample-rate -> sample_rate_capability_unknown
    dev = caps.RFCapabilities("device", rx_capable=True, requires_hardware=True,
                              freq_min_hz=2.4e9, freq_max_hz=2.5e9)
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="g-1", freq_hz=2.45e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="generic-soapy", device_caps=dev)
    assert exc.value.category == "sample_rate_capability_unknown"


def test_physical_missing_gain_caps_fail_closed():
    dev = caps.RFCapabilities("device", rx_capable=True, requires_hardware=True,
                              freq_min_hz=2.4e9, freq_max_hz=2.5e9,
                              sample_rate_min=1e6, sample_rate_max=20e6)  # no gain range
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="g-1", freq_hz=2.45e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="generic-soapy", device_caps=dev)
    assert exc.value.category == "gain_capability_unknown"


def test_generic_with_full_device_caps_validates():
    dev = caps.RFCapabilities("device", rx_capable=True, requires_hardware=True,
                              freq_min_hz=2.4e9, freq_max_hz=2.5e9,
                              sample_rate_min=1e6, sample_rate_max=20e6,
                              gain_min_db=0, gain_max_db=60)
    plan = rfc.validate_capture(device_id="g-1", freq_hz=2.45e9, sample_rate=2e6, duration_s=3,
                                gain_db=40, adapter="generic-soapy", device_caps=dev)
    assert plan.direction == "rx" and plan.adapter == "generic-soapy"


def test_simulation_uses_explicit_synthetic_caps_not_physical():
    # simulation is NOT physical: its explicit synthetic envelope applies; never reported as hw
    plan = rfc.validate_capture(device_id="sim-0", freq_hz=2.4e9, sample_rate=2e6, duration_s=3,
                                gain_db=40, adapter="simulation")
    assert plan.direction == "rx" and plan.physically_qualified is False
    # out-of-band against the synthetic envelope is still rejected
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="sim-0", freq_hz=10e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="simulation")
    assert exc.value.category == "frequency_out_of_band"


def test_physical_duration_enforced_before_anything():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="g-1", freq_hz=1e9, sample_rate=2e6, duration_s=999,
                             gain_db=40, adapter="generic-soapy")
    assert exc.value.category == "duration_too_long"


# --- bounded operator override: explicit, device-specific, never model-supplied ----
def test_operator_override_supplies_bounded_envelope():
    # an operator-configured bounded capability set lets a non-reporting device be authorized
    override = caps.RFCapabilities("operator-override", rx_capable=True, requires_hardware=True,
                                   freq_min_hz=2.40e9, freq_max_hz=2.48e9,
                                   sample_rate_min=1e6, sample_rate_max=10e6,
                                   gain_min_db=0, gain_max_db=50)
    plan = rfc.validate_capture(device_id="g-1", freq_hz=2.45e9, sample_rate=2e6, duration_s=3,
                                gain_db=40, adapter="generic-soapy", operator_caps=override)
    assert plan.direction == "rx"
    # the override is BOUNDED: a request outside it is still rejected
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="g-1", freq_hz=5.0e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="generic-soapy", operator_caps=override)
    assert exc.value.category == "frequency_out_of_band"


def test_model_request_cannot_widen_bounds():
    # the request's freq/rate are validated AGAINST the envelope; they are never used AS bounds.
    # A coordinator asking for an out-of-band frequency on Pluto is rejected, not honoured.
    with pytest.raises(rfc.ConstraintViolation) as exc:
        rfc.validate_capture(device_id="pluto-0", freq_hz=6.5e9, sample_rate=2e6, duration_s=3,
                             gain_db=40, adapter="plutosdr")
    assert exc.value.category == "frequency_out_of_band"


# --- capture() WIRING: rejection happens BEFORE the subprocess runs --------
def test_capture_boundary_rejects_before_subprocess(monkeypatch):
    from aithernet.hardware.capture import CaptureError, CaptureService

    async def _noop(**kw):  # runtime._emit_event stub
        return None

    svc = object.__new__(CaptureService)            # bypass heavy __init__
    svc.qcfg = types.SimpleNamespace(max_duration_seconds=10.0)
    svc.runtime = types.SimpleNamespace(_emit_event=_noop)

    async def _must_not_run(*a, **k):
        raise AssertionError("capture subprocess ran despite a constraint violation")

    svc._run_script = _must_not_run
    device = types.SimpleNamespace(id="pluto-0", driver="plutosdr", capabilities=None)
    with pytest.raises(CaptureError) as exc:
        asyncio.run(svc.capture(device=device, freq_hz=10_000_000_000.0, sample_rate=2e6,
                                gain_db=40.0, duration_s=3.0))
    assert "frequency_out_of_band" in str(exc.value)


def test_capture_boundary_allows_valid_then_reaches_source(monkeypatch):
    # a valid Pluto receive request passes the gate and proceeds to the canonical capture path
    from aithernet.hardware.capture import CaptureError, CaptureService

    async def _noop(**kw):
        return None

    reached = {"source": False}

    svc = object.__new__(CaptureService)
    svc.qcfg = types.SimpleNamespace(max_duration_seconds=10.0)
    svc.runtime = types.SimpleNamespace(_emit_event=_noop)

    def _resolve_source(device):
        reached["source"] = True
        raise CaptureError("stop after the gate (canonical source path reached)")

    svc.resolve_source = _resolve_source
    device = types.SimpleNamespace(id="pluto-0", driver="plutosdr", capabilities=None)
    with pytest.raises(CaptureError):
        asyncio.run(svc.capture(device=device, freq_hz=2.437e9, sample_rate=2e6,
                                gain_db=40.0, duration_s=3.0))
    assert reached["source"] is True                # the valid request reached the canonical route
