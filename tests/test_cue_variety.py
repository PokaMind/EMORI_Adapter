"""Variation is a property of the CONVERSATION, not of one sentence's hash.

The live session that prompted this file performed, across sixteen
sentences: one talking shape eight times, one family's sink-and-hold five times,
and three of the library's sixty recordings. Every individual plan was correct.
What was wrong was the sequence -- and nothing in the planner had ever been able
to see a sequence, because it is pure and was given only the current sentence.

These tests are written against the sequence: the same scheduler, many sentences,
and assertions about how much the robot repeats itself.
"""

from __future__ import annotations

import pytest

from emori_adapter.behavior import repertoire as R
from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CueScheduler
from emori_adapter.behavior.cues import (CUE_FAMILIES, CueConfig, VarietyHint,
                                         _shape_candidates, pick_varied, plan_cue)


# --- the picker -----------------------------------------------------------

def test_pick_varied_avoids_what_was_just_performed():
    assert pick_varied(["a", "b", "c"], ("a",), 0) == "b"
    assert pick_varied(["a", "b", "c"], ("a", "b"), 0) == "c"


def test_pick_varied_rotates_by_seed_when_nothing_is_recent():
    picks = {pick_varied(["a", "b", "c"], (), seed) for seed in range(3)}
    assert picks == {"a", "b", "c"}


def test_pick_varied_falls_back_to_the_least_recent_option():
    # Everything has been performed: the one used LONGEST ago wins, so a saturated
    # memory degrades to round-robin instead of collapsing onto one favourite.
    assert pick_varied(["a", "b"], ("a", "b"), 0) == "b"
    assert pick_varied(["a", "b"], ("b", "a"), 0) == "a"


def test_pick_varied_is_total():
    assert pick_varied([], ("a",), 3) is None


# --- the talking grammar --------------------------------------------------

def test_every_named_shape_produces_motion():
    for name in R.TALK_SHAPES:
        intents = R.talking_shape(name, beats=6, amp=1.0)
        assert intents, name
        assert all(i.kind == "goto" for i in intents), name


def test_an_unknown_shape_never_silences_a_sentence():
    assert R.talking_shape("no-such-shape", beats=5, amp=1.0)


def test_the_shapes_are_actually_different_gestures():
    # The complaint was three gestures on a loop, so the test that matters is that
    # the shapes differ in their POSE SEQUENCE, not merely in amplitude.
    seen = {}
    for name in R.TALK_SHAPES:
        intents = R.talking_shape(name, beats=6, amp=1.0, z_limit_mm=18.0)
        seen[name] = tuple((round(i.head.pitch, 1), round(i.head.yaw, 1),
                            round(i.head.roll, 1), round(i.antennas[0], 2))
                           for i in intents if i.head and i.antennas)
    assert len(set(seen.values())) == len(R.TALK_SHAPES)


def test_a_question_rises_and_a_statement_does_not():
    rise = R.talking_shape("rise", beats=5, amp=1.0)
    pitches = [i.head.pitch for i in rise if i.head]
    assert pitches[-1] < pitches[0]          # the head ends HIGHER than it started
    assert pitches[-1] < 0                   # and above the neutral gaze


def test_the_flow_no_longer_only_dips():
    # Rows 0 and 1 are all-positive pitch (the head bows on every beat); row 2 was
    # added because a run of flow sentences trended one way all session.
    lift = R.talking_flow(beats=8, variant=2)
    assert any(i.head.pitch < 0 for i in lift)


# --- shape selection reads the sentence -----------------------------------

@pytest.mark.parametrize("text,expected", [
    ("What do you need from me to get this moving?", "rise"),
    ("That is absolutely unacceptable!", "punch"),
    ("I need the time, the room, and the name of whoever booked it, please.", "list_three"),
    ("No. Not today.", "sway"),
])
def test_the_sentence_form_proposes_the_shape(text, expected):
    assert _shape_candidates(text)[0] == expected


def test_a_tint_narrows_the_shortlist_to_shapes_the_emotion_can_wear():
    # A carried intimacy never punches, whatever the punctuation says.
    assert "punch" not in _shape_candidates("Stop that right now!", tint="intimacy")


# --- the sequence ---------------------------------------------------------

# Deliberately clear of BOTH the text-affect lexicon and the delivery read (no
# question marks, exclamations or ellipses): these tests are about the GRAMMAR, and
# a sentence that names a feeling -- in its words or in how it is built -- is planned
# as that family instead (covered by test_cue_text_affect.py).
_SENTENCES = [
    "I cannot just sit around while the morning goes by like this.",
    "My employer expects me back at the desk by noon.",
    "The desk said it would be twenty minutes, three times now.",
    "The booking was made two weeks ago by the office itself.",
    "I have already given up the whole of today, and the pay that came with it.",
    "The office has my number and nobody has called it.",
    "I need a real estimate so I can tell them whether I am returning.",
    "Five hours is out of the question.",
]


def _run(sentences, *, cue_category=None, config=None):
    """Feed a scheduler a turn's worth of sentences; return the plan slugs."""

    sent: list = []
    clock = {"t": 0.0}
    sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                       clock=lambda: clock["t"],
                       config=config or CueConfig(recorded=True, talk=True, carry=True))
    out = []
    for i, text in enumerate(sentences):
        clock["t"] += 3.0
        out.append(sch.on_sentence(text=text, cue_category=(cue_category if i == 0 else None),
                                   cue_body=None, lang="en",
                                   affect=AffectSnapshot(fresh=True)))
    return out


def test_a_turn_of_untagged_sentences_never_repeats_a_shape_back_to_back():
    results = _run(_SENTENCES)
    shapes = [r.reason for r in results]
    assert all(a != b for a, b in zip(shapes, shapes[1:])), shapes


def test_a_turn_of_untagged_sentences_uses_most_of_the_grammar():
    results = _run(_SENTENCES)
    kinds = {r.reason.split("/")[1] for r in results if r.reason.startswith("talking/")}
    # Eight sentences, and the live session performed ONE shape for all of them.
    assert len(kinds) >= 5, kinds


def test_a_carried_family_stops_replaying_its_own_block():
    results = _run(_SENTENCES[:5], cue_category="tension")
    assert results[0].family == "tension"
    # It stays tension for the whole turn (the emotion must not evaporate) ...
    assert [r.family for r in results] == ["tension"] * 5
    # ... but past the decay point it is performed as a TINT on the talking
    # grammar, which is what stops four identical sinks in a row.
    tinted = [r for r in results if "tint:tension" in r.reason]
    assert len(tinted) >= 2, [r.reason for r in results]
    assert all(r.source == "carried" for r in tinted)


def test_consecutive_recordings_are_different_performances():
    cfg = CueConfig(recorded=True, talk=True, carry=True)
    sent: list = []
    clock = {"t": 0.0}
    sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                       clock=lambda: clock["t"], config=cfg)
    played = []
    for i in range(6):
        clock["t"] += 10.0            # well past the recording gap every time
        res = sch.on_sentence(text="I am so frightened by all of this, truly.",
                              cue_category="tension", cue_body=None, lang="en",
                              affect=AffectSnapshot(fresh=True))
        if res.recorded:
            played.append(res.recorded)
    # The frightened line's tone owns exactly two recordings and only one of them
    # fits a four-second sentence, so the honest outcome is: play it, then perform
    # the AUTHORED fear shapes rather than play it again (a wrong-emotion recording
    # from the family table would be worse than none).
    assert played, "the first frightened sentence must still perform its recording"
    assert all(a != b for a, b in zip(played, played[1:])), played
    assert len(played) < 6, played


def test_the_family_variants_rotate_across_a_conversation():
    cfg = CueConfig(recorded=False, talk=True, carry=False)
    variants = set()
    for fam in ("tension", "softening"):
        sent: list = []
        clock = {"t": 0.0}
        sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                           clock=lambda: clock["t"], config=cfg)
        for i in range(5):
            clock["t"] += 3.0
            res = sch.on_sentence(text=_SENTENCES[i], cue_category=fam, cue_body=None,
                                  lang="en", affect=AffectSnapshot(fresh=True))
            variants.add(res.reason.split("/")[2])
    assert len(variants) >= 4, variants


# --- the invariants the variety layer must not break ----------------------

def test_every_family_read_still_fits_its_sentence_and_settles():
    cfg = CueConfig(recorded=False, body_yaw=True, z_mm=18.0, amp=1.3)
    text = "This sentence is a fairly ordinary length for a spoken reply."
    for family in CUE_FAMILIES:
        for v in range(5):
            hint = VarietyHint(shapes=tuple("%s:v%d" % (family, i) for i in range(v)))
            plan = plan_cue(family, text=text, config=cfg, seed=v, variety=hint,
                            allow_recorded=False)
            assert plan.intents
            assert sum(i.duration for i in plan.intents) <= plan.budget_s + 1e-6
            assert "settle" in plan.intents[-1].reason
            assert all(i.speech for i in plan.intents)


def test_every_talking_shape_fits_its_sentence_and_settles():
    cfg = CueConfig(recorded=False, body_yaw=True, z_mm=18.0, amp=1.3)
    for i, text in enumerate(_SENTENCES):
        hint = VarietyHint(shapes=tuple("talk:" + s for s in R.TALK_SHAPES[:i % 5]))
        plan = plan_cue(None, text=text, config=cfg, seed=i, variety=hint)
        assert sum(x.duration for x in plan.intents) <= plan.budget_s + 1e-6
        assert "settle" in plan.intents[-1].reason
        assert plan.shape.startswith("talk:"), (text, plan.shape)


def test_de_escalation_never_strikes_a_beat_at_an_agitated_learner():
    cfg = CueConfig(recorded=False, deescalate_at=0.6)
    for text in ("That is unacceptable!", "First, second, and third, listen to me,"):
        plan = plan_cue(None, text=text, escalation=0.9, config=cfg, seed=1)
        assert plan.shape not in ("talk:punch", "talk:list_three", "talk:lean_press")
        assert "deescalated" in plan.reason


def test_the_planner_without_a_hint_is_still_deterministic():
    a = plan_cue("levity", text="Well, that is one way to put it.", seed=7,
                 config=CueConfig(recorded=False))
    b = plan_cue("levity", text="Well, that is one way to put it.", seed=7,
                 config=CueConfig(recorded=False))
    assert a.reason == b.reason
    assert [i.model_dump() for i in a.intents] == [i.model_dump() for i in b.intents]


# --- a sentence that is only a cue ----------------------------------------

def test_a_cue_only_sentence_holds_a_gesture_for_its_voice_and_hands_its_emotion_on():
    """Pokamind's second cue can land on its own sentence boundary.

    It arrives with a cue and no text. Its gesture waits behind the voice hold, so it runs
    only if a sound comes, and the family goes on to the sentence it was written for.
    """

    sent: list = []
    clock = {"t": 0.0}
    sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                       clock=lambda: clock["t"],
                       config=CueConfig(recorded=False, talk=True, carry=True))
    clock["t"] += 3.0
    spoken = sch.on_sentence(text="I have been waiting since eight this morning.",
                             cue_category=None, cue_body=None, lang="en",
                             affect=AffectSnapshot(fresh=True))
    assert spoken.planned
    submitted = len(sent)

    clock["t"] += 1.0
    only = sch.on_sentence(text="   ", cue_category="levity", cue_body=None, lang="en",
                           affect=AffectSnapshot(fresh=True))
    assert only.planned
    assert only.family == "levity" and only.reason == "cue_only/levity"
    assert len(sent) == submitted + 1
    assert all(intent.speech for intent in sent[-1]), "it must wait for a voice to perform on"

    clock["t"] += 1.0
    nxt = sch.on_sentence(text="Though I suppose the coffee here is not bad.",
                          cue_category=None, cue_body=None, lang="en",
                          affect=AffectSnapshot(fresh=True))
    assert nxt.family == "levity", "the cue must reach the sentence it was written for"
    assert nxt.source == "carried"


def test_an_empty_sentence_with_no_cue_stays_still():
    sent: list = []
    sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                       config=CueConfig(recorded=False))
    res = sch.on_sentence(text="", cue_category=None, cue_body=None, lang="en",
                          affect=AffectSnapshot(fresh=True))
    assert not res.planned and res.reason == "empty_text"
    assert sent == []


# --- never a gesture with no feeling behind it ----------------------------

@pytest.mark.parametrize("text,expect", [
    ("Is it a big problem for me to stay here?", "asking"),
    ("That is completely out of the question!", "pressing"),
    ("I suppose... if that is really the only way.", "trailing"),
])
def test_a_sentence_that_names_no_feeling_still_performs_how_it_is_said(text, expect):
    """A roleplay line is rarely emotionally blank.

    In live sessions, 10-25% of a session's sentences reached the plain talking
    grammar because no word in them matched the lexicon -- "Is it a big problem for
    me to stay here?" carries worry, and the robot performed a shape with nothing
    behind it. When the words name nothing, the sentence's own construction is read
    instead.
    """

    plan = plan_cue(None, text=text, config=CueConfig(recorded=True), seed=5)
    assert plan.source == "delivery", plan.reason
    assert "text:" + expect in plan.reason
    assert plan.family in CUE_FAMILIES


def test_the_delivery_read_never_outranks_real_evidence():
    cfg = CueConfig(recorded=False)
    # A cue on the wire wins.
    assert plan_cue("tension", text="Are you serious about that?", config=cfg).source == "pokamind"
    # The sentence's own WORDS win (they name a feeling; a question mark does not).
    assert plan_cue(None, text="Am I really so frightened of this?", config=cfg).source == "text"
    # And the turn's carried emotion wins: it is evidence about this reply. (The
    # scheduler passes the carried family AS the category, which is what makes the
    # delivery read -- which only ever fills a blank -- stand down.)
    carried = plan_cue("tension", text="So what is the next step from here?",
                       carried=True, config=cfg)
    assert carried.source == "carried"


def test_a_fragment_is_not_over_read():
    # Three words is the floor: "Right now?" is not a performance.
    plan = plan_cue(None, text="Right now?", config=CueConfig(recorded=False))
    assert plan.source != "delivery"


def test_the_force_of_a_plain_sentence_reaches_the_body():
    """An insistent line and a hedged one must not be performed identically."""

    cfg = CueConfig(recorded=False, amp=1.0)
    insist = plan_cue(None, text="I cannot afford to lose more pay over this.",
                      config=cfg, seed=4)
    hedge = plan_cue(None, text="I suppose we could maybe wait a little longer.",
                     config=cfg, seed=4)
    # Amplitude is the comparable quantity: peak rotation is not, because the shapes
    # lead with different axes (lean_press with z, gaze_arc with yaw).
    assert insist.k > hedge.k
    assert hedge.shape in ("talk:gaze_arc", "talk:sway", "talk:head_cock"), hedge.shape


def test_a_session_reaches_new_recordings_instead_of_a_favourite():
    """Across six live sessions only 14 of ~50 usable recordings were ever reached,
    and fear1 appeared in five of them. Selection now prefers what this session has
    not performed yet."""

    cfg = CueConfig(recorded=True, talk=True, carry=False)
    sent: list = []
    clock = {"t": 0.0}
    sch = CueScheduler(lambda intents, guard: (sent.append(intents), True)[1],
                       clock=lambda: clock["t"], config=cfg)
    played = []
    text = ("I am absolutely furious about this whole situation and the way it "
            "has been handled from the start.")
    for i in range(8):
        clock["t"] += 10.0
        res = sch.on_sentence(text=text, cue_category="tension", cue_body=None,
                              lang="en", affect=AffectSnapshot(fresh=True))
        if res.recorded:
            played.append(res.recorded)
    # The anger tone owns five recordings; the session performs each once and then
    # falls back to the authored shapes rather than replaying a favourite.
    assert len(played) >= 4, played
    assert len(set(played)) == len(played), played


def test_the_briefing_is_narrated_not_acted():
    """The scenario briefing is not the character speaking.

    "As the team lead, you need to speak with Sam" is the
    robot reading the learner their situation in the second person. There is no
    emotion in it to perform, and gesticulating through it is exactly the "generic
    movement" complaint -- so it is narrated: unhurried, smaller, and only the shapes
    that read as explaining.
    """

    cfg = CueConfig(recorded=False, amp=1.6)
    brief = plan_cue(None, text="You will be speaking with Sam, who has been "
                                "waiting for a parcel since Monday.", config=cfg, seed=7)
    acted = plan_cue(None, text="I cannot afford to lose another day of pay over this.",
                     config=cfg, seed=7)
    assert brief.shape in ("talk:gaze_arc", "talk:list_three", "talk:sway"), brief.shape
    assert brief.k < acted.k
