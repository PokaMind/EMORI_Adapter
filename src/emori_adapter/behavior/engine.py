"""Behaviour engine: normalised signals -> Reachy Mini motion intents.

This is the seed of the open-source ``hri_engagement_behaviors`` package:
a generic engagement-to-behaviour mapping. It is intentionally
ROS-free and deterministic so it can be unit-tested; the ROS 2 node simply feeds it
signals decoded from ``/humans/*`` topics and forwards the resulting intents to the
robot.

Decision policy (highest priority first):

1. **De-escalation** - if context.escalation is high (agitated learner), go slow,
   grounded and low-amplitude. Safety policy: never escalate back.
2. **Engagement recovery** - if DISENGAGING / attention dropping, do a clear
   re-capture: nod + gaze to learner + antenna wiggle (+ optional backchannel).
3. **Attention seeking** - if DISENGAGED, a gentle curious tilt + wiggle.
4. **Affirm** - if ENGAGING, perk up and lean in slightly.
5. **Maintain** - if ENGAGED, attentive neutral gaze with an occasional slow nod.

Affect (valence/arousal) then modulates antenna posture. Cooldowns prevent the
robot from spamming the same gesture every 2 s cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from emori_adapter.behavior import repertoire as R
from emori_adapter.models import DerivedSignals, EngagementLevel, MotionIntent


@dataclass
class BehaviorConfig:
    escalation_threshold: float = 0.6
    backchannel_sound: str = ""  # set to a daemon sound filename to enable audio backchannels
    backchannel_cooldown_cycles: int = 3
    recovery_cooldown_cycles: int = 2
    nod_when_engaged_every: int = 4  # slow affirming nod cadence while ENGAGED
    weights: dict[str, float] = field(default_factory=dict)  # reserved for tuning

    @classmethod
    def from_yaml(cls, path: str | Path) -> "BehaviorConfig":
        """Load overrides from configs/behavior.yaml (a ``behavior:`` mapping)."""
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        section = data.get("behavior", data)
        known = {k: v for k, v in section.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class BehaviorEngine:
    """Map :class:`DerivedSignals` to an ordered list of :class:`MotionIntent`."""

    def __init__(self, config: BehaviorConfig | None = None) -> None:
        self.config = config or BehaviorConfig()
        self._seq = 0
        self._last_backchannel = -999
        self._last_recovery = -999

    def step(self, signals: DerivedSignals) -> list[MotionIntent]:
        self._seq += 1
        cfg = self.config
        intents: list[MotionIntent] = []
        ctx = signals.context

        # 1) De-escalation takes precedence over everything else.
        if ctx.escalation >= cfg.escalation_threshold:
            intents.extend(R.calm_settle())
            intents.append(R.droop_antennas(reason="de-escalation: soften posture"))
            return self._finalise(intents)

        level = signals.engagement_level

        # 2) Engagement recovery (attention dropping / disengaging).
        if level == EngagementLevel.DISENGAGING or ctx.note == "attention-dropping":
            if self._seq - self._last_recovery >= cfg.recovery_cooldown_cycles:
                self._last_recovery = self._seq
                intents.append(R.gaze_to(0.0, pitch_deg=6.0, reason="recovery: return gaze to learner"))
                intents.extend(R.nod(reason="recovery: affirming nod"))
                intents.extend(R.antenna_wiggle(reason="recovery: re-capture attention"))
                self._maybe_backchannel(intents, reason="recovery backchannel")
            else:
                intents.append(R.perk_antennas(reason="recovery (cooldown): stay alert"))

        # 3) Disengaged: gentle attention seeking.
        elif level == EngagementLevel.DISENGAGED:
            intents.append(R.head_tilt(reason="attention-seeking curious tilt"))
            intents.extend(R.antenna_wiggle(reason="attention-seeking wiggle"))

        # 4) Engaging: affirm and lean in (clearly visible).
        elif level == EngagementLevel.ENGAGING:
            intents.append(R.perk_antennas())
            intents.append(R.gaze_to(0.0, pitch_deg=10.0, reason="lean in"))

        # 5) Engaged: stay visibly alive - gentle scan of the learner + periodic nod,
        #    so the robot never freezes between cycles.
        elif level == EngagementLevel.ENGAGED:
            intents.append(R.look_around(self._seq, reason="maintain attentive gaze"))
            if self._seq % max(1, cfg.nod_when_engaged_every) == 0:
                intents.extend(R.nod(depth_deg=14.0, reason="slow affirming nod"))

        else:  # UNKNOWN
            intents.append(R.neutral_gaze(reason="idle / unknown"))

        # Affect modulation: antenna posture follows valence (unless already set).
        self._apply_affect(intents, signals)
        return self._finalise(intents)

    # --- helpers ---------------------------------------------------------
    def _apply_affect(self, intents: list[MotionIntent], signals: DerivedSignals) -> None:
        already_moves_antennas = any(i.antennas is not None or i.kind == "antennas" for i in intents)
        if already_moves_antennas:
            return
        if signals.valence < -0.25:
            intents.append(R.droop_antennas(reason="empathetic (negative valence)"))
        elif signals.valence > 0.35 and signals.arousal > 0.0:
            intents.append(R.perk_antennas(reason="mirroring positive affect"))

    def _maybe_backchannel(self, intents: list[MotionIntent], reason: str) -> None:
        cfg = self.config
        if not cfg.backchannel_sound:
            return
        if self._seq - self._last_backchannel >= cfg.backchannel_cooldown_cycles:
            self._last_backchannel = self._seq
            intents.append(R.backchannel(cfg.backchannel_sound, reason=reason))

    def _finalise(self, intents: list[MotionIntent]) -> list[MotionIntent]:
        # Stable ordering: higher priority first, otherwise preserve insertion order.
        return sorted(intents, key=lambda i: -i.priority)
