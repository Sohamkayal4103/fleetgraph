"""Sanitized artifact-transfer event types (Stage 13D.2, Part J).

Payloads carry ids, states, and bounded counts ONLY — never bytes, absolute paths, digests of
unrelated objects, signatures, keys, credentials, environments, or full manifests. Progress
events are rate-limited by the worker.
"""

from __future__ import annotations

EVENT_ARTIFACT_INDEXED = "artifact.indexed"
EVENT_ARTIFACT_OFFER_QUEUED = "artifact.offer.queued"
EVENT_ARTIFACT_OFFER_RECEIVED = "artifact.offer.received"
EVENT_ARTIFACT_REQUEST_QUEUED = "artifact.request.queued"
EVENT_ARTIFACT_REQUEST_RECEIVED = "artifact.request.received"
EVENT_ARTIFACT_TRANSFER_AUTHORIZED = "artifact.transfer.authorized"
EVENT_ARTIFACT_TRANSFER_REJECTED = "artifact.transfer.rejected"
EVENT_ARTIFACT_TRANSFER_STARTED = "artifact.transfer.started"
EVENT_ARTIFACT_TRANSFER_PROGRESS = "artifact.transfer.progress"
EVENT_ARTIFACT_TRANSFER_PAUSED = "artifact.transfer.paused"
EVENT_ARTIFACT_TRANSFER_RESUMED = "artifact.transfer.resumed"
EVENT_ARTIFACT_TRANSFER_VERIFYING = "artifact.transfer.verifying"
EVENT_ARTIFACT_TRANSFER_COMPLETED = "artifact.transfer.completed"
EVENT_ARTIFACT_TRANSFER_FAILED = "artifact.transfer.failed"
EVENT_ARTIFACT_TRANSFER_CANCELLED = "artifact.transfer.cancelled"
EVENT_ARTIFACT_TRANSFER_EXPIRED = "artifact.transfer.expired"
EVENT_ARTIFACT_STORE_GC = "artifact.store.gc"
