"""A rejected turn is not silence: somebody spoke and was not heard.

A learner answers, the web app's filled-pause detection scores the entire 1208 ms
answer as one long "uhh" and reports `effectiveSpeechMs: 0`, the guard rejects it, a
second attempt is rejected for length -- and, without this, fifteen seconds later the
robot would announce the session complete. To the person in front of it, the robot ignored them and left.

The watchdog cannot know whether a rejection was a chair or a sentence. It can
know that a rejection is evidence of SOUND, which silence is not, so it buys a
bounded second chance -- while every hard ceiling still holds.
"""

from __future__ import annotations

from emori_adapter.hostapp.watchdog import ConversationWatchdog, TurnState


def _open(now: float = 0.0, **kw) -> ConversationWatchdog:
    wd = ConversationWatchdog(**kw)
    wd.on_armed(now=now)
    wd.on_user_speaking(now=now)
    return wd


def test_a_rejection_buys_another_window_instead_of_ending_the_session():
    wd = _open()
    # 12 s in: the turn has stalled, so it re-prompts (as before).
    assert wd.due(now=12.0) == "reprompt"
    # The learner answers; the endpointer throws it away.
    wd.on_turn_rejected(now=13.0)
    # The old behaviour force-ended at 15 s from open. It must not.
    assert wd.due(now=16.0) is None
    # The clock now runs from the rejection, so they get a full fresh window.
    assert wd.due(now=23.5) == "reprompt"
    assert wd.due(now=28.5) == "force_end"


def test_the_second_chance_is_bounded_so_an_empty_room_still_ends():
    wd = _open(reject_extensions=2)
    for t in (5.0, 10.0, 15.0):        # a blip every five seconds, forever
        wd.on_turn_rejected(now=t)
    # Only the first two extended anything: the clock still runs from t=10.
    assert wd.due(now=24.0) == "reprompt"
    assert wd.due(now=25.1) == "force_end"


def test_the_absolute_ceiling_still_caps_the_whole_open_window():
    wd = _open(abs_ceiling_s=30.0)
    for t in (5.0, 12.0, 20.0, 28.0):
        wd.on_turn_rejected(now=t)
    assert wd.due(now=31.0) == "force_end"
    assert wd.state is TurnState.TIMED_OUT


def test_zero_extensions_reproduces_the_old_behaviour_exactly():
    wd = _open(reject_extensions=0)
    wd.on_turn_rejected(now=5.0)
    assert wd.due(now=10.5) == "reprompt"
    assert wd.due(now=15.5) == "force_end"


def test_a_rejection_outside_an_open_turn_is_ignored():
    wd = ConversationWatchdog()
    wd.on_armed(now=0.0)                 # ARMED is timer-inert
    wd.on_turn_rejected(now=1.0)
    assert wd.due(now=99.0) is None
    assert wd.state is TurnState.ARMED


def test_a_submitted_turn_forgets_the_rejections_it_took_to_get_there():
    wd = _open(reject_extensions=1)
    wd.on_turn_rejected(now=4.0)
    wd.on_submitted(now=6.0)
    wd.on_ai_utterance(now=8.0)
    wd.on_armed(now=9.0)
    wd.on_user_speaking(now=10.0)
    wd.on_turn_rejected(now=14.0)        # the NEXT turn gets its own allowance
    assert wd.due(now=18.0) is None
