"""Context tracking: the temporal / semantic / relational signals that EMORI
publishes on the (REP-155-extension) ``/humans/persons/<id>/context`` topics.

This is the seed of the open-source ``hri_context_analyzer`` package. Here it
lives in pure Python so it can be unit-tested and reused; the ROS 2 package wraps
it.
"""

from __future__ import annotations

from collections import deque

from emori_adapter.models import AnalysisCycle, ContextSignal


# The valence at which a learner reads as positively engaged rather than agitated:
# `behavior.cues._affect_band` calls everything above this the "up" band. Kept here
# as a named constant so the escalation gate and the affect band cannot drift apart.
_UP_VALENCE = 0.35


class ContextTracker:
    """Maintain short histories to derive trends, escalation, and rapport."""

    def __init__(self, window: int = 8) -> None:
        self._engagement: deque[float] = deque(maxlen=window)
        self._valence: deque[float] = deque(maxlen=window)
        self._arousal: deque[float] = deque(maxlen=window)

    def update(
        self,
        *,
        engagement: float,
        valence: float,
        arousal: float,
        dominant_style: str,
        cycle: AnalysisCycle,
    ) -> ContextSignal:
        self._engagement.append(engagement)
        self._valence.append(valence)
        self._arousal.append(arousal)

        trend = _slope(self._engagement)

        # Escalation: sustained high arousal with negative valence (a learner
        # getting agitated). 0..1. This is the signal the de-escalation behaviour
        # and the trainer "struggling trainee" alert key off.
        #
        # The two terms are not added independently: a learner at arousal 1.0 and valence 0.0
        # would score 0.6 x 1.0 = 0.60 -- exactly the threshold -- with no negative valence
        # anywhere. For example a learner read at valence +0.78,
        # arousal +1.00 (engaged and positive, the "up" affect band) scored 0.60 for
        # three minutes, so the de-escalation policy collapsed every cue to
        # `softening/up/.../deescalated` -- amplitudes x0.4, beats capped at 2,
        # antennas at REST, body yaw off, recordings withheld. Head motion fell from
        # 13-17 degrees to 3-4 and the robot looked switched off. The same cycle was
        # simultaneously "positive and engaged" (the band) and "agitated" (this
        # signal), which cannot both be true.
        #
        # So the arousal term is now GATED by valence, which is what the sentence
        # above always said: it counts fully while valence is negative or neutral
        # and fades to nothing by the time valence reaches the "up" band boundary
        # (_UP_VALENCE, the same 0.35 `cues._affect_band` uses -- the two lanes now
        # agree by construction). Negative valence still contributes on its own, so
        # a genuinely agitated learner scores exactly as before.
        neg_v = max(0.0, -valence)
        gate = 1.0 if valence <= 0.0 else max(0.0, 1.0 - (float(valence) / _UP_VALENCE))
        escalation = max(0.0, min(1.0, 0.6 * max(0.0, arousal) * gate + 0.7 * neg_v))

        # Rapport: positive valence + engagement, smoothed over the window.
        rapport = max(0.0, min(1.0, 0.5 * _mean(self._engagement) + 0.5 * (0.5 + 0.5 * _mean(self._valence))))

        attention = engagement  # proxy; refined later by gaze/eye-contact in ROS layer

        note = ""
        if escalation > 0.6:
            note = "escalation"
        elif trend < -0.15:
            note = "attention-dropping"
        elif trend > 0.15:
            note = "re-engaging"

        return ContextSignal(
            engagement_trend=round(_clip(trend), 3),
            escalation=round(escalation, 3),
            rapport=round(rapport, 3),
            attention=round(attention, 3),
            dominant_style=dominant_style,
            topic=str(cycle.meta.get("topic", "")),
            note=note,
        )


def _mean(seq: deque[float]) -> float:
    return sum(seq) / len(seq) if seq else 0.0


def _slope(seq: deque[float]) -> float:
    """Cheap trend estimate: mean of recent half minus mean of older half."""
    n = len(seq)
    if n < 2:
        return 0.0
    half = n // 2
    older = list(seq)[:half] or list(seq)[:1]
    recent = list(seq)[half:]
    return (sum(recent) / len(recent)) - (sum(older) / len(older))


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))
