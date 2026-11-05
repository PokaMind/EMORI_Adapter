"""Behaviour reachability on the LIVE contract shape.

The flagship behaviours (de-escalation, engagement recovery) were originally tuned
on the rich mock; these tests prove they are reachable on the THIN live shape the
Pokamind web app actually sends (dense 31-style dict + body fields + 2 scores).
"""

from __future__ import annotations

from emori_adapter.behavior import BehaviorEngine
from emori_adapter.constants import COMM_STYLES, normalize_body
from emori_adapter.models import EngagementLevel
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.translation import TranslationConfig, Translator
from emori_adapter.translation.styles import GroupBaseline


def _run_live_arc(cycles_per_phase: int = 4):
    tr = Translator()
    src = MockPokamindSource(realtime=False, shape="live", cycles_per_phase=cycles_per_phase)
    return [tr.translate(c) for c in src.cycles()]


def test_live_shape_is_the_documented_contract():
    src = MockPokamindSource(realtime=False, shape="live")
    cycles = list(src.cycles())
    assert cycles, "live mock yielded nothing"
    for c in cycles:
        assert len(c.communication_styles) == len(COMM_STYLES)  # dense, all 31
        assert abs(sum(c.communication_styles.values()) - 100.0) < 1.0
        assert c.facial_metrics == {} and c.tone_metrics == {} and c.verbal_metrics == {}
        assert c.prosody == {} and c.meta == {}
        if c.body_language:  # non-failure frames carry exactly the live fields
            assert set(c.body_language) == {"posture", "head_position", "gesture_type",
                                            "overall_energy", "engagement_score",
                                            "confidence_score"}
            assert set(c.body_language_metrics) == {"engagement", "confidence"}
    # the periodic body-analysis-failure frame is simulated too
    assert any(not c.body_language for c in cycles)


def test_live_arc_reaches_engaged_and_disengaged():
    reports = _run_live_arc()
    levels = {r.engagement_level for r in reports}
    assert EngagementLevel.ENGAGED in levels, (
        f"ENGAGED unreachable on live shape; max score "
        f"{max(r.engagement_score for r in reports):.2f}")
    assert (EngagementLevel.DISENGAGED in levels
            or EngagementLevel.DISENGAGING in levels), (
        f"DISENGAGED unreachable on live shape; min score "
        f"{min(r.engagement_score for r in reports):.2f}")


def test_live_arc_reaches_escalation_and_deescalation_fires():
    reports = _run_live_arc()
    max_esc = max(r.context.escalation for r in reports)
    assert max_esc >= 0.6, f"escalation peaked at {max_esc:.2f}; de-escalation unreachable live"

    # And the BehaviorEngine must actually fire the de-escalation behaviour on that arc.
    engine = BehaviorEngine()
    tr = Translator()
    src = MockPokamindSource(realtime=False, shape="live", cycles_per_phase=4)
    reasons = []
    for c in src.cycles():
        for intent in engine.step(tr.translate(c)):
            reasons.append(intent.reason)
    assert any("de-escalation" in r for r in reasons), "calm_settle never fired on live arc"


def test_live_body_failure_frames_degrade_gracefully():
    """Frames with no body data (learner looked away) must still translate."""
    tr = Translator()
    src = MockPokamindSource(realtime=False, shape="live")
    for c in src.cycles():
        sig = tr.translate(c)  # must not raise
        assert 0.0 <= sig.engagement_score <= 1.0


def test_body_vocab_normalization():
    bl = normalize_body({"posture": "leaning_forward", "head_position": "tilted_right",
                         "overall_energy": "medium", "gesture_type": "open_palm",
                         "engagement_score": 70})
    assert bl["posture"] == "lean_forward"
    assert bl["head_position"] == "tilted"
    assert bl["overall_energy"] == "moderate"
    assert bl["engagement_score"] == 70  # numerics pass through


def test_group_baseline_adapts_to_dense_distribution():
    b = GroupBaseline(gain=4.0, alpha=0.05, warmup=3)
    # first `warmup` updates: raw mass back (absolute fallback)
    assert b.update(0.26) == 0.26
    b.update(0.26), b.update(0.26)
    assert not b.warmed_up
    # a dense-shape engaged swing (0.26 -> 0.42) must produce a strong signal
    assert b.update(0.42) > 0.4
    assert b.warmed_up
    # and settle back toward zero when the mass returns to baseline
    assert abs(b.update(0.26)) < 0.2


def test_translation_config_from_yaml(tmp_path):
    p = tmp_path / "behavior.yaml"
    p.write_text("translation:\n  engagement_high: 0.7\n  style_gain: 2.0\n", encoding="utf-8")
    cfg = TranslationConfig.from_yaml(p)
    assert cfg.engagement_high == 0.7
    assert cfg.style_gain == 2.0
    assert cfg.engagement_low == 0.38  # default preserved
    tr = Translator(cfg)
    assert tr._engagement.HIGH == 0.7
