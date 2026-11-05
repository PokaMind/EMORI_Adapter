#!/usr/bin/env bash
# Run a command as a transient unit in system.slice, as the hostapp service runs.
#
# The hostapp's speed under load depends on sharing a cgroup parent with the kiosk
# browser and the camera bridge, so a recording harness must run in system.slice too.
# The unit takes the service's user, working directory and environment file.
#
#   sudo scripts/golden_in_slice.sh [--dry-run] -- <command> [args...]
#
# Stop emori-hostapp first: both would bind the same port.
#
#   sudo systemctl stop emori-hostapp
#   sudo scripts/golden_in_slice.sh -- /opt/emori/.venv/bin/python \
#       scripts/golden_host.py --out /opt/emori/artifacts/golden/run1
#   sudo systemctl start emori-hostapp
#
# Environment overrides: EMORI_GOLDEN_USER (default pollen), EMORI_GOLDEN_DIR (default
# /opt/emori), EMORI_GOLDEN_ENV (default $EMORI_GOLDEN_DIR/.env), EMORI_GOLDEN_UNIT.
set -euo pipefail

user="${EMORI_GOLDEN_USER:-pollen}"
dir="${EMORI_GOLDEN_DIR:-/opt/emori}"
envfile="${EMORI_GOLDEN_ENV:-$dir/.env}"
unit="${EMORI_GOLDEN_UNIT:-emori-golden-host}"
dry=0

if [ "${1:-}" = "--dry-run" ]; then
  dry=1
  shift
fi
if [ "${1:-}" = "--" ]; then
  shift
fi
if [ "$#" -eq 0 ]; then
  echo "usage: $0 [--dry-run] -- <command> [args...]" >&2
  exit 2
fi

cmd=(
  systemd-run
  --unit="$unit"
  --slice=system.slice
  --uid="$user"
  --working-directory="$dir"
  --property=SupplementaryGroups=audio
  --property=UMask=0077
  --setenv=XDG_RUNTIME_DIR=/run/user/"$(id -u "$user" 2>/dev/null || echo 1000)"
)
if [ -f "$envfile" ]; then
  cmd+=(--property=EnvironmentFile="$envfile")
fi
cmd+=(--wait --pipe --collect -- "$@")

if [ "$dry" -eq 1 ]; then
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi
exec "${cmd[@]}"
