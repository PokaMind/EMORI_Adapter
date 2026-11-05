#!/usr/bin/env bash
# Smoke test for the standalone image: do the three publishable nodes actually RUN in a
# container that has no EMORI application in it?
#
# `colcon build` is not evidence. An ament_python package builds perfectly while its node
# imports something that is not there, and the failure only appears at `ros2 run`. So this
# starts all three for real and asks the graph what it can see. An ImportError shows up
# here as a node that never appears in `ros2 node list`, and the node's own log is printed
# so the reason is in the same output as the failure.
#
# Run by `deploy/ros2/Dockerfile.standalone`'s CMD, after the entrypoint has sourced
# Humble and the overlay. Driven by `scripts/ros_standalone_check.py`.
set -uo pipefail

# The facade's `log` transport accepts every request and records it: no host application,
# no shared key, no network and no robot, while the node's whole path still executes.
# `hostapp_http` here would be a facade spending a second per request on a connection
# refused by nothing.
export EMORI_SKILL_TRANSPORT="${EMORI_SKILL_TRANSPORT:-log}"

# Settle before asking, then poll. The flat sleep is enough on a laptop and is not enough
# on an emulated arm64 image under Docker Desktop; the poll is what makes the result mean
# "these nodes do not start" rather than "this box was slow today".
SETTLE_S="${EMORI_SMOKE_SETTLE_S:-8}"
DEADLINE_S="${EMORI_SMOKE_DEADLINE_S:-60}"

NODES=("/hri_context_analyzer" "/hri_engagement_behaviors" "/skill_facade")
# /skill/set_expression proves the behaviour lane and the facade agreed on the one topic
# that is not an action; /diagnostics proves both ledgers are up. Between them they say
# the graph is wired, not merely populated.
TOPICS=("/skill/set_expression" "/diagnostics")

declare -A LOGS=(
  ["context_analyzer"]="/tmp/context_analyzer.log"
  ["behavior_node"]="/tmp/behavior_node.log"
  ["skill_facade"]="/tmp/skill_facade.log"
)
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT

dump_logs() {
  for name in context_analyzer behavior_node skill_facade; do
    echo "----- ${name} -----"
    cat "${LOGS[${name}]}" 2>/dev/null || echo "(no output)"
  done
}

start() {  # start <package> <executable>
  local package="$1" executable="$2"
  ros2 run "${package}" "${executable}" >"${LOGS[${executable}]}" 2>&1 &
  PIDS+=("$!")
  echo "started ${package}/${executable} pid $!"
}

echo "== standalone smoke: ROS $(ros2 --version 2>/dev/null || echo humble), transport=${EMORI_SKILL_TRANSPORT}"
start hri_context_analyzer     context_analyzer
start hri_engagement_behaviors behavior_node
start emori_skill_facade       skill_facade

sleep "${SETTLE_S}"

missing_nodes=""
missing_topics=""
node_list=""
topic_list=""
deadline=$((SECONDS + DEADLINE_S))
while :; do
  node_list="$(ros2 node list 2>/dev/null)"
  topic_list="$(ros2 topic list 2>/dev/null)"

  missing_nodes=""
  for node in "${NODES[@]}"; do
    grep -qx -- "${node}" <<<"${node_list}" || missing_nodes+=" ${node}"
  done
  missing_topics=""
  for topic in "${TOPICS[@]}"; do
    grep -qx -- "${topic}" <<<"${topic_list}" || missing_topics+=" ${topic}"
  done

  [[ -z "${missing_nodes}" && -z "${missing_topics}" ]] && break
  (( SECONDS >= deadline )) && break
  sleep 2
done

echo "== ros2 node list"
echo "${node_list}"
echo "== ros2 topic list"
echo "${topic_list}"

# A node that exited is a different failure from a node that is up but invisible, and the
# difference is the whole diagnosis, so say which one happened.
for pid in "${PIDS[@]}"; do
  kill -0 "${pid}" 2>/dev/null || echo "WARNING: pid ${pid} is no longer running"
done

if [[ -n "${missing_nodes}" || -n "${missing_topics}" ]]; then
  echo "FAIL: missing nodes:${missing_nodes:-" (none)"} | missing topics:${missing_topics:-" (none)"}"
  dump_logs
  exit 1
fi

echo "PASS: all three nodes and both topics are up with no EMORI core installed"
exit 0
