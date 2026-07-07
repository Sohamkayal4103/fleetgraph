"""Canonical export-record envelope construction (Stage 14E, Part 6).

A versioned, bounded, pseudonymous envelope with a deterministic payload digest. The payload
is the ALREADY-REDACTED minimized dict — never an internal model, raw SQL row, arbitrary
object dump, private path, or secret. Records are immutable after sealing; a correction creates
a superseding record rather than mutating the original.
"""

from __future__ import annotations

from datetime import datetime

from aithernet.data import categories as cat
from aithernet.transport.canonical import canonical_hash

RECORD_SCHEMA_VERSION = "1"
_MAX_PAYLOAD_BYTES = 256 * 1024


def payload_digest(payload: dict) -> str:
    return canonical_hash(payload)


def build_record_fields(
    *,
    category: str,
    purpose: str | None = None,
    sensitivity: str | None = None,
    tenant_id: str,
    node_pseudonym: str,
    subject_pseudonym: str | None,
    payload: dict,
    redaction_policy_version: str,
    retention_class: str,
    consent_revision_id: str | None,
    source_type: str | None = None,
    source_id: str | None = None,
    mission_id: str | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
    conversation_id: str | None = None,
    artifact_id: str | None = None,
    correlation_id: str | None = None,
    event_time: datetime | None = None,
    lineage: dict | None = None,
) -> dict:
    """Return a field dict for :class:`DataExportRecord` (the canonical envelope minus state)."""
    purpose = purpose or cat.CATEGORY_DEFAULT_PURPOSE.get(category, cat.PURPOSE_OPERATIONS)
    sensitivity = sensitivity or cat.CATEGORY_DEFAULT_SENSITIVITY.get(
        category, cat.SENSITIVITY_LOW
    )
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "category": category,
        "purpose": purpose,
        "sensitivity": sensitivity,
        "tenant_id": tenant_id,
        "node_pseudonym": node_pseudonym,
        "subject_pseudonym": subject_pseudonym,
        "event_time": event_time,
        "source_type": source_type,
        "source_id": source_id,
        "mission_id": mission_id,
        "run_id": run_id,
        "step_id": step_id,
        "conversation_id": conversation_id,
        "artifact_id": artifact_id,
        "correlation_id": correlation_id,
        "payload_json": payload,
        "payload_digest": payload_digest(payload),
        "consent_revision_id": consent_revision_id,
        "redaction_policy_version": redaction_policy_version,
        "retention_class": retention_class,
        "lineage_json": lineage or {},
    }


def envelope_from_record(record) -> dict:
    """The canonical wire envelope serialized into a batch (immutable view of a record)."""
    return {
        "record_id": record.id,
        "schema_version": record.schema_version,
        "category": record.category,
        "purpose": record.purpose,
        "sensitivity": record.sensitivity,
        "tenant_id": record.tenant_id,
        "node_pseudonym": record.node_pseudonym,
        "subject_pseudonym": record.subject_pseudonym,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "event_time": record.event_time.isoformat() if record.event_time else None,
        "source_type": record.source_type,
        "source_id": record.source_id,
        "mission_id": record.mission_id,
        "run_id": record.run_id,
        "step_id": record.step_id,
        "conversation_id": record.conversation_id,
        "artifact_id": record.artifact_id,
        "correlation_id": record.correlation_id,
        "payload": record.payload_json or {},
        "payload_digest": record.payload_digest,
        "consent_revision_id": record.consent_revision_id,
        "redaction_policy_version": record.redaction_policy_version,
        "retention_class": record.retention_class,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "lineage": record.lineage_json or {},
    }


def payload_size(payload: dict) -> int:
    import json

    return len(json.dumps(payload, default=str).encode("utf-8"))


def payload_within_bounds(payload: dict) -> bool:
    return payload_size(payload) <= _MAX_PAYLOAD_BYTES
