"""The door on the admin service: fail-closed, six digits, rate limit, cookie.

This surface can reboot the robot, put it on a different network and stop the
hostapp, and it is reachable by anyone within Wi-Fi range of the site (or of the
robot's own AP).  So the three things pinned here are not "nice to have":

* with no PIN configured NOTHING is served but ``/healthz`` — a robot shipped
  before an operator chose a PIN must not be administrable by a stranger;
* a PIN that is not exactly six digits is a MISCONFIGURATION, not a shorter
  password: the rate limits below are sized for a 10^6 keyspace;
* five wrong guesses per IP under a sixty-per-minute global ceiling, because a
  per-IP bucket alone is not a limit when the attacker picks their own IPs.
"""

from __future__ import annotations

import time

import pytest

from emori_adapter.admin import auth, server
from test_admin_service_fakes import PIN, SALT, base_env, install


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    base_env(monkeypatch)
    install(monkeypatch)


def _client():
    from starlette.testclient import TestClient

    return TestClient(server.create_app(background=False))


def _login(client, pin: str = PIN):
    return client.post("/api/login", json={"pin": pin})


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------
def test_no_pin_serves_nothing_but_healthz(monkeypatch):
    monkeypatch.delenv("EMORI_ADMIN_PIN", raising=False)
    client = _client()

    # readycheck's gate_admin reads pin_configured (admin_pin_unset).
    assert client.get("/healthz").json() == {"status": "ok", "service": "emori-admin",
                                             "pin_configured": False}
    assert client.get("/").status_code == 503
    denied = client.get("/api/overview")
    assert denied.status_code == 503
    assert denied.json()["error"] == "admin_not_configured"


@pytest.mark.parametrize("bad", ["1234", "1234567", "12345a", "12 456", "١٢٣٤٥٦"])
def test_a_pin_that_is_not_six_digits_is_a_misconfiguration(monkeypatch, bad):
    """Accepting `1234` would cut the brute-force keyspace by four orders of
    magnitude while the rate limits stayed sized for six digits."""
    monkeypatch.setenv("EMORI_ADMIN_PIN", bad)
    client = _client()

    assert client.get("/").status_code == 503
    assert client.get("/api/system").json()["error"] == "admin_pin_invalid"
    # ...and it cannot be used to log in either.
    assert _login(client, bad).status_code == 503


def test_healthz_says_a_usable_pin_is_configured_and_nothing_else(monkeypatch):
    assert _client().get("/healthz").json() == {"status": "ok", "service": "emori-admin",
                                                "pin_configured": True}
    monkeypatch.setenv("EMORI_ADMIN_PIN", "1234")
    assert _client().get("/healthz").json()["pin_configured"] is False


def test_whitespace_around_the_pin_in_dotenv_is_tolerated(monkeypatch):
    """`.env` values keep their trailing spaces; a stray one must not close the
    admin surface on a robot the operator cannot reach any other way."""
    monkeypatch.setenv("EMORI_ADMIN_PIN", "  " + PIN + " ")
    client = _client()

    assert _login(client).status_code == 204


# --------------------------------------------------------------------------
# the login itself
# --------------------------------------------------------------------------
def test_correct_pin_sets_a_usable_cookie():
    client = _client()

    response = _login(client)
    assert response.status_code == 204
    assert auth.COOKIE_NAME in response.cookies
    # The cookie alone now opens the API.
    assert client.get("/api/system").status_code == 200


def test_cookie_flags_are_httponly_lax_and_NOT_secure():
    """Not `secure`: the robot serves plain HTTP on a site LAN and on its own
    AP (no certificate for 10.42.0.1), so a secure cookie would never come back."""
    client = _client()

    header = _login(client).headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "secure" not in header
    assert "max-age=%d" % auth.COOKIE_TTL_S in header


def test_wrong_pin_is_401_and_grants_nothing():
    client = _client()

    assert _login(client, "999999").status_code == 401
    assert client.get("/api/overview").status_code == 401


def test_a_body_without_a_pin_is_rejected_not_crashed():
    client = _client()

    assert client.post("/api/login", json={}).status_code == 401
    assert client.post("/api/login", content=b"").status_code == 401
    assert client.post("/api/login", content=b"not json at all").status_code == 401


def test_form_encoded_login_also_works():
    """`curl -d pin=...` from a laptop during a field debug must work; the robot
    venv deliberately has no python-multipart, hence the hand parser."""
    client = _client()

    assert client.post("/api/login", data={"pin": PIN}).status_code == 204


def test_logout_clears_the_cookie():
    client = _client()
    _login(client)

    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/system").status_code == 401


def test_five_wrong_guesses_then_rate_limited():
    client = _client()

    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert _login(client, "000000").status_code == 401
    assert _login(client, "000000").status_code == 429
    # ...and the correct PIN is refused too while the window is open: a limiter
    # that lets the right answer through is a limiter an attacker walks past.
    assert _login(client).status_code == 429


def test_a_malformed_body_still_counts_as_a_failure():
    """Otherwise an attacker keeps the window empty with junk bodies and guesses
    at full speed between them."""
    client = _client()

    for _ in range(auth.LOGIN_MAX_FAILURES):
        client.post("/api/login", json={"pin": "nope"})
    assert _login(client).status_code == 429


# --------------------------------------------------------------------------
# cross-origin (CSRF)
# --------------------------------------------------------------------------
# `SameSite=lax` is scoped to the SITE and ignores the PORT, so the Reachy
# daemon on :8000 of this same host -- unauthenticated, and a known reflector of
# query text into HTML -- counts as same-site with :8070 and the admin cookie is
# attached to whatever it asks for. The routes that take no body are CORS-simple
# requests, so nothing else stands in the way of a poweroff.
HOSTILE = {"Origin": "http://reachy-mini.local:8000",
           "Referer": "http://reachy-mini.local:8000/oauth/callback",
           "Host": "reachy-mini.local:8070"}


def test_a_same_site_page_on_another_port_cannot_power_the_robot_off(monkeypatch):
    run, _ = install(monkeypatch)
    client = _client()
    _login(client)          # a real, valid 12 h session, exactly as an operator has

    denied = client.post("/api/system/poweroff", headers=HOSTILE)

    assert denied.status_code == 403
    assert denied.json()["error"] == "bad_origin"
    # ...and the deferred thread that would really run it was never armed.
    time.sleep(0.05)
    assert run.ran("sudo", "systemctl", "poweroff") is False


@pytest.mark.parametrize("path", [
    "/api/system/reboot", "/api/network/hotspot", "/api/audio/test-sound",
    "/api/network/scan", "/api/readycheck/run", "/api/logout",
])
def test_every_bodyless_mutation_refuses_a_foreign_origin(monkeypatch, path):
    """These take no body, so the JSON content-type preflight that incidentally
    protects the others never happens for them."""
    client = _client()
    _login(client)

    assert client.post(path, headers=HOSTILE).status_code == 403


def test_a_foreign_origin_cannot_log_in_either(monkeypatch):
    client = _client()

    assert client.post("/api/login", json={"pin": PIN},
                       headers=HOSTILE).status_code == 403


def test_our_own_page_still_works(monkeypatch):
    """Same-origin fetch: the browser sends Origin, and it matches Host."""
    client = _client()
    same = {"Origin": "http://10.42.0.1:8070", "Host": "10.42.0.1:8070"}

    assert client.post("/api/login", json={"pin": PIN}, headers=same).status_code == 204
    assert client.get("/api/system", headers=same).status_code == 200
    assert client.post("/api/logout", headers=same).status_code == 204


def test_a_referer_is_used_when_there_is_no_origin(monkeypatch):
    client = _client()
    _login(client)

    ours = client.get("/api/system", headers={
        "Referer": "http://10.42.0.1:8070/", "Host": "10.42.0.1:8070"})
    theirs = client.get("/api/system", headers={
        "Referer": "http://10.42.0.1:8000/x", "Host": "10.42.0.1:8070"})

    assert ours.status_code == 200
    assert theirs.status_code == 403


def test_a_header_less_client_is_still_allowed(monkeypatch):
    """curl/httpx during a field debug declare no origin at all, and a browser
    always declares one on a cross-origin POST -- so this branch is ours."""
    client = _client()
    _login(client)

    assert client.get("/api/system").status_code == 200
    assert client.post("/api/readycheck/run").status_code in (200, 409)


@pytest.mark.parametrize("origin", ["null", "http://evil.example", "  ", "http://"])
def test_an_unparseable_or_null_origin_is_not_a_match(origin):
    class _Req:
        headers = {"origin": origin, "host": "10.42.0.1:8070"}

    # "  " declares nothing (curl); everything else declares a foreign origin.
    assert server.origin_ok(_Req()) is (origin.strip() == "")


# --------------------------------------------------------------------------
# the limiter itself
# --------------------------------------------------------------------------
def test_global_ceiling_survives_an_attacker_who_owns_many_addresses():
    limiter = auth.LoginLimiter()
    now = 1000.0
    for index in range(auth.LOGIN_MAX_FAILURES_GLOBAL):
        limiter.record_failure("10.0.0.%d" % index, now=now)

    # A previously untouched address is blocked by the ceiling alone.
    assert limiter.blocked("192.168.1.7", now=now) is True
    # The block lifts one window later.
    assert limiter.blocked("192.168.1.7", now=now + auth.LOGIN_WINDOW_S + 1) is False


def test_a_successful_login_does_not_reset_the_global_bucket():
    limiter = auth.LoginLimiter()
    now = 500.0
    for index in range(auth.LOGIN_MAX_FAILURES_GLOBAL):
        limiter.record_failure("10.0.0.%d" % index, now=now)
    limiter.clear("10.0.0.1")

    assert limiter.blocked("172.16.0.9", now=now) is True


def test_per_ip_window_expires():
    limiter = auth.LoginLimiter()
    for _ in range(auth.LOGIN_MAX_FAILURES):
        limiter.record_failure("10.1.1.1", now=10.0)

    assert limiter.blocked("10.1.1.1", now=10.0) is True
    assert limiter.blocked("10.1.1.1", now=10.0 + auth.LOGIN_WINDOW_S + 0.5) is False


# --------------------------------------------------------------------------
# cookie primitives
# --------------------------------------------------------------------------
def test_cookie_roundtrip_and_expiry():
    now = 1_700_000_000.0
    cookie = auth.make_cookie(PIN, SALT, now=now)

    assert auth.cookie_valid(cookie, PIN, SALT, now=now) is True
    assert auth.cookie_valid(cookie, PIN, SALT, now=now + auth.COOKIE_TTL_S - 1) is True
    assert auth.cookie_valid(cookie, PIN, SALT, now=now + auth.COOKIE_TTL_S + 1) is False


def test_a_cookie_stamped_in_the_future_is_refused():
    """Clock skew slack, not a time machine: a far-future stamp is forged."""
    now = 1_700_000_000.0
    cookie = auth.make_cookie(PIN, SALT, now=now + 3600)

    assert auth.cookie_valid(cookie, PIN, SALT, now=now) is False
    # ...but a couple of minutes of NTP drift is tolerated.
    near = auth.make_cookie(PIN, SALT, now=now + auth.COOKIE_FUTURE_SLACK_S / 2)
    assert auth.cookie_valid(near, PIN, SALT, now=now) is True


def test_changing_the_pin_or_the_salt_invalidates_every_cookie():
    now = time.time()
    cookie = auth.make_cookie(PIN, SALT, now=now)

    assert auth.cookie_valid(cookie, "999999", SALT, now=now) is False
    assert auth.cookie_valid(cookie, PIN, "other-salt", now=now) is False


def test_the_pin_never_appears_in_the_cookie():
    assert PIN not in auth.make_cookie(PIN, SALT, now=1.0)


@pytest.mark.parametrize("hostile", [
    "", "garbage", "1700000000", "1700000000.", ".abc", "notanint.deadbeef",
    "1700000000.\udcff", "1700000000." + "f" * 4096, "ñ.ñ",
])
def test_a_hostile_cookie_is_a_refusal_never_an_exception(hostile):
    """Starlette decodes header bytes as latin-1; one crafted byte must not turn
    every request into an unauthenticated 500 on the dashboard."""
    assert auth.cookie_valid(hostile, PIN, SALT, now=1_700_000_000.0) is False


def test_a_forged_cookie_does_not_open_the_api():
    client = _client()
    client.cookies.set(auth.COOKIE_NAME, "%d.%s" % (int(time.time()), "0" * 32))

    assert client.get("/api/overview").status_code == 401


def test_constant_time_equal_never_raises_on_non_ascii():
    assert auth.constant_time_equal("ñ", "ñ") is True
    assert auth.constant_time_equal("ñ", "n") is False
    assert auth.constant_time_equal(None, "x") is False
    assert auth.constant_time_equal("\udcff", "\udcff") is True


# --------------------------------------------------------------------------
# salt + robot name
# --------------------------------------------------------------------------
@pytest.fixture()
def _unset_salt(monkeypatch, tmp_path):
    """No EMORI_ADMIN_SALT, and the generated one lands under tmp_path."""

    monkeypatch.delenv("EMORI_ADMIN_SALT", raising=False)
    monkeypatch.setenv("EMORI_ADMIN_STATE_FILE", str(tmp_path / "admin_state.json"))
    monkeypatch.setattr(auth, "_fallback_salt_cache", "")
    return tmp_path


def test_an_unset_salt_mints_a_random_persisted_one(_unset_salt, monkeypatch):
    """The salt is what stops a captured cookie being an offline PIN cracker.

    The cookie is HMAC(HMAC(salt, pin), issued) and the PIN is six digits by
    mandate, so a salt an outsider can derive (the old EMORI_ROBOT_ID fallback,
    with the robot's name printed on the unauthenticated login page) means one
    observed cookie yields the PIN in about a second -- past every rate limit,
    and surviving expiry, logout and reboot.
    """
    monkeypatch.setenv("EMORI_ROBOT_ID", "ward-3")

    first = auth.admin_salt()
    monkeypatch.setattr(auth, "_fallback_salt_cache", "")   # as if restarted
    second = auth.admin_salt()

    assert first == second                       # stable across restarts
    assert "ward-3" not in first                 # not derivable from the outside
    assert len(first) >= 32
    salt_file = _unset_salt / "admin_cookie_salt"
    assert salt_file.exists()
    assert salt_file.read_text(encoding="utf-8").strip() == first


def test_an_unwritable_state_dir_still_serves_and_says_so(_unset_salt, monkeypatch,
                                                          caplog):
    """A read-only filesystem must not take the recovery tool off the air; it
    degrades to the guessable derivation and names the risk in the journal."""
    blocker = _unset_salt / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("EMORI_ADMIN_STATE_FILE", str(blocker / "sub" / "state.json"))
    monkeypatch.setenv("EMORI_ROBOT_ID", "ward-3")
    monkeypatch.setattr(auth, "_salt_warned", False)

    with caplog.at_level("WARNING"):
        value = auth.admin_salt()

    assert "ward-3" in value                     # still a working service
    assert "offline" in caplog.text.lower()


def test_a_configured_salt_always_wins(_unset_salt, monkeypatch):
    monkeypatch.setenv("EMORI_ADMIN_SALT", "an-operator-chosen-salt")

    assert auth.admin_salt() == "an-operator-chosen-salt"
    assert not (_unset_salt / "admin_cookie_salt").exists()


def test_robot_name_prefers_the_display_name(monkeypatch):
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Reception")
    assert auth.robot_name() == "Reception"
    monkeypatch.delenv("EMORI_ROBOT_NAME")
    monkeypatch.setenv("EMORI_ROBOT_ID", "robot-7")
    assert auth.robot_name() == "robot-7"
    monkeypatch.delenv("EMORI_ROBOT_ID")
    assert auth.robot_name() == "EMORI"


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
def test_pages_render_without_the_ui_module(monkeypatch):
    """The admin service must serve the fallback page even if admin/page.py is missing."""
    monkeypatch.setattr(server, "_page_constant", lambda name: None)
    client = _client()

    login_page = client.get("/")
    assert login_page.status_code == 200
    assert "Example Robot" in login_page.text
    _login(client)
    assert client.get("/").status_code == 200


def test_page_placeholders_are_filled_and_escaped(monkeypatch):
    monkeypatch.setenv("EMORI_ROBOT_NAME", "<script>x</script>")
    monkeypatch.setattr(server, "_page_constant",
                        lambda name: "<h1>__ROBOT_NAME__</h1><p>__ERROR__</p>")
    client = _client()

    body = client.get("/?e=bad").text
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body
    assert "Wrong PIN." in body


def test_the_openapi_schema_is_not_served():
    """FastAPI serves /docs and /openapi.json WITHOUT a cookie; on an open AP
    that is a free map of every route that can reboot the robot."""
    client = _client()

    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
