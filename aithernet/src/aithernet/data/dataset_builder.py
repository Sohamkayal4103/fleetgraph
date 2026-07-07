"""beta.10 Part 5: training-dataset builders over the local research spool.

Builds versioned, training-ready datasets from normalized mission records, one dataset CLASS at a
time (coordinator decisions, coding, tool-calling, recovery, routing, rf-analysis). Splits are
assigned by MISSION (a stable hash of mission_id), never by individual record — so related retries,
duplicated missions, peer copies, and corrected variants of the same mission never leak across
train/validation/evaluation. A permanently held-out evaluation slice is reserved.

Each build emits: train/validation/evaluation JSONL, schema.json, dataset-card.json,
provenance.json, statistics.json, and SHA256SUMS — the training-ready package the readiness report
references. This does NOT fine-tune a model (beta.10 builds the infrastructure + readiness report).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from aithernet.data.research_spool import DATASET_SCHEMA_VERSION, ResearchSpool

#: Dataset class -> the record_type(s) it draws from in the normalized spool.
DATASET_CLASSES: dict[str, tuple[str, ...]] = {
    "coordinator": ("coordinator_decision",),
    "routing": ("coordinator_decision", "route_selection"),
    "tool-calling": ("mcp_tool_call", "tool_argument"),
    "recovery": ("provider_recovery", "tool_schema_recovery"),
    "coding": ("coding_task", "coding_patch"),
    "rf-analysis": ("rf_capture", "rf_feature"),
    # 1.0.0-beta.1 OTA RF transport dataset classes.
    "rf-transport-routing": ("rf_transport_selection",),
    "modem-profile-selection": ("rf_profile_selection",),
    "frame-recovery": ("rf_frame_recovery",),
    "link-adaptation": ("rf_link_adaptation",),
    "delivery-prediction": ("rf_delivery_outcome",),
    "retransmission-policy": ("rf_retransmission",),
    "snr-error-analysis": ("rf_channel_analysis",),
    "generated-modem-validation": ("rf_modem_validation",),
    "peer-message-delivery": ("rf_peer_delivery",),
    "peer-rf-delivery": ("rf_peer_delivery",),
    "simulated-channel-outcomes": ("rf_channel_outcome",),
    "physical-tx": ("rf_physical_tx",),
    # 1.0.0-beta.3 fleet / mesh / OTA-MAC dataset classes.
    "mesh-authorization": ("rf_mesh_authorization",),
    "cross-tenant-rejection": ("rf_cross_tenant_rejection",),
    "peer-selection": ("rf_peer_delivery", "rf_transport_selection"),
    "channel-access": ("rf_channel_access",),
    "rts-cts-outcomes": ("rf_rts_cts_outcome",),
    "collision-recovery": ("rf_collision_recovery",),
    "delivery-latency": ("rf_delivery_latency",),
    "fleet-health": ("rf_fleet_health",),
    # 1.0.0-beta.4 provider-neutral agent-runtime dataset classes.
    "provider-selection": ("provider_invocation",),
    "model-selection": ("provider_invocation",),
    "provider-routing": ("coordinator_decision", "provider_invocation"),
    "quota-recovery": ("provider_quota_event",),
    "fallback-outcomes": ("provider_fallback",),
    "coordinator-quality": ("coordinator_decision", "provider_invocation"),
    "coding-agent-quality": ("coding_task", "provider_invocation"),
    "provider-diversity": ("provider_invocation",),
}

#: Stable split weights. The evaluation slice is permanently held out.
_SPLITS = (("train", 0.8), ("validation", 0.1), ("evaluation", 0.1))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def assign_split(mission_id: str) -> str:
    """Deterministically map a mission to a split by hashing its id (leakage-free by mission)."""
    h = int(hashlib.sha256((mission_id or "").encode()).hexdigest(), 16)
    frac = (h % 10_000) / 10_000.0
    upto = 0.0
    for name, weight in _SPLITS:
        upto += weight
        if frac < upto:
            return name
    return _SPLITS[-1][0]


def _read_normalized(spool: ResearchSpool) -> list[dict]:
    records = []
    for p in sorted(spool.subdir("normalized").glob("*.json")):
        try:
            records.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return records


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_dataset(dataset_class: str, *, spool: ResearchSpool, version: int = 1) -> dict:
    """Build one dataset class from the spool's normalized records into a versioned snapshot dir.

    Returns the dataset manifest (counts, split distribution, provenance, checksums). Raises
    ValueError for an unknown class."""
    if dataset_class not in DATASET_CLASSES:
        raise ValueError(f"unknown dataset class {dataset_class!r} "
                         f"(choose: {', '.join(sorted(DATASET_CLASSES))})")
    spool.ensure_layout()
    types = set(DATASET_CLASSES[dataset_class])
    records = [r for r in _read_normalized(spool) if r.get("record_type") in types]

    by_split: dict[str, list[dict]] = {name: [] for name, _ in _SPLITS}
    missions: set[str] = set()
    for r in records:
        mid = str(r.get("mission_id") or "")
        missions.add(mid)
        by_split[assign_split(mid)].append(r)

    out_dir = spool.subdir("snapshots") / f"dataset-{dataset_class}-v{version:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    split_counts = {}
    for name, _ in _SPLITS:
        f = out_dir / f"{name}.jsonl"
        with open(f, "w", encoding="utf-8") as fh:
            for r in by_split[name]:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        split_counts[name] = len(by_split[name])

    schema = {"schema_version": DATASET_SCHEMA_VERSION, "dataset_class": dataset_class,
              "record_types": sorted(types), "format": "jsonl"}
    provenance = {"built_at": _now(), "source": "research-spool/normalized",
                  "mission_count": len(missions), "record_count": len(records),
                  "split_policy": "mission-grouped sha256 (leakage-free)",
                  "held_out_evaluation": True}
    statistics = {"records": len(records), "missions": len(missions), "splits": split_counts}
    card = {"name": f"aithernet-{dataset_class}-v{version:04d}",
            "schema_version": DATASET_SCHEMA_VERSION, "dataset_class": dataset_class,
            "description": f"Aithernet {dataset_class} dataset built from owner research spool.",
            "splits": split_counts, "leakage_control": "split assigned per mission, not per record",
            "license_provenance": "owner-operated node; provider outputs; secret-scanned",
            "token_estimate": sum(len(json.dumps(r)) for r in records) // 4}

    for fname, obj in (("schema.json", schema), ("dataset-card.json", card),
                       ("provenance.json", provenance), ("statistics.json", statistics)):
        (out_dir / fname).write_text(json.dumps(obj, indent=2, sort_keys=True))

    sums = {p.name: _sha256_file(p) for p in sorted(out_dir.iterdir()) if p.name != "SHA256SUMS"}
    (out_dir / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())))

    return {"dataset_class": dataset_class, "version": version, "path": str(out_dir),
            "record_count": len(records), "mission_count": len(missions),
            "splits": split_counts, "checksums": sums, "schema_version": DATASET_SCHEMA_VERSION}
