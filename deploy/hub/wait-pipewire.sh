#!/bin/sh
# Hold the hub's start until the host's PipeWire socket exists. ExecStartPre of
# emori-hub.service.
#
# The container bind-mounts the socket, and Compose refuses a missing source, so on a cold
# boot that beat PipeWire the hub would fail to start. This waits instead, and gives up
# with a failure after a limit so the unit's restart tries again.
#
# When a voice delay is configured (EMORI_VOICE_DELAY_MS) the voice tap listens to the
# emori_voice sink that emori-voice-delay.service creates on the host, and the container
# cannot create it. This also waits for that sink, but only for a while and without failing:
# without it the tap is unavailable and the head runs a little behind the voice, which is
# how a robot with no delay behaves.
#
# EMORI_HUB_PIPEWIRE_WAIT_S (default 60) bounds the wait for the socket and
# EMORI_HUB_VOICE_SINK_WAIT_S (default 20) the wait for the sink.
set -u

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
SOCKET="$RUNTIME_DIR/pipewire-0"
ROOT="${EMORI_ROOT:-/opt/emori}"
SINK="emori_voice"

# A whole number of seconds, else the default.
seconds() {
    case "$1" in
        ''|*[!0-9]*) echo "$2" ;;
        *) echo "$1" ;;
    esac
}

wait_s="$(seconds "${EMORI_HUB_PIPEWIRE_WAIT_S:-}" 60)"
i=0
while [ ! -S "$SOCKET" ]; do
    if [ "$i" -ge "$wait_s" ]; then
        echo "wait-pipewire: $SOCKET did not appear within ${wait_s} s" >&2
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done

# The value the unit's environment gives, else the one in .env, which the hub reads too.
delay_ms="${EMORI_VOICE_DELAY_MS:-}"
if [ -z "$delay_ms" ]; then
    delay_ms="$(grep -E '^EMORI_VOICE_DELAY_MS=' "$ROOT/.env" 2>/dev/null | tail -n1 \
        | cut -d= -f2- | tr -d '\r" ' || true)"
fi
case "$delay_ms" in
    ''|0|*[!0-9]*) exit 0 ;;
esac
command -v pw-cli >/dev/null 2>&1 || exit 0

sink_wait_s="$(seconds "${EMORI_HUB_VOICE_SINK_WAIT_S:-}" 20)"
i=0
while ! pw-cli ls Node 2>/dev/null | grep -q "node.name = \"$SINK\""; do
    if [ "$i" -ge "$sink_wait_s" ]; then
        echo "wait-pipewire: EMORI_VOICE_DELAY_MS is set and the $SINK sink is not there;" \
            "starting without it" >&2
        exit 0
    fi
    i=$((i + 1))
    sleep 1
done
exit 0
