#!/usr/bin/env bash
# Exit 0 when the program on the gateway port has something a restart of it or of the motor would
# interrupt: a session, a training lease or an add-on mode. Exit 1 when it has nothing to interrupt.
#
# The summary is asked first, because it does not touch the robot, while /api/status and /healthz
# ask it and can take seconds with a session running. Only a program that answers none of them has
# nothing to interrupt, and one that answers and cannot say counts as busy. The token goes to curl
# on stdin, so it is in no argument list.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

# NAME: its last value in the env files, in the order the programs rank them.
setting() {
  local file
  for file in .env configs/site/settings.env secrets/site.env secrets/admin.env; do
    [ -r "$file" ] && grep -E "^$1=" "$file" 2>/dev/null
  done | tail -n 1 | cut -d= -f2- | tr -d "\r\"' "
}

port="$(setting HOSTAPP_PORT)"
port="${port:-8080}"
token="$(setting EMORI_ADMIN_TOKEN)"
case "$token" in
  ''|*[!A-Za-z0-9._~+/=-]*) ;;
  *)
    if body="$(printf 'header = "X-EMORI-Admin-Token: %s"\n' "$token" \
      | curl -fsS -m 3 -K - "http://127.0.0.1:$port/api/admin/summary" 2>/dev/null)"; then
      case "$body" in
        *'"idle":false'*|*'"idle": false'*) exit 0 ;;
        *'"session_active":true'*|*'"session_active": true'*) exit 0 ;;
        *'"idle":true'*|*'"idle": true'*) exit 1 ;;
      esac
    fi ;;
esac
if body="$(curl -fsS -m 3 "http://127.0.0.1:$port/api/status" 2>/dev/null)"; then
  case "$body" in
    *'"session":null'*|*'"session": null'*) exit 1 ;;
  esac
  exit 0
fi
# Longer than the robot call behind it, and twice: a wrong "nothing to interrupt" ends a session.
for _ in 1 2; do
  curl -fsS -m 8 -o /dev/null "http://127.0.0.1:$port/healthz" 2>/dev/null && exit 0
done
exit 1
