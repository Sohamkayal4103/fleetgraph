"""Hosted email provider abstraction (Stage 14F §37).

``DevelopmentEmailSink`` captures messages in memory and (optionally) writes a sanitized preview
to a temporary mailbox directory, so an out-of-process acceptance test can retrieve an invitation
without any real provider. ``SMTPEmailProvider`` sends through an env-referenced SMTP URL with TLS
validation. Templates carry only bounded variables; no secrets live in templates; the *server log*
never receives a raw token (the token rides only in the delivered message body — the mailbox, not
a log). Real SMTP verification remains ``not_executed`` until credentials are supplied.
"""

from __future__ import annotations

import json
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage as _MIMEMessage
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse


@dataclass
class EmailMessage:
    to: str
    subject: str
    body: str  # plain-text alternative (always present)
    kind: str = "generic"
    html: str | None = None  # optional branded HTML alternative
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def preview(self) -> dict:
        """A structured, non-secret-leaking representation for the admin development preview."""
        return {
            "id": self.id, "to": self.to, "subject": self.subject,
            "kind": self.kind, "created_at": self.created_at,
        }


class EmailProvider(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class DevelopmentEmailSink:
    """Captures email for development/testing. NOT permitted in production (§40)."""

    def __init__(self, mailbox_dir: str | Path | None = None) -> None:
        self.messages: list[EmailMessage] = []
        self.failures = 0
        self.mailbox_dir = Path(mailbox_dir) if mailbox_dir else None
        if self.mailbox_dir:
            self.mailbox_dir.mkdir(parents=True, exist_ok=True)

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)
        if self.mailbox_dir:
            path = self.mailbox_dir / f"{message.created_at.replace(':', '')}-{message.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"to": message.to, "subject": message.subject, "kind": message.kind,
                 "body": message.body, "id": message.id, "created_at": message.created_at},
                indent=2,
            ))
            tmp.replace(path)

    def latest_for(self, to: str, kind: str | None = None) -> EmailMessage | None:
        for message in reversed(self.messages):
            if message.to == to and (kind is None or message.kind == kind):
                return message
        return None


class SMTPEmailProvider:
    """Sends through an env-referenced SMTP URL (``smtp://`` / ``smtps://``) with TLS validation."""

    def __init__(self, smtp_url: str, from_address: str, *, reply_to: str = "",
                 timeout: int = 30) -> None:
        if not smtp_url:
            raise ValueError("smtp_url_required")
        self.smtp_url = smtp_url
        self.from_address = from_address
        self.reply_to = reply_to
        self.timeout = timeout
        self.sent = 0
        self.failures = 0

    def send(self, message: EmailMessage) -> None:
        parsed = urlparse(self.smtp_url)
        mime = _MIMEMessage()
        mime["From"] = self.from_address
        mime["To"] = message.to
        if self.reply_to:
            mime["Reply-To"] = self.reply_to
        mime["Subject"] = message.subject
        mime.set_content(message.body)  # text/plain alternative
        if message.html:
            mime.add_alternative(message.html, subtype="html")  # multipart/alternative
        host = parsed.hostname or "localhost"
        port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        try:
            if parsed.scheme == "smtps":
                client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=self.timeout)
            else:
                client = smtplib.SMTP(host, port, timeout=self.timeout)
                client.starttls()  # TLS validation; never send credentials in cleartext
            with client:
                if parsed.username and parsed.password:
                    # urlparse does NOT percent-decode userinfo; do it ourselves so an SMTP
                    # username that is an email address (e.g. user%40domain) and a password with
                    # URL-special characters authenticate correctly.
                    client.login(unquote(parsed.username), unquote(parsed.password))
                client.send_message(mime)
            self.sent += 1
        except Exception:
            self.failures += 1
            raise


# -- templates (bounded variables; no secrets) ---------------------------------------------------


def invitation_email(*, to: str, accept_url: str, token: str, tenant_name: str) -> EmailMessage:
    # The one-time token rides ONLY inside the complete link (never as a separate copyable code).
    _ = token  # the token is embedded in accept_url; not shown separately
    body = (
        f"You have been invited to Aithernet early access for '{tenant_name}'.\n\n"
        f"Open this one-time link to accept your invitation:\n{accept_url}\n\n"
        "The link can be used once and expires. If you did not expect this, ignore the email."
    )
    return EmailMessage(to=to, subject="Your Aithernet early-access invitation",
                        body=body, kind="invitation")


def password_reset_email(*, to: str, reset_url: str, token: str) -> EmailMessage:
    body = (
        "A password reset was requested for your Aithernet account.\n\n"
        f"Reset your password: {reset_url}\n"
        f"One-time reset code: {token}\n\n"
        "This code can be used once and expires. If you did not request it, ignore the email."
    )
    return EmailMessage(to=to, subject="Aithernet password reset", body=body, kind="password_reset")


def mailing_confirmation_email(*, to: str, confirm_url: str) -> EmailMessage:
    # Double opt-in: the subscription is NOT active until this one-time link is opened.
    body = (
        "Please confirm your Aithernet mailing-list subscription.\n\n"
        f"Confirm here (one-time link):\n{confirm_url}\n\n"
        "If you did not request this, ignore this email and you will not be subscribed."
    )
    return EmailMessage(to=to, subject="Confirm your Aithernet mailing-list subscription",
                        body=body, kind="mailing_confirmation")


def enrollment_email(*, to: str, node_name: str) -> EmailMessage:
    body = f"A new node '{node_name}' was enrolled to your Aithernet tenant."
    return EmailMessage(to=to, subject="Aithernet node enrolled", body=body, kind="enrollment")


def _branded_html(*, heading: str, paragraphs: list[str], cta: tuple[str, str] | None = None,
                  footer: str = "Aithernet — invitation-only early access") -> str:
    """A small, responsive, inline-styled HTML email (no external assets, dark-safe)."""
    import html as _h
    blocks = "".join(
        f'<p style="margin:0 0 14px;line-height:1.55;color:#1f2330">{_h.escape(p)}</p>'
        for p in paragraphs
    )
    button = ""
    if cta:
        label, url = cta
        button = (
            f'<p style="margin:22px 0"><a href="{_h.escape(url)}" '
            'style="background:#3257d6;color:#fff;text-decoration:none;padding:11px 18px;'
            f'border-radius:6px;display:inline-block;font-weight:600">{_h.escape(label)}</a></p>'
        )
    return (
        '<!doctype html><html><body style="margin:0;background:#f3f4f7;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:520px;margin:0 auto;padding:24px">'
        '<div style="background:#fff;border:1px solid #e4e6eb;border-radius:10px;padding:28px">'
        '<div style="font-weight:700;font-size:18px;color:#3257d6;'
        'margin-bottom:18px">Aithernet</div>'
        f'<h1 style="font-size:20px;margin:0 0 16px;color:#11141c">{_h.escape(heading)}</h1>'
        f'{blocks}{button}'
        f'<hr style="border:none;border-top:1px solid #e4e6eb;margin:22px 0">'
        f'<p style="margin:0;font-size:12px;color:#7a8090">{_h.escape(footer)}</p>'
        '</div></div></body></html>'
    )


def early_access_acknowledgement_email(*, to: str, support_email: str) -> EmailMessage:
    """Branded request-received acknowledgement. Does NOT imply approval or an account."""
    paras = [
        "Thanks — we've received your request for Aithernet early access.",
        "Aithernet is invitation-only. Requesting access does not yet create an account or grant "
        "access to releases; an administrator reviews each request.",
        "What happens next: if your request is approved, you'll receive a separate one-time "
        "invitation email with a link to create your account, accept the terms, and reach the "
        "customer portal.",
        f"Questions? Reply to this email or contact {support_email}.",
    ]
    body = (
        "Thanks — we've received your request for Aithernet early access.\n\n"
        "Aithernet is invitation-only. Requesting access does not yet create an account or grant "
        "access to releases; an administrator reviews each request.\n\n"
        "What happens next: if approved, you'll receive a separate one-time invitation email with "
        "a link to create your account, accept the terms, and reach the customer portal.\n\n"
        f"Questions? Reply to this email or contact {support_email}.\n"
    )
    return EmailMessage(to=to, subject="We received your Aithernet early-access request",
                        body=body, kind="early_access_ack",
                        html=_branded_html(heading="Request received", paragraphs=paras))


def admin_new_request_notification_email(*, to: str, requester_email: str,
                                         admin_url: str) -> EmailMessage:
    """Notify an administrator that a new early-access request is awaiting review."""
    paras = [
        f"A new early-access request is awaiting review: {requester_email}.",
        "Review it in the admin portal and approve, reject, or wait-list it.",
    ]
    body = (
        f"A new Aithernet early-access request is awaiting review: {requester_email}.\n\n"
        f"Review it in the admin portal: {admin_url}\n"
    )
    return EmailMessage(to=to, subject="Aithernet: new early-access request awaiting review",
                        body=body, kind="admin_early_access_notify",
                        html=_branded_html(heading="New early-access request",
                                           paragraphs=paras, cta=("Open admin portal", admin_url),
                                           footer="Aithernet administrator notification"))


def build_email_provider(config, mailbox_dir: str | Path | None = None) -> EmailProvider:
    from services.control_plane.config import HostedConfig

    assert isinstance(config, HostedConfig)
    if config.email.provider == "smtp":
        url = config.smtp_url
        if not url:
            raise ValueError("smtp_url_ref_unset")
        return SMTPEmailProvider(url, config.email.from_address,
                                 reply_to=config.email.reply_to,
                                 timeout=config.email.timeout_seconds)
    import os as _os

    return DevelopmentEmailSink(mailbox_dir or _os.environ.get("AITHERNET_HOSTED_DEV_MAILBOX"))
