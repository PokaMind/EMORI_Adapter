"""Affect estimation: valence/arousal (circumplex) and a discrete Expression label.

Pokamind is careful (EU AI Act) to describe *observable communication styles*,
not inferred emotions. We honour that: valence/arousal here are derived affect
proxies used only to modulate robot behaviour, and the discrete label maps onto
``hri_msgs/Expression`` so the robot can publish its *own* expressive intent in a
ROS4HRI-standard way. They are not claims about the learner's true emotion.

On the LIVE contract the only affect inputs are the style distribution (dense,
31 keys) and ``body_language.overall_energy`` — the facial positivity / verbal
warmth / tone pitch channels never arrive. The style terms therefore use
``GroupBaseline`` deviations (like engagement) so live affect actually swings far
enough to reach the de-escalation threshold; the metric terms remain as bonuses
for the richer mock shape.
"""

from __future__ import annotations

from typing import Optional

from emori_adapter.constants import (
    AROUSAL_HIGH_STYLES,
    AROUSAL_LOW_STYLES,
    VALENCE_NEGATIVE_STYLES,
    VALENCE_POSITIVE_STYLES,
    normalize_body,
)
from emori_adapter.models import AnalysisCycle, ExpressionLabel
from emori_adapter.translation.styles import GroupBaseline, group_mass


def _metric(d: dict[str, float], key: str, default: float = 50.0) -> float:
    try:
        return max(0.0, min(1.0, float(d.get(key, default)) / 100.0))
    except (TypeError, ValueError):
        return default / 100.0


def _style_term(
    styles: dict[str, float],
    pos_names: tuple[str, ...],
    neg_names: tuple[str, ...],
    pos_baseline: Optional[GroupBaseline],
    neg_baseline: Optional[GroupBaseline],
) -> float:
    """pos-vs-neg style signal: adaptive deviations when baselines are provided."""
    pos = group_mass(styles, pos_names)
    neg = group_mass(styles, neg_names)
    if pos_baseline is not None and neg_baseline is not None:
        pos_sig = pos_baseline.update(pos)
        neg_sig = neg_baseline.update(neg)
        if pos_baseline.warmed_up:
            return 2.0 * (pos_sig - neg_sig)  # deviations are ±0.5 -> term is ±1 (pre-mix)
    return pos - neg


def valence_arousal(
    styles: dict[str, float],
    cycle: AnalysisCycle,
    *,
    baselines: Optional[dict[str, GroupBaseline]] = None,
) -> tuple[float, float]:
    """Return (valence, arousal), each in -1..1.

    ``baselines`` (from the stateful Translator) holds four GroupBaselines keyed
    ``val_pos / val_neg / aro_high / aro_low``; stateless calls fall back to
    absolute group masses (the legacy behaviour).
    """
    b = baselines or {}
    # Valence from positive vs negative style mass, plus facial positivity / verbal warmth.
    v_styles = _style_term(styles, VALENCE_POSITIVE_STYLES, VALENCE_NEGATIVE_STYLES,
                           b.get("val_pos"), b.get("val_neg"))
    v_face = _metric(cycle.facial_metrics, "positivity") - 0.5 if cycle.facial_metrics else 0.0
    v_warm = _metric(cycle.verbal_metrics, "warmth") - 0.5 if cycle.verbal_metrics else 0.0
    valence = _clip(1.0 * v_styles + 0.6 * v_face + 0.4 * v_warm)

    # Arousal from high vs low arousal styles, plus body energy / tone pitch variety.
    a_styles = _style_term(styles, AROUSAL_HIGH_STYLES, AROUSAL_LOW_STYLES,
                           b.get("aro_high"), b.get("aro_low"))
    a_energy = _energy(cycle.body_language) - 0.5
    a_pitch = _metric(cycle.tone_metrics, "pitch_variety") - 0.5 if cycle.tone_metrics else 0.0
    arousal = _clip(1.0 * a_styles + 0.5 * a_energy + 0.4 * a_pitch)
    return valence, arousal


def _energy(body_language: dict) -> float:
    bl = normalize_body(body_language)
    mapping = {"low": 0.2, "moderate": 0.5, "high": 0.85}
    return mapping.get(str(bl.get("overall_energy", "moderate")).lower(), 0.5)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def to_expression(valence: float, arousal: float) -> tuple[ExpressionLabel, float]:
    """Map a valence/arousal point to a discrete hri_msgs/Expression label + confidence.

    Confidence is the distance from neutral (origin), so a strong affect reads as a
    confident label and a near-neutral point reads as low-confidence "neutral".
    """
    magnitude = min(1.0, (valence**2 + arousal**2) ** 0.5)
    # Near the origin: low-confidence "neutral" (confidence = closeness to neutral).
    if magnitude < 0.18:
        return "neutral", round(1.0 - magnitude, 3)

    # Away from the origin we emit a non-neutral label whose confidence IS the
    # magnitude (distance from neutral). We never return high-confidence "neutral":
    # a calm-but-positive point reads as a softer "happy" (content), not neutral.
    if valence >= 0:
        confidence = magnitude if arousal >= -0.15 else magnitude * 0.6
        return "happy", round(confidence, 3)
    if arousal >= 0.35:
        return "angry", round(magnitude, 3)
    if arousal >= 0.0:
        return "scared", round(magnitude, 3)
    return "sad", round(magnitude, 3)
