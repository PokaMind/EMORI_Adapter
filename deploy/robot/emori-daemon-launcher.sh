#!/bin/bash
# EMORI daemon launcher: identical environment to the stock ReachyMiniOS wireless
# launcher.sh, but runs the daemon with --no-media so EMORI owns the camera/mic/
# speaker (Chromium kiosk + the camera bridge own media; the daemon owns MOTION only).
#
# Referenced by the systemd drop-in (reachy-mini-daemon-override.conf). The env below
# was captured from the v1.9.0 image launcher at
# /venvs/mini_daemon/lib/python3.12/site-packages/reachy_mini/daemon/app/services/wireless/launcher.sh
# as shipped in ReachyMiniOS v0.2.3. If a robot OS update changes that file, re-diff
# and update this mirror BEFORE reinstalling the override.
#
# Note: --no-wake-up-on-start is preserved from the image launcher; motors therefore
# boot 'disabled' and the EMORI hostapp enables them at daemon-ready (RobotPlayer).
source /venvs/mini_daemon/bin/activate
export GST_PLUGIN_PATH=$GST_PLUGIN_PATH:/opt/gst-plugins-rs/lib/aarch64-linux-gnu/:/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0/
export PATH=$PATH:/opt/uv
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib/aarch64-linux-gnu/
export LIBCAMERA_IPA_MODULE_PATH=/usr/local/lib/aarch64-linux-gnu/libcamera/ipa
export LIBCAMERA_IPA_CONFIG_PATH=/usr/local/share/libcamera/ipa

# Cap glibc malloc arenas (from the image launcher; see reachy_mini issue #1165).
export MALLOC_ARENA_MAX=2

# Ensure WiFi is not soft-blocked (can happen after a crash or kernel module reload)
sudo rfkill unblock wifi

exec python -u -m reachy_mini.daemon.app.main --wireless-version --no-wake-up-on-start --no-media
