"""The hosted control-plane service (Stage 14F).

A single cohesive service composed from domain mixins that share one engine + session model, an
injectable clock (no uncontrolled sleeps), a blob store and an email provider. Every query is
server-scoped by the authenticated principal — a tenant id is NEVER trusted from client data.
Audit records carry actor/tenant/action/bounded-target only; never credentials or payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from services.control_plane.config import HostedConfig
from services.control_plane.db import create_hosted_engine, init_database, make_session_factory
from services.control_plane.errors import ForbiddenError, RateLimitError
from services.control_plane.models import (
    HostedAuditRecord,
    HostedBootstrapState,
    HostedMembership,
    HostedPlatformGrant,
    HostedRateLimitState,
    HostedRole,
    HostedUser,
)
from services.control_plane.roles import ALL_ROLES, role_permissions
from services.control_plane.service_accounts import AccountsMixin
from services.control_plane.service_fleet import FleetMixin
from services.control_plane.service_intake import IntakeMixin
from services.control_plane.service_releases import ReleasesMixin
from services.control_plane.service_research import ResearchMixin


@dataclass
class Principal:
    """The authenticated hosted user + the tenants/roles authorization is computed from."""

    user_id: str
    email: str
    is_platform_admin: bool
    memberships: dict[str, str] = field(default_factory=dict)  # tenant_id -> role
    platform_roles: set[str] = field(default_factory=set)  # release_manager / support_operator

    def tenant_ids(self) -> list[str]:
        return sorted(self.memberships)

    def role_in(self, tenant_id: str) -> str | None:
        return self.memberships.get(tenant_id)


class ControlPlaneService(AccountsMixin, FleetMixin, ReleasesMixin, IntakeMixin, ResearchMixin):
    def __init__(
        self,
        config: HostedConfig,
        *,
        engine: Engine | None = None,
        clock: Callable[[], datetime] | None = None,
        blob_store=None,
        email_provider=None,
        turnstile_verifier=None,
    ) -> None:
        self.config = config
        #: Injected in tests so Cloudflare is never called; None => the real stdlib verifier.
        self._turnstile_verifier = turnstile_verifier
        self.engine = engine or create_hosted_engine(config)
        init_database(self.engine)
        self._session_factory = make_session_factory(self.engine)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._seed_roles()
        # Lazily-built dependencies (injected in tests / the app factory).
        if blob_store is None:
            from services.control_plane.blobstore import build_blob_store

            blob_store = build_blob_store(config)
        self.blob_store = blob_store
        if email_provider is None:
            from services.control_plane.email import build_email_provider

            email_provider = build_email_provider(config)
        self.email = email_provider

    # -- infrastructure ----------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def _session(self) -> Session:
        return self._session_factory()

    def _seed_roles(self) -> None:
        with self._session() as s:
            for role in ALL_ROLES:
                if s.get(HostedRole, role) is None:
                    s.add(HostedRole(name=role, description=role,
                                     permissions_json=sorted(role_permissions(role))))
            s.commit()

    # -- principal ---------------------------------------------------------------------------

    def _principal(self, s: Session, user: HostedUser) -> Principal:
        rows = s.execute(
            select(HostedMembership).where(HostedMembership.user_id == user.id)
        ).scalars().all()
        grants = s.execute(
            select(HostedPlatformGrant).where(HostedPlatformGrant.user_id == user.id)
        ).scalars().all()
        return Principal(
            user_id=user.id, email=user.email, is_platform_admin=user.is_platform_admin,
            memberships={m.tenant_id: m.role for m in rows},
            platform_roles={g.role for g in grants},
        )

    def require_platform_admin(self, principal: Principal) -> None:
        if not principal.is_platform_admin:
            raise ForbiddenError("platform_admin_required")

    def require_platform_permission(self, principal: Principal, permission: str) -> None:
        """Authorize a platform-level action: platform admins always pass; otherwise the principal
        must hold a platform role (release_manager / support_operator) granting the permission."""
        if principal.is_platform_admin:
            return
        from services.control_plane.roles import role_has_permission

        if any(role_has_permission(role, permission) for role in principal.platform_roles):
            return
        raise ForbiddenError("platform_permission_denied")

    def grant_platform_role(self, principal: Principal, user_id: str, role: str) -> dict:
        from services.control_plane.roles import RELEASE_MANAGER, SUPPORT_OPERATOR

        self.require_platform_admin(principal)
        if role not in (RELEASE_MANAGER, SUPPORT_OPERATOR):
            raise ForbiddenError("invalid_platform_role")
        with self._session() as s:
            exists = s.execute(
                select(HostedPlatformGrant).where(
                    HostedPlatformGrant.user_id == user_id, HostedPlatformGrant.role == role
                )
            ).scalar_one_or_none()
            if exists is None:
                s.add(HostedPlatformGrant(user_id=user_id, role=role, created_at=self.now()))
                self._audit(s, action="platform.grant", actor_user_id=principal.user_id,
                            target=f"{user_id}:{role}")
            s.commit()
        return {"user_id": user_id, "role": role, "granted": True}

    def require_tenant_permission(
        self, principal: Principal, tenant_id: str, permission: str
    ) -> None:
        """Authorize a tenant-scoped action. Platform admins are allowed; otherwise the principal
        must hold a membership in *this* tenant whose role grants the permission."""
        if principal.is_platform_admin:
            return
        role = principal.role_in(tenant_id)
        if role is None:
            raise ForbiddenError("not_a_member")
        from services.control_plane.roles import role_has_permission

        if not role_has_permission(role, permission):
            raise ForbiddenError("permission_denied")

    # -- audit -------------------------------------------------------------------------------

    def _audit(
        self, s: Session, *, action: str, actor_user_id: str | None = None,
        actor_kind: str = "user", tenant_id: str | None = None, target: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        s.add(HostedAuditRecord(
            actor_user_id=actor_user_id, actor_kind=actor_kind, tenant_id=tenant_id,
            action=action, target=(target or "")[:120], metadata_json=metadata or {},
            created_at=self.now(),
        ))

    def list_audit(self, principal: Principal, limit: int = 200) -> list[dict]:
        self.require_platform_admin(principal)
        with self._session() as s:
            rows = s.execute(
                select(HostedAuditRecord).order_by(HostedAuditRecord.created_at.desc()).limit(limit)
            ).scalars().all()
            return [
                {
                    "id": r.id, "action": r.action, "actor_user_id": r.actor_user_id,
                    "actor_kind": r.actor_kind, "tenant_id": r.tenant_id, "target": r.target,
                    "metadata": r.metadata_json, "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    # -- rate limiting -----------------------------------------------------------------------

    def _check_rate(self, s: Session, bucket: str, key: str, limit_per_minute: int) -> None:
        window = int(self.now().timestamp() // 60)
        row = s.execute(
            select(HostedRateLimitState).where(
                HostedRateLimitState.bucket == bucket,
                HostedRateLimitState.key == key,
                HostedRateLimitState.window_start == window,
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(HostedRateLimitState(bucket=bucket, key=key, window_start=window, count=1))
            return
        if row.count >= limit_per_minute:
            raise RateLimitError(f"rate_limited:{bucket}")
        row.count += 1

    # -- bootstrap state ---------------------------------------------------------------------

    def _bootstrap_row(self, s: Session) -> HostedBootstrapState:
        row = s.get(HostedBootstrapState, "singleton")
        if row is None:
            row = HostedBootstrapState(id="singleton", completed=False)
            s.add(row)
            s.flush()
        return row

    def is_bootstrapped(self) -> bool:
        with self._session() as s:
            return self._bootstrap_row(s).completed

    # -- readiness + diagnostics -------------------------------------------------------------

    def readiness(self) -> dict:
        from services.control_plane.migrations import validate_schema

        validation = validate_schema(self.engine)
        problems = self.config.validate()
        status = "ready" if validation.ok and not problems else "degraded"
        return {
            "status": status,
            "schema_ok": validation.ok,
            "pending_migrations": validation.pending_migrations,
            "config_problems": sorted(set(problems)),
            "environment": self.config.environment,
        }

    def diagnostics(self) -> dict:
        with self._session() as s:
            counts = {
                "tenants": _count(s, "hosted_tenants"),
                "users": _count(s, "hosted_users"),
                "nodes": _count(s, "hosted_nodes"),
                "invitations": _count(s, "hosted_invitations"),
                "releases": _count(s, "hosted_releases"),
                "support_cases": _count(s, "hosted_support_cases"),
            }
        return {
            "version": _version(),
            "environment": self.config.environment,
            "database_dialect": self.engine.dialect.name,
            "bootstrap_enabled": self.config.bootstrap_enabled,
            "bootstrapped": self.is_bootstrapped(),
            "email_provider": self.config.email.provider,
            "storage_backend": self.config.object_storage.backend,
            "counts": counts,
        }

    def email_test(self, *, to: str, subject: str = "Aithernet email readiness test") -> dict:
        """Send ONE bounded test email to ``to`` through the CONFIGURED provider and return a
        secret-free result. Classifies connection / authentication / sender / recipient / timeout
        failures distinctly, and records an audit entry. Never prints or returns credentials.

        With the development sink the message is captured in memory (NOT delivered) and reported as
        such — production verification needs ``AITHERNET_HOSTED_EMAIL=smtp`` with a real SMTP URL.
        """
        import smtplib
        import socket
        import ssl

        from services.control_plane.email import EmailMessage

        provider = self.config.email.provider
        body = ("This is an Aithernet email readiness test.\n\n"
                "If you received this, outbound email from the hosted control plane works.")
        msg = EmailMessage(to=to, subject=subject, body=body, kind="email_test")
        outcome, detail = "sent", None
        try:
            self.email.send(msg)
        except smtplib.SMTPAuthenticationError:
            outcome, detail = "auth_failed", "SMTP authentication was rejected"
        except smtplib.SMTPSenderRefused:
            outcome, detail = "sender_refused", "the From address was rejected by the server"
        except smtplib.SMTPRecipientsRefused:
            outcome, detail = "recipient_refused", "the destination address was rejected"
        except TimeoutError:
            outcome, detail = "timeout", "the SMTP operation timed out"
        except ssl.SSLError:
            outcome, detail = "tls_failed", "the STARTTLS/SSL handshake failed"
        except smtplib.SMTPNotSupportedError:
            outcome, detail = "starttls_unsupported", "the server did not offer STARTTLS"
        except (smtplib.SMTPConnectError, ConnectionError, socket.gaierror):
            outcome, detail = "connect_failed", "could not connect/resolve the SMTP server"
        except smtplib.SMTPException as exc:
            outcome, detail = "failed", type(exc).__name__
        except OSError:
            outcome, detail = "connect_failed", "network error reaching the SMTP server"
        ok = outcome == "sent"
        with self._session() as s:
            self._audit(s, action="email.test", actor_kind="system", target=to[:120],
                        metadata={"provider": provider, "outcome": outcome})
            s.commit()
        return {
            "ok": ok,
            "provider": provider,
            "to": to,
            "kind": "email_test",
            "outcome": outcome,
            "detail": detail,
            "from_address": self.config.email.from_address,
            "reply_to": self.config.email.reply_to or None,
            "note": ("development sink (NON-production): captured in memory, NOT delivered"
                     if provider == "development" else "delivery attempted via SMTP"),
        }

    def drive_status(self, principal: Principal) -> dict:
        """Bounded Google Drive archive status for the admin portal — NEVER exposes OAuth tokens,
        credential paths, encryption keys, or folder contents. Reports only presence/state."""
        self.require_platform_admin(principal)
        import os

        creds_ref = os.environ.get("AITHERNET_DRIVE_CREDENTIALS_REF")
        folder = os.environ.get("AITHERNET_DRIVE_FOLDER_ID")
        # A *reference* being set is non-secret; the actual credential value is never read here.
        credential_present = bool(creds_ref and os.environ.get(creds_ref))
        return {
            "configured": bool(folder),
            "enabled": bool(folder and credential_present),
            "credential_present": credential_present,
            "last_successful_archive_at": None,
            "failure_category": None,
            "pending_archive_count": 0,
            "note": "Google Drive archiving is not active in this deployment profile."
            if not folder else None,
        }

    def expiry(self, seconds: int) -> datetime:
        return self.now() + timedelta(seconds=seconds)


def _count(s: Session, table: str) -> int:
    from sqlalchemy import text

    return int(s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def _version() -> str:
    from services.control_plane import __version__

    return __version__
