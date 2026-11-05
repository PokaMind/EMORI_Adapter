#!/usr/bin/env bash
# EMORI on-robot setup: bridge the Reachy Mini built-in CSI camera (IMX708) into a
# v4l2loopback device so Chromium getUserMedia (Pokamind) can use it. A libcamera CSI
# camera does NOT appear to Chromium as a normal webcam, so the loopback bridge is
# required (confirmed on Raspberry Pi OS Bookworm & Trixie).
#
# Run on the Reachy Mini Wireless CM4 (user 'pollen'):  sudo bash setup-camera-bridge.sh
set -euo pipefail

# /dev/video10 is bcm2835-codec-decode on ReachyMiniOS (video10-31 are all taken by
# bcm2835/rpi devices); 40 is free. Keep this in sync with camera-bridge.service.
VIDEO_NR="${VIDEO_NR:-40}"
# The SECOND node, for the v2 ROS robot node's face look-at capture. Created at module
# load beside video40 rather than on demand, because `docker start` on a container whose
# `devices:` names a missing node fails outright and only recovers through restart
# backoff -- and camera-bridge.service has no ordering relationship to docker.service.
# systemd-modules-load.service (which reads /etc/modules-load.d) runs long before docker,
# so listing both here is what wins that race with no new unit.
ROS_VIDEO_NR="${EMORI_CAM_ROS_VIDEO_NR:-41}"

echo "[1/4] Installing v4l2loopback + GStreamer libcamera plugins + Chromium/Xvfb kiosk…"
apt-get update
# chromium + xvfb: the kiosk browser and its virtual display — NOT preinstalled on
# ReachyMiniOS (package is 'chromium' on Trixie; there is no 'chromium-browser').
apt-get install -y \
  v4l2loopback-dkms v4l2loopback-utils \
  gstreamer1.0-libcamera gstreamer1.0-tools \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-base \
  chromium xvfb

echo "[2/4] Persisting loopback nodes at /dev/video${VIDEO_NR} + /dev/video${ROS_VIDEO_NR} (exclusive_caps=1, required for Chromium)…"
echo v4l2loopback > /etc/modules-load.d/v4l2loopback.conf
# Two nodes, created together at module load: ${VIDEO_NR} for Chromium and
# ${ROS_VIDEO_NR} for the ROS robot node. video_nr, exclusive_caps and card_label are
# ARRAYS, one entry per node, and they must be the same length. max_buffers is NOT --
# it is one int (v0.15.3 modinfo), and `max_buffers=2,2` fails every load with EINVAL:
# no /dev/video40/41 after a reboot, camera bridge in a restart loop, no kiosk, and
# emori-ros2-v2 not created -- a short
# exclusive_caps array silently leaves the extra node non-exclusive, which is a node
# Chromium will happily list and a second consumer can steal.
echo "options v4l2loopback video_nr=${VIDEO_NR},${ROS_VIDEO_NR} exclusive_caps=1,1 card_label=\"ReachyCam\",\"ReachyCamROS\" max_buffers=2" \
  > /etc/modprobe.d/v4l2loopback.conf

echo "[3/4] (Re)loading the module now…"
modprobe -r v4l2loopback 2>/dev/null || true
modprobe v4l2loopback video_nr="${VIDEO_NR},${ROS_VIDEO_NR}" exclusive_caps=1,1 \
  card_label="ReachyCam","ReachyCamROS" max_buffers=2

echo "[4/4] Sanity checks…"
echo "  cameras seen by libcamera:"; rpicam-hello --list-cameras || libcamera-hello --list-cameras || true
echo "  video nodes:"; ls -l /dev/video* || true
cat <<EOF

Done. Next:
  * Free the camera from the daemon (install deploy/robot/reachy-mini-daemon-override.conf
    so it runs --no-media on every boot), then start the bridge (SYSTEM unit, not --user):
      sudo cp deploy/robot/camera-bridge.service /etc/systemd/system/emori-camera-bridge.service
      sudo systemctl enable --now emori-camera-bridge
  * The bridge feeds /dev/video${VIDEO_NR}; Chromium will only list it WHILE the bridge runs.
  * /dev/video${ROS_VIDEO_NR} is for the v2 ROS robot node (face look-at) and stays EMPTY until
    EMORI_CAM_ROS_VIDEO_NR=${ROS_VIDEO_NR} is set in /etc/default/emori-camera and the bridge is
    restarted. Without that the bridge tees nothing and this node is simply unused.
  * On Trixie, also disable Chromium's 'PipeWire Camera support' (chrome://flags) or it
    ignores the loopback device. See docs/onrobot-runbook.md.
EOF
