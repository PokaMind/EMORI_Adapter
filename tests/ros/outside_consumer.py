"""An outside consumer of our REP-155 topics: standard messages only, zero EMORI code.

Everything that reads this graph today was written in this repository, against our own
message package, by people who know what the fields mean. That proves the graph works;
it proves nothing about whether it is STANDARD. This script is the control experiment.
It runs in its own container (`deploy/ros2/Dockerfile.consumer`, Humble or Jazzy plus
`hri_msgs`/`tf2_msgs` and nothing else), joins the adapter container's network
namespace so `ROS_LOCALHOST_ONLY=1` still holds, subscribes to the topics a ROS4HRI
consumer is entitled to expect, and writes what it saw as JSON.

Since v2 it also reads the two topics a ROS 2 *robot* is entitled to expect --
`/joint_states` (`sensor_msgs/JointState`) and `/tf` (`tf2_msgs/TFMessage`) -- because
that is the claim v2 makes and v1 could not: the Reachy Mini is not behind an HTTP
daemon any more, it is a node on this graph, and anything that speaks plain ROS can read
its state. A consumer that can plot the nine joint angles and look up base_link -> head
without knowing what EMORI is IS the proof. The v1 topics are unchanged, so the same
script serves both checks and `--v2` only adds assertions on the harness side.

It imports `rclpy`, `std_msgs`, `hri_msgs`, `sensor_msgs`, `tf2_msgs` and the standard
library. Nothing else, and all five are ROS standards. If it ever needed one line of
`emori_adapter` or one field of `emori_msgs`, the interface would not be standard and
this file would be a very expensive way of not noticing.

`/humans/persons/<id>/context` therefore appears in `topics_seen` carrying a type this
container cannot decode. That is RECORDED (`context_decodable: false`) rather than
hidden: the EMORI extension really is outside REP-155, and an honest boundary report
says where the standard stops.

Two things that look like details and are not:

* **QoS.** `tracked` and `anonymous` are published transient_local / depth 1 /
  reliable. A subscriber that takes the default volatile profile matches no publisher
  at all and reports an empty graph that looks exactly like a dead robot.
* **Vocabulary.** The expression labels are read off the installed `Expression` class
  at runtime. Hardcoding the seven labels EMORI happens to use would make this script
  agree with EMORI instead of with the interface, which is the one thing it is for.

    python3 outside_consumer.py --person-id learner_01 --seconds 30 --out /out/report.json
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from hri_msgs.msg import EngagementLevel, Expression, IdsList
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from tf2_msgs.msg import TFMessage

# Must match the publisher exactly (docs/ros4hri-mapping.md): a mismatch here is
# silence, not an error.
_LATCHED = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                      reliability=ReliabilityPolicy.RELIABLE)
_VOLATILE_DEPTH = 10
# /joint_states arrives at ~50 Hz. A depth-10 queue at that rate holds 0.2 s, and this
# node is spun with a 0.2 s timeout, so a queue that small drops samples and the measured
# rate reads low for reasons that have nothing to do with the publisher. Deep enough to
# cover a whole spin gap, shallow enough not to be a buffer of stale state.
_STREAM_DEPTH = 100


def message_constants(message_class: type) -> dict[str, object]:
    """The upper-case constants a generated message class declares about itself.

    rosidl puts interface constants into the class namespace (through the metaclass'
    ``__prepare__``), so this is the installed interface stating its own vocabulary
    instead of us telling it what we assume it contains. ``SLOT_TYPES`` and friends are
    filtered out by type: only strings and integers are constants here.
    """

    constants: dict[str, object] = {}
    for name in dir(message_class):
        if name.startswith("_") or not name.isupper():
            continue
        value = getattr(message_class, name, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)):
            constants[name] = value
    return constants


class OutsideConsumer(Node):
    """Counts what arrives on the four standard topics, judges nothing."""

    def __init__(self, person_id: str) -> None:
        super().__init__("outside_consumer")

        constants = message_constants(Expression)
        self.vocabulary = sorted({v for v in constants.values() if isinstance(v, str)})
        # hri_msgs 2.1.0 carries `Expression.expression` as a STRING; older releases use
        # a uint8 with the same constant names. Keep the numeric map so a numeric
        # release reports real labels rather than a page of "unknown".
        self._numeric_labels = {int(v): k.lower() for k, v in constants.items()
                                if isinstance(v, int)}
        self.levels = {k: int(v) for k, v in message_constants(EngagementLevel).items()
                       if isinstance(v, int)}

        self.tracked_ids: list[str] = []
        self.anonymous: bool | None = None
        self.engagement_counts: Counter[int] = Counter()
        self.invalid_levels = 0
        self.expressions: Counter[str] = Counter()
        self.unknown_expression_labels: list[str] = []

        # v2: the robot itself. These are plain ROS, not ROS4HRI -- which is the point.
        self.joint_state_count = 0
        self.joint_names: list[str] = []
        self.tf_frames: list[str] = []
        # Wall-clock bracket around the JointState stream. The rate is computed from the
        # FIRST and LAST message this node saw rather than from the listening window,
        # because the window includes however long discovery took and that would report
        # a 50 Hz publisher as 35 Hz on a slow start.
        self._first_joint_state: float | None = None
        self._last_joint_state: float | None = None

        base = f"/humans/persons/{person_id}"
        self.create_subscription(IdsList, "/humans/persons/tracked", self._on_tracked,
                                 _LATCHED)
        self.create_subscription(Bool, f"{base}/anonymous", self._on_anonymous, _LATCHED)
        self.create_subscription(EngagementLevel, f"{base}/engagement_status",
                                 self._on_engagement, _VOLATILE_DEPTH)
        self.create_subscription(Expression, f"{base}/expression", self._on_expression,
                                 _VOLATILE_DEPTH)
        self.create_subscription(JointState, "/joint_states", self._on_joint_state,
                                 _STREAM_DEPTH)
        self.create_subscription(TFMessage, "/tf", self._on_tf, _STREAM_DEPTH)

    # --- callbacks ----------------------------------------------------------
    def _on_tracked(self, msg: IdsList) -> None:
        self.tracked_ids = [str(i) for i in msg.ids]

    def _on_anonymous(self, msg: Bool) -> None:
        self.anonymous = bool(msg.data)

    def _on_engagement(self, msg: EngagementLevel) -> None:
        level = int(msg.level)
        self.engagement_counts[level] += 1
        # "Invalid" against the constants THIS release declares, not against a range
        # this script remembers from the REP.
        if level not in set(self.levels.values()):
            self.invalid_levels += 1

    def _on_expression(self, msg: Expression) -> None:
        raw = msg.expression
        label = raw if isinstance(raw, str) else self._numeric_labels.get(
            int(raw), f"<{int(raw)}>")
        self.expressions[label] += 1
        if label not in self.vocabulary and label not in self.unknown_expression_labels:
            self.unknown_expression_labels.append(label)

    def _on_joint_state(self, msg: JointState) -> None:
        now = time.monotonic()
        if self._first_joint_state is None:
            self._first_joint_state = now
            # Recorded from the FIRST message only: the names are a fixed contract, and
            # appending every message's list would be 1500 copies of the same nine
            # strings. A publisher that changes them mid-run is a different bug, and the
            # count below is what would show it.
            self.joint_names = [str(name) for name in msg.name]
        self._last_joint_state = now
        self.joint_state_count += 1

    def _on_tf(self, msg: TFMessage) -> None:
        for transform in msg.transforms:
            edge = f"{transform.header.frame_id}->{transform.child_frame_id}"
            if edge not in self.tf_frames:
                self.tf_frames.append(edge)

    def joint_state_hz(self) -> float:
        """Messages per second over the bracket they actually arrived in.

        0.0 with fewer than two messages: one sample has no rate, and reporting the
        window's reciprocal would turn "the topic is dead" into a small positive number.
        """

        if (self.joint_state_count < 2 or self._first_joint_state is None
                or self._last_joint_state is None):
            return 0.0
        span = self._last_joint_state - self._first_joint_state
        if span <= 0.0:
            return 0.0
        return (self.joint_state_count - 1) / span

    # --- report -------------------------------------------------------------
    def report(self, *, person_id: str, seconds: float) -> dict:
        """Everything observed, plus the boundary this container could not cross."""

        topics = {name: sorted(types)
                  for name, types in sorted(self.get_topic_names_and_types())}
        return {
            "person_id": person_id,
            "seconds": seconds,
            "tracked_ids": list(self.tracked_ids),
            "anonymous": self.anonymous,
            # JSON keys are strings whatever we do here, so say so explicitly rather
            # than letting json.dumps quietly decide.
            "engagement_counts": {str(k): v
                                  for k, v in sorted(self.engagement_counts.items())},
            "invalid_levels": self.invalid_levels,
            "expressions": dict(sorted(self.expressions.items())),
            "unknown_expression_labels": list(self.unknown_expression_labels),
            "expression_vocabulary": list(self.vocabulary),
            "engagement_constants": dict(sorted(self.levels.items())),
            # v2. Empty/zero against a v1 graph, which publishes neither -- that is a
            # recorded difference between the two architectures, not a failure, and the
            # harness only asserts on them with --v2.
            "joint_state_count": self.joint_state_count,
            "joint_names": list(self.joint_names),
            "joint_state_hz": round(self.joint_state_hz(), 2),
            "tf_frames": sorted(self.tf_frames),
            "topics_seen": topics,
            # The honest boundary: emori_msgs is not installed here, so the EMORI
            # extension topic is visible by name and type and unreadable by content.
            "context_decodable": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read EMORI's REP-155 topics with hri_msgs only and report.")
    parser.add_argument("--person-id", default="learner_01")
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="how long to listen before writing the report")
    parser.add_argument("--out", default="/out/report.json")
    args = parser.parse_args()

    rclpy.init()
    node = OutsideConsumer(args.person_id)
    try:
        # A deadline loop, not `rclpy.spin`: this container's whole job is to listen for
        # a bounded window and then write one file, and `spin` never returns. The 0.2 s
        # timeout keeps the loop responsive to the deadline without busy-waiting.
        deadline = time.monotonic() + max(0.0, args.seconds)
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
        # Taken while the node is still alive: the graph query needs it.
        report = node.report(person_id=args.person_id, seconds=args.seconds)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()

    text = json.dumps(report, indent=2, sort_keys=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    # Also to stdout, so `docker logs` carries the evidence even if the bind mount was
    # wrong -- which is the failure mode that otherwise looks like "it saw nothing".
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
