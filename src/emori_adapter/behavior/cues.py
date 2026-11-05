"""Per-sentence expression cues: a Pokamind cue family -> a bounded motion plan.

Pokamind tags every AI sentence with a ``[family| direction]`` audio cue and, at
playback onset of that sentence, forwards the *family* (and, when the server provides it,
the freeform direction text) on the kiosk
``pokamind_ai_utterance`` postMessage. The tag is extracted from the RAW sentence
*before* the session TTS ladder mutates the text, and the brackets never leave the
iframe -- so the robot can move to the voice's intent without any risk of a cue
being spoken.

This module turns that per-sentence signal into the motion performed WHILE the
sentence is spoken. Rules that make it safe to run on every sentence of a live
session:

* **Pure.** No I/O, no hostapp import, no clock, no robot. A plan is a function of
  ``(cue_category, cue_body, text, valence, arousal, escalation, fresh)`` plus a
  :class:`CueConfig` - which is what makes the whole expression pipeline testable
  off the robot.
* **Bounded.** ``sum(intent.duration) <= min(sentence_duration_s(text), EMORI_CUE_MAX_S)``
  and every plan ends in a settle intent, so a sentence's motion can never outlive
  the voice it decorates and the robot holds the attend pose rather than flailing
  if the audio runs long. The cap also keeps one sentence from occupying the
  12-slot RobotPlayer queue while the next sentence's cue is ~0.3 s away.
* **Never louder than the room.** Above ``EMORI_CUE_DEESCALATE_AT`` the excited
  families down-map to ``softening`` and amplitudes collapse, regardless of what
  the AI's voice is doing - the same healthcare policy ``BehaviorEngine.step``
  applies on the per-cycle lane, so the two lanes agree instead of fighting.
* **Every sentence is a performance.** The server tags one cue per reply, but a reply is many sentences -- so the original
  "uncategorized falls back to talking_gesture" rule left ~10 of 11 sentences on
  the fixed antenna-flick loop (measured live). Now an uncategorized sentence gets
  :func:`repertoire.talking_flow` -- seeded, full-DOF conversational gesticulation
  -- and the scheduler CARRIES the turn's last family onto its untagged sentences
  at reduced amplitude. ``EMORI_CUE_TALK=0`` restores the legacy antenna-flick
  fallback; empty text and ``EMORI_CUE_MOTION=0`` still reproduce it byte-for-byte.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, replace
from typing import Optional

from emori_adapter.behavior import repertoire as R
from emori_adapter.models import HeadPose, MotionIntent

log = logging.getLogger("emori.behavior.cues")

# The closed cue vocabulary of the wire protocol.
CUE_FAMILIES: tuple[str, ...] = ("tension", "levity", "energy", "intimacy", "softening", "release")

# The session page's playback model: 333 ms per word, computed over the
# cue-STRIPPED text. Reusing the number is what makes the motion envelope match the
# voice the learner actually hears.
S_PER_WORD = 0.333
MIN_SENTENCE_S = 0.9
MAX_SENTENCE_S = 6.0

_CUE_MAX_S_DEFAULT = 4.0
_CUE_MAX_S_LIMIT = 8.0
# The TOTAL amplitude scalar (k x lexicon x EMORI_CUE_AMP) is capped so the raised
# base angles can never leave the platform's comfortable envelope: the largest
# authored head angle is 16 deg, and 16 x 1.55 = 24.8 deg < the ~25 deg Stewart range.
_AMP_CAP = 1.55
# An untagged sentence riding the turn's carried family performs it a notch softer
# than the sentence the LLM actually tagged, so the authored beat stays the loudest.
_CARRIED_K = 0.8
_DEESCALATE_AT_DEFAULT = 0.6
# Above the threshold every excited family reads as agitation mirrored back.
_DEESCALATE_MAP = {"tension": "softening", "energy": "softening", "levity": "softening"}
_DEESCALATE_K = 0.4
_DEESCALATE_BEAT_CAP = 2

# The reason string driver.on_utterance has always stamped on the speaking
# gesture; the uncategorized fallback must reproduce it byte-for-byte.
TALKING_REASON = "speaking (AI turn)"

# Pollen's OWN recorded emotion moves (the reachy-mini-emotions-library dataset
# cached beside the daemon), curated per family with their measured durations so
# the planner can pick one that fits the sentence being spoken. Played by the
# daemon itself at 50 fps -- the real authored performances, antennas at 110+
# degrees and all -- and stopped by RobotPlayer the moment a newer sentence
# preempts. Selection is seed-picked, so the same family cycles its repertoire.
#
# The rosters cover the whole usable library: with only three or four candidates per
# family the short ones won every fit test and a session used the same few
# recordings over and over. Every name below is measured (see
# RECORDED_DURATIONS, read from the dataset's own time[] arrays), and
# the scheduler's variety memory rotates through them, so a family now has a
# repertoire instead of a favourite.
RECORDED_FAMILY_MOVES: dict[str, tuple[tuple[str, float], ...]] = {
    "tension": (("irritated1", 2.5), ("displeased2", 2.9), ("displeased1", 3.4),
                ("fear1", 3.5), ("contempt1", 3.6), ("impatient2", 3.9),
                ("impatient1", 4.0), ("reprimand3", 4.3), ("go_away1", 4.8),
                ("rage1", 5.1), ("irritated2", 5.3), ("furious1", 5.7),
                ("frustrated1", 6.0)),
    "levity": (("cheerful1", 2.8), ("laughing2", 2.9), ("proud2", 3.2),
               ("amazed1", 3.4), ("welcoming2", 4.3), ("laughing1", 4.6)),
    "energy": (("success1", 2.3), ("success2", 2.4), ("enthusiastic1", 2.7),
               ("proud2", 3.2), ("proud3", 3.4), ("enthusiastic2", 3.4),
               ("electric1", 3.5), ("proud1", 3.8), ("helpful1", 4.4)),
    "intimacy": (("grateful1", 2.5), ("understanding2", 2.6), ("helpful2", 3.8),
                 ("understanding1", 3.9), ("serenity1", 4.6), ("loving1", 5.6),
                 ("calming1", 6.1)),
    "softening": (("oops1", 2.5), ("understanding2", 2.6), ("oops2", 2.7),
                  ("understanding1", 3.9), ("serenity1", 4.6), ("resigned1", 4.7),
                  ("downcast1", 5.9), ("uncomfortable1", 6.0), ("calming1", 6.1)),
    "release": (("grateful1", 2.5), ("helpful2", 3.8), ("serenity1", 4.6),
                ("relief1", 5.0), ("relief2", 6.9)),
}
# A recorded move may overrun the spoken sentence by this factor. 1.6 rather than
# 1.35: since the voice-locked player stops a recording the moment
# the VOICE stops (not just when the next sentence preempts), a longer move is
# cut where the sentence ends rather than running past it -- so the tolerance
# buys the library's longer, fuller-bodied performances instead of leaving every
# family with only its two shortest.
_RECORDED_OVERRUN = 1.6
GREETING_MOVE = ("welcoming1", 3.5)
FAREWELL_MOVE = ("grateful1", 2.5)

# The server's word-count rule: whitespace tokens for space-delimited
# scripts, per-character for CJK. Catalan and English take the whitespace path.
# Codepoint ranges rather than literal characters so this file stays ASCII (the
# repo has been bitten by cp1252 decoding of non-ASCII source before).
_CJK_WIDE_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F), (0xFF00, 0xFFEF))
_CJK_CHAR_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))
_LATIN_CHAR_RE = re.compile(r"[a-zA-Z]")
_LATIN_WORD_RE = re.compile(r"[a-zA-Z]+")


class SkillError(Exception):
    """A bounded skill request that cannot be realised.

    ``code`` is the wire error code ``/api/skill`` returns ("bad_skill" /
    "out_of_range"), so the HTTP layer never has to interpret the message.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CueConfig:
    """Cue-planning limits. Defaults are the shipped ones; ``from_env`` reads the robot."""

    motion: bool = True                      # EMORI_CUE_MOTION=0 is a true kill-switch
    max_s: float = _CUE_MAX_S_DEFAULT        # EMORI_CUE_MAX_S
    z_mm: float = R.CUE_Z_MM_DEFAULT         # EMORI_CUE_Z_MM (18 on a Reachy Mini Wireless)
    body_yaw: bool = False                   # EMORI_CUE_BODY_YAW (on, on a Reachy Mini Wireless)
    deescalate_at: float = _DEESCALATE_AT_DEFAULT  # EMORI_CUE_DEESCALATE_AT
    amp: float = 1.0                         # EMORI_CUE_AMP: robot-tunable amplitude multiplier
    talk: bool = True                        # EMORI_CUE_TALK=0: legacy antenna-flick fallback
    carry: bool = True                       # EMORI_CUE_CARRY=0: untagged sentences never inherit
    # Play Pollen's OWN recorded emotion moves on freshly-authored cues when one
    # fits the sentence. Dataclass default False keeps the pure planner tests on
    # the authored shapes; from_env defaults ON (EMORI_CUE_RECORDED=0 disables),
    # mirroring how body_yaw ships off-by-default in code and armed on the robot.
    recorded: bool = False
    # Text-derived affect. The cue contract says every reply carries a cue, but in
    # practice long stretches of a live conversation arrive with none -- so without
    # this the robot would read most of the conversation on the talking flow. When no cue arrives, the
    # sentence ITSELF is read: "I am truly so very frightened" is tension whether or
    # not the LLM remembered its bracket. EMORI_CUE_TEXT=0 disables.
    text_affect: bool = True
    # Pollen's conversational recordings on untagged sentences -- a question asks,
    # a "Yes," nods, "I think" looks up -- seeded so they alternate with the talking
    # flow rather than replace it. Needs ``recorded``. EMORI_CUE_DIALOGUE=0 disables.
    dialogue_moves: bool = True

    @classmethod
    def from_env(cls) -> "CueConfig":
        return cls(
            motion=_env_flag("EMORI_CUE_MOTION", True),
            max_s=_env_float("EMORI_CUE_MAX_S", _CUE_MAX_S_DEFAULT, 0.5, _CUE_MAX_S_LIMIT),
            z_mm=R.cue_z_limit_mm(),
            body_yaw=R.cue_body_yaw_enabled(),
            deescalate_at=_env_float("EMORI_CUE_DEESCALATE_AT", _DEESCALATE_AT_DEFAULT, 0.0, 1.0),
            amp=_env_float("EMORI_CUE_AMP", 1.0, 0.5, 2.0),
            talk=_env_flag("EMORI_CUE_TALK", True),
            carry=_env_flag("EMORI_CUE_CARRY", True),
            recorded=_env_flag("EMORI_CUE_RECORDED", True),
            text_affect=_env_flag("EMORI_CUE_TEXT", True),
            dialogue_moves=_env_flag("EMORI_CUE_DIALOGUE", True),
        )


@dataclass(frozen=True)
class CuePlan:
    """One sentence's motion: bounded intents plus the trace fields the driver logs."""

    family: str                      # "" for the talking / legacy fallbacks
    intents: list[MotionIntent]
    budget_s: float
    k: float
    reason: str                      # trace slug, e.g. "tension/low" or "talking/a+"
    # True when this plan IS the voice's motion (a family, carried or authored, or
    # the expressive talking flow): it claims the speaking window so the per-cycle
    # engagement lane yields, and it owes a settle at end of turn. False only for
    # the legacy fallbacks (motion off, talk off, empty text).
    speaking: bool = False
    # Where the family came from: "pokamind" (the authored cue on the wire),
    # "text" (read from the sentence itself), "carried" (inherited from an earlier
    # sentence of the turn), "dialogue" (no family; a Pollen dialogue gesture), or
    # "" (the talking flow / a fallback). The driver stamps it on the relayed
    # envelope so the ROS graph sees the family the robot actually performs.
    source: str = ""
    # The Pollen recording this plan performs, "" when it is all authored motion.
    recorded: str = ""
    # The STRUCTURAL shape this plan performed, as the variety memory names it:
    # "talk:gaze_arc", "tension:v3", "rec:oops1". The scheduler remembers the last
    # few and hands them back on the next sentence (VarietyHint) so the planner can
    # rotate away from them -- which is what keeps a turn from performing one
    # gesture over and over. Purely a selection key; nothing downstream reads it.
    shape: str = ""


@dataclass(frozen=True)
class VarietyHint:
    """What the robot performed on the last few sentences, newest FIRST.

    The planner is pure, so it cannot remember anything itself: the scheduler owns
    the memory and passes it in. Without it, selection is a hash of the sentence
    text, which is uniform in the limit but says nothing about whether THIS
    sentence looks like the one before it -- and a conversation is judged on
    exactly that (four consecutive sentences of one carried family, or half a
    session on one talking shape, reads as a loop).
    """

    shapes: tuple[str, ...] = ()     # e.g. ("talk:flow", "tension:v0", "rec:fear1")
    moves: tuple[str, ...] = ()      # recorded move names, newest first
    families: tuple[str, ...] = ()   # performed families, newest first
    # Every recording played SO FAR THIS SESSION. The rolling `moves` window stops
    # an immediate repeat; this stops a favourite. Live across six sessions only 14
    # of ~50 usable recordings were ever reached and fear1 appeared in five of them,
    # so the library's variety existed on disk and nowhere else.
    used: frozenset[str] = frozenset()


def pick_varied(options, recent, seed: int, used=()):
    """Deterministically pick from ``options``, preferring what is not in ``recent``.

    Rotation starts at ``seed % len`` (so two different sentences with the same
    recent-set still differ), then walks forward to the first option that has not
    been performed recently. When everything has, the LEAST recent wins -- the
    memory degrades to round-robin rather than collapsing back onto a favourite.
    Pure and total: an empty ``options`` returns None.
    """

    opts = [o for o in options if o]
    if not opts:
        return None
    start = int(seed) % len(opts)
    order = [opts[(start + i) % len(opts)] for i in range(len(opts))]
    recent = tuple(recent or ())
    used = frozenset(used or ())
    # Best: never performed this session AND not recent. Then: not recent. Then the
    # least recent of all -- the memory degrades to round-robin, never to a favourite.
    for opt in order:
        if opt not in recent and opt not in used:
            return opt
    for opt in order:
        if opt not in recent:
            return opt
    return max(order, key=lambda o: recent.index(o))


def word_count(text: str) -> int:
    """Count words the way the session's playback estimate does, so our timing matches the voice."""

    if not isinstance(text, str):
        return 0
    trimmed = text.strip()
    if not trimmed:
        return 0
    if _count_chars(trimmed, _CJK_WIDE_RANGES) > len(_LATIN_CHAR_RE.findall(trimmed)):
        return _count_chars(trimmed, _CJK_CHAR_RANGES) + len(_LATIN_WORD_RE.findall(trimmed))
    return len(trimmed.split())


def _count_chars(text: str, ranges: tuple[tuple[int, int], ...]) -> int:
    return sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in ranges))


def sentence_duration_s(text: str) -> float:
    """How long Pokamind will take to speak this sentence, clamped to a sane window."""

    return max(MIN_SENTENCE_S, min(MAX_SENTENCE_S, S_PER_WORD * word_count(text)))


def intensity(valence: float, arousal: float) -> float:
    """Amplitude scalar k in 0.35..1.0, monotonic in affect magnitude.

    ``magnitude`` is the same quantity ``expression.to_expression`` uses, so the
    robot's expressive amplitude and the published Expression confidence move
    together instead of telling two stories.
    """

    magnitude = min(1.0, (float(valence) ** 2 + float(arousal) ** 2) ** 0.5)
    return max(0.5, min(1.0, 0.65 + 0.35 * magnitude))


def plan_cue(
    category: Optional[str],
    *,
    text: str = "",
    body: Optional[str] = None,
    lang: str = "",
    valence: float = 0.0,
    arousal: float = 0.0,
    escalation: float = 0.0,
    engagement_level: int = 0,
    fresh: bool = True,
    seed: int = 0,
    carried: bool = False,
    config: Optional[CueConfig] = None,
    allow_recorded: bool = True,
    variety: Optional[VarietyHint] = None,
    carry_depth: int = 0,
) -> CuePlan:
    """Plan the motion for ONE spoken sentence.

    ``category`` is Pokamind's ``cue_category`` (one of :data:`CUE_FAMILIES`, or
    anything else - unknown values fall back rather than raise). ``body`` is the
    freeform direction, absent on today's wire: ``body=None`` is the shipped path
    and the lexicon reads its tone words when the server sends them.

    ``seed`` is a per-sentence integer (the scheduler hashes the text) that picks
    the plan's mirror side and micro-variant, so two same-family sentences never
    perform the identical shape -- deterministically, keeping this function pure.
    ``carried`` marks a family INHERITED from an earlier sentence of the same turn
    rather than authored on this one: the plan runs a notch softer and says so in
    its slug. ``carry_depth`` is how many sentences in a row have now inherited it;
    past ``_CARRY_DECAY_AT`` the family stops being performed as its own block and
    becomes a TINT on the talking grammar instead (see ``_talking_plan``), because
    the third identical sink in a row stops reading as an emotion and starts
    reading as a loop.

    ``variety`` is what the robot performed on the last few sentences: shapes,
    recordings and families, newest first. Selection rotates away from them, so
    variation is a property of the CONVERSATION rather than of each sentence's
    hash. Omitting it reproduces the original seed-only behaviour exactly.
    """

    cfg = config or CueConfig()
    hint = variety or VarietyHint()
    stripped = text.strip() if isinstance(text, str) else ""
    budget = min(sentence_duration_s(stripped), max(0.2, cfg.max_s))

    if not cfg.motion:
        return _fallback_plan(stripped, budget, "motion_off")
    if not stripped:
        # An empty utterance still means "the AI is speaking": today's 3 beats.
        return _fallback_plan(stripped, budget, "empty_text")

    family = category.strip().lower() if isinstance(category, str) else ""
    source = "pokamind" if family in CUE_FAMILIES else ""
    mods = _lexicon_mods(body)
    if mods.force_family:
        family = mods.force_family
        source = source or "pokamind"
    text_slug = ""
    tmods = _text_mods(stripped) if cfg.text_affect else _Mods()
    if family not in CUE_FAMILIES and not (body or "").strip() and tmods.force_family:
        # Pokamind forwarded no cue for this sentence (most sentences of a live
        # reply): read the sentence itself. The voice that says
        # "I am truly so very frightened" IS frightened, bracket or no bracket.
        family, mods, source, text_slug = tmods.force_family, tmods, "text", tmods.slug
        carried = False
    elif carried and family in CUE_FAMILIES and tmods.force_family \
            and tmods.force_family != family:
        # A carried family is a guess about THIS sentence; its own words are not.
        # "Thank you, that is kind" inside a tense reply performs gratitude, not the
        # turn's leftover tension (the emotion must match what is being said).
        family, mods, source, text_slug = tmods.force_family, tmods, "text", tmods.slug
        carried = False
    elif (family in CUE_FAMILIES and not mods.moves and tmods.force_family == family
            and tmods.moves):
        # Authored (or carried) family with no direction of its own: the sentence's
        # words pick the RECORDING within that family -- a fearful tension line
        # performs fear1, an angry one irritated1, never the other way round.
        mods = replace(mods, moves=tmods.moves, slug=mods.slug or tmods.slug)
    if family not in CUE_FAMILIES and cfg.text_affect and not (body or "").strip():
        # Still nothing: read the sentence's DELIVERY rather than perform a shape
        # with no feeling behind it. Weakest evidence, so it runs last and only
        # replaces the generic path.
        pmods = _punct_mods(stripped)
        if pmods.force_family or pmods.moves:
            family = pmods.force_family or "intimacy"
            mods, source, text_slug = pmods, "delivery", pmods.slug
            carried = False
    if carried and family in CUE_FAMILIES:
        source = "carried"
    if family not in CUE_FAMILIES:
        if not cfg.talk:
            return _fallback_plan(stripped, budget, "uncategorized")
        return _talking_plan(stripped, budget, valence=valence, arousal=arousal,
                             escalation=escalation, fresh=fresh, seed=seed, cfg=cfg,
                             allow_recorded=allow_recorded, hint=hint)

    if carried and carry_depth >= _CARRY_DECAY_AT and cfg.talk:
        # The turn's emotion has now coloured this many sentences in a row. Keep the
        # colour, drop the block: the sentence is performed in the talking grammar
        # TINTED by the family (its amplitude, tempo, antenna posture and the shapes
        # that suit it), which is how a person stays angry across four sentences
        # without repeating one gesture four times.
        plan = _talking_plan(stripped, budget, valence=valence, arousal=arousal,
                             escalation=escalation, fresh=fresh, seed=seed, cfg=cfg,
                             allow_recorded=allow_recorded, hint=hint, tint=family)
        return replace(plan, family=family, source="carried",
                       reason=plan.reason + "/tint:" + family)

    k = intensity(valence, arousal) if fresh else _UNFRESH_K
    k = max(mods.k_floor, k * mods.k_scale)
    if carried:
        k *= _CARRIED_K
    if source == "text":
        k *= _TEXT_K                     # read, not authored: a notch more reserved
    band = _affect_band(valence, arousal) if fresh else "neutral"

    # Safety outranks expression: an agitated learner gets slow, low-amplitude,
    # drooped motion whatever the AI's voice is doing.
    deescalated = float(escalation) >= cfg.deescalate_at
    if deescalated:
        family = _DEESCALATE_MAP.get(family, family)
        k = max(0.0, min(1.0, k * _DEESCALATE_K))
        mods = mods.deescalated()
        cfg = replace(cfg, body_yaw=False)   # no body swing while calming someone down

    # A freshly-AUTHORED (or text-read) cue may perform one of Pollen's own
    # recordings when one fits this sentence -- the recording made for THIS feeling
    # first (mods.moves), the family's generic table after. Carried sentences keep
    # the authored shapes (so a reply mixes the official performance with our
    # variations), a de-escalated moment never plays a flourish at an agitated
    # learner, and the scheduler withholds a second recording right after one
    # (``allow_recorded``) so set-pieces alternate with the talking flow.
    if cfg.recorded and allow_recorded and not carried and not deescalated:
        candidates = _recorded_candidates(family, mods, budget, _RECORDED_OVERRUN)
        durations = dict(candidates)
        name = pick_varied(list(durations), hint.moves, seed >> 3, used=hint.used)
        if name is not None and name in hint.moves:
            # Every recording this feeling owns has just been played. A tone that
            # names its own recordings (frightened -> fear1/scared1) may have only
            # ONE that fits a short sentence, and playing it on every sentence is
            # precisely the loop this is all here to stop -- so perform the family's
            # authored shapes instead. Borrowing the family's generic table is not an
            # option: the tension table is anger-flavoured, and a frightened line
            # performing irritated1 is a worse error than a missing set-piece.
            name = None
        if name is not None:
            dur = durations[name]
            intents = [
                MotionIntent(kind="play_recorded", move=name, duration=dur,
                             reason="cue %s (recorded %s)" % (family, name)),
            ]
            # The set-piece, then the hands keep talking for the rest of the sentence.
            remainder = budget - dur
            if remainder >= 0.9:
                intents += _voice_beats(remainder, amp=min(_AMP_CAP, min(1.0, k) * cfg.amp * 0.8),
                                        seed=seed >> 7, cfg=cfg, hint=hint,
                                        reason="cue %s (talking on)" % family)
            intents.append(R.settle_to_attend(duration=0.3,
                                              body_yaw=(0.0 if cfg.body_yaw else None),
                                              reason="cue %s (settle)" % family))
            slug = "%s/%s" % (family, band)
            if text_slug:
                slug += "/text:" + text_slug
            slug += "/rec-" + name
            return CuePlan(family=family, intents=_mark_speech(intents, family),
                           budget_s=max(budget, dur + 0.3), k=min(1.0, k),
                           reason=slug, speaking=True, source=source, recorded=name,
                           shape="rec:" + name)

    # Which of the family's structural reads to perform: rotate away from the ones
    # the last few sentences used rather than trusting the seed's low bits (two
    # sentences of one turn hashing into the same variant is a coin flip, and live
    # it kept landing heads).
    variant_key = pick_varied(["%s:v%d" % (family, i) for i in range(_FAMILY_VARIANTS)],
                              hint.shapes, seed >> 1)
    ctx = _Ctx(budget=budget, k=min(1.0, k), band=band, cfg=cfg, mods=mods,
               reason="cue " + family,
               sign=(-1.0 if seed & 1 else 1.0),
               variant=int(variant_key.rsplit("v", 1)[1]))
    if mods.suppress_beats:
        # "firm / serious / deliberate": the family's shape yields to one held pose.
        body_intents = [R.neutral_gaze(duration=0.75 * budget, reason=ctx.reason + " (held)")]
        settle_antennas, settle_s = R.ANTENNA_REST, 0.25 * budget
    else:
        body_intents, settle_antennas, settle_s = _BUILDERS[family](ctx)
    if mods.interpolation:
        body_intents = [i.model_copy(update={"interpolation": mods.interpolation}) for i in body_intents]

    intents = _prepend(ctx) + body_intents
    intents.append(R.settle_to_attend(duration=settle_s, antennas=settle_antennas,
                                      body_yaw=(0.0 if cfg.body_yaw else None),
                                      reason=ctx.reason + " (settle)"))
    slug = "%s/%s/v%d%s" % (family, band, ctx.variant, "+" if ctx.sign > 0 else "-")
    if carried:
        slug += "/carried"
    if text_slug:
        slug += "/text:" + text_slug
    elif mods.slug:
        slug += "/" + mods.slug
    if deescalated:
        slug += "/deescalated"
    return CuePlan(family=family,
                   intents=_mark_speech(_quick_attack(_fit_budget(intents, budget),
                                                      family=family), family),
                   budget_s=budget, k=ctx.k, reason=slug, speaking=True, source=source,
                   shape=variant_key)


# --- skill facade ---------------------------------------------------------

# The bounded motion vocabulary /api/skill exposes to the ROS graph.
REPLAY_MOTIONS: tuple[str, ...] = (
    "greeting", "farewell", "nod", "listening", "thinking", "calm_settle",
    "talking", "head_tilt", "antenna_wiggle", "look_around",
)
EXPRESSION_LABELS: tuple[str, ...] = (
    "neutral", "happy", "sad", "angry", "surprised", "scared", "disgusted",
)
# A skill request is not a sentence, so it gets a shorter fixed budget.
SKILL_MAX_S = 2.0
_LABEL_FAMILY = {
    "happy": "levity", "surprised": "levity", "angry": "tension", "scared": "tension",
    "sad": "softening", "disgusted": "softening", "neutral": "",
}


def skill_to_intents(skill: str, params: dict, *, config: Optional[CueConfig] = None) -> list[MotionIntent]:
    """Map a bounded skill request onto repertoire primitives.

    Raises :class:`SkillError` for unknown skills / out-of-range params. Every
    returned plan is duration-capped like a cue plan, because it lands in the same
    single-writer queue.
    """

    cfg = config or CueConfig()
    budget = min(SKILL_MAX_S, max(0.2, cfg.max_s))
    params = params or {}
    name = str(skill or "").strip()

    if name == "set_expression":
        label = str(params.get("label", "")).strip().lower()
        if label not in EXPRESSION_LABELS:
            raise SkillError("out_of_range")
        confidence = _skill_float(params, "confidence", 1.0, 0.0, 1.0)
        family = _LABEL_FAMILY[label]
        if not family:
            return [R.settle_to_attend(duration=min(0.6, budget), reason="skill set_expression neutral")]
        k = max(0.35, min(1.0, 0.55 + 0.45 * confidence))
        ctx = _Ctx(budget=budget, k=k, band="neutral", cfg=cfg, mods=_Mods(),
                   reason="skill set_expression " + label)
        body_intents, settle_antennas, settle_s = _BUILDERS[family](ctx)
        body_intents.append(R.settle_to_attend(duration=settle_s, antennas=settle_antennas,
                                               reason=ctx.reason + " (settle)"))
        return _fit_budget(body_intents, budget)

    if name == "look_at":
        yaw = _skill_float(params, "yaw_deg", 0.0, -30.0, 30.0)
        pitch = _skill_float(params, "pitch_deg", 0.0, -25.0, 25.0)
        duration = _skill_float(params, "duration", 0.6, 0.2, 1.5)
        return [R.gaze_to(yaw, pitch_deg=pitch, duration=duration, reason="skill look_at")]

    if name == "replay_motion":
        motion = str(params.get("name", "")).strip().lower()
        if motion not in REPLAY_MOTIONS:
            raise SkillError("out_of_range")
        strength = _skill_float(params, "intensity", 1.0, 0.0, 1.0)
        return _fit_budget(_replay(motion, strength), budget)

    raise SkillError("bad_skill")


def _replay(motion: str, strength: float) -> list[MotionIntent]:
    """Existing repertoire gestures, amplitude-scaled where the primitive allows it."""

    reason = "skill replay " + motion
    if motion == "greeting":
        return R.greeting_gesture(reason=reason)
    if motion == "farewell":
        return R.farewell_gesture(reason=reason)
    if motion == "listening":
        return R.listening_gesture(reason=reason)
    if motion == "thinking":
        return R.thinking_gesture(reason=reason)
    if motion == "calm_settle":
        return R.calm_settle(reason=reason)
    if motion == "antenna_wiggle":
        return R.antenna_wiggle(reason=reason)
    if motion == "nod":
        return R.nod(depth_deg=6.0 + 12.0 * strength, reason=reason)
    if motion == "head_tilt":
        return [R.head_tilt(angle_deg=8.0 + 14.0 * strength, reason=reason)]
    if motion == "look_around":
        return [R.look_around(0, reason=reason)]
    return R.talking_gesture(beats=max(2, min(8, int(2 + round(6 * strength)))), reason=reason)


# --- internals ------------------------------------------------------------

# With no cycle yet this session we have not seen the learner at all: pick a
# middle amplitude and no affect sub-variant rather than guessing one.
_UNFRESH_K = 0.9


@dataclass(frozen=True)
class _Mods:
    """Modifiers a freeform direction body applies to a recipe."""

    force_family: str = ""
    k_floor: float = 0.0
    k_scale: float = 1.0
    amp_scale: float = 1.0
    roll_scale: float = 1.0
    duration_scale: float = 1.0
    beat_bonus: int = 0
    beat_time_scale: float = 1.0
    beat_cap: int = 0                 # 0 = uncapped
    z_bonus_mm: float = 0.0
    pitch_override: Optional[float] = None
    antennas: Optional[tuple[float, float]] = None
    interpolation: str = ""
    suppress_beats: bool = False
    tremor: bool = False
    prepend: str = ""                 # "" | "nod" | "surprise" | "tilt"
    slug: str = ""
    # Preferred Pollen recordings for this tone, tried BEFORE the family's generic
    # table: "frightened" performs fear1, not irritated1 -- the sub-emotion the
    # direction names is the recording to play. Names as in the emotions library.
    moves: tuple[str, ...] = ()

    def deescalated(self) -> "_Mods":
        """De-escalation drops the body-yaw terms and caps beats at 2."""

        cap = _DEESCALATE_BEAT_CAP if not self.beat_cap else min(self.beat_cap, _DEESCALATE_BEAT_CAP)
        return replace(self, beat_cap=cap, beat_bonus=0)


# 16 keyword groups x 6 families, first match wins on a lowercase substring scan.
# When the server sends `cue_body` (the freeform creative direction after the
# pipe) on a categorized sentence, these groups colour the family's gesture. New groups go at the END so the
# established first-match behaviour of the original eleven never shifts.
_LEXICON: tuple[tuple[tuple[str, ...], _Mods], ...] = (
    (("nervous", "anxious", "tight", "tensing", "bracing", "hesitant", "uneasy",
      "tremb", "shak", "quiver", "jitter", "fear", "frighten", "scared", "afraid"),
     _Mods(force_family="tension", k_floor=0.85, z_bonus_mm=2.0, tremor=True, slug="nervous",
           moves=("fear1", "scared1"))),
    (("laugh", "chuckl", "amused", "grin", "playful", "wry", "dry"),
     _Mods(force_family="levity", beat_bonus=1, roll_scale=1.3, slug="amused",
           moves=("laughing2", "cheerful1", "laughing1"))),
    # "gentle" lives with the WARM (softening) words, not the hushed (intimacy) ones:
    # "[softening| voice dropping to a gentle, reassuring murmur]"
    # performed intimacy/grateful1 -- gratitude for an apology -- because "gentle"
    # matched here first. Hushed keeps only the words that mean a lowered voice.
    # Warm is checked BEFORE hushed: a "gentle, reassuring murmur" is softening.
    (("warm", "kind", "caring", "reassur", "comfort", "sooth", "gentle", "tender"),
     _Mods(force_family="softening", antennas=(-0.15, 0.15), interpolation="minjerk", slug="warm",
           moves=("understanding1", "calming1", "understanding2"))),
    (("whisper", "hush", "quiet", "soft", "low", "confid", "murmur", "private"),
     _Mods(force_family="intimacy", amp_scale=0.6, duration_scale=1.3, slug="hushed",
           moves=("understanding2",))),
    (("excit", "bright", "eager", "energ", "upbeat", "quick"),
     _Mods(force_family="energy", beat_bonus=1, beat_time_scale=0.85, slug="eager",
           moves=("enthusiastic1", "enthusiastic2", "success2"))),
    (("firm", "serious", "grave", "stern", "steady", "deliberate"),
     _Mods(suppress_beats=True, slug="firm")),
    (("sad", "heavy", "weary", "deflat", "resign", "tired", "drain", "exhaust",
      "tearful", "choked"),
     _Mods(force_family="softening", antennas=R.ANTENNA_DROOPED, pitch_override=10.0,
           duration_scale=1.2, slug="heavy", moves=("downcast1", "resigned1", "yes_sad1"))),
    (("relie", "exhal", "sigh", "releas", "unwind"),
     _Mods(force_family="release", slug="relief", moves=("relief1", "relief2", "serenity1"))),
    (("curious", "question", "wonder", "puzzl", "intrigu"),
     _Mods(prepend="tilt", slug="curious", moves=("inquiring2", "inquiring1", "inquiring3"))),
    (("emphat", "pointed", "insist", "press", "urgent"),
     _Mods(prepend="nod", slug="emphatic", moves=("yes1", "proud2"))),
    (("surpris", "startl", "sudden"),
     _Mods(prepend="surprise", slug="surprised", moves=("surprised1", "surprised2", "amazed1"))),
    # --- five more tone groups for the live wire ---
    (("cold", "icy", "distant", "flat"),
     _Mods(force_family="tension", amp_scale=0.8, duration_scale=1.2, slug="cold",
           moves=("contempt1", "indifferent1", "displeased2"))),
    (("proud", "assured", "certain", "triumph"),
     _Mods(force_family="energy", beat_time_scale=1.25, k_floor=0.7, slug="assured",
           moves=("proud2", "proud1", "proud3"))),
    (("hope", "lighter", "lifting"),
     _Mods(force_family="release", antennas=(0.5, -0.5), slug="hopeful",
           moves=("helpful1", "serenity1"))),
    (("teas", "mischie", "sly", "impish"),
     _Mods(force_family="levity", roll_scale=1.5, beat_bonus=1, slug="sly",
           moves=("laughing2", "cheerful1"))),
    (("apolog", "sorry", "contrite", "regret"),
     _Mods(force_family="softening", pitch_override=11.0, duration_scale=1.2, slug="contrite",
           moves=("oops1", "oops2", "uncomfortable1"))),
    # --- anger / irritation directions perform the recordings made for them.
    # Only the words that NAME anger: "clipped"/"sharp"/"tight" are tone-of-voice words
    # the cue vocabulary lists under tension and must not turn an apology into
    # irritated1 (e.g. "[softening| ...] Oh, I am so dreadfully sorry"
    # performed the angry recording because its direction mentioned a sharp tone).
    (("angry", "anger", "irritat", "annoy", "furious", "frustrat", "exasperat", "outrag"),
     _Mods(force_family="tension", k_floor=0.8, slug="irritated",
           moves=("irritated1", "displeased2", "displeased1", "frustrated1"))),
)

# --- the sentence itself, when Pokamind forwards no cue --------------------------
# Lowercase, accent-stripped substring scan of the SPOKEN sentence, EN/ES/CA (the
# kiosk languages), first match wins, specific phrases before single words. Each
# group names the family, the Pollen recordings that perform exactly that feeling,
# and a slug so the on-robot trace reads "tension/neutral/text:fear/rec-fear1".
_TEXT_K = 0.9
_TEXT_LEXICON: tuple[tuple[tuple[str, ...], _Mods], ...] = (
    # fear / anxiety
    (("frighten", "scared", "afraid", "terrif", "fear", "panic", "anxious", "worried",
      "nervous", "trembl", "shaking", "dread", "miedo", "asustad", "aterr", "panico",
      "ansios", "preocupad", "nervios", "espant", "tinc por", "fa por", "quina por",
      "atemor", "preocupat", "nervi"),
     _Mods(force_family="tension", k_floor=0.85, z_bonus_mm=2.0, tremor=True, slug="fear",
           moves=("fear1", "scared1"))),
    # anger / indignation
    (("angry", "furious", "outrage", "unacceptable", "how dare", "ridiculous", "annoy",
      "irritat", "frustrat", "disrespect", "unfair", "enfad", "furios", "indign",
      "inacept", "inaccept", "molest", "frustrad", "injust", "ridicul", "vergonz", "vergony"),
     _Mods(force_family="tension", k_floor=0.8, slug="anger",
           moves=("irritated1", "displeased2", "displeased1", "frustrated1", "furious1"))),
    # confusion / lost
    (("confus", "don't understand", "do not understand", "no entiendo", "no entenc",
      "i am lost", "i'm lost", "what do you mean", "que quieres decir", "que vols dir",
      "don't know where", "do not know where", "no se donde", "no se on", "where do i go",
      "where should i go", "perdid"),
     _Mods(force_family="tension", amp_scale=0.9, slug="confused",
           moves=("incomprehensible2", "uncertain1", "confused1", "lost1"))),
    # pain / illness
    (("unwell", "sick", "in pain", "it hurts", "hurts", "dying", "me duele", "dolor",
      "enferm", "em fa mal", "malalt", "mareo", "mareig", "feel faint"),
     _Mods(force_family="tension", k_floor=0.75, slug="pain",
           moves=("uncomfortable1", "downcast1"))),
    # urgency
    (("emergency", "urgent", "hurry", "quickly", "right now", "immediately", "urgen",
      "rapido", "ahora mismo", "de pressa", "ara mateix", "de inmediato"),
     _Mods(force_family="energy", beat_time_scale=0.85, slug="urgent",
           moves=("impatient1", "impatient2"))),
    # apology / contrition
    (("sorry", "apolog", "forgive", "my fault", "excuse me", "lo siento", "perdon",
      "disculp", "ho sento", "perdo", "em sap greu"),
     _Mods(force_family="softening", pitch_override=11.0, duration_scale=1.2, slug="sorry",
           moves=("oops1", "oops2", "uncomfortable1"))),
    # sadness / exhaustion
    (("sad", "lonely", "crying", "tears", "heartbroken", "exhaust", "so tired",
      "strength gives", "can't go on", "cannot go on", "triste", "llorand", "cansad",
      "agotad", "trist", "ploran", "cansat", "esgotat"),
     _Mods(force_family="softening", antennas=R.ANTENNA_DROOPED, pitch_override=10.0,
           duration_scale=1.2, slug="sad", moves=("downcast1", "resigned1", "yes_sad1"))),
    # gratitude / affection
    (("thank", "grateful", "appreciate", "kind of you", "gracias", "agradez", "agradec",
      "muy amable", "gracies", "agraeix", "molt amable"),
     _Mods(force_family="intimacy", slug="thanks", moves=("grateful1", "loving1", "helpful2"))),
    # reassurance / empathy offered
    (("don't worry", "do not worry", "it's okay", "it is okay", "i understand",
      "of course", "take your time", "no te preocupes", "no et preocupis", "tranquil",
      "entiendo", "entenc", "es clar", "por supuesto", "per descomptat", "t'entenc", "te entiendo"),
     _Mods(force_family="softening", antennas=(-0.15, 0.15), interpolation="minjerk",
           slug="reassure", moves=("understanding1", "understanding2", "calming1"))),
    # joy / amusement
    (("haha", "ha ha", "funny", "joke", "laugh", "kidding", "silly", "wonderful",
      "great news", "happy", "glad", "delighted", "jaja", "gracios", "broma", "feliz",
      "content", "encantad", "divertit", "riure", "felic"),
     _Mods(force_family="levity", beat_bonus=1, roll_scale=1.3, slug="joy",
           moves=("cheerful1", "laughing2", "laughing1"))),
    # excitement / celebration
    (("amazing", "incredible", "wow", "fantastic", "excited", "can't wait", "let's go",
      "come on", "absolutely", "increible", "genial", "fantastic", "emocion", "vamos",
      "venga", "som-hi", "vinga", "perfecte", "perfecto"),
     _Mods(force_family="energy", beat_bonus=1, beat_time_scale=0.85, slug="excited",
           moves=("enthusiastic1", "enthusiastic2", "success2", "amazed1", "electric1"))),
    # surprise
    (("really?", "no way", "seriously", "en serio", "de verdad", "de debo", "de veritat"),
     _Mods(prepend="surprise", force_family="energy", slug="surprise",
           moves=("surprised1", "surprised2", "amazed1"))),
    # relief / calm
    (("relief", "relieved", "finally", "at last", "phew", "thank goodness", "better now",
      "it's over", "calm down", "alivio", "por fin", "menos mal", "ya esta", "alleuj",
      "per fi", "menys mal", "ja esta"),
     _Mods(force_family="release", slug="relief", moves=("relief1", "relief2", "serenity1"))),
    # hope
    (("i hope", "hopefully", "i wish", "espero", "ojala", "tant de bo"),
     _Mods(force_family="release", antennas=(0.5, -0.5), slug="hope",
           moves=("helpful1", "serenity1"))),
)

# --- how the sentence is DELIVERED, when its words name no feeling ------------
# A roleplay line is rarely emotionally blank -- "Just tell me what paperwork you
# need to process this" carries insistence, and "Is it a big problem for me to stay
# here?" carries worry -- but neither contains a word the lexicon above knows. In live
# sessions 10-25% of the sentences reached the plain talking grammar this way and performed a shape with no feeling behind it at all.
#
# So when the words name nothing, read how the sentence is BUILT. This is weaker
# evidence than a word, and it is treated as such: it sits below the wire cue, below
# the direction body, below the word lexicon and below the turn's carried emotion --
# it only ever replaces "generic". The families are the mild ones on purpose; a
# question mark is not licence to perform fear.
_PUNCT_MIN_WORDS = 3
_PUNCT_LEXICON: tuple[tuple[str, _Mods], ...] = (
    # A question: leaning in to ask. Pollen's inquiring set is exactly this gesture.
    ("?", _Mods(prepend="tilt", k_scale=0.9, slug="asking",
                moves=("inquiring2", "inquiring1", "inquiring3", "curious1"))),
    # An exclamation: the line is being pressed, not merely said.
    ("!", _Mods(force_family="energy", beat_time_scale=0.9, k_floor=0.7, slug="pressing",
                moves=("enthusiastic1", "electric1", "proud2", "success2"))),
    # Trailing off: the character is hesitating, not finishing.
    ("...", _Mods(force_family="intimacy", amp_scale=0.75, duration_scale=1.25,
                  slug="trailing", moves=("uncertain1", "thoughtful2", "shy1"))),
)


def _punct_mods(text: str) -> _Mods:
    """The sentence's delivery, when its words name no feeling. Never for fragments."""

    low = _normalize_text(text)
    if word_count(low) < _PUNCT_MIN_WORDS:
        return _Mods()
    # Checked in order of how much they change the reading; the ellipsis first,
    # because "So... what happens now?" is hesitant before it is a question.
    for mark, mods in (_PUNCT_LEXICON[2], _PUNCT_LEXICON[0], _PUNCT_LEXICON[1]):
        if mark == "..." and ("..." in text or "…" in text):
            return mods
        if mark != "..." and text.rstrip().endswith(mark):
            return mods
    return _Mods()


# Dialogue acts an untagged sentence can PERFORM with one of Pollen's conversational
# recordings (the library's own intended use: inquiring = "ask", understanding = "I
# see", yes1/no1, thoughtful = "let me think", helpful = "I can help", come1 = "this
# way"). Checked in order; the first rule that matches the (lowercase, accent-stripped)
# sentence wins. The question mark rule is last so a "Yes, but where?" still nods.
_DIALOGUE_ACTS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("help", ("let me help", "i can help", "i will help", "i'll help", "te ayudo",
              "puedo ayudar", "t'ajudo", "puc ajudar", "deja que te ayude", "deixa que t'ajudi"),
     ("helpful1", "helpful2")),
    ("come", ("come with me", "follow me", "this way", "over here", "ven conmigo",
              "sigueme", "por aqui", "vine amb mi", "segueix-me", "per aqui"),
     ("come1",)),
    ("think", ("i think", "i suppose", "maybe", "perhaps", "i guess", "let me think",
               "creo que", "supongo", "quiza", "tal vez", "dejame pensar", "crec que",
               "suposo", "potser", "deixa'm pensar"),
     ("thoughtful2", "thoughtful1", "uncertain1")),
    ("ack", ("i see", "i understand", "right,", "okay,", "ok,", "entiendo", "vale,",
             "entenc", "d'acord"),
     ("understanding1", "understanding2")),
    ("yes", ("yes", "si,", "si.", "si!", "vale", "exactly", "of course", "claro", "es clar",
             "d'acord"),
     ("yes1", "understanding2", "proud2")),
    ("no", ("no,", "no.", "no!", "never", "nunca", "mai", "no puedo", "no puc", "not possible"),
     ("no1", "no_excited1", "no_sad1")),
    # "ask" is not here: a question is always a question, so the delivery read (_PUNCT_LEXICON) owns it now: ungated, and it still performs a
    # curious shape on the sentences where no inquiring recording fits.
)
# A dialogue move may overrun its sentence more than a family move (1.35x): the player
# now cuts a recording the moment the voice stops, so the tolerance buys the tail of
# the gesture on a long sentence and never a runaway on a short one.
_DIALOGUE_OVERRUN = 1.5

# Measured lengths (s) of the recordings this module may name, from the dataset's
# own time[] arrays (the Hugging Face dataset cache on the robot). A name missing here is never chosen.
RECORDED_DURATIONS: dict[str, float] = {
    "amazed1": 3.4, "anxiety1": 8.1, "attentive1": 4.3, "attentive2": 6.5, "calming1": 6.1,
    "cheerful1": 2.8, "come1": 3.2, "confused1": 7.9, "contempt1": 3.6, "displeased1": 3.4,
    "displeased2": 2.9, "downcast1": 5.9, "electric1": 3.5, "enthusiastic1": 2.7,
    "enthusiastic2": 3.4, "exhausted1": 18.3, "fear1": 3.5, "frustrated1": 6.0, "furious1": 5.7,
    "grateful1": 2.5, "helpful1": 4.4, "helpful2": 3.8, "impatient1": 4.0, "impatient2": 3.9,
    "incomprehensible2": 3.4, "indifferent1": 2.6, "inquiring1": 2.1, "inquiring2": 2.6,
    "inquiring3": 2.9, "irritated1": 2.5, "irritated2": 5.3, "laughing1": 4.6, "laughing2": 2.9,
    "lost1": 8.1, "loving1": 5.6, "no1": 2.7, "no_excited1": 4.5, "no_sad1": 7.0, "oops1": 2.5,
    "oops2": 2.7, "proud1": 3.8, "proud2": 3.2, "proud3": 3.4, "relief1": 5.0, "relief2": 6.9,
    "resigned1": 4.7, "scared1": 7.2, "serenity1": 4.6, "success1": 2.3, "success2": 2.4,
    "surprised1": 2.5, "surprised2": 3.0, "thoughtful1": 5.9, "thoughtful2": 5.5, "tired1": 7.4,
    "uncertain1": 6.1, "uncomfortable1": 6.0, "understanding1": 3.9, "understanding2": 2.6,
    "welcoming1": 3.5, "welcoming2": 4.3, "yes1": 3.4, "yes_sad1": 5.1,
    # The rest of the library, measured the same way. Sleep, dance,
    # boredom and the other >10 s set-pieces are deliberately absent -- nothing
    # that long can be cut cleanly at the end of a spoken sentence.
    "curious1": 11.8, "disgusted1": 6.9, "go_away1": 4.8, "lonely1": 10.2, "rage1": 5.1,
    "reprimand1": 4.7, "reprimand3": 4.3, "sad1": 9.0, "sad2": 7.3, "shy1": 7.8,
}


@dataclass
class _Ctx:
    """Everything a family builder needs once affect and the lexicon are resolved."""

    budget: float
    k: float
    band: str
    cfg: CueConfig
    mods: _Mods
    reason: str
    sign: float = 1.0      # seed-derived mirror side: flips the asymmetric terms
    variant: int = 0       # seed-derived micro-variant: nudges tempo / depth

    def amp(self, k: Optional[float] = None) -> float:
        return min(_AMP_CAP, (self.k if k is None else k) * self.mods.amp_scale * self.cfg.amp)

    def beats(self, slot: float, lo: int, hi: int) -> int:
        n = max(lo, min(hi, int(math.floor(self.budget / slot + 0.5))))
        return max(1, n)

    def tempo(self) -> float:
        """Beat-time scale from the micro-variant: one side brisker, one lazier."""

        return 0.9 if self.variant & 1 == 0 else 1.1


def _affect_band(valence: float, arousal: float) -> str:
    """Which learner the AI's voice is talking to: low / neutral / up."""

    if valence < -0.25:
        return "low"
    if valence > 0.35 and arousal > 0.0:
        return "up"
    return "neutral"


def _scaled(pair: tuple[float, float], amp: float) -> tuple[float, float]:
    return (pair[0] * amp, pair[1] * amp)


def _fallback_plan(text: str, budget: float, reason: str) -> CuePlan:
    """The pre-cue behaviour, byte-for-byte: driver.on_utterance's talking gesture."""

    beats = max(2, min(8, len(text) // 25)) if text else 3
    return CuePlan(family="", intents=R.talking_gesture(beats=beats, reason=TALKING_REASON),
                   budget_s=budget, k=1.0, reason=reason, speaking=False)


def _talking_plan(text: str, budget: float, *, valence: float, arousal: float,
                  escalation: float, fresh: bool, seed: int, cfg: CueConfig,
                  allow_recorded: bool = True, hint: Optional[VarietyHint] = None,
                  tint: str = "") -> CuePlan:
    """Full-DOF conversational gesticulation for a sentence with no family.

    This is what most sentences of a live session get (Pokamind authors ONE cue
    per multi-sentence reply), so it is where "the robot moves while it speaks"
    actually lives. Seeded mirror + variant keep consecutive sentences from
    looping the same shape; the de-escalation policy applies here exactly as it
    does to the families, because most of a calming reply is untagged sentences.

    Two things make it the voice's motion rather than a metronome beside it:
    every beat is ``pace="voice"`` + ``antenna_voice`` (the player
    lands it on the next syllable onset and scales the antennas to the voice
    level), and an untagged sentence that IS a dialogue act -- a question, a
    "Yes,", an "I think", "let me help" -- performs one of Pollen's conversational
    recordings first, seeded so it alternates with the plain flow.
    """

    hint = hint or VarietyHint()
    k = intensity(valence, arousal) if fresh else _UNFRESH_K
    amp = min(_AMP_CAP, k * cfg.amp)
    tempo = 1.0
    emph_amp, emph_tempo, emph_shapes = _emphasis(text)
    amp = min(_AMP_CAP, amp * emph_amp)
    tempo *= emph_tempo
    if tint in _TINTS:
        # The turn's emotion, carried as a COLOUR rather than replayed as a block.
        amp = min(_AMP_CAP, amp * _TINTS[tint][1])
        tempo = _TINTS[tint][2]
    body_yaw_on = cfg.body_yaw
    deescalated = float(escalation) >= cfg.deescalate_at
    if deescalated:
        amp = min(amp, 0.5)
        tempo = max(tempo, 1.15)
        body_yaw_on = False
    sign = -1.0 if seed & 1 else 1.0
    variant = (seed >> 1) & 3
    phase = (seed >> 3) % 8

    if (cfg.recorded and cfg.dialogue_moves and allow_recorded and not deescalated
            and (seed >> 5) & 1 == 0):
        act = _dialogue_act(text)
        if act is not None:
            act_slug, moves = act
            fitting = {m: RECORDED_DURATIONS[m] for m in moves
                       if m in RECORDED_DURATIONS
                       and RECORDED_DURATIONS[m] <= budget * _DIALOGUE_OVERRUN}
            if fitting:
                name = pick_varied(list(fitting), hint.moves, seed >> 4, used=hint.used)
                dur = fitting[name]
                intents = [MotionIntent(kind="play_recorded", move=name, duration=dur,
                                        reason="%s (recorded %s)" % (TALKING_REASON, name))]
                remainder = budget - dur
                if remainder >= 0.9:
                    intents += _voice_beats(remainder, amp=amp * 0.8, seed=seed >> 7, cfg=cfg,
                                            reason=TALKING_REASON, hint=hint)
                intents.append(R.settle_to_attend(
                    duration=0.3, body_yaw=(0.0 if body_yaw_on else None),
                    reason=TALKING_REASON + " (settle)"))
                return CuePlan(family="", intents=_mark_speech(intents, tint),
                               budget_s=max(budget, dur + 0.3), k=amp,
                               reason="talking/%s/rec-%s" % (act_slug, name),
                               speaking=True, source="dialogue", recorded=name,
                               shape="rec:" + name)

    beats = max(3, min(8, int(math.floor(budget / 0.36 + 0.5))))
    if deescalated:
        beats = min(beats, 3)
    # WHICH shape: the sentence's own form proposes a shortlist (a question rises,
    # an exclamation punches, an enumeration steps down, a short line sways), a tint
    # narrows it to the shapes that suit the turn's emotion, and the variety memory
    # picks the one the robot has not just performed.
    candidates = _shape_candidates(text, tint)
    if emph_shapes and not tint:
        # The sentence's own force outranks its punctuation for shape choice: an
        # insistent line leans in whatever mark it ends with.
        ordered = tuple(s for s in emph_shapes if s in candidates)
        candidates = (ordered + tuple(c for c in candidates if c not in ordered)) or candidates
    shape = pick_varied(["talk:" + s for s in candidates],
                        hint.shapes, seed >> 8) or "talk:flow"
    shape = shape.split(":", 1)[1]
    if deescalated and shape in ("punch", "list_three", "lean_press"):
        shape = "sway"                    # never strike a beat at an agitated learner
    intents = R.talking_shape(shape, beats=beats, beat_s=0.34 * tempo, amp=amp, sign=sign,
                              variant=variant, phase=phase, z_limit_mm=cfg.z_mm,
                              body_yaw_enabled=body_yaw_on, reason=TALKING_REASON)
    intents = [i.model_copy(update={"pace": "voice", "antenna_voice": True}) for i in intents]
    intents.append(R.settle_to_attend(duration=0.3, body_yaw=(0.0 if body_yaw_on else None),
                                      reason=TALKING_REASON + " (settle)"))
    slug = "talking/%s/%s%s%d" % (shape, "abc"[variant % 3], "+" if sign > 0 else "-", phase)
    if deescalated:
        slug += "/deescalated"
    return CuePlan(family="", intents=_mark_speech(_quick_attack(_fit_budget(intents, budget)),
                                                   tint),
                   budget_s=budget, k=amp, reason=slug, speaking=True,
                   shape="talk:" + shape)


def _voice_beats(seconds: float, *, amp: float, seed: int, cfg: CueConfig,
                 reason: str, hint: Optional[VarietyHint] = None) -> list[MotionIntent]:
    """Voice-paced talking beats for the tail of a sentence after a recording.

    Takes a shape from the same grammar the untagged sentences use (rotating away
    from the recent ones), so the hands that keep talking after a set-piece are not
    always the same hands.
    """

    hint = hint or VarietyHint()
    beats = max(2, min(6, int(math.floor(seconds / 0.36 + 0.5))))
    shape = pick_varied(["talk:" + s for s in _TAIL_SHAPES], hint.shapes, seed) or "talk:flow"
    out = R.talking_shape(shape.split(":", 1)[1], beats=beats, beat_s=0.34,
                          amp=min(_AMP_CAP, max(0.3, amp)),
                          sign=(-1.0 if seed & 1 else 1.0), variant=(seed >> 1) & 3,
                          phase=(seed >> 3) % 8, z_limit_mm=cfg.z_mm,
                          body_yaw_enabled=cfg.body_yaw, reason=reason)
    return [i.model_copy(update={"pace": "voice", "antenna_voice": True}) for i in out]


# The shapes that read well as the TAIL of a sentence whose set-piece already
# played: continuations, not new statements (no punch, no list, no question rise).
_TAIL_SHAPES: tuple[str, ...] = ("flow", "sway", "nod_run", "head_cock", "gaze_arc")

# How many sentences in a row may perform a carried family as its own block before
# it decays into a tint on the talking grammar. Two is the measured limit: a turn
# that ran four consecutive `tension/.../carried` sinks and read as a
# robot on a loop, and the third was already indistinguishable from the second.
_CARRY_DECAY_AT = 2

# How many structural reads each family builder offers (v0..v4).
_FAMILY_VARIANTS = 5

# A family carried as a TINT: (shapes it suits, amplitude scale, tempo scale). The
# emotion survives in how the talking grammar is performed -- how big, how fast,
# and which shapes are even eligible -- instead of replaying the family's block.
_TINTS: dict[str, tuple[tuple[str, ...], float, float]] = {
    "tension": (("head_cock", "punch", "sway", "lean_press"), 0.95, 0.9),
    "levity": (("flow", "nod_run", "punch", "list_three"), 1.1, 0.9),
    "energy": (("lean_press", "flow", "punch", "nod_run"), 1.15, 0.85),
    "intimacy": (("sway", "lean_press", "head_cock"), 0.7, 1.25),
    "softening": (("nod_run", "sway", "head_cock", "gaze_arc"), 0.8, 1.15),
    "release": (("rise", "sway", "gaze_arc", "flow"), 0.9, 1.1),
}


def _shape_candidates(text: str, tint: str = "") -> tuple[str, ...]:
    """The structural shapes that suit THIS sentence, best first.

    Read off the sentence's own form, which is the cheapest honest signal we have
    about how it is going to be said: a question rises, an exclamation strikes once,
    an enumeration steps down, a five-word line barely moves, a long clause wanders
    and comes back. A tint (a carried emotion) intersects the shortlist with the
    shapes that emotion can wear; if the intersection is empty the tint's own list
    wins, because the emotion matters more than the punctuation.
    """

    t = (text or "").strip()
    low = _normalize_text(t)
    words = word_count(t)
    if "?" in t:
        base = ("rise", "head_cock", "gaze_arc", "flow", "sway")
    elif "!" in t or (words <= 8 and t.isupper()):
        base = ("punch", "nod_run", "lean_press", "flow")
    elif t.count(",") >= 2 or " and then " in low or "; " in t:
        base = ("list_three", "flow", "lean_press", "gaze_arc")
    elif words <= 5:
        base = ("sway", "punch", "head_cock", "nod_run")
    elif words >= 16:
        base = ("gaze_arc", "flow", "lean_press", "nod_run", "head_cock")
    else:
        base = ("flow", "nod_run", "lean_press", "gaze_arc", "head_cock", "sway", "list_three")
    if tint in _TINTS:
        allowed = _TINTS[tint][0]
        both = tuple(s for s in base if s in allowed)
        return both or allowed
    return base


def _mark_speech(intents: list[MotionIntent], tint: str = "") -> list[MotionIntent]:
    """Every intent of a cue plan performs the voice: the player locks it to the voice."""

    return [i.model_copy(update={"speech": True, "tint": tint}) for i in intents]


# How far a family's beats spread the antennas, past the end of its plan.
_SUSTAIN_ANTENNA_SCALE: dict[str, float] = {"tension": 0.6, "levity": 1.1, "energy": 1.15,
                                            "intimacy": 0.4, "softening": 0.5, "release": 0.7}


def sustain_for(family: str, seq: int) -> MotionIntent:
    """One beat for a sentence that has outlasted its plan, in that plan's family.

    Its amplitude and tempo are the ones the family wears as a carried tint, so the rest of a
    long sentence stays in the emotion it started in.
    """

    if family not in _TINTS:
        return R.sustain_beat(seq)
    _, amp, tempo = _TINTS[family]
    return R.sustain_beat(seq, reason="speaking (sustain %s)" % family, amp=amp, tempo=tempo,
                          antenna_scale=_SUSTAIN_ANTENNA_SCALE[family]).model_copy(
        update={"tint": family})


# The attend pose every cue plan starts from (settle_to_attend) -- the reference for
# the quick attack below.
_ATTACK_S = 0.3
_ATTACK_FRAC = 0.4
_ATTACK_MIN_DURATION_S = 0.8
# A small move at its usual length takes too long to show at all: linear, it reaches 1 degree
# after duration / amplitude. So it is made as short as this many seconds per degree, and no
# shorter than the floor, which is higher where a snap would be out of character.
_ATTACK_S_PER_DEG = 0.055
_ATTACK_FLOOR_S = 0.15
_ATTACK_SOFT_FLOOR_S = 0.22
_SOFT_FAMILIES = frozenset({"softening", "intimacy"})


def _quick_attack(intents: list[MotionIntent], *, family: str = "") -> list[MotionIntent]:
    """Make the plan's first move VISIBLE within the first 100 ms of the voice.

    Min-jerk (and ease-in-out) interpolation starts at zero velocity: a 2.2 s sink
    has moved under 1 % of its distance after 200 ms, so a plan that is issued the
    instant the voice starts still LOOKS like "speech, then movement". When the first goto is long, it is split into a 0.3 s linear
    attack to 40 % of the way (immediately visible) followed by the remainder of the
    original move to the full pose -- same destination, same total time, no new
    shape, just an onset that coincides with the voice.
    """

    if not intents:
        return intents
    first = intents[0]
    if first.kind != "goto" or first.head is None:
        return intents
    if first.duration < _ATTACK_MIN_DURATION_S:
        # Too short to split, and an ease-in spends its first quarter barely moving.
        interpolation = "linear" if first.interpolation == "ease_in_out" else first.interpolation
        amplitude = max(abs(first.head.pitch - 2.0), abs(first.head.roll), abs(first.head.yaw))
        floor = _ATTACK_SOFT_FLOOR_S if family in _SOFT_FAMILIES else _ATTACK_FLOOR_S
        duration = first.duration
        if interpolation == "linear" and amplitude >= 1.0:
            duration = min(duration, max(floor, amplitude * _ATTACK_S_PER_DEG))
        return [first.model_copy(update={"interpolation": interpolation, "duration": duration})
                ] + list(intents[1:])
    start = HeadPose(pitch=2.0)     # settle_to_attend's pose
    target = first.head

    def lerp(a: float, b: float) -> float:
        return a + (b - a) * _ATTACK_FRAC

    mid_head = HeadPose(x=lerp(start.x, target.x), y=lerp(start.y, target.y),
                        z=lerp(start.z, target.z), roll=lerp(start.roll, target.roll),
                        pitch=lerp(start.pitch, target.pitch), yaw=lerp(start.yaw, target.yaw))
    attack = first.model_copy(update={"head": mid_head, "duration": _ATTACK_S,
                                      "interpolation": "linear",
                                      "reason": first.reason + " (attack)"})
    rest = first.model_copy(update={"duration": max(0.15, first.duration - _ATTACK_S)})
    return [attack, rest] + list(intents[1:])


def _normalize_text(text: str) -> str:
    """Lowercase, accent-stripped, single-spaced -- the form the text lexicon scans."""

    import unicodedata

    low = unicodedata.normalize("NFKD", str(text or "").lower())
    low = "".join(ch for ch in low if not unicodedata.combining(ch))
    return " ".join(low.replace("’", "'").split())


def _text_mods(text: str) -> _Mods:
    """The sentence's own feeling, when Pokamind forwarded no cue for it."""

    low = _normalize_text(text)
    if not low:
        return _Mods()
    padded = " " + low + " "
    for keywords, mods in _TEXT_LEXICON:
        for word in keywords:
            needle = _normalize_text(word)
            if not needle:
                continue
            # Phrases and punctuation marks match anywhere; single words must start
            # at a word boundary so "sad" never fires on "saddle" but does on "sadly".
            if " " in needle or not needle[-1].isalnum():
                if needle in padded:
                    return mods
            elif (" " + needle) in padded:
                return mods
    return _Mods()


# Intensity, for the sentences that name no feeling and end in a full stop -- the
# largest remaining slice of a roleplay. This claims no emotion:
# it reads how hard the line is being PUSHED, which is a property of the words
# themselves ("I cannot afford to lose more pay" is not delivered like "I moved here
# last week"), and turns it into amplitude, tempo and which shapes are eligible. A
# sentence performed with the wrong force still reads as a person; a sentence
# performed with no force reads as a machine.
_INSISTENT = ("really", "absolutely", "completely", "never", "always", "must",
              "need to", "have to", "cannot", "can't", "will not", "won't", "every",
              "right now", "immediately", "no way", "de verdad", "nunca", "siempre",
              "tengo que", "no puedo", "ara mateix", "mai", "sempre", "haig de",
              "no puc", "de veritat")
_IMPERATIVE = ("just tell me", "tell me", "please", "look,", "listen", "let me",
               "give me", "show me", "dime", "escucha", "por favor", "digue'm",
               "escolta", "si us plau")
_HEDGED = ("maybe", "perhaps", "i think", "i suppose", "i guess", "kind of", "a bit",
           "probably", "i am not sure", "i'm not sure", "quiza", "tal vez", "supongo",
           "creo que", "potser", "suposo", "crec que", "no estoy segur", "no estic segur")


# The scenario BRIEFING -- "As the team lead, you need to
# speak with Sam..." -- is not the character speaking, it is the robot reading the
# learner their situation in the second person. Once the delivery read is
# applied, essentially every remaining feelingless sentence was one of
# these. They should not be performed as emotion (there is none to perform) but they
# should not be gesticulated at either: narration is steady and explanatory.
_BRIEFING = ("you are ", "you will be ", "you'll be ", "you need to ", "as the ",
             "your task", "your goal", "in this scenario", "you must ",
             "eres ", "vas a ", "tu tarea", "en este escenario",
             "ets ", "seras ", "la teva tasca", "en aquest escenari")


def _emphasis(text: str) -> tuple[float, float, tuple[str, ...]]:
    """(amplitude scale, tempo scale, preferred shapes) for an untagged sentence."""

    low = " " + _normalize_text(text) + " "
    if any((" " + _normalize_text(w)).rstrip() + " " in low for w in _BRIEFING):
        # Explaining, not feeling: unhurried, and the shapes that read as narration.
        return 0.8, 1.2, ("gaze_arc", "list_three", "sway")
    if any((" " + _normalize_text(w)) in low for w in _HEDGED):
        # Thinking aloud: smaller, slower, and the shapes that wander.
        return 0.85, 1.15, ("gaze_arc", "sway", "head_cock")
    if any((" " + _normalize_text(w)) in low for w in _IMPERATIVE):
        # Asking for something: forward, and it lands on a beat.
        return 1.1, 0.95, ("lean_press", "punch", "nod_run")
    if any((" " + _normalize_text(w)) in low for w in _INSISTENT):
        return 1.15, 0.92, ("lean_press", "nod_run", "flow", "punch")
    return 1.0, 1.0, ()


def _dialogue_act(text: str) -> Optional[tuple[str, tuple[str, ...]]]:
    """Which Pollen dialogue gesture an untagged sentence can perform, if any."""

    low = _normalize_text(text)
    if not low:
        return None
    padded = " " + low + " "
    for slug, keys, moves in _DIALOGUE_ACTS:
        for key in keys:
            needle = _normalize_text(key)
            if not needle:
                continue
            if slug in ("yes", "no"):
                # Affirmations and refusals are read at the START of the sentence.
                if low.startswith(needle):
                    return slug, moves
            elif needle == "?":
                if "?" in low:
                    return slug, moves
            elif (" " + needle) in padded or needle in padded and " " in needle:
                return slug, moves
    return None


def _recorded_candidates(family: str, mods: _Mods, budget: float,
                         overrun: float) -> list[tuple[str, float]]:
    """Recordings that fit this sentence: the tone's own first, the family's after.

    When any of the tone's preferred recordings fit, ONLY those are offered (a
    frightened line performs fear1, never the family's irritated1); otherwise the
    family table is the pool, exactly as before the lexicon learned to prefer.
    """

    limit = budget * overrun
    if mods.moves:
        # A tone that names its own recordings never borrows the family table: the
        # tension table is anger-flavoured (irritated1, displeased), and a "tight,
        # trembling" direction whose fear1 does not fit a short sentence must perform
        # the authored nervous shape -- not an angry recording.
        return [(m, RECORDED_DURATIONS[m]) for m in mods.moves
                if m in RECORDED_DURATIONS and RECORDED_DURATIONS[m] <= limit]
    return [m for m in RECORDED_FAMILY_MOVES.get(family, ()) if m[1] <= limit]


def _lexicon_mods(body: Optional[str]) -> _Mods:
    if not body:
        return _Mods()
    low = str(body).lower()
    for keywords, mods in _LEXICON:
        if any(word in low for word in keywords):
            return mods
    return _Mods()


def _prepend(c: _Ctx) -> list[MotionIntent]:
    """The lexicon's opening intents (live now that `cue_body` is on the wire)."""

    amp = c.amp()
    if c.mods.prepend == "tilt":
        return [R.head_tilt(angle_deg=14.0 * amp, reason=c.reason + " (curious)")]
    if c.mods.prepend == "nod":
        return R.nod(depth_deg=14.0 * amp, duration=0.22, reason=c.reason + " (emphatic)")
    if c.mods.prepend == "surprise":
        return [MotionIntent(kind="goto", head=HeadPose(pitch=-10.0 * amp,
                                                        z=R.clamp_cue_z(6.0 * amp, limit=c.cfg.z_mm)),
                             antennas=_scaled((1.6, -1.6), min(amp, 1.2)), duration=0.2,
                             interpolation="cartoon", reason=c.reason + " (startle)")]
    return []


def _fit_budget(intents: list[MotionIntent], budget_s: float) -> list[MotionIntent]:
    """Scale a plan so its motion never outlives the sentence it decorates.

    Durations are floored to whole milliseconds so the invariant holds exactly
    rather than to within float noise; RobotPlayer's own ``max(0.12, duration)``
    sleep covers anything that lands very short.
    """

    total = sum(max(0.0, i.duration) for i in intents)
    scale = 1.0 if (total <= budget_s or total <= 0.0) else budget_s / total
    return [i.model_copy(update={"duration": math.floor(max(0.0, i.duration) * scale * 1000.0) / 1000.0})
            for i in intents]


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "True", "yes", "on")


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    clamped = min(high, max(low, value))
    if clamped != value:
        log.warning("%s=%s is outside %s..%s; using %s", name, value, low, high, clamped)
    return clamped


def _skill_float(params: dict, key: str, default: float, low: float, high: float) -> float:
    """Skill params are range-CHECKED, not clamped: a caller out of range is a 400."""

    raw = params.get(key, None)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SkillError("out_of_range") from None
    if not (low <= value <= high) or math.isnan(value):
        raise SkillError("out_of_range")
    return value


# --- the six family recipes (each returns body intents, settle antennas, settle s) ---


def _plan_tension(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Anxious, bracing, "nervous": the head sinks toward the shell and holds."""

    k, antennas = c.k, (-0.85, 0.85)
    if c.band == "low":
        k = max(k, 0.8)                     # deepest sink for a learner already low
        antennas = (-1.2, 1.2)              # the measured droop range, not a hint of it
    elif c.band == "up":
        k *= 0.7                            # read as intrigue rather than dread
        antennas = R.ANTENNA_CURIOUS
    amp = c.amp(k)
    b, d = c.budget, c.mods.duration_scale
    # Four structural reads of the same emotion, so consecutive tense sentences
    # never replay one shape: classic sink, shivering sink, averted-gaze sink,
    # and a slower, deeper brace.
    pitch, sink_s, hold_s, avert = 15.0, 0.55, 0.30, 0.0
    tremor = 2.0 * amp if c.mods.tremor else 0.0
    if c.variant == 1:
        tremor = max(tremor, 1.6 * amp)
    elif c.variant == 2:
        avert = -7.0 * amp * c.sign
    elif c.variant == 3:
        pitch, sink_s, hold_s = 16.0, 0.70, 0.18
    elif c.variant == 4:
        # "Clipped": impatience rather than dread. The head does NOT sink -- it
        # stays up and snaps twice, sharply, off-axis. Tension has two bodies (the
        # one that shrinks and the one that bristles) and only ever performing the
        # first is why four tense sentences in a row looked like one gesture.
        flick = 9.0 * amp
        body = [
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=-2.0 * amp, yaw=c.sign * flick,
                                       roll=-c.sign * 3.0 * amp,
                                       z=R.clamp_cue_z(2.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled((0.9, -0.2), amp),
                         body_yaw=R.cue_body_yaw(c.sign * 0.08 * amp, enabled=c.cfg.body_yaw),
                         duration=0.28 * b * d, interpolation="cartoon", reason=c.reason),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=1.0 * amp, yaw=-c.sign * flick * 0.7,
                                       roll=c.sign * 3.0 * amp),
                         antennas=_scaled((-0.2, 0.9), amp),
                         body_yaw=R.cue_body_yaw(-c.sign * 0.06 * amp, enabled=c.cfg.body_yaw),
                         duration=0.24 * b * d, interpolation="cartoon",
                         reason=c.reason + " (clipped)"),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=4.0 * amp, yaw=c.sign * 2.0 * amp),
                         antennas=_scaled((-0.5, 0.5), amp),
                         duration=0.33 * b * d, interpolation="ease_in_out",
                         reason=c.reason + " (hold)"),
        ]
        return body, _scaled((-0.3, 0.3), amp), 0.15 * b
    body = R.sink_toward_shell(
        pitch_deg=pitch * amp, roll_deg=4.0 * amp * c.sign, yaw_deg=avert,
        z_mm=-(10.0 + c.mods.z_bonus_mm) * amp, z_limit_mm=c.cfg.z_mm,
        antennas=_scaled(c.mods.antennas or antennas, amp),
        sink_s=sink_s * b * d, hold_s=hold_s * b * d,
        tremor_roll_deg=tremor,
        body_yaw_rad=-0.10 * amp * c.sign, body_yaw_enabled=c.cfg.body_yaw,
        reason=c.reason)
    return body, _scaled((-0.25, 0.25), amp), 0.15 * b


def _plan_levity(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Amused, light, a dry laugh: livelier rotation + open antennas."""

    k, n = c.k, c.beats(0.30, 2, 5)
    if c.band == "low":
        k *= 0.6                            # never laugh at a struggling learner
        n = min(n, 2)
    elif c.band == "up":
        n += 1
    amp = c.amp(k)
    n = _beat_count(n, c.mods)
    if c.variant == 2:
        n, amp = n + 2, amp * 0.85           # a giggle: quicker, smaller
    elif c.variant == 3:
        n, amp = max(2, n - 2), min(_AMP_CAP, amp * 1.15)   # a grin: two big beats
    elif c.variant == 4:
        # A head TOSS: the laugh that throws the head back and rolls it, then
        # recovers -- one arc instead of a bounce train.
        b, d = c.budget, c.mods.duration_scale
        body = [
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=-12.0 * amp, roll=c.sign * 9.0 * amp,
                                       yaw=-c.sign * 5.0 * amp,
                                       z=R.clamp_cue_z(5.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled(c.mods.antennas or (1.5, -1.5), min(amp, 1.2)),
                         body_yaw=R.cue_body_yaw(c.sign * 0.14 * amp, enabled=c.cfg.body_yaw),
                         duration=0.30 * b * d, interpolation="cartoon", reason=c.reason),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=-4.0 * amp, roll=-c.sign * 5.0 * amp,
                                       z=R.clamp_cue_z(2.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled((1.0, -1.0), min(amp, 1.2)),
                         body_yaw=R.cue_body_yaw(-c.sign * 0.06 * amp, enabled=c.cfg.body_yaw),
                         duration=0.28 * b * d, interpolation="minjerk",
                         reason=c.reason + " (toss)"),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=2.0 * amp, roll=c.sign * 2.0 * amp),
                         antennas=_scaled((0.8, -0.8), min(amp, 1.2)),
                         duration=0.30 * b * d, interpolation="ease_in_out",
                         reason=c.reason + " (recover)"),
        ]
        return body, R.ANTENNA_PERKED, 0.3
    body = R.levity_bounce(
        beats=n, beat_s=0.22 * c.tempo() * c.mods.beat_time_scale * c.mods.duration_scale / max(0.2, k),
        pitch_up=-10.0 * amp, pitch_down=3.0 * amp, yaw_deg=10.0 * amp * c.sign,
        roll_deg=6.0 * amp * c.mods.roll_scale * c.sign, z_mm=4.0 * amp, z_limit_mm=c.cfg.z_mm,
        body_yaw_rad=0.16 * amp * c.sign, body_yaw_enabled=c.cfg.body_yaw,
        antennas_up=_scaled(c.mods.antennas or (1.3, -0.35), min(amp, 1.2)),
        antennas_down=_scaled(c.mods.antennas or (-0.35, 1.3), min(amp, 1.2)),
        reason=c.reason)
    return body, R.ANTENNA_PERKED, 0.3


def _plan_energy(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Excited, driving, emphatic: talking_gesture with amplitude and a settle."""

    k, n, yaw = c.k, c.beats(0.30, 3, 7), 12.0
    if c.band == "low":
        k *= 0.6                            # energetic but not overwhelming
        yaw *= 0.5
    elif c.band == "up":
        n += 1
    amp = c.amp(k)
    n = _beat_count(n, c.mods)
    pitch_a, z_mm = 12.0, 3.0
    if c.variant == 2:
        z_mm, yaw = 5.0, yaw * 1.2           # the drive rises on every push
    elif c.variant == 3:
        n, pitch_a = max(2, n - 2), 15.0     # punchy: fewer, deeper strikes
    elif c.variant == 4:
        # The drive that RISES: the platform comes up and forward across the
        # sentence with two accents on the way, instead of bobbing in place.
        b, d = c.budget, c.mods.duration_scale
        body = []
        for i in range(3):
            frac = (i + 1) / 3.0
            body.append(MotionIntent(
                kind="goto",
                head=HeadPose(pitch=(4.0 - 11.0 * frac) * amp,
                              yaw=c.sign * (6.0 - 8.0 * frac) * amp,
                              roll=c.sign * 3.0 * frac * amp,
                              z=R.clamp_cue_z(6.0 * frac * amp, limit=c.cfg.z_mm)),
                antennas=_scaled((0.6 + 0.6 * frac, -(0.6 + 0.6 * frac)), min(amp, 1.2)),
                body_yaw=R.cue_body_yaw(c.sign * 0.16 * frac * amp, enabled=c.cfg.body_yaw),
                duration=(0.34 if i < 2 else 0.28) * b * d,
                interpolation="cartoon" if i == 1 else "ease_in_out", reason=c.reason))
        return body, R.ANTENNA_PERKED, 0.3
    body = R.energize_bob(
        beats=n, beat_s=0.24 * c.tempo() * c.mods.beat_time_scale * c.mods.duration_scale / max(0.2, k),
        pitch_a=pitch_a * amp, pitch_b=2.0 * amp, yaw_deg=yaw * amp * c.sign,
        body_yaw_rad=0.20 * amp * c.sign, body_yaw_enabled=c.cfg.body_yaw,
        z_mm=z_mm * amp, z_limit_mm=c.cfg.z_mm,
        antennas_a=_scaled(c.mods.antennas or (1.0, -0.3), min(amp, 1.2)),
        antennas_b=_scaled(c.mods.antennas or (-0.3, 1.0), min(amp, 1.2)),
        reason=c.reason)
    return body, R.ANTENNA_PERKED, 0.3


def _plan_intimacy(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Confiding, close, hushed: lean toward the person, then be still."""

    z, hold = 5.0, 0.25
    if c.band == "low":
        z, hold = 7.0, 0.35                 # extra lean, longer hold
    elif c.band == "up":
        z = 3.0                             # the learner is already open
    amp = c.amp()
    b, d = c.budget, c.mods.duration_scale
    pitch, roll = -4.0, 2.5 * c.sign
    lean_frac = 0.6
    if c.variant == 1:
        pitch, lean_frac = -2.0, 0.8         # a slow, hovering drift closer
    elif c.variant == 2:
        z, hold = z + 2.0, hold * 1.5        # closer, and stiller for longer
    elif c.variant == 3:
        pitch, roll = -6.0, 4.0 * c.sign     # conspiratorial: a deeper, tilted lean
    elif c.variant == 4:
        # Folding in: the antennas come slowly DOWN to rest while the head settles
        # a little lower and simply stays. The stillest thing the robot can do, and
        # the only intimacy read whose antennas close rather than hold.
        b, d = c.budget, c.mods.duration_scale
        held = HeadPose(pitch=2.0 * amp, roll=c.sign * 1.5 * amp,
                        z=R.clamp_cue_z((z - 2.0) * amp, limit=c.cfg.z_mm))
        body = [
            MotionIntent(kind="goto", head=held,
                         antennas=_scaled((0.25, -0.25), amp), duration=0.55 * b * d,
                         interpolation="minjerk", reason=c.reason),
            MotionIntent(kind="goto", head=held, antennas=R.ANTENNA_REST,
                         duration=0.35 * b * d, interpolation="minjerk",
                         reason=c.reason + " (fold)"),
        ]
        return body, R.ANTENNA_REST, 0.15 * b
    antennas = _scaled(c.mods.antennas or (0.35, -0.35), amp)
    body = R.lean_in_intimate(
        pitch_deg=pitch * amp, roll_deg=roll * amp, z_mm=(z + 1.0) * amp,
        z_limit_mm=c.cfg.z_mm,
        antennas=antennas, lean_s=lean_frac * b * d, hold_s=hold * b * d, reason=c.reason)
    return body, antennas, 0.15 * b


def _plan_softening(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Gentling, apologising, easing off: one small nod and a soft settle."""

    pitch, antennas, settle = 9.0, (-0.4, 0.4), (-0.4, 0.4)
    if c.band == "low":
        pitch, antennas, settle = 11.0, R.ANTENNA_DROOP_DEEP, R.ANTENNA_DROOPED
    elif c.band == "up":
        pitch, antennas, settle = 6.0, R.ANTENNA_REST, R.ANTENNA_REST
    if c.mods.pitch_override is not None:
        pitch = c.mods.pitch_override
    if c.mods.antennas is not None:
        antennas = settle = c.mods.antennas
    amp = c.amp()
    b, d = c.budget, c.mods.duration_scale
    roll = 3.0 * c.sign
    if c.variant == 1:
        pitch *= 0.85                       # a touch shallower
    elif c.variant == 3:
        roll = 7.0 * c.sign                 # the nod arrives through a sympathetic tilt
    if c.variant == 4:
        # The sympathetic HOLD: no nod at all -- the head cocks over toward the
        # person and stays there while the antennas ease down. "I am listening to
        # this" rather than "I am agreeing with this", and the only softening read
        # without a dip in it.
        held = HeadPose(pitch=pitch * 0.35 * amp, roll=roll * 2.2 * amp,
                        z=R.clamp_cue_z(-2.0 * amp, limit=c.cfg.z_mm))
        body = [
            MotionIntent(kind="goto", head=held, antennas=_scaled(antennas, amp * 0.8),
                         duration=0.55 * b * d, interpolation="minjerk", reason=c.reason),
            MotionIntent(kind="goto", head=held, antennas=_scaled(antennas, amp),
                         duration=0.35 * b * d, interpolation="minjerk",
                         reason=c.reason + " (hold)"),
        ]
        return body, _scaled(settle, amp), 0.30 * b
    if c.variant == 2:
        # A double-nod: two smaller gentling dips read as "I hear you, truly".
        half = 0.5
        body = R.soften_settle(
            nod_pitch=pitch * 0.75 * amp, return_pitch=0.0, z_mm=-3.0 * amp,
            roll_deg=roll * amp, z_limit_mm=c.cfg.z_mm,
            antennas=_scaled(antennas, amp), nod_s=0.35 * b * d * half,
            return_s=0.25 * b * d * half, reason=c.reason)
        body += R.soften_settle(
            nod_pitch=pitch * 0.9 * amp, return_pitch=-1.0 * amp, z_mm=-4.0 * amp,
            roll_deg=0.0, z_limit_mm=c.cfg.z_mm,
            antennas=_scaled(antennas, amp), nod_s=0.3 * b * d * half,
            return_s=0.3 * b * d * half, reason=c.reason)
    else:
        body = R.soften_settle(
            nod_pitch=pitch * amp, return_pitch=-1.0 * amp, z_mm=-4.0 * amp,
            roll_deg=roll * amp, z_limit_mm=c.cfg.z_mm,
            antennas=_scaled(antennas, amp), nod_s=0.35 * b * d, return_s=0.35 * b * d,
            reason=c.reason)
    return body, _scaled(settle, amp), 0.30 * b


def _plan_release(c: _Ctx) -> tuple[list[MotionIntent], tuple[float, float], float]:
    """Relief, exhale, letting go: sink, then open up. The shape inverts mid-sentence."""

    out_frac, settle_antennas = 0.45, R.ANTENNA_REST
    if c.band == "low":
        out_frac = 0.55                     # a longer exhale for a learner still low
    elif c.band == "up":
        settle_antennas = R.ANTENNA_PERKED  # brisker, ends open
    amp = c.amp()
    b, d = c.budget, c.mods.duration_scale
    # Adapted from Pollen's recorded relief1: the exhale is a VERTICAL drop
    # (measured -20.6 mm) with a single committed antenna bloom, not a pitch bow.
    sink_pitch, open_pitch, sink_z, open_z = 4.0, -4.0, -16.0, -2.0
    bloom = c.mods.antennas or R.ANTENNA_BLOOM
    bloom_amp = min(amp, 1.15)               # 1.9 rad x 1.15 = 2.2, inside the measured range
    if c.variant == 1:
        sink_z, bloom_amp = -10.0, bloom_amp * 0.55   # a quieter sigh
    elif c.variant == 2:
        out_frac = min(0.75, out_frac + 0.2)          # one long slow exhale
    elif c.variant == 3:
        open_pitch, open_z = -8.0, 4.0                # letting go LIFTS
    elif c.variant == 4:
        # The SLOW open: no drop first. One long unbroken rise with the antennas
        # blooming the whole way -- relief that arrives gradually rather than all
        # at once, which is most of the relief a real conversation contains.
        body = [
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=1.0 * amp,
                                       z=R.clamp_cue_z(-4.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled(R.ANTENNA_PERKED, min(amp, 1.0)),
                         duration=0.30 * b * d, interpolation="minjerk", reason=c.reason),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=-3.0 * amp, roll=c.sign * 2.0 * amp,
                                       z=R.clamp_cue_z(2.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled(bloom, bloom_amp * 0.6),
                         body_yaw=R.cue_body_yaw(c.sign * 0.06 * amp, enabled=c.cfg.body_yaw),
                         duration=0.35 * b * d, interpolation="minjerk",
                         reason=c.reason + " (opening)"),
            MotionIntent(kind="goto",
                         head=HeadPose(pitch=-6.0 * amp,
                                       z=R.clamp_cue_z(5.0 * amp, limit=c.cfg.z_mm)),
                         antennas=_scaled(bloom, bloom_amp),
                         body_yaw=R.cue_body_yaw(0.0, enabled=c.cfg.body_yaw),
                         duration=0.35 * b * d, interpolation="ease_in_out",
                         reason=c.reason + " (open)"),
        ]
        return body, _scaled(settle_antennas, min(amp, 1.0)), 0.25 * b
    body = R.release_exhale(
        sink_pitch=sink_pitch * amp, open_pitch=open_pitch * amp,
        sink_z_mm=sink_z * amp, open_z_mm=open_z, z_limit_mm=c.cfg.z_mm,
        antennas_in=_scaled(c.mods.antennas or R.ANTENNA_PERKED, min(amp, 1.0)),
        antennas_out=_scaled(bloom, bloom_amp), sway_roll_deg=3.0 * amp * c.sign,
        body_yaw_rad=0.10 * amp * c.sign, body_yaw_enabled=c.cfg.body_yaw,
        sink_s=0.30 * b * d, out_s=out_frac * b * d, reason=c.reason)
    return body, _scaled(settle_antennas, min(amp, 1.0)), 0.25 * b


def _beat_count(n: int, mods: _Mods) -> int:
    n = max(1, n + mods.beat_bonus)
    return min(n, mods.beat_cap) if mods.beat_cap else n


_BUILDERS = {
    "tension": _plan_tension,
    "levity": _plan_levity,
    "energy": _plan_energy,
    "intimacy": _plan_intimacy,
    "softening": _plan_softening,
    "release": _plan_release,
}
