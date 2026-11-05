"""The privacy gate on the DDS wire, exercised on a machine with no ROS.

`emori_adapter_ros/privacy.py` decides what leaves the robot on the ROS 2 domain. On
the robot the only other control is `ROS_LOCALHOST_ONLY=1`, which is a networking setting
that says nothing about who may read a topic once they are on the host — so if this
module is wrong, the failure is a roster student id and a transcript of the robot's
half of a conversation sitting on a DDS domain, and nothing anywhere goes red.

The module is loaded by PATH (`ros2_ws` is not importable here and installing it would
need ROS), which is the same pattern `tests/test_skill_facade.py` uses and the reason
`privacy.py` is stdlib-only.

The node file itself is checked as TEXT, for the same reason every other ROS assertion
in this suite is: the publisher cannot be imported without `rclpy`, and the thing worth
pinning is which fields it routes through the gate and which it deliberately does not.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_ROS_WS = Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
_PACKAGE = _ROS_WS / "emori" / "emori_adapter_ros" / "emori_adapter_ros"
_PRIVACY = _PACKAGE / "privacy.py"
_PUBLISHER = _PACKAGE / "publisher_node.py"

_MODULE_NAME = "emori_adapter_ros_privacy_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PRIVACY)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves annotations through
    # `sys.modules[cls.__module__]`; an unregistered module makes the decorator raise.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


privacy = _load()


# --- the pseudonym ------------------------------------------------------------------


def test_a_pseudonym_is_stable_prefixed_and_short_enough_to_read():
    """Stable inside one salt, because a consumer has to be able to group a session."""

    first = privacy.pseudonym("student-4711", "pepper")
    assert first == privacy.pseudonym("student-4711", "pepper")
    assert first.startswith("p-")
    assert len(first) == len("p-") + 16
    assert re.fullmatch(r"p-[0-9a-f]{16}", first)


def test_a_different_salt_is_a_different_pseudonym_for_the_same_person():
    """This is the whole value of the salt: two captures must not join on the id."""

    assert (privacy.pseudonym("student-4711", "salt-a")
            != privacy.pseudonym("student-4711", "salt-b"))


def test_two_people_under_one_salt_stay_two_people():
    assert (privacy.pseudonym("student-4711", "pepper")
            != privacy.pseudonym("student-4712", "pepper"))


def test_an_absent_id_stays_absent_instead_of_becoming_a_plausible_person():
    """Hashing "" would mint a real-looking token that a dashboard groups rows under."""

    assert privacy.pseudonym("", "pepper") == ""
    assert privacy.pseudonym("", "") == ""


def test_the_pseudonym_is_the_documented_construction():
    """Pinned so a refactor cannot silently change every id in every historised row."""

    import hashlib

    expected = "p-" + hashlib.sha256(b"pepper:student-4711").hexdigest()[:16]
    assert privacy.pseudonym("student-4711", "pepper") == expected


# --- reading the environment ----------------------------------------------------------


def test_the_defaults_are_the_protective_ones():
    """Nothing set anywhere means: pseudonymise, and do not put speech on the wire."""

    posture = privacy.Privacy.from_env({})
    assert posture.deidentify is True
    assert posture.speech_text is False
    assert posture.ephemeral is True
    assert re.fullmatch(r"[0-9a-f]{32}", posture.salt), "not a 16-byte hex salt"


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("No", False), (" off ", False),
])
def test_the_knobs_read_the_spellings_a_compose_file_actually_contains(raw, expected):
    assert privacy.Privacy.from_env({"EMORI_ROS_DEIDENTIFY": raw}).deidentify is expected
    assert privacy.Privacy.from_env({"EMORI_ROS_SPEECH_TEXT": raw}).speech_text is expected


@pytest.mark.parametrize("raw", ["", "  ", "flase", "maybe", "2", "y"])
def test_an_unrecognised_value_falls_back_to_the_protective_default(raw):
    """A typo must not be how a privacy control gets switched off."""

    posture = privacy.Privacy.from_env({"EMORI_ROS_DEIDENTIFY": raw,
                                        "EMORI_ROS_SPEECH_TEXT": raw})
    assert posture.deidentify is True
    assert posture.speech_text is False


def test_a_configured_salt_is_used_verbatim_and_is_not_ephemeral():
    posture = privacy.Privacy.from_env({"EMORI_ROS_ID_SALT": "  study-2026-09  "})
    assert posture.salt == "study-2026-09"
    assert posture.ephemeral is False


def test_the_ephemeral_salt_is_minted_once_per_process():
    """Stable within a run (a session groups) and gone at restart (two runs do not).

    Both halves matter, and only the first is testable here — so it is the one pinned:
    if `from_env` minted a fresh salt per call, every message of one session would
    carry a different pseudonym and the id would be noise rather than a pseudonym.
    """

    first = privacy.Privacy.from_env({})
    second = privacy.Privacy.from_env({"EMORI_ROS_SPEECH_TEXT": "1"})
    assert first.salt == second.salt == privacy.ephemeral_salt()
    assert first.ephemeral is True and second.ephemeral is True


def test_from_env_falls_back_to_the_real_environment(monkeypatch):
    """The node calls it with no argument; that path has to be the same one."""

    monkeypatch.setenv("EMORI_ROS_DEIDENTIFY", "0")
    monkeypatch.setenv("EMORI_ROS_SPEECH_TEXT", "1")
    monkeypatch.setenv("EMORI_ROS_ID_SALT", "from-the-container")
    posture = privacy.Privacy.from_env()
    assert (posture.deidentify, posture.speech_text) == (False, True)
    assert posture.salt == "from-the-container" and posture.ephemeral is False


# --- applying it ----------------------------------------------------------------------


def test_both_identity_fields_are_pseudonymised_together():
    posture = privacy.Privacy(deidentify=True, salt="pepper")
    person, session = posture.apply_ids("student-4711", "sess-99")
    assert person == privacy.pseudonym("student-4711", "pepper")
    assert session == privacy.pseudonym("sess-99", "pepper")
    assert person != session


def test_turning_deidentify_off_passes_the_real_ids_through_unchanged():
    """The opt-out has to be a real opt-out, or a study cannot be run at all."""

    posture = privacy.Privacy(deidentify=False, salt="pepper")
    assert posture.apply_ids("student-4711", "sess-99") == ("student-4711", "sess-99")


def test_speech_is_withheld_by_default_and_the_cue_body_goes_with_it():
    """`cue_body` is free-form direction about the same sentence: same exposure."""

    posture = privacy.Privacy()
    assert posture.apply_speech("Let's try that again.", "warm, slower") == ("", "")


def test_turning_speech_text_on_publishes_the_sentence_verbatim():
    posture = privacy.Privacy(speech_text=True)
    assert posture.apply_speech("Let's try that again.", "warm") == (
        "Let's try that again.", "warm")


def test_the_posture_is_frozen():
    """Read once at node start: a capture half of which is pseudonymised is worthless."""

    posture = privacy.Privacy()
    with pytest.raises(Exception):
        posture.deidentify = False


@pytest.mark.parametrize("posture, expected", [
    (privacy.Privacy(), "deidentify=1 speech_text=0 salt=configured"),
    (privacy.Privacy(ephemeral=True), "deidentify=1 speech_text=0 salt=ephemeral"),
    (privacy.Privacy(deidentify=False, speech_text=True),
     "deidentify=0 speech_text=1 salt=configured"),
])
def test_the_log_line_says_everything_needed_to_interpret_a_capture(posture, expected):
    assert posture.describe() == expected


# --- what the node does with it -------------------------------------------------------


def _publisher_source() -> str:
    return _PUBLISHER.read_text(encoding="utf-8")


def test_the_publisher_builds_the_posture_once_and_logs_it():
    source = _publisher_source()
    assert "from emori_adapter_ros.privacy import Privacy" in source
    constructions = source.count("Privacy.from_env(")
    assert constructions == 1, "the posture must be read once, at node start"
    assert "describe()" in source, "the effective posture is not in the journal"
    # An unlinkable salt is a real limitation of the capture; say so in the log rather
    # than leaving an analyst to discover it when two days refuse to join.
    assert "not linkable across restarts" in source.lower()


def test_both_identity_carrying_messages_go_through_the_gate():
    """`Context` and `SpeechCue` are the two messages with an identity on them."""

    source = _publisher_source()
    context = source.split("def _publish(", 1)[1]
    cue = source.split("def _publish_speech_cue(", 1)[1].split("\n    def ", 1)[0]

    assert "ctx.person_id, ctx.session_id = self._privacy.apply_ids(" in context
    assert "msg.person_id, msg.session_id = self._privacy.apply_ids(" in cue
    assert "msg.text, msg.cue_body = self._privacy.apply_speech(" in cue
    # ...and no raw assignment survives alongside the gated one.
    assert not re.search(r"ctx\.person_id\s*=\s*sig\.", context)
    assert not re.search(r"msg\.person_id\s*=\s*event\.", cue)
    assert not re.search(r"msg\.text\s*=\s*_CUE_BRACKETS", cue)


def test_the_fields_the_behaviour_lane_needs_are_never_touched():
    """Withholding these would protect nobody and would stop the head moving.

    `cue_category`, `est_duration`, `lang`, `ended` and `seq` describe the ROBOT's own
    utterance plan, not the learner. The engagement policy budgets the head against
    exactly those, so a privacy change that swept them up would look like a privacy win
    and land as a robot that stares straight ahead while it talks.
    """

    cue = _publisher_source().split("def _publish_speech_cue(", 1)[1]
    assert "msg.cue_category = event.cue_category" in cue
    assert "msg.lang = event.lang" in cue
    assert "msg.est_duration = float(event.est_duration)" in cue
    assert "msg.ended = bool(event.ended)" in cue
    assert "msg.seq = max(0, int(event.seq))" in cue


def test_the_topic_namespace_is_still_the_stable_parameter():
    """Pseudonymising the topic TREE would churn the graph every session for no gain.

    The namespace id is a launch parameter, not an identity; the identity rides inside
    the message, which is exactly the half the gate acts on.
    """

    source = _publisher_source()
    assert 'base = f"/humans/persons/{self.person_id}"' in source
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "create_publisher"]
    assert calls, "the publisher creates no publishers -- has the node moved?"
    for call in calls:
        # Every topic name stays a literal or an f-string over `base`, which is what
        # the graph-symmetry checks in tests/test_ros_contracts.py walk.
        assert isinstance(call.args[1], (ast.Constant, ast.JoinedStr))


def test_privacy_is_pure_stdlib_so_it_stays_testable_here():
    """One ROS import in this module and every test above becomes a collection error."""

    tree = ast.parse(_PRIVACY.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".", 1)[0])
    assert modules <= {"hashlib", "os", "secrets", "collections", "dataclasses",
                       "__future__"}, sorted(modules)
