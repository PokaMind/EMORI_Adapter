"""``state_snapshot``: the robot's state, read once and served the same on every transport.

The REST shim and the ROS services answer the same two questions - ``GET /api/state/full``
and ``GET /api/daemon/status`` - and they must not be able to disagree. Both are built from
``make_state_snapshot``, so these tests hold the factory to the shim's wire from the
outside: the shim is built over the same fakes and asked over a real ASGI client, and what
it says is compared with what the factory says, field for field.

Loaded BY PATH, like the other ``reachy_mini_ros2`` tests: ``ros2_ws`` is not on
``sys.path`` and putting it there would need ROS. The module under test is the one the shim
itself loaded, so the shim's by-path fallback is exercised too.
"""

from __future__ import annotations

import ast
import importlib.util
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import numpy as np
import pytest
from starlette.testclient import TestClient

_PACKAGE = (
    Path(__file__).resolve().parents[1]
    / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2"
)
_SHIM = _PACKAGE / "rest_shim.py"
_SNAPSHOT = _PACKAGE / "state_snapshot.py"

_FROZEN = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shim_mod = _load("reachy_mini_rest_shim_for_state_snapshot", _SHIM)
snap = shim_mod._state


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now`` is always the same instant."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return _FROZEN


class Backend:
    """``backend_host``: the reads and the two readiness answers, with switches."""

    def __init__(self, **fields: Any) -> None:
        self.kind = "mujoco"
        self.mode = "enabled"
        self.ready: Any = True
        self.fault: Any = ""
        self.commanded: Optional[np.ndarray] = None
        self.pose_reads = 0
        self.pitch = 0.1
        self.raise_on: set[str] = set()
        self.__dict__.update(fields)

    def _maybe_raise(self, what: str) -> None:
        if what in self.raise_on:
            raise RuntimeError(f"{what} failed")

    def is_ready(self) -> bool:
        self._maybe_raise("is_ready")
        return self.ready

    def error(self) -> str:
        self._maybe_raise("error")
        return self.fault

    def present_head_pose(self) -> np.ndarray:
        self._maybe_raise("present_head_pose")
        self.pose_reads += 1
        return snap._local_xyzrpy_to_matrix(0.001, 0.002, 0.012, 0.05, self.pitch, -0.25)

    def commanded_head_pose(self) -> Optional[np.ndarray]:
        return self.commanded

    def present_joints(self) -> list[float]:
        self._maybe_raise("present_joints")
        return [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26, 0.31, -0.32]

    def motor_mode(self) -> str:
        self._maybe_raise("motor_mode")
        return self.mode


class Arbiter:
    """Just what the state reads ask of the arbiter."""

    def __init__(self) -> None:
        self.motion = {"active": True, "head_moving": False, "client": "hostapp",
                       "lane": "interaction"}

    def motion_snapshot(self) -> dict:
        return dict(self.motion)


def _factory(backend=None, arbiter=None, **kwargs):
    kwargs.setdefault("state_cache_s", 1e9)
    return snap.make_state_snapshot(
        arbiter if arbiter is not None else Arbiter(),
        backend if backend is not None else Backend(),
        **kwargs,
    )


def _rest(backend, arbiter=None, **kwargs) -> TestClient:
    kwargs.setdefault("state_cache_s", 1e9)
    app = shim_mod.build_app(arbiter if arbiter is not None else Arbiter(), backend, **kwargs)
    return TestClient(app, base_url="http://shim.test", raise_server_exceptions=False)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(snap, "datetime", _FrozenDatetime)


# --- the module stands alone ----------------------------------------------------------


def test_the_snapshot_module_imports_no_ros_and_no_web_stack():
    """It is shared by the REST shim and the ROS surface, and loads on a box with neither."""

    tree = ast.parse(_SNAPSHOT.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.add(node.module.split(".")[0])
    forbidden = {"rclpy", "fastapi", "uvicorn", "starlette", "reachy_mini", "reachy_mini_msgs"}
    assert not top_level & forbidden, sorted(top_level & forbidden)


def test_the_shim_serves_the_models_and_constants_of_the_shared_module():
    assert shim_mod.BACKEND_MARKER == snap.BACKEND_MARKER == "reachy_mini_ros2"
    assert shim_mod.XYZRPYPose is snap.XYZRPYPose
    assert shim_mod.Matrix4x4Pose is snap.Matrix4x4Pose
    assert shim_mod.FullBodyTarget is snap.FullBodyTarget


def test_mode_value_reads_an_enum_or_a_string():
    assert snap.mode_value(SimpleNamespace(value="gravity_compensation")) \
        == "gravity_compensation"
    assert snap.mode_value("enabled") == "enabled"
    assert snap.mode_value(None) == ""


# --- one frozen reading, through both transports --------------------------------------


@pytest.mark.parametrize("use_pose_matrix", [False, True])
@pytest.mark.parametrize("with_command", [False, True])
def test_rest_and_the_factory_agree_on_every_field_over_one_frozen_reading(
        frozen_clock, use_pose_matrix, with_command):
    commanded = (snap._local_xyzrpy_to_matrix(0.0, 0.0, 0.012, 0.0, 0.02, -0.2)
                 if with_command else None)
    arbiter = Arbiter()
    seen = {"gaze": True, "face": 1.2345}
    kwargs = dict(gaze_active=lambda: seen["gaze"], face_seen=lambda: seen["face"])
    served = _rest(Backend(commanded=commanded), arbiter, **kwargs).get(
        "/api/state/full", params={"use_pose_matrix": str(use_pose_matrix).lower()})
    direct = _factory(Backend(commanded=commanded), arbiter, **kwargs).snapshot(use_pose_matrix)

    assert served.status_code == 200
    assert served.json() == direct
    assert direct["timestamp"].startswith("2026-01-02T03:04:05.678")
    assert direct["motion"]["face_seen_s"] == 1.23
    assert direct["motion"]["gaze_active"] is True
    assert ("command" in direct["motion"]) is with_command
    assert ("residual" in direct["motion"]) is with_command
    assert ("m" in direct["head_pose"]) is use_pose_matrix


def test_a_frozen_reading_is_served_whole_until_it_expires():
    clock = {"now": 100.0}
    backend = Backend()
    state = _factory(backend, state_cache_s=0.05, clock=lambda: clock["now"])

    first = state.snapshot(False)
    clock["now"] += 0.049
    assert state.snapshot(False) == first
    assert state.snapshot(True)["timestamp"] == first["timestamp"]
    assert backend.pose_reads == 1

    clock["now"] += 0.002
    backend.pitch = 0.3
    fresh = state.snapshot(False)
    assert backend.pose_reads == 2
    assert fresh["head_pose"]["pitch"] != first["head_pose"]["pitch"]


def test_a_cache_of_zero_reads_the_backend_every_time():
    backend = Backend()
    state = _factory(backend, state_cache_s=0.0)
    state.snapshot(False)
    state.snapshot(False)
    assert backend.pose_reads == 2


def test_the_motion_block_is_never_served_from_the_reading_cache():
    arbiter = Arbiter()
    backend = Backend()
    state = _factory(backend, arbiter)

    assert state.snapshot(False)["motion"]["head_moving"] is False
    arbiter.motion["head_moving"] = True
    again = state.snapshot(False)
    assert again["motion"]["head_moving"] is True
    assert backend.pose_reads == 1


def test_no_motion_snapshot_on_the_arbiter_means_no_motion_key():
    state = _factory(Backend(), SimpleNamespace())
    assert "motion" not in state.snapshot(False)
    assert state.motion_block(np.eye(4)) is None


def test_a_face_seen_that_raises_costs_only_the_presence_field():
    def boom():
        raise RuntimeError("detector gone")

    motion = _factory(face_seen=boom).snapshot(False)["motion"]
    assert motion["face_seen_s"] is None
    assert motion["gaze_active"] is False


def test_the_residual_is_the_measured_pose_against_the_commanded_one():
    commanded = snap._local_xyzrpy_to_matrix(0.0, 0.0, 0.012, 0.0, 0.0, 0.0)
    backend = Backend(commanded=commanded)
    motion = _factory(backend).snapshot(False)["motion"]
    assert motion["command"] == {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    measured = backend.present_head_pose()
    expected = snap.rpy_of(measured[:3, :3] @ commanded[:3, :3].T)
    assert motion["residual"] == expected
    assert math.isclose(motion["residual"]["yaw"], -0.25, abs_tol=0.02)


def test_the_snapshot_keeps_the_daemons_joint_layout():
    full = _factory().snapshot(False)
    assert full["head_joints"] == [0.11, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26]
    assert full["body_yaw"] == 0.11
    assert full["antennas_position"] == [0.31, -0.32]
    assert full["control_mode"] == "enabled"
    assert full["passive_joints"] is None
    assert full["doa"] is None


# --- readiness: ready, not ready, unreadable, over both transports --------------------

_READINESS_CASES = {
    "ready": (dict(), True, ""),
    "not ready with a reason": (
        dict(ready=False, fault="control loop: serial port disappeared"),
        False, "control loop: serial port disappeared"),
    "not ready and silent": (
        dict(ready=False, fault=""), False, "the backend control loop is not running"),
    "readiness check raises": (
        dict(raise_on={"is_ready"}), False,
        "the backend readiness check raised: is_ready failed"),
    "reason unreadable": (
        dict(ready=False, raise_on={"error"}), False,
        "the backend error could not be read: error failed"),
}


@pytest.mark.parametrize("name", list(_READINESS_CASES))
def test_rest_and_the_factory_give_the_same_status(name):
    fields, ready, why = _READINESS_CASES[name]
    served = _rest(Backend(**fields), wireless_version=True).get("/api/daemon/status")
    direct = _factory(Backend(**fields), wireless_version=True)

    assert served.status_code == 200
    assert served.json() == direct.status()
    assert direct.readiness() == (ready, why)
    body = direct.status()
    assert body["state"] == ("running" if ready else "error")
    assert body["backend_status"]["ready"] is ready
    assert body["backend_status"]["error"] == (why or None)
    assert body["error"] == (why or None)
    assert body["backend"] == "reachy_mini_ros2"


def test_a_host_without_a_readiness_probe_reads_as_ready():
    class Bare:
        kind = "hardware"

        def motor_mode(self):
            return "enabled"

    state = _factory(Bare())
    assert state.readiness() == (True, "")
    assert state.status()["state"] == "running"


def test_readiness_follows_the_backend_live():
    backend = Backend(ready=False, fault="down")
    state = _factory(backend)
    assert state.status()["state"] == "error"
    backend.ready, backend.fault = True, ""
    assert state.status()["state"] == "running"


def test_the_status_names_what_the_node_is():
    body = _factory(Backend(kind="mockup"), version="9.9", robot_name="mini",
                    backend_name="other", wireless_version=True).status()
    assert body["simulation_enabled"] is False
    assert body["mockup_sim_enabled"] is True
    assert body["version"] == "9.9"
    assert body["robot_name"] == "mini"
    assert body["backend"] == "other"
    assert body["wireless_version"] is True
    assert body["hardware_id"] is None
    assert body["no_media"] is True and body["media_released"] is False

    sim = _factory(Backend(kind="mujoco")).status()
    assert sim["simulation_enabled"] is True and sim["mockup_sim_enabled"] is False
    forced = _factory(Backend(kind="mujoco"), simulation_enabled=False,
                      mockup_sim_enabled=True).status()
    assert forced["simulation_enabled"] is False and forced["mockup_sim_enabled"] is True


# --- a backend that raises: the factory lets it out, REST answers 500 -----------------
# The ROS handlers that wrap this are required to catch it and answer; the contract they
# are written against is that the factory itself does not swallow a failed read.


@pytest.mark.parametrize("what, route", [
    ("motor_mode", "/api/daemon/status"),
    ("present_head_pose", "/api/state/full"),
    ("present_joints", "/api/state/full"),
])
def test_a_raising_backend_read_is_a_500_on_rest_and_an_exception_from_the_factory(
        what, route):
    served = _rest(Backend(raise_on={what})).get(route)
    state = _factory(Backend(raise_on={what}))

    assert served.status_code == 500
    with pytest.raises(RuntimeError, match=f"{what} failed"):
        (state.status if route.endswith("status") else (lambda: state.snapshot(False)))()


def test_a_failed_read_is_not_cached():
    backend = Backend(raise_on={"present_joints"})
    state = _factory(backend)
    with pytest.raises(RuntimeError):
        state.snapshot(False)
    backend.raise_on.clear()
    assert state.snapshot(False)["body_yaw"] == 0.11


def test_a_missing_state_module_is_an_import_error_the_node_can_handle(tmp_path, monkeypatch):
    import importlib.util
    import shutil

    source = Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" \
        / "reachy_mini_ros2" / "rest_shim.py"
    copy = tmp_path / "rest_shim.py"
    shutil.copy(source, copy)
    spec = importlib.util.spec_from_file_location("rest_shim_without_its_sibling", copy)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.delitem(sys.modules, "reachy_mini_ros2_state_snapshot_by_path", raising=False)
    sys.modules[spec.name] = module      # `@dataclass` looks its module up here
    try:
        with pytest.raises(ImportError):
            spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
