"""The token-gated hostapp surface the phone superadmin reaches in through.

``emori-admin`` runs as its OWN process (it has to survive restarting the hostapp),
so everything it needs goes over HTTP or a subprocess -- except the kiosk language,
which is a sequence over live in-process objects. These tests pin the three things
that make that surface safe to expose on a site LAN:

* it is **fail closed** -- no ``EMORI_ADMIN_TOKEN`` configured means 503, never an
  anonymous route that can re-language the robot;
* it refuses while anything at all is running (a session, a badge lease, an add-on
  mode), because a switch swaps the menu, the pages and the session claim under
  somebody who is already talking to the robot;
* the switch it performs is the SAME sequence the hidden head gesture performs --
  ``lang_state.set()`` on its own leaves the kiosk in the old language until the
  next restart.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
import pytest

from emori_adapter.hostapp import admin_api, server
from emori_adapter.hostapp.extensions import HostExtension

TOKEN = "0123456789abcdef0123456789abcdef"
HEADER = {admin_api.TOKEN_HEADER: TOKEN}


class _IdleRuntime(HostExtension):
    """An add-on that is reachable and doing nothing.

    ``mode`` is what its status reports; anything but "idle" counts as running.
    ``reserved`` stands for a badge scan that owns the robot.
    """

    def __init__(self, mode: str = "idle", reserved: bool = False) -> None:
        self.mode = mode
        self.reserved = reserved

    def status(self) -> dict:
        return {"mode": self.mode, "enabled": {}}

    def busy(self) -> bool:
        return self.mode != "idle"

    def selection_reserved(self) -> bool:
        return self.reserved


class _BlindRuntime(_IdleRuntime):
    """An add-on that cannot say what state it is in -- the fail-closed case."""

    def status(self) -> dict:
        raise RuntimeError("add-on state unreadable")

    def busy(self) -> bool:
        raise RuntimeError("add-on state unreadable")


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


class _FakeMicProfile:
    """Only ``.profile`` is read by the summary; wpctl is never involved."""

    def __init__(self, profile: str | None = "idle") -> None:
        self.profile = profile

    def set_profile(self, profile: str) -> bool:
        self.profile = profile
        return True


@pytest.fixture(autouse=True)
def _no_inherited_token(monkeypatch):
    """A developer's own /opt/emori-shaped .env must not decide these outcomes.

    ``emori_adapter.config`` loads .env into os.environ at import, so a real
    EMORI_ADMIN_TOKEN would silently turn the fail-closed tests green.
    """

    monkeypatch.delenv("EMORI_ADMIN_TOKEN", raising=False)


def _app(monkeypatch, tmp_path, *, lang: str = "ca", runtime=None, spoken=None,
         mic_profile=None, gesture: bool = False):
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "EMORI_LANG", lang)
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "PARTNER", "emori-robot")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    # NEVER the default artifacts/ path: a test that persisted a language there
    # would re-language every later test in the run.
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    overrides = {
        "robot_client": DryRunRobotClient(),
        "player": _RecordingPlayer(),
        "fiware_factory": None,
        "idle": False,
        "watchdog": False,
        "gesture": gesture,
        "extension": runtime if runtime is not None else _IdleRuntime(),
        "mic_profile": mic_profile if mic_profile is not None else _FakeMicProfile(),
        "tts": None,
        "stt": None,
        "speak": (lambda text, code: spoken.append((text, code)))
        if spoken is not None else (lambda text, code: None),
    }
    return server._build_app(overrides=overrides)


def _join_speaker_threads(timeout_s: float = 2.0) -> None:
    # The confirmation is spoken off the request thread (pw-play blocks for the
    # length of the line); join it before asserting on what was said.
    for thread in threading.enumerate():
        if thread.name == "lang-toggle-speak":
            thread.join(timeout=timeout_s)


def _client(monkeypatch, tmp_path, *, token: str | None = TOKEN, **kwargs):
    if token is not None:
        monkeypatch.setenv("EMORI_ADMIN_TOKEN", token)
    app = _app(monkeypatch, tmp_path, **kwargs)
    return app, TestClient(app)


# --- fail closed --------------------------------------------------------------

def test_without_a_configured_token_every_route_is_503(monkeypatch, tmp_path):
    """No shared secret in .env = no admin surface at all, not an open one."""

    app, client = _client(monkeypatch, tmp_path, token=None)

    summary = client.get("/api/admin/summary")
    switch = client.post("/api/admin/lang", json={"lang": "en"})

    assert summary.status_code == 503
    assert summary.json() == {"error": "admin_not_configured"}
    assert switch.status_code == 503
    # ...and nothing moved.
    assert app.state.lang_state.current() == "ca"


def test_a_configured_token_does_not_make_the_routes_public(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)

    assert client.get("/api/admin/summary").status_code == 401
    assert client.post("/api/admin/lang", json={"lang": "en"}).status_code == 401
    assert client.get("/api/admin/summary").json() == {"error": "unauthorized"}


def test_a_wrong_token_is_401_and_changes_nothing(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)

    response = client.post("/api/admin/lang", json={"lang": "en"},
                           headers={admin_api.TOKEN_HEADER: TOKEN[:-1] + "0"})

    assert response.status_code == 401
    assert app.state.lang_state.current() == "ca"


def test_a_non_ascii_token_header_is_rejected_not_a_500(monkeypatch, tmp_path):
    """Starlette decodes header bytes as latin-1 and ``hmac.compare_digest``
    refuses non-ASCII str: one crafted byte must not become a 500.

    Sent as raw BYTES because that is what a hostile client puts on the wire --
    httpx would refuse to encode the same value from a str.
    """

    _, client = _client(monkeypatch, tmp_path)

    response = client.get("/api/admin/summary",
                          headers={admin_api.TOKEN_HEADER: b"se\xf1al\xf1"})

    assert response.status_code == 401


def test_the_token_is_read_per_request_not_frozen_at_mount(monkeypatch, tmp_path):
    """An operator who fixes a typo in .env and restarts the ADMIN unit only."""

    _, client = _client(monkeypatch, tmp_path, token=None)
    assert client.get("/api/admin/summary").status_code == 503

    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    assert client.get("/api/admin/summary", headers=HEADER).status_code == 200


def test_surrounding_whitespace_in_the_env_value_still_matches(monkeypatch, tmp_path):
    """`.env` values routinely carry a trailing space nobody can see."""

    _, client = _client(monkeypatch, tmp_path, token=f"  {TOKEN}\t")

    assert client.get("/api/admin/summary", headers=HEADER).status_code == 200


# --- summary ------------------------------------------------------------------

def test_the_summary_reports_what_the_phone_renders(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, lang="ca",
                        mic_profile=_FakeMicProfile("session"))

    body = client.get("/api/admin/summary", headers=HEADER).json()

    assert body == {"lang": "ca", "allowed_langs": ["ca", "en"], "mic_profile": "session",
                    "session_active": False, "addon_mode": "idle", "idle": True}


def test_the_summary_follows_a_switch_and_a_live_session(monkeypatch, tmp_path):
    from emori_adapter.hostapp.driver import SessionInfo

    app, client = _client(monkeypatch, tmp_path, lang="ca")
    app.state.lang_state.set("en")
    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))

    body = client.get("/api/admin/summary", headers=HEADER).json()

    assert body["lang"] == "en"
    assert body["session_active"] is True


def test_an_unreadable_runtime_reports_a_null_mode_not_a_500(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, runtime=_BlindRuntime())

    body = client.get("/api/admin/summary", headers=HEADER)

    assert body.status_code == 200
    assert body.json()["addon_mode"] is None


def test_an_unapplied_mic_profile_is_reported_as_null(monkeypatch, tmp_path):
    """None = wpctl never took (a dev box); the phone must not read it as a level."""

    _, client = _client(monkeypatch, tmp_path, mic_profile=_FakeMicProfile(None))

    assert client.get("/api/admin/summary", headers=HEADER).json()["mic_profile"] is None


# --- may the host app restart now? (summary "idle") ----------------------------

def test_the_summary_says_idle_only_when_nothing_at_all_runs(monkeypatch, tmp_path):
    """The ONE answer emori-admin's Apply button and its auto-apply restart on: the
    strictest predicate this process has, fail-closed."""

    from emori_adapter.hostapp.driver import SessionInfo

    def idle(**kwargs) -> bool:
        app, client = _client(monkeypatch, tmp_path, **kwargs)
        return app, client.get("/api/admin/summary", headers=HEADER).json()["idle"]

    assert idle()[1] is True
    # An add-on mode, a badge lease, an unreadable add-on: busy.
    assert idle(runtime=_IdleRuntime(mode="running"))[1] is False
    assert idle(runtime=_IdleRuntime(reserved=True))[1] is False
    assert idle(runtime=_BlindRuntime())[1] is False
    # A live training session: busy.
    app, client = _client(monkeypatch, tmp_path)
    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="ca"))
    assert client.get("/api/admin/summary", headers=HEADER).json()["idle"] is False


# --- the switch ---------------------------------------------------------------

def test_the_switch_performs_the_whole_gesture_sequence(monkeypatch, tmp_path):
    """Not just lang_state: the driver stamp, the page-reload verb and the spoken
    confirmation are what make a switch visible on a screenless robot."""

    spoken: list[tuple[str, str]] = []
    app, client = _client(monkeypatch, tmp_path, lang="ca", spoken=spoken)

    response = client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "lang": "en"}
    assert app.state.lang_state.current() == "en"
    assert app.state.driver.default_lang == "en"
    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] == "reload"
    assert 'const LANG="en"' in client.get("/").text
    _join_speaker_threads()
    assert spoken == [("Now in English.", "en")]


def test_the_switch_persists_across_a_rebuild(monkeypatch, tmp_path):
    """The state file is what survives the power cut that ends every day."""

    _, client = _client(monkeypatch, tmp_path, lang="ca")
    client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)

    restarted = _app(monkeypatch, tmp_path, lang="ca")

    assert restarted.state.lang_state.current() == "en"


def test_switching_to_the_language_already_in_force_is_a_silent_no_op(monkeypatch,
                                                                     tmp_path):
    spoken: list[tuple[str, str]] = []
    app, client = _client(monkeypatch, tmp_path, lang="ca", spoken=spoken)

    response = client.post("/api/admin/lang", json={"lang": "ca"}, headers=HEADER)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "lang": "ca"}
    # No reload verb (a landing page mid-render must not be told to reload for
    # nothing) and no announcement out of the speaker.
    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None
    _join_speaker_threads()
    assert spoken == []
    assert app.state.lang_state.current() == "ca"


def test_the_language_already_in_force_is_a_no_op_even_mid_session(monkeypatch,
                                                                   tmp_path):
    """The already-lit button must never answer 409.

    The phone renders EN/CA as two buttons with the current one highlighted, so
    a tap on it is a re-affirmation, not a change. It writes nothing, reloads
    nothing and speaks nothing, so there is no live session to protect -- and
    answering "session in progress" for a request that changes nothing reads as
    a broken robot to the operator standing in front of it.
    """

    from emori_adapter.hostapp.driver import SessionInfo

    spoken: list[tuple[str, str]] = []
    app, client = _client(monkeypatch, tmp_path, lang="ca", spoken=spoken)
    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1",
                                               lang="ca"))

    response = client.post("/api/admin/lang", json={"lang": "ca"}, headers=HEADER)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "lang": "ca"}
    # ...and it stayed a no-op: no reload verb, no announcement, no state file.
    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None
    _join_speaker_threads()
    assert spoken == []
    assert not (tmp_path / "kiosk_lang.json").exists()


@pytest.mark.parametrize("body", [{"lang": "fr"}, {"lang": ""}, {"lang": None},
                                  {}, {"lang": ["en"]}, [], "en"])
def test_a_language_this_robot_does_not_ship_is_a_400(monkeypatch, tmp_path, body):
    app, client = _client(monkeypatch, tmp_path, lang="ca")

    response = client.post("/api/admin/lang", json=body, headers=HEADER)

    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_lang"}
    assert app.state.lang_state.current() == "ca"


def test_an_unparseable_body_is_a_400_not_a_500(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)

    response = client.post("/api/admin/lang", content=b"{not json",
                           headers={**HEADER, "Content-Type": "application/json"})

    assert response.status_code == 400


def test_the_language_code_is_normalised_the_way_the_kiosk_normalises_it(monkeypatch,
                                                                        tmp_path):
    app, client = _client(monkeypatch, tmp_path, lang="ca")

    assert client.post("/api/admin/lang", json={"lang": " EN "},
                       headers=HEADER).json() == {"ok": True, "lang": "en"}
    assert app.state.lang_state.current() == "en"


# --- the idle gate ------------------------------------------------------------

def test_a_live_training_session_refuses_the_switch(monkeypatch, tmp_path):
    from emori_adapter.hostapp.driver import SessionInfo

    spoken: list[tuple[str, str]] = []
    app, client = _client(monkeypatch, tmp_path, lang="ca", spoken=spoken)
    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="ca"))

    response = client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)

    assert response.status_code == 409
    assert response.json() == {"error": "busy"}
    assert app.state.lang_state.current() == "ca"
    assert app.state.driver.default_lang == "ca"
    _join_speaker_threads()
    assert spoken == []


@pytest.mark.parametrize("runtime", [
    _IdleRuntime(mode="running"),      # an add-on mode owns the robot
    _IdleRuntime(reserved=True),             # a badge scan has claimed it for training
    _BlindRuntime(),                         # it cannot say -> fail closed
])
def test_anything_running_on_the_robot_refuses_the_switch(monkeypatch, tmp_path,
                                                          runtime):
    app, client = _client(monkeypatch, tmp_path, lang="ca", runtime=runtime)

    response = client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)

    assert response.status_code == 409
    assert app.state.lang_state.current() == "ca"


def test_the_busy_gate_is_checked_before_anything_is_written(monkeypatch, tmp_path):
    """A 409 must leave no half-switch behind: no state file, no reload verb."""

    from emori_adapter.hostapp.driver import SessionInfo

    app, client = _client(monkeypatch, tmp_path, lang="ca")
    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="ca"))

    client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)

    assert client.get("/api/pending-cmd?verbs=reload").json()["cmd"] is None
    assert not (tmp_path / "kiosk_lang.json").exists()


# --- one sequence, two inputs -------------------------------------------------

def _effects(app, client, spoken) -> tuple:
    """Everything a switch is supposed to leave behind, in one comparable tuple."""

    _join_speaker_threads()
    return (app.state.lang_state.current(), app.state.driver.default_lang,
            client.get("/api/pending-cmd?verbs=reload").json()["cmd"],
            list(spoken))


def test_the_gesture_and_the_api_run_the_same_sequence(monkeypatch, tmp_path):
    """The refactor's whole point: two inputs, ONE implementation.

    If they ever diverge, one of them silently stops doing part of the switch --
    which is exactly the bug the shared helper exists to prevent.
    """

    spoken: list[tuple[str, str]] = []
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    app = _app(monkeypatch, tmp_path, lang="ca", spoken=spoken, gesture=True)
    client = TestClient(app)
    toggle = app.state.gesture_ticker.on_lang_toggle
    assert callable(toggle)

    client.post("/api/admin/lang", json={"lang": "en"}, headers=HEADER)
    by_api = _effects(app, client, spoken)

    spoken.clear()
    toggle()                                   # the double head turn, back to Catalan
    by_gesture = _effects(app, client, spoken)

    assert by_api == ("en", "en", "reload", [("Now in English.", "en")])
    assert by_gesture == ("ca", "ca", "reload", [("Ara en català.", "ca")])


# --- the optional add-on seam ------------------------------------------------------

class _RaisingAdminExtension(HostExtension):
    """An add-on whose admin object cannot be built."""

    def admin_api(self):
        raise RuntimeError("broken add-on")


def _mounted(extension, *, idle=True):
    from fastapi import FastAPI

    app = FastAPI()
    admin_api.mount(app, lang_state=None, driver=None, mic_profile=None,
                    robot_idle=lambda: idle, set_lang=lambda code: code,
                    extension=extension)
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.mark.parametrize("extension", [HostExtension(), _RaisingAdminExtension(), None])
def test_the_admin_routes_mount_without_an_add_on(monkeypatch, extension):
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    client = _mounted(extension)

    summary = client.get("/api/admin/summary", headers=HEADER)
    site = client.get("/api/admin/site", headers=HEADER)

    assert summary.status_code == 200
    assert summary.json()["addon_mode"] is None and summary.json()["idle"] is True
    assert site.status_code == 200
    # No add-on, no add-on routes.
    assert client.get("/api/admin/audio-devices", headers=HEADER).status_code == 404


def test_the_default_extension_neither_vetoes_a_restart_nor_renames_a_setting(monkeypatch):
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)

    assert _mounted(HostExtension(), idle=True).get(
        "/api/admin/summary", headers=HEADER).json()["idle"] is True
    assert _mounted(HostExtension(), idle=False).get(
        "/api/admin/summary", headers=HEADER).json()["idle"] is False
