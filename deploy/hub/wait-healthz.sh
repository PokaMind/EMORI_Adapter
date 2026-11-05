#!/bin/sh
# Hold "started" until the hub answers /healthz. ExecStartPost of emori-hub.service.
#
# The unit is a plain process to systemd, active as soon as it forks, so units ordered after
# it (the kiosk) would start against a port that is not open yet. This keeps the start job
# open until the gateway answers, which makes After=emori-hub.service mean ready. A hub that
# does not answer within the limit fails the start, and the unit's restart tries again.
#
# EMORI_HUB_HEALTHZ_WAIT_S (default 60) bounds the wait. The port is HOSTAPP_PORT, with the
# hub's own default.
set -u

port="${HOSTAPP_PORT:-8080}"
case "$port" in
    ''|*[!0-9]*) port=8080 ;;
esac
case "${EMORI_HUB_HEALTHZ_WAIT_S:-}" in
    ''|*[!0-9]*) wait_s=60 ;;
    *) wait_s="$EMORI_HUB_HEALTHZ_WAIT_S" ;;
esac

i=0
while ! curl -fsS -m 3 -o /dev/null "http://127.0.0.1:${port}/healthz" 2>/dev/null; do
    if [ "$i" -ge "$wait_s" ]; then
        echo "wait-healthz: the hub did not answer /healthz on :${port} within ${wait_s} s" >&2
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done
exit 0
