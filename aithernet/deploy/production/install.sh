#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# install.sh — first-time bring-up of the production stack (idempotent steps).
#
# Order: validate config (fail closed) -> build images -> migrate DB ->
# optional first-admin bootstrap (only when AITHERNET_HOSTED_BOOTSTRAP=1) ->
# start the stack -> show health. Prints no secrets.
#
# For the Cloudflare Tunnel profile, run with the prod project + overlay:
#   AITHERNET_COMPOSE_PROJECT=aithernet_prod \
#   AITHERNET_COMPOSE_OVERLAY=docker-compose.cloudflare.yml \
#   deploy/production/install.sh --env-file /etc/aithernet/hosted.env
# -----------------------------------------------------------------------------
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"

echo "[install] project=${PROJECT} overlay=${AITHERNET_COMPOSE_OVERLAY:-<none>}" >&2

echo "[install] 1/5 validating config (fails closed on placeholders/missing values) ..." >&2
dc config -q

echo "[install] 2/5 building tracked images ..." >&2
dc build

echo "[install] 3/5 running database migrations ..." >&2
dc --profile tasks run --rm migrate

# Bootstrap the first platform admin ONLY when explicitly enabled for first boot.
if grep -qE '^AITHERNET_HOSTED_BOOTSTRAP=1' "${ENV_FILE}"; then
  echo "[install] 4/5 bootstrapping first admin (AITHERNET_HOSTED_BOOTSTRAP=1) ..." >&2
  dc --profile tasks run --rm bootstrap-admin
  echo "[install]     -> set AITHERNET_HOSTED_BOOTSTRAP=0 in ${ENV_FILE} now and re-run check.sh" >&2
else
  echo "[install] 4/5 skipping admin bootstrap (AITHERNET_HOSTED_BOOTSTRAP!=1)" >&2
fi

echo "[install] 5/5 starting the stack ..." >&2
dc up -d

echo "[install] done. Service health:" >&2
dc ps --format "table {{.Service}}\t{{.Status}}\t{{.Health}}"
echo "[install] Only the reverse proxy publishes a port, bound to loopback for cloudflared." >&2
echo "[install] postgres / minio / control-plane / ingestion remain internal + unpublished." >&2
