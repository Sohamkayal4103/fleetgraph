# Web deployment record — unified customer origin `www.aithernet.online` + "Signal" redesign

**Type:** web / control-plane / edge deployment + UI/UX redesign. **No installed package files
change → no `1.0.0-beta.5`; the immutable `v1.0.0-beta.4` tag and package are untouched.**

**Status: PUBLISHED — promoted from STAGED on 2026-07-03 after production verification (§18) passed.**
The unified customer origin and the premium "Signal" redesign are live on
`https://www.aithernet.online` (public marketing + `/app` customer portal + same-origin `/api`).
`admin.aithernet.online` remains a separate Cloudflare-Access origin; `app.aithernet.online` API
compatibility for installed beta.4 nodes is preserved.

## Scope (two phases, one record)
1. **Unified customer origin** — all public + customer-facing pages on the single canonical hostname
   `https://www.aithernet.online`; portal path-routed under `/app`; `/login` as the sign-in route;
   host-only cookies; `admin.` kept separate; `app.` API/node/download/ingest preserved for beta.4.
2. **"Signal" premium redesign** — an original Aithernet design language (calm near-white surfaces,
   deep ink, one blue accent with a restrained indigo companion for gradients, a mono technical
   eyebrow, a fluid modular type scale, a faint signal-field motif) applied across the public site
   and the authenticated portal so the whole surface reads as one premium infrastructure product.
Full design/route table/security rationale: [`unified-www-origin.md`](unified-www-origin.md).

## Components changed
**Public marketing site** (repo `aithernet-site`, branch `main`):
- New shared shell across all 18 pages: a polished **Platform mega-menu** (accessible `<details>`,
  no-JS baseline + `session.js` close-on-outside/Escape/one-open enhancement) with the mature IA
  Product · Platform ▾ · Use cases · Developers · Security · Company.
- Homepage rebuilt: two-column hero + an original framed **system diagram**, a trust/value strip,
  problem → solution (stack panel) → **seven product modules** → a **5-step "how it works" stepper**
  → use cases → security → gradient CTA. Honest copy only (no fabricated metrics/logos/testimonials).
- `/product` and `/platform` rebuilt as substantial pages (module cards, framed diagrams, "what it
  is / isn't"); `/docs` reworked into a developer entry point (quick-start stepper + grouped doc
  cards); the 14 utility pages inherit the system through the shared stylesheet + new header/footer.
- Design system stylesheet `assets/style.css` (tokens, type/spacing scale, buttons, mega-menu, hero
  + diagram, modules, stepper, dark CTA, footer, mobile) — all legacy classes preserved.
- Session-aware header preserved (`#acct-action`: Sign in→`/login` / Open portal→`/app`) + a
  session-aware hero CTA (Get early access → Open portal when signed in). Clean same-origin paths,
  `.html` compatibility, `#navtoggle` mobile nav, and the no-flash `acct-pending` failsafe intact.
- **Asset cache-busting** (`?v=signal-1` on `style.css`/`session.js`) so each release is a new edge
  cache key — the edge serves the correct assets immediately, with no manual purge dependency.
- **New test + CI gate**: `scripts/check-links.mjs` fails on any visible `.html`, `app.aithernet.online`,
  `#/` route, admin link on the public site, dead internal link, missing asset, or a page missing the
  `#acct-action`/`#navtoggle` hooks — wired into the Pages deploy workflow (gates every publish).

**Portal SPA** (repo `aithernet`, branch `unified-www-origin`, `portal/`):
- History-API path routing; asset base `/app/`; owns `/login` `/logout` `/status` `/app/*`; hardened
  `return_to` open-redirect gate; `/login`-while-authed → `/app` with no form flash.
- "Signal" design language applied (`src/styles.css`): matched palette/spacing/shadows, `PORTAL`
  brand pill, pill-hover primary nav (added Platform → `/platform`), sticky sidebar with a gradient
  active-rail, elevated cards/stat cards, and calm dashed empty-state surfaces ("0 nodes" / "no
  support cases" / not-provisioned now read as intentional). Header/footer pixel-aligned to the site.

**Control plane** (`services/control_plane/`) and **edge** (`deploy/reverse-proxy/Caddyfile.cloudflare`,
`deploy/edge/routing.mjs`): unchanged since the beta.4 unified-origin cutover — same-origin `/api`,
`session-status` (`no-store`, non-wildcard CORS, `portal_url` = `https://www.aithernet.online/app`),
marketing proxy to GitHub Pages with clean-path→`.html` rewrite, and legacy `app.`→`www` browser
redirects with the API preserved.

## Verification performed in-repo (all green)
- Portal vitest: **57/57 pass** (16 files) — path routing, deep links, anonymous→/login,
  /login-when-authed→/app (no flash), /logout, open-redirect rejection, same-origin nav, unified
  shell, session.js.
- Edge routing policy: **12/12 pass** (`node --test deploy/edge/routing.test.mjs`) — incl. clean
  marketing paths → marketing origin, and `/app/assets/*.js` **and** `*.css` → app origin unrewritten.
- Control-plane session-status pytest: **8/8 pass** — 200 for anon+auth, `no-store`, no token/id
  leakage, narrow credentialed CORS.
- Site link-hygiene: **18/18 pages pass** (`node scripts/check-links.mjs`); gated in CI.
- Portal production build: **pass** (`tsc --noEmit && vite build`) → bundle `index-BWKab8QD.js` /
  `index-zdLIv1Fm.css`.

## Production verification (§18) — PASSED (2026-07-03)
Portal deployed via `AITHERNET_HOSTED_ENV_FILE=… AITHERNET_COMPOSE_PROJECT=aithernet_prod
AITHERNET_COMPOSE_OVERLAY=docker-compose.cloudflare.yml ./deploy/production/upgrade.sh` (backup →
build → migrate → restart); all services healthy. Site published via `aithernet-site@main` → GitHub
Pages → Caddy marketing proxy.

- Cloudflare cache/edge state clean for the live Signal redesign; `www.aithernet.online` serves the
  Signal design live; homepage grep confirms **no** `app.aithernet.online`/`#/signin` links and **no**
  stale `#/signin` routes; clean `/login` and `/status` links.
- Signal assets live: `/assets/style.css?v=signal-1` → **text/css**; `/assets/session.js?v=signal-1`
  → **application/javascript**.
- `/login` → **200**, **text/html**, **Cache-Control: no-store**.
- `www /api/v1/auth/session-status` → JSON with `portal_url` `https://www.aithernet.online/app`.
- `app.aithernet.online` browser root → **302 → https://www.aithernet.online/app**; `app.` API routes
  still return **JSON** and are **not** redirected.
- `/app/assets/*.js` → **JavaScript** MIME; `/app/assets/*.css` → **CSS** MIME (live bundle
  `index-BWKab8QD.js` / `index-zdLIv1Fm.css`).
- Browser journey verified as one cohesive Signal shell: www → Product → Platform → Documentation →
  Sign in/Open portal → `/login` → `/app` → Nodes → Product → Privacy/legal → Open portal → `/app`.
  No hostname transition for customer/public navigation; no stale hash routes; no re-login.
- `admin.aithernet.online` remains separate and protected. beta.4 installed package unchanged.
  Node heartbeat/doctor verification passed.

## Source
- `aithernet@unified-www-origin` → `5f1ddc3` (portal Signal design system) atop the unified-origin
  shell commits.
- `aithernet-site@main` → `c2549b8` (Signal redesign) + `db88ad2` (asset cache-bust) atop `08c3188`.

## Package impact
None. Installed beta.4 nodes keep calling `app.aithernet.online/v1/node/*`, `/v1/downloads/*`,
`/ingest/*` — proxied unchanged. No beta.5. The immutable `v1.0.0-beta.4` tag, package, signature,
and release record (`d5b2e07d`, PUBLISHED early-access) are untouched.

## Rollback
Revert `aithernet-site@main` to `08c3188` (Pages redeploys); retag/redeploy the previous
`aithernet/frontend` image via `deploy/production/upgrade.sh` after `git checkout` of the prior portal
commit. Edge/DNS/`app.`/`admin.` are unchanged by this promotion, so no DNS or Access rollback is
involved.

## Final classification
**AITHERNET UNIFIED CUSTOMER ORIGIN + "SIGNAL" REDESIGN — DEPLOYED (PUBLISHED).** One cohesive,
premium customer product surface live on `https://www.aithernet.online` across public marketing and
the authenticated `/app` portal; same-origin `/api`; host-only sessions; `admin.` isolated; `app.`
API/node/download/ingest compatibility and the immutable `1.0.0-beta.4` package all preserved.
