#!/usr/bin/env bash
# EMORI camera bridge: feed the Reachy Mini CSI camera (IMX708, via libcamera) into the
# v4l2loopback node /dev/video40 so Chromium/Pokamind sees a normal webcam.
#
# Split out of camera-bridge.service so the exposure/brightness knobs are shell
# variables with defaults instead of a frozen ExecStart line: systemd does NOT do
# "${VAR:-default}" expansion, /bin/sh does. Overrides live in /etc/default/emori-camera
# (EnvironmentFile=- in the unit, so a missing file is not an error).
#
# v2 adds an OPTIONAL second consumer: EMORI_CAM_ROS_VIDEO_NR names a second
# v4l2loopback node (41 on the robot) that the ROS robot node captures for face look-at.
# Unset -- the default, and what every v1 robot has -- means no tee and a byte-identical
# pipeline. The device is created by deploy/robot/ros-loopback.sh, not by this script.
set -euo pipefail

VIDEO_NR="${EMORI_CAM_VIDEO_NR:-40}"
W="${EMORI_CAM_WIDTH:-1280}"
H="${EMORI_CAM_HEIGHT:-720}"
# 15 fps doubles the frame duration limit (33 ms -> 66 ms), which is the ceiling
# libcamera's AE can spend on exposure. Indoors that is the cheapest real gain, and
# the idle badge scan does not need 30 fps. Worth confirming on a new robot image that
# AE actually uses it.
FPS="${EMORI_CAM_FPS:-15}"
# videobalance: brightness -1..1 (0 = unchanged), contrast 0..2 (1 = unchanged).
# A software boost, so it amplifies noise as well as signal — it is the FALLBACK for
# when libcamerasrc exposes no exposure/gain control on this build.
BALANCE="${EMORI_CAM_BALANCE:-1}"
BRIGHTNESS="${EMORI_CAM_BRIGHTNESS:-0.05}"
CONTRAST="${EMORI_CAM_CONTRAST:-1.30}"
# Extra libcamerasrc properties, space separated.
#
# The default is FOCUS. The IMX708 is an autofocus module, but libcamera defaults AfMode
# to manual ("it will never perform any action nor move the lens of its own accord"), and
# a bridge that sets no focus control leaves the lens parked wherever it powered up. A
# badge then only decodes at that one distance, so scanning means hunting for an angle.
#   af-mode=2   continuous - rescan on its own as someone walks up
#   af-range=2  full       - includes the closest macro positions ('normal' may not, and
#                            a held-up badge is exactly a close subject)
#   af-speed=1  fast       - a kiosk cannot wait out a leisurely sweep
#
# ...and EXPOSURE. The badge is normally the brightest thing in frame (a phone screen), so
# in a dim room plain AE exposes for the room and blows the badge out. Measured on the
# robot: the black modules washed to luma 66 instead of near zero, and neither jsQR nor
# zxing-cpp could decode it. ae-constraint-mode=1 (highlight priority) pulls exposure back
# only when highlights would clip -- black level 66 -> 1, sharpness 344 -> 3442 on the same
# scene. Deliberately NOT a fixed negative exposure-value: that scored similarly here but
# would under-expose a PRINTED badge in the same dim room, which this adapts to.
# Property NAMES differ between libcamera releases, so every entry here is checked against
# this build's gst-inspect below and silently dropped if absent - an unknown property makes
# gst-launch exit 1 and systemd restart-loop. Override wholesale in /etc/default/emori-camera.
SRC_CONTROLS="${EMORI_CAM_SRC_CONTROLS:-af-mode=2 af-range=2 af-speed=1 ae-constraint-mode=1}"
GST="${EMORI_CAM_GST:-gst-launch-1.0}"
GST_INSPECT="${EMORI_CAM_GST_INSPECT:-gst-inspect-1.0}"

# Keep only the control properties this libcamerasrc build actually exposes.
#
# Probe ONCE into a variable and match with a heredoc. Never `"$GST_INSPECT" ... | grep -q`:
# this script runs under `set -o pipefail`, `grep -q` exits at the first match, and the
# listing is ~100 KiB, so gst-inspect dies on SIGPIPE mid-write and pipefail promotes that
# to a FAILED probe. Every property then reads as missing and the bridge silently drops
# focus -- observed on the robot, where `af-mode` was reported absent by this loop
# while the very same grep matched by hand (no pipefail in an interactive shell).
# A heredoc is not a pipeline, so there is no producer left to signal.
PROPS="$("$GST_INSPECT" libcamerasrc 2>/dev/null || true)"
keep=""
for kv in $SRC_CONTROLS; do
  name="${kv%%=*}"
  if grep -qE "^  ${name} +:" <<EOF
$PROPS
EOF
  then
    keep="$keep $kv"
  else
    echo "camera-bridge: libcamerasrc has no property '${name}' - ignoring" >&2
  fi
done

balance=""
if [ "$BALANCE" = "1" ]; then
  balance="videobalance brightness=${BRIGHTNESS} contrast=${CONTRAST} ! "
fi

# The ROS robot node's own capture device. EMPTY (the default) means no tee at all, so a
# v1 robot's pipeline is character-for-character the single-consumer one.
ROS_NR="${EMORI_CAM_ROS_VIDEO_NR:-}"
ROS_W="${EMORI_CAM_ROS_WIDTH:-320}"
ROS_H="${EMORI_CAM_ROS_HEIGHT:-240}"
ROS_FPS="${EMORI_CAM_ROS_FPS:-10}"

ros_tee=""
ros_branch=""
if [ -n "$ROS_NR" ] && [ -e "/dev/video${ROS_NR}" ]; then
  # leaky=downstream on BOTH branches, and this is the safety property of the whole
  # change: without it a stalled ROS consumer back-pressures the tee and stalls the
  # KIOSK's branch, i.e. face tracking would be able to break the badge scanner. A leaky
  # queue drops old frames instead, which is what both consumers want.
  #
  # NOTE the kiosk branch has never had a queue before this change. A leaky queue can
  # drop a frame the badge scanner would have decoded; max-size-buffers=2 keeps that to
  # ~66 ms of slack at 30 fps and the scanner re-reads continuously. Asserted
  # deliberately in tests/test_camera_bridge.py so nobody removes it by accident and
  # nobody replaces it with a NON-leaky one.
  ros_tee="tee name=camsrc camsrc. ! queue leaky=downstream max-size-buffers=2 ! "
  # add-borders=false: 1280x720 -> 320x240 is an ASPECT CHANGE. With borders it is a
  # letterbox, which moves the principal point and is NOT modelled by the intrinsics;
  # without them it is a deterministic anamorphic squeeze, which IS (x by 320/1280, y by
  # 240/720) -- and which is why fx and fy legitimately differ at 320x240 even though the
  # sensor has square pixels. See docs/ros2-v2-native-robot.md, "the intrinsics".
  # videorate BEFORE videoscale: scale 10 fps, not 30.
  ros_branch=" camsrc. ! queue leaky=downstream max-size-buffers=2 ! videorate ! videoscale add-borders=false ! video/x-raw,width=${ROS_W},height=${ROS_H},framerate=${ROS_FPS}/1 ! videoconvert ! video/x-raw,format=YUY2 ! v4l2sink device=/dev/video${ROS_NR} sync=false"
elif [ -n "$ROS_NR" ]; then
  # Configured but absent is a WARNING, never a failure: the kiosk's camera is the
  # shipped capability and it must come up even when the ROS loopback did not.
  echo "camera-bridge: /dev/video${ROS_NR} is absent - running without the ROS tee. Create it with 'sudo /opt/emori/deploy/robot/ros-loopback.sh ${ROS_NR}'." >&2
fi

# The tee sits IMMEDIATELY after the NV12 caps, so the ROS branch scales the cheap
# sensor-native format instead of a converted one, and so the kiosk branch keeps the
# videobalance/YUY2 chain it has always had.
PIPELINE="libcamerasrc${keep} ! video/x-raw,width=${W},height=${H},framerate=${FPS}/1,format=NV12 ! ${ros_tee}videoconvert ! video/x-raw,format=YUY2 ! ${balance}v4l2sink device=/dev/video${VIDEO_NR} sync=false${ros_branch}"

if [ "${EMORI_CAM_PRINT_PIPELINE:-0}" = "1" ]; then
  echo "$PIPELINE"
  exit 0
fi
exec "$GST" -e $PIPELINE
