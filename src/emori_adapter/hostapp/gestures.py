"""Physical-gesture session control for the screenless robot.

No screen, no voice wake-word: the learner controls the session by physically
touching the robot. The Reachy Mini has no accelerometer/touch chip, but its HEAD
(6× XL330-M288-T) is back-drivable in ``gravity_compensation`` mode (the body-yaw
and antenna motors are not torque-controllable and go limp), so a human nudging the
head shows up as a deflection in the daemon's reported ``head_pose``. We poll
``/api/state/full`` and detect that head deflection. (The hostapp's RobotPlayer keeps
the head in ``gravity_compensation`` — re-asserting it after every self-move that
would otherwise re-stiffen the motors; see hostapp/driver.py::RobotPlayer.)

Training keeps its binary HEAD-TILT/NUDGE end control.  Outside training, the
same back-drivable head is a signed selector: a clear left or right movement is
handed to any add-on as a direction.  Training begins
only through the identity-bearing QR/card path, never through a head movement.

A firm push of the head BACKWARDS, held a little over half a second, is the way out of
whatever is running: :class:`BackPushDetector`.

The detector is a pure function (``detect_deflection``) over (current pose,
baseline) so it is unit-testable without a robot. The ``GestureDetector`` class
keeps the legacy binary training behavior; ``DirectionalGestureDetector`` keeps
the same rolling baseline + refractory window for safe directional selection.

CRITICAL: the robot also MOVES its own head (greeting/talking/idle gestures). The
caller MUST suppress detection while the robot is self-commanding the head (the
``armed`` flag), or the robot would trigger itself. Thresholds are env-tunable
because the exact back-drive deflection of a gentle tap can only be measured on
the real robot (the MuJoCo sim does not model a human finger).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class GestureDirection(str, Enum):
    """A head movement a person makes on purpose.

    LEFT and RIGHT are the two sides :func:`detect_direction` can answer. BACK is not a
    side and never comes out of it: it is what an add-on is handed to leave a mode.
    """

    LEFT = "left"
    RIGHT = "right"
    BACK = "back"


def _residual_enabled() -> bool:
    """``EMORI_GESTURE_RESIDUAL=0`` pins the sensor to the ABSOLUTE head pose.

    The one-variable rollback for residual sensing and everything that hangs off it
    (the narrowed head reservation, and with it the cue lane, the skill intake and the
    idle scan). Read per call, like every other gesture knob on this robot, so an
    operator's answer to "the robot is gesturing too much while it talks" is one line
    in ``/opt/emori/.env`` and a hostapp restart.
    """

    return (os.environ.get("EMORI_GESTURE_RESIDUAL", "1") or "").strip() != "0"


def _head_angles(state: Optional[dict]) -> Optional[tuple[float, float, float]]:
    """(roll, pitch, yaw) for the gesture detectors, in radians, or None.

    Prefers ``motion.residual`` -- ``measured - commanded``, computed by the v2 shim --
    over the absolute head pose, because push_turn's entire job is to tell a HUMAN's
    hand on the head from the robot's own motion, and subtracting the robot's own
    command is how you do that instead of going deaf every time the robot moves.

    Identical to the old behaviour whenever the robot is not moving: the command is
    then a constant, the residual is the absolute pose minus that constant, and the
    detectors' EMA baselines absorb a constant offset by construction.  So this changes
    nothing at idle, and changes everything during motion -- which is what finally lets
    the robot gesture while it speaks without deafening its own sensor.

    A state with no ``motion.residual`` -- Pollen's daemon, a v1 node, a backend that
    cannot report its command -- falls through to ``state["head_pose"]``, byte for byte
    today's code path.
    """
    if not isinstance(state, dict):
        return None
    if _residual_enabled():
        motion = state.get("motion")
        residual = motion.get("residual") if isinstance(motion, dict) else None
        if isinstance(residual, dict):
            try:
                return (float(residual["roll"]), float(residual["pitch"]),
                        float(residual["yaw"]))
            except (KeyError, TypeError, ValueError):
                # A malformed residual is not a reason to lose the sensor: fall through
                # to the absolute pose, which is what a v1 robot reads anyway.
                pass
    hp = state.get("head_pose")
    if not isinstance(hp, dict):
        return None
    try:
        return (float(hp.get("roll", 0.0)), float(hp.get("pitch", 0.0)), float(hp.get("yaw", 0.0)))
    except (TypeError, ValueError):
        return None


def detect_deflection(current: tuple[float, float, float],
                      baseline: tuple[float, float, float],
                      threshold_rad: float) -> bool:
    """True when the head has been deflected from baseline beyond ``threshold_rad``
    on any axis (roll/pitch/yaw). Pure function — the heart of the gesture, tested
    directly. A human tilt/nudge moves the head well beyond the resting jitter."""
    return any(abs(c - b) >= threshold_rad for c, b in zip(current, baseline))


#: A split push: each axis must carry at least this fraction of its own threshold...
AGREEING_AXIS_SHARE = 0.5
#: ...and the two together (in threshold units) must exceed the threshold by this much.
AGREEING_AXES_MARGIN = 1.25


def detect_direction(
    current: tuple[float, float, float],
    baseline: tuple[float, float, float],
    yaw_threshold_rad: float,
    roll_threshold_rad: float,
    ambiguity_ratio: float,
) -> GestureDirection | None:
    """Classify a deliberate signed sideways deflection.

    Directional selection deliberately accepts only a clear sideways motion:
    yaw and roll agree on the side, or one is sufficiently stronger than the
    other.  Pitch and opposed roll/yaw movements are intentional no-ops so a
    nod or an uncertain diagonal movement never reads as a direction.
    """

    roll = current[0] - baseline[0]
    pitch = current[1] - baseline[1]
    yaw = current[2] - baseline[2]
    if abs(pitch) >= min(yaw_threshold_rad, roll_threshold_rad):
        return None

    yaw_vote = 1 if yaw >= yaw_threshold_rad else -1 if yaw <= -yaw_threshold_rad else 0
    roll_vote = 1 if roll >= roll_threshold_rad else -1 if roll <= -roll_threshold_rad else 0
    if yaw_vote and roll_vote and yaw_vote != roll_vote:
        return None
    if yaw_vote and abs(yaw) >= max(abs(roll) * ambiguity_ratio, yaw_threshold_rad):
        return GestureDirection.RIGHT if yaw_vote > 0 else GestureDirection.LEFT
    if roll_vote and abs(roll) >= max(abs(yaw) * ambiguity_ratio, roll_threshold_rad):
        return GestureDirection.RIGHT if roll_vote > 0 else GestureDirection.LEFT
    if yaw_vote and roll_vote and yaw_vote == roll_vote:
        return GestureDirection.RIGHT if yaw_vote > 0 else GestureDirection.LEFT
    # One push, split between the two axes. On this head a sideways push is a turn AND
    # a tilt the same way, in a proportion that depends on where the hand lands: a clear
    # LEFT measured roll -0.219 / yaw -0.163 - both agreeing, 0.27 rad together, and
    # neither alone past its threshold or 1.4x the other, so it selected nothing and the
    # operator's turns were "ignored". Agreeing axes that each carry a
    # real share of the motion and together exceed the threshold by a margin are that
    # push. Opposed axes (a release stroke, a diagonal) still select nothing.
    if (yaw * roll > 0
            and min(abs(yaw) / yaw_threshold_rad, abs(roll) / roll_threshold_rad)
            >= AGREEING_AXIS_SHARE
            and math.hypot(yaw / yaw_threshold_rad, roll / roll_threshold_rad)
            >= AGREEING_AXES_MARGIN):
        return GestureDirection.RIGHT if yaw > 0 else GestureDirection.LEFT
    return None


@dataclass
class GestureDetector:
    """Rolling-baseline head-tilt detector with a refractory window.

    Fed a pose each tick via :meth:`update`; returns True exactly once per physical
    tap (then ignores further frames until the head returns near baseline AND the
    refractory window elapses, so one nudge != many triggers).
    """
    threshold_rad: float = 0.20          # ~11.5 deg: a deliberate nudge, not jitter
    refractory_ticks: int = 8            # min ticks between two accepted gestures
    baseline_alpha: float = 0.05         # slow EMA so the baseline tracks drift, not taps
    _baseline: Optional[tuple[float, float, float]] = field(default=None)
    _cooldown: int = 0
    _triggered_high: bool = False        # latched while deflected, cleared on return

    def reset(self) -> None:
        self._baseline = None
        self._cooldown = 0
        self._triggered_high = False

    def update(self, current: Optional[tuple[float, float, float]]) -> bool:
        """Feed one pose sample. Returns True on the rising edge of a deflection."""
        if current is None:
            return False
        if self._baseline is None:
            self._baseline = current
            return False
        deflected = detect_deflection(current, self._baseline, self.threshold_rad)
        if self._cooldown > 0:
            self._cooldown -= 1
        fired = False
        if deflected and not self._triggered_high and self._cooldown == 0:
            fired = True
            self._triggered_high = True
            self._cooldown = self.refractory_ticks
        elif not deflected:
            # head returned near baseline -> re-arm, and let the baseline track slow drift
            self._triggered_high = False
            b = self._baseline
            a = self.baseline_alpha
            self._baseline = tuple(b[i] + a * (current[i] - b[i]) for i in range(3))  # type: ignore[assignment]
        return fired


@dataclass
class BackPushDetector:
    """A firm push of the head backwards, held: the way out of whatever is running.

    It reads one number, the pitch of ``motion.residual``: where the head is against where
    the motors were told to put it, negative when the head is further back than commanded.
    Having the command already subtracted is what lets it keep listening while the robot
    moves, so someone can leave while the robot is talking, unlike the detectors below,
    which stand down for every move of the robot's own.

    What keeps the robot from setting it off itself is the sign and the time together.
    Measured over a scripted session with nobody touching the head: a head at rest never
    reads back of its command, and a moving one crosses ``threshold_rad`` only in dips far
    shorter than the hold. A person's push stays there for as long as they push.

    Only ever armed where the residual is really reported: on an absolute pose, a head
    looking up at a tall person would read as a push.
    """

    threshold_rad: float = 0.15
    stay_rad: float = 0.12
    hold_ticks: int = 7
    release_rad: float = 0.05
    _run: int = field(default=0, init=False, repr=False)
    _fired: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # A typo in a robot's .env must not turn the robot's own moves into exits.
        self.threshold_rad = max(0.10, float(self.threshold_rad))
        self.stay_rad = min(float(self.stay_rad), self.threshold_rad)
        self.hold_ticks = max(3, int(self.hold_ticks))

    @property
    def pending(self) -> bool:
        """A push has started and has not yet been held long enough, or let go."""

        return self._run > 0

    def reset(self) -> None:
        """Forget a push under way. One that already fired stays spent until released."""

        self._run = 0

    def update(self, pitch: Optional[float]) -> bool:
        """One 10 Hz sample of the residual pitch. True exactly once per push."""

        if pitch is None:
            return False
        if self._fired:
            if pitch > -self.release_rad:
                self._fired = False
            return False
        if self._run == 0:
            if pitch <= -self.threshold_rad:
                self._run = 1
            return False
        if pitch > -self.stay_rad:
            self._run = 0
            return False
        self._run += 1
        if self._run < self.hold_ticks:
            return False
        self._run = 0
        self._fired = True
        return True


@dataclass
class DirectionalGestureDetector:
    """Rolling-baseline signed detector used only outside a training session.

    Its safety semantics intentionally mirror :class:`GestureDetector`: one
    physical movement can fire at most once, it must return near baseline before
    rearming, and the baseline follows only slow settled drift.  A rejected
    pitch/ambiguous movement still latches until return so it cannot turn into a
    later sideways command while someone is holding the head off-centre.

    On top of that it requires a movement to have a BEGINNING that this detector
    actually watched:

    * settle-gated baseline -- an anchor is only adopted from ``settle_ticks``
      consecutive samples that agree within ``settle_epsilon_rad``, so a
      baseline can never be captured out of a head that is still travelling;
    * rise-from-settled -- a crossing only fires if the deflection sat below
      ``quiet_epsilon_rad`` for ``quiet_ticks`` samples first.

    Both come from measurements on the robot, where two spurious RIGHT selections
    fired out of head motion that was DECAYING, not rising: once with d_roll=+0.248
    on the sample right after "baseline established" (then 0.335 and falling), and
    once with 0.077/0.175/0.229 -> latched (then 0.335 and falling).  Gating on yaw-dominance would
    have rejected both, and every REAL turn with them: on this stiff-motor firmware a genuine
    operator push-turn also manifests as roll (measured d_roll=+0.236, which is why
    EMORI_GESTURE_INVERT exists).  The shape of the trajectory in time is the only thing that
    separates the two.

    On top of BOTH of those sits one hard invariant, because whatever a direction
    starts must not start by accident:

        no single physical turn-and-release may ever produce a fire in the
        direction of the RELEASE stroke.

    The rearm guard alone does not deliver it.  A push-turn that starts within
    ~0.6 s of the robot's own head motion is anchored *mid-turn*: the detector
    was suspended, so the first pose it sees demonstrably still is the pose the
    HUMAN is holding.  From there the human's straighten-back is a
    full-amplitude, opposite-sign deflection off an anchor they placed
    themselves -- and it would fire the other direction.  So the detector also remembers ``_home``: the
    last anchor it adopted while nothing suggested a human was mid-gesture.  An
    anchor sitting further than a selection threshold from ``_home`` is known to
    be displaced, and a stroke off it that travels back TOWARDS ``_home`` is a
    release, not a selection -- it re-baselines and never fires.

    The turn that placed that displaced anchor is therefore EATEN, deliberately:
    the detector genuinely cannot tell a head a human is holding from a head the
    robot parked, and guessing costs a wrong direction.  The human turns again, off a
    clean anchor, and it works.
    """

    yaw_threshold_rad: float = 0.20
    roll_threshold_rad: float = 0.20
    ambiguity_ratio: float = 1.40
    refractory_ticks: int = 8
    baseline_alpha: float = 0.05
    # A head that varies by less than this across settle_ticks consecutive
    # samples is at rest.  Measured hold jitter on this robot is ~0.01 rad.
    settle_epsilon_rad: float = 0.03
    settle_ticks: int = 3
    # "Rest" for the rise-from-settled rule: the deflection from the adopted
    # baseline must sit below this for quiet_ticks consecutive samples before
    # any crossing may be read as a deliberate turn.  Larger than
    # settle_epsilon_rad because it is measured against a fixed baseline rather
    # than across a sliding window, so it must tolerate the whole hold jitter.
    quiet_epsilon_rad: float = 0.05
    quiet_ticks: int = 3
    _baseline: Optional[tuple[float, float, float]] = field(default=None)
    _cooldown: int = 0
    _triggered_high: bool = False
    _settle: list[tuple[float, float, float]] = field(default_factory=list)
    _require_quiescence: bool = False
    # Consecutive samples observed within quiet_epsilon_rad of the baseline.
    _quiet_run: int = 0
    # The head has been at rest against THIS baseline, so a rise off it is a
    # human movement rather than the tail of an older one.  Cleared by a fire
    # (one rest -> one selection) and by every baseline adoption.
    _rise_armed: bool = False
    # ...and whether that ever happened for this baseline at all.  Distinguishes
    # a baseline captured mid-motion (recoverable: re-anchor on the pose the
    # head actually comes to rest at) from a post-fire hold (NOT recoverable:
    # re-anchoring there would turn the release stroke into an opposite turn).
    _seen_rest: bool = False
    # The head's undisturbed neutral, as far as this detector can know it: the
    # last anchor adopted while nothing suggested a human was mid-gesture, kept
    # current by the same slow EMA as the baseline -- but ONLY while the anchor
    # still sits near it, so a pose a human is holding can never become "home".
    # None until the first such anchor exists, which simply disables the
    # release-stroke rule rather than guessing at a neutral.
    _home: Optional[tuple[float, float, float]] = field(default=None)
    # A movement that LATCHED but classified as no direction (pitch nod, opposed
    # axes, ambiguity band).  It selected nothing, so re-anchoring on the pose
    # the stiff head is now parked at is safe -- and necessary, or the detector
    # is deaf forever (the EMA never runs while deflected, and the post-fire
    # re-anchor refusal would otherwise apply to a fire that never happened).
    _parked_without_direction: bool = False
    # One-shot edge of the above for the caller, consumed by take_vetoed_latch().
    _vetoed_latch: bool = False
    # "A human has hold of the head right now", as far as this detector can tell:
    # the head is deflected from its anchor, or a selection's refractory window is
    # still running.  A LEVEL, not an edge -- GestureTicker.head_reserved_for_sensing
    # reads it to decide whether the head is currently an input device, and the
    # trailing "...and for a second afterwards" timer lives there, with the clock.
    _held: bool = False
    # Whether the running refractory window was opened by a SELECTION (a human turned
    # the head) rather than by `suspend_until_settled` (the robot moved its own head).
    # Only the first kind means the head is in a human's hands. Measured on the robot:
    # with both kinds counted as `held`, every idle-scan move reserved the
    # head for the whole refractory second and the ROS graph lost 20 of 31 engagement
    # gestures in one session to `head_reserved` refusals.
    _cooldown_from_selection: bool = False

    def __post_init__(self) -> None:
        # These four are env-tunable on the robot, and a value that swallows the
        # selection threshold would silently disable the guard it exists for
        # (a "quiet" sample that is already past the threshold would arm AND
        # fire on the same tick).  Clamp rather than trust.
        self.settle_ticks = max(1, int(self.settle_ticks))
        self.quiet_ticks = max(1, int(self.quiet_ticks))
        ceiling = min(self.yaw_threshold_rad, self.roll_threshold_rad) / 2.0
        self.quiet_epsilon_rad = max(0.0, min(float(self.quiet_epsilon_rad), ceiling))
        self.settle_epsilon_rad = max(0.0, min(float(self.settle_epsilon_rad), ceiling))

    @property
    def held(self) -> bool:
        """Whether the last sample showed the head away from its anchor, or still
        inside a selection's refractory window.

        Set by :meth:`update` out of state it already computes, so this costs nothing
        and cannot drift from what the detector actually decided.
        """

        return self._held

    def reset(self) -> None:
        """Forget everything: a hard boundary such as entering/leaving training.

        Only for boundaries where no human can be mid-gesture.  After a
        selection the robot's own motion is NOT such a boundary --
        use :meth:`suspend_until_settled`.
        """

        self._baseline = None
        self._cooldown = 0
        self._triggered_high = False
        self._settle = []
        self._require_quiescence = False
        self._quiet_run = 0
        self._rise_armed = False
        self._seen_rest = False
        self._held = False
        self._cooldown_from_selection = False
        # Safe to drop: reset() leaves the detector in the trusted state, so the
        # very next settled anchor re-establishes home (see _adopt_baseline).
        self._home = None
        self._parked_without_direction = False
        self._vetoed_latch = False

    def take_vetoed_latch(self) -> bool:
        """True once per movement that latched without selecting a direction.

        The caller (``GestureTicker``) answers it by recentring the stiff head:
        a robot-authored move suspends and re-anchors this detector on a clean
        neutral, which is the only way a robot whose head is PARKED at a vetoed
        pose gets its sensor back within one gesture instead of never.
        """

        signalled = self._vetoed_latch
        self._vetoed_latch = False
        return signalled

    def suspend_until_settled(self) -> None:
        """Drop the baseline but stay disarmed until the head is still again.

        Robot-authored motion invalidates the baseline, but a full
        :meth:`reset` would also clear the rearm guard that makes one physical
        movement fire at most once.  A human who is still holding the head
        off-centre would then have their RELEASE stroke measured from a
        baseline anchored at the deflected pose and read as a full-amplitude
        gesture in the opposite direction, a second selection from one movement.
        """

        self._baseline = None
        self._settle = []
        self._require_quiescence = True
        self._triggered_high = True
        self._quiet_run = 0
        self._rise_armed = False
        # A suspend follows a movement that was already read, so the head is NOT
        # to be re-anchored wherever it stops (see _seen_rest): the human may
        # still be holding it.  Only genuine rest re-arms.
        self._seen_rest = True
        # A robot-authored move is itself the answer to a parked head.
        self._parked_without_direction = False
        # The ROBOT is moving the head, so whatever this detector last believed about a
        # human's hand is now unknowable.  Dropping the level here is safe because the
        # caller's trailing release timer still covers the second after a real hold.
        self._held = False
        self._cooldown_from_selection = False
        # ``_home`` deliberately SURVIVES this method: the robot moving its own
        # head does not move the head's mechanical neutral, and this is exactly
        # the window in which the anchor gets captured on a pose a human is
        # holding -- so it is the one memory left that can tell that anchor from
        # a real rest, and with it the release stroke from a turn.
        self._cooldown = max(self._cooldown, self.refractory_ticks)

    def _adopt_baseline(
        self, current: tuple[float, float, float]
    ) -> Optional[tuple[float, float, float]]:
        """The pose to anchor on, or None while the head is still moving.

        ALWAYS settle-gated.  Anchoring on the first sample after a reset is how
        the robot fired two spurious RIGHTs: the boot/self-motion
        tail was still decaying when the baseline was captured, so the remainder
        of that same decay measured as a full-amplitude human turn a few ticks
        later ("baseline established" -> d_roll=+0.248 on the very next sample).
        A baseline is only worth anything if the head was demonstrably still
        when it was taken.
        """

        # Captured BEFORE the adoption clears _require_quiescence below.  An
        # anchor taken post-suspend or while still latched may well be a pose a
        # HUMAN is holding, so it must not be allowed to redefine home -- that
        # is the whole mechanism behind the opposite-direction release fire.
        trusted = not self._require_quiescence and not self._triggered_high
        window = self._settle
        window.append(current)
        if len(window) > self.settle_ticks:
            del window[: -self.settle_ticks]
        if len(window) < self.settle_ticks:
            return None
        for axis in range(3):
            values = [sample[axis] for sample in window]
            if max(values) - min(values) > self.settle_epsilon_rad:
                return None
        settled = tuple(
            sum(sample[axis] for sample in window) / len(window) for axis in range(3)
        )
        self._settle = []
        self._require_quiescence = False
        # A fresh anchor has never been rested against: the rise-from-settled
        # rule starts over, so the very next sample cannot fire.
        self._quiet_run = 0
        self._rise_armed = False
        self._seen_rest = False
        # A new anchor supersedes whatever the head was parked at.
        self._parked_without_direction = False
        if trusted or self._home is None:
            self._home = settled  # type: ignore[assignment]
        return settled  # type: ignore[return-value]

    def _anchor_is_displaced(self, baseline: tuple[float, float, float]) -> bool:
        """Whether the anchor sits far enough from home to be somebody's hold."""

        home = self._home
        if home is None:
            return False
        limit = min(self.roll_threshold_rad, self.yaw_threshold_rad)
        return any(abs(baseline[axis] - home[axis]) > limit for axis in range(3))

    def _is_return_stroke(
        self,
        current: tuple[float, float, float],
        baseline: tuple[float, float, float],
    ) -> bool:
        """Whether this deflection is a head being let go, not a head being turned.

        Scoped to the axis the movement is actually happening on, not to the
        largest displacement anywhere: the robot legitimately parks the head off
        neutral in PITCH (the session-start look-up is -14 deg = 0.24 rad), and a
        genuine roll push-turn made from that pose must still select a direction.  So
        this only bites when the axis being moved is itself displaced beyond a
        selection threshold and the movement closes that displacement by more
        than the hold jitter -- i.e. the head is travelling back towards home.
        """

        home = self._home
        if home is None:
            return False
        axis = max(range(3), key=lambda index: abs(current[index] - baseline[index]))
        displacement = baseline[axis] - home[axis]
        if abs(displacement) <= min(self.roll_threshold_rad, self.yaw_threshold_rad):
            return False
        stroke = current[axis] - baseline[axis]
        if stroke * displacement >= 0:
            return False  # moving further out, or not moving on this axis at all
        return abs(current[axis] - home[axis]) <= abs(displacement) - self.quiet_epsilon_rad

    def _reanchor(self, current: tuple[float, float, float]) -> bool:
        """Adopt a new settle-gated anchor here, if the head is demonstrably still."""

        settled = self._adopt_baseline(current)
        if settled is None:
            return False
        self._baseline = settled
        return True

    def _track_rest(
        self,
        current: tuple[float, float, float],
        baseline: tuple[float, float, float],
    ) -> None:
        """Arm the rise-from-settled rule once the head rests on the baseline."""

        if max(abs(current[axis] - baseline[axis]) for axis in range(3)) <= self.quiet_epsilon_rad:
            self._quiet_run += 1
            if self._quiet_run >= self.quiet_ticks:
                self._rise_armed = True
                self._seen_rest = True
        else:
            self._quiet_run = 0

    def _rebaseline_on_stale_anchor(self, current: tuple[float, float, float]) -> bool:
        """Re-anchor a baseline the head has never been at rest against.

        Without this, refusing to fire on a stale anchor would make the detector
        permanently deaf: the head comes to rest at a pose that is (falsely)
        "deflected" forever, the EMA never runs while deflected, and no later
        turn is ever seen.  So a displaced but demonstrably MOTIONLESS head that
        we never saw rise from rest becomes the new rest -- a re-baseline, not a
        selection, which is exactly what the decaying robot trajectories needed.

        Deliberately NOT applied once ``_seen_rest`` is true: after a real fire
        the human may still be holding the head off-centre, and re-anchoring
        there would read their release stroke as a full turn the other way.

        ``_parked_without_direction`` is the one exemption, because that state
        is the same deafness with none of the risk: the latch selected NOTHING,
        so there is nothing started for a release to tear down, and every veto
        ``detect_direction`` applies (pitch magnitude, opposed votes, dominance
        ratio) is invariant under negation -- the release stroke off the parked
        pose is the outbound stroke's mirror image, and is vetoed with it.
        """

        if self._seen_rest and not self._parked_without_direction:
            return False
        return self._reanchor(current)

    def _is_deflected(
        self,
        current: tuple[float, float, float],
        baseline: tuple[float, float, float],
    ) -> bool:
        """Whether any meaningful axis is still away from the settled baseline."""

        return (
            abs(current[0] - baseline[0]) >= self.roll_threshold_rad
            or abs(current[1] - baseline[1])
            >= min(self.yaw_threshold_rad, self.roll_threshold_rad)
            or abs(current[2] - baseline[2]) >= self.yaw_threshold_rad
        )

    def update(
        self,
        current: Optional[tuple[float, float, float]],
        *,
        armed: bool = True,
    ) -> GestureDirection | None:
        """Return one signed command on a clear rising edge, otherwise ``None``.

        ``armed=False`` is the hard self-motion guard: it discards all detector
        state, so the first settled sample after a robot-authored movement only
        establishes a new baseline.

        A fire needs BOTH halves of a real push-turn to have been observed:
        a settle-gated baseline (the head was still when it was anchored) and a
        rest on that baseline (``_rise_armed``) before the crossing.  A pose that
        is merely far from the anchor -- the decaying tail of a robot pose, a
        boot transient, a release stroke -- re-baselines instead of selecting a
        direction.

        ...and it must not be travelling back towards ``_home`` off an anchor the
        detector knows is displaced, which is the one case the two rules above
        cannot see: there the anchor itself was captured on the human's own hold,
        so the release reads as a perfectly-formed turn the other way.
        """

        if not armed:
            self.reset()
            return None
        if current is None:
            return None
        # Before the baseline check: a refractory window opened by a selection
        # has to keep running while we wait for the head to settle, or a
        # suspended detector could never rearm.
        if self._cooldown > 0:
            self._cooldown -= 1
        if self._baseline is None:
            # Still settling: there is no anchor to be deflected FROM, so the detector
            # genuinely cannot say whether a hand is on the head.
            self._held = False
            self._baseline = self._adopt_baseline(current)
            return None

        deflected = self._is_deflected(current, self._baseline)
        # A deflection, or the refractory window a selection just opened: both mean the
        # head is in play rather than available as an output device.
        self._held = deflected or (self._cooldown > 0 and self._cooldown_from_selection)
        self._track_rest(current, self._baseline)

        if deflected:
            if self._is_return_stroke(current, self._baseline):
                # The anchor is somewhere the head was PUT and the head is now
                # travelling back to neutral off it.  This is the release half
                # of one physical turn-and-release, whichever half of it this
                # detector happened to watch -- never a second selection.  Take
                # the neutral it is heading for as the new anchor (settle-gated,
                # so nothing is adopted until it actually stops there).
                self._reanchor(current)
                return None
            if not self._rise_armed:
                # The head is away from the anchor without ever having been at
                # rest on it: this is the TAIL of an older movement (settling
                # after a robot pose, a boot transient, a release stroke), not a
                # deliberate turn.  Never a selection.
                self._rebaseline_on_stale_anchor(current)
                return None
            if not self._triggered_high and self._cooldown == 0:
                self._triggered_high = True
                self._cooldown = self.refractory_ticks
                self._cooldown_from_selection = True
                # One rest, one selection: the next fire needs a fresh rest.
                self._rise_armed = False
                self._quiet_run = 0
                direction = detect_direction(
                    current,
                    self._baseline,
                    self.yaw_threshold_rad,
                    self.roll_threshold_rad,
                    self.ambiguity_ratio,
                )
                if direction is None:
                    # Latched, selected nothing, and a stiff head STAYS where it
                    # was pushed: without this the detector is deaf for good --
                    # the EMA only runs while undeflected, and the anchor can
                    # never be moved to the pose the head is parked at.
                    self._parked_without_direction = True
                    self._vetoed_latch = True
                return direction
            return None

        # Settled motion is the only time the EMA may move.  Do not absorb a
        # held pitch or ambiguous diagonal into the baseline.
        #
        # Rearm only once the refractory window has also elapsed: if the head
        # comes to rest part-way through a release stroke the baseline can
        # adopt there, and clearing the latch immediately would let the rest of
        # that same stroke fire the opposite direction.
        if self._cooldown == 0:
            self._triggered_high = False
        baseline = self._baseline
        alpha = self.baseline_alpha
        # Home follows the same drift the baseline does -- otherwise a stale home
        # would eventually read every anchor as "displaced" and eat genuine turns
        # in one direction.  But ONLY while the anchor is still near it: a head a
        # human is holding off-centre reads as settled too, and letting home creep
        # onto it over a few seconds of holding would give back the exact
        # opposite-direction release fire this exists to stop.
        if self._home is not None and not self._anchor_is_displaced(baseline):
            home = self._home
            self._home = tuple(
                home[index] + alpha * (current[index] - home[index]) for index in range(3)
            )  # type: ignore[assignment]
        self._baseline = tuple(
            baseline[index] + alpha * (current[index] - baseline[index])
            for index in range(3)
        )
        return None
