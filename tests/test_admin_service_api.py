"""The admin HTTP contract: allowlists, proxy failure shapes, aggregation.

The scenario every test here is written against is the bad day, not the good
one: the operator is holding a phone in front of a robot whose daemon is dead,
whose hostapp is wedged, and which has no route to the internet.  The service
must still answer every route with a shape the page can render — so "everything
is down" is the DEFAULT of the doubles in ``test_admin_service_fakes`` and the
tests below opt IN to things working.

The other half is the allowlists.  ``unit`` is the only request-supplied value
in this whole service that reaches an argv, and these tests pin that a rejected
unit never reaches a subprocess at all.
"""

from __future__ import annotations

import json
import time

import pytest

from emori_adapter.admin import audio as audio_mod
from emori_adapter.admin import probes, server
from emori_adapter.admin import system as system_mod
from emori_adapter.admin.state import AdminState
from test_admin_service_fakes import PIN, base_env, http_ok, http_status, install

DAEMON = "/api/daemon/status"
UNIT = "emori-hostapp.service"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123",
             state_file=str(tmp_path / "admin_state.json"))
    return install(monkeypatch)


@pytest.fixture()
def run(env):
    return env[0]


@pytest.fixture()
def http(env):
    return env[1]


def _client(state=None):
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False, state=state))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


# --------------------------------------------------------------------------
# every route is behind the cookie
# --------------------------------------------------------------------------
def test_every_api_route_needs_the_cookie(env):
    from starlette.testclient import TestClient

    app = server.create_app(background=False)
    client = TestClient(app)
    checked = 0
    for route in app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set())
        if not path.startswith("/api/") or path in ("/api/login", "/api/logout"):
            continue
        for method in sorted(methods & {"GET", "POST"}):
            response = client.request(method, path)
            assert response.status_code == 401, (method, path)
            assert response.json()["error"] == "unauthorized"
            checked += 1
    assert checked >= 15  # the contract has this many gated routes


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------
def test_overview_with_every_sub_probe_down(env):
    """A robot with a dead daemon, a dead hostapp and no network still renders."""
    body = _client().get("/api/overview").json()

    assert set(body) == {"network", "services", "system", "hostapp", "daemon",
                         "pokamind", "lang", "audio", "addon"}
    assert body["network"] == {"mode": None, "ssid": None, "ip": None,
                               "signal": None, "internet": False}
    assert body["services"] == []
    assert body["hostapp"] == {"ok": False, "healthz": None}
    assert body["daemon"] == {"ok": False}
    assert body["pokamind"] == {"api_ok": False, "token": None}
    assert body["lang"] is None and body["addon"] is None
    assert body["audio"] == {"speaker": None, "mic": None}
    assert set(body["system"]) == {"uptime_s", "cpu_pct", "mem_pct", "disk_pct",
                                   "temp_c", "commit"}


def test_overview_threads_every_live_value_through(env, run, http):
    http.add("GET", "/wifi/status", http_ok(
        {"mode": "wlan", "known_networks": ["example"], "connected_network": "example"}))
    http.add("GET", DAEMON, http_ok({"state": "running"}))
    http.add("GET", "/healthz", http_ok({"status": "ok", "demo": False}))
    http.add("GET", "/api/admin/summary", http_ok({"lang": "ca"}))
    http.add("GET", "/api/admin/pokamind", http_ok({
        "configured": True, "credential": "robot_token", "robot_id": "reception-01",
        "mismatch": False, "restart_pending": False,
        "last_whoami": {"ok": True, "code": "ok"}}))
    http.add("GET", "/api/volume/current", http_ok({"volume": 82}))
    http.add("GET", "/api/volume/microphone/current", http_ok({"volume": 30}))
    http.add("GET", "/", http_ok({}))  # POKAMIND_BASE root
    run.add(["nmcli", "-t", "-f", "CONNECTIVITY"], stdout="full\n")
    run.add(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"],
            stdout="wlan0:wifi:connected:example\nlo:loopback:unmanaged:\n")
    run.add(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show"],
            stdout="IP4.ADDRESS[1]:192.168.1.42/24\n")
    run.add(["nmcli", "-t", "-f", "ACTIVE,SIGNAL"], stdout="no:41\nyes:73\n")
    run.add(["systemctl", "show"],
            stdout="Id=%s\nActiveState=active\nSubState=running\n"
                   "ActiveEnterTimestamp=Mon 2026-08-04 09:00:00 UTC\n" % UNIT)

    body = _client().get("/api/overview").json()

    assert body["network"] == {"mode": "wlan", "ssid": "example", "ip": "192.168.1.42",
                               "signal": 73, "internet": True}
    assert body["daemon"]["ok"] is True
    assert body["hostapp"]["ok"] is True
    assert body["pokamind"] == {
        "api_ok": True,
        # The Home dot: reduced from the hostapp's status, never the token itself.
        "token": {"credential": "robot_token", "robot_id": "reception-01",
                  "mismatch": False, "restart_pending": False, "verified": True}}
    assert body["lang"] == "ca"
    assert body["audio"] == {"speaker": 82, "mic": 30}
    assert body["services"][0]["unit"] == UNIT
    assert body["services"][0]["active"] == "active"


def test_overview_is_cached_so_two_phones_do_not_double_the_load(env, http):
    client = _client()

    client.get("/api/overview")
    client.get("/api/overview")

    assert len(http.called("GET", "/wifi/status")) == 1


def test_a_sub_probe_that_raises_only_greys_out_its_own_tile(env, monkeypatch):
    monkeypatch.setattr(system_mod, "info", lambda: 1 / 0)

    body = _client().get("/api/overview").json()

    assert body["system"] == {key: None for key in
                              ("uptime_s", "cpu_pct", "mem_pct", "disk_pct",
                               "temp_c", "commit")}
    assert "network" in body


def test_lang_is_not_asked_for_when_no_token_is_configured(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "s.json"))  # no EMORI_ADMIN_TOKEN
    _, http = install(monkeypatch)

    assert _client().get("/api/overview").json()["lang"] is None
    assert http.called("GET", "/api/admin/summary") == []


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def test_network_status_reports_the_daemon_being_down_structurally(env):
    response = _client().get("/api/network/status")
    body = response.json()

    # 502, not 200: the page's one fetch helper branches on the status line, so
    # a failure served at 200 is a failure the operator is never shown.
    assert response.status_code == 502
    assert body["ok"] is False
    assert body["error"] == "daemon_down"
    assert body["mode"] is None


def test_scan_gets_the_twenty_second_budget(env, http):
    http.add("POST", "/wifi/scan_and_list", http_ok(["example", "example-guest", ""]))

    body = _client().post("/api/network/scan").json()

    assert body == {"ok": True, "ssids": ["example", "example-guest"]}
    assert http.called("POST", "/wifi/scan_and_list")[0]["timeout_s"] == probes.SCAN_TIMEOUT_S


def test_scan_with_the_daemon_down_is_a_structured_failure(env):
    response = _client().post("/api/network/scan")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "daemon_down", "status": 0}


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/network/status", None),
    ("POST", "/api/network/scan", None),
    ("POST", "/api/network/connect", {"ssid": "example", "password": "hunter2hunter2"}),
    ("POST", "/api/network/forget", {"ssid": "example"}),
    ("POST", "/api/network/hotspot", None),
    ("GET", "/api/network/error", None),
    ("GET", "/api/services", None),
])
def test_no_network_route_reports_a_rejected_change_as_a_success(env, method, path,
                                                                 body):
    """The sharp case: "Start hotspot now" against a dead or busy daemon.

    At HTTP 200 the page's api() resolves, run() reports success, and the panel
    tells the operator "join <ap_ssid> from your phone and open
    http://10.42.0.1:8070" -- sending them off the site Wi-Fi after a mutation
    the daemon never accepted, with the robot exactly where it was.
    """
    client = _client()

    response = client.request(method, path, json=body)

    assert response.status_code == 502
    assert response.json()["ok"] is False


def test_a_busy_daemon_is_a_failure_too_not_an_accepted_change(env, http):
    """The daemon 409s every /wifi/* mutation while its own busy lock is held --
    including one this service's own watchdog took."""
    http.add("POST", "/wifi/setup_hotspot", http_status(409))

    response = _client().post("/api/network/hotspot")

    assert response.status_code == 502
    assert response.json()["error"] == "http_409"


@pytest.mark.parametrize("bad", [None, "", "x" * 33, "example\nssid", 7])
def test_a_bad_ssid_never_reaches_the_daemon(env, http, bad):
    response = _client().post("/api/network/connect",
                              json={"ssid": bad, "password": "hunter2hunter2"})

    assert response.status_code == 400
    assert response.json()["error"] == "bad_ssid"
    assert http.called("POST", "/wifi/connect") == []


@pytest.mark.parametrize("bad", ["short", "x" * 64, 12345])
def test_a_password_that_cannot_be_a_wpa_key_is_refused(env, http, bad):
    response = _client().post("/api/network/connect",
                              json={"ssid": "example", "password": bad})

    assert response.status_code == 400
    assert http.called("POST", "/wifi/connect") == []


def test_connect_forwards_credentials_as_url_params_only(env, http):
    http.add("POST", "/wifi/connect", http_ok(None))

    body = _client().post("/api/network/connect",
                          json={"ssid": "example", "password": "correct horse"}).json()

    call = http.called("POST", "/wifi/connect")[0]
    assert call["params"] == {"ssid": "example", "password": "correct horse"}
    assert call["json"] is None
    # "accepted", never "connected": the daemon connects in a background thread.
    assert body == {"ok": True, "accepted": True}


def test_an_open_network_may_be_joined_without_a_password(env, http):
    http.add("POST", "/wifi/connect", http_ok(None))

    assert _client().post("/api/network/connect", json={"ssid": "guest"}).json()["ok"]
    assert http.called("POST", "/wifi/connect")[0]["params"]["password"] == ""


def test_a_failed_connect_never_echoes_the_password_back(env, http):
    http.add("POST", "/wifi/connect", http_status(409))

    body = _client().post("/api/network/connect",
                          json={"ssid": "example", "password": "s3cret-passphrase"}).text

    assert "s3cret-passphrase" not in body


def test_hotspot_uses_the_configured_ap_identity(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "s.json"))
    monkeypatch.setenv("EMORI_AP_SSID", "emori-setup")
    monkeypatch.setenv("EMORI_AP_PASSWORD", "test-ap-passphrase")
    run, http = install(monkeypatch)
    http.add("POST", "/wifi/setup_hotspot", http_ok(None))
    run.add(["nmcli", "-t", "-g", "802-11-wireless.ssid"], stdout="emori-setup\n")

    body = _client().post("/api/network/hotspot").json()

    assert http.called("POST", "/wifi/setup_hotspot")[0]["params"] == {
        "ssid": "emori-setup", "password": "test-ap-passphrase"}
    assert body["ap_ssid"] == "emori-setup"


def test_network_error_surfaces_the_daemons_own_last_error(env, http):
    http.add("GET", "/wifi/error", http_ok({"error": "Secrets were required"}))

    assert _client().get("/api/network/error").json() == {
        "ok": True, "error_text": "Secrets were required"}


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------
def test_audio_snapshot_reports_levels_and_the_resolved_multipliers(env, http):
    http.add("GET", "/api/volume/current", http_ok({"volume": 70}))
    http.add("GET", "/api/volume/microphone/current", http_ok({"volume": 25}))
    http.add("GET", "/api/status", http_ok({"mic_profile": "idle"}))

    body = _client().get("/api/audio").json()

    assert body["speaker"] == 70 and body["mic"] == 25
    assert body["applied_profile"] == "idle"
    assert body["mic_session_mult"] == audio_mod.DEFAULT_SESSION_MULT
    assert body["mic_idle_mult"] == audio_mod.DEFAULT_IDLE_MULT
    assert body["persisted"] == {}


def test_setting_the_speaker_persists_it_for_the_next_boot(env, http, tmp_path):
    http.add("POST", "/api/volume/set", http_ok({"volume": 90}))
    state = AdminState(str(tmp_path / "state.json"))

    body = _client(state=state).post("/api/audio/speaker", json={"volume": 90}).json()

    assert body == {"ok": True, "volume": 90}
    assert http.called("POST", "/api/volume/set")[0]["json"] == {"volume": 90}
    # The whole point: a reboot re-applies this instead of leaving it at 0.
    assert state.get()["speaker_volume"] == 90


def test_a_level_the_daemon_refused_is_not_persisted(env, tmp_path):
    state = AdminState(str(tmp_path / "state.json"))

    response = _client(state=state).post("/api/audio/speaker", json={"volume": 90})

    assert response.status_code == 502
    assert response.json()["error"] == "daemon_down"
    assert state.get() == {}


@pytest.mark.parametrize("bad", [None, -1, 101, "loud", True, {}])
def test_a_volume_outside_the_daemons_scale_is_refused(env, http, bad):
    response = _client().post("/api/audio/speaker", json={"volume": bad})

    assert response.status_code == 400
    assert response.json()["error"] == "bad_volume"
    assert http.called("POST", "/api/volume/set") == []


def test_mic_volume_uses_the_microphone_route(env, http, tmp_path):
    http.add("POST", "/api/volume/microphone/set", http_ok({"volume": 40}))
    state = AdminState(str(tmp_path / "state.json"))

    _client(state=state).post("/api/audio/mic", json={"volume": 40})

    assert state.get()["mic_volume"] == 40


def test_test_sound_maps_a_dead_daemon_to_502(env):
    response = _client().post("/api/audio/test-sound")

    assert response.status_code == 502
    assert response.json()["error"] == "daemon_down"


def test_mic_profiles_persist_and_are_applied_to_the_live_profile(env, run, http,
                                                                  tmp_path):
    http.add("GET", "/api/status", http_ok({"mic_profile": "session"}))
    run.add(["wpctl", "set-volume"])
    state = AdminState(str(tmp_path / "state.json"))

    body = _client(state=state).post("/api/audio/mic-profiles",
                                     json={"session": 1.75, "idle": 0.4}).json()

    assert state.get() == {"mic_session_mult": 1.75, "mic_idle_mult": 0.4}
    # Felt immediately: the CURRENT profile's new multiplier goes straight to wpctl.
    assert run.argv_for("wpctl") == [
        ["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", "1.75"]]
    assert body["immediate"] == {"applied": True, "profile": "session", "volume": 1.75}


def test_mic_profiles_survive_a_dev_box_with_no_wpctl(env, http, tmp_path):
    http.add("GET", "/api/status", http_ok({"mic_profile": "idle"}))
    state = AdminState(str(tmp_path / "state.json"))

    body = _client(state=state).post("/api/audio/mic-profiles", json={"idle": 0.5}).json()

    assert body["ok"] is True
    assert body["immediate"] == {"applied": False, "reason": "missing", "profile": "idle"}
    assert state.get()["mic_idle_mult"] == 0.5


def test_mic_profiles_do_not_guess_when_the_hostapp_is_down(env, run, tmp_path):
    state = AdminState(str(tmp_path / "state.json"))

    body = _client(state=state).post("/api/audio/mic-profiles", json={"session": 2.0}).json()

    assert body["immediate"]["reason"] == "no_applied_profile"
    assert run.argv_for("wpctl") == []


def test_a_null_multiplier_drops_the_override(env, tmp_path):
    state = AdminState(str(tmp_path / "state.json"))
    state.update(mic_session_mult=2.5)

    _client(state=state).post("/api/audio/mic-profiles", json={"session": None})

    assert "mic_session_mult" not in state.get()


@pytest.mark.parametrize("bad", [-0.1, 4.1, "loud", True])
def test_a_multiplier_outside_the_wpctl_ceiling_is_refused(env, bad):
    response = _client().post("/api/audio/mic-profiles", json={"session": bad})

    assert response.status_code == 400


def test_an_empty_mic_profile_body_is_refused(env):
    assert _client().post("/api/audio/mic-profiles", json={}).status_code == 400


# --------------------------------------------------------------------------
# language
# --------------------------------------------------------------------------
def test_language_switch_is_a_pure_proxy_with_the_token_header(env, http):
    http.add("POST", "/api/admin/lang", http_ok({"ok": True, "lang": "ca"}))

    body = _client().post("/api/lang", json={"lang": "ca"}).json()

    call = http.called("POST", "/api/admin/lang")[0]
    assert call["json"] == {"lang": "ca"}
    assert call["headers"][server.TOKEN_HEADER] == "tok-123"
    assert body["lang"] == "ca"


def test_a_session_in_progress_comes_through_as_409_busy(env, http):
    """`lang_state.set()` alone is not a language switch; the hostapp refuses
    mid-session and the operator has to be told that, not shown an error."""
    http.add("POST", "/api/admin/lang", http_status(409, {"error": "busy"}))

    response = _client().post("/api/lang", json={"lang": "en"})

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "busy"}


def test_a_dead_hostapp_is_502_hostapp_down(env):
    response = _client().post("/api/lang", json={"lang": "en"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "hostapp_down"}


def test_the_hostapp_failing_closed_on_its_token_is_reported_as_such(env, http):
    http.add("POST", "/api/admin/lang", http_status(503))

    response = _client().post("/api/lang", json={"lang": "en"})

    assert response.status_code == 503
    assert response.json()["error"] == "hostapp_admin_disabled"


def test_a_rejected_token_is_not_reported_as_the_operators_fault(env, http):
    http.add("POST", "/api/admin/lang", http_status(403))

    response = _client().post("/api/lang", json={"lang": "en"})

    assert response.status_code == 502
    assert response.json()["error"] == "token_rejected"


@pytest.mark.parametrize("bad", ["fr", "", None, "CA", ["ca"]])
def test_only_the_two_supported_languages_are_accepted(env, http, bad):
    response = _client().post("/api/lang", json={"lang": bad})

    assert response.status_code == 400
    assert http.called("POST", "/api/admin/lang") == []


def test_lang_without_a_configured_token_is_503(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "s.json"))
    install(monkeypatch)

    response = _client().get("/api/lang")

    assert response.status_code == 503
    assert response.json()["error"] == "admin_token_unset"


def test_get_lang_returns_the_hostapp_summary(env, http):
    http.add("GET", "/api/admin/summary",
             http_ok({"lang": "en", "mic_profile": "idle", "session_active": False}))

    body = _client().get("/api/lang").json()

    assert body == {"ok": True, "lang": "en", "mic_profile": "idle",
                    "session_active": False}


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------
def test_services_lists_every_allowlisted_unit_from_one_systemctl_call(env, run):
    # The hub's unit is not listed while the host app runs the gateway (and the reverse):
    # the one its condition skips is inactive on purpose.
    units = [u for u in system_mod.ALLOWED_UNITS if u != "emori-hub.service"]
    run.add(["systemctl", "show"], stdout="\n\n".join(
        "Id=%s\nActiveState=active\nSubState=running\nActiveEnterTimestamp=T%d" % (u, i)
        for i, u in enumerate(units)))

    body = _client().get("/api/services").json()

    assert [row["unit"] for row in body["services"]] == units
    assert body["services"][0]["since"] == "T0"
    assert len(run.argv_for("systemctl", "show")) == 1


def test_a_unit_missing_from_this_machine_is_still_reported(env, run):
    run.add(["systemctl", "show"], stdout="Id=%s\nActiveState=inactive\n" % UNIT)

    rows = {row["unit"]: row for row in _client().get("/api/services").json()["services"]}

    assert rows["emori-camera-bridge.service"]["active"] == "unknown"


@pytest.mark.parametrize("unit", [
    "sshd.service", "emori-hostapp.service; rm -rf /", "", None,
    "../../etc/systemd/system/x.service", "emori-hostapp",
])
def test_a_unit_outside_the_allowlist_never_reaches_a_subprocess(env, run, unit):
    response = _client().post("/api/services/action",
                              json={"unit": unit, "action": "restart"})

    assert response.status_code == 400
    assert response.json()["error"] == "unit_not_allowed"
    assert run.argv_for("sudo") == []


@pytest.mark.parametrize("action", ["reload", "mask", "", None, "restart;poweroff"])
def test_an_action_outside_the_allowlist_is_refused(env, run, action):
    response = _client().post("/api/services/action",
                              json={"unit": UNIT, "action": action})

    assert response.status_code == 400
    assert run.argv_for("sudo") == []


def test_restarting_a_managed_unit_uses_a_sudo_argv_list(env, run):
    run.add(["sudo", "systemctl", "restart", UNIT])

    body = _client().post("/api/services/action",
                          json={"unit": UNIT, "action": "restart"}).json()

    assert body == {"ok": True, "unit": UNIT, "action": "restart", "deferred": False}
    assert run.argv_for("sudo") == [["sudo", "systemctl", "restart", UNIT]]


def test_the_admin_service_may_restart_itself_but_never_stop_itself(env, run):
    """A stop from the page you are holding is a one-way door on a screenless robot."""
    response = _client().post("/api/services/action",
                              json={"unit": system_mod.SELF_UNIT, "action": "stop"})

    assert response.status_code == 400
    assert response.json()["error"] == "self_stop_refused"
    assert run.argv_for("sudo") == []


def test_a_self_restart_is_deferred_so_the_answer_reaches_the_phone(env, run,
                                                                    monkeypatch):
    monkeypatch.setattr(system_mod, "SELF_ACTION_DELAY_S", 0.01)

    body = _client().post("/api/services/action",
                          json={"unit": system_mod.SELF_UNIT,
                                "action": "restart"}).json()

    assert body["ok"] is True and body["deferred"] is True
    _wait_for(run, "sudo", "systemctl", "restart", system_mod.SELF_UNIT)


def _wait_for(run, *prefix, timeout_s: float = 3.0):
    """The deferred commands run on a thread; give it a moment to land."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if run.ran(*prefix):
            return
        time.sleep(0.01)
    raise AssertionError("deferred command never ran: %s" % (prefix,))


def test_a_failing_systemctl_is_502_with_its_own_stderr(env, run):
    run.add(["sudo", "systemctl", "restart", UNIT], returncode=1,
            stderr="Failed to restart: Unit not found.\n")

    response = _client().post("/api/services/action",
                              json={"unit": UNIT, "action": "restart"})

    assert response.status_code == 502
    assert "Unit not found" in response.json()["detail"]


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
def test_logs_read_the_journal_without_sudo(env, run):
    run.add(["journalctl"], stdout="2026-08-04T09:00:00 line one\nline two\n")

    body = _client().get("/api/logs?unit=%s&lines=200" % UNIT).json()

    assert body["lines"] == ["2026-08-04T09:00:00 line one", "line two"]
    argv = run.argv_for("journalctl")[0]
    assert argv[:1] != ["sudo"]
    assert argv == ["journalctl", "-u", UNIT, "-n", "200", "--no-pager",
                    "-o", "short-iso"]


@pytest.mark.parametrize("asked,used", [(99999, "1000"), (0, "1"), (-5, "1"),
                                        (350, "350")])
def test_the_line_count_is_clamped(env, run, asked, used):
    run.add(["journalctl"], stdout="")

    _client().get("/api/logs?unit=%s&lines=%d" % (UNIT, asked))

    assert run.argv_for("journalctl")[0][4] == used


def test_logs_for_a_foreign_unit_are_refused_before_any_subprocess(env, run):
    response = _client().get("/api/logs?unit=sshd.service")

    assert response.status_code == 400
    assert run.argv_for("journalctl") == []
    # The 400 names the units that ARE readable, so the UI can offer them.
    assert UNIT in response.json()["units"]


def test_a_journal_this_process_cannot_read_is_a_structured_502(env, run):
    run.add(["journalctl"], returncode=1, stderr="No journal files were opened")

    response = _client().get("/api/logs?unit=%s" % UNIT)

    assert response.status_code == 502
    assert response.json()["ok"] is False


# --------------------------------------------------------------------------
# system + power
# --------------------------------------------------------------------------
def test_system_reports_what_it_can_measure_on_any_platform(env):
    body = _client().get("/api/system").json()

    assert body["ok"] is True
    for key in ("uptime_s", "cpu_pct", "mem_pct", "disk_pct", "temp_c", "commit",
                "loadavg", "python", "hostname"):
        assert key in body


def test_the_deployed_commit_comes_from_git_when_git_is_there(env, run):
    run.add(["git", "-C"], stdout="597abc1-dirty\n")

    assert _client().get("/api/system").json()["commit"] == "597abc1-dirty"


@pytest.mark.parametrize("route,action", [("reboot", "reboot"), ("poweroff", "poweroff")])
def test_power_actions_are_deferred_so_the_page_gets_its_answer(env, run, monkeypatch,
                                                                route, action):
    """The response has to reach the phone BEFORE the machine goes away, or
    every reboot looks like a network error to the person who asked for it."""
    monkeypatch.setattr(system_mod, "SELF_ACTION_DELAY_S", 0.01)

    body = _client().post("/api/system/%s" % route).json()

    assert body == {"ok": True, "action": action, "deferred": True}
    _wait_for(run, "sudo", "systemctl", action)
    # ...and it is the double, never the real thing: this suite may be run ON
    # the robot, where a leaked `sudo systemctl poweroff` would end the session.
    assert run.argv_for("sudo", "systemctl", action) == [
        ["sudo", "systemctl", action]]


# --------------------------------------------------------------------------
# readycheck (route mapping; the runner has its own tests)
# --------------------------------------------------------------------------
class _StubRunner:
    def __init__(self, **kwargs):
        self.starts = 0

    def start(self):
        self.starts += 1
        if self.starts > 1:
            return {"ok": False, "error": "already_running", "started_at": 1.0}
        return {"ok": True, "running": True, "started_at": 1.0}

    def status(self):
        return {"running": False, "started_at": 1.0,
                "result": {"gates": [{"name": "admin", "status": "PASS"}],
                           "exit_code": 0, "counts": {"PASS": 1}},
                "error": None}


def test_readycheck_is_single_flight(env, monkeypatch):
    monkeypatch.setattr(system_mod, "ReadycheckRunner", _StubRunner)
    client = _client()

    assert client.post("/api/readycheck/run").status_code == 200
    second = client.post("/api/readycheck/run")
    assert second.status_code == 409
    assert second.json()["error"] == "already_running"


def test_readycheck_status_carries_the_parsed_gates(env, monkeypatch):
    monkeypatch.setattr(system_mod, "ReadycheckRunner", _StubRunner)

    body = _client().get("/api/readycheck/status").json()

    assert body["running"] is False
    assert body["result"]["gates"][0]["status"] == "PASS"


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------
def test_camera_snapshot_proxies_the_jpeg(env, http):
    http.add("GET", "/api/qr-frame",
             http_ok(None, content=b"\xff\xd8jpegbytes", content_type="image/jpeg"))

    response = _client().get("/api/camera/snapshot")

    assert response.status_code == 200
    assert response.content == b"\xff\xd8jpegbytes"
    assert response.headers["content-type"].startswith("image/jpeg")


@pytest.mark.parametrize("status,error", [(403, "debug_disabled"), (404, "no_frame")])
def test_camera_refusals_are_passed_through_verbatim(env, http, status, error):
    http.add("GET", "/api/qr-frame", http_status(status))

    response = _client().get("/api/camera/snapshot")

    assert response.status_code == status
    assert response.json()["error"] == error


def test_camera_with_the_hostapp_down_is_502(env):
    response = _client().get("/api/camera/snapshot")

    assert response.status_code == 502
    assert response.json()["error"] == "hostapp_down"


# --------------------------------------------------------------------------
# nothing is cached in a phone's back/forward store
# --------------------------------------------------------------------------
def test_pages_are_served_no_store(env):
    client = _client()

    assert client.get("/").headers["cache-control"] == "no-store"


def test_the_json_contract_is_json_serialisable(env):
    """Everything a route returns must survive json.dumps -- a stray dataclass
    would 500 the page instead of the peer that was actually down."""
    client = _client()

    for path in ("/api/overview", "/api/network/status", "/api/audio",
                 "/api/services", "/api/system", "/api/readycheck/status"):
        json.dumps(client.get(path).json())
