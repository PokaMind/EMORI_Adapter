"""WatchdogTicker / driver wiring for the presence-aware watchdog policy."""

from __future__ import annotations

import time

from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
from emori_adapter.robot.client import DryRunRobotClient


def _driver(monkeypatch=None) -> LiveDriver:
    d = LiveDriver(DryRunRobotClient(), fiware_factory=None)
    d.start_session(SessionInfo(session_id="t", person_id="p", lang="en"))
    return d


def _stop(ticker, driver):
    ticker.stop()
    ticker.join()
    driver.player.stop()
    driver.player.join()


def test_production_defaults_come_from_the_driver(monkeypatch):
    for k in ("EMORI_TURN_REPROMPT_S", "EMORI_TURN_CEILING_S", "EMORI_TURN_ABS_CEILING_S",
              "EMORI_TURN_FIRST_CEILING_S", "EMORI_TURN_PRESENT_CEILING_S"):
        monkeypatch.delenv(k, raising=False)
    wd = LiveDriver(DryRunRobotClient(), fiware_factory=None).watchdog
    assert (wd.reprompt_after_s, wd.open_ceiling_s, wd.abs_ceiling_s) == (12.0, 35.0, 90.0)
    assert (wd.first_reprompt_s, wd.first_ceiling_s) == (20.0, 50.0)
    assert wd.present_ceiling_s == 180.0 and wd.self_noise_s == 3.0


def test_camera_presence_windows(monkeypatch):
    d = _driver()
    ticker = WatchdogTicker(d, tick_s=10.0)
    now = time.monotonic()
    assert ticker._present(now) is False                  # nothing seen this session
    d.last_face_seen_at = now - 7.0
    assert ticker._present(now) is True                   # face window 8 s
    d.last_face_seen_at = now - 9.0
    assert ticker._present(now) is False
    d.last_body_seen_at = now - 29.0
    assert ticker._present(now) is True                   # body window 30 s
    d.start_session(SessionInfo(session_id="t2", person_id="p2", lang="en"))
    assert ticker._present(time.monotonic()) is False     # per learner
    d.player.stop()
    d.player.join()


def test_note_face_seen_only_during_a_session():
    d = LiveDriver(DryRunRobotClient(), fiware_factory=None)
    d.note_face_seen()
    assert d.last_face_seen_at is None
    d.start_session(SessionInfo(session_id="t", person_id="p", lang="en"))
    d.note_face_seen()
    assert d.last_face_seen_at is not None
    assert d._body_ever_seen is False                     # never arms absence-end
    d.player.stop()
    d.player.join()


def test_walkaway_stands_down_while_the_turn_is_open():
    d = _driver()
    d.on_utterance({"text": "hello", "seq": 1})           # exchanged -> not booting
    d.watchdog.on_armed(now=time.monotonic())
    d.watchdog.on_user_speaking(now=time.monotonic())     # OPEN
    d.last_activity = time.monotonic() - 100.0
    ticker = WatchdogTicker(d, tick_s=0.01, walkaway_s=0.001, end_grace_s=30.0)
    ticker.start()
    try:
        time.sleep(0.3)
        assert d.counters.get("walkaway_ends", 0) == 0
        d.watchdog.on_armed(now=time.monotonic())         # ARMED: walk-away owns it
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not d.counters.get("walkaway_ends"):
            time.sleep(0.01)
        assert d.counters.get("walkaway_ends", 0) == 1
    finally:
        _stop(ticker, d)


def test_reprompt_is_an_authored_gesture_and_never_displaces_an_end(monkeypatch):
    d = _driver()
    sent = []
    monkeypatch.setattr(d, "submit_legacy_motion",
                        lambda intents, **kw: sent.append(list(intents)) or True)
    ticker = WatchdogTicker(d, tick_s=10.0, spoken_reprompt=True)
    ticker._reprompt(time.monotonic(), False)
    assert sent == []                      # spoken path: wait for the muted window
    assert d.pop_cmd() == "reprompt"
    ticker.on_reprompt_stage("reprompt-start")
    assert sent and all(i.kind in ("goto", "antennas") for i in sent[0])  # no play_recorded
    ticker.on_reprompt_stage("reprompt-start")   # a duplicate ack moves nothing
    assert len(sent) == 1
    d.enqueue_cmd("end")
    ticker._reprompt(time.monotonic(), False)
    assert d.pop_cmd() == "end"                           # the learner's end survives
    assert d.counters["watchdog_reprompts"] == 2
    d.player.stop()
    d.player.join()


def test_reprompt_is_not_spoken_unless_enabled(monkeypatch):
    monkeypatch.delenv("EMORI_REPROMPT_SPOKEN", raising=False)
    d = _driver()
    ticker = WatchdogTicker(d, tick_s=10.0)
    ticker._reprompt(time.monotonic(), False)
    assert d.pop_cmd() is None
    d.player.stop()
    d.player.join()


def test_force_end_is_counted_by_kind_and_still_asks_pokamind(monkeypatch):
    d = _driver()
    d.on_utterance({"text": "hello", "seq": 1})
    monkeypatch.setattr(d.watchdog, "open_ceiling_s", 0.05)
    monkeypatch.setattr(d.watchdog, "first_ceiling_s", 0.05)
    d.watchdog.on_armed(now=time.monotonic())
    d.watchdog.on_user_speaking(now=time.monotonic())
    ticker = WatchdogTicker(d, tick_s=0.01, walkaway_s=0, end_grace_s=30.0)
    ticker.start()
    try:
        deadline = time.monotonic() + 2.0
        cmd = None
        while cmd is None and time.monotonic() < deadline:
            cmd = d.pop_cmd()
            time.sleep(0.005)
        assert cmd == "end"                                # (e) ends WITH analysis
        assert d.counters["watchdog_force_ends"] == 1
        assert d.counters["watchdog_end_silent"] == 1
        assert d.tracker.active is True                    # waiting for Pokamind
    finally:
        _stop(ticker, d)


def test_a_session_that_talked_gets_the_long_grace(monkeypatch):
    d = _driver()
    d.on_utterance({"text": "hello", "seq": 1})           # exchanged
    ticker = WatchdogTicker(d, tick_s=0.01, walkaway_s=0, end_grace_s=0.05,
                            end_grace_talked_s=30.0, session_start_s=0)
    d.enqueue_cmd("end")
    d.arm_end_grace()
    ticker.start()
    try:
        time.sleep(0.3)
        assert d.tracker.active is True                    # still waiting for the analysis
        assert d.counters.get("end_local_fallback", 0) == 0
    finally:
        _stop(ticker, d)


def test_a_session_that_never_exchanged_keeps_the_short_grace():
    d = _driver()
    ticker = WatchdogTicker(d, tick_s=0.01, walkaway_s=0, end_grace_s=0.05,
                            end_grace_talked_s=30.0, session_start_s=0)
    d.enqueue_cmd("end")
    d.arm_end_grace()
    ticker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and d.tracker.active:
            time.sleep(0.01)
        assert d.counters.get("end_local_fallback", 0) == 1
    finally:
        _stop(ticker, d)


def test_status_exposes_the_watchdog_and_presence():
    d = _driver()
    s = d.status()
    assert s["watchdog"]["first_turn"] is True and s["face_seen_s"] is None
    d.player.stop()
    d.player.join()


def test_session_page_relays_reprompt_into_the_frame():
    from emori_adapter.hostapp import pages
    assert "d.cmd==='reprompt'" in pages.SESSION_HTML
    assert "emoriCmd('reprompt')" in pages.SESSION_HTML


def test_tts_interlock_holds_for_a_camera_less_session(monkeypatch):
    from starlette.testclient import TestClient
    from emori_adapter.hostapp import server

    app = server._build_app(overrides={"watchdog": False, "idle": False, "gesture": False})
    d = app.state.driver
    c = TestClient(app)
    d.start_session(SessionInfo(session_id="cl", person_id="p", lang="en"))
    d.on_utterance({"text": "the roleplay is speaking", "seq": 1})
    assert d.tracker.cycles == 0                           # no camera, no cycles
    assert c.get("/api/tts", params={"text": "hi"}).status_code == 409


def test_gesture_poll_feeds_the_robot_face_tracker_into_presence(monkeypatch):
    """/api/state/full already carries motion.gaze_active (v2 shim, gaze.is_active());
    the 10 Hz gesture poll turns it into watchdog presence with no new HTTP call."""
    from tests.test_head_reserved_narrowing import _Clock, _FakeDriver, _NTicks, _ticker

    class _Driver(_FakeDriver):
        def __init__(self):
            super().__init__()
            self.faces = 0

        def note_face_seen(self):
            self.faces += 1

    class _Client:
        def __init__(self, gaze):
            self.gaze = gaze

        def read_state(self):
            return {"head_pose": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
                    "motion": {"active": False, "head_moving": False, "client": "",
                               "lane": "", "gaze_active": self.gaze}}

    for gaze, expected in ((True, 3), (False, 0)):
        driver = _Driver()
        driver.client = _Client(gaze)
        _d, ticker = _ticker(monkeypatch, clock=_Clock(), driver=driver)
        ticker._stop = _NTicks(3)
        ticker._run()
        assert driver.faces == expected


def test_a_bundle_that_ignores_the_spoken_reprompt_still_gets_the_gesture(monkeypatch):
    d = _driver()
    sent = []
    monkeypatch.setattr(d, "submit_legacy_motion",
                        lambda intents, **kw: sent.append(list(intents)) or True)
    ticker = WatchdogTicker(d, tick_s=10.0, spoken_reprompt=True)
    now = time.monotonic()
    ticker._reprompt(now, False)
    ticker._flush_deferred_gesture(now + 3.9)
    assert sent == []
    ticker._flush_deferred_gesture(now + 4.0)
    assert len(sent) == 1 and d.counters["reprompt_unacked"] == 1


def test_the_debug_sink_routes_reprompt_stages_to_the_ticker(monkeypatch):
    from starlette.testclient import TestClient
    from emori_adapter.hostapp import server

    import emori_adapter.hostapp.driver as drv

    seen = []
    monkeypatch.setattr(drv.WatchdogTicker, "on_reprompt_stage",
                        lambda self, stage: seen.append(stage))
    app = server._build_app(overrides={"idle": False, "gesture": False})
    c2 = TestClient(app)          # no lifespan: the ticker thread is never started
    c2.post("/api/emori-debug", json={"type": "pokamind_emori_dbg", "stage": "reprompt-start"})
    c2.post("/api/emori-debug", json={"type": "pokamind_emori_dbg", "stage": "started"})
    assert seen == ["reprompt-start"]


def test_face_freshness_prefers_the_detector_over_the_write_proxy():
    from emori_adapter.hostapp.driver import _face_fresh

    assert _face_fresh({"face_seen_s": 0.3, "gaze_active": False}) is True    # still learner
    assert _face_fresh({"face_seen_s": 4.0, "gaze_active": True}) is False
    assert _face_fresh({"face_seen_s": None, "gaze_active": True}) is True     # older image
    assert _face_fresh({"gaze_active": False}) is False
    assert _face_fresh(None) is False
