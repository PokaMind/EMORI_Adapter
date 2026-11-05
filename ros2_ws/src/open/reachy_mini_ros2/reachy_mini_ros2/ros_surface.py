"""The robot's link surface: the REST shim's questions and commands, as ROS services.

A REST client reads the pose, asks for the daemon status, submits a move, reads the move
trace and sets the gaze flag. A ROS client does the same here, on a node of its own,
``reachy_mini_link``. The state reads are the ones the shim serves (they come from the
same :mod:`state_snapshot` factory), and the arbiter is the same arbiter, so both
transports give one answer.

Why a second node. Every accepted ROS goal parks an executor thread for the whole move,
and the main node's pool is small. A callback group does not reserve a thread, so a state
read could wait behind a goto. This node has an executor of its own with two threads and
one reentrant group: a state read never waits for a submit, and neither waits for a goal.

Three rules keep the link alive:

* **A handler never raises.** In Jazzy's rclpy an exception in a callback is stored on its
  task and re-raised out of the executor's spin, which then ends. No response goes out and
  the heartbeat on that executor stops, so a client sees the link as down for good while
  every process stays up. So each handler body is a catch-all that logs and still answers
  with the failure shape its type allows: ``ok=false``, ``state="error"``,
  ``accepted=false`` with ``backend_error``, ``success=false``.
* **The spin logs and continues.** :func:`logged_spin` drives the executor so that
  anything that does escape is logged and the next callback still runs.
* **Nothing here blocks.** ``submit_move`` returns on acceptance, as the REST routes do.

Services on this node, all absolute names under ``/reachy_mini``: ``submit_move``,
``get_full_state``, ``get_status``, ``get_move_trace``, ``set_gaze``, ``play_sound`` and
``set_wobbling``, and the ``link_heartbeat`` topic (a ``std_msgs/Header``, 2 Hz,
best-effort) a client reads as "the node is up and spinning". ``play_sound`` and
``set_wobbling`` are inert, as the shim's media routes are.

This module imports no ROS at load time. :func:`load_ros_bindings` resolves ``rclpy`` and
the message types, and everything that needs them takes a :class:`RosBindings`, so the
handlers and the node wiring run under test with fakes.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "HEARTBEAT_PERIOD_S",
    "HEARTBEAT_TOPIC",
    "LinkHandlers",
    "LinkSurface",
    "REASON_LIMIT",
    "RosBindings",
    "fill_full_state",
    "load_ros_bindings",
    "logged_spin",
    "start_link_surface",
]

#: The link's liveness signal, and how often it goes out.
HEARTBEAT_TOPIC = "/reachy_mini/link_heartbeat"
HEARTBEAT_PERIOD_S = 0.5

#: Executor threads on the link node: one for a state read, one for a submit.
LINK_THREADS = 2

#: How long one ``spin_once`` waits for work, which is also how soon a stop is noticed.
SPIN_SLICE_S = 0.25

#: After a callback escapes with an exception, the spin waits this long before it goes on,
#: so an executor that fails at once cannot turn the loop into a busy one.
SPIN_FAULT_PAUSE_S = 0.05

#: A repeated failure of one handler is logged at most this often, with a count.
COMPLAINT_PERIOD_S = 30.0

#: The longest ``reason`` kept. REST bounds its trace tag the same way; the arbiter keeps
#: the string in its trace for the life of the process, so a caller cannot make it huge.
# What a SubmitMove can ask for: everything the arbiter runs from its queue, and the unqueued
# target, which it applies at once or refuses.
SUBMITTABLE_KINDS = ("goto", "set_target", "wake_up", "goto_sleep", "play_recorded")
# Building a message per record takes a service thread's time on a CPU that is already busy.
TRACE_RESPONSE_MAX = 1024
REASON_LIMIT = 160

#: Service and topic names. Absolute, so the node's own name does not decide them.
SUBMIT_MOVE = "/reachy_mini/submit_move"
GET_FULL_STATE = "/reachy_mini/get_full_state"
GET_STATUS = "/reachy_mini/get_status"
GET_MOVE_TRACE = "/reachy_mini/get_move_trace"
SET_GAZE = "/reachy_mini/set_gaze"
PLAY_SOUND = "/reachy_mini/play_sound"
SET_WOBBLING = "/reachy_mini/set_wobbling"

#: The arbiter's answer when it, or what was asked of it, raised.
BACKEND_ERROR = "backend_error"

#: The ``set_gaze`` answer on a node built without the face look-at component.
GAZE_DISABLED = "gaze_disabled"

_TRACE_INTS = ("seq", "queued")
_TRACE_FLOATS = ("t",)


# --- the sibling modules -------------------------------------------------------------
# Loaded by path when there is no package around this file (the tests), under the name the
# REST shim uses, so both share one module object.


def _load_state_snapshot() -> Any:
    try:
        from . import state_snapshot as loaded

        return loaded
    except ImportError:
        import importlib.util
        from pathlib import Path

        name = "reachy_mini_ros2_state_snapshot_by_path"
        loaded = sys.modules.get(name)
        if loaded is None:
            spec = importlib.util.spec_from_file_location(
                name, Path(__file__).with_name("state_snapshot.py"))
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[name] = loaded
            spec.loader.exec_module(loaded)
        return loaded


def _default_to_matrix() -> Callable[[Any], Any]:
    """``PoseRpy`` (or anything with x, y, z, roll, pitch, yaw) -> a 4x4 matrix."""

    import numpy as np

    to_matrix, _ = _load_state_snapshot().pose_conversions()

    def convert(pose: Any) -> Any:
        return np.asarray(
            to_matrix(float(pose.x), float(pose.y), float(pose.z),
                      float(pose.roll), float(pose.pitch), float(pose.yaw)),
            dtype=float)

    return convert


def _default_request_cls() -> type:
    found = _load_state_snapshot().import_sibling("arbiter", ("MotionRequest",))
    if not found:
        raise ImportError("reachy_mini_ros2.arbiter is not importable")
    return found[0]


# --- logging -------------------------------------------------------------------------


class _Log:
    """One face over stdlib loggers and rclpy's, which take a single string."""

    def __init__(self, log: Any | None) -> None:
        self._log = log if log is not None else logging.getLogger("reachy_mini.ros_surface")

    def _emit(self, level: str, message: str) -> None:
        fn = getattr(self._log, level, None)
        if fn is None:
            return
        try:
            fn(message)
        except Exception:  # noqa: BLE001 - logging must never take the link down
            pass

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warning(self, message: str) -> None:
        self._emit("warning", message)

    def error(self, message: str) -> None:
        self._emit("error", message)


class _Complaints:
    """Log a failure once, then at most every ``period_s`` with how many were held back.

    ``get_full_state`` is polled at 10 Hz, so a backend that stays broken would otherwise
    write ten lines a second.
    """

    def __init__(self, log: _Log, clock: Callable[[], float], period_s: float) -> None:
        self._log = log
        self._clock = clock
        self._period = period_s
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[float, int]] = {}

    def report(self, where: str, error: BaseException) -> None:
        now = self._clock()
        with self._lock:
            last, held = self._seen.get(where, (None, 0))
            if last is not None and now - last < self._period:
                self._seen[where] = (last, held + 1)
                return
            self._seen[where] = (now, 0)
        extra = f" ({held} more since the last report)" if held else ""
        self._log.error(f"{where} failed: {type(error).__name__}: {error}{extra}")


# --- the spin ------------------------------------------------------------------------


def logged_spin(
    executor: Any,
    log: Any | None = None,
    *,
    stop: threading.Event | None = None,
    ok: Callable[[], bool] = lambda: True,
    fatal: tuple[type[BaseException], ...] = (),
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Spin ``executor`` until ``stop`` is set or ``ok()`` goes false, whatever it raises.

    ``spin_once`` is where a callback's stored exception comes back out. It is logged
    here and the loop goes on, so one bad callback costs one callback. ``fatal`` is the
    exceptions that mean the context is going away; they end the loop quietly.
    """

    logger = _Log(log)
    complaints = _Complaints(logger, clock, COMPLAINT_PERIOD_S)
    halt = stop if stop is not None else threading.Event()
    while ok() and not halt.is_set():
        try:
            executor.spin_once(timeout_sec=SPIN_SLICE_S)
        except fatal:
            return
        except Exception as error:  # noqa: BLE001 - this is the point of the function
            complaints.report("the link executor", error)
            halt.wait(SPIN_FAULT_PAUSE_S)


# --- the message fills ---------------------------------------------------------------


def _fill_pose(target: Any, source: dict[str, Any]) -> None:
    for key in ("x", "y", "z", "roll", "pitch", "yaw"):
        setattr(target, key, float(source[key]))


def _fill_rpy(target: Any, source: dict[str, Any]) -> None:
    for key in ("roll", "pitch", "yaw"):
        setattr(target, key, float(source[key]))


def _stamp_of(value: Any) -> tuple[int, int]:
    """``(sec, nanosec)`` of the snapshot's ISO timestamp; ``(0, 0)`` when it has none."""

    if value is None:
        return 0, 0
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86400 + delta.seconds, delta.microseconds * 1000


def fill_full_state(message: Any, snapshot: dict[str, Any]) -> None:
    """Write a ``/api/state/full`` dict into a ``reachy_mini_msgs/FullState``.

    The dict is read completely before the message is touched, so a snapshot that is
    malformed never leaves a half-written message behind. A reading without joints is
    refused: a zero pose built from nothing would be believed.
    """

    joints = [float(v) for v in (snapshot.get("head_joints") or [])]
    antennas = [float(v) for v in (snapshot.get("antennas_position") or [])]
    body_yaw = snapshot.get("body_yaw")
    if len(joints) < 7 or len(antennas) < 2 or body_yaw is None:
        raise ValueError("the backend reported no joints")
    head = snapshot["head_pose"]
    sec, nanosec = _stamp_of(snapshot.get("timestamp"))
    motion = snapshot.get("motion")

    message.stamp.sec = sec
    message.stamp.nanosec = nanosec
    message.control_mode = str(snapshot.get("control_mode") or "")
    _fill_pose(message.head_pose, head)
    message.head_joints = joints
    message.body_yaw = float(body_yaw)
    message.antennas_position = antennas
    message.has_motion = motion is not None
    if motion is None:
        return
    block = message.motion
    block.active = bool(motion.get("active"))
    block.head_moving = bool(motion.get("head_moving"))
    block.head_reserved = bool(motion.get("head_reserved"))
    block.client = str(motion.get("client") or "")
    block.lane = str(motion.get("lane") or "")
    block.gaze_active = bool(motion.get("gaze_active"))
    seen = motion.get("face_seen_s")
    block.has_face_seen = seen is not None
    block.face_seen_s = float(seen) if seen is not None else 0.0
    command = motion.get("command")
    block.has_command = command is not None
    if command is not None:
        _fill_rpy(block.command, command)
    residual = motion.get("residual")
    block.has_residual = residual is not None
    if residual is not None:
        _fill_rpy(block.residual, residual)


def _finite(values: Iterable[float], what: str) -> None:
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError(f"{what} is not a finite number")


# --- the handlers --------------------------------------------------------------------


class LinkHandlers:
    """What each service does, with no ROS in sight.

    ``state`` is a :class:`state_snapshot.StateSnapshot` (anything with ``snapshot`` and
    ``status``). ``set_gaze`` and ``gaze_run`` are the face look-at component's run switch
    and its flag; both ``None`` is a node without gaze. ``trace_record`` makes an empty
    ``MoveTraceRecord``. Every public handler answers and none raises.
    """

    def __init__(
        self,
        arbiter: Any,
        state: Any,
        *,
        set_gaze: Callable[[bool], None] | None = None,
        gaze_run: Callable[[], bool] | None = None,
        trace_record: Callable[[], Any] | None = None,
        request_cls: type | None = None,
        to_matrix: Callable[[Any], Any] | None = None,
        backend_name: str | None = None,
        log: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._arbiter = arbiter
        self._state = state
        self._set_gaze = set_gaze
        self._gaze_run = gaze_run
        self._trace_record = trace_record
        self._request_cls = request_cls
        self._to_matrix = to_matrix
        self._backend_name = backend_name or _load_state_snapshot().BACKEND_MARKER
        self._log = _Log(log)
        self._complaints = _Complaints(self._log, clock, COMPLAINT_PERIOD_S)
        self._inert_said = threading.Event()

    # --- moves -----------------------------------------------------------------------

    def submit_move(self, request: Any, response: Any) -> Any:
        try:
            outcome = self._arbiter.submit(self._motion_request(request))
            response.accepted = bool(getattr(outcome, "accepted", False))
            response.error_code = str(getattr(outcome, "error_code", "") or "")
            response.move_id = str(getattr(outcome, "move_id", "") or "")
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._complaints.report("submit_move", error)
            response.accepted = False
            response.error_code = BACKEND_ERROR
            response.move_id = ""
        return response

    def _motion_request(self, request: Any) -> Any:
        # The arbiter would take an unknown kind, hold a queue slot for it and fail it later,
        # so a caller would hear "accepted" for something that can never run.
        if request.kind not in SUBMITTABLE_KINDS:
            raise ValueError(f"unknown move kind {request.kind!r}")
        if self._request_cls is None:
            self._request_cls = _default_request_cls()
        if self._to_matrix is None:
            self._to_matrix = _default_to_matrix()
        head = None
        if request.use_head:
            pose = request.head_pose
            _finite((pose.x, pose.y, pose.z, pose.roll, pose.pitch, pose.yaw), "head_pose")
            head = self._to_matrix(pose)
        antennas = None
        if request.use_antennas:
            _finite(request.antennas, "antennas")
            antennas = (float(request.antennas[0]), float(request.antennas[1]))
        body_yaw = None
        if request.use_body_yaw:
            _finite((request.body_yaw,), "body_yaw")
            body_yaw = float(request.body_yaw)
        _finite((request.duration,), "duration")
        return self._request_cls(
            kind=request.kind,
            lane=request.lane,
            client=request.client,
            head_pose=head,
            antennas=antennas,
            body_yaw=body_yaw,
            duration=float(request.duration),
            interpolation=request.interpolation,
            dataset=request.dataset,
            move=request.move,
            preempt=bool(request.preempt),
            reason=str(request.reason or "")[:REASON_LIMIT],
            supersede=bool(request.supersede),
        )

    def get_move_trace(self, request: Any, response: Any) -> Any:
        try:
            make = self._trace_record
            if make is None:
                raise RuntimeError("no MoveTraceRecord type was given")
            records = []
            # Oldest first, so a caller that asks again from the last seq gets the rest.
            for found in self._arbiter.trace_since(int(request.since))[:TRACE_RESPONSE_MAX]:
                record = make()
                for key, value in found.items():
                    if key in _TRACE_INTS:
                        value = int(value)
                    elif key in _TRACE_FLOATS:
                        value = float(value)
                    else:
                        value = str(value if value is not None else "")
                    setattr(record, key, value)
                records.append(record)
            response.records = records
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._complaints.report("get_move_trace", error)
            response.records = []
        return response

    # --- state -----------------------------------------------------------------------

    def get_full_state(self, request: Any, response: Any) -> Any:
        try:
            fill_full_state(response.state, self._state.snapshot(False))
            response.ok = True
            response.error = ""
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._complaints.report("get_full_state", error)
            response.ok = False
            response.error = f"{type(error).__name__}: {error}"
        return response

    def get_status(self, request: Any, response: Any) -> Any:
        try:
            status = self._state.status()
            backend = status.get("backend_status") or {}
            response.state = str(status.get("state") or "error")
            response.motor_control_mode = str(backend.get("motor_control_mode") or "")
            response.error = str(status.get("error") or "")
            response.ready = bool(backend.get("ready"))
            response.simulation_enabled = bool(status.get("simulation_enabled"))
            response.mockup_sim_enabled = bool(status.get("mockup_sim_enabled"))
            response.no_media = bool(status.get("no_media"))
            response.media_released = bool(status.get("media_released"))
            response.wireless_version = bool(status.get("wireless_version"))
            response.version = str(status.get("version") or "")
            response.robot_name = str(status.get("robot_name") or "")
            response.backend = str(status.get("backend") or self._backend_name)
            response.gaze_run = self._gaze_run_flag()
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._complaints.report("get_status", error)
            response.state = "error"
            response.error = f"{type(error).__name__}: {error}"
            response.ready = False
            response.backend = self._backend_name
            response.gaze_run = False
        return response

    def _gaze_run_flag(self) -> bool:
        if self._gaze_run is None:
            return False
        try:
            return bool(self._gaze_run())
        except Exception as error:  # noqa: BLE001 - a flag nobody can read is off
            self._complaints.report("the gaze run flag", error)
            return False

    # --- gaze and media --------------------------------------------------------------

    def set_gaze(self, request: Any, response: Any) -> Any:
        """``success=false, message="gaze_disabled"`` is the shim's 503: back off."""

        if self._set_gaze is None:
            response.success = False
            response.message = GAZE_DISABLED
            return response
        try:
            self._set_gaze(bool(request.data))
            response.success = True
            response.message = ""
        except Exception as error:  # noqa: BLE001 - see the module docstring
            self._complaints.report("set_gaze", error)
            response.success = False
            response.message = BACKEND_ERROR
        return response

    def play_sound(self, request: Any, response: Any) -> Any:
        self._inert(f"play_sound({getattr(request, 'file', '')})")
        response.ok = True
        return response

    def set_wobbling(self, request: Any, response: Any) -> Any:
        self._inert(f"set_wobbling({bool(getattr(request, 'data', False))})")
        response.success = True
        response.message = ""
        return response

    def _inert(self, what: str) -> None:
        if not self._inert_said.is_set():
            self._inert_said.set()
            self._log.info(
                f"link: {what} answered ok but is inert - this node carries no media "
                "server (EMORI's robot speech goes through the kiosk browser).")


# --- the node ------------------------------------------------------------------------


@dataclass(frozen=True)
class RosBindings:
    """Everything the node wiring takes from ROS, so a test can hand it fakes."""

    create_node: Callable[[str], Any]
    executor_cls: Callable[..., Any]
    group_cls: Callable[[], Any]
    state_qos: Any
    ok: Callable[[], bool]
    fatal: tuple[type[BaseException], ...]
    SubmitMove: Any
    GetFullState: Any
    GetRobotStatus: Any
    GetMoveTrace: Any
    PlaySound: Any
    SetBool: Any
    Header: Any
    MoveTraceRecord: Any


def load_ros_bindings() -> RosBindings:
    """Import ``rclpy`` and the message types. Raises ``ImportError`` where ROS is absent."""

    import rclpy
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Header
    from std_srvs.srv import SetBool

    from reachy_mini_msgs.msg import MoveTraceRecord
    from reachy_mini_msgs.srv import (
        GetFullState,
        GetMoveTrace,
        GetRobotStatus,
        PlaySound,
        SubmitMove,
    )

    def link_node(name: str) -> Any:
        # The launch file names the robot node with a remap, and a remap on the command line
        # applies to every node in the process, which would make this one /reachy_mini too.
        return Node(name, use_global_arguments=False, start_parameter_services=False)

    return RosBindings(
        create_node=link_node,
        executor_cls=MultiThreadedExecutor,
        group_cls=ReentrantCallbackGroup,
        state_qos=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE),
        ok=rclpy.ok,
        fatal=(ExternalShutdownException,),
        SubmitMove=SubmitMove,
        GetFullState=GetFullState,
        GetRobotStatus=GetRobotStatus,
        GetMoveTrace=GetMoveTrace,
        PlaySound=PlaySound,
        SetBool=SetBool,
        Header=Header,
        MoveTraceRecord=MoveTraceRecord,
    )


class LinkSurface:
    """The ``reachy_mini_link`` node, its executor and its thread."""

    NODE_NAME = "reachy_mini_link"

    def __init__(
        self,
        handlers: LinkHandlers,
        bindings: RosBindings,
        *,
        log: Any | None = None,
        threads: int = LINK_THREADS,
        heartbeat_period_s: float = HEARTBEAT_PERIOD_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handlers = handlers
        self._ros = bindings
        self._log = _Log(log)
        self._threads = max(2, int(threads))
        self._period = heartbeat_period_s
        self._complaints = _Complaints(self._log, clock, COMPLAINT_PERIOD_S)
        self._stop = threading.Event()
        self._node: Any = None
        self._executor: Any = None
        self._thread: threading.Thread | None = None
        self._heartbeat: Any = None

    @property
    def node(self) -> Any:
        return self._node

    def start(self) -> None:
        ros = self._ros
        node = ros.create_node(self.NODE_NAME)
        group = ros.group_cls()
        handlers = self._handlers
        for srv_type, name, callback in (
            (ros.SubmitMove, SUBMIT_MOVE, handlers.submit_move),
            (ros.GetFullState, GET_FULL_STATE, handlers.get_full_state),
            (ros.GetRobotStatus, GET_STATUS, handlers.get_status),
            (ros.GetMoveTrace, GET_MOVE_TRACE, handlers.get_move_trace),
            (ros.SetBool, SET_GAZE, handlers.set_gaze),
            (ros.PlaySound, PLAY_SOUND, handlers.play_sound),
            (ros.SetBool, SET_WOBBLING, handlers.set_wobbling),
        ):
            node.create_service(srv_type, name, callback, callback_group=group)
        self._heartbeat = node.create_publisher(ros.Header, HEARTBEAT_TOPIC, ros.state_qos)
        node.create_timer(self._period, self._beat, callback_group=group)

        executor = ros.executor_cls(num_threads=self._threads)
        executor.add_node(node)
        self._node = node
        self._executor = executor
        self._thread = threading.Thread(
            target=logged_spin, name=self.NODE_NAME, daemon=True,
            args=(executor, self._log),
            kwargs={"stop": self._stop, "ok": ros.ok, "fatal": ros.fatal})
        self._thread.start()
        self._log.info(f"{self.NODE_NAME} up: submit_move, get_full_state, get_status, "
                       "get_move_trace, set_gaze and the 2 Hz link_heartbeat")

    def _beat(self) -> None:
        try:
            message = self._ros.Header()
            message.stamp = self._node.get_clock().now().to_msg()
            message.frame_id = ""
            self._heartbeat.publish(message)
        except Exception as error:  # noqa: BLE001 - a missed beat must not end the timer
            self._complaints.report("the link heartbeat", error)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop spinning and release the node. Never raises."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        for release in (getattr(self._executor, "shutdown", None),
                        getattr(self._node, "destroy_node", None)):
            if callable(release):
                try:
                    release()
                except Exception as error:  # noqa: BLE001 - teardown is best effort
                    self._log.warning(f"link teardown: {error}")
        self._executor = None
        self._node = None


def start_link_surface(
    arbiter: Any,
    state: Any,
    *,
    set_gaze: Callable[[bool], None] | None = None,
    gaze_run: Callable[[], bool] | None = None,
    backend_name: str | None = None,
    log: Any | None = None,
    bindings: Optional[RosBindings] = None,
) -> LinkSurface:
    """Build the handlers and the node, and start spinning. Returns the running surface."""

    ros = bindings if bindings is not None else load_ros_bindings()
    handlers = LinkHandlers(
        arbiter, state, set_gaze=set_gaze, gaze_run=gaze_run,
        trace_record=ros.MoveTraceRecord, backend_name=backend_name, log=log)
    surface = LinkSurface(handlers, ros, log=log)
    surface.start()
    return surface
