"""CueScheduler: recordings are spaced out, and the result says its source."""

from __future__ import annotations

from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CueScheduler
from emori_adapter.behavior.cues import CueConfig

UNFRESH = AffectSnapshot()


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSubmit:
    def __init__(self) -> None:
        self.calls: list[tuple[list, object]] = []

    def __call__(self, intents, guard):
        self.calls.append((list(intents), guard))
        return True

    @property
    def last_intents(self):
        return self.calls[-1][0]


def _scheduler(clock):
    return CueScheduler(FakeSubmit(), clock=clock, config=CueConfig(recorded=True))


def _say(s, text, category=None, body=None):
    return s.on_sentence(text=text, cue_category=category, cue_body=body, lang="en",
                         affect=UNFRESH)


LONG = " ".join(["palabra"] * 12)


def test_two_recordings_never_play_back_to_back():
    clock = FakeClock()
    s = _scheduler(clock)
    first = _say(s, LONG, "tension", "nervous")
    assert first.recorded == "fear1" and first.source == "pokamind"
    clock.advance(1.0)
    second = _say(s, LONG, "levity", "amused")      # a fresh authored cue, 1 s later
    assert second.recorded == "", second.reason      # withheld: the authored shape instead
    assert second.family == "levity" and second.planned
    clock.advance(7.0)
    third = _say(s, LONG, "levity", "amused")       # past the gap: a recording again
    assert third.recorded != ""


def test_a_text_read_family_reports_its_source():
    clock = FakeClock()
    s = _scheduler(clock)
    out = _say(s, "I am truly so very frightened of all of this.")
    assert out.planned and out.family == "tension" and out.source == "text"
    assert out.recorded == "fear1"
    assert "/text:fear/rec-fear1" in out.reason


def test_the_carry_still_colours_untagged_sentences_and_says_so():
    clock = FakeClock()
    s = _scheduler(clock)
    _say(s, LONG, "softening")
    clock.advance(1.0)
    out = _say(s, LONG)
    assert out.family == "softening" and out.source == "carried"
    assert out.recorded == ""


def test_reset_forgets_the_recording_clock():
    clock = FakeClock()
    s = _scheduler(clock)
    assert _say(s, LONG, "tension", "nervous").recorded
    s.reset()
    clock.advance(0.5)
    assert _say(s, LONG, "tension", "nervous").recorded, "a new session starts fresh"
