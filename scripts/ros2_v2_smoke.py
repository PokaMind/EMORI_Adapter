"""Bring up the v2 graph and prove the robot is a node in it.

The v1 smoke asked "did five nodes start and do the topics carry anything". v2 makes a
bigger claim -- there is no Reachy daemon any more, the robot lives inside this container
-- and a bigger claim needs different evidence. A node list cannot tell you whether
Pollen's 50 Hz control loop is actually turning, and a counter the node writes about
itself cannot either. So this asks four questions whose answers cannot be faked by a node
that started and then wedged:

  1. Is `reachy_mini` in `ros2 node list` alongside the four EMORI nodes? With
     `--motor-launch robot.launch.py`, is it there alone? That launch file is the node for a
     hub that hosts the four, and a copy of one here would be a second one on the graph.
  2. Does `/joint_states` arrive at the control-loop rate, and does the camera produce
     frames? (`ros2 topic hz`, i.e. the subscriber's clock, not the publisher's claim.)
  3. Does one real `GoTo` action goal run to `completed: true`? That is the whole motion
     path -- action server, arbiter, asyncio loop, Pollen's backend, MuJoCo -- in one
     round trip.
  4. Does the REST compatibility shim answer `/api/daemon/status` FROM THE HOST on :8000,
     reporting `backend: reachy_mini_ros2`? That is the hostapp's unchanged view of the
     world, checked from where the hostapp stands.

Then it prints the /diagnostics ledgers, because the numbers are the point.

And, opt-in, a fifth question that is about the graph being HONEST rather than working:

  5. When the control loop dies under a node that is otherwise healthy, does anything
     notice? (`--kill-backend-check`.) The failure it guards is the quiet one: Pollen's
     `ready` is a `threading.Event` that is set once and never cleared, so a crashed
     control loop leaves the REST shim answering `state: running`, /diagnostics saying
     OK, the action servers accepting goals -- and a robot that has not moved since.

    python scripts/ros2_v2_smoke.py
    python scripts/ros2_v2_smoke.py --build        # build the image first (slow)
    python scripts/ros2_v2_smoke.py --keep-up      # leave the graph running
    python scripts/ros2_v2_smoke.py --kill-backend-check   # + the fault-injection run
    python scripts/ros2_v2_smoke.py --motor-launch robot.launch.py   # the node with no graph

Pollen's Windows daemon must NOT be running: the compose file publishes :8000 and both
want it. This says so and stops rather than producing a run that looks like v2 and is v1.

Standard library only, deliberately: this should be runnable from a bare checkout by
someone verifying the claim, with no virtualenv and nothing installed.

Exit codes: 0 PASS, 1 FAIL (an assertion, a failed build, a timeout), 2 Docker is not
usable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = "deploy/ros2/docker-compose.v2.yml"
CONTAINER = "emori-ros2-v2"
IMAGE = "emori/ros2-jazzy-v2:latest"
SHIM_URL = "http://127.0.0.1:8000/api/daemon/status"
SHIM_BACKEND = "reachy_mini_ros2"

# The whole v2 graph. `reachy_mini` first because it is the one v1 could not have, and
# `fiware_bridge` is absent because v2.launch.py guards it with IfCondition(with_fiware)
# and this smoke sets EMORI_WITH_FIWARE=false (the hostapp is the only FIWARE writer).
EXPECTED_NODES = ("reachy_mini", "emori_adapter", "hri_context_analyzer",
                  "hri_engagement_behaviors", "skill_facade")
# The two values of EMORI_MOTOR_LAUNCH the compose files know, and what each one starts.
GRAPH_LAUNCH = "v2.launch.py"
MOTOR_ONLY_LAUNCH = "robot.launch.py"
MOTOR_ONLY_NODES = ("reachy_mini",)
# What the hub needs of the node. The node only logs when it cannot serve these, and goes on.
LINK_SERVICES = ("/reachy_mini/submit_move", "/reachy_mini/get_full_state")

JOINT_STATES_TOPIC = "/joint_states"
CAMERA_TOPIC = "/reachy_mini/camera/image_raw"
# Same floors, and the same reasons, as scripts/rehearse_motion_owner.py: 20% of headroom
# under the 50 Hz control loop, and "the renderer thread is alive" rather than "fast".
MIN_JOINT_STATE_HZ = 40.0
MIN_CAMERA_HZ = 3.0
# `ros2 topic hz` prints a running average about once a second; 5 s is two or three
# prints, which is enough for the average to settle.
HZ_WINDOW_S = 5

# The MuJoCo model compile, the emotions dataset load and five nodes' imports. Generous,
# and still well short of "hung".
GRAPH_READY_S = 180.0
SHIM_READY_S = 120.0
POLL_S = 3.0

# A 1 cm lift of the head over half a second, on the interaction lane. Small enough to be
# safe on any backend, large enough that MuJoCo's solver has to do something.
GOAL_YAML = ('{use_head: true, head_pose: {position: {z: 0.01}, '
             'orientation: {w: 1.0}}, duration: 0.5, interpolation: minjerk, '
             'lane: interaction, client: smoke}')
GOAL_TIMEOUT_S = 60

# --- the fault-injection run (--kill-backend-check) ----------------------------------
# A SECOND, throwaway container running ONLY the robot node, on its own port, so it can
# be killed without touching the graph the checks above measured (and so `--keep-up`
# still means what it says).
#
# `docker exec ... pkill` was the obvious way to do this and is the wrong one: killing
# the process proves nothing about a node that stays up, which is the case that matters.
# What has to be produced is a LIVE node whose control-loop thread is dead -- and the
# only honest way to make that happen on demand is for the node to do it to itself, so
# `backend_host.py` reads REACHY_FAULT_INJECT_S and fails its own control loop after
# that many seconds. It is documented as test-only, it is never set on a robot, and it is
# the one switch in this repository that exists purely to be caught by this check.
FAULT_CONTAINER = "emori-ros2-v2-fault"
FAULT_PORT = 8100
FAULT_STATUS_URL = f"http://127.0.0.1:{FAULT_PORT}/api/daemon/status"
# The fault fires at 12 s; /diagnostics (the tick that notices and ends the process) runs
# every 8 s. Both are started within a few hundred milliseconds of each other, so the
# window in which the shim must be reporting `state: error` is [12 s, 16 s] -- about four
# seconds, which a 0.25 s poll cannot miss. Aligning them (the 5 s default with a 15 s
# fault) would make the window a race.
FAULT_AFTER_S = 12
FAULT_DIAGNOSTICS_PERIOD_S = 8.0
FAULT_READY_S = 180.0
FAULT_EXIT_S = 120.0
FAULT_POLL_S = 0.25
# The node's own code for "my control loop died"; `robot_node.BACKEND_DEAD_EXIT_CODE`.
BACKEND_DEAD_EXIT_CODE = 70


# --- the judgement, with no Docker anywhere near it ----------------------------------


def parse_topic_hz(text: str) -> float | None:
    """The settled rate out of `ros2 topic hz`, or None if it printed none.

    The LAST `average rate:` line, because the first is computed over a fraction of a
    window and always reads low. None rather than 0.0 so "the topic does not exist" and
    "the topic is slow" stay different answers.

    Deliberately a second copy of `rehearse_motion_owner.parse_topic_hz`: this script is
    standard-library-only on purpose, and that one imports httpx and PyYAML at module
    level. `tests/test_ros2_v2_deploy.py` pins the two to the same answer on the same
    sample, so the duplication cannot drift into a disagreement.
    """

    latest: float | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("average rate:"):
            continue
        try:
            latest = float(stripped.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            continue
    return latest


def goal_succeeded(text: str) -> bool:
    """Did `ros2 action send_goal --feedback` end in a completed move?

    Both halves are required. `status: SUCCEEDED` only says the action server returned a
    result; `completed: true` is the arbiter saying the move actually ran. A refusal
    (`dropped`, `not_owner`, `head_reserved`) is deliberately a SUCCEEDED goal with
    `completed: false` -- refusing is a normal outcome, not a transport error -- so
    checking the status alone would pass on a robot that never moved.
    """

    lowered = (text or "").lower()
    return "completed: true" in lowered and "status: succeeded" in lowered


def nodes_for_launch(launch: str) -> tuple[str, ...]:
    return MOTOR_ONLY_NODES if launch == MOTOR_ONLY_LAUNCH else EXPECTED_NODES


def missing_services(seen: list[str]) -> list[str]:
    return [name for name in LINK_SERVICES if name not in seen]


def stray_nodes(seen: list[str], launch: str) -> list[str]:
    """The graph's nodes in a run whose launch file should not have started them."""

    wanted = nodes_for_launch(launch)
    return [name for name in EXPECTED_NODES
            if name not in wanted and not missing_nodes(seen, (name,))]


def missing_nodes(seen: list[str], expected: tuple[str, ...] = EXPECTED_NODES) -> list[str]:
    """Which of the expected nodes are absent, names normalised without the leading /."""

    present = {name.strip().lstrip("/") for name in seen if name.strip()}
    return [name for name in expected if name not in present]


def shim_verdict(status: object) -> list[str]:
    """Why the thing answering :8000 is not our shim. Empty list = it is."""

    if not isinstance(status, dict):
        return ["/api/daemon/status did not answer with a JSON object"]
    failures: list[str] = []
    if str(status.get("state") or "") != "running":
        failures.append(f"the shim reports state={status.get('state')!r}, expected "
                        "'running' -- the node is up but its backend is not")
    backend = str(status.get("backend") or "")
    if backend != SHIM_BACKEND:
        failures.append(
            f"backend is {backend or '<absent>'!r}, expected {SHIM_BACKEND!r}. Pollen's "
            "daemon has no `backend` field at all, so this almost certainly means the "
            "Windows daemon is still holding :8000 and the container never bound it")
    return failures


def fault_verdict(states: list[str], exit_code: int | None) -> list[str]:
    """Why the fault-injection run did not prove what it set out to. Empty list = it did.

    Two claims, and both are needed. That the shim SAID `error` is what a hostapp, an
    installer wait-loop or this very script reads; that the process then EXITED non-zero
    is what makes the supervisor rebuild the node instead of leaving a live husk in the
    graph. A run that only ever showed `running` and then exited 0 is indistinguishable
    from a clean shutdown -- which is exactly the bug.
    """

    failures: list[str] = []
    if "running" not in states:
        failures.append(
            "the fault-injection node never reported state=running, so the run proves "
            f"nothing about a HEALTHY node dying (saw: {sorted(set(states)) or 'nothing'})")
    if "error" not in states:
        failures.append(
            "the shim never reported state=error: a dead control loop was still being "
            f"advertised as a working robot (saw: {sorted(set(states)) or 'nothing'})")
    if exit_code is None:
        failures.append("the fault-injection container never exited")
    elif exit_code == 0:
        failures.append(
            "the fault-injection container exited 0. A node that loses its control loop "
            "and exits cleanly has told the supervisor there was nothing to restart")
    return failures


# --- the Docker half -----------------------------------------------------------------


def _run(command: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=timeout, check=False)


def _exec(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Run a command inside the container through the image's own entrypoint.

    `docker exec` does not run the image ENTRYPOINT, so the Jazzy overlay would not be
    sourced. Arguments are passed as a list and never through a shell -- on Git Bash for
    Windows, a bare `/joint_states` typed at a prompt is rewritten by MSYS into
    `C:/Program Files/Git/joint_states`, and the measurement then silently concerns a
    topic that does not exist. Anyone reproducing these commands by hand there needs
    MSYS_NO_PATHCONV=1.
    """

    return _run(["docker", "exec", CONTAINER, "/entrypoint.sh", *args], timeout=timeout)


def _http_json(url: str, timeout: float = 3.0) -> object | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def docker_available() -> bool:
    try:
        return _run(["docker", "version"], timeout=60.0).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def node_list() -> list[str]:
    # --no-daemon: no resident ros2 daemon is left running in the container afterwards.
    result = _exec("ros2", "node", "list", "--no-daemon", "--spin-time", "5", timeout=45.0)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def topic_hz(topic: str) -> float | None:
    # `ros2 topic hz` never exits on its own; `timeout` inside the container kills it,
    # so a non-zero return code is normal and the output is the result.
    result = _exec("timeout", str(HZ_WINDOW_S), "ros2", "topic", "hz", topic,
                   timeout=HZ_WINDOW_S + 45.0)
    return parse_topic_hz(result.stdout + result.stderr)


def send_goto_goal() -> subprocess.CompletedProcess:
    return _exec("timeout", str(GOAL_TIMEOUT_S), "ros2", "action", "send_goal",
                 "/reachy_mini/goto", "reachy_mini_msgs/action/GoTo", GOAL_YAML,
                 timeout=GOAL_TIMEOUT_S + 45.0)


def diagnostics_echo(seconds: int = 12) -> str:
    result = _exec("timeout", str(seconds), "ros2", "topic", "echo", "--no-daemon",
                   "/diagnostics", timeout=seconds + 45.0)
    return result.stdout


def kill_backend_check() -> list[str]:
    """Run one node, fail its control loop on purpose, and watch what the graph says.

    Returns the failures, empty if it behaved. Never raises: this is an opt-in extra and
    a Docker hiccup here must not be reported as a robot that lies.
    """

    print(f"[v2-smoke] 6. fault injection: a node whose control loop dies at "
          f"{FAULT_AFTER_S}s (container {FAULT_CONTAINER}, port {FAULT_PORT})")
    _run(["docker", "rm", "-f", FAULT_CONTAINER], timeout=60.0)

    started = _run([
        "docker", "run", "-d", "--name", FAULT_CONTAINER,
        "-p", f"{FAULT_PORT}:{FAULT_PORT}",
        # mockup, not mujoco: no model compile, no renderer, nothing to go slowly wrong.
        # The control loop is the same `wrapped_run` on the same thread either way.
        "-e", "REACHY_BACKEND=mockup",
        "-e", f"REACHY_FAULT_INJECT_S={FAULT_AFTER_S}",
        "-e", "ROS_LOCALHOST_ONLY=1",
        IMAGE,
        "ros2", "run", "reachy_mini_ros2", "robot_node", "--ros-args",
        "-p", "backend:=mockup",
        "-p", f"rest_shim.port:={FAULT_PORT}",
        "-p", "camera.enabled:=false",
        "-p", f"diagnostics_period_s:={FAULT_DIAGNOSTICS_PERIOD_S}",
    ], timeout=120.0)
    if started.returncode != 0:
        print(started.stderr[-2000:], file=sys.stderr)
        return ["could not start the fault-injection container"]

    states: list[str] = []
    exit_code: int | None = None
    try:
        # Poll from the moment it is launched: the window in which the shim is up AND
        # the control loop is dead is bounded by the diagnostics tick that ends the
        # process, so the evidence has to be collected while it is happening.
        deadline = time.monotonic() + FAULT_READY_S + FAULT_EXIT_S
        while time.monotonic() < deadline:
            status = _http_json(FAULT_STATUS_URL, timeout=1.0)
            if isinstance(status, dict):
                state = str(status.get("state") or "")
                if not states or states[-1] != state:
                    print(f"[v2-smoke]    state={state!r} "
                          f"error={status.get('error')!r}")
                states.append(state)
                if state == "error":
                    # Seen. Nothing left to learn from polling; wait for the exit.
                    break
            elif states:
                # It answered and then stopped answering: the shim was torn down, which
                # only happens on the way out.
                break
            time.sleep(FAULT_POLL_S)

        waited = _run(["docker", "wait", FAULT_CONTAINER], timeout=FAULT_EXIT_S + 60.0)
        if waited.returncode == 0 and waited.stdout.strip():
            try:
                exit_code = int(waited.stdout.strip().splitlines()[-1])
            except ValueError:
                exit_code = None
        print(f"[v2-smoke]    container exit code: {exit_code}"
              + (" (the node's own BACKEND_DEAD_EXIT_CODE)"
                 if exit_code == BACKEND_DEAD_EXIT_CODE else ""))
        logs = _run(["docker", "logs", "--tail", "25", FAULT_CONTAINER], timeout=60.0)
        for line in (logs.stdout + logs.stderr).strip().splitlines()[-12:]:
            print(f"[v2-smoke]    | {line}")
    finally:
        _run(["docker", "rm", "-f", FAULT_CONTAINER], timeout=60.0)

    return fault_verdict(states, exit_code)


def _wait(predicate, *, seconds: float, what: str) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            print(f"[v2-smoke]   {what} is up")
            return True
        time.sleep(POLL_S)
    print(f"[v2-smoke]   TIMEOUT: {what} did not come up within {seconds:.0f}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", action="store_true",
                        help="docker compose build first (slow; off by default)")
    parser.add_argument("--keep-up", action="store_true",
                        help="leave the graph running afterwards")
    parser.add_argument("--source", default="mock",
                        help="EMORI_SOURCE for the run (default: mock, i.e. no hostapp)")
    parser.add_argument("--motor-launch", default=GRAPH_LAUNCH,
                        choices=(GRAPH_LAUNCH, MOTOR_ONLY_LAUNCH),
                        help="EMORI_MOTOR_LAUNCH for the run: the graph with the node, or "
                             "the node alone")
    parser.add_argument("--kill-backend-check", action="store_true",
                        help="also run a throwaway node whose control loop is failed on "
                             "purpose, and check the shim reports state=error and the "
                             "process exits non-zero (adds ~40 s)")
    args = parser.parse_args()

    if not docker_available():
        print("[v2-smoke] docker is not usable from here")
        return 2

    # Before anything else: :8000. The interlock exists because the failure it prevents
    # is silent -- the daemon and the shim speak the same dialect on the same port.
    existing = _http_json(SHIM_URL)
    if existing is not None and shim_verdict(existing):
        print("[v2-smoke] REFUSING TO RUN: something is already answering "
              f"{SHIM_URL} and it is not our shim.")
        for line in shim_verdict(existing):
            print(f"[v2-smoke]   - {line}")
        print("[v2-smoke] Stop it first:  taskkill /F /IM reachy-mini-daemon.exe")
        return 1

    compose = ["docker", "compose", "-f", COMPOSE_FILE]
    environment = {"EMORI_SOURCE": args.source, "EMORI_MOTOR_LAUNCH": args.motor_launch}
    expected = nodes_for_launch(args.motor_launch)
    failures: list[str] = []
    up = False
    try:
        if args.build:
            print(f"[v2-smoke] building {IMAGE}")
            build = _run([*compose, "build"], timeout=3600.0)
            if build.returncode != 0:
                print(build.stdout[-4000:])
                print(build.stderr[-4000:], file=sys.stderr)
                print("[v2-smoke] FAIL: the image did not build")
                return 1

        print(f"[v2-smoke] {' '.join([*compose, 'up', '-d'])}  (EMORI_SOURCE={args.source})")
        env = dict(os.environ)
        env.update(environment)
        result = subprocess.run([*compose, "up", "-d"], cwd=str(REPO_ROOT), env=env,
                                capture_output=True, text=True, timeout=600.0,
                                check=False)
        up = True
        if result.returncode != 0:
            print(result.stdout[-4000:])
            print(result.stderr[-4000:], file=sys.stderr)
            print("[v2-smoke] FAIL: docker compose up failed")
            return 1

        # 1. the graph -------------------------------------------------------------
        print(f"[v2-smoke] 1. waiting for {len(expected)} nodes")
        if not _wait(lambda: not missing_nodes(node_list(), expected), seconds=GRAPH_READY_S,
                     what="the v2 graph"):
            seen = node_list()
            failures.append(f"nodes missing from the graph: {missing_nodes(seen, expected)} "
                            f"(saw {seen})")
        strays = stray_nodes(node_list(), args.motor_launch)
        if strays:
            failures.append(f"{args.motor_launch} should not have started {strays}")
        listed = _exec("ros2", "service", "list", "--no-daemon", "--spin-time", "5").stdout
        absent = missing_services([line.strip() for line in listed.splitlines()])
        if absent:
            failures.append(f"the node does not serve {absent}, which the hub needs")

        # 2. the shim, from the HOST ------------------------------------------------
        # From the host on purpose: that is where the hostapp stands, and a shim bound
        # to 127.0.0.1 inside the container would pass every in-container check and be
        # unreachable by the one process that needs it.
        print("[v2-smoke] 2. asking the REST shim on :8000, from this host")
        if not _wait(lambda: not shim_verdict(_http_json(SHIM_URL)),
                     seconds=SHIM_READY_S, what="the REST shim"):
            failures += shim_verdict(_http_json(SHIM_URL)) or ["the shim never answered"]
        else:
            print(f"[v2-smoke]    {json.dumps(_http_json(SHIM_URL))[:300]}")

        # 3. the rates -------------------------------------------------------------
        print(f"[v2-smoke] 3. measuring {JOINT_STATES_TOPIC} and {CAMERA_TOPIC}")
        joint_hz = topic_hz(JOINT_STATES_TOPIC)
        camera_hz = topic_hz(CAMERA_TOPIC)
        print(f"[v2-smoke]    {JOINT_STATES_TOPIC} = {joint_hz} Hz, "
              f"{CAMERA_TOPIC} = {camera_hz} Hz")
        if joint_hz is None or joint_hz < MIN_JOINT_STATE_HZ:
            failures.append(f"{JOINT_STATES_TOPIC} is {joint_hz} Hz, expected at least "
                            f"{MIN_JOINT_STATE_HZ} -- the 50 Hz control loop IS the robot")
        if camera_hz is None or camera_hz < MIN_CAMERA_HZ:
            failures.append(f"{CAMERA_TOPIC} is {camera_hz} Hz, expected at least "
                            f"{MIN_CAMERA_HZ} -- the offscreen renderer is not producing")

        # 4. one real move ----------------------------------------------------------
        print("[v2-smoke] 4. sending one GoTo goal (1 cm lift, 0.5 s, interaction lane)")
        goal = send_goto_goal()
        tail = (goal.stdout + goal.stderr).strip().splitlines()[-12:]
        for line in tail:
            print(f"[v2-smoke]    | {line}")
        if not goal_succeeded(goal.stdout + goal.stderr):
            failures.append("the GoTo goal did not finish with completed: true -- the "
                            "action server, the arbiter or the backend did not carry it")

        # 5. the ledgers ------------------------------------------------------------
        print("[v2-smoke] 5. /diagnostics")
        echo = diagnostics_echo()
        print(echo[-4000:] if echo.strip() else "[v2-smoke]    (nothing published)")

        # 6. a node whose control loop dies --------------------------------------------
        if args.kill_backend_check:
            failures += kill_backend_check()

    except subprocess.TimeoutExpired as error:
        failures.append(f"a docker command timed out: {error}")
    finally:
        if args.keep_up:
            print("[v2-smoke] --keep-up: leaving the graph running")
        elif up:
            print("[v2-smoke] tearing down")
            _run([*compose, "down"], timeout=300.0)

    if failures:
        for failure in failures:
            print(f"[v2-smoke]   - {failure}")
        print("[v2-smoke] FAIL")
        return 1
    print("[v2-smoke] PASS: the robot is a node on this graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
