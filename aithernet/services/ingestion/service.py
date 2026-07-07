"""Ingestion-service logic (Stage 14E, Part 15/16/17).

Authenticated, idempotent, integrity-verified batch ingestion with tenant + node isolation,
durable batch/record indexing, blob-stored bundles, receipts, retention/deletion, and dataset
lineage. Deterministic errors; a node may only submit as itself; no cross-tenant access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from aithernet.data import ingest_auth
from aithernet.data.batch import BatchError, open_bundle
from aithernet.data.blobstore import BlobStore
from services.ingestion.config import IngestionConfig
from services.ingestion.models import (
    Base,
    IngestedBatch,
    IngestedRecord,
    IngestionCredential,
    IngestionDataset,
    IngestionDatasetMember,
    IngestionDatasetVersion,
    IngestionDeletionRequest,
    IngestionFailure,
    IngestionNode,
    IngestionNonce,
    IngestionReceipt,
    IngestionTenant,
    new_uuid,
    utcnow,
)


class AuthResult:
    def __init__(self, ok: bool, code: str = "ok", tenant_id: str | None = None,
                 node_id: str | None = None, nonce: str | None = None) -> None:
        self.ok, self.code, self.tenant_id, self.node_id, self.nonce = (
            ok, code, tenant_id, node_id, nonce
        )


class IngestionError(Exception):
    def __init__(self, code: str, status: int = 400, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.status = status
        self.detail = detail


class IngestionService:
    def __init__(self, config: IngestionConfig) -> None:
        self.config = config
        self.engine = create_engine(config.database_url)
        Base.metadata.create_all(self.engine)  # restart-safe, idempotent (checkfirst)
        self._sf = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.blobs = BlobStore(config.blob_root)

    def _session(self) -> Session:
        return self._sf()

    # -- enrollment (admin) -----------------------------------------------------

    def enroll_tenant(self, tenant_id: str, name: str) -> dict:
        with self._session() as s:
            if s.get(IngestionTenant, tenant_id) is None:
                s.add(IngestionTenant(id=tenant_id, name=name))
                s.commit()
            return {"tenant_id": tenant_id, "name": name}

    def enroll_node(self, tenant_id: str, node_id: str, *, key_id: str, public_key: str) -> dict:
        with self._session() as s:
            if s.get(IngestionTenant, tenant_id) is None:
                raise IngestionError("unknown_tenant", 404)
            existing = s.scalars(
                select(IngestionNode).where(IngestionNode.tenant_id == tenant_id,
                                            IngestionNode.node_id == node_id)
            ).first()
            if existing is None:
                s.add(IngestionNode(tenant_id=tenant_id, node_id=node_id))
            cred = s.scalars(
                select(IngestionCredential).where(
                    IngestionCredential.tenant_id == tenant_id,
                    IngestionCredential.node_id == node_id,
                    IngestionCredential.key_id == key_id,
                )
            ).first()
            if cred is None:
                s.add(IngestionCredential(tenant_id=tenant_id, node_id=node_id, key_id=key_id,
                                          public_key=public_key, status="active"))
            else:
                cred.public_key = public_key
                cred.status = "active"
            s.commit()
            return {"tenant_id": tenant_id, "node_id": node_id, "key_id": key_id}

    def revoke_credential(self, tenant_id: str, node_id: str, key_id: str) -> dict:
        with self._session() as s:
            cred = s.scalars(
                select(IngestionCredential).where(
                    IngestionCredential.tenant_id == tenant_id,
                    IngestionCredential.node_id == node_id,
                    IngestionCredential.key_id == key_id,
                )
            ).first()
            if cred is None:
                raise IngestionError("not_found", 404)
            cred.status = "revoked"
            s.commit()
            return {"key_id": key_id, "status": "revoked"}

    # -- authentication ---------------------------------------------------------

    def authenticate(self, *, headers: dict, method: str, target: str, body: bytes) -> AuthResult:
        low = {k.lower(): v for k, v in headers.items()}
        tenant_id = low.get(ingest_auth.HEADER_TENANT)
        node_id = low.get(ingest_auth.HEADER_NODE)
        key_id = low.get(ingest_auth.HEADER_KEY_ID)
        ts = low.get(ingest_auth.HEADER_TIMESTAMP)
        nonce = low.get(ingest_auth.HEADER_NONCE)
        digest = low.get(ingest_auth.HEADER_DIGEST)
        signature = low.get(ingest_auth.HEADER_SIGNATURE)
        if not all([tenant_id, node_id, key_id, ts, nonce, digest, signature]):
            return AuthResult(False, "missing_auth_header")
        now = utcnow()
        with self._session() as s:
            tenant = s.get(IngestionTenant, tenant_id)
            if tenant is None or tenant.status != "active":
                return AuthResult(False, "unknown_tenant", tenant_id, node_id)
            cred = s.scalars(
                select(IngestionCredential).where(
                    IngestionCredential.tenant_id == tenant_id,
                    IngestionCredential.node_id == node_id,
                    IngestionCredential.key_id == key_id,
                )
            ).first()
            if cred is None:
                return AuthResult(False, "unknown_key_id", tenant_id, node_id)
            if cred.status != "active":
                return AuthResult(False, "key_revoked", tenant_id, node_id)
            if cred.expires_at is not None and _aware(cred.expires_at) < now:
                return AuthResult(False, "key_expired", tenant_id, node_id)
            try:
                request_time = datetime.fromtimestamp(int(ts), tz=UTC)
            except (TypeError, ValueError):
                return AuthResult(False, "bad_timestamp", tenant_id, node_id)
            delta = (now - request_time).total_seconds()
            if abs(delta) > self.config.clock_skew_seconds:
                return AuthResult(False, "timestamp_out_of_window", tenant_id, node_id)
            if digest != ingest_auth.content_digest(body):
                return AuthResult(False, "body_digest_mismatch", tenant_id, node_id)
            message = ingest_auth.canonical_request_bytes(
                tenant_id=tenant_id, node_id=node_id, key_id=key_id, method=method,
                target=target, timestamp=ts, nonce=nonce, digest=digest,
            )
            if not ingest_auth.verify_signature(cred.public_key, message, signature):
                return AuthResult(False, "invalid_signature", tenant_id, node_id)
            # Replay protection.
            seen = s.scalars(
                select(IngestionNonce).where(IngestionNonce.tenant_id == tenant_id,
                                             IngestionNonce.node_id == node_id,
                                             IngestionNonce.nonce == nonce)
            ).first()
            if seen is not None:
                return AuthResult(False, "nonce_replayed", tenant_id, node_id)
            s.add(IngestionNonce(
                tenant_id=tenant_id, node_id=node_id, nonce=nonce,
                expires_at=now + timedelta(seconds=self.config.nonce_retention_seconds),
            ))
            s.execute(
                IngestionNonce.__table__.delete().where(IngestionNonce.expires_at < now)
            )
            s.commit()
        return AuthResult(True, "ok", tenant_id, node_id, nonce)

    # -- batch ingestion --------------------------------------------------------

    def ingest_batch(self, *, auth: AuthResult, idempotency_key: str, body: bytes) -> dict:
        if len(body) > self.config.max_bundle_bytes:
            self._record_failure(auth.tenant_id, auth.node_id, "oversized_bundle")
            raise IngestionError("oversized_bundle", 413)
        if not idempotency_key:
            raise IngestionError("missing_idempotency_key", 400)
        with self._session() as s:
            existing = s.scalars(
                select(IngestedBatch).where(IngestedBatch.tenant_id == auth.tenant_id,
                                            IngestedBatch.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                receipt = s.get(IngestionReceipt, existing.receipt_id)
                return _receipt_dict(existing, receipt)

        # Decrypt (if configured) + bomb-bounded decompress + verify.
        try:
            manifest, records = open_bundle(
                body, encryption_key_b64=self.config.decryption_key_b64,
                max_uncompressed=self.config.max_uncompressed_bytes,
            )
        except BatchError as exc:
            self._record_failure(auth.tenant_id, auth.node_id, f"bundle:{exc.code}")
            raise IngestionError("invalid_bundle", 400, str(exc)) from exc

        if manifest.get("tenant_id") != auth.tenant_id:
            self._record_failure(auth.tenant_id, auth.node_id, "tenant_mismatch")
            raise IngestionError("tenant_mismatch", 403)
        for rec in records:
            if not isinstance(rec, dict) or "record_id" not in rec:
                raise IngestionError("schema_invalid", 400)

        rel, _digest = self.blobs.put(body)
        with self._session() as s:
            batch = IngestedBatch(
                batch_id=manifest.get("batch_id", new_uuid()), tenant_id=auth.tenant_id,
                node_id=auth.node_id, node_pseudonym=manifest.get("node_pseudonym"),
                idempotency_key=idempotency_key, record_count=len(records),
                manifest_digest=manifest.get("records_digest"),
                records_digest=manifest.get("records_digest"), byte_size=len(body),
                relative_object_path=rel, status="stored",
            )
            s.add(batch)
            s.flush()
            for rec in records:
                s.add(IngestedRecord(
                    batch_pk=batch.id, tenant_id=auth.tenant_id,
                    record_id=rec.get("record_id"), category=rec.get("category"),
                    payload_digest=rec.get("payload_digest"),
                    subject_pseudonym=rec.get("subject_pseudonym"),
                ))
            receipt = IngestionReceipt(
                tenant_id=auth.tenant_id, batch_id=batch.batch_id,
                records_digest=manifest.get("records_digest"), record_count=len(records),
                status="accepted",
            )
            s.add(receipt)
            s.flush()
            batch.receipt_id = receipt.id
            s.commit()
            return _receipt_dict(batch, receipt)

    def get_batch(self, tenant_id: str, batch_id: str) -> dict:
        with self._session() as s:
            batch = s.scalars(
                select(IngestedBatch).where(IngestedBatch.tenant_id == tenant_id,
                                            IngestedBatch.batch_id == batch_id)
            ).first()
            if batch is None:
                raise IngestionError("not_found", 404)
            return {"batch_id": batch.batch_id, "status": batch.status,
                    "record_count": batch.record_count, "records_digest": batch.records_digest,
                    "created_at": batch.created_at.isoformat()}

    def get_receipt(self, tenant_id: str, receipt_id: str) -> dict:
        with self._session() as s:
            receipt = s.get(IngestionReceipt, receipt_id)
            if receipt is None or receipt.tenant_id != tenant_id:
                raise IngestionError("not_found", 404)
            return {"receipt_id": receipt.id, "batch_id": receipt.batch_id,
                    "records_digest": receipt.records_digest, "status": receipt.status}

    # -- deletion ---------------------------------------------------------------

    def request_deletion(self, *, tenant_id: str, scope: str, target_id: str | None) -> dict:
        with self._session() as s:
            deleted = 0
            if scope == "batch" and target_id:
                batch = s.scalars(
                    select(IngestedBatch).where(IngestedBatch.tenant_id == tenant_id,
                                                IngestedBatch.batch_id == target_id)
                ).first()
                if batch is not None:
                    for rec in s.scalars(
                        select(IngestedRecord).where(IngestedRecord.batch_pk == batch.id)
                    ):
                        rec.deleted = True
                        deleted += 1
                    batch.status = "deleted"
                    if batch.relative_object_path:
                        self.blobs.delete(batch.relative_object_path)
                    # Propagate through dataset lineage.
                    self._exclude_members(s, tenant_id, batch.batch_id)
            req = IngestionDeletionRequest(
                tenant_id=tenant_id, scope=scope, target_id=target_id, status="completed",
                records_deleted=deleted,
                receipt_json={"records_deleted": deleted, "scope": scope},
            )
            s.add(req)
            s.commit()
            return {"deletion_id": req.id, "status": "completed", "records_deleted": deleted}

    def get_deletion(self, tenant_id: str, deletion_id: str) -> dict:
        with self._session() as s:
            req = s.get(IngestionDeletionRequest, deletion_id)
            if req is None or req.tenant_id != tenant_id:
                raise IngestionError("not_found", 404)
            return {"deletion_id": req.id, "status": req.status,
                    "records_deleted": req.records_deleted, "receipt": req.receipt_json}

    def _exclude_members(self, s, tenant_id: str, batch_id: str) -> None:
        rec_ids = [r.record_id for r in s.scalars(
            select(IngestedRecord).join(
                IngestedBatch, IngestedBatch.id == IngestedRecord.batch_pk
            ).where(IngestedBatch.batch_id == batch_id)
        )]
        if rec_ids:
            for m in s.scalars(
                select(IngestionDatasetMember).where(
                    IngestionDatasetMember.record_id.in_(rec_ids)
                )
            ):
                m.excluded = True

    # -- admin datasets (separately authenticated; not node-facing) -------------

    def create_dataset(self, tenant_id: str, name: str, purpose: str = "model_training") -> dict:
        with self._session() as s:
            ds = IngestionDataset(tenant_id=tenant_id, name=name, purpose=purpose)
            s.add(ds)
            s.commit()
            return {"dataset_id": ds.id, "name": ds.name}

    def create_dataset_version(self, tenant_id: str, dataset_id: str) -> dict:
        from aithernet.data.crypto import sha256_hex

        with self._session() as s:
            ds = s.get(IngestionDataset, dataset_id)
            if ds is None or ds.tenant_id != tenant_id:
                raise IngestionError("not_found", 404)
            records = list(s.scalars(
                select(IngestedRecord).where(IngestedRecord.tenant_id == tenant_id,
                                             IngestedRecord.category == "training",
                                             IngestedRecord.deleted.is_(False))
            ))
            ver_no = (s.scalar(
                select(func.max(IngestionDatasetVersion.version)).where(
                    IngestionDatasetVersion.dataset_id == dataset_id
                )
            ) or 0) + 1
            digests = sorted(r.payload_digest or "" for r in records)
            version = IngestionDatasetVersion(
                dataset_id=dataset_id, version=ver_no,
                content_digest=sha256_hex(("|".join(digests)).encode()),
                source_batch_ids_json=sorted({r.batch_pk for r in records}),
                member_count=len(records),
            )
            s.add(version)
            s.flush()
            for r in records:
                s.add(IngestionDatasetMember(version_id=version.id, record_id=r.record_id))
            s.commit()
            return {"version_id": version.id, "version": ver_no, "member_count": len(records),
                    "content_digest": version.content_digest}

    # -- diagnostics ------------------------------------------------------------

    def readiness(self) -> dict:
        try:
            with self._session() as s:
                s.execute(select(func.count()).select_from(IngestionTenant))
            return {"status": "ready"}
        except Exception:  # noqa: BLE001
            return {"status": "degraded"}

    def diagnostics(self) -> dict:
        with self._session() as s:
            return {
                "tenants": int(s.scalar(select(func.count()).select_from(IngestionTenant)) or 0),
                "nodes": int(s.scalar(select(func.count()).select_from(IngestionNode)) or 0),
                "batches": int(s.scalar(select(func.count()).select_from(IngestedBatch)) or 0),
                "records": int(s.scalar(select(func.count()).select_from(IngestedRecord)) or 0),
                "failures": int(s.scalar(select(func.count()).select_from(IngestionFailure)) or 0),
            }

    def _record_failure(self, tenant_id, node_id, category: str) -> None:
        with self._session() as s:
            s.add(IngestionFailure(tenant_id=tenant_id, node_id=node_id, category=category))
            s.commit()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _receipt_dict(batch: IngestedBatch, receipt) -> dict:
    return {
        "batch_id": batch.batch_id,
        "receipt_id": batch.receipt_id or (receipt.id if receipt else None),
        "records_digest": batch.records_digest,
        "record_count": batch.record_count,
        "status": "accepted",
        "duplicate": False,
    }
