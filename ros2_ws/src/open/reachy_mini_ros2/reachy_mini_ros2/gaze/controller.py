"""What the head is actually told to do: clamp, slew, deadband, hold, relax.

The geometry says where the face is; this says how the head is allowed to get there,
and what happens when the face goes away. It is the whole difference between a
controller and an open loop with a delay, so it is pure - numpy and ``conversions.py``,
no ROS, no onnxruntime, no GStreamer, no relative import - and every rule in it is a row
in ``tests/test_gaze_controller.py``.

Three decisions live here and nowhere else:

* **Gaze authors no roll.** It carries the COMMANDED roll straight through. Zeroing it
  would flatten the expression lane's authored tilt 200 ms after every cue settles
  (``repertoire.py`` authors roll in nine places); taking it from the MEASURED pose
  would command the head into whatever is deflecting it - a human's hand - and drive
  the ``measured - commanded`` residual on the roll axis to zero, which is precisely the
  signal the host application's push-turn detector reads. Carrying the command is a
  no-op by construction: gaze writes back exactly what the last author asked for.
* **The slew anchor advances only on an ACCEPTED write.** A refusal that advanced it
  would spend the step budget on a move that never happened, and the head would lurch by
  the accumulated amount on the first tick that was accepted.
* **A face that was never seen is never written to.** Not "written to the identity" -
  not written to at all. Otherwise gaze would undo ``start_session``'s deliberate -14 deg
  look-up, in an empty room, at the moment when nothing else is working either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

#: How long after the last accepted write `gaze_active` stays true. One second, so the
#: flag reads as "gaze is driving the head" rather than flickering with the 5 Hz tick.
GAZE_ACTIVE_WINDOW_S = 1.0

#: The counter `on_outcome` names for an accepted write; the refusal codes get
#: `gaze_skipped_<code>`. Named here because /diagnostics and the runbook read these
#: exact strings.
COUNTER_TARGETS = "gaze_targets"
COUNTER_DEADBAND = "gaze_deadband"
COUNTER_SKIPPED_PREFIX = "gaze_skipped_"

#: The other reason `limit` returns None: nobody can say where the head is, so there is
#: no honest anchor to slew from. Kept OFF `gaze_deadband` on purpose - the two look
#: identical to the caller and mean opposite things to an operator ("the head is already
#: right" versus "gaze is blind"), and an operator reads these counters to tell a still
#: face from a room with nothing in it.
COUNTER_SKIPPED_NO_ANCHOR = COUNTER_SKIPPED_PREFIX + "no_anchor"

#: The follower's smallest step. Without a floor an eased approach halves the error for
#: ever; 3 mrad is a sixth of a degree, below anything a person reads as motion.
_MIN_SERVO_STEP_RAD = 0.003


@dataclass
class GazeParams:
    """Every knob the component and the controller share, in one object.

    Defaults are the shipped ones; ``robot_node`` declares each as a ROS parameter of
    the same name under ``gaze.`` and builds this from them, so the table in the docs,
    the launch arguments and this dataclass cannot drift apart silently.
    """

    enabled: bool = False
    model_path: str = "/opt/emori/models/face_detection_yunet_2026may.onnx"
    # 5 Hz is chosen against the TRACKER, not the CPU: `max_misses` 20 at 5 Hz is 4.0 s,
    # the same number as `relax_s`, so the lock is released at the same moment the head
    # starts relaxing. Lowering this silently changes the loss behaviour too.
    detect_hz: float = 5.0
    score_threshold: float = 0.6
    select_score: float = 0.75
    nms_threshold: float = 0.3
    min_area_frac: float = 0.003
    max_jump: float = 0.5
    max_misses: int = 20
    switch_margin: float = 1.35
    switch_ticks: int = 5
    id_ttl_s: float = 10.0
    # 2.0 deg. The adaptive filter already has a 0.02-normalised dead zone on the image
    # centre, which at 320 px and fx = 139.0 is ~0.023 rad of joint angle; this is that
    # plus margin for detector jitter on a still face. Its job is to keep `gaze_targets`
    # and the motor duty cycle honest - NOT to keep the push-turn sensor quiet, which is
    # what residual sensing is for.
    deadband_rad: float = 0.025
    # 0.05 rad per tick at 5 Hz is 0.25 rad/s, about 14 deg/s: ten control-loop steps of
    # 5 mrad each, which is smooth on the XL330s and reads as a head following a person
    # rather than snapping to one.
    max_step_rad: float = 0.05
    # 25.8 deg. `look_around` sweeps 18 deg and the frame edge asks for 41 deg at the
    # corrected fx, so this stops a false detection at the edge throwing the head to a
    # mechanical limit.
    max_yaw_rad: float = 0.45
    # 20 deg; the widest add-on feedback pitch is 9 deg.
    max_pitch_rad: float = 0.35
    # The first of the two quiet windows after a face is lost. Behaviourally inert on
    # the head - nothing is written before `relax_s` either way - and named anyway,
    # because it is the boundary an operator reads `gaze_lost_s` against: under
    # `hold_s` the gap is a blink or a turn, past it the person may really be gone.
    hold_s: float = 1.5
    relax_s: float = 4.0
    settle_after_move_s: float = 0.0
    # Only the robot can settle a MECHANISM sign. `EMORI_GESTURE_INVERT` is the precedent:
    # the geometry is measured and certain, the wiring of nine servos is not.
    invert_yaw: bool = False
    sensor_full_width_px: int = 4608
    fx_scale: float = 1.0
    fy_scale: float = 1.0
    # `ros:gaze`, not `gaze`: the arbiter reads everything before the colon as the
    # expression OWNER, so the sub-client inherits the ROS graph's rights while its
    # accepts and refusals get their own lines on /diagnostics.
    client: str = "ros:gaze"
    lane: str = "expression"
    # Speech moves are rendered AROUND the face, not around neutral: while gaze holds a
    # face, every other author's head pose is composed onto the aim (see
    # `MotionArbiter.set_pose_anchor`). Without it the robot either looks at the learner
    # and stands still (every talking beat refused gaze `busy`) or moves and looks at the
    # wall (the beats are authored about yaw 0).
    anchor_moves: bool = True
    # The follower. `detect_hz` is 5 Hz on the robot, so one `max_step_rad`
    # jump per detection would make a head that lurches 5x a second.
    # Detection only moves the GOAL, and a `servo_hz` loop glides the head
    # towards it: each step is `ease` of the remaining error, never more than
    # `max_speed_rad_s` and never less than a hair, so it starts brisk and lands soft.
    # 0 disables the follower and restores the one-write-per-detection behaviour.
    servo_hz: float = 25.0
    max_speed_rad_s: float = 1.0
    ease: float = 0.3
    # How long a face aim stays good enough to aim a gesture at. One `hold_s`: past it
    # the person may really have left, and a gesture should go back to neutral.
    anchor_ttl_s: float = 1.5
    # How much of that aim a Pollen RECORDED move gets: yaw | full | off. Yaw only,
    # because the emotions library already spends its full pitch range: measured over
    # its 73 pickable moves at an aim of (-0.35, +/-0.20), composing the
    # pitch too puts 16% of frames out of reach (freeze-then-snap) against 6.1-6.6% for
    # yaw only and 6.0% with no anchor at all. `MotionArbiter.set_pose_anchor` reads it.
    anchor_recorded: str = "yaw"


def _load_conversions() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """``(matrix_to_rotation_rpy, xyzrpy_to_matrix)`` from ``conversions.py``.

    Same shape as ``rest_shim._conversions()`` and for the same reason: this module is
    loaded BY PATH by its test (``ros2_ws`` is not on ``sys.path``, and putting it there
    would need ROS), so a package-relative import would make the test impossible.

    The fallback loads the sibling FILE rather than re-deriving the arithmetic.
    ``conversions.py`` says of itself that it is "deliberately the ONLY place the three
    meet", and a second copy of its gimbal-lock guard living here would be exactly the
    bug that module exists to prevent - a head that tilts the wrong way, silently, only
    on hardware.
    """

    import importlib

    for candidate in ("reachy_mini_ros2.conversions",):
        try:
            module = importlib.import_module(candidate)
            return module.matrix_to_rotation_rpy, module.xyzrpy_to_matrix
        except Exception:  # noqa: BLE001 - not installed as a package is the host case
            pass

    import importlib.util
    import pathlib
    import sys

    path = pathlib.Path(__file__).resolve().parent.parent / "conversions.py"
    name = "reachy_mini_ros2_conversions_by_path"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module.matrix_to_rotation_rpy, module.xyzrpy_to_matrix


_matrix_to_rotation_rpy, _xyzrpy_to_matrix = _load_conversions()


class GazeController:
    """One face observation in, at most one bounded head target out."""

    def __init__(self, params: GazeParams) -> None:
        self._params = params
        # The last ACCEPTED target, as (pitch, yaw). None means "no anchor": the next
        # tick anchors on whatever pose is currently commanded, which is what makes
        # `reanchor()` mean "resume from wherever the head actually is now".
        self._last_sent: tuple[float, float] | None = None
        self._pending: tuple[float, float] | None = None
        # None until a face has been locked at least once. While it is None nothing is
        # ever written - see the module docstring.
        self._last_seen: float | None = None
        self._last_accepted: float = -1e9
        self._rest_pose: np.ndarray | None = None
        self._relaxed = True
        self._written = False
        # The last roll anybody COMMANDED, carried when the backend cannot say what it
        # is commanding right now. Emitting 0.0 there would flatten the expression
        # lane's authored tilt, which is the one thing the roll carry exists to prevent.
        self._last_roll = 0.0
        # Which counter the last `limit` that returned None wants bumped.
        self._skip_counter = COUNTER_DEADBAND

    # --- the run flag -------------------------------------------------------
    def set_enabled(self, enabled: bool, commanded_pose: Any = None) -> None:
        """Arm or disarm the controller, capturing the pose to relax back to.

        The rest pose is the head pose that was COMMANDED at the instant gaze was
        switched on: in a training session that is ``start_session``'s -14 deg look-up,
        in an add-on mode it is the mode's feedback pose. Relaxing to the identity
        instead would make gaze a second author of the robot's neutral, which is the host
        application's job (``_recenter_head``, pitch = +4 deg).

        Disarming keeps the anchor and the rest pose, so ``go_quiet`` walks the head
        back once rather than abandoning it mid-turn - but ONLY if gaze has actually
        moved it. A controller that never wrote must not write on its way out: the head
        would be somebody else's by then, and the pose it was armed with is stale.
        """

        if enabled:
            self._rest_pose = (None if commanded_pose is None
                               else np.asarray(commanded_pose, dtype=float).reshape((4, 4)).copy())
            self._relaxed = False
            self._last_seen = None
            self._last_sent = None
            self._pending = None
            self._written = False
            self._last_roll = 0.0
            self._skip_counter = COUNTER_DEADBAND
            return

        self._last_seen = None
        self._pending = None
        if not self._written:
            self._relaxed = True

    # --- the aim -------------------------------------------------------------
    def aim(self, target: Any) -> tuple[float, float]:
        """Where the head WANTS to point for this observation, as ``(pitch, yaw)``.

        The same sign and clamp as `limit`, and none of its state: no slew, no deadband,
        no anchor. This is the goal the slew walks towards, which is what a speech
        gesture should be centred on - a slew that is half done is not where the person
        is.
        """

        _roll, pitch, yaw = _matrix_to_rotation_rpy(np.asarray(target, dtype=float))
        if self._params.invert_yaw:
            yaw = -yaw
        return (float(np.clip(pitch, -self._params.max_pitch_rad, self._params.max_pitch_rad)),
                float(np.clip(yaw, -self._params.max_yaw_rad, self._params.max_yaw_rad)))

    # --- the limiter --------------------------------------------------------
    def limit(self, target: Any, commanded: Any = None, *,
              invert: bool = True, servo_dt: float | None = None) -> np.ndarray | None:
        """Roll carry, clamp, slew and deadband.  ``None`` means "write nothing".

        ``invert`` is what tells a GEOMETRY-derived target from a pose that is already
        in mechanism space.  ``invert_yaw`` is a mechanism fact:
        it corrects the sign of an aim computed from pixels.  ``go_quiet`` hands this
        the pose the head was COMMANDED with, which has already been through whatever
        sign the mechanism has - negating that would walk the relax to the mirror image
        of the rest pose, so every session and every add-on mode would end with the
        head parked at -yaw instead of where it started.
        """

        _roll, pitch, yaw = _matrix_to_rotation_rpy(np.asarray(target, dtype=float))
        if invert and self._params.invert_yaw:
            yaw = -yaw

        roll_cmd = self._last_roll
        # The anchor is the pose the backend is commanding RIGHT NOW, whenever it will
        # say. Not `_last_sent`: that is only what gaze itself wrote last, and the head
        # may have been moved since by a writer gaze never saw. Measured on a robot: a
        # 0.4 s recentring GoTo landed between two 3 Hz detections, the
        # falling-edge poll never saw `head_moving`, and the next write slewed from the
        # stale anchor -- a 0.4 rad slam at 1.8 rad/s and a 0.5 rad residual, i.e. what
        # residual push-turn sensing would read as a human hand. Anchoring on the
        # command makes every other author's move the new starting point for free;
        # `_last_sent` is only the fallback for a backend that cannot report a command.
        anchor = None
        if commanded is not None:
            cmd = np.asarray(commanded, dtype=float).reshape((4, 4))
            roll_cmd, cmd_pitch, cmd_yaw = _matrix_to_rotation_rpy(cmd)
            self._last_roll = float(roll_cmd)
            anchor = (float(cmd_pitch), float(cmd_yaw))
        if anchor is None:
            anchor = self._last_sent
        if anchor is None:
            # No accepted write to slew from AND a backend that will not say what it is
            # commanding. Assuming the head is at neutral would jump it to within one
            # step of neutral from wherever it actually is; skipping the tick costs one
            # 200 ms cycle and is the only honest answer.
            self._pending = None
            self._skip_counter = COUNTER_SKIPPED_NO_ANCHOR
            return None

        pitch = float(np.clip(pitch, -self._params.max_pitch_rad,
                              self._params.max_pitch_rad))
        yaw = float(np.clip(yaw, -self._params.max_yaw_rad, self._params.max_yaw_rad))

        d_pitch, d_yaw = pitch - anchor[0], yaw - anchor[1]
        if max(abs(d_pitch), abs(d_yaw)) < self._params.deadband_rad:
            # The head is already where this observation would put it. Nothing to send,
            # and nothing to advance: `_last_sent` must keep meaning "the last target
            # the arbiter took".
            self._pending = None
            self._skip_counter = COUNTER_DEADBAND
            return None

        if servo_dt is None:
            step = self._params.max_step_rad
        else:
            # One follower tick: a fraction of what is left, capped by the speed limit,
            # floored so the last degree is not approached for ever.
            error = max(abs(d_pitch), abs(d_yaw))
            step = min(self._params.max_speed_rad_s * float(servo_dt),
                       max(self._params.ease * error, _MIN_SERVO_STEP_RAD))
        pitch = anchor[0] + float(np.clip(d_pitch, -step, step))
        yaw = anchor[1] + float(np.clip(d_yaw, -step, step))

        self._pending = (pitch, yaw)
        return _xyzrpy_to_matrix(0.0, 0.0, 0.0, roll_cmd, pitch, yaw)

    # --- what the arbiter answered ------------------------------------------
    def on_outcome(self, accepted: bool, code: str, now: float) -> str:
        """Apply the refusal policy and name the counter the caller should bump.

        The policy, in one place because getting it wrong is invisible until the head
        lurches: an accept commits the slew anchor; ``busy`` does NOT (the head did not
        go where we asked, so spending the step budget on it would be a lie that is paid
        back all at once on the next accepted tick); ``head_reserved`` re-anchors,
        because a human is holding the head and the next accepted tick must slew from
        wherever they left it.
        """

        if accepted:
            self.commit(now)
            return COUNTER_TARGETS
        refusal = code or "unknown"
        if refusal == "head_reserved":
            self.reanchor()
        return COUNTER_SKIPPED_PREFIX + refusal

    def commit(self, now: float) -> None:
        """The arbiter took the last limited target: advance the slew anchor."""

        if self._pending is not None:
            self._last_sent = self._pending
            self._pending = None
            self._written = True
        self._last_accepted = float(now)

    def reanchor(self) -> None:
        """Forget the slew anchor; the next tick anchors on the commanded pose."""

        self._last_sent = None
        self._pending = None

    # --- seeing, and not seeing ---------------------------------------------
    def note_seen(self, now: float) -> None:
        """A face was observed on this tick."""

        self._last_seen = float(now)
        self._relaxed = False

    def handle_loss(self, now: float, commanded: Any = None) -> np.ndarray | None:
        """The target for a tick with no face, or ``None`` to write nothing.

        | elapsed                  | behaviour                                     |
        |--------------------------|-----------------------------------------------|
        | never locked             | nothing, ever - the no-face case must not move |
        |                          | the head at all                                |
        | < `relax_s` (4.0 s)      | nothing; the tracker still holds its lock, so  |
        |                          | a re-acquisition resumes with no transient     |
        | >= `relax_s`             | slew to `_rest_pose` through the same limiter  |
        | rest reached             | nothing until the next face                    |

        A head left staring at where somebody used to be is the creepiest failure
        available, which is why the relax exists; a head that relaxes after 1.5 s is a
        head that gives up on a person who blinked, which is why it waits 4.0 s.
        """

        if self._last_seen is None or self._relaxed:
            return None
        if (now - self._last_seen) < self._params.relax_s:
            return None
        return self.go_quiet(commanded)

    def relax_goal(self, now: float) -> np.ndarray | None:
        """The follower's goal for a tick with no face: the rest pose once `relax_s` has
        passed since the last sighting, else None (hold). Same table as `handle_loss`."""

        if self._last_seen is None or self._relaxed:
            return None
        if (now - self._last_seen) < self._params.relax_s:
            return None
        if self._rest_pose is None:
            self._relaxed = True
            return None
        return self._rest_pose.copy()

    def mark_relaxed(self) -> None:
        """The follower reached the rest pose: nothing more to write until a face."""

        self._relaxed = True

    def go_quiet(self, commanded: Any = None) -> np.ndarray | None:
        """One step of the walk back to the rest pose, or ``None`` when there is none.

        Used both by the relax after a lost face and by the run flag going false: a
        disabled detector must not leave the head parked on somebody's empty chair.
        """

        if self._relaxed:
            return None
        if self._rest_pose is None:
            # Gaze was armed by a backend that could not say what it was commanding.
            # Relaxing to a guessed neutral would make gaze a second author of the robot's
            # neutral; going quiet is the honest degradation.
            self._relaxed = True
            return None
        # invert=False: `_rest_pose` is a pose the robot was already COMMANDED, not an
        # aim derived from pixels, so the mechanism sign has been applied to it once
        # already and applying it again would relax to the mirror image.
        target = self.limit(self._rest_pose, commanded, invert=False)
        if target is None:
            # Either the head is at rest (the deadband) or nobody can say where it is
            # (no anchor, no command). Both end the walk: continuing to write into the
            # second case would be guessing at a pose, which is what `_rest_pose is
            # None` above already refuses to do.
            self._relaxed = True
        return target

    # --- observation --------------------------------------------------------
    def is_active(self, now: float) -> bool:
        """True while a target was accepted within the last second."""

        return (now - self._last_accepted) < GAZE_ACTIVE_WINDOW_S

    @property
    def skip_counter(self) -> str:
        """Which counter the last ``limit`` that returned ``None`` wants bumped.

        ``limit`` returns ``None`` for two unrelated reasons and the caller cannot tell
        them apart from the return value alone, so the controller names the counter -
        exactly as ``on_outcome`` does for a refusal. Conflating them would let a
        backend that never reports a commanded pose inflate ``gaze_deadband`` at
        ``detect_hz`` for ever while reading as "the head is already right".
        """

        return self._skip_counter

    def lost_for(self, now: float) -> float:
        """Seconds since the last observation; 0.0 before the first lock."""

        return 0.0 if self._last_seen is None else max(0.0, now - self._last_seen)

    @property
    def rest_pose(self) -> np.ndarray | None:
        """The pose the head returns to when the face is gone for good."""

        return None if self._rest_pose is None else self._rest_pose.copy()

    @property
    def has_seen_a_face(self) -> bool:
        return self._last_seen is not None
