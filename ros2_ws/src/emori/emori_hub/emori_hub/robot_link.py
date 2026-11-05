"""``RosRobotClient``: the robot client the hub uses, over ROS services.

The driver and the player take a :class:`~emori_adapter.robot.client.RobotClient` and do
not care how it reaches the robot. This one reaches the robot node's own services, and it
fails the way the REST client does, call by call, because the code above it was written
against that failure and is not edited for this one.

**What "the link is down" means.** REST has one failure a caller sees: the call fails now.
A DDS client can also be waiting for a server it has not discovered, or for one that died
mid-call. One predicate maps all of that onto the REST failure::

    link up = every required service is matched, and a heartbeat arrived within 3 s

The required services are :data:`REQUIRED_SERVICES`. ``play_sound`` and ``set_wobbling`` do
nothing on the robot and stay out of the list, so an inert service cannot hold ``health()``
false. Link down then behaves as a refused connection:

==========================  ==========================================================
``health()``                False
``read_state()``            None; also on ``ok=false`` and on any non-finite float,
                            which JSON cannot carry (REST answers a 500 or a null,
                            depending on the framework) and the gesture detectors have
                            no guard for
``apply()``, ``wake_up()``  a WARNING, and returns
``set_motor_mode()``        latches and warns, once per mode
``set_gaze()``,             False, with a 2 s backoff per gate
``set_head_reserved()``
``play_recorded_move()``    a WARNING, and None
``stop_recorded_move()``    a DEBUG line
media                       latches off
==========================  ==========================================================

**Boot.** ``health()`` needs the link, so the player's boot loop cannot reach its one motor
mode call or its wake before all the required services are matched. That is what closes
service skew at boot: the mode call happens once, and a latched ``enabled`` would leave a
cold-booted robot answering every move and never moving.

**A call in flight.** The caller blocks on an event in 250 ms slices and reads the link
between slices. A wedged node with a live heartbeat costs the full timeout, as REST does; a
dead one releases the caller within a slice of its heartbeat going stale, and later calls
fail at once.

**The gates before the first link-up.** Building the app pushes both gates once and records
them as pushed even when the push failed, and later pushes only fire on an edge. Over REST a
running node hears that push at once. Over DDS it lands before discovery, so without more a
restarted hub would leave the node's face detector running on an idle kiosk. So while the
link has never been up each gate remembers its latest value and answers False, and if the
link comes up within 5 s of construction a one-shot thread sends what is remembered, unless
a newer push already got through. It tries a gate at most 3 times, 1 s apart, outside the
backoff, because nothing else would send it again. Every send on a gate holds that gate's
lock and sends the newest value asked for, so an older value cannot land after a newer one.
After the first link-up, or if the link comes up later than 5 s, this never runs again: a
node that appears later is a fresh node, which REST would not have reached either.

**Identity and motion timing.** Every move is client ``hostapp``, lane ``interaction``,
``preempt`` false, as the REST routes default them. ``supersede`` comes from the intent and
the trace tag goes in ``reason``. Losing either would silently restore a queue in which each
sentence's first move waits behind the last sentence's tail.

Nothing here imports ROS at load time. :func:`load_link_bindings` resolves ``rclpy`` and the
service types, and everything else takes what it needs by injection, so the whole client
runs under test on a machine with no ROS.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from emori_adapter import motion_trace
from emori_adapter.models import MotionIntent
from emori_adapter.robot.client import (
    _FLAG_BACKOFF_S,
    _FLAG_TIMEOUT_S,
    HttpRobotClient,
    _antennas_wire,
    _xyzrpy,
    motion_sync_enabled,
)
from emori_hub.spin import SupervisedSpin

__all__ = [
    "HEARTBEAT_TOPIC",
    "INERT_SERVICES",
    "LinkBindings",
    "LinkDown",
    "LinkNode",
    "NODE_NAME",
    "NODE_NAMESPACE",
    "REQUIRED_SERVICES",
    "RobotLink",
    "RosRobotClient",
    "SERVICE_NAMES",
    "full_state_dict",
    "load_link_bindings",
    "start_robot_link",
]

log = logging.getLogger("emori.robot.link")

NODE_NAMESPACE = "/emori"
NODE_NAME = "robot_link"

#: The services the link needs before it counts as up.
REQUIRED_SERVICES = ("submit_move", "get_full_state", "get_status", "set_motor_mode",
                     "set_gaze", "set_head_reserved", "stop")
#: Answered by the robot and doing nothing there.
INERT_SERVICES = ("play_sound", "set_wobbling")
#: Absolute, so this node's own name does not decide them.
SERVICE_NAMES = {name: f"/reachy_mini/{name}" for name in REQUIRED_SERVICES + INERT_SERVICES}
HEARTBEAT_TOPIC = "/reachy_mini/link_heartbeat"

#: A heartbeat older than this is a dead node. The robot sends two a second.
HEARTBEAT_TIMEOUT_S = 3.0
#: How often a blocked caller reads the link.
WAIT_SLICE_S = 0.25
#: The REST client's deadline for everything but the two gates.
CALL_TIMEOUT_S = 5.0

#: How long after construction a first link-up still delivers the remembered gates. Long
#: enough for discovery with a node that is already running.
REPLAY_WINDOW_S = 5.0
REPLAY_ATTEMPTS = 3
REPLAY_GAP_S = 1.0
_REPLAY_POLL_S = 0.05

#: Who this is to the arbiter.
CLIENT_NAME = "hostapp"
LANE = "interaction"
#: What a REST route leaves at the request's default for a move with no duration of its own.
_DEFAULT_DURATION = 0.5
_DEFAULT_INTERPOLATION = "minjerk"
#: A trace tag is bounded on this path as on the REST one.
_TAG_LIMIT = 40

_POSE_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")
_RPY_KEYS = ("roll", "pitch", "yaw")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Nothing on the link executor raises in normal running: its one callback of ours stores a
#: time, and a clean spin returns within a quarter of a second. So this many faults in a row,
#: about two seconds' worth, is an executor that has broken, and the callers it serves are
#: waiting on nothing.
_SPIN_FAULT_LIMIT = 40


class LinkDown(Exception):
    """A call that got no answer. What ``httpx.HTTPError`` is to the REST client."""


# --- the link: is it there, and how to make a request ---------------------------------


class RobotLink:
    """The service clients and the heartbeat stamp, and the one predicate over them.

    ``clients`` holds an rclpy service client per name in :data:`SERVICE_NAMES` and
    ``requests`` what builds its request. Anything with ``service_is_ready`` and
    ``call_async`` will do.
    """

    def __init__(
        self,
        clients: Mapping[str, Any],
        requests: Mapping[str, Callable[[], Any]],
        *,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clients = clients
        self._requests = requests
        self._heartbeat_timeout = heartbeat_timeout_s
        self._clock = clock
        self._last_beat: Optional[float] = None

    def note_heartbeat(self, _message: Any = None) -> None:
        """The subscription's callback. It stores a time, so nothing here can block."""

        self._last_beat = self._clock()

    def client(self, name: str) -> Any:
        return self._clients[name]

    def request(self, name: str) -> Any:
        return self._requests[name]()

    def is_ready(self, name: str) -> bool:
        """Is this service matched. A client whose context has gone reads as unmatched."""

        try:
            return bool(self._clients[name].service_is_ready())
        except Exception:  # noqa: BLE001 - shutting down is a link that is down
            return False

    def first_unmatched(self) -> str:
        """The first required service with no server; empty when all are matched."""

        return next((name for name in REQUIRED_SERVICES if not self.is_ready(name)), "")

    def down_reason(self) -> str:
        """Why the link is down, naming what is missing; empty when it is up."""

        missing = self.first_unmatched()
        if missing:
            return f"service {SERVICE_NAMES[missing]} is not matched"
        beat = self._last_beat
        if beat is None:
            return "no link heartbeat yet"
        age = self._clock() - beat
        if age > self._heartbeat_timeout:
            return f"no link heartbeat for {age:.1f} s"
        return ""

    def is_up(self) -> bool:
        return not self.down_reason()


# --- the state read ------------------------------------------------------------------


def _pose(message: Any, keys: tuple[str, ...]) -> dict[str, float]:
    return {key: float(getattr(message, key)) for key in keys}


def _timestamp(stamp: Any) -> Optional[str]:
    """``builtin_interfaces/Time`` as the REST body spells it; None when it was never set."""

    if not stamp.sec and not stamp.nanosec:
        return None
    moment = _EPOCH + timedelta(seconds=int(stamp.sec), microseconds=int(stamp.nanosec) // 1000)
    return moment.isoformat().replace("+00:00", "Z")


def full_state_dict(state: Any) -> dict[str, Any]:
    """A ``reachy_mini_msgs/FullState`` as the body of the REST state read, key for key.

    ``motion`` is left out when the node has none, because a missing block is how the code
    above reads an older robot. ``command`` and ``residual`` likewise.
    """

    full: dict[str, Any] = {
        "control_mode": str(state.control_mode) or None,
        "head_pose": _pose(state.head_pose, _POSE_KEYS),
        "head_joints": [float(value) for value in state.head_joints],
        "body_yaw": float(state.body_yaw),
        "antennas_position": [float(value) for value in state.antennas_position],
        "timestamp": _timestamp(state.stamp),
        "passive_joints": None,
        "doa": None,
    }
    if not state.has_motion:
        return full
    block = state.motion
    motion: dict[str, Any] = {
        "active": bool(block.active),
        "head_moving": bool(block.head_moving),
        "head_reserved": bool(block.head_reserved),
        "client": str(block.client),
        "lane": str(block.lane),
        "gaze_active": bool(block.gaze_active),
        "face_seen_s": float(block.face_seen_s) if block.has_face_seen else None,
    }
    if block.has_command:
        motion["command"] = _pose(block.command, _RPY_KEYS)
    if block.has_residual:
        motion["residual"] = _pose(block.residual, _RPY_KEYS)
    full["motion"] = motion
    return full


def _all_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_finite(item) for item in value)
    return True


# --- the client ----------------------------------------------------------------------


@dataclass
class _Gate:
    """One boolean gate on the robot: the lock every send holds, and its latest value."""

    service: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    # The newest value anybody asked for; None until somebody does.
    wanted: Optional[bool] = None
    # A value got through, so there is nothing left for the replay to deliver.
    acknowledged: bool = False
    backoff_until: float = 0.0


class RosRobotClient:
    """A :class:`RobotClient` over :class:`RobotLink`. See the module docstring."""

    #: What ``/healthz`` reports. A wrapper that forwards attributes reports it too.
    runtime = "ros"

    RECORDED_DATASET = HttpRobotClient.RECORDED_DATASET

    def __init__(
        self,
        link: RobotLink,
        *,
        timeout: float = CALL_TIMEOUT_S,
        flag_timeout_s: float = _FLAG_TIMEOUT_S,
        flag_backoff_s: float = _FLAG_BACKOFF_S,
        wait_slice_s: float = WAIT_SLICE_S,
        replay_window_s: float = REPLAY_WINDOW_S,
        replay_attempts: int = REPLAY_ATTEMPTS,
        replay_gap_s: float = REPLAY_GAP_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._link = link
        self._timeout = timeout
        self._flag_timeout = flag_timeout_s
        self._flag_backoff = flag_backoff_s
        self._wait_slice = wait_slice_s
        self._replay_window = replay_window_s
        self._replay_attempts = replay_attempts
        self._replay_gap = replay_gap_s
        self._clock = clock
        self._supersede = motion_sync_enabled()
        self._modes_refused: set[str] = set()
        self._media_ok = True
        self._gaze = _Gate("set_gaze")
        self._head_reserved = _Gate("set_head_reserved")
        self._seen_up = False
        self._closed = threading.Event()
        self._constructed_at = clock()
        self._replay = threading.Thread(target=self._replay_gates, name="robot-link-replay",
                                        daemon=True)
        self._replay.start()

    @property
    def link(self) -> RobotLink:
        """The link's own questions, none of which calls the robot: for a status reader."""

        return self._link

    # --- one call -----------------------------------------------------------------------

    def _call(self, name: str, request: Any, timeout_s: float) -> Any:
        """One service call: its response, or :class:`LinkDown`. Blocks at most ``timeout_s``."""

        if self._closed.is_set():
            raise LinkDown("the client is closed")
        link = self._link
        why = link.down_reason() or self._unmatched_inert(name)
        if why:
            raise LinkDown(why)
        client = link.client(name)
        try:
            future = client.call_async(request)
            done = threading.Event()
            future.add_done_callback(lambda _future: done.set())
        except Exception as error:  # noqa: BLE001 - whatever the transport raises is this
            raise LinkDown(f"{name}: {error}") from error
        deadline = time.monotonic() + timeout_s
        while not done.wait(min(self._wait_slice, max(0.0, deadline - time.monotonic()))):
            why = link.down_reason()
            if not why and time.monotonic() >= deadline:
                why = f"{name} got no answer in {timeout_s:g} s"
            if why:
                self._abandon(client, future)
                raise LinkDown(why)
        try:
            response = future.result()
        except Exception as error:  # noqa: BLE001 - as above
            raise LinkDown(f"{name}: {error}") from error
        if response is None:
            raise LinkDown(f"{name} was cancelled")
        return response

    def _unmatched_inert(self, name: str) -> str:
        """The inert services are not in the link, so each is checked when it is called."""

        if name in REQUIRED_SERVICES or self._link.is_ready(name):
            return ""
        return f"service {SERVICE_NAMES[name]} is not matched"

    @staticmethod
    def _abandon(client: Any, future: Any) -> None:
        """Stop holding a request nobody is waiting for; a late answer is then dropped."""

        forget = getattr(client, "remove_pending_request", None)
        if callable(forget):
            try:
                forget(future)
            except Exception:  # noqa: BLE001 - it has gone already
                pass

    # --- connectivity -------------------------------------------------------------------

    def health(self) -> bool:
        try:
            response = self._call("get_status", self._link.request("get_status"), self._timeout)
        except Exception as error:  # noqa: BLE001 - see the module docstring
            log.warning("robot status check failed: %s", error)
            return False
        return response.state == "running"

    def robot_status(self) -> dict[str, Any]:
        """The robot's status for host tools, with what the link has to say about it."""

        body: dict[str, Any] = {"state": "error", "error": "", "motor_control_mode": "",
                                "first_unmatched": self._link.first_unmatched()}
        try:
            response = self._call("get_status", self._link.request("get_status"), self._timeout)
        except Exception as error:  # noqa: BLE001 - this route reports the failure
            body["error"] = str(error)
            return body
        for key in ("state", "motor_control_mode", "error", "version", "robot_name", "backend"):
            body[key] = str(getattr(response, key))
        for key in ("ready", "simulation_enabled", "mockup_sim_enabled", "no_media",
                    "media_released", "wireless_version", "gaze_run"):
            body[key] = bool(getattr(response, key))
        return body

    def wake_up(self) -> None:
        self._send_move("wake_up", label="wake_up", tagged=False)

    def set_motor_mode(self, mode: str) -> None:
        """Set the motor mode. A refusal or a failure latches that mode off for good.

        On a separate service client from motion, as the REST client keeps a separate pool.
        """

        if mode in self._modes_refused:
            return
        try:
            request = self._link.request("set_motor_mode")
            request.mode = mode
            response = self._call("set_motor_mode", request, self._timeout)
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._modes_refused.add(mode)
            log.warning("motor mode %r failed (%s); not retrying.", mode, error)
            return
        if not response.ok:
            self._modes_refused.add(mode)
            log.warning("robot refused motor mode %r (%s); not retrying. The head stays stiff, "
                        "so any back-drivable head-tilt gesture will not fire.",
                        mode, response.message)

    # --- the two gates ------------------------------------------------------------------

    def set_gaze(self, enabled: bool) -> bool:
        """Is face look-at wanted right now. See ``HttpRobotClient.set_gaze``."""

        return self._push_gate(self._gaze, bool(enabled))

    def set_head_reserved(self, reserved: bool) -> bool:
        """Is a hand on the head. See ``HttpRobotClient.set_head_reserved``."""

        return self._push_gate(self._head_reserved, bool(reserved))

    def _link_has_been_up(self) -> bool:
        if not self._seen_up and self._link.is_up():
            self._seen_up = True
        return self._seen_up

    def _push_gate(self, gate: _Gate, value: bool) -> bool:
        """True only when the robot acknowledged. False lets the next tick try again."""

        with gate.lock:
            gate.wanted = value
            if not self._link_has_been_up():
                return False
            now = self._clock()
            if now < gate.backoff_until:
                return False
            if self._send_gate(gate, value):
                gate.acknowledged = True
                gate.backoff_until = 0.0
                return True
            gate.backoff_until = now + self._flag_backoff
            return False

    def _send_gate(self, gate: _Gate, value: bool) -> bool:
        try:
            request = self._link.request(gate.service)
            request.data = value
            response = self._call(gate.service, request, self._flag_timeout)
        except Exception as error:  # noqa: BLE001 - a blip: retry, but later
            log.debug("%s failed: %s", gate.service, error)
            return False
        if not response.success:
            # gaze_disabled is a node that a restart may change, so this backs off and
            # never latches.
            log.debug("%s refused: %s", gate.service, response.message)
            return False
        return True

    def _replay_gates(self) -> None:
        """Deliver what the first pushes could not. One pass, then this thread is gone."""

        deadline = self._constructed_at + self._replay_window
        while not self._link_has_been_up():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            if self._closed.wait(min(_REPLAY_POLL_S, remaining)):
                return
        for attempt in range(self._replay_attempts):
            waiting = [gate for gate in (self._gaze, self._head_reserved)
                       if not self._replay_one(gate)]
            if not waiting:
                return
            if attempt + 1 < self._replay_attempts and self._closed.wait(self._replay_gap):
                return
        log.warning("the first gate values did not reach the robot after %d attempts: %s",
                    self._replay_attempts, ", ".join(gate.service for gate in waiting))

    def _replay_one(self, gate: _Gate) -> bool:
        """True when there is nothing left to send on this gate."""

        with gate.lock:
            if gate.acknowledged or gate.wanted is None:
                return True
            if not self._send_gate(gate, gate.wanted):
                return False
            gate.acknowledged = True
            gate.backoff_until = 0.0
            return True

    # --- intent dispatch ----------------------------------------------------------------

    def apply(self, intent: MotionIntent) -> None:
        if intent.kind in ("goto", "look_at", "antennas"):
            self._goto(intent)
        elif intent.kind == "set_target":
            self._set_target(intent)
        elif intent.kind == "play_sound":
            self._play_sound(intent.sound)
        elif intent.kind == "wobble":
            self._wobble(bool(intent.wobble))
        elif intent.kind == "wake_up":
            self.wake_up()
        elif intent.kind == "play_recorded":
            self.play_recorded_move(intent.move or "", supersede=intent.supersede)
        else:  # pragma: no cover - guarded by the Literal type
            log.debug("ignoring unknown intent kind: %s", intent.kind)

    def _goto(self, intent: MotionIntent) -> None:
        self._send_move(
            "goto",
            label="ros:goto",
            supersede=intent.supersede,
            head=intent.head,
            antennas=intent.antennas,
            body_yaw=intent.body_yaw,
            duration=float(intent.duration),
            interpolation=intent.interpolation,
        )

    def _set_target(self, intent: MotionIntent) -> None:
        self._send_move(
            "set_target",
            label="ros:set_target",
            tagged=False,
            head=intent.head,
            antennas=intent.antennas,
            body_yaw=intent.body_yaw,
        )

    def _move_request(
        self,
        kind: str,
        *,
        label: str,
        tagged: bool = True,
        supersede: bool = False,
        head: Any = None,
        antennas: Any = None,
        body_yaw: Optional[float] = None,
        duration: float = _DEFAULT_DURATION,
        interpolation: str = _DEFAULT_INTERPOLATION,
        dataset: str = "",
        move: str = "",
    ) -> Any:
        request = self._link.request("submit_move")
        request.kind = kind
        request.lane = LANE
        request.client = CLIENT_NAME
        request.preempt = False
        request.duration = float(duration)
        request.interpolation = interpolation
        request.dataset = dataset
        request.move = move
        request.supersede = bool(supersede and self._supersede)
        tag = motion_trace.current_tag().strip() if tagged else ""
        request.reason = f"{label}:{tag[:_TAG_LIMIT]}" if tag else label
        if head is not None:
            request.use_head = True
            for key, value in _xyzrpy(head).items():
                setattr(request.head_pose, key, value)
        if antennas is not None:
            request.use_antennas = True
            request.antennas = _antennas_wire(antennas)
        if body_yaw is not None:
            request.use_body_yaw = True
            request.body_yaw = float(body_yaw)
        return request

    def _send_move(self, kind: str, **fields: Any) -> None:
        """Submit one move. Logs what went wrong and returns, whatever happened."""

        path = f"submit_move:{kind}"
        try:
            request = self._move_request(kind, **fields)
            response = self._call("submit_move", request, self._timeout)
        except Exception as error:  # noqa: BLE001 - see the module docstring
            motion_trace.emit("post", path=path, status=0)
            log.warning("%s failed: %s", path, error)
            return
        if response.accepted:
            return
        code = str(response.error_code) or "refused"
        if kind == "set_target" and code == "busy":
            # A target that meets a running move is ignored, and REST answers it with a 200.
            return
        motion_trace.emit("post", path=path, status=409)
        log.warning("%s refused: %s", path, code)

    # --- recorded moves -----------------------------------------------------------------

    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None:
        """Start one recorded emotion move; returns its id, which ``stop`` takes.

        A refusal or an empty id is None, so the player skips its wait.
        """

        if not name:
            return None
        try:
            request = self._move_request(
                "play_recorded",
                label=f"ros:recorded:{name}",
                supersede=supersede,
                dataset=self.RECORDED_DATASET,
                move=name,
            )
            response = self._call("submit_move", request, self._timeout)
        except Exception as error:  # noqa: BLE001 - a failed flourish must not kill the queue
            log.warning("recorded move %r failed: %s", name, error)
            return None
        if response.accepted:
            return str(response.move_id) or None
        log.warning("recorded move %r refused: %s", name, response.error_code)
        return None

    def stop_recorded_move(self, uuid: str) -> None:
        """Stop a running recorded move. An empty id would stop every move, so it stops none."""

        if not uuid:
            return
        try:
            request = self._link.request("stop")
            request.move_id = str(uuid)
            response = self._call("stop", request, self._timeout)
        except Exception as error:  # noqa: BLE001
            log.debug("stop recorded move %s failed: %s", uuid, error)
            return
        if not response.stopped:
            log.warning("stop recorded move %s refused: it is not running", uuid)

    # --- media --------------------------------------------------------------------------

    def _play_sound(self, sound: str | None) -> None:
        if not sound or not self._media_ok:
            return
        try:
            request = self._link.request("play_sound")
            request.file = sound
            response = self._call("play_sound", request, self._timeout)
        except Exception as error:  # noqa: BLE001
            log.info("play_sound failed (%s); muting further audio", error)
            self._media_ok = False
            return
        if not response.ok:
            log.info("play_sound(%s) refused; muting further audio", sound)
            self._media_ok = False

    def _wobble(self, enable: bool) -> None:
        try:
            request = self._link.request("set_wobbling")
            request.data = enable
            self._call("set_wobbling", request, self._timeout)
        except Exception as error:  # noqa: BLE001
            log.debug("wobble toggle failed: %s", error)

    # --- the state read -----------------------------------------------------------------

    def read_state(self) -> Optional[dict]:
        """The robot's present state, as the REST read returns it, or None.

        A blocking call on the caller's thread and not a topic: the gesture detectors count
        ticks, and the tick's period includes this round trip.
        """

        try:
            request = self._link.request("get_full_state")
            response = self._call("get_full_state", request, self._timeout)
            if not response.ok:
                log.debug("read_state refused: %s", response.error)
                return None
            state = full_state_dict(response.state)
        except Exception as error:  # noqa: BLE001 - the sensor simply skips that tick
            log.debug("read_state failed: %s", error)
            return None
        if not _all_finite(state):
            log.debug("read_state: the robot reported a non-finite number")
            return None
        return state

    def close(self) -> None:
        """Later calls fail at once. The node belongs to whoever started it."""

        self._closed.set()
        if self._replay is not threading.current_thread():
            self._replay.join(timeout=self._flag_timeout + self._wait_slice)


# --- the node ------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkBindings:
    """Everything the node wiring takes from ROS, so a test can hand it fakes."""

    create_node: Callable[..., Any]
    executor_cls: Callable[..., Any]
    group_cls: Callable[[], Any]
    state_qos: Any
    ok: Callable[[], bool]
    fatal: tuple[type[BaseException], ...]
    Header: Any
    # Service type per name in SERVICE_NAMES.
    services: Mapping[str, Any]


def load_link_bindings() -> LinkBindings:
    """Import ``rclpy`` and the service types. Raises ``ImportError`` where ROS is absent."""

    import rclpy
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Header
    from std_srvs.srv import SetBool

    from reachy_mini_msgs.srv import (
        GetFullState,
        GetRobotStatus,
        PlaySound,
        SetMotorMode,
        StopMove,
        SubmitMove,
    )

    return LinkBindings(
        create_node=Node,
        executor_cls=SingleThreadedExecutor,
        group_cls=ReentrantCallbackGroup,
        state_qos=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE),
        ok=rclpy.ok,
        fatal=(ExternalShutdownException,),
        Header=Header,
        services={
            "submit_move": SubmitMove,
            "get_full_state": GetFullState,
            "get_status": GetRobotStatus,
            "set_motor_mode": SetMotorMode,
            "set_gaze": SetBool,
            "set_head_reserved": SetBool,
            "stop": StopMove,
            "play_sound": PlaySound,
            "set_wobbling": SetBool,
        },
    )


class LinkNode:
    """The ``/emori/robot_link`` node, its executor and its thread.

    The executor runs client responses and the heartbeat's stamp and nothing else, so no
    work elsewhere in the process can starve a response a caller is waiting for.
    """

    def __init__(self, bindings: LinkBindings) -> None:
        self._ros = bindings
        self._node: Any = None
        self._executor: Any = None
        self.spin: SupervisedSpin | None = None
        self.link: RobotLink | None = None

    @property
    def node(self) -> Any:
        return self._node

    def start(self) -> None:
        ros = self._ros
        node = ros.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
        group = ros.group_cls()
        clients = {name: node.create_client(ros.services[name], SERVICE_NAMES[name],
                                            callback_group=group)
                   for name in SERVICE_NAMES}
        requests = {name: ros.services[name].Request for name in SERVICE_NAMES}
        self.link = RobotLink(clients, requests)
        node.create_subscription(ros.Header, HEARTBEAT_TOPIC, self.link.note_heartbeat,
                                 ros.state_qos, callback_group=group)
        executor = ros.executor_cls()
        executor.add_node(node)
        self._node = node
        self._executor = executor
        self.spin = SupervisedSpin("ros-link", executor, ok=ros.ok, fatal=ros.fatal,
                                   fault_limit=_SPIN_FAULT_LIMIT, logger=log)
        self.spin.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop spinning and release the node. Never raises."""

        if self.spin is not None:
            self.spin.stop(timeout)
        for release in (getattr(self._executor, "shutdown", None),
                        getattr(self._node, "destroy_node", None)):
            if callable(release):
                try:
                    release()
                except Exception:  # noqa: BLE001 - teardown is best effort
                    log.warning("link teardown failed", exc_info=True)
        self._executor = None
        self._node = None


def start_robot_link(bindings: Optional[LinkBindings] = None) -> LinkNode:
    """Build the node and start spinning. Returns it; ``.link`` is what the client takes."""

    node = LinkNode(bindings if bindings is not None else load_link_bindings())
    node.start()
    return node
