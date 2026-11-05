"""Translation: Pokamind cycles -> engagement / affect / context signals."""

from __future__ import annotations

from emori_adapter.models import AnalysisCycle, EngagementLevel
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.translation import Translator


def _cycle(**kw) -> AnalysisCycle:
    base = dict(timestamp="2026-06-03T10:00:00.000Z", session_id="s", person_id="learner_01", seq=0)
    base.update(kw)
    return AnalysisCycle(**base)


def test_engaged_cycle_scores_high():
    c = _cycle(
        communication_styles={"mirroring": 35, "acknowledging": 30, "investigative": 20},
        facial_metrics={"engagement": 85, "eye_contact": 80, "positivity": 75},
        body_language_metrics={"engagement": 80, "confidence": 75},
        verbal_metrics={"warmth": 70},
        body_language={"posture": "lean_forward", "head_position": "nodding", "overall_energy": "moderate"},
    )
    sig = Translator().translate(c)
    assert sig.engagement_score >= 0.62
    assert sig.engagement_level == EngagementLevel.ENGAGED
    assert sig.valence > 0


def test_withdrawn_cycle_scores_low():
    c = _cycle(
        communication_styles={"withdrawing": 35, "quiet": 30, "reserved": 25},
        facial_metrics={"engagement": 20, "eye_contact": 25, "positivity": 35},
        body_language_metrics={"engagement": 25, "confidence": 30},
        body_language={"posture": "lean_back", "head_position": "tilted", "overall_energy": "low"},
    )
    sig = Translator().translate(c)
    assert sig.engagement_score <= 0.38
    assert sig.engagement_level == EngagementLevel.DISENGAGED


def test_agitated_cycle_flags_escalation_and_negative_valence():
    c = _cycle(
        communication_styles={"rapid": 35, "abrupt": 30, "corrective": 20},
        facial_metrics={"engagement": 55, "positivity": 20},
        tone_metrics={"pitch_variety": 80},
        verbal_metrics={"warmth": 20},
        body_language={"posture": "lean_forward", "head_position": "shaking", "overall_energy": "high"},
    )
    sig = Translator().translate(c)
    assert sig.valence < 0
    assert sig.arousal > 0
    assert sig.context.escalation >= 0.6


def test_full_arc_visits_engaged_and_disengaged():
    tr = Translator()  # single stateful translator across the whole arc
    reports = [tr.translate(c) for c in MockPokamindSource(realtime=False).cycles()]
    levels = {r.engagement_level for r in reports}
    assert EngagementLevel.ENGAGED in levels
    assert EngagementLevel.DISENGAGED in levels or EngagementLevel.DISENGAGING in levels
    # at least one cycle should flag escalation (the 'agitated' phase)
    assert any(r.context.escalation >= 0.6 for r in reports)
