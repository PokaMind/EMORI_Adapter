"""The `emori_msgs` interface contract, checked as TEXT so it runs without ROS.

Nothing in `tests/` has ever imported `ros2_ws`, and nothing can: ROS 2 does not
install on the dev machines or on the robot's CM4 itself (the graph runs in a container).
That is precisely why the ROS side is where a cross-file break survives a green suite.

The break this file exists to catch: a node such as
`hri_engagement_behaviors/behavior_node.py` rewritten to import ``SpeechCue``,
``SetExpression``, ``LookAt`` and ``ReplayMotion`` from ``emori_msgs`` while the
package on disk still declares only ``Context`` -- an ImportError at node start on the
robot, invisible to a fully passing Python suite.  Two rules close it, and both are
pure text:

1. every ``emori_msgs`` symbol any ROS node imports exists as a file on disk;
2. every interface file on disk is listed in ``CMakeLists.txt`` -- the classic
   "added a msg, forgot the build" bug, which fails the same way (the generated
   package simply does not contain the type).

Plus the append-only rule for ``Context``: the v1 field ORDER is a golden list, because
every existing subscriber is compiled against it and reordering is a silent wire break.
"""

from __future__ import annotations

import ast
import re
import sys
import textwrap
from pathlib import Path

import pytest

_ROS_WS = Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
# The workspace has two halves: `open/` (standalone packages) and `emori/` (packages that
# use the adapter core). colcon recurses into both, and so do the `rglob` scans below.
_OPEN = _ROS_WS / "open"
_EMORI = _ROS_WS / "emori"
_MSGS = _OPEN / "emori_msgs"


def _package(name: str) -> Path:
    """`name`'s directory in this workspace, whichever half it lives in.

    A name in neither half (an apt package such as `v4l2_camera`) comes back as a path
    that does not exist, which is how the launch check tells ours from somebody else's.
    """

    for half in (_OPEN, _EMORI):
        if (half / name).is_dir():
            return half / name
    return _ROS_WS / name


def _field_names(path: Path) -> list[str]:
    """The declared FIELD names of a .msg body, in file order (comments dropped).

    Constants are not fields and must not be counted as ones. rosidl spells a constant
    `float32 NOT_REPORTED=-1.0`, which occupies the same two columns a field does but
    takes no space on the wire - so the moment an interface starts documenting its own
    contract (`Context.msg` now declares NOT_REPORTED and the REPORT_* bits), a helper
    that strips the `=...` and keeps the name turns every constant into a phantom field
    and fails the append-only golden lists below for a change that appended nothing.
    The second token carrying an `=` is the whole difference, and it is unambiguous:
    a field name may not contain one.
    """

    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line == "---":
            continue
        parts = line.split()
        if len(parts) >= 2 and "=" not in parts[1]:
            names.append(parts[1])
    return names


def _action_sections(path: Path) -> list[list[str]]:
    """A .action's goal / result / feedback field names, split on the `---` rules.

    Constants are skipped for the same reason as in `_field_names`: an action that grows
    an error-code constant block has not grown a goal field.
    """

    sections: list[list[str]] = [[]]
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped == "---":
            sections.append([])
            continue
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 2 and "=" not in parts[1]:
            sections[-1].append(parts[1])
    return sections


# --- 1. the imports a node makes are satisfied by files that exist --------------

def test_every_emori_msgs_symbol_a_ros_node_imports_exists_on_disk():
    """An interface a node imports but nobody declared is an ImportError on the robot.

    This is the whole reason the file exists: the failure mode is a node that dies at
    startup inside a container, on a robot, in front of a person -- and a suite that
    stayed green the entire time because no test may import ROS.
    """

    declared = {p.stem for p in (_MSGS / "msg").glob("*.msg")}
    declared |= {p.stem for p in (_MSGS / "action").glob("*.action")}

    pattern = re.compile(r"^from\s+emori_msgs\.(?:msg|action)\s+import\s+(.+)$", re.M)
    imported: dict[str, str] = {}
    for source in _ROS_WS.rglob("*.py"):
        for match in pattern.finditer(source.read_text(encoding="utf-8")):
            for name in match.group(1).split(","):
                name = name.strip().split(" as ", 1)[0].strip()
                if name:
                    imported.setdefault(name, str(source.relative_to(_ROS_WS)))

    assert imported, "no ROS node imports emori_msgs at all -- has the graph moved?"
    missing = sorted(f"{name} (imported by {where})"
                     for name, where in imported.items() if name not in declared)
    assert missing == [], "imported from emori_msgs but not declared on disk: %s" % missing


# --- 2. the build lists every interface that exists -----------------------------

def test_cmakelists_generates_every_interface_file_on_disk():
    """A .msg nobody listed is generated into nothing, and fails like a missing file."""

    cmake = (_MSGS / "CMakeLists.txt").read_text(encoding="utf-8")
    on_disk = sorted(
        [f"msg/{p.name}" for p in (_MSGS / "msg").glob("*.msg")]
        + [f"action/{p.name}" for p in (_MSGS / "action").glob("*.action")]
    )
    assert on_disk, "emori_msgs declares no interfaces at all"
    unlisted = [entry for entry in on_disk if f'"{entry}"' not in cmake]
    assert unlisted == [], "on disk but absent from rosidl_generate_interfaces: %s" % unlisted


def test_the_build_declares_the_dependencies_the_new_interfaces_need():
    """`builtin_interfaces` for SpeechCue.source_time, `action_msgs` for the actions.

    rosidl fails the colcon build with a bare "unknown type" if either is missing, and
    the robot image is built off the robot -- so this is the cheapest place to notice.
    """

    cmake = (_MSGS / "CMakeLists.txt").read_text(encoding="utf-8")
    package = (_MSGS / "package.xml").read_text(encoding="utf-8")

    for dependency in ("std_msgs", "builtin_interfaces", "action_msgs"):
        assert f"find_package({dependency} REQUIRED)" in cmake, dependency
        assert f"<depend>{dependency}</depend>" in package, dependency

    generate = cmake.split("rosidl_generate_interfaces", 1)[1]
    dependencies = generate.split("DEPENDENCIES", 1)[1]
    for dependency in ("std_msgs", "builtin_interfaces", "action_msgs"):
        assert dependency in dependencies, f"{dependency} missing from DEPENDENCIES"


# --- 3. Context is append-only --------------------------------------------------

# The v1 wire, in order.  Every field a compiled subscriber already binds; appending
# is safe, reordering or renaming is not.  A deliberate v2 append extends this list at
# the END and nowhere else.
_CONTEXT_V1_FIELDS = [
    "header",
    "person_id",
    "session_id",
    "seq",
    "engagement_score",
    "engagement_level",
    "valence",
    "arousal",
    "expression",
    "expression_confidence",
    "engagement_trend",
    "escalation",
    "rapport",
    "attention",
    "dominant_style",
    "topic",
    "note",
]


# The v2 append: the OBSERVATION half of the message, in the order it
# was appended.  Pinned for the same reason as v1 -- the moment anything subscribes to
# it, its order is a wire contract too.
_CONTEXT_V2_FIELDS = [
    "posture",
    "head_position",
    "gesture_type",
    "overall_energy",
    "body_engagement",
    "body_confidence",
    "face_positivity",
    "voice_warmth",
    "pitch_variety",
    "style_names",
    "style_values",
    "report_flags",
    "lang",
    "age_s",
]


def test_context_v1_fields_are_unchanged_and_unreordered():
    fields = _field_names(_MSGS / "msg" / "Context.msg")
    assert fields[: len(_CONTEXT_V1_FIELDS)] == _CONTEXT_V1_FIELDS


def test_context_v2_is_appended_after_v1_and_nowhere_else():
    assert _field_names(_MSGS / "msg" / "Context.msg") == (_CONTEXT_V1_FIELDS
                                                           + _CONTEXT_V2_FIELDS)


def test_the_person_report_floats_are_typed_and_documented_as_sentinel_bearing():
    """Every optional float must be float32 AND must say -1.0 means "not reported".

    The type matters because the sentinel has to survive the wire; the COMMENT matters
    because the next person to read this message will otherwise plot -1.0 as a score.
    That mistake is cheaper to prevent in a .msg comment than to find in a dashboard.
    """

    text = (_MSGS / "msg" / "Context.msg").read_text(encoding="utf-8")
    lines = text.splitlines()
    for field in ("body_engagement", "body_confidence", "face_positivity",
                  "voice_warmth", "pitch_variety", "age_s"):
        i = next(n for n, line in enumerate(lines) if re.match(rf"\S+\s+{field}\b", line))
        assert lines[i].startswith("float32"), f"{field} must be float32: {lines[i]}"
        # the sentinel is stated on the field's own line, or on its continuation
        assert "-1.0" in " ".join(lines[i:i + 2]), f"{field} does not document -1.0"

    # The two style arrays are only meaningful indexed against each other.
    assert "string[] style_names" in text
    assert "float32[] style_values" in text
    assert "uint8   report_flags" in text


def test_context_msg_constants_match_the_core_contract():
    """One number, now written down in three places, and all three must agree.

    The sentinel and the channel bits must not exist only in `emori_adapter.translation`:
    a consumer would have to install the adapter core - or guess - to know that
    -1.0 on `face_positivity` is absence and not a miserable learner. They are now
    declared by `Context.msg` itself, so `ros2 interface show emori_msgs/msg/Context` is
    the whole contract; `hri_context_analyzer.contracts` mirrors them because that module
    must import with no ROS at all; and the publisher still takes them from the core it
    already depends on.

    Three copies is two more than anybody wants, and each one exists for a reason that
    survives review - so the drift they invite is closed here instead. A renumbered bit
    would not raise anywhere: it would gate the wrong channel, forever, silently.
    """

    import importlib.util
    import sys

    from emori_adapter.translation import (
        EXPRESSION_CONSTANT_NAMES,
        NOT_REPORTED,
        PersonReportFlags,
    )

    constants: dict[str, str] = {}
    for line in (_MSGS / "msg" / "Context.msg").read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].strip().split()
        if len(parts) >= 2 and "=" in parts[1]:
            name, _, value = parts[1].partition("=")
            constants[name] = value

    assert float(constants["NOT_REPORTED"]) == NOT_REPORTED == -1.0

    declared = {name: int(value) for name, value in constants.items()
                if name.startswith("REPORT_")}
    assert declared == {f"REPORT_{flag.name}": int(flag) for flag in PersonReportFlags}

    # ...and the analyzer's own mirror, which is what the node actually gates on. Loaded
    # by PATH: `ros2_ws` is not on `sys.path`, and putting it there would need ROS.
    pure = _OPEN / "hri_context_analyzer" / "hri_context_analyzer" / "contracts.py"
    spec = importlib.util.spec_from_file_location("hri_context_analyzer_contracts", pure)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert {f"REPORT_{flag.name}": int(flag) for flag in module.ReportFlags} == declared
    # The label table is duplicated for the same reason and drifts the same way: a
    # release that renames one label publishes six expressions and drops the seventh.
    assert module.EXPRESSION_CONSTANT_NAMES == EXPRESSION_CONSTANT_NAMES


# --- 4. the new interfaces carry the fields their consumers read ----------------

@pytest.mark.parametrize(
    "filename, required",
    [
        # what behavior_node's _on_speech_cue reads
        ("SpeechCue.msg", ["header", "person_id", "session_id", "seq", "text",
                           "cue_category", "cue_body", "lang", "source",
                           "est_duration", "source_time", "ended",
                           "cue_category_authored"]),
        # what behavior_node's _publish_expression writes
        ("SetExpression.msg", ["header", "person_id", "label", "confidence",
                               "intensity", "reason"]),
    ],
)
def test_new_messages_carry_every_field_the_nodes_use(filename, required):
    assert _field_names(_MSGS / "msg" / filename) == required


def test_speech_cue_carries_the_correlation_stamp_as_a_real_time_type():
    """`source_time` is the ENVELOPE's clock, not the publisher's.

    A float would lose the correlation this field exists for; the whole point is that a
    ROS consumer can line a cue up against the Pokamind timestamp that produced it.
    """

    text = (_MSGS / "msg" / "SpeechCue.msg").read_text(encoding="utf-8")
    assert "builtin_interfaces/Time source_time" in text
    assert "std_msgs/Header header" in text


@pytest.mark.parametrize(
    "filename, goal, result, feedback",
    [
        ("LookAt.action", ["yaw_deg", "pitch_deg", "duration", "reason"],
         ["reached", "error_code"], ["progress"]),
        ("ReplayMotion.action", ["name", "intensity", "reason"],
         ["completed", "error_code"], ["progress"]),
    ],
)
def test_actions_carry_the_designed_goal_result_and_feedback(filename, goal, result, feedback):
    sections = _action_sections(_MSGS / "action" / filename)
    assert len(sections) == 3, f"{filename} must have exactly two `---` rules"
    assert sections == [goal, result, feedback]


# --- 5. the skill vocabulary the node clamps to is the one the hostapp accepts ---

def test_the_nodes_replay_vocabulary_matches_the_hostapp_skill_contract():
    """Two files, one closed vocabulary: a drift here is a silent 400 on every gesture.

    `policy.SKILL_MOTIONS` cannot import the adapter core (the package installs
    without it, which is its point), so the list is duplicated by design
    -- and a duplicated constant is a constant that drifts unless something asserts it
    does not.

    Read as TEXT, from `policy.py`, where the decision now lives: the node above it is
    plumbing and holds no vocabulary of its own.  `tests/test_engagement_policy.py`
    asserts the same equality on the loaded module; this one survives even if importing
    the module ever stops being possible here.
    """

    from emori_adapter.behavior.cues import EXPRESSION_LABELS, REPLAY_MOTIONS

    node = (_OPEN / "hri_engagement_behaviors" / "hri_engagement_behaviors"
            / "policy.py").read_text(encoding="utf-8")

    block = node.split("SKILL_MOTIONS = (", 1)[1].split(")", 1)[0]
    assert sorted(re.findall(r'"([a-z_]+)"', block)) == sorted(REPLAY_MOTIONS)

    labels_block = node.split("EXPRESSION_LABELS = (", 1)[1].split(")", 1)[0]
    assert sorted(re.findall(r'"([a-z_]+)"', labels_block)) == sorted(EXPRESSION_LABELS)

    # Every label the policy can publish must be one the hostapp's set_expression accepts.
    expressions = node.split("def cue_expression", 1)[1]
    labels = set(re.findall(r'"([a-z_]+)"', expressions.split("}.get(", 1)[0]))
    assert labels - set(EXPRESSION_LABELS) <= {"tension", "levity", "energy",
                                               "intimacy", "softening", "release"}


# --- 6. the graph is CONNECTED: every client has a server, every sub has a pub ---
#
# The symmetric half of rule 1: the interfaces can exist, the imports resolve and every
# test pass while `skill_facade`, the node holding the only ActionServers in the graph,
# is missing. Every skill request the engagement node computes then hits
# `server_is_ready() is False`, counts itself as "dropped" and vanishes. Same shape on
# the other side: a `speech_cue` subscriber with no publisher makes the whole
# per-sentence lane dead code.
#
# Text-level, like everything here, because no test may import ROS.


def _render(node, env: dict) -> str:
    """Render a topic/action-name expression into a comparable template.

    An f-string placeholder becomes ``*``: ``f"{base}/context"`` where
    ``base = f"/humans/persons/{self.person_id}"`` is the same topic as
    ``f"/humans/persons/{pid}/context"``, and the person id is a runtime parameter that
    both sides read from the same launch file.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, "*")
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(part, env) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _render(node.value, env)
    return "*"


def _graph_edges() -> dict[str, set[tuple[str, str]]]:
    """(type, name) pairs per role, over every node source in the workspace."""

    edges: dict[str, set[tuple[str, str]]] = {
        "create_subscription": set(), "create_publisher": set(),
        "ActionClient": set(), "ActionServer": set(),
    }
    for source in _ROS_WS.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        env: dict[str, str] = {}
        # `from hri_msgs.msg import EngagementLevel as EngagementLevelMsg` must not read
        # as a different message type from the publisher's `EngagementLevel`.
        alias = {a.asname: a.name for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) for a in node.names if a.asname}
        for node in ast.walk(tree):
            # Track simple local string/f-string names so `base` resolves.
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
                rendered = _render(node.value, env)
                if rendered:
                    env[node.targets[0].id] = rendered
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            role = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if role not in edges:
                continue
            args = node.args
            # create_publisher(Type, name, ...) / ActionClient(self, Type, name)
            offset = 1 if role in ("ActionClient", "ActionServer") else 0
            if len(args) < offset + 2:
                continue
            kind = args[offset]
            if not isinstance(kind, ast.Name):
                continue
            edges[role].add((alias.get(kind.id, kind.id), _render(args[offset + 1], env)))
    return edges


def test_every_action_client_in_the_graph_has_a_server():
    """A client with no server does not fail -- it silently drops, which is worse."""

    edges = _graph_edges()
    assert edges["ActionClient"], "no ActionClient in the workspace -- has the graph moved?"
    orphans = sorted(edges["ActionClient"] - edges["ActionServer"])
    assert orphans == [], "action clients with no server anywhere in ros2_ws: %s" % orphans


def test_every_subscribed_topic_in_the_graph_has_a_publisher():
    """The mirror image: a subscriber whose callback can never fire is dead code."""

    edges = _graph_edges()
    assert edges["create_subscription"], "no subscriptions at all -- has the graph moved?"
    orphans = sorted(edges["create_subscription"] - edges["create_publisher"])
    assert orphans == [], "subscribed but published by nobody in ros2_ws: %s" % orphans


def test_the_node_is_told_which_device_to_capture_from():
    """`camera.enabled` says THIS node publishes the camera; `camera.device` says what
    from. On a robot backend the first without the second is a node that logs "nothing to
    capture from" and publishes an empty topic, which is what a robot does when the
    device is not wired through.

    The device is deliberately never guessed: a node that opens a video device nobody
    named is a node that can take the camera from whatever else is reading it.
    """

    launch_file = _OPEN / "reachy_mini_ros2" / "launch" / "robot.launch.py"
    if not launch_file.is_file():  # pragma: no cover - package missing
        pytest.skip("reachy_mini_ros2 is not on disk yet")
    source = launch_file.read_text(encoding="utf-8")
    assert '"camera.device": video_device' in source

    node = _OPEN / "reachy_mini_ros2" / "reachy_mini_ros2" / "robot_node.py"
    node_source = node.read_text(encoding="utf-8")
    assert 'self.declare_parameter("camera.device", "")' in node_source, "empty = no camera"


def test_the_shim_is_told_which_robot_it_is_pretending_to_be():
    """The shim's one job is to be indistinguishable from Pollen's daemon.

    On a Wireless robot the daemon answers `wireless_version: true`; a shim whose node
    resolves the parameter for `find_serial_port` and then never passes it on answers
    `false`. Nothing in the hostapp reads that field today, which is exactly why it
    would stay wrong.
    """

    node_file = _OPEN / "reachy_mini_ros2" / "reachy_mini_ros2" / "robot_node.py"
    if not node_file.is_file():  # pragma: no cover - package missing
        pytest.skip("reachy_mini_ros2 is not on disk yet")
    tree = ast.parse(node_file.read_text(encoding="utf-8"))

    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "start_rest_shim"]
    assert len(calls) == 1, "one shim, started in one place"
    passed = {kw.arg for kw in calls[0].keywords}
    assert "wireless_version" in passed, sorted(passed)


def test_the_robot_nodes_death_takes_the_whole_launch_down():
    """`reachy_mini` IS the robot. Without `on_exit=Shutdown()` a robot_node that dies -
    an OOM kill, a control loop that faulted, the non-zero exit the node itself takes
    when its backend dies - leaves the rest of the launch running: the analyzer, the
    behaviours and the facade all still publishing, all still deciding what the robot
    should do, with nothing left to do it. The container stays "up", the unit stays
    "active", and the only symptom is a robot that stopped moving.

    `v2.launch.py` INCLUDES this file rather than copying its nodes, so it inherits the
    handler; checked at the source.
    """

    launch_file = _OPEN / "reachy_mini_ros2" / "launch" / "robot.launch.py"
    if not launch_file.is_file():  # pragma: no cover - package missing
        pytest.skip("reachy_mini_ros2 is not on disk yet")
    tree = ast.parse(launch_file.read_text(encoding="utf-8"))

    handlers: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Node"):
            continue
        fields = {kw.arg: kw.value for kw in node.keywords}
        executable = fields.get("executable")
        if not isinstance(executable, ast.Constant):
            continue
        on_exit = fields.get("on_exit")
        handlers[executable.value] = (
            on_exit.func.id if isinstance(on_exit, ast.Call)
            and isinstance(on_exit.func, ast.Name) else "")

    assert handlers.get("robot_node") == "Shutdown", handlers

    # ...and launch must not escalate to SIGKILL before the node has parked the robot.
    # The defaults are 5 s and 5 s; `backend.reset_to_sleep()` alone is bounded at 8 s.
    timeouts = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Node"):
            fields = {kw.arg: kw.value for kw in node.keywords}
            executable = fields.get("executable")
            if isinstance(executable, ast.Constant) and executable.value == "robot_node":
                timeouts = {name: value.value for name, value in fields.items()
                            if name in ("sigterm_timeout", "sigkill_timeout")
                            and isinstance(value, ast.Constant)}
    assert float(timeouts.get("sigterm_timeout", 0)) >= 15.0, timeouts
    assert float(timeouts.get("sigkill_timeout", 0)) >= float(timeouts["sigterm_timeout"])
    # ...and it is imported, or the launch file dies at parse time on the robot.
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "launch.actions"
                for alias in node.names}
    assert "Shutdown" in imported
    # The camera and the audio capturer are NOT the robot: losing either is a degraded
    # graph, not a dead one, and taking the launch down for them would be worse.
    assert handlers.get("v4l2_camera_node", "") == ""
    assert handlers.get("audio_capturer_node", "") == ""


def test_every_launched_executable_exists_as_a_console_script():
    """A launch file naming an executable no setup.py installs dies at `ros2 launch`.

    Includes are followed: v2's `v2.launch.py` brings the robot up by including
    `reachy_mini_ros2/launch/robot.launch.py` rather than by copying its nodes, so the
    file this test can read names none of them. Following the include is what keeps the
    check meaningful for the launch command the v2 image actually runs.

    A launch file whose included package is not on disk is SKIPPED by name rather
    than failed: a checkout without `reachy_mini_ros2` is "not built here", not
    "broken".
    """

    launches = sorted((_EMORI / "emori_adapter_bringup" / "launch").glob("*.py"))
    assert launches, "no launch files found"

    def _nodes(tree):
        """(package, executable) for every `Node(...)` with constant names."""

        found: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "Node"):
                continue
            fields = {kw.arg: kw.value for kw in node.keywords}
            package, executable = fields.get("package"), fields.get("executable")
            if isinstance(package, ast.Constant) and isinstance(executable, ast.Constant):
                found.add((package.value, executable.value))
        return found

    def _includes(tree):
        """(package, launch file) for every PythonLaunchDescriptionSource(...).

        The package comes from the `get_package_share_directory("...")` call inside the
        source expression and the file from the `*.launch.py` constant beside it, which
        is the only shape a ROS 2 include can honestly take: a share directory lookup
        plus a path join.
        """

        found: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "PythonLaunchDescriptionSource"):
                continue
            packages, files = [], []
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == "get_package_share_directory"
                        and inner.args and isinstance(inner.args[0], ast.Constant)):
                    packages.append(inner.args[0].value)
                elif isinstance(inner, ast.Constant) and str(
                        inner.value).endswith(".launch.py"):
                    files.append(inner.value)
            for package in packages:
                for name in files:
                    found.add((package, name))
        return found

    launched: set[tuple[str, str]] = set()
    skipped: list[str] = []
    for launch in launches:
        tree = ast.parse(launch.read_text(encoding="utf-8"))
        included = _includes(tree)
        unbuilt = [f"{package}/launch/{name}" for package, name in sorted(included)
                   if not (_package(package) / "launch" / name).is_file()]
        if unbuilt:
            skipped.append(f"{launch.name} (includes {', '.join(unbuilt)}, not on disk)")
            continue
        launched |= _nodes(tree)
        for package, name in sorted(included):
            launched |= _nodes(ast.parse(
                (_package(package) / "launch" / name).read_text(encoding="utf-8")))

    if not launched and skipped:
        pytest.skip("every launch file waits on a package that is not written yet: "
                    + "; ".join(skipped))
    assert launched, "the launch file starts no nodes"
    missing = []
    for package, executable in sorted(launched):
        directory = _package(package)
        if not directory.is_dir():
            # Not ours: `v4l2_camera` and `audio_common` are apt packages pulled in by
            # rosdep, and a text check inside this repository can say nothing true about
            # somebody else's console scripts. The workspace packages below are the ones
            # a refactor here can actually break.
            continue
        setup = directory / "setup.py"
        if not setup.exists() or f"{executable} = " not in setup.read_text(encoding="utf-8"):
            missing.append(f"{package}/{executable}")
    assert missing == [], "launched but not in the package's console_scripts: %s" % missing

    # ...and the workspace's own nodes really were covered, so a renamed package cannot
    # make this test vacuous by looking external.
    assert ("emori_skill_facade", "skill_facade") in launched


def test_the_only_component_that_may_move_the_robot_posts_the_hostapp():
    """The single-writer rule, asserted where it can actually break.

    skill_facade is the one ROS component allowed to turn a decision into motion, and
    the whole point is that it POSTs /api/skill rather than the Reachy daemon: a daemon
    POST would bypass RobotPlayer's generation guard, its drop_if_busy interlock, the
    `busy` gate push-turn needs, and the add-on ownership check, all at once.

    v2 changes who the writer is, not the rule. `reachy_mini_ros2` IS the daemon
    endpoint now - its REST compatibility shim SERVES the daemon's own routes so the
    hostapp's `HttpRobotClient` keeps working unchanged against a robot that is a ROS
    node - and `reachy_mini_msgs` names those routes to say which daemon call each
    interface replaces. Both are therefore exempt from the string check. The invariant
    was never "this text may not appear in the workspace"; it is: **no EMORI node may
    CALL the daemon**. Every other package is an EMORI node, `emori_skill_native`
    included - its whole point is that it reaches the robot through the arbiter's
    actions rather than over the daemon's HTTP.
    """

    package = _OPEN / "emori_skill_facade" / "emori_skill_facade"

    # The wire lives in the transport, which is the integration plug point: whoever adds a
    # second transport must not be able to quietly move the hostapp hop out of the file
    # this assertion reads.
    transport = (package / "transport.py").read_text(encoding="utf-8")
    assert "/api/skill" in transport
    assert "X-Emori-Skill-Key" in transport

    # The node must be the enforcement point for the owner knob, not merely aware of it
    # -- and the gate stays in the node rather than in a transport, so it holds for
    # every transport that is ever plugged in, including ones not written yet.
    node = (package / "facade_node.py").read_text(encoding="utf-8")
    assert "EMORI_ROS_MOTION_OWNER" in node
    assert 'self.motion_owner != "ros"' in node

    # The two packages that ARE the robot, and so are allowed to speak the dialect.
    serves_the_dialect = {"reachy_mini_ros2", "reachy_mini_msgs"}
    checked: set[str] = set()
    for source in _ROS_WS.rglob("*.py"):
        package = source.relative_to(_ROS_WS).parts[1]  # parts[0] is open/ or emori/
        if package in serves_the_dialect:
            continue
        checked.add(package)
        text = source.read_text(encoding="utf-8")
        assert "/api/move/" not in text, f"{source.name} calls the daemon directly"

    # ...and the exemption must not quietly become the whole workspace: the EMORI nodes
    # are the ones this rule is about, so their being checked is part of the assertion.
    assert {"emori_skill_facade", "emori_skill_native", "hri_context_analyzer",
            "hri_engagement_behaviors", "emori_adapter_ros", "emori_adapter_bringup",
            "fiware_bridge"} <= checked, sorted(checked)


def test_the_node_no_longer_holds_a_daemon_client():
    """Single-writer rule: the engagement node emits skill requests, never motor code.

    `HttpRobotClient` POSTs the daemon directly, bypassing RobotPlayer's generation
    guard, the `busy` gesture interlock and the add-on ownership check all at once.
    """

    # Parsed, not grepped: the module docstring explains at length WHY the client is
    # gone, and prose about a symbol is not a use of it.  `ast` needs no ROS installed.
    # Both halves of the package, because the decision moved out of the node into
    # `policy.py` and the rule has to move with it.
    package = _OPEN / "hri_engagement_behaviors" / "hri_engagement_behaviors"
    for source in (package / "behavior_node.py", package / "policy.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names |= {alias.name for alias in node.names}
                assert "robot.client" not in (node.module or ""), source.name
        assert "HttpRobotClient" not in names, source.name


# --- 7. the context fan-out: every channel a topic, and no channel a fabricated zero -
#
# `hri_context_analyzer` is where the person report stops being an EMORI-private message
# and becomes something rqt_plot, the FIWARE bridge and a trainer dashboard can read
# without knowing our types.  Two things can go wrong there and neither raises:
#
#   * a documented topic is simply never created, so a consumer subscribes to a
#     name that will never publish and waits forever;
#   * a flag-gated channel is published UNCONDITIONALLY, which turns "Pokamind never
#     reported a face this session" into an unbroken line of 0.0 (or of -1.0, which is
#     worse: it looks like data).  Live, the facial/tone/verbal channels never arrive,
#     so this is not a corner case -- it is every single session.
#
# Both are checked structurally, so a rename moves the assertion with the code.

_ANALYZER = (_OPEN / "hri_context_analyzer" / "hri_context_analyzer"
             / "context_analyzer_node.py")
_PUBLISHER = _EMORI / "emori_adapter_ros" / "emori_adapter_ros" / "publisher_node.py"

_CONTEXT = "/humans/persons/*/context"
_PERSON = "/humans/persons/*"

# The fan-out topic set, verbatim.  `*` is the person-id parameter both sides read from the
# same launch file (see `_render`).
_FANOUT_TOPICS = {
    # v1, unchanged
    f"{_CONTEXT}/escalation", f"{_CONTEXT}/rapport", f"{_CONTEXT}/attention",
    f"{_CONTEXT}/engagement_trend", f"{_CONTEXT}/dominant_style",
    # v2 derived scalars
    f"{_CONTEXT}/engagement_score", f"{_CONTEXT}/valence", f"{_CONTEXT}/arousal",
    f"{_CONTEXT}/age_s", f"{_CONTEXT}/degraded",
    # v2 person report, one topic per observed channel
    f"{_CONTEXT}/body/posture", f"{_CONTEXT}/body/head_position",
    f"{_CONTEXT}/body/gesture_type", f"{_CONTEXT}/body/energy",
    f"{_CONTEXT}/body/engagement", f"{_CONTEXT}/body/confidence",
    f"{_CONTEXT}/face/positivity",
    f"{_CONTEXT}/voice/warmth", f"{_CONTEXT}/voice/pitch_variety",
    f"{_CONTEXT}/top_styles",
    # the REP-155 standard person topic -- NOT under /context
    f"{_PERSON}/expression",
}

# Published every cycle because they are always computed: withholding them would make a
# healthy node indistinguishable from a dead one.  `degraded` belongs here on purpose --
# a health signal that goes quiet when things degrade is not a health signal.
_ALWAYS_PUBLISHED = {
    f"{_CONTEXT}/escalation", f"{_CONTEXT}/rapport", f"{_CONTEXT}/attention",
    f"{_CONTEXT}/engagement_trend", f"{_CONTEXT}/dominant_style",
    f"{_CONTEXT}/engagement_score", f"{_CONTEXT}/valence", f"{_CONTEXT}/arousal",
    f"{_CONTEXT}/degraded",
}


def _analyzer_publishers() -> dict[str, str]:
    """topic name -> the attribute holding its publisher, for the analyzer node."""

    tree = ast.parse(_ANALYZER.read_text(encoding="utf-8"))
    env: dict[str, str] = {}
    topics: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and isinstance(value, (ast.Constant, ast.JoinedStr)):
            env[target.id] = _render(value, env)
        elif (isinstance(target, ast.Attribute) and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "create_publisher" and len(value.args) >= 2):
            topics[_render(value.args[1], env)] = target.attr
    return topics


def _statement_level_publishes(body, attribute_topic, methods, seen) -> set[str]:
    """Topics published from `body` at statement level, FOLLOWING `self.<helper>()`.

    Not following them was a hole big enough to drive the bug through: `.publish(...)`
    only has to move one level down into a `self._publish_x(msg)` helper to become
    invisible to a check whose whole job is to notice it.  The guard would then be
    defeated by a refactor rather than by intent -- the exact way this kind of check
    dies, and the reason `pub_expression` was published every cycle while a test named
    "no flag-gated channel is published unconditionally" stayed green.

    A helper that takes its publisher as an ARGUMENT (`_publish_reported`) is not
    resolved and does not need to be: it publishes `publisher.publish(...)`, whose
    attribute is a local name no topic maps to, and every one of its call sites is
    inside a gate it evaluates itself.
    """

    published: set[str] = set()
    for statement in body:
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
            continue
        func = statement.value.func
        if not isinstance(func, ast.Attribute):
            continue
        if (func.attr == "publish" and isinstance(func.value, ast.Attribute)):
            published.add(attribute_topic.get(func.value.attr, func.value.attr))
        elif (isinstance(func.value, ast.Name) and func.value.id == "self"
                and func.attr in methods and func.attr not in seen):
            published |= _statement_level_publishes(
                methods[func.attr].body, attribute_topic, methods, seen | {func.attr})
    return published


def _unconditionally_published() -> set[str]:
    """The topics reached by a `.publish(...)` at statement level in `_on_context`.

    Anything inside an `if` is gated by construction and does not appear here.  A helper
    IS followed (see above), so gating must be visible at the call site or in a branch --
    an early `return` buried in a helper does not count, on purpose: this is the one
    check standing between a cleared flag and an unbroken line of confident zeroes.
    """

    tree = ast.parse(_ANALYZER.read_text(encoding="utf-8"))
    attribute_topic = {attr: topic for topic, attr in _analyzer_publishers().items()}
    node_class = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == "ContextAnalyzerNode")
    methods = {n.name: n for n in node_class.body if isinstance(n, ast.FunctionDef)}
    return _statement_level_publishes(methods["_on_context"].body, attribute_topic,
                                      methods, frozenset({"_on_context"}))


def test_the_analyzer_publishes_every_documented_fan_out_topic():
    assert set(_analyzer_publishers()) == _FANOUT_TOPICS


def test_the_fan_out_topics_join_the_same_graph_the_symmetry_checks_walk():
    """The new publishers must be visible to `_graph_edges`, not only to this test.

    They are publish-only today, so `test_every_subscribed_topic_...` has nothing to say
    about them yet; the moment a node subscribes to `.../context/degraded`, that test has
    to find the publisher rather than report an orphan.  It only can if the topic name
    resolves the same way from both sides -- an unrendered `*` in the middle of a name is
    a different topic as far as that comparison is concerned.
    """

    edges = _graph_edges()
    names = {name for _, name in edges["create_publisher"]}
    assert _FANOUT_TOPICS <= names

    # The person id is the only wildcard any of these may contain.
    for topic in _FANOUT_TOPICS:
        assert "*" not in topic.split("/humans/persons/*", 1)[1], topic

    # ...and the fan-out's own subscription still lands on the bridge's publisher.
    assert ("Context", _CONTEXT) in edges["create_subscription"]
    assert ("Context", _CONTEXT) in edges["create_publisher"]


def test_no_flag_gated_channel_is_published_unconditionally():
    """The flag-gating rule, asserted where it can actually be broken.

    A clear flag means the topic is NOT published.  Emitting 0.0 for a channel that never
    reported is the KPI poisoning this whole message version exists to prevent, and it
    fails silently: the dashboard looks perfectly healthy, it is simply describing a
    person nobody ever measured.
    """

    unconditional = _unconditionally_published()
    assert unconditional == _ALWAYS_PUBLISHED, (
        "published every cycle regardless of what Pokamind reported: %s"
        % sorted(unconditional - _ALWAYS_PUBLISHED))


def test_the_unconditional_check_follows_a_helper_instead_of_being_blinded_by_one():
    """The guard above is only worth its docstring if a helper cannot hide a publish.

    Checked on a synthetic node rather than on the real file, because the real file is
    (correctly) clean: a check that can only be exercised by breaking production code is
    a check nobody ever confirms works.  Move any `self.pub_x.publish(...)` one level
    down into a helper and the naive version of `_unconditionally_published` returns an
    empty set while every session emits a fabricated channel.
    """

    tree = ast.parse(textwrap.dedent("""
        class ContextAnalyzerNode:
            def _on_context(self, msg):
                self._publish_face(msg)

            def _publish_face(self, msg):
                self.pub_face_pos.publish(msg.face_positivity)
    """))
    methods = {n.name: n for n in tree.body[0].body if isinstance(n, ast.FunctionDef)}

    found = _statement_level_publishes(
        methods["_on_context"].body, {"pub_face_pos": f"{_CONTEXT}/face/positivity"},
        methods, frozenset({"_on_context"}))

    assert found == {f"{_CONTEXT}/face/positivity"}


def test_a_degraded_cycle_makes_no_expression_claim_on_the_standard_person_topic():
    """`report_flags == 0` means nothing was observed -- and the expression is DERIVED.

    An empty cycle lands on the origin of the valence/arousal plane, which
    `to_expression` maps to "neutral" at confidence 1.0.  `/humans/persons/<id>/expression`
    is the standard REP-155 topic and carries nowhere to say "degraded", so publishing
    that is a confident claim about a person nobody measured -- on the one topic the
    wider ROS4HRI world actually reads.
    """

    tree = ast.parse(_ANALYZER.read_text(encoding="utf-8"))
    callback = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_on_context")
    guards = [statement for statement in callback.body
              if isinstance(statement, ast.If)
              and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                      and call.func.attr == "_publish_expression"
                      for call in ast.walk(statement))]

    assert guards, "_publish_expression is called without a report_flags guard"
    assert "flags" in {name.id for name in ast.walk(guards[0].test)
                       if isinstance(name, ast.Name)}
    # ...and it is not ALSO reachable unconditionally, which the guard above would miss.
    assert f"{_PERSON}/expression" not in _unconditionally_published()


def test_a_dropped_expression_label_is_named_in_the_journal_rather_than_silent():
    """The one drop with no evidence at all: the topic is advertised in `__init__`.

    `ros2 topic list` shows `/humans/persons/<id>/expression` whether or not a single
    message crosses it, so the runbook's own verification confirms a topic that
    publishes nothing.  The partial case is worse -- six labels publish and `scared`
    never does, which reads downstream as a learner who was never once frightened.
    """

    source = _ANALYZER.read_text(encoding="utf-8")
    # The helper and its reason table moved into the package's pure half so they stay
    # testable with no ROS installed (`tests/test_context_analyzer_pure.py`); the node
    # keeps the half that can only run in a container - the logger and the once-only
    # bookkeeping - and that split is what the two halves of this test check.
    pure = _ANALYZER.with_name("contracts.py").read_text(encoding="utf-8")
    helper = pure.split("def set_expression_field", 1)[1].split("\ndef ", 1)[0]

    # Every exit from the helper names WHY, and none of them is a bare False.
    assert "return False" not in helper
    reasons = set(re.findall(r'return "([a-z_]+)"', helper))
    table = next(node for node in ast.walk(ast.parse(pure))
                 if isinstance(node, ast.Assign)
                 and getattr(node.targets[0], "id", "") == "EXPRESSION_DROP_REASONS")
    assert reasons == {key.value for key in table.value.keys}, (
        "a drop reason with no sentence to log for it")

    # ...and the caller logs it instead of returning in silence, ONCE per (label, reason):
    # the failure is constant, and a warning every 2s cycle is a warning nobody reads.
    warn = source.split("def _warn_expression_drop", 1)[1]
    assert "_warn_expression_drop(" in source.split("def _publish_expression", 1)[1]
    assert "_expression_drops" in warn and ".warn(" in warn

    # ...and which branch the INSTALLED hri_msgs took is recorded at node start, so a
    # silent topic is a line in the journal rather than an afternoon in a container.
    assert "expression_field_shape(Expression())" in source


def test_the_mock_source_withholds_freshness_instead_of_inventing_it():
    """`age_s` is measured against the WALL clock, and the mock stamps a fixed epoch.

    `mock` is the default source in `slice2.launch.py` and `topics.yaml`, so publishing
    `time.time()` minus 2026-06-03 is a demo whose freshness signal grows by a day every
    day -- large, positive, and therefore not the sentinel, so every consumer doing what
    `age_s` is for rejects every cycle while rqt_plot draws a straight climbing line.
    """

    source = _PUBLISHER.read_text(encoding="utf-8")
    body = source.split("def _publish(", 1)[1]
    assignment = next(line for line in body.splitlines() if "ctx.age_s" in line)

    assert "_age_seconds" in assignment and "source_kind" in assignment, (
        "age_s is claimed for every source, including the fixed-epoch mock arc")
    assert "NOT_REPORTED" in body.split("ctx.age_s", 1)[1].split("\n\n", 1)[0]


def test_the_expression_goes_on_the_person_topic_not_under_context():
    """`/humans/persons/<id>/expression` is the name the wider ROS4HRI world reads.

    Publishing it under `/context/` would close the documented gap on paper and leave
    every standard consumer still unable to find it.
    """

    topics = _analyzer_publishers()
    assert f"{_PERSON}/expression" in topics
    assert f"{_CONTEXT}/expression" not in topics
    assert ("Expression", f"{_PERSON}/expression") in _graph_edges()["create_publisher"]


def test_the_expression_field_is_introspected_rather_than_assumed():
    """hri_msgs declares `Expression.expression` as a string in some releases and as a
    uint8 with class constants in others.  Assuming either one is a node that dies on the
    first non-neutral cycle, inside a container, on a robot, in front of a person.
    """

    source = _ANALYZER.read_text(encoding="utf-8")
    pure = _ANALYZER.with_name("contracts.py").read_text(encoding="utf-8")
    helper = pure.split("def set_expression_field", 1)[1].split("\ndef ", 1)[0]

    assert "isinstance" in helper, "no string-release branch"
    assert "EXPRESSION_CONSTANT_NAMES" in helper, "no uint8-release branch"
    # The label table lives in the package's OWN pure half, not in the adapter core: the
    # analyzer ships in the standalone image, which installs no `emori_adapter` at all,
    # so an import of it there is an ImportError at node start. (That the package imports
    # no core at all is `tests/test_ros_standalone.py`; here it is only where the table
    # comes from.)
    assert "from .contracts import" in source
    # ...and the wire contract itself comes off the generated message class, so the node
    # and `Context.msg` cannot drift apart about what "not reported" means or which bit
    # gates which channel.
    assert "Context.NOT_REPORTED" in source
    assert "Context.REPORT_BODY" in source
    # NEUTRAL is 0 in every numeric release: `getattr(...) or getattr(...)` would discard
    # exactly the label the robot shows most often.
    assert re.search(r"getattr\([^\n]*\)\s+or\s+getattr", helper) is None
    assert "is None" in helper
    # ...and the helper is used by the node, not merely defined and admired.
    assert "set_expression_field(" in source


def test_both_ros_nodes_take_the_sentinel_from_the_core_they_share():
    """Two files deciding independently what "not reported" means is one file too many.

    `publisher_node` writes the sentinel and the analyzer withholds on it. They no longer
    share a Python import - the analyzer has to build in the standalone image, where no
    `emori_adapter` is installed - so they share the INTERFACE instead: the publisher
    takes `NOT_REPORTED` from the core it already depends on, the analyzer reads
    `Context.NOT_REPORTED` off the generated message class, and
    `test_context_msg_constants_match_the_core_contract` pins the two to one number.
    Neither file spells it; if either did, changing the other would quietly start
    publishing one node's sentinel as though it were the other's measurement.
    """

    publisher = _PUBLISHER.read_text(encoding="utf-8")
    assert "from emori_adapter.translation import" in publisher
    assert "NOT_REPORTED" in publisher

    analyzer = _ANALYZER.read_text(encoding="utf-8")
    assert "Context.NOT_REPORTED" in analyzer
    # Parsed, not grepped: the module docstring explains at length WHY the core import is
    # gone, and prose about a package is not a dependency on it.
    tree = ast.parse(analyzer)
    imported = {node.module or "" for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    assert not [m for m in imported if m.split(".", 1)[0] == "emori_adapter"], (
        "the analyzer is a publishable package: it may not import the core")
    # Not one literal anywhere in it, comments included - the number lives in Context.msg.
    assert "-1.0" not in analyzer, "the analyzer spells the sentinel instead of reading it"

    assert re.search(r"=\s*-1\.0\b", publisher) is None, (
        "publisher_node.py spells the sentinel itself instead of importing it")


# --- 8. the face look-at component ----------------------------------------------
# Everything here is a cross-file break a green host suite cannot see: a subpackage that
# is not installed, an undeclared message dependency, a launch argument that names a
# parameter the node never declared. All three fail identically on the robot -- a node
# that dies inside a container, on a robot, with a traceback nobody is watching -- which
# is the failure this whole file exists for.

_REACHY = _OPEN / "reachy_mini_ros2"
_GAZE = _REACHY / "reachy_mini_ros2" / "gaze"


def _gaze_or_skip() -> Path:
    if not _GAZE.is_dir():  # pragma: no cover - package missing
        pytest.skip("the gaze package is not on disk yet")
    return _GAZE


def test_the_gaze_subpackage_is_installed_and_not_merely_present():
    """An ament_python package that forgets a subpackage installs an importable PARENT
    and raises ImportError at node start on the robot.

    `find_packages` is RUN here rather than trusted: it is the same call `setup.py`
    makes, so this checks what would be installed rather than what is on disk.
    """

    _gaze_or_skip()
    from setuptools import find_packages

    packages = set(find_packages(where=str(_REACHY), exclude=["test"]))
    assert "reachy_mini_ros2" in packages
    assert "reachy_mini_ros2.gaze" in packages, (
        f"gaze/ exists but setup.py would not install it; found {sorted(packages)}")
    assert (_GAZE / "__init__.py").is_file(), "no __init__.py means it is not a package"


def test_the_package_declares_the_hri_messages_the_component_publishes():
    """`hri_msgs` is in the image, but a package that does not declare it is a package
    nobody else can build - and the declaration is the only machine-readable record that
    this node speaks ROS4HRI."""

    _gaze_or_skip()
    manifest = (_REACHY / "package.xml").read_text(encoding="utf-8")
    assert "<exec_depend>hri_msgs</exec_depend>" in manifest

    component = (_GAZE / "component.py").read_text(encoding="utf-8")
    assert "from hri_msgs.msg import" in component


def test_the_component_publishes_the_two_rep_155_topics():
    """`/humans/faces/tracked` and `/humans/faces/<id>/roi` are the standard names. A
    node that invented its own would be invisible to every ROS4HRI consumer, which is
    the entire reason for using them."""

    component = (_gaze_or_skip() / "component.py").read_text(encoding="utf-8")
    assert 'FACES_TOPIC = "/humans/faces/tracked"' in component
    assert 'f"/humans/faces/{self._face_id}/roi"' in component


def test_the_roi_message_shape_is_checked_rather_than_assumed():
    """`hri_context_analyzer/contracts.py` exists because `hri_msgs/Expression` is not
    stable across releases. Same doctrine, once, at construction: a shape surprise must
    degrade ONE topic and warn, never take the robot down."""

    component = (_gaze_or_skip() / "component.py").read_text(encoding="utf-8")
    assert '_ROI_FIELDS = ("xmin", "ymin", "xmax", "ymax", "c")' in component
    assert "get_fields_and_field_types()" in component
    assert "face ROIs will not be published" in component


def test_every_gaze_launch_argument_names_a_parameter_the_node_declares():
    """A launch file that passes `gaze.detect_hz` to a node which never declared it is a
    node that refuses the parameter and dies before it has built anything."""

    _gaze_or_skip()
    launch_source = (_REACHY / "launch" / "robot.launch.py").read_text(encoding="utf-8")
    node_source = (_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(
        encoding="utf-8")

    declared = set(re.findall(r'declare_parameter\(\s*"(gaze\.[a-z_]+)"', node_source))
    passed = set(re.findall(r'"(gaze\.[a-z_]+)":\s*ParameterValue', launch_source))
    assert passed, "the launch file passes no gaze parameters at all"
    assert passed <= declared, (
        f"the launch file passes undeclared {sorted(passed - declared)}")

    # ...and every launch ARGUMENT named gaze_* is actually used rather than declared
    # and quietly dropped, which is the failure that makes a robot knob do nothing.
    arguments = set(re.findall(r'DeclareLaunchArgument\(\s*"(gaze_[a-z_]+)"',
                               launch_source))
    used = set(re.findall(r'LaunchConfiguration\("(gaze_[a-z_]+)"\)', launch_source))
    assert arguments == used, f"declared-but-unused gaze arguments: {arguments ^ used}"


def test_the_gaze_parameters_and_the_shared_dataclass_agree():
    """`GazeParams` is what the pure modules take and what the docs table lists. A
    parameter the node declares but never reads, or a field it never declares, is a knob
    that silently does nothing on the robot."""

    _gaze_or_skip()
    node_source = (_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(
        encoding="utf-8")
    controller = (_GAZE / "controller.py").read_text(encoding="utf-8")

    declared = {name.split(".", 1)[1] for name in
                re.findall(r'declare_parameter\(\s*"(gaze\.[a-z_]+)"', node_source)}
    fields: set[str] = set()
    for node in ast.walk(ast.parse(controller)):
        if isinstance(node, ast.ClassDef) and node.name == "GazeParams":
            fields = {item.target.id for item in node.body
                      if isinstance(item, ast.AnnAssign)}
    assert fields, "GazeParams declares no fields"
    assert declared == fields, (
        f"node-only: {sorted(declared - fields)}; "
        f"dataclass-only: {sorted(fields - declared)}")


def test_the_camera_thread_no_longer_gives_up_for_the_life_of_the_process():
    """`_CAMERA_MISS_LIMIT` must not `break` out of the capture loop permanently after
    one WARN. Any `systemctl restart emori-camera-bridge` - which `Restart=on-failure`
    can do on its own - triggers it, and the symptom is a camera topic that silently
    stops and a face detector reporting an empty room for ever."""

    source = (_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(encoding="utf-8")
    assert "_CAMERA_MISS_LIMIT" in source
    assert "giving up" not in source, "the permanent give-up is back"
    assert "rebuilding the pipeline in" in source, "no backoff/rebuild path"
    assert "def _build_v4l2_pipeline" in source


def test_a_starved_gaze_camera_raises_the_nodes_diagnostic_level():
    """The one camera condition that IS the robot being unwell for this feature: gaze
    enabled with zero frames looks exactly like an empty room from every other angle."""

    source = (_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(encoding="utf-8")
    assert "the gaze camera is not producing frames" in source
    assert "_camera_starved_periods" in source


def test_the_gaze_writer_is_a_sub_client_of_the_ros_graph():
    """`ros:gaze`, not `gaze`: the arbiter reads everything before the colon as the
    expression OWNER, so a client literally named `gaze` is refused `not_owner` for ever
    and the feature never moves the head once."""

    controller = (_gaze_or_skip() / "controller.py").read_text(encoding="utf-8")
    assert 'client: str = "ros:gaze"' in controller
    assert 'lane: str = "expression"' in controller

    arbiter = (_REACHY / "reachy_mini_ros2" / "arbiter.py").read_text(encoding="utf-8")
    assert "def owner_of(" in arbiter
    assert "owner_of(req.client) != self._expression_owner" in arbiter


# --- 8a. the gaze tick's ORDER, which no host test can see ------------------------
# `component.py` is the one gaze module that imports rclpy and hri_msgs, so it cannot be
# imported here (see this file's docstring) and the pure controller underneath it cannot
# see the order its caller does things in. Every defect below was a real one, and all
# four are invisible in review and silent on the robot: a head that lurches once after
# each cue gesture, a counter that says "the head is already right" about an empty room,
# a /diagnostics face id that outlives the person, and a latched topic still advertising
# a face after the detector was switched off.


def _gaze_method(name: str) -> ast.FunctionDef:
    """The named method of `GazeComponent`, parsed rather than grepped.

    Order is the thing under test here, and a substring search cannot see order.
    """

    tree = ast.parse((_gaze_or_skip() / "component.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"GazeComponent has no {name}()")


def _self_calls(node: ast.AST) -> list[tuple[int, str]]:
    """Every `self.<name>(...)` and `self._x.<name>(...)` inside, as (line, name)."""

    found: list[tuple[int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            found.append((child.lineno, child.func.attr))
    return sorted(found)


def _first_line_of(node: ast.AST, call: str) -> int:
    lines = [line for line, name in _self_calls(node) if name == call]
    assert lines, f"no call to {call}() at all"
    return lines[0]


def test_the_move_end_edge_is_detected_before_the_target_is_built():
    """Detecting it AFTER the write is one tick too late, and the tick it is late by is
    the one that lurches: the target was already computed from the stale slew anchor and
    already committed by the time the outcome came back.

    It is also read from the arbiter rather than from our own `busy` refusals, because a
    gaze tick that landed in the deadband never reaches the arbiter at all - so a cue
    gesture that ran entirely between two writes would otherwise never be noticed."""

    run = _gaze_method("_run")
    assert _first_line_of(run, "_note_motion_edge") < _first_line_of(run, "_tick"), (
        "the busy -> idle edge must be detected before limit() builds a target from the "
        "anchor it invalidates")

    edge = _gaze_method("_note_motion_edge")
    names = {name for _line, name in _self_calls(edge)}
    assert "motion_snapshot" in names, "status() copies counters and reads the backend"
    assert "reanchor" in names


def test_every_early_return_in_the_gaze_loop_still_costs_a_full_tick():
    """`_last_detect` gates the loop at `detect_hz`. Advancing it only on the full path
    let the relax-on-disable walk run at the CAMERA rate - 0.5 rad/s where
    `max_step_rad` documents 0.25 - and double-counted `gaze_targets` for that window."""

    run = _gaze_method("_run")
    stamps = [node.lineno for node in ast.walk(run)
              if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == "_last_detect"
                      for t in node.targets)]
    assert len(stamps) == 1, f"_last_detect is assigned {len(stamps)} times in _run"
    assert stamps[0] < _first_line_of(run, "_quiet_tick"), (
        "the disabled branch continues without advancing the detect clock")
    assert stamps[0] < _first_line_of(run, "_tick")


def test_a_tick_with_no_face_bumps_no_deadband_counter():
    """`gaze_deadband` is "ticks that decided the head was already right", and an
    operator reads it against `faces_detected` to tell a still face from an empty room.
    Routing `handle_loss` - which returns None for never-locked, inside-the-hold and
    already-relaxed alike - through the same `target is None` branch made an empty room
    inflate it at `detect_hz` for ever."""

    source = (_gaze_or_skip() / "component.py").read_text(encoding="utf-8")
    assert '"gaze_deadband"' not in source, (
        "the counter must be named by the controller's skip_counter, so that "
        "'the head is already right' and 'nobody can say where the head is' get "
        "separate lines on /diagnostics")

    tick = _gaze_method("_tick")
    losses = [node for node in ast.walk(tick)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "handle_loss"]
    assert len(losses) == 1
    written = [node for node in ast.walk(tick)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "_write"
               and any(isinstance(arg, ast.Call)
                       and isinstance(arg.func, ast.Attribute)
                       and arg.func.attr == "handle_loss" for arg in node.args)]
    assert not written, "_write(handle_loss(...)) counts every face-less tick"


def test_the_face_lock_on_diagnostics_does_not_outlive_the_person():
    """`face_id` is "the current lock, empty when none". An operator triaging a robot
    cannot tell a held lock from a dead one if the last id and the last confidence stay
    on /diagnostics for the rest of the process."""

    publish = _gaze_method("_publish_faces")
    cleared = {target.attr for node in ast.walk(publish) if isinstance(node, ast.Assign)
               for target in node.targets
               if isinstance(target, ast.Attribute)}
    assert "_face_score" in cleared
    assert "_face_id" in cleared, "the id must clear once its TTL has run out"
    source = (_gaze_or_skip() / "component.py").read_text(encoding="utf-8")
    assert "now >= self._face_id_expires_at" in source, (
        "clearing the id before its TTL would break the re-acquisition reuse rule")


def test_switching_gaze_off_empties_the_latched_faces_topic():
    """`/humans/faces/tracked` is TRANSIENT_LOCAL, so the last sample is what a REP-155
    consumer that attaches later is told. `ids=[fXXXX]` left latched while the detector
    is off advertises a face nobody is tracking."""

    flag = _gaze_method("_apply_flag_change")
    empties = [node for node in ast.walk(flag)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "_publish_ids"
               and node.args and isinstance(node.args[0], ast.List)
               and not node.args[0].elts]
    assert empties, "the disarm branch must publish ids=[]"


def test_the_sim_camera_does_not_hand_the_gaze_thread_a_buffer_it_reuses():
    """`mujoco.Renderer.render()` returns its own internal output array and overwrites
    it on the next call, so parking that reference in the frame slot gives the gaze
    thread torn frames and non-deterministic detections. The v4l2 path is already safe:
    `rgb_frame_from_buffer` ends in `.copy()`."""

    source = (_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(encoding="utf-8")
    assert "self._offer_frame_to_gaze(frame.copy())" in source


# --- 9. where a recorded move eases in FROM -----------------------------------------
# The arbiter's ease-in is tested against a start state it is handed; which state the
# NODE hands it is decided in robot_node.py, which imports rclpy and so cannot be
# imported here. It is run instead, lifted out of the source with fakes for the backend.


def _node_function(name: str, **namespace):
    """The named function of robot_node.py (method or module level), made callable."""

    import math
    import os
    from typing import Any

    import numpy as np

    tree = ast.parse((_REACHY / "reachy_mini_ros2" / "robot_node.py").read_text(
        encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            scope = {"np": np, "math": math, "os": os, "Any": Any, **namespace}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "robot_node.py",
                         "exec"), scope)
            return scope[name]
    raise AssertionError(f"robot_node.py has no {name}()")


def test_a_recorded_move_eases_in_from_the_command_not_the_measurement():
    """The measured head yaw can sit 0.12 rad off the commanded one (-0.117 vs
    -0.0001). Easing in from the measurement would make the servo COMMAND jump by
    that much at t = 0 - the very snap the ease-in exists to remove. Measured values
    are only the fallback for a channel the backend has not been commanded on yet."""

    import numpy as np
    from types import SimpleNamespace

    start_state = _node_function("_recorded_start_state")
    commanded = np.eye(4)
    measured = np.eye(4)
    measured[0, 3] = 0.5

    class Host:
        def __init__(self, backend, head):
            self.backend, self._head = backend, head

        def commanded_head_pose(self):
            return self._head

        def present_head_pose(self):
            return measured

        def present_antennas(self):
            return (0.7, -0.7)

        def present_body_yaw(self):
            return -0.117

    backend = SimpleNamespace(target_antenna_joint_positions=np.array([0.1, -0.1]),
                              target_body_yaw=-0.0001)
    head, antennas, body_yaw = start_state(SimpleNamespace(
        backend_host=Host(backend, commanded)))
    assert head is commanded
    np.testing.assert_allclose(antennas, [0.1, -0.1])
    assert body_yaw == pytest.approx(-0.0001)

    # Never commanded yet (a fresh backend), or no backend at all: the measurement.
    for backend in (SimpleNamespace(target_antenna_joint_positions=None,
                                    target_body_yaw=None), None):
        head, antennas, body_yaw = start_state(SimpleNamespace(
            backend_host=Host(backend, None)))
        assert head is measured
        np.testing.assert_allclose(antennas, [0.7, -0.7])
        assert body_yaw == pytest.approx(-0.117)


def test_a_typo_in_a_float_knob_costs_the_knob_and_not_the_node(monkeypatch):
    """EMORI_RECORDED_BLEND_S and EMORI_GAZE_SERVO_HZ are read in `__init__`. Now that
    compose passes them through from deploy/ros2/.env, a bare `float()` would turn one
    mistyped line there into a robot node that never starts."""

    env_float = _node_function("_env_float")
    for raw, want in (("", 0.3), ("0.5", 0.5), (" 0 ", 0.0), ("0,5", 0.3),
                      ("nan", 0.3), ("inf", 0.3)):
        monkeypatch.setenv("EMORI_RECORDED_BLEND_S", raw)
        assert env_float("EMORI_RECORDED_BLEND_S", 0.3) == want, raw
    monkeypatch.delenv("EMORI_RECORDED_BLEND_S")
    assert env_float("EMORI_RECORDED_BLEND_S", 0.3) == 0.3


# --- reachy_mini_msgs: the link interfaces ----------------------------------------

_REACHY_MSGS = _OPEN / "reachy_mini_msgs"


def _interface_files(package: Path) -> list[str]:
    return sorted(f"{kind}/{p.name}" for kind in ("msg", "srv", "action")
                  for p in (package / kind).glob(f"*.{kind}"))


def _interface_body(package: Path, entry: str) -> list[str]:
    """The non-comment lines of an interface file."""

    text = (package / entry).read_text(encoding="utf-8")
    return [line.split("#", 1)[0].strip() for line in text.splitlines()
            if line.split("#", 1)[0].strip()]


def test_reachy_mini_msgs_cmake_lists_every_interface_file_on_disk():
    cmake = (_REACHY_MSGS / "CMakeLists.txt").read_text(encoding="utf-8")
    on_disk = _interface_files(_REACHY_MSGS)
    assert on_disk
    unlisted = [entry for entry in on_disk if f'"{entry}"' not in cmake]
    assert unlisted == [], "on disk but absent from rosidl_generate_interfaces: %s" % unlisted


def test_reachy_mini_msgs_declares_builtin_interfaces():
    cmake = (_REACHY_MSGS / "CMakeLists.txt").read_text(encoding="utf-8")
    package = (_REACHY_MSGS / "package.xml").read_text(encoding="utf-8")
    assert "find_package(builtin_interfaces REQUIRED)" in cmake
    assert "<depend>builtin_interfaces</depend>" in package
    assert "builtin_interfaces" in cmake.split("DEPENDENCIES", 1)[1]


def test_submit_move_carries_supersede_and_the_trace_tag():
    body = _interface_body(_REACHY_MSGS, "srv/SubmitMove.srv")
    request, response = body[:body.index("---")], body[body.index("---") + 1:]
    assert "bool supersede" in request
    assert "string reason" in request
    assert response == ["bool accepted", "string error_code", "string move_id"]


def test_the_trace_service_returns_the_fields_the_arbiter_records(monkeypatch):
    from importlib import util

    spec = util.spec_from_file_location(
        "_arbiter_for_trace_fields",
        _OPEN / "reachy_mini_ros2" / "reachy_mini_ros2" / "arbiter.py")
    module = util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    record = [line.split()[1] for line in _interface_body(
        _REACHY_MSGS, "msg/MoveTraceRecord.msg")]
    assert tuple(record) == module.TRACE_FIELDS
    service = _interface_body(_REACHY_MSGS, "srv/GetMoveTrace.srv")
    assert service == ["int64 since", "---", "MoveTraceRecord[] records"]
