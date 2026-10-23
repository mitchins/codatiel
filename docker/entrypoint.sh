#!/bin/sh
# Container entrypoint: never persists secrets.
# GH_TOKEN (read-only) is injected into git via process-environment config
# (GIT_CONFIG_COUNT/KEY/VALUE), which git reads without writing any file.
# gh CLI reads GH_TOKEN natively. Nothing here lands in the image.
set -eu
if [ -n "${GH_TOKEN:-}" ]; then
    export GIT_CONFIG_COUNT=1
    # shellcheck disable=SC2034
    export GIT_CONFIG_KEY_0="url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf"
    export GIT_CONFIG_VALUE_0="https://github.com/"
fi
exec codatiel-i2 "$@"
