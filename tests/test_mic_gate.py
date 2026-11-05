"""The gate that shuts the microphone to what is not the person in front of the robot."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from emori_adapter.hostapp import mic_gate
from emori_adapter.hostapp.mic_gate import FRONT_RAD, OBSERVE, OFF, ON, MicGate
from emori_adapter.hostapp.mic_profile import IDLE_PROFILE, SESSION_PROFILE, MicProfile

FRONT = (FRONT_RAD, True)
SIDE = (FRONT_RAD + math.radians(80), True)
QUIET = (FRONT_RAD, False)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    def __init__(self, *, mode=ON, face_age=None, speaking=False) -> None:
        self.reading = QUIET
        self.face_age = face_age
        self.speaking = speaking
        self.calls: list[list[str]] = []
        self.clock = _Clock()
        self.gate = MicGate(mode=mode, direction=lambda: self.reading,
                            face_age=lambda: self.face_age,
                            robot_speaking=lambda: self.speaking,
                            run=self._run, clock=self.clock)
        self.gate.set_active(True)

    def _run(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    def hear(self, reading, seconds, step=0.1):
        self.reading = reading
        for _ in range(round(seconds / step)):
            self.gate.tick()
            self.clock.now += step

    @property
    def muted(self):
        states = [c[-1] for c in self.calls if "set-mute" in c]
        return bool(states) and states[-1] == "1"


def test_a_voice_from_the_side_closes_the_microphone_once_it_has_lasted():
    rig = _Rig()
    rig.hear(SIDE, 0.2)
    assert not rig.muted
    rig.hear(SIDE, 0.3)
    assert rig.muted
    assert rig.gate.snapshot()["closed"] is True


def test_a_voice_in_front_is_never_closed():
    rig = _Rig()
    rig.hear(FRONT, 30)
    assert rig.calls == []


def test_a_voice_in_front_reopens_at_once():
    rig = _Rig()
    rig.hear(SIDE, 0.5)
    assert rig.muted
    rig.hear(FRONT, 0.1)
    assert not rig.muted


def test_closing_lapses_by_itself_when_the_side_goes_quiet():
    rig = _Rig()
    rig.hear(SIDE, 0.5)
    rig.hear(QUIET, 1.0)
    assert rig.muted
    rig.hear(QUIET, 0.7)
    assert not rig.muted


def test_a_short_sound_from_the_side_never_closes():
    rig = _Rig()
    for _ in range(5):
        rig.hear(SIDE, 0.2)
        rig.hear(FRONT, 0.2)
    assert not rig.muted
    assert rig.calls == []


def test_no_direction_reading_opens_the_microphone():
    rig = _Rig()
    rig.hear(SIDE, 0.5)
    assert rig.muted
    rig.hear(None, 0.1)
    assert not rig.muted


def test_it_stays_open_while_the_robot_speaks():
    rig = _Rig(speaking=True)
    rig.hear(SIDE, 5)
    assert rig.calls == []


def test_the_robot_speaking_reopens_a_closed_microphone():
    rig = _Rig()
    rig.hear(SIDE, 0.5)
    rig.speaking = True
    rig.hear(QUIET, 0.1)
    assert not rig.muted


def test_a_face_gone_too_long_closes_and_its_return_opens():
    rig = _Rig(face_age=9.0)
    rig.hear(FRONT, 0.1)
    assert rig.muted
    rig.face_age = 0.1
    rig.hear(FRONT, 0.1)
    assert not rig.muted


def test_a_face_never_seen_cannot_close_it():
    rig = _Rig(face_age=None)
    rig.hear(FRONT, 30)
    assert rig.calls == []


def test_a_face_reader_that_fails_is_no_evidence():
    rig = _Rig()
    rig.gate._face_age = lambda: 1 / 0
    rig.hear(FRONT, 1)
    assert rig.calls == []


def test_observe_decides_and_touches_nothing():
    rig = _Rig(mode=OBSERVE)
    rig.hear(SIDE, 0.5)
    assert rig.calls == []
    assert rig.gate.snapshot()["closed"] is True
    assert rig.gate.snapshot()["closed_direction"] == 1


def test_inactive_it_does_nothing_and_releasing_opens():
    rig = _Rig()
    rig.hear(SIDE, 0.5)
    assert rig.muted
    rig.gate.set_active(False)
    assert not rig.muted
    rig.hear(SIDE, 5)
    assert not rig.muted


def test_starting_clears_a_mute_left_behind(monkeypatch):
    calls = []
    gate = MicGate(mode=ON, direction=lambda: None,
                   run=lambda argv, **_k: calls.append(argv) or SimpleNamespace(returncode=0),
                   poll_s=60.0)
    gate.start()
    gate.stop()
    assert calls[0][-2:] == ["@DEFAULT_AUDIO_SOURCE@", "0"]


def test_off_starts_no_thread_and_still_clears_a_mute_left_behind():
    # Turning the gate off must be a way out, even after a crash with it closed.
    calls = []
    gate = MicGate(mode=OFF, direction=lambda: FRONT,
                   run=lambda argv, **_k: calls.append(argv) or SimpleNamespace(returncode=0))
    gate.start()
    assert gate._thread is None
    assert calls == [["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"]]


def test_a_tick_finishing_after_the_release_cannot_shut_the_microphone():
    rig = _Rig()
    rig.hear(SIDE, 0.2)

    def release_during_the_read():
        rig.gate.set_active(False)
        return SIDE

    rig.gate._direction = release_during_the_read
    rig.clock.now += 1.0
    rig.gate.tick()
    assert not rig.muted
    assert rig.gate.snapshot()["closed"] is False


def test_a_stopped_gate_cannot_shut_the_microphone():
    rig = _Rig()
    rig.gate._stop.set()
    rig.gate._close("direction")
    assert not rig.muted


@pytest.mark.parametrize("raw,expected", [("", OFF), ("on", ON), ("OBSERVE", OBSERVE),
                                          ("yes", OFF), ("1", OFF)])
def test_anything_unrecognised_reads_as_off(monkeypatch, raw, expected):
    monkeypatch.setenv("EMORI_MIC_GATE", raw)
    assert mic_gate.gate_mode() == expected


def test_a_wpctl_that_is_missing_does_not_raise():
    def missing(argv, **_kwargs):
        raise FileNotFoundError(argv[0])

    gate = MicGate(mode=ON, direction=lambda: SIDE, run=missing, clock=_Clock())
    gate.set_active(True)
    gate._mute(True)


def test_the_gate_follows_the_capture_profile():
    gate = _Rig().gate
    profile = MicProfile(run=lambda *_a, **_k: SimpleNamespace(returncode=0))
    profile.listener = lambda name: gate.set_active(name == SESSION_PROFILE)
    profile.set_profile(IDLE_PROFILE)
    assert gate.snapshot()["active"] is False
    profile.set_profile(SESSION_PROFILE)
    assert gate.snapshot()["active"] is True


def test_a_failing_listener_does_not_stop_the_gain_moving():
    calls = []
    profile = MicProfile(run=lambda *_a, **_k: calls.append(1) or SimpleNamespace(returncode=0))
    profile.listener = lambda _name: 1 / 0
    assert profile.set_profile(SESSION_PROFILE) is True
    assert calls
