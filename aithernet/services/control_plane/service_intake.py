"""Public intake: early-access requests + mailing list (Stage 14F productization).

Both endpoints are PUBLIC and anti-enumeration: callers always get the same bounded response
regardless of whether an email already has an account, invitation, membership or subscription.
The browser never chooses a role, tenant, expiry, redirect or template. Cloudflare Turnstile is
verified server-side via an injected verifier (tests never call Cloudflare); when configured as
required, submissions fail closed. Early-access auto-approval (off by default) issues an invitation
for the LOWEST customer role through the existing deterministic invitation machinery.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request

from sqlalchemy import func, select

from services.control_plane import roles, security
from services.control_plane.email import (
    admin_new_request_notification_email,
    early_access_acknowledgement_email,
    invitation_email,
    mailing_confirmation_email,
)
from services.control_plane.errors import ControlPlaneError, NotFoundError
from services.control_plane.models import (
    HostedEarlyAccessRequest,
    HostedInvitation,
    HostedMailingSubscriber,
    HostedUser,
)

#: A repeat early-access request within this window does not resend/reissue (anti-spam).
_EARLY_ACCESS_COOLDOWN_SECONDS = 10 * 60
_MAILING_CONFIRM_TTL_SECONDS = 3 * 24 * 3600
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: The single generic public response (never reveals account/invitation/subscription existence).
_GENERIC_OK = {"status": "ok",
               "message": "If your address is eligible, you'll receive an email shortly."}


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def _ip_hash(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(("aith-ip:" + ip).encode()).hexdigest()[:32]


def _default_turnstile_verify(*, secret: str, token: str, remote_ip: str | None, url: str) -> bool:
    """Server-side Cloudflare Turnstile siteverify (stdlib; never used in tests)."""
    data = urllib.parse.urlencode(
        {"secret": secret, "response": token, **({"remoteip": remote_ip} if remote_ip else {})}
    ).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:  # noqa: S310
            import json
            return bool(json.loads(resp.read()).get("success"))
    except Exception:  # noqa: BLE001 — any failure is a non-pass; caller fails closed if required
        return False


class IntakeMixin:
    """Early-access + mailing-list intake. Mixed into ``ControlPlaneService``."""

    # -- Turnstile -----------------------------------------------------------
    def _turnstile_ok(self, token: str | None, remote_ip: str | None) -> bool:
        import os

        cfg = self.config.intake
        # The secret is referenced by env-var NAME; the value lives only in the environment.
        secret = os.environ.get(cfg.turnstile_secret_ref) or None
        if not secret:
            # Not configured: permitted only when not strictly required.
            return not cfg.turnstile_required
        if not token:
            return False
        verifier = getattr(self, "_turnstile_verifier", None) or _default_turnstile_verify
        result = verifier(secret=secret, token=token, remote_ip=remote_ip,
                          url=cfg.turnstile_verify_url)
        # Accept either a bool (the default verifier) or Cloudflare's {"success": ...} shape.
        ok = result.get("success") if isinstance(result, dict) else result
        return bool(ok)

    # -- early access --------------------------------------------------------
    def submit_early_access(self, *, email: str, privacy_ack: bool, mailing_opt_in: bool = False,
                            turnstile_token: str | None = None, client_ip: str | None = None,
                            rate_key: str | None = None) -> dict:
        """Anti-enumeration early-access intake. Returns the SAME generic response always."""
        email = _norm_email(email)
        if not privacy_ack or not _EMAIL_RE.match(email) or len(email) > 254:
            # Still generic to the caller; nothing is created.
            return dict(_GENERIC_OK)
        with self._session() as s:
            # Rate limit by IP and by normalized email (separate buckets) — fail closed silently.
            try:
                self._check_rate(s, "early_access_ip", rate_key or client_ip or "global",
                                 self.config.rate_limits.early_access_per_minute)
                self._check_rate(s, "early_access_email", email,
                                 self.config.rate_limits.early_access_per_minute)
            except Exception:  # noqa: BLE001 — rate limited: generic response, no disclosure
                s.commit()
                return dict(_GENERIC_OK)
            if not self._turnstile_ok(turnstile_token, client_ip):
                self._audit(s, action="early_access.turnstile_failed", actor_kind="system")
                s.commit()
                return dict(_GENERIC_OK)

            status, invitation_id, accept = self._handle_early_access(s, email)
            s.add(HostedEarlyAccessRequest(email=email, status=status,
                                           mailing_opt_in=bool(mailing_opt_in),
                                           invitation_id=invitation_id,
                                           ip_hash=_ip_hash(client_ip), updated_at=self.now()))
            self._audit(s, action="early_access.request", actor_kind="system",
                        target=email[:120], metadata={"outcome": status})
            # Optional, SEPARATE mailing-list opt-in (double opt-in still required).
            mail = None
            if mailing_opt_in:
                mail = self._begin_mailing_subscription(s, email, source="early_access_form")
            s.commit()
        # Side-effecting emails happen AFTER commit (never inside the txn).
        if accept:
            self.email.send(invitation_email(to=email, accept_url=accept["url"],
                                             token=accept["token"], tenant_name=accept["tenant"]))
        elif status == "pending":
            # A genuinely new pending request: acknowledge the requester AND notify an admin.
            support = self.config.email.reply_to or self.config.email.from_address
            self.email.send(early_access_acknowledgement_email(to=email, support_email=support))
            admin_to = self.config.email.from_address
            # Admin notification points at the SEPARATE admin origin with a real path route.
            admin_url = f"{self.config.urls.admin.rstrip('/')}/admin/early-access"
            self.email.send(admin_new_request_notification_email(
                to=admin_to, requester_email=email, admin_url=admin_url))
        if mailing_opt_in and mail and mail.get("confirm_url"):
            self.email.send(mailing_confirmation_email(to=email, confirm_url=mail["confirm_url"]))
        return dict(_GENERIC_OK)

    def _handle_early_access(self, s, email: str):
        """Deterministic outcome for an email. Returns (status, invitation_id, accept)."""
        # Existing account: do not mint a new tenant/invite; they sign in (existing-password flow).
        user = s.execute(select(HostedUser.id).where(HostedUser.email == email)).first()
        if user is not None:
            return "existing_account_suppressed", None, None
        if not self.config.intake.auto_approve:
            # Reviewable queue item. A recent pending request from this email should not re-send the
            # acknowledgement (anti-spam) — the new row is recorded as a suppressed duplicate.
            recent = s.execute(
                select(HostedEarlyAccessRequest)
                .where(HostedEarlyAccessRequest.email == email,
                       HostedEarlyAccessRequest.status == "pending")
                .order_by(HostedEarlyAccessRequest.created_at.desc())
            ).scalars().first()
            if recent is not None:
                age = (self.now() - _aware(recent.created_at)).total_seconds()
                if age < _EARLY_ACCESS_COOLDOWN_SECONDS:
                    return "duplicate_suppressed", None, None
            return "pending", None, None
        # Auto-approve: at most ONE active invitation per email; honour a short cooldown.
        existing = s.execute(
            select(HostedInvitation).where(
                HostedInvitation.email == email,
                HostedInvitation.status == "pending",
            )
        ).scalars().first()
        if existing is not None:
            age = (self.now() - _aware(existing.created_at)).total_seconds()
            if age < _EARLY_ACCESS_COOLDOWN_SECONDS:
                return "duplicate_suppressed", existing.id, None
            existing.status = "revoked"  # supersede the stale one; keep exactly one active
            existing.revoked_at = self.now()
        inv_id, token, tenant_name = self._issue_system_invitation(s, email)
        # Canonical customer path route under www (no hash). Old hash links still resolve via the
        # SPA's hash→path shim during the compatibility window.
        url = f"{self.config.urls.portal.rstrip('/')}/app/accept-invite?token={token}"
        return "invited", inv_id, {"url": url, "token": token, "tenant": tenant_name}

    def _issue_system_invitation(self, s, email: str):
        """Create a NEW tenant + a lowest-role invitation as the SYSTEM actor. Returns (id, token,
        tenant_name). The role is fixed server-side — never chosen by the browser."""
        role = self.config.intake.default_customer_role
        if role not in roles.CUSTOMER_ROLES or role == roles.TENANT_ADMIN:
            role = roles.TENANT_OPERATOR  # never platform_admin / tenant_admin via this path
        tenant_name = email.split("@")[0][:80] or "early-access"
        token = security.generate_token(24)
        inv = HostedInvitation(
            tenant_id=None, proposed_tenant_name=tenant_name, email=email, role=role,
            token_digest=security.token_digest(token), inviter_user_id=None,
            note="early-access auto-approval", expires_at=self.expiry(7 * 24 * 3600),
            created_at=self.now(),
        )
        s.add(inv)
        self._audit(s, action="invitation.create", actor_kind="system", target=email,
                    metadata={"source": "early_access_auto", "role": role})
        s.flush()
        return inv.id, token, tenant_name

    # -- mailing list (double opt-in) ---------------------------------------
    def subscribe_mailing(self, *, email: str, turnstile_token: str | None = None,
                          client_ip: str | None = None, rate_key: str | None = None) -> dict:
        email = _norm_email(email)
        if not _EMAIL_RE.match(email) or len(email) > 254:
            return dict(_GENERIC_OK)
        with self._session() as s:
            try:
                self._check_rate(s, "mailing_ip", rate_key or client_ip or "global",
                                 self.config.rate_limits.mailing_per_minute)
                self._check_rate(s, "mailing_email", email,
                                 self.config.rate_limits.mailing_per_minute)
            except Exception:  # noqa: BLE001
                s.commit()
                return dict(_GENERIC_OK)
            if not self._turnstile_ok(turnstile_token, client_ip):
                s.commit()
                return dict(_GENERIC_OK)
            mail = self._begin_mailing_subscription(s, email, source="public_form")
            self._audit(s, action="mailing.subscribe", actor_kind="system", target=email[:120])
            s.commit()
        if mail and mail.get("confirm_url"):
            self.email.send(mailing_confirmation_email(to=email, confirm_url=mail["confirm_url"]))
        return dict(_GENERIC_OK)

    def _begin_mailing_subscription(self, s, email: str, *, source: str):
        """Create/refresh a PENDING subscriber + confirm token. Returns {confirm_url} or None
        (already confirmed -> no email; anti-enumeration)."""
        sub = s.execute(
            select(HostedMailingSubscriber).where(HostedMailingSubscriber.email == email)
        ).scalar_one_or_none()
        if sub is not None and sub.status == "confirmed":
            return None  # already subscribed; never disclose, never resend
        token = security.generate_token(24)
        digest = security.token_digest(token)
        if sub is None:
            sub = HostedMailingSubscriber(email=email, source=source, created_at=self.now())
            s.add(sub)
        sub.status = "pending"
        sub.confirm_token_digest = digest
        sub.confirm_expires_at = self.expiry(_MAILING_CONFIRM_TTL_SECONDS)
        sub.unsubscribed_at = None
        s.flush()
        url = f"{self.config.urls.public_site.rstrip('/')}/mailing-confirm.html?token={token}"
        return {"confirm_url": url}

    def confirm_mailing(self, token: str) -> dict:
        digest = security.token_digest(token or "")
        with self._session() as s:
            sub = s.execute(
                select(HostedMailingSubscriber).where(
                    HostedMailingSubscriber.confirm_token_digest == digest)
            ).scalar_one_or_none()
            if (sub is None or sub.confirm_expires_at is None
                    or _aware(sub.confirm_expires_at) <= self.now()):
                return {"status": "invalid"}
            unsub = security.generate_token(24)
            sub.status = "confirmed"
            sub.confirmed_at = self.now()
            sub.confirm_token_digest = None
            sub.confirm_expires_at = None
            sub.unsubscribe_token_digest = security.token_digest(unsub)
            self._audit(s, action="mailing.confirm", actor_kind="system", target=sub.email[:120])
            s.commit()
        # The raw unsubscribe token is NOT returned in the HTTP response; it rides only in the
        # unsubscribe link placed in future mailing-list messages.
        return {"status": "confirmed"}

    def unsubscribe_mailing(self, token: str) -> dict:
        digest = security.token_digest(token or "")
        with self._session() as s:
            sub = s.execute(
                select(HostedMailingSubscriber).where(
                    HostedMailingSubscriber.unsubscribe_token_digest == digest)
            ).scalar_one_or_none()
            if sub is None:
                return {"status": "ok"}  # generic; deterministic no-op
            sub.status = "unsubscribed"
            sub.unsubscribed_at = self.now()
            self._audit(s, action="mailing.unsubscribe", actor_kind="system",
                        target=sub.email[:120])
            s.commit()
        return {"status": "ok"}

    # -- admin: early-access request review ----------------------------------
    def _early_access_dict(self, s, r) -> dict:
        inv_status = None
        if r.invitation_id:
            inv = s.get(HostedInvitation, r.invitation_id)
            inv_status = inv.status if inv else None
        return {"id": r.id, "email": r.email, "status": r.status,
                "mailing_opt_in": bool(r.mailing_opt_in),
                "invitation_id": r.invitation_id, "invitation_status": inv_status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None}

    def list_early_access_requests(self, principal, *, status: str | None = None,
                                   limit: int = 500) -> dict:
        """Admin: the early-access review queue (newest first), optionally filtered by status."""
        self.require_platform_admin(principal)
        with self._session() as s:
            q = (select(HostedEarlyAccessRequest)
                 .order_by(HostedEarlyAccessRequest.created_at.desc()).limit(int(limit)))
            if status:
                q = q.where(HostedEarlyAccessRequest.status == status)
            rows = s.execute(q).scalars().all()
            counts = dict(s.execute(
                select(HostedEarlyAccessRequest.status, func.count())
                .group_by(HostedEarlyAccessRequest.status)).all())
            return {"requests": [self._early_access_dict(s, r) for r in rows],
                    "counts": {k: int(v) for k, v in counts.items()}}

    def _get_request(self, s, request_id: str):
        req = s.get(HostedEarlyAccessRequest, request_id)
        if req is None:
            raise NotFoundError("early_access_request_not_found")
        return req

    def approve_early_access_request(self, principal, *, request_id: str, role: str | None = None,
                                     tenant_name: str | None = None) -> dict:
        """Admin: approve a request -> create + send an invitation FROM THE STORED EMAIL (no
        re-keying), then mark the request approved and link the invitation. Idempotent: if an
        active invitation already exists for the request, it is returned without resending."""
        self.require_platform_admin(principal)
        with self._session() as s:
            req = self._get_request(s, request_id)
            email = req.email
            if req.invitation_id is not None:
                inv = s.get(HostedInvitation, req.invitation_id)
                if inv is not None and inv.status == "pending":
                    return {"status": "already_invited", "request_id": req.id,
                            "invitation_id": inv.id, "email": email}
        # create_invitation runs its own transaction and SENDS the invitation email.
        chosen_role = role or self.config.intake.default_customer_role
        if chosen_role not in roles.CUSTOMER_ROLES or chosen_role == roles.TENANT_ADMIN:
            chosen_role = roles.TENANT_OPERATOR
        result = self.create_invitation(
            principal, email=email, role=chosen_role,
            proposed_tenant_name=(tenant_name or email.split("@")[0][:80] or "early-access"),
            note="early-access approval")
        with self._session() as s:
            req = self._get_request(s, request_id)
            req.status = "approved"
            req.invitation_id = result["invitation_id"]
            req.updated_at = self.now()
            self._audit(s, action="early_access.approve", actor_user_id=principal.user_id,
                        target=email, metadata={"invitation_id": result["invitation_id"]})
            s.commit()
        return {"status": "approved", "request_id": request_id,
                "invitation_id": result["invitation_id"], "email": email}

    def _set_request_status(self, principal, request_id: str, new_status: str, action: str) -> dict:
        self.require_platform_admin(principal)
        if new_status not in ("rejected", "waitlisted", "pending"):
            raise ControlPlaneError("invalid_request_status")
        with self._session() as s:
            req = self._get_request(s, request_id)
            req.status = new_status
            req.updated_at = self.now()
            self._audit(s, action=action, actor_user_id=principal.user_id, target=req.email)
            s.commit()
            return self._early_access_dict(s, req)

    def resend_early_access_invitation(self, principal, *, request_id: str) -> dict:
        """Admin: re-send the invitation already issued for an approved request, to its stored
        email. Truthful when the request has no invitation yet (approve first)."""
        self.require_platform_admin(principal)
        with self._session() as s:
            req = self._get_request(s, request_id)
            invitation_id = req.invitation_id
        if invitation_id is None:
            raise ControlPlaneError("no_invitation_to_resend")
        return self.resend_invitation(principal, invitation_id=invitation_id)

    def reject_early_access_request(self, principal, *, request_id: str) -> dict:
        """Admin: reject a request (no invitation; no email storm)."""
        return self._set_request_status(principal, request_id, "rejected", "early_access.reject")

    def waitlist_early_access_request(self, principal, *, request_id: str) -> dict:
        """Admin: wait-list a request for later review."""
        return self._set_request_status(principal, request_id, "waitlisted",
                                        "early_access.waitlist")

    def mailing_status(self, principal) -> dict:
        """Admin-only bounded counts — never exposes addresses."""
        self.require_platform_admin(principal)
        with self._session() as s:
            rows = s.execute(
                select(HostedMailingSubscriber.status, func.count())
                .group_by(HostedMailingSubscriber.status)
            ).all()
        counts = {status: int(n) for status, n in rows}
        return {"pending": counts.get("pending", 0), "confirmed": counts.get("confirmed", 0),
                "unsubscribed": counts.get("unsubscribed", 0)}


def _aware(dt):
    from datetime import UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
