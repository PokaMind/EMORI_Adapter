"""The push of the head back that leaves whatever is running."""

from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from emori_adapter.hostapp.driver import GestureTicker, LiveDriver
from emori_adapter.hostapp.gestures import BackPushDetector, GestureDirection

PUSH = -0.25


def _feed(detector, samples):
    return [detector.update(sample) for sample in samples]


# --- the detector ---------------------------------------------------------------------


def test_a_firm_push_held_fires_once_on_the_hold():
    detector = BackPushDetector()
    assert _feed(detector, [0.04, 0.04] + [PUSH] * 6) == [False] * 8
    assert detector.pending
    assert detector.update(PUSH) is True
    assert not detector.pending
    assert _feed(detector, [PUSH] * 20) == [False] * 20


def test_the_weakest_measured_push_counts():
    # 0.206 rad of deflection from a head at rest at +0.04 residual.
    assert any(_feed(BackPushDetector(), [0.04] * 3 + [-0.166] * 8))


@pytest.mark.parametrize("samples", [
    [PUSH],                                 # a knock
    [PUSH] * 3,                             # a brush
    [PUSH, PUSH, 0.04, PUSH, PUSH, 0.04],   # two brushes do not add up
    [-0.14] * 30,                           # a lean below the threshold
    [0.25] * 30,                            # pressed forward
    # The longest dip of the robot's own while it moves, repeated.
    [-0.16, -0.20, -0.13, -0.11, -0.08, -0.05, 0.02] * 10,
])
def test_what_is_not_a_held_push_never_fires(samples):
    assert not any(_feed(BackPushDetector(), samples))


def test_a_dip_inside_a_real_push_does_not_lose_it():
    assert any(_feed(BackPushDetector(), [PUSH, PUSH, -0.13, PUSH, -0.14, PUSH, PUSH, PUSH]))


def test_it_fires_again_only_after_the_head_came_back():
    detector = BackPushDetector()
    assert sum(_feed(detector, [PUSH] * 10)) == 1
    assert sum(_feed(detector, [-0.10] * 10 + [PUSH] * 10)) == 0
    assert sum(_feed(detector, [0.0] + [PUSH] * 10)) == 1


def test_a_missing_sample_neither_counts_nor_breaks_the_hold():
    detector = BackPushDetector()
    assert _feed(detector, [PUSH, None, PUSH, None, PUSH, PUSH, PUSH, PUSH]) == [False] * 8
    assert detector.update(PUSH) is True


def test_a_reset_forgets_a_push_under_way_but_not_a_spent_one():
    detector = BackPushDetector()
    _feed(detector, [PUSH] * 3)
    detector.reset()
    assert not detector.pending
    assert sum(_feed(detector, [PUSH] * 6)) == 0
    assert detector.update(PUSH) is True
    detector.reset()
    assert sum(_feed(detector, [PUSH] * 20)) == 0


def test_bad_knob_values_clamp_so_the_robot_cannot_set_itself_off():
    detector = BackPushDetector(threshold_rad=0.01, hold_ticks=1, stay_rad=0.5)
    assert detector.threshold_rad == 0.10
    assert detector.hold_ticks == 3
    assert detector.stay_rad == 0.10


# --- the ticker ------------------------------------------------------------------------


class _Driver:
    def __init__(self, *, active=False) -> None:
        self.counters: Counter = Counter()
        self.player = SimpleNamespace(busy=False)
        self.tracker = SimpleNamespace(active=active, cycles=0)
        self.cmds: list[str] = []
        self.motions: list[list] = []
        self.touched = 0
        self.grace = 0
        self.last_drop_if_busy = None

    def enqueue_cmd(self, cmd: str) -> None:
        self.cmds.append(cmd)

    def arm_end_grace(self) -> None:
        self.grace += 1

    def submit_legacy_motion(self, intents, *, drop_if_busy=False) -> bool:
        self.motions.append([i.reason for i in intents])
        self.last_drop_if_busy = drop_if_busy
        return True

    def touch(self) -> None:
        self.touched += 1


def _ticker(monkeypatch, driver, *, on_back=None, mode="push_turn", exit_on="1"):
    monkeypatch.setenv("EMORI_GESTURE_MODE", mode)
    monkeypatch.setenv("EMORI_GESTURE_BACK_EXIT", exit_on)
    monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    monkeypatch.delenv("EMORI_GESTURE_RESIDUAL", raising=False)
    ticker = GestureTicker(driver)
    ticker.on_back = on_back
    return ticker


def _motion(pitch, *, head_moving=False):
    return {"active": head_moving, "head_moving": head_moving,
            "residual": {"roll": 0.0, "pitch": pitch, "yaw": 0.0}}


def _push(ticker, *, ticks=7, head_moving=False):
    for _ in range(ticks):
        ticker.process_pose((0.0, PUSH, 0.0), _motion(PUSH, head_moving=head_moving))


def test_a_push_in_a_mode_leaves_it_and_acknowledges(monkeypatch):
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    _push(ticker)
    assert asked == [1]
    assert driver.counters["gesture_exit"] == 1
    assert driver.motions[-1] == ["head push heard"] * 5
    assert driver.cmds == []


def test_a_push_at_idle_acknowledges_nothing(monkeypatch):
    driver = _Driver()
    ticker = _ticker(monkeypatch, driver, on_back=lambda: False)
    _push(ticker)
    assert driver.counters["gesture_exit"] == 0
    assert all("head push heard" not in reasons for reasons in driver.motions)


def test_a_failing_add_on_does_not_stop_the_sensing_thread(monkeypatch):
    driver = _Driver()

    def boom():
        raise RuntimeError("add-on fault")

    ticker = _ticker(monkeypatch, driver, on_back=boom)
    _push(ticker)
    assert driver.counters["gesture_exit"] == 0


def test_a_push_works_while_the_robot_moves_its_own_head(monkeypatch):
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    _push(ticker, head_moving=True)
    assert asked == [1]


def test_a_push_in_a_session_ends_it_with_an_acknowledgement_that_cannot_drop(monkeypatch):
    driver = _Driver(active=True)
    ticker = _ticker(monkeypatch, driver)
    # head_moving keeps the legacy any-axis detector out of it: this is the new path alone.
    _push(ticker, head_moving=True)
    assert driver.cmds == ["end"]
    assert driver.grace == 1
    assert driver.counters["gesture_end"] == 1
    assert driver.motions[-1] == ["gesture ack"] * 5
    assert driver.last_drop_if_busy is False


def test_a_push_in_a_session_stops_the_sentence_so_the_acknowledgement_shows(monkeypatch):
    driver = _Driver(active=True)
    stopped = []
    driver.cue = SimpleNamespace(preempt=stopped.append)
    ticker = _ticker(monkeypatch, driver)
    _push(ticker, head_moving=True)
    assert stopped == ["head pushed back"]


def test_the_legacy_session_nudge_still_ends_a_session(monkeypatch):
    driver = _Driver(active=True)
    ticker = _ticker(monkeypatch, driver)
    ticker.process_pose((0.0, 0.0, 0.0), _motion(0.0))
    ticker.process_pose((0.0, 0.0, 0.3), _motion(0.0))
    assert driver.cmds == ["end"]


def test_one_movement_ends_one_thing(monkeypatch):
    driver = _Driver(active=True)
    ticker = _ticker(monkeypatch, driver)
    _push(ticker, head_moving=True)
    _push(ticker, ticks=10, head_moving=True)
    assert driver.cmds == ["end"]


@pytest.mark.parametrize("env,mode", [("0", "push_turn"), ("1", "back_drive")])
def test_off_or_on_the_wrong_sensing_mode_it_is_not_armed(monkeypatch, env, mode):
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True,
                     mode=mode, exit_on=env)
    _push(ticker)
    assert asked == []


def test_an_absolute_pose_is_never_read_as_a_push(monkeypatch):
    # A head looking up at a tall person, on a robot that reports no residual.
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    for _ in range(20):
        ticker.process_pose((0.0, PUSH, 0.0), {"head_moving": False})
    assert asked == []


def test_limp_motors_do_not_count(monkeypatch):
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    ticker._note_motors({"control_mode": "disabled"})
    _push(ticker)
    assert asked == []
    ticker._note_motors({"control_mode": "enabled"})
    _push(ticker, ticks=8)
    assert asked == [1]


def test_a_badge_lease_takes_no_push(monkeypatch):
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    ticker.set_training_predicate(lambda: True)
    _push(ticker)
    assert asked == []


def test_a_push_under_way_is_not_recentred_under_the_hand(monkeypatch):
    driver = _Driver()
    ticker = _ticker(monkeypatch, driver, on_back=lambda: True)

    class _Latching:
        held = False
        latch = True

        def update(self, angles, *, armed=True):
            return None

        def reset(self):
            pass

        def suspend_until_settled(self):
            pass

        def take_vetoed_latch(self):
            return self.latch

    ticker.direction_detector = _Latching()
    for _ in range(5):
        ticker.process_pose((0.0, PUSH, 0.0), _motion(PUSH))
    assert driver.motions == []
    # Let go too early: the head may be parked, so now it is recentred.
    ticker.direction_detector.latch = False
    ticker.process_pose((0.0, 0.0, 0.0), _motion(0.0))
    assert driver.motions == [["push-turn recenter"]]


def test_the_invert_flag_does_not_touch_the_push(monkeypatch):
    monkeypatch.setenv("EMORI_GESTURE_INVERT", "1")
    driver = _Driver()
    asked = []
    ticker = _ticker(monkeypatch, driver, on_back=lambda: asked.append(1) or True)
    _push(ticker)
    assert asked == [1]


# --- ending a session ------------------------------------------------------------------


def _live():
    """Just the part of LiveDriver these answers come from."""

    driver = LiveDriver.__new__(LiveDriver)
    driver._lock = threading.RLock()
    driver._gesture_end_wanted = False
    driver._turn_armed_seen = False
    driver.frame_alive_at = None
    driver.end_requested_at = None
    driver._pending_cmd = None
    return driver


def test_a_frame_that_never_came_up_gets_the_grace_clock_at_once():
    driver = _live()
    driver.request_gesture_end()
    assert driver._pending_cmd == "end"
    assert driver.end_requested_at is not None


def test_during_the_opening_the_end_waits_and_arms_nothing():
    driver = _live()
    driver.frame_alive_at = 1.0
    driver.request_gesture_end()
    assert driver._pending_cmd is None
    # Armed, the grace clock would stand every watchdog down and lose the analysis.
    assert driver.end_requested_at is None
    assert driver._gesture_end_wanted is True


def test_once_a_turn_has_opened_the_end_goes_straight_out():
    driver = _live()
    driver.frame_alive_at = 1.0
    driver._turn_armed_seen = True
    driver.request_gesture_end()
    assert driver._pending_cmd == "end"
    assert driver.end_requested_at is not None


def test_asking_twice_only_counts_once():
    driver = _live()
    assert driver.request_gesture_end() is True
    driver._pending_cmd = None
    assert driver.request_gesture_end() is False
    assert driver._pending_cmd is None


def test_a_deep_push_that_both_detectors_read_ends_the_session_once(monkeypatch):
    class _Asking(_Driver):
        wanted = False

        def request_gesture_end(self):
            first = not self.wanted
            self.wanted = True
            if first:
                self.enqueue_cmd("end")
            return first

    driver = _Asking(active=True)
    ticker = _ticker(monkeypatch, driver)
    ticker.process_pose((0.0, 0.0, 0.0), _motion(0.0))
    _push(ticker, ticks=12)
    assert driver.cmds == ["end"]
    assert driver.counters["gesture_end"] == 1
    assert len(driver.motions) == 1


def test_back_is_not_a_side():
    assert GestureDirection.BACK.value == "back"
    assert GestureDirection.BACK not in (GestureDirection.LEFT, GestureDirection.RIGHT)
