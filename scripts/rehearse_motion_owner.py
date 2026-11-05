"""Rehearse EMORI_ROS_MOTION_OWNER end to end, in the simulator, and prove it.

The target architecture says the ROS 2 graph must be the component that *decides and
executes* expressive motion. The knob that says so (EMORI_ROS_MOTION_OWNER) is only worth
something if it is exercised end to end: a Translator/BehaviorEngine stand-down that is
coded but never run proves nothing. Reading a counter on one side of the boundary is not proof --
the facade's ledger counts what it POSTED, the hostapp's counts what it did with it, and
the only question anyone actually cares about is whether the physical head moved and
whose lane moved it. So this samples all three.

Two robot modes, chosen with --robot:

  daemon (v1, the default)
      Pollen's FastAPI daemon runs on the Windows host and the ROS graph asks the
      hostapp to move it over /api/skill.

  native (v2)
      There is NO daemon. `reachy_mini_ros2` owns Pollen's backend inside the
      container, and its REST compatibility shim answers the daemon dialect on :8000 so
      the hostapp cannot tell. The facade posts nothing over HTTP either: the
      `ros_native` transport sends skills straight to the robot node's actions. That
      removes both HTTP hops from the loop, so the rehearsal gains two witnesses that
      only exist in v2 -- `ros2 topic hz /joint_states` (the 50 Hz control loop is
      genuinely running) and the camera's frame rate -- and the robot node's own
      /diagnostics ledger, which counts what the ARBITER accepted rather than what
      somebody posted.

      The Windows daemon must NOT be running: it wants the same :8000, and a robot where
      it wins is a robot that looks like v2 and behaves like v1. This refuses to start
      unless whatever answers there reports `backend: reachy_mini_ros2`.

What it does, for ONE owner value per run:

  1. --robot daemon: Reachy daemon on :8000 (reused if already up; started with --sim
     --headless --no-media if not, and killed on the way out only if this run started
     it). --robot native: no daemon at all; :8000 is checked to be free or ours.
  2. For owner=ros only: a loopback hop on :8096 (scripts/dev/loopback_proxy.py).
     Docker Desktop cannot host-network, and /api/skill refuses anything that is not
     127.0.0.1. Rather than weaken the production gate for the dev box, the request
     takes one extra hop and still has to pass it.
  3. The hostapp on :8095 with the skill intake open and a per-run random key.
  4. The ROS container (deploy/ros2/docker-compose.yml), pointed at the hostapp's
     /ws/relay and at the proxy's /api/skill.
  5. A 4 Hz sampler on GET /api/state/present_head_pose -- the only witness that is
     not a counter written by the code under test.
  6. scripts/replay_session.py over tests/fixtures/rehearsal_session.jsonl, in real
     time, then an 8 s tail so the last sentence's motion and ledgers land.
  7. Collect: the hostapp's /api/status counters, both /diagnostics ledgers
     (emori_skill_facade, hri_engagement_behaviors), and the container log.
  8. Tear it all down (unless --keep-up) and assert the table below.

Every threshold is a named constant at the top with the reason it has that value.

    .venv-reachy\\Scripts\\python.exe scripts\\rehearse_motion_owner.py --owner ros
    .venv-reachy\\Scripts\\python.exe scripts\\rehearse_motion_owner.py --owner hostapp
    .venv-reachy\\Scripts\\python.exe scripts\\rehearse_motion_owner.py --owner none

Exit 0 = every assertion held. Exit 1 = at least one did not, or a step timed out.
The ledger is written to artifacts/rehearsals/<utc>-<owner>.json either way, because a
failed rehearsal is the one whose numbers are worth keeping.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv-reachy" / "Scripts" / "python.exe"
DAEMON_EXE = ROOT / ".venv-reachy" / "Scripts" / "reachy-mini-daemon.exe"
PROXY_SCRIPT = ROOT / "scripts" / "dev" / "loopback_proxy.py"
REPLAY_SCRIPT = ROOT / "scripts" / "replay_session.py"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "rehearsal_session.jsonl"
COMPOSE_FILE = ROOT / "deploy" / "ros2" / "docker-compose.yml"
CONTAINER = "emori-ros2"
DAEMON_URL = "http://127.0.0.1:8000"

OWNERS = ("ros", "hostapp", "none")

# --- v2: the robot as a native ROS 2 node --------------------------------------------
# `daemon` is v1 and the default: Pollen's daemon on the host, motion requested over
# HTTP. `native` is v2: no daemon anywhere, the robot node owns the backend, and the
# same :8000 is answered by its REST compatibility shim.
ROBOT_MODES = ("daemon", "native")
V2_COMPOSE_FILE = ROOT / "deploy" / "ros2" / "docker-compose.v2.yml"
V2_CONTAINER = "emori-ros2-v2"
V1_IMAGE = "emori/ros2-humble:latest"
V2_IMAGE = "emori/ros2-jazzy-v2:latest"
# What GET /api/daemon/status reports when the answer comes from our shim rather than
# from Pollen's daemon. This is the whole safety interlock for --robot native: the port
# is the same, the dialect is the same, and this one field is the difference.
SHIM_BACKEND = "reachy_mini_ros2"
# The robot node's /diagnostics status name, and the graph it belongs to. `reachy_mini`
# is the node name too.
ROBOT_STATUS = "reachy_mini"
EXPECTED_NODES_NATIVE = ("reachy_mini", "emori_adapter", "hri_context_analyzer",
                         "hri_engagement_behaviors", "skill_facade")
# The two rate witnesses v2 has and v1 cannot have. Measured with `ros2 topic hz` inside
# the container, because a counter the node writes about itself is not evidence that the
# 50 Hz loop is running -- the topic's arrival rate is.
JOINT_STATES_TOPIC = "/joint_states"
CAMERA_TOPIC = "/reachy_mini/camera/image_raw"
# `ros2 topic hz` prints a running average once a second; 5 s is two or three prints,
# enough for the average to settle and short enough to run twice in a teardown.
TOPIC_HZ_WINDOW_S = 5
# GET /api/state/full is a whole state document and `timestamp` moves every single
# sample. Counting that as head motion would make the pose assertion pass against a
# robot welded to the bench, which is the one thing the pose sampler exists to rule out.
STATE_MOTION_FIELDS = ("head_pose", "body_yaw", "antennas_position")
# Only used by --fiware: where deploy/fiware/docker-compose.yml publishes Orion-LD.
DEFAULT_ORION_URL = "http://localhost:1026"

# --- the assertion table -------------------------------------------------------------
# The rehearsal fixture is 24 analysis cycles over ~46 s. Every threshold below is a
# floor well under what a healthy run produces, because this is a pass/fail gate on a
# timing-sensitive stack, not a benchmark: it has to fail on "the lane never ran" and
# never on "the box was busy for a second".

# owner!=hostapp: how many cycles must show the hostapp's engagement lane standing
# down. 5 of 24 -- a handful proves the gate is live; requiring all 24 would fail on a
# single cycle lost to a slow replay POST.
MIN_SUPPRESSED_CYCLES = 5
# owner=ros: accepted /api/skill requests. The ROS node emits at most one request per
# cycle plus one per sentence, so ~24+7 are offered; 8 accepted survives the 0.35 s
# per-kind rate limit and the 12-slot queue's drops with room to spare.
MIN_SKILL_ACCEPTED = 8
# owner=ros: requests the facade says it POSTed. Must be at least what the hostapp
# accepted -- a gap between the two is the interesting failure, and this floor is what
# makes "the facade never posted at all" a failure rather than a quiet pass.
MIN_FACADE_POSTED = 8
# owner=ros: requests the facade recorded as ACCEPTED by the hostapp (its own view of
# the 200s). Lower than posted on purpose: drops and rate limits are healthy.
MIN_FACADE_ACCEPTED = 5
# owner!=ros: requests the facade SUPPRESSED at its own owner gate, i.e. never sent.
# This is the whole promise of owner=hostapp ("zero behaviour change"), so it has to be
# a positive number, not merely posted==0.
MIN_FACADE_SUPPRESSED = 8
# every owner: cycles the ROS engagement node processed. The graph must be alive and
# reading the relay in all three modes -- that is what makes `none` an observation
# posture rather than a dead container.
MIN_BEHAVIOR_CYCLES = 20
# owner=ros: per-sentence cue events the engagement node handled. The fixture carries
# 7 sentences across all six families; 5 tolerates a relay reconnect.
MIN_BEHAVIOR_CUES = 5
# owner in (ros, hostapp): pose samples that differ from their predecessor by more than
# POSE_EPSILON. The head physically moved, whoever moved it. `none` is informational:
# a still robot is the correct outcome there, but the sim's own settling makes 0 an
# unreliable assertion.
MIN_POSE_CHANGES = 5
# Radians (or metres) of change in any single pose component that counts as motion.
# Well above the MuJoCo sim's idle numerical drift, well below any authored gesture.
POSE_EPSILON = 0.01
# Pose sampling rate. 4 Hz against a daemon whose moves last 0.3-2 s: fast enough to
# catch a single nod, slow enough that the sampler is not itself load.
POSE_SAMPLE_HZ = 4.0
# Refusals that can only mean the robot is misconfigured rather than busy: the owner knob
# disagreeing across the boundary, a key mismatch, or a request that did not arrive
# over loopback. Each must be exactly zero on an owner=ros run.
MISCONFIGURATION_REFUSALS = ("not_owner", "bad_key", "not_loopback")

# --- the native (v2) assertion table -------------------------------------------------
# Same shape and the same spirit as above -- floors, not benchmarks -- with three rows
# v1 has no way to produce.

# The 50 Hz control loop, measured at the topic rather than claimed by a counter. 40 Hz
# leaves 20% of headroom for a Docker Desktop VM that is also running the hostapp, the
# replay and a MuJoCo physics step; below that the loop is genuinely not keeping up.
MIN_JOINT_STATE_HZ = 40.0
# The simulator's offscreen eye camera renders at a requested 10 fps. 3 Hz is "the
# renderer thread is alive and producing frames", not "the renderer is fast".
MIN_CAMERA_HZ = 3.0
# owner=ros: goals the robot node's ARBITER accepted from client `ros`, and moves it
# carried to completion. Same floor as MIN_SKILL_ACCEPTED, and for the same reason: the
# ROS graph offers roughly 24 cycle requests plus 7 cue requests over the fixture.
MIN_ROBOT_ACCEPTED = 8
MIN_ROBOT_COMPLETED = 8
# owner=hostapp: the hostapp's own expressive lane, arriving through the REST shim as
# client `hostapp`. The whole promise of v2's migration story is that this path still
# works with a hostapp that was never told the daemon went away.
MIN_ROBOT_ACCEPTED_HOSTAPP = 8

# --- timeouts ------------------------------------------------------------------------
# The daemon needs ~4 s cold on this box; 40 s covers a first MuJoCo model compile.
DAEMON_READY_S = 40.0
# The hostapp loads the voice menu and roster (and any add-on's start-up assets) before it serves /healthz.
HOSTAPP_READY_S = 60.0
# How long ONE /healthz probe may take before it is called a miss. This is a transport
# timeout, not an assertion: HOSTAPP_READY_S above is still the deadline.
#
# It has to be this large because of --robot native. GET /healthz calls
# `driver.client.health()`, a synchronous GET to REACHY_BASE_URL (:8000), and in native
# mode the robot does not exist yet -- the container is started in step 4, AFTER the
# hostapp in step 3. An unreachable :8000 costs about 2 s per call on Windows (httpx
# tries the resolved addresses in turn before raising WinError 10061), so a 2.0 s probe
# times out on EVERY poll and a perfectly healthy hostapp is declared dead after 60 s of
# 200 OKs (`curl /healthz` answers `{"status":"ok",..., "robot":false}` in just over 2 s
# while such a probe times out).
HOSTAPP_PROBE_TIMEOUT_S = 10.0
# colcon overlay + five nodes + the relay websocket, with headroom.
NODES_READY_S = 60.0
# After the replay's last envelope: the final sentence's motion, the 5 s diagnostics
# timer and the hostapp's own tail all have to land before anything is read.
TAIL_WAIT_S = 8.0
# How long `ros2 topic echo /diagnostics` is allowed to collect. Both ledgers publish
# every 5 s, so 12 s guarantees at least two of each.
DIAGNOSTICS_ECHO_S = 12
# Lines of container log kept in the JSON ledger.
CONTAINER_LOG_TAIL = 400
# The graph the launch file brings up under THIS rehearsal's container env. All four
# must be present before the replay runs. `fiware_bridge` is deliberately absent:
# slice2.launch.py guards it with IfCondition(with_fiware) and container_env() sets
# EMORI_WITH_FIWARE=false (the hostapp is the only FIWARE writer, two would double every
# KPI), so waiting for it would time out on every single run.
EXPECTED_NODES = ("emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
                  "skill_facade")
# The two /diagnostics publishers this rehearsal reads.
FACADE_STATUS = "emori_skill_facade"
BEHAVIORS_STATUS = "hri_engagement_behaviors"

# Windows: a new process group so a Ctrl-C in this console does not tear the children
# down underneath the teardown code that is supposed to be reporting on them.
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# --- pure helpers (tests/test_rehearsal_tooling.py covers these without Docker) -------

@dataclass(frozen=True)
class Check:
    """One row of the assertion table: what was asked, and what the run answered."""

    name: str
    ok: bool
    detail: str


def as_int(values: dict, key: str, default: int = 0) -> int:
    """Read a counter that may be an int (hostapp JSON) or a string (DiagnosticArray).

    ``diagnostic_msgs/KeyValue.value`` is a string by definition, so every ROS-side
    counter arrives as ``'12'``. Anything unreadable is the default rather than a
    crash: a malformed ledger must fail its assertion, not the run's reporting.
    """

    raw = values.get(key, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def parse_diagnostics(text: str) -> dict[str, dict[str, str]]:
    """``ros2 topic echo /diagnostics`` output -> {status name: {key: value}}.

    The echo is a stream of YAML documents separated by ``---``, each a DiagnosticArray
    with one or more statuses. Documents are parsed one at a time and a broken one is
    skipped, because the echo is stopped mid-stream by a timeout and the last document
    is therefore usually truncated. The LAST status seen for a name wins: these are
    cumulative ledgers, so the newest is the whole run.
    """

    out: dict[str, dict[str, str]] = {}
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            chunks.append("\n".join(current))
            current = []
            continue
        current.append(line)
    chunks.append("\n".join(current))

    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            doc = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        for status in doc.get("status") or []:
            if not isinstance(status, dict):
                continue
            name = str(status.get("name", "")).strip()
            if not name:
                continue
            values: dict[str, str] = {}
            for pair in status.get("values") or []:
                if isinstance(pair, dict) and "key" in pair:
                    values[str(pair["key"])] = str(pair.get("value", ""))
            out[name] = values
    return out


def flatten_pose(sample: Any) -> tuple[float, ...]:
    """Every number in a pose reading, in a stable order.

    The daemon has answered this route with a flat list, a nested 4x4 and a dict of
    named axes across firmware revisions, and the rehearsal does not care which: it
    only asks "did any component move". Dict keys are sorted so two samples of the same
    shape always line up; non-numeric leaves are ignored.
    """

    out: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            out.append(float(node))
            return
        if isinstance(node, dict):
            for key in sorted(node):
                walk(node[key])
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(sample)
    return tuple(out)


def count_pose_changes(samples: Sequence[Any], *, epsilon: float = POSE_EPSILON) -> int:
    """How many samples differ from their predecessor by more than ``epsilon``.

    Samples of a different LENGTH than their predecessor are skipped rather than
    counted: a shape change is the daemon answering differently, not the head moving.
    """

    changes = 0
    previous: Optional[tuple[float, ...]] = None
    for sample in samples:
        current = flatten_pose(sample)
        if not current:
            continue
        if previous is not None and len(previous) == len(current):
            delta = max(abs(a - b) for a, b in zip(current, previous))
            if delta > epsilon:
                changes += 1
        previous = current
    return changes


def pose_of_state(sample: Any) -> Any:
    """The moving parts of ``GET /api/state/full``, with the clock left out.

    --robot native has no ``/api/state/present_head_pose``: the REST shim serves the
    daemon's ``/api/state/full`` instead, which is a whole state document. Two of its
    fields move on every single sample whatever the robot is doing -- ``timestamp``
    always, and ``control_mode`` is a string that ``flatten_pose`` ignores anyway -- so
    handing the raw document to ``count_pose_changes`` would report the sampler's own
    clock as head motion and pass the assertion against a robot that never moved.

    Anything that is not a full-state DOCUMENT is returned unchanged. That second
    condition is load-bearing: v1's ``/api/state/present_head_pose`` answers with
    Pollen's ``XYZRPYPose``, i.e. the mapping ``{"x","y","z","roll","pitch","yaw"}`` --
    a dict that carries NONE of STATE_MOTION_FIELDS. Reducing it would give ``{}`` for
    every sample, ``flatten_pose`` would return nothing, ``count_pose_changes`` would
    skip every one of them, and the v1 rehearsal would fail ``pose_changes 0 >= 5``
    with a daemon that is moving the head perfectly well. So the reduction happens only
    when the document actually looks like
    ``/api/state/full`` -- which always carries ``head_pose`` -- and every other shape
    (a flat list, a 4x4, an XYZRPY mapping) flows straight through as before.

    This does not soften the v2 protection: a ``/api/state/full`` document still loses
    its ``timestamp``, which is the one thing the reduction exists to drop.
    """

    if not isinstance(sample, dict):
        return sample
    if not any(name in sample for name in STATE_MOTION_FIELDS):
        return sample
    return {name: sample[name] for name in STATE_MOTION_FIELDS if name in sample}


def parse_topic_hz(text: str) -> Optional[float]:
    """The settled rate out of ``ros2 topic hz`` output, or None if it printed none.

    The command prints a running ``average rate: <n>`` roughly once a second until it is
    killed, so the LAST line is the most settled one: the first is computed over a
    fraction of a window and always reads low. None (rather than 0.0) when nothing
    parseable arrived, so "the topic does not exist" and "the topic is slow" are
    different answers in the ledger.
    """

    latest: Optional[float] = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("average rate:"):
            continue
        try:
            latest = float(stripped.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            continue
    return latest


def status_backend(status: Any) -> str:
    """The ``backend`` field of ``GET /api/daemon/status``; '' when unreadable.

    Pollen's daemon does not have this field at all, which is precisely what makes it
    usable as the interlock below: absent means "not ours".
    """

    if not isinstance(status, dict):
        return ""
    return str(status.get("backend") or "")


def native_port_blocker(status: Any) -> str:
    """Why --robot native must not start against whatever is already on :8000.

    Empty string = go ahead (nothing answered, or what answered is our own shim from a
    previous run). Otherwise the sentence to print before refusing.

    This exists because the failure it prevents is silent. The Windows daemon and the
    shim speak the same dialect on the same port, so a rehearsal started with the daemon
    still up produces a completely healthy-looking run of v1 while claiming to be v2 --
    and `docker compose up` would either fail on the port bind or, worse, succeed with
    the shim unreachable from the host.
    """

    if status is None:
        return ""
    backend = status_backend(status)
    if backend == SHIM_BACKEND:
        return ""
    return ("something is already answering %s/api/daemon/status and it is not the v2 "
            "shim (backend=%r, expected %r). --robot native runs WITHOUT Pollen's "
            "daemon: stop it first (taskkill /F /IM reachy-mini-daemon.exe) or take "
            "down a previous v2 container." % (DAEMON_URL, backend or "<absent>",
                                               SHIM_BACKEND))


def shim_is_ready(status: Any) -> bool:
    """The shim is up AND it is ours AND the node behind it says it is running."""

    if not isinstance(status, dict):
        return False
    return (str(status.get("state") or "") == "running"
            and status_backend(status) == SHIM_BACKEND)


def _ge(name: str, value: int, floor: int) -> Check:
    return Check(name, value >= floor, "%s >= %s" % (value, floor))


def _ge_hz(name: str, value: Optional[float], floor: float) -> Check:
    """A rate row. `None` (the topic never printed a rate) is a failure, not a zero."""

    if value is None:
        return Check(name, False, "no rate measured (topic absent?) >= %.1f" % floor)
    return Check(name, value >= floor, "%.1f >= %.1f" % (value, floor))


def _eq(name: str, value: int, expected: int) -> Check:
    return Check(name, value == expected, "%s == %s" % (value, expected))


def _no_skill_traffic(hostapp: dict) -> Check:
    """owner != ros: the hostapp must not have processed a single skill request.

    Not merely "none accepted": a refusal counter above zero means the facade POSTed
    anyway, which is the exact promise (``hostapp`` = the facade does NOT post) that
    this mode makes.
    """

    hot = {k: as_int(hostapp, k) for k in sorted(hostapp) if k.startswith("skill_")}
    offenders = {k: v for k, v in hot.items() if v > 0}
    return Check("hostapp.no skill_* counter moved", not offenders,
                 "clean" if not offenders else "saw %s" % offenders)


def evaluate(owner: str, *, hostapp: dict, facade: dict, behaviors: dict,
             pose_changes: int) -> list[Check]:
    """The assertion table, as data. Pure: the tests drive it with literal dicts."""

    checks: list[Check] = [
        _ge("behaviors.cycles", as_int(behaviors, "cycles"), MIN_BEHAVIOR_CYCLES),
    ]
    if owner == "ros":
        checks += [
            _ge("hostapp.engagement_suppressed_by_owner",
                as_int(hostapp, "engagement_suppressed_by_owner"),
                MIN_SUPPRESSED_CYCLES),
            _ge("hostapp.skill_accepted", as_int(hostapp, "skill_accepted"),
                MIN_SKILL_ACCEPTED),
        ]
        checks += [
            _eq("hostapp.skill_refused_%s" % code,
                as_int(hostapp, "skill_refused_%s" % code), 0)
            for code in MISCONFIGURATION_REFUSALS
        ]
        checks += [
            _ge("facade.posted", as_int(facade, "posted"), MIN_FACADE_POSTED),
            _ge("facade.accepted", as_int(facade, "accepted"), MIN_FACADE_ACCEPTED),
            _eq("facade.suppressed", as_int(facade, "suppressed"), 0),
            _eq("facade.unreachable", as_int(facade, "unreachable"), 0),
            _ge("behaviors.cue", as_int(behaviors, "cue"), MIN_BEHAVIOR_CUES),
            _ge("pose_changes", pose_changes, MIN_POSE_CHANGES),
        ]
        return checks

    # hostapp | none: the facade computes everything and posts nothing.
    checks += [
        _ge("facade.suppressed", as_int(facade, "suppressed"), MIN_FACADE_SUPPRESSED),
        _eq("facade.posted", as_int(facade, "posted"), 0),
        _no_skill_traffic(hostapp),
    ]
    if owner == "hostapp":
        checks += [
            _eq("hostapp.engagement_suppressed_by_owner",
                as_int(hostapp, "engagement_suppressed_by_owner"), 0),
            _ge("pose_changes", pose_changes, MIN_POSE_CHANGES),
        ]
    else:
        checks.append(
            _ge("hostapp.engagement_suppressed_by_owner",
                as_int(hostapp, "engagement_suppressed_by_owner"),
                MIN_SUPPRESSED_CYCLES))
    return checks


def evaluate_native(owner: str, *, hostapp: dict, facade: dict, behaviors: dict,
                    robot: dict, pose_changes: int, joint_hz: Optional[float],
                    camera_hz: Optional[float]) -> list[Check]:
    """The --robot native assertion table, as data.

    Four columns per owner, and they are four DIFFERENT witnesses on purpose: what the
    hostapp did, what the facade says it sent, what the robot node's arbiter accepted,
    and what the topics actually carried. v1 could only ever cross-check the first two,
    because everything past the hostapp was a black box behind an HTTP call.

    `none` has no row of its own -- there is nothing for the robot node to accept
    from anybody -- so it is judged exactly as v1 judges it, minus the skill counters.
    """

    checks: list[Check] = [
        _ge("behaviors.cycles", as_int(behaviors, "cycles"), MIN_BEHAVIOR_CYCLES),
        # Always, in every mode: if the loop is not running, no other number means
        # anything. These two are the rows that make "the node started and then wedged"
        # fail, and they are the only rows `none` can be judged on besides the graph --
        # `none` authors no expressive motion, so a floor on moves completed or on head
        # movement would be asserting that nobody did what nobody was allowed to do.
        _ge_hz("joint_states_hz", joint_hz, MIN_JOINT_STATE_HZ),
        _ge_hz("camera_hz", camera_hz, MIN_CAMERA_HZ),
        # The intake is shut in native mode (EMORI_SKILL_API=0) for EVERY owner: the
        # facade reaches the robot through actions, so a skill counter that moved would
        # mean an HTTP lane nobody asked for is still open.
        _no_skill_traffic(hostapp),
    ]
    if owner == "ros":
        checks += [
            _ge("hostapp.engagement_suppressed_by_owner",
                as_int(hostapp, "engagement_suppressed_by_owner"),
                MIN_SUPPRESSED_CYCLES),
            _ge("facade.posted", as_int(facade, "posted"), MIN_FACADE_POSTED),
            _ge("facade.accepted", as_int(facade, "accepted"), MIN_FACADE_ACCEPTED),
            _eq("facade.suppressed", as_int(facade, "suppressed"), 0),
            _eq("facade.unreachable", as_int(facade, "unreachable"), 0),
            _ge("robot.accepted_ros", as_int(robot, "accepted_ros"),
                MIN_ROBOT_ACCEPTED),
            _ge("robot.completed", as_int(robot, "completed"), MIN_ROBOT_COMPLETED),
            # The one refusal that can only mean the two owner gates disagree. Drops and
            # preemptions are healthy arbitration; this is a misconfigured robot.
            _eq("robot.refused_not_owner", as_int(robot, "refused_not_owner"), 0),
            _ge("behaviors.cue", as_int(behaviors, "cue"), MIN_BEHAVIOR_CUES),
            _ge("pose_changes", pose_changes, MIN_POSE_CHANGES),
        ]
        return checks

    if owner == "hostapp":
        checks += [
            _eq("hostapp.engagement_suppressed_by_owner",
                as_int(hostapp, "engagement_suppressed_by_owner"), 0),
            _ge("facade.suppressed", as_int(facade, "suppressed"),
                MIN_FACADE_SUPPRESSED),
            _eq("facade.posted", as_int(facade, "posted"), 0),
            # Through the REST shim, from a hostapp that does not know the daemon is
            # gone. These two rows ARE the migration promise.
            _ge("robot.accepted_hostapp", as_int(robot, "accepted_hostapp"),
                MIN_ROBOT_ACCEPTED_HOSTAPP),
            _ge("robot.completed", as_int(robot, "completed"), MIN_ROBOT_COMPLETED),
            _ge("pose_changes", pose_changes, MIN_POSE_CHANGES),
        ]
        return checks

    # `none`: an observation posture. The graph computes everything, the facade posts
    # nothing, and the robot node is up and publishing but authors no expression. Head
    # motion is deliberately NOT asserted, exactly as in v1: the ungated interaction
    # mechanics (greeting, recenter, watchdog) still move the head a little and the sim
    # settles on its own, so any floor here would measure the wrong thing.
    checks += [
        _ge("facade.suppressed", as_int(facade, "suppressed"), MIN_FACADE_SUPPRESSED),
        _eq("facade.posted", as_int(facade, "posted"), 0),
        _ge("hostapp.engagement_suppressed_by_owner",
            as_int(hostapp, "engagement_suppressed_by_owner"), MIN_SUPPRESSED_CYCLES),
    ]
    return checks


def compose_file_for(robot: str) -> Path:
    """Which compose file a robot mode brings up. v1's is never touched by v2."""

    return V2_COMPOSE_FILE if robot == "native" else COMPOSE_FILE


def container_for(robot: str) -> str:
    """Different names on purpose: the two graphs must be able to coexist on a box."""

    return V2_CONTAINER if robot == "native" else CONTAINER


def default_image_for(robot: str) -> str:
    """Humble for v1, Jazzy for v2 -- and never the wrong one by default.

    reachy-mini 1.10.0 needs Python >= 3.11, so it cannot be installed on Humble at all;
    running --robot native against the v1 image would fail deep inside a launch file
    rather than here.
    """

    return V2_IMAGE if robot == "native" else V1_IMAGE


def expected_nodes_for(robot: str) -> tuple[str, ...]:
    return EXPECTED_NODES_NATIVE if robot == "native" else EXPECTED_NODES


def hostapp_env(owner: str, *, port: int, skill_key: str,
                base: Optional[dict] = None, fiware: bool = False,
                orion_url: str = DEFAULT_ORION_URL,
                skill_api: bool = True) -> dict[str, str]:
    """The exact environment the rehearsal's hostapp runs with.

    Deliberately explicit and in one place: a rehearsal whose hostapp differs from the
    documented one proves something nobody can reproduce. HOSTAPP_HOST is NOT set --
    the default 0.0.0.0 is what lets the container reach /ws/relay through
    host.docker.internal, and the loopback gate on /api/skill is unaffected because the
    proxy connects to 127.0.0.1.

    `fiware=True` (--fiware) is the one deliberate departure: it points the hostapp at a
    local Orion-LD so the run leaves a Session entity behind for
    scripts/arise_consumer_check.py to score. It costs one broker round trip per cycle,
    so it is off unless asked for -- a motion rehearsal should not be measuring the
    broker.

    `skill_api=False` is what --robot native uses. In v2 the facade reaches the robot
    node through ROS actions, so the HTTP intake is not a lane that is merely unused --
    it is a lane that must be PROVABLY shut, and the assertion that no `skill_*` counter
    moved only means something if it could not have.
    """

    env = dict(os.environ if base is None else base)
    env.update({
        "HOSTAPP_PORT": str(port),
        # The MuJoCo head drifts under gravity_compensation and the tilt detector reads
        # that drift as begin/end taps, which ends a session the instant it opens.
        "EMORI_GESTURE": "0",
        # No Orion-LD in the loop by default: this rehearsal is about motion, and a
        # missing broker would add retry latency to every cycle. --fiware turns it on
        # for the analytics proof, which needs a real Session entity in the broker.
        "EMORI_FIWARE": "1" if fiware else "0",
        "EMORI_ROS_MOTION_OWNER": owner,
        "EMORI_SKILL_API": "1" if skill_api else "0",
        "EMORI_SKILL_KEY": skill_key,
        "EMORI_BEHAVIOR_CONFIG": "configs/behavior.yaml",
        # The fallback must stay OFF or an owner=ros run could silently become a
        # hostapp run halfway through and still pass its counters.
        "EMORI_ROS_OWNER_FALLBACK_S": "0",
        # The idle scan moves the head every 5 s on its own. With it on, pose_changes
        # would be satisfied by a robot nobody drove, which is the one thing this
        # measurement exists to rule out.
        "EMORI_IDLE_AFTER_S": "0",
        # No PipeWire on Windows: the tap would log a warning and add a dead thread.
        "EMORI_VOICE_LINK": "0",
        # Rehearsals are not sessions; don't litter artifacts/recordings/.
        "EMORI_RECORD": "0",
    })
    if fiware:
        env["ORION_URL"] = orion_url
    return env


def container_env(owner: str, *, host_port: int, proxy_port: int, skill_key: str,
                  base: Optional[dict] = None) -> dict[str, str]:
    """The environment `docker compose up` substitutes into docker-compose.yml."""

    env = dict(os.environ if base is None else base)
    env.update({
        "EMORI_SOURCE": "pokamind",
        "EMORI_WS_URL": "ws://host.docker.internal:%d/ws/relay" % host_port,
        "EMORI_ROS_MOTION_OWNER": owner,
        "EMORI_SKILL_URL": "http://host.docker.internal:%d/api/skill" % proxy_port,
        "EMORI_SKILL_KEY": skill_key,
        # The hostapp is the FIWARE writer everywhere; two writers double every KPI.
        "FIWARE_BRIDGE_ENABLED": "0",
        "EMORI_WITH_FIWARE": "false",
    })
    return env


def v2_container_env(owner: str, *, host_port: int,
                     base: Optional[dict] = None) -> dict[str, str]:
    """The environment `docker compose up` substitutes into docker-compose.v2.yml.

    Shorter than v1's on purpose, and the absences are the interesting part:

    * no EMORI_SKILL_URL and no EMORI_SKILL_KEY. `ros_native` does not POST anything, so
      there is no intake to address and no shared secret to get wrong. That also means
      no loopback proxy: the hop existed only because Docker Desktop cannot host-network
      and /api/skill refuses anything that is not 127.0.0.1.
    * REACHY_BACKEND=mujoco, because in v2 the container IS the robot.
    """

    env = dict(os.environ if base is None else base)
    env.update({
        "REACHY_BACKEND": "mujoco",
        "EMORI_SOURCE": "pokamind",
        "EMORI_WS_URL": "ws://host.docker.internal:%d/ws/relay" % host_port,
        "EMORI_ROS_MOTION_OWNER": owner,
        "EMORI_SKILL_TRANSPORT": "ros_native",
        "FIWARE_BRIDGE_ENABLED": "0",
        "EMORI_WITH_FIWARE": "false",
    })
    return env


# --- the live parts ------------------------------------------------------------------

def _stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def log(message: str) -> None:
    print("[%s] %s" % (_stamp(), message), flush=True)


def _python() -> str:
    """The venv interpreter by absolute path; the 3.14 system Python has no mujoco."""

    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _http_ok(url: str, *, timeout_s: float = 2.0) -> bool:
    try:
        return httpx.get(url, timeout=timeout_s).status_code < 500
    except Exception:  # noqa: BLE001 - "not up yet" is every exception this can raise
        return False


def _wait_for(predicate, *, seconds: float, what: str, poll_s: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            log("  %s is up" % what)
            return True
        time.sleep(poll_s)
    log("  TIMEOUT: %s did not come up within %.0fs" % (what, seconds))
    return False


def _popen(args: Sequence[str], *, env: Optional[dict] = None,
           cwd: Optional[Path] = None) -> subprocess.Popen:
    return subprocess.Popen(list(args), env=env, cwd=str(cwd or ROOT),
                            creationflags=_NEW_GROUP)


def _run(args: Sequence[str], *, env: Optional[dict] = None,
         timeout_s: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=str(ROOT), env=env, capture_output=True,
                          text=True, timeout=timeout_s, check=False)


class PoseSampler(threading.Thread):
    """The one witness that is not a counter written by the code under test.

    The route differs by robot mode. v1 polls the daemon's
    /api/state/present_head_pose; v2's REST shim does not implement that route and
    serves /api/state/full instead, so the reading is reduced to its motion fields by
    `pose_of_state` before it is stored -- the document also carries a timestamp, and a
    clock is not a head.
    """

    def __init__(self, *, hz: float = POSE_SAMPLE_HZ,
                 path: str = "/api/state/present_head_pose") -> None:
        super().__init__(name="pose-sampler", daemon=True)
        self.period_s = 1.0 / max(0.5, hz)
        self.path = path
        self.samples: list[Any] = []
        # NOT `self._stop`: threading.Thread uses a private method of that exact name in
        # join()/_wait_for_tstate_lock(), so shadowing it with an Event makes every join
        # raise "'Event' object is not callable" after the thread has finished.
        self._stopping = threading.Event()

    def run(self) -> None:
        client = httpx.Client(base_url=DAEMON_URL, timeout=1.5)
        try:
            while not self._stopping.is_set():
                try:
                    self.samples.append(pose_of_state(client.get(self.path).json()))
                except Exception:  # noqa: BLE001 - a missed sample is not a failure
                    pass
                self._stopping.wait(self.period_s)
        finally:
            client.close()

    def stop(self) -> None:
        self._stopping.set()


def daemon_running() -> bool:
    return _http_ok(DAEMON_URL + "/api/daemon/status")


def daemon_status() -> Optional[dict]:
    """Whatever is answering GET /api/daemon/status on :8000, or None if nothing is."""

    try:
        return httpx.get(DAEMON_URL + "/api/daemon/status", timeout=2.0).json()
    except Exception:  # noqa: BLE001 - "nothing there" is every exception this raises
        return None


def container_nodes(container: str = CONTAINER) -> list[str]:
    """`ros2 node list` inside the container, through the image's own entrypoint."""

    result = _run(["docker", "exec", container, "/entrypoint.sh", "ros2", "node", "list",
                   "--no-daemon", "--spin-time", "5"], timeout_s=30.0)
    if result.returncode != 0:
        return []
    return [line.strip().lstrip("/") for line in result.stdout.splitlines()
            if line.strip()]


def read_diagnostics(container: str = CONTAINER) -> tuple[dict[str, dict[str, str]], str]:
    """Collect /diagnostics for DIAGNOSTICS_ECHO_S and parse the latest of each ledger."""

    result = _run(["docker", "exec", container, "/entrypoint.sh", "timeout",
                   str(DIAGNOSTICS_ECHO_S), "ros2", "topic", "echo", "--no-daemon",
                   "/diagnostics"],
                  timeout_s=DIAGNOSTICS_ECHO_S + 20.0)
    return parse_diagnostics(result.stdout), result.stdout


def measure_topic_hz(container: str, topic: str,
                     window_s: int = TOPIC_HZ_WINDOW_S) -> Optional[float]:
    """`ros2 topic hz <topic>` for a bounded window, parsed to one number.

    `ros2 topic hz` never exits on its own, so it is bounded by `timeout` INSIDE the
    container (the same shape the diagnostics echo uses) and killed by SIGTERM, which
    means a non-zero return code is normal and is ignored -- the output is the result.

    The topic path is passed as a list argument to subprocess, never through a shell.
    That matters on this box: a `/joint_states` typed into Git Bash would be rewritten by
    MSYS into `C:/Program Files/Git/joint_states` and the measurement would silently be
    of a topic that does not exist. Anyone running the command by hand here needs
    MSYS_NO_PATHCONV=1.
    """

    result = _run(["docker", "exec", container, "/entrypoint.sh", "timeout",
                   str(window_s), "ros2", "topic", "hz", topic],
                  timeout_s=window_s + 30.0)
    return parse_topic_hz(result.stdout + result.stderr)


def container_logs(container: str = CONTAINER) -> list[str]:
    result = _run(["docker", "logs", "--tail", str(CONTAINER_LOG_TAIL), container],
                  timeout_s=30.0)
    return (result.stdout + result.stderr).splitlines()


def _terminate(process: Optional[subprocess.Popen], name: str) -> None:
    if process is None or process.poll() is not None:
        return
    log("  stopping %s (pid %s)" % (name, process.pid))
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:  # noqa: BLE001 - escalate rather than leave it running
        try:
            process.kill()
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - nothing further this process can do
            log("  WARN: %s (pid %s) did not stop" % (name, process.pid))


def print_table(owner: str, checks: list[Check], *, robot: str = "daemon") -> bool:
    width = max(len(c.name) for c in checks) if checks else 10
    print("")
    print("=== rehearsal: EMORI_ROS_MOTION_OWNER=%s robot=%s ===" % (owner, robot))
    for check in checks:
        print("  %-4s %-*s  %s" % ("PASS" if check.ok else "FAIL", width,
                                   check.name, check.detail))
    passed = all(c.ok for c in checks)
    print("  --> %s (%d/%d)" % ("PASS" if passed else "FAIL",
                                sum(1 for c in checks if c.ok), len(checks)))
    return passed


def rehearse(args: argparse.Namespace) -> int:
    owner: str = args.owner
    robot: str = args.robot
    native = robot == "native"
    skill_key = secrets.token_hex(16)
    fixture = Path(args.fixture).resolve()
    if not fixture.exists():
        log("fixture not found: %s (run scripts/gen_rehearsal_fixture.py)" % fixture)
        return 1

    compose_file = compose_file_for(robot)
    container = container_for(robot)
    image = args.image or default_image_for(robot)
    expected_nodes = expected_nodes_for(robot)
    # v2's shim has no /api/state/present_head_pose; /api/state/full is the route the
    # hostapp's own gesture sensor polls at 10 Hz, so it is the one that must work.
    pose_path = "/api/state/full" if native else "/api/state/present_head_pose"

    started_daemon: Optional[subprocess.Popen] = None
    proxy: Optional[subprocess.Popen] = None
    hostapp: Optional[subprocess.Popen] = None
    sampler: Optional[PoseSampler] = None
    compose_up = False
    ledger: dict[str, Any] = {
        "owner": owner,
        "robot": robot,
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fixture": str(fixture),
        "host_port": args.host_port,
        "proxy_port": args.proxy_port,
        "image": image,
        "compose_file": str(compose_file),
        "container": container,
        "fiware": bool(args.fiware),
        "hostapp_env": {k: v for k, v in
                        hostapp_env(owner, port=args.host_port, skill_key="<redacted>",
                                    base={}, fiware=args.fiware,
                                    orion_url=args.orion_url,
                                    skill_api=not native).items()},
        "container_env": (
            {k: v for k, v in v2_container_env(owner, host_port=args.host_port,
                                               base={}).items()}
            if native else
            {k: v for k, v in container_env(owner, host_port=args.host_port,
                                            proxy_port=args.proxy_port,
                                            skill_key="<redacted>", base={}).items()}),
    }
    ok = False
    try:
        # 1. the robot -----------------------------------------------------------
        if native:
            # The interlock. Same port, same dialect, one field's difference -- see
            # native_port_blocker() for why a silent pass is the failure mode here.
            blocker = native_port_blocker(daemon_status())
            if blocker:
                log("1. REFUSING TO RUN: %s" % blocker)
                return 1
            log("1. --robot native: no daemon; :8000 is free (or already our shim)")
        elif daemon_running():
            log("1. Reachy daemon already up on :8000 (left running at the end)")
        else:
            log("1. starting Reachy daemon --sim --headless --no-media")
            started_daemon = _popen([str(DAEMON_EXE), "--sim", "--headless", "--no-media"])
            if not _wait_for(daemon_running, seconds=DAEMON_READY_S, what="daemon"):
                return 1

        # 2. the loopback hop ----------------------------------------------------
        if native:
            # ros_native posts nothing over HTTP, so the hop that existed only to pass
            # /api/skill's loopback gate has nothing left to carry.
            log("2. no loopback proxy in --robot native (the facade uses ROS actions)")
        elif owner == "ros":
            log("2. starting loopback proxy :%d -> 127.0.0.1:%d"
                % (args.proxy_port, args.host_port))
            proxy = _popen([_python(), str(PROXY_SCRIPT),
                            "--listen", str(args.proxy_port),
                            "--target", str(args.host_port)])
        else:
            log("2. no loopback proxy needed (owner=%s never posts)" % owner)

        # 3. the hostapp ---------------------------------------------------------
        log("3. starting hostapp on :%d (owner=%s, skill intake %s)"
            % (args.host_port, owner, "CLOSED" if native else "open"))
        hostapp = _popen([_python(), "-m", "emori_adapter.hostapp.server"],
                         env=hostapp_env(owner, port=args.host_port,
                                         skill_key=skill_key, fiware=args.fiware,
                                         orion_url=args.orion_url,
                                         skill_api=not native))
        health = "http://127.0.0.1:%d/healthz" % args.host_port
        if not _wait_for(lambda: _http_ok(health, timeout_s=HOSTAPP_PROBE_TIMEOUT_S),
                         seconds=HOSTAPP_READY_S, what="hostapp"):
            return 1

        # 4. the graph -----------------------------------------------------------
        if not args.build and _run(["docker", "image", "inspect", image],
                                   timeout_s=60.0).returncode != 0:
            # A missing image would otherwise surface as a compose error 20 lines deep.
            # Note the arch: emori/ros2-humble:latest is an arm64 build for the CM4 and
            # runs here only under emulation, far too slowly to rehearse against.
            log("image %s is not present. Build it for this host first:\n"
                "  docker compose -f %s build\n"
                "...or re-run with --build." % (image, compose_file))
            return 1
        compose = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
        if args.build:
            compose.append("--build")
        log("4. %s" % " ".join(compose))
        env = (v2_container_env(owner, host_port=args.host_port) if native else
               container_env(owner, host_port=args.host_port,
                             proxy_port=args.proxy_port, skill_key=skill_key))
        result = _run(compose, env=env, timeout_s=1800.0 if args.build else 300.0)
        compose_up = True
        if result.returncode != 0:
            log("  docker compose up failed:\n%s\n%s" % (result.stdout, result.stderr))
            return 1

        def nodes_ready() -> bool:
            seen = set(container_nodes(container))
            return all(node in seen for node in expected_nodes)

        if not _wait_for(nodes_ready, seconds=NODES_READY_S,
                         what="the %d ROS nodes" % len(expected_nodes), poll_s=3.0):
            log("  nodes seen: %s" % sorted(container_nodes(container)))
            return 1
        ledger["nodes"] = sorted(container_nodes(container))

        # 4b. native only: the shim must be OURS and running -----------------------
        # The node list above proves reachy_mini started; this proves the compatibility
        # shim inside it bound the published port and speaks the dialect the hostapp is
        # about to use. Without it a rehearsal can reach step 6 and only then discover
        # that every RobotPlayer request got a connection refused.
        if native:
            if not _wait_for(lambda: shim_is_ready(daemon_status()),
                             seconds=DAEMON_READY_S,
                             what="the REST shim on :8000 (backend=%s)" % SHIM_BACKEND):
                log("  /api/daemon/status said: %r" % (daemon_status(),))
                return 1
            ledger["shim_status"] = daemon_status()

        # 5. the witness ---------------------------------------------------------
        log("5. sampling %s%s at %.0f Hz" % (DAEMON_URL, pose_path, POSE_SAMPLE_HZ))
        sampler = PoseSampler(path=pose_path)
        sampler.start()

        # 5b. --fiware only: open a hostapp SESSION ------------------------------
        # The fixture is a stream of /api/cycle envelopes and nothing else, so on a
        # plain run every cycle lands as `cycles_outside_session` and no Session
        # entity is ever minted. The NGSI-LD writer is built in start_session(), and
        # start_session() is called from GET /session (the kiosk page load IS the
        # start of a session). One GET is therefore the whole difference between a
        # broker that receives this rehearsal and a broker that does not.
        if args.fiware:
            log("5b. opening a hostapp session (GET /session) so FIWARE has one to write")
            try:
                opened = httpx.get("http://127.0.0.1:%d/session" % args.host_port,
                                   timeout=30.0)
                log("   /session -> HTTP %d" % opened.status_code)
                ledger["session_page_status"] = opened.status_code
            except Exception as e:  # noqa: BLE001 - report it, do not mask it
                log("   /session failed: %s" % e)
                ledger["session_page_status"] = str(e)

        # 6. the replay ----------------------------------------------------------
        log("6. replaying %s (real timing)" % fixture.name)
        replay = _run([_python(), str(REPLAY_SCRIPT), str(fixture), "--base-url",
                       "http://localhost:%d" % args.host_port], timeout_s=300.0)
        ledger["replay_returncode"] = replay.returncode
        ledger["replay_tail"] = replay.stdout.splitlines()[-20:]
        log("   replay finished (rc=%d); waiting %.0fs for the tail"
            % (replay.returncode, TAIL_WAIT_S))
        time.sleep(TAIL_WAIT_S)

        # 7. collect -------------------------------------------------------------
        log("7. collecting counters and both /diagnostics ledgers")
        sampler.stop()
        sampler.join(timeout=5.0)
        pose_changes = count_pose_changes(sampler.samples)
        try:
            status = httpx.get("http://127.0.0.1:%d/api/status" % args.host_port,
                               timeout=10.0).json()
        except Exception as e:  # noqa: BLE001 - report it, do not mask it
            log("  /api/status unreadable: %s" % e)
            status = {}
        counters = dict(status.get("counters") or {})
        statuses, raw_echo = read_diagnostics(container)
        facade = statuses.get(FACADE_STATUS, {})
        behaviors = statuses.get(BEHAVIORS_STATUS, {})
        robot_ledger = statuses.get(ROBOT_STATUS, {})
        joint_hz: Optional[float] = None
        camera_hz: Optional[float] = None
        if native:
            # Measured AFTER the replay and with the sampler stopped: an idle graph is
            # the honest steady state for a control loop, and a rate taken while the
            # box is also replaying 39 envelopes measures the replay.
            log("7b. measuring %s and %s (%ds each)"
                % (JOINT_STATES_TOPIC, CAMERA_TOPIC, TOPIC_HZ_WINDOW_S))
            joint_hz = measure_topic_hz(container, JOINT_STATES_TOPIC)
            camera_hz = measure_topic_hz(container, CAMERA_TOPIC)
            log("   %s = %s Hz, %s = %s Hz"
                % (JOINT_STATES_TOPIC, joint_hz, CAMERA_TOPIC, camera_hz))
        ledger.update({
            "pose_samples": len(sampler.samples),
            "pose_changes": pose_changes,
            "hostapp_counters": counters,
            "diagnostics": statuses,
            "diagnostics_raw_chars": len(raw_echo),
            "joint_states_hz": joint_hz,
            "camera_hz": camera_hz,
            "container_log": container_logs(container),
        })

        # 9. assert --------------------------------------------------------------
        if native:
            checks = evaluate_native(owner, hostapp=counters, facade=facade,
                                     behaviors=behaviors, robot=robot_ledger,
                                     pose_changes=pose_changes, joint_hz=joint_hz,
                                     camera_hz=camera_hz)
        else:
            checks = evaluate(owner, hostapp=counters, facade=facade,
                              behaviors=behaviors, pose_changes=pose_changes)
        ledger["checks"] = [{"name": c.name, "ok": c.ok, "detail": c.detail}
                            for c in checks]
        ok = print_table(owner, checks, robot=robot)
        ledger["passed"] = ok
        return 0 if ok else 1
    finally:
        # 8. teardown ------------------------------------------------------------
        ledger.setdefault("passed", ok)
        # v1's name is left exactly as it was, so older ledgers and any script that
        # globs them keep working; a native run says so in the filename.
        stem = owner if robot == "daemon" else "%s-%s" % (robot, owner)
        out = Path(args.json) if args.json else (
            ROOT / "artifacts" / "rehearsals" / ("%s-%s.json" % (
                dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"), stem)))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(ledger, indent=2, default=str) + "\n",
                           encoding="utf-8")
            log("ledger: %s" % out)
        except OSError as e:
            log("could not write the ledger (%s)" % e)
        if sampler is not None:
            sampler.stop()
        if args.keep_up:
            log("--keep-up: leaving the container, hostapp and proxy running")
        else:
            log("8. tearing down")
            if compose_up:
                _run(["docker", "compose", "-f", str(compose_file), "down"],
                     timeout_s=180.0)
            _terminate(hostapp, "hostapp")
            _terminate(proxy, "loopback proxy")
            # Only if THIS run started it: a daemon someone left up with a MuJoCo
            # window open is somebody's live session, not this script's to kill.
            if started_daemon is not None:
                _terminate(started_daemon, "reachy daemon")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Rehearse EMORI_ROS_MOTION_OWNER end to end in the simulator.")
    ap.add_argument("--owner", required=True, choices=OWNERS,
                    help="which lane owns expressive motion for this run")
    ap.add_argument("--robot", default="daemon", choices=ROBOT_MODES,
                    help="daemon = v1 (Pollen's daemon on this host, motion over HTTP); "
                         "native = v2 (no daemon; reachy_mini_ros2 owns the backend "
                         "inside the container and its REST shim answers :8000)")
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE),
                    help="JSONL session to replay (default: the rehearsal fixture)")
    ap.add_argument("--host-port", type=int, default=8095,
                    help="hostapp port (8080/8090 are squatted on this box)")
    ap.add_argument("--proxy-port", type=int, default=8096,
                    help="loopback hop port, owner=ros + --robot daemon only")
    ap.add_argument("--image", default="",
                    help="image the compose file is expected to have built "
                         "(default: %s for --robot daemon, %s for --robot native)"
                         % (V1_IMAGE, V2_IMAGE))
    ap.add_argument("--build", dest="build", action="store_true",
                    help="docker compose up --build (slow; off by default)")
    ap.add_argument("--no-build", dest="build", action="store_false",
                    help="reuse the existing image (the default)")
    ap.set_defaults(build=False)
    ap.add_argument("--fiware", action="store_true",
                    help="run the hostapp with EMORI_FIWARE=1 and ORION_URL set, so the "
                         "run leaves a Session entity for scripts/arise_consumer_check.py "
                         "(needs deploy/fiware/docker-compose.yml up)")
    ap.add_argument("--orion-url", default=DEFAULT_ORION_URL,
                    help="Orion-LD the hostapp writes to with --fiware")
    ap.add_argument("--keep-up", action="store_true",
                    help="leave the container, hostapp and proxy running afterwards")
    ap.add_argument("--json", default="",
                    help="where to write the ledger "
                         "(default: artifacts/rehearsals/<utc>-<owner>.json)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return rehearse(args)


if __name__ == "__main__":
    sys.exit(main())
