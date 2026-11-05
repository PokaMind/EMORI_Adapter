"""Unregistered-badge cue: optional spoken cue + antenna ack for unregistered badges."""

from __future__ import annotations

import hashlib
import struct

import pytest

from emori_adapter.hostapp.extensions import HostExtension


def _pcm16_wav(*, sample_rate: int = 24000, frames: int = 2400) -> bytes:
    pcm = b"\x00\x00" * frames
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


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


def _client(monkeypatch, player):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
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


def test_unregistered_badge_feedback_enabled(monkeypatch, tmp_path):
    raw = _pcm16_wav()
    cue = tmp_path / "badge-cue.wav"
    cue.write_bytes(raw)
    monkeypatch.setenv("EMORI_UNREGISTERED_BADGE_FEEDBACK", "1")
    monkeypatch.setenv("EMORI_UNREGISTERED_BADGE_CUE_WAV", str(cue))
    monkeypatch.setenv(
        "EMORI_UNREGISTERED_BADGE_CUE_SHA256", hashlib.sha256(raw).hexdigest()
    )
    player = _RecordingPlayer()

    with _client(monkeypatch, player) as client:
        qr = client.post("/api/badge/claim", json={"payload": "unregistered-test-badge"})
        assert qr.status_code == 409
        assert qr.json() == {"error_code": "badge_unrecognized"}

        identify = client.post("/api/identify", json={"payload": "unregistered-test-badge"})
        assert identify.status_code == 409
        assert identify.json() == {"error_code": "badge_unrecognized"}

        served = client.get("/api/badge-cue")
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("audio/wav")
        assert served.content == raw

    reasons = [intent.reason for batch in player.submitted for intent in batch]
    assert any("unregistered badge" in reason for reason in reasons)


def test_unregistered_badge_feedback_default_off(monkeypatch):
    monkeypatch.delenv("EMORI_UNREGISTERED_BADGE_FEEDBACK", raising=False)
    player = _RecordingPlayer()

    with _client(monkeypatch, player) as client:
        qr = client.post("/api/badge/claim", json={"payload": "unregistered-test-badge"})
        assert qr.status_code == 409
        assert qr.json() == {"error_code": "training_unavailable"}
        assert client.get("/api/badge-cue").status_code == 404

    assert player.submitted == []


def test_enabled_flag_with_bad_digest_falls_back_to_opaque(monkeypatch, tmp_path):
    cue = tmp_path / "badge-cue.wav"
    cue.write_bytes(_pcm16_wav())
    monkeypatch.setenv("EMORI_UNREGISTERED_BADGE_FEEDBACK", "1")
    monkeypatch.setenv("EMORI_UNREGISTERED_BADGE_CUE_WAV", str(cue))
    monkeypatch.setenv("EMORI_UNREGISTERED_BADGE_CUE_SHA256", "0" * 64)
    player = _RecordingPlayer()

    with _client(monkeypatch, player) as client:
        qr = client.post("/api/badge/claim", json={"payload": "unregistered-test-badge"})
        assert qr.status_code == 409
        assert qr.json() == {"error_code": "training_unavailable"}
        assert client.get("/api/badge-cue").status_code == 404


# --- the cue file itself ---------------------------------------------------------------

def _load_cue(tmp_path, raw: bytes):
    from emori_adapter.hostapp.badge_cue import load_badge_cue_wav

    path = tmp_path / "cue.wav"
    path.write_bytes(raw)
    return load_badge_cue_wav(path, hashlib.sha256(raw).hexdigest())


def test_a_reviewed_cue_loads(tmp_path):
    raw = _pcm16_wav()
    assert _load_cue(tmp_path, raw) == raw


@pytest.mark.parametrize("damage", [
    lambda raw: raw[:200],                 # truncated: the header promises more
    lambda raw: raw + b"junk",             # a stale declared length
    lambda raw: _pcm16_wav(sample_rate=44100),
    lambda raw: b"not a wav at all",
])
def test_a_cue_that_is_not_a_complete_reviewed_wav_is_refused(tmp_path, damage):
    from emori_adapter.hostapp.badge_cue import BadgeCueError

    with pytest.raises(BadgeCueError):
        _load_cue(tmp_path, damage(_pcm16_wav()))


def test_a_cue_with_the_wrong_digest_is_refused(tmp_path):
    from emori_adapter.hostapp.badge_cue import BadgeCueError, load_badge_cue_wav

    path = tmp_path / "cue.wav"
    path.write_bytes(_pcm16_wav())
    with pytest.raises(BadgeCueError):
        load_badge_cue_wav(path, "0" * 64)
