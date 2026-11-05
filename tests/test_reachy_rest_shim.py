"""The REST shim, proven by the hostapp's own robot client — not by a hand-written one.

``rest_shim.py`` exists for exactly one reason: when v2 deletes the Pollen daemon, the
hostapp must not notice. The only honest way to test that claim is to run the REAL
``emori_adapter.robot.client.HttpRobotClient`` (and the REAL ``RobotPlayer`` that drives
it) against the shim over a real socket. A test that posted hand-built JSON would pass
happily while the client sent something else — which is precisely the class of bug that
cost this project a 404-ing ``/api/move/stop/{uuid}`` and 2.5 s of Pollen emotion playing
under the next sentence.

So: one free local port, a ``FakeArbiter``/``FakeBackendHost`` that record what the shim
asked for, and every assertion phrased as "what the hostapp did" → "what the arbiter saw".
The fakes implement the arbiter interface verbatim, so the
real arbiter drops in behind them.

The module is loaded BY PATH: ``ros2_ws`` is not on ``sys.path`` and installing the ROS
package would need ROS, which exists nowhere but the container. The shim imports only
fastapi/uvicorn/pydantic/numpy at module scope precisely so this is possible.
"""

from __future__ import annotations

import ast
import importlib.util
import math
from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Any, Optional

import socket

import httpx
import numpy as np
import pytest

from emori_adapter.hostapp.driver import RobotPlayer
from emori_adapter.models import HeadPose, MotionIntent
from emori_adapter.robot.client import HttpRobotClient

_SHIM = (
    Path(__file__).resolve().parents[1]
    / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2" / "rest_shim.py"
)

_MODULE_NAME = "reachy_mini_rest_shim_under_test"

# `backend_host.py` is the shim's other half: it is what answers "is the control loop
# turning", and the shim's `state` field is nothing but that answer in the daemon's
# dialect. Loadable by path for the same reason the shim is - every `reachy_mini` import
# inside it is lazy, so on this box it needs only numpy.
_BACKEND_HOST = _SHIM.with_name("backend_host.py")


def _load_by_path(name: str, path: Path):
    if not path.is_file():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"{path} does not exist yet")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` resolves annotations through
    # `sys.modules[cls.__module__]`, and an unregistered module makes it raise.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load():
    return _load_by_path(_MODULE_NAME, _SHIM)


shim_mod = _load()
backend_host_mod = _load_by_path("reachy_mini_backend_host_under_test", _BACKEND_HOST)


# --- the arbiter interface, faked -----------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """`arbiter.Outcome`, field for field."""

    accepted: bool
    error_code: str = ""
    move_id: str = ""


@dataclass
class FakeBackendHost:
    """`backend_host`: three cheap, thread-safe present-state reads,
    plus the two the shim needs to answer honestly about a dead control loop."""

    kind: str = "mujoco"
    mode: str = "disabled"
    reads: int = 0
    ready: bool = True
    fault: str = ""
    # `None` = a backend that cannot say what it is commanding (a fake, a backend before
    # its first tick), which is the v1-equivalent case the shim must degrade to.
    commanded: Optional[np.ndarray] = None
    # Overrides the pitch ramp when a test needs a pose it can do arithmetic on.
    measured: Optional[np.ndarray] = None

    def is_ready(self) -> bool:
        return self.ready

    def error(self) -> str:
        return self.fault

    def present_head_pose(self) -> np.ndarray:
        # A pose that MOVES: pitch ramps one degree per read, so a test can tell a fresh
        # read from a cached one and the gesture sensor has something to sense.
        self.reads += 1
        if self.measured is not None:
            return self.measured
        return shim_mod._local_xyzrpy_to_matrix(
            0.0, 0.0, 0.012, 0.0, math.radians(self.reads), -0.25
        )

    def commanded_head_pose(self) -> Optional[np.ndarray]:
        return self.commanded

    def present_joints(self) -> list[float]:
        # body_yaw, stewart_1..6, right_antenna, left_antenna — the node's publish order.
        return [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26, 0.31, -0.32]

    def motor_mode(self) -> str:
        return self.mode


@dataclass
class FakeArbiter:
    """`MotionArbiter`: submit / wait / stop / running / status /
    set_target / set_motor_mode, recording everything the shim hands it."""

    backend_host: FakeBackendHost
    submitted: list[Any] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)
    targets: list[tuple[Any, Any, Any]] = field(default_factory=list)
    mode_calls: list[str] = field(default_factory=list)
    refuse_submit: str = ""
    refuse_set_target: str = ""
    refuse_modes: tuple[str, ...] = ()
    head_reserved: bool = False
    reserved_calls: list[bool] = field(default_factory=list)
    # What `motion_snapshot()` answers. The real arbiter derives these from the running
    # request; here they are set directly so a test can stage "an antenna-only cue is
    # running" without also staging a move.
    motion: dict = field(default_factory=lambda: {
        "active": False, "head_moving": False, "client": "", "lane": ""})
    _running: list[str] = field(default_factory=list)
    _minted: int = 0

    def submit(self, req: Any) -> Outcome:
        self.submitted.append(req)
        if self.refuse_submit:
            return Outcome(False, self.refuse_submit, "")
        self._minted += 1
        move_id = f"move-{self._minted}"
        self._running.append(move_id)
        return Outcome(True, "", move_id)

    def wait(self, move_id: str, timeout: Optional[float] = None) -> Outcome:
        return Outcome(True, "", move_id)

    def trace_since(self, after: int = 0) -> list[dict]:
        return [{"seq": after + 1, "event": "accepted"}]

    def stop(self, move_id: str = "") -> int:
        self.stop_calls.append(move_id)
        if not move_id:
            count = len(self._running)
            self._running.clear()
            return count
        if move_id in self._running:
            self._running.remove(move_id)
            return 1
        return 0

    def running(self) -> list[str]:
        return list(self._running)

    def status(self) -> dict:
        return {
            "busy": bool(self._running),
            "running_move_id": self._running[0] if self._running else "",
            "running_client": "",
            "running_lane": "",
            "queue_depth": 0,
            "owner": "",
            "head_reserved": self.head_reserved,
            "expression_owner": "hostapp",
            "motor_mode": self.backend_host.mode,
            "backend": "fake",
        }

    def motion_snapshot(self) -> dict:
        return dict(self.motion)

    def set_head_reserved(self, reserved: bool) -> None:
        self.reserved_calls.append(bool(reserved))
        self.head_reserved = bool(reserved)

    def set_target(self, head_pose=None, antennas=None, body_yaw=None) -> Outcome:
        self.targets.append((head_pose, antennas, body_yaw))
        if self.refuse_set_target:
            return Outcome(False, self.refuse_set_target, "")
        return Outcome(True, "", "")

    def set_motor_mode(self, mode: str) -> Outcome:
        self.mode_calls.append(mode)
        if mode in self.refuse_modes:
            return Outcome(False, "backend_error", "")
        self.backend_host.mode = mode
        return Outcome(True, "", mode)


@dataclass
class Rig:
    shim: Any
    arbiter: FakeArbiter
    backend_host: FakeBackendHost

    @property
    def url(self) -> str:
        return self.shim.url


def _rig(**kwargs) -> Rig:
    backend_host = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend_host, **kwargs)
    # Port 0: the kernel hands us a free one, so the suite never fights a real daemon (or
    # a second test) for :8000. `state_cache_s=0` so every GET is a fresh backend read.
    shim = shim_mod.start_rest_shim(
        arbiter,
        backend_host,
        host="127.0.0.1",
        port=0,
        wireless_version=True,
        state_cache_s=0.0,
    )
    assert shim.wait_until_ready(10.0), "shim never came up"
    assert shim.port > 0
    return Rig(shim=shim, arbiter=arbiter, backend_host=backend_host)


@pytest.fixture
def rig():
    built = _rig()
    try:
        yield built
    finally:
        built.shim.stop()


@pytest.fixture
def client(rig):
    c = HttpRobotClient(rig.url)
    try:
        yield c
    finally:
        c.close()


# --- the hostapp's boot sequence ------------------------------------------------------


def test_health_reads_the_shims_daemon_status(client):
    assert client.health() is True


def test_daemon_status_carries_the_backend_marker(rig):
    """The one field the daemon never had: how tooling refuses to rehearse against
    a stray Pollen daemon squatting on the same port."""

    body = httpx.get(f"{rig.url}/api/daemon/status", timeout=5.0).json()
    assert body["backend"] == "reachy_mini_ros2"
    assert body["state"] == "running"
    assert body["backend_status"] == {
        "motor_control_mode": "disabled", "error": None, "ready": True}
    assert body["no_media"] is True
    assert body["media_released"] is False
    assert body["simulation_enabled"] is True      # FakeBackendHost.kind == "mujoco"
    assert body["mockup_sim_enabled"] is False
    assert body["wireless_version"] is True
    assert body["hardware_id"] is None
    assert body["robot_name"] == "reachy_mini"
    assert body["error"] is None
    assert set(body) == {
        "state", "backend_status", "simulation_enabled", "mockup_sim_enabled", "no_media",
        "media_released", "wireless_version", "version", "hardware_id", "robot_name",
        "backend", "error",
    }


def test_a_dead_control_loop_is_reported_as_an_error_not_as_running(rig):
    """`state: running` is a claim about the 50 Hz control loop, not about this HTTP
    server - and the shim is served from the node's own process, so it answers
    perfectly well with a dead backend behind it.

    Before this, a control loop that faulted left every reader believing in a robot
    that had not moved since: the hostapp's health check passed, the installer's wait
    loop succeeded, `ros2_v2_smoke.py` passed, and the only symptom was stillness.
    """

    rig.backend_host.ready = False
    rig.backend_host.fault = "control loop: serial port disappeared"

    body = httpx.get(f"{rig.url}/api/daemon/status", timeout=5.0).json()
    assert body["state"] == "error"
    assert body["backend_status"]["ready"] is False
    assert body["backend_status"]["error"] == "control loop: serial port disappeared"
    assert body["error"] == "control loop: serial port disappeared"
    # The marker still identifies US: a reader has to be able to tell "our shim, broken"
    # from "Pollen's daemon squatting on the port", which are opposite next steps.
    assert body["backend"] == "reachy_mini_ros2"
    # ...and the field set is unchanged, so nothing downstream has to learn a new shape.
    assert set(body) == {
        "state", "backend_status", "simulation_enabled", "mockup_sim_enabled", "no_media",
        "media_released", "wireless_version", "version", "hardware_id", "robot_name",
        "backend", "error",
    }

    # It recovers by itself: this is a live read, not a latch.
    rig.backend_host.ready, rig.backend_host.fault = True, ""
    assert httpx.get(f"{rig.url}/api/daemon/status", timeout=5.0).json()["state"] == "running"


def test_a_dead_control_loop_does_not_change_the_motor_status_route(rig):
    """`/api/motors/status` answers what the motors are in, which is a different
    question and one the hostapp branches on separately. Making it fail too would turn
    one fault into two unrelated-looking ones."""

    rig.backend_host.ready = False
    rig.backend_host.fault = "control loop: gone"
    response = httpx.get(f"{rig.url}/api/motors/status", timeout=5.0)
    assert response.status_code == 200
    assert response.json() == {"mode": "disabled"}


def test_a_backend_host_without_the_readiness_probe_still_reads_as_running():
    """The shim is a compatibility layer. An older node - or any host that does not
    implement `is_ready` - must not be reported as a broken robot."""

    class Older(FakeBackendHost):
        is_ready = None       # type: ignore[assignment]

    host = Older()
    arbiter = FakeArbiter(backend_host=host)
    shim = shim_mod.start_rest_shim(arbiter, host, host="127.0.0.1", port=0,
                                    state_cache_s=0.0)
    try:
        assert shim.wait_until_ready(10.0)
        body = httpx.get(f"{shim.url}/api/daemon/status", timeout=5.0).json()
        assert body["state"] == "running"
        assert body["backend_status"]["ready"] is True
    finally:
        shim.stop()


def test_the_shim_can_take_the_port_the_daemon_just_left():
    """The daemon-to-node handover, which is the only way this shim is ever started on a
    robot.

    The hostapp polls `:8000` at 10 Hz, so when Pollen's daemon stops there are dozens of
    its connections sitting in TIME_WAIT on local port 8000. Measured on a robot: a bare
    `bind()` fails there with EADDRINUSE for about a minute, and the node comes up owning
    the serial bus with `shim=disabled` -- motors with an owner, and a host application
    with nothing to talk to. SO_REUSEADDR is what makes the handover
    survive the sockets of the process being replaced.
    """

    host = FakeBackendHost()
    shim = shim_mod.start_rest_shim(FakeArbiter(backend_host=host), host,
                                    host="127.0.0.1", port=0, state_cache_s=0.0)
    try:
        assert shim.sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    finally:
        shim.stop()



# --- the predicate underneath it: BackendHost.is_ready --------------------------------


class _FakeEvent:
    def __init__(self, value: bool = False) -> None:
        self._value = value

    def is_set(self) -> bool:
        return self._value

    def set(self) -> None:
        self._value = True


class _FakeThread:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class _FakePollenBackend:
    """Pollen's `Backend`, reduced to the three members readiness is decided by.

    `ready` and `should_stop` really are `threading.Event`s, and `error` really is a
    plain attribute that `wrapped_run` sets to `str(e)` before re-raising
    (`daemon/backend/abstract.py`:435-442).
    """

    def __init__(self) -> None:
        self.ready = _FakeEvent(True)
        self.should_stop = _FakeEvent(False)
        self.error = None


def _host_with(backend, *, alive: bool = True):
    host = backend_host_mod.BackendHost(backend_host_mod.BackendConfig(backend="mockup"))
    host.backend = backend
    host._thread = _FakeThread(alive)
    return host


def test_a_control_loop_that_never_started_is_not_ready():
    backend = _FakePollenBackend()
    backend.ready = _FakeEvent(False)
    host = _host_with(backend)
    assert host.is_ready() is False
    assert "ready" in host.error()


def test_a_control_loop_that_was_asked_to_stop_is_not_ready():
    """`should_stop` is how `close()` ends the loop, and how Pollen ends it too. A
    backend whose loop is winding down is not one that can move a robot."""

    backend = _FakePollenBackend()
    backend.should_stop.set()
    assert _host_with(backend).is_ready() is False


def test_a_control_loop_that_recorded_an_error_is_not_ready():
    """`wrapped_run` catches, sets `error`, calls `close()` and re-raises - so `error`
    is the ONLY trace a crashed control loop leaves inside the process. `ready` is a
    `threading.Event` nothing ever clears, so it still reads set."""

    backend = _FakePollenBackend()
    backend.error = "serial port disappeared"
    host = _host_with(backend)
    assert backend.ready.is_set() is True, "the trap: ready is still set"
    assert host.is_ready() is False
    assert host.error() == "serial port disappeared"


def test_a_dead_thread_is_not_ready_even_when_pollen_booked_nothing():
    """The belt to the braces. A thread can end without an exception and without
    `should_stop` - an `os._exit` inside a C extension, a killed greenlet, a `run()`
    that simply returned - and nothing in Pollen records that anywhere."""

    host = _host_with(_FakePollenBackend(), alive=False)
    assert host.is_ready() is False
    assert "thread" in host.error()


def test_a_backend_that_is_running_reads_ready_with_no_error():
    host = _host_with(_FakePollenBackend())
    assert host.is_ready() is True
    assert host.error() == ""


def test_a_host_with_no_backend_at_all_is_not_ready():
    host = backend_host_mod.BackendHost(backend_host_mod.BackendConfig(backend="mockup"))
    assert host.is_ready() is False
    assert host.error()


def test_set_motor_mode_and_status_round_trip(client, rig):
    client.set_motor_mode("enabled")
    assert rig.arbiter.mode_calls == ["enabled"]
    assert httpx.get(f"{rig.url}/api/motors/status", timeout=5.0).json() == {"mode": "enabled"}


def test_wake_up_is_an_interaction_lane_submit_from_the_hostapp(client, rig):
    client.wake_up()
    (req,) = rig.arbiter.submitted
    assert req.kind == "wake_up"
    assert req.lane == "interaction"
    assert req.client == "hostapp"


def test_goto_arrives_in_metres_radians_with_antennas_in_wire_order(client, rig):
    """The hostapp authors mm + degrees + ``(left, right)``; the arbiter must receive
    metres + radians + ``[right, left]`` — the daemon's units, unchanged by v2."""

    client.apply(MotionIntent(kind="goto", head=HeadPose(z=10.0, yaw=15.0),
                              antennas=(0.2, -0.2), duration=0.3))
    (req,) = rig.arbiter.submitted
    assert req.kind == "goto"
    assert req.duration == pytest.approx(0.3)
    assert req.interpolation == "minjerk"
    # (left=0.2, right=-0.2) authored -> [right, left] on the wire -> straight through.
    assert req.antennas == pytest.approx((-0.2, 0.2))
    assert req.body_yaw is None

    pose = np.asarray(req.head_pose, dtype=float)
    assert pose.shape == (4, 4)
    x, y, z, roll, pitch, yaw = shim_mod._local_matrix_to_xyzrpy(pose)
    assert z == pytest.approx(0.010)            # 10 mm
    assert yaw == pytest.approx(math.radians(15.0))
    assert (x, y, roll, pitch) == pytest.approx((0.0, 0.0, 0.0, 0.0))


def test_look_at_and_antenna_only_gotos_take_the_same_route(client, rig):
    client.apply(MotionIntent(kind="antennas", antennas=(0.4, -0.1), duration=0.2))
    (req,) = rig.arbiter.submitted
    assert req.kind == "goto"
    assert req.head_pose is None
    assert req.antennas == pytest.approx((-0.1, 0.4))


def test_set_target_reaches_the_arbiters_streaming_path(client, rig):
    client.apply(MotionIntent(kind="set_target", head=HeadPose(pitch=4.0), antennas=(0.8, 0.2)))
    (head, antennas, body_yaw) = rig.arbiter.targets[-1]
    assert antennas == pytest.approx((0.2, 0.8))     # [right, left]
    assert body_yaw is None
    _, _, _, _, pitch, _ = shim_mod._local_matrix_to_xyzrpy(head)
    assert pitch == pytest.approx(math.radians(4.0))


def test_set_target_during_a_move_answers_the_daemons_exact_ignored_body(rig):
    rig.arbiter.refuse_set_target = "busy"
    r = httpx.post(f"{rig.url}/api/move/set_target",
                   json={"target_body_yaw": 0.1}, timeout=5.0)
    assert r.status_code == 200
    assert r.json() == {"status": "ignored", "reason": "move_running"}


def test_recorded_move_play_returns_a_uuid_and_stop_reaches_the_arbiter(client, rig):
    uuid = client.play_recorded_move("fear1")
    assert uuid
    (req,) = rig.arbiter.submitted
    assert req.kind == "play_recorded"
    assert req.dataset == "pollen-robotics/reachy-mini-emotions-library"
    assert req.move == "fear1"
    assert rig.arbiter.running() == [uuid]

    client.stop_recorded_move(uuid)
    assert rig.arbiter.stop_calls == [uuid]
    assert rig.arbiter.running() == []


@pytest.mark.parametrize("header, wanted", [("1", True), ("0", False), ("", False)])
def test_the_supersede_header_reaches_the_arbiter(rig, header, wanted):
    r = httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.3, "antennas": [0.1, 0.1]},
                   headers={"X-Reachy-Supersede": header}, timeout=5.0)
    assert r.status_code == 200
    assert rig.arbiter.submitted[-1].supersede is wanted


def test_a_recorded_move_can_supersede_too(rig):
    r = httpx.post(f"{rig.url}/api/move/play/recorded-move-dataset/some/dataset/fear1", json={},
                   headers={"X-Reachy-Supersede": "1"}, timeout=5.0)
    assert r.status_code == 200
    assert rig.arbiter.submitted[-1].supersede is True


def test_a_move_keeps_its_callers_name_for_the_trace(rig):
    httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.3, "antennas": [0.1, 0.1]},
               headers={"X-Reachy-Trace": "12.3"}, timeout=5.0)
    assert rig.arbiter.submitted[-1].reason == "rest:goto:12.3"
    httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.3, "antennas": [0.1, 0.1]},
               timeout=5.0)
    assert rig.arbiter.submitted[-1].reason == "rest:goto"


def test_the_trace_route_hands_over_the_arbiters_records(rig):
    assert httpx.get(f"{rig.url}/api/move/trace", params={"after": 4}, timeout=5.0).json() \
        == [{"seq": 5, "event": "accepted"}]


def test_running_moves_are_listed_in_the_daemons_shape(client, rig):
    uuid = client.play_recorded_move("happy1")
    body = httpx.get(f"{rig.url}/api/move/running", timeout=5.0).json()
    assert body == [{"uuid": uuid}]


def test_stopping_an_unknown_move_is_a_404_and_never_raises(client, rig):
    r = httpx.post(f"{rig.url}/api/move/stop", json={"uuid": "no-such-move"}, timeout=5.0)
    assert r.status_code == 404
    client.stop_recorded_move("no-such-move")   # the client logs it; it must not raise


def test_read_state_gives_the_gesture_sensor_roll_pitch_yaw(client, rig):
    state = client.read_state()
    assert state is not None
    # `motion` is EMORI's extra key (v2 only) and is excluded here on purpose: this
    # assertion is the golden one for the DAEMON dialect, and it must keep reading the
    # same set of field names it read before gaze existed.
    assert set(state) - {"motion"} == {
        "control_mode", "head_pose", "head_joints", "body_yaw", "antennas_position",
        "timestamp", "passive_joints", "doa",
    }
    assert set(state["head_pose"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert state["head_pose"]["z"] == pytest.approx(0.012)
    assert state["head_pose"]["yaw"] == pytest.approx(-0.25)
    assert state["control_mode"] == "disabled"
    assert state["body_yaw"] == pytest.approx(0.11)
    assert state["antennas_position"] == pytest.approx([0.31, -0.32])   # [right, left]
    assert state["head_joints"] == pytest.approx([0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26])
    assert state["passive_joints"] is None and state["doa"] is None


def test_read_state_tracks_a_moving_head(client, rig):
    """The 10 Hz poll must see the head move, or the head-tilt gesture can never fire."""

    first = client.read_state()["head_pose"]["pitch"]
    second = client.read_state()["head_pose"]["pitch"]
    assert second > first


def test_state_is_cached_so_a_10hz_poll_costs_one_backend_read():
    """The CM4 runs the 50 Hz control loop in this same process now; a poll that reads
    the backend three times a tick is a cost v1 did not have."""

    backend_host = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend_host)
    shim = shim_mod.start_rest_shim(arbiter, backend_host, host="127.0.0.1", port=0,
                                    state_cache_s=5.0)
    try:
        assert shim.wait_until_ready(10.0)
        for _ in range(4):
            httpx.get(f"{shim.url}/api/state/full", timeout=5.0)
        assert backend_host.reads == 1
    finally:
        shim.stop()


# --- EMORI's two flags, and the motion block -------------------------------------------
# The node cannot compute either flag for itself: only the hostapp knows whether a
# session is running, and only the hostapp's gesture sensor knows whether a hand is on
# the head. These two routes and the `motion` key are the whole bridge.


def _state(rig) -> dict:
    return httpx.get(f"{rig.url}/api/state/full", timeout=5.0).json()


def test_head_reserved_arms_and_clears_the_arbiters_flag(rig):
    """`arbiter.set_head_reserved` has existed since the arbiter was written and has
    been dead code ever since, because nothing could reach it: the ROS graph's
    expression lane was never actually protected while a human had hold of the head."""

    for value in (True, False, True):
        body = httpx.post(f"{rig.url}/api/emori/head_reserved", json={"reserved": value},
                          timeout=5.0)
        assert body.status_code == 200
        assert body.json() == {"status": "ok", "reserved": value}
    assert rig.arbiter.reserved_calls == [True, False, True]
    assert rig.arbiter.head_reserved is True


def test_a_head_reserved_body_without_the_field_is_a_422(rig):
    """The flag decides whether the robot may move its own head. A missing field must
    not read as False."""

    assert httpx.post(f"{rig.url}/api/emori/head_reserved", json={}, timeout=5.0)\
        .status_code == 422


def test_the_gaze_route_reaches_the_injected_setter():
    backend_host = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend_host)
    seen: list[bool] = []
    shim = shim_mod.start_rest_shim(arbiter, backend_host, host="127.0.0.1", port=0,
                                    state_cache_s=0.0, gaze=seen.append,
                                    gaze_active=lambda: bool(seen and seen[-1]))
    try:
        assert shim.wait_until_ready(10.0)
        answer = httpx.post(f"{shim.url}/api/emori/gaze", json={"enabled": True},
                            timeout=5.0)
        assert answer.status_code == 200
        assert answer.json() == {"status": "ok", "enabled": True}
        assert seen == [True]

        state = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()
        assert state["motion"]["gaze_active"] is True

        httpx.post(f"{shim.url}/api/emori/gaze", json={"enabled": False}, timeout=5.0)
        assert seen == [True, False]
    finally:
        shim.stop()


def test_a_node_without_gaze_answers_503_and_not_404(rig):
    """404 is reserved to mean "this robot is v1 or Pollen's daemon", and the hostapp
    client LATCHES on 404 and never asks again. A v2 node that merely has
    `gaze.enabled:=false` today may have it true after the next restart, so it must not
    be latched off."""

    answer = httpx.post(f"{rig.url}/api/emori/gaze", json={"enabled": True}, timeout=5.0)
    assert answer.status_code == 503
    assert answer.json() == {"status": "unavailable", "reason": "gaze_disabled"}


def test_an_unknown_emori_route_is_still_a_404(rig):
    """The latch has to keep working for the case it was written for."""

    assert httpx.post(f"{rig.url}/api/emori/nonsense", json={}, timeout=5.0)\
        .status_code == 404


def test_the_motion_block_names_who_is_moving_the_robot(rig):
    rig.arbiter.motion = {"active": True, "head_moving": True,
                          "client": "ros:gaze", "lane": "expression"}
    motion = _state(rig)["motion"]
    assert motion["active"] is True
    assert motion["head_moving"] is True
    assert motion["client"] == "ros:gaze"
    assert motion["lane"] == "expression"
    assert motion["gaze_active"] is False           # no gaze injected in this robot


def test_a_backend_that_cannot_say_what_it_commands_reports_no_residual(rig):
    """The v1-equivalent case, and the one every consumer falls back from. A `motion`
    with no `residual` means "use the absolute pose", byte for byte today's path."""

    assert rig.backend_host.commanded is None
    motion = _state(rig)["motion"]
    assert "command" not in motion
    assert "residual" not in motion


def test_a_node_whose_arbiter_cannot_answer_omits_motion_entirely():
    """An older node, or a fake. A missing `motion` is how the hostapp recognises v1."""

    class OlderArbiter(FakeArbiter):
        motion_snapshot = None      # type: ignore[assignment]

    backend_host = FakeBackendHost()
    arbiter = OlderArbiter(backend_host=backend_host)
    shim = shim_mod.start_rest_shim(arbiter, backend_host, host="127.0.0.1", port=0,
                                    state_cache_s=0.0)
    try:
        assert shim.wait_until_ready(10.0)
        state = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()
        assert "motion" not in state
        # ...and every daemon field is exactly where it was.
        assert set(state) == {
            "control_mode", "head_pose", "head_joints", "body_yaw",
            "antennas_position", "timestamp", "passive_joints", "doa"}
    finally:
        shim.stop()


@pytest.mark.parametrize("deflection", [
    (0.03, 0.0, 0.0),          # `settle_epsilon_rad`
    (0.0, 0.0, 0.05),          # `quiet_epsilon_rad`
    (0.05, 0.02, 0.01),
])
@pytest.mark.parametrize("command_rpy", [
    (0.0, 0.0, 0.0), (0.05, -0.08, 0.1), (-0.1, 0.1, -0.05),
])
def test_at_the_detectors_own_epsilons_the_residual_is_a_plain_difference(
        rig, command_rpy, deflection):
    """The "no-op at rest" property, at the scale the thresholds were tuned on.

    Every number in the push-turn detectors - `settle_epsilon_rad` 0.03,
    `quiet_epsilon_rad` 0.05 - was measured against an absolute pose with the command
    sitting still. At those amplitudes and with the command within +-0.1 rad of
    neutral, the proper rotation difference and a component-wise Euler subtraction agree
    to 0.0052 rad `[measured]`, so nothing the detectors do changes.
    """

    roll, pitch, yaw = command_rpy
    command = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, roll, pitch, yaw)
    measured = shim_mod._local_xyzrpy_to_matrix(
        0.0, 0.0, 0.0, roll + deflection[0], pitch + deflection[1], yaw + deflection[2])
    rig.backend_host.commanded = command
    rig.backend_host.measured = measured

    residual = _state(rig)["motion"]["residual"]
    for axis, expected in zip(("roll", "pitch", "yaw"), deflection):
        assert residual[axis] == pytest.approx(expected, abs=0.006)


@pytest.mark.parametrize("command_rpy", [
    (0.0, 0.0, 0.0), (0.1, 0.1, 0.1), (-0.1, 0.1, -0.05),
])
def test_a_real_push_turn_still_crosses_the_threshold_under_any_command(rig, command_rpy):
    """The property that actually has to hold, rather than an exact equality.

    At a full 0.236 rad deflection (the measured stiffness of this robot's head) the two
    forms diverge by up to 0.026 rad `[measured]` - Euler angles do not subtract - so
    the honest assertion is not "the residual equals 0.236" but "it is still a push".
    `roll_threshold_rad` is 0.20, and the residual reads >= 0.233 for every command
    within +-0.1 rad of neutral.
    """

    roll, pitch, yaw = command_rpy
    command = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, roll, pitch, yaw)
    measured = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, roll + 0.236, pitch, yaw)
    rig.backend_host.commanded = command
    rig.backend_host.measured = measured

    residual = _state(rig)["motion"]["residual"]
    assert residual["roll"] >= 0.20
    assert abs(residual["pitch"]) < 0.05 and abs(residual["yaw"]) < 0.05


def test_with_no_command_at_all_the_residual_is_the_absolute_pose(rig):
    """The v1-identical case, spelled out: an unmoving robot commands a constant, and
    the residual is the absolute pose minus that constant. With the constant at the
    identity the two are the same numbers the detectors have always read."""

    measured = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.012, 0.21, -0.03, 0.02)
    rig.backend_host.commanded = np.eye(4)
    rig.backend_host.measured = measured

    residual = _state(rig)["motion"]["residual"]
    assert residual == pytest.approx({"roll": 0.21, "pitch": -0.03, "yaw": 0.02},
                                     abs=1e-9)


def test_the_residual_is_a_rotation_difference_and_not_a_subtraction(rig):
    """Away from neutral the two forms diverge, and the proper one is the one that
    stays right. Pinned exactly, so nobody "simplifies" it into a subtraction."""

    command = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.4, -0.4)
    measured = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.236, 0.4, -0.4)
    rig.backend_host.commanded = command
    rig.backend_host.measured = measured

    motion = _state(rig)["motion"]
    expected = shim_mod._rpy_of(measured[:3, :3] @ command[:3, :3].T)
    for axis in ("roll", "pitch", "yaw"):
        assert motion["residual"][axis] == pytest.approx(expected[axis], abs=1e-9)
    assert motion["command"] == pytest.approx(
        {"roll": 0.0, "pitch": 0.4, "yaw": -0.4}, abs=1e-9)


def test_a_robot_moving_its_own_head_produces_no_residual(rig):
    """The thesis of the whole design, in one assertion: when the robot commands a pose
    and the servos reach it, `measured - commanded` is zero however far the head turned.
    That is what lets the gesture sensor stay LIVE while the robot moves."""

    for yaw in (0.0, 0.2, 0.45, -0.45):
        pose = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.0, yaw)
        rig.backend_host.commanded = pose
        rig.backend_host.measured = pose
        residual = _state(rig)["motion"]["residual"]
        assert max(abs(residual[axis]) for axis in residual) < 1e-9


def test_the_motion_block_is_not_served_from_the_pose_cache():
    """A `head_moving` flag 50 ms stale is a gesture interlock that fires 50 ms late.
    The backend READING stays cached; the motion block does not."""

    backend_host = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend_host)
    shim = shim_mod.start_rest_shim(arbiter, backend_host, host="127.0.0.1", port=0,
                                    state_cache_s=5.0)
    try:
        assert shim.wait_until_ready(10.0)
        first = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()
        assert first["motion"]["head_moving"] is False

        arbiter.motion = {"active": True, "head_moving": True,
                          "client": "hostapp", "lane": "interaction"}
        second = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()
        assert second["motion"]["head_moving"] is True
        assert backend_host.reads == 1, "the POSE cache is untouched"
    finally:
        shim.stop()


# --- BackendHost.commanded_head_pose, the other half of the residual -------------------


def test_the_commanded_pose_prefers_the_pose_actually_fed_to_ik():
    """Pollen updates `_last_target_head_pose` on every tick of a goto's interpolation,
    so it is the INSTANTANEOUS command; `target_head_pose` is the goal and is only a
    fallback for a backend that has been commanded but has not run IK yet."""

    host = backend_host_mod.BackendHost.__new__(backend_host_mod.BackendHost)
    live = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.1, 0.2)
    goal = shim_mod._local_xyzrpy_to_matrix(0.0, 0.0, 0.0, 0.0, 0.9, 0.9)

    class Backend:
        _last_target_head_pose = live
        target_head_pose = goal

    host.backend = Backend()
    assert np.allclose(host.commanded_head_pose(), live)

    class GoalOnly:
        target_head_pose = goal

    host.backend = GoalOnly()
    assert np.allclose(host.commanded_head_pose(), goal)


def test_a_backend_that_cannot_say_answers_none():
    """"Unknown" is a real answer here and every caller degrades to what it did before
    the command existed - which is what makes a v1 rollback byte-identical."""

    host = backend_host_mod.BackendHost.__new__(backend_host_mod.BackendHost)
    host.backend = None
    assert host.commanded_head_pose() is None

    class Silent:
        pass

    host.backend = Silent()
    assert host.commanded_head_pose() is None

    class Malformed:
        _last_target_head_pose = [1.0, 2.0, 3.0]

    host.backend = Malformed()
    assert host.commanded_head_pose() is None


def test_the_commanded_pose_is_a_copy():
    """The caller does matrix arithmetic on it; handing out the backend's own array
    would let a residual computation scribble on the robot's command."""

    host = backend_host_mod.BackendHost.__new__(backend_host_mod.BackendHost)
    live = np.eye(4)

    class Backend:
        _last_target_head_pose = live

    host.backend = Backend()
    got = host.commanded_head_pose()
    got[0, 0] = 99.0
    assert live[0, 0] == 1.0


# --- refusals, the vocabulary the hostapp already understands --------------------------


def test_a_refused_motor_mode_is_a_500_and_the_client_latches_it(rig):
    """v1.9.0's firmware answers ``gravity_compensation`` with an empty 500 and
    ``HttpRobotClient`` latches the mode off after ONE failure. v2 inherits the refusal
    (the arbiter latches it too), so the shim must keep the shape the latch keys on."""

    rig.arbiter.refuse_modes = ("gravity_compensation",)
    c = HttpRobotClient(rig.url)
    try:
        c.set_motor_mode("gravity_compensation")
        assert rig.arbiter.mode_calls == ["gravity_compensation"]
        c.set_motor_mode("gravity_compensation")
        c.set_motor_mode("gravity_compensation")
        assert rig.arbiter.mode_calls == ["gravity_compensation"]   # never sent again
        # ... and the latch is per mode: 'enabled' still gets through.
        c.set_motor_mode("enabled")
        assert rig.arbiter.mode_calls == ["gravity_compensation", "enabled"]
    finally:
        c.close()


def test_an_unknown_motor_mode_is_422_and_never_reaches_the_arbiter(rig):
    r = httpx.post(f"{rig.url}/api/motors/set_mode/floppy", json={}, timeout=5.0)
    assert r.status_code == 422
    assert rig.arbiter.mode_calls == []


def test_a_refused_move_is_409_with_the_arbiters_error_code(rig):
    rig.arbiter.refuse_submit = "dropped"
    r = httpx.post(f"{rig.url}/api/move/goto",
                   json={"duration": 0.3, "body_yaw": 0.1}, timeout=5.0)
    assert r.status_code == 409
    assert r.json()["error_code"] == "dropped"


def test_a_missing_recorded_move_is_404_not_409(rig):
    rig.arbiter.refuse_submit = "not_found"
    r = httpx.post(
        f"{rig.url}/api/move/play/recorded-move-dataset/pollen-robotics/lib/nope",
        json={}, timeout=5.0)
    assert r.status_code == 404
    assert r.json()["error_code"] == "not_found"


def test_an_unknown_interpolation_is_rejected_at_the_boundary(rig):
    r = httpx.post(f"{rig.url}/api/move/goto",
                   json={"duration": 0.3, "interpolation": "bouncy"}, timeout=5.0)
    assert r.status_code == 422
    assert rig.arbiter.submitted == []


def test_a_refused_goto_does_not_take_down_the_hostapp(client, rig):
    rig.arbiter.refuse_submit = "head_reserved"
    client.apply(MotionIntent(kind="goto", head=HeadPose(pitch=3.0)))  # logs, never raises
    assert rig.arbiter.submitted[-1].kind == "goto"


# --- the labelling headers ------------------------------------------------------------


def test_the_reachy_headers_relabel_client_and_lane(rig):
    """Documented for a future hostapp that wants its expressive motion droppable
    while its interaction mechanics stay on the default lane."""

    httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.2},
               headers={"X-Reachy-Client": "kiosk", "X-Reachy-Lane": "expression"},
               timeout=5.0)
    req = rig.arbiter.submitted[-1]
    assert (req.client, req.lane) == ("kiosk", "expression")


def test_an_unlabelled_request_is_the_hostapp_on_the_interaction_lane(rig):
    httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.2}, timeout=5.0)
    req = rig.arbiter.submitted[-1]
    assert (req.client, req.lane) == ("hostapp", "interaction")


def test_an_unknown_lane_is_the_arbiters_call_not_the_shims(rig):
    """`bad_lane` is a refusal rule in the arbiter; the shim must not pre-empt it, or
    the two would drift and only one of them would be tested."""

    rig.arbiter.refuse_submit = "bad_lane"
    r = httpx.post(f"{rig.url}/api/move/goto", json={"duration": 0.2},
                   headers={"X-Reachy-Lane": "nonsense"}, timeout=5.0)
    assert r.status_code == 409
    assert rig.arbiter.submitted[-1].lane == "nonsense"


# --- the inert surfaces ---------------------------------------------------------------


def test_media_routes_answer_200_so_the_client_never_latches_audio_off(client, rig):
    client.apply(MotionIntent(kind="play_sound", sound="mmhm.wav"))
    client.apply(MotionIntent(kind="wobble", wobble=True))
    client.apply(MotionIntent(kind="wobble", wobble=False))
    assert client._media_ok is True
    # ... and none of it became motion.
    assert rig.arbiter.submitted == []


def test_health_check_is_a_post(rig):
    assert httpx.post(f"{rig.url}/health-check", timeout=5.0).json() == {"status": "ok"}
    assert httpx.get(f"{rig.url}/health-check", timeout=5.0).status_code == 405


def test_goto_sleep_is_served_too(rig):
    r = httpx.post(f"{rig.url}/api/move/play/goto_sleep", json={}, timeout=5.0)
    assert r.status_code == 200 and r.json()["uuid"]
    assert rig.arbiter.submitted[-1].kind == "goto_sleep"


# --- the whole hostapp player, booting against the shim --------------------------------


def test_robot_player_boots_against_the_shim(rig):
    """The proof the shim exists for: the hostapp's real player, with its real client,
    reaches its wake-and-ready state against the ROS 2 node.

    The boot sequence is `health()` → motors `enabled` (v1.9.0 boots them disabled and
    nothing else ever enables them) → `wake_up()` → 0.6 s settle → re-assert
    `gravity_compensation` so the head is back-drivable for the gesture sensor.
    """

    client = HttpRobotClient(rig.url)
    player = RobotPlayer(client, gravity_comp=True)
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if "gravity_compensation" in rig.arbiter.mode_calls:
                break
            time.sleep(0.05)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
        client.close()

    assert rig.arbiter.mode_calls[0] == "enabled", rig.arbiter.mode_calls
    assert "gravity_compensation" in rig.arbiter.mode_calls, rig.arbiter.mode_calls
    kinds = [req.kind for req in rig.arbiter.submitted]
    assert kinds == ["wake_up"], kinds
    assert rig.arbiter.submitted[0].client == "hostapp"
    assert player.busy is False


def test_robot_player_plays_a_batch_through_the_shim(rig):
    """One submitted batch, one arbiter request per intent, in order."""

    client = HttpRobotClient(rig.url)
    player = RobotPlayer(client, gravity_comp=False)
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not rig.arbiter.submitted:
            time.sleep(0.05)   # let the boot wake land first
        player.submit([
            MotionIntent(kind="goto", head=HeadPose(pitch=-8.0), duration=0.05),
            MotionIntent(kind="goto", head=HeadPose(pitch=0.0), duration=0.05),
        ])
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and len(rig.arbiter.submitted) < 3:
            time.sleep(0.05)
    finally:
        player.stop()
        player.join(timeout_s=5.0)
        client.close()

    kinds = [req.kind for req in rig.arbiter.submitted]
    assert kinds == ["wake_up", "goto", "goto"], kinds
    pitches = [shim_mod._local_matrix_to_xyzrpy(r.head_pose)[4]
               for r in rig.arbiter.submitted[1:]]
    assert pitches[0] == pytest.approx(math.radians(-8.0))
    assert pitches[1] == pytest.approx(0.0)


# --- the shim's own lifecycle ---------------------------------------------------------


def test_stop_is_idempotent_and_frees_the_port():
    backend_host = FakeBackendHost()
    shim = shim_mod.start_rest_shim(FakeArbiter(backend_host=backend_host), backend_host,
                                    host="127.0.0.1", port=0)
    assert shim.wait_until_ready(10.0)
    port = shim.port
    shim.stop()
    shim.stop()   # idempotent
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/api/daemon/status", timeout=2.0)


def test_the_shim_never_imports_the_adapter_core():
    """`reachy_mini_ros2` ships in images with no EMORI core installed (and in the
    publishable direction generally), so one `from emori_adapter...` would be an
    ImportError at node start — a failure that reads exactly like a slow boot.

    Parsed, not grepped: the module docstring discusses `HttpRobotClient` at length, and
    prose about a package is not a dependency on it.
    """

    tree = ast.parse(_SHIM.read_text(encoding="utf-8"), filename=str(_SHIM))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    offenders = [m for m in modules if m == "emori_adapter" or m.startswith("emori_adapter.")]
    assert offenders == []
    assert "rclpy" not in modules   # the shim is pure HTTP; the node owns the ROS side


def test_the_motion_block_reports_face_freshness_for_the_hosts_watchdog():
    """motion.face_seen_s: how long since the node's face tracker last selected a face.
    The host's conversation watchdog reads it as presence: gaze_active only
    says a head WRITE was accepted, and goes quiet on a learner who stands still."""

    backend_host = FakeBackendHost()
    arbiter = FakeArbiter(backend_host=backend_host)
    ages = iter((0.4, None))
    shim = shim_mod.start_rest_shim(arbiter, backend_host, host="127.0.0.1", port=0,
                                    state_cache_s=0.0, face_seen=lambda: next(ages))
    try:
        assert shim.wait_until_ready(10.0)
        first = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()["motion"]
        second = httpx.get(f"{shim.url}/api/state/full", timeout=5.0).json()["motion"]
        assert first["face_seen_s"] == 0.4
        assert second["face_seen_s"] is None
    finally:
        shim.stop()


def test_a_node_without_gaze_reports_no_face(rig):
    assert _state(rig)["motion"]["face_seen_s"] is None
