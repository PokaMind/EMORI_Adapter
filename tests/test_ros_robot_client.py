"""``RosRobotClient``, on a machine with no ROS: what "the link is down" does, call by call.

``rclpy`` and the generated types exist only in the hub's image, so the package is put on
``sys.path`` and every service client is a fake. Every request and response here is built
from the real ``.srv`` files and refuses a field they do not declare, so a client that writes
something the interface lacks fails here and not on the robot.

Each failure below has a REST twin in ``HttpRobotClient``, which the code above the client
was written against. ``tests/test_robot_client_contract.py`` runs the two side by side.
"""

from __future__ import annotations

import ast
import logging
import math
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from emori_adapter import motion_trace
from emori_adapter.models import HeadPose, MotionIntent

from tests.test_reachy_ros_surface import _msg, _set_bool, _srv

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
_OPEN = _ROOT / "ros2_ws" / "src" / "open"
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from emori_hub import robot_link  # noqa: E402

RosRobotClient = robot_link.RosRobotClient

_INTERFACES: dict[str, Callable[[], tuple[Any, Any]]] = {
    "submit_move": lambda: _srv("SubmitMove"),
    "get_full_state": lambda: _srv("GetFullState"),
    "get_status": lambda: _srv("GetRobotStatus"),
    "set_motor_mode": lambda: _srv("SetMotorMode"),
    "stop": lambda: _srv("StopMove"),
    "play_sound": lambda: _srv("PlaySound"),
    "set_gaze": _set_bool,
    "set_head_reserved": _set_bool,
    "set_wobbling": _set_bool,
}


# --- fakes: the two ROS shapes the client touches -------------------------------------


class Future:
    """rclpy's Future, as far as the client uses it: a callback and a result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._result: Any = None
        self._error: BaseException | None = None
        self._callbacks: list[Callable[[Any], None]] = []

    def add_done_callback(self, callback: Callable[[Any], None]) -> None:
        with self._lock:
            if not self._done:
                self._callbacks.append(callback)
                return
        callback(self)

    def result(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result

    def finish(self, result: Any = None, error: BaseException | None = None) -> None:
        with self._lock:
            self._done, self._result, self._error = True, result, error
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)


class Service:
    """One service client with the server behind it.

    ``answer(request, response)`` fills the response, or raises to fail the call. ``hang``
    leaves the future unfinished, a node that took the request and went quiet.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.ready = True
        self.hang = False
        self.raises: BaseException | None = None
        self.requests: list[Any] = []
        self.forgotten: list[Any] = []
        self.answer: Callable[[Any, Any], Any] | None = None
        self.response_cls = _INTERFACES[name]()[1]
        self.defer: Callable[[Callable[[], None]], None] | None = None

    def service_is_ready(self) -> bool:
        return self.ready

    def call_async(self, request: Any) -> Future:
        if self.raises is not None:
            raise self.raises
        self.requests.append(request)
        future = Future()
        if self.hang:
            return future

        def run() -> None:
            response = self.response_cls()
            try:
                if self.answer is not None:
                    response = self.answer(request, response) or response
                future.finish(response)
            except Exception as error:  # noqa: BLE001 - the future carries it
                future.finish(error=error)

        if self.defer is not None:
            self.defer(run)
        else:
            run()
        return future

    def remove_pending_request(self, future: Any) -> None:
        self.forgotten.append(future)


def _accepting(_request: Any, response: Any) -> Any:
    response.accepted = True
    response.move_id = "move-1"
    return response


def _succeeding(_request: Any, response: Any) -> Any:
    response.success = True
    return response


def _ok(_request: Any, response: Any) -> Any:
    response.ok = True
    return response


def _running(_request: Any, response: Any) -> Any:
    response.state = "running"
    return response


def _stopped_one(_request: Any, response: Any) -> Any:
    response.stopped = 1
    return response


def _full_state(_request: Any, response: Any) -> Any:
    response.ok = True
    state = response.state
    state.stamp.sec = 1_800_000_000
    state.stamp.nanosec = 123_456_000
    state.control_mode = "enabled"
    state.head_pose.x, state.head_pose.z, state.head_pose.yaw = 0.001, 0.012, -0.25
    state.head_joints = [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26]
    state.body_yaw = 0.11
    state.antennas_position = [0.31, -0.32]
    state.has_motion = True
    block = state.motion
    block.head_moving = True
    block.client, block.lane = "hostapp", "interaction"
    block.has_face_seen, block.face_seen_s = True, 0.4
    block.has_command = True
    block.command.yaw = 0.1
    block.has_residual = True
    block.residual.pitch = 0.05
    return response


_DEFAULT_ANSWERS = {
    "submit_move": _accepting,
    "get_full_state": _full_state,
    "get_status": _running,
    "set_motor_mode": _ok,
    "set_gaze": _succeeding,
    "set_head_reserved": _succeeding,
    "stop": _stopped_one,
    "play_sound": _ok,
    "set_wobbling": _succeeding,
}


class Bench:
    """A link with every service matched and a heartbeat, unless a test changes that."""

    def __init__(self, **link_options: Any) -> None:
        self.services = {name: Service(name) for name in robot_link.SERVICE_NAMES}
        for name, service in self.services.items():
            service.answer = _DEFAULT_ANSWERS[name]
        requests = {name: _INTERFACES[name]()[0] for name in robot_link.SERVICE_NAMES}
        self.link = robot_link.RobotLink(self.services, requests, **link_options)
        self.link.note_heartbeat()
        self.clients: list[RosRobotClient] = []

    def __getitem__(self, name: str) -> Service:
        return self.services[name]

    def client(self, **options: Any) -> RosRobotClient:
        options.setdefault("timeout", 0.3)
        options.setdefault("flag_timeout_s", 0.1)
        options.setdefault("wait_slice_s", 0.02)
        options.setdefault("replay_gap_s", 0.01)
        built = RosRobotClient(self.link, **options)
        self.clients.append(built)
        return built

    def close(self) -> None:
        for client in self.clients:
            client.close()

    def go_down(self) -> None:
        """No server for anything: what a stopped robot node looks like."""

        for service in self.services.values():
            service.ready = False


@pytest.fixture
def bench():
    built = Bench()
    yield built
    built.close()


class _Trace:
    """The running motion trace, as far as ``emit`` and ``set_tag`` read it."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def emit(self, stream: str, fields: dict, *, at: float | None = None) -> None:
        self.records.append((stream, fields))


@pytest.fixture
def trace(monkeypatch):
    stub = _Trace()
    monkeypatch.setattr(motion_trace, "_trace", stub)
    yield stub
    motion_trace._current.tag = ""


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- the module stands alone ----------------------------------------------------------


def test_the_module_imports_no_ros_when_it_is_loaded():
    tree = ast.parse((_HUB / "emori_hub" / "robot_link.py").read_text(encoding="utf-8"))
    forbidden = {"rclpy", "std_msgs", "std_srvs", "reachy_mini_msgs"}
    top = {alias.name.split(".")[0] for node in tree.body if isinstance(node, ast.Import)
           for alias in node.names}
    top |= {node.module.split(".")[0] for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module and not node.level}
    assert top & forbidden == set()


def test_hub_code_never_asks_for_the_hardware_serial():
    """Inside a container there is no device tree, so the answer would quietly be another
    number. Its callers are host processes and stay there."""

    for path in (_HUB / "emori_hub").rglob("*.py"):
        assert "robot_identity" not in path.read_text(encoding="utf-8"), path


def test_every_service_the_client_calls_has_a_server_on_the_robot():
    package = _OPEN / "reachy_mini_ros2" / "reachy_mini_ros2"
    served = ((package / "ros_surface.py").read_text(encoding="utf-8")
              + (package / "robot_node.py").read_text(encoding="utf-8"))
    for name, wire in robot_link.SERVICE_NAMES.items():
        assert f'"{wire}"' in served, name
    assert f'"{robot_link.HEARTBEAT_TOPIC}"' in served


def test_the_required_list_is_the_seven_and_leaves_the_inert_two_out():
    assert set(robot_link.REQUIRED_SERVICES) == {
        "submit_move", "get_full_state", "get_status", "set_motor_mode", "set_gaze",
        "set_head_reserved", "stop"}
    assert set(robot_link.INERT_SERVICES) == {"play_sound", "set_wobbling"}


# --- link up: every required service matched, and a heartbeat ------------------------


@pytest.mark.parametrize("missing", robot_link.REQUIRED_SERVICES)
def test_health_is_false_until_each_required_service_is_matched(bench, missing):
    client = bench.client()
    bench[missing].ready = False
    assert client.health() is False
    assert bench.link.first_unmatched() == missing
    bench[missing].ready = True
    assert client.health() is True
    assert bench.link.first_unmatched() == ""


@pytest.mark.parametrize("inert", robot_link.INERT_SERVICES)
def test_an_inert_service_cannot_hold_health_false(bench, inert):
    bench[inert].ready = False
    assert bench.client().health() is True


def test_a_stale_heartbeat_is_a_dead_link_however_matched_the_services_look():
    now = [100.0]
    bench = Bench(clock=lambda: now[0])
    client = bench.client(clock=lambda: now[0])
    try:
        assert client.health() is True
        now[0] += robot_link.HEARTBEAT_TIMEOUT_S + 0.1
        assert bench.link.is_up() is False
        assert "heartbeat" in bench.link.down_reason()
        assert client.health() is False
        bench.link.note_heartbeat()
        assert client.health() is True
    finally:
        bench.close()


def test_no_heartbeat_yet_is_a_link_that_is_down():
    bench = Bench()
    bench.link._last_beat = None
    assert bench.link.is_up() is False
    assert bench.client().health() is False
    bench.close()


def test_a_client_whose_context_has_gone_reads_as_unmatched(bench):
    def gone() -> bool:
        raise RuntimeError("the context is shut down")

    bench["stop"].service_is_ready = gone
    assert bench.link.first_unmatched() == "stop"


def test_the_log_names_what_is_missing(bench, caplog):
    client = bench.client()
    bench["get_status"].ready = False
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        assert client.health() is False
    assert "/reachy_mini/get_status" in caplog.text


# --- health ---------------------------------------------------------------------------


def test_health_is_the_robots_state(bench):
    client = bench.client()
    assert client.health() is True

    def failing(_request, response):
        response.state = "error"
        return response

    bench["get_status"].answer = failing
    assert client.health() is False


# --- read_state -----------------------------------------------------------------------


def test_read_state_is_the_rest_body_key_for_key(bench):
    state = bench.client().read_state()
    assert state == {
        "control_mode": "enabled",
        "head_pose": {"x": 0.001, "y": 0.0, "z": 0.012, "roll": 0.0, "pitch": 0.0,
                      "yaw": -0.25},
        "head_joints": [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26],
        "body_yaw": 0.11,
        "antennas_position": [0.31, -0.32],
        "timestamp": "2027-01-15T08:00:00.123456Z",
        "passive_joints": None,
        "doa": None,
        "motion": {
            "active": False, "head_moving": True, "head_reserved": False,
            "client": "hostapp", "lane": "interaction", "gaze_active": False,
            "face_seen_s": 0.4,
            "command": {"roll": 0.0, "pitch": 0.0, "yaw": 0.1},
            "residual": {"roll": 0.0, "pitch": 0.05, "yaw": 0.0},
        },
    }


def test_read_state_leaves_out_what_the_node_did_not_report(bench):
    def plain(request, response):
        _full_state(request, response)
        response.state.has_motion = False
        response.state.control_mode = ""
        response.state.stamp.sec = 0
        response.state.stamp.nanosec = 0
        return response

    bench["get_full_state"].answer = plain
    state = bench.client().read_state()
    assert "motion" not in state
    assert state["control_mode"] is None
    assert state["timestamp"] is None


def test_read_state_gives_the_motion_block_only_what_it_has(bench):
    def partial(request, response):
        _full_state(request, response)
        block = response.state.motion
        block.has_command = block.has_residual = block.has_face_seen = False
        return response

    bench["get_full_state"].answer = partial
    motion = bench.client().read_state()["motion"]
    assert motion["face_seen_s"] is None
    assert "command" not in motion and "residual" not in motion


def test_read_state_is_none_with_the_link_down(bench):
    client = bench.client()
    bench.go_down()
    assert client.read_state() is None
    assert bench["get_full_state"].requests == []


def test_read_state_is_none_on_a_refused_read(bench):
    def refused(_request, response):
        response.ok = False
        response.error = "RuntimeError: backend"
        return response

    bench["get_full_state"].answer = refused
    assert bench.client().read_state() is None


@pytest.mark.parametrize("spoil", [
    lambda state: setattr(state.head_pose, "x", math.nan),
    lambda state: setattr(state.head_pose, "yaw", math.inf),
    lambda state: setattr(state.motion.residual, "pitch", math.nan),
    lambda state: setattr(state.motion.command, "yaw", -math.inf),
    lambda state: setattr(state, "body_yaw", math.nan),
    lambda state: setattr(state, "head_joints", [math.nan] * 7),
    lambda state: setattr(state, "antennas_position", [0.0, math.inf]),
    lambda state: setattr(state.motion, "face_seen_s", math.nan),
], ids=["pose-x", "pose-yaw", "residual", "command", "body-yaw", "joints", "antennas", "face"])
def test_read_state_is_none_on_any_non_finite_number(bench, spoil):
    """JSON cannot carry a NaN. The detectors have no guard of their own, and one NaN in
    their baseline would deafen the push-turn sensor until it is next reset."""

    def poisoned(request, response):
        _full_state(request, response)
        spoil(response.state)
        return response

    bench["get_full_state"].answer = poisoned
    assert bench.client().read_state() is None


def test_read_state_is_none_on_a_malformed_answer(bench):
    bench["get_full_state"].answer = lambda _r, _s: SimpleNamespace(ok=True, state=None)
    assert bench.client().read_state() is None


# --- apply ----------------------------------------------------------------------------


def test_a_goto_converts_units_and_antenna_order_at_the_boundary(bench):
    bench.client().apply(MotionIntent(
        kind="goto", head=HeadPose(x=10.0, y=-5.0, z=2.0, roll=90.0, pitch=-45.0, yaw=180.0),
        antennas=(0.2, -0.4), body_yaw=0.3, duration=1.25, interpolation="ease_in_out"))
    request = bench["submit_move"].requests[-1]
    assert (request.kind, request.client, request.lane) == ("goto", "hostapp", "interaction")
    assert request.preempt is False
    assert request.use_head is True
    assert request.head_pose.x == pytest.approx(0.010)
    assert request.head_pose.y == pytest.approx(-0.005)
    assert request.head_pose.z == pytest.approx(0.002)
    assert request.head_pose.roll == pytest.approx(math.pi / 2)
    assert request.head_pose.pitch == pytest.approx(-math.pi / 4)
    assert request.head_pose.yaw == pytest.approx(math.pi)
    assert request.use_antennas is True
    assert list(request.antennas) == [-0.4, 0.2]  # authored (left, right), wire [right, left]
    assert request.use_body_yaw is True
    assert request.body_yaw == 0.3
    assert (request.duration, request.interpolation) == (1.25, "ease_in_out")


@pytest.mark.parametrize("kind", ["goto", "look_at", "antennas"])
def test_the_three_goto_kinds_send_only_what_they_carry(bench, kind):
    bench.client().apply(MotionIntent(kind=kind, antennas=(0.1, 0.2)))
    request = bench["submit_move"].requests[-1]
    assert request.kind == "goto"
    assert request.use_head is False
    assert request.use_body_yaw is False
    assert request.use_antennas is True


def test_a_superseding_intent_and_a_trace_tag_reach_the_submit_request(bench, trace):
    """Drop either one and the queue the voice timing removed is back, with no error."""

    client = bench.client()
    motion_trace.set_tag(12, 3)
    client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0), supersede=True))
    request = bench["submit_move"].requests[-1]
    assert request.supersede is True
    assert request.reason == "ros:goto:12.3"
    # The robot's move trace is joined back to the plan on this ending.
    assert re.search(r":(\d+)\.(\d+)$", request.reason).groups() == ("12", "3")


def test_a_recorded_move_carries_supersede_and_the_tag_too(bench, trace):
    client = bench.client()
    motion_trace.set_tag(7, 0)
    assert client.play_recorded_move("fear1", supersede=True) == "move-1"
    request = bench["submit_move"].requests[-1]
    assert (request.kind, request.move) == ("play_recorded", "fear1")
    assert request.dataset == "pollen-robotics/reachy-mini-emotions-library"
    assert request.supersede is True
    assert request.reason == "ros:recorded:fear1:7.0"
    assert (request.client, request.lane, request.preempt) == ("hostapp", "interaction", False)


def test_a_recorded_intent_takes_its_supersede_from_the_intent(bench):
    bench.client().apply(MotionIntent(kind="play_recorded", move="joy1", supersede=True))
    request = bench["submit_move"].requests[-1]
    assert (request.move, request.supersede) == ("joy1", True)


def test_an_intent_that_does_not_supersede_says_so_and_no_tag_means_a_plain_reason(bench):
    bench.client().apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0)))
    request = bench["submit_move"].requests[-1]
    assert request.supersede is False
    assert request.reason == "ros:goto"


def test_motion_sync_off_stops_asking_to_supersede(bench, monkeypatch):
    monkeypatch.setenv("EMORI_MOTION_SYNC", "0")
    client = bench.client()
    client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0), supersede=True))
    client.play_recorded_move("fear1", supersede=True)
    assert [r.supersede for r in bench["submit_move"].requests] == [False, False]


def test_the_trace_tag_is_bounded_as_it_is_on_the_rest_path(bench, trace):
    motion_trace._current.tag = "x" * 100
    bench.client().apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0)))
    assert bench["submit_move"].requests[-1].reason == "ros:goto:" + "x" * 40


@pytest.mark.parametrize("failure", ["down", "refused", "raises", "hangs"])
def test_a_move_that_fails_warns_and_never_raises(bench, trace, caplog, failure):
    client = bench.client(timeout=0.05)
    if failure == "down":
        bench.go_down()
    elif failure == "refused":
        def refuse(_request, response):
            response.accepted = False
            response.error_code = "dropped"
            return response

        bench["submit_move"].answer = refuse
    elif failure == "raises":
        bench["submit_move"].raises = RuntimeError("publish failed")
    else:
        bench["submit_move"].hang = True
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0)))
        client.wake_up()
    assert "submit_move:goto" in caplog.text
    assert "submit_move:wake_up" in caplog.text
    posts = [fields for stream, fields in trace.records if stream == "post"]
    assert {fields["status"] for fields in posts} == ({409} if failure == "refused" else {0})


def test_wake_up_is_a_submit_on_the_hostapp_identity_and_carries_no_tag(bench, trace):
    motion_trace.set_tag(5, 1)
    bench.client().apply(MotionIntent(kind="wake_up", duration=9.0))
    request = bench["submit_move"].requests[-1]
    assert (request.kind, request.client, request.lane) == ("wake_up", "hostapp", "interaction")
    assert request.reason == "wake_up"
    # What a REST route leaves at the request's defaults: the wake has no duration argument.
    assert (request.duration, request.interpolation) == (0.5, "minjerk")


def test_a_set_target_goes_through_submit_on_the_hostapp_identity(bench):
    bench.client().apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0)))
    request = bench["submit_move"].requests[-1]
    assert (request.kind, request.client, request.lane) == ("set_target", "hostapp",
                                                            "interaction")
    assert request.supersede is False


def test_a_target_that_meets_a_running_move_is_not_a_failure(bench, trace, caplog):
    def busy(_request, response):
        response.accepted = False
        response.error_code = "busy"
        return response

    bench["submit_move"].answer = busy
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        bench.client().apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0)))
    assert caplog.text == ""
    assert trace.records == []


# --- motor mode -----------------------------------------------------------------------


def test_a_refused_mode_is_asked_once_and_other_modes_still_go(bench, caplog):
    def refuse_gravity(request, response):
        response.ok = request.mode != "gravity_compensation"
        response.message = "" if response.ok else "backend_error"
        return response

    bench["set_motor_mode"].answer = refuse_gravity
    client = bench.client()
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        client.set_motor_mode("gravity_compensation")
        client.set_motor_mode("gravity_compensation")
        client.set_motor_mode("enabled")
        client.set_motor_mode("enabled")
    assert [r.mode for r in bench["set_motor_mode"].requests] == [
        "gravity_compensation", "enabled", "enabled"]
    assert caplog.text.count("refused motor mode") == 1


@pytest.mark.parametrize("failure", ["down", "hangs", "raises"])
def test_a_mode_call_that_fails_latches_that_mode_and_never_raises(bench, caplog, failure):
    client = bench.client(timeout=0.05)
    service = bench["set_motor_mode"]
    if failure == "down":
        bench.go_down()
    elif failure == "hangs":
        service.hang = True
    else:
        service.raises = RuntimeError("transport")
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        client.set_motor_mode("enabled")
        client.set_motor_mode("enabled")
    assert caplog.text.count("not retrying") == 1
    assert "enabled" in client._modes_refused
    for each in bench.services.values():
        each.ready = True
    sent = len(service.requests)
    client.set_motor_mode("enabled")
    assert len(service.requests) == sent


# --- the two gates --------------------------------------------------------------------


GATES = [("set_gaze", "set_gaze"), ("set_head_reserved", "set_head_reserved")]


@pytest.mark.parametrize("method, service", GATES)
def test_an_acknowledged_gate_is_true_and_carries_its_value(bench, method, service):
    client = bench.client()
    assert getattr(client, method)(True) is True
    assert getattr(client, method)(False) is True
    assert [r.data for r in bench[service].requests] == [True, False]


@pytest.mark.parametrize("failure", ["refused", "hangs", "raises", "link"])
@pytest.mark.parametrize("method, service", GATES)
def test_a_failed_gate_is_false_and_backs_off_two_seconds(bench, method, service, failure):
    now = [50.0]
    client = bench.client(clock=lambda: now[0], flag_timeout_s=0.03)
    gate = bench[service]
    assert _wait_until(lambda: not client._replay.is_alive())
    assert getattr(client, method)(True) is True
    gate.requests.clear()
    answer = gate.answer
    if failure == "refused":
        def refuse(_request, response):
            response.success = False
            response.message = "gaze_disabled"
            return response

        gate.answer = refuse
    elif failure == "hangs":
        gate.hang = True
    elif failure == "raises":
        gate.raises = RuntimeError("transport")
    else:
        bench.go_down()

    assert getattr(client, method)(False) is False
    sent = len(gate.requests)
    assert getattr(client, method)(False) is False       # inside the backoff: nothing sent
    now[0] += 1.9
    assert getattr(client, method)(False) is False
    assert len(gate.requests) == sent

    for each in bench.services.values():
        each.ready = True
    gate.hang, gate.raises, gate.answer = False, None, answer
    now[0] += 0.2
    assert getattr(client, method)(False) is True         # not latched: a restart may fix it
    assert getattr(client, method)(True) is True          # and an answer clears the backoff


def test_each_gate_keeps_its_own_backoff(bench):
    client = bench.client()
    assert _wait_until(lambda: not client._replay.is_alive())
    bench["set_gaze"].raises = RuntimeError("transport")
    assert client.set_gaze(True) is False
    assert client.set_head_reserved(True) is True


def test_a_gate_gives_up_at_its_own_short_deadline(bench):
    client = bench.client(timeout=5.0, flag_timeout_s=0.05)
    assert _wait_until(lambda: not client._replay.is_alive())
    bench["set_gaze"].hang = True
    started = time.monotonic()
    assert client.set_gaze(True) is False
    assert time.monotonic() - started < 1.0


# --- recorded moves -------------------------------------------------------------------


def test_a_recorded_move_returns_the_id_the_stop_takes(bench):
    client = bench.client()
    assert client.play_recorded_move("fear1") == "move-1"
    client.stop_recorded_move("move-1")
    assert bench["stop"].requests[-1].move_id == "move-1"


def test_an_empty_name_sends_nothing(bench):
    assert bench.client().play_recorded_move("") is None
    assert bench["submit_move"].requests == []


def test_an_empty_stop_sends_nothing_because_it_would_stop_every_move(bench):
    bench.client().stop_recorded_move("")
    assert bench["stop"].requests == []


@pytest.mark.parametrize("answer", [
    lambda _r, s: (setattr(s, "accepted", False), setattr(s, "error_code", "dropped"), s)[-1],
    lambda _r, s: (setattr(s, "accepted", True), setattr(s, "move_id", ""), s)[-1],
], ids=["refused", "accepted-with-no-id"])
def test_no_id_means_none_so_the_player_skips_its_wait(bench, answer):
    bench["submit_move"].answer = answer
    assert bench.client().play_recorded_move("fear1") is None


def test_a_recorded_move_with_the_link_down_warns_and_is_none(bench, caplog):
    client = bench.client()
    bench.go_down()
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        assert client.play_recorded_move("fear1") is None
    assert "recorded move 'fear1' failed" in caplog.text


def test_a_stop_with_the_link_down_is_a_debug_line(bench, caplog):
    client = bench.client()
    bench.go_down()
    with caplog.at_level(logging.DEBUG, logger="emori.robot.link"):
        client.stop_recorded_move("move-1")
    records = [r for r in caplog.records if "stop recorded move" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.DEBUG]


def test_a_stop_of_a_finished_move_warns_as_the_rest_404_does(bench, caplog):
    bench["stop"].answer = None
    with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
        bench.client().stop_recorded_move("move-1")
    assert "stop recorded move move-1" in caplog.text


# --- media ----------------------------------------------------------------------------


def test_a_sound_reaches_the_inert_service(bench):
    bench.client().apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    assert bench["play_sound"].requests[-1].file == "mmhm.wav"


@pytest.mark.parametrize("failure", ["unmatched", "down", "refused"])
def test_a_media_failure_mutes_further_audio(bench, failure):
    client = bench.client()
    if failure == "unmatched":
        bench["play_sound"].ready = False
    elif failure == "down":
        bench.go_down()
    else:
        bench["play_sound"].answer = lambda _r, s: (setattr(s, "ok", False), s)[-1]
    client.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    assert client._media_ok is False
    sent = len(bench["play_sound"].requests)
    bench["play_sound"].ready = True
    client.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    assert len(bench["play_sound"].requests) == sent
    # Motion is unaffected by the media latch, unless the whole link is down.
    if failure != "down":
        client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))
        assert bench["submit_move"].requests


def test_wobble_toggles_and_a_failure_does_not_raise(bench):
    client = bench.client()
    client.apply(MotionIntent(kind="wobble", wobble=True))
    client.apply(MotionIntent(kind="wobble", wobble=False))
    assert [r.data for r in bench["set_wobbling"].requests] == [True, False]
    bench["set_wobbling"].ready = False
    client.apply(MotionIntent(kind="wobble", wobble=True))
    assert client._media_ok is True


# --- a call in flight -----------------------------------------------------------------


def test_a_wedged_node_with_a_live_heartbeat_costs_the_full_timeout(bench):
    client = bench.client(timeout=0.25, wait_slice_s=0.05)
    bench["get_status"].hang = True
    started = time.monotonic()
    assert client.health() is False
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 1.0
    assert len(bench["get_status"].forgotten) == 1       # a late answer is not held for


def test_a_dead_node_releases_the_caller_within_a_slice_and_later_calls_fail_at_once():
    bench = Bench(heartbeat_timeout_s=0.1)
    client = bench.client(timeout=5.0, wait_slice_s=0.05)
    bench["get_status"].hang = True
    started = time.monotonic()
    try:
        assert client.health() is False
        assert time.monotonic() - started < 2.0
        started = time.monotonic()
        assert client.read_state() is None
        assert time.monotonic() - started < 1.0
    finally:
        bench.close()


def test_a_service_lost_mid_call_releases_the_caller(bench):
    client = bench.client(timeout=5.0, wait_slice_s=0.02)
    bench["get_status"].hang = True
    threading.Timer(0.05, lambda: setattr(bench["stop"], "ready", False)).start()
    started = time.monotonic()
    assert client.health() is False
    assert time.monotonic() - started < 2.0


def test_an_answer_that_arrives_on_another_thread_is_taken(bench):
    client = bench.client(timeout=2.0, wait_slice_s=0.01)
    bench["get_status"].defer = lambda run: threading.Timer(0.05, run).start()
    assert client.health() is True


def test_a_cancelled_future_is_a_failed_call(bench):
    bench["get_status"].answer = lambda _r, _s: None
    bench["get_status"].response_cls = lambda: None
    assert bench.client().health() is False


def test_an_exception_on_the_future_is_a_failed_call(bench):
    def explode(_request, _response):
        raise RuntimeError("the response could not be taken")

    bench["get_status"].answer = explode
    assert bench.client().health() is False


# --- the first gate values ------------------------------------------------------------


def _never_up() -> Bench:
    bench = Bench()
    bench.link._last_beat = None
    return bench


def test_while_the_link_has_never_been_up_a_gate_remembers_and_says_false():
    bench = _never_up()
    client = bench.client(replay_window_s=0.0)
    try:
        assert client.set_gaze(True) is False
        assert client.set_head_reserved(False) is False
        assert client._gaze.wanted is True
        assert client._head_reserved.wanted is False
        assert bench["set_gaze"].requests == []
    finally:
        bench.close()


def test_the_link_coming_up_in_time_delivers_the_latest_value_of_each_gate():
    bench = _never_up()
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is False
        assert client.set_gaze(False) is False       # the newest one is the one owed
        assert client.set_head_reserved(True) is False
        bench.link.note_heartbeat()
        assert _wait_until(lambda: bench["set_gaze"].requests
                           and bench["set_head_reserved"].requests)
        assert _wait_until(lambda: not client._replay.is_alive())
        assert [r.data for r in bench["set_gaze"].requests] == [False]
        assert [r.data for r in bench["set_head_reserved"].requests] == [True]
    finally:
        bench.close()


def test_a_gate_nobody_pushed_is_not_invented():
    bench = _never_up()
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is False
        bench.link.note_heartbeat()
        assert _wait_until(lambda: not client._replay.is_alive())
        assert bench["set_head_reserved"].requests == []
    finally:
        bench.close()


def test_a_dropped_first_call_is_retried():
    """A matched client does not prove the server has matched its response writer."""

    bench = _never_up()
    gate = bench["set_gaze"]

    def second_time_lucky(request, response):
        response.success = len(gate.requests) >= 2
        return response

    gate.answer = second_time_lucky
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is False
        bench.link.note_heartbeat()
        assert _wait_until(lambda: not client._replay.is_alive())
        assert len(gate.requests) == 2
        assert client._gaze.acknowledged is True
    finally:
        bench.close()


def test_three_failed_attempts_warn_and_stop(caplog):
    bench = _never_up()
    gate = bench["set_gaze"]
    gate.answer = lambda _r, response: (setattr(response, "success", False), response)[-1]
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is False
        with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
            bench.link.note_heartbeat()
            assert _wait_until(lambda: not client._replay.is_alive())
        assert len(gate.requests) == 3
        assert "set_gaze" in caplog.text
    finally:
        bench.close()


def test_the_replay_runs_outside_the_backoff():
    bench = _never_up()
    gate = bench["set_gaze"]
    gate.answer = lambda _r, response: (setattr(response, "success",
                                                len(gate.requests) > 1), response)[-1]
    now = [10.0]
    client = bench.client(replay_window_s=5.0, clock=lambda: now[0])
    try:
        assert client.set_gaze(True) is False
        client._gaze.backoff_until = 1e9
        bench.link.note_heartbeat()
        assert _wait_until(lambda: not client._replay.is_alive())
        assert len(gate.requests) == 2
        assert client._gaze.backoff_until == 0.0
    finally:
        bench.close()


def test_a_failed_replay_does_not_hold_back_a_live_push():
    bench = _never_up()
    gate = bench["set_gaze"]
    gate.answer = lambda _r, response: (setattr(response, "success", False), response)[-1]
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is False
        bench.link.note_heartbeat()
        assert _wait_until(lambda: not client._replay.is_alive())
        gate.answer = _succeeding
        assert client.set_gaze(True) is True
    finally:
        bench.close()


def test_no_replay_when_the_link_comes_up_later_than_the_window():
    """A node that appears later is a fresh node, which REST would not have reached either."""

    bench = _never_up()
    client = bench.client(replay_window_s=0.05)
    try:
        assert client.set_gaze(True) is False
        assert _wait_until(lambda: not client._replay.is_alive())
        bench.link.note_heartbeat()
        time.sleep(0.05)
        assert bench["set_gaze"].requests == []
        assert client.set_gaze(False) is True        # a live push works as it always did
    finally:
        bench.close()


def test_no_replay_when_a_newer_push_already_got_through():
    bench = _never_up()
    gate = bench["set_gaze"]
    client = bench.client(replay_window_s=5.0)
    held = threading.Event()
    release = threading.Event()
    original = client._replay_one

    def after_the_push(which):
        held.set()
        release.wait(2.0)
        return original(which)

    client._replay_one = after_the_push
    try:
        assert client.set_gaze(True) is False
        bench.link.note_heartbeat()
        assert held.wait(2.0)
        assert client.set_gaze(False) is True
        release.set()
        assert _wait_until(lambda: not client._replay.is_alive())
        assert [r.data for r in gate.requests] == [False]
    finally:
        release.set()
        bench.close()


def test_no_replay_after_the_first_link_up_when_the_robot_restarts():
    """The node's flag goes off with it and no edge sends it again. Unchanged from REST."""

    bench = Bench()
    client = bench.client(replay_window_s=5.0)
    try:
        assert client.set_gaze(True) is True
        assert _wait_until(lambda: not client._replay.is_alive())
        bench.go_down()
        assert client.set_gaze(True) is False
        for each in bench.services.values():
            each.ready = True
        time.sleep(0.05)
        assert [r.data for r in bench["set_gaze"].requests] == [True]
    finally:
        bench.close()


def test_a_newer_push_racing_the_replay_lands_last():
    """Sending holds the gate's lock, so an older value cannot land after a newer one."""

    bench = _never_up()
    gate = bench["set_gaze"]
    entered = threading.Event()
    leave = threading.Event()
    landed: list[bool] = []

    def slow(request, response):
        entered.set()
        leave.wait(2.0)
        landed.append(request.data)
        response.success = True
        return response

    gate.answer = slow
    gate.defer = lambda run: threading.Thread(target=run, daemon=True).start()
    client = bench.client(replay_window_s=5.0, flag_timeout_s=2.0)
    try:
        assert client.set_gaze(True) is False
        bench.link.note_heartbeat()
        assert entered.wait(2.0)                     # the replay is mid-send with True

        pushed: list[bool] = []
        pusher = threading.Thread(target=lambda: pushed.append(client.set_gaze(False)))
        pusher.start()
        time.sleep(0.05)
        assert pushed == []                          # it waits for the send in flight
        leave.set()
        pusher.join(3.0)
        assert pushed == [True]
        assert landed == [True, False]
    finally:
        leave.set()
        bench.close()


# --- the status the host tools read ---------------------------------------------------


def test_robot_status_carries_the_robots_answer(bench):
    def answer(_request, response):
        response.state = "running"
        response.motor_control_mode = "enabled"
        response.ready = True
        response.no_media = True
        response.version = "1.2.3"
        response.robot_name = "reachy_mini"
        response.backend = "reachy_mini_ros2"
        response.gaze_run = True
        return response

    bench["get_status"].answer = answer
    body = bench.client().robot_status()
    assert body["state"] == "running"
    assert body["motor_control_mode"] == "enabled"
    assert body["ready"] is True and body["gaze_run"] is True
    assert body["backend"] == "reachy_mini_ros2"
    assert body["first_unmatched"] == ""


def test_robot_status_with_the_link_down_names_the_first_unmatched_service(bench):
    client = bench.client()
    bench["set_motor_mode"].ready = False
    body = client.robot_status()
    assert body["state"] == "error"
    assert body["first_unmatched"] == "set_motor_mode"
    assert "/reachy_mini/set_motor_mode" in body["error"]


# --- close ----------------------------------------------------------------------------


def test_after_close_calls_fail_at_once_and_close_is_repeatable(bench):
    client = bench.client()
    client.close()
    client.close()
    assert client.health() is False
    assert client.read_state() is None
    assert client.set_gaze(True) is False
    client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))
    assert bench["submit_move"].requests == []


def test_close_ends_the_replay_thread_while_it_is_waiting():
    bench = _never_up()
    client = bench.client(replay_window_s=30.0)
    assert client._replay.is_alive()
    client.close()
    assert not client._replay.is_alive()


def test_the_thread_is_named_for_the_journal(bench):
    assert bench.client()._replay.name == "robot-link-replay"


# --- the node -------------------------------------------------------------------------


class _FakeNode:
    def __init__(self, name: str, *, namespace: str = "") -> None:
        self.name, self.namespace = name, namespace
        self.clients: dict[str, tuple[Any, Any]] = {}
        self.subscriptions: list[tuple[Any, ...]] = []
        self.destroyed = False

    def create_client(self, srv_type: Any, name: str, *, callback_group: Any = None) -> Any:
        self.clients[name] = (srv_type, callback_group)
        return SimpleNamespace(service_is_ready=lambda: True)

    def create_subscription(self, msg_type: Any, topic: str, callback: Any, qos: Any,
                            *, callback_group: Any = None) -> Any:
        self.subscriptions.append((msg_type, topic, callback, qos, callback_group))
        return object()

    def destroy_node(self) -> None:
        self.destroyed = True


class _FakeExecutor:
    """Like Jazzy's, a callback's exception comes back out of the call that spins it."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.spins = 0
        self.fail_next: BaseException | None = None
        self.shut_down = False

    def add_node(self, node: Any) -> None:
        self.nodes.append(node)

    def spin_once(self, timeout_sec: float = 0.0) -> None:
        self.spins += 1
        fail, self.fail_next = self.fail_next, None
        if fail is not None:
            raise fail
        time.sleep(0.001)

    def shutdown(self) -> None:
        self.shut_down = True


class _Fatal(Exception):
    pass


def _bindings(executor: _FakeExecutor) -> robot_link.LinkBindings:
    services = {name: SimpleNamespace(Request=_INTERFACES[name]()[0])
                for name in robot_link.SERVICE_NAMES}
    return robot_link.LinkBindings(
        create_node=_FakeNode, executor_cls=lambda: executor, group_cls=object,
        state_qos="state-qos", ok=lambda: True, fatal=(_Fatal,), Header=object,
        services=services)


def test_the_node_is_wired_to_the_absolute_names_with_one_group():
    executor = _FakeExecutor()
    running = robot_link.start_robot_link(_bindings(executor))
    try:
        node = running.node
        assert (node.name, node.namespace) == ("robot_link", "/emori")
        assert set(node.clients) == set(robot_link.SERVICE_NAMES.values())
        groups = {group for _srv_type, group in node.clients.values()}
        assert len(groups) == 1
        (_msg_type, topic, callback, qos, group), = node.subscriptions
        assert (topic, qos) == ("/reachy_mini/link_heartbeat", "state-qos")
        assert group in groups
        assert executor.nodes == [node]
        assert running.link.down_reason() == "no link heartbeat yet"
        callback(object())
        assert running.link.is_up()
    finally:
        running.stop()
    assert executor.shut_down and node.destroyed


def test_a_callback_that_raises_costs_that_callback_and_the_spin_goes_on(caplog):
    executor = _FakeExecutor()
    running = robot_link.start_robot_link(_bindings(executor))
    try:
        assert _wait_until(lambda: executor.spins > 0)
        with caplog.at_level(logging.WARNING, logger="emori.robot.link"):
            executor.fail_next = RuntimeError("a callback raised")
            before = executor.spins
            assert _wait_until(lambda: executor.spins > before + 2)
        assert "carrying on" in caplog.text
        assert running.spin.thread.name == "ros-link"
    finally:
        running.stop()


def test_the_context_going_away_ends_the_spin_quietly():
    executor = _FakeExecutor()
    running = robot_link.start_robot_link(_bindings(executor))
    thread = running.spin.thread
    executor.fail_next = _Fatal()
    assert _wait_until(lambda: not thread.is_alive())
    running.stop()


def test_stop_never_raises_when_a_release_fails():
    executor = _FakeExecutor()
    running = robot_link.start_robot_link(_bindings(executor))

    def broken() -> None:
        raise RuntimeError("already gone")

    executor.shutdown = broken
    running.stop()


def test_the_bindings_refuse_to_load_without_ros():
    try:
        import rclpy  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            robot_link.load_link_bindings()
    else:  # pragma: no cover - only in the hub's image
        assert robot_link.load_link_bindings().services


def test_the_interface_files_still_have_the_fields_this_file_writes():
    # The fakes are built from the files; this reads one back to prove that is what runs.
    assert _msg("PoseRpy")().x == 0.0
    request = _srv("SubmitMove")[0]()
    with pytest.raises(AttributeError):
        request.not_a_field = 1
