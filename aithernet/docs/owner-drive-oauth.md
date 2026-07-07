# Owner Google Drive — admin OAuth connection (server-side)

The Aithernet hosted **owner archive** collects sanitized research packages that enrolled,
consenting nodes upload to hosted ingestion. Accepted packages are written into the **owner's**
Google Drive by a server-side archive worker. This document describes how the product owner/admin
connects that Drive from the admin portal.

This is a **server/portal-only** capability. It does **not** change the client. Clients never use
Google Drive, never hold Google credentials, never see `oauth-client.json`, and take no action if
Drive is disconnected — their uploads keep succeeding and archive jobs simply queue server-side.

## What the admin does (normal path — no token pasting)

1. Open the admin portal → **Research** → *Owner Drive connection*.
2. Click **Connect Google Drive** (optionally enter an account hint/email).
3. Approve access on Google's consent screen.
4. Google redirects back; the portal shows **connected**. Queued archive jobs then sync
   automatically for **all** consenting/enrolled nodes — there is no per-node or per-mission Drive
   setup, and no need to click *Process archive jobs* in steady state (that button is manual
   retry/testing only).
5. To stop, click **Disconnect Drive**. The server forgets the refresh token entirely.

If Google's token is later revoked or expires, the archive worker flags the connection
(`error` / `reconnect_required`); the admin reconnects once. Clients are never affected.

## OAuth flow (what happens on the server)

- `POST /v1/admin/research/drive/oauth/start` (admin-only, CSRF-guarded) mints a single-use
  **state** nonce bound to the admin session and returns the Google authorization URL. The browser
  navigates to it.
- Google redirects to
  `GET /v1/admin/research/drive/oauth/callback?code=…&state=…` on the admin origin.
- The callback validates the state (exists, unconsumed, unexpired, and belongs to the current admin
  session — this is the CSRF / login-CSRF defence for the round-trip), then exchanges the code
  **server-side** for a refresh token, verifies Drive access, ensures the archive root folder, and
  stores the refresh token **server-side only**. It never returns a token to the browser.
- On success the browser is redirected back to `…/admin/research?drive=connected`.

## Scope (least privilege)

Aithernet requests only **`https://www.googleapis.com/auth/drive.file`**. With this scope the app
can see and manage **only the files and folders it creates** — the *Aithernet Research Archive*
tree — and nothing else in the owner's Drive. A pre-existing arbitrary root folder is not required;
the app creates and owns its own tree. (Supporting an arbitrary existing root would require a
broader Drive scope, which this design intentionally avoids.)

## Archive folder structure

```
Aithernet Research Archive/         (created + owned by the app; name overridable)
  tenants/
    <tenant_id>/
      nodes/
        <node_id>/
          packages/                 (one file per accepted package: <sha256>.bin)
```

One owner Drive connection serves every tenant and node. Uploads are idempotent by package SHA-256
(a retry resolves to the same file). Quarantined packages (those that failed the server-side secret
scan) are **never** written to Drive — only bounded quarantine metadata (category + short detail) is
retained; the secret itself is never stored or surfaced.

## Server configuration (operator)

Set these in the hosted server environment (e.g. the production env file). **Never** commit or print
their values; the control plane reads them at use time and never logs them.

| Variable | Required | Meaning |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | yes | Google OAuth **Web application** client id (public; embedded in the consent URL). |
| `GOOGLE_OAUTH_CLIENT_SECRET` | yes | OAuth client **secret** (server-side only; never sent to the browser). |
| `GOOGLE_OAUTH_REDIRECT_URI` | yes | The exact callback URL registered in Google Cloud (see below). |
| `GOOGLE_OAUTH_ARCHIVE_ROOT_NAME` | no | Archive root folder name (default `Aithernet Research Archive`). |
| `AITHERNET_ADMIN_BASE_URL` | optional | Admin origin for the post-callback redirect back to the SPA (e.g. `https://admin.aithernet.online`). When unset, the callback derives it from the request's own admin Host, so it still lands correctly. |

When the three required values are absent, the admin portal shows **"Google Drive OAuth is not
configured on the server"** with this same guidance (never a stack trace, never a secret).

### Register the redirect URI in Google Cloud

In Google Cloud Console → *APIs & Services → Credentials*, create an **OAuth client ID** of type
**Web application** and add an **Authorized redirect URI** that matches `GOOGLE_OAUTH_REDIRECT_URI`
exactly. For the standard Cloudflare deployment the admin origin proxies `/api/*` to the control
plane, so the value is:

```
https://admin.aithernet.online/api/v1/admin/research/drive/oauth/callback
```

Enable the **Google Drive API** for the project. The admin origin is protected by Cloudflare Access;
the admin's browser carries its Access + session cookies through Google's redirect, so the callback
resolves for the logged-in admin.

## Security properties

- The refresh/access token and client secret are **server-side only** — never returned by any API,
  never logged, never placed in portal HTML, never in the client bundle or release artifacts.
- Status APIs expose only: connected/disconnected/error/unconfigured, account label/email, root
  folder id/name, scope, last sync, a bounded error category, and job counts.
- Admin Drive routes require platform-admin authorization server-side (not route hiding). Normal
  clients cannot reach them. State-changing routes require CSRF; the OAuth round-trip is protected
  by the state nonce bound to the admin session.
- The advanced "connect with a refresh token" form exists only for out-of-band provisioning; the
  token is write-only and never displayed back.
