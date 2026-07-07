#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# upgrade.sh — safe in-place upgrade of the running production stack.
#
# Order: backup (logical dump, kept in the backups volume) -> validate config ->
# rebuild images from the checked-out source -> migrate -> restart -> health.
# Prints no secrets. Checkout the new tag/commit BEFORE running this.
#
# Rollback (if an upgrade misbehaves):
#   git checkout <previous-tag>
#   deploy/production/build.sh   --env-file <ENV>
#   deploy/production/up.sh      --env-file <ENV>
#   # if a migration must be undone, restore the pre-upgrade backup:
#   deploy/production/restore.sh --env-file <ENV> /path/to/pre-upgrade-backup.json
# -----------------------------------------------------------------------------
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"

echo "[upgrade] project=${PROJECT} overlay=${AITHERNET_COMPOSE_OVERLAY:-<none>}" >&2

echo "[upgrade] 1/5 taking a pre-upgrade backup ..." >&2
dc --profile backup run --rm backup

echo "[upgrade] 2/5 validating config ..." >&2
dc config -q

echo "[upgrade] 3/5 rebuilding images from the current checkout ..." >&2
dc build

echo "[upgrade] 4/5 running migrations ..." >&2
dc --profile tasks run --rm migrate

echo "[upgrade] 5/5 restarting the stack ..." >&2
dc up -d

echo "[upgrade] done. Service health:" >&2
dc ps --format "table {{.Service}}\t{{.Status}}\t{{.Health}}"
echo "[upgrade] Copy the pre-upgrade backup off-host. See the header for rollback steps." >&2
