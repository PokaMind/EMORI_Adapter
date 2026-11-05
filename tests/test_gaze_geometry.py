"""The arithmetic that turns a pixel into a head pose, and the crop correction in it.

``reachy_mini_ros2/gaze/geometry.py`` is Pollen's own look-at geometry (Apache-2.0, vendored)
plus one thing Pollen does not have: ``intrinsics_for_frame``, which corrects a 1.2x
error in ``fx``.

The error is not a judgement call. The robot's sensor is ``imx708_wide`` (active array
4608 x 2592, read off ``/sys/class/video4linux/v4l-subdev0/name``), and
the calibration in ``camera_constants.py`` was taken at 3840 x 2592 with square pixels -
a 4608-column sensor cannot produce 3840 square-pixel columns except by cropping 768 of
them. Pollen's table labels that mode ``crop 1.0`` anyway, and ``scale_intrinsics``
scales ``fx`` by ``target_w / 3840`` on the assumption that the target image shows only
the calibrated field. The camera bridge's frames show all of it.

So this file pins the correction AS A CORRECTION: the corrected numbers, and the exact
factor by which they differ from the naive ones. Deleting ``intrinsics_for_frame`` and
falling back to ``scale_intrinsics`` would otherwise produce a head that undershoots
every horizontal target by ~14% - stable, slow, and invisible without this arithmetic.

Loaded by PATH: ``ros2_ws`` is not on ``sys.path`` and putting it there would need ROS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "gaze" / "geometry.py")
_MODULE_NAME = "reachy_mini_gaze_geometry_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


geometry = _load()

# The frame the camera bridge tees to the robot node.
_W, _H = 320, 240


@pytest.fixture(scope="module")
def intrinsics():
    return geometry.intrinsics_for_frame(_W, _H)


def _rpy(pose: np.ndarray) -> tuple[float, float, float]:
    roll, pitch, yaw = Rotation.from_matrix(np.asarray(pose)[:3, :3]).as_euler("xyz")
    return float(roll), float(pitch), float(yaw)


# --- 1. the corrected intrinsics ------------------------------------------------

def test_the_corrected_intrinsics_are_the_documented_numbers(intrinsics):
    K, _D = intrinsics
    assert round(float(K[0, 0]), 3) == 139.014        # 2001.808 * 320 / 4608
    assert round(float(K[1, 1]), 3) == 185.470        # 2003.078 * 240 / 2592
    assert round(float(K[0, 2]), 3) == 159.019        # (384 + 1905.876) * 320 / 4608
    assert round(float(K[1, 2]), 3) == 122.993        # 1328.324 * 240 / 2592


def test_the_correction_is_exactly_the_crop_ratio_and_only_in_fx(intrinsics):
    """The whole point, pinned as a DIFFERENCE. If somebody "simplifies"
    `intrinsics_for_frame` back into `scale_intrinsics`, this is what says so."""

    K, _D = intrinsics
    naive = geometry.scale_intrinsics(
        geometry.WIRELESS_K, geometry.CALIBRATION_SIZE, (_W, _H), 1.0)

    assert float(naive[0, 0]) / float(K[0, 0]) == pytest.approx(4608 / 3840, abs=1e-12)
    # Vertical is the full field in BOTH, so upstream's scaling is already right there.
    assert float(naive[1, 1]) == pytest.approx(float(K[1, 1]), abs=1e-9)
    assert float(naive[1, 2]) == pytest.approx(float(K[1, 2]), abs=1e-9)


def test_square_pixels_come_out_square_at_the_bridges_own_resolution():
    """The sanity check that is not circular: the sensor has square pixels, so at
    1280x720 - which covers the full field in both axes - the corrected fx and fy must
    agree. The naive pair does not, on a sensor where it has to."""

    K, _D = geometry.intrinsics_for_frame(1280, 720)
    assert float(K[0, 0]) == pytest.approx(float(K[1, 1]), rel=0.002)
    assert float(K[0, 0]) == pytest.approx(556.1, abs=0.5)


def test_the_trim_knobs_are_per_axis():
    """One knob can trim loop gain but can never correct an aspect error, which is why
    fx_scale and fy_scale are separate parameters rather than one."""

    base, _ = geometry.intrinsics_for_frame(_W, _H)
    trimmed, _ = geometry.intrinsics_for_frame(_W, _H, fx_scale=1.1, fy_scale=0.9)
    assert float(trimmed[0, 0]) == pytest.approx(float(base[0, 0]) * 1.1)
    assert float(trimmed[1, 1]) == pytest.approx(float(base[1, 1]) * 0.9)
    # The principal point is the loop's fixed point and is NOT a gain: trimming must
    # not move where the head converges to.
    assert float(trimmed[0, 2]) == pytest.approx(float(base[0, 2]))
    assert float(trimmed[1, 2]) == pytest.approx(float(base[1, 2]))


def test_a_different_tee_resolution_needs_no_code_change():
    """`width`/`height` come from `frame.shape`, so a robot that tees 320x180 gets the
    right cy rather than one calibrated for a 4:3 frame."""

    K, _D = geometry.intrinsics_for_frame(320, 180)
    assert float(K[1, 2]) == pytest.approx(1328.3239717935594 * 180 / 2592)
    assert float(K[0, 2]) == pytest.approx(
        geometry.intrinsics_for_frame(320, 240)[0][0, 2])


@pytest.mark.parametrize("width, height", [(0, 240), (320, 0), (-1, 240)])
def test_nonsense_frame_dimensions_are_refused(width, height):
    with pytest.raises(ValueError):
        geometry.intrinsics_for_frame(width, height)


# --- 2. pixel -> head pose ------------------------------------------------------

# The table computed with T_world_head = I and the default
# head-to-camera transform. Signs are the contract: a face RIGHT of centre asks for a
# NEGATIVE yaw (the robot turns to its own right), a face BELOW centre asks for a
# POSITIVE pitch (nose down). That agrees with `repertoire.look_around`, whose yaw goes
# +18 -> 0 -> -18 labelled "left / centre / right".
_TABLE = [
    #  u       v      roll     pitch      yaw
    (159.0, 123.0, +0.0000, +0.0000, +0.0000),
    (280.0, 123.0, +0.0001, -0.0004, -0.7199),
    (40.0, 123.0, -0.0001, -0.0004, +0.7078),
    (158.8, 200.0, +0.0003, +0.3914, +0.0016),
    (158.8, 40.0, -0.0004, -0.4274, +0.0017),
    (319.0, 239.0, -0.1866, +0.3964, -0.8719),
]


@pytest.mark.parametrize("u, v, roll, pitch, yaw", _TABLE)
def test_the_look_at_table_reproduces(intrinsics, u, v, roll, pitch, yaw):
    K, D = intrinsics
    pose = geometry.look_at_image_pose(u, v, K, D, np.eye(4))
    assert _rpy(pose) == pytest.approx((roll, pitch, yaw), abs=5e-4)


def test_aiming_at_the_principal_point_is_the_identity_rotation(intrinsics):
    """`cx`/`cy` is the FIXED POINT of the gaze loop: the pixel at which the controller
    asks for no change at all. If it were anywhere else the head would drift for ever."""

    K, D = intrinsics
    pose = geometry.look_at_image_pose(float(K[0, 2]), float(K[1, 2]), K, D, np.eye(4))
    assert np.allclose(pose[:3, :3], np.eye(3), atol=1e-6)


@pytest.mark.parametrize("u, v, expected_yaw_sign", [(280.0, 123.0, -1), (40.0, 123.0, +1)])
def test_a_face_off_to_one_side_turns_the_head_that_way(intrinsics, u, v,
                                                        expected_yaw_sign):
    K, D = intrinsics
    _roll, _pitch, yaw = _rpy(geometry.look_at_image_pose(u, v, K, D, np.eye(4)))
    assert np.sign(yaw) == expected_yaw_sign
    assert abs(yaw) > 0.5


@pytest.mark.parametrize("v, expected_pitch_sign", [(200.0, +1), (40.0, -1)])
def test_a_face_above_or_below_centre_pitches_the_head(intrinsics, v,
                                                       expected_pitch_sign):
    K, D = intrinsics
    _roll, pitch, _yaw = _rpy(geometry.look_at_image_pose(158.8, v, K, D, np.eye(4)))
    assert np.sign(pitch) == expected_pitch_sign
    assert abs(pitch) > 0.3


def test_every_target_is_rotation_only(intrinsics):
    """`look_at_world_pose` never translates, and every head pose this repo has ever
    sent is rotation-only (`HeadPose` defaults x=y=z=0). Documented as a constraint on
    anything that might later want to RAISE the head: the next gaze tick would undo it."""

    K, D = intrinsics
    for u in (0.0, 80.0, 159.0, 240.0, 319.0):
        for v in (0.0, 60.0, 123.0, 180.0, 239.0):
            pose = geometry.look_at_image_pose(u, v, K, D, np.eye(4))
            assert np.array_equal(pose[:3, 3], np.zeros(3))
            assert float(pose[3, 3]) == 1.0


def test_the_head_pose_is_carried_into_the_target(intrinsics):
    """The frame slot carries the pose SAMPLED AT CAPTURE, and the geometry composes
    with it. A target computed as if the head were at neutral would re-add the motion
    already executed, which is visible hunting around a stationary face."""

    K, D = intrinsics
    turned = np.eye(4)
    turned[:3, :3] = Rotation.from_euler("xyz", [0.0, 0.0, 0.4]).as_matrix()
    centred = geometry.look_at_image_pose(280.0, 123.0, K, D, np.eye(4))
    from_turned = geometry.look_at_image_pose(280.0, 123.0, K, D, turned)
    assert _rpy(from_turned)[2] == pytest.approx(_rpy(centred)[2] + 0.4, abs=0.02)


# --- 3. undistortion ------------------------------------------------------------

def _distort(x: float, y: float, K: np.ndarray, D: np.ndarray) -> tuple[float, float]:
    """The forward OpenCV model: normalised undistorted -> distorted pixel.

    Written out here rather than imported because `undistort_points` is the INVERSE and
    a round-trip against the same code would prove only that it is self-consistent.
    Coefficient order is OpenCV's: k1 k2 p1 p2 k3 k4 k5 k6 s1 s2 s3 s4.
    """

    d = np.zeros(14)
    d[: len(D)] = D
    r2 = x * x + y * y
    r4, r6 = r2 * r2, r2 * r2 * r2
    cdist = 1.0 + d[0] * r2 + d[1] * r4 + d[4] * r6
    icdist2 = 1.0 / (1.0 + d[5] * r2 + d[6] * r4 + d[7] * r6)
    xd = (x * cdist * icdist2 + 2.0 * d[2] * x * y + d[3] * (r2 + 2.0 * x * x)
          + d[8] * r2 + d[9] * r4)
    yd = (y * cdist * icdist2 + d[2] * (r2 + 2.0 * y * y) + 2.0 * d[3] * x * y
          + d[10] * r2 + d[11] * r4)
    return float(xd * K[0, 0] + K[0, 2]), float(yd * K[1, 1] + K[1, 2])


def test_undistortion_round_trips_across_the_frame(intrinsics):
    """A 5x5 grid over the whole 320x240 frame, back through the forward model.

    `k1 = -1.465` is a strong barrel term and the solver is a damped fixed point, so
    "it converges at the centre" says nothing about the corners - which is exactly where
    a face at the edge of a 98 deg field lives.
    """

    K, D = intrinsics
    worst = 0.0
    for u in np.linspace(2.0, _W - 3.0, 5):
        for v in np.linspace(2.0, _H - 3.0, 5):
            x_n, y_n = geometry.undistort_points(float(u), float(v), K, D)
            back_u, back_v = _distort(x_n, y_n, K, D)
            worst = max(worst, float(np.hypot(back_u - u, back_v - v)))
    assert worst < 0.2, f"worst round-trip error {worst:.3f} px"
