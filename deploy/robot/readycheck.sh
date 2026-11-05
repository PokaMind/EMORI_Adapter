#!/usr/bin/env bash
# EMORI readiness self-check on the robot. Exit 0 = ready, 1 = a gate FAILed, 2 = --strict skip.
#   ssh pollen@reachy-mini.local 'bash /opt/emori/deploy/robot/readycheck.sh'
#   ssh pollen@reachy-mini.local 'bash /opt/emori/deploy/robot/readycheck.sh --json'
# Read-only: it starts, restarts and configures nothing.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"   # so the module's load_dotenv() picks up /opt/emori/.env
exec "$ROOT/.venv/bin/python" -m emori_adapter.hostapp.readycheck "$@"
