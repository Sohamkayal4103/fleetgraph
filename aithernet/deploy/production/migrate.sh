#!/usr/bin/env bash
# Run hosted database migrations (one-shot, tasks profile).
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc --profile tasks run --rm migrate
