#!/bin/sh
# Create the ROS robot node's loopback capture device, live, idempotently.
#
#   sudo /opt/emori/deploy/robot/ros-loopback.sh [video_nr]
#
# Called from camera-bridge.service's second ExecStartPre (as root) and by hand. A
# missing/empty number is a NO-OP, so a v1 robot's unit behaves identically.
#
# This exists as a SCRIPT rather than an inline ExecStartPre for the same reason
# camera-bridge.sh does: systemd does NOT do "${VAR:-default}" expansion, /bin/sh does.
# systemd expands ${VAR} in an Exec line itself and has no ':-' form at all, so the
# default would either be mangled or passed through literally depending on the version.
set -eu

NR="${1:-${EMORI_CAM_ROS_VIDEO_NR:-}}"
[ -n "$NR" ] || exit 0
[ -e "/dev/video${NR}" ] && exit 0

# v4l2loopback-ctl add, NOT a module reload. `modprobe -r v4l2loopback` would destroy
# /dev/video40 out from under Chromium's open handle -- the kiosk camera dies and only a
# kiosk restart brings it back. v0.15.3 on the robot image supports a live add, so the
# module stays loaded and video40 is never touched.
#   -n ReachyCamROS  card_label, so `v4l2-ctl --list-devices` names it
#   -x 1             exclusive_caps, as video40 has: one capture consumer, the node
#   -b 2             max_buffers=2, matching the persisted modprobe options
v4l2loopback-ctl add -n ReachyCamROS -x 1 -b 2 "$NR"
# A node added live gets no udev pass, so it is born root:root 0600 (measured on the
# robot) while the boot-time video40 is root:video 0660. Match it: the robot node
# runs as root and would not care, but the rehearsal clip producer runs as pollen and
# the unit test for this path must be able to write frames without sudo.
chgrp video "/dev/video${NR}" && chmod 660 "/dev/video${NR}"
