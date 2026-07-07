# Aithernet hosted control plane — production deployment runbook (Stage 14F)

A single-host, production-oriented deployment of the Aithernet hosted control plane,
ingestion service, customer/admin/public frontend, PostgreSQL, MinIO and a reverse proxy.
Every build input is **tracked** — a clean clone deploys with no manually created Dockerfile,
prebuilt frontend, or operator nginx file. Placeholder domains are `*.example.invalid`.

> This profile is a single host. Public TLS with a real domain, managed/HA PostgreSQL, real
> SMTP and real object storage remain operational verification steps it does not prove.

All operator commands take the env-file path explicitly (`--env-file`), defaulting to
`/etc/aithernet/hosted.env` only when documented. Scripts stop on error, never print secrets,
never overwrite secrets, and require explicit confirmation for destructive actions.

## 1. Clean server prerequisites
- Linux host with Docker Engine + the Docker Compose plugin.
- Outbound network access for image pulls and the frontend `npm ci` build stage.
- A DNS domain whose `www.`, `app.`, `api.`, `ingest.`, `downloads.` records point at the host
  (required only for real public TLS; the local profile uses loopback).

## 2. Git clone
```
git clone <repo-url> aithernet && cd aithernet
git checkout stage-14f-early-access-production
```

## 3. Production environment file
```
sudo install -d -m 0750 /etc/aithernet
sudo install -m 0600 .env.production.example /etc/aithernet/hosted.env
sudo $EDITOR /etc/aithernet/hosted.env
```

## 4. Secret generation (outside git)
```
openssl rand -hex 32      # AITHERNET_HOSTED_SESSION_KEY, INGEST_ADMIN_TOKEN
openssl rand -base64 24   # POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD
# Release signing keypair (kept off-host / in a KMS):
python -m services.control_plane.cli  # see `release sign` / your KMS workflow
```
Put the values in `/etc/aithernet/hosted.env` (mode 0600). Never commit them. Production mode
**fails closed** on any remaining placeholder, loopback public URL, insecure cookie, SQLite, or
the development email sink.

## 5. Domain variables
Set `AITHERNET_PUBLIC_SITE_BASE_URL`, `AITHERNET_PORTAL_BASE_URL`,
`AITHERNET_CONTROL_PLANE_BASE_URL`, `AITHERNET_INGESTION_BASE_URL`,
`AITHERNET_DOWNLOAD_BASE_URL` to your real `https://…` hostnames, and `ACME_EMAIL` to a
monitored mailbox. Point `deploy/reverse-proxy/Caddyfile` site labels at the same hostnames.

## 6. Validate + build images
```
deploy/production/check.sh  --env-file /etc/aithernet/hosted.env
deploy/production/build.sh  --env-file /etc/aithernet/hosted.env
```
`build.sh` builds the Python application image (`deploy/compose/Dockerfile`, installs Aithernet
from `pyproject` + the PostgreSQL driver) and the frontend image
(`deploy/compose/Dockerfile.frontend`, runs `npm ci` + `npm run build` on `portal/` from source).

## 7. Migrations
```
deploy/production/migrate.sh --env-file /etc/aithernet/hosted.env
```

## 8. First admin bootstrap
Set `AITHERNET_HOSTED_BOOTSTRAP=1` and `AITHERNET_HOSTED_ADMIN_EMAIL` / `..._PASSWORD` in the env
file for the first boot, then:
```
deploy/production/bootstrap-admin.sh --env-file /etc/aithernet/hosted.env
```
Return `AITHERNET_HOSTED_BOOTSTRAP=0` afterwards. Bootstrap refuses to run twice.

## 9. Stack startup
```
deploy/production/up.sh --env-file /etc/aithernet/hosted.env
```
Only the reverse proxy publishes host ports (80/443). PostgreSQL, MinIO, the control plane and
ingestion stay on the internal network, reachable only through explicit proxy routes.

## 10. Health verification
```
deploy/production/status.sh --env-file /etc/aithernet/hosted.env
```
All services should report `healthy`. The proxy waits on control-plane + ingestion + frontend
**health** (not mere container start) before it is considered up.

## 11. Backup
```
deploy/production/backup.sh --env-file /etc/aithernet/hosted.env
```
Writes a timestamped logical JSON dump into the `backups` volume. Copy it off-host on a schedule.

## 12. Restore
```
deploy/production/restore.sh --env-file /etc/aithernet/hosted.env /path/to/aithernet-backup.json
```
DESTRUCTIVE: clears and reloads all hosted tables from the backup (requires `yes` confirmation).

## 13. Update
```
git pull
deploy/production/backup.sh  --env-file /etc/aithernet/hosted.env
deploy/production/build.sh   --env-file /etc/aithernet/hosted.env
deploy/production/migrate.sh --env-file /etc/aithernet/hosted.env
deploy/production/up.sh      --env-file /etc/aithernet/hosted.env
deploy/production/status.sh  --env-file /etc/aithernet/hosted.env
```

## 14. Rollback
```
git checkout <previous-tag>
deploy/production/build.sh   --env-file /etc/aithernet/hosted.env
deploy/production/up.sh      --env-file /etc/aithernet/hosted.env
# If a migration must be undone, restore the pre-update backup:
deploy/production/restore.sh --env-file /etc/aithernet/hosted.env /path/to/pre-update-backup.json
```

## 15. Shutdown
```
deploy/production/down.sh --env-file /etc/aithernet/hosted.env             # keep volumes
deploy/production/down.sh --env-file /etc/aithernet/hosted.env --volumes   # DELETE volumes (confirm)
```

## 16. Log inspection
```
deploy/production/logs.sh --env-file /etc/aithernet/hosted.env control-plane --since 30m
deploy/production/logs.sh --env-file /etc/aithernet/hosted.env             # all services
```

## Local loopback smoke (no domain/TLS)
Use the loopback override + a development env file (HTTP on `http://localhost:8080`):
```
docker compose --env-file <dev.env> \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.local.yml up -d
```
Set `AITHERNET_HOSTED_ENV=development`, loopback `*_BASE_URL`, `AITHERNET_HOSTED_EMAIL=development`,
`AITHERNET_HOSTED_STORAGE_BACKEND=local`, `AITHERNET_HOSTED_COOKIE_SECURE=0` in `<dev.env>`.

This runbook uses placeholder domains only and contains no real legal documents or credentials.
