"""The motion arbiter: one robot, several things that want to move it, one answer.

In v1 all of this lived inside the host application's ``RobotPlayer`` - one queue, one
thread, a ``busy`` flag, ``drop_if_busy``, generation guards, a ``head_reserved`` gate
and an add-on ownership check.  That worked while the host application was the only
writer.  With the robot driven natively from ROS there are two askers (the ROS graph
and, through the REST compatibility shim, the host application itself), so the
arbitration has to move to where the robot is.  This module is that move: the same
rules, in one place, with every refusal given a name a caller can act on.

What it is not: a scheduler that tries to be clever.  One move runs at a time, on one
asyncio loop, in one thread.  Pollen's ``Backend`` enforces that anyway - ``play_move``
takes ``_play_move_lock`` and simply *logs and returns* if a second move arrives, which
would look exactly like a move that succeeded instantly - so the arbiter never starts
two, and cancellation is ``Task.cancel()``, which is precisely what the daemon's own
stop endpoint does.

Pure Python: ``asyncio`` + ``numpy`` + the standard library.  No ROS, and no hard
dependency on ``reachy_mini`` either (the two enums it would import are resolved
lazily and degrade to the plain strings they already are), so the whole decision
surface is loadable by path and tested on a machine with neither installed -
``tests/test_reachy_arbiter.py``.

Priority, high to low: ``safety`` > ``interaction`` > ``expression``.  Safety outranks
everything on purpose: a calm-settle when a learner is distressed is not an expression
and must not queue behind one.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Sequence
import uuid

import numpy as np

# Priority order, high to low. The tuple IS the ordering; do not sort it elsewhere.
LANES = ("safety", "interaction", "expression")

# Every way a request can be turned down. This vocabulary is shared on purpose: the
# REST shim maps it to HTTP codes, the actions return it in `error_code`, and the
# EMORI skill facade already speaks `dropped` / `not_owner` / `head_reserved` from the
# host application's own `/api/skill` contract, so a refusal reads the same on both
# sides of the boundary.
REFUSALS = ("bad_lane", "not_owner", "head_reserved", "dropped", "queue_full", "busy",
            "backend_error")

# How a move that DID start can end. "" means it ran to its end.
#
# `motors_disabled` is the one that is not a fault: the motors were not under position
# control when the move came up to run, so the backend would have written the whole
# trajectory into the void and reported success. Pollen's `goto_sleep` ends with
# `set_motor_control_mode(Disabled)` (daemon/backend/abstract.py:1315), so a robot that
# was told to sleep is in exactly that state until something enables it again - and a
# cached "enabled" is what makes the next `goto` look like it worked.
OUTCOMES = ("", "cancelled", "preempted", "backend_error", "timeout", "motors_disabled")

# The kinds of work the arbiter knows how to drive on the backend.
MOVE_KINDS = ("goto", "set_target", "wake_up", "goto_sleep", "play_recorded")
# How long `head_moving` stays true after the LAST thing that moved the head: an accepted
# head `set_target`, or a head move finishing. The servos do not stop when the command
# does -- measured on a robot, a 0.05 rad gaze step or a finished nod leaves a 0.1-0.26
# rad measured-minus-commanded transient for a few hundred milliseconds, and without this
# window the host application's residual push-turn sensor reads those as a hand on the
# head, reserves the head, and refuses most engagement gestures. A settle
# window is the honest shape of "the robot is still moving itself".
HEAD_SETTLE_S = 0.25

# Pollen's motor control modes, spelled as the daemon spells them on the wire.
MOTOR_MODES = ("enabled", "disabled", "gravity_compensation")

# Pollen's interpolation techniques (`reachy_mini.utils.interpolation`).
INTERPOLATIONS = ("linear", "minjerk", "ease_in_out", "cartoon")
DEFAULT_INTERPOLATION = "minjerk"

# Who may use the expression lane. Same three values, same fallback, as
# EMORI_ROS_MOTION_OWNER and `emori_skill_facade.contract.normalize_owner`: an
# unrecognised value reads as `hostapp`, because the fail-closed direction for a knob
# that decides who moves a robot is "not the new thing".
EXPRESSION_OWNERS = ("hostapp", "ros", "none")
DEFAULT_EXPRESSION_OWNER = "hostapp"

# The default client name, matching the host application's identity through the shim.
DEFAULT_CLIENT = "hostapp"
DEFAULT_LANE = "interaction"

# An expression request is dropped once this many requests are already waiting behind a
# running move. It is the host application's `drop_if_busy` rule, to the number: a third
# queued expression is not more expressive, it is a stale one that will land seconds
# after the moment it was about.
EXPRESSION_BACKLOG_LIMIT = 2
FIRST_MOTION_HEAD_RAD = float(np.radians(1.0))
FIRST_MOTION_ANTENNA_RAD = float(np.radians(3.0))
# A recording eases in from where the head is, and the ease is a slow start: 0.3 s to close a
# 2 degree gap shows nothing for 150 ms. So it takes no longer than these rates ask for.
BLEND_HEAD_DEG_PER_S = 60.0
BLEND_ANTENNA_DEG_PER_S = 200.0
BLEND_MIN_S = 0.08
# A head the motors cannot reach: how finely the share of its translation that fits is found, and
# how far inside the boundary it then stays.
HEAD_FIT_STEPS = 12
HEAD_FIT_MARGIN = 0.98
# A servo's position in steps: 4096 to the turn, and step 2048 is zero.
SERVO_ZERO_STEP = 2048
SERVO_RAD_PER_STEP = 2.0 * np.pi / 4096.0
# How much of a recording's stillness stays in front of its first motion.
LEAD_IN_KEPT_S = 0.05
# What a superseding request may cut. A wake-up or a goto-sleep always finishes: the motors
# are changing state underneath them.
SUPERSEDABLE_KINDS = ("goto", "play_recorded")
# One record per acceptance, start and settlement. 8192 records hold about 2700 moves, which
# is more than one long session, and each is a short tuple kept in memory.
TRACE_CAPACITY = 8192
TRACE_FIELDS = ("seq", "t", "event", "move_id", "kind", "client", "lane", "reason", "error_code",
                "queued")

# How many finished moves stay answerable by `wait()` / `status()` before their ids are
# forgotten. Bounded because the arbiter runs for weeks on a robot.
_HISTORY = 256

# How a Pollen recorded move (the emotions library) is placed on the face gaze holds.
# `goto` and `set_target` are composed onto the aim; `play_recorded`
# handed the library object to Pollen untouched, so every `rec-*` cue played about
# neutral while the learner stood to one side - and snapped onto its first frame, since
# Pollen's `play_move` defaults to no initial goto. Values:
#   yaw  - compose the aim's YAW only (the default);
#   full - compose yaw and pitch, the same aim `goto` gets;
#   off  - play every recorded move as authored (still eased in; see below).
# Why yaw by default, measured on the emotions dataset (73 pickable moves, 4,497
# frames, Pollen's AnalyticalKinematics with automatic body yaw): 6.0% of frames are out
# of reach with no anchor at all, because the library already spends its full +/-22.5
# deg of pitch. At an aim of (-0.35, +/-0.20) - by geometry a standing adult at 80 cm
# likely sits on the -0.35 pitch clamp; the real aim is not measured yet, which is what
# the per-move INFO line is for - yaw only reaches 6.1-6.6%, full aim 16.0-16.3%. An
# unreachable frame is not an error: the head holds its last joints and snaps when a
# reachable one comes back, so full aim would read as freeze-then-snap. The cost of yaw
# only is a pitch dip of up to 20 deg as the move starts, which the ease-in below smooths
# and the host application's settle goto restores.
RECORDED_ANCHORS = ("yaw", "full", "off")
DEFAULT_RECORDED_ANCHOR = "yaw"
# The recorded-move ease-in, seconds; 0 turns it off. Measured with the head held on a
# face, the jump onto a move's first frame is a median 17.7 deg (p90 28.7, max 46.4)
# unanchored and still up to 30 deg anchored; eased as an OFFSET that decays to zero
# (min-jerk) the worst entry over the 73 moves is 2.30 deg per 10 ms tick, and the
# authored motion is kept exactly - 13 of those moves travel more than 3 deg in their
# own first 0.3 s (fear1 17, furious1 10.9), which a slerp from a fixed start towards the
# moving move would flatten. Not Pollen's `initial_goto_duration`: that adds a goto in
# FRONT of the move and delays every one of them against the voice-locked host app.
DEFAULT_RECORDED_BLEND_S = 0.3


def normalize_expression_owner(value: str | None) -> str:
    """``hostapp`` | ``ros`` | ``none``; anything else reads as ``hostapp``."""

    candidate = (value or "").strip().lower()
    return candidate if candidate in EXPRESSION_OWNERS else DEFAULT_EXPRESSION_OWNER


def owner_of(client: str) -> str:
    """The expression-owner identity a client name belongs to.

    A client may name a SUB-CLIENT after a colon (``ros:gaze``), which is what gives the
    per-client counters (``accepted_ros:gaze``) a separate line on /diagnostics without
    inventing a second ownership concept.  Everything before the colon is the owner; a
    name with no colon is unchanged, which is why ``hostapp`` and ``ros`` behave exactly
    as they did.
    """

    return (client or "").split(":", 1)[0].strip().lower() or DEFAULT_CLIENT


def _moves_head(req: "MotionRequest") -> bool:
    """Does this running request move the HEAD, as opposed to only the antennas?

    The same distinction an add-on's mode feedback draws with its own
    ``_moves_head``, for the same reason: an antenna-only batch cannot move the measured
    pose, so suppressing the host application's gesture sensor for one would disarm mode
    selection while the robot is merely giving phase feedback.
    """

    if req.kind == "goto":
        return req.head_pose is not None or req.body_yaw is not None
    return True          # play_recorded / wake_up / goto_sleep all move the head


@dataclass(frozen=True)
class MotionRequest:
    """One thing somebody wants the robot to do, and the context to judge it by.

    Frozen because a request that can be edited after it was judged is a request that
    was judged on something else.  Empty strings mean "the default": a ROS message
    whose ``lane`` field was never set says ``""``, and that is an unspecified lane,
    not a wrong one.
    """

    kind: str
    lane: str = DEFAULT_LANE
    client: str = DEFAULT_CLIENT
    head_pose: np.ndarray | None = None
    antennas: tuple[float, float] | None = None
    body_yaw: float | None = None
    duration: float = 0.5
    interpolation: str = DEFAULT_INTERPOLATION
    dataset: str = ""
    move: str = ""
    preempt: bool = False
    reason: str = ""
    # The newest thing this client said replaces what it said before: its running goto or
    # recording is cut, its queued work is flushed, and this goes next. `preempt` above is
    # one lane against another; this is one client against itself, because a robot that
    # finishes the last sentence's gesture before starting this one's is late by that long.
    supersede: bool = False

    def __post_init__(self) -> None:
        # Normalising HERE rather than in three callers is the point: the shim, the
        # action servers and the native transport all hand us partly-filled requests.
        object.__setattr__(self, "lane", (self.lane or DEFAULT_LANE).strip().lower())
        object.__setattr__(self, "client", (self.client or DEFAULT_CLIENT).strip())
        object.__setattr__(self, "kind", (self.kind or "").strip().lower())
        interpolation = (self.interpolation or DEFAULT_INTERPOLATION).strip().lower()
        if interpolation not in INTERPOLATIONS:
            interpolation = DEFAULT_INTERPOLATION
        object.__setattr__(self, "interpolation", interpolation)


@dataclass(frozen=True)
class Outcome:
    """The answer to a request: taken or not, why not, and what to call it.

    From ``submit`` ``accepted`` means "the arbiter took it"; from ``wait`` it means
    "the move ran to its end".  ``error_code`` is a member of ``REFUSALS`` in the first
    case and of ``OUTCOMES`` in the second.
    """

    accepted: bool
    error_code: str = ""
    move_id: str = ""


# eq=False: entries are compared and removed BY IDENTITY. A generated __eq__
# would compare the MotionRequest inside, whose `head_pose` is a numpy array,
# and `entry in self._queue` would then depend on array truthiness.
@dataclass(eq=False)
class _Entry:
    """One accepted request, from acceptance to settlement."""

    move_id: str
    req: MotionRequest
    done: threading.Event = field(default_factory=threading.Event)
    error_code: str = ""
    finished: bool = False
    started_at: float = 0.0
    cancel_reason: str = ""
    task: "asyncio.Task[None] | None" = None


def _interpolation_value(name: str) -> Any:
    """Pollen's ``InterpolationTechnique`` when it is installed, else the plain string.

    ``InterpolationTechnique`` is a ``str`` Enum, so its members compare equal to their
    own values and the backend cannot tell the two apart.  That is what lets this
    module - and therefore the whole arbiter test suite - run with no ``reachy_mini``
    installed at all.
    """

    value = name if name in INTERPOLATIONS else DEFAULT_INTERPOLATION
    try:
        from reachy_mini.utils.interpolation import InterpolationTechnique
    except Exception:  # noqa: BLE001 - absence is the expected case off-robot
        return value
    return InterpolationTechnique(value)


def _motor_mode_value(mode: str) -> Any:
    """Pollen's ``MotorControlMode`` when it is installed, else the plain string."""

    try:
        from reachy_mini.io.protocol import MotorControlMode
    except Exception:  # noqa: BLE001 - absence is the expected case off-robot
        return mode
    return MotorControlMode(mode)


class _MotorsDisabled(RuntimeError):
    """Raised inside ``_execute`` when the LIVE motor mode is not ``enabled``.

    Its own exception type rather than a return value because ``_execute`` is awaited by
    ``_run``, whose whole job is turning "what happened" into an outcome code; a
    ``backend_error`` here would say "the robot faulted" about a robot that is simply
    asleep.
    """


def _backend_name(backend: Any) -> str:
    """``mujoco`` | ``mockup`` | ``robot`` from the backend's class name."""

    name = type(backend).__name__.lower()
    for candidate in ("mujoco", "mockup", "robot"):
        if candidate in name:
            return candidate
    return name


# --- placing a recorded move on the learner -----------------------------------------
# Pure numpy on purpose: the module's no-import rule (see the purity test) holds here too.

def _aim_rotation(pitch: float, yaw: float) -> np.ndarray:
    """``Rz(yaw) @ Ry(pitch)``: the rotation a head pose is composed onto.

    The same extrinsic-XYZ convention as conversions.py, with no roll - gaze never
    authors roll, and the gesture's own tilt is kept.
    """

    sp, cp, sy, cy = np.sin(pitch), np.cos(pitch), np.sin(yaw), np.cos(yaw)
    return np.array([[cy * cp, -sy, cy * sp],
                     [sy * cp, cy, sy * sp],
                     [-sp, 0.0, cp]])


def _min_jerk(a: float) -> float:
    """``a^3 (10 - 15a + 6a^2)`` on ``a`` clamped to [0, 1]: zero velocity at both ends."""

    a = min(1.0, max(0.0, float(a)))
    return a * a * a * (10.0 - 15.0 * a + 6.0 * a * a)


def _frames_first_motion_s(times: Any, frames: Any) -> float | None:
    """``_first_motion_s`` read off a recording's raw frames, or None if it has none.

    An interpolated frame costs about 6 ms on the robot, so a search through ``evaluate``
    would hold up the loop for hundreds of ms on the very move that has to start now. The
    raw frames cost microseconds each.
    """

    if not (isinstance(times, list) and isinstance(frames, list) and times
            and len(times) == len(frames)):
        return None
    try:
        first_head = np.asarray(frames[0]["head"], dtype=float)[:3, :3]
        first_antennas = np.asarray(frames[0]["antennas"], dtype=float)
        floor = float(np.cos(FIRST_MOTION_HEAD_RAD))
        for t, frame in zip(times, frames):
            head = np.asarray(frame["head"], dtype=float)[:3, :3]
            # (trace(R) - 1) / 2 is the cosine of the angle between the two rotations.
            if (float(np.sum(first_head * head)) - 1.0) / 2.0 <= floor:
                return float(t)
            antennas = np.asarray(frame["antennas"], dtype=float)
            if float(np.max(np.abs(antennas - first_antennas))) >= FIRST_MOTION_ANTENNA_RAD:
                return float(t)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return 0.0


def _first_motion_s(move: Any, *, step: float = 0.02, limit: float = 2.5) -> float:
    """When a recording first visibly moves: 1 degree of head or 3 of an antenna, or 0.0.

    A few recordings hold still for over a second before they do anything. Played from the
    top, the head stays still under the words it was meant to go with.
    """

    found = _frames_first_motion_s(getattr(move, "timestamps", None),
                                   getattr(move, "trajectory", None))
    if found is not None:
        return found
    first_head, first_antennas, _ = move.evaluate(0.0)
    if first_head is not None:
        first_head = np.asarray(first_head, dtype=float).reshape((4, 4))[:3, :3]
    if first_antennas is not None:
        first_antennas = np.asarray(first_antennas, dtype=float)
    end = min(limit, float(move.duration) - 1e-3)
    t = step
    while t < end:
        head, antennas, _ = move.evaluate(t)
        if first_head is not None and head is not None:
            turn = first_head.T @ np.asarray(head, dtype=float).reshape((4, 4))[:3, :3]
            if float(np.linalg.norm(_log_so3(turn))) >= FIRST_MOTION_HEAD_RAD:
                return t
        if first_antennas is not None and antennas is not None:
            if float(np.max(np.abs(np.asarray(antennas, dtype=float) - first_antennas))) \
                    >= FIRST_MOTION_ANTENNA_RAD:
                return t
        t += step
    return 0.0


def _log_so3(rotation: Any) -> np.ndarray:
    """The axis-angle vector of a 3x3 rotation: its axis, scaled by its angle.

    ``arctan2`` rather than ``arccos`` of the trace, which loses every digit near the
    identity - and "the head is already nearly on the first frame" is the common case.
    """

    r = np.asarray(rotation, dtype=float)
    twice_sin = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    norm = float(np.linalg.norm(twice_sin))
    if norm < 1e-12:
        # The identity - or 180 deg, where no arc is the shortest. No head pose is ever
        # the latter (the platform's whole range is a few tens of degrees), and "no
        # correction" there is exactly the snap every recorded move made before this.
        return np.zeros(3)
    angle = float(np.arctan2(norm / 2.0, (np.trace(r) - 1.0) / 2.0))
    return twice_sin * (angle / norm)


def _exp_so3(vector: Any) -> np.ndarray:
    """Rodrigues: the 3x3 rotation of an axis-angle vector (the inverse of ``_log_so3``)."""

    w = np.asarray(vector, dtype=float)
    angle = float(np.linalg.norm(w))
    if angle < 1e-12:
        return np.eye(3)
    k = w / angle
    skew = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + np.sin(angle) * skew + (1.0 - np.cos(angle)) * (skew @ skew)


def stewart_limits(motors: Any) -> tuple[np.ndarray, np.ndarray]:
    """``(low, high)``, the six Stewart servos' limits in radians, from Pollen's motor config."""

    rows = [motors[f"stewart_{number}"] for number in range(1, 7)]
    low = np.array([row.angle_limit_min for row in rows], dtype=float)
    high = np.array([row.angle_limit_max for row in rows], dtype=float)
    return ((low - SERVO_ZERO_STEP) * SERVO_RAD_PER_STEP,
            (high - SERVO_ZERO_STEP) * SERVO_RAD_PER_STEP)


def head_reach_check(joints_for: Callable[[Any, Any], Any],
                     limits: tuple[np.ndarray, np.ndarray]) -> Callable[[Any, Any], bool]:
    """``reachable(head, body_yaw)``: the IK takes this head, and every leg stays within its servo.

    ``joints_for`` gives the body yaw and the six Stewart joints, None or NaN when refused. The IK
    knows the geometry and not the servos, so it takes heads a leg's servo cannot follow. The
    limits are not optional: nearly every frame drawn in only as far as the IK allows lands past
    one, which is further from what the robot can do than the frame was.
    """

    low, high = limits

    def reachable(head: Any, body_yaw: Any) -> bool:
        joints = joints_for(head, body_yaw)
        if joints is None:
            return False
        joints = np.asarray(joints, dtype=float)
        if np.any(np.isnan(joints)):
            return False
        return bool(np.all(joints[1:] >= low) and np.all(joints[1:] <= high))

    return reachable


def _fit_into_reach(head: Any, body_yaw: Any, reachable: Callable[[Any, Any], bool]) -> Any:
    """``head``, with its translation drawn toward neutral until ``reachable`` accepts it.

    Only the translation changes, by a bisection on the share of it that is kept, then
    ``HEAD_FIT_MARGIN`` off the boundary. A head that does not fit at zero translation comes back
    as it went in, and so does one when ``reachable`` raises: the backend then treats it as it
    would have with no fit at all.
    """

    try:
        if reachable(head, body_yaw):
            return head
        fitted = np.array(head, dtype=float)
        translation = fitted[:3, 3].copy()
        fitted[:3, 3] = 0.0
        if not reachable(fitted, body_yaw):
            return head
        low, high = 0.0, 1.0
        for _ in range(HEAD_FIT_STEPS):
            middle = (low + high) / 2.0
            fitted[:3, 3] = translation * middle
            if reachable(fitted, body_yaw):
                low = middle
            else:
                high = middle
        fitted[:3, 3] = translation * low * HEAD_FIT_MARGIN
        return fitted
    except Exception:  # noqa: BLE001 - never worth the gesture
        return head


class _PlacedMove:
    """A Pollen recorded move, played around the face and eased in from where the head IS.

    Pollen's ``play_move`` duck-types its argument: it reads ``duration``, ``sound_path``
    and ``evaluate(t) -> (head 4x4, antennas (2,), body_yaw)`` and nothing else, and
    writes every evaluated frame straight to the targets at 100 Hz
    (daemon/backend/abstract.py:816-932, reachy_mini 1.10.0). So this wrapper is the whole
    integration, and four things happen in ``evaluate``:

    1. ``t`` is clamped to ``[0, duration - 1 ms]``. Pollen samples ``t`` AFTER its loop
       check, and ``RecordedMove.evaluate`` raises for ``t >= timestamps[-1]``
       (recorded_move.py:122-123) - all 8 real moves checked raise at ``duration + 2 ms``,
       which would end a move that played in full as ``backend_error``.
    2. The head's rotation is composed onto ``rotation`` (the aim, captured ONCE when the
       move started). Translation is left alone, as in the goto path.
    3. For ``t < blend_s``, an OFFSET decays from "the start state" to zero. It is the
       difference, measured once, between where the robot was commanded to be and the
       composed first frame; ``r = 1 - min_jerk(t / blend_s)`` of it is added on top of
       the moving move. At ``t = 0`` the output is exactly the start; from ``blend_s``
       on it is exactly the composed move; and in between the move keeps its own
       velocity - a start already on the first frame changes nothing at any ``t``.
    4. A head the motors cannot reach goes through ``_fit_into_reach``. The backend holds its last
       accepted target for a frame the IK refuses, and head and body yaw stand still while the
       antennas go on, and a goal past a servo's limit is one that servo does not follow. The
       emotions library pins translation at its limits, so some recordings leave the reachable
       region for most of their length. The rotation is what makes the gesture, so it stays as
       authored and the head sits a little lower or less far forward.
    """

    def __init__(self, inner: Any, rotation: Any = None, start: Any = None,
                 blend_s: float = 0.0, skip_s: float = 0.0,
                 head_reachable: Callable[[Any, Any], bool] | None = None) -> None:
        self._inner = inner
        self._head_reachable = head_reachable
        # The move as Pollen sees it starts this far into the recording.
        self._skip_s = max(0.0, float(skip_s))
        self._rotation = None if rotation is None else np.asarray(rotation, dtype=float)
        self._start = start
        self._blend_s = max(0.0, float(blend_s)) if start is not None else 0.0
        self._offset: tuple | None = None

    @property
    def duration(self) -> float:
        return self._inner.duration - self._skip_s

    @property
    def sound_path(self) -> Any:
        return getattr(self._inner, "sound_path", None)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this class does not define. Private names are never
        # forwarded: a half-built instance must not recurse through `self._inner`.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _composed(self, t: float) -> tuple[Any, Any, Any]:
        head, antennas, body_yaw = self._inner.evaluate(t + self._skip_s)
        if head is not None:
            head = np.array(head, dtype=float).reshape((4, 4))
            if self._rotation is not None:
                head[:3, :3] = self._rotation @ head[:3, :3]
        return head, antennas, body_yaw

    def entry_offset(self) -> tuple[Any, Any, Any, Any]:
        """``(w, dp, da, db)``: start minus the composed first frame, computed once.

        ``w`` is the rotation from the first frame to the start head as an axis-angle
        vector (world frame, so it is applied on the LEFT), ``dp`` the translation,
        ``da`` the antennas and ``db`` the body yaw. A part either side cannot say is
        None and is simply not eased.
        """

        if self._offset is None:
            w = dp = da = db = None
            if self._start is not None:
                start_head, start_antennas, start_body_yaw = self._start
                head, antennas, body_yaw = self._composed(0.0)
                if start_head is not None and head is not None:
                    w = _log_so3(start_head[:3, :3] @ head[:3, :3].T)
                    dp = start_head[:3, 3] - head[:3, 3]
                if start_antennas is not None and antennas is not None:
                    da = start_antennas - np.asarray(antennas, dtype=float)
                if start_body_yaw is not None and body_yaw is not None:
                    db = float(start_body_yaw) - float(body_yaw)
            self._offset = (w, dp, da, db)
        return self._offset

    def fit_blend(self) -> None:
        """Shorten the ease to what the gap it has to close asks for."""

        if self._blend_s <= 0.0:
            return
        w, _dp, da, _db = self.entry_offset()
        head = float(np.degrees(np.linalg.norm(w))) if w is not None else 0.0
        antenna = float(np.degrees(np.max(np.abs(da)))) if da is not None else 0.0
        fitted = max(BLEND_MIN_S, head / BLEND_HEAD_DEG_PER_S, antenna / BLEND_ANTENNA_DEG_PER_S)
        self._blend_s = min(self._blend_s, fitted)

    def evaluate(self, t: float) -> tuple[Any, Any, Any]:
        t = min(max(0.0, float(t)), max(0.0, float(self.duration) - 1e-3))
        head, antennas, body_yaw = self._composed(t)
        if self._blend_s > 0.0 and t < self._blend_s:
            w, dp, da, db = self.entry_offset()
            r = 1.0 - _min_jerk(t / self._blend_s)
            if head is not None and w is not None:
                head[:3, :3] = _exp_so3(r * w) @ head[:3, :3]
                head[:3, 3] = head[:3, 3] + r * dp
            if antennas is not None and da is not None:
                antennas = np.asarray(antennas, dtype=float) + r * da
            if body_yaw is not None and db is not None:
                body_yaw = float(body_yaw) + r * db
        if head is not None and self._head_reachable is not None:
            head = _fit_into_reach(head, body_yaw, self._head_reachable)
        return head, antennas, body_yaw


class MotionArbiter:
    """The single decision point for "may this move the robot, and when".

    Thread-safe by construction: every public method may be called from an rclpy
    callback, from one of the REST shim's HTTP worker threads, or from the loop thread
    itself.  State lives under one lock; work is handed to the asyncio loop with
    ``call_soon_threadsafe``; listeners are called outside the lock so a slow publisher
    cannot wedge the robot.
    """

    def __init__(self, backend: Any, *, loop: asyncio.AbstractEventLoop,
                 queue_size: int = 12,
                 expression_owner: str = DEFAULT_EXPRESSION_OWNER,
                 recorded_moves_factory: Callable[[str], Any] | None = None,
                 motor_mode_reader: Callable[[], str] | None = None,
                 recorded_blend_s: float = DEFAULT_RECORDED_BLEND_S,
                 recorded_skip_lead_in: bool = True,
                 start_state_reader: Callable[[], Any] | None = None,
                 head_reachable: Callable[[Any, Any], bool] | None = None,
                 log: Any = None) -> None:
        self._backend = backend
        self._head_reachable = head_reachable
        self._loop = loop
        self._queue_size = max(1, int(queue_size))
        self._expression_owner = normalize_expression_owner(expression_owner)
        self._recorded_moves_factory = recorded_moves_factory
        # The recorded-move ease-in, and where it starts from: `(head 4x4, antennas (2,),
        # body_yaw)` as COMMANDED at the moment a recorded move starts. The node reads
        # the command, not the measurement - on a robot the measured head yaw can sit
        # 0.12 rad off the commanded one, and easing in from the measurement would make
        # the servo command itself jump by that much.
        try:
            blend = float(recorded_blend_s)
        except (TypeError, ValueError):
            blend = DEFAULT_RECORDED_BLEND_S
        self._recorded_skip_lead_in = bool(recorded_skip_lead_in)
        self._lead_ins: dict[str, float] = {}
        self._recorded_blend_s = blend if np.isfinite(blend) and blend > 0.0 else 0.0
        self._start_state_reader = start_state_reader
        # Where "what mode are the motors in" is ANSWERED, every time it is asked. The
        # node passes `backend_host.motor_mode`, which reads
        # `backend.get_motor_control_mode()` - a plain attribute read on `RobotBackend`,
        # so there is no cost worth caching for and a cache here would be a lie: Pollen
        # changes the mode behind us (`goto_sleep` disables the motors at the end of
        # every sleep) and a robot reported `enabled` while limp is a robot that accepts
        # moves it cannot make.
        self._motor_mode_reader = motor_mode_reader
        self._log = log

        # Public so the node can label the status message with the name it launched
        # with (`mujoco` / `mockup` / `robot`) rather than a guess from a class name.
        self.backend_name = _backend_name(backend)

        self._lock = threading.RLock()
        self._queue: deque[_Entry] = deque()
        self._trace: deque[tuple] = deque(maxlen=TRACE_CAPACITY)
        self._trace_seq = 0
        self._current: _Entry | None = None
        self._entries: dict[str, _Entry] = {}
        self._history: deque[str] = deque()
        self._closed = False

        self._owner = ""
        self._owner_expires_at: float | None = None
        self._head_reserved = False
        self._head_settle_until = 0.0     # monotonic; see HEAD_SETTLE_S
        # A mode the backend has already refused once is never asked for again: the
        # robot's v1.9.0 firmware answers `gravity_compensation` with an empty 500 and
        # wedges, and the host application latches the same refusal for the same reason.
        self._refused_modes: set[str] = set()
        self._motor_mode = self._read_motor_mode()

        self._listeners: list[Callable[[dict], None]] = []
        self._last_signature: tuple = ()

        # The motion ledger is exact by construction:
        #   accepted + sum(refused_*) == submitted
        #   completed + cancelled + preempted + failed == accepted   (once settled)
        # which is why a motor-mode change gets its own two counters rather than
        # borrowing `refused_backend_error`: it is not a motion request, and a mode the
        # firmware refuses would otherwise make the numbers on /diagnostics stop adding
        # up for a reason nobody could find.
        self.counters: dict[str, int] = {
            "submitted": 0, "accepted": 0, "completed": 0, "cancelled": 0,
            "preempted": 0, "failed": 0, "motor_mode_set": 0, "motor_mode_refused": 0,
        }
        for code in REFUSALS:
            self.counters[f"refused_{code}"] = 0
        # Deliberately NOT a member of REFUSALS, and the distinction is the ledger's:
        # this move was accepted and then refused at the moment it came up to run, so it
        # belongs to `failed` in the settlement identity and NOT to the sum of
        # `refused_*` over REFUSALS, which must keep adding up to `submitted`. It gets
        # its own name so /diagnostics can tell "the robot is asleep" from "the robot
        # faulted", which are different problems with different fixes.
        self.counters["refused_motors_disabled"] = 0
        # Not part of either ledger identity: an anchored move is still exactly one
        # accepted move. It counts how often a head pose was re-centred on a face.
        self.counters["anchored"] = 0
        # The recorded-move share of `anchored` (a recorded move bumps both), and how
        # many recorded moves were eased in from the commanded start state. Same rule:
        # counts of HOW a move played, never of whether it did.
        self.counters["anchored_recorded"] = 0
        self.counters["blended_recorded"] = 0

        self._pose_anchor: Callable[[], Any] | None = None
        self._anchor_exempt = ""
        self._recorded_anchor = DEFAULT_RECORDED_ANCHOR

    # --- the face anchor ----------------------------------------------------
    def set_pose_anchor(self, anchor: Callable[[], Any] | None, *,
                        exempt_client: str = "",
                        recorded: str = DEFAULT_RECORDED_ANCHOR) -> None:
        """Centre other authors' head poses on ``anchor()``, a ``(pitch, yaw)`` or None.

        Speech gestures are authored about neutral. With a person off to one side that
        is a robot that talks to the wall, and gaze cannot help: it is refused ``busy``
        for the whole of every beat, and a voice-paced talking flow is nothing but beats.
        So the rotation of every non-safety head pose is composed ONTO the face aim - the
        gesture keeps its shape and its size, and is played where the person is.

        ``exempt_client`` is gaze itself (its targets already ARE the aim), and safety is
        never touched: a calm-settle goes where it was told to. An anchor that raises or
        answers None leaves the pose exactly as submitted.

        ``recorded`` is how much of the aim a Pollen recorded move gets - one of
        ``RECORDED_ANCHORS``, see there for why ``yaw`` is the default. An unrecognised
        value reads as ``off``, the behaviour before recorded moves were anchored at all:
        the fail-closed direction for a knob that moves a robot is "not the new thing".
        """

        mode = (recorded or "").strip().lower()
        if mode not in RECORDED_ANCHORS:
            self._warn(f"unknown recorded-move anchor {recorded!r}; known modes are "
                       f"{RECORDED_ANCHORS}. Recorded moves will play as authored (off)")
            mode = "off"
        with self._lock:
            self._pose_anchor = anchor
            self._anchor_exempt = str(exempt_client or "")
            self._recorded_anchor = mode

    def _aim_for(self, req: MotionRequest) -> tuple[float, float] | None:
        """The face aim ``(pitch, yaw)`` this request may be centred on, or None.

        None for safety, for gaze itself, with no anchor, and whenever the anchor
        raises, answers None, or answers something that is not a pair of numbers.
        """

        anchor = self._pose_anchor
        if (anchor is None or req.lane == "safety"
                or (self._anchor_exempt and req.client == self._anchor_exempt)):
            return None
        try:
            aim = anchor()
            if aim is None or len(aim) < 2:
                return None
            return float(aim[0]), float(aim[1])
        except Exception:  # noqa: BLE001 - a broken anchor must not cost the move
            return None

    def _anchored(self, req: MotionRequest, head: Any) -> Any:
        """``head`` with its rotation composed onto the face aim, or ``head`` unchanged."""

        if head is None:
            return head
        aim = self._aim_for(req)
        if aim is None:
            return head
        pose = np.array(head, dtype=float).reshape((4, 4))
        pose[:3, :3] = _aim_rotation(*aim) @ pose[:3, :3]
        with self._lock:
            self.counters["anchored"] += 1
        return pose

    def _placed(self, req: MotionRequest, move: Any) -> Any:
        """``move`` aimed at the face and eased in, or ``move`` itself.

        Called on the loop immediately before ``play_move``, after the (possibly slow)
        library lookup: the aim and the start state are read ONCE, at the instant the
        move starts, and never per frame. A move played around a face that moves with
        every 3 Hz detection would wobble, and it would stop being the authored gesture.

        Returned unchanged when it cannot be placed at all (no callable ``evaluate`` or
        no usable ``duration``: whatever it is, it is Pollen's to judge), and when there is
        neither an aim nor a start state - with nothing to add, the library object is
        exactly what played before this existed. Anything that goes wrong while placing
        plays the move as authored: an ease-in is never worth a gesture.

        One INFO line per recorded move, whatever happens to it. The face aim is not
        exposed anywhere else, and this line is how it gets measured on the robot.
        """

        label = f"recorded move {req.move or '?'} ({req.client}/{req.lane})"
        try:
            duration = float(move.duration)
            placeable = (callable(getattr(move, "evaluate", None))
                         and bool(np.isfinite(duration)) and duration > 0.0)
        except Exception:  # noqa: BLE001 - no duration is "not a Move", not a fault
            placeable = False
        if not placeable:
            self._info(f"{label}: played as authored (no evaluate/duration to place)")
            return move

        mode = self._recorded_anchor
        aim = self._aim_for(req)
        rotation = None
        rot_pitch = rot_yaw = 0.0
        if aim is not None and mode != "off":
            rot_pitch = aim[0] if mode == "full" else 0.0
            rot_yaw = aim[1]
            rotation = _aim_rotation(rot_pitch, rot_yaw)
        start = self._read_start_state() if self._recorded_blend_s > 0.0 else None
        skip = self._lead_in_to_skip(req, move)
        aim_text = "none" if aim is None else f"({aim[0]:+.3f},{aim[1]:+.3f})"
        head_text = (f"mode={mode} aim={aim_text} rot_yaw={rot_yaw:+.3f} "
                     f"rot_pitch={rot_pitch:+.3f} skip={skip:.2f}s")
        if rotation is None and start is None and skip <= 0.0 and self._head_reachable is None:
            self._info(f"{label}: {head_text} blend=off start_jump=n/a")
            return move

        try:
            placed = _PlacedMove(move, rotation, start, self._recorded_blend_s, skip,
                                 head_reachable=self._head_reachable)
            if self._recorded_skip_lead_in:
                placed.fit_blend()
            jump = "n/a"
            if start is not None:
                w = placed.entry_offset()[0]
                if w is not None:
                    jump = f"{float(np.degrees(np.linalg.norm(w))):.1f}deg"
        except Exception as error:  # noqa: BLE001 - never worth the gesture
            self._warn(f"{label}: could not be placed ({error}); played as authored")
            return move

        with self._lock:
            if rotation is not None:
                self.counters["anchored"] += 1
                self.counters["anchored_recorded"] += 1
            if start is not None:
                self.counters["blended_recorded"] += 1
        blend_text = f"{self._recorded_blend_s:.2f}s" if start is not None else "off"
        self._info(f"{label}: {head_text} blend={blend_text} start_jump={jump}")
        return placed

    def _lead_in_to_skip(self, req: MotionRequest, move: Any) -> float:
        """How far into this recording to start, worked out once per recording."""

        if not self._recorded_skip_lead_in:
            return 0.0
        key = f"{req.dataset}/{req.move}"
        if key not in self._lead_ins:
            try:
                self._lead_ins[key] = max(0.0, _first_motion_s(move) - LEAD_IN_KEPT_S)
            except Exception:  # noqa: BLE001 - an ease-in is never worth a gesture, nor is this
                self._lead_ins[key] = 0.0
        return self._lead_ins[key]

    def _read_start_state(self) -> tuple[Any, Any, Any] | None:
        """``(head 4x4, antennas (2,), body_yaw)`` as commanded now, or None.

        Each part may be None on its own (that part is then simply not eased); a reader
        that raises, or that can say nothing at all, means no ease-in - the move still
        plays, and is still aimed.
        """

        reader = self._start_state_reader
        if reader is None:
            return None
        try:
            head, antennas, body_yaw = reader()
            head = None if head is None else np.array(head, dtype=float).reshape((4, 4))
            antennas = (None if antennas is None
                        else np.array(antennas, dtype=float).reshape((2,)))
            body_yaw = None if body_yaw is None else float(body_yaw)
        except Exception as error:  # noqa: BLE001 - a start we cannot read is no start
            self._debug(f"recorded-move start state unreadable ({error}); no ease-in")
            return None
        if head is None and antennas is None and body_yaw is None:
            return None
        return head, antennas, body_yaw

    # --- submission ---------------------------------------------------------
    def submit(self, req: MotionRequest) -> Outcome:
        """Judge one request: refuse it now, or take it and name it.

        Returns immediately in every case.  An accepted request carries a ``move_id``
        the caller can ``wait`` on, ``stop``, or watch on ``/reachy_mini/motion_status``.
        """

        if req.kind == "set_target":
            return self.set_target(req.head_pose, req.antennas, req.body_yaw,
                                   client=req.client, lane=req.lane)

        cancels: list[tuple[_Entry, str]] = []
        settled: list[_Entry] = []
        with self._lock:
            self.counters["submitted"] += 1
            refusal = self._refusal_for(req)
            if refusal:
                self.counters[f"refused_{refusal}"] += 1
                self._bump_client_refusal_locked(req.client)
                self._debug(f"refused {req.kind} from {req.client} on {req.lane}: {refusal}")
                return Outcome(False, refusal, "")

            entry = _Entry(move_id=uuid.uuid4().hex, req=req)
            self._entries[entry.move_id] = entry

            if req.lane == "safety":
                # Safety cancels what is running AND flushes queued expression work:
                # the point of a calm-settle is that it happens now, not after the
                # three cheerful gestures somebody queued a second ago.
                if self._current is not None:
                    self._current.cancel_reason = "preempted"
                    cancels.append((self._current, "preempted"))
                for queued in [e for e in self._queue if e.req.lane == "expression"]:
                    self._queue.remove(queued)
                    self._settle_locked(queued, "preempted")
                    settled.append(queued)
                self._insert_behind_safety_locked(entry)
            elif req.supersede:
                current = self._current
                if (current is not None and current.req.client == req.client
                        and current.req.lane != "safety"
                        and current.req.kind in SUPERSEDABLE_KINDS):
                    current.cancel_reason = "preempted"
                    cancels.append((current, "preempted"))
                for queued in [e for e in self._queue
                               if e.req.client == req.client and e.req.lane != "safety"
                               and e.req.kind in SUPERSEDABLE_KINDS]:
                    self._queue.remove(queued)
                    self._settle_locked(queued, "preempted")
                    settled.append(queued)
                self._insert_behind_safety_locked(entry)
            else:
                if (req.preempt and req.lane == "interaction"
                        and self._current is not None
                        and self._current.req.lane == "expression"):
                    # Interaction preempts EXPRESSION only. It must never cut a safety
                    # move short, and "one interaction move interrupts another" would
                    # make the conversation mechanics fight themselves.
                    self._current.cancel_reason = "preempted"
                    cancels.append((self._current, "preempted"))
                if req.preempt and req.lane == "interaction":
                    # ...and it goes AHEAD of queued expression work, for the reason it
                    # asked to preempt at all. Cutting the running expression short only
                    # to queue behind the two expressions waiting behind it would delay
                    # the recenter by exactly as long as the caller said it could not
                    # wait. Same shape as `_insert_behind_safety_locked`, one lane down.
                    self._insert_ahead_of_expression_locked(entry)
                else:
                    self._queue.append(entry)

            self.counters["accepted"] += 1
            self._bump_locked(f"accepted_{req.client}")
            self._trace_locked("accepted", entry)

        for target, _reason in cancels:
            self._request_cancel(target)
        for done in settled:
            done.done.set()
        self._schedule_pump()
        self._notify()
        return Outcome(True, "", entry.move_id)

    def wait(self, move_id: str, timeout: float | None = None) -> Outcome:
        """Block until that move settles.  ``error_code`` is a member of ``OUTCOMES``.

        An id the arbiter no longer knows about answers ``timeout``: the caller's
        situation is the same either way - there is nothing left to wait for - and
        inventing a fourth answer for "too old" would only give callers a case to
        forget.
        """

        with self._lock:
            entry = self._entries.get(move_id)
        if entry is None or not entry.done.wait(timeout):
            return Outcome(False, "timeout", move_id)
        return Outcome(entry.error_code == "", entry.error_code, move_id)

    def stop(self, move_id: str = "") -> int:
        """Cancel one move, or (empty id) everything accepted and not yet settled.

        Cancelling is ``Task.cancel()`` on the coroutine driving the backend, which is
        exactly what the daemon's own stop does: the robot holds its current target
        rather than snapping anywhere.
        """

        cancels: list[_Entry] = []
        settled: list[_Entry] = []
        with self._lock:
            targets: list[_Entry] = []
            if move_id:
                entry = self._entries.get(move_id)
                if entry is not None and not entry.finished:
                    targets.append(entry)
            else:
                if self._current is not None:
                    targets.append(self._current)
                targets.extend(self._queue)
            for entry in targets:
                entry.cancel_reason = "cancelled"
                if entry is self._current:
                    cancels.append(entry)
                elif entry in self._queue:
                    self._queue.remove(entry)
                    self._settle_locked(entry, "cancelled")
                    settled.append(entry)
            stopped = len(targets)

        for entry in cancels:
            self._request_cancel(entry)
        for entry in settled:
            entry.done.set()
        self._schedule_pump()
        self._notify()
        return stopped

    def running(self) -> list[str]:
        """Every accepted move that has not settled, the running one first.

        Queued moves are included because that is what a client means by "is anything
        in flight" - and it is what the daemon reports, since it creates a task per
        request the moment it accepts one.
        """

        with self._lock:
            ids = [self._current.move_id] if self._current is not None else []
            ids.extend(entry.move_id for entry in self._queue)
        return ids

    # --- streaming targets --------------------------------------------------
    def set_target(self, head_pose: np.ndarray | None = None,
                   antennas: Sequence[float] | None = None,
                   body_yaw: float | None = None, *,
                   client: str | None = None, lane: str | None = None) -> Outcome:
        """Apply a pose target directly, or refuse ``busy`` while a move runs.

        Unqueued by design.  A target is the caller's latest wish, not a goal: queueing
        it would replay a stale wish seconds later.  Pollen's daemon answers
        ``{"status": "ignored", "reason": "move_running"}`` in the same situation; we
        call that ``busy`` and the shim translates it back.

        ``client`` and ``lane`` are keyword-only additions to the interface so a target
        arriving over ROS can be attributed to the graph rather than to the host
        application; omitting them keeps the documented three-argument behaviour.
        """

        req = MotionRequest(kind="set_target", lane=lane or DEFAULT_LANE,
                            client=client or DEFAULT_CLIENT, head_pose=head_pose,
                            antennas=tuple(antennas) if antennas is not None else None,
                            body_yaw=body_yaw)
        with self._lock:
            self.counters["submitted"] += 1
            refusal = self._refusal_for(req)
            if not refusal and self._current is not None:
                refusal = "busy"
            if refusal:
                self.counters[f"refused_{refusal}"] += 1
                self._bump_client_refusal_locked(req.client)
                return Outcome(False, refusal, "")

        try:
            self._backend.set_target(
                head=self._anchored(req, head_pose),
                antennas=(np.asarray(antennas, dtype=float)
                          if antennas is not None else None),
                body_yaw=body_yaw,
            )
        except Exception as error:  # noqa: BLE001 - a backend fault is an answer, not a crash
            with self._lock:
                self.counters["refused_backend_error"] += 1
                self._bump_client_refusal_locked(req.client)
            self._warn(f"set_target failed on the backend: {error}")
            return Outcome(False, "backend_error", "")

        with self._lock:
            self.counters["accepted"] += 1
            self.counters["completed"] += 1
            self._bump_locked(f"accepted_{req.client}")
            self._bump_locked(f"completed_{req.client}")
            if head_pose is not None:
                self._head_settle_until = time.monotonic() + HEAD_SETTLE_S
        return Outcome(True, "", "")

    # --- gates --------------------------------------------------------------
    def set_head_reserved(self, reserved: bool) -> None:
        """Reserve the head as an INPUT device: no expression may move it.

        Only the expression lane is blocked.  Interaction mechanics (a recenter after
        the push-turn ends, a wake) and safety must still run, or reserving the head
        would leave it stuck wherever a person left it.
        """

        with self._lock:
            self._head_reserved = bool(reserved)
        self._notify()

    def acquire_owner(self, client: str, ttl_s: float) -> bool:
        """Take the exclusive owner lock, or renew it if this client already holds it.

        ``ttl_s <= 0`` means "until released".  Anything else expires on the loop, so a
        client that dies holding the lock does not leave the robot permanently deaf to
        everyone else.
        """

        name = (client or "").strip()
        if not name:
            return False
        with self._lock:
            current = self._expire_owner_locked()
            if current and current != name:
                return False
            self._owner = name
            self._owner_expires_at = (time.monotonic() + float(ttl_s)
                                      if ttl_s and ttl_s > 0 else None)
            ttl = float(ttl_s) if ttl_s and ttl_s > 0 else 0.0
        if ttl > 0:
            self._schedule_owner_expiry(ttl)
        self._notify()
        return True

    def release_owner(self, client: str) -> bool:
        """Release the lock.  An empty client releases whatever is held.

        The service spells "release" as an empty client, and the caller genuinely may
        not know who the holder is; a named client may only release itself.
        """

        with self._lock:
            current = self._expire_owner_locked()
            if not current:
                return False
            if client and client.strip() != current:
                return False
            self._owner = ""
            self._owner_expires_at = None
        self._notify()
        return True

    def set_motor_mode(self, mode: str) -> Outcome:
        """Set the motor control mode, latching a mode the backend refuses.

        The robot's v1.9.0 firmware refuses ``gravity_compensation`` with an empty 500
        and wedges the daemon's readiness afterwards.  The host application latches
        that refusal after one failure; so does this, for the same reason - retrying a
        mode that will never be granted spends the serial bus and risks the wedge
        again.
        """

        normalized = (mode or "").strip().lower()
        with self._lock:
            if normalized not in MOTOR_MODES:
                self.counters["motor_mode_refused"] += 1
                self._warn(f"unknown motor mode {mode!r}; known modes are {MOTOR_MODES}")
                return Outcome(False, "backend_error", "")
            if normalized in self._refused_modes:
                self.counters["motor_mode_refused"] += 1
                return Outcome(False, "backend_error", "")

        try:
            self._backend.set_motor_control_mode(_motor_mode_value(normalized))
        except Exception as error:  # noqa: BLE001 - the firmware really does refuse
            with self._lock:
                self._refused_modes.add(normalized)
                self.counters["motor_mode_refused"] += 1
            self._warn(f"motor mode {normalized!r} refused by the backend ({error}); "
                       "latched off - it will not be attempted again")
            return Outcome(False, "backend_error", "")

        with self._lock:
            self.counters["motor_mode_set"] += 1
            self._motor_mode = normalized
        self._notify()
        return Outcome(True, "", "")

    # --- observation --------------------------------------------------------
    def status(self) -> dict:
        """The ``MotionStatus`` fields plus the counters, as one plain dict."""

        with self._lock:
            owner = self._expire_owner_locked()
            current = self._current
            return {
                "busy": current is not None,
                "running_move_id": current.move_id if current else "",
                # Who is moving the robot, by name and by lane. `_notify`'s change
                # signature is deliberately left alone: `running_move_id` is already in
                # it and changes whenever these two do.
                "running_client": current.req.client if current else "",
                "running_lane": current.req.lane if current else "",
                "queue_depth": len(self._queue),
                "owner": owner,
                "head_reserved": self._head_reserved,
                "expression_owner": self._expression_owner,
                # Read through, never cached: see `_motor_mode_reader`. `MotionStatus`
                # and /diagnostics are how an operator finds out the motors went limp,
                # and a field that only changes when WE change it cannot tell them.
                "motor_mode": self._live_motor_mode(),
                "backend": self.backend_name,
                "counters": dict(self.counters),
            }

    def motion_snapshot(self) -> dict:
        """Who is moving the robot right now: four fields, no backend read, no copy.

        ``status()`` reads the LIVE motor mode (through ``_live_motor_mode`` and so
        through the backend) and copies the whole counters dict.  This is polled at
        10 Hz by the host application's gesture ticker, on the same CM4 that is running
        the 50 Hz control loop, and must do neither - which is the same cost the shim's
        50 ms pose cache was written to avoid.

        ``head_moving`` rather than plain ``active`` because an antenna-only cue cannot
        move the measured head pose, and suppressing the gesture sensor for one would
        disarm mode selection while the robot is merely giving phase feedback.
        """

        with self._lock:
            current = self._current
            req = current.req if current is not None else None
            return {
                "active": req is not None,
                "head_moving": ((req is not None and _moves_head(req))
                                or time.monotonic() < self._head_settle_until),
                "head_reserved": self._head_reserved,
                "client": req.client if req is not None else "",
                "lane": req.lane if req is not None else "",
            }

    def add_status_listener(self, cb: Callable[[dict], None]) -> None:
        """Call ``cb(status_dict)`` whenever a ``MotionStatus`` field changes.

        Counters are excluded from the change test on purpose: they move on every
        request, and a listener that publishes a latched topic does not want a message
        per refusal.
        """

        with self._lock:
            self._listeners.append(cb)

    def close(self, timeout: float = 2.0) -> None:
        """Stop everything, refuse further work, and wait for the loop to catch up.

        Not part of the published interface the shim codes against - it is the node's
        teardown hook.  The wait is the point: ``stop`` only *schedules* the cancels on
        the loop, so returning immediately would let the caller stop the loop with a
        cancelled task still pending, which asyncio reports as "Task was destroyed but
        it is pending" and which on a real robot means the backend thread is joined
        while a move is still writing targets to it.
        """

        with self._lock:
            self._closed = True
        self.stop("")
        deadline = time.monotonic() + max(0.0, timeout)
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.005)

    # --- internals: judging -------------------------------------------------
    def _refusal_for(self, req: MotionRequest) -> str:
        """The rules, in the order they are applied.  Lock held."""

        if self._closed:
            return "backend_error"
        if req.lane not in LANES:
            return "bad_lane"
        owner = self._expire_owner_locked()
        if owner and owner != req.client and req.lane != "safety":
            return "not_owner"
        if req.lane == "expression":
            # `owner_of`, not the raw name: `ros:gaze` is the ROS graph's gaze
            # sub-client and must pass the same ownership test `ros` passes. The
            # exclusive owner lock above is deliberately NOT relaxed the same way - a
            # lock is a lock, and `ros` holding one should still lock out `ros:gaze`.
            if (self._expression_owner == "none"
                    or owner_of(req.client) != self._expression_owner):
                return "not_owner"
            if self._head_reserved:
                return "head_reserved"
            if self._current is not None and len(self._queue) >= EXPRESSION_BACKLOG_LIMIT:
                return "dropped"
        # A superseding request is about to flush the queue it would otherwise be refused by.
        if (req.kind != "set_target" and not req.supersede
                and len(self._queue) >= self._queue_size):
            return "queue_full"
        return ""

    def _insert_behind_safety_locked(self, entry: _Entry) -> None:
        """Put a request at the front, behind any safety already waiting."""

        index = 0
        for index, queued in enumerate(self._queue):
            if queued.req.lane != "safety":
                break
        else:
            index = len(self._queue)
        self._queue.insert(index, entry)

    def _insert_ahead_of_expression_locked(self, entry: _Entry) -> None:
        """Put a preempting interaction request in front of queued EXPRESSION work.

        Behind everything that is not expression - safety, and interaction that was
        asked for first - so this reorders exactly one lane against exactly one other,
        which is the only reordering `preempt` was ever documented to do.
        """

        index = len(self._queue)
        for position, queued in enumerate(self._queue):
            if queued.req.lane == "expression":
                index = position
                break
        self._queue.insert(index, entry)

    def _expire_owner_locked(self) -> str:
        """The owner, after dropping an expired lock.  Lock held.

        The TTL is also scheduled on the loop (so listeners hear about the expiry even
        if nobody asks), but it is evaluated here as well: a lock that has run out must
        not still be refusing requests merely because the loop is busy.
        """

        if self._owner and self._owner_expires_at is not None \
                and time.monotonic() >= self._owner_expires_at:
            self._owner = ""
            self._owner_expires_at = None
        return self._owner

    # --- internals: running -------------------------------------------------
    def _schedule_pump(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._pump)
        except RuntimeError:
            # The loop is closed (shutdown raced a submit). Nothing to drive.
            pass

    def _pump(self) -> None:
        """Start the next move if none is running.  Runs ON the loop thread."""

        entry: _Entry | None = None
        with self._lock:
            if self._current is not None or self._closed:
                return
            while self._queue:
                candidate = self._queue.popleft()
                if candidate.finished:
                    continue
                entry = candidate
                break
            if entry is None:
                return
            entry.started_at = time.monotonic()
            self._current = entry
            self._trace_locked("started", entry)
            # Under the lock: anyone who sees this entry as current then also sees its task, and
            # their cancel lands after its first step. A task cancelled before that never runs
            # `_run` at all, so the entry would never settle and nothing would ever run again.
            entry.task = self._loop.create_task(self._run(entry))

        self._notify()

    async def _run(self, entry: _Entry) -> None:
        """Drive one request on the backend and settle it, whatever happens."""

        error = ""
        try:
            await self._execute(entry.req)
        except asyncio.CancelledError:
            # Deliberately not re-raised: this is a top-level task nobody awaits, and
            # the reason the canceller recorded ("cancelled" or "preempted") is more
            # informative than the exception.
            error = entry.cancel_reason or "cancelled"
        except _MotorsDisabled as refusal:
            error = "motors_disabled"
            self._warn(f"move {entry.move_id} ({entry.req.kind}) not attempted: {refusal}")
        except Exception as failure:  # noqa: BLE001 - a backend fault is an outcome
            error = "backend_error"
            self._warn(f"move {entry.move_id} ({entry.req.kind}) failed: {failure}")
        finally:
            with self._lock:
                if self._current is entry:
                    self._current = None
                self._settle_locked(entry, error)
            entry.done.set()
            self._pump()
            self._notify()

    async def _execute(self, req: MotionRequest) -> None:
        """The only place this package calls a backend coroutine."""

        if req.kind == "goto":
            self._require_enabled_motors(req)
            antennas = (np.asarray(req.antennas, dtype=float)
                        if req.antennas is not None else None)
            await self._backend.goto_target(
                head=self._anchored(req, req.head_pose),
                antennas=antennas,
                duration=max(1e-3, float(req.duration)),
                method=_interpolation_value(req.interpolation),
                body_yaw=req.body_yaw,
            )
        elif req.kind == "wake_up":
            # The daemon's own wake path is `set_motor_control_mode(Enabled)` and THEN
            # `wake_up()` (daemon/daemon.py:474, and again in the antenna-touch
            # launcher). Mirrored here for the same reason: a wake that inherits a robot
            # parked limp by `goto_sleep` would otherwise play the whole wake trajectory
            # into a robot that cannot move, sound and all.
            self._enable_motors_for_wake()
            await self._backend.wake_up()
        elif req.kind == "goto_sleep":
            await self._backend.goto_sleep()
        elif req.kind == "play_recorded":
            self._require_enabled_motors(req)
            move = await self._resolve_recorded_move(req.dataset, req.move)
            # Placed HERE: after the lookup (which may have downloaded) and immediately
            # before playback, so the aim and the start state are those of the instant
            # the move starts.
            await self._backend.play_move(self._placed(req, move))
        else:
            raise ValueError(f"unknown motion kind {req.kind!r}; known kinds {MOVE_KINDS}")

    def _require_enabled_motors(self, req: MotionRequest) -> None:
        """Refuse a move the motors cannot make.  Checked when the move RUNS.

        Not at submit time, because the answer changes while a request waits: a
        `goto_sleep` queued in front of this one ends by disabling the motors
        (Pollen's own `goto_sleep`, daemon/backend/abstract.py:1315), and a gate that
        ran at acceptance would have read `enabled` and waved it through.

        An UNREADABLE mode ("" - a backend that has not started, or one whose read
        raised) is not a refusal.  Same doctrine as ``BackendHost.motor_mode``: a
        backend that cannot say is not a backend saying no, and refusing on silence
        would make an unrelated read failure look like a sleeping robot.
        """

        mode = self._live_motor_mode()
        if mode and mode != "enabled":
            raise _MotorsDisabled(
                f"the motors are in {mode!r}, not 'enabled'; {req.kind} would have been "
                "written into the void and reported as a completed move. Wake the robot "
                "(/reachy_mini/wake_up) or set the mode (/reachy_mini/set_motor_mode "
                "enabled) first")

    def _enable_motors_for_wake(self) -> None:
        """Enable the motors before a wake, the way the daemon does.

        A refusal is logged and not raised: ``set_motor_mode`` has its own latch for a
        mode the firmware will never grant, and a wake that gave up because of it would
        leave the robot asleep with nothing left to try.
        """

        if self._live_motor_mode() == "enabled":
            return
        outcome = self.set_motor_mode("enabled")
        if not outcome.accepted:
            self._warn("could not enable the motors before wake_up "
                       f"({outcome.error_code}); waking anyway")

    def _live_motor_mode(self) -> str:
        """The mode the motors are in NOW, or the last one read if the read fails."""

        reader = self._motor_mode_reader
        try:
            mode = reader() if reader is not None else self._read_motor_mode()
        except Exception:  # noqa: BLE001 - a backend that cannot say is not an error
            return self._motor_mode
        mode = str(getattr(mode, "value", mode) or "")
        if mode:
            self._motor_mode = mode
        return mode or self._motor_mode

    async def _resolve_recorded_move(self, dataset: str, name: str) -> Any:
        """Look the move up off the loop: the first lookup may hit HuggingFace.

        A dataset that is not in the local cache downloads, and doing that inline would
        make the move uncancellable for as long as it took.
        """

        factory = self._recorded_moves_factory
        if factory is None:
            raise RuntimeError("no recorded_moves_factory: this node cannot play "
                               "recorded moves (pass one to MotionArbiter)")
        library = await self._loop.run_in_executor(None, factory, dataset)
        return library.get(name)

    def _request_cancel(self, entry: _Entry) -> None:
        """Cancel a running task from any thread."""

        def _cancel() -> None:
            task = entry.task
            if task is not None and not task.done():
                task.cancel()

        try:
            self._loop.call_soon_threadsafe(_cancel)
        except RuntimeError:
            pass

    def _settle_locked(self, entry: _Entry, error: str) -> None:
        """Book one entry as finished exactly once.  Lock held."""

        if entry.finished:
            return
        entry.finished = True
        entry.error_code = error
        self._trace_locked("settled", entry)
        if _moves_head(entry.req):
            self._head_settle_until = time.monotonic() + HEAD_SETTLE_S
        if error == "":
            self.counters["completed"] += 1
            self._bump_locked(f"completed_{entry.req.client}")
        elif error == "preempted":
            self.counters["preempted"] += 1
        elif error == "cancelled":
            self.counters["cancelled"] += 1
        else:
            # `failed` keeps the settlement identity exact
            # (completed + cancelled + preempted + failed == accepted); the named
            # counter beside it is what makes a limp robot findable on /diagnostics.
            self.counters["failed"] += 1
            if error == "motors_disabled":
                self.counters["refused_motors_disabled"] += 1

        self._history.append(entry.move_id)
        while len(self._history) > _HISTORY:
            self._entries.pop(self._history.popleft(), None)

    def _trace_locked(self, event: str, entry: _Entry) -> None:
        self._trace_seq += 1
        self._trace.append((self._trace_seq, time.monotonic(), event, entry.move_id,
                            entry.req.kind, entry.req.client, entry.req.lane, entry.req.reason,
                            entry.error_code, len(self._queue)))

    def trace_since(self, after: int = 0) -> list[dict]:
        """The records after sequence number ``after``, oldest first; below 0, the last one.

        The only record of when a move really started and ended, since a client only hears
        when it was accepted. Times are this host's monotonic clock, which a container
        shares with the host it runs on.
        """

        with self._lock:
            if after < 0:
                found = list(self._trace)[-1:]
            else:
                found = [record for record in self._trace if record[0] > after]
        return [dict(zip(TRACE_FIELDS, record)) for record in found]

    def _bump_locked(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def _bump_client_refusal_locked(self, client: str) -> None:
        """``refused_<client>``, the per-client view of every refusal.  Lock held.

        Ledger discipline, said out loud because the module docstring promises it: this
        is a SECOND VIEW, not part of the identity.  ``accepted + sum(refused_<code>)
        == submitted`` counts only the codes in ``REFUSALS``, and this key is not one of
        them - unless a client is *named* after one, which is the single way a second
        view could corrupt the first.  A client called ``busy`` would land on the same
        key as the ``busy`` refusal and silently break the sum, so that collision is
        skipped rather than booked.  No shipped client name collides (``hostapp``,
        ``ros``, ``ros:gaze``); the guard is here so a future one cannot.
        """

        name = (client or "").strip()
        if not name or name in REFUSALS or name == "motors_disabled":
            return
        self._bump_locked(f"refused_{name}")

    # --- internals: telling the world ---------------------------------------
    def _schedule_owner_expiry(self, ttl_s: float) -> None:
        def _arm() -> None:
            self._loop.call_later(ttl_s, self._on_owner_timeout)

        try:
            self._loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            pass

    def _on_owner_timeout(self) -> None:
        with self._lock:
            self._expire_owner_locked()
        self._notify()

    def _notify(self) -> None:
        """Fire the listeners if a ``MotionStatus`` field actually changed."""

        with self._lock:
            state = self.status()
            signature = (state["busy"], state["running_move_id"], state["queue_depth"],
                         state["owner"], state["head_reserved"],
                         state["expression_owner"], state["motor_mode"])
            if signature == self._last_signature:
                return
            self._last_signature = signature
            listeners = list(self._listeners)

        for listener in listeners:
            try:
                listener(state)
            except Exception as error:  # noqa: BLE001 - a listener must not wedge motion
                self._warn(f"status listener failed: {error}")

    def _read_motor_mode(self) -> str:
        try:
            mode = self._backend.get_motor_control_mode()
        except Exception:  # noqa: BLE001 - a backend that cannot say is not an error
            return ""
        return str(getattr(mode, "value", mode))

    # --- internals: logging -------------------------------------------------
    def _info(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "info", print)(message)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "warn", getattr(self._log, "warning", print))(message)

    def _debug(self, message: str) -> None:
        if self._log is not None:
            getattr(self._log, "debug", print)(message)
