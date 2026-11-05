"""EMORI_LANG is a BOOT default; the live kiosk language is runtime state.

Everything the language reaches -- the verbal menu that is loaded, the page the kiosk
renders, the ``emori_lang`` claim Pokamind opens the session with, the voice the pages
ask ``/api/tts`` for -- must not be frozen at import. On a standalone robot that means
one language until someone SSHes in and restarts the unit, which is exactly what this
appliance no longer has. These tests pin that a switch takes effect for the NEXT page
load and the NEXT claim, in the same process.
"""

from __future__ import annotations

import json
import threading

from fastapi.testclient import TestClient

from emori_adapter.hostapp import pages, server
from emori_adapter.hostapp.extensions import HostExtension
from tests.test_robot_token_hostapp import sso_claims, use_golden_token


class _FakeExtension(HostExtension):
    """An add-on that is present and doing nothing, or running a mode, or holding a badge."""

    def __init__(self, busy: bool = False, reserved: bool = False) -> None:
        self._busy = busy
        self._reserved = reserved

    def busy(self) -> bool:
        return self._busy

    def selection_reserved(self) -> bool:
        return self._reserved


class _RecordingPlayer:
    busy = False

    def __init__(self) -> None:
        self.submitted: list[list] = []

    def submit(self, intents, *, drop_if_busy=False, guard=None) -> bool:
        del drop_if_busy
        if guard is not None and not guard():
            return False
        self.submitted.append(list(intents))
        return True

    def stop(self) -> None:
        pass

    def join(self, *, timeout_s: float) -> bool:
        del timeout_s
        return True


def _app(monkeypatch, tmp_path, *, lang: str = "ca", gesture: bool = False,
         extension=None, spoken=None, roster: str = "", lang_toggle: str = ""):
    from emori_adapter.robot.client import DryRunRobotClient

    # Never inherited from the developer's shell: the toggle flag decides whether a
    # single turn is held, which is the difference between two behaviours under test.
    if lang_toggle:
        monkeypatch.setenv("EMORI_LANG_TOGGLE", lang_toggle)
    else:
        monkeypatch.delenv("EMORI_LANG_TOGGLE", raising=False)
    monkeypatch.setattr(server, "EMORI_LANG", lang)
    use_golden_token(monkeypatch, server)
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "PARTNER", "emori-robot")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    # NEVER the default artifacts/ path: a test that persisted a language there would
    # re-language every later test in the run.
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    if roster:
        monkeypatch.setenv("EMORI_ROSTER", roster)
    overrides = {
        "robot_client": DryRunRobotClient(),
        "player": _RecordingPlayer(),
        "fiware_factory": None,
        "idle": False,
        "watchdog": False,
        "gesture": gesture,
        "extension": extension if extension is not None else _FakeExtension(),
        "tts": None,
        "stt": None,
    }
    if spoken is not None:
        overrides["speak"] = lambda text, code: spoken.append((text, code))
    return server._build_app(overrides=overrides)


def _prompt(html: str) -> str:
    return json.loads(html.split("const PROMPT=", 1)[1].split(", OPTIONS=", 1)[0])


def _options(html: str) -> list[dict]:
    return json.loads(html.split(", OPTIONS=", 1)[1].split(", demo=", 1)[0])


# --- the pages ----------------------------------------------------------------

def test_the_landing_page_follows_a_runtime_switch(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lang="ca")
    client = TestClient(app)

    catalan = client.get("/").text
    assert 'const LANG="ca"' in catalan
    assert "Mostra la teva acreditació" in catalan
    assert _prompt(catalan) == server._load_menu("ca").prompt
    assert len(_options(catalan)) == len(server._load_menu("ca").options)

    app.state.lang_state.set("en")
    english = client.get("/").text

    # No restart, no rebuild: the SAME app now serves the other language, the other
    # course's menu and the other set of number words.
    assert 'const LANG="en"' in english
    assert "Show your badge to begin" in english
    assert _prompt(english) == server._load_menu("en").prompt
    assert len(_options(english)) == len(server._load_menu("en").options)


def test_the_legacy_pages_still_render_after_a_switch(monkeypatch, tmp_path):
    """/identify and /manual carry no localized chrome, but they go through the same
    renderer -- a switch must not leave an unsubstituted placeholder in them."""

    app = _app(monkeypatch, tmp_path, lang="ca")
    client = TestClient(app)
    app.state.lang_state.set("en")
    for path in ("/identify", "/manual"):
        body = client.get(path)
        assert body.status_code == 200
        assert "__STRINGS__" not in body.text and "__LANG__" not in body.text


def test_the_voice_menu_endpoint_follows_the_switch(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lang="ca")
    client = TestClient(app)

    before = client.get("/api/voice-menu").json()
    app.state.lang_state.set("en")
    after = client.get("/api/voice-menu").json()

    assert before["prompt"] == server._load_menu("ca").prompt
    assert after["prompt"] == server._load_menu("en").prompt
    assert before["grammar"] != after["grammar"]      # Vosk hears the other numbers


# --- the SSO claim ------------------------------------------------------------

def _claim(client, badge: str = "") -> dict:
    body = client.post("/api/voice-select", json={"text": "1", "qr": badge}).json()
    return sso_claims(body["session_url"])


def test_the_sso_claim_follows_a_runtime_switch(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lang="ca")
    client = TestClient(app)

    assert _claim(client)["emori_lang"] == "ca"
    app.state.lang_state.set("en")
    assert _claim(client)["emori_lang"] == "en"


def test_a_roster_badge_never_overrides_the_runtime_language(monkeypatch, tmp_path):
    """The GLOBAL kiosk switch is the only session-language authority -- a per-badge
    roster ``lang`` (still parsed, see identity._roster_lang, for backward roster
    compatibility) must not reach the SSO claim even when it disagrees with the
    current runtime language, in EITHER direction.
    """

    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "course_id: emori-demo\n"
        "staff:\n"
        '  "EMORI:john-002": {student_id: john-002, email: j@example.org, name: John, lang: en}\n'
        '  "EMORI:marta-003": {student_id: marta-003, email: m@example.org, name: Marta, lang: ca}\n',
        encoding="utf-8",
    )
    app = _app(monkeypatch, tmp_path, lang="ca", roster=str(roster))
    client = TestClient(app)

    # Kiosk boots ca; john's roster pins "en" -- the OLD contract gave "en" here.
    john = client.post("/api/voice-select", json={"text": "1", "qr": "EMORI:john-002"}).json()
    assert sso_claims(john["session_url"])["emori_lang"] == "ca"
    # One badge at a time: john's session ends before marta's card is welcome.
    client.get(john["session_url"])
    with app.state.driver._lock:
        app.state.driver._finalize_locked(None)
    # Switch the kiosk at runtime; marta's roster pins "ca" -- the OLD contract would
    # give "ca" regardless of the switch. The claim must follow the switch instead.
    app.state.lang_state.set("en")
    assert _claim(client, "EMORI:marta-003")["emori_lang"] == "en"


def test_a_walkin_session_page_speaks_the_runtime_language(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lang="ca")
    client = TestClient(app)

    assert 'const LANG="ca"' in client.get("/session").text
    app.state.lang_state.set("en")
    assert 'const LANG="en"' in client.get("/session").text


# --- persistence across a restart ---------------------------------------------

def test_the_switch_survives_a_process_restart(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, lang="ca")
    app.state.lang_state.set("en")

    # A fresh process with the SAME deployment default reads the state file.
    restarted = _app(monkeypatch, tmp_path, lang="ca")

    assert restarted.state.lang_state.current() == "en"
    assert 'const LANG="en"' in TestClient(restarted).get("/").text


# --- the page-level reload verb -----------------------------------------------

def test_the_landing_page_polls_only_for_the_reload_verb(monkeypatch, tmp_path):
    assert f"/api/pending-cmd?verbs={server.PAGE_RELOAD_CMD}" in pages.VOICE_HTML
    assert f"d.cmd==='{server.PAGE_RELOAD_CMD}'" in pages.VOICE_HTML
    assert "location.reload()" in pages.VOICE_HTML


def test_a_filtered_poll_never_steals_a_session_verb(monkeypatch, tmp_path):
    """The single pending-cmd slot is shared. A landing tab left open must not
    drain the ``end`` a live /session page is waiting for."""

    app = _app(monkeypatch, tmp_path)
    client = TestClient(app)
    app.state.driver.enqueue_cmd("end")

    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None
    assert client.get("/api/pending-cmd").json()["cmd"] == "end"      # still there


def test_a_filtered_poll_delivers_its_own_verb(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    client = TestClient(app)
    app.state.driver.enqueue_cmd(server.PAGE_RELOAD_CMD)

    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] == "reload"
    assert client.get("/api/pending-cmd").json()["cmd"] is None       # drained once


# --- the gesture -> switch wiring ---------------------------------------------

def test_the_gesture_toggle_switches_speaks_and_reloads(monkeypatch, tmp_path):
    spoken: list[tuple[str, str]] = []
    app = _app(monkeypatch, tmp_path, lang="ca", gesture=True, spoken=spoken)
    client = TestClient(app)
    ticker = app.state.gesture_ticker
    assert callable(ticker.on_lang_toggle)

    ticker.on_lang_toggle()                       # the double turn completed

    assert app.state.lang_state.current() == "en"
    assert app.state.driver.default_lang == "en"
    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] == "reload"
    assert 'const LANG="en"' in client.get("/").text
    # Spoken in the language being switched TO -- hearing it IS the confirmation.
    _join_speaker_threads()
    assert spoken == [("Now in English.", "en")]

    ticker.on_lang_toggle()
    assert app.state.lang_state.current() == "ca"
    _join_speaker_threads()
    assert spoken[-1] == ("Ara en català.", "ca")


def _join_speaker_threads(timeout_s: float = 2.0) -> None:
    # The confirmation is spoken off the gesture thread (pw-play blocks for the
    # length of the line, and a ticker parked in it stops sensing).
    for thread in threading.enumerate():
        if thread.name == "lang-toggle-speak":
            thread.join(timeout=timeout_s)


def test_the_toggle_is_armed_only_while_the_robot_is_idle(monkeypatch, tmp_path):
    # The toggle ships OFF (driver.GestureTicker), so every test of what ARMS it
    # has to opt in first -- otherwise it would only be re-testing the default.
    idle = _app(monkeypatch, tmp_path, gesture=True,
                lang_toggle="1").state.gesture_ticker
    assert idle._toggle_armed() is True            # noqa: SLF001 - the gate under test

    running = _app(monkeypatch, tmp_path, gesture=True, lang_toggle="1",
                   extension=_FakeExtension(busy=True)).state.gesture_ticker
    assert running._toggle_armed() is False        # noqa: SLF001

    leased = _app(monkeypatch, tmp_path, gesture=True, lang_toggle="1",
                  extension=_FakeExtension(reserved=True)).state.gesture_ticker
    assert leased._toggle_armed() is False         # noqa: SLF001


def test_a_live_training_session_disarms_the_toggle(monkeypatch, tmp_path):
    from emori_adapter.hostapp.driver import SessionInfo

    app = _app(monkeypatch, tmp_path, gesture=True, lang_toggle="1")
    ticker = app.state.gesture_ticker
    assert ticker._toggle_armed() is True          # noqa: SLF001

    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="ca"))

    assert ticker._toggle_armed() is False         # noqa: SLF001


def test_the_toggle_flag_off_leaves_the_ticker_alone(monkeypatch, tmp_path):
    ticker = _app(monkeypatch, tmp_path, gesture=True,
                  lang_toggle="0").state.gesture_ticker
    assert ticker._toggle_armed() is False         # noqa: SLF001


# --- the spoken confirmation itself -------------------------------------------

class _FakeTts:
    def __init__(self, path: str | None = "line.wav") -> None:
        self.calls: list[tuple[str, str]] = []
        self._path = path

    def synth_path(self, text: str, lang: str = "en") -> str | None:
        self.calls.append((text, lang))
        return self._path


def test_the_confirmation_is_synthesised_in_the_new_language_and_played():
    played: list[list[str]] = []

    def _run(argv, **kwargs):
        played.append(list(argv))
        return type("R", (), {"returncode": 0})()

    tts = _FakeTts()
    assert server._speak_on_robot(tts, "Ara en català.", "ca", run=_run) is True
    assert tts.calls == [("Ara en català.", "ca")]
    assert played and played[0][0] == "pw-play" and played[0][-1] == "line.wav"


def test_a_robot_with_no_voice_or_no_player_stays_silent_without_raising():
    def _boom(argv, **kwargs):
        raise FileNotFoundError("pw-play")

    assert server._speak_on_robot(None, "hi", "en", run=_boom) is False
    assert server._speak_on_robot(_FakeTts(path=None), "hi", "en", run=_boom) is False
    assert server._speak_on_robot(_FakeTts(), "hi", "en", run=_boom) is False
