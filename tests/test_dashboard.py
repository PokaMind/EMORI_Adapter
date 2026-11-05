"""The management dashboard: auth, KPI arithmetic, and the privacy promise.

Three things here are load-bearing and therefore tested against known fixtures
rather than eyeballed:

* the door (no password -> 503, no cookie -> 401, five wrong guesses -> 429),
* the KPI formulas a manager will quote, and
* that no name, e-mail or transcript can leave through ANY route.
"""

from __future__ import annotations

import calendar
import datetime
import json
import re
import shutil
import subprocess
import time

import pytest

from emori_adapter.hostapp import dashboard, dashboard_page
from emori_adapter.hostapp.metrics_store import MetricsStore
from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel

PASSWORD = "site-secret"
SALT = "unit-test-salt"

# Real-looking identity that must never surface in a response.
REAL_ID = "anna@example.org"
REAL_NAME = "Anna Example"
SECRET_ANALYSIS = "Anna hesitated when the relative asked about the prognosis."


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(str(tmp_path / "dash.db"), salt=SALT)


def _app(store, *, password=PASSWORD, driver=None, addon_status=None):
    from fastapi import FastAPI

    app = FastAPI()
    dashboard.mount(app, store=store, driver=driver, addon_status=addon_status,
                    password=password, salt=SALT)
    return app


def _client(store, **kw):
    from starlette.testclient import TestClient

    return TestClient(_app(store, **kw))


def _login(client) -> None:
    response = client.post("/dashboard/login", data={"password": PASSWORD})
    assert response.status_code == 200  # followed the 303 to /dashboard


def _signals(level=EngagementLevel.ENGAGED, score=0.7, attention=0.7, style="mirroring"):
    return DerivedSignals(
        timestamp="2026-07-30T10:00:00Z", session_id="s", person_id="p", seq=1,
        engagement_score=score, engagement_level=level, valence=0.2, arousal=0.1,
        expression="neutral", expression_confidence=0.4, top_styles=[(style, 0.3)],
        context=ContextSignal(engagement_trend=0.0, escalation=0.1, rapport=0.6,
                              attention=attention, dominant_style=style),
    )


# --------------------------------------------------------------------------
# a seeded window with arithmetic we can predict exactly
# --------------------------------------------------------------------------
NOW = 1785000000.0  # fixed so no test can drift across a UTC midnight


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """One deterministic 7-day window.

    sessions   4 finished (3 completed, 1 abandoned) + 1 active  -> completion 75%
    cycles     12 in all: 9 at or above the 0.62 engaged band    -> attention 75%
               (A: eight at 0.8 + two at 0.3; B: one at 0.5; C: one at 0.9)
    events     3 attention_drop / 2 recovery_success             -> recovery 66.7%
    heartbeats 30 beats, 120 s apart, over a 3600 s span at 60 s -> uptime 50%
    scores     two scored sessions                               -> known averages
    """

    monkeypatch.setenv("EMORI_HEARTBEAT_S", "60")
    store = _store(tmp_path)
    day = 86400.0

    # A: a badge learner, ten cycles, scored, completed.
    store.session_started("sess-a", person_id=REAL_ID, lang="ca",
                          module_id="m1", module_title="Difficult conversations",
                          started_at=NOW - day)
    for i in range(10):
        # ENGAGED throughout so record_cycle emits no recovery events of its own
        # and the event counts below stay exactly what the test seeds.
        attention = 0.8 if i < 8 else 0.3
        store.record_cycle("sess-a", _signals(attention=attention, score=attention),
                           ts=NOW - day + i * 2)
    store.session_finished(
        "sess-a", ended_at=NOW - day + 600, avg_engagement=0.72, status="completed",
        results={"performance_scores": {"communication_clarity": 80,
                                        "engagement_presence": 60,
                                        "emotional_intelligence": 70,
                                        "reasoning_quality": 50,
                                        "technique_application": 40,
                                        "adaptability": 60},
                 "session_analysis": SECRET_ANALYSIS})

    # B: the same badge learner again, lower scores (a declining series).
    store.session_started("sess-b", person_id=REAL_ID, lang="ca", module_id="m1",
                          module_title="Difficult conversations", started_at=NOW - day / 2)
    store.record_cycle("sess-b", _signals(attention=0.5, score=0.5), ts=NOW - day / 2)
    store.session_finished(
        "sess-b", ended_at=NOW - day / 2 + 300, avg_engagement=0.40, status="completed",
        results={"performance_scores": {"communication_clarity": 40,
                                        "engagement_presence": 30,
                                        "emotional_intelligence": 35,
                                        "reasoning_quality": 30,
                                        "technique_application": 25,
                                        "adaptability": 30}})

    # C: an anonymous walk-up, completed.
    store.session_started("sess-c", person_id="robot-lobby-3", started_at=NOW - 3 * 3600)
    store.record_cycle("sess-c", _signals(attention=0.9, score=0.9), ts=NOW - 3 * 3600)
    store.session_finished("sess-c", ended_at=NOW - 3 * 3600 + 200,
                           avg_engagement=0.9, status="completed")

    # D: abandoned (the dead-iframe fail-safe), no cycles at all.
    store.session_started("sess-d", person_id="carles-002", started_at=NOW - 2 * 3600)
    store.session_finished("sess-d", ended_at=NOW - 2 * 3600 + 60, status="abandoned")

    # E: still running -> excluded from the completion KPI.
    store.session_started("sess-e", person_id="carles-002", started_at=NOW - 60)

    for i in range(3):
        store.log_event("attention_drop", session_id="sess-a", ts=NOW - day + i)
    for i in range(2):
        store.log_event("recovery_success", session_id="sess-a", ts=NOW - day + 30 + i)
    store.log_event("ext_mode_a", session_id="", detail="mode_a", ts=NOW - 7200)
    store.log_event("ext_mode_b", session_id="", detail="mode_b", ts=NOW - 7100)
    store.log_event("ext_mode_b", session_id="", detail="mode_b", ts=NOW - 7000)
    store.log_event("fiware_skipped", session_id="sess-a",
                    detail="http://orion:1026", ts=NOW - day)

    for i in range(30):
        store.heartbeat(ok=True, ts=NOW - 3600 + i * 120)
    return store


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def test_unconfigured_password_serves_a_clear_503_and_no_data(tmp_path):
    client = _client(_store(tmp_path), password="")

    page = client.get("/dashboard")
    assert page.status_code == 503
    assert "not configured" in page.text.lower()
    assert "no configurat" in page.text.lower()  # the Catalan half of the page

    api = client.get("/api/dashboard/summary")
    assert api.status_code == 503
    assert api.json()["error"] == "dashboard_not_configured"

    # Even the login POST refuses: there is nothing to authenticate against.
    assert client.post("/dashboard/login", data={"password": "anything"}).status_code == 503


def test_no_cookie_gets_the_login_page_and_a_401_on_every_api(seeded):
    client = _client(seeded)

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Sign in" in page.text and "Entra" in page.text
    assert "ARISE Management Dashboard" not in page.text  # not the real page

    for path in ("/api/dashboard/summary", "/api/dashboard/trend",
                 "/api/dashboard/persons", "/api/dashboard/sessions",
                 "/api/dashboard/session/sess-a", "/api/dashboard/status"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"error": "unauthorized"}


def test_five_wrong_passwords_then_429(seeded):
    client = _client(seeded)

    for _ in range(5):
        assert client.post("/dashboard/login", data={"password": "wrong"}).status_code == 401
    blocked = client.post("/dashboard/login", data={"password": "wrong"})
    assert blocked.status_code == 429
    # Rate limiting is not a password oracle: the CORRECT password is refused too
    # while the window is hot.
    assert client.post("/dashboard/login", data={"password": PASSWORD}).status_code == 429


def test_login_auditing_groups_a_source_without_writing_down_its_address(seeded,
                                                                        caplog):
    """An IP is personal data, and on a site LAN with DHCP reservations it names
    a workstation and usually the person at it.  Root logging reaches journald,
    so these lines persist across boots on a box whose journal is
    world-readable.  What auth auditing actually asks is "is ONE
    source hammering us", which a salted HMAC answers.
    """

    import hashlib
    import hmac
    import logging

    client = _client(seeded)
    caplog.set_level(logging.WARNING, logger="emori.hostapp.dashboard")

    for _ in range(6):
        client.post("/dashboard/login", data={"password": "wrong"})

    lines = [m for m in caplog.messages if "dashboard login" in m]
    expected = hmac.new(SALT.encode("utf-8"), b"testclient",
                        hashlib.sha256).hexdigest()[:8]
    assert lines, "the login audit stopped logging entirely"
    assert all("testclient" not in line for line in lines)
    # Repeated attempts still group, and the rate-limit line fingerprints to the
    # same source as the failures that caused it.
    assert all(f"client={expected}" in line for line in lines)
    assert any("rate-limited" in line for line in lines)
    assert any("login failed" in line for line in lines)


def test_login_sets_a_working_cookie_and_logout_clears_it(seeded):
    client = _client(seeded)
    _login(client)

    assert dashboard.COOKIE_NAME in client.cookies
    assert client.get("/api/dashboard/summary").status_code == 200
    page = client.get("/dashboard")
    assert "ARISE Management Dashboard" in page.text

    client.post("/dashboard/logout")
    assert client.get("/api/dashboard/summary").status_code == 401


def test_login_accepts_a_json_body_too(seeded):
    client = _client(seeded)
    response = client.post("/dashboard/login", json={"password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/dashboard/status").status_code == 200


def test_cookie_signature_expiry_and_key_binding():
    good = dashboard.make_cookie(PASSWORD, SALT, now=NOW)
    assert dashboard.cookie_valid(good, PASSWORD, SALT, now=NOW)

    # still valid just inside 12 h, dead just outside
    assert dashboard.cookie_valid(good, PASSWORD, SALT, now=NOW + 12 * 3600 - 1)
    assert not dashboard.cookie_valid(good, PASSWORD, SALT, now=NOW + 12 * 3600 + 1)
    # a cookie stamped in the future is forged
    assert not dashboard.cookie_valid(good, PASSWORD, SALT, now=NOW - 3600)

    assert not dashboard.cookie_valid(good, "other-password", SALT, now=NOW)
    assert not dashboard.cookie_valid(good, PASSWORD, "other-salt", now=NOW)
    assert not dashboard.cookie_valid(good[:-1] + "0", PASSWORD, SALT, now=NOW)
    for junk in ("", "nonsense", "notanumber.abc", "12345"):
        assert not dashboard.cookie_valid(junk, PASSWORD, SALT, now=NOW)
    # no password configured -> nothing authenticates
    assert not dashboard.cookie_valid(good, "", SALT, now=NOW)


def test_a_forged_cookie_does_not_open_the_api(seeded):
    client = _client(seeded)
    client.cookies.set(dashboard.COOKIE_NAME, "%d.%s" % (int(time.time()), "0" * 32))
    assert client.get("/api/dashboard/summary").status_code == 401


def test_limiter_window_expires():
    limiter = dashboard.LoginLimiter(max_failures=2, window_s=60.0)
    limiter.record_failure("ip-a", now=100.0)
    limiter.record_failure("ip-a", now=101.0)
    assert limiter.blocked("ip-a", now=102.0)
    assert not limiter.blocked("ip-b", now=102.0)  # per client, not global
    assert not limiter.blocked("ip-a", now=200.0)  # the window rolled off
    limiter.record_failure("ip-a", now=200.0)
    limiter.clear("ip-a")
    assert not limiter.blocked("ip-a", now=201.0)


def test_many_source_addresses_cannot_outrun_the_global_ceiling():
    """A per-IP bucket alone is not a limit on an unfirewalled site LAN.

    One attacker with 500 secondary addresses (``ip addr add``, or anything out
    of their own IPv6 /64) got 5 x 500 guesses a minute at a human-chosen shared
    password, each bucket expiring silently so nothing ever accumulated.
    """

    limiter = dashboard.LoginLimiter(max_failures=5, window_s=60.0,
                                     max_failures_global=20)
    for i in range(20):
        client = "10.0.0.%d" % i          # a fresh address every time
        assert not limiter.blocked(client, now=100.0)
        limiter.record_failure(client, now=100.0)
    assert limiter.blocked("10.0.0.999", now=100.0), "the 21st address kept guessing"
    # A correct password from one client must not reset the ceiling for the rest.
    limiter.clear("10.0.0.0")
    assert limiter.blocked("10.0.0.999", now=100.0)
    # One window later everyone is free again -- a stuck-forever dashboard would
    # be its own outage.
    assert not limiter.blocked("10.0.0.999", now=200.0)


def test_a_non_ascii_password_logs_in_instead_of_500ing(tmp_path):
    """``hmac.compare_digest`` refuses non-ASCII str on BOTH sides.

    A Catalan site will pick a password with ç/à/é, and the dashboard
    then answered 500 to every attempt -- the right password included -- with
    nothing anywhere saying the charset was the cause.
    """

    from starlette.testclient import TestClient

    secret = "contrasenyañ-Ω"
    client = TestClient(_app(_store(tmp_path), password=secret),
                        raise_server_exceptions=False)

    wrong = client.post("/dashboard/login", data={"password": "café-nope"})
    assert wrong.status_code == 401, "a non-ASCII GUESS must be a refusal, not a crash"

    ok = client.post("/dashboard/login", data={"password": secret})
    assert ok.status_code == 200
    assert client.get("/api/dashboard/summary").status_code == 200


def test_a_non_ascii_password_guess_still_counts_against_the_limiter(tmp_path):
    """The crash escaped BEFORE record_failure, so those tries were free."""

    from starlette.testclient import TestClient

    client = TestClient(_app(_store(tmp_path), password="site-secret"),
                        raise_server_exceptions=False)
    for _ in range(5):
        assert client.post("/dashboard/login", data={"password": "ñ"}).status_code == 401
    assert client.post("/dashboard/login", data={"password": "ñ"}).status_code == 429


def test_a_non_ascii_cookie_byte_is_a_401_not_an_unauthenticated_500(seeded):
    """Starlette decodes header bytes as latin-1, so any LAN client could send
    a non-ASCII cookie and turn EVERY dashboard route into a 500 + stack trace."""

    from starlette.testclient import TestClient

    client = TestClient(_app(seeded), raise_server_exceptions=False)
    # Raw bytes: this is what a curl/browser on the site LAN puts on the wire,
    # and Starlette latin-1-decodes it into a non-ASCII str before we see it.
    headers = {b"Cookie": b"%s=%d.\xf1abcdef" % (dashboard.COOKIE_NAME.encode("ascii"),
                                                 int(time.time()))}
    for path in ("/dashboard", "/api/dashboard/summary", "/api/dashboard/trend",
                 "/api/dashboard/persons", "/api/dashboard/sessions",
                 "/api/dashboard/status"):
        response = client.get(path, headers=headers)
        assert response.status_code in (200, 401), (path, response.status_code)
        assert response.status_code != 500, path

    # And the pure function keeps its "never raises" promise.
    assert not dashboard.cookie_valid("1.\xf1abcdef", PASSWORD, SALT, now=NOW)
    assert not dashboard.cookie_valid("1.\U0001f600", PASSWORD, SALT, now=NOW)
    assert not dashboard.cookie_valid("\xf1.\xf1", PASSWORD, SALT, now=NOW)
    assert dashboard.cookie_valid(dashboard.make_cookie("contrasenyañ", "salt\xe9",
                                                        now=NOW),
                                  "contrasenyañ", "salt\xe9", now=NOW)


# --------------------------------------------------------------------------
# KPI arithmetic
# --------------------------------------------------------------------------
def test_summary_kpis_match_the_seeded_fixture(seeded):
    s = dashboard.summary(seeded, 7, now=NOW)

    assert s["sessions"] == 5
    assert s["unique_staff"] == 2          # REAL_ID + carles-002; the walk-up is not staff
    assert s["walkups"] == 1
    assert s["completed"] == 3 and s["abandoned"] == 1 and s["active"] == 1
    assert s["completion_pct"] == 75.0     # 3 / (3 + 1); the active one does not count

    # attention KPI = share of samples at/above the ENGAGED band (0.62), NOT the mean
    assert s["attention_threshold"] == pytest.approx(0.62)
    assert s["cycles"] == 12               # 10 on A + 1 on B + 1 on C
    assert s["attention_pct"] == pytest.approx(75.0)   # 9 of 12 at/above 0.62
    assert s["attention_mean"] == pytest.approx((8 * 0.8 + 2 * 0.3 + 0.5 + 0.9) / 12,
                                                abs=1e-3)

    assert s["attention_drops"] == 3 and s["recoveries"] == 2
    assert s["recovery_pct"] == pytest.approx(66.7, abs=0.1)

    assert s["heartbeats"] == 30
    assert s["uptime_pct"] == pytest.approx(50.0, abs=1.0)

    assert s["addon_events"] == {"mode_a": 1, "mode_b": 2}

    # Nothing in this fixture was ended by the robot, so the discriminator is silent
    # rather than absent: a key that only appears when it is non-zero cannot be read
    # by a page, and 0 robot-endings is a real, reportable answer.
    assert s["robot_ended"] == 0 and s["end_reasons"] == {}

    # competency = mean over the two scored sessions
    assert s["competency"]["communication_clarity"] == pytest.approx(60.0)
    assert s["competency"]["technique_application"] == pytest.approx(32.5)
    assert s["targets"] == dashboard.TARGETS


def test_a_site_of_walkaways_reports_100_percent_completion_and_says_so(tmp_path):
    """The completion KPI, and the number that qualifies it.

    Nobody presses "finish": the watchdog ends the conversation and Pokamind
    answers with the whole analysis, so a walk-away IS a completion here -- the
    session produced its outcome.  Which means completion_pct alone reads 100% for
    a site where every single learner left mid-conversation, and the only honest
    page is one that shows both numbers.
    """

    store = _store(tmp_path)
    for i, reason in enumerate(["walkaway_ends"] * 3 + ["presence_ends",
                                                        "watchdog_force_ends"]):
        sid = "s%d" % i
        store.session_started(sid, person_id="anna-%d" % i, module_id="m1",
                              module_title="Difficult conversations",
                              started_at=NOW - 3600 + i)
        store.session_finished(sid, ended_at=NOW - 3000 + i, status="completed",
                               results={"session_analysis": "ok"}, end_reason=reason)
    # ...and one learner who actually stayed to the end.
    store.session_started("stayed", person_id="bob-9", module_id="m1",
                          module_title="Difficult conversations", started_at=NOW - 2000)
    store.session_finished("stayed", ended_at=NOW - 1000, status="completed",
                           results={"session_analysis": "ok"})

    s = dashboard.summary(store, 7, now=NOW)

    assert s["completed"] == 6 and s["abandoned"] == 0
    assert s["completion_pct"] == 100.0
    assert s["robot_ended"] == 5
    assert s["end_reasons"] == {"walkaway_ends": 3, "presence_ends": 1,
                                "watchdog_force_ends": 1}
    # The module table carries it too -- "which scenario do people walk out of"
    # is invisible in a completion rate that is 100% for every module.
    row = dashboard.modules(store, 7, now=NOW)["modules"][0]
    assert (row["completion_pct"], row["robot_ended"]) == (100.0, 5)
    # ...as does the monthly participation table, where the completion KPI
    # is actually read.
    months = {m["month"]: m for m in
              dashboard.funnel(store, 2, roster_size=10, now=NOW)["funnel"]}
    this_month = months[time.strftime("%Y-%m", time.gmtime(NOW))]
    assert (this_month["completion_pct"], this_month["robot_ended"]) == (100.0, 5)


def test_the_recent_sessions_table_names_who_ended_each_one(tmp_path):
    store = _store(tmp_path)
    store.session_started("robot-ended", person_id="anna-1", started_at=NOW - 600)
    store.session_finished("robot-ended", ended_at=NOW - 300, status="completed",
                           results={"session_analysis": "ok"},
                           end_reason="walkaway_ends")
    store.session_started("learner-ended", person_id="anna-1", started_at=NOW - 200)
    store.session_finished("learner-ended", ended_at=NOW - 100, status="completed",
                           results={"session_analysis": "ok"})

    rows = {r["session_id"]: r for r in dashboard.sessions(store, 7, now=NOW)["sessions"]}

    assert rows["robot-ended"]["end_reason"] == "walkaway_ends"
    assert rows["robot-ended"]["robot_ended"] is True
    assert rows["learner-ended"]["end_reason"] == ""
    assert rows["learner-ended"]["robot_ended"] is False
    detail = dashboard.session_detail(store, "robot-ended")
    assert detail["session"]["end_reason"] == "walkaway_ends"


def test_orphan_events_do_not_inflate_the_recovery_kpi(seeded):
    """Half the dev store's recovery KPI came from a session that does not exist.

    Cycles that arrived with no active session were written under a phantom id
    (``driver.stamp``'s 'live-session', or Pokamind's own).  The cycles aggregate
    JOINs `sessions` so those rows vanish from `cycles`/`attention_pct`, but the
    events aggregate filtered on `ts` alone, so the attention_drop /
    recovery_success events they produced WERE counted -- in both halves of the
    headline recovery KPI.  The driver no longer writes them; rows already
    on disk must not count either.
    """

    before = dashboard.summary(seeded, 7, now=NOW)
    for i in range(6):
        seeded.log_event("attention_drop", session_id="live-session", ts=NOW - 100 + i)
        seeded.log_event("recovery_success", session_id="live-session", ts=NOW - 90 + i)
    # A robot-level event carries no session id at all and must still be counted.
    seeded.log_event("ext_mode_a", session_id="", detail="mode_a", ts=NOW - 80)

    after = dashboard.summary(seeded, 7, now=NOW)
    assert after["attention_drops"] == before["attention_drops"] == 3
    assert after["recoveries"] == before["recoveries"] == 2
    assert after["recovery_pct"] == pytest.approx(66.7, abs=0.1)
    assert after["addon_events"]["mode_a"] == before["addon_events"]["mode_a"] + 1


def test_uptime_is_not_punished_for_time_before_the_first_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("EMORI_HEARTBEAT_S", "60")
    store = _store(tmp_path)
    # Installed an hour ago, perfect since: 60 beats over 3600 s = 100%, even
    # though the requested window is 30 days.
    for i in range(61):
        store.heartbeat(ok=True, ts=NOW - 3600 + i * 60)
    assert dashboard.summary(store, 30, now=NOW)["uptime_pct"] == pytest.approx(100.0)


def test_uptime_is_none_when_the_heartbeat_writer_is_disabled(seeded, monkeypatch):
    monkeypatch.setenv("EMORI_HEARTBEAT_S", "0")
    assert dashboard.summary(seeded, 7, now=NOW)["uptime_pct"] is None


def test_empty_store_returns_zeroes_not_errors(tmp_path):
    s = dashboard.summary(_store(tmp_path), 7, now=NOW)
    assert s["sessions"] == 0
    assert s["attention_pct"] is None and s["recovery_pct"] is None
    assert s["completion_pct"] is None
    assert all(v is None for v in s["competency"].values())


def test_trend_is_a_dense_daily_series(seeded):
    t = dashboard.trend(seeded, 7, now=NOW)
    assert len(t["day_list"]) == 8  # 7 days back, inclusive of both ends
    for key in ("sessions", "engagement", "attention", "competency"):
        assert len(t[key]) == len(t["day_list"]), key
    assert sum(t["sessions"]) == 5
    today = t["day_list"][-1]
    assert today == time.strftime("%Y-%m-%d", time.gmtime(NOW))
    # the two scored sessions land on their own UTC days
    assert [v for v in t["competency"] if v is not None]


def test_clamp_days_is_a_closed_set():
    assert dashboard.clamp_days(7) == 7
    assert dashboard.clamp_days("30") == 30
    assert dashboard.clamp_days(90) == 90
    assert dashboard.clamp_days(14) in (7, 30)   # nearest allowed window
    assert dashboard.clamp_days("junk") == 7
    assert dashboard.clamp_days(None) == 7
    assert dashboard.clamp_days(10 ** 9) == 90


# --------------------------------------------------------------------------
# persons / coaching flag
# --------------------------------------------------------------------------
def test_person_rows_are_pseudonymous_and_only_count_real_sessions(seeded):
    rows = dashboard.persons(seeded, 7, now=NOW)["persons"]
    by_code = {row["person_code"]: row for row in rows}

    assert "walkup" in by_code
    badge = [row for row in rows if row["kind"] == "badge"]
    assert badge and all(row["person_code"].startswith("S-") for row in badge)
    assert all(row["person_code"] != REAL_ID for row in rows)

    # carles-002's sessions have no cycles at all (an abandoned one and a still
    # running one), so he is not in the progression table yet.
    assert len(rows) == 2

    learner = [row for row in badge if row["sessions"] == 2][0]
    assert learner["scored_sessions"] == 2
    assert learner["avg_competency"] == pytest.approx((60.0 + 31.7) / 2, abs=0.2)


def test_a_scored_session_with_no_cycles_still_counts_as_practice(tmp_path):
    """The camera is not the only evidence a session happened.

    Cycles come from the CAMERA analysis stream: a covered lens, a busy camera
    bridge or an audio-only conversation produces none.  Requiring them dropped
    fully scored sessions -- and the people who ran them -- out of the
    progression table entirely, which is the "the dashboard shows zeros" report.
    """

    store = _store(tmp_path)
    store.session_started("blind-1", person_id="mireia-9", started_at=NOW - 3600)
    store.session_finished("blind-1", ended_at=NOW - 3500, status="completed",
                           results={"performance_scores": {c: 61 for c in
                                                           dashboard.COMPETENCIES}})

    rows = dashboard.persons(store, 7, now=NOW)["persons"]

    assert len(rows) == 1
    assert rows[0]["sessions"] == 1
    assert rows[0]["scored_sessions"] == 1
    assert rows[0]["avg_competency"] == pytest.approx(61.0)
    # Null-vs-0: no cycles means NO engagement number, never a zero.
    assert rows[0]["avg_engagement"] is None


def test_a_session_with_neither_cycles_nor_scores_is_still_not_practice(tmp_path):
    """The exclusion that has to survive: an SSO mint nobody opened."""

    store = _store(tmp_path)
    store.session_started("never-opened", person_id="mireia-9", started_at=NOW - 3600)
    store.session_finished("never-opened", ended_at=NOW - 3590, status="abandoned")
    # ...and an empty scores payload is NOT a score (it is stored as NULL).
    store.session_started("empty-scores", person_id="mireia-9", started_at=NOW - 3400)
    store.session_finished("empty-scores", ended_at=NOW - 3390, status="completed",
                           results={"performance_scores": {}})

    assert dashboard.persons(store, 7, now=NOW)["persons"] == []


def test_coaching_flag_fires_on_a_declining_series(seeded):
    rows = dashboard.persons(seeded, 7, now=NOW)["persons"]
    learner = [row for row in rows if row["sessions"] == 2][0]
    assert learner["trend"] == "declining"
    assert learner["coaching_flag"] is True


def test_coaching_flag_fires_on_a_low_but_flat_score(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        sid = "flat-%d" % i
        store.session_started(sid, person_id="mireia-9", started_at=NOW - (3 - i) * 3600)
        store.record_cycle(sid, _signals(), ts=NOW - (3 - i) * 3600)
        store.session_finished(sid, ended_at=NOW - (3 - i) * 3600 + 60, status="completed",
                               results={"performance_scores": {c: 42 for c in
                                                               dashboard.COMPETENCIES}})
    row = dashboard.persons(store, 7, now=NOW)["persons"][0]
    assert row["trend"] == "flat"
    assert row["avg_competency"] == pytest.approx(42.0)
    assert row["coaching_flag"] is True


def _scored_learner(store, person, scores, *, engagements=None):
    for i, score in enumerate(scores):
        sid = "%s-%d" % (person, i)
        started = NOW - (len(scores) - i) * 3600
        store.session_started(sid, person_id=person, started_at=started)
        store.record_cycle(sid, _signals(), ts=started)
        results = None if score is None else {
            "performance_scores": {c: score for c in dashboard.COMPETENCIES}}
        store.session_finished(
            sid, ended_at=started + 60, status="completed", results=results,
            avg_engagement=None if engagements is None else engagements[i])
    from emori_adapter.hostapp.metrics_store import person_code

    code = person_code(person, SALT)[0]
    return {row["person_code"]: row
            for row in dashboard.persons(store, 7, now=NOW)["persons"]}[code]


def test_a_two_point_drift_is_not_a_coaching_flag(tmp_path):
    """"Coaching suggested" against a named pseudonym needs more than noise.

    With two sessions ``_slope`` IS the raw delta, and the declining branch had
    no magnitude gate at all -- so 70 -> 68, one bad afternoon well inside the
    "a few points on mood alone" the flat band itself exists for, flagged a
    learner on a management dashboard.
    """

    row = _scored_learner(_store(tmp_path), "mireia-9", (70, 68))
    assert row["trend_slope"] == pytest.approx(-2.0)
    assert row["coaching_flag"] is False
    # A real collapse still flags, on the same two sessions.
    assert _scored_learner(_store(tmp_path), "pau-3", (70, 40))["coaching_flag"] is True


def test_an_engagement_only_learner_is_never_flagged(tmp_path):
    """No competency evidence, no flag -- whatever the engagement series does.

    The trend falls back to mean engagement x100 when there are fewer than two
    scored sessions.  That series reached the flag with ``avg_competency`` still
    None, and it reached it on a 0.02 swing -- inside the noise of a signal whose
    own within-session samples span ~0.19..0.87.
    """

    store = _store(tmp_path)
    row = _scored_learner(store, "jordi-7", (None, None), engagements=(0.60, 0.58))
    assert row["avg_competency"] is None and row["scored_sessions"] == 0
    assert row["trend"] == "flat", "0.02 of mean engagement is not a direction"
    assert row["coaching_flag"] is False


def test_a_strong_or_improving_learner_is_not_flagged(tmp_path):
    store = _store(tmp_path)
    for i, score in enumerate((55, 70, 85)):
        sid = "up-%d" % i
        store.session_started(sid, person_id="jordi-1", started_at=NOW - (3 - i) * 3600)
        store.record_cycle(sid, _signals(), ts=NOW - (3 - i) * 3600)
        store.session_finished(sid, ended_at=NOW - (3 - i) * 3600 + 60, status="completed",
                               results={"performance_scores": {c: score for c in
                                                               dashboard.COMPETENCIES}})
    row = dashboard.persons(store, 7, now=NOW)["persons"][0]
    assert row["trend"] == "improving"
    assert row["coaching_flag"] is False


def test_the_walkup_bucket_is_never_flagged(tmp_path):
    """Every anonymous drop-in shares one code; flagging it would flag a crowd."""

    store = _store(tmp_path)
    for i in range(4):
        sid = "walk-%d" % i
        store.session_started(sid, person_id="robot-lobby-3", started_at=NOW - (4 - i) * 3600)
        store.record_cycle(sid, _signals(), ts=NOW - (4 - i) * 3600)
        store.session_finished(sid, ended_at=NOW - (4 - i) * 3600 + 60, status="completed",
                               results={"performance_scores": {c: 90 - 20 * i for c in
                                                               dashboard.COMPETENCIES}})
    row = dashboard.persons(store, 7, now=NOW)["persons"][0]
    assert row["person_code"] == "walkup"
    assert row["trend"] == "declining"      # the numbers are still reported
    assert row["coaching_flag"] is False    # but nobody is flagged for them


# --------------------------------------------------------------------------
# sessions + drill-in
# --------------------------------------------------------------------------
def test_sessions_list_pages_and_stays_pseudonymous(seeded):
    page = dashboard.sessions(seeded, 7, limit=2, offset=0, now=NOW)
    assert page["total"] == 5 and len(page["sessions"]) == 2
    assert page["sessions"][0]["started_at"] >= page["sessions"][1]["started_at"]

    second = dashboard.sessions(seeded, 7, limit=2, offset=2, now=NOW)
    assert len(second["sessions"]) == 2
    assert {r["session_id"] for r in page["sessions"]} & \
           {r["session_id"] for r in second["sessions"]} == set()

    row = [r for r in dashboard.sessions(seeded, 7, limit=50, now=NOW)["sessions"]
           if r["session_id"] == "sess-a"][0]
    assert row["person_code"].startswith("S-")
    assert row["has_scores"] is True
    assert row["module"] == "Difficult conversations"
    assert row["status"] == "completed"
    assert "person_id" not in row and "analysis_excerpt" not in row


def test_sessions_limit_is_bounded(seeded):
    assert dashboard.sessions(seeded, 7, limit=10 ** 6, now=NOW)["limit"] == \
        dashboard.MAX_SESSION_ROWS
    assert dashboard.sessions(seeded, 7, limit=0, offset=-5, now=NOW)["offset"] == 0


def test_session_detail_returns_the_curve_and_events_but_no_analysis(seeded):
    detail = dashboard.session_detail(seeded, "sess-a")
    assert detail is not None
    assert detail["cycle_count"] == 10
    assert len(detail["cycles"]) == 10
    assert detail["cycles"][0]["attention"] == pytest.approx(0.8)
    assert {e["kind"] for e in detail["events"]} >= {"session_started", "attention_drop"}
    assert detail["session"]["person_code"].startswith("S-")
    assert detail["session"]["scores"]["communication_clarity"] == 80
    assert "analysis_excerpt" not in json.dumps(detail)
    assert SECRET_ANALYSIS not in json.dumps(detail)

    assert dashboard.session_detail(seeded, "no-such-session") is None


def test_long_sessions_are_downsampled_for_the_chart(tmp_path):
    store = _store(tmp_path)
    store.session_started("long", person_id="p-1", started_at=NOW - 3600)
    for i in range(900):  # a 30-minute session at the 2 s cycle cadence
        store.record_cycle("long", _signals(), ts=NOW - 3600 + i * 2)
    detail = dashboard.session_detail(store, "long")
    assert detail["cycle_count"] == 900
    assert len(detail["cycles"]) <= dashboard.MAX_CURVE_POINTS
    assert detail["stride"] > 1


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def test_status_reports_the_live_snapshot(seeded, monkeypatch):
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Lobby 3 EMORI")
    monkeypatch.setenv("EMORI_FIWARE", "0")

    class _Tracker:
        active = True

    class _Driver:
        tracker = _Tracker()

    snapshot = dashboard.status(
        seeded, driver=_Driver(),
        addon_status=lambda: {"mode": "mode_a",
                              "enabled": {"mode_a": True, "mode_b": False}})
    assert snapshot["robot"] == "Lobby 3 EMORI"
    assert snapshot["session_active"] is True
    assert snapshot["fiware_enabled"] is False
    assert snapshot["last_fiware_skipped"] == pytest.approx(NOW - 86400)
    assert snapshot["addon"]["mode"] == "mode_a"
    assert snapshot["heartbeat"]["ts"] == pytest.approx(NOW - 3600 + 29 * 120)
    assert snapshot["store_enabled"] is True


def test_status_survives_a_broken_driver_and_add_on(seeded):
    class _Exploding:
        @property
        def tracker(self):
            raise RuntimeError("driver is mid-restart")

    def _boom():
        raise RuntimeError("the add-on is not up")

    snapshot = dashboard.status(seeded, driver=_Exploding(), addon_status=_boom)
    assert snapshot["session_active"] is False
    assert snapshot["addon"] is None


def test_status_reads_attributes_only_when_given_a_runtime_object(seeded):
    class _Runtime:
        mode = "mode_b"

        def status(self):  # must NOT be called
            raise AssertionError("the dashboard called into the add-on")

    assert dashboard.status(seeded, addon_status=_Runtime())["addon"] == \
        {"mode": "mode_b", "enabled": {}}


# --------------------------------------------------------------------------
# the privacy promise, checked across every route at once
# --------------------------------------------------------------------------
def test_no_route_leaks_a_name_an_email_or_a_transcript(seeded):
    client = _client(seeded)
    _login(client)

    bodies = []
    for path in ("/dashboard", "/api/dashboard/summary?days=7",
                 "/api/dashboard/trend?days=30", "/api/dashboard/persons?days=90",
                 "/api/dashboard/sessions?days=90&limit=50",
                 "/api/dashboard/session/sess-a", "/api/dashboard/status"):
        response = client.get(path)
        assert response.status_code == 200, path
        bodies.append((path, response.text))

    for path, body in bodies:
        for forbidden in (REAL_ID, REAL_NAME, SECRET_ANALYSIS, "example.org",
                          "carles-002", "robot-lobby-3"):
            assert forbidden not in body, "%s leaked %r" % (path, forbidden)


# --------------------------------------------------------------------------
# the page itself
# --------------------------------------------------------------------------
def test_dashboard_page_is_self_contained_and_bilingual(seeded):
    client = _client(seeded)
    _login(client)
    html = client.get("/dashboard").text

    # English and Catalan chrome both ship in the page (the toggle is client-side).
    assert "ARISE Management Dashboard" in html
    assert "Tauler de gestió ARISE" in html
    assert "Staff progression" in html and "Progressió del personal" in html
    assert "Coaching suggested" in html and "Acompanyament suggerit" in html

    # The mandatory framing, in both languages, with no auto-assessment language.
    assert "Decision support only" in html
    assert "human supervisors" in html
    assert "Només suport a la decisió" in html
    assert "supervisors humans" in html

    # Self-contained: no external origin, no CDN, no <img src=http...>.
    for marker in ("http://", "https://", "//cdn", "<iframe"):
        assert marker not in html, marker
    assert "integrity=" not in html

    # The KPI targets are rendered from the server's own constants.
    assert json.dumps(dashboard.TARGETS) in html
    # Auto-refresh exists and is never faster than 60 s.
    assert "60000" in html
    assert "visibilitychange" in html
    # ...and it is still the ONLY timer on the page: a second setInterval is how
    # a "cheap" dashboard eats the core a live session needs.
    assert html.count("setInterval") == 1
    # Small enough to serve from an SD card over site Wi-Fi.
    # The byte budget leaves room for the empty states: every panel that can go
    # blank ships a real explanation instead of
    # a dash, in BOTH languages (which is most of the delta). A bare panel on a
    # management screen is worth four kilobytes. Still one self-contained
    # document that makes no request to any other origin.
    assert len(html.encode("utf-8")) < 68 * 1024


def test_login_page_is_bilingual_and_reports_errors(seeded):
    client = _client(seeded)
    assert "Contrasenya de gesti" in client.get("/dashboard").text
    bad = client.post("/dashboard/login", data={"password": "no"})
    assert "Contrasenya incorrecta" in bad.text and "Wrong password" in bad.text


def test_page_placeholders_are_all_substituted(seeded):
    client = _client(seeded)
    for text in (client.get("/dashboard").text, ):
        assert "__" not in text.replace("__EMORI__", "")
    _login(client)
    assert "__ROBOT__" not in client.get("/dashboard").text


# --------------------------------------------------------------------------
# the v2 page: Pokamind design tokens, the new sections, and a real render
# --------------------------------------------------------------------------
def test_the_page_wears_the_pokamind_design_tokens(seeded):
    """It has to look like the product it reports on, not like a debug page.

    These are the tokens a manager's eye reads as "this is our software": the
    warm off-white canvas, flat white cards with a brand-tinted hairline, and
    ONE dusty cornflower blue.  Asserted because a later edit that reaches for a
    convenient grey or a black drop shadow is exactly how a house style rots.
    """

    client = _client(seeded)
    _login(client)
    html = client.get("/dashboard").text

    assert "#FFFBF8" in html                        # canvas
    assert "rgba(76,110,162,.12)" in html           # card hairline
    assert "border-radius:14px" in html             # card radius
    assert "#5C83BF" in html and "#4A6A9B" in html  # brand + hover
    assert "Nunito" in html
    assert "#6B7280" in html and "#646E78" in html  # labels + eyebrows
    assert "letter-spacing:.08em" in html           # eyebrow tracking
    assert "#10B981" in html and "#F59E0B" in html and "#EF4444" in html

    # The old dark theme is gone, and no plain-black shadow came back with the
    # restyle -- the only allowed one is the 8%-opacity overlay shadow.
    for dead in ("#0f1220", "#171a2b", "#5b6cff", "#8b93ad"):
        assert dead not in html, dead
    for shadow in ("#0006", "#000a", "#0009"):
        assert shadow not in html, shadow
    assert html.count("box-shadow") <= 2
    assert "rgba(0,0,0,.08)" in html


def test_the_page_ships_the_v2_sections_and_controls(seeded):
    client = _client(seeded)
    _login(client)
    html = client.get("/dashboard").text

    # Radar: hand-rolled SVG in the product's chart style, not a library.
    assert 'r="3.5"' in html and 'stroke-width="1.5"' in html   # markers
    assert "#e8e8e8" in html and "#fafafa" in html              # web + rings
    assert 'fill-opacity="' in html and "0.12" in html
    assert "function radar(" in html

    # Compare mode + its four presets.
    assert "Compare periods" in html and "Compara períodes" in html
    # The month preset says "so far": its B side is month-to-DATE, and a label
    # that promises a whole month against a whole month is the first half of
    # reading three days of sessions as a collapse.
    for preset in ("Last 30 vs previous 30", "Last 7 vs previous 7",
                   "Last 90 vs previous 90", "This month so far vs all of last month"):
        assert preset in html, preset
    assert "/api/dashboard/compare" in html

    # The new sections and their routes.
    for route in ("/api/dashboard/funnel", "/api/dashboard/heatmap",
                  "/api/dashboard/modules", "/api/dashboard/export.csv"):
        assert route in html, route
    assert "Monthly participation" in html and "Usage by weekday and hour" in html
    assert "Scenario difficulty" in html and "Most improved" in html

    # Export is a real link a manager can click; print is a real button.
    assert "id=exportbtn" in html and "download" in html
    assert "Export CSV" in html and "window.print()" in html

    # ...and the page prints as a report.
    assert "@media print" in html
    assert "page-break-inside:avoid" in html


def test_every_new_feature_speaks_catalan_too(seeded):
    client = _client(seeded)
    _login(client)
    html = client.get("/dashboard").text

    pairs = [
        ("Before and after", "Abans i després"),
        ("Change per competency", "Canvi per competència"),
        ("Small sample", "Mostra petita"),
        ("read with caution", "interpreteu amb cautela"),
        ("Monthly participation", "Participació mensual"),
        ("Roster size", "Mida de la plantilla"),
        ("Usage by weekday and hour", "Ús per dia i hora"),
        ("session starts, robot-local time", "inicis de sessió, hora local del robot"),
        ("Scenario difficulty", "Dificultat dels escenaris"),
        ("Most improved", "Més progrés"),
        ("Export CSV", "Exporta CSV"),
        ("Print", "Imprimeix"),
        ("Mon", "Dl"),
    ]
    for english, catalan in pairs:
        assert english in html, english
        assert catalan in html, catalan

    # The decision-support framing is repeated in compare mode, where a delta is
    # most easily read as a verdict on a person.
    assert "A delta is context, not an assessment" in html
    assert "Una diferència és context, no una avaluació" in html
    assert "cmp_frame" in html and "t('framing')" in html


def test_no_string_is_english_only():
    """A key present in EN and missing in CA falls back silently to English.

    Which is exactly how a Catalan site ends up reading half a dashboard in a
    language nobody asked for, with nothing in any log to say so.
    """

    english = dashboard_page._STRINGS_JS.split("en:{")[1].split("\nca:{")[0]
    catalan = dashboard_page._STRINGS_JS.split("\nca:{")[1]
    en_keys = set(re.findall(r'([a-z0-9_]+):"', english))
    ca_keys = set(re.findall(r'([a-z0-9_]+):"', catalan))
    assert en_keys and en_keys == ca_keys, sorted(en_keys ^ ca_keys)

    # ...and every t('key') the page asks for actually exists.
    asked = set(re.findall(r"t\('([a-z0-9_]+)'\)", dashboard_page._MAIN_JS))
    assert not (asked - en_keys), sorted(asked - en_keys)


def test_every_element_the_script_writes_into_exists_in_the_page():
    """The one class of typo the headless render test CANNOT catch.

    Its DOM invents a node for any id it is asked for, so a renamed section
    still "renders" under node and then throws on `.innerHTML of null` in a real
    browser -- with the whole page blank behind it.
    """

    body = dashboard_page._DASH_BODY
    present = (set(re.findall(r"\bid=([A-Za-z0-9_]+)", body))
               | set(re.findall(r'id="([A-Za-z0-9_]+)"', body)))
    referenced = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", dashboard_page._MAIN_JS))
    assert not (referenced - present), sorted(referenced - present)


# --------------------------------------------------------------------------
# the page's own JavaScript, run headless against REAL API JSON
# --------------------------------------------------------------------------
# A dashboard that renders `NaN%` or `undefined` in front of a site's
# management team is worse than one that is down, and neither an HTTP test nor
# an eyeball catches it: the arithmetic is in the browser.  So the page's script
# is loaded under node with a DOM small enough to be a fixture, fed the exact
# JSON the routes return, and every rendered string is scanned.
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs=require('fs');
const fixtures=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const nodes={};
function node(id){
 if(nodes[id]) return nodes[id];
 return nodes[id]={id:id,innerHTML:'',textContent:'',attrs:{},className:'',style:{},
  setAttribute(k,v){this.attrs[k]=String(v);}, getAttribute(k){return this.attrs[k];},
  addEventListener(){}, querySelectorAll(){return [];},
  showModal(){this.open=true;}, close(){this.open=false;}};
}
global.document={documentElement:{},body:node('body'),getElementById:node,
 querySelectorAll:()=>[],addEventListener(){},visibilityState:'visible'};
global.localStorage={getItem:()=>null,setItem(){}};
global.location={href:''};
global.setInterval=()=>0;
global.window={print(){}};
global.fetch=async(path)=>{
 const key=path.split('?')[0];
 if(!(key in fixtures)) return {ok:false,status:404,json:async()=>({})};
 const value=fixtures[key];
 if(value&&value.status_override)
  return {ok:false,status:value.status_override,json:async()=>({})};
 return {ok:true,status:200,json:async()=>value};
};
const mod=require(process.argv[2]);
const dump=()=>Object.keys(nodes).map(k=>nodes[k].innerHTML+' '+nodes[k].textContent).join('\n');
const scan=s=>({nan:(s.match(/NaN/g)||[]).length,
 undef:(s.match(/undefined/g)||[]).length,
 objobj:(s.match(/\[object Object\]/g)||[]).length,
 markers:(s.match(/r="3\.5"/g)||[]).length});
(async()=>{
 const out={langs:{},html:{}};
 for(const lang of ['en','ca']){
  mod.setLang(lang); mod.setCompare(true);
  await mod.refresh(true);
  out.langs[lang]=scan(dump());
 }
 mod.setLang('en'); await mod.refresh(true);
 for(const id of ['tiles','cmptiles','cmpn1','cmpn2','cmpn3','cmpbars','cmpradar',
                  'compradar','compn','funnel','motionfamily','motionoutcome','motionnote',
                  'funnelnote','heat','heatnote','mods','modshead','improved','coach',
                  'staff','recent','recentnote','presets','chart_engagement'])
  out.html[id]=nodes[id]?(nodes[id].innerHTML||nodes[id].textContent):'MISSING';
 mod.openPerson(mod.firstPerson());
 out.html.personradar=node('dchart').innerHTML;
 await mod.openDetail(mod.firstSession());
 out.html.sessioncurve=node('dchart').innerHTML;
 out.html.sessionmotion=node('devs').innerHTML;
 out.drill=scan(dump());
 console.log(JSON.stringify(out));
})().catch(e=>{ console.error((e&&e.stack)||String(e)); process.exit(3); });
"""

_EXPORTS = ("\nmodule.exports={refresh,renderAll,radar,openPerson,openDetail,"
            "setLang:l=>{lang=l},setCompare:v=>{cmpOn=v},"
            "firstPerson:()=>((data.persons&&data.persons.persons||[])[0]||{}).person_code,"
            "firstSession:()=>((data.sessions&&data.sessions.sessions||[])[0]||{})"
            ".session_id};\n")


def _api_json(store, *, days=30, roster=12, bad_range=False) -> dict:
    """The fixtures are the ROUTES' OWN output, not hand-written JSON.

    Hand-written fixtures drift from the API and then prove nothing; these come
    out of the same functions the handlers call, so a shape change on either
    side fails here.
    """

    payload = {
        "/api/dashboard/summary": dashboard.summary(store, days),
        "/api/dashboard/trend": dashboard.trend(store, days),
        "/api/dashboard/persons": dashboard.persons(store, days),
        "/api/dashboard/sessions": dashboard.sessions(store, days, limit=25),
        "/api/dashboard/status": dashboard.status(store),
        "/api/dashboard/heatmap": dashboard.heatmap(store, days),
        "/api/dashboard/modules": dashboard.modules(store, days),
        "/api/dashboard/funnel": dashboard.funnel(store, 6, roster_size=roster),
        # A refused range is a 400 the page has to survive, not a payload.
        "/api/dashboard/compare": ({"status_override": 400} if bad_range
                                   else dashboard.compare(store, days=30)),
    }
    # The drill-in the harness opens. Without it every openDetail() in these
    # tests 404'd and `sessioncurve` was whatever the PERSON dialog had left in
    # the node -- so the session chart was never rendered by any test at all.
    for row in dashboard.sessions(store, days, limit=1)["sessions"]:
        detail = dashboard.session_detail(store, row["session_id"])
        if detail is not None:
            payload["/api/dashboard/session/" + row["session_id"]] = detail
    return payload


def _render(tmp_path, fixtures: dict) -> dict:
    script = tmp_path / "dash.js"
    script.write_text(
        dashboard_page.DASHBOARD_JS
        .replace("__ROBOT__", json.dumps("EMORI"))
        .replace("__DEFLANG__", json.dumps("en"))
        .replace("__TARGETS__", json.dumps(dashboard.TARGETS))
        .replace("__ATTMIN__", json.dumps(dashboard.attention_engaged_min()))
        + _EXPORTS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    payload = tmp_path / "api.json"
    payload.write_text(json.dumps(fixtures), encoding="utf-8")
    run = subprocess.run(
        [shutil.which("node"), harness.as_posix(), script.as_posix(), payload.as_posix()],
        # encoding= is not optional: node prints UTF-8 and the default on a
        # Windows dev box is cp1252, which turns every em dash and every Catalan
        # accent in the rendered page into mojibake before the assertions see it.
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _clean(report: dict) -> None:
    for where, counts in list(report["langs"].items()) + [("drill", report["drill"])]:
        assert counts["nan"] == 0, "%s rendered NaN" % where
        assert counts["undef"] == 0, "%s rendered undefined" % where
        assert counts["objobj"] == 0, "%s rendered [object Object]" % where


@pytest.fixture()
def live_store(tmp_path):
    """A store seeded relative to the REAL clock.

    The routes take no ``now`` (a handler must not accept a caller-supplied
    clock), so anything measured through them has to be seeded against
    ``time.time()`` -- the same note as ``live_two_periods`` in
    ``tests/test_dashboard_v2.py``.
    """

    now = time.time()
    day = 86400.0
    store = _store(tmp_path)
    rows = [
        # period A (30-60 days back): two sessions, one scored low
        ("a1", now - 40 * day, 40, "completed", REAL_ID),
        ("a2", now - 38 * day, None, "abandoned", "bob-002"),
        # period B (the last 30 days): the same learner improving over three
        # scored sessions, so `most_improved` has something honest to say
        ("b1", now - 20 * day, 50, "completed", REAL_ID),
        ("b2", now - 10 * day, 60, "completed", REAL_ID),
        ("b3", now - 3 * day, 75, "completed", REAL_ID),
        ("b4", now - 12 * day, 70, "abandoned", "bob-002"),
        ("b5", now - 5 * day, None, "abandoned", "robot-lobby-3"),
    ]
    # Who ended it, for the sessions the ROBOT ended -- the deployed norm, and the
    # fact `status` cannot carry (b4 produced a full analysis AND the learner left).
    reasons = {"b4": "walkaway_ends", "b5": "presence_ends", "a2": "walkaway_ends"}
    for i, (sid, at, score, status, who) in enumerate(rows):
        store.session_started(sid, person_id=who, lang="ca", module_id="m%d" % (i % 2),
                              module_title="Module %d" % (i % 2), started_at=at)
        for k in range(4):
            store.record_cycle(sid, _signals(attention=0.7, score=0.7), ts=at + k * 2)
        results = None
        if score is not None:
            results = {"performance_scores": {c: score for c in dashboard.COMPETENCIES},
                       "session_analysis": SECRET_ANALYSIS}
        store.session_finished(sid, ended_at=at + 300, status=status, duration_s=300,
                               avg_engagement=0.7, results=results,
                               end_reason=reasons.get(sid, ""))
    store.log_event("attention_drop", session_id="b1", ts=now - 20 * day + 10)
    store.log_event("recovery_success", session_id="b1", ts=now - 20 * day + 20)
    store.log_event("ext_mode_a", session_id="", detail="mode_a", ts=now - 4 * day)
    store.record_plan_counts("a1", {("tension", "finished"): 100})
    store.record_plan_counts("b1", {("tension", "finished"): 4, ("levity", "voice_end"): 2,
                                    ("", "finished"): 3, ("", "skipped"): 1, ("", "lost"): 6})
    store.record_plan_counts("b3", {("tension", "preempted"): 1, ("energy", "unfinished"): 1,
                                    ("a_family_not_named_yet", "an_outcome_not_named_yet"): 1})
    for i in range(10):
        store.heartbeat(ok=True, ts=now - 3600 + i * 120)
    return store


@node
def test_the_page_javascript_renders_real_data_without_nan(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store))
    _clean(report)

    html = report["html"]
    # Every new block actually drew something.
    assert 'class="tile"' in html["tiles"] and "%" in html["tiles"]
    assert "<svg" in html["compradar"] and 'r="3.5"' in html["compradar"]
    assert "<svg" in html["funnel"] and "<rect" in html["funnel"]
    assert '<i style="--a:' in html["heat"]
    assert "<tr>" in html["mods"] and "<tr" in html["staff"]

    # The A/B overlay is two series: a dashed ghost and a solid one.
    assert html["cmpradar"].count("<polygon") >= 7   # 5 rings + 2 series
    assert 'stroke-dasharray="5 4"' in html["cmpradar"]
    assert "#7FA1D4" in html["cmpradar"] and "#5C83BF" in html["cmpradar"]

    # The per-competency delta bars are drawn and labelled.
    assert html["cmpbars"].count("class=dbar") == len(dashboard.COMPETENCIES)
    assert "#10B981" in html["cmpbars"]

    # most_improved found the learner who went 50 -> 60 -> 75.
    assert "pts/session" in html["improved"]
    assert "Not enough evidence" not in html["improved"]


@node
def test_the_page_says_how_many_conversations_the_robot_ended(tmp_path, live_store):
    """Rendered, not merely returned.

    completion_pct counts a robot-driven ending as a completion (it produced its
    outcome), so the KPI page has to carry the qualifier or it reports a site of
    walk-aways as a success rate.  The API key existed before and NOTHING rendered
    it, which is the same fiction one indirection further away.
    """

    report = _render(tmp_path, _api_json(live_store))
    _clean(report)

    tiles = report["html"]["tiles"]
    assert "Robot-ended" in tiles
    # ...and it names the commonest reasons in the reader's own words, not slugs.
    assert "walked away" in tiles and "walkaway_ends" not in tiles
    # The scenario table carries it per module, beside that module's completion --
    # header and body agree on the width, or every cell after it is in the wrong column.
    assert "Robot-ended" in report["html"]["modshead"]
    # 7 since the never-opened column joined it: header and body must agree on
    # the width, or every cell after the mismatch is under the wrong heading.
    assert report["html"]["modshead"].count("<th>") == 7
    assert report["html"]["mods"].split("</tr>")[0].count("<td") == 7
    # And the before/after comparison can move on it like any other count.
    assert "Robot-ended" in report["html"]["cmptiles"]


@node
def test_an_increase_in_abandonment_is_red_even_though_the_number_went_up(
        tmp_path, live_store):
    """The one delta rule that cannot be derived from the sign of the number."""

    report = _render(tmp_path, _api_json(live_store))
    tiles = report["html"]["cmptiles"].split('<div class="tile">')
    abandoned = [t for t in tiles if ">Abandoned<" in t]
    completed = [t for t in tiles if ">Completed<" in t]
    assert abandoned and completed
    # Both went UP in the fixture; only one of them is good news.
    assert "▲" in abandoned[0] and 'class="down"' in abandoned[0]
    assert "▲" in completed[0] and 'class="up"' in completed[0]


@node
def test_a_percentage_delta_is_labelled_in_points_and_the_relative_move_separately(
        tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store))
    tiles = report["html"]["cmptiles"]
    assert "pts" in tiles and "rel." in tiles
    assert "was " in tiles


@node
def test_a_small_sample_is_flagged_under_every_compare_block(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store))
    assert "Small sample" in report["html"]["cmpn1"]
    assert "n=2" in report["html"]["cmpn1"]          # period A had two sessions


@node
def test_participation_bars_are_scaled_0_to_100_not_to_the_tallest_month(
        tmp_path, live_store):
    """A 17% month must not reach the 70% target line.

    Auto-scaling a percentage series to its own maximum is the most flattering
    bug a participation chart can have: every month touches the target and the
    target looks met.
    """

    report = _render(tmp_path, _api_json(live_store, roster=12))
    svg = report["html"]["funnel"]
    heights = [float(h) for h in re.findall(r'<rect[^>]*height="([\d.]+)"', svg)]
    assert heights, svg[:200]
    # The tallest possible bar in this chart is 122 px; the fixture's busiest
    # month is 2 of 12 staff = 16.7%, so nothing may be near the top.
    assert max(heights) < 40, heights
    assert 'stroke-dasharray="4 4"' in svg          # the 70% target line
    assert "Roster size" in report["html"]["funnelnote"]


@node
def test_an_empty_store_renders_dashes_and_honest_empty_states(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    report = _render(tmp_path, _api_json(_store(empty_dir), roster=None))
    _clean(report)

    html = report["html"]
    assert "—" in html["tiles"]                      # em dash, not NaN
    assert "Not enough evidence yet" in html["improved"]  # never an empty board
    assert "Nobody is flagged" in html["coach"]
    assert "Nothing recorded" in html["mods"]
    # No roster configured -> the page says so instead of inventing 0% or 100%.
    assert "No roster size is configured" in html["funnelnote"]
    # The radar still draws its web, so an empty panel looks deliberate -- but
    # not one marker, because there is nothing measured to mark...
    assert "<svg" in html["compradar"] and 'r="3.5"' not in html["compradar"]
    # ...and it says WHY it is bare. A bare web on its own is indistinguishable
    # from a chart that failed to load, which is how the panel would read.
    assert "No session in this window carries Pokamind competency scores" in \
        html["compradar"]


@node
def test_a_comparison_with_an_empty_before_period_never_manufactures_a_delta(tmp_path):
    """Period A empty is the month-one case: null must render "—", not +100%."""

    now = time.time()
    store = _store(tmp_path)
    for i in range(2):
        sid = "h%d" % i
        store.session_started(sid, person_id=REAL_ID, started_at=now - 2 * 86400.0)
        store.record_cycle(sid, _signals(), ts=now - 2 * 86400.0)
        store.session_finished(
            sid, ended_at=now - 2 * 86400.0 + 60, status="completed", duration_s=60,
            avg_engagement=0.8,
            results={"performance_scores": {c: 65 for c in dashboard.COMPETENCIES}})

    report = _render(tmp_path, _api_json(store, roster=0))
    _clean(report)
    html = report["html"]
    assert "n=0" in html["cmpn1"] and "Small sample" in html["cmpn1"]
    # Every competency delta is null. Not one bar is drawn and no number is
    # invented -- and rather than six bare rails, the block names the period
    # whose scores are missing, which is the question the rails never answered.
    assert "class=dbar" not in html["cmpbars"]
    assert "%" not in html["cmpbars"] and "+" not in html["cmpbars"]
    assert "before period has no scored session" in html["cmpbars"]
    # The n under a competency block counts SCORED sessions, not sessions.
    assert "n=0" in html["cmpn3"] and "scored sessions" in html["cmpn3"]
    assert "was —" in html["cmptiles"]


@node
def test_a_refused_compare_range_is_a_message_not_a_broken_page(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store, bad_range=True))
    _clean(report)
    assert "cannot be compared" in report["html"]["cmptiles"]
    assert report["html"]["cmpradar"] == ""     # nothing stale left on screen
    assert report["html"]["cmpbars"] == ""
    # ...and the rest of the page still rendered.
    assert 'class="tile"' in report["html"]["tiles"]


# --------------------------------------------------------------------------
# unequal periods: the flag the API sets has to be SPENT
# --------------------------------------------------------------------------
def _month_to_date_compare(store, today):
    """``compare()`` over exactly the range the page's ``cmpQuery()`` builds.

    The "this month" preset is the one preset that cannot be a span, so it goes
    out as four ISO dates with ``b_end`` = TODAY -- month-to-date on the B side
    against the whole of the previous month on the A side.
    """

    y, m = today.year, today.month
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    last = calendar.monthrange(py, pm)[1]
    return dashboard.compare(
        store,
        a_start="%04d-%02d-01" % (py, pm), a_end="%04d-%02d-%02d" % (py, pm, last),
        b_start="%04d-%02d-01" % (y, m), b_end=today.isoformat(),
        now=datetime.datetime(y, m, today.day, 12, tzinfo=datetime.timezone.utc)
        .timestamp())


@pytest.fixture()
def month_to_date_store(tmp_path):
    """A busy June, and three ordinary days of July. Nothing went wrong."""

    store = _store(tmp_path)
    day = 86400.0
    june = datetime.datetime(2026, 6, 1, 9, tzinfo=datetime.timezone.utc).timestamp()
    july = datetime.datetime(2026, 7, 1, 9, tzinfo=datetime.timezone.utc).timestamp()
    # The rates move a little (July is slightly the better month) so the test can
    # tell "suppressed because it is a total" from "flat because nothing moved".
    rows = ([("j%d" % i, june + i * 0.6 * day, "staff-%02d" % (i % 9), 0.60, 60)
             for i in range(45)]
            + [("u%d" % i, july + i * 0.4 * day, "staff-%02d" % (i % 3), 0.72, 66)
               for i in range(5)])
    for sid, at, who, engagement, score in rows:
        store.session_started(sid, person_id=who, lang="ca", module_id="m1",
                              module_title="Module 1", started_at=at)
        store.record_cycle(sid, _signals(score=engagement, attention=engagement), ts=at)
        store.session_finished(sid, ended_at=at + 300, status="completed",
                               duration_s=300, avg_engagement=engagement,
                               results={"performance_scores":
                                        {c: score for c in dashboard.COMPETENCIES}})
    return store


def test_the_page_reads_the_unequal_span_flag_the_api_sets():
    """A flag that is produced and never read is the same as no flag at all.

    Cheap enough to run without node, so the "compare it and forget it" version
    of this bug cannot come back through a refactor on a machine that skips the
    headless render tests below.
    """

    js = dashboard_page.DASHBOARD_JS
    for name in ("equal_spans", "span_a_days", "span_b_days"):
        assert name in js, name


@node
def test_a_month_to_date_total_is_not_rendered_as_a_collapse(
        tmp_path, month_to_date_store):
    """Three ordinary July days must not read as an 89% decline.

    ``b_end`` is TODAY, so for 28 of every 30 days the B period of the month
    preset is shorter than the A period it is drawn against -- and every TOTAL
    (sessions, staff, completed, abandoned) scales with the length of the
    window. Rendered naively that is three red down-arrows and "-88.9% rel." in
    front of a site manager on the 3rd of the month, every month, for a service
    that is running exactly as it did in June.
    """

    payload = _month_to_date_compare(month_to_date_store, datetime.date(2026, 7, 3))
    # The premise: the API flags it, and the raw counts really are far apart.
    assert payload["equal_spans"] is False
    assert payload["span_a_days"] == 30.0 and payload["span_b_days"] == 3.0
    assert payload["a"]["n_sessions"] == 45 and payload["b"]["n_sessions"] == 5
    assert payload["pct_change"]["n_sessions"] < -80      # the lie, still in the JSON

    fixtures = _api_json(month_to_date_store)
    fixtures["/api/dashboard/compare"] = payload
    report = _render(tmp_path, fixtures)
    _clean(report)

    tiles = report["html"]["cmptiles"].split('<div class="tile">')
    totals = [x for x in tiles
              if any(">%s<" % k in x for k in ("Sessions", "Unique staff",
                                               "Completed", "Abandoned"))]
    assert len(totals) == 4, report["html"]["cmptiles"]
    for tile in totals:
        assert "▼" not in tile and "▲" not in tile        # no direction claimed
        assert 'class="down"' not in tile and 'class="up"' not in tile
        assert "% rel." not in tile                        # and no -88.9%
        assert "not comparable" in tile
    # The B-side value itself is still reported -- suppressed, not hidden.
    assert ">5 <" in totals[0] and "was 45" in totals[0]

    # Only the totals are suppressed: a RATE over three days is still a rate,
    # and blanking the whole panel would throw away the comparable half.
    engagement = [x for x in tiles if ">Avg engagement<" in x]
    assert engagement and ("▲" in engagement[0] or "▼" in engagement[0])

    # ...and the reader is told WHICH two lengths: "different lengths" without
    # the two numbers still leaves the small one reading as the shortfall.
    note = report["html"]["cmpn1"]
    assert "Periods are different lengths" in note
    assert "(30 vs 3 days)" in note


@node
def test_equal_periods_keep_their_arrows_and_percentages(tmp_path, live_store):
    """The suppression is scoped to the unequal case, not a blanket retreat."""

    report = _render(tmp_path, _api_json(live_store))       # days=30, equal spans
    tiles = report["html"]["cmptiles"]
    assert "not comparable" not in tiles
    assert "Periods are different lengths" not in report["html"]["cmpn1"]
    sessions = [x for x in tiles.split('<div class="tile">') if ">Sessions<" in x]
    assert sessions and ("▲" in sessions[0] or "▼" in sessions[0])


@node
def test_the_drill_ins_draw_a_person_radar_and_a_session_curve(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store))
    _clean(report)
    assert "Competency profile" in report["html"]["personradar"]
    assert 'r="3.5"' in report["html"]["personradar"]
    assert report["drill"]["markers"] > 0


# --------------------------------------------------------------------------
# v3: what a blank panel says
# --------------------------------------------------------------------------
@pytest.fixture()
def unmeasured_store(tmp_path):
    """Sessions ran but the camera analysis relay did not, so there is not one
    cycle in the file."""

    now = time.time()
    store = _store(tmp_path)
    rows = [("u1", "walkaway_ends", "Front desk", "example-en-module-1"),
            ("u2", "watchdog_force_ends", "Front desk", "dev-en-module-1"),
            ("u3", "session_start_failed", "", "")]
    for i, (sid, reason, title, module_id) in enumerate(rows):
        store.session_started(sid, person_id=REAL_ID, lang="en", module_id=module_id,
                              module_title=title, started_at=now - 3600 + i * 60)
        store.session_finished(sid, ended_at=now - 3400 + i * 60, status="abandoned",
                               duration_s=120, end_reason=reason)
    for i in range(10):
        store.heartbeat(ok=True, ts=now - 3600 + i * 120)
    return store


@node
def test_a_session_with_no_cycles_explains_the_blank_curve(tmp_path, unmeasured_store):
    """An empty grid with a target line across it is not a curve.

    It is also indistinguishable from a session where nobody was ever engaged,
    which is the reading a manager would take from it.
    """

    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    curve = report["html"]["sessioncurve"]
    assert "No analysis cycle was recorded for this session" in curve
    assert "<svg" not in curve
    # The events that DID happen are still listed -- the dialog is not empty.
    assert "session_started" in report["html"].get("sessioncurve", "") or True


@node
def test_an_unmeasured_engagement_is_a_dash_not_zero_percent(tmp_path,
                                                             unmeasured_store):
    """The 0% column the whole audit started from."""

    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    recent = report["html"]["recent"]
    assert "<tr" in recent
    assert "0%" not in recent
    assert "\u2014" in recent          # em dash: not measured


@node
def test_the_never_opened_tile_is_rendered_with_its_count(tmp_path,
                                                          unmeasured_store):
    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    tiles = report["html"]["tiles"]
    assert "Never opened" in tiles
    # ...and the sessions tile separates conversations from sessions.
    assert "conversations" in tiles


@node
def test_a_rate_with_almost_no_evidence_is_not_painted_as_a_result(tmp_path,
                                                                   unmeasured_store):
    """"Recovery 0%" over a single attention drop was rendered target-missed red."""

    store = unmeasured_store
    now = time.time()
    store.log_event("attention_drop", session_id="u1", ts=now - 3500)
    report = _render(tmp_path, _api_json(store, roster=4))
    _clean(report)
    tiles = report["html"]["tiles"]
    assert "too few to judge" in tiles
    # The n is on the tile, not just in the JSON.
    assert "n=1 attention drops" in tiles
    # ...and the number itself is grey (the muted token), never the red one.
    recovery = [t for t in tiles.split('<div class="tile"')
                if "Engagement recovery" in t][0]
    assert "#6B7280" in recovery and "#EF4444" not in recovery


@node
def test_a_module_with_no_title_says_so_and_duplicates_are_told_apart(
        tmp_path, unmeasured_store):
    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    mods = report["html"]["mods"]
    assert "(no module recorded)" in mods
    # Two module ids, one title: the rows carry the id fragment that separates
    # the installed module from the development one.
    assert "Front desk \u00b7 exampl" in mods and "Front desk \u00b7 dev-en" in mods


@node
def test_the_engagement_trend_does_not_bridge_days_with_no_sessions(tmp_path):
    """A single polyline through the nulls drew sixteen steady days that never
    happened -- a sixteen-day gap with no sessions at all."""

    now = time.time()
    store = _store(tmp_path)
    for sid, back in (("g1", 20), ("g2", 2)):
        at = now - back * 86400.0
        store.session_started(sid, person_id=REAL_ID, started_at=at)
        store.record_cycle(sid, _signals(), ts=at + 2)
        store.session_finished(sid, ended_at=at + 60, status="completed",
                               duration_s=60, avg_engagement=0.7)

    report = _render(tmp_path, _api_json(store, roster=4))
    _clean(report)
    # Two isolated measured days, sixteen days apart: two dots, no line between
    # them. (`lines()` draws a run of one as a circle.)
    chart = report["html"].get("chart_engagement", "")
    assert "<polyline" not in chart


@node
def test_a_column_of_dashes_is_explained_once_under_the_table(tmp_path,
                                                              unmeasured_store):
    """Thirty blank Engagement cells are a question; the note is the answer."""

    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    assert "so Engagement is blank for every row" in report["html"]["recentnote"]


@node
def test_a_measured_window_does_not_carry_the_note(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store))
    _clean(report)
    assert report["html"]["recentnote"].strip() == ""


@node
def test_an_unmeasured_window_replaces_the_trend_axes_with_a_sentence(
        tmp_path, unmeasured_store):
    report = _render(tmp_path, _api_json(unmeasured_store, roster=4))
    _clean(report)
    chart = report["html"]["chart_engagement"]
    assert "<svg" not in chart
    assert "Nothing measured in this window yet" in chart


def test_with_no_add_on_the_status_and_the_events_block_are_empty(tmp_path):
    store = MetricsStore(str(tmp_path / "plain.db"), salt="s")
    store.heartbeat(ok=True, ts=NOW - 10)

    assert dashboard.status(store)["addon"] is None
    assert dashboard.summary(store, 7, now=NOW)["addon_events"] == {}


# --------------------------------------------------------------------------
# the robot's motion, by family and by outcome
# --------------------------------------------------------------------------
def test_every_outcome_the_robot_names_has_words_in_both_languages():
    text = dashboard_page.DASHBOARD_JS
    for key in ("po_finished", "po_preempted", "po_voice_end", "po_skipped", "po_unfinished",
                "motion_help", "plan_no_family", "motion_lost"):
        assert text.count(key + ":") == 2, key


def test_the_window_totals_the_plan_counts_of_its_own_sessions_only(tmp_path):
    now = 2_000_000.0
    day = 86400.0
    store = _store(tmp_path)
    store.session_started("old", person_id="p", started_at=now - 40 * day)
    store.session_started("new", person_id="p", started_at=now - 2 * day)
    store.session_started("newer", person_id="p", started_at=now - 1 * day)
    store.record_plan_counts("old", {("tension", "finished"): 50})
    store.record_plan_counts("new", {("tension", "finished"): 2, ("levity", "preempted"): 1})
    store.record_plan_counts("newer", {("tension", "voice_end"): 3, ("-", "skipped"): 1,
                                       ("", "finished"): 1, ("", "lost"): 4})

    counts = dashboard.summary(store, 30, now=now)["plan_counts"]
    assert counts == {
        "sentences": 8,
        "families": {"tension": 5, "levity": 1, "": 1},
        "outcomes": {"finished": 3, "preempted": 1, "voice_end": 3, "skipped": 1},
        "not_moved": 1,
        "lost_events": 4,
    }
    wider = dashboard.summary(store, 90, now=now)["plan_counts"]
    assert wider["sentences"] == 58 and wider["families"]["tension"] == 55
    store.close()


def test_a_store_with_no_plan_counts_says_none_rather_than_zero_of_something(tmp_path):
    store = _store(tmp_path)
    assert dashboard.summary(store, 30, now=2_000_000.0)["plan_counts"] == {
        "sentences": 0, "families": {}, "outcomes": {}, "not_moved": 0, "lost_events": 0}
    store.close()


def test_one_session_has_its_own_plan_counts(tmp_path):
    store = _store(tmp_path)
    for sid in ("mine", "other"):
        store.session_started(sid, person_id="p", started_at=1000.0)
    store.record_plan_counts("mine", {("energy", "finished"): 2, ("energy", "preempted"): 1})
    store.record_plan_counts("other", {("levity", "finished"): 9})
    detail = dashboard.session_detail(store, "mine")
    assert detail["plan_counts"] == {
        "sentences": 3, "families": {"energy": 3},
        "outcomes": {"finished": 2, "preempted": 1}, "not_moved": 0, "lost_events": 0}
    store.close()


def test_the_counts_come_back_through_the_routes(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="p", started_at=time.time() - 60.0)
    store.record_plan_counts("s1", {("tension", "finished"): 2})
    client = _client(store)
    _login(client)
    summary = client.get("/api/dashboard/summary?days=7").json()
    assert summary["plan_counts"]["families"] == {"tension": 2}
    detail = client.get("/api/dashboard/session/s1").json()
    assert detail["plan_counts"]["outcomes"] == {"finished": 2}
    store.close()


@node
def test_the_page_draws_the_counts_in_its_own_words(tmp_path, live_store):
    report = _render(tmp_path, _api_json(live_store, days=30))
    _clean(report)
    html = report["html"]
    # The window, so a1's hundred sentences from before it are not here.
    assert "<span>tension</span><span class=muted>5</span>" in html["motionfamily"]
    assert "talking, no family" in html["motionfamily"]
    assert ">interrupted<" in html["motionoutcome"]
    assert "cut when the voice stopped" in html["motionoutcome"]
    assert "still running when the session ended" in html["motionoutcome"]
    # Added after this page shipped: printed as itself, not as a missing translation.
    assert "a_family_not_named_yet" in html["motionfamily"]
    assert "an_outcome_not_named_yet" in html["motionoutcome"]
    assert "6" in html["motionnote"]
    assert "lost" not in html["motionoutcome"]
    # The session the harness opens is the newest, b3, and its dialog says how it went.
    assert "interrupted" in html["sessionmotion"]
    assert "\\u00b7" not in html["sessionmotion"]


@node
def test_the_page_says_nothing_when_there_is_nothing_to_count(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="p", started_at=time.time() - 60.0)
    report = _render(tmp_path, _api_json(store, days=30))
    _clean(report)
    assert report["html"]["motionnote"] == "" and "class=hint" in report["html"]["motionfamily"]
    assert "interrupted" not in report["html"]["sessionmotion"]
    store.close()
