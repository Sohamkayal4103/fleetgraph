#!/usr/bin/env bash
# Create the first platform admin (reads AITHERNET_HOSTED_ADMIN_EMAIL/PASSWORD from
# the env file). Refuses once bootstrap has completed. Never prints the password.
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
parse_common "$@"
dc --profile tasks run --rm bootstrap-admin
