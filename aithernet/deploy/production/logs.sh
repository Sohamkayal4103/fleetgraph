#!/usr/bin/env bash
# Tail logs. Extra args (service names, --since, --tail) pass through.
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc logs --no-color "${REST[@]}"
