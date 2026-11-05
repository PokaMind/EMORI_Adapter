"""Which backend answers Wi-Fi and volume: Pollen's daemon (v1) or emori-admin itself (v2).

On a v2 robot :8000 is the ROS node's REST shim, which has no /wifi or /api/volume
routes, so a "scan" sent there answers 404 and the robot cannot be joined to a site
Wi-Fi from a phone. One selector (``admin/host_backend.py``) decides, and every Wi-Fi/volume call goes through
it. These tests pin how it decides, that it heals by itself, and that the phone's
contract is the same either way.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from emori_adapter.admin import audio as audio_mod
from emori_adapter.admin import host_backend, netman, network, probes, server
from test_admin_service_fakes import (
    NOT_FOUND,
    PIN,
    V2_STATUS,
    FakeAlsa,
    FakeHttp,
    FakeNM,
    base_env,
    http_ok,
    http_status,
    install,
)

DAEMON_STATUS = {"state": "running", "no_media": True}  # Pollen's: no `backend` key
SCAN = ((" ", "Example Wi-Fi", "AA:BB", 80, "WPA2"), (" ", "Guest", "CC:DD", 40, ""))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    run, http = install(monkeypatch)
    clock = Clock()
    monkeypatch.setattr(host_backend.selector(), "_clock", clock)
    return run, http, clock


@pytest.fixture()
def v2(env, monkeypatch):
    """A v2 robot: the shim on :8000 (no /wifi, no /api/volume), nmcli, ALSA."""
    _, http, clock = env
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
    for method, path in (("GET", "/wifi/status"), ("POST", "/wifi/scan_and_list"),
                         ("GET", "/api/volume/current")):
        http.add(method, path, http_status(404, NOT_FOUND))
    nm, alsa = FakeNM(scan=SCAN), FakeAlsa()

    def run(argv, **kwargs):
        return (alsa if argv[0] in ("aplay", "amixer", "pw-play") else nm)(argv, **kwargs)

    monkeypatch.setattr(probes, "run", run)
    return nm, alsa, http, clock


def _daemon_calls(http, prefix):
    return [c for c in http.calls if c["path"].startswith(prefix)]


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------
def test_the_v2_shim_on_8000_selects_the_host(env):
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))

    decision = host_backend.decision()

    assert (decision.backend, decision.reason) == ("host", "v2_status")


def test_pollens_daemon_on_8000_keeps_the_daemon(env):
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))

    assert host_backend.decision().backend == "daemon"


def test_a_route_missing_404_selects_the_host_and_the_same_call_is_answered_by_it(env,
                                                                               monkeypatch):
    """No v2 marker needed: the first /wifi call that 404s is answered by nmcli at
    once, so the operator's very first tap works."""
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("POST", "/wifi/scan_and_list", http_status(404, NOT_FOUND))
    nm = FakeNM(scan=SCAN)
    monkeypatch.setattr(probes, "run", nm)

    assert network.scan() == {"ok": True, "ssids": ["Example Wi-Fi", "Guest"]}
    assert host_backend.decision().reason == "route_missing"
    network.scan()
    assert len(http.called("POST", "/wifi/scan_and_list")) == 1  # never asked again


def test_the_daemons_own_forget_404_is_an_answer_not_a_missing_router(env):
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("POST", "/wifi/forget", http_status(
        404, {"detail": "Network 'x' not found in saved networks."}))

    body = network.forget("x")

    # Its own word, not "http_404": the phone says the list is out of date, and never
    # mistakes this for a missing route.
    assert body == {"ok": False, "error": "not_saved", "status": 404}
    assert host_backend.current() == "daemon"


def test_a_404_on_volume_selects_the_host_too(env, monkeypatch):
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("GET", "/api/volume/current", http_status(404, NOT_FOUND))
    monkeypatch.setattr(probes, "run", FakeAlsa())

    assert audio_mod.speaker_get() == 82
    assert host_backend.current() == "host"


def test_forced_daemon_never_switches(env, monkeypatch):
    _, http, _ = env
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "daemon")
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
    http.add("GET", "/wifi/status", http_status(404, NOT_FOUND))

    body = network.status(with_internet=False)

    assert host_backend.current() == "daemon"
    assert body["ok"] is False and body["error"] == "route_missing"


def test_a_missing_route_reaches_the_phone_as_route_missing_never_http_404(env, monkeypatch):
    """The word the Network/Audio tabs turn into "what broke + how to get back in".

    Forced daemon on a shim robot is the one way left to get a missing route.
    """

    _, http, _ = env
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "daemon")
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
    for method, path in (("GET", "/wifi/status"), ("GET", "/wifi/error"),
                         ("POST", "/wifi/scan_and_list"), ("POST", "/wifi/setup_hotspot"),
                         ("GET", "/api/volume/current"),
                         ("GET", "/api/volume/microphone/current"),
                         ("POST", "/api/volume/set"), ("POST", "/api/volume/test-sound")):
        http.add(method, path, http_status(404, NOT_FOUND))
    client = _client()

    status = client.get("/api/network/status")
    error = client.get("/api/network/error")
    scan = client.post("/api/network/scan")
    hotspot = client.post("/api/network/hotspot")
    audio = client.get("/api/audio").json()
    speaker = client.post("/api/audio/speaker", json={"volume": 40})
    chime = client.post("/api/audio/test-sound")

    assert status.status_code == 502 and status.json()["error"] == "route_missing"
    for response in (error, scan, hotspot, speaker, chime):
        assert response.status_code == 502
        assert response.json() == {"ok": False, "error": "route_missing", "status": 404}
    assert audio["speaker"] is None and audio["speaker_error"] == "route_missing"
    assert audio["mic"] is None and audio["mic_error"] == "route_missing"


def test_forced_host_never_asks_8000(env, monkeypatch):
    run, http, _ = env
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "host")
    monkeypatch.setattr(probes, "run", FakeNM())

    network.status(with_internet=False)

    assert http.calls == []


def test_an_unknown_setting_warns_once_and_means_auto(env, monkeypatch, caplog):
    _, http, _ = env
    monkeypatch.setenv("EMORI_ADMIN_HOST_BACKEND", "nmcli")
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))

    with caplog.at_level(logging.WARNING, logger="emori.admin.backend"):
        assert host_backend.current() == "host"
        host_backend.current()

    assert caplog.text.count("is not one of auto|daemon|host") == 1


def test_the_answer_is_cached_for_a_minute_then_a_v1_rollback_heals(env, caplog):
    _, http, clock = env
    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))

    with caplog.at_level(logging.INFO, logger="emori.admin.backend"):
        assert host_backend.current() == "host"
        clock.now += 59
        assert host_backend.current() == "host"
        assert len(http.called("GET", "/api/daemon/status")) == 1
        # Somebody rolls the robot back to Pollen's daemon.
        http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
        clock.now += 2
        assert host_backend.current() == "daemon"

    lines = [r.getMessage() for r in caplog.records]
    assert len([m for m in lines if "Wi-Fi/volume backend: host" in m]) == 1
    assert any("Wi-Fi/volume backend: daemon (was host)" in m for m in lines)


def test_a_silent_8000_with_the_v2_fence_raised_is_a_host_robot(env, monkeypatch, tmp_path):
    flag = tmp_path / "V2_OWNS_THE_BUS"
    flag.write_text("")
    monkeypatch.setattr(host_backend.selector(), "fence_flag", str(flag))

    decision = host_backend.decision()

    assert (decision.backend, decision.reason, decision.decisive) == (
        "host", "fence_flag", True)


def test_a_silent_8000_without_the_flag_keeps_the_last_answer_and_asks_again_soon(env):
    _, http, clock = env

    first = host_backend.decision()
    assert (first.backend, first.decisive) == ("daemon", False)  # v1's "daemon_down"

    http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
    clock.now += host_backend.UNSURE_TTL_S + 0.1
    assert host_backend.current() == "host"

    # The node restarts: :8000 goes quiet. Wi-Fi must not fall back to a daemon
    # that is not there.
    del http._routes[("GET", "/api/daemon/status")]
    clock.now += host_backend.DECISION_TTL_S + 1
    decision = host_backend.decision()
    assert (decision.backend, decision.reason) == ("host", "unreachable")


def test_a_daemon_without_a_wifi_router_is_remembered_past_one_ttl(env, monkeypatch):
    _, http, clock = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    http.add("GET", "/wifi/status", http_status(404, NOT_FOUND))
    monkeypatch.setattr(probes, "run", FakeNM())

    network.wifi_status()
    clock.now += host_backend.DECISION_TTL_S + 1

    assert host_backend.current() == "host"
    clock.now += host_backend.ROUTE_MISSING_STICKY_S
    assert host_backend.current() == "daemon"


# --------------------------------------------------------------------------
# the missing-route case, end to end through the phone's API
# --------------------------------------------------------------------------
def _client():
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


def test_scan_on_a_v2_robot_answers_networks_not_404(v2):
    nm, _, http, _ = v2

    response = _client().post("/api/network/scan")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ssids": ["Example Wi-Fi", "Guest"]}
    assert http.called("POST", "/wifi/scan_and_list") == []


def test_joining_a_site_wifi_on_a_v2_robot(v2, monkeypatch):
    nm, _, http, _ = v2
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"
    client = _client()

    accepted = client.post("/api/network/connect",
                           json={"ssid": "Example Wi-Fi", "password": "correct horse"})
    status = client.get("/api/network/status").json()

    assert accepted.json() == {"ok": True, "accepted": True}
    assert status["mode"] == "wlan" and status["ssid"] == "Example Wi-Fi"
    assert status["known_networks"] == ["Example Wi-Fi"]
    assert _daemon_calls(http, "/wifi") == []


def test_the_network_status_contract_is_identical_on_both_backends(v2, monkeypatch):
    nm, _, _, _ = v2
    nm.add_profile("Example Wi-Fi")
    nm.active = "Example Wi-Fi"
    host_body = _client().get("/api/network/status").json()

    daemon_http = FakeHttp()
    daemon_http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    daemon_http.add("GET", "/wifi/status", http_ok(
        {"mode": "wlan", "known_networks": ["Example Wi-Fi"], "connected_network": "Example Wi-Fi"}))
    monkeypatch.setattr(probes, "request", daemon_http)
    host_backend.reset()
    daemon_body = _client().get("/api/network/status").json()

    assert set(host_body) == set(daemon_body)
    for key in ("ok", "error", "mode", "ssid", "known_networks"):
        assert host_body[key] == daemon_body[key], key


def test_audio_on_a_v2_robot(v2, tmp_path):
    _, alsa, http, _ = v2
    client = _client()

    snapshot = client.get("/api/audio").json()
    set_speaker = client.post("/api/audio/speaker", json={"volume": 40})
    test_sound = client.post("/api/audio/test-sound")

    assert snapshot["speaker"] == 82 and snapshot["mic"] == 61
    assert snapshot["speaker_error"] is None and snapshot["mic_error"] is None
    assert set_speaker.json() == {"ok": True, "volume": 40}
    assert ["amixer", "-c", "2", "sset", "PCM,0", "40%"] in alsa.calls
    assert test_sound.status_code == 200 and test_sound.json() == {"ok": True}
    assert alsa.played()  # really played this time (v1's --no-media daemon never did)
    assert _daemon_calls(http, "/api/volume") == []


def test_the_overview_reads_the_host_on_a_v2_robot(v2):
    nm, _, _, _ = v2
    nm.add_profile("Hotspot", hotspot=True)
    nm.active = "Hotspot"

    body = _client().get("/api/overview").json()

    assert body["network"]["mode"] == "hotspot"
    assert body["audio"] == {"speaker": 82, "mic": 61}
    assert body["daemon"] == {"ok": True}  # the shim says running


# --------------------------------------------------------------------------
# the background workers on v2
# --------------------------------------------------------------------------
def test_the_watchdog_reads_and_acts_on_the_host_on_v2(v2, monkeypatch):
    nm, _, http, _ = v2
    nm.add_profile("Hotspot", hotspot=True)  # disconnected: nothing active
    monkeypatch.setattr(network, "WATCHDOG_POLL_S", 0.001)
    monkeypatch.setenv("EMORI_ADMIN_WIFI_GRACE_S", "0.002")

    async def drive():
        task = asyncio.create_task(server.wifi_watchdog_task())
        for _ in range(400):
            await asyncio.sleep(0.005)
            if nm.active == "Hotspot":
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert nm.active == "Hotspot"
    assert ["sudo", "-n", "nmcli", "connection", "up", "id", "Hotspot"] in nm.calls
    assert _daemon_calls(http, "/wifi") == []


def test_the_boot_fallback_runs_on_a_host_robot_after_the_rebrand(v2, monkeypatch):
    nm, _, _, _ = v2
    order = []
    monkeypatch.setattr(netman, "ensure_wifi_on_startup",
                        lambda: order.append("ensure") or "hotspot")

    async def drive():
        done = asyncio.Event()
        task = asyncio.create_task(server.host_wifi_boot_task(done))
        await asyncio.sleep(0.05)
        assert order == []           # still waiting for the rebrand
        order.append("rebrand")
        done.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(drive())

    assert order == ["rebrand", "ensure"]


def test_the_boot_fallback_does_nothing_on_a_daemon_robot(env, monkeypatch):
    _, http, _ = env
    http.add("GET", "/api/daemon/status", http_ok(DAEMON_STATUS))
    called = []
    monkeypatch.setattr(netman, "ensure_wifi_on_startup", lambda: called.append(1))

    asyncio.run(server.host_wifi_boot_task(None))

    assert called == []


def test_the_boot_fallback_waits_for_8000_to_say_what_it_is(env, monkeypatch):
    _, http, clock = env
    monkeypatch.setattr(server, "HOST_WIFI_RESOLVE_POLL_S", 0.001)
    called = []
    monkeypatch.setattr(netman, "ensure_wifi_on_startup", lambda: called.append(1) or "x")
    polls = []
    real = host_backend.decision

    def decision():
        polls.append(1)
        if len(polls) == 3:  # the v2 node comes up on the third look
            http.add("GET", "/api/daemon/status", http_ok(V2_STATUS))
        clock.now += host_backend.UNSURE_TTL_S + 1
        return real()

    monkeypatch.setattr(host_backend, "decision", decision)

    asyncio.run(server.host_wifi_boot_task(None))

    assert len(polls) == 3 and called == [1]


def test_the_boot_fallback_gives_up_when_8000_never_answers(env, monkeypatch):
    monkeypatch.setattr(server, "HOST_WIFI_RESOLVE_S", 0.0)
    called = []
    monkeypatch.setattr(netman, "ensure_wifi_on_startup", lambda: called.append(1))

    asyncio.run(server.host_wifi_boot_task(None))

    assert called == []


def test_the_boot_fallback_task_is_started_with_the_service(env, monkeypatch):
    from starlette.testclient import TestClient

    names = []
    real_create_task = asyncio.create_task

    def spy(coro, *, name=None):
        names.append(name)
        return real_create_task(coro, name=name)

    monkeypatch.setattr(asyncio, "create_task", spy)
    with TestClient(server.create_app()):
        pass

    assert "admin-host-wifi-boot" in names and "admin-ap-rebrand" in names


def test_boot_audio_reapplies_through_the_host_without_a_chirp(v2, tmp_path):
    _, alsa, _, _ = v2
    from emori_adapter.admin.state import AdminState

    state = AdminState(str(tmp_path / "s.json"))
    state.update(speaker_volume=70, mic_volume=55)

    outcome = audio_mod.reapply_persisted(state)

    assert outcome == {"pending": False, "speaker": 70, "mic": 55}
    assert ["amixer", "-c", "2", "sset", "PCM,0", "70%"] in alsa.calls
    assert ["amixer", "-c", "2", "sset", "Mic,0", "55%"] in alsa.calls
    assert alsa.played() == []  # nobody near a booting robot hears a test sound
