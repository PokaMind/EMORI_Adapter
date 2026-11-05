"""The head could not be driven home while push_turn asserted gravity_comp."""

from __future__ import annotations

from emori_adapter.hostapp.driver import LiveDriver, push_turn_gesture_mode


class _Client:
    def __init__(self) -> None:
        self.motor_modes: list[str] = []

    def set_motor_mode(self, mode: str) -> None:
        self.motor_modes.append(mode)


def test_push_turn_never_asks_for_gravity_compensation(monkeypatch):
    """On firmware v1.9.0 that mode 500s and poisons every later move.

    The daemon then reports backend_status.ready=false and answers every goto
    with 200 plus a move id while executing none of them -- so the head cannot
    spring back to neutral after a turn, and stays where it was pushed.
    push_turn is detected on STIFF motors, so it must never ask for it.
    """

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    assert push_turn_gesture_mode() is True

    driver = LiveDriver(_Client(), gesture_enabled=True)

    assert driver.player._gravity_comp is False, (  # noqa: SLF001 - construction seam
        "push_turn asked the daemon for gravity_compensation"
    )


def test_back_drive_still_asks_for_gravity_compensation(monkeypatch):
    """The legacy gesture genuinely needs a limp head; do not break it."""

    monkeypatch.setenv("EMORI_GESTURE_MODE", "back_drive")
    assert push_turn_gesture_mode() is False

    driver = LiveDriver(_Client(), gesture_enabled=True)

    assert driver.player._gravity_comp is True  # noqa: SLF001 - construction seam


def test_no_gesture_means_no_compliance_request(monkeypatch):
    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")

    driver = LiveDriver(_Client(), gesture_enabled=False)

    assert driver.player._gravity_comp is False  # noqa: SLF001 - construction seam
