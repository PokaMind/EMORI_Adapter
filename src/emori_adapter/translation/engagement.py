"""Engagement estimation: a 0..1 scalar plus an ``EngagementLevel`` with hysteresis.

The scalar fuses three sources, in priority order of reliability:

* metric dimensions Pokamind already computes — on the LIVE contract that is only
  ``body_language_metrics.engagement`` (facial metrics never arrive); the richer
  mock shape also feeds facial engagement / eye contact;
* communication-style mass (engagement-positive vs engagement-negative styles),
  measured as a *deviation from the session's own baseline* (``GroupBaseline``) so
  the dense live distribution and the sparse mock produce the same dynamic range;
* a small categorical body-language nudge (leaning_forward / nodding vs
  leaning_back) — live values are folded onto the canonical vocabulary first.

The continuous scalar is then quantised into the REP-155 ``EngagementLevel`` enum
with hysteresis + trend so the robot doesn't flip-flop on noise: crossing *up* a
band reads as ENGAGING, crossing *down* reads as DISENGAGING, and stable highs/lows
settle to ENGAGED / DISENGAGED.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from emori_adapter.constants import (
    ENGAGEMENT_NEGATIVE_STYLES,
    ENGAGEMENT_POSITIVE_STYLES,
    normalize_body,
)
from emori_adapter.models import AnalysisCycle, EngagementLevel
from emori_adapter.translation.styles import GroupBaseline, group_mass


def _metric(d: dict[str, float], key: str, default: float = 50.0) -> float:
    """Read a 0..100 metric defensively and return it as 0..1."""
    try:
        return max(0.0, min(1.0, float(d.get(key, default)) / 100.0))
    except (TypeError, ValueError):
        return default / 100.0


def engagement_score(
    styles: dict[str, float],
    cycle: AnalysisCycle,
    *,
    pos_baseline: Optional[GroupBaseline] = None,
    neg_baseline: Optional[GroupBaseline] = None,
) -> float:
    """Fuse metrics + styles + body language into a 0..1 engagement estimate."""
    # --- metric channel (weight 0.55) ---
    has_face = bool(cycle.facial_metrics)
    has_body = bool(cycle.body_language_metrics)
    metric_parts: list[float] = []
    if has_face:
        metric_parts.append(_metric(cycle.facial_metrics, "engagement"))
        metric_parts.append(_metric(cycle.facial_metrics, "eye_contact"))
    if has_body:
        metric_parts.append(_metric(cycle.body_language_metrics, "engagement"))
    metric_score = sum(metric_parts) / len(metric_parts) if metric_parts else 0.5

    # --- style channel (weight 0.30) ---
    pos = group_mass(styles, ENGAGEMENT_POSITIVE_STYLES)
    neg = group_mass(styles, ENGAGEMENT_NEGATIVE_STYLES)
    if pos_baseline is not None and neg_baseline is not None:
        pos_sig = pos_baseline.update(pos)
        neg_sig = neg_baseline.update(neg)
        if pos_baseline.warmed_up:
            # adaptive: bounded deviations from the session's own baseline
            style_score = max(0.0, min(1.0, 0.5 + pos_sig - neg_sig))
        else:
            style_score = max(0.0, min(1.0, 0.5 + 0.5 * (pos - neg)))
    else:
        # stateless call (no Translator): absolute masses, the legacy formula
        style_score = max(0.0, min(1.0, 0.5 + 0.5 * (pos - neg)))

    # --- categorical body-language nudge (weight 0.15) ---
    bl = normalize_body(cycle.body_language)
    nudge = 0.5
    posture = str(bl.get("posture", "")).lower()
    head = str(bl.get("head_position", "")).lower()
    if posture in ("lean_forward", "upright"):
        nudge += 0.25
    elif posture in ("lean_back", "slouch"):
        nudge -= 0.25
    if head == "nodding":
        nudge += 0.15
    elif head == "shaking":
        nudge -= 0.1
    nudge = max(0.0, min(1.0, nudge))

    score = 0.55 * metric_score + 0.30 * style_score + 0.15 * nudge
    return max(0.0, min(1.0, score))


class EngagementTracker:
    """Quantise the engagement scalar into an EngagementLevel with hysteresis.

    Band thresholds are configurable (configs/behavior.yaml ``translation:``); the
    defaults match the values validated on both the rich-mock and live-shaped arcs.
    """

    TREND_EPS = 0.04

    def __init__(self, window: int = 4, *, high: float = 0.62, low: float = 0.38) -> None:
        self.HIGH = high
        self.LOW = low
        self._history: deque[float] = deque(maxlen=window)
        self._level = EngagementLevel.UNKNOWN

    def update(self, score: float) -> EngagementLevel:
        prev = self._history[-1] if self._history else score
        self._history.append(score)
        delta = score - prev

        if score >= self.HIGH:
            level = EngagementLevel.ENGAGED
        elif score <= self.LOW:
            level = EngagementLevel.DISENGAGED
        else:
            # mid-band: report the direction of travel
            if delta > self.TREND_EPS:
                level = EngagementLevel.ENGAGING
            elif delta < -self.TREND_EPS:
                level = EngagementLevel.DISENGAGING
            else:
                # stable mid -> keep prior committed state, else ENGAGING by default
                level = self._level if self._level != EngagementLevel.UNKNOWN else EngagementLevel.ENGAGING

        self._level = level
        return level

    @property
    def level(self) -> EngagementLevel:
        return self._level
