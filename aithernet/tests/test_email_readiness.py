"""Production SMTP readiness tests (Phase 4).

Covers the email config env loading (From / Reply-To / timeout), the SMTP provider's Reply-To +
timeout wiring, and the service-level ``email_test`` diagnostic — including failure classification
and the guarantee that no credentials leak into the bounded result.
"""

from __future__ import annotations

import smtplib

import pytest

from services.control_plane.config import HostedConfig
from services.control_plane.email import SMTPEmailProvider, build_email_provider
from services.control_plane.service import ControlPlaneService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_SESSION_KEY", "x" * 48)
    cfg = HostedConfig()
    cfg.database_url = f"sqlite:///{tmp_path / 'hosted.db'}"
    cfg.object_storage.local_root = str(tmp_path / "blobs")
    return ControlPlaneService(cfg)


def test_email_config_loads_from_reply_and_timeout(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_EMAIL_FROM", "aithernet@aithernet.online")
    monkeypatch.setenv("AITHERNET_HOSTED_EMAIL_REPLY_TO", "adityapawar@aithernet.online")
    monkeypatch.setenv("AITHERNET_HOSTED_EMAIL_TIMEOUT", "12")
    cfg = HostedConfig.from_env()
    assert cfg.email.from_address == "aithernet@aithernet.online"
    assert cfg.email.reply_to == "adityapawar@aithernet.online"
    assert cfg.email.timeout_seconds == 12


def test_email_timeout_malformed_keeps_default(monkeypatch):
    monkeypatch.setenv("AITHERNET_HOSTED_EMAIL_TIMEOUT", "not-an-int")
    cfg = HostedConfig.from_env()
    assert cfg.email.timeout_seconds == 30


def test_smtp_provider_sets_reply_to_and_timeout(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"], captured["port"], captured["timeout"] = host, port, timeout
        def starttls(self): captured["starttls"] = True
        def login(self, u, p): captured["login"] = (u, p)
        def send_message(self, mime): captured["mime"] = mime
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    p = SMTPEmailProvider("smtp://user:pass@smtp.example.invalid:587",
                          "from@aithernet.online", reply_to="reply@aithernet.online", timeout=9)
    from services.control_plane.email import EmailMessage
    p.send(EmailMessage(to="dest@example.invalid", subject="s", body="b"))
    assert captured["timeout"] == 9
    assert captured["starttls"] is True
    assert captured["mime"]["Reply-To"] == "reply@aithernet.online"
    assert captured["mime"]["From"] == "from@aithernet.online"
    assert p.sent == 1


def test_smtp_provider_unquotes_email_username_and_password(monkeypatch):
    """A Zoho-style email username + special-char password are percent-encoded in the URL and
    MUST be decoded before LOGIN, or authentication fails."""
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): pass
        def starttls(self): pass
        def login(self, u, p): captured["login"] = (u, p)
        def send_message(self, mime): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    # username adityapawar@aithernet.online and password with @ and / both percent-encoded
    p = SMTPEmailProvider(
        "smtp://adityapawar%40aithernet.online:p%40ss%2Fword@smtp.zoho.com:587",
        "aithernet@aithernet.online")
    from services.control_plane.email import EmailMessage
    p.send(EmailMessage(to="dest@example.invalid", subject="s", body="b"))
    assert captured["login"] == ("adityapawar@aithernet.online", "p@ss/word")


def test_build_provider_development_when_not_smtp():
    cfg = HostedConfig()  # provider defaults to development
    prov = build_email_provider(cfg)
    assert type(prov).__name__ == "DevelopmentEmailSink"


def test_service_email_test_development_sink_captures_not_delivers(svc):
    result = svc.email_test(to="customer@example.invalid")
    assert result["provider"] == "development"
    assert result["ok"] is True
    assert result["outcome"] == "sent"
    assert "NOT delivered" in result["note"]
    # captured in the in-memory sink, addressed to the intended recipient
    assert svc.email.latest_for("customer@example.invalid", kind="email_test") is not None


def test_service_email_test_classifies_auth_failure(svc, monkeypatch):
    def boom(_msg):
        raise smtplib.SMTPAuthenticationError(535, b"auth failed")
    monkeypatch.setattr(svc.email, "send", boom)
    result = svc.email_test(to="dest@example.invalid")
    assert result["ok"] is False
    assert result["outcome"] == "auth_failed"
    # the bounded result must never leak credentials or raw exception payloads
    blob = repr(result).lower()
    for forbidden in ("password", "pass@", "535", "secret", "token"):
        assert forbidden not in blob


def test_service_email_test_classifies_connect_failure(svc, monkeypatch):
    def boom(_msg):
        raise ConnectionRefusedError("connection refused")
    monkeypatch.setattr(svc.email, "send", boom)
    result = svc.email_test(to="dest@example.invalid")
    assert result["ok"] is False
    assert result["outcome"] == "connect_failed"


def test_service_email_test_records_audit(svc):
    svc.email_test(to="audit@example.invalid")
    # the system audit entry is retrievable by a platform admin
    admin = svc.bootstrap_admin("admin@example.invalid", "pw-correct-horse-battery")
    from services.control_plane.service import Principal
    principal = Principal(user_id=admin["user_id"], email=admin["email"], is_platform_admin=True)
    actions = [r.get("action") for r in svc.list_audit(principal, limit=50)]
    assert "email.test" in actions
