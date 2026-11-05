# Reachy Mini — hardware notes

Facts about the Pollen Robotics Reachy Mini that shaped this repository, checked against
the official Pollen Robotics / Hugging Face documentation and the robot itself, and the
daemon REST surface EMORI uses.

## Expressive outputs

Reachy Mini has no LED eyes or eye-screen; expressive outputs are head, antennas, body yaw
and sound.

| Output | Detail |
|---|---|
| **Head** | 6-DOF on a Stewart platform (6× Dynamixel XL330) — roll/pitch/yaw + x/y/z |
| **Body** | 1 rotation (body yaw) |
| **Antennas** | 2 actuated antennas (the primary "emotive" channel) |
| **Speaker** | 5 W — speech + backchannel sounds |
| **"Wobbling"** | daemon-provided audio-reactive head micro-motion |
| Camera / mics | wide-angle camera + 4-mic array (perception, not expression) |

EMORI's engagement behaviours are therefore designed around **head gaze/nod/tilt +
body yaw + antenna gestures + audio backchannel** (`src/emori_adapter/behavior/`).

## The compute module

The Wireless model runs on a Raspberry Pi CM4. Some third-party pages say "Raspberry Pi 5";
treat that as unverified. This matters for on-robot performance budgeting and for the
deployment image (`pollen-robotics/reachy-mini-os`, a pi-gen fork).

## The camera module, and a correction to Pollen's own calibration table

`/sys/class/video4linux/v4l-subdev0/name` reports **`imx708_wide`**, and `dmesg` shows
`imx708@1a` on both CSI ports. That is a Raspberry Pi **Camera Module 3 Wide**: published
~102° H / ~67° V, and an active array of **4608 × 2592**. Its libcamera modes are
1536×864, 2304×1296 and 4608×2592 — all full-field.

The SDK's calibration (`reachy_mini/media/camera_constants.py`,
`ReachyMiniLiteCamSpecs.K`) was taken at **3840 × 2592** with `fx = 2001.808`,
`fy = 2003.078` — square pixels. **A 4608-column sensor cannot produce 3840 square-pixel
columns except by cropping**, so the calibrated field spans 3840 of 4608 columns, a factor
of **1.2**, and the full 2592 rows. Pollen's mode table nevertheless labels every mode
`crop 1.0`, and `scale_intrinsics` scales `fx` by `target_w / 3840` on that basis.

Anything computing intrinsics for a frame that covers the **full** field — which the camera
bridge's output does — must therefore scale `fx` by `target_w / 4608`, not `target_w / 3840`,
and shift `cx` by the crop's left edge, `(4608 − 3840)/2 = 384` native columns. Getting this
wrong makes `fx` 1.2× too large and any aim loop ~14 % slow: stable, convergent, and
invisible. `ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/gaze/geometry.py::intrinsics_for_frame`
is where EMORI does it;
[`ros2-v2-native-robot.md`](ros2-v2-native-robot.md) § "Face look-at" carries the arithmetic
and the non-circular sanity check (corrected, 1280×720 gives `fx = fy`; naive does not).

The camera is **not** reachable through the daemon on the robot — see the `--no-media` note
at the bottom of this file. On a v2 robot the ROS robot node reads it through a second
v4l2loopback node (`/dev/video41`) that the EMORI camera bridge tees, because
`/dev/video40` is loaded `exclusive_caps=Y` and belongs to the kiosk's Chromium.

## Software facts that shaped this repo

- **Simulator = MuJoCo**, bundled in the `reachy-mini` pip package (`reachy-mini[mujoco]`,
  pins `mujoco==3.3.0`). **No Hugging Face download needed** for the sim — the HF token is
  only for installing community *apps* / publishing.
- **Python 3.10–3.13** (3.12 recommended for the Windows sim venv; the CM4 robot OS,
  Debian Trixie, ships **3.13** and the adapter installs there — `pyproject.toml` is
  `>=3.10,<3.14`). **3.14 cannot run the sim** (no `mujoco==3.3.0` wheel).
- **Control = daemon REST/WebSocket on `localhost:8000`** (FastAPI), not gRPC and not DDS.
  Motors are Dynamixel-family serial via a Rust controller (`rustypot`).
- **No official ROS 2 stack** for Reachy Mini (that's Reachy *2*). EMORI bridges the
  SDK/daemon to ROS 2 itself.
- Variants: **Lite** (tethered) vs **Wireless** (onboard CM4 + WiFi + battery + IMU). The
  simulator mimics a Lite (so **no IMU in sim**).

## Verified REST surface (used by `HttpRobotClient`)

`POST /api/move/goto` (`{head_pose:{x,y,z(m),roll,pitch,yaw(rad)}, antennas:[r,l], body_yaw, duration, interpolation}`
— the daemon's `SetAntennasCmd` is `[right, left]`),
`POST /api/move/set_target`, `POST /api/move/play/wake_up`, `POST /api/media/play_sound` (`{file}`),
`POST /api/media/wobbling/{enable,disable}`,
`POST /api/motors/set_mode/{mode}` (`enabled`|`disabled`|`gravity_compensation`; EMORI sets
`gravity_compensation` so the back-drivable head can be nudged for screenless control),
`GET /api/daemon/status` (readiness — returns the full `DaemonStatus` model; assert `state=="running"`),
`GET /api/state/full`. Note `/health-check` is **not** a readiness endpoint (it's a POST-only
watchdog-reset route) — use `/api/daemon/status` for readiness.

> **Media routes are off-limits on the robot.** On the robot EMORI runs the daemon
> `--no-media` (MOTION only) so Chromium + the camera bridge own the camera and the
> kiosk + Piper own the mic/speaker. Consequently `/api/daemon/status.face_target` is
> never populated and there is no direction-of-arrival field — **the daemon is not a
> presence sensor**, and no walk-away logic may read it. Never POST a media-acquisition
> route (`/api/media/acquire`) or a face-tracking enable route: reacquiring media kills
> the camera bridge and, because `release_media()` is all-or-nothing, the mic and
> speaker with it. `play_sound`/`wobbling` above are likewise inert under `--no-media`.
> The presence signal arrives in the session's body analysis — see
> [`onrobot-runbook.md`](onrobot-runbook.md) § "Presence / walk-away".
