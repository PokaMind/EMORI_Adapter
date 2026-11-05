"""The parts of the motion-owner rehearsal that can be proved without Docker.

``scripts/rehearse_motion_owner.py`` is the proof that ``EMORI_ROS_MOTION_OWNER``
actually does what it says, and the thing about a proof harness is that nobody reviews
it as carefully as the code it judges: a harness whose assertion table is subtly wrong
prints PASS for a broken flip, and that is worse than no rehearsal at all. So the three
pieces that decide PASS/FAIL -- parsing the ROS ledgers, counting pose changes, and the
table itself -- are unit-tested here against literal inputs, with no daemon, no hostapp
and no container in sight.

Plus the fixture: ``tests/fixtures/rehearsal_session.jsonl`` is generated, and a
generated file that has drifted from its generator is a fixture nobody can regenerate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HARNESS_PATH = _REPO / "scripts" / "rehearse_motion_owner.py"
_GENERATOR_PATH = _REPO / "scripts" / "gen_rehearsal_fixture.py"
_FIXTURE = _REPO / "tests" / "fixtures" / "rehearsal_session.jsonl"


def _load(name: str, path: Path):
    """Import a script by path. ``scripts/`` is not a package and never will be.

    The module has to be in ``sys.modules`` BEFORE it executes: both files use
    ``from __future__ import annotations``, so every dataclass field annotation is a
    string that ``dataclasses`` resolves by looking its own module back up.
    """

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load("rehearse_motion_owner", _HARNESS_PATH)


@pytest.fixture(scope="module")
def generator():
    return _load("gen_rehearsal_fixture", _GENERATOR_PATH)


# A real `ros2 topic echo /diagnostics` stream: two DiagnosticArray documents separated
# by `---`, the second one cut off mid-message because the echo was stopped by a
# timeout. Both ledgers publish on the same topic, and the counters arrive as STRINGS
# (diagnostic_msgs/KeyValue.value is a string by definition).
ECHO = """header:
  stamp:
    sec: 1758000000
    nanosec: 500000000
  frame_id: ''
status:
- level: "\\0"
  name: emori_skill_facade
  message: ledger
  hardware_id: hostapp_http
  values:
  - key: posted
    value: '4'
  - key: accepted
    value: '2'
  - key: suppressed
    value: '0'
  - key: unreachable
    value: '0'
- level: "\\0"
  name: hri_engagement_behaviors
  message: ledger
  hardware_id: learner_01
  values:
  - key: cycles
    value: '9'
  - key: cue
    value: '2'
---
header:
  stamp:
    sec: 1758000005
    nanosec: 500000000
  frame_id: ''
status:
- level: "\\0"
  name: emori_skill_facade
  message: ledger
  hardware_id: hostapp_http
  values:
  - key: posted
    value: '31'
  - key: accepted
    value: '19'
  - key: suppressed
    value: '0'
  - key: unreachable
    value: '0'
- level: "\\0"
  name: hri_engagement_behaviors
  message: ledger
  hardware_id: learner_01
  values:
  - key: cycles
    value: '24'
  - key: cue
    value: '7'
---
header:
  stamp:
    sec: 17580000"""


# --- reading the two ROS ledgers --------------------------------------------------

def test_the_latest_status_per_name_wins(harness):
    """These are cumulative counters, so the newest document IS the whole run.

    Taking the first (or summing) would under- or over-count by however many 5 s ticks
    the echo happened to catch.
    """

    parsed = harness.parse_diagnostics(ECHO)

    assert set(parsed) == {"emori_skill_facade", "hri_engagement_behaviors"}
    assert parsed["emori_skill_facade"]["posted"] == "31"
    assert parsed["hri_engagement_behaviors"]["cycles"] == "24"


def test_a_truncated_trailing_document_is_skipped_not_fatal(harness):
    """The echo is always stopped mid-stream by `timeout`, so the last document is
    normally half-written. That must cost the run nothing."""

    assert harness.parse_diagnostics(ECHO)["emori_skill_facade"]["accepted"] == "19"


def test_an_empty_or_unparseable_echo_is_an_empty_ledger(harness):
    """No container, no topic, no ROS: the assertions then fail on the NUMBERS, with
    the numbers printed, rather than on a traceback nobody can read."""

    assert harness.parse_diagnostics("") == {}
    assert harness.parse_diagnostics("!!! not yaml at all: [") == {}


def test_string_counters_are_read_as_integers(harness):
    values = {"posted": "31", "accepted": "", "junk": "many"}
    assert harness.as_int(values, "posted") == 31
    assert harness.as_int(values, "accepted") == 0
    assert harness.as_int(values, "junk") == 0
    assert harness.as_int(values, "absent", 7) == 7


# --- the one witness that is not a counter ----------------------------------------

def test_pose_changes_count_only_real_movement(harness):
    """POSE_EPSILON sits above the MuJoCo sim's idle numerical drift and below any
    authored gesture, so a still robot reads 0 and a nodding one does not."""

    still = [[0.0, 0.0, 0.10], [0.0, 0.0, 0.1001], [0.0, 0.0, 0.0999]]
    assert harness.count_pose_changes(still) == 0

    nodding = [[0.0, 0.0, 0.10], [0.0, 0.25, 0.10], [0.0, 0.0, 0.10]]
    assert harness.count_pose_changes(nodding) == 2


def test_pose_readings_are_compared_whatever_shape_the_daemon_answers(harness):
    """The route has answered a flat list, a nested matrix and a dict of named axes
    across firmware revisions; the question ("did any component move") is the same."""

    nested = [[[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0], [0.0, 1.0]],
              [[1.0, 0.0], [0.0, 0.5]]]
    assert harness.count_pose_changes(nested) == 1

    named = [{"yaw": 0.0, "pitch": 0.0}, {"pitch": 0.0, "yaw": 0.4}]
    assert harness.count_pose_changes(named) == 1


def test_a_sample_of_a_different_shape_is_not_a_movement(harness):
    """The daemon answering differently is not the head moving."""

    assert harness.count_pose_changes([[0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0]]) == 0


def test_empty_and_unreadable_samples_are_skipped(harness):
    assert harness.count_pose_changes([]) == 0
    assert harness.count_pose_changes([None, {}, [], "nope"]) == 0


# --- the assertion table ----------------------------------------------------------

def _ros_run(harness, **overrides):
    """A healthy owner=ros run, so each test can break exactly one thing."""

    data = {
        "hostapp": {"engagement_suppressed_by_owner": 24, "skill_accepted": 19,
                    "skill_intents_submitted": 57, "skill_dropped": 3,
                    "skill_refused_rate_limited": 9},
        "facade": {"posted": "31", "accepted": "19", "suppressed": "0",
                   "unreachable": "0"},
        "behaviors": {"cycles": "24", "cue": "7"},
        "pose_changes": 41,
    }
    data.update(overrides)
    return harness.evaluate("ros", **data)


def _failed(checks) -> list[str]:
    return [c.name for c in checks if not c.ok]


def test_a_healthy_ros_run_passes_every_row(harness):
    assert _failed(_ros_run(harness)) == []


def test_a_ros_run_where_nothing_was_accepted_fails(harness):
    """The exact shape of "the facade posted and the hostapp refused every one"."""

    checks = _ros_run(
        harness,
        hostapp={"engagement_suppressed_by_owner": 24, "skill_refused_not_owner": 31},
        facade={"posted": "31", "accepted": "0", "suppressed": "0", "unreachable": "0"},
    )
    assert "hostapp.skill_accepted" in _failed(checks)
    assert "hostapp.skill_refused_not_owner" in _failed(checks)
    assert "facade.accepted" in _failed(checks)


def test_a_ros_run_that_could_not_reach_the_hostapp_fails(harness):
    """The Docker Desktop trap: no loopback hop, so every POST is refused or dropped."""

    checks = _ros_run(harness, facade={"posted": "31", "accepted": "0",
                                       "suppressed": "0", "unreachable": "31"})
    assert "facade.unreachable" in _failed(checks)


def test_a_ros_run_whose_robot_never_moved_fails(harness):
    """Counters on both sides can agree perfectly about a robot that stood still."""

    assert "pose_changes" in _failed(_ros_run(harness, pose_changes=0))


def test_a_ros_run_with_a_silent_graph_fails(harness):
    checks = _ros_run(harness, behaviors={"cycles": "0", "cue": "0"})
    assert "behaviors.cycles" in _failed(checks)
    assert "behaviors.cue" in _failed(checks)


def test_a_healthy_hostapp_run_passes(harness):
    """owner=hostapp promises zero behaviour change: the graph computes and publishes
    everything and POSTs nothing at all."""

    checks = harness.evaluate(
        "hostapp",
        hostapp={"cycles": 24},
        facade={"posted": "0", "suppressed": "31", "unreachable": "0"},
        behaviors={"cycles": "24", "cue": "7"},
        pose_changes=38,
    )
    assert _failed(checks) == []


def test_a_hostapp_run_where_the_facade_posted_anyway_fails(harness):
    """The double-drive trap, caught from either side: the facade's own ledger shows
    the posts AND the hostapp's shows what it did with them."""

    checks = harness.evaluate(
        "hostapp",
        hostapp={"skill_refused_not_owner": 12},
        facade={"posted": "12", "suppressed": "0"},
        behaviors={"cycles": "24"},
        pose_changes=38,
    )
    failed = _failed(checks)
    assert "facade.posted" in failed
    assert "facade.suppressed" in failed
    assert "hostapp.no skill_* counter moved" in failed


def test_a_hostapp_run_that_suppressed_its_own_lane_fails(harness):
    """`hostapp` must never stand its own expressive lanes down."""

    checks = harness.evaluate(
        "hostapp",
        hostapp={"engagement_suppressed_by_owner": 24},
        facade={"posted": "0", "suppressed": "31"},
        behaviors={"cycles": "24"},
        pose_changes=38,
    )
    assert "hostapp.engagement_suppressed_by_owner" in _failed(checks)


def test_a_healthy_none_run_passes_and_ignores_the_pose(harness):
    """`none` means nothing expressive moves; a still robot is the right answer, and
    the sim's own settling makes "pose_changes == 0" an unreliable assertion."""

    checks = harness.evaluate(
        "none",
        hostapp={"engagement_suppressed_by_owner": 24},
        facade={"posted": "0", "suppressed": "31"},
        behaviors={"cycles": "24"},
        pose_changes=0,
    )
    assert _failed(checks) == []
    assert "pose_changes" not in [c.name for c in checks]


def test_a_none_run_that_kept_driving_fails(harness):
    checks = harness.evaluate(
        "none",
        hostapp={"engagement_suppressed_by_owner": 0},
        facade={"posted": "0", "suppressed": "31"},
        behaviors={"cycles": "24"},
        pose_changes=0,
    )
    assert "hostapp.engagement_suppressed_by_owner" in _failed(checks)


@pytest.mark.parametrize("owner", ["ros", "hostapp", "none"])
def test_every_owner_demands_a_live_graph(harness, owner):
    """A container that never came up must never be able to pass any mode."""

    checks = harness.evaluate(owner, hostapp={}, facade={}, behaviors={},
                              pose_changes=0)
    assert "behaviors.cycles" in _failed(checks)


# --- the environment the rehearsal runs the hostapp with --------------------------

def test_the_hostapp_env_is_explicit_and_reproducible(harness):
    env = harness.hostapp_env("ros", port=8095, skill_key="abc", base={})

    assert env["HOSTAPP_PORT"] == "8095"
    assert env["EMORI_ROS_MOTION_OWNER"] == "ros"
    assert env["EMORI_SKILL_API"] == "1" and env["EMORI_SKILL_KEY"] == "abc"
    assert env["EMORI_GESTURE"] == "0" and env["EMORI_FIWARE"] == "0"
    assert env["EMORI_BEHAVIOR_CONFIG"] == "configs/behavior.yaml"
    # The idle scan would move the head on its own every 5 s, which is exactly the
    # measurement the pose sampler exists to make; and the fallback would turn an
    # owner=ros run into a hostapp run halfway through while still passing.
    assert env["EMORI_IDLE_AFTER_S"] == "0"
    assert env["EMORI_ROS_OWNER_FALLBACK_S"] == "0"
    # NOT set: the default 0.0.0.0 bind is what lets the container reach /ws/relay
    # through host.docker.internal. The loopback gate on /api/skill is unaffected --
    # the proxy connects to 127.0.0.1, which is the entire point of the hop.
    assert "HOSTAPP_HOST" not in env
    # --fiware is opt-in, and off it must not leave a broker URL behind at all: an
    # ORION_URL with EMORI_FIWARE=0 is the kind of half-set knob that makes a later
    # "why is nothing historized" hunt take an afternoon.
    assert "ORION_URL" not in env


def test_the_fiware_flag_points_the_hostapp_at_a_broker_and_nothing_else(harness):
    """--fiware: the analytics proof needs a Session
    entity in Orion-LD, and that is the only difference it makes."""

    off = harness.hostapp_env("ros", port=8095, skill_key="abc", base={})
    on = harness.hostapp_env("ros", port=8095, skill_key="abc", base={}, fiware=True,
                             orion_url="http://localhost:1026")

    assert on["EMORI_FIWARE"] == "1"
    assert on["ORION_URL"] == "http://localhost:1026"
    assert {k: v for k, v in on.items() if k not in {"EMORI_FIWARE", "ORION_URL"}} == \
        {k: v for k, v in off.items() if k != "EMORI_FIWARE"}


def test_the_container_env_points_at_the_proxy_not_the_hostapp(harness):
    env = harness.container_env("ros", host_port=8095, proxy_port=8096,
                                skill_key="abc", base={})

    assert env["EMORI_SKILL_URL"] == "http://host.docker.internal:8096/api/skill"
    assert env["EMORI_WS_URL"] == "ws://host.docker.internal:8095/ws/relay"
    assert env["EMORI_ROS_MOTION_OWNER"] == "ros"
    # The hostapp is the FIWARE writer everywhere; two writers double every KPI.
    assert env["FIWARE_BRIDGE_ENABLED"] == "0"
    assert env["EMORI_WITH_FIWARE"] == "false"


def test_the_harness_expects_the_whole_graph(harness):
    """Every node slice2.launch.py starts unconditionally, and no more.

    `fiware_bridge` is guarded by IfCondition(with_fiware) and container_env() sets
    EMORI_WITH_FIWARE=false, so expecting it would hang every run for NODES_READY_S.
    """

    assert set(harness.EXPECTED_NODES) == {
        "emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
        "skill_facade"}
    assert "fiware_bridge" not in harness.EXPECTED_NODES
    assert harness.container_env("ros", host_port=1, proxy_port=2, skill_key="",
                                 base={})["EMORI_WITH_FIWARE"] == "false"


# --- v2: --robot native -----------------------------------------------------------
# The v2 harness gains three witnesses the v1 one could not have (two topic rates and
# the robot node's own arbiter ledger) and loses one lane entirely (the HTTP intake).
# Each of those is a new way for a harness to print PASS about a broken flip, so each is
# driven here on literal inputs.


def test_the_settled_rate_is_the_one_that_counts(harness):
    """`ros2 topic hz` prints a running average until it is killed.

    The FIRST line is computed over a fraction of a window and always reads low; taking
    it would fail a perfectly healthy 50 Hz publisher.
    """

    echo = ("average rate: 31.402\n"
            "\tmin: 0.004s max: 0.061s std dev: 0.01121s window: 33\n"
            "average rate: 49.881\n"
            "\tmin: 0.018s max: 0.022s std dev: 0.00061s window: 83\n"
            "average rate: 49.973\n"
            "\tmin: 0.018s max: 0.022s std dev: 0.00048s window: 133\n")

    assert harness.parse_topic_hz(echo) == pytest.approx(49.973)


def test_no_rate_at_all_is_none_and_not_zero(harness):
    """"the topic does not exist" and "the topic is slow" are different answers.

    `ros2 topic hz` on a topic nobody publishes prints nothing at all and then dies to
    `timeout`; recording that as 0.0 would hide the difference in the ledger.
    """

    assert harness.parse_topic_hz("") is None
    assert harness.parse_topic_hz("WARNING: topic [/nope] does not appear to be published"
                                  ) is None
    assert harness.parse_topic_hz("average rate: not-a-number\n") is None


def test_a_rate_row_fails_loudly_when_nothing_was_measured(harness):
    checks = harness.evaluate_native(
        "ros", hostapp={"engagement_suppressed_by_owner": 24},
        facade={"posted": "31", "accepted": "31", "suppressed": "0", "unreachable": "0"},
        behaviors={"cycles": "24", "cue": "7"},
        robot={"accepted_ros": "31", "completed": "31"},
        pose_changes=41, joint_hz=None, camera_hz=8.0)
    failed = {c.name: c.detail for c in checks if not c.ok}

    assert "joint_states_hz" in failed
    assert "topic absent" in failed["joint_states_hz"]


def test_the_state_document_is_reduced_to_the_parts_that_can_move(harness):
    """GET /api/state/full carries a timestamp, and a clock is not a head.

    This is the single most dangerous thing in the native harness: left in, `timestamp`
    changes on EVERY sample, `pose_changes` equals the sample count, and the one witness
    that is not a counter written by the code under test becomes a witness that a clock
    is running.
    """

    full = {"control_mode": "enabled",
            "head_pose": {"x": 0.0, "y": 0.0, "z": 0.1,
                          "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "body_yaw": 0.0, "antennas_position": [0.0, 0.0],
            "head_joints": [0.0] * 7, "passive_joints": None, "doa": None,
            "timestamp": 1758000000.123}

    reduced = harness.pose_of_state(full)
    assert set(reduced) == {"head_pose", "body_yaw", "antennas_position"}

    later = dict(full, timestamp=1758000000.373)
    samples = [harness.pose_of_state(full), harness.pose_of_state(later)]
    assert harness.count_pose_changes(samples) == 0
    # ...and the raw documents would have counted that tick as motion.
    assert harness.count_pose_changes([full, later]) == 1


def test_a_v1_shaped_pose_reading_passes_straight_through(harness):
    """The daemon's /api/state/present_head_pose is not a full-state document.

    It answers with Pollen's `XYZRPYPose` - a MAPPING of six named axes - or, with
    `use_pose_matrix`, a flattened 4x4. Neither carries `head_pose`/`body_yaw`/
    `antennas_position`, so reducing them to those fields yields `{}` and the pose
    assertion reads zero motion from a head that moved all run (observed: a v1 rehearsal
    at `pose_changes 0 >= 5` with 218 good samples).
    """

    matrix = [[1.0, 0.0], [0.0, 1.0]]
    assert harness.pose_of_state(matrix) == matrix
    assert harness.pose_of_state(None) is None

    xyzrpy = {"x": 0.0, "y": 0.0, "z": 0.012,
              "roll": 0.0, "pitch": 0.1, "yaw": -0.25}
    assert harness.pose_of_state(xyzrpy) == xyzrpy
    moved = dict(xyzrpy, yaw=-0.10)
    assert harness.count_pose_changes(
        [harness.pose_of_state(xyzrpy), harness.pose_of_state(moved)]) == 1


def test_native_refuses_to_run_against_pollens_own_daemon(harness):
    """The interlock. Same port, same dialect, one field's difference.

    Without it a rehearsal started with the Windows daemon still up produces a
    completely healthy-looking run of v1 while claiming to be v2 — which is worse than
    a crash, because the numbers would be real.
    """

    # Pollen's daemon has no `backend` field at all.
    pollen = {"state": "running", "simulation_enabled": True, "no_media": True}
    assert harness.native_port_blocker(pollen)
    assert "reachy_mini_ros2" in harness.native_port_blocker(pollen)
    # Nothing there: compose is about to bind it, which is the intended case.
    assert harness.native_port_blocker(None) == ""
    # Our own shim, left up by --keep-up: fine, that is the same thing again.
    assert harness.native_port_blocker(
        {"state": "running", "backend": "reachy_mini_ros2"}) == ""
    # Something else entirely on :8000.
    assert harness.native_port_blocker({"backend": "some_other_daemon"})


def test_the_shim_must_be_running_and_ours_before_the_replay(harness):
    assert harness.shim_is_ready({"state": "running", "backend": "reachy_mini_ros2"})
    # Ours, but the backend behind it never came up.
    assert not harness.shim_is_ready({"state": "starting", "backend": "reachy_mini_ros2"})
    # Running, but it is the daemon.
    assert not harness.shim_is_ready({"state": "running"})
    assert not harness.shim_is_ready(None)


def _native_ros(harness, **overrides):
    """A healthy --robot native --owner ros run, so each test breaks one thing."""

    data = {
        "hostapp": {"engagement_suppressed_by_owner": 24, "cycles": 24},
        "facade": {"posted": "31", "accepted": "31", "suppressed": "0",
                   "unreachable": "0"},
        "behaviors": {"cycles": "24", "cue": "7"},
        "robot": {"accepted_ros": "31", "completed": "31", "submitted": "31",
                  "refused_dropped": "0"},
        "pose_changes": 41,
        "joint_hz": 49.9,
        "camera_hz": 9.8,
    }
    data.update(overrides)
    return harness.evaluate_native("ros", **data)


def test_a_healthy_native_ros_run_passes_every_row(harness):
    assert _failed(_native_ros(harness)) == []


def test_a_native_run_whose_control_loop_is_not_keeping_up_fails(harness):
    """Every counter can agree perfectly about a node whose 50 Hz loop is at 12 Hz."""

    assert "joint_states_hz" in _failed(_native_ros(harness, joint_hz=12.0))


def test_a_native_run_with_a_dead_camera_fails(harness):
    assert "camera_hz" in _failed(_native_ros(harness, camera_hz=0.0))


def test_a_native_ros_run_the_arbiter_refused_on_ownership_fails(harness):
    """The two owner gates disagreeing is a misconfigured rig, not arbitration.

    Drops and preemptions are healthy; `not_owner` means the facade thinks it owns
    expression and the robot node does not.
    """

    checks = _native_ros(harness, robot={"accepted_ros": "0", "completed": "0",
                                         "refused_not_owner": "31"})
    failed = _failed(checks)
    assert "robot.accepted_ros" in failed
    assert "robot.refused_not_owner" in failed
    assert "robot.completed" in failed


def test_a_native_run_that_left_the_http_intake_open_fails(harness):
    """--robot native shuts /api/skill. A skill counter that moved means a second lane.

    The assertion "no skill_* counter moved" only means something because the intake
    could not have served one -- hostapp_env(skill_api=False) is the other half of it.
    """

    checks = _native_ros(harness, hostapp={"engagement_suppressed_by_owner": 24,
                                           "skill_accepted": 4})
    assert "hostapp.no skill_* counter moved" in _failed(checks)


def test_a_native_ros_run_whose_robot_never_moved_fails(harness):
    assert "pose_changes" in _failed(_native_ros(harness, pose_changes=0))


def test_a_healthy_native_hostapp_run_passes(harness):
    """The migration promise, measured: a hostapp that was never told the daemon went
    away drives the robot through the REST shim and the arbiter counts it as `hostapp`."""

    checks = harness.evaluate_native(
        "hostapp",
        hostapp={"cycles": 24},
        facade={"posted": "0", "accepted": "0", "suppressed": "31"},
        behaviors={"cycles": "24", "cue": "7"},
        robot={"accepted_hostapp": "28", "completed": "28"},
        pose_changes=38, joint_hz=49.8, camera_hz=9.6)

    assert _failed(checks) == []


def test_a_native_hostapp_run_where_nothing_reached_the_arbiter_fails(harness):
    """The exact shape of "the shim is up and the hostapp cannot drive through it"."""

    checks = harness.evaluate_native(
        "hostapp",
        hostapp={"cycles": 24},
        facade={"posted": "0", "suppressed": "31"},
        behaviors={"cycles": "24"},
        robot={"accepted_hostapp": "0", "completed": "0"},
        pose_changes=38, joint_hz=49.8, camera_hz=9.6)
    failed = _failed(checks)

    assert "robot.accepted_hostapp" in failed
    assert "robot.completed" in failed


def test_a_native_hostapp_run_where_the_facade_posted_anyway_fails(harness):
    checks = harness.evaluate_native(
        "hostapp",
        hostapp={"cycles": 24},
        facade={"posted": "12", "suppressed": "0"},
        behaviors={"cycles": "24"},
        robot={"accepted_hostapp": "28", "completed": "28"},
        pose_changes=38, joint_hz=49.8, camera_hz=9.6)
    failed = _failed(checks)

    assert "facade.posted" in failed and "facade.suppressed" in failed


def test_a_healthy_native_none_run_passes_and_ignores_the_robots_own_counters(harness):
    """`none` authors no expressive motion, so asserting that moves completed would be
    asserting that nobody did what nobody was allowed to do.

    What IS asserted: the graph is alive, the robot node is publishing at rate, the
    facade suppressed everything, and the hostapp stood its own lanes down too. The
    ungated interaction mechanics (greeting, recenter, watchdog) still move the head a
    little and the sim settles on its own, which is why v1 measured 11 pose changes in
    this mode and why no floor belongs here.
    """

    checks = harness.evaluate_native(
        "none",
        hostapp={"engagement_suppressed_by_owner": 24},
        facade={"posted": "0", "suppressed": "31"},
        behaviors={"cycles": "24"},
        robot={"completed": "0", "submitted": "0"},
        pose_changes=0, joint_hz=49.9, camera_hz=9.7)

    assert _failed(checks) == []
    names = [c.name for c in checks]
    assert "pose_changes" not in names and "robot.completed" not in names


def test_a_native_none_run_that_kept_driving_fails(harness):
    checks = harness.evaluate_native(
        "none",
        hostapp={"engagement_suppressed_by_owner": 0},
        facade={"posted": "12", "suppressed": "0"},
        behaviors={"cycles": "24"},
        robot={"completed": "12"},
        pose_changes=40, joint_hz=49.9, camera_hz=9.7)
    failed = _failed(checks)

    assert "hostapp.engagement_suppressed_by_owner" in failed
    assert "facade.posted" in failed and "facade.suppressed" in failed


@pytest.mark.parametrize("owner", ["ros", "hostapp", "none"])
def test_every_native_owner_demands_a_live_loop_and_a_live_graph(harness, owner):
    """A container that came up and then wedged must never pass any mode."""

    checks = harness.evaluate_native(owner, hostapp={}, facade={}, behaviors={},
                                     robot={}, pose_changes=0, joint_hz=None,
                                     camera_hz=None)
    failed = _failed(checks)

    assert "behaviors.cycles" in failed
    assert "joint_states_hz" in failed and "camera_hz" in failed


def test_the_native_thresholds_say_what_they_mean(harness):
    """Named constants, and the two that decide "is the robot real" pinned by value."""

    assert harness.MIN_JOINT_STATE_HZ == 40.0      # 20% under the 50 Hz control loop
    assert harness.MIN_CAMERA_HZ == 3.0            # the sim renderer asks for 10 fps
    assert harness.JOINT_STATES_TOPIC == "/joint_states"
    assert harness.CAMERA_TOPIC == "/reachy_mini/camera/image_raw"
    assert harness.ROBOT_STATUS == "reachy_mini"
    assert harness.SHIM_BACKEND == "reachy_mini_ros2"


# --- what each robot mode brings up -------------------------------------------------


def test_the_two_robot_modes_never_share_a_file_an_image_or_a_container(harness):
    """v2 must not be able to disturb v1, nor v1 v2.

    Also: reachy-mini 1.10.0 needs Python >= 3.11, so it cannot be installed on Humble
    at all — --robot native against the v1 image would fail deep inside a launch file.
    """

    assert set(harness.ROBOT_MODES) == {"daemon", "native"}
    assert harness.compose_file_for("daemon").name == "docker-compose.yml"
    assert harness.compose_file_for("native").name == "docker-compose.v2.yml"
    assert harness.container_for("daemon") == "emori-ros2"
    assert harness.container_for("native") == "emori-ros2-v2"
    assert harness.default_image_for("daemon") == "emori/ros2-humble:latest"
    assert harness.default_image_for("native") == "emori/ros2-jazzy-v2:latest"


def test_native_waits_for_the_robot_node_as_well_as_the_graph(harness):
    """`reachy_mini` is the node that has to exist for any of this to mean anything."""

    assert set(harness.expected_nodes_for("daemon")) == set(harness.EXPECTED_NODES)
    assert "reachy_mini" not in harness.expected_nodes_for("daemon")
    native = set(harness.expected_nodes_for("native"))
    assert native == set(harness.EXPECTED_NODES) | {"reachy_mini"}


def test_native_closes_the_http_intake_the_hostapp_no_longer_needs(harness):
    """Everything else about the hostapp's environment is identical to a v1 run."""

    on = harness.hostapp_env("ros", port=8095, skill_key="abc", base={})
    off = harness.hostapp_env("ros", port=8095, skill_key="abc", base={},
                              skill_api=False)

    assert on["EMORI_SKILL_API"] == "1"
    assert off["EMORI_SKILL_API"] == "0"
    assert {k: v for k, v in off.items() if k != "EMORI_SKILL_API"} == \
        {k: v for k, v in on.items() if k != "EMORI_SKILL_API"}
    # Still pinned in native mode: the idle ticker would move the head on its own.
    assert off["EMORI_IDLE_AFTER_S"] == "0"
    assert off["EMORI_ROS_OWNER_FALLBACK_S"] == "0"


def test_the_v2_container_env_has_no_http_lane_to_configure(harness):
    """ros_native posts nothing, so there is no intake to address and no key to share.

    The absences are the interesting part: a v2 run that still carried EMORI_SKILL_URL
    would be one env var away from silently being a v1 run.
    """

    env = harness.v2_container_env("ros", host_port=8095, base={})

    assert env["EMORI_SKILL_TRANSPORT"] == "ros_native"
    assert env["REACHY_BACKEND"] == "mujoco"
    assert env["EMORI_WS_URL"] == "ws://host.docker.internal:8095/ws/relay"
    assert env["EMORI_ROS_MOTION_OWNER"] == "ros"
    assert "EMORI_SKILL_URL" not in env and "EMORI_SKILL_KEY" not in env
    # The hostapp is the FIWARE writer everywhere; two writers double every KPI.
    assert env["FIWARE_BRIDGE_ENABLED"] == "0"
    assert env["EMORI_WITH_FIWARE"] == "false"


def test_the_harness_offers_both_robot_modes_on_the_command_line(harness):
    parser = harness.build_parser()

    native = parser.parse_args(["--owner", "ros", "--robot", "native"])
    assert native.robot == "native" and native.image == ""

    default = parser.parse_args(["--owner", "ros"])
    assert default.robot == "daemon", "v1 stays the default"


# --- the fixture ------------------------------------------------------------------

def test_the_fixture_on_disk_is_what_the_generator_produces(generator):
    """A generated file that has drifted from its generator cannot be regenerated."""

    assert _FIXTURE.read_text(encoding="utf-8") == generator.generate()


def test_the_generator_is_deterministic(generator):
    assert generator.generate() == generator.generate()


def test_the_fixture_keeps_every_analysis_cycle_and_the_completion(generator):
    rows = [json.loads(line) for line in
            _FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = [r["type"] for r in rows]

    assert kinds.count("pokamind_analysis_update") == 24
    assert kinds[-1] == "pokamind_session_completed"
    # One end marker per sentence: without it the speaking window is only ever opened
    # and the ROS node's settle path never runs.
    assert kinds.count("pokamind_tts_ended") == kinds.count("pokamind_ai_utterance")


def test_every_sentence_carries_a_family_and_all_six_are_covered(generator):
    rows = [json.loads(line) for line in
            _FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    families = [r["cue_category"] for r in rows
                if r["type"] == "pokamind_ai_utterance"]

    assert families and all(families)
    assert set(families) == set(generator.CUE_FAMILIES)


def test_the_fixture_stays_inside_one_minute_of_replay(generator):
    rows = [json.loads(line) for line in
            _FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    stamps = [generator.parse_timestamp(r["timestamp"]) for r in rows
              if r.get("timestamp")]

    assert stamps == sorted(stamps), "replay_session.py replays in FILE order"
    assert (stamps[-1] - stamps[0]).total_seconds() <= 70.0
