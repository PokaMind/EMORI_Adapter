"""Tests for the host-app SSO entry and its kiosk endpoints."""

from __future__ import annotations

import pytest

from emori_adapter.hostapp import jwt_sso
from emori_adapter.hostapp.jwt_sso import build_sso_url
from tests.test_robot_token_hostapp import sso_claims, use_golden_token


def test_the_only_sso_minter_is_the_robots_own_token():
    # No shared-secret minter or verifier is left to sign a login with by mistake.
    assert not hasattr(jwt_sso, "mint_partner_jwt")
    assert not hasattr(jwt_sso, "verify_partner_jwt")
    assert callable(jwt_sso.mint_robot_sso_jwt)


def test_sso_url():
    url = build_sso_url("https://app.pokamind.com/", "emori-robot", "TOK")
    assert url == "https://app.pokamind.com/partner/sso/login?token=TOK&partner=emori-robot"


# --- Verbal topic selection endpoint (screenless) -------------------------------------

def _client(monkeypatch, *, demo: bool):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    use_golden_token(monkeypatch, server)
    monkeypatch.setattr(server, "DEMO", demo)
    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
    return TestClient(server._build_app())


def _menu():
    """The menu the app under test loads. WHICH course that is is deployment data, so
    these tests read the course instead of freezing one set of ids."""
    from emori_adapter.hostapp import server

    return server._load_menu(server.EMORI_LANG)


def test_voice_menu_endpoint(monkeypatch):
    c = _client(monkeypatch, demo=False)
    body = c.get("/api/voice-menu").json()
    assert body["prompt"]
    keys = [o["key"] for o in body["options"]]
    assert keys == list(_menu().options)                       # every option is offered
    assert keys == [str(n) for n in range(1, len(keys) + 1)]   # numbered from one, in order


def test_voice_select_carries_module_id_into_jwt(monkeypatch):
    """A spoken "two" must start the menu's SECOND module via SSO (skips TopicSelection)."""
    from urllib.parse import parse_qs, unquote, urlparse

    c = _client(monkeypatch, demo=False)
    second = _menu().options["2"]
    d = c.post("/api/voice-select", json={"text": "number two please"}).json()
    assert d["matched"] is True and d["exact"] is True
    assert d["topic"] == second.name
    assert d["module_id"] == second.module_id

    # The session URL must embed an SSO JWT whose module_id claim is the chosen module,
    # so Pokamind starts that module directly with no topic screen.
    sso = unquote(parse_qs(urlparse(d["session_url"]).query)["sso"][0])
    assert parse_qs(urlparse(sso).query)["token"][0]
    claims = sso_claims(d["session_url"])
    assert claims["module_id"] == second.module_id
    assert claims["course_id"] == "emori-roleplay"


def test_voice_select_defaults_when_unrecognised(monkeypatch):
    menu = _menu()
    fallback = menu.options[menu.default]
    c = _client(monkeypatch, demo=False)
    d = c.post("/api/voice-select", json={"text": "banana split"}).json()
    assert d["matched"] is True and d["exact"] is False  # fell back to default
    assert d["topic"] == fallback.name
    assert d["module_id"] == fallback.module_id


def test_voice_select_demo_mode_uses_synthetic_session(monkeypatch):
    c = _client(monkeypatch, demo=True)
    d = c.post("/api/voice-select", json={"text": "three"}).json()
    assert d["matched"] is True and d["module_id"] == _menu().options["3"].module_id
    assert "demo=1" in d["session_url"]


# --- Turn-taking events (screenless watchdog ingestion) ------------------------

def _client_no_tickers(monkeypatch, *, demo: bool):
    """Like ``_client`` but disables the background tickers so no live 1s daemon
    can call _finalize_locked mid-test and corrupt counter/status assertions."""
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    use_golden_token(monkeypatch, server)
    monkeypatch.setattr(server, "DEMO", demo)
    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
    return TestClient(server._build_app(overrides={"watchdog": False, "idle": False}))


def test_turn_events_dispatch_ok(monkeypatch):
    c = _client_no_tickers(monkeypatch, demo=False)
    # No active session: the handlers just bump counters + feed the (un-polled) watchdog.
    r = c.post("/api/cycle", json={"type": "pokamind_turn_armed", "session_id": "s1"})
    assert r.status_code == 200 and r.json().get("ok") is True
    r = c.post("/api/cycle", json={"type": "pokamind_turn_submitted", "session_id": "s1"})
    assert r.json().get("ok") is True
    r = c.post("/api/cycle", json={"type": "pokamind_tts_ended", "session_id": "s1"})
    assert r.json().get("ok") is True
    r = c.post("/api/cycle", json={"type": "pokamind_turn_rejected", "session_id": "s1"})
    assert r.json().get("ok") is True

    counters = c.get("/api/status").json()["counters"]
    assert counters.get("turn_armed", 0) >= 1
    assert counters.get("turn_submitted", 0) >= 1
    assert counters.get("tts_ended", 0) >= 1
    assert counters.get("turn_rejected", 0) >= 1
    # The ticker is disabled, so the watchdog never fired reprompt/force_end.
    assert counters.get("watchdog_reprompts", 0) == 0
    assert counters.get("watchdog_force_ends", 0) == 0


def test_user_speaking_activity_feeds_watchdog(monkeypatch):
    """The throttled kiosk relay (pokamind_user_speaking) must reach the driver
    counter AND the watchdog's speaking-activity stall clock."""
    c = _client_no_tickers(monkeypatch, demo=False)
    driver = c.app.state.driver
    r = c.post("/api/cycle", json={"type": "pokamind_user_speaking"})
    assert r.status_code == 200
    assert driver.counters["user_speaking_activity"] == 1


def test_listening_gesture_is_distinct_from_greeting():
    """The 'your turn' arm cue must be its own gesture, not greeting (session-start only)."""
    from emori_adapter.behavior import repertoire as R
    assert hasattr(R, "listening_gesture")
    listening = R.listening_gesture()
    greeting = R.greeting_gesture()
    assert isinstance(listening, list) and len(listening) >= 1
    # quieter than the greeting wave (fewer motion beats), and a valid intent list
    assert len(listening) < len(greeting)
    assert listening[0].kind == "goto"


# --- screenless gesture session control (head-tilt) ---------------------------

def test_pending_cmd_endpoint_drains(monkeypatch):
    c = _client_no_tickers(monkeypatch, demo=False)
    # nothing queued yet. ``tt_rev`` rides the same poll (tests/test_tt_inject.py)
    # and is 0 == "no overrides were ever set", so the channel is inert here.
    assert c.get("/api/pending-cmd").json() == {"cmd": None, "tt_rev": 0}


def test_gesture_ticker_emits_direction_when_idle_and_end_when_training():
    """Idle head movement emits a direction; training retains only binary end."""
    from emori_adapter.hostapp.gestures import GestureDirection
    from emori_adapter.hostapp.driver import GestureTicker, LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    class IdlePlayer:
        busy = False

        def submit(self, intents, *, drop_if_busy=False):
            return True

    client = DryRunRobotClient()
    # This test exercises the idle gesture state machine. Inject an already-idle
    # player so boot wake self-motion is covered only by the dedicated regressions.
    driver = LiveDriver(client, player=IdlePlayer(), fiware_factory=None)
    directions = []
    activity_seen_by_handler = []

    def on_direction(direction):
        directions.append(direction)
        activity_seen_by_handler.append(driver.last_activity)

    gt = GestureTicker(driver, on_direction=on_direction)

    rest = (0.0, 0.0, 0.0)
    right = (0.0, 0.0, 0.30)
    training_tilt = (0.0, 0.30, 0.0)

    driver.last_activity = 0.0
    # A selection needs a settle-gated anchor AND an observed rest on it before
    # the rise (a decaying head otherwise reads as a turn).
    for _ in range(6):
        gt.process_pose(rest)
    gt.process_pose(right)
    assert directions == [GestureDirection.RIGHT]
    assert driver.pop_cmd() is None
    assert driver.last_activity > 0.0
    assert activity_seen_by_handler[0] > 0.0

    # Once training is active, direction selection is suppressed and the
    # pre-existing binary detector still emits the sole "end" command.
    driver.tracker.start(SessionInfo(session_id="g1", person_id="p1", lang="en"))
    gt.process_pose(rest)
    gt.process_pose(training_tilt)
    assert driver.pop_cmd() == "end"
    assert directions == [GestureDirection.RIGHT]


def test_gesture_ticker_suppresses_self_authored_motion_until_settled():
    """An executing goto cannot be mistaken for a staff right-head selection."""
    import threading
    import time

    from emori_adapter.hostapp.driver import GestureTicker, LiveDriver, RobotPlayer
    from emori_adapter.models import MotionIntent

    class MotionClient:
        def __init__(self):
            self.apply_started = threading.Event()
            self.allow_apply_return = threading.Event()
            self.settle_started = threading.Event()
            self.allow_settle = threading.Event()
            self._motor_mode_calls = 0

        def health(self):
            return True

        def wake_up(self):
            pass

        def set_motor_mode(self, mode):
            assert mode == "gravity_compensation"
            self._motor_mode_calls += 1
            if self._motor_mode_calls > 1:
                self.settle_started.set()
                assert self.allow_settle.wait(timeout=2.0)

        def apply(self, intent):
            assert intent.kind == "goto"
            self.apply_started.set()
            assert self.allow_apply_return.wait(timeout=2.0)

    client = MotionClient()
    player = RobotPlayer(client, gravity_comp=True)
    driver = LiveDriver(client, player=player, fiware_factory=None)
    directions = []
    ticker = GestureTicker(driver, on_direction=directions.append)
    ticker.process_pose((0.0, 0.0, 0.0))  # baseline before robot-authored yaw

    assert player.submit([MotionIntent(kind="goto", duration=1.0)]) is True
    try:
        assert client.apply_started.wait(timeout=1.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        client.allow_apply_return.set()
        assert client.settle_started.wait(timeout=2.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        client.allow_settle.set()
        deadline = time.monotonic() + 1.0
        while player.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert player.busy is False

        # The first post-motion pose is a baseline, not a delayed direction.
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []
    finally:
        client.allow_apply_return.set()
        client.allow_settle.set()


def test_robot_player_marks_busy_before_publishing_a_batch_to_its_worker():
    """No consumer may take a queued batch before ``busy`` is visible."""
    import threading

    from emori_adapter.hostapp.driver import RobotPlayer
    from emori_adapter.models import MotionIntent

    class ObservingQueue:
        def qsize(self):
            return 0

        def put_nowait(self, intents):
            assert player.busy is True

    player = object.__new__(RobotPlayer)
    player._motion_state_lock = threading.Lock()
    player._motion_active = threading.Event()
    player._stop = threading.Event()
    player._wake_event = threading.Event()
    player._q = ObservingQueue()

    assert player.submit([MotionIntent(kind="goto")]) is True


def test_robot_player_startup_wake_suppresses_direction_until_settled():
    """A delayed daemon's boot wake is self-motion, not a right selection."""
    import threading
    import time

    from emori_adapter.hostapp.driver import GestureTicker, LiveDriver, RobotPlayer

    class DelayedWakeClient:
        def __init__(self):
            self.first_health_check = threading.Event()
            self.ready = threading.Event()
            self.wake_started = threading.Event()
            self.allow_wake = threading.Event()
            self.compliance_started = threading.Event()
            self.allow_compliance = threading.Event()

        def health(self):
            self.first_health_check.set()
            return self.ready.is_set()

        def wake_up(self):
            self.wake_started.set()
            assert self.allow_wake.wait(timeout=3.0)

        def set_motor_mode(self, mode):
            assert mode == "gravity_compensation"
            self.compliance_started.set()
            assert self.allow_compliance.wait(timeout=3.0)

    client = DelayedWakeClient()
    player = RobotPlayer(client, gravity_comp=True)
    driver = LiveDriver(client, player=player, fiware_factory=None)
    directions = []
    ticker = GestureTicker(driver, on_direction=directions.append)
    ticker.process_pose((0.0, 0.0, 0.0))

    assert client.first_health_check.wait(timeout=1.0)
    client.ready.set()
    try:
        assert client.wake_started.wait(timeout=2.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        client.allow_wake.set()
        assert client.compliance_started.wait(timeout=2.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        client.allow_compliance.set()
        deadline = time.monotonic() + 1.0
        while player.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert player.busy is False
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []
    finally:
        client.allow_wake.set()
        client.allow_compliance.set()


def test_robot_player_explicit_wake_intent_uses_fixed_wake_settle():
    """The duration-less daemon wake endpoint always gets its full settle guard."""
    import threading
    import time

    from emori_adapter.hostapp.driver import GestureTicker, LiveDriver, RobotPlayer
    from emori_adapter.models import MotionIntent

    class ExplicitWakeClient:
        def __init__(self):
            self.startup_compliance_done = threading.Event()
            self.apply_started = threading.Event()
            self.allow_apply = threading.Event()
            self.compliance_started = threading.Event()
            self.allow_compliance = threading.Event()
            self._mode_calls = 0

        def health(self):
            return True

        def wake_up(self):
            pass

        def set_motor_mode(self, mode):
            assert mode == "gravity_compensation"
            self._mode_calls += 1
            if self._mode_calls == 1:
                self.startup_compliance_done.set()
            else:
                self.compliance_started.set()
                assert self.allow_compliance.wait(timeout=2.0)

        def apply(self, intent):
            assert intent.kind == "wake_up"
            self.apply_started.set()
            assert self.allow_apply.wait(timeout=2.0)

    client = ExplicitWakeClient()
    player = RobotPlayer(client, gravity_comp=True)
    driver = LiveDriver(client, player=player, fiware_factory=None)
    directions = []
    ticker = GestureTicker(driver, on_direction=directions.append)
    assert client.startup_compliance_done.wait(timeout=1.0)
    ticker.process_pose((0.0, 0.0, 0.0))

    assert player.submit([MotionIntent(kind="wake_up", duration=0.12)]) is True
    try:
        assert client.apply_started.wait(timeout=1.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        # The shutdown-aware player waits on its stop Event rather than a
        # raw sleep.  A supplied 0.12s intent must still retain the 0.6s wake
        # settle guard before it asks the daemon to restore compliance.
        client.allow_apply.set()
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []
        assert not client.compliance_started.wait(timeout=0.1)
        assert client.compliance_started.wait(timeout=1.0)
        assert player.busy is True
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []

        client.allow_compliance.set()
        deadline = time.monotonic() + 1.0
        while player.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert player.busy is False
        ticker.process_pose((0.0, 0.0, 0.30))
        assert directions == []
    finally:
        client.allow_apply.set()
        client.allow_compliance.set()


def test_gesture_sensor_env_gated_off_for_sim(monkeypatch):
    """EMORI_GESTURE=0 disables the head-tilt sensor (required on the MuJoCo sim, where the
    back-drivable head drifts under gravity and the detector reads that as spurious taps).
    Observable proxy: the player's gravity_comp follows gesture_enabled."""
    from emori_adapter.hostapp import server

    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")

    monkeypatch.setenv("EMORI_GESTURE", "0")
    app_off = server._build_app(overrides={"watchdog": False, "idle": False})
    assert app_off.state.driver.player._gravity_comp is False

    monkeypatch.delenv("EMORI_GESTURE", raising=False)   # default = on (real robot)
    app_on = server._build_app(overrides={"watchdog": False, "idle": False})
    assert app_on.state.driver.player._gravity_comp is True

    # explicit override still wins (tests)
    app_ov = server._build_app(overrides={"watchdog": False, "idle": False, "gesture": False})
    assert app_ov.state.driver.player._gravity_comp is False


def test_walkaway_end_finalizes_an_idle_active_session():
    """An active session with no activity past EMORI_WALKAWAY_S is gracefully ended.

    Walk-away first asks Pokamind to complete (so its analysis is captured); when Pokamind
    never answers (tiny end_grace_s here), the local fail-safe finalizes the session."""
    import time as _time
    from emori_adapter.hostapp.driver import LiveDriver, WatchdogTicker, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None)
    driver.tracker.start(SessionInfo(session_id="w1", person_id="p1", lang="en"))
    # tiny walkaway window; make last_activity stale. end_grace_s tiny so the fail-safe
    # local finalize runs within the test window (no real Pokamind to complete).
    wt = WatchdogTicker(driver, tick_s=0.01, walkaway_s=0.05, end_grace_s=0.05)
    driver.last_activity = _time.monotonic() - 1.0   # already stale
    wt.start()
    try:
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and driver.tracker.active:
            _time.sleep(0.01)
    finally:
        wt.stop()
        wt._t.join(timeout=1.0)
    assert driver.counters.get("walkaway_ends", 0) == 1   # asked Pokamind to complete
    assert driver.counters.get("end_local_fallback", 0) == 1  # Pokamind never answered
    assert driver.tracker.active is False            # session ended


# --- the walk-away wedge (a session that ends ROBOT-side must free the page) ----------
# When the walk-away fail-safe finalizes the session inside the hostapp (Pokamind never
# answered `emori_cmd:end`) and nothing tells the /session page, the page keeps polling a
# dead session forever, the renderer keeps spinning, and the kiosk never returns to the
# badge screen. `home` is the page-level verb that frees it, riding
# the /api/pending-cmd poll the page already runs every 250 ms.

def _analysis_envelope() -> dict:
    """A body-bearing cycle -- the only way tracker.cycles reaches 1, which is what makes
    start_session finalize the PREVIOUS learner (driver.start_session, cycles > 0)."""
    return {"type": "pokamind_analysis_update", "seq": 1,
            "body_language": {"posture": "upright", "engagement_score": 52.4},
            "body_language_metrics": {"engagement": 52.4, "confidence": 43.7},
            "communication_styles": {"neutral": 12.41, "steady": 7.81}}


def test_robot_side_walkaway_finalize_sends_the_session_page_home(monkeypatch):
    """The wedge: Pokamind never answers `end`, the hostapp finalizes
    locally (end_local_fallback), and the page must be told to leave."""
    import time as _time
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    c = _client_no_tickers(monkeypatch, demo=False)
    driver = c.app.state.driver
    driver.start_session(SessionInfo(session_id="w1", person_id="p1", lang="en"))
    assert c.get("/api/pending-cmd").json()["cmd"] is None    # nothing queued yet

    wt = WatchdogTicker(driver, tick_s=0.01, walkaway_s=0.05, end_grace_s=0.05,
                        session_start_s=0)
    driver.last_activity = _time.monotonic() - 1.0
    wt.start()
    try:
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and driver.tracker.active:
            _time.sleep(0.01)
    finally:
        wt.stop()
        wt._t.join(timeout=1.0)
    assert driver.counters.get("end_local_fallback", 0) == 1

    assert c.get("/api/pending-cmd").json()["cmd"] == "home"
    # STICKY, unlike the single-slot begin/end verbs: one dropped poll (a stalled CM4
    # renderer, a lost response) would otherwise lose the only copy of `home` and
    # re-wedge exactly the page this exists to free.
    assert c.get("/api/pending-cmd").json()["cmd"] == "home"


def test_normal_completion_never_queues_home(monkeypatch):
    """finish() on the page already speaks the summary and navigates itself. A `home`
    queued by the same finalize would be drained by a poll landing between the enqueue
    and the /api/cycle response -- cutting the summary off and double-navigating."""
    from emori_adapter.hostapp.driver import SessionInfo

    c = _client_no_tickers(monkeypatch, demo=False)
    driver = c.app.state.driver
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    r = c.post("/api/cycle", json={"type": "pokamind_session_completed",
                                   "session_id": "s1", "status": "completed"})
    assert r.json()["completed"] is True
    assert driver.tracker.active is False                     # the session DID finalize
    assert c.get("/api/pending-cmd").json()["cmd"] is None     # ...silently


def test_a_new_session_page_is_never_sent_home(monkeypatch):
    """Opening /session finalizes the previous learner (start_session, cycles > 0). The
    only page polling by then is the NEW one, so a `home` from that finalize -- or one
    left armed by an earlier robot-side end -- would kill the session just started."""
    from emori_adapter.hostapp.driver import SessionInfo

    c = _client_no_tickers(monkeypatch, demo=False)
    driver = c.app.state.driver
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    c.post("/api/cycle", json=_analysis_envelope())
    assert driver.tracker.cycles == 1
    with driver._lock:                       # a robot-side end arms `home`...
        driver._finalize_locked(results=None)
    assert c.get("/api/pending-cmd").json()["cmd"] == "home"

    driver.start_session(SessionInfo(session_id="s2", person_id="p2", lang="en"))
    c.post("/api/cycle", json=_analysis_envelope())
    assert c.get("/session").status_code == 200      # ...the next learner's page opens
    assert c.get("/api/pending-cmd").json()["cmd"] is None


def test_session_page_acts_on_home_itself_and_only_once(monkeypatch):
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    # `home` is a PAGE verb: unlike begin/end it must never be relayed into the iframe.
    assert "d.cmd==='home'" in html
    assert "emoriCmd('home')" not in html
    # It shares the `over` latch with finish(), so whichever fires first wins and the
    # page navigates exactly once no matter how many polls see the sticky verb.
    assert "function goHome(why,line){ if(over) return; over=true;" in html
    assert "stopSpeech(); location.href='/';" in html
    # ...and the summary finish() speaks is stopped before ITS navigation too,
    # behind a once-latch: next() is reachable from BOTH the say() callback and the
    # 30s cap, and the second arrival must not re-navigate mid-navigation.
    assert "const next=()=>{ if(went) return; went=true; stopSpeech(); " \
           "location.href=(d&&d.next_url)||'/'; };" in html
    # ...and it survives template substitution, not just the template.
    rendered = _client_no_tickers(monkeypatch, demo=False).get("/session").text
    assert "d.cmd==='home'" in rendered


# --- the landing announcement must not play over the session's own opening TTS --------
# A beginSession that force-navigates on a hard 4 s timer while a ~4.4 s Piper WAV
# announcement is still playing, with nothing to stop that <audio>, keeps talking over
# Pokamind's first spoken line on the robot's single speaker.

def test_say_tracks_its_audio_so_a_page_can_stop_it():
    from emori_adapter.hostapp import pages

    js = pages._SPEECH_JS
    assert "function stopSpeech()" in js
    assert "__sayAudio.push(a)" in js
    # Pausing the ELEMENT is the load-bearing half: the robot voice is a Piper WAV in an
    # <audio>, so speechSynthesis.cancel() alone (the pre-existing call) stops nothing.
    assert "a.pause()" in js
    assert "speechSynthesis.cancel()" in js


def test_say_rearms_its_deadline_from_the_real_audio_duration():
    from emori_adapter.hostapp import pages

    js = pages._SPEECH_JS
    assert "function sayMaxMs(text)" in js
    assert "arm(sayMaxMs(text))" in js          # until the decoder knows better...
    assert "a.onloadedmetadata=" in js          # ...then the measured length wins
    assert "a.duration*1000" in js


def test_begin_session_stops_the_announcement_on_every_navigation_path():
    from emori_adapter.hostapp import pages

    html = pages.VOICE_HTML
    begin = html[html.index("async function beginSession("):html.index("function stopCam()")]
    # ONE navigation statement, and it silences this page first. The overlap above --
    # a landing WAV still talking over Pokamind's opening line on the single speaker --
    # is now structurally impossible for a different reason as well: the landing page no
    # longer speaks on this path at all (see the latency test below), so the only thing
    # stopSpeech() has left to catch is a menu prompt interrupted by a topic BUTTON.
    assert begin.count("location.href=") == 1
    assert "stopSpeech();\n    location.href=started.session_url;" in begin


def test_the_landing_page_never_delays_navigation_to_finish_a_sentence():
    """Navigation must not BE the speech-completion callback.

    `say(line, go)` would mean the session URL only starts loading when the announcement
    finishes -- about 3 s at the front of the session-start wait. The line is
    spoken by /session now (pages.START_COVER), which is the document that survives the
    navigation; this page must not open a mouth on the begin path at all, because its
    audio would be destroyed mid-word the moment the new document commits.
    """
    from emori_adapter.hostapp import pages

    html = pages.VOICE_HTML
    begin = html[html.index("async function beginSession("):html.index("function stopCam()")]
    # CODE only: the comment block in there quotes the `say(line, go)` it replaced, and an
    # assertion that a rewritten comment can satisfy pins nothing.
    code = "\n".join(ln for ln in begin.splitlines() if not ln.lstrip().startswith("//"))
    assert "say(" not in code                      # nothing to wait for...
    assert "sayMaxMs" not in code                  # ...so no speech fail-safe either
    assert "setTimeout(" not in code               # ...and no timer in front of the load
    # The one thing between the answer and the navigation is the mint itself (~15 ms).
    assert code.index("await fetch('/api/voice-select'") < code.index("location.href=")


def test_the_session_page_speaks_the_covering_line_it_inherited():
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    speak = "if(START_LINE) say(START_LINE,()=>coverHold(false),coverAudible);"
    assert "const START_LINE=__START_LINE__;" in html
    assert speak in html
    # LAST in the script: the frame append and armSessionStartDeadline() are synchronous
    # and both must already have happened, so speaking can never gate the load.
    assert html.index("armSessionStartDeadline();") < html.index(speak)
    assert html.index(speak) > html.index("stage.appendChild(f);")
    # Nothing on this page waits for the line: its completion callback only tells the robot
    # that it has ended, and the third argument, the START hook, tells hushCover() whether
    # the line is audible yet.


# --- the defaults a robot gets with nothing in .env -----------------------------------

def _server_constants(env_overrides: dict) -> dict:
    """``server``'s module-level defaults, imported in a fresh interpreter."""

    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if key not in ("POKAMIND_BASE", "EMORI_COURSE_ID", "EMORI_LANG",
                          "EMORI_SITE_SOURCES", "POKAMIND_ROBOT_TOKEN")}
    env.update(env_overrides)
    env["PYTHONPATH"] = str(root / "src")
    code = ("import json; from emori_adapter.hostapp import server as s; "
            "print(json.dumps([s.POKAMIND_BASE, s.COURSE_ID, s.EMORI_LANG]))")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(root),
                         capture_output=True, text=True, timeout=120, check=True)
    return dict(zip(("base", "course", "lang"), json.loads(out.stdout.strip().splitlines()[-1])))


def test_the_defaults_are_the_api_origin_the_generic_course_and_english():
    values = _server_constants({"POKAMIND_BASE": "", "EMORI_COURSE_ID": " ", "EMORI_LANG": ""})

    assert values == {"base": "https://api.pokamind.com", "course": "emori", "lang": "en"}


def test_explicit_values_still_win():
    values = _server_constants({"POKAMIND_BASE": "http://localhost:4000",
                                "EMORI_COURSE_ID": "course-a", "EMORI_LANG": "ca"})

    assert values == {"base": "http://localhost:4000", "course": "course-a", "lang": "ca"}


@pytest.mark.parametrize("app_origin, expected", [
    ("https://learn.example.test", ["https://api.pokamind.com",
                                    "https://learn.example.test"]),
    (None, ["https://api.pokamind.com", "https://app.pokamind.com"]),
    ("", ["https://api.pokamind.com", "https://app.pokamind.com"]),
    ("not an origin", ["https://api.pokamind.com", "https://app.pokamind.com"]),
])
def test_the_records_app_origin_replaces_the_hostname_guess(app_origin, expected):
    from emori_adapter.hostapp.tt_inject import target_origins

    assert target_origins("", "https://api.pokamind.com", app_origin) == expected


def test_explicit_target_origins_are_kept_beside_the_app_origin():
    from emori_adapter.hostapp.tt_inject import target_origins

    assert target_origins("https://extra.example.test", "https://api.pokamind.com",
                          "https://learn.example.test") == [
        "https://extra.example.test", "https://api.pokamind.com",
        "https://learn.example.test"]
