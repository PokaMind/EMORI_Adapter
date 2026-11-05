"""Idle head-tilt begin: flagged restore of the legacy idle head-tilt 'begin'."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace


class _BeginDriver:
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
        del drop_if_busy
        self.motions.append(list(intents))
        return True

    def touch(self) -> None:
        self.touched += 1


class _AlwaysFire:
    def update(self, angles) -> bool:
        del angles
        return True

    def reset(self) -> None:
        pass


class _NeverDirection:
    def update(self, angles, *, armed=True):
        del angles, armed
        return None

    def reset(self) -> None:
        pass

    def take_vetoed_latch(self) -> bool:
        # Never latches at all, so it never latches without a direction either.
        return False


def _ticker(monkeypatch, driver, *, begin_flag: str | None):
    if begin_flag is None:
        monkeypatch.delenv("EMORI_GESTURE_BEGIN", raising=False)
    else:
        monkeypatch.setenv("EMORI_GESTURE_BEGIN", begin_flag)
    from emori_adapter.hostapp.driver import GestureTicker

    directions: list[object] = []
    ticker = GestureTicker(driver, on_direction=directions.append)
    ticker.detector = _AlwaysFire()
    ticker.direction_detector = _NeverDirection()
    return ticker, directions


def test_flag_on_restores_idle_begin(monkeypatch):
    driver = _BeginDriver()
    ticker, directions = _ticker(monkeypatch, driver, begin_flag="1")

    ticker.process_pose((0.5, 0.0, 0.0))

    assert driver.cmds == ["begin"]
    assert driver.counters["gesture_begin"] == 1
    assert directions == []
    assert driver.touched == 1
    assert len(driver.motions) == 1  # perk-antennas acknowledgement


def test_flag_off_keeps_begin_deleted(monkeypatch):
    driver = _BeginDriver()
    ticker, directions = _ticker(monkeypatch, driver, begin_flag=None)

    ticker.process_pose((0.5, 0.0, 0.0))

    assert driver.cmds == []
    assert driver.counters["gesture_begin"] == 0
    assert directions == []  # _NeverDirection returned None


def test_flag_on_never_interferes_with_training_end(monkeypatch):
    driver = _BeginDriver()
    driver.tracker.active = True
    ticker, _ = _ticker(monkeypatch, driver, begin_flag="1")

    ticker.process_pose((0.5, 0.0, 0.0))

    assert driver.cmds == ["end"]
    assert driver.counters["gesture_end"] == 1
    assert driver.counters["gesture_begin"] == 0
