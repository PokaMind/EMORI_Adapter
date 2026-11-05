"""``ros_surface``: the robot's link services, with fakes where ROS would be.

``rclpy`` and the generated message types exist only in the node's image, so this module
is loaded BY PATH and everything that would touch ROS is injected. Two things keep the
fakes honest:

* every request and response here is built from the real ``.srv`` and ``.msg`` files in
  ``reachy_mini_msgs``, and refuses an attribute the file does not declare or a value of
  the wrong type. A handler that writes a field the interface lacks fails here, not on the
  robot.
* the arbiter behind the handlers records what it was asked, and the real
  ``MotionRequest`` is the request class, so supersede and the trace tag are checked on
  the object the arbiter would get.

The node wiring is driven through a fake executor that, like Jazzy's, re-raises a
callback's exception out of the call that spins it. The raising-handler tests are the ones
that matter: they make a handler raise and check the heartbeat is still published.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import math
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

_OPEN = Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open"
_PACKAGE = _OPEN / "reachy_mini_ros2" / "reachy_mini_ros2"
_MSGS = _OPEN / "reachy_mini_msgs"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


surface = _load("reachy_mini_ros_surface_under_test", _PACKAGE / "ros_surface.py")
arbiter_mod = _load("reachy_mini_arbiter_for_ros_surface", _PACKAGE / "arbiter.py")
snap = surface._load_state_snapshot()


# --- messages built from the interface files ------------------------------------------

_PRIMITIVES = {
    "bool": (bool, False),
    "string": (str, ""),
    "float64": (float, 0.0),
    "float32": (float, 0.0),
    "int32": (int, 0),
    "int64": (int, 0),
    "uint32": (int, 0),
    "uint64": (int, 0),
}


def _lines(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]


class Message:
    """An interface instance that accepts only its own fields, with their types."""

    def __init__(self, fields: dict[str, Any], kinds: dict[str, Any]) -> None:
        object.__setattr__(self, "_kinds", kinds)
        for key, value in fields.items():
            object.__setattr__(self, key, value)

    def __setattr__(self, key: str, value: Any) -> None:
        kinds = self._kinds
        if key not in kinds:
            raise AttributeError(f"{key} is not a field of this interface")
        kind = kinds[key]
        if isinstance(kind, tuple):
            element, size = kind
            if not isinstance(value, (list, tuple)) or (size and len(value) != size):
                raise TypeError(f"{key} must be a list of {size or 'any number of'}")
            for item in value:
                self._check(key, element, item)
        else:
            self._check(key, kind, value)
        object.__setattr__(self, key, value)

    @staticmethod
    def _check(key: str, kind: Any, value: Any) -> None:
        if isinstance(kind, type):
            if kind is float and isinstance(value, bool):
                raise TypeError(f"{key} must be a float")
            if kind is int and isinstance(value, bool):
                raise TypeError(f"{key} must be an int")
            if kind is float and isinstance(value, int):
                raise TypeError(f"{key} must be a float, not an int")
            if not isinstance(value, kind):
                raise TypeError(f"{key} must be {kind.__name__}, got {type(value).__name__}")


def _build(body: list[str]) -> Message:
    fields: dict[str, Any] = {}
    kinds: dict[str, Any] = {}
    for line in body:
        type_name, name = line.split()[:2]
        array = None
        if type_name.endswith("]"):
            type_name, _, size = type_name[:-1].partition("[")
            array = int(size) if size else 0
        if type_name in _PRIMITIVES:
            kind, default = _PRIMITIVES[type_name]
            fields[name] = ([default] * array if array else []) if array is not None else default
            kinds[name] = (kind, array) if array is not None else kind
        elif array is not None:
            fields[name], kinds[name] = [], (object, array)
        elif type_name == "builtin_interfaces/Time":
            fields[name] = _build(["int32 sec", "uint32 nanosec"])
            kinds[name] = object
        else:
            fields[name] = _build(_lines(_MSGS / "msg" / f"{type_name}.msg"))
            kinds[name] = object
    return Message(fields, kinds)


def _srv(name: str) -> tuple[Any, Any]:
    body = _lines(_MSGS / "srv" / f"{name}.srv")
    split = body.index("---")
    return (lambda: _build(body[:split])), (lambda: _build(body[split + 1:]))


def _msg(name: str):
    return lambda: _build(_lines(_MSGS / "msg" / f"{name}.msg"))


def _set_bool():
    return (lambda: _build(["bool data"])), (lambda: _build(["bool success", "string message"]))


def _pose(**fields: float) -> Message:
    pose = _msg("PoseRpy")()
    for key, value in fields.items():
        setattr(pose, key, float(value))
    return pose


def _submit(**fields: Any) -> Message:
    request = _srv("SubmitMove")[0]()
    for key, value in fields.items():
        setattr(request, key, value)
    return request


# --- the fakes behind the handlers ----------------------------------------------------


class Backend:
    def __init__(self) -> None:
        self.kind = "mujoco"
        self.mode = "enabled"
        self.ready = True
        self.fault = ""
        self.commanded: np.ndarray | None = None
        self.raise_on: set[str] = set()
        self.joints = [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26, 0.31, -0.32]

    def _maybe_raise(self, what: str) -> None:
        if what in self.raise_on:
            raise RuntimeError(f"{what} failed")

    def is_ready(self) -> bool:
        self._maybe_raise("is_ready")
        return self.ready

    def error(self) -> str:
        return self.fault

    def present_head_pose(self) -> np.ndarray:
        self._maybe_raise("present_head_pose")
        return snap._local_xyzrpy_to_matrix(0.001, 0.002, 0.012, 0.05, 0.1, -0.25)

    def commanded_head_pose(self) -> np.ndarray | None:
        return self.commanded

    def present_joints(self) -> list[float]:
        self._maybe_raise("present_joints")
        return list(self.joints)

    def motor_mode(self) -> str:
        return self.mode


class Arbiter:
    """What the handlers ask of the arbiter, recording and raising on demand."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.outcome = SimpleNamespace(accepted=True, error_code="", move_id="m-1")
        self.fail = False
        self.records: list[dict[str, Any]] = []
        self.trace_asked: list[int] = []
        self.motion = {"active": True, "head_moving": False, "head_reserved": False,
                       "client": "hostapp", "lane": "interaction"}

    def submit(self, request: Any) -> Any:
        if self.fail:
            raise RuntimeError("the arbiter fell over")
        self.submitted.append(request)
        return self.outcome

    def trace_since(self, after: int) -> list[dict[str, Any]]:
        self.trace_asked.append(after)
        if self.fail:
            raise RuntimeError("the arbiter fell over")
        return list(self.records)

    def motion_snapshot(self) -> dict:
        return dict(self.motion)


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.lines.append(("info", message))

    def warning(self, message: str) -> None:
        self.lines.append(("warning", message))

    def error(self, message: str) -> None:
        self.lines.append(("error", message))

    def of(self, level: str) -> list[str]:
        return [text for kind, text in self.lines if kind == level]


class Gaze:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.run = False
        self.active = False
        self.fail = False

    def set(self, enabled: bool) -> None:
        if self.fail:
            raise RuntimeError("gaze fell over")
        self.calls.append(enabled)
        self.run = enabled


class Bench:
    """Handlers over a real state factory, a fake arbiter and the real MotionRequest."""

    def __init__(self, *, with_gaze: bool = True, clock=None) -> None:
        self.backend = Backend()
        self.arbiter = Arbiter()
        self.gaze = Gaze()
        self.log = Log()
        self.state = snap.make_state_snapshot(
            self.arbiter, self.backend, state_cache_s=1e9,
            gaze_active=(lambda: self.gaze.active) if with_gaze else None,
            face_seen=(lambda: 1.234) if with_gaze else None)
        self.handlers = surface.LinkHandlers(
            self.arbiter, self.state,
            set_gaze=self.gaze.set if with_gaze else None,
            gaze_run=(lambda: self.gaze.run) if with_gaze else None,
            trace_record=_msg("MoveTraceRecord"),
            request_cls=arbiter_mod.MotionRequest,
            log=self.log, **({"clock": clock} if clock else {}))

    _KINDS = {"submit_move": "SubmitMove", "get_full_state": "GetFullState",
              "get_status": "GetRobotStatus", "get_move_trace": "GetMoveTrace",
              "play_sound": "PlaySound"}

    def call(self, name: str, request: Any) -> Any:
        _, response = _srv(self._KINDS[name])
        return getattr(self.handlers, name)(request, response())


@pytest.fixture
def bench() -> Bench:
    return Bench()


# --- the module stands alone ----------------------------------------------------------


def test_the_surface_imports_no_ros_when_it_is_loaded():
    tree = ast.parse((_PACKAGE / "ros_surface.py").read_text(encoding="utf-8"))
    forbidden = {"rclpy", "std_msgs", "std_srvs", "reachy_mini_msgs", "reachy_mini",
                 "fastapi", "uvicorn"}
    top_level = {alias.name.split(".")[0] for node in tree.body
                 if isinstance(node, ast.Import) for alias in node.names}
    top_level |= {node.module.split(".")[0] for node in tree.body
                  if isinstance(node, ast.ImportFrom) and node.module and not node.level}
    assert top_level & forbidden == set()


# --- submit_move ----------------------------------------------------------------------


def test_a_goto_reaches_the_arbiter_with_supersede_the_trace_tag_and_the_head_matrix(bench):
    request = _submit(
        kind="goto", lane="expression", client="hub", use_head=True,
        head_pose=_pose(x=0.001, z=0.012, roll=0.05, pitch=0.1, yaw=-0.25),
        use_antennas=True, antennas=[0.3, -0.3], use_body_yaw=True, body_yaw=0.2,
        duration=1.5, interpolation="cartoon", preempt=True, supersede=True,
        reason="plan-7:cue-3")
    response = bench.call("submit_move", request)

    assert (response.accepted, response.error_code, response.move_id) == (True, "", "m-1")
    (sent,) = bench.arbiter.submitted
    assert isinstance(sent, arbiter_mod.MotionRequest)
    assert (sent.kind, sent.lane, sent.client) == ("goto", "expression", "hub")
    assert sent.supersede is True and sent.preempt is True
    assert sent.reason == "plan-7:cue-3"
    assert sent.antennas == (0.3, -0.3) and sent.body_yaw == 0.2
    assert sent.duration == 1.5 and sent.interpolation == "cartoon"
    expected = snap._local_xyzrpy_to_matrix(0.001, 0.0, 0.012, 0.05, 0.1, -0.25)
    assert np.allclose(sent.head_pose, expected)


def test_a_field_that_is_not_marked_used_is_not_sent(bench):
    bench.call("submit_move", _submit(
        kind="goto", head_pose=_pose(x=9), antennas=[1.0, 1.0], body_yaw=9.0, duration=0.5))
    (sent,) = bench.arbiter.submitted
    assert sent.head_pose is None and sent.antennas is None and sent.body_yaw is None
    assert sent.supersede is False


def test_a_recorded_move_names_its_dataset_and_move(bench):
    bench.call("submit_move", _submit(kind="play_recorded", dataset="emotions", move="happy1",
                                    client="hub", lane="interaction"))
    (sent,) = bench.arbiter.submitted
    assert (sent.kind, sent.dataset, sent.move) == ("play_recorded", "emotions", "happy1")


def test_an_empty_lane_client_and_interpolation_take_the_arbiter_defaults(bench):
    bench.call("submit_move", _submit(kind="goto", duration=0.5))
    (sent,) = bench.arbiter.submitted
    assert sent.lane == arbiter_mod.DEFAULT_LANE
    assert sent.client == arbiter_mod.DEFAULT_CLIENT
    assert sent.interpolation == arbiter_mod.DEFAULT_INTERPOLATION


def test_a_refusal_carries_the_arbiters_code_and_no_move_id(bench):
    bench.arbiter.outcome = SimpleNamespace(accepted=False, error_code="head_reserved",
                                          move_id="")
    response = bench.call("submit_move", _submit(kind="goto", duration=0.5))
    assert (response.accepted, response.error_code, response.move_id) == (
        False, "head_reserved", "")


def test_a_reason_longer_than_the_limit_is_cut(bench):
    bench.call("submit_move", _submit(kind="goto", duration=0.5, reason="x" * 5000))
    assert len(bench.arbiter.submitted[0].reason) == surface.REASON_LIMIT


def test_an_arbiter_that_raises_is_a_backend_error_and_not_an_exception(bench):
    bench.arbiter.fail = True
    response = bench.call("submit_move", _submit(kind="goto", duration=0.5))
    assert (response.accepted, response.error_code, response.move_id) == (
        False, "backend_error", "")
    assert any("submit_move failed" in line for line in bench.log.of("error"))


@pytest.mark.parametrize("fields", [
    lambda: {"use_head": True, "head_pose": _pose(x=math.nan)},
    lambda: {"use_antennas": True, "antennas": [math.nan, 0.0]},
    lambda: {"use_body_yaw": True, "body_yaw": math.inf},
    lambda: {"duration": math.nan},
], ids=["head", "antennas", "body_yaw", "duration"])
def test_input_that_is_not_a_number_never_reaches_the_arbiter(bench, fields):
    wanted = {"kind": "goto", "duration": 0.5, **fields()}
    response = bench.call("submit_move", _submit(**wanted))
    assert (response.accepted, response.error_code) == (False, "backend_error")
    assert bench.arbiter.submitted == []


# --- get_full_state -------------------------------------------------------------------


def test_the_state_service_answers_what_the_rest_snapshot_says(bench):
    bench.backend.commanded = snap._local_xyzrpy_to_matrix(0.0, 0.0, 0.012, 0.0, 0.0, 0.0)
    bench.gaze.active = True
    expected = bench.state.snapshot(False)
    response = bench.call("get_full_state", _srv("GetFullState")[0]())

    assert response.ok is True and response.error == ""
    state = response.state
    assert state.control_mode == expected["control_mode"] == "enabled"
    for key in ("x", "y", "z", "roll", "pitch", "yaw"):
        assert getattr(state.head_pose, key) == pytest.approx(expected["head_pose"][key])
    assert state.head_joints == expected["head_joints"]
    assert len(state.head_joints) == 7
    assert state.body_yaw == expected["body_yaw"]
    assert state.antennas_position == expected["antennas_position"] == [0.31, -0.32]
    assert state.has_motion is True
    motion, rest = state.motion, expected["motion"]
    assert (motion.active, motion.head_moving, motion.head_reserved) == (True, False, False)
    assert (motion.client, motion.lane) == ("hostapp", "interaction")
    assert motion.gaze_active is True
    assert motion.has_face_seen is True and motion.face_seen_s == rest["face_seen_s"] == 1.23
    assert motion.has_command and motion.has_residual
    for key in ("roll", "pitch", "yaw"):
        assert getattr(motion.command, key) == pytest.approx(rest["command"][key])
        assert getattr(motion.residual, key) == pytest.approx(rest["residual"][key])


def test_the_stamp_is_when_the_backend_was_read(bench):
    expected = bench.state.snapshot(False)["timestamp"]
    response = bench.call("get_full_state", _srv("GetFullState")[0]())
    sec, nanosec = surface._stamp_of(expected)
    assert (response.state.stamp.sec, response.state.stamp.nanosec) == (sec, nanosec)
    assert sec > 1_700_000_000


def test_a_node_without_commanded_pose_or_face_says_so():
    bench = Bench(with_gaze=False)
    response = bench.call("get_full_state", _srv("GetFullState")[0]())
    motion = response.state.motion
    assert response.ok is True
    assert motion.has_command is False and motion.has_residual is False
    assert motion.has_face_seen is False and motion.face_seen_s == 0.0
    assert motion.gaze_active is False


def test_an_arbiter_that_cannot_describe_motion_leaves_has_motion_false(bench):
    bench.arbiter.motion_snapshot = None
    response = bench.call("get_full_state", _srv("GetFullState")[0]())
    assert response.ok is True and response.state.has_motion is False


@pytest.mark.parametrize("failing", ["present_head_pose", "present_joints"])
def test_a_backend_that_raises_is_ok_false_with_the_reason(bench, failing):
    bench.backend.raise_on.add(failing)
    response = bench.call("get_full_state", _srv("GetFullState")[0]())
    assert response.ok is False
    assert failing in response.error
    assert response.state.head_joints == []


def test_a_reading_with_no_joints_is_not_a_zero_pose(bench):
    bench.backend.joints = []
    response = bench.call("get_full_state", _srv("GetFullState")[0]())
    assert response.ok is False and "no joints" in response.error
    assert response.state.body_yaw == 0.0 and response.state.head_joints == []


def test_a_failure_that_repeats_is_logged_once_per_period():
    now = [0.0]
    bench = Bench(clock=lambda: now[0])
    bench.backend.raise_on.add("present_joints")
    for _ in range(50):
        bench.call("get_full_state", _srv("GetFullState")[0]())
    assert len(bench.log.of("error")) == 1
    now[0] = surface.COMPLAINT_PERIOD_S + 1
    bench.call("get_full_state", _srv("GetFullState")[0]())
    assert len(bench.log.of("error")) == 2
    assert "49 more" in bench.log.of("error")[1]


# --- get_status -----------------------------------------------------------------------


def test_the_status_service_answers_what_the_rest_status_says(bench):
    bench.gaze.run = True
    expected = bench.state.status()
    response = bench.call("get_status", _srv("GetRobotStatus")[0]())

    assert response.state == expected["state"] == "running"
    assert response.motor_control_mode == expected["backend_status"]["motor_control_mode"]
    assert response.ready is True and response.error == ""
    assert response.simulation_enabled is expected["simulation_enabled"]
    assert response.mockup_sim_enabled is expected["mockup_sim_enabled"]
    assert response.no_media is True and response.media_released is False
    assert response.wireless_version is expected["wireless_version"]
    assert response.version == expected["version"]
    assert response.robot_name == expected["robot_name"]
    assert response.backend == "reachy_mini_ros2"


def test_a_backend_that_is_not_ready_is_state_error_with_its_reason(bench):
    bench.backend.ready = False
    bench.backend.fault = "the serial bus is gone"
    expected = bench.state.status()
    response = bench.call("get_status", _srv("GetRobotStatus")[0]())
    assert response.state == expected["state"] == "error"
    assert response.ready is False
    assert response.error == expected["error"] == "the serial bus is gone"


def test_a_status_read_that_raises_is_state_error_and_not_an_exception(bench):
    bench.state = SimpleNamespace(status=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    handlers = surface.LinkHandlers(bench.arbiter, bench.state, log=bench.log)
    response = handlers.get_status(None, _srv("GetRobotStatus")[1]())
    assert response.state == "error" and "boom" in response.error
    assert response.ready is False and response.backend == "reachy_mini_ros2"


def test_gaze_run_is_the_run_flag_and_not_the_activity(bench):
    bench.gaze.run, bench.gaze.active = False, True
    assert bench.call("get_status", _srv("GetRobotStatus")[0]()).gaze_run is False
    bench.gaze.run, bench.gaze.active = True, False
    assert bench.call("get_status", _srv("GetRobotStatus")[0]()).gaze_run is True


def test_gaze_run_is_false_on_a_node_without_gaze():
    bench = Bench(with_gaze=False)
    assert bench.call("get_status", _srv("GetRobotStatus")[0]()).gaze_run is False


# --- get_move_trace -------------------------------------------------------------------


def _trace_record(seq: int) -> dict[str, Any]:
    values = {"seq": seq, "t": 12.5 + seq, "event": "started", "move_id": f"m-{seq}",
              "kind": "goto", "client": "hub", "lane": "expression",
              "reason": "plan-1:cue-2", "error_code": "", "queued": 2}
    assert tuple(values) == arbiter_mod.TRACE_FIELDS
    return values


def test_the_trace_service_returns_the_records_the_arbiter_holds(bench):
    bench.arbiter.records = [_trace_record(5), _trace_record(6)]
    response = bench.call("get_move_trace", _srv("GetMoveTrace")[0]())
    assert [record.seq for record in response.records] == [5, 6]
    first = response.records[0]
    assert (first.t, first.event, first.move_id) == (17.5, "started", "m-5")
    assert (first.kind, first.client, first.lane) == ("goto", "hub", "expression")
    assert (first.reason, first.error_code, first.queued) == ("plan-1:cue-2", "", 2)


def test_the_trace_service_passes_since_through_including_the_newest_only_form(bench):
    request = _srv("GetMoveTrace")[0]()
    request.since = 41
    bench.call("get_move_trace", request)
    request.since = -1
    bench.call("get_move_trace", request)
    assert bench.arbiter.trace_asked == [41, -1]


def test_the_trace_service_matches_the_real_arbiters_trace(bench):
    """The records come out of ``trace_since`` itself, so a renamed field breaks here."""

    real = arbiter_mod.MotionArbiter.__new__(arbiter_mod.MotionArbiter)
    real._lock = threading.Lock()
    real._trace = [tuple(_trace_record(n).values()) for n in (1, 2, 3)]
    bench.arbiter.trace_since = real.trace_since
    request = _srv("GetMoveTrace")[0]()
    request.since = 1
    assert [r.seq for r in bench.call("get_move_trace", request).records] == [2, 3]
    request.since = -1
    assert [r.seq for r in bench.call("get_move_trace", request).records] == [3]


def test_a_trace_read_that_raises_is_an_empty_list_and_a_log_line(bench):
    bench.arbiter.fail = True
    response = bench.call("get_move_trace", _srv("GetMoveTrace")[0]())
    assert response.records == []
    assert any("get_move_trace failed" in line for line in bench.log.of("error"))


# --- gaze and media -------------------------------------------------------------------


def _bool(value: bool) -> Message:
    request = _set_bool()[0]()
    request.data = value
    return request


def _set_bool_call(handler, request):
    return handler(request, _set_bool()[1]())


def test_set_gaze_drives_the_run_switch(bench):
    on = _set_bool_call(bench.handlers.set_gaze, _bool(True))
    off = _set_bool_call(bench.handlers.set_gaze, _bool(False))
    assert (on.success, off.success) == (True, True)
    assert bench.gaze.calls == [True, False]


def test_set_gaze_on_a_node_without_gaze_says_gaze_disabled():
    bench = Bench(with_gaze=False)
    response = _set_bool_call(bench.handlers.set_gaze, _bool(True))
    assert (response.success, response.message) == (False, "gaze_disabled")


def test_set_gaze_that_raises_is_not_a_success(bench):
    bench.gaze.fail = True
    response = _set_bool_call(bench.handlers.set_gaze, _bool(True))
    assert (response.success, response.message) == (False, "backend_error")


def test_the_media_services_answer_ok_and_say_so_once(bench):
    sound = _srv("PlaySound")[0]()
    sound.file = "chime.wav"
    assert bench.call("play_sound", sound).ok is True
    assert _set_bool_call(bench.handlers.set_wobbling, _bool(True)).success is True
    assert _set_bool_call(bench.handlers.set_wobbling, _bool(False)).success is True
    assert len(bench.log.of("info")) == 1
    assert "inert" in bench.log.of("info")[0]


# --- the spin -------------------------------------------------------------------------


class _Executor:
    """Jazzy's behaviour that matters: a callback's exception comes out of ``spin_once``."""

    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.nodes: list[Any] = []
        self.threads: int | None = None
        self.shut_down = False
        self.spun = 0

    def add_node(self, node: Any) -> None:
        self.nodes.append(node)

    def spin_once(self, timeout_sec: float = 0.0) -> None:
        self.spun += 1
        try:
            callback = self.inbox.get(timeout=timeout_sec)
        except queue.Empty:
            return
        callback()

    def shutdown(self) -> None:
        self.shut_down = True

    def run(self, callback, wait: float = 2.0) -> None:
        """Post ``callback`` and return once the spin has run it (and raised, if it did)."""

        done = threading.Event()

        def wrapped() -> None:
            try:
                callback()
            finally:
                done.set()

        self.inbox.put(wrapped)
        assert done.wait(wait), "the spin never ran the callback"


def _spin_in_thread(executor, log, **kwargs):
    stop = threading.Event()
    thread = threading.Thread(
        target=surface.logged_spin, args=(executor, log), kwargs={"stop": stop, **kwargs},
        daemon=True)
    thread.start()
    return stop, thread


def test_the_spin_logs_an_exception_that_escapes_a_callback_and_goes_on():
    executor, log = _Executor(), Log()
    stop, thread = _spin_in_thread(executor, log)
    ran: list[str] = []

    def explode():
        raise RuntimeError("a handler raised")

    executor.run(explode)
    executor.run(lambda: ran.append("after"))
    stop.set()
    thread.join(2)

    assert ran == ["after"] and not thread.is_alive()
    assert any("a handler raised" in line for line in log.of("error"))


def test_the_spin_ends_quietly_on_the_exceptions_named_fatal():
    class Gone(Exception):
        pass

    class Dying(_Executor):
        def spin_once(self, timeout_sec: float = 0.0) -> None:
            raise Gone()

    log = Log()
    thread = threading.Thread(target=surface.logged_spin, args=(Dying(), log),
                              kwargs={"fatal": (Gone,)}, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and log.of("error") == []


def test_the_spin_ends_when_the_context_goes_away():
    alive = [True]
    executor = _Executor()
    thread = threading.Thread(target=surface.logged_spin, args=(executor, Log()),
                              kwargs={"ok": lambda: alive[0]}, daemon=True)
    thread.start()
    alive[0] = False
    thread.join(2)
    assert not thread.is_alive()


# --- the node -------------------------------------------------------------------------


class _Publisher:
    def __init__(self, kind: Any, name: str, qos: Any) -> None:
        self.kind, self.name, self.qos = kind, name, qos
        self.messages: list[Any] = []
        self.fail = False

    def publish(self, message: Any) -> None:
        if self.fail:
            raise RuntimeError("the context is invalid")
        self.messages.append(message)


class _Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.services: dict[str, tuple[Any, Any, Any]] = {}
        self.publishers: list[_Publisher] = []
        self.timers: list[tuple[float, Any, Any]] = []
        self.destroyed = False
        self.clock = SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: SimpleNamespace(sec=7, nanosec=9)))

    def create_service(self, kind, name, callback, callback_group=None):
        self.services[name] = (kind, callback, callback_group)

    def create_publisher(self, kind, name, qos):
        publisher = _Publisher(kind, name, qos)
        self.publishers.append(publisher)
        return publisher

    def create_timer(self, period, callback, callback_group=None):
        self.timers.append((period, callback, callback_group))

    def get_clock(self):
        return self.clock

    def destroy_node(self) -> None:
        self.destroyed = True


class _Header:
    """``std_msgs/Header``: a stamp and a frame id."""

    def __init__(self) -> None:
        self.stamp: Any = None
        self.frame_id = ""


def _bindings(executor: _Executor, node: _Node):
    def make_executor(num_threads: int):
        executor.threads = num_threads
        return executor

    kinds = {name: SimpleNamespace(name=name) for name in (
        "SubmitMove", "GetFullState", "GetRobotStatus", "GetMoveTrace", "PlaySound", "SetBool")}
    return surface.RosBindings(
        create_node=lambda name: node, executor_cls=make_executor,
        group_cls=lambda: SimpleNamespace(reentrant=True),
        state_qos=SimpleNamespace(best_effort=True), ok=lambda: True, fatal=(),
        Header=_Header, MoveTraceRecord=_msg("MoveTraceRecord"), **kinds)


@pytest.fixture
def link():
    bench = Bench()
    executor, node = _Executor(), _Node("unbuilt")
    bindings = _bindings(executor, node)
    link = surface.LinkSurface(bench.handlers, bindings, log=bench.log)
    link.start()
    yield SimpleNamespace(link=link, bench=bench, executor=executor, node=node,
                          beat=node.timers[0][1], heartbeat=node.publishers[0])
    link.stop()


def test_the_node_serves_every_link_service_on_one_reentrant_group_and_two_threads(link):
    assert set(link.node.services) == {
        "/reachy_mini/submit_move", "/reachy_mini/get_full_state", "/reachy_mini/get_status",
        "/reachy_mini/get_move_trace", "/reachy_mini/set_gaze", "/reachy_mini/play_sound",
        "/reachy_mini/set_wobbling"}
    types = {name: kind.name for name, (kind, _, _) in link.node.services.items()}
    assert types["/reachy_mini/submit_move"] == "SubmitMove"
    assert types["/reachy_mini/get_full_state"] == "GetFullState"
    assert types["/reachy_mini/get_status"] == "GetRobotStatus"
    assert types["/reachy_mini/get_move_trace"] == "GetMoveTrace"
    assert types["/reachy_mini/set_gaze"] == types["/reachy_mini/set_wobbling"] == "SetBool"
    groups = {id(group) for _, _, group in link.node.services.values()}
    groups |= {id(group) for _, _, group in link.node.timers}
    assert len(groups) == 1
    assert link.executor.threads == 2
    assert link.executor.nodes == [link.node]


def test_the_heartbeat_is_a_best_effort_header_at_two_hertz(link):
    (publisher,) = link.node.publishers
    assert publisher.name == "/reachy_mini/link_heartbeat"
    assert publisher.kind is _Header
    assert publisher.qos.best_effort is True
    assert link.node.timers[0][0] == 0.5
    link.executor.run(link.beat)
    (message,) = publisher.messages
    assert (message.stamp.sec, message.stamp.nanosec) == (7, 9)


def _beats(link) -> int:
    return len(link.heartbeat.messages)


def test_a_backend_that_raises_inside_a_handler_leaves_the_heartbeat_running(link):
    link.bench.backend.raise_on.add("present_joints")
    link.bench.arbiter.fail = True
    before = _beats(link)
    kind, callback, _ = link.node.services["/reachy_mini/get_full_state"]
    _, make_response = _srv("GetFullState")
    answered: list[Any] = []
    link.executor.run(lambda: answered.append(callback(_srv("GetFullState")[0](),
                                                       make_response())))
    link.executor.run(link.beat)
    link.executor.run(
        lambda: answered.append(link.node.services["/reachy_mini/submit_move"][1](
            _submit(kind="goto", duration=0.5), _srv("SubmitMove")[1]())))
    link.executor.run(link.beat)

    assert answered[0].ok is False and answered[1].error_code == "backend_error"
    assert _beats(link) == before + 2


def test_a_handler_that_raises_past_its_own_catch_all_does_not_stop_the_spin(link):
    """The response itself refuses the failure answer, so the exception gets out."""

    class Poisoned:
        def __setattr__(self, key, value):
            raise RuntimeError("cannot write the response")

    link.bench.backend.raise_on.add("present_joints")
    callback = link.node.services["/reachy_mini/get_full_state"][1]
    link.executor.run(lambda: callback(_srv("GetFullState")[0](), Poisoned()))
    before = _beats(link)
    link.executor.run(link.beat)
    link.executor.run(link.beat)
    assert _beats(link) == before + 2
    assert any("the link executor failed" in line for line in link.bench.log.of("error"))
    answered = link.node.services["/reachy_mini/get_status"][1](
        _srv("GetRobotStatus")[0](), _srv("GetRobotStatus")[1]())
    assert answered.state in ("running", "error")


def test_a_heartbeat_that_fails_to_publish_does_not_raise(link):
    link.heartbeat.fail = True
    link.beat()
    link.heartbeat.fail = False
    link.beat()
    assert _beats(link) == 1
    assert any("heartbeat failed" in line for line in link.bench.log.of("error"))


def test_stopping_ends_the_thread_and_releases_the_node(link):
    thread = link.link._thread
    assert thread is not None and thread.is_alive()
    link.link.stop()
    assert not thread.is_alive()
    assert link.executor.shut_down and link.node.destroyed


def test_stopping_never_raises_when_the_context_is_already_gone():
    bench = Bench()
    executor, node = _Executor(), _Node("n")

    def refuse() -> None:
        raise RuntimeError("context is invalid")

    executor.shutdown = refuse
    node.destroy_node = refuse
    link = surface.LinkSurface(bench.handlers, _bindings(executor, node), log=bench.log)
    link.start()
    link.stop()
    assert len(bench.log.of("warning")) == 2


def test_start_link_surface_builds_the_handlers_and_starts_the_node():
    bench = Bench()
    executor, node = _Executor(), _Node("n")
    running = surface.start_link_surface(
        bench.arbiter, bench.state, set_gaze=bench.gaze.set, gaze_run=lambda: bench.gaze.run,
        log=bench.log, bindings=_bindings(executor, node))
    try:
        assert "/reachy_mini/submit_move" in node.services
        assert running.node is node
    finally:
        running.stop()


def test_real_loggers_are_accepted(caplog):
    """The stdlib logger and rclpy's both end up behind the same three methods."""

    bench = Bench()
    handlers = surface.LinkHandlers(bench.arbiter, bench.state, log=logging.getLogger("t"))
    bench.arbiter.fail = True
    with caplog.at_level(logging.ERROR):
        handlers.submit_move(_submit(kind="goto", duration=0.5), _srv("SubmitMove")[1]())
    assert "submit_move failed" in caplog.text


@pytest.mark.parametrize("kind", ["", "gto"])
def test_a_kind_the_queue_cannot_run_is_refused_at_once(bench, kind):
    response = bench.call("submit_move", _submit(kind=kind, duration=0.5))
    assert (response.accepted, response.error_code) == (False, "backend_error")
    assert bench.arbiter.submitted == []


def test_a_long_trace_comes_back_in_pieces_oldest_first(bench):
    limit = surface.TRACE_RESPONSE_MAX
    bench.arbiter.records = [_trace_record(seq) for seq in range(1, limit + 11)]
    response = bench.call("get_move_trace", _srv("GetMoveTrace")[0]())
    assert [record.seq for record in response.records] == list(range(1, limit + 1))
