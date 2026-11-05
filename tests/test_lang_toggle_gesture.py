"""The hidden idle language toggle: a DOUBLE head turn, one way then the other.

The robot is an appliance -- no screen, no keyboard, no shell -- so the only way to
put it into English for a demo and back into Catalan afterwards is physical. The
single turn belongs to an installed add-on (one mode per side), so the
toggle is the one thing a single turn can never be: a full turn one way IMMEDIATELY
followed by a full turn the other way, and only while the robot is idle.

The precedence that costs something is documented in driver.GestureTicker: while the
toggle is armed a lone single turn is HELD for up to EMORI_LANG_TOGGLE_WINDOW_S
(2.5 s by default) before it selects its mode, because a turn cannot be known to be
lone until the window closes. These tests pin both halves of that trade.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from emori_adapter.hostapp.gestures import GestureDirection

LEFT = GestureDirection.LEFT
RIGHT = GestureDirection.RIGHT
POSE = (0.0, 0.0, 0.4)


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
    """The legacy binary detector: never part of a directional selection."""

    def update(self, angles):
        del angles
        return False

    def reset(self) -> None:
        pass


class _Scripted:
    """A directional detector the TEST drives: one queued direction per tick."""

    def __init__(self) -> None:
        self.queued: list[GestureDirection] = []

    def turn(self, direction: GestureDirection) -> None:
        self.queued.append(direction)

    def update(self, angles, *, armed=True):
        del angles, armed
        return self.queued.pop(0) if self.queued else None

    def reset(self) -> None:
        self.queued.clear()

    def suspend_until_settled(self) -> None:
        pass

    def take_vetoed_latch(self) -> bool:
        # The test drives directions directly, so no movement is ever refused.
        return False


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Rig:
    """One ticker plus everything the test needs to observe it."""

    def __init__(self, ticker, driver, clock, selections, toggles) -> None:
        self.ticker = ticker
        self.driver = driver
        self.clock = clock
        self.selections = selections
        self.toggles = toggles

    def turn(self, direction: GestureDirection) -> None:
        """One physical turn: the detector fires it on the next processed pose."""

        self.ticker.direction_detector.turn(direction)
        self.ticker.process_pose(POSE)

    def tick(self) -> None:
        """A pose sample with no turn in it (the 10 Hz idle poll)."""

        self.ticker.process_pose(POSE)


def _rig(monkeypatch, *, idle=True, is_training=None, mode="push_turn",
         handler=True, **env) -> _Rig:
    monkeypatch.setenv("EMORI_GESTURE_MODE", mode)
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    monkeypatch.delenv("EMORI_GESTURE_INVERT", raising=False)
    for name in ("EMORI_LANG_TOGGLE", "EMORI_LANG_TOGGLE_WINDOW_S"):
        monkeypatch.delenv(name, raising=False)
    # The toggle now ships OFF (it holds every lone selection for the window),
    # so these tests -- which are ABOUT the toggle -- arm it explicitly.  A test
    # can still pass EMORI_LANG_TOGGLE="0" to get the shipped default back.
    monkeypatch.setenv("EMORI_LANG_TOGGLE", "1")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from emori_adapter.hostapp.driver import GestureTicker

    driver = _FakeDriver()
    clock = _Clock()
    selections: list[GestureDirection] = []
    toggles: list[int] = []
    ticker = GestureTicker(
        driver,
        on_direction=selections.append,
        is_training=is_training,
        is_idle=(None if idle is None else (lambda: idle)),
        on_lang_toggle=((lambda: toggles.append(1)) if handler else None),
        clock=clock,
    )
    ticker.detector = _NeverFire()
    ticker.direction_detector = _Scripted()
    return _Rig(ticker, driver, clock, selections, toggles)


# --- the toggle itself --------------------------------------------------------

def test_a_double_turn_toggles_the_language_and_selects_no_mode(monkeypatch):
    rig = _rig(monkeypatch)

    rig.turn(LEFT)
    assert rig.selections == []          # held: this could still be half a toggle
    rig.clock.advance(1.0)
    rig.turn(RIGHT)

    assert rig.toggles == [1]
    # BOTH component turns are consumed: a language switch must never also start
    # an add-on mode.
    assert rig.selections == []
    assert rig.driver.counters["lang_toggle"] == 1


def test_the_double_turn_works_in_either_order(monkeypatch):
    rig = _rig(monkeypatch)

    rig.turn(RIGHT)
    rig.clock.advance(0.4)
    rig.turn(LEFT)

    assert rig.toggles == [1] and rig.selections == []


def test_the_toggle_fires_once_per_pair_not_once_per_turn(monkeypatch):
    rig = _rig(monkeypatch)

    for direction in (LEFT, RIGHT, LEFT, RIGHT):
        rig.clock.advance(0.4)
        rig.turn(direction)

    assert rig.toggles == [1, 1]
    assert rig.selections == []


# --- the single turn still selects its mode -------------------------

def test_a_lone_turn_still_selects_its_mode_once_the_window_closes(monkeypatch):
    """The documented cost: up to EMORI_LANG_TOGGLE_WINDOW_S of latency."""

    rig = _rig(monkeypatch)

    rig.turn(LEFT)
    assert rig.selections == []
    rig.clock.advance(2.4)
    rig.tick()
    assert rig.selections == []          # the window has not closed yet

    rig.clock.advance(0.2)
    rig.tick()

    assert rig.selections == [LEFT]
    assert rig.toggles == []


def test_two_turns_the_same_way_are_two_selections_not_a_toggle(monkeypatch):
    rig = _rig(monkeypatch)

    rig.turn(LEFT)
    rig.clock.advance(0.5)
    rig.turn(LEFT)

    # The first one can no longer become a toggle, so it is released immediately.
    assert rig.selections == [LEFT]
    assert rig.toggles == []

    rig.clock.advance(2.6)
    rig.tick()
    assert rig.selections == [LEFT, LEFT]


def test_two_opposite_turns_further_apart_than_the_window_are_two_selections(monkeypatch):
    rig = _rig(monkeypatch)

    rig.turn(LEFT)
    rig.clock.advance(2.6)
    rig.tick()                            # the window closes -> the turn is dispatched
    rig.turn(RIGHT)
    rig.clock.advance(2.6)
    rig.tick()

    assert rig.selections == [LEFT, RIGHT]
    assert rig.toggles == []


def test_the_window_is_env_tunable(monkeypatch):
    rig = _rig(monkeypatch, EMORI_LANG_TOGGLE_WINDOW_S="0.5")

    rig.turn(LEFT)
    rig.clock.advance(0.6)
    rig.tick()

    assert rig.selections == [LEFT]


def test_an_unparseable_window_falls_back_to_the_default(monkeypatch):
    rig = _rig(monkeypatch, EMORI_LANG_TOGGLE_WINDOW_S="soon")

    rig.turn(LEFT)
    rig.clock.advance(2.4)
    rig.tick()
    assert rig.selections == []
    rig.clock.advance(0.2)
    rig.tick()
    assert rig.selections == [LEFT]


# --- when the toggle must stay out of the way ---------------------------------

def test_the_toggle_is_off_unless_a_robot_explicitly_asks_for_it(monkeypatch):
    """The SHIPPED default: no hold, no added latency.

    An armed toggle makes every lone head turn wait out the double-turn window
    before an add-on hears it.  A deployed robot's .env carries EMORI_LANG_TOGGLE=0; the code
    default agrees with it instead of contradicting it.
    """

    monkeypatch.delenv("EMORI_LANG_TOGGLE", raising=False)
    rig = _rig(monkeypatch)
    monkeypatch.delenv("EMORI_LANG_TOGGLE", raising=False)
    from emori_adapter.hostapp.driver import GestureTicker

    shipped = GestureTicker(
        _FakeDriver(),
        on_direction=rig.selections.append,
        is_idle=lambda: True,
        on_lang_toggle=lambda: rig.toggles.append(1),
        clock=rig.clock,
    )
    shipped.detector = _NeverFire()
    shipped.direction_detector = _Scripted()
    shipped.direction_detector.turn(LEFT)
    shipped.process_pose(POSE)

    assert rig.selections == [LEFT]  # dispatched on the spot, nothing held
    assert rig.toggles == []


def test_the_flag_off_restores_the_immediate_selection(monkeypatch):
    """``EMORI_LANG_TOGGLE=0`` is a clean kill switch: no hold, no latency."""

    rig = _rig(monkeypatch, EMORI_LANG_TOGGLE="0")

    rig.turn(LEFT)

    assert rig.selections == [LEFT]
    assert rig.toggles == []
    rig.clock.advance(1.0)
    rig.turn(RIGHT)
    assert rig.selections == [LEFT, RIGHT]
    assert rig.toggles == []


def test_the_toggle_is_disarmed_while_an_addon_mode_is_running(monkeypatch):
    """Only an IDLE robot may be re-languaged, and a turn during a live mode is
    that mode's own control input -- it must reach the controller undelayed."""

    rig = _rig(monkeypatch, idle=False)

    rig.turn(LEFT)
    rig.clock.advance(0.5)
    rig.turn(RIGHT)

    assert rig.selections == [LEFT, RIGHT]
    assert rig.toggles == []


def test_without_a_way_to_prove_the_robot_is_idle_the_toggle_stays_disarmed(monkeypatch):
    # No predicate installed (a robot with no add-on at all) -> fail closed.
    rig = _rig(monkeypatch, idle=None)
    rig.turn(LEFT)
    assert rig.selections == [LEFT] and rig.toggles == []

    # ...and the same when nothing can act on a toggle.
    rig = _rig(monkeypatch, handler=False)
    rig.turn(LEFT)
    assert rig.selections == [LEFT] and rig.toggles == []


def test_a_training_lease_suppresses_the_toggle_entirely(monkeypatch):
    """A QR has claimed the robot: head movement is neither a selection nor a
    language switch until that lease is gone."""

    rig = _rig(monkeypatch, is_training=lambda: True)

    rig.turn(LEFT)
    rig.clock.advance(0.5)
    rig.turn(RIGHT)
    rig.clock.advance(3.0)
    rig.tick()

    assert rig.selections == [] and rig.toggles == []


def test_a_training_session_keeps_the_legacy_binary_end_gesture(monkeypatch):
    rig = _rig(monkeypatch)
    rig.driver.tracker.active = True

    class _AlwaysFire:
        def update(self, angles):
            del angles
            return True

        def reset(self) -> None:
            pass

    rig.ticker.detector = _AlwaysFire()
    rig.turn(LEFT)

    assert rig.driver.cmds == ["end"]
    assert rig.selections == [] and rig.toggles == []


def test_a_held_turn_is_dropped_when_the_robot_leaves_idle(monkeypatch):
    """The world changed under a turn that is up to 2.5 s stale.

    Replaying it into a mode that has since started (a QR training lease) would
    tear that mode down; the human can simply turn again.
    """

    idle = {"value": True}
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    for name in ("EMORI_LANG_TOGGLE_WINDOW_S",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMORI_LANG_TOGGLE", "1")   # the toggle now ships OFF
    from emori_adapter.hostapp.driver import GestureTicker

    driver = _FakeDriver()
    clock = _Clock()
    selections: list[GestureDirection] = []
    ticker = GestureTicker(driver, on_direction=selections.append,
                           is_idle=lambda: idle["value"],
                           on_lang_toggle=lambda: None, clock=clock)
    ticker.detector = _NeverFire()
    ticker.direction_detector = _Scripted()

    ticker.direction_detector.turn(LEFT)
    ticker.process_pose(POSE)
    assert selections == []

    idle["value"] = False
    ticker.process_pose(POSE)

    assert selections == []


# --- the physics of the second turn ------------------------------------------

def test_a_held_turn_still_recenters_the_stiff_head(monkeypatch):
    """Without this the double turn is physically impossible on push_turn.

    A stiff head HOLDS where it was turned, so the detector stays latched at its
    deflected baseline and the SECOND half of the toggle is never measured.
    """

    rig = _rig(monkeypatch)

    rig.turn(LEFT)

    assert len(rig.driver.motions) == 1
    intent = rig.driver.motions[0][0]
    assert intent.kind == "goto" and intent.head is not None
    assert intent.head.yaw == 0.0 and intent.head.roll == 0.0


def test_back_drive_sensing_holds_the_turn_without_moving_the_head(monkeypatch):
    rig = _rig(monkeypatch, mode="back_drive")

    rig.turn(LEFT)

    assert rig.driver.motions == []       # legacy behaviour unchanged
    rig.clock.advance(2.6)
    rig.tick()
    assert rig.selections == [LEFT]


def test_real_pose_samples_produce_a_toggle_end_to_end(monkeypatch):
    """No scripted detector: the stock classifier, at the robot's 10 Hz, real angles.

    Everything the two halves have to survive in between is in here -- the refractory
    window, the re-arm at baseline, and the recenter that a stiff head needs before the
    second turn is measurable at all.
    """

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    monkeypatch.delenv("EMORI_GESTURE_INVERT", raising=False)
    for name in ("EMORI_LANG_TOGGLE_WINDOW_S",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMORI_LANG_TOGGLE", "1")   # the toggle now ships OFF
    from emori_adapter.hostapp.driver import GestureTicker

    driver = _FakeDriver()
    clock = _Clock()
    selections: list[GestureDirection] = []
    toggles: list[int] = []
    ticker = GestureTicker(driver, on_direction=selections.append,
                           is_idle=lambda: True,
                           on_lang_toggle=lambda: toggles.append(1), clock=clock)
    rest = (0.0, 0.0, 0.0)

    def _tick(pose) -> None:
        ticker.process_pose(pose)
        clock.advance(0.1)                # the ticker's own 10 Hz

    for _ in range(6):
        # settle_ticks anchors the baseline, quiet_ticks arms the rise rule
        _tick(rest)
    _tick((0.0, 0.0, 0.26))                # a firm turn to the robot's right
    assert selections == [] and toggles == []

    # The human lets go and the head springs back (the recenter this ticker just
    # submitted); the detector re-arms once the refractory window elapses.
    for _ in range(10):
        _tick(rest)
    assert selections == [] and toggles == []      # still inside the 2.5 s window

    _tick((0.0, 0.0, -0.26))               # ...and back the other way

    assert toggles == [1]
    assert selections == []
    assert clock.t - 1000.0 < 2.5          # the whole gesture fits in the window


def test_a_held_turn_is_released_on_time_while_the_head_recenters(monkeypatch):
    """The recenter sets ``player.busy``, and the polling loop then skips
    ``process_pose`` entirely -- so the window has to keep running there too."""

    rig = _rig(monkeypatch)
    rig.turn(LEFT)
    rig.driver.player.busy = True
    rig.clock.advance(2.6)

    class _OneTick:
        def __init__(self) -> None:
            self.ticks = 0

        def wait(self, _timeout):
            self.ticks += 1
            return self.ticks > 1

        def set(self) -> None:
            pass

    rig.ticker._stop = _OneTick()         # noqa: SLF001 - drive one loop iteration
    rig.ticker._run()                     # noqa: SLF001

    assert rig.selections == [LEFT]
