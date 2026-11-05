"""What the robot was asked to do, as a structure that two runs can agree on exactly.

Plan content is deterministic given the text, the sentence index and the scheduler's clock.
The number of calls is not: a sentence that outlasts its plan gets sustain beats for as long
as the real voice runs, and a breath after the settle gets more beats and the same settle
again. Antenna angles on a voice-scaled move carry the live level. So the beats and the
settles that follow them come out of the sequence and are compared as a rate, idle gestures
come out because they run on their own clock, and what is left must match.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Optional

SUSTAIN_REASON = "speaking (sustain"
IDLE_REASON = "idle:"
# Calls that move the robot. The gate pushes and the motor mode are the flag stream.
MOTION_METHODS = frozenset({"apply", "play_recorded_move", "stop_recorded_move", "wake_up"})
FLAG_METHODS = frozenset({"set_gaze", "set_head_reserved", "set_motor_mode"})
_STRUCTURE_FIELDS = ("kind", "head", "body_yaw", "duration", "interpolation", "move", "sound",
                     "wobble", "reason", "speech", "pace", "antenna_voice", "tint", "supersede")
_DEFAULTS = {"speech": False, "pace": "fixed", "antenna_voice": False, "tint": "",
             "supersede": False}


@dataclass(frozen=True)
class Step:
    """One motion call. ``structure`` is everything but the time and the antenna angles."""

    t: float
    method: str
    structure: dict
    antennas: Optional[tuple[float, float]] = None
    duration: float = 0.0

    @property
    def reason(self) -> str:
        return str(self.structure.get("reason") or "")

    @property
    def kind(self) -> str:
        return str(self.structure.get("kind") or "")

    @property
    def voice_scaled(self) -> bool:
        return bool(self.structure.get("antenna_voice"))

    def key(self, *, digits: int = 6, with_antennas: bool = True) -> str:
        """A canonical string, for aligning two sequences. Floats are rounded for this only.

        Antennas on a voice-scaled move stay out: their values carry the live level.
        """

        material = dict(self.structure)
        if with_antennas and not self.voice_scaled:
            material["antennas"] = self.antennas
        return json.dumps(_rounded(material, digits), sort_keys=True)


def _rounded(value: Any, digits: int) -> Any:
    if isinstance(value, float):
        return round(value, digits) + 0.0
    if isinstance(value, dict):
        return {k: _rounded(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(v, digits) for v in value]
    return value


def step_from_call(record: dict) -> Optional[Step]:
    method = record.get("method")
    if method not in MOTION_METHODS:
        return None
    args = record.get("args") or {}
    t = float(record.get("t_start", 0.0))
    if method == "apply":
        intent = args.get("intent") or {}
        structure = {name: intent.get(name) for name in _STRUCTURE_FIELDS}
        for name, default in _DEFAULTS.items():
            if structure.get(name) is None:
                structure[name] = default
        raw = intent.get("antennas")
        antennas = (float(raw[0]), float(raw[1])) if raw else None
        return Step(t, method, structure, antennas, float(intent.get("duration") or 0.0))
    if method == "play_recorded_move":
        structure = {"kind": "play_recorded", "move": args.get("name"),
                     "supersede": bool(args.get("supersede"))}
        return Step(t, method, structure)
    if method == "stop_recorded_move":
        # It names a move id the robot handed out, so it matches by being there at all.
        return Step(t, method, {"kind": "stop_recorded"})
    return Step(t, method, {"kind": "wake_up"})


def steps_from_calls(records: Iterable[dict]) -> list[Step]:
    steps = [step_from_call(record) for record in records]
    return sorted((s for s in steps if s is not None), key=lambda s: s.t)


@dataclass
class PlanStreams:
    plan: list[Step]
    sustain: list[Step]
    idle: list[Step]
    # Settles the linger loop repeated, taken out with the beats they follow.
    linger_settles: list[Step]


def is_sustain(step: Step) -> bool:
    return step.reason.startswith(SUSTAIN_REASON)


def is_idle(step: Step) -> bool:
    return step.reason.startswith(IDLE_REASON)


def split_plan(steps: list[Step]) -> PlanStreams:
    """Separate the plan from the beats, their settles, and the idle gestures.

    A settle after a run of beats is the linger loop's only if the plan already played it:
    the same move as the last one kept. When the beats ran before the plan's settle, that
    settle is the plan's own and stays.
    """

    out = PlanStreams([], [], [], [])
    after_beats = False
    for step in steps:
        if is_idle(step):
            out.idle.append(step)
        elif is_sustain(step):
            out.sustain.append(step)
            after_beats = True
        else:
            if after_beats and out.plan and step.key() == out.plan[-1].key():
                out.linger_settles.append(step)
            else:
                out.plan.append(step)
            after_beats = False
    return out


def flag_edges(records: Iterable[dict]) -> list[dict]:
    """The gate pushes and motor modes as edges: a repeat of the same value is not one."""

    edges: list[dict] = []
    last: dict[str, Any] = {}
    for record in sorted(records, key=lambda r: float(r.get("t_start", 0.0))):
        method = record.get("method")
        if method not in FLAG_METHODS:
            continue
        args = record.get("args") or {}
        value = next(iter(args.values()), None)
        if method in last and last[method] == value:
            continue
        last[method] = value
        edges.append({"t": float(record.get("t_start", 0.0)), "flag": method, "value": value})
    return edges
