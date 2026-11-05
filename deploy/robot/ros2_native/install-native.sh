#!/usr/bin/env bash
# Install the v2 ROS 2 graph NATIVELY on the Reachy Mini CM4 -- no Docker, no daemon.
#
# This is the route for a robot where the ~1.4 GB container image, the Docker daemon and
# the device passthrough are not wanted. The container route
# (deploy/ros2/docker-compose.v2.robot.yml) is the lower-risk one and is what the runbook
# recommends; read README.md in this directory before choosing.
#
#   sudo -u pollen bash deploy/robot/ros2_native/install-native.sh
#
# WHAT IT REPLACES. reachy-mini-daemon. Not "runs beside": reachy_mini_ros2 opens the
# same /dev/ttyAMA3 and binds the same :8000, so the daemon unit is disabled and EMORI's
# --no-media drop-in removed. The hostapp keeps REACHY_BASE_URL=http://127.0.0.1:8000
# and is not touched at all -- the REST shim answers there instead.
#
# THIS SCRIPT HAS NEVER BEEN RUN ON HARDWARE. See README.md section "What was actually
# validated". Read it before running, and run it with a way back in (emori-admin on :8070
# is deliberately independent of every unit this touches).
set -euo pipefail

EMORI_ROOT="${EMORI_ROOT:-/opt/emori}"
# The default MUST stay /opt/emori/ros2: reachy-mini-ros2.service hardcodes
# `source /opt/emori/ros2/.pixi/envs/default/setup.bash` in its ExecStart, because a unit
# that shells out to `pixi run` pays a lock check on every restart. Override both
# together or neither.
ENV_ROOT="${ENV_ROOT:-${EMORI_ROOT}/ros2}"
PIXI_BIN="${PIXI_BIN:-$HOME/.pixi/bin/pixi}"
# ros4hri's interface package. RoboStack does not build ros-jazzy-hri-msgs for any
# platform, so on this route it is a source package like emori_msgs. Pinned to a tag
# rather than a branch: an interface that changes under a running graph is a wire break.
HRI_MSGS_REPO="${HRI_MSGS_REPO:-https://github.com/ros4hri/hri_msgs.git}"
HRI_MSGS_REF="${HRI_MSGS_REF:-2.3.2}"

say() { echo "== $*"; }

# 0. Guards. Every one of these is a failure that otherwise shows up an hour later as
#    "the motors do not move" with no obvious cause.
say "[0/8] guards"
if [ ! -d "${EMORI_ROOT}/ros2_ws/src" ]; then
  echo "  !! ${EMORI_ROOT}/ros2_ws/src is missing -- deploy the EMORI checkout first." >&2
  exit 1
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "  !  uname -m is $(uname -m), not aarch64. Continuing, but the pixi lock's"
  echo "     linux-aarch64 half is the one that was reasoned about."
fi
if [ ! -x "${PIXI_BIN}" ] && ! command -v pixi >/dev/null 2>&1; then
  echo "  !! pixi not found. Install it for this user (no root needed):" >&2
  echo "       curl -fsSL https://pixi.sh/install.sh | bash" >&2
  echo "     then re-run, or set PIXI_BIN=/path/to/pixi." >&2
  exit 1
fi
command -v "${PIXI_BIN}" >/dev/null 2>&1 || PIXI_BIN="$(command -v pixi)"
say "  pixi: $(${PIXI_BIN} --version)"

# 1. The environment. `pixi install` reads pixi.lock when it is present and resolves
#    from scratch when it is not; either way the env lands under ${ENV_ROOT}/.pixi.
say "[1/8] RoboStack environment in ${ENV_ROOT}"
mkdir -p "${ENV_ROOT}"
cp "$(dirname "$0")/pixi.toml" "${ENV_ROOT}/pixi.toml"
if [ -f "$(dirname "$0")/pixi.lock" ]; then
  cp "$(dirname "$0")/pixi.lock" "${ENV_ROOT}/pixi.lock"
  say "  using the committed lock file"
else
  say "  no pixi.lock committed -- resolving from the channels (slower, and the"
  say "  versions you get are today's, not the ones this was written against)"
fi
( cd "${ENV_ROOT}" && "${PIXI_BIN}" install )
ENV_PREFIX="${ENV_ROOT}/.pixi/envs/default"
[ -x "${ENV_PREFIX}/bin/ros2" ] || { echo "  !! no ros2 at ${ENV_PREFIX}/bin" >&2; exit 1; }

# 2. The robot SDK, into that environment's Python.
#    NOT a [pypi-dependencies] entry in pixi.toml -- see the long comment there. The
#    short version: pixi cannot resolve PyPI for a platform it cannot execute, and
#    PyGObject (which reachy-mini requires) publishes no aarch64 wheel at all, so it and
#    its pycairo dependency are COMPILED HERE. That needs a toolchain and two sets of
#    headers, and the failure without them is a wall of meson output that never mentions
#    the missing package by name -- which is why they are installed rather than assumed.
#    No [mujoco] extra: REACHY_BACKEND=robot never imports it and it is ~300 MB.
say "[2/8] build deps for PyGObject/pycairo, then reachy-mini 1.10.0 (no mujoco extra)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential pkg-config libcairo2-dev libgirepository1.0-dev \
    gir1.2-gstreamer-1.0
"${ENV_PREFIX}/bin/python" -m pip install --no-cache-dir "reachy-mini==1.10.0"

# 3. The EMORI core. emori_skill_native imports emori_adapter.behavior.cues; the three
#    publishable ROS packages do not import it at all and never will.
say "[3/8] the EMORI core into the same environment"
"${ENV_PREFIX}/bin/python" -m pip install --no-cache-dir "${EMORI_ROOT}[pokamind]"

# 4. The emotions library, into THIS user's HuggingFace cache.
#    Same warm-up the v2 container image does at build time (Dockerfile.v2) and the same
#    one the node does at startup, and it is here for the startup one's sake: preloading
#    85 moves happens BEFORE the REST shim binds :8000, so on a cold cache the first boot
#    after an install holds the hostapp's REACHY_BASE_URL down for the length of a
#    dataset download over a venue's wifi -- or fails outright on an air-gapped robot and
#    leaves every recorded move to fail one at a time, later, in front of someone.
#    Doing it here makes that a one-off cost during an install nobody is watching.
#    Non-fatal: an install must not fail because HuggingFace was unreachable.
say "[4/8] warming the emotions library cache (85 moves, ~a minute on a cold cache)"
EMOTIONS_DATASET="${EMOTIONS_DATASET:-pollen-robotics/reachy-mini-emotions-library}"
if "${ENV_PREFIX}/bin/python" - "${EMOTIONS_DATASET}" <<'PYWARM'
import sys
from reachy_mini.motion.recorded_move import RecordedMoves
print(f"  {len(RecordedMoves(sys.argv[1]).list_moves())} moves cached")
PYWARM
then
  say "  emotions library cached"
else
  say "  !  could not warm the emotions cache (no network?). The node will still start;"
  say "     the FIRST recorded move will be slow, or fail if this robot is air-gapped."
fi

# 5. hri_msgs from source (see the comment on HRI_MSGS_REPO).
say "[5/8] hri_msgs ${HRI_MSGS_REF} into the workspace"
if [ -d "${EMORI_ROOT}/ros2_ws/src/hri_msgs" ]; then
  say "  already present"
else
  git clone --depth 1 --branch "${HRI_MSGS_REF}" "${HRI_MSGS_REPO}" \
      "${EMORI_ROOT}/ros2_ws/src/hri_msgs"
fi

# 6. Build the workspace inside the environment.
#    The packages live in ros2_ws/src/open/ and ros2_ws/src/emori/. A deploy extracts
#    the repository over /opt/emori and never deletes, so a robot deployed from an older
#    layout still carries top-level ros2_ws/src/<pkg> copies, and colcon refuses a
#    workspace with two packages of one name. Remove only a top-level copy whose
#    replacement is on disk.
say "[6/8] colcon build"
for pkg_dir in "${EMORI_ROOT}"/ros2_ws/src/open/* "${EMORI_ROOT}"/ros2_ws/src/emori/*; do
  stale="${EMORI_ROOT}/ros2_ws/src/$(basename "${pkg_dir}")"
  if [ -f "${pkg_dir}/package.xml" ] && [ -d "${stale}" ]; then
    say "  removing pre-split copy ${stale}"
    rm -rf "${stale}"
  fi
done
( cd "${EMORI_ROOT}/ros2_ws" \
  && source "${ENV_PREFIX}/setup.bash" \
  && "${ENV_PREFIX}/bin/colcon" build --event-handlers console_direct+ )

# The unit's ExecStart sources EXACTLY these two files, in this order, and nothing else:
#   1. the RoboStack environment prefix. `setup.bash` at the env root is what RoboStack's
#      ros-<distro>-ros-workspace conda package installs (the same file sourced three
#      lines above for the colcon build), and it is the name RoboStack's own "Getting
#      started" documents for a non-`pixi shell` activation;
#   2. the colcon overlay, which is where emori_adapter_bringup and reachy_mini_ros2 live.
# Checked here rather than discovered at the first `systemctl start`, where the failure
# is `Package 'emori_adapter_bringup' not found` in a journal nobody is tailing yet.
for setup_file in "${ENV_PREFIX}/setup.bash" "${EMORI_ROOT}/ros2_ws/install/setup.bash"; do
  [ -f "${setup_file}" ] || {
    echo "  !! ${setup_file} is missing -- reachy-mini-ros2.service sources it and will" >&2
    echo "     fail to resolve emori_adapter_bringup without it." >&2
    exit 1
  }
  say "  unit will source: ${setup_file}"
done

# 7. Device permissions. The servo bus is root:dialout, /dev/video40 is root:video.
#    Group membership is read at LOGIN, so the unit has to be (re)started after this --
#    which step 7 does, and which is why this is step 6.
say "[7/8] device groups for pollen"
sudo usermod -aG dialout,video pollen
say "  pollen is now in: $(id -nG pollen)"
say "  (a login shell opened before this will still not see the new groups)"

# 8. The units. The daemon goes first: two processes on one serial bus is not a degraded
#    robot, it is a robot where the motors belong to whichever one won the race.
say "[8/8] systemd"
sudo systemctl disable --now reachy-mini-daemon || true
sudo rm -f /etc/systemd/system/reachy-mini-daemon.service.d/override.conf
sudo rmdir /etc/systemd/system/reachy-mini-daemon.service.d 2>/dev/null || true
sudo cp "$(dirname "$0")/reachy-mini-ros2.service" \
        /etc/systemd/system/reachy-mini-ros2.service
sudo systemctl daemon-reload
sudo systemctl enable --now reachy-mini-ros2

echo
say "waiting for the REST shim on :8000 (the hostapp's unchanged REACHY_BASE_URL)"
for _ in $(seq 1 40); do
  backend=$(curl -s -m 3 http://127.0.0.1:8000/api/daemon/status 2>/dev/null \
            | python3 -c 'import sys,json;print(json.load(sys.stdin).get("backend"))' \
            2>/dev/null || true)
  [ "$backend" = "reachy_mini_ros2" ] && break
  sleep 2
done
echo "daemon status : $(curl -s -m 5 http://127.0.0.1:8000/api/daemon/status || echo UNREACHABLE)"
echo "motors        : $(curl -s -m 5 http://127.0.0.1:8000/api/motors/status || echo UNREACHABLE)"
echo "ros nodes     : $("${ENV_PREFIX}/bin/ros2" node list 2>/dev/null | tr '\n' ' ' || echo UNLISTABLE)"
echo
echo "If 'backend' above is not reachy_mini_ros2, the daemon is still holding :8000:"
echo "  sudo systemctl status reachy-mini-daemon reachy-mini-ros2"
echo "  journalctl -u reachy-mini-ros2 -n 100 --no-pager"
echo
echo "To go BACK to v1 (the daemon + the containerised observer graph):"
echo "  sudo systemctl disable --now reachy-mini-ros2"
echo "  sudo cp ${EMORI_ROOT}/deploy/robot/reachy-mini-daemon-override.conf \\"
echo "       /etc/systemd/system/reachy-mini-daemon.service.d/override.conf"
echo "  sudo systemctl daemon-reload && sudo systemctl enable --now reachy-mini-daemon"
