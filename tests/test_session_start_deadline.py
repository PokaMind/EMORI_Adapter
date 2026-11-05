"""A session that never starts must END, loudly, instead of holding the robot.

The wedge: a staff badge is scanned, `GET /session` loads, and the Pokamind iframe
never comes up. Everything after that page load happens cross-origin, so without these
fail-safes nothing adapter-side is watching -- the robot keeps the greeting's open
antennas, says nothing, ends nothing, and answers no gesture. Four halves are pinned here:

* the /session page's own deadline (it is the only thing that can see the frame),
* `POST /api/session-start-failed` -> `driver.abort_unstarted_session`, which hands back
  the training lease and the raised session mic gain,
* the adapter-side belt-and-braces for a kiosk that cannot even POST,
* the end GESTURE, which must have a dead-iframe fail-safe too and must not reset the
  walk-away clock, or nudging a stuck robot keeps it stuck.

The same node harness then carries the START-LATENCY work, because every one
of those three changes moves something that happens in the first second of a session and
must not disturb any of the four halves above:

* A  the landing page navigates IMMEDIATELY and /session speaks the covering line, so the
     announcement no longer sits in front of the load (3,158 ms measured),
* B  the menu mic flushes on the voiced->silence edge instead of a fixed 3.5 s timer
     (~1,750 ms measured), with the old timer kept as the backstop,
* C  a preconnect warms the Pokamind origins while the visitor chooses -- and nothing
     else: no pre-boot, no second sign-in, no second microphone.
"""

from __future__ import annotations

from collections import Counter
import json
import re
import shutil
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from emori_adapter.hostapp.extensions import HostExtension


@pytest.fixture(autouse=True)
def _badges_on_the_sample_roster(sample_roster):
    """These tests scan ``EMORI:anna-001``: the shipped roster example lists nobody, so the
    host app reads a copy of the test-only sample roster (tests/conftest.py)."""


def _driver():
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    return LiveDriver(DryRunRobotClient(), fiware_factory=None)


def _analysis_envelope() -> dict:
    """A body-bearing cycle -- the only thing that moves tracker.cycles off 0."""
    return {"type": "pokamind_analysis_update", "seq": 1,
            "body_language": {"posture": "upright", "engagement_score": 52.4},
            "body_language_metrics": {"engagement": 52.4, "confidence": 43.7},
            "communication_styles": {"neutral": 12.41, "steady": 7.81}}


# --- driver: the abort itself -------------------------------------------------------


def test_abort_unstarted_session_finalizes_once_and_is_idempotent():
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    try:
        driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))

        assert driver.abort_unstarted_session("session_never_started") is True
        assert driver.tracker.active is False
        assert driver.counters["session_start_failed"] == 1

        # The kiosk may POST twice (a retry, a second page). The second call must be a
        # no-op: by then the session it named is gone and another may have started.
        assert driver.abort_unstarted_session("session_never_started") is False
        assert driver.counters["session_start_failed"] == 1
    finally:
        driver.player.stop()
        driver.player.join()


def test_abort_refuses_a_session_that_has_already_exchanged_a_cycle():
    """cycles > 0 means a real conversation is happening in front of the robot."""
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    try:
        driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
        driver.on_analysis(driver.stamp(_analysis_envelope()))
        assert driver.tracker.cycles == 1

        assert driver.abort_unstarted_session("session_never_started") is False
        assert driver.tracker.active is True
        assert driver.counters.get("session_start_failed", 0) == 0
    finally:
        driver.player.stop()
        driver.player.join()


def test_abort_refuses_a_conversation_the_camera_stream_never_narrated():
    """cycles are fed ONLY by the camera analysis stream.

    The AI taking a turn is proof the iframe came up and the session is running, so a
    conversation whose camera stream produces nothing must not be read as "never
    started" -- that is a live session ended mid-sentence.
    """
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    try:
        driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
        driver.on_utterance(driver.stamp({"type": "pokamind_ai_utterance", "seq": 1,
                                          "text": "Bon dia, en què puc ajudar?"}))
        assert driver.tracker.cycles == 0
        assert driver.tracker.utterances == 1

        assert driver.abort_unstarted_session("session_never_started") is False
        assert driver.tracker.active is True
        assert driver.counters.get("session_start_failed", 0) == 0
    finally:
        driver.player.stop()
        driver.player.join()


# --- driver: the adapter-side belt-and-braces ---------------------------------------


def test_watchdog_ends_a_session_whose_iframe_never_came_up():
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    driver.touch()                       # activity fresh -> the walk-away timer is not due
    driver.session_started_at = time.monotonic() - 100.0

    ticker = WatchdogTicker(driver, tick_s=0.01, session_start_s=0.001,
                            walkaway_s=45.0, absence_s=0, end_grace_s=30.0)
    ticker.start()
    try:
        deadline = time.monotonic() + 2.0
        cmd = None
        while cmd is None and time.monotonic() < deadline:
            cmd = driver.pop_cmd()
            if cmd is None:
                time.sleep(0.005)
        assert cmd == "end"                                       # asked Pokamind first
        assert driver.counters.get("session_never_started", 0) == 1
        assert driver.counters.get("walkaway_ends", 0) == 0       # not the blind timer
        assert driver.end_requested_at is not None                # ...and armed the grace
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()


def test_the_walkaway_timer_never_kills_a_session_that_is_still_booting():
    """Regression: on a slow network the iframe's JS can take >45s, and a
    walk-away timer would kill every attempt at open+45s -- it would beat the
    (raised) session-start deadline because nothing touches the activity clock
    before the frame is alive. Nobody can walk away from a
    conversation that never started: while the start deadline is still
    running and the frame has not come up, the walk-away stands down and the
    start machinery reports the honest reason."""
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    driver.last_activity = time.monotonic() - 100.0     # the boot ate the budget
    driver.session_started_at = time.monotonic() - 50.0  # inside session_start_s

    ticker = WatchdogTicker(driver, tick_s=0.01, session_start_s=130.0,
                            walkaway_s=45.0, absence_s=0, end_grace_s=30.0)
    ticker.start()
    try:
        time.sleep(0.1)
        assert driver.pop_cmd() is None
        assert driver.counters.get("walkaway_ends", 0) == 0
        assert driver.tracker.active is True

        # The moment the frame is alive the walk-away is back in force (its
        # budget restarted by the frame-alive touch in the real flow; here the
        # clock is still stale, so it fires promptly and correctly).
        driver.frame_alive_at = time.monotonic()
        deadline = time.monotonic() + 2.0
        cmd = None
        while cmd is None and time.monotonic() < deadline:
            cmd = driver.pop_cmd()
            if cmd is None:
                time.sleep(0.005)
        assert cmd == "end"
        assert driver.counters.get("walkaway_ends", 0) == 1
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()


def test_a_live_frame_is_never_killed_for_being_slow_to_exchange():
    """Regression: the iframe is alive and posting, the page's deadline is
    disarmed, but `exchanged` (first analysis/AI line) has not arrived within
    session_start_s -- the ticker must not silently end a healthy boot with no
    log line.  A frame-alive mark (POST
    /api/frame-alive, mirrored from the page's own disarm) must suppress the
    session_never_started kill; every later fail-safe stays armed."""
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    driver.touch()
    driver.session_started_at = time.monotonic() - 100.0
    driver.frame_alive_at = time.monotonic()     # the page relayed a pokamind_* message

    ticker = WatchdogTicker(driver, tick_s=0.01, session_start_s=0.001,
                            walkaway_s=45.0, absence_s=0, end_grace_s=30.0)
    ticker.start()
    try:
        time.sleep(0.1)                          # many ticks past the deadline
        assert driver.pop_cmd() is None
        assert driver.counters.get("session_never_started", 0) == 0
        assert driver.tracker.active is True
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()


def test_a_new_session_starts_with_a_clean_frame_alive_mark():
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    try:
        driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
        driver.frame_alive_at = time.monotonic()
        assert driver.abort_unstarted_session("session_never_started") is True
        assert driver.frame_alive_at is None     # finalize clears it
        driver.start_session(SessionInfo(session_id="s2", person_id="p1", lang="en"))
        assert driver.frame_alive_at is None     # s1's mark cannot shield s2
    finally:
        driver.player.stop()
        driver.player.join()


@pytest.mark.parametrize("evidence", ["cycle", "utterance"])
def test_the_start_deadline_can_never_fire_once_the_session_has_exchanged_anything(
    evidence,
):
    """Either kind of traffic proves the iframe came up.

    ``cycles`` is the camera analysis stream alone, so a spoken turn has to count as
    well or this fail-safe ends audio-only conversations at 45 s.
    """
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    if evidence == "cycle":
        driver.on_analysis(driver.stamp(_analysis_envelope()))
    else:
        driver.on_utterance(driver.stamp({"type": "pokamind_ai_utterance", "seq": 1,
                                          "text": "Bon dia."}))
    driver.session_started_at = time.monotonic() - 100.0

    ticker = WatchdogTicker(driver, tick_s=0.01, session_start_s=0.001,
                            walkaway_s=0, absence_s=0, end_grace_s=30.0)
    ticker.start()
    try:
        time.sleep(0.15)
        assert driver.counters.get("session_never_started", 0) == 0
        assert driver.tracker.active is True
        assert driver.pop_cmd() is None
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()


# --- the end gesture: same grace clock, and no free recovery-clock reset ------------


class _GestureDriver:
    """The GestureTicker's collaborator surface (mirrors tests/test_gesture_begin.py)."""

    def __init__(self, *, cycles: int) -> None:
        self.counters: Counter = Counter()
        self.player = SimpleNamespace(busy=False)
        self.tracker = SimpleNamespace(active=True, cycles=cycles)
        self.cmds: list[str] = []
        self.motions: list[list] = []
        self.touched = 0
        self.grace_arms = 0

    def enqueue_cmd(self, cmd: str) -> None:
        self.cmds.append(cmd)

    def submit_legacy_motion(self, intents, *, drop_if_busy=False) -> bool:
        del drop_if_busy
        self.motions.append(list(intents))
        return True

    def arm_end_grace(self) -> None:
        self.grace_arms += 1

    def touch(self) -> None:
        self.touched += 1


class _AlwaysFire:
    def update(self, angles) -> bool:
        del angles
        return True

    def reset(self) -> None:
        pass


class _NeverDirection:
    def update(self, angles, *, armed=True):
        del angles, armed
        return None

    def reset(self) -> None:
        pass

    def take_vetoed_latch(self) -> bool:
        return False


def _gesture_ticker(driver):
    from emori_adapter.hostapp.driver import GestureTicker

    ticker = GestureTicker(driver, on_direction=lambda _d: None)
    ticker.detector = _AlwaysFire()
    ticker.direction_detector = _NeverDirection()
    return ticker


def test_end_gesture_before_the_first_cycle_arms_the_grace_and_leaves_the_clock_alone():
    """A nudge at a robot that never started is not evidence of a conversation.

    touch() there reset the 45 s walk-away clock, so the person trying to un-stick the
    robot was the reason the only recovery never fired.
    """
    driver = _GestureDriver(cycles=0)

    _gesture_ticker(driver).process_pose((0.5, 0.0, 0.0))

    assert driver.cmds == ["end"]
    assert driver.counters["gesture_end"] == 1
    assert driver.grace_arms == 1        # the dead-iframe fail-safe is now armed
    assert driver.touched == 0           # ...and the recovery clock was NOT reset
    assert len(driver.motions) == 1      # perk-antennas acknowledgement, unchanged


def test_end_gesture_mid_conversation_still_refreshes_activity():
    driver = _GestureDriver(cycles=3)

    _gesture_ticker(driver).process_pose((0.5, 0.0, 0.0))

    assert driver.cmds == ["end"]
    assert driver.grace_arms == 1
    assert driver.touched == 1


# --- server: the page's report ------------------------------------------------------


class _LeaseExtension(HostExtension):
    """Records the session release and keeps the liveness probe it is handed."""

    def __init__(self) -> None:
        self.ended: list[object] = []
        self.alive = None

    def attach(self, ctx) -> None:
        self.alive = ctx.session_alive

    def session_ended(self, session_id: object | None = None) -> None:
        self.ended.append(session_id)


class _RecordingMic:
    def __init__(self) -> None:
        self.profiles: list[str] = []

    def set_profile(self, profile: str) -> bool:
        self.profiles.append(profile)
        return True

    @property
    def profile(self) -> str | None:
        return self.profiles[-1] if self.profiles else None


@pytest.fixture()
def kiosk(monkeypatch):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    extension = _LeaseExtension()
    mic = _RecordingMic()
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "fiware_factory": None,
        "idle": False, "watchdog": False, "gesture": False,
        "extension": extension, "mic_profile": mic,
        "on_session_end": None, "tts": None, "stt": None,
    })
    client = TestClient(app)
    client.extension = extension
    client.mic = mic
    return client


def _open_badge_session(client) -> None:
    minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
    assert minted.status_code == 200
    assert client.get(minted.json()["session_url"]).status_code == 200


def test_frame_alive_refreshes_the_walkaway_clock(kiosk):
    """Regression: a slow Pokamind boot (a cold server + scenario generation) can
    mount the iframe at +39s or +44s, and a blind EMORI_WALKAWAY_S=45 inactivity
    timer would kill the session at exactly open+45s -- mid-intro, learner standing
    right there.
    The frame coming alive IS activity: POST /api/frame-alive must restart the
    walk-away budget, so the timer measures silence from the mount, not from
    the badge scan."""
    with kiosk as client:
        _open_badge_session(client)
        driver = client.app.state.driver
        driver.last_activity = time.monotonic() - 100.0   # the slow boot ate the budget

        assert client.post("/api/frame-alive").json() == {"ok": True}

        assert driver.frame_alive_at is not None
        assert time.monotonic() - driver.last_activity < 5.0
        # Only the FIRST alive mark refreshes: a live-but-stuck frame that keeps
        # posting must not postpone the walk-away fail-safe forever.
        driver.last_activity = time.monotonic() - 100.0
        assert client.post("/api/frame-alive").json() == {"ok": True}
        assert time.monotonic() - driver.last_activity > 50.0


def test_opening_stages_refresh_the_walkaway_clock(kiosk):
    """Regression: even measured from the mount, a cold Pokamind boot can eat the
    whole 45s walk-away budget (warmup-ready at +34s, intro still silent at +46s),
    and the timer must not kill a visibly progressing session. The
    one-shot opening/warmup stages the kiosk relays ARE that progress; each must
    refresh the activity clock. Per-frame telemetry must not, or a live session's
    walk-away detection dies."""
    with kiosk as client:
        _open_badge_session(client)
        driver = client.app.state.driver

        for stage in ("opening-mount", "warmup-start", "warmup-ready"):
            driver.last_activity = time.monotonic() - 100.0
            r = client.post("/api/emori-debug",
                            json={"type": "pokamind_emori_dbg", "stage": stage})
            assert r.json() == {"ok": True}
            assert time.monotonic() - driver.last_activity < 5.0, stage

        # High-rate per-frame VAD and non-lifecycle stages leave the clock alone.
        for body in ({"type": "pokamind_emori_vad", "prob": 0.4},
                     {"type": "pokamind_emori_dbg", "stage": "endpoint-fire"},
                     {"type": "pokamind_emori_dbg"}):
            driver.last_activity = time.monotonic() - 100.0
            assert client.post("/api/emori-debug", json=body).json() == {"ok": True}
            assert time.monotonic() - driver.last_activity > 50.0, body


def test_session_start_failed_hands_back_the_lease_and_the_mic(kiosk):
    with kiosk as client:
        _open_badge_session(client)
        driver = client.app.state.driver
        assert driver.tracker.active is True
        assert client.mic.profile == "session"

        response = client.post("/api/session-start-failed",
                               json={"reason": "session_never_started"})

        assert response.status_code == 202
        assert response.json() == {"aborted": True}
        assert driver.tracker.active is False
        # The two things a failed start leaves behind: an add-on's training lease
        # and the raised capture gain. Both are released by the ordinary finalize chain.
        assert client.extension.ended != []
        deadline = time.monotonic() + 2.0
        while client.mic.profile != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)      # the profile hand-back runs on its own thread
        assert client.mic.profile == "idle"


def test_session_start_failed_refuses_a_live_conversation_and_is_idempotent(kiosk):
    with kiosk as client:
        _open_badge_session(client)
        driver = client.app.state.driver
        client.post("/api/cycle", json=_analysis_envelope())
        assert driver.tracker.cycles == 1

        live = client.post("/api/session-start-failed", json={"reason": "whatever"})
        assert live.status_code == 202
        assert live.json() == {"aborted": False}
        assert driver.tracker.active is True

        # ...and a repeat against an already-ended session is equally harmless.
        assert driver.abort_unstarted_session("session_never_started") is False


def test_the_training_supervisor_reads_a_spoken_turn_as_alive(kiosk):
    """An add-on's supervisor force-idles when this says dead.

    It must therefore agree with the abort above about what "alive" means: the
    camera analysis stream is not the only proof a roleplay is running.
    """
    with kiosk as client:
        _open_badge_session(client)
        alive = client.extension.alive
        assert callable(alive)
        assert alive() is False       # opened, nothing exchanged: the wedge itself

        client.post("/api/cycle", json={"type": "pokamind_ai_utterance", "seq": 1,
                                        "text": "Bon dia."})

        assert alive() is True


def test_session_start_failed_never_logs_an_arbitrary_reason(kiosk, caplog):
    """The reason reaches a WARNING, so only a safe code may pass the boundary."""
    import logging

    with kiosk as client:
        _open_badge_session(client)
        with caplog.at_level(logging.WARNING, logger="emori.hostapp.driver"):
            client.post("/api/session-start-failed",
                        json={"reason": "Anna Example <anna@example.org> never started"})

    assert "anna@example.org" not in caplog.text
    assert "session_never_started" in caplog.text


# --- the page halves ----------------------------------------------------------------


def test_session_page_arms_the_start_deadline_and_speaks_when_it_fires(monkeypatch, kiosk):
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    # The deadline is armed when the frame is appended and disarmed by the FIRST
    # pokamind_* message -- never by onload, which fires for a blank error document too.
    assert "armSessionStartDeadline();" in html
    assert "noteFrameAlive();" in html
    assert "/api/session-start-failed" in html
    # A mute reset is the one thing this must never be.
    assert "goHome(why, S.session_start_failed);" in html

    with kiosk as client:
        _open_badge_session(client)
        rendered = client.get("/session").text
    assert "const SESSION_START_MS=45000;" in rendered
    assert "__SESSION_START_MS__" not in rendered


def test_the_page_deadline_is_env_tunable_and_can_be_switched_off(monkeypatch):
    from emori_adapter.hostapp.server import _session_start_ms

    monkeypatch.delenv("EMORI_SESSION_START_MS", raising=False)
    assert _session_start_ms() == 45000
    monkeypatch.setenv("EMORI_SESSION_START_MS", "20000")
    assert _session_start_ms() == 20000
    monkeypatch.setenv("EMORI_SESSION_START_MS", "0")
    assert _session_start_ms() == 0
    # A misconfigured value must not silently disable the fail-safe.
    for bad in ("abc", "10", "9999999"):
        monkeypatch.setenv("EMORI_SESSION_START_MS", bad)
        assert _session_start_ms() == 45000


def test_say_arms_its_fail_safe_before_the_fetch_and_bounds_the_request():
    """A wedged /api/tts stopped the kiosk dead: no speech AND no callback.

    Every caller here only advances inside that callback (listen after the menu,
    startCam after the give-up line, go home after the completion line).
    """
    from emori_adapter.hostapp import pages

    js = pages._SPEECH_JS
    body = js[js.index("function say(text,cb,onstart){"):]
    pre_arm = body.index("arm(Math.max(sayMaxMs(text),8000)+3000);")
    assert pre_arm < body.index("fetch('/api/tts")
    assert "AbortSignal.timeout(8000)" in body


def test_say_never_speaks_after_its_own_fail_safe_already_advanced_the_caller():
    """sayMaxMs floors at 5000, so a short line pre-armed at exactly the 8 s abort.

    The pre-arm was registered first and won that race, so the caller advanced and
    the fetch's own handlers then started speaking over the next state. Both the
    ordering and the shared latch are pinned here.
    """
    from emori_adapter.hostapp import pages

    js = pages._SPEECH_JS
    body = js[js.index("function say(text,cb,onstart){"):]
    # The latch every path consults, exposed on the callback itself.
    assert "f.done=false; return f;" in body
    # Neither branch of the fetch may produce sound once it is set -- nor once stopSpeech()
    # has asked for silence, which is the other half of the same guard (see below).
    assert body.count("if(fin.done||gen!==__sayGen) return;") == 2
    assert body.index("if(fin.done||gen!==__sayGen) return;") < body.index("new Audio(")
    assert body.index("if(fin.done||gen!==__sayGen) return;   // ...browser") < body.index(
        "new SpeechSynthesisUtterance(text)"
    )


def test_stop_speech_silences_a_line_that_has_not_started_talking_yet():
    """stopSpeech() must silence an unstarted say() too.

    It pauses the <audio> elements that EXIST; a line still waiting on /api/tts had none,
    its fetch kept running, and the .then then opened a new <audio> and started talking
    AFTER the page asked for silence. Every previous caller navigated immediately (the
    document died first), so it never showed. The session page's covering line is silenced
    and the page STAYS -- so the request has to be cancellable, and it is: stopSpeech bumps
    a generation that both fetch handlers check.
    """
    from emori_adapter.hostapp import pages

    js = pages._SPEECH_JS
    assert "function stopSpeech(){\n  __sayGen++;" in js
    say = js[js.index("function say(text,cb,onstart){"):]
    assert "const gen=__sayGen;" in say
    # The caller still advances: the generation guard suppresses SOUND, never the callback,
    # which arrives from say()'s own arm() deadline exactly as it did before.
    assert "cb&&cb();" in say
    # ...so the fail-safe that produces it is still armed BEFORE the fetch, unguarded.
    assert say.index("arm(Math.max(sayMaxMs(text),8000)+3000);") \
        < say.index("if(fin.done||gen!==__sayGen) return;")


def test_every_qr_rejection_is_visible_and_never_carries_the_payload():
    from emori_adapter.hostapp import pages

    js = pages._QR_SCAN_JS
    assert "function qrReject(code,text)" in js
    assert "type:'emori_qr_reject',code:code" in js
    # The debug event carries the SERVER's content-free error code and nothing else.
    assert "payload" not in js[js.index("function qrReject("):js.index("function qrLadder(")]
    # ...and the server-side decoder now leaves the same trace the in-page one does.
    assert "qrTel({pass:'zxing'" in js
    for html in (pages.VOICE_HTML, pages.IDENTIFY_HTML):
        assert "qrReject(" in html


# --- the auth-failure report: the one pokamind_* that is NOT proof of life -----------
#
# Pokamind's partner-SSO callback posts {type:'pokamind_auth_failed',code} when it cannot
# sign a badge in.  The page's frame-alive rule is a bare PREFIX match ("ANY pokamind_*
# message proves the frame came up"), so without an explicit intercept that message would
# call noteFrameAlive() -- clearing the start deadline, POSTing /api/frame-alive to disarm
# the adapter-side watchdog, and latching frameSpoke so sessionStartFailed() early-returns
# forever.  The failure report would SUPPRESS the failure it reports, turning a bounded
# 45 s ending into a hang until the walk-away path mislabels it.  Pinned here both in the
# source (the intercept is FIRST) and by running the real page script under node.


def _listener_body() -> str:
    """The session page's `message` listener, source only (no node required)."""
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    start = html.index("addEventListener('message',e=>{")
    return html[start:html.index("// EMORI screenless session control", start)]


def test_the_auth_failure_intercept_runs_before_the_frame_alive_prefix_match():
    body = _listener_body()
    intercept = body.index("if(d.type==='pokamind_auth_failed')")
    prefix_match = body.index("d.type.indexOf('pokamind_')===0")
    assert intercept < prefix_match, "auth failure must not fall through to noteFrameAlive"
    # ...and it must leave the listener, so it can never reach the relay below either.
    assert "sessionStartFailed(authFailCode(d)); return; }" in body


def test_the_auth_failure_code_is_built_to_the_hostapps_own_grammar():
    """The sanitiser and the validator that accepts its output must agree.

    A code the server refuses is silently rewritten to `session_never_started` -- an auth
    failure would then be recorded as a timeout, which is the wrong diagnosis.
    """
    from emori_adapter.hostapp import pages
    from emori_adapter.hostapp.server import _SAFE_STATUS_CODE_RE

    js = pages.SESSION_HTML
    helper = js[js.index("function authFailCode("):js.index("function armSessionStartDeadline(")]
    assert "typeof raw==='string'" in helper      # a non-string never reaches the slug
    assert "replace(/[^a-z0-9_]/g,'')" in helper  # ...and nothing but the slug alphabet does
    assert ".slice(0,64)" in helper               # the validator's length bound
    # The bare fallback (no code at all) is itself a valid, honest code.
    assert _SAFE_STATUS_CODE_RE.fullmatch("auth_failed")


# --- the same page script, RUN, with the real messages ------------------------------
# The source assertions above cannot see an ordering the engine actually takes, so the
# rendered page's script is executed under node against a DOM small enough to be a
# fixture and fed real postMessage payloads.  Every fetch it makes is recorded: that is
# how "did it disarm the watchdog?" and "did it report the failure?" are observed.
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_PAGE_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const script=fs.readFileSync(process.argv[2],'utf8');
const spec=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
// `audio` is the covering line's lifecycle as the page's own speech ladder drives it:
// 'play' when an <audio> starts, 'pause' when stopSpeech() silences one. Nothing else in
// this page can pause an element, so 'play' without 'pause' IS "the line finished".
const out={fetches:[],status:[],href:null,threw:[],audio:[]};

const realTimeout=global.setTimeout;
// Every page timer is unref'd (the start deadline is 45 s) so the harness owns the exit;
// the one ref'd timer is the settle below.
global.setTimeout=(fn,ms)=>{ const t=realTimeout(fn,ms); if(t&&t.unref) t.unref(); return t; };
global.setInterval=()=>0;            // the /api/pending-cmd poller: not under test

const el=()=>({textContent:'',style:{},attrs:{},appendChild(){},addEventListener(){},
  setAttribute(k,v){this.attrs[k]=v;},removeAttribute(){},load(){},pause(){}});
global.status={_t:'',get textContent(){return this._t;},
  set textContent(v){this._t=String(v); out.status.push(this._t);}};
// `out.order` interleaves the frame append with every fetch, which is the only way to
// see that the covering line (A) is a passenger on the load rather than a gate in front
// of it. Additive: `out.fetches` still records exactly what it always did.
out.order=[]; out.appended=[];
global.stage={appendChild(f){ const src=String((f&&f.src)||'');
  out.appended.push(src); out.order.push('frame:'+src); }};
global.document={createElement:()=>el(),getElementById:()=>el(),addEventListener(){},
  querySelectorAll:()=>[],body:el()};
global.location={search:'?sso=https://sso.test/auth/partner-callback/emori-robot',
  get href(){return out.href;},
  set href(v){ if(out.href===null) out.href=String(v); }};
const listeners={};
global.addEventListener=(t,fn)=>{ (listeners[t]=listeners[t]||[]).push(fn); };
global.window=global;
global.fetch=(url,opts)=>{ let body=null;
  try{ body=(opts&&opts.body)?JSON.parse(opts.body):null; }catch(e){ body=String((opts&&opts.body)||''); }
  out.fetches.push({url:String(url),method:(opts&&opts.method)||'GET',body:body});
  out.order.push('fetch:'+String(url).split('?')[0]);
  // Piper is 503 by default (the dev box has no voice), which routes say() into its
  // browser-TTS fallback and completes it instantly. `ttsWav` opts INTO the real path --
  // an <audio> element with a playing/paused lifecycle -- which is the only way to observe
  // whether a line was cut off or allowed to finish.
  if(spec.ttsWav && String(url).indexOf('/api/tts')===0)
    return Promise.resolve({ok:true,status:200,json:async()=>({}),blob:async()=>({})});
  return Promise.resolve({ok:false,status:503,json:async()=>({}),blob:async()=>({})}); };
global.speechSynthesis={cancel(){},speak(u){ if(u&&u.onstart) u.onstart();
  if(u&&u.onend) u.onend(); }};
global.SpeechSynthesisUtterance=function(t){ this.text=t; };
global.Audio=function(src){ const a=el(); a.src=String(src||'');
  a.play=()=>{ out.audio.push('play'); if(a.onplay) a.onplay(); return Promise.resolve(); };
  a.pause=()=>{ out.audio.push('pause'); };
  return a; };
global.URL={createObjectURL:()=>'blob:x',revokeObjectURL(){}};

// Payloads JSON cannot express: the shapes a hostile or broken sender can still produce.
const expand=(m)=>{
  if(m==='__UNDEFINED__') return undefined;
  if(m==='__STRING__') return 'pokamind_auth_failed';
  if(m==='__NUMBER__') return 42;
  if(m==='__ARRAY__') return ['pokamind_auth_failed'];
  if(m==='__THROWING_CODE__') return {type:'pokamind_auth_failed',
    get code(){ throw new Error('boom'); }};
  if(m==='__THROWING_TOSTRING__') return {type:'pokamind_auth_failed',
    code:{toString(){ throw new Error('boom'); }}};
  if(m==='__THROWING_STAGE__') return {type:'pokamind_emori_dbg',
    get stage(){ throw new Error('boom'); }};
  return m;
};

vm.runInThisContext(script);
(async()=>{
  for(const raw of spec.messages){
    // "__WAIT__:1200" advances REAL time between messages: the covering line's interlock
    // is a decision about how long the line has been audible, so a message list with no
    // clock cannot express the case it guards.
    if(typeof raw==='string' && raw.indexOf('__WAIT__:')===0){
      await new Promise(r=>realTimeout(r,parseInt(raw.split(':')[1],10)||0)); continue; }
    for(const fn of (listeners['message']||[])){
      try{ fn({data:expand(raw),origin:'https://app.pokamind.test'}); }
      catch(e){ out.threw.push(String((e&&e.message)||e)); }
    }
  }
  await new Promise(r=>realTimeout(r,700));   // let say()/goHome finish their ladder
  console.log(JSON.stringify(out));
})().catch(e=>{ console.error((e&&e.stack)||String(e)); process.exit(3); });
"""


def _run_page(tmp_path, rendered: str, messages: list, **opts) -> dict:
    body = rendered[rendered.index("<script>") + len("<script>"):rendered.rindex("</script>")]
    script = tmp_path / "session.js"
    script.write_text(body, encoding="utf-8")
    spec = tmp_path / "messages.json"
    spec.write_text(json.dumps({"messages": messages, **opts}), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_PAGE_HARNESS, encoding="utf-8")
    run = subprocess.run(
        [shutil.which("node"), harness.as_posix(), script.as_posix(), spec.as_posix()],
        # encoding= is not optional: the page carries Catalan strings and the Windows
        # default codepage would mangle them before the assertions see them.
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _urls(report: dict) -> list[str]:
    return [f["url"].split("?")[0] for f in report["fetches"]]


def _reported(report: dict) -> list[str]:
    return [f["body"]["reason"] for f in report["fetches"]
            if f["url"] == "/api/session-start-failed" and isinstance(f["body"], dict)]


@pytest.fixture()
def rendered_session(kiosk) -> str:
    with kiosk as client:
        _open_badge_session(client)
        return client.get("/session").text


@node
def test_auth_failed_ends_the_session_and_never_counts_as_frame_alive(tmp_path,
                                                                     rendered_session):
    report = _run_page(tmp_path, rendered_session,
                       [{"type": "pokamind_auth_failed", "code": "invalid_token",
                         "detail": "auth/invalid-custom-token"}])

    assert report["threw"] == []
    # The failure is REPORTED, with its own code -- not swallowed, not a timeout.
    assert _reported(report) == ["auth_failed_invalid_token"]
    # ...and the two disarms that would have made the report unactionable never happen.
    assert "/api/frame-alive" not in _urls(report)
    assert "/api/cycle" not in _urls(report)
    # The ending is spoken and the kiosk goes home: a mute reset is what this must not be.
    assert "/api/tts" in _urls(report)
    assert report["href"] == "/"


@node
@pytest.mark.parametrize("kind", ["pokamind_tts_ended", "pokamind_emori_dbg",
                                  "pokamind_analysis_update"])
def test_every_other_pokamind_message_still_proves_the_frame_is_alive(tmp_path,
                                                                     rendered_session,
                                                                     kind):
    """The regression the intercept must not cause: it is one type, not a category."""
    report = _run_page(tmp_path, rendered_session, [{"type": kind, "seq": 1}])

    assert report["threw"] == []
    assert "/api/frame-alive" in _urls(report)
    assert _reported(report) == []
    assert report["href"] is None                     # the session is still running


@node
def test_a_late_auth_failure_cannot_kill_a_frame_that_already_came_up(tmp_path,
                                                                     rendered_session):
    """frameSpoke is the page's own "a real session is running" latch.

    Once it is set the failure path stands down -- and the server's
    ``abort_unstarted_session`` refuses an exchanged session behind it as well.
    """
    report = _run_page(tmp_path, rendered_session,
                       [{"type": "pokamind_analysis_update", "seq": 1},
                        {"type": "pokamind_auth_failed", "code": "signin_failed"}])

    assert report["threw"] == []
    assert "/api/frame-alive" in _urls(report)
    assert _reported(report) == []
    assert report["href"] is None


@node
@pytest.mark.parametrize("code,expected", [
    ("no_token", "auth_failed_no_token"),
    ("invalid_token", "auth_failed_invalid_token"),
    ("signin_failed", "auth_failed_signin_failed"),
    ("auth_error", "auth_failed_auth_error"),
    ("Anna Example <anna@example.org>", "auth_failed_annaexampleannaexampleorg"),
    ("../../etc/passwd", "auth_failed_etcpasswd"),
    ("x" * 400, "auth_failed_" + "x" * 52),
    ("", "auth_failed"),
    ("!!!", "auth_failed"),
])
def test_the_reported_code_is_always_one_the_server_accepts(tmp_path, rendered_session,
                                                            code, expected):
    """Sanitised in the page, and accepted verbatim by the route that records it.

    The code reaches a request body, a WARNING log line and the on-screen status, and it
    arrives by postMessage -- so it is attacker-influenceable in principle.
    """
    from emori_adapter.hostapp.server import _SAFE_STATUS_CODE_RE

    report = _run_page(tmp_path, rendered_session,
                       [{"type": "pokamind_auth_failed", "code": code}])

    assert report["threw"] == []
    assert _reported(report) == [expected]
    # The whole point of the grammar: the server takes this code as-is.
    assert _SAFE_STATUS_CODE_RE.fullmatch(expected), expected
    # ...and nothing the message carried survived except the slug alphabet.
    assert not re.search(r"[^a-z0-9_]", expected)


@node
def test_a_hostile_or_malformed_message_can_never_throw_inside_the_listener(tmp_path,
                                                                           rendered_session):
    """One throw here kills the listener for the rest of the session.

    The same handler carries the relay, the auto-begin and the VAD telemetry, so an
    exception on a junk message would silently end the whole conversation.
    """
    report = _run_page(tmp_path, rendered_session, [
        None, "__UNDEFINED__", "__STRING__", "__NUMBER__", "__ARRAY__",
        {}, {"type": 42}, {"type": "pokamind_auth_failed"},
        {"type": "pokamind_auth_failed", "code": 42},
        {"type": "pokamind_auth_failed", "code": None},
        {"type": "pokamind_auth_failed", "code": {"nested": True}},
        "__THROWING_CODE__", "__THROWING_TOSTRING__",
    ])

    assert report["threw"] == []
    # Every one of them degrades to the bare, honest code -- and the first one to arrive
    # is the only one that acts (the page latches, exactly as the deadline path does).
    assert _reported(report) == ["auth_failed"]
    assert "/api/frame-alive" not in _urls(report)


def test_the_sanitised_code_survives_the_route_that_records_it(kiosk, caplog):
    """End to end: the exact string the page sends is the string the driver logs."""
    import logging

    with kiosk as client:
        _open_badge_session(client)
        with caplog.at_level(logging.WARNING, logger="emori.hostapp.driver"):
            response = client.post("/api/session-start-failed",
                                   json={"reason": "auth_failed_invalid_token"})

    assert response.status_code == 202
    assert response.json() == {"aborted": True}
    # NOT degraded to session_never_started: an auth failure is not a timeout.
    assert "auth_failed_invalid_token" in caplog.text


# ====================================================================================
# START LATENCY.  From the visitor picking a topic to the first scenario word, three
# stages were the robot's own dead time.
# ====================================================================================


def _pick_a_topic(client) -> dict:
    """A real menu answer -> the session URL it mints, topic and all."""

    picked = client.post("/api/voice-select", json={"text": "one"})
    assert picked.status_code == 200, picked.text
    body = picked.json()
    assert body["matched"] and body["exact"] and body["topic"]
    return body


@pytest.fixture()
def picked_session(kiosk):
    """The rendered /session page for a session someone actually CHOSE a topic for.

    ``rendered_session`` above opens by badge, whose roster module is not on the live
    menu, so its topic -- and therefore its covering line -- is empty by design.
    """
    with kiosk as client:
        body = _pick_a_topic(client)
        return client.get(body["session_url"]).text, body["topic"]


# --- A: navigate first, speak over the load -----------------------------------------


def test_the_covering_line_is_rendered_per_language_and_names_the_topic():
    from emori_adapter.hostapp import pages

    en = pages.start_cover_line("en", "Difficult Conversations at Work")
    ca = pages.start_cover_line("ca", "Converses difícils")
    assert en.startswith("Starting Difficult Conversations at Work.")
    assert ca.startswith("Comencem amb Converses difícils.")
    # Longer than the bare "Starting <topic>" it replaces: with the load overlapped the
    # extra seconds fill dead air instead of adding to it.
    assert len(en) > len("Starting Difficult Conversations at Work") + 20
    assert len(ca) > len("Comencem amb Converses difícils") + 20
    # Both kiosk languages, and an unknown one degrades to English rather than to silence.
    assert set(pages.START_COVER) == {"en", "ca"}
    assert pages.start_cover_line("de", "X") == pages.start_cover_line("en", "X")
    # No topic = no announcement: the badge-direct and walk-in paths never had one, and
    # inventing a sentence there would be new behaviour, not a latency fix.
    assert pages.start_cover_line("en", "") == ""
    assert pages.start_cover_line("ca", "   ") == ""


def test_the_session_page_carries_the_line_for_the_topic_that_was_picked(picked_session):
    rendered, topic = picked_session
    assert "__START_LINE__" not in rendered
    assert f'const START_LINE="Starting {topic}.' in rendered


def test_a_session_nobody_picked_a_topic_for_stays_silent(kiosk):
    with kiosk as client:
        _open_badge_session(client)
        rendered = client.get("/session").text
    assert 'const START_LINE="";' in rendered


@node
def test_the_covering_line_never_gates_the_frame_load(tmp_path, picked_session):
    """A: the announcement must be a passenger on the load, not a gate in front of it.

    The landing page does not make navigation the say() completion callback. Here the
    page that speaks is the one that ALSO owns the iframe, so the ordering is what
    proves the load is not waiting: the frame is appended and the 45 s deadline armed
    before a single byte of TTS is asked for.
    """
    rendered, topic = picked_session
    report = _run_page(tmp_path, rendered, [])

    assert report["threw"] == []
    assert report["appended"] != []                      # the Pokamind frame went in
    tts = [u for u in report["order"] if u == "fetch:/api/tts"]
    assert tts, "the covering line was never spoken"
    frame_at = next(i for i, ev in enumerate(report["order"]) if ev.startswith("frame:"))
    assert frame_at < report["order"].index("fetch:/api/tts")
    # ...and it really is THIS session's line, in this session's language.
    said = [f["url"] for f in report["fetches"] if f["url"].startswith("/api/tts")]
    assert quote(topic) in said[0]
    assert "lang=en" in said[0]


@node
def test_the_covering_line_leaves_the_start_deadline_and_the_auth_intercept_alone(
    tmp_path, picked_session
):
    """The session-start fail-safes still hold with the covering line in place."""
    rendered, _topic = picked_session

    # 1. A live frame still disarms both halves of the deadline.
    alive = _run_page(tmp_path, rendered, [{"type": "pokamind_analysis_update", "seq": 1}])
    assert alive["threw"] == []
    assert "/api/frame-alive" in _urls(alive)
    assert _reported(alive) == []

    # 2. An auth failure is still reported, still never counts as frame-alive, and the
    #    covering line does not survive into the ending (goHome stopSpeech()es first).
    failed = _run_page(tmp_path, rendered,
                       [{"type": "pokamind_auth_failed", "code": "invalid_token"}])
    assert failed["threw"] == []
    assert _reported(failed) == ["auth_failed_invalid_token"]
    assert "/api/frame-alive" not in _urls(failed)
    assert failed["href"] == "/"


def test_the_page_still_arms_the_deadline_before_it_ever_speaks():
    """Source-level companion: `say` is the LAST statement, after the arm and the append."""
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    speak = html.index("if(START_LINE) say(START_LINE,()=>coverHold(false),coverAudible);")
    assert html.index("armSessionStartDeadline();") < speak
    assert html.index("stage.appendChild(f);") < speak
    assert html.index("addEventListener('message',e=>{") < speak


# --- the covering line must STOP when Pokamind starts talking ------------------------
#
# The announcement was moved onto the /session page, over the load -- and nothing was
# left to end it. The line is ~6 s and the load is ~16 s, so it normally finishes first; but a
# warm frame (or a shorter module title) puts the robot's own "Starting X..." on the same
# single speaker as the scenario's opening words. Current Pokamind web app versions emit an
# opening trace on the EXISTING pokamind_emori_dbg channel, and 'opening-intro-first-play' fires when the
# scenario's first audio actually starts playing: the hand-over signal this page needs.
#
# The three properties that make it safe to act on a message from a cross-origin frame:
# it is an EXACT stage match (the auth intercept and the frame-alive prefix rule above it
# are untouched, and every other stage still just relays), its ABSENCE changes nothing,
# and it can never leave the visitor with a clipped word.


def test_the_interlock_is_an_exact_stage_match_and_leaves_the_other_rules_alone():
    from emori_adapter.hostapp import pages

    html = pages.SESSION_HTML
    body = _listener_body()
    # An exact stage, never a prefix: 'opening-mount' / 'opening-latency' and every other
    # debug stage must stay pure relay traffic.
    assert "m.stage==='opening-intro-first-play'" in html
    assert "stage.indexOf('opening" not in html
    assert "if(isOpeningFirstPlay(d)) hushCover();" in body
    # ORDER: the auth intercept still runs first, the frame-alive prefix rule still runs
    # before the debug branch, and the interlock lives INSIDE that branch.
    assert body.index("if(d.type==='pokamind_auth_failed')") \
        < body.index("d.type.indexOf('pokamind_')===0") \
        < body.index("if(d.type==='pokamind_emori_dbg')") \
        < body.index("if(isOpeningFirstPlay(d)) hushCover();")
    # ...and the stage is read behind a guard, like authFailCode: this listener also
    # carries the relay, the auto-begin and the VAD telemetry.
    assert "function isOpeningFirstPlay(m){\n  try{" in html


@node
def test_pokamind_starting_to_speak_silences_the_covering_line(tmp_path, picked_session):
    """The case that matters: two voices on the robot's one speaker."""
    rendered, _topic = picked_session
    report = _run_page(tmp_path, rendered, [
        {"type": "pokamind_emori_dbg", "stage": "opening-mount"},
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-requested"},
        "__WAIT__:1300",                       # the line has been talking for a while
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-first-play"},
    ], ttsWav=True)

    assert report["threw"] == []
    # It was speaking, and it stopped -- pausing the element is the only thing that
    # silences a Piper WAV (speechSynthesis.cancel() does not).
    assert report["audio"] == ["play", "pause"]
    # ...and the frame is still a live session: nothing was ended, nothing navigated.
    assert "/api/frame-alive" in _urls(report)
    assert _reported(report) == []
    assert report["href"] is None
    # The debug relay is untouched: every opening stage still reaches the hostapp sink.
    assert len([f for f in report["fetches"] if f["url"] == "/api/emori-debug"]) == 3


@node
def test_a_frame_that_never_reports_first_play_leaves_todays_behaviour(tmp_path,
                                                                      picked_session):
    """Robustness: an older frontend, or a module whose intro never plays.

    Nothing calls the interlock and the line simply finishes, exactly as it does today.
    """
    rendered, _topic = picked_session
    report = _run_page(tmp_path, rendered, [
        {"type": "pokamind_emori_dbg", "stage": "opening-mount"},
        {"type": "pokamind_emori_dbg", "stage": "opening-warmup-trigger"},
        {"type": "pokamind_emori_dbg", "stage": "opening-endpointer-ready"},
        {"type": "pokamind_emori_dbg", "stage": "opening-latency", "ms": 15900},
        "__WAIT__:1300",
        {"type": "pokamind_tts_ended", "seq": 1},
    ], ttsWav=True)

    assert report["threw"] == []
    assert report["audio"] == ["play"]              # started, never silenced
    assert "/api/frame-alive" in _urls(report)
    assert report["href"] is None


@node
def test_an_early_first_play_never_leaves_a_clipped_word(tmp_path, picked_session):
    """The deliberate choice, both halves of it.

    A first-play inside the first second of the line being AUDIBLE is not honoured: under
    a second Piper has emitted a syllable or two, so cutting produces "Star--" and then
    silence -- on a screenless robot that is indistinguishable from a crash, and it would
    happen on every affected session. Overlapping the remainder of one bounded sentence is
    the lesser harm, and a first-play that early is not physically the ~16 s load this line
    covers anyway. Before the line is audible there is nothing to clip, so THAT case is
    cancelled outright -- which requires stopSpeech() to be able to cancel a say() whose
    WAV has not arrived yet.
    """
    rendered, _topic = picked_session

    # (a) audible for ~50 ms when the trace lands: it finishes.
    early = _run_page(tmp_path, rendered, [
        "__WAIT__:60",
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-first-play"},
        "__WAIT__:1300",
        # ...and the decision is made ONCE: a duplicate trace cannot re-open it later.
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-first-play"},
    ], ttsWav=True)
    assert early["threw"] == []
    assert early["audio"] == ["play"]

    # (b) not audible yet (Piper still synthesising): cancelled, so it never starts at all.
    unstarted = _run_page(tmp_path, rendered, [
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-first-play"},
        "__WAIT__:400",
    ], ttsWav=True)
    assert unstarted["threw"] == []
    assert unstarted["audio"] == []
    assert "/api/tts" in _urls(unstarted)      # it was ASKED for, and then dropped


@node
def test_a_hostile_opening_trace_can_never_throw_or_silence_the_line(tmp_path,
                                                                     picked_session):
    """The stage arrives by postMessage, so in principle any frame can author it.

    One throw here kills the listener for the rest of the session (the relay, the
    auto-begin and the VAD telemetry all live in it), and a near-miss stage must not be
    able to take the speaker away from a line that is legitimately talking.
    """
    rendered, _topic = picked_session
    report = _run_page(tmp_path, rendered, [
        "__WAIT__:1300",
        {"type": "pokamind_emori_dbg", "stage": "opening-intro-first-playing"},
        {"type": "pokamind_emori_dbg", "stage": "OPENING-INTRO-FIRST-PLAY"},
        {"type": "pokamind_emori_dbg", "stage": 42},
        {"type": "pokamind_emori_dbg", "stage": {"nested": True}},
        {"type": "pokamind_emori_dbg"},
        "__THROWING_STAGE__",
    ], ttsWav=True)

    assert report["threw"] == []
    assert report["audio"] == ["play"]          # not one of them silenced the line
    assert "/api/frame-alive" in _urls(report)
    assert _reported(report) == []


# --- B: flush the menu mic on end-of-speech -----------------------------------------
#
# The landing page's capture chain cannot be reasoned about from source alone: the
# question is WHEN a buffer reaches Vosk, which is a property of the run.  So the same
# treatment the /session page gets -- run the REAL rendered script under a DOM shim -- is
# applied to the menu page, with a fake AudioContext whose ScriptProcessor this harness
# owns.  Audio is fed as whole 4096-sample frames of alternating +/-amp (so the RMS the
# page measures IS amp), and every fetch is stamped with how much audio had been fed when
# it happened.  4096 @ 48 kHz = 85.33 ms of wall audio and 80 ms of 20 ms RMS frames after
# the page's own decimation to 16 kHz, which is why the expectations below are ranges.

_MENU_HARNESS = r"""
const fs=require('fs'), vm=require('vm');
const script=fs.readFileSync(process.argv[2],'utf8');
const spec=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const RATE=spec.sampleRate||48000, FRAME=spec.frame||4096, FRAME_MS=FRAME/RATE*1000;
const out={fetches:[],created:[],intervals:[],gum:[],threw:[],fedMs:0,
           href:null,capture:null,hasProc:false};

// Node >= 21 predefines `navigator` and `performance` as NON-WRITABLE globals, so a plain
// `global.navigator = {...}` fails SILENTLY in sloppy mode: the page then sees the real
// Node navigator, getUserMedia throws, and both `startCam` and `startServerListen` fall
// into their catch branches -- a harness that measures nothing while looking green.
const def=(k,v)=>Object.defineProperty(global,k,
  {value:v,writable:true,configurable:true,enumerable:true});

const realTimeout=global.setTimeout;
def('setTimeout',(fn,ms)=>{ const t=realTimeout(fn,ms); if(t&&t.unref) t.unref(); return t; });
const intervals=[];
def('setInterval',(fn,ms)=>{ intervals.push({fn:fn,ms:ms,cleared:false});
  out.intervals.push(ms); return intervals.length; });
def('clearInterval',(id)=>{ if(intervals[id-1]) intervals[id-1].cleared=true; });
def('requestAnimationFrame',()=>0);      // the QR scan loop is not under test
def('jsQR',()=>null);

const ctx2d={drawImage(){},getImageData:()=>({data:new Uint8ClampedArray(4)})};
const els={};
const el=(id)=>({id:id,textContent:'',innerHTML:'',className:'',style:{},srcObject:null,
  readyState:1,videoWidth:0,videoHeight:0,onclick:null,
  appendChild(){},addEventListener(){},setAttribute(){},removeAttribute(){},
  load(){},pause(){},play(){return Promise.resolve();},toBlob(){},getContext:()=>ctx2d});
def('document',{createElement:(tag)=>{ out.created.push(String(tag)); return el(tag); },
  getElementById:(id)=>(els[id]=els[id]||el(id)), addEventListener(){},
  querySelectorAll:()=>[], body:el('body')});
def('location',{search:'',reload(){},
  get href(){return out.href;},
  set href(v){ if(out.href===null) out.href=String(v); }});
def('addEventListener',()=>{});
def('window',global);
def('performance',{now:()=>Date.now()});

const audioTrack={getSettings:()=>({echoCancellation:true}),stop(){}};
const stream={getTracks:()=>[audioTrack],getAudioTracks:()=>[audioTrack]};
def('navigator',{sendBeacon:()=>true, mediaDevices:{ getUserMedia:(cs)=>{
  const kind=(cs&&cs.audio)?'audio':'video';
  out.gum.push(kind);
  // No camera in the harness: the scan loop is not what is being measured, and a
  // rejection is the branch that leaves the page sitting on the verbal menu.
  if(kind==='video') return Promise.reject(new Error('no camera'));
  return Promise.resolve(stream); } }});

let proc=null, lastHandler=null;
def('AudioContext',function(){ this.sampleRate=RATE; this.destination={};
  this.createMediaStreamSource=()=>({connect(){}});
  this.createScriptProcessor=()=>{ let h=null;
    proc={connect(){},disconnect(){},
      get onaudioprocess(){ return h; },
      // `lastHandler` REMEMBERS the callback even after the page detaches it. A real
      // ScriptProcessor's audio thread can deliver an event it queued before the
      // disconnect, and the JS engine holds the closure, not the property -- that late
      // delivery is what `orphan` steps below replay.
      set onaudioprocess(fn){ h=fn; if(fn) lastHandler=fn; }};
    return proc; };
  this.close=()=>{}; });

def('fetch',(url,opts)=>{ const u=String(url), method=(opts&&opts.method)||'GET';
  let body=null, bytes=0;
  try{ if(opts&&typeof opts.body==='string') body=JSON.parse(opts.body); }catch(e){}
  try{ if(opts&&opts.body&&opts.body.byteLength!==undefined) bytes=opts.body.byteLength; }catch(e){}
  out.fetches.push({url:u,method:method,body:body,bytes:bytes,fedMs:Math.round(out.fedMs)});
  const ok=(d)=>Promise.resolve({ok:true,status:200,json:async()=>d,blob:async()=>({})});
  if(u.indexOf('/api/stt/health')===0) return ok({available:true});
  if(u.indexOf('/api/stt?')===0) return ok(spec.stt||{});
  if(u.indexOf('/api/voice-select')===0) return ok(spec.select||{});
  return Promise.resolve({ok:false,status:503,json:async()=>({}),blob:async()=>({})}); });
def('speechSynthesis',{cancel(){},speak(u){ if(u&&u.onend) u.onend(); }});
def('SpeechSynthesisUtterance',function(t){ this.text=t; });
def('Audio',function(){ return el('audio'); });
def('URL',{createObjectURL:()=>'blob:x',revokeObjectURL(){}});

const settle=()=>new Promise(r=>realTimeout(r,15));
const feed=(amp,frames,orphan)=>{ for(let f=0;f<frames;f++){
    const arr=new Float32Array(FRAME);
    for(let i=0;i<FRAME;i++) arr[i]=(i%2?amp:-amp);
    out.fedMs+=FRAME_MS;
    const fn=orphan?lastHandler:(proc&&proc.onaudioprocess);
    if(fn){
      try{ fn({inputBuffer:{getChannelData:()=>arr}}); }
      catch(e){ out.threw.push(String((e&&e.message)||e)); } } } };

vm.runInThisContext(script);
(async()=>{
  await settle();                       // initStt() + the camera rejection
  global.listen();                      // the badge greeting's say() callback, directly
  await settle();                       // getUserMedia(audio) + the capture chain
  for(const step of (spec.steps||[])){
    if(step.kind==='backstop'){
      for(const iv of intervals){ if(iv.ms===step.ms && !iv.cleared) iv.fn(); }
    } else if(step.kind==='stopListen'){
      // The teardown every miss, give-up, badge and button tap performs. The capture
      // callback can still be handed a queued event afterwards -- that is the point.
      global.stopListen();
    } else if(step.kind==='wait'){
      await new Promise(r=>realTimeout(r,step.ms||0));
    } else {
      feed(step.kind==='silence'?(step.amp||0.0005):(step.amp||0.2), step.frames,
           step.orphan===true);
    }
    await settle();
  }
  await settle();
  out.capture=global.__emoriCapture||null;
  out.gateBypass=global.__emoriGateBypass||0;
  out.hasProc=!!proc;
  console.log(JSON.stringify(out));
})().catch(e=>{ console.error((e&&e.stack)||String(e)); process.exit(3); });
"""


def _run_menu(tmp_path, rendered: str, steps: list, **spec) -> dict:
    body = rendered[rendered.index("<script>") + len("<script>"):rendered.rindex("</script>")]
    script = tmp_path / "menu.js"
    script.write_text(body, encoding="utf-8")
    spec_file = tmp_path / "menu-spec.json"
    spec_file.write_text(json.dumps({"steps": steps, **spec}), encoding="utf-8")
    harness = tmp_path / "menu-harness.js"
    harness.write_text(_MENU_HARNESS, encoding="utf-8")
    run = subprocess.run(
        [shutil.which("node"), harness.as_posix(), script.as_posix(), spec_file.as_posix()],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report["threw"] == [], report["threw"]
    assert report["hasProc"] is True, "the capture chain never came up in the harness"
    return report


def _posts(report: dict) -> list[dict]:
    return [f for f in report["fetches"] if f["url"].startswith("/api/stt?")]


@pytest.fixture()
def menu_page(kiosk):
    with kiosk as client:
        return client.get("/").text


def test_the_backstop_interval_is_still_the_old_fixed_timer():
    from emori_adapter.hostapp import pages, server
    from emori_adapter.hostapp.voice_topics import VoiceMenu

    js = pages._STT_JS
    assert "sttTimer=setInterval(flushStt, MENU.flushMs);" in js
    assert "setInterval(flushStt, 3500)" not in js      # the fixed TRIGGER is gone...
    # ...and 3.5 s is now the default of the knob that keeps it as the BACKSTOP.
    assert server._menu_tuning(VoiceMenu.default_menu(), "en")["flushMs"] == 3500


def test_the_edge_detector_reuses_the_capture_gate_rather_than_adding_one():
    """One noise gate, one dBFS floor, one frame length -- shared by both callers.

    A second detector with its own threshold is how the on-robot tuning knob stops meaning
    anything: EMORI_STT_ENERGY_DBFS would then gate the window and not the edge.
    """
    from emori_adapter.hostapp import pages

    js = pages._STT_JS
    assert "function voicedMs16k(pcm){" in js
    assert "const ms=voicedMs16k(pcm);" in js                           # the window gate
    assert "const voiced=voicedMs16k(toPcm16k([chunk], rate));" in js   # ...and the edge
    assert js.count("Math.pow(10,STT_GATE.dbfs/20)") == 1               # exactly one floor
    # ...and the CDP breadcrumb still measures the WINDOW, not the last 85 ms chunk.
    assert "window.__emoriVoicedMs=ms;" in js
    assert js.index("window.__emoriVoicedMs=ms;") > js.index("const ms=voicedMs16k(pcm);")


@node
def test_a_spoken_answer_is_flushed_on_the_silence_edge_not_on_the_timer(tmp_path,
                                                                        menu_page):
    """B: the measured defect -- an answer waiting out the rest of a 3.5 s window."""
    report = _run_menu(tmp_path, menu_page,
                       [{"kind": "speech", "frames": 6},      # ~512 ms of voice
                        {"kind": "silence", "frames": 9}])    # ~768 ms of room tone

    posts = _posts(report)
    assert len(posts) == 1
    # It went out ~1.3 s after the visitor started talking, with NO backstop tick fired --
    # the old page could not have sent anything before 3,500 ms.
    assert 1100 <= posts[0]["fedMs"] <= 1400
    assert report["capture"] is not None                      # the CDP counters exist


@node
def test_an_impulse_is_never_enough_to_flush_a_near_empty_buffer(tmp_path, menu_page):
    """A cough or a door bang clears the energy floor for ~150-300 ms.

    Below eosMinVoicedMs the edge simply declines, which is always safe: the backstop
    still flushes the same buffer on the old schedule and the unchanged energy window gate
    still judges it.
    """
    report = _run_menu(tmp_path, menu_page,
                       [{"kind": "speech", "frames": 2},        # ~160 ms voiced
                        {"kind": "silence", "frames": 14}])     # ~1.2 s of quiet after

    assert _posts(report) == []
    # ...and the backstop that WOULD have carried it is still armed.
    assert 3500 in report["intervals"]


@node
def test_a_hesitation_mid_phrase_does_not_split_the_answer(tmp_path, menu_page):
    """"uh... three": a few hundred ms of thinking is not the end of an utterance.

    A split half reaching Vosk can score below EMORI_MENU_CONF_MIN, and that is a MISS --
    the mic is torn down and the visitor is re-prompted for an answer they already gave.
    """
    report = _run_menu(tmp_path, menu_page,
                       [{"kind": "speech", "frames": 5},
                        {"kind": "silence", "frames": 5},       # ~427 ms: under the 700
                        {"kind": "speech", "frames": 5},
                        {"kind": "silence", "frames": 9}])      # ~768 ms: the real end

    posts = _posts(report)
    assert len(posts) == 1
    # ONE post carrying BOTH bursts: 24 frames of 48 kHz audio decimated to 16 kHz s16.
    assert posts[0]["bytes"] == (24 * 4096 // 3) * 2
    assert posts[0]["fedMs"] >= 1900


@node
def test_a_room_that_never_falls_silent_still_reaches_vosk_on_the_backstop(tmp_path,
                                                                          menu_page):
    """The wedge guard.

    Continuous speech -- or any noise that stays above the floor -- never produces an
    edge, so the fixed interval has to remain the thing that cannot be starved.
    """
    report = _run_menu(tmp_path, menu_page,
                       [{"kind": "speech", "frames": 48}])      # ~4.1 s, no silence at all
    assert _posts(report) == []                                 # no edge, correctly

    after = _run_menu(tmp_path, menu_page,
                      [{"kind": "speech", "frames": 48},
                       {"kind": "backstop", "ms": 3500}])
    assert len(_posts(after)) == 1


@node
def test_the_edge_can_be_switched_off_and_the_page_is_exactly_the_old_one(tmp_path, kiosk,
                                                                          monkeypatch):
    monkeypatch.setenv("EMORI_MENU_EOS_SILENCE_MS", "0")
    with kiosk as client:
        page = client.get("/").text

    report = _run_menu(tmp_path, page,
                       [{"kind": "speech", "frames": 6},
                        {"kind": "silence", "frames": 14}])
    assert _posts(report) == []                                 # edge disabled entirely

    after = _run_menu(tmp_path, page,
                      [{"kind": "speech", "frames": 6},
                       {"kind": "silence", "frames": 14},
                       {"kind": "backstop", "ms": 3500}])
    assert len(_posts(after)) == 1


def test_the_edge_thresholds_are_env_tunable_and_floored(monkeypatch):
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.voice_topics import VoiceMenu

    for name in ("EMORI_MENU_EOS_SILENCE_MS", "EMORI_MENU_EOS_MIN_VOICED_MS",
                 "EMORI_MENU_FLUSH_MS"):
        monkeypatch.delenv(name, raising=False)
    tuning = server._menu_tuning(VoiceMenu.default_menu(), "en")
    assert tuning["eosSilenceMs"] == 700
    assert tuning["eosMinVoicedMs"] == 250
    assert tuning["flushMs"] == 3500

    monkeypatch.setenv("EMORI_MENU_EOS_SILENCE_MS", "900")
    monkeypatch.setenv("EMORI_MENU_EOS_MIN_VOICED_MS", "400")
    monkeypatch.setenv("EMORI_MENU_FLUSH_MS", "2500")
    tuning = server._menu_tuning(VoiceMenu.default_menu(), "en")
    assert (tuning["eosSilenceMs"], tuning["eosMinVoicedMs"], tuning["flushMs"]) \
        == (900, 400, 2500)

    # A backstop of 0 ms would spin flushStt on every event-loop tick; the floor keeps the
    # one guard that cannot be starved intact even on a fat-fingered .env.
    monkeypatch.setenv("EMORI_MENU_FLUSH_MS", "0")
    monkeypatch.setenv("EMORI_MENU_EOS_SILENCE_MS", "-5")
    tuning = server._menu_tuning(VoiceMenu.default_menu(), "en")
    assert tuning["flushMs"] == 500
    assert tuning["eosSilenceMs"] == 0      # clamped to "off", never negative


# --- what the edge is allowed to cost ----------------------------------------------
#
# Three defects the edge introduced, all of them invisible while a flush could only ever
# be a whole 3.5 s window:
#
#   * the capture callback read `audioCtx.sampleRate` at the CALL SITE, outside
#     noteCapture's try/catch, so any audioprocess event delivered after stopListen()
#     nulled it threw inside the audio callback;
#   * an edge flush that scored below confMin was an onMiss(), which tears down the mic
#     and burns one of only 3 tries -- intermittent chatter burned all three in 3.6 s;
#   * flushStt emptied pcmBuf BEFORE the 0.5 s length check, so a short edge flush
#     destroyed the audio the backstop was supposed to carry.


def test_the_capture_callback_never_touches_the_context_it_can_outlive():
    """Source companion to the teardown run below: the deref is GONE, not guarded.

    A `try{}` around the call site would be undone by the next edit that reformats it;
    a rate that was never read from `audioCtx` inside the callback cannot come back
    without reintroducing the identifier this pins as absent.
    """
    from emori_adapter.hostapp import pages

    js = pages._STT_JS
    handler = js[js.index("proc.onaudioprocess=e=>{"):js.index("// The BACKSTOP")]
    assert "audioCtx" not in handler                        # nothing teardown can null
    assert "noteCapture(ch, capRate)" in handler
    assert "if(capGen!==listenGen) return;" in handler      # ...and a late event is inert
    assert "const capRate=audioCtx.sampleRate, capGen=listenGen;" in js
    # ...and the handler is detached before the disconnect, not merely disconnected.
    assert "proc.onaudioprocess=null; proc.disconnect();" in js


@node
def test_a_capture_callback_after_teardown_can_never_throw(tmp_path, menu_page):
    """Regression: stopListen() nulls audioCtx while onaudioprocess is still installed.

    Every miss, give-up, badge scan and topic-button tap runs that teardown, and a
    ScriptProcessor event queued by the audio thread before the disconnect is still
    delivered afterwards. `orphan` replays exactly that event.
    """
    report = _run_menu(tmp_path, menu_page, [
        {"kind": "speech", "frames": 4},
        {"kind": "stopListen"},
        {"kind": "speech", "frames": 3, "orphan": True},     # ...delivered after teardown
        {"kind": "silence", "frames": 9, "orphan": True},
    ])

    assert report["threw"] == []          # _run_menu asserts this too; say it out loud
    # And the late audio is inert: it neither reaches Vosk nor survives into a later window.
    assert _posts(report) == []


@node
def test_room_chatter_can_never_burn_the_visitors_tries(tmp_path, menu_page):
    """Regression: intermittent room chatter replayed against the menu.

    Intermittent chatter -- 0.43 s bursts, 0.77 s gaps -- clears eosMinVoicedMs on every
    burst, so the edge fires roughly three times where the backstop flushes once. Each
    sub-confidence answer as an onMiss() would mean: mic torn down, one of 3 tries gone, and
    a give-up in 3.6 s for a visitor who had not spoken yet. An edge flush may only ACCEPT
    now, so none of that is reachable; the miss still happens, on the BACKSTOP, at exactly
    the pre-edge rate.
    """
    burst = [{"kind": "speech", "frames": 5},        # ~427 ms voiced
             {"kind": "silence", "frames": 9}]       # ~768 ms of gap
    report = _run_menu(tmp_path, menu_page, burst * 3,     # ~3.6 s of room chatter
                       stt={"text": "one", "conf": 0.1})

    spoken = [f["url"] for f in report["fetches"] if f["url"].startswith("/api/tts")]
    # Not one re-prompt, not one give-up -- and therefore not one try spent.
    assert not [u for u in spoken if "Sorry" in u], spoken
    assert not [u for u in spoken if "badge" in u], spoken
    # The mic was never torn down and reopened either (one video + one audio, as opened).
    assert report["gum"] == ["video", "audio"]
    # ...and the edge posted ONCE, not once per burst: a 3.5 s Vosk decode costs ~1 s of
    # CM4 CPU, so the speculative post is capped at one per backstop period.
    assert len(_posts(report)) == 1

    # The other half of the contract: the judgement itself is not lost, it is deferred to
    # the backstop -- one miss per 3.5 s window, exactly what the pre-edge page did.
    judged = _run_menu(tmp_path, menu_page, burst * 3 + [{"kind": "backstop", "ms": 3500}],
                       stt={"text": "one", "conf": 0.1})
    reprompts = [f["url"] for f in judged["fetches"]
                 if f["url"].startswith("/api/tts") and "Sorry" in f["url"]]
    assert len(reprompts) == 1
    assert judged["gum"] == ["video", "audio", "audio"]   # re-prompt -> mic reopened


def test_an_edge_flush_may_only_ever_accept():
    """Both exits of the speculative path, pinned in source.

    A confidence below the gate and a non-exact match are the two ways an answer becomes
    a MISS, and the edge must take neither.
    """
    from emori_adapter.hostapp import pages

    js = pages._STT_JS
    assert "if(conf<MENU.confMin){ if(!edge) onMiss(); return; }" in js
    assert "select(d.text, edge); }catch(e){}" in js
    select = pages.VOICE_HTML
    body = select[select.index("async function select(text,speculative){"):
                  select.index("function onMiss()")]
    assert body.index("if(speculative) return;") < body.index("onMiss();")
    # The buffer survives a speculative flush: only the backstop drains its window, so the
    # deferred judgement still has the audio to judge.
    assert "if(edge){ edgeSpent=true; }" in js
    assert "else { gateJudged++; edgeSpent=false; pcmBuf=[]; }" in js
    assert "if(edge&&edgeSpent) return;" in js          # ...capped at one per backstop


@node
def test_a_short_edge_flush_never_destroys_the_audio(tmp_path, kiosk, monkeypatch):
    """A short edge flush keeps the audio, at the knob settings that make it reachable.

    flushStt emptied pcmBuf BEFORE the 0.5 s length check, so a flush shorter than that
    threw the visitor's audio away and the backstop -- the thing that is supposed to
    guarantee every buffer reaches Vosk -- had nothing left to carry.
    """
    monkeypatch.setenv("EMORI_MENU_EOS_SILENCE_MS", "200")
    monkeypatch.setenv("EMORI_MENU_EOS_MIN_VOICED_MS", "120")
    with kiosk as client:
        page = client.get("/").text

    # The edge fires at ~427 ms of buffer -- under the 0.5 s floor, so nothing is POSTed.
    # That much was always true; what mattered is whether the audio still exists after it.
    edge_only = [{"kind": "speech", "frames": 2},   # ~171 ms voiced: over the 120 ms knob
                 {"kind": "silence", "frames": 3}]  # ~256 ms silent: over the 200 ms knob
    report = _run_menu(tmp_path, page, edge_only)
    assert _posts(report) == []                     # too short to post, correctly

    frames = 10
    carried = _run_menu(tmp_path, page, edge_only + [
        {"kind": "silence", "frames": 5},           # the window grows past 0.5 s...
        {"kind": "backstop", "ms": 3500},           # ...and the backstop carries it
    ])
    posts = _posts(carried)
    assert len(posts) == 1
    # EVERY frame is still there, the two VOICED ones included: 48 kHz decimated to 16 kHz
    # s16le, nothing dropped. Before the fix this window was 5 frames short -- exactly the
    # speech -- because the sub-0.5 s edge flush had already emptied pcmBuf.
    assert posts[0]["bytes"] == (frames * 4096 // 3) * 2


@node
def test_a_broken_energy_floor_cannot_deafen_the_menu(tmp_path, kiosk, monkeypatch):
    """The claim the backstop makes about itself, made true.

    EMORI_STT_ENERGY_DBFS=-5 is inside the accepted range and makes voicedMs return 0 for
    everything, including someone shouting at the robot. The edge then cannot fire at all
    (it needs a voiced frame) and the backstop fired but was dropped by the energy window gate
    -- so nothing reached Vosk and the menu could only be answered by button. The gate may
    skip windows now, but never the first window after the mic opens.
    """
    monkeypatch.setenv("EMORI_STT_ENERGY_DBFS", "-5")
    with kiosk as client:
        page = client.get("/").text

    report = _run_menu(tmp_path, page, [
        {"kind": "speech", "frames": 12},                  # a real answer, read as silence
        {"kind": "silence", "frames": 9},
        {"kind": "backstop", "ms": 3500},
    ])
    assert len(_posts(report)) == 1                        # ...and it reached Vosk anyway
    assert report["gateBypass"] == 1                       # visibly, for the operator
    assert report["capture"] == {"voiced": 0, "silence": 0}  # the edge never even armed

    # ONCE per mic session, not a gate that has quietly stopped working: the second window
    # of room tone is dropped exactly as before.
    twice = _run_menu(tmp_path, page, [
        {"kind": "speech", "frames": 12},
        {"kind": "backstop", "ms": 3500},
        {"kind": "speech", "frames": 12},
        {"kind": "backstop", "ms": 3500},
    ])
    assert len(_posts(twice)) == 1
    assert twice["gateBypass"] == 1


@node
def test_a_healthy_floor_never_pays_for_the_escape_hatch(tmp_path, menu_page):
    """The bypass is free when the gate works: a real answer passes it, so it never fires."""
    report = _run_menu(tmp_path, menu_page, [
        {"kind": "speech", "frames": 6},
        {"kind": "silence", "frames": 9},
        {"kind": "backstop", "ms": 3500},
    ])
    assert report["gateBypass"] == 0
    assert len(_posts(report)) >= 1


def test_the_env_docs_no_longer_claim_the_backstop_rescues_a_broken_floor():
    """The env docs do not claim the backstop rescues a broken energy floor, in BOTH places.

    It said the backstop made "an energy floor set so high nothing reads as voiced" unable
    to wedge the menu. Measured, it did not: the backstop fired and the window gate then
    dropped the buffer. The escape hatch above is what makes the claim true, so the docs
    must point at the hatch -- and at the counter that diagnoses the typo -- not at the
    interval that never did it.
    """
    import inspect
    from pathlib import Path

    from emori_adapter.hostapp import server

    env = Path(__file__).resolve().parents[1].joinpath(".env.example") \
        .read_text(encoding="utf-8")
    source = inspect.getsource(server._menu_tuning)

    # The sentence that was false, in both of its wordings.
    for text in (env, source):
        assert "set so high nothing reads as voiced, unable to wedge" not in text
        assert "nothing is ever voiced -- unable to wedge the menu" not in text
    # Each doc names the mechanism that actually carries the guarantee...
    assert "never the FIRST window" in env
    assert "never the FIRST window of a mic session" in source
    # ...and the counter an operator reads on the robot to spot the typo that caused it.
    assert "__emoriGateBypass" in env and "__emoriGateBypass" in source
    # The backstop's remaining, TRUE claim is still documented: a room that never falls
    # silent produces no edge, so the fixed interval is what gets that audio to Vosk.
    assert "never falls silent" in env and "never falls silent" in source


# --- C: warm the origin while the visitor is choosing --------------------------------


def test_the_menu_page_preconnects_to_both_pokamind_origins(monkeypatch):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    monkeypatch.setattr(server, "POKAMIND_BASE", "https://api.pokamind.com")
    app = server._build_app(overrides={"robot_client": DryRunRobotClient(),
                                       "fiware_factory": None, "idle": False,
                                       "watchdog": False, "gesture": False,
                                       "tts": None, "stt": None})
    with TestClient(app) as client:
        page = client.get("/").text

    assert "__PRECONNECT__" not in page
    # BOTH: the iframe first loads the SSO endpoint on the API origin and is then 302'd to
    # the frontend origin, which is where the session document and its bundle live.
    for origin in ("https://api.pokamind.com", "https://app.pokamind.com"):
        assert f'<link rel="dns-prefetch" href="{origin}">' in page
        assert f'<link rel="preconnect" href="{origin}">' in page
    # An iframe navigation uses the CREDENTIALED connection pool; an anonymous preconnect
    # warms the wrong one.
    assert "preconnect" in page and "crossorigin" not in page


def test_the_preconnect_can_never_become_a_fetch_of_the_app(monkeypatch):
    """C's whole safety argument: a link HINT opens a socket and sends no request.

    `preload`/`prefetch`/`prerender` all fetch (and prerender BOOTS) the document -- on
    this kiosk that would mean a second Pokamind app signing in with the one-time SSO
    token, with Chromium's --use-fake-ui-for-media-stream auto-granting it
    the microphone the landing page is capturing menu answers with.
    """
    from emori_adapter.hostapp import server

    monkeypatch.setattr(server, "POKAMIND_BASE", "https://api.pokamind.com")
    links = server._preconnect_links()
    assert links
    assert set(re.findall(r'rel="([a-z\-]+)"', links)) == {"dns-prefetch", "preconnect"}
    assert "<iframe" not in links and "<script" not in links


def test_a_junk_pokamind_base_yields_no_links_rather_than_markup(monkeypatch):
    """POKAMIND_BASE is deployment config, and it is written into the page's <head>.

    A value that cannot be reduced to a bare scheme://host[:port] produces NO link at
    all -- never a half-parsed one, and never an attribute that closes itself.
    """
    from emori_adapter.hostapp import server

    shape = re.compile(r'(<link rel="(?:dns-prefetch|preconnect)" '
                       r'href="https?://[a-z0-9.\-]+(?::\d{1,5})?">)*')
    for bad in ("", "not a url", 'https://a.test/" onload="x', "javascript:alert(1)",
                "https://user:pw@evil.test", "https://a.test/path?q=1"):
        monkeypatch.setattr(server, "POKAMIND_BASE", bad)
        links = server._preconnect_links()
        assert shape.fullmatch(links), links
        assert "onload" not in links

    for empty in ("", "not a url", "javascript:alert(1)", "https://user:pw@evil.test"):
        monkeypatch.setattr(server, "POKAMIND_BASE", empty)
        assert server._preconnect_links() == ""


@node
def test_the_menu_page_never_boots_pokamind_or_opens_a_second_microphone(tmp_path,
                                                                        menu_page):
    """The pre-boot hazard, pinned as an absence.

    A hidden pre-boot iframe was REJECTED rather than shipped behind a flag, so the
    running menu page must touch nothing but its own origin: no cross-origin request, no
    iframe, and exactly the two getUserMedia calls it always had (the QR camera, then the
    menu mic) -- a third would be a second claim on the single capture device.
    """
    report = _run_menu(tmp_path, menu_page,
                       [{"kind": "speech", "frames": 6},
                        {"kind": "silence", "frames": 9}])

    cross = [f["url"] for f in report["fetches"] if not f["url"].startswith("/")]
    assert cross == []
    assert "iframe" not in report["created"]
    assert report["gum"] == ["video", "audio"]
    assert report["href"] is None            # nothing navigated: no session was started
