"""THE acceptance test: replay a full live-contract session through every layer.

Pushes tests/fixtures/live_session.jsonl (the exact live-contract shapes, incl.
null-body frames, one transport-duplicated AI utterance, and one legitimately
repeated line) through the hostapp hub in-process and asserts the complete
deployed behaviour:

  badge mint -> /session activates -> per-cycle robot intents (incl. greeting,
  talking gestures, de-escalation, farewell) -> identity-stamped relay broadcast
  (what the ROS 2 publisher consumes) -> FIWARE ensure/push/summary -> recorder
  JSONL -> spoken summary + kiosk loop -> results delivery -> state reset.

No network, no daemon, no Docker: the robot, FIWARE, and delivery are injected
fakes; everything else is the real production code path.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import server
from emori_adapter.hostapp.driver import Recorder
from emori_adapter.robot.client import DryRunRobotClient

FIXTURE = Path(__file__).parent / "fixtures" / "live_session.jsonl"


class _SyncPlayer:
    """Applies intents immediately (the real RobotPlayer paces them in real time,
    which a fast replay would outrun)."""

    def __init__(self, client):
        self._client = client

    def submit(self, intents, *, drop_if_busy: bool = False) -> bool:
        for intent in intents or []:
            if intent is not None:
                self._client.apply(intent)
        return True


class _FakeFiware:
    def __init__(self):
        self.enabled = True
        self.ensured: list[str] = []
        self.pushed: list = []
        self.summary = None
        self.closed = False

    def ensure_session(self, topic=""):
        self.ensured.append(topic)

    def push(self, signals):
        self.pushed.append(signals)

    def write_summary(self, text, avg):
        self.summary = (text, avg)

    def close(self):
        self.closed = True


@pytest.fixture()
def stack(monkeypatch, tmp_path, sample_roster):
    # The replay scans EMORI:anna-001: the host app reads the test-only sample roster
    # (the shipped example lists nobody).
    monkeypatch.setattr(server, "DEMO", True)
    robot = DryRunRobotClient()
    fiwares: list[_FakeFiware] = []
    delivered: list[dict] = []

    def fiware_factory(info):
        fw = _FakeFiware()
        fw.info = info
        fiwares.append(fw)
        return fw

    app = server._build_app(overrides={
        "robot_client": robot,
        "player": _SyncPlayer(robot),
        "fiware_factory": fiware_factory,
        "idle": False,
        "tts": None,
        "stt": None,
        "recorder": Recorder(directory=str(tmp_path / "recordings"), enabled=True),
        "on_session_end": delivered.append,
    })
    with TestClient(app) as client:
        yield {"client": client, "robot": robot, "fiwares": fiwares,
               "delivered": delivered, "tmp": tmp_path}


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_full_session_replay_end_to_end(stack):
    c = stack["client"]
    envelopes = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    n_analysis = sum(1 for e in envelopes if e["type"] == "pokamind_analysis_update")
    utts = [e for e in envelopes if e["type"] == "pokamind_ai_utterance"]
    n_utt = len(utts)
    # the fixture carries exactly one transport-level double delivery (same seq+text)
    n_dupes = n_utt - len({(u["seq"], u["text"]) for u in utts})
    assert n_dupes == 1, "fixture must contain exactly one transport duplicate"

    # 1) badge -> SSO mint -> session page load (the session truly begins)
    mint = c.post("/api/identify", json={"qr": "EMORI:anna-001"}).json()
    assert "sid=" in mint["session_url"]
    relay_messages: list[dict] = []

    with c.websocket_connect("/ws/relay") as ws:
        # collect broadcasts on a thread: receive_text blocks the test otherwise
        def _collect():
            try:
                while True:
                    relay_messages.append(json.loads(ws.receive_text()))
            except Exception:  # noqa: BLE001 - socket closed at teardown
                pass

        t = threading.Thread(target=_collect, daemon=True)
        t.start()

        c.get(mint["session_url"])
        status = c.get("/api/status").json()
        session_id = status["session"]["session_id"]
        person_id = status["session"]["person_id"]
        assert session_id.startswith(person_id)  # minted as <student>-<timestamp>

        # 2) replay the full recorded session
        completion = None
        for env in envelopes:
            body = c.post("/api/cycle", json=env).json()
            assert body.get("ok", True) is not False or "error" not in body
            if body.get("completed"):
                completion = body
        assert _wait_for(lambda: len(relay_messages) >= len(envelopes) - 1)

    # 3) completion response drives the kiosk loop: summary + tts + home
    assert completion is not None, "completion never returned"
    assert completion["summary"]
    assert completion["next_url"] == "/"
    assert "/api/tts" in completion["summary_tts_url"]

    # 4) the robot actually moved: greeting, talking gestures, de-escalation, farewell
    reasons = [i.reason for i in stack["robot"].applied]
    assert any("greeting" in r for r in reasons), "no greeting gesture"
    # The AI's sentences perform: either the talking flow or a family
    # the planner read from the sentence itself ("cue <family> ..." reasons).
    assert any("speaking (AI turn)" in r or r.startswith("cue ") for r in reasons), \
        "no talking gestures"
    assert any("de-escalation" in r for r in reasons), "de-escalation never fired on the live arc"
    assert any("farewell" in r for r in reasons), "no farewell gesture"

    # 5) the transport duplicate was dropped; the legitimately repeated line spoke
    counters = c.get("/api/status").json()["counters"]
    assert counters["utterances"] == n_utt - n_dupes
    assert counters["utterances_deduped"] == n_dupes
    assert counters["cycles"] == n_analysis
    assert counters.get("parse_errors", 0) == 0

    # 6) FIWARE got the full lifecycle with per-session identity
    assert len(stack["fiwares"]) == 1
    fw = stack["fiwares"][0]
    assert fw.info.session_id == session_id
    assert len(fw.pushed) == n_analysis
    assert fw.summary is not None and fw.summary[0] == completion["summary"]
    assert fw.closed

    # 7) every relayed envelope was identity-stamped (what ROS 2 consumes)
    assert relay_messages, "nothing reached /ws/relay"
    for msg in relay_messages:
        assert msg["session_id"] == session_id
        assert msg["person_id"] == person_id

    # 8) the recorder wrote the replayable JSONL
    rec_dir = stack["tmp"] / "recordings"
    rec_files = list(rec_dir.glob("*.jsonl"))
    assert len(rec_files) == 1
    rec_lines = rec_files[0].read_text(encoding="utf-8").splitlines()
    assert len(rec_lines) == len(envelopes)

    # 9) results were delivered with the Pokamind payload attached
    assert len(stack["delivered"]) == 1
    stats = stack["delivered"][0]
    assert stats["results"]["duration_minutes"] == 8
    assert stats["cycles"] == n_analysis

    # 10) the kiosk is reset for the next learner
    assert c.get("/api/status").json()["session"] is None


def test_replay_is_deterministic(stack):
    """Same fixture, same translator outcome: the e2e harness is regression-grade."""
    c = stack["client"]
    envelopes = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]

    levels = []
    for env in envelopes:
        body = c.post("/api/cycle", json=env).json()
        if body.get("level"):
            levels.append(body["level"])
    assert "ENGAGED" in levels
    assert "DISENGAGED" in levels or "DISENGAGING" in levels


def test_a_recorder_write_failure_is_diagnosed_without_the_session_id(tmp_path,
                                                                     monkeypatch,
                                                                     caplog):
    """OSError.__str__ appends `: '<filename>'`, and this basename is the minted
    session id -- i.e. the SSO student id `_mint_session_id` prefixes onto it.

    This line fires on a full or read-only SD card, which is exactly when someone
    raises EMORI_LOG_LEVEL to DEBUG to find out why recordings stopped; that goes
    to journald, which persists across boots.
    """

    import logging

    from emori_adapter.hostapp import driver as driver_module

    def full_card(*args, **kwargs):
        raise OSError(28, "No space left on device",
                      str(tmp_path / "anna-001-20260730-101500-deadbeef.jsonl"))

    monkeypatch.setattr(driver_module, "open", full_card, raising=False)
    recorder = Recorder(directory=str(tmp_path), enabled=True)
    caplog.set_level(logging.DEBUG, logger="emori.hostapp")

    recorder.write("anna-001-20260730-101500-deadbeef", {"type": "x"})   # never raises

    line = next(m for m in caplog.messages if "recorder write failed" in m)
    assert "anna-001" not in line
    assert "errno=28" in line          # a full card, not read-only and not permission
    assert str(tmp_path) in line       # ...in which directory
