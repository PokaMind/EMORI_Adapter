"""End the session on absence-of-person, immune to ambient noise."""

from __future__ import annotations

import time


def _driver():
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    return LiveDriver(DryRunRobotClient())


def test_mark_present_updates_clock_and_latch():
    driver = _driver()
    driver.last_presence = 0.0
    driver._body_ever_seen = False
    driver.mark_present()
    assert driver._body_ever_seen is True
    assert driver.last_presence > 0.0


def test_status_exposes_presence_fields():
    driver = _driver()
    status = driver.status()
    assert "last_present_s" in status
    assert "body_ever_seen" in status


def test_absence_due_requires_prior_body_and_expiry(monkeypatch):
    from emori_adapter.hostapp.driver import WatchdogTicker

    monkeypatch.setenv("EMORI_ABSENCE_S", "20")
    driver = _driver()
    ticker = WatchdogTicker(driver)
    now = time.monotonic()

    driver._body_ever_seen = False
    driver.last_presence = now - 100.0
    assert ticker._absence_due(now) is False  # camera-off session: never fires

    driver._body_ever_seen = True
    driver.last_presence = now - 5.0
    assert ticker._absence_due(now) is False  # person still present

    driver.last_presence = now - 25.0
    assert ticker._absence_due(now) is True  # gone past EMORI_ABSENCE_S


def test_absence_s_defaults_off_until_a_human_enables_it(monkeypatch):
    """New session-ending path: inert on a fresh deploy (fail-closed posture)."""
    from emori_adapter.hostapp.driver import WatchdogTicker

    monkeypatch.delenv("EMORI_ABSENCE_S", raising=False)
    driver = _driver()
    ticker = WatchdogTicker(driver)
    assert ticker.absence_s == 0.0

    now = time.monotonic()
    driver._body_ever_seen = True
    driver.last_presence = now - 3600.0
    assert ticker._absence_due(now) is False  # disabled -> never fires


# --- wiring: the presence signal must actually reach the predicate ----------------
# The three tests above exercise the API in isolation; these two pin the field names
# and the loop branch, so a renamed Pokamind field or a misplaced branch cannot make
# the feature silently never arm.

def _analysis_envelope(**overrides) -> dict:
    payload = {
        "type": "pokamind_analysis_update",
        "seq": 1,
        "body_language": {"posture": "upright", "engagement_score": 52.4},
        "body_language_metrics": {"engagement": 52.4, "confidence": 43.7},
        "communication_styles": {"neutral": 12.41, "steady": 7.81},
    }
    payload.update(overrides)
    return payload


def test_on_analysis_marks_presence_only_for_body_bearing_cycles():
    """Field names are load-bearing: these are the keys real recorded envelopes carry
    (tests/fixtures/live_session.jsonl) and the same ones translation/engagement.py reads."""
    driver = _driver()
    try:
        # Each field alone must arm presence -- asserted separately because has_body is
        # an OR, so testing them together would let a misspelling hide behind the other.
        for solo in ({"body_language": {}}, {"body_language_metrics": {}}):
            driver._body_ever_seen = False
            driver.last_presence = 0.0
            env = driver.stamp(_analysis_envelope(**solo))
            assert driver.on_analysis(env)["ok"] is True
            assert driver._body_ever_seen is True, f"presence not armed with {solo} emptied"
            assert driver.last_presence > 0.0

        # Camera sees nobody (both body fields empty) -> the clock must NOT move.
        driver._body_ever_seen = False
        driver.last_presence = 0.0
        empty = _analysis_envelope(body_language={}, body_language_metrics={})
        assert driver.on_analysis(driver.stamp(empty))["ok"] is True
        assert driver._body_ever_seen is False
        assert driver.last_presence == 0.0
    finally:
        driver.player.stop()
        driver.player.join()


def test_accepted_turn_refreshes_the_clock_but_never_arms_the_latch():
    """Audio evidence must not ARM absence-end.

    A camera-less session (unavailable/black camera -- documented on this robot) emits
    empty body fields on every cycle, so on_analysis never marks presence and only an
    accepted turn would refresh the clock. Turns are 25-60s apart, so a latch armed
    from audio would absence-end a live, fully attended conversation mid-answer --
    exactly the case docs/configuration.md (``EMORI_ABSENCE_S``) promises falls through to
    EMORI_WALKAWAY_S.
    """
    driver = _driver()
    try:
        driver._body_ever_seen = False
        driver.last_presence = 0.0
        driver.on_turn_submitted({"type": "emori_turn_submitted"})
        assert driver._body_ever_seen is False   # audio never enables the feature
        assert driver.last_presence > 0.0        # but it does suppress a false negative
    finally:
        driver.player.stop()
        driver.player.join()


def test_start_session_rearms_presence_for_the_next_learner():
    """The latch is per-session, not per-process: a previous learner's body must not
    leave absence-end armed against a fresh (possibly camera-less) session."""
    from emori_adapter.hostapp.driver import SessionInfo

    driver = _driver()
    try:
        driver.mark_present()
        assert driver._body_ever_seen is True
        driver.start_session(SessionInfo(session_id="s2", person_id="p2", lang="en"))
        assert driver._body_ever_seen is False
    finally:
        driver.player.stop()
        driver.player.join()


def test_run_loop_ends_via_pokamind_when_the_person_is_gone():
    """The absence branch must reach _request_pokamind_end (analysis-preserving), not
    a bare local finalize, and must not also fire the blind walk-away timer."""
    from emori_adapter.hostapp.driver import SessionInfo, WatchdogTicker

    driver = _driver()
    driver.tracker.start(SessionInfo(session_id="pw", person_id="p1", lang="en"))
    driver.touch()                                   # activity fresh -> walkaway not due
    driver._body_ever_seen = True
    driver.last_presence = time.monotonic() - 100.0  # person gone

    ticker = WatchdogTicker(driver, tick_s=0.01, absence_s=0.001,
                            walkaway_s=45.0, end_grace_s=30.0)
    ticker.start()
    try:
        # Poll the COMMAND, not the counter: _request_pokamind_end bumps the counter
        # before it enqueues, so a counter-first poll could pop None on a loaded box.
        deadline = time.monotonic() + 2.0
        cmd = None
        while cmd is None and time.monotonic() < deadline:
            cmd = driver.pop_cmd()
            if cmd is None:
                time.sleep(0.005)
        assert cmd == "end"                                     # asked Pokamind to complete
        assert driver.counters.get("presence_ends", 0) == 1
        assert driver.counters.get("walkaway_ends", 0) == 0     # not the blind timer
        assert driver.tracker.active is True                    # analysis not discarded
        assert driver.counters.get("end_local_fallback", 0) == 0
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()
