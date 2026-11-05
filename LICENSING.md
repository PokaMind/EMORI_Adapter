# Licensing

## Licence

The whole repository is licensed under the Apache License, Version 2.0 (Apache-2.0),
Copyright 2026 Pokamind AB. The licence text is in [`LICENSE`](LICENSE) and the
attributions are in [`NOTICE`](NOTICE). The few files written by other people keep their
own licence and copyright; they are listed under [Third-party code](#third-party-code).

Pokamind itself is a separate, closed-source service operated by Pokamind AB. It is not
part of this repository; this repository contains only the robot side and its client of
the public Pokamind API.

## ROS 2 packages

Every ROS 2 package is Apache-2.0 and carries its own `LICENSE` file (the same text as the
root `LICENSE`) and an Apache-2.0 `<license>` in its `package.xml`.

| Package | Directory | Licence |
|---|---|---|
| `emori_msgs` | `ros2_ws/src/open/` | Apache-2.0 |
| `hri_context_analyzer` | `ros2_ws/src/open/` | Apache-2.0 |
| `hri_engagement_behaviors` | `ros2_ws/src/open/` | Apache-2.0 |
| `emori_skill_facade` | `ros2_ws/src/open/` | Apache-2.0 |
| `reachy_mini_msgs` | `ros2_ws/src/open/` | Apache-2.0 |
| `reachy_mini_ros2` | `ros2_ws/src/open/` | Apache-2.0 |
| `emori_adapter_ros` | `ros2_ws/src/emori/` | Apache-2.0 |
| `emori_adapter_bringup` | `ros2_ws/src/emori/` | Apache-2.0 |
| `emori_skill_native` | `ros2_ws/src/emori/` | Apache-2.0 |
| `emori_hub` | `ros2_ws/src/emori/` | Apache-2.0 |
| `fiware_bridge` | `ros2_ws/src/emori/` | Apache-2.0 |

The directory says how a package relates to the rest of the code, not how it is licensed.
The packages under `open/` are standalone: they never import `emori_adapter` and never
depend on a package under `emori/`, so they build and run without the EMORI core
(`tests/test_ros_standalone.py` and `tests/test_ros_publishing.py` enforce this). The
packages under `emori/` use the `emori_adapter` core.

## Third-party code

| What | Where | Licence and copyright | Notice shipped |
|---|---|---|---|
| Face detector, tracker and look-at geometry, vendored and modified from Pollen Robotics' `reachy_mini` 1.10.0 (`vision/face_detector.py`, `vision/face_tracking.py`, `vision/look_at.py`, `media/camera_utils.py`) | `ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/gaze/{vision,geometry,tracking}.py` | Apache-2.0, Copyright 2025 Pollen Robotics (the upstream `LICENSE` at tag `v1.10.0`; upstream has no `NOTICE` file) | `ros2_ws/src/open/reachy_mini_ros2/THIRD_PARTY_NOTICES.md`; each file's docstring names its source and marks the changes `EMORI:` |
| The YuNet face-detection model `face_detection_yunet_2026may.onnx`, unmodified (OpenCV Zoo, via Pollen's Hugging Face repository) | `deploy/ros2/models/` | MIT, Copyright (c) 2020 Shiqi Yu | `deploy/ros2/models/LICENSE.face_detection_yunet.txt` |
| jsQR, the QR decoder the kiosk page uses | `src/emori_adapter/hostapp/static/jsqr.js` | Apache-2.0, Copyright Cosmo Wolfe | the root `LICENSE` and `NOTICE` |

The vendored Python is under the same licence as the package it sits in, so the package's
`LICENSE` covers it; what Apache-2.0 §4 adds is keeping Pollen's copyright and saying the
files were changed, which the notices file does. The model's MIT notice must travel with
any copy of the model: `deploy/ros2/Dockerfile.v2.robot` copies
`LICENSE.face_detection_yunet.txt` into the image next to the `.onnx` it bakes in.
