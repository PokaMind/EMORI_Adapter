"""`hri_context_analyzer`'s decision half, exercised on a machine with no ROS.

The node cannot be imported here: it needs ``rclpy`` and a built ``emori_msgs``, which
exist in the container on the robot and nowhere else - not on the dev boxes, not on the
CM4 host, not in CI. So everything the node *decides* lives in
``hri_context_analyzer/contracts.py``, which is pure stdlib, and this file is why that
split was worth making.

What it protects is one rule with an expensive failure mode: **a channel that reported
nothing is not published at all.** Live, only the styles and body channels arrive, so
``face/positivity`` and the two ``voice/`` topics never exist on a real session. Publish
them anyway - as ``0.0``, or as the sentinel - and the dashboard looks perfectly
healthy while describing a person nobody ever measured. That bug raises nothing, logs
nothing, and is invisible until somebody asks why the learners all score zero on warmth.

The module is loaded by PATH: ``ros2_ws`` is not on ``sys.path``, and putting it there
would mean installing ROS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_CONTRACTS = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
              / "open" / "hri_context_analyzer" / "hri_context_analyzer" / "contracts.py")

_MODULE_NAME = "hri_context_analyzer_contracts_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _CONTRACTS)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`; an unregistered module makes the decorator raise.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


contracts = _load()

# What the node passes in from `Context.NOT_REPORTED`. Spelled here and nowhere in the
# package under test, which is the point: the planner withholds on the number the
# interface declares, not on one it believes.
SENTINEL = -1.0

BASE = "/humans/persons/learner_01/context"


def _ctx(**overrides) -> SimpleNamespace:
    """A Context-shaped object: every channel silent, nothing observed, flags clear."""

    fields = dict(
        escalation=0.10, rapport=0.50, attention=0.40, engagement_trend=0.0,
        dominant_style="mirroring",
        engagement_score=0.60, valence=0.20, arousal=0.30,
        age_s=SENTINEL,
        posture="", head_position="", gesture_type="", overall_energy="",
        body_engagement=SENTINEL, body_confidence=SENTINEL,
        face_positivity=SENTINEL, voice_warmth=SENTINEL, pitch_variety=SENTINEL,
        style_names=[], style_values=[],
        report_flags=0,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _plan(ctx) -> dict[str, object]:
    """topic suffix -> value, for the cycle `ctx` describes."""

    items = contracts.plan_fan_out(ctx, base=BASE, not_reported=SENTINEL)
    topics = [item.topic for item in items]
    assert len(topics) == len(set(topics)), f"a topic is planned twice: {topics}"
    return {item.topic[len(BASE) + 1:]: item.value for item in items}


# --- the nine that are always true ----------------------------------------------------


def test_the_always_published_nine_are_planned_on_the_emptiest_cycle_there_is():
    """A health signal that goes quiet when things degrade is not a health signal.

    These are the topics the node spells out statement by statement (see
    `tests/test_ros_contracts.py::test_no_flag_gated_channel_is_published_unconditionally`),
    so the two lists agreeing is what keeps the fan-out honest in both directions:
    nothing gated slips into the unconditional block, nothing unconditional goes silent.
    """

    planned = _plan(_ctx())
    assert set(contracts.ALWAYS_TOPICS) <= set(planned)
    assert planned["dominant_style"] == "mirroring"
    assert planned["escalation"] == pytest.approx(0.10)
    assert planned["engagement_score"] == pytest.approx(0.60)

    # ...even when the style itself is empty: "" is what the topic has to say.
    assert "dominant_style" in _plan(_ctx(dominant_style=""))


def test_a_degraded_cycle_says_so_and_claims_nothing_else():
    """`report_flags == 0`: the cycle carried no observation of the person at all."""

    planned = _plan(_ctx(report_flags=0))

    assert planned["degraded"] is True
    assert contracts.should_publish_expression(0) is False
    # Nothing but the always-nine survives a cycle that measured nothing.
    assert set(planned) == set(contracts.ALWAYS_TOPICS)


def test_an_observed_cycle_is_not_degraded_and_may_claim_an_expression():
    planned = _plan(_ctx(report_flags=int(contracts.ReportFlags.BODY)))

    assert planned["degraded"] is False
    assert contracts.should_publish_expression(int(contracts.ReportFlags.BODY)) is True


# --- the flag gate --------------------------------------------------------------------


def test_a_body_only_cycle_publishes_the_body_channels_and_nothing_facial_or_vocal():
    """The live shape: Pokamind sends body + styles, and never a face or a voice.

    The facial and tone/verbal topics must not exist on this cycle. An absent topic is
    something a subscriber can detect; a zero is a measurement it cannot distinguish
    from a real one.
    """

    planned = _plan(_ctx(
        report_flags=int(contracts.ReportFlags.BODY),
        posture="lean_forward", head_position="nodding", gesture_type="open_palm",
        overall_energy="moderate",
        body_engagement=0.72, body_confidence=0.55,
        # Present in the struct, never measured - exactly how the translator fills them.
        face_positivity=SENTINEL, voice_warmth=SENTINEL, pitch_variety=SENTINEL,
    ))

    assert planned["body/posture"] == "lean_forward"
    assert planned["body/head_position"] == "nodding"
    assert planned["body/gesture_type"] == "open_palm"
    assert planned["body/energy"] == "moderate"
    assert planned["body/engagement"] == pytest.approx(0.72)
    assert planned["body/confidence"] == pytest.approx(0.55)

    for absent in ("face/positivity", "voice/warmth", "voice/pitch_variety", "top_styles"):
        assert absent not in planned, f"{absent} published on a body-only cycle"


def test_a_cleared_flag_withholds_the_channel_even_when_it_carries_a_number():
    """The flag is the modality's own word for whether it spoke; the value cannot vote."""

    planned = _plan(_ctx(report_flags=0, body_engagement=0.9, face_positivity=0.8,
                         voice_warmth=0.7, pitch_variety=0.6))

    for absent in ("body/engagement", "face/positivity", "voice/warmth",
                   "voice/pitch_variety"):
        assert absent not in planned


def test_a_value_equal_to_the_sentinel_is_withheld_even_with_the_flag_set():
    """Two conditions, not one: a body cycle can name a posture without scoring one."""

    planned = _plan(_ctx(
        report_flags=int(contracts.ReportFlags.BODY),
        posture="upright", body_engagement=SENTINEL, body_confidence=0.41,
    ))

    assert planned["body/posture"] == "upright"
    assert "body/engagement" not in planned
    assert planned["body/confidence"] == pytest.approx(0.41)


def test_a_categorical_channel_says_nothing_with_an_empty_string():
    planned = _plan(_ctx(report_flags=int(contracts.ReportFlags.BODY),
                         posture="", head_position="tilted"))

    assert "body/posture" not in planned
    assert planned["body/head_position"] == "tilted"


@pytest.mark.parametrize("flag, topic, field", [
    ("FACE", "face/positivity", "face_positivity"),
    ("VERBAL", "voice/warmth", "voice_warmth"),
    ("TONE", "voice/pitch_variety", "pitch_variety"),
])
def test_each_rich_channel_needs_its_own_flag(flag, topic, field):
    """`voice/warmth` is the VERBAL channel and `pitch_variety` the TONE one.

    However the topic names read - this is the mapping the translator fills, and getting
    it backwards would publish a warmth score every time the tone channel spoke.
    """

    bit = int(getattr(contracts.ReportFlags, flag))
    assert topic in _plan(_ctx(report_flags=bit, **{field: 0.5}))
    everything_else = 31 & ~bit
    assert topic not in _plan(_ctx(report_flags=everything_else, **{field: 0.5}))


# --- freshness ------------------------------------------------------------------------


def test_freshness_is_withheld_at_the_sentinel_and_published_otherwise():
    """`age_s` belongs to no modality, so the sentinel alone gates it.

    The mock source stamps a fixed epoch and therefore reports no age at all; publishing
    a number there would be a freshness signal that grows by a day every day.
    """

    assert "age_s" not in _plan(_ctx(age_s=SENTINEL))
    assert _plan(_ctx(age_s=1.75))["age_s"] == pytest.approx(1.75)
    # ...and it does not need a flag: a degraded cycle can still be a fresh one.
    assert "age_s" in _plan(_ctx(report_flags=0, age_s=0.0))


# --- styles ---------------------------------------------------------------------------


def test_the_style_list_is_one_readable_line():
    assert contracts.format_styles(["a", "b"], [0.31, 0.18]) == "a:0.31,b:0.18"
    assert contracts.format_styles([], []) == ""
    # Ragged input is truncated rather than raising: the wire arrays are validated
    # upstream, and a node is not the place to discover they were not.
    assert contracts.format_styles(["a", "b"], [0.5]) == "a:0.50"


def test_top_styles_needs_the_styles_flag_and_a_non_empty_list():
    names, values = ["mirroring", "paced"], [0.31, 0.18]

    styles = int(contracts.ReportFlags.STYLES)
    assert _plan(_ctx(report_flags=styles, style_names=names,
                      style_values=values))["top_styles"] == "mirroring:0.31,paced:0.18"
    # Flag set, nothing in the list: an empty line is not a value.
    assert "top_styles" not in _plan(_ctx(report_flags=styles))
    # List present, flag clear: the channel did not speak this cycle.
    assert "top_styles" not in _plan(_ctx(report_flags=int(contracts.ReportFlags.BODY),
                                          style_names=names, style_values=values))


# --- the expression field, whichever hri_msgs release is installed --------------------


class _StringExpression:
    """The hri_msgs release that declares `expression` as a plain string (2.1.0)."""

    def __init__(self) -> None:
        self.expression = ""
        self.confidence = 0.0


class _NumericExpression:
    """The release that declares it as a uint8 with class constants."""

    NEUTRAL = 0
    HAPPY = 1
    SAD = 2
    ANGRY = 3
    SURPRISED = 4
    DISGUSTED = 6

    def __init__(self) -> None:
        self.expression = 0


class _FieldlessExpression:
    """A release with no `expression` field at all - the shape the node warns about."""


def test_expression_field_shape_names_the_installed_release():
    assert contracts.expression_field_shape(_StringExpression()) == "str"
    assert contracts.expression_field_shape(_NumericExpression()) == "numeric"
    assert contracts.expression_field_shape(_FieldlessExpression()) == "absent"


def test_a_string_release_takes_the_label_verbatim_and_lowercased():
    message = _StringExpression()

    assert contracts.set_expression_field(message, "  Happy ") == ""
    assert message.expression == "happy"


def test_a_numeric_release_takes_the_class_constant_including_zero():
    """NEUTRAL is 0, and 0 is falsy: a truthiness check here drops the commonest label."""

    message = _NumericExpression()

    assert contracts.set_expression_field(message, "neutral") == ""
    assert message.expression == 0

    assert contracts.set_expression_field(message, "angry") == ""
    assert message.expression == _NumericExpression.ANGRY


@pytest.mark.parametrize("message, label, reason", [
    (_StringExpression(), "", "empty_label"),
    (_StringExpression(), "   ", "empty_label"),
    (_FieldlessExpression(), "happy", "no_expression_field"),
    # A label the shared table never mapped: dropped, not coerced to neutral.
    (_NumericExpression(), "smug", "unmapped_label"),
    # Mapped, but this release spells it differently - `scared` has no constant here.
    (_NumericExpression(), "scared", "constant_absent"),
])
def test_every_way_a_label_can_be_dropped_is_named_rather_than_silent(message, label, reason):
    assert contracts.set_expression_field(message, label) == reason
    assert reason in contracts.EXPRESSION_DROP_REASONS, "a drop with no sentence to log"


def test_the_label_table_covers_the_seven_labels_and_nothing_else():
    assert set(contracts.EXPRESSION_CONSTANT_NAMES) == {
        "neutral", "happy", "sad", "angry", "surprised", "scared", "disgusted"}
    assert all(name == label.upper()
               for label, name in contracts.EXPRESSION_CONSTANT_NAMES.items())


# --- the planner's own shape ----------------------------------------------------------


def test_every_planned_item_names_a_kind_the_node_can_publish():
    """Three std_msgs wrappers and no fourth: the node's dispatch has three branches."""

    everything = _ctx(
        report_flags=31, age_s=0.5,
        posture="upright", head_position="nodding", gesture_type="pointing",
        overall_energy="high",
        body_engagement=0.7, body_confidence=0.6, face_positivity=0.5,
        voice_warmth=0.4, pitch_variety=0.3,
        style_names=["mirroring"], style_values=[0.31],
    )
    items = contracts.plan_fan_out(everything, base=BASE, not_reported=SENTINEL)

    assert {item.kind for item in items} == {"float", "string", "bool"}
    assert all(item.topic.startswith(f"{BASE}/") for item in items)
    # The whole fan-out, every channel reporting: twenty topics under /context.
    assert len(items) == 20
