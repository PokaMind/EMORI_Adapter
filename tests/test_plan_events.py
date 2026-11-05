"""The stream of what happened to each sentence's motion plan, and what it costs the motion.

The first half pins the cost: the motion is the same with the stream on and off, the motion
path only ever puts, a put takes no lock and cannot wait, and a full queue costs a record and
never a move. The second half makes each outcome come out of a real player on a recording
client, and the driver's records join to the trace and reach the metrics store once.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from emori_adapter import motion_trace, plan_events
from emori_adapter.hostapp import driver as driver_module
from emori_adapter.hostapp.driver import LiveDriver, RobotPlayer, SessionInfo
from emori_adapter.hostapp.metrics_store import MetricsStore
from emori_adapter.models import MotionIntent
from emori_adapter.plan_events import (
    ENDED,
    FINISHED,
    LOST,
    NO_VOICE,
    NOT_PERFORMED,
    PLANNED,
    PREEMPTED,
    REFUSED,
    SKIPPED,
    STARTED,
    UNFINISHED,
    VOICE_END,
    PlanEventReader,
    PlanEventStream,
    PlanJoin,
    SessionPlanCounts,
    batch_outcome,
)
from emori_adapter.robot.client import DryRunRobotClient

_DRIVER_SOURCE = Path(driver_module.__file__)
_STREAM_SOURCE = Path(plan_events.__file__)


def wait_for(predicate: Any, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _goto(reason: str = "cue", **fields: Any) -> MotionIntent:
    return MotionIntent(kind="goto", duration=fields.pop("duration", 0.12), antennas=(0.1, 0.1),
                        reason=reason, **fields)


class _Voice:
    """A voice a test scripts: nothing until ``speak``, and silence after ``stop_speaking``."""

    available = True
    level = 0.5

    def __init__(self) -> None:
        self.onset_count = 0
        self.active = False
        self._silent_s = 100.0

    def speak(self) -> None:
        self.onset_count += 1
        self.active = True
        self._silent_s = 0.0

    def stop_speaking(self) -> None:
        self.active = False
        self._silent_s = 100.0

    def silent_for_s(self) -> float:
        return self._silent_s

    def active_since(self, _t0: float) -> bool:
        return self.active

    def rose_since(self, _t0: float) -> bool:
        return False

    def wait_for_frame(self, timeout_s: float) -> None:
        time.sleep(min(timeout_s, 0.005))

    def wait_for_onset(self, timeout_s: float, *, stop: Any = None, abort: Any = None) -> bool:
        time.sleep(min(timeout_s, 0.005))
        return False


class _Bench:
    """A real player on a recording client, with the stream read from a queue here."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, voice: Any = None) -> None:
        monkeypatch.setattr(RobotPlayer, "_WAKE_SETTLE_S", 0.0)
        self.client = DryRunRobotClient()
        self.player = RobotPlayer(self.client, voice=voice)
        self.stream = PlanEventStream()
        self.queue = self.stream.subscribe(256)
        self.player.plan_events = self.stream
        self._plan = 100

    def submit(self, intents: list, *, guard: Any = None) -> int:
        self._plan += 1
        motion_trace._current.plan = self._plan
        try:
            assert self.player.submit(intents, guard=guard)
        finally:
            motion_trace.end_plan()
        return self._plan

    def records(self, plan: int) -> list[tuple]:
        return [event for event in self.queue.drain() if event[1] == plan]

    def wait_idle(self, timeout_s: float = 8.0) -> None:
        assert wait_for(lambda: not self.player.busy, timeout_s)

    def close(self) -> None:
        self.player.stop()
        self.player.join()


@pytest.fixture
def bench(monkeypatch):
    made: list[_Bench] = []

    def make(voice: Any = None) -> _Bench:
        made.append(_Bench(monkeypatch, voice))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _kinds(records: list[tuple]) -> list[str]:
    return [record[0] for record in records]


def _applied(client: DryRunRobotClient) -> list[str]:
    return [i.reason for i in client.applied if i.kind != "wake_up"]


# --- the motion is the same -----------------------------------------------------------


def _run_sentences(stream_on: bool) -> tuple[list, list]:
    client = DryRunRobotClient()
    driver = LiveDriver(client, metrics=_NoStore())
    if not stream_on:
        driver.plan_events = None
        driver.player.plan_events = None
    try:
        for seq, (text, cue) in enumerate((
                ("One plain sentence about the plan.", "softening"),
                ("And another that the planner reads for itself.", ""),
                ("A third, with a family of its own.", "tension")), start=1):
            driver.on_utterance({"text": text, "seq": seq, "cue_category": cue})
            assert wait_for(lambda: not driver.player.busy)
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    return ([(i.kind, i.reason, i.duration, i.antennas, i.head, i.supersede, i.move)
             for i in client.applied if i.kind != "wake_up"],
            list(client.recorded_played))


class _NoStore:
    """The driver's store when a test only wants its session's motion."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: None

    def recovery_counts(self, _session_id: str) -> dict:
        return {"attempts": 0, "successes": 0}


def test_the_motion_is_the_same_with_the_stream_on_and_off():
    """Same sentences, same commanded intents, in the same order. The stream only observes."""

    off = _run_sentences(stream_on=False)
    on = _run_sentences(stream_on=True)
    assert off == on and off[0]


def test_the_stream_works_with_the_trace_off():
    assert motion_trace.enabled() is False
    driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
    reader = PlanEventReader(driver.plan_events)
    try:
        driver.on_utterance({"text": "A sentence with the trace off.", "seq": 3,
                             "cue_category": "energy"})
        assert wait_for(lambda: not driver.player.busy)
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    steps = reader.drain()
    assert [(s.started, s.ended) for s in steps] == [
        (False, False), (True, False), (True, True)]
    assert steps[-1].plan_id > 0 and steps[-1].family == "energy"


def test_with_no_stream_a_sentence_gets_no_plan_id_at_all():
    """No consumer and no trace: nothing is named, and the player sees a batch with no plan."""

    driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
    driver.plan_events = None
    driver.player.plan_events = None
    seen: list[int] = []
    real = driver.player._play_batch

    def play(intents: list, guard: Any, plan: int = 0, *rest: Any) -> bool:
        seen.append(plan)
        return real(intents, guard, plan, *rest)

    driver.player._play_batch = play
    try:
        driver.on_utterance({"text": "A sentence nobody is reading.", "seq": 1})
        assert wait_for(lambda: seen and not driver.player.busy)
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    assert seen == [0]


# --- the motion path only puts ---------------------------------------------------------


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _stream_calls(tree: ast.AST) -> list[str]:
    """Every method called on the stream, by the names the driver's source holds it under."""

    called: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in ("tap", "plan_events"):
            called.append(node.func.attr)
    return called


def test_the_driver_only_ever_puts_on_the_stream():
    tree = ast.parse(_DRIVER_SOURCE.read_text(encoding="utf-8"))
    assert set(_stream_calls(tree)) == {"put"}
    # planned twice (with a plan, and with the kill-switch off), started, and ended twice.
    assert len(_stream_calls(tree)) == 5


def test_no_function_on_the_motion_path_reads_the_stream_or_writes_the_store():
    tree = ast.parse(_DRIVER_SOURCE.read_text(encoding="utf-8"))
    functions = _functions(tree)
    for name in ("_play_batch", "_run", "submit", "on_utterance", "_apply_traced"):
        attributes = {node.attr for node in ast.walk(functions[name])
                      if isinstance(node, ast.Attribute)}
        assert not attributes & {"take", "drain", "subscribe", "record_plan_counts",
                                 "_plan_counts", "metrics", "close_open"}, name


def test_the_counts_reach_the_store_only_where_a_session_ends():
    tree = ast.parse(_DRIVER_SOURCE.read_text(encoding="utf-8"))
    writers = {name for name, function in _functions(tree).items()
               for node in ast.walk(function)
               if isinstance(node, ast.Attribute) and node.attr == "record_plan_counts"}
    assert writers == {"_finalize_locked", "start_session", "shutdown"}


def test_a_put_takes_no_lock_and_cannot_wait():
    tree = ast.parse(_STREAM_SOURCE.read_text(encoding="utf-8"))
    names = {node.attr if isinstance(node, ast.Attribute) else node.id
             for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Name))}
    assert not names & {"Lock", "RLock", "Condition", "Semaphore", "Event", "Queue",
                        "sleep", "wait", "acquire"}
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "collections", "dataclasses"}
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    for owner in ("PlanEventQueue", "PlanEventStream"):
        put = _functions(classes[owner])["put"]
        assert not [node for node in ast.walk(put) if isinstance(node, (ast.With, ast.Try))]
    calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(_functions(classes["PlanEventQueue"])["put"])
             if isinstance(node, ast.Call)}
    assert calls == {"len", "append"}


def test_a_put_takes_about_a_microsecond_with_two_readers():
    """Not a benchmark: a bound that only a lock, a wait or I/O in there would break."""

    stream = PlanEventStream()
    stream.subscribe(1 << 20)
    stream.subscribe(4)
    event = (ENDED, 1, FINISHED, "")
    rounds = 100_000
    started = time.perf_counter()
    for _ in range(rounds):
        stream.put(event)
    assert (time.perf_counter() - started) / rounds < 5e-6


def test_a_drain_in_another_thread_does_not_hold_a_put_up():

    stream = PlanEventStream()
    queue = stream.subscribe(64)
    stop = threading.Event()

    def drain() -> None:
        while not stop.is_set():
            queue.drain()

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        for i in range(20_000):
            stream.put((ENDED, i, FINISHED, ""))
        assert time.monotonic() - started < 2.0
    finally:
        stop.set()
        thread.join(5.0)


# --- a full queue ----------------------------------------------------------------------


def test_a_full_queue_keeps_what_it_has_and_counts_the_rest():
    stream = PlanEventStream()
    queue = stream.subscribe(2)
    for plan in (1, 2, 3, 4):
        stream.put((ENDED, plan, FINISHED, ""))
    assert queue.dropped == 2
    assert [event[1] for event in queue.drain()] == [1, 2]
    stream.put((ENDED, 5, FINISHED, ""))
    assert [event[1] for event in queue.drain()] == [5]
    assert queue.dropped == 2


def test_one_reader_full_costs_the_other_reader_nothing():
    stream = PlanEventStream()
    small, big = stream.subscribe(1), stream.subscribe(8)
    for plan in (1, 2, 3):
        stream.put((ENDED, plan, FINISHED, ""))
    assert (small.dropped, big.dropped) == (2, 0)
    assert len(big.drain()) == 3


def test_a_full_queue_costs_an_event_and_never_the_motion(bench):
    full = bench()
    full.queue.capacity = 0
    empty = bench()
    plan = full.submit([_goto("a"), _goto("b"), _goto("c")])
    empty.submit([_goto("a"), _goto("b"), _goto("c")])
    full.wait_idle()
    empty.wait_idle()
    assert _applied(full.client) == _applied(empty.client) == ["a", "b", "c"]
    assert full.records(plan) == []
    # started and ended, both counted.
    assert full.queue.dropped == 2


# --- each outcome, from a real player --------------------------------------------------


def test_finished(bench):
    r = bench()
    plan = r.submit([_goto("a"), _goto("b")])
    r.wait_idle()
    records = r.records(plan)
    assert _kinds(records) == [STARTED, ENDED]
    assert records[-1][2:] == (FINISHED, "")
    assert _applied(r.client) == ["a", "b"]


def test_preempted_mid_plan(bench):
    r = bench()
    live = [True]
    # A hold far longer than the test: only the revoke can end it.
    plan = r.submit([_goto("a", duration=5.0), _goto("b"), _goto("c"), _goto("d")],
                    guard=lambda: live[0])
    assert wait_for(lambda: _applied(r.client) == ["a"])
    live[0] = False
    r.player.wake()
    r.wait_idle()
    records = r.records(plan)
    assert _kinds(records) == [STARTED, ENDED]
    assert records[-1][2:] == (PREEMPTED, "")
    assert _applied(r.client) == ["a"]


def test_a_batch_the_next_sentence_overtook_in_the_queue_is_preempted_and_never_started(bench):
    r = bench()
    live = [True]
    r.submit([_goto("ahead", duration=0.5)])
    assert wait_for(lambda: _applied(r.client) == ["ahead"])
    plan = r.submit([_goto("behind")], guard=lambda: live[0])
    live[0] = False
    r.wait_idle()
    assert r.records(plan) == [(ENDED, plan, PREEMPTED, "")]
    assert _applied(r.client) == ["ahead"]


def test_preempted_while_waiting_for_the_voice_is_not_a_start(bench):
    voice = _Voice()
    r = bench(voice)
    live = [True]
    plan = r.submit([_goto("a", speech=True), _goto("settle", speech=True)],
                    guard=lambda: live[0])
    assert wait_for(lambda: r.player.busy)
    time.sleep(0.05)
    live[0] = False
    r.wait_idle()
    assert r.records(plan) == [(ENDED, plan, PREEMPTED, "")]
    assert _applied(r.client) == []


def test_a_plan_overtaken_with_only_its_settle_left_has_finished(bench):
    r = bench()
    live = [True]
    plan = r.submit([_goto("a"), _goto("last", duration=5.0), _goto("settle")],
                    guard=lambda: live[0])
    assert wait_for(lambda: _applied(r.client) == ["a", "last"])
    live[0] = False
    r.player.wake()
    r.wait_idle()
    assert r.records(plan)[-1][2:] == (FINISHED, "")


def test_a_recording_the_robot_refused_did_not_perform(bench):
    r = bench()
    r.client.play_recorded_move = lambda *_a, **_k: None
    plan = r.submit([_recorded(), _goto("settle")])
    r.wait_idle()
    assert r.records(plan)[-1][2:] == (SKIPPED, REFUSED)


def test_cut_by_the_voices_end(bench):
    voice = _Voice()
    r = bench(voice)
    voice.speak()
    plan = r.submit([_goto("a", speech=True, duration=5.0), _goto("b", speech=True),
                     _goto("c", speech=True), _goto("settle", speech=True)])
    assert wait_for(lambda: _applied(r.client)[:1] == ["a"])
    voice.stop_speaking()
    r.wait_idle()
    records = r.records(plan)
    assert _kinds(records) == [STARTED, ENDED]
    assert records[-1][2:] == (VOICE_END, "")
    assert _applied(r.client) == ["a", "settle"]


def test_skipped_because_the_voice_never_came(bench, monkeypatch):
    monkeypatch.setattr(RobotPlayer, "_VOICE_HOLD_S", 0.05)
    voice = _Voice()
    r = bench(voice)
    plan = r.submit([_goto("a", speech=True), _goto("settle", speech=True)])
    r.wait_idle()
    # The settle went out, and is not the plan having started.
    assert r.records(plan) == [(ENDED, plan, SKIPPED, NO_VOICE)]
    assert _applied(r.client) == ["settle"]


def _recorded() -> MotionIntent:
    return MotionIntent(kind="play_recorded", move="oops1", duration=3.0, speech=True,
                        reason="recorded")


def test_a_recorded_move_cut_by_the_voices_end(bench):
    voice = _Voice()
    r = bench(voice)
    voice.speak()
    plan = r.submit([_recorded(), _goto("settle", speech=True)])
    assert wait_for(lambda: r.client.recorded_played == ["oops1"])
    voice.stop_speaking()
    r.wait_idle()
    assert r.records(plan)[-1][2:] == (VOICE_END, "")
    assert len(r.client.recorded_stopped) == 1
    assert _applied(r.client) == ["settle"]


def test_a_recorded_move_stopped_by_a_newer_sentence_is_preempted(bench):
    """The whole performance is one intent, so its index alone would read as finished."""

    voice = _Voice()
    r = bench(voice)
    voice.speak()
    live = [True]
    plan = r.submit([_recorded(), _goto("settle", speech=True)], guard=lambda: live[0])
    assert wait_for(lambda: r.client.recorded_played == ["oops1"])
    live[0] = False
    r.wait_idle()
    records = r.records(plan)
    assert _kinds(records) == [STARTED, ENDED]
    assert records[-1][2:] == (PREEMPTED, "")
    assert len(r.client.recorded_stopped) == 1


def test_a_batch_with_no_plan_says_nothing(bench):
    r = bench()
    assert r.player.submit([_goto("greeting")])
    r.wait_idle()
    assert r.queue.drain() == []


def test_the_first_command_time_is_put_once_however_long_the_plan(bench):
    r = bench()
    plan = r.submit([_goto("a"), _goto("b"), _goto("c")])
    r.wait_idle()
    assert _kinds(r.records(plan)).count(STARTED) == 1


# --- the rules, one by one -------------------------------------------------------------


@pytest.mark.parametrize(("n", "issued", "flags", "expected"), [
    (1, 0, {}, (FINISHED, "")),
    (1, -1, {}, (PREEMPTED, "")),
    (2, 0, {}, (FINISHED, "")),
    (4, 3, {}, (FINISHED, "")),
    (4, 2, {}, (FINISHED, "")),
    (4, 1, {}, (PREEMPTED, "")),
    (4, -1, {}, (PREEMPTED, "")),
    (4, 3, {"voice_cut": True}, (VOICE_END, "")),
    (4, 3, {"no_voice": True}, (SKIPPED, NO_VOICE)),
    (2, 0, {"preempted": True}, (PREEMPTED, "")),
    (2, 1, {"preempted": True, "voice_cut": True}, (PREEMPTED, "")),
])
def test_how_a_batch_is_named(n, issued, flags, expected):
    named = {"no_voice": False, "voice_cut": False, "preempted": False, **flags}
    assert batch_outcome(n, issued, **named) == expected


# --- skipped before it reaches the player ----------------------------------------------


def _one_sentence(envelope: dict, *, prepare: Any = None) -> list[Any]:
    driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
    reader = PlanEventReader(driver.plan_events)
    if prepare is not None:
        prepare(driver)
    try:
        driver.on_utterance(envelope)
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    return reader.drain()


def test_skipped_while_the_head_is_an_input():
    (step,) = _one_sentence({"text": "Said over a reserved head.", "seq": 2,
                             "cue_category": "levity"},
                            prepare=lambda d: d.set_head_reserved(lambda: True))
    assert (step.ended, step.outcome, step.skip_reason) == (True, SKIPPED, "head_reserved")
    assert (step.family, step.family_authored, step.family_source) == ("", "levity", "")
    assert step.slug == "head_reserved" and step.seq == 2 and step.plan_id > 0


def test_skipped_when_the_motion_lane_is_closed():
    (step,) = _one_sentence({"text": "Said while another lane owns the robot.", "seq": 3,
                             "cue_category": "energy"},
                            prepare=lambda d: d.set_legacy_motion_enabled(False))
    assert (step.outcome, step.skip_reason, step.family) == (SKIPPED, "not_owner", "")
    assert step.family_authored == "energy"


def test_skipped_with_no_words_and_no_family_it_knows():
    (step,) = _one_sentence({"text": "", "seq": 4, "cue_category": "not_a_family"})
    assert (step.outcome, step.skip_reason) == (SKIPPED, "empty_text")


def test_skipped_a_cue_with_no_words_on_a_robot_with_no_voice_signal():
    (step,) = _one_sentence({"text": "   ", "seq": 5, "cue_category": "levity"})
    assert (step.outcome, step.skip_reason, step.family) == (SKIPPED, "cue_only", "")
    assert step.slug == "cue_only/levity"


def test_skipped_with_the_kill_switch_off(monkeypatch):
    monkeypatch.setenv("EMORI_CUE_MOTION", "0")
    (step,) = _one_sentence({"text": "The fixed talking gesture.", "seq": 6,
                             "cue_category": "tension"})
    assert (step.outcome, step.skip_reason, step.family_authored) == (
        SKIPPED, "cue_motion_off", "tension")
    assert step.plan_id > 0


def test_a_double_delivery_is_not_a_sentence():
    driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
    reader = PlanEventReader(driver.plan_events)
    driver.set_head_reserved(lambda: True)
    try:
        for _ in range(2):
            driver.on_utterance({"text": "The same line, delivered twice.", "seq": 9})
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    assert len(reader.drain()) == 1


def test_a_queue_full_of_batches_is_named_like_a_closed_lane():
    """The scheduler cannot tell the two apart: it was refused, so nothing will perform."""

    class Refusing(RobotPlayer):
        def submit(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

    driver = LiveDriver(DryRunRobotClient(), player=Refusing(DryRunRobotClient()),
                        metrics=_NoStore())
    reader = PlanEventReader(driver.plan_events)
    try:
        driver.on_utterance({"text": "Nobody will perform this.", "seq": 1})
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    (step,) = reader.drain()
    assert (step.outcome, step.skip_reason) == (SKIPPED, "not_owner")


# --- what a record says about a sentence ------------------------------------------------


def test_a_record_holds_no_words_and_no_direction():
    driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
    reader = PlanEventReader(driver.plan_events)
    queue = driver.plan_events.subscribe(64)
    try:
        driver.on_utterance({"text": "The private words of a sentence.", "seq": 4,
                             "cue_category": "tension", "cue_body": "a hushed direction"})
        assert wait_for(lambda: not driver.player.busy)
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()
    assert reader.drain()
    assert not [event for event in queue.drain()
                if "private" in repr(event) or "hushed" in repr(event)]


def test_the_stream_agrees_with_the_trace_on_every_sentence(tmp_path):
    """The two ways to read a sentence's plan say the same thing, and share its id."""

    import json

    assert motion_trace.start(tmp_path) is not None
    try:
        driver = LiveDriver(DryRunRobotClient(), metrics=_NoStore())
        reader = PlanEventReader(driver.plan_events)
        try:
            for seq, cue in enumerate(("tension", "", "levity"), start=1):
                driver.on_utterance({"text": f"Sentence number {seq} of a short reply.",
                                     "seq": seq, "cue_category": cue})
                assert wait_for(lambda: not driver.player.busy)
        finally:
            driver.shutdown(timeout_s=1.0)
            driver.player.stop()
            driver.player.join()
        steps = reader.drain()
    finally:
        motion_trace.stop()

    records = [json.loads(line) for path in tmp_path.glob("*.jsonl")
               for line in path.read_text(encoding="utf-8").splitlines()]
    sentences = {r["plan"]: r for r in records if r["s"] == "sentence"}
    ends = {r["plan"]: r for r in records if r["s"] == "batch_end"}
    first_apply: dict[int, float] = {}
    for r in records:
        if r["s"] == "apply":
            first_apply.setdefault(r["plan"], r["t"])
    last = [s for s in steps if s.ended]
    assert len(last) == 3
    for step in last:
        sentence = sentences[step.plan_id]
        assert sentence["seq"] == step.seq
        assert sentence["performed"] == step.family
        assert sentence["authored"] == step.family_authored
        assert sentence["family_source"] == step.family_source
        assert sentence["slug"] == step.slug
        assert ends[step.plan_id]["outcome"] == step.outcome
        assert ends[step.plan_id]["skip_reason"] == step.skip_reason
        delay = first_apply[step.plan_id] - sentence["t"]
        assert step.first_apply_delay_s == pytest.approx(delay, abs=0.02)


# --- the join --------------------------------------------------------------------------


def _planned(plan: int, *, family: str = "energy", skip: str = "", at: float = 10.0) -> tuple:
    return (PLANNED, plan, plan * 10, family, "tension", "authored", "slug", at, skip)


def test_a_players_record_that_comes_before_the_plan_is_named_waits_for_it():
    join = PlanJoin()
    assert join.feed((STARTED, 1, 10.5)) == []
    assert join.feed((ENDED, 1, FINISHED, "")) == []
    planned, started, ended = join.feed(_planned(1))
    assert (planned.started, planned.ended) == (False, False)
    assert (started.started, started.first_apply_delay_s) == (True, 0.5)
    assert (ended.ended, ended.outcome, ended.family) == (True, FINISHED, "energy")
    assert join.unmatched == 0


def test_a_step_says_everything_known_of_its_plan_by_then():
    join = PlanJoin()
    (planned,) = join.feed(_planned(2))
    (started,) = join.feed((STARTED, 2, 12.25))
    (ended,) = join.feed((ENDED, 2, VOICE_END, ""))
    assert planned.first_apply_delay_s == plan_events.NOT_STARTED
    assert started.first_apply_delay_s == 2.25
    assert (ended.started, ended.first_apply_delay_s, ended.outcome) == (True, 2.25, VOICE_END)
    assert (ended.family, ended.family_authored, ended.family_source, ended.slug, ended.seq) == (
        "energy", "tension", "authored", "slug", 20)


def test_a_plan_that_ended_without_starting_has_no_delay():
    join = PlanJoin()
    join.feed(_planned(3))
    (ended,) = join.feed((ENDED, 3, PREEMPTED, ""))
    assert (ended.started, ended.first_apply_delay_s) == (False, plan_events.NOT_STARTED)


def test_a_join_cannot_grow_without_bound():
    join = PlanJoin()
    for plan in range(1, 1001):
        join.feed(_planned(plan))
        join.feed((ENDED, plan + 100_000, FINISHED, ""))
    assert len(join._open) <= 64
    assert len(join._held) <= 64
    assert join.unmatched == 1000 - 64


# --- the counts, in memory --------------------------------------------------------------


def test_counts_are_by_family_and_outcome_and_each_take_starts_again():
    stream = PlanEventStream()
    counts = SessionPlanCounts(stream)
    for plan, family, outcome in ((1, "energy", FINISHED), (2, "energy", FINISHED),
                                  (3, "energy", PREEMPTED), (4, "", VOICE_END)):
        stream.put(_planned(plan, family=family))
        stream.put((STARTED, plan, 11.0))
        stream.put((ENDED, plan, outcome, ""))
    stream.put(_planned(5, family="", skip="head_reserved"))
    assert counts.take() == {("energy", FINISHED): 2, ("energy", PREEMPTED): 1,
                             ("", VOICE_END): 1, (NOT_PERFORMED, SKIPPED): 1}
    assert counts.take() == {}


def test_a_plan_that_moved_nothing_is_not_counted_as_performing_its_family():
    stream = PlanEventStream()
    counts = SessionPlanCounts(stream)
    stream.put(_planned(1))
    stream.put((ENDED, 1, PREEMPTED, ""))  # dropped at pickup: never started
    stream.put(_planned(2))
    stream.put((STARTED, 2, 11.0))
    stream.put((ENDED, 2, SKIPPED, REFUSED))  # the robot said no to its recording
    stream.put(_planned(3))
    assert counts.take() == {(NOT_PERFORMED, PREEMPTED): 1, (NOT_PERFORMED, SKIPPED): 1,
                             (NOT_PERFORMED, UNFINISHED): 1}


def test_a_refused_recording_is_a_skipped_plan_and_the_trace_can_say_so():
    assert batch_outcome(2, 1, no_voice=False, voice_cut=False, preempted=False,
                         refused=True) == (SKIPPED, REFUSED)
    assert batch_outcome(2, 1, no_voice=True, voice_cut=False, preempted=False,
                         refused=True) == (SKIPPED, NO_VOICE)


def test_a_plan_still_running_at_the_end_counts_as_that_once():
    stream = PlanEventStream()
    counts = SessionPlanCounts(stream)
    stream.put(_planned(1))
    stream.put((STARTED, 1, 11.0))
    assert counts.take() == {("energy", UNFINISHED): 1}
    # Its end arrives during the next session and is nobody's.
    stream.put((ENDED, 1, PREEMPTED, ""))
    assert counts.take() == {}


def test_what_the_queue_lost_is_counted_once_as_lost():
    stream = PlanEventStream()
    counts = SessionPlanCounts(stream, capacity=2)
    for plan in (1, 2, 3):
        stream.put(_planned(plan, skip="head_reserved"))
    assert counts.take() == {(NOT_PERFORMED, SKIPPED): 2, ("", LOST): 1}
    assert counts.take() == {}


# --- the counts, at a session's end -----------------------------------------------------


def _info(session_id: str) -> SessionInfo:
    return SessionInfo(session_id=session_id, person_id="p", topic="t", lang="en")


@pytest.fixture
def store(tmp_path):
    made = MetricsStore(str(tmp_path / "dash.db"), salt="unit-test-salt")
    yield made
    made.close()


def _rows(store: MetricsStore) -> dict:
    return {(r["session_id"], r["family"], r["outcome"]): r["n"]
            for r in store.query("SELECT session_id, family, outcome, n FROM plan_counts")}


def _driver(store: MetricsStore) -> LiveDriver:
    return LiveDriver(DryRunRobotClient(), metrics=store)


def _stop(driver: LiveDriver) -> None:
    driver.shutdown(timeout_s=1.0)
    driver.player.stop()
    driver.player.join()


def test_a_session_writes_once_at_its_end_and_never_per_sentence(store, monkeypatch):
    driver = _driver(store)
    writes: list[dict] = []
    real = store.record_plan_counts

    def recording(session_id: str, counts: Any) -> bool:
        writes.append(dict(counts))
        return real(session_id, counts)

    monkeypatch.setattr(store, "record_plan_counts", recording)
    try:
        driver.start_session(_info("s1"))
        writes.clear()
        driver.set_head_reserved(lambda: True)
        for seq in (1, 2, 3):
            driver.on_utterance({"text": f"Sentence {seq}.", "seq": seq, "cue_category": "energy"})
        assert writes == [] and _rows(store) == {}
        driver.on_completed({"session_id": "s1", "status": "failed"})
    finally:
        _stop(driver)
    assert writes[0] == {(NOT_PERFORMED, SKIPPED): 3}
    assert [w for w in writes if w] == [writes[0]]
    assert _rows(store) == {("s1", NOT_PERFORMED, SKIPPED): 3}


def test_a_session_that_ends_abnormally_still_writes_what_it_has(store):
    driver = _driver(store)
    try:
        driver.start_session(_info("walked-off"))
        driver.set_head_reserved(lambda: True)
        driver.on_utterance({"text": "Then nobody answered.", "seq": 1})
        with driver._lock:
            driver._finalize_locked(results=None, end_reason="watchdog_force_ends")
    finally:
        _stop(driver)
    assert _rows(store) == {("walked-off", NOT_PERFORMED, SKIPPED): 1}


def test_a_session_replaced_before_its_first_cycle_writes_what_it_said(store):
    """It reaches no finalize at all, and the next session must not inherit its sentences."""

    driver = _driver(store)
    try:
        driver.start_session(_info("first"))
        driver.set_head_reserved(lambda: True)
        driver.on_utterance({"text": "The intro line and nothing else.", "seq": 1})
        driver.start_session(_info("second"))
        driver.on_utterance({"text": "A line of the second session.", "seq": 2})
        driver.on_utterance({"text": "And another one.", "seq": 3})
        with driver._lock:
            driver._finalize_locked(results=None)
    finally:
        _stop(driver)
    assert _rows(store) == {("first", NOT_PERFORMED, SKIPPED): 1,
                            ("second", NOT_PERFORMED, SKIPPED): 2}


def test_a_stop_does_not_wait_past_its_limit_for_the_driver_to_come_free(store):
    """A finalize holds this lock across a broker call, and a stop has a deadline."""

    driver = _driver(store)
    driver.start_session(_info("held"))
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with driver._lock:
            held.set()
            release.wait(10.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(5.0)
        started = time.monotonic()
        driver.shutdown(timeout_s=0.1)
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        holder.join(5.0)
        _stop(driver)


def test_a_process_stopping_mid_session_writes_what_it_has(store):
    driver = _driver(store)
    try:
        driver.start_session(_info("cut-off"))
        driver.set_head_reserved(lambda: True)
        driver.on_utterance({"text": "Just before the stop.", "seq": 1})
    finally:
        _stop(driver)
    assert _rows(store) == {("cut-off", NOT_PERFORMED, SKIPPED): 1}


def test_what_the_robot_says_between_sessions_is_nobodys(store):
    driver = _driver(store)
    try:
        driver.set_head_reserved(lambda: True)
        driver.on_utterance({"text": "A page left open with no session.", "seq": 1})
        driver.start_session(_info("s1"))
        with driver._lock:
            driver._finalize_locked(results=None)
    finally:
        _stop(driver)
    assert _rows(store) == {}


def test_a_store_that_fails_costs_the_counts_and_not_the_ending(tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    driver = LiveDriver(DryRunRobotClient(),
                        metrics=MetricsStore(str(blocked / "dash.db"), salt="unit-test-salt"))
    try:
        driver.start_session(_info("s1"))
        driver.on_utterance({"text": "Said to a robot with no card.", "seq": 1})
        with driver._lock:
            stats = driver._finalize_locked(results=None)
    finally:
        _stop(driver)
    assert stats["session"].session_id == "s1"
    assert driver.tracker.active is False
