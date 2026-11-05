"""The outside-consumer proof, checked without Docker and without ROS.

Two halves, and both would otherwise only be exercised by a 30 s container run on a
machine with the whole stack up — which is to say, almost never:

1. `tests/ros/outside_consumer.py` must import NOTHING of ours. That is not a style
   rule, it is the entire experiment: a consumer that quietly imported `emori_adapter`
   would "prove" our REP-155 topics are standard by reading them with our own code.
   Checked with `ast`, because a grep for the word would miss `import emori_msgs.msg`
   inside a function and a passing container run would miss it entirely.
2. `scripts/outside_consumer_check.py`'s judgement. The Docker orchestration cannot be
   tested here; the assertions can, and they are the part that decides PASS or FAIL, so
   the script keeps them in a pure function this file drives on sample reports.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONSUMER = _ROOT / "tests" / "ros" / "outside_consumer.py"
_CHECKER = _ROOT / "scripts" / "outside_consumer_check.py"
_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile.consumer"

_MODULE_NAME = "outside_consumer_check_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module name this file imports, anywhere in it."""

    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module; there is no package here to relative-import.
            assert node.level == 0, f"{path.name} uses a relative import"
            roots.add((node.module or "").split(".", 1)[0])
    return roots


# --- 1. the consumer is genuinely an outsider ----------------------------------------


def test_the_consumer_imports_nothing_of_ours():
    """The control experiment only controls for anything if it cannot see our code."""

    ours = sorted(root for root in _imported_roots(_CONSUMER) if root.startswith("emori"))
    assert ours == [], f"tests/ros/outside_consumer.py imports EMORI modules: {ours}"


def test_the_consumer_imports_only_ros_standards_and_the_standard_library():
    """Five ROS standards are the interface; everything else must be stdlib.

    `hri_msgs` is ROS4HRI; `sensor_msgs`/`tf2_msgs`/`std_msgs` are core ROS 2 and are how
    a robot publishes its state anywhere. A SIXTH dependency would mean the consumer
    image needs something beyond two apt packages, and the honest description of the
    experiment would change.
    """

    allowed = ({"rclpy", "hri_msgs", "std_msgs", "sensor_msgs", "tf2_msgs"}
               | set(sys.stdlib_module_names))
    outside = sorted(_imported_roots(_CONSUMER) - allowed)
    assert outside == [], f"unexpected dependencies in the outside consumer: {outside}"


def test_the_consumer_matches_the_latched_qos_or_it_sees_nothing():
    """`tracked`/`anonymous` are transient_local depth 1; volatile matches no publisher.

    This is the single most likely way for the check to report an empty graph that
    looks exactly like a robot publishing nothing (docs/ros4hri-mapping.md).
    """

    source = _CONSUMER.read_text(encoding="utf-8")
    profile = source.split("_LATCHED = ", 1)[1].split("\n\n", 1)[0]
    assert "depth=1" in profile
    assert "TRANSIENT_LOCAL" in profile
    assert "RELIABLE" in profile
    for topic in ('"/humans/persons/tracked"', 'f"{base}/anonymous"'):
        subscription = source.split(topic, 1)[1].split(")", 1)[0]
        assert "_LATCHED" in subscription, topic


def test_the_consumer_reads_the_expression_vocabulary_off_the_installed_message():
    """Hardcoding our seven labels would make the check agree with us, not with hri_msgs.

    The container's hri_msgs 2.1.0 declares more labels than EMORI uses (`pleading`,
    `vulnerable`, ...), so a hardcoded list would also report false unknowns the first
    time somebody legitimately extended the set.
    """

    source = _CONSUMER.read_text(encoding="utf-8")
    assert "message_constants(Expression)" in source
    for label in ("happy", "sad", "angry", "surprised", "disgusted"):
        assert f'"{label}"' not in source, f"the label {label!r} is hardcoded"


def test_the_consumer_bounds_its_own_run():
    """`rclpy.spin` never returns, and this container exists to write a file and exit."""

    source = _CONSUMER.read_text(encoding="utf-8")
    assert "spin_once" in source
    assert "time.monotonic()" in source and "deadline" in source
    assert "rclpy.spin(" not in source


def test_the_consumer_image_carries_no_emori_code():
    """Instructions only: the comments in that file talk about emori_msgs on purpose."""

    raw = _DOCKERFILE.read_text(encoding="utf-8").splitlines()
    dockerfile = "\n".join(line for line in raw if not line.lstrip().startswith("#"))
    assert "hri-msgs" in dockerfile
    assert "tests/ros/outside_consumer.py" in dockerfile
    # The two things that would end the experiment: our workspace, or a way to install it.
    assert "emori_msgs" not in dockerfile
    assert "COPY src" not in dockerfile and "COPY ros2_ws" not in dockerfile
    assert "pip install" not in dockerfile
    # Exactly one COPY, and it is the script.
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert copies == ["COPY tests/ros/outside_consumer.py /consumer/outside_consumer.py"]


# --- v2: one Dockerfile, two distros, and the robot's own topics ----------------------


def test_the_consumer_image_takes_its_distro_as_a_build_arg():
    """A Humble consumer reading a Jazzy graph discovers every topic and receives none.

    DDS interoperates across distros; the message type hashes do not. So v1's check and
    v2's check need different images, and the way that must NOT be solved is a second
    near-identical Dockerfile — it would drift the day one of them grew a package.
    """

    raw = _DOCKERFILE.read_text(encoding="utf-8").splitlines()
    dockerfile = "\n".join(line for line in raw if not line.lstrip().startswith("#"))
    assert "ARG BASE=ros:jazzy-ros-base" in dockerfile
    assert "FROM ${BASE}" in dockerfile
    # The apt names are built from the distro arg, not written twice.
    assert "ros-${ROS_DISTRO_ARG}-hri-msgs" in dockerfile
    assert "ros-${ROS_DISTRO_ARG}-tf2-msgs" in dockerfile
    assert "ros-humble-" not in dockerfile and "ros-jazzy-" not in dockerfile


def test_the_checker_pairs_each_profile_with_its_own_distro_and_container():
    """The pairing is the thing: a v2 container read by a v1 consumer is a silent zero."""

    assert checker.V1_BASE == "ros:humble-ros-base" and checker.V1_DISTRO == "humble"
    assert checker.V2_BASE == "ros:jazzy-ros-base" and checker.V2_DISTRO == "jazzy"
    assert checker.V2_CONTAINER == "emori-ros2-v2"
    assert checker.DEFAULT_CONTAINER == "emori-ros2"
    # Different tags, or one `docker build` silently replaces the other's image.
    assert checker.V2_IMAGE != checker.DEFAULT_IMAGE


def test_the_consumer_subscribes_to_the_robots_own_topics():
    """`/joint_states` and `/tf` are the v2 claim: the robot is ON the graph.

    Read off the source rather than asserted through a run, because there is no ROS
    here — and because the topic NAMES are the contract with every other ROS tool.
    """

    source = _CONSUMER.read_text(encoding="utf-8")
    assert '"/joint_states"' in source and '"/tf"' in source
    assert "JointState" in source and "TFMessage" in source


def test_the_joint_state_queue_is_deep_enough_for_a_50_hz_stream():
    """Depth 10 at 50 Hz holds 0.2 s, and the node spins with a 0.2 s timeout.

    The symptom of getting this wrong is not an error: it is a measured rate that reads
    ~35 Hz against a publisher doing a perfect 50, i.e. a FAIL the graph did not earn.
    """

    source = _CONSUMER.read_text(encoding="utf-8")
    depth = int(source.split("_STREAM_DEPTH = ", 1)[1].split("\n", 1)[0])
    assert depth >= 50
    for topic in ('"/joint_states"', '"/tf"'):
        subscription = source.split(topic, 1)[1].split(")", 1)[0]
        assert "_STREAM_DEPTH" in subscription, topic


_V2_EXTRAS = {
    "joint_state_count": 1481,
    "joint_names": list(checker.EXPECTED_JOINT_NAMES),
    "joint_state_hz": 49.9,
    "tf_frames": ["base_link->head"],
}


def _v2_report(**overrides) -> dict:
    report = _report(**_V2_EXTRAS)
    report.update(overrides)
    return report


def test_a_healthy_v2_report_passes_with_nothing_to_say():
    assert checker.evaluate_v2(_v2_report()) == []


def test_a_v1_report_run_through_the_v2_check_fails_on_every_row():
    """v1 publishes neither topic, so `--v2` against v1 must be loud, not half-green."""

    failures = checker.evaluate_v2(_report())
    assert len(failures) == 4


def test_a_single_latched_joint_state_is_not_a_stream():
    failures = checker.evaluate_v2(_v2_report(joint_state_count=1))
    assert len(failures) == 1 and "/joint_states messages" in failures[0]


def test_every_one_of_the_nine_joints_must_be_named_on_the_wire():
    """A consumer addresses a motor by name or not at all; eight of nine is a gap."""

    short = [n for n in checker.EXPECTED_JOINT_NAMES if n != "left_antenna"]
    failures = checker.evaluate_v2(_v2_report(joint_names=short))
    assert len(failures) == 1 and "left_antenna" in failures[0]


def test_an_unexpected_joint_name_is_reported_too():
    """The names are a contract in both directions: a stray one means a renamed motor."""

    wide = list(checker.EXPECTED_JOINT_NAMES) + ["neck_pitch"]
    failures = checker.evaluate_v2(_v2_report(joint_names=wide))
    assert len(failures) == 1 and "neck_pitch" in failures[0]


def test_the_antenna_wire_order_is_part_of_the_expected_set():
    """[right, left] is the daemon's order and therefore the arbiter's and the shim's.

    Pinned here because it is the one name pair a reader would "fix" on sight.
    """

    assert checker.EXPECTED_JOINT_NAMES.index("right_antenna") < \
        checker.EXPECTED_JOINT_NAMES.index("left_antenna")
    assert len(checker.EXPECTED_JOINT_NAMES) == 9


def test_a_slow_control_loop_fails_even_with_plenty_of_messages():
    """Count and rate are different failures: a 20 Hz loop still sends 600 in 30 s."""

    failures = checker.evaluate_v2(_v2_report(joint_state_hz=20.0))
    assert len(failures) == 1 and "Hz" in failures[0]


def test_exactly_the_rate_threshold_passes():
    at = float(checker.MIN_JOINT_STATE_HZ)
    assert checker.evaluate_v2(_v2_report(joint_state_hz=at)) == []


def test_the_head_must_be_locatable_in_the_base_frame():
    """Without base_link -> head the pose is a number, not something rviz can draw."""

    failures = checker.evaluate_v2(_v2_report(tf_frames=["odom->base_link"]))
    assert len(failures) == 1 and "base_link->head" in failures[0]


def test_extra_tf_edges_are_fine():
    """A robot with a camera frame or a map is not one that broke the contract."""

    wide = ["base_link->head", "head->eye_camera", "map->base_link"]
    assert checker.evaluate_v2(_v2_report(tf_frames=wide)) == []


@pytest.mark.parametrize("report", [{}, "not a report", {"joint_state_count": None}])
def test_a_missing_or_malformed_v2_report_fails_instead_of_raising(report):
    failures = checker.evaluate_v2(report)
    assert failures and all(isinstance(line, str) for line in failures)


# --- 2. the checker's judgement -------------------------------------------------------


_VOCABULARY = ["angry", "disgusted", "happy", "neutral", "sad", "scared", "surprised"]


def _report(**overrides) -> dict:
    """A report from a healthy 30 s run, which every case below spoils one way."""

    report = {
        "person_id": "learner_01",
        "tracked_ids": ["learner_01"],
        "anonymous": False,
        "engagement_counts": {"2": 4, "3": 9, "4": 2},
        "invalid_levels": 0,
        "expressions": {"happy": 6, "neutral": 9},
        "unknown_expression_labels": [],
        "expression_vocabulary": list(_VOCABULARY),
        "topics_seen": {"/humans/persons/learner_01/context": ["emori_msgs/msg/Context"]},
        "context_decodable": False,
    }
    report.update(overrides)
    return report


def test_a_healthy_report_passes_with_nothing_to_say():
    assert checker.evaluate(_report(), "learner_01", _VOCABULARY) == []


def test_the_person_must_be_announced_on_the_latched_tracked_topic():
    """A standard consumer discovers the person there or not at all."""

    failures = checker.evaluate(_report(tracked_ids=[]), "learner_01", _VOCABULARY)
    assert len(failures) == 1 and "tracked_ids" in failures[0]


def test_a_second_person_in_the_tracked_list_is_a_failure():
    failures = checker.evaluate(_report(tracked_ids=["learner_01", "learner_02"]),
                                "learner_01", _VOCABULARY)
    assert len(failures) == 1 and "tracked_ids" in failures[0]


def test_too_few_engagement_messages_reads_as_a_dead_lane():
    failures = checker.evaluate(_report(engagement_counts={"3": 2}), "learner_01",
                                _VOCABULARY)
    assert len(failures) == 1 and "engagement_status" in failures[0]


def test_exactly_the_threshold_passes():
    """The boundary is a decision, so it is pinned rather than left to the next reader."""

    at_threshold = {"3": checker.MIN_ENGAGEMENT_SAMPLES}
    assert checker.evaluate(_report(engagement_counts=at_threshold), "learner_01",
                            _VOCABULARY) == []


def test_a_level_outside_the_rep_constants_is_a_failure():
    failures = checker.evaluate(_report(invalid_levels=3), "learner_01", _VOCABULARY)
    assert len(failures) == 1 and "invalid_levels" in failures[0]


def test_a_label_outside_the_installed_vocabulary_is_a_failure():
    failures = checker.evaluate(_report(expressions={"neutral": 3, "smug": 1}),
                                "learner_01", _VOCABULARY)
    assert len(failures) == 1 and "smug" in failures[0]


def test_the_consumers_own_unknown_list_is_believed():
    failures = checker.evaluate(_report(unknown_expression_labels=["smug"]),
                                "learner_01", _VOCABULARY)
    assert len(failures) == 1 and "smug" in failures[0]


def test_a_richer_hri_msgs_vocabulary_is_accepted():
    """hri_msgs 2.1.0 declares labels EMORI never publishes; that is not a failure."""

    wide = _VOCABULARY + ["pleading", "vulnerable", "despaired", "guilty"]
    assert checker.evaluate(_report(expressions={"pleading": 2}), "learner_01", wide) == []


def test_every_problem_is_reported_in_one_pass():
    """Each retry costs a container start and 30 s; one run must list everything."""

    broken = _report(tracked_ids=["someone_else"], engagement_counts={},
                     invalid_levels=1, expressions={"smug": 1},
                     unknown_expression_labels=["smug"])
    assert len(checker.evaluate(broken, "learner_01", _VOCABULARY)) == 5


@pytest.mark.parametrize("report", [
    {}, {"tracked_ids": ["learner_01"]}, "not a report",
])
def test_a_missing_or_malformed_report_fails_instead_of_raising(report):
    """The container can die mid-write; that must read as FAIL, not as a traceback."""

    failures = checker.evaluate(report, "learner_01", _VOCABULARY)
    assert failures and all(isinstance(line, str) for line in failures)


# --- 3. the privacy half, read where the message can be decoded -----------------------

_ECHO_PSEUDONYM = """header:
  stamp:
    sec: 1758000000
    nanosec: 0
  frame_id: camera
person_id: p-6f1c2a9b4d0e5f73
session_id: p-11aa22bb33cc44dd
seq: 7
"""

_ECHO_REAL = _ECHO_PSEUDONYM.replace("p-6f1c2a9b4d0e5f73", "student-4711")


def test_a_pseudonymised_context_passes_when_the_knob_is_on():
    assert checker.evaluate_context_echo(_ECHO_PSEUDONYM, deidentify=True) == []


def test_a_real_id_on_dds_with_the_knob_on_is_the_failure_this_check_exists_for():
    failures = checker.evaluate_context_echo(_ECHO_REAL, deidentify=True)
    assert len(failures) == 1 and "student-4711" in failures[0]


def test_the_opt_out_is_checked_in_the_other_direction_too():
    """A deployment that turned de-identification off must actually get real ids."""

    assert checker.evaluate_context_echo(_ECHO_REAL, deidentify=False) == []
    failures = checker.evaluate_context_echo(_ECHO_PSEUDONYM, deidentify=False)
    assert len(failures) == 1 and "not publishing what this deployment asked for" in failures[0]


def test_an_empty_echo_is_a_failure_not_a_pass():
    """`ros2 topic echo --once` timing out must never read as "the ids were fine"."""

    failures = checker.evaluate_context_echo("", deidentify=True)
    assert len(failures) == 1 and "person_id" in failures[0]


def test_the_checker_itself_needs_nothing_installed():
    """It is meant to be runnable from a bare checkout by someone verifying our claim."""

    allowed = set(sys.stdlib_module_names)
    outside = sorted(_imported_roots(_CHECKER) - allowed)
    assert outside == [], f"the checker grew a dependency: {outside}"
