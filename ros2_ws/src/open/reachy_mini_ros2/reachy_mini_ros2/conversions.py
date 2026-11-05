"""Pose arithmetic between Pollen's 4x4 matrices and ROS's Pose/Quaternion - pure.

Pollen's ``Backend`` speaks 4x4 homogeneous matrices and the daemon's REST surface
speaks ``{x, y, z, roll, pitch, yaw}``; ROS speaks ``geometry_msgs/Pose`` with a
quaternion.  Three representations of one thing, and every bug that lives here is the
same bug: a robot that tilts the wrong way, silently, only on hardware.

So this module is deliberately the ONLY place the three meet, it imports nothing but
``numpy`` (no ROS, no scipy, no ``reachy_mini``), and it is exercised on the host by
``tests/test_reachy_conversions.py`` round-tripping every function against
``scipy.spatial.transform.Rotation`` - which is what Pollen itself uses, so the test
compares us against the convention that will actually be on the wire rather than
against our own arithmetic.

The convention, for the avoidance of doubt: roll/pitch/yaw are **extrinsic XYZ** Euler
angles, i.e. ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``, which is scipy's lowercase
``"xyz"`` and exactly what ``reachy_mini.daemon.app.models.XYZRPYPose`` does.  Units are
metres and radians; quaternions are ``(x, y, z, w)``, ROS's order, not ``(w, x, y, z)``.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# Below this, a quaternion is treated as degenerate and replaced by the identity rather
# than normalised into noise.  A zero quaternion reaches us from an uninitialised
# geometry_msgs/Pose (all four components 0.0), which is common enough - a caller that
# only wants to move the antennas leaves `head_pose` untouched - that it must not raise.
_QUAT_EPS = 1e-12

# Gimbal-lock guard for the matrix -> RPY extraction.  Within this of +-pi/2 pitch the
# roll/yaw split is arbitrary: only their sum (or difference) is determined.  We put the
# whole thing into yaw and zero the roll; scipy makes the opposite choice, and both are
# correct - the ROTATIONS agree, which is what the test compares.  Without the guard the
# two arctan2 calls read a 0/0 and return noise.
_GIMBAL_EPS = 1e-9

__all__ = [
    "matrix_to_pose",
    "matrix_to_quaternion",
    "matrix_to_rotation_rpy",
    "matrix_to_xyzrpy",
    "pose_to_matrix",
    "quaternion_to_matrix",
    "quaternion_to_rpy",
    "rpy_to_quaternion",
    "xyzrpy_to_matrix",
]


def rpy_to_quaternion(roll: float, pitch: float,
                      yaw: float) -> tuple[float, float, float, float]:
    """Extrinsic-XYZ Euler angles (radians) to a ROS ``(x, y, z, w)`` quaternion."""

    half_r, half_p, half_y = roll / 2.0, pitch / 2.0, yaw / 2.0
    sr, cr = np.sin(half_r), np.cos(half_r)
    sp, cp = np.sin(half_p), np.cos(half_p)
    sy, cy = np.sin(half_y), np.cos(half_y)

    # q = qz * qy * qx, matching R = Rz @ Ry @ Rx.
    return (
        float(sr * cp * cy - cr * sp * sy),
        float(cr * sp * cy + sr * cp * sy),
        float(cr * cp * sy - sr * sp * cy),
        float(cr * cp * cy + sr * sp * sy),
    )


def quaternion_to_rpy(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    """A ROS ``(x, y, z, w)`` quaternion to extrinsic-XYZ Euler angles (radians)."""

    return matrix_to_rotation_rpy(quaternion_to_matrix(x, y, z, w)[:3, :3])


def quaternion_to_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    """A ROS ``(x, y, z, w)`` quaternion to a 4x4 homogeneous matrix (no translation)."""

    norm = float(np.sqrt(x * x + y * y + z * z + w * w))
    if norm < _QUAT_EPS:
        # An all-zero orientation is an unset Pose field, not a caller error: answer
        # with "no rotation" so `use_head=false` style requests do not explode here.
        return np.eye(4)
    x, y, z, w = x / norm, y / norm, z / norm, w / norm

    matrix = np.eye(4)
    matrix[:3, :3] = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    return matrix


def matrix_to_quaternion(matrix: Sequence[Sequence[float]]) -> tuple[float, float,
                                                                     float, float]:
    """A 3x3 or 4x4 rotation to a ROS ``(x, y, z, w)`` quaternion, with ``w >= 0``.

    Shepperd's branch: taking the largest of the four possible denominators keeps the
    division well conditioned for every rotation, including the 180-degree ones where
    the naive trace formula divides by something arbitrarily close to zero.
    """

    m = np.asarray(matrix, dtype=float)
    if m.shape == (4, 4):
        m = m[:3, :3]
    if m.shape != (3, 3):
        raise ValueError(f"expected a 3x3 or 4x4 rotation, got shape {np.shape(matrix)}")

    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = float(np.sqrt(trace + 1.0)) * 2.0
        w, x = 0.25 * s, (m[2, 1] - m[1, 2]) / s
        y, z = (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = float(np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])) * 2.0
        w, x = (m[2, 1] - m[1, 2]) / s, 0.25 * s
        y, z = (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = float(np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])) * 2.0
        w, x = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s
        y, z = 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = float(np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])) * 2.0
        w, x = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s
        y, z = (m[1, 2] + m[2, 1]) / s, 0.25 * s

    # q and -q are the same rotation; pinning the sign of w makes the value comparable
    # across calls, which is what lets a test assert on the numbers at all.
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    return float(x), float(y), float(z), float(w)


def matrix_to_rotation_rpy(rotation: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """A 3x3 (or 4x4) rotation to extrinsic-XYZ ``(roll, pitch, yaw)`` in radians."""

    m = np.asarray(rotation, dtype=float)
    if m.shape == (4, 4):
        m = m[:3, :3]
    sin_pitch = float(np.clip(-m[2, 0], -1.0, 1.0))
    cos_pitch = float(np.sqrt(max(0.0, 1.0 - sin_pitch * sin_pitch)))
    if cos_pitch < _GIMBAL_EPS:
        # Gimbal lock: roll and yaw are the same axis, so only their sum is determined.
        # Put it all in yaw and zero the roll. scipy zeroes the other end instead; the
        # two triples differ, the rotations they describe do not.
        return 0.0, float(np.arcsin(sin_pitch)), float(np.arctan2(-m[0, 1], m[1, 1]))
    return (
        float(np.arctan2(m[2, 1], m[2, 2])),
        float(np.arcsin(sin_pitch)),
        float(np.arctan2(m[1, 0], m[0, 0])),
    )


def xyzrpy_to_matrix(x: float, y: float, z: float,
                     roll: float, pitch: float, yaw: float) -> np.ndarray:
    """``{x, y, z, roll, pitch, yaw}`` (metres, radians) to Pollen's 4x4 matrix.

    The exact counterpart of ``reachy_mini.daemon.app.models.XYZRPYPose.to_pose_array``;
    this is the function the REST compatibility shim uses on every goto it receives.
    """

    sr, cr = np.sin(roll), np.cos(roll)
    sp, cp = np.sin(pitch), np.cos(pitch)
    sy, cy = np.sin(yaw), np.cos(yaw)

    matrix = np.eye(4)
    matrix[:3, :3] = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])
    matrix[:3, 3] = [x, y, z]
    return matrix


def matrix_to_xyzrpy(
    matrix: Sequence[Sequence[float]],
) -> tuple[float, float, float, float, float, float]:
    """Pollen's 4x4 matrix to ``(x, y, z, roll, pitch, yaw)`` - metres and radians."""

    m = np.asarray(matrix, dtype=float)
    if m.shape != (4, 4):
        raise ValueError(f"expected a 4x4 pose, got shape {np.shape(matrix)}")
    roll, pitch, yaw = matrix_to_rotation_rpy(m[:3, :3])
    return float(m[0, 3]), float(m[1, 3]), float(m[2, 3]), roll, pitch, yaw


def pose_to_matrix(pose: Any) -> np.ndarray:
    """A ``geometry_msgs/Pose``-like object to Pollen's 4x4 matrix.

    Duck-typed on ``.position.{x,y,z}`` / ``.orientation.{x,y,z,w}`` so this module
    stays importable with no ROS installed and the tests can pass a plain namespace.
    A mapping (``{"position": {...}, "orientation": {...}}``) works too, because the
    REST shim reads dictionaries off the wire.
    """

    position = _member(pose, "position")
    orientation = _member(pose, "orientation")
    matrix = quaternion_to_matrix(
        float(_member(orientation, "x", 0.0)),
        float(_member(orientation, "y", 0.0)),
        float(_member(orientation, "z", 0.0)),
        float(_member(orientation, "w", 0.0)),
    )
    matrix[:3, 3] = [
        float(_member(position, "x", 0.0)),
        float(_member(position, "y", 0.0)),
        float(_member(position, "z", 0.0)),
    ]
    return matrix


def matrix_to_pose(
    matrix: Sequence[Sequence[float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    """Pollen's 4x4 matrix to ``((x, y, z), (qx, qy, qz, qw))``.

    Returns plain tuples rather than a ``geometry_msgs/Pose`` on purpose: this module
    must import on a machine with no ROS, and the caller that HAS ROS is one line away
    from filling a message with them.
    """

    m = np.asarray(matrix, dtype=float)
    if m.shape != (4, 4):
        raise ValueError(f"expected a 4x4 pose, got shape {np.shape(matrix)}")
    return (float(m[0, 3]), float(m[1, 3]), float(m[2, 3])), matrix_to_quaternion(m[:3, :3])


def _member(obj: Any, name: str, default: Any = None) -> Any:
    """``obj.name`` for a message, ``obj["name"]`` for a dict off the wire."""

    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
