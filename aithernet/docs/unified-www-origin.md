# Unified customer origin — `www.aithernet.online`

Branch: `unified-www-origin`. Goal: every public + customer-facing page lives under the
single canonical hostname `https://www.aithernet.online`, so a customer never transitions
between `www.` and `app.` during normal navigation. Administration stays isolated on
`admin.aithernet.online`. The immutable `v1.0.0-beta.4` tag is never rewritten.

This document is the audit, the route table, the decisions, the edge design, the exact
external (Cloudflare / DNS / OAuth) steps, and the rollback plan. It is the backbone of the
final report.

---

## 1. Architecture — before

Three browser surfaces across **two origins on two different platforms**:

| Surface | Host (before) | Served by |
|---|---|---|
| Public marketing site (static `.html`) | `www.aithernet.online` | **GitHub Pages**, repo `aithernet-site` (`www` → `adityapawar0401.github.io`), `CNAME = www.aithernet.online` |
| Customer portal + auth + public-dynamic (one **hash-routed** React SPA) | `app.aithernet.online` | Cloudflare Tunnel → Caddy (`Caddyfile.cloudflare`) → `portal:8102` container |
| Same-origin control-plane API / ingest / downloads / node | `app.aithernet.online/api`, `/ingest`, `/v1/downloads`, `/v1/node` | same tunnel → Caddy → `control-plane:8100` / `ingestion:8090` |
| Administrator portal (same SPA, `#/admin*`) | `admin.aithernet.online` | same tunnel/Caddy, **Cloudflare Access** at edge + platform-admin authz server-side |

Key facts established by the audit (not assumed):

- The SPA (`portal/`) is a **dependency-free hash router** (`portal/src/routes/router.tsx`): routes
  are `#/portal*` (customer), `#/admin*` (admin), `#/signin`, `#/accept-invite`, `#/status`.
- The API client (`portal/src/api/client.ts`) already defaults to the **same-origin relative base
  `/api`** — CORS is not the primary transport for app↔api; it is same-origin already on `app.`.
- Session cookies (`services/control_plane/app.py:_set_session_cookies`) are already **host-only**
  (no `Domain` attribute), `Path=/`, `HttpOnly` (session), readable CSRF cookie (double-submit),
  `SameSite=Lax`, `Secure` in production. Cookie names: `aithernet_hosted_session`,
  `aithernet_hosted_csrf`.
- Customer authentication is **email + password → server session cookie**. There is **no customer
  OAuth / OIDC**. The only OAuth in the tree is server-side Google Drive (admin archive, out of
  browser scope). Admin identity is **Cloudflare Access** (OTP / IdP) in front of the admin host.
- The **node package is config-driven**: `src/aithernet/hosted/localconfig.py` stores
  `control_plane_base_url` captured at enrollment (`= https://app.aithernet.online` for beta.4
  installs). There is **no hard-coded host in package code** → website routing needs **no beta.5**.
  Installed nodes will keep calling `app.aithernet.online/v1/node/*` and `/v1/downloads/*`.
- Invitation / notification links are built in `services/control_plane/service_intake.py` from
  `config.urls.portal` as `…/#/accept-invite?token=` and `…/#/admin/early-access`.

## 2. Architecture — after

One canonical browser origin `www.aithernet.online`, path-routed at the edge across the two
**unchanged, still-separate** origins (we unify by routing, not by merging codebases). `www` is
routed through the **existing Cloudflare Tunnel to the existing Caddy reverse proxy**, which is the
single path router:

```
Browser ──HTTPS──> Cloudflare edge ──Tunnel(outbound)──> cloudflared (host) ──http 127.0.0.1:8087──▶
   www.aithernet.online                                                    Caddy (Caddyfile.cloudflare)
                     │
   ┌─────────────────┴───────────────────────────────────────────┐
   │ Caddy path match on the www host:                            │
   │  APP allowlist → internal containers:                        │
   │   /app, /app/*, /login, /logout, /status, /reset,            │
   │   /accept-invite            → portal:8102 (SPA shell)         │
   │   /api/*, /v1/downloads/*, /v1/public/*, /v1/node/*,         │
   │   /health/*                 → control-plane:8100             │
   │   /ingest/*                 → ingestion:8090                 │
   │  everything else (marketing) → reverse_proxy GitHub Pages     │
   │   (adityapawar0401.github.io, upstream Host = www...,         │
   │    extensionless → .html)                                    │
   └──────────────────────────────────────────────────────────────┘

admin.aithernet.online  — UNCHANGED, separate origin, Cloudflare Access + server-side authz.
app.aithernet.online    — remains a tunnel host (same Caddy) AND the legacy compat host
                          (browser GET → 301 to www; programmatic API preserved unchanged).
```

**Why Caddy, not a Cloudflare Worker.** A Worker bound to `www/*` in front of GitHub Pages hits the
custom-domain problem: GitHub Pages 301-redirects the `*.github.io` host to the custom domain, and a
Worker cannot cleanly override the upstream `Host` (it is derived from the fetch URL; using the
`www` URL loops back into the Worker route). Removing the GitHub CNAME to dodge that would break
clean rollback (a later Worker removal would leave `www` proxied at a Pages site that no longer
recognises the domain → 404). Caddy, reached through the tunnel, sets the upstream `Host` header
freely (`header_up Host www.aithernet.online`, SNI `adityapawar0401.github.io`), so GitHub Pages
serves content with **no redirect and no CNAME change**, and rollback is a one-line DNS flip. The
deterministic edge-routing *policy* is captured and tested in `deploy/edge/routing.mjs` (+
`routing.test.mjs`) so the exact path→origin allowlist is verifiable independently of Caddy, and is
drop-in reusable if a Worker is ever preferred.

The portal SPA is migrated from **hash routing to History-API path routing**, mounted with asset
base `/app/`. It owns these browser paths: `/login`, `/logout`, `/status`, `/app`, `/app/*`,
`/accept-invite`, and (on the admin origin only) `/admin/*`.

## 3. Canonical route table

| Path (under `https://www.aithernet.online`) | Owner origin | Method(s) | Cache |
|---|---|---|---|
| `/` | GitHub Pages | GET/HEAD | CDN default |
| `/product` `/platform` `/use-cases` `/security` `/docs` `/privacy` | GitHub Pages | GET/HEAD | CDN default |
| `/company` `/hardware` `/install` `/supported-systems` `/rf` `/rf-notices` `/api` `/release-notes` `/request-access` | GitHub Pages | GET/HEAD | CDN default |
| `/assets/*` (marketing) | GitHub Pages | GET/HEAD | immutable (site-hashed) |
| `/login` | app origin (SPA shell) | GET/HEAD | `no-store` |
| `/logout` | app origin (SPA shell) | GET/HEAD | `no-store` |
| `/status` | app origin (SPA shell) | GET/HEAD | short revalidate |
| `/app` `/app/*` | app origin (SPA shell) | GET/HEAD | HTML `no-store`; `/app/assets/*` immutable |
| `/accept-invite` | app origin (SPA shell) | GET/HEAD | `no-store` |
| `/api/v1/*` | app origin → control-plane | GET/POST/PUT/PATCH/DELETE | `no-store` (auth), else per-route |
| `/api/v1/auth/*` `/api/v1/session*` | app origin → control-plane | as above | `no-store` |
| `/ingest/*` | app origin → ingestion | POST/GET | `no-store` |
| `/v1/downloads/*` `/v1/public/*` `/v1/node/*` `/health/*` | app origin → control-plane | node/programmatic | per-route |
| everything else | GitHub Pages (marketing 404) | GET/HEAD | — |

App-path SPA routes (in `portal/src/routes/router.tsx`): `/app` overview, `/app/nodes`,
`/app/meshes`, `/app/downloads`, `/app/data`, `/app/account`, `/app/support`,
`/app/getting-started`, `/app/policies`, `/app/sessions`.

## 4. Administrator decision — KEEP `admin.aithernet.online` (separate origin)

Chosen: **administration stays on its own browser origin** `admin.aithernet.online`, behind
Cloudflare Access, with platform-admin authorization still enforced server-side. Rationale:

- A **separate browser origin is a strictly stronger boundary** than a path prefix: cookies are
  host-only and cannot be sent to `www` at all; the admin CSP/`frame-ancestors`/COOP are fully
  independent; a bug in public/portal JS cannot reach admin storage or admin cookies.
- The `/admin/*`-under-`www` option was assessed (see below) and is **not** adopted because the
  full set of proofs the spec requires cannot be *proven* from this repo without live Cloudflare
  Access path-policy testing (encoded-slash / trailing-slash / case / normalization bypass), and
  because it would only *weaken* the current isolation for cosmetic single-host consistency. The
  spec explicitly permits keeping admin separate, and admin is **not** customer-facing navigation,
  so leaving it on its own host does not violate the "no hostname transition during customer
  navigation" requirement.

`/admin/*` single-host assessment (why it is deferred, not done):
- Would require a Cloudflare Access **self-hosted app** scoped to `www.aithernet.online/admin/*`
  AND `/admin/api/*`, deny-by-default, existing admin identity policy, MFA/OTP, separate admin
  cookies, no public caching, and proven-safe path normalization. Path-based Access is documented
  by Cloudflare as weaker than host-based and is bypass-prone (`/admin/../`, `%2f`, `//admin`,
  case). None of that can be *production-proven* here. Verdict: **do not silently expose admin**;
  keep the separate origin.

Customer navigation therefore never needs `/admin`. Admin links are removed from any
customer-facing surface and the admin portal is reached only by admins at `admin.aithernet.online`.

## 5. Session cutover — one-time reauthentication

Existing host-only cookies on `app.aithernet.online` **cannot** become `www.aithernet.online`
cookies (host-only, no shared `Domain` — by design, and we keep it that way). We therefore adopt
**documented one-time reauthentication** (the spec's preferred option): after cutover, a customer's
first visit to `www` is unauthenticated → `/login` → new host-only `www` session cookie. No session
value, CSRF, or token is ever transported in a URL. No `Domain=.aithernet.online` broadening.

## 6. Old-host (`app.aithernet.online`) compatibility

- **Browser GET** on `app.` → `301` to the `www` equivalent, query preserved after validation:
  `/` → `/app`, `/sign-in` & `/signin` → `/login`, `/portal*` → `/app*`, `/status` → `/status`.
  Old **hash** deep links (`#/portal/nodes`, `#/accept-invite?token=`) still work because the SPA
  is still served on `app.` during the window and a hash→path shim in the router rewrites them.
- **Programmatic API** on `app.` (`/api/*`, `/v1/node/*`, `/v1/downloads/*`, `/ingest/*`,
  `/health/*`) is **preserved unchanged** — never redirected to a browser page, never method-
  downgraded. Installed beta.4 nodes keep enrolling, heart-beating and downloading.

## 7. Edge router — Caddy (`deploy/reverse-proxy/Caddyfile.cloudflare`)

The existing Caddy proxy gains a `www.aithernet.online` site block that is the path router:

- **App allowlist → internal containers.** A fixed set of path prefixes (`/app`, `/login`,
  `/logout`, `/status`, `/reset`, `/accept-invite`, `/api/*`, `/ingest/*`, `/v1/downloads/*`,
  `/v1/public/*`, `/v1/node/*`, `/health/*`) route to `portal:8102`, `control-plane:8100` or
  `ingestion:8090` exactly as the `app.` host already does. Method, body and query are preserved by
  Caddy's `reverse_proxy`. This is a closed allowlist — **no open proxy, no user-controlled
  destination.**
- **Everything else → GitHub Pages** (`reverse_proxy` to `adityapawar0401.github.io` with
  `header_up Host www.aithernet.online`, upstream TLS/SNI `adityapawar0401.github.io`, and an
  extensionless→`.html` rewrite for the spec's clean marketing routes). GitHub Pages serves the
  site directly (no redirect) and no GitHub CNAME change is needed.
- **Header hygiene.** Inbound spoofable `X-Forwarded-*` / `Forwarded` / `X-Real-IP` are stripped at
  ingress; the real client IP arrives as Cloudflare's `CF-Connecting-IP` (origin is loopback-only,
  reachable only by `cloudflared`). `Strict-Transport-Security` + the standard security headers are
  emitted on every response.
- **Legacy `app.` browser redirects** (GET/HEAD only) → `www` equivalents; the programmatic API
  paths on `app.` are matched first and proxied unchanged (never redirected, never downgraded).
- **Deterministic 404/405**: unknown marketing paths fall through to GitHub Pages' 404; unknown
  `/api` paths return the control plane's JSON 404 (never the SPA shell).

`cloudflared` gains a `www.aithernet.online` ingress rule to the same loopback origin. The edge
routing *policy* (path→origin, method rules, header strip-list) is unit-tested in
`deploy/edge/routing.mjs` + `deploy/edge/routing.test.mjs`.

## 8. External (operator-gated) steps — Cloudflare / DNS / OAuth

These cannot be performed from the repo. Execute in order; each is reversible.

**8.1 `cloudflared` ingress — add `www` (host, then service reload).**
On the production host, add the `www.aithernet.online` rule from
`deploy/production/cloudflared/config.template.yml` (already updated) to `/etc/cloudflared/config.yml`,
then `sudo systemctl restart cloudflared`. Redeploy the proxy container so it picks up the updated
`Caddyfile.cloudflare` and the `AITHERNET_WWW_HOST` env (`deploy/production/upgrade.sh`).

**8.2 DNS — repoint `www` at the tunnel (Cloudflare dashboard / API). ONE-LINE ROLLBACK.**
- Today `www` is a `CNAME → adityapawar0401.github.io` (GitHub Pages).
- Change it to the tunnel, **Proxied**: `www  CNAME  <TUNNEL-UUID>.cfargotunnel.com  (Proxied)`
  (the same target as `app`/`admin`). Caddy then path-routes `www` and reverse-proxies marketing
  back to GitHub Pages with the correct upstream `Host` — **do not change or delete the GitHub
  Pages CNAME/custom domain**; it must stay so Pages serves the site for `Host: www.aithernet.online`.
- **Rollback:** set `www` back to `CNAME → adityapawar0401.github.io`. GitHub Pages serves it
  directly again; `app`/`admin` untouched. No data change.

**8.4 OAuth / identity — customer auth: NONE required.** Customer login is email+password; there
is no external IdP callback URL to change. The only identity-provider config is **Cloudflare Access
on `admin.aithernet.online`**, which is **unchanged** (admin stays on its host). If admin is ever
moved to `/admin` later, add a Cloudflare Access self-hosted app for `www.aithernet.online/admin/*`
(deferred — see §4). Google Drive OAuth (server-side archive) is unaffected by browser routing.

**8.5 Cache Rules (dashboard) — belt-and-suspenders over origin headers.**
- `Cache Level: Bypass` (or `no-store` respect) for `www.aithernet.online/login`, `/logout`,
  `/status`, `/app`, `/app/*` (HTML), `/api/*`, `/ingest/*`, `/accept-invite`.
- `Cache Everything` + `Edge TTL` only for `www.aithernet.online/app/assets/*` and marketing
  static. Never cache a `Set-Cookie` or authenticated HTML response.

**8.6 After production qualification** — flip the temporary `302`s to `301` (see §15 of the
brief) and, only then, permanently redirect `aithernet.online/*` and legacy `app.` browser pages.

## 9. Rollback plan

1. **DNS (primary, one line):** set `www  CNAME → adityapawar0401.github.io` again. Cloudflare stops
   sending `www` to the tunnel; GitHub Pages serves the marketing site directly (its CNAME/custom
   domain was never removed). `app.`/`admin.` are untouched, portal still works there. No data change.
2. **cloudflared:** remove the `www` ingress rule and `systemctl restart cloudflared`.
3. **Origin:** `Caddyfile.cloudflare` changes are additive (a new `www` block + legacy-redirect
   matchers on the `app` block). Revert the file and redeploy the proxy container. Keep the previous
   proxy image/config as the rollback artifact.
4. **Portal build:** the previous hash-routed `aithernet/frontend:latest` image is the rollback
   artifact — retag/redeploy to restore the old SPA.
5. **Redirects** start as **temporary (302)** so nothing is cached hard until qualified (§15).

## 10. Deterministic test coverage

- `portal/src/test/*` — path router, `/app` deep links + refresh, `/login`/`/logout`, anonymous
  redirect, `return_to` open-redirect rejection, host-only cookie assumptions, no-token-in-URL,
  hash→path compat shim, asset-base collision.
- `deploy/edge/worker.test.mjs` — app-path vs marketing routing, method/query/body preservation,
  header stripping, no-open-proxy, unknown-route 404, no admin proxy, HTTPS enforcement.
- `tests/test_*` (pytest) — invitation link now `www/app/accept-invite`, CORS narrow to `www`
  (never wildcard-with-credentials), session-status `no-store`, node/API compat unchanged.

## 11. Limitations / not-done-here (require live infra or the separate repo)

- The `www` DNS repoint to the tunnel, the `cloudflared` ingress add + reload, the proxy redeploy,
  Cache Rules, and Cloudflare Access are **operator-gated** (§8) — provided as exact steps, not
  executed here.
- Real-browser production verification (desktop/mobile, cookies, headers, redirects) requires the
  live cutover; the deterministic + local suites stand in until then.
- The marketing link/metadata edits live in the **separate `aithernet-site` repo** (committed
  there on its own `unified-www-origin` branch), not in this repo.
</content>
