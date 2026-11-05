"""Per-sentence cue planning: the mapping table, its bounds, and its no-op path.

Everything here is pure: no ROS, no robot, no hostapp, no clock. The three
properties that matter on a live robot are asserted as properties rather than
examples -- a plan is always bounded by the sentence it decorates, always ends
settled, and never exceeds the (unverified-on-hardware) z / body-yaw limits.
"""

from __future__ import annotations

import pytest

from emori_adapter.behavior import repertoire as R
from emori_adapter.behavior.cues import (
    CUE_FAMILIES,
    CueConfig,
    SkillError,
    intensity,
    plan_cue,
    sentence_duration_s,
    skill_to_intents,
    word_count,
)

# The three affect bands of the mapping table, as (valence, arousal) probes.
BANDS = {"low": (-0.6, 0.2), "neutral": (0.0, 0.0), "up": (0.6, 0.5)}
WORD_COUNTS = (1, 2, 3, 5, 8, 13, 20, 40, 200)


def _text(words: int) -> str:
    return " ".join(["palabra"] * words)


def _dump(intents) -> list[dict]:
    return [i.model_dump() for i in intents]


def _driver_beats(text: str) -> int:
    """driver.on_utterance's formula, copied verbatim (driver.py:911)."""

    return max(2, min(8, len(text) // 25)) if text else 3


# --- bounds ---------------------------------------------------------------


@pytest.mark.parametrize("family", CUE_FAMILIES)
@pytest.mark.parametrize("band", sorted(BANDS))
@pytest.mark.parametrize("words", WORD_COUNTS)
def test_plan_is_bounded_and_ends_settled(family, band, words):
    valence, arousal = BANDS[band]
    cfg = CueConfig()
    text = _text(words)
    plan = plan_cue(family, text=text, valence=valence, arousal=arousal, config=cfg)

    budget = min(sentence_duration_s(text), cfg.max_s)
    assert plan.intents, "a cue family must always produce motion"
    assert plan.budget_s == pytest.approx(budget)
    # The whole point: motion never outlives the sentence it decorates.
    assert sum(i.duration for i in plan.intents) <= budget
    assert plan.intents[-1].reason.endswith("(settle)")
    assert plan.intents[-1].kind == "goto"


@pytest.mark.parametrize("family", CUE_FAMILIES)
@pytest.mark.parametrize("band", sorted(BANDS))
def test_z_and_body_yaw_stay_inside_the_unverified_limits(family, band):
    valence, arousal = BANDS[band]
    cfg = CueConfig()
    plan = plan_cue(family, text=_text(12), valence=valence, arousal=arousal, config=cfg)
    for intent in plan.intents:
        if intent.head is not None:
            assert abs(intent.head.z) <= cfg.z_mm
        # body yaw has never been measured on a robot: default is OFF, everywhere.
        assert intent.body_yaw is None


def test_z_limit_follows_the_env_knob(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_Z_MM", "2.0")
    monkeypatch.setenv("EMORI_CUE_RECORDED", "0")   # authored shapes carry the z here
    cfg = CueConfig.from_env()
    assert cfg.z_mm == 2.0
    plan = plan_cue("tension", text=_text(12), valence=-0.6, arousal=0.3, config=cfg)
    assert any(i.head is not None and i.head.z < 0 for i in plan.intents)
    assert all(i.head is None or abs(i.head.z) <= 2.0 for i in plan.intents)


def test_body_yaw_terms_appear_only_when_the_flag_is_on(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_BODY_YAW", "1")
    cfg = CueConfig.from_env()
    assert cfg.body_yaw is True
    plan = plan_cue("levity", text=_text(12), valence=0.6, arousal=0.5, config=cfg)
    yaws = [i.body_yaw for i in plan.intents if i.body_yaw is not None]
    assert yaws
    assert all(abs(y) <= R.CUE_BODY_YAW_MAX_RAD for y in yaws)


def test_cue_max_s_caps_a_long_sentence():
    cfg = CueConfig(max_s=1.5)
    plan = plan_cue("energy", text=_text(60), config=cfg)
    assert plan.budget_s == pytest.approx(1.5)
    assert sum(i.duration for i in plan.intents) <= 1.5


# --- the no-family path: full-DOF talking, with the legacy shape behind a switch ---


@pytest.mark.parametrize("text", ["Hi.", "a" * 60, _text(30), "  spaced  "])
def test_uncategorized_gets_the_full_dof_talking_flow(text):
    """An untagged sentence is most sentences of a live reply: it must MOVE.

    The original contract (uncategorized == the antenna-flick loop, byte-for-byte)
    is what made the robot look antenna-only in a live session -- 10 of 11
    sentences took it. Now the untagged sentence performs the seeded
    conversational flow, and the byte-for-byte guarantee lives behind
    EMORI_CUE_TALK=0 instead.
    """

    plan = plan_cue(None, text=text)
    assert plan.family == ""
    assert plan.speaking is True
    assert plan.reason.startswith("talking/")
    budget = min(sentence_duration_s(text.strip()), CueConfig().max_s)
    assert sum(i.duration for i in plan.intents) <= budget
    assert plan.intents[-1].reason.endswith("(settle)")
    # The whole platform participates, not just the antennas.
    heads = [i.head for i in plan.intents[:-1] if i.head is not None]
    assert any(abs(h.yaw) > 0 for h in heads)
    assert any(abs(h.roll) > 0 for h in heads)
    assert any(abs(h.z) > 0 for h in heads)
    assert any(i.antennas is not None for i in plan.intents[:-1])


def test_talk_kill_switch_restores_the_legacy_gesture():
    text = _text(10)
    plan = plan_cue(None, text=text, config=CueConfig(talk=False))
    expected = R.talking_gesture(beats=_driver_beats(text), reason="speaking (AI turn)")
    assert _dump(plan.intents) == _dump(expected)
    assert plan.reason == "uncategorized"
    assert plan.speaking is False


@pytest.mark.parametrize("category", ["", "   ", "unknown", "TENSION!", "[levity| dry]", "42"])
def test_unknown_categories_fall_back_without_raising(category):
    plan = plan_cue(category, text=_text(10))
    assert plan.family == ""
    assert plan.speaking is True
    assert plan.reason.startswith("talking/")
    assert sum(i.duration for i in plan.intents) <= plan.budget_s


def test_the_seed_varies_the_talking_flow_between_sentences():
    """Two sentences must never loop the identical shape."""

    text = _text(12)
    plans = [plan_cue(None, text=text, seed=s) for s in range(4)]
    shapes = [_dump(p.intents) for p in plans]
    assert len({repr(s) for s in shapes}) >= 3  # mirror x variant = 4 looks
    reasons = {p.reason for p in plans}
    assert len(reasons) >= 3


def test_the_seed_mirrors_a_family_plan_too():
    a = plan_cue("levity", text=_text(12), valence=0.6, arousal=0.5, seed=0)
    b = plan_cue("levity", text=_text(12), valence=0.6, arousal=0.5, seed=1)
    assert _dump(a.intents) != _dump(b.intents)
    assert a.family == b.family == "levity"


def test_a_carried_family_is_softer_and_says_so():
    fresh = plan_cue("tension", text=_text(12), valence=-0.5, arousal=0.4)
    carried = plan_cue("tension", text=_text(12), valence=-0.5, arousal=0.4, carried=True)
    assert carried.family == "tension"
    assert "/carried" in carried.reason
    assert carried.k < fresh.k
    assert carried.speaking is True


def test_amp_scales_the_motion_and_the_cap_holds():
    text = _text(12)
    soft = plan_cue("energy", text=text, valence=0.6, arousal=0.5, config=CueConfig(amp=1.0))
    loud = plan_cue("energy", text=text, valence=0.6, arousal=0.5, config=CueConfig(amp=2.0))
    peak = lambda p: max(abs(i.head.pitch) for i in p.intents if i.head is not None)  # noqa: E731
    assert peak(loud) > peak(soft)
    # The cap: even at EMORI_CUE_AMP=2.0 no authored angle leaves the platform range.
    for plan in (soft, loud):
        for intent in plan.intents:
            if intent.head is not None:
                assert abs(intent.head.pitch) <= 25.0
                assert abs(intent.head.yaw) <= 25.0
                assert abs(intent.head.roll) <= 25.0


def test_talking_de_escalates_like_the_families(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_BODY_YAW", "1")
    cfg = CueConfig.from_env()
    plan = plan_cue(None, text=_text(30), valence=-0.6, arousal=0.9, escalation=0.9, config=cfg)
    assert plan.reason.endswith("/deescalated")
    assert plan.k <= 0.5
    assert len([i for i in plan.intents if not i.reason.endswith("(settle)")]) <= 3
    assert all(i.body_yaw is None for i in plan.intents)


def test_the_settle_returns_the_body_to_centre_when_body_yaw_is_on(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_BODY_YAW", "1")
    cfg = CueConfig.from_env()
    for category in ("levity", None):
        plan = plan_cue(category, text=_text(12), valence=0.6, arousal=0.5, config=cfg)
        assert plan.intents[-1].reason.endswith("(settle)")
        assert plan.intents[-1].body_yaw == 0.0


def test_empty_text_keeps_the_three_beat_gesture_even_with_a_family():
    plan = plan_cue("tension", text="   ")
    assert _dump(plan.intents) == _dump(R.talking_gesture(beats=3, reason="speaking (AI turn)"))
    assert plan.reason == "empty_text"


def test_motion_kill_switch_returns_the_legacy_gesture():
    plan = plan_cue("levity", text=_text(10), config=CueConfig(motion=False))
    assert _dump(plan.intents) == _dump(
        R.talking_gesture(beats=_driver_beats(_text(10)), reason="speaking (AI turn)"))
    assert plan.reason == "motion_off"


def test_family_names_are_case_and_space_tolerant():
    assert plan_cue(" Tension ", text=_text(10)).family == "tension"


# --- 6 families x 3 affect bands = 18 distinct realised motions ------------


def test_eighteen_recipes_are_all_distinct():
    plans = {}
    for family in CUE_FAMILIES:
        for band, (valence, arousal) in BANDS.items():
            plan = plan_cue(family, text=_text(12), valence=valence, arousal=arousal)
            assert plan.family == family
            assert plan.reason.startswith("%s/%s/v" % (family, band))
            plans[(family, band)] = _dump(plan.intents)
    assert len(plans) == 18
    for family in CUE_FAMILIES:
        shapes = [plans[(family, band)] for band in BANDS]
        assert shapes[0] != shapes[1] != shapes[2] and shapes[0] != shapes[2], family
    # ...and every family reads differently from every other family.
    neutral = [plans[(f, "neutral")] for f in CUE_FAMILIES]
    assert len({repr(s) for s in neutral}) == len(CUE_FAMILIES)


def test_tension_is_stillness_and_sinks_toward_the_shell():
    plan = plan_cue("tension", text=_text(12), valence=-0.5, arousal=0.4)
    # The 0.3 s "(attack)" is the sink's own first 40 %, split off so the motion is
    # visible at the voice onset -- not a new shape.
    shapes = [i for i in plan.intents if not i.reason.endswith("(attack)")]
    assert len(shapes) == 3  # sink, hold, settle -- never more
    assert shapes[0].head.z < 0 and shapes[0].head.pitch > 0
    assert shapes[0].antennas[0] < 0  # drooped
    assert plan.intents[0].reason.endswith("(attack)") and plan.intents[0].head.pitch > 0


def test_levity_is_livelier_rotation_with_open_antennas():
    plan = plan_cue("levity", text=_text(12), valence=0.6, arousal=0.5)
    assert len(plan.intents) >= 4
    assert any(abs(i.head.yaw) > 0 and abs(i.head.roll) > 0 for i in plan.intents if i.head)
    assert plan.intents[-1].antennas == R.ANTENNA_PERKED


def test_a_struggling_learner_never_gets_the_full_bounce():
    low = plan_cue("levity", text=_text(20), valence=-0.6, arousal=0.2)
    up = plan_cue("levity", text=_text(20), valence=0.6, arousal=0.5)
    assert len(low.intents) < len(up.intents)


def test_unfresh_affect_ignores_the_bands():
    """No cycle yet this session: do not guess a learner we have not seen."""

    stale = plan_cue("softening", text=_text(12), valence=-0.9, arousal=0.9, fresh=False)
    assert stale.reason.startswith("softening/neutral")
    assert stale.k == pytest.approx(0.9)


# --- de-escalation --------------------------------------------------------


@pytest.mark.parametrize("family", ["tension", "energy", "levity"])
def test_de_escalation_down_maps_the_loud_families(family):
    plan = plan_cue(family, text=_text(20), valence=-0.6, arousal=0.8, escalation=0.8)
    assert plan.family == "softening"
    assert plan.reason.endswith("/deescalated")
    assert plan.k <= intensity(-0.6, 0.8) * 0.4 + 1e-9
    assert sum(i.duration for i in plan.intents) <= plan.budget_s


def test_de_escalation_leaves_the_quiet_families_alone_but_still_damps():
    calm = plan_cue("intimacy", text=_text(20), valence=-0.6, arousal=0.8, escalation=0.8)
    hot = plan_cue("intimacy", text=_text(20), valence=-0.6, arousal=0.8, escalation=0.0)
    assert calm.family == "intimacy"
    assert calm.k < hot.k


def test_de_escalation_caps_beats_and_drops_body_yaw(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_BODY_YAW", "1")
    cfg = CueConfig.from_env()
    plan = plan_cue("energy", text=_text(40), valence=-0.6, arousal=0.9, escalation=0.9, config=cfg)
    beats = [i for i in plan.intents
             if not (i.reason.endswith("(settle)") or i.reason.endswith("(attack)"))]
    assert len(beats) <= 2
    assert all(i.body_yaw is None for i in plan.intents)


def test_de_escalation_threshold_is_tunable():
    cfg = CueConfig(deescalate_at=0.2)
    assert plan_cue("energy", text=_text(12), escalation=0.3, config=cfg).family == "softening"
    assert plan_cue("energy", text=_text(12), escalation=0.3).family == "energy"


# --- timing model ---------------------------------------------------------


def test_word_count_matches_pokamind_semantics():
    assert word_count("") == 0
    assert word_count("   ") == 0
    assert word_count("one two  three\nfour") == 4
    # CJK counts characters, plus any Latin words mixed in...
    assert word_count("你好世界") == 4
    assert word_count("你好世界你好世界 hello") == 9
    # ...but a mostly-Latin string stays on the whitespace path, CJK chars and all,
    # exactly as Pokamind's own word count decides it.
    assert word_count("你好 hello there") == 3


def test_sentence_duration_is_333ms_per_word_clamped():
    assert sentence_duration_s(_text(1)) == pytest.approx(0.9)      # clamped up
    assert sentence_duration_s(_text(9)) == pytest.approx(2.997)    # 0.333 * 9
    assert sentence_duration_s(_text(200)) == pytest.approx(6.0)    # clamped down
    assert sentence_duration_s("") == pytest.approx(0.9)


def test_intensity_is_monotonic_in_magnitude():
    values = [intensity(v, 0.0) for v in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert values == sorted(values)
    assert 0.5 <= values[0] and values[-1] <= 1.0
    assert intensity(3.0, 3.0) <= 1.0  # magnitude saturates like to_expression's


# --- the freeform-direction lexicon -----------------------------------------

LEXICON_PROBES = [
    ("nervous and tight", "tension"),
    ("a dry chuckle", "levity"),
    ("almost a whisper", "intimacy"),
    ("warm and reassuring", "softening"),
    ("bright and eager", "energy"),
    ("firm, deliberate", None),      # family stays, beats suppressed
    ("heavy and weary", "softening"),
    ("a relieved exhale", "release"),
    ("curious, wondering", None),    # family stays, opening tilt
    ("emphatic and pointed", None),  # family stays, opening nod
    ("startled, sudden", None),      # family stays, opening startle
    # --- the expansion groups ---
    ("cold and distant", "tension"),
    ("proud and assured", "energy"),
    ("a hopeful, lighter note", "release"),
    ("teasing, a little sly", "levity"),
    ("genuinely sorry, contrite", "softening"),
    ("voice trembling and shaky", "tension"),
    ("tired and drained", "softening"),
]


def test_a_nervous_direction_shivers_the_antennas_too():
    """'antennas shaking' is a first-class read: the tremor flutters them."""

    plan = plan_cue("tension", text=_text(20), body="voice trembling and shaky")
    tremor = [i for i in plan.intents if i.reason.endswith("(tremor)")]
    assert len(tremor) == 2
    assert tremor[0].antennas != tremor[1].antennas


@pytest.mark.parametrize("body,forced", LEXICON_PROBES)
def test_every_lexicon_group_shifts_the_plan(body, forced):
    text = _text(20)
    shipped = plan_cue("tension", text=text, body=None)
    directed = plan_cue("tension", text=text, body=body)
    assert _dump(directed.intents) != _dump(shipped.intents), body
    if forced:
        assert directed.family == forced
    else:
        assert directed.family == "tension"
    assert sum(i.duration for i in directed.intents) <= directed.budget_s
    assert directed.intents[-1].reason.endswith("(settle)")


def test_an_unmatched_direction_is_the_shipped_plan():
    text = _text(20)
    assert _dump(plan_cue("levity", text=text, body="in Klingon").intents) == \
        _dump(plan_cue("levity", text=text, body=None).intents)


def test_body_none_is_the_shipped_path():
    """A sentence whose cue was thinned away still plans without a body."""

    text = _text(20)
    assert _dump(plan_cue("levity", text=text).intents) == \
        _dump(plan_cue("levity", text=text, body=None).intents)


def test_de_escalation_outranks_a_forced_family():
    plan = plan_cue("intimacy", text=_text(20), body="nervous and tight", escalation=0.9)
    assert plan.family == "softening"


# --- skill facade ---------------------------------------------------------


def test_skill_set_expression_maps_every_label():
    for label in ("neutral", "happy", "sad", "angry", "surprised", "scared", "disgusted"):
        intents = skill_to_intents("set_expression", {"label": label, "confidence": 0.8})
        assert intents
        assert sum(i.duration for i in intents) <= 2.0
        # every skill plan leaves the head in the attend pose, like a cue plan
        assert intents[-1].head.pitch == pytest.approx(2.0)


def test_skill_look_at_range_checks():
    intents = skill_to_intents("look_at", {"yaw_deg": -20, "pitch_deg": 10, "duration": 1.0})
    assert len(intents) == 1 and intents[0].head.yaw == -20
    for params in ({"yaw_deg": 45}, {"pitch_deg": -40}, {"duration": 9.0}, {"yaw_deg": "left"}):
        with pytest.raises(SkillError) as excinfo:
            skill_to_intents("look_at", params)
        assert excinfo.value.code == "out_of_range"


def test_skill_replay_motion_covers_the_vocabulary():
    for name in ("greeting", "farewell", "nod", "listening", "thinking", "calm_settle",
                 "talking", "head_tilt", "antenna_wiggle", "look_around"):
        intents = skill_to_intents("replay_motion", {"name": name, "intensity": 0.5})
        assert intents
        assert sum(i.duration for i in intents) <= 2.0


def test_skill_junk_is_rejected_with_a_wire_code():
    with pytest.raises(SkillError) as unknown:
        skill_to_intents("dance", {})
    assert unknown.value.code == "bad_skill"
    for skill, params in (("set_expression", {"label": "purple"}),
                          ("set_expression", {"label": "happy", "confidence": 4}),
                          ("replay_motion", {"name": "backflip"}),
                          ("replay_motion", {"name": "nod", "intensity": -2})):
        with pytest.raises(SkillError) as bad:
            skill_to_intents(skill, params)
        assert bad.value.code == "out_of_range"


def test_skill_intensity_scales_the_motion():
    soft = skill_to_intents("replay_motion", {"name": "nod", "intensity": 0.0})
    hard = skill_to_intents("replay_motion", {"name": "nod", "intensity": 1.0})
    assert hard[0].head.pitch > soft[0].head.pitch


@pytest.mark.parametrize("family", CUE_FAMILIES)
def test_each_family_performs_four_distinct_shapes(family):
    """Uniqueness is structural, not just mirrored: variants 0..3 all differ."""

    shapes = []
    for v in range(4):
        plan = plan_cue(family, text=_text(12), seed=v << 1, fresh=False)
        assert plan.reason.startswith("%s/neutral/v%d" % (family, v))
        shapes.append(repr(_dump(plan.intents)))
    assert len(set(shapes)) == 4, family


def test_consecutive_same_family_sentences_never_repeat_a_shape():
    """Scheduler rule: the same shape class twice in a row flips a variant bit."""

    from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CueScheduler

    sent = []
    scheduler = CueScheduler(lambda intents, guard: (sent.append(list(intents)), True)[1],
                             config=CueConfig())
    text = "the very same sentence spoken twice"
    r1 = scheduler.on_sentence(text=text, cue_category="tension", cue_body=None,
                               lang="en", affect=AffectSnapshot())
    # index differs so the seed differs, but force the collision case too:
    r2 = scheduler.on_sentence(text=text, cue_category="tension", cue_body=None,
                               lang="en", affect=AffectSnapshot())
    assert r1.reason != r2.reason or sent[0] != sent[1]
    assert [i.model_dump() for i in sent[0]] != [i.model_dump() for i in sent[1]]


# --- Pollen's own recorded moves on freshly-authored cues -------------------


def test_an_authored_cue_can_perform_an_official_recording():
    cfg = CueConfig(recorded=True)
    plan = plan_cue("release", text=_text(12), seed=0, fresh=False, config=cfg)
    assert plan.intents[0].kind == "play_recorded"
    assert plan.intents[0].move
    assert plan.reason.startswith("release/neutral/rec-")
    assert plan.intents[-1].reason.endswith("(settle)")
    assert plan.speaking is True


def test_the_seed_cycles_the_official_repertoire():
    cfg = CueConfig(recorded=True)
    moves = {plan_cue("tension", text=_text(12), seed=seed << 3, fresh=False,
                      config=cfg).intents[0].move for seed in range(4)}
    assert len(moves) >= 3


def test_carried_and_deescalated_cues_keep_the_authored_shapes():
    cfg = CueConfig(recorded=True)
    carried = plan_cue("tension", text=_text(12), carried=True, fresh=False, config=cfg)
    assert all(i.kind != "play_recorded" for i in carried.intents)
    hot = plan_cue("energy", text=_text(12), escalation=0.9, fresh=False, config=cfg)
    assert all(i.kind != "play_recorded" for i in hot.intents)


def test_a_short_sentence_gets_no_recording_it_cannot_fit():
    cfg = CueConfig(recorded=True)
    plan = plan_cue("tension", text="Ok.", seed=0, fresh=False, config=cfg)
    assert all(i.kind != "play_recorded" for i in plan.intents)


def test_recorded_stays_off_unless_armed():
    plan = plan_cue("release", text=_text(12), seed=0, fresh=False)
    assert all(i.kind != "play_recorded" for i in plan.intents)


@pytest.mark.parametrize("family", CUE_FAMILIES)
def test_every_family_has_its_own_sustaining_beat(family):
    from emori_adapter.behavior.cues import sustain_for

    beat = sustain_for(family, 0)
    assert family in beat.reason and beat.tint == family
    assert beat.speech and beat.pace == "voice"


def test_a_soft_family_sustains_smaller_than_a_lively_one():
    from emori_adapter.behavior.cues import sustain_for

    soft, lively = sustain_for("intimacy", 0), sustain_for("energy", 0)
    assert abs(soft.head.yaw) < abs(lively.head.yaw)
    assert abs(soft.antennas[0]) < abs(lively.antennas[0])


def test_no_family_gives_the_plain_sustaining_beat():
    from emori_adapter.behavior import repertoire as R
    from emori_adapter.behavior.cues import sustain_for

    assert sustain_for("", 3).model_dump() == R.sustain_beat(3).model_dump()


def test_a_plan_says_which_family_it_is_for():
    from emori_adapter.behavior.cues import CueConfig, plan_cue

    plan = plan_cue("tension", text="The room is on the second floor, next to the lifts.",
                    config=CueConfig(recorded=False, talk=True, carry=True))
    assert all(i.tint == "tension" for i in plan.intents)
