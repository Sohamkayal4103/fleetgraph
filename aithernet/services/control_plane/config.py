"""Hosted control-plane configuration (Stage 14F §40).

Loaded from the environment for the standalone process. Secrets are NEVER stored inline — only
the env-var *name* (a ``*_ref``) is configured and the value is read at use time. Production mode
fails closed on placeholder secrets, insecure cookies, wildcard origins, loopback public URLs,
missing release-signing keys, a SQLite central database and the development email sink.

No real domain, DNS, TLS, SMTP, object store or PostgreSQL is required to construct or validate a
*development* configuration; example documentation uses ``*.example.invalid`` hostnames.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

#: Marker values that must never survive into a production deployment.
PLACEHOLDER_TOKENS = ("change-me", "changeme", "placeholder", "example", "REPLACE", "<", "TODO")


class HostedConfigError(ValueError):
    """A hosted configuration is invalid (sanitized — names only, never secret values)."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _ref(ref_name: str | None) -> str | None:
    """Resolve a secret *reference*: the configured value names an env var holding the secret."""
    if not ref_name:
        return None
    return os.environ.get(ref_name) or None


@dataclass
class HostedUrls:
    # `portal` is the canonical CUSTOMER origin (https://www.aithernet.online in production): the
    # public site, /login, /app and the same-origin /api all live here. `admin` is the SEPARATE,
    # Cloudflare-Access-protected administrator origin (https://admin.aithernet.online) — customer
    # links never point at it. `public_site` == `portal` in production (one canonical origin).
    public_site: str = "http://127.0.0.1:8101"
    portal: str = "http://127.0.0.1:8102"
    admin: str = "http://127.0.0.1:8102"
    control_plane: str = "http://127.0.0.1:8100"
    ingestion: str = "http://127.0.0.1:8090"
    downloads: str = "http://127.0.0.1:8100"
    support: str = "http://127.0.0.1:8102"


@dataclass
class SessionConfig:
    signing_key_ref: str = "AITHERNET_HOSTED_SESSION_KEY"
    cookie_name: str = "aithernet_hosted_session"
    csrf_cookie_name: str = "aithernet_hosted_csrf"
    secure: bool = False
    same_site: str = "lax"
    ttl_seconds: int = 60 * 60 * 12


@dataclass
class EmailConfig:
    provider: str = "development"  # development | smtp
    smtp_url_ref: str = "AITHERNET_HOSTED_SMTP_URL"
    from_address: str = "aithernet@app.example.invalid"
    reply_to: str = ""
    timeout_seconds: int = 30


@dataclass
class EnrollmentConfig:
    code_ttl_seconds: int = 60 * 30
    maximum_attempts: int = 5
    challenge_ttl_seconds: int = 60 * 5


@dataclass
class HeartbeatConfig:
    interval_seconds: int = 60
    recent_seconds: int = 180
    stale_seconds: int = 60 * 15
    offline_seconds: int = 60 * 60


@dataclass
class ReleaseConfig:
    channel: str = "early-access"
    signing_key_ref: str = "AITHERNET_RELEASE_SIGNING_KEY"
    public_key_ref: str = "AITHERNET_RELEASE_PUBLIC_KEY"
    signing_key_id: str = "rk1"
    #: Active signing-key id, overridable from the environment for traceable key rotation.
    #: Stored in release metadata; the hosted manifest's ``signing_key_id`` reflects the key
    #: actually selected. Unknown/revoked ids fail closed (see ``signing_keys`` below).
    storage_backend: str = "local"  # local | s3
    #: Non-secret registry of selectable signing-key ids -> {ref, public_key_ref, status}. The
    #: PRIVATE seed is always read from the env var named by ``ref`` (never stored here). ``status``
    #: is "active" or "revoked"; a revoked or unknown id is rejected at sign time. Rotation history
    #: is the ordered set of ids that have ever signed a release (visible via the release records).
    signing_keys: dict = field(default_factory=dict)
    #: Bounded maximum for a single authenticated release-artifact upload. The conservative default
    #: is sized for the small artifacts (the ~16 MiB ``.deb`` plus wheel/sdist/manifest/signature);
    #: it is raised via ``AITHERNET_RELEASE_MAX_ARTIFACT_BYTES`` in deployments that also ship the
    #: CatGPT Gateway runtime image (~650 MiB). The reverse proxy grants this larger body limit ONLY
    #: to the release-artifact route; every other API route keeps its conservative few-MB limit. The
    #: control plane re-enforces this bound server-side (defence in depth), so it holds even if a
    #: proxy is misconfigured/bypassed.
    max_artifact_bytes: int = 64 * 1024 * 1024


@dataclass
class ObjectStorageConfig:
    backend: str = "local"  # local | s3
    local_root: str = "./hosted-blobs"
    s3_endpoint_ref: str = "AITHERNET_HOSTED_S3_ENDPOINT"
    s3_bucket: str = "aithernet-hosted"
    access_key_ref: str = "AITHERNET_HOSTED_S3_ACCESS_KEY"
    secret_key_ref: str = "AITHERNET_HOSTED_S3_SECRET_KEY"


@dataclass
class RateLimits:
    login_per_minute: int = 10
    invitation_per_minute: int = 20
    enrollment_per_minute: int = 20
    downloads_per_minute: int = 120
    early_access_per_minute: int = 5
    mailing_per_minute: int = 5


@dataclass
class GoogleOAuthConfig:
    """Server-side Google OAuth for the OWNER Drive archive connection (admin/owner only).

    The client id + redirect URI are NOT secrets (the client id is embedded in the browser-facing
    Google authorization URL, and the redirect URI is a public callback path). The client SECRET is
    read only at use time from the env var named by ``client_secret_ref`` — never stored inline,
    never logged, never returned to any browser. Clients never receive any of these. When the three
    required values are absent the admin portal shows a clear "not configured" message with
    non-secret operator guidance; nothing fails with a stack trace.

    Scope: least-privilege ``drive.file`` — the app can see/manage ONLY the files and folders it
    creates (the "Aithernet Research Archive" tree), never the owner's other Drive content."""

    client_id: str = ""
    client_secret_ref: str = "GOOGLE_OAUTH_CLIENT_SECRET"
    redirect_uri: str = ""
    scope: str = "https://www.googleapis.com/auth/drive.file"
    archive_root_name: str = "Aithernet Research Archive"
    auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"


@dataclass
class IntakeConfig:
    """Public intake (early-access requests + mailing list) + Cloudflare Turnstile.

    ``auto_approve`` stays FALSE in templates/tests; when true, a valid early-access submission
    issues a system invitation for ``default_customer_role`` automatically. The browser never
    chooses a role/tenant. Turnstile is verified server-side via a secret REFERENCE (env-var name);
    when ``turnstile_required`` is true, submissions fail closed if verification does not pass."""

    auto_approve: bool = False
    default_customer_role: str = "tenant_operator"  # lowest role that can enroll + use the product
    turnstile_required: bool = False
    turnstile_secret_ref: str = "AITHERNET_TURNSTILE_SECRET"
    turnstile_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@dataclass
class HostedConfig:
    environment: str = "development"  # development | production
    host: str = "127.0.0.1"
    port: int = 8100
    database_url_ref: str = "AITHERNET_HOSTED_DATABASE_URL"
    database_url: str = "sqlite:///./hosted-control-plane.db"
    pool_size: int = 10
    max_overflow: int = 20
    urls: HostedUrls = field(default_factory=HostedUrls)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    enrollment: EnrollmentConfig = field(default_factory=EnrollmentConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    releases: ReleaseConfig = field(default_factory=ReleaseConfig)
    object_storage: ObjectStorageConfig = field(default_factory=ObjectStorageConfig)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    google_oauth: GoogleOAuthConfig = field(default_factory=GoogleOAuthConfig)
    bootstrap_enabled: bool = True
    ingestion_admin_token_ref: str = "AITHERNET_HOSTED_INGESTION_ADMIN_TOKEN"

    # -- resolved secrets (never persisted; read from referenced env vars) -------------------

    @property
    def session_signing_key(self) -> str | None:
        return _ref(self.sessions.signing_key_ref)

    @property
    def release_signing_key_b64(self) -> str | None:
        return _ref(self.releases.signing_key_ref)

    @property
    def release_public_key_b64(self) -> str | None:
        return _ref(self.releases.public_key_ref)

    @property
    def smtp_url(self) -> str | None:
        return _ref(self.email.smtp_url_ref)

    @property
    def google_oauth_client_secret(self) -> str | None:
        """The owner Google OAuth client secret, read at use time from its referenced env var.

        Never stored inline, never logged, never returned to any browser."""
        return _ref(self.google_oauth.client_secret_ref)

    @property
    def google_oauth_configured(self) -> bool:
        """True when the three required server-side values are present (client id + secret +
        redirect URI). Used to gate the admin "Connect Google Drive" flow and to show a clear,
        non-secret "not configured" message otherwise."""
        return bool(
            self.google_oauth.client_id
            and self.google_oauth_client_secret
            and self.google_oauth.redirect_uri
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    # -- construction ------------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> HostedConfig:
        cfg = cls()
        cfg.environment = _env("AITHERNET_HOSTED_ENV", "development") or "development"
        cfg.host = _env("AITHERNET_HOSTED_HOST", cfg.host) or cfg.host
        cfg.port = int(_env("AITHERNET_HOSTED_PORT", str(cfg.port)) or cfg.port)
        cfg.database_url = (
            _ref(cfg.database_url_ref) or _env("AITHERNET_HOSTED_DATABASE_URL", cfg.database_url)
            or cfg.database_url
        )
        cfg.bootstrap_enabled = (_env("AITHERNET_HOSTED_BOOTSTRAP", "1") or "1") in ("1", "true")
        cfg.object_storage.backend = _env(
            "AITHERNET_HOSTED_STORAGE_BACKEND", cfg.object_storage.backend
        ) or cfg.object_storage.backend
        cfg.object_storage.local_root = _env(
            "AITHERNET_HOSTED_BLOB_ROOT", cfg.object_storage.local_root
        ) or cfg.object_storage.local_root
        cfg.releases.storage_backend = _env(
            "AITHERNET_RELEASE_STORAGE", cfg.releases.storage_backend
        ) or cfg.releases.storage_backend
        # Env-overridable upload bound. Raised in production to admit the shipped CatGPT Gateway
        # runtime image (~650 MiB, a signed release artifact); the conservative default still
        # applies everywhere it is not set.
        cfg.releases.max_artifact_bytes = int(
            _env("AITHERNET_RELEASE_MAX_ARTIFACT_BYTES", str(cfg.releases.max_artifact_bytes))
            or cfg.releases.max_artifact_bytes
        )
        # Traceable, configurable signing-key id (non-secret). The PRIVATE seed is still read only
        # from the env var named by signing_key_ref. An optional JSON registry maps selectable ids
        # to {ref, public_key_ref, status}; unknown/revoked ids fail closed at sign time.
        cfg.releases.signing_key_id = _env(
            "AITHERNET_RELEASE_SIGNING_KEY_ID", cfg.releases.signing_key_id
        ) or cfg.releases.signing_key_id
        registry_raw = _env("AITHERNET_RELEASE_SIGNING_KEYS")
        if registry_raw:
            import json
            try:
                parsed = json.loads(registry_raw)
                if isinstance(parsed, dict):
                    cfg.releases.signing_keys = parsed
            except ValueError:
                pass  # malformed registry is ignored; the default single-key registry applies
        if not cfg.releases.signing_keys:
            cfg.releases.signing_keys = {
                cfg.releases.signing_key_id: {
                    "ref": cfg.releases.signing_key_ref,
                    "public_key_ref": cfg.releases.public_key_ref,
                    "status": "active",
                }
            }
        cfg.email.provider = (_env("AITHERNET_HOSTED_EMAIL", cfg.email.provider)
                              or cfg.email.provider)
        cfg.email.from_address = (_env("AITHERNET_HOSTED_EMAIL_FROM", cfg.email.from_address)
                                  or cfg.email.from_address)
        cfg.email.reply_to = _env("AITHERNET_HOSTED_EMAIL_REPLY_TO", cfg.email.reply_to) or ""
        try:
            cfg.email.timeout_seconds = int(
                _env("AITHERNET_HOSTED_EMAIL_TIMEOUT", str(cfg.email.timeout_seconds)) or "30")
        except ValueError:
            pass  # keep the safe default on a malformed timeout
        cfg.sessions.secure = (_env("AITHERNET_HOSTED_COOKIE_SECURE", "0") or "0") in ("1", "true")
        # Owner Drive Google OAuth (server-side; admin/owner only). Client id + redirect URI are
        # non-secret; the client SECRET stays a REFERENCE (read from GOOGLE_OAUTH_CLIENT_SECRET at
        # use time). Absent values => the admin portal shows a clear "not configured" message.
        cfg.google_oauth.client_id = _env("GOOGLE_OAUTH_CLIENT_ID", "") or ""
        cfg.google_oauth.redirect_uri = _env("GOOGLE_OAUTH_REDIRECT_URI", "") or ""
        cfg.google_oauth.archive_root_name = _env(
            "GOOGLE_OAUTH_ARCHIVE_ROOT_NAME", cfg.google_oauth.archive_root_name
        ) or cfg.google_oauth.archive_root_name
        cfg.intake.auto_approve = (
            _env("AITHERNET_AUTO_APPROVE_EARLY_ACCESS", "0") or "0") in ("1", "true")
        cfg.intake.turnstile_required = (
            _env("AITHERNET_TURNSTILE_REQUIRED", "0") or "0") in ("1", "true")
        for key, attr in (
            ("AITHERNET_PUBLIC_SITE_BASE_URL", "public_site"),
            ("AITHERNET_PORTAL_BASE_URL", "portal"),
            ("AITHERNET_ADMIN_BASE_URL", "admin"),
            ("AITHERNET_CONTROL_PLANE_BASE_URL", "control_plane"),
            ("AITHERNET_INGESTION_BASE_URL", "ingestion"),
            ("AITHERNET_DOWNLOAD_BASE_URL", "downloads"),
            ("AITHERNET_SUPPORT_BASE_URL", "support"),
        ):
            value = _env(key)
            if value:
                setattr(cfg.urls, attr, value)
        if cfg.is_production:
            cfg.sessions.secure = True
        return cfg

    # -- validation --------------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of blocking problems. Empty means startup-valid for this environment."""
        problems: list[str] = []
        for url in (self.urls.public_site, self.urls.portal, self.urls.control_plane):
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                problems.append(f"invalid_url:{parsed.scheme or 'none'}")
        if self.is_production:
            problems.extend(self._production_problems())
        return problems

    def _production_problems(self) -> list[str]:
        problems: list[str] = []
        # Public URLs must be HTTPS and not loopback.
        for name, url in (
            ("public_site", self.urls.public_site),
            ("portal", self.urls.portal),
            ("control_plane", self.urls.control_plane),
        ):
            parsed = urlparse(url)
            if parsed.scheme != "https":
                problems.append(f"public_url_not_https:{name}")
            host = (parsed.hostname or "").lower()
            if host in ("127.0.0.1", "localhost", "::1") or host.endswith(".local"):
                problems.append(f"public_url_loopback:{name}")
        if not self.sessions.secure:
            problems.append("insecure_session_cookie")
        if self.is_sqlite:
            problems.append("sqlite_central_database")
        if self.email.provider == "development":
            problems.append("development_email_sink")
        key = self.session_signing_key
        if not key or self._is_placeholder(key) or len(key) < 32:
            problems.append("session_signing_key_missing_or_weak")
        if self.bootstrap_enabled:
            problems.append("bootstrap_still_enabled")
        return problems

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        low = value.lower()
        return any(token.lower() in low for token in PLACEHOLDER_TOKENS)

    def require_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise HostedConfigError("hosted_config_invalid:" + ",".join(sorted(set(problems))))
