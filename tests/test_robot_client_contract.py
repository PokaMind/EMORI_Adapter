"""One contract, every robot client: what the code above the client sees must not differ.

The matrix runs four clients: ``HttpRobotClient`` against the real REST shim over a socket,
``RosRobotClient`` against a fake link, and ``TracingRobotClient`` around each. All four end
at the same arbiter and backend fakes, and the fake link is not a hand-written answer: its
services are the robot's real ``ros_surface`` handlers over the real ``state_snapshot``, with
the three services the main node serves copied from ``robot_node``. So every assertion reads
"what the caller did" and then "what the arbiter saw", on both transports.

A new robot call goes in the Protocol, in both clients and in this file.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import numpy as np
import pytest

from emori_adapter import motion_trace
from emori_adapter.hostapp.driver import RobotPlayer
from emori_adapter.models import HeadPose, MotionIntent
from emori_adapter.robot.client import HttpRobotClient
from emori_adapter.robot.tracing import TracingRobotClient

from tests.test_reachy_ros_surface import arbiter_mod, snap, surface
from tests.test_reachy_rest_shim import FakeArbiter, FakeBackendHost, shim_mod
from tests.test_ros_robot_client import Bench, _Trace, _wait_until


@dataclass
class Stand:
    """A client, the arbiter and backend at the far end of it, and what a test can do to it."""

    client: Any
    arbiter: FakeArbiter
    backend: FakeBackendHost
    gaze: list
    disable_gaze: Callable[[], None]
    take_down: Callable[[], None]
    stop: Callable[[], None]

    @property
    def inner(self) -> Any:
        return getattr(self.client, "inner", self.client)


class _Gaze:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.available = True

    def set(self, enabled: bool) -> None:
        self.calls.append(bool(enabled))


def _rest_stand() -> Stand:
    backend = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend)
    gaze = _Gaze()
    holder: dict[str, Any] = {}

    def start(with_gaze: bool = True) -> Any:
        shim = shim_mod.start_rest_shim(
            arbiter, backend, host="127.0.0.1", port=0, state_cache_s=0.0,
            gaze=gaze.set if with_gaze else None,
            gaze_active=(lambda: False) if with_gaze else None,
            face_seen=(lambda: 0.4) if with_gaze else None)
        assert shim.wait_until_ready(10.0)
        holder["shim"] = shim
        return shim

    client = HttpRobotClient(start().url, timeout=2.0)

    def stop() -> None:
        client.close()
        holder["shim"].stop()

    def take_down() -> None:
        holder["shim"].stop()
        # A port nothing listens on, with a short deadline so the row stays quick.
        client._http = type(client._http)(base_url=client.base_url, timeout=0.3)
        client._mode_http = type(client._mode_http)(base_url=client.base_url, timeout=0.3)

    def disable_gaze() -> None:
        holder["shim"].stop()
        shim = start(with_gaze=False)
        client._http = type(client._http)(base_url=shim.url, timeout=2.0)

    return Stand(client, arbiter, backend, gaze.calls, disable_gaze, take_down, stop)


class _Server:
    """Answers on one thread, as an executor does, so a caller really waits for a result."""

    def __init__(self) -> None:
        self._work: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._work.get()
            if job is None:
                return
            job()

    def defer(self, job: Callable[[], None]) -> None:
        self._work.put(job)

    def stop(self) -> None:
        self._work.put(None)
        self._thread.join(2.0)


def _ros_stand() -> Stand:
    backend = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend)
    gaze = _Gaze()
    state = snap.make_state_snapshot(arbiter, backend, state_cache_s=0.0,
                                     gaze_active=lambda: False, face_seen=lambda: 0.4)
    handlers = surface.LinkHandlers(arbiter, state, set_gaze=gaze.set, gaze_run=lambda: False,
                                    request_cls=arbiter_mod.MotionRequest)
    bench = Bench()
    server = _Server()

    # What robot_node's three services do.
    def set_motor_mode(request: Any, response: Any) -> Any:
        outcome = arbiter.set_motor_mode(request.mode)
        response.ok = bool(outcome.accepted)
        response.message = "" if outcome.accepted else outcome.error_code
        response.mode = arbiter.status()["motor_mode"]
        return response

    def stop_move(request: Any, response: Any) -> Any:
        response.stopped = int(arbiter.stop(request.move_id))
        return response

    def set_head_reserved(request: Any, response: Any) -> Any:
        arbiter.set_head_reserved(bool(request.data))
        response.success = True
        return response

    answers = {
        "submit_move": handlers.submit_move,
        "get_full_state": handlers.get_full_state,
        "get_status": handlers.get_status,
        "set_gaze": handlers.set_gaze,
        "play_sound": handlers.play_sound,
        "set_wobbling": handlers.set_wobbling,
        "set_motor_mode": set_motor_mode,
        "stop": stop_move,
        "set_head_reserved": set_head_reserved,
    }
    for name, answer in answers.items():
        bench[name].answer = answer
        bench[name].defer = server.defer
    client = bench.client(timeout=2.0, flag_timeout_s=0.5)

    def stop() -> None:
        client.close()
        server.stop()

    def disable_gaze() -> None:
        bench["set_gaze"].answer = surface.LinkHandlers(
            arbiter, state, request_cls=arbiter_mod.MotionRequest).set_gaze

    return Stand(client, arbiter, backend, gaze.calls, disable_gaze, bench.go_down, stop)


_BUILDERS = {"http": _rest_stand, "ros": _ros_stand}
CLIENTS = ["http", "ros", "http+tracing", "ros+tracing"]


@pytest.fixture(params=CLIENTS)
def stand(request) -> Iterator[Stand]:
    transport, _, wrapped = request.param.partition("+")
    built = _BUILDERS[transport]()
    if wrapped:
        built.client = TracingRobotClient(built.client)
    try:
        yield built
    finally:
        built.stop()


@pytest.fixture
def trace(monkeypatch):
    stub = _Trace()
    monkeypatch.setattr(motion_trace, "_trace", stub)
    yield stub
    motion_trace._current.tag = ""


def _settled(stand: Stand) -> None:
    """The first-push replay has nothing to say here; wait so a count is exact."""

    replay = getattr(stand.inner, "_replay", None)
    if replay is not None:
        assert _wait_until(lambda: not replay.is_alive())


def _pose(request: Any) -> tuple[float, ...]:
    return shim_mod._local_matrix_to_xyzrpy(request.head_pose)


# --- what every client must have ------------------------------------------------------


@pytest.mark.parametrize("method", [
    "health", "wake_up", "set_motor_mode", "apply", "read_state", "set_gaze",
    "set_head_reserved", "play_recorded_move", "stop_recorded_move", "close",
])
def test_the_whole_protocol_is_there(stand, method):
    assert callable(getattr(stand.client, method))
    assert callable(getattr(stand.inner, method))


# --- connectivity ---------------------------------------------------------------------


def test_health_follows_the_control_loop(stand):
    assert stand.client.health() is True
    stand.backend.ready = False
    stand.backend.fault = "the control loop died"
    assert stand.client.health() is False


def test_a_robot_that_is_gone_fails_every_call_the_same_quiet_way(stand):
    stand.take_down()
    assert stand.client.health() is False
    assert stand.client.read_state() is None
    stand.client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=2.0)))
    stand.client.wake_up()
    stand.client.set_motor_mode("enabled")
    stand.client.set_motor_mode("enabled")
    assert "enabled" in stand.inner._modes_refused
    assert stand.client.set_gaze(True) is False
    assert stand.client.set_head_reserved(True) is False
    assert stand.client.play_recorded_move("fear1") is None
    stand.client.stop_recorded_move("move-1")
    stand.client.apply(MotionIntent(kind="wobble", wobble=True))
    stand.client.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    assert stand.inner._media_ok is False
    assert stand.arbiter.submitted == []


# --- the state read -------------------------------------------------------------------


def test_read_state_has_the_keys_and_values_of_the_robots_reading(stand):
    stand.backend.measured = shim_mod._local_xyzrpy_to_matrix(0.001, 0.002, 0.012, 0.05, 0.1, -0.25)
    state = stand.client.read_state()
    assert set(state) >= {"control_mode", "head_pose", "head_joints", "body_yaw",
                          "antennas_position", "timestamp", "motion"}
    assert state["head_pose"]["x"] == pytest.approx(0.001)
    assert state["head_pose"]["yaw"] == pytest.approx(-0.25)
    assert state["head_joints"] == [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26]
    assert state["body_yaw"] == 0.11
    assert state["antennas_position"] == [0.31, -0.32]
    assert state["control_mode"] == "disabled"
    motion = state["motion"]
    assert motion["face_seen_s"] == 0.4
    assert "residual" not in motion and "command" not in motion


def test_read_state_carries_the_residual_when_the_robot_reports_a_command(stand):
    stand.backend.measured = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.0, 0.1)
    stand.backend.commanded = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    motion = stand.client.read_state()["motion"]
    assert motion["residual"]["yaw"] == pytest.approx(0.1)
    assert motion["command"]["yaw"] == pytest.approx(0.0)


def test_a_non_finite_pose_is_no_sample(stand):
    measured = np.eye(4)
    measured[0, 0] = math.nan
    stand.backend.measured = measured
    assert stand.client.read_state() is None
    stand.backend.measured = np.eye(4)
    # Over HTTP the refused sample closes the connection, and the next read may be the one
    # that finds out. That read is one more skipped sample; the one after it is good.
    assert _wait_until(lambda: stand.client.read_state() is not None)


def test_a_backend_that_raises_is_no_sample(stand):
    def broken() -> Any:
        raise RuntimeError("the backend fell over")

    stand.backend.present_head_pose = broken
    assert stand.client.read_state() is None


def test_both_transports_give_one_answer_for_one_frozen_reading():
    """Key for key and value for value, the timestamp's spelling included."""

    answers = {}
    for name, build in _BUILDERS.items():
        built = build()
        try:
            built.backend.measured = shim_mod._local_xyzrpy_to_matrix(
                0.001, 0.002, 0.012, 0.05, 0.1, -0.25)
            built.backend.commanded = shim_mod._local_xyzrpy_to_matrix(
                0.0, 0.0, 0.01, 0.0, 0.05, 0.0)
            built.arbiter.motion = {"active": True, "head_moving": True, "head_reserved": True,
                                    "client": "hostapp", "lane": "interaction"}
            answers[name] = built.client.read_state()
        finally:
            built.stop()
    for answer in answers.values():
        stamp = answer.pop("timestamp")
        assert stamp.endswith("Z") and "T" in stamp
    assert answers["http"] == answers["ros"]


# --- moves: what the arbiter saw ------------------------------------------------------


def test_a_goto_reaches_the_arbiter_the_same_way(stand):
    stand.client.apply(MotionIntent(
        kind="goto", head=HeadPose(x=10.0, z=2.0, roll=30.0, pitch=-8.0, yaw=20.0),
        antennas=(0.2, -0.4), body_yaw=0.3, duration=1.25, interpolation="ease_in_out"))
    request = stand.arbiter.submitted[-1]
    assert (request.kind, request.client, request.lane) == ("goto", "hostapp", "interaction")
    assert request.preempt is False and request.supersede is False
    x, y, z, roll, pitch, yaw = _pose(request)
    assert (x, y, z) == pytest.approx((0.010, 0.0, 0.002))
    assert (roll, pitch, yaw) == pytest.approx(
        (math.radians(30.0), math.radians(-8.0), math.radians(20.0)))
    assert request.antennas == (-0.4, 0.2)
    assert request.body_yaw == 0.3
    assert (request.duration, request.interpolation) == (1.25, "ease_in_out")


def test_an_antenna_only_move_leaves_the_head_out(stand):
    stand.client.apply(MotionIntent(kind="antennas", antennas=(0.1, 0.2)))
    request = stand.arbiter.submitted[-1]
    assert request.kind == "goto"
    assert request.head_pose is None and request.body_yaw is None
    assert request.antennas == (0.2, 0.1)


def test_a_wake_is_the_same_request_with_the_same_defaults(stand):
    stand.client.wake_up()
    request = stand.arbiter.submitted[-1]
    assert (request.kind, request.client, request.lane) == ("wake_up", "hostapp", "interaction")
    assert request.reason == "wake_up"
    assert (request.duration, request.interpolation) == (0.5, "minjerk")
    assert request.preempt is False and request.supersede is False


def test_a_superseding_intent_and_a_trace_tag_reach_the_arbiter(stand, trace):
    motion_trace.set_tag(12, 3)
    stand.client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0), supersede=True))
    request = stand.arbiter.submitted[-1]
    assert request.supersede is True
    assert request.reason.endswith(":12.3")


def test_motion_sync_off_stops_asking_to_supersede(monkeypatch):
    monkeypatch.setenv("EMORI_MOTION_SYNC", "0")
    for build in _BUILDERS.values():
        built = build()
        try:
            built.client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0), supersede=True))
            assert built.arbiter.submitted[-1].supersede is False
        finally:
            built.stop()


def test_a_refused_move_never_raises(stand):
    stand.arbiter.refuse_submit = "dropped"
    stand.client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=4.0)))
    stand.client.wake_up()
    assert len(stand.arbiter.submitted) == 2


# --- recorded moves -------------------------------------------------------------------


def test_a_recorded_move_returns_the_id_that_stops_it(stand):
    move_id = stand.client.play_recorded_move("fear1")
    assert move_id == "move-1"
    request = stand.arbiter.submitted[-1]
    assert (request.kind, request.move) == ("play_recorded", "fear1")
    assert request.dataset == "pollen-robotics/reachy-mini-emotions-library"
    assert (request.client, request.lane) == ("hostapp", "interaction")
    assert request.supersede is False
    stand.client.stop_recorded_move(move_id)
    assert stand.arbiter.stop_calls == ["move-1"]


def test_a_recorded_move_can_supersede_and_carries_the_tag(stand, trace):
    motion_trace.set_tag(7, 0)
    stand.client.play_recorded_move("fear1", supersede=True)
    request = stand.arbiter.submitted[-1]
    assert request.supersede is True
    assert request.reason.endswith(":7.0")


def test_a_refused_recording_is_none_so_the_player_skips_its_wait(stand):
    stand.arbiter.refuse_submit = "dropped"
    assert stand.client.play_recorded_move("fear1") is None


def test_an_empty_name_and_an_empty_id_ask_nothing(stand):
    assert stand.client.play_recorded_move("") is None
    stand.client.stop_recorded_move("")
    assert stand.arbiter.submitted == [] and stand.arbiter.stop_calls == []


def test_stopping_a_move_that_has_finished_is_quiet_for_the_caller(stand):
    stand.client.stop_recorded_move("long-gone")
    assert stand.arbiter.stop_calls == ["long-gone"]


# --- motor mode -----------------------------------------------------------------------


def test_a_refused_mode_is_attempted_once(stand):
    stand.arbiter.refuse_modes = ("gravity_compensation",)
    for _ in range(3):
        stand.client.set_motor_mode("gravity_compensation")
    stand.client.set_motor_mode("enabled")
    stand.client.set_motor_mode("enabled")
    assert stand.arbiter.mode_calls == ["gravity_compensation", "enabled", "enabled"]
    assert stand.backend.mode == "enabled"


# --- the gates ------------------------------------------------------------------------


def test_the_gates_reach_the_robot_and_answer_true(stand):
    _settled(stand)
    assert stand.client.set_gaze(True) is True
    assert stand.client.set_gaze(False) is True
    assert stand.gaze == [True, False]
    assert stand.client.set_head_reserved(True) is True
    assert stand.arbiter.reserved_calls == [True]


def test_a_node_without_gaze_backs_off_and_does_not_latch(stand):
    _settled(stand)
    stand.disable_gaze()
    assert stand.client.set_gaze(True) is False
    inner = stand.inner
    if isinstance(inner, HttpRobotClient):
        assert "/api/emori/gaze" not in inner._routes_absent
        assert inner._routes_backoff["/api/emori/gaze"] > time.monotonic()
    else:
        assert inner._gaze.backoff_until > time.monotonic()
    assert stand.client.set_head_reserved(True) is True    # the other gate is unaffected


# --- media ----------------------------------------------------------------------------


def test_the_inert_media_calls_change_nothing(stand):
    stand.client.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    stand.client.apply(MotionIntent(kind="wobble", wobble=True))
    stand.client.apply(MotionIntent(kind="wobble", wobble=False))
    assert stand.inner._media_ok is True
    assert stand.arbiter.submitted == []


# --- the real player ------------------------------------------------------------------


def test_the_real_player_boots(stand):
    """``health`` -> motors ``enabled`` -> ``wake_up`` -> settle -> back to compliant."""

    player = RobotPlayer(stand.client, gravity_comp=True)
    try:
        assert _wait_until(lambda: "gravity_compensation" in stand.arbiter.mode_calls, 15.0)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
    assert stand.arbiter.mode_calls[0] == "enabled", stand.arbiter.mode_calls
    assert [request.kind for request in stand.arbiter.submitted] == ["wake_up"]
    assert stand.arbiter.submitted[0].client == "hostapp"
    assert player.busy is False


def test_the_real_player_plays_a_batch_one_request_per_intent_in_order(stand):
    player = RobotPlayer(stand.client, gravity_comp=False)
    try:
        assert _wait_until(lambda: bool(stand.arbiter.submitted), 15.0)
        player.submit([
            MotionIntent(kind="goto", head=HeadPose(pitch=-8.0), duration=0.05),
            MotionIntent(kind="goto", head=HeadPose(pitch=0.0), duration=0.05),
        ])
        assert _wait_until(lambda: len(stand.arbiter.submitted) >= 3, 15.0)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
    assert [request.kind for request in stand.arbiter.submitted] == ["wake_up", "goto", "goto"]
    pitches = [_pose(request)[4] for request in stand.arbiter.submitted[1:]]
    assert pitches == pytest.approx([math.radians(-8.0), 0.0])


def test_the_real_player_sends_a_sentence_superseding_and_tagged(stand, trace):
    """The first and last move of a sentence supersede, and each names its plan."""

    player = RobotPlayer(stand.client, gravity_comp=False)
    try:
        assert _wait_until(lambda: bool(stand.arbiter.submitted), 15.0)
        plan = motion_trace.begin_plan()
        player.submit([
            MotionIntent(kind="goto", head=HeadPose(pitch=-4.0), duration=0.05, speech=True),
            MotionIntent(kind="goto", head=HeadPose(pitch=4.0), duration=0.05, speech=True),
            MotionIntent(kind="goto", head=HeadPose(pitch=0.0), duration=0.05, speech=True),
        ])
        motion_trace.end_plan()
        assert _wait_until(lambda: len(stand.arbiter.submitted) >= 4, 15.0)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
    moves = stand.arbiter.submitted[1:]
    assert [request.supersede for request in moves] == [True, False, True]
    assert [request.reason.rsplit(":", 1)[-1] for request in moves] == [
        f"{plan}.0", f"{plan}.1", f"{plan}.2"]


def test_the_real_player_plays_a_recording_and_cuts_it_when_revoked(stand):
    current = [True]
    player = RobotPlayer(stand.client, gravity_comp=False)
    try:
        assert _wait_until(lambda: bool(stand.arbiter.submitted), 15.0)
        player.submit([MotionIntent(kind="play_recorded", move="fear1", duration=5.0)],
                      guard=lambda: current[0])
        assert _wait_until(lambda: len(stand.arbiter.submitted) >= 2, 15.0)
        current[0] = False
        assert _wait_until(lambda: bool(stand.arbiter.stop_calls), 15.0)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
    assert stand.arbiter.submitted[-1].move == "fear1"
    assert stand.arbiter.stop_calls == ["move-2"]


# --- the tracing wrapper --------------------------------------------------------------


@pytest.mark.parametrize("transport", ["http", "ros"])
def test_the_tracing_wrapper_records_every_call_and_changes_nothing(transport):
    built = _BUILDERS[transport]()
    inner = built.client
    client = TracingRobotClient(inner)
    try:
        assert client.health() is True
        assert client.read_state() is not None
        client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=1.0), reason="why"))
        assert client.play_recorded_move("fear1", supersede=True) == "move-2"
        client.stop_recorded_move("move-2")
        client.set_motor_mode("enabled")
        client.wake_up()
        _wait_until(lambda: not getattr(inner, "_replay", threading.Thread()).is_alive())
        assert client.set_gaze(True) is True
        assert client.set_head_reserved(False) is True
    finally:
        built.stop()
    records = client.sink.records
    assert [record["method"] for record in records] == [
        "health", "read_state", "apply", "play_recorded_move", "stop_recorded_move",
        "set_motor_mode", "wake_up", "set_gaze", "set_head_reserved"]
    assert records[2]["args"]["intent"]["reason"] == "why"
    assert records[3]["args"] == {"name": "fear1", "supersede": True}
    assert built.arbiter.submitted[1].supersede is True


# --- where the two differ today, stated -----------------------------------------------


def test_a_non_finite_translation_is_no_sample_on_the_link_and_no_angle_on_either():
    """REST puts what JSON cannot carry through the framework: a 500 from an older FastAPI, a
    null from this one, and the angles the detectors read are untouched. The link gives no
    sample. The gesture sensor reads roll, pitch and yaw, so neither can hand it a NaN."""

    measured = np.eye(4)
    measured[0, 3] = math.nan
    answers = {}
    for name, build in _BUILDERS.items():
        built = build()
        try:
            built.backend.measured = measured
            answers[name] = built.client.read_state()
        finally:
            built.stop()
    assert answers["ros"] is None
    assert answers["http"] is None or answers["http"]["head_pose"]["x"] is None


def test_a_target_over_rest_reaches_the_arbiter():
    built = _rest_stand()
    try:
        built.client.apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0)))
    finally:
        built.stop()
    assert len(built.arbiter.targets) == 1


def test_a_target_over_the_link_reaches_the_arbiter():
    built = _ros_stand()
    try:
        built.client.apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0)))
    finally:
        built.stop()
    assert [request.kind for request in built.arbiter.submitted] == ["set_target"]

