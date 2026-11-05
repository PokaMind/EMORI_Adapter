#!/usr/bin/env bash
# Build the hub image the way the robot builds it, and run it beside a motor node, on a
# public CI runner. Each step is one subcommand so that a failure says which layer broke:
#
#   hub-ci.sh motor-layer    the node's Python over the base image (deploy/robot/ros2_native/
#                            motor-overlay.sh builds the same layer on the robot)
#   hub-ci.sh payload        the build context deploy/hub/hub-build.sh is given on the robot:
#                            the package closure, the wheels, the hub's files and its Python
#   hub-ci.sh hub-base       Dockerfile.hub-base over the motor image
#   hub-ci.sh hub            Dockerfile.hub over the hub base
#   hub-ci.sh up             the motor node (mockup backend) and the hub, side by side
#   hub-ci.sh smoke          the hub answers, the robot link is up, the motor's services reply
#   hub-ci.sh tests          tests_ros/, inside the hub image
#   hub-ci.sh stop           SIGTERM: the hub exits 0 within its stop budget
#   hub-ci.sh logs | down
#
# What this cannot show. The base here is built from this checkout by
# deploy/ros2/Dockerfile.v2.robot. The robot's own base is a filesystem not built from this
# checkout, so the layering a robot ends up with is proved only on the robot.
#
# Needs: Docker with BuildKit, the base image emori/ros2-jazzy-v2:robot-base already built.
# Exit: 0 | 1 a check failed | 2 usage
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

WORK="${EMORI_CI_WORK:-${RUNNER_TEMP:-/tmp}/emori-ci-hub}"
BASE_IMAGE=emori/ros2-jazzy-v2:robot-base
MOTOR_IMAGE=emori/ros2-jazzy-v2:robot
HUB_BASE_IMAGE=emori/hub-base:robot
HUB_IMAGE=emori/hub:robot
MOTOR_NAME=emori-ci-motor
HUB_NAME=emori-ci-hub
HUB_PORT="${HOSTAPP_PORT:-8095}"
DOMAIN="${ROS_DOMAIN_ID:-0}"
PEERS_FILE="$ROOT/deploy/hub/fastdds_peers.xml"
STOP_BUDGET_S=12
# Appended to the files a layer carries, to find them again where Python imports them from.
MARKER="ci-layer-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# The files and the install path deploy/robot/ros2_native/motor-overlay.sh uses. A test holds
# the two lists equal.
MOTOR_SITE=reachy_mini_ros2/lib/python3.12/site-packages/reachy_mini_ros2
MOTOR_SOURCE=ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2
MOTOR_FILES=(arbiter.py rest_shim.py robot_node.py ros_surface.py state_snapshot.py)
HUB_SITE=emori_hub/lib/python3.12/site-packages/emori_hub
HUB_SOURCE=ros2_ws/src/emori/emori_hub/emori_hub

say() { printf '== %s\n' "$*"; }
fail() { printf '!! %s\n' "$*" >&2; exit 1; }
host_ids() { printf '%s:%s' "$(id -u)" "$(id -g)"; }

# The text of a file as Python sees it in an image, to look for the marker in.
python_source() { # <image> <module>
  docker run --rm "$1" python3 -c \
    "import importlib, sys; sys.stdout.write(open(importlib.import_module('$2').__file__).read())"
}

motor_layer() {
  docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || fail "there is no $BASE_IMAGE to build on"
  local context="$WORK/motor" file
  rm -rf "$context"
  mkdir -p "$context/install-delta/$MOTOR_SITE"
  for file in "${MOTOR_FILES[@]}"; do
    cp "$MOTOR_SOURCE/$file" "$context/install-delta/$MOTOR_SITE/$file"
    printf '\n# %s\n' "$MARKER" >> "$context/install-delta/$MOTOR_SITE/$file"
  done
  docker build --pull=false --build-arg "BASE=$BASE_IMAGE" --build-arg "EMORI_V2_SOURCE=ci" \
    -f deploy/ros2/Dockerfile.v2.robot.delta -t "$MOTOR_IMAGE" "$context"
  local module text
  for module in reachy_mini_ros2.arbiter reachy_mini_ros2.rest_shim; do
    text="$(python_source "$MOTOR_IMAGE" "$module")"
    grep -q "$MARKER" <<<"$text" || fail "the layer is not where $module is imported from"
  done
  say "the motor layer replaces the files Python imports"
}

payload() {
  docker image inspect "$MOTOR_IMAGE" >/dev/null 2>&1 || fail "there is no $MOTOR_IMAGE"
  local context="$WORK/hub" packages
  rm -rf "$context"
  mkdir -p "$context/debs" "$context/wheels" "$context/hub/install-delta/$HUB_SITE"
  cp deploy/hub/Dockerfile.hub deploy/hub/Dockerfile.hub-base "$context/"
  cp deploy/hub/constraints-hub.txt "$context/wheels/"
  cp deploy/hub/hub-entry.sh deploy/hub/pw-record.sh deploy/hub/fastdds_peers.xml deploy/hub/emori-hub-site.pth \
    "$context/hub/"

  # The closure, resolved against the image's own package index, as the robot's payload is.
  packages="$(grep -vE '^[[:space:]]*(#|$)' deploy/hub/hub-packages.txt | tr '\n' ' ')"
  docker run --rm -v "$context/debs:/out" -e "PACKAGES=$packages" -e "HOST_IDS=$(host_ids)" \
    --entrypoint bash "$MOTOR_IMAGE" -c '
      set -euo pipefail
      apt-get update -qq
      apt-get install -y --no-install-recommends --download-only \
        -o Dir::Cache::archives=/out $PACKAGES
      rm -rf /out/lock /out/partial
      chown -R "$HOST_IDS" /out'
  ls "$context/debs"/*.deb >/dev/null

  docker run --rm -v "$context/wheels:/w" -e "HOST_IDS=$(host_ids)" \
    --entrypoint bash "$MOTOR_IMAGE" -c '
      set -euo pipefail
      pip3 wheel --no-deps --no-cache-dir -w /w -r /w/constraints-hub.txt
      chown -R "$HOST_IDS" /w'
  ls "$context/wheels"/*.whl >/dev/null

  # The hub's own Python, laid over the tree the base image already holds.
  cp "$HUB_SOURCE"/*.py "$context/hub/install-delta/$HUB_SITE/"
  printf '\n# %s\n' "$MARKER" >> "$context/hub/install-delta/$HUB_SITE/main.py"
  say "payload: $(find "$context" -type f | wc -l) files, $(du -sm "$context" | cut -f1) MB"
}

hub_base() {
  [ -d "$WORK/hub/debs" ] || fail "run payload first"
  docker build --pull=false --build-arg "EMORI_HUB_BASE_SOURCE=ci" \
    -f "$WORK/hub/Dockerfile.hub-base" -t "$HUB_BASE_IMAGE" "$WORK/hub"
}

hub_image() {
  docker image inspect "$HUB_BASE_IMAGE" >/dev/null 2>&1 || fail "run hub-base first"
  docker build --pull=false --build-arg "EMORI_HUB_SOURCE=ci" \
    -f "$WORK/hub/Dockerfile.hub" -t "$HUB_IMAGE" "$WORK/hub"
  local text
  text="$(python_source "$HUB_IMAGE" emori_hub.main)"
  grep -q "$MARKER" <<<"$text" || fail "the hub layer is not where emori_hub is imported from"
  # The interpreter the hub runs: its pinned packages, and the checkout ahead of the image's
  # own copy of the adapter core.
  docker run --rm -v "$ROOT:/opt/emori:ro" "$HUB_IMAGE" /opt/emori-hub/venv/bin/python -I -c '
import emori_adapter, fastapi, jwt, numpy, segno, starlette, uvicorn, yaml
assert emori_adapter.__file__.startswith("/opt/emori/src/"), emori_adapter.__file__'
  say "the hub image holds its layer, its environment and the checkout's core"
}

ros_environment() {
  printf '%s\n' \
    -e "RMW_IMPLEMENTATION=rmw_fastrtps_cpp" \
    -e "ROS_DOMAIN_ID=$DOMAIN" \
    -e "ROS_LOCALHOST_ONLY=0" \
    -e "FASTRTPS_DEFAULT_PROFILES_FILE=/etc/emori/fastdds_peers.xml"
}

# Read into a variable first: with pipefail, a grep that stops reading early would fail
# the pipeline it ends.
motor_offers() { # <service>
  local listing
  listing="$(docker exec "$MOTOR_NAME" /entrypoint.sh ros2 service list 2>/dev/null)" || return 1
  grep -qx "$1" <<<"$listing"
}

up() {
  down
  mapfile -t ros < <(ros_environment)
  docker run -d --name "$MOTOR_NAME" --network host --ipc private --init --stop-signal SIGINT \
    "${ros[@]}" \
    -e REACHY_BACKEND=mockup -e REACHY_REST_SHIM_ENABLED=false \
    -e EMORI_SOURCE=mock -e EMORI_WITH_FIWARE=false -e FIWARE_BRIDGE_ENABLED=0 \
    -e EMORI_SKILL_TRANSPORT=log -e EMORI_BEHAVIOR_CONFIG=/emori/configs/behavior.yaml \
    -v "$PEERS_FILE:/etc/emori/fastdds_peers.xml:ro" \
    "$MOTOR_IMAGE"
  local deadline=$((SECONDS + 180))
  until motor_offers /reachy_mini/get_status; do
    (( SECONDS < deadline )) || { logs; fail "the motor node never offered /reachy_mini/get_status"; }
    sleep 3
  done
  say "the motor node is up"

  # The robot's user, no capabilities, no core files, a private IPC namespace.
  docker run -d --name "$HUB_NAME" --network host --ipc private --init \
    --user "$(host_ids)" --cap-drop ALL --security-opt no-new-privileges:true \
    --tmpfs /tmp:size=128m --ulimit core=0:0 --stop-signal SIGTERM \
    --stop-timeout "$STOP_BUDGET_S" -w /opt/emori \
    "${ros[@]}" \
    -e HOME=/tmp -e ROS_LOG_DIR=/tmp/roslog \
    -e EMORI_ROBOT_TRANSPORT=ros -e HOSTAPP_HOST=127.0.0.1 -e "HOSTAPP_PORT=$HUB_PORT" \
    -e EMORI_GESTURE=0 -e EMORI_ADDONS=off -e EMORI_WITH_FIWARE=false -e FIWARE_BRIDGE_ENABLED=0 \
    -v "$ROOT:/opt/emori" -v "$PEERS_FILE:/etc/emori/fastdds_peers.xml:ro" \
    "$HUB_IMAGE"
  deadline=$((SECONDS + 120))
  until curl -fsS -m 3 "http://127.0.0.1:$HUB_PORT/healthz" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { logs; fail "the hub never answered /healthz"; }
    [ "$(docker inspect -f '{{.State.Running}}' "$HUB_NAME")" = true ] \
      || { logs; fail "the hub container stopped"; }
    sleep 2
  done
  say "the hub answers on :$HUB_PORT"
}

json_field() { # <json> <key>
  python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$1" "$2"
}

smoke() {
  local health status deadline
  # Discovery is not awaited at start, so the link may take a few seconds to match.
  deadline=$((SECONDS + 60))
  while :; do
    health="$(curl -fsS -m 3 "http://127.0.0.1:$HUB_PORT/healthz")"
    [ "$(json_field "$health" robot)" = True ] && break
    (( SECONDS < deadline )) || { logs; fail "the hub never reached the robot: $health"; }
    sleep 2
  done
  [ "$(json_field "$health" runtime)" = ros ] || fail "the hub reports runtime $(json_field "$health" runtime)"
  status="$(curl -fsS -m 5 "http://127.0.0.1:$HUB_PORT/api/robot/status")"
  [ "$(json_field "$status" state)" = running ] || fail "robot status: $status"
  [ -z "$(json_field "$status" first_unmatched)" ] || fail "an unmatched service: $status"
  say "robot status: $status"

  local services name
  services="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 service list)"
  for name in submit_move get_full_state get_status set_motor_mode set_gaze set_head_reserved \
              stop play_sound set_wobbling; do
    grep -qx "/reachy_mini/$name" <<<"$services" || fail "the graph has no /reachy_mini/$name"
  done

  local reply
  reply="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 service call \
    /reachy_mini/get_status reachy_mini_msgs/srv/GetRobotStatus '{}')"
  grep -q "state='running'" <<<"$reply" || { echo "$reply"; fail "get_status did not say running"; }
  reply="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 service call \
    /reachy_mini/get_full_state reachy_mini_msgs/srv/GetFullState '{}')"
  grep -q "ok=True" <<<"$reply" || { echo "$reply"; fail "get_full_state returned no sample"; }
  say "the motor's services answer from the hub's container"

  # A node the hub could not build leaves the kiosk running, so nothing above would notice.
  status="$(curl -fsS -m 5 "http://127.0.0.1:$HUB_PORT/api/robot/status")"
  python3 -c '
import json, sys
nodes = json.loads(sys.argv[1]).get("hub_nodes") or {}
wanted = {"session", "speech", "gesture", "motion", "gateway", "addon"}
missing = sorted(wanted - set(nodes.get("hosted") or []))
failed = nodes.get("failed")
if missing or failed:
    sys.exit(f"hub nodes: missing {missing}, failed {failed}")
' "$status" || { logs; fail "the hub does not host its facade nodes"; }
  reply="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 service call \
    /emori/gateway/status emori_msgs/srv/GetHubStatus '{}')"
  grep -q "runtime='ros'" <<<"$reply" || { echo "$reply"; fail "the gateway did not say ros"; }
  reply="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 topic list)"
  for name in /diagnostics /emori/session/state /emori/session/events /emori/motion/player \
              /emori/motion/plan_events /emori/gesture/state /emori/addon/status; do
    grep -qx "$name" <<<"$reply" || fail "the graph has no $name"
  done
  say "the hub's own nodes are on the graph and answer"

  # A remap meant for one node reaches every node in its process, and only a real launch shows it.
  reply="$(docker exec "$HUB_NAME" /usr/local/bin/hub-entry.sh ros2 node list --no-daemon --spin-time 8)"
  grep -qx /reachy_mini_link <<<"$reply" || { echo "$reply"; fail "the graph has no /reachy_mini_link"; }
  [ "$(grep -cx /reachy_mini <<<"$reply")" = 1 ] || { echo "$reply"; fail "two nodes share a name"; }
}

tests() {
  docker run --rm --network host --ipc private --user "$(host_ids)" \
    --tmpfs /tmp:size=256m -w /opt/emori \
    -e HOME=/tmp -e ROS_LOG_DIR=/tmp/roslog -e EMORI_REQUIRE_ROS=1 \
    -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp -e ROS_LOCALHOST_ONLY=0 \
    -v "$ROOT:/opt/emori:ro" "$HUB_IMAGE" bash -c '
      set -euo pipefail
      pip3 install --quiet --break-system-packages --target /tmp/py pytest
      # The interpreter the hub runs, and the ROS path the entrypoint set up kept behind these.
      # ROS ships pytest plugins for an older pytest, which stop the current one from loading, and
      # these tests use none of them.
      PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      PYTHONPATH=/tmp/py:/opt/emori/src:$PYTHONPATH /opt/emori-hub/venv/bin/python -m pytest \
        tests_ros -p no:cacheprovider -o pythonpath=src -o addopts= -ra'
}

stop_hub() {
  local began=$SECONDS code
  docker stop -t "$STOP_BUDGET_S" "$HUB_NAME" >/dev/null
  code="$(docker inspect -f '{{.State.ExitCode}}' "$HUB_NAME")"
  [ "$code" = 0 ] || { logs; fail "the hub exited $code on SIGTERM"; }
  (( SECONDS - began < STOP_BUDGET_S )) || fail "the hub needed the whole stop budget"
  say "the hub stopped with 0 in $((SECONDS - began)) s"
}

logs() {
  for name in "$MOTOR_NAME" "$HUB_NAME"; do
    echo "----- $name -----"
    docker logs "$name" 2>&1 | tail -n 80 || true
  done
}

down() {
  docker rm -f "$HUB_NAME" "$MOTOR_NAME" >/dev/null 2>&1 || true
}

case "${1:-}" in
  motor-layer) motor_layer ;;
  payload) payload ;;
  hub-base) hub_base ;;
  hub) hub_image ;;
  up) up ;;
  smoke) smoke ;;
  tests) tests ;;
  stop) stop_hub ;;
  logs) logs ;;
  down) down ;;
  *) echo "usage: $0 motor-layer | payload | hub-base | hub | up | smoke | tests | stop | logs | down" >&2
     exit 2 ;;
esac
