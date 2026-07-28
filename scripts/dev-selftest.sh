#!/usr/bin/env bash
# Serial-Contract-Test auf einem Ziel-Host ausführen (Ehrlichkeits-Regel:
# an der Serial-Grenze zählt nur ein grüner Lauf auf echtem CM4).
set -euo pipefail
host="${1:?usage: dev-selftest.sh <host> [slot]}"
slot="${2:-0}"
exec ssh "root@${host}" "python -m station_agent selftest serial --slot ${slot}"
