"""Ingestion-service ORM models (Stage 14E, Part 17).

A SEPARATE declarative Base + database from the node. Binary bundles are NOT stored in
relational columns — only metadata + a blob-store reference. Datasets/lineage live here for the
eventual administrative portal; node-facing endpoints never expose another tenant's records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for ingestion-service models (independent of the node store)."""


class IngestionTenant(Base):
    __tablename__ = "ingestion_tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    quota_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=100000)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionNode(Base):
    __tablename__ = "ingestion_nodes"
    __table_args__ = (UniqueConstraint("tenant_id", "node_id", name="uq_ingestion_node"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class IngestionCredential(Base):
    __tablename__ = "ingestion_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_id", "key_id", name="uq_ingestion_credential"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key_id: Mapped[str] = mapped_column(String(80), nullable=False)
    public_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionNonce(Base):
    __tablename__ = "ingestion_nonces"
    __table_args__ = (UniqueConstraint("tenant_id", "node_id", "nonce", name="uq_ingestion_nonce"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class IngestedBatch(Base):
    __tablename__ = "ingested_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_ingested_batch_idem"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_pseudonym: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    records_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relative_object_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stored", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class IngestedRecord(Base):
    __tablename__ = "ingested_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    batch_pk: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    subject_pseudonym: Mapped[str | None] = mapped_column(String(80), nullable=True)
    deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionReceipt(Base):
    __tablename__ = "ingestion_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    records_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionFailure(Base):
    __tablename__ = "ingestion_failures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class IngestionDeletionRequest(Base):
    __tablename__ = "ingestion_deletion_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    records_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionDataset(Base):
    __tablename__ = "ingestion_datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionDatasetVersion(Base):
    __tablename__ = "ingestion_dataset_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_batch_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletion_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IngestionDatasetMember(Base):
    __tablename__ = "ingestion_dataset_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    excluded: Mapped[bool] = mapped_column(default=False, nullable=False)
