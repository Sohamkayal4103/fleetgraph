#!/usr/bin/env bash
# Start the complete stack in the background.
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc up -d "${REST[@]}"
