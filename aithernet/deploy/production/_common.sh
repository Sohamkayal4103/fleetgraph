#!/usr/bin/env bash
# Shared helpers for the Aithernet production operator scripts (Stage 14F).
# Sourced by every deploy/production/*.sh. Deterministic, fail-fast, secret-safe.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/docker-compose.yml"
PROJECT="${AITHERNET_COMPOSE_PROJECT:-aithernet}"
# Default env-file path is documented; override with --env-file or the env var.
ENV_FILE="${AITHERNET_HOSTED_ENV_FILE:-/etc/aithernet/hosted.env}"

# parse_common <args...> : consumes a leading `--env-file PATH`; leaves the rest in REST[].
REST=()
parse_common() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --env-file) ENV_FILE="${2:?--env-file requires a path}"; shift 2 ;;
      *) REST+=("$1"); shift ;;
    esac
  done
  [[ -f "${COMPOSE_FILE}" ]] || { echo "ERROR: compose file missing: ${COMPOSE_FILE}" >&2; exit 1; }
  [[ -f "${ENV_FILE}" ]] || {
    echo "ERROR: env file not found: ${ENV_FILE}" >&2
    echo "       pass --env-file PATH or set AITHERNET_HOSTED_ENV_FILE" >&2
    exit 1
  }
}

# dc <compose-args...> : run docker compose with the operator env file + tracked compose file,
# plus any overlay compose files named in AITHERNET_COMPOSE_OVERLAY (space-separated; relative
# names are resolved under deploy/compose/). Empty overlay => identical to the base behaviour, so
# existing local/lan callers are unaffected.
dc() {
  local files=( -f "${COMPOSE_FILE}" ) f
  for f in ${AITHERNET_COMPOSE_OVERLAY:-}; do
    case "${f}" in /*) : ;; *) f="${REPO_ROOT}/deploy/compose/${f}" ;; esac
    [[ -f "${f}" ]] || { echo "ERROR: overlay compose file missing: ${f}" >&2; exit 1; }
    files+=( -f "${f}" )
  done
  docker compose --env-file "${ENV_FILE}" -p "${PROJECT}" "${files[@]}" "$@"
}

# confirm <prompt> : require an explicit "yes" for destructive actions (skipped with --yes).
confirm() {
  local prompt="$1"
  for a in "${REST[@]:-}"; do [[ "${a}" == "--yes" || "${a}" == "-y" ]] && return 0; done
  printf '%s [type "yes" to proceed] ' "${prompt}" >&2
  local ans=""; read -r ans || true
  [[ "${ans}" == "yes" ]] || { echo "aborted." >&2; exit 1; }
}
