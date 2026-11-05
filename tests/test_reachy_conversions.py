"""Pose arithmetic for the native robot node, checked against the library Pollen uses.

``reachy_mini_ros2/conversions.py`` is the single place three representations of one
rotation meet: Pollen's 4x4 matrices (what ``Backend`` takes and returns), the daemon's
``{x, y, z, roll, pitch, yaw}`` (what the host application's HTTP client sends, and what
the REST shim must keep answering), and ``geometry_msgs/Pose`` with a quaternion (what
ROS speaks).  Every bug that can live in that file is the same bug: a head that tilts
the wrong way, silently, and only on hardware.

So the module is pure numpy with no scipy, and this file checks it against
``scipy.spatial.transform.Rotation`` - which IS what ``reachy_mini`` uses internally.
Re-deriving the arithmetic here would only prove we can make the same mistake twice; the
point is to compare against the convention that will actually be on the wire.

The convention under test: roll/pitch/yaw are **extrinsic XYZ** Euler angles, scipy's
lowercase ``"xyz"``, i.e. ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`` - exactly what
``reachy_mini.daemon.app.models.XYZRPYPose`` does.  Quaternions are ``(x, y, z, w)``.

Loaded by PATH: ``ros2_ws`` is not on ``sys.path`` and putting it there would need ROS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import warnings

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "conversions.py")
_MODULE_NAME = "reachy_mini_conversions_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


conversions = _load()


# A spread that covers the easy case, both signs on every axis, a near-degenerate one
# and two at the gimbal-lock boundary, where roll and yaw stop being distinguishable.
_ANGLES = [
    (0.0, 0.0, 0.0),
    (0.1, -0.2, 0.3),
    (-0.9, 0.4, -1.4),
    (np.pi / 2, 0.0, 0.0),
    (0.0, 0.0, np.pi - 1e-6),
    (0.3, np.pi / 2 - 1e-7, -0.4),
    (-0.2, -np.pi / 2 + 1e-7, 1.1),
]

_TRANSLATIONS = [(0.0, 0.0, 0.0), (0.01, -0.02, 0.03), (-0.1, 0.25, -0.005)]


def _same_rotation(quat_a, quat_b) -> bool:
    """q and -q are the same rotation; compare as rotations, not as four numbers."""

    a = np.asarray(quat_a, dtype=float)
    b = np.asarray(quat_b, dtype=float)
    return bool(np.allclose(a, b, atol=1e-9) or np.allclose(a, -b, atol=1e-9))


# --- 1. against scipy, which is what Pollen uses -------------------------------

@pytest.mark.parametrize("rpy", _ANGLES)
def test_rpy_to_quaternion_agrees_with_scipy(rpy):
    """The `xyz` in `R.from_euler("xyz", ...)` is extrinsic, and that is load-bearing.

    Read it as intrinsic (`"XYZ"`) and every off-axis pose is wrong by a composition
    order -- small enough to look like calibration, large enough to matter.
    """

    assert _same_rotation(conversions.rpy_to_quaternion(*rpy),
                          Rotation.from_euler("xyz", rpy).as_quat())


@pytest.mark.parametrize("rpy", _ANGLES)
def test_xyzrpy_to_matrix_agrees_with_the_daemons_own_pose_array(rpy):
    """This is `XYZRPYPose.to_pose_array` re-implemented; it must be the same matrix."""

    matrix = conversions.xyzrpy_to_matrix(0.01, -0.02, 0.03, *rpy)
    expected = np.eye(4)
    expected[:3, :3] = Rotation.from_euler("xyz", rpy).as_matrix()
    expected[:3, 3] = [0.01, -0.02, 0.03]
    assert np.allclose(matrix, expected, atol=1e-12)


@pytest.mark.parametrize("rpy", _ANGLES)
def test_matrix_to_quaternion_agrees_with_scipy(rpy):
    matrix = conversions.xyzrpy_to_matrix(0.0, 0.0, 0.0, *rpy)
    assert _same_rotation(conversions.matrix_to_quaternion(matrix),
                          Rotation.from_matrix(matrix[:3, :3]).as_quat())


def test_matrix_to_quaternion_survives_a_half_turn():
    """The trace formula divides by ~0 at 180 degrees; the branch exists for this."""

    for axis in ("x", "y", "z"):
        matrix = np.eye(4)
        matrix[:3, :3] = Rotation.from_euler(axis, np.pi).as_matrix()
        quaternion = conversions.matrix_to_quaternion(matrix)
        assert np.isclose(np.linalg.norm(quaternion), 1.0, atol=1e-9)
        assert _same_rotation(quaternion, Rotation.from_euler(axis, np.pi).as_quat())


@pytest.mark.parametrize("rpy", _ANGLES)
def test_matrix_to_xyzrpy_agrees_with_scipy_including_at_gimbal_lock(rpy):
    """At +-90 degrees of pitch, roll and yaw are one axis: only their sum is determined.

    We zero the roll, scipy zeroes the yaw, and both are right. What must NOT happen is
    the unguarded version, where two arctan2 calls read a 0/0 and return noise -- which
    is a head that snaps to an arbitrary attitude exactly when it is looking straight up.
    """

    matrix = conversions.xyzrpy_to_matrix(0.0, 0.0, 0.0, *rpy)
    ours = conversions.matrix_to_xyzrpy(matrix)[3:]
    with warnings.catch_warnings():
        # scipy warns that it had to pick one of the infinitely many equivalent triples.
        # So did we; that is the case under test, not a problem to be surfaced.
        warnings.simplefilter("ignore", UserWarning)
        theirs = Rotation.from_matrix(matrix[:3, :3]).as_euler("xyz")
    # Compare the rotations they describe, not the angle triples: scipy is free to
    # choose a different but equivalent triple, and so are we.
    assert np.allclose(Rotation.from_euler("xyz", ours).as_matrix(),
                       Rotation.from_euler("xyz", theirs).as_matrix(), atol=1e-7)


# --- 2. round trips ------------------------------------------------------------

@pytest.mark.parametrize("rpy", _ANGLES)
def test_quaternion_round_trips_through_rpy(rpy):
    quaternion = conversions.rpy_to_quaternion(*rpy)
    recovered = conversions.rpy_to_quaternion(*conversions.quaternion_to_rpy(*quaternion))
    assert _same_rotation(quaternion, recovered)


@pytest.mark.parametrize("rpy", _ANGLES)
@pytest.mark.parametrize("xyz", _TRANSLATIONS)
def test_matrix_round_trips_through_xyzrpy(xyz, rpy):
    matrix = conversions.xyzrpy_to_matrix(*xyz, *rpy)
    recovered = conversions.xyzrpy_to_matrix(*conversions.matrix_to_xyzrpy(matrix))
    assert np.allclose(matrix, recovered, atol=1e-9)


@pytest.mark.parametrize("rpy", _ANGLES)
@pytest.mark.parametrize("xyz", _TRANSLATIONS)
def test_matrix_round_trips_through_a_ros_pose(xyz, rpy):
    """The trip a GoTo goal actually makes: matrix -> Pose -> matrix."""

    matrix = conversions.xyzrpy_to_matrix(*xyz, *rpy)
    (px, py, pz), (qx, qy, qz, qw) = conversions.matrix_to_pose(matrix)
    pose = SimpleNamespace(position=SimpleNamespace(x=px, y=py, z=pz),
                           orientation=SimpleNamespace(x=qx, y=qy, z=qz, w=qw))
    assert np.allclose(conversions.pose_to_matrix(pose), matrix, atol=1e-9)


def test_pose_to_matrix_reads_a_mapping_too():
    """The REST shim reads dictionaries off the wire; it must not need a message class."""

    matrix = conversions.xyzrpy_to_matrix(0.0, 0.01, 0.02, 0.1, 0.2, 0.3)
    (px, py, pz), (qx, qy, qz, qw) = conversions.matrix_to_pose(matrix)
    as_dict = {"position": {"x": px, "y": py, "z": pz},
               "orientation": {"x": qx, "y": qy, "z": qz, "w": qw}}
    assert np.allclose(conversions.pose_to_matrix(as_dict), matrix, atol=1e-9)


# --- 3. the degenerate inputs that actually reach it ---------------------------

def test_an_unset_orientation_reads_as_no_rotation():
    """`use_head=false` leaves geometry_msgs/Pose zeroed, including w.

    An all-zero quaternion is not a caller error - it is an untouched message field -
    and normalising it would divide by zero. It must mean "no rotation".
    """

    pose = SimpleNamespace(position=SimpleNamespace(x=0.0, y=0.0, z=0.05),
                           orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0))
    matrix = conversions.pose_to_matrix(pose)
    assert np.allclose(matrix[:3, :3], np.eye(3))
    assert np.allclose(matrix[:3, 3], [0.0, 0.0, 0.05])


def test_an_unnormalised_quaternion_is_normalised_rather_than_trusted():
    quaternion = conversions.rpy_to_quaternion(0.2, -0.3, 0.4)
    doubled = tuple(2.0 * value for value in quaternion)
    assert np.allclose(conversions.quaternion_to_matrix(*doubled),
                       conversions.quaternion_to_matrix(*quaternion), atol=1e-12)


@pytest.mark.parametrize("bad", [np.eye(3), np.zeros((2, 2)), np.zeros((4, 3))])
def test_a_wrongly_shaped_pose_is_refused_rather_than_silently_truncated(bad):
    with pytest.raises(ValueError):
        conversions.matrix_to_xyzrpy(bad)
    with pytest.raises(ValueError):
        conversions.matrix_to_pose(bad)


# --- 4. the module must stay pure ---------------------------------------------

def test_the_module_imports_no_ros_and_no_robot_library():
    """It is loaded by path on a machine with neither; an import here is a collection
    error for every test in this file, so it is pinned deliberately."""

    import ast

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "numpy"}, imported
