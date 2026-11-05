"""The fan-out: what the relay tap costs a session, what it loses, and what it lets out.

The tap runs on the request thread of ``POST /api/cycle``, the thread a session's motion is
planned and submitted on. So the first half of this file is about that thread: a consumer
that is stuck, slow or gone must not change what ``offer`` costs, nothing the consumer holds
may be something ``offer`` waits for, and the body of ``offer`` is pinned to a queue put. The
second half is the data: numbering with no silent gaps, the privacy gate on every field that
could name a person or carry a sentence, and what stays off the wire unless it is asked for.

No ROS is needed. The publisher and the adapter node are fakes.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
_ROS_PACKAGE = _ROOT / "ros2_ws" / "src" / "emori" / "emori_adapter_ros"
for _path in (_HUB, _ROS_PACKAGE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from emori_adapter_ros.privacy import Privacy  # noqa: E402
from emori_hub import fanout as fo  # noqa: E402
from emori_hub.fanout import FanOut, SessionEvent  # noqa: E402

from emori_adapter.hostapp.relay import RelayHub  # noqa: E402

_FANOUT_SOURCE = Path(fo.__file__)

SALT = "test-salt"


def _privacy(*, deidentify: bool = True, speech_text: bool = False) -> Privacy:
    return Privacy(deidentify=deidentify, speech_text=speech_text, salt=SALT)


def _wait_until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    return predicate()


def _envelope(kind: str, **fields: Any) -> dict:
    return {"type": kind, "session_id": "s-1234", "person_id": "student-7", "lang": "en", **fields}


_STAMP = "2026-01-02T03:04:05Z"
_STAMP_EPOCH = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()


def _utterance(**fields: Any) -> dict:
    return _envelope("pokamind_ai_utterance", **{
        "text": "Let us start with how your week went.", "seq": 3, "source": "turn",
        "timestamp": _STAMP, **fields})


def _map(envelope: dict, *, privacy: Privacy | None = None, raw: bool = False,
         seq: int = 1, **more: Any) -> SessionEvent:
    return fo.event_from_envelope(envelope, seq=seq, stamp=100.0,
                                  privacy=privacy or _privacy(), raw=raw, **more)


class _Sink:
    """A publisher and an adapter node that record what they are given."""

    def __init__(self) -> None:
        self.events: list[SessionEvent] = []
        self.fed: list[dict] = []
        self.threads: set[str] = set()

    def publish(self, event: SessionEvent) -> None:
        self.threads.add(threading.current_thread().name)
        self.events.append(event)

    def feed(self, envelope: dict) -> None:
        self.threads.add(threading.current_thread().name)
        self.fed.append(envelope)


class _Stuck:
    """A publisher that holds the consumer thread until it is let go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[SessionEvent] = []

    def publish(self, event: SessionEvent) -> None:
        self.entered.set()
        self.release.wait(30.0)
        self.events.append(event)


@pytest.fixture
def started():
    made: list[FanOut] = []

    def make(**kwargs: Any) -> FanOut:
        kwargs.setdefault("privacy", _privacy())
        fanout = FanOut(**kwargs)
        fanout.start()
        made.append(fanout)
        return fanout

    yield make
    for fanout in made:
        fanout.stop()


# --- the thread the tap runs on ---------------------------------------------------------


def test_a_stuck_consumer_leaves_the_tap_a_queue_put_that_returns_at_once(started):
    """The consumer is inside a sink for good. Every offer still returns, and the loss is counted."""

    stuck = _Stuck()
    fanout = started(publish=stuck.publish)
    fanout.offer(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0), "the consumer never reached the sink"

    offers = 5 * fanout.capacity
    results: list[bool] = []
    began = time.perf_counter()

    def hammer() -> None:
        for _ in range(offers):
            results.append(fanout.offer(json.dumps(_utterance())))

    thread = threading.Thread(target=hammer, name="request-thread")
    thread.start()
    thread.join(10.0)
    spent = time.perf_counter() - began
    try:
        assert not thread.is_alive(), "offer waited for a consumer that is stuck"
        assert len(results) == offers
        # The one in the consumer's hands is gone from the queue, so one more fits than the
        # queue is long.
        assert results.count(True) == fanout.capacity
        assert results.count(False) == offers - fanout.capacity
        counters = fanout.stats()
        assert counters["dropped"] == offers - fanout.capacity
        assert counters["depth"] == fanout.capacity
        assert counters["published"] == 0
        assert counters["busy_for_s"] is not None
        assert spent < 5.0, f"{offers} offers took {spent:.2f} s with the consumer stuck"
    finally:
        stuck.release.set()


def test_no_lock_the_consumer_takes_is_one_the_tap_waits_for(started):
    """The tap's lock is held by offers and notes alone, so the consumer cannot hold it."""

    stuck = _Stuck()
    fanout = started(publish=stuck.publish)
    fanout.offer(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0)
    try:
        assert fanout._lock.acquire(blocking=False), "the consumer is holding the tap's lock"
        fanout._lock.release()
        assert fanout.offer("{}") is True
    finally:
        stuck.release.set()


def test_the_tap_runs_nothing_of_the_consumers_on_the_callers_thread(started):
    sink = _Sink()
    fanout = started(publish=sink.publish, feed=sink.feed)
    for _ in range(20):
        fanout.offer(json.dumps(_utterance()))
    assert _wait_until(lambda: len(sink.events) == 20)
    assert sink.threads == {"ros-fanout"}


def _called_names(function: ast.FunctionDef) -> set[str]:
    names = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            names.add(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "?"))
    return names


def _method(name: str) -> ast.FunctionDef:
    tree = ast.parse(_FANOUT_SOURCE.read_text(encoding="utf-8"))
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        if cls.name == "FanOut":
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"FanOut.{name} is gone")


@pytest.mark.parametrize("name", ["offer", "note", "_enqueue"])
def test_the_tap_side_is_a_queue_put_and_nothing_that_can_wait_or_write(name):
    """Pinned by reading the code: it parses nothing, logs nothing, waits for nothing and
    calls no sink. Adding any of those to the path a session runs on fails here."""

    allowed = {"put_nowait", "_enqueue", "_clock", "startswith", "isinstance"}
    called = _called_names(_method(name))
    assert called <= allowed, f"{name} calls {sorted(called - allowed)}"


def test_the_tap_side_reaches_for_no_sink_no_logger_and_no_parser():
    used = {node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(_method("_enqueue")) if isinstance(node, (ast.Attribute, ast.Name))}
    assert "put_nowait" in used
    forbidden = {"_publish", "_feed", "log", "json", "time", "sleep", "join", "wait", "get",
                 "acquire", "_thread", "_parse", "_privacy"}
    assert not used & forbidden, sorted(used & forbidden)


def test_loading_the_module_needs_no_ros_and_no_core():
    tree = ast.parse(_FANOUT_SOURCE.read_text(encoding="utf-8"))
    top_level = {alias.name.split(".")[0] for node in tree.body
                 if isinstance(node, (ast.Import, ast.ImportFrom))
                 for alias in (node.names if isinstance(node, ast.Import)
                               else [ast.alias(name=node.module or "")])}
    assert not top_level & {"rclpy", "emori_adapter", "emori_msgs", "emori_adapter_ros"}


def test_through_the_relay_a_stuck_consumer_does_not_slow_a_publish(started):
    """The same, through the real relay: ``publish`` is what ``/api/cycle`` calls."""

    stuck = _Stuck()
    fanout = started(publish=stuck.publish)
    relay = RelayHub()
    fanout.install(relay)
    relay.publish(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0)

    done = threading.Event()

    def cycle() -> None:
        for _ in range(3 * fanout.capacity):
            relay.publish(json.dumps(_utterance()))
        done.set()

    thread = threading.Thread(target=cycle)
    thread.start()
    try:
        assert done.wait(10.0), "RelayHub.publish waited on the fan-out"
        assert fanout.stats()["dropped"] > 0
    finally:
        stuck.release.set()
        thread.join(10.0)


def test_a_consumer_that_was_never_started_still_costs_the_tap_nothing():
    fanout = FanOut(publish=lambda _event: None, privacy=_privacy())
    results = [fanout.offer("{}") for _ in range(fanout.capacity + 10)]
    assert results.count(False) == 10
    assert fanout.stats()["dropped"] == 10
    assert fanout.stats()["running"] is False


def test_the_consumer_tells_the_journal_what_the_tap_could_not(started, caplog):
    """The tap must not log, so the thread behind it reports the drops once it can."""

    stuck = _Stuck()
    fanout = started(publish=stuck.publish)
    fanout.offer(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0)
    for _ in range(fanout.capacity + 7):
        fanout.offer("{}")
    with caplog.at_level(logging.WARNING, logger="emori.hub.fanout"):
        stuck.release.set()
        assert _wait_until(lambda: "dropped" in caplog.text)
    assert "7 envelopes dropped" in caplog.text


# --- numbering ---------------------------------------------------------------------------


def test_events_arrive_in_the_order_of_the_offers_with_no_number_missing(started):
    sink = _Sink()
    fanout = started(publish=sink.publish)
    kinds = ["pokamind_analysis_update", "pokamind_ai_utterance", "pokamind_tts_ended",
             "pokamind_turn_armed", "pokamind_user_speaking", "pokamind_turn_submitted"]
    for kind in kinds:
        fanout.offer(json.dumps(_envelope(kind)))
    assert _wait_until(lambda: len(sink.events) == len(kinds))
    assert [event.seq for event in sink.events] == [1, 2, 3, 4, 5, 6]
    assert [event.type for event in sink.events] == kinds


def test_a_dropped_envelope_leaves_a_hole_in_seq_and_only_a_hole(started):
    stuck = _Stuck()
    fanout = started(publish=stuck.publish)
    fanout.offer(json.dumps(_envelope("pokamind_ai_utterance")))
    assert stuck.entered.wait(10.0)
    total = fanout.capacity + 40
    for _ in range(total):
        fanout.offer(json.dumps(_envelope("pokamind_analysis_update")))
    stuck.release.set()
    assert _wait_until(lambda: len(stuck.events) == 1 + fanout.capacity)
    seqs = [event.seq for event in stuck.events]
    assert seqs == sorted(seqs)
    missing = sorted(set(range(1, total + 2)) - set(seqs))
    assert len(missing) == 40 == fanout.stats()["dropped"]
    # What was dropped is the newest: the queue was full, so the late ones found no room.
    assert missing == list(range(total + 2 - 40, total + 2))


def test_an_unreadable_envelope_still_uses_its_number_so_a_gap_always_means_a_loss(started):
    sink = _Sink()
    fanout = started(publish=sink.publish, feed=sink.feed)
    fanout.offer(json.dumps(_envelope("pokamind_turn_armed")))
    fanout.offer("this is not json")
    fanout.offer(json.dumps(["not", "an", "object"]))
    fanout.offer(json.dumps({"no_type": True}))
    fanout.offer(json.dumps(_envelope("pokamind_tts_ended")))
    assert _wait_until(lambda: len(sink.events) == 5)
    assert [event.seq for event in sink.events] == [1, 2, 3, 4, 5]
    assert [event.type for event in sink.events] == [
        "pokamind_turn_armed", fo.UNREADABLE_TYPE, fo.UNREADABLE_TYPE, fo.UNREADABLE_TYPE,
        "pokamind_tts_ended"]
    assert fanout.stats()["unreadable"] == 3
    assert len(sink.fed) == 3  # the adapter node is fed what parsed, and nothing else


def test_hub_notes_are_numbered_among_the_envelopes_and_never_fed_to_the_adapter(started):
    sink = _Sink()
    fanout = started(publish=sink.publish, feed=sink.feed)
    fanout.offer(json.dumps(_envelope("pokamind_ai_utterance")))
    assert fanout.note("hub_session_finalized", session_id="s-1234", person_id="student-7",
                       lang="ca", status="completed")
    fanout.offer(json.dumps(_envelope("pokamind_tts_ended")))
    assert _wait_until(lambda: len(sink.events) == 3)
    assert [(event.seq, event.type) for event in sink.events] == [
        (1, "pokamind_ai_utterance"), (2, "hub_session_finalized"), (3, "pokamind_tts_ended")]
    finalized = sink.events[1]
    assert finalized.status == "completed" and finalized.lang == "ca"
    assert finalized.session_id.startswith("p-") and finalized.person_id.startswith("p-")
    assert [fed["type"] for fed in sink.fed] == ["pokamind_ai_utterance", "pokamind_tts_ended"]


def test_a_note_cannot_pose_as_a_relayed_envelope(started):
    fanout = started(publish=lambda _event: None)
    assert fanout.note("pokamind_session_completed") is False
    assert fanout.note("") is False
    assert fanout.note(None) is False  # type: ignore[arg-type]
    assert fanout.stats()["seq"] == 0


# --- sinks that fail ---------------------------------------------------------------------


def test_a_publisher_that_raises_costs_one_event_and_the_feed_still_gets_the_envelope(started):
    sink = _Sink()
    calls: list[int] = []

    def flaky(event: SessionEvent) -> None:
        calls.append(event.seq)
        if event.seq == 2:
            raise RuntimeError("the middleware is down")
        sink.publish(event)

    fanout = started(publish=flaky, feed=sink.feed)
    for _ in range(3):
        fanout.offer(json.dumps(_utterance()))
    assert _wait_until(lambda: len(sink.fed) == 3)
    assert calls == [1, 2, 3]
    assert [event.seq for event in sink.events] == [1, 3]
    counters = fanout.stats()
    assert counters["publish_errors"] == 1 and counters["published"] == 2 and counters["fed"] == 3


def test_a_feed_that_raises_costs_one_envelope_and_the_next_still_goes(started):
    sink = _Sink()
    calls = 0

    def flaky(envelope: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the node is gone")
        sink.feed(envelope)

    fanout = started(publish=sink.publish, feed=flaky)
    for _ in range(3):
        fanout.offer(json.dumps(_utterance()))
    assert _wait_until(lambda: len(sink.events) == 3 and calls == 3)
    counters = fanout.stats()
    assert counters["feed_errors"] == 1 and counters["fed"] == 2
    assert len(sink.fed) == 2


def test_the_adapter_node_is_fed_the_envelope_as_the_relay_sent_it_and_not_the_gated_one(started):
    """The node applies its own gate on what it publishes; this hop is inside the process."""

    sink = _Sink()
    fanout = started(publish=sink.publish, feed=sink.feed)
    sent = _utterance(cue_category="levity")
    fanout.offer(json.dumps(sent))
    assert _wait_until(lambda: sink.fed)
    assert sink.fed[0] == sent
    assert sink.events[0].text == "" and sink.events[0].person_id != "student-7"


def test_a_mapping_that_fails_still_publishes_its_number(started):
    sink = _Sink()

    class Broken:
        deidentify = True
        speech_text = False

        def apply_ids(self, _person, _session):
            raise RuntimeError("the gate broke")

    fanout = started(publish=sink.publish, privacy=Broken())
    fanout.offer(json.dumps(_utterance()))
    assert _wait_until(lambda: sink.events)
    assert sink.events[0].type == fo.UNREADABLE_TYPE and sink.events[0].seq == 1
    # Nothing the broken gate was meant to hold back is in what was published.
    assert "student-7" not in repr(sink.events[0])
    assert fanout.stats()["mapping_errors"] == 1


# --- the gate ---------------------------------------------------------------------------


def test_by_default_ids_are_pseudonyms_and_the_sentence_is_withheld_but_measured():
    event = _map(_utterance(cue_category="levity", cue_family="levity",
                            cue_family_source="pokamind", cue_slug="levity_nod",
                            cue_recorded="amazed1", cue_budget_s=2.5))
    assert event.person_id == Privacy(salt=SALT).apply_ids("student-7", "s-1234")[0]
    assert event.session_id == Privacy(salt=SALT).apply_ids("student-7", "s-1234")[1]
    assert event.person_id.startswith("p-") and event.session_id.startswith("p-")
    assert event.text == ""
    assert event.est_duration > 0.0, "withholding the words must not withhold their length"
    assert (event.cue_category, event.cue_family, event.cue_family_source) == (
        "levity", "levity", "pokamind")
    assert (event.cue_slug, event.cue_recorded, event.cue_budget_s) == ("levity_nod", "amazed1", 2.5)
    assert (event.source, event.lang, event.type) == ("turn", "en", "pokamind_ai_utterance")
    assert event.payload_json == ""
    assert "student-7" not in repr(event) and "Let us start" not in repr(event)


def test_the_pseudonyms_agree_with_the_adapter_nodes_for_the_same_salt():
    """One person, one pseudonym across Context, SpeechCue and SessionEvent."""

    node = Privacy(deidentify=True, speech_text=False, salt=SALT)
    event = _map(_utterance())
    assert (event.person_id, event.session_id) == node.apply_ids("student-7", "s-1234")


def test_with_de_identification_off_the_ids_travel_as_they_are():
    event = _map(_utterance(), privacy=_privacy(deidentify=False))
    assert (event.person_id, event.session_id) == ("student-7", "s-1234")


def test_with_the_speech_gate_open_the_sentence_travels_without_a_surviving_cue_tag():
    event = _map(_utterance(text="[levity| lighten up] That was a good answer."),
                 privacy=_privacy(speech_text=True))
    assert event.text == "That was a good answer."


def test_the_duration_is_of_the_sentence_without_its_tag():
    seen: list[str] = []

    def measure(text: str) -> float:
        seen.append(text)
        return 1.5

    event = _map(_utterance(text="[energy| go] Tell me more."), duration_of=measure)
    assert seen == ["Tell me more."] and event.est_duration == 1.5


def test_an_end_of_speech_marker_has_no_sentence_and_no_duration():
    event = _map(_envelope("pokamind_tts_ended"))
    assert (event.text, event.est_duration, event.cue_category) == ("", 0.0, "")


def test_what_the_learner_said_is_on_no_field_of_an_analysis_event():
    event = _map(_envelope("pokamind_analysis_update", transcript_delta="my private answer",
                           communication_styles={"warm": 0.4}))
    assert "private" not in repr(event)
    assert event.type == "pokamind_analysis_update" and event.text == ""


def test_a_completion_says_whether_it_has_results_and_carries_none_of_them():
    scored = {"performance_scores": {"clarity": 4}, "session_analysis": "You spoke well."}
    with_results = _map(_envelope("pokamind_session_completed", status="completed", results=scored),
                        has_results=lambda results: bool(results))
    assert with_results.has_results is True and with_results.status == "completed"
    assert "spoke well" not in repr(with_results) and with_results.payload_json == ""
    without = _map(_envelope("pokamind_session_completed", status="failed", results={}),
                   has_results=lambda results: bool(results))
    assert without.has_results is False and without.status == "failed"


def test_has_results_is_decided_by_the_function_the_driver_decides_by():
    pytest.importorskip("emori_adapter.hostapp.metrics_store")
    from emori_adapter.hostapp.metrics_store import results_recorded

    body = {"body_language_metrics": {"posture": 0.5}, "duration_minutes": 3}
    event = _map(_envelope("pokamind_session_completed", results=body))
    assert event.has_results is results_recorded(body) is False


def test_the_two_types_read_here_are_the_cores_constants():
    from emori_adapter.sources.websocket import AI_UTTERANCE_EVENT, COMPLETED_EVENT

    assert (fo.AI_UTTERANCE_TYPE, fo.COMPLETED_TYPE) == (AI_UTTERANCE_EVENT, COMPLETED_EVENT)


def test_the_envelopes_own_clock_becomes_source_time_and_a_missing_one_is_zero():
    assert _map(_utterance()).source_time == pytest.approx(_STAMP_EPOCH)
    assert _map(_envelope("pokamind_turn_armed")).source_time == 0.0
    assert _map(_envelope("pokamind_turn_armed", timestamp="not a time")).source_time == 0.0


def test_every_text_field_is_bounded_and_a_number_that_is_not_one_becomes_zero():
    event = _map(_utterance(cue_category="x" * 5000, cue_budget_s="NaN", source="y" * 500,
                            lang="z" * 99))
    assert len(event.cue_category) == 200 and len(event.source) == 32 and len(event.lang) == 16
    assert event.cue_budget_s == 0.0
    assert _map(_utterance(cue_budget_s=-4)).cue_budget_s == 0.0
    assert len(_map(_envelope("t" * 500)).type) == 64


def test_a_type_that_is_not_text_is_an_unreadable_envelope_and_not_a_guess():
    for bad in (None, 7, ["pokamind_ai_utterance"], ""):
        assert _map({"type": bad, "session_id": "s", "person_id": "p"}).type == fo.UNREADABLE_TYPE


# --- the raw envelope -------------------------------------------------------------------


def test_the_raw_envelope_is_not_on_the_wire_unless_asked_for():
    assert _map(_utterance()).payload_json == ""
    assert fo.raw_envelopes_enabled({}) is False
    for typo in ("flase", "", "2", "enabled"):
        assert fo.raw_envelopes_enabled({fo.RAW_ENVELOPES_VAR: typo}) is False
    for word in ("1", "true", "YES", " on "):
        assert fo.raw_envelopes_enabled({fo.RAW_ENVELOPES_VAR: word}) is True


def test_a_raw_envelope_still_passes_the_gate():
    raw = json.loads(_map(_utterance(cue_body="lean in", transcript_delta="hello there"),
                          raw=True).payload_json)
    assert raw["type"] == "pokamind_ai_utterance"
    assert raw["person_id"].startswith("p-") and raw["session_id"].startswith("p-")
    assert not {"text", "cue_body", "transcript_delta"} & set(raw)
    assert "student-7" not in json.dumps(raw)
    assert raw["seq"] == 3


def test_a_raw_envelope_keeps_its_text_only_when_the_speech_gate_and_the_ids_gate_say_so():
    raw = json.loads(_map(_utterance(), raw=True,
                          privacy=_privacy(deidentify=False, speech_text=True)).payload_json)
    assert raw["text"].startswith("Let us start")
    assert (raw["person_id"], raw["session_id"]) == ("student-7", "s-1234")


def test_a_raw_envelope_too_long_to_send_whole_is_withheld_and_counted(started):
    sink = _Sink()
    fanout = started(publish=sink.publish, raw=True)
    fanout.offer(json.dumps(_envelope("pokamind_session_completed",
                                      results={"summary": "x" * (fo.RAW_LIMIT_CHARS + 1)})))
    fanout.offer(json.dumps(_envelope("pokamind_turn_armed")))
    assert _wait_until(lambda: len(sink.events) == 2)
    assert sink.events[0].payload_json == "" and sink.events[1].payload_json != ""
    assert fanout.stats()["raw_withheld"] == 1


# --- the message ------------------------------------------------------------------------


def _blank_message() -> SimpleNamespace:
    return SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=0)),
                           source_time=SimpleNamespace(sec=0, nanosec=0))


def test_every_field_of_the_message_is_filled_from_the_event():
    event = SessionEvent(
        seq=9, stamp=12.25, type="pokamind_ai_utterance", session_id="p-a", person_id="p-b",
        lang="ca", source_time=1767323045.5, text="hola", cue_category="levity",
        cue_family="levity", cue_family_source="text", cue_slug="s", cue_recorded="r",
        cue_budget_s=1.5, est_duration=2.0, source="turn", has_results=True, status="completed",
        payload_json="{}")
    message = fo.fill_message(_blank_message(), event)
    assert (message.header.stamp.sec, message.header.stamp.nanosec) == (12, 250_000_000)
    assert (message.source_time.sec, message.source_time.nanosec) == (1767323045, 500_000_000)
    for name in ("seq", "type", "session_id", "person_id", "lang", "text", "cue_category",
                 "cue_family", "cue_family_source", "cue_slug", "cue_recorded", "cue_budget_s",
                 "est_duration", "source", "has_results", "status", "payload_json"):
        assert getattr(message, name) == getattr(event, name), name


def test_the_message_and_the_event_have_the_same_fields():
    """A field added to the interface and not to the event, or the reverse, fails here."""

    text = (_ROOT / "ros2_ws/src/open/emori_msgs/msg/SessionEvent.msg").read_text(encoding="utf-8")
    declared = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].split()
        if len(line) == 2 and "=" not in line[1]:
            declared.add(line[1])
    carried = set(SessionEvent.__dataclass_fields__) - {"stamp"} | {"header"}
    assert declared == carried


def test_a_sequence_number_past_the_wire_type_wraps_instead_of_failing():
    event = SessionEvent(seq=2**32 + 5, stamp=0.0, type="t")
    assert fo.fill_message(_blank_message(), event).seq == 5


# --- hanging it off the relay -----------------------------------------------------------


def test_install_makes_the_fanout_the_tap_and_uninstall_puts_the_old_one_back(started):
    seen: list[str] = []
    relay = RelayHub(tap=seen.append)
    fanout = started(publish=lambda _event: None)
    fanout.install(relay)
    relay.publish("one")
    assert seen == ["one"], "the tap that was there still runs"
    assert fanout.stats()["seq"] == 1
    fanout.uninstall()
    relay.publish("two")
    assert seen == ["one", "two"] and fanout.stats()["seq"] == 1


def test_the_old_tap_failing_does_not_stop_the_fanout_hearing_the_envelope(started, caplog):
    def broken(_message: str) -> None:
        raise RuntimeError("the old tap fell over")

    relay = RelayHub(tap=broken)
    fanout = started(publish=lambda _event: None)
    fanout.install(relay)
    relay.publish("{}")
    assert fanout.stats()["seq"] == 1
    assert "relay tap failed" in caplog.text


def test_uninstall_leaves_a_tap_someone_else_has_since_taken(started):
    relay = RelayHub()
    fanout = started(publish=lambda _event: None)
    fanout.install(relay)

    def theirs(_message: str) -> None:
        return None

    relay.tap = theirs
    fanout.uninstall()
    assert relay.tap is theirs


def test_what_the_request_thread_publishes_reaches_the_graph_in_order_through_the_relay(started):
    sink = _Sink()
    fanout = started(publish=sink.publish, feed=sink.feed)
    relay = RelayHub()
    fanout.install(relay)

    async def run() -> None:
        await relay.startup()
        for kind in ("pokamind_analysis_update", "pokamind_ai_utterance", "pokamind_tts_ended"):
            relay.publish(json.dumps(_envelope(kind)))
        await relay.shutdown()

    asyncio.run(run())
    assert _wait_until(lambda: len(sink.events) == 3)
    assert [event.type for event in sink.events] == [
        "pokamind_analysis_update", "pokamind_ai_utterance", "pokamind_tts_ended"]
    assert [fed["type"] for fed in sink.fed] == [event.type for event in sink.events]


def test_a_frame_a_websocket_client_sent_is_not_fanned_out(started):
    relay = RelayHub()
    fanout = started(publish=lambda _event: None)
    fanout.install(relay)
    relay.publish("{}", exclude=object())
    assert fanout.stats()["seq"] == 0


def test_build_fanout_starts_the_thread_taps_the_relay_and_reads_the_environment():
    app = SimpleNamespace(state=SimpleNamespace(hub=RelayHub()))
    sink = _Sink()
    fanout = fo.build_fanout(
        app, publish=sink.publish, feed=sink.feed,
        environ={"EMORI_ROS_ID_SALT": "pepper", fo.RAW_ENVELOPES_VAR: "1"})
    try:
        assert app.state.fanout is fanout and app.state.hub.tap is not None
        app.state.hub.publish(json.dumps(_utterance()))
        assert _wait_until(lambda: sink.events)
        event = sink.events[0]
        assert event.person_id == Privacy(salt="pepper").apply_ids("student-7", "s-1234")[0]
        assert event.text == "" and json.loads(event.payload_json)["type"] == "pokamind_ai_utterance"
    finally:
        fanout.stop()


def test_load_privacy_reads_the_adapter_nodes_switches():
    gate = fo.load_privacy({"EMORI_ROS_DEIDENTIFY": "0", "EMORI_ROS_SPEECH_TEXT": "1",
                            "EMORI_ROS_ID_SALT": "pepper"})
    assert (gate.deidentify, gate.speech_text, gate.salt) == (False, True, "pepper")
    shipped = fo.load_privacy({})
    assert (shipped.deidentify, shipped.speech_text) == (True, False)


# --- stopping ---------------------------------------------------------------------------


def test_stop_refuses_what_comes_after_and_counts_what_it_left_behind():
    stuck = _Stuck()
    fanout = FanOut(publish=stuck.publish, privacy=_privacy())
    fanout.start()
    fanout.offer(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0)
    for _ in range(5):
        fanout.offer(json.dumps(_utterance()))
    stopper = threading.Thread(target=lambda: fanout.stop(timeout=0.2))
    stopper.start()
    stopper.join(5.0)
    assert not stopper.is_alive(), "stop waited for a consumer that is stuck"
    assert fanout.offer("{}") is False
    assert fanout.stats()["refused_after_stop"] == 1
    stuck.release.set()
    assert _wait_until(lambda: fanout.stats()["abandoned_at_stop"] == 5)


def test_stop_on_a_quiet_fanout_returns_true_and_the_thread_is_gone(started):
    fanout = started(publish=lambda _event: None)
    assert fanout.stop() is True
    assert fanout.stats()["running"] is False


def test_stop_on_a_fanout_never_started_is_fine():
    assert FanOut(publish=lambda _event: None, privacy=_privacy()).stop() is True


def test_stop_reports_a_consumer_still_inside_a_sink():
    stuck = _Stuck()
    fanout = FanOut(publish=stuck.publish, privacy=_privacy())
    fanout.start()
    fanout.offer(json.dumps(_utterance()))
    assert stuck.entered.wait(10.0)
    try:
        assert fanout.stop(timeout=0.1) is False
    finally:
        stuck.release.set()


def test_the_queue_is_the_relays_size():
    from emori_adapter.hostapp import relay

    assert fo.QUEUE_SIZE == 256
    assert "maxsize=256" in Path(relay.__file__).read_text(encoding="utf-8")
