"""Communication-style helpers.

Pokamind sends 31 style percentages (0..100). We remap a few off-list aliases,
keep only canonical styles, and expose them as fractions (0..1) plus simple
group aggregates the engagement/affect logic uses.
"""

from __future__ import annotations

from emori_adapter.constants import COMM_STYLES, STYLE_REMAP

_STYLE_SET = set(COMM_STYLES)


def normalize_styles(raw: dict[str, float]) -> dict[str, float]:
    """Remap aliases, drop unknowns, clamp to 0..1 fractions.

    Input values are treated as percentages (0..100); output is 0..1.
    """
    out: dict[str, float] = {}
    for name, value in raw.items():
        key = STYLE_REMAP.get(name, name)
        if key not in _STYLE_SET:
            continue
        try:
            frac = max(0.0, min(1.0, float(value) / 100.0))
        except (TypeError, ValueError):
            continue
        out[key] = out.get(key, 0.0) + frac
    # Clamp after accumulation: two aliases collapsing to one canonical key
    # (e.g. assertive + decisive -> direct) must not push the fraction past 1.0.
    return {k: min(1.0, v) for k, v in out.items()}


def top_styles(styles: dict[str, float], n: int = 4) -> list[tuple[str, float]]:
    """Return the top-n styles by value (value > 0), as (name, fraction)."""
    ranked = sorted(
        ((k, v) for k, v in styles.items() if v > 0.0),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return ranked[:n]


def group_mass(styles: dict[str, float], names: tuple[str, ...]) -> float:
    """Sum of fractions over a named group of styles."""
    return sum(styles.get(name, 0.0) for name in names)


class GroupBaseline:
    """Adaptive scaling for a style-group mass: deviation from a running baseline.

    Why: the live Pokamind distribution is DENSE - all 31 styles, each ~3-10 %,
    so absolute group masses barely move (pos-neg differences of ~0.05), while the
    rich mock concentrates 85 %+ on a few styles. Measuring each group as a bounded
    *deviation from its own running mean* gives both shapes the same dynamic range,
    so one set of thresholds (0.62/0.38 bands, 0.6 escalation) works live and mocked.

    The EMA alpha is slow (0.05) so a sustained state stays elevated for ~20 cycles
    before the baseline absorbs it; the absolute-value fallback covers the first
    ``warmup`` cycles when no baseline exists yet.
    """

    def __init__(self, *, gain: float = 4.0, alpha: float = 0.05, warmup: int = 3,
                 clip: float = 0.5) -> None:
        self.gain = gain
        self.alpha = alpha
        self.warmup = warmup
        self.clip = clip
        self._mean: float | None = None
        self._n = 0

    def update(self, mass: float) -> float:
        """Feed this cycle's group mass; return the bounded deviation signal.

        During warmup the raw mass is returned (callers use their legacy absolute
        formula); afterwards the result is ``clip(±(mass - mean) * gain)``.
        """
        self._n += 1
        if self._mean is None:
            self._mean = mass
        else:
            self._mean += self.alpha * (mass - self._mean)
        if self._n <= self.warmup:
            return mass
        dev = (mass - self._mean) * self.gain
        return max(-self.clip, min(self.clip, dev))

    @property
    def warmed_up(self) -> bool:
        return self._n > self.warmup
