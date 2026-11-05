#!/usr/bin/env bash
# Build the hub image on the robot from a payload the deploy shipped to
# /opt/emori/hub-build. Run by scripts/deploy_to_robot.py; it prints key=value lines.
#
#   hub-build.sh top  <source-hash>                      the hub image only (copies files)
#   hub-build.sh base <source-hash> <base-source-hash>   the base first, then the hub image
#
# The robot never receives a heavy layer: the payload holds files and the build runs here on
# top of the motor image already present. A build that dies leaves nothing behind, because
# the payload and the builder cache are removed from a trap on every exit, and again before
# the next build starts. The image a retag displaces from :prev is removed by id.
#
# Exit: 0 built | 2 usage | 3 refused, nothing changed | 4 the build failed
set -uo pipefail

PAYLOAD=/opt/emori/hub-build
HUB=emori/hub:robot
HUB_PREV=emori/hub:prev
BASE=emori/hub-base:robot
MOTOR=emori/ros2-jazzy-v2:robot
FLOOR_MB=400
NEED_TOP_MB=100
NEED_BASE_MB=563

mode="${1:-}"; source_hash="${2:-}"; base_source="${3:-}"
case "$mode" in
  top) [ "$#" -eq 2 ] || mode="" ;;
  base) [ "$#" -eq 3 ] || mode="" ;;
  *) mode="" ;;
esac
if [ -z "$mode" ] || ! [[ "$source_hash" =~ ^[0-9a-f]{16}$ ]] \
   || { [ "$mode" = base ] && ! [[ "$base_source" =~ ^[0-9a-f]{16}$ ]]; }; then
  echo "usage: $0 top <source> | base <source> <base-source>" >&2
  exit 2
fi

docker() { timeout 1800 sudo -n docker "$@"; }
refuse() { printf '!! %s\n' "$*" >&2; echo "hub_build=refused"; exit 3; }

clean() {
  # A signal ends this script and not its build, because `timeout` puts that in a process group
  # of its own: it would go on for minutes, retag what it built and keep its cache past the prune.
  pkill -TERM -P "$$" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -P "$$" >/dev/null 2>&1 || break
    sleep 1
  done
  rm -rf "$PAYLOAD"
  docker builder prune -f >/dev/null 2>&1 || true
}
trap clean EXIT

docker image inspect "$MOTOR" >/dev/null 2>&1 || refuse "there is no $MOTOR to build on"
[ -f "$PAYLOAD/Dockerfile.hub" ] || refuse "the payload holds no Dockerfile.hub"
if [ "$mode" = top ]; then
  docker image inspect "$BASE" >/dev/null 2>&1 || refuse "there is no $BASE to build on"
  need=$NEED_TOP_MB
else
  [ -d "$PAYLOAD/debs" ] || refuse "the payload holds no debs/"
  need=$NEED_BASE_MB
fi
free_mb="$(df -m --output=avail /opt/emori | tail -n1 | tr -d ' ')"
[ "$free_mb" -ge $((need + FLOOR_MB)) ] \
  || refuse "$free_mb MB free, and this build needs $need MB plus a floor of $FLOOR_MB MB"

# Leftovers of an earlier build that died before its trap ran.
docker builder prune -f >/dev/null 2>&1 || true

if [ "$mode" = base ]; then
  old_base="$(docker image inspect -f '{{.Id}}' "$BASE" 2>/dev/null || true)"
  docker build --pull=false -f "$PAYLOAD/Dockerfile.hub-base" \
    --build-arg "EMORI_HUB_BASE_SOURCE=$base_source" -t "$BASE" "$PAYLOAD" \
    || { echo "hub_build=failed"; exit 4; }
  # The base it displaced, by id, as for :prev below. Images built on it keep the layers they use.
  if [ -n "$old_base" ] && [ "$old_base" != "$(docker image inspect -f '{{.Id}}' "$BASE" 2>/dev/null)" ]; then
    docker rmi "$old_base" >/dev/null 2>&1 || true
  fi
fi

old_prev="$(docker image inspect -f '{{.Id}}' "$HUB_PREV" 2>/dev/null || true)"
if docker image inspect "$HUB" >/dev/null 2>&1; then
  docker tag "$HUB" "$HUB_PREV"
fi
docker build --pull=false -f "$PAYLOAD/Dockerfile.hub" \
  --build-arg "EMORI_HUB_SOURCE=$source_hash" -t "$HUB" "$PAYLOAD" \
  || { echo "hub_build=failed"; exit 4; }

# A rebuild that made the same image has nothing new to go back from: :prev stays what it was.
now_hub="$(docker image inspect -f '{{.Id}}' "$HUB" 2>/dev/null || true)"
now_prev="$(docker image inspect -f '{{.Id}}' "$HUB_PREV" 2>/dev/null || true)"
if [ -n "$old_prev" ] && [ "$now_hub" = "$now_prev" ]; then
  docker tag "$old_prev" "$HUB_PREV"
  now_prev="$old_prev"
fi
# The retag left the image it displaced untagged: remove that one, never a general prune.
if [ -n "$old_prev" ] && [ "$old_prev" != "$now_prev" ]; then
  docker rmi "$old_prev" >/dev/null 2>&1 || true
fi
echo "hub_build=ok"
