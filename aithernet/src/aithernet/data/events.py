"""Sanitized data-platform event-type constants (Stage 14E, Part 28).

These are emitted to the node event bus / audit log for operators. They NEVER carry raw
record payloads, secrets, tokens, keys, or private paths — only ids, categories, and counts.
High-frequency record events are rate-limited by the service.
"""

from __future__ import annotations

EVENT_CONSENT_GRANTED = "data.consent.granted"
EVENT_CONSENT_WITHDRAWN = "data.consent.withdrawn"
EVENT_RECORD_COLLECTED = "data.record.collected"
EVENT_RECORD_HELD = "data.record.held"
EVENT_RECORD_APPROVED = "data.record.approved"
EVENT_RECORD_REJECTED = "data.record.rejected"
EVENT_RECORD_REDACTED = "data.record.redacted"
EVENT_BATCH_CREATED = "data.batch.created"
EVENT_BATCH_SEALED = "data.batch.sealed"
EVENT_DELIVERY_STARTED = "data.delivery.started"
EVENT_DELIVERY_SUCCEEDED = "data.delivery.succeeded"
EVENT_DELIVERY_RETRY_SCHEDULED = "data.delivery.retry_scheduled"
EVENT_DELIVERY_DEAD_LETTERED = "data.delivery.dead_lettered"
EVENT_DESTINATION_ENABLED = "data.destination.enabled"
EVENT_DESTINATION_DISABLED = "data.destination.disabled"
EVENT_DESTINATION_DEGRADED = "data.destination.degraded"
EVENT_DELETION_REQUESTED = "data.deletion.requested"
EVENT_DELETION_COMPLETED = "data.deletion.completed"
EVENT_DELETION_FAILED = "data.deletion.failed"
EVENT_DATASET_CREATED = "data.dataset.created"
EVENT_DATASET_VERSION_CREATED = "data.dataset.version_created"
EVENT_RETENTION_EXPIRED = "data.retention.expired"
