"""The consultancy (v2) dashboard routes: compare, heatmap, modules, funnel, CSV.

These are the numbers managers report, so the tests here are about
three things and nothing else:

* **the arithmetic**, against a two-period fixture whose every count is written
  out by hand above the assertions, including the SIGN of each delta;
* **sample-size honesty** — no evidence must be ``null`` and never 0, in both
  period blocks and in every delta derived from them;
* **the cost**, because all of this runs on ~0.9 of a free CM4 core: every SQL
  statement the new routes issue is EXPLAIN QUERY PLAN'd, and none of them may
  scan a table (``cycles`` above all, the only one that grows without bound).

The privacy promise is tested at the end across the new surface too — a CSV
export is the easiest way in the whole system to walk off with a spreadsheet of
staff, so it gets the same "no name, no e-mail, no transcript" check every JSON
route already has.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time

import pytest

from emori_adapter.hostapp import dashboard
from emori_adapter.hostapp.metrics_store import MetricsStore, person_code
from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel

PASSWORD = "site-secret"
SALT = "unit-test-salt"
NOW = 1785000000.0  # 2026-07-25 17:20:00 UTC, a Saturday. Fixed: no midnight drift.
DAY = 86400.0

# Real-looking identity that must never surface in any response or CSV cell.
REAL_ID = "anna@example.org"
SECRET_ANALYSIS = "Anna hesitated when the relative asked about the prognosis."


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(str(tmp_path / "dash.db"), salt=SALT)


def _app(store, *, password=PASSWORD, roster_size=None):
    from fastapi import FastAPI

    app = FastAPI()
    dashboard.mount(app, store=store, password=password, salt=SALT,
                    roster_size=roster_size)
    return app


def _client(store, **kw):
    from starlette.testclient import TestClient

    client = TestClient(_app(store, **kw))
    client.post("/dashboard/login", data={"password": PASSWORD})
    return client


def _signals(attention=0.7):
    """Always ENGAGED, so `record_cycle` emits no recovery events of its own and
    the event counts below are exactly what the fixture seeds."""

    return DerivedSignals(
        timestamp="2026-07-25T17:20:00Z", session_id="s", person_id="p", seq=1,
        engagement_score=attention, engagement_level=EngagementLevel.ENGAGED,
        valence=0.2, arousal=0.1, expression="neutral", expression_confidence=0.4,
        top_styles=[("mirroring", 0.3)],
        context=ContextSignal(engagement_trend=0.0, escalation=0.1, rapport=0.6,
                              attention=attention, dominant_style="mirroring"),
    )


def _session(store, sid, *, person, at, lang="ca", module_id="m1", module="Module 1",
             status="completed", duration=None, engagement=None, score=None,
             attentions=(), analysis=None):
    store.session_started(sid, person_id=person, lang=lang, module_id=module_id,
                          module_title=module, started_at=at)
    for i, attention in enumerate(attentions):
        store.record_cycle(sid, _signals(attention), ts=at + i * 2)
    results = None
    if score is not None or analysis is not None:
        results = {}
        if score is not None:
            results["performance_scores"] = {c: score for c in dashboard.COMPETENCIES}
        if analysis is not None:
            results["session_analysis"] = analysis
    store.session_finished(sid, ended_at=at + (duration or 60), status=status,
                           duration_s=duration, avg_engagement=engagement,
                           results=results)


# --------------------------------------------------------------------------
# a two-period fixture with arithmetic written out by hand
# --------------------------------------------------------------------------
def _seed_two_periods(store, now: float):
    """The fixture body, anchored at whatever ``now`` the caller measures from."""

    a_at, b_at = now - 10 * DAY, now - 3 * DAY

    _session(store, "a1", person=REAL_ID, at=a_at, lang="ca", status="completed",
             duration=300, engagement=0.5, score=40, attentions=(0.5, 0.5, 0.7, 0.7),
             analysis=SECRET_ANALYSIS)
    _session(store, "a2", person="bob-002", at=a_at + 60, lang="en", module_id="m2",
             module="Module 2", status="abandoned", duration=100)
    for i in range(2):
        store.log_event("attention_drop", session_id="a1", ts=a_at + 10 + i)
    store.log_event("recovery_success", session_id="a1", ts=a_at + 20)
    store.log_event("ext_mode_a", session_id="", detail="mode_a", ts=a_at + 30)

    _session(store, "b1", person=REAL_ID, at=b_at, lang="ca", status="completed",
             duration=600, engagement=0.8, score=70, attentions=(0.7, 0.7, 0.7, 0.9))
    _session(store, "b2", person="bob-002", at=b_at + 60, lang="en", module_id="m2",
             module="Module 2", status="completed", duration=400, engagement=0.6,
             score=50)
    _session(store, "b3", person="cara-003", at=b_at + 120, lang="en",
             status="abandoned", duration=200)
    for i in range(2):
        store.log_event("attention_drop", session_id="b1", ts=b_at + 10 + i)
    for i in range(2):
        store.log_event("recovery_success", session_id="b1", ts=b_at + 20 + i)
    store.log_event("ext_mode_a", session_id="", detail="mode_a", ts=b_at + 30)
    store.log_event("ext_mode_b", session_id="", detail="mode_b",
                    ts=b_at + 31)
    return store


@pytest.fixture()
def live_two_periods(tmp_path):
    """The same shape, seeded relative to the REAL clock.

    The HTTP handlers deliberately take no ``now`` (a route must not accept a
    caller-supplied clock), so the tests that go through them cannot use the
    frozen NOW — a fixture pinned to a date in the past silently empties every
    window as the calendar moves.
    """

    return _seed_two_periods(_store(tmp_path), time.time())


@pytest.fixture()
def two_periods(tmp_path):
    """days=7 -> B = [NOW-7d, NOW), A = [NOW-14d, NOW-7d).

    period A (10 days ago)          period B (3 days ago)
    ----------------------------    ----------------------------------------
    2 sessions, 2 badge staff       3 sessions, 3 badge staff
    1 completed / 1 abandoned       2 completed / 1 abandoned
      -> completion 50.0%             -> completion 66.7%
    1 scored, all six at 40         2 scored, all six at 70 and 50 -> 60
    cycles 0.5,0.5,0.7,0.7          cycles 0.7,0.7,0.7,0.9
      -> attention 50.0%              -> attention 100.0%
    2 drops / 1 recovery -> 50%     2 drops / 2 recoveries -> 100%
    durations 300 + 100 -> 200 s    durations 600 + 400 + 200 -> 400 s
    langs ca 1 / en 1               langs ca 1 / en 2
    """

    return _seed_two_periods(_store(tmp_path), NOW)


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------
def test_compare_measures_both_halves_the_same_way(two_periods):
    out = dashboard.compare(two_periods, days=7, now=NOW)
    a, b = out["a"], out["b"]

    assert out["mode"] == "days" and out["days"] == 7 and out["equal_spans"] is True
    assert a["start"] == NOW - 14 * DAY and a["end"] == NOW - 7 * DAY
    assert b["start"] == NOW - 7 * DAY and b["end"] == NOW

    assert (a["n_sessions"], b["n_sessions"]) == (2, 3)
    assert (a["unique_staff"], b["unique_staff"]) == (2, 3)
    assert (a["completed"], b["completed"]) == (1, 2)
    assert (a["abandoned"], b["abandoned"]) == (1, 1)
    assert a["completion_pct"] == 50.0
    assert b["completion_pct"] == pytest.approx(66.7, abs=0.1)
    assert (a["n_scored_sessions"], b["n_scored_sessions"]) == (1, 2)
    assert a["competency_mean"] == 40.0 and b["competency_mean"] == 60.0
    assert a["competencies"]["adaptability"] == 40.0
    assert b["competencies"]["adaptability"] == 60.0
    assert (a["n_cycles"], b["n_cycles"]) == (4, 4)
    assert a["attention_pct"] == 50.0 and b["attention_pct"] == 100.0
    assert a["recovery_pct"] == 50.0 and b["recovery_pct"] == 100.0
    assert a["avg_duration_s"] == 200.0 and b["avg_duration_s"] == 400.0
    assert a["lang_split"] == {"ca": 1, "en": 1}
    assert b["lang_split"] == {"en": 2, "ca": 1}
    assert a["addon_events"] == {"mode_a": 1}
    assert b["addon_events"] == {"mode_a": 1, "mode_b": 1}


def test_compare_deltas_have_the_right_sign_and_magnitude(two_periods):
    out = dashboard.compare(two_periods, days=7, now=NOW)
    delta, change = out["delta"], out["pct_change"]

    # B - A, never A - B: a positive delta must mean "better than before".
    assert delta["n_sessions"] == 1
    assert delta["unique_staff"] == 1
    assert delta["completion_pct"] == pytest.approx(16.7, abs=0.1)
    assert delta["competency_mean"] == pytest.approx(20.0)
    assert delta["attention_pct"] == pytest.approx(50.0)
    assert delta["recovery_pct"] == pytest.approx(50.0)
    assert delta["avg_duration_s"] == pytest.approx(200.0)
    assert delta["competencies"]["reasoning_quality"] == pytest.approx(20.0)

    assert change["n_sessions"] == pytest.approx(50.0)          # 2 -> 3
    assert change["competency_mean"] == pytest.approx(50.0)     # 40 -> 60
    assert change["attention_pct"] == pytest.approx(100.0)      # 50 -> 100


def test_compare_deltas_are_the_other_way_round_when_the_periods_are(two_periods):
    """A sanity check the sign convention cannot pass by accident."""

    swapped = dashboard.compare(
        two_periods,
        a_start="2026-07-22", a_end="2026-07-22",   # the BETTER week, as A
        b_start="2026-07-15", b_end="2026-07-15",   # the WORSE one, as B
        now=NOW)
    assert swapped["mode"] == "explicit"
    assert swapped["delta"]["competency_mean"] == pytest.approx(-20.0)
    assert swapped["delta"]["n_sessions"] == -1          # 3 sessions -> 2
    assert swapped["pct_change"]["competency_mean"] == pytest.approx(-33.3, abs=0.1)


def test_compare_never_fabricates_evidence_it_does_not_have(tmp_path):
    """A period with no sessions is null, not zero, everywhere it matters."""

    out = dashboard.compare(_store(tmp_path), days=7, now=NOW)
    for block in (out["a"], out["b"]):
        assert block["n_sessions"] == 0            # a COUNT is honestly zero
        assert block["completion_pct"] is None     # ...a RATE is not
        assert block["attention_pct"] is None
        assert block["recovery_pct"] is None
        assert block["avg_duration_s"] is None
        assert block["competency_mean"] is None
        assert all(v is None for v in block["competencies"].values())
    assert out["delta"]["completion_pct"] is None
    assert out["delta"]["competency_mean"] is None
    assert out["delta"]["competencies"]["adaptability"] is None
    assert out["delta"]["n_sessions"] == 0
    # 0 -> 0 is not "+0%", it is undefined.
    assert out["pct_change"]["n_sessions"] is None


def test_a_missing_measurement_on_one_side_is_not_an_improvement(tmp_path):
    """null -> 62 must not read as "+62": that is a measurement appearing, not
    a learner improving."""

    store = _store(tmp_path)
    _session(store, "b1", person="anna-1", at=NOW - 3 * DAY, score=62)
    out = dashboard.compare(store, days=7, now=NOW)
    assert out["a"]["competency_mean"] is None
    assert out["b"]["competency_mean"] == 62.0
    assert out["delta"]["competency_mean"] is None
    assert out["pct_change"]["competency_mean"] is None


def test_compare_carries_the_n_behind_every_rate(two_periods):
    """"100% recovery" over two drops and over two hundred are different claims."""

    out = dashboard.compare(two_periods, days=7, now=NOW)
    for block in (out["a"], out["b"]):
        for key in ("n_sessions", "n_scored_sessions", "n_cycles", "n_finished",
                    "n_attention_drops", "n_recoveries", "n_duration",
                    "n_heartbeats"):
            assert isinstance(block[key], int), key
    assert out["b"]["n_attention_drops"] == 2 and out["b"]["n_recoveries"] == 2


def test_explicit_iso_ranges_are_inclusive_of_the_end_day(two_periods):
    """"1 - 7 July" includes the 7th; every window inside is half-open."""

    out = dashboard.compare(two_periods,
                            a_start="2026-07-08", a_end="2026-07-16",
                            b_start="2026-07-17", b_end="2026-07-25", now=NOW)
    assert out["mode"] == "explicit"
    assert out["a"]["n_sessions"] == 2 and out["b"]["n_sessions"] == 3
    assert out["span_a_days"] == 9.0 and out["equal_spans"] is True
    # The end date's own day is inside the window: the A sessions are on the 15th.
    tight = dashboard.compare(two_periods, a_start="2026-07-15", a_end="2026-07-15",
                              b_start="2026-07-22", b_end="2026-07-22", now=NOW)
    assert tight["a"]["n_sessions"] == 2 and tight["b"]["n_sessions"] == 3


def test_unequal_explicit_spans_are_flagged_not_refused(two_periods):
    out = dashboard.compare(two_periods, a_start="2026-07-01", a_end="2026-07-16",
                            b_start="2026-07-20", b_end="2026-07-25", now=NOW)
    assert out["equal_spans"] is False
    assert out["span_a_days"] == 16.0 and out["span_b_days"] == 6.0


@pytest.mark.parametrize("kwargs", [
    {"a_start": "2026-07-01"},                                   # partial
    {"a_start": "2026-07-01", "a_end": "2026-07-07",
     "b_start": "2026-07-08"},                                   # still partial
    {"a_start": "not-a-date", "a_end": "2026-07-07",
     "b_start": "2026-07-08", "b_end": "2026-07-14"},            # unparseable
    {"a_start": "2026-07-07", "a_end": "2026-07-01",
     "b_start": "2026-07-08", "b_end": "2026-07-14"},            # reversed
])
def test_an_unusable_explicit_range_is_refused_not_silently_replaced(tmp_path, kwargs):
    """Answering about a different period than the one asked for is how a
    before/after chart lies, so this is the one place the API refuses."""

    with pytest.raises(ValueError):
        dashboard.compare(_store(tmp_path), now=NOW, **kwargs)


def test_compare_span_is_bounded_but_not_a_closed_set():
    assert dashboard.clamp_span_days(21) == 21          # a 3-week pilot
    assert dashboard.clamp_span_days(0) == 1
    assert dashboard.clamp_span_days(-5) == 1
    assert dashboard.clamp_span_days(10 ** 6) == dashboard.COMPARE_MAX_DAYS
    assert dashboard.clamp_span_days("junk") == dashboard.COMPARE_DEFAULT_DAYS
    assert dashboard.clamp_span_days(None) == dashboard.COMPARE_DEFAULT_DAYS


def test_uptime_in_a_past_period_is_not_divided_by_time_that_has_not_happened(tmp_path,
                                                                              monkeypatch):
    """Period A ends in the past: its denominator must end there too."""

    monkeypatch.setenv("EMORI_HEARTBEAT_S", "60")
    store = _store(tmp_path)
    # Perfect uptime through period A (a beat a minute for its whole 7 days),
    # then nothing at all in period B.
    start = NOW - 14 * DAY
    for i in range(7 * 24 * 60):
        store.heartbeat(ok=True, ts=start + i * 60)
    out = dashboard.compare(store, days=7, now=NOW)
    assert out["a"]["uptime_pct"] == pytest.approx(100.0, abs=1.0)
    assert out["b"]["uptime_pct"] == 0.0        # measured silence IS zero uptime
    assert out["delta"]["uptime_pct"] == pytest.approx(-100.0, abs=1.0)


def test_summary_still_matches_the_helper_it_was_factored_into(two_periods):
    """summary() is now a projection of window_metrics(); the numbers must agree."""

    window = dashboard.window_metrics(two_periods, NOW - 7 * DAY, None, now=NOW)
    s = dashboard.summary(two_periods, 7, now=NOW)
    assert s["sessions"] == window["n_sessions"] == 3
    assert s["cycles"] == window["n_cycles"]
    assert s["competency"] == window["competencies"]
    assert s["attention_pct"] == window["attention_pct"]
    assert s["recovery_pct"] == window["recovery_pct"]
    assert s["attention_drops"] == window["n_attention_drops"]


# --------------------------------------------------------------------------
# heatmap
# --------------------------------------------------------------------------
def test_heatmap_buckets_by_weekday_and_hour(tmp_path):
    store = _store(tmp_path)
    # 2026-07-22 is a Wednesday (weekday 2 with Monday=0); 17:20 UTC.
    _session(store, "w1", person="anna-1", at=NOW - 3 * DAY)
    _session(store, "w2", person="bob-2", at=NOW - 3 * DAY + 300)   # same hour
    _session(store, "w3", person="cara-3", at=NOW - 3 * DAY + 7200)  # +2 h
    _session(store, "w4", person="dan-4", at=NOW - 1 * DAY)          # Friday

    out = dashboard.heatmap(store, 7, now=NOW, localtime=time.gmtime)
    assert out["weekday_base"] == "monday"
    assert len(out["grid"]) == 7 and all(len(row) == 24 for row in out["grid"])
    assert out["grid"][2][17] == 2      # Wednesday 17:00
    assert out["grid"][2][19] == 1      # Wednesday 19:00
    assert out["grid"][4][17] == 1      # Friday 17:00
    assert out["total"] == 4 and out["max"] == 2
    assert out["weekday_totals"] == [0, 0, 3, 0, 1, 0, 0]
    assert sum(out["hour_totals"]) == 4
    assert out["utc_offset_s"] == 0     # gmtime == UTC, by construction


def test_heatmap_is_robot_local_not_utc(tmp_path):
    """"Nobody uses it before 10:00" is a claim about the site's clock."""

    store = _store(tmp_path)
    _session(store, "w1", person="anna-1", at=NOW - 3 * DAY)     # 17:20 UTC

    def _plus_two_hours(stamp):
        return time.gmtime(stamp + 7200)

    out = dashboard.heatmap(store, 7, now=NOW, localtime=_plus_two_hours)
    assert out["grid"][2][19] == 1, "the local-time conversion was not applied"
    assert out["utc_offset_s"] == 7200
    assert dashboard.heatmap(store, 7, now=NOW,
                             localtime=time.gmtime)["grid"][2][17] == 1


def test_heatmap_of_an_empty_store_is_a_dense_zero_grid(tmp_path):
    out = dashboard.heatmap(_store(tmp_path), 7, now=NOW, localtime=time.gmtime)
    assert out["total"] == 0 and out["max"] == 0
    assert sum(sum(row) for row in out["grid"]) == 0
    assert len(out["grid"]) == 7 and len(out["grid"][0]) == 24


# --------------------------------------------------------------------------
# modules ("scenario difficulty")
# --------------------------------------------------------------------------
def test_modules_rank_by_use_and_report_outcome_per_scenario(two_periods):
    out = dashboard.modules(two_periods, 30, now=NOW)
    rows = {row["module_id"]: row for row in out["modules"]}

    assert [row["module_id"] for row in out["modules"]] == ["m1", "m2"]  # n desc
    m1 = rows["m1"]
    assert m1["module_title"] == "Module 1"
    assert m1["n_sessions"] == 3            # a1, b1, b3
    assert (m1["completed"], m1["abandoned"]) == (2, 1)
    assert m1["completion_pct"] == pytest.approx(66.7, abs=0.1)
    assert m1["n_scored"] == 2 and m1["competency_mean"] == pytest.approx(55.0)
    # mean of the per-session avg_engagement (0.5 and 0.8), NOT a cycle mean:
    # a 30-minute session must not outvote a 5-minute one.
    assert m1["n_engagement"] == 2 and m1["avg_engagement"] == pytest.approx(0.65)

    m2 = rows["m2"]
    assert m2["n_sessions"] == 2 and m2["completion_pct"] == 50.0
    assert m2["n_scored"] == 1 and m2["competency_mean"] == pytest.approx(50.0)


def test_a_module_people_walk_out_of_is_visible_even_at_100_percent_completion(tmp_path):
    """Two scenarios, both 100% "completed", and only one of them is going well.

    A robot-driven ending that came back with an analysis is stored 'completed',
    so completion_pct cannot separate these two rows at all -- which is exactly
    the question this table exists to answer for a course designer.
    """

    store = _store(tmp_path)
    for i, reason in enumerate(["walkaway_ends", "presence_ends", "walkaway_ends"]):
        _session(store, "hard%d" % i, person="anna-%d" % i, at=NOW - DAY,
                 module_id="hard", module="Hard one", analysis="ok")
        store.session_finished("hard%d" % i, ended_at=NOW - DAY + 60,
                               status="completed", end_reason=reason)
    for i in range(3):
        _session(store, "easy%d" % i, person="bob-%d" % i, at=NOW - DAY,
                 module_id="easy", module="Easy one", analysis="ok")

    rows = {row["module_id"]: row for row in dashboard.modules(store, 7, now=NOW)["modules"]}

    assert rows["hard"]["completion_pct"] == rows["easy"]["completion_pct"] == 100.0
    assert (rows["hard"]["robot_ended"], rows["easy"]["robot_ended"]) == (3, 0)


def test_robot_endings_are_comparable_across_two_periods(two_periods):
    """The before/after question: did the walk-aways go up after the change?"""

    two_periods.session_finished("b3", ended_at=NOW - 3 * DAY + 60, status="abandoned",
                                 end_reason="walkaway_ends")
    out = dashboard.compare(two_periods, days=7, now=NOW)

    assert (out["a"]["robot_ended"], out["b"]["robot_ended"]) == (0, 1)
    assert out["delta"]["robot_ended"] == 1
    assert "robot_ended" in dashboard.COMPARE_METRICS


def test_a_module_nobody_scored_reports_null_not_zero(tmp_path):
    store = _store(tmp_path)
    _session(store, "s1", person="anna-1", at=NOW - DAY, module_id="hard",
             module="Hard one", status="abandoned")
    row = dashboard.modules(store, 7, now=NOW)["modules"][0]
    assert row["n_sessions"] == 1 and row["n_scored"] == 0
    assert row["competency_mean"] is None
    assert row["avg_engagement"] is None
    assert row["completion_pct"] == 0.0, "an abandoned session IS a 0% completion"


def test_modules_fall_back_to_the_spoken_topic_when_there_is_no_title(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-1", module_id="", module_title="",
                          topic="Difficult conversations", started_at=NOW - DAY)
    row = dashboard.modules(store, 7, now=NOW)["modules"][0]
    assert row["module_title"] == "Difficult conversations"


# --------------------------------------------------------------------------
# funnel (participation vs the roster)
# --------------------------------------------------------------------------
def test_funnel_participation_is_active_badge_staff_over_the_roster(tmp_path):
    store = _store(tmp_path)
    # This month: 3 distinct badge staff (+ a walk-up that must NOT count).
    for i, person in enumerate(("anna-1", "bob-2", "cara-3")):
        _session(store, "m-%d" % i, person=person, at=NOW - DAY)
    _session(store, "m-walk", person="robot-lobby-3", at=NOW - DAY)
    # ...and the same person twice more, which is still one participating human.
    _session(store, "m-again", person="anna-1", at=NOW - 2 * DAY)

    out = dashboard.funnel(store, 6, roster_size=10, now=NOW)
    assert out["roster_size"] == 10 and out["target_pct"] == 70.0
    assert len(out["funnel"]) == 6 and out["month_list"][-1] == "2026-07"
    this_month = out["funnel"][-1]
    assert this_month["active_staff"] == 3
    assert this_month["n_sessions"] == 5      # the walk-up IS a session
    assert this_month["participation_pct"] == 30.0
    # A month with nothing in it is a real 0 participants out of 10, not null.
    assert out["funnel"][0]["active_staff"] == 0
    assert out["funnel"][0]["participation_pct"] == 0.0
    assert out["funnel"][0]["completion_pct"] is None   # ...but no completion rate


def test_funnel_participation_is_null_without_a_roster(tmp_path):
    store = _store(tmp_path)
    _session(store, "s1", person="anna-1", at=NOW - DAY)
    for size in (None, 0, -3, "junk"):
        out = dashboard.funnel(store, 3, roster_size=size, now=NOW)
        assert out["roster_size"] is None, size
        assert all(row["participation_pct"] is None for row in out["funnel"]), size
        assert out["funnel"][-1]["active_staff"] == 1, "the count is still reported"


def test_funnel_accepts_a_callable_roster_size(tmp_path):
    """server.py passes a callable so a reloaded roster needs no restart."""

    store = _store(tmp_path)
    _session(store, "s1", person="anna-1", at=NOW - DAY)
    size = [4]
    out = dashboard.funnel(store, 1, roster_size=lambda: size[0], now=NOW)
    assert out["funnel"][-1]["participation_pct"] == 25.0
    size[0] = 2
    out = dashboard.funnel(store, 1, roster_size=lambda: size[0], now=NOW)
    assert out["funnel"][-1]["participation_pct"] == 50.0


def test_funnel_months_are_dense_utc_calendar_months(tmp_path):
    store = _store(tmp_path)
    _session(store, "old", person="anna-1", at=NOW - 45 * DAY)   # 2026-06-10
    _session(store, "new", person="bob-2", at=NOW - DAY)         # 2026-07-24

    out = dashboard.funnel(store, 6, roster_size=2, now=NOW)
    assert out["month_list"] == ["2026-02", "2026-03", "2026-04", "2026-05",
                                 "2026-06", "2026-07"]
    by_month = {row["month"]: row for row in out["funnel"]}
    assert by_month["2026-06"]["n_sessions"] == 1
    assert by_month["2026-07"]["n_sessions"] == 1
    assert by_month["2026-05"]["n_sessions"] == 0
    # The window starts at the first day of the OLDEST month, not months*30 days.
    assert out["since"] == pytest.approx(
        time.mktime((2026, 2, 1, 0, 0, 0, 0, 1, 0)) - time.timezone, abs=1.0)


def test_funnel_month_span_is_bounded():
    assert dashboard.clamp_months(6) == 6
    assert dashboard.clamp_months(0) == 1
    assert dashboard.clamp_months(999) == dashboard.FUNNEL_MAX_MONTHS
    assert dashboard.clamp_months("junk") == dashboard.FUNNEL_DEFAULT_MONTHS


def test_funnel_crossing_a_year_boundary(tmp_path):
    out = dashboard.funnel(_store(tmp_path), 4, now=1767225600.0)  # 2026-01-01
    assert out["month_list"] == ["2025-10", "2025-11", "2025-12", "2026-01"]


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


def test_export_has_one_row_per_session_and_the_documented_columns(two_periods):
    rows = _parse_csv(dashboard.export_csv(two_periods, 30, now=NOW))
    assert rows[0] == list(dashboard.EXPORT_COLUMNS)
    assert rows[0][:9] == ["session_id", "started_at", "person_code", "kind", "lang",
                           "module", "duration_s", "avg_engagement", "status"]
    # end_reason is appended AFTER the six competencies so the score columns a
    # review workbook addresses by position never move.
    assert rows[0][9:] == list(dashboard.COMPETENCIES) + ["end_reason"]
    assert len(rows) == 1 + 5           # header + the fixture's five sessions

    by_id = {row[0]: row for row in rows[1:]}
    b1 = by_id["b1"]
    assert b1[1] == "2026-07-22T17:20:00Z"      # ISO, UTC, explicit Z
    assert b1[2] == person_code(REAL_ID, SALT)[0] and b1[2].startswith("S-")
    assert b1[3] == "badge" and b1[4] == "ca" and b1[5] == "Module 1"
    assert b1[6] == "600.0" and b1[8] == "completed"
    assert b1[9:15] == ["70.0"] * 6
    assert b1[15] == ""                 # nobody but the robot fills end_reason

    # A session nobody scored leaves the competency cells EMPTY, not 0.
    b3 = by_id["b3"]
    assert b3[8] == "abandoned"
    assert b3[9:15] == [""] * 6


def test_export_leaks_no_pii(two_periods):
    text = dashboard.export_csv(two_periods, 90, now=NOW)
    for forbidden in (REAL_ID, SECRET_ANALYSIS, "example.org", "bob-002",
                      "cara-003", "analysis"):
        assert forbidden not in text, forbidden


def test_export_neutralises_spreadsheet_formulas(tmp_path):
    """The file is opened by double-click in Excel; a leading = is executable."""

    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-1",
                          module_title="=HYPERLINK(\"http://evil\",\"x\")",
                          started_at=NOW - DAY)
    store.session_finished("s1", ended_at=NOW - DAY + 60, status="completed")
    rows = _parse_csv(dashboard.export_csv(store, 7, now=NOW))
    assert rows[1][5].startswith("'="), rows[1][5]


def test_export_row_count_is_bounded(tmp_path):
    store = _store(tmp_path)
    rows = dashboard.export_rows(store, 7, now=NOW, limit=10 ** 9)
    assert rows == [list(dashboard.EXPORT_COLUMNS)]
    assert dashboard.export_filename(7, now=NOW) == "emori-sessions-7d-20260725.csv"


# --------------------------------------------------------------------------
# persons: the radar map and most_improved
# --------------------------------------------------------------------------
def _improving(store, person, scores, *, at=NOW):
    for i, score in enumerate(scores):
        _session(store, "%s-%d" % (person, i), person=person,
                 at=at - (len(scores) - i) * 3600, score=score, attentions=(0.7,))


def test_each_person_carries_a_competency_map_for_the_radar(two_periods):
    rows = dashboard.persons(two_periods, 30, now=NOW)["persons"]
    anna = {row["person_code"]: row for row in rows}[person_code(REAL_ID, SALT)[0]]
    assert anna["scored_sessions"] == 2
    assert set(anna["competencies"]) == set(dashboard.COMPETENCIES)
    # 40 in period A, 70 in period B -> 55 on every axis.
    assert all(v == pytest.approx(55.0) for v in anna["competencies"].values())


def test_a_competency_nobody_scored_is_null_on_the_radar(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-1", started_at=NOW - DAY)
    store.record_cycle("s1", _signals(), ts=NOW - DAY)
    store.session_finished("s1", ended_at=NOW - DAY + 60, status="completed",
                           results={"performance_scores": {"adaptability": 80}})
    row = dashboard.persons(store, 7, now=NOW)["persons"][0]
    assert row["competencies"]["adaptability"] == 80.0
    assert row["competencies"]["reasoning_quality"] is None


def test_most_improved_ranks_positive_competency_slopes(tmp_path):
    store = _store(tmp_path)
    _improving(store, "anna-1", (40, 55, 70))     # slope +15
    _improving(store, "bob-2", (50, 55, 60))      # slope +5
    _improving(store, "cara-3", (80, 60, 40))     # declining
    out = dashboard.persons(store, 7, now=NOW)

    board = out["most_improved"]
    assert [row["person_code"] for row in board] == \
        [person_code("anna-1", SALT)[0], person_code("bob-2", SALT)[0]]
    assert board[0]["competency_slope"] == pytest.approx(15.0)
    assert board[0]["scored_sessions"] == 3
    assert board[0]["avg_competency"] == pytest.approx(55.0)
    assert len(board) <= 3


def test_most_improved_needs_three_scored_sessions_and_never_the_walkup_bucket(tmp_path):
    store = _store(tmp_path)
    _improving(store, "anna-1", (40, 90))                 # only two -> not eligible
    _improving(store, "robot-lobby-3", (30, 50, 70))       # the walk-up crowd
    out = dashboard.persons(store, 7, now=NOW)

    assert out["most_improved"] == []
    codes = {row["person_code"] for row in out["persons"]}
    assert "walkup" in codes, "the walk-up rows are still reported, just not ranked"
    two_point = {row["person_code"]: row for row in out["persons"]}[
        person_code("anna-1", SALT)[0]]
    assert two_point["competency_slope"] == pytest.approx(50.0), \
        "the slope is still reported; only the ranking demands three points"


def test_competency_slope_is_null_below_two_scored_sessions(tmp_path):
    store = _store(tmp_path)
    _improving(store, "anna-1", (60,))
    row = dashboard.persons(store, 7, now=NOW)["persons"][0]
    assert row["scored_sessions"] == 1
    assert row["competency_slope"] is None, "0.0 would read as 'measured, flat'"


# --------------------------------------------------------------------------
# cost: every new statement, EXPLAIN'd
# --------------------------------------------------------------------------
class _SpyStore:
    """Records the SQL a route issues, then answers it for real."""

    def __init__(self, store):
        self._store = store
        self.sql: list[tuple[str, tuple]] = []

    @property
    def enabled(self) -> bool:
        return True

    def query(self, sql, params=()):
        self.sql.append((sql, params))
        return self._store.query(sql, params)


def _plans(store, spy, tmp_path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(tmp_path / "dash.db"))
    try:
        out = []
        for sql, params in spy.sql:
            for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params):
                out.append((sql, str(row[-1])))
        return out
    finally:
        conn.close()


def test_no_new_query_scans_a_table(two_periods, tmp_path):
    """~0.9 of a core is free on the CM4 and a live roleplay owns it.

    `cycles` is the table that grows without bound (~18 k rows a busy day), and
    the ONLY way any route may reach it is through the covering index, joined via
    `sessions` -- a bare scan is a hundred milliseconds of SD-card read on the
    same core the 2-second cycle write is on.
    """

    spy = _SpyStore(two_periods)
    dashboard.compare(spy, days=7, now=NOW)
    dashboard.heatmap(spy, 7, now=NOW, localtime=time.gmtime)
    dashboard.modules(spy, 7, now=NOW)
    dashboard.funnel(spy, 6, roster_size=10, now=NOW)
    dashboard.export_rows(spy, 7, now=NOW)
    assert len(spy.sql) >= 16, "the routes issued nothing to check"

    for sql, plan in _plans(two_periods, spy, tmp_path):
        upper = plan.upper()
        if upper.startswith("SCAN "):
            assert "USING" in upper and "INDEX" in upper, \
                "full table scan: %s\n%s" % (plan, sql[:120])
        if "CYCLES" in upper:
            assert "COVERING INDEX IDX_CYCLES_SESSION_COV" in upper, \
                "cycles reached off the covering index: %s\n%s" % (plan, sql[:120])


def test_the_windowed_session_queries_use_the_started_at_index(two_periods, tmp_path):
    spy = _SpyStore(two_periods)
    dashboard.modules(spy, 7, now=NOW)
    dashboard.heatmap(spy, 7, now=NOW, localtime=time.gmtime)
    dashboard.export_rows(spy, 7, now=NOW)
    plans = [plan for _, plan in _plans(two_periods, spy, tmp_path)]
    assert plans, "nothing was recorded"
    for plan in plans:
        assert "idx_sessions_started" in plan, plan


def test_the_compare_cycle_aggregate_stays_on_the_covering_index(two_periods, tmp_path):
    spy = _SpyStore(two_periods)
    dashboard.window_metrics(spy, NOW - 7 * DAY, NOW, now=NOW)
    joined = [(sql, plan) for sql, plan in _plans(two_periods, spy, tmp_path)
              if "FROM cycles" in sql]
    assert joined, "window_metrics stopped reading cycles"
    text = " | ".join(plan for _, plan in joined)
    assert "COVERING INDEX idx_cycles_session_cov" in text, text
    assert "idx_sessions_started" in text, text


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------
V2_ROUTES = ("/api/dashboard/compare?days=7", "/api/dashboard/heatmap?days=7",
             "/api/dashboard/modules?days=7", "/api/dashboard/funnel?months=6",
             "/api/dashboard/export.csv?days=7")


def test_every_v2_route_is_auth_gated(live_two_periods):
    from starlette.testclient import TestClient

    anonymous = TestClient(_app(live_two_periods))
    for path in V2_ROUTES:
        assert anonymous.get(path).status_code == 401, path

    unconfigured = TestClient(_app(live_two_periods, password=""))
    for path in V2_ROUTES:
        assert unconfigured.get(path).status_code == 503, path


def test_every_v2_route_answers_when_signed_in(live_two_periods):
    client = _client(live_two_periods, roster_size=12)
    for path in V2_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path

    compared = client.get("/api/dashboard/compare?days=7").json()
    assert compared["a"]["n_sessions"] == 2 and compared["b"]["n_sessions"] == 3
    assert compared["delta"]["n_sessions"] == 1
    assert client.get("/api/dashboard/funnel?months=6").json()["roster_size"] == 12
    assert client.get("/api/dashboard/modules?days=30").json()["modules"][0][
        "n_sessions"] == 3
    assert client.get("/api/dashboard/heatmap?days=30").json()["total"] == 5


def test_the_compare_route_400s_on_a_broken_range(live_two_periods):
    client = _client(live_two_periods)
    response = client.get("/api/dashboard/compare?a_start=nope&a_end=2026-07-07"
                          "&b_start=2026-07-08&b_end=2026-07-14")
    assert response.status_code == 400
    assert response.json()["error"] == "bad_range"


def test_the_csv_route_is_an_attachment_with_no_pii(live_two_periods):
    client = _client(live_two_periods)
    response = client.get("/api/dashboard/export.csv?days=30")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    for forbidden in (REAL_ID, SECRET_ANALYSIS, "example.org", "bob-002"):
        assert forbidden not in response.text, forbidden
    rows = _parse_csv(response.text)
    assert len(rows) == 6 and rows[0][0] == "session_id"


def test_no_v2_route_leaks_a_name_an_email_or_a_transcript(live_two_periods):
    client = _client(live_two_periods, roster_size=12)
    for path in V2_ROUTES + ("/api/dashboard/persons?days=30",):
        body = client.get(path).text
        for forbidden in (REAL_ID, SECRET_ANALYSIS, "example.org", "bob-002",
                          "cara-003", "robot-lobby-3"):
            assert forbidden not in body, "%s leaked %r" % (path, forbidden)


def test_a_junk_window_does_not_reach_the_database(live_two_periods):
    client = _client(live_two_periods)
    assert client.get("/api/dashboard/heatmap?days=99999").json()["days"] == 90
    assert client.get("/api/dashboard/modules?days=abc").status_code == 422
    assert client.get("/api/dashboard/funnel?months=99999").json()["months"] == \
        dashboard.FUNNEL_MAX_MONTHS


# --------------------------------------------------------------------------
# v3: a session whose conversation never opened is not an abandoned conversation
# --------------------------------------------------------------------------
def _never_opened(store, sid, *, at, person="anna-001"):
    """The SSO minted, the iframe never started one: stored abandoned, and a
    TECHNICAL failure rather than a learner who gave up."""

    store.session_started(sid, person_id=person, started_at=at)
    store.session_finished(sid, ended_at=at + 30, status="abandoned",
                           end_reason="session_start_failed")


def test_a_session_that_never_opened_is_kept_out_of_the_completion_rate(tmp_path):
    """A low completion rate overall can hide a high one among the conversations that
    started.

    When sessions that never opened at all sit in the denominator, the KPI measures
    the iframe, not the conversations.
    """

    store = _store(tmp_path)
    _session(store, "c1", person=REAL_ID, at=NOW - DAY, status="completed",
             duration=300, engagement=0.7)
    _session(store, "c2", person=REAL_ID, at=NOW - DAY + 10, status="abandoned",
             duration=100)
    for i in range(3):
        _never_opened(store, "n%d" % i, at=NOW - DAY + 20 + i)

    window = dashboard.window_metrics(store, NOW - 7 * DAY, None, now=NOW)
    assert window["n_sessions"] == 5
    assert window["not_started"] == 3
    assert window["n_conversations"] == 2
    # 1 completed of (1 completed + 1 abandoned conversation) -- not of four.
    assert window["completion_pct"] == 50.0
    store.close()


def test_the_never_opened_count_travels_with_the_module_and_the_month(tmp_path):
    store = _store(tmp_path)
    _session(store, "c1", person=REAL_ID, at=NOW - DAY, status="completed",
             duration=300, module_id="m1", module="Module 1")
    _never_opened(store, "n1", at=NOW - DAY + 5)

    mods = {m["module_id"]: m for m in
            dashboard.modules(store, days=7, now=NOW)["modules"]}
    assert mods["m1"]["not_started"] == 0 and mods["m1"]["completion_pct"] == 100.0
    # The never-opened row carries no module at all -- that is its whole story.
    assert mods[""]["not_started"] == 1
    assert mods[""]["completion_pct"] is None    # no conversation either way

    month = dashboard.funnel(store, months=1, roster_size=4, now=NOW)["funnel"][-1]
    assert month["not_started"] == 1 and month["completion_pct"] == 100.0
    store.close()


def test_the_compare_block_moves_on_never_opened_like_any_other_count(tmp_path):
    store = _store(tmp_path)
    _never_opened(store, "n1", at=NOW - 40 * DAY)
    for i in range(3):
        _never_opened(store, "b%d" % i, at=NOW - 3 * DAY + i)

    out = dashboard.compare(store, days=30, now=NOW)
    assert out["a"]["not_started"] == 1 and out["b"]["not_started"] == 3
    assert out["delta"]["not_started"] == 2
    assert "not_started" in out["metrics"] and "n_conversations" in out["metrics"]
    store.close()


# --------------------------------------------------------------------------
# v3: the timezone the robot is actually in
# --------------------------------------------------------------------------
def test_the_heatmap_names_the_summer_timezone_when_dst_is_in_force(tmp_path,
                                                                    monkeypatch):
    """A robot in a daylight-saving zone must not caption standard time all through
    the summer, an hour out, with nothing on the page to say so."""

    import time as time_module

    store = _store(tmp_path)
    _session(store, "s1", person=REAL_ID, at=NOW - DAY)
    monkeypatch.setattr(dashboard.time, "tzname", ("STD", "DST"))

    def summer(stamp):
        # One hour ahead of UTC, and saying so (struct_time is not a namedtuple).
        parts = tuple(time_module.gmtime(stamp + 3600))[:8] + (1,)
        return time_module.struct_time(parts)

    out = dashboard.heatmap(store, days=7, now=NOW, localtime=summer)
    assert out["tz"] == "DST"
    # ...and the offset is a whole number of minutes, not 3599 seconds.
    assert out["utc_offset_s"] == 3600
    store.close()


def test_the_heatmap_keeps_the_standard_name_out_of_dst(tmp_path, monkeypatch):
    import time as time_module

    store = _store(tmp_path)
    _session(store, "s1", person=REAL_ID, at=NOW - DAY)
    monkeypatch.setattr(dashboard.time, "tzname", ("STD", "DST"))
    out = dashboard.heatmap(store, days=7, now=NOW, localtime=time_module.gmtime)
    assert out["tz"] == "STD" and out["utc_offset_s"] == 0
    store.close()


# --------------------------------------------------------------------------
# v3: a roster the site outgrew
# --------------------------------------------------------------------------
def test_more_badges_than_the_roster_is_flagged_not_drawn_as_150_percent(tmp_path):
    store = _store(tmp_path)
    for i, person in enumerate(("anna-001", "bob-002", "cara-003")):
        _session(store, "s%d" % i, person=person, at=NOW - DAY + i)

    out = dashboard.funnel(store, months=1, roster_size=2, now=NOW)
    assert out["roster_exceeded"] is True
    # The true number is still reported -- the PAGE clamps the bar, not the API.
    assert out["funnel"][-1]["participation_pct"] == 150.0

    fits = dashboard.funnel(store, months=1, roster_size=4, now=NOW)
    assert fits["roster_exceeded"] is False
    store.close()
