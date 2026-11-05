"""Translation layer: Pokamind analysis -> normalised ROS4HRI-style signals.

The :class:`Translator` is stateful: it keeps short rolling histories (trends,
escalation) and per-session :class:`GroupBaseline`s that auto-calibrate the style
channels to whatever distribution shape arrives — the sparse rich mock or the
dense 31-key live contract. :class:`TranslationConfig` exposes the tuning knobs
(engagement bands, baseline gain) and loads from configs/behavior.yaml's
``translation:`` section so the same file tunes every deployment path.

Alongside the derived signals sits :func:`person_report` — the *observation* half,
pure and stateless, which keeps "Pokamind never reported this channel" distinct from
"Pokamind reported zero" all the way onto the ROS wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from emori_adapter.models import AnalysisCycle, DerivedSignals
from emori_adapter.translation.context import ContextTracker
from emori_adapter.translation.engagement import EngagementTracker, engagement_score
from emori_adapter.translation.expression import to_expression, valence_arousal
from emori_adapter.translation.person_report import (
    EXPRESSION_CONSTANT_NAMES,
    NOT_REPORTED,
    PersonReport,
    PersonReportFlags,
    person_report,
)
from emori_adapter.translation.styles import GroupBaseline, normalize_styles, top_styles


@dataclass
class TranslationConfig:
    """Tuning for the translation layer (see configs/behavior.yaml ``translation:``)."""

    engagement_high: float = 0.62   # score >= high -> ENGAGED
    engagement_low: float = 0.38    # score <= low  -> DISENGAGED
    style_gain: float = 4.0         # GroupBaseline deviation gain
    style_alpha: float = 0.05       # GroupBaseline EMA speed (slow = sustained states persist)
    style_warmup: int = 3           # cycles of absolute fallback before deviations kick in

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TranslationConfig":
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        section = data.get("translation", {}) or {}
        known = {k: v for k, v in section.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Translator:
    """Converts Pokamind cycles into normalised engagement/affect/context signals."""

    def __init__(self, config: TranslationConfig | None = None) -> None:
        self.config = config or TranslationConfig()
        c = self.config
        self._engagement = EngagementTracker(high=c.engagement_high, low=c.engagement_low)
        self._context = ContextTracker()

        def _baseline() -> GroupBaseline:
            return GroupBaseline(gain=c.style_gain, alpha=c.style_alpha, warmup=c.style_warmup)

        self._eng_pos = _baseline()
        self._eng_neg = _baseline()
        self._affect_baselines = {
            "val_pos": _baseline(), "val_neg": _baseline(),
            "aro_high": _baseline(), "aro_low": _baseline(),
        }

    def translate(self, cycle: AnalysisCycle) -> DerivedSignals:
        styles = normalize_styles(cycle.communication_styles)
        score = engagement_score(styles, cycle,
                                 pos_baseline=self._eng_pos, neg_baseline=self._eng_neg)
        level = self._engagement.update(score)
        valence, arousal = valence_arousal(styles, cycle, baselines=self._affect_baselines)
        expression, expr_conf = to_expression(valence, arousal)
        tops = top_styles(styles, n=4)
        context = self._context.update(
            engagement=score,
            valence=valence,
            arousal=arousal,
            dominant_style=tops[0][0] if tops else "neutral",
            cycle=cycle,
        )
        return DerivedSignals(
            timestamp=cycle.timestamp,
            session_id=cycle.session_id,
            person_id=cycle.person_id,
            seq=cycle.seq,
            engagement_score=score,
            engagement_level=level,
            valence=valence,
            arousal=arousal,
            expression=expression,
            expression_confidence=expr_conf,
            top_styles=tops,
            context=context,
        )


__all__ = [
    "EXPRESSION_CONSTANT_NAMES",
    "NOT_REPORTED",
    "PersonReport",
    "PersonReportFlags",
    "TranslationConfig",
    "Translator",
    "person_report",
]
