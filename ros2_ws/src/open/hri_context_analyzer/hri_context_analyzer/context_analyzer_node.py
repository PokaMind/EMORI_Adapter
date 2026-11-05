"""Context Analysis Module node.

Subscribes the structured ``emori_msgs/Context`` and fans it out to standardised,
single-purpose REP-155-extension topics under ``/humans/persons/<id>/context/``::

    .../context/escalation        std_msgs/Float32   (0..1)
    .../context/rapport           std_msgs/Float32   (0..1)
    .../context/attention         std_msgs/Float32   (0..1)
    .../context/engagement_trend  std_msgs/Float32   (-1..1)
    .../context/dominant_style    std_msgs/String

    # v2 -- the derived scalars a generic consumer had to parse the custom message for
    .../context/engagement_score  std_msgs/Float32   (0..1)
    .../context/valence           std_msgs/Float32   (-1..1)
    .../context/arousal           std_msgs/Float32   (-1..1)
    .../context/age_s             std_msgs/Float32   (freshness; withheld when unknown)
    .../context/degraded          std_msgs/Bool      (report_flags == 0)

    # v2 -- the raw person report, one topic per channel, EACH FLAG-GATED
    .../context/body/posture        std_msgs/String
    .../context/body/head_position  std_msgs/String
    .../context/body/gesture_type   std_msgs/String
    .../context/body/energy         std_msgs/String
    .../context/body/engagement     std_msgs/Float32
    .../context/body/confidence     std_msgs/Float32
    .../context/face/positivity     std_msgs/Float32   (live: never published)
    .../context/voice/warmth        std_msgs/Float32   (live: never published)
    .../context/voice/pitch_variety std_msgs/Float32   (live: never published)
    .../context/top_styles          std_msgs/String    ("name:0.31,name:0.18,...")

and, on the standard person topic rather than under ``/context``::

    /humans/persons/<id>/expression   hri_msgs/Expression  (withheld on a degraded cycle)

These scalar topics are what generic downstream consumers (the FIWARE bridge, a
trainer dashboard, rqt_plot) subscribe to - they don't need to know the EMORI
custom message.

**This package imports nothing from EMORI's core.** Taking the sentinel and the label
table from ``emori_adapter.translation`` would make a standalone ROS 2 package depend
on the adapter core: the standalone image builds the
four packages under ``ros2_ws/src/open`` and installs no core at all, so that import is an
``ImportError`` at node start - with the topic list still looking exactly right until
somebody subscribes. The wire contract now lives where a contract belongs, in
``Context.msg`` (``Context.NOT_REPORTED``, ``Context.REPORT_*``), and the decisions live
next door in :mod:`hri_context_analyzer.contracts`, which is pure stdlib and therefore
testable on a machine with no ROS.

**The rule that governs the per-channel topics.** A flag-gated topic is not published
*at all* when its channel said nothing this cycle. It is never published as ``0.0``.
Live, the facial / tone / verbal channels never arrive, so ``face/positivity`` and the
two ``voice/`` topics simply do not exist on a real session - and that is the honest
answer. A subscriber then sees an absent (or stale) topic, which it can detect, rather
than a stream of confident zeroes, which it cannot: a plotted zero and a measured zero
are the same pixel, and the difference between them is a learner nobody looked at
versus a learner who scored nothing.
"""

from __future__ import annotations

import rclpy
from rclpy.node import Node

from std_msgs.msg import Bool, Float32, String
from hri_msgs.msg import Expression
from emori_msgs.msg import Context

from .contracts import (
    ALWAYS_TOPICS,
    EXPRESSION_DROP_REASONS,
    FanOutItem,
    ReportFlags,
    expression_field_shape,
    plan_fan_out,
    set_expression_field,
    should_publish_expression,
)


# The generated message declares the channel bits; `contracts.ReportFlags` mirrors them
# so the planner stays importable with no ROS. Two spellings of one contract, so they
# are compared out loud at node start against the INSTALLED emori_msgs: a rebuilt
# interface that renumbered a bit would otherwise gate the wrong channel forever, and
# the only symptom would be a topic that never publishes.
_WIRE_FLAGS: tuple[tuple[str, int, ReportFlags], ...] = (
    ("REPORT_BODY", Context.REPORT_BODY, ReportFlags.BODY),
    ("REPORT_FACE", Context.REPORT_FACE, ReportFlags.FACE),
    ("REPORT_TONE", Context.REPORT_TONE, ReportFlags.TONE),
    ("REPORT_VERBAL", Context.REPORT_VERBAL, ReportFlags.VERBAL),
    ("REPORT_STYLES", Context.REPORT_STYLES, ReportFlags.STYLES),
)


class ContextAnalyzerNode(Node):
    def __init__(self) -> None:
        super().__init__("hri_context_analyzer")
        self.declare_parameter("person_id", "learner_01")
        self.person_id = self.get_parameter("person_id").value
        person = f"/humans/persons/{self.person_id}"
        base = f"{person}/context"

        self.sub = self.create_subscription(Context, base, self._on_context, 10)
        self.pub_esc = self.create_publisher(Float32, f"{base}/escalation", 10)
        self.pub_rap = self.create_publisher(Float32, f"{base}/rapport", 10)
        self.pub_att = self.create_publisher(Float32, f"{base}/attention", 10)
        self.pub_trend = self.create_publisher(Float32, f"{base}/engagement_trend", 10)
        self.pub_style = self.create_publisher(String, f"{base}/dominant_style", 10)

        # v2 derived scalars: always computed, therefore always published.
        self.pub_eng = self.create_publisher(Float32, f"{base}/engagement_score", 10)
        self.pub_val = self.create_publisher(Float32, f"{base}/valence", 10)
        self.pub_aro = self.create_publisher(Float32, f"{base}/arousal", 10)
        self.pub_age = self.create_publisher(Float32, f"{base}/age_s", 10)
        self.pub_degraded = self.create_publisher(Bool, f"{base}/degraded", 10)

        # v2 person report: one topic per observed channel, published only when the
        # channel actually reported (see the module docstring).
        self.pub_posture = self.create_publisher(String, f"{base}/body/posture", 10)
        self.pub_head = self.create_publisher(String, f"{base}/body/head_position", 10)
        self.pub_gesture = self.create_publisher(String, f"{base}/body/gesture_type", 10)
        self.pub_energy = self.create_publisher(String, f"{base}/body/energy", 10)
        self.pub_body_eng = self.create_publisher(Float32, f"{base}/body/engagement", 10)
        self.pub_body_conf = self.create_publisher(Float32, f"{base}/body/confidence", 10)
        self.pub_face_pos = self.create_publisher(Float32, f"{base}/face/positivity", 10)
        self.pub_warmth = self.create_publisher(Float32, f"{base}/voice/warmth", 10)
        self.pub_pitch = self.create_publisher(Float32, f"{base}/voice/pitch_variety", 10)
        self.pub_top_styles = self.create_publisher(String, f"{base}/top_styles", 10)

        # The planner names topics, not attributes, so one lookup table turns a decision
        # into a publisher. Built from the publishers above rather than from a list of
        # strings: a topic the planner can name but nobody advertised would otherwise be
        # a cycle that silently goes nowhere.
        self._base = base
        self._pubs = {
            f"{base}/escalation": self.pub_esc,
            f"{base}/rapport": self.pub_rap,
            f"{base}/attention": self.pub_att,
            f"{base}/engagement_trend": self.pub_trend,
            f"{base}/dominant_style": self.pub_style,
            f"{base}/engagement_score": self.pub_eng,
            f"{base}/valence": self.pub_val,
            f"{base}/arousal": self.pub_aro,
            f"{base}/age_s": self.pub_age,
            f"{base}/degraded": self.pub_degraded,
            f"{base}/body/posture": self.pub_posture,
            f"{base}/body/head_position": self.pub_head,
            f"{base}/body/gesture_type": self.pub_gesture,
            f"{base}/body/energy": self.pub_energy,
            f"{base}/body/engagement": self.pub_body_eng,
            f"{base}/body/confidence": self.pub_body_conf,
            f"{base}/face/positivity": self.pub_face_pos,
            f"{base}/voice/warmth": self.pub_warmth,
            f"{base}/voice/pitch_variety": self.pub_pitch,
            f"{base}/top_styles": self.pub_top_styles,
        }
        self._always = frozenset(f"{base}/{suffix}" for suffix in ALWAYS_TOPICS)
        self._missing_topics: set = set()

        # The sentinel comes off the generated class, so this node and the interface
        # cannot disagree about what "not reported" means - and the number itself is
        # spelled in exactly one place in the repository, `Context.msg`.
        self._not_reported = float(Context.NOT_REPORTED)
        drift = [name for name, wire, mirror in _WIRE_FLAGS if int(wire) != int(mirror)]
        if drift:
            self.get_logger().error(
                f"emori_msgs/Context and hri_context_analyzer.contracts disagree about "
                f"{', '.join(drift)}: this container's emori_msgs was built from a "
                "different Context.msg, and flag-gated channels will gate on the wrong "
                "bit - rebuild the workspace")

        # The REP-155 standard person topic, not a /context extension: this is the one
        # the wider ROS4HRI ecosystem already knows how to read.
        expression_topic = f"{person}/expression"
        self.pub_expression = self.create_publisher(Expression, expression_topic, 10)
        self._expression_topic = expression_topic
        # Which branch of `set_expression_field` this container's hri_msgs will take,
        # recorded ONCE, at start: the Expression field layout differs between hri_msgs
        # builds, and a node that discovers the answer and keeps it to itself is no
        # better off.
        self._expression_shape = expression_field_shape(Expression())
        self._expression_drops: set = set()

        self.get_logger().info(f"context analyzer fanning out {base} -> /context/<key> "
                               f"and {self._expression_topic} "
                               f"(hri_msgs/Expression field: {self._expression_shape})")
        if self._expression_shape == "absent":
            self.get_logger().warn(
                f"the installed hri_msgs/Expression has no `expression` field: "
                f"{self._expression_topic} is advertised but will never publish - "
                "`ros2 interface show hri_msgs/msg/Expression` in this container")

    # --- publishing ---------------------------------------------------------
    def _publish_item(self, item: FanOutItem) -> None:
        """Put one planned item on its topic, in the std_msgs wrapper its kind names."""

        publisher = self._pubs.get(item.topic)
        if publisher is None:
            # Unreachable while the table above is built from the publishers themselves;
            # said out loud anyway, because the alternative failure is a channel that
            # decided to publish and simply did not.
            if item.topic not in self._missing_topics:
                self._missing_topics.add(item.topic)
                self.get_logger().error(
                    f"the fan-out planned {item.topic}, which this node never "
                    "advertised: nothing will ever arrive on it")
            return
        if item.kind == "float":
            publisher.publish(Float32(data=float(item.value)))
        elif item.kind == "bool":
            publisher.publish(Bool(data=bool(item.value)))
        else:
            publisher.publish(String(data=str(item.value)))

    def _on_context(self, msg: Context) -> None:
        flags = int(msg.report_flags)

        # The nine topics that are computed every cycle and are therefore published
        # every cycle, one visible statement each. Spelled out rather than looped
        # because `tests/test_ros_contracts.py` reads THIS function with `ast` to prove
        # that nothing else reaches a publisher unconditionally: a `for` loop hides
        # every publish from that check, and the guarantee that a cleared flag means
        # silence would then rest on nobody looking. `contracts.ALWAYS_TOPICS` names the
        # same nine, and the loop below skips them so each still goes out exactly once.
        self.pub_esc.publish(Float32(data=float(msg.escalation)))
        self.pub_rap.publish(Float32(data=float(msg.rapport)))
        self.pub_att.publish(Float32(data=float(msg.attention)))
        self.pub_trend.publish(Float32(data=float(msg.engagement_trend)))
        self.pub_style.publish(String(data=str(msg.dominant_style)))
        self.pub_eng.publish(Float32(data=float(msg.engagement_score)))
        self.pub_val.publish(Float32(data=float(msg.valence)))
        self.pub_aro.publish(Float32(data=float(msg.arousal)))
        # `degraded` is the one topic that is MORE useful the emptier the cycle is, so
        # it is published unconditionally: silence here would be indistinguishable from
        # the node being dead, which is the opposite of what a health signal is for.
        self.pub_degraded.publish(Bool(data=flags == 0))

        # Everything else is a judgement about whether this cycle measured anything, and
        # that judgement is made in the pure half where it can be tested without ROS.
        for item in plan_fan_out(msg, base=self._base, not_reported=self._not_reported):
            if item.topic not in self._always:
                self._publish_item(item)

        if should_publish_expression(flags):
            self._publish_expression(msg)

    def _publish_expression(self, msg: Context) -> None:
        """Republish the discrete affect label on the standard ROS4HRI person topic."""

        expression = Expression()
        if hasattr(expression, "header"):
            expression.header.stamp = msg.header.stamp
            expression.header.frame_id = msg.header.frame_id
        reason = set_expression_field(expression, msg.expression)
        if reason:
            self._warn_expression_drop(msg.expression, reason)
            return
        if hasattr(expression, "confidence"):
            expression.confidence = float(msg.expression_confidence)
        self.pub_expression.publish(expression)

    def _warn_expression_drop(self, label: str, reason: str) -> None:
        """Say ONCE, per (label, reason), that this label never reaches the topic.

        Once and not every cycle: the failure is CONSTANT, not intermittent - the
        installed message either has the constant or it never will - and a warning
        repeated every ~2 s is a warning that gets scrolled past. Keyed by the pair, so
        a release that maps six labels and drops the seventh still says so the first
        time that seventh one is derived.
        """

        key = ((label or "").strip().lower(), reason)
        if key in self._expression_drops:
            return
        self._expression_drops.add(key)
        self.get_logger().warn(
            f"expression {key[0] or '(empty)'!r} dropped: "
            f"{EXPRESSION_DROP_REASONS.get(reason, reason)}; "
            f"the installed hri_msgs/Expression field is {self._expression_shape}, and "
            f"{self._expression_topic} will never carry this label")


def main() -> None:
    rclpy.init()
    node = ContextAnalyzerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
