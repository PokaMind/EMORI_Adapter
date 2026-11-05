"""Arousal alone is not agitation.

Measured on the robot. A learner was read at valence **+0.78**, arousal
**+1.00** for three minutes -- engaged, positive, animated, the "up" affect band --
and the escalation signal sat at exactly **0.60**, the de-escalation threshold,
because the arousal term counted on its own. Every cue for the rest of the session
came out `softening/up/.../deescalated`: amplitudes x0.4, beats capped at 2,
antennas at REST, body yaw off, Pollen recordings withheld. Head motion fell from
13-17 degrees to 3-4, and from across the room the robot had stopped moving.

The same cycle cannot be both "positive and engaged" and "agitated". These tests
pin the two lanes together.
"""

from __future__ import annotations

import pytest

from emori_adapter.models import AnalysisCycle
from emori_adapter.translation.context import ContextTracker


def _escalation(valence: float, arousal: float) -> float:
    cycle = AnalysisCycle(timestamp="2026-08-23T00:00:00Z", session_id="s", person_id="p")
    sig = ContextTracker().update(engagement=0.6, valence=valence, arousal=arousal,
                                  dominant_style="steady", cycle=cycle)
    return sig.escalation


def test_the_measured_session_no_longer_reads_as_agitation():
    # The exact numbers from the robot, which produced 0.60 (the threshold) before.
    assert _escalation(0.78, 1.0) == 0.0


def test_a_genuinely_agitated_learner_still_escalates():
    # High arousal WITH negative valence -- the case the policy exists for. Unchanged.
    assert _escalation(-0.5, 0.9) >= 0.6
    assert _escalation(-0.9, 1.0) >= 0.6


def test_negative_valence_alone_still_counts():
    # Someone quietly miserable is not aroused, but is not fine either.
    assert _escalation(-0.9, 0.0) > 0.6


def test_neutral_valence_keeps_the_full_arousal_term():
    # At valence 0 nothing is gated: this is the ungated value.
    assert _escalation(0.0, 1.0) == pytest.approx(0.6, abs=1e-6)


def test_the_gate_fades_rather_than_switching():
    # Halfway to the "up" band, half the arousal term survives -- no cliff that a
    # learner could oscillate across from one cycle to the next.
    assert _escalation(0.175, 1.0) == pytest.approx(0.3, abs=0.01)


def test_the_gate_closes_exactly_at_the_affect_band_boundary():
    from emori_adapter.behavior.cues import _affect_band
    from emori_adapter.translation.context import _UP_VALENCE

    # The band calls this learner "up"; escalation must therefore not call them agitated.
    assert _affect_band(_UP_VALENCE + 0.01, 1.0) == "up"
    assert _escalation(_UP_VALENCE + 0.01, 1.0) == 0.0


def test_escalation_stays_in_range():
    for v in (-1.0, -0.4, 0.0, 0.4, 1.0):
        for a in (-1.0, 0.0, 0.5, 1.0):
            assert 0.0 <= _escalation(v, a) <= 1.0
