"""``/emori/motion``: the motion player's state, and a gated way to hand it a batch.

**Nothing here is on the path a session's motion takes.** That path is a sentence reaching
``POST /api/cycle``, the cue scheduler planning it, and the player submitting it to the
robot. This node reads that path's state and is read by nobody on it:

* **The state tick** runs at 5 Hz on the facade executor. It reads four values the core
  already holds: the player's busy flag, the length of its queue, the driver's motion gate
  and the number of times the gate was closed or reopened. None of them is read through a
  lock: the busy flag is an ``Event`` that is only ever tested, the queue's length is the
  length of its deque, and the gate is a bool and an int that are each written whole. The
  driver's own properties for the gate take a lock the submit path takes, so they are not
  used here; the tick takes no lock of the player's or the driver's, ever. A test holds both
  locks from another thread and requires the tick to finish.
* **The plan events** go out on the same tick. A sentence's plan is reported by the driver and
  the player through one non-blocking put on a bounded queue (``emori_adapter.plan_events``),
  and the tick drains that queue, so they take no wake-up and no thread of their own. The
  tick is the only reader of the queue, and the drain takes no lock.
* **The batch service** is refused outright, before it reads anything, unless the hub was
  started with ``EMORI_HUB_MOTION_SUBMIT=1``, and it is refused for as long as a training
  session is open. A session's motion is the cue scheduler's alone, so a batch from the
  graph can never queue in front of a sentence's plan or take the head from it. Past those
  two checks it is the player's own entry, ``LiveDriver.submit_legacy_motion``, with the
  same ownership guard, the same ``yields`` a cycle's motion has and the voice mark the
  player takes for a speech batch.

**What is lost, and where it shows.** A tick that arrives late is a facade being starved
by something else on its executor, and the state it publishes is then late. Nothing about
the motion changes. The late ticks, the values the tick could not read and the batches
refused, by reason, are counted and are on ``/diagnostics`` under ``motion``. So are the plan
events the queue could not hold, those that could not be published and those whose plan was
never named. The robot moves the same either way.

The topics and the service:

=============================  =====================================================
``/emori/motion/player``       ``emori_msgs/PlayerState``, latched, on change. Polled at 5 Hz.
``/emori/motion/plan_events``  ``emori_msgs/MotionPlanEvent``, a sentence's plan: planned,
                               started, ended. Drained at 5 Hz.
``/emori/motion/submit_batch``  ``emori_msgs/SubmitMotionBatch``. The error codes are
                               :data:`ERROR_CODES`.
=============================  =====================================================

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from emori_hub.facade_kit import (
    NODE_NAMESPACE,
    OK,
    WARN,
    Guarded,
    Health,
    NodeBindings,
    TickClock,
    load_node_bindings,
    register_diagnostics,
    stamp_header,
)

__all__ = [
    "ERROR_CODES",
    "MAX_BATCH",
    "MAX_DURATION_S",
    "MOTION_SUBMIT_VAR",
    "MotionFacade",
    "NODE_NAME",
    "PLAN_EVENTS_TOPIC",
    "PLAYER_TOPIC",
    "PlayerReader",
    "PlayerSnapshot",
    "STATE_PERIOD_S",
    "SUBMIT_SERVICE",
    "build_motion_node",
    "fill_plan_event",
    "intent_from_message",
    "load_motion_bindings",
    "motion_submit_enabled",
]

log = logging.getLogger("emori.hub.motion")

NODE_NAME = "motion"
PLAYER_TOPIC = "/emori/motion/player"
PLAN_EVENTS_TOPIC = "/emori/motion/plan_events"
SUBMIT_SERVICE = "/emori/motion/submit_batch"

MOTION_SUBMIT_VAR = "EMORI_HUB_MOTION_SUBMIT"
_TRUE = frozenset({"1", "true", "yes", "on"})

#: 5 Hz.
STATE_PERIOD_S = 0.2

#: The player holds twelve batches; a batch longer than this is a mistake, not a motion.
MAX_BATCH = 32
#: The longest single move accepted. A recorded move is the longest thing the player waits
#: out, and none of those is longer than this.
MAX_DURATION_S = 30.0
_DEFAULT_INTERPOLATION = "minjerk"
_REASON_MAX = 160
#: The kinds a message can describe completely: sound and wobble need fields it has none of.
_KINDS = ("goto", "look_at", "antennas", "set_target", "wake_up", "play_recorded")

ERROR_CODES = (
    "disabled",          # the hub was not started with EMORI_HUB_MOTION_SUBMIT=1
    "session_active",    # a training session is open
    "not_motion_owner",  # another lane owns motion, as /api/skill refuses
    "head_reserved",     # the head is an input device, as /api/skill refuses
    "busy",              # a badge's lease, a running mode, or a person at the landing page
    "no_player",         # this hub has no motion player
    "empty",             # no intents
    "too_many",          # more than MAX_BATCH
    "invalid_intent",    # a kind, a number or a duration that cannot be played
    "motion_disabled",   # the driver's motion gate is closed (an add-on mode owns the robot)
    "refused",           # the player did not take it: its queue is full, or drop_if_busy
    "internal",
)


def motion_submit_enabled(environ: Mapping[str, str]) -> bool:
    """Only a spelled-out yes opens the batch service, so a typo cannot."""

    return str(environ.get(MOTION_SUBMIT_VAR, "")).strip().lower() in _TRUE


# --- reading the player ----------------------------------------------------------------


@dataclass(frozen=True)
class PlayerSnapshot:
    busy: bool = False
    queue_depth: int = 0
    legacy_motion_enabled: bool = True
    generation: int = 0
    motion_owner: str = ""


class PlayerReader:
    """The four values of ``PlayerState``, each read without a lock.

    A value that cannot be read is replaced by its neutral one and counted in
    :attr:`unreadable`, so a rename in the core shows as a count and not as a failed tick.
    A test builds the real player and driver and reads them through this class, which is
    what turns such a rename into a failing test.
    """

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        self.unreadable = 0

    def read(self) -> PlayerSnapshot:
        driver = self._driver
        player = getattr(driver, "player", None)
        return PlayerSnapshot(
            busy=bool(self._get(lambda: player.busy, False)),
            queue_depth=int(self._get(lambda: len(player._q.queue), 0)),
            legacy_motion_enabled=bool(self._get(lambda: driver._legacy_motion_enabled, True)),
            generation=int(self._get(lambda: driver._legacy_motion_generation, 0)),
            motion_owner=str(self._get(lambda: driver.motion_owner, "")),
        )

    def _get(self, read: Callable[[], Any], neutral: Any) -> Any:
        try:
            return read()
        except Exception:  # noqa: BLE001 - counted, and the tick still publishes the rest
            self.unreadable += 1
            return neutral


def fill_player(message: Any, snapshot: PlayerSnapshot) -> Any:
    message.busy = snapshot.busy
    message.queue_depth = max(0, min(255, snapshot.queue_depth))
    message.legacy_motion_enabled = snapshot.legacy_motion_enabled
    message.generation = snapshot.generation & 0xFFFFFFFF
    message.motion_owner = snapshot.motion_owner
    return message


def fill_plan_event(message: Any, step: Any) -> Any:
    """Copy one ``emori_adapter.plan_events.PlanStep`` onto a ``MotionPlanEvent``."""

    message.plan_id = str(step.plan_id)
    message.seq = max(0, step.seq) & 0xFFFFFFFF
    message.family = step.family
    message.slug = step.slug
    message.started = step.started
    message.ended = step.ended
    message.outcome = step.outcome
    message.first_apply_delay_s = float(step.first_apply_delay_s)
    message.family_authored = step.family_authored
    message.family_source = step.family_source
    message.skip_reason = step.skip_reason
    return message


def _plan_reader(driver: Any) -> Any:
    """A reader on the driver's plan events; ``None`` for a driver that has none."""

    stream = getattr(driver, "plan_events", None)
    if stream is None:
        return None
    from emori_adapter.plan_events import PlanEventReader

    return PlanEventReader(stream)


# --- the batch -------------------------------------------------------------------------


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


def intent_from_message(message: Any) -> Any:
    """One ``emori_msgs/MotionIntent`` as the core's ``MotionIntent``. Raises ``ValueError``.

    A ``has_*`` flag off leaves that part uncommanded. A duration of zero or less means the
    model's own default, and an empty interpolation or pace means its default too.
    """

    from emori_adapter.models import HeadPose, MotionIntent

    kind = str(message.kind)
    if kind not in _KINDS:
        raise ValueError(f"kind {kind!r} cannot be submitted")
    duration = _finite(message.duration)
    if duration > MAX_DURATION_S:
        raise ValueError("duration is too long")
    head = None
    if message.has_head:
        head = HeadPose(x=_finite(message.x_mm), y=_finite(message.y_mm), z=_finite(message.z_mm),
                        roll=_finite(message.roll_deg), pitch=_finite(message.pitch_deg),
                        yaw=_finite(message.yaw_deg))
    fields: dict[str, Any] = {
        "kind": kind,
        "head": head,
        "antennas": ((_finite(message.antenna_left_rad), _finite(message.antenna_right_rad))
                     if message.has_antennas else None),
        "body_yaw": _finite(message.body_yaw_rad) if message.has_body_yaw else None,
        "interpolation": str(message.interpolation) or _DEFAULT_INTERPOLATION,
        "move": str(message.move) or None,
        "reason": str(message.reason)[:_REASON_MAX],
        "speech": bool(message.speech),
        "pace": str(message.pace) or "fixed",
        "antenna_voice": bool(message.antenna_voice),
        "tint": str(message.tint),
        "supersede": bool(message.supersede),
    }
    if duration > 0.0:
        fields["duration"] = duration
    elif duration < 0.0:
        raise ValueError("duration is negative")
    if kind == "play_recorded" and not fields["move"]:
        raise ValueError("a recorded move needs a name")
    return MotionIntent(**fields)


class MotionFacade:
    """The state tick and the batch service, with no ROS in them."""

    def __init__(
        self,
        app: Any,
        *,
        publish_player: Callable[[PlayerSnapshot], Any],
        submit_enabled: bool,
        publish_plan_event: Optional[Callable[[Any], Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._publish = publish_player
        self.submit_enabled = submit_enabled
        self.reader = PlayerReader(app.state.driver)
        self._publish_plan_event = publish_plan_event
        self.plans = _plan_reader(app.state.driver) if publish_plan_event is not None else None
        self.plan_events_published = 0
        self.plan_events_failed = 0
        self.guard = Guarded(log, monotonic=monotonic)
        self.clock = TickClock(STATE_PERIOD_S, monotonic=monotonic)
        self._last: Optional[PlayerSnapshot] = None
        self.published = 0
        self.refused: dict[str, int] = {}
        self.accepted = 0

    # --- the state tick ------------------------------------------------------------------

    def tick(self) -> None:
        """Timer body: publish the player's state if it differs from the last published."""

        self.clock.tick()
        self.guard.run("the motion state tick", self._tick, default=None)
        self.guard.run("the plan events", self._drain_plan_events, default=None)

    def _tick(self) -> None:
        snapshot = self.reader.read()
        if snapshot == self._last:
            return
        self._publish(snapshot)
        self._last = snapshot
        self.published += 1

    def _drain_plan_events(self) -> None:
        """What the driver and the player have put since the last tick, in order."""

        if self.plans is None:
            return
        for step in self.plans.drain():
            # One at a time: what is drained is gone, so a failure costs that message only.
            try:
                self._publish_plan_event(step)
            except Exception:  # noqa: BLE001 - counted, and the rest still go out
                self.guard.note_failure("a plan event")
                self.plan_events_failed += 1
            else:
                self.plan_events_published += 1

    # --- the batch service ---------------------------------------------------------------

    def submit_batch(self, request: Any, response: Any) -> Any:
        """Service body. Never raises."""

        code = self.guard.run("the motion batch service", lambda: self._submit(request),
                              default="internal")
        if code:
            self.refused[code] = self.refused.get(code, 0) + 1
        else:
            self.accepted += 1
        response.accepted = not code
        response.error_code = code
        return response

    def _session_open(self) -> bool:
        tracker = getattr(self._app.state.driver, "tracker", None)
        # An unreadable tracker is a session: a batch must not guess its way past that.
        return True if tracker is None else bool(getattr(tracker, "active", True))

    def _submit(self, request: Any) -> str:
        if not self.submit_enabled:
            return "disabled"
        if self._session_open():
            return "session_active"
        driver = self._app.state.driver
        # What /api/skill refuses too: another writer owns the head, or a person's hand does.
        if driver.motion_owner != "hostapp":
            return "not_motion_owner"
        if driver.head_reserved():
            return "head_reserved"
        # And nobody is mid-way to a session: a badge's lease lifts the head reservation for
        # the very moments a person is standing there being greeted.
        glue = self._app.state.glue
        if not glue.robot_idle() or not glue.landing_idle():
            return "busy"
        submit = getattr(driver, "submit_legacy_motion", None)
        if not callable(submit) or getattr(driver, "player", None) is None:
            return "no_player"
        messages = list(request.intents)
        if not messages:
            return "empty"
        if len(messages) > MAX_BATCH:
            return "too_many"
        try:
            intents = [intent_from_message(message) for message in messages]
        except (ValueError, TypeError):
            # The model's own validation error is a ValueError.
            return "invalid_intent"
        if submit(intents, drop_if_busy=bool(request.drop_if_busy), yields=self._yields(driver)):
            return ""
        return "refused" if self.reader.read().legacy_motion_enabled else "motion_disabled"

    @staticmethod
    def _yields(driver: Any) -> Callable[[], bool]:
        """A batch gives way to a session, and to a sentence as a cycle's motion does."""

        from emori_adapter.robot.client import motion_sync_enabled

        tracker = driver.tracker
        cue = getattr(driver, "cue", None)
        if cue is None or not motion_sync_enabled():
            return lambda: bool(tracker.active)
        generation = cue.generation
        return lambda: bool(tracker.active) or cue.generation != generation

    # --- what /diagnostics says ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "ticks": self.clock.ticks,
            "late_ticks": self.clock.late,
            "longest_tick_gap_s": round(self.clock.longest_gap_s, 3),
            "published": self.published,
            "unreadable_values": self.reader.unreadable,
            "callback_errors": self.guard.errors,
            "submit_enabled": self.submit_enabled,
            "batches_accepted": self.accepted,
            "batches_refused": sum(self.refused.values()),
            **{f"refused_{code}": count for code, count in sorted(self.refused.items())},
            "plan_events_published": self.plan_events_published,
            "plan_events_failed": self.plan_events_failed,
            "plan_events_dropped": self.plans.dropped if self.plans is not None else 0,
            "plan_events_unmatched": self.plans.unmatched if self.plans is not None else 0,
            "plan_events_capacity": self.plans.capacity if self.plans is not None else 0,
        }

    def health(self) -> Health:
        stats = self.stats()
        problems = []
        if stats["late_ticks"]:
            problems.append(f"{stats['late_ticks']} late state ticks")
        if stats["unreadable_values"]:
            problems.append(f"{stats['unreadable_values']} values the tick could not read")
        if stats["callback_errors"]:
            problems.append(f"{stats['callback_errors']} callback errors")
        if stats["plan_events_dropped"]:
            problems.append(f"{stats['plan_events_dropped']} plan events the queue could not hold")
        if problems:
            return Health(WARN, "; ".join(problems) + ". Motion is not affected.", stats)
        return Health(OK, "the player's state is published", stats)


# --- the node --------------------------------------------------------------------------


def load_motion_types() -> Mapping[str, Any]:
    from emori_msgs.msg import MotionPlanEvent, PlayerState
    from emori_msgs.srv import SubmitMotionBatch

    return {"MotionPlanEvent": MotionPlanEvent, "PlayerState": PlayerState,
            "SubmitMotionBatch": SubmitMotionBatch}


def load_motion_bindings() -> NodeBindings:
    """Import ``rclpy`` and the interfaces. Raises ``ImportError`` where ROS is absent."""

    return load_node_bindings(load_motion_types)


def build_motion_node(
    app: Any,
    bindings: Optional[NodeBindings] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    """The ``/emori/motion`` node for ``app``, to be added to the facade executor.

    The state tick is on a callback group of its own and the batch service on another, so a
    service that waits does not hold the tick. ``node.facade`` is the :class:`MotionFacade`.
    """

    ros = bindings if bindings is not None else load_motion_bindings()
    source = os.environ if environ is None else environ
    node = ros.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
    state_group, command_group = ros.exclusive_group(), ros.exclusive_group()
    player_type = ros.types["PlayerState"]
    publisher = node.create_publisher(player_type, PLAYER_TOPIC, ros.latched_qos)

    def publish(snapshot: PlayerSnapshot) -> None:
        message = fill_player(player_type(), snapshot)
        stamp_header(node, message.header)
        publisher.publish(message)

    plan_type = ros.types["MotionPlanEvent"]
    plan_publisher = node.create_publisher(plan_type, PLAN_EVENTS_TOPIC, ros.default_qos)

    def publish_plan_event(step: Any) -> None:
        message = fill_plan_event(plan_type(), step)
        stamp_header(node, message.header)
        plan_publisher.publish(message)

    facade = MotionFacade(app, publish_player=publish,
                          submit_enabled=motion_submit_enabled(source),
                          publish_plan_event=publish_plan_event)
    node.create_timer(STATE_PERIOD_S, facade.tick, callback_group=state_group)
    node.create_service(ros.types["SubmitMotionBatch"], SUBMIT_SERVICE, facade.submit_batch,
                        callback_group=command_group)
    register_diagnostics(app, "motion", facade.health)
    node.facade = facade
    return node
