"""Engagement-to-behaviour mapping (seed of hri_engagement_behaviors).

Two lanes live here, and they are deliberately separate:

* the **per-cycle** lane (``BehaviorEngine``) maps ~2 s analysis cycles to motion;
* the **per-sentence** lane (``plan_cue`` / ``CueScheduler``) maps the Pokamind cue
  family of the sentence the AI is speaking right now to bounded expression motion.

Both are pure of I/O; the hostapp owns arbitration between them.
"""

from __future__ import annotations

from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CuePlanResult, CueScheduler
from emori_adapter.behavior.cues import (
    CUE_FAMILIES,
    CueConfig, FAREWELL_MOVE, GREETING_MOVE,
    CuePlan,
    SkillError,
    intensity,
    plan_cue,
    sentence_duration_s,
    skill_to_intents,
    word_count,
)
from emori_adapter.behavior.engine import BehaviorConfig, BehaviorEngine

__all__ = [
    "FAREWELL_MOVE",
    "GREETING_MOVE",
    "BehaviorEngine",
    "BehaviorConfig",
    "AffectSnapshot",
    "CUE_FAMILIES",
    "CueConfig",
    "CuePlan",
    "CuePlanResult",
    "CueScheduler",
    "SkillError",
    "intensity",
    "plan_cue",
    "sentence_duration_s",
    "skill_to_intents",
    "word_count",
]
