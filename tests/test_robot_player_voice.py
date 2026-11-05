"""RobotPlayer, voice-locked: motion happens WHILE the sentence is spoken.

The player runs its real thread against a recording client and a scripted voice.
Every assertion is about WHEN the robot was commanded relative to the voice and to
a preempting sentence -- the four properties voice-locked motion needs:

* a preempting sentence takes the head within one slice, not after a 1.2 s goto;
* a speech plan waits (bounded) for the voice to become audible before moving;
* a speech plan jumps to its settle the moment the voice stops;
* a Pollen recording is STOPPED (with the daemon's real contract) when the voice
  stops or a newer sentence arrives -- and never cut while the voice still speaks.
"""

from __future__ import annotations

import threading
import time

import pytest

from emori_adapter.hostapp.driver import RobotPlayer
from emori_adapter.models import HeadPose, MotionIntent
from emori_adapter.robot.client import DryRunRobotClient


class RecordingClient(DryRunRobotClient):
    def __init__(self) -> None:
        super().__init__()
        self.applied: list[tuple[float, MotionIntent]] = []
        self.played: list[tuple[float, str]] = []
        self.stopped: list[tuple[float, str]] = []

    def wake_up(self) -> None:
        # DryRun's wake_up appends a raw intent to ``applied``; these tests only
        # measure the batches they submit, so the boot wake records nothing.
        return None

    def apply(self, intent: MotionIntent) -> None:
        self.applied.append((time.monotonic(), intent))

    def play_recorded_move(self, name: str, *, supersede: bool = False) -> str | None:
        self.played.append((time.monotonic(), name))
        return "uuid-" + name

    def stop_recorded_move(self, uuid: str) -> None:
        self.stopped.append((time.monotonic(), uuid))


class ScriptedVoice:
    """A VoiceLink stand-in the test drives: speak(), hush(), pulse()."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.level = 1.0
        self._cv = threading.Condition()
        self._active = False
        self._last_active: float | None = None
        self._onsets = 0

    # --- scripting -----------------------------------------------------------
    def speak(self) -> None:
        with self._cv:
            self._active = True
            self._last_active = time.monotonic()
            self._onsets += 1
            self._cv.notify_all()

    def hush(self) -> None:
        with self._cv:
            self._active = False
            self._cv.notify_all()

    def pulse(self) -> None:
        with self._cv:
            if self._active:
                self._last_active = time.monotonic()
            self._onsets += 1
            self._cv.notify_all()

    # --- the VoiceLink surface the player uses -------------------------------
    @property
    def active(self) -> bool:
        with self._cv:
            if self._active:
                self._last_active = time.monotonic()
            return self._active

    @property
    def onset_count(self) -> int:
        with self._cv:
            return self._onsets

    def silent_for_s(self) -> float:
        with self._cv:
            if self._active:
                self._last_active = time.monotonic()
                return 0.0
            if self._last_active is None:
                return float("inf")
            return time.monotonic() - self._last_active

    def active_since(self, t0: float) -> bool:
        with self._cv:
            if self._active:
                self._last_active = time.monotonic()
            return self._last_active is not None and self._last_active >= t0

    def rose_since(self, t0: float) -> bool:
        return False

    def wait_for_frame(self, timeout_s: float) -> None:
        with self._cv:
            self._cv.wait(min(timeout_s, 0.02))

    def wait_active(self, timeout_s: float, *, stop=None) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while not self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (stop is not None and stop.is_set()):
                    return False
                self._cv.wait(min(0.05, remaining))
            return True

    def wait_for_onset(self, timeout_s: float, *, after=None, stop=None, abort=None) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            base = self._onsets if after is None else after
            while self._onsets <= base:
                if abort is not None:
                    self._cv.release()
                    try:
                        if abort():
                            return False
                    finally:
                        self._cv.acquire()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (stop is not None and stop.is_set()):
                    return False
                self._cv.wait(min(0.05, remaining))
            return True


class Gen:
    """A generation guard like the cue scheduler's."""

    def __init__(self) -> None:
        self.n = 0

    def guard(self):
        mine = self.n
        return lambda: mine == self.n

    def bump(self) -> None:
        self.n += 1


@pytest.fixture
def fast_wake(monkeypatch):
    monkeypatch.setattr(RobotPlayer, "_WAKE_SETTLE_S", 0.01)


def _beats(n: int, *, duration: float, speech: bool = True, pace: str = "fixed") -> list[MotionIntent]:
    out = [MotionIntent(kind="goto", head=HeadPose(pitch=float(i)), antennas=(0.8, -0.8),
                        duration=duration, speech=speech, pace=pace, antenna_voice=True,
                        reason="beat %d" % i) for i in range(n)]
    out.append(MotionIntent(kind="goto", head=HeadPose(pitch=2.0), duration=0.3,
                            speech=speech, reason="cue settle"))
    return out


def _player(voice=None) -> tuple[RobotPlayer, RecordingClient]:
    client = RecordingClient()
    player = RobotPlayer(client, voice=voice)
    # Let the boot wake (DryRun health() is True; settle patched to 10 ms) finish so
    # the first submitted batch is measured from a quiet player.
    time.sleep(0.15)
    return player, client


def _wait(pred, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _reasons(client: RecordingClient) -> list[str]:
    return [i.reason for _, i in client.applied]


# --- preemption is immediate ------------------------------------------------------

def test_a_preempting_sentence_takes_the_head_within_a_slice(fast_wake):
    player, client = _player()
    gen = Gen()
    try:
        player.submit(_beats(3, duration=1.0), guard=gen.guard())
        assert _wait(lambda: len(client.applied) >= 1)
        n_old = len(client.applied)
        t_bump = time.monotonic()
        gen.bump()
        # The old plan must stop issuing; the next batch must start promptly.
        player.submit(_beats(1, duration=0.2, speech=False), guard=gen.guard())
        assert _wait(lambda: any(t >= t_bump and i.reason == "beat 0"
                                 for t, i in client.applied[n_old:]))
        first_new = next(t for t, i in client.applied[n_old:] if i.reason == "beat 0")
        assert first_new - t_bump < 0.4, "the new sentence waited for the old goto to finish"
        # At most the beat already in flight came from the old plan after the bump.
        old_after = [i for t, i in client.applied[n_old:] if i.reason in ("beat 1", "beat 2")]
        assert len(old_after) <= 1
    finally:
        player.stop()
        player.join()


# --- hold until the voice is audible ---------------------------------------------

def test_a_speech_plan_waits_for_the_voice_then_moves(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        t0 = time.monotonic()
        player.submit(_beats(2, duration=0.2))
        time.sleep(0.3)
        assert client.applied == [], "moved before the voice was audible"
        voice.speak()
        assert _wait(lambda: len(client.applied) >= 1)
        assert client.applied[0][0] - t0 >= 0.25
    finally:
        player.stop()
        player.join()


def test_the_hold_is_bounded_when_the_voice_never_comes(fast_wake, monkeypatch):
    monkeypatch.setattr(RobotPlayer, "_VOICE_HOLD_S", 0.6)
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        t0 = time.monotonic()
        player.submit(_beats(1, duration=0.1))
        assert _wait(lambda: len(client.applied) >= 1, timeout_s=2.5)
        assert 0.5 <= client.applied[0][0] - t0 <= 1.5
    finally:
        player.stop()
        player.join()


def test_the_hold_outlasts_the_measured_event_to_audio_lead(fast_wake):
    """The sentence event can lead its audio by 1.0-3.5 s (6.4 s on the intro). The plan must still be waiting when the voice finally comes."""

    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        player.submit(_beats(2, duration=0.2))
        time.sleep(1.6)
        assert client.applied == [], "performed in silence"
        voice.speak()
        assert _wait(lambda: len(client.applied) >= 1, timeout_s=1.0)
    finally:
        player.stop()
        player.join()


def test_a_plan_sustains_with_micro_beats_while_the_voice_outlives_it(fast_wake):
    """A 4 s estimate must not leave the head still for the rest of a 7 s sentence."""

    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_beats(2, duration=0.2))          # ~0.4 s of plan + settle
        assert _wait(lambda: any(i.reason == "speaking (sustain)" for _, i in client.applied),
                     timeout_s=2.5), "no sustain beats while the voice kept speaking"
        assert "cue settle" not in _reasons(client), "settled while the voice still spoke"
        voice.hush()
        assert _wait(lambda: "cue settle" in _reasons(client), timeout_s=2.5)
        sustain = [i for _, i in client.applied if i.reason == "speaking (sustain)"]
        assert sustain and all(i.speech and i.pace == "voice" for i in sustain)
    finally:
        player.stop()
        player.join()


def test_a_non_speech_batch_ignores_the_voice(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        player.submit(_beats(1, duration=0.1, speech=False))
        assert _wait(lambda: len(client.applied) >= 1, timeout_s=0.5)
    finally:
        player.stop()
        player.join()


# --- settle when the voice stops --------------------------------------------------

def test_a_speech_plan_settles_the_moment_the_voice_stops(fast_wake):
    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_beats(8, duration=0.3))
        assert _wait(lambda: len(client.applied) >= 2)
        voice.hush()
        assert _wait(lambda: "cue settle" in _reasons(client), timeout_s=2.0)
        reasons = _reasons(client)
        assert reasons[-1] == "cue settle"
        assert len([r for r in reasons if r.startswith("beat")]) < 8, "kept beating after the voice"
    finally:
        player.stop()
        player.join()


# --- recorded moves ---------------------------------------------------------------

def _recorded(name: str, dur: float) -> list[MotionIntent]:
    return [MotionIntent(kind="play_recorded", move=name, duration=dur, speech=True,
                         reason="cue (recorded %s)" % name),
            MotionIntent(kind="goto", head=HeadPose(pitch=2.0), duration=0.1, speech=True,
                         reason="cue settle")]


def test_a_recording_is_stopped_when_the_voice_stops_and_the_settle_follows(fast_wake):
    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_recorded("fear1", 3.5))
        assert _wait(lambda: len(client.played) == 1)
        time.sleep(0.7)
        voice.hush()
        assert _wait(lambda: len(client.stopped) == 1, timeout_s=2.0)
        assert client.stopped[0][1] == "uuid-fear1"
        assert client.stopped[0][0] - client.played[0][0] < 2.5
        assert _wait(lambda: "cue settle" in _reasons(client))
    finally:
        player.stop()
        player.join()


def test_a_recording_is_never_cut_while_the_voice_still_speaks(fast_wake):
    """The move plays out in full under a live voice, then the hands keep talking
    (sustain) until the voice stops -- and only then the settle."""

    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_recorded("grateful1", 1.0))
        assert _wait(lambda: len(client.played) == 1)
        assert _wait(lambda: any(i.reason == "speaking (sustain)" for _, i in client.applied),
                     timeout_s=2.5)
        assert client.stopped == [], "the recording was cut while the voice still spoke"
        assert "cue settle" not in _reasons(client)
        voice.hush()
        assert _wait(lambda: "cue settle" in _reasons(client), timeout_s=2.5)
    finally:
        player.stop()
        player.join()


def test_a_preempted_recording_is_stopped_and_its_settle_skipped(fast_wake):
    player, client = _player()
    gen = Gen()
    try:
        player.submit(_recorded("relief1", 5.0), guard=gen.guard())
        assert _wait(lambda: len(client.played) == 1)
        gen.bump()
        assert _wait(lambda: len(client.stopped) == 1, timeout_s=1.0)
        time.sleep(0.2)
        assert "cue settle" not in _reasons(client)
    finally:
        player.stop()
        player.join()


# --- the voice paces the beats and the antennas follow the level -------------------

def test_voice_paced_beats_advance_on_onsets_not_on_the_clock(fast_wake):
    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_beats(3, duration=0.34, pace="voice"))
        assert _wait(lambda: len(client.applied) >= 1)
        # Without onsets a beat holds up to max(0.34, 0.45) s; with an onset once the move
        # has finished (0.34 s) it advances early, and never before that.
        t_first = client.applied[0][0]
        time.sleep(0.36)
        voice.pulse()
        assert _wait(lambda: len(client.applied) >= 2)
        gap = client.applied[1][0] - t_first
        assert 0.33 <= gap <= 0.43, gap
    finally:
        player.stop()
        player.join()


def test_antennas_follow_the_voice_level(fast_wake):
    voice = ScriptedVoice()
    voice.speak()
    voice.level = 0.0
    player, client = _player(voice)
    try:
        player.submit(_beats(1, duration=0.1))
        assert _wait(lambda: len(client.applied) >= 1)
        a = client.applied[0][1].antennas
        assert a == pytest.approx((0.8 * 0.55, -0.8 * 0.55))
    finally:
        player.stop()
        player.join()


# --- never before the voice ----------------------------------------------------------

def test_a_voice_that_never_comes_means_no_motion_only_the_settle(fast_wake, monkeypatch):
    """Motion in silence is what this lane removes: on a hold timeout the body of the
    plan is skipped and only the settle is performed."""

    monkeypatch.setattr(RobotPlayer, "_VOICE_HOLD_S", 0.4)
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        player.submit(_beats(3, duration=0.2))
        assert _wait(lambda: "cue settle" in _reasons(client), timeout_s=2.0)
        assert not any(r.startswith("beat") for r in _reasons(client)), "moved in silence"
    finally:
        player.stop()
        player.join()


def test_a_preempting_sentence_is_honoured_during_the_hold(fast_wake):
    """The hold for sentence n must not delay sentence n+1 (the 20 s hold is sliced)."""

    voice = ScriptedVoice()
    player, client = _player(voice)
    gen = Gen()
    try:
        player.submit(_beats(2, duration=0.2), guard=gen.guard())      # holds for the voice
        time.sleep(0.4)
        t_bump = time.monotonic()
        gen.bump()
        player.submit(_beats(1, duration=0.1, speech=False), guard=gen.guard())
        assert _wait(lambda: any(i.reason == "beat 0" for _, i in client.applied), timeout_s=1.0)
        t_new = next(t for t, i in client.applied if i.reason == "beat 0")
        assert t_new - t_bump < 0.5, "the new sentence waited out the old hold"
    finally:
        player.stop()
        player.join()


# --- a breath mid-sentence -------------------------------------------------------------

def test_the_plan_resumes_when_the_voice_comes_back_after_a_pause(fast_wake):
    """Settle on the pause, sustain again when the voice returns, settle again at the end."""

    voice = ScriptedVoice()
    voice.speak()
    player, client = _player(voice)
    try:
        player.submit(_beats(2, duration=0.15))
        assert _wait(lambda: len([r for r in _reasons(client) if r.startswith("beat")]) >= 2)
        voice.hush()
        assert _wait(lambda: _reasons(client).count("cue settle") >= 1, timeout_s=2.0)
        n_after_first_settle = len(client.applied)
        voice.speak()                      # the breath is over, the sentence goes on
        assert _wait(lambda: any(i.reason == "speaking (sustain)"
                                 for _, i in client.applied[n_after_first_settle:]),
                     timeout_s=1.5), "did not resume after the pause"
        voice.hush()
        assert _wait(lambda: _reasons(client).count("cue settle") >= 2, timeout_s=2.0)
    finally:
        player.stop()
        player.join()


# --- one move in flight ---------------------------------------------------------------------

def test_a_beat_is_not_released_before_the_last_one_has_finished(fast_wake):
    player, client = _player()
    try:
        player.submit(_beats(3, duration=0.25, speech=False))
        assert _wait(lambda: len(client.applied) >= 3)
        times = [t for t, _ in client.applied]
        assert all(b - a >= 0.23 for a, b in zip(times, times[1:]))
    finally:
        player.stop()
        player.join()


def test_a_voice_paced_beat_waits_for_the_last_one_to_finish(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(3, duration=0.3, pace="voice"))
        assert _wait(lambda: len(client.applied) >= 1)
        for _ in range(12):           # syllables far faster than the move can repeat
            voice.pulse()
            time.sleep(0.02)
        assert _wait(lambda: len(client.applied) >= 2)
        assert client.applied[1][0] - client.applied[0][0] >= 0.28
    finally:
        player.stop()
        player.join()


def test_the_old_pacing_comes_back_with_the_kill_switch(fast_wake, monkeypatch):
    monkeypatch.setenv("EMORI_MOTION_SYNC", "0")
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(3, duration=0.4, pace="voice"))
        assert _wait(lambda: len(client.applied) >= 1)
        for _ in range(20):
            voice.pulse()
            time.sleep(0.02)
        assert _wait(lambda: len(client.applied) >= 2)
        assert client.applied[1][0] - client.applied[0][0] < 0.38
    finally:
        player.stop()
        player.join()


# --- what replaces what -----------------------------------------------------------------------

def test_the_first_move_and_the_settle_replace_what_came_before(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(2, duration=0.12))
        assert _wait(lambda: len(client.applied) >= 2)
        voice.hush()
        assert _wait(lambda: any(i.reason == "cue settle" for _, i in client.applied))
        flags = {i.reason: i.supersede for _, i in client.applied}
        assert flags["beat 0"] is True and flags["beat 1"] is False
        assert flags["cue settle"] is True
    finally:
        player.stop()
        player.join()


def test_a_sustain_beat_does_not_replace_anything(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(1, duration=0.05))
        assert _wait(lambda: any("sustain" in i.reason for _, i in client.applied))
        assert all(not i.supersede for _, i in client.applied if "sustain" in i.reason)
    finally:
        player.stop()
        player.join()


# --- the voice ends in the middle of a move ---------------------------------------------------

def test_the_settle_goes_out_when_the_voice_stops_not_when_the_move_ends(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(2, duration=2.0))
        assert _wait(lambda: len(client.applied) >= 1)
        t_hush = time.monotonic()
        voice.hush()
        assert _wait(lambda: any(i.reason == "cue settle" for _, i in client.applied))
        settled = next(t for t, i in client.applied if i.reason == "cue settle")
        assert settled - t_hush < RobotPlayer._VOICE_END_S + 0.12
    finally:
        player.stop()
        player.join()


# --- whose voice is it ------------------------------------------------------------------------

def test_an_onset_between_the_event_and_the_pickup_counts(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    gen = Gen()
    try:
        # The player is busy with a batch that is not a sentence when the event arrives.
        player.submit([MotionIntent(kind="goto", duration=0.3, reason="other")], guard=gen.guard())
        assert _wait(lambda: len(client.applied) >= 1)
        t_other = client.applied[0][0]
        player.submit(_beats(2, duration=0.1))     # the event
        voice.speak()                              # its voice, while the player is still busy
        assert _wait(lambda: any(i.reason == "beat 0" for _, i in client.applied), 1.0)
        # Straight after the other batch, and not another 0.25 s to decide it is the voice.
        started = next(t for t, i in client.applied if i.reason == "beat 0")
        assert started - t_other < 0.45
    finally:
        player.stop()
        player.join()


def test_a_rise_just_before_the_event_counts_as_this_sentences_voice(fast_wake):
    voice = ScriptedVoice()
    voice.rose_since = lambda t0: True
    player, client = _player(voice)
    try:
        t0 = time.monotonic()
        player.submit(_beats(2, duration=0.1))
        assert _wait(lambda: len(client.applied) >= 1, 1.0)
        assert client.applied[0][0] - t0 < 0.1
    finally:
        player.stop()
        player.join()


def test_the_last_sentences_hangover_is_not_this_ones_voice(fast_wake):
    voice = ScriptedVoice()
    voice.silent_for_s = lambda: 0.15             # still inside the hangover, no energy now
    type(voice).active = property(lambda self: True)
    player, client = _player(voice)
    try:
        player.submit(_beats(2, duration=0.1))
        time.sleep(0.5)
        assert client.applied == []
    finally:
        player.stop()
        player.join()
        del type(voice).active


# --- the engagement lane stands down -----------------------------------------------------------

def test_a_long_recording_gives_way_at_once_when_its_batch_yields(fast_wake):
    """A batch of moves from the graph must not sit in front of a session's first sentence."""

    client = RecordingClient()
    player = RobotPlayer(client)
    flip = [False]
    try:
        assert _wait(lambda: not player.busy)
        player.submit([MotionIntent(kind="play_recorded", move="m", duration=3.0)],
                      yields=lambda: flip[0])
        assert _wait(lambda: client.played)
        started = time.monotonic()
        flip[0] = True
        assert _wait(lambda: client.stopped, timeout_s=1.5)
        assert time.monotonic() - started < 0.5
        assert _wait(lambda: not player.busy, timeout_s=1.5)
    finally:
        player.stop()


def test_a_plan_counts_as_performing_until_it_has_finished(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        assert player.performing_speech is False
        voice.speak()
        player.submit(_beats(1, duration=0.05))
        assert player.performing_speech is True
        assert _wait(lambda: len(client.applied) >= 2)
        assert player.performing_speech is True      # sustaining
        voice.hush()
        assert _wait(lambda: player.performing_speech is False, 4.0)
    finally:
        player.stop()
        player.join()


def test_a_revoke_wakes_a_waiting_player_at_once(fast_wake):
    player, client = _player()
    gen = Gen()
    try:
        player.submit(_beats(2, duration=5.0, speech=False), guard=gen.guard())
        assert _wait(lambda: len(client.applied) >= 1)
        gen.bump()
        player.wake()
        player.submit([MotionIntent(kind="goto", duration=0.1, reason="next")])
        assert _wait(lambda: any(i.reason == "next" for _, i in client.applied), 0.2)
    finally:
        player.stop()
        player.join()


# --- a batch that gives way ---------------------------------------------------------------------

def _nod() -> list[MotionIntent]:
    return [MotionIntent(kind="goto", head=HeadPose(pitch=16.0), duration=2.0, reason="down"),
            MotionIntent(kind="goto", head=HeadPose(pitch=-2.0), duration=0.3, reason="back")]


def test_a_batch_that_gives_way_brings_the_head_back_as_it_goes(fast_wake):
    player, client = _player()
    gave_way = {"now": False}
    try:
        player.submit(_nod(), yields=lambda: gave_way["now"])
        assert _wait(lambda: len(client.applied) >= 1)
        t0 = time.monotonic()
        gave_way["now"] = True
        assert _wait(lambda: any(i.reason == "back" for _, i in client.applied), 1.0)
        back = next((t, i) for t, i in client.applied if i.reason == "back")
        assert back[0] - t0 < 0.2 and back[1].supersede is True
        player.submit([MotionIntent(kind="goto", duration=0.1, reason="sentence")])
        assert _wait(lambda: any(i.reason == "sentence" for _, i in client.applied), 1.0)
    finally:
        player.stop()
        player.join()


def test_a_batch_that_has_given_way_before_it_starts_moves_nothing(fast_wake):
    player, client = _player()
    try:
        player.submit(_nod(), yields=lambda: True)
        time.sleep(0.15)
        assert client.applied == []
    finally:
        player.stop()
        player.join()


def test_a_revoked_batch_sends_nothing_more_even_if_it_would_give_way(fast_wake):
    # Ownership gone is not a sentence taking over: not one more move may go out.
    player, client = _player()
    gen = Gen()
    try:
        player.submit(_nod(), guard=gen.guard(), yields=lambda: False)
        assert _wait(lambda: len(client.applied) >= 1)
        gen.bump()
        time.sleep(0.15)
        assert [i.reason for _, i in client.applied] == ["down"]
    finally:
        player.stop()
        player.join()


# --- the page's own line on the speaker --------------------------------------------------------

def test_nothing_binds_while_the_pages_own_line_is_playing(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        player.hold_voice(True)
        player.submit(_beats(2, duration=0.1))
        voice.speak()                              # the covering line, on the same speaker
        for _ in range(5):
            voice.pulse()
            time.sleep(0.04)
        assert client.applied == []
        player.hold_voice(False)
        time.sleep(0.02)
        assert client.applied == [], "its last syllable must not count"
        voice.pulse()                              # this sentence's own first onset
        assert _wait(lambda: len(client.applied) >= 1, 1.0)
    finally:
        player.stop()
        player.join()


def test_a_page_that_goes_away_cannot_hold_binding_for_long(fast_wake, monkeypatch):
    monkeypatch.setattr(RobotPlayer, "_VOICE_COVER_MAX_S", 0.15)
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        player.hold_voice(True)
        player.submit(_beats(2, duration=0.1))
        voice.speak()
        assert _wait(lambda: len(client.applied) >= 1, 1.5)
    finally:
        player.stop()
        player.join()


def test_the_last_move_of_a_batch_gives_way_too(fast_wake):
    player, client = _player()
    gave_way = {"now": False}
    try:
        player.submit([MotionIntent(kind="goto", duration=3.0, reason="only")],
                      yields=lambda: gave_way["now"])
        assert _wait(lambda: len(client.applied) >= 1)
        gave_way["now"] = True
        player.submit([MotionIntent(kind="goto", duration=0.1, reason="sentence")])
        assert _wait(lambda: any(i.reason == "sentence" for _, i in client.applied), 0.5)
    finally:
        player.stop()
        player.join()


def _family_plan(n: int, *, duration: float, family: str) -> list[MotionIntent]:
    return [i.model_copy(update={"tint": family}) for i in _beats(n, duration=duration)]


def test_a_sentence_that_outlasts_its_plan_stays_in_its_family(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_family_plan(1, duration=0.05, family="tension"))
        assert _wait(lambda: any("sustain" in i.reason for _, i in client.applied))
        beat = next(i for _, i in client.applied if "sustain" in i.reason)
        assert beat.reason == "speaking (sustain tension)" and beat.tint == "tension"
    finally:
        player.stop()
        player.join()


def test_a_sentence_with_no_family_sustains_as_it_always_did(fast_wake):
    voice = ScriptedVoice()
    player, client = _player(voice)
    try:
        voice.speak()
        player.submit(_beats(1, duration=0.05))
        assert _wait(lambda: any("sustain" in i.reason for _, i in client.applied))
        beat = next(i for _, i in client.applied if "sustain" in i.reason)
        assert beat.reason == "speaking (sustain)"
    finally:
        player.stop()
        player.join()
