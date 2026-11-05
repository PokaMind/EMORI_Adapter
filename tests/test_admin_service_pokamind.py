"""emori-admin's Pokamind tab: a proxy that never keeps the token.

The admin service holds no robot credential (admin/probes.py), so every Pokamind route
is a proxy to the hostapp, which does. What this side adds, and what is pinned here:

* a replaced token takes effect through the allowlisted ``emori-hostapp`` restart,
  issued only after the hostapp confirmed the save;
* the full token is shown only after the PIN is typed AGAIN, failures count against
  the login limiter, and the event is logged without the value;
* the token is never logged, never cached, never echoed back by a refusal.
"""

from __future__ import annotations

import logging

import pytest

from emori_adapter.admin import server
from test_admin_service_fakes import PIN, base_env, http_ok, http_status, install

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]
STATUS = {
    "configured": True, "credential": "robot_token", "source": "file", "token_error": None,
    "robot_id": "test-robot-01", "masked": "pkr1.test-robot-01.…dHh8",
    "fingerprint": "Vkdap1RjR0wChd9d", "env_robot_id": None, "mismatch": False,
    "restart_pending": False, "last_whoami": None,
}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "admin_state.json"))
    return install(monkeypatch)


def _client():
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False))
    assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


def test_the_status_is_the_hostapp_s_carried_through_with_the_shared_secret(env):
    _, http = env
    http.add("GET", "/api/admin/pokamind", http_ok(STATUS))

    body = _client().get("/api/pokamind").json()

    assert body == {"ok": True, **STATUS}
    call = http.called("GET", "/api/admin/pokamind")[0]
    assert call["headers"] == {"X-EMORI-Admin-Token": "tok-123"}


def test_a_dead_hostapp_or_a_missing_admin_token_says_which(env, monkeypatch):
    client = _client()
    down = client.get("/api/pokamind")
    assert (down.status_code, down.json()["error"]) == (502, "hostapp_down")

    monkeypatch.delenv("EMORI_ADMIN_TOKEN")
    unset = client.get("/api/pokamind")
    assert (unset.status_code, unset.json()["error"]) == (503, "admin_token_unset")


def test_test_connection_waits_for_the_live_answer(env):
    _, http = env
    http.add("POST", "/api/admin/pokamind/verify",
             http_ok({"ok": False, "last_whoami": {"ok": False, "code": "robot_revoked"}}))

    body = _client().post("/api/pokamind/verify").json()

    assert body["last_whoami"]["code"] == "robot_revoked"
    assert http.called("POST", "/api/admin/pokamind/verify")[0]["timeout_s"] == (
        server.POKAMIND_PROXY_TIMEOUT_S)


# --- replace -------------------------------------------------------------------------------


_SAVED = {"saved": True, "restart_required": True, "robot_id": "test-robot-01",
          "masked": "pkr1.test-robot-01.…dHh8", "fingerprint": "Vkdap1RjR0wChd9d",
          "org": "example-org", "robot_id_changed": False, "env_robot_id_mismatch": False}


def test_a_saved_token_restarts_the_hostapp_through_the_allowlist(env, caplog):
    run, http = env
    http.add("POST", "/api/admin/pokamind/token", http_ok(_SAVED))
    run.add(["sudo", "systemctl", "restart", "emori-hostapp.service"])

    with caplog.at_level(logging.DEBUG):
        reply = _client().post("/api/pokamind/token",
                               json={"token": f"  {GOLDEN_TOKEN}\n", "pin": PIN})

    assert reply.status_code == 200
    assert reply.json()["restarted"] is True and reply.json()["saved"] is True
    forwarded = http.called("POST", "/api/admin/pokamind/token")[0]
    assert forwarded["json"] == {"token": GOLDEN_TOKEN}
    assert forwarded["timeout_s"] == server.POKAMIND_PROXY_TIMEOUT_S
    assert run.argv_for("sudo", "systemctl", "restart") == [
        ["sudo", "systemctl", "restart", "emori-hostapp.service"]]
    assert all(SEED not in record.getMessage() for record in caplog.records)
    assert SEED not in reply.text


def test_a_restart_that_fails_is_reported_not_hidden(env):
    run, http = env
    http.add("POST", "/api/admin/pokamind/token", http_ok(_SAVED))
    run.add(["sudo", "systemctl", "restart"], returncode=1, stderr="Unit not found")

    body = _client().post("/api/pokamind/token",
                          json={"token": GOLDEN_TOKEN, "pin": PIN}).json()

    assert body["saved"] is True and body["restarted"] is False
    assert body["restart_error"] == "exit_1"


@pytest.mark.parametrize("hostapp_answer, status, error", [
    (http_status(409, {"error": "busy"}), 409, "busy"),
    (http_status(422, {"error": "token_not_verified", "code": "robot_revoked"}), 422,
     "token_not_verified"),
    (http_status(400, {"error": "token_invalid", "code": "seed_invalid"}), 400,
     "token_invalid"),
])
def test_a_refusal_restarts_nothing_and_carries_the_hostapp_s_reason(env, hostapp_answer,
                                                                      status, error):
    run, http = env
    http.add("POST", "/api/admin/pokamind/token", hostapp_answer)

    reply = _client().post("/api/pokamind/token", json={"token": GOLDEN_TOKEN, "pin": PIN})

    assert (reply.status_code, reply.json()["error"]) == (status, error)
    assert not run.ran("sudo", "systemctl")


@pytest.mark.parametrize("payload", [{}, {"token": ""}, {"token": "   "}, {"token": 7},
                                     {"token": "x" * 600}])
def test_an_empty_or_oversized_paste_never_leaves_this_process(env, payload):
    _, http = env

    reply = _client().post("/api/pokamind/token", json=payload)

    assert (reply.status_code, reply.json()["code"]) == (400, "token_malformed")
    assert http.called("POST", "/api/admin/pokamind/token") == []


@pytest.mark.parametrize("pin", [None, "", "000000", 123456])
def test_a_replacement_needs_the_pin_again(env, pin, caplog):
    """The token decides which Pokamind record configures the whole robot: a phone left
    signed in is not enough to hand the robot to another one."""

    run, http = env
    http.add("POST", "/api/admin/pokamind/token", http_ok(_SAVED))
    payload = {"token": GOLDEN_TOKEN} if pin is None else {"token": GOLDEN_TOKEN, "pin": pin}

    with caplog.at_level(logging.DEBUG):
        reply = _client().post("/api/pokamind/token", json=payload)

    assert (reply.status_code, reply.json()["error"]) == (403, "bad_pin")
    assert http.called("POST", "/api/admin/pokamind/token") == []
    assert not run.ran("sudo", "systemctl")
    assert all(SEED not in record.getMessage() for record in caplog.records)


def test_wrong_replacement_pins_count_against_the_login_limiter(env):
    client = _client()

    for _ in range(5):
        assert client.post("/api/pokamind/token",
                           json={"token": GOLDEN_TOKEN, "pin": "000000"}).status_code == 403
    limited = client.post("/api/pokamind/token", json={"token": GOLDEN_TOKEN, "pin": PIN})

    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")


# --- reveal --------------------------------------------------------------------------------


def test_reveal_needs_the_pin_again_and_logs_only_the_event(env, caplog):
    _, http = env
    http.add("POST", "/api/admin/pokamind/reveal",
             http_ok({"robot_id": "test-robot-01", "token": GOLDEN_TOKEN}))
    client = _client()

    with caplog.at_level(logging.DEBUG):
        reply = client.post("/api/pokamind/reveal", json={"pin": PIN})

    assert reply.status_code == 200
    assert reply.json()["token"] == GOLDEN_TOKEN
    assert reply.json()["visible_s"] == 30
    assert reply.headers["cache-control"] == "no-store"
    lines = [record.getMessage() for record in caplog.records]
    assert any("REVEALED" in line and "test-robot-01" in line for line in lines)
    assert all(SEED not in line for line in lines)


def test_a_wrong_pin_reveals_nothing_and_is_not_a_sign_out(env):
    _, http = env
    client = _client()

    reply = client.post("/api/pokamind/reveal", json={"pin": "000000"})

    # 403, not 401: the page treats 401 as "signed out" and would drop the operator
    # back on the login screen.
    assert (reply.status_code, reply.json()["error"]) == (403, "bad_pin")
    assert http.called("POST", "/api/admin/pokamind/reveal") == []


def test_wrong_reveal_pins_count_against_the_login_limiter(env):
    client = _client()

    for _ in range(5):
        assert client.post("/api/pokamind/reveal", json={"pin": "000000"}).status_code == 403
    limited = client.post("/api/pokamind/reveal", json={"pin": PIN})

    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")


def test_every_pokamind_route_needs_the_cookie(env):
    from starlette.testclient import TestClient

    anonymous = TestClient(server.create_app(background=False))
    for method, path in (("get", "/api/pokamind"), ("post", "/api/pokamind/verify"),
                         ("post", "/api/pokamind/token"), ("post", "/api/pokamind/reveal")):
        assert getattr(anonymous, method)(path).status_code == 401, path
