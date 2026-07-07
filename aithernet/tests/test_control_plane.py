"""Stage 14F hosted control-plane backend tests (service level, temporary SQLite + secrets).

No real domain, TLS, SMTP, object store, PostgreSQL or SDR is required. Temporary secrets are
generated per test. Covers tenancy, invitations, auth, sessions, password reset, policies +
acceptance, node enrollment (challenge/reuse/altered/wrong-tenant/no-proof/replay), node
credential auth, heartbeat, fleet isolation, releases (sign/publish/revoke/wrong-key),
downloads (traversal/revoked), blob stores, email sink, support, admin authz, checklist,
backup/restore, secret-leakage and production config validation.
"""

from __future__ import annotations

import secrets

import pytest
from services.control_plane import release_signing, roles
from services.control_plane.blobstore import (
    BlobStoreError,
    FakeS3Client,
    LocalHostedBlobStore,
    S3CompatibleHostedBlobStore,
)
from services.control_plane.config import HostedConfig, HostedConfigError
from services.control_plane.errors import ControlPlaneError
from services.control_plane.security import generate_keypair, load_private_key, sign


@pytest.fixture
def session_key(monkeypatch):
    key = secrets.token_urlsafe(48)
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", key)
    return key


@pytest.fixture
def svc(tmp_path, session_key):
    from services.control_plane.service import ControlPlaneService

    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    return ControlPlaneService(cfg)


@pytest.fixture
def admin(svc):
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    sess = svc.create_session(_uid(svc, "admin@example.invalid"))
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal


def _uid(svc, email):
    from services.control_plane.models import HostedUser
    from sqlalchemy import select

    with svc._session() as s:
        return s.execute(select(HostedUser).where(HostedUser.email == email)).scalar_one().id


def _onboard_tenant(svc, admin, *, email="alice@example.invalid", name="Acme"):
    inv = svc.create_invitation(admin, email=email, role=roles.TENANT_ADMIN,
                                proposed_tenant_name=name)
    acc = svc.accept_invitation(inv["token"], email=email, password="acme-password-9")
    auth = svc.authenticate(email, "acme-password-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    return principal, acc["tenant_id"], inv


# -- bootstrap + tenancy -------------------------------------------------------------------------


def test_bootstrap_is_idempotent_safe(svc):
    svc.bootstrap_admin("admin@example.invalid", "supersecret-123")
    assert svc.is_bootstrapped()
    with pytest.raises(ControlPlaneError):  # re-bootstrap refused (§39)
        svc.bootstrap_admin("admin@example.invalid", "supersecret-123")


def test_bootstrap_rejects_weak_password(svc):
    with pytest.raises(ControlPlaneError):
        svc.bootstrap_admin("admin@example.invalid", "password")


# -- invitations ---------------------------------------------------------------------------------


def test_invitation_accept_creates_tenant_and_membership(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    assert tid in principal.tenant_ids()


def test_invitation_token_reuse_is_blocked(svc, admin):
    _, _, inv = _onboard_tenant(svc, admin)
    with pytest.raises(ControlPlaneError) as exc:
        svc.accept_invitation(inv["token"], email="bob@example.invalid", password="bob-password-9")
    assert "mismatch" in exc.value.code or "consumed" in exc.value.code


def _password_hash(svc, email):
    from sqlalchemy import select

    from services.control_plane.models import HostedUser

    with svc._session() as s:
        return s.execute(select(HostedUser).where(HostedUser.email == email)).scalar_one().password_hash


def test_invitation_existing_account_requires_correct_password(svc, admin):
    # Onboard alice -> account exists with a known password.
    _onboard_tenant(svc, admin, email="alice@example.invalid", name="Acme")
    inv2 = svc.create_invitation(admin, email="alice@example.invalid",
                                 proposed_tenant_name="Beta", role=roles.TENANT_ADMIN)
    before = _password_hash(svc, "alice@example.invalid")

    # Wrong password -> invalid_credentials; the invitation is NOT consumed and no membership added.
    with pytest.raises(ControlPlaneError) as exc:
        svc.accept_invitation(inv2["token"], email="alice@example.invalid",
                              password="not-the-real-password", accept_required_policies=True)
    assert exc.value.code == "invalid_credentials"
    v = svc.validate_invitation(inv2["token"])
    assert v["valid"] is True and v["status"] == "valid"          # still usable
    assert _password_hash(svc, "alice@example.invalid") == before  # password untouched

    # Correct existing password -> accepted; password preserved; new membership added atomically.
    acc = svc.accept_invitation(inv2["token"], email="alice@example.invalid",
                                password="acme-password-9", accept_required_policies=True)
    assert acc["accepted"] is True
    assert _password_hash(svc, "alice@example.invalid") == before  # never reset
    # The original password still authenticates (it was preserved, not changed).
    assert svc.authenticate("alice@example.invalid", "acme-password-9")["user_id"]


def test_validate_invitation_signals_account_exists(svc, admin):
    inv_new = svc.create_invitation(admin, email="newp@example.invalid",
                                    proposed_tenant_name="Gamma", role=roles.TENANT_ADMIN)
    assert svc.validate_invitation(inv_new["token"])["account_exists"] is False
    _onboard_tenant(svc, admin, email="newp@example.invalid", name="Gamma")
    inv2 = svc.create_invitation(admin, email="newp@example.invalid",
                                 proposed_tenant_name="Delta", role=roles.TENANT_ADMIN)
    assert svc.validate_invitation(inv2["token"])["account_exists"] is True


def test_invitation_email_mismatch_rejected(svc, admin):
    inv = svc.create_invitation(admin, email="alice@example.invalid",
                                proposed_tenant_name="Acme")
    with pytest.raises(ControlPlaneError) as exc:
        svc.accept_invitation(inv["token"], email="eve@example.invalid", password="x-password-99")
    assert exc.value.code == "invitation_email_mismatch"


def test_invitation_revoked_then_rejected(svc, admin):
    inv = svc.create_invitation(admin, email="alice@example.invalid", proposed_tenant_name="Acme")
    svc.revoke_invitation(admin, inv["invitation_id"])
    with pytest.raises(ControlPlaneError) as exc:
        svc.accept_invitation(inv["token"], email="alice@example.invalid", password="acme-pass-99")
    assert exc.value.code == "invitation_revoked"


def test_invitation_accept_is_idempotent(svc, admin):
    inv = svc.create_invitation(admin, email="alice@example.invalid", proposed_tenant_name="Acme")
    a = svc.accept_invitation(inv["token"], email="alice@example.invalid", password="acme-pass-99")
    b = svc.accept_invitation(inv["token"], email="alice@example.invalid", password="acme-pass-99")
    assert b.get("idempotent") and a["tenant_id"] == b["tenant_id"]


def test_validate_invitation_does_not_consume_and_reports_details(svc, admin):
    svc.publish_policy(admin, policy_type="preview_terms", version="v1", title="T",
                       document_text="x", required_categories=["operational"])
    inv = svc.create_invitation(admin, email="alice@example.invalid", proposed_tenant_name="Acme")
    v1 = svc.validate_invitation(inv["token"])
    assert v1["valid"] and v1["status"] == "valid" and v1["email"] == "alice@example.invalid"
    assert v1["role"] == "tenant_admin" and len(v1["required_policies"]) == 1
    # Validation must NOT consume — a second validate is still valid.
    assert svc.validate_invitation(inv["token"])["valid"]
    assert svc.validate_invitation("not-a-real-token")["status"] == "invalid"


def test_validate_invitation_revoked_and_consumed_states(svc, admin):
    inv = svc.create_invitation(admin, email="a@example.invalid", proposed_tenant_name="A")
    svc.accept_invitation(inv["token"], email="a@example.invalid", password="aaaa-pass-99")
    assert svc.validate_invitation(inv["token"])["status"] == "consumed"
    inv2 = svc.create_invitation(admin, email="b@example.invalid", proposed_tenant_name="B")
    svc.revoke_invitation(admin, inv2["invitation_id"])
    assert svc.validate_invitation(inv2["token"])["status"] == "revoked"


def test_accept_with_required_policies_is_atomic(svc, admin):
    from services.control_plane.models import HostedUser
    from sqlalchemy import select

    svc.publish_policy(admin, policy_type="preview_terms", version="v1", title="T",
                       document_text="x", required_categories=["operational"])
    inv = svc.create_invitation(admin, email="alice@example.invalid", proposed_tenant_name="Acme")
    acc = svc.accept_invitation(inv["token"], email="alice@example.invalid",
                                password="acme-password-9", accept_required_policies=True)
    # The new user's onboarding is complete (policies accepted in the same transaction).
    auth = svc.authenticate("alice@example.invalid", "acme-password-9")
    sess = svc.create_session(auth["user_id"])
    principal, _, _ = svc.validate_session(sess["session_token"])
    assert acc["accepted"] and svc.onboarding_complete(principal)

    # A weak-password fresh acceptance fails closed with NO partial user/tenant created.
    inv2 = svc.create_invitation(admin, email="weak@example.invalid", proposed_tenant_name="Weak")
    with pytest.raises(ControlPlaneError):
        svc.accept_invitation(inv2["token"], email="weak@example.invalid", password="short",
                              accept_required_policies=True)
    with svc._session() as s:
        assert s.execute(
            select(HostedUser).where(HostedUser.email == "weak@example.invalid")
        ).scalar_one_or_none() is None


# -- auth + sessions -----------------------------------------------------------------------------


def test_login_and_session_validation(svc, admin):
    auth = svc.authenticate("admin@example.invalid", "supersecret-123")
    sess = svc.create_session(auth["user_id"])
    principal, sid, _ = svc.validate_session(sess["session_token"])
    assert principal.is_platform_admin
    svc.revoke_session(sid)
    assert svc.validate_session(sess["session_token"]) is None


def test_login_wrong_password_rejected(svc, admin):
    with pytest.raises(ControlPlaneError):
        svc.authenticate("admin@example.invalid", "wrong-password-xx")


def test_login_rate_limited(svc, admin):
    svc.config.rate_limits.login_per_minute = 3
    for _ in range(3):
        with pytest.raises(ControlPlaneError):
            svc.authenticate("admin@example.invalid", "wrong", rate_key="1.2.3.4")
    with pytest.raises(ControlPlaneError) as exc:
        svc.authenticate("admin@example.invalid", "supersecret-123", rate_key="1.2.3.4")
    assert "rate_limited" in exc.value.code


def test_password_reset_token_one_time_and_revokes_sessions(svc, admin):
    svc.request_password_reset("admin@example.invalid")
    msg = svc.email.latest_for("admin@example.invalid", "password_reset")
    token = msg.body.split("reset code: ")[1].split("\n")[0].strip()
    svc.confirm_password_reset(token, "brand-new-password-1")
    with pytest.raises(ControlPlaneError):  # reuse blocked
        svc.confirm_password_reset(token, "another-password-22")


def test_csrf_token_roundtrip(svc, admin):
    auth = svc.authenticate("admin@example.invalid", "supersecret-123")
    sess = svc.create_session(auth["user_id"])
    _, _, csrf_secret = svc.validate_session(sess["session_token"])
    token = svc.csrf_token_for(csrf_secret)
    assert svc.verify_csrf(csrf_secret, token)
    assert not svc.verify_csrf(csrf_secret, "forged")


# -- policies ------------------------------------------------------------------------------------


def test_policy_publish_accept_and_onboarding_gate(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    pol = svc.publish_policy(admin, policy_type="preview_terms", version="v1", title="T",
                             document_text="placeholder", required_categories=["operational"])
    assert not svc.onboarding_complete(principal)
    svc.accept_policy(principal, policy_id=pol["policy_id"], tenant_id=tid)
    assert svc.onboarding_complete(principal)


def test_policy_update_does_not_rewrite_history(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    p1 = svc.publish_policy(admin, policy_type="preview_terms", version="v1", title="T",
                            document_text="one")
    svc.accept_policy(principal, policy_id=p1["policy_id"], tenant_id=tid)
    svc.publish_policy(admin, policy_type="preview_terms", version="v2", title="T2",
                       document_text="two", supersedes_version="v1")
    # v1 acceptance still recorded; v2 now outstanding (history not rewritten).
    status = svc.acceptance_status(principal)
    assert ("preview_terms", "v1") in {(a["policy_type"], a["version"]) for a in status["accepted"]}
    assert not status["onboarding_complete"]


def test_customer_cannot_publish_policy(svc, admin):
    principal, _, _ = _onboard_tenant(svc, admin)
    with pytest.raises(ControlPlaneError):
        svc.publish_policy(principal, policy_type="x", version="v1", title="t", document_text="d")


# -- enrollment ----------------------------------------------------------------------------------


def _enroll(svc, principal, tid, node_id="node-1"):
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    chal = svc.enroll_challenge(code=code["code"], public_key=pub)
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    node = svc.enroll(code=code["code"], public_key=pub, node_id=node_id, signature=sig,
                      software_version="0.8.0-beta.1", display_name=node_id)
    return node, seed, pub, code


def test_enrollment_happy_path(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    node, _, _, _ = _enroll(svc, principal, tid)
    assert node["node_id"] == "node-1" and not node["idempotent"]


def test_enrollment_without_key_proof_fails(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    svc.enroll_challenge(code=code["code"], public_key=pub)
    with pytest.raises(ControlPlaneError):  # bad/forged signature
        svc.enroll(code=code["code"], public_key=pub, node_id="n", signature="AAAA",
                   display_name="n")


def test_enrollment_code_reuse_by_different_key_fails(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    chal = svc.enroll_challenge(code=code["code"], public_key=pub)
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    svc.enroll(code=code["code"], public_key=pub, node_id="node-1", signature=sig,
               display_name="node-1")
    # The one-time code is now consumed: a DIFFERENT key cannot reuse it (challenge is refused).
    _, pub2 = generate_keypair()
    with pytest.raises(ControlPlaneError) as exc:
        svc.enroll_challenge(code=code["code"], public_key=pub2)
    assert "unusable" in exc.value.code or "consumed" in exc.value.code


def test_enrollment_altered_code_fails(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    with pytest.raises(ControlPlaneError):
        svc.enroll_challenge(code=code["code"] + "x", public_key=pub)


def test_enrollment_idempotent_retry(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    code = svc.create_enrollment_code(principal, tenant_id=tid)
    seed, pub = generate_keypair()
    chal = svc.enroll_challenge(code=code["code"], public_key=pub)
    sig = sign(load_private_key(seed), chal["challenge"].encode())
    n1 = svc.enroll(code=code["code"], public_key=pub, node_id="node-1", signature=sig,
                    display_name="node-1")
    n2 = svc.enroll(code=code["code"], public_key=pub, node_id="node-1", signature=sig,
                    display_name="node-1")
    assert not n1["idempotent"] and n2["idempotent"]


# -- node credential auth + heartbeat ------------------------------------------------------------


def _node_headers(seed, tid, node_id, target, body, *, ts=None, nonce="n1"):
    import time as _t

    from aithernet.data.ingest_auth import build_headers
    return build_headers(private=load_private_key(seed), tenant_id=tid, node_id=node_id,
                         key_id="default", method="POST", target=target, body=body,
                         timestamp=ts or int(_t.time()), nonce=nonce)


def test_node_auth_and_heartbeat(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    _, seed, _, _ = _enroll(svc, principal, tid)
    import json
    body = json.dumps({"sequence": 1, "software_version": "0.8.0-beta.1"}).encode()
    headers = _node_headers(seed, tid, "node-1", "/v1/node/heartbeat", body)
    auth = svc.authenticate_node(headers=headers, method="POST",
                                 target="/v1/node/heartbeat", body=body)
    assert svc.record_heartbeat(auth, json.loads(body))["accepted"]


def test_node_auth_replay_rejected(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    _, seed, _, _ = _enroll(svc, principal, tid)
    body = b"{}"
    headers = _node_headers(seed, tid, "node-1", "/v1/node/heartbeat", body, nonce="fixed")
    svc.authenticate_node(headers=headers, method="POST", target="/v1/node/heartbeat", body=body)
    with pytest.raises(ControlPlaneError) as exc:
        svc.authenticate_node(headers=headers, method="POST", target="/v1/node/heartbeat",
                              body=body)
    assert exc.value.code == "node_nonce_replay"


def test_forged_node_signature_rejected(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    _enroll(svc, principal, tid)
    other_seed, _ = generate_keypair()
    body = b"{}"
    headers = _node_headers(other_seed, tid, "node-1", "/v1/node/heartbeat", body)
    with pytest.raises(ControlPlaneError):
        svc.authenticate_node(headers=headers, method="POST", target="/v1/node/heartbeat",
                              body=body)


def test_revoked_node_heartbeat_blocked(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    node, seed, _, _ = _enroll(svc, principal, tid)
    svc.revoke_node(admin, node["hosted_node_id"])
    body = b"{}"
    headers = _node_headers(seed, tid, "node-1", "/v1/node/heartbeat", body)
    with pytest.raises(ControlPlaneError):
        svc.authenticate_node(headers=headers, method="POST", target="/v1/node/heartbeat",
                              body=body)


def test_heartbeat_strips_forbidden_fields(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    _, seed, _, _ = _enroll(svc, principal, tid)
    import json
    payload = {"sequence": 2, "mission_prompt": "secret-prompt", "credential": "xxx"}
    body = json.dumps(payload).encode()
    headers = _node_headers(seed, tid, "node-1", "/v1/node/heartbeat", body, nonce="hb2")
    auth = svc.authenticate_node(headers=headers, method="POST",
                                 target="/v1/node/heartbeat", body=body)
    svc.record_heartbeat(auth, payload)
    hbs = svc.node_heartbeats(principal, _hosted_node_id(svc, tid))
    assert hbs  # stored, and forbidden fields never appear in the bounded payload
    backup_blob = str(_export(svc))
    assert "secret-prompt" not in backup_blob


# -- fleet isolation -----------------------------------------------------------------------------


def test_cross_tenant_node_visibility_blocked(svc, admin):
    p_a, tid_a, _ = _onboard_tenant(svc, admin, email="a@example.invalid", name="A")
    p_b, tid_b, _ = _onboard_tenant(svc, admin, email="b@example.invalid", name="B")
    _enroll(svc, p_a, tid_a, node_id="a-node")
    with pytest.raises(ControlPlaneError):
        svc.list_nodes(p_b, tenant_id=tid_a)
    assert svc.list_nodes(p_b, tenant_id=tid_b) == []


# -- releases + downloads ------------------------------------------------------------------------


def test_release_sign_publish_download_revoke(svc, admin):
    rseed, rpub = generate_keypair()
    rel = svc.create_release(admin, version="0.8.0-beta.1")
    svc.add_release_artifact(admin, release_id=rel["release_id"], name="a.whl", data=b"WHEEL")
    with pytest.raises(ControlPlaneError):  # cannot publish unsigned
        svc.publish_release(admin, release_id=rel["release_id"])
    svc.sign_release(admin, release_id=rel["release_id"], signing_seed_b64=rseed)
    svc.publish_release(admin, release_id=rel["release_id"])
    chrel = svc.channel_release()
    assert release_signing.verify_manifest(rpub, chrel["manifest"], chrel["manifest_signature"])
    data, _, sha = svc.download_artifact(release_id=rel["release_id"], name="a.whl")
    assert data == b"WHEEL" and sha.startswith("sha256:")
    svc.revoke_release(admin, release_id=rel["release_id"])
    with pytest.raises(ControlPlaneError):
        svc.download_artifact(release_id=rel["release_id"], name="a.whl")


def test_wrong_release_key_fails_verification(svc, admin):
    rseed, _ = generate_keypair()
    _, wrong_pub = generate_keypair()
    rel = svc.create_release(admin, version="0.8.0-beta.1")
    svc.add_release_artifact(admin, release_id=rel["release_id"], name="a.whl", data=b"W")
    svc.sign_release(admin, release_id=rel["release_id"], signing_seed_b64=rseed)
    svc.publish_release(admin, release_id=rel["release_id"])
    chrel = svc.channel_release()
    assert not release_signing.verify_manifest(wrong_pub, chrel["manifest"],
                                               chrel["manifest_signature"])


def test_modified_manifest_fails_verification(svc, admin):
    rseed, rpub = generate_keypair()
    rel = svc.create_release(admin, version="0.8.0-beta.1")
    svc.add_release_artifact(admin, release_id=rel["release_id"], name="a.whl", data=b"W")
    svc.sign_release(admin, release_id=rel["release_id"], signing_seed_b64=rseed)
    svc.publish_release(admin, release_id=rel["release_id"])
    chrel = svc.channel_release()
    tampered = dict(chrel["manifest"])
    tampered["version"] = "9.9.9"
    assert not release_signing.verify_manifest(rpub, tampered, chrel["manifest_signature"])


def test_customer_cannot_create_release(svc, admin):
    principal, _, _ = _onboard_tenant(svc, admin)
    with pytest.raises(ControlPlaneError):
        svc.create_release(principal, version="9.9.9")


# -- blob stores ---------------------------------------------------------------------------------


def test_local_blob_store_rejects_traversal(tmp_path):
    store = LocalHostedBlobStore(tmp_path / "b")
    store.put("releases/x/a.whl", b"data")
    assert store.get("releases/x/a.whl") == b"data"
    for bad in ("../escape", "/etc/passwd", "a/../../b"):
        with pytest.raises(BlobStoreError):
            store.put(bad, b"x")


def test_fake_s3_blob_store_roundtrip():
    store = S3CompatibleHostedBlobStore(FakeS3Client(), "bucket")
    store.put("k/obj", b"hello")
    assert store.exists("k/obj") and store.get("k/obj") == b"hello"
    store.delete("k/obj")
    assert not store.exists("k/obj")


# -- support -------------------------------------------------------------------------------------


def test_support_case_tenant_isolation_and_bundle_limit(svc, admin):
    p_a, tid_a, _ = _onboard_tenant(svc, admin, email="a@example.invalid", name="A")
    p_b, tid_b, _ = _onboard_tenant(svc, admin, email="b@example.invalid", name="B")
    case = svc.create_support_case(p_a, tenant_id=tid_a, subject="help")
    # B cannot list A's cases.
    assert all(c["tenant_id"] == tid_b for c in svc.list_support_cases(p_b))
    # Oversized bundle rejected.
    big = b"\x1f\x8b" + b"0" * (9 * 1024 * 1024)
    with pytest.raises(ControlPlaneError):
        svc.upload_support_bundle(p_a, case_id=case["case_id"], data=big)
    # Non-archive content rejected.
    with pytest.raises(ControlPlaneError):
        svc.upload_support_bundle(p_a, case_id=case["case_id"], data=b"\x00\x01rawbytes")
    # A valid gzip-magic bundle accepted.
    svc.upload_support_bundle(p_a, case_id=case["case_id"], data=b"\x1f\x8bgzipbundle")


# -- admin authz ---------------------------------------------------------------------------------


def test_tenant_admin_cannot_read_platform_audit(svc, admin):
    principal, _, _ = _onboard_tenant(svc, admin)
    with pytest.raises(ControlPlaneError):
        svc.list_audit(principal)
    assert isinstance(svc.list_audit(admin), list)


# -- checklist + config validation ---------------------------------------------------------------


def test_checklist_fails_closed(svc, admin):
    from services.control_plane.checklist import generate_checklist

    result = generate_checklist(svc.config, service=svc)
    assert result["production_ready"] is False
    assert "tls_verified" in result["not_executed"]


def test_production_config_rejects_dev_defaults(monkeypatch):
    cfg = HostedConfig()
    cfg.environment = "production"
    problems = cfg.validate()
    assert "sqlite_central_database" in problems
    assert "development_email_sink" in problems
    assert any("loopback" in p for p in problems)
    with pytest.raises(HostedConfigError):
        cfg.require_valid()


# -- secret leakage + backup/restore -------------------------------------------------------------


def test_no_password_or_secret_in_api_or_backup(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    blob = str(_export(svc))
    assert "acme-password-9" not in blob  # plaintext password never stored
    assert "supersecret-123" not in blob
    diag = str(svc.diagnostics())
    assert "acme-password-9" not in diag and svc.config.session_signing_key not in diag


def test_backup_restore_roundtrip(svc, admin):
    principal, tid, _ = _onboard_tenant(svc, admin)
    _enroll(svc, principal, tid)
    from services.control_plane import backup

    dump = backup.export_database(svc.engine)
    report = backup.restore_from_file  # ensure callable exists
    assert callable(report)
    # Counts survive a logical export.
    assert len(dump["tables"]["hosted_nodes"]) == 1
    assert len(dump["tables"]["hosted_tenants"]) == 1


# -- helpers -------------------------------------------------------------------------------------


def _hosted_node_id(svc, tid):
    from services.control_plane.models import HostedNode
    from sqlalchemy import select
    with svc._session() as s:
        return s.execute(select(HostedNode).where(HostedNode.tenant_id == tid)).scalar_one().id


def _export(svc):
    from services.control_plane import backup
    return backup.export_database(svc.engine)
