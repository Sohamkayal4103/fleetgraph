# Aithernet — production deployment via Cloudflare Tunnel (app + admin)

This runbook deploys the **existing** hosted control plane, ingestion service,
customer/admin/public SPA, PostgreSQL and MinIO on a single Ubuntu 24.04 amd64
host, published to the Internet through a **Cloudflare Tunnel** — with **no**
public 80/443 bind and **no** router/NAT port opened.

It reuses the same tracked images and scripts as the base production runbook
(`README.md`); only the proxy is overridden (loopback origin + `Caddyfile.cloudflare`)
and `cloudflared` fronts it.

> **Canonical customer origin (see `docs/unified-www-origin.md`).** `www.aithernet.online`
> is now the ONE customer-facing hostname: the Caddy origin answers for `www` too,
> path-routing `/app`, `/login`, `/api`, `/status`, `/v1/node`, `/v1/downloads`,
> `/ingest` to the internal containers and reverse-proxying every other (marketing)
> path to the GitHub Pages site (repo `aithernet-site`). `app.aithernet.online`
> remains as the legacy compat host (programmatic API preserved; browser GET →
> 302 to `www`). `admin.aithernet.online` stays a separate Cloudflare-Access origin.
> The portal SPA is **path-routed** under `/app` (no more hash routing).

```
Browser ──HTTPS──> Cloudflare edge ──Tunnel(outbound)──> cloudflared (host)
   app.aithernet.online / admin.aithernet.online             │ http 127.0.0.1:8087
                                                              ▼
                                                 Caddy proxy (Caddyfile.cloudflare)
                                       ┌──────────────┬───────────────┬───────────┐
                                   portal:8102   control-plane:8100  ingestion:8090
                                                       │
                                            postgres:5432  minio:9000  (internal only)
```

> Nothing here is "live" until the operator-gated steps (§3–§7) are completed on
> a real host with real Cloudflare credentials. Do not treat the templates as a
> running deployment.

## Architecture decisions

- **One backend, three hostnames.** `www` (canonical customer origin), `app`
  (legacy compat) and `admin` all route to the same control plane and SPA. The SPA
  is path-routed: `/app*` (customer), `/admin*` (admin, admin host only), `/login`,
  `/logout`, `/status`; the control plane authorizes platform-admin actions
  **server-side**. Route hiding is never authorization.
- **Host-only cookies.** The session cookie is host-only + `SameSite=Lax`. A
  session at `app.` is **not** sent to `admin.` — admins sign in at `admin.`
  separately. The cookie `Domain` is **not** broadened across subdomains.
- **Admin defense-in-depth.** `admin.aithernet.online` is gated by **Cloudflare
  Access** at the edge **and** by Aithernet platform-admin authorization at the
  control plane. Both are required.
- **Same-origin API.** The browser calls `/api`, `/ingest`, `/v1/downloads` on
  the same host it loaded the SPA from, so CSRF (HMAC double-submit) + the
  HttpOnly session cookie stay same-origin. Control plane / ingestion / postgres
  / minio are never published.
- **Invitations** use `AITHERNET_PORTAL_BASE_URL=https://www.aithernet.online`,
  producing the canonical link
  `https://www.aithernet.online/app/accept-invite?token=<one-time-token>`. Old
  `app.aithernet.online/#/accept-invite?token=…` links still resolve via the SPA's
  hash→path shim during the compatibility window.

## 1. Host prerequisites (Ubuntu 24.04 amd64)
- Docker Engine + Docker Compose plugin.
- Outbound network access (image pulls, the frontend `npm ci` build, the tunnel).
- No inbound ports required. Keep the host firewall default-deny on inbound; the
  tunnel and Docker need only outbound.

## 2. Clone + production env file (secrets OUTSIDE git)
```
git clone <repo-url> aithernet && cd aithernet
git checkout beta-qualification

sudo install -d -m 0750 /etc/aithernet
sudo install -m 0600 deploy/production/cloudflare.env.example /etc/aithernet/hosted.env
sudo $EDITOR /etc/aithernet/hosted.env       # fill every REPLACE_WITH_* value
```
Generate secrets locally:
```
openssl rand -hex 32      # AITHERNET_HOSTED_SESSION_KEY, INGEST_ADMIN_TOKEN
openssl rand -base64 24   # POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD, S3 keys
```
Production **fails closed** on any leftover placeholder, non-HTTPS/loopback public
URL, insecure cookie, SQLite, the dev email sink, or a still-enabled bootstrap.

The stack uses the production project + the Cloudflare overlay. Export these once
per shell (the scripts read them):
```
export AITHERNET_COMPOSE_PROJECT=aithernet_prod
export AITHERNET_COMPOSE_OVERLAY=docker-compose.cloudflare.yml
```

## 3. Validate, build, migrate, start
```
deploy/production/check.sh    --env-file /etc/aithernet/hosted.env
deploy/production/install.sh  --env-file /etc/aithernet/hosted.env
```
`install.sh` validates → builds → migrates → (bootstraps the first admin only if
`AITHERNET_HOSTED_BOOTSTRAP=1`) → starts → prints health. After bootstrap, set
`AITHERNET_HOSTED_BOOTSTRAP=0` and re-run `check.sh`.

Confirm only the proxy publishes a port — and only on loopback:
```
docker compose -p aithernet_prod \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.cloudflare.yml \
  --env-file /etc/aithernet/hosted.env ps
ss -ltnp | grep -E '127\.0\.0\.1:8087' || true     # origin is loopback-only
ss -ltnp | grep -E '0\.0\.0\.0:(80|443|5432|9000|9001)' && echo "UNEXPECTED PUBLIC BIND" || echo "no public DB/MinIO/HTTP bind (good)"
```

## 4. Install cloudflared + create the tunnel (operator-gated)
```
# Install (Cloudflare apt repo) — see Cloudflare docs for the current key/repo.
sudo apt-get install -y cloudflared        # or the official .deb

cloudflared tunnel login                   # browser auth (one time)
cloudflared tunnel create aithernet-prod   # prints a <TUNNEL-UUID> + creds JSON

sudo install -d -m 0750 /etc/cloudflared
sudo install -m 0640 deploy/production/cloudflared/config.template.yml /etc/cloudflared/config.yml
sudo install -m 0600 ~/.cloudflared/<TUNNEL-UUID>.json /etc/cloudflared/<TUNNEL-UUID>.json
sudo $EDITOR /etc/cloudflared/config.yml   # set <TUNNEL-UUID> + credentials-file path
```
The credentials JSON is a **secret** (mode 0600, outside git). Never commit it.

## 5. DNS routes (operator-gated)
```
cloudflared tunnel route dns aithernet-prod app.aithernet.online
cloudflared tunnel route dns aithernet-prod admin.aithernet.online
```
This creates proxied `CNAME` records (`app`, `admin` → `<UUID>.cfargotunnel.com`).

**Canonical `www` cutover (see `docs/unified-www-origin.md` §8).** To make `www` the
one customer origin, repoint it at the SAME tunnel, **Proxied**:
`www CNAME <UUID>.cfargotunnel.com` (do this in the Cloudflare dashboard — do NOT
`route dns` over the existing `www` record, and do NOT remove the GitHub Pages
custom domain: Caddy proxies the marketing paths to GitHub Pages with the correct
upstream `Host`). Roll back by setting `www CNAME adityapawar0401.github.io` again.
Keep the redirects temporary (302) until production tests pass, then promote (301).

## 6. Run cloudflared as a service
```
sudo cloudflared service install        # or: systemctl enable --now cloudflared
systemctl status cloudflared
```

## 7. Cloudflare Access for admin (operator-gated)
In the Cloudflare Zero Trust dashboard:
1. Access → Applications → Add → Self-hosted.
2. Application domain: `admin.aithernet.online`.
3. Policy: allow only the operator identity (e.g. email `adityapawar@aithernet.online`
   via one-time-PIN or an IdP). Default-deny everyone else.
4. Leave `app.aithernet.online` **without** Access so invited customers can reach
   the portal; authorization there is the Aithernet session + invitation flow.

## 8. Live verification (HTTPS, after §4–§7)
```
curl -fsS https://app.aithernet.online/health/ready          # 200, JSON readiness
curl -fsS -o /dev/null -w '%{http_code}\n' https://admin.aithernet.online/   # 302/Access challenge
deploy/production/healthcheck.sh --env-file /etc/aithernet/hosted.env
```
- The TLS cert is Cloudflare's (edge). The origin never serves TLS.
- `admin.` must present the Cloudflare Access challenge; an unauthenticated
  request must NOT reach the SPA.
- Verify invitation links render as
  `https://app.aithernet.online/#/accept-invite?token=…`.

## 9. Backup / restore / upgrade / logs
```
deploy/production/backup.sh     --env-file /etc/aithernet/hosted.env
deploy/production/restore.sh    --env-file /etc/aithernet/hosted.env /path/to/backup.json   # DESTRUCTIVE
deploy/production/upgrade.sh    --env-file /etc/aithernet/hosted.env       # backup→build→migrate→up
deploy/production/logs.sh       --env-file /etc/aithernet/hosted.env control-plane --since 30m
```
Copy backups **off-host** on a schedule. Logs are bounded (json-file, 10m × 5).

## 10. Isolation guarantees
- Project name `aithernet_prod` → its own `aithernet_prod_*` named volumes,
  fully isolated from `aithernet_local` and `aithernet_lan`. Never run
  `docker compose down -v` against any of these.
- Only `deploy/compose/docker-compose.cloudflare.yml` is layered on the base;
  the local/lan overrides are untouched.

This runbook uses placeholder credentials only and contains no real secrets.
