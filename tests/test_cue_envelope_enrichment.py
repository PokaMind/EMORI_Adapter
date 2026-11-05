"""The relayed utterance envelope says which family the robot PERFORMS (ros4hri).

``/api/cycle`` must not fan an ai_utterance out to the relay (ROS 2 publisher,
recorder) BEFORE the driver plans it: the ROS SpeechCue would only ever carry what
Pokamind forwarded -- and when Pokamind forwarded nothing (which can last many turns
in a row) the graph showed a robot that stood still while it was in fact reading
the sentence and performing tension. Now the driver plans first and stamps the
family it performs (cue_family / cue_family_source / cue_slug, and cue_category when
the wire had none), and the relay publishes THAT envelope.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from emori_adapter.hostapp import server


class _LegacyPlayer:
    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []

    def submit(self, intents, *, drop_if_busy: bool = False) -> bool:
        self.batches.append(list(intents))
        return True


def _driver():
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    return LiveDriver(DryRunRobotClient(), player=_LegacyPlayer(), fiware_factory=None)


def _utt(text, **extra):
    payload = {"type": "pokamind_ai_utterance", "text": text, "seq": 1,
               "source": "turn", "lang": "en"}
    payload.update(extra)
    return payload


def test_a_text_read_family_is_stamped_onto_the_envelope():
    driver = _driver()
    stamped = driver.stamp(_utt("I am truly so very frightened."))
    driver.on_utterance(stamped)
    assert stamped["cue_family"] == "tension"
    assert stamped["cue_family_source"] == "text"
    assert stamped["cue_category"] == "tension"       # the wire had none: now it says
    assert stamped["cue_slug"].startswith("tension/")
    assert stamped["cue_budget_s"] > 0


def test_pokamind_s_own_family_is_left_untouched():
    driver = _driver()
    stamped = driver.stamp(_utt("I am frightened.", cue_category="levity"))
    driver.on_utterance(stamped)
    assert stamped["cue_category"] == "levity"
    assert stamped["cue_family"] == "levity"
    assert stamped["cue_family_source"] == "pokamind"


def test_a_direction_that_names_another_family_is_what_goes_out():
    driver = _driver()
    stamped = driver.stamp(_utt("I hear you.", cue_category="levity",
                                cue_body="in a nervous, uneasy voice"))
    driver.on_utterance(stamped)
    assert stamped["cue_family"] == stamped["cue_category"] == "tension"
    assert stamped["cue_category_authored"] == "levity"


def test_when_ros_owns_the_motion_the_family_stays_for_the_node_that_performs_it(monkeypatch):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    driver = _driver()
    stamped = driver.stamp(_utt("I am frightened.", cue_category="levity"))
    driver.on_utterance(stamped)
    assert stamped["cue_category"] == "levity"


def test_a_sentence_on_a_reserved_head_publishes_no_family():
    driver = _driver()
    driver.cue._head_reserved = lambda: True
    stamped = driver.stamp(_utt("I am frightened.", cue_category="levity"))
    driver.on_utterance(stamped)
    assert stamped["cue_category"] == ""
    assert stamped["cue_category_authored"] == "levity"


def test_a_plain_sentence_stamps_no_family_but_still_its_slug():
    driver = _driver()
    stamped = driver.stamp(_utt("Tell me more about what happened next, if you would."))
    driver.on_utterance(stamped)
    assert stamped["cue_family"] == ""
    assert stamped["cue_family_source"] == ""
    assert "cue_category" not in stamped
    assert stamped["cue_slug"].startswith("talking/")


def test_the_relay_receives_the_enriched_envelope(monkeypatch):
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
    app = server._build_app(overrides={"watchdog": False, "idle": False,
                                       "player": _LegacyPlayer(), "voice": None})
    with TestClient(app) as c:
        with c.websocket_connect("/ws/relay") as ws:
            r = c.post("/api/cycle", json=_utt("I am so very sorry for bothering you."))
            assert r.status_code == 200 and r.json().get("ok") is True
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "pokamind_ai_utterance"
            assert msg["cue_family"] == "softening"
            assert msg["cue_family_source"] == "text"
            assert msg["cue_category"] == "softening"
            assert "cue_slug" in msg


def test_the_page_can_say_its_own_line_is_on_the_speaker(monkeypatch):
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
    held = []

    class Player(_LegacyPlayer):
        def hold_voice(self, on):
            held.append(on)

    app = server._build_app(overrides={"watchdog": False, "idle": False, "player": Player(),
                                       "voice": None})
    with TestClient(app) as c:
        assert c.post("/api/voice-cover", json={"on": True}).json() == {"ok": True}
        assert c.post("/api/voice-cover", json={"on": False}).json() == {"ok": True}
    assert held == [True, False]
