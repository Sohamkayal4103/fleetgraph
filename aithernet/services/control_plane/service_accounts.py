"""Accounts domain (Stage 14F §8-§12): bootstrap, tenancy, auth, sessions, invitations, policies.

Mixed into :class:`ControlPlaneService`. Every method server-scopes by the authenticated
principal. Tokens are shown once and stored only as digests; passwords are stored only as scrypt
hashes; acceptance history is never silently rewritten by a policy update.
"""

from __future__ import annotations

from sqlalchemy import select

from services.control_plane import security
from services.control_plane.email import invitation_email, password_reset_email
from services.control_plane.errors import (
    AuthError,
    ControlPlaneError,
    ForbiddenError,
    NotFoundError,
)
from services.control_plane.models import (
    HostedInvitation,
    HostedMembership,
    HostedPasswordReset,
    HostedPolicyAcceptance,
    HostedPolicyDocument,
    HostedSession,
    HostedTenant,
    HostedUser,
)
from services.control_plane.roles import (
    CUSTOMER_ROLES,
    TENANT_ADMIN,
    USERS_INVITE,
)
from services.control_plane.timeutil import ensure_aware


class AccountsMixin:
    # -- bootstrap ---------------------------------------------------------------------------

    def bootstrap_admin(self, email: str, password: str, *, force: bool = False) -> dict:
        """Create the first platform admin. Idempotent-safe: refuses once bootstrap completes
        unless explicitly forced. Stores only a password hash; never echoes the password."""
        email = _norm_email(email)
        policy = security.password_policy_error(password)
        if policy:
            raise ControlPlaneError(policy)
        with self._session() as s:
            row = self._bootstrap_row(s)
            if row.completed and not force:
                raise ForbiddenError("bootstrap_already_completed")
            if not self.config.bootstrap_enabled and not force:
                raise ForbiddenError("bootstrap_disabled")
            existing = s.execute(
                select(HostedUser).where(HostedUser.email == email)
            ).scalar_one_or_none()
            if existing is not None:
                existing.is_platform_admin = True
                user = existing
            else:
                user = HostedUser(
                    email=email, password_hash=security.hash_password(password),
                    display_name=email.split("@")[0], is_platform_admin=True,
                    email_verified=True, created_at=self.now(),
                )
                s.add(user)
            row.completed = True
            row.completed_at = self.now()
            self._audit(s, action="bootstrap.admin", actor_kind="system", target=user.email)
            s.commit()
            return {"user_id": user.id, "email": user.email, "is_platform_admin": True}

    # -- tenancy -----------------------------------------------------------------------------

    def create_tenant(self, name: str, *, kind: str = "individual", tenant_id: str | None = None,
                      actor_user_id: str | None = None) -> dict:
        with self._session() as s:
            tid = tenant_id or _slug(name)
            if s.get(HostedTenant, tid) is not None:
                raise ControlPlaneError("tenant_exists")
            tenant = HostedTenant(id=tid, name=name, kind=kind, created_at=self.now())
            s.add(tenant)
            self._audit(s, action="tenant.create", actor_user_id=actor_user_id, tenant_id=tid,
                        target=name)
            s.commit()
            return _tenant_dict(tenant)

    def list_tenants(self, principal) -> list[dict]:
        with self._session() as s:
            if principal.is_platform_admin:
                rows = s.execute(select(HostedTenant)).scalars().all()
            else:
                ids = principal.tenant_ids()
                rows = s.execute(
                    select(HostedTenant).where(HostedTenant.id.in_(ids))
                ).scalars().all() if ids else []
            return [_tenant_dict(t) for t in rows]

    def add_membership(self, tenant_id: str, user_id: str, role: str) -> dict:
        with self._session() as s:
            existing = s.execute(
                select(HostedMembership).where(
                    HostedMembership.tenant_id == tenant_id, HostedMembership.user_id == user_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.role = role
                s.commit()
                return {"tenant_id": tenant_id, "user_id": user_id, "role": role}
            m = HostedMembership(tenant_id=tenant_id, user_id=user_id, role=role,
                                 created_at=self.now())
            s.add(m)
            s.commit()
            return {"tenant_id": tenant_id, "user_id": user_id, "role": role}

    def list_memberships(self, principal) -> list[dict]:
        with self._session() as s:
            rows = s.execute(
                select(HostedMembership).where(HostedMembership.user_id == principal.user_id)
            ).scalars().all()
            return [
                {"tenant_id": m.tenant_id, "role": m.role,
                 "tenant_name": _tenant_name(s, m.tenant_id)}
                for m in rows
            ]

    def list_users(self, principal) -> list[dict]:
        """Bounded user list for the admin portal (platform admin only). No password material."""
        self.require_platform_admin(principal)
        with self._session() as s:
            users = s.execute(select(HostedUser).order_by(HostedUser.created_at)).scalars().all()
            out = []
            for u in users:
                memberships = s.execute(
                    select(HostedMembership).where(HostedMembership.user_id == u.id)
                ).scalars().all()
                out.append({
                    "user_id": u.id, "email": u.email, "display_name": u.display_name,
                    "is_platform_admin": u.is_platform_admin, "email_verified": u.email_verified,
                    "disabled": u.disabled, "created_at": u.created_at.isoformat(),
                    "memberships": [
                        {"tenant_id": m.tenant_id, "role": m.role} for m in memberships
                    ],
                })
            return out

    # -- authentication + sessions -----------------------------------------------------------

    def authenticate(self, email: str, password: str, *, rate_key: str = "global") -> dict:
        email = _norm_email(email)
        with self._session() as s:
            self._check_rate(s, "login", rate_key, self.config.rate_limits.login_per_minute)
            user = s.execute(
                select(HostedUser).where(HostedUser.email == email)
            ).scalar_one_or_none()
            now = self.now()
            if user is None:
                security.hash_password(password)  # constant-ish work to blunt user enumeration
                s.commit()
                raise AuthError("invalid_credentials")
            if user.disabled:
                s.commit()
                raise AuthError("account_disabled")
            if user.locked_until and ensure_aware(user.locked_until) > now:
                s.commit()
                raise AuthError("account_locked")
            if not security.verify_password(password, user.password_hash):
                user.failed_logins += 1
                if user.failed_logins >= 10:
                    user.locked_until = self.expiry(900)
                self._audit(s, action="auth.login.failed", actor_user_id=user.id,
                            target=str(user.failed_logins))
                s.commit()
                raise AuthError("invalid_credentials")
            user.failed_logins = 0
            user.locked_until = None
            self._audit(s, action="auth.login.ok", actor_user_id=user.id)
            s.commit()
            return {"user_id": user.id, "email": user.email}

    def create_session(self, user_id: str, *, user_agent: str | None = None) -> dict:
        """Create a fresh session (rotation after authentication) and return the raw cookie value +
        CSRF token. Only a digest of the cookie value is persisted."""
        with self._session() as s:
            user = s.get(HostedUser, user_id)
            if user is None:
                raise NotFoundError("user_not_found")
            csrf_secret = security.generate_token(16)
            session = HostedSession(
                user_id=user_id, token_digest="pending", csrf_secret=csrf_secret,
                user_agent=(user_agent or "")[:200] or None, expires_at=self.expiry(
                    self.config.sessions.ttl_seconds),
                created_at=self.now(),
            )
            s.add(session)
            s.flush()
            raw = security.sign_session_token(
                self._signing_key(), session.id, security.generate_token(24))
            session.token_digest = security.token_digest(raw)
            csrf_token = security.make_csrf_token(self._signing_key(), csrf_secret)
            s.commit()
            return {
                "session_token": raw, "csrf_token": csrf_token,
                "expires_at": session.expires_at.isoformat(), "session_id": session.id,
            }

    def validate_session(self, raw_token: str | None):
        if not raw_token:
            return None
        digest = security.token_digest(raw_token)
        with self._session() as s:
            session = s.execute(
                select(HostedSession).where(HostedSession.token_digest == digest)
            ).scalar_one_or_none()
            if session is None or session.revoked or ensure_aware(session.expires_at) <= self.now():
                return None
            user = s.get(HostedUser, session.user_id)
            if user is None or user.disabled:
                return None
            principal = self._principal(s, user)
            return principal, session.id, session.csrf_secret

    def public_session_status(self, raw_token: str | None) -> dict:
        """Public-safe account status for the marketing site. No secrets, no mutation.

        Returns ``{authenticated, account?: {display_name, email}, portal_url}``. Anonymous or
        invalid/expired/revoked sessions return ``{authenticated: False, portal_url}`` (HTTP 200).
        """
        portal_url = (self.config.urls.portal or "").rstrip("/")
        result = self.validate_session(raw_token)
        if result is None:
            return {"authenticated": False, "portal_url": portal_url}
        principal, _, _ = result
        with self._session() as s:
            user = s.get(HostedUser, principal.user_id)
            display_name = (user.display_name if user else None) or None
        return {
            "authenticated": True,
            "account": {"display_name": display_name, "email": principal.email},
            "portal_url": portal_url,
        }

    def revoke_session(self, session_id: str, *, actor_user_id: str | None = None) -> dict:
        with self._session() as s:
            session = s.get(HostedSession, session_id)
            if session is None:
                raise NotFoundError("session_not_found")
            if actor_user_id and session.user_id != actor_user_id:
                raise ForbiddenError("not_session_owner")
            session.revoked = True
            self._audit(s, action="auth.session.revoke", actor_user_id=session.user_id)
            s.commit()
            return {"session_id": session_id, "revoked": True}

    def list_sessions(self, principal) -> list[dict]:
        with self._session() as s:
            rows = s.execute(
                select(HostedSession).where(HostedSession.user_id == principal.user_id)
            ).scalars().all()
            return [
                {"session_id": x.id, "revoked": x.revoked, "user_agent": x.user_agent,
                 "expires_at": x.expires_at.isoformat(), "created_at": x.created_at.isoformat()}
                for x in rows
            ]

    def csrf_token_for(self, csrf_secret: str) -> str:
        return security.make_csrf_token(self._signing_key(), csrf_secret)

    def verify_csrf(self, csrf_secret: str, presented: str | None) -> bool:
        return security.verify_csrf_token(self._signing_key(), csrf_secret, presented or "")

    # -- password reset ----------------------------------------------------------------------

    def request_password_reset(self, email: str) -> dict:
        email = _norm_email(email)
        with self._session() as s:
            user = s.execute(
                select(HostedUser).where(HostedUser.email == email)
            ).scalar_one_or_none()
            if user is None:
                return {"sent": True}  # do not leak whether the account exists
            token = security.generate_token(24)
            s.add(HostedPasswordReset(
                user_id=user.id, token_digest=security.token_digest(token),
                expires_at=self.expiry(3600), created_at=self.now(),
            ))
            self._audit(s, action="auth.reset.request", actor_user_id=user.id)
            s.commit()
        reset_url = f"{self.config.urls.portal}/reset?token={token}"
        self.email.send(password_reset_email(to=email, reset_url=reset_url, token=token))
        return {"sent": True}

    def confirm_password_reset(self, token: str, new_password: str) -> dict:
        policy = security.password_policy_error(new_password)
        if policy:
            raise ControlPlaneError(policy)
        digest = security.token_digest(token)
        with self._session() as s:
            reset = s.execute(
                select(HostedPasswordReset).where(HostedPasswordReset.token_digest == digest)
            ).scalar_one_or_none()
            if reset is None or reset.used or ensure_aware(reset.expires_at) <= self.now():
                raise ControlPlaneError("reset_token_invalid")
            user = s.get(HostedUser, reset.user_id)
            if user is None:
                raise NotFoundError("user_not_found")
            user.password_hash = security.hash_password(new_password)
            reset.used = True
            # Revoke all existing sessions after a password change.
            for session in s.execute(
                select(HostedSession).where(HostedSession.user_id == user.id)
            ).scalars().all():
                session.revoked = True
            self._audit(s, action="auth.reset.confirm", actor_user_id=user.id)
            s.commit()
            return {"reset": True}

    # -- invitations -------------------------------------------------------------------------

    def create_invitation(
        self, principal, *, email: str, role: str = TENANT_ADMIN,
        tenant_id: str | None = None, proposed_tenant_name: str | None = None,
        policy_bundle_id: str | None = None, note: str | None = None,
        ttl_seconds: int = 7 * 24 * 3600,
    ) -> dict:
        email = _norm_email(email)
        if role not in CUSTOMER_ROLES:
            raise ControlPlaneError("invalid_invitation_role")
        with self._session() as s:
            self._check_rate(s, "invitation", principal.user_id,
                             self.config.rate_limits.invitation_per_minute)
            if tenant_id is not None:
                self.require_tenant_permission(principal, tenant_id, USERS_INVITE)
            elif not principal.is_platform_admin:
                raise ForbiddenError("new_tenant_requires_platform_admin")
            token = security.generate_token(24)
            inv = HostedInvitation(
                tenant_id=tenant_id, proposed_tenant_name=proposed_tenant_name, email=email,
                role=role, token_digest=security.token_digest(token),
                inviter_user_id=principal.user_id, policy_bundle_id=policy_bundle_id,
                note=(note or None), expires_at=self.expiry(ttl_seconds), created_at=self.now(),
            )
            s.add(inv)
            self._audit(s, action="invitation.create", actor_user_id=principal.user_id,
                        tenant_id=tenant_id, target=email)
            s.flush()
            inv_id = inv.id
            s.commit()
        # Canonical browser-facing invitation link: the configured PUBLIC portal origin + the SPA
        # hash route, with the one-time token in the query. The SPA reads the token automatically.
        accept_url = f"{self.config.urls.portal.rstrip('/')}/app/accept-invite?token={token}"
        self.email.send(invitation_email(
            to=email, accept_url=accept_url, token=token,
            tenant_name=proposed_tenant_name or tenant_id or "Aithernet"))
        # The raw token is returned ONCE to the inviter for out-of-band delivery; never stored.
        return {"invitation_id": inv_id, "email": email, "token": token,
                "expires_at": inv.expires_at.isoformat()}

    def resend_invitation(self, principal, *, invitation_id: str,
                          ttl_seconds: int = 7 * 24 * 3600) -> dict:
        """Admin: re-send an existing invitation to its RECORDED email.

        Secure-invitation policy: the one-time token is ROTATED (any previously-emailed link is
        invalidated) and the expiry is deliberately reset, so the customer always receives a fresh,
        truthful link. Operates in-place on the SAME invitation row (no duplicate invitation, no
        membership change). State is handled truthfully: accepted/revoked/consumed invitations are
        refused with a distinct error; pending OR expired invitations are reactivated and resent.
        The raw token is used only to build the email link — it is never returned, stored, or
        logged. Rate-limited to absorb repeated button clicks.
        """
        with self._session() as s:
            # Rate-limit BEFORE any work so repeated clicks can't fan out emails.
            self._check_rate(s, "invitation_resend", principal.user_id,
                             self.config.rate_limits.invitation_per_minute)
            inv = s.get(HostedInvitation, invitation_id)
            if inv is None:
                raise NotFoundError("invitation_not_found")
            # Authorization mirrors create_invitation: tenant USERS_INVITE, else platform admin.
            if inv.tenant_id is not None:
                self.require_tenant_permission(principal, inv.tenant_id, USERS_INVITE)
            elif not principal.is_platform_admin:
                raise ForbiddenError("new_tenant_requires_platform_admin")
            # Truthful terminal-state handling — never silently "resend" something unusable.
            if inv.status == "accepted" or inv.accepted_at is not None:
                raise ControlPlaneError("invitation_already_accepted")
            if inv.revoked_at is not None or inv.status == "revoked":
                raise ControlPlaneError("invitation_revoked")
            if inv.used_count >= inv.maximum_uses:
                raise ControlPlaneError("invitation_consumed")
            # Pending or expired -> rotate the token and reset the expiry (reactivate to pending).
            token = security.generate_token(24)
            inv.token_digest = security.token_digest(token)
            inv.expires_at = self.expiry(ttl_seconds)
            inv.status = "pending"
            email = inv.email
            tenant_label = inv.proposed_tenant_name or inv.tenant_id or "Aithernet"
            new_expiry = inv.expires_at.isoformat()
            self._audit(s, action="invitation.resend", actor_user_id=principal.user_id,
                        tenant_id=inv.tenant_id, target=email,
                        metadata={"invitation_id": invitation_id, "expires_at": new_expiry})
            s.commit()
        accept_url = f"{self.config.urls.portal.rstrip('/')}/app/accept-invite?token={token}"
        # SMTP submission happens here; the token never leaves this method except inside the link.
        self.email.send(invitation_email(
            to=email, accept_url=accept_url, token=token, tenant_name=tenant_label))
        return {"status": "resent", "invitation_id": invitation_id, "email": email,
                "expires_at": new_expiry}

    def validate_invitation(self, token: str) -> dict:
        """Public: validate a one-time token WITHOUT consuming it; return bounded onboarding
        details (invited email, tenant/role, expiry, state, required policy versions). Never
        exposes the token digest or any secret. Used by the public invitation page."""
        digest = security.token_digest(token)
        with self._session() as s:
            inv = s.execute(
                select(HostedInvitation).where(HostedInvitation.token_digest == digest)
            ).scalar_one_or_none()
            if inv is None:
                return {"valid": False, "status": "invalid"}
            if inv.revoked_at is not None or inv.status == "revoked":
                status = "revoked"
            elif ensure_aware(inv.expires_at) <= self.now():
                status = "expired"
            elif inv.used_count >= inv.maximum_uses or inv.status == "accepted":
                status = "consumed"
            else:
                status = "valid"
            required = [
                {"policy_id": p.id, "policy_type": p.policy_type, "version": p.version,
                 "title": p.title}
                for p in self._active_required_policies(s)
            ]
            # Whether this email already has an account decides the accept UI: an existing account
            # must SIGN IN with its existing password (never set a new one); a fresh email creates
            # one. We expose only the boolean — never the password hash or any secret.
            account_exists = s.execute(
                select(HostedUser.id).where(HostedUser.email == inv.email)
            ).first() is not None
            return {
                "valid": status == "valid", "status": status, "email": inv.email,
                "tenant_id": inv.tenant_id, "proposed_tenant_name": inv.proposed_tenant_name,
                "role": inv.role, "expires_at": inv.expires_at.isoformat(),
                "required_policies": required, "account_exists": account_exists,
            }

    def _active_required_policies(self, s):
        return s.execute(
            select(HostedPolicyDocument).where(
                HostedPolicyDocument.published.is_(True),
                HostedPolicyDocument.superseded_by.is_(None),
            )
        ).scalars().all()

    def accept_invitation(self, token: str, *, email: str, password: str,
                          display_name: str | None = None,
                          accept_required_policies: bool = False) -> dict:
        email = _norm_email(email)
        digest = security.token_digest(token)
        with self._session() as s:
            inv = s.execute(
                select(HostedInvitation).where(HostedInvitation.token_digest == digest)
            ).scalar_one_or_none()
            if inv is None:
                raise ControlPlaneError("invitation_invalid")
            if inv.email != email:
                raise ControlPlaneError("invitation_email_mismatch")
            existing_user = s.execute(
                select(HostedUser).where(HostedUser.email == email)
            ).scalar_one_or_none()
            # Idempotent retry: already accepted by this email -> return the existing result.
            if inv.status == "accepted" and existing_user is not None:
                tid = inv.tenant_id
                if tid and self._is_member(s, tid, existing_user.id):
                    return {"user_id": existing_user.id, "tenant_id": tid, "accepted": True,
                            "idempotent": True}
            if inv.revoked_at is not None or inv.status == "revoked":
                raise ControlPlaneError("invitation_revoked")
            if ensure_aware(inv.expires_at) <= self.now():
                raise ControlPlaneError("invitation_expired")
            if inv.used_count >= inv.maximum_uses:
                raise ControlPlaneError("invitation_consumed")
            # Create or reuse the user.
            if existing_user is None:
                policy = security.password_policy_error(password)
                if policy:
                    raise ControlPlaneError(policy)
                user = HostedUser(
                    email=email, password_hash=security.hash_password(password),
                    display_name=(display_name or email.split("@")[0]), email_verified=True,
                    created_at=self.now(),
                )
                s.add(user)
                s.flush()
            else:
                # Existing account: REQUIRE authentication with the existing password. The password
                # is NEVER reset by acceptance. Verify BEFORE any mutation so a wrong password
                # neither consumes the invitation nor adds membership/policy — the whole
                # transaction rolls back and the invitation stays usable.
                if not security.verify_password(password, existing_user.password_hash):
                    raise ControlPlaneError("invalid_credentials")
                user = existing_user
            # Resolve the tenant (existing or freshly created from the proposed name).
            if inv.tenant_id:
                tenant_id = inv.tenant_id
            else:
                name = inv.proposed_tenant_name or email.split("@")[0]
                tenant_id = _unique_slug(s, name)
                s.add(HostedTenant(id=tenant_id, name=name, kind="organization",
                                   created_at=self.now()))
                inv.tenant_id = tenant_id
            if not self._is_member(s, tenant_id, user.id):
                s.add(HostedMembership(tenant_id=tenant_id, user_id=user.id, role=inv.role,
                                       created_at=self.now()))
            # Affirmative policy acceptance recorded in the SAME transaction — if any of the
            # account/tenant/membership/policy/consume steps fail, the whole thing rolls back and
            # no partially-onboarded account remains.
            if accept_required_policies:
                for doc in self._active_required_policies(s):
                    s.add(HostedPolicyAcceptance(
                        user_id=user.id, tenant_id=tenant_id, policy_id=doc.id,
                        policy_type=doc.policy_type, version=doc.version,
                        document_digest=doc.document_digest, source="invitation",
                        required_scopes_json=list(doc.required_categories_json),
                        accepted_at=self.now(),
                    ))
            inv.used_count += 1
            inv.accepted_at = self.now()
            if inv.used_count >= inv.maximum_uses:
                inv.status = "accepted"
            self._audit(s, action="invitation.accept", actor_user_id=user.id, tenant_id=tenant_id)
            s.commit()
            return {"user_id": user.id, "tenant_id": tenant_id, "accepted": True}

    def revoke_invitation(self, principal, invitation_id: str) -> dict:
        with self._session() as s:
            inv = s.get(HostedInvitation, invitation_id)
            if inv is None:
                raise NotFoundError("invitation_not_found")
            if inv.tenant_id:
                self.require_tenant_permission(principal, inv.tenant_id, USERS_INVITE)
            else:
                self.require_platform_admin(principal)
            inv.status = "revoked"
            inv.revoked_at = self.now()
            self._audit(s, action="invitation.revoke", actor_user_id=principal.user_id,
                        tenant_id=inv.tenant_id, target=inv.email)
            s.commit()
            return {"invitation_id": invitation_id, "revoked": True}

    def list_invitations(self, principal, tenant_id: str | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(HostedInvitation)
            if principal.is_platform_admin and tenant_id is None:
                pass
            elif tenant_id is not None:
                self.require_tenant_permission(principal, tenant_id, USERS_INVITE)
                stmt = stmt.where(HostedInvitation.tenant_id == tenant_id)
            else:
                stmt = stmt.where(HostedInvitation.tenant_id.in_(principal.tenant_ids() or [""]))
            rows = s.execute(stmt.order_by(HostedInvitation.created_at.desc())).scalars().all()
            return [_invitation_dict(i) for i in rows]

    # -- policies ----------------------------------------------------------------------------

    def publish_policy(
        self, principal, *, policy_type: str, version: str, title: str, document_text: str,
        required_categories: list[str] | None = None, optional_categories: list[str] | None = None,
        tenant_scope: str | None = None, supersedes_version: str | None = None,
    ) -> dict:
        self.require_platform_admin(principal)
        import hashlib

        digest = "sha256:" + hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        with self._session() as s:
            dup = s.execute(
                select(HostedPolicyDocument).where(
                    HostedPolicyDocument.policy_type == policy_type,
                    HostedPolicyDocument.version == version,
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise ControlPlaneError("policy_version_exists")
            doc = HostedPolicyDocument(
                policy_type=policy_type, version=version, title=title, document_digest=digest,
                document_ref=document_text[:8000], effective_date=self.now(),
                required_categories_json=required_categories or [],
                optional_categories_json=optional_categories or [], tenant_scope=tenant_scope,
                published=True, published_at=self.now(), created_at=self.now(),
            )
            s.add(doc)
            s.flush()
            # A new version supersedes the prior one WITHOUT rewriting historical acceptances.
            if supersedes_version:
                prior = s.execute(
                    select(HostedPolicyDocument).where(
                        HostedPolicyDocument.policy_type == policy_type,
                        HostedPolicyDocument.version == supersedes_version,
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    prior.superseded_by = doc.id
            self._audit(s, action="policy.publish", actor_user_id=principal.user_id,
                        target=f"{policy_type}:{version}")
            pid = doc.id
            s.commit()
            return {"policy_id": pid, "policy_type": policy_type, "version": version,
                    "document_digest": digest}

    def list_active_policies(self, policy_type: str | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(HostedPolicyDocument).where(
                HostedPolicyDocument.published.is_(True),
                HostedPolicyDocument.superseded_by.is_(None),
            )
            if policy_type:
                stmt = stmt.where(HostedPolicyDocument.policy_type == policy_type)
            rows = s.execute(stmt).scalars().all()
            return [_policy_dict(p) for p in rows]

    def accept_policy(
        self, principal, *, policy_id: str, tenant_id: str | None = None,
        required_scopes: list[str] | None = None, optional_choices: dict | None = None,
        source: str = "portal",
    ) -> dict:
        with self._session() as s:
            doc = s.get(HostedPolicyDocument, policy_id)
            if doc is None or not doc.published:
                raise NotFoundError("policy_not_found")
            acc = HostedPolicyAcceptance(
                user_id=principal.user_id, tenant_id=tenant_id, policy_id=doc.id,
                policy_type=doc.policy_type, version=doc.version,
                document_digest=doc.document_digest, source=source,
                required_scopes_json=required_scopes or list(doc.required_categories_json),
                optional_choices_json=optional_choices or {}, accepted_at=self.now(),
            )
            s.add(acc)
            self._audit(s, action="policy.accept", actor_user_id=principal.user_id,
                        tenant_id=tenant_id, target=f"{doc.policy_type}:{doc.version}")
            s.commit()
            return {"acceptance_id": acc.id, "policy_id": doc.id, "version": doc.version}

    def acceptance_status(self, principal) -> dict:
        """Which active required policies the principal has accepted (at the current version)."""
        active = self.list_active_policies()
        with self._session() as s:
            accepted = {
                (a.policy_type, a.version)
                for a in s.execute(
                    select(HostedPolicyAcceptance).where(
                        HostedPolicyAcceptance.user_id == principal.user_id,
                        HostedPolicyAcceptance.withdrawn.is_(False),
                    )
                ).scalars().all()
            }
        outstanding = [
            p for p in active if (p["policy_type"], p["version"]) not in accepted
        ]
        return {
            "active": active,
            "accepted": [{"policy_type": t, "version": v} for t, v in sorted(accepted)],
            "outstanding": outstanding,
            "onboarding_complete": not outstanding,
        }

    def onboarding_complete(self, principal) -> bool:
        return self.acceptance_status(principal)["onboarding_complete"]

    # -- internal helpers --------------------------------------------------------------------

    def _signing_key(self) -> str:
        key = self.config.session_signing_key
        if key:
            return key
        if self.config.is_production:
            raise ControlPlaneError("session_signing_key_missing")
        # Development fallback derived from a stable per-process value (never used in production).
        return "dev-insecure-session-signing-key-0000000000000000"

    def _is_member(self, s, tenant_id: str, user_id: str) -> bool:
        return s.execute(
            select(HostedMembership).where(
                HostedMembership.tenant_id == tenant_id, HostedMembership.user_id == user_id
            )
        ).scalar_one_or_none() is not None


# -- module helpers ------------------------------------------------------------------------------


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (name or "tenant").lower()).strip("-")
    return base or "tenant"


def _unique_slug(s, name: str) -> str:
    base = _slug(name)
    candidate = base
    n = 1
    while s.get(HostedTenant, candidate) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _tenant_name(s, tenant_id: str) -> str:
    t = s.get(HostedTenant, tenant_id)
    return t.name if t else tenant_id


def _tenant_dict(t: HostedTenant) -> dict:
    return {"tenant_id": t.id, "name": t.name, "kind": t.kind, "status": t.status,
            "created_at": t.created_at.isoformat()}


def _invitation_dict(i: HostedInvitation) -> dict:
    return {
        "invitation_id": i.id, "tenant_id": i.tenant_id, "email": i.email, "role": i.role,
        "status": i.status, "used_count": i.used_count, "maximum_uses": i.maximum_uses,
        "expires_at": i.expires_at.isoformat(), "created_at": i.created_at.isoformat(),
    }


def _policy_dict(p: HostedPolicyDocument) -> dict:
    return {
        "policy_id": p.id, "policy_type": p.policy_type, "version": p.version, "title": p.title,
        "document_digest": p.document_digest, "required_categories": p.required_categories_json,
        "optional_categories": p.optional_categories_json,
        "effective_date": p.effective_date.isoformat() if p.effective_date else None,
    }
