"""The ROS engagement policy, exercised on a machine with no ROS.

``hri_engagement_behaviors`` decides what a robot does with a disengaging learner, and
until now that decision could only be run inside a container, on a robot, in front of a
person.  ``policy.py`` exists so it can be run here instead: it is stdlib-only, takes its
clock as an argument and imports nothing from ``emori_adapter`` (the package must
build with no EMORI core installed - ``tests/test_ros_standalone.py`` pins that).

The three properties this file is really about are the three defects the node shipped
with, each of which was invisible to a green suite:

* a recovery cycle sent THREE skill requests and every sentence two, into a 12-slot
  queue that refuses a batch while two are already waiting - so the extra requests did
  not add expression, they chose which gesture got dropped;
* the expression was published BEFORE the speech-yield check, so a cycle that had
  decided to stay out of the spoken sentence's way still put a request on the wire;
* the expression was re-sent every cycle instead of when it changed.

The module is loaded by PATH (``ros2_ws`` is not on ``sys.path`` and installing it would
need ROS), the same way ``tests/test_skill_facade.py`` loads the facade.
"""

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
import sys

import pytest

_REPO = Path(__file__).resolve().parents[1]
_POLICY = (_REPO / "ros2_ws" / "src" / "open" / "hri_engagement_behaviors"
           / "hri_engagement_behaviors" / "policy.py")
_REPO_CONFIG = _REPO / "configs" / "behavior.yaml"

_MODULE_NAME = "hri_engagement_policy_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _POLICY)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`; an unregistered module makes the decorator raise.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


policy = _load()


class _Clock:
    """A clock the test moves by hand, so no assertion below depends on wall time."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


def _make(**overrides):
    clock = _Clock()
    engine = policy.EngagementPolicy(policy.PolicyConfig(**overrides), clock=clock)
    return engine, clock


def _cycle(level: int, *, seq: int = 1, escalation: float = 0.0, valence: float = 0.0,
           arousal: float = 0.0, note: str = "", expression: str = "neutral",
           confidence: float = 0.9):
    return policy.CycleSignals(seq=seq, engagement_level=level, escalation=escalation,
                               valence=valence, arousal=arousal, note=note,
                               expression=expression, expression_confidence=confidence)


def _cue(*, seq: int = 1, family: str = "", est_duration: float = 1.0, ended: bool = False):
    return policy.SpeechCueSignals(seq=seq, cue_category=family,
                                   est_duration=est_duration, ended=ended)


# --- 1. the constants are the core's constants ---------------------------------------


def test_constants_match_the_core():
    """Four vocabularies are duplicated on purpose; a duplicate drifts unless pinned.

    The ROS package must install without ``emori_adapter``, so it cannot import these -
    but a motion name the hostapp does not know is a 400 on every gesture, and a level
    that disagrees with REP-155 is a ladder that fires the wrong rung.
    """

    from emori_adapter.behavior.cues import EXPRESSION_LABELS, REPLAY_MOTIONS
    from emori_adapter.models import EngagementLevel

    for level in EngagementLevel:
        assert getattr(policy, level.name) == int(level), level.name
    assert policy.LEVEL_NAMES == tuple(level.name for level in sorted(EngagementLevel))

    assert policy.SKILL_MOTIONS == REPLAY_MOTIONS
    assert policy.EXPRESSION_LABELS == EXPRESSION_LABELS

    # Every table in the module may only select from those two vocabularies.
    assert policy.CUE_MOTION_DEFAULT in REPLAY_MOTIONS
    for family, motion in policy.CUE_MOTIONS.items():
        assert motion in REPLAY_MOTIONS, family
        assert policy.cue_expression(family) in EXPRESSION_LABELS, family
    assert policy.cue_expression("a-family-this-build-never-heard-of") == "neutral"


# --- 2. the ladder ---------------------------------------------------------------------


@pytest.mark.parametrize("level, note, expected", [
    (policy.UNKNOWN, "", "none"),
    (policy.DISENGAGED, "", "head_tilt"),
    (policy.ENGAGING, "", "antenna_wiggle"),
    (policy.ENGAGED, "", "look_around"),
    (policy.DISENGAGING, "", "nod"),
    # The note is the translator's early warning and outranks the level it arrives with.
    (policy.ENGAGED, "attention-dropping", "nod"),
])
def test_the_ladder_maps_each_level(level, note, expected):
    engine, _ = _make()
    decision = engine.on_cycle(_cycle(level, note=note))

    assert decision.outcome == expected
    if expected == "none":
        assert decision.requests == ()
    else:
        assert [r.kind for r in decision.requests] == ["replay_motion"]
        assert decision.requests[0].name == expected
        assert decision.requests[0].reason == f"engagement:{expected}"
    assert decision.log.startswith(f"cyc 01 {policy.LEVEL_NAMES[level]} esc=0.000 -> ")


# --- 3. de-escalation preempts everything ----------------------------------------------


def test_escalation_preempts_speech_and_quarters_intensity():
    """Safety outranks the sentence being spoken - the one cycle that does not yield."""

    engine, clock = _make()
    engine.on_speech_cue(_cue(seq=1, family="energy", est_duration=4.0))
    assert engine._speaking_until == pytest.approx(4.0)

    clock.now = 1.0
    decision = engine.on_cycle(_cycle(policy.ENGAGED, seq=2, escalation=0.9))

    assert decision.outcome == "calm_settle"
    assert [(r.kind, r.name) for r in decision.requests] == [("replay_motion", "calm_settle")]
    assert decision.requests[0].reason == "engagement:deescalate"
    # k for a flat affect is 0.55; the de-escalated request carries k * deescalated_k.
    assert decision.requests[0].intensity == pytest.approx(0.55 * 0.4)
    # The preempt also ends the cue lane: the very next cycle must not be yielded, and
    # the sentence's end marker must not then owe a settle for a plan that was cancelled.
    assert engine._speaking_until == 0.0
    assert engine.on_speech_cue(_cue(seq=1, ended=True)).requests == ()


def test_a_yielded_cycle_publishes_nothing_not_even_an_expression():
    """An expression is not published above this check."""

    engine, clock = _make()
    engine.on_speech_cue(_cue(seq=1, family="levity", est_duration=2.0))

    clock.now = 0.5
    yielded = engine.on_cycle(_cycle(policy.ENGAGED, seq=2, expression="happy"))
    assert yielded.requests == ()
    assert yielded.outcome == "yielded"
    assert engine.counters["yielded_to_speech"] == 1
    assert engine.counters["expression_changes"] == 0

    # And the same expression still reaches the robot once the sentence is over: the
    # cycle yielded, it did not swallow the state change.
    clock.now = 2.5
    after = engine.on_cycle(_cycle(policy.ENGAGED, seq=3, expression="happy"))
    assert [(r.kind, r.label) for r in after.requests] == [("set_expression", "happy")]


def test_never_more_than_one_request_per_cycle():
    """The bounded-request rule, swept over every shape a cycle can take.

    Three requests per recovery cycle is what filled the hostapp's 12-slot queue; with
    ``drop_if_busy`` the queue then refused whichever one arrived last, so "more
    requests" meant "less reliable motion".
    """

    levels = (policy.UNKNOWN, policy.DISENGAGED, policy.ENGAGING,
              policy.ENGAGED, policy.DISENGAGING)
    for level, escalation, speaking, note, expression in itertools.product(
            levels, (0.0, 0.59, 0.6, 0.95), (False, True),
            ("", "attention-dropping"), ("neutral", "happy", "sad")):
        engine, clock = _make()
        if speaking:
            engine.on_speech_cue(_cue(seq=1, family="energy", est_duration=3.0))
            clock.now = 1.0
        for seq in range(1, 7):  # several cycles: cooldown and nod cadence both engage
            decision = engine.on_cycle(_cycle(level, seq=seq, escalation=escalation,
                                              note=note, expression=expression))
            assert len(decision.requests) <= 1, (level, escalation, speaking, note, seq)


# --- 4. recovery cooldown and the engaged nod cadence ----------------------------------


def test_recovery_honours_the_cooldown():
    """Same rule as BehaviorEngine.step: a recapture every N cycles, not every cycle."""

    engine, _ = _make(recovery_cooldown_cycles=2)
    outcomes = [engine.on_cycle(_cycle(policy.DISENGAGING, seq=seq)).outcome
                for seq in range(1, 7)]

    assert outcomes == ["nod", "cooldown", "nod", "cooldown", "nod", "cooldown"]
    assert engine.counters["cooldown"] == 3
    assert engine.counters["engagement"] == 3
    # A cooldown cycle asks for nothing at all - the ROS lane has no antennas-only
    # primitive to spend a queue slot on.
    engine2, _ = _make(recovery_cooldown_cycles=2)
    engine2.on_cycle(_cycle(policy.DISENGAGING, seq=1))
    assert engine2.on_cycle(_cycle(policy.DISENGAGING, seq=2)).requests == ()

    # Cooldown 1 means every cycle, which is the "no cooldown" configuration.
    engine3, _ = _make(recovery_cooldown_cycles=1)
    assert [engine3.on_cycle(_cycle(policy.DISENGAGING, seq=s)).outcome
            for s in (1, 2, 3)] == ["nod", "nod", "nod"]


def test_engaged_nods_every_nth_cycle():
    engine, _ = _make(nod_when_engaged_every=4)
    outcomes = [engine.on_cycle(_cycle(policy.ENGAGED, seq=seq)).outcome
                for seq in range(1, 9)]

    assert outcomes == ["look_around"] * 3 + ["nod"] + ["look_around"] * 3 + ["nod"]
    assert engine.counters["engagement"] == 8
    assert engine.counters["cycles"] == 8


# --- 5. an expression is state, not an event -------------------------------------------


def test_an_expression_change_replaces_look_around_but_not_a_recovery_nod():
    engine, _ = _make()

    # The idle scan is what an expression may take the place of.
    changed = engine.on_cycle(_cycle(policy.ENGAGED, seq=1, expression="happy",
                                     confidence=0.8))
    assert changed.outcome == "expression"
    assert [(r.kind, r.label, r.confidence) for r in changed.requests] == [
        ("set_expression", "happy", pytest.approx(0.8))]
    assert changed.requests[0].reason == "engagement:engaged"
    assert engine.counters["expression_changes"] == 1

    # Unchanged on the next cycle -> no second request; the idle scan comes back.
    assert engine.on_cycle(_cycle(policy.ENGAGED, seq=2, expression="happy")).outcome \
        == "look_around"
    assert engine.counters["expression_changes"] == 1

    # A recovery nod is the behaviour trying to bring the learner back; a face is not a
    # substitute for it, so the change waits rather than displacing the nod.
    recovery = engine.on_cycle(_cycle(policy.DISENGAGING, seq=3, expression="sad"))
    assert [(r.kind, r.name) for r in recovery.requests] == [("replay_motion", "nod")]
    assert engine.counters["expression_changes"] == 1
    # Cycle 4 is the engaged-nod cadence cycle, which is a real gesture and outranks a
    # face just as the recovery nod did.
    assert engine.on_cycle(_cycle(policy.ENGAGED, seq=4, expression="sad")).outcome == "nod"
    assert engine.counters["expression_changes"] == 1
    # ...and the change is still pending, so it goes out on the next cycle that has room.
    assert engine.on_cycle(_cycle(policy.ENGAGED, seq=5, expression="sad")).outcome \
        == "expression"

    # `neutral` is the resting face, so cycle 1 of a session does not spend a queue slot
    # announcing it.
    fresh, _ = _make()
    assert fresh.on_cycle(_cycle(policy.ENGAGED, expression="neutral")).outcome \
        == "look_around"
    # A label outside the hri_msgs vocabulary would be a 400; it reads as neutral.
    assert fresh.on_cycle(_cycle(policy.ENGAGED, seq=2, expression="smug")).outcome \
        == "look_around"


# --- 6. the per-sentence lane -----------------------------------------------------------


def test_a_sentence_is_one_request_and_owns_the_head_for_its_budget():
    """One request per sentence (no set_expression rides along)."""

    engine, clock = _make(cue_max_s=4.0)
    decision = engine.on_speech_cue(_cue(seq=7, family="levity", est_duration=10.0))

    assert [(r.kind, r.name) for r in decision.requests] == [
        ("replay_motion", "antenna_wiggle")]
    assert decision.requests[0].reason == "cue:levity"
    assert "budget=4.00s" in decision.log  # clamped to cue_max_s, not the 10 s estimate
    assert engine.counters["cue"] == 1

    clock.now = 3.9
    assert engine.on_cycle(_cycle(policy.ENGAGED, seq=1)).outcome == "yielded"
    clock.now = 4.0
    assert engine.on_cycle(_cycle(policy.ENGAGED, seq=2)).outcome != "yielded"

    # An unknown family is plain talking beats, and a nonsense duration is floored at 0
    # rather than parking the head for a negative time.
    clock.now = 10.0
    other = engine.on_speech_cue(_cue(seq=8, family="  BANANA ", est_duration=-3.0))
    assert other.requests[0].name == "talking"
    assert other.requests[0].reason == "cue:banana"
    assert engine._speaking_until == pytest.approx(10.0)

    # Escalation down-maps the family to a settle here too, on the same k rule.
    engine.on_cycle(_cycle(policy.ENGAGED, seq=3, escalation=0.9))
    escalated = engine.on_speech_cue(_cue(seq=9, family="energy", est_duration=2.0))
    assert escalated.requests[0].name == "calm_settle"
    assert escalated.requests[0].intensity == pytest.approx(0.55 * 0.4)


def test_the_end_marker_settles_only_when_a_cue_was_open():
    """An unconditional settle is one extra head motion per AI turn, every turn."""

    engine, _ = _make()
    assert engine.on_speech_cue(_cue(seq=1, ended=True)).requests == ()
    assert engine.counters["settles"] == 0

    # A sentence with no family never opens the cue lane, so its end owes nothing.
    engine.on_speech_cue(_cue(seq=2, family="", est_duration=1.0))
    assert engine.on_speech_cue(_cue(seq=2, ended=True)).requests == ()
    assert engine.counters["settles"] == 0

    # A real family does open it, and owes exactly one settle - once.
    engine.on_speech_cue(_cue(seq=3, family="tension", est_duration=1.0))
    end = engine.on_speech_cue(_cue(seq=3, ended=True))
    assert [(r.kind, r.name, r.reason) for r in end.requests] == [
        ("replay_motion", "calm_settle", "cue:end")]
    assert engine.counters["settles"] == 1
    assert engine.on_speech_cue(_cue(seq=3, ended=True)).requests == ()
    assert engine.counters["settles"] == 1


def test_no_cycle_yet_means_fixed_k():
    """Do not guess the affect of a learner no cycle has described yet."""

    engine, _ = _make(fresh_default_k=0.7)
    first = engine.on_speech_cue(_cue(seq=1, family="energy", est_duration=1.0))
    assert first.requests[0].intensity == pytest.approx(0.7)

    engine.on_cycle(_cycle(policy.ENGAGED, seq=1, valence=1.0, arousal=0.0))
    second = engine.on_speech_cue(_cue(seq=2, family="energy", est_duration=1.0))
    assert second.requests[0].intensity == pytest.approx(policy.intensity(1.0, 0.0))
    assert second.requests[0].intensity == pytest.approx(1.0)


# --- 7. configuration -------------------------------------------------------------------


def test_from_yaml_reads_escalation_threshold_and_ignores_unknown_keys(tmp_path):
    """One file, two lanes: each loader takes the keys it knows and ignores the rest."""

    path = tmp_path / "behavior.yaml"
    path.write_text(
        "behavior:\n"
        "  escalation_threshold: 0.42\n"
        "  nod_when_engaged_every: 7\n"
        "  cue_max_s: 2.5\n"
        "  backchannel_sound: 'mm-hm.wav'\n"   # hostapp-only; not a ROS knob
        "  weights: {a: 1}\n"                  # reserved, and not a number
        "translation:\n  engagement_high: 0.9\n",
        encoding="utf-8")

    config = policy.PolicyConfig.from_yaml(path)
    assert config.deescalate_at == pytest.approx(0.42)
    assert config.nod_when_engaged_every == 7
    assert config.cue_max_s == pytest.approx(2.5)
    assert config.deescalated_k == pytest.approx(0.4)      # untouched default
    assert config.recovery_cooldown_cycles == 2            # untouched default
    assert config.source == f"yaml:{path}"
    assert not hasattr(config, "backchannel_sound")

    # The shipped tuning file must configure BOTH lanes to de-escalate at the same
    # point, or a rehearsal with owner=ros compares two different policies.
    from emori_adapter.behavior import BehaviorConfig

    ros = policy.PolicyConfig.from_yaml(_REPO_CONFIG)
    hostapp = BehaviorConfig.from_yaml(_REPO_CONFIG)
    assert ros.deescalate_at == pytest.approx(hostapp.escalation_threshold)
    assert ros.recovery_cooldown_cycles == hostapp.recovery_cooldown_cycles
    assert ros.nod_when_engaged_every == hostapp.nod_when_engaged_every
    # ...and the two ROS-only keys are actually in the file (the container reads it).
    assert ros.cue_max_s == pytest.approx(4.0)
    assert ros.deescalated_k == pytest.approx(0.4)

    # A malformed number falls back rather than taking the graph down at node start.
    broken = tmp_path / "broken.yaml"
    broken.write_text("behavior:\n  escalation_threshold: 'soon'\n", encoding="utf-8")
    assert policy.PolicyConfig.from_yaml(broken).deescalate_at == pytest.approx(0.6)


def test_env_overrides_apply_only_when_set_and_parseable():
    base = policy.PolicyConfig()

    assert base.with_env({}) == base
    assert base.with_env({"EMORI_CUE_MAX_S": ""}).source == "defaults"
    assert base.with_env({"EMORI_CUE_MAX_S": "   "}).cue_max_s == pytest.approx(4.0)
    assert base.with_env({"EMORI_CUE_DEESCALATE_AT": "soon"}).source == "defaults"
    assert base.with_env({"EMORI_CUE_DEESCALATE_AT": "soon"}).deescalate_at \
        == pytest.approx(0.6)

    applied = base.with_env({"EMORI_CUE_MAX_S": "1.5", "EMORI_CUE_DEESCALATE_AT": "0.8"})
    assert applied.cue_max_s == pytest.approx(1.5)
    assert applied.deescalate_at == pytest.approx(0.8)
    assert applied.source == "defaults+env"
    assert base.cue_max_s == pytest.approx(4.0)  # the original is not mutated

    # The provenance stays readable when a file WAS loaded, which is the case the
    # startup log line exists to disambiguate.
    from_file = policy.PolicyConfig(source="yaml:/emori/configs/behavior.yaml")
    assert from_file.with_env({"EMORI_CUE_MAX_S": "2"}).source \
        == "yaml:/emori/configs/behavior.yaml+env"
    assert from_file.with_env({"EMORI_CUE_MAX_S": "2"}).with_env(
        {"EMORI_CUE_MAX_S": "3"}).source == "yaml:/emori/configs/behavior.yaml+env"
