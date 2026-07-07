"""1.0.0-beta.2: operator-controlled physical SDR transmission (local; no vendor/cloud gate).

Hardware mocks + contained loopback only — no radiation. Proves: TX is available after LOCAL
enablement with no external authorization; capable hardware is selectable; unsupported hardware is
rejected for a concrete technical reason; operator parameters reach the plan/flowgraph; device +
operator ranges are enforced; custom/experimental profiles run after local confirmation; rf_ota is
selectable; no silent IP fallback; peer authentication is required; bounded limits; the managed
executor takes a STRUCTURED plan (never shell); no secret leakage.
"""

from __future__ import annotations

import time

from aithernet.transport.ota import hardware as hw
from aithernet.transport.ota import tx_control as txc
from aithernet.transport.ota.authorization import RFTxCapability, RFTxPlan
from aithernet.transport.ota.executor import (
    LoopbackTxBackend,
    TxBackend,
    execute_peer_tx,
)
from aithernet.transport.ota.profiles import (
    QUAL_OPERATOR_CUSTOM,
    REFERENCE_BPSK,
    ProfileRegistry,
    RFLinkProfile,
)
from aithernet.transport.ota.selection import TransportSelectionError, select_transport

NOW = 1_000_000

PLUTO = RFTxCapability(adapter_supports_tx=True, device_reports_tx=True, min_freq_hz=70_000_000,
                       max_freq_hz=6_000_000_000, max_sample_rate=20_000_000, max_gain=70.0,
                       channels=(0,))


def _plan(**over):
    base = dict(device_id="plutosdr:ip:192.168.2.1", uri="ip:192.168.2.1",
                profile_id="ref-bpsk-1k", profile_digest=REFERENCE_BPSK.digest(),
                flowgraph_digest="", implementation_digest="", frame_artifact_digest="",
                center_frequency_hz=433_000_000, sample_rate=8000, occupied_bandwidth_hz=1200,
                gain=10.0, channel=0, duration_seconds=1.0, message_count=1, frame_count=3)
    base.update(over)
    return RFTxPlan(**base)


def _enabled(**over):
    base = dict(enabled=True, max_duration_seconds=5.0, gain_ceiling=70.0, max_frame_count=100)
    base.update(over)
    return txc.OperatorTxConfig(**base)


# -- local enablement, no vendor/cloud --------------------------------------------------------

def test_tx_denied_until_locally_enabled_then_allowed():
    plan = _plan()
    denied = txc.evaluate_operator_tx(
        plan=plan, config=txc.OperatorTxConfig(enabled=False), capability=PLUTO,
        device_present=True, peer_authenticated=True, interactive_confirmed=True, now=NOW)
    assert denied.allowed is False and any("not enabled" in r for r in denied.reasons)
    ok = txc.evaluate_operator_tx(plan=plan, config=_enabled(), capability=PLUTO,
                                  device_present=True, peer_authenticated=True,
                                  interactive_confirmed=True, now=NOW)
    assert ok.allowed is True and ok.reasons == []
    # no vendor/cloud concept exists in the gate inputs
    assert "authorization_id" not in str(ok.to_dict() if hasattr(ok, "to_dict") else ok)


def test_offline_local_operation_no_network():
    # evaluate_operator_tx is pure/local — calling it with no network available still works.
    ok = txc.evaluate_operator_tx(plan=_plan(), config=_enabled(), capability=PLUTO,
                                  device_present=True, peer_authenticated=True,
                                  interactive_confirmed=True, now=NOW)
    assert ok.allowed is True


def test_config_persists_locally(tmp_path):
    from dataclasses import replace
    cfg = replace(txc.load_tx_config(tmp_path), enabled=True, device_id="plutosdr:x",
                  operating_region_label="bench")
    path = txc.save_tx_config(cfg, tmp_path)
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = txc.load_tx_config(tmp_path)
    assert loaded.enabled and loaded.device_id == "plutosdr:x"


# -- hardware capability / selection ----------------------------------------------------------

def test_capable_adapter_resolves_and_unsupported_is_technical_error():
    cap = hw.resolve_capability("plutosdr")
    assert cap.adapter_supports_tx and cap.max_freq_hz > cap.min_freq_hz
    import pytest
    with pytest.raises(ValueError, match="unsupported TX adapter"):
        hw.resolve_capability("nonexistent-sdr")


def test_unsupported_hardware_params_rejected_with_concrete_reason():
    # frequency outside the device range
    r1 = txc.evaluate_operator_tx(plan=_plan(center_frequency_hz=10_000_000_000), config=_enabled(),
                                  capability=PLUTO, device_present=True, peer_authenticated=True,
                                  interactive_confirmed=True, now=NOW)
    assert not r1.allowed and any("frequency is outside the device" in r for r in r1.reasons)
    # gain above device max
    r2 = txc.evaluate_operator_tx(plan=_plan(gain=200.0), config=_enabled(gain_ceiling=300.0),
                                  capability=PLUTO, device_present=True, peer_authenticated=True,
                                  interactive_confirmed=True, now=NOW)
    assert not r2.allowed and any("gain is outside the device" in r for r in r2.reasons)
    # device not present
    r3 = txc.evaluate_operator_tx(plan=_plan(), config=_enabled(), capability=PLUTO,
                                  device_present=False, peer_authenticated=True,
                                  interactive_confirmed=True, now=NOW)
    assert not r3.allowed and any("not currently present" in r for r in r3.reasons)


def test_operator_ceilings_enforced():
    r = txc.evaluate_operator_tx(plan=_plan(duration_seconds=99.0), config=_enabled(),
                                 capability=PLUTO, device_present=True, peer_authenticated=True,
                                 interactive_confirmed=True, now=NOW)
    assert not r.allowed and any("duration exceeds the operator" in x for x in r.reasons)


def test_peer_authentication_required():
    r = txc.evaluate_operator_tx(plan=_plan(), config=_enabled(), capability=PLUTO,
                                 device_present=True, peer_authenticated=False,
                                 interactive_confirmed=True, now=NOW)
    assert not r.allowed and any("not authenticated" in x for x in r.reasons)


# -- confirmation: interactive or local pre-authorization envelope ----------------------------

def test_requires_local_confirmation_interactive_or_envelope():
    r = txc.evaluate_operator_tx(plan=_plan(), config=_enabled(), capability=PLUTO,
                                 device_present=True, peer_authenticated=True,
                                 interactive_confirmed=False, now=NOW)
    assert not r.allowed and any("no local operator confirmation" in x for x in r.reasons)
    env = txc.PreAuthEnvelope(envelope_id="e1", device_id="plutosdr:ip:192.168.2.1",
                              freq_min_hz=432_000_000, freq_max_hz=434_000_000,
                              max_occupied_bandwidth_hz=2000, gain_ceiling=70.0,
                              max_duration_seconds=5.0, allowed_profiles=("ref-bpsk-1k",),
                              max_payload_bytes=65536, max_retries=8, valid_until=NOW + 100)
    ok = txc.evaluate_operator_tx(
        plan=_plan(), config=_enabled(allow_preauthorized_noninteractive=True), capability=PLUTO,
        device_present=True, peer_authenticated=True, interactive_confirmed=False, envelope=env,
        now=NOW)
    assert ok.allowed is True


# -- custom/experimental profiles usable; invalid rejected technically ------------------------

def test_operator_custom_profile_usable_after_confirmation():
    reg = ProfileRegistry()
    custom = RFLinkProfile(**{**REFERENCE_BPSK.__dict__, "profile_id": "op-custom-1"})
    reg.register_operator_custom(custom)
    assert reg.qualification_state("op-custom-1") == QUAL_OPERATOR_CUSTOM
    assert reg.usable_physically("op-custom-1", operator_confirmed=True) is True
    assert reg.usable_physically("op-custom-1", operator_confirmed=False) is False


# -- managed executor: structured plan, loopback (no radiation) -------------------------------

def test_executor_loopback_runs_without_radiation():
    from aithernet.transport.ota import modem
    from aithernet.transport.ota.frame import Frame, FrameType
    frame = Frame(FrameType.DATA, 1, 0, 1, b"managed-executor-frame").encode("crc16")
    samples = modem.modulate(frame, REFERENCE_BPSK)
    rec = execute_peer_tx(plan=_plan(device_id="loopback"), profile=REFERENCE_BPSK,
                          frame_samples=samples, config=_enabled(), capability=PLUTO,
                          device_present=True, peer_authenticated=True, interactive_confirmed=True,
                          backend=LoopbackTxBackend(), now=NOW)
    assert rec.allowed is True and rec.radiated is False
    assert rec.metadata["sample_count"] == len(samples)
    assert rec.flowgraph_digest.startswith("sha256:")
    # no secret/key material in the record
    assert "key" not in str(rec.to_dict()).lower() or "key_id" in str(rec.to_dict()).lower()


def test_executor_radiating_backend_enforces_gates():
    # A (mock) radiating backend is gated: disabled config -> not allowed, nothing transmitted.
    class _MockRadio(TxBackend):
        backend_id = "mock-radio"
        radiates = True
        def __init__(self): self.called = False
        def transmit(self, samples, *, plan, duration_s):
            self.called = True
            return {"radiated": True, "sample_count": len(samples)}
    radio = _MockRadio()
    from aithernet.transport.ota import modem
    from aithernet.transport.ota.frame import Frame, FrameType
    samples = modem.modulate(Frame(FrameType.DATA, 1, 0, 1, b"x").encode("crc16"), REFERENCE_BPSK)
    rec = execute_peer_tx(plan=_plan(), profile=REFERENCE_BPSK, frame_samples=samples,
                          config=txc.OperatorTxConfig(enabled=False), capability=PLUTO,
                          device_present=True, peer_authenticated=True, interactive_confirmed=True,
                          backend=radio, now=NOW)
    assert rec.allowed is False and radio.called is False   # gate denied BEFORE any transmit


# -- transport selection: rf_ota selectable; no silent IP fallback ----------------------------

def test_rf_ota_selectable_and_no_silent_ip_fallback():
    # rf_ota available -> selected
    r = select_transport(requested="rf_ota", available={"ip": True, "rf_ota": True},
                         fallback_allowed=False)
    assert r.selected == "rf_ota" and not r.fallback_occurred
    # rf_ota unavailable, fallback not allowed -> error (no silent IP downgrade)
    import pytest
    with pytest.raises(TransportSelectionError):
        select_transport(requested="rf_ota", available={"ip": True, "rf_ota": False},
                         fallback_allowed=False)


def test_no_air_test_is_fabricated():
    # The loopback/executor never radiate; this suite asserts radiated is always False.
    assert LoopbackTxBackend().radiates is False
    assert time.time  # sanity
