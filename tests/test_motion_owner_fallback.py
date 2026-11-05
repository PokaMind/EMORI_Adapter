"""``EMORI_ROS_OWNER_FALLBACK_S`` — the dead-man's handle on owner=ros.

``EMORI_ROS_MOTION_OWNER=ros`` hands the two expressive lanes to a process in another
container, reached over HTTP, whose failure mode is silence. The honest outcome of that
silence is a robot that stops being expressive, and that is what ships: an operator can
see it, and the demonstration's central claim ("movement originates in the ROS graph")
stays true. For an unattended venue run the site may decide a still robot in front of a
person is the worse of the two failures, and this knob is how it says so.

Everything below is about the boundary between those two, because a fallback that fires
too eagerly is worse than none at all: it silently turns an owner=ros robot into a hostapp
rig, and every counter on both sides still looks healthy.

The driver is built exactly the way ``tests/test_cue_driver_wiring.py`` builds one (the
helper is copied, not imported: these two files pin different behaviours of the same
object and should not be able to break each other).
"""

from __future__ import annotations

import pytest

from emori_adapter.behavior import repertoire as R
from emori_adapter.models import ContextSignal, DerivedSignals


class _GuardPlayer:
    """Today's player: records the guard closure so preemption can be asserted."""

    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.guards: list = []

    def submit(self, intents, *, drop_if_busy: bool = False, guard=None) -> bool:
        self.batches.append(list(intents))
        self.guards.append(guard)
        return True


class _Watchdog:
    """Counts the three calls the driver owes the conversation watchdog."""

    def __init__(self) -> None:
        self.ai = 0
        self.armed = 0

    def on_ai_utterance(self, now: float) -> None:
        self.ai += 1

    def on_armed(self, now: float) -> None:
        self.armed += 1


def _driver(player=None):
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    driver = LiveDriver(DryRunRobotClient(), player=player or _GuardPlayer(),
                        fiware_factory=None)
    driver.watchdog = _Watchdog()
    return driver


def _analysis(seq: int = 1) -> dict:
    return {
        "type": "pokamind_analysis_update",
        "seq": seq,
        "body_language": {"posture": "upright", "engagement_score": 52.4},
        "body_language_metrics": {"engagement": 52.4, "confidence": 43.7},
        "communication_styles": {"neutral": 12.41, "steady": 7.81},
    }


def _session(driver, *, cycles: int = 1) -> None:
    """A running session with cycles on the clock: the fallback's preconditions."""

    from emori_adapter.hostapp.driver import SessionInfo

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    for seq in range(cycles):
        driver.on_analysis(driver.stamp(_analysis(seq)))


def _age(driver, seconds: float) -> None:
    """Pretend ``seconds`` have passed since the session (and any beat) started."""

    if driver.session_started_at is not None:
        driver.session_started_at -= seconds
    if driver._last_skill_accepted is not None:
        driver._last_skill_accepted -= seconds


# --- the shipped default ----------------------------------------------------------

def test_the_fallback_is_off_unless_it_is_configured(monkeypatch):
    """The whole point of owner=ros is that this process does NOT drive."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    driver = _driver()
    _session(driver, cycles=3)
    _age(driver, 3600.0)

    assert driver.expressive_motion_enabled is False
    assert driver.counters["owner_fallback_episodes"] == 0


@pytest.mark.parametrize("raw", ["", "0", "-5", "not-a-number", "   "])
def test_an_unset_or_unreadable_window_reads_as_off(monkeypatch, raw):
    """A typo must never be the thing that turns the hostapp back on."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", raw)
    driver = _driver()
    _session(driver, cycles=3)
    _age(driver, 3600.0)

    assert driver.expressive_motion_enabled is False


def test_the_knob_is_read_once_at_construction(monkeypatch):
    """Like every other motion knob: a robot env change takes effect on restart."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    driver = _driver()
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "5")
    _session(driver, cycles=3)
    _age(driver, 3600.0)

    assert driver.expressive_motion_enabled is False


def test_the_shipped_hostapp_owner_is_untouched(monkeypatch):
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "5")
    driver = _driver()
    assert driver.motion_owner == "hostapp"
    assert driver.expressive_motion_enabled is True


# --- when it does fire ------------------------------------------------------------

def test_a_silent_ros_lane_hands_the_expressive_lanes_back(monkeypatch, caplog):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)

    assert driver.expressive_motion_enabled is False       # still inside the window
    _age(driver, 25.0)
    with caplog.at_level("WARNING", logger="emori.hostapp.driver"):
        assert driver.expressive_motion_enabled is True

    assert driver.counters["owner_fallback_episodes"] == 1
    assert any("expressive lanes back" in r.getMessage() for r in caplog.records)


def test_the_warning_is_once_per_episode_not_once_per_cycle(monkeypatch):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    _age(driver, 25.0)

    for _ in range(10):
        assert driver.expressive_motion_enabled is True
    assert driver.counters["owner_fallback_episodes"] == 1


def test_an_accepted_skill_request_takes_the_lanes_straight_back(monkeypatch, caplog):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    _age(driver, 25.0)
    assert driver.expressive_motion_enabled is True

    with caplog.at_level("INFO", logger="emori.hostapp.driver"):
        driver.note_skill_accepted()
    assert driver.expressive_motion_enabled is False
    assert any("ROS motion resumed" in r.getMessage() for r in caplog.records)

    # ...and a SECOND silence is a second episode, not a suppressed one.
    _age(driver, 25.0)
    assert driver.expressive_motion_enabled is True
    assert driver.counters["owner_fallback_episodes"] == 2


def test_the_window_is_measured_from_the_last_accepted_request(monkeypatch):
    """A beat resets the clock; a refusal or a drop never calls this at all."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    _age(driver, 15.0)
    driver.note_skill_accepted()
    _age(driver, 15.0)                       # 30 s into the session, 15 s since the beat

    assert driver.expressive_motion_enabled is False


def test_a_fresh_session_is_not_immediately_let_down(monkeypatch):
    """The silence is measured from the LATER of the session start and the last beat.

    Without that, a robot whose previous learner left an hour ago would fall back on the
    very first cycle of the next session -- before the ROS graph has been given a
    single chance to post anything for this person.
    """

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    driver.note_skill_accepted()
    driver._last_skill_accepted -= 3600.0           # the previous session, long ago

    _session(driver, cycles=1)                      # a new learner right now
    assert driver.expressive_motion_enabled is False


# --- the preconditions ------------------------------------------------------------

def test_nothing_falls_back_without_a_session(monkeypatch):
    """An idle robot between learners is not a failure to be recovered from."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "1")
    driver = _driver()
    assert driver.expressive_motion_enabled is False
    assert driver.counters["owner_fallback_episodes"] == 0


def test_nothing_falls_back_before_the_first_cycle(monkeypatch):
    """No cycles means no analysis stream, which means the ROS graph has nothing to
    have been silent ABOUT: a session whose camera has not produced a frame yet is not
    evidence that anything is wedged."""

    from emori_adapter.hostapp.driver import SessionInfo

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    _age(driver, 600.0)

    assert driver.tracker.cycles == 0
    assert driver.expressive_motion_enabled is False


def test_owner_none_never_falls_back(monkeypatch):
    """`none` is a deliberate "nothing moves", not a delegation that can fail."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "none")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "1")
    driver = _driver()
    _session(driver, cycles=3)
    _age(driver, 3600.0)

    assert driver.expressive_motion_enabled is False
    assert driver.counters["owner_fallback_episodes"] == 0


# --- what the lanes actually do while it is open ----------------------------------

def test_the_engagement_lane_submits_again_while_the_fallback_is_open(monkeypatch):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    suppressed = driver.counters["engagement_suppressed_by_owner"]
    assert suppressed >= 1

    _age(driver, 25.0)
    before = len(driver.player.batches)
    sig = DerivedSignals(timestamp="2026-09-16T00:00:00Z", session_id="s1",
                         person_id="p1", context=ContextSignal(escalation=0.1))
    driver._submit_engagement(R.calm_settle(), sig)

    assert len(driver.player.batches) == before + 1
    assert driver.counters["engagement_suppressed_by_owner"] == suppressed


def test_the_cue_lane_comes_back_too(monkeypatch):
    """Both gated lanes read the same property, so both have to return together."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    driver = _driver()
    _session(driver, cycles=2)
    _age(driver, 25.0)
    before = len(driver.player.batches)

    driver.on_utterance({"type": "pokamind_ai_utterance", "text": "A tense line.",
                         "seq": 1, "lang": "en", "cue_category": "tension"})
    assert len(driver.player.batches) > before


# --- the native transport disarms the window outright -----------------------------


def test_the_fallback_is_disarmed_under_the_native_transport(monkeypatch, caplog):
    """Under ``ros_native`` there is no /api/skill in the path at all.

    The window is measured from ``note_skill_accepted()``, which only the HTTP transport
    ever reaches: the native transport talks to the arbiter directly. So the liveness
    clock this knob reads stays None for ever and the window would open on EVERY
    session -- putting the hostapp's expressive lanes on the head beside a ROS graph
    that is working perfectly. A robot that sets both is asking for the one failure this
    dead-man's handle exists to avoid, so it is refused loudly instead of obeyed.
    """

    from emori_adapter.hostapp.driver import owner_fallback_s

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")
    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    monkeypatch.setenv("EMORI_SKILL_TRANSPORT", "ros_native")

    with caplog.at_level("WARNING", logger="emori.hostapp.driver"):
        assert owner_fallback_s() == 0.0
    assert any("IGNORED under" in r.getMessage() for r in caplog.records)

    driver = _driver()
    _session(driver, cycles=10)
    _age(driver, 3600.0)

    assert driver.expressive_motion_enabled is False
    assert driver.counters["owner_fallback_episodes"] == 0


def test_the_http_transport_keeps_the_window_it_has_always_had(monkeypatch):
    """The refusal is about the native transport, not about the knob."""

    from emori_adapter.hostapp.driver import owner_fallback_s

    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "20")
    for transport in ("", "hostapp_http", "log"):
        monkeypatch.setenv("EMORI_SKILL_TRANSPORT", transport)
        assert owner_fallback_s() == 20.0


def test_an_off_window_under_the_native_transport_says_nothing(monkeypatch, caplog):
    """The shipped v2 robot sets the transport and leaves the window alone; it must not
    grow a warning for a knob it never touched."""

    from emori_adapter.hostapp.driver import owner_fallback_s

    monkeypatch.delenv("EMORI_ROS_OWNER_FALLBACK_S", raising=False)
    monkeypatch.setenv("EMORI_SKILL_TRANSPORT", "ros_native")

    with caplog.at_level("WARNING", logger="emori.hostapp.driver"):
        assert owner_fallback_s() == 0.0
    assert caplog.records == []
