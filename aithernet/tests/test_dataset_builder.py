"""beta.10 Part 5: training-dataset builders with leakage-free mission-grouped splits.

Proves splits are assigned per MISSION (so retries/duplicates/corrected variants of one mission
never leak across train/validation/evaluation), that a build emits the full training-ready package
(JSONL splits + schema + card + provenance + statistics + SHA256SUMS), and that an evaluation slice
is held out.
"""

from __future__ import annotations

import json

import pytest

from aithernet.data.dataset_builder import (
    DATASET_CLASSES,
    assign_split,
    build_dataset,
)
from aithernet.data.research_spool import ResearchSpool


def _seed(spool, n_missions=30):
    spool.ensure_layout()
    norm = spool.subdir("normalized")
    for i in range(n_missions):
        mid = f"m{i}"
        # two records per mission (a retry) — they must land in the SAME split.
        for j in range(2):
            rec = {"record_type": "coordinator_decision", "mission_id": mid,
                   "attempt": j, "decision": "respond"}
            (norm / f"{mid}-{j}.json").write_text(json.dumps(rec))


def test_split_assignment_is_stable_and_per_mission():
    a = assign_split("mission-xyz")
    assert a == assign_split("mission-xyz")  # stable
    assert a in ("train", "validation", "evaluation")
    # all records of one mission map to one split (no per-record leakage)
    assert assign_split("m7") == assign_split("m7")


def test_build_emits_full_package_and_no_leakage(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    _seed(spool, n_missions=40)
    man = build_dataset("coordinator", spool=spool, version=3)
    out = spool.subdir("snapshots") / "dataset-coordinator-v0003"
    for fname in ("train.jsonl", "validation.jsonl", "evaluation.jsonl", "schema.json",
                  "dataset-card.json", "provenance.json", "statistics.json", "SHA256SUMS"):
        assert (out / fname).is_file(), f"missing {fname}"
    assert man["record_count"] == 80 and man["mission_count"] == 40
    assert sum(man["splits"].values()) == 80
    # leakage check: each mission's two records share one split
    split_of = {}
    for name in ("train", "validation", "evaluation"):
        for line in (out / f"{name}.jsonl").read_text().splitlines():
            r = json.loads(line)
            mid = r["mission_id"]
            assert split_of.setdefault(mid, name) == name, f"{mid} leaked across splits"


def test_evaluation_slice_is_held_out(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    _seed(spool, n_missions=60)
    man = build_dataset("coordinator", spool=spool, version=1)
    assert man["splits"]["evaluation"] > 0  # a held-out evaluation slice exists


def test_checksums_cover_every_artifact(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    _seed(spool, n_missions=10)
    build_dataset("coordinator", spool=spool, version=1)
    out = spool.subdir("snapshots") / "dataset-coordinator-v0001"
    listed = {ln.split("  ", 1)[1] for ln in (out / "SHA256SUMS").read_text().splitlines()}
    on_disk = {p.name for p in out.iterdir() if p.name != "SHA256SUMS"}
    assert listed == on_disk


def test_unknown_class_rejected(tmp_path):
    spool = ResearchSpool(tmp_path / "research")
    with pytest.raises(ValueError, match="unknown dataset class"):
        build_dataset("nonsense", spool=spool)
    assert set(DATASET_CLASSES) >= {"coordinator", "coding", "tool-calling", "recovery",
                                    "routing", "rf-analysis"}
