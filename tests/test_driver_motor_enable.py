"""RobotPlayer must enable motors when the daemon becomes reachable.

On the real robot (daemon v1.9.0, launcher runs --no-wake-up-on-start) motors boot in
mode 'disabled'; if EMORI never sets 'enabled', every goto returns 200 but the head is
limp. gravity_compensation cannot be relied on for this (it is a firmware HTTP 500 on
v1.9.0 and HttpRobotClient latches it off), so 'enabled' is the required startup mode.
"""
from __future__ import annotations

import time

from emori_adapter.hostapp.driver import RobotPlayer


class _Recorder:
    def __init__(self) -> None:
        self.modes: list[str] = []
        self.woke = 0

    def health(self) -> bool:
        return True

    def set_motor_mode(self, mode: str) -> None:
        self.modes.append(mode)

    def wake_up(self) -> None:
        self.woke += 1

    def apply(self, intent) -> None:  # pragma: no cover - not exercised here
        pass


def _run_player_until(rec: _Recorder, done, **kw) -> RobotPlayer:
    """Run a player until ``done(rec)`` or timeout (the wake settle is 0.6 s)."""
    player = RobotPlayer(rec, **kw)
    for _ in range(100):
        if done(rec):
            break
        time.sleep(0.05)
    player.stop()
    player.join()
    return player


def test_player_enables_motors_before_first_wake():
    rec = _Recorder()
    _run_player_until(rec, lambda r: r.woke, gravity_comp=False)
    assert rec.woke, "player never woke the robot"
    assert "enabled" in rec.modes, f"expected an 'enabled' motor-mode call, got {rec.modes}"
    # 'enabled' must come first so the wake (and everything after) can physically move
    assert rec.modes[0] == "enabled"


def test_gravity_comp_mode_still_asserted_after_enable():
    # With the gesture sensor on, compliance is still re-asserted after the wake
    # (which runs a 0.6 s settle first) — enabling motors must not silence the
    # gravity_compensation re-assert path.
    rec = _Recorder()
    _run_player_until(rec, lambda r: "gravity_compensation" in r.modes, gravity_comp=True)
    assert "gravity_compensation" in rec.modes, f"got {rec.modes}"
    assert rec.modes[0] == "enabled"
