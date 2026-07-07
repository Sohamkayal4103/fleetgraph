# Edge routing — canonical origin `www.aithernet.online`

The production edge router is the **Caddy reverse proxy reached through the Cloudflare Tunnel**
(`deploy/reverse-proxy/Caddyfile.cloudflare`). It path-routes the one canonical hostname:

| Path | Origin |
|---|---|
| `/app`, `/app/*`, `/login`, `/logout`, `/status`, `/reset`, `/accept-invite` | portal SPA (`portal:8102`) |
| `/api/*`, `/v1/downloads/*`, `/v1/public/*`, `/v1/node/*`, `/health/*` | control plane (`control-plane:8100`) |
| `/ingest/*` | ingestion (`ingestion:8090`) |
| everything else | static marketing site (GitHub Pages, repo `aithernet-site`) |

`routing.mjs` is the **single source of truth** for that path→origin allowlist, the method rules,
the marketing extensionless→`.html` rewrite, and the spoofable-header strip-list. It is what the
Caddy config implements, expressed as a pure, deterministically testable function — and it is
drop-in reusable as a Cloudflare Worker if the edge is ever moved off Caddy.

```
node --test deploy/edge/routing.test.mjs      # 10 deterministic routing-policy tests
```

Why Caddy and not a Worker: a Worker in front of GitHub Pages cannot cleanly override the upstream
`Host`, so GitHub Pages' custom-domain 301 redirect gets in the way; Caddy sets `header_up Host` and
proxies the site with no CNAME change and a one-line DNS rollback. See `docs/unified-www-origin.md`
§7 for the full rationale and the operator-gated Cloudflare/DNS steps.
