"""The `emori_msgs/Context` fan-out contract, in plain Python, with no ROS and no core.

``context_analyzer_node.py`` cannot be imported anywhere ``rclpy`` and a built
``emori_msgs`` are absent - which is the dev boxes, the CM4 host and CI, i.e. every
machine a test actually runs on. So the node is split in two: this module decides
*what* this cycle earns a topic, the node knows *how* to put it there. Every rule below
is then checkable on a laptop (``tests/test_context_analyzer_pure.py``) instead of only
inside a container on a robot.

The second reason this file exists is an import the node must not have:
``from emori_adapter.translation import ...``. The analyzer is published as a
ROS 2 package and ships in the standalone image, which builds the four packages under
``ros2_ws/src/open`` and installs no EMORI core at all; that import is an ``ImportError`` at
node start there, with ``ros2 topic list`` still looking exactly right until somebody
subscribes. The constants here mirror the ones ``Context.msg`` now declares for itself,
and ``tests/test_ros_contracts.py::test_context_msg_constants_match_the_core_contract``
pins interface, mirror and core to the same numbers so the three cannot drift apart.

**The rule the fan-out exists to enforce.** A channel that said nothing this cycle is
not published *at all* - not as ``0.0``, not as the sentinel. Live, only the styles and
body channels arrive, so ``face/positivity`` and the two ``voice/`` topics simply do not
exist on a real session. An absent topic is something a subscriber can detect; an
unbroken line of confident zeroes is not, and a plotted zero and a measured zero are the
same pixel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import Any, Literal, Sequence


class ReportFlags(IntFlag):
    """Which input channels reported anything at all this cycle.

    The pure mirror of ``Context.REPORT_*`` (and of the core's ``PersonReportFlags``,
    which the publisher on the other side of the wire still uses). ``0`` is the
    degraded cycle: nothing whatsoever was observed of this person.
    """

    BODY = 1
    FACE = 2
    TONE = 4
    VERBAL = 8
    STYLES = 16


# label -> the hri_msgs/Expression class-constant name, for the releases that declare
# the field as a uint8 with constants instead of a plain string. Duplicated from the
# core's table by design: the package must build with no EMORI code installed, and
# `test_context_msg_constants_match_the_core_contract` asserts the two agree.
EXPRESSION_CONSTANT_NAMES: dict[str, str] = {
    "neutral": "NEUTRAL",
    "happy": "HAPPY",
    "sad": "SAD",
    "angry": "ANGRY",
    "surprised": "SURPRISED",
    "scared": "SCARED",
    "disgusted": "DISGUSTED",
}


# Why a label never reached the topic, in the words the journal needs. The drop is
# INVISIBLE otherwise: `pub_expression` is created in `__init__`, so
# `/humans/persons/<id>/expression` is listed by `ros2 topic list` whether or not one
# message ever crosses it - an operator running the runbook's own check confirms a topic
# that publishes nothing. The partial case is worse than the total one: six labels
# publish and `scared` silently never does, which reads downstream as a learner who was
# never once frightened.
EXPRESSION_DROP_REASONS = {
    "empty_label": "the cycle carried no expression label",
    "no_expression_field": "the installed hri_msgs/Expression has no `expression` field",
    "unmapped_label": "this label is in no EXPRESSION_CONSTANT_NAMES entry",
    "constant_absent": "the installed hri_msgs/Expression declares no such constant "
                       "(a release that spells this label differently)",
}


def expression_field_shape(msg) -> str:
    """Which hri_msgs release is installed: ``"str"``, ``"numeric"`` or ``"absent"``.

    Logged once at start, so a silent expression topic is a line in the journal rather
    than an afternoon inside a container.
    """

    if not hasattr(msg, "expression"):
        return "absent"
    return "str" if isinstance(getattr(msg, "expression"), str) else "numeric"


def set_expression_field(msg, label: str) -> str:
    """Assign ``label`` to ``msg.expression``, whichever shape hri_msgs declares.

    ``hri_msgs/Expression`` is not stable across releases: some declare ``expression``
    as a plain string, others as a ``uint8`` with class constants (``Expression.HAPPY``).
    Assuming either one is a node that raises ``AssertionError`` deep inside the
    serialiser on the first non-neutral cycle, inside a container, on a robot - so the
    installed message is asked what it is instead.

    Returns ``""`` when the field was set, else the ``EXPRESSION_DROP_REASONS`` key
    naming why it was not. A reason and not a bare ``False``: an unknown label is
    dropped rather than coerced to neutral - "the robot showed nothing" is true and
    "the robot was neutral" is a claim - but a drop nobody can name is a topic that
    exists and never publishes, which is the same silence as a dead node.
    """

    text = (label or "").strip().lower()
    if not text:
        return "empty_label"
    if not hasattr(msg, "expression"):
        return "no_expression_field"

    if isinstance(getattr(msg, "expression"), str):
        msg.expression = text
        return ""

    constant = EXPRESSION_CONSTANT_NAMES.get(text)
    if constant is None:
        return "unmapped_label"
    # `is None` rather than truthiness: NEUTRAL is 0 in every release that uses the
    # numeric form, and `or` would discard exactly the most common label.
    value = getattr(type(msg), constant, None)
    if value is None:
        value = getattr(msg, constant, None)
    if value is None:
        return "constant_absent"
    msg.expression = int(value)
    return ""


def format_styles(names: Sequence[str], values: Sequence[float]) -> str:
    """``"mirroring:0.31,paced:0.18"`` - the top-N list as one human-readable line."""

    return ",".join(f"{name}:{float(value):.2f}"
                    for name, value in zip(names, values))


def should_publish_expression(report_flags: int) -> bool:
    """Whether this cycle may make a claim on ``/humans/persons/<id>/expression``.

    The expression is DERIVED from valence/arousal, and an empty cycle derives the origin
    of that plane - which the translator maps to "neutral" at confidence 1.0. The
    standard REP-155 topic carries nowhere to say "degraded", so publishing that would be
    a confident claim about a person nobody observed, on the one topic the wider ROS4HRI
    world actually reads. A cycle that reported nothing makes no claim.
    """

    return int(report_flags) != 0


@dataclass(frozen=True)
class FanOutItem:
    """One topic this cycle earned: its name, its std_msgs wrapper, and the value.

    ``kind`` rather than the message class itself, so the planner stays importable with
    no ROS on the machine; the node maps the three kinds onto ``Float32`` / ``String`` /
    ``Bool`` and nothing else in the fan-out needs to know they exist.
    """

    topic: str
    kind: Literal["float", "string", "bool"]
    value: float | str | bool


# The topics computed on every cycle and therefore published on every cycle. Withholding
# them would make a healthy node indistinguishable from a dead one, and `degraded`
# belongs here above all: a health signal that goes quiet when things degrade is not a
# health signal. The node publishes exactly these, one visible statement each, so that
# `tests/test_ros_contracts.py::test_no_flag_gated_channel_is_published_unconditionally`
# can read the callback with `ast` and prove nothing else escapes its gate.
ALWAYS_TOPICS: tuple[str, ...] = (
    "escalation",
    "rapport",
    "attention",
    "engagement_trend",
    "dominant_style",
    "engagement_score",
    "valence",
    "arousal",
    "degraded",
)


def plan_fan_out(ctx: Any, *, base: str, not_reported: float) -> list[FanOutItem]:
    """The complete list of topics this cycle earns, in publication order.

    ``ctx`` is anything carrying the ``Context`` field names - the generated message in
    the container, a plain object in a test. ``base`` is the person's context namespace
    (``/humans/persons/<id>/context``); ``not_reported`` is ``Context.NOT_REPORTED``,
    passed in rather than spelled here so the node can only ever withhold on the number
    the interface itself declares.

    Absence is expressed by the item simply not being in the list. Three different
    reasons produce it, and they are not interchangeable: a categorical channel says
    nothing with ``""``, a numeric channel says nothing with the sentinel, and a whole
    modality says nothing by leaving its bit clear in ``report_flags``. A float needs
    *both* of the last two to be satisfied - a body cycle can name a posture without
    scoring one.
    """

    flags = int(getattr(ctx, "report_flags", 0) or 0)
    items: list[FanOutItem] = []

    def add(suffix: str, kind: str, value) -> None:
        items.append(FanOutItem(f"{base}/{suffix}", kind, value))

    def add_text(suffix: str, text) -> None:
        """A categorical value only when there is one; "" is absence, not a value."""

        if text:
            add(suffix, "string", str(text))

    def add_reported(suffix: str, reported: bool, value) -> None:
        """A float only when it is a MEASUREMENT: the modality spoke AND scored."""

        if reported and float(value) != not_reported:
            add(suffix, "float", float(value))

    # v1: the interpreted signal, always computed and therefore always published.
    add("escalation", "float", float(ctx.escalation))
    add("rapport", "float", float(ctx.rapport))
    add("attention", "float", float(ctx.attention))
    add("engagement_trend", "float", float(ctx.engagement_trend))
    add("dominant_style", "string", str(ctx.dominant_style))

    # v2 derived scalars, on the same terms.
    add("engagement_score", "float", float(ctx.engagement_score))
    add("valence", "float", float(ctx.valence))
    add("arousal", "float", float(ctx.arousal))
    add("degraded", "bool", flags == 0)

    # Freshness is only a fact when the envelope carried a clock of its own; it belongs
    # to no modality, so the sentinel alone gates it.
    if float(ctx.age_s) != not_reported:
        add("age_s", "float", float(ctx.age_s))

    add_text("body/posture", ctx.posture)
    add_text("body/head_position", ctx.head_position)
    add_text("body/gesture_type", ctx.gesture_type)
    add_text("body/energy", ctx.overall_energy)

    body = bool(flags & ReportFlags.BODY)
    add_reported("body/engagement", body, ctx.body_engagement)
    add_reported("body/confidence", body, ctx.body_confidence)
    add_reported("face/positivity", bool(flags & ReportFlags.FACE), ctx.face_positivity)
    # warmth is the VERBAL channel and pitch_variety the TONE one, as the translator
    # fills them - not the other way round, however the topic names read.
    add_reported("voice/warmth", bool(flags & ReportFlags.VERBAL), ctx.voice_warmth)
    add_reported("voice/pitch_variety", bool(flags & ReportFlags.TONE), ctx.pitch_variety)

    if flags & ReportFlags.STYLES:
        add_text("top_styles", format_styles(ctx.style_names, ctx.style_values))

    return items
