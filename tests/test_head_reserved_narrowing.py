"""``head_reserved_for_sensing`` narrows to "a hand is on the head".

The predicate is NAMED "the head is an input device right now". As "push_turn is on and
no training session is running" it would be true at idle and in every add-on mode, i.e.
almost always. Three shipped consumers read it
and all three would stand down permanently on that answer: the per-sentence cue lane
(reason=head_reserved, a robot that does not move while it speaks), the /api/skill
intake, and the idle scan.

Narrowing it is only safe because the sensor does not go blind when the robot moves:
the v2 shim reports ``motion.residual`` = measured - commanded, so a robot-authored move
is subtracted rather than suppressed. So the predicate has two regimes, and this file
pins both -- including the automatic return to the old wide answer the moment the
residual stops arriving, which is what makes a v1 rollback a non-event.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from emori_adapter.behavior import AffectSnapshot
from emori_adapter.hostapp.gestures import GestureDirection


class _Clock:
    """A clock the test drives, so a 1.0 s release timer needs no sleep."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeDriver:
    def __init__(self) -> None:
        self.counters: Counter = Counter()
        self.player = SimpleNamespace(busy=False)
        self.tracker = SimpleNamespace(active=False)
        self.motions: list[list] = []

    def enqueue_cmd(self, cmd: str) -> None:
        pass

    def submit_legacy_motion(self, intents, *, drop_if_busy=False) -> bool:
        self.motions.append(list(intents))
        return True

    def touch(self) -> None:
        pass


class _StateClient:
    """A v2 shim that answers every poll with a settled head and a live residual.

    Advances the test clock by one tick per read, so a loop driven by ``_run`` ages
    exactly as the real 10 Hz sensing thread does.
    """

    def __init__(self, clock: _Clock, tick_s: float = 0.1) -> None:
        self._clock = clock
        self._tick_s = tick_s
        self.reads = 0

    def read_state(self) -> dict:
        self.reads += 1
        self._clock.advance(self._tick_s)
        return {"head_pose": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
                "motion": _motion(residual=(0.0, 0.0, 0.0))}


class _NTicks:
    """A stop event that lets ``GestureTicker._run`` run exactly ``n`` iterations."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.ticks = 0

    def wait(self, _timeout) -> bool:
        self.ticks += 1
        return self.ticks > self.n

    def set(self) -> None:
        pass


def _motion(*, residual=None, head_moving=False) -> dict:
    block = {"active": head_moving, "head_moving": head_moving, "client": "",
             "lane": "", "gaze_active": False}
    if residual is not None:
        block["command"] = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
        block["residual"] = {"roll": residual[0], "pitch": residual[1],
                             "yaw": residual[2]}
    return block


def _ticker(monkeypatch, *, clock, driver=None):
    """A ticker running the SHIPPED detector at the SHIPPED defaults."""

    for name in ("EMORI_GESTURE_SETTLE_EPSILON_RAD", "EMORI_GESTURE_SETTLE_TICKS",
                 "EMORI_GESTURE_QUIET_EPSILON_RAD", "EMORI_GESTURE_QUIET_TICKS",
                 "EMORI_GESTURE_BEGIN", "EMORI_LANG_TOGGLE", "EMORI_GESTURE_INVERT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    from emori_adapter.hostapp.driver import GestureTicker

    driver = driver if driver is not None else _FakeDriver()
    ticker = GestureTicker(driver, on_direction=lambda _d: None, clock=clock)

    class _NeverFire:
        def update(self, angles):
            del angles
            return False

        def reset(self) -> None:
            pass

    ticker.detector = _NeverFire()
    return driver, ticker


def _settle(ticker, motion, ticks: int = 6) -> None:
    for _ in range(ticks):
        ticker.process_pose((0.0, 0.0, 0.0), motion)


# --- head_held: the hand, the refractory window, and the release stroke ------------


def test_head_held_is_false_on_a_head_nobody_is_touching(monkeypatch):
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))

    assert ticker.head_held() is False


def test_head_held_is_true_while_the_head_is_deflected(monkeypatch):
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    live = _motion(residual=(0.0, 0.0, 0.0))
    _settle(ticker, live)

    # A firm turn: 0.236 rad of roll is the amplitude measured on the robot.
    ticker.process_pose((0.236, 0.0, 0.0), _motion(residual=(0.236, 0.0, 0.0)))

    assert ticker.head_held() is True


def test_head_held_survives_the_refractory_window_and_the_release_stroke(monkeypatch):
    """A selection opens a refractory window; the human has not let go yet.

    Handing the head back mid-gesture would read a single physical turn-and-release as
    two turns, so the hold must outlive both the window and the second after it.
    """

    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    live = _motion(residual=(0.0, 0.0, 0.0))
    _settle(ticker, live)
    ticker.process_pose((0.236, 0.0, 0.0), _motion(residual=(0.236, 0.0, 0.0)))
    assert ticker.head_held() is True

    # The head springs back to neutral; the detector's refractory window is running.
    for _ in range(3):
        clock.advance(0.1)
        ticker.process_pose((0.0, 0.0, 0.0), live)
    assert ticker.head_held() is True

    # ...and once it has elapsed, the trailing release timer still covers a second.
    for _ in range(10):
        clock.advance(0.1)
        ticker.process_pose((0.0, 0.0, 0.0), live)
    assert ticker.head_held() is True

    clock.advance(1.01)
    ticker.process_pose((0.0, 0.0, 0.0), live)
    assert ticker.head_held() is False


# --- the two regimes ---------------------------------------------------------------


def test_the_narrow_predicate_needs_a_residual_to_have_arrived(monkeypatch):
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)

    # Nothing has been read yet: the robot is assumed to be v1, and v1 stands down.
    assert ticker.residual_sensing_live() is False
    assert ticker.head_reserved_for_sensing() is True

    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))

    assert ticker.residual_sensing_live() is True
    assert ticker.head_reserved_for_sensing() is False


def test_at_idle_the_head_waits_still_for_a_push_even_with_the_residual_live(monkeypatch):
    """An idle scan that leaves the head tilted between sessions makes pushes get eaten
    as release strokes or come before the detector's rest ("only right worked, not
    left"). While nothing is running the head is reserved; the moment
    something runs, the narrow predicate is back."""

    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))
    assert ticker.head_reserved_for_sensing() is False     # no idle gate installed yet

    running = {"idle": True}
    ticker.set_idle_predicate(lambda: running["idle"])
    assert ticker.head_reserved_for_sensing() is True
    running["idle"] = False                                  # an add-on mode starts
    assert ticker.head_reserved_for_sensing() is False

    def broken() -> bool:
        raise RuntimeError("runtime not ready")

    ticker.set_idle_predicate(broken)
    assert ticker.head_reserved_for_sensing() is False


def test_the_narrow_predicate_still_reserves_the_head_for_a_hand(monkeypatch):
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))

    ticker.process_pose((0.236, 0.0, 0.0), _motion(residual=(0.236, 0.0, 0.0)))

    assert ticker.head_reserved_for_sensing() is True


def test_the_wide_predicate_comes_back_within_two_seconds_of_the_residual_stopping(
        monkeypatch):
    """Fail-CLOSED. A node that stops reporting the residual -- a backend that cannot
    say, a rollback to v1 mid-session -- must put the old blanket stand-down back
    without anybody noticing a rollback happened."""

    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))
    assert ticker.head_reserved_for_sensing() is False

    clock.advance(1.9)
    assert ticker.head_reserved_for_sensing() is False   # still inside the window

    clock.advance(0.2)
    assert ticker.head_reserved_for_sensing() is True    # ...and back to wide


def test_a_long_robot_move_does_not_age_the_residual_clock_out(monkeypatch):
    """The robot is healthy; only its motion is busy. The predicate must not widen.

    ``_run`` reads the state on EVERY tick, including one it is about to suppress,
    precisely so the residual-liveness clock keeps ticking while the hostapp's own
    player is moving the head. A move that outlasts ``_RESIDUAL_LIVE_S`` is ordinary --
    ``talking_gesture(beats=8)`` is ~2.4 s plus settle, and the farewell plays a
    recorded move -- so a fast path that skipped the read would flip
    ``head_reserved_for_sensing`` back to its old wide answer in the middle of one,
    push ``set_head_reserved(True)`` to the arbiter, and cut expression-lane gaze for
    the rest of the move. Thirty ticks at 0.1 s is three seconds, well past the window.
    """

    clock = _Clock()
    driver, ticker = _ticker(monkeypatch, clock=clock)
    driver.client = _StateClient(clock)

    ticker._stop = _NTicks(5)  # noqa: SLF001 - drive exactly five loop iterations
    ticker._run()  # noqa: SLF001
    assert ticker.residual_sensing_live() is True
    assert ticker.head_reserved_for_sensing() is False

    driver.player.busy = True
    ticker._stop = _NTicks(30)  # noqa: SLF001
    ticker._run()  # noqa: SLF001

    assert driver.client.reads == 35          # the suppressed ticks read too
    assert ticker.residual_sensing_live() is True
    assert ticker.head_reserved_for_sensing() is False


def test_a_v1_state_never_narrows_the_predicate(monkeypatch):
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)

    _settle(ticker, None)                       # Pollen's daemon: no motion block
    assert ticker.head_reserved_for_sensing() is True

    _settle(ticker, _motion())                  # a v2 node that cannot report a command
    assert ticker.head_reserved_for_sensing() is True


def test_the_environment_variable_pins_the_wide_predicate(monkeypatch):
    """EMORI_GESTURE_RESIDUAL=0 reverts the cue lane, the skill intake and the idle
    scan together -- one line in the robot's .env and a hostapp restart."""

    monkeypatch.setenv("EMORI_GESTURE_RESIDUAL", "0")
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)

    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))

    assert ticker.residual_sensing_live() is False
    assert ticker.head_reserved_for_sensing() is True


@pytest.mark.parametrize("mode", ["back_drive"])
def test_a_rig_without_push_turn_is_untouched(monkeypatch, mode):
    monkeypatch.setenv("EMORI_GESTURE_MODE", mode)
    from emori_adapter.hostapp.driver import GestureTicker

    ticker = GestureTicker(_FakeDriver(), on_direction=lambda _d: None)

    assert ticker.head_reserved_for_sensing() is False


def test_training_still_owns_the_head_outright(monkeypatch):
    """A training session drives the head through its own repertoire, and the QR lease
    covers the window before the browser loads /session. Neither depends on residual
    sensing, and neither changed."""

    clock = _Clock()
    driver, ticker = _ticker(monkeypatch, clock=clock)
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0)))

    driver.tracker.active = True
    assert ticker.head_reserved_for_sensing() is False

    driver.tracker.active = False
    ticker.set_training_predicate(lambda: True)
    assert ticker.head_reserved_for_sensing() is False


# --- what the narrowing actually buys: the robot gestures while it speaks ----------


def test_the_cue_lane_plans_at_idle_under_the_narrow_predicate(monkeypatch):
    """Miss 4, asserted where it is visible.

    ``CueScheduler.on_sentence`` checks this predicate FIRST and returns
    ``CuePlanResult(False, ..., "head_reserved")``. Under the wide answer that was every
    sentence of every reply, which is the whole "the robot only moves its antennas"
    report. Under the narrow one it plans, unless a hand is actually on the head.
    """

    from emori_adapter.hostapp.driver import GestureTicker, LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)

    class _Player:
        busy = False

        def __init__(self) -> None:
            self.batches: list[list] = []

        def submit(self, intents, *, drop_if_busy=False, guard=None) -> bool:
            self.batches.append(list(intents))
            return True

    clock = _Clock()
    driver = LiveDriver(DryRunRobotClient(), player=_Player(), fiware_factory=None)
    ticker = GestureTicker(driver, on_direction=lambda _d: None, clock=clock)
    driver.set_head_reserved(ticker.head_reserved_for_sensing)

    def _plan():
        return driver.cue.on_sentence(text="A steady line.", cue_category="calm",
                                      cue_body=None, lang="en", affect=AffectSnapshot())

    # Wide: nothing has reported a residual, so the lane stands down as it ships today.
    assert _plan().reason == "head_reserved"

    # Narrow: the shim reports a residual and no hand is on the head.
    for _ in range(6):
        ticker.process_pose((0.0, 0.0, 0.0), _motion(residual=(0.0, 0.0, 0.0)))
    result = _plan()
    assert result.reason != "head_reserved"
    assert result.planned is True

    # ...and a hand on the head still takes the lane back.
    ticker.process_pose((0.236, 0.0, 0.0), _motion(residual=(0.236, 0.0, 0.0)))
    assert _plan().reason == "head_reserved"


def test_a_selection_still_fires_with_the_narrow_predicate_live(monkeypatch):
    """The narrowing must not cost the gesture it is named after."""

    clock = _Clock()
    driver, ticker = _ticker(monkeypatch, clock=clock)
    fired: list[GestureDirection] = []
    ticker.on_direction = fired.append

    live = _motion(residual=(0.0, 0.0, 0.0))
    _settle(ticker, live)
    ticker.process_pose((0.236, 0.0, 0.0), _motion(residual=(0.236, 0.0, 0.0)))

    assert fired == [GestureDirection.RIGHT]


def test_the_robots_own_move_does_not_read_as_a_hand_during_its_refractory(monkeypatch):
    """`suspend_until_settled` opens the same refractory window a selection does; if
    `held` counted both, every idle-scan move would reserve the head for the whole
    second after it settled, and the ROS graph would lose most engagement gestures to
    `head_reserved` (20 of 31 in one measured session). Only a SELECTION's refractory is a hand."""
    clock = _Clock()
    _driver, ticker = _ticker(monkeypatch, clock=clock)
    live = _motion(residual=(0.0, 0.0, 0.0))
    _settle(ticker, live)
    assert ticker.head_held() is False
    # The robot moves its own head (a nod, an idle scan): suppressed, then re-armed.
    _settle(ticker, _motion(residual=(0.0, 0.0, 0.0), head_moving=True), ticks=4)
    for _ in range(40):          # settle, baseline, and the whole refractory window
        clock.advance(0.1)
        ticker.process_pose((0.0, 0.0, 0.0), live)
        assert ticker.head_held() is False, "the robot's own move must not reserve the head"
