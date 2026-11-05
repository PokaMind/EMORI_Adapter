#!/usr/bin/env bash
# Put this checkout's robot node Python into the motor image already on the robot.
#
#   bash deploy/robot/ros2_native/motor-overlay.sh            # build the layer
#   bash deploy/robot/ros2_native/motor-overlay.sh --apply    # build it, and restart the node
#   bash deploy/robot/ros2_native/motor-overlay.sh --rollback # back to the image underneath
#   bash deploy/robot/ros2_native/motor-overlay.sh --delta DIR [--apply] [--source HASH]
#
# The motor image is 1.7 GB and a rebuild takes 45 minutes off the robot, so a change to the
# node's Python goes in as one small layer on top. The first run tags the image it found as
# :robot-base, every later layer replaces the last one on that same base, and --rollback goes
# back to it. Restarting the node parks and wakes the motors, so it refuses during a session.
#
# A change to a message or service needs compiled interfaces, which the robot does not build.
# `scripts/build_hub_payload.py --layers motor` compiles them on the computer into
# motor/install-delta/ against the robot's own filesystem. Unpack that payload on the robot and
# pass its motor/ folder as DIR: the layer then holds that install tree whole, node Python
# included, instead of the five files copied from this checkout. That image is also tagged
# :robot-interfaces, and from then on a plain run builds on it, because the node's Python needs
# those interfaces: going back to :robot-base would stop it serving the hub. --rollback goes
# back to :robot-base from either form, and removes that tag.
#
# Exit: 0 done | 2 usage | 3 refused, nothing changed | 4 the build failed | 6 the node did not
#       come up on the new layer, and is back on the image it replaced
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

IMAGE=emori/ros2-jazzy-v2:robot
BASE=emori/ros2-jazzy-v2:robot-base
INTERFACES=emori/ros2-jazzy-v2:robot-interfaces
SOURCE=ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2
SITE=reachy_mini_ros2/lib/python3.12/site-packages/reachy_mini_ros2
FILES=(arbiter.py rest_shim.py robot_node.py ros_surface.py state_snapshot.py)
MIN_FREE_MB=300

APPLY=no
ROLLBACK=no
DELTA=""
SOURCE_HASH=""
usage() { echo "usage: $0 [--apply | --rollback | --delta DIR [--apply] [--source HASH]]" >&2; exit 2; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=yes ;;
    --rollback) ROLLBACK=yes; APPLY=yes ;;
    --delta) [ "$#" -ge 2 ] || usage; DELTA="$2"; shift ;;
    # What the deploy reads back to know this motor layer matches its checkout.
    --source) [ "$#" -ge 2 ] || usage; SOURCE_HASH="$2"; shift ;;
    *) usage ;;
  esac
  shift
done
if [ "$ROLLBACK" = yes ] && [ -n "$DELTA" ]; then usage; fi

refuse() { printf '!! %s\n' "$*" >&2; exit 3; }

# The compiled install tree is checked before anything else, so a wrong folder changes nothing.
if [ -n "$DELTA" ]; then
  [ -d "$DELTA/install-delta" ] || refuse "$DELTA holds no install-delta/ folder"
  [ -d "$DELTA/install-delta/reachy_mini_ros2" ] \
    || refuse "$DELTA/install-delta holds no reachy_mini_ros2: it is not a motor layer"
fi

if [ "$APPLY" = yes ] && bash deploy/robot/session-busy.sh; then
  refuse "a session is running, and restarting the node would park the motors under it"
fi

if [ "$ROLLBACK" = yes ]; then
  sudo -n docker image inspect "$BASE" >/dev/null 2>&1 || refuse "there is no $BASE to go back to"
  sudo -n docker tag "$BASE" "$IMAGE"
else
  sudo -n docker image inspect "$IMAGE" >/dev/null 2>&1 || refuse "there is no $IMAGE to build on"
  free_mb="$(df -m --output=avail "$ROOT" | tail -n1 | tr -d ' ')"
  [ "$free_mb" -ge "$MIN_FREE_MB" ] || refuse "$free_mb MB free, under $MIN_FREE_MB MB"
  sudo -n docker image inspect "$BASE" >/dev/null 2>&1 || sudo -n docker tag "$IMAGE" "$BASE"

  context="$(mktemp -d)"
  trap 'rm -rf "$context"' EXIT
  interfaces=none
  from="$BASE"
  if [ -n "$DELTA" ]; then
    cp -a "$DELTA/install-delta" "$context/install-delta"
    interfaces="$(cd "$context/install-delta" && find . -type f -print0 | sort -z \
      | xargs -0 sha256sum | sha256sum | cut -c1-16)"
  else
    if sudo -n docker image inspect "$INTERFACES" >/dev/null 2>&1; then
      from="$INTERFACES"
      interfaces="$(sudo -n docker image inspect \
        -f '{{ index .Config.Labels "org.pokamind.emori.v2-interfaces" }}' "$INTERFACES")"
    fi
    mkdir -p "$context/install-delta/$SITE"
    for file in "${FILES[@]}"; do
      cp "$SOURCE/$file" "$context/install-delta/$SITE/$file"
    done
  fi
  commit="$(grep -E '^commit=' artifacts/last-deploy.env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  displaced="$(sudo -n docker image inspect -f '{{.Id}}' "$IMAGE")"
  if ! sudo -n docker build --pull=false --build-arg "BASE=$from" \
      --build-arg "EMORI_V2_SOURCE=${commit:-unknown}" \
      --build-arg "EMORI_MOTOR_SOURCE=$SOURCE_HASH" \
      --build-arg "EMORI_V2_INTERFACES=$interfaces" \
      -f deploy/ros2/Dockerfile.v2.robot.delta -t "$IMAGE" "$context"; then
    echo "!! the build failed: $IMAGE is what it was" >&2
    exit 4
  fi
  old_interfaces="$(sudo -n docker image inspect -f '{{.Id}}' "$INTERFACES" 2>/dev/null || true)"
  if [ -n "$DELTA" ]; then
    sudo -n docker tag "$IMAGE" "$INTERFACES"
  fi
fi

echo "motor image: $(sudo -n docker image inspect -f '{{.Id}}' "$IMAGE" | cut -c8-19)"
if [ "$APPLY" = yes ] && ! bash deploy/robot/ros2_native/v2-stack.sh apply v2; then
  if [ "$ROLLBACK" = yes ]; then
    exit 6
  fi
  # A layer that does not run must not stay tagged as what this robot runs: the next deploy
  # would read its label, call it current and never try again.
  echo "!! the node did not come up on this layer: going back to the image it replaced" >&2
  sudo -n docker tag "$displaced" "$IMAGE"
  if [ -n "$DELTA" ] && [ -n "$old_interfaces" ]; then
    sudo -n docker tag "$old_interfaces" "$INTERFACES"
  fi
  bash deploy/robot/ros2_native/v2-stack.sh apply v2 || true
  exit 6
fi
if [ "$ROLLBACK" != yes ]; then
  # Only the layer this one replaced, never a prune: the base and every other tag stay. Kept
  # until now because it is the way back if the new layer does not run.
  if [ "$displaced" != "$(sudo -n docker image inspect -f '{{.Id}}' "$INTERFACES" 2>/dev/null || true)" ]       && [ "$displaced" != "$(sudo -n docker image inspect -f '{{.Id}}' "$BASE")" ]       && [ "$displaced" != "$(sudo -n docker image inspect -f '{{.Id}}' "$IMAGE")" ]; then
    sudo -n docker image rm "$displaced" >/dev/null 2>&1 || true
  fi
fi
# After the restart: a running container holds on to the image behind this tag.
if [ "$ROLLBACK" = yes ] && sudo -n docker image inspect "$INTERFACES" >/dev/null 2>&1; then
  sudo -n docker image rm "$INTERFACES" >/dev/null \
    || { echo "!! could not remove $INTERFACES, so a later plain run would build on it" >&2; exit 4; }
fi
