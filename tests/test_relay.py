"""Tests for the hostapp hub: /api/cycle fan-out, /ws/relay producer, dedup, status.

These exercise the LIVE spine — the exact path a real kiosk session takes — with an
injected DryRun robot client and no network/daemon/FIWARE.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import server
from emori_adapter.hostapp.driver import UtteranceDeduper
from emori_adapter.robot.client import DryRunRobotClient

from tests.test_live_contract import live_envelope, null_body_envelope


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setattr(server, "DEMO", True)
    robot = DryRunRobotClient()
    app = server._build_app(overrides={
        "robot_client": robot,
        "fiware_factory": None,
        "idle": False,
        "tts": None,
        "stt": None,
        "recorder": type("NullRec", (), {"write": staticmethod(lambda *a, **k: None)})(),
        "on_session_end": None,  # never write the real artifacts/outbox from tests
    })
    with TestClient(app) as c:
        c.robot = robot
        yield c


def test_cycle_analysis_drives_robot(app_client):
    r = app_client.post("/api/cycle", json=live_envelope())
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["level"]  # an EngagementLevel name


def test_null_body_cycle_does_not_500(app_client):
    """The reproduced live crash must be gone at the endpoint level too."""
    r = app_client.post("/api/cycle", json=null_body_envelope())
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_utterance_dedup_via_endpoint(app_client):
    env = {"type": "pokamind_ai_utterance", "text": "I understand this is difficult.",
           "seq": 1, "timestamp": "2026-06-11T10:30:24.000Z"}
    first = app_client.post("/api/cycle", json=env).json()
    # a transport-level double delivery carries the SAME seq+text -> dropped
    second = app_client.post("/api/cycle", json=dict(env)).json()
    # a legitimately repeated identical line arrives with a NEW seq -> spoken again
    third = app_client.post("/api/cycle", json=dict(env, seq=2)).json()
    assert first["spoke"] is True
    assert second["spoke"] is False and second["deduped"] is True
    assert third["spoke"] is True


def test_relay_has_a_producer_now(app_client):
    """THE architectural fix: /api/cycle must broadcast to /ws/relay clients."""
    with app_client.websocket_connect("/ws/relay") as ws:
        app_client.post("/api/cycle", json=live_envelope(seq=9))
        received = json.loads(ws.receive_text())
    assert received["type"] == "pokamind_analysis_update"
    assert received["seq"] == 9
    assert "session_id" in received  # identity was stamped before fan-out


def test_completed_returns_summary_and_resets(app_client):
    # open a demo session so a tracker session is active
    app_client.get("/session?demo=1&sid=nonexistent")
    for i in range(3):
        app_client.post("/api/cycle", json=live_envelope(seq=i))
    done = app_client.post("/api/cycle", json={
        "type": "pokamind_session_completed", "status": "completed",
        "session_id": "x", "partner": "demo", "results": {"k": 1}}).json()
    assert done["completed"] is True
    assert done["summary"]
    assert done["next_url"] == "/"
    assert "/api/tts" in done["summary_tts_url"]
    st = app_client.get("/api/status").json()
    assert st["session"] is None  # reset for the next learner


def test_status_counters(app_client):
    app_client.post("/api/cycle", json=live_envelope())
    app_client.post("/api/cycle", json={"type": "pokamind_ai_utterance", "text": "hi", "seq": 0})
    st = app_client.get("/api/status").json()
    assert st["counters"]["cycles"] >= 1
    assert st["counters"]["utterances"] >= 1
    assert "relay_clients" in st


def test_non_pokamind_payloads_ignored(app_client):
    r = app_client.post("/api/cycle", json={"type": "evil_injection", "x": 1})
    assert r.json() == {"ok": True, "ignored": "evil_injection"}


def test_session_page_activates_pending_mint(app_client):
    d = app_client.post("/api/start", json={"student_id": "anna-001"}).json()
    assert "sid=" in d["session_url"]
    app_client.get(d["session_url"])
    st = app_client.get("/api/status").json()
    assert st["session"] is not None
    assert st["session"]["person_id"] == "anna-001"
    assert st["session"]["session_id"].startswith("anna-001-")


def test_deduper_window_expires():
    d = UtteranceDeduper(window_s=5.0)
    assert d.is_duplicate("Hello there", seq=7, now=100.0) is False
    assert d.is_duplicate("  hello   THERE ", seq=7, now=103.0) is True  # normalized dup, same seq
    assert d.is_duplicate("Hello there", seq=8, now=103.5) is False  # new seq = legit repeat
    assert d.is_duplicate("Hello there", seq=7, now=106.0) is False  # window expired


def test_tts_stt_unavailable_503(app_client):
    assert app_client.get("/api/tts?text=hi&lang=en").status_code == 503
    assert app_client.get("/api/stt/health").json() == {"available": False}
    assert app_client.post("/api/stt?lang=en", content=b"\x00" * 8000).status_code == 503


def test_a_cue_with_no_words_is_not_a_duplicate_but_its_double_delivery_is():
    d = UtteranceDeduper(window_s=1.5)
    assert d.is_duplicate("", seq=4, now=100.0, cue="levity") is False
    assert d.is_duplicate("", seq=4, now=100.5, cue="levity") is True
    assert d.is_duplicate("", seq=5, now=100.6, cue="energy") is False
    assert d.is_duplicate("", seq=6, now=100.7) is True         # neither words nor a cue
