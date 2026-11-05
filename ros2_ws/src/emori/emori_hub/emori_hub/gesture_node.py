"""``/emori/gesture``: what the head-turn sensor recognises and what it is doing.

====================  =========================================  ====================
Interface             Is                                         Read from
====================  =========================================  ====================
``events``            one :class:`GestureEvent` per gesture      the ticker's
                      recognised, in order                       ``on_direction`` hook
                                                                 and ``driver.counters``
``state``             the sensor's mode and what suppresses it,  the ticker's own
                      latched; on change, checked at 5 Hz        predicates
``reset_sensing``     forget both detector baselines             ``GestureTicker.
                                                                 reset_sensing``
``inject_turn``       hand the add-on's hook a signed turn as    ``GestureTicker.
                      if the head had made it                    on_direction``
====================  =========================================  ====================

**The tee.** A head turn reaches the add-on through ``gesture_ticker.on_direction``. This node
replaces that attribute with a function that publishes the :class:`GestureEvent` and then calls
the function it replaced, with the same argument and on the same thread (the ticker's), and
returns what that returns or raises what that raises. A publish that raises is counted and
never stops the hook: the add-on does not depend on the graph. Because the attribute is what is
replaced, whatever else reads it (a route that selects a mode by hand) gets the same behaviour
and the event with it. If the ticker has no hook there is nothing to tee, and none is added: a
hook where there was none would make the ticker act on a turn it ignores today. The tee is taken
off when the node is destroyed, if it is still the one installed.

**The other three kinds** (an end nudge in a session, the legacy begin, the language toggle) are
not passed through a hook. They are seen as the driver's counters rising, which the driver
already keeps, polled at 5 Hz. A gesture is therefore published up to a tick late, and none is
missed: a counter that rose twice in a tick publishes twice.

**Inject** is refused unless ``EMORI_HUB_GESTURE_INJECT`` is on, and while a training session is
running, because a turn then means something else (the end nudge). It does what the ticker does
for a recognised turn short of moving the head: it marks the driver active and calls the hook,
which is the tee, so the event is published as well. It does not recentre the head.

**Where the work runs.** Nothing here is on the path a session's motion takes, and nothing
calls the robot client. The tee runs on the gesture ticker's thread and costs one publish. The
timers read attributes the ticker and the driver already keep and call the predicates the
ticker calls itself every tick. The services run in a group of their own.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from emori_hub.facade_kit import (
    Counters,
    KitBindings,
    env_flag,
    load_kit_bindings,
    release_with,
    stamp,
)

__all__ = [
    "GestureBindings",
    "GestureFacade",
    "INJECT_VAR",
    "NODE_NAME",
    "NODE_NAMESPACE",
    "STATE_PERIOD_S",
    "build_gesture_node",
    "load_gesture_bindings",
]

log = logging.getLogger("emori.hub.gesture")

NODE_NAME = "gesture"
NODE_NAMESPACE = "/emori"
EVENTS_TOPIC = "/emori/gesture/events"
STATE_TOPIC = "/emori/gesture/state"
RESET_SERVICE = "/emori/gesture/reset_sensing"
INJECT_SERVICE = "/emori/gesture/inject_turn"
INJECT_VAR = "EMORI_HUB_GESTURE_INJECT"

STATE_PERIOD_S = 0.2

#: ``driver.counters`` key to the GestureEvent kind it stands for.
_COUNTER_KINDS = (
    ("gesture_end", "KIND_END_NUDGE"),
    ("gesture_begin", "KIND_BEGIN"),
    ("lang_toggle", "KIND_LANG_TOGGLE"),
)


@dataclass(frozen=True)
class GestureBindings:
    """Everything this node takes from ROS, so a test can hand it fakes."""

    kit: KitBindings
    GestureEvent: Any
    GestureState: Any


def load_gesture_bindings() -> GestureBindings:
    """Import ``rclpy`` and the interfaces. Raises ``ImportError`` where ROS is absent."""

    from emori_msgs.msg import GestureEvent, GestureState

    return GestureBindings(kit=load_kit_bindings(), GestureEvent=GestureEvent,
                           GestureState=GestureState)


class GestureFacade:
    """The ``/emori/gesture`` node. Public methods are what its callbacks call."""

    def __init__(
        self,
        app: Any,
        bindings: GestureBindings,
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._ros = bindings
        self._driver = app.state.driver
        self._extension = getattr(app.state, "extension", None)
        self._ticker = getattr(app.state, "gesture_ticker", None)
        self._inject_allowed = env_flag(os.environ if environ is None else environ, INJECT_VAR)
        self.counters = Counters()
        self._last_state: Optional[tuple] = None
        self._seen = {name: self._counter(name) for name, _kind in _COUNTER_KINDS}
        self._tee: Optional[Callable[[Any], Any]] = None
        self._original: Optional[Callable[[Any], Any]] = None
        self._closed = False

        kit = bindings.kit
        node = kit.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
        self.node = node
        state_group = kit.group_exclusive()
        command_group = kit.group_exclusive()
        self._events_pub = node.create_publisher(bindings.GestureEvent, EVENTS_TOPIC,
                                                 kit.qos_events)
        self._state_pub = node.create_publisher(bindings.GestureState, STATE_TOPIC,
                                                kit.qos_latched)
        node.create_service(kit.Trigger, RESET_SERVICE, self._on_reset,
                            callback_group=command_group)
        node.create_service(kit.SetBool, INJECT_SERVICE, self._on_inject,
                            callback_group=command_group)
        # One wake-up for both: each one costs the whole process a walk over the executor's
        # entities with the interpreter's lock held, and what they publish can wait 200 ms.
        self._timers = [node.create_timer(STATE_PERIOD_S, self._tick, callback_group=state_group)]
        self._install_tee()

    # --- stopping --------------------------------------------------------------------

    def close(self) -> None:
        """Take the tee off, if it is still the one installed. Never raises."""

        if self._closed:
            return
        self._closed = True
        ticker, tee = self._ticker, self._tee
        if ticker is not None and tee is not None and ticker.on_direction is tee:
            ticker.on_direction = self._original
        self._tee = None

    def stats(self) -> dict[str, Any]:
        return {**self.counters.snapshot(), "tee_installed": self._tee is not None}

    # --- the tee ---------------------------------------------------------------------

    def _install_tee(self) -> None:
        ticker = self._ticker
        if ticker is None:
            return
        original = getattr(ticker, "on_direction", None)
        if original is None:
            log.info("the gesture ticker has no direction hook; head turns are not published")
            return
        if getattr(original, "emori_tee", False):
            log.warning("the direction hook is already tee'd; not wrapping it again")
            return

        def tee(direction: Any) -> Any:
            self._publish_turn(direction)
            return original(direction)

        tee.emori_tee = True  # type: ignore[attr-defined]
        tee.__wrapped__ = original  # type: ignore[attr-defined]
        self._original, self._tee = original, tee
        ticker.on_direction = tee

    def _publish_turn(self, direction: Any) -> None:
        """Never raises: the hook behind it must run whatever happens here."""

        try:
            name = str(getattr(direction, "value", direction) or "").strip().lower()
            self._publish_event(self._ros.GestureEvent.KIND_HEAD_TURN,
                                name if name in ("left", "right") else "")
        except Exception:  # noqa: BLE001 - see above
            self.counters.complain("turn_publish_errors", "could not publish a head turn")

    def _publish_event(self, kind: int, direction: str = "") -> None:
        message = self._ros.GestureEvent()
        message.kind = kind
        message.direction = direction
        stamp(self.node, message)
        self._events_pub.publish(message)
        self.counters.add("events_published")

    # --- what the driver counts ------------------------------------------------------

    def _counter(self, name: str) -> int:
        try:
            return int(self._driver.counters.get(name, 0))
        except Exception:  # noqa: BLE001
            return 0

    def _tick(self) -> None:
        self._tick_events()
        self._tick_state()

    def _tick_events(self) -> None:
        for name, kind_name in _COUNTER_KINDS:
            now = self._counter(name)
            risen, self._seen[name] = now - self._seen[name], now
            for _ in range(max(0, risen)):
                try:
                    self._publish_event(getattr(self._ros.GestureEvent, kind_name))
                except Exception:  # noqa: BLE001 - counted; the next tick goes on
                    self.counters.complain("event_publish_errors",
                                           f"could not publish a {name} event")

    # --- state -----------------------------------------------------------------------

    def sensor_state(self) -> dict[str, Any]:
        """The sensor's state as plain values, from what the ticker already keeps."""

        ticker = self._ticker
        if ticker is None:
            return {"mode": "off", "head_reserved": False, "residual_live": False,
                    "head_held": False, "robot_moving": False, "moving_reason": "",
                    "selection_reserved": self._selection_reserved()}
        player = getattr(self._driver, "player", None)
        moving = bool(getattr(player, "busy", False))
        push_turn = getattr(ticker, "_push_turn", None)
        if push_turn is None:
            from emori_adapter.hostapp.driver import push_turn_gesture_mode

            push_turn = push_turn_gesture_mode()
        return {
            "mode": "push_turn" if push_turn else "back_drive",
            "head_reserved": bool(ticker.head_reserved_for_sensing()),
            "residual_live": bool(ticker.residual_sensing_live()),
            "head_held": bool(ticker.head_held()),
            "robot_moving": moving,
            "moving_reason": "player.busy" if moving else "",
            "selection_reserved": self._selection_reserved(),
        }

    def _selection_reserved(self) -> bool:
        if self._extension is None:
            return False
        try:
            return bool(self._extension.selection_reserved())
        except Exception:  # noqa: BLE001 - an add-on that cannot say is reserving
            return True

    def _tick_state(self) -> None:
        try:
            state = self.sensor_state()
        except Exception:  # noqa: BLE001 - the last published state stands
            self.counters.complain("state_read_errors", "could not read the gesture state")
            return
        key = tuple(state.values())
        if key == self._last_state:
            return
        message = self._ros.GestureState()
        for name, value in state.items():
            setattr(message, name, value)
        stamp(self.node, message)
        try:
            self._state_pub.publish(message)
        except Exception:  # noqa: BLE001 - tried again on the next change
            self.counters.complain("state_publish_errors", "could not publish the gesture state")
            return
        self._last_state = key

    # --- commands --------------------------------------------------------------------

    def reset_sensing(self) -> tuple[bool, str]:
        ticker = self._ticker
        if ticker is None:
            return False, "no_gesture_sensor"
        ticker.reset_sensing()
        return True, ""

    def inject_turn(self, left: bool) -> tuple[bool, str]:
        """Hand the hook a turn. ``(accepted, message)``; the message is a code when refused."""

        if not self._inject_allowed:
            return False, "inject_disabled"
        ticker = self._ticker
        if ticker is None:
            return False, "no_gesture_sensor"
        tracker = getattr(self._driver, "tracker", None)
        if tracker is not None and tracker.active:
            return False, "session_active"
        # As the sensor's own path: a turn cannot select a mode during a badge's lease.
        reserved = getattr(ticker, "_training_reserved", None)
        if callable(reserved) and reserved():
            return False, "training_reserved"
        hook = ticker.on_direction
        if hook is None:
            return False, "no_direction_hook"
        from emori_adapter.hostapp.gestures import GestureDirection

        self._driver.touch()
        hook(GestureDirection.LEFT if left else GestureDirection.RIGHT)
        self.counters.add("injected_turns")
        return True, ""

    def _on_reset(self, _request: Any, response: Any) -> Any:
        try:
            response.success, response.message = self.reset_sensing()
        except Exception:  # noqa: BLE001 - a service never raises into the executor
            self.counters.complain("reset_errors", "a reset_sensing failed")
            response.success, response.message = False, "reset_failed"
        return response

    def _on_inject(self, request: Any, response: Any) -> Any:
        try:
            response.success, response.message = self.inject_turn(bool(request.data))
        except Exception:  # noqa: BLE001
            self.counters.complain("inject_errors", "an inject_turn failed")
            response.success, response.message = False, "inject_failed"
        return response


def build_gesture_node(app: Any, *, bindings: Optional[GestureBindings] = None,
                       environ: Optional[Mapping[str, str]] = None) -> Any:
    """The ``/emori/gesture`` node for ``app``. Destroying it takes the tee off."""

    facade = GestureFacade(app, bindings if bindings is not None else load_gesture_bindings(),
                           environ=environ)
    return release_with(facade.node, facade.close, facade)
