"""The watchdog policy: it may not end a session on a learner who is there.

Three session shapes that an earlier, tighter policy ended on a learner who was there:
  S1  first turn, first sound ~19 s after the turn opened -> the 15 s silent ceiling;
  S2  first turn, learner speaking but every attempt guard-rejected -> 40 s abs ceiling;
  S3  seventh turn, two rejected attempts -> 40 s abs ceiling.
The replays below use those timelines, in seconds from `pokamind_turn_armed`.
"""

from __future__ import annotations

from emori_adapter.hostapp.watchdog import ConversationWatchdog, TurnState

POLICY = dict(open_ceiling_s=35.0, reprompt_after_s=12.0, abs_ceiling_s=90.0,
              reject_extensions=3, first_ceiling_s=50.0, first_reprompt_s=20.0,
              max_reprompts=2, max_reprompts_per_turn=3, reprompt_every_s=15.0,
              present_ceiling_s=180.0, self_noise_s=3.0)


def _wd(**kw) -> ConversationWatchdog:
    return ConversationWatchdog(**{**POLICY, **kw})


def _open(wd: ConversationWatchdog, t: float = 0.0) -> ConversationWatchdog:
    wd.on_armed(now=t)
    wd.on_user_speaking(now=t)
    return wd


def _later_turn(wd: ConversationWatchdog) -> ConversationWatchdog:
    """One accepted turn, then the AI replies, then the NEXT turn opens at t=0."""
    _open(wd, -30.0)
    wd.on_submitted(now=-25.0)
    wd.on_ai_utterance(now=-20.0)
    return _open(wd, 0.0)


def _run(wd, until: float, *, present=False, step=0.1, events=()):
    """Tick like the 1 Hz ticker (finer), applying (t, kind) events; return actions."""
    pending = sorted(events)
    out = []
    t = 0.0
    while t <= until + 1e-9:
        while pending and pending[0][0] <= t + 1e-9:
            et, kind = pending.pop(0)
            getattr(wd, kind)(now=et)
        seen = present(t) if callable(present) else present
        action = wd.due(now=t, present=seen)
        if action:
            out.append((round(t, 1), action, wd.last_end_kind if action == "force_end" else ""))
        t = round(t + step, 6)
    return out


# --- the three sessions that were ended on a learner who was there -----------------

def test_s1_first_turn_is_not_ended_while_the_learner_is_still_thinking():
    wd = _open(_wd())
    events = [(18.9, "on_activity"), (21.66, "on_turn_rejected"), (29.22, "on_submitted")]
    actions = _run(wd, 29.3, events=events)
    # Old policy: force_end at 15.3 s. New: nothing at all -- the answer is submitted.
    assert actions == []
    assert wd.state is TurnState.SUBMITTED


def test_s2_trying_learner_is_not_ended_at_40s():
    wd = _open(_wd())
    events = [(19.2, "on_activity"), (21.87, "on_turn_rejected"), (37.3, "on_activity"),
              (40.03, "on_turn_rejected"), (47.9, "on_activity"),
              (50.9, "on_turn_rejected"), (55.86, "on_turn_rejected")]
    actions = _run(wd, 95.0, events=events)
    assert all(t > 40.91 for t, _a, _k in actions)            # old end was at 40.9 s
    ends = [(t, k) for t, a, k in actions if a == "force_end"]
    assert ends == [(90.0, "abs_ceiling")]                     # unseen: bounded at 90 s


def test_s2_trying_learner_who_is_seen_gets_the_present_cap():
    wd = _open(_wd())
    events = [(19.2, "on_activity"), (21.87, "on_turn_rejected"),
              (40.03, "on_turn_rejected"), (50.9, "on_turn_rejected")]
    actions = _run(wd, 185.0, present=True, events=events)
    ends = [(t, k) for t, a, k in actions if a == "force_end"]
    assert ends == [(180.0, "present_cap")]


def test_s3_turn7_is_not_ended_at_40s_and_is_reprompted_first():
    wd = _later_turn(_wd())
    events = [(8.36, "on_turn_rejected"), (22.8, "on_activity"),
              (24.37, "on_turn_rejected"), (41.72, "on_turn_rejected")]
    actions = _run(wd, 80.0, events=events)
    # Old policy: force_end at 40.9 s. New: two perceivable re-prompts instead (12 s of
    # quiet after each rejected attempt), and no end while the attempts keep coming.
    assert [(t, a) for t, a, _k in actions if t <= 40.92] == [(20.4, "reprompt"),
                                                              (36.4, "reprompt")]
    ends = [(t, k) for t, a, k in actions if a == "force_end"]
    assert ends == [(76.8, "silent")]            # 41.72 + 35 s of quiet, nobody seen
    assert sum(1 for _t, a, _k in actions if a == "reprompt") == 3   # per-turn cap


# --- (d) the empty room is still bounded ------------------------------------------

def test_silent_empty_room_first_turn_two_reprompts_then_end_at_50s():
    actions = _run(_open(_wd()), 60.0)
    assert actions == [(20.0, "reprompt", ""), (35.0, "reprompt", ""),
                       (50.0, "force_end", "silent")]


def test_silent_empty_room_later_turn_ends_at_35s():
    actions = _run(_later_turn(_wd()), 40.0)
    assert actions == [(12.0, "reprompt", ""), (27.0, "reprompt", ""),
                       (35.0, "force_end", "silent")]


def test_noisy_empty_room_ends_at_the_unseen_ceiling():
    events = [(float(t), "on_activity") for t in range(4, 200, 4)]
    actions = _run(_later_turn(_wd()), 100.0, events=events)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(90.0, "abs_ceiling")]


def test_a_seen_but_silent_person_is_still_bounded_by_the_present_cap():
    actions = _run(_later_turn(_wd()), 185.0, present=True)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(180.0, "present_cap")]
    assert sum(1 for _t, a, _k in actions if a == "reprompt") == 2   # 12 s, 27 s


def test_leaving_after_a_long_quiet_ends_once_nobody_is_seen():
    actions = _run(_later_turn(_wd()), 70.0, present=lambda t: t < 60.0)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(60.0, "silent")]


# --- somebody seen who has stopped answering ---------------------------------------

SILENCE = dict(present_silence_s=45.0, present_quiet_s=10.0)


def test_a_seen_person_who_stays_silent_is_reprompted_then_ended():
    actions = _run(_later_turn(_wd(**SILENCE)), 60.0, present=True)
    assert actions == [(12.0, "reprompt", ""), (27.0, "reprompt", ""),
                       (45.0, "force_end", "present_silent")]


def test_the_first_turn_keeps_its_longer_window_when_seen():
    actions = _run(_open(_wd(**SILENCE)), 60.0, present=True)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(50.0, "present_silent")]


def test_stray_sound_only_delays_the_silence_end():
    events = [(19.0, "on_turn_rejected"), (37.0, "on_turn_rejected"), (41.0, "on_turn_rejected")]
    actions = _run(_later_turn(_wd(**SILENCE)), 70.0, present=True, events=events)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(51.0, "present_silent")]


def test_s3_turn7_with_attempts_still_coming_is_not_ended_before_the_last_one():
    events = [(8.36, "on_turn_rejected"), (22.8, "on_activity"),
              (24.37, "on_turn_rejected"), (41.72, "on_turn_rejected")]
    actions = _run(_later_turn(_wd(**SILENCE)), 70.0, present=True, events=events)
    ends = [t for t, a, _k in actions if a == "force_end"]
    assert len(ends) == 1 and ends[0] > 41.72


def test_a_seen_person_still_talking_is_left_to_the_cap():
    events = [(float(t), "on_activity") for t in range(4, 200, 4)]
    actions = _run(_later_turn(_wd(**SILENCE)), 185.0, present=True, events=events)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(180.0, "present_cap")]


def test_zero_turns_the_silence_end_off():
    actions = _run(_later_turn(_wd(present_silence_s=0.0)), 185.0, present=True)
    assert [(t, k) for t, a, k in actions if a == "force_end"] == [(180.0, "present_cap")]


# --- (c) the re-prompt, and the robot's own noise ---------------------------------

def test_the_robots_own_reprompt_noise_does_not_keep_an_empty_room_alive():
    # motor/speech noise right after each re-prompt, inside the self-noise blank
    events = [(13.0, "on_activity"), (14.0, "on_turn_rejected"),
              (28.0, "on_activity"), (29.5, "on_turn_rejected")]
    actions = _run(_later_turn(_wd()), 40.0, events=events)
    assert actions == [(12.0, "reprompt", ""), (27.0, "reprompt", ""),
                       (35.0, "force_end", "silent")]


def test_reprompts_are_capped_per_turn():
    events = [(t, "on_activity") for t in (13.0 + 3.1, 16.2 + 12 + 3.1, 44.5 + 12 + 3.1)]
    actions = _run(_later_turn(_wd()), 85.0, events=events)
    assert sum(1 for _t, a, _k in actions if a == "reprompt") == 3


# --- (b) first turn ---------------------------------------------------------------

def test_first_turn_grace_is_per_session_not_per_process():
    wd = _later_turn(_wd())
    wd.on_session_start(now=100.0)
    _open(wd, 100.0)
    assert wd.due(now=112.0) is None          # a later turn would have re-prompted here
    assert wd.due(now=120.0) == "reprompt"
    assert wd.snapshot(now=120.0)["first_turn"] is True


# --- the class defaults are still the legacy behaviour ----------------------------

def test_legacy_defaults_are_unchanged():
    wd = _open(ConversationWatchdog())
    assert wd.due(now=10.0, present=True) == "reprompt"    # presence extends nothing
    assert wd.due(now=15.0, present=True) == "force_end"
    assert wd.last_end_kind == "silent"


def test_a_turn_that_never_opens_is_ended_by_the_armed_ceiling():
    """A hung or failed endpointer load never posts the turn's open: with a learner in
    view nothing else would ever end it (the walk-away is held off by their cycles)."""

    from emori_adapter.hostapp.watchdog import ConversationWatchdog

    wd = ConversationWatchdog(armed_ceiling_s=90.0)
    wd.on_armed(now=100.0)
    assert wd.due(now=189.0, present=True) is None
    assert wd.due(now=190.0, present=True) == "force_end"
    assert wd.last_end_kind == "armed_ceiling"

    # Once the turn opens, the armed clock is over: only the open-turn rules apply.
    opened = ConversationWatchdog(armed_ceiling_s=90.0, open_ceiling_s=1000.0,
                                  abs_ceiling_s=1000.0)
    opened.on_armed(now=100.0)
    opened.on_user_speaking(now=150.0)
    assert opened.due(now=195.0, present=True) != "force_end"   # a re-prompt, not an end

    legacy = ConversationWatchdog()
    legacy.on_armed(now=100.0)
    assert legacy.due(now=10_000.0) is None     # default: ARMED stays inert
