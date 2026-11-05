"""The hub's interfaces in `emori_msgs`, checked as TEXT so this runs without ROS.

`test_ros_contracts.py` pins what the graph's own nodes import. This file pins what the
hub adds beside it: the session, speech, gesture, motion, add-on and gateway interfaces
that a person with `ros2 topic echo` or `ros2 service call` sees. A field that moves is a
silent mis-read in a compiled subscriber, so every one is a golden list here, and the
rules that keep a gate closed (no raw envelope, no speech text by default) are checked
in the interface text itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MSGS = _ROOT / "ros2_ws" / "src" / "open" / "emori_msgs"


def _body(relative: str) -> list[tuple[str, str]]:
    """``(type, name)`` per line of an interface, constants and separators included.

    A constant keeps its ``=value`` on the name, so a test can tell it from a field, and a
    ``---`` rule is a ``("---", "")`` row.
    """

    rows: list[tuple[str, str]] = []
    for line in (_MSGS / relative).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "---":
            rows.append(("---", ""))
            continue
        kind, name = line.split(None, 1)
        rows.append((kind, name.strip()))
    return rows


def _fields(relative: str) -> list[tuple[str, str]]:
    return [(kind, name) for kind, name in _body(relative) if "=" not in name]


def _constants(relative: str) -> list[str]:
    return [name for _kind, name in _body(relative) if "=" in name]


def _spelled(rows: str) -> list[tuple[str, str]]:
    return [tuple(line.split()) for line in rows.strip().splitlines()]  # type: ignore[misc]


_HEADER = ("std_msgs/Header", "header")

_MESSAGES = {
    "msg/SessionState.msg": """
        std_msgs/Header header
        uint8 phase
        uint32 seq
        string session_id
        string person_id
        bool anonymous
        string topic
        string module_id
        string lang
        uint32 cycles
        uint32 utterances
        bool exchanged
        string watchdog_state
        float32 idle_for_s
        string last_status
        string last_end_reason
        float32 last_avg_engagement
        string last_dominant_style
    """,
    "msg/SessionEvent.msg": """
        std_msgs/Header header
        uint32 seq
        string type
        string session_id
        string person_id
        string lang
        builtin_interfaces/Time source_time
        string text
        string cue_category
        string cue_family
        string cue_family_source
        string cue_slug
        string cue_recorded
        float32 cue_budget_s
        float32 est_duration
        string source
        bool has_results
        string status
        string payload_json
    """,
    "msg/GestureEvent.msg": """
        std_msgs/Header header
        uint8 kind
        string direction
    """,
    "msg/GestureState.msg": """
        std_msgs/Header header
        string mode
        bool head_reserved
        bool residual_live
        bool head_held
        bool robot_moving
        string moving_reason
        bool selection_reserved
    """,
    "msg/VoiceState.msg": """
        std_msgs/Header header
        bool enabled
        bool available
        bool active
        float32 level
        float32 rms
        float32 noise_rms
        uint32 onsets
        uint32 frames
        float32 silent_for_s
    """,
    "msg/PlayerState.msg": """
        std_msgs/Header header
        bool busy
        uint8 queue_depth
        bool legacy_motion_enabled
        uint32 generation
        string motion_owner
    """,
    "msg/MotionPlanEvent.msg": """
        std_msgs/Header header
        string plan_id
        uint32 seq
        string family
        string slug
        bool started
        bool ended
        string outcome
        float32 first_apply_delay_s
        string family_authored
        string family_source
        string skip_reason
    """,
    "msg/AddonStatus.msg": """
        std_msgs/Header header
        bool installed
        string name
        uint32 api_version
        bool busy
        bool selection_reserved
        string mode
        string status_json
    """,
    "msg/KioskLanguage.msg": """
        std_msgs/Header header
        string current
        string[] allowed
    """,
}

_SERVICES = {
    "srv/SessionCommand.srv": ("string verb\nstring reason", "bool accepted\nstring error_code"),
    "srv/StartSession.srv": (
        "string menu_text\nstring badge_payload",
        "bool accepted\nstring session_id\nstring topic\nstring error_code"),
    "srv/SetLanguage.srv": ("string lang", "bool ok\nstring lang\nstring error_code"),
    "srv/Recognize.srv": (
        "uint8[] pcm_s16le_16k\nstring lang\nstring[] grammar",
        "bool ok\nstring text\nfloat32 conf\nstring error_code"),
    "srv/SubmitMotionBatch.srv": (
        "MotionIntent[] intents\nbool drop_if_busy", "bool accepted\nstring error_code"),
    "srv/GetHubStatus.srv": (
        "", "string runtime\nbool demo\nbool robot\nstring status_json"),
    "srv/GetVoiceMenu.srv": (
        "string lang",
        "string prompt\nstring[] grammar\nstring[] option_keys\nstring[] option_names"),
    "srv/SetTtOverrides.srv": (
        "string patch_json", "bool ok\nuint32 rev\nstring error_code"),
}


@pytest.mark.parametrize("relative", sorted(_MESSAGES))
def test_a_hub_message_carries_exactly_its_designed_fields_in_order(relative):
    assert _fields(relative) == _spelled(_MESSAGES[relative])


@pytest.mark.parametrize("relative", sorted(_MESSAGES))
def test_every_hub_message_that_is_stamped_starts_with_a_header(relative):
    assert _fields(relative)[0] == _HEADER


@pytest.mark.parametrize("relative", sorted(_SERVICES))
def test_a_hub_service_carries_exactly_its_designed_request_and_response(relative):
    request, response = _SERVICES[relative]
    rows = _fields(relative)
    assert ("---", "") in rows
    split = rows.index(("---", ""))
    assert rows[:split] == (_spelled(request) if request else [])
    assert rows[split + 1:] == _spelled(response)


def test_the_say_action_has_a_goal_a_result_and_a_stage_as_feedback():
    rows = _fields("action/Say.action")
    first, second = [i for i, row in enumerate(rows) if row == ("---", "")]
    assert rows[:first] == _spelled("string text\nstring lang")
    assert rows[first + 1:second] == _spelled("bool played\nstring error_code")
    assert rows[second + 1:] == _spelled("string stage")


def test_the_phase_constants_are_the_ones_a_subscriber_decodes():
    assert _constants("msg/SessionState.msg") == [
        "PHASE_NO_SESSION=0", "PHASE_BOOTING=1", "PHASE_FRAME_ALIVE=2",
        "PHASE_EXCHANGED=3", "PHASE_END_REQUESTED=4", "PHASE_FINALIZED=5"]
    assert _constants("msg/GestureEvent.msg") == [
        "KIND_HEAD_TURN=0", "KIND_END_NUDGE=1", "KIND_BEGIN=2", "KIND_LANG_TOGGLE=3"]


def test_the_session_event_carries_no_field_that_could_hold_a_raw_results_payload():
    """Scores and summaries stay off the wire unless the raw opt-in is on, and then they
    travel inside `payload_json`, the one field whose text says so."""

    names = [name for _kind, name in _fields("msg/SessionEvent.msg")]
    assert "results" not in names
    text = (_MSGS / "msg" / "SessionEvent.msg").read_text(encoding="utf-8")
    assert "EMORI_ROS_RAW_ENVELOPES=1" in text
    assert "EMORI_ROS_SPEECH_TEXT" in text
    assert "gapless" in text


def test_the_state_messages_that_name_a_person_say_which_switch_pseudonymises_them():
    for relative in ("msg/SessionState.msg", "msg/SessionEvent.msg"):
        text = (_MSGS / relative).read_text(encoding="utf-8")
        assert re.search(r"pseudonym", text), relative


def test_speech_cue_appends_the_authored_family_and_moves_nothing_before_it():
    assert [name for _kind, name in _fields("msg/SpeechCue.msg")] == [
        "header", "person_id", "session_id", "seq", "text", "cue_category", "cue_body",
        "lang", "source", "est_duration", "source_time", "ended", "cue_category_authored"]


# --- the motion command is the adapter's own, unit for unit -------------------------------


#: What the adapter's MotionIntent has that the message leaves out on purpose: sound and
#: wobble are not performed by the hub's player, and priority orders nothing there.
_NOT_CARRIED = {"sound", "wobble", "priority"}


def test_the_motion_intent_message_mirrors_the_adapter_model():
    from emori_adapter.models import HeadPose, MotionIntent

    message = {name for _kind, name in _fields("msg/MotionIntent.msg")}
    expected: set[str] = set()
    for name in MotionIntent.model_fields:
        if name in _NOT_CARRIED:
            continue
        if name == "head":
            expected.add("has_head")
            expected.update(f"{axis}_{'mm' if axis in 'xyz' else 'deg'}"
                            for axis in HeadPose.model_fields)
        elif name == "antennas":
            expected.update({"has_antennas", "antenna_left_rad", "antenna_right_rad"})
        elif name == "body_yaw":
            expected.update({"has_body_yaw", "body_yaw_rad"})
        else:
            expected.add(name)
    assert message == expected, (
        "MotionIntent.msg and emori_adapter.models.MotionIntent have drifted: "
        f"only in the message {sorted(message - expected)}, only in the model "
        f"{sorted(expected - message)}")


# --- the build lists everything on disk, services included ---------------------------------


def test_cmakelists_generates_every_msg_srv_and_action_on_disk_and_nothing_else():
    cmake = (_MSGS / "CMakeLists.txt").read_text(encoding="utf-8")
    listed = set(re.findall(r'^\s*"((?:msg|srv|action)/[^"]+)"', cmake, re.M))
    on_disk = {f"{folder}/{path.name}"
               for folder, pattern in (("msg", "*.msg"), ("srv", "*.srv"), ("action", "*.action"))
               for path in (_MSGS / folder).glob(pattern)}
    assert on_disk
    assert listed == on_disk


def test_the_hub_interfaces_need_no_dependency_the_package_did_not_have():
    """Humble builds this package on its own, so the hub's additions must not widen it."""

    package = (_MSGS / "package.xml").read_text(encoding="utf-8")
    depends = set(re.findall(r"<depend>([^<]+)</depend>", package))
    assert depends == {"std_msgs", "builtin_interfaces", "action_msgs"}


def test_the_changelog_records_the_additions():
    text = (_MSGS / "CHANGELOG.rst").read_text(encoding="utf-8")
    for name in ("SessionEvent", "SessionState", "SubmitMotionBatch", "Say", "cue_category_authored",
                 "MotionPlanEvent", "family_authored", "family_source", "skip_reason"):
        assert name in text, name
