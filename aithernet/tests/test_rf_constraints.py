"""Tests for the deterministic receive-only RF mission constraint gate (Stage 14G, PHASE 8).

The central guarantee: the AI coordinator can never widen the envelope. These tests prove transmit
is impossible, out-of-band/over-duration requests are rejected, gain + artifact limits are clamped
DOWN (never up), the objective text is NOT interpreted (the gate is parameter-level), and the
authorized plan carries a stable audit digest. Pure — no hardware, no model.
"""

from __future__ import annotations

import pytest

from aithernet.missions import rf_constraints as rfc

# Resolve a concrete envelope from the plutosdr adapter (device-specific limits live there now).
_PLUTO = rfc.resolve_bounds("plutosdr")


def _req(**kw):
    base = dict(objective="survey occupancy", device_id="pluto-0",
                center_frequency_hz=2_437_000_000.0, sample_rate=2_000_000.0, duration_s=3.0)
    base.update(kw)
    return rfc.RFMissionRequest(**base)


def _validate(req, bounds=_PLUTO):
    return rfc.validate(req, bounds)


# --- the receive-only gate -------------------------------------------------
@pytest.mark.parametrize("direction", ["tx", "rx_tx", "txrx", "transmit", "both", "RX_TX", ""])
def test_transmit_is_always_rejected(direction):
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(direction=direction))
    assert exc.value.category == "transmit_forbidden"


def test_receive_only_is_authorized():
    plan = _validate(_req())
    assert plan.direction == "rx"
    assert plan.digest


def test_objective_text_is_not_interpreted():
    # a hostile-sounding objective with a RECEIVE-ONLY request is still authorized — the gate is
    # parameter-level (direction=rx), never a text classifier. No transmit can result.
    plan = _validate(_req(objective="jam and transmit on the band"))
    assert plan.direction == "rx"


# --- bounds: rejected (not silently moved) ---------------------------------
@pytest.mark.parametrize("freq", [10_000_000.0, 7_000_000_000.0])
def test_frequency_out_of_band_rejected(freq):
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(center_frequency_hz=freq))
    assert exc.value.category == "frequency_out_of_band"


@pytest.mark.parametrize("rate", [1000.0, 200_000_000.0])
def test_sample_rate_out_of_range_rejected(rate):
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(sample_rate=rate))
    assert exc.value.category == "sample_rate_out_of_range"


def test_duration_over_ceiling_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(duration_s=999.0))
    assert exc.value.category == "duration_too_long"


def test_nonpositive_duration_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(duration_s=0))
    assert exc.value.category == "invalid_duration"


# --- objective + device ----------------------------------------------------
def test_missing_objective_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(objective="   "))
    assert exc.value.category == "missing_objective"


def test_oversized_objective_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(objective="x" * 2001))
    assert exc.value.category == "objective_too_long"


def test_missing_device_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(device_id=""))
    assert exc.value.category == "missing_device"


def test_device_allowlist_enforced():
    # operator policy narrows the allowed device set; resolved against the pluto adapter
    bounds = rfc.resolve_bounds("plutosdr",
                                policy=rfc.GlobalRxPolicy(allowed_devices=("pluto-A",)))
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(device_id="pluto-B"), bounds)
    assert exc.value.category == "device_unauthorized"
    # the authorized device passes
    assert _validate(_req(device_id="pluto-A"), bounds).device_id == "pluto-A"


# --- clamping (down, never up) ---------------------------------------------
def test_gain_clamped_down():
    plan = _validate(_req(gain_db=500))
    assert plan.gain_db == _PLUTO.gain_max_db
    assert "gain_db" in plan.clamped


def test_artifact_limits_clamped_down_never_up():
    b = _PLUTO
    # request MORE than the bound -> clamped to the bound
    over = _validate(_req(artifact_max_bytes=b.artifact_max_bytes * 10,
                             artifact_max_count=b.artifact_max_count + 100))
    assert over.artifact_max_bytes == b.artifact_max_bytes
    assert over.artifact_max_count == b.artifact_max_count
    assert "artifact_max_bytes" in over.clamped and "artifact_max_count" in over.clamped
    # request LESS than the bound -> honoured (a mission may ask for less)
    less = _validate(_req(artifact_max_bytes=1024, artifact_max_count=2))
    assert less.artifact_max_bytes == 1024 and less.artifact_max_count == 2
    assert less.clamped == ()


def test_invalid_artifact_limits_rejected():
    with pytest.raises(rfc.ConstraintViolation) as exc:
        _validate(_req(artifact_max_count=0))
    assert exc.value.category == "invalid_artifact_limits"


# --- audit digest ----------------------------------------------------------
def test_digest_is_deterministic_and_param_sensitive():
    a = _validate(_req())
    b = _validate(_req())
    assert a.digest == b.digest                       # deterministic
    c = _validate(_req(center_frequency_hz=2_412_000_000.0))
    assert c.digest != a.digest                       # changes with the capture parameters


def test_explain_bounds_is_safe():
    out = rfc.explain_bounds(_PLUTO)
    assert out["direction"].startswith("rx")
    assert out["frequency_hz"][0] < out["frequency_hz"][1]
    assert out["duration_s_max"] > 0
    assert out["adapter"] == "plutosdr"
    assert out["physically_qualified"] is False     # not tested on real hardware yet
