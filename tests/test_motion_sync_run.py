"""scripts/motion_sync_run.py: the scripted motion run, with fakes for the robot.

CI has no ROS, no robot and no PipeWire. Everything that would touch them is a fake with a
log, and everything else is the real thing: the session goes through the real hostapp app,
the real driver, the real dashboard store and the real trace writer, over its HTTP routes.
What is pinned:

* the fixture covers every case the run promises, and the real cue scheduler agrees with
  what each case expects;
* a tracked session opens over existing routes, with no network and no Pokamind call;
* a run leaves nothing behind, and a neighbouring real row survives it;
* no completion envelope can be sent, so no mail and no results file;
* each refusal, and that refusing touches nothing;
* the restore, on a finished run, a failed run, SIGTERM, SIGINT and the time bound.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import motion_sync_run as run

fixture = run.fixture
audio = run.audio_calibrate
probe = run.joints_probe

_REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------


class FakeHttp:
    """Answers from a table of ``{path: (status, body)}`` and logs what was asked."""

    def __init__(self, routes=None, log=None):
        self.routes = routes or {}
        self.log = [] if log is None else log
        self.closed = False

    def get(self, path):
        self.log.append(("GET", path))
        return self.routes.get(path, (404, None))

    def post(self, path, payload=None):
        self.log.append(("POST", path, payload))
        return self.routes.get(path, (200, {"ok": True}))

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class FakeMachine(run.Machine):
    """The robot's shell: every command is logged and answered, none is run."""

    def __init__(self, root, *, kiosk=None, hostapp_env=None, free=10**10, tools=True,
                 broker=False, container=None):
        super().__init__(root)
        self.kiosk = kiosk or {"LoadState": "loaded", "ActiveState": "active",
                               "SubState": "running", "MainPID": "11"}
        self.hostapp_env = {} if hostapp_env is None else hostapp_env
        self.free = free
        self.tools = tools
        self.broker = broker
        self.container = {} if container is None else container
        self.commands = []
        self.spawned = []

    def env(self):
        return {}

    def run(self, argv, *, timeout=20.0):
        self.commands.append(list(argv))
        if argv[:3] == ["sudo", "-n", "systemctl"] and argv[3] == "start":
            self.kiosk = dict(self.kiosk, ActiveState="active", SubState="running")
        if argv[:3] == ["sudo", "-n", "systemctl"] and argv[3] == "stop" and argv[4] == run.KIOSK_UNIT:
            self.kiosk = dict(self.kiosk, ActiveState="inactive", SubState="dead")
        return 0, ""

    def spawn(self, argv, **kwargs):
        self.spawned.append(list(argv))
        proc = FakeProc()
        proc.returncode = 0 if argv[0] == "pw-play" else None   # the track has already played
        return proc

    def which(self, tool):
        return f"/usr/bin/{tool}" if self.tools else None

    def free_bytes(self, path):
        return self.free

    def unit_state(self, unit):
        return dict(self.kiosk) if unit == run.KIOSK_UNIT else {"MainPID": "22"}

    def process_env(self, pid):
        return self.hostapp_env

    def broker_answers(self, url):
        return self.broker

    def container_env(self, container):
        return self.container

    def current_slice(self):
        return "0::/system.slice/motion-sync-run.service"

    def sleep(self, seconds):
        time.sleep(min(seconds, 0.005))     # yields, and takes no real time

    def mutations(self):
        """Commands that change something: anything but ``systemctl show``."""

        return [c for c in self.commands if c[:2] != ["systemctl", "show"]
                and c[:2] != ["docker", "inspect"]]


class FakeJoints:
    def __init__(self, path, *, silent=False):
        self.path = path
        self.silent = silent
        self.started = False
        self.stopped = False

    def start(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text('{"t":1.0,"rpy":[0,0,0],"ant":[0,0],"yaw":0}\n', encoding="utf-8")
        self.started = True

    def wait_first(self, timeout_s=0.0):
        if self.silent:
            raise run.RunAborted("no motion sample")

    def stop(self):
        self.stopped = True


def healthy_status(**fields):
    status = {"session": None, "robot": True, "last_present_s": 90000.0, "fiware": False,
              "motion_trace": {"enabled": True}, "voice_link": {"available": True}}
    status.update(fields)
    return status


def healthy_routes(**fields):
    return {"/api/status": (200, healthy_status(**fields))}


def healthy_daemon():
    return FakeHttp({"/api/daemon/status": (200, {"state": "running", "backend_status": {
        "motor_control_mode": "enabled"}})})


# --------------------------------------------------------------------------------------
# the fixture
# --------------------------------------------------------------------------------------


def test_both_languages_run_the_same_cases_in_the_same_order():
    en, ca = fixture.build("en"), fixture.build("ca")
    assert [s.case for s in en] == [s.case for s in ca]
    assert [(s.family, s.expect_family, s.audio, s.gap_ms, s.seq) for s in en] \
        == [(s.family, s.expect_family, s.audio, s.gap_ms, s.seq) for s in ca]
    for sentences in (en, ca):
        spoken = [s.text for s in sentences if s.text]
        # Reuse of a pool line is deliberate, but a text never repeats inside a burst of seq.
        assert all(isinstance(t, str) and t == t.strip() for t in spoken)
        assert not [s.case for s in sentences if s.audio and not s.parts]


@pytest.mark.parametrize("lang", fixture.LANGS)
def test_the_fixture_has_every_promised_case(lang):
    sentences = fixture.build(lang)
    cases = [s.case for s in sentences]
    for offset in (-6400, -3500, -1000, -150, 0, 40, 80, 150):
        assert [s for s in sentences if s.event_offset_ms == offset]
    for gap in (0, 30, 100, 300):
        assert [s for s in sentences if s.gap_ms == gap and "second" in s.case]
    for family in ("tension", "levity", "energy", "intimacy", "softening", "release"):
        assert any(c.startswith(f"family_{family}") for c in cases)
    for needed in ("one_word_a", "normal_a", "long_a", "comma_pause_a", "breath_a", "no_family_a",
                   "carried_1", "body_forces_tension", "cue_only_audio", "cue_only_silent",
                   "burst_a_0", "burst_a_1", "burst_a_2", "engagement_inside_a", "firm_hold_a",
                   "covering_line", "covering_overlap_first"):
        assert needed in cases
    by_case = {s.case: s for s in sentences}
    assert by_case["comma_pause_a"].pause_s == pytest.approx(0.7)
    assert by_case["breath_a"].pause_s == pytest.approx(0.4)
    assert len(by_case["comma_pause_a"].parts) == 2
    assert by_case["cue_only_audio"].text == "" and by_case["cue_only_audio"].audio
    assert by_case["cue_only_silent"].text == "" and not by_case["cue_only_silent"].audio
    assert not by_case["covering_line"].posted
    assert by_case["engagement_inside_a"].engagement_at_s
    assert len(by_case["long_a"].text) >= 150  # about 12 s at Piper's rate


@pytest.mark.parametrize("lang", fixture.LANGS)
def test_exceptions_name_a_real_class_and_only_where_allowed(lang):
    sentences = fixture.build(lang)
    allowed = {"E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"}
    named = [s for s in sentences if s.expected_exception is not None]
    assert named and all(s.expected_exception in allowed for s in named)
    # Sentences with no audio are E6, and E6 is only for sentences with no audio.
    assert {s.case for s in sentences if not s.audio} \
        == {s.case for s in sentences if s.expected_exception == "E6"}
    assert {s.case for s in sentences if s.expected_exception == "E5"} == {"one_word_a", "one_word_b"}
    assert {s.case for s in sentences if s.expected_exception == "E8"} \
        == {s.case for s in sentences if s.burst is not None} | {"covering_overlap_first"}


def test_the_manifest_names_the_silence_inside_a_sentence():
    pause = _sentence("p", parts=("one", "two"), pause_s=0.5, seq=1)
    place = run.Placement(pause, 800, 2400, (800, 1800), 0.1, 0.0)
    assert run._pauses(place, 1000) == [[1.3, 1.8]]


@pytest.mark.parametrize("lang", fixture.LANGS)
def test_the_real_scheduler_performs_the_family_each_case_expects(lang):
    """The words stay out of the planner's way, so a case's family is the case's doing."""

    from emori_adapter.behavior.cue_scheduler import AffectSnapshot, CueScheduler
    from emori_adapter.behavior.cues import CUE_FAMILIES, CueConfig

    assert set(fixture.FAMILIES) == set(CUE_FAMILIES)
    scheduler = CueScheduler(lambda intents, guard: True, config=CueConfig())
    for sentence in fixture.build(lang):
        if not sentence.posted:
            continue
        result = scheduler.on_sentence(text=sentence.text, cue_category=sentence.family,
                                       cue_body=sentence.cue_body, lang=lang,
                                       affect=AffectSnapshot())
        assert (result.family or None) == sentence.expect_family, sentence.case
        if sentence.turn_end:
            scheduler.on_tts_ended()


# --------------------------------------------------------------------------------------
# the layout and the schedule
# --------------------------------------------------------------------------------------


RATE = 1000


def _sentence(case, **fields):
    fields.setdefault("parts", ("x",))
    fields.setdefault("text", "x")
    return fixture.Sentence(case=case, **fields)


def _lay(*sentences, lengths=None):
    table = lengths or {s.case: tuple(1000 for _ in s.parts) for s in sentences}
    return run.lay_out(tuple(sentences), table, RATE)


def test_sentences_are_placed_by_samples_with_their_gaps_and_pauses():
    first = round((run.PILOT_START_S + run.PILOT_S + run.LEAD_IN_S) * RATE)
    a = _sentence("a", gap_ms=0.0)
    b = _sentence("b", gap_ms=300.0, parts=("p", "q"), pause_s=0.7)
    c = _sentence("c", gap_ms=0.0)
    placed = _lay(a, b, c, lengths={"a": (1000,), "b": (500, 400), "c": (100,)})
    assert placed[0].start == first and placed[0].end == first + 1000
    assert placed[1].start == first + 1300
    assert placed[1].part_starts == (first + 1300, first + 1300 + 500 + 700)
    assert placed[1].end == first + 1300 + 500 + 700 + 400
    assert placed[2].start == placed[1].end


def test_an_early_event_lands_after_the_previous_sentence_ended():
    a = _sentence("a", gap_ms=0.0)
    b = _sentence("b", event_offset_ms=-6400.0, gap_ms=100.0)
    placed = _lay(a, b)
    assert placed[1].event_at_s > placed[0].end / RATE
    assert placed[1].event_at_s == pytest.approx(placed[1].start / RATE - 6.4)
    assert placed[1].event_offset_ms == -6400.0


def test_a_negative_gap_overlaps_and_a_burst_lands_inside_50_ms():
    cover = _sentence("cover", posted=False, gap_ms=0.0)
    under = _sentence("under", gap_ms=-300.0)
    placed = _lay(cover, under)
    assert placed[1].start == placed[0].end - 300

    burst = [_sentence(f"b{i}", burst=i, gap_ms=0.0) for i in range(3)]
    placed = _lay(*burst)
    assert placed[2].event_at_s - placed[0].event_at_s == pytest.approx(0.05)
    assert placed[2].start == placed[1].end
    assert placed[1].event_offset_ms == pytest.approx(25.0 - 1000.0)


def test_a_sentence_with_no_audio_keeps_a_slot_and_no_times_in_the_manifest():
    silent = _sentence("silent", audio=False, parts=(), seq=7, expected_exception="E6")
    placed = _lay(silent, lengths={})
    assert placed[0].end - placed[0].start == round(fixture.NO_AUDIO_SLOT_S * RATE)
    row = run.build_manifest(run_id="r", commit="c", profile="loaded", lang="en",
                             motion_source="ros", k_ms=None, t_wav0=100.0, placements=placed,
                             rate=RATE)["sentences"][0]
    assert row["wav_start_s"] is None and row["wav_end_s"] is None and row["audio"] is False


def test_the_schedule_keeps_the_session_alive_inside_the_walkaway_limit():
    sentences = tuple(s for s in fixture.build("en"))
    lengths = {s.case: tuple(20000 for _ in s.parts) for s in sentences if s.audio}
    placements = run.lay_out(sentences, lengths, 22050)
    schedule = run.build_schedule(placements, 22050)
    assert [p.at_s for p in schedule] == sorted(p.at_s for p in schedule)
    assert run.KEEPALIVE_S * 2 < 45.0
    times = [p.at_s for p in schedule if p.kind == run.ANALYSIS]
    assert times and max(b - a for a, b in zip(times, times[1:])) <= run.KEEPALIVE_S
    utterances = [p for p in schedule if p.kind == run.UTTERANCE]
    assert len(utterances) == sum(1 for s in sentences if s.posted)
    assert [p for p in schedule if p.kind == run.TTS_ENDED]


def test_the_engagement_case_lands_inside_its_sentence():
    sentence = _sentence("long", gap_ms=0.0, engagement_at_s=(2.0,))
    placed = _lay(sentence, lengths={"long": (9000,)})
    inside = [p for p in run.build_schedule(placed, RATE) if p.kind == run.ANALYSIS]
    assert inside[0].at_s == pytest.approx(placed[0].start / RATE + 2.0)


def test_the_manifest_has_exactly_the_agreed_fields():
    sentence = _sentence("a", seq=3, family="energy", cue_body="eager", expect_family="energy")
    cover = _sentence("cover", posted=False)
    placed = _lay(sentence, cover)
    manifest = run.build_manifest(run_id="r1", commit="abc", profile="loaded", lang="en",
                                  motion_source="ros", k_ms=12.5, t_wav0=100.0, placements=placed,
                                  rate=RATE)
    assert set(manifest) == {"schema", "run_id", "commit", "profile", "lang", "motion_source",
                             "k_ms", "t_wav0", "sentences", "voice_delay_ms"}
    assert manifest["schema"] == 1
    assert len(manifest["sentences"]) == 1  # the covering line has no event
    row = manifest["sentences"][0]
    assert set(row) == {"seq", "case", "family", "cue_body_given", "expect_family", "chars", "audio",
                        "wav_start_s", "wav_end_s", "pauses", "event_offset_ms",
                        "t_event_planned",
                        "expected_exception"}
    assert row["cue_body_given"] is True and row["seq"] == 3
    assert row["t_event_planned"] == pytest.approx(100.0 + placed[0].event_at_s)
    assert "text" not in row


# --------------------------------------------------------------------------------------
# what is posted
# --------------------------------------------------------------------------------------


def test_an_utterance_is_the_envelope_the_page_sends():
    plain = _sentence("a", text="Hello.", seq=4, source="scenario")
    envelope = run.utterance_envelope(plain, "ca")
    assert set(envelope) == {"type", "text", "seq", "source", "lang", "timestamp"}
    assert (envelope["type"], envelope["text"], envelope["seq"], envelope["source"],
            envelope["lang"]) == ("pokamind_ai_utterance", "Hello.", 4, "scenario", "ca")
    assert envelope["timestamp"].endswith("Z")

    cued = _sentence("b", text="Hello.", seq=5, family="levity", cue_body="x" * 300)
    envelope = run.utterance_envelope(cued, "en")
    assert envelope["cue_category"] == "levity" and len(envelope["cue_body"]) == 200


def test_the_envelope_types_are_the_ones_the_hostapp_routes():
    from emori_adapter.sources.websocket import AI_UTTERANCE_EVENT, ANALYSIS_EVENT, TTS_ENDED_EVENT

    assert run.UTTERANCE == AI_UTTERANCE_EVENT
    assert run.ANALYSIS == ANALYSIS_EVENT
    assert run.TTS_ENDED == TTS_ENDED_EVENT


@pytest.mark.parametrize("kind", ["pokamind_session_completed", "pokamind_turn_armed", "other", None])
def test_a_run_cannot_post_anything_else(kind):
    client = FakeHttp()
    with pytest.raises(ValueError):
        run.post_cycle(client, {"type": kind})
    assert client.log == []


def _driver(tmp_path):
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.hostapp.metrics_store import MetricsStore
    from emori_adapter.robot.client import DryRunRobotClient

    return LiveDriver(DryRunRobotClient(), fiware_factory=None,
                      metrics=MetricsStore(str(tmp_path / "dashboard.db")))


def test_the_calm_cycle_says_someone_is_there_and_does_not_escalate(tmp_path):
    from emori_adapter.behavior.cues import CueConfig
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver(tmp_path)
    driver.start_session(SessionInfo(session_id="s", person_id="p", lang="en"))
    for seq in range(30):
        stamped = driver.stamp(run.analysis_envelope(seq))
        before = driver.last_activity
        driver.last_activity = before - 1000.0
        assert driver.on_analysis(stamped)["ok"] is True
        assert driver.last_activity > before - 1000.0      # touches the walk-away clock
    assert driver._body_ever_seen is True                  # the camera lane's presence
    assert driver.last_body_seen_at is not None
    assert driver._cue_affect.escalation < CueConfig().deescalate_at
    assert driver.tracker.cycles == 30


# --------------------------------------------------------------------------------------
# the real hostapp, its real store and its real trace
# --------------------------------------------------------------------------------------


class AppHttp:
    """The harness's HTTP interface over the real app, in process."""

    def __init__(self, client):
        self._client = client

    @staticmethod
    def _answer(response):
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def get(self, path):
        return self._answer(self._client.get(path))

    def post(self, path, payload=None):
        return self._answer(self._client.post(path, json={} if payload is None else payload))

    def close(self):
        pass


class _Mic:
    profile = "idle"

    def set_profile(self, name):
        self.profile = name


@pytest.fixture
def real(monkeypatch, tmp_path):
    """The hostapp as deployed, with every durable write under ``tmp_path``."""

    from starlette.testclient import TestClient

    from emori_adapter import motion_trace
    from emori_adapter.hostapp import metrics_store, server
    from emori_adapter.hostapp.driver import Recorder
    from emori_adapter.robot.client import DryRunRobotClient
    from tests.test_robot_token_hostapp import use_golden_token

    use_golden_token(monkeypatch, server)
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "RESULTS_FILE", str(tmp_path / "artifacts" / "last_results.json"))

    real_connect = socket.socket.connect

    def loopback_only(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        assert host in ("127.0.0.1", "::1", "localhost"), "a run must not reach the network"
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)

    store = metrics_store.MetricsStore(str(tmp_path / "artifacts" / "dashboard.db"))
    metrics_store.set_store(store)
    trace = motion_trace.start(tmp_path / "artifacts" / "motion-trace")
    assert trace is not None
    ended = []
    app = server._build_app(overrides={
        "watchdog": False, "idle": False, "gesture": False, "voice": None,
        "fiware_factory": None, "robot_client": DryRunRobotClient(),
        "mic_profile": _Mic(), "extension": server.HostExtension(),
        "recorder": Recorder(directory=str(tmp_path / "artifacts" / "recordings")),
        "on_session_end": lambda stats: ended.append(stats), "scope_background": False,
    })
    client = TestClient(app)
    yield SimpleNamespace(app=app, client=client, http=AppHttp(client), store=store,
                          ended=ended, root=tmp_path, driver=app.state.driver)
    motion_trace.stop()
    store.close()
    metrics_store.set_store(None)


def _rows(db, table, where="1=1", params=()):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
    finally:
        conn.close()


def _db(real_fixture):
    return real_fixture.root / "artifacts" / "dashboard.db"


def _paths(real_fixture):
    return run.Paths.for_hostapp(real_fixture.root, {})


def test_a_session_opens_as_tracked_over_the_existing_routes_with_a_recognisable_id(real):
    sid = run.mint_session(real.http, "r1")
    assert real.driver.tracker.active is False          # a mint alone starts nothing
    run.open_session(real.http, sid)
    assert real.driver.tracker.active is True
    assert real.driver.tracker.info.session_id == sid
    assert sid.startswith("motion-sync-r1-")
    assert run.is_own_session(sid, "r1")
    status = run.status_of(real.http)
    assert run.active_session_id(status) == sid
    assert status["fiware"] is False

    # So a sentence is planned, not suppressed as head_reserved.
    code, reply = run.post_cycle(real.http, run.utterance_envelope(
        _sentence("a", text="The rota goes up on Monday.", seq=1), "en"))
    assert code == 200 and reply["spoke"] is True
    assert real.driver.counters["cue_planned"] == 1
    assert real.driver.counters.get("cue_suppressed", 0) == 0


def test_a_tracked_session_is_what_releases_the_head_for_the_cue_lane(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import metrics_store, server
    from emori_adapter.hostapp.driver import Recorder
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    store = metrics_store.MetricsStore(str(tmp_path / "dashboard.db"))
    metrics_store.set_store(store)
    try:
        app = server._build_app(overrides={
            "watchdog": False, "idle": False, "gesture": True, "voice": None,
            "fiware_factory": None, "robot_client": DryRunRobotClient(), "mic_profile": _Mic(),
            "extension": server.HostExtension(), "scope_background": False,
            "recorder": Recorder(directory=str(tmp_path / "recordings"))})
        http = AppHttp(TestClient(app))
        ticker = app.state.gesture_ticker
        assert ticker is not None
        assert ticker.head_reserved_for_sensing() is True  # no session: the head is an input
        run.open_session(http, run.mint_session(http, "r1"))
        assert ticker.head_reserved_for_sensing() is False
    finally:
        store.close()
        metrics_store.set_store(None)


def test_the_signed_login_goes_nowhere(real):
    # The reply holds a real signed login, and the harness keeps only the session id.
    code, reply = real.http.post("/api/start", {"student_id": run.student_id("r1"),
                                                "email": run.SYNTHETIC_EMAIL})
    assert code == 200 and reply["demo"] is False and "sso_url" in reply

    log = []

    class Spy(AppHttp):
        def get(self, path):
            log.append(path)
            return super().get(path)

    spy = Spy(real.client)
    sid = run.mint_session(spy, "r1")
    assert isinstance(sid, str) and "token" not in sid and "sso" not in sid
    run.open_session(spy, sid)
    assert all("token" not in path and "sso" not in path for path in log)


def test_ending_the_session_sends_no_mail_writes_no_results_and_arms_no_verb(real):
    sid = run.mint_session(real.http, "r1")
    run.open_session(real.http, sid)
    run.post_cycle(real.http, run.analysis_envelope(1))
    run.post_cycle(real.http, run.utterance_envelope(
        _sentence("a", text="The rota goes up on Monday.", seq=1), "en"))

    minted = []
    assert run.end_session(real.http, "r1", wait=lambda s: None, on_minted=minted.append) is True

    assert real.driver.tracker.active is False
    assert real.ended == []                                          # the completion hook
    assert not (real.root / "artifacts" / "last_results.json").exists()
    assert not (real.root / "artifacts" / "outbox").exists()
    assert real.driver._pending_cmd is None                          # nothing for the next page
    assert len(minted) == 1 and run.is_own_session(minted[0], "r1")


def test_ending_a_session_that_never_exchanged_anything_also_works(real):
    run.open_session(real.http, run.mint_session(real.http, "r1"))
    assert run.end_session(real.http, "r1", wait=lambda s: None) is True
    assert real.driver.tracker.active is False
    assert real.ended == []


def test_ending_never_touches_a_session_this_run_did_not_open(real):
    run.open_session(real.http, run.mint_session(real.http, "someone-else"))
    active = real.driver.tracker.info.session_id
    assert run.end_session(real.http, "r1", wait=lambda s: None) is False
    assert real.driver.tracker.info.session_id == active


def test_an_end_left_by_a_watchdog_is_drained_before_the_kiosk_returns(real):
    real.driver.enqueue_cmd("end")
    run.drain_stale_verbs(real.http)
    assert real.driver._pending_cmd is None

    # A page-level verb that is not addressed to a session is left alone.
    real.driver.enqueue_cmd("reload")
    run.drain_stale_verbs(real.http)
    assert real.driver._pending_cmd == "reload"


def test_the_purge_removes_this_run_and_a_neighbouring_real_row_survives(real):
    neighbour = "anna-001-20261102-101500-feedfacefeedface"
    real.store.session_started(neighbour, person_id="anna-001", lang="en")
    real.store.log_event("session_started", session_id=neighbour)
    real.store.session_finished(neighbour, status="completed", avg_engagement=0.5)
    # A row that shares this run's second, the only part of an id the two have in common.
    conn = sqlite3.connect(str(_db(real)))
    with conn:
        conn.execute("INSERT INTO cycles (session_id, ts) VALUES (?, ?)",
                     ("20261102-101500-feedfacefeedface", 1.0))
    conn.close()
    recordings = real.root / "artifacts" / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    (recordings / f"{neighbour}.jsonl").write_text("{}\n", encoding="utf-8")

    sid = run.mint_session(real.http, "r1")
    run.open_session(real.http, sid)
    run.post_cycle(real.http, run.analysis_envelope(1))
    run.post_cycle(real.http, run.utterance_envelope(
        _sentence("a", text="The rota goes up on Monday.", seq=1), "en"))
    closing = []
    assert run.end_session(real.http, "r1", wait=lambda s: None, on_minted=closing.append)

    ids = [sid, *closing]
    from emori_adapter.hostapp.metrics_store import deidentify_session_id

    keys = [deidentify_session_id(i) for i in ids]
    assert not any(key.startswith("motion-sync") for key in keys)    # why the prefix cannot find it
    assert _rows(_db(real), "sessions", "session_id=?", (keys[0],))
    assert (recordings / f"{sid}.jsonl").exists()

    removed = run.purge_rows(_db(real), ids, "r1")
    deleted = run.purge_files(_paths(real), ids, "r1")

    assert removed["sessions"] == 2 and removed["cycles"] >= 1 and removed["persons"] == 2
    for key in keys:
        for table in ("sessions", "cycles", "events"):
            assert not _rows(_db(real), table, "session_id=?", (key,))
    assert not (recordings / f"{sid}.jsonl").exists() and deleted

    # The neighbour: its session, its cycle, its event, its person and its recording.
    kept = "20261102-101500-feedfacefeedface"
    assert len(_rows(_db(real), "sessions", "session_id=?", (kept,))) == 1
    assert len(_rows(_db(real), "cycles", "session_id=?", (kept,))) == 1
    assert _rows(_db(real), "events", "session_id=?", (kept,))
    assert len(_rows(_db(real), "persons")) == 1
    assert (recordings / f"{neighbour}.jsonl").exists()


@pytest.mark.parametrize("bad", ["anna-001-20261102-101500-feedfacefeedface",
                                 "motion-sync-other-20261102-101500-feedfacefeedface",
                                 "motion-sync-r1", "20261102-101500-feedfacefeedface", "", None])
def test_the_purge_refuses_any_id_this_run_did_not_mint(tmp_path, bad):
    db = tmp_path / "x.db"
    sqlite3.connect(str(db)).close()
    with pytest.raises(ValueError):
        run.purge_rows(db, [bad], "r1")
    with pytest.raises(ValueError):
        run.purge_files(run.Paths(root=tmp_path, db=db, outbox=tmp_path), [bad], "r1")


def test_a_person_row_another_session_names_is_kept(real):
    sid = run.mint_session(real.http, "r1")
    run.open_session(real.http, sid)
    code = _rows(_db(real), "sessions")[0][3]
    conn = sqlite3.connect(str(_db(real)))
    with conn:
        conn.execute("INSERT INTO sessions (session_id, person_code) VALUES ('other', ?)", (code,))
    conn.close()
    removed = run.purge_rows(_db(real), [sid], "r1")
    assert removed["persons"] == 0
    assert len(_rows(_db(real), "persons", "person_code=?", (code,))) == 1


def test_the_trace_is_found_where_the_writer_puts_it(real):
    from emori_adapter import motion_trace

    sid = run.mint_session(real.http, "r1")
    run.open_session(real.http, sid)
    motion_trace.emit("voice", edge="rise")
    motion_trace.set_session(None)
    assert run.trace_path(_paths(real), sid).exists()


# --------------------------------------------------------------------------------------
# a whole run, on the real app, with the audio and the robot faked
# --------------------------------------------------------------------------------------


def _synth(_factory=None):
    """A voice of steady noise: half a second a line, and always the same for a given line."""

    def speak(text):
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        return np.concatenate([np.zeros(200), rng.uniform(-0.3, 0.3, 4000), np.zeros(200)]), 8000

    return speak


class FakeCapture:
    """Hears exactly what the WAV holds, ``lag`` samples late, on a clock that starts now."""

    def __init__(self, command, path, rate, *, env=None, wav=None, lag=40):
        self.path = Path(path)
        self.rate = rate
        self._wav = wav
        self._lag = lag
        self.started_at = time.monotonic()
        self.deleted = False
        self.stopped = False

    def start(self, keep_head_s=0.0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._wav), "rb") as read:
            pcm = read.readframes(read.getnframes())
        self.path.write_bytes(b"\0\0" * self._lag + pcm)
        self._head = (b"\0\0" * self._lag + pcm)[: int(keep_head_s * self.rate) * 2]

    def head(self):
        return self._head

    def samples(self):
        return len(self._head) // 2

    def time_of(self, index):
        return self.started_at + index / self.rate

    def stop(self):
        self.stopped = True

    def delete(self):
        self.deleted = True
        self.path.unlink(missing_ok=True)


def _tiny_fixture():
    return (
        fixture.Sentence(case="a", parts=("Line one.",), text="Line one.", seq=1, gap_ms=0.0,
                         turn_end=False),
        fixture.Sentence(case="b", parts=("Line two.",), text="Line two.", seq=2, family="energy",
                         expect_family="energy", gap_ms=50.0, engagement_at_s=(0.1,)),
        fixture.Sentence(case="silent", parts=(), text="Line three.", seq=3, audio=False, gap_ms=0.0,
                         turn_end=False, expected_exception="E6"),
    )


class QuietRelay:
    """A relay carrying a sentence's words. Sending anything back is a test failure."""

    def __init__(self):
        self.messages = [json.dumps({"type": "pokamind_ai_utterance", "seq": 1,
                                     "text": "the words of a sentence", "cue_family": "energy",
                                     "cue_category": "energy"}),
                         json.dumps({"type": "pokamind_analysis_update", "seq": 2})]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.05)
        raise TimeoutError

    def send(self, _message):
        raise AssertionError("the relay listener must never send")


def _scripted(real, monkeypatch, *, machine=None, **kwargs):
    monkeypatch.setattr(run.fixture, "NO_AUDIO_SLOT_S", 0.3)
    monkeypatch.setattr(run, "LEAD_IN_S", 0.1)
    monkeypatch.setattr(run, "TAIL_S", 0.1)
    monkeypatch.setattr(run, "SETTLE_QUIET_S", 0.01)
    machine = machine or FakeMachine(real.root, hostapp_env={"HOSTAPP_PORT": "8090"})
    rendered = run.render(_tiny_fixture(), "en", real.root / "artifacts" / "motion-sync" / "audio",
                          _synth, voice_id="test")
    cfg = run.Config(profile="loaded", lang="en", motion_source="ros", run_id="r1", max_s=120.0,
                     confirm_clear=True)
    daemon = lambda: FakeHttp({  # noqa: E731
        "/api/daemon/status": (200, {"state": "running"}),
        "/api/move/running": (200, [{"uuid": "abcdef0123456789"}]),
        "/api/state/full": (200, {"motion": {"active": False}})})
    joints = []

    def joints_factory(cfg_, machine_, path, latest, max_s):
        probe_ = FakeJoints(path)
        joints.append(probe_)
        return probe_

    captures = []

    def capture_factory(command, path, rate, env=None):
        captures.append(real.driver.tracker.active)
        return FakeCapture(command, path, rate, wav=rendered.wav)

    code = run.run_scripted(cfg, machine, hostapp_factory=lambda: real.http,
                            daemon_factory=daemon, hostapp_pid=None,
                            hostapp_env=machine.hostapp_env, relay_url="ws://x/ws/relay",
                            rendered=rendered, joints_factory=joints_factory,
                            run_kwargs={"relay_connect": lambda url: QuietRelay(),
                                        "capture_factory": capture_factory}, **kwargs)
    return SimpleNamespace(code=code, machine=machine, joints=joints, rendered=rendered,
                           captures=captures,
                           run_dir=real.root / "artifacts" / "motion-sync" / "r1")


def _assert_nothing_left(real, outcome):
    """The state a run must leave, however it ended."""

    assert real.driver.tracker.active is False
    assert real.ended == []
    assert not (real.root / "artifacts" / "last_results.json").exists()
    assert not (real.root / "artifacts" / "outbox").exists()
    assert list((real.root / "artifacts" / "recordings").glob("motion-sync*")) == []
    assert list((real.root / "artifacts" / "motion-trace").glob("motion-sync*")) == []
    for table in ("sessions", "cycles", "events"):
        assert _rows(_db(real), table) == []
    assert _rows(_db(real), "persons") == []
    assert not list(outcome.run_dir.glob("*.raw"))
    commands = outcome.machine.commands
    assert ["sudo", "-n", "systemctl", "start", run.KIOSK_UNIT] in commands
    assert ["sudo", "-n", "systemctl", "stop", f"{run.DEADMAN_UNIT}.timer"] in commands
    assert ["sudo", "-n", "systemctl", "stop", f"{run.LOAD_UNIT}.service"] in commands
    assert outcome.machine.kiosk["ActiveState"] == "active"
    assert all(j.stopped for j in outcome.joints)
    assert not list((real.root / "artifacts" / "motion-sync").glob("check-*"))


def test_a_whole_run_records_everything_and_leaves_nothing(real, monkeypatch):
    outcome = _scripted(real, monkeypatch)

    assert outcome.code == 0
    _assert_nothing_left(real, outcome)

    manifest = json.loads((outcome.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "r1" and manifest["motion_source"] == "ros"
    assert isinstance(manifest["t_wav0"], float) and manifest["k_ms"] is None
    assert [s["seq"] for s in manifest["sentences"]] == [1, 2, 3]
    assert manifest["sentences"][2]["audio"] is False

    truth = json.loads((outcome.run_dir / "truth.json").read_text(encoding="utf-8"))
    assert [row["seq"] for row in truth["sentences"]] == [1, 2]  # no audio, no truth
    for row, sentence in zip(truth["sentences"], manifest["sentences"]):
        # The capture is 40 samples late at 8 kHz, on its own clock: 5 ms, to the sample.
        offset = row["t_voice"] - sentence["wav_start_s"]
        assert row["t_end"] - row["t_voice"] == pytest.approx(
            sentence["wav_end_s"] - sentence["wav_start_s"], abs=0.002)
        assert offset == pytest.approx(manifest["t_wav0"], abs=0.002)

    trace = (outcome.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["s"] for line in trace]
    assert kinds[0] == "meta" and "sentence" in kinds and "tts_ended" in kinds
    sentences = [json.loads(line) for line in trace if json.loads(line)["s"] == "sentence"]
    assert {s["seq"] for s in sentences} == {1, 2, 3}
    assert all(s["slug"] != "head_reserved" for s in sentences)

    published = (outcome.run_dir / "published.jsonl").read_text(encoding="utf-8")
    assert json.loads(published.splitlines()[0])["cue_family"] == "energy"
    assert "words" not in published
    moves = (outcome.run_dir / "moves.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(moves[0])["running"] == 1
    assert (outcome.run_dir / "joints.jsonl").exists()
    # The closing session's own id is on the ledger too, for a later --cleanup.
    ledger = json.loads((outcome.run_dir / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger["session_ids"]) == 2

    # Nothing in the run directory holds the signed login.
    for item in outcome.run_dir.iterdir():
        text = item.read_text(encoding="utf-8", errors="replace")
        assert "sso" not in text and "eyJ" not in text


def test_a_run_uses_the_routes_a_page_uses_and_no_others(real, monkeypatch):
    posted = []

    class Spy(AppHttp):
        def post(self, path, payload=None):
            posted.append((path, (payload or {}).get("type")))
            return super().post(path, payload)

    real.http = Spy(real.client)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 0
    assert {path for path, _ in posted} == {"/api/start", "/api/frame-alive", "/api/cycle",
                                            "/api/session-start-failed"}
    kinds = {kind for path, kind in posted if path == "/api/cycle"}
    assert kinds == {run.UTTERANCE, run.ANALYSIS, run.TTS_ENDED}


def test_the_capture_starts_after_the_ceremony_so_it_still_holds_the_pilot(real, monkeypatch):
    # It keeps only its first seconds for finding the pilot, and a greeting can outlast them.
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 0 and outcome.captures == [True]


def test_a_monitor_that_delivers_nothing_aborts_before_any_audio(real, monkeypatch):
    monkeypatch.setattr(run, "CAPTURE_START_S", 0.05)
    monkeypatch.setattr(FakeCapture, "samples", lambda self: 0)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 1
    assert not [argv for argv in outcome.machine.spawned if argv[0] == "pw-play"]
    _assert_nothing_left(real, outcome)


def test_calibration_is_copied_in_when_present(real, monkeypatch):
    target = real.root / run.CALIBRATION_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"k_ms": 41.5, "note": "upper bound"}), encoding="utf-8")
    outcome = _scripted(real, monkeypatch)
    manifest = json.loads((outcome.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["k_ms"] == 41.5
    copied = json.loads((outcome.run_dir / "calibration.json").read_text(encoding="utf-8"))
    assert copied["k_ms"] == 41.5


def test_the_kiosk_is_stopped_after_the_timer_that_brings_it_back(real, monkeypatch):
    outcome = _scripted(real, monkeypatch)
    commands = outcome.machine.commands
    timer = next(i for i, c in enumerate(commands) if f"--unit={run.DEADMAN_UNIT}" in c)
    stop = commands.index(["sudo", "-n", "systemctl", "stop", run.KIOSK_UNIT])
    assert timer < stop
    assert f"start {run.KIOSK_UNIT}" in commands[timer][-1]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_mid_run_still_restores_everything(real, monkeypatch, signum):
    def interrupted(self):
        signal.raise_signal(signum)
        time.sleep(0.3)                # a real wait for the handler to run
        raise AssertionError("the handler should have raised")

    monkeypatch.setattr(run.ScriptedRun, "_watch", interrupted)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 1
    _assert_nothing_left(real, outcome)
    # A stopped run keeps what it recorded so far, and has no truth to offer.
    assert (outcome.run_dir / "trace.jsonl").exists()
    assert not (outcome.run_dir / "truth.json").exists()


def test_a_failure_mid_run_still_restores_everything(real, monkeypatch):
    def fails(self):
        raise run.RunAborted("something broke")

    monkeypatch.setattr(run.ScriptedRun, "_watch", fails)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 1
    _assert_nothing_left(real, outcome)


def test_a_restore_step_that_fails_is_reported_and_the_rest_still_run(real, monkeypatch):
    def fails(*_a, **_k):
        raise OSError("disk trouble")

    monkeypatch.setattr(run, "purge_files", fails)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 1
    assert ["sudo", "-n", "systemctl", "start", run.KIOSK_UNIT] in outcome.machine.commands
    assert real.driver.tracker.active is False
    assert all(j.stopped for j in outcome.joints)


def test_a_session_ending_underneath_the_run_stops_it_cleanly(real, monkeypatch):
    original = run.ScriptedRun._watch

    def ends_first(self):
        real.driver.abort_unstarted_session("x")      # refuses, the session has exchanged
        with real.driver._lock:
            real.driver._finalize_locked(results=None, end_reason="test")
        original(self)

    monkeypatch.setattr(run.ScriptedRun, "_watch", ends_first)
    outcome = _scripted(real, monkeypatch)
    assert outcome.code == 1
    _assert_nothing_left(real, outcome)


def test_a_whole_run_over_real_sockets(real, monkeypatch):
    """The harness's own HTTP client and relay listener against the app as it is served."""

    import uvicorn

    pytest.importorskip("websockets")
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(real.app, host="127.0.0.1", port=port, ws="wsproto",
                                           log_level="warning", access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        monkeypatch.setattr(run.fixture, "NO_AUDIO_SLOT_S", 0.3)
        monkeypatch.setattr(run, "LEAD_IN_S", 0.1)
        monkeypatch.setattr(run, "TAIL_S", 0.1)
        monkeypatch.setattr(run, "SETTLE_QUIET_S", 0.01)
        machine = FakeMachine(real.root)
        rendered = run.render(_tiny_fixture(), "en", real.root / "artifacts" / "audio", _synth,
                              voice_id="test")
        base = f"http://127.0.0.1:{port}"
        cfg = run.Config(run_id="r1", max_s=60.0, confirm_clear=True, hostapp_url=base)
        daemon = lambda: FakeHttp({  # noqa: E731
            "/api/daemon/status": (200, {"state": "running"}),
            "/api/move/running": (200, []), "/api/state/full": (200, {})})
        code = run.run_scripted(
            cfg, machine, hostapp_factory=lambda: run.Http(base), daemon_factory=daemon,
            hostapp_pid=None, hostapp_env={}, relay_url=f"ws://127.0.0.1:{port}/ws/relay",
            rendered=rendered,
            joints_factory=lambda c, m, path, latest, max_s: FakeJoints(path),
            run_kwargs={"capture_factory": lambda command, path, rate, env=None: FakeCapture(
                command, path, rate, wav=rendered.wav)})
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
    assert code == 0
    run_dir = real.root / "artifacts" / "motion-sync" / "r1"
    published = [json.loads(line) for line in
                 (run_dir / "published.jsonl").read_text(encoding="utf-8").splitlines()]
    # What the hub fanned out, stamped with the family the driver performed.
    assert [row["seq"] for row in published] == [1, 2, 3]
    assert published[1]["cue_family"] == "energy"
    assert set(published[0]) == {"t", "seq", "cue_family", "cue_category"}
    assert real.driver.tracker.active is False
    assert real.ended == []
    assert _rows(_db(real), "sessions") == []


def test_the_harness_client_reports_a_transport_failure_as_status_zero():
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    client = run.Http(f"http://127.0.0.1:{port}", timeout=0.5)
    assert client.get("/api/status") == (0, None)
    assert client.post("/api/cycle", {"type": "x"}) == (0, None)
    client.close()


def test_the_keepalive_rate_holds_the_real_watchdog_quiet(tmp_path):
    """The rule, with the clocks scaled down: cycles at this rate never reach the walk-away."""

    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    scale = 0.05
    driver = _driver(tmp_path)
    driver.start_session(SessionInfo(session_id="s", person_id="p", lang="en"))
    ticker = WatchdogTicker(driver, tick_s=0.01, walkaway_s=45.0 * scale, end_grace_s=0.05,
                            absence_s=20.0 * scale)
    ticker.start()
    try:
        for seq in range(4):
            driver.on_analysis(driver.stamp(run.analysis_envelope(seq)))
            driver.on_tts_ended(driver.stamp(run.tts_ended_envelope()))
            time.sleep(run.KEEPALIVE_S * scale)
        assert driver.tracker.active is True
        assert driver.counters.get("walkaway_ends", 0) == 0
        assert driver.counters.get("presence_ends", 0) == 0
        assert driver.counters.get("watchdog_force_ends", 0) == 0
        # And with the cycles stopped, the same watchdog does end it: the rate is what held it.
        deadline = time.monotonic() + 8.0
        while driver.tracker.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert driver.tracker.active is False
        assert driver.counters.get("presence_ends", 0) == 1
    finally:
        ticker.stop()
        ticker.join(timeout_s=1.0)


def test_a_second_signal_cannot_interrupt_the_restore():
    calls = []
    with pytest.raises(run.Interrupted):
        with run.signals_deferred():
            signal.raise_signal(signal.SIGTERM)
            time.sleep(0.05)
            calls.append("still restoring")
    assert calls == ["still restoring"]


def test_the_whole_run_has_a_bound_that_signals_itself(monkeypatch):
    sent = []
    monkeypatch.setattr(run.os, "kill", lambda pid, signum: sent.append(signum))
    with run.Guard(0.01):
        deadline = time.monotonic() + 2.0
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
    assert sent == [signal.SIGTERM]


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no hang-up on this system")
def test_a_lost_login_stops_a_run_like_any_other_signal():
    assert signal.SIGHUP in run.STOP_SIGNALS


def test_a_closed_terminal_cannot_stop_the_restore(monkeypatch):
    def closed(*_args, **_kwargs):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", closed)
    run._say("still here")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="the cleanup runs as a POSIX user")
def test_the_timer_cleans_up_as_this_user_and_starts_the_kiosk_whatever_happens(tmp_path):
    argv = run.deadman_command(tmp_path, 300.0, "r1")
    script = argv[-1]
    assert argv[:3] == ["sudo", "-n", "systemd-run"] and "--on-active=300" in argv
    assert f"'#{os.getuid()}'" in script and "--cleanup r1" in script
    assert script.endswith(f"; exec /bin/systemctl start {run.KIOSK_UNIT}")
    assert f"timeout {run.CLEANUP_BOUND_S}" in script


def test_a_timer_with_no_run_only_starts_the_kiosk(tmp_path):
    script = run.deadman_command(tmp_path, 300.0, None)[-1]
    assert script == f"exec /bin/systemctl start {run.KIOSK_UNIT}"


def test_rearming_replaces_the_timer_and_names_the_next_run(tmp_path, monkeypatch):
    armed = []
    monkeypatch.setattr(run, "deadman_command", lambda _root, after, run_id: (
        armed.append((after, run_id)) or ["arm"]))
    machine = FakeMachine(tmp_path)
    kiosk = run.Kiosk(machine, 500.0)
    kiosk.stop("r-1")
    kiosk.rearm("r-2")
    assert armed == [(500.0, "r-1"), (500.0, "r-2")]
    assert ["sudo", "-n", "systemctl", "stop", f"{run.DEADMAN_UNIT}.timer"] in machine.commands


def test_a_time_bound_is_set_on_every_scripted_run(real, monkeypatch):
    bounds = []

    class Recording(run.Guard):
        def __init__(self, bound_s):
            bounds.append(bound_s)
            super().__init__(bound_s)

    outcome = _scripted(real, monkeypatch, guard_factory=Recording)
    assert outcome.code == 0 and bounds == [120.0]
    timer = next(c for c in outcome.machine.commands if f"--unit={run.DEADMAN_UNIT}" in c)
    assert f"--on-active={int(120.0 + run.RESTORE_MARGIN_S)}" in timer


# --------------------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------------------


def _refused(machine, status=None, daemon=None, cfg=None):
    hostapp = FakeHttp({"/api/status": (200, status if status is not None else healthy_status())})
    cfg = cfg or run.Config(confirm_clear=True)
    found = run.scripted_refusals(cfg, machine, hostapp, daemon or healthy_daemon(),
                                  machine.hostapp_env)
    return {refusal.name for refusal in found}


def test_a_healthy_robot_is_not_refused(tmp_path):
    assert _refused(FakeMachine(tmp_path)) == set()


def test_refuses_while_a_session_is_active(tmp_path):
    status = healthy_status(session={"session_id": "a-learner"})
    assert "session_active" in _refused(FakeMachine(tmp_path), status)


def test_a_run_needs_the_operator_to_say_the_head_is_clear(tmp_path):
    machine = FakeMachine(tmp_path)
    assert _refused(machine, cfg=run.Config()) == {"not_confirmed_clear"}
    assert _refused(machine, cfg=run.Config(confirm_clear=True)) == set()


def test_the_presence_time_neither_clears_nor_refuses_a_run(tmp_path):
    # An idle robot's camera never updates it: it says nothing about who is near the head.
    machine = FakeMachine(tmp_path)
    assert _refused(machine, healthy_status(last_present_s=1.0)) == set()
    assert _refused(machine, healthy_status(last_present_s=9e4), cfg=run.Config()) \
        == {"not_confirmed_clear"}


def test_confirming_the_head_is_clear_overrides_nothing_else(tmp_path):
    status = healthy_status(session={"session_id": "a-learner"})
    assert "session_active" in _refused(FakeMachine(tmp_path), status)


@pytest.mark.parametrize("field, value, name", [
    ("free", 500 * 1024 * 1024, "disk_low"),
    ("kiosk", {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"}, "kiosk_state"),
    ("kiosk", {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"},
     "kiosk_state"),
    ("tools", False, "tools_missing"),
])
def test_refuses_on_the_machine_state(tmp_path, field, value, name):
    assert name in _refused(FakeMachine(tmp_path, **{field: value}))


def test_confirming_does_not_override_the_disk_or_the_kiosk(tmp_path):
    machine = FakeMachine(tmp_path, free=1, kiosk={"LoadState": "loaded", "ActiveState": "failed",
                                                   "SubState": "failed"})
    assert {"disk_low", "kiosk_state"} <= _refused(machine)


def test_a_bridge_nobody_can_read_counts_as_a_refusal(tmp_path):
    class Denied(FakeMachine):
        def container_env(self, container):
            return None

    assert "fiware_unknown" in _refused(Denied(tmp_path, hostapp_env={"EMORI_FIWARE": "0"}))


def test_a_container_docker_will_not_describe_reads_as_unknown(tmp_path):
    class Refusing(FakeMachine):
        def run(self, argv, *, timeout=20.0):
            return 1, ""

    assert run.Machine.container_env(Refusing(tmp_path), "emori-ros2-v2") is None


@pytest.mark.parametrize("daemon_reply", [(0, None), (200, {"state": "error"}), (404, None),
                                          (200, {"state": "running", "backend_status": {
                                              "motor_control_mode": "disabled"}})])
def test_refuses_when_the_robot_is_not_running(tmp_path, daemon_reply):
    daemon = FakeHttp({"/api/daemon/status": daemon_reply})
    assert "robot_down" in _refused(FakeMachine(tmp_path), daemon=daemon)


def test_refuses_when_the_hostapp_says_no_robot_or_does_not_answer(tmp_path):
    assert "robot_down" in _refused(FakeMachine(tmp_path), healthy_status(robot=False))
    hostapp = FakeHttp({"/api/status": (0, None)})
    found = run.scripted_refusals(run.Config(), FakeMachine(tmp_path), hostapp, healthy_daemon(), {})
    assert "hostapp_down" in {r.name for r in found}


def test_refuses_with_no_trace_to_record(tmp_path):
    status = healthy_status(motion_trace={"enabled": False})
    assert "trace_off" in _refused(FakeMachine(tmp_path), status)


def test_refuses_when_a_session_would_write_fiware(tmp_path):
    answers = FakeMachine(tmp_path, broker=True)
    assert "fiware_write" in _refused(answers)
    off = FakeMachine(tmp_path, broker=True, hostapp_env={"EMORI_FIWARE": "0"})
    assert "fiware_write" not in _refused(off)
    bridge = FakeMachine(tmp_path, container={"FIWARE_BRIDGE_ENABLED": "1"},
                         hostapp_env={"EMORI_FIWARE": "0"})
    assert "fiware_write" in _refused(bridge)
    unreadable = FakeMachine(tmp_path)
    unreadable.hostapp_env = None
    found = run.scripted_refusals(run.Config(), unreadable, FakeHttp(healthy_routes()),
                                  healthy_daemon(), None)
    assert "fiware_unknown" in {r.name for r in found}


def test_refuses_when_the_hostapp_gives_up_faster_than_the_keepalive(tmp_path):
    machine = FakeMachine(tmp_path, hostapp_env={"EMORI_WALKAWAY_S": "12"})
    assert "timer_short" in _refused(machine)
    assert "timer_short" not in _refused(FakeMachine(tmp_path, hostapp_env={"EMORI_WALKAWAY_S": "45"}))
    assert "timer_short" not in _refused(FakeMachine(tmp_path, hostapp_env={"EMORI_ABSENCE_S": "0"}))


def test_refuses_while_an_addon_mode_holds_the_head(tmp_path):
    routes = healthy_routes()
    routes["/api/addon/status"] = (200, {"mode": "busy"})
    found = run.scripted_refusals(run.Config(confirm_clear=True), FakeMachine(tmp_path),
                                  FakeHttp(routes), healthy_daemon(), {})
    assert "addon_busy" in {r.name for r in found}
    routes["/api/addon/status"] = (200, {"mode": "idle"})
    found = run.scripted_refusals(run.Config(confirm_clear=True), FakeMachine(tmp_path),
                                  FakeHttp(routes), healthy_daemon(), {})
    assert not found
    routes["/api/addon/status"] = (503, {"error_code": "addon_unavailable"})
    found = run.scripted_refusals(run.Config(confirm_clear=True), FakeMachine(tmp_path),
                                  FakeHttp(routes), healthy_daemon(), {})
    assert not found


def _no_factories_touched(*_a, **_k):
    raise AssertionError("a refused run must not get this far")


def test_a_refused_run_touches_nothing(tmp_path, capsys):
    machine = FakeMachine(tmp_path, free=1)
    hostapp = FakeHttp(healthy_routes())
    code = run.run_scripted(run.Config(), machine, hostapp_factory=lambda: hostapp,
                            daemon_factory=healthy_daemon, hostapp_pid=None, hostapp_env={},
                            relay_url="", run_factory=_no_factories_touched,
                            joints_factory=_no_factories_touched)
    assert code == 2
    assert machine.mutations() == [] and machine.spawned == []
    assert [entry for entry in hostapp.log if entry[0] != "GET"] == []
    assert "disk_low" in capsys.readouterr().out
    assert not (tmp_path / "artifacts").exists()


def test_silent_joint_states_refuse_before_the_kiosk_is_touched(tmp_path, capsys):
    machine = FakeMachine(tmp_path)
    rendered = run.Rendered(tmp_path / "x.wav", 8000, _lay(_sentence("a", gap_ms=0.0)))
    code = run.run_scripted(
        run.Config(profile="quiet", run_id="r1", confirm_clear=True), machine,
        hostapp_factory=lambda: FakeHttp(healthy_routes()), daemon_factory=healthy_daemon,
        hostapp_pid=None, hostapp_env={}, relay_url="", rendered=rendered,
        run_factory=_no_factories_touched,
        joints_factory=lambda cfg, m, path, latest, max_s: FakeJoints(path, silent=True))
    assert code == 2
    assert ["sudo", "-n", "systemctl", "stop", run.KIOSK_UNIT] not in machine.commands
    assert "joints_silent" in capsys.readouterr().out
    assert not list((tmp_path / "artifacts" / "motion-sync").glob("check-*"))


def test_a_dry_run_says_what_it_would_do_and_touches_nothing(tmp_path, capsys):
    machine = FakeMachine(tmp_path)
    hostapp = FakeHttp(healthy_routes())
    daemon = healthy_daemon()
    assert run.run_dry(run.Config(confirm_clear=True), machine, hostapp, daemon, {}) == 0
    out = capsys.readouterr().out
    assert "would stop " + run.KIOSK_UNIT in out and "nothing would refuse" in out
    assert machine.mutations() == [] and machine.spawned == []
    assert [entry for entry in hostapp.log + daemon.log if entry[0] != "GET"] == []
    assert not (tmp_path / "artifacts").exists()


def test_a_dry_run_reports_a_refusal_without_acting_on_it(tmp_path, capsys):
    machine = FakeMachine(tmp_path, free=1)
    assert run.run_dry(run.Config(), machine, FakeHttp(healthy_routes()), healthy_daemon(), {}) == 0
    assert "disk_low" in capsys.readouterr().out


def test_arguments_default_the_motion_source_to_the_mode():
    assert run.parse_args([]).motion_source == "ros"
    assert run.parse_args(["--observe"]).motion_source == "http"
    assert run.parse_args(["--observe", "--motion-source", "ros"]).motion_source == "ros"
    with pytest.raises(SystemExit):
        run.parse_args(["--run-id", "../escape"])
    with pytest.raises(SystemExit):
        run.parse_args(["--runs", "0"])


def test_the_hostapp_address_comes_from_its_own_environment():
    assert run.resolve_hostapp_url(run.Config(), {"HOSTAPP_PORT": "8090"}) == "http://127.0.0.1:8090"
    assert run.resolve_hostapp_url(run.Config(), {}).endswith(":" + run.HOSTAPP_FALLBACK_PORT)
    assert run.resolve_hostapp_url(run.Config(hostapp_url="http://x:1/"), {}) == "http://x:1"


class _OnAHub(FakeMachine):
    """The hostapp's unit is stopped, and the hub's main process is its compose client."""

    worker = "501"

    def unit_state(self, unit):
        return {"MainPID": {run.HOSTAPP_UNIT: "0", run.HUB_UNIT: "500"}.get(unit, "0")}

    def process_env(self, pid):
        return {"HOSTAPP_PORT": "8090"} if pid == "500" else None

    def container_worker(self, container):
        return self.worker if container == run.HUB_CONTAINER else None


def test_on_a_hub_the_environment_is_the_units_and_the_process_is_the_containers(tmp_path):
    pid, env = run.hostapp_environ(_OnAHub(tmp_path))
    assert (pid, env) == ("501", {"HOSTAPP_PORT": "8090"})
    assert run.resolve_hostapp_url(run.Config(), env) == "http://127.0.0.1:8090"


def test_a_hub_whose_container_cannot_be_read_keeps_its_environment(tmp_path):
    machine = _OnAHub(tmp_path)
    machine.worker = None
    assert run.hostapp_environ(machine) == ("500", {"HOSTAPP_PORT": "8090"})


def test_with_neither_program_running_there_is_no_environment(tmp_path):
    class Neither(FakeMachine):
        def unit_state(self, unit):
            return {"MainPID": "0"}

    assert run.hostapp_environ(Neither(tmp_path)) == (None, None)


# --------------------------------------------------------------------------------------
# --observe
# --------------------------------------------------------------------------------------


def test_observe_changes_nothing_and_copies_the_trace(tmp_path, capsys):
    real_id = "anna-001-20261102-101500-feedfacefeedface"
    trace_dir = tmp_path / "artifacts" / "motion-trace"
    trace_dir.mkdir(parents=True)
    (trace_dir / f"{real_id}.jsonl").write_text('{"s":"meta"}\n', encoding="utf-8")

    log = []
    statuses = [healthy_status(session={"session_id": real_id, "lang": "ca"})] * 3
    statuses.append(healthy_status())

    class Hostapp(FakeHttp):
        def get(self, path):
            log.append(("GET", path))
            return (200, statuses.pop(0) if len(statuses) > 1 else statuses[-1])

        def post(self, path, payload=None):
            raise AssertionError("observing must post nothing")

    machine = FakeMachine(tmp_path)
    moves_log = []
    daemon = lambda: FakeHttp({"/api/daemon/status": (200, {"state": "running"}),  # noqa: E731
                               "/api/move/running": (200, [])}, moves_log)
    relay = QuietRelay()
    code = run.run_observe(
        run.Config(observe=True, motion_source="http", run_id="obs", max_s=30.0), machine,
        hostapp=Hostapp(), daemon_factory=daemon, hostapp_pid=None, hostapp_env={}, relay_url="",
        joints_factory=lambda cfg, m, path, latest, max_s: FakeJoints(path),
        relay_connect=lambda url: relay)

    assert code == 0
    assert machine.mutations() == [] and machine.spawned == []
    assert {entry[1].split("?")[0] for entry in moves_log}         <= {"/api/daemon/status", "/api/move/running", "/api/move/trace"}
    assert {entry[0] for entry in moves_log} == {"GET"}
    run_dir = tmp_path / "artifacts" / "motion-sync" / "obs"
    assert (run_dir / "trace.jsonl").read_text(encoding="utf-8") == '{"s":"meta"}\n'
    assert (trace_dir / f"{real_id}.jsonl").exists()                # the original stays
    assert not (run_dir / "truth.json").exists()                    # real sessions have none
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "observe" and manifest["t_wav0"] is None
    assert manifest["sentences"] == [] and manifest["lang"] == "ca"
    # A session id can hold a student id: it goes in no file and no line of output.
    assert real_id not in capsys.readouterr().out
    for item in run_dir.iterdir():
        assert real_id not in item.read_text(encoding="utf-8")
        assert "the words of a sentence" not in item.read_text(encoding="utf-8")


def test_observe_refuses_on_a_robot_that_is_down_and_waits_for_a_session(tmp_path):
    machine = FakeMachine(tmp_path)
    daemon = lambda: FakeHttp({"/api/daemon/status": (200, {"state": "error"})})  # noqa: E731
    code = run.run_observe(run.Config(observe=True), machine, hostapp=FakeHttp(healthy_routes()),
                           daemon_factory=daemon, hostapp_pid=None, hostapp_env={}, relay_url="")
    assert code == 2

    idle = FakeHttp(healthy_routes())
    code = run.run_observe(run.Config(observe=True, wait_s=0.0), machine, hostapp=idle,
                           daemon_factory=healthy_daemon, hostapp_pid=None, hostapp_env={},
                           relay_url="")
    assert code == 2
    assert machine.mutations() == []


# --------------------------------------------------------------------------------------
# --cleanup, after a run was killed
# --------------------------------------------------------------------------------------


def test_cleanup_finishes_what_a_killed_run_left(real):
    sid = run.mint_session(real.http, "r1")
    run_dir = real.root / run.RUNS_DIR / "r1"
    run.write_ledger(run_dir, "r1", [sid])
    run.open_session(real.http, sid)
    run.post_cycle(real.http, run.analysis_envelope(1))

    machine = FakeMachine(real.root)
    code = run.run_cleanup(run.Config(cleanup="r1"), machine, hostapp=real.http, hostapp_env={})

    assert code == 0
    assert real.driver.tracker.active is False
    assert _rows(_db(real), "sessions") == []
    assert list((real.root / "artifacts" / "recordings").glob("motion-sync*")) == []
    assert (run_dir / "trace.jsonl").exists()


def test_cleanup_takes_the_verb_a_walk_away_left_for_the_next_page(real):
    sid = run.mint_session(real.http, "r1")
    run_dir = real.root / run.RUNS_DIR / "r1"
    run.write_ledger(run_dir, "r1", [sid])
    run.open_session(real.http, sid)
    run.post_cycle(real.http, run.analysis_envelope(1))
    real.driver.enqueue_cmd("end")

    code = run.run_cleanup(run.Config(cleanup="r1"), FakeMachine(real.root), hostapp=real.http,
                           hostapp_env={})

    assert code == 0
    assert real.driver.pop_cmd() is None


def test_cleanup_leaves_the_verb_of_a_session_it_does_not_own(real):
    sid = run.mint_session(real.http, "r1")
    run_dir = real.root / run.RUNS_DIR / "r1"
    run.write_ledger(run_dir, "r1", [sid])
    other = run.mint_session(real.http, "someone-else")
    run.open_session(real.http, other)
    real.driver.enqueue_cmd("end")

    code = run.run_cleanup(run.Config(cleanup="r1"), FakeMachine(real.root), hostapp=real.http,
                           hostapp_env={})

    assert code == 1
    assert real.driver.pop_cmd() == "end"


def test_a_verb_the_hostapp_will_not_hand_over_is_reported():
    with pytest.raises(run.RunAborted):
        run.drain_stale_verbs(FakeHttp({"/api/pending-cmd?verbs=end,begin": (0, None)}),
                              wait=lambda _s: None)


def test_cleanup_refuses_without_a_ledger_or_with_a_bad_id(tmp_path):
    machine = FakeMachine(tmp_path)
    for run_id in ("r1", "../r1"):
        code = run.run_cleanup(run.Config(cleanup=run_id), machine,
                               hostapp=FakeHttp(healthy_routes()), hostapp_env={})
        assert code == 2


# --------------------------------------------------------------------------------------
# the load stand-in
# --------------------------------------------------------------------------------------


def test_the_load_worker_is_bounded_and_runs_where_chromium_runs():
    argv = run.load_worker_command(500.0)
    assert "--slice=system.slice" in argv
    assert f"CPUQuota={run.LOAD_QUOTA}" in argv
    assert "RuntimeMaxSec=500" in argv
    assert f"--unit={run.LOAD_UNIT}" in argv


def test_the_camera_is_not_touched(real, monkeypatch):
    outcome = _scripted(real, monkeypatch)
    # Only the unit: a temporary folder named after this test holds the word as well.
    assert not [c for c in outcome.machine.commands
                if any("emori-camera" in str(part) for part in c)]


# --------------------------------------------------------------------------------------
# the motion sources
# --------------------------------------------------------------------------------------


def _conversions():
    import importlib.util

    path = _REPO / "ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/conversions.py"
    spec = importlib.util.spec_from_file_location("motion_sync_test_conversions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("rpy", [(0.0, 0.0, 0.0), (0.1, -0.2, 0.3), (-0.4, 0.25, -0.5),
                                 (0.6, 0.0, 0.0), (0.0, -0.6, 0.0), (0.0, 0.0, 0.7)])
def test_the_probes_angles_are_the_ones_the_state_route_reports(rpy):
    conversions = _conversions()
    roll, pitch, yaw = rpy
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch),
                              math.cos(yaw), math.sin(yaw))
    matrix = np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                       [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                       [-sp, cp * sr, cp * cr]])
    x, y, z, w = conversions.matrix_to_quaternion(matrix)
    assert probe.rpy_from_quaternion(x, y, z, w) == pytest.approx(
        list(conversions.matrix_to_rotation_rpy(matrix)), abs=1e-9)
    assert probe.rpy_from_quaternion(x, y, z, w) == pytest.approx(list(rpy), abs=1e-9)


def test_the_probe_reads_joints_by_name():
    names = ["body_yaw", "stewart_1", "right_antenna", "left_antenna"]
    assert probe.joints_from_message(names, [0.5, 9.0, 0.1, 0.2]) == ([0.2, 0.1], 0.5)
    assert probe.joints_from_message(["stewart_1"], [1.0]) is None


def test_the_merger_prints_one_record_per_tick_and_survives_a_silent_topic():
    merger = probe.Merger()
    assert merger.on_pose(1.0, [0.1, 0.2, 0.3]) is None
    record = merger.on_joints(1.001, [0.4, 0.5], 0.6)
    assert record == {"t": 1.001, "rpy": [0.1, 0.2, 0.3], "ant": [0.4, 0.5], "yaw": 0.6}
    assert merger.on_pose(1.02, [0.1, 0.2, 0.4]) is None
    assert merger.on_joints(1.021, [0.4, 0.5], 0.6)["rpy"] == [0.1, 0.2, 0.4]
    # The joint topic goes quiet: the pose keeps printing, with the last joints it saw.
    assert merger.on_pose(1.04, [0.0, 0.0, 0.1]) is None
    silent = merger.on_pose(1.06, [0.0, 0.0, 0.2])
    assert silent is not None and silent["ant"] == [0.4, 0.5]


class FakeRclpy:
    """Enough of rclpy to deliver a few messages, then let the probe's stop flag end it."""

    def __init__(self, stop, messages):
        self.stop = stop
        self.messages = messages
        self.subscriptions = {}
        self.node_kwargs = None
        self.shut_down = False

    def init(self, args=None):
        self.init_args = args

    def create_node(self, name, **kwargs):
        self.node_name = name
        self.node_kwargs = kwargs
        return self

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions[topic] = (msg_type, callback, qos)

    def spin_once(self, node, timeout_sec=None):
        if self.messages:
            topic, message = self.messages.pop(0)
            self.subscriptions[topic][1](message)
        else:
            self.stop.set()

    def destroy_node(self):
        pass

    def shutdown(self):
        self.shut_down = True


def _pose_message():
    return SimpleNamespace(pose=SimpleNamespace(orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0,
                                                                            w=1.0)))


def _joint_message():
    return SimpleNamespace(name=["body_yaw", "right_antenna", "left_antenna"],
                           position=[0.3, 0.1, 0.2])


def test_the_probe_subscribes_quietly_and_stamps_at_reception(capsys):
    stop = threading.Event()
    rclpy = FakeRclpy(stop, [("/reachy_mini/head_pose", _pose_message()),
                             ("/joint_states", _joint_message()),
                             ("/reachy_mini/motion_status", SimpleNamespace(
                                 busy=True, queue_depth=2, running_move_id="abc"))])
    ticks = iter(range(100, 200))
    probe.run(rclpy=rclpy, qos="best-effort", pose_type="P", joint_type="J", status_type="S",
              max_s=50.0, clock=lambda: float(next(ticks)), stop=stop)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["rpy"] == [0.0, 0.0, 0.0] and lines[0]["ant"] == [0.2, 0.1]
    assert lines[0]["yaw"] == 0.3
    assert lines[1]["status"]["queue_depth"] == 2
    assert set(rclpy.subscriptions) == {"/joint_states", "/reachy_mini/head_pose",
                                        "/reachy_mini/motion_status"}
    assert all(sub[2] == "best-effort" for sub in rclpy.subscriptions.values())
    assert rclpy.node_kwargs == {"enable_rosout": False, "start_parameter_services": False}
    assert rclpy.shut_down is True


def test_the_topic_names_are_the_ones_the_node_publishes():
    source = (_REPO / "ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/robot_node.py").read_text(
        encoding="utf-8")
    for topic in (probe.JOINT_TOPIC, probe.POSE_TOPIC, probe.STATUS_TOPIC):
        assert f'"{topic}"' in source
    names = (_REPO / "ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/backend_host.py").read_text(
        encoding="utf-8")
    for name in (probe.JOINT_BODY_YAW, probe.JOINT_LEFT_ANTENNA, probe.JOINT_RIGHT_ANTENNA):
        assert f'"{name}"' in names


def test_the_probe_command_starts_no_ros2_daemon_and_logs_to_tmp():
    argv = run.ros_probe_command("emori-ros2-v2", 400.0)
    assert argv[:3] == ["docker", "exec", "-i"]
    assert any(part.startswith("ROS_LOG_DIR=/tmp/") for part in argv)
    assert not any(part == "ros2" or part.startswith("ros2 ") or " ros2 " in part for part in argv)
    assert "emori-ros2-v2" in argv and probe.MARKER in argv and "400" in argv


def test_the_probe_stops_when_the_host_closes_its_pipe_and_removes_its_log_dir(tmp_path):
    source = (_REPO / "scripts/motion_sync/joints_probe.py").read_text(encoding="utf-8")
    assert "ROS_LOG_DIR" in source and "shutil.rmtree(log_dir" in source
    # stdin closing is the stop signal.
    stop = threading.Event()

    class Closed:
        class buffer:
            @staticmethod
            def read(_n):
                return b""

    original = sys.stdin
    sys.stdin = Closed()
    try:
        probe._watch_stdin(stop)
    finally:
        sys.stdin = original
    assert stop.is_set()


def test_the_probe_really_boots_the_way_the_container_starts_it(tmp_path):
    """The exact bootstrap in a real interpreter, with stand-ins for the ROS packages."""

    packages = tmp_path / "packages"
    log_note = tmp_path / "log-dir.txt"
    for name in ("rclpy", "geometry_msgs", "sensor_msgs"):
        (packages / name).mkdir(parents=True)
    (packages / "rclpy" / "__init__.py").write_text(
        "import os, time, types\n"
        "def init(args=None):\n"
        f"    open({str(log_note)!r}, 'w').write(os.environ['ROS_LOG_DIR'])\n"
        "class Node:\n"
        "    def __init__(self): self.subs = {}\n"
        "    def create_subscription(self, t, topic, cb, qos): self.subs[topic] = cb\n"
        "    def destroy_node(self): pass\n"
        "def create_node(name, **kw): return Node()\n"
        "def spin_once(node, timeout_sec=None):\n"
        "    q = types.SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)\n"
        "    node.subs['/reachy_mini/head_pose'](types.SimpleNamespace(\n"
        "        pose=types.SimpleNamespace(orientation=q)))\n"
        "    node.subs['/joint_states'](types.SimpleNamespace(\n"
        "        name=['body_yaw', 'right_antenna', 'left_antenna'], position=[0.3, 0.1, 0.2]))\n"
        "    time.sleep(0.02)\n"
        "def shutdown(): pass\n", encoding="utf-8")
    (packages / "rclpy" / "qos.py").write_text("qos_profile_sensor_data = 1\n", encoding="utf-8")
    for name, message in (("geometry_msgs", "PoseStamped"), ("sensor_msgs", "JointState")):
        (packages / name / "__init__.py").write_text("", encoding="utf-8")
        (packages / name / "msg.py").write_text(f"class {message}: pass\n", encoding="utf-8")

    argv = run.ros_probe_command("ctr", 60.0)
    boot = argv[argv.index("-c") + 2]
    env = dict(os.environ, PYTHONPATH=str(packages))
    proc = subprocess.Popen([sys.executable, "-u", "-c", boot, probe.MARKER, "--max-s", "60"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    try:
        source = (_REPO / "scripts/motion_sync/joints_probe.py").read_bytes()
        proc.stdin.write(b"%d\n" % len(source) + source)
        proc.stdin.flush()
        record = json.loads(proc.stdout.readline())
        assert set(record) == {"t", "rpy", "ant", "yaw"}
        assert record["ant"] == [0.2, 0.1] and record["yaw"] == 0.3
        assert record["t"] == pytest.approx(time.monotonic(), abs=30.0)
        proc.stdin.close()
        assert proc.wait(timeout=15.0) == 0          # closing its stdin is what ends it
    finally:
        if proc.poll() is None:
            proc.kill()
    used = Path(log_note.read_text(encoding="utf-8"))
    assert probe.MARKER in used.name
    assert not used.exists()                         # its temporary log directory is gone


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                    reason="needs a POSIX bash")
def test_the_container_shell_line_parses():
    argv = run.ros_probe_command("ctr", 60.0)
    shell = argv[argv.index("-c") + 1]
    assert subprocess.run(["bash", "-n", "-c", shell], capture_output=True).returncode == 0


def test_the_ros_source_saves_motion_routes_status_and_stops_the_probe(tmp_path):
    class Pipe:
        def __init__(self, lines):
            self.lines = lines
            self.written = b""
            self.closed = False

        def __iter__(self):
            return iter(self.lines)

        def write(self, data):
            self.written += data

        def flush(self):
            pass

        def close(self):
            self.closed = True

    now = time.monotonic()
    lines = [json.dumps({"t": now, "rpy": [0, 0, 0], "ant": [0, 0], "yaw": 0}).encode() + b"\n",
             json.dumps({"t": now, "status": {"queue_depth": 3}}).encode() + b"\n",
             b"not json\n"]
    proc = SimpleNamespace(stdin=Pipe([]), stdout=Pipe(lines), wait=lambda timeout=None: 0,
                           terminate=lambda: None, kill=lambda: None)
    machine = FakeMachine(tmp_path)
    machine.spawn = lambda argv, **kwargs: (machine.spawned.append(argv) or proc)
    latest = run.LatestMotion()
    joints = run.RosJoints(machine, "ctr", tmp_path / "joints.jsonl", latest, max_s=100.0)
    joints.start()
    joints.wait_first(2.0)
    joints.stop()
    assert proc.stdin.closed is True
    saved = (tmp_path / "joints.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(saved) == 1 and json.loads(saved[0])["rpy"] == [0, 0, 0]
    assert latest.snapshot() == {"queue_depth": 3}
    # The header line says how many bytes of source follow, then the probe's own source.
    head, source = proc.stdin.written.split(b"\n", 1)
    assert int(head) == len(source) and b"def rpy_from_quaternion" in source


def test_a_probe_on_another_clock_is_refused(tmp_path):
    joints = run.RosJoints(FakeMachine(tmp_path), "ctr", tmp_path / "j.jsonl", run.LatestMotion(),
                           max_s=10.0)
    joints.first_t, joints.first_seen_at = 5.0, 5000.0
    joints._first.set()
    with pytest.raises(run.RunAborted, match="clock"):
        joints.wait_first(0.1)


def test_the_http_source_maps_the_wire_and_dates_a_sample_by_its_read():
    state = {"head_pose": {"roll": 0.1, "pitch": 0.2, "yaw": 0.3}, "body_yaw": 0.4,
             "antennas_position": [0.5, 0.6], "timestamp": "2026-01-01T00:00:00.000000Z"}
    from datetime import datetime, timezone

    read_at = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    record, stamp = run.joints_from_state(state, monotonic_now=1000.0, wall_now=read_at + 0.03)
    assert record["rpy"] == [0.1, 0.2, 0.3]
    assert record["ant"] == [0.6, 0.5]                # the wire says [right, left]
    assert record["yaw"] == 0.4
    assert record["t"] == pytest.approx(999.97)       # when it was read, not when it arrived
    assert stamp == state["timestamp"]
    # A clock that makes no sense falls back to the arrival.
    record, _ = run.joints_from_state(state, monotonic_now=1000.0, wall_now=read_at + 3600.0)
    assert record["t"] == 1000.0


def test_the_http_source_skips_a_cached_reading(tmp_path):
    state = {"head_pose": {"roll": 0, "pitch": 0, "yaw": 0}, "body_yaw": 0,
             "antennas_position": [0, 0], "timestamp": "2026-01-01T00:00:00Z",
             "motion": {"active": True, "lane": "interaction", "head_moving": True, "client": "c"}}
    latest = run.LatestMotion()
    source = run.HttpJoints(FakeHttp({"/api/state/full": (200, state)}), tmp_path / "j.jsonl", latest)
    source._out = run.JsonlFile(tmp_path / "j.jsonl")
    source.tick()
    source.tick()
    source._out.close()
    assert len((tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert source.samples == 1
    assert latest.snapshot()["lane"] == "interaction"


def test_a_relay_message_keeps_only_the_agreed_fields():
    record = run.published_record(json.dumps({
        "type": "pokamind_ai_utterance", "seq": 4, "text": "the words", "person_id": "someone",
        "cue_family": "levity", "cue_category": "levity", "cue_body": "direction"}), 5.0)
    assert record == {"t": 5.0, "seq": 4, "cue_family": "levity", "cue_category": "levity"}
    assert run.published_record(json.dumps({"type": "pokamind_analysis_update"}), 5.0) is None
    assert run.published_record("not json", 5.0) is None


def test_the_load_sampler_reads_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "975").mkdir()
    (proc / "976").mkdir()
    (proc / "loadavg").write_text("5.25 5.0 4.9 1/2 3\n", encoding="utf-8")
    stat = "42 (a b) S " + " ".join(["0"] * 10) + " {u} {s} 0 0"
    (proc / "42" / "stat").write_text(stat.format(u=100, s=50), encoding="utf-8")
    (proc / "975" / "cmdline").write_bytes(b"/usr/bin/pipewire\0")
    (proc / "975" / "stat").write_text(stat.format(u=10, s=0), encoding="utf-8")
    (proc / "976" / "cmdline").write_bytes(b"/usr/bin/pipewire\0-c\0filter-chain.conf\0")
    assert run.find_pipewire(proc) == "975"

    times = iter([10.0, 12.0])
    sampler = run.LoadSampler(tmp_path / "load.jsonl", 42, proc_root=proc,
                              clock=lambda: next(times))
    sampler._ticks = 100
    sampler._out = run.JsonlFile(tmp_path / "load.jsonl")
    sampler.tick()
    (proc / "42" / "stat").write_text(stat.format(u=200, s=50), encoding="utf-8")
    sampler.tick()
    sampler._out.close()
    records = [json.loads(line)
               for line in (tmp_path / "load.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[1]["load1"] == 5.25
    assert records[1]["hostapp_cpu"] == pytest.approx(50.0)
    assert set(records[1]) == {"t", "load1", "hostapp_cpu", "pipewire_cpu"}


# --------------------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------------------


def test_a_sample_time_is_the_tightest_stamp_nearby():
    # 100 samples per block at 1 kHz: each block's last sample exists 100 ms after the last.
    stamps = [(100 * n, 10.0 + 0.1 * (n - 1) + (0.02 if n % 2 else 0.05)) for n in range(1, 50)]
    # Sample 99 was there by 10.02 s, the earliest arrival for its place in a block.
    assert audio.time_of_sample(stamps, 99, 1000) == pytest.approx(10.02, abs=1e-6)
    assert audio.time_of_sample(stamps, 500, 1000) == pytest.approx(10.02 + 0.401, abs=1e-6)


def test_a_sample_time_follows_a_drifting_clock():
    rate = 1000
    stamps = [(100 * n, 0.1 * n * 1.001) for n in range(1, 6000)]
    assert audio.time_of_sample(stamps, 500000, rate) == pytest.approx(500.5, abs=0.2)


def test_best_offset_finds_a_copy_to_the_sample_and_says_how_strongly():
    rng = np.random.default_rng(1)
    needle = rng.uniform(-1, 1, 400)
    hay = rng.uniform(-0.02, 0.02, 5000)
    hay[1234: 1634] += needle * 0.5
    at, strength = audio.best_offset(hay, needle)
    assert at == 1234 and strength > 0.9
    _, weak = audio.best_offset(rng.uniform(-1, 1, 5000), needle)
    assert weak < 0.3


def test_the_pilot_is_found_in_the_capture_and_gives_t_wav0():
    rate = 8000
    pilot = run.make_pilot(rate)
    lag = 1000
    track = np.zeros(lag + round(run.PILOT_START_S * rate) + pilot.size + 2000)
    track[lag + round(run.PILOT_START_S * rate):][: pilot.size] = pilot
    pcm = (track * 32767).astype("<i2").tobytes()
    capture = SimpleNamespace(head=lambda: pcm, time_of=lambda index: 500.0 + index / rate)
    zero, t_wav0 = run.find_pilot(capture, rate, sleep=lambda s: None)
    assert zero == lag
    assert t_wav0 == pytest.approx(500.0 + lag / rate)


def test_no_pilot_aborts_the_run():
    capture = SimpleNamespace(head=lambda: b"\0\0" * 8000, time_of=lambda index: 0.0)
    with pytest.raises(run.RunAborted, match="pilot"):
        run.find_pilot(capture, 8000, timeout_s=0.02, sleep=lambda s: None)


def test_the_track_holds_each_sentence_at_its_sample_and_the_cache_is_reused(tmp_path):
    sentences = _tiny_fixture()
    calls = []

    def factory():
        calls.append(1)
        return _synth()

    rendered = run.render(sentences, "en", tmp_path, factory, voice_id="v")
    again = run.render(sentences, "en", tmp_path, factory, voice_id="v")
    assert calls == [1]
    assert [p.start for p in again.placements] == [p.start for p in rendered.placements]
    samples = rendered.samples()
    first = rendered.placements[0]
    assert abs(samples[first.start]) >= run.SILENCE_THRESHOLD        # the first audible sample
    assert np.max(np.abs(samples[first.start - 400: first.start])) < 0.01
    assert rendered.wav.parent == tmp_path

    # A changed fixture renders afresh and the old file goes.
    changed = (*sentences, fixture.Sentence(case="z", parts=("More.",), text="More.", seq=9))
    run.render(changed, "en", tmp_path, factory, voice_id="v")
    assert calls == [1, 1]
    assert len(list(tmp_path.glob("en-*.wav"))) == 1


def test_a_part_starts_on_its_first_audible_sample():
    trimmed = run.trim_silence(np.array([0.0, 0.001, 0.5, 0.0, 0.4, 0.001, 0.0]))
    assert trimmed.tolist() == [0.5, 0.0, 0.4]
    assert run.trim_silence(np.zeros(5)).size == 0


def test_truth_comes_from_the_capture_and_a_lost_sentence_is_left_out(tmp_path):
    sentences = (*_tiny_fixture()[:2],)
    rendered = run.render(sentences, "en", tmp_path, _synth, voice_id="v")
    with wave.open(str(rendered.wav), "rb") as read:
        pcm = np.frombuffer(read.readframes(read.getnframes()), dtype="<i2").copy()
    lag = 123
    second = rendered.placements[1]
    pcm[second.start:] = 0                                # the second sentence never played
    raw = tmp_path / "capture.raw"
    (np.concatenate([np.zeros(lag, dtype="<i2"), pcm]).astype("<i2")).tofile(raw)
    capture = SimpleNamespace(path=raw, time_of=lambda index: 1000.0 + index / rendered.rate)
    truth = run.compute_truth(capture, rendered, lag)
    assert [row["seq"] for row in truth["sentences"]] == [1]
    row = truth["sentences"][0]
    first = rendered.placements[0]
    assert row["t_voice"] == pytest.approx(1000.0 + (lag + first.start) / rendered.rate, abs=1e-3)
    assert row["t_end"] == pytest.approx(1000.0 + (lag + first.end) / rendered.rate, abs=1e-3)


def test_a_capture_stamps_blocks_as_they_arrive_and_deletes_its_file(tmp_path):
    class Stream:
        def __init__(self):
            self.blocks = [b"\1\0" * 10, b"\2\0" * 6, b""]

        def read(self, _n):
            return self.blocks.pop(0)

    proc = SimpleNamespace(stdout=Stream(), terminate=lambda: None, wait=lambda timeout=None: 0,
                           kill=lambda: None)
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append((argv, kwargs))
        return proc

    ticks = iter([1.0, 2.0])
    capture = audio.StampedCapture(["pw-record"], tmp_path / "c.raw", 1000, spawn=spawn,
                                   clock=lambda: next(ticks))
    capture.start(keep_head_s=0.008)
    capture._thread.join(2.0)
    capture.stop()
    assert capture.stamps == [(10, 1.0), (16, 2.0)]
    assert capture.samples() == 16
    assert capture.head()[:2] == b"\1\0"
    assert spawned[0][1]["bufsize"] == 0                  # stamped on arrival, not on a full buffer
    assert (tmp_path / "c.raw").stat().st_size == 32
    capture.delete()
    assert not (tmp_path / "c.raw").exists()


def test_calibration_measures_the_mic_against_the_monitor_and_calls_it_an_upper_bound(tmp_path):
    rate = 8000
    track, starts = audio.make_chirp_track(rate, count=4)
    monitor = track.astype("float64") / 32768.0
    rng = np.random.default_rng(2)
    delay = int(0.05 * rate)                     # the sound reaches the microphone 50 ms later
    mic = np.concatenate([np.zeros(delay), monitor * 0.1]) + rng.uniform(-0.002, 0.002,
                                                                         monitor.size + delay)
    # The two captures started 200 ms apart, and sit on one clock.
    monitor_time = lambda i: 7.0 + i / rate  # noqa: E731
    mic_time = lambda i: 7.2 + i / rate  # noqa: E731
    shifted = mic[int(0.2 * rate):]
    offsets = audio.monitor_to_mic_ms(monitor, shifted, monitor_time, mic_time, rate, count=4)
    assert len(offsets) == 4
    assert audio.median(offsets) == pytest.approx(50.0, abs=1.0)

    record = audio.calibration_record(offsets)
    assert set(record) == {"k_ms", "note"}
    assert record["k_ms"] == pytest.approx(50.0, abs=1.0)
    assert "upper bound" in record["note"]


def test_calibration_with_nothing_heard_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        audio.calibration_record([])
    rate = 8000
    track, _ = audio.make_chirp_track(rate, count=2)
    monitor = track.astype("float64") / 32768.0
    silent = np.zeros(monitor.size)
    assert audio.monitor_to_mic_ms(monitor, silent, lambda i: i / rate, lambda i: i / rate,
                                   rate, count=2) == []


def test_calibrate_plays_records_writes_and_deletes_its_audio(tmp_path):
    rate = 8000
    track, _ = audio.make_chirp_track(rate, count=2)
    pcm = track.tobytes()

    class Stream:
        def __init__(self, data):
            self.data = data

        def read(self, n):
            block, self.data = self.data[:n], self.data[n:]
            return block

    played = []

    def spawn(argv, **kwargs):
        if argv[0] == "pw-play":
            played.append(argv)
            return FakeProc()
        return SimpleNamespace(stdout=Stream(pcm), terminate=lambda: None,
                               wait=lambda timeout=None: 0, kill=lambda: None,
                               poll=lambda: None)

    out = tmp_path / "out" / "calibration.json"
    work = tmp_path / "work"
    record = audio.calibrate(out, work, spawn=spawn, rate=rate, count=2)
    assert played and "k_ms" in record
    assert json.loads(out.read_text(encoding="utf-8"))["note"] == record["note"]
    assert list(work.iterdir()) == []


def test_calibrate_runs_under_the_same_refusals_and_restores_the_kiosk(tmp_path):
    machine = FakeMachine(tmp_path, free=1)
    code = run.run_calibrate(run.Config(), machine, hostapp=FakeHttp(healthy_routes()),
                             daemon=healthy_daemon())
    assert code == 2 and machine.mutations() == []

    machine = FakeMachine(tmp_path)

    def fails(*_a, **_k):
        raise ValueError("nothing heard")

    code = run.run_calibrate(run.Config(), machine, hostapp=FakeHttp(healthy_routes()),
                             daemon=healthy_daemon(), calibrate=fails)
    assert code == 1
    assert ["sudo", "-n", "systemctl", "stop", run.KIOSK_UNIT] in machine.commands
    assert ["sudo", "-n", "systemctl", "start", run.KIOSK_UNIT] in machine.commands
    assert machine.kiosk["ActiveState"] == "active"


def test_the_covering_line_tells_the_hostapp_when_it_starts_and_ends():
    cover = _sentence("cover", parts=("One line.",), posted=False, cover=True, turn_end=False)
    posts = run.build_schedule(_lay(cover), 1000)
    kinds = [p.kind for p in posts if p.kind in (run.COVER_ON, run.COVER_OFF)]
    assert kinds == [run.COVER_ON, run.COVER_OFF]


def test_the_track_goes_where_the_kiosk_voice_goes():
    assert run.play_command(Path("a.wav")) == ["pw-play", "a.wav"]
    assert run.play_command(Path("a.wav"), "emori_voice") \
        == ["pw-play", "--target", "emori_voice", "a.wav"]


def test_the_sink_follows_what_the_hostapp_was_told():
    assert run.voice_sink({}) == ""
    assert run.voice_sink({"EMORI_VOICE_DELAY_MS": "0"}) == ""
    assert run.voice_sink({"EMORI_VOICE_DELAY_MS": "150"}) == "emori_voice"
    assert run.voice_sink({"EMORI_VOICE_LINK_SINK": "other"}) == "other"


def test_events_go_by_the_audio_ahead_of_the_delay():
    placed = _lay(_sentence("a", gap_ms=0.0))
    manifest = run.build_manifest(run_id="r", commit="c", profile="quiet", lang="en",
                                  motion_source="ros", k_ms=1.0, t_wav0=100.0, placements=placed,
                                  rate=1000, voice_delay_s=0.12)
    assert manifest["voice_delay_ms"] == 120.0
    assert manifest["sentences"][0]["t_event_planned"] == pytest.approx(
        100.0 - 0.12 + placed[0].event_at_s)
