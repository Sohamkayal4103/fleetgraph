"""Stage 14E — privacy-preserving telemetry, consent, data export, and dataset lineage.

A local-first data platform: bounded local collection, explicit per-category consent, structured
redaction + pseudonymization, a durable export outbox of immutable sealed batches, a destination
abstraction (local archive / HTTP ingestion / Google Drive / air-gapped), retention + traceable
deletion, and a dataset registry with lineage. The product is fully functional with this disabled.
"""

from __future__ import annotations

from aithernet.data.service import DataPlatformService

__all__ = ["DataPlatformService"]
