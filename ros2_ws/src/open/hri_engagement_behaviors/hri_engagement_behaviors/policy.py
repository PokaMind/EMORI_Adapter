"""Pure engagement policy: ROS4HRI signals in, at most ONE bounded skill request out.

This is the decision half of ``hri_engagement_behaviors`` and the half worth testing:
stdlib only, no ``rclpy``, no ``emori_adapter``, no clock of its own (the node injects
one).  ``behavior_node`` owns the ROS plumbing and calls exactly two methods here.

Three rules shape everything below:

* **One request per event.**  A recovery cycle does not emit three requests
  (``set_expression`` + ``replay_motion`` + ``look_at``), nor a sentence two.  The
  hostapp's own lane submits at most one batch per cycle into a 12-slot queue whose
  intake refuses a batch while two are already waiting (``drop_if_busy``), so
  three-per-cycle does not express more - it makes the queue drop the ones that
  mattered, and the drop lands on whichever request happened to be last.
* **Yield means yield.**  A cycle that has decided to stay out of the spoken sentence's
  way puts no request on the wire, not even an expression: it returns an empty decision.
* **An expression is state, not an event.**  It is requested only when the label
  actually changes, and only in place of the idle ``look_around`` (or of nothing at
  all).  It never displaces a recovery nod: the nod is the behaviour trying to bring a
  disengaging learner back, and a face is not a substitute for it.

The ladder mirrors ``emori_adapter.behavior.engine.BehaviorEngine.step`` (de-escalate,
recover, attention-seek, affirm, maintain) projected onto the bounded ``/api/skill``
vocabulary - copied rather than imported, because this package must build and run with
nothing from the adapter core installed (that is the point of this package).  The
duplication is pinned by ``tests/test_ros_contracts.py`` and ``tests/test_engagement_policy.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import math
from pathlib import Path

# REP-155 engagement levels: the wire values of hri_msgs/EngagementLevel, which is also
# what emori_msgs/Context.engagement_level carries.
UNKNOWN, DISENGAGED, ENGAGING, ENGAGED, DISENGAGING = 0, 1, 2, 3, 4
LEVEL_NAMES: tuple[str, ...] = (
    "UNKNOWN", "DISENGAGED", "ENGAGING", "ENGAGED", "DISENGAGING",
)

# The bounded `replay_motion` vocabulary of the /api/skill contract.
# A name outside this set is a 400 at the hostapp, so the tables below may only ever
# select from it - the ROS lane is deliberately NOT allowed to author motor values.
SKILL_MOTIONS = (
    "greeting", "farewell", "nod", "listening", "thinking",
    "calm_settle", "talking", "head_tilt", "antenna_wiggle", "look_around",
)
# The hri_msgs/Expression vocabulary the hostapp's set_expression accepts.
EXPRESSION_LABELS = (
    "neutral", "happy", "sad", "angry", "surprised", "scared", "disgusted",
)

# Cue family -> the closest bounded primitive (the six families read
# as: tension = stillness/sink, levity = livelier + open antennas, energy = the dense
# talking beats, intimacy = minimal motion, softening = a small nod, release = settle).
# The per-family motor recipes live hostapp-side in `behavior/cues.py`; the ROS lane can
# only ask for a *name*, and this is the honest projection onto it.
CUE_MOTIONS = {
    "tension": "calm_settle",
    "levity": "antenna_wiggle",
    "energy": "talking",
    "intimacy": "listening",
    "softening": "nod",
    "release": "calm_settle",
}
# A sentence with no family (or a family this build does not know) is plain talking beats.
CUE_MOTION_DEFAULT = "talking"

# The `behavior:` keys of configs/behavior.yaml this lane reads, mapped onto the fields
# below. `escalation_threshold` is the hostapp's name for the same number; both lanes
# must de-escalate at the same point or a rehearsal compares two different policies.
_YAML_TO_FIELD = {
    "escalation_threshold": "deescalate_at",
    "recovery_cooldown_cycles": "recovery_cooldown_cycles",
    "nod_when_engaged_every": "nod_when_engaged_every",
    "cue_max_s": "cue_max_s",
    "deescalated_k": "deescalated_k",
}
_INT_FIELDS = frozenset({"recovery_cooldown_cycles", "nod_when_engaged_every"})
# Two knobs may be overridden per container without editing the tuning file.
_ENV_TO_FIELD = (
    ("EMORI_CUE_DEESCALATE_AT", "deescalate_at"),
    ("EMORI_CUE_MAX_S", "cue_max_s"),
)


def cue_expression(family: str) -> str:
    """The hri_msgs/Expression vocabulary label a cue family reads as."""

    return {
        "tension": "scared",
        "levity": "happy",
        "energy": "happy",
        "intimacy": "neutral",
        "softening": "sad",
        "release": "neutral",
    }.get(family, "neutral")


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def intensity(valence: float, arousal: float) -> float:
    """The intensity scalar ``k``, clamped to ``[0.35, 1.0]``.

    Mirrors ``emori_adapter.behavior.cues.intensity`` deliberately rather than importing
    it: this package installs without the adapter core, and a skill request is worth more
    than an ImportError at node start.
    """

    magnitude = _clamp(math.hypot(float(valence), float(arousal)), 0.0, 1.0)
    return _clamp(0.55 + 0.45 * magnitude, 0.35, 1.0)


@dataclass
class PolicyConfig:
    """The five tunable numbers, plus where they came from (logged at startup).

    ``source`` exists because the previous failure mode was silent: ``EMORI_BEHAVIOR_CONFIG``
    never reached the container, the node fell back to code defaults that happened to
    equal the shipped file, and nothing on the robot could tell the two apart.
    """

    deescalate_at: float = 0.6
    recovery_cooldown_cycles: int = 2
    nod_when_engaged_every: int = 4
    cue_max_s: float = 4.0
    deescalated_k: float = 0.4
    fresh_default_k: float = 0.7
    source: str = "defaults"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PolicyConfig":
        """Read the ``behavior:`` section of a tuning file; unknown keys are ignored.

        The hostapp's ``BehaviorConfig.from_yaml`` ignores keys it does not know and so
        does this, which is what lets one file configure both lanes.
        """

        import yaml  # lazy: the pure half stays importable on a box with no yaml at all

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        section = data.get("behavior", data) if isinstance(data, dict) else {}
        if not isinstance(section, dict):
            section = {}
        values: dict[str, float] = {}
        for key, name in _YAML_TO_FIELD.items():
            if key not in section:
                continue
            try:
                values[name] = int(section[key]) if name in _INT_FIELDS else float(section[key])
            except (TypeError, ValueError):
                # A malformed number must not take the graph down; the default stands.
                continue
        return cls(source=f"yaml:{path}", **values)

    def with_env(self, env: Mapping[str, str]) -> "PolicyConfig":
        """Apply the two per-container overrides, but only when set AND parseable."""

        updated = replace(self)
        changed = False
        for name, field_name in _ENV_TO_FIELD:
            raw = str(env.get(name, "") or "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            setattr(updated, field_name, value)
            changed = True
        if changed and not updated.source.endswith("+env"):
            updated.source += "+env"
        return updated


@dataclass(frozen=True)
class CycleSignals:
    """One ``emori_msgs/Context`` cycle, reduced to what the policy actually reads."""

    seq: int
    engagement_level: int
    escalation: float
    valence: float
    arousal: float
    note: str = ""
    expression: str = ""
    expression_confidence: float = 0.0


@dataclass(frozen=True)
class SpeechCueSignals:
    """One ``emori_msgs/SpeechCue``: a sentence starting, or its end marker."""

    seq: int
    cue_category: str = ""
    est_duration: float = 0.0
    ended: bool = False


@dataclass(frozen=True)
class SkillRequest:
    """One bounded /api/skill request. ``kind`` is the skill name the intake accepts."""

    kind: str
    name: str = ""
    intensity: float = 0.0
    label: str = ""
    confidence: float = 0.0
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    duration: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    """What one event produced: zero or one request, a named outcome, and a log line."""

    requests: tuple[SkillRequest, ...]
    outcome: str
    log: str


class EngagementPolicy:
    """The ladder and the speech arbitration, with no ROS and no I/O.

    ``clock`` returns seconds from any monotonic-ish source; the node passes the ROS
    clock, tests pass a counter they advance by hand.
    """

    def __init__(self, config: PolicyConfig, *, clock: Callable[[], float]) -> None:
        self.config = config
        self._clock = clock
        self.counters: dict[str, int] = {
            "cycles": 0, "engagement": 0, "cue": 0, "yielded_to_speech": 0,
            "cooldown": 0, "expression_changes": 0, "settles": 0,
        }
        # The last affect seen, so a sentence arriving between cycles is scaled by the
        # learner in front of the robot rather than by a guess. `fresh` false = no cycle
        # yet this session -> k is fixed (affect suppression).
        self._valence = 0.0
        self._arousal = 0.0
        self._escalation = 0.0
        self._expression_confidence = 0.0
        self._fresh = False
        # Speech owns the head while a sentence is being spoken: the
        # per-cycle lane yields rather than interleaving into nonsense.
        self._speaking_until = 0.0
        # A cue with a real family is outstanding, i.e. the end marker owes exactly one
        # settle. Mirrors LiveDriver._cue_open: a session whose sentences never carried a
        # family must not gain one extra head motion per AI turn - least of all when the
        # mic opens, which is when the push-turn baseline matters most.
        self._cue_open = False
        self._last_recovery = -1_000_000
        # The robot's resting face. Asking for `neutral` on cycle 1 would be a request
        # that changes nothing, so the first cycle only speaks up when the learner is not
        # neutral - which is exactly when an expression is worth a queue slot.
        self._last_label = "neutral"

    # --- the per-cycle lane -------------------------------------------------
    def on_cycle(self, s: CycleSignals) -> Decision:
        cfg = self.config
        self._valence = float(s.valence)
        self._arousal = float(s.arousal)
        self._escalation = float(s.escalation)
        self._expression_confidence = float(s.expression_confidence)
        self._fresh = True
        k = intensity(self._valence, self._arousal)
        self.counters["cycles"] += 1
        index = self.counters["cycles"]
        level = int(s.engagement_level)

        # 1) De-escalation outranks everything, including the sentence being spoken: an
        #    agitated learner beats whatever the AI's voice is doing. This is the one
        #    cycle that preempts the cue lane, exactly as LiveDriver._submit_engagement
        #    does on the hostapp side.
        if self._escalation >= cfg.deescalate_at:
            self._speaking_until = 0.0
            self._cue_open = False
            # Counted on the engagement lane: a settle the ledger does not show is a
            # safety response nobody can prove happened.
            self.counters["engagement"] += 1
            return self._cycle_decision(s, self._replay(
                "calm_settle", k * cfg.deescalated_k, "engagement:deescalate"), "calm_settle")

        # 2) Yield means yield: nothing goes on the wire, not even an expression.
        if self._clock() < self._speaking_until:
            self.counters["yielded_to_speech"] += 1
            return self._cycle_decision(s, None, "yielded")

        # 3) The ladder, in BehaviorEngine.step's priority order.
        motion = ""
        outcome = ""
        if level == DISENGAGING or s.note == "attention-dropping":
            if index - self._last_recovery >= max(1, int(cfg.recovery_cooldown_cycles)):
                self._last_recovery = index
                motion = "nod"
            else:
                # The hostapp engine stays alert with an antennas-only intent; the ROS
                # lane has no antennas-only primitive, so the honest projection of
                # "recovery is on cooldown" is to ask for nothing at all this cycle.
                self.counters["cooldown"] += 1
                outcome = "cooldown"
        elif level == DISENGAGED:
            motion = "head_tilt"
        elif level == ENGAGING:
            motion = "antenna_wiggle"
        elif level == ENGAGED:
            # Same cadence as BehaviorEngine.step: every Nth cycle of the session, not
            # every Nth request, so the nod keeps a steady wall-clock rhythm.
            motion = ("nod" if index % max(1, int(cfg.nod_when_engaged_every)) == 0
                      else "look_around")

        # 4) An expression is state. It replaces the idle scan (or nothing), never a
        #    motion the ladder chose in order to repair or hold the interaction.
        label = (s.expression or "neutral").strip().lower()
        if label not in EXPRESSION_LABELS:  # defensive: the hostapp would answer 400
            label = "neutral"
        if label != self._last_label and motion in ("", "look_around"):
            self._last_label = label
            self.counters["expression_changes"] += 1
            # `confidence` is a MEASUREMENT of the learner's expression, not this
            # request's amplitude; k travels in `intensity`, the field that exists for it.
            request = SkillRequest(
                kind="set_expression", label=label,
                confidence=_clamp(s.expression_confidence, 0.0, 1.0),
                intensity=_clamp(k, 0.0, 1.0),
                reason=f"engagement:{_level_name(level).lower()}")
            return self._cycle_decision(s, request, "expression")

        if motion:
            self.counters["engagement"] += 1
            return self._cycle_decision(s, self._replay(motion, k, f"engagement:{motion}"), motion)
        return self._cycle_decision(s, None, outcome or "none")

    # --- the per-sentence lane ----------------------------------------------
    def on_speech_cue(self, c: SpeechCueSignals) -> Decision:
        cfg = self.config
        family = (c.cue_category or "").strip().lower()

        if c.ended:
            # The tail of the last plan dies with the voice; settle and hand the head
            # back to the per-cycle lane. ONLY if a cue was actually open: an
            # unconditional settle would be one extra head motion per AI turn on a
            # session whose sentences never carried a family.
            self._speaking_until = 0.0
            if not self._cue_open:
                return Decision((), "none", f"cue {int(c.seq)} end -> none")
            self._cue_open = False
            self.counters["settles"] += 1
            return Decision((self._replay("calm_settle", 0.5, "cue:end"),), "calm_settle",
                            f"cue {int(c.seq)} end -> calm_settle")

        motion = CUE_MOTIONS.get(family, CUE_MOTION_DEFAULT)
        self._cue_open = self._cue_open or family in CUE_MOTIONS
        # `fresh` false: no cycle has arrived yet, so do not guess a learner we have not
        # seen - a fixed middling k, no affect variant.
        k = intensity(self._valence, self._arousal) if self._fresh else cfg.fresh_default_k
        if self._escalation >= cfg.deescalate_at:
            motion, k = "calm_settle", k * cfg.deescalated_k
        # One sentence's motion budget is capped so a long plan can never
        # outlive the voice that asked for it, nor occupy the 12-slot queue.
        budget = _clamp(c.est_duration, 0.0, cfg.cue_max_s)
        self._speaking_until = self._clock() + budget
        self.counters["cue"] += 1
        # No set_expression per sentence: it was a second, competing <=2 s motion plan
        # for the same head, and the family's own replay is the one that carries meaning.
        return Decision(
            (self._replay(motion, k, f"cue:{family or 'none'}"),), motion,
            f"cue {int(c.seq)} family={family or '(none)'} budget={budget:.2f}s -> {motion}")

    # --- helpers ------------------------------------------------------------
    def _replay(self, name: str, k: float, reason: str) -> SkillRequest:
        return SkillRequest(kind="replay_motion", name=name,
                            intensity=_clamp(k, 0.0, 1.0), reason=reason)

    def _cycle_decision(self, s: CycleSignals, request: SkillRequest | None,
                        outcome: str) -> Decision:
        requests: tuple[SkillRequest, ...] = () if request is None else (request,)
        log = (f"cyc {int(s.seq):02d} {_level_name(int(s.engagement_level))} "
               f"esc={float(s.escalation):.3f} -> {outcome}")
        return Decision(requests, outcome, log)


def _level_name(level: int) -> str:
    """REP-155 level -> its name; anything off the scale reads as UNKNOWN, not a crash."""

    return LEVEL_NAMES[level] if 0 <= level < len(LEVEL_NAMES) else "UNKNOWN"
