"""Server-side Google OAuth for the OWNER Drive archive connection (admin/owner only).

This is the hosted, control-plane counterpart of the client's file-based
``aithernet.data.destinations.google_drive_client.GoogleDriveOAuth``. It exists so the product
OWNER/ADMIN can connect Google Drive from the admin portal with a normal OAuth consent flow —
WITHOUT anyone pasting a refresh token, and WITHOUT shipping any Google credential to clients.

Design + security:
- The Google OAuth **client id + secret** live ONLY in server configuration (``GOOGLE_OAUTH_*``
  env). The client secret is read at use time and is NEVER logged, returned to a browser, or
  written to disk. The authorization-code exchange and every token refresh happen server-side.
- Least-privilege scope ``drive.file``: the app can see/manage ONLY the files/folders it creates
  (the "Aithernet Research Archive" tree), never the owner's other Drive content.
- The obtained **refresh token** is handed back to the caller once (the service persists it in the
  server-side ``research_owner_archive_connections`` row); it is never returned by any status API.
- No heavy Google SDK: the token + Drive REST endpoints are called over ``httpx`` (already a core
  dependency). Errors are mapped to bounded categories; response bodies (which can echo secrets or
  codes) are never included in raised errors or logs.

``ServerDriveOAuth`` implements the tiny ``access_token()`` surface that the existing
``aithernet.data.destinations.google_drive_client.RealGoogleDriveClient`` needs, so the archive
worker reuses the shipped Drive REST client unchanged (no client-package change required).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import httpx


class OwnerDriveOAuthError(Exception):
    """A bounded, sanitized OAuth failure — a category only (never a secret, code, or body)."""

    def __init__(self, category: str, message: str = "") -> None:
        super().__init__(message or category)
        self.category = category


@dataclass(frozen=True)
class OwnerDriveOAuthClient:
    """Resolved server-side OAuth client values (secret included; never serialized or logged)."""

    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    auth_uri: str
    token_uri: str
    archive_root_name: str


def resolve_client(config) -> OwnerDriveOAuthClient | None:
    """Build the OAuth client from ``HostedConfig``, or ``None`` when not fully configured.

    Not configured (missing client id, secret, or redirect URI) is a normal, non-error state: the
    admin portal renders a "Google Drive OAuth is not configured on the server" message instead."""
    if not config.google_oauth_configured:
        return None
    go = config.google_oauth
    secret = config.google_oauth_client_secret
    if not (go.client_id and secret and go.redirect_uri):
        return None
    return OwnerDriveOAuthClient(
        client_id=go.client_id, client_secret=secret, redirect_uri=go.redirect_uri,
        scope=go.scope, auth_uri=go.auth_uri, token_uri=go.token_uri,
        archive_root_name=go.archive_root_name)


def build_authorize_url(client: OwnerDriveOAuthClient, *, state: str,
                        login_hint: str | None = None) -> str:
    """The Google consent URL. ``access_type=offline`` + ``prompt=consent`` so Google returns a
    refresh token; ``state`` is the CSRF nonce bound to the admin session."""
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "scope": client.scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return client.auth_uri + "?" + urllib.parse.urlencode(params)


def exchange_code(client: OwnerDriveOAuthClient, *, code: str,
                  http: httpx.Client | None = None) -> dict:
    """Exchange an authorization code for tokens server-side. Returns a dict with ``refresh_token``
    (and a short-lived access token). Raises :class:`OwnerDriveOAuthError` on failure — never a
    Google response body."""
    if not code:
        raise OwnerDriveOAuthError("missing_code", "no authorization code")
    owns = http is None
    http = http or httpx.Client(timeout=30)
    try:
        resp = http.post(client.token_uri, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": client.client_id, "client_secret": client.client_secret,
            "redirect_uri": client.redirect_uri,
        })
    except httpx.HTTPError as exc:  # network error — no secret in the message
        raise OwnerDriveOAuthError("token_endpoint_unreachable",
                                   "could not reach the Google token endpoint") from exc
    finally:
        if owns:
            http.close()
    if resp.status_code != 200:
        # The body may echo the code/secret — never surface it.
        raise OwnerDriveOAuthError("token_exchange_failed",
                                   "the authorization code could not be exchanged")
    tok = resp.json()
    if not tok.get("refresh_token"):
        # Happens when the account previously granted consent and Google withholds a new refresh
        # token. prompt=consent avoids this; surface a reconnect-with-consent hint.
        raise OwnerDriveOAuthError("no_refresh_token",
                                   "Google did not return a refresh token")
    return {
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token", ""),
        "expires_in": int(tok.get("expires_in", 3000)),
        "scope": tok.get("scope", client.scope),
    }


class ServerDriveOAuth:
    """Minimal ``access_token()`` provider backed by a server-side refresh token.

    Passed to the shipped ``RealGoogleDriveClient`` so the archive worker reuses the client Drive
    REST code without modification. A revoked/expired refresh token raises
    ``OwnerDriveOAuthError('expired_credentials')`` so the worker can mark the connection degraded
    and prompt the owner to reconnect."""

    def __init__(self, client: OwnerDriveOAuthClient, refresh_token: str, *,
                 http: httpx.Client | None = None) -> None:
        self._client = client
        self._refresh = refresh_token
        self._http = http
        self._access: tuple[str, float] | None = None

    def access_token(self) -> str:
        import time
        if self._access and self._access[1] > time.time():
            return self._access[0]
        http = self._http or httpx.Client(timeout=30)
        owns = self._http is None
        try:
            resp = http.post(self._client.token_uri, data={
                "grant_type": "refresh_token", "refresh_token": self._refresh,
                "client_id": self._client.client_id, "client_secret": self._client.client_secret,
            })
        except httpx.HTTPError as exc:
            raise OwnerDriveOAuthError("token_endpoint_unreachable",
                                       "could not reach the Google token endpoint") from exc
        finally:
            if owns:
                http.close()
        if resp.status_code in (400, 401):
            # invalid_grant: the refresh token was revoked or expired -> reconnect required.
            raise OwnerDriveOAuthError("expired_credentials",
                                       "the owner Drive credentials were revoked or expired")
        if resp.status_code != 200:
            raise OwnerDriveOAuthError("token_refresh_failed",
                                       "could not refresh the owner Drive access token")
        tok = resp.json()
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3000)) - 60)
        return self._access[0]


def fetch_account_email(access_token: str, *, http: httpx.Client | None = None) -> str | None:
    """Best-effort owner account email via the Drive ``about`` endpoint (works with ``drive.file``).

    Returns None on any failure — the email is a nicety for the admin UI, never required. No secret
    is logged; the access token is sent only in the Authorization header."""
    owns = http is None
    http = http or httpx.Client(timeout=15)
    try:
        resp = http.get("https://www.googleapis.com/drive/v3/about",
                        params={"fields": "user"},
                        headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code != 200:
            return None
        user = (resp.json() or {}).get("user") or {}
        email = user.get("emailAddress")
        return str(email)[:120] if email else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns:
            http.close()
