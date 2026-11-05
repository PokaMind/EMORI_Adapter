#!/usr/bin/env bash
# EMORI screenless kiosk: run REAL Chromium on a VIRTUAL display (Xvfb) so it captures
# the robot mic + camera (via /dev/video40) and plays Pokamind's AI voice out the robot
# speaker — with NO monitor attached. `chromium --headless` is NOT used: it emits no
# audio. Loads the EMORI host app, which embeds Pokamind via partner SSO (module_id
# pre-picks the topic, so no on-screen selection is needed).
#
# Prereqs: daemon running with --no-media; emori-camera-bridge running; emori-hostapp up.
set -euo pipefail

# The kiosk lands on '/' — the QR-badge + verbal-menu start page; the session page
# is only ever reached through the SSO mint the hostapp produces.
# kiosk.service passes EMORI_KIOSK_URL explicitly; this fallback is for a hand
# run on the robot, where the appliance's hostapp listens on HOSTAPP_PORT=8090
# (set in .env.example).  The old 8080 fallback silently opened a page that is not
# there and the kiosk came up blank.
URL="${EMORI_KIOSK_URL:-http://localhost:${HOSTAPP_PORT:-8090}/}"
DISP="${DISPLAY_NUM:-:99}"
CHROME="$(command -v chromium-browser || command -v chromium)"

# The voice goes through the delay sink when one is set and up (voice-delay.sh). If it is not
# up in time the voice goes straight to the speaker, which is the older timing and still works.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DELAY_MS="$(grep -E '^EMORI_VOICE_DELAY_MS=' "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ' || true)"
if [ -n "$DELAY_MS" ] && [ "$DELAY_MS" != 0 ]; then
  for _ in $(seq 1 20); do
    # Read whole, then matched: grep -q stops at its match, and under pipefail a listing still
    # being written would then read as no match.
    if grep -q 'node.name = "emori_voice"' <<<"$(pw-cli ls Node 2>/dev/null || true)"; then
      export PULSE_SINK=emori_voice
      break
    fi
    sleep 0.25
  done
fi

export DISPLAY="$DISP"
# Virtual framebuffer (no real screen). 1280x800 is plenty for an unseen page.
pgrep -f "Xvfb $DISP" >/dev/null || (Xvfb "$DISP" -screen 0 1280x800x24 -nolisten tcp & sleep 2)

# NB: no --auto-select-desktop-capture-source flag — it only affects getDisplayMedia
# (screen capture), not getUserMedia; ReachyCam is picked up as the default camera
# because /dev/video40 is the only loopback video node Chromium accepts.
#
# GL: Xvfb has no GPU, so --use-gl=egl can fail to initialize and blank the page.
# SwiftShader (software ANGLE) is the reliable virtual-display choice; if its CPU
# cost is too high on the CM4 (validate during the thermal soak), fall back to
# --disable-gpu instead.
# PipeWire camera MUST be disabled: under a systemd service there is no xdg desktop
# portal, so Chromium's PipeWire camera path fails ("Failed to get a proxy for the
# portal") and the preview stays BLACK instead of falling back to V4L2/ReachyCam.
# (This replaces the manual chrome://flags step.)
exec "$CHROME" \
  --kiosk --start-fullscreen --no-first-run --noerrdialogs --disable-infobars \
  --password-store=basic \
  --use-fake-ui-for-media-stream \
  --autoplay-policy=no-user-gesture-required \
  --use-gl=angle --use-angle=swiftshader --disable-gpu-compositing \
  --disable-features=PipeWireCamera,WebRtcPipeWireCapturer,WebRtcPipeWireCamera \
  --host-resolver-rules="MAP localhost 127.0.0.1" \
  --remote-allow-origins=* \
  --remote-debugging-port="${EMORI_KIOSK_CDP_PORT:-9222}" \
  --user-data-dir="$HOME/.emori-kiosk" \
  "$URL"

# NOTE (Raspberry Pi OS Trixie): Trixie's Chromium prefers PipeWire/libcamera camera
# capture and may IGNORE /dev/video40. Disable it ONCE in this profile:
#   chrome://flags -> search 'pipewire' -> 'PipeWire Camera support' = Disabled -> Relaunch
# (persists in --user-data-dir). Then ReachyCam appears in getUserMedia.
