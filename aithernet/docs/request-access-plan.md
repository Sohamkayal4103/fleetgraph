# Request-access workflow — implementation plan (Phase 5)

**Status: PLAN ONLY. Not implemented.** The public site and portal remain
invitation-only (access requests go to `adityapawar@aithernet.online`) until this
workflow is implemented, tested, and SMTP delivery is proven. This document is the
spec for a separate, reviewed commit — it must not be half-shipped as a weak
browser-only form.

## 0. Why a plan, not code now

Self-service request-access is the largest remaining full-stack feature: a public
unauthenticated write path, email verification, abuse resistance, an admin review
queue, and an approval bridge into the existing invitation subsystem. Mixing it
into the production-deployment commit would be unreviewable and would put an
untested public endpoint on the Internet. It depends on **proven production SMTP**
(see `deploy/production/README.smtp.md`), which is not yet verified.

## 1. Reuse, don't rebuild

| Concern | Reuse |
| --- | --- |
| Abuse limits | `ControlPlaneService._check_rate(s, bucket, key, limit_per_minute)` + a new `RateLimits.request_access_per_minute` |
| Token storage | `security.token_digest(token)` — store ONLY the digest, show the raw token once (in the verification link) |
| Email | `self.email.send(...)` + a new `request_access_verification_email(...)` template (bounded variables, no secrets) |
| Approval → onboarding | the existing `create_invitation(...)` path (atomic invite + canonical `#/accept-invite?token=` link) |
| Audit | `self._audit(s, action="request_access.*", ...)` |
| CSRF / auth | public submit is unauthenticated POST; admin queue/approve/reject use `csrf_guarded` + `require_platform_admin` |

## 2. Data model — `HostedAccessRequest`

| Column | Notes |
| --- | --- |
| `id` | uuid |
| `email` | normalized (lowercased, trimmed) via the existing `_norm_email` |
| `status` | `pending_verification` → `verified` → (`approved` \| `rejected`) ; plus `expired`, `consumed` |
| `verify_token_digest` | digest only; raw token rides only in the verification link |
| `verify_expires_at` | e.g. 24h |
| `verified_at` | set when the email link is opened |
| `note` | optional, bounded free text from the requester (length-capped, escaped) |
| `requested_context` | optional bounded JSON (e.g. intended use); size-capped |
| `created_at`, `updated_at` | clock-injected |
| `reviewed_by_user_id`, `reviewed_at`, `decision_reason` | admin decision metadata |
| `invitation_id` | set when approval creates an invitation |

Unique partial index on `email` for non-terminal statuses to make duplicate
handling deterministic.

## 3. Endpoints

### Public (unauthenticated, rate-limited, enumeration-safe)
- `POST /v1/access-requests` — body `{email, note?}`.
  - Validate + normalize email (reject malformed; cap `note`).
  - Rate limit by **client IP** and by **email** (two buckets).
  - Upsert: if a non-terminal request exists for the email, do **not** reveal it;
    (optionally) resend verification within a cooldown.
  - Always return the **same** bounded response regardless of whether the email
    is new, pending, already a customer, or rate-limited:
    `202 {"status": "ok", "message": "If eligible, a verification email was sent."}`
    → no account/request enumeration.
  - Send the verification email (raw token in the link only).
  - Audit `request_access.submit` (store a hash of IP, not raw, if retained).
- `POST /v1/access-requests/verify` — body `{token}` (or `GET` with token then a
  POST confirm to avoid prefetch-consumption).
  - Look up by `token_digest`; check not expired/consumed.
  - Mark `verified`; audit `request_access.verify`.
  - Bounded response; never reveal the email or internal state.

### Admin (session + CSRF + platform-admin)
- `GET /v1/admin/access-requests?status=verified` — paginated queue (bounded fields).
- `POST /v1/admin/access-requests/{id}/approve` — body `{role?, tenant?}`.
  - Transactionally: mark `approved`, call `create_invitation(...)`, store
    `invitation_id`, audit `request_access.approve`. Invitation email delivers the
    canonical `#/accept-invite?token=` link.
- `POST /v1/admin/access-requests/{id}/reject` — body `{reason?}`; mark `rejected`,
  audit `request_access.reject`. (No email by default, or a neutral decline.)

## 4. State machine

```
submit ─▶ pending_verification ─(link opened, not expired)─▶ verified
   │                                   │
   │ (expiry)                          ├─ admin approve ─▶ approved ─(invite accepted)─▶ consumed
   ▼                                   └─ admin reject  ─▶ rejected
expired
```
Replay/expiry/duplicate are explicit terminal or no-op transitions, mirroring the
invitation state handling already in the portal.

## 5. Security & privacy

- **No enumeration**: identical public responses for new / existing / customer /
  rate-limited. Verification existence is never confirmed to an anonymous caller.
- **Tokens**: high-entropy (`secrets.token_urlsafe`), single-use, expiring, stored
  only as digests; raw token only in the email link; never logged.
- **Rate limits**: per-IP and per-email, plus a global ceiling; fail closed.
- **Abuse**: cap `note` length; strip control chars; consider a proof-of-work or
  hCaptcha **only if** approved (the site currently ships no third-party trackers).
- **CSRF/origin**: public POST is same-origin via the proxy; admin actions use the
  existing `csrf_guarded` double-submit. No CORS is added.
- **Privacy disclosure**: the submit form states what is collected (email, optional
  note, timestamp), why, and retention; link to `/privacy.html`. Rejected/expired
  requests are purged on a schedule.
- **Production SMTP required**: verification + approval email only work once
  `email-test` proves real delivery.

## 6. Frontend (portal SPA)

- New public route `#/request-access`: email + optional note, privacy disclosure,
  bounded success state ("check your email"), and a verify landing
  `#/request-access/verify?token=` that auto-reads + scrubs the token from the URL
  (same token-hygiene as `#/accept-invite`).
- New admin route `#/admin/access-requests`: the verified queue with approve/reject.
- Only after this ships do the static site's "Request access" links change from
  `mailto:` to `https://app.aithernet.online/#/request-access`.

## 7. Test matrix (all must pass before shipping)

- submit: valid/malformed email; note length cap; normalization.
- enumeration: identical response for new vs existing vs customer vs rate-limited.
- rate limits: per-IP and per-email windows; fail closed at the ceiling.
- verification: success; **expired**; **replay** (already consumed); wrong/garbage
  token; token never stored raw.
- duplicate: second submit for a pending email does not create a second row and
  does not leak state.
- approval authorization: non-admin and unauthenticated are rejected; CSRF missing
  is rejected.
- approve: creates exactly one invitation via the existing subsystem; status →
  approved; invitation link is the canonical form.
- reject: status → rejected; no invitation created.
- audit: submit/verify/approve/reject each write a record.
- email: verification + invitation messages carry the token only in the link and
  never in logs (extend the existing email tests).
- privacy: purge job removes expired/rejected requests.

## 8. Rollout order

1. Prove production SMTP (`email-test`).
2. Implement backend (`HostedAccessRequest` + endpoints + tests) — separate commit.
3. Implement portal routes (public + admin) — separate commit.
4. Flip the static site links from `mailto:` to `#/request-access`.
5. Clean-machine end-to-end: request → verify → approve → invite → account.

Until step 4, the public site stays invitation-only and truthful.
