#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# healthcheck.sh — external readiness probe against the LOOPBACK origin that
# cloudflared forwards to. Confirms the public health endpoint answers 200 via
# the proxy for the app host, and shows per-service container health. No secrets.
#
# Reads AITHERNET_ORIGIN_HTTP_BIND (default 8087) and AITHERNET_APP_HOST from the
# env file to build the same request cloudflared makes.
# -----------------------------------------------------------------------------
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"

# Pull the two non-secret values we need from the env file (no other values read).
ORIGIN_PORT="$(grep -E '^AITHERNET_ORIGIN_HTTP_BIND=' "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
APP_HOST="$(grep -E '^AITHERNET_APP_HOST=' "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
ORIGIN_PORT="${ORIGIN_PORT:-8087}"
APP_HOST="${APP_HOST:-app.aithernet.online}"
URL="http://127.0.0.1:${ORIGIN_PORT}/health/ready"

echo "[health] container health:" >&2
dc ps --format "table {{.Service}}\t{{.Status}}\t{{.Health}}"

echo "[health] probing origin ${URL} with Host: ${APP_HOST} ..." >&2
code="$(curl -fsS -o /dev/null -w '%{http_code}' -H "Host: ${APP_HOST}" "${URL}" 2>/dev/null || true)"
if [[ "${code}" == "200" ]]; then
  echo "[health] OK: control plane readiness returned 200 through the proxy." >&2
  exit 0
fi
echo "[health] FAIL: readiness probe returned '${code:-no-response}' (expected 200)." >&2
echo "[health] Check: is the stack up? is the origin bound to 127.0.0.1:${ORIGIN_PORT}?" >&2
exit 1
