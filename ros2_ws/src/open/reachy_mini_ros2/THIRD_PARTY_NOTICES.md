# Third-party notices — reachy_mini_ros2

This package is Apache-2.0, Copyright 2026 Pokamind AB, except for the code below, which
was copied from Pollen Robotics' Reachy Mini SDK and modified here.

## Pollen Robotics `reachy_mini` 1.10.0

* Upstream: <https://github.com/pollen-robotics/reachy_mini>, tag `v1.10.0`
  (also published on PyPI as `reachy-mini==1.10.0`).
* Licence: Apache License 2.0, the same text as this package's `LICENSE`.
* Copyright 2025 Pollen Robotics (as stated in the upstream `LICENSE`). Upstream ships no
  `NOTICE` file.

| File in this package | Taken from (upstream `src/`) |
|---|---|
| `reachy_mini_ros2/gaze/vision.py` | `reachy_mini/vision/face_detector.py` |
| `reachy_mini_ros2/gaze/geometry.py` | `reachy_mini/vision/look_at.py`, `reachy_mini/media/camera_utils.py`, and the camera constants in `reachy_mini/media/camera_constants.py` |
| `reachy_mini_ros2/gaze/tracking.py` | `reachy_mini/vision/face_tracking.py` |

Each file's module docstring says it is vendored, from which version, and why, and marks
the changes from upstream with `EMORI:`. That docstring and this file are the notice of
modification Apache-2.0 §4(b) asks for. `geometry.py`'s `intrinsics_for_frame` is not in
upstream at all.

## Not part of this package

The YuNet face-detection model the gaze component loads (`EMORI_GAZE_MODEL`,
`face_detection_yunet_2026may.onnx`) is not shipped in this package. It is MIT-licensed
(Copyright (c) 2020 Shiqi Yu, via the OpenCV Zoo); whoever installs it supplies it and its
licence. The EMORI repository keeps its copy and that licence in `deploy/ros2/models/`.
