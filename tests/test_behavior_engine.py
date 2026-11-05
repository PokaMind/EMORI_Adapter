"""Behaviour engine: signals -> robot motion intents (deterministic)."""

from __future__ import annotations

from emori_adapter.behavior import BehaviorConfig, BehaviorEngine
from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel


def _signals(level: EngagementLevel, *, escalation=0.0, valence=0.0, arousal=0.0, note="", seq=0) -> DerivedSignals:
    return DerivedSignals(
        timestamp="t", session_id="s", person_id="learner_01", seq=seq,
        engagement_score=0.5, engagement_level=level, valence=valence, arousal=arousal,
        context=ContextSignal(escalation=escalation, note=note),
    )


def _reasons(intents) -> str:
    return " | ".join(i.reason for i in intents)


def test_escalation_triggers_de_escalation():
    eng = BehaviorEngine()
    intents = eng.step(_signals(EngagementLevel.ENGAGED, escalation=0.8, valence=-0.6, arousal=0.8))
    assert any("de-escalation" in i.reason for i in intents)
    # de-escalation explicitly disables audio-reactive wobble
    assert any(i.kind == "wobble" and i.wobble is False for i in intents)


def test_disengaging_triggers_recovery_with_nod():
    eng = BehaviorEngine()
    intents = eng.step(_signals(EngagementLevel.DISENGAGING, note="attention-dropping"))
    r = _reasons(intents)
    assert "recovery" in r
    assert any("nod" in i.reason for i in intents)
    assert any(i.kind == "antennas" for i in intents)  # re-capture wiggle


def test_engaged_maintains_gaze():
    eng = BehaviorEngine()
    intents = eng.step(_signals(EngagementLevel.ENGAGED, seq=1))
    assert any("maintain attentive gaze" in i.reason for i in intents)


def test_engaged_nods_on_cadence():
    eng = BehaviorEngine(BehaviorConfig(nod_when_engaged_every=1))
    intents = eng.step(_signals(EngagementLevel.ENGAGED))
    assert any("nod" in i.reason for i in intents)


def test_negative_valence_softens_antennas_when_engaged():
    # ENGAGED steady-state uses a head-only neutral gaze, so affect governs antennas:
    # a sad/negative-valence learner should get empathetic drooped antennas.
    eng = BehaviorEngine()
    intents = eng.step(_signals(EngagementLevel.ENGAGED, valence=-0.5, seq=1))
    assert any("empathetic" in i.reason for i in intents)
    assert any(i.kind == "goto" for i in intents)  # gaze still present


def test_positive_valence_perks_antennas_when_engaged():
    eng = BehaviorEngine()
    intents = eng.step(_signals(EngagementLevel.ENGAGED, valence=0.6, arousal=0.3, seq=1))
    assert any("mirroring positive affect" in i.reason for i in intents)
