"""Effective-consent evaluation (Stage 14E, Part 5).

Consent is append-only: grants + revisions record history, and the *effective* state is
derived here, never inferred from application use. Unknown, expired, withdrawn, or superseded
state behaves as **not authorized**. Training and raw-artifact consent never default to enabled.

This module is pure with respect to policy: it reads the current grant for a category and the
node's collection config, and returns a typed :class:`CategoryConsent`. The export queue + the
policy engine consume it; the coordinator never participates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aithernet.data import categories as cat
from aithernet.state.models import DataConsentGrant, utcnow
from aithernet.state.repositories import DataConsentGrantRepository


def _aware(value: datetime | None) -> datetime | None:
    """Coerce a SQLite-loaded naive datetime to UTC-aware for safe comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class CategoryConsent:
    category: str
    collection_allowed: bool
    export_allowed: bool
    raw_artifact_allowed: bool
    grant_id: str | None
    revision_id: str | None
    revocation_state: str
    reason: str

    def to_summary(self) -> dict:
        return {
            "category": self.category,
            "collection_allowed": self.collection_allowed,
            "export_allowed": self.export_allowed,
            "raw_artifact_allowed": self.raw_artifact_allowed,
            "revocation_state": self.revocation_state,
            "grant_id": self.grant_id,
            "reason": self.reason,
        }


def _grant_is_effective(grant: DataConsentGrant | None, now: datetime) -> bool:
    if grant is None:
        return False
    if grant.revocation_state != "active":
        return False
    expires_at = _aware(grant.expires_at)
    effective_at = _aware(grant.effective_at)
    if expires_at is not None and expires_at < now:
        return False
    if effective_at is not None and effective_at > now:
        return False
    return True


def evaluate(
    session, *, tenant_id: str, node_id: str, category: str, config, now: datetime | None = None
) -> CategoryConsent:
    """Return the effective consent for one category on this node."""
    now = now or utcnow()
    grant = DataConsentGrantRepository(session).current_for_category(tenant_id, node_id, category)
    effective = _grant_is_effective(grant, now)
    collection = config.collection
    export_cfg = config.export

    # Collection eligibility (operator config + consent for sensitive categories).
    if category == cat.CATEGORY_OPERATIONAL:
        collection_allowed = collection.operational_enabled
    elif category == cat.CATEGORY_ANALYTICS:
        collection_allowed = collection.product_analytics_enabled
    elif category == cat.CATEGORY_TRAINING:
        collection_allowed = collection.training_trajectories_enabled and effective
    elif category == cat.CATEGORY_RAW_ARTIFACT:
        collection_allowed = collection.raw_artifacts_enabled and effective
    else:
        collection_allowed = False

    # Export eligibility — always requires the master export switch + an effective grant whose
    # export scope is enabled. Sensitive categories require their own category consent.
    grant_export = effective and grant is not None and grant.export_scope == "enabled"
    export_allowed = bool(export_cfg.enabled and grant_export)
    if category in (cat.CATEGORY_TRAINING, cat.CATEGORY_RAW_ARTIFACT):
        # Belt-and-suspenders: never export sensitive categories without an effective grant.
        export_allowed = export_allowed and effective

    raw_allowed = bool(
        category == cat.CATEGORY_RAW_ARTIFACT
        and effective
        and grant is not None
        and grant.raw_artifact_scope == "enabled"
    )

    reason = "ok"
    if grant is None:
        reason = "no_grant"
    elif not effective:
        reason = f"grant_{grant.revocation_state}"

    return CategoryConsent(
        category=category,
        collection_allowed=collection_allowed,
        export_allowed=export_allowed,
        raw_artifact_allowed=raw_allowed,
        grant_id=grant.id if grant else None,
        revision_id=grant.current_revision_id if grant else None,
        revocation_state=grant.revocation_state if grant else "none",
        reason=reason,
    )


def effective_by_category(session, *, tenant_id, node_id, config, now=None) -> dict[str, dict]:
    """Effective consent summary for all four categories (for the API/dashboard)."""
    return {
        category: evaluate(
            session, tenant_id=tenant_id, node_id=node_id, category=category,
            config=config, now=now,
        ).to_summary()
        for category in cat.ALL_CATEGORIES
    }
