"""Ingestion-service configuration (Stage 14E).

Loaded from environment variables for the standalone process. No secrets are committed; the
admin token + any decryption key are referenced from the environment. Conservative bounds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class IngestionConfig:
    database_url: str = "sqlite:///./ingestion.db"
    blob_root: str = "./ingestion-blobs"
    host: str = "127.0.0.1"
    port: int = 8090
    admin_token: str | None = None          # operator-set; never committed
    decryption_key_b64: str | None = None    # optional per-deployment batch decryption key
    max_bundle_bytes: int = 16 * 1024 * 1024
    max_uncompressed_bytes: int = 64 * 1024 * 1024
    clock_skew_seconds: int = 60
    nonce_retention_seconds: int = 600
    rate_limit_per_minute: int = 600

    @classmethod
    def from_env(cls) -> IngestionConfig:
        return cls(
            database_url=os.environ.get("INGEST_DATABASE_URL", "sqlite:///./ingestion.db"),
            blob_root=os.environ.get("INGEST_BLOB_ROOT", "./ingestion-blobs"),
            host=os.environ.get("INGEST_HOST", "127.0.0.1"),
            port=int(os.environ.get("INGEST_PORT", "8090")),
            admin_token=os.environ.get("INGEST_ADMIN_TOKEN"),
            decryption_key_b64=(
                os.environ.get(os.environ["INGEST_DECRYPTION_KEY_REF"])
                if os.environ.get("INGEST_DECRYPTION_KEY_REF") else None
            ),
        )
