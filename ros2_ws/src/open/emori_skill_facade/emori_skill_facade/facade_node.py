"""``skill_facade`` - the ONLY ROS component allowed to turn a decision into motion.

The engagement node decides and publishes bounded skill requests; nothing else in the
graph may touch a robot.  This node is the single place those requests become movement,
and it does that through a *transport* (``transport.py``) rather than by knowing any
wire format itself - today the hostapp's ``/api/skill``, never the Reachy daemon.  That
one indirection is what keeps the single-writer rule intact with ROS in the picture: the
hostapp's one ``RobotPlayer`` queue keeps its generation guards, its ``drop_if_busy``
interlock, the ``busy`` gate the push-turn detector needs, and the add-on ownership
check.  A node POSTing the daemon directly would bypass all four at once.

It serves:

* ``/skill/replay_motion`` (``emori_msgs/action/ReplayMotion``) - a bounded gesture name;
* ``/skill/look_at``       (``emori_msgs/action/LookAt``)       - a bounded gaze target;
* ``/skill/set_expression``(``emori_msgs/msg/SetExpression``)   - fire-and-forget state.

``EMORI_ROS_MOTION_OWNER`` is enforced HERE (and, defence in depth, again by the hostapp
intake): only ``ros`` sends.  On the shipped default (``hostapp``) every request is
accepted, counted and logged, and nothing leaves the node - which is what makes turning
the graph on for the first time on a live robot a non-event.

The six-counter ledger is published on ``/diagnostics`` every ``ledger_period_s`` and
logged at INFO every 30 s, so "is the graph moving the robot, and if not, who refused"
is answerable from ``ros2 topic echo /diagnostics`` on a robot with no debugger attached.

This package is deliberately *three* files.  Everything that decides
(``contract.py``) and everything that speaks a wire (``transport.py``) is importable
with no ROS installed at all; only this file touches rclpy, and its ROS imports are
guarded for exactly that reason.  Humble runs on the robot and nowhere else: no dev box
and no CI has it, so a mapping bug would otherwise be discovered by a robot standing in
front of a person.  ``tests/test_skill_facade.py`` exercises the other two off the robot.
"""

from __future__ import annotations

import os
import threading
import time

from emori_skill_facade.contract import (
    DEFAULT_MOTION_OWNER,
    SkillOutcome,
    apply_outcome,
    look_at_body,
    may_post,
    new_counters,
    normalize_owner,
    replay_motion_body,
    set_expression_body,
    suppressed_outcome,
)
from emori_skill_facade.transport import (
    DEFAULT_SKILL_URL,
    DEFAULT_TIMEOUT_S,
    DEFAULT_TRANSPORT,
    TRANSPORTS,
    select_transport,
    url_host,
)

try:  # pragma: no cover - present on the robot's container, never on a dev box
    import rclpy
    from rclpy.action import ActionServer
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node

    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from emori_msgs.action import LookAt, ReplayMotion
    from emori_msgs.msg import SetExpression

    ROS_AVAILABLE = True
except ImportError:  # the pure halves must import on a machine with no ROS at all
    ROS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

# How often the ledger reaches /diagnostics. Five seconds is short enough that a
# rehearsal's last tick lands before teardown and cheap enough to leave on in production.
DEFAULT_LEDGER_PERIOD_S = 5.0
# ...and how often the same numbers reach the container log, where they survive the run.
LEDGER_LOG_PERIOD_S = 30.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class SkillFacadeNode(Node):
    def __init__(self) -> None:
        super().__init__("skill_facade")
        self.declare_parameter("skill_url", os.environ.get("EMORI_SKILL_URL", DEFAULT_SKILL_URL))
        self.declare_parameter("skill_key", os.environ.get("EMORI_SKILL_KEY", ""))
        self.declare_parameter(
            "motion_owner", os.environ.get("EMORI_ROS_MOTION_OWNER", DEFAULT_MOTION_OWNER))
        self.declare_parameter("timeout_s", _env_float("EMORI_SKILL_TIMEOUT_S",
                                                       DEFAULT_TIMEOUT_S))
        self.declare_parameter(
            "transport", os.environ.get("EMORI_SKILL_TRANSPORT", DEFAULT_TRANSPORT))
        self.declare_parameter("ledger_period_s", DEFAULT_LEDGER_PERIOD_S)

        self.skill_url = str(self.get_parameter("skill_url").value).strip() or DEFAULT_SKILL_URL
        self.skill_key = str(self.get_parameter("skill_key").value or "")
        self.motion_owner = normalize_owner(str(self.get_parameter("motion_owner").value))
        self.timeout_s = max(0.1, float(self.get_parameter("timeout_s").value))
        self.ledger_period_s = max(0.5, float(self.get_parameter("ledger_period_s").value))

        self.counters = new_counters()
        self._lock = threading.Lock()
        self._warned_no_key = False
        self._last_unreachable = 0
        self._last_ledger_log = time.monotonic()

        self.transport = self._build_transport(str(self.get_parameter("transport").value))
        self.transport_name = self.transport.name
        self.url_host = url_host(self.skill_url)

        # One group, multithreaded executor: the hop is short but it is I/O, and an
        # action callback must never be able to stall the expression subscription.
        group = ReentrantCallbackGroup()
        self._replay_server = ActionServer(
            self, ReplayMotion, "/skill/replay_motion", self._execute_replay,
            callback_group=group)
        self._look_at_server = ActionServer(
            self, LookAt, "/skill/look_at", self._execute_look_at, callback_group=group)
        self.create_subscription(SetExpression, "/skill/set_expression",
                                 self._on_expression, 10, callback_group=group)

        self._diagnostics = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self._ledger_timer = self.create_timer(self.ledger_period_s, self._publish_ledger)

        self.get_logger().info(f"transport: {self.transport.describe()}")
        if may_post(self.motion_owner):
            self.get_logger().info(
                f"motion_owner='ros': every skill request is sent via "
                f"{self.transport_name} - movement originates in this graph")
            if not self.skill_key and self.transport_name == DEFAULT_TRANSPORT:
                self.get_logger().error(
                    "motion_owner='ros' but EMORI_SKILL_KEY is empty: every request will "
                    "be refused 403 bad_key by the hostapp. Set the same secret on both "
                    "sides or the graph moves nothing.")
        else:
            self.get_logger().info(
                f"motion_owner={self.motion_owner!r}: skill requests are accepted, counted "
                "and logged, and NOTHING is sent (the hostapp drives the robot)")

    def _build_transport(self, requested: str):
        """Resolve the ``transport`` parameter, degrading to the one that cannot move.

        ``select_transport`` refuses an unknown name on purpose, and a node cannot pass
        that refusal to anyone: it would either die in a restart loop or, worse, be
        "fixed" by defaulting to the transport that drives a robot.  A misconfigured
        name therefore lands on ``log`` - loud in the log, inert on the robot - which is
        the same fail-closed rule ``normalize_owner`` applies to the owner knob.
        """

        try:
            # `node=self`: a transport that speaks ROS rather than HTTP (v2's
            # `ros_native`) owns its action clients on this node's executor. The
            # built-ins ignore it, so the one factory signature stays the same for all.
            return select_transport(requested, url=self.skill_url, key=self.skill_key,
                                    timeout_s=self.timeout_s, log=self.get_logger(),
                                    node=self)
        except ValueError as error:
            self.get_logger().error(
                f"{error}; falling back to 'log', which moves nothing. Fix "
                "EMORI_SKILL_TRANSPORT (or the launch argument) to one of "
                f"{sorted(TRANSPORTS)}.")
            return select_transport("log", log=self.get_logger())

    def _count(self, outcome: SkillOutcome) -> None:
        with self._lock:
            self.counters[outcome.counter] = self.counters.get(outcome.counter, 0) + 1

    # --- the one hop off this graph ----------------------------------------
    def _submit(self, body: dict, reason: str) -> SkillOutcome:
        """Submit one bounded skill request.  All decisions come from ``contract.py``."""

        # The ROS-side half of the single-writer rule, spelled out AT the point of
        # enforcement rather than one indirection away: `self.motion_owner` was put
        # through `normalize_owner` in __init__, so this comparison is `may_post` by
        # construction, and this line is what an auditor (and tests/test_ros_contracts.py)
        # reads to answer "what stops this node from moving the robot".
        if self.motion_owner != "ros":
            outcome = suppressed_outcome()
            self._count(outcome)
            self.get_logger().debug(f"suppressed {body.get('skill')} ({reason}): "
                                    f"motion_owner={self.motion_owner!r}")
            return outcome
        # Only the HTTP transport authenticates. Under `ros_native` there is no hostapp
        # in the path and no key to be empty, so this line was pure noise on the robot --
        # and noise at WARN level, on a card that is 94% full. Gated exactly as the
        # ERROR in __init__ already is.
        if (not self.skill_key and not self._warned_no_key
                and self.transport_name == DEFAULT_TRANSPORT):
            self._warned_no_key = True
            self.get_logger().warn("EMORI_SKILL_KEY is empty; the hostapp will refuse this")

        with self._lock:
            self.counters["posted"] += 1
        outcome = self.transport.submit(body, reason)
        self._count(outcome)
        return outcome

    # --- the ledger ---------------------------------------------------------
    def _publish_ledger(self) -> None:
        """Six numbers on /diagnostics: what this facade did, and whether it is well.

        The severity is the operator's whole triage: ERROR when this graph owns motion
        but cannot authenticate (nothing will ever move), WARN while the intake is not
        answering (something is moving nothing, but it is a network fault, not a
        misconfiguration), OK otherwise - including for a facade that is deliberately
        suppressing everything, which is the shipped default and not a problem.
        """

        with self._lock:
            counters = dict(self.counters)

        level, message = DiagnosticStatus.OK, f"transport={self.transport_name}"
        if self.motion_owner == "ros" and not self.skill_key \
                and self.transport_name == DEFAULT_TRANSPORT:
            level = DiagnosticStatus.ERROR
            message = ("motion_owner='ros' with an empty EMORI_SKILL_KEY: every request "
                       "is refused 403 bad_key and the robot will not move")
        elif counters["unreachable"] > self._last_unreachable:
            level = DiagnosticStatus.WARN
            message = ("the skill intake did not answer "
                       f"{counters['unreachable'] - self._last_unreachable} request(s) "
                       f"since the last tick ({self.url_host or self.skill_url})")
        self._last_unreachable = counters["unreachable"]

        values = [KeyValue(key=name, value=str(count)) for name, count in counters.items()]
        values.append(KeyValue(key="motion_owner", value=self.motion_owner))
        values.append(KeyValue(key="transport", value=self.transport_name))
        values.append(KeyValue(key="url_host", value=self.url_host))

        # The transport's OWN breakdown: WHICH refusal, not just how many. Without it
        # `refused=19` on the robot was unattributable above DEBUG - the native transport
        # logs the refusal code at debug level, and INFO-level docker logs drop it, so
        # the only way to learn whether nineteen goals were `busy` or `not_owner` was to
        # restart the container with a different log level and hope it happened again.
        #
        # `counters` is a plain method on NativeTransport, so the `callable` branch is
        # the live one; the isinstance branch covers a transport that makes it a
        # property. HostappHttpTransport and LogOnlyTransport have neither and are
        # unaffected.
        breakdown = getattr(self.transport, "counters", None)
        if callable(breakdown):
            try:
                breakdown = breakdown()
            except Exception:  # noqa: BLE001 - a ledger read must not break diagnostics
                breakdown = None
        if isinstance(breakdown, dict):
            values.extend(KeyValue(key=str(key), value=str(value))
                          for key, value in sorted(breakdown.items()))

        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [DiagnosticStatus(
            level=level, name="emori_skill_facade", message=message,
            hardware_id=self.transport_name, values=values)]
        self._diagnostics.publish(array)

        now = time.monotonic()
        if now - self._last_ledger_log >= LEDGER_LOG_PERIOD_S:
            self._last_ledger_log = now
            ledger = " ".join(f"{name}={count}" for name, count in counters.items())
            self.get_logger().info(
                f"ledger owner={self.motion_owner} transport={self.transport_name} {ledger}")

    # --- servers -----------------------------------------------------------
    def _execute_replay(self, goal_handle):
        goal = goal_handle.request
        outcome = self._submit(replay_motion_body(goal.name, goal.intensity), goal.reason)
        goal_handle.succeed()
        return apply_outcome(ReplayMotion.Result(), outcome, done_field="completed")

    def _execute_look_at(self, goal_handle):
        goal = goal_handle.request
        outcome = self._submit(
            look_at_body(goal.yaw_deg, goal.pitch_deg, goal.duration), goal.reason)
        goal_handle.succeed()
        return apply_outcome(LookAt.Result(), outcome, done_field="reached")

    def _on_expression(self, msg: SetExpression) -> None:
        # Fire and forget: an expression is state, not a goal worth a result.
        self._submit(set_expression_body(msg.label, msg.confidence), msg.reason)

    # --- teardown ----------------------------------------------------------
    def destroy_node(self) -> bool:
        """Close the transport before the node, if it has anything to close.

        ``ros_native`` owns a batch worker THREAD that outlives ``rclpy.shutdown()``:
        it is a daemon thread, so the process does exit, but between shutdown and exit
        it can still be waiting on a future that will never complete and - worse - can
        still send a goal to an action client whose node is being destroyed under it.
        ``hostapp_http`` owns an httpx client and ``log`` owns nothing; each knows what
        it has, which is why this asks rather than assumes.
        """

        closer = getattr(getattr(self, "transport", None), "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as error:  # noqa: BLE001 - teardown is best effort
                self.get_logger().warn(f"transport close failed: {error}")
        # `Node` is `object` when rclpy is absent (see the guarded import), which is the
        # only way this file is readable off the robot at all - and the only way this method
        # is testable there.
        parent = getattr(super(), "destroy_node", None)
        return parent() if callable(parent) else True


def main() -> None:
    if not ROS_AVAILABLE:  # pragma: no cover - the guarded import is for tests only
        raise RuntimeError("emori_skill_facade needs ROS 2 (rclpy + emori_msgs + "
                           "diagnostic_msgs); it runs inside the emori-ros2 container, "
                           "never on the host")
    rclpy.init()
    node = SkillFacadeNode()
    executor = MultiThreadedExecutor()
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
