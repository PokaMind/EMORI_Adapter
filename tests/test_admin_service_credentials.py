"""The credentials only this robot knows: admin PIN, recovery hotspot, dashboard password.

They are generated at install and live in ``secrets/admin.env`` (0600), which outranks
``.env``. The phone can reveal or change each one, and every such action:

* needs the PIN typed AGAIN, counted against the login limiter (a wrong one is a 403,
  not a 401, which the page would read as "signed out");
* answers ``Cache-Control: no-store`` when it carries a value;
* leaves an event in the journal, never a value.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from emori_adapter import site_settings
from emori_adapter.admin import auth, network, server, site_apply
from emori_adapter.admin import system as system_mod
from test_admin_service_fakes import PIN, base_env, install

NEW_PIN = "135790"
AP_SSID = "Robot-Hotspot-01"
AP_PASSWORD = "hotspot-pass-8812"
DASH_PASSWORD = "dash-pass-3390-qq"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "admin_state.json"))
    monkeypatch.delenv("EMORI_DASH_PASSWORD", raising=False)
    run, http = install(monkeypatch)
    monkeypatch.setattr(site_settings, "base_dir", lambda root=None: tmp_path)
    deferred: list[list[str]] = []
    # The self-restart runs on a thread a moment later on the robot; here, record it.
    monkeypatch.setattr(system_mod, "_deferred",
                        lambda argv, delay_s=None: deferred.append(list(argv)))
    return {"run": run, "http": http, "deferred": deferred, "root": tmp_path}


def _client(watcher=None):
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False, apply_watcher=watcher))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


def _admin_env(root) -> dict:
    return site_settings.read_env_file(root / site_settings.ADMIN_SECRETS_FILE)


SELF_RESTART = ["sudo", "systemctl", "restart", "emori-admin.service"]


# --------------------------------------------------------------------------
# PIN change
# --------------------------------------------------------------------------
def test_a_pin_change_is_saved_in_force_and_restarts_the_service(env, caplog):
    admin_env = env["root"] / site_settings.ADMIN_SECRETS_FILE
    site_settings.write_env_file(admin_env, {"EMORI_AP_SSID": AP_SSID})
    client = _client()

    with caplog.at_level(logging.DEBUG):
        reply = client.post("/api/pin/change", json={"current": PIN, "new": NEW_PIN})

    assert reply.status_code == 200 and reply.json() == {"ok": True, "restarting": True}
    assert reply.headers["cache-control"] == "no-store"
    # Merged into what admin.env already held.
    assert _admin_env(env["root"]) == {"EMORI_AP_SSID": AP_SSID, "EMORI_ADMIN_PIN": NEW_PIN}
    assert os.environ["EMORI_ADMIN_PIN"] == NEW_PIN
    assert env["deferred"] == [SELF_RESTART]
    # This phone stays signed in; any other phone signs in again, with the new PIN.
    assert client.get("/api/system").status_code == 200
    from starlette.testclient import TestClient

    other = TestClient(server.create_app(background=False))
    assert other.post("/api/login", json={"pin": PIN}).status_code == 401
    assert other.post("/api/login", json={"pin": NEW_PIN}).status_code == 204
    lines = " ".join(r.getMessage() for r in caplog.records)
    assert "PIN changed" in lines
    assert PIN not in lines and NEW_PIN not in lines


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_admin_env_is_private(env):
    _client().post("/api/pin/change", json={"current": PIN, "new": NEW_PIN})

    path = env["root"] / site_settings.ADMIN_SECRETS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("body, status, error", [
    ({"current": "000000", "new": NEW_PIN}, 403, "bad_pin"),
    ({"new": NEW_PIN}, 403, "bad_pin"),
    ({"current": PIN, "new": "12345"}, 400, "pin_invalid"),
    ({"current": PIN, "new": "1234567"}, 400, "pin_invalid"),
    ({"current": PIN, "new": "12a456"}, 400, "pin_invalid"),
    ({"current": PIN, "new": 135790}, 400, "pin_invalid"),
    ({"current": PIN, "new": PIN}, 400, "pin_unchanged"),
])
def test_a_refused_pin_change_writes_nothing_and_restarts_nothing(env, body, status, error):
    reply = _client().post("/api/pin/change", json=body)

    assert (reply.status_code, reply.json()["error"]) == (status, error)
    assert not (env["root"] / site_settings.ADMIN_SECRETS_FILE).exists()
    assert os.environ["EMORI_ADMIN_PIN"] == PIN
    assert env["deferred"] == []


def test_a_pin_that_cannot_be_saved_leaves_the_old_one(env, monkeypatch):
    def fail(updates, root=None):
        raise OSError("read-only")

    monkeypatch.setattr(auth, "save_admin_secrets", fail)

    reply = _client().post("/api/pin/change", json={"current": PIN, "new": NEW_PIN})

    assert (reply.status_code, reply.json()["error"]) == (500, "pin_save_failed")
    assert os.environ["EMORI_ADMIN_PIN"] == PIN and env["deferred"] == []


def test_wrong_current_pins_count_against_the_login_limiter(env):
    client = _client()

    for _ in range(5):
        assert client.post("/api/pin/change",
                           json={"current": "000000", "new": NEW_PIN}).status_code == 403
    limited = client.post("/api/pin/change", json={"current": PIN, "new": NEW_PIN})

    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")
    assert os.environ["EMORI_ADMIN_PIN"] == PIN


def test_change_pin_itself(env):
    assert auth.change_pin("000000", NEW_PIN, root=env["root"]) == (False, "bad_pin")
    assert auth.change_pin(PIN, "x", root=env["root"]) == (False, "pin_invalid")
    assert auth.change_pin(PIN, PIN, root=env["root"]) == (False, "pin_unchanged")
    assert auth.change_pin(PIN, NEW_PIN, root=env["root"]) == (True, "ok")
    assert auth.configured_pin() == (NEW_PIN, "")


# --------------------------------------------------------------------------
# reveal
# --------------------------------------------------------------------------
def test_the_hotspot_is_revealed_after_the_pin_and_logged_without_values(env, monkeypatch,
                                                                         caplog):
    monkeypatch.setenv("EMORI_AP_SSID", AP_SSID)
    monkeypatch.setenv("EMORI_AP_PASSWORD", AP_PASSWORD)

    with caplog.at_level(logging.DEBUG):
        reply = _client().post("/api/credentials/reveal", json={"which": "ap", "pin": PIN})

    assert reply.status_code == 200
    assert reply.json() == {"ok": True, "which": "ap", "ssid": AP_SSID,
                            "password": AP_PASSWORD, "visible_s": 30}
    assert reply.headers["cache-control"] == "no-store"
    lines = " ".join(r.getMessage() for r in caplog.records)
    assert "REVEALED" in lines
    assert AP_PASSWORD not in lines and AP_SSID not in lines


def test_the_dashboard_password_is_revealed_too(env, monkeypatch):
    monkeypatch.setenv("EMORI_DASH_PASSWORD", DASH_PASSWORD)

    reply = _client().post("/api/credentials/reveal", json={"which": "dashboard", "pin": PIN})

    assert reply.json()["password"] == DASH_PASSWORD and reply.json()["visible_s"] == 30
    assert reply.headers["cache-control"] == "no-store"


def test_an_unset_credential_says_so(env):
    reply = _client().post("/api/credentials/reveal", json={"which": "dashboard", "pin": PIN})

    assert (reply.status_code, reply.json()["error"]) == (404, "credential_unset")


@pytest.mark.parametrize("which", [None, "pin", "token", 3])
def test_only_the_two_credentials_can_be_revealed(env, which):
    reply = _client().post("/api/credentials/reveal", json={"which": which, "pin": PIN})

    assert (reply.status_code, reply.json()["error"]) == (400, "bad_request")


def test_a_wrong_reveal_pin_is_a_403_and_counts(env, monkeypatch):
    monkeypatch.setenv("EMORI_DASH_PASSWORD", DASH_PASSWORD)
    client = _client()

    for _ in range(5):
        wrong = client.post("/api/credentials/reveal",
                            json={"which": "dashboard", "pin": "000000"})
        assert (wrong.status_code, wrong.json()["error"]) == (403, "bad_pin")
        assert DASH_PASSWORD not in wrong.text
    limited = client.post("/api/credentials/reveal", json={"which": "dashboard", "pin": PIN})

    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")
    assert DASH_PASSWORD not in limited.text


# --------------------------------------------------------------------------
# hotspot change
# --------------------------------------------------------------------------
def test_a_new_hotspot_is_saved_and_the_service_restarts_to_rebrand_it(env, caplog):
    with caplog.at_level(logging.DEBUG):
        reply = _client().post("/api/credentials/ap", json={
            "pin": PIN, "ssid": AP_SSID, "password": AP_PASSWORD})

    assert reply.status_code == 200 and reply.json() == {"ok": True, "restarting": True}
    assert reply.headers["cache-control"] == "no-store"
    assert _admin_env(env["root"]) == {"EMORI_AP_SSID": AP_SSID,
                                       "EMORI_AP_PASSWORD": AP_PASSWORD}
    assert network.ap_credentials() == (AP_SSID, AP_PASSWORD)
    assert env["deferred"] == [SELF_RESTART]
    lines = " ".join(r.getMessage() for r in caplog.records)
    assert AP_PASSWORD not in lines and AP_PASSWORD not in reply.text


def test_one_half_of_the_hotspot_can_change_alone(env, monkeypatch):
    monkeypatch.setenv("EMORI_AP_PASSWORD", AP_PASSWORD)

    _client().post("/api/credentials/ap", json={"pin": PIN, "ssid": AP_SSID, "password": ""})

    assert _admin_env(env["root"]) == {"EMORI_AP_SSID": AP_SSID}
    assert network.ap_credentials() == (AP_SSID, AP_PASSWORD)


@pytest.mark.parametrize("body, error", [
    ({"ssid": "x" * 33}, "bad_ssid"),
    ({"ssid": "é" * 17}, "bad_ssid"),          # 34 bytes
    ({"ssid": "line\nbreak"}, "bad_ssid"),
    ({"ssid": 7}, "bad_ssid"),
    ({"password": "short12"}, "bad_password"),
    ({"password": "p" * 64}, "bad_password"),
    ({"password": "pàssword-long"}, "bad_password"),
    ({"password": "tab\tinside1"}, "bad_password"),
    # Read back stripped: the hotspot would not be what the operator wrote down.
    ({"ssid": " Hotspot"}, "bad_ssid"),
    ({"password": "trailing-space "}, "bad_password"),
    ({}, "nothing_to_set"),
    ({"ssid": "", "password": None}, "nothing_to_set"),
])
def test_a_hotspot_that_cannot_work_is_refused(env, body, error):
    reply = _client().post("/api/credentials/ap", json={"pin": PIN, **body})

    assert (reply.status_code, reply.json()["error"]) == (400, error)
    assert not (env["root"] / site_settings.ADMIN_SECRETS_FILE).exists()
    assert env["deferred"] == []


def test_the_hotspot_needs_the_pin_again(env):
    reply = _client().post("/api/credentials/ap", json={"pin": "000000", "ssid": AP_SSID})

    assert (reply.status_code, reply.json()["error"]) == (403, "bad_pin")
    assert not (env["root"] / site_settings.ADMIN_SECRETS_FILE).exists()


def test_set_ap_credentials_itself(env):
    assert network.set_ap_credentials(None, None, root=env["root"]) == (False, "nothing_to_set")
    assert network.set_ap_credentials("", None, root=env["root"]) == (False, "bad_ssid")
    assert network.set_ap_credentials(None, "x" * 7, root=env["root"]) == (False, "bad_password")
    assert network.set_ap_credentials(AP_SSID, AP_PASSWORD, root=env["root"]) == (True, "ok")
    assert network.valid_ap_password("A" * 63) and not network.valid_ap_password("A" * 7)


# --------------------------------------------------------------------------
# dashboard password reset
# --------------------------------------------------------------------------
def test_a_dashboard_reset_answers_the_password_once_and_never_logs_it(env, caplog):
    watcher = site_apply.SiteApplyWatcher(fetch=lambda m, p: None, restart=lambda: {},
                                          idle_s=600)
    client = _client(watcher)

    with caplog.at_level(logging.DEBUG):
        first = client.post("/api/credentials/dashboard/reset", json={"pin": PIN})
        second = client.post("/api/credentials/dashboard/reset", json={"pin": PIN})

    body = first.json()
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert len(body["password"]) == 16 and body["visible_s"] == 30
    assert body["restart_pending"] is True
    assert second.json()["password"] != body["password"]
    assert _admin_env(env["root"]) == {"EMORI_DASH_PASSWORD": second.json()["password"]}
    assert os.environ["EMORI_DASH_PASSWORD"] == second.json()["password"]
    # The hostapp reads it at its start: the watcher applies it once the robot is idle.
    assert watcher.queued() == ["dashboard_password"]
    lines = " ".join(r.getMessage() for r in caplog.records)
    assert "RESET" in lines
    assert body["password"] not in lines and second.json()["password"] not in lines
    # It never restarts anything itself.
    assert env["deferred"] == [] and not env["run"].ran("sudo")


def test_a_dashboard_reset_needs_the_pin_again(env):
    reply = _client().post("/api/credentials/dashboard/reset", json={"pin": "999999"})

    assert (reply.status_code, reply.json()["error"]) == (403, "bad_pin")
    assert "password" not in reply.json()


# --------------------------------------------------------------------------
# the door
# --------------------------------------------------------------------------
_ROUTES = [("/api/pin/change", {"current": PIN, "new": NEW_PIN}),
           ("/api/credentials/reveal", {"which": "ap", "pin": PIN}),
           ("/api/credentials/ap", {"pin": PIN, "ssid": AP_SSID}),
           ("/api/credentials/dashboard/reset", {"pin": PIN})]


def test_every_credential_route_needs_the_cookie(env):
    from starlette.testclient import TestClient

    anonymous = TestClient(server.create_app(background=False))
    for path, body in _ROUTES:
        reply = anonymous.post(path, json=body)
        assert (reply.status_code, reply.json()["error"]) == (401, "unauthorized"), path
    assert not (env["root"] / site_settings.ADMIN_SECRETS_FILE).exists()


def test_no_credential_route_answers_a_foreign_page(env):
    client = _client()
    for path, body in _ROUTES:
        reply = client.post(path, json=body, headers={"Origin": "http://reachy-mini.local:8000"})
        assert (reply.status_code, reply.json()["error"]) == (403, "bad_origin"), path
    assert not (env["root"] / site_settings.ADMIN_SECRETS_FILE).exists()
    assert os.environ["EMORI_ADMIN_PIN"] == PIN
