"""Privacy-preserving data-platform service (Stage 14E).

The single node-side orchestration surface: consent (append-only, effective-state derived),
deterministic collection policy, structured redaction + pseudonymization, immutable canonical
records, a durable export outbox with sealed/compressed/encrypted batches, the destination
abstraction, retention + traceable deletion (propagated through dataset lineage), and the
dataset registry. It is local-first and OFF-leaning: export is disabled + paused by default;
training + raw-artifact collection require explicit recorded consent. Nothing here creates a
MissionStep, and no record/event/receipt ever carries a secret, token, key, or raw private path.
"""

from __future__ import annotations

import contextlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aithernet.data import categories as cat
from aithernet.data import consent as consent_eval
from aithernet.data import crypto
from aithernet.data import events as ev
from aithernet.data import policy as policy_engine
from aithernet.data import records as record_builder
from aithernet.data.batch import seal_batch
from aithernet.data.blobstore import BlobStore
from aithernet.data.crypto import pseudonymize, sha256_hex
from aithernet.data.destinations.airgapped import AirGappedBundleDestination
from aithernet.data.destinations.drive import GoogleDriveArchiveDestination
from aithernet.data.destinations.http import HTTPIngestionDestination
from aithernet.data.destinations.local import LocalArchiveDestination
from aithernet.data.redaction import Redactor, contains_residual_secret
from aithernet.data.worker import DataExportWorker
from aithernet.state.models import new_uuid, utcnow
from aithernet.state.repositories import (
    DataCategoryPolicyRepository,
    DataConsentAuditRepository,
    DataConsentGrantRepository,
    DataConsentProfileRepository,
    DataConsentRevisionRepository,
    DataDatasetMemberRepository,
    DataDatasetRepository,
    DataDatasetVersionRepository,
    DataDeletionRequestRepository,
    DataDeliveryAttemptRepository,
    DataExportBatchRepository,
    DataExportDestinationRepository,
    DataExportRecordRepository,
)

if TYPE_CHECKING:
    from aithernet.orchestrator.runtime import NodeRuntime

#: Sentinel distinguishing "derived key not yet computed" from "no secret (key is None)".
_UNSET = object()


class DataError(Exception):
    code = "data_error"


class NotFoundError(DataError):
    code = "not_found"


class ForbiddenError(DataError):
    code = "forbidden"


class ConflictError(DataError):
    code = "conflict"


class ValidationError(DataError):
    code = "validation_error"


class ConsentError(DataError):
    code = "consent_required"


@dataclass
class _Throttle:
    """A tiny per-minute event throttle (rate-limit high-frequency record events)."""

    limit: int = 30
    _count: int = 0
    _window: int = 0

    def allow(self, minute: int) -> bool:
        if minute != self._window:
            self._window = minute
            self._count = 0
        self._count += 1
        return self._count <= self.limit


class DataPlatformService:
    """The node-side privacy + data-export platform (Stage 14E)."""

    def __init__(self, runtime: NodeRuntime) -> None:
        self.runtime = runtime
        self.node_id = runtime.config.node_id
        self.config = runtime.config.data_platform
        self.tenant_id = self.config.tenant_id
        self.export_paused = self.config.export.paused
        self.collection_paused = False
        self.worker = DataExportWorker(self)
        self._throttle = _Throttle()
        # Test/integration injection hooks (never affect production credential loading).
        self._drive_client = None
        self._http_client_factory = None
        self._pseudo_key_cache: object = _UNSET   # derived bytes, or None when the secret is absent
        self._node_pseudonym_cache: str | None = None

    # -- lifecycle --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if not self.enabled:
            return
        with contextlib.suppress(Exception):
            self._ensure_default_destination()
        # Automatic operational telemetry: one bounded snapshot of the node's own state.
        if self.config.collection.operational_enabled:
            with contextlib.suppress(Exception):
                self.collect_operational_snapshot()
        await self.worker.start()

    async def shutdown(self) -> None:
        await self.worker.shutdown()

    # -- identity / pseudonymization -------------------------------------------

    def _pseudo_key(self) -> bytes | None:
        """The tenant-separated DERIVED pseudonymization key, or None if the secret is absent.

        Derived via HKDF-SHA-256 from a high-entropy master secret loaded from an env-REFERENCED
        name (never a node fingerprint). Stable across restart + backup/restore (depends only on
        the secret + tenant + policy version + key id, never the DB).
        """
        if self._pseudo_key_cache is _UNSET:
            priv = self.config.privacy
            master = (
                crypto.load_master_secret(os.environ.get(priv.pseudonymization_key_ref))
                if priv.pseudonymization_key_ref else None
            )
            if master is None:
                self._pseudo_key_cache = None
            else:
                self._pseudo_key_cache = crypto.derive_tenant_pseudonym_key(
                    master, tenant_id=self.tenant_id,
                    policy_version=priv.pseudonymization_policy_version,
                    key_id=priv.pseudonymization_key_id,
                )
        return self._pseudo_key_cache  # type: ignore[return-value]

    def pseudonymization_ready(self) -> bool:
        """True when pseudonymization is disabled, or the master secret is present + strong."""
        if not self.config.privacy.pseudonymization_enabled:
            return True
        return self._pseudo_key() is not None

    def _pseudonymization_reason(self) -> str:
        if self.pseudonymization_ready():
            return "ok"
        return "pseudonymization_secret_missing" if self.config.privacy.pseudonymization_key_ref \
            else "pseudonymization_secret_unconfigured"

    def node_pseudonym(self) -> str:
        if self._node_pseudonym_cache is None:
            key = self._pseudo_key()
            if key is None:
                # Fail closed: an explicit sentinel, NEVER a public/predictable fallback.
                return "pseu_unavailable"
            self._node_pseudonym_cache = pseudonymize(self.node_id, key=key, namespace="node")
        return self._node_pseudonym_cache

    def _redactor(self) -> Redactor:
        return Redactor(
            pseudo_key=self._pseudo_key(),
            pseudonymization_enabled=self.config.privacy.pseudonymization_enabled,
        )

    def _blob_store(self) -> BlobStore:
        root = os.path.join(os.path.dirname(self.runtime.config.database_path) or ".",
                            "data-platform")
        return BlobStore(root)

    # -- audit / events ---------------------------------------------------------

    def _audit(self, session, *, event_type: str, category: str | None = None,
               detail: str = "") -> None:
        DataConsentAuditRepository(session).create(
            tenant_id=self.tenant_id, node_id=self.node_id, event_type=event_type,
            category=category, detail=detail[:300],
        )

    async def _emit(self, event_type: str, message: str, *, category: str | None = None,
                    payload: dict | None = None, throttle: bool = False) -> None:
        if throttle:
            minute = int(utcnow().timestamp() // 60)
            if not self._throttle.allow(minute):
                return
        with contextlib.suppress(Exception):
            await self.runtime.publish_ephemeral_event(
                event_type=event_type, source="data_platform", message=message,
                payload={"category": category, **(payload or {})},
            )

    # -- consent ----------------------------------------------------------------

    def accept_profile(
        self, *, policy_version: str, document_digest: str | None = None,
        required_categories: list[str] | None = None, optional_categories: list[str] | None = None,
        effective_date: datetime | None = None, source: str = "operator",
        subject: str | None = None,
    ) -> dict:
        """Record affirmative acceptance of a versioned participation policy."""
        required = [c for c in (required_categories or []) if c in cat.ALL_CATEGORIES]
        optional = [c for c in (optional_categories or []) if c in cat.ALL_CATEGORIES]
        with self.runtime.session_scope() as session:
            repo = DataConsentProfileRepository(session)
            prior = repo.active(self.tenant_id, self.node_id)
            if prior is not None:
                prior.state = "superseded"
            profile = repo.create(
                tenant_id=self.tenant_id, node_id=self.node_id, subject=subject,
                policy_version=policy_version, document_digest=document_digest,
                required_categories_json=required, optional_categories_json=optional,
                effective_date=effective_date, accepted_at=utcnow(), source=source,
                state="active",
            )
            self._audit(session, event_type=ev.EVENT_CONSENT_GRANTED,
                        detail=f"profile policy={policy_version}")
            session.commit()
            return {"profile_id": profile.id, "policy_version": policy_version,
                    "required_categories": required, "optional_categories": optional}

    def grant_consent(
        self, *, category: str, purpose: str | None = None, collection: bool = True,
        export: bool = False, raw_artifact: bool = False, destination_scope: str = "local",
        expires_at: datetime | None = None, source: str = "operator",
        policy_version: str | None = None, terms_digest: str | None = None,
        subject: str | None = None,
    ) -> dict:
        if category not in cat.ALL_CATEGORIES:
            raise ValidationError(f"unknown category: {category}")
        now = utcnow()
        with self.runtime.session_scope() as session:
            profile = DataConsentProfileRepository(session).active(self.tenant_id, self.node_id)
            # Sensitive categories require a recorded, active participation profile.
            if category in cat.CONSENT_REQUIRED_CATEGORIES and profile is None:
                raise ConsentError(
                    "an accepted participation policy is required before this category"
                )
            grants = DataConsentGrantRepository(session)
            grant = grants.create(
                profile_id=profile.id if profile else None, tenant_id=self.tenant_id,
                node_id=self.node_id, subject=subject, category=category,
                purpose=purpose
                or cat.CATEGORY_DEFAULT_PURPOSE.get(category, cat.PURPOSE_OPERATIONS),
                destination_scope=destination_scope,
                collection_scope="enabled" if collection else "disabled",
                export_scope="enabled" if export else "disabled",
                raw_artifact_scope="enabled" if raw_artifact else "disabled",
                policy_version=policy_version or (profile.policy_version if profile else None),
                terms_digest=terms_digest, granted_at=now, effective_at=now,
                expires_at=expires_at, revocation_state="active", source=source,
            )
            revision = DataConsentRevisionRepository(session).create(
                grant_id=grant.id, revision_no=1, state="granted",
                scopes_json={"collection": collection, "export": export,
                             "raw_artifact": raw_artifact}, source=source,
            )
            grant.current_revision_id = revision.id
            self._audit(session, event_type=ev.EVENT_CONSENT_GRANTED, category=category,
                        detail=f"export={export} raw={raw_artifact}")
            session.commit()
            return {"grant_id": grant.id, "category": category, "export": export,
                    "raw_artifact": raw_artifact, "revision_id": revision.id}

    def withdraw_consent(self, grant_id: str, *, reason: str = "", quarantine: bool = True) -> dict:
        """Withdraw a grant. Blocks new exports immediately and reconciles queued records."""
        with self.runtime.session_scope() as session:
            grants = DataConsentGrantRepository(session)
            grant = grants.get(grant_id)
            if grant is None or grant.tenant_id != self.tenant_id:
                raise NotFoundError("grant not found")
            grant.revocation_state = "withdrawn"
            revisions = DataConsentRevisionRepository(session)
            grant.current_revision_id = revisions.create(
                grant_id=grant.id, revision_no=revisions.next_revision_no(grant.id),
                state="withdrawn", reason=reason[:300] or "withdrawn", source="operator",
            ).id
            # Reconcile queued (not-yet-delivered) records of this category: quarantine (default)
            # or cancel — never delete the audit evidence.
            reconciled = 0
            records = DataExportRecordRepository(session)
            target_state = cat.RECORD_QUARANTINED if quarantine else cat.RECORD_CANCELLED
            for rec in records.list(category=grant.category, tenant_id=self.tenant_id, limit=10000):
                if rec.status in (cat.RECORD_COLLECTED, cat.RECORD_HELD, cat.RECORD_APPROVED,
                                  cat.RECORD_BATCHED):
                    rec.status = target_state
                    reconciled += 1
            self._audit(session, event_type=ev.EVENT_CONSENT_WITHDRAWN, category=grant.category,
                        detail=f"reconciled={reconciled}")
            session.commit()
            grant_category = grant.category
        with contextlib.suppress(RuntimeError):
            import asyncio
            asyncio.get_running_loop().create_task(
                self._emit(ev.EVENT_CONSENT_WITHDRAWN, "Consent withdrawn.",
                           category=grant_category)
            )
        return {"grant_id": grant_id, "state": "withdrawn", "reconciled": reconciled}

    def effective_consent(self) -> dict:
        with self.runtime.session_scope() as session:
            return consent_eval.effective_by_category(
                session, tenant_id=self.tenant_id, node_id=self.node_id, config=self.config,
            )

    def list_grants(self) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [
                {"grant_id": g.id, "category": g.category, "purpose": g.purpose,
                 "collection_scope": g.collection_scope, "export_scope": g.export_scope,
                 "raw_artifact_scope": g.raw_artifact_scope, "revocation_state": g.revocation_state,
                 "expires_at": _iso(g.expires_at), "created_at": _iso(g.created_at)}
                for g in DataConsentGrantRepository(session).list(tenant_id=self.tenant_id)
            ]

    # -- collection -------------------------------------------------------------

    def collect(
        self, *, category: str, payload: dict, source_type: str | None = None,
        source_id: str | None = None, sensitivity: str | None = None,
        subject: str | None = None, mission_id: str | None = None, run_id: str | None = None,
        step_id: str | None = None, conversation_id: str | None = None,
        artifact_id: str | None = None, correlation_id: str | None = None,
    ) -> str | None:
        """Collect one candidate record. Returns its id, or None if excluded by policy/consent.

        Hidden collection is impossible: an excluded candidate is never stored. Sensitive
        categories without effective consent are excluded here, not silently kept.
        """
        if not self.enabled or self.collection_paused:
            return None
        if category not in cat.ALL_CATEGORIES:
            raise ValidationError(f"unknown category: {category}")
        # Fail closed: sensitive categories are NEVER collected without a real pseudonym key
        # (no silent fallback to a public/predictable value).
        if category in cat.CONSENT_REQUIRED_CATEGORIES and not self.pseudonymization_ready():
            with self.runtime.session_scope() as session:
                self._audit(session, event_type=ev.EVENT_RECORD_REJECTED, category=category,
                            detail=self._pseudonymization_reason())
                session.commit()
            return None
        with self.runtime.session_scope() as session:
            cc = consent_eval.evaluate(
                session, tenant_id=self.tenant_id, node_id=self.node_id, category=category,
                config=self.config,
            )
            cpol = DataCategoryPolicyRepository(session).get_for(
                self.tenant_id, self.node_id, category
            )
            decision = policy_engine.decide(
                category=category,
                sensitivity=sensitivity or cat.CATEGORY_DEFAULT_SENSITIVITY[category],
                consent=cc, category_policy=cpol, privacy_config=self.config.privacy,
                redaction_policy_version=self.config.privacy.redaction_policy_version,
            )
            if decision.decision == cat.DECISION_EXCLUDE:
                return None

            # Redact + minimize BEFORE the record is persisted.
            redactor = self._redactor()
            redaction = redactor.redact(payload if isinstance(payload, dict) else {})
            if redaction.rejected or contains_residual_secret(redaction.redacted):
                self._audit(session, event_type=ev.EVENT_RECORD_REJECTED, category=category,
                            detail="redaction_rejected")
                session.commit()
                return None
            if not record_builder.payload_within_bounds(redaction.redacted):
                redaction.redacted = {"_truncated": True}

            pkey = self._pseudo_key()
            subj_pseu = (
                pseudonymize(subject, key=pkey, namespace="subject")
                if subject and pkey is not None else None
            )
            retention_days = self._retention_days(decision.retention_class)
            fields = record_builder.build_record_fields(
                category=category, sensitivity=sensitivity, tenant_id=self.tenant_id,
                node_pseudonym=self.node_pseudonym(), subject_pseudonym=subj_pseu,
                payload=redaction.redacted,
                redaction_policy_version=decision.redaction_policy_version,
                retention_class=decision.retention_class, consent_revision_id=cc.revision_id,
                source_type=source_type, source_id=source_id, mission_id=mission_id,
                run_id=run_id, step_id=step_id, conversation_id=conversation_id,
                artifact_id=artifact_id, correlation_id=correlation_id,
                lineage={
                    "redaction_actions": sorted(set(redaction.actions))[:32],
                    # Non-secret key id + policy version recorded so future rotation is traceable.
                    "pseudonymization_key_id": self.config.privacy.pseudonymization_key_id,
                    "pseudonymization_policy_version":
                        self.config.privacy.pseudonymization_policy_version,
                },
            )
            byte_size = record_builder.payload_size(redaction.redacted)
            record = DataExportRecordRepository(session).create(
                **fields, status=decision.initial_status,
                held_reason="inspect_before_export" if decision.initial_status == cat.RECORD_HELD
                else None,
                byte_size=byte_size,
                expires_at=utcnow() + timedelta(days=retention_days),
            )
            self._audit(session, event_type=ev.EVENT_RECORD_COLLECTED, category=category,
                        detail=f"status={decision.initial_status}")
            session.commit()
            record_id = record.id
        return record_id

    def collect_operational(self, payload: dict, **kw) -> str | None:
        return self.collect(category=cat.CATEGORY_OPERATIONAL, payload=payload,
                            source_type="telemetry", **kw)

    def collect_operational_snapshot(self) -> str | None:
        """Collect ONE bounded operational-telemetry record from the node's own real state.

        The payload is server-derived (software version, runtime status, mission count,
        readiness) — the operator triggers a snapshot but cannot inject arbitrary content. This
        is the automatic operational telemetry; it carries no objectives/prompts/secrets.
        """
        snapshot: dict = {"software_version": getattr(self.runtime, "version", "unknown"),
                          "runtime_status": getattr(self.runtime, "runtime_status", "unknown")}
        with contextlib.suppress(Exception):
            from aithernet.state.repositories import MissionRepository
            with self.runtime.session_scope() as session:
                snapshot["mission_count"] = MissionRepository(session).count()
        return self.collect_operational(snapshot)

    def collect_analytics(self, payload: dict, **kw) -> str | None:
        return self.collect(category=cat.CATEGORY_ANALYTICS, payload=payload,
                            source_type="analytics", **kw)

    def collect_training(self, payload: dict, **kw) -> str | None:
        return self.collect(category=cat.CATEGORY_TRAINING, payload=payload,
                            source_type="trajectory", **kw)

    def collect_raw_artifact_metadata(self, payload: dict, **kw) -> str | None:
        return self.collect(category=cat.CATEGORY_RAW_ARTIFACT, payload=payload,
                            source_type="artifact", **kw)

    def _retention_days(self, retention_class: str) -> int:
        r = self.config.retention
        return {
            cat.RETENTION_OPERATIONAL: r.operational_days,
            cat.RETENTION_ANALYTICS: r.analytics_days,
            cat.RETENTION_TRAINING: r.training_days,
            cat.RETENTION_RAW_ARTIFACT: r.raw_artifact_days,
            cat.RETENTION_DELIVERY_EVIDENCE: r.delivery_evidence_days,
            cat.RETENTION_CONSENT_AUDIT: r.consent_audit_days,
        }.get(retention_class, r.operational_days)

    # -- inspection -------------------------------------------------------------

    def list_records(self, *, category: str | None = None, status: str | None = None,
                     limit: int = 200) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [
                _record_summary(r)
                for r in DataExportRecordRepository(session).list(
                    category=category, status=status, tenant_id=self.tenant_id, limit=limit
                )
            ]

    def get_record(self, record_id: str, *, include_payload: bool = True) -> dict:
        with self.runtime.session_scope() as session:
            r = DataExportRecordRepository(session).get(record_id)
            if r is None or r.tenant_id != self.tenant_id:
                raise NotFoundError("record not found")
            summary = _record_summary(r)
            if include_payload:
                # The stored payload is ALREADY redacted — safe for operator inspection.
                summary["payload"] = r.payload_json or {}
                summary["lineage"] = r.lineage_json or {}
            return summary

    def preview_redaction(self, category: str, payload: dict) -> dict:
        red = self._redactor().redact(payload if isinstance(payload, dict) else {})
        return {"redacted": red.redacted, "actions": sorted(set(red.actions)),
                "rejected": red.rejected, "requires_approval": red.requires_approval}

    def approve_record(self, record_id: str) -> dict:
        return self._set_record_status(record_id, cat.RECORD_APPROVED,
                                       from_states=(cat.RECORD_HELD, cat.RECORD_COLLECTED),
                                       event=ev.EVENT_RECORD_APPROVED)

    def reject_record(self, record_id: str) -> dict:
        return self._set_record_status(record_id, cat.RECORD_CANCELLED,
                                       from_states=(cat.RECORD_HELD, cat.RECORD_COLLECTED,
                                                    cat.RECORD_APPROVED),
                                       event=ev.EVENT_RECORD_REJECTED)

    def _set_record_status(self, record_id, status, *, from_states, event) -> dict:
        with self.runtime.session_scope() as session:
            r = DataExportRecordRepository(session).get(record_id)
            if r is None or r.tenant_id != self.tenant_id:
                raise NotFoundError("record not found")
            if r.status not in from_states:
                raise ValidationError(f"record is {r.status}, cannot transition")
            r.status = status
            self._audit(session, event_type=event, category=r.category)
            session.commit()
            return {"record_id": record_id, "status": status}

    def delete_record(self, record_id: str) -> dict:
        """Operator deletes a queued record (content cleared; tombstone kept for lineage)."""
        with self.runtime.session_scope() as session:
            r = DataExportRecordRepository(session).get(record_id)
            if r is None or r.tenant_id != self.tenant_id:
                raise NotFoundError("record not found")
            r.status = cat.RECORD_DELETED
            r.payload_json = {}
            r.deletion_state = "deleted"
            DataDatasetMemberRepository(session).mark_excluded(record_id, utcnow())
            session.commit()
            return {"record_id": record_id, "status": "deleted"}

    def pause_collection(self, paused: bool) -> dict:
        self.collection_paused = paused
        return {"collection_paused": paused}

    def pause_export(self, paused: bool) -> dict:
        self.export_paused = paused
        return {"export_paused": paused}

    # -- destinations -----------------------------------------------------------

    def add_destination(self, *, kind: str, name: str, config: dict | None = None) -> dict:
        if kind not in cat.ALL_DESTINATION_KINDS:
            raise ValidationError(f"unknown destination kind: {kind}")
        sanitized = self._sanitize_destination_config(kind, config or {})
        with self.runtime.session_scope() as session:
            dest = DataExportDestinationRepository(session).create(
                tenant_id=self.tenant_id, kind=kind, name=name[:120], enabled=False,
                status="configured", config_json=sanitized, readiness="unknown",
            )
            session.commit()
            return self._destination_summary(dest)

    def _sanitize_destination_config(self, kind: str, config: dict) -> dict:
        """Keep only non-secret config — refs (env var NAMES) only, never secret VALUES."""
        allowed = {
            cat.DEST_LOCAL: ("root",),
            cat.DEST_AIRGAPPED: ("bundle_root",),
            cat.DEST_HTTP: ("base_url", "tenant_id", "node_id", "credential_ref", "key_id"),
            cat.DEST_DRIVE: ("folder_id", "oauth_credential_ref", "encryption_key_ref"),
        }.get(kind, ())
        return {k: config[k] for k in allowed if k in config and isinstance(config[k], (str, int))}

    def set_destination_enabled(self, destination_id: str, enabled: bool) -> dict:
        with self.runtime.session_scope() as session:
            dest = DataExportDestinationRepository(session).get(destination_id)
            if dest is None or dest.tenant_id != self.tenant_id:
                raise NotFoundError("destination not found")
            built = self._build_destination(dest)
            ok, reason = built.validate()
            if enabled and not ok:
                raise ValidationError(f"destination invalid: {reason}")
            dest.enabled = enabled
            dest.status = "enabled" if enabled else "disabled"
            self._audit(session, event_type=ev.EVENT_DESTINATION_ENABLED if enabled
                        else ev.EVENT_DESTINATION_DISABLED, detail=f"kind={dest.kind}")
            session.commit()
            return self._destination_summary(dest)

    def verify_destination(self, destination_id: str) -> dict:
        with self.runtime.session_scope() as session:
            dest = DataExportDestinationRepository(session).get(destination_id)
            if dest is None or dest.tenant_id != self.tenant_id:
                raise NotFoundError("destination not found")
            built = self._build_destination(dest)
            ready = built.readiness()
            dest.readiness = ready.state
            dest.last_error = None if ready.ready else ready.detail
            session.commit()
            return {"destination_id": destination_id, "ready": ready.ready,
                    "state": ready.state, "detail": ready.detail}

    def list_destinations(self) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [
                self._destination_summary(d)
                for d in DataExportDestinationRepository(session).list(tenant_id=self.tenant_id)
            ]

    def _build_destination(self, dest):
        cfg = dest.config_json or {}
        if dest.kind == cat.DEST_LOCAL:
            root = cfg.get("root") or self.config.destinations.local.root or self._default_root()
            return LocalArchiveDestination(
                root=root, minimum_free_bytes=self.config.export.minimum_free_disk_bytes
            )
        if dest.kind == cat.DEST_AIRGAPPED:
            root = (cfg.get("bundle_root") or self.config.air_gapped.bundle_root
                    or self._default_root("air-gapped"))
            return AirGappedBundleDestination(
                root=root, minimum_free_bytes=self.config.export.minimum_free_disk_bytes
            )
        if dest.kind == cat.DEST_HTTP:
            return HTTPIngestionDestination(
                base_url=cfg.get("base_url") or self.config.destinations.ingestion.base_url or "",
                tenant_id=cfg.get("tenant_id") or self.tenant_id,
                node_id=cfg.get("node_id") or self.node_id,
                credential_ref=cfg.get("credential_ref")
                or self.config.destinations.ingestion.credential_ref,
                key_id=cfg.get("key_id", "default"),
                client_factory=self._http_client_factory,
            )
        if dest.kind == cat.DEST_DRIVE:
            return GoogleDriveArchiveDestination(
                folder_id=cfg.get("folder_id") or self.config.destinations.google_drive.folder_id,
                client=self._drive_client,
            )
        raise ValidationError(f"unknown destination kind: {dest.kind}")

    def _default_root(self, sub: str = "archive") -> str:
        return os.path.join(self._blob_store().root, sub)

    def _ensure_default_destination(self) -> None:
        if not self.config.destinations.local.enabled:
            return
        with self.runtime.session_scope() as session:
            repo = DataExportDestinationRepository(session)
            if any(d.kind == cat.DEST_LOCAL for d in repo.list(tenant_id=self.tenant_id)):
                return
            repo.create(
                tenant_id=self.tenant_id, kind=cat.DEST_LOCAL, name="default-local",
                enabled=True, status="enabled",
                config_json={"root": self.config.destinations.local.root or self._default_root()},
                readiness="unknown",
            )
            session.commit()

    # -- batches / delivery -----------------------------------------------------

    def build_batch(self, *, destination_id: str, category: str | None = None,
                    max_records: int | None = None) -> dict:
        """Seal approved records into an immutable, compressed (+encrypted) batch."""
        # Fail closed: no pseudonymized export without the master secret (training/raw/external).
        if not self.pseudonymization_ready():
            raise ValidationError(
                f"pseudonymization unavailable ({self._pseudonymization_reason()})"
            )
        max_records = min(max_records or self.config.export.maximum_batch_records,
                          self.config.export.maximum_batch_records)
        with self.runtime.session_scope() as session:
            dest = DataExportDestinationRepository(session).get(destination_id)
            if dest is None or dest.tenant_id != self.tenant_id:
                raise NotFoundError("destination not found")
            if not dest.enabled:
                raise ValidationError("destination is not enabled")
            records = DataExportRecordRepository(session).batchable(
                self.tenant_id, category=category, limit=max_records
            )
            if not records:
                raise ValidationError("no approved records to batch")
            envelopes = [record_builder.envelope_from_record(r) for r in records]
            ids_sorted = sorted(r.id for r in records)
            idempotency_key = sha256_hex(("|".join(ids_sorted)).encode()).split(":", 1)[1][:48]
            existing = DataExportBatchRepository(session).get_by_idempotency(
                destination_id, idempotency_key
            )
            if existing is not None:
                return self._batch_summary(existing)

            enc_key = self._encryption_key_for(dest)
            sealed = seal_batch(
                batch_id=new_uuid(), tenant_id=self.tenant_id,
                node_pseudonym=self.node_pseudonym(), destination_id=destination_id,
                idempotency_key=idempotency_key, record_envelopes=envelopes,
                consent_summary=self._consent_summary(records),
                retention_summary=self._retention_summary(records),
                created_at_iso=utcnow().isoformat(), encryption_key_b64=enc_key,
            )
            rel, _digest = self._blob_store().put(sealed.bundle)
            batch = DataExportBatchRepository(session).create(
                id=sealed.manifest["batch_id"], tenant_id=self.tenant_id,
                node_pseudonym=self.node_pseudonym(), destination_id=destination_id,
                idempotency_key=idempotency_key, record_count=len(records),
                uncompressed_bytes=sealed.uncompressed_bytes,
                compressed_bytes=sealed.compressed_bytes,
                manifest_digest=sealed.manifest_digest, records_digest=sealed.records_digest,
                consent_summary_json=sealed.manifest["consent_summary"],
                retention_summary_json=sealed.manifest["retention_summary"],
                encryption_metadata_json=sealed.encryption_metadata,
                relative_object_path=rel, status=cat.BATCH_PENDING,
                max_attempts=self.config.export.maximum_attempts,
                next_attempt_at=utcnow(), sealed_at=utcnow(),
            )
            for r in records:
                r.status = cat.RECORD_BATCHED
                r.batch_id = batch.id
                r.destination_id = destination_id
            self._audit(session, event_type=ev.EVENT_BATCH_SEALED,
                        detail=f"records={len(records)}")
            session.commit()
            return self._batch_summary(batch)

    def _encryption_key_for(self, dest) -> str | None:
        """Load the AES key for an external destination from an env-referenced secret."""
        if dest.kind == cat.DEST_DRIVE:
            ref = (dest.config_json or {}).get("encryption_key_ref") \
                or self.config.destinations.google_drive.encryption_key_ref
            return os.environ.get(ref) if ref else None
        return None

    async def deliver_batch(self, batch_id: str, *, owner: str) -> None:
        """Worker callback: deliver one claimed batch; persist terminal/retry/dead-letter state."""
        with self.runtime.session_scope() as session:
            batch = DataExportBatchRepository(session).get(batch_id)
            if batch is None:
                return
            dest_row = DataExportDestinationRepository(session).get(batch.destination_id)
            attempt_no = batch.attempt_count
            rel = batch.relative_object_path
            manifest = {
                "batch_id": batch.id, "records_digest": batch.records_digest,
                "record_count": batch.record_count,
            }
            idempotency_key = batch.idempotency_key

        if dest_row is None or not dest_row.enabled:
            self._finalize_failure(batch_id, owner=owner, attempt_no=attempt_no,
                                   category="destination_unavailable")
            return
        try:
            bundle = self._blob_store().get(rel)
        except Exception:  # noqa: BLE001
            self._finalize_failure(batch_id, owner=owner, attempt_no=attempt_no,
                                   category="bundle_missing")
            return

        destination = self._build_destination(dest_row)
        await self._emit(ev.EVENT_DELIVERY_STARTED, "Batch delivery started.")
        result = await destination.upload(
            batch_id=batch_id, idempotency_key=idempotency_key, bundle=bundle, manifest=manifest,
        )
        if result.ok:
            self._finalize_success(batch_id, owner=owner, attempt_no=attempt_no,
                                   receipt=result.receipt)
            await self._emit(ev.EVENT_DELIVERY_SUCCEEDED, "Batch delivered.")
        else:
            self._finalize_failure(batch_id, owner=owner, attempt_no=attempt_no,
                                   category=result.failure_category or "unknown",
                                   detail=result.detail)

    def _finalize_success(self, batch_id, *, owner, attempt_no, receipt) -> None:
        with self.runtime.session_scope() as session:
            now = utcnow()
            DataDeliveryAttemptRepository(session).create(
                batch_id=batch_id, destination_id="", attempt_no=attempt_no, outcome="success",
                started_at=now,
            )
            DataExportBatchRepository(session).release_and_update(
                batch_id, owner=owner,
                fields={"status": cat.BATCH_RECEIPT_VERIFIED, "delivered_at": now,
                        "receipt_json": receipt, "failure_category": None},
            )
            for r in DataExportRecordRepository(session).list_for_batch(batch_id):
                r.status = cat.RECORD_RECEIPT_VERIFIED
                r.delivered_at = now
            session.commit()

    def _finalize_failure(self, batch_id, *, owner, attempt_no, category, detail="") -> None:
        cfg = self.config.export
        with self.runtime.session_scope() as session:
            now = utcnow()
            batch = DataExportBatchRepository(session).get(batch_id)
            if batch is None:
                return
            DataDeliveryAttemptRepository(session).create(
                batch_id=batch_id, destination_id="", attempt_no=attempt_no, outcome="failure",
                failure_category=category, detail=(detail or "")[:300], started_at=now,
            )
            if batch.attempt_count >= batch.max_attempts:
                DataExportBatchRepository(session).release_and_update(
                    batch_id, owner=owner,
                    fields={"status": cat.BATCH_DEAD_LETTER, "failure_category": category,
                            "dead_letter_reason": f"{category}:{detail}"[:300]},
                )
                dead = True
            else:
                backoff = min(cfg.retry_max_seconds,
                              cfg.retry_base_seconds * (2 ** max(0, batch.attempt_count - 1)))
                backoff += random.uniform(0, cfg.retry_base_seconds)
                DataExportBatchRepository(session).release_and_update(
                    batch_id, owner=owner,
                    fields={"status": cat.BATCH_RETRY_WAIT, "failure_category": category,
                            "next_attempt_at": now + timedelta(seconds=backoff)},
                )
                dead = False
            session.commit()
        with contextlib.suppress(RuntimeError):
            import asyncio
            event = ev.EVENT_DELIVERY_DEAD_LETTERED if dead else ev.EVENT_DELIVERY_RETRY_SCHEDULED
            asyncio.get_running_loop().create_task(
                self._emit(event, "Batch delivery failure.", payload={"category": category})
            )

    def redrive(self, batch_id: str) -> dict:
        with self.runtime.session_scope() as session:
            batch = DataExportBatchRepository(session).get(batch_id)
            if batch is None or batch.tenant_id != self.tenant_id:
                raise NotFoundError("batch not found")
            if batch.status != cat.BATCH_DEAD_LETTER:
                raise ValidationError("only dead-lettered batches can be redriven")
            batch.status = cat.BATCH_PENDING
            batch.attempt_count = 0
            batch.next_attempt_at = utcnow()
            batch.dead_letter_reason = None
            session.commit()
            return {"batch_id": batch_id, "status": "pending"}

    def list_batches(self, *, status: str | None = None) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [self._batch_summary(b)
                    for b in DataExportBatchRepository(session).list(status=status)]

    def get_batch(self, batch_id: str) -> dict:
        with self.runtime.session_scope() as session:
            b = DataExportBatchRepository(session).get(batch_id)
            if b is None or b.tenant_id != self.tenant_id:
                raise NotFoundError("batch not found")
            summary = self._batch_summary(b)
            summary["attempts"] = [
                {"attempt_no": a.attempt_no, "outcome": a.outcome,
                 "failure_category": a.failure_category}
                for a in DataDeliveryAttemptRepository(session).list_for_batch(batch_id)
            ]
            return summary

    def _consent_summary(self, records) -> dict:
        out: dict[str, int] = {}
        for r in records:
            out[r.category] = out.get(r.category, 0) + 1
        return {"category_counts": out}

    def _retention_summary(self, records) -> dict:
        out: dict[str, int] = {}
        for r in records:
            out[r.retention_class] = out.get(r.retention_class, 0) + 1
        return {"retention_classes": out}

    # -- retention / deletion ---------------------------------------------------

    async def run_retention(self) -> dict:
        with self.runtime.session_scope() as session:
            expired = DataExportRecordRepository(session).expire_old(utcnow())
            session.commit()
        if expired:
            await self._emit(ev.EVENT_RETENTION_EXPIRED, "Records expired by retention.",
                             payload={"count": expired})
        return {"expired": expired}

    def request_deletion(
        self, *, scope: str, target_id: str | None = None, subject: str | None = None,
        category: str | None = None, reason: str = "", hold: bool = False,
    ) -> dict:
        """Create + execute a traceable deletion that propagates through dataset lineage."""
        if scope not in ("record", "subject", "batch", "category"):
            raise ValidationError("scope must be record|subject|batch|category")
        _pk = self._pseudo_key()
        subj_pseu = (
            pseudonymize(subject, key=_pk, namespace="subject")
            if subject and _pk is not None else None
        )
        with self.runtime.session_scope() as session:
            req = DataDeletionRequestRepository(session).create(
                tenant_id=self.tenant_id, scope=scope, target_id=target_id,
                subject_pseudonym=subj_pseu, category=category, reason=reason[:300] or None,
                status="processing", hold=hold,
            )
            self._audit(session, event_type=ev.EVENT_DELETION_REQUESTED, category=category,
                        detail=f"scope={scope}")
            session.commit()
            deletion_id = req.id
        if not hold:
            self._execute_deletion(deletion_id)
        return self.get_deletion(deletion_id)

    def _execute_deletion(self, deletion_id: str) -> None:
        with self.runtime.session_scope() as session:
            req = DataDeletionRequestRepository(session).get(deletion_id)
            if req is None or req.hold:
                return
            records_repo = DataExportRecordRepository(session)
            members = DataDatasetMemberRepository(session)
            targets = self._deletion_targets(session, req)
            deleted = 0
            dataset_impact = 0
            for rec in targets:
                rec.status = cat.RECORD_DELETED
                rec.payload_json = {}
                rec.deletion_state = "deleted"
                dataset_impact += members.mark_excluded(rec.id, utcnow())
                deleted += 1
            req.local_deleted = deleted
            req.status = "completed"
            req.executed_at = utcnow()
            req.impact_json = {"records_deleted": deleted,
                               "dataset_members_excluded": dataset_impact}
            req.receipt_json = {"local": {"records_deleted": deleted, "tombstoned": True}}
            self._audit(session, event_type=ev.EVENT_DELETION_COMPLETED,
                        detail=f"deleted={deleted}")
            session.commit()
            _ = records_repo  # repo used via targets

    def _deletion_targets(self, session, req):
        repo = DataExportRecordRepository(session)
        if req.scope == "record" and req.target_id:
            r = repo.get(req.target_id)
            return [r] if r and r.tenant_id == self.tenant_id else []
        if req.scope == "batch" and req.target_id:
            return repo.list_for_batch(req.target_id)
        if req.scope == "subject" and req.subject_pseudonym:
            return repo.list_for_subject(self.tenant_id, req.subject_pseudonym)
        if req.scope == "category" and req.category:
            return repo.list(category=req.category, tenant_id=self.tenant_id, limit=100000)
        return []

    def get_deletion(self, deletion_id: str) -> dict:
        with self.runtime.session_scope() as session:
            req = DataDeletionRequestRepository(session).get(deletion_id)
            if req is None or req.tenant_id != self.tenant_id:
                raise NotFoundError("deletion request not found")
            return _deletion_summary(req)

    def list_deletions(self) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [_deletion_summary(d)
                    for d in DataDeletionRequestRepository(session).list(tenant_id=self.tenant_id)]

    # -- datasets ---------------------------------------------------------------

    def create_dataset(self, *, name: str, purpose: str | None = None) -> dict:
        with self.runtime.session_scope() as session:
            ds = DataDatasetRepository(session).create(
                tenant_id=self.tenant_id, name=name[:120],
                purpose=purpose or cat.PURPOSE_MODEL_TRAINING, tenant_scope=self.tenant_id,
            )
            self._audit(session, event_type=ev.EVENT_DATASET_CREATED, detail=name[:80])
            session.commit()
            return {"dataset_id": ds.id, "name": ds.name, "purpose": ds.purpose}

    def create_dataset_version(
        self, dataset_id: str, *, split_label: str | None = None,
        exclusions: list[str] | None = None,
    ) -> dict:
        """Create an immutable dataset version from consented, delivered training records."""
        exclusions = set(exclusions or [])
        with self.runtime.session_scope() as session:
            ds = DataDatasetRepository(session).get(dataset_id)
            if ds is None or ds.tenant_id != self.tenant_id:
                raise NotFoundError("dataset not found")
            records_repo = DataExportRecordRepository(session)
            # Reproducible membership: training records that reached receipt_verified/delivered.
            members = [
                r for r in records_repo.list(category=cat.CATEGORY_TRAINING,
                                             tenant_id=self.tenant_id, limit=100000)
                if r.status in (cat.RECORD_RECEIPT_VERIFIED, cat.RECORD_DELIVERED)
                and r.id not in exclusions and r.deletion_state != "deleted"
            ]
            if not members:
                raise ValidationError("no eligible training records for a dataset version")
            versions = DataDatasetVersionRepository(session)
            version_no = versions.next_version(dataset_id)
            digests = sorted(r.payload_digest for r in members)
            content_digest = sha256_hex(("|".join(digests)).encode())
            batch_ids = sorted({r.batch_id for r in members if r.batch_id})
            category_counts = {cat.CATEGORY_TRAINING: len(members)}
            hw = sorted({(r.payload_json or {}).get("device_family") for r in members
                         if (r.payload_json or {}).get("device_family")})
            outcomes: dict[str, int] = {}
            dates = sorted(r.created_at for r in members if r.created_at)
            for r in members:
                o = (r.payload_json or {}).get("outcome") or "unknown"
                outcomes[o] = outcomes.get(o, 0) + 1
            version = versions.create(
                dataset_id=dataset_id, version=version_no, purpose=ds.purpose,
                tenant_scope=ds.tenant_scope,
                selection_policy_version="s1",
                consent_policy_versions_json=sorted({
                    (DataConsentGrantRepository(session).get(r.consent_revision_id) and "") or "p1"
                    for r in members
                }) or ["p1"],
                redaction_policy_version=self.config.privacy.redaction_policy_version,
                source_batch_ids_json=batch_ids, category_counts_json=category_counts,
                date_range_json={"start": _iso(dates[0]) if dates else None,
                                 "end": _iso(dates[-1]) if dates else None},
                hardware_families_json=hw, outcome_distribution_json=outcomes,
                content_digest=content_digest,
                manifest_json={"member_count": len(members), "content_digest": content_digest,
                               "source_batch_ids": batch_ids},
                exclusions_json=sorted(exclusions), split_label=split_label,
                member_count=len(members),
            )
            member_repo = DataDatasetMemberRepository(session)
            for r in members:
                member_repo.create(version_id=version.id, record_id=r.id, category=r.category)
            self._audit(session, event_type=ev.EVENT_DATASET_VERSION_CREATED,
                        detail=f"v{version_no} members={len(members)}")
            session.commit()
            return self._version_summary(version)

    def list_datasets(self) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [{"dataset_id": d.id, "name": d.name, "purpose": d.purpose,
                     "created_at": _iso(d.created_at)}
                    for d in DataDatasetRepository(session).list(tenant_id=self.tenant_id)]

    def list_dataset_versions(self, dataset_id: str) -> list[dict]:
        with self.runtime.session_scope() as session:
            return [self._version_summary(v)
                    for v in DataDatasetVersionRepository(session).list_for_dataset(dataset_id)]

    def dataset_manifest(self, version_id: str) -> dict:
        with self.runtime.session_scope() as session:
            v = DataDatasetVersionRepository(session).get(version_id)
            if v is None:
                raise NotFoundError("dataset version not found")
            members = DataDatasetMemberRepository(session).list_for_version(version_id)
            return {
                **self._version_summary(v),
                "manifest": v.manifest_json,
                "members": [{"record_id": m.record_id, "excluded": m.excluded} for m in members],
            }

    # -- diagnostics ------------------------------------------------------------

    def diagnostics(self) -> dict:
        with self.runtime.session_scope() as session:
            rec_counts = DataExportRecordRepository(session).counts_by_status()
            batch_counts = DataExportBatchRepository(session).counts_by_status()
            dests = DataExportDestinationRepository(session).list(tenant_id=self.tenant_id)
            dest_health = []
            for d in dests:
                with contextlib.suppress(Exception):
                    dest_health.append({"destination_id": d.id, "kind": d.kind,
                                        "enabled": d.enabled,
                                        **self._build_destination(d).health()})
        free = 0
        with contextlib.suppress(Exception):
            import shutil
            free = shutil.disk_usage(self._blob_store().root).free
        return {
            "enabled": self.enabled,
            "tenant_id": self.tenant_id,
            "collection_paused": self.collection_paused,
            "export_enabled": self.config.export.enabled,
            "export_paused": self.export_paused,
            "worker_running": self.worker.is_running(),
            "worker_degraded": self.worker.is_degraded(),
            "records_by_status": rec_counts,
            "pending_records": rec_counts.get("collected", 0) + rec_counts.get("held", 0)
            + rec_counts.get("approved", 0),
            "held_records": rec_counts.get("held", 0),
            "batches_by_status": batch_counts,
            "pending_batches": batch_counts.get("pending", 0) + batch_counts.get("retry_wait", 0),
            "dead_letter_batches": batch_counts.get("dead_letter", 0),
            "destinations": dest_health,
            "free_disk_bytes": free,
            "air_gapped": self.config.air_gapped.enabled,
            "drive_status": self._drive_status(),
            "pseudonymization_ready": self.pseudonymization_ready(),
            "pseudonymization_reason": self._pseudonymization_reason(),
            "pseudonymization_key_id": self.config.privacy.pseudonymization_key_id,
        }

    def _drive_status(self) -> dict:
        gd = self.config.destinations.google_drive
        return {"configured": bool(gd.folder_id), "enabled": gd.enabled,
                "credentials_present": bool(gd.oauth_credential_ref
                                            and os.environ.get(gd.oauth_credential_ref))}

    # -- summaries --------------------------------------------------------------

    def _destination_summary(self, d) -> dict:
        return {"destination_id": d.id, "kind": d.kind, "name": d.name, "enabled": d.enabled,
                "status": d.status, "readiness": d.readiness, "config": d.config_json,
                "last_error": d.last_error, "created_at": _iso(d.created_at)}

    def _batch_summary(self, b) -> dict:
        return {"batch_id": b.id, "destination_id": b.destination_id, "status": b.status,
                "record_count": b.record_count, "uncompressed_bytes": b.uncompressed_bytes,
                "compressed_bytes": b.compressed_bytes, "manifest_digest": b.manifest_digest,
                "records_digest": b.records_digest, "encrypted": b.encryption_metadata_json
                is not None, "attempt_count": b.attempt_count, "max_attempts": b.max_attempts,
                "failure_category": b.failure_category, "delivered_at": _iso(b.delivered_at),
                "receipt": b.receipt_json, "created_at": _iso(b.created_at)}

    def _version_summary(self, v) -> dict:
        return {"version_id": v.id, "dataset_id": v.dataset_id, "version": v.version,
                "purpose": v.purpose, "member_count": v.member_count,
                "content_digest": v.content_digest, "category_counts": v.category_counts_json,
                "hardware_families": v.hardware_families_json,
                "outcome_distribution": v.outcome_distribution_json,
                "source_batch_ids": v.source_batch_ids_json, "split_label": v.split_label,
                "deletion_status": v.deletion_status, "created_at": _iso(v.created_at)}


# -- module helpers -------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _record_summary(r) -> dict:
    return {"record_id": r.id, "category": r.category, "purpose": r.purpose,
            "sensitivity": r.sensitivity, "status": r.status, "byte_size": r.byte_size,
            "mission_id": r.mission_id, "batch_id": r.batch_id,
            "retention_class": r.retention_class, "expires_at": _iso(r.expires_at),
            "redaction_policy_version": r.redaction_policy_version,
            "created_at": _iso(r.created_at)}


def _deletion_summary(d) -> dict:
    return {"deletion_id": d.id, "scope": d.scope, "target_id": d.target_id,
            "category": d.category, "status": d.status, "hold": d.hold,
            "local_deleted": d.local_deleted, "remote_state": d.remote_state,
            "impact": d.impact_json, "receipt": d.receipt_json,
            "requested_at": _iso(d.requested_at), "executed_at": _iso(d.executed_at)}
