"""The hostapp side of the per-sentence expression lane.

``driver.on_utterance`` has always thrown away everything on the utterance envelope
except the text length and the seq. Pokamind puts the sentence's cue FAMILY there --
extracted from the raw text before the TTS ladder mutates it -- and that field is the
whole expression pipeline. These pin the wiring, and above all the two properties a
load-bearing file like driver.py has to keep while gaining one:

* a sentence WITHOUT ``cue_category`` performs too: it inherits the
  turn's carried family, or gets the seeded full-DOF talking flow -- the legacy
  byte-identical path now lives behind EMORI_CUE_MOTION=0 / EMORI_CUE_TALK=0;
* the lane can never take the head away from an add-on, from push-turn
  selection, or from a de-escalating cycle.

The planner itself is tested in tests/test_cue_mapping.py; nothing here re-tests a
motion shape, only which lane submitted it and when.
"""

from __future__ import annotations

import pytest

from emori_adapter.behavior import repertoire as R
from emori_adapter.models import ContextSignal, DerivedSignals


class _LegacyPlayer:
    """An injected player of the shape that predates ``guard=`` (test_e2e_replay's)."""

    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []

    def submit(self, intents, *, drop_if_busy: bool = False) -> bool:
        self.batches.append(list(intents))
        return True


class _GuardPlayer:
    """Today's player: records the guard closure so preemption can be asserted."""

    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.guards: list = []

    def submit(self, intents, *, drop_if_busy: bool = False, guard=None) -> bool:
        self.batches.append(list(intents))
        self.guards.append(guard)
        return True


class _Watchdog:
    """Counts the three calls the driver owes the conversation watchdog."""

    def __init__(self) -> None:
        self.ai = 0
        self.armed = 0

    def on_ai_utterance(self, now: float) -> None:
        self.ai += 1

    def on_armed(self, now: float) -> None:
        self.armed += 1


def _driver(player=None):
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    driver = LiveDriver(DryRunRobotClient(), player=player or _GuardPlayer(),
                        fiware_factory=None)
    driver.watchdog = _Watchdog()
    return driver


def _utterance(text: str = "I understand how hard that was.", *, seq: int = 1,
               **extra) -> dict:
    payload = {"type": "pokamind_ai_utterance", "text": text, "seq": seq,
               "source": "turn", "lang": "en"}
    payload.update(extra)
    return payload


def _analysis(**overrides) -> dict:
    payload = {
        "type": "pokamind_analysis_update",
        "seq": 1,
        "body_language": {"posture": "upright", "engagement_score": 52.4},
        "body_language_metrics": {"engagement": 52.4, "confidence": 43.7},
        "communication_styles": {"neutral": 12.41, "steady": 7.81},
    }
    payload.update(overrides)
    return payload


def _dump(batch) -> list[dict]:
    return [i.model_dump() for i in batch]


# --- the no-cue path is now the expressive talking flow ----------------------------

def test_a_sentence_without_a_cue_gets_the_full_dof_talking_flow():
    """Most sentences of a live reply are untagged; they must MOVE, not flick antennas.

    Measured on a robot: 11 utterances, 1 cue_planned -- Pokamind authors ONE cue
    per multi-sentence reply, so family-only routing would leave the robot on the
    fixed antenna loop for nearly the whole session.
    """

    driver = _driver(_LegacyPlayer())
    text = "Tell me more about what happened next, if you would."
    out = driver.on_utterance(_utterance(text))

    beats = max(2, min(8, len(text) // 25))
    assert out == {"ok": True, "spoke": True, "beats": beats, "n": 1}
    assert len(driver.player.batches) == 1
    plan = driver.player.batches[0]
    assert _dump(plan) != _dump(R.talking_gesture(beats=beats, reason="speaking (AI turn)"))
    assert plan[-1].reason.endswith("(settle)")
    # The talking flow IS the voice's motion: it claims the window like a family.
    assert driver.cue.speaking is True
    assert driver.counters["cue_planned"] == 1


def test_emori_cue_talk_zero_restores_the_legacy_gesture(monkeypatch):
    """The byte-for-byte fallback now lives behind EMORI_CUE_TALK=0."""

    monkeypatch.setenv("EMORI_CUE_TALK", "0")
    driver = _driver(_LegacyPlayer())      # config is read at construction
    text = "Tell me more about what happened next, if you would."
    driver.on_utterance(_utterance(text))

    beats = max(2, min(8, len(text) // 25))
    assert _dump(driver.player.batches[0]) == _dump(
        R.talking_gesture(beats=beats, reason="speaking (AI turn)"))
    assert driver.cue.speaking is False


def test_the_kill_switch_restores_the_talking_gesture_for_a_cued_sentence(monkeypatch):
    """EMORI_CUE_MOTION=0 is a true kill-switch, not a quieter cue."""

    monkeypatch.setenv("EMORI_CUE_MOTION", "0")
    driver = _driver(_LegacyPlayer())      # config is read at construction
    text = "That sounds genuinely frightening."
    driver.on_utterance(_utterance(text, cue_category="tension"))

    beats = max(2, min(8, len(text) // 25))
    assert _dump(driver.player.batches[0]) == _dump(
        R.talking_gesture(beats=beats, reason="speaking (AI turn)"))
    assert driver.cue.speaking is False


def test_a_no_cue_session_settles_exactly_once_on_tts_ended():
    """The talking flow claims the window, so end of turn owes exactly one settle."""

    driver = _driver(_LegacyPlayer())
    driver.on_utterance(_utterance())
    driver.on_tts_ended({"type": "pokamind_tts_ended"})

    assert len(driver.player.batches) == 2          # the talking flow, then the settle
    assert driver.player.batches[-1][0].reason.startswith("cue settle")
    assert driver.watchdog.armed == 1
    assert driver.counters["tts_ended"] == 1

    # The second marker for the same speaking run owes nothing further.
    driver.on_tts_ended({"type": "pokamind_tts_ended"})
    assert len(driver.player.batches) == 2


# --- the cued path ----------------------------------------------------------------

def test_a_cue_category_plans_the_family_instead_of_the_talking_gesture():
    driver = _driver()
    driver.on_utterance(_utterance("You are safe here.", cue_category="tension"))

    assert len(driver.player.batches) == 1
    plan = driver.player.batches[0]
    assert _dump(plan) != _dump(R.talking_gesture(beats=2, reason="speaking (AI turn)"))
    # Every plan ends in the settle, so a plan cut short by the next sentence recovers.
    assert plan[-1].reason.endswith("(settle)")
    assert driver.counters["cue_planned"] == 1
    assert driver.cue.speaking is True


def test_an_unknown_family_falls_back_without_raising():
    """A future Pokamind family (or junk) must degrade, never 500 the cycle."""

    driver = _driver()
    out = driver.on_utterance(_utterance("Anything at all.", cue_category="not_a_family"))

    assert out["ok"] is True and out["spoke"] is True
    plan = driver.player.batches[0]
    assert plan and plan[-1].reason.endswith("(settle)")
    assert all(i.reason.startswith("speaking (AI turn)") for i in plan)


def test_the_next_sentence_preempts_the_previous_plan():
    """The interruption mechanism: the older plan's guard closes at the next dequeue."""

    driver = _driver()
    driver.on_utterance(_utterance("First sentence, tense.", seq=1, cue_category="tension"))
    first_guard = driver.player.guards[0]
    assert first_guard is not None and first_guard() is True

    driver.on_utterance(_utterance("Second sentence, lighter.", seq=2, cue_category="levity"))
    assert first_guard() is False                 # sentence n is abandoned mid-flight
    assert driver.player.guards[1]() is True      # sentence n+1 owns the head


def test_an_untagged_sentence_preempts_the_tail_and_carries_the_turns_family():
    """The MIXED turn: Pokamind authors ONE cue per reply, so untagged sentences
    mid-turn are the normal case. The previous sentence's plan still dies at the
    boundary (no tension tail playing underneath its replacement) -- but the turn's
    family now CARRIES onto the untagged sentence at reduced amplitude, so the
    reply keeps its emotional colour instead of collapsing to a neutral loop.
    """

    driver = _driver()
    driver.on_utterance(_utterance("First, and tense.", seq=1, cue_category="tension"))
    first_guard = driver.player.guards[0]
    assert first_guard() is True and driver.cue.speaking is True

    driver.on_utterance(_utterance("Second, with no family at all.", seq=2))

    assert first_guard() is False                  # the tense plan is abandoned
    assert driver.cue.speaking is True             # its successor owns the head now
    carried = driver.player.batches[-1]
    assert any(i.reason.startswith("cue tension") for i in carried)


def test_the_carried_family_dies_with_the_turn():
    """tts_ended ends the reply, so the next turn's untagged opener starts neutral."""

    driver = _driver()
    driver.on_utterance(_utterance("First, and tense.", seq=1, cue_category="tension"))
    driver.on_tts_ended({"type": "pokamind_tts_ended"})

    driver.on_utterance(_utterance("A fresh turn with no family.", seq=2))
    plan = driver.player.batches[-1]
    assert not any(i.reason.startswith("cue tension") for i in plan)
    assert all(i.reason.startswith("speaking (AI turn)") for i in plan)


def test_an_unknown_family_claims_the_window_and_settles_once():
    """A seventh Pokamind family routes to the talking flow, which owns the head
    while it runs and owes exactly one settle at end of turn."""

    driver = _driver()
    driver.on_utterance(_utterance("A brand new family.", cue_category="wonder"))
    before = len(driver.player.batches)

    assert driver.cue.speaking is True
    driver.on_tts_ended({"type": "pokamind_tts_ended"})
    assert len(driver.player.batches) == before + 1
    assert driver.player.batches[-1][0].reason.startswith("cue settle")


def test_dedup_still_fires_before_any_planning():
    """The deduper is the first thing on this path and must stay there."""

    driver = _driver()
    driver.on_utterance(_utterance("Same line.", seq=7, cue_category="levity"))
    out = driver.on_utterance(_utterance("Same line.", seq=7, cue_category="levity"))

    assert out == {"ok": True, "spoke": False, "deduped": True}
    assert len(driver.player.batches) == 1
    assert driver.counters["utterances_deduped"] == 1
    assert driver.counters["cue_planned"] == 1


def test_the_watchdog_still_sees_every_ai_sentence():
    """An AI reply disarms the turn watchdog; a cue must not have displaced that."""

    driver = _driver()
    driver.on_utterance(_utterance("One.", seq=1, cue_category="energy"))
    driver.on_utterance(_utterance("Two.", seq=2))
    assert driver.watchdog.ai == 2


def test_tts_ended_settles_exactly_once():
    driver = _driver()
    driver.on_utterance(_utterance("A tense line.", cue_category="tension"))
    before = len(driver.player.batches)

    driver.on_tts_ended({"type": "pokamind_tts_ended"})
    settle = driver.player.batches[before:]
    assert len(settle) == 1 and len(settle[0]) == 1
    assert settle[0][0].kind == "goto"
    assert driver.cue.speaking is False
    assert driver.watchdog.armed == 1

    # The second marker for the same speaking run owes nothing.
    driver.on_tts_ended({"type": "pokamind_tts_ended"})
    assert len(driver.player.batches) == before + 1


def test_head_reserved_plans_nothing_at_all():
    """Push-turn selection reads the head's own pose; a self-move wipes its baseline."""

    driver = _driver()
    driver.set_head_reserved(lambda: True)
    driver.on_utterance(_utterance("Something expressive.", cue_category="levity"))

    assert driver.player.batches == []
    assert driver.counters["cue_suppressed"] == 1
    assert driver.cue.speaking is False


def test_a_broken_head_predicate_does_not_stop_the_session():
    driver = _driver()

    def boom() -> bool:
        raise RuntimeError("sensor gone")

    driver.set_head_reserved(boom)
    assert driver.head_reserved() is False
    assert driver.on_utterance(_utterance("Still speaking.", cue_category="energy"))["ok"] is True


# --- arbitration with the per-cycle engagement lane -------------------------------

def test_engagement_yields_its_motion_while_a_sentence_is_spoken():
    driver = _driver()
    driver.on_utterance(_utterance("A confiding line.", cue_category="intimacy"))
    before = len(driver.player.batches)

    assert driver.on_analysis(driver.stamp(_analysis()))["ok"] is True
    assert len(driver.player.batches) == before          # the head belongs to the voice
    assert driver.counters["engagement_yielded_to_speech"] == 1
    # Everything else the cycle produces still ran.
    assert driver.counters["cycles"] == 1
    assert driver._cue_affect.fresh is True


def test_engagement_submits_normally_when_no_sentence_is_running():
    driver = _driver()
    assert driver.on_analysis(driver.stamp(_analysis()))["ok"] is True
    assert len(driver.player.batches) == 1
    assert driver.counters["engagement_yielded_to_speech"] == 0


def test_an_escalating_cycle_preempts_a_live_cue():
    """Safety outranks expression: the calm settle goes through mid-sentence."""

    driver = _driver()
    driver.on_utterance(_utterance("An excited line.", cue_category="energy"))
    before = len(driver.player.batches)
    assert driver.cue.speaking is True

    sig = DerivedSignals(timestamp="2026-08-11T00:00:00Z", session_id="s1",
                         person_id="p1", context=ContextSignal(escalation=0.9))
    calm = R.calm_settle()
    driver._submit_engagement(calm, sig)

    assert len(driver.player.batches) == before + 1
    assert _dump(driver.player.batches[-1]) == _dump(R.superseding(calm))
    assert driver.counters["cue_preempted_by_escalation"] == 1
    assert driver.cue.speaking is False
    assert driver.counters["engagement_yielded_to_speech"] == 0


def test_the_de_escalating_motion_is_never_dropped_behind_a_busy_queue():
    """The preempt cancels every queued cue batch; its replacement must not be dropped.

    Dropped, it would leave the head parked wherever the aborted plan last put it (for
    tension: sunk toward the shell, antennas drooped) with nothing following it -- while
    the counter claimed the de-escalation happened.
    """

    class _BusyPlayer(_GuardPlayer):
        busy = True

        def submit(self, intents, *, drop_if_busy: bool = False, guard=None) -> bool:
            if drop_if_busy:                       # a full queue refuses these
                return False
            return super().submit(intents, drop_if_busy=drop_if_busy, guard=guard)

    driver = _driver(_BusyPlayer())
    driver.on_utterance(_utterance("An excited line.", cue_category="energy"))
    before = len(driver.player.batches)

    sig = DerivedSignals(timestamp="2026-08-11T00:00:00Z", session_id="s1",
                         person_id="p1", context=ContextSignal(escalation=0.9))
    calm = R.calm_settle()
    driver._submit_engagement(calm, sig)

    assert len(driver.player.batches) == before + 1
    assert _dump(driver.player.batches[-1]) == _dump(R.superseding(calm))


# --- EMORI_ROS_MOTION_OWNER -------------------------------------------------------

@pytest.mark.parametrize("owner", ["ros", "none"])
def test_the_expressive_lanes_stand_down_when_the_hostapp_does_not_own_motion(
        monkeypatch, owner):
    """`ros` means movement originates in the ROS graph; `none` means nothing moves.

    Both are lies while this process keeps submitting the very lanes the ROS graph
    recomputes -- that is the double-drive trap the knob exists to prevent.
    """

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", owner)
    driver = _driver()                              # the owner is read at construction
    assert driver.expressive_motion_enabled is False

    driver.on_utterance(_utterance("A tense line.", cue_category="tension"))
    driver.on_utterance(_utterance("A line with no family.", seq=2))
    sig = DerivedSignals(timestamp="2026-08-11T00:00:00Z", session_id="s1",
                         person_id="p1", context=ContextSignal(escalation=0.1))
    driver._submit_engagement(R.calm_settle(), sig)

    assert driver.player.batches == []
    assert driver.cue.speaking is False
    assert driver.counters["engagement_suppressed_by_owner"] == 1


def test_the_shipped_default_still_owns_its_own_motion():
    driver = _driver()
    assert driver.motion_owner == "hostapp"
    assert driver.expressive_motion_enabled is True
    driver.on_utterance(_utterance("A tense line.", cue_category="tension"))
    assert len(driver.player.batches) == 1


def test_a_calm_cycle_below_the_threshold_still_yields():
    driver = _driver()
    driver.on_utterance(_utterance("An excited line.", cue_category="energy"))
    before = len(driver.player.batches)

    sig = DerivedSignals(timestamp="2026-08-11T00:00:00Z", session_id="s1",
                         person_id="p1", context=ContextSignal(escalation=0.1))
    driver._submit_engagement(R.calm_settle(), sig)

    assert len(driver.player.batches) == before
    assert driver.counters["engagement_yielded_to_speech"] == 1


# --- submit_legacy_motion's new optional guard ------------------------------------

def test_guard_passthrough_is_backward_compatible_with_a_legacy_player():
    """The injected players that predate ``guard=`` must keep working unchanged."""

    driver = _driver(_LegacyPlayer())
    intents = R.talking_gesture(beats=2)

    assert driver.submit_legacy_motion(intents, guard=lambda: True) is True
    assert len(driver.player.batches) == 1
    # A closed caller guard is honoured in the compatibility seam too, since that
    # player executes synchronously and would otherwise never re-check it.
    assert driver.submit_legacy_motion(intents, guard=lambda: False) is False
    assert len(driver.player.batches) == 1


def test_the_two_guards_are_anded_never_ored():
    """Neither guard may authorise motion the other one has revoked."""

    driver = _driver()
    intents = R.talking_gesture(beats=2)

    assert driver.submit_legacy_motion(intents, guard=lambda: True) is True
    forwarded = driver.player.guards[-1]
    assert forwarded is not None and forwarded() is True

    # Ownership moved to an add-on after the batch was queued.
    driver.set_legacy_motion_enabled(False)
    assert forwarded() is False
    assert driver.submit_legacy_motion(intents, guard=lambda: True) is False


def test_an_unreadable_caller_guard_stays_closed():
    driver = _driver()

    def boom() -> bool:
        raise RuntimeError("scheduler gone")

    assert driver.submit_legacy_motion(R.talking_gesture(beats=2), guard=boom) is True
    assert driver.player.guards[-1]() is False


def test_submitting_without_a_guard_is_unchanged():
    driver = _driver()
    assert driver.submit_legacy_motion(R.talking_gesture(beats=2)) is True
    assert driver.player.guards[-1]() is True


@pytest.mark.parametrize("family", ["tension", "levity", "energy", "intimacy",
                                    "softening", "release"])
def test_every_wire_family_reaches_the_robot_through_one_queue(family):
    """All six survive the driver, and none of them bypasses submit_legacy_motion."""

    driver = _driver()
    driver.on_utterance(_utterance("A sentence in this family.", cue_category=family))
    assert len(driver.player.batches) == 1
    assert driver.counters["cue_planned"] == 1


# --- Pollen recorded-move playback through RobotPlayer ----------------------


def test_the_player_stops_a_recorded_move_the_moment_its_guard_dies():
    """A daemon-side recording outlives per-intent sleeps; preemption must reach
    into the daemon and stop it, or the tail would animate the next sentence."""

    import time as _time

    from emori_adapter.hostapp.driver import RobotPlayer
    from emori_adapter.models import MotionIntent
    from emori_adapter.robot.client import DryRunRobotClient

    client = DryRunRobotClient()
    player = RobotPlayer(client)
    try:
        alive = {"v": True}
        player.submit([MotionIntent(kind="play_recorded", move="relief1",
                                    duration=5.0, reason="test")],
                      guard=lambda: alive["v"])
        deadline = _time.monotonic() + 3.0
        while not client.recorded_played and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert client.recorded_played == ["relief1"]

        alive["v"] = False                       # the next sentence arrives
        deadline = _time.monotonic() + 3.0
        while not client.recorded_stopped and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert client.recorded_stopped, "preemption never stopped the daemon move"
    finally:
        player.stop()
        player.join()


def test_the_greeting_plays_an_official_recording_when_armed(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_RECORDED", "1")
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    try:
        played = [i for b in driver.player.batches for i in b
                  if i.kind == "play_recorded"]
        assert played and played[0].move == "welcoming1"
    finally:
        driver.player.stop() if hasattr(driver.player, "stop") else None
