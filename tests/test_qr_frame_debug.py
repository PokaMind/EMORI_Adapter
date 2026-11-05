"""See exactly what the scanner sees, without taking the camera away from it.

Diagnosing "the badge will not scan" on a screenless robot meant stopping the kiosk to free
/dev/video40 (v4l2loopback is exclusive_caps=1, so only one reader), grabbing frames, and
starting it again -- which takes the scanner down at precisely the moment someone is
holding a badge in front of it, and shows the RAW camera rather than the frame the decoder
was actually handed. This endpoint has the page publish its own latest frame instead.

Deployment-gated and memory-only, exactly like the other debug captures: a camera frame
is the most sensitive thing this robot holds, so it never reaches disk and only the most
recent one is kept.
"""

from __future__ import annotations



def _client(monkeypatch):
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    app = server._build_app(  # noqa: SLF001 - the app factory used by every route test
        overrides={
            "robot_client": DryRunRobotClient(),
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "tts": None,
            "stt": None,
        }
    )
    return TestClient(app)


JPEG = b"\xff\xd8\xff\xe0" + b"emori-test-frame" * 4 + b"\xff\xd9"


def test_frame_debug_is_disabled_by_default(monkeypatch):
    # POST is now ALWAYS accepted: it feeds the server-side zxing decoder (the second
    # reader that catches hand-held badges jsQR misses). Only RETENTION (GET) is the
    # sensitive part, and it stays debug-gated.
    monkeypatch.delenv("EMORI_QR_FRAME_DEBUG", raising=False)
    with _client(monkeypatch) as client:
        assert client.post("/api/qr-frame", content=JPEG).status_code == 200
        assert client.get("/api/qr-frame").status_code == 403


def test_frame_round_trips_when_enabled(monkeypatch):
    monkeypatch.setenv("EMORI_QR_FRAME_DEBUG", "1")
    with _client(monkeypatch) as client:
        assert client.post("/api/qr-frame", content=JPEG).status_code == 200
        got = client.get("/api/qr-frame")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/jpeg"
    assert got.content == JPEG


def test_only_the_latest_frame_is_kept(monkeypatch):
    # Memory-only and single-slot: a rolling buffer of camera frames is a liability, and
    # the only frame worth seeing is the one on screen right now.
    monkeypatch.setenv("EMORI_QR_FRAME_DEBUG", "1")
    with _client(monkeypatch) as client:
        client.post("/api/qr-frame", content=JPEG)
        client.post("/api/qr-frame", content=b"\xff\xd8second\xff\xd9")
        got = client.get("/api/qr-frame")
    assert got.content == b"\xff\xd8second\xff\xd9"


def test_absent_frame_is_404_not_a_stale_one(monkeypatch):
    monkeypatch.setenv("EMORI_QR_FRAME_DEBUG", "1")
    with _client(monkeypatch) as client:
        assert client.get("/api/qr-frame").status_code == 404


def test_oversized_frame_is_refused(monkeypatch):
    # The page is asked for a small JPEG; anything large is a bug or a leak, not a frame.
    monkeypatch.setenv("EMORI_QR_FRAME_DEBUG", "1")
    with _client(monkeypatch) as client:
        assert client.post("/api/qr-frame", content=b"x" * (3 * 1024 * 1024)).status_code == 413
