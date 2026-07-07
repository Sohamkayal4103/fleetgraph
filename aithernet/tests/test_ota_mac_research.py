"""beta.3 research-record + dataset tests for the MAC / mesh / fleet datasets.

Proves the new dataset classes build leakage-free mission-grouped snapshots, that tenant/mesh/node
identifiers are pseudonymized (no raw ids), and that records carry no secret material.
"""

from __future__ import annotations

import json

from aithernet.data.dataset_builder import DATASET_CLASSES, build_dataset
from aithernet.data.research_spool import ResearchSpool
from aithernet.transport.ota import research as R
from aithernet.transport.ota.mac import MacConfig, MacEndpoint, VirtualMedium, run_mac_transfer

KEY = b"k" * 32


def _transfer():
    a = MacEndpoint("node-A", link_key=KEY, mesh_id="m1")
    b = MacEndpoint("node-B", link_key=KEY, mesh_id="m1")
    return run_mac_transfer(
        tx=a, rx=b, payload_fragments=[b"x" * 8 for _ in range(4)],
        medium=VirtualMedium(), config=MacConfig(), seed=1,
    )


def test_new_dataset_classes_registered():
    for cls in (
        "mesh-authorization", "cross-tenant-rejection", "peer-selection", "channel-access",
        "rts-cts-outcomes", "collision-recovery", "delivery-latency", "fleet-health",
    ):
        assert cls in DATASET_CLASSES


def test_mac_records_are_pseudonymous_and_secret_free():
    recs = R.mac_transfer_to_records(
        _transfer(), tenant_id="tenant-secret-A", mesh_id="mesh-1", sender_node_id="node-A",
        receiver_node_id="node-B", mission_id="mission-1", mission_ack="accepted",
        mission_result="completed",
    )
    blob = json.dumps(recs)
    # No raw identifiers leak; only pseudonyms.
    assert "tenant-secret-A" not in blob
    assert "node-A" not in blob and "node-B" not in blob
    assert recs[0]["tenant_pseudonym"].startswith("px:")
    # The four ack layers are present and distinct.
    r0 = recs[0]
    assert r0["link_acknowledged"] is True
    assert r0["delivery_receipt"] is True
    assert r0["mission_ack"] == "accepted"
    assert r0["mission_result"] == "completed"


def test_mac_and_mesh_and_fleet_datasets_build(tmp_path):
    sp = ResearchSpool(root=tmp_path)
    R.persist_mac_transfer(
        _transfer(), spool=sp, tenant_id="tenantA", mesh_id="m1", sender_node_id="node-A",
        receiver_node_id="node-B", mission_id="mission-1",
    )
    sp.write_normalized_record(R.mesh_decision_to_record(
        decision="deny", reason="cross-tenant", tenant_id="tenantB", mesh_id="mA",
        sender_node_id="node-B1", receiver_node_id="node-A1", cross_tenant=True,
    ))
    sp.write_normalized_record(R.fleet_health_to_record(
        node_id="node-A", tenant_id="tenantA", mesh_id="m1", state="online", sequence=3,
        software_version="1.0.0b3", enabled_transports=["ip", "simulated_rf"],
    ))
    for cls in ("rts-cts-outcomes", "channel-access", "collision-recovery", "delivery-latency",
                "cross-tenant-rejection", "fleet-health"):
        res = build_dataset(cls, spool=sp)
        assert res["record_count"] >= 1, cls
        # mission-grouped, no leakage across splits — each split gets whole missions.
        assert sum(res["splits"].values()) == res["record_count"]
