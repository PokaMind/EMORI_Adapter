"""The mock source must emit cycles whose shape matches the Pokamind contract."""

from __future__ import annotations

from emori_adapter.constants import COMM_STYLES
from emori_adapter.models import AnalysisCycle
from emori_adapter.sources.mock import MockPokamindSource

_STYLE_SET = set(COMM_STYLES)


def test_mock_cycles_have_valid_shapes():
    src = MockPokamindSource(cycles_per_phase=2, realtime=False)
    cycles = list(src.cycles())
    assert len(cycles) == 2 * 6  # 6 phases

    for c in cycles:
        assert isinstance(c, AnalysisCycle)
        assert c.session_id and c.person_id
        # every style key must be canonical (after the mock's own generation)
        assert set(c.communication_styles).issubset(_STYLE_SET)
        # 4 x 5 metric dicts present with 5 keys each
        for metrics in (c.facial_metrics, c.tone_metrics, c.body_language_metrics, c.verbal_metrics):
            assert len(metrics) == 5
            assert all(0.0 <= v <= 100.0 for v in metrics.values())
        # body language carries categorical fields
        assert "posture" in c.body_language


def test_mock_seq_is_monotonic():
    src = MockPokamindSource(cycles_per_phase=1, realtime=False)
    seqs = [c.seq for c in src.cycles()]
    assert seqs == sorted(seqs)
    assert seqs[0] == 0
