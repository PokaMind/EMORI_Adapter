"""The host volume backend: the daemon's Linux VolumeControl (ALSA path) for v2.

On the v1 robot the daemon answered ``{"volume": 82, "platform": "Linux", "device":
"Reachy Mini Audio"}`` -- the ``aplay -l`` card name, i.e. its amixer path. On v2
there is no daemon, so emori-admin moves the same mixer controls itself, with the
same card and control selection, and answers in the same shapes.
"""

from __future__ import annotations

import wave

import pytest

from emori_adapter.admin import audio as audio_mod
from emori_adapter.admin import mixer, probes
from test_admin_service_fakes import FakeAlsa, base_env, install


@pytest.fixture()
def alsa(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "host")
    install(monkeypatch)
    fake = FakeAlsa()
    monkeypatch.setattr(probes, "run", fake)
    monkeypatch.setattr(mixer, "daemon_test_sound", lambda: "/assets/impatient1.wav")
    return fake


def test_the_card_is_the_reachy_mini_audio_one_not_the_first():
    cards = mixer.parse_cards(
        "card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]\n"
        "card 2: Audio [Reachy Mini Audio], device 0: USB Audio [USB Audio]\n"
        "card 2: Audio [Reachy Mini Audio], device 1: USB Audio [USB Audio #1]\n")

    assert cards == [mixer.Card(0, "vc4-hdmi-0"), mixer.Card(2, "Reachy Mini Audio")]


def test_a_respeaker_card_counts_too_and_no_match_means_the_default_card(alsa):
    alsa.aplay = "card 1: Array [ReSpeaker 4 Mic Array (UAC1.0)], device 0: USB Audio [x]\n"
    assert mixer.find_card() == mixer.Card(1, "ReSpeaker 4 Mic Array (UAC1.0)")

    mixer.reset()
    alsa.aplay = "card 0: vc4hdmi0 [vc4-hdmi-0], device 0: x [y]\n"
    assert mixer.find_card() == mixer.DEFAULT_CARD


def test_controls_are_chosen_by_capability_in_order():
    from test_admin_service_fakes import AMIXER_SCONTENTS

    assert mixer.parse_controls(AMIXER_SCONTENTS, "pvolume") == ["PCM"]
    assert mixer.parse_controls(AMIXER_SCONTENTS, "cvolume") == ["Mic"]


@pytest.mark.parametrize("text,expected", [
    ("  Front Left: Playback 104 [82%] [-5.75dB] [on]\n", 82),
    ("  Front Left: Capture 19 [61%] [12.00dB] [on]\n", 61),
    ("  Mono: Capture 12 [40%] [on]\n", 40),          # the daemon would answer 500 here
    ("  Playback channels: Front Left - Front Right\n", -1),
    ("", -1),
])
def test_the_level_is_the_first_left_channel_percent(text, expected):
    assert mixer.parse_percent(text) == expected


def test_reading_the_speaker_is_the_daemons_answer(alsa):
    result = mixer.request("GET", "/api/volume/current")

    assert result.status == 200
    assert result.payload["volume"] == 82 and result.payload["device"] == "Reachy Mini Audio"
    assert set(result.payload) == {"volume", "platform", "device"}
    assert ["amixer", "-c", "2", "sget", "PCM,0"] in alsa.calls


def test_the_index_one_controls_are_raised_once_like_the_daemon_does(alsa):
    mixer.request("GET", "/api/volume/current")
    mixer.request("GET", "/api/volume/microphone/current")

    raised = [c for c in alsa.calls if c[4:6] in (["PCM,1", "100%"], ["Mic,1", "100%"])]
    assert raised == [["amixer", "-c", "2", "sset", "Mic,1", "100%"],
                      ["amixer", "-c", "2", "sset", "PCM,1", "100%"]]


def test_setting_moves_the_index_zero_control_and_plays_the_test_sound(alsa, monkeypatch):
    monkeypatch.setenv("EMORI_PLAY_TARGET", "alsa_output.usb-Reachy")

    result = mixer.request("POST", "/api/volume/set", json_body={"volume": 35})

    assert result.ok and result.payload["volume"] == 35
    assert ["amixer", "-c", "2", "sset", "PCM,0", "35%"] in alsa.calls
    assert alsa.played() == [["pw-play", "--target=alsa_output.usb-Reachy",
                              "/assets/impatient1.wav"]]


def test_the_microphone_route_moves_the_capture_control_silently(alsa):
    mixer.request("POST", "/api/volume/microphone/set", json_body={"volume": 20})

    assert ["amixer", "-c", "2", "sset", "Mic,0", "20%"] in alsa.calls
    assert alsa.played() == []


def test_the_next_control_is_tried_when_the_first_refuses(alsa):
    alsa.scontents = ("Simple mixer control 'Speaker',0\n  Capabilities: pvolume\n"
                      "Simple mixer control 'PCM',0\n  Capabilities: pvolume\n")
    alsa.failing_controls = {"Speaker"}

    assert mixer.request("POST", "/api/volume/set", json_body={"volume": 50}).ok
    assert alsa.levels[("PCM", "0")] == 50


def test_no_usable_control_is_a_failure_not_a_silent_success(alsa):
    """The daemon's shell chain of zero commands 'succeeded'; this does not."""
    alsa.scontents = "Simple mixer control 'Auto Gain',0\n  Capabilities: pswitch\n"

    result = mixer.request("POST", "/api/volume/set", json_body={"volume": 50})

    assert result.status == 500 and result.error == "mixer_failed"


def test_no_alsa_tools_is_a_named_503(alsa):
    alsa.missing = {"aplay", "amixer"}

    result = mixer.request("GET", "/api/volume/current")

    assert result.status == 503 and result.error == "alsa_missing"
    assert audio_mod.speaker_get() is None


@pytest.mark.parametrize("unstartable", [{"aplay"}, {"amixer"}])
def test_alsa_tools_that_are_there_but_cannot_start_are_not_alsa_missing(alsa, unstartable):
    """os_error is not "not installed": readycheck must not send anyone to apt for it."""

    alsa.unstartable = set(unstartable)

    result = mixer.request("GET", "/api/volume/current")

    assert result.status == 503 and result.error == "mixer_failed"


@pytest.mark.parametrize("bad", [None, {}, {"volume": 101}, {"volume": -1},
                                 {"volume": True}, {"volume": "40"}])
def test_a_level_outside_the_scale_is_refused_like_the_daemons_422(alsa, bad):
    assert mixer.request("POST", "/api/volume/set", json_body=bad).status == 422


def test_the_test_sound_is_played_and_reports_like_the_daemon(alsa):
    result = mixer.request("POST", "/api/volume/test-sound")

    assert result.payload == {"status": "ok", "message": "Test sound played"}
    assert alsa.played() == [["pw-play", "/assets/impatient1.wav"]]


def test_a_sound_already_playing_is_the_daemons_busy_answer(alsa):
    assert mixer._play_lock.acquire(blocking=False)
    try:
        result = mixer.request("POST", "/api/volume/test-sound")
    finally:
        mixer._play_lock.release()

    assert result.ok and result.payload["status"] == "busy"
    assert alsa.played() == []


def test_no_pw_play_is_a_named_failure(alsa):
    alsa.missing = {"pw-play"}

    result = mixer.request("POST", "/api/volume/test-sound")

    assert result.status == 503 and result.error == "pw_play_missing"


def test_without_the_daemons_asset_a_tone_is_written_and_played(alsa, monkeypatch):
    monkeypatch.setattr(mixer, "daemon_test_sound", lambda: None)

    mixer.request("POST", "/api/volume/test-sound")

    path = alsa.played()[0][-1]
    with wave.open(path, "rb") as tone:
        assert tone.getnchannels() == 1 and tone.getsampwidth() == 2
        assert 0.2 < tone.getnframes() / tone.getframerate() < 0.5


def test_the_daemons_asset_is_found_where_a_reachy_mini_package_lives(monkeypatch, tmp_path):
    asset = tmp_path / "venvs" / "lib" / "python3.12" / "site-packages" / "reachy_mini" \
        / "assets" / "impatient1.wav"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"RIFF")
    monkeypatch.setattr(mixer.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(mixer, "_ASSET_GLOBS", (
        str(tmp_path / "venvs" / "lib" / "python3*" / "site-packages" / "reachy_mini"
            / "assets" / "impatient1.wav"),))

    assert mixer.daemon_test_sound() == str(asset)


def test_unknown_volume_routes_are_not_found(alsa):
    assert mixer.request("GET", "/api/volume/whatever").payload == {"detail": "Not Found"}


def test_the_api_on_the_host_backend_persists_what_the_mixer_accepted(alsa, tmp_path):
    from starlette.testclient import TestClient

    from emori_adapter.admin import server
    from emori_adapter.admin.state import AdminState
    from test_admin_service_fakes import PIN

    state = AdminState(str(tmp_path / "s.json"))
    client = TestClient(server.create_app(background=False, state=state))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204

    ok = client.post("/api/audio/mic", json={"volume": 44})
    alsa.missing = {"aplay", "amixer"}
    mixer.reset()
    refused = client.post("/api/audio/speaker", json={"volume": 90})

    assert ok.json() == {"ok": True, "volume": 44}
    assert refused.status_code == 502 and refused.json()["error"] == "alsa_missing"
    assert state.get() == {"mic_volume": 44.0}
