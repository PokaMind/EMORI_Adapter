"""``reachy_mini`` - the Reachy Mini as a ROS 2 node, with no daemon in the path.

Pollen ships the robot as a FastAPI daemon on ``127.0.0.1:8000``.  Everything in front
of that socket - the 50 Hz control loop, the kinematics, the recorded moves - is a plain
Python object with no transport of its own, so this node owns it directly:
``backend_host.py`` builds and runs it, ``arbiter.py`` decides what may move it, and
this file is the ROS surface on top.  The gain is not tidiness.  It is that
``/joint_states`` and ``/tf`` become real (a consumer that has never heard of EMORI can
read the robot), that motion arbitration becomes observable
(``/reachy_mini/motion_status``), and that a goal has a result instead of a POST having
a status code.

The host application does not have to know.  ``rest_shim.py`` serves
the daemon's own dialect on the same port, so ``REACHY_BASE_URL=http://127.0.0.1:8000``
keeps working unchanged - it is one more arbiter client, named ``hostapp``.  That import
is lazy and its absence is a warning rather than a crash, because the node is perfectly
useful without it and a missing compatibility layer must not take the robot down.

Threads, because there are four and getting them wrong is the whole risk:

1. the **backend** thread - Pollen's ``wrapped_run``, the 50 Hz control loop;
2. the **arbiter** thread - one asyncio loop, where every move coroutine runs and where
   ``Task.cancel()`` happens (exactly the daemon's ``stop``);
3. the **camera** thread - ``mujoco.Renderer`` must be created and used on one thread,
   and it is never the backend's;
4. the **executor** threads - rclpy, multi-threaded and reentrant so a blocking action
   callback cannot stall the ``set_target`` subscription.

``rclpy`` is imported at column 0 rather than behind a guard: there is no pure half left
in this file to protect.  ``arbiter.py`` and ``conversions.py`` are the pure halves and
they import none of this, which is what lets the arbitration rules be tested on a
machine with neither ROS nor ``reachy_mini`` installed.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from typing import Any

import numpy as np
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from geometry_msgs.msg import PoseStamped, TransformStamped
from sensor_msgs.msg import CameraInfo, Image, JointState
from std_srvs.srv import SetBool, Trigger
from tf2_ros import TransformBroadcaster

from reachy_mini_msgs.action import GoTo, PlayRecordedMove
from reachy_mini_msgs.msg import BodyTarget, MotionStatus
from reachy_mini_msgs.srv import SetMotorMode, SetOwner, StopMove

from reachy_mini_ros2.arbiter import (
    DEFAULT_RECORDED_BLEND_S,
    MotionArbiter,
    MotionRequest,
    head_reach_check,
    normalize_expression_owner,
    owner_of,
    stewart_limits,
)
from reachy_mini_ros2.backend_host import (
    DEFAULT_EMOTIONS_DATASET,
    JOINT_NAMES,
    BackendConfig,
    BackendHost,
    RecordedMovesCache,
    _hardware_config_path,
)
from reachy_mini_ros2.conversions import matrix_to_pose, pose_to_matrix
from reachy_mini_ros2.gaze.controller import GazeParams
from reachy_mini_ros2.video import rgb_frame_from_buffer

# How often an in-flight goal publishes feedback and re-checks its cancel flag. Short
# enough that "stop" feels immediate, long enough not to spin a core doing nothing.
FEEDBACK_PERIOD_S = 0.1

# Grace on top of a goto's own duration before the action gives up waiting. The backend
# interpolates at 100 Hz and can be a tick late; it is never seconds late.
GOAL_GRACE_S = 5.0

# A recorded move does not declare its duration to the caller, so the action needs a
# ceiling. The Pollen emotions library tops out at a few seconds per move; two minutes
# is "something is wrong", not "this one is long".
RECORDED_MOVE_TIMEOUT_S = 120.0

# The Trigger services (wake_up / goto_sleep) answer within this. A wake that is still
# running afterwards is reported as success with a message saying so, rather than as a
# failure: it was accepted, it is happening, and the caller asked a yes/no question.
SERVICE_WAIT_S = 5.0

# Startup wake is allowed longer than the service: it runs from sleep, which is the
# longest wake there is, and nothing is waiting on the answer.
STARTUP_WAKE_WAIT_S = 20.0

# MotionStatus is latched AND repeated at this period, so "who owns motion" is answerable
# by a tool that attached a moment ago and by one whose latched sample arrived stale.
STATUS_PERIOD_S = 1.0

# How many consecutive empty pulls end the capture thread. At camera.fps this is about
# a minute of a device that opened and then produced nothing -- long enough to ride out a
# producer restarting, short enough that a node does not poll a dead pipeline all day.
_CAMERA_MISS_LIMIT = 600

# Below this, /joint_states is not keeping up with the 50 Hz control loop and the
# diagnostics say so. The rehearsal asserts >= 40 Hz for the same reason.
JOINT_RATE_WARN_HZ = 40.0

# How long `backend.reset_to_sleep()` is given at shutdown. Pollen's own sequence is a
# lift (<=1.5 s), a 0.2 s settle, then `goto_sleep` - which itself is up to a 1 s move, a
# 0.2 s pause, a 2 s move and a 2 s wait, and up to 5 s when it is already parked. Eight
# seconds covers the worst of those with a margin; the systemd and compose stop timeouts
# are set above it so the supervisor never SIGKILLs a robot mid-park.
SLEEP_ON_SHUTDOWN_TIMEOUT_S = 8.0

# The exit code a node uses when its control loop died under it. Non-zero on purpose:
# `Restart=always` / `restart: unless-stopped` is the supervisor, and a process that
# exits 0 after losing the robot has told it there was nothing to restart.
BACKEND_DEAD_EXIT_CODE = 70


def _env_float(name: str, default: float) -> float:
    """``float(os.environ[name])``, or ``default`` when it is unset, empty or not a number.

    For knobs an operator types into deploy/ros2/.env: a typo there must cost that one
    knob, not raise inside ``__init__`` and take the robot node - and the robot - down.
    """

    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        return float(default)
    return value if math.isfinite(value) else float(default)


class ReachyMiniNode(Node):
    """One robot, owned in this process, exposed as topics, actions and services."""

    def __init__(self) -> None:
        super().__init__("reachy_mini")

        self._declare_parameters()
        config = BackendConfig(
            backend=self._param_str("backend", "mujoco").lower(),
            scene=self._param_str("scene", "empty"),
            headless=bool(self.get_parameter("headless").value),
            kinematics_engine=self._param_str("kinematics_engine", "AnalyticalKinematics"),
            serialport=self._param_str("serialport", ""),
            wireless_version=bool(self.get_parameter("wireless_version").value),
            hardware_config=self._param_str("hardware_config", ""),
        )
        self.backend_name = config.backend
        self._base_frame = self._param_str("base_frame", "base_link")
        self._head_frame = self._param_str("head_frame", "head")
        self._expression_owner = normalize_expression_owner(
            self._param_str("expression_owner", ""))
        self._set_target_client = self._param_str("set_target_client", "ros")
        self._camera_name = self._param_str("camera.name", "eye_camera")
        self._camera_fps = max(1.0, float(self.get_parameter("camera.fps").value))
        self._camera_size = (int(self.get_parameter("camera.width").value),
                             int(self.get_parameter("camera.height").value))
        self._camera_device = self._param_str("camera.device", "")
        self._dataset = self._param_str("dataset", DEFAULT_EMOTIONS_DATASET)

        # --- counters the diagnostics turn into rates --------------------------
        self._joint_messages = 0
        self._camera_frames = 0
        self._last_rate_sample = time.monotonic()
        self._last_joint_messages = 0
        self._last_camera_frames = 0
        self._joint_hz = 0.0
        self._camera_hz = 0.0
        # Consecutive diagnostics periods with gaze running and no frames at all. Two
        # in a row is a WARN; one is a bridge that was restarted between samples.
        self._camera_starved_periods = 0

        self._shim: Any = None
        self._link: Any = None
        self._camera_stop = threading.Event()
        self._camera_thread: threading.Thread | None = None
        self._gaze: Any = None
        # Whether a camera is going to run at all, computed HERE because the shim (built
        # at step 5) has to know whether gaze exists, and the camera only starts at step
        # 7. A node with no camera never builds the component and the shim's
        # `/api/emori/gaze` answers 503.
        self._camera_wanted = bool(self.get_parameter("camera.enabled").value) and (
            self.backend_name == "mujoco" or bool(self._camera_device))

        # The backend-death latch. `_backend_was_ready` only ever goes True, so a node
        # that is still starting is never mistaken for one whose control loop died, and
        # `_backend_dead` makes the ERROR-and-exit happen exactly once however many
        # diagnostics ticks notice it.
        self._backend_was_ready = False
        self._backend_dead = False
        self.exit_code = 0

        # False from the first moment of teardown. The 50 Hz callbacks below run on
        # POLLEN'S control-loop thread, and `rclpy` shuts its context down from a signal
        # handler - so between SIGTERM and `shutdown()` every publish raises
        # `RCLError: publisher's context is invalid`. See `_publish` for why that is not
        # cosmetic.
        self._publishing = True
        self._publish_failures = 0

        # The 50 Hz publishers, declared BEFORE the backend starts. Step 1 below spawns
        # Pollen's control-loop thread, which calls _on_joint_positions/_on_head_pose
        # immediately -- a good hundred milliseconds before step 3 builds the ROS
        # surface. Without these three names the first callback raises AttributeError
        # inside `wrapped_run`, which re-raises, and the control-loop thread DIES: no
        # /joint_states, no /tf and a robot that never moves, with the traceback the
        # only evidence. Same idiom as _publish_status's `getattr(self, "_status_pub")`.
        self._joint_pub: Any = None
        self._pose_pub: Any = None
        self._tf: Any = None

        # --- 1. the robot ------------------------------------------------------
        self.backend_host = BackendHost(config, log=self.get_logger())
        self.backend_host.start(on_joint_positions=self._on_joint_positions,
                                on_head_pose=self._on_head_pose)

        # --- 2. the arbiter, on its own asyncio loop in its own thread ---------
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(target=self._run_loop,
                                             name="reachy_mini_arbiter", daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(5.0)

        self._moves = RecordedMovesCache(self._dataset, log=self.get_logger())
        self.arbiter = MotionArbiter(
            self.backend_host.backend, loop=self._loop,
            queue_size=int(self.get_parameter("queue_size").value),
            expression_owner=self._expression_owner,
            recorded_moves_factory=self._moves,
            # Read through to `backend.get_motor_control_mode()` on every ask. A cache
            # of the mode last SET is wrong the moment anything else changes it - and something always does: Pollen's
            # `goto_sleep` disables the motors at the end of every sleep.
            motor_mode_reader=self.backend_host.motor_mode,
            # Pollen's `play_move` snaps onto a recorded move's first frame (no initial
            # goto by default); the arbiter eases that jump out over this long, starting
            # from what the backend is COMMANDING at the moment the move starts.
            recorded_blend_s=float(self.get_parameter("recorded_blend_s").value),
            recorded_skip_lead_in=os.environ.get("EMORI_RECORDED_SKIP_LEADIN", "1").strip() != "0",
            start_state_reader=self._recorded_start_state,
            head_reachable=self._recorded_head_check(config),
            log=self.get_logger())
        self.arbiter.backend_name = self.backend_name

        # --- 3. the ROS surface ------------------------------------------------
        self._build_interfaces()

        # --- 4. the emotions dataset, BEFORE anything can be asked for one -----
        # Ordered ahead of the shim deliberately. `RecordedMoves` re-reads and re-parses
        # every JSON in the dataset, and on a cold HuggingFace cache it downloads; a shim
        # already answering `state: running` during that window invites the host
        # application to POST a recorded move that then blocks behind the load. Nothing
        # answers until the node can perform what it advertises.
        if bool(self.get_parameter("preload_datasets").value):
            self._moves.preload()

        # --- 4b. the face look-at component, built before the shim -------------
        # Built here and STARTED with the camera (step 7): its publishers and its run
        # flag have to exist before `_start_rest_shim` wires the two `/api/emori` routes,
        # but its thread has nothing to read until frames are flowing.
        self._build_gaze()

        # --- 5. the compatibility shim, optional by construction ---------------
        if bool(self.get_parameter("rest_shim.enabled").value):
            self._start_rest_shim()

        # --- 5b. the ROS twin of the shim's routes, on a node of its own --------
        if bool(self.get_parameter("link.enabled").value):
            self._start_link_surface()

        # --- 6. motors, wake ----------------------------------------------------
        if bool(self.get_parameter("motors_enabled_on_start").value):
            outcome = self.arbiter.set_motor_mode("enabled")
            if not outcome.accepted:
                self.get_logger().error(
                    "could not enable the motors at startup; the robot will not move "
                    "until POST /api/motors/set_mode/enabled or /reachy_mini/set_motor_mode "
                    "succeeds")
        if bool(self.get_parameter("wake_up_on_start").value):
            self._wake_up_on_start()

        if bool(self.get_parameter("camera.enabled").value):
            if self.backend_name == "mujoco":
                self._start_camera()
            elif self._camera_device:
                self._start_v4l2_camera()
            else:
                self.get_logger().info(
                    f"camera.enabled is true, the backend is {self.backend_name!r} and "
                    "camera.device is empty: nothing to capture from. Set camera.device "
                    "(the robot uses /dev/video40) or launch with_camera_v4l2:=true.")
        if self._gaze is not None and self._camera_thread is not None:
            self._gaze.start()

        shim_url = getattr(self._shim, "url", None) or "disabled"
        self.get_logger().info(
            f"reachy_mini up: backend={self.backend_name} motors={self.arbiter.status()['motor_mode'] or 'unknown'} "
            f"shim={shim_url} expression_owner={self._expression_owner} "
            f"camera={'on' if self._camera_thread else 'off'} "
            f"gaze={'built' if self._gaze is not None else 'off'}")

    # --- the face look-at component -----------------------------------------
    def _build_gaze(self) -> None:
        """Build the gaze component, or leave it None and say why.

        Imported lazily and tolerantly, exactly like the REST shim: the component needs
        ``hri_msgs`` and ``onnxruntime``, and a robot node that refuses to start because
        it cannot look at faces has the priorities backwards.
        """

        params = self._gaze_params()
        if not params.enabled:
            self.get_logger().info(
                "gaze.enabled is false: no face detector is built. This is a capability "
                "switch; POST /api/emori/gaze will answer 503.")
            return
        if not self._camera_wanted:
            self.get_logger().info(
                "gaze.enabled is true but no camera will run, so there is nothing to "
                "detect faces in. Set camera.device (the robot tees /dev/video41).")
            return
        try:
            from reachy_mini_ros2.gaze.component import GazeComponent
        except Exception as error:  # noqa: BLE001 - a degraded node, never a dead one
            self.get_logger().warn(
                f"gaze is unavailable ({error}); the robot will not look at faces. "
                "Everything else is unaffected. hri_msgs and onnxruntime are what this "
                "needs.")
            return
        self._gaze = GazeComponent(self, self.arbiter, self.backend_host, params,
                                   camera_frame=self._camera_name)
        if params.anchor_moves:
            self.arbiter.set_pose_anchor(self._gaze.pose_anchor, exempt_client=params.client,
                                         recorded=params.anchor_recorded)
        self.get_logger().info(
            f"gaze: client={params.client} lane={params.lane} "
            f"anchor_moves={'on' if params.anchor_moves else 'off'} "
            f"(ttl {params.anchor_ttl_s:.1f}s) anchor_recorded={params.anchor_recorded} "
            f"recorded_blend={float(self.get_parameter('recorded_blend_s').value):g}s "
            f"follower={params.servo_hz:g}Hz <= {params.max_speed_rad_s:g} rad/s")

    def _recorded_head_check(self, config: BackendConfig) -> Any:
        """What a recorded move's head has to pass on its way out, or None to send it as authored."""

        # Another engine's IK can hold state or take far longer than a frame allows.
        if (config.kinematics_engine != "AnalyticalKinematics"
                or os.environ.get("EMORI_RECORDED_FIT", "1").strip() == "0"):
            return None
        try:
            from reachy_mini.utils.hardware_config.parser import parse_yaml_config  # noqa: PLC0415

            # "none" opts out of writing gains. The servos keep their limits all the same.
            path = _hardware_config_path(config.hardware_config) or _hardware_config_path("")
            motors = parse_yaml_config(path).motors
            return head_reach_check(self._head_joints, stewart_limits(motors))
        except Exception as error:  # noqa: BLE001 - no limits to fit to: played as authored
            self.get_logger().warning(f"recorded moves go out as authored: no motor limits ({error!r})")
            return None

    def _head_joints(self, head: Any, body_yaw: Any) -> Any:
        """Pollen's IK for this target, asked as its own backend asks before moving.

        The analytical IK keeps nothing between calls and takes about 70 us on the robot. A frame
        in reach asks once, and one that has to be fitted up to 14 times, about 2 ms of its 10 ms.
        """

        backend = self.backend_host.backend
        if body_yaw is None:
            body_yaw = getattr(backend, "target_body_yaw", None) or 0.0
        return backend.head_kinematics.ik(head, body_yaw=float(body_yaw))

    def _recorded_start_state(self) -> tuple[Any, Any, Any]:
        """Where a recorded move eases in FROM: ``(head 4x4, antennas, body_yaw)``.

        COMMANDED, not measured, on every channel that can say: the ease-in exists to keep
        the servo command continuous, and on a robot the measured head yaw can sit 0.12
        rad off the commanded one (-0.117 vs -0.0001) - easing in from the measurement
        would make the command itself jump by that much. Pollen keeps the three
        commands as `_last_target_head_pose` (via ``commanded_head_pose``),
        ``target_antenna_joint_positions`` and ``target_body_yaw``; each falls back to the
        measured value when the backend has not been commanded yet. Called on the arbiter
        loop, once per recorded move; nothing here touches the serial bus.
        """

        host = self.backend_host
        backend = host.backend
        head = host.commanded_head_pose()
        if head is None:
            head = host.present_head_pose()
        antennas = getattr(backend, "target_antenna_joint_positions", None)
        if antennas is None:
            antennas = host.present_antennas()
        body_yaw = getattr(backend, "target_body_yaw", None)
        if body_yaw is None:
            body_yaw = host.present_body_yaw()
        return head, np.array(antennas, dtype=float), float(body_yaw)

    def _set_gaze_enabled(self, enabled: bool) -> None:
        if self._gaze is not None:
            self._gaze.set_enabled(bool(enabled))

    def _gaze_is_active(self) -> bool:
        return bool(self._gaze is not None and self._gaze.is_active())

    def _offer_frame_to_gaze(self, frame: np.ndarray) -> None:
        """Hand the frame over WITH the head pose sampled at this instant.

        `present_head_pose` is a lock-and-copy of a cached matrix that the backend's
        50 Hz publisher fills, so this never touches the serial bus - and sampling it
        here rather than on the gaze thread is what stops a 300 ms old pixel being
        composed with a fresh pose and re-adding the motion already executed.
        """

        if self._gaze is None:
            return
        self._gaze.offer(frame, self.backend_host.present_head_pose(), time.monotonic())

    # --- parameters ---------------------------------------------------------
    def _declare_parameters(self) -> None:
        self.declare_parameter("backend", os.environ.get("REACHY_BACKEND", "mujoco"))
        self.declare_parameter("scene", "empty")
        self.declare_parameter("headless", True)
        self.declare_parameter("kinematics_engine", "AnalyticalKinematics")
        self.declare_parameter("serialport", os.environ.get("REACHY_SERIALPORT", ""))
        self.declare_parameter("wireless_version",
                               os.environ.get("REACHY_WIRELESS", "") not in ("", "0", "false"))
        self.declare_parameter("hardware_config", "")
        # The same knob, with the same three values and the same fail-closed fallback,
        # that gates the EMORI skill facade. Read here so the ROS graph and the host
        # application cannot disagree about who is allowed to be expressive.
        self.declare_parameter("expression_owner",
                               os.environ.get("EMORI_ROS_MOTION_OWNER", "hostapp"))
        self.declare_parameter("set_target_client", "ros")
        self.declare_parameter("queue_size", 12)
        self.declare_parameter("motors_enabled_on_start", True)
        self.declare_parameter("wake_up_on_start", True)
        # Park and de-torque on the way out - on the REAL robot by default, and nowhere
        # else. A simulator has nothing to drop; a robot whose process exits with the
        # motors still holding a pose is a robot that snaps down whenever the torque
        # eventually goes, and one whose head is left up under torque is a robot heating
        # nine servos with nobody watching. Pollen's daemon does the same thing on
        # shutdown (daemon/daemon.py:557-573) and for the same reason.
        #
        # Three values, not two. `auto` is the default and resolves against the
        # ALREADY-RESOLVED `backend` parameter - not against REACHY_BACKEND - so a
        # launch file or params file that sets `backend` decides this too, and the one
        # robot that overrides the env is not the one that silently never parks. `true` /
        # `false` are the explicit overrides, reachable from the launch argument, the
        # unit, the compose environment and `--ros-args`, because a bench session that
        # restarts the node twenty times does not want twenty sleep trajectories.
        self.declare_parameter("sleep_on_shutdown",
                               os.environ.get("REACHY_SLEEP_ON_SHUTDOWN", "auto"))
        self.declare_parameter("preload_datasets", True)
        self.declare_parameter("dataset", DEFAULT_EMOTIONS_DATASET)
        # The recorded-move ease-in, seconds (arbiter.DEFAULT_RECORDED_BLEND_S says why
        # 0.3). 0 = snap onto the first frame, as every recorded move did before.
        self.declare_parameter("recorded_blend_s", _env_float(
            "EMORI_RECORDED_BLEND_S", DEFAULT_RECORDED_BLEND_S))
        self.declare_parameter("rest_shim.enabled", True)
        self.declare_parameter("rest_shim.host", "0.0.0.0")
        self.declare_parameter("rest_shim.port", 8000)
        self.declare_parameter("link.enabled", True)
        self.declare_parameter("camera.enabled", True)
        self.declare_parameter("camera.name", "eye_camera")
        self.declare_parameter("camera.fps", 10.0)
        self.declare_parameter("camera.width", 320)
        self.declare_parameter("camera.height", 240)
        # A real capture device for the non-sim backends. Empty means "no camera",
        # never "guess": a node that opens a video device nobody asked for is a node
        # that took the camera away from whatever else was reading it.
        self.declare_parameter("camera.device", "")
        # Nominal, not calibrated. CameraInfo has to say something, and an invented
        # K is honest only if it is labelled: this is the value to override once
        # somebody runs a calibration on the robot.
        self.declare_parameter("camera.fovy_deg", 60.0)
        self.declare_parameter("base_frame", "base_link")
        self.declare_parameter("head_frame", "head")
        self.declare_parameter("diagnostics_period_s", 5.0)
        self._declare_gaze_parameters()

    def _declare_gaze_parameters(self) -> None:
        """The face look-at component's knobs.

        ``gaze.enabled`` is a CAPABILITY switch - false means the detector is never
        built at all. Whether gaze is RUNNING is a separate, fail-closed question
        answered by ``POST /api/emori/gaze``, because an idle kiosk in a public corridor
        must not be running a face detector on nobody's behalf.
        """

        defaults = GazeParams()
        self.declare_parameter("gaze.enabled", os.environ.get("EMORI_GAZE", "1") != "0")
        self.declare_parameter("gaze.model_path",
                               os.environ.get("EMORI_GAZE_MODEL", defaults.model_path))
        self.declare_parameter("gaze.detect_hz", defaults.detect_hz)
        self.declare_parameter("gaze.score_threshold", defaults.score_threshold)
        self.declare_parameter("gaze.select_score", defaults.select_score)
        self.declare_parameter("gaze.nms_threshold", defaults.nms_threshold)
        self.declare_parameter("gaze.min_area_frac", defaults.min_area_frac)
        self.declare_parameter("gaze.max_jump", defaults.max_jump)
        self.declare_parameter("gaze.max_misses", defaults.max_misses)
        self.declare_parameter("gaze.switch_margin", defaults.switch_margin)
        self.declare_parameter("gaze.switch_ticks", defaults.switch_ticks)
        self.declare_parameter("gaze.id_ttl_s", defaults.id_ttl_s)
        self.declare_parameter("gaze.deadband_rad", defaults.deadband_rad)
        self.declare_parameter("gaze.max_step_rad", defaults.max_step_rad)
        self.declare_parameter("gaze.max_yaw_rad", defaults.max_yaw_rad)
        self.declare_parameter("gaze.max_pitch_rad", defaults.max_pitch_rad)
        self.declare_parameter("gaze.hold_s", defaults.hold_s)
        self.declare_parameter("gaze.relax_s", defaults.relax_s)
        self.declare_parameter("gaze.settle_after_move_s", defaults.settle_after_move_s)
        self.declare_parameter("gaze.invert_yaw", defaults.invert_yaw)
        self.declare_parameter("gaze.sensor_full_width_px", defaults.sensor_full_width_px)
        self.declare_parameter("gaze.fx_scale", defaults.fx_scale)
        self.declare_parameter("gaze.fy_scale", defaults.fy_scale)
        self.declare_parameter("gaze.client", defaults.client)
        # Empty = derived from the expression owner (`_gaze_lane`); a value is obeyed.
        self.declare_parameter("gaze.lane", os.environ.get("EMORI_GAZE_LANE", ""))
        self.declare_parameter("gaze.anchor_moves",
                               os.environ.get("EMORI_GAZE_ANCHOR", "1") != "0")
        self.declare_parameter("gaze.anchor_ttl_s", defaults.anchor_ttl_s)
        # yaw | full | off: how much of the face aim a Pollen RECORDED move gets
        # (arbiter.RECORDED_ANCHORS says why yaw). Empty means the default; anything
        # else unrecognised plays recorded moves as authored, and the arbiter says so.
        self.declare_parameter("gaze.anchor_recorded", os.environ.get(
            "EMORI_GAZE_ANCHOR_RECORDED", "") or defaults.anchor_recorded)
        # Parsed like EMORI_RECORDED_BLEND_S: now that compose passes it through, a typo
        # in deploy/ros2/.env must cost the knob, not the node.
        self.declare_parameter("gaze.servo_hz", _env_float(
            "EMORI_GAZE_SERVO_HZ", defaults.servo_hz))
        self.declare_parameter("gaze.max_speed_rad_s", defaults.max_speed_rad_s)
        self.declare_parameter("gaze.ease", defaults.ease)

    def _gaze_params(self) -> GazeParams:
        """Every ``gaze.*`` parameter, in one object the pure modules already take."""

        return GazeParams(
            enabled=bool(self.get_parameter("gaze.enabled").value),
            model_path=self._param_str("gaze.model_path", GazeParams().model_path),
            detect_hz=float(self.get_parameter("gaze.detect_hz").value),
            score_threshold=float(self.get_parameter("gaze.score_threshold").value),
            select_score=float(self.get_parameter("gaze.select_score").value),
            nms_threshold=float(self.get_parameter("gaze.nms_threshold").value),
            min_area_frac=float(self.get_parameter("gaze.min_area_frac").value),
            max_jump=float(self.get_parameter("gaze.max_jump").value),
            max_misses=int(self.get_parameter("gaze.max_misses").value),
            switch_margin=float(self.get_parameter("gaze.switch_margin").value),
            switch_ticks=int(self.get_parameter("gaze.switch_ticks").value),
            id_ttl_s=float(self.get_parameter("gaze.id_ttl_s").value),
            deadband_rad=float(self.get_parameter("gaze.deadband_rad").value),
            max_step_rad=float(self.get_parameter("gaze.max_step_rad").value),
            max_yaw_rad=float(self.get_parameter("gaze.max_yaw_rad").value),
            max_pitch_rad=float(self.get_parameter("gaze.max_pitch_rad").value),
            hold_s=float(self.get_parameter("gaze.hold_s").value),
            relax_s=float(self.get_parameter("gaze.relax_s").value),
            settle_after_move_s=float(
                self.get_parameter("gaze.settle_after_move_s").value),
            invert_yaw=bool(self.get_parameter("gaze.invert_yaw").value),
            sensor_full_width_px=int(
                self.get_parameter("gaze.sensor_full_width_px").value),
            fx_scale=float(self.get_parameter("gaze.fx_scale").value),
            fy_scale=float(self.get_parameter("gaze.fy_scale").value),
            client=self._param_str("gaze.client", GazeParams().client),
            lane=self._gaze_lane(self._param_str("gaze.client", GazeParams().client)),
            anchor_moves=bool(self.get_parameter("gaze.anchor_moves").value),
            anchor_ttl_s=float(self.get_parameter("gaze.anchor_ttl_s").value),
            anchor_recorded=self._param_str("gaze.anchor_recorded",
                                            GazeParams().anchor_recorded),
            servo_hz=float(self.get_parameter("gaze.servo_hz").value),
            max_speed_rad_s=float(self.get_parameter("gaze.max_speed_rad_s").value),
            ease=float(self.get_parameter("gaze.ease").value),
        )

    def _gaze_lane(self, client: str) -> str:
        """``gaze.lane`` if set, else the lane gaze is actually allowed to write on.

        Gaze is a sub-client of the ROS graph (``ros:gaze``). With the graph owning the
        expression lane that is where it belongs, under every cue. With the HOST
        APPLICATION owning it (EMORI_ROS_MOTION_OWNER=hostapp, where the voice-locked
        talking flow lives) every gaze target on that lane is refused ``not_owner`` and
        the robot never looks at anybody - so it writes on ``interaction`` instead, which
        has no owner rule and still yields ``busy`` to every running move.
        """

        explicit = self._param_str("gaze.lane", "")
        if explicit:
            return explicit
        if self._expression_owner == owner_of(client):
            return GazeParams().lane
        return "interaction"

    def _sleep_on_shutdown_enabled(self) -> bool:
        """``auto`` (the real robot only) | ``true`` | ``false``."""

        raw = self._param_str("sleep_on_shutdown", "auto").lower()
        if raw in ("true", "1", "yes", "on"):
            return True
        if raw in ("false", "0", "no", "off"):
            return False
        return self.backend_name == "robot"

    def _param_str(self, name: str, default: str) -> str:
        value = self.get_parameter(name).value
        return str(value).strip() if value is not None else default

    # --- the ROS surface ----------------------------------------------------
    def _build_interfaces(self) -> None:
        group = ReentrantCallbackGroup()

        self._joint_pub = self.create_publisher(JointState, "/joint_states", 10)
        self._pose_pub = self.create_publisher(PoseStamped, "/reachy_mini/head_pose", 10)
        self._tf = TransformBroadcaster(self)

        # Latched: "who owns motion, and is the robot busy" must be answerable by a tool
        # that attached after the last change, without waiting for the next one.
        latched = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._status_pub = self.create_publisher(MotionStatus,
                                                 "/reachy_mini/motion_status", latched)
        self._image_pub = self.create_publisher(Image, "/reachy_mini/camera/image_raw", 10)
        self._camera_info_pub = self.create_publisher(CameraInfo,
                                                      "/reachy_mini/camera/camera_info", 10)
        self._diagnostics_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)

        self._goto_server = ActionServer(
            self, GoTo, "/reachy_mini/goto", self._execute_goto,
            cancel_callback=self._on_cancel, goal_callback=self._on_goal,
            callback_group=group)
        self._recorded_server = ActionServer(
            self, PlayRecordedMove, "/reachy_mini/play_recorded_move",
            self._execute_recorded, cancel_callback=self._on_cancel,
            goal_callback=self._on_goal, callback_group=group)

        self.create_service(Trigger, "/reachy_mini/wake_up", self._on_wake_up,
                            callback_group=group)
        self.create_service(Trigger, "/reachy_mini/goto_sleep", self._on_goto_sleep,
                            callback_group=group)
        self.create_service(SetMotorMode, "/reachy_mini/set_motor_mode",
                            self._on_set_motor_mode, callback_group=group)
        self.create_service(StopMove, "/reachy_mini/stop", self._on_stop,
                            callback_group=group)
        self.create_service(SetBool, "/reachy_mini/set_head_reserved",
                            self._on_set_head_reserved, callback_group=group)
        self.create_service(SetOwner, "/reachy_mini/set_owner", self._on_set_owner,
                            callback_group=group)

        self.create_subscription(BodyTarget, "/reachy_mini/set_target",
                                 self._on_body_target, 10, callback_group=group)

        self.arbiter.add_status_listener(self._publish_status)
        self.create_timer(STATUS_PERIOD_S, self._publish_status)
        self.create_timer(max(0.5, float(self.get_parameter("diagnostics_period_s").value)),
                          self._publish_diagnostics)

    # --- the 50 Hz hooks (called on the BACKEND thread) ---------------------
    def _publish(self, publish, *args: Any) -> bool:
        """Publish from the control-loop thread without ever raising back into it.

        This is not defensive tidiness.  These callbacks are invoked from inside
        Pollen's ``wrapped_run`` (``daemon/backend/abstract.py``:435-442), which catches
        whatever they raise, records it on ``backend.error``, calls ``close()`` and
        RE-RAISES - so one exception here does not drop one sample, it ENDS the 50 Hz
        control loop for good.

        And there is a guaranteed exception: ``rclpy`` tears its context down from a
        signal handler, so from the instant SIGTERM lands until ``shutdown()`` runs,
        every ``publish`` raises ``RCLError: publisher's context is invalid``.  Without
        this guard the control loop dies during teardown - which is precisely when
        ``shutdown()`` is trying to drive it through ``reset_to_sleep()``, and a park
        with no control loop under it leaves the robot torqued wherever it stopped.
        """

        if not self._publishing:
            return False
        try:
            publish(*args)
        except Exception:  # noqa: BLE001 - see the docstring: raising kills the robot
            self._publish_failures += 1
            return False
        return True

    def _on_joint_positions(self, message: dict) -> None:
        if self._joint_pub is None:
            # The control loop is already turning and the ROS surface is not built yet
            # (see __init__). Dropping these few samples is correct; raising here kills
            # the backend thread for good.
            return
        head = message.get("head_joint_positions") or []
        antennas = message.get("antennas_joint_positions") or []
        positions = [float(v) for v in list(head)[:7]] + [float(v) for v in list(antennas)[:2]]
        if len(positions) != len(JOINT_NAMES):
            # Never publish a JointState whose names and positions disagree in length;
            # every consumer is entitled to treat that as corrupt.
            return
        state = JointState()
        state.header.stamp = self.get_clock().now().to_msg()
        state.name = list(JOINT_NAMES)
        state.position = positions
        if self._publish(self._joint_pub.publish, state):
            self._joint_messages += 1

    def _on_head_pose(self, message: dict) -> None:
        if self._pose_pub is None or self._tf is None:
            return  # same startup window as _on_joint_positions
        matrix = message.get("head_pose")
        if matrix is None:
            return
        (x, y, z), (qx, qy, qz, qw) = matrix_to_pose(np.asarray(matrix, dtype=float))
        stamp = self.get_clock().now().to_msg()

        pose = PoseStamped()
        pose.header.stamp = stamp
        pose.header.frame_id = self._base_frame
        pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = x, y, z
        pose.pose.orientation.x, pose.pose.orientation.y = qx, qy
        pose.pose.orientation.z, pose.pose.orientation.w = qz, qw
        self._publish(self._pose_pub.publish, pose)

        transform = TransformStamped()
        transform.header.stamp = stamp
        transform.header.frame_id = self._base_frame
        transform.child_frame_id = self._head_frame
        transform.transform.translation.x = x
        transform.transform.translation.y = y
        transform.transform.translation.z = z
        transform.transform.rotation.x, transform.transform.rotation.y = qx, qy
        transform.transform.rotation.z, transform.transform.rotation.w = qz, qw
        self._publish(self._tf.sendTransform, transform)

    # --- action servers -----------------------------------------------------
    def _on_goal(self, _goal_request) -> GoalResponse:
        # Every goal is accepted at the ROS level; the arbiter is what decides, and it
        # answers with a REASON. Rejecting here would lose the reason.
        return GoalResponse.ACCEPT

    def _on_cancel(self, _goal_handle) -> CancelResponse:
        return CancelResponse.ACCEPT

    def _execute_goto(self, goal_handle):
        goal = goal_handle.request
        request = MotionRequest(
            kind="goto", lane=goal.lane, client=goal.client,
            head_pose=pose_to_matrix(goal.head_pose) if goal.use_head else None,
            antennas=((float(goal.antennas[0]), float(goal.antennas[1]))
                      if goal.use_antennas else None),
            body_yaw=float(goal.body_yaw) if goal.use_body_yaw else None,
            duration=float(goal.duration), interpolation=goal.interpolation,
            preempt=bool(goal.preempt), reason=goal.reason)
        return self._run_goal(goal_handle, request, GoTo.Result(), GoTo.Feedback,
                              float(goal.duration) + GOAL_GRACE_S)

    def _execute_recorded(self, goal_handle):
        goal = goal_handle.request
        request = MotionRequest(
            kind="play_recorded", lane=goal.lane, client=goal.client,
            dataset=goal.dataset or self._dataset, move=goal.name,
            preempt=bool(goal.preempt), reason=goal.reason)
        return self._run_goal(goal_handle, request, PlayRecordedMove.Result(),
                              PlayRecordedMove.Feedback, RECORDED_MOVE_TIMEOUT_S)

    def _run_goal(self, goal_handle, request: MotionRequest, result: Any,
                  feedback_type: Any, budget_s: float):
        """Submit, then wait - the same three lines for both actions.

        A refusal SUCCEEDS with ``completed=false`` and the code.  Aborting would tell a
        caller "something went wrong" when what happened is "the arbiter said no", and
        those need different responses from whoever asked.
        """

        outcome = self.arbiter.submit(request)
        if not outcome.accepted:
            goal_handle.succeed()
            result.completed = False
            result.error_code = outcome.error_code
            result.move_id = ""
            return result

        final = self._await_move(goal_handle, outcome.move_id, feedback_type,
                                 max(FEEDBACK_PERIOD_S, budget_s))
        if goal_handle.is_cancel_requested:
            goal_handle.canceled()
        else:
            goal_handle.succeed()
        result.completed = bool(final.accepted)
        result.error_code = final.error_code
        result.move_id = outcome.move_id
        return result

    def _await_move(self, goal_handle, move_id: str, feedback_type: Any, budget_s: float):
        """Poll the arbiter, publish progress, and honour a cancel request."""

        started = time.monotonic()
        cancelled = False
        while True:
            outcome = self.arbiter.wait(move_id, timeout=FEEDBACK_PERIOD_S)
            if outcome.error_code != "timeout":
                return outcome
            if goal_handle.is_cancel_requested and not cancelled:
                cancelled = True
                self.arbiter.stop(move_id)
            elapsed = time.monotonic() - started
            feedback = feedback_type()
            feedback.progress = float(min(1.0, elapsed / budget_s)) if budget_s > 0 else 0.0
            goal_handle.publish_feedback(feedback)
            if elapsed > budget_s:
                self.get_logger().warn(
                    f"move {move_id} did not settle within {budget_s:.1f}s; reporting a "
                    "timeout while the arbiter keeps whatever it is doing")
                return outcome

    # --- services -----------------------------------------------------------
    def _on_wake_up(self, _request, response):
        return self._trigger("wake_up", response)

    def _on_goto_sleep(self, _request, response):
        return self._trigger("goto_sleep", response)

    def _trigger(self, kind: str, response):
        outcome = self.arbiter.submit(MotionRequest(kind=kind, lane="interaction",
                                                    client="ros", reason=f"service:{kind}"))
        if not outcome.accepted:
            response.success = False
            response.message = outcome.error_code
            return response
        final = self.arbiter.wait(outcome.move_id, timeout=SERVICE_WAIT_S)
        if final.error_code == "timeout" and outcome.move_id in self.arbiter.running():
            # Accepted and still going: that is a yes to the question that was asked.
            response.success = True
            response.message = f"{kind} still running after {SERVICE_WAIT_S:.0f}s"
            return response
        response.success = bool(final.accepted)
        response.message = final.error_code or kind
        return response

    def _on_set_motor_mode(self, request, response):
        outcome = self.arbiter.set_motor_mode(request.mode)
        response.ok = bool(outcome.accepted)
        response.message = "" if outcome.accepted else outcome.error_code
        response.mode = self.arbiter.status()["motor_mode"]
        return response

    def _on_stop(self, request, response):
        response.stopped = int(self.arbiter.stop(request.move_id))
        return response

    def _on_set_head_reserved(self, request, response):
        self.arbiter.set_head_reserved(bool(request.data))
        response.success = True
        response.message = ("head reserved: expression may not move it"
                            if request.data else "head released")
        return response

    def _on_set_owner(self, request, response):
        client = (request.client or "").strip()
        if client:
            response.ok = bool(self.arbiter.acquire_owner(client, float(request.ttl_s)))
        else:
            response.ok = bool(self.arbiter.release_owner(""))
        response.owner = self.arbiter.status()["owner"]
        return response

    def _on_body_target(self, message: BodyTarget) -> None:
        outcome = self.arbiter.set_target(
            pose_to_matrix(message.head_pose) if message.use_head else None,
            ((float(message.antennas[0]), float(message.antennas[1]))
             if message.use_antennas else None),
            float(message.body_yaw) if message.use_body_yaw else None,
            client=self._set_target_client)
        if not outcome.accepted:
            self.get_logger().debug(f"set_target refused: {outcome.error_code}")

    # --- MotionStatus and diagnostics ---------------------------------------
    def _publish_status(self, state: dict | None = None) -> None:
        """Called by the arbiter on every change, and by a 1 Hz timer regardless."""

        publisher = getattr(self, "_status_pub", None)
        if publisher is None:
            return
        state = state if state is not None else self.arbiter.status()
        message = MotionStatus()
        message.busy = bool(state["busy"])
        message.running_move_id = str(state["running_move_id"])
        message.queue_depth = min(255, int(state["queue_depth"]))
        message.owner = str(state["owner"])
        message.head_reserved = bool(state["head_reserved"])
        message.expression_owner = str(state["expression_owner"])
        message.motor_mode = str(state["motor_mode"])
        message.backend = str(state["backend"])
        # Same reason as `_publish`: the arbiter calls this listener from its own loop
        # thread on every change, including the cancellations `shutdown()` issues after
        # rclpy's context is already gone.
        self._publish(publisher.publish, message)

    def _publish_diagnostics(self) -> None:
        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_rate_sample)
        self._joint_hz = (self._joint_messages - self._last_joint_messages) / elapsed
        self._camera_hz = (self._camera_frames - self._last_camera_frames) / elapsed
        self._last_rate_sample = now
        self._last_joint_messages = self._joint_messages
        self._last_camera_frames = self._camera_frames

        state = self.arbiter.status()
        ready = self.backend_host.is_ready()
        # Noticed here, acted on at the END of this method: `_on_backend_death` shuts
        # rclpy down, and doing that first would invalidate the publisher's context
        # before the one DiagnosticStatus that says ERROR ever went out - so the last
        # word on /diagnostics would be the healthy sample before the death.
        died = not ready and self._backend_was_ready
        self._backend_was_ready = self._backend_was_ready or ready
        values = [
            KeyValue(key="backend", value=self.backend_name),
            KeyValue(key="ready", value=str(ready).lower()),
            KeyValue(key="motor_mode", value=str(state["motor_mode"])),
            KeyValue(key="busy", value=str(state["busy"]).lower()),
            KeyValue(key="queue_depth", value=str(state["queue_depth"])),
            KeyValue(key="owner", value=str(state["owner"])),
            KeyValue(key="head_reserved", value=str(state["head_reserved"]).lower()),
            KeyValue(key="expression_owner", value=str(state["expression_owner"])),
            KeyValue(key="joint_state_hz", value=f"{self._joint_hz:.1f}"),
            KeyValue(key="camera_hz", value=f"{self._camera_hz:.1f}"),
            KeyValue(key="publish_failures", value=str(self._publish_failures)),
            KeyValue(key="rest_shim", value=str(getattr(self._shim, "url", "") or "disabled")),
            KeyValue(key="running_client", value=str(state["running_client"])),
            KeyValue(key="running_lane", value=str(state["running_lane"])),
        ]
        gaze_running = False
        if self._gaze is not None:
            gaze_values = self._gaze.diagnostics()
            gaze_running = gaze_values.get("gaze_enabled") == "true"
            values.extend(KeyValue(key=key, value=value)
                          for key, value in gaze_values.items())
        values.extend(KeyValue(key=name, value=str(count))
                      for name, count in sorted(state["counters"].items()))

        # A camera that has gone quiet is not normally the robot being unwell - but it
        # IS when something is trying to look at faces through it, because that feature
        # silently does nothing and looks exactly like an empty room.
        starved = gaze_running and self._camera_hz == 0.0
        self._camera_starved_periods = (
            self._camera_starved_periods + 1 if starved else 0)

        level, message = DiagnosticStatus.OK, f"backend={self.backend_name} ready"
        if not ready:
            level = DiagnosticStatus.ERROR
            message = ("the backend is not ready: nothing will move and /joint_states is "
                       "empty")
        elif self._joint_hz < JOINT_RATE_WARN_HZ:
            level = DiagnosticStatus.WARN
            message = (f"/joint_states at {self._joint_hz:.1f} Hz, below the {JOINT_RATE_WARN_HZ:.0f} Hz "
                       "the 50 Hz control loop should produce")
        elif self._camera_starved_periods >= 2:
            level = DiagnosticStatus.WARN
            message = ("the gaze camera is not producing frames: gaze is enabled and "
                       f"camera_hz has been 0.0 for {self._camera_starved_periods} "
                       "periods. Check the camera bridge's ROS tee "
                       "(EMORI_CAM_ROS_VIDEO_NR) and /dev/video41.")

        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [DiagnosticStatus(level=level, name="reachy_mini", message=message,
                                         hardware_id=self.backend_name, values=values)]
        self._diagnostics_pub.publish(array)
        if died:
            self._on_backend_death()

    def _on_backend_death(self) -> None:
        """The control loop was ready and is not any more: end the process, loudly.

        A node that keeps spinning here is the worst possible failure, because it is the
        quietest: the action servers still accept goals, the REST shim still answers,
        /joint_states simply stops, and every client goes on believing in a robot that
        has not moved in an hour.  There is no recovery from inside - Pollen's backends
        cannot be re-run after ``wrapped_run`` returns, the serial port is closed and the
        control loop's Rust half is gone - so the honest move is to die and let the
        supervisor (`Restart=always`, `restart: unless-stopped`) build a new one.

        Once, because this fires from a periodic timer that will tick again.
        """

        if self._backend_dead:
            return
        self._backend_dead = True
        self.exit_code = BACKEND_DEAD_EXIT_CODE
        self.get_logger().error(
            "the backend control loop has DIED after having been ready "
            f"({self.backend_host.error() or 'no reason recorded'}). Nothing will move "
            "and /joint_states has stopped. Exiting with "
            f"{BACKEND_DEAD_EXIT_CODE} so the supervisor restarts this node; a process "
            "that stayed up here would keep accepting goals for a robot that is gone.")
        try:
            rclpy.shutdown()
        except Exception as error:  # noqa: BLE001 - already shut down is fine
            self.get_logger().warn(f"rclpy.shutdown() during backend death: {error}")

    # --- the sim camera -----------------------------------------------------
    def _start_camera(self) -> None:
        model, data = self.backend_host.mujoco_scene()
        if model is None:
            self.get_logger().warn("no MuJoCo scene to render; camera disabled")
            return
        self._camera_thread = threading.Thread(target=self._camera_loop, args=(model, data),
                                               name="reachy_mini_camera", daemon=True)
        self._camera_thread.start()

    def _camera_loop(self, model: Any, data: Any) -> None:
        """Render the sim camera offscreen.  One thread, one Renderer, for its lifetime.

        ``mujoco.Renderer`` binds a GL context to the thread that creates it, so the
        renderer must never be shared - and it must not live on the backend thread
        either, or a slow render would eat into the 50 Hz control loop.
        """

        # EGL is the headless path that works CPU-only with Mesa; the image sets this
        # too, but a node launched by hand on a bare machine would otherwise fail with
        # an unhelpful GLFW error.
        os.environ.setdefault("MUJOCO_GL", "egl")
        try:
            import mujoco
            width, height = self._camera_size
            renderer = mujoco.Renderer(model, height=height, width=width)
        except Exception as error:  # noqa: BLE001 - no camera is a degraded node, not a dead one
            self.get_logger().warn(f"sim camera unavailable ({error}); no frames will be "
                                   "published on /reachy_mini/camera/image_raw")
            return

        fovy = self._camera_fovy(model)
        period = 1.0 / self._camera_fps
        while not self._camera_stop.is_set() and rclpy.ok():
            started = time.monotonic()
            try:
                renderer.update_scene(data, self._camera_name)
                frame = renderer.render()
            except Exception as error:  # noqa: BLE001 - one bad frame must not end the stream
                self.get_logger().warn(f"render failed: {error}")
                self._camera_stop.wait(period)
                continue
            self._publish_frame(frame, fovy)
            # `.copy()` because `mujoco.Renderer.render()` hands back its own internal
            # output buffer and reuses it on the very next call. `_publish_frame` has
            # already serialised the bytes by now, but the gaze thread only PARKS the
            # reference and reads it later - so without a copy the next render would
            # rewrite the pixels underneath a detection in flight. The v4l2 path needs
            # no such copy: `rgb_frame_from_buffer` already ends in one (video.py:38).
            self._offer_frame_to_gaze(frame.copy())
            self._camera_stop.wait(max(0.0, period - (time.monotonic() - started)))
        try:
            renderer.close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass

    # --- the real camera ----------------------------------------------------
    def _start_v4l2_camera(self) -> None:
        """Capture from a V4L2 device in this node, through GStreamer.

        The alternative was ROS's own `v4l2_camera` node, and it was measured and
        rejected for the robot: it depends on `cv_bridge`, `cv_bridge` depends on
        `libopencv-dev`, and that pulls VTK, GDAL, LLVM and Mesa -- 640 MB on a card with
        2.8 GB free, on a robot that renders nothing. GStreamer's `v4l2src` costs 63 MB
        (plugins base + good), and PyGObject is already in the image because Pollen's own
        media module imports it.

        On a robot the device is `camera.device` (`REACHY_VIDEO_DEVICE`): the robot
        compose profile names `/dev/video41`, the second v4l2loopback node the camera
        bridge tees. Not `/dev/video40`: v4l2loopback with `exclusive_caps=Y` gives its
        buffers to one capture consumer at a time, and on a kiosk robot that consumer is
        Chromium, so a capture on video40 there refuses to start and says why.
        """

        self._camera_thread = threading.Thread(target=self._v4l2_camera_loop,
                                               name="reachy_mini_camera", daemon=True)
        self._camera_thread.start()

    def _v4l2_camera_loop(self) -> None:
        """One pipeline at a time, latest-frame-wins, REBUILT when the device goes away.

        `max-buffers=1 drop=true sync=false` on the sink is what makes a slow consumer a
        consumer of newer frames rather than a growing queue of old ones: this node
        publishes at `camera.fps`, and the device runs at whatever it runs at.

        The outer loop is not defensive programming, it is the production case: any
        `systemctl restart emori-camera-bridge` - which `Restart=on-failure` can do on
        its own - takes the v4l2loopback writer away for a second or two, and the old
        code answered that by breaking out of the capture loop for the LIFE OF THE
        PROCESS after one WARN. The symptom is a camera topic that silently stops and a
        face detector that reports an empty room for ever, on a robot that otherwise
        looks perfectly well. So: tear the pipeline down, back off (1, 2, 4 ... 30 s)
        and build a new one, until the node is actually shutting down.
        """

        width, height = self._camera_size
        device = self._camera_device
        fovy = float(self.get_parameter("camera.fovy_deg").value)
        attempt = 0

        while not self._camera_stop.is_set() and rclpy.ok():
            built = self._build_v4l2_pipeline(device, width, height,
                                              quiet=attempt > 0)
            if built is not None:
                gst, pipeline, sink = built
                self.get_logger().info(
                    f"camera: capturing {width}x{height} from {device} at "
                    f"{self._camera_fps:.0f} Hz")
                published = self._capture_v4l2(gst, sink, device, width, height, fovy)
                try:
                    pipeline.set_state(gst.State.NULL)
                except Exception:  # noqa: BLE001 - teardown is best effort
                    pass
                if published:
                    attempt = 0
            if self._camera_stop.is_set() or not rclpy.ok():
                return
            delay = min(2 ** attempt, 30)
            attempt = min(attempt + 1, 5)
            self.get_logger().warn(
                f"camera {device!r}: rebuilding the pipeline in {delay}s. The robot is "
                "unaffected; /reachy_mini/camera/image_raw is empty until it comes back.")
            self._camera_stop.wait(delay)

    def _build_v4l2_pipeline(self, device: str, width: int, height: int, *,
                             quiet: bool = False):
        """``(Gst, pipeline, sink)`` for a playing pipeline, or None with a WARN.

        `quiet` suppresses the WARN on a retry: a device that is gone stays gone for as
        long as the bridge takes to come back, and the robot is at 94% disk.
        """

        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            pipeline = Gst.parse_launch(
                f"v4l2src device={device} ! videoconvert ! videoscale ! "
                f"video/x-raw,format=RGB,width={width},height={height} ! "
                "appsink name=sink max-buffers=1 drop=true sync=false")
            sink = pipeline.get_by_name("sink")
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("the pipeline refused to play")
            # PLAYING is ASYNC: `set_state` returning anything but FAILURE means "asked",
            # not "streaming". The real answer arrives on the bus, and without reading it
            # a device that refused us looks exactly like a camera that is merely slow --
            # a warning every few seconds, forever, and nobody ever learns why. On a
            # robot the bus says `Device '/dev/video40' is busy / Call to S_FMT
            # failed for YUYV @ 1280x720` when v4l2loopback has handed its buffers to
            # ONE capture consumer and the kiosk's Chromium already has them.
            failure = pipeline.get_bus().timed_pop_filtered(
                3 * Gst.SECOND, Gst.MessageType.ERROR)
            if failure is not None:
                error, _debug = failure.parse_error()
                raise RuntimeError(error.message)
        except Exception as error:  # noqa: BLE001 - no camera is a degraded node, not a dead one
            if not quiet:
                self.get_logger().warn(
                    f"camera {device!r} unavailable ({error}); no frames will be "
                    "published on /reachy_mini/camera/image_raw. The robot is "
                    "unaffected.")
            return None
        return Gst, pipeline, sink

    def _capture_v4l2(self, gst: Any, sink: Any, device: str, width: int, height: int,
                      fovy: float) -> int:
        """Publish frames until the node stops or the device goes quiet.

        Returns how many frames were published, which is what tells the caller whether
        this was a working pipeline that died (start the backoff again from 1 s) or a
        device that never produced anything (keep backing off).
        """

        period = 1.0 / self._camera_fps
        misses = 0
        published = 0
        while not self._camera_stop.is_set() and rclpy.ok():
            started = time.monotonic()
            try:
                # try-pull-sample, not pull-sample: the blocking form waits for a frame or
                # EOS forever, and a device that went quiet would hold this thread past
                # `_camera_stop` and past rclpy shutdown.
                sample = sink.emit("try-pull-sample", int(2 * gst.SECOND))
                frame = None
                if sample is not None:
                    buffer = sample.get_buffer()
                    ok, mapped = buffer.map(gst.MapFlags.READ)
                    if ok:
                        try:
                            frame = rgb_frame_from_buffer(bytes(mapped.data), width, height)
                        finally:
                            buffer.unmap(mapped)
            except Exception as error:  # noqa: BLE001 - one bad frame must not end the stream
                self.get_logger().warn(f"capture failed: {error}")
                frame = None
            if frame is None:
                misses += 1
                # A device that has stopped producing is worth one line, not one per
                # frame. At the miss limit this pipeline is declared dead and the caller
                # builds a new one -- the device may well have been recreated under us.
                if misses in (1, 50):
                    self.get_logger().warn(f"camera {device!r}: {misses} frame(s) missed")
                if misses >= _CAMERA_MISS_LIMIT:
                    self.get_logger().warn(
                        f"camera {device!r}: no frames after {misses} attempts; the "
                        "pipeline is dead and will be rebuilt.")
                    return published
                self._camera_stop.wait(period)
                continue
            misses = 0
            published += 1
            self._publish_frame(frame, fovy)
            self._offer_frame_to_gaze(frame)
            self._camera_stop.wait(max(0.0, period - (time.monotonic() - started)))
        return published

    def _camera_fovy(self, model: Any) -> float:
        """The camera's vertical field of view in degrees, for an honest CameraInfo."""

        try:
            import mujoco
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, self._camera_name)
            if index >= 0:
                return float(model.cam_fovy[index])
        except Exception:  # noqa: BLE001 - fall back to MuJoCo's own default
            pass
        return 45.0

    def _publish_frame(self, frame: np.ndarray, fovy: float) -> None:
        height, width = int(frame.shape[0]), int(frame.shape[1])
        stamp = self.get_clock().now().to_msg()

        image = Image()
        image.header.stamp = stamp
        image.header.frame_id = self._camera_name
        image.height, image.width = height, width
        image.encoding = "rgb8"
        image.is_bigendian = 0
        image.step = width * 3
        image.data = np.ascontiguousarray(frame, dtype=np.uint8).tobytes()
        self._publish(self._image_pub.publish, image)

        info = CameraInfo()
        info.header = image.header
        info.height, info.width = height, width
        info.distortion_model = "plumb_bob"
        info.d = [0.0, 0.0, 0.0, 0.0, 0.0]
        focal = (height / 2.0) / math.tan(math.radians(fovy) / 2.0)
        cx, cy = width / 2.0, height / 2.0
        info.k = [focal, 0.0, cx, 0.0, focal, cy, 0.0, 0.0, 1.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [focal, 0.0, cx, 0.0, 0.0, focal, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
        if self._publish(self._camera_info_pub.publish, info):
            self._camera_frames += 1

    # --- the REST compatibility shim ---------------------------------------
    def _start_rest_shim(self) -> None:
        """Serve the daemon's dialect on :8000 so the host application never notices.

        Imported lazily and tolerantly: ``rest_shim.py`` is a compatibility layer, and a
        robot node that refuses to start because a compatibility layer is missing has
        the priorities backwards.
        """

        try:
            from .rest_shim import start_rest_shim
        except ImportError as error:
            self.get_logger().warn(
                f"rest_shim is not available ({error}); the daemon-compatible REST "
                "surface will not be served. The ROS actions and services are "
                "unaffected, but a host application pointed at this port will not "
                "find a robot.")
            return

        host = self._param_str("rest_shim.host", "0.0.0.0")
        port = int(self.get_parameter("rest_shim.port").value)
        try:
            self._shim = start_rest_shim(
                self.arbiter, self.backend_host, host=host, port=port,
                version=_reachy_mini_version(), backend_name="reachy_mini_ros2",
                # The shim's whole job is to be indistinguishable from the daemon, and
                # this is a field where it was distinguishable: on a wireless robot
                # Pollen's daemon answers `wireless_version: true`, and the shim answered
                # false because the node knew and never said.
                wireless_version=bool(self.get_parameter("wireless_version").value),
                # Both None when this node has no gaze component, which is what makes
                # `/api/emori/gaze` answer 503 rather than pretending.
                gaze=self._set_gaze_enabled if self._gaze is not None else None,
                gaze_active=self._gaze_is_active if self._gaze is not None else None,
                face_seen=self._gaze.face_seen_s if self._gaze is not None else None,
                log=self.get_logger())
        except Exception as error:  # noqa: BLE001 - a busy port must not kill the robot
            self.get_logger().error(
                f"could not start the REST shim on {host}:{port} ({error}); the ROS "
                "surface is up but the host application has nothing to talk to")

    # --- the link surface ---------------------------------------------------
    def _start_link_surface(self) -> None:
        """Serve the shim's questions and commands as services on ``reachy_mini_link``.

        Imported lazily and tolerantly, like the shim: a node that cannot build its link
        still moves, and says so.
        """

        try:
            from reachy_mini_ros2.ros_surface import start_link_surface
            from reachy_mini_ros2.state_snapshot import make_state_snapshot

            state = make_state_snapshot(
                self.arbiter, self.backend_host,
                version=_reachy_mini_version(), backend_name="reachy_mini_ros2",
                wireless_version=bool(self.get_parameter("wireless_version").value),
                gaze_active=self._gaze_is_active if self._gaze is not None else None,
                face_seen=self._gaze.face_seen_s if self._gaze is not None else None)
            self._link = start_link_surface(
                self.arbiter, state,
                set_gaze=self._set_gaze_enabled if self._gaze is not None else None,
                gaze_run=self._gaze.run_flag if self._gaze is not None else None,
                backend_name="reachy_mini_ros2", log=self.get_logger())
        except Exception as error:  # noqa: BLE001 - the robot must come up without it
            self.get_logger().error(
                f"could not start the link surface ({error}); the robot moves, but "
                "/reachy_mini/submit_move and its siblings are not served")

    # --- startup / shutdown -------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._loop_ready.set)
        self._loop.run_forever()

    def _wake_up_on_start(self) -> None:
        outcome = self.arbiter.submit(MotionRequest(kind="wake_up", lane="interaction",
                                                    client="ros", reason="startup"))
        if not outcome.accepted:
            self.get_logger().warn(f"startup wake_up refused: {outcome.error_code}")
            return
        final = self.arbiter.wait(outcome.move_id, timeout=STARTUP_WAKE_WAIT_S)
        if not final.accepted:
            self.get_logger().warn(f"startup wake_up did not complete: {final.error_code}")

    def shutdown(self) -> None:
        """Camera, shim, SLEEP, arbiter, backend - in that order, and the order matters.

        The camera and the shim go first so nothing new arrives while the robot parks.
        Then the park itself, which is the whole point of this method on real hardware:
        ``backend.reset_to_sleep()`` re-establishes position control from whatever state
        the last client left (gravity compensation, a per-motor torque cut, limp), lifts
        to the init pose, plays the sleep trajectory and ends de-torqued AT the sleep
        pose.  Skipping it leaves a robot holding its head up on nine torqued servos
        until something cuts the power, and then dropping it.  This is exactly what
        Pollen's daemon does at ``daemon/daemon.py``:557-573, in the same position
        relative to ``should_stop`` and ``close``.

        The arbiter closes AFTER the sleep, not before: ``close()`` sets ``_closed``,
        and a closed arbiter refuses everything - so parking through it is not an
        option, which is why the sleep goes straight to the backend on the arbiter's
        own loop.  Running it there rather than on a new loop is what keeps the
        single-writer rule true for the last move of the process.
        """

        # First, and before the camera: from here on nothing this process publishes can
        # succeed (rclpy's context went down with the signal), and the only thing left
        # to decide is whether the attempts are silent or fatal.
        self._publishing = False
        self._camera_stop.set()
        if self._gaze is not None:
            # Before the camera thread: the gaze thread writes to the arbiter, and a
            # target landing during the park would be a second author of the last move
            # of the process.
            self._gaze.stop()
        if self._camera_thread is not None:
            self._camera_thread.join(2.0)
        if self._link is not None:
            self._link.stop()
        if self._shim is not None:
            try:
                self._shim.stop()
            except Exception as error:  # noqa: BLE001 - teardown is best effort
                self.get_logger().warn(f"rest shim stop failed: {error}")
        self._sleep_on_shutdown()
        try:
            self.arbiter.close()
        except Exception as error:  # noqa: BLE001 - teardown is best effort
            self.get_logger().warn(f"arbiter close failed: {error}")
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(2.0)
        self.backend_host.close()

    def _sleep_on_shutdown(self) -> None:
        """Park and de-torque the robot, or say why it did not happen and carry on.

        Never raises.  A teardown that dies half way through leaves the serial port
        held and the next start fails to find a bus - which is a worse outcome than a
        robot that was not parked, and it is also the failure that hides the reason.
        """

        if not self._sleep_on_shutdown_enabled():
            return
        backend = self.backend_host.backend
        if backend is None or not hasattr(backend, "reset_to_sleep"):
            return

        # Whatever was still in flight has to stop FIRST. `rclpy.spin` has returned, so
        # nothing new can arrive, but a goal that was running when the signal landed is
        # still writing targets on this same loop - and a park that fights it is two
        # writers into one head at the one moment nobody is watching. This is `stop`,
        # not `close`: a closed arbiter refuses everything, and the park would be the
        # first thing it refused.
        self.arbiter.stop("")
        deadline = time.monotonic() + 2.0
        while self.arbiter.running() and time.monotonic() < deadline:
            time.sleep(0.005)

        self.get_logger().info("parking the robot: reset_to_sleep()")
        try:
            future = asyncio.run_coroutine_threadsafe(backend.reset_to_sleep(), self._loop)
            future.result(timeout=SLEEP_ON_SHUTDOWN_TIMEOUT_S)
            self.get_logger().info("the robot is parked and de-torqued")
        except Exception as error:  # noqa: BLE001 - shutdown continues either way
            self.get_logger().error(
                f"could not park the robot on shutdown ({error!r}); the motors may still "
                "be torqued at whatever pose the last move left. Power-cycle or run "
                "reset_to_sleep by hand before moving it.")


def _reachy_mini_version() -> str:
    """The Pollen version this node is driving, reported by the shim as the daemon's."""

    try:
        from importlib.metadata import version
        return version("reachy-mini")
    except Exception:  # noqa: BLE001 - a version nobody can read is not worth failing over
        return "unknown"


def main() -> None:
    rclpy.init()
    try:
        # Construction is where the robot is built and woken, so it is also where the
        # honest failures live (no serial port, a backend that never becomes ready).
        # Those must not land in a `finally` that then trips over an undefined `node`.
        node = ReachyMiniNode()
    except BaseException:
        rclpy.try_shutdown()
        raise
    # One thread per core, never fewer than four. The four that matter are: the
    # `set_target` subscription (which must not queue behind anything), the two action
    # servers, and the services - all on one ReentrantCallbackGroup, so a blocking
    # `_await_move` in one holds a thread for the length of the move. The default of
    # (cpu_count) is 1 on a container pinned to a single CPU, and one thread means the
    # first goal that waits deafens the node until it settles.
    executor = MultiThreadedExecutor(num_threads=max(4, os.cpu_count() or 4))
    try:
        rclpy.spin(node, executor=executor)
    except (KeyboardInterrupt, ExternalShutdownException):
        # ExternalShutdownException is how `_on_backend_death`'s rclpy.shutdown() comes
        # back here; the exit code is already set on the node.
        pass
    finally:
        exit_code = node.exit_code
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
