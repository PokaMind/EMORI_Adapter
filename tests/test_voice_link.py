"""VoiceLink: the robot's own speaker output as a live signal.

Pure signal tests: synthetic s16 frames are fed through ``feed`` with an injected
clock, so nothing here needs PipeWire, a robot or real time. The properties a speech
plan depends on are pinned: the voice is ``active`` while energy is above the
adaptive floor (with the hangover), onsets count the rises out of dips, ``level``
follows the envelope, and an unavailable tap (no pw-record) fails safe.
"""

from __future__ import annotations

import math
import struct

from emori_adapter.hostapp import voice_link as vl


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _frame(amplitude: float, *, n: int = vl._FRAME_SAMPLES, freq: float = 220.0) -> bytes:
    """One 20 ms s16 frame of a sine at ``amplitude`` (int16 units), or silence."""

    samples = [int(amplitude * math.sin(2 * math.pi * freq * i / vl._RATE)) for i in range(n)]
    return struct.pack("<%dh" % n, *samples)


def _feed(link: vl.VoiceLink, clock: Clock, amplitude: float, frames: int) -> None:
    for _ in range(frames):
        link.feed(_frame(amplitude), now=clock.now)
        clock.advance(0.02)


def _link(clock: Clock) -> vl.VoiceLink:
    link = vl.VoiceLink(clock=clock, enabled=True, sink="", spawn=lambda cmd: None)
    # Tests drive the signal directly; mark the tap as delivering.
    with link._cv:
        link._available = True
    return link


def test_silence_is_not_active_and_speech_is():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 0.0, 10)
    assert link.active is False
    assert link.level == 0.0
    assert math.isinf(link.silent_for_s())
    _feed(link, clock, 3000.0, 5)
    assert link.active is True
    assert link.level > 0.5
    assert link.silent_for_s() < 0.05


def test_hangover_bridges_consonant_gaps_then_releases():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 3000.0, 5)
    # 100 ms of silence: still active (hangover 200 ms).
    _feed(link, clock, 0.0, 5)
    assert link.active is True
    # 300 ms more: the voice has stopped.
    _feed(link, clock, 0.0, 15)
    assert link.active is False
    assert link.silent_for_s() >= 0.35


def test_onsets_count_rises_out_of_dips_with_a_refractory():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 0.0, 5)
    assert link.onset_count == 0
    # Three "syllables": 120 ms of voice separated by 60 ms dips.
    for _ in range(3):
        _feed(link, clock, 3000.0, 6)
        _feed(link, clock, 0.0, 3)
    assert link.onset_count == 3
    # A single 20 ms dip inside a word does NOT make a new onset.
    _feed(link, clock, 3000.0, 6)          # the rise after the last 60 ms dip: onset 4
    before = link.onset_count
    _feed(link, clock, 0.0, 1)
    _feed(link, clock, 3000.0, 6)
    assert link.onset_count == before


def test_a_sharp_energy_jump_is_an_onset_too():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 0.0, 5)
    _feed(link, clock, 800.0, 10)      # quiet voicing
    n = link.onset_count
    _feed(link, clock, 4000.0, 2)      # a stressed syllable: >2.2x jump
    assert link.onset_count == n + 1


def test_noise_floor_adapts_and_a_constant_hum_is_not_voice():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 150.0, 100)     # a steady hum for two seconds
    assert link.active is False or link.silent_for_s() > 0.3
    _feed(link, clock, 3000.0, 5)      # real voice clears it
    assert link.active is True


def test_active_since_and_wait_helpers():
    clock = Clock()
    link = _link(clock)
    t0 = clock.now
    _feed(link, clock, 0.0, 5)
    assert link.active_since(t0) is False
    assert link.wait_active(0.0) is False
    _feed(link, clock, 3000.0, 3)
    assert link.active_since(t0) is True
    assert link.wait_active(0.0) is True
    base = link.onset_count
    assert link.wait_for_onset(0.0, after=base) is False
    _feed(link, clock, 0.0, 4)
    _feed(link, clock, 3000.0, 2)
    assert link.wait_for_onset(0.0, after=base) is True


def test_snapshot_carries_no_audio_only_shape():
    clock = Clock()
    link = _link(clock)
    _feed(link, clock, 3000.0, 3)
    snap = link.snapshot()
    assert set(snap) == {"enabled", "available", "active", "level", "rms", "noise_rms",
                         "onsets", "frames", "frame_gap_ms", "silent_for_s"}
    assert snap["available"] is True and snap["frames"] == 3


def test_unavailable_without_pw_record_is_fail_safe(caplog):
    link = vl.VoiceLink(enabled=True, sink="", which=lambda name: None)
    assert link.start() is False
    assert link.available is False
    assert link.wait_active(0.0) is False
    assert link.wait_for_onset(0.0) is False
    link.stop()
    assert link.join() is True


def test_disabled_by_env_never_starts(monkeypatch):
    monkeypatch.setenv("EMORI_VOICE_LINK", "0")
    link = vl.VoiceLink(sink="", spawn=lambda cmd: None)
    assert link.enabled is False
    assert link.start() is False


def test_the_capture_command_taps_the_default_sink_monitor():
    cmd = vl.pw_record_command("")
    assert cmd[0] == "pw-record" and "{ stream.capture.sink=true }" in cmd
    assert "--raw" in cmd and cmd[-1] == "-" and "--target" not in cmd
    named = vl.pw_record_command("alsa_output.reachy")
    assert named[named.index("--target") + 1] == "alsa_output.reachy"


def test_the_tap_takes_out_the_buffer_pw_record_keeps_on_a_pipe():
    """pw-record holds 4096 bytes on a pipe, 128 ms of voice the motion would be late by."""

    command = vl.tap_command("a_sink", which=lambda name: f"/usr/bin/{name}")
    assert command[:2] == ["stdbuf", "-o0"]
    assert command[2:] == vl.pw_record_command("a_sink")


def test_with_no_stdbuf_the_tap_still_runs_and_says_it_is_late(caplog):
    command = vl.tap_command("", which=lambda name: None)
    assert command == vl.pw_record_command("")
    assert "128 ms" in caplog.text


class _Capture:
    """A ``pw-record`` whose reads return what a real one's do, then stop the link."""

    def __init__(self, reads: list[int], stop) -> None:
        self._reads = list(reads)
        self._stop = stop
        self.stdout = self

    def read(self, _size: int) -> bytes:
        if not self._reads:
            self._stop()
            return b""
        return bytes(self._reads.pop(0))

    def terminate(self) -> None:
        pass


def _run_capture(reads: list[int]) -> dict:
    holder: dict = {}
    link = vl.VoiceLink(enabled=True, sink="", which=lambda name: None,
                        spawn=lambda cmd: holder.update(cmd=cmd) or _Capture(reads, link.stop))
    assert link.start() is True
    assert link.join(timeout_s=5.0) is True
    return link.snapshot()


def test_a_capture_that_worked_does_not_count_against_the_tap(monkeypatch):
    """Each PipeWire restart ends a capture that was working. A count of those would turn the tap
    off for good on a robot that stays up long enough."""

    monkeypatch.setattr(vl, "_RETRY_BACKOFF_S", 0.0)
    wanted = vl._MAX_RETRIES + 5
    spawned: list[int] = []

    def spawn(_cmd):
        spawned.append(1)
        if len(spawned) >= wanted:
            link.stop()
        return _Capture([640], lambda: None)

    link = vl.VoiceLink(enabled=True, sink="", which=lambda name: None, spawn=spawn)
    assert link.start() is True
    assert link.join(timeout_s=5.0) is True
    assert len(spawned) == wanted


def test_a_capture_that_never_delivers_is_given_up_on(monkeypatch):
    monkeypatch.setattr(vl, "_RETRY_BACKOFF_S", 0.0)
    spawned: list[int] = []

    def spawn(_cmd):
        spawned.append(1)
        return _Capture([], lambda: None)

    link = vl.VoiceLink(enabled=True, sink="", which=lambda name: None, spawn=spawn)
    assert link.start() is True
    assert link.join(timeout_s=5.0) is True
    assert len(spawned) == vl._MAX_RETRIES + 1


def test_reads_shorter_than_a_frame_make_whole_frames():
    """A read gets one PipeWire cycle, about 10.7 ms. Counted as frames they would halve every
    length here that is a number of frames, the 40 ms dip that makes an onset among them."""

    assert _run_capture([342, 340, 342, 340, 342, 340])["frames"] == 3


def test_the_short_end_of_a_block_is_not_a_frame_of_its_own():
    assert _run_capture([640, 640, 640, 640, 640, 640, 256])["frames"] == 6


def test_the_status_shows_whether_frames_arrive_as_they_are_made_or_in_blocks():
    clock = Clock()
    link = _link(clock)
    assert link.snapshot()["frame_gap_ms"] is None
    _feed(link, clock, 0.0, 20)
    assert link.snapshot()["frame_gap_ms"] == 20.0

    blocks = _link(clock)
    for _ in range(10):
        for _ in range(6):
            blocks.feed(_frame(0.0), now=clock.now)
        clock.advance(0.128)
    assert blocks.snapshot()["frame_gap_ms"] == 0.0


def test_a_rise_out_of_silence_is_remembered_and_a_syllable_inside_a_word_is_not():
    t = {"now": 100.0}
    link = vl.VoiceLink(clock=lambda: t["now"], enabled=True)

    def feed(level: int, frames: int) -> None:
        for _ in range(frames):
            t["now"] += 0.02
            link.feed(struct.pack("<h", level) * 320)

    feed(0, 20)
    assert not link.rose_since(0.0)
    feed(4000, 5)
    rose_at = t["now"] - 0.08
    assert link.rose_since(rose_at - 0.05)
    feed(4000, 10)
    assert not link.rose_since(t["now"] - 0.05)     # still the same word
    feed(0, 5)
    feed(4000, 2)
    assert link.rose_since(t["now"] - 0.05)         # up again out of a dip


def test_the_trace_holds_rises_out_of_silence_and_not_every_syllable(monkeypatch):
    emitted = []
    monkeypatch.setattr(vl.motion_trace, "emit", lambda stream, **f: emitted.append((stream, f)))
    t = {"now": 100.0}
    link = vl.VoiceLink(clock=lambda: t["now"], enabled=True)

    def feed(level: int, frames: int) -> None:
        for _ in range(frames):
            t["now"] += 0.02
            link.feed(struct.pack("<h", level) * 320)

    feed(0, 20)
    feed(2000, 10)
    feed(9000, 10)        # a sharp jump inside the word
    onsets = [f for s, f in emitted if s == "voice" and f.get("edge") == "onset"]
    assert len(onsets) == 1 and onsets[0]["kind"] == "dip"
    assert link.onset_count == 2     # the player still paces on both


def test_with_a_delay_set_the_tap_listens_ahead_of_it(monkeypatch):
    monkeypatch.setenv("EMORI_VOICE_DELAY_MS", "120")
    monkeypatch.delenv("EMORI_VOICE_LINK_SINK", raising=False)
    assert vl.VoiceLink(enabled=True)._sink == vl.VOICE_DELAY_SINK
    monkeypatch.setenv("EMORI_VOICE_LINK_SINK", "another")
    assert vl.VoiceLink(enabled=True)._sink == "another"


def test_with_no_delay_the_tap_listens_to_the_speaker(monkeypatch):
    monkeypatch.delenv("EMORI_VOICE_LINK_SINK", raising=False)
    for value in ("", "0", "nonsense"):
        monkeypatch.setenv("EMORI_VOICE_DELAY_MS", value)
        assert vl.VoiceLink(enabled=True)._sink == ""
