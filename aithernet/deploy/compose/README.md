# Aithernet Stage 14F — single-host compose deployment

> This document covers the **Stage 14F single-host compose stack** under
> `deploy/compose/` and `deploy/reverse-proxy/`. The existing `deploy/README.md`
> covers the Stage 14A systemd single-node install and is unchanged.
>
> **Scope note:** This is a **LOCAL / single-host deployment profile**. Public
> TLS with a real domain, an externally reachable hostname, and a managed
> production PostgreSQL remain **operational verification steps that this profile
> does NOT prove**. The local profile validates that the services start, wire to
> PostgreSQL, and route through the proxy — nothing about real-world TLS or DNS.

## 0. Files

| File | Purpose |
| --- | --- |
| `docker-compose.yml` | Production-oriented single-host stack (proxy, control-plane, ingestion, portal, public-site, postgres, minio, worker, one-shot migrate/bootstrap/backup). |
| `docker-compose.local.yml` | Local-dev override: loopback HTTP, dev email sink, local blobs, `AITHERNET_HOSTED_ENV=development`. |
| `../reverse-proxy/Caddyfile` | Primary supported reverse proxy (auto-TLS via ACME). |
| `../reverse-proxy/Caddyfile.local` | Loopback HTTP proxy for the local smoke. |
| `../reverse-proxy/nginx.conf.example` | Alternative Nginx proxy template. |
| `../../.env.production.example` | Environment template — **placeholders only**. |

## 1. Configure secrets

```bash
# From the repo root:
cp .env.production.example .env.production       # or deploy/compose/.env.production
chmod 600 .env.production
```

`.env.production` is **gitignored** and must **never** be committed. Generate real
secrets OUTSIDE git:

```bash
openssl rand -hex 32      # AITHERNET_HOSTED_SESSION_KEY, INGEST_ADMIN_TOKEN
openssl rand -base64 24   # POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD
```

Provide the release signing keypair as PEM files mounted read-only (see the
commented `volumes:` on the `control-plane` service) referenced by
`AITHERNET_RELEASE_SIGNING_KEY` / `AITHERNET_RELEASE_PUBLIC_KEY`.

Then point the stack at your real env file. The committed compose references the
**template** (`../../.env.production.example`) so it is safe to commit; edit the
`env_file:` entries to `../../.env.production` (or `./.env.production`) before a
real run.

Replace the `*.example.invalid` placeholder hostnames (and the `ACME_EMAIL`) with
your real domain and operator mailbox in both `.env.production` and the chosen
proxy template.

## 2. Production bring-up

```bash
cd /home/operator/gnu/aithernet

# 1. Start data + app services (proxy waits for healthy control-plane/ingestion).
docker compose -f deploy/compose/docker-compose.yml up -d

# 2. Run database migrations (one-shot; PostgreSQL is the central DB).
docker compose -f deploy/compose/docker-compose.yml --profile tasks \
  run --rm migrate

# 3. Bootstrap the first admin (override the email with your operator address).
docker compose -f deploy/compose/docker-compose.yml --profile tasks \
  run --rm bootstrap-admin \
  python -m services.control_plane.cli admin bootstrap \
  --email ops@example.invalid --generate-secret

# 4. Health-check.
docker compose -f deploy/compose/docker-compose.yml ps
curl -fsS https://api.example.invalid/health/ready
curl -fsS https://ingest.example.invalid/ingest/v1/readiness
```

The optional background worker (control plane runs work **in-process** by
default) can be started with `--profile worker`.

## 3. Local-dev profile (smoke / `deploy acceptance`)

Combines the base file with the local override (loopback HTTP on host `:8080`,
development email sink, local blob storage, `AITHERNET_HOSTED_ENV=development`,
throwaway secrets). PostgreSQL is still the central DB.

```bash
docker compose \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.local.yml up -d

# Smoke the routed health endpoints via the local proxy:
curl -fsS http://localhost:8080/health/ready
curl -fsS http://localhost:8080/ingest/v1/readiness
```

Tear down: `docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.local.yml down -v`.

## 4. Backup and restore

Backups run as a one-shot job under the `backup` profile and write a timestamped
archive into the `backups` named volume:

```bash
docker compose -f deploy/compose/docker-compose.yml --profile backup \
  run --rm backup
# -> /backups/aithernet-backup-<UTC timestamp>.tar.gz inside the volume.
```

Restore from a specific archive:

```bash
docker compose -f deploy/compose/docker-compose.yml --profile tasks \
  run --rm migrate \
  python -m services.control_plane.cli restore --input /backups/<archive>.tar.gz
```

(The `migrate`/`bootstrap-admin`/`backup` task services share the control-plane
image, so any `services.control_plane.cli` subcommand — `backup --output`,
`restore --input` — can be run through them.)

## 5. Upgrade / rollback

**Upgrade**

1. **Stop** accepting new work (optional: scale proxy down or set maintenance).
2. **Backup** first: `... --profile backup run --rm backup`.
3. **Deploy the new image**: bump the image tag / rebuild, then
   `docker compose -f deploy/compose/docker-compose.yml pull` (or `build`).
4. **Migrate**: `... --profile tasks run --rm migrate`.
5. **Bring up**: `docker compose -f deploy/compose/docker-compose.yml up -d`.
6. **Health-check**: `/health/ready` and `/ingest/v1/readiness` return 200.

**Rollback**

1. Stop the services: `docker compose -f deploy/compose/docker-compose.yml down`.
2. Restore the pre-upgrade backup:
   `... restore --input /backups/<pre-upgrade-archive>.tar.gz`.
3. Re-pin the **previous image tag** in the compose file (or `.env.production`).
4. `docker compose -f deploy/compose/docker-compose.yml up -d` and re-run the
   health checks above.

## 6. Logs

```bash
# Follow all services:
docker compose -f deploy/compose/docker-compose.yml logs -f

# A single service:
docker compose -f deploy/compose/docker-compose.yml logs -f control-plane
docker compose -f deploy/compose/docker-compose.yml logs -f ingestion
docker compose -f deploy/compose/docker-compose.yml logs -f proxy
```

## 7. Network exposure

Only the `proxy` service publishes ports (80/443 in production; `8080` in the
local override). PostgreSQL, MinIO, the control-plane, ingestion, portal, and
public-site are reachable **only on the internal compose network** and are never
published to the host. The MinIO console (`:9001`) is likewise unpublished.
