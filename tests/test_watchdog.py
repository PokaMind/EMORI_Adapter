"""Pure-logic tests for the ConversationWatchdog + a tiny WatchdogTicker integration.

The watchdog is a pure state machine over an injected ``now`` -- these tests never
rely on real time for the logic cases. One deterministic real-thread test exercises
the internal lock probabilistically; the optional ticker test uses tiny env-tuned
thresholds and always stops/joins the daemon.
"""

from __future__ import annotations

import threading
import time

from emori_adapter.hostapp.watchdog import ConversationWatchdog, TurnState


def _wd() -> ConversationWatchdog:
    return ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0)


# --- core state machine ----------------------------------------------------------

def test_starts_idle():
    wd = _wd()
    assert wd.state is TurnState.IDLE


def test_armed_then_open_then_submitted():
    wd = _wd()
    wd.on_armed(now=0.0)
    assert wd.state is TurnState.ARMED
    wd.on_user_speaking(now=1.0)
    assert wd.state is TurnState.OPEN
    wd.on_submitted(now=2.0)
    assert wd.state is TurnState.SUBMITTED


def test_armed_alone_never_force_ends():
    """ARMED is timer-inert: an armed-but-never-spoke turn must not be force-ended."""
    wd = _wd()
    wd.on_armed(now=0.0)
    assert wd.due(now=999.0) is None
    assert wd.state is TurnState.ARMED


def test_reprompt_due_after_threshold_while_open():
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=9.0) is None          # not yet
    assert wd.due(now=10.5) == "reprompt"   # crossed reprompt threshold
    assert wd.due(now=10.6) is None         # only fires once per turn
    assert wd.due(now=11.0) is None         # idempotent thereafter


def test_force_end_due_after_ceiling():
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    wd.due(now=10.5)                        # consume the reprompt
    assert wd.due(now=15.5) == "force_end"


def test_force_end_is_terminal_one_shot():
    """After force_end (state -> TIMED_OUT) due() returns None at any later now."""
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=15.5) == "force_end"
    assert wd.state is TurnState.TIMED_OUT
    assert wd.due(now=16.0) is None
    assert wd.due(now=1000.0) is None


def test_ai_utterance_closes_the_open_turn():
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    wd.on_ai_utterance(now=3.0)             # AI replied => turn advanced
    assert wd.state is TurnState.IDLE
    assert wd.due(now=30.0) is None


def test_ai_utterance_disarms_a_pending_reprompt():
    """A healthy AI reply must cancel a reprompt that was about to fire."""
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=9.9) is None
    wd.on_ai_utterance(now=9.95)
    assert wd.due(now=10.5) is None         # disarmed -> no reprompt


def test_rearm_after_timeout_reopens_cleanly():
    """After force_end, a fresh arm+speak re-opens relative to the NEW _opened_at
    and reprompt fires again (no stale latch suppresses it)."""
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=15.5) == "force_end"
    wd.on_armed(now=20.0)
    wd.on_user_speaking(now=20.0)
    assert wd.due(now=29.0) is None
    assert wd.due(now=30.5) == "reprompt"   # 20.0 + 10.0 reprompt window
    assert wd.due(now=35.5) == "force_end"  # 20.0 + 15.0 ceiling


def test_ceiling_dominates_misconfiguration():
    """reprompt_after_s >= open_ceiling_s must fail safe: force_end, never reprompt."""
    wd = ConversationWatchdog(open_ceiling_s=5.0, reprompt_after_s=10.0)
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=6.0) == "force_end"
    assert wd.state is TurnState.TIMED_OUT


def test_submitted_clears_any_pending_reprompt_latch():
    """on_submitted resets the clock+latch so a later re-open is not suppressed."""
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    wd.due(now=10.5)                        # latch _reprompted = True
    wd.on_submitted(now=11.0)
    # from SUBMITTED, on_user_speaking is a no-op (only ARMED/IDLE open); arm first:
    wd.on_armed(now=12.0)
    wd.on_user_speaking(now=12.0)
    assert wd.due(now=22.5) == "reprompt"   # fresh window, latch was cleared


# --- poller contract -------------------------------------------------------------

def test_poller_invokes_reprompt_then_force_end():
    wd = _wd()
    actions = []
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    # simulate the poller calling due() and dispatching the action
    for now in (5.0, 10.5, 12.0, 15.5):
        action = wd.due(now=now)
        if action:
            actions.append((now, action))
    assert actions == [(10.5, "reprompt"), (15.5, "force_end")]


# --- concurrency intent ----------------------------------------------------------

def test_watchdog_owns_a_lock():
    wd = _wd()
    assert isinstance(wd._lock, type(threading.Lock()))


def test_concurrent_churn_fires_each_action_at_most_once():
    """Hammer due() from the main thread while workers churn on_user_speaking on a
    SINGLE open window. With a correct lock, reprompt and force_end each fire at most
    once per opened window and nothing raises."""
    wd = _wd()
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)

    errors: list[BaseException] = []
    reprompts = 0
    force_ends = 0
    stop = threading.Event()

    def churn():
        try:
            while not stop.is_set():
                # while OPEN, on_user_speaking is a no-op (only opens from ARMED/IDLE)
                wd.on_user_speaking(now=0.0)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    workers = [threading.Thread(target=churn, daemon=True) for _ in range(4)]
    for w in workers:
        w.start()
    try:
        for now in (10.5, 10.6, 10.7, 15.5, 16.0, 20.0):
            a = wd.due(now=now)
            if a == "reprompt":
                reprompts += 1
            elif a == "force_end":
                force_ends += 1
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=1.0)
    assert not errors
    assert reprompts <= 1
    assert force_ends <= 1


# --- optional: WatchdogTicker integration (tiny thresholds, real daemon) --------

class _NoopClient:
    """Minimal robot client so RobotPlayer/LiveDriver construct without a daemon."""

    def health(self):
        return False

    def wake_up(self):
        pass

    def apply(self, intent):
        pass


def test_watchdog_ticker_dispatches_controlled_reprompt_then_force_end(monkeypatch):
    """The ticker dispatches watchdog actions without relying on OS timer precision.

    ``ConversationWatchdog`` timing/order is covered by the pure state-machine
    tests above.  Here we control its two actions so a busy host cannot skip a
    20ms re-prompt window and turn this integration assertion into a flake.
    """
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker

    driver = LiveDriver(_NoopClient(), fiware_factory=None)
    # force_end is guarded on tracker.active -> give it an active session to finalize.
    driver.tracker.start(SessionInfo(session_id="wd-int", person_id="p1", lang="en"))
    actions = iter(("reprompt", "force_end"))

    def due(*, now, present=False):
        del now, present
        return next(actions, None)

    monkeypatch.setattr(driver.watchdog, "due", due)

    # end_grace_s tiny so the local fallback finalize happens within the test window
    # (force_end now first ASKS Pokamind to complete -> analysis; a dead iframe never
    # answers, so the fallback ends it locally).
    ticker = WatchdogTicker(driver, tick_s=0.01, walkaway_s=0, end_grace_s=0.05)
    ticker_stopped = False
    player_stopped = False
    ticker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and driver.tracker.active:
            time.sleep(0.01)
    finally:
        ticker.stop()
        ticker_stopped = ticker.join()
        driver.player.stop()
        player_stopped = driver.player.join()

    assert ticker_stopped is True
    assert player_stopped is True
    assert driver.counters.get("watchdog_reprompts", 0) == 1
    assert driver.counters.get("watchdog_force_ends", 0) == 1
    assert driver.counters.get("end_local_fallback", 0) == 1   # Pokamind never completed
    assert driver.tracker.active is False          # fallback finalized the active session
    assert driver.status() is not None             # no deadlock; status returns


def test_walkaway_asks_pokamind_to_complete_so_analysis_is_captured(monkeypatch):
    """Walk-away must FIRST ask Pokamind to complete (emori_cmd:end -> stopRecording ->
    session_completed with results) so the learner's analysis + spoken summary are
    produced. A local finalize(results=None) would end the session but discard the
    analysis. Only if Pokamind never completes (dead iframe) does the local fallback fire."""
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker

    driver = LiveDriver(_NoopClient(), fiware_factory=None)
    driver.tracker.start(SessionInfo(session_id="wa", person_id="p1", lang="en"))
    driver.last_activity = time.monotonic() - 100.0   # long-idle -> walk-away due at once

    ticker = WatchdogTicker(driver, tick_s=0.01, walkaway_s=0.001, end_grace_s=30.0)
    ticker.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and driver.counters.get("walkaway_ends", 0) < 1:
            time.sleep(0.005)
        # It asked Pokamind to end (queued 'end' for the /session page to relay) and the
        # session is STILL active, waiting for Pokamind's completion (with the analysis) --
        # NOT locally finalized-and-discarded.
        assert driver.counters.get("walkaway_ends", 0) == 1
        assert driver.pop_cmd() == "end"
        assert driver.tracker.active is True
        assert driver.counters.get("end_local_fallback", 0) == 0
        # Pokamind now completes with results -> the session finalizes WITH analysis.
        driver.on_completed({"type": "pokamind_session_completed", "results": {"ok": 1}})
        assert driver.tracker.active is False
    finally:
        ticker.stop()
        ticker.join()
        driver.player.stop()
        driver.player.join()


# --- stall clock -----------------------------------------------------------------

class TestStallClock:
    def test_activity_refreshes_the_stall_clock(self):
        wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0)
        wd.on_armed(now=0.0)
        wd.on_user_speaking(now=0.0)
        # user keeps talking: activity every second up to t=20 -> a >15s turn NEVER force-ends
        for t in range(1, 21):
            wd.on_activity(now=float(t))
            assert wd.due(now=float(t)) is None
        # then goes silent: reprompt at stall+10, force_end at stall+15
        assert wd.due(now=29.9) is None
        assert wd.due(now=30.0) == "reprompt"
        assert wd.due(now=34.9) is None
        assert wd.due(now=35.0) == "force_end"

    def test_activity_outside_open_is_ignored(self):
        wd = ConversationWatchdog()
        wd.on_activity(now=5.0)          # IDLE: no crash, no state change
        assert wd.state is TurnState.IDLE

    def test_without_activity_behavior_is_unchanged(self):
        wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0)
        wd.on_armed(now=0.0)
        wd.on_user_speaking(now=0.0)
        assert wd.due(now=9.9) is None
        assert wd.due(now=10.0) == "reprompt"
        assert wd.due(now=15.0) == "force_end"

    def test_activity_relatches_the_reprompt(self):
        wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0)
        wd.on_armed(now=0.0)
        wd.on_user_speaking(now=0.0)
        assert wd.due(now=10.0) == "reprompt"
        wd.on_activity(now=11.0)         # user resumed -> new stall window
        assert wd.due(now=20.9) is None
        assert wd.due(now=21.0) == "reprompt"
