"""Ownership of Pollen's ``Backend`` in this process: build it, run it, read it, stop it.

Pollen ships the Reachy Mini as a FastAPI daemon, but the daemon is a thin shell: the
``Backend`` hierarchy (``MujocoBackend``, ``MockupSimBackend``, ``RobotBackend``), the
kinematics engines and ``motion/`` reference no HTTP at all.  Their only outward channel
is ``reachy_mini.io.publisher.Publisher``, which wraps *any* callable.  That is the whole
reason this package can exist: a ROS node can hold the backend directly and hand those
publishers to itself, and the 50 Hz control loop stops being something on the other side
of a socket.

This module is the seam.  Everything version- and backend-specific about Pollen lives
here - how a backend is constructed, which thread ``wrapped_run`` needs, that ``ready``
and ``should_stop`` are ``threading.Event``s, and that MuJoCo mirrors the antennas
internally - so ``robot_node.py`` can be about ROS and ``arbiter.py`` about arbitration.

Imports of ``reachy_mini`` are deliberately LAZY.  The package is only installed in the
Jazzy image and on the robot; keeping the import inside the call that needs it lets the
module be parsed, linted and ast-checked on a developer machine that has neither ROS nor
Pollen, which is where most of this repository's tests run.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Sequence

import numpy as np

# The canonical emotions library. 85 moves; pre-downloaded into the v2 image at build
# time so a rehearsal needs no network.
DEFAULT_EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"

# The nine joints, in the order every nine-valued array in this package uses:
# /joint_states, the REST shim's `head_joints`, everything. Pollen reports the head as
# 7 values (body yaw first, then the Stewart platform) and the antennas as 2.
JOINT_NAMES: tuple[str, ...] = (
    "body_yaw",
    "stewart_1", "stewart_2", "stewart_3", "stewart_4", "stewart_5", "stewart_6",
    "right_antenna", "left_antenna",
)

# How long to wait for the backend thread to say it is ready. MuJoCo took 0.51 s in the
# feasibility probe; the real robot has to talk to nine servos over a 1 Mbaud bus first.
DEFAULT_READY_TIMEOUT_S = 20.0

BACKENDS = ("mujoco", "mockup", "robot")

# TEST ONLY.  ``REACHY_FAULT_INJECT_S=<seconds>`` makes the control-loop thread raise
# that many seconds after it becomes ready, which is the only way to produce - on
# purpose, in a container, on a laptop - the failure this module now has to report
# honestly: Pollen's ``wrapped_run`` catches the exception, records it on
# ``backend.error``, calls ``close()`` and re-raises, so the THREAD dies while the
# process, the ROS node and the REST shim all keep running and answering.
#
# It is read from the environment rather than exposed as a parameter so that it cannot
# be set by a launch file or a rosparam dump, and it is loud: every path logs that a
# fault was armed.  ``scripts/ros2_v2_smoke.py --kill-backend-check`` is its only
# caller.  It is never set on a robot.
FAULT_INJECT_ENV = "REACHY_FAULT_INJECT_S"


@dataclass
class BackendConfig:
    """Everything the node's parameters say about which robot to build."""

    backend: str = "mujoco"
    scene: str = "empty"
    headless: bool = True
    kinematics_engine: str = "AnalyticalKinematics"
    # "" means "find it": the wireless unit is the Pi's own UART (/dev/ttyAMA3), the
    # lite is a USB device found by VID:PID. Pollen's own finder answers both.
    serialport: str = ""
    wireless_version: bool = False
    hardware_config: str = ""
    check_collision: bool = False
    # Off by default on every backend, including the real robot: `use_audio` opens the
    # ReSpeaker over USB for direction-of-arrival, and on an EMORI robot PipeWire already
    # owns the microphone. The daemon we replace runs with --no-media for the same
    # reason.
    use_audio: bool = False
    log_level: str = "INFO"


class BackendHost:
    """Build, run and read one Pollen backend, and cache its 50 Hz state.

    The state cache is not an optimisation for its own sake: the REST compatibility
    shim's ``/api/state/full`` is polled at 10 Hz by the host application's gesture
    sensor, and answering that from the last published message costs nothing, while
    reaching into the backend's kinematics on every poll would compete with the control
    loop for the GIL on a CM4.
    """

    def __init__(self, config: BackendConfig, log: Any = None) -> None:
        self.config = config
        self._log = log
        # `mujoco` | `mockup` | `robot`, resolved once and public: the REST shim reports
        # it as the daemon's `simulation_enabled` / `mockup_sim_enabled`, and a client
        # that asks "am I talking to a simulator" deserves a straight answer.
        self.kind = (config.backend or "mujoco").strip().lower()
        self.backend: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._head_pose: np.ndarray | None = None
        self._head_joints: list[float] | None = None
        self._antennas: list[float] | None = None
        self._joint_messages = 0
        self._pose_messages = 0
        self._motor_mode = ""
        self._fault_timer: threading.Timer | None = None

    # --- life cycle ---------------------------------------------------------
    def start(self, on_joint_positions: Callable[[dict], None] | None = None,
              on_head_pose: Callable[[dict], None] | None = None,
              ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S) -> None:
        """Construct the backend, hook its publishers, run it, wait for ``ready``.

        Raises ``RuntimeError`` if the backend never becomes ready: a node that spins
        with a dead backend publishes an empty ``/joint_states`` forever and looks
        exactly like a slow start, which is the failure that wastes an afternoon.
        """

        from reachy_mini.io.publisher import Publisher

        self.backend = self._build()

        # Publisher, not a bare callable: the backend calls `.put(model)` and the
        # wrapper is what turns a pydantic model into the JSON string we parse. Handing
        # it a plain function is the first thing that goes wrong when someone writes
        # this from memory.
        self.backend.set_joint_positions_publisher(
            Publisher(lambda payload: self._on_joint_positions(payload, on_joint_positions)))
        self.backend.set_pose_publisher(
            Publisher(lambda payload: self._on_head_pose(payload, on_head_pose)))

        self._thread = threading.Thread(target=self.backend.wrapped_run,
                                        name="reachy_mini_backend", daemon=True)
        self._thread.start()
        if not self.backend.ready.wait(ready_timeout_s):
            error = getattr(self.backend, "error", None)
            raise RuntimeError(
                f"the {self.config.backend} backend was not ready after {ready_timeout_s}s"
                + (f": {error}" if error else ""))
        self._motor_mode = self.motor_mode()
        self._arm_fault_injection()

    def _arm_fault_injection(self) -> None:
        """TEST ONLY: schedule a failure inside the control-loop thread.  See
        ``FAULT_INJECT_ENV``."""

        raw = (os.environ.get(FAULT_INJECT_ENV) or "").strip()
        if not raw:
            return
        try:
            delay = float(raw)
        except ValueError:
            self._warn(f"{FAULT_INJECT_ENV}={raw!r} is not a number; ignoring")
            return
        if delay <= 0:
            return
        backend = self.backend

        def _fault() -> None:
            # Exactly what a real fault does: set `error`, stop the loop, and leave the
            # thread dead. `should_stop` is what makes Pollen's `run()` return, which is
            # how the thread ends without a traceback nobody asked for.
            self._warn(f"{FAULT_INJECT_ENV}: injecting a control-loop failure now")
            try:
                backend.error = f"injected by {FAULT_INJECT_ENV}={delay}"
                backend.should_stop.set()
            except Exception as error:  # noqa: BLE001 - test scaffolding, never fatal
                self._warn(f"{FAULT_INJECT_ENV}: could not inject: {error}")

        self._warn(f"{FAULT_INJECT_ENV}={delay}: THIS IS A TEST BUILD - the control loop "
                   "will be failed on purpose. Never set this on a robot.")
        self._fault_timer = threading.Timer(delay, _fault)
        self._fault_timer.daemon = True
        self._fault_timer.start()

    def _build(self) -> Any:
        """The one place a Pollen backend class is named.

        Mirrors ``reachy_mini.daemon.daemon.Daemon._setup_backend`` - including the
        serial-port search, which is the part with a genuine failure mode: no port, or
        two ports, are both errors the daemon refuses to guess its way past, and so do
        we.
        """

        name = self.kind
        if name == "mockup":
            from reachy_mini.daemon.backend.mockup_sim.backend import MockupSimBackend
            return MockupSimBackend(
                check_collision=self.config.check_collision,
                kinematics_engine=self.config.kinematics_engine,
                use_audio=self.config.use_audio,
            )
        if name == "mujoco":
            from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
            return MujocoBackend(
                scene=self.config.scene,
                check_collision=self.config.check_collision,
                kinematics_engine=self.config.kinematics_engine,
                headless=self.config.headless,
                use_audio=self.config.use_audio,
            )
        if name != "robot":
            raise ValueError(f"unknown backend {self.config.backend!r}; known: {BACKENDS}")

        from reachy_mini.daemon.backend.robot.backend import RobotBackend
        from reachy_mini.daemon.utils import find_serial_port

        serialport = (self.config.serialport or "").strip()
        if not serialport:
            ports = find_serial_port(wireless_version=self.config.wireless_version)
            if not ports:
                raise RuntimeError(
                    "no Reachy Mini serial port found. On the wireless unit that is "
                    "/dev/ttyAMA3 (check the device is passed into the container and "
                    "the user is in `dialout`); on the lite it is found by USB VID:PID. "
                    "Set the `serialport` parameter to name one explicitly.")
            if len(ports) > 1:
                raise RuntimeError(f"several Reachy Mini serial ports found ({ports}); "
                                   "set the `serialport` parameter to choose one")
            serialport = ports[0]
            self._info(f"found Reachy Mini serial port: {serialport}")

        hardware_config = _hardware_config_path(self.config.hardware_config)
        self._info(f"motor config (PID gains): {hardware_config or 'firmware defaults'}")
        return RobotBackend(
            serialport=serialport,
            log_level=self.config.log_level,
            check_collision=self.config.check_collision,
            kinematics_engine=self.config.kinematics_engine,
            use_audio=self.config.use_audio,
            wireless_version=self.config.wireless_version,
            hardware_config_filepath=hardware_config,
        )

    def close(self) -> None:
        """Stop the control loop and release the hardware.  Safe to call twice."""

        if self._fault_timer is not None:
            self._fault_timer.cancel()
            self._fault_timer = None
        backend, self.backend = self.backend, None
        if backend is None:
            return
        try:
            backend.should_stop.set()
        except Exception as error:  # noqa: BLE001 - teardown is best effort
            self._warn(f"could not signal the backend to stop: {error}")
        if self._thread is not None:
            self._thread.join(5.0)
            self._thread = None
        try:
            backend.close()
        except Exception as error:  # noqa: BLE001 - teardown is best effort
            self._warn(f"backend close failed: {error}")

    # --- the 50 Hz hooks ----------------------------------------------------
    def _on_joint_positions(self, payload: str, forward: Callable[[dict], None] | None) -> None:
        message = self._parse(payload)
        if message is None:
            return
        head = message.get("head_joint_positions") or []
        antennas = message.get("antennas_joint_positions") or []
        with self._lock:
            self._head_joints = [float(v) for v in head]
            self._antennas = [float(v) for v in antennas]
            self._joint_messages += 1
        if forward is not None:
            forward(message)

    def _on_head_pose(self, payload: str, forward: Callable[[dict], None] | None) -> None:
        message = self._parse(payload)
        if message is None:
            return
        matrix = message.get("head_pose")
        if matrix is not None:
            with self._lock:
                self._head_pose = np.asarray(matrix, dtype=float)
        with self._lock:
            self._pose_messages += 1
        if forward is not None:
            forward(message)

    def _parse(self, payload: Any) -> dict | None:
        """Publisher payloads are JSON strings; tolerate a dict for tests."""

        if isinstance(payload, dict):
            return payload
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8")
            decoded = json.loads(payload)
        except (TypeError, ValueError) as error:
            self._warn(f"unparsable backend message: {error}")
            return None
        return decoded if isinstance(decoded, dict) else None

    # --- present state ------------------------------------------------------
    def present_head_pose(self) -> np.ndarray:
        """The head frame relative to ``base_link`` as Pollen's 4x4 matrix."""

        with self._lock:
            if self._head_pose is not None:
                return self._head_pose.copy()
        if self.backend is None:
            return np.eye(4)
        try:
            return np.asarray(self.backend.get_present_head_pose(), dtype=float).copy()
        except Exception:  # noqa: BLE001 - before the first tick there is nothing to read
            return np.eye(4)

    def commanded_head_pose(self) -> np.ndarray | None:
        """The head pose the backend is currently COMMANDING, or None if it cannot say.

        The counterpart of ``present_head_pose``, and the difference between the two is
        the whole point: ``measured - commanded`` is what a human's hand does to a stiff
        head, and it is independent of whatever the robot is doing to itself.  The REST
        shim publishes that residual so the host application's push-turn sensor can tell
        a person from the robot's own motion, and the gaze controller reads the command
        so it can carry the last author's roll forward unchanged.

        Pollen's ``AbstractBackend`` keeps ``_last_target_head_pose`` - the pose actually
        fed to IK, updated on every tick of ``goto_target``'s interpolation and by every
        ``set_target`` - so this is the INSTANTANEOUS command, not a stale goal.
        ``target_head_pose`` is the fallback for a backend that has been commanded but
        has not yet run IK.

        ``None`` means "unknown" (a fake in a test, a backend before its first tick, a
        malformed target), and every caller degrades to exactly what it did before the
        command was available - which is what makes a v1 rollback byte-identical.
        """

        backend = self.backend
        if backend is None:
            return None
        for attr in ("_last_target_head_pose", "target_head_pose"):
            pose = getattr(backend, attr, None)
            if pose is None:
                continue
            try:
                return np.asarray(pose, dtype=float).reshape((4, 4)).copy()
            except Exception:  # noqa: BLE001 - a malformed target is "unknown", not a fault
                return None
        return None

    def present_joints(self) -> list[float]:
        """The nine joint positions in ``JOINT_NAMES`` order, radians.

        The antennas are ``[right, left]``, Pollen's wire order.  MuJoCo stores them
        mirrored (``[left, right]``, sign-flipped) and negates on the way in and out, so
        its readings are self-consistent with its own commands; the simulator is
        left-right mirrored against the real robot, which is Pollen's quirk and not one
        worth "fixing" here - correcting it would make the sim disagree with the daemon
        that every other Reachy tool talks to.
        """

        with self._lock:
            head = list(self._head_joints) if self._head_joints else None
            antennas = list(self._antennas) if self._antennas else None
        if head is None or antennas is None:
            head, antennas = self._read_joints_from_backend()
        joints = list(head[:7]) + list(antennas[:2])
        # A backend that has not started yet reports nothing; pad rather than publish a
        # JointState whose name and position arrays disagree in length, which is a
        # message every ROS consumer is entitled to treat as corrupt.
        while len(joints) < len(JOINT_NAMES):
            joints.append(0.0)
        return [float(v) for v in joints[:len(JOINT_NAMES)]]

    def present_antennas(self) -> tuple[float, float]:
        """``(right, left)`` in radians."""

        joints = self.present_joints()
        return joints[7], joints[8]

    def present_body_yaw(self) -> float:
        """Body yaw in radians (the first head joint)."""

        return self.present_joints()[0]

    def motor_mode(self) -> str:
        """``enabled`` | ``disabled`` | ``gravity_compensation``, as the daemon spells it."""

        if self.backend is None:
            return self._motor_mode
        try:
            mode = self.backend.get_motor_control_mode()
        except Exception:  # noqa: BLE001 - a backend that cannot say is not an error
            return self._motor_mode
        self._motor_mode = str(getattr(mode, "value", mode))
        return self._motor_mode

    def is_ready(self) -> bool:
        """Is the 50 Hz control loop actually turning RIGHT NOW?

        ``backend.ready`` alone is not that question.  It is a ``threading.Event`` the
        backend sets once, at the top of ``run()``, and nothing ever clears it - so a
        control loop that died an hour ago still reads ready, the REST shim still
        answers ``state: running``, /diagnostics still says OK, and the robot has not
        moved since.  Four conditions, because the loop can end four ways:

        * ``ready`` - it never started at all;
        * ``should_stop`` - something asked it to stop (our own ``close``, or Pollen);
        * ``backend.error`` - ``wrapped_run`` caught an exception, recorded it, called
          ``close()`` and re-raised (``daemon/backend/abstract.py``:435-442), which is
          the only trace a crashed control loop leaves inside the process;
        * the thread - the belt to those braces, and the one that catches a death no
          layer of Pollen booked anywhere.
        """

        backend = self.backend
        thread = self._thread
        return bool(
            backend is not None
            and backend.ready.is_set()
            and not backend.should_stop.is_set()
            and not getattr(backend, "error", None)
            and thread is not None
            and thread.is_alive()
        )

    def error(self) -> str:
        """What ``wrapped_run`` recorded when it died, or ``""``.

        Also answers for the two silent deaths Pollen books nowhere: a thread that
        ended without an exception, and one that was told to stop by something other
        than our own ``close()``.  A reader of ``/api/daemon/status`` needs a sentence,
        not an empty string, whichever way the loop ended.
        """

        backend = self.backend
        if backend is None:
            return "the backend is not running"
        recorded = getattr(backend, "error", None)
        if recorded:
            return str(recorded)
        if backend.should_stop.is_set():
            return "the backend control loop was asked to stop"
        if not backend.ready.is_set():
            return "the backend control loop has not become ready"
        thread = self._thread
        if thread is not None and not thread.is_alive():
            return "the backend control-loop thread is no longer running"
        return ""

    def message_counts(self) -> tuple[int, int]:
        """``(joint messages, pose messages)`` seen so far - the diagnostics rate source."""

        with self._lock:
            return self._joint_messages, self._pose_messages

    def mujoco_scene(self) -> tuple[Any, Any]:
        """``(model, data)`` for the sim camera, or ``(None, None)`` on any other backend."""

        backend = self.backend
        model, data = getattr(backend, "model", None), getattr(backend, "data", None)
        return (model, data) if model is not None and data is not None else (None, None)

    def _read_joints_from_backend(self) -> tuple[Sequence[float], Sequence[float]]:
        if self.backend is None:
            return [], []
        try:
            head = list(self.backend.get_present_head_joint_positions())
            antennas = list(self.backend.get_present_antenna_joint_positions())
        except Exception:  # noqa: BLE001 - before the first tick there is nothing to read
            return [], []
        return head, antennas

    # --- logging ------------------------------------------------------------
    def _info(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "info", print)(message)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "warn", getattr(self._log, "warning", print))(message)


class RecordedMovesCache:
    """One ``RecordedMoves`` library per dataset, loaded once and kept.

    ``RecordedMoves`` re-reads and re-parses every JSON in the dataset on construction,
    and the daemon's own router builds a fresh one **per request** - which is affordable
    when a request is a human pressing a button and is not when a behaviour graph plays
    a gesture a second.  So we cache, and we warm the cache at startup.

    Offline behaviour is Pollen's: ``RecordedMoves`` asks the HuggingFace cache first
    (``local_files_only=True``) and only falls back to the network if the dataset is not
    there.  We do not set ``HF_HUB_OFFLINE`` - the daemon does not either - because that
    would turn a first run on a fresh machine into a hard failure instead of a slow one.
    Pre-loading at startup is what makes the fallback never fire in practice.

    The instance is callable, so it can be handed straight to
    ``MotionArbiter(recorded_moves_factory=...)``.
    """

    def __init__(self, default_dataset: str = DEFAULT_EMOTIONS_DATASET,
                 loader: Callable[[str], Any] | None = None, log: Any = None) -> None:
        self.default_dataset = default_dataset or DEFAULT_EMOTIONS_DATASET
        self._loader = loader
        self._log = log
        self._lock = threading.Lock()
        self._libraries: dict[str, Any] = {}

    def __call__(self, dataset: str = "") -> Any:
        return self.get(dataset)

    def get(self, dataset: str = "") -> Any:
        """The library for ``dataset`` (empty = the default), loading it if needed."""

        name = (dataset or "").strip() or self.default_dataset
        with self._lock:
            library = self._libraries.get(name)
        if library is not None:
            return library

        library = self._load(name)
        with self._lock:
            # Another thread may have won the race; keep whichever landed first so two
            # callers never hold different objects for the same dataset.
            library = self._libraries.setdefault(name, library)
        return library

    def list_moves(self, dataset: str = "") -> list[str]:
        return list(self.get(dataset).list_moves())

    def has(self, dataset: str, name: str) -> bool:
        """Whether that move exists - what the REST shim needs to answer 404 rather than 500."""

        try:
            return name in set(self.list_moves(dataset))
        except Exception as error:  # noqa: BLE001 - an unknown dataset is a "no", not a crash
            self._warn(f"could not list moves in {dataset!r}: {error}")
            return False

    def preload(self, dataset: str = "") -> bool:
        """Warm the cache at startup.  Returns False instead of raising: a node that
        cannot reach HuggingFace must still come up and move the robot."""

        name = (dataset or "").strip() or self.default_dataset
        try:
            count = len(self.list_moves(name))
        except Exception as error:  # noqa: BLE001 - network or cache miss, not fatal
            self._warn(f"could not preload {name!r}: {error}; recorded moves will be "
                       "loaded on first use and the first one may be slow")
            return False
        self._info(f"preloaded {count} recorded moves from {name}")
        return True

    def _load(self, dataset: str) -> Any:
        loader = self._loader
        if loader is None:
            from reachy_mini.motion.recorded_move import RecordedMoves
            loader = RecordedMoves
        return loader(dataset)

    def _info(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "info", print)(message)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "warn", getattr(self._log, "warning", print))(message)


def _hardware_config_path(configured: str) -> str | None:
    """The motor config the real robot boots with: Pollen's own unless told otherwise.

    Pollen's daemon passes ``assets/config/hardware_config.yaml`` by DEFAULT
    (``daemon/app/main.py``, ``--hardware-config-filepath``), and that file is where the
    per-motor PID gains live - P 300 on the six Stewart servos, 200 on the body and the
    antennas. v2 passed nothing, so ``RobotBackend`` wrote no gains and every servo held
    on its firmware default after a power cycle. Measured on a robot: the holding head
    then ticks at ~26 Hz, loud enough to mask a learner at 80 cm (a voice-activity detector
    scored > 0.5 on 5-13% of speech frames with torque on, 72% with it off).

    ``none`` (any case) is the explicit opt-out; any other value is a path and is used as
    given, exactly like the daemon's flag.
    """

    value = (configured or "").strip()
    if value.lower() == "none":
        return None
    if value:
        return value
    try:
        import reachy_mini  # noqa: PLC0415 - only present where a real robot can be
    except Exception:  # noqa: BLE001 - no SDK, no bundled config: firmware defaults
        return None
    bundled = Path(reachy_mini.__file__).resolve().parent / "assets" / "config" / "hardware_config.yaml"
    return str(bundled) if bundled.is_file() else None
