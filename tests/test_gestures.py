"""Tests for the physical head-tilt gesture detector (screenless session control)."""

from __future__ import annotations

from emori_adapter.hostapp.gestures import (
    DirectionalGestureDetector,
    GestureDetector,
    _head_angles,
    detect_direction,
    detect_deflection,
    GestureDirection,
)


# --- pure deflection function -------------------------------------------------

def test_no_deflection_within_threshold():
    assert detect_deflection((0.0, 0.05, 0.0), (0.0, 0.0, 0.0), threshold_rad=0.2) is False


def test_deflection_beyond_threshold_on_pitch():
    assert detect_deflection((0.0, 0.30, 0.0), (0.0, 0.0, 0.0), threshold_rad=0.2) is True


def test_deflection_on_any_axis():
    assert detect_deflection((0.25, 0.0, 0.0), (0.0, 0.0, 0.0), 0.2) is True   # roll
    assert detect_deflection((0.0, 0.0, -0.25), (0.0, 0.0, 0.0), 0.2) is True  # yaw


def test_head_angles_parsing():
    st = {"head_pose": {"roll": 0.1, "pitch": 0.2, "yaw": 0.3}}
    assert _head_angles(st) == (0.1, 0.2, 0.3)
    assert _head_angles({}) is None
    assert _head_angles(None) is None
    assert _head_angles({"head_pose": "bad"}) is None


# --- stateful detector --------------------------------------------------------

def _rest():
    return (0.0, 0.0, 0.0)


def test_first_sample_sets_baseline_no_fire():
    d = GestureDetector(threshold_rad=0.2)
    assert d.update(_rest()) is False           # establishes baseline


def test_single_tap_fires_once_on_rising_edge():
    d = GestureDetector(threshold_rad=0.2, refractory_ticks=5)
    d.update(_rest())                            # baseline
    assert d.update((0.0, 0.05, 0.0)) is False   # small jitter, no fire
    assert d.update((0.0, 0.30, 0.0)) is True    # deliberate nudge -> FIRE
    assert d.update((0.0, 0.32, 0.0)) is False   # still deflected -> latched, no re-fire
    assert d.update((0.0, 0.31, 0.0)) is False


def test_returns_to_baseline_then_rearms():
    d = GestureDetector(threshold_rad=0.2, refractory_ticks=2)
    d.update(_rest())
    assert d.update((0.0, 0.30, 0.0)) is True    # tap 1
    # return near baseline (re-arm) and let refractory elapse
    d.update(_rest())
    d.update(_rest())
    d.update(_rest())
    assert d.update((0.0, 0.30, 0.0)) is True    # tap 2 fires after re-arm


def test_refractory_blocks_immediate_second_fire():
    d = GestureDetector(threshold_rad=0.2, refractory_ticks=10)
    d.update(_rest())
    assert d.update((0.0, 0.30, 0.0)) is True
    # even if it dips and spikes fast, the refractory window suppresses a second fire
    d.update(_rest())
    assert d.update((0.0, 0.30, 0.0)) is False   # cooldown not elapsed


def test_baseline_tracks_slow_drift_not_taps():
    # a slow drift below threshold should NOT fire even after many ticks
    d = GestureDetector(threshold_rad=0.2, baseline_alpha=0.5)
    d.update(_rest())
    fired = False
    pitch = 0.0
    for _ in range(40):
        pitch += 0.01                            # very slow creep
        if d.update((0.0, pitch, 0.0)):
            fired = True
    assert fired is False                        # baseline absorbs the drift


def test_reset_clears_state():
    d = GestureDetector(threshold_rad=0.2)
    d.update(_rest())
    d.update((0.0, 0.30, 0.0))
    d.reset()
    assert d._baseline is None
    assert d.update(_rest()) is False            # baseline re-established, no fire


# --- signed direction detector -------------------------------------

def test_positive_yaw_is_right_and_negative_yaw_is_left():
    baseline = (0.0, 0.0, 0.0)
    assert detect_direction(
        (0.0, 0.0, 0.25), baseline, 0.20, 0.20, 1.4
    ) is GestureDirection.RIGHT
    assert detect_direction(
        (0.0, 0.0, -0.25), baseline, 0.20, 0.20, 1.4
    ) is GestureDirection.LEFT


def test_pitch_and_opposed_roll_yaw_are_ignored():
    baseline = (0.0, 0.0, 0.0)
    assert detect_direction((0.0, 0.30, 0.0), baseline, 0.20, 0.20, 1.4) is None
    assert detect_direction((0.25, 0.0, -0.25), baseline, 0.20, 0.20, 1.4) is None


def test_roll_uses_the_same_signed_direction_when_yaw_is_quiet():
    baseline = (0.0, 0.0, 0.0)
    assert detect_direction(
        (0.25, 0.0, 0.0), baseline, 0.20, 0.20, 1.4
    ) is GestureDirection.RIGHT
    assert detect_direction(
        (-0.25, 0.0, 0.0), baseline, 0.20, 0.20, 1.4
    ) is GestureDirection.LEFT


def test_a_push_split_between_roll_and_yaw_the_same_way_selects_that_side():
    """A clear LEFT can read roll -0.219 / yaw -0.163: both agreeing, neither alone
    past its threshold or 1.4x the other - and it must still select that side."""

    baseline = (0.0, 0.0, 0.0)
    assert detect_direction((-0.219, 0.07, -0.163), baseline, 0.20, 0.20, 1.4)         is GestureDirection.LEFT
    assert detect_direction((0.219, 0.07, 0.163), baseline, 0.20, 0.20, 1.4)         is GestureDirection.RIGHT


def test_a_split_push_still_needs_real_motion_on_both_axes_and_in_total():
    baseline = (0.0, 0.0, 0.0)
    # Agreeing, but one axis is only a wobble: the dominant axis alone decides, and
    # 0.18 is under its threshold.
    assert detect_direction((-0.18, 0.0, -0.05), baseline, 0.20, 0.20, 1.4) is None
    # Both real, agreeing, but together not clearly more than a threshold's worth.
    assert detect_direction((-0.15, 0.0, -0.15), baseline, 0.20, 0.20, 1.4) is None
    # Opposed axes are a release stroke or a diagonal - never a selection.
    assert detect_direction((0.22, 0.0, -0.17), baseline, 0.20, 0.20, 1.4) is None
    # A nod is still a nod.
    assert detect_direction((-0.2, 0.21, -0.2), baseline, 0.20, 0.20, 1.4) is None


def _rest_on(detector: DirectionalGestureDetector, pose=(0.0, 0.0, 0.0), ticks: int = 6):
    """Hold the head still long enough to anchor a baseline AND arm the rise rule."""

    for _ in range(ticks):
        assert detector.update(pose) is None


def test_one_deflection_fires_once_until_return_and_refractory():
    detector = DirectionalGestureDetector(refractory_ticks=2)
    _rest_on(detector)
    assert detector.update((0, 0, .3)) is GestureDirection.RIGHT
    assert detector.update((0, 0, .3)) is None
    assert detector.update((0, 0, 0)) is None
    assert detector.update((0, 0, 0)) is None
    assert detector.update((0, 0, 0)) is None
    assert detector.update((0, 0, -.3)) is GestureDirection.LEFT


def test_unarmed_self_motion_is_suppressed_and_rebaselined():
    detector = DirectionalGestureDetector()
    detector.update((0, 0, 0))
    assert detector.update((0, 0, .4), armed=False) is None
    # The first re-armed sample establishes a fresh baseline at the robot's
    # settled self-motion position rather than treating it as a human command.
    assert detector.update((0, 0, .4), armed=True) is None


def test_release_stroke_after_a_selection_cannot_pick_the_opposite_direction():
    """One physical turn must select once, even though it drops the baseline.

    A RIGHT turn is selected, the robot's answering pose sets ``player.busy`` ->
    ``reset_sensing()``, and the human's RELEASE stroke -- measured against a baseline
    re-anchored on the still-held, deflected pose -- must not read as a full-amplitude
    LEFT turn 1.4 s later.
    """

    detector = DirectionalGestureDetector()
    rest = (0.03, 0.05, 0.02)
    # settle_ticks to anchor + quiet_ticks to arm the rise rule: an idle robot
    # has been sitting still for minutes, so this costs a real turn nothing.
    for _ in range(6):
        assert detector.update(rest) is None

    # The human turns the stiff head towards their right and holds it there.
    assert detector.update((0.14, 0.05, 0.02)) is None
    assert detector.update((0.26, 0.04, 0.02)) is GestureDirection.RIGHT

    # The answer authors a robot pose, so the baseline is no longer trustworthy.
    detector.suspend_until_settled()

    # The human lets go.  These are the exact per-tick roll deltas the robot
    # logged at 14:13:24-25, which drove the old detector to emit LEFT.
    for roll in (0.260, 0.200, 0.142, 0.101, 0.029):
        assert detector.update((roll, 0.05, 0.02)) is None
    for _ in range(12):
        assert detector.update(rest) is None

    # ...and a genuine, separate turn is still accepted afterwards.
    assert detector.update((-0.25, 0.05, 0.02)) is GestureDirection.LEFT


def test_suspend_until_settled_refuses_to_baseline_on_a_moving_head():
    """The re-baseline must wait for a head that is demonstrably at rest.

    Anchoring on the first sample after robot-authored motion captures whatever
    pose a human is still holding, making every later sample deflected.
    """

    detector = DirectionalGestureDetector()
    assert detector.update((0.0, 0.0, 0.0)) is None
    detector.suspend_until_settled()

    # Still travelling: no baseline may be adopted, so nothing can fire.
    for yaw in (0.30, 0.24, 0.18, 0.12, 0.06):
        assert detector.update((0.0, 0.0, yaw)) is None

    # Held still -> baseline adopts here, so this pose reads as neutral.
    for _ in range(10):
        assert detector.update((0.0, 0.0, 0.0)) is None


# --- two spurious RIGHT selections ------------------------------------------------
#
# Both fired out of head motion that was SETTLING, not rising.  They cannot be
# rejected by demanding yaw-dominance: on this stiff-motor firmware a genuine
# operator push-turn also manifests as roll (measured d_roll=+0.236 for a turn
# the operator called LEFT, which is why EMORI_GESTURE_INVERT exists).  Only the
# shape of the trajectory in time separates them, so these replay it.


def _replay_deltas(detector: DirectionalGestureDetector, anchor_roll: float, deltas):
    """Feed absolute poses reconstructed from the logged d_roll values."""

    return [detector.update((anchor_roll + delta, 0.0, 0.010)) for delta in deltas]


def test_settling_head_right_after_a_baseline_does_not_select_a_mode():
    """15:40:43 -- d_roll=+0.248 on the sample after 'baseline established'.

    The anchor was taken mid-settle, so the remainder of the robot's own
    movement measured as a full-amplitude human turn one tick later.  A turn
    that is already past the threshold the first time it is seen has no
    beginning: it is the tail of something that started before the anchor.
    """

    detector = DirectionalGestureDetector()
    # The old detector anchored on the first sample it saw; give it the anchor
    # anyway so this pins the rise-from-settled rule on its own.
    anchor = 0.087
    for _ in range(detector.settle_ticks):
        assert detector.update((anchor, 0.0, 0.010)) is None
    assert detector._baseline is not None  # noqa: SLF001

    fired = _replay_deltas(
        detector,
        anchor,
        # rise to the logged peak, then the head keeps settling back down
        [0.248, 0.335, 0.300, 0.251, 0.190, 0.120, 0.061, 0.022, 0.006, 0.002],
    )

    assert fired == [None] * 10


def test_rising_tail_from_a_mid_motion_baseline_does_not_select_a_mode():
    """15:41:48 -- baseline at :46, then 0.077/0.175/0.229 latched a RIGHT.

    Unlike the first one this looks like a rise, and that is the trap: it never
    passed through rest.  Every sample after the anchor is already further out
    than the hold jitter, so the movement began before this baseline existed.
    """

    detector = DirectionalGestureDetector()
    anchor = 0.041
    for _ in range(detector.settle_ticks):
        assert detector.update((anchor, 0.0, 0.010)) is None

    fired = _replay_deltas(
        detector,
        anchor,
        [0.077, 0.175, 0.229, 0.335, 0.302, 0.240, 0.171, 0.098, 0.030, 0.004],
    )

    assert fired == [None] * 10


def test_no_baseline_is_adopted_while_the_head_is_still_travelling():
    """Guard (a): the anchor itself has to come from a demonstrably still head."""

    detector = DirectionalGestureDetector()

    for roll in (0.041, 0.118, 0.216, 0.376, 0.341, 0.283, 0.212, 0.140):
        assert detector.update((roll, 0.0, 0.010)) is None
        assert detector._baseline is None, "anchored on a head that was in flight"  # noqa: SLF001

    # It comes to rest -> NOW there is something to measure against...
    for _ in range(6):
        assert detector.update((0.070, 0.0, 0.010)) is None
    assert detector._baseline is not None  # noqa: SLF001

    # ...and a real turn off that rest is still a selection.
    assert detector.update((0.310, 0.0, 0.010)) is GestureDirection.RIGHT


def test_a_stale_anchor_rebaselines_instead_of_going_deaf():
    """Refusing to fire must not mean refusing to ever fire again.

    A head that comes to rest at a pose the detector never rested against would
    otherwise read as permanently deflected: the EMA only runs while undeflected,
    so nothing would ever re-anchor and the robot would stop responding to turns
    altogether -- a quieter failure than the false fire, and a worse one.
    """

    detector = DirectionalGestureDetector()
    # A brief plateau mid-motion is enough to satisfy the settle window, so a
    # stale anchor is still reachable -- it is just no longer able to fire.
    for _ in range(3):
        assert detector.update((0.300, 0.0, 0.0)) is None
    assert detector._baseline is not None  # noqa: SLF001

    # The motion resumes before the rise rule could arm, and parks far away.
    for roll in (0.372, 0.451, 0.528, 0.577):
        assert detector.update((roll, 0.0, 0.0)) is None
    for _ in range(8):
        assert detector.update((0.600, 0.0, 0.0)) is None

    # The parked pose became the new rest instead of a permanent deflection.
    assert abs(detector._baseline[0] - 0.600) < 0.01  # noqa: SLF001

    # A genuine turn from the NEW rest selects normally.
    assert detector.update((0.850, 0.0, 0.0)) is GestureDirection.RIGHT


def test_a_genuine_push_turn_from_rest_still_fires_without_extra_latency():
    """The real profile: settled -> rise -> hold, at the shipped thresholds.

    The rise-from-settled rule must cost a genuine turn nothing once the head
    has been resting, which it has been for the whole time nobody is touching
    the robot.  It fires on the FIRST sample at or past the threshold.
    """

    detector = DirectionalGestureDetector()
    rest = (0.030, 0.050, 0.020)
    for _ in range(6):
        assert detector.update(rest) is None

    # A firm operator turn: measured on the robot as roll, not yaw.
    assert detector.update((0.095, 0.050, 0.020)) is None
    assert detector.update((0.178, 0.050, 0.020)) is None
    assert detector.update((0.268, 0.050, 0.020)) is GestureDirection.RIGHT
    # Held there afterwards -> exactly one selection.
    for roll in (0.281, 0.276, 0.270):
        assert detector.update((roll, 0.050, 0.020)) is None


def test_a_genuine_left_push_turn_from_rest_still_fires():
    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None
    assert detector.update((-0.110, 0.0, 0.0)) is None
    assert detector.update((-0.245, 0.0, 0.0)) is GestureDirection.LEFT


# --- the release-stroke invariant ---------------------------------------------
#
#   no single physical turn-and-release may ever produce a fire in the direction
#   of the RELEASE stroke.
#
# What a direction starts must not start by accident, so the opposite direction has
# to be impossible, not unlikely.  The rearm guard alone does not deliver it: a turn made within
# ~0.6 s of the robot's own head motion is ANCHORED MID-TURN (sensing was
# suspended, so the first demonstrably-still pose is the one the human is
# holding), and the straighten-back off that anchor is a textbook full-amplitude
# turn the other way.


def _turn_and_release(detector, axis: int, peak: float, *, hold_ticks: int):
    """Play one physical turn-hold-release; return (phase, fired) per tick."""

    def pose(value: float) -> tuple[float, float, float]:
        angles = [0.0, 0.0, 0.0]
        angles[axis] = value
        return tuple(angles)  # type: ignore[return-value]

    timeline = []
    for fraction in (0.35, 0.70, 1.0):
        timeline.append(("out", detector.update(pose(peak * fraction))))
    for _ in range(hold_ticks):
        timeline.append(("out", detector.update(pose(peak))))
    # ...and the operator straightens the stiff head back to where it was.
    for fraction in (0.65, 0.35, 0.0):
        timeline.append(("back", detector.update(pose(peak * fraction))))
    for _ in range(8):
        timeline.append(("back", detector.update(pose(0.0))))
    return timeline


def test_no_turn_and_release_can_ever_fire_in_the_direction_of_the_release():
    """The invariant, swept at the SHIPPED thresholds over the shapes that matter.

    ``suspended`` is the case that matters most: the robot had just moved
    its own head, so the anchor lands on the human's hold and the release is
    measured off it.  The turn itself is then eaten -- deliberately, since a held
    head and a robot-parked head are the same samples -- but it must never come
    back as the opposite direction.
    """

    for axis in (0, 2):                       # roll (this robot's turn axis) and yaw
        for peak in (0.26, 0.35, -0.26, -0.35):
            for hold_ticks in (4, 12):
                for suspended in (False, True):
                    detector = DirectionalGestureDetector()
                    for _ in range(6):        # an idle robot: home is known
                        assert detector.update((0.0, 0.0, 0.0)) is None
                    if suspended:
                        detector.suspend_until_settled()
                    timeline = _turn_and_release(detector, axis, peak, hold_ticks=hold_ticks)

                    case = f"axis={axis} peak={peak} hold={hold_ticks} suspended={suspended}"
                    outbound = (GestureDirection.RIGHT if peak > 0
                                else GestureDirection.LEFT)
                    release = (GestureDirection.LEFT if peak > 0
                               else GestureDirection.RIGHT)
                    fires = [(phase, fired) for phase, fired in timeline if fired is not None]
                    assert all(fired is outbound for _phase, fired in fires), case
                    assert all(phase == "out" for phase, _fired in fires), case
                    assert release not in [fired for _phase, fired in fires], case
                    # Exactly-one, not at-most-one: a detector that simply never
                    # fires would satisfy the invariant and be useless.  A turn
                    # off a CLEAN anchor selects once; a turn the detector was
                    # suspended through is eaten, which is the documented cost.
                    assert len(fires) == (0 if suspended else 1), case


def test_a_turn_anchored_mid_hold_releases_without_selecting_the_opposite_mode():
    """The concrete failure, at the exact amplitudes the robot reports.

    The robot moves its own head; ~0.4 s later the operator turns the stiff head
    (measured d_roll=+0.236 for what they call LEFT) and holds it while nothing
    happens; then they put it back.  Without the ``_home`` rule that straighten-
    back would fire -- with EMORI_GESTURE_INVERT=1 as the OTHER direction.
    """

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None
    detector.suspend_until_settled()               # a robot pose

    turn = [detector.update((roll, 0.0, 0.0)) for roll in (0.09, 0.18, 0.26)]
    hold = [detector.update((0.26, 0.0, 0.0)) for _ in range(9)]
    assert turn + hold == [None] * 12, "an anchor taken mid-turn cannot be a selection"
    assert detector._baseline is not None  # noqa: SLF001
    assert abs(detector._baseline[0] - 0.26) < 0.01  # noqa: SLF001 - anchored on the hold
    assert abs(detector._home[0]) < 0.01, "the hold redefined home"  # noqa: SLF001

    release = [detector.update((roll, 0.0, 0.0)) for roll in (0.17, 0.09, 0.0)]
    assert release == [None] * 3

    # ...and the human's SECOND, clean turn works normally.
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None
    assert detector.update((0.26, 0.0, 0.0)) is GestureDirection.RIGHT


def test_a_turn_further_away_from_home_off_a_displaced_anchor_still_selects():
    """The rule is one-directional on purpose: only the way BACK is a release."""

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None
    detector.suspend_until_settled()
    for _ in range(9):                              # anchored on a displaced hold
        assert detector.update((0.26, 0.0, 0.0)) is None

    assert detector.update((0.50, 0.0, 0.0)) is GestureDirection.RIGHT


def test_a_robot_parked_pitch_does_not_eat_a_genuine_roll_turn():
    """Scoped to the axis being MOVED, not the largest displacement anywhere.

    The session-start look-up is -14 deg = 0.244 rad, i.e. further from home than
    a selection threshold, and the robot parks the head there for a whole
    session.  A rule that measured displacement across all axes would read every
    push-turn made from that pose as a release and the robot would go deaf.
    """

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None
    detector.suspend_until_settled()
    for _ in range(8):
        assert detector.update((0.0, -0.244, 0.0)) is None

    assert detector.update((0.28, -0.244, 0.0)) is GestureDirection.RIGHT


# --- a movement that latches but selects nothing --------------------------------


def test_a_movement_that_latches_without_a_direction_does_not_go_deaf():
    """A vetoed latch must not park the detector permanently.

    ``detect_direction`` refuses a nod, opposed axes and the ambiguity band --
    but the movement has already LATCHED, and on stiff motors the head stays
    where it was pushed.  The EMA only runs while undeflected and the post-fire
    re-anchor refusal applied to a fire that never happened, so the anchor could
    never move to the parked pose: every later turn, however genuine, was read
    against a baseline the head would never return to.
    """

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None

    # A pure nod: past the deflection threshold, refused by the classifier.
    parked = [detector.update((0.0, 0.25, 0.0)) for _ in range(12)]
    assert parked == [None] * 12

    # It says so exactly once, so the ticker can answer it with a recenter.
    assert detector.take_vetoed_latch() is True
    assert detector.take_vetoed_latch() is False

    # ...and even on a robot that cannot recenter, the parked pose became the new
    # rest, so a genuine turn made from there is still a selection.
    assert detector.update((0.30, 0.25, 0.0)) is GestureDirection.RIGHT


def test_a_pure_pitch_nod_never_selects_a_mode_however_it_ends():
    """The other half of the vetoed latch: recovering from a veto must not create a
    selection."""

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None

    nod = [detector.update((0.0, 0.25, 0.0)) for _ in range(15)]
    back = [detector.update((0.0, pitch, 0.0)) for pitch in (0.18, 0.10, 0.0)]
    back += [detector.update((0.0, 0.0, 0.0)) for _ in range(8)]

    assert nod + back == [None] * 26


def test_a_selection_never_reports_a_vetoed_latch():
    """The recenter signal must not fire for movements that DID select a direction.

    Those already recenter through the dispatch path; signalling twice would
    submit a competing motion against an add-on's own pose.
    """

    detector = DirectionalGestureDetector()
    for _ in range(6):
        assert detector.update((0.0, 0.0, 0.0)) is None

    assert detector.update((0.26, 0.0, 0.0)) is GestureDirection.RIGHT
    assert detector.take_vetoed_latch() is False


def test_directional_baseline_tracks_slow_drift_without_selecting_a_mode():
    detector = DirectionalGestureDetector(baseline_alpha=0.5)
    assert detector.update((0.0, 0.0, 0.0)) is None
    yaw = 0.0
    for _ in range(40):
        yaw += 0.01
        assert detector.update((0.0, 0.0, yaw)) is None
