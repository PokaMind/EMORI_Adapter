"""The session node: what it reads, what it will do when asked, and that it does nothing else.

Three things are held here.

* **It acts only when asked.** Building the node, polling, publishing state and answering a
  query start, end and change nothing. A command or a start does exactly what the kiosk's own
  path for it does, and a start that cannot finish leaves no page waiting and no verb queued.
* **It stays off the path a session's motion runs on.** State is read without the driver's
  lock, the module names nothing of the cue, player or submit path, and a publisher that has
  stopped answering costs ``POST /api/cycle`` nothing.
* **It says the same as the interface.** The state view has the message's fields, and every
  one is filled.

No ROS is needed: the node's bindings and the loopback call are fakes, and the hostapp is
the real one built without its lifespan, so no thread is started.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
from dataclasses import fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from starlette.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
_ROS_PACKAGE = _ROOT / "ros2_ws" / "src" / "emori" / "emori_adapter_ros"
for _path in (_HUB, _ROS_PACKAGE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from emori_adapter_ros.privacy import Privacy  # noqa: E402
from emori_hub import session_node as sn  # noqa: E402
from emori_hub.session_node import SessionFacade, SessionStateView, StartResult  # noqa: E402

from emori_adapter.hostapp import server  # noqa: E402
from emori_adapter.hostapp.driver import SessionInfo  # noqa: E402
from emori_adapter.hostapp import pages  # noqa: E402
from emori_adapter.hostapp.extensions import BADGE_OK, HostExtension  # noqa: E402
from emori_adapter.hostapp.start_slot import LANDING_FRESH_S, LandingSeen, StartSlot  # noqa: E402
from emori_adapter.robot.client import DryRunRobotClient  # noqa: E402

_MSGS = _ROOT / "ros2_ws" / "src" / "open" / "emori_msgs"
_SOURCE = Path(sn.__file__)

SALT = "test-salt"


def _privacy(*, deidentify: bool = True) -> Privacy:
    return Privacy(deidentify=deidentify, speech_text=False, salt=SALT)


def _wait_until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    return predicate()


def _msg_fields(relative: str) -> list[str]:
    names = []
    for line in (_MSGS / relative).read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 2 and "=" not in parts[1]:
            names.append(parts[1])
    return names


# --- a driver with nothing behind it ------------------------------------------------------


class _Tracker:
    def __init__(self) -> None:
        self.info: SessionInfo | None = None
        self.cycles = 0
        self.utterances = 0

    @property
    def active(self) -> bool:
        return self.info is not None

    @property
    def exchanged(self) -> bool:
        return self.cycles > 0 or self.utterances > 0


class _Driver:
    def __init__(self) -> None:
        self.tracker = _Tracker()
        self.end_requested_at: float | None = None
        self.frame_alive_at: float | None = None
        self.last_activity = time.monotonic()
        self.watchdog = SimpleNamespace(state=SimpleNamespace(name="IDLE"))
        self._last_finalized: tuple | None = None
        self.queued: list[str] = []
        self.aborted: list[str] = []
        self.abort_result = True
        self.pending = False

    def enqueue_cmd(self, cmd: str) -> None:
        self.queued.append(cmd)

    def enqueue_cmd_if_empty(self, cmd: str) -> bool:
        if self.pending:
            return False
        self.queued.append(cmd)
        return True

    def abort_unstarted_session(self, reason: str) -> bool:
        self.aborted.append(reason)
        return self.abort_result

    def open(self, **fields_: Any) -> None:
        self.tracker.info = SessionInfo(**{
            "session_id": "20260101-000000-abc", "person_id": "student-7", "lang": "en",
            "topic": "Feedback", "module_id": "m1", "anonymous": False, **fields_})

    def close(self, session_id: str = "20260101-000000-abc", *, avg: float | None = 0.5,
              style: str = "calm") -> None:
        self.tracker.info = None
        self._last_finalized = (session_id, 1.0, 1.0, {
            "avg_engagement": avg, "dominant_style": style})


class _Ticker:
    def __init__(self) -> None:
        self.ended: list[str] = []

    def _request_pokamind_end(self, counter: str) -> None:
        self.ended.append(counter)


def _fake_app(**more: Any) -> SimpleNamespace:
    driver = _Driver()
    discarded: list[tuple[str, str]] = []
    glue = SimpleNamespace(robot_idle=lambda: not driver.tracker.active,
                           menu_for=lambda lang="": None, tt_store=None,
                           landing_idle=lambda: True, discarded=discarded,
                           discard_minted=lambda sid, qr="": discarded.append((sid, qr)))
    state = SimpleNamespace(
        driver=driver, glue=glue, start_slot=StartSlot(), watchdog_ticker=_Ticker(),
        lang_state=SimpleNamespace(is_allowed=lambda lang: lang in ("en", "ca")),
        metrics=None)
    for name, value in more.items():
        setattr(state, name, value)
    return SimpleNamespace(state=state)


def _facade(app: Any = None, **kwargs: Any) -> SessionFacade:
    kwargs.setdefault("privacy", _privacy())
    return SessionFacade(app if app is not None else _fake_app(), **kwargs)


# --- the state -------------------------------------------------------------------------------


def test_the_view_has_the_messages_fields_and_nothing_else():
    declared = [name for name in _msg_fields("msg/SessionState.msg") if name != "header"]
    assert [f.name for f in fields(SessionStateView)] == declared


class _Message:
    """A message that remembers which fields were set on it."""

    def __init__(self) -> None:
        object.__setattr__(self, "set", [])
        object.__setattr__(self, "header", SimpleNamespace(stamp=None))

    def __setattr__(self, name: str, value: Any) -> None:
        self.set.append(name)
        object.__setattr__(self, name, value)


def test_every_field_of_the_message_is_filled_from_the_view():
    view = SessionStateView(
        phase=sn.PHASE_EXCHANGED, seq=2**32 + 3, session_id="a", person_id="b", anonymous=True,
        topic="t", module_id="m", lang="ca", cycles=4, utterances=5, exchanged=True,
        watchdog_state="OPEN", idle_for_s=1.5, last_status="completed",
        last_end_reason="ros_end_requested", last_avg_engagement=0.25, last_dominant_style="calm")
    stamp = object()
    message = sn.fill_state_message(_Message(), view, stamp)
    assert message.header.stamp is stamp
    assert sorted(message.set) == sorted(f.name for f in fields(SessionStateView))
    for field in fields(SessionStateView):
        expected = getattr(view, field.name)
        if field.name == "seq":
            expected = 3
        assert getattr(message, field.name) == expected, field.name


def test_phases_follow_the_driver_and_a_session_that_has_not_begun_is_not_one_that_has():
    app = _fake_app()
    driver, facade = app.state.driver, _facade(app)
    assert facade.read().phase == sn.PHASE_NO_SESSION
    driver.open()
    assert facade.read().phase == sn.PHASE_BOOTING
    driver.frame_alive_at = 1.0
    assert facade.read().phase == sn.PHASE_FRAME_ALIVE
    driver.tracker.utterances = 1
    view = facade.read()
    assert view.phase == sn.PHASE_EXCHANGED and view.exchanged and view.utterances == 1
    driver.end_requested_at = 2.0
    assert facade.read().phase == sn.PHASE_END_REQUESTED


def test_a_finished_session_reads_finalized_for_a_moment_and_then_no_session():
    now = [100.0]
    app = _fake_app()
    facade = _facade(app, clock=lambda: now[0])
    app.state.driver.open()
    facade.read()
    app.state.driver.close()
    assert facade.read().phase == sn.PHASE_FINALIZED
    now[0] += sn.FINALIZED_HOLD_S - 0.1
    assert facade.read().phase == sn.PHASE_FINALIZED
    now[0] += 0.2
    assert facade.read().phase == sn.PHASE_NO_SESSION


def test_the_outcome_of_the_last_session_stays_after_the_phase_has_gone():
    class Metrics:
        def query(self, sql: str, params: tuple) -> list[dict]:
            self.asked = params
            return [{"status": "abandoned", "end_reason": "ros_end_requested"}]

    metrics = Metrics()
    now = [0.0]
    app = _fake_app(metrics=metrics)
    facade = _facade(app, clock=lambda: now[0])
    app.state.driver.close("anna-001-20260101-000000-abc", avg=0.5, style="calm")
    view = facade.read()
    assert (view.last_status, view.last_end_reason) == ("abandoned", "ros_end_requested")
    assert (view.last_avg_engagement, view.last_dominant_style) == (0.5, "calm")
    assert metrics.asked == ("20260101-000000-abc",), "the row is stored without the student id"
    now[0] += 60.0
    later = facade.read()
    assert later.phase == sn.PHASE_NO_SESSION and later.last_status == "abandoned"


def test_an_unmeasured_session_is_minus_one_and_an_unreadable_store_leaves_the_rest_empty():
    class Broken:
        def query(self, *_args: Any) -> list[dict]:
            raise RuntimeError("disk")

    app = _fake_app(metrics=Broken())
    facade = _facade(app)
    app.state.driver.close(avg=None, style="")
    view = facade.read()
    assert view.last_avg_engagement == -1.0
    assert (view.last_status, view.last_end_reason) == ("", "")


def test_ids_are_pseudonyms_unless_the_gate_is_off():
    app = _fake_app()
    app.state.driver.open()
    view = _facade(app).read()
    gate = _privacy()
    assert (view.person_id, view.session_id) == gate.apply_ids("student-7", "20260101-000000-abc")
    assert "student-7" not in view.person_id
    raw = _facade(app, privacy=_privacy(deidentify=False)).read()
    assert (raw.person_id, raw.session_id) == ("student-7", "20260101-000000-abc")


def test_next_state_numbers_changes_and_repeats_a_quiet_state_on_the_heartbeat():
    app = _fake_app()
    facade = _facade(app)
    first = facade.next_state(now=0.0)
    assert first is not None and first.seq == 1
    assert facade.next_state(now=1.0) is None
    again = facade.next_state(now=sn.HEARTBEAT_S + 0.1)
    assert again is not None and again.seq == 1, "a repeat carries the number it repeats"
    app.state.driver.open()
    changed = facade.next_state(now=sn.HEARTBEAT_S + 0.2)
    assert changed is not None and changed.seq == 2 and changed.phase == sn.PHASE_BOOTING
    app.state.driver.last_activity -= 30.0
    assert facade.next_state(now=sn.HEARTBEAT_S + 0.4) is None, "the idle clock alone is no change"


def test_reading_the_state_takes_no_lock_of_the_drivers():
    """The state is read on a timer; if that took the lock a session holds, it would queue behind it."""

    class Trap:
        def __enter__(self) -> None:
            raise AssertionError("the state read took the driver's lock")

        def __exit__(self, *_exc: Any) -> None:
            return None

        def acquire(self, *_args: Any, **_kwargs: Any) -> bool:
            raise AssertionError("the state read took the driver's lock")

    app = _fake_app()
    app.state.driver._lock = Trap()
    app.state.driver.open()
    facade = _facade(app)
    assert facade.read().phase == sn.PHASE_BOOTING
    assert facade.next_state(now=0.0) is not None


def test_a_state_read_goes_on_while_another_thread_holds_the_drivers_real_lock():
    app = _fake_app()
    app.state.driver._lock = threading.Lock()
    app.state.driver.open()
    facade = _facade(app)
    app.state.driver._lock.acquire()
    done = threading.Event()
    threading.Thread(target=lambda: (facade.read(), done.set()), daemon=True).start()
    try:
        assert done.wait(5.0)
    finally:
        app.state.driver._lock.release()


# --- command ---------------------------------------------------------------------------------


def test_an_unknown_verb_is_refused_and_touches_nothing():
    app = _fake_app()
    app.state.driver.open()
    facade = _facade(app)
    for verb in ("", "halt", "END", "begin;end", None):
        assert facade.command(verb, "") == (False, "unknown_verb")  # type: ignore[arg-type]
    assert app.state.driver.queued == [] and app.state.watchdog_ticker.ended == []


@pytest.mark.parametrize("verb", ["begin", "end", "reprompt"])
def test_a_verb_for_a_running_session_is_refused_when_there_is_none(verb):
    app = _fake_app()
    facade = _facade(app)
    assert facade.command(verb, "") == (False, "no_session")
    assert app.state.driver.queued == [] and app.state.watchdog_ticker.ended == []


def test_end_goes_through_the_watchdogs_own_end_request_with_the_reason_that_names_it():
    app = _fake_app()
    app.state.driver.open()
    assert _facade(app).command("end", "operator") == (True, "")
    assert app.state.watchdog_ticker.ended == ["ros_end_requested"] == [sn.END_REASON]
    assert app.state.driver.queued == [], "the queue is the watchdog's to write, not ours"


def test_end_while_an_end_is_already_pending_asks_for_nothing_twice():
    app = _fake_app()
    app.state.driver.open()
    app.state.driver.end_requested_at = 1.0
    assert _facade(app).command("end") == (False, "end_pending")
    assert app.state.watchdog_ticker.ended == []


def test_end_with_no_watchdog_to_ask_is_unavailable_and_does_not_improvise():
    app = _fake_app(watchdog_ticker=None)
    app.state.driver.open()
    assert _facade(app).command("end") == (False, "unavailable")
    assert app.state.driver.queued == []


def test_begin_and_reprompt_use_the_command_slot_and_reprompt_never_displaces_a_verb():
    app = _fake_app()
    driver, facade = app.state.driver, _facade(app)
    driver.open()
    assert facade.command("begin") == (True, "")
    assert facade.command("reprompt") == (True, "")
    assert driver.queued == ["begin", "reprompt"]
    driver.pending = True
    assert facade.command("reprompt") == (False, "command_pending")
    assert facade.command("begin") == (False, "command_pending")
    assert driver.queued == ["begin", "reprompt"]


def test_a_begin_does_not_take_the_place_of_an_end():
    app = _fake_app()
    driver, facade = app.state.driver, _facade(app)
    driver.open()
    driver.end_requested_at = 1.0
    assert facade.command("begin") == (False, "end_pending")
    assert driver.queued == []


def test_reload_is_for_an_idle_robot_because_it_would_take_the_place_of_a_pending_end():
    app = _fake_app()
    driver, facade = app.state.driver, _facade(app)
    assert facade.command("reload") == (True, "")
    assert driver.queued == ["reload"]
    driver.open()
    assert facade.command("reload") == (False, "busy")
    assert driver.queued == ["reload"]


def test_abort_unstarted_reports_whether_it_acted_and_a_reason_is_only_ever_a_plain_code():
    app = _fake_app()
    driver, facade = app.state.driver, _facade(app)
    assert facade.command("abort_unstarted", "frame_never_came") == (True, "")
    assert facade.command("abort_unstarted", "Not Plain! \n{}") == (True, "")
    assert driver.aborted == ["frame_never_came", "ros_abort_unstarted"]
    driver.abort_result = False
    assert facade.command("abort_unstarted") == (False, "no_unstarted_session")


# --- the real hostapp ------------------------------------------------------------------------


class _Metrics:
    """Takes every call the driver makes of its store and keeps the one that matters."""

    def __init__(self) -> None:
        self.finished: list[dict] = []
        self.events: list[str] = []

    def session_finished(self, session_id: str, **kwargs: Any) -> None:
        self.finished.append({"session_id": session_id, **kwargs})

    def log_event(self, kind: str, **_kwargs: Any) -> None:
        self.events.append(kind)

    def recovery_counts(self, _session_id: str) -> dict:
        return {"attempts": 0, "successes": 0}

    def query(self, *_args: Any) -> list[dict]:
        return []

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None


@pytest.fixture
def real(monkeypatch):
    """The hostapp as it is built, without its lifespan: no thread of its own is running."""

    def build() -> SimpleNamespace:
        monkeypatch.setattr(server, "DEMO", True)
        monkeypatch.setattr(server, "PARTNER", "test-partner")
        monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
        app = server._build_app(overrides={
            "robot_client": DryRunRobotClient(), "fiware_factory": None, "idle": False,
            "gesture": False, "tts": None, "stt": None, "voice": None})
        app.state.driver.metrics = _Metrics()
        return SimpleNamespace(app=app, client=TestClient(app), driver=app.state.driver,
                               metrics=app.state.driver.metrics)

    return build


def _info(sid: str = "20260101-000000-abc") -> SessionInfo:
    return SessionInfo(session_id=sid, person_id="walkin", lang="en", module_id="m1")


def test_building_the_node_and_polling_it_start_nothing_and_change_nothing(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy())
    for step in range(5):
        facade.next_state(now=float(step) * 3)
        facade.voice_menu("")
    assert not rig.driver.tracker.active
    assert rig.driver.pop_cmd() is None
    assert not rig.app.state.start_slot.waiting()
    assert rig.driver.counters["sessions_started"] == 0
    assert rig.client.get("/").status_code == 200


def test_end_ends_a_session_the_way_the_watchdog_does_and_the_row_names_the_caller(real):
    rig = real()
    rig.driver.start_session(_info())
    facade = SessionFacade(rig.app, privacy=_privacy())
    assert facade.command("end", "operator") == (True, "")
    assert rig.driver.counters["ros_end_requested"] == 1
    assert rig.driver.pop_cmd() == "end"
    assert rig.driver.end_requested_at is not None, "the dead-iframe fail-safe is armed"
    assert rig.metrics.events == ["ros_end_requested"]
    rig.driver.on_completed({"type": "pokamind_session_completed", "session_id": "x",
                             "results": None})
    assert rig.metrics.finished[-1]["end_reason"] == "ros_end_requested"
    assert rig.metrics.finished[-1]["status"] == "abandoned"
    assert not rig.driver.tracker.active


def test_that_end_is_the_same_as_the_watchdogs_but_for_its_name(real):
    ros, dog = real(), real()
    for rig in (ros, dog):
        rig.driver.start_session(_info())
    SessionFacade(ros.app, privacy=_privacy()).command("end")
    dog.app.state.watchdog_ticker._request_pokamind_end("walkaway_ends")

    def seen(rig: SimpleNamespace) -> tuple:
        return (rig.driver.pop_cmd(), rig.driver.end_requested_at is not None,
                rig.driver.tracker.active, sum(rig.driver.counters.values()) - 1)

    assert seen(ros) == seen(dog)
    assert ros.driver._auto_end_reason == "ros_end_requested"


def test_a_ros_end_that_pokamind_never_answers_is_closed_locally_like_any_other(real):
    rig = real()
    rig.driver.start_session(_info())
    SessionFacade(rig.app, privacy=_privacy()).command("end")
    with rig.driver._lock:
        rig.driver._finalize_locked(results=None, end_reason="end_local_fallback")
    assert rig.metrics.finished[-1]["end_reason"] == "ros_end_requested", (
        "the pending request wins over the fallback that executed it")


def test_the_voice_menu_is_the_one_the_landing_page_reads(real):
    rig = real()
    prompt, grammar, keys, names = SessionFacade(rig.app, privacy=_privacy()).voice_menu("")
    served = rig.client.get("/api/voice-menu").json()
    assert prompt == served["prompt"]
    assert keys == [option["key"] for option in served["options"]]
    assert names == [option["name"] for option in served["options"]]
    assert isinstance(grammar, list)


def test_a_language_not_on_offer_has_no_menu_and_costs_the_cache_nothing(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy())
    assert facade.voice_menu("xx") == ("", [], [], [])
    assert facade.voice_menu("../../etc") == ("", [], [], [])


def test_tt_overrides_are_closed_unless_the_http_routes_own_switch_is_on(real):
    rig = real()
    closed = SessionFacade(rig.app, privacy=_privacy(), environ={})
    assert closed.set_tt_overrides("{}") == (False, 0, "tt_inject_disabled")
    assert rig.app.state.glue.tt_store.snapshot()["rev"] == 0
    opened = SessionFacade(rig.app, privacy=_privacy(), environ={sn.TT_INJECT_VAR: "1"})
    assert opened.set_tt_overrides("not json") == (False, 0, "invalid_json")
    ok, rev, error = opened.set_tt_overrides(json.dumps({"patch": "nope"}))
    assert (ok, rev) == (False, 0) and error
    assert rig.app.state.glue.tt_store.snapshot()["rev"] == 0


# --- start -----------------------------------------------------------------------------------


def _via(rig: SimpleNamespace) -> Callable[[str, dict], tuple[int, dict]]:
    """The loopback call, answered by the real routes without a socket, with the kiosk on its
    landing page and nobody using it."""

    rig.client.get("/api/pending-cmd?verbs=reload&phase=scan")

    def post(path: str, payload: dict) -> tuple[int, dict]:
        response = rig.client.post(path, json=payload)
        return response.status_code, response.json()

    return post


_MINTED = (200, {"session_url": "/session?demo=1&sid=s1", "topic": "T"})


def _mint(answer: tuple[int, dict] = _MINTED,
          posts: list | None = None) -> Callable[[str, dict], tuple[int, dict]]:
    """A kiosk that accepts the text, and answers ``answer`` when asked to mint."""

    def post(path: str, payload: dict) -> tuple[int, dict]:
        if payload.get("preview"):
            return 200, {"matched": True, "exact": True}
        if posts is not None:
            posts.append((path, dict(payload)))
        return answer

    return post


def test_a_start_mints_through_the_kiosks_own_route_and_the_next_visit_to_the_landing_page_lands_there(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig))
    result = facade.start("number two please")
    assert result.accepted and result.error_code == ""
    assert result.topic
    assert rig.app.state.start_slot.waiting()
    assert rig.driver.pop_cmd() == "reload"
    assert not rig.driver.tracker.active, "the session opens when the page does, not before"
    gate = _privacy()
    landing = rig.client.get("/", follow_redirects=False)
    assert landing.status_code == 302 and landing.headers["location"].startswith("/session?")
    sid = landing.headers["location"].split("sid=")[1].split("&")[0]
    assert result.session_id == gate.apply_ids("", sid)[1]
    assert rig.client.get("/", follow_redirects=False).status_code == 200, "once only"
    opened = rig.client.get(landing.headers["location"])
    assert opened.status_code == 200 and rig.driver.tracker.active


def test_the_landing_page_is_what_it_was_when_nothing_asked(real):
    rig = real()
    first = rig.client.get("/", follow_redirects=False)
    assert first.status_code == 200 and "text/html" in first.headers["content-type"]
    assert rig.client.get("/", follow_redirects=False).text == first.text


def test_a_target_nobody_collected_expires_instead_of_starting_a_session_later(real):
    rig = real()
    now = [0.0]
    rig.app.state.start_slot = StartSlot(clock=lambda: now[0])
    SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig)).start("one")
    assert rig.app.state.start_slot.waiting()
    now[0] += 3600.0
    assert rig.client.get("/", follow_redirects=False).status_code == 200


def test_an_empty_request_starts_nothing_and_does_not_even_ask_the_kiosk():
    calls: list[Any] = []
    facade = _facade(post_json=lambda *a: calls.append(a) or (200, {}))
    for text in ("", "   ", None):
        assert facade.start(text) == StartResult(False, error_code="no_match")  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.parametrize("answer,code", [
    ((400, {"error_code": "invalid_qr"}), "invalid_qr"),
    ((409, {"error_code": "qr_already_claimed"}), "qr_already_claimed"),
    ((409, {"error_code": "training_unavailable"}), "training_unavailable"),
    ((409, {"error_code": "badge_unrecognized"}), "invalid_qr"),
    ((409, {"error_code": "something_new"}), "training_unavailable"),
    ((500, {}), "training_unavailable"),
    ((0, {}), "training_unavailable"),
    ((200, {"matched": True}), "training_unavailable"),
    ((200, {"session_url": "https://elsewhere.example/session"}), "training_unavailable"),
    ((200, {"session_url": "//elsewhere.example"}), "training_unavailable"),
    ((200, {"session_url": 7}), "training_unavailable"),
])
def test_a_start_that_cannot_finish_leaves_no_page_waiting_and_no_verb_queued(answer, code):
    app = _fake_app()
    facade = _facade(app, post_json=_mint(answer))
    assert facade.start("one") == StartResult(False, error_code=code)
    assert not app.state.start_slot.waiting() and app.state.driver.queued == []


@pytest.mark.parametrize("preview,code", [
    ((200, {"matched": False, "prompt": "p"}), "no_match"),
    ((200, {"matched": True, "exact": False, "key": "1", "topic": "T"}), "no_match"),
    ((400, {"error_code": "invalid_qr"}), "invalid_qr"),
    ((409, {"error_code": "badge_unrecognized"}), "invalid_qr"),
    ((0, {}), "training_unavailable"),
])
def test_only_a_real_choice_of_the_menu_goes_on_to_a_mint(preview, code):
    posts: list[Any] = []

    def post(path: str, payload: dict) -> tuple[int, dict]:
        posts.append(dict(payload))
        return preview

    app = _fake_app()
    assert _facade(app, post_json=post).start("banana", "BADGE") == StartResult(
        False, error_code=code)
    assert posts == [{"text": "banana", "qr": "BADGE", "preview": True}], "the badge is not claimed"
    assert not app.state.start_slot.waiting() and app.state.driver.queued == []


def test_text_the_menu_does_not_know_starts_no_default_topic_on_the_real_routes(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig))
    assert facade.start("banana") == StartResult(False, error_code="no_match")
    assert not rig.app.state.start_slot.waiting()
    assert rig.client.get("/", follow_redirects=False).status_code == 200


def test_a_start_is_busy_unless_the_kiosk_is_on_an_idle_landing_page(real):
    rig = real()
    posts: list[Any] = []
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=lambda path, payload: (
        posts.append(path) or (200, {})))
    assert facade.start("one").error_code == "busy", "no kiosk has said anything"
    rig.client.get("/api/pending-cmd?verbs=reload&phase=listen")
    assert facade.start("one").error_code == "busy", "someone is being read the menu"
    assert posts == []


def test_a_landing_page_that_went_quiet_is_not_idle():
    now = [0.0]
    seen = LandingSeen(clock=lambda: now[0])
    assert not seen.idle(), "nothing heard yet"
    seen.note("scan")
    assert seen.idle()
    now[0] += LANDING_FRESH_S
    assert not seen.idle()
    seen.note("greet")
    assert not seen.idle()


def test_a_mint_refused_after_the_fact_gives_the_badge_back():
    app = _fake_app()

    def post(_path: str, payload: dict) -> tuple[int, dict]:
        if payload.get("preview"):
            return 200, {"matched": True, "exact": True}
        app.state.driver.open()
        return _MINTED

    assert _facade(app, post_json=post).start("one", "BADGE").error_code == "busy"
    assert app.state.glue.discarded == [("s1", "BADGE")]


class _LeasingExtension(HostExtension):
    """What an add-on does with a badge: the claim reserves selection until the session ends."""

    def __init__(self) -> None:
        super().__init__()
        self.reserved = False

    def claim_badge(self, payload: str) -> str:
        self.reserved = True
        return BADGE_OK

    def bind_session(self, session_id: str) -> bool:
        return True

    def session_opened(self, session_id: str) -> bool:
        return True

    def session_ended(self, session_id: str) -> None:
        self.reserved = False

    def selection_reserved(self) -> bool:
        return self.reserved

    def busy(self) -> bool:
        return self.reserved


def test_a_start_with_a_badge_lands_although_its_own_claim_reserves_selection(
        real, sample_roster, monkeypatch):
    monkeypatch.setattr(server, "DEMO", True)
    extension = _LeasingExtension()
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "fiware_factory": None, "idle": False,
        "gesture": False, "tts": None, "stt": None, "voice": None, "extension": extension})
    app.state.driver.metrics = _Metrics()
    rig = SimpleNamespace(app=app, client=TestClient(app), driver=app.state.driver)
    facade = SessionFacade(app, privacy=_privacy(), post_json=_via(rig))
    assert facade.start("one", "EMORI:anna-001").accepted
    assert extension.selection_reserved(), "the claim holds the lease"
    landing = rig.client.get("/", follow_redirects=False)
    assert landing.status_code == 302 and landing.headers["location"].startswith("/session?")


def test_someone_who_starts_using_the_page_after_the_start_comes_first(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig))
    assert facade.start("one").accepted
    answer = rig.client.get("/api/pending-cmd?verbs=reload&phase=greet").json()
    assert answer.get("cmd") in (None, ""), "the page that said it is busy is not reloaded"
    assert not rig.app.state.start_slot.waiting()
    assert rig.driver.pop_cmd(only=frozenset({"reload"})) is None
    assert rig.client.get("/", follow_redirects=False).status_code == 200
    facade.forget_abandoned()


def test_a_language_change_still_reloads_a_page_that_is_busy(real):
    rig = real()
    rig.driver.enqueue_cmd("reload")
    answer = rig.client.get("/api/pending-cmd?verbs=reload&phase=listen").json()
    assert answer["cmd"] == "reload", "only a waiting start is undone"


def test_a_second_tab_saying_it_is_idle_does_not_hide_the_kiosk_being_used():
    now = [0.0]
    seen = LandingSeen(clock=lambda: now[0])
    seen.note("listen", "kiosk")
    seen.note("scan", "a phone on the same page")
    assert not seen.idle()
    seen.note("scan", "kiosk")
    assert seen.idle()
    now[0] += LANDING_FRESH_S - 1.0
    seen.note("listen", "a phone on the same page")
    assert not seen.idle()
    now[0] += LANDING_FRESH_S
    seen.note("scan", "kiosk")
    assert seen.idle(), "a page that went quiet stops counting"


def test_a_line_the_robot_plays_itself_counts_as_busy():
    now = [0.0]
    seen = LandingSeen(clock=lambda: now[0])
    seen.note("scan")
    seen.hold(3.0)
    assert not seen.idle()
    now[0] += 3.0
    seen.note("scan")
    assert seen.idle()


def test_the_landing_page_says_when_it_is_speaking():
    page = pages.VOICE_HTML
    assert "(__sayOpen>0||cueOpen)?'speaking':phase" in page
    assert "&phase='+encodeURIComponent(doing)+'&page='+pageId" in page


def test_a_reload_is_refused_while_someone_is_using_the_page(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig))
    assert facade.command("reload") == (True, "")
    rig.driver.pop_cmd()
    rig.client.get("/api/pending-cmd?verbs=reload&phase=listen")
    assert facade.command("reload") == (False, "busy")


def test_a_failure_while_building_the_node_leaves_no_fan_out_running(monkeypatch):
    app = _fake_app()
    app.state.hub = SimpleNamespace(tap=None)
    bindings = _bindings()
    created: list[Any] = []
    original = bindings.create_node

    def create_node(*args: Any, **kwargs: Any) -> Any:
        node = original(*args, **kwargs)
        node.create_timer = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no timer"))
        created.append(node)
        return node

    bindings = replace(bindings, create_node=create_node)
    with pytest.raises(RuntimeError):
        sn.build_session_node(app, bindings, environ={}, post_json=_mint())
    assert app.state.hub.tap is None and app.state.fanout is None
    assert not [thread for thread in threading.enumerate()
                if thread.name == "ros-fanout" and thread.is_alive()]
    assert created[0].destroyed


def test_a_badge_claim_that_reserves_selection_does_not_refuse_its_own_start():
    app = _fake_app()
    reserved = [False]
    app.state.glue.robot_idle = lambda: not reserved[0]

    def post(_path: str, payload: dict) -> tuple[int, dict]:
        if payload.get("preview"):
            return 200, {"matched": True, "exact": True}
        reserved[0] = True
        return _MINTED

    assert _facade(app, post_json=post).start("one", "BADGE").accepted
    assert app.state.glue.discarded == []


def test_a_start_nobody_collected_is_undone_once():
    now = [0.0]
    app = _fake_app(start_slot=StartSlot(clock=lambda: now[0]))
    facade = _facade(app, post_json=_mint())
    assert facade.start("one", "BADGE").accepted
    facade.forget_abandoned()
    assert app.state.glue.discarded == [], "it still has time"
    now[0] += 3600.0
    facade.forget_abandoned()
    facade.forget_abandoned()
    assert app.state.glue.discarded == [("s1", "BADGE")]


def test_a_start_the_kiosk_collected_is_not_undone():
    app = _fake_app()
    facade = _facade(app, post_json=_mint())
    assert facade.start("one", "BADGE").accepted
    assert app.state.start_slot.take()
    facade.forget_abandoned()
    assert app.state.glue.discarded == []


def test_a_learners_session_is_not_taken_over_by_a_target_left_waiting(real):
    rig = real()
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=_via(rig))
    assert facade.start("one").accepted
    rig.driver.pop_cmd()
    slot = rig.app.state.start_slot
    learner = rig.client.post("/api/voice-select", json={"text": "one"}).json()
    assert rig.client.get(learner["session_url"]).status_code == 200
    assert rig.driver.tracker.active
    assert not slot.waiting(), "another session opening clears what was waiting"
    assert rig.client.get("/", follow_redirects=False).status_code == 200
    assert slot.abandoned() is not None, "and it goes back to be undone"


def test_nothing_is_handed_out_while_a_session_runs_and_it_is_still_there_afterwards(real):
    rig = real()
    rig.client.get("/api/pending-cmd?verbs=reload&phase=scan")
    slot = rig.app.state.start_slot
    slot.put("/session?demo=1&sid=s9")
    rig.driver.start_session(_info())
    assert rig.client.get("/", follow_redirects=False).status_code == 200
    assert slot.waiting()


def test_a_minted_session_that_is_discarded_is_gone_on_the_real_routes(real):
    rig = real()
    minted = rig.client.post("/api/voice-select", json={"text": "one"}).json()
    sid = minted["session_url"].split("sid=")[1].split("&")[0]
    rig.app.state.glue.discard_minted(sid)
    rig.app.state.glue.discard_minted(sid)  # and twice is the same as once


def test_a_start_is_busy_while_anything_runs_or_an_earlier_one_has_not_been_collected():
    app = _fake_app()
    posts: list[Any] = []
    facade = _facade(app, post_json=_mint(posts=posts))
    app.state.driver.open()
    assert facade.start("one").error_code == "busy"
    assert posts == [], "nothing is minted for a robot that is in use"
    app.state.driver.close()
    assert facade.start("one").accepted
    assert facade.start("one").error_code == "busy", "the first has not been collected"
    assert len(posts) == 1


def test_someone_who_walks_up_while_the_mint_is_out_keeps_the_robot():
    app = _fake_app()
    idle = [True]
    app.state.glue.landing_idle = lambda: idle[0]

    def post(_path: str, payload: dict) -> tuple[int, dict]:
        if not payload.get("preview"):
            idle[0] = False
        return (200, {"matched": True, "exact": True}) if payload.get("preview") else _MINTED

    assert _facade(app, post_json=post).start("one").error_code == "busy"
    assert not app.state.start_slot.waiting() and app.state.driver.queued == []
    assert app.state.glue.discarded == [("s1", "")]


def test_two_starts_at_once_mint_once():
    app = _fake_app()
    entered, release = threading.Event(), threading.Event()
    posts: list[Any] = []

    def post(path: str, payload: dict) -> tuple[int, dict]:
        if payload.get("preview"):
            return 200, {"matched": True, "exact": True}
        posts.append((path, payload))
        entered.set()
        release.wait(10.0)
        return _MINTED

    facade = _facade(app, post_json=post)
    first: list[StartResult] = []
    thread = threading.Thread(target=lambda: first.append(facade.start("one")), daemon=True)
    thread.start()
    assert entered.wait(5.0)
    assert facade.start("one").error_code == "busy"
    release.set()
    thread.join(5.0)
    assert first[0].accepted and len(posts) == 1


def test_what_is_posted_is_the_menu_text_and_the_badge_and_goes_to_the_selection_route():
    seen: list[Any] = []

    def post(path: str, payload: dict) -> tuple[int, dict]:
        seen.append((path, dict(payload)))
        return ((200, {"matched": True, "exact": True}) if payload.get("preview")
                else (200, {"session_url": "/session?demo=1&sid=s1"}))

    _facade(post_json=post).start("  number two ", "BADGE")
    assert seen == [
        ("/api/voice-select", {"text": "number two", "qr": "BADGE", "preview": True}),
        ("/api/voice-select", {"text": "number two", "qr": "BADGE"}),
    ]


class _Echo(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        status = 409 if body.get("text") == "refuse" else 200
        raw = json.dumps({"echo": body, "error_code": "training_unavailable"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: Any) -> None:
        return None


def test_the_loopback_call_reads_a_refusals_body_too_and_ignores_a_proxy(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    try:
        port = httpd.server_address[1]
        status, body = sn._loopback_post("/x", {"text": "hi"}, port=port, timeout=5.0)
        assert status == 200 and body["echo"] == {"text": "hi"}
        status, body = sn._loopback_post("/x", {"text": "refuse"}, port=port, timeout=5.0)
        assert status == 409 and body["error_code"] == "training_unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert sn._loopback_post("/x", {}, port=port, timeout=1.0) == (0, {})


def test_the_port_the_loopback_call_uses_is_the_one_the_server_listens_on():
    seen: list[int] = []
    facade = _facade(environ={sn.PORT_VAR: "8123"})
    original = sn._loopback_post
    try:
        sn._loopback_post = lambda path, payload, *, port, timeout=0: seen.append(port) or (0, {})
        facade.start("one")
    finally:
        sn._loopback_post = original
    assert seen == [8123]


# --- the slot --------------------------------------------------------------------------------


def test_the_slot_hands_a_target_out_once_and_keeps_only_the_latest():
    slot = StartSlot()
    assert slot.take() is None and not slot.waiting()
    slot.put("/session?sid=a")
    slot.put("/session?sid=b")
    assert slot.waiting() and slot.take() == "/session?sid=b" and slot.take() is None


def test_the_slot_expires_a_target():
    now = [0.0]
    slot = StartSlot(ttl_s=30.0, clock=lambda: now[0])
    slot.put("/session?sid=a")
    now[0] = 29.9
    assert slot.waiting()
    now[0] = 30.0
    assert not slot.waiting() and slot.take() is None


@pytest.mark.parametrize("target", [
    "", "/", "//elsewhere.example/session", "http://elsewhere.example/session",
    "/sessionx", "/session\r\nSet-Cookie: a=b", "/session?x=\\\\evil", "session", None, 5])
def test_the_slot_takes_a_session_path_and_nothing_that_could_send_a_kiosk_elsewhere(target):
    slot = StartSlot()
    with pytest.raises(ValueError):
        slot.put(target)  # type: ignore[arg-type]
    assert not slot.waiting()


def test_a_slot_that_is_cleared_is_empty():
    slot = StartSlot()
    slot.put("/session")
    slot.clear()
    assert slot.take() is None


# --- events of the hub's own -----------------------------------------------------------------


def test_a_session_opening_and_closing_are_noted_to_the_fan_out_when_there_is_one(real):
    rig = real()
    notes: list[dict] = []
    rig.app.state.fanout = SimpleNamespace(
        note=lambda kind, **fields_: notes.append({"kind": kind, **fields_}))
    assert rig.client.get("/session?demo=1&sid=20260101-000000-xyz").status_code == 200
    assert rig.driver.tracker.active
    with rig.driver._lock:
        rig.driver._finalize_locked(results=None)
    assert [note["kind"] for note in notes] == ["hub_session_opened", "hub_session_finalized"]
    assert all(note["session_id"] for note in notes)
    assert notes[1]["lang"] == "en"


def test_without_a_fan_out_the_session_runs_as_it_did(real):
    rig = real()
    assert rig.client.get("/session?demo=1&sid=20260101-000000-xyz").status_code == 200
    with rig.driver._lock:
        rig.driver._finalize_locked(results=None)
    assert not rig.driver.tracker.active


def test_a_fan_out_that_raises_cannot_cost_a_session_its_opening(real):
    rig = real()

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("queue")

    rig.app.state.fanout = SimpleNamespace(note=boom)
    assert rig.client.get("/session?demo=1&sid=20260101-000000-xyz").status_code == 200
    assert rig.driver.tracker.active


# --- the node on fake ROS --------------------------------------------------------------------


class _Msg:
    """A message of any of the node's types: fields are plain attributes, a Time nests."""

    def __init__(self) -> None:
        self.header = SimpleNamespace(stamp=None)
        self.source_time = SimpleNamespace(sec=0, nanosec=0)
        self.header.stamp = SimpleNamespace(sec=0, nanosec=0)


class _Publisher:
    def __init__(self, kind: type, topic: str, qos: Any) -> None:
        self.kind, self.topic, self.qos = kind, topic, qos
        self.sent: list[Any] = []
        self.hold: threading.Event | None = None

    def publish(self, message: Any) -> None:
        if self.hold is not None:
            self.hold.wait(30.0)
        self.sent.append(message)


class _Service:
    def __init__(self, kind: type, name: str, callback: Callable, group: Any) -> None:
        self.kind, self.name, self.callback, self.group = kind, name, callback, group

    def call(self, **request: Any) -> Any:
        return self.callback(SimpleNamespace(**request), _Msg())


class _Node:
    def __init__(self, name: str, namespace: str) -> None:
        self.name, self.namespace = name, namespace
        self.publishers: dict[str, _Publisher] = {}
        self.services: dict[str, _Service] = {}
        self.timers: list[tuple[float, Callable, Any]] = []
        self.destroyed = False

    def get_clock(self) -> Any:
        return SimpleNamespace(now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=7, nanosec=5)))

    def create_publisher(self, kind: type, topic: str, qos: Any) -> _Publisher:
        self.publishers[topic] = _Publisher(kind, topic, qos)
        return self.publishers[topic]

    def create_service(self, kind: type, name: str, callback: Callable, *,
                       callback_group: Any = None) -> _Service:
        self.services[name] = _Service(kind, name, callback, callback_group)
        return self.services[name]

    def create_timer(self, period: float, callback: Callable, *, callback_group: Any = None) -> None:
        self.timers.append((period, callback, callback_group))

    def destroy_node(self) -> None:
        self.destroyed = True


class _State(_Msg):
    pass


class _Event(_Msg):
    pass


def _bindings() -> sn.SessionBindings:
    group = object()
    return sn.SessionBindings(
        create_node=_Node, group_cls=lambda: group, latched_qos="LATCHED", events_qos="EVENTS",
        state_type=_State, event_type=_Event, command_type="Command", start_type="Start",
        voice_menu_type="Menu", tt_overrides_type="Tt")


@pytest.fixture
def built(real):
    made: list[Any] = []

    def build(**kwargs: Any) -> tuple[Any, Any]:
        rig = real()
        node = sn.build_session_node(
            rig.app, _bindings(), environ={"EMORI_ROS_ID_SALT": SALT, **kwargs.pop("environ", {})},
            **kwargs)
        made.append(node)
        return rig, node

    yield build
    for node in made:
        node.destroy_node()


def test_the_node_has_the_interface_names_and_qos_the_design_gives(built):
    rig, node = built()
    assert (node.name, node.namespace) == ("session", "/emori")
    assert node.publishers["/emori/session/state"].qos == "LATCHED"
    assert node.publishers["/emori/session/events"].qos == "EVENTS"
    assert set(node.services) == {"/emori/session/command", "/emori/session/start",
                                  "/emori/session/voice_menu", "/emori/session/set_tt_overrides"}
    assert len({id(service.group) for service in node.services.values()}) == 1
    assert node.timers[0][0] == sn.POLL_S


def test_the_state_is_published_at_once_so_a_late_subscriber_has_one_to_find(built):
    _rig, node = built()
    sent = node.publishers["/emori/session/state"].sent
    assert len(sent) == 1 and sent[0].phase == sn.PHASE_NO_SESSION and sent[0].seq == 1
    assert (sent[0].header.stamp.sec, sent[0].header.stamp.nanosec) == (7, 5)


def test_the_timer_publishes_a_change_and_only_a_change(built):
    rig, node = built()
    sent = node.publishers["/emori/session/state"].sent
    _period, tick, _group = node.timers[0]
    tick()
    tick()
    assert len(sent) == 1
    rig.driver.start_session(_info())
    tick()
    assert len(sent) == 2 and sent[1].phase == sn.PHASE_BOOTING and sent[1].seq == 2


def test_a_timer_tick_that_fails_is_logged_and_does_not_raise(built, monkeypatch, caplog):
    _rig, node = built()
    monkeypatch.setattr(node.session_facade, "next_state",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    node.timers[0][1]()
    assert "could not be published" in caplog.text


def test_a_command_over_the_service_ends_the_session_and_answers(built):
    rig, node = built()
    rig.driver.start_session(_info())
    answer = node.services["/emori/session/command"].call(verb="end", reason="")
    assert (answer.accepted, answer.error_code) == (True, "")
    assert rig.driver.counters["ros_end_requested"] == 1
    refused = node.services["/emori/session/command"].call(verb="reload", reason="")
    assert (refused.accepted, refused.error_code) == (False, "busy")


def test_a_service_that_fails_still_answers(built, monkeypatch):
    _rig, node = built()
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
    for name in ("command", "start", "voice_menu", "set_tt_overrides"):
        monkeypatch.setattr(node.session_facade, name, boom)
    command = node.services["/emori/session/command"].call(verb="end", reason="")
    assert (command.accepted, command.error_code) == (False, "internal")
    start = node.services["/emori/session/start"].call(menu_text="one", badge_payload="")
    assert (start.accepted, start.error_code) == (False, "internal")
    menu = node.services["/emori/session/voice_menu"].call(lang="")
    assert menu.prompt == "" and menu.option_keys == []
    tt = node.services["/emori/session/set_tt_overrides"].call(patch_json="{}")
    assert (tt.ok, tt.error_code) == (False, "internal")


def test_a_start_over_the_service_answers_with_the_topic_and_a_pseudonym(built):
    holder: dict[str, Any] = {}
    rig, node = built(post_json=lambda path, payload: holder["post"](path, payload))
    holder["post"] = _via(rig)
    answer = node.services["/emori/session/start"].call(menu_text="number two please",
                                                        badge_payload="")
    assert answer.accepted and answer.topic and answer.session_id.startswith("p-")
    assert answer.error_code == ""


def test_the_voice_menu_service_fills_every_list(built):
    rig, node = built()
    answer = node.services["/emori/session/voice_menu"].call(lang="")
    served = rig.client.get("/api/voice-menu").json()
    assert answer.prompt == served["prompt"]
    assert answer.option_keys == [option["key"] for option in served["options"]]


def test_the_envelopes_the_relay_carries_come_out_as_events_with_the_gate_applied(built):
    rig, node = built()
    events = node.publishers["/emori/session/events"]
    rig.app.state.hub.publish(json.dumps({
        "type": "pokamind_tts_ended", "session_id": "s-1", "person_id": "student-7", "lang": "en"}))
    assert _wait_until(lambda: events.sent)
    assert events.sent[0].type == "pokamind_tts_ended"
    assert events.sent[0].person_id == _privacy().apply_ids("student-7", "s-1")[0]
    assert events.sent[0].header.stamp is not None


def test_a_session_events_gap_free_numbering_survives_the_hub_notes(built):
    rig, node = built()
    events = node.publishers["/emori/session/events"]
    rig.client.get("/session?demo=1&sid=20260101-000000-xyz")
    with rig.driver._lock:
        rig.driver._finalize_locked(results=None)
    assert _wait_until(lambda: len(events.sent) == 2)
    assert [e.type for e in events.sent] == ["hub_session_opened", "hub_session_finalized"]
    assert [e.seq for e in events.sent] == [1, 2]


def test_destroying_the_node_stops_the_fan_out_and_gives_the_relay_its_tap_back(built):
    rig, node = built()
    assert rig.app.state.hub.tap is not None
    node.destroy_node()
    assert node.destroyed and rig.app.state.hub.tap is None
    assert node.fanout.stats()["running"] is False


# --- off the path a session's motion runs on --------------------------------------------------


_MOTION_PATH = {"on_utterance", "on_sentence", "plan_cue", "_play_batch", "submit",
                "submit_legacy_motion", "submit_batch", "player", "cue", "RobotPlayer",
                "CueScheduler", "play_recorded_move", "stop_recorded_move"}


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.alias):
            out.add(node.name.split(".")[-1])
    return out


def test_the_module_names_nothing_of_the_cue_player_or_submit_path():
    assert _names(_SOURCE) & _MOTION_PATH == set()


def test_the_cycle_route_does_not_know_about_this_node_or_the_start_slot():
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    cycle = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "cycle")
    mentioned = {n.attr for n in ast.walk(cycle) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(cycle) if isinstance(n, ast.Name)}
    assert not mentioned & {"start_slot", "fanout", "watchdog_ticker", "_note_session"}


def test_the_only_thing_the_node_hangs_on_the_relay_is_the_put_only_tap(built):
    rig, node = built()
    assert rig.app.state.hub.tap == node.fanout._tap


def test_a_publisher_that_has_stopped_answering_does_not_slow_the_request_thread(built):
    rig, node = built()
    stuck = node.publishers["/emori/session/events"]
    stuck.hold = threading.Event()
    hub = rig.app.state.hub
    try:
        hub.publish(json.dumps({"type": "pokamind_tts_ended", "session_id": "s"}))
        time.sleep(0.05)
        started = time.perf_counter()
        for _ in range(200):
            hub.publish(json.dumps({"type": "pokamind_tts_ended", "session_id": "s"}))
        spent = time.perf_counter() - started
        assert spent < 1.0, f"200 publishes took {spent:.3f}s behind a stuck publisher"
    finally:
        stuck.hold.set()


def test_a_cycle_posted_while_a_start_waits_on_the_kiosk_is_answered_at_once(real):
    """The loopback call blocks a facade thread for as long as the kiosk takes; the cycle must not."""

    rig = real()
    entered, release = threading.Event(), threading.Event()

    def slow(_path: str, _payload: dict) -> tuple[int, dict]:
        entered.set()
        release.wait(30.0)
        return 0, {}

    _via(rig)
    facade = SessionFacade(rig.app, privacy=_privacy(), post_json=slow)
    thread = threading.Thread(target=lambda: facade.start("one"), daemon=True)
    thread.start()
    try:
        assert entered.wait(5.0)
        started = time.perf_counter()
        answer = rig.client.post("/api/cycle", json={
            "type": "pokamind_tts_ended", "session_id": "s1"})
        assert answer.status_code == 200 and time.perf_counter() - started < 2.0
    finally:
        release.set()
        thread.join(5.0)
