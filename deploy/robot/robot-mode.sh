#!/usr/bin/env bash
# Switch the Reachy Mini between EMORI kiosk mode and stock DAEMON/app mode.
#
#   robot-mode emori    -> EMORI kiosk appliance owns the media
#                          (daemon runs --no-media; emori-hostapp or emori-hub, whichever the
#                          flag file selects, + camera-bridge + kiosk up)
#   robot-mode daemon   -> stock daemon owns camera/mic for its own app/dashboard
#                          (EMORI services stopped; --no-media override removed)
#   robot-mode status   -> just print the current mode + service state
#
# The two cannot run at once: EMORI takes the camera (/dev/video0) and mic away from the
# daemon. This flips both the running services AND the boot state, so the last mode you
# select survives a reboot. Passwordless sudo is used internally, so run it plain.
set -euo pipefail

OVERRIDE=/etc/systemd/system/reachy-mini-daemon.service.d/override.conf
DISABLED="$OVERRIDE.disabled"
# The hub's flag file (see emori-hub.service): present means the hub, not the host app,
# runs the kiosk's gateway. Overridable only so the tests can point it elsewhere.
HUB_FLAG="${EMORI_HUB_FLAG:-/opt/emori/HUB_IS_ROS}"
EMORI_UNITS="emori-hostapp emori-camera-bridge emori-kiosk"
# The hub's unit is managed only where it is installed. Both gateway units stay enabled;
# the one the flag does not select is a skipped start.
if systemctl cat emori-hub >/dev/null 2>&1; then
  EMORI_UNITS="emori-hostapp emori-hub emori-camera-bridge emori-kiosk"
fi
GATEWAY_UNIT=emori-hostapp
[ -e "$HUB_FLAG" ] && GATEWAY_UNIT=emori-hub

current_mode() { [ -f "$OVERRIDE" ] && echo emori || echo daemon; }

print_status() {
  echo
  echo "MODE: $(current_mode)"
  local nm
  nm=$(curl -s --max-time 2 localhost:8000/api/daemon/status 2>/dev/null | grep -o '"no_media":[a-z]*' || echo 'no_media:?')
  echo "  reachy-mini-daemon: $(systemctl is-active reachy-mini-daemon)  ($nm)"
  for u in $EMORI_UNITS; do
    echo "  $u: $(systemctl is-active "$u")"
  done
}

case "${1:-status}" in
  emori)
    echo "== switching to EMORI kiosk mode =="
    [ -f "$DISABLED" ] && sudo mv "$DISABLED" "$OVERRIDE"
    sudo systemctl daemon-reload
    sudo systemctl enable $EMORI_UNITS >/dev/null 2>&1 || true
    sudo systemctl restart reachy-mini-daemon      # comes up --no-media, releases camera/mic
    sleep 3
    sudo systemctl restart emori-camera-bridge "$GATEWAY_UNIT" emori-kiosk
    ;;
  daemon|app)
    echo "== switching to stock DAEMON/app mode =="
    sudo systemctl stop $EMORI_UNITS 2>/dev/null || true     # release camera + mic
    sudo systemctl disable $EMORI_UNITS >/dev/null 2>&1 || true
    [ -f "$OVERRIDE" ] && sudo mv "$OVERRIDE" "$DISABLED"
    sudo systemctl daemon-reload
    sudo systemctl restart reachy-mini-daemon      # comes up owning the media
    ;;
  status)
    ;;
  *)
    echo "usage: robot-mode {emori|daemon|status}" >&2
    exit 2
    ;;
esac

print_status
