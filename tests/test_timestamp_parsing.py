"""Timestamp, style-alias and expression edge cases in the translation layer."""

from __future__ import annotations

from emori_adapter.sources.csv_replay import _fallback_stamp
from emori_adapter.translation.expression import to_expression
from emori_adapter.translation.styles import normalize_styles


def test_normalize_styles_clamps_alias_collision():
    # 'assertive' and 'decisive' both remap to canonical 'direct'; must not exceed 1.0.
    out = normalize_styles({"direct": 100, "assertive": 100, "decisive": 100})
    assert out["direct"] <= 1.0


def test_fallback_stamp_valid_beyond_one_hour():
    # seq=1800 @ 2s = 3600s -> must NOT produce minute "60"
    stamp = _fallback_stamp(1800, 2.0)
    assert stamp == "2026-06-03T11:00:00.000Z"
    hh, mm, ss = stamp.split("T")[1].rstrip("Z").split(":")
    assert int(mm) < 60 and float(ss) < 60


def test_expression_never_high_confidence_neutral():
    # Strong positive valence + low arousal must not be high-confidence "neutral".
    label, conf = to_expression(0.6, -0.5)
    assert not (label == "neutral" and conf > 0.5)


def test_expression_neutral_only_near_origin():
    label, conf = to_expression(0.05, -0.02)
    assert label == "neutral" and conf > 0.8  # near origin = confident neutral


def test_expression_quadrants():
    assert to_expression(0.7, 0.5)[0] == "happy"
    assert to_expression(-0.6, 0.6)[0] == "angry"
    assert to_expression(-0.5, -0.5)[0] == "sad"
