"""The management-facing dashboard: auth, cheap SQL aggregates, one HTML page.

A manager standing in front of the robot with a phone must be able to answer four
questions without SSH and without a cloud account: *is anyone using it, is it
working, is it helping, is it up*.  This module is the read side of
:mod:`emori_adapter.hostapp.metrics_store` — it never writes, never creates a
table, and never touches an add-on's runtime.

Shape and constraints (why it looks like this):

* **Pull-only.**  ~0.9 of one core is free on the CM4 and a live roleplay owns it.
  No websockets, no server-side chart rendering, no background aggregation task:
  every route is one or a few indexed ``SELECT``s, the page draws its own SVG, and
  it refreshes no faster than every 60 s (and not at all while the tab is hidden).
* **Fail closed.**  ``EMORI_DASH_PASSWORD`` unset means the dashboard serves a
  503 "not configured" page and nothing else.  There is no anonymous mode; the
  robot ships with the key unset.
* **No PII, ever.**  Everything returned is either an aggregate or a pseudonymous
  ``S-xxxxxx`` code.  ``sessions.analysis_excerpt`` — the only free text the store
  holds — is deliberately NOT exposed by any route here.
* **Decision support only.**  The per-person view exists so a supervisor can see
  who might want a coaching conversation.  It is not an assessment, and the page
  says so in both languages (ARISE People-Before-Machines).

Routes (all under ``/dashboard`` and ``/api/dashboard`` — nothing else is touched):

==================================  ====================================
``GET  /dashboard``                 login page, or the dashboard when authed
``POST /dashboard/login``           form/JSON password -> signed cookie
``POST /dashboard/logout``          clears the cookie
``GET  /api/dashboard/summary``     KPI tiles for a 7/30/90-day window
``GET  /api/dashboard/trend``       per-day series for the charts
``GET  /api/dashboard/persons``     pseudonymous progression + coaching flags
``GET  /api/dashboard/sessions``    recent sessions (paged)
``GET  /api/dashboard/session/{id}``one session's cycle curve + events
``GET  /api/dashboard/status``      live snapshot (session, FIWARE, heartbeat)
``GET  /api/dashboard/compare``     period A vs period B, with deltas
``GET  /api/dashboard/heatmap``     session starts by robot-local weekday x hour
``GET  /api/dashboard/modules``     per-scenario difficulty view
``GET  /api/dashboard/funnel``      monthly participation vs the roster
``GET  /api/dashboard/export.csv``  per-session rows, pseudonymous, as CSV
==================================  ====================================

The v2 routes above share ONE rule with the rest of the module:
**no evidence is never zero.**  Every rate is ``null`` when its denominator is
empty and every block carries the ``n`` it was computed from, so a period with
two sessions cannot be read as a period with a 0% score.

Env:
  ``EMORI_DASH_PASSWORD``  shared management password (empty -> 503, no service)
  ``EMORI_DASH_SALT``      also keys the login cookie (see :func:`_cookie_key`)
  ``EMORI_ROBOT_NAME``     display name in the header (falls back to ``EMORI_ROBOT_ID``)
  ``EMORI_HEARTBEAT_S``    the uptime denominator (must match the writer's cadence)
"""

# NOTE: no `from __future__ import annotations` -- FastAPI evaluates handler
# annotations at def time inside mount()'s closure scope (same as speech_api.py).

import calendar
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from emori_adapter.hostapp import dashboard_page, metrics_store
from emori_adapter.plan_events import LOST, NOT_PERFORMED

log = logging.getLogger("emori.hostapp.dashboard")

COOKIE_NAME = "emori_dash"
COOKIE_TTL_S = 12 * 3600
# Clock skew guard: a cookie stamped in the future is a forged/replayed one.
COOKIE_FUTURE_SLACK_S = 120.0

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 60.0
# Ceiling across ALL clients in the same window (see :class:`LoginLimiter`).
LOGIN_MAX_FAILURES_GLOBAL = 60

# The dashboard's headline targets, rendered as target lines on the KPI tiles.
# `participation` is the monthly one the funnel view is judged against: of the
# staff on the roster, what share practised at all this month.
TARGETS = {"attention": 80.0, "recovery": 70.0, "uptime": 95.0, "completion": 70.0,
           "participation": 70.0}

ALLOWED_DAYS = (7, 30, 90)
DEFAULT_DAYS = 7
MAX_SESSION_ROWS = 200

# --- v2 (before/after comparison) knobs ----------------------------------
# `compare` is NOT restricted to ALLOWED_DAYS: a "before vs after" question is
# asked about whatever the intervention's window was (a 21-day pilot, a term),
# and it costs the same indexed queries at any span.  Bounded anyway, because
# each period walks that many days of cycles through the covering index.
COMPARE_DEFAULT_DAYS = 30
COMPARE_MAX_DAYS = 365
FUNNEL_DEFAULT_MONTHS = 6
FUNNEL_MAX_MONTHS = 24
# The scenario table is a decision aid, not an inventory: a robot runs a handful
# of modules and the tail is noise.
MAX_MODULE_ROWS = 50
# One export must not be able to materialise a year of sessions in memory on a
# CM4.  ~5 k rows is about 700 kB of CSV and far more than any review reads.
MAX_EXPORT_ROWS = 5000
# A 30-minute session is ~900 cycles; the drill-in chart is 600 px wide, so more
# than ~300 points is bytes on the wire that no pixel can show.
MAX_CURVE_POINTS = 300

COMPETENCIES = (
    "communication_clarity",
    "engagement_presence",
    "emotional_intelligence",
    "reasoning_quality",
    "technique_application",
    "adaptability",
)

# The endings that mean the CONVERSATION NEVER HAPPENED: the SSO minted, the
# iframe never opened a session, nobody ever spoke. They are stored `abandoned`
# because they certainly did not produce an outcome, but they are a TECHNICAL
# failure, not a learner who gave up -- and while they sat in the completion
# denominator the rate understated real completion. Counted and reported
# separately (`not_started`), and excluded from `completion_pct`; every other
# abandonment still counts against it.
NOT_STARTED_REASONS = ("session_start_failed", "session_never_started")
# Inlined as literals (never parameters): these two queries already carry their
# window's parameters and SQLite has no list binding. The values are constants in
# this file, not input.
# Who ended a session: any reason on the row names the robot's own decision, except this
# one, where a person ended it with a head gesture and the analysis never came back.
_PERSON_ENDED_REASONS = ("gesture_end",)
_ROBOT_ENDED_SQL = ("SUM(CASE WHEN end_reason IS NULL OR end_reason='' "
                    "OR end_reason IN ('gesture_end') THEN 0 ELSE 1 END)")


def _robot_ended(reason: str) -> bool:
    return bool(reason) and reason not in _PERSON_ENDED_REASONS


_NOT_STARTED_SQL = ", ".join("'%s'" % reason for reason in NOT_STARTED_REASONS)

# Per-session change (in 0..100 competency points) below which a person's trend
# is reported as "steady" rather than improving/declining.  Sessions are noisy
# and an LLM-scored competency moves a few points on mood alone; calling that a
# trend would produce a coaching flag out of noise.
TREND_FLAT_BAND = 1.5
# The same band for the ENGAGEMENT fallback series (mean engagement x100).  Not
# the same number, because it is not the same unit: 1.5 there is a 0.015 change
# in a per-session mean whose own within-session samples span ~0.19..0.87, i.e.
# far inside the noise of the signal itself.  5.0 (= 0.05 of mean engagement) is
# the smallest move worth calling a direction.
TREND_FLAT_BAND_ENGAGEMENT = 5.0
# "mean competency < 50 across >= 3 sessions" -> coaching suggested.
COACHING_MIN_SESSIONS = 3
COACHING_SCORE_FLOOR = 50.0
# ...and a DECLINE must be this steep (competency points per session) before it
# suggests coaching.  A trend line is enough to draw; it is not enough to put a
# "Coaching suggested" chip against a named pseudonym on a management dashboard.
# With two sessions `_slope` IS the raw delta, so at TREND_FLAT_BAND a 70 -> 68
# pair -- one bad afternoon inside the "few points on mood alone" the band above
# exists for -- flagged a learner.  This is the gate that stops it.
COACHING_MIN_DECLINE = 5.0

_attention_min: Optional[float] = None


def attention_engaged_min() -> float:
    """The attention value at or above which a cycle counts as *attending*.

    ``ContextSignal.attention`` is currently the engagement score itself
    (``translation/context.py``: "proxy; refined later by gaze/eye-contact in the
    ROS layer"), so the only non-arbitrary threshold is the band at which the
    translator itself calls a learner ENGAGED — ``TranslationConfig.engagement_high``
    (0.62).  Read from that dataclass rather than duplicated, so retuning the
    bands retunes the KPI with them; hardcoded fallback if the import fails.
    """

    global _attention_min
    if _attention_min is None:
        try:
            from emori_adapter.translation import TranslationConfig

            _attention_min = float(TranslationConfig().engagement_high)
        except Exception:  # noqa: BLE001 - a dashboard must not need the translator
            _attention_min = 0.62
    return _attention_min


def robot_name() -> str:
    return (os.environ.get("EMORI_ROBOT_NAME", "").strip()
            or os.environ.get("EMORI_ROBOT_ID", "").strip()
            or "EMORI")


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def _cookie_key(password: str, salt: str) -> bytes:
    """The HMAC key the session cookie is signed with.

    Derived from the shared password AND ``EMORI_DASH_SALT`` so that (a) the
    cookie value never contains the password, (b) changing either the password or
    the salt invalidates every outstanding cookie, and (c) two robots with the
    same password do not accept each other's cookies.  One HMAC, not PBKDF2:
    nothing here is a stored password hash — the secret is only ever in memory
    from the environment, and this key is recomputed per request.
    """

    return hmac.new(("emori-dash-cookie-v1|" + salt).encode("utf-8"),
                    password.encode("utf-8"), hashlib.sha256).digest()


def _equal(supplied: Any, expected: Any) -> bool:
    """Constant-time equality for two strings of ANY charset.  Never raises.

    ``hmac.compare_digest`` refuses str arguments containing non-ASCII
    ("comparing strings with non-ASCII characters is not supported"), and BOTH
    operands on this module's two comparison sites are outside our control:
    the shared password is operator-chosen (on a Catalan site it will
    contain ç/à/é, and the dashboard would then 500 on every login attempt
    forever), and the cookie is whatever a client sends — Starlette decodes
    header bytes as latin-1, so one crafted byte turned every ``/dashboard`` and
    ``/api/dashboard/*`` request into an unauthenticated 500.  Comparing UTF-8
    bytes keeps the timing property and cannot raise.
    """

    try:
        return hmac.compare_digest(str(supplied).encode("utf-8", "surrogatepass"),
                                   str(expected).encode("utf-8", "surrogatepass"))
    except Exception:  # noqa: BLE001 - a comparison must never reach the client
        return False


def _sign(key: bytes, issued_at: int) -> str:
    # 128 bits of tag is far past what a LAN dashboard needs and keeps the cookie
    # short enough to read in a browser inspector while debugging.
    return hmac.new(key, str(issued_at).encode("ascii"), hashlib.sha256).hexdigest()[:32]


def make_cookie(password: str, salt: str, *, now: Optional[float] = None) -> str:
    issued = int(now if now is not None else time.time())
    return "%d.%s" % (issued, _sign(_cookie_key(password, salt), issued))


def cookie_valid(value: str, password: str, salt: str, *,
                 now: Optional[float] = None, ttl_s: float = COOKIE_TTL_S) -> bool:
    """Constant-time signature check + 12 h expiry.  Never raises."""

    if not password or not value or "." not in value:
        return False
    stamp, _, signature = value.partition(".")
    try:
        issued = int(stamp)
        expected = _sign(_cookie_key(password, salt), issued)
    except Exception:  # noqa: BLE001 - a hostile cookie is a 401, never a 500
        return False
    if not _equal(signature, expected):
        return False
    seconds = float(now if now is not None else time.time())
    return -COOKIE_FUTURE_SLACK_S <= (seconds - issued) <= ttl_s


class LoginLimiter:
    """Per-IP failure counter (5/minute) UNDER a global ceiling (60/minute).

    The per-IP bucket alone was not a limit at all on the deployment this ships
    into: an unfirewalled local network, one shared password, no second factor.  An
    attacker adds 500 secondary addresses to their interface (or picks them out
    of their own IPv6 /64) and gets 2,500 guesses a minute at a human-chosen
    password, each bucket quietly expiring so nothing accumulates and nothing
    alerts.  The global ceiling makes the total rate independent of how many
    source addresses the attacker owns.

    The tradeoff is explicit: while an attack is in progress a legitimate
    manager who mistypes can be told to wait too.  60 failures/minute is far
    above anything a handful of managers produce, the block lifts after one
    window, and being briefly rate-limited during an attack beats handing over
    every learner's progression table.

    In-memory on purpose — one hostapp process, and a restart clearing the
    counters is not the threat this defends against.
    """

    def __init__(self, *, max_failures: int = LOGIN_MAX_FAILURES,
                 window_s: float = LOGIN_WINDOW_S,
                 max_failures_global: int = LOGIN_MAX_FAILURES_GLOBAL) -> None:
        self.max_failures = int(max_failures)
        self.max_failures_global = int(max_failures_global)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._all: list[float] = []

    def _recent(self, client: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(client, ()) if now - t < self.window_s]
        if recent:
            self._fails[client] = recent
        else:
            self._fails.pop(client, None)
        return recent

    def blocked(self, client: str, *, now: Optional[float] = None) -> bool:
        seconds = float(now if now is not None else time.time())
        with self._lock:
            # The global list is pruned FIRST and is O(failures-in-window), not
            # O(distinct-IPs): once the ceiling is reached we answer without
            # touching `_fails` at all.  `dashboard_login` is `async def`, so
            # every scan below runs on the asyncio event loop that also carries
            # /ws/relay for the live session — it has to stay small.
            self._all = [t for t in self._all if seconds - t < self.window_s]
            if len(self._all) >= self.max_failures_global:
                return True
            # Bounded memory: a scan of a dict that only grows with distinct
            # attacking IPs, and the global ceiling above bounds how many of
            # those can accumulate inside one window.
            for other in [k for k in self._fails if k != client]:
                self._recent(other, seconds)
            return len(self._recent(client, seconds)) >= self.max_failures

    def record_failure(self, client: str, *, now: Optional[float] = None) -> None:
        seconds = float(now if now is not None else time.time())
        with self._lock:
            self._fails.setdefault(client, []).append(seconds)
            self._all.append(seconds)

    def clear(self, client: str) -> None:
        # Deliberately does NOT clear the global bucket: one correct password
        # among an attacker's guesses must not reset the ceiling for everyone.
        with self._lock:
            self._fails.pop(client, None)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _rows(store: Any, sql: str, params: tuple = ()) -> list[dict]:
    """One SELECT.  ``MetricsStore.query`` never raises; a missing store is []."""

    if store is None:
        return []
    try:
        return store.query(sql, params)
    except Exception:  # noqa: BLE001 - a dashboard read must never 500 the hostapp
        log.warning("dashboard query failed", exc_info=False)
        return []


def _one(store: Any, sql: str, params: tuple = ()) -> dict:
    rows = _rows(store, sql, params)
    return rows[0] if rows else {}


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # NaN -> None


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_days(value: Any, *, allowed: tuple = ALLOWED_DAYS) -> int:
    """Query strings are user input; the window is a closed set, not a number."""

    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    if allowed and days not in allowed:
        # Nearest allowed window rather than a 400: a bookmarked ?days=14 should
        # show something sensible, not an error page.
        return min(allowed, key=lambda option: abs(option - days))
    return days


def clamp_span_days(value: Any, *, default: int = COMPARE_DEFAULT_DAYS,
                    maximum: int = COMPARE_MAX_DAYS) -> int:
    """A free-form span in days, bounded.  Unlike :func:`clamp_days` this is not
    a closed set — see :data:`COMPARE_DEFAULT_DAYS`."""

    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(days, maximum))


def clamp_months(value: Any, *, default: int = FUNNEL_DEFAULT_MONTHS,
                 maximum: int = FUNNEL_MAX_MONTHS) -> int:
    try:
        months = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(months, maximum))


def parse_iso(value: Any, *, inclusive_end: bool = False) -> Optional[float]:
    """``YYYY-MM-DD`` (or a full ISO stamp) -> unix seconds, **UTC**.  None if unusable.

    A bare date is midnight UTC.  With ``inclusive_end`` a bare date means the
    END of that day (midnight + 24 h), because "1 - 7 July" is how a human writes
    a range that includes the 7th — every window in this module is
    half-open ``[start, end)``, so the +1 day happens here and nowhere else.
    """

    text = str(value or "").strip().rstrip("Zz")
    if not text:
        return None
    for fmt, date_only in (("%Y-%m-%d", True), ("%Y-%m-%dT%H:%M:%S", False),
                           ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
                           ("%Y-%m-%d %H:%M", False)):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        seconds = float(calendar.timegm(parsed))
        return seconds + 86400.0 if (date_only and inclusive_end) else seconds
    return None


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def _completion(completed: int, abandoned: int, not_started: int) -> Optional[float]:
    """``completed / conversations that actually started``.

    ``not_started`` is subtracted from the abandoned side only (a completed
    session by definition opened), and clamped: a store where the two counts
    disagree must not produce a >100% rate or a negative denominator.
    """

    gave_up = max(0, int(abandoned) - max(0, int(not_started)))
    return _pct(int(completed), int(completed) + gave_up)


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if not whole or whole <= 0 or part is None:
        return None
    return round(100.0 * float(part) / float(whole), 1)


def _scores(raw: Any) -> dict:
    """``sessions.performance_scores`` JSON -> {competency: 0..100}, filtered."""

    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for key in COMPETENCIES:
        value = _f(parsed.get(key))
        if value is not None:
            out[key] = max(0.0, min(100.0, value))
    return out


def _mean(values) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _slope(values: list) -> float:
    """Least-squares slope over an evenly spaced sequence (points per session)."""

    clean = [v for v in values if v is not None]
    n = len(clean)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(clean) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(clean))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def _direction(values: list, *, band: float = TREND_FLAT_BAND) -> str:
    slope = _slope(values)
    if slope > band:
        return "improving"
    if slope < -band:
        return "declining"
    return "flat"


def _DAY(column: str) -> str:
    """SQL for "the UTC calendar day of this unix-timestamp column".

    ``CAST(... AS INTEGER)`` because the stored timestamps are REAL and older
    SQLite builds than the dev box's 3.49 reject a fractional ``'unixepoch'``.
    Never interpolates anything but a literal column name from this module.
    """

    return "strftime('%%Y-%%m-%%d', CAST(%s AS INTEGER), 'unixepoch')" % column


def _day_keys(since: float, now: float) -> list[str]:
    """Dense UTC ``YYYY-MM-DD`` list so a quiet day is a gap in the chart, not
    a missing point that silently shortens the x axis."""

    days: list[str] = []
    start = int(since // 86400) * 86400
    stop = int(now // 86400) * 86400
    while start <= stop:
        days.append(time.strftime("%Y-%m-%d", time.gmtime(start)))
        start += 86400
    return days


# --------------------------------------------------------------------------
# aggregates (module-level and store-only, so they are unit-testable without HTTP)
# --------------------------------------------------------------------------
def _window(column: str, start: float, end: Optional[float]) -> tuple[str, tuple]:
    """``(sql fragment, params)`` for the half-open window ``[start, end)``.

    ``end=None`` means "up to now", which is what every single-window route asks
    for; :func:`compare` is the only caller that needs a closed one.  One helper
    so the two shapes cannot drift into two different definitions of a day.
    """

    if end is None:
        return "%s >= ?" % column, (float(start),)
    return "%s >= ? AND %s < ?" % (column, column), (float(start), float(end))


def _plan_counts(rows: list[dict]) -> dict:
    """``plan_counts`` rows, added up by family and by outcome.

    ``sentences`` is what the outcomes add up to.  ``families`` says what the robot performed:
    an empty family is a sentence on the talking flow, and a plan that moved nothing is in
    ``not_moved`` instead, because it performed no family at all.  ``lost_events`` is not a
    number of sentences: it is the records a session's queue could not hold, so they are kept
    out of the rest.
    """

    families: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    sentences = lost = not_moved = 0
    for row in rows:
        family, outcome = str(row.get("family") or ""), str(row.get("outcome") or "")
        n = _i(row.get("n"))
        if outcome == LOST:
            lost += n
            continue
        sentences += n
        outcomes[outcome] = outcomes.get(outcome, 0) + n
        if family == NOT_PERFORMED:
            not_moved += n
        else:
            families[family] = families.get(family, 0) + n
    return {"sentences": sentences, "families": families, "outcomes": outcomes,
            "not_moved": not_moved, "lost_events": lost}


def window_metrics(store: Any, start: float, end: Optional[float] = None, *,
                   now: Optional[float] = None) -> dict:
    """Every aggregate the KPI tiles and the period comparison share, for ONE window.

    Factored out of :func:`summary` so ``/api/dashboard/compare`` computes period
    A and period B with the *same* SQL rather than a second copy that drifts:
    a before/after claim is only worth anything if both halves were measured the
    same way.  Formulas are documented on :func:`summary`, which is now a thin
    presentation layer over this.

    Every query is either indexed on ``sessions.started_at`` or reaches ``cycles``
    through the join-through-sessions pattern (``idx_cycles_session_cov``); none
    of them scans ``cycles`` on its own.

    ``n_*`` counters are returned beside every rate on purpose: "recovery 100%"
    over one drop and over two hundred are different claims, and the caller must
    be able to tell them apart.
    """

    seconds = float(now if now is not None else time.time())
    win, args = _window("started_at", start, end)
    sess = _one(store,
                "SELECT COUNT(*) AS n, "
                "COUNT(DISTINCT CASE WHEN person_kind='badge' THEN person_code END) "
                "AS staff, "
                "SUM(CASE WHEN person_kind='walkup' THEN 1 ELSE 0 END) AS walkups, "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
                "SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) AS abandoned, "
                "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active, "
                + _ROBOT_ENDED_SQL + " AS robot_ended, "
                "SUM(CASE WHEN end_reason IN (" + _NOT_STARTED_SQL + ") THEN 1 ELSE 0 END) "
                "AS not_started, "
                "SUM(CASE WHEN performance_scores IS NULL THEN 0 ELSE 1 END) AS scored, "
                "SUM(CASE WHEN duration_s IS NULL THEN 0 ELSE 1 END) AS n_duration, "
                "AVG(duration_s) AS avg_duration "
                "FROM sessions WHERE " + win, args)

    threshold = attention_engaged_min()
    # Joined to `sessions` rather than filtered on `cycles.ts`: the only index on
    # cycles is (session_id, ts, ...), so a bare `WHERE ts >= ?` is a full scan of
    # the largest table on the robot (~18 k rows a busy day).  Through the join,
    # idx_sessions_started picks the window and idx_cycles_session_cov answers the
    # aggregate from the index alone.  It is also the truer question -- these are
    # the cycles of the sessions this window contains.
    cycle_win, cycle_args = _window("s.started_at", start, end)
    cycles = _one(store,
                  "SELECT COUNT(*) AS n, AVG(c.engagement_score) AS eng, "
                  "AVG(c.attention) AS att, "
                  "SUM(CASE WHEN c.attention >= ? THEN 1 ELSE 0 END) AS attentive "
                  "FROM cycles c JOIN sessions s ON s.session_id = c.session_id "
                  "WHERE " + cycle_win, (threshold,) + cycle_args)

    # Session-scoped events must belong to a session that EXISTS.  `cycles` above
    # is joined to `sessions`, so a cycle written without one is silently dropped
    # from every count on this page -- but the attention_drop / recovery_success
    # events those same cycles produced were counted on `ts` alone, and they are
    # the whole numerator and denominator of the headline recovery KPI.  In the
    # dev store half of both came from cycles that arrived with no session at all
    # (driver.on_analysis no longer writes those; this keeps the ones already on
    # disk out of the KPI).  Robot-level events -- the add-on's ext_* kinds,
    # fiware_skipped, the walk-away counters -- carry no session id and are
    # deliberately still counted.
    event_win, event_args = _window("e.ts", start, end)
    events = {str(row.get("kind", "")): _i(row.get("n"))
              for row in _rows(store, "SELECT kind, COUNT(*) AS n FROM events e "
                                      "WHERE " + event_win + " AND (e.session_id = '' "
                                      "OR EXISTS (SELECT 1 FROM sessions s "
                                      "WHERE s.session_id = e.session_id)) "
                                      "GROUP BY kind", event_args)}

    beat_win, beat_args = _window("ts", start, end)
    beats = _i(_one(store, "SELECT COUNT(*) AS n FROM heartbeats WHERE " + beat_win,
                    beat_args).get("n"))
    first_beat = _f(_one(store, "SELECT MIN(ts) AS t FROM heartbeats").get("t"))
    interval = metrics_store.heartbeat_interval_s()
    uptime_pct = None
    if interval > 0 and first_beat is not None:
        # Elapsed starts at the FIRST heartbeat ever written, so a robot installed
        # on Tuesday is not scored 0% for the previous three weeks of the window;
        # and it stops at the window's end (or now, whichever is earlier), so a
        # historical period A is not divided by time that has not happened in it.
        observed_from = max(start, first_beat)
        observed_to = min(seconds, end) if end is not None else seconds
        elapsed = observed_to - observed_from
        if elapsed >= interval:
            uptime_pct = min(100.0, round(100.0 * beats / (elapsed / interval), 1))

    scored = [_scores(row.get("performance_scores")) for row in
              _rows(store, "SELECT performance_scores FROM sessions WHERE " + win +
                    " AND performance_scores IS NOT NULL", args)]
    competencies = {}
    for key in COMPETENCIES:
        value = _mean([entry.get(key) for entry in scored if key in entry])
        competencies[key] = None if value is None else round(value, 1)

    lang_split = {}
    for row in _rows(store, "SELECT lang, COUNT(*) AS n FROM sessions WHERE " + win +
                     " GROUP BY lang ORDER BY n DESC", args):
        lang_split[str(row.get("lang") or "")] = _i(row.get("n"))

    # Every add-on usage kind the hostapp logged as `ext_<mode>`, keyed by the suffix: a
    # new mode shows up here without a code change.  Empty with no add-on installed.
    addon_events = {k[len("ext_"):]: v for k, v in events.items()
                    if k.startswith("ext_") and len(k) > len("ext_")}

    # WHY the robot ended those conversations, one indexed pass over the same
    # window.  Rendered beside completion_pct: "97% completed" and "197 of them
    # ended because the person walked away" are both true, and a page that shows
    # only the first is the 100%-completion fiction wearing a different hat.
    end_reasons = {}
    for row in _rows(store, "SELECT end_reason, COUNT(*) AS n FROM sessions WHERE " +
                     win + " AND end_reason IS NOT NULL AND end_reason <> '' "
                     "GROUP BY end_reason ORDER BY n DESC", args):
        end_reasons[str(row.get("end_reason") or "")] = _i(row.get("n"))

    # How the window's sentences went on the robot: the same window, through its
    # sessions, so `plan_counts`' own primary key answers each one.
    plan_counts = _plan_counts(_rows(
        store, "SELECT family, outcome, SUM(n) AS n FROM plan_counts WHERE session_id IN "
        "(SELECT session_id FROM sessions WHERE " + win + ") GROUP BY family, outcome", args))

    finished = _i(sess.get("completed")) + _i(sess.get("abandoned"))
    not_started = _i(sess.get("not_started"))
    cycle_n = _i(cycles.get("n"))
    return {
        "start": float(start),
        "end": None if end is None else float(end),
        "n_sessions": _i(sess.get("n")),
        "n_scored_sessions": _i(sess.get("scored")),
        "n_cycles": cycle_n,
        "n_finished": finished,
        "n_heartbeats": beats,
        "n_attention_drops": events.get("attention_drop", 0),
        "n_recoveries": events.get("recovery_success", 0),
        "n_duration": _i(sess.get("n_duration")),
        "unique_staff": _i(sess.get("staff")),
        "walkups": _i(sess.get("walkups")),
        "active": _i(sess.get("active")),
        "completed": _i(sess.get("completed")),
        "abandoned": _i(sess.get("abandoned")),
        # Sessions whose conversation never opened, and the count that excludes
        # them -- the honest denominator for "how many people practised".
        "not_started": not_started,
        "n_conversations": max(0, _i(sess.get("n")) - not_started),
        "completion_pct": _completion(_i(sess.get("completed")),
                                      _i(sess.get("abandoned")), not_started),
        "robot_ended": _i(sess.get("robot_ended")),
        "end_reasons": end_reasons,
        "avg_engagement": _round(_f(cycles.get("eng")), 3),
        "attention_mean": _round(_f(cycles.get("att")), 3),
        "attention_pct": _pct(_i(cycles.get("attentive")), cycle_n),
        "attention_threshold": threshold,
        "recovery_pct": _pct(events.get("recovery_success", 0),
                             events.get("attention_drop", 0)),
        "uptime_pct": uptime_pct,
        "avg_duration_s": _round(_f(sess.get("avg_duration")), 1),
        "competencies": competencies,
        "competency_mean": _round(
            _mean([v for v in competencies.values() if v is not None]), 1),
        "lang_split": lang_split,
        "addon_events": addon_events,
        "events": events,
        "plan_counts": plan_counts,
    }


def summary(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None) -> dict:
    """The KPI tiles.

    Formulas (documented because they are the numbers a manager will quote):

    * ``attention_pct`` — **% of 2-second cycle samples whose ``attention`` is at
      or above :func:`attention_engaged_min` (0.62)**, over the window.  Not the
      mean of ``attention``: a mean hides a session that alternated between rapt
      and lost, and the target ("attention > 80%") is a share-of-time claim.
      ``attention_mean`` is returned alongside for context.
    * ``recovery_pct`` — ``recovery_success`` events / ``attention_drop`` events.
      Both are produced by :class:`~..metrics_store.RecoveryTracker`; a drop that
      re-engages more than 90 s later is not counted as a success.
    * ``uptime_pct`` — heartbeat rows present / rows expected over the window,
      where expected = elapsed / ``EMORI_HEARTBEAT_S``.  Elapsed starts at the
      FIRST heartbeat ever written, so a robot installed on Tuesday is not scored
      0% for the previous three weeks of a 30-day window.
    * ``completion_pct`` — ``completed`` / (``completed`` + abandoned conversations),
      where a session whose conversation NEVER OPENED (``end_reason`` in
      :data:`NOT_STARTED_REASONS`) is not an abandoned conversation and is
      reported separately as ``not_started``.  Sessions still ``active`` are
      excluded (they have not finished either way).
      ``completed`` means the conversation PRODUCED ITS OUTCOME (Pokamind returned
      an analysis or scores), not that a learner pressed anything -- nobody presses
      anything, the watchdog ends the session and Pokamind answers with the whole
      analysis.  So this rate alone cannot tell you that people are walking off,
      and ``robot_ended`` is returned beside it for exactly that: how many of the
      window's sessions the ROBOT decided were over, with ``end_reasons`` naming
      which trigger (``walkaway_ends``, ``presence_ends``, ``watchdog_force_ends``,
      ``session_never_started``, ...).  The KPI page renders both.

    The arithmetic itself lives in :func:`window_metrics` (shared with
    ``/api/dashboard/compare``); this function is the window + the key names the
    page has always used.  ``competency`` and ``sessions`` keep their original
    names here — the v2 blocks call the same things ``competencies`` and
    ``n_sessions`` — because the shipped page reads these.
    """

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    window = window_metrics(store, since, None, now=seconds)
    out = dict(window)
    out.update({
        "days": days,
        "since": since,
        "generated_at": seconds,
        "sessions": window["n_sessions"],
        "cycles": window["n_cycles"],
        "attention_drops": window["n_attention_drops"],
        "recoveries": window["n_recoveries"],
        "heartbeats": window["n_heartbeats"],
        "heartbeat_interval_s": metrics_store.heartbeat_interval_s(),
        "competency": window["competencies"],
        "targets": dict(TARGETS),
    })
    return out


# --------------------------------------------------------------------------
# v2: period comparison ("did the intervention do anything?")
# --------------------------------------------------------------------------
# The numbers a delta is computed for.  Deliberately a list and not "every float
# in the block": `attention_threshold` is a constant, `start`/`end` are stamps,
# and subtracting either would produce a confident-looking meaningless number.
COMPARE_METRICS = (
    "n_sessions", "n_conversations", "not_started",
    "n_scored_sessions", "n_cycles", "unique_staff", "walkups",
    "completed", "abandoned", "robot_ended", "completion_pct", "avg_engagement",
    "attention_pct", "recovery_pct", "uptime_pct", "avg_duration_s", "competency_mean",
)


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """``b - a``, or None if either side has no evidence.

    Null-safe in the strict direction: a period with no scored session has a
    ``null`` competency, and "null -> 62" is not "+62".  Anything else would let
    the comparison manufacture an improvement out of a missing measurement.
    """

    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return b - a
    return round(float(b) - float(a), 3)


def _pct_change(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Relative change in %, or None when it would not mean anything.

    Undefined against a zero baseline (0 -> 5 sessions is not "+infinity%"), so
    the UI must fall back to the absolute delta there.  Note that for a metric
    that is ITSELF a percentage the honest reading of ``delta`` is *points*, and
    ``pct_change`` is the relative move — both are returned, labelled.
    """

    if a is None or b is None or float(a) == 0.0:
        return None
    return round(100.0 * (float(b) - float(a)) / abs(float(a)), 1)


def resolve_periods(*, days: Any = None, a_start: Any = None, a_end: Any = None,
                    b_start: Any = None, b_end: Any = None,
                    now: Optional[float] = None
                    ) -> tuple[tuple[float, float], tuple[float, float], dict]:
    """``(period_a, period_b, meta)`` from either ``days=N`` or four ISO dates.

    ``days=N``: B is the last N days, A is the N days immediately before it — the
    only comparison that needs no explanation in a management review.  Explicit
    dates win when all four are supplied and parse; anything partial or reversed
    raises ``ValueError`` rather than silently falling back to a window the
    caller did not ask for (a silently different period is exactly how a
    before/after chart lies).
    """

    seconds = float(now if now is not None else time.time())
    supplied = [v for v in (a_start, a_end, b_start, b_end) if str(v or "").strip()]
    if supplied:
        if len(supplied) != 4:
            raise ValueError("a_start, a_end, b_start and b_end must all be given")
        bounds = (parse_iso(a_start), parse_iso(a_end, inclusive_end=True),
                  parse_iso(b_start), parse_iso(b_end, inclusive_end=True))
        if any(v is None for v in bounds):
            raise ValueError("dates must be ISO (YYYY-MM-DD)")
        a0, a1, b0, b1 = bounds  # type: ignore[misc]
        if a1 <= a0 or b1 <= b0:
            raise ValueError("each period must end after it starts")
        span_a = (a1 - a0) / 86400.0
        span_b = (b1 - b0) / 86400.0
        if max(span_a, span_b) > COMPARE_MAX_DAYS:
            raise ValueError("a period may not exceed %d days" % COMPARE_MAX_DAYS)
        return (a0, a1), (b0, b1), {
            "mode": "explicit",
            "span_a_days": round(span_a, 2), "span_b_days": round(span_b, 2),
            # Comparing 7 days against 30 is legitimate (a pilot week vs the
            # month before) but the rates are the only comparable part of it.
            # Flagged rather than refused, so the UI can say so.
            "equal_spans": abs(span_a - span_b) < 1e-6,
        }
    span = clamp_span_days(days if days is not None else COMPARE_DEFAULT_DAYS)
    b0 = seconds - span * 86400.0
    a0 = b0 - span * 86400.0
    return (a0, b0), (b0, seconds), {"mode": "days", "days": span,
                                     "span_a_days": float(span),
                                     "span_b_days": float(span),
                                     "equal_spans": True}


def compare(store: Any, *, days: Any = None, a_start: Any = None, a_end: Any = None,
            b_start: Any = None, b_end: Any = None,
            now: Optional[float] = None) -> dict:
    """Period A vs period B on every shared KPI, with null-safe deltas.

    This is the before/after question — "is it better than before?" — and the only
    two ways it goes wrong are (1) measuring the two halves differently, which
    :func:`window_metrics` prevents by being the single implementation, and (2)
    reading a delta computed from three sessions as a result.  Hence ``n_*`` in
    both blocks and ``null`` (never 0) wherever a period has no evidence.

    Raises ``ValueError`` on an unusable explicit range (see
    :func:`resolve_periods`).
    """

    seconds = float(now if now is not None else time.time())
    (a0, a1), (b0, b1), meta = resolve_periods(
        days=days, a_start=a_start, a_end=a_end, b_start=b_start, b_end=b_end,
        now=seconds)
    a = window_metrics(store, a0, a1, now=seconds)
    b = window_metrics(store, b0, b1, now=seconds)

    delta: dict = {key: _diff(a.get(key), b.get(key)) for key in COMPARE_METRICS}
    change: dict = {key: _pct_change(a.get(key), b.get(key)) for key in COMPARE_METRICS}
    delta["competencies"] = {key: _diff(a["competencies"].get(key),
                                        b["competencies"].get(key))
                             for key in COMPETENCIES}
    change["competencies"] = {key: _pct_change(a["competencies"].get(key),
                                               b["competencies"].get(key))
                              for key in COMPETENCIES}
    return {"generated_at": seconds, "a": a, "b": b, "delta": delta,
            "pct_change": change, "metrics": list(COMPARE_METRICS),
            "targets": dict(TARGETS), **meta}


# --------------------------------------------------------------------------
# v2: when is the robot actually used?
# --------------------------------------------------------------------------
def _tz_label(stamp: float, convert: Callable) -> str:
    """The name of the timezone the robot is in RIGHT NOW, DST included.

    ``time.tzname[0]`` is the standard-time name whatever the date: on a robot in a
    zone with daylight saving time, the heatmap caption would name standard time
    through the whole summer while every bucket in it was in summer time -- an hour
    out, and nothing on the page would say so.  ``tzname[1]`` is the DST name, chosen only when
    the converted stamp says DST is in force.
    """

    names = time.tzname or ("",)
    try:
        isdst = int(getattr(convert(stamp), "tm_isdst", 0) or 0)
    except (OSError, OverflowError, ValueError):
        isdst = 0
    if isdst > 0 and len(names) > 1 and names[1]:
        return names[1]
    return names[0] if names else ""


def heatmap(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None,
            localtime: Optional[Callable] = None) -> dict:
    """Session starts bucketed by (weekday, hour) — the staffing question.

    **Timezone: robot-local, not UTC.**  Every other series in this module is
    UTC (a day boundary only has to be consistent), but "nobody uses it before
    10:00" is a claim about the site's clock — on a Europe/Madrid robot a UTC
    bucketing would shift every session an hour or two and put the evening peak
    in the wrong column.  The conversion uses the host's own timezone via
    ``time.localtime``; ``utc_offset_s`` and ``tz`` report which one that was, so
    a reader can tell whether the robot's clock is configured.

    ``weekday`` is 0=Monday .. 6=Sunday (``time.struct_time.tm_wday``), NOT
    SQLite's ``%w`` (0=Sunday) — a working-week grid reads left to right.

    Bucketed in Python from an indexed ``started_at`` range rather than in SQL:
    the input is one row per session (hundreds at most over 90 days), the
    ``'localtime'`` modifier would make the result depend on SQLite's tz handling
    instead of the host's, and this way the whole thing is testable with
    ``localtime=time.gmtime``.
    """

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    convert = localtime or time.localtime
    grid = [[0] * 24 for _ in range(7)]
    total = 0
    for row in _rows(store, "SELECT started_at FROM sessions WHERE started_at >= ?",
                     (since,)):
        stamp = _f(row.get("started_at"))
        if stamp is None:
            continue
        try:
            local = convert(stamp)
        except (OSError, OverflowError, ValueError):  # a corrupt stamp is not fatal
            continue
        grid[local.tm_wday % 7][local.tm_hour % 24] += 1
        total += 1

    try:
        # Rounded to the minute: `seconds` carries a fractional part, so the raw
        # subtraction reported 3599 for a robot exactly one hour ahead of UTC.
        offset = int(round((calendar.timegm(convert(seconds)) - seconds) / 60.0)) * 60
    except (OSError, OverflowError, ValueError):
        offset = 0
    return {
        "days": days, "since": since, "generated_at": seconds,
        "tz": _tz_label(seconds, convert), "utc_offset_s": offset,
        "weekday_base": "monday",
        "grid": grid,
        "weekday_totals": [sum(hours) for hours in grid],
        "hour_totals": [sum(grid[d][h] for d in range(7)) for h in range(24)],
        "max": max((max(hours) for hours in grid), default=0),
        "total": total,
    }


# --------------------------------------------------------------------------
# v2: which scenario is hard?
# --------------------------------------------------------------------------
def modules(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None,
            limit: int = MAX_MODULE_ROWS) -> dict:
    """Per-module usage and outcome — the "scenario difficulty" view.

    A module whose learners score low and abandon often is either genuinely hard
    or badly written, and either way it is the row a course designer wants.  What
    the numbers are:

    * ``avg_engagement`` — mean of the per-session ``sessions.avg_engagement``
      (one number per session, written at finalize), NOT a mean over cycles: a
      30-minute session must not outvote a 5-minute one when the question is
      "how did sessions on this scenario go".  ``n_engagement`` says how many
      sessions actually carried one.
    * ``competency_mean`` — mean over the scored sessions of that module's mean
      competency, with ``n_scored`` beside it.
    * ``completion_pct`` — completed / finished, as everywhere else.
    * ``robot_ended`` -- of those sessions, how many the ROBOT ended (walk-away,
      absence, a stalled turn).  A scenario people leave in the middle of is the
      row a course designer most wants, and it is invisible in ``completion_pct``:
      a walk-away Pokamind still analysed is stored ``completed``, exactly like a
      conversation somebody stayed for.

    One indexed pass over ``sessions`` in the window (never ``cycles``); the
    grouping and the JSON parse happen in Python because ``performance_scores``
    is a JSON blob SQLite cannot average.
    """

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    rows = _rows(store,
                 "SELECT module_id, module_title, topic, status, end_reason, "
                 "avg_engagement, performance_scores FROM sessions "
                 "WHERE started_at >= ?", (since,))
    not_started_reasons = set(NOT_STARTED_REASONS)

    grouped: dict[tuple, dict] = {}
    for row in rows:
        module_id = str(row.get("module_id") or "")
        title = str(row.get("module_title") or row.get("topic") or "")
        key = (module_id, title)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {"module_id": module_id, "module_title": title,
                                    "n_sessions": 0, "completed": 0, "abandoned": 0,
                                    "active": 0, "robot_ended": 0, "not_started": 0,
                                    "_eng": [], "_scores": []}
        entry["n_sessions"] += 1
        status = str(row.get("status") or "")
        if status in ("completed", "abandoned", "active"):
            entry[status] += 1
        reason = str(row.get("end_reason") or "")
        if _robot_ended(reason):
            entry["robot_ended"] += 1
        if reason in not_started_reasons:
            entry["not_started"] += 1
        engagement = _f(row.get("avg_engagement"))
        if engagement is not None:
            entry["_eng"].append(engagement)
        mean = _mean(list(_scores(row.get("performance_scores")).values()))
        if mean is not None:
            entry["_scores"].append(mean)

    out = []
    for entry in grouped.values():
        out.append({
            "module_id": entry["module_id"],
            "module_title": entry["module_title"],
            "n_sessions": entry["n_sessions"],
            "n_engagement": len(entry["_eng"]),
            "n_scored": len(entry["_scores"]),
            "completed": entry["completed"],
            "abandoned": entry["abandoned"],
            "active": entry["active"],
            "robot_ended": entry["robot_ended"],
            "not_started": entry["not_started"],
            "completion_pct": _completion(entry["completed"], entry["abandoned"],
                                          entry["not_started"]),
            "avg_engagement": _round(_mean(entry["_eng"]), 3),
            "competency_mean": _round(_mean(entry["_scores"]), 1),
        })
    out.sort(key=lambda m: (-m["n_sessions"], m["module_title"], m["module_id"]))
    return {"days": days, "since": since, "generated_at": seconds,
            "modules": out[:max(1, int(limit or MAX_MODULE_ROWS))],
            "total_modules": len(out)}


# --------------------------------------------------------------------------
# v2: monthly participation against the roster (target 70%)
# --------------------------------------------------------------------------
def _month_keys(months: int, *, now: float) -> list[str]:
    """The last ``months`` UTC calendar months, oldest first, ending with this one."""

    stamp = time.gmtime(now)
    index = stamp.tm_year * 12 + (stamp.tm_mon - 1)
    keys = []
    for back in range(months - 1, -1, -1):
        total = index - back
        keys.append("%04d-%02d" % (total // 12, total % 12 + 1))
    return keys


def _month_start(key: str) -> float:
    year, month = int(key[:4]), int(key[5:7])
    return float(calendar.timegm((year, month, 1, 0, 0, 0, 0, 1, -1)))


def funnel(store: Any, months: int = FUNNEL_DEFAULT_MONTHS, *,
           roster_size: Any = None, now: Optional[float] = None) -> dict:
    """Per calendar month: how many of the roster practised at all.

    ``participation_pct = active_badge_staff / roster_size * 100`` against the
    participation target of 70% a month.  The denominator is the number of staff on the
    robot's roster and NOTHING else about them: :func:`mount` takes an ``int`` or
    a zero-arg callable, never the roster object, so no name or badge id can
    reach this module even by accident.  Unknown or zero roster -> the percentage
    is ``null``, not 0% and not 100%: a robot with no roster configured has no
    participation story, and inventing one would put a red tile in a review for a
    number nobody set up.

    Walk-ups are counted in ``n_sessions`` but never in ``active_staff`` — they
    all share one pseudonym, so counting them would make one busy afternoon look
    like one more participating employee.

    Months are **UTC** calendar months (consistent with ``/trend``'s UTC days);
    only the heatmap uses robot-local time, and only because "what hour" is a
    question about the site's clock.
    """

    seconds = float(now if now is not None else time.time())
    span = clamp_months(months)
    keys = _month_keys(span, now=seconds)
    since = _month_start(keys[0])
    index = {key: i for i, key in enumerate(keys)}
    # Literal, not built through %-formatting like _DAY() -- so single %s here.
    month_sql = "strftime('%Y-%m', CAST(started_at AS INTEGER), 'unixepoch')"

    size = roster_size() if callable(roster_size) else roster_size
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    if size is not None and size <= 0:
        size = None

    staff = [0] * len(keys)
    for row in _rows(store, "SELECT " + month_sql + " AS month, "
                     "COUNT(DISTINCT person_code) AS n FROM sessions "
                     "WHERE started_at >= ? AND person_kind='badge' GROUP BY month",
                     (since,)):
        slot = index.get(str(row.get("month")))
        if slot is not None:
            staff[slot] = _i(row.get("n"))

    rows = [{"month": key, "n_sessions": 0, "completed": 0, "abandoned": 0,
             "robot_ended": 0, "not_started": 0,
             "active_staff": staff[i], "completion_pct": None,
             "participation_pct": (None if size is None
                                   else _pct(staff[i], size))}
            for i, key in enumerate(keys)]
    for row in _rows(store, "SELECT " + month_sql + " AS month, COUNT(*) AS n, "
                     "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
                     "SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) AS abandoned, "
                     + _ROBOT_ENDED_SQL + " AS robot_ended, "
                     "SUM(CASE WHEN end_reason IN (" + _NOT_STARTED_SQL + ") THEN 1 ELSE 0 END) "
                     "AS not_started "
                     "FROM sessions WHERE started_at >= ? GROUP BY month", (since,)):
        slot = index.get(str(row.get("month")))
        if slot is None:
            continue
        completed, abandoned = _i(row.get("completed")), _i(row.get("abandoned"))
        month_not_started = _i(row.get("not_started"))
        # robot_ended beside the monthly completion rate, for the same reason it sits
        # beside the KPI tile: this is the monthly-completion KPI, and a
        # month of walk-aways that Pokamind analysed reads 100% without it.
        rows[slot].update({"n_sessions": _i(row.get("n")), "completed": completed,
                           "abandoned": abandoned,
                           "robot_ended": _i(row.get("robot_ended")),
                           "not_started": month_not_started,
                           "completion_pct": _completion(completed, abandoned,
                                                         month_not_started)})
    # More distinct badges practised in a month than the roster lists: the roster
    # is out of date (or somebody scanned a badge that is not on it). Reported so
    # the page can say so instead of drawing a 150% bar off the top of the chart
    # and calling it participation.
    exceeded = bool(size) and any(
        (row["active_staff"] or 0) > int(size) for row in rows)
    return {"months": span, "since": since, "generated_at": seconds,
            "roster_size": size, "target_pct": TARGETS["participation"],
            "roster_exceeded": exceeded,
            "month_list": keys, "funnel": rows}


# --------------------------------------------------------------------------
# v2: CSV export
# --------------------------------------------------------------------------
# `end_reason` is appended AFTER the competencies rather than beside `status`: a
# review workbook built on this export addresses the score columns by position, and
# the point of adding a column is not to move the six that were already there.
EXPORT_COLUMNS = (("session_id", "started_at", "person_code", "kind", "lang",
                   "module", "duration_s", "avg_engagement", "status")
                  + COMPETENCIES + ("end_reason",))


def _csv_safe(value: Any) -> str:
    """Text that a spreadsheet cannot mistake for a formula.

    Nothing in this export is learner input (module titles are course metadata),
    but the file is opened by a manager in Excel and a leading ``=``/``+``/``-``/
    ``@`` is a code-execution prompt in most spreadsheets.  One apostrophe is the
    standard neutralisation and costs a character on data that never has one.
    """

    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_rows(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None,
                limit: int = MAX_EXPORT_ROWS) -> list[list]:
    """The export as a list of rows (header first).  Same privacy rules as the API.

    Everything here is already in ``/api/dashboard/sessions``; the CSV exists so a
    review can be prepared off the robot.  It therefore carries exactly the same
    fields — the STORED (de-identified) session id, the ``S-xxxxxx`` pseudonym —
    and above all NOT ``analysis_excerpt``, the only free text the store holds.
    """

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    capped = max(1, min(int(limit or MAX_EXPORT_ROWS), MAX_EXPORT_ROWS))
    rows: list[list] = [list(EXPORT_COLUMNS)]
    for row in _rows(store,
                     "SELECT session_id, started_at, person_code, person_kind, lang, "
                     "module_title, topic, duration_s, avg_engagement, status, "
                     "end_reason, performance_scores FROM sessions WHERE started_at >= ? "
                     "ORDER BY started_at DESC LIMIT ?", (since, capped)):
        started = _f(row.get("started_at"))
        duration = _f(row.get("duration_s"))
        engagement = _f(row.get("avg_engagement"))
        scores = _scores(row.get("performance_scores"))
        rows.append([
            _csv_safe(row.get("session_id")),
            "" if started is None else time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(started)),
            _csv_safe(row.get("person_code")),
            _csv_safe(row.get("person_kind")),
            _csv_safe(row.get("lang")),
            _csv_safe(row.get("module_title") or row.get("topic") or ""),
            "" if duration is None else round(duration, 1),
            "" if engagement is None else round(engagement, 3),
            _csv_safe(row.get("status")),
            *["" if scores.get(key) is None else scores[key] for key in COMPETENCIES],
            _csv_safe(row.get("end_reason") or ""),
        ])
    return rows


def export_csv(store: Any, days: int = DEFAULT_DAYS, *,
               now: Optional[float] = None) -> str:
    """:func:`export_rows` rendered as CSV text (CRLF, BOM).

    The BOM is there because the file is opened by double-click in Excel on a
    site laptop and a Catalan module title is unreadable without it.
    """

    buffer = io.StringIO()
    csv.writer(buffer, quoting=csv.QUOTE_MINIMAL).writerows(
        export_rows(store, days, now=now))
    return "\ufeff" + buffer.getvalue()


def export_filename(days: int, *, now: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%d", time.gmtime(now if now is not None else time.time()))
    return "emori-sessions-%dd-%s.csv" % (int(days), stamp)


def trend(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None) -> dict:
    """Per-UTC-day series: sessions, mean engagement, mean competency."""

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    keys = _day_keys(since, seconds)
    index = {day: i for i, day in enumerate(keys)}

    sessions = [0] * len(keys)
    for row in _rows(store,
                     "SELECT " + _DAY("started_at") + " AS day, "
                     "COUNT(*) AS n FROM sessions WHERE started_at >= ? "
                     "GROUP BY day ORDER BY day", (since,)):
        slot = index.get(str(row.get("day")))
        if slot is not None:
            sessions[slot] = _i(row.get("n"))

    engagement: list = [None] * len(keys)
    attention: list = [None] * len(keys)
    # Same join as summary(), for the same index reason.
    for row in _rows(store,
                     "SELECT " + _DAY("c.ts") + " AS day, "
                     "AVG(c.engagement_score) AS eng, AVG(c.attention) AS att "
                     "FROM cycles c JOIN sessions s ON s.session_id = c.session_id "
                     "WHERE s.started_at >= ? GROUP BY day ORDER BY day", (since,)):
        slot = index.get(str(row.get("day")))
        if slot is None:
            continue
        engagement[slot] = _round(_f(row.get("eng")), 3)
        attention[slot] = _round(_f(row.get("att")), 3)

    buckets: dict[str, list] = {}
    for row in _rows(store,
                     "SELECT " + _DAY("started_at") + " AS day, "
                     "performance_scores FROM sessions WHERE started_at >= ? "
                     "AND performance_scores IS NOT NULL", (since,)):
        scores = _scores(row.get("performance_scores"))
        mean = _mean(list(scores.values()))
        if mean is not None:
            buckets.setdefault(str(row.get("day")), []).append(mean)
    competency: list = [None] * len(keys)
    for day, values in buckets.items():
        slot = index.get(day)
        if slot is not None:
            competency[slot] = round(sum(values) / len(values), 1)

    return {"days": days, "since": since, "generated_at": seconds, "day_list": keys,
            "sessions": sessions, "engagement": engagement, "attention": attention,
            "competency": competency}


def persons(store: Any, days: int = DEFAULT_DAYS, *, now: Optional[float] = None) -> dict:
    """Pseudonymous per-person progression + the coaching flag.

    Only sessions that left EVIDENCE are counted -- at least one cycle row, or a
    competency score.  An SSO mint that was never opened, and the junk rows a
    dev-box backfill can import, have neither and are still not a learner's
    practice history.  Scores had to join cycles as evidence because they are the
    stronger of the two and they arrive by a different road: the cycle series is
    the CAMERA analysis stream, so a scored session run with a covered lens, a
    busy camera bridge or an audio-only conversation carried a full Pokamind
    analysis and still vanished from this table entirely, taking its person with
    it.  A row with scores and no cycles simply reports ``null`` engagement --
    which is the null-vs-0 rule, not a zero.

    ``coaching_flag`` = a scored decline of at least :data:`COACHING_MIN_DECLINE`
    points per session, OR a mean competency below 50 across at least three
    scored sessions.  Both require actual competency scores: an engagement-only
    drift is reported as a trend but never flags anybody.  Never set for the
    ``walkup`` bucket either, which is every anonymous drop-in collapsed into one
    code — flagging "them" would be flagging a crowd.

    Each row also carries ``competencies`` — the mean of each of the six
    competencies across that person's scored sessions, ``null`` where that
    competency was never scored — which is what a per-person radar is drawn from.

    **``most_improved`` lives here** (a top-level key of THIS response, not of
    :func:`summary`): it is a ranking of these same rows, so computing it
    anywhere else would mean re-reading and re-parsing every scored session.  It
    is the top three by ``competency_slope`` (least-squares points per session
    over the SCORED series only) among people with at least
    :data:`COACHING_MIN_SESSIONS` scored sessions and a positive slope.  The
    three gates are the same honesty the coaching flag gets: no engagement
    fallback (an engagement drift is not evidence of improvement), no two-point
    "trend", and never the ``walkup`` bucket — one shared pseudonym improving is
    a different crowd on a better day, not a person who is learning.
    """

    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    rows = _rows(store,
                 "SELECT s.person_code, s.person_kind, s.started_at, s.avg_engagement, "
                 "s.performance_scores FROM sessions s WHERE s.started_at >= ? "
                 "AND (EXISTS (SELECT 1 FROM cycles c WHERE c.session_id = s.session_id) "
                 "     OR (s.performance_scores IS NOT NULL AND s.performance_scores <> '')) "
                 "ORDER BY s.person_code, s.started_at", (since,))

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("person_code") or ""), []).append(row)

    out = []
    for code, entries in grouped.items():
        kind = str(entries[0].get("person_kind") or "")
        stamps = [_f(e.get("started_at")) for e in entries]
        stamps = [s for s in stamps if s is not None]
        engagements = [_f(e.get("avg_engagement")) for e in entries]
        means = []
        # Per-competency series for this person, so the UI can draw a radar
        # without a second pass over the same JSON blobs.
        per_competency: dict[str, list] = {key: [] for key in COMPETENCIES}
        for entry in entries:
            scores_row = _scores(entry.get("performance_scores"))
            for key, value in scores_row.items():
                per_competency[key].append(value)
            mean = _mean(list(scores_row.values()))
            if mean is not None:
                means.append(mean)
        competencies = {key: _round(_mean(values), 1)
                        for key, values in per_competency.items()}
        # Trend prefers the competency series (what a supervisor would discuss);
        # with fewer than two scored sessions it falls back to the engagement
        # series scaled to 0..100 -- same range, but NOT the same units, so it
        # gets its own flat band (see TREND_FLAT_BAND_ENGAGEMENT).
        scored = len(means) >= 2
        series = means if scored else [
            None if e is None else e * 100.0 for e in engagements]
        direction = _direction(
            series, band=TREND_FLAT_BAND if scored else TREND_FLAT_BAND_ENGAGEMENT)
        slope = _slope(series)
        avg_competency = _mean(means)
        # Two independent reasons to suggest a conversation, and both now demand
        # competency evidence:
        #   * a decline steep enough to be a decline (>= COACHING_MIN_DECLINE
        #     points/session) in the SCORED series.  The engagement fallback can
        #     no longer raise a flag at all: "attention drifted 0.02" is not
        #     evidence about how someone handled a difficult conversation, and it
        #     was reaching the flag with avg_competency still None.
        #   * a mean below the floor across at least COACHING_MIN_SESSIONS.
        flag = bool(
            (scored and direction == "declining" and slope <= -COACHING_MIN_DECLINE)
            or (len(means) >= COACHING_MIN_SESSIONS
                and avg_competency is not None
                and avg_competency < COACHING_SCORE_FLOOR))
        if kind == "walkup":
            flag = False
        out.append({
            "person_code": code,
            "kind": kind,
            "sessions": len(entries),
            "scored_sessions": len(means),
            "first_seen": min(stamps) if stamps else None,
            "last_seen": max(stamps) if stamps else None,
            "avg_engagement": (None if _mean(engagements) is None
                               else round(_mean(engagements), 3)),
            "avg_competency": None if avg_competency is None else round(avg_competency, 1),
            "competencies": competencies,
            "trend": direction,
            "trend_slope": round(slope, 2),
            # The slope of the SCORED series alone, whatever `trend_slope` was
            # computed from.  None below two scored sessions: a slope through one
            # point is 0.0, and a rendered 0.0 reads as "measured, no movement".
            "competency_slope": round(_slope(means), 2) if len(means) >= 2 else None,
            "coaching_flag": flag,
        })
    out.sort(key=lambda p: (-p["sessions"], p["person_code"]))
    return {"days": days, "since": since, "generated_at": seconds, "persons": out,
            "most_improved": _most_improved(out)}


def _most_improved(rows: list[dict], *, top: int = 3) -> list[dict]:
    """The three clearest positive competency slopes.  See :func:`persons`.

    A projection of rows that are already computed, not a second query.  Returns
    fewer than ``top`` — often zero — rather than padding the list: "nobody has
    three scored sessions yet" is the honest answer in month one, and a
    most-improved board filled with noise is worse than an empty one.
    """

    eligible = [row for row in rows
                if row["kind"] != "walkup"
                and row["scored_sessions"] >= COACHING_MIN_SESSIONS
                and row["competency_slope"] is not None
                and row["competency_slope"] > 0]
    eligible.sort(key=lambda p: (-p["competency_slope"], p["person_code"]))
    return [{"person_code": row["person_code"],
             "kind": row["kind"],
             "scored_sessions": row["scored_sessions"],
             "competency_slope": row["competency_slope"],
             "avg_competency": row["avg_competency"],
             "last_seen": row["last_seen"]}
            for row in eligible[:top]]


def sessions(store: Any, days: int = DEFAULT_DAYS, *, limit: int = 25, offset: int = 0,
             now: Optional[float] = None) -> dict:
    seconds = float(now if now is not None else time.time())
    since = seconds - days * 86400.0
    limit = max(1, min(int(limit or 25), MAX_SESSION_ROWS))
    offset = max(0, int(offset or 0))
    rows = _rows(store,
                 "SELECT session_id, started_at, ended_at, person_code, person_kind, "
                 "lang, module_title, topic, duration_s, avg_engagement, dominant_style, "
                 "status, end_reason, "
                 "CASE WHEN performance_scores IS NULL THEN 0 ELSE 1 END AS has_scores "
                 "FROM sessions WHERE started_at >= ? ORDER BY started_at DESC "
                 "LIMIT ? OFFSET ?", (since, limit, offset))
    total = _i(_one(store, "SELECT COUNT(*) AS n FROM sessions WHERE started_at >= ?",
                    (since,)).get("n"))
    return {"days": days, "since": since, "generated_at": seconds, "total": total,
            "limit": limit, "offset": offset,
            "sessions": [_session_row(row) for row in rows]}


def _session_row(row: dict) -> dict:
    return {
        # The STORED id, which metrics_store.deidentify_session_id has already
        # stripped of the SSO student id `_mint_session_id` embeds -- otherwise
        # every row here would publish `anna-001` next to `S-99bebd` and hand a
        # reader the map back from each pseudonym to a person.  It round-trips
        # to /api/dashboard/session/{id} unchanged.
        "session_id": str(row.get("session_id") or ""),
        "started_at": _f(row.get("started_at")),
        "ended_at": _f(row.get("ended_at")),
        "person_code": str(row.get("person_code") or ""),
        "person_kind": str(row.get("person_kind") or ""),
        "lang": str(row.get("lang") or ""),
        # module_title, falling back to the spoken topic -- both are course
        # metadata (a menu label), never anything the learner said.
        "module": str(row.get("module_title") or row.get("topic") or ""),
        "duration_s": _f(row.get("duration_s")),
        "avg_engagement": _f(row.get("avg_engagement")),
        "dominant_style": str(row.get("dominant_style") or ""),
        "status": str(row.get("status") or ""),
        # Who ended it: a watchdog reason slug, or "" for a learner-driven ending.
        # Course metadata about the ROBOT's decision -- never anything a person said.
        "end_reason": str(row.get("end_reason") or ""),
        "robot_ended": _robot_ended(str(row.get("end_reason") or "")),
        "has_scores": bool(_i(row.get("has_scores"))),
    }


def session_detail(store: Any, session_id: str, *,
                   max_points: int = MAX_CURVE_POINTS) -> Optional[dict]:
    """One session's cycle curve + its events, for the drill-in chart.

    Deliberately does NOT return ``analysis_excerpt``: it is the only free text in
    the store and it is *about* a named person's conversation.  The dashboard's
    promise is aggregates and pseudonyms.
    """

    head = _one(store,
                "SELECT session_id, started_at, ended_at, person_code, person_kind, "
                "lang, module_title, topic, duration_s, avg_engagement, dominant_style, "
                "status, end_reason, performance_scores, "
                "CASE WHEN performance_scores IS NULL THEN 0 ELSE 1 END AS has_scores "
                "FROM sessions WHERE session_id = ?", (str(session_id),))
    if not head:
        return None
    raw = _rows(store,
                "SELECT ts, engagement_level, engagement_score, attention "
                "FROM cycles WHERE session_id = ? ORDER BY ts", (str(session_id),))
    stride = max(1, (len(raw) + max_points - 1) // max_points) if raw else 1
    curve = [{"ts": _f(row.get("ts")),
              "level": _i(row.get("engagement_level")),
              "engagement_score": _f(row.get("engagement_score")),
              "attention": _f(row.get("attention"))}
             for row in raw[::stride]]
    events = [{"ts": _f(row.get("ts")), "kind": str(row.get("kind") or "")}
              for row in _rows(store,
                               "SELECT ts, kind FROM events WHERE session_id = ? "
                               "ORDER BY ts LIMIT 400", (str(session_id),))]
    detail = _session_row(head)
    detail["scores"] = _scores(head.get("performance_scores"))
    plan_counts = _plan_counts(_rows(
        store, "SELECT family, outcome, n FROM plan_counts WHERE session_id = ?",
        (str(session_id),)))
    return {"session": detail, "cycles": curve, "cycle_count": len(raw),
            "stride": stride, "events": events, "plan_counts": plan_counts}


def status(store: Any, *, driver: Any = None, addon_status: Any = None) -> dict:
    """Live snapshot: is a session running, is the FIWARE feed up, are we alive."""

    active = False
    try:
        tracker = getattr(driver, "tracker", None)
        active = bool(getattr(tracker, "active", False))
    except Exception:  # noqa: BLE001 - a driver in a strange state is not fatal
        active = False

    beat = _one(store, "SELECT ts, ok, load, mem_free_mb, session_active FROM heartbeats "
                       "ORDER BY ts DESC LIMIT 1")
    skipped = _one(store, "SELECT ts FROM events WHERE kind='fiware_skipped' "
                          "ORDER BY ts DESC LIMIT 1")
    return {
        "robot": robot_name(),
        "session_active": active,
        "fiware_enabled": os.environ.get("EMORI_FIWARE", "1") != "0",
        "last_fiware_skipped": _f(skipped.get("ts")),
        "addon": _addon_snapshot(addon_status),
        "heartbeat": ({"ts": _f(beat.get("ts")), "ok": bool(_i(beat.get("ok"))),
                       "load": _f(beat.get("load")),
                       "mem_free_mb": _i(beat.get("mem_free_mb")),
                       "session_active": bool(_i(beat.get("session_active")))}
                      if beat else None),
        "heartbeat_interval_s": metrics_store.heartbeat_interval_s(),
        "store_enabled": bool(getattr(store, "enabled", False)),
        "generated_at": time.time(),
        "targets": dict(TARGETS),
    }


def _addon_snapshot(addon_status: Any) -> Optional[dict]:
    """The installed add-on's public mode, read the cheapest safe way.

    An add-on keeps its own boundary, so this never reaches into it: either the hostapp
    handed us its already-validated status callable (the add-on's ``status()``
    projection), or we do plain attribute reads on the object and take only the
    scalar the header shows.  Any exception at all means "no add-on information".
    """

    if callable(addon_status):
        try:
            value = addon_status()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(value, dict):
            enabled = value.get("enabled")
            return {"mode": str(value.get("mode") or ""),
                    "enabled": (dict(enabled) if isinstance(enabled, dict) else {})}
        return None
    if addon_status is None:
        return None
    try:
        mode = getattr(addon_status, "mode", "")
        return {"mode": str(mode) if isinstance(mode, str) else "", "enabled": {}}
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# mount
# --------------------------------------------------------------------------
def mount(app, *, store, driver=None, addon_status=None,
          password=None, salt=None, roster_size=None) -> None:
    """Attach ``/dashboard`` + ``/api/dashboard/*``.

    Called from ``server.py`` right beside ``speech_api.mount(...)``.  ``store``
    is the :class:`~..metrics_store.MetricsStore` (``driver.metrics``); ``driver``
    is optional and only read for "is a session running"; ``addon_status`` is
    an optional zero-arg callable returning the installed add-on's safe projection.

    ``roster_size`` is the participation denominator for ``/api/dashboard/funnel``:
    an ``int``, or a zero-arg callable returning one (server.py passes a callable
    so a roster reloaded at runtime would be picked up without a restart).
    Deliberately a COUNT and not the ``Roster`` — this module must not be able to
    read a name or a badge id even by mistake.  ``None``/0 -> participation is
    reported as ``null``.

    ``password`` / ``salt`` exist for tests — in production both come from the
    environment on every request, so an operator who fixes a typo in the unit
    file and restarts gets the new secret with no other moving parts.
    """

    from fastapi import Request
    from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                                   Response)

    limiter = LoginLimiter()

    def _password() -> str:
        if password is not None:
            return str(password)
        return os.environ.get("EMORI_DASH_PASSWORD", "")

    def _salt() -> str:
        if salt is not None:
            return str(salt)
        return metrics_store.resolve_salt()

    def _authed(request) -> bool:
        secret = _password()
        return bool(secret) and cookie_valid(
            request.cookies.get(COOKIE_NAME, ""), secret, _salt())

    def _api_guard(request):
        """``None`` = allowed; otherwise the response to return immediately."""

        if not _password():
            return JSONResponse({"error": "dashboard_not_configured"}, status_code=503)
        if not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def _client(request) -> str:
        client = getattr(request, "client", None)
        return str(getattr(client, "host", "") or "unknown")

    def _client_code(client: str) -> str:
        """The login-audit identifier that goes in the JOURNAL.

        An IP address is personal data under the GDPR, and on a local network with
        DHCP reservations it names a workstation and usually the person at it --
        so the raw address is not written to a file that is world-readable to
        anyone holding the robot and survives reboots.  What authentication
        auditing actually asks is "is ONE source hammering us", and a salted
        HMAC answers that: repeated attempts still group, the rate-limit line
        and the failure line still fingerprint to the same value, and it is not
        a network identifier.  Same salt as every other pseudonym on this box
        (:func:`metrics_store.resolve_salt`), so it is stable across restarts.
        """

        return hmac.new(_salt().encode("utf-8"), (client or "").encode("utf-8"),
                        hashlib.sha256).hexdigest()[:8]

    # --- pages ------------------------------------------------------------
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page_route(request: Request):
        if not _password():
            return HTMLResponse(dashboard_page.NOT_CONFIGURED_HTML, status_code=503)
        if not _authed(request):
            return HTMLResponse(_login_html(request.query_params.get("e", "")))
        return HTMLResponse(_dashboard_html())

    @app.post("/dashboard/login")
    async def dashboard_login(request: Request):
        secret = _password()
        if not secret:
            return HTMLResponse(dashboard_page.NOT_CONFIGURED_HTML, status_code=503)
        client = _client(request)
        if limiter.blocked(client):
            log.warning("dashboard login rate-limited for client=%s", _client_code(client))
            return HTMLResponse(_login_html("rate"), status_code=429)
        supplied = await _form_password(request)
        # Constant-time on both sides: the length of a wrong password must not
        # be readable from the response time.  `_equal` and not compare_digest
        # directly -- a non-ASCII password (either side) raises out of it.
        if not _equal(supplied, secret):
            limiter.record_failure(client)
            log.warning("dashboard login failed for client=%s", _client_code(client))
            return HTMLResponse(_login_html("bad"), status_code=401)
        limiter.clear(client)
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            COOKIE_NAME, make_cookie(secret, _salt()), max_age=COOKIE_TTL_S,
            httponly=True, samesite="lax", path="/",
            # Not `secure`: the robot serves plain HTTP on a local network and a
            # secure cookie would simply never be sent back.
        )
        return response

    @app.post("/dashboard/logout")
    def dashboard_logout():
        response = RedirectResponse("/dashboard", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # --- api --------------------------------------------------------------
    @app.get("/api/dashboard/summary")
    def api_summary(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        return denied if denied is not None else summary(store, clamp_days(days))

    @app.get("/api/dashboard/trend")
    def api_trend(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        return denied if denied is not None else trend(store, clamp_days(days))

    @app.get("/api/dashboard/persons")
    def api_persons(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        return denied if denied is not None else persons(store, clamp_days(days))

    @app.get("/api/dashboard/sessions")
    def api_sessions(request: Request, days: int = DEFAULT_DAYS, limit: int = 25,
                     offset: int = 0):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        return sessions(store, clamp_days(days), limit=limit, offset=offset)

    @app.get("/api/dashboard/session/{session_id}")
    def api_session(request: Request, session_id: str):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        detail = session_detail(store, session_id)
        if detail is None:
            return JSONResponse({"error": "unknown_session"}, status_code=404)
        return detail

    @app.get("/api/dashboard/status")
    def api_status(request: Request):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        return status(store, driver=driver, addon_status=addon_status)

    # --- api v2 (before/after analytics) ----------------------------------
    @app.get("/api/dashboard/compare")
    def api_compare(request: Request, days: Optional[int] = None, a_start: str = "",
                    a_end: str = "", b_start: str = "", b_end: str = ""):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        try:
            return compare(store, days=days, a_start=a_start, a_end=a_end,
                           b_start=b_start, b_end=b_end)
        except ValueError as e:
            # A malformed explicit range is the ONE place this API refuses rather
            # than degrading: silently answering about a different period than
            # the one asked for is how a before/after chart lies.
            return JSONResponse({"error": "bad_range", "detail": str(e)},
                                status_code=400)

    @app.get("/api/dashboard/heatmap")
    def api_heatmap(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        return denied if denied is not None else heatmap(store, clamp_days(days))

    @app.get("/api/dashboard/modules")
    def api_modules(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        return denied if denied is not None else modules(store, clamp_days(days))

    @app.get("/api/dashboard/funnel")
    def api_funnel(request: Request, months: int = FUNNEL_DEFAULT_MONTHS):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        return funnel(store, clamp_months(months), roster_size=roster_size)

    @app.get("/api/dashboard/export.csv")
    def api_export(request: Request, days: int = DEFAULT_DAYS):
        denied = _api_guard(request)
        if denied is not None:
            return denied
        window = clamp_days(days)
        return Response(
            content=export_csv(store, window),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     'attachment; filename="%s"' % export_filename(window),
                     # The file is pseudonymous, but it is still a whole window of
                     # session rows: no proxy or browser cache should keep a copy.
                     "Cache-Control": "no-store"},
        )

    log.info("management dashboard mounted at /dashboard (%s)",
             "password set" if _password() else "NO PASSWORD - will serve 503")
    # The likely misconfiguration is password-set / salt-unset: an operator sets
    # the field with a visible effect and leaves the opaque one.  The fallback
    # salt is then "emori-dashboard-fallback:" + EMORI_ROBOT_ID -- a value that
    # appears in every `robot-<id>@emori.local` report address -- so the S-xxxxxx
    # codes in an exported DB or a screenshot are enumerable over a 30-person
    # roster in milliseconds.  metrics_store warns once at first use; say it
    # again HERE, where it is tied to a dashboard that is actually being served.
    if _password() and not os.environ.get("EMORI_DASH_SALT", "").strip() and salt is None:
        log.warning("dashboard is served but EMORI_DASH_SALT is unset: person codes "
                    "derive from a guessable fallback and are reversible by anyone "
                    "who knows EMORI_ROBOT_ID - set EMORI_DASH_SALT before a pilot")


async def _form_password(request) -> str:
    """Read ``password`` from a urlencoded form or a JSON body.

    Hand-parsed on purpose: ``fastapi.Form`` needs ``python-multipart``, and the
    robot venv is fixed (fastapi/uvicorn/pydantic/stdlib only).
    """

    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    content_type = str(request.headers.get("content-type", "")).lower()
    if "application/json" in content_type:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            return ""
        return str(payload.get("password", "")) if isinstance(payload, dict) else ""
    from urllib.parse import parse_qs

    try:
        fields = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    except Exception:  # noqa: BLE001
        return ""
    values = fields.get("password") or [""]
    return str(values[0])


def _login_html(error: str) -> str:
    # One pass, script-safe JSON (pages.fill / pages.script_json): the robot name is
    # set on the admin page or comes from Pokamind, and this page needs no login.
    from emori_adapter.hostapp import pages  # noqa: PLC0415

    key = error if error in ("bad", "rate") else ""
    return pages.fill(dashboard_page.LOGIN_HTML, {
        "__ROBOT__": pages.script_json(robot_name()),
        "__ERROR__": pages.script_json(key),
    })


def _dashboard_html() -> str:
    from emori_adapter.hostapp import pages  # noqa: PLC0415

    return pages.fill(dashboard_page.DASHBOARD_HTML, {
        "__ROBOT__": pages.script_json(robot_name()),
        "__DEFLANG__": pages.script_json("en"),
        "__TARGETS__": pages.script_json(TARGETS),
        "__ATTMIN__": pages.script_json(attention_engaged_min()),
    })
