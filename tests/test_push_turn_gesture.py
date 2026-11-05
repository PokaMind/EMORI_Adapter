"""Push-turn gesture mode: detect a firm head TURN against STIFF motors, then spring back.

The back-drive gesture needs the head in gravity_compensation (firmware-500 on daemon v1.9.0).
Push-turn keeps motors enabled (still posable) and reads a firm human turn (measured 14-28deg vs a
0.05deg hold jitter — well past the 0.20 rad DirectionalGestureDetector threshold). Because a stiff
head HOLDS where it is turned, the ticker must spring it back to neutral after a turn so the detector
re-arms — UNLESS an add-on pose already took the head (player busy), in which case that pose owns
the recenter.
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from emori_adapter.hostapp.gestures import GestureDirection


class _FakeDriver:
    def __init__(self) -> None:
        self.counters: Counter = Counter()
        self.player = SimpleNamespace(busy=False)
        self.tracker = SimpleNamespace(active=False)
        self.cmds: list[str] = []
        self.motions: list[list] = []
        self.touched = 0

    def enqueue_cmd(self, cmd: str) -> None:
        self.cmds.append(cmd)

    def submit_legacy_motion(self, intents, *, drop_if_busy=False) -> bool:
        self.motions.append(list(intents))
        return True

    def touch(self) -> None:
        self.touched += 1


class _NeverFire:
    def update(self, angles):
        del angles
        return False

    def reset(self) -> None:
        pass


class _FireOnce:
    def __init__(self, direction: GestureDirection) -> None:
        self._d = direction
        self._fired = False

    def update(self, angles, *, armed=True):
        del angles, armed
        if self._fired:
            return None
        self._fired = True
        return self._d

    def reset(self) -> None:
        self._fired = False

    def suspend_until_settled(self) -> None:
        self.suspended = getattr(self, "suspended", 0) + 1
        self._fired = False

    def take_vetoed_latch(self) -> bool:
        # This stub only ever returns a direction, so it never latches without one.
        return False


def _ticker(monkeypatch, driver, *, mode: str, on_direction):
    monkeypatch.setenv("EMORI_GESTURE_MODE", mode)
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    from emori_adapter.hostapp.driver import GestureTicker

    ticker = GestureTicker(driver, on_direction=on_direction)
    ticker.detector = _NeverFire()
    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)
    return ticker


def test_push_turn_springs_head_back_after_a_turn_with_no_addon_pose(monkeypatch):
    driver = _FakeDriver()
    fired: list[object] = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    ticker.process_pose((0.0, 0.0, 0.4))  # a firm left turn (yaw 0.4 rad)

    assert fired == [GestureDirection.LEFT]
    # a recenter motion was submitted so the next turn re-arms
    assert len(driver.motions) == 1
    intent = driver.motions[0][0]
    assert intent.kind == "goto" and intent.head is not None
    assert intent.head.yaw == 0.0 and intent.head.roll == 0.0


def test_push_turn_does_not_spring_back_when_an_addon_pose_took_the_head(monkeypatch):
    driver = _FakeDriver()

    def posing_on_direction(direction):
        # A configured add-on dispatches a pose synchronously, marking the player busy.
        driver.player.busy = True

    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=posing_on_direction)

    ticker.process_pose((0.0, 0.0, 0.4))

    # the add-on pose owns the head; the ticker must NOT add a competing recenter
    assert driver.motions == []


def test_busy_to_idle_transition_rearms_before_trusting_pose(monkeypatch):
    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)
    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)

    driver.player.busy = True
    ticker.process_pose((0.0, 0.0, 0.5))
    assert fired == []  # robot-authored motion never self-triggers

    driver.player.busy = False
    ticker.process_pose((0.0, 0.0, 0.5))
    assert fired == []  # transition tick: re-arm only, do NOT trust the pose

    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)
    ticker.process_pose((0.0, 0.0, 0.5))
    assert len(fired) == 1  # next tick: armed again, deliberate turn fires


def test_polling_loop_busy_fast_path_records_the_transition(monkeypatch):
    """The real ticker loop skips process_pose while busy, so it must mark the edge.

    Without this the busy->idle re-arm above would be inert on the robot: only the
    unit-test seam (a direct process_pose call) would ever set the flag.
    """

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)
    driver.player.busy = True

    class _OneTick:
        def __init__(self) -> None:
            self.ticks = 0

        def wait(self, _timeout):
            self.ticks += 1
            return self.ticks > 1  # run the body once, then stop the loop

        def set(self) -> None:
            pass

    ticker._stop = _OneTick()
    ticker._run()

    assert ticker._was_busy is True

    # ...and the first idle sample after it re-arms instead of firing.
    driver.player.busy = False
    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)
    ticker.process_pose((0.0, 0.0, 0.5))
    assert fired == []


def test_a_state_read_that_raises_still_records_the_busy_edge(monkeypatch):
    """The loop reads the state on every tick, so an unreachable node must not cost
    the LOCAL suppression bookkeeping.

    Letting the read escape to the loop's outer handler would skip the busy branch, so
    ``_was_busy`` would stay False for the whole outage and the first sample after the
    node came back would be trusted against a baseline taken before the robot moved --
    the spurious selection this ticker exists to avoid.
    """

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    class _Dead:
        def read_state(self):
            raise ConnectionError("node restarting")

    driver.client = _Dead()
    driver.player.busy = True

    class _OneTick:
        def __init__(self) -> None:
            self.ticks = 0

        def wait(self, _timeout):
            self.ticks += 1
            return self.ticks > 1

        def set(self) -> None:
            pass

    ticker._stop = _OneTick()
    ticker._run()

    assert ticker._was_busy is True
    assert fired == []


def test_back_drive_mode_never_springs_back(monkeypatch):
    driver = _FakeDriver()
    fired: list[object] = []
    ticker = _ticker(monkeypatch, driver, mode="back_drive", on_direction=fired.append)

    ticker.process_pose((0.0, 0.0, 0.4))

    assert fired == [GestureDirection.LEFT]
    assert driver.motions == []  # legacy behaviour unchanged


def test_idle_motion_stands_down_while_the_head_is_the_input_device(monkeypatch):
    """The idle scan must not fight push-turn sensing for the head.

    Measured on the robot: IdleTicker fired R.look_around every 5 s, which swings
    head yaw +-18 deg (0.31 rad) -- larger than the 0.20 rad selection
    threshold. Every one of those motions set player.busy, which wipes the
    directional baseline, and the next settled tick re-anchored the baseline
    ONTO whatever pose the head was left in. On a stiff push-turn head that
    silently absorbed the human's turn, so turning the head did nothing at all.
    """

    from emori_adapter.hostapp.driver import GestureTicker, IdleTicker

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    driver = _FakeDriver()
    driver.last_activity = 0.0
    ticker = GestureTicker(driver, on_direction=lambda _d: None)

    assert ticker.head_reserved_for_sensing() is True

    idle = IdleTicker(driver, idle_after_s=0.0, head_reserved=ticker.head_reserved_for_sensing)
    idle._tick()  # noqa: SLF001 - drive one iteration without the thread

    assert driver.motions == [], "idle motion moved the head while sensing was armed"

    # Training owns the head again, so the robot may look alive as before.
    driver.tracker.active = True
    assert ticker.head_reserved_for_sensing() is False
    idle._tick()  # noqa: SLF001
    assert len(driver.motions) == 1


def test_gesture_invert_maps_the_measured_side_onto_the_operators_side(monkeypatch):
    """A stiff head turns as ROLL, and this robot's coupling sign is inverted.

    Measured on the robot: the operator turned LEFT and the detector reported
    d_roll=+0.236 -> RIGHT, so without this every turn reads the opposite way.
    """

    driver = _FakeDriver()
    fired: list[object] = []
    monkeypatch.setenv("EMORI_GESTURE_INVERT", "1")
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)
    ticker.direction_detector = _FireOnce(GestureDirection.RIGHT)

    ticker.process_pose((0.24, 0.0, 0.0))

    assert fired == [GestureDirection.LEFT]


def test_gesture_direction_is_unchanged_without_the_invert_flag(monkeypatch):
    driver = _FakeDriver()
    fired: list[object] = []
    monkeypatch.delenv("EMORI_GESTURE_INVERT", raising=False)
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)
    ticker.direction_detector = _FireOnce(GestureDirection.RIGHT)

    ticker.process_pose((0.24, 0.0, 0.0))

    assert fired == [GestureDirection.RIGHT]


def test_robot_motion_suspends_sensing_instead_of_fully_resetting(monkeypatch):
    """Robot-authored motion must not clear the one-movement-fires-once guard.

    A full reset() here would let a single physical turn select BOTH directions: the
    robot's pose sets player.busy, the guard is wiped, and the human's release stroke
    selects the opposite direction ~1.4 s later.
    """

    driver = _FakeDriver()
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=lambda _d: None)

    ticker.reset_sensing()

    assert getattr(ticker.direction_detector, "suspended", 0) == 1


def _shape_env(monkeypatch) -> None:
    for name in (
        "EMORI_GESTURE_SETTLE_EPSILON_RAD",
        "EMORI_GESTURE_SETTLE_TICKS",
        "EMORI_GESTURE_QUIET_EPSILON_RAD",
        "EMORI_GESTURE_QUIET_TICKS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ticker_wires_the_trajectory_shape_guards_into_the_detector(monkeypatch):
    """The robot fix is inert unless the ticker's own detector carries it."""

    _shape_env(monkeypatch)
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    from emori_adapter.hostapp.driver import GestureTicker

    detector = GestureTicker(_FakeDriver(), on_direction=lambda _d: None).direction_detector

    assert (detector.settle_ticks, detector.quiet_ticks) == (3, 3)
    assert detector.settle_epsilon_rad == 0.03
    assert detector.quiet_epsilon_rad == 0.05


def test_trajectory_shape_guards_are_tunable_on_the_rig(monkeypatch):
    _shape_env(monkeypatch)
    monkeypatch.setenv("EMORI_GESTURE_QUIET_TICKS", "5")
    monkeypatch.setenv("EMORI_GESTURE_SETTLE_EPSILON_RAD", "0.02")
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    from emori_adapter.hostapp.driver import GestureTicker

    detector = GestureTicker(_FakeDriver(), on_direction=lambda _d: None).direction_detector

    assert detector.quiet_ticks == 5
    assert detector.settle_epsilon_rad == 0.02


def test_a_disabled_shape_guard_falls_back_to_the_measured_default(monkeypatch):
    """0 or garbage would silently remove the guard while reading as a retune."""

    _shape_env(monkeypatch)
    monkeypatch.setenv("EMORI_GESTURE_QUIET_TICKS", "0")
    monkeypatch.setenv("EMORI_GESTURE_QUIET_EPSILON_RAD", "not-a-number")
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    from emori_adapter.hostapp.driver import GestureTicker

    detector = GestureTicker(_FakeDriver(), on_direction=lambda _d: None).direction_detector

    assert detector.quiet_ticks == 3
    assert detector.quiet_epsilon_rad == 0.05


def test_a_quiet_epsilon_past_the_threshold_cannot_arm_and_fire_on_one_tick(monkeypatch):
    """A rest window wider than the selection threshold would defeat itself."""

    from emori_adapter.hostapp.gestures import DirectionalGestureDetector

    detector = DirectionalGestureDetector(quiet_epsilon_rad=0.9, settle_epsilon_rad=0.9)

    assert detector.quiet_epsilon_rad == 0.10
    assert detector.settle_epsilon_rad == 0.10


def test_a_non_finite_shape_knob_does_not_take_the_hostapp_down(monkeypatch):
    """NaN/inf survive float() and every guard after it, and then kill the boot.

    ``settle_ticks``/``quiet_ticks`` go through int(), which RAISES on nan and on
    inf -- inside GestureTicker.__init__, i.e. the hostapp does not start at all.
    A NaN epsilon is quieter and worse: every comparison against it is False, so
    every settle window counts as settled and the guard is gone while the trace
    still reads like a tuned rig.
    """

    _shape_env(monkeypatch)
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    monkeypatch.setenv("EMORI_GESTURE_SETTLE_TICKS", "nan")
    monkeypatch.setenv("EMORI_GESTURE_QUIET_EPSILON_RAD", "inf")
    monkeypatch.setenv("EMORI_GESTURE_SETTLE_EPSILON_RAD", "-inf")
    from emori_adapter.hostapp.driver import GestureTicker, gesture_shape_param

    assert gesture_shape_param("EMORI_GESTURE_SETTLE_TICKS") == 3
    assert gesture_shape_param("EMORI_GESTURE_QUIET_EPSILON_RAD") == 0.05
    assert gesture_shape_param("EMORI_GESTURE_SETTLE_EPSILON_RAD") == 0.03

    detector = GestureTicker(_FakeDriver(), on_direction=lambda _d: None).direction_detector

    assert (detector.settle_ticks, detector.quiet_ticks) == (3, 3)
    assert (detector.settle_epsilon_rad, detector.quiet_epsilon_rad) == (0.03, 0.05)


def _real_detector_ticker(monkeypatch):
    """A ticker running the SHIPPED detector at the SHIPPED defaults."""

    _shape_env(monkeypatch)
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    monkeypatch.delenv("EMORI_LANG_TOGGLE", raising=False)
    from emori_adapter.hostapp.driver import GestureTicker

    driver = _FakeDriver()
    fired: list[object] = []
    ticker = GestureTicker(driver, on_direction=fired.append)
    ticker.detector = _NeverFire()
    return driver, ticker, fired


def test_a_head_parked_by_a_movement_that_selected_nothing_is_recentred(monkeypatch):
    """The vetoed latch's other half: the ticker has to ANSWER it.

    A nod (or an opposed/ambiguous diagonal) latches, selects nothing, and leaves
    the stiff head parked off-neutral. Nothing else moves it -- the selection
    path never ran, so neither did the recenter that normally re-arms sensing --
    and the detector sits deflected against its own anchor. Recentring is the
    robot-authored motion that suspends, springs the head back and re-anchors on
    a clean neutral, which is the difference between one lost gesture and a robot
    that has stopped responding.
    """

    driver, ticker, fired = _real_detector_ticker(monkeypatch)

    for _ in range(6):
        ticker.process_pose((0.0, 0.0, 0.0))
    assert driver.motions == [], "an idle head must not be moved"

    ticker.process_pose((0.0, 0.25, 0.0))  # a nod: past threshold, no direction

    assert fired == [], "a nod must never read as a direction"
    assert len(driver.motions) == 1
    intent = driver.motions[0][0]
    assert intent.kind == "goto" and intent.reason == "push-turn recenter"
    assert intent.head.yaw == 0.0 and intent.head.roll == 0.0

    # Exactly once: the signal is an edge, not a level, or every tick of a held
    # nod would queue another competing motion.
    ticker.process_pose((0.0, 0.25, 0.0))
    assert len(driver.motions) == 1


def test_a_genuine_turn_still_selects_and_recenters_once(monkeypatch):
    """The vetoed-latch path must not have changed the ordinary one."""

    driver, ticker, fired = _real_detector_ticker(monkeypatch)

    for _ in range(6):
        ticker.process_pose((0.0, 0.0, 0.0))
    ticker.process_pose((0.26, 0.0, 0.0))

    assert fired == [GestureDirection.RIGHT]
    assert len(driver.motions) == 1
    assert driver.motions[0][0].reason == "push-turn recenter"


def test_idle_motion_is_unchanged_without_push_turn_sensing(monkeypatch):
    from emori_adapter.hostapp.driver import IdleTicker

    driver = _FakeDriver()
    driver.last_activity = 0.0
    idle = IdleTicker(driver, idle_after_s=0.0)

    idle._tick()  # noqa: SLF001

    assert len(driver.motions) == 1


# --- residual sensing: telling a HUMAN's hand from the robot's own motion ----------
#
# The v2 shim reports `motion.residual` = measured - commanded on /api/state/full. That
# one number is what lets push_turn stay awake while the robot moves: subtracting the
# robot's own command is strictly better than going deaf, which is what suppression did.
# A state without it is a v1 robot or Pollen's daemon and must behave byte for byte as
# before.


def _state(*, roll=0.0, pitch=0.0, yaw=0.0, motion=None) -> dict:
    state = {"head_pose": {"x": 0.0, "y": 0.0, "z": 0.0,
                           "roll": roll, "pitch": pitch, "yaw": yaw},
             "body_yaw": 0.0, "antennas_position": [0.0, 0.0]}
    if motion is not None:
        state["motion"] = motion
    return state


def _motion(*, active=False, head_moving=False, gaze_active=False,
            client="", lane="", residual=None, command=None) -> dict:
    block = {"active": active, "head_moving": head_moving, "client": client,
             "lane": lane, "gaze_active": gaze_active}
    if command is not None:
        block["command"] = {"roll": command[0], "pitch": command[1], "yaw": command[2]}
    if residual is not None:
        block["residual"] = {"roll": residual[0], "pitch": residual[1], "yaw": residual[2]}
    return block


def test_head_angles_prefers_the_residual_when_the_shim_reports_one(monkeypatch):
    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)
    from emori_adapter.hostapp.gestures import _head_angles

    state = _state(roll=0.30, pitch=0.30, yaw=0.30,
                   motion=_motion(command=(0.30, 0.30, 0.30), residual=(0.0, 0.0, 0.0)))

    # The head is 0.30 rad off neutral on every axis and NOBODY is touching it: the
    # robot put it there itself, and the sensor has to read exactly nothing.
    assert _head_angles(state) == (0.0, 0.0, 0.0)


def test_head_angles_falls_back_to_the_absolute_pose_on_a_v1_rig(monkeypatch):
    """Pollen's daemon and a v1 node answer without a `motion` key at all."""

    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)
    from emori_adapter.hostapp.gestures import _head_angles

    assert _head_angles(_state(roll=0.24)) == (0.24, 0.0, 0.0)
    # ...and so does a v2 node whose backend cannot report what it is commanding.
    assert _head_angles(_state(roll=0.24, motion=_motion())) == (0.24, 0.0, 0.0)
    # ...and a malformed residual degrades rather than blinding the sensor.
    broken = _state(roll=0.24, motion=_motion())
    broken["motion"]["residual"] = {"roll": "not-a-number"}
    assert _head_angles(broken) == (0.24, 0.0, 0.0)


def test_residual_sensing_is_revertible_with_one_environment_variable(monkeypatch):
    """EMORI_GESTURE_RESIDUAL=0 is the documented rollback for the whole change."""

    monkeypatch.setenv("EMORI_GESTURE_RESIDUAL", "0")
    from emori_adapter.hostapp.gestures import _head_angles

    state = _state(roll=0.30, motion=_motion(command=(0.30, 0.0, 0.0),
                                             residual=(0.0, 0.0, 0.0)))
    assert _head_angles(state) == (0.30, 0.0, 0.0)


def test_a_turn_fires_identically_whether_it_arrives_absolute_or_as_a_residual(monkeypatch):
    """The no-op-at-rest property, which is the entire safety argument.

    The same 0.236 rad roll deflection (the robot's measured push-turn amplitude) has to
    select the same mode whether it is read off a robot commanding nothing, or off a
    robot holding a 0.30 rad nose-up pose it authored itself. If those two disagree,
    every threshold in this file was tuned against a signal that no longer exists.
    """

    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)
    from emori_adapter.hostapp.gestures import _head_angles

    def _fire(states) -> list:
        driver, ticker, fired = _real_detector_ticker(monkeypatch)
        for state in states:
            ticker.process_pose(_head_angles(state), state.get("motion"))
        return fired

    rest = [_state() for _ in range(6)]
    absolute = _fire([*rest, _state(roll=0.236)])

    held = _motion(command=(0.0, 0.30, 0.0), residual=(0.0, 0.0, 0.0))
    turned = _motion(command=(0.0, 0.30, 0.0), residual=(0.236, 0.0, 0.0))
    residual = _fire([
        *[_state(pitch=0.30, motion=held) for _ in range(6)],
        _state(roll=0.236, pitch=0.30, motion=turned),
    ])

    assert absolute == [GestureDirection.RIGHT]
    assert residual == absolute


# --- suppression narrows to a running HEAD move -----------------------------------


def test_a_ros_authored_head_move_suppresses_and_rebaselines_like_player_busy(monkeypatch):
    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    moving = _motion(active=True, head_moving=True, client="ros:gaze", lane="expression")
    ticker.process_pose((0.0, 0.0, 0.5), moving)
    assert fired == []
    assert ticker._was_busy is True

    # ...and the busy->idle re-arm fires on the first tick after the move clears.
    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)
    ticker.process_pose((0.0, 0.0, 0.5), _motion())
    assert fired == []
    ticker.direction_detector = _FireOnce(GestureDirection.LEFT)
    ticker.process_pose((0.0, 0.0, 0.5), _motion())
    assert fired == [GestureDirection.LEFT]


def test_gaze_alone_never_suppresses_the_sensor(monkeypatch):
    """A directional gesture still works with a person present.

    If a live gaze stream suppressed sensing, the robot would stop reading head turns
    precisely while somebody was standing in front of it.
    """

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    ticker.process_pose((0.0, 0.0, 0.5), _motion(gaze_active=True))

    assert fired == [GestureDirection.LEFT]
    assert ticker._was_busy is False


def test_an_antenna_only_cue_does_not_suppress_the_sensor(monkeypatch):
    """`motion.active` with head_moving false is a phase cue on the antennas, which
    cannot move the measured pose -- the same line an add-on's feedback already draws."""

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    ticker.process_pose((0.0, 0.0, 0.5),
                        _motion(active=True, head_moving=False, client="hostapp"))

    assert fired == [GestureDirection.LEFT]


def test_a_long_run_with_gaze_live_still_selects_a_mode(monkeypatch):
    """Sixty ticks (six seconds) of a face being tracked, then a deliberate turn."""

    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)
    driver, ticker, fired = _real_detector_ticker(monkeypatch)
    live = _motion(active=False, gaze_active=True, client="ros:gaze", lane="expression",
                   command=(0.0, 0.0, 0.20), residual=(0.0, 0.0, 0.0))

    for _ in range(60):
        ticker.process_pose((0.0, 0.0, 0.0), live)
    assert fired == []

    turning = dict(live, residual={"roll": 0.236, "pitch": 0.0, "yaw": 0.0})
    ticker.process_pose((0.236, 0.0, 0.0), turning)

    assert fired == [GestureDirection.RIGHT]


def test_a_state_without_motion_behaves_exactly_as_it_did_before(monkeypatch):
    """v1 rollback: no motion block, no new behaviour."""

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)

    ticker.process_pose((0.0, 0.0, 0.4), None)

    assert fired == [GestureDirection.LEFT]


def test_the_polling_loop_reads_the_motion_block_and_suppresses_on_a_ros_head_move(
        monkeypatch):
    """The ticker's own loop, not the unit seam: a ROS-authored head move is invisible
    in `player.busy` and can only be seen in the state the loop already reads."""

    driver = _FakeDriver()
    fired: list = []
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=fired.append)
    driver.client = SimpleNamespace(read_state=lambda: _state(
        yaw=0.5, motion=_motion(active=True, head_moving=True, client="ros")))

    class _OneTick:
        def __init__(self) -> None:
            self.ticks = 0

        def wait(self, _timeout):
            self.ticks += 1
            return self.ticks > 1

        def set(self) -> None:
            pass

    ticker._stop = _OneTick()  # noqa: SLF001 - drive one loop iteration
    ticker._run()  # noqa: SLF001

    assert fired == []
    assert ticker._was_busy is True


def test_a_deferred_language_toggle_turn_is_still_flushed_while_suppressed(monkeypatch):
    """The double-turn window must keep running under the new suppressor too."""

    driver = _FakeDriver()
    ticker = _ticker(monkeypatch, driver, mode="push_turn", on_direction=lambda _d: None)
    flushed: list[int] = []
    ticker._flush_deferred_turn = lambda: flushed.append(1)  # noqa: SLF001 - seam

    ticker.process_pose((0.0, 0.0, 0.5),
                        _motion(active=True, head_moving=True, client="ros"))

    assert flushed == [1]
