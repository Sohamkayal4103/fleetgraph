"""Items 2+3: RF exchange -> research spool persistence -> RF dataset snapshots.

Runs real simulated RF exchanges, persists secret-free records to the spool, then builds RF dataset
snapshots with mission-grouped leakage-free splits + full package + checksums. Proves no key
material is persisted and that retries of one logical message stay in one split.
"""

from __future__ import annotations

from aithernet.data.dataset_builder import build_dataset
from aithernet.data.research_spool import ResearchSpool
from aithernet.transport.identity import IdentityManager
from aithernet.transport.ota import channel as ch
from aithernet.transport.ota.profiles import REFERENCE_BPSK
from aithernet.transport.ota.research import persist_rf_exchange
from aithernet.transport.ota.simulated_rf import SimulatedRFLink

SECRET = b"out-of-band-pairing-secret-for-records!"


def _exchange(tmp_path, mid):
    a = IdentityManager(tmp_path / f"a{mid}", node_id="node-A", node_name="A").initialize()
    b = IdentityManager(tmp_path / f"b{mid}", node_id="node-B", node_name="B").initialize()
    link = SimulatedRFLink(REFERENCE_BPSK, ch.ChannelModel(snr_db=14.0, seed=mid))
    link.add_endpoint("node-A", peer_id="node-B", peer_public_key_b64=b.public_key_b64,
                      shared_secret=SECRET)
    link.add_endpoint("node-B", peer_id="node-A", peer_public_key_b64=a.public_key_b64,
                      shared_secret=SECRET)
    from aithernet.transport.envelope import (
        EnvelopeRecipient,
        EnvelopeSender,
        MessageEnvelope,
    )
    env = MessageEnvelope(sender=EnvelopeSender(node_id="node-A", fingerprint=a.fingerprint),
                          recipient=EnvelopeRecipient(node_id="node-B"),
                          payload={"type": "mission_request", "n": mid}).sign(a)
    _, _, rec = link.transmit(sender="node-A", receiver="node-B", envelope=env)
    return rec


def test_persist_rf_exchange_is_secret_free(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    rec = _exchange(tmp_path, 1)
    receipts = persist_rf_exchange(rec, spool=spool, profile=REFERENCE_BPSK, mission_id="m1")
    assert receipts and all(r["dest"] == "normalized" for r in receipts)
    # no key material anywhere in the spool
    import json
    for p in (spool.subdir("normalized")).glob("*.json"):
        blob = p.read_text()
        assert SECRET.decode() not in blob
        d = json.loads(blob)
        assert "link_key" not in d and "key" not in d
        assert d["record_type"].startswith("rf_")
        assert d["canonical_payload_digest"].startswith("sha256:")


def test_rf_datasets_build_with_mission_grouped_splits(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    # 24 distinct missions, each persisted twice (a "retry") -> must not leak across splits.
    for mid in range(24):
        rec = _exchange(tmp_path, mid)
        persist_rf_exchange(rec, spool=spool, profile=REFERENCE_BPSK, mission_id=f"mission-{mid}")
        persist_rf_exchange(rec, spool=spool, profile=REFERENCE_BPSK, mission_id=f"mission-{mid}")

    for cls in ("peer-rf-delivery", "frame-recovery", "retransmission-policy",
                "simulated-channel-outcomes"):
        man = build_dataset(cls, spool=spool, version=1)
        out = spool.subdir("snapshots") / f"dataset-{cls}-v0001"
        for f in ("train.jsonl", "validation.jsonl", "evaluation.jsonl", "schema.json",
                  "dataset-card.json", "provenance.json", "statistics.json", "SHA256SUMS"):
            assert (out / f).is_file(), f"{cls} missing {f}"
        assert man["mission_count"] == 24 and man["splits"]["evaluation"] > 0
        # leakage: each mission's records all land in one split
        import json
        split_of = {}
        for name in ("train", "validation", "evaluation"):
            for line in (out / f"{name}.jsonl").read_text().splitlines():
                mid = json.loads(line)["mission_id"]
                assert split_of.setdefault(mid, name) == name, f"{mid} leaked in {cls}"
