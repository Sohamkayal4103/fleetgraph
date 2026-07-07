#!/usr/bin/env bash
# Show service status + health (does not print secrets).
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc ps --format "table {{.Service}}\t{{.Status}}\t{{.Health}}"
