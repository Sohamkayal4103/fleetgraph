#!/usr/bin/env bash
# Restore the hosted database from a logical backup JSON file (host path).
# DESTRUCTIVE: clears + reloads all hosted tables. Requires explicit confirmation.
#   restore.sh --env-file PATH [--yes] /path/to/aithernet-backup.json
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
BACKUP=""
for a in "${REST[@]:-}"; do [[ "${a}" == --* ]] || BACKUP="${a}"; done
[[ -n "${BACKUP}" && -f "${BACKUP}" ]] || { echo "ERROR: pass a backup file path" >&2; exit 1; }
ABS="$(cd -- "$(dirname -- "${BACKUP}")" && pwd)/$(basename -- "${BACKUP}")"
confirm "Restore will CLEAR and reload all hosted tables from ${BACKUP}."
dc run --rm -v "${ABS}:/restore/backup.json:ro" --entrypoint python control-plane \
  -m services.control_plane.cli restore --input /restore/backup.json
