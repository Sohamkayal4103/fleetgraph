"""Deterministic selection/policy engine (Stage 14E, Part 7).

Given a candidate record's category + sensitivity, the effective consent, the node's typed
category policy, and the privacy config, this decides the record's fate using EXPLICIT TYPED
RULES — never a user-supplied expression, Python, SQL predicate, or regex over private data.
The coordinator never participates in telemetry policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from aithernet.data import categories as cat
from aithernet.data.consent import CategoryConsent

_HELD_SENSITIVITIES = (cat.SENSITIVITY_HIGH, cat.SENSITIVITY_RESTRICTED)


@dataclass
class PolicyDecision:
    decision: str            # collect | exclude | queue_export | require_approval
    initial_status: str      # the DataExportRecord.status to persist
    retention_class: str
    redaction_policy_version: str
    reason: str


def decide(
    *,
    category: str,
    sensitivity: str,
    consent: CategoryConsent,
    category_policy,        # DataCategoryPolicy | None
    privacy_config,
    retention_class: str | None = None,
    redaction_policy_version: str = "r1",
) -> PolicyDecision:
    retention_class = retention_class or cat.CATEGORY_DEFAULT_RETENTION.get(
        category, cat.RETENTION_OPERATIONAL
    )

    # 1) No collection consent/permission for this category → exclude entirely (no hidden
    #    collection: an excluded record is never stored).
    if not consent.collection_allowed:
        return PolicyDecision(
            decision=cat.DECISION_EXCLUDE, initial_status=cat.RECORD_EXPIRED,
            retention_class=retention_class, redaction_policy_version=redaction_policy_version,
            reason=f"collection_not_allowed:{consent.reason}",
        )

    # 2) Collected. If export is not allowed, keep it local only.
    if not consent.export_allowed:
        return PolicyDecision(
            decision=cat.DECISION_COLLECT, initial_status=cat.RECORD_COLLECTED,
            retention_class=retention_class, redaction_policy_version=redaction_policy_version,
            reason="collect_local_only",
        )

    # 3) Export allowed — decide auto-approve vs. hold for explicit approval.
    require_approval = bool(
        getattr(privacy_config, "inspect_before_export", True)
        or (category_policy is not None and category_policy.require_approval)
        or sensitivity in _HELD_SENSITIVITIES
        or category in (cat.CATEGORY_TRAINING, cat.CATEGORY_RAW_ARTIFACT)
    )
    if require_approval:
        return PolicyDecision(
            decision=cat.DECISION_REQUIRE_APPROVAL, initial_status=cat.RECORD_HELD,
            retention_class=retention_class, redaction_policy_version=redaction_policy_version,
            reason="held_for_approval",
        )
    return PolicyDecision(
        decision=cat.DECISION_QUEUE_EXPORT, initial_status=cat.RECORD_APPROVED,
        retention_class=retention_class, redaction_policy_version=redaction_policy_version,
        reason="auto_approved",
    )
