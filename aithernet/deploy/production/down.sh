#!/usr/bin/env bash
# Stop the stack. Pass --volumes (or -v) to ALSO remove persistent data volumes
# (DESTRUCTIVE — requires explicit confirmation).
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
WIPE=0
for a in "${REST[@]:-}"; do [[ "${a}" == "--volumes" || "${a}" == "-v" ]] && WIPE=1; done
if [[ "${WIPE}" == "1" ]]; then
  confirm "This will DELETE all persistent volumes (database, blobs, backups)."
  dc down -v
else
  dc down
fi
