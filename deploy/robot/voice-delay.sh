#!/usr/bin/env bash
# The kiosk's voice, a moment late. Run by emori-voice-delay.service.
#
# A head takes longer to answer a command than a voice takes to reach the speaker, so a head
# that starts on the first word is late however quickly it is told. This puts a sink called
# emori_voice in front of the speaker with EMORI_VOICE_DELAY_MS on it. The kiosk plays into
# it (start-kiosk.sh), and the hostapp's voice tap listens to it, ahead of the delay, so the
# head hears each word before the room does.
#
# Everything else keeps playing straight to the speaker. With the value unset or 0 there is
# no sink, and this exits at once.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

value() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ' || true; }

DELAY_MS="$(value EMORI_VOICE_DELAY_MS)"
case "${DELAY_MS:-0}" in
  ''|0) exit 0 ;;
  *[!0-9]*) echo "EMORI_VOICE_DELAY_MS must be a whole number of ms: $DELAY_MS" >&2; exit 2 ;;
esac

exec pw-loopback \
  --name emori-voice-delay \
  --channels 2 --channel-map '[ FL FR ]' \
  --delay "$(awk -v ms="$DELAY_MS" 'BEGIN { printf "%.3f", ms / 1000 }')" \
  --capture-props 'media.class=Audio/Sink node.name=emori_voice node.description=EMORI_voice_delay node.virtual=true priority.session=1 priority.driver=1' \
  --playback-props 'node.name=emori_voice.output node.passive=true stream.dont-remix=true'
