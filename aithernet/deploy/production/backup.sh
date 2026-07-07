#!/usr/bin/env bash
# Write a timestamped logical backup into the backups volume (one-shot).
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc --profile backup run --rm backup
echo "[backup] written into the '${PROJECT}_backups' volume." >&2
