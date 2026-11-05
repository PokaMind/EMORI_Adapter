"""Session start/end must move the mic gain AND lift the robot's gaze.

B1: the capture source is shared, so the level has to follow the mode -- ``session``
while a learner is standing in front of the robot, ``idle`` otherwise
(see mic_profile.py for the measured numbers).
B2: the head must come UP to face a standing person when the session opens, instead
of holding wherever the last mode (or a human's push-turn) left it.
"""

from __future__ import annotations

import pytest

from emori_adapter.hostapp.extensions import HostExtension


@pytest.fixture(autouse=True)
def _pitch_from_the_default(monkeypatch):
    """A developer's own .env must not decide where "up" is; the tuning test sets it."""
    monkeypatch.delenv("EMORI_ATTEND_UP_PITCH_DEG", raising=False)


@pytest.fixture(autouse=True)
def _badges_on_the_sample_roster(sample_roster):
    """These tests scan ``EMORI:anna-001``: the shipped roster example lists nobody, so the
    host app reads a copy of the test-only sample roster (tests/conftest.py)."""


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


class _RecordingMic:
    """Records the profile TRANSITIONS the hostapp asks for.

    Levels are pinned in tests/test_mic_profile.py; what matters here is which
    profile is asserted at which lifecycle moment.
    """

    def __init__(self) -> None:
        self.profiles: list[str] = []

    def set_profile(self, profile: str) -> bool:
        self.profiles.append(profile)
        return True

    @property
    def profile(self) -> str | None:
        return self.profiles[-1] if self.profiles else None


def _client(monkeypatch, player, mic):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    monkeypatch.delenv("EMORI_SCAN_GREETING", raising=False)
    app = server._build_app(
        overrides={
            "robot_client": DryRunRobotClient(),
            "player": player,
            "mic_profile": mic,
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "extension": HostExtension(),
            "on_session_end": None,
            "tts": None,
            "stt": None,
        }
    )
    return TestClient(app)


def _open_session(client):
    """Badge -> SSO mint -> the /session page load that starts the session."""
    minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
    assert minted.status_code == 200
    opened = client.get(minted.json()["session_url"])
    assert opened.status_code == 200


def _reasons(player: _RecordingPlayer) -> list[str]:
    return [intent.reason for batch in player.submitted for intent in batch]


# --- B1: mode-scoped capture gain -------------------------------------------


def test_startup_asserts_the_idle_profile_once(monkeypatch):
    mic = _RecordingMic()
    with _client(monkeypatch, _RecordingPlayer(), mic):
        pass
    assert mic.profiles[0] == "idle"


def test_session_start_raises_the_capture_gain(monkeypatch):
    mic = _RecordingMic()
    with _client(monkeypatch, _RecordingPlayer(), mic) as client:
        _open_session(client)
        assert mic.profile == "session"


def test_session_completion_returns_the_gain_to_idle(monkeypatch):
    mic = _RecordingMic()
    with _client(monkeypatch, _RecordingPlayer(), mic) as client:
        _open_session(client)
        completed = client.post(
            "/api/cycle",
            json={"type": "pokamind_session_completed", "status": "completed"},
        )
        assert completed.json()["completed"] is True
        assert mic.profile == "idle"
    assert mic.profiles == ["idle", "session", "idle"]


def test_robot_side_end_also_returns_the_gain_to_idle(monkeypatch):
    """Walk-away / watchdog force-end finalize inside the driver, with no page
    involved. They must hand the mic back too, or an add-on's guards spend the
    rest of the day at session gain."""
    mic = _RecordingMic()
    with _client(monkeypatch, _RecordingPlayer(), mic) as client:
        _open_session(client)
        driver = client.app.state.driver
        with driver._lock:
            driver._finalize_locked(results=None)
        assert mic.profile == "idle"


# --- B2: look at the person on session start ---------------------------------


def test_session_start_looks_up_at_the_person(monkeypatch):
    player = _RecordingPlayer()
    with _client(monkeypatch, player, _RecordingMic()) as client:
        _open_session(client)
    lifted = [
        intent
        for batch in player.submitted
        for intent in batch
        if intent.head is not None and intent.head.pitch < 0
    ]
    assert lifted, "session start must leave the head looking UP at a standing person"
    # ...and it must be the LAST head pose of the opening sequence: greeting_gesture
    # settles at +4 (nose down), which on a table-top robot aims at a waist.
    last_head = [
        intent
        for batch in player.submitted
        for intent in batch
        if intent.head is not None
    ][-1]
    assert last_head.head.pitch < 0
    assert "look up" in last_head.reason


def test_look_up_is_a_repertoire_pose_on_the_legacy_submit_path(monkeypatch):
    """It must obey the same interlock as every other training motion: while an
    add-on mode owns the robot, legacy motion is disabled and this cannot fire."""
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    player = _RecordingPlayer()
    driver = LiveDriver(DryRunRobotClient(), player=player, fiware_factory=None)
    driver.set_legacy_motion_enabled(False)
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    assert player.submitted == []

    driver.set_legacy_motion_enabled(True)
    driver.start_session(SessionInfo(session_id="s2", person_id="p2", lang="en"))
    assert any("look up" in reason for reason in _reasons(player))


def test_look_up_pitch_is_env_tunable_and_clamped(monkeypatch):
    """Only the physical table height settles how far up 'at the person' is."""
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_ATTEND_UP_PITCH_DEG", "-20")
    player = _RecordingPlayer()
    driver = LiveDriver(DryRunRobotClient(), player=player, fiware_factory=None)
    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    assert player.submitted[-1][0].head.pitch == -20.0

    # A typo'd +40 would drive the Stewart platform to its limit and read as broken.
    monkeypatch.setenv("EMORI_ATTEND_UP_PITCH_DEG", "40")
    driver.start_session(SessionInfo(session_id="s2", person_id="p2", lang="en"))
    assert player.submitted[-1][0].head.pitch == 0.0

    monkeypatch.setenv("EMORI_ATTEND_UP_PITCH_DEG", "up")
    driver.start_session(SessionInfo(session_id="s3", person_id="p3", lang="en"))
    assert player.submitted[-1][0].head.pitch == -14.0
