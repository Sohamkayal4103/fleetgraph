"""Stage 14E — data categories, purposes, sensitivities, and lifecycle states.

These are explicit typed constants (never user-supplied expressions). They underpin the
deterministic policy engine: data is separated by *category* and *sensitivity*, and the four
categories are independent — consent for one never implies consent for another.
"""

from __future__ import annotations

# --- Data categories (Part 4) ----------------------------------------------
CATEGORY_OPERATIONAL = "operational"            # A. operational telemetry
CATEGORY_ANALYTICS = "analytics"                # B. product analytics
CATEGORY_TRAINING = "training"                  # C. model-training trajectories (opt-in)
CATEGORY_RAW_ARTIFACT = "raw_artifact"          # D. raw artifacts (separate opt-in)

ALL_CATEGORIES = (
    CATEGORY_OPERATIONAL,
    CATEGORY_ANALYTICS,
    CATEGORY_TRAINING,
    CATEGORY_RAW_ARTIFACT,
)

#: Categories that must NEVER default to enabled and require explicit consent.
CONSENT_REQUIRED_CATEGORIES = frozenset({CATEGORY_TRAINING, CATEGORY_RAW_ARTIFACT})

# --- Purposes (Part 5) -----------------------------------------------------
PURPOSE_OPERATIONS = "operations"
PURPOSE_PRODUCT_IMPROVEMENT = "product_improvement"
PURPOSE_MODEL_TRAINING = "model_training"
PURPOSE_ARCHIVE = "archive"

ALL_PURPOSES = (
    PURPOSE_OPERATIONS,
    PURPOSE_PRODUCT_IMPROVEMENT,
    PURPOSE_MODEL_TRAINING,
    PURPOSE_ARCHIVE,
)

#: The default purpose each category serves.
CATEGORY_DEFAULT_PURPOSE = {
    CATEGORY_OPERATIONAL: PURPOSE_OPERATIONS,
    CATEGORY_ANALYTICS: PURPOSE_PRODUCT_IMPROVEMENT,
    CATEGORY_TRAINING: PURPOSE_MODEL_TRAINING,
    CATEGORY_RAW_ARTIFACT: PURPOSE_ARCHIVE,
}

# --- Sensitivity levels ----------------------------------------------------
SENSITIVITY_LOW = "low"
SENSITIVITY_MODERATE = "moderate"
SENSITIVITY_HIGH = "high"
SENSITIVITY_RESTRICTED = "restricted"

CATEGORY_DEFAULT_SENSITIVITY = {
    CATEGORY_OPERATIONAL: SENSITIVITY_LOW,
    CATEGORY_ANALYTICS: SENSITIVITY_LOW,
    CATEGORY_TRAINING: SENSITIVITY_HIGH,
    CATEGORY_RAW_ARTIFACT: SENSITIVITY_RESTRICTED,
}

# --- Retention classes (Part 21) -------------------------------------------
RETENTION_OPERATIONAL = "operational"
RETENTION_ANALYTICS = "analytics"
RETENTION_DELIVERY_EVIDENCE = "delivery_evidence"
RETENTION_CONSENT_AUDIT = "consent_audit"
RETENTION_TRAINING = "training"
RETENTION_RAW_ARTIFACT = "raw_artifact"

CATEGORY_DEFAULT_RETENTION = {
    CATEGORY_OPERATIONAL: RETENTION_OPERATIONAL,
    CATEGORY_ANALYTICS: RETENTION_ANALYTICS,
    CATEGORY_TRAINING: RETENTION_TRAINING,
    CATEGORY_RAW_ARTIFACT: RETENTION_RAW_ARTIFACT,
}

# --- Policy-engine decisions (Part 7) --------------------------------------
DECISION_COLLECT = "collect"               # store locally only
DECISION_EXCLUDE = "exclude"               # drop entirely (no collection)
DECISION_QUEUE_EXPORT = "queue_export"     # collect + eligible for export
DECISION_REQUIRE_APPROVAL = "require_approval"  # collect + held for operator approval
DECISION_DELETE = "delete"

# --- Export-record / outbox lifecycle states (Part 10) ---------------------
RECORD_COLLECTED = "collected"
RECORD_HELD = "held"
RECORD_APPROVED = "approved"
RECORD_BATCHED = "batched"
RECORD_LEASED = "leased"
RECORD_UPLOADING = "uploading"
RECORD_DELIVERED = "delivered"
RECORD_RECEIPT_VERIFIED = "receipt_verified"
RECORD_RETRY_WAIT = "retry_wait"
RECORD_CANCELLED = "cancelled"
RECORD_QUARANTINED = "quarantined"
RECORD_DEAD_LETTER = "dead_letter"
RECORD_EXPIRED = "expired"
RECORD_DELETED = "deleted"

#: Record states an export batch-builder may select from (collected+approved, export-eligible).
BATCHABLE_RECORD_STATES = (RECORD_APPROVED,)

#: Batch states.
BATCH_OPEN = "open"
BATCH_SEALED = "sealed"
BATCH_PENDING = "pending"
BATCH_DELIVERING = "delivering"
BATCH_DELIVERED = "delivered"
BATCH_RECEIPT_VERIFIED = "receipt_verified"
BATCH_RETRY_WAIT = "retry_wait"
BATCH_DEAD_LETTER = "dead_letter"
BATCH_CANCELLED = "cancelled"

CLAIMABLE_BATCH_STATES = (BATCH_PENDING, BATCH_RETRY_WAIT)

# --- Destination kinds (Part 12) -------------------------------------------
DEST_LOCAL = "local_archive"
DEST_HTTP = "http_ingestion"
DEST_DRIVE = "google_drive"
DEST_AIRGAPPED = "air_gapped"

ALL_DESTINATION_KINDS = (DEST_LOCAL, DEST_HTTP, DEST_DRIVE, DEST_AIRGAPPED)

#: Destinations that leave the local node (require encryption for sensitive bundles).
EXTERNAL_DESTINATION_KINDS = frozenset({DEST_HTTP, DEST_DRIVE})

# --- Raw-artifact export scopes (Part 20) ----------------------------------
RAW_SCOPE_METADATA_ONLY = "metadata_only"
RAW_SCOPE_SELECTED = "selected_artifact"
RAW_SCOPE_CAMPAIGN = "campaign_artifacts"
RAW_SCOPE_ALL_APPROVED = "all_approved_artifacts"
