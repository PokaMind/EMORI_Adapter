# reachy_mini_ros2

The [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) driven as a **native
ROS 2 component**: no daemon, no HTTP hop, no socket between the decision and the servo.

Licence: Apache-2.0 (`LICENSE`). Copyright 2026 Pokamind AB, except the three files
under `reachy_mini_ros2/gaze/` vendored from Pollen Robotics' `reachy_mini` 1.10.0
(Apache-2.0, Copyright 2025 Pollen Robotics): see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Why a native node is possible

Pollen ships the robot as a FastAPI daemon, but the daemon is a shell. `Backend` and its
three subclasses (`MujocoBackend`, `MockupSimBackend`, `RobotBackend`), the kinematics
engines and Pollen's `motion/` package reference no HTTP whatsoever; their only outward
channel is `reachy_mini.io.publisher.Publisher`, which wraps any callable. So a ROS node
can hold the backend directly and hand those publishers to itself: in the simulator, with
no daemon process, the backend runs headless, publishes pose and joint messages at 50 Hz,
plays and cancels recorded moves, and renders the camera offscreen with `MUJOCO_GL=egl`
and no GPU.

## The modules

Paths are relative to the `reachy_mini_ros2/` module directory of this package.

| File | ROS? | What it is |
|---|---|---|
| `arbiter.py` | **no** | Who may move the robot, when, and what a refusal is called. Pure asyncio + numpy; tested on a host with neither ROS nor `reachy_mini` installed. |
| `conversions.py` | **no** | The only place Pollen's 4x4 matrices, `{x,y,z,roll,pitch,yaw}` and `geometry_msgs/Pose` meet. Pure numpy. |
| `backend_host.py` | no | Builds, runs and reads one Pollen backend; caches its 50 Hz state; caches recorded-move libraries. All `reachy_mini` imports are lazy. |
| `robot_node.py` | yes | The ROS surface: topics, TF, actions, services, diagnostics, the sim camera. |
| `rest_shim.py` | no | Pollen's daemon dialect on the same port, so an existing HTTP client never notices. Optional: the node logs a warning and carries on without it. |
| `video.py` | no | Turns a captured GStreamer buffer (with its row stride) into an image array. |
| `gaze/` | `component.py` only | Face look-at: `vision.py` (YuNet detector), `geometry.py` (look-at geometry and intrinsics), `tracking.py` (which face), `controller.py` (clamp, slew, hold, relax) and `component.py`, the thread that ties them to the node. |

## What it publishes and serves

| Interface | Type | Note |
|---|---|---|
| `/joint_states` | `sensor_msgs/JointState` | 9 joints at ~50 Hz, every backend message. |
| `/reachy_mini/head_pose` | `geometry_msgs/PoseStamped` | `base_link` frame. |
| `/tf` | `base_link` → `head` | Broadcast at the same rate. |
| `/reachy_mini/motion_status` | `reachy_mini_msgs/MotionStatus` | **Latched** (transient_local, depth 1), on change and every 1 s. |
| `/reachy_mini/camera/image_raw` + `camera_info` | `sensor_msgs/Image` (`rgb8`), `CameraInfo` | `camera.width` x `camera.height` at `camera.fps`. Source: the MuJoCo renderer (sim); on a robot, GStreamer `v4l2src` on `camera.device` (e.g. `/dev/video41`); or the separate `v4l2_camera` node with the launch argument `with_camera_v4l2:=true`, which turns the in-node capture off, and face look-at with it. `CameraInfo` is nominal (K from `camera.fovy_deg`, D = 0), not calibrated. |
| `/humans/faces/tracked` | `hri_msgs/IdsList` | **Latched** (transient_local, depth 1), once a second and on change: the locked face id, or `[]`. Only when the look-at component was built (`gaze.enabled`, a camera, `hri_msgs` and `onnxruntime` importable). |
| `/humans/faces/<id>/roi` | `hri_msgs/NormalizedRegionOfInterest2D` | Frame `eye_camera`, box clipped to `[0,1]`, `c` = detector confidence. One publisher at a time; the id is re-minted after 10 s without a lock. |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | Status name `reachy_mini`: backend, ready, motor mode, measured rates, every arbiter counter. |
| `/reachy_mini/goto` | `action GoTo` | |
| `/reachy_mini/play_recorded_move` | `action PlayRecordedMove` | |
| `/reachy_mini/set_target` | `msg BodyTarget` | Streaming target, unqueued. |
| `/reachy_mini/{wake_up,goto_sleep}` | `std_srvs/Trigger` | |
| `/reachy_mini/set_motor_mode` | `srv SetMotorMode` | |
| `/reachy_mini/stop` | `srv StopMove` | Empty id stops everything. |
| `/reachy_mini/set_head_reserved` | `std_srvs/SetBool` | |
| `/reachy_mini/set_owner` | `srv SetOwner` | Empty client releases. |

Joint order everywhere: `body_yaw`, `stewart_1` … `stewart_6`, `right_antenna`,
`left_antenna`. Units: metres and radians. Antennas: `[right, left]`.

The REST shim (`rest_shim.port`, default `:8000`) serves `GET /api/daemon/status`,
`POST /health-check`, `GET /api/motors/status`, `POST /api/motors/set_mode/{mode}`,
`POST /api/move/{goto,set_target,stop}`, `POST /api/move/play/{wake_up,goto_sleep}`,
`POST /api/move/play/recorded-move-dataset/{dataset}/{name}`, `GET /api/move/running`,
`GET /api/state/full`, the inert `POST /api/media/{play_sound,wobbling/enable,wobbling/disable}`
(200), and two EMORI routes: `POST /api/emori/gaze` (the look-at run switch) and
`POST /api/emori/head_reserved`. It listens on all interfaces, without authentication,
like the daemon it replaces.

## The arbiter, which is the point

One robot, two askers (the ROS graph, and the host application through the shim). In v1
the arbitration lived inside the host application's `RobotPlayer`, which worked while it
was the only writer. Now it lives next to the robot, and every rule has a name:

Checked in this order, on every request:

1. unknown lane → `bad_lane`;
2. the owner lock is held by another client and this is not the safety lane → `not_owner`;
3. expression lane and (`expression_owner == "none"` or this is not the expression owner) → `not_owner`;
4. expression lane and the head is reserved → `head_reserved`;
5. expression lane, a move is running and two are already queued → `dropped`;
6. the queue is full → `queue_full`.

Then: **safety** cancels the running move, flushes queued expression work and goes to the
front. **interaction** with `preempt` cancels a running *expression* move and nothing
else. One move runs at a time, on one asyncio loop, in one thread — Pollen's `play_move`
takes a lock and silently returns if a second move arrives, which would look exactly like
a move that succeeded instantly, so the arbiter never starts two. Cancellation is
`Task.cancel()`, which is precisely what the daemon's own stop does.

`set_target` is never queued: it is the caller's latest wish, not a goal. While a move
runs it is refused `busy` — the daemon answers `{"status":"ignored"}` in the same
situation.

`expression_owner` gates the **expression lane only**. Goals on `interaction` (the default
when `lane` is empty) or `safety`, `BodyTarget` messages on `/reachy_mini/set_target`
(client `set_target_client`, lane `interaction`) and REST requests without an
`X-Reachy-Lane` header are not subject to it. To keep everyone else out, take the owner
lock with `/reachy_mini/set_owner`; to keep other machines out, keep
`ROS_LOCALHOST_ONLY=1`.

## Parameters

| Name | Default | |
|---|---|---|
| `backend` | `mujoco` (env `REACHY_BACKEND`) | `mujoco` \| `mockup` \| `robot` |
| `scene` / `headless` | `empty` / `true` | MuJoCo only |
| `kinematics_engine` | `AnalyticalKinematics` | `Placo` needs the `placo` wheel (Linux) |
| `serialport` | `""` (env `REACHY_SERIALPORT`) | empty = find it; wireless is `/dev/ttyAMA3` |
| `wireless_version` | env `REACHY_WIRELESS` | |
| `hardware_config` | `""` | Pollen's `hardware_config.yaml` (PID gains) |
| `expression_owner` | env `EMORI_ROS_MOTION_OWNER`, else `hostapp` | `hostapp` \| `ros` \| `none`; anything else reads as `hostapp` |
| `set_target_client` | `ros` | who a `BodyTarget` on the topic is attributed to |
| `queue_size` | `12` | the host application's queue depth, to the number |
| `motors_enabled_on_start` / `wake_up_on_start` | `true` / `true` | |
| `preload_datasets` / `dataset` | `true` / the Pollen emotions library | |
| `sleep_on_shutdown` | `auto` (env `REACHY_SLEEP_ON_SHUTDOWN`) | `auto` parks and de-torques on SIGINT for the `robot` backend only (bounded at 8 s); `true` / `false` force it |
| `recorded_blend_s` | `0.3` (env `EMORI_RECORDED_BLEND_S`) | ease-in of a Pollen recorded move from the commanded pose; `0` snaps onto its first frame |
| `rest_shim.enabled` / `.host` / `.port` | `true` / `0.0.0.0` / `8000` | |
| `camera.enabled` / `.name` / `.fps` / `.width` / `.height` | `true` / `eye_camera` / `10` / `320` / `240` | the in-node capture, MuJoCo or GStreamer |
| `camera.device` | `""` | V4L2 device for the non-sim backends; empty = no camera |
| `camera.fovy_deg` | `60.0` | nominal vertical field of view for `CameraInfo` |
| `base_frame` / `head_frame` | `base_link` / `head` | |
| `diagnostics_period_s` | `5.0` | |

Face look-at (`gaze.*`). `gaze.enabled` is a capability switch (the detector is built or
not); whether it runs is `POST /api/emori/gaze`, off until the host application asks.

| Name | Default | |
|---|---|---|
| `gaze.enabled` | `true` unless env `EMORI_GAZE=0` | build the component at all |
| `gaze.model_path` | `/opt/emori/models/face_detection_yunet_2026may.onnx` (env `EMORI_GAZE_MODEL`) | the YuNet weights |
| `gaze.detect_hz` | `5.0` (launch argument from env `EMORI_GAZE_DETECT_HZ`) | detections per second |
| `gaze.score_threshold` / `gaze.select_score` | `0.6` / `0.75` | "is a face" / "willing to aim at it" |
| `gaze.nms_threshold` | `0.3` | |
| `gaze.min_area_frac` | `0.003` | smallest eligible face, as a fraction of the frame |
| `gaze.max_jump` | `0.5` | largest centre jump (normalised image units) still the same face |
| `gaze.max_misses` | `20` | detections without the face before the lock drops (4.0 s at 5 Hz) |
| `gaze.switch_margin` / `gaze.switch_ticks` | `1.35` / `5` | a challenger needs 1.35x the area for 5 detections |
| `gaze.id_ttl_s` | `10.0` | how long a face id survives a lost lock |
| `gaze.deadband_rad` / `gaze.max_step_rad` | `0.025` / `0.05` | deadband; per-detection step when the follower is off |
| `gaze.max_yaw_rad` / `gaze.max_pitch_rad` | `0.45` / `0.35` | clamps |
| `gaze.hold_s` / `gaze.relax_s` / `gaze.settle_after_move_s` | `1.5` / `4.0` / `0.0` | after a lost face: hold, then relax |
| `gaze.invert_yaw` | `false` | mechanism sign |
| `gaze.sensor_full_width_px` / `gaze.fx_scale` / `gaze.fy_scale` | `4608` / `1.0` / `1.0` | intrinsics and gain trims |
| `gaze.client` | `ros:gaze` | the arbiter client name |
| `gaze.lane` | `""` (env `EMORI_GAZE_LANE`) | empty = `expression` when the owner is `ros`, else `interaction` |
| `gaze.anchor_moves` / `gaze.anchor_ttl_s` | `true` (env `EMORI_GAZE_ANCHOR`) / `1.5` | compose other authors' head moves onto the face aim |
| `gaze.anchor_recorded` | `yaw` (env `EMORI_GAZE_ANCHOR_RECORDED`) | `yaw` \| `full` \| `off` for Pollen recorded moves |
| `gaze.servo_hz` / `gaze.max_speed_rad_s` / `gaze.ease` | `25.0` (env `EMORI_GAZE_SERVO_HZ`) / `1.0` / `0.3` | the follower; `servo_hz` 0 = one write per detection |

Parameters are read once, at start: `ros2 param set` changes nothing on a running node.

## Launch arguments

`launch/robot.launch.py`:

| Argument | Env | Default | Effect |
|---|---|---|---|
| `backend` | `REACHY_BACKEND` | `mujoco` | `mujoco` \| `mockup` \| `robot` |
| `headless` | — | `true` | MuJoCo only |
| `serialport` | `REACHY_SERIALPORT` | `""` | empty = find it |
| `wireless_version` | `REACHY_WIRELESS` | `false` | |
| `expression_owner` | `EMORI_ROS_MOTION_OWNER` | `hostapp` | |
| `rest_port` | `REACHY_REST_PORT` | `8000` | |
| `with_camera_v4l2` | — | `false` | start the separate `v4l2_camera` node and turn the in-node capture (and face look-at) off |
| `video_device` | `REACHY_VIDEO_DEVICE` | `/dev/video40` | the device either capture reads; the robot compose profile sets `/dev/video41` |
| `with_audio` | — | `false` | `audio_capturer_node`; not run on hardware |
| `sleep_on_shutdown` | `REACHY_SLEEP_ON_SHUTDOWN` | `auto` | |
| `gaze_enabled` / `gaze_model` / `gaze_detect_hz` | `EMORI_GAZE` / `EMORI_GAZE_MODEL` / `EMORI_GAZE_DETECT_HZ` | `true` / the baked model / `5.0` | |
| `gaze_select_score` / `gaze_invert_yaw` / `gaze_fx_scale` / `gaze_fy_scale` | — | `0.75` / `false` / `1.0` / `1.0` | |

`emori_adapter_bringup`'s `v2.launch.py` includes this file and forwards only `backend`,
`serialport`, `wireless_version`, `rest_port`, `with_camera_v4l2` and `expression_owner`;
through it the others take their environment defaults, and `headless`, `with_audio`,
`gaze_select_score`, `gaze_invert_yaw`, `gaze_fx_scale` and `gaze_fy_scale` cannot be set
at all. `with_camera_v4l2:=true` needs `ros-jazzy-v4l2-camera` and `with_audio:=true`
needs `ros-jazzy-audio-common`: neither is in `package.xml` or in the robot image.

## Dependencies that are not rosdep keys

`reachy-mini` (with its Rust `reachy_mini_motor_controller` and
`reachy_mini_rust_kinematics` wheels), `mujoco`, `numpy`, `scipy`, `fastapi` and
`uvicorn` come from **pip**, not rosdep — there are no keys for them on any distro, and
inventing local ones would make this package unbuildable for anybody but us:

```sh
pip install "reachy-mini[mujoco]==1.10.0"       # sim or dev
pip install "reachy-mini==1.10.0"               # robot: no mujoco needed
```

`reachy-mini` 1.10.0 requires Python ≥ 3.11, which is why this targets **Jazzy**
(Ubuntu 24.04, Python 3.12) and not Humble.

## Running it

```sh
ros2 launch reachy_mini_ros2 robot.launch.py                        # MuJoCo, headless
ros2 launch reachy_mini_ros2 robot.launch.py \
    backend:=robot wireless_version:=true serialport:=/dev/ttyAMA3 \
    video_device:=/dev/video41
```

On a kiosk robot `/dev/video40` belongs to the kiosk's Chromium (v4l2loopback serves one
capture consumer), so the node reads the camera bridge's second loopback, `/dev/video41`.

On the robot, **stop and disable Pollen's `reachy-mini-daemon` service first.** It holds
the serial port, and two processes driving nine servos over one 1 Mbaud bus is not a
situation that degrades gracefully. The REST shim then answers on `:8000` in the
daemon's place, so an HTTP client pointed at `http://127.0.0.1:8000` keeps working.

## Known limits, stated rather than discovered

* **Run on hardware, briefly, not soaked.** The serial backend has driven a real Reachy
  Mini Wireless (real bus and servos, `/joint_states` 48–49 Hz, a clean SIGINT park) and
  the camera has fed face look-at on one. Audio capture has not been run on hardware, and
  nothing has run for days on end. Most other figures in this
  README were measured in MuJoCo.
* **`gravity_compensation` is refused by the robot's v1.9.0 firmware** (empty 500, then
  a wedged `ready:false`). The arbiter latches the refusal after one failure, exactly as
  the host application does. It is inherited from Pollen's backend, not introduced here.
* **MuJoCo mirrors the antennas.** Internally it stores them `[left, right]` and negates
  on the way in and out, so the sim is self-consistent but left-right mirrored against
  the real robot. That is Pollen's quirk; "fixing" it here would make the simulator
  disagree with the daemon every other Reachy tool talks to.
* **No media.** Sound playback and the wobbler are accepted and ignored; the robot's
  audio belongs to PipeWire and its camera to the v4l2loopback bridge.
* **`ros2_control` is not used.** A Python node driving Pollen's own 50 Hz control loop
  is the honest equivalent for this robot; a C++ hardware interface would have to
  reimplement the Rust motor controller to gain anything.
