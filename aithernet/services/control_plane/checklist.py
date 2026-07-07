"""Production-readiness checklist generator (Stage 14F §47).

Identifies unresolved items required before a real public deployment. It FAILS CLOSED: it never
reports production-ready while required items are missing. Items that depend on real infrastructure
or credentials (domain, DNS, TLS, production database, object storage, SMTP, Google Drive,
release-signing key, backup destination, legal documents, penetration review) stay unresolved
until explicitly provided — local tests passing does NOT make them resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from services.control_plane.config import HostedConfig


@dataclass
class ChecklistItem:
    key: str
    resolved: bool
    required: bool
    detail: str


# Items that require real infrastructure/credentials/legal/security review — never auto-resolved.
_NOT_EXECUTED = (
    ("domain_configured", "A real domain is configured for public URLs"),
    ("dns_verified", "Public DNS resolves to the deployment"),
    ("tls_verified", "Real TLS certificate issued and serving"),
    ("production_database_configured", "Central PostgreSQL configured (not SQLite)"),
    ("object_storage_configured", "Production S3-compatible object storage configured"),
    ("smtp_verified", "Real SMTP provider verified"),
    ("google_drive_verified", "Google Drive archive destination verified"),
    ("backup_destination_verified", "Off-host backup destination verified"),
    ("legal_documents_finalized", "Legal documents are no longer placeholders"),
    ("security_review_completed", "Public penetration / security review completed"),
)


def generate_checklist(config: HostedConfig, *, service=None) -> dict:
    items: list[ChecklistItem] = []

    # Config-derivable required items (can be resolved by software/configuration).
    config_problems = set(config.validate())
    https_ok = all(
        urlparse(u).scheme == "https"
        for u in (config.urls.public_site, config.urls.portal, config.urls.control_plane)
    )
    items.append(ChecklistItem(
        "public_urls_https", https_ok, True,
        "All public URLs use HTTPS" if https_ok else "Public URLs are not all HTTPS"))
    items.append(ChecklistItem(
        "secure_cookies", config.sessions.secure, True,
        "Session cookies are Secure"))
    items.append(ChecklistItem(
        "central_db_not_sqlite", not config.is_sqlite, True,
        "Central database is not SQLite"))
    items.append(ChecklistItem(
        "session_signing_key_present",
        bool(config.session_signing_key) and "session_signing_key_missing_or_weak"
        not in config_problems, True, "A strong session signing key is configured"))
    items.append(ChecklistItem(
        "email_provider_not_development", config.email.provider != "development", True,
        "A real email provider is configured"))
    items.append(ChecklistItem(
        "release_signing_key_present", bool(config.release_signing_key_b64), True,
        "A release-signing key is configured"))
    items.append(ChecklistItem(
        "bootstrap_disabled", not config.bootstrap_enabled, True,
        "Bootstrap is disabled after first admin"))

    # First platform admin bootstrapped (queryable if a service is supplied).
    admin_done = bool(service.is_bootstrapped()) if service is not None else False
    items.append(ChecklistItem(
        "platform_admin_bootstrapped", admin_done, True,
        "First platform admin created"))

    # Infrastructure/legal/security items: classified not_executed until real verification.
    for key, detail in _NOT_EXECUTED:
        items.append(ChecklistItem(key, resolved=False, required=True, detail=detail))

    unresolved_required = [i for i in items if i.required and not i.resolved]
    return {
        "production_ready": not unresolved_required,
        "environment": config.environment,
        "unresolved_required": [i.key for i in unresolved_required],
        "not_executed": [k for k, _ in _NOT_EXECUTED],
        "items": [
            {"key": i.key, "resolved": i.resolved, "required": i.required, "detail": i.detail}
            for i in items
        ],
    }
