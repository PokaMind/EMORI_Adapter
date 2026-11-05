#!/usr/bin/env bash
# THIS robot's ROS 2 stack, as the one-click deploy leaves it:
#
#   v2  the robot is a ROS 2 node. The container emori-ros2-v2 (Route A of
#       deploy/robot/ros2_native/README.md) owns the motor bus and answers Pollen's daemon
#       API on :8000; Pollen's daemon is fenced off the bus. The default.
#   v1  Pollen's daemon owns the motor bus.
#
#   bash deploy/robot/ros2_native/v2-stack.sh status
#   bash deploy/robot/ros2_native/v2-stack.sh docker         # install Docker (needs internet)
#   bash deploy/robot/ros2_native/v2-stack.sh apply [v1|v2]  # default: .env EMORI_ROS2_STACK, else v2
#
# `apply v2` is the README's "Committed to v2", in its order: the v1 graph and Pollen's
# daemon disabled, the daemon fenced (drop-in + flag file), the container up, then the
# look-at camera tee armed. It needs Docker and the image emori/ros2-jazzy-v2:robot ON the
# robot; scripts/deploy_to_robot.py ships the image before it runs this. Without either,
# the robot stays on v1, which keeps working, and the next deploy tries again. When the
# node does not answer as `reachy_mini_ros2` on :8000 within EMORI_V2_WAIT_S seconds, the
# robot goes straight back to v1: a fence with no node behind it is a robot whose motors
# have no owner.
#
# `apply v1` is the README's "Rolling back to v1", including its EMORI_WITH_ROS2=0 unless a
# v1 graph can run. Its step 0b (EMORI_GESTURE_RESIDUAL=0) is not needed: the hostapp finds
# residual sensing unavailable on v1 by itself.
#
# Prints key=value lines (stack_before, stack, changed, reason) and keeps them in
# artifacts/last-ros2-stack.env for the deploy's last screen.
# While the hub runs the kiosk's gateway (the flag file /opt/emori/HUB_IS_ROS is up), the
# robot cannot go to v1: the hub's robot client talks to the motor node, and v1 has none. So
# `apply v1` refuses (reason hub_mode) until deploy/robot/hub-stack.sh has handed the gateway
# back to the host app. A v2 that fails to come up is the one case that cannot refuse, since
# the motors need an owner: it lowers the flag, stops the hub and starts the host app.
#
# Exit: 0 the robot runs the stack asked for | 2 usage | 5 v2 asked for but not possible
#       yet (no Docker, no image): still v1; or v1 asked for from hub mode; or Docker hangs, and
#       nothing changed
#       | 6 v2 did not come up: back on v1
#       | 7 v1 asked for, and Pollen's daemon did not answer, or the motor container may still be
#       running, so the fence stays up and the daemon off
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"

IMAGE=emori/ros2-jazzy-v2:robot
V1_IMAGE=emori/ros2-humble:latest
CONTAINER=emori-ros2-v2
COMPOSE=(docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml)
SOURCE_LABEL=org.pokamind.emori.v2-source
TEE_NR=41
# Overridable only so the tests can point them somewhere that is not /opt, /etc or a robot.
FENCE="${EMORI_V2_FENCE:-/opt/emori/V2_OWNS_THE_BUS}"
HUB_FLAG="${EMORI_HUB_FLAG:-/opt/emori/HUB_IS_ROS}"
DROPIN_DIR="${EMORI_DAEMON_DROPIN_DIR:-/etc/systemd/system/reachy-mini-daemon.service.d}"
CAM_DEFAULTS="${EMORI_CAMERA_DEFAULTS:-/etc/default/emori-camera}"
# Served by both owners of :8000; only the v2 shim's answer carries a `backend` field.
STATUS_URL="http://127.0.0.1:8000/api/daemon/status"
[ -n "${EMORI_DAEMON_STATUS_URL:-}" ] && STATUS_URL="$EMORI_DAEMON_STATUS_URL"
WAIT_S="${EMORI_V2_WAIT_S:-180}"
ME="$(id -un)"
mkdir -p artifacts
RESULT=artifacts/last-ros2-stack.env

current_stack() { [ -e "$FENCE" ] && echo v2 || echo v1; }

env_stack() {  # .env's EMORI_ROS2_STACK, lower-cased ("" when unset)
  grep -E '^EMORI_ROS2_STACK=' .env 2>/dev/null | tail -n1 | cut -d= -f2- \
    | tr -d "\r\"' " | tr 'A-Z' 'a-z' || true
}

# Docker that gives no answer in this long is hung, not down, and the two are told apart: a hung
# one may still be running the motor container.
DOCKER_WAIT_S="${EMORI_V2_DOCKER_WAIT_S:-30}"
DOCKER_HUNG=no
V1_BLOCKED=""
have_docker() {
  local rc=0
  DOCKER_HUNG=no
  command -v docker >/dev/null 2>&1 || return 1
  sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker info >/dev/null 2>&1 || rc=$?
  case "$rc" in 124|137) DOCKER_HUNG=yes ;; esac
  [ "$rc" = 0 ]
}
have_image() { sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker image inspect "$IMAGE" >/dev/null 2>&1; }

# Docker says the container is not running, or does not exist. No answer says neither.
container_gone() {
  local state rc=0
  state="$(sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" || rc=$?
  case "$rc" in 124|137) return 1 ;; esac
  [ "$state" != true ]
}

container_running() {
  [ "$(sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ]
}

backend() {  # the `backend` field of :8000's status ("" for Pollen's daemon); 1 = no answer
  local body
  body="$(curl -fsS -m 3 "$STATUS_URL" 2>/dev/null)" || return 1
  printf '%s' "$body" | sed -nE 's/.*"backend"[[:space:]]*:[[:space:]]*"([a-z0-9_]+)".*/\1/p'
}

wait_for() {  # v1|v2: until :8000 answers as that stack, at most WAIT_S seconds
  local i answer
  for i in $(seq 1 $(( (WAIT_S + 2) / 3 ))); do
    if answer="$(backend)"; then
      if [ "$1" = v2 ] && [ "$answer" = reachy_mini_ros2 ]; then return 0; fi
      if [ "$1" = v1 ] && [ -z "$answer" ]; then return 0; fi
    fi
    sleep 3
  done
  return 1
}

cam_tee() {  # the look-at tee's loopback number in the bridge's own file ("" = off)
  grep -E '^[[:space:]]*EMORI_CAM_ROS_VIDEO_NR=' "$CAM_DEFAULTS" 2>/dev/null \
    | tail -n1 | cut -d= -f2- | tr -d "\r\"' " || true
}

set_cam_tee() {  # VALUE: the one place the tee is armed (install.sh step 5b)
  if grep -qE '^[[:space:]]*EMORI_CAM_ROS_VIDEO_NR=' "$CAM_DEFAULTS" 2>/dev/null; then
    sudo -n sed -i -E "s/^[[:space:]]*EMORI_CAM_ROS_VIDEO_NR=.*/EMORI_CAM_ROS_VIDEO_NR=$1/" \
      "$CAM_DEFAULTS"
  else
    echo "EMORI_CAM_ROS_VIDEO_NR=$1" | sudo -n tee -a "$CAM_DEFAULTS" >/dev/null
  fi
}

restart_camera() {
  # The kiosk too: a bridge restarted alone leaves the kiosk on black frames.
  sudo -n systemctl try-restart emori-camera-bridge emori-kiosk
}

restart_hostapp() {  # the kiosk is PartOf=emori-hostapp and follows it
  # The hub is deliberately not restarted here: to_v1 runs this, and a hub brought up
  # against a robot with no motor node has nothing to talk to.
  sudo -n systemctl try-restart emori-hostapp emori-admin
}

hub_mode() { [ -e "$HUB_FLAG" ]; }

finish() {  # BEFORE AFTER CHANGED REASON EXIT [SENTENCE]
  printf 'stack_before=%s\nstack=%s\nchanged=%s\nreason=%s\n' "$1" "$2" "$3" "$4" \
    | tee "$RESULT"
  [ -n "${6:-}" ] && printf '  %s\n' "$6"
  exit "$5"
}

# BEFORE: a hung Docker cannot take the motor container down or say that it is gone, so nothing
# may be decided about the bus until it answers.
refuse_if_docker_hung() {
  have_docker && return 0
  [ "$DOCKER_HUNG" = yes ] || return 0
  finish "$1" "$1" no docker_hung 5 \
    "Docker does not answer within ${DOCKER_WAIT_S} s, so nothing changed: sudo systemctl restart docker, then run this again"
}

# --- v1: Pollen's daemon owns the bus --------------------------------------------------

# REASON: the daemon must not open the bus beside a container that may still be running, so the
# fence goes back up and the daemon stays off.
keep_the_fence() {
  V1_BLOCKED="$1"
  sudo -n touch "$FENCE"
  echo "  !! $CONTAINER may still own the bus: Pollen's daemon is not started"
  echo "  !! sudo systemctl restart docker, then: bash deploy/robot/ros2_native/v2-stack.sh apply v1"
}

to_v1() {  # the README's rollback; 0 when Pollen's daemon answers afterwards
  local tee
  if ! have_docker && [ "$DOCKER_HUNG" = yes ]; then
    keep_the_fence docker_hung
    return 1
  fi
  tee="$(cam_tee)"
  if [ -n "$tee" ]; then
    set_cam_tee ""
    restart_camera
    echo "  look-at camera tee off"
    # A readycheck override naming the tee just switched off would FAIL the gaze gate.
    if grep -qE "^EMORI_READYCHECK_ROS_VIDEO_DEVICE=/dev/video${tee}[[:space:]]*$" .env 2>/dev/null; then
      sed -i -E "s|^(EMORI_READYCHECK_ROS_VIDEO_DEVICE=/dev/video${tee})|#\1|" .env
      echo "  .env: EMORI_READYCHECK_ROS_VIDEO_DEVICE commented out (it named that tee)"
    fi
  fi
  # Lower the fence first, or the daemon start below is skipped.
  sudo -n rm -f "$FENCE"
  # The container restarts with Docker, so it is taken down while Docker can do that: the next boot
  # starts Docker, and so does the kiosk's start, through the hub's unit. If Docker is down the
  # container is not running, and it cannot start with the fence down, which is one of its binds.
  # One made before that bind would, and readycheck fails on that.
  have_docker || sudo -n timeout -k 5 60 systemctl start docker >/dev/null 2>&1 || true
  if have_docker; then
    sudo -n timeout -k 5 120 "${COMPOSE[@]}" down >/dev/null 2>&1 \
      || sudo -n timeout -k 5 60 docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if ! container_gone; then
      keep_the_fence container_not_down
      return 1
    fi
  elif [ "$DOCKER_HUNG" = yes ]; then
    keep_the_fence docker_hung
    return 1
  else
    echo "  !! Docker is down, so $CONTAINER is not removed; it cannot start with the fence down."
    echo "  !! When Docker is back: bash deploy/robot/ros2_native/v2-stack.sh apply v1"
  fi
  sudo -n mkdir -p "$DROPIN_DIR"
  sudo -n cp deploy/robot/reachy-mini-daemon-override.conf "$DROPIN_DIR/override.conf"
  sudo -n systemctl daemon-reload
  sudo -n systemctl enable --now reachy-mini-daemon
  wait_for v1 || return 1
  if grep -qE '^EMORI_ROS_MOTION_OWNER=ros' .env 2>/dev/null; then
    sed -i 's/^EMORI_ROS_MOTION_OWNER=.*/EMORI_ROS_MOTION_OWNER=hostapp/' .env
    echo "  expressive motion handed back to the hostapp (EMORI_ROS_MOTION_OWNER=hostapp)"
  fi
  # The v1 graph the commit disabled comes back where its image is still here; with
  # none, EMORI_WITH_ROS2=1 would have readycheck look for a graph that cannot run.
  if systemctl cat emori-ros2 >/dev/null 2>&1 \
     && sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker image inspect "$V1_IMAGE" >/dev/null 2>&1; then
    sudo -n systemctl enable --now emori-ros2 >/dev/null 2>&1 \
      && echo "  the v1 ROS 2 graph (emori-ros2) is back on"
  elif grep -qE '^EMORI_WITH_ROS2=1' .env 2>/dev/null; then
    sed -i 's/^EMORI_WITH_ROS2=1/EMORI_WITH_ROS2=0/' .env
    echo "  .env: EMORI_WITH_ROS2=0 (no v1 ROS 2 graph image on this robot)"
  fi
  restart_hostapp
  return 0
}

apply_v1() {
  local before
  before="$(current_stack)"
  if hub_mode; then
    finish "$before" "$before" no hub_mode 5 \
      "the hub runs the gateway on this robot; run deploy/robot/hub-stack.sh apply hostapp first"
  fi
  refuse_if_docker_hung "$before"
  if [ "$before" = v1 ] && ! container_running; then
    finish v1 v1 no unchanged 0
  fi
  echo "  switching this robot to v1 (Pollen's daemon owns the motor bus)"
  if to_v1; then
    finish "$before" v1 yes switched 0
  fi
  [ -n "$V1_BLOCKED" ] && finish "$before" "$(current_stack)" no "$V1_BLOCKED" 7
  finish "$before" v1 yes daemon_not_up 7 \
    "!! Pollen's daemon did not answer on :8000 within ${WAIT_S} s: sudo systemctl status reachy-mini-daemon"
}

# --- v2: the robot is a ROS 2 node -----------------------------------------------------

v2_failed() {  # BEFORE REASON SENTENCE: back to v1, so the motors keep an owner
  echo "  !! $3"
  echo "     putting the robot back on v1 (Pollen's daemon) so its motors keep an owner"
  # The hub has no motor node to talk to once v1 owns the bus: the gateway goes back to
  # the host app, which drives Pollen's daemon.
  local was_hub=no
  if hub_mode; then
    was_hub=yes
    sudo -n rm -f "$HUB_FLAG"
    sudo -n systemctl stop emori-hub
    sudo -n systemctl daemon-reload
  fi
  # The next `apply v2` runs the hostapp's way: the graph goes back in the motor container.
  [ -f .env ] && sed -i -E 's|^EMORI_HUB_GRAPH=.*|EMORI_HUB_GRAPH=0|' .env
  [ -f deploy/ros2/.env ] \
    && sed -i -E 's|^EMORI_MOTOR_LAUNCH=.*|EMORI_MOTOR_LAUNCH=v2.launch.py|' deploy/ros2/.env
  local daemon_up=1
  to_v1 && daemon_up=0
  if [ "$was_hub" = yes ]; then
    sudo -n systemctl start emori-hostapp
    sudo -n systemctl start --no-block emori-kiosk
  fi
  if [ "$daemon_up" = 0 ]; then
    finish "$1" v1 "$([ "$1" = v1 ] && echo no || echo yes)" "$2" 6
  fi
  [ -n "$V1_BLOCKED" ] && finish "$1" "$(current_stack)" yes "${2}_and_${V1_BLOCKED}" 7
  finish "$1" v1 yes "${2}_and_daemon_not_up" 7 \
    "!! and Pollen's daemon did not answer either: sudo systemctl status reachy-mini-daemon"
}

apply_v2() {
  local before
  before="$(current_stack)"
  refuse_if_docker_hung "$before"
  if ! have_docker; then
    [ "$before" = v2 ] && v2_failed v2 no_docker "this robot is fenced for v2 but has no working Docker"
    finish v1 v1 no no_docker 5 \
      "v2 needs Docker on the robot; it stays on v1 for now (the next deploy installs it)"
  fi
  if ! have_image; then
    [ "$before" = v2 ] && v2_failed v2 no_image "this robot is fenced for v2 but has no $IMAGE"
    finish v1 v1 no no_image 5 \
      "v2 needs the image $IMAGE on the robot; it stays on v1 for now (the deploy computer ships it)"
  fi
  [ "$before" = v1 ] && echo "  switching this robot to v2 (the robot as a ROS 2 node)"
  # 1. Nothing else may own the bus: the v1 graph off, Pollen's daemon off and fenced.
  #    `disable` alone is not a fence (emori-camera-bridge Wants= the daemon), and
  #    `mask` fails on the stock image: the drop-in's Condition is the fence.
  if systemctl cat emori-ros2 >/dev/null 2>&1; then
    sudo -n systemctl disable --now emori-ros2 >/dev/null 2>&1 || true
  fi
  sudo -n systemctl disable --now reachy-mini-daemon >/dev/null 2>&1 || true
  sudo -n mkdir -p "$DROPIN_DIR"
  sudo -n cp deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf \
    "$DROPIN_DIR/zz-v2-fence.conf"
  sudo -n systemctl daemon-reload
  sudo -n touch "$FENCE"
  # 2. The node. `up -d` recreates the container only when its image or settings changed, and that
  #    parks the robot, so it waits for the next apply if anything would be interrupted.
  local recreate=()
  if [ "$before" = v2 ] && container_running && bash deploy/robot/session-busy.sh; then
    recreate=(--no-recreate)
    echo "  $CONTAINER left as it is: a session is running, and a change to it waits for the next deploy"
  fi
  if ! sudo -n timeout -k 5 300 "${COMPOSE[@]}" up -d "${recreate[@]}"; then
    v2_failed "$before" compose_failed "docker compose could not start $CONTAINER"
  fi
  if ! wait_for v2; then
    v2_failed "$before" v2_not_up \
      "$CONTAINER did not answer as reachy_mini_ros2 on :8000 within ${WAIT_S} s"
  fi
  echo "  $CONTAINER answers on :8000 as reachy_mini_ros2"
  # 3. Face look-at: the camera tee the node's detector reads.
  if [ -z "$(cam_tee)" ]; then
    set_cam_tee "$TEE_NR"
    restart_camera
    echo "  look-at camera tee armed (EMORI_CAM_ROS_VIDEO_NR=$TEE_NR in $CAM_DEFAULTS)"
  fi
  if [ "$before" = v1 ]; then
    restart_hostapp
    finish v1 v2 yes switched 0
  fi
  finish v2 v2 no unchanged 0
}

# --- docker, status ---------------------------------------------------------------------

install_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "  installing Docker (docker.io, docker-compose)"
    sudo -n apt-get update -qq || return 1
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-upgrade \
      docker.io docker-compose || return 1
  fi
  id -nG "$ME" | tr ' ' '\n' | grep -qx docker || sudo -n usermod -aG docker "$ME"
  sudo -n systemctl enable --now docker >/dev/null 2>&1 || true
  have_docker
}

status() {
  local source answer
  echo "stack=$(current_stack)"
  echo "wanted=$(env_stack)"
  if have_docker; then
    echo "docker=yes"
    if have_image; then
      source="$(sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker image inspect -f "{{index .Config.Labels \"$SOURCE_LABEL\"}}" \
        "$IMAGE" 2>/dev/null)"
      case "$source" in ''|'<no value>') source=unknown ;; esac
      echo "image=yes"
      echo "image_source=$source"
      echo "image_created=$(sudo -n timeout -k 5 "$DOCKER_WAIT_S" docker image inspect -f '{{.Created}}' "$IMAGE" 2>/dev/null \
        | cut -c1-10)"
    else
      echo "image=no"
    fi
    container_running && echo "container=running" || echo "container=no"
  else
    echo "docker=no"
  fi
  if answer="$(backend)"; then echo "backend=${answer:-daemon}"; else echo "backend=none"; fi
}

case "${1:-}" in
  status) status ;;
  docker)
    if install_docker; then echo "docker=ready"; else echo "docker=failed"; exit 1; fi ;;
  apply)
    want="${2:-$(env_stack)}"
    case "${want:-v2}" in
      v2) apply_v2 ;;
      v1) apply_v1 ;;
      *) echo "EMORI_ROS2_STACK must be v1 or v2, not '$want'" >&2; exit 2 ;;
    esac ;;
  *) echo "usage: $0 status | docker | apply [v1|v2]" >&2; exit 2 ;;
esac
