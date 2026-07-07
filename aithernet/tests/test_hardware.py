"""Stage 14B managed-hardware tests: discovery/identity, capabilities, leasing, binding, API,
and ops integration. No physical SDR hardware is required — an explicit fake discovery backend is
injected through the registry, and the legacy RF backend id is used for bindings.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from _hw_util import FakeDiscoveryBackend, descriptor, make_hw_runtime
from aithernet.api.app import create_app
from aithernet.config.settings import (
    HardwareBindingConfig,
    HardwareConfig,
    HardwareDiscoveryProviderConfig,
    NodeConfig,
)
from aithernet.hardware.contracts import (
    BindingNotFoundError,
    DeviceUnavailableError,
    LeaseCompatibilityError,
    LeaseConflictError,
)
from aithernet.hardware.identity import compute_hardware_key, is_usable_serial
from aithernet.hardware.registry import HardwareDiscoveryRegistry
from aithernet.state.models import MissionStep
from aithernet.state.repositories import (
    MissionRepository,
    SDRDeviceLeaseRepository,
    SDRDeviceRepository,
)

run = asyncio.run


def _devices(runtime):
    with runtime.session_scope() as session:
        return SDRDeviceRepository(session).list(node_id=runtime.config.node_id)


def _first_device_id(runtime):
    devs = _devices(runtime)
    assert devs
    return devs[0].id


# -- discovery & identity --------------------------------------------------------------------


def test_provider_unavailable_reports_no_devices(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    fake.available_flag = False
    avail = run(rt.hardware_inventory.refresh())
    assert avail == {"fake": False}
    assert _devices(rt) == []


def test_one_device_discovered_and_persisted(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    devs = _devices(rt)
    assert len(devs) == 1
    assert devs[0].presence_state == "present"
    assert devs[0].status == "present"
    assert devs[0].vendor == "Ettus"


def test_repeated_discovery_updates_same_record(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    first_id = _first_device_id(rt)
    run(rt.hardware_inventory.refresh())
    devs = _devices(rt)
    assert len(devs) == 1
    assert devs[0].id == first_id  # updated, not duplicated


def test_enumeration_order_change_keeps_identity(tmp_path):
    d1 = descriptor(serial="AAA")
    d2 = descriptor(serial="BBB", product="N210")
    rt, fake = make_hw_runtime(tmp_path, descriptors=[d1, d2])
    run(rt.hardware_inventory.refresh())
    ids = {d.serial: d.id for d in _devices(rt)}
    fake.set_descriptors([d2, d1])  # reordered enumeration
    run(rt.hardware_inventory.refresh())
    ids2 = {d.serial: d.id for d in _devices(rt)}
    assert ids == ids2  # no new device from a reordered list


def test_duplicate_serial_across_providers_does_not_collide(tmp_path):
    rt, fake_a = make_hw_runtime(tmp_path, descriptors=[descriptor(serial="DUP")],
                                 provider_id="alpha")
    fake_b = FakeDiscoveryBackend("beta", [descriptor(serial="DUP")])
    rt.hardware_discovery.register(fake_b)
    run(rt.hardware_inventory.refresh())
    devs = _devices(rt)
    assert len(devs) == 2  # same serial, different providers -> distinct records
    assert len({d.hardware_key for d in devs}) == 2


def test_no_serial_is_identity_limited_but_stable(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor(serial=None)])
    run(rt.hardware_inventory.refresh())
    devs = _devices(rt)
    assert len(devs) == 1
    assert devs[0].identity_limited is True
    first_id = devs[0].id
    run(rt.hardware_inventory.refresh())
    assert _first_device_id(rt) == first_id  # stable across restarts despite no serial


def test_missing_then_returning_device(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    fake.set_descriptors([])  # device disappears
    run(rt.hardware_inventory.refresh())
    with rt.session_scope() as s:
        dev = SDRDeviceRepository(s).get(dev_id)
        assert dev.presence_state == "missing"
        assert dev.status == "missing"
    fake.set_descriptors([descriptor()])  # device returns
    run(rt.hardware_inventory.refresh())
    with rt.session_scope() as s:
        dev = SDRDeviceRepository(s).get(dev_id)
        assert dev.presence_state == "present"
    assert len(_devices(rt)) == 1  # not duplicated


def test_provider_failure_does_not_crash(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    fake.fail_discover = True
    avail = run(rt.hardware_inventory.refresh())
    assert avail == {"fake": True}  # available, but discover failed (no crash)
    assert _devices(rt) == []


def test_metadata_is_bounded_and_sanitized(tmp_path):
    desc = descriptor(metadata={"device_path": "/dev/bus/usb/001/004", "label": "ok", "x" * 80: 1})
    rt, _ = make_hw_runtime(tmp_path, descriptors=[desc])
    run(rt.hardware_inventory.refresh())
    meta = _devices(rt)[0].metadata_json
    assert "device_path" not in meta  # forbidden key dropped
    assert meta.get("label") == "ok"


def test_production_registry_invents_no_devices():
    reg = HardwareDiscoveryRegistry(HardwareConfig(enabled=True))
    assert reg.provider_ids() == []
    assert reg.has_providers is False


def test_identity_helpers():
    assert is_usable_serial("ABC123") is True
    assert is_usable_serial("00000000") is False
    assert is_usable_serial(None) is False
    k1, lim1 = compute_hardware_key(provider_id="p", driver="uhd", vendor="v", product="x",
                                    serial="S1")
    k2, _ = compute_hardware_key(provider_id="p", driver="uhd", vendor="v", product="x",
                                 serial="S1")
    assert k1 == k2 and lim1 is False
    _, lim2 = compute_hardware_key(provider_id="p", driver="uhd", vendor="v", product="x",
                                   serial=None)
    assert lim2 is True


# -- capabilities ----------------------------------------------------------------------------


def _caps(runtime, device_id):
    from aithernet.state.repositories import SDRDeviceCapabilityRepository
    with runtime.session_scope() as session:
        c = SDRDeviceCapabilityRepository(session).get_for_device(device_id)
        return c.capabilities_json if c else None


def test_capabilities_rx_only_tx_only_duplex(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[
        descriptor(serial="RX", rx=True, tx=False),
        descriptor(serial="TX", rx=False, tx=True, product="TXdev"),
        descriptor(serial="DUP", rx=True, tx=True, product="Duplex"),
    ])
    run(rt.hardware_inventory.refresh())
    by_serial = {d.serial: d.id for d in _devices(rt)}
    assert _caps(rt, by_serial["RX"])["rx_supported"] is True
    assert _caps(rt, by_serial["RX"])["tx_supported"] is False
    assert _caps(rt, by_serial["TX"])["tx_supported"] is True
    assert _caps(rt, by_serial["DUP"])["rx_supported"] is True


def test_capabilities_ranges_and_channels(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[
        descriptor(channels=4, freq=((1e6, 2e9),), sample_rates=((1e6, 10e6),))])
    run(rt.hardware_inventory.refresh())
    cj = _caps(rt, _first_device_id(rt))
    assert cj["channel_count"] == 4
    assert cj["frequency_ranges"] == [[1e6, 2e9]]
    assert cj["sample_rate_ranges"] == [[1e6, 10e6]]


def test_capabilities_unknown_stays_unknown(tmp_path):
    bare = {"vendor": "X", "product": "Y", "serial": "Z", "driver": "d", "device_kind": "sdr"}
    rt, _ = make_hw_runtime(tmp_path, descriptors=[bare])
    run(rt.hardware_inventory.refresh())
    cj = _caps(rt, _first_device_id(rt))
    assert cj["rx_supported"] is None  # unknown, never inferred
    assert cj["tx_supported"] is None
    assert cj["channel_count"] is None


def test_capability_refresh_bumps_revision(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor(channels=2)])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    with rt.session_scope() as s:
        rev1 = SDRDeviceRepository(s).get(dev_id).capability_revision
    fake.set_descriptors([descriptor(channels=4)])  # capability changed
    run(rt.hardware_inventory.refresh())
    with rt.session_scope() as s:
        assert SDRDeviceRepository(s).get(dev_id).capability_revision > rev1


# -- leasing ---------------------------------------------------------------------------------


def _acquire(rt, device_id, **kw):
    kw.setdefault("backend_id", "legacy_gr_mcp")
    return run(rt.hardware_leases.acquire(device_id, **kw))


def test_acquire_exclusive_and_conflict(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    lease_id = _acquire(rt, dev_id)
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "active"
    with pytest.raises(LeaseConflictError):
        _acquire(rt, dev_id)


def test_shared_receive_supported_and_unsupported(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[
        descriptor(serial="OK", shared_receive_safe=True),
        descriptor(serial="NO", shared_receive_safe=False, product="NoShare"),
    ])
    run(rt.hardware_inventory.refresh())
    by_serial = {d.serial: d.id for d in _devices(rt)}
    a = _acquire(rt, by_serial["OK"], direction="rx", mode="shared_receive")
    b = _acquire(rt, by_serial["OK"], direction="rx", mode="shared_receive")
    assert a != b  # two shared-receive leases coexist
    with pytest.raises(LeaseCompatibilityError):
        _acquire(rt, by_serial["NO"], direction="rx", mode="shared_receive")


def test_shared_transmit_is_rejected(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor(shared_receive_safe=True)])
    run(rt.hardware_inventory.refresh())
    with pytest.raises(LeaseCompatibilityError):
        _acquire(rt, _first_device_id(rt), direction="tx", mode="shared_receive")


def test_renew_release_revoke(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    lease_id = _acquire(rt, dev_id)
    assert run(rt.hardware_leases.renew(lease_id)) is True
    assert run(rt.hardware_leases.release(lease_id)) is True
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "released"
    # device free again
    lease2 = _acquire(rt, dev_id)
    assert run(rt.hardware_leases.revoke(lease2)) is True
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease2).state == "revoked"


def test_renew_creates_no_mission_step(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    lease_id = _acquire(rt, _first_device_id(rt))
    with rt.session_scope() as s:
        before = s.scalar(select(func.count()).select_from(MissionStep))
    for _ in range(3):
        run(rt.hardware_leases.renew(lease_id))
    with rt.session_scope() as s:
        after = s.scalar(select(func.count()).select_from(MissionStep))
    assert before == after == 0


def test_disabled_device_cannot_be_leased(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    with rt.session_scope() as s:
        SDRDeviceRepository(s).update(dev_id, enabled=False)
        s.commit()
    with pytest.raises(DeviceUnavailableError):
        _acquire(rt, dev_id)


def test_incompatible_request_rejected(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[
        descriptor(freq=((1e9, 2e9),), channels=1)])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    with pytest.raises(LeaseCompatibilityError):
        _acquire(rt, dev_id, frequency_hz=500e6)  # outside range
    with pytest.raises(LeaseCompatibilityError):
        _acquire(rt, dev_id, channels=[3])  # no such channel


def test_device_missing_during_lease_orphans_it(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    lease_id = _acquire(rt, dev_id)
    fake.set_descriptors([])  # device vanishes
    run(rt.hardware_inventory.refresh())  # triggers on_device_missing
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "orphaned"


def test_terminal_mission_releases_lease(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    with rt.session_scope() as s:
        mission = MissionRepository(s).create(content="capture")
        mission_id = mission.id
        s.commit()
    lease_id = _acquire(rt, dev_id, mission_id=mission_id)
    with rt.session_scope() as s:
        MissionRepository(s).update_status(mission_id, "completed")
        s.commit()
    run(rt.hardware_leases.reconcile())
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "released"


def test_restart_recovery_no_conflicting_lease(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    lease_id = _acquire(rt, dev_id)
    # Simulate restart: orphan all holding leases (no token survives).
    rt.hardware_leases.recover()
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "orphaned"
    # During recovery (before reconciliation) a conflicting lease is NOT granted.
    with pytest.raises(LeaseConflictError):
        _acquire(rt, dev_id)
    # After reconciliation the stale lease expires and the device frees up.
    run(rt.hardware_leases.reconcile())
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(lease_id).state == "expired"
    new_lease = _acquire(rt, dev_id)
    with rt.session_scope() as s:
        assert SDRDeviceLeaseRepository(s).get(new_lease).state == "active"


def test_lease_history_retained(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    lease_id = _acquire(rt, dev_id)
    run(rt.hardware_leases.release(lease_id))
    from aithernet.state.repositories import SDRDeviceLeaseEventRepository
    with rt.session_scope() as s:
        events = SDRDeviceLeaseEventRepository(s).list_for_lease(lease_id)
    types = {e.event_type for e in events}
    assert {"requested", "acquired", "released"} <= types


# -- backend binding -------------------------------------------------------------------------


def test_unknown_backend_binding_rejected(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    with pytest.raises(BindingNotFoundError):
        _acquire(rt, _first_device_id(rt), backend_id="does-not-exist")


def test_config_admin_binding_supplies_device_args(tmp_path):
    cfg = NodeConfig(
        node_id="hw-node", node_name="hw-node", database_url=f"sqlite:///{tmp_path}/hw.db",
        hardware=HardwareConfig(
            enabled=True, discovery_on_start=False, lease_duration_seconds=5,
            lease_renew_interval_seconds=2,
            bindings=[HardwareBindingConfig(device_selector="Ettus:B210",
                                            backend_id="legacy_gr_mcp",
                                            device_args={"driver": "uhd", "addr": "192.168.10.2"})],
        ),
    )
    from aithernet.orchestrator.runtime import NodeRuntime
    rt = NodeRuntime.from_config(cfg)
    fake = FakeDiscoveryBackend("fake", [descriptor()])
    rt.hardware_discovery.register(fake)
    run(rt.hardware_inventory.refresh())
    dev_id = _first_device_id(rt)
    with rt.session_scope() as s:
        device = SDRDeviceRepository(s).get(dev_id)
        args = rt.hardware_leases.derive_binding(s, device, "legacy_gr_mcp")
    assert args["driver"] == "uhd"
    assert args["addr"] == "192.168.10.2"


def test_coordinator_cannot_inject_raw_device_args():
    from aithernet.coordinator.contracts import CoordinatorDecision
    from aithernet.orchestrator.decision_router import rf_device_from_decision
    decision = CoordinatorDecision.from_model_json({
        "summary": "lease", "next_target": "rf_device", "action": "acquire",
        "message": "m", "expected_result": "lease",
        "structured_payload": {
            "action": "acquire_lease", "device_id": "d1", "backend_id": "legacy_gr_mcp",
            # malicious extra keys that MUST be ignored:
            "device_args": {"driver": "evil"}, "command": "rm -rf /", "addr": "1.2.3.4",
            "env": {"X": "y"}, "path": "/etc/passwd",
        },
    }, mission_id="m1")
    spec = rf_device_from_decision(decision)
    assert spec["device_id"] == "d1"
    assert "device_args" not in spec
    assert "command" not in spec
    assert "env" not in spec
    assert "path" not in spec


# -- API -------------------------------------------------------------------------------------


def _client(rt) -> TestClient:
    return TestClient(create_app(runtime=rt))


def test_api_devices_capabilities_providers_health(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    with _client(rt) as c:
        assert c.post("/hardware/refresh").status_code == 200
        devs = c.get("/hardware/devices").json()
        assert len(devs) == 1 and devs[0]["status"] == "present"
        did = devs[0]["device_id"]
        caps = c.get(f"/hardware/devices/{did}/capabilities").json()
        assert caps["rx_supported"] is True
        assert c.get("/hardware/providers").json()[0]["available"] is True
        assert isinstance(c.get(f"/hardware/devices/{did}/health-events").json(), list)


def test_api_lease_lifecycle_and_no_token_leak(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    with _client(rt) as c:
        c.post("/hardware/refresh")
        did = c.get("/hardware/devices").json()[0]["device_id"]
        r = c.post("/hardware/leases/acquire",
                   json={"device_id": did, "backend_id": "legacy_gr_mcp", "direction": "rx"})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "active"
        assert "lease_token" not in body and "owner_worker_id" not in body
        lid = body["lease_id"]
        # conflict -> 409
        assert c.post("/hardware/leases/acquire",
                      json={"device_id": did, "backend_id": "legacy_gr_mcp"}).status_code == 409
        assert c.post(f"/hardware/leases/{lid}/release").json()["state"] == "released"


def test_api_operator_actions_gated_when_disabled(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()], operator=False)
    with _client(rt) as c:
        c.post("/hardware/refresh")
        did = c.get("/hardware/devices").json()[0]["device_id"]
        r = c.post("/hardware/leases/acquire",
                   json={"device_id": did, "backend_id": "legacy_gr_mcp"})
        assert r.status_code == 403  # operator lease actions disabled


def test_api_reads_perform_no_writes(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    with _client(rt) as c:
        c.post("/hardware/refresh")
        before = c.get("/hardware/status").json()["devices_by_status"]
        c.get("/hardware/devices")
        c.get("/hardware/leases")
        after = c.get("/hardware/status").json()["devices_by_status"]
        assert before == after


# -- ops integration -------------------------------------------------------------------------


def test_readiness_required_provider_available(tmp_path):
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()], required=True)
    with _client(rt) as c:
        assert c.get("/health/ready").status_code == 200


def test_readiness_required_provider_unavailable(tmp_path):
    rt, fake = make_hw_runtime(tmp_path, descriptors=[descriptor()], required=True)
    fake.available_flag = False
    with _client(rt) as c:
        assert c.get("/health/ready").status_code == 503


def test_no_hardware_node_stays_ready(tmp_path):
    cfg = NodeConfig(node_id="plain", node_name="plain",
                     database_url=f"sqlite:///{tmp_path}/plain.db")
    from aithernet.orchestrator.runtime import NodeRuntime
    rt = NodeRuntime.from_config(cfg)
    with _client(rt) as c:
        assert c.get("/health/ready").status_code == 200
        snap = c.get("/health/ready").json()
        comp = {x["name"]: x for x in snap["components"]}
        assert comp["hardware_inventory"]["state"] == "disabled"


def test_preflight_hardware_checks(tmp_path):
    from aithernet.ops.preflight import run_preflight
    cfg = NodeConfig(
        node_id="hw", node_name="hw", database_url=f"sqlite:///{tmp_path}/hw.db",
        hardware=HardwareConfig(enabled=True, providers={
            "soapy": HardwareDiscoveryProviderConfig(
                kind="soapy", command="definitely-not-a-real-tool-xyz", required=True),
            "lab": HardwareDiscoveryProviderConfig(kind="static", devices=[descriptor()]),
        }),
    )
    report = run_preflight(cfg)
    names = {c.name: c for c in report.checks}
    assert names["hardware_provider:soapy"].status == "fail"  # required + missing tool
    assert names["hardware_provider:lab"].status == "ok"


def test_preflight_hardware_enabled_no_providers(tmp_path):
    from aithernet.ops.preflight import run_preflight
    cfg = NodeConfig(node_id="hw", node_name="hw", database_url=f"sqlite:///{tmp_path}/hw.db",
                     hardware=HardwareConfig(enabled=True))
    report = run_preflight(cfg)
    assert any(c.name == "hardware_providers" and c.status == "warn" for c in report.checks)


def test_diagnostics_include_hardware_summary(tmp_path):
    from aithernet.ops.diagnostics import build_diagnostics
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    info = build_diagnostics(rt.config, now_iso="2026-06-14T00:00:00+00:00")
    counts = info["counts"]
    assert "hardware_devices_by_status" in counts
    assert counts["hardware_devices_by_status"].get("present") == 1
    # no device path / serial leaks into diagnostics
    import json as _json
    assert "/dev/bus/usb" not in _json.dumps(info)


def test_backup_preserves_inventory_identity(tmp_path):
    from aithernet.ops.backup import create_backup, restore_backup, verify_backup
    rt, _ = make_hw_runtime(tmp_path, descriptors=[descriptor()])
    run(rt.hardware_inventory.refresh())
    original_key = _devices(rt)[0].hardware_key
    rt.engine.dispose()
    backup = create_backup(rt.config, backup_dir=str(tmp_path / "backups"),
                           now_iso="2026-06-14T00:00:00+00:00")
    assert verify_backup(backup.path).ok
    restore_backup(backup.path, rt.config, config_path=None, dry_run=False,
                   now_iso="2026-06-14T00:00:01+00:00")
    # reopen and confirm the device identity persisted through backup+restore
    from aithernet.orchestrator.runtime import NodeRuntime
    rt2 = NodeRuntime.from_config(rt.config)
    assert _devices(rt2)[0].hardware_key == original_key
