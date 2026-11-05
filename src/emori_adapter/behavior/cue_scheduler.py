"""One cue plan per spoken sentence, newest sentence wins.

The scheduler is the small stateful shell around the pure planner in ``cues.py``.
It owns exactly three things:

1. **A generation counter.** Every plan is submitted with a guard closure that
   compares the generation captured at submit time with the current one.
   ``RobotPlayer._run`` re-evaluates that guard before the batch *and before every
   individual intent*, so the moment sentence *n+1* arrives, the remaining intents
   of sentence *n* are skipped at the very next dequeue step, and its first move
   replaces whatever is still running (``MotionIntent.supersede``). No way for the
   tail of an old sentence to animate a new one.
2. **A clock-based ``speaking`` window.** The per-cycle engagement lane reads it to
   yield the head while the AI talks, rather than interleaving two motion sources
   into nonsense. Clock-based, not queue-based, so it is honest even when the
   submit path drops the plan.
3. **The suppression rules** that decide when to plan nothing at all.

``submit`` is injected: in production the hostapp passes
``lambda intents, guard: driver.submit_legacy_motion(intents, drop_if_busy=False, guard=guard)``
so this module never imports the hostapp, never touches the robot, and stays
testable with a list-appending fake.
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Optional

from emori_adapter.behavior import repertoire as R
from emori_adapter.behavior.cues import CUE_FAMILIES, CueConfig, VarietyHint, plan_cue
from emori_adapter.models import MotionIntent

log = logging.getLogger("emori.behavior.cue_scheduler")

SubmitFn = Callable[[list[MotionIntent], Callable[[], bool]], bool]

# How long an authored family keeps colouring the turn's untagged sentences.
# on_tts_ended clears it at every turn boundary, so the TTL only matters when the
# kiosk's tts_ended marker is lost -- it bounds a stale emotion, never a live one.
_CARRY_TTL_S = 20.0
# Minimum spacing between two Pollen recordings (set-pieces): the authored shapes
# and the voice-paced talking flow fill the sentences in between. With the widened
# rosters, the variety memory and session-unique selection, a second recording is a
# DIFFERENT performance rather than the same one again, and a longer gap (6 s left
# three recordings in sixteen sentences) withholds the fullest-bodied, most
# characterful motion the robot owns.
_RECORDED_GAP_S = 3.0

# What a cue with no words of its own is planned as: long enough for one short gesture.
_CUE_ONLY_TEXT = "mm"

# How many recent structural shapes / recordings the planner is told about. Shapes:
# 4 of the 9 talking shapes (and of the 5 reads per family) leaves the planner real
# choice while making an immediate repeat impossible. Moves: 6, roughly half the
# largest family roster, so a family cycles rather than favours.
_SHAPE_MEMORY = 4
_MOVE_MEMORY = 6
_FAMILY_MEMORY = 4


@dataclass(frozen=True)
class AffectSnapshot:
    """The learner as of the most recent analysis cycle (never a guess).

    ``fresh`` False means no cycle has arrived yet this session: the planner then
    uses a fixed middle amplitude and no affect sub-variant instead of reading a
    zeroed snapshot as a perfectly neutral learner.
    """

    valence: float = 0.0        # -1..1
    arousal: float = 0.0        # -1..1
    escalation: float = 0.0     # 0..1
    engagement_level: int = 0   # EngagementLevel int value
    fresh: bool = False


@dataclass(frozen=True)
class CuePlanResult:
    """What happened to one sentence -- the driver's counters and trace line."""

    planned: bool
    family: str
    intents: int
    budget_s: float
    reason: str
    # Mirrors CuePlan.speaking: the plan claimed the speaking window, so the driver
    # owes exactly one settle when the voice stops.
    speaking: bool = False
    # Mirrors CuePlan.source / CuePlan.recorded: where the family came from and
    # which Pollen recording (if any) the sentence performs -- the driver stamps
    # both on the relayed envelope for the ROS graph and the trace line.
    source: str = ""
    recorded: str = ""


class CueScheduler:
    """Plan and submit one bounded motion plan per AI sentence."""

    def __init__(
        self,
        submit: SubmitFn,
        *,
        clock: Callable[[], float] = time.monotonic,
        config: Optional[CueConfig] = None,
        head_reserved: Optional[Callable[[], bool]] = None,
        on_revoke: Optional[Callable[[], None]] = None,
        voice_available: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._submit = submit
        self._on_revoke = on_revoke
        self._voice_available = voice_available
        self._clock = clock
        self._config = config if config is not None else CueConfig.from_env()
        self._head_reserved = head_reserved
        self._lock = threading.Lock()
        self._generation = 0
        self._speaking_until = 0.0
        # The turn's carried family: the ONE cue Pokamind authors per reply keeps
        # colouring the reply's untagged sentences (at reduced amplitude) until the
        # turn ends. A per-sentence index feeds the planner's variation seed.
        self._carry_family = ""
        self._carry_until = 0.0
        self._carry_depth = 0
        self._sentence_index = 0
        # The variety memory, newest first. It is the scheduler's, not
        # the planner's, because the planner is pure -- and it deliberately survives
        # turn boundaries: a robot that starts every reply with the same gesture is
        # exactly as repetitive as one that repeats within a reply.
        self._recent_shapes: list[str] = []
        self._recent_moves: list[str] = []
        self._recent_families: list[str] = []
        # Every recording performed this session: the rolling window above stops a
        # repeat, this stops a favourite. Cleared per session in reset().
        self._used_moves: set[str] = set()
        self._last_recorded_at = -1e9
        self.counters: dict[str, int] = {"planned": 0, "suppressed": 0, "not_owner": 0,
                                         "settled": 0, "cue_only": 0}

    # --- per-sentence lane ------------------------------------------------
    def on_sentence(
        self, *, text: str, cue_category: Optional[str], cue_body: Optional[str],
        lang: str, affect: AffectSnapshot,
    ) -> CuePlanResult:
        """Plan the motion for one spoken sentence and preempt the previous one."""

        if self._reserved():
            # push_turn reads the human's turn out of the head's own pose: any
            # robot-authored motion wipes the directional detector's rolling
            # baseline. Kill the previous tail, plan nothing new.
            self._bump()
            self._clear_speaking()
            self.counters["suppressed"] += 1
            return CuePlanResult(False, "", 0, 0.0, "head_reserved")

        raw = str(text or "")
        # A sentence that is ONLY a cue. Pokamind's dense contract puts a second
        # bracket before the sentence whose beat shifts, and when that bracket lands
        # on its own sentence boundary the spoken text strips to nothing, carrying
        # exactly the emotions the reply was reaching for.
        #
        # The last sentence's family stops here. A short gesture of the new one goes
        # behind the voice hold: it performs if the sentence turns out to have a sound,
        # and if not, the head stays still until the next sentence revokes it. The family
        # goes to the carry either way, so the sentence the cue was written for performs it.
        if not raw.strip():
            family = cue_category.strip().lower() if isinstance(cue_category, str) else ""
            if family in CUE_FAMILIES and self._config.carry:
                with self._lock:
                    self._carry_family = family
                    self._carry_until = self._clock() + _CARRY_TTL_S
                    self._carry_depth = 0
                self.counters["cue_only"] = self.counters.get("cue_only", 0) + 1
                if self._voice_available is not None and not self._voice_available():
                    # With nothing to tell whether a sound came, the gesture would run on a
                    # timer over silence. The carry is the part that always matters.
                    return CuePlanResult(False, family, 0, 0.0, "cue_only/" + family)
                plan = plan_cue(family, text=_CUE_ONLY_TEXT, lang=lang, valence=affect.valence,
                                arousal=affect.arousal, escalation=affect.escalation,
                                engagement_level=affect.engagement_level, fresh=affect.fresh,
                                config=self._config, allow_recorded=False)
                ok = self._send(list(plan.intents), self._bump())
                # What the moves were planned from, which an escalation can have changed. The
                # carry above stays the authored family, as a guess about the next sentence.
                performed = plan.family or family
                if not ok:
                    self._clear_speaking()
                    self.counters["not_owner"] += 1
                    return CuePlanResult(False, performed, len(plan.intents), plan.budget_s,
                                         "not_owner")
                with self._lock:
                    self._speaking_until = (
                        self._clock() + plan.budget_s if plan.speaking else 0.0)
                return CuePlanResult(True, performed, len(plan.intents), plan.budget_s,
                                     "cue_only/" + performed, speaking=plan.speaking,
                                     source=plan.source)
            self.counters["suppressed"] += 1
            return CuePlanResult(False, "", 0, 0.0, "empty_text")

        with self._lock:
            index = self._sentence_index
            self._sentence_index += 1
        # Deterministic per-sentence seed: the text picks the shape, the index keeps
        # a repeated line from repeating its motion. Purity stays in the planner.
        seed = (zlib.crc32(raw.encode("utf-8", "ignore")) ^ index) & 0xFFFFFFFF

        family = cue_category.strip().lower() if isinstance(cue_category, str) else ""
        if family not in CUE_FAMILIES:
            family = ""
        carried = False
        carry_depth = 0
        if not family and self._config.carry:
            with self._lock:
                if self._carry_family and self._clock() < self._carry_until:
                    family, carried = self._carry_family, True
                    carry_depth = self._carry_depth

        # One of Pollen's recordings is a set-piece; two in a row read as a robot
        # cycling its library. Withhold a second one while the previous recording
        # is still fresh, so the authored shapes and the talking flow come between.
        with self._lock:
            allow_recorded = (self._clock() - self._last_recorded_at) >= _RECORDED_GAP_S
            hint = VarietyHint(shapes=tuple(self._recent_shapes),
                               moves=tuple(self._recent_moves),
                               families=tuple(self._recent_families),
                               used=frozenset(self._used_moves))
        plan = plan_cue(
            family or cue_category, text=text, body=cue_body, lang=lang,
            valence=affect.valence, arousal=affect.arousal, escalation=affect.escalation,
            engagement_level=affect.engagement_level, fresh=affect.fresh,
            seed=seed, carried=carried, config=self._config, allow_recorded=allow_recorded,
            variety=hint, carry_depth=carry_depth,
        )
        guard = self._bump()
        ok = self._send(list(plan.intents), guard)
        if ok:
            with self._lock:
                # The speaking window is what makes the per-cycle engagement lane
                # stand down and what the driver settles at end of turn. Family
                # plans AND the expressive talking flow claim it; only the legacy
                # fallbacks (motion off / talk off / empty text) do not.
                self._speaking_until = (
                    self._clock() + plan.budget_s if plan.speaking else 0.0)
                if plan.family:
                    # An authored family re-arms the carry; a carried one refreshes
                    # it, so a long reply keeps its colour to the last sentence.
                    # ``_carry_depth`` counts how many sentences in a row have now
                    # inherited it -- the planner decays the family into a tint on
                    # the talking grammar past _CARRY_DECAY_AT rather than
                    # performing the same block a third time.
                    self._carry_depth = (self._carry_depth + 1
                                         if plan.family == self._carry_family and carried
                                         else (1 if carried else 0))
                    self._carry_family = plan.family
                    self._carry_until = self._clock() + _CARRY_TTL_S
                if plan.recorded:
                    self._last_recorded_at = self._clock()
                    self._remember(self._recent_moves, plan.recorded, _MOVE_MEMORY)
                    self._used_moves.add(plan.recorded)
                if plan.shape:
                    self._remember(self._recent_shapes, plan.shape, _SHAPE_MEMORY)
                if plan.family:
                    self._remember(self._recent_families, plan.family, _FAMILY_MEMORY)
            self.counters["planned"] += 1
            return CuePlanResult(True, plan.family, len(plan.intents), plan.budget_s,
                                 plan.reason, speaking=plan.speaking,
                                 source=plan.source, recorded=plan.recorded)

        # Legacy motion disabled (an add-on owns the robot): never claim the
        # head we did not get.
        self._clear_speaking()
        self.counters["not_owner"] += 1
        return CuePlanResult(False, plan.family, len(plan.intents), plan.budget_s, "not_owner")

    def on_tts_ended(self) -> None:
        """The AI stopped speaking: kill any tail and return to the attend pose.

        The turn is over, so its emotional colour ends with it: the carried family
        never leaks into the next reply.
        """

        guard = self._bump()
        self._clear_speaking()
        self._clear_carry()
        settle = R.settle_to_attend(body_yaw=(0.0 if self._config.body_yaw else None),
                                    reason="cue settle (AI finished speaking)")
        if self._send([settle.model_copy(update={"supersede": True})], guard):
            self.counters["settled"] += 1

    def on_session_end(self) -> None:
        """Session over: nothing further from this scheduler may reach the robot."""

        self._bump()
        self._clear_speaking()
        self._clear_carry()

    def preempt(self, reason: str = "") -> None:
        """Abandon the live plan without submitting anything.

        The de-escalation path uses this: an escalation cycle arriving mid-sentence
        preempts the cue and lets the per-cycle lane submit its calm settle, because
        safety outranks expression -- and the carried emotion dies with the plan.
        """

        self._bump()
        self._clear_speaking()
        self._clear_carry()
        if reason:
            log.debug("cue preempted: %s", reason)

    def reset(self) -> None:
        """New session: no plan from the previous one may survive it."""

        self._bump()
        self._clear_speaking()
        self._clear_carry()
        with self._lock:
            self._sentence_index = 0
            self._last_recorded_at = -1e9
            # The variety memory survives turns on purpose, but not sessions: the
            # next learner has not seen any of it, and starting them on the shapes
            # the last conversation happened to end on is variety spent for nobody.
            self._recent_shapes.clear()
            self._recent_moves.clear()
            self._recent_families.clear()
            self._used_moves.clear()
        for key in self.counters:
            self.counters[key] = 0

    @property
    def speaking(self) -> bool:
        """True while the current plan's budget is still running."""

        with self._lock:
            return self._clock() < self._speaking_until

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    # --- helpers ----------------------------------------------------------
    def _bump(self) -> Callable[[], bool]:
        """Invalidate every outstanding plan and return the new guard."""

        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._on_revoke is not None:
            self._on_revoke()

        def guard() -> bool:
            with self._lock:
                return generation == self._generation

        return guard

    def _clear_speaking(self) -> None:
        with self._lock:
            self._speaking_until = 0.0

    def _clear_carry(self) -> None:
        with self._lock:
            self._carry_family = ""
            self._carry_until = 0.0
            self._carry_depth = 0

    @staticmethod
    def _remember(memory: list, item: str, limit: int) -> None:
        """Push onto a newest-first bounded memory, without duplicating an entry."""

        if item in memory:
            memory.remove(item)
        memory.insert(0, item)
        del memory[limit:]

    def _reserved(self) -> bool:
        if self._head_reserved is None:
            return False
        try:
            return bool(self._head_reserved())
        except Exception:  # noqa: BLE001 - a broken predicate must not stop the session
            log.warning("cue head_reserved predicate failed; planning anyway", exc_info=True)
            return False

    def _send(self, intents: list[MotionIntent], guard: Callable[[], bool]) -> bool:
        try:
            return bool(self._submit(intents, guard))
        except Exception:  # noqa: BLE001 - a failed cue is never fatal to the session
            log.warning("cue submit failed", exc_info=True)
            return False
