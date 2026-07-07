# Aithernet — production email (SMTP) readiness

The hosted control plane sends transactional email: invitations, password resets,
and (future) request-access verification + support notifications. In production
the development email sink is **rejected** — `AITHERNET_HOSTED_ENV=production`
fails closed if `AITHERNET_HOSTED_EMAIL=development`.

> Status: the SMTP provider and a production-safe `email-test` diagnostic are
> **implemented and unit-tested**. Real end-to-end delivery is **not tested**
> until an operator supplies a working SMTP credential and runs the test below.
> Do not claim SMTP works until a real message is sent and received.

## Configuration (operator env file, OUTSIDE git, mode 0600)

| Variable | Meaning | Example (placeholder) |
| --- | --- | --- |
| `AITHERNET_HOSTED_EMAIL` | provider — must be `smtp` in production | `smtp` |
| `AITHERNET_HOSTED_SMTP_URL` | full SMTP URL incl. host, port, app credential | `smtp://USER:APP_PASSWORD@smtp.zoho.com:587` |
| `AITHERNET_HOSTED_EMAIL_FROM` | From address | `aithernet@aithernet.online` |
| `AITHERNET_HOSTED_EMAIL_REPLY_TO` | Reply-To (optional) | `adityapawar@aithernet.online` |
| `AITHERNET_HOSTED_EMAIL_TIMEOUT` | per-operation timeout, seconds (optional) | `30` |

- **TLS mode** comes from the URL scheme: `smtp://` uses STARTTLS on the given
  port (typically 587); `smtps://` uses implicit TLS (typically 465). Credentials
  are never sent in cleartext — STARTTLS is negotiated before `AUTH`.
- The **credential is a secret**: store it only in the env file (0600), never in
  git, never in chat, never in logs. The SMTP URL value is read at runtime from
  the env var named by `AITHERNET_HOSTED_SMTP_URL`.
- **Bounded retry**: a send raises on failure (it is not silently dropped); the
  caller decides on retry. Invitations/resets can be re-issued by the operator.

### Zoho specifics (the configured mailbox provider)
- Use a **dedicated application password** (Zoho → Security → App Passwords), not
  the mailbox login password.
- Confirm the Zoho plan permits SMTP. Typical host/port: `smtp.zoho.com:587`
  (STARTTLS) or `smtp.zoho.com:465` (SSL). The `From` should match a verified
  Zoho sender (`aithernet@aithernet.online` or `adityapawar@aithernet.online`).
- SPF/DKIM/DMARC must be valid for `aithernet.online` (operator-managed in
  Cloudflare DNS) or mail will land in spam / be rejected.

## Verify delivery (the only proof that counts)

After filling the env file on the production host:

```
# Validate config first (fails closed on the dev sink / placeholders):
deploy/production/check.sh --env-file /etc/aithernet/hosted.env

# Send ONE bounded test message to a mailbox you control:
docker compose -p aithernet_prod \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.cloudflare.yml \
  --env-file /etc/aithernet/hosted.env \
  run --rm control-plane \
  python -m services.control_plane.cli email-test --to you@yourdomain.example
```

`email-test`:
- sends to **only** the `--to` address;
- prints **no** credentials;
- reports a distinct `outcome`: `sent`, `auth_failed`, `sender_refused`,
  `recipient_refused`, `connect_failed`, or `timeout`;
- records an `email.test` audit entry;
- exits non-zero on failure (and exit code 2 if still on the development sink).

Delivery is **proven** only when: `outcome: sent`, the message arrives in the
destination inbox, and a reply path works. Record the result honestly
(`outcome` + whether the human received it) — not merely that the command ran.

## Failure triage

| `outcome` | Likely cause |
| --- | --- |
| `connect_failed` | wrong host/port, blocked egress, DNS failure |
| `auth_failed` | wrong username/app-password, SMTP not enabled on the plan |
| `sender_refused` | `From` not a verified/allowed sender for the account |
| `recipient_refused` | destination rejected (typo / blocklist) |
| `timeout` | network egress blocked or server slow; raise `*_EMAIL_TIMEOUT` |

This document contains placeholders only — no real credentials.
