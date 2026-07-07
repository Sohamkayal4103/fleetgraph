"""Resend-invitation tests (live acceptance defect #1).

Resending re-sends the EXISTING invitation to its recorded email with a ROTATED one-time token and
a deliberately reset expiry. The raw token is never returned; no duplicate invitation or membership
is created; terminal states (accepted/revoked/consumed) are refused truthfully; pending/expired are
reactivated; SMTP submission is proven; repeated clicks are rate-limited; an audit row is written.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from services.control_plane import roles
from services.control_plane.config import HostedConfig
from services.control_plane.errors import ControlPlaneError, RateLimitError
from services.control_plane.models import HostedInvitation, HostedMembership
from services.control_plane.service import ControlPlaneService, Principal
from sqlalchemy import select


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    a = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse")
    return Principal(user_id=a["user_id"], email=a["email"], is_platform_admin=True)


def _invitation(svc, admin, email="cust@example.invalid"):
    return svc.create_invitation(admin, email=email, proposed_tenant_name="Acme",
                                 role=roles.TENANT_OPERATOR)


def _all_invites(svc):
    with svc._session() as s:
        return s.execute(select(HostedInvitation)).scalars().all()


def test_resend_rotates_token_resets_expiry_and_sends_email(svc, admin):
    inv = _invitation(svc, admin)
    created_email = svc.email.latest_for("cust@example.invalid", kind="invitation")
    out = svc.resend_invitation(admin, invitation_id=inv["invitation_id"])

    assert out["status"] == "resent" and out["email"] == "cust@example.invalid"
    assert "token" not in out                       # the raw token is NEVER returned
    assert out["expires_at"] >= inv["expires_at"]   # expiry deliberately reset/extended
    # SMTP submission proven: a fresh invitation email was sent (distinct from the create email).
    resent = svc.email.latest_for("cust@example.invalid", kind="invitation")
    assert resent is not None and resent is not created_email
    # The OLD token no longer validates (rotated); the SAME single invitation row remains.
    assert svc.validate_invitation(inv["token"])["status"] == "invalid"
    assert len(_all_invites(svc)) == 1
    # An audit row was recorded.
    actions = [a["action"] for a in svc.list_audit(admin)]
    assert "invitation.resend" in actions


def test_resend_no_duplicate_invitation_or_membership(svc, admin):
    inv = _invitation(svc, admin)
    svc.resend_invitation(admin, invitation_id=inv["invitation_id"])
    svc.resend_invitation(admin, invitation_id=inv["invitation_id"])
    # Idempotent on the SAME invitation row — two resends never fork a second invitation.
    assert len(_all_invites(svc)) == 1
    # Two resends submit two more emails (each a fresh link); create + 2 resends = 3 total.
    sent = [m for m in svc.email.messages if m.kind == "invitation"]
    assert len(sent) == 3
    # Resending never creates a tenant membership.
    with svc._session() as s:
        assert s.execute(select(HostedMembership)).scalars().all() == []


def test_resend_refuses_accepted_revoked_consumed(svc, admin):
    # accepted -> refused truthfully
    inv = _invitation(svc, admin, email="a@example.invalid")
    svc.accept_invitation(inv["token"], email="a@example.invalid",
                          password="cust-correct-horse-9", accept_required_policies=True)
    with pytest.raises(ControlPlaneError) as e_acc:
        svc.resend_invitation(admin, invitation_id=inv["invitation_id"])
    assert "accepted" in e_acc.value.code

    # revoked -> refused truthfully
    inv2 = _invitation(svc, admin, email="b@example.invalid")
    svc.revoke_invitation(admin, inv2["invitation_id"])
    with pytest.raises(ControlPlaneError) as e_rev:
        svc.resend_invitation(admin, invitation_id=inv2["invitation_id"])
    assert "revoked" in e_rev.value.code

    # consumed (used_count >= maximum_uses) -> refused truthfully
    inv3 = _invitation(svc, admin, email="c@example.invalid")
    with svc._session() as s:
        row = s.get(HostedInvitation, inv3["invitation_id"])
        row.used_count = row.maximum_uses
        s.commit()
    with pytest.raises(ControlPlaneError) as e_con:
        svc.resend_invitation(admin, invitation_id=inv3["invitation_id"])
    assert "consumed" in e_con.value.code


def test_resend_reactivates_an_expired_invitation(svc, admin):
    inv = _invitation(svc, admin, email="d@example.invalid")
    with svc._session() as s:
        row = s.get(HostedInvitation, inv["invitation_id"])
        row.expires_at = datetime.now(UTC) - timedelta(days=2)
        s.commit()
    out = svc.resend_invitation(admin, invitation_id=inv["invitation_id"])
    assert out["status"] == "resent"
    # Fresh expiry is in the future and the invitation is usable again.
    assert datetime.fromisoformat(out["expires_at"]) > datetime.now(UTC)


def test_resend_is_rate_limited(svc, admin):
    inv = _invitation(svc, admin, email="e@example.invalid")
    limit = svc.config.rate_limits.invitation_per_minute
    with pytest.raises(RateLimitError):
        for _ in range(limit + 2):
            svc.resend_invitation(admin, invitation_id=inv["invitation_id"])


def test_early_access_resend_truthful_without_invitation(svc, admin):
    # A request that has not been approved yet has no invitation to resend.
    svc.submit_early_access(email="pending@example.invalid", privacy_ack=True, client_ip="9.9.9.9")
    listing = svc.list_early_access_requests(admin, status="pending")
    req_id = listing["requests"][0]["id"]
    with pytest.raises(ControlPlaneError) as exc:
        svc.resend_early_access_invitation(admin, request_id=req_id)
    assert "no_invitation_to_resend" in exc.value.code
    # After approval, the early-access resend re-sends the issued invitation.
    svc.approve_early_access_request(admin, request_id=req_id)
    out = svc.resend_early_access_invitation(admin, request_id=req_id)
    assert out["status"] == "resent" and out["email"] == "pending@example.invalid"
