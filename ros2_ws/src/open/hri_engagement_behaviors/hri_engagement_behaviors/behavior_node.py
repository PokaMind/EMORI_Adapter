"""Engagement-behaviour node - the ROS engagement AUTHORITY.

Consumes ROS4HRI signals and *decides*; it never moves the robot itself:

* subscribes ``/humans/persons/<id>/engagement_status`` (hri_msgs/EngagementLevel)
  - the standard REP-155 topic (logged, demonstrates standard consumption);
* subscribes ``/humans/persons/<id>/context`` (emori_msgs/Context) - the rich
  per-cycle signal it maps to behaviour through :mod:`hri_engagement_behaviors.policy`;
* subscribes ``/humans/persons/<id>/speech_cue`` (emori_msgs/SpeechCue) - the
  per-sentence expression cue the adapter bridge republishes at sentence onset;
* publishes ``/skill/set_expression`` (emori_msgs/SetExpression) and sends
  ``/skill/replay_motion`` / ``/skill/look_at`` goals - **bounded skill requests**;
* publishes ``diagnostic_msgs/DiagnosticArray`` on ``/diagnostics`` every 5 s, so the
  ledger this lane keeps is readable from outside the container without parsing logs.

This file is deliberately thin.  Every judgement - the ladder, the speech arbitration,
the de-escalation preempt, when an expression is worth a request - lives in
:mod:`hri_engagement_behaviors.policy`, which is stdlib-only and imports nothing from
``emori_adapter``: this package must build and run with no EMORI core installed,
and a decision that can only be exercised inside a ROS container is a decision nobody
tests.  What is left here is parameters, subscriptions, action clients and counters.

The single-writer rule is why this file holds no daemon URL at all.  Everything decided
here leaves as ONE bounded skill request per event, and ``emori_skill_facade`` is the
only ROS component allowed to turn one into robot motion - which it does by POSTing the
hostapp's ``/api/skill``, never the daemon, so the hostapp's single ``RobotPlayer``
queue (generation guards, ``drop_if_busy``, the ``busy`` gesture interlock, push_turn
handling) keeps every one of its traps.  "One per event" is load-bearing: that queue
holds 12 slots and refuses a batch while two are already waiting, so a node that sent
three requests a cycle was not more expressive, it was lossy.

``EMORI_ROS_MOTION_OWNER`` (hostapp | ros | none, default ``hostapp``) decides whether
those requests are executed.  It is enforced on each side of the boundary exactly once
- the facade will not POST, and the hostapp intake answers 409 ``not_owner`` - so this
node always computes and always publishes; the parameter is read here only to say so
out loud at startup and in the ledger, because a graph that looks alive while nothing
moves is the failure mode that wastes an afternoon on the robot.
"""

from __future__ import annotations

import os
from pathlib import Path

from hri_engagement_behaviors.policy import (
    SKILL_MOTIONS,
    CycleSignals,
    Decision,
    EngagementPolicy,
    PolicyConfig,
    SkillRequest,
    SpeechCueSignals,
)

# These are deliberately NOT behind a `try: ... except ImportError` guard the way the
# facade's are.  There is no pure half left in this file to protect - `policy.py` is the
# pure half and it imports none of this - so a guard would buy nothing, and it would cost
# something real: `tests/test_ros_contracts.py` finds the emori_msgs symbols the graph
# depends on with a column-0 regex, and this node is the graph's only unguarded importer
# of `LookAt`, `ReplayMotion` and `SetExpression`.  Indenting these four lines would
# silently retire the check that exists because exactly that import broke on the robot.
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node

# diagnostic_msgs ships with ros-base, so the ledger costs the image nothing.
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from hri_msgs.msg import EngagementLevel as EngagementLevelMsg
from emori_msgs.action import LookAt, ReplayMotion
from emori_msgs.msg import Context, SetExpression, SpeechCue

# How often the ledger goes on /diagnostics, and how often it is also said in the log.
# Five seconds is short enough that a 60 s rehearsal sees several ticks; thirty keeps
# `docker logs` readable over a whole session.
DIAGNOSTICS_PERIOD_S = 5.0
LEDGER_LOG_PERIOD_S = 30.0


class EngagementBehaviorNode(Node):
    def __init__(self) -> None:
        super().__init__("hri_engagement_behaviors")
        self.declare_parameter("person_id", "learner_01")
        # Declared for launch-file compatibility only: the ROS lane asks for bounded
        # skills by name and /api/skill has no sound skill, so a backchannel can only
        # be produced by the hostapp's own engine.
        self.declare_parameter("backchannel_sound", "")
        # Path to configs/behavior.yaml (mounted/copied into the container) so the
        # documented tuning file reaches the ROS engine too. Empty = defaults.
        self.declare_parameter("behavior_yaml", os.environ.get("EMORI_BEHAVIOR_CONFIG", ""))
        # hostapp | ros | none. Read for the startup line and the ledger only - the
        # facade enforces it.
        self.declare_parameter(
            "motion_owner", os.environ.get("EMORI_ROS_MOTION_OWNER", "hostapp"))
        self.person_id = self.get_parameter("person_id").value
        backchannel = str(self.get_parameter("backchannel_sound").value or "").strip()
        behavior_yaml = str(self.get_parameter("behavior_yaml").value or "").strip()
        self.motion_owner = str(self.get_parameter("motion_owner").value).strip().lower()

        self.config = self._load_config(behavior_yaml)
        self.policy = EngagementPolicy(self.config, clock=self._now)
        self.counters: dict[str, int] = {"sent": 0, "expressions": 0, "dropped": 0}
        self._warned: set[str] = set()

        base = f"/humans/persons/{self.person_id}"
        self.expression_pub = self.create_publisher(SetExpression, "/skill/set_expression", 10)
        self.diagnostics_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self.replay_client = ActionClient(self, ReplayMotion, "/skill/replay_motion")
        self.look_at_client = ActionClient(self, LookAt, "/skill/look_at")

        self.create_subscription(EngagementLevelMsg, f"{base}/engagement_status",
                                 self._on_engagement, 10)
        self.create_subscription(Context, f"{base}/context", self._on_context, 10)
        self.create_subscription(SpeechCue, f"{base}/speech_cue", self._on_speech_cue, 10)

        self.create_timer(DIAGNOSTICS_PERIOD_S, self._publish_diagnostics)
        self.create_timer(LEDGER_LOG_PERIOD_S, self._log_ledger)

        if backchannel:
            self.get_logger().info(
                f"backchannel_sound={backchannel!r} is ignored on the ROS lane: /api/skill "
                "exposes no sound skill, so audio backchannels stay with the hostapp engine")
        if self.motion_owner != "ros":
            self.get_logger().info(
                f"motion_owner={self.motion_owner!r}: skill requests are computed and "
                "published, and the facade will NOT execute them (the hostapp drives)")
        else:
            self.get_logger().info(
                "motion_owner='ros': the facade POSTs every request to the hostapp "
                "/api/skill - movement originates in this graph")
        self.get_logger().info(f"engagement behaviours active for '{self.person_id}'")

    # --- configuration ------------------------------------------------------
    def _load_config(self, behavior_yaml: str) -> PolicyConfig:
        """Resolve the effective tuning, and SAY it - both halves matter.

        The bug this replaces was silent: ``EMORI_BEHAVIOR_CONFIG`` never reached the
        container, the node fell back to code defaults that happened to equal the shipped
        file, and nothing in the logs could tell a loaded file from a missing one.  A
        path that does not resolve is an ERROR (someone meant to tune this run), and the
        effective numbers are printed on every start whether or not a file was read.
        """

        config = PolicyConfig()
        if behavior_yaml:
            path = Path(behavior_yaml)
            if not path.is_file():
                self.get_logger().error(
                    f"behavior_yaml={behavior_yaml!r} is not a readable file (missing, or a "
                    "directory): the tuning file was NOT loaded and the built-in defaults "
                    "are in force for this run")
            else:
                try:
                    config = PolicyConfig.from_yaml(behavior_yaml)
                except Exception as exc:  # malformed YAML must not take the graph down
                    self.get_logger().error(
                        f"behavior_yaml={behavior_yaml!r} could not be parsed ({exc}): the "
                        "built-in defaults are in force for this run")
                    config = PolicyConfig()
        config = config.with_env(os.environ)
        self.get_logger().info(
            f"behaviour policy source={config.source} "
            f"deescalate_at={config.deescalate_at:.2f} "
            f"recovery_cooldown_cycles={config.recovery_cooldown_cycles} "
            f"nod_when_engaged_every={config.nod_when_engaged_every} "
            f"cue_max_s={config.cue_max_s:.2f} deescalated_k={config.deescalated_k:.2f}")
        return config

    # --- clock --------------------------------------------------------------
    def _now(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    # --- subscriptions ------------------------------------------------------
    def _on_engagement(self, msg: EngagementLevelMsg) -> None:
        # Standard ROS4HRI topic - logged to show REP-155 consumption.
        self.get_logger().debug(f"engagement_status level={msg.level}")

    def _on_context(self, msg: Context) -> None:
        decision = self.policy.on_cycle(CycleSignals(
            seq=int(msg.seq),
            engagement_level=int(msg.engagement_level),
            escalation=float(msg.escalation),
            valence=float(msg.valence),
            arousal=float(msg.arousal),
            note=str(msg.note or ""),
            expression=str(msg.expression or ""),
            expression_confidence=float(msg.expression_confidence),
        ))
        self._dispatch(decision)
        self.get_logger().info(decision.log)

    def _on_speech_cue(self, msg: SpeechCue) -> None:
        """One sentence of assistant speech (or its end marker)."""

        decision = self.policy.on_speech_cue(SpeechCueSignals(
            seq=int(msg.seq),
            cue_category=str(msg.cue_category or ""),
            est_duration=float(msg.est_duration),
            ended=bool(msg.ended),
        ))
        self._dispatch(decision)
        self.get_logger().debug(decision.log)

    # --- bounded skill requests ---------------------------------------------
    def _dispatch(self, decision: Decision) -> None:
        """Turn the policy's (at most one) request into ROS traffic."""

        for request in decision.requests:
            if request.kind == "replay_motion":
                self._send_replay(request)
            elif request.kind == "look_at":
                self._send_look_at(request)
            elif request.kind == "set_expression":
                self._publish_expression(request)
            else:  # unreachable unless the policy grows a kind this node cannot send
                self._count("dropped")
                self._warn_once(f"kind:{request.kind}",
                                f"policy asked for an unknown skill kind {request.kind!r}")

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _warn_once(self, key: str, message: str) -> None:
        """Say a standing problem ONCE at WARNING, then stop repeating it per cycle."""

        if key in self._warned:
            return
        self._warned.add(key)
        self.get_logger().warn(message)

    def _publish_expression(self, request: SkillRequest) -> None:
        msg = SetExpression()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.person_id = self.person_id
        msg.label = request.label
        msg.confidence = float(min(1.0, max(0.0, request.confidence)))
        msg.intensity = float(min(1.0, max(0.0, request.intensity)))
        msg.reason = request.reason
        self.expression_pub.publish(msg)
        self._count("expressions")

    def _send_replay(self, request: SkillRequest) -> None:
        if request.name not in SKILL_MOTIONS:  # defensive: the hostapp would answer 400
            self._count("dropped")
            return
        if not self.replay_client.server_is_ready():
            # Never block the executor waiting for a facade that is not up: a dropped
            # request is one missed gesture, a blocked spin is a dead graph.
            # The FIRST drop is a WARNING, not a debug line: "every skill request is
            # discarded because the facade is not in this graph" is a dead motion chain,
            # and counting it quietly is how it stays invisible for an afternoon.
            self._count("dropped")
            self._warn_once("replay_motion",
                            "no /skill/replay_motion server (is emori_skill_facade running?) "
                            f"- dropping {request.name} and every skill request until it appears")
            return
        goal = ReplayMotion.Goal()
        goal.name = request.name
        goal.intensity = float(min(1.0, max(0.0, request.intensity)))
        goal.reason = request.reason
        self._count("sent")
        self._track(self.replay_client.send_goal_async(goal))

    def _send_look_at(self, request: SkillRequest) -> None:
        if not self.look_at_client.server_is_ready():
            self._count("dropped")
            self._warn_once("look_at", "no /skill/look_at server (is emori_skill_facade "
                                       "running?) - dropping the gaze request")
            return
        goal = LookAt.Goal()
        # The /api/skill ranges; out-of-range is a 400, so clamp here.
        goal.yaw_deg = float(min(30.0, max(-30.0, request.yaw_deg)))
        goal.pitch_deg = float(min(25.0, max(-25.0, request.pitch_deg)))
        goal.duration = float(min(1.5, max(0.2, request.duration)))
        goal.reason = request.reason
        self._count("sent")
        self._track(self.look_at_client.send_goal_async(goal))

    # --- what the facade answered -------------------------------------------
    #
    # A goal that is accepted and then refused by the hostapp (429 rate_limit, 409
    # legacy_motion_disabled, 423 head_reserved) succeeds as an action and carries the
    # reason in `error_code`. Without this callback that refusal is invisible from the
    # ROS side, and "the robot did not move" has two indistinguishable explanations.

    def _track(self, send_future) -> None:
        send_future.add_done_callback(self._on_goal_response)

    def _on_goal_response(self, future) -> None:
        try:
            handle = future.result()
        except Exception as exc:  # the facade died mid-goal; do not take the node with it
            self._count("result_exception")
            self._warn_once("goal_response", f"skill goal was never answered: {exc}")
            return
        if handle is None or not getattr(handle, "accepted", False):
            self._count("result_rejected")
            return
        handle.get_result_async().add_done_callback(self._on_goal_result)

    def _on_goal_result(self, future) -> None:
        try:
            result = future.result().result
        except Exception as exc:
            self._count("result_exception")
            self._warn_once("goal_result", f"skill result was never delivered: {exc}")
            return
        code = str(getattr(result, "error_code", "") or "").strip()
        self._count(f"result_{code or 'ok'}")

    # --- the ledger ----------------------------------------------------------
    def ledger(self) -> dict[str, str]:
        """Everything this lane knows about itself, as flat strings for KeyValue."""

        values = {key: str(value) for key, value in self.policy.counters.items()}
        values.update({key: str(value) for key, value in self.counters.items()})
        values["motion_owner"] = self.motion_owner
        values["config_source"] = self.config.source
        return values

    def _publish_diagnostics(self) -> None:
        status = DiagnosticStatus()
        status.level = DiagnosticStatus.OK
        status.name = "hri_engagement_behaviors"
        status.message = f"owner={self.motion_owner} config={self.config.source}"
        status.hardware_id = str(self.person_id)
        status.values = [KeyValue(key=key, value=value)
                         for key, value in sorted(self.ledger().items())]
        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [status]
        self.diagnostics_pub.publish(array)

    def _log_ledger(self) -> None:
        self.get_logger().info(
            "ledger " + " ".join(f"{key}={value}" for key, value in sorted(self.ledger().items())))


def main() -> None:
    rclpy.init()
    node = EngagementBehaviorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
