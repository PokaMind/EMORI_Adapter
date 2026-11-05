"""The screenless acknowledgement cue: motion, because the speaker is not available.

Measured on a robot, a learner waits ~3.5 s between finishing their sentence and hearing the
reply.  On a screenless robot that is 3.5 s of nothing, which is indistinguishable from a
crash -- the same wait on the web app is covered by response text streaming in within
~50 ms.  The TTS interlock reserves the speaker for the session's own voice during
a live session, so a spoken backchannel is forbidden by design and motion is the only
channel left.

These pin the two properties that make the cue correct rather than merely present: it
fires on an ACCEPTED turn only, and it is visually distinct from the other session cues.
"""

from __future__ import annotations

from emori_adapter.behavior import repertoire as R


def test_thinking_gesture_exists_and_is_a_valid_intent_list():
    intents = R.thinking_gesture()
    assert isinstance(intents, list) and len(intents) >= 2
    assert all(i.kind == "goto" for i in intents)


def test_the_acknowledgement_lands_first_and_fast():
    """The nod must read as 'heard you' within a beat of the learner stopping."""
    intents = R.thinking_gesture()
    ack = intents[0]
    # down-nod: positive pitch is down (see nod(); greeting uses negative to look UP)
    assert ack.head.pitch > 0
    # short: a slow acknowledgement is not an acknowledgement
    assert ack.duration <= 0.3


def test_it_settles_into_a_held_considering_pose():
    """After the nod it must HOLD, so the silence that follows reads as 'working'."""
    intents = R.thinking_gesture()
    hold = intents[-1]
    assert hold.head.pitch < 0                      # raised, not slumped
    assert hold.antennas == R.ANTENNA_CURIOUS       # asymmetric == considering
    # off-axis: a dead-centre hold is indistinguishable from the neutral resting pose
    assert hold.head.yaw != 0 or hold.head.roll != 0


def test_it_is_distinct_from_the_other_session_cues():
    """A learner must not confuse 'thinking' with 'your turn' or with speech."""
    thinking = R.thinking_gesture()
    listening = R.listening_gesture()
    # listening is symmetric and forward ("go ahead"); thinking is asymmetric and raised
    assert listening[0].antennas != R.ANTENNA_CURIOUS
    assert thinking[-1].antennas == R.ANTENNA_CURIOUS
    # speech is a multi-beat sequence; thinking settles and holds
    assert len(thinking) < len(R.talking_gesture())


def test_total_duration_fits_inside_the_measured_reply_wait():
    """It must finish and hold, not still be moving when the reply starts speaking."""
    total = sum(i.duration for i in R.thinking_gesture())
    assert total < 1.5   # measured submit -> first audible sample was ~3.6 s


def test_submitted_turn_nods_and_rejected_turn_does_not():
    """The guard throws away blips; the robot must not appear to accept what it discarded."""
    from emori_adapter.hostapp import driver as driver_mod

    captured: list[list] = []

    class _Driver(driver_mod.LiveDriver):
        def __init__(self):  # noqa: D107 - bypass the full driver construction
            pass

        def submit_legacy_motion(self, intents, *, drop_if_busy=False):
            captured.append(list(intents))
            return True

    d = _Driver()
    d.counters = {"turn_submitted": 0, "turn_rejected": 0}
    d._lock = __import__("threading").Lock()
    d.watchdog = type("W", (), {"on_submitted": lambda self, now: None,
                                # a rejected turn now buys a bounded second chance
                                # (watchdog.on_turn_rejected); this stub only has to
                                # accept it -- the behaviour is covered in
                                # test_watchdog_reject_extension.py.
                                "on_turn_rejected": lambda self, now: None})()
    d.refresh_presence = lambda: None
    d.touch = lambda: None

    d.on_turn_submitted({})
    assert len(captured) == 1, "an accepted turn must be acknowledged"
    assert captured[0][-1].antennas == R.ANTENNA_CURIOUS

    captured.clear()
    d.on_turn_rejected({})
    assert captured == [], "a guard-rejected blip must NOT be acknowledged"
