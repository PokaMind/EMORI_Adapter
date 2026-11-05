"""The sentence itself, Pollen dialogue gestures, voice-locked plans.

Measured in a live session: 52 utterances, ONE authored family -- every reply after
the opening arrived on the wire with no cue_category, so the robot performed the whole
conversation on the talking flow. These pin the three planner answers: an untagged
sentence is READ for its own feeling (EN/ES/CA), a tone performs the Pollen recording
made for exactly that feeling (fear1 for "frightened", never irritated1), and an
untagged dialogue act (a question, a "Yes,", "let me help") performs one of Pollen's
conversational recordings -- seeded, alternating with the plain flow. Plus the marks
the voice-locked player reads: every cue plan is ``speech``; talking beats are
``pace="voice"`` with voice-following antennas; the legacy fallbacks stay bare.
"""

from __future__ import annotations

import pytest

from emori_adapter.behavior.cues import CUE_FAMILIES, CueConfig, plan_cue


def _text(words: int) -> str:
    return " ".join(["palabra"] * words)


def _rec_cfg(**kw) -> CueConfig:
    return CueConfig(recorded=True, **kw)


# --- marks the player reads ----------------------------------------------------------


def test_every_cue_plan_is_marked_as_speech_and_the_fallbacks_are_not():
    for family in CUE_FAMILIES:
        plan = plan_cue(family, text=_text(10), fresh=False)
        assert plan.speaking and all(i.speech for i in plan.intents)
    talking = plan_cue(None, text=_text(10))
    assert all(i.speech for i in talking.intents)
    legacy = plan_cue(None, text=_text(10), config=CueConfig(talk=False))
    assert not any(i.speech for i in legacy.intents)
    off = plan_cue("tension", text=_text(10), config=CueConfig(motion=False))
    assert not any(i.speech for i in off.intents)


def test_talking_beats_are_voice_paced_with_voice_following_antennas():
    plan = plan_cue(None, text=_text(12))
    beats = plan.intents[:-1]
    assert beats and all(i.pace == "voice" and i.antenna_voice for i in beats)
    assert plan.intents[-1].pace == "fixed"          # the settle is a plain goto


# --- the sentence itself ----------------------------------------------------------------


@pytest.mark.parametrize("text,family,slug", [
    ("I am truly so very frightened.", "tension", "fear"),
    ("Estoy muy asustada, no sé qué hacer.", "tension", "fear"),
    ("Tinc por que sigui greu.", "tension", "fear"),
    ("This is unacceptable and frankly unfair.", "tension", "anger"),
    ("I am very confused, I do not know where to go.", "tension", "confused"),
    ("I am so unwell and my head hurts.", "tension", "pain"),
    ("I am so very sorry if I have been bothering you.", "softening", "sorry"),
    ("Ho sento molt, perdó.", "softening", "sorry"),
    ("I am so tired, my strength gives out.", "softening", "sad"),
    ("Thank you, that is very kind of you.", "intimacy", "thanks"),
    ("Moltes gràcies, molt amable.", "intimacy", "thanks"),
    ("Don't worry, I understand, take your time.", "softening", "reassure"),
    ("Haha, that is really funny!", "levity", "joy"),
    ("That is amazing, let's go right away!", "energy", "excited"),
    ("Finally, what a relief, it is over.", "release", "relief"),
    ("I hope the doctor can see me soon.", "release", "hope"),
    ("Please hurry, this is an emergency!", "energy", "urgent"),
])
def test_the_sentence_itself_is_read_when_no_cue_arrives(text, family, slug):
    plan = plan_cue(None, text=text, fresh=False)
    assert plan.family == family, plan.reason
    assert plan.source == "text"
    assert "/text:%s" % slug in plan.reason
    assert plan.speaking is True


def test_text_affect_never_overrides_an_authored_cue_but_a_carried_one_yields():
    plan = plan_cue("levity", text="I am truly so very frightened.", fresh=False)
    assert plan.family == "levity" and plan.source == "pokamind"
    # A carried family is a guess about this sentence; its own words win.
    carried = plan_cue("softening", text="I am frightened.", carried=True, fresh=False)
    assert carried.family == "tension" and carried.source == "text"
    still = plan_cue("softening", text=_text(8), carried=True, fresh=False)
    assert still.family == "softening" and still.source == "carried"
    # A freeform direction body also wins over the text.
    bodied = plan_cue(None, text="I am frightened.", body="warm and caring", fresh=False)
    assert bodied.family == "softening"


def test_text_affect_can_be_switched_off():
    plan = plan_cue(None, text="I am truly so very frightened.", fresh=False,
                    config=CueConfig(text_affect=False))
    assert plan.family == "" and plan.reason.startswith("talking/")


def test_a_plain_sentence_stays_on_the_talking_flow():
    plan = plan_cue(None, text="Please, can you look at my papers so I know where to go.",
                    fresh=False)
    assert plan.family == "" and plan.reason.startswith("talking/")
    assert plan.source == ""


def test_text_read_families_are_a_notch_more_reserved_than_authored_ones():
    read = plan_cue(None, text="I am truly so very frightened right now.", fresh=False)
    authored = plan_cue("tension", text="I am truly so very frightened right now.",
                        body="nervous", fresh=False)
    assert read.k <= authored.k


# --- the recording made for exactly this feeling -----------------------------------------


def test_a_frightened_line_performs_fear_not_irritation():
    plans = [plan_cue(None, text="I am truly so very frightened of all of this.",
                      seed=seed << 3, fresh=False, config=_rec_cfg()) for seed in range(4)]
    moves = {p.recorded for p in plans}
    assert moves <= {"fear1"}, moves
    assert all(p.intents[0].kind == "play_recorded" for p in plans)
    assert all(p.reason.startswith("tension/neutral/text:fear/rec-") for p in plans)
    assert all(p.intents[-1].reason.endswith("(settle)") for p in plans)


def test_a_direction_body_prefers_its_own_recording_over_the_family_table():
    plan = plan_cue("tension", text=_text(12), body="voice tightening with fear",
                    seed=0, fresh=False, config=_rec_cfg())
    assert plan.recorded == "fear1"
    sorry = plan_cue("softening", text=_text(12), body="apologetic, sorry", seed=0,
                     fresh=False, config=_rec_cfg())
    assert sorry.recorded in ("oops1", "oops2")


def test_a_recording_is_followed_by_voice_paced_beats_when_the_sentence_runs_longer():
    # 18 words at 0.333 s/word under a 6 s cap: fear1 (3.5 s) leaves 2.5 s of sentence.
    plan = plan_cue("tension", text=_text(18), body="nervous", seed=0, fresh=False,
                    config=_rec_cfg(max_s=6.0))
    kinds = [i.kind for i in plan.intents]
    assert kinds[0] == "play_recorded" and kinds[-1] == "goto"
    tail = plan.intents[1:-1]
    assert tail and all(i.pace == "voice" for i in tail), "the hands must keep talking"


def test_allow_recorded_false_keeps_the_authored_shapes():
    plan = plan_cue("tension", text=_text(12), seed=0, fresh=False, config=_rec_cfg(),
                    allow_recorded=False)
    assert all(i.kind != "play_recorded" for i in plan.intents)
    assert plan.recorded == ""


# --- dialogue acts --------------------------------------------------------------------------


@pytest.mark.parametrize("text,act", [
    ("Yes, that is exactly what the nurse told me this morning.", "yes"),
    ("No, I cannot do that today, the desk is closed.", "no"),
    ("I think perhaps we should wait for the nurse to come back.", "think"),
    ("Let me help you with those papers and the forms today.", "help"),
    ("Come with me, this way, the office is just over here.", "come"),
    ("I see, that makes sense to me now, the second floor.", "ack"),
])
def test_untagged_dialogue_acts_perform_pollens_conversational_recordings(text, act):
    found = False
    for seed in range(0, 64, 32):      # seed bit 5 gates the act: one of the two fires
        plan = plan_cue(None, text=text, seed=seed, fresh=False, config=_rec_cfg())
        if plan.recorded:
            found = True
            assert plan.family == "" and plan.source == "dialogue"
            assert plan.reason == "talking/%s/rec-%s" % (act, plan.recorded)
            assert plan.intents[0].kind == "play_recorded"
            assert plan.intents[-1].reason.endswith("(settle)")
            assert all(i.speech for i in plan.intents)
    assert found, "no seed performed the dialogue act"


def test_dialogue_acts_alternate_with_the_plain_flow_by_seed():
    # A question is no longer an example here: the delivery read
    # owns "?" ungated (see test_a_question_always_performs_asking_not_a_coin_flip).
    # The remaining acts still alternate, so a reply is not all set-pieces.
    text = "I think perhaps we should wait for the nurse to come back."
    plans = [plan_cue(None, text=text, seed=seed, fresh=False, config=_rec_cfg())
             for seed in range(64)]
    assert any(p.recorded for p in plans) and any(not p.recorded for p in plans)


def test_dialogue_moves_need_recorded_and_can_be_switched_off():
    text = "I think perhaps we should wait for the nurse to come back."
    assert not any(plan_cue(None, text=text, seed=s, fresh=False).recorded for s in range(64))
    cfg = CueConfig(recorded=True, dialogue_moves=False)
    assert not any(plan_cue(None, text=text, seed=s, fresh=False, config=cfg).recorded
                   for s in range(64))


def test_the_delivery_read_follows_the_text_affect_switch():
    """It reads the sentence, so EMORI_CUE_TEXT owns it -- not EMORI_CUE_DIALOGUE."""

    text = "Please, can you tell me where the doctor is this afternoon?"
    assert plan_cue(None, text=text, seed=1, fresh=False, config=_rec_cfg()).source == "delivery"
    off = CueConfig(recorded=True, dialogue_moves=True, text_affect=False)
    assert plan_cue(None, text=text, seed=1, fresh=False, config=off).source != "delivery"


def test_a_dialogue_recording_must_fit_the_sentence():
    short = plan_cue(None, text="Why?", seed=0, fresh=False, config=_rec_cfg())
    assert short.recorded == ""          # 0.9 s budget fits no recording at all


def test_tone_of_voice_words_do_not_turn_an_apology_into_anger():
    """A "[softening| ...]" with a sharp/clipped direction must not perform
    irritated1. Only words that NAME anger may force tension; Pokamind's own
    tension vocabulary (tight, clipped, cold) stays a colour, never an override."""

    for body in ("voice clipped and sharp with worry", "snapping, tight"):
        plan = plan_cue("softening", text="Oh, I am so dreadfully sorry.", body=body,
                        seed=0, fresh=False, config=_rec_cfg())
        assert plan.family in ("softening", "tension"), plan.reason
        assert plan.recorded not in ("irritated1", "displeased1", "displeased2",
                                     "frustrated1", "furious1"), plan.reason
    angry = plan_cue("softening", text=_text(10), body="openly angry, furious", seed=0,
                     fresh=False, config=_rec_cfg())
    assert angry.family == "tension" and angry.recorded in ("irritated1", "displeased2",
                                                            "displeased1")


def test_a_gentle_reassuring_murmur_is_softening_not_gratitude():
    """"[softening| voice dropping to a gentle, reassuring murmur]" must not perform
    intimacy/grateful1. Warm words are read before hushed ones, and an
    apology never performs a thank-you."""

    plan = plan_cue("softening", text="Oh, I am so terribly sorry to be such a bother.",
                    body="voice dropping to a gentle, reassuring murmur", seed=0,
                    fresh=False, config=_rec_cfg())
    assert plan.family == "softening", plan.reason
    assert plan.recorded in ("", "understanding1", "understanding2", "calming1"), plan.reason
    whisper = plan_cue("intimacy", text=_text(12), body="almost a whisper", seed=0,
                       fresh=False, config=_rec_cfg())
    assert whisper.family == "intimacy" and whisper.recorded in ("", "understanding2")


# --- the emotion must match what is being said, and be visible at once --------------------


def test_a_carried_family_yields_to_the_sentence_s_own_words():
    """"Thank you, that is kind" inside a tense reply performs gratitude, not the
    turn's leftover tension."""

    plan = plan_cue("tension", text="Thank you, that is so kind of you.", carried=True,
                    fresh=False)
    assert plan.family == "intimacy" and plan.source == "text"
    assert "/text:thanks" in plan.reason
    neutral = plan_cue("tension", text=_text(10), carried=True, fresh=False)
    assert neutral.family == "tension" and neutral.source == "carried"


def test_an_authored_family_picks_the_recording_its_words_name():
    """An authored tension line that says fear performs fear1; one that says anger
    performs the angry recording -- the family table no longer mixes them."""

    fear = plan_cue("tension", text="I am so frightened, please help me find it.",
                    seed=0, fresh=False, config=_rec_cfg())
    assert fear.family == "tension" and fear.source == "pokamind"
    assert fear.recorded == "fear1", fear.reason
    anger = plan_cue("tension", text="This is unacceptable, I am furious about it.",
                     seed=0, fresh=False, config=_rec_cfg())
    assert anger.recorded in ("irritated1", "displeased2", "displeased1"), anger.reason
    # Words of a DIFFERENT family never change an authored family's recording.
    calm = plan_cue("tension", text="Thank you, that is so kind of you.", seed=0,
                    fresh=False, config=_rec_cfg())
    assert calm.family == "tension"


def test_the_first_move_is_visible_at_the_voice_onset():
    """A plan whose first goto is long gets a 0.3 s linear attack to 40 % of the way,
    same destination and same total time -- so the head is moving within the first
    100 ms of the sentence instead of ramping invisibly through min-jerk."""

    plan = plan_cue("tension", text=_text(12), fresh=False)      # sink: a long first goto
    first, second = plan.intents[0], plan.intents[1]
    assert first.reason.endswith("(attack)") and first.interpolation == "linear"
    assert first.duration == 0.3
    assert second.head == plan.intents[1].head
    assert abs(first.head.pitch - 2.0) == pytest.approx(0.4 * abs(second.head.pitch - 2.0))
    assert sum(i.duration for i in plan.intents) <= plan.budget_s
    # Short-beat plans (the talking flow) need no attack: their first move is quick.
    talk = plan_cue(None, text=_text(12), fresh=False)
    assert not talk.intents[0].reason.endswith("(attack)")


def test_de_escalation_suppresses_dialogue_and_text_recordings():
    plan = plan_cue(None, text="I am truly so very frightened of this.", seed=0,
                    escalation=0.95, fresh=False, config=_rec_cfg())
    assert all(i.kind != "play_recorded" for i in plan.intents)
    assert "deescalated" in plan.reason

def test_a_question_always_performs_asking_not_a_coin_flip():
    """Every question, not half of them.

    The "ask" dialogue act was gated behind seed bit 5, so a session's questions
    performed inquiring roughly half the time and a plain talking shape the rest.
    A question is always a question: the delivery read owns it now, ungated, and it
    carries an emotion (leaning in to ask) rather than only a recording.
    """

    text = "Please, can you tell me where the doctor is this afternoon?"
    for seed in range(0, 128, 16):
        plan = plan_cue(None, text=text, seed=seed, fresh=False, config=_rec_cfg())
        assert plan.source == "delivery", plan.reason
        assert "text:asking" in plan.reason
        assert plan.family in ("intimacy",)
        assert all(i.speech for i in plan.intents)


def test_a_question_still_asks_when_no_recording_fits():
    # Short question, no inquiring recording fits the budget: the SHAPE still asks.
    plan = plan_cue(None, text="What happens now, exactly?", seed=3, fresh=False,
                    config=_rec_cfg())
    assert plan.source == "delivery" and "text:asking" in plan.reason
    assert plan.recorded == "" or plan.recorded.startswith("inquiring")
