"""Scan greeting: optional greeting gesture at successful badge identify."""

from __future__ import annotations

import pytest

from emori_adapter.hostapp.extensions import HostExtension


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


def _client(monkeypatch, player, *, env_flag: str | None):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    if env_flag is None:
        monkeypatch.delenv("EMORI_SCAN_GREETING", raising=False)
    else:
        monkeypatch.setenv("EMORI_SCAN_GREETING", env_flag)
    app = server._build_app(
        overrides={
            "robot_client": DryRunRobotClient(),
            "player": player,
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "extension": HostExtension(),
            "tts": None,
            "stt": None,
        }
    )
    return TestClient(app)


def test_scan_greeting_enabled_plays_on_successful_identify(monkeypatch):
    player = _RecordingPlayer()
    with _client(monkeypatch, player, env_flag="1") as client:
        response = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert response.status_code == 200
    reasons = [intent.reason for batch in player.submitted for intent in batch]
    assert any("badge scan greeting" in reason for reason in reasons)


def test_scan_greeting_default_off(monkeypatch):
    player = _RecordingPlayer()
    with _client(monkeypatch, player, env_flag=None) as client:
        response = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert response.status_code == 200
    assert player.submitted == []


def test_scan_greeting_suppressed_while_legacy_motion_disabled(monkeypatch):
    player = _RecordingPlayer()
    with _client(monkeypatch, player, env_flag="1") as client:
        client.app.state.driver.set_legacy_motion_enabled(False)
        response = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert response.status_code == 200
    assert player.submitted == []
