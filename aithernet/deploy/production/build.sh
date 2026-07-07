#!/usr/bin/env bash
# Build all application + frontend images from tracked Dockerfiles.
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc build "${REST[@]}"
