"""How the head is allowed to move towards a face, and what it does when she leaves.

``reachy_mini_ros2/gaze/controller.py`` is the only part of the look-at feature that
decides how the robot MOVES, and every rule in it exists because of a specific way the
head can misbehave:

* a slew anchor that advanced on a refusal -> the head lurches by the whole accumulated
  amount on the first tick the arbiter accepts;
* a roll of zero -> every authored tilt in the expression repertoire is flattened
  200 ms after it settles;
* a roll taken from the measured pose -> the head is commanded INTO a human's hand, and
  the ``measured - commanded`` residual on the roll axis goes to zero, which silently
  disables push-turn mode selection;
* a relax to the identity -> gaze becomes a second author of the robot's neutral and
  undoes ``start_session``'s deliberate -14 deg look-up.

None of those show up as an exception. They show up as a robot that is slightly wrong.

Loaded by PATH: ``ros2_ws`` is not on ``sys.path`` and putting it there would need ROS.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "gaze" / "controller.py")
_MODULE_NAME = "reachy_mini_gaze_controller_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


controller = _load()
_rpy = controller._matrix_to_rotation_rpy
_matrix = controller._xyzrpy_to_matrix


@dataclass
class _Outcome:
    """`arbiter.Outcome`, reduced to the two fields the controller reads."""

    accepted: bool
    error_code: str = ""


class FakeArbiter:
    """Records `set_target` and answers with whatever the test scripted.

    Not a mock: the refusal SEQUENCE is the thing under test - `busy` in the middle of
    a slew has to leave the head exactly where an uninterrupted slew would have had it
    one tick earlier, and that is only observable over several calls.
    """

    def __init__(self, answers: list[_Outcome] | None = None) -> None:
        self.calls: list[dict] = []
        self.answers = list(answers or [])
        # What the backend is COMMANDING: Pollen's `_last_target_head_pose` follows every
        # accepted set_target, and the controller anchors its slew on it. A refused
        # write leaves it where it was, exactly like the arbiter.
        self.commanded = np.eye(4)

    def set_target(self, **kwargs) -> _Outcome:
        self.calls.append(kwargs)
        outcome = self.answers.pop(0) if self.answers else _Outcome(True)
        if outcome.accepted:
            self.commanded = np.asarray(kwargs["head_pose"], dtype=float)
        return outcome

    @property
    def yaws(self) -> list[float]:
        return [_rpy(call["head_pose"])[2] for call in self.calls]

    @property
    def pitches(self) -> list[float]:
        return [_rpy(call["head_pose"])[1] for call in self.calls]


def _params(**overrides) -> "controller.GazeParams":
    return replace(controller.GazeParams(), **overrides)


def _armed(rest=None, **overrides):
    """A controller that has been switched on, with `rest` as its rest pose."""

    gaze = controller.GazeController(_params(**overrides))
    gaze.set_enabled(True, np.eye(4) if rest is None else rest)
    return gaze


def _tick(gaze, arbiter, target, commanded, now) -> str | None:
    """One tick from the limiter down: limit, write, apply the answer.

    This is the loop `component.py` runs; reproduced here so the controller's refusal
    policy is exercised exactly the way the component drives it.
    """

    limited = gaze.limit(target, commanded)
    if limited is None:
        return gaze.skip_counter
    outcome = arbiter.set_target(head_pose=limited, antennas=None, body_yaw=None,
                                 client="ros:gaze", lane="expression")
    return gaze.on_outcome(outcome.accepted, outcome.error_code, now)


# --- 1. the deadband ------------------------------------------------------------

def test_a_target_the_head_is_already_at_is_not_written():
    """A 5 Hz stream of `set_target`s that each ask for 0.5 deg is a motor duty cycle
    and a /diagnostics counter that both lie about what the robot is doing."""

    gaze = _armed()
    assert gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.01), np.eye(4)) is None


def test_a_deadbanded_tick_advances_no_state():
    """`_last_sent` must keep meaning "the last target the arbiter took". A deadband
    that quietly advanced it would let the head drift by 0.034 rad a tick for ever."""

    gaze = _armed()
    arbiter = FakeArbiter()
    small = _matrix(0, 0, 0, 0.0, 0.0, 0.01)
    for _ in range(20):
        assert _tick(gaze, arbiter, small, np.eye(4), 0.0) == controller.COUNTER_DEADBAND
    assert arbiter.calls == []

    # ...and a real target afterwards still starts from the commanded pose, not drift.
    assert _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4), 0.0) \
        == controller.COUNTER_TARGETS
    assert arbiter.yaws[0] == pytest.approx(0.05)


# --- 2. the slew ----------------------------------------------------------------

def test_a_big_step_arrives_over_twelve_ticks_and_never_faster():
    """0.6 rad at 0.05 rad/tick is 12 ticks = 2.4 s: a head following a person rather
    than snapping to one, and ten 5 mrad control-loop steps per tick on the XL330s."""

    gaze = _armed(max_yaw_rad=1.0)
    arbiter = FakeArbiter()
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.6)

    counters = [_tick(gaze, arbiter, target, arbiter.commanded, float(i)) for i in range(20)]
    assert counters.count(controller.COUNTER_TARGETS) == 12
    assert counters[12] == controller.COUNTER_DEADBAND

    previous = 0.0
    for yaw in arbiter.yaws:
        assert abs(yaw - previous) <= gaze._params.max_step_rad + 1e-12
        previous = yaw
    assert arbiter.yaws[-1] == pytest.approx(0.6)


def test_the_clamp_is_the_last_word_on_how_far_the_head_turns():
    """The frame edge asks for 41 deg at the corrected fx and `look_around` sweeps 18.
    The clamp is what stops one false detection at the edge throwing the head to a
    mechanical limit."""

    gaze = _armed()
    arbiter = FakeArbiter()
    for i in range(40):
        _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 1.2, 1.4), arbiter.commanded, float(i))
    assert max(arbiter.yaws) == pytest.approx(gaze._params.max_yaw_rad)
    assert max(arbiter.pitches) == pytest.approx(gaze._params.max_pitch_rad)


def test_the_yaw_sign_can_be_inverted_without_touching_the_geometry():
    """The geometry is measured and certain; the wiring of nine servos is not, and
    `driver.py::_oriented` already records this robot surprising us on a sign."""

    plain = _armed()
    inverted = _armed(invert_yaw=True)
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.4)
    assert _rpy(plain.limit(target, np.eye(4)))[2] == pytest.approx(0.05)
    assert _rpy(inverted.limit(target, np.eye(4)))[2] == pytest.approx(-0.05)


# --- 3. roll ---------------------------------------------------------------------

@pytest.mark.parametrize("commanded_roll", [0.0, 0.09, -0.09])
def test_the_emitted_roll_is_the_commanded_one(commanded_roll):
    """Gaze authors no roll and erases none. Carrying the command back is a no-op by
    construction, which is what leaves the push-turn residual identical to today's."""

    gaze = _armed()
    commanded = _matrix(0, 0, 0, commanded_roll, 0.0, 0.0)
    emitted = gaze.limit(_matrix(0, 0, 0, 0.3, 0.0, 0.4), commanded)
    assert _rpy(emitted)[0] == pytest.approx(commanded_roll, abs=1e-9)


def test_the_measured_roll_never_reaches_the_target():
    """The critique's proposed fix, and why it is not adopted: commanding the measured
    roll drives the `measured - commanded` residual on the roll axis to zero - and that
    residual is the entire signal push-turn selects modes with."""

    gaze = _armed()
    commanded = _matrix(0, 0, 0, 0.0, 0.0, 0.0)
    measured_roll = 0.236                       # a stiff head, deflected by a hand
    emitted = gaze.limit(_matrix(0, 0, 0, measured_roll, 0.0, 0.4), commanded)
    assert _rpy(emitted)[0] == pytest.approx(0.0, abs=1e-9)


# --- 4. what the arbiter answered -----------------------------------------------

def test_busy_does_not_advance_the_slew_anchor():
    """`busy` is the common and CORRECT case - it is what makes gaze yield for every
    add-on pose and cue gesture. The head did not go where we asked, so committing
    would spend the step budget on a move that never happened and the next accepted
    tick would lurch by the accumulated amount."""

    gaze = _armed(max_yaw_rad=1.0)
    arbiter = FakeArbiter([_Outcome(False, "busy"), _Outcome(True)])
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.6)

    assert _tick(gaze, arbiter, target, np.eye(4), 1.0) == "gaze_skipped_busy"
    assert _tick(gaze, arbiter, target, np.eye(4), 2.0) == controller.COUNTER_TARGETS
    assert arbiter.yaws == pytest.approx([0.05, 0.05])


def test_head_reserved_re_anchors_on_the_commanded_pose():
    """A human has hold of the head. The next accepted tick must slew from wherever
    they left it, not from where gaze last thought it was."""

    gaze = _armed(max_yaw_rad=1.0)
    arbiter = FakeArbiter()
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.9)
    for i in range(4):
        assert _tick(gaze, arbiter, target, arbiter.commanded, float(i)) \
            == controller.COUNTER_TARGETS
    assert arbiter.yaws[-1] == pytest.approx(0.20)

    arbiter.answers = [_Outcome(False, "head_reserved")]
    assert _tick(gaze, arbiter, target, np.eye(4), 5.0) == "gaze_skipped_head_reserved"

    # The hand left the head at 0.7 rad; the next write starts from THERE.
    moved = _matrix(0, 0, 0, 0.0, 0.0, 0.7)
    assert _tick(gaze, arbiter, target, moved, 6.0) == controller.COUNTER_TARGETS
    assert arbiter.yaws[-1] == pytest.approx(0.75)


def test_an_unnamed_refusal_still_gets_a_counter():
    """`backend_error`, `bad_lane`, `dropped` - anything the arbiter grows later gets
    a line on /diagnostics rather than vanishing."""

    gaze = _armed()
    assert gaze.on_outcome(False, "backend_error", 1.0) == "gaze_skipped_backend_error"
    assert gaze.on_outcome(False, "", 1.0) == "gaze_skipped_unknown"


def test_gaze_active_falls_one_second_after_the_last_accepted_write():
    gaze = _armed()
    assert gaze.is_active(0.0) is False
    gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4))
    gaze.commit(100.0)
    assert gaze.is_active(100.5) is True
    assert gaze.is_active(100.99) is True
    assert gaze.is_active(101.01) is False


# --- 5. losing the face ---------------------------------------------------------

@pytest.mark.parametrize("elapsed", [0.0, 1.0, 3.0, 10.0, 600.0])
def test_a_face_that_was_never_seen_is_never_written_to(elapsed):
    """The no-face case - learner off-camera, tee down, detector unavailable - must not
    move the head AT ALL. Otherwise gaze undoes `start_session`'s -14 deg look-up ("so
    the robot does not open a conversation staring at the learner's waist") at exactly
    the moment nothing else is working either."""

    gaze = _armed(rest=_matrix(0, 0, 0, 0.0, -0.244, 0.0))
    assert gaze.has_seen_a_face is False
    assert gaze.handle_loss(elapsed, np.eye(4)) is None


def test_a_short_gap_leaves_the_head_where_it_is():
    """A blink, a turn, one badly lit frame. The tracker still holds its lock for 4.0 s,
    so a re-acquisition inside the window resumes with no visible transient."""

    gaze = _armed()
    gaze.note_seen(0.0)
    for elapsed in (0.2, 1.0, 1.49, 2.0, 3.9):
        assert gaze.handle_loss(elapsed, np.eye(4)) is None


def test_past_the_relax_the_head_returns_to_the_pose_gaze_was_armed_with():
    """NOT the identity. In a training session the rest pose is the -14 deg look-up; in
    an add-on mode it is that mode's feedback pose. Relaxing to a neutral would make
    gaze a second author of the robot's neutral, which is `_recenter_head`'s job."""

    rest = _matrix(0, 0, 0, 0.0, -0.244, 0.0)
    gaze = _armed(rest=rest, max_yaw_rad=1.0)
    arbiter = FakeArbiter()

    # Look 0.3 rad to one side, then lose the face.
    gaze.note_seen(0.0)
    for i in range(8):
        _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 0.0, 0.3), arbiter.commanded, float(i))
    assert arbiter.yaws[-1] == pytest.approx(0.3)

    commanded = _matrix(0, 0, 0, 0.0, 0.0, 0.3)
    emitted = []
    for tick in range(40):
        target = gaze.handle_loss(4.0 + tick * 0.2, commanded)
        if target is None:
            break
        emitted.append(_rpy(target))
        gaze.commit(4.0 + tick * 0.2)
        commanded = target

    assert emitted, "the head must not be left staring at an empty chair"
    assert emitted[-1][1] == pytest.approx(-0.244, abs=0.04)
    assert emitted[-1][2] == pytest.approx(0.0, abs=0.04)
    for previous, current in zip(emitted, emitted[1:]):
        assert abs(current[2] - previous[2]) <= gaze._params.max_step_rad + 1e-12


def test_once_at_rest_the_controller_says_nothing_at_all():
    """No idle stream. A 5 Hz `set_target` into an empty room would inflate the ledger
    for ever and hold the motors at a duty cycle nobody asked for."""

    rest = _matrix(0, 0, 0, 0.0, -0.244, 0.0)
    gaze = _armed(rest=rest)
    gaze.note_seen(0.0)
    commanded = np.eye(4)
    for tick in range(60):
        target = gaze.handle_loss(4.0 + tick * 0.2, commanded)
        if target is not None:
            gaze.commit(4.0 + tick * 0.2)
            commanded = target
    assert gaze.handle_loss(100.0, commanded) is None
    assert gaze.handle_loss(1000.0, commanded) is None


def test_a_re_acquisition_inside_the_hold_cancels_the_relax():
    gaze = _armed(rest=_matrix(0, 0, 0, 0.0, -0.244, 0.0))
    gaze.note_seen(0.0)
    assert gaze.handle_loss(2.0, np.eye(4)) is None
    gaze.note_seen(2.2)
    assert gaze.handle_loss(4.5, np.eye(4)) is None, "the clock restarted"
    assert gaze.handle_loss(6.5, np.eye(4)) is not None


def test_lost_for_reports_zero_before_the_first_lock():
    gaze = _armed()
    assert gaze.lost_for(500.0) == 0.0
    gaze.note_seen(500.0)
    assert gaze.lost_for(502.5) == pytest.approx(2.5)


# --- 6. the run flag ------------------------------------------------------------

def test_disabling_walks_the_head_back_once_and_then_stops():
    """The run flag going false is the end of a session or an add-on mode. Leaving
    the head aimed at where the person was standing is the failure the relax exists to
    prevent, and it is the same failure whether the face left or the flag did."""

    rest = _matrix(0, 0, 0, 0.0, -0.244, 0.0)
    gaze = _armed(rest=rest, max_yaw_rad=1.0)
    arbiter = FakeArbiter()
    gaze.note_seen(0.0)
    for i in range(8):
        _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 0.0, 0.3), arbiter.commanded, float(i))

    gaze.set_enabled(False)
    commanded = _matrix(0, 0, 0, 0.0, 0.0, 0.3)
    steps = 0
    while (target := gaze.go_quiet(commanded)) is not None:
        gaze.commit(float(steps))
        commanded = target
        steps += 1
        assert steps < 50, "the walk back must terminate"
    assert steps > 0
    assert _rpy(commanded)[1] == pytest.approx(-0.244, abs=0.04)
    assert gaze.go_quiet(commanded) is None


def test_disabling_a_controller_that_never_moved_writes_nothing():
    """An idle kiosk that was armed and disarmed without anybody appearing. The pose it
    was armed with is stale by then, and the head belongs to somebody else."""

    gaze = _armed(rest=_matrix(0, 0, 0, 0.0, -0.244, 0.0))
    gaze.set_enabled(False)
    assert gaze.go_quiet(_matrix(0, 0, 0, 0.0, 0.3, 0.0)) is None


def test_re_arming_forgets_the_previous_session():
    """A new rest pose, a clean anchor, and no face seen yet - otherwise the first tick
    of a new session slews from wherever the last one ended."""

    gaze = _armed(rest=_matrix(0, 0, 0, 0.0, -0.244, 0.0))
    gaze.note_seen(0.0)
    gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4))
    gaze.commit(0.0)

    fresh = _matrix(0, 0, 0, 0.0, 0.07, 0.0)
    gaze.set_enabled(True, fresh)
    assert gaze.has_seen_a_face is False
    assert np.allclose(gaze.rest_pose, fresh)
    assert gaze.handle_loss(100.0, np.eye(4)) is None


# --- 6a. the mechanism sign, and the two poses it must not be applied to ----------

def test_the_relax_returns_to_the_rest_pose_and_not_its_mirror_image():
    """`invert_yaw` corrects an aim computed from PIXELS. `_rest_pose` is a pose the
    robot was already commanded - the mechanism sign is in it once already - so
    inverting it again walks the head to -yaw. An add-on's feedback poses may set yaw, and
    a real robot may well need `invert_yaw=True`, at which point
    every session and every add-on mode would end parked at the mirror image."""

    rest = _matrix(0, 0, 0, 0.0, 0.0, 0.30)
    gaze = _armed(rest=rest, invert_yaw=True, max_yaw_rad=1.0)

    # Look away from the rest pose, then lose the face for good.
    gaze.note_seen(0.0)
    arbiter = FakeArbiter()
    for i in range(6):
        _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 0.0, -0.3), arbiter.commanded, float(i))

    commanded = arbiter.calls[-1]["head_pose"]
    for tick in range(60):
        target = gaze.handle_loss(4.0 + tick * 0.2, commanded)
        if target is None:
            break
        gaze.commit(4.0 + tick * 0.2)
        commanded = target
    assert _rpy(commanded)[2] == pytest.approx(0.30, abs=0.04)


def test_the_run_flag_walk_back_is_not_mirrored_either():
    """The same pose, the same rest capture, the other exit. `go_quiet` is reached both
    by the relax and by the flag going false, and neither may mirror."""

    rest = _matrix(0, 0, 0, 0.0, 0.0, 0.30)
    gaze = _armed(rest=rest, invert_yaw=True, max_yaw_rad=1.0)
    gaze.note_seen(0.0)
    arbiter = FakeArbiter()
    for i in range(6):
        _tick(gaze, arbiter, _matrix(0, 0, 0, 0.0, 0.0, -0.3), arbiter.commanded, float(i))

    gaze.set_enabled(False)
    commanded = arbiter.calls[-1]["head_pose"]
    steps = 0
    while (target := gaze.go_quiet(commanded)) is not None:
        gaze.commit(float(steps))
        commanded = target
        steps += 1
        assert steps < 50, "the walk back must terminate"
    assert _rpy(commanded)[2] == pytest.approx(0.30, abs=0.04)


def test_the_geometry_target_is_still_mirrored():
    """...and the guard above must not have disabled the knob it guards."""

    gaze = _armed(invert_yaw=True)
    emitted = gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4))
    assert _rpy(emitted)[2] == pytest.approx(-0.05)


# --- 6b. a backend that will not say what it is commanding ------------------------

def test_a_missing_command_carries_the_last_known_roll_rather_than_flattening_it():
    """`commanded_head_pose()` returns None on a backend before its first IK tick, and
    permanently on one that exposes neither target attribute. Emitting roll 0.0 there
    is the defect the roll carry exists to prevent - the expression lane's authored
    tilt, flattened, 200 ms after it settles."""

    gaze = _armed(max_yaw_rad=1.0)
    authored = _matrix(0, 0, 0, 0.09, 0.0, 0.0)
    first = gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.6), authored)
    assert _rpy(first)[0] == pytest.approx(0.09, abs=1e-9)
    gaze.commit(0.0)

    blind = gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.6), None)
    assert blind is not None, "an anchor exists; only the command is missing"
    assert _rpy(blind)[0] == pytest.approx(0.09, abs=1e-9)
    assert _rpy(blind)[2] == pytest.approx(0.10)


def test_the_first_tick_with_no_anchor_and_no_command_writes_nothing():
    """Anchoring at (0, 0) would claim the head is at neutral, so the first write would
    jump it to within one step of neutral from wherever it actually is. Skipping one
    200 ms tick is the only honest answer, and it gets its OWN counter: an operator
    reading `gaze_deadband` must not be told "the head is already right"."""

    gaze = _armed()
    assert gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.6), None) is None
    assert gaze.skip_counter == controller.COUNTER_SKIPPED_NO_ANCHOR
    assert controller.COUNTER_SKIPPED_NO_ANCHOR != controller.COUNTER_DEADBAND

    # ...and the moment a command arrives the tick works normally again.
    assert gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.6), np.eye(4)) is not None


def test_a_deadbanded_tick_still_names_the_deadband_counter():
    gaze = _armed()
    assert gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.01), np.eye(4)) is None
    assert gaze.skip_counter == controller.COUNTER_DEADBAND


# --- 6c. re-anchoring on the edge of somebody else's move -------------------------

def test_a_move_that_ended_between_two_ticks_costs_one_step_and_not_the_backlog():
    """The component polls `motion_snapshot()` and calls `reanchor()` BEFORE the target
    is built, so this is the controller half of that contract: after a re-anchor the
    next emitted target is `commanded +- max_step_rad`, never `stale anchor +-
    max_step_rad`. Get it wrong and a cue gesture that moved the head 0.55 rad is paid
    back in one 200 ms tick."""

    gaze = _armed(max_yaw_rad=1.0)
    arbiter = FakeArbiter()
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.9)
    for i in range(6):
        _tick(gaze, arbiter, target, arbiter.commanded, float(i))
    assert arbiter.yaws[-1] == pytest.approx(0.30)

    # A cue gesture ran and left the head at -0.25; gaze's ticks during it never even
    # reached the arbiter (they were deadbanded), so only the edge poll knows.
    gaze.reanchor()
    moved = _matrix(0, 0, 0, 0.0, 0.0, -0.25)
    assert _tick(gaze, arbiter, target, moved, 9.0) == controller.COUNTER_TARGETS
    assert arbiter.yaws[-1] == pytest.approx(-0.20)
    assert abs(arbiter.yaws[-1] - _rpy(moved)[2]) <= gaze._params.max_step_rad + 1e-12



# --- 7. the module must stay pure ------------------------------------------------

def test_the_controller_imports_nothing_it_cannot_be_tested_without():
    """`rclpy` is not installed on any dev box, and neither is GStreamer. A relative
    import would also break the by-path loading this whole file depends on."""

    import ast

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: this module is loaded by path"
            if node.module:
                top_level.add(node.module.split(".")[0])
    assert top_level <= {"__future__", "dataclasses", "typing", "numpy"}, top_level


def test_a_move_gaze_never_saw_is_still_the_slew_anchor():
    """A 0.4 s recentring GoTo can fall between two 3 Hz detections, so the edge poll
    never sees `head_moving`; a next write slewing from gaze's own stale anchor would
    be a 0.4 rad slam at 1.8 rad/s with a 0.5 rad residual. The commanded pose is
    the anchor whenever the backend reports one, so the write after ANY other author's
    move is one step from where that author left the head, edge seen or not."""

    gaze = _armed(max_yaw_rad=1.0)
    arbiter = FakeArbiter()
    target = _matrix(0, 0, 0, 0.0, 0.0, 0.9)
    for i in range(6):
        _tick(gaze, arbiter, target, arbiter.commanded, float(i))
    assert arbiter.yaws[-1] == pytest.approx(0.30)

    # Another writer parks the head at -0.25 between two ticks; NO reanchor() call.
    moved = _matrix(0, 0, 0, 0.0, 0.0, -0.25)
    assert _tick(gaze, arbiter, target, moved, 9.0) == controller.COUNTER_TARGETS
    assert arbiter.yaws[-1] == pytest.approx(-0.20)


# --- the aim a speech gesture is centred on -------------------------------------

def test_the_aim_is_the_goal_not_the_slew_step():
    """A gesture is centred on where the person IS, not on how far the slew has got:
    `aim` has the limiter's sign and clamp and none of its step or deadband."""

    gaze = _armed()
    target = _matrix(0, 0, 0, 0.0, -0.2, 0.3)
    pitch, yaw = gaze.aim(target)
    assert (pitch, yaw) == (pytest.approx(-0.2), pytest.approx(0.3))
    # ...and asking leaves the slew exactly where it was.
    assert _rpy(gaze.limit(target, np.eye(4)))[2] == pytest.approx(0.05)


def test_the_aim_obeys_the_clamp_and_the_mechanism_sign():
    gaze = _armed(invert_yaw=True)
    pitch, yaw = gaze.aim(_matrix(0, 0, 0, 0.0, 1.2, 1.4))
    assert pitch == pytest.approx(gaze._params.max_pitch_rad)
    assert yaw == pytest.approx(-gaze._params.max_yaw_rad)


def test_a_recorded_move_gets_the_aims_yaw_only_by_default():
    """Pollen's emotions library already spends its full +/-22.5 deg of pitch: composing
    the learner's look-up pitch as well put 16% of its frames out of reach at a robot's
    likely aim (freeze-then-snap), against 6.1-6.6% for yaw only."""

    assert controller.GazeParams().anchor_recorded == "yaw"


# --- the follower: glide, don't lurch ---------------------------------------------

def test_a_follower_step_is_a_fraction_of_the_error_capped_by_the_speed_limit():
    """Without a follower the head moves "in small bursts, with major delays": one
    0.05 rad jump per 3 Hz detection. A follower step at 25 Hz is `ease` of what is
    left, never more than max_speed * dt."""

    gaze = _armed()
    dt = 1.0 / 25.0
    far = _rpy(gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4), servo_dt=dt))[2]
    assert far == pytest.approx(gaze._params.max_speed_rad_s * dt)        # capped
    near = _rpy(gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.05), np.eye(4), servo_dt=dt))[2]
    assert near == pytest.approx(gaze._params.ease * 0.05)                # eased
    # The detection-rate path is untouched.
    assert _rpy(gaze.limit(_matrix(0, 0, 0, 0.0, 0.0, 0.4), np.eye(4)))[2] == \
        pytest.approx(gaze._params.max_step_rad)


def test_the_follower_converges_smoothly_and_lands_inside_the_deadband():
    gaze = _armed()
    arbiter = FakeArbiter()
    target = _matrix(0, 0, 0, 0.0, -0.1, 0.3)
    steps = []
    for i in range(200):
        limited = gaze.limit(target, arbiter.commanded, servo_dt=1.0 / 25.0)
        if limited is None:
            break
        before = _rpy(arbiter.commanded)[2]
        arbiter.set_target(head_pose=limited, antennas=None, body_yaw=None)
        gaze.on_outcome(True, "", float(i))
        steps.append(_rpy(arbiter.commanded)[2] - before)
    assert i < 60, "a 17 deg turn should settle well inside 2.4 s"
    assert max(steps) <= gaze._params.max_speed_rad_s / 25.0 + 1e-9
    # Soft landing: the last step is a small fraction of the first.
    assert steps[-1] < 0.3 * steps[0]
    assert abs(_rpy(arbiter.commanded)[2] - 0.3) < gaze._params.deadband_rad
    assert abs(_rpy(arbiter.commanded)[1] + 0.1) < gaze._params.deadband_rad


def test_the_follower_relax_goal_waits_for_relax_s_then_is_the_rest_pose():
    rest = _matrix(0, 0, 0, 0.0, -0.24, 0.0)
    gaze = _armed(rest=rest)
    assert gaze.relax_goal(0.0) is None                     # never locked: never move
    gaze.note_seen(10.0)
    assert gaze.relax_goal(10.0 + gaze._params.relax_s - 0.1) is None
    np.testing.assert_allclose(gaze.relax_goal(10.0 + gaze._params.relax_s), rest)
    gaze.mark_relaxed()
    assert gaze.relax_goal(99.0) is None
