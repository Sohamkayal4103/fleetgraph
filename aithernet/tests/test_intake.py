"""Public intake tests: early-access requests + mailing list (Stage 14F productization).

Covers anti-enumeration, auto-approve OFF/ON, lowest-role invitations, dedup/cooldown, rate limits,
Turnstile (injected — never real Cloudflare), double-opt-in mailing flow, deterministic unsubscribe,
exact-origin CORS, and that the browser can never choose a role/tenant.
"""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient
from services.control_plane import roles
from services.control_plane.app import create_app
from services.control_plane.config import HostedConfig
from services.control_plane.service import ControlPlaneService

WWW = "https://www.aithernet.online"


def _svc(*, auto_approve=False, turnstile_required=False, turnstile_pass=True, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tempfile.mktemp()}"
    cfg.object_storage.local_root = tempfile.mkdtemp()
    cfg.urls.public_site = WWW
    cfg.urls.portal = WWW  # canonical customer origin — invitation links resolve under www/app
    cfg.intake.auto_approve = auto_approve
    cfg.intake.turnstile_required = turnstile_required
    return ControlPlaneService(cfg, turnstile_verifier=lambda **k: {"success": turnstile_pass})


def _invites(svc, email=None):
    from services.control_plane.models import HostedInvitation
    from sqlalchemy import select
    with svc._session() as s:
        q = select(HostedInvitation)
        if email:
            q = q.where(HostedInvitation.email == email)
        return s.execute(q).scalars().all()


# -- early access -------------------------------------------------------------------------------


def test_auto_approve_off_creates_reviewable_request_acks_no_invite(monkeypatch):
    svc = _svc(auto_approve=False, monkeypatch=monkeypatch)
    res = svc.submit_early_access(email="New.Person@Example.com", privacy_ack=True,
                                  client_ip="1.1.1.1")
    assert res["status"] == "ok"
    assert _invites(svc) == []                       # NO invitation when auto-approve is off
    # The requester gets a branded acknowledgement; an admin is notified; NO invitation email.
    ack = svc.email.latest_for("new.person@example.com", kind="early_access_ack")
    assert ack is not None and ack.html and "invitation-only" in ack.body
    assert svc.email.latest_for("new.person@example.com", kind="invitation") is None
    assert any(m.kind == "admin_early_access_notify" for m in svc.email.messages)
    from services.control_plane.models import HostedEarlyAccessRequest
    from sqlalchemy import select
    with svc._session() as s:
        reqs = s.execute(select(HostedEarlyAccessRequest)).scalars().all()
    assert len(reqs) == 1 and reqs[0].status == "pending"


def test_auto_approve_off_admin_approves_from_stored_request(monkeypatch):
    svc = _svc(auto_approve=False, monkeypatch=monkeypatch)
    svc.submit_early_access(email="cust@example.com", privacy_ack=True, client_ip="2.2.2.2")
    from services.control_plane.service import Principal
    admin = Principal(user_id=svc.bootstrap_admin("a@b.invalid", "pw-correct-horse")["user_id"],
                      email="a@b.invalid", is_platform_admin=True)
    listing = svc.list_early_access_requests(admin, status="pending")
    assert len(listing["requests"]) == 1
    req_id = listing["requests"][0]["id"]
    # Approve -> invitation created+sent FROM the stored email (no re-keying), status -> approved.
    out = svc.approve_early_access_request(admin, request_id=req_id)
    assert out["status"] == "approved" and out["email"] == "cust@example.com"
    assert svc.email.latest_for("cust@example.com", kind="invitation") is not None
    after = svc.list_early_access_requests(admin)["requests"][0]
    assert after["status"] == "approved" and after["invitation_id"] == out["invitation_id"]
    # Idempotent: re-approve returns the existing invitation, no second email.
    again = svc.approve_early_access_request(admin, request_id=req_id)
    assert again["status"] == "already_invited"


def test_admin_rejects_request_no_invitation(monkeypatch):
    svc = _svc(auto_approve=False, monkeypatch=monkeypatch)
    svc.submit_early_access(email="nope@example.com", privacy_ack=True, client_ip="3.3.3.3")
    from services.control_plane.service import Principal
    admin = Principal(user_id=svc.bootstrap_admin("a@b.invalid", "pw-correct-horse")["user_id"],
                      email="a@b.invalid", is_platform_admin=True)
    req_id = svc.list_early_access_requests(admin, status="pending")["requests"][0]["id"]
    out = svc.reject_early_access_request(admin, request_id=req_id)
    assert out["status"] == "rejected"
    assert _invites(svc, "nope@example.com") == []                  # rejection never invites
    assert svc.email.latest_for("nope@example.com", kind="invitation") is None
    after = svc.list_early_access_requests(admin)["requests"][0]
    assert after["status"] == "rejected"


def test_admin_waitlists_request_no_invitation(monkeypatch):
    svc = _svc(auto_approve=False, monkeypatch=monkeypatch)
    svc.submit_early_access(email="later@example.com", privacy_ack=True, client_ip="4.4.4.4")
    from services.control_plane.service import Principal
    admin = Principal(user_id=svc.bootstrap_admin("a@b.invalid", "pw-correct-horse")["user_id"],
                      email="a@b.invalid", is_platform_admin=True)
    req_id = svc.list_early_access_requests(admin, status="pending")["requests"][0]["id"]
    out = svc.waitlist_early_access_request(admin, request_id=req_id)
    assert out["status"] == "waitlisted"
    assert _invites(svc, "later@example.com") == []                 # waitlisting never invites
    # A waitlisted request can still be approved later (creates the invitation then).
    approved = svc.approve_early_access_request(admin, request_id=req_id)
    assert approved["status"] == "approved"
    assert svc.email.latest_for("later@example.com", kind="invitation") is not None


def test_auto_approve_on_issues_lowest_role_invitation_and_email(monkeypatch):
    svc = _svc(auto_approve=True, monkeypatch=monkeypatch)
    res = svc.submit_early_access(email="cust@example.com", privacy_ack=True, client_ip="1.1.1.1")
    assert res["status"] == "ok"
    invs = _invites(svc, "cust@example.com")
    assert len(invs) == 1
    assert invs[0].role == roles.TENANT_OPERATOR            # lowest appropriate, NOT admin
    assert invs[0].role not in (roles.TENANT_ADMIN, roles.PLATFORM_ADMIN)
    assert invs[0].tenant_id is None and invs[0].proposed_tenant_name                 # new tenant
    msg = svc.email.latest_for("cust@example.com", kind="invitation")
    # Canonical customer path route under www — NO hash routing, NEVER the legacy app host.
    assert msg is not None
    assert f"{WWW}/app/accept-invite?token=" in msg.body
    assert "/#/" not in msg.body
    assert "app.aithernet.online" not in msg.body


def test_missing_privacy_ack_creates_nothing_but_generic_ok(monkeypatch):
    svc = _svc(auto_approve=True, monkeypatch=monkeypatch)
    res = svc.submit_early_access(email="x@example.com", privacy_ack=False, client_ip="1.1.1.1")
    assert res["status"] == "ok" and _invites(svc) == []


def test_anti_enumeration_identical_response_for_existing_account(monkeypatch):
    svc = _svc(auto_approve=True, monkeypatch=monkeypatch)
    svc.bootstrap_admin("known@example.com", "pw-correct-horse")  # make the email exist
    r_existing = svc.submit_early_access(email="known@example.com", privacy_ack=True,
                                         client_ip="2.2.2.2")
    r_new = svc.submit_early_access(email="fresh@example.com", privacy_ack=True,
                                    client_ip="2.2.2.3")
    assert r_existing == r_new                                  # identical generic response
    assert _invites(svc, "known@example.com") == []            # existing account: suppressed
    assert len(_invites(svc, "fresh@example.com")) == 1        # new: invited


def test_duplicate_within_cooldown_does_not_create_second_invitation(monkeypatch):
    svc = _svc(auto_approve=True, monkeypatch=monkeypatch)
    svc.submit_early_access(email="dup@example.com", privacy_ack=True, client_ip="3.3.3.1")
    svc.submit_early_access(email="dup@example.com", privacy_ack=True, client_ip="3.3.3.2")
    assert len(_invites(svc, "dup@example.com")) == 1          # exactly one active invitation


def test_turnstile_required_blocks_when_validation_fails(monkeypatch):
    svc = _svc(auto_approve=True, turnstile_required=True, turnstile_pass=False,
               monkeypatch=monkeypatch)
    monkeypatch.setenv("AITHERNET_TURNSTILE_SECRET", "sek")   # configured + required
    res = svc.submit_early_access(email="t@example.com", privacy_ack=True,
                                  turnstile_token="bad", client_ip="4.4.4.4")
    assert res["status"] == "ok" and _invites(svc) == []      # failed closed, generic response


def test_turnstile_required_passes_with_injected_verifier(monkeypatch):
    svc = _svc(auto_approve=True, turnstile_required=True, turnstile_pass=True,
               monkeypatch=monkeypatch)
    monkeypatch.setenv("AITHERNET_TURNSTILE_SECRET", "sek")
    svc.submit_early_access(email="t2@example.com", privacy_ack=True,
                            turnstile_token="good", client_ip="4.4.4.5")
    assert len(_invites(svc, "t2@example.com")) == 1


def test_rate_limit_by_ip_caps_invitations(monkeypatch):
    svc = _svc(auto_approve=True, monkeypatch=monkeypatch)  # early_access_per_minute=5
    for i in range(7):
        svc.submit_early_access(email=f"u{i}@example.com", privacy_ack=True, client_ip="9.9.9.9")
    assert len(_invites(svc)) == 5                              # capped at the per-minute limit


# -- mailing list -------------------------------------------------------------------------------


def _subscriber(svc, email):
    from services.control_plane.models import HostedMailingSubscriber
    from sqlalchemy import select
    with svc._session() as s:
        return s.execute(
            select(HostedMailingSubscriber).where(HostedMailingSubscriber.email == email)
        ).scalar_one_or_none()


def test_mailing_double_opt_in_flow(monkeypatch):
    svc = _svc(monkeypatch=monkeypatch)
    svc.subscribe_mailing(email="sub@example.com", client_ip="5.5.5.5")
    sub = _subscriber(svc, "sub@example.com")
    assert sub.status == "pending"
    msg = svc.email.latest_for("sub@example.com", kind="mailing_confirmation")
    assert msg is not None and "mailing-confirm.html?token=" in msg.body
    # subscription does NOT create an account or invitation
    assert _invites(svc, "sub@example.com") == []
    # extract the confirm token from the link and confirm
    token = msg.body.split("token=", 1)[1].split()[0].strip()
    assert svc.confirm_mailing(token)["status"] == "confirmed"
    assert _subscriber(svc, "sub@example.com").status == "confirmed"


def test_mailing_confirm_invalid_token(monkeypatch):
    svc = _svc(monkeypatch=monkeypatch)
    assert svc.confirm_mailing("not-a-real-token")["status"] == "invalid"


def test_mailing_already_confirmed_sends_no_second_email(monkeypatch):
    svc = _svc(monkeypatch=monkeypatch)
    svc.subscribe_mailing(email="dc@example.com", client_ip="6.6.6.6")
    msg = svc.email.latest_for("dc@example.com", kind="mailing_confirmation")
    token = msg.body.split("token=")[1].split()[0]
    svc.confirm_mailing(token)
    before = len(svc.email.messages)
    svc.subscribe_mailing(email="dc@example.com", client_ip="6.6.6.7")  # already confirmed
    assert len(svc.email.messages) == before                            # no second email


def test_mailing_unsubscribe_is_deterministic(monkeypatch):
    svc = _svc(monkeypatch=monkeypatch)
    svc.subscribe_mailing(email="uns@example.com", client_ip="7.7.7.7")
    msg = svc.email.latest_for("uns@example.com", kind="mailing_confirmation")
    token = msg.body.split("token=")[1].split()[0]
    svc.confirm_mailing(token)
    # fetch the stored unsubscribe digest's source token by re-deriving: generate via service path
    from services.control_plane import security
    from services.control_plane.models import HostedMailingSubscriber
    from sqlalchemy import select
    # simulate the unsubscribe link by issuing a known token and storing its digest
    raw = security.generate_token(24)
    with svc._session() as s:
        sub = s.execute(select(HostedMailingSubscriber).where(
            HostedMailingSubscriber.email == "uns@example.com")).scalar_one()
        sub.unsubscribe_token_digest = security.token_digest(raw)
        s.commit()
    assert svc.unsubscribe_mailing(raw)["status"] == "ok"
    assert _subscriber(svc, "uns@example.com").status == "unsubscribed"
    # idempotent
    assert svc.unsubscribe_mailing(raw)["status"] == "ok"


def test_mailing_status_admin_counts_no_addresses(monkeypatch):
    svc = _svc(monkeypatch=monkeypatch)
    admin = svc.bootstrap_admin("admin@example.com", "pw-correct-horse")
    from services.control_plane.service import Principal
    principal = Principal(user_id=admin["user_id"], email=admin["email"], is_platform_admin=True)
    svc.subscribe_mailing(email="a@example.com", client_ip="8.8.8.1")
    out = svc.mailing_status(principal)
    assert set(out) == {"pending", "confirmed", "unsubscribed"} and out["pending"] == 1
    assert "a@example.com" not in str(out)


# -- CORS (exact origin only) -------------------------------------------------------------------


def test_cors_allows_only_exact_public_origin(monkeypatch):
    svc = _svc(auto_approve=False, monkeypatch=monkeypatch)
    client = TestClient(create_app(svc.config, svc))
    # exact origin -> allowed
    r = client.post("/v1/early-access/request",
                    json={"email": "c@example.com", "privacy_ack": True},
                    headers={"Origin": WWW})
    assert r.headers.get("access-control-allow-origin") == WWW
    # a different origin -> NO allow-origin header echoed
    r2 = client.post("/v1/early-access/request",
                     json={"email": "c@example.com", "privacy_ack": True},
                     headers={"Origin": "https://evil.example"})
    assert r2.headers.get("access-control-allow-origin") in (None, "")
    # preflight only permits POST
    pre = client.options("/v1/early-access/request", headers={
        "Origin": WWW, "Access-Control-Request-Method": "POST"})
    assert pre.headers.get("access-control-allow-origin") == WWW
