"""MockPokamindSource - a scripted, realistic stand-in for a live Pokamind session.

It walks a deliberate *engagement arc* so the whole pipeline (translation ->
behaviour -> robot) is exercised end to end without any live-service cost:

    settling -> engaged -> attention drop (DISENGAGING) -> recovery ->
    agitation (escalation -> de-escalation) -> calm close

Every cycle carries the exact field shapes Pokamind emits (31 styles, 4x5 metric
dicts, 11 body-language fields, prosody, transcript delta), so swapping in the real
:class:`~emori_adapter.sources.websocket.PokamindBridgeSource` later changes only
where the data comes from, not its shape.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

from emori_adapter.constants import (
    BODY_METRIC_KEYS,
    COMM_STYLES,
    FACIAL_METRIC_KEYS,
    TONE_METRIC_KEYS,
    VERBAL_METRIC_KEYS,
)
from emori_adapter.models import AnalysisCycle
from emori_adapter.sources.base import SessionSource

# Phase -> (dominant styles with weights, engagement 0..1, valence -1..1, arousal -1..1, body cues)
_PHASES = [
    ("settling",   {"neutral": 0.4, "steady": 0.3, "paced": 0.2}, 0.55, 0.05, 0.0,
     {"posture": "upright", "head_position": "upright", "overall_energy": "moderate"}),
    ("engaged",    {"mirroring": 0.35, "acknowledging": 0.3, "investigative": 0.2}, 0.82, 0.35, 0.2,
     {"posture": "lean_forward", "head_position": "nodding", "overall_energy": "moderate"}),
    ("drifting",   {"withdrawing": 0.35, "quiet": 0.3, "reserved": 0.25}, 0.30, -0.1, -0.2,
     {"posture": "lean_back", "head_position": "tilted", "overall_energy": "low"}),
    ("recovering", {"acknowledging": 0.35, "mirroring": 0.3, "exploratory": 0.2}, 0.7, 0.2, 0.15,
     {"posture": "upright", "head_position": "nodding", "overall_energy": "moderate"}),
    ("agitated",   {"rapid": 0.35, "abrupt": 0.3, "energetic": 0.25, "corrective": 0.15}, 0.5, -0.55, 0.7,
     {"posture": "lean_forward", "head_position": "shaking", "overall_energy": "high"}),
    ("calming",    {"softening": 0.3, "paced": 0.3, "acknowledging": 0.25}, 0.6, 0.1, -0.1,
     {"posture": "upright", "head_position": "upright", "overall_energy": "low"}),
]


# Rich-mock body cues -> the live Pokamind vocabulary.
_LIVE_BODY_MAP = {
    "posture": {"upright": "upright", "lean_forward": "leaning_forward",
                "lean_back": "leaning_back"},
    "head_position": {"upright": "straight", "nodding": "nodding",
                      "tilted": "tilted_left", "shaking": "shaking"},
    "overall_energy": {"low": "low", "moderate": "medium", "high": "high"},
}


class MockPokamindSource(SessionSource):
    """Two shapes:

    * ``shape="rich"`` (default): the full historical mock — 4x5 metric dicts,
      prosody, meta. Exercises every translation channel.
    * ``shape="live"``: the EXACT staged-contract shape — dense 31-style dict,
      6 body fields with Pokamind's real vocabulary, ``body_language_metrics``
      = {engagement, confidence} only, no facial/tone/verbal/prosody/meta, and a
      periodic body-analysis-failure cycle (empty body, like a null frame after
      sanitization). Use this to validate behaviour against what the robot will
      actually receive.
    """

    def __init__(
        self,
        *,
        session_id: str = "sim-session",
        person_id: str = "learner_01",
        cycles_per_phase: int = 3,
        period_s: float = 2.0,
        realtime: bool = True,
        seed: int = 7,
        topic: str = "Delivering difficult news to an anxious family member",
        shape: str = "rich",
    ) -> None:
        if shape not in ("rich", "live"):
            raise ValueError(f"unknown mock shape: {shape!r} (rich|live)")
        self.session_id = session_id
        self.person_id = person_id
        self.cycles_per_phase = cycles_per_phase
        self.period_s = period_s
        self.realtime = realtime
        self.topic = topic
        self.shape = shape
        self._seed = seed
        self._rng = random.Random(seed)
        self._t0 = 0.0

    def cycles(self) -> Iterator[AnalysisCycle]:
        # Reseed so each iteration yields the SAME deterministic arc (re-iterable mock).
        self._rng = random.Random(self._seed)
        seq = 0
        for _, dom, eng, val, aro, body in _PHASES:
            for _ in range(self.cycles_per_phase):
                cycle = self._make_cycle(seq, dom, eng, val, aro, body)
                yield cycle
                seq += 1
                if self.realtime:
                    time.sleep(self.period_s)

    # --- generation helpers ---
    def _make_cycle(self, seq, dom, eng, val, aro, body) -> AnalysisCycle:
        if self.shape == "live":
            return self._make_live_cycle(seq, dom, eng, val, aro, body)
        styles = self._styles(dom)
        return AnalysisCycle(
            timestamp=self._stamp(seq),
            session_id=self.session_id,
            person_id=self.person_id,
            seq=seq,
            communication_styles=styles,
            facial_metrics=self._metrics(FACIAL_METRIC_KEYS, eng, val),
            tone_metrics=self._metrics(TONE_METRIC_KEYS, eng, val, arousal=aro),
            body_language_metrics=self._metrics(BODY_METRIC_KEYS, eng, val),
            verbal_metrics=self._metrics(VERBAL_METRIC_KEYS, eng, val),
            body_language=dict(body),
            prosody=self._prosody(aro),
            transcript_delta="",
            meta={"topic": self.topic, "module_type": "roleplay", "language": "en"},
        )

    def _make_live_cycle(self, seq, dom, eng, val, aro, body) -> AnalysisCycle:
        # Every 7th cycle simulates a body-analysis failure (live: all-null body,
        # which the envelope sanitizer reduces to empty dicts).
        body_failed = seq > 0 and seq % 7 == 0
        live_body: dict = {}
        metrics: dict = {}
        if not body_failed:
            live_body = {
                _k: _LIVE_BODY_MAP[_k].get(str(body.get(_k, "")), "not_visible")
                for _k in ("posture", "head_position", "overall_energy")
            }
            live_body["gesture_type"] = "pointing" if aro > 0.4 else "open_palm"
            e_score = round(max(0.0, min(100.0, 90.0 * eng + 5.0 + self._rng.uniform(-6, 6))), 1)
            c_score = round(max(0.0, min(100.0, e_score * 0.9 + self._rng.uniform(-5, 5))), 1)
            live_body["engagement_score"] = e_score
            live_body["confidence_score"] = c_score
            metrics = {"engagement": e_score, "confidence": c_score}
        return AnalysisCycle(
            timestamp=self._stamp(seq),
            session_id=self.session_id,
            person_id=self.person_id,
            seq=seq,
            communication_styles=self._dense_styles(dom),
            body_language_metrics=metrics,
            body_language=live_body,
        )

    def _dense_styles(self, dom: dict[str, float]) -> dict[str, float]:
        """All 31 styles, as the live wire sends them: a dense distribution summing
        ~100, with the phase-dominant styles boosted but NOT dominant in absolute
        terms (live each style is typically 3-10 %)."""
        weights = {name: 1.0 + self._rng.uniform(-0.15, 0.15) for name in COMM_STYLES}
        for name, w in dom.items():
            if name in weights:
                weights[name] *= 1.0 + 8.0 * w  # w 0.2-0.4 -> 2.6x-4.2x
        total = sum(weights.values())
        return {k: round(v / total * 100.0, 2) for k, v in weights.items()}

    def _styles(self, dom: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        # dominant styles as percentages with small noise
        total = 0.0
        for name, w in dom.items():
            pct = max(0.0, w * 100.0 + self._rng.uniform(-4, 4))
            out[name] = round(pct, 1)
            total += pct
        # sprinkle a little mass on a couple of random other styles (noise floor)
        for _ in range(2):
            name = self._rng.choice(COMM_STYLES)
            out.setdefault(name, round(self._rng.uniform(2, 7), 1))
        return out

    def _metrics(self, keys, eng, val, arousal: float | None = None) -> dict[str, float]:
        """Produce 0..100 metric values loosely consistent with engagement/valence."""
        base = 50 + 30 * eng + 20 * val
        out = {}
        for k in keys:
            jitter = self._rng.uniform(-8, 8)
            v = base + jitter
            if k in ("engagement", "eye_contact", "confidence"):
                v = 50 + 45 * eng + jitter
            if k in ("warmth", "positivity"):
                v = 50 + 45 * val + jitter
            if k == "energy" and arousal is not None:
                v = 50 + 45 * arousal + jitter
            out[k] = round(max(0.0, min(100.0, v)), 1)
        return out

    def _prosody(self, aro: float) -> dict[str, float]:
        return {
            "rate": round(3.5 + 2.5 * aro + self._rng.uniform(-0.3, 0.3), 2),
            "f0_mean": round(150 + 60 * aro + self._rng.uniform(-10, 10), 1),
            "f0_std": round(20 + 25 * abs(aro) + self._rng.uniform(-3, 3), 1),
            "pauses": round(max(0.0, 0.4 - 0.3 * aro + self._rng.uniform(-0.1, 0.1)), 2),
        }

    def _stamp(self, seq: int) -> str:
        # Deterministic, dependency-free ISO-ish timestamp (offset from a fixed epoch).
        secs = seq * int(self.period_s)
        hh, rem = divmod(secs, 3600)
        mm, ss = divmod(rem, 60)
        return f"2026-06-03T10:{mm:02d}:{ss:02d}.000Z" if hh == 0 else f"2026-06-03T{10 + hh:02d}:{mm:02d}:{ss:02d}.000Z"
