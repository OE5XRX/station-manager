#!/usr/bin/env bash
# Serial-Contract-Test auf einem Ziel-Host ausführen (Ehrlichkeits-Regel:
# an der Serial-Grenze zählt nur ein grüner Lauf auf echtem CM4).
set -euo pipefail
host="${1:?usage: dev-selftest.sh <host> [slot]}"
slot="${2:-0}"

# `slot` is interpolated into a remote shell command — require a plain integer
# so nothing shell-special can ride along.
if ! [[ "${slot}" =~ ^[0-9]+$ ]]; then
    echo "error: slot must be a non-negative integer, got: '${slot}'" >&2
    exit 2
fi

exec ssh -- "root@${host}" "python -m station_agent selftest serial --slot ${slot}"
