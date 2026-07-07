"""Stage 14C.1 tests: real-provider probe PARSERS (sanitized fixtures), qualification persistence,
support classification, provenance, and API. No physical SDR hardware is required — the fixtures
are sanitized captures of real SoapySDR/UHD output (serials/URIs/host facts replaced), and a fake
discovery provider exercises persistence. Physical acceptance evidence comes from real hardware
(see the Stage 14C.1 report), never from these synthetic fixtures.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from _hw_util import make_hw_runtime
from aithernet.api.app import create_app
from aithernet.hardware.discovery import descriptor_to_discovered
from aithernet.hardware.probing import (
    parse_soapy_find,
    parse_soapy_probe,
    parse_uhd_find,
    soapy_find_to_descriptors,
    soapy_probe_serial,
    soapy_probe_to_capabilities,
    uhd_find_to_descriptors,
)
from aithernet.hardware.qualification import _rank
from aithernet.state.repositories import (
    HardwareQualificationCheckRepository,
    HardwareQualificationRunRepository,
    HardwareRFMeasurementRepository,
)

run = asyncio.run

# -- sanitized fixtures (real SoapyPlutoSDR output with serial/uri/host facts replaced) -------

SOAPY_FIND = """\
######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Found device 0
  device = PlutoSDR
  driver = plutosdr
  label = PlutoSDR #0 ip:device.local
  uri = ip:device.local

Found device 1
  device = PlutoSDR
  driver = plutosdr
  label = PlutoSDR #1 ip:device.local
  uri = ip:device.local
"""

SOAPY_PROBE = """\
######################################################
##     Soapy SDR -- the SDR abstraction library     ##
######################################################

Probe device driver=plutosdr

----------------------------------------------------
-- Device identification
----------------------------------------------------
  driver=PlutoSDR
  hardware=ADALM-PLUTO
  ad9361-phy,model=ad9363a
  fw_version=vX.Y
  hw_model=Analog Devices PlutoSDR Rev.B (Z7010-AD9363A)
  hw_serial=FAKE0SERIAL0001
  uri=ip:device.local

----------------------------------------------------
-- Peripheral summary
----------------------------------------------------
  Channels: 1 Rx, 1 Tx
  Timestamps: NO

----------------------------------------------------
-- RX Channel 0
----------------------------------------------------
  Full-duplex: YES
  Supports AGC: YES
  Stream formats: CS8, CS12, CS16, CF32
  Native format: CS16 [full-scale=2048]
  Antennas: A_BALANCED
  Full gain range: [0, 73] dB
  Full freq range: [70, 6000] MHz
  Sample rates: [0.0651042, 61.44] MSps
  Filter bandwidths: 0.2, 1, 2, 3, 4 MHz

----------------------------------------------------
-- TX Channel 0
----------------------------------------------------
  Full-duplex: YES
  Stream formats: CS8, CS12, CS16, CF32
  Antennas: A
  Full gain range: [0, 89] dB
  Full freq range: [70, 6000] MHz
  Sample rates: [0.0651042, 61.44] MSps
"""

UHD_FIND = """\
--------------------------------------------------
-- UHD Device 0
--------------------------------------------------
Device Address:
    serial: 30AD3F8
    name: MyB210
    product: B210
    type: b200
"""


# -- Soapy find parsing + dedup --------------------------------------------------------------


def test_soapy_find_parses_two_entries():
    blocks = parse_soapy_find(SOAPY_FIND)
    assert len(blocks) == 2
    assert blocks[0]["driver"] == "plutosdr"
    assert blocks[0]["uri"] == "ip:device.local"


def test_soapy_find_dedup_collapses_to_one_device():
    descs = soapy_find_to_descriptors("soapy", SOAPY_FIND)
    keys = {descriptor_to_discovered("soapy", d).hardware_key for d in descs}
    assert len(keys) == 1  # PlutoSDR #0/#1 share the uri -> ONE stable identity
    # product comes from the stable `device` field, never the #N label
    assert all(d["product"] == "PlutoSDR" for d in descs)
    assert all("#" not in (d["product"] or "") for d in descs)


def test_soapy_find_uses_uri_not_label_for_identity():
    descs = soapy_find_to_descriptors("soapy", SOAPY_FIND)
    dev = descriptor_to_discovered("soapy", descs[0])
    assert dev.transport == "ip:device.local"
    assert "#0" not in (dev.product or "") and "#1" not in (dev.product or "")


# -- Soapy probe parsing ---------------------------------------------------------------------


def test_soapy_probe_parses_capabilities():
    p = parse_soapy_probe(SOAPY_PROBE)
    assert soapy_probe_serial(p) == "FAKE0SERIAL0001"
    assert p["rx_channels"] == 1 and p["tx_channels"] == 1
    assert p["hardware_timestamp"] is False
    caps = soapy_probe_to_capabilities(p, driver="plutosdr")
    assert caps.rx_supported is True and caps.tx_supported is True
    assert caps.full_duplex is True
    assert caps.frequency_ranges[0].min_hz == 70e6 and caps.frequency_ranges[0].max_hz == 6e9
    assert caps.sample_rate_ranges[0].max == 61.44e6
    assert "CF32" in caps.stream_formats
    assert "A_BALANCED" in caps.antennas and "A" in caps.antennas
    assert caps.source == "probed" and caps.confidence == "high"
    assert caps.extra["serial"] == "FAKE0SERIAL0001"
    assert caps.extra["tx"]["gain_range"] == [0.0, 89.0]
    assert caps.device_args["driver"] == "plutosdr"


def test_soapy_probe_unknown_stays_unknown():
    minimal = """\
-- Device identification
  driver=Generic
-- Peripheral summary
  Channels: 1 Rx, 0 Tx
"""
    p = parse_soapy_probe(minimal)
    caps = soapy_probe_to_capabilities(p, driver="generic")
    assert caps.frequency_ranges == []   # not reported -> unknown, never inferred
    assert caps.sample_rate_ranges == []
    assert caps.tx_supported is False  # explicitly 0 Tx
    assert caps.hardware_timestamp is None  # not reported


def test_soapy_probe_malformed_does_not_crash():
    for bad in ("", "garbage\nlines\n", "-- Device identification\nno equals here\n"):
        p = parse_soapy_probe(bad)
        caps = soapy_probe_to_capabilities(p, driver=None)
        assert caps.source == "probed"


def test_uhd_find_parser():
    descs = uhd_find_to_descriptors("uhd", UHD_FIND)
    assert len(descs) == 1
    assert descs[0]["serial"] == "30AD3F8"
    assert descs[0]["product"] == "B210"
    assert parse_uhd_find(UHD_FIND)[0]["type"] == "b200"


# -- provider failure preserves capabilities (Part B.9) --------------------------------------


def test_probe_failure_preserves_existing_capabilities(tmp_path):
    from _hw_util import descriptor

    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor(channels=2)])
    run(rt.hardware_inventory.refresh())
    from aithernet.state.repositories import SDRDeviceCapabilityRepository, SDRDeviceRepository
    with rt.session_scope() as s:
        dev_id = SDRDeviceRepository(s).list(node_id=rt.config.node_id)[0].id
        before = SDRDeviceCapabilityRepository(s).get_for_device(dev_id).capabilities_json

    # A provider whose probe now FAILS (returns None) must not corrupt the stored capabilities.
    async def _none_probe(device):
        return None

    fake.probe = _none_probe
    run(rt.hardware_inventory.refresh(force_probe=True))
    with rt.session_scope() as s:
        after = SDRDeviceCapabilityRepository(s).get_for_device(dev_id).capabilities_json
    assert after == before  # unchanged


# -- support classification ------------------------------------------------------------------


def test_support_classification_rank_order():
    assert _rank("discovered") < _rank("probed") < _rank("rx_qualified")
    assert _rank("rx_qualified") < _rank("capture_qualified")
    assert _rank("capture_qualified") < _rank("survey_qualified")
    assert _rank("failed") == 0
    assert _rank("nonsense") == 0


# -- qualification persistence + provenance + API --------------------------------------------


def _seed_qualification(rt, device_id):
    with rt.session_scope() as s:
        run_row = HardwareQualificationRunRepository(s).create(
            node_id=rt.config.node_id, device_id=device_id, hardware_key="soapy:abc",
            provider_id="fake", status="passed", support_classification="capture_qualified",
            capability_revision=1,
            environment_json={"gnuradio_version": "3.10.9.2", "soapysdr_version": "v0.8.1",
                              "os": "Ubuntu 24.04", "host_id": "host-deadbeef"},
            checks_total=2, checks_passed=2)
        run_id = run_row.id
        HardwareQualificationCheckRepository(s).create(
            run_id=run_id, node_id=rt.config.node_id, device_id=device_id, name="physical_capture",
            category="capture", status="passed", detail="48000000 bytes",
            evidence_json={"bytes": 48000000, "sample_count": 6000000, "digest": "sha256:abc"})
        HardwareRFMeasurementRepository(s).create(
            node_id=rt.config.node_id, device_id=device_id, qualification_run_id=run_id,
            capability_revision=1, lease_id="lease-1", backend_id="legacy_gr_mcp", kind="capture",
            center_frequency_hz=2.437e9, sample_rate_sps=2e6, gain_db=40.0, antenna="A_BALANCED",
            channel="0", stream_format="CF32", capture_bytes=48000000, sample_count=6000000,
            sha256="sha256:abc", artifact_id="art-1", processing_revision="14c1-gr-capture-1",
            confidence="high")
        s.commit()
    return run_id


def test_qualification_persistence_and_provenance(tmp_path):
    from _hw_util import descriptor

    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    from aithernet.state.repositories import SDRDeviceRepository
    with rt.session_scope() as s:
        dev_id = SDRDeviceRepository(s).list(node_id=rt.config.node_id)[0].id
    run_id = _seed_qualification(rt, dev_id)
    with rt.session_scope() as s:
        m = HardwareRFMeasurementRepository(s).list(node_id=rt.config.node_id)[0]
        # provenance fields (Part I)
        assert m.device_id == dev_id and m.lease_id == "lease-1"
        assert m.capability_revision == 1 and m.backend_id == "legacy_gr_mcp"
        assert m.center_frequency_hz == 2.437e9 and m.sample_rate_sps == 2e6
        assert m.sha256 == "sha256:abc" and m.capture_bytes == 48000000
        assert m.processing_revision == "14c1-gr-capture-1"
    with _client(rt) as c:
        runs = c.get("/hardware/qualifications").json()
        assert len(runs) == 1 and runs[0]["support_classification"] == "capture_qualified"
        detail = c.get(f"/hardware/qualifications/{run_id}").json()
        assert detail["environment"]["gnuradio_version"] == "3.10.9.2"
        checks = c.get(f"/hardware/qualifications/{run_id}/checks").json()
        assert any(ch["name"] == "physical_capture" and ch["status"] == "passed" for ch in checks)
        status = c.get(f"/hardware/devices/{dev_id}/qualification-status").json()
        assert status["qualified"] is True
        matrix = c.get("/hardware/support-matrix").json()
        assert matrix[0]["classification"] == "capture_qualified"
        meas = c.get("/hardware/measurements").json()
        assert meas[0]["sha256"] == "sha256:abc"


def test_qualification_report_exposes_no_raw_paths(tmp_path):
    import json as _json

    from _hw_util import descriptor

    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    from aithernet.state.repositories import SDRDeviceRepository
    with rt.session_scope() as s:
        dev_id = SDRDeviceRepository(s).list(node_id=rt.config.node_id)[0].id
    run_id = _seed_qualification(rt, dev_id)
    with _client(rt) as c:
        blob = _json.dumps(c.get(f"/hardware/qualifications/{run_id}").json())
        blob += _json.dumps(c.get("/hardware/measurements").json())
        assert "/home/" not in blob and "/dev/" not in blob and "/tmp/" not in blob


def test_qualification_run_gated_when_operator_disabled(tmp_path):
    from _hw_util import descriptor

    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()], operator=False)
    run(rt.hardware_inventory.refresh())
    from aithernet.state.repositories import SDRDeviceRepository
    with rt.session_scope() as s:
        dev_id = SDRDeviceRepository(s).list(node_id=rt.config.node_id)[0].id
    with _client(rt) as c:
        # preflight is read-only (allowed); the RF run is operator-gated.
        assert c.post("/hardware/qualifications/preflight",
                      json={"device_id": dev_id}).status_code == 200
        assert c.post("/hardware/qualifications/run",
                      json={"device_id": dev_id}).status_code == 403


def _client(rt) -> TestClient:
    return TestClient(create_app(runtime=rt))
