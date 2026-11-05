#!/bin/sh
# What a deploy does for the hub after it has put new files in /opt/emori.
#
#   hub-refresh.sh addons     install the shipped add-ons for the hub container, when the
#                             hub image is here and deploy/hub/hub-addons.sh exists
#   hub-refresh.sh restart    restart a running hub onto the checkout just shipped, but only
#                             when the image it runs was built from that checkout, and never
#                             with a session in progress
#
# The hub's own code lives in the image and the adapter core it imports lives in the
# bind-mounted checkout. A deploy copies the core first and the image second, so a deploy that
# stops between the two would start a new core under an old image. The image carries the hash
# of the checkout it was built from (label org.pokamind.emori.hub-source); a restart is
# issued only when that equals the hash the deploy shipped, and otherwise the running hub is
# left alone with a line saying to deploy again.
#
# The shipped hash comes from EMORI_HUB_SOURCE_EXPECTED, else from the first line of
# artifacts/hub-source.expected. Neither present counts as a mismatch.
#
# Nothing here fails a deploy: every outcome is a printed line and exit status 0. With no hub
# unit installed, or with the host application running the gateway, it does nothing.
#
# EMORI_HUB_IMAGE, EMORI_ROOT and EMORI_HUB_UNIT exist so the tests can point it elsewhere.
set -u

ROOT="${EMORI_ROOT:-/opt/emori}"
IMAGE="${EMORI_HUB_IMAGE:-emori/hub:robot}"
UNIT="${EMORI_HUB_UNIT:-emori-hub}"
LABEL=org.pokamind.emori.hub-source
cd "$ROOT" 2>/dev/null || exit 0

have_image() { sudo -n docker image inspect "$IMAGE" >/dev/null 2>&1; }

expected() {
    if [ -n "${EMORI_HUB_SOURCE_EXPECTED:-}" ]; then
        printf '%s' "$EMORI_HUB_SOURCE_EXPECTED"
    elif [ -f artifacts/hub-source.expected ]; then
        head -n 1 artifacts/hub-source.expected | tr -d '\r \t'
    fi
}

actual() {
    sudo -n docker image inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$IMAGE" 2>/dev/null \
        | tr -d '\r \t'
}

do_addons() {
    [ -f deploy/hub/hub-addons.sh ] || return 0
    have_image || return 0
    if ! bash deploy/hub/hub-addons.sh; then
        echo "  !! the add-ons could not be installed for the hub container; the hub runs without them"
    fi
    return 0
}

# NAME: its last value in the env files, in the order the programs rank them.
setting() {
    for file in .env configs/site/settings.env secrets/site.env secrets/admin.env; do
        [ -r "$file" ] && grep -E "^$1=" "$file" 2>/dev/null
    done | tail -n 1 | cut -d= -f2- | tr -d "\r\"' "
}

# 0 when the running hub has something a restart would interrupt: a session, a training lease
# or an add-on mode. Asked the way hub-stack.sh asks: the summary first, because it does not touch
# the robot, while /api/status and /healthz ask it and can take seconds with a session running.
# Only a hub that answers none of them has nothing to interrupt, and one that answers and cannot
# say counts as busy. The token goes to curl on stdin, so it is in no argument list.
busy() {
    port="$(setting HOSTAPP_PORT)"
    port="${port:-8080}"
    token="$(setting EMORI_ADMIN_TOKEN)"
    case "$token" in
        ''|*[!A-Za-z0-9._~+/=-]*) ;;
        *)
            if body="$(printf 'header = "X-EMORI-Admin-Token: %s"\n' "$token" \
                | curl -fsS -m 3 -K - "http://127.0.0.1:$port/api/admin/summary" 2>/dev/null)"; then
                case "$body" in
                    *'"idle":false'*|*'"idle": false'*) return 0 ;;
                    *'"session_active":true'*|*'"session_active": true'*) return 0 ;;
                    *'"idle":true'*|*'"idle": true'*) return 1 ;;
                esac
            fi ;;
    esac
    if body="$(curl -fsS -m 3 "http://127.0.0.1:$port/api/status" 2>/dev/null)"; then
        case "$body" in
            *'"session":null'*|*'"session": null'*) return 1 ;;
        esac
        return 0
    fi
    # Longer than the robot call behind it, and twice: a wrong "nothing to interrupt" ends a session.
    for _ in 1 2; do
        curl -fsS -m 8 -o /dev/null "http://127.0.0.1:$port/healthz" 2>/dev/null && return 0
    done
    return 1
}

do_restart() {
    # try-restart semantics: a hub that is not running (the host application runs the
    # gateway, or the unit was stopped on purpose) stays as it is.
    systemctl is-active --quiet "$UNIT" 2>/dev/null || return 0
    want="$(expected)"
    have="$(actual)"
    case "$have" in '<novalue>'|'<no value>') have= ;; esac
    if [ -n "$want" ] && [ "$want" = "$have" ]; then
        if busy; then
            echo "  !! $UNIT left running: a session is in progress, or it cannot say, and a restart would end it."
            echo "  !! The new code runs from its next restart: sudo systemctl restart $UNIT"
        elif sudo -n systemctl try-restart "$UNIT"; then
            echo "  restarted $UNIT onto the code just installed"
        else
            echo "  !! $UNIT did not restart cleanly: sudo systemctl status $UNIT"
        fi
    else
        echo "  !! $UNIT left running: its image (${have:-unlabelled}) was not built from the code"
        echo "  !! just installed (${want:-no hash shipped}). Deploy again so the image is rebuilt."
    fi
    return 0
}

case "${1:-}" in
    addons) do_addons ;;
    restart) do_restart ;;
    *) echo "usage: $0 addons | restart" >&2; exit 2 ;;
esac
exit 0
