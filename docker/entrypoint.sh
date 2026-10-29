#!/bin/sh
# Container entrypoint: never persists secrets.
# GH_TOKEN (read-only) is injected into git via process-environment config
# (GIT_CONFIG_COUNT/KEY/VALUE), which git reads without writing any file.
# gh CLI reads GH_TOKEN natively. Nothing here lands in the image.
set -eu
# Credential helper reads the LIVE $GH_TOKEN on every git invocation, so
# hourly GitHub App installation-token rotation never breaks checkouts.
# Works for PATs and installation tokens alike; nothing is persisted.
git config --global credential.https://github.com.helper \
    '!f() { echo "username=x-access-token"; echo "password=$GH_TOKEN"; }; f' \
    2>/dev/null || true
exec codatiel-i2 "$@"
