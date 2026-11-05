"""Look-at geometry and camera intrinsics for the face look-at component.

# Vendored from reachy_mini 1.10.0 (reachy_mini/vision/face_detector.py,
# reachy_mini/vision/face_tracking.py, reachy_mini/vision/look_at.py,
# reachy_mini/media/camera_utils.py) -- Copyright 2025 Pollen Robotics, Apache-2.0
# (the same licence as this package; see THIRD_PARTY_NOTICES.md).
# Vendored rather than imported because (a) the pinned dev venv is reachy_mini 1.8.0,
# which has no vision/ and no intrinsics_for_size, so an import here would make every
# host test of this logic impossible; (b) the SDK's FaceDetector calls hf_hub_download
# in its CONSTRUCTOR, and this node runs with HF_HUB_OFFLINE=1 and no yunet in the
# cache; (c) face_tracking.py's thread is welded to the daemon's GStreamer pipe, which
# v2 deleted.  Changes from upstream are marked EMORI:.

Pure numpy + scipy.  No ROS, no onnxruntime, no relative import: this module is loaded
BY PATH by `tests/test_gaze_geometry.py`, the same convention `tests/test_reachy_video.py`
documents, because `ros2_ws` is not on `sys.path` and putting it there would need ROS.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from scipy.spatial.transform import Rotation as R

# --- the wireless robot's calibration, copied from reachy_mini.media.camera_constants ---
# `ReachyMiniLiteCamSpecs.K` / `.D` (camera_constants.py:219-241), which
# `ReachyMiniWirelessCamSpecs` inherits unchanged.  Copied rather than imported so this
# module needs no `reachy_mini` at all, and so the numbers are visible next to the
# arithmetic in `intrinsics_for_frame` that corrects them.

#: The resolution the calibration above was taken at (1.10.0's `_CALIBRATION_SIZE`).
CALIBRATION_SIZE: tuple[int, int] = (3840, 2592)

#: The imx708_wide's full active array width, in native columns. On the robot,
#: /sys/class/video4linux/v4l-subdev0/name == "imx708_wide"; the IMX708
#: active array is 4608 x 2592.  See `intrinsics_for_frame` for why this matters.
SENSOR_FULL_WIDTH_PX = 4608

WIRELESS_K: npt.NDArray[np.float64] = np.array(
    [
        [2001.8076426486707, 0.0, 1905.876059826701],
        [0.0, 2003.0778885944105, 1328.3239717935594],
        [0.0, 0.0, 1.0],
    ]
)

WIRELESS_D: npt.NDArray[np.float64] = np.array(
    [
        -1.4652320301298614,
        0.6542714131667414,
        0.012147809271745049,
        -0.002677286460143648,
        0.3035939941825349,
        -1.4300809080461876,
        0.570024082887235,
        0.3567299243352951,
        0.003057363348400015,
        0.0003357614008682464,
        -0.009897126394310923,
        -0.002050919484589521,
    ]
)


def undistort_points(
    u: float,
    v: float,
    K: npt.NDArray[np.float64],
    D: npt.NDArray[np.float64],
    max_iterations: int = 20,
    epsilon: float = 0.01,
) -> tuple[float, float]:
    """Undistort a single pixel coordinate to normalized camera coordinates.

    Pure numpy equivalent of cv2.undistortPoints(). Supports the OpenCV distortion
    model with up to 12 coefficients (rational model + thin prism):
        D = (k1, k2, p1, p2, k3, k4, k5, k6, s1, s2, s3, s4)

    Also works with 5-coefficient models (k1, k2, p1, p2, k3) and zero-distortion.

    The algorithm matches OpenCV's cvUndistortPointsInternal:
        1. Remove camera intrinsics to get normalized distorted coordinates.
        2. Iteratively solve for undistorted coordinates using a damped
           fixed-point method with adaptive step size.

    Args:
        u: Horizontal pixel coordinate.
        v: Vertical pixel coordinate.
        K: 3x3 camera intrinsic matrix [[fx, 0, cx], [0, fy, cy], [0, 0, 1]].
        D: Distortion coefficients array. Supports lengths 0, 4, 5, 8, 12, or 14.
            Unused positions default to 0.
        max_iterations: Maximum number of iterations (default 20).
        epsilon: Convergence threshold in pixel reprojection error (default 0.01).

    Returns:
        (x_n, y_n): Normalized undistorted coordinates (on the z=1 plane).

    Reference:
        OpenCV distortion model and undistortPoints algorithm:
        https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html
        https://github.com/opencv/opencv/blob/4.x/modules/calib3d/src/undistort.dispatch.cpp

    """
    # Extract intrinsics
    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])

    # Step 1: Remove intrinsics to get normalized distorted coordinates
    x0 = (u - cx) / fx
    y0 = (v - cy) / fy

    # Pad D to 14 elements so indexing is safe (OpenCV convention)
    d = np.zeros(14)
    n = min(len(D), 14)
    d[:n] = D[:n]

    # OpenCV coefficient ordering: k1=d[0], k2=d[1], p1=d[2], p2=d[3], k3=d[4],
    # k4=d[5], k5=d[6], k6=d[7], s1=d[8], s2=d[9], s3=d[10], s4=d[11]

    # Step 2: Damped fixed-point iteration matching OpenCV's algorithm.
    # We want to find (x, y) such that distort(x, y) = (x0, y0).
    x = x0
    y = y0
    alpha = 1.0  # damping factor
    prev_error = float("inf")

    for _ in range(max_iterations):
        r2 = x * x + y * y

        # icdist = (1 + k4*r2 + k5*r4 + k6*r6) / (1 + k1*r2 + k2*r4 + k3*r6)
        # This is the inverse of the radial distortion factor.
        numerator = 1.0 + (d[7] * r2 + d[6]) * r2 + d[5]  # k6*r2 + k5
        numerator = numerator * r2 + 1.0  # full: 1 + k4*r2 + k5*r4 + k6*r6
        # Recompute correctly using Horner's method:
        numerator = 1.0 + ((d[7] * r2 + d[6]) * r2 + d[5]) * r2
        denominator = 1.0 + ((d[4] * r2 + d[1]) * r2 + d[0]) * r2

        if denominator == 0.0:
            icdist = 1.0
        else:
            icdist = numerator / denominator

        if icdist < 0:
            # Distortion model is invalid at this radius, fall back to pinhole
            return float(x0), float(y0)

        # Tangential distortion
        delta_x = (
            2.0 * d[2] * x * y + d[3] * (r2 + 2.0 * x * x) + d[8] * r2 + d[9] * r2 * r2
        )
        delta_y = (
            d[2] * (r2 + 2.0 * y * y)
            + 2.0 * d[3] * x * y
            + d[10] * r2
            + d[11] * r2 * r2
        )

        # Damped fixed-point update
        new_x = (1.0 - alpha) * x + alpha * (x0 - delta_x) * icdist
        new_y = (1.0 - alpha) * y + alpha * (y0 - delta_y) * icdist

        # Compute reprojection error to check convergence
        # Forward-project (new_x, new_y) back to pixel coordinates
        nr2 = new_x * new_x + new_y * new_y
        nr4 = nr2 * nr2
        nr6 = nr4 * nr2
        cdist = 1.0 + d[0] * nr2 + d[1] * nr4 + d[4] * nr6
        icdist2_den = 1.0 + d[5] * nr2 + d[6] * nr4 + d[7] * nr6
        icdist2 = 1.0 / icdist2_den if icdist2_den != 0.0 else 1.0

        a1 = 2.0 * new_x * new_y
        a2 = nr2 + 2.0 * new_x * new_x
        a3 = nr2 + 2.0 * new_y * new_y

        xd = new_x * cdist * icdist2 + d[2] * a1 + d[3] * a2 + d[8] * nr2 + d[9] * nr4
        yd = new_y * cdist * icdist2 + d[2] * a3 + d[3] * a1 + d[10] * nr2 + d[11] * nr4

        x_proj = xd * fx + cx
        y_proj = yd * fy + cy
        error = ((x_proj - u) ** 2 + (y_proj - v) ** 2) ** 0.5

        if error < epsilon:
            return float(new_x), float(new_y)

        if error > prev_error:
            # Reduce step size when diverging
            alpha *= 0.5
        else:
            x = new_x
            y = new_y

        prev_error = error

    return float(x), float(y)


def scale_intrinsics(
    K_original: npt.NDArray[np.float64],
    original_size: tuple[int, int],
    target_size: tuple[int, int],
    crop_scale: float,
) -> npt.NDArray[np.float64]:
    """Scale camera intrinsics for a different resolution with cropping.

    Args:
        K_original: Original 3x3 camera matrix
        original_size: (width, height) of original calibration
        target_size: (width, height) of target resolution
        crop_scale: Scale factor due to digital zoom/crop (>1 means more zoomed in)

    Returns:
        K_scaled: Adjusted camera matrix for target resolution

    """
    K_scaled: npt.NDArray[np.float64] = K_original.copy()

    orig_w, orig_h = original_size
    target_w, target_h = target_size

    # Extract original parameters
    fx = K_original[0, 0]
    fy = K_original[1, 1]
    cx = K_original[0, 2]
    cy = K_original[1, 2]

    # Focal length scaling has two components:
    # 1. Resolution scaling: focal length in pixels scales with image dimensions
    # 2. Crop/zoom scaling: cropping increases effective focal length

    resolution_scale_x = target_w / orig_w
    resolution_scale_y = target_h / orig_h

    fx_scaled = fx * resolution_scale_x * crop_scale
    fy_scaled = fy * resolution_scale_y * crop_scale

    # Principal point scales with resolution
    # For centered crop, it stays at the image center after scaling
    cx_scaled = (cx / orig_w) * target_w
    cy_scaled = (cy / orig_h) * target_h

    K_scaled[0, 0] = fx_scaled
    K_scaled[1, 1] = fy_scaled
    K_scaled[0, 2] = cx_scaled
    K_scaled[1, 2] = cy_scaled

    return K_scaled


# Camera extrinsics from the CAD: camera offset in the head frame, plus the optical->head axis rotation.
DEFAULT_HEAD_TO_CAMERA_TRANSFORM: npt.NDArray[np.float64] = np.eye(4)
DEFAULT_HEAD_TO_CAMERA_TRANSFORM[:3, 3] = [0.0437, 0.0, 0.0512]
DEFAULT_HEAD_TO_CAMERA_TRANSFORM[:3, :3] = np.array(
    [
        [0.0, 0.0, 1.0],
        [-1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
    ]
)


def default_head_to_camera_transform() -> npt.NDArray[np.float64]:
    """Return the default transform from head frame to camera frame."""
    return DEFAULT_HEAD_TO_CAMERA_TRANSFORM.copy()


def look_at_world_pose(x: float, y: float, z: float) -> npt.NDArray[np.float64]:
    """Return a head pose that re-centers the head on the origin and aims its +X axis at the world point."""
    target_position = np.array([x, y, z], dtype=np.float64)
    target_norm = np.linalg.norm(target_position)
    if target_norm < 1e-12:
        return np.eye(4, dtype=np.float64)

    target_vector = target_position / target_norm
    straight_head_vector = np.array([1.0, 0.0, 0.0], dtype=np.float64)

    axis = np.cross(straight_head_vector, target_vector)
    axis_norm = np.linalg.norm(axis)
    if axis_norm < 1e-8:
        if np.dot(straight_head_vector, target_vector) > 0:
            rot_mat = np.eye(3, dtype=np.float64)
        else:
            perp = (
                np.array([0.0, 1.0, 0.0], dtype=np.float64)
                if abs(straight_head_vector[0]) < 0.9
                else np.array([0.0, 0.0, 1.0], dtype=np.float64)
            )
            axis = np.cross(straight_head_vector, perp)
            axis /= np.linalg.norm(axis)
            rot_mat = R.from_rotvec(np.pi * axis).as_matrix()
    else:
        axis /= axis_norm
        angle = np.arccos(
            np.clip(np.dot(straight_head_vector, target_vector), -1.0, 1.0)
        )
        rot_mat = R.from_rotvec(angle * axis).as_matrix()

    target_head_pose = np.eye(4, dtype=np.float64)
    target_head_pose[:3, :3] = rot_mat
    return target_head_pose


def look_at_image_pose(
    u: float,
    v: float,
    K: npt.NDArray[np.float64],
    D: npt.NDArray[np.float64],
    T_world_head: npt.NDArray[np.float64],
    T_head_cam: npt.NDArray[np.float64] | None = None,
) -> npt.NDArray[np.float64]:
    """Return the head pose that looks at a pixel in the current camera frame."""
    if T_head_cam is None:
        T_head_cam = DEFAULT_HEAD_TO_CAMERA_TRANSFORM

    x_n, y_n = undistort_points(u, v, K, D)

    ray_cam = np.array([x_n, y_n, 1.0], dtype=np.float64)

    # Aim along the ray direction; adding the camera-head offset biases it up.
    ray_world = (T_world_head @ T_head_cam)[:3, :3] @ ray_cam
    return look_at_world_pose(
        float(ray_world[0]), float(ray_world[1]), float(ray_world[2])
    )


def intrinsics_for_frame(
    width: int,
    height: int,
    *,
    full_width_px: int = SENSOR_FULL_WIDTH_PX,
    fx_scale: float = 1.0,
    fy_scale: float = 1.0,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """EMORI: K and D for a frame that covers the sensor's FULL field.

    NOT ``scale_intrinsics(K, CALIBRATION_SIZE, (w, h), 1.0)``.  The calibration was
    taken at 3840x2592 on an imx708_wide whose active array is 4608x2592, i.e. it is a
    horizontal CROP of 768 columns -- so scaling fx by w/3840 assumes the target image
    shows only the calibrated field, and the camera bridge's 1280x720 shows all of it.
    That single mistake makes fx 1.2x too large and the yaw loop gain ~14% low.

    The check that is not circular: at 1280x720 the corrected pair is fx = fy = 556.1,
    i.e. square pixels on a square-pixel sensor, giving a ~98 deg HFOV beside a ~66 deg
    VFOV -- the Camera Module 3 Wide's published figures.  The naive pair gives fx != fy
    on a square-pixel sensor and an implausible 87 deg HFOV beside a 66 deg VFOV.

    Vertical is unaffected: the calibration's 2592 rows ARE the full field, so fy and cy
    scale by height/2592 exactly as upstream would.

    `cx`/`cy` is the FIXED POINT of the gaze loop - the pixel the head converges to.
    `fx`/`fy` set only the loop GAIN, which is why this correction changes convergence
    speed rather than aim, and why `fx_scale`/`fy_scale` are per-axis trim knobs rather
    than a safety mechanism.  They are separate because one knob can trim gain but can
    never correct an aspect error.
    """

    if width <= 0 or height <= 0:
        raise ValueError(f"a frame has positive dimensions; got {width}x{height}")
    if full_width_px <= 0:
        raise ValueError(f"the sensor width must be positive; got {full_width_px}")

    # The crop's left edge in native sensor columns: (4608 - 3840) / 2 = 384.
    left = (full_width_px - CALIBRATION_SIZE[0]) / 2.0
    K = WIRELESS_K.copy()
    K[0, 0] = WIRELESS_K[0, 0] * width / full_width_px * fx_scale
    K[1, 1] = WIRELESS_K[1, 1] * height / CALIBRATION_SIZE[1] * fy_scale
    K[0, 2] = (left + WIRELESS_K[0, 2]) * width / full_width_px
    K[1, 2] = WIRELESS_K[1, 2] * height / CALIBRATION_SIZE[1]
    return K, WIRELESS_D
