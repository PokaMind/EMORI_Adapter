"""Cue scheduling: newest sentence wins, and the robot always settles.

The scheduler is the only stateful part of the expression pipeline, so these
tests pin the two behaviours a live session depends on: the generation guard that
lets sentence n+1 preempt sentence n mid-flight, and the clock-based ``speaking``
window the per-cycle engagement lane yields to.
"""

from __future__ import annotations

from emori_adapter.behavior import repertoire as R
from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CueScheduler
from emori_adapter.behavior.cues import CueConfig

FRESH = AffectSnapshot(valence=0.4, arousal=0.3, escalation=0.0, engagement_level=3, fresh=True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSubmit:
    """Stands in for driver.submit_legacy_motion(intents, drop_if_busy=False, guard=)."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[list, object]] = []

    def __call__(self, intents, guard):
        self.calls.append((list(intents), guard))
        return self.accept

    @property
    def last_intents(self):
        return self.calls[-1][0]

    @property
    def guards(self):
        return [guard for _, guard in self.calls]


def _sentence(scheduler, text="hello there my friend how are you", category="levity", body=None):
    return scheduler.on_sentence(text=text, cue_category=category, cue_body=body,
                                 lang="en", affect=FRESH)


def _scheduler(submit=None, clock=None, **kwargs):
    return CueScheduler(submit or FakeSubmit(), clock=clock or FakeClock(),
                        config=kwargs.pop("config", CueConfig()), **kwargs)


# --- preemption -----------------------------------------------------------


def test_a_new_sentence_preempts_the_previous_plan():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)

    first = _sentence(scheduler, category="tension")
    assert first.planned and first.family == "tension"
    assert submit.guards[0]() is True

    second = _sentence(scheduler, category="levity")
    assert second.planned
    # RobotPlayer re-checks the guard between intents: sentence n's tail dies here.
    assert submit.guards[0]() is False
    assert submit.guards[1]() is True


def test_tts_ended_submits_exactly_one_settle_and_stops_speaking():
    submit = FakeSubmit()
    clock = FakeClock()
    scheduler = _scheduler(submit, clock)

    _sentence(scheduler)
    assert scheduler.speaking is True

    scheduler.on_tts_ended()
    assert len(submit.calls) == 2
    assert len(submit.last_intents) == 1
    assert submit.last_intents[0].reason.startswith("cue settle")
    assert submit.last_intents[0].head.pitch == R.settle_to_attend().head.pitch
    assert scheduler.speaking is False
    assert submit.guards[0]() is False  # the sentence's tail is dead too


def test_session_end_and_reset_submit_nothing_and_kill_the_tail():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)
    _sentence(scheduler)

    scheduler.on_session_end()
    assert len(submit.calls) == 1
    assert submit.guards[0]() is False
    assert scheduler.speaking is False

    _sentence(scheduler)
    assert scheduler.counters["planned"] == 2
    scheduler.reset()
    assert len(submit.calls) == 2
    assert submit.guards[-1]() is False
    assert scheduler.counters == {"planned": 0, "suppressed": 0, "not_owner": 0,
                                  "settled": 0, "cue_only": 0}


def test_preempt_abandons_the_plan_without_submitting():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)
    _sentence(scheduler)

    scheduler.preempt("escalation")
    assert len(submit.calls) == 1
    assert submit.guards[0]() is False
    assert scheduler.speaking is False


# --- the speaking window --------------------------------------------------


def test_speaking_is_clock_based_and_expires_with_the_budget():
    submit = FakeSubmit()
    clock = FakeClock()
    scheduler = _scheduler(submit, clock)

    result = _sentence(scheduler)
    assert scheduler.speaking is True
    clock.advance(result.budget_s - 0.01)
    assert scheduler.speaking is True
    clock.advance(0.02)
    assert scheduler.speaking is False


def test_a_refused_submit_never_claims_the_head():
    submit = FakeSubmit(accept=False)
    scheduler = _scheduler(submit)

    result = _sentence(scheduler)
    assert result.planned is False
    assert result.reason == "not_owner"
    assert scheduler.speaking is False
    assert scheduler.counters["not_owner"] == 1


def test_a_raising_submit_is_never_fatal():
    def boom(intents, guard):
        raise RuntimeError("player gone")

    scheduler = _scheduler(boom)
    result = _sentence(scheduler)
    assert result.planned is False and result.reason == "not_owner"
    assert scheduler.speaking is False


# --- suppression ----------------------------------------------------------


def test_head_reserved_plans_nothing():
    submit = FakeSubmit()
    reserved = {"value": True}
    scheduler = _scheduler(submit, head_reserved=lambda: reserved["value"])

    result = _sentence(scheduler)
    assert result.planned is False and result.reason == "head_reserved"
    assert submit.calls == []
    assert scheduler.speaking is False
    assert scheduler.counters["suppressed"] == 1

    reserved["value"] = False
    assert _sentence(scheduler).planned is True


def test_a_broken_head_predicate_does_not_stop_the_session():
    def boom():
        raise RuntimeError("gesture ticker gone")

    submit = FakeSubmit()
    scheduler = _scheduler(submit, head_reserved=boom)
    assert _sentence(scheduler).planned is True


def test_cue_motion_kill_switch_restores_the_legacy_gesture(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_MOTION", "0")
    submit = FakeSubmit()
    scheduler = CueScheduler(submit, clock=FakeClock())  # config comes from the env

    text = "hello there my friend how are you"
    result = _sentence(scheduler, text=text)
    assert result.planned is True and result.family == ""
    assert result.reason == "motion_off"
    beats = max(2, min(8, len(text) // 25))
    expected = R.talking_gesture(beats=beats, reason="speaking (AI turn)")
    assert [i.model_dump() for i in submit.last_intents] == [i.model_dump() for i in expected]


def test_uncategorized_sentences_still_speak():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)
    result = scheduler.on_sentence(text="Hello.", cue_category=None, cue_body=None,
                                   lang="ca", affect=AffectSnapshot())
    assert result.planned is True and result.family == ""
    assert result.speaking is True          # the talking flow owns the head while it runs
    assert scheduler.speaking is True
    assert submit.last_intents


# --- the carried family ---------------------------------------------------


def _untagged(scheduler, text="and that is perfectly fine to feel"):
    return scheduler.on_sentence(text=text, cue_category=None, cue_body=None,
                                 lang="en", affect=FRESH)


def test_an_untagged_sentence_inherits_the_turns_family():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)

    tagged = _sentence(scheduler, category="tension")
    assert tagged.family == "tension"

    carried = _untagged(scheduler)
    assert carried.family == "tension"
    assert "/carried" in carried.reason
    assert carried.speaking is True


def test_the_carry_clears_at_end_of_turn():
    submit = FakeSubmit()
    scheduler = _scheduler(submit)
    _sentence(scheduler, category="tension")
    scheduler.on_tts_ended()

    fresh = _untagged(scheduler)
    assert fresh.family == ""
    assert fresh.reason.startswith("talking/")


def test_the_carry_expires_after_its_ttl():
    submit = FakeSubmit()
    clock = FakeClock()
    scheduler = _scheduler(submit, clock)
    _sentence(scheduler, category="levity")

    clock.advance(21.0)
    stale = _untagged(scheduler)
    assert stale.family == ""
    assert stale.reason.startswith("talking/")


def test_carry_can_be_disabled():
    submit = FakeSubmit()
    scheduler = _scheduler(submit, config=CueConfig(carry=False))
    _sentence(scheduler, category="tension")

    result = _untagged(scheduler)
    assert result.family == ""
    assert result.reason.startswith("talking/")


def test_a_repeated_line_does_not_repeat_its_motion():
    """The per-sentence index feeds the seed, so even identical text varies."""

    submit = FakeSubmit()
    scheduler = _scheduler(submit)
    _untagged(scheduler, text="I hear you.")
    first = [i.model_dump() for i in submit.last_intents]
    _untagged(scheduler, text="I hear you.")
    second = [i.model_dump() for i in submit.last_intents]
    assert first != second


def test_every_revoke_is_announced():
    woken = []
    sched = CueScheduler(lambda intents, guard: True, on_revoke=lambda: woken.append(1))
    sched.preempt("test")
    sched.on_session_end()
    assert len(woken) == 2


def test_a_cue_with_no_words_reports_the_family_its_moves_were_planned_from():
    sent = []
    sched = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                         config=CueConfig(recorded=False, talk=True, carry=True),
                         voice_available=lambda: True)
    only = sched.on_sentence(text="", cue_category="tension", cue_body=None, lang="en",
                             affect=AffectSnapshot(fresh=True, escalation=0.95))
    assert sent and all("softening" in intent.reason for intent in sent[0])
    assert only.family == "softening" and only.reason == "cue_only/softening"


def test_a_cue_with_no_words_whose_batch_was_refused_says_not_owner():
    sched = CueScheduler(lambda intents, guard: False,
                         config=CueConfig(recorded=False, talk=True, carry=True),
                         voice_available=lambda: True)
    only = sched.on_sentence(text="", cue_category="levity", cue_body=None, lang="en",
                             affect=AffectSnapshot(fresh=True))
    assert not only.planned and only.reason == "not_owner"
    assert sched.counters["not_owner"] == 1


def test_a_cue_with_no_words_moves_nothing_when_nothing_can_hear_a_sound():
    sent = []
    sched = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                         config=CueConfig(recorded=False, talk=True, carry=True),
                         voice_available=lambda: False)
    only = sched.on_sentence(text="", cue_category="levity", cue_body=None, lang="en",
                             affect=AffectSnapshot(fresh=True))
    assert not only.planned and only.reason == "cue_only/levity" and sent == []
    after = sched.on_sentence(text="The room is on the second floor.", cue_category=None,
                              cue_body=None, lang="en", affect=AffectSnapshot(fresh=True))
    assert after.family == "levity" and after.source == "carried"
