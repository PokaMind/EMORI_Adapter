"""The robot's state, read once and served the same way over every transport.

The REST shim answers ``GET /api/state/full`` and ``GET /api/daemon/status``, and the ROS
services answer the same two questions. Both go through the one factory here, so a pose, a
motion block or a readiness verdict cannot differ between them, and the node keeps
answering when the shim is gone.

The factory is closed over the backend host, the arbiter and the gaze callables, and it
owns the short reading cache with its lock. ``snapshot`` goes through Pollen's ``FullState``
model when ``reachy_mini`` is importable, so the wire shape cannot drift from the daemon
this imitates.

Nothing here imports ROS, and every ``reachy_mini`` import is guarded, so the module loads
by path on a machine that has only numpy, scipy and pydantic.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import numpy as np
from pydantic import BaseModel

__all__ = [
    "BACKEND_MARKER",
    "SHIM_VERSION",
    "StateSnapshot",
    "import_sibling",
    "make_state_snapshot",
    "mode_value",
    "pose_conversions",
    "rpy",
    "rpy_of",
]

#: Answered verbatim as ``backend`` in the status. Tooling asserts on it to prove it is
#: talking to the ROS 2 node and not to a leftover Pollen daemon on the same port.
BACKEND_MARKER = "reachy_mini_ros2"

#: The version reported when ``reachy_mini`` cannot say its own.
SHIM_VERSION = "0.1.0"

# --- Pollen's wire models -----------------------------------------------------------
# Imported when `reachy_mini` is installed (it always is in the node's image); the local
# fall-backs exist so this module can be loaded - and tested - on a machine that only has
# pydantic. The field names are identical either way: they ARE the contract.
try:  # pragma: no cover - exercised by whichever branch the environment provides
    from reachy_mini.daemon.app.models import (  # noqa: F401
        AnyPose,
        FullBodyTarget,
        FullState,
        Matrix4x4Pose,
        XYZRPYPose,
    )
except Exception:  # pragma: no cover - reachy_mini absent

    class XYZRPYPose(BaseModel):  # type: ignore[no-redef]
        """Position in metres, orientation as extrinsic xyz Euler angles in radians."""

        x: float = 0.0
        y: float = 0.0
        z: float = 0.0
        roll: float = 0.0
        pitch: float = 0.0
        yaw: float = 0.0

    class Matrix4x4Pose(BaseModel):  # type: ignore[no-redef]
        """A 4x4 homogeneous pose, flattened row-major; translation in metres."""

        m: tuple[
            float, float, float, float,
            float, float, float, float,
            float, float, float, float,
            float, float, float, float,
        ]

    AnyPose = XYZRPYPose | Matrix4x4Pose  # type: ignore[misc,assignment]

    class FullBodyTarget(BaseModel):  # type: ignore[no-redef]
        """Streaming target: head pose, antennas ``[right, left]``, body yaw."""

        target_head_pose: Optional[AnyPose] = None
        target_antennas: Optional[tuple[float, float]] = None
        target_body_yaw: Optional[float] = None
        timestamp: Optional[datetime] = None

    class FullState(BaseModel):  # type: ignore[no-redef]
        """The 10 Hz state snapshot the hostapp's gesture sensor polls."""

        control_mode: Optional[str] = None
        head_pose: Optional[AnyPose] = None
        head_joints: Optional[list[float]] = None
        body_yaw: Optional[float] = None
        antennas_position: Optional[list[float]] = None
        timestamp: Optional[datetime] = None
        passive_joints: Optional[list[float]] = None
        doa: Optional[dict] = None


# --- sibling modules, resolved on first use ------------------------------------------
# `conversions` may not be importable (no package install under pytest), so the pose
# arithmetic falls back to local copies of the same two functions.

_conversion_cache: Optional[tuple[Callable[..., Any], Callable[..., Any]]] = None


def _local_xyzrpy_to_matrix(
    x: float, y: float, z: float, roll: float, pitch: float, yaw: float
) -> np.ndarray:
    """(x, y, z, roll, pitch, yaw) -> 4x4, exactly as ``XYZRPYPose.to_pose_array``.

    ``"xyz"`` is EXTRINSIC in scipy (lower-case), which is the convention Pollen's
    ``XYZRPYPose`` uses in both directions. Getting this wrong is invisible on a nod
    (single axis) and mirrors a curious tilt (roll+yaw together), so it is copied from
    ``daemon/app/models.py`` rather than re-derived.
    """
    from scipy.spatial.transform import Rotation

    pose = np.eye(4)
    pose[:3, 3] = [x, y, z]
    pose[:3, :3] = Rotation.from_euler("xyz", [roll, pitch, yaw]).as_matrix()
    return pose


def _local_matrix_to_xyzrpy(pose: Any) -> tuple[float, float, float, float, float, float]:
    """4x4 -> (x, y, z, roll, pitch, yaw), exactly as ``XYZRPYPose.from_pose_array``."""
    from scipy.spatial.transform import Rotation

    arr = np.asarray(pose, dtype=float).reshape((4, 4))
    roll, pitch, yaw = Rotation.from_matrix(arr[:3, :3]).as_euler("xyz")
    return (
        float(arr[0, 3]), float(arr[1, 3]), float(arr[2, 3]),
        float(roll), float(pitch), float(yaw),
    )


def import_sibling(module: str, names: tuple[str, ...]) -> Optional[tuple[Any, ...]]:
    """Import ``names`` from a sibling module of this package, or return None."""
    import importlib

    candidates = []
    if __package__:
        candidates.append(f"{__package__}.{module}")
    candidates.append(f"reachy_mini_ros2.{module}")
    for candidate in candidates:
        try:
            loaded = importlib.import_module(candidate)
            return tuple(getattr(loaded, name) for name in names)
        except Exception:
            continue
    return None


def pose_conversions() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """``(xyzrpy_to_matrix, matrix_to_xyzrpy)``, from ``conversions`` when it is there."""
    global _conversion_cache
    if _conversion_cache is None:
        found = import_sibling("conversions", ("xyzrpy_to_matrix", "matrix_to_xyzrpy"))
        if found is not None and all(callable(fn) for fn in found):
            _conversion_cache = (found[0], found[1])
        else:
            _conversion_cache = (_local_xyzrpy_to_matrix, _local_matrix_to_xyzrpy)
    return _conversion_cache


# --- small helpers -------------------------------------------------------------------


def mode_value(mode: Any) -> str:
    """Motor mode as the daemon spells it, whatever the backend hands back."""
    return str(getattr(mode, "value", mode) or "")


def rpy(pose: Any) -> dict[str, float]:
    """``{roll, pitch, yaw}`` of a 4x4, through ``conversions`` when it is importable."""

    _, from_matrix = pose_conversions()
    _x, _y, _z, roll, pitch, yaw = from_matrix(
        np.asarray(pose, dtype=float).reshape((4, 4)))
    return {"roll": float(roll), "pitch": float(pitch), "yaw": float(yaw)}


def rpy_of(rotation: Any) -> dict[str, float]:
    """``{roll, pitch, yaw}`` of a 3x3 rotation, as a proper rotation rather than a
    component-wise Euler subtraction.

    The two agree to < 0.005 rad whenever the command is within +-0.1 rad of neutral -
    which is every case the push-turn thresholds were tuned on - and this form stays
    right when it is not.
    """

    pose = np.eye(4)
    pose[:3, :3] = np.asarray(rotation, dtype=float).reshape((3, 3))
    return rpy(pose)


def reachy_mini_version() -> str | None:
    try:
        import reachy_mini

        return str(getattr(reachy_mini, "__version__", "") or "") or None
    except Exception:  # pragma: no cover - reachy_mini absent
        return None


# --- the factory ---------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    """What one robot answers about itself, as callables over a shared reading cache.

    ``read_backend`` is the cached backend reading, ``snapshot`` the ``FullState`` dict
    with its ``motion`` block, ``motion_block`` the block alone, ``readiness`` the
    ``(ready, why not)`` pair and ``status`` the daemon-status dict. ``motor_mode`` is the
    backend's mode in the daemon's spelling.
    """

    read_backend: Callable[[], tuple[np.ndarray, list[float], str, datetime]]
    snapshot: Callable[[bool], dict[str, Any]]
    motion_block: Callable[[np.ndarray], Optional[dict[str, Any]]]
    readiness: Callable[[], tuple[bool, str]]
    status: Callable[[], dict[str, Any]]
    motor_mode: Callable[[], str]


def make_state_snapshot(
    arbiter: Any,
    backend_host: Any,
    *,
    version: str | None = None,
    backend_name: str = BACKEND_MARKER,
    robot_name: str = "reachy_mini",
    wireless_version: bool = False,
    simulation_enabled: bool | None = None,
    mockup_sim_enabled: bool | None = None,
    state_cache_s: float = 0.05,
    gaze_active: Callable[[], bool] | None = None,
    face_seen: Callable[[], float | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> StateSnapshot:
    """Close the state reads over one robot.

    ``gaze_active`` is the face look-at component's liveness and ``face_seen`` how fresh
    its last detected face is; both ``None`` is a node without gaze. ``clock`` times the
    reading cache and exists so a test can move it without sleeping.
    """
    kind = str(getattr(backend_host, "kind", "") or "")
    sim = simulation_enabled if simulation_enabled is not None else kind == "mujoco"
    mockup = mockup_sim_enabled if mockup_sim_enabled is not None else kind == "mockup"
    resolved_version = version or reachy_mini_version() or SHIM_VERSION

    # `/api/state/full` is polled at 10 Hz by the gesture sensor and would otherwise take
    # a backend read per poll on the same CM4 that is running the 50 Hz control loop.
    # One cached snapshot per `state_cache_s` keeps the poll cheap without ever serving a
    # pose old enough to hide a human's nudge (the gesture sensor's own window is ~0.4 s).
    state_lock = threading.Lock()
    state_cache: dict[str, Any] = {"at": 0.0, "reading": None}

    def motor_mode() -> str:
        return mode_value(backend_host.motor_mode())

    def read_backend() -> tuple[np.ndarray, list[float], str, datetime]:
        """One cached backend reading; the timestamp is when it was READ, not served."""
        now = clock()
        with state_lock:
            reading = state_cache["reading"]
            if reading is not None and (now - state_cache["at"]) < state_cache_s:
                return reading

        reading = (
            np.asarray(backend_host.present_head_pose(), dtype=float).reshape((4, 4)),
            [float(j) for j in backend_host.present_joints()],
            motor_mode(),
            datetime.now(timezone.utc),
        )
        with state_lock:
            state_cache["at"] = now
            state_cache["reading"] = reading
        return reading

    def snapshot(use_pose_matrix: bool) -> dict[str, Any]:
        pose, joints, mode, stamp = read_backend()

        if use_pose_matrix:
            head_pose: Any = Matrix4x4Pose(m=tuple(pose.flatten().tolist()))
        else:
            _, from_matrix = pose_conversions()
            x, y, z, roll, pitch, yaw = from_matrix(pose)
            head_pose = XYZRPYPose(x=x, y=y, z=z, roll=roll, pitch=pitch, yaw=yaw)

        # `present_joints` is body_yaw, stewart_1..6, right_antenna, left_antenna - the
        # node's publishing order. The daemon's `head_joints` is its first seven (body yaw
        # at index 0) and `antennas_position` is the wire order [right, left].
        full = FullState.model_validate(
            {
                "control_mode": mode or None,
                "head_pose": head_pose,
                "head_joints": joints[:7],
                "body_yaw": joints[0] if joints else None,
                "antennas_position": joints[7:9],
                "passive_joints": None,
                "doa": None,
                "timestamp": stamp,
            }
        ).model_dump(mode="json")

        # AFTER `model_dump`, never inside `model_validate`: pydantic v2 drops unknown
        # keys on validate, so a `motion` handed to Pollen's own model would vanish
        # without a word. This is an EXTRA key on a JSON object - `read_state` returns
        # the raw dict, `pose_of_state` reduces to its own field list and `_head_angles`
        # reads `head_pose`, so nothing that speaks the daemon dialect can see it.
        motion = motion_block(pose)
        if motion is not None:
            full["motion"] = motion
        return full

    def motion_block(pose: np.ndarray) -> dict[str, Any] | None:
        """Who is moving the robot, and by how much a human is deflecting it.

        Deliberately NOT cached beside the reading: a `head_moving` flag 50 ms stale is a
        gesture interlock that fires 50 ms late. The cost is four attribute reads under
        an uncontended lock, one `getattr` plus a 4x4 copy, and one 3x3 matmul.

        None when the arbiter cannot answer (an older node, a fake in a test); the host
        application reads a missing `motion` as "this is v1" and falls back to exactly
        today's behaviour.
        """

        snapshot_fn = getattr(arbiter, "motion_snapshot", None)
        if not callable(snapshot_fn):
            return None
        motion: dict[str, Any] = dict(snapshot_fn())
        motion["gaze_active"] = bool(gaze_active()) if gaze_active is not None else False
        # Presence for the host's conversation watchdog: how fresh the last detected face
        # is. None = no face yet / gaze off / a node without gaze.
        try:
            seen = face_seen() if face_seen is not None else None
        except Exception:  # noqa: BLE001 - presence is advisory; never cost the state read
            seen = None
        motion["face_seen_s"] = None if seen is None else round(float(seen), 2)

        command_fn = getattr(backend_host, "commanded_head_pose", None)
        cmd = command_fn() if callable(command_fn) else None
        if cmd is not None:
            # The RESIDUAL is the whole point of this block: `measured - commanded` is
            # what a human's hand does to a stiff head, and it is INDEPENDENT of
            # whatever the robot is doing to itself. Computed here because this is the
            # only place that holds both matrices.
            cmd = np.asarray(cmd, dtype=float).reshape((4, 4))
            motion["command"] = rpy(cmd)
            motion["residual"] = rpy_of(pose[:3, :3] @ cmd[:3, :3].T)
        return motion

    def readiness() -> tuple[bool, str]:
        """``(is the control loop turning, why not)``.

        Both answers come from ``backend_host`` and both are wrapped: a host that does
        not implement them (an older node, a fake in a test) reads as ready, because the
        alternative is a compatibility layer that reports a healthy robot as broken.
        """

        probe = getattr(backend_host, "is_ready", None)
        if not callable(probe):
            return True, ""
        try:
            ready = bool(probe())
        except Exception as failure:  # noqa: BLE001 - an unreadable state is not ready
            return False, f"the backend readiness check raised: {failure}"
        if ready:
            return True, ""
        reason = getattr(backend_host, "error", None)
        try:
            text = str(reason() or "") if callable(reason) else str(reason or "")
        except Exception as failure:  # noqa: BLE001 - a reason nobody can read
            text = f"the backend error could not be read: {failure}"
        return False, text or "the backend control loop is not running"

    def status() -> dict[str, Any]:
        mode = motor_mode()
        # `running` is a claim about the 50 Hz control loop, not about the server that
        # relays it. The node answers perfectly well with a dead backend behind it - and
        # a hostapp (or `scripts/ros2_v2_smoke.py`, or the installer's wait loop) that
        # reads `running` there will wait forever for a robot that is gone. Pollen's own
        # daemon has `error` in its DaemonState enum (`io/protocol.py`:51) and sets
        # exactly this pair when its backend faults.
        ready, error = readiness()
        return {
            "state": "running" if ready else "error",
            "backend_status": {"motor_control_mode": mode,
                               "error": error or None,
                               "ready": ready},
            "simulation_enabled": bool(sim),
            "mockup_sim_enabled": bool(mockup),
            "no_media": True,
            "media_released": False,
            "wireless_version": bool(wireless_version),
            "version": resolved_version,
            "hardware_id": None,
            "robot_name": robot_name,
            # Not a daemon field. It is how tooling tells this node apart from a real
            # Pollen daemon squatting on :8000 before a rehearsal starts.
            "backend": backend_name,
            "error": error or None,
        }

    return StateSnapshot(
        read_backend=read_backend,
        snapshot=snapshot,
        motion_block=motion_block,
        readiness=readiness,
        status=status,
        motor_mode=motor_mode,
    )
