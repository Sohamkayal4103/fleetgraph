#!/usr/bin/env bash
# Validate the production env file + compose interpolation. FAILS CLOSED (non-zero)
# if any required ${VAR:?} value is missing/empty. Prints no secrets.
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
echo "[check] validating compose config against ${ENV_FILE} ..." >&2
if dc config -q; then
  echo "[check] OK: all required values are present and the compose config is valid." >&2
else
  echo "[check] FAILED: missing/invalid required values (see errors above)." >&2
  exit 1
fi
