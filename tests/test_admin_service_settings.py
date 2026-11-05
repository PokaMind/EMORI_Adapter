"""emori-admin's Settings and Menu tabs: PIN-gated proxies to the hostapp.

The hostapp owns the robot-local settings (``GET/POST /api/admin/site``, loopback and
the shared token); emori-admin holds no credential of its own beyond that token, and
adds only what belongs to the phone:

* the admin-owned keys (PIN, hotspot, dashboard password) are answered HERE, as "set"
  and a source, never a value, and are never forwarded;
* a change to the Pokamind address, or the mail server needs the PIN typed again;
* Apply restarts the hostapp only when it says it is idle.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from emori_adapter import site_settings
from emori_adapter.admin import server
from test_admin_service_fakes import PIN, base_env, http_ok, http_status, install

SITE = {
    "keys": [
        {"key": "EMORI_SMTP_HOST", "owner": "hostapp", "value": "smtp.example.org",
         "set": True, "source": "admin", "secret": False, "editable": True,
         "restart": "hostapp", "running_differs": False, "problem": None},
        {"key": "EMORI_SMTP_PASS", "owner": "hostapp", "value": None, "set": True,
         "source": "admin", "secret": True, "editable": True, "restart": "hostapp",
         "running_differs": False, "problem": None},
        {"key": "EMORI_ADMIN_PIN", "owner": "admin", "value": None, "set": True,
         "source": "env", "secret": True, "editable": False, "restart": "admin",
         "running_differs": False, "problem": None},
        {"key": "EMORI_DASH_PASSWORD", "owner": "admin", "value": None, "set": True,
         "source": "admin", "secret": True, "editable": False, "restart": "hostapp",
         "running_differs": True, "problem": None},
    ],
    "restart_pending": True, "restart_reasons": ["settings_saved"], "invalid_running": {},
    "scope": {"status": "ok", "auto": True, "fetched_at": 1.0, "robot_id": "bench-01",
              "digest": "sha256:00", "contract": 2, "stale": False,
              "derived": {}, "ignored_keys": [],
              "last_sync_code": "ok", "last_sync_at": 1.0, "last_sync_reason": "boot"},
}
IDLE = {"lang": "ca", "allowed_langs": ["ca", "en"], "mic_profile": None,
        "session_active": False, "addon_mode": "idle", "idle": True}
HOSTAPP_RESTART = ["sudo", "systemctl", "restart", "emori-hostapp.service"]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "admin_state.json"))
    monkeypatch.delenv("EMORI_DASH_PASSWORD", raising=False)
    monkeypatch.delenv(site_settings.SOURCES_ENV, raising=False)
    run, http = install(monkeypatch)
    monkeypatch.setattr(site_settings, "base_dir", lambda root=None: tmp_path)
    return run, http


def _client():
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


# --------------------------------------------------------------------------
# GET /api/settings
# --------------------------------------------------------------------------
def test_settings_are_the_hostapp_s_with_the_admin_keys_answered_here(env, tmp_path):
    _, http = env
    http.add("GET", "/api/admin/site", http_ok(SITE))
    site_settings.write_env_file(tmp_path / site_settings.ADMIN_SECRETS_FILE,
                                 {"EMORI_AP_PASSWORD": "hotspot-pass-8812"})

    reply = _client().get("/api/settings")
    body = reply.json()
    rows = {row["key"]: row for row in body["keys"]}

    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    call = http.called("GET", "/api/admin/site")[0]
    assert call["headers"] == {"X-EMORI-Admin-Token": "tok-123"}
    assert call["timeout_s"] == server.POKAMIND_PROXY_TIMEOUT_S
    assert rows["EMORI_SMTP_HOST"] == SITE["keys"][0]          # the hostapp's, untouched
    # Every admin-owned key, answered by this service: set and source, never a value.
    for key in site_settings.admin_keys():
        assert rows[key]["owner"] == "admin" and rows[key]["value"] is None
        assert rows[key]["secret"] is True and rows[key]["editable"] is False
    assert rows["EMORI_ADMIN_PIN"]["set"] is True and rows["EMORI_ADMIN_PIN"]["source"] == "env"
    assert rows["EMORI_AP_PASSWORD"]["source"] == "admin"
    assert rows["EMORI_AP_SSID"]["set"] is False and rows["EMORI_AP_SSID"]["source"] is None
    # Only the hostapp knows whether IT runs the dashboard password on file.
    assert rows["EMORI_DASH_PASSWORD"]["running_differs"] is True
    assert "hotspot-pass-8812" not in reply.text and PIN not in reply.text
    assert set(body["auto_apply"]) >= {"enabled", "idle_s", "last_decision", "reasons"}


def test_a_hostapp_that_lists_no_admin_keys_still_gets_them_from_here(env):
    _, http = env
    http.add("GET", "/api/admin/site", http_ok({**SITE, "keys": SITE["keys"][:2]}))

    keys = [row["key"] for row in _client().get("/api/settings").json()["keys"]]

    assert set(site_settings.admin_keys()) <= set(keys)


def test_settings_with_the_hostapp_down_say_so(env):
    reply = _client().get("/api/settings")

    assert (reply.status_code, reply.json()["error"]) == (502, "hostapp_down")


# --------------------------------------------------------------------------
# POST /api/settings
# --------------------------------------------------------------------------
def test_a_save_is_forwarded_as_is_and_logged_by_name(env, caplog):
    _, http = env
    http.add("POST", "/api/admin/site", http_ok({"saved": ["EMORI_SMTP_HOST",
                                                           "EMORI_SMTP_PASS"],
                                                 "restart_required": ["EMORI_SMTP_HOST"]}))

    with caplog.at_level(logging.DEBUG):
        reply = _client().post("/api/settings", json={"EMORI_SMTP_HOST": "smtp.example.org",
                                                      "EMORI_SMTP_PASS": "smtp-pass-1177",
                                                      "pin": PIN})

    assert reply.status_code == 200 and reply.json()["saved"][0] == "EMORI_SMTP_HOST"
    assert reply.headers["cache-control"] == "no-store"
    call = http.called("POST", "/api/admin/site")[0]
    assert call["json"] == {"EMORI_SMTP_HOST": "smtp.example.org",
                            "EMORI_SMTP_PASS": "smtp-pass-1177"}
    assert call["timeout_s"] == server.POKAMIND_PROXY_TIMEOUT_S
    lines = " ".join(r.getMessage() for r in caplog.records)
    assert "EMORI_SMTP_PASS" in lines and "smtp-pass-1177" not in lines


@pytest.mark.parametrize("key", ["EMORI_ADMIN_PIN", "EMORI_AP_SSID", "EMORI_AP_PASSWORD",
                                 "EMORI_DASH_PASSWORD"])
def test_an_admin_owned_key_is_refused_and_never_forwarded(env, key):
    _, http = env

    reply = _client().post("/api/settings", json={key: "12345678", "pin": PIN})

    assert reply.status_code == 400
    assert reply.json() == {"ok": False, "error": "key_not_editable", "key": key,
                            "code": "key_wrong_owner"}
    assert http.called("POST", "/api/admin/site") == []


@pytest.mark.parametrize("body, error, code", [
    ({}, "invalid_body", None),
    ({"pin": PIN}, "invalid_body", None),
    ({"NOT_A_SETTING": "x"}, "key_not_editable", "key_unknown"),
    ({"lower_case": "x"}, "key_not_editable", "key_unknown"),
    ({"EMORI_SMTP_HOST": 5}, "invalid_value", "value_not_text"),
    ({"EMORI_SMTP_HOST": "x" * 20000}, "invalid_value", "value_not_text"),
])
def test_a_malformed_save_never_leaves_this_process(env, body, error, code):
    _, http = env

    reply = _client().post("/api/settings", json=body)

    assert (reply.status_code, reply.json()["error"]) == (400, error)
    assert reply.json().get("code") == code
    assert http.calls == [] or http.called("POST", "/api/admin/site") == []


@pytest.mark.parametrize("key, value", [("POKAMIND_BASE", "https://api.example.org"),
                                        # where the write-only SMTP password is sent
                                        ("EMORI_SMTP_HOST", "smtp.example.org"),
                                        ("EMORI_SMTP_PORT", "2525"),
                                        ("EMORI_SMTP_USER", "robot@example.org")])
def test_a_trust_setting_needs_the_pin_again(env, key, value):
    _, http = env
    http.add("POST", "/api/admin/site", http_ok({"saved": [key], "restart_required": [key]}))
    client = _client()

    missing = client.post("/api/settings", json={key: value})
    wrong = client.post("/api/settings", json={key: value, "pin": "000000"})
    right = client.post("/api/settings", json={key: value, "pin": PIN})

    assert (missing.status_code, missing.json()["error"]) == (403, "bad_pin")
    assert (wrong.status_code, wrong.json()["error"]) == (403, "bad_pin")
    assert right.status_code == 200
    calls = http.called("POST", "/api/admin/site")
    assert len(calls) == 1 and calls[0]["json"] == {key: value}   # the PIN stays here


def test_a_wrong_settings_pin_counts_against_the_login_limiter(env):
    client = _client()

    for _ in range(5):
        client.post("/api/settings", json={"POKAMIND_BASE": "https://x.example", "pin": "1"})
    limited = client.post("/api/settings", json={"POKAMIND_BASE": "https://x.example",
                                                 "pin": PIN})

    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")


def test_a_new_pokamind_address_the_hostapp_could_not_verify_is_reported(env):
    _, http = env
    http.add("POST", "/api/admin/site", http_status(422, {"error": "base_not_verified",
                                                          "code": "whoami_unreachable"}))

    reply = _client().post("/api/settings", json={"POKAMIND_BASE": "https://x.example",
                                                  "pin": PIN})

    assert reply.status_code == 422
    assert reply.json() == {"ok": False, "error": "base_not_verified",
                            "code": "whoami_unreachable"}


def test_the_hostapp_s_own_refusal_reaches_the_phone(env):
    _, http = env
    http.add("POST", "/api/admin/site", http_status(400, {
        "error": "invalid_value", "key": "EMORI_SMTP_PORT", "code": "value_not_port"}))

    reply = _client().post("/api/settings", json={"EMORI_SMTP_PORT": "99999", "pin": PIN})

    assert reply.status_code == 400 and reply.json()["code"] == "value_not_port"


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
def test_apply_restarts_the_hostapp_when_it_is_idle(env):
    run, http = env
    http.add("GET", "/api/admin/summary", http_ok(IDLE))
    run.add(HOSTAPP_RESTART)

    reply = _client().post("/api/settings/apply")

    assert reply.status_code == 200 and reply.json() == {"ok": True, "restarted": True}
    assert run.argv_for("sudo") == [HOSTAPP_RESTART]


@pytest.mark.parametrize("summary", [dict(IDLE, session_active=True, idle=False),
                                     dict(IDLE, addon_mode="running", idle=False),
                                     dict(IDLE, idle=False),   # a badge lease
                                     dict(IDLE, addon_mode=None, idle=None),
                                     {"lang": "ca"}])
def test_apply_never_restarts_under_a_visitor(env, summary):
    run, http = env
    http.add("GET", "/api/admin/summary", http_ok(summary))

    reply = _client().post("/api/settings/apply")

    assert (reply.status_code, reply.json()["error"]) == (409, "busy")
    assert not run.ran("sudo")


def test_apply_with_the_hostapp_silent_is_not_a_guess(env):
    run, _ = env

    reply = _client().post("/api/settings/apply")

    assert (reply.status_code, reply.json()["error"]) == (502, "hostapp_down")
    assert not run.ran("sudo")


def test_a_failed_apply_says_so(env):
    run, http = env
    http.add("GET", "/api/admin/summary", http_ok(IDLE))
    run.add(HOSTAPP_RESTART, returncode=1, stderr="Unit not found")

    reply = _client().post("/api/settings/apply")

    assert reply.status_code == 502
    assert reply.json() == {"ok": False, "error": "restart_failed", "detail": "exit_1"}


# --------------------------------------------------------------------------
# the other proxies
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method, path, body, hostapp_path, forwarded, timeout", [
    ("post", "/api/scope/sync", None, "/api/admin/scope/sync", None,
     server.POKAMIND_PROXY_TIMEOUT_S),
    ("get", "/api/menu?lang=ca", None, "/api/admin/menu", None,
     server.POKAMIND_PROXY_TIMEOUT_S),
    ("post", "/api/menu/save", {"lang": "en", "menu": {"prompt": "Hi"}, "extra": 1},
     "/api/admin/menu/save", {"lang": "en", "menu": {"prompt": "Hi"}},
     server.POKAMIND_PROXY_TIMEOUT_S),
    ("post", "/api/menu/reset", {"lang": "ca", "menu": {"prompt": "x"}},
     "/api/admin/menu/reset", {"lang": "ca"}, server.POKAMIND_PROXY_TIMEOUT_S),
    ("post", "/api/menu/preview", {"lang": "ca"}, "/api/admin/menu/preview", {"lang": "ca"},
     server.POKAMIND_PROXY_TIMEOUT_S),
    ("post", "/api/menu/preview", {"lang": "ca", "menu": {"prompt": "x"}},
     "/api/admin/menu/preview", {"lang": "ca", "menu": {"prompt": "x"}},
     server.POKAMIND_PROXY_TIMEOUT_S),
    ("post", "/api/results-email/test", {"to": " a@example.org "},
     "/api/admin/results-email/test", {"to": "a@example.org"}, server.RESULTS_TEST_TIMEOUT_S),
])
def test_each_proxy_carries_the_token_the_body_and_its_timeout(env, method, path, body,
                                                               hostapp_path, forwarded,
                                                               timeout):
    _, http = env
    http.add(method.upper(), hostapp_path, http_ok({"answer": 1}))
    client = _client()

    reply = client.request(method.upper(), path, json=body) if body is not None \
        else client.request(method.upper(), path)

    assert reply.status_code == 200 and reply.json() == {"ok": True, "answer": 1}
    assert reply.headers["cache-control"] == "no-store"
    call = http.called(method.upper(), hostapp_path)[0]
    assert call["headers"] == {"X-EMORI-Admin-Token": "tok-123"}
    assert call["timeout_s"] == timeout
    assert call["json"] == forwarded


def test_the_menu_language_reaches_the_hostapp_in_the_query(env):
    _, http = env
    http.add("GET", "/api/admin/menu", http_ok({"lang": "ca"}))
    client = _client()

    client.get("/api/menu?lang=ca")
    client.get("/api/menu")

    urls = [c["url"] for c in http.called("GET", "/api/admin/menu")]
    assert urls[0].endswith("/api/admin/menu?lang=ca") and urls[1].endswith("/api/admin/menu")


@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/menu?lang=fr", None),
    ("post", "/api/menu/save", {"lang": "fr", "menu": {}}),
    ("post", "/api/menu/save", {"lang": "ca"}),
    ("post", "/api/menu/save", {"lang": "ca", "menu": ["x"]}),
    ("post", "/api/menu/save", {"lang": "ca", "menu": {"prompt": "x" * 70000}}),
    ("post", "/api/menu/reset", {}),
    ("post", "/api/results-email/test", {"to": ""}),
    ("post", "/api/results-email/test", {"to": ["a@example.org"]}),
])
def test_a_malformed_proxy_request_never_leaves_this_process(env, method, path, body):
    _, http = env
    client = _client()

    reply = client.request(method.upper(), path, json=body) if body is not None \
        else client.request(method.upper(), path)

    assert reply.status_code == 400
    assert http.calls == []


def test_a_menu_refusal_carries_its_problems_to_the_phone(env):
    _, http = env
    http.add("POST", "/api/admin/menu/save", http_status(400, {
        "error": "menu_invalid", "errors": ["name_invalid"],
        "problems": [{"code": "name_invalid", "option": "2", "keyword": None}]}))

    reply = _client().post("/api/menu/save", json={"lang": "ca", "menu": {"prompt": "x"}})

    assert reply.status_code == 400 and reply.json()["problems"][0]["option"] == "2"


def test_a_failed_test_email_reads_as_its_code(env):
    _, http = env
    http.add("POST", "/api/admin/results-email/test",
             http_status(409, {"sent": False, "code": "test_busy"}))

    reply = _client().post("/api/results-email/test", json={"to": "a@example.org"})

    assert reply.status_code == 409
    assert reply.json() == {"ok": False, "error": "test_busy", "sent": False,
                            "code": "test_busy"}


def test_a_language_outside_the_scope_is_refused_in_words(env):
    _, http = env
    http.add("POST", "/api/admin/lang", http_status(400, {"error": "unsupported_lang"}))

    reply = _client().post("/api/lang", json={"lang": "en"})

    assert (reply.status_code, reply.json()["error"]) == (400, "unsupported_lang")


# --------------------------------------------------------------------------
# the setup checklist and the door
# --------------------------------------------------------------------------
def test_the_setup_checklist_survives_a_dead_hostapp(env):
    reply = _client().get("/api/setup")
    body = reply.json()

    assert reply.status_code == 200
    assert body["scope"] is None and body["people"] is None and body["addon"] is None
    assert body["menus"] == {"ca": None, "en": None}
    assert body["readycheck"] == {"running": False, "ran": False, "fail": 0, "warn": 0}


def test_the_checklist_counts_the_readycheck_the_last_deploy_ran(env, monkeypatch, tmp_path):
    """Deploy-to-Robot already ran readycheck on the robot: the Home step need not wait
    for somebody to press Run here (counts only; a run started here replaces it)."""

    from emori_adapter import site_settings

    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    record = tmp_path / server.DEPLOY_READYCHECK_FILE
    record.parent.mkdir(parents=True)
    record.write_text("rc=0\nfail=0\nwarn=2\nwarn_gates=voice_menu,tts\n",
                      encoding="utf-8")

    body = _client().get("/api/setup").json()

    assert body["readycheck"] == {"running": False, "ran": True, "fail": 0, "warn": 2,
                                  "source": "deploy"}


def test_a_deploy_record_without_counts_is_not_a_readycheck(env, monkeypatch, tmp_path):
    from emori_adapter import site_settings

    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    record = tmp_path / server.DEPLOY_READYCHECK_FILE
    record.parent.mkdir(parents=True)
    record.write_text("rc=0\nfail=\nwarn=\nwarn_gates=\n", encoding="utf-8")

    assert _client().get("/api/setup").json()["readycheck"]["ran"] is False


def test_the_setup_checklist_is_cached_like_the_overview(env):
    _, http = env
    http.add("GET", "/api/admin/roster", http_ok({"people": []}))
    client = _client()

    client.get("/api/setup")
    client.get("/api/setup")

    assert len(http.called("GET", "/api/admin/roster")) == 1


_ROUTES = [("get", "/api/settings"), ("post", "/api/settings"), ("post", "/api/settings/apply"),
           ("post", "/api/scope/sync"), ("get", "/api/menu"), ("post", "/api/menu/save"),
           ("post", "/api/menu/reset"), ("post", "/api/menu/preview"),
           ("post", "/api/results-email/test"),
           ("get", "/api/setup")]


def test_every_new_route_needs_the_cookie(env):
    from starlette.testclient import TestClient

    _, http = env
    anonymous = TestClient(server.create_app(background=False))
    for method, path in _ROUTES:
        assert anonymous.request(method.upper(), path).status_code == 401, path
    assert http.calls == []


def test_every_new_route_refuses_a_foreign_page(env):
    _, http = env
    client = _client()
    for method, path in _ROUTES:
        reply = client.request(method.upper(), path,
                               headers={"Origin": "http://reachy-mini.local:8000"})
        assert (reply.status_code, reply.json()["error"]) == (403, "bad_origin"), path
    assert http.calls == []


def test_a_robot_without_the_admin_token_says_so(env, monkeypatch):
    client = _client()
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")

    for method, path in _ROUTES:
        if path == "/api/setup":
            continue  # answered here, with whatever can be read
        reply = client.request(method.upper(), path)
        assert (reply.status_code, reply.json()["error"]) == (503, "admin_token_unset"), path


def test_the_page_asks_for_the_pin_for_exactly_the_keys_this_service_checks():
    import re

    from emori_adapter.admin import page

    block = page.APP_HTML[page.APP_HTML.index("var ST_PIN={"):]
    block = block[:block.index("};")]

    assert set(re.findall(r"([A-Z][A-Z0-9_]+):1", block)) == set(server.PIN_SETTINGS)


@pytest.mark.parametrize("answer, error", [
    (http_status(503, {"error": "speech_unavailable"}), "speech_unavailable"),
    (http_status(503, {"error": "admin_not_configured"}), "hostapp_admin_disabled"),
    (http_status(503), "hostapp_admin_disabled"),
])
def test_a_route_s_own_unavailable_is_not_read_as_a_token_problem(env, answer, error):
    _, http = env
    http.add("POST", "/api/admin/menu/preview", answer)

    reply = _client().post("/api/menu/preview", json={"lang": "ca"})

    assert (reply.status_code, reply.json()["error"]) == (503, error)
