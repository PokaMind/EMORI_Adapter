"""The motion trace: what it records, what it refuses to record, and what it costs.

A trace that changes the motion path, fills the card or leaks a sentence would be worse
than no trace, so those three are pinned alongside the join the report depends on.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from emori_adapter import motion_trace
from emori_adapter.hostapp.driver import LiveDriver, RobotPlayer
from emori_adapter.hostapp.voice_link import VoiceLink
from emori_adapter.models import MotionIntent
from emori_adapter.motion_trace import MotionTrace
from emori_adapter.robot.client import DryRunRobotClient


def read_records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_for(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def trace(tmp_path):
    active = motion_trace.start(tmp_path)
    assert active is not None
    yield active
    motion_trace.stop()


def all_records(tmp_path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(tmp_path.glob("*.jsonl")):
        records.extend(read_records(path))
    return records


# --- off by default ---------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    (None, False), ("", False), ("0", False), ("off", False),
    ("1", True), ("true", True), ("on", True),
])
def test_the_knob_is_off_unless_set(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("EMORI_MOTION_TRACE", raising=False)
    else:
        monkeypatch.setenv("EMORI_MOTION_TRACE", raw)
    assert motion_trace.default_enabled() is expected


def test_not_started_means_no_work_and_no_plan_ids(tmp_path):
    assert motion_trace.enabled() is False
    motion_trace.emit("apply", plan=1)
    motion_trace.set_session("s1")
    assert motion_trace.begin_plan() == 0
    assert motion_trace.current_plan() == 0
    assert motion_trace.snapshot() == {"enabled": False}
    assert list(tmp_path.iterdir()) == []


# --- the file ---------------------------------------------------------------
def test_records_go_to_their_sessions_file_under_a_meta_line(tmp_path):
    t = MotionTrace(tmp_path, clock=lambda: 12.34567)
    t.emit("apply", {"plan": 7})
    t.flush()
    t.set_session("abc/../123")
    t.emit("apply", {"plan": 8})
    t.set_session(None)

    idle = read_records(tmp_path / "no-session.jsonl")
    assert idle[0]["s"] == "meta" and idle[0]["schema"] == motion_trace.SCHEMA_VERSION
    assert idle[1] == {"t": 12.3457, "s": "apply", "plan": 7}
    assert [r["plan"] for r in read_records(tmp_path / "abc_.._123.jsonl")[1:]] == [8]


def test_an_explicit_time_wins_over_the_clock(tmp_path):
    t = MotionTrace(tmp_path, clock=lambda: 99.0)
    t.emit("voice", {"edge": "rise"}, at=1.5)
    t.flush()
    assert read_records(tmp_path / "no-session.jsonl")[1]["t"] == 1.5


def test_a_full_buffer_keeps_the_newest_and_counts_the_loss(tmp_path):
    t = MotionTrace(tmp_path, capacity=3)
    for i in range(5):
        t.emit("apply", {"i": i})
    assert t.dropped == 2
    t.flush()
    assert [r["i"] for r in read_records(tmp_path / "no-session.jsonl")[1:]] == [2, 3, 4]
    assert t.dropped == 2


def test_low_disk_stops_writing_and_counts_the_loss(tmp_path):
    t = MotionTrace(tmp_path, min_free_bytes=100, disk_free=lambda _p: 99)
    t.emit("apply", {"i": 1})
    assert t.flush() == 0
    assert t.dropped == 1
    assert list(tmp_path.glob("*.jsonl")) == []


def test_a_full_file_stops_growing(tmp_path):
    t = MotionTrace(tmp_path, max_file_bytes=10)
    t.emit("apply", {"i": 1})
    t.flush()
    size = (tmp_path / "no-session.jsonl").stat().st_size
    t.emit("apply", {"i": 2})
    assert t.flush() == 0
    assert (tmp_path / "no-session.jsonl").stat().st_size == size
    assert t.dropped == 1


def test_the_directory_budget_removes_the_oldest_files_only(tmp_path):
    import os

    old = tmp_path / "old.jsonl"
    old.write_text("x" * 600)
    os.utime(old, (1000, 1000))
    mid = tmp_path / "mid.jsonl"
    mid.write_text("x" * 300)
    os.utime(mid, (2000, 2000))

    t = MotionTrace(tmp_path, max_dir_bytes=500)
    t.set_session("new")
    t.emit("apply", {"i": 1})
    t.flush()

    assert not old.exists()
    assert mid.exists()
    assert (tmp_path / "new.jsonl").exists()


def test_a_write_failure_is_counted_and_never_raises(tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    t = MotionTrace(blocked)
    t.emit("apply", {"i": 1})
    assert t.flush() == 0
    assert t.dropped == 1


def test_stop_flushes_what_is_buffered(tmp_path):
    t = MotionTrace(tmp_path)
    t.start()
    t.emit("apply", {"i": 1})
    t.stop()
    assert read_records(tmp_path / "no-session.jsonl")[1]["i"] == 1


# --- the plan id ------------------------------------------------------------
def test_a_plan_id_belongs_to_the_thread_that_named_it(trace):
    plan = motion_trace.begin_plan()
    assert plan > 0
    seen: list[int] = []
    other = threading.Thread(target=lambda: seen.append(motion_trace.current_plan()))
    other.start()
    other.join()
    assert seen == [0]
    assert motion_trace.current_plan() == plan
    motion_trace.end_plan()
    assert motion_trace.current_plan() == 0


class _VoiceAlwaysOn:
    available = True
    active = True
    level = 0.5
    onset_count = 3

    def silent_for_s(self) -> float:
        return 0.0

    def active_since(self, _t0: float) -> bool:
        return True

    def rose_since(self, _t0: float) -> bool:
        return False

    def wait_for_frame(self, _timeout_s: float) -> None:
        return None

    def wait_for_onset(self, _timeout_s: float, *, stop=None, abort=None) -> bool:
        return True


def _goto(**kw) -> MotionIntent:
    return MotionIntent(kind="goto", duration=0.12, antennas=(0.1, 0.1), **kw)


def test_the_player_joins_every_record_of_a_batch_on_its_plan_id(trace, tmp_path):
    client = DryRunRobotClient()
    player = RobotPlayer(client)
    try:
        plan = motion_trace.begin_plan()
        assert player.submit([_goto(reason="cue tension"), _goto(reason="cue settle")])
        motion_trace.end_plan()
        assert wait_for(lambda: not player.busy and len(client.applied) >= 2)
    finally:
        player.stop()
        player.join()
    trace.flush()

    mine = [r for r in all_records(tmp_path) if r.get("plan") == plan]
    assert [r["s"] for r in mine] == ["pickup", "apply", "apply", "batch_end"]
    assert mine[0]["n"] == 2 and mine[0]["voice"] is False
    assert [r["i"] for r in mine[1:3]] == [0, 1]
    assert [r["reason"] for r in mine[1:3]] == ["cue tension", "cue settle"]
    assert all(r["ms"] >= 0 and r["kind"] == "goto" for r in mine[1:3])
    assert mine[3]["revoked"] is False and mine[3]["moved"] is True


def test_a_failing_client_still_records_the_attempt_and_the_batch_goes_on(trace, tmp_path):
    class Broken(DryRunRobotClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def apply(self, intent: MotionIntent) -> None:
            self.calls += 1
            raise OSError("bus")

    client = Broken()
    player = RobotPlayer(client)
    try:
        plan = motion_trace.begin_plan()
        assert player.submit([_goto(reason="a"), _goto(reason="b")])
        motion_trace.end_plan()
        assert wait_for(lambda: not player.busy and client.calls >= 2)
    finally:
        player.stop()
        player.join()
    trace.flush()
    assert [r["i"] for r in all_records(tmp_path)
            if r.get("plan") == plan and r["s"] == "apply"] == [0, 1]


def test_the_voice_seen_record_says_how_the_sentence_bound_to_its_voice(trace, tmp_path):
    voice = _VoiceAlwaysOn()
    player = RobotPlayer(DryRunRobotClient(), voice=voice)
    try:
        plan = motion_trace.begin_plan()
        assert player.submit([_goto(speech=True, reason="a"), _goto(speech=True, reason="b")])
        motion_trace.end_plan()
        # Nothing has counted as a new onset, so the sentence binds only once the voice
        # has continued past the fallback.
        assert wait_for(lambda: any(r.get("plan") == plan and r["s"] == "voice_seen"
                                    for r in (trace.flush() and []) or all_records(tmp_path)))
        voice.active = False
        voice.silent_for_s = lambda: 10.0
        assert wait_for(lambda: not player.busy, timeout_s=5.0)
    finally:
        player.stop()
        player.join()
    trace.flush()
    seen = [r for r in all_records(tmp_path) if r.get("plan") == plan and r["s"] == "voice_seen"]
    assert [r["bind"] for r in seen] == ["continued"]


# --- the voice --------------------------------------------------------------
def _frame(amplitude: int) -> bytes:
    return (amplitude.to_bytes(2, "little", signed=True)) * 320


def test_the_voice_records_edges_not_frames(trace, tmp_path):
    now = [100.0]
    link = VoiceLink(clock=lambda: now[0], enabled=True, sink="")

    def feed(amplitude: int, frames: int) -> None:
        for _ in range(frames):
            now[0] += 0.02
            link.feed(_frame(amplitude))

    feed(0, 5)
    feed(4000, 10)
    feed(0, 15)
    trace.flush()

    edges = [(r["edge"], r.get("kind")) for r in all_records(tmp_path) if r["s"] == "voice"]
    assert edges == [("onset", "dip"), ("rise", None), ("fall", None)]
    fall = [r for r in all_records(tmp_path) if r.get("edge") == "fall"][0]
    # When the hangover expired is not when the voice stopped: the record carries both.
    assert fall["t"] - fall["last"] > 0.19


# --- nothing a person said --------------------------------------------------
def test_a_sentence_record_holds_no_words_and_no_direction(trace, tmp_path):
    driver = LiveDriver(DryRunRobotClient())
    try:
        for _ in range(2):         # a literal double delivery
            driver.on_utterance({"text": "The private words of a sentence.", "seq": 4,
                                 "cue_category": "tension", "cue_body": "a hushed direction"})
    finally:
        driver.shutdown(timeout_s=1.0)
    trace.flush()

    raw = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    assert "private" not in raw
    assert "hushed" not in raw
    sentences = [r for r in all_records(tmp_path) if r["s"] == "sentence"]
    assert sentences[0]["authored"] == "tension"
    assert sentences[0]["seq"] == 4
    assert sentences[0]["chars"] == len("The private words of a sentence.")
    assert sentences[1]["deduped"] is True


def test_a_cue_only_sentence_is_visible_in_the_trace(trace, tmp_path):
    driver = LiveDriver(DryRunRobotClient())
    try:
        driver.on_utterance({"text": "   ", "seq": 5, "cue_category": "levity"})
    finally:
        driver.shutdown(timeout_s=1.0)
    trace.flush()
    record = [r for r in all_records(tmp_path) if r["s"] == "sentence"][0]
    assert record["authored"] == record["performed"] == "levity"
    assert record["chars"] == 0 and record["slug"] == "cue_only/levity"


def test_the_motion_is_the_same_with_the_trace_on_and_off(tmp_path):
    """Same input, same commanded intents. Tracing observes and never decides."""

    def run() -> list:
        client = DryRunRobotClient()
        driver = LiveDriver(client)
        try:
            driver.on_utterance({"text": "One plain sentence about the plan.", "seq": 1,
                                 "cue_category": "softening"})
            assert wait_for(lambda: not driver.player.busy, timeout_s=8.0)
        finally:
            driver.shutdown(timeout_s=1.0)
        return [(i.kind, i.reason, i.duration, i.antennas) for i in client.applied
                if i.kind != "wake_up"]

    off = run()
    assert motion_trace.start(tmp_path) is not None
    try:
        on = run()
    finally:
        motion_trace.stop()
    assert off == on and off
