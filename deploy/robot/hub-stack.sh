#!/usr/bin/env bash
# Which program is the robot's host application: the hostapp (a systemd unit) or the hub (a
# container that reaches the motor container over ROS 2).
#
#   bash deploy/robot/hub-stack.sh status
#   bash deploy/robot/hub-stack.sh apply ros [--with-motor]
#   bash deploy/robot/hub-stack.sh apply hostapp [--with-motor]
#
# The flag file /opt/emori/HUB_IS_ROS decides which of the two units may start, at boot as
# well: emori-hub.service carries ConditionPathExists on it, and the drop-in
# emori-hostapp-hub-fence.conf carries the negation on emori-hostapp.service. Neither unit
# Conflicts= with the other, so a skipped start is the only thing that keeps one from
# starting beside the other.
#
# `apply ros` raises the flag, stops the hostapp and restarts the hub (restart, not start:
# the hub's environment snapshot is written only when the unit starts). Anything that goes
# wrong after the hostapp was stopped puts it back.
#
# `apply hostapp` is the way out. It must still end when the hub, or Docker, does not
# answer, so every docker call is bounded by `timeout`, the kiosk is started with
# --no-block, and the script polls the port instead of trusting a start job.
#
# --with-motor: when the motor container runs a different launch file than the other program
# needs, it has to be recreated, which parks and wakes the robot. Without the option the
# script refuses instead of leaving a program with no graph.
#
# Nothing here ever interrupts a session. Every path that stops or restarts the hostapp, the
# hub or the motor first asks the running program (GET /api/admin/summary, with the admin
# token, read from the env files and never printed) and exits 3 unless it is idle. A program
# that does not answer at all has no session to interrupt. The check is made again after the
# slow preparation and right before the first change, and again before a rollback.
#
# Detaches: the work runs in a transient unit (emori-hub-stack), so an ssh drop cannot leave
# the robot with neither program. A second run while one is going exits 3. --no-wait answers
# as soon as the unit runs; artifacts/last-hub-stack.env then says how it ended.
#
# Prints key=value lines and keeps them in artifacts/last-hub-stack.env; the progress goes to
# the journal and to artifacts/last-hub-stack.log.
# Exit: 0 done | 2 usage | 3 busy (a session, or another run) | 5 not possible yet, nothing
#       changed | 6 the hub did not come up, back on the hostapp | 7 the hostapp was asked
#       for and did not answer, or answers with the motor's graph missing
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

UNIT=emori-hub-stack
HUB_IMAGE=emori/hub:robot
HUB_SOURCE_LABEL=org.pokamind.emori.hub-source
MOTOR=emori-ros2-v2
HUB_CONTAINER=emori-hub
MOTOR_COMPOSE=(compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml)
MOTOR_ENV=deploy/ros2/.env
HUB_LAUNCH=robot.launch.py
HOSTAPP_LAUNCH=v2.launch.py
SUBMIT_MOVE=/reachy_mini/submit_move
NODES=(reachy_mini emori_adapter hri_context_analyzer hri_engagement_behaviors skill_facade)
MIN_FREE_MB="${EMORI_HUB_MIN_FREE_MB:-300}"
# Overridable only so the tests can point them somewhere that is not /opt or /etc.
FLAG="${EMORI_HUB_FLAG:-/opt/emori/HUB_IS_ROS}"
DROPIN_DIR="${EMORI_HOSTAPP_DROPIN_DIR:-/etc/systemd/system/emori-hostapp.service.d}"
HEALTHZ_WAIT_S="${EMORI_HUB_HEALTHZ_WAIT_S:-90}"
NODES_WAIT_S="${EMORI_HUB_NODES_WAIT_S:-180}"
RESTART_LIMIT_S="${EMORI_HUB_RESTART_LIMIT_S:-200}"
READYCHECK_RETRY_S="${EMORI_HUB_READYCHECK_RETRY_S:-20}"
mkdir -p artifacts
RESULT=artifacts/last-hub-stack.env
LOG=artifacts/last-hub-stack.log
# Which way the run going now is headed, for the phone: the result is written only at its end.
TARGET_FILE=artifacts/hub-stack.target
TARGET=""

WITH_MOTOR=no
NO_WAIT=no
MOTOR_TOUCHED=no   # this run edited the launch file and graph settings, so it owes a restore
MODE_BEFORE=""
LAUNCH=""
NODES_STATE=""
READYCHECK_STATE=""
SESSION=""

say() { printf '%s\n' "$*" | tee -a "$LOG"; }

# --- the robot, and what is configured -------------------------------------------------

current_mode() { [ -e "$FLAG" ] && echo hub || echo hostapp; }

# KEY DEFAULT: the last value in the env files, in the order the programs rank them.
setting() {
  local key="$1" value="" file line
  for file in .env configs/site/settings.env secrets/site.env secrets/admin.env; do
    [ -r "$file" ] || continue
    line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1)" || true
    [ -n "$line" ] && value="${line#*=}"
  done
  value="$(printf '%s' "$value" | tr -d "\r\"' ")"
  echo "${value:-$2}"
}

# FILE KEY VALUE: replace the key's line, or append one.
set_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    sed -i -E "s|^${key}=.*|${key}=${value}|" "$file"
  else
    if [ -s "$file" ] && [ -n "$(tail -c1 "$file")" ]; then printf '\n' >> "$file"; fi
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# SECONDS ARGS...: docker, bounded. The CLI has no timeout of its own and Docker can hang.
dk() {
  local limit="${EMORI_HUB_DOCKER_TIMEOUT_S:-$1}"
  shift
  sudo -n timeout -k 5 "$limit" docker "$@"
}

# SECONDS ARGS...: systemctl, bounded the same way.
sd() {
  local limit="$1"
  shift
  sudo -n timeout -k 5 "$limit" systemctl "$@"
}

unit_state() { systemctl is-active "$1" 2>/dev/null || true; }

hub_unit_running() {
  case "$(unit_state emori-hub)" in active|activating|reloading|deactivating) return 0 ;; esac
  return 1
}

# The motor's launch file as the running container has it: robot.launch.py, v2.launch.py, or
# unknown (no answer, or a file this script does not know).
motor_launch() {
  local body name
  body="$(dk 20 inspect -f '{{json .Config.Cmd}}' "$MOTOR" 2>/dev/null)" || { echo unknown; return; }
  name="$(printf '%s' "$body" | grep -oE '[A-Za-z0-9_]+\.launch\.py' | head -n1)"
  case "$name" in
    "$HUB_LAUNCH"|"$HOSTAPP_LAUNCH") echo "$name" ;;
    *) echo unknown ;;
  esac
}

# 0 when the motor container lists the service the hub's robot client submits moves to.
motor_serves_moves() {
  local listed
  # Into a variable first. This script runs under pipefail: piped straight in, `grep -q` leaves
  # at the first match, the rest of the list meets a closed pipe, and the check reads as false.
  listed="$(dk 40 exec "$MOTOR" /entrypoint.sh ros2 service list --no-daemon --spin-time 5 \
    2>/dev/null)" || return 1
  grep -qx "$SUBMIT_MOVE" <<<"$listed"
}

# 0 when every node of the graph is listed. They live in the motor container until the graph
# moves into the hub.
graph_nodes_present() {
  local listed node
  listed="$(dk 40 exec "$MOTOR" /entrypoint.sh ros2 node list --no-daemon --spin-time 5 2>/dev/null)" \
    || return 1
  for node in "${NODES[@]}"; do
    printf '%s\n' "$listed" | grep -qx "/$node" || return 1
  done
}

# --- the running program, and whether it is busy ---------------------------------------

hostapp_port() { setting HOSTAPP_PORT 8080; }

# PATH: a GET on the running program with the admin token. The token goes to curl through
# its config on stdin, so it is in no argument list and in no output.
admin_get() {
  local token
  token="$(setting EMORI_ADMIN_TOKEN "")"
  [ -n "$token" ] || return 2
  case "$token" in *[!A-Za-z0-9._~+/=-]*) return 2 ;; esac
  printf 'header = "X-EMORI-Admin-Token: %s"\n' "$token" \
    | curl -fsS -m 3 -K - "http://127.0.0.1:$(hostapp_port)$1" 2>/dev/null
}

# Sets SESSION:
#   silent     nothing answers, not even /healthz: there is no session to interrupt
#   idle       the program says nothing is running and nothing may be interrupted
#   nosession  only the unauthenticated status answered, and it shows no session
#   busy       a session, a training lease or an add-on mode is running
#   unknown    something answers but its state cannot be read
probe_session() {
  local body verdict
  SESSION=unknown
  if body="$(admin_get /api/admin/summary)" \
     && verdict="$(printf '%s' "$body" | python3 -c '
import json, sys
body = json.load(sys.stdin)
idle = body.get("idle")
if not isinstance(idle, bool):
    sys.exit(1)
print("idle" if idle and not body.get("session_active") else "busy")
' 2>/dev/null)"; then
    SESSION="$verdict"
    return
  fi
  if body="$(curl -fsS -m 3 "http://127.0.0.1:$(hostapp_port)/api/status" 2>/dev/null)" \
     && verdict="$(printf '%s' "$body" | python3 -c '
import json, sys
body = json.load(sys.stdin)
print("busy" if body.get("session") else "nosession")
' 2>/dev/null)"; then
    SESSION="$verdict"
    return
  fi
  # Last, because it asks the robot: with the motor stalled a session runs on behind a /healthz
  # that takes seconds. Longer than that call, and twice, before calling the program gone.
  local attempt
  for attempt in 1 2; do
    curl -fsS -m 8 -o /dev/null "http://127.0.0.1:$(hostapp_port)/healthz" 2>/dev/null && return
  done
  SESSION=silent
}

both_units_down() {
  case "$(unit_state emori-hostapp)" in active|activating|reloading) return 1 ;; esac
  hub_unit_running && return 1
  return 0
}

# WHERE: exit 3 unless a change may start now. Switching to the hub needs a confirmed idle.
require_idle() {
  probe_session
  case "$SESSION" in
    idle) return 0 ;;
    silent) both_units_down && return 0
            finish_busy session_unknown "$1: neither program answers, yet one of them is running" ;;
    busy) finish_busy session_running "$1: a session, a training lease or an add-on is running" ;;
    nosession) finish_busy session_unknown \
                 "$1: cannot confirm the robot is idle without EMORI_ADMIN_TOKEN" ;;
    *) finish_busy session_unknown "$1: the running program answers but its state is unreadable" ;;
  esac
}

# WHERE: the same for a change that takes a program away. No answer is fine, and so is a
# status that shows no session when the token is missing, so a robot whose hub is failing can
# always be recovered.
require_quiet() {
  probe_session
  case "$SESSION" in
    silent|idle|nosession) return 0 ;;
    busy) finish_busy session_running "$1: a session, a training lease or an add-on is running" ;;
    *) finish_busy session_unknown "$1: the running program answers but its state is unreadable" ;;
  esac
}

# --- results ----------------------------------------------------------------------------

# REASON EXIT [SENTENCE]. Mode before and now, and whether anything changed, are read here.
finish() {
  local now changed=no
  now="$(current_mode)"
  if [ "$now" != "$MODE_BEFORE" ] || [ "$MOTOR_TOUCHED" = yes ]; then changed=yes; fi
  {
    printf 'mode_before=%s\nmode=%s\nchanged=%s\nreason=%s\n' \
      "$MODE_BEFORE" "$now" "$changed" "$1"
    [ -n "$TARGET" ] && printf 'target=%s\n' "$TARGET"
    [ -n "$LAUNCH" ] && printf 'launch=%s\n' "$LAUNCH"
    [ -n "$NODES_STATE" ] && printf 'nodes=%s\n' "$NODES_STATE"
    [ -n "$READYCHECK_STATE" ] && printf 'readycheck=%s\n' "$READYCHECK_STATE"
    printf 'exit=%s\nrun=%s\n' "$2" "${EMORI_HUB_STACK_RUN:-}"
  } | tee "$RESULT"
  [ -n "${3:-}" ] && say "  $3"
  exit "$2"
}

finish_busy() { finish "$1" 3 "!! $2; nothing was changed"; }

# REASON SENTENCE: not possible, nothing touched.
refuse() { finish "$1" 5 "!! $2"; }

# --- the checks before a switch to the hub ---------------------------------------------

free_mb() { df -m --output=avail "$ROOT" 2>/dev/null | tail -n1 | tr -d ' '; }

# The image carries the hash of the files it was built from. A deploy records the hash of
# the checkout it shipped in artifacts/hub-source.expected; when the two differ, the core on
# the bind mount and the hub in the image would not agree.
check_image() {
  local label expected
  dk 20 image inspect "$HUB_IMAGE" >/dev/null 2>&1 \
    || refuse no_hub_image "there is no $HUB_IMAGE on this robot (deploy again)"
  label="$(dk 20 image inspect -f "{{index .Config.Labels \"$HUB_SOURCE_LABEL\"}}" "$HUB_IMAGE" \
    2>/dev/null)"
  case "$label" in
    ''|'<no value>'|unknown)
      refuse hub_image_unlabelled "$HUB_IMAGE has no source label, so it cannot be matched to this checkout" ;;
  esac
  expected="$(cat artifacts/hub-source.expected 2>/dev/null | tr -d ' \r\n')"
  if [ -z "$expected" ]; then
    say "  no hub source recorded for this checkout; the image label $label is not compared"
  elif [ "$label" != "$expected" ]; then
    refuse hub_image_stale "$HUB_IMAGE was built from $label, this checkout is $expected (deploy again)"
  fi
}

# Whether the add-on's packages for the hub need building.
addons_stale() {
  local project site=hub-site/lib/python3.12/site-packages
  for project in addons/*/pyproject.toml; do
    [ -f "$project" ] || continue
    if [ ! -d "$site" ] || [ -z "$(ls -A "$site" 2>/dev/null)" ]; then return 0; fi
    if [ "$project" -nt "$site" ]; then return 0; fi
  done
  return 1
}

build_addons() {
  [ -f deploy/hub/hub-addons.sh ] || return 0
  addons_stale || return 0
  say "  building the add-on packages for the hub"
  if ! timeout -k 5 900 bash deploy/hub/hub-addons.sh; then
    say "  !! hub-addons.sh failed; the hub starts without them (readycheck hub_identity says so)"
  fi
}

# --- waits ------------------------------------------------------------------------------

# ros|hostapp: until /healthz answers as that program, at most HEALTHZ_WAIT_S seconds.
wait_healthz() {
  local body runtime end=$((SECONDS + HEALTHZ_WAIT_S))
  while [ "$SECONDS" -lt "$end" ]; do
    if body="$(curl -fsS -m 8 "http://127.0.0.1:$(hostapp_port)/healthz" 2>/dev/null)"; then
      runtime=http
      printf '%s' "$body" | grep -Eq '"runtime"[[:space:]]*:[[:space:]]*"ros"' && runtime=ros
      if [ "$1" = ros ] && [ "$runtime" = ros ]; then return 0; fi
      if [ "$1" = hostapp ] && [ "$runtime" != ros ]; then return 0; fi
    fi
    sleep 1
  done
  return 1
}

# Until the check passes, at most NODES_WAIT_S seconds by the clock: one check takes as long as
# `ros2` needs to start and spin, which on a robot is many seconds.
wait_motor() {  # serves|nodes
  local end=$((SECONDS + NODES_WAIT_S))
  while :; do
    if [ "$1" = serves ] && motor_serves_moves; then return 0; fi
    if [ "$1" = nodes ] && graph_nodes_present; then return 0; fi
    [ "$SECONDS" -lt "$end" ] || return 1
    sleep 3
  done
}

# Right after a start a gate can still be warming up, the robot link's first heartbeat among
# them, so a failure is looked at once more, as the deploy's own verdict does.
run_readycheck() {
  if timeout -k 5 180 bash deploy/robot/readycheck.sh --no-speak; then
    READYCHECK_STATE=pass
    return 0
  fi
  say "  readycheck failed; checking once more in ${READYCHECK_RETRY_S} s"
  sleep "$READYCHECK_RETRY_S"
  if timeout -k 5 180 bash deploy/robot/readycheck.sh --no-speak; then
    READYCHECK_STATE=pass
    return 0
  fi
  READYCHECK_STATE=fail
  return 1
}

# --- the motor ---------------------------------------------------------------------------

# GRAPH LAUNCH: write the two settings and recreate the motor container. The graph setting
# is the hub's (the hub reads it from its env snapshot); the launch file is the motor's
# (compose reads it from deploy/ros2/.env).
recreate_motor() {
  MOTOR_TOUCHED=yes
  set_key .env EMORI_HUB_GRAPH "$1"
  set_key "$MOTOR_ENV" EMORI_MOTOR_LAUNCH "$2"
  say "  bringing the motor container up on $2 (a changed launch file parks and wakes it)"
  if ! dk 150 "${MOTOR_COMPOSE[@]}" up -d >/dev/null 2>&1; then
    say "  !! docker compose did not finish bringing the motor up"
    return 1
  fi
  # Compose recreates only a container whose settings changed. One that still runs the old
  # launch file means the compose file does not read the setting.
  if [ "$(motor_launch)" != "$2" ]; then
    say "  !! the motor container is not running $2"
    return 1
  fi
  return 0
}

# --- apply hostapp ----------------------------------------------------------------------

# 0 when the hostapp answers. Does not exit, so a failed switch to the hub can use it.
back_to_hostapp() {
  say "  stopping the hub, starting the hostapp"
  sudo -n rm -f "$FLAG"
  sd 60 stop emori-hub >/dev/null 2>&1 || true
  # A compose client that died leaves its container up on the port, and the unit's own cleanup
  # runs only when it starts, which it no longer may.
  dk 30 rm -f "$HUB_CONTAINER" >/dev/null 2>&1 || true
  sd 60 daemon-reload >/dev/null 2>&1 || true
  if [ "$WITH_MOTOR" = yes ]; then
    if recreate_motor 0 "$HOSTAPP_LAUNCH"; then
      if wait_motor nodes; then
        NODES_STATE=present
      else
        # The shim still serves motion, so the hostapp can run without the graph.
        NODES_STATE=missing
        say "  !! the five graph nodes are not all listed after ${NODES_WAIT_S} s; going on"
      fi
    else
      NODES_STATE=missing
    fi
  fi
  sd 60 start emori-hostapp >/dev/null 2>&1 || true
  # --no-block: its page loads in its own time, and nothing here waits for that.
  sd 30 start --no-block emori-kiosk >/dev/null 2>&1 || true
  wait_healthz hostapp
}

# Something answers on the port and does not say it is the hub.
hostapp_answers() {
  local body
  body="$(curl -fsS -m 8 "http://127.0.0.1:$(hostapp_port)/healthz" 2>/dev/null)" || return 1
  ! printf '%s' "$body" | grep -Eq '"runtime"[[:space:]]*:[[:space:]]*"ros"'
}

# The hub's container running, whether or not it answers. A Docker that hangs reads as none,
# and then it is hostapp_answers that sees the port is not the host app's.
hub_container_running() {
  [ "$(dk 20 inspect -f '{{.State.Running}}' "$HUB_CONTAINER" 2>/dev/null)" = true ]
}

# Docker says the motor is not on the hub's launch file. A Docker that gives no answer does not.
motor_off_the_hub() {
  local launch
  launch="$(motor_launch)"
  [ "$launch" = "$HOSTAPP_LAUNCH" ] && return 0
  [ "$launch" = unknown ] && dk 20 info >/dev/null 2>&1
}

settings_say_hub() {
  grep -qE "^EMORI_MOTOR_LAUNCH=$HUB_LAUNCH\$" "$MOTOR_ENV" 2>/dev/null \
    || [ "$(setting EMORI_HUB_GRAPH 0)" = 1 ]
}

apply_hostapp() {
  if ! systemctl cat emori-hostapp >/dev/null 2>&1; then
    refuse removed "this release has no hostapp unit any more; redeploy the previous release"
  fi
  # Nothing to do only with the host app answering. A way back that stopped short has lowered
  # the flag and may have written the settings back, and it is the second run that finishes it.
  # Settings still set for the hub are unfinished, whatever the flag says, and so is a hub
  # container on the port or a motor still on the hub's launch file. The container need not
  # answer: a hub that does not is the kind a switch rolls back from.
  if [ ! -e "$FLAG" ] && ! hub_unit_running && ! settings_say_hub && hostapp_answers \
     && ! hub_container_running && motor_off_the_hub; then
    finish unchanged 0
  fi
  LAUNCH="$(motor_launch)"
  [ "$LAUNCH" = unknown ] && say "  the motor's launch file cannot be read; treating it as $HOSTAPP_LAUNCH"
  require_quiet "going back to the hostapp"
  if [ "$LAUNCH" = "$HUB_LAUNCH" ] && [ "$WITH_MOTOR" != yes ]; then
    refuse needs_motor_recreate \
      "the graph lives in the hub and the motor runs $HUB_LAUNCH: use --with-motor (it parks and wakes the robot); the hub is left running"
  fi
  [ "$LAUNCH" = "$HUB_LAUNCH" ] || WITH_MOTOR=no
  # A run cut off after it wrote the settings and before the motor moved: the container is
  # already right, and only the settings need putting back.
  if [ "$WITH_MOTOR" != yes ] && [ "$LAUNCH" = "$HOSTAPP_LAUNCH" ] && settings_say_hub; then
    MOTOR_TOUCHED=yes
    set_key .env EMORI_HUB_GRAPH 0
    set_key "$MOTOR_ENV" EMORI_MOTOR_LAUNCH "$HOSTAPP_LAUNCH"
  fi
  if back_to_hostapp; then
    [ "$WITH_MOTOR" = yes ] && run_readycheck
    if [ "$NODES_STATE" = missing ]; then
      finish nodes_missing 7 \
        "!! the hostapp answers, and the motor's graph did not come back: sudo docker logs emori-ros2-v2"
    fi
    finish switched 0
  fi
  finish hostapp_not_up 7 \
    "!! the hostapp did not answer on :$(hostapp_port) within ${HEALTHZ_WAIT_S} s: sudo systemctl status emori-hostapp"
}

# --- apply ros --------------------------------------------------------------------------

# REASON SENTENCE: the hub is not up; the hostapp comes back, and the motor with it if this
# run recreated it or it runs the hub's launch file, which leaves the hostapp with no graph.
# Asks about a session first, like every other path.
hub_failed() {
  say "  !! $2"
  say "     putting the robot back on the hostapp"
  WITH_MOTOR="$MOTOR_TOUCHED"
  [ "$(motor_launch)" = "$HUB_LAUNCH" ] && WITH_MOTOR=yes
  require_quiet "the hub failed ($1) and cannot be taken away"
  if back_to_hostapp; then
    finish "$1" 6
  fi
  finish "${1}_and_hostapp_not_up" 7 \
    "!! and the hostapp did not answer either: sudo systemctl status emori-hostapp"
}

apply_ros() {
  local graph launch
  if ! systemctl cat emori-hub >/dev/null 2>&1; then
    refuse no_hub_unit "emori-hub.service is not installed on this robot"
  fi
  dk 20 info >/dev/null 2>&1 || refuse no_docker "Docker does not answer"
  check_image
  local free
  free="$(free_mb)"
  case "$free" in ''|*[!0-9]*) free=0 ;; esac
  [ "$free" -ge "$MIN_FREE_MB" ] || refuse low_disk "$free MB free, under $MIN_FREE_MB MB"
  motor_serves_moves || refuse motor_not_serving "the motor container does not list $SUBMIT_MOVE"
  launch="$(motor_launch)"
  LAUNCH="$launch"
  if [ "$WITH_MOTOR" != yes ]; then
    graph="$(setting EMORI_HUB_GRAPH 0)"
    case "$graph:$launch" in
      "0:$HOSTAPP_LAUNCH"|"1:$HUB_LAUNCH") ;;
      *:unknown) refuse launch_unknown "the motor's launch file cannot be read, so it cannot be matched to EMORI_HUB_GRAPH=$graph" ;;
      *) refuse graph_launch_mismatch "EMORI_HUB_GRAPH=$graph does not fit the motor's $launch (--with-motor recreates it)" ;;
    esac
  fi
  require_idle "switching to the hub"
  build_addons
  # The preparation took time; a session may have started in it.
  require_idle "switching to the hub"

  if [ "$WITH_MOTOR" = yes ]; then
    if hub_unit_running; then
      say "  stopping the hub before the motor's settings change"
      sd 60 stop emori-hub >/dev/null 2>&1 || true
    fi
    if recreate_motor 1 "$HUB_LAUNCH"; then
      wait_motor serves || hub_failed motor_not_up \
        "the recreated motor container does not list $SUBMIT_MOVE within ${NODES_WAIT_S} s"
    else
      hub_failed motor_compose_failed "docker compose could not recreate the motor container"
    fi
    LAUNCH="$HUB_LAUNCH"
  fi

  say "  switching this robot to the hub"
  sudo -n touch "$FLAG"
  sudo -n mkdir -p "$DROPIN_DIR"
  sudo -n cp deploy/robot/emori-hostapp-hub-fence.conf "$DROPIN_DIR/zz-hub-fence.conf"
  sd 60 daemon-reload >/dev/null 2>&1 || true
  # Enabled stays enabled after a rollback; the flag decides which unit starts.
  sd 60 enable emori-hub emori-hub-resolv.path >/dev/null 2>&1 || true
  sd 60 start emori-hub-resolv.path >/dev/null 2>&1 || true
  sd 60 stop emori-hostapp >/dev/null 2>&1 || true
  # Blocks until the hub answers /healthz (the unit's ExecStartPost).
  if ! sd "$RESTART_LIMIT_S" restart emori-hub >/dev/null 2>&1; then
    hub_failed hub_not_started "emori-hub did not start: sudo journalctl -u emori-hub"
  fi
  sd 30 start --no-block emori-kiosk >/dev/null 2>&1 || true
  wait_healthz ros || hub_failed hub_not_answering \
    "the hub did not answer /healthz as runtime ros within ${HEALTHZ_WAIT_S} s"
  run_readycheck || hub_failed readycheck_failed "readycheck failed against the hub"
  finish switched 0
}

# --- status, and the command line -------------------------------------------------------

status() {
  echo "mode=$(current_mode)"
  echo "hostapp=$(unit_state emori-hostapp)"
  echo "hub=$(unit_state emori-hub)"
  echo "graph=$(setting EMORI_HUB_GRAPH 0)"
  echo "launch=$(motor_launch)"
}

usage() {
  echo "usage: $0 status | apply ros|hostapp [--with-motor] [--no-wait]" >&2
  exit 2
}

# Re-run this script in a transient unit and wait for it. Inside the unit EMORI_HUB_STACK_INSIDE
# is set and this returns at once.
detach() {
  local run runtime rc=0 recorded
  [ -n "${EMORI_HUB_STACK_INSIDE:-}" ] && return 0
  if [ "$(unit_state "$UNIT")" = active ]; then
    echo "reason=busy"
    echo "!! another hub-stack run is going (journalctl -u $UNIT)"
    exit 3
  fi
  run="$(date +%s)-$$"
  : > "$LOG"
  # A transient unit has no login session, so nothing sets the runtime directory, and the
  # readycheck it runs could not reach PipeWire without it.
  runtime="XDG_RUNTIME_DIR=/run/user/$(id -u)"
  if [ "$NO_WAIT" = yes ]; then
    sudo -n systemd-run --collect --quiet --unit="$UNIT" -p "User=$(id -un)" \
      -p "WorkingDirectory=$ROOT" --setenv="$runtime" \
      --setenv=EMORI_HUB_STACK_INSIDE=1 --setenv="EMORI_HUB_STACK_RUN=$run" \
      -- bash "$ROOT/deploy/robot/hub-stack.sh" "$@" || rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "reason=not_run"
      echo "!! the run did not start (systemd-run exit $rc)"
      exit "$rc"
    fi
    echo "reason=started"
    echo "run=$run"
    exit 0
  fi
  sudo -n systemd-run --collect --wait --quiet --unit="$UNIT" -p "User=$(id -un)" \
    -p "WorkingDirectory=$ROOT" --setenv="$runtime" \
    --setenv=EMORI_HUB_STACK_INSIDE=1 --setenv="EMORI_HUB_STACK_RUN=$run" \
    -- bash "$ROOT/deploy/robot/hub-stack.sh" "$@" || rc=$?
  cat "$LOG" 2>/dev/null
  recorded="$(grep -E '^run=' "$RESULT" 2>/dev/null | tail -n1 | cut -d= -f2-)"
  if [ "$recorded" = "$run" ]; then
    cat "$RESULT"
    exit "$(grep -E '^exit=' "$RESULT" | tail -n1 | cut -d= -f2-)"
  fi
  echo "reason=not_run"
  echo "!! the run left no result (systemd-run exit $rc): journalctl -u $UNIT"
  [ "$rc" -ne 0 ] && exit "$rc"
  exit 1
}

case "${1:-}" in
  status) status ;;
  apply)
    target="${2:-}"
    case "$target" in ros|hostapp) ;; *) usage ;; esac
    for word in "${@:3}"; do
      case "$word" in
        --with-motor) WITH_MOTOR=yes ;;
        --no-wait) NO_WAIT=yes ;;
        *) usage ;;
      esac
    done
    [ $# -le 4 ] || usage
    detach "$@"
    [ -z "${EMORI_HUB_STACK_INSIDE:-}" ] || : > "$LOG"
    MODE_BEFORE="$(current_mode)"
    TARGET="$target"
    printf '%s\n' "$TARGET" > "$TARGET_FILE"
    if [ "$target" = ros ]; then apply_ros; else apply_hostapp; fi ;;
  *) usage ;;
esac
