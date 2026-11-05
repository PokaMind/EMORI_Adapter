"""On-robot SQLite metrics store — the management dashboard's only data source.

The robot already streams every cycle to FIWARE, but Orion-LD/QuantumLeap is an
OPTIONAL off-robot dependency: on a site LAN with no broker the historized
series simply does not exist, and a manager standing in front of the robot still
has to be able to answer "is this thing being used, and is it working?".  So the
hostapp keeps its own tiny local copy of the same numbers.  The two feeds are
deliberately redundant, not layered: FIWARE stays the interoperability story
(NGSI-LD + QuantumLeap + Grafana), this file is the appliance story.

Design constraints this module is shaped by:

* **Never breaks a session.**  Every write is best-effort — an exception is
  logged at WARNING and swallowed, exactly like :class:`~emori_adapter.fiware.FiwareClient`
  disables itself rather than propagate.  A full disk must cost the dashboard,
  never the conversation.
* **Threads AND the event loop write here.**  Cycles arrive on FastAPI's
  threadpool, finalizes on the watchdog thread, heartbeats on an asyncio task.
  One WRITE connection with ``check_same_thread=False`` behind one lock; every
  statement is a single short INSERT/UPDATE so no caller is ever parked on I/O
  long enough to matter.  Reads go through a SECOND connection behind a
  SEPARATE lock, which is the whole point of WAL: a manager's 90-day dashboard
  aggregate must never park the 2-second cycle write that is on the driver's
  motion path — nor the short per-session read the driver's own finalize does
  (see :meth:`MetricsStore.query`).
* **No real identity ever reaches the disk.**  A person is an HMAC-derived
  pseudonym (``S-xxxxxx``); anonymous walk-ups collapse to the single code
  ``walkup``; and the session id is stripped of the SSO student id that
  ``server.py:_mint_session_id`` embeds in it (see
  :func:`deidentify_session_id`).  The dashboard shows per-person progression
  without the robot storing who anybody is.

Env:
  ``EMORI_DASH_DB``    path to the SQLite file (default ``artifacts/dashboard.db``)
  ``EMORI_DASH_SALT``  HMAC key for pseudonymisation (see :func:`resolve_salt`)
  ``EMORI_DASH_RETENTION_DAYS``  raw-cycle retention (see :func:`retention_days`)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional

from emori_adapter import fiware_schema as schema

log = logging.getLogger("emori.metrics")

DEFAULT_DB_PATH = "artifacts/dashboard.db"
# 2 = the (session_id, ts) events index + the one-time legacy session-id
# de-identification pass (see :func:`migrate_session_ids`).  ``user_version`` is
# what makes that pass ONE-time: it is stamped after the pass runs, so an
# already-migrated robot file never scans again.
SCHEMA_VERSION = 5

# Raw 2-second cycle samples are the only table that grows without bound (a busy
# day is ~18 k rows; a year of them is the difference between a 5 MB file and a
# 200 MB one on an SD card, and every dashboard aggregate is O(rows-in-window)).
# The deepest window any route can ask for is 90 days, so a year of samples is
# already 4x more history than the dashboard can show.  Sessions, events, plan
# counts and heartbeats are tiny and are NEVER pruned -- the session history, the
# recovery KPI events and the uptime series survive in full.
DEFAULT_RETENTION_DAYS = 365.0
# One prune deletes in chunks, each its own transaction, so a first run against a
# year of backlog cannot hold the write lock (and therefore `record_cycle`) for
# more than one chunk at a time.
PRUNE_CHUNK_ROWS = 2000
PRUNE_MAX_CHUNKS = 500
PRUNE_INTERVAL_S = 24 * 3600.0

# REP-155 (hri_msgs/EngagementLevel): 0 UNKNOWN, 1 DISENGAGED, 2 ENGAGING,
# 3 ENGAGED, 4 DISENGAGING.  "Attention dropped" is the pair of levels that mean
# the learner is losing/has lost the thread; UNKNOWN is NOT one of them.
#
# UNKNOWN is NOT, however, what a session's first cycle looks like:
# ``EngagementTracker.update`` (translation/engagement.py) never returns it --
# a learner sitting still listening to the scenario is DISENGAGED on cycle 1.
# The "no opinion yet" state is therefore ``previous is None``, and that is what
# :class:`RecoveryTracker` gates on.
DROP_LEVELS = frozenset({1, 4})
ENGAGED_LEVEL = 3
RECOVERY_WINDOW_S = 90.0

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        started_at REAL,
        ended_at REAL,
        person_code TEXT,
        person_kind TEXT,
        lang TEXT,
        module_id TEXT,
        module_title TEXT,
        topic TEXT,
        is_roleplay INTEGER,
        duration_s REAL,
        avg_engagement REAL,
        dominant_style TEXT,
        performance_scores TEXT,
        analysis_excerpt TEXT,
        status TEXT,
        -- WHY the conversation ended, when the ROBOT decided it: the watchdog
        -- counter name (walkaway_ends / presence_ends / watchdog_force_ends /
        -- session_never_started), or one of the driver's own local endings.  NULL
        -- means the learner drove the ending.  `status` answers "did this session
        -- produce an outcome"; without this column the two questions collapsed
        -- into one and a day of walk-aways read as a 100% completion rate.
        end_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS cycles (
        session_id TEXT,
        ts REAL,
        engagement_level INTEGER,
        engagement_score REAL,
        valence REAL,
        arousal REAL,
        escalation REAL,
        rapport REAL,
        attention REAL,
        engagement_trend REAL,
        dominant_style TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        ts REAL,
        session_id TEXT,
        kind TEXT,
        detail TEXT
    )""",
    # How a session's sentences went: one row per family and outcome, written once
    # when the session ends (emori_adapter.plan_events).  A table of its own and not a
    # column on `sessions`, so the dashboard can total a window in SQL.  Added by this
    # statement alone on a file that predates it: CREATE TABLE IF NOT EXISTS runs on
    # every connect, and there is no history to carry over.
    """CREATE TABLE IF NOT EXISTS plan_counts (
        session_id TEXT,
        family TEXT,
        outcome TEXT,
        n INTEGER,
        PRIMARY KEY (session_id, family, outcome)
    )""",
    """CREATE TABLE IF NOT EXISTS heartbeats (
        ts REAL PRIMARY KEY,
        ok INTEGER,
        load REAL,
        mem_free_mb INTEGER,
        session_active INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS persons (
        person_code TEXT PRIMARY KEY,
        kind TEXT,
        first_seen REAL,
        last_seen REAL
    )""",
    # COVERING, and it replaces the old (session_id, ts) index rather than joining
    # it: `summary` and `trend` aggregate engagement_score/attention over every
    # cycle row in the window, and with a 2-column index each of those rows also
    # cost a rowid lookup into the largest b-tree on the robot.  Carrying the two
    # measures in the index answers both aggregates from the index alone (measured:
    # the trend variant 128.6 -> 99.3 ms, the summary variant 38.4 -> 20.3 ms on a
    # 220k-cycle window).  (session_id, ts) is a strict PREFIX of this, so every
    # lookup the old index served -- `session_detail`'s per-session curve above all
    # -- is served unchanged; keeping both would have doubled the index write that
    # `record_cycle` pays on the SD card for no read that needs it.
    "DROP INDEX IF EXISTS idx_cycles_session_ts",
    "CREATE INDEX IF NOT EXISTS idx_cycles_session_cov "
    "ON cycles(session_id, ts, engagement_score, attention)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, session_id)",
    # The drill-in ("SELECT ts, kind FROM events WHERE session_id=? ORDER BY ts")
    # had no usable index at all: idx_events_kind leads with `kind`, so opening
    # ONE session's timeline scanned the whole events table -- the one table that
    # is never pruned and therefore only grows.  Leading with session_id makes it
    # a range lookup, and carrying `ts` means the ORDER BY is free (no temp
    # b-tree).  Events are written a handful of times per session, not per cycle,
    # so the extra index write is nowhere near `record_cycle`'s hot path.
    "CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_person ON sessions(person_code, started_at)",
)

# Columns added to `sessions` after robots had already been recording.  ``CREATE
# TABLE IF NOT EXISTS`` above is a no-op on a file that already has the table, so
# a deployed dashboard.db never grows a new column from _SCHEMA alone -- and every
# later write naming it fails, which for this store means the session row silently
# stops being closed at all.  Kept as data so the ALTER and the CREATE cannot drift.
_ADDED_SESSION_COLUMNS = (("end_reason", "TEXT"),)


def add_missing_session_columns(conn: sqlite3.Connection) -> int:
    """ALTER in the ``sessions`` columns a file written by an older build lacks.

    Idempotent and cheap: one ``PRAGMA table_info`` read, and an ``ALTER TABLE ...
    ADD COLUMN`` only for what is genuinely absent (which fills the column with
    NULL for every historical row -- exactly the right answer for ``end_reason``,
    since nothing recorded who ended those sessions).  Never raises: a store that
    cannot migrate degrades to the older shape rather than taking the hostapp down
    with it.  Returns the number of columns added.  Caller commits.
    """

    try:
        present = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
    except Exception as e:  # noqa: BLE001 - an unreadable pragma is not fatal
        log.warning("sessions column migration could not read the table (%s: %s)",
                    type(e).__name__, e)
        return 0
    if not present:  # a fresh file: _SCHEMA just created the table complete
        return 0
    added = 0
    for name, decl in _ADDED_SESSION_COLUMNS:
        if name in present:
            continue
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (name, decl))
            added += 1
        except Exception as e:  # noqa: BLE001
            log.warning("sessions column migration failed on %s (%s: %s)",
                        name, type(e).__name__, e)
    if added:
        log.info("added %d missing sessions column(s)", added)
    return added


# --------------------------------------------------------------------------
# pseudonymisation
# --------------------------------------------------------------------------
_WALKUP_CODE = "walkup"
_salt_warned = False


def resolve_salt(salt: Optional[str] = None) -> str:
    """The HMAC key for :func:`person_code`.

    ``EMORI_DASH_SALT`` is the real answer.  Unset, we derive a deterministic
    fallback from ``EMORI_ROBOT_ID`` and warn ONCE: a per-boot random salt would
    silently break per-person progression across restarts (every learner would
    look like a new person), which is worse than a guessable-but-stable one for
    a code that never leaves the robot.
    """

    global _salt_warned
    if salt:
        return salt
    configured = os.environ.get("EMORI_DASH_SALT", "").strip()
    if configured:
        return configured
    if not _salt_warned:
        _salt_warned = True
        log.warning(
            "EMORI_DASH_SALT is unset; person codes derive from a fallback salt "
            "(stable across restarts, but set EMORI_DASH_SALT on any deployed robot)"
        )
    return "emori-dashboard-fallback:" + os.environ.get("EMORI_ROBOT_ID", "").strip()


def person_code(person_id: str, salt: Optional[str] = None) -> tuple[str, str]:
    """``person_id`` -> ``(code, kind)``.  Never reversible, never a name.

    Anonymous walk-ups all share the robot's own identity (``robot-<slug>``, see
    hostapp/identity.py), which is not a person at all — they collapse to one
    ``walkup`` bucket rather than becoming a fake "returning learner".
    """

    pid = (person_id or "").strip()
    if not pid or pid.startswith("robot-") or pid == "walkin":
        return _WALKUP_CODE, "walkup"
    digest = hmac.new(
        resolve_salt(salt).encode("utf-8"), pid.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "S-" + digest[:6], "badge"


# `<student_id>-<YYYYmmdd>-<HHMMSS>[-<16 hex nonce>][-rqh-<32 hex>]` is exactly the
# shape hostapp/server.py:_mint_session_id produces.  Greedy leading group, like
# scripts/dashboard_backfill.py:_SESSION_ID_RE, so the split lands on the LAST
# timestamp -- the one the mint appended -- and a student id that itself contains
# a date cannot move it.
_MINTED_SESSION_RE = re.compile(r"^.+-(?P<rest>\d{8}-\d{6}(?:-.*)?)$")


def deidentify_session_id(session_id: Any) -> str:
    """Drop the SSO student id that a minted session id carries.

    ``anna-001-20260730-101500-deadbeefdeadbeef`` -> ``20260730-101500-deadbeefdeadbeef``.

    Without this the store writes ``anna-001`` into ``sessions.session_id`` and
    every dashboard route hands it back beside that person's ``S-xxxxxx`` code --
    a 1:1 map from the pseudonym to the human, and a DB copy that is personal
    data rather than pseudonymous data.  What survives is the timestamp and the
    random nonce: unique, sortable, still enough to line a row up with its
    ``artifacts/outbox`` report by suffix, and about nobody.

    Ids that were not minted from an identity (unit tests, the ROS bridge stub,
    a hand-made id) do not match the shape and are stored verbatim.  Idempotent:
    the output never matches the pattern again.
    """

    text = str(session_id or "")
    match = _MINTED_SESSION_RE.match(text)
    return match.group("rest") if match is not None else text


# Tables whose `session_id` column the migration rewrites.  `cycles` is in the
# list even though only `sessions`/`events` carry the reviewed rows: renaming a
# session without its cycles would orphan that session's curve from the
# join-through-sessions aggregates every dashboard KPI uses.
_SESSION_ID_TABLES = ("sessions", "events", "cycles")


def migrate_session_ids(conn: sqlite3.Connection) -> int:
    """One-time: push legacy ``session_id`` values through :func:`deidentify_session_id`.

    :func:`deidentify_session_id` was added AFTER robots had been recording, so a
    deployed ``dashboard.db`` still holds rows whose id embeds the SSO student id
    (the audited robot file has two).  Every dashboard route hands the stored id
    back beside that person's ``S-xxxxxx`` code, which is a 1:1 map from the
    pseudonym to the human — so the fix has to reach the rows already on disk, not
    only the writers.

    Distinct-value driven (one ``UPDATE ... WHERE session_id=?`` per legacy id, of
    which there are ~2), not a table rewrite.  Idempotent twice over: the guard
    below skips ids that are already de-identified, and ``deidentify_session_id``
    never matches its own output — the ``user_version`` stamp in :meth:`_connect`
    is what stops the *scan* from happening again, not what stops a double
    rewrite.  A collision with an existing row (two minted ids that de-identify to
    the same value — impossible in practice, the mint appends 8 random bytes) is
    logged and the row is left alone rather than replacing real data.

    Returns the number of rows rewritten.  Caller commits.
    """

    total = 0
    for table in _SESSION_ID_TABLES:
        try:
            found = [row[0] for row in
                     conn.execute("SELECT DISTINCT session_id FROM %s" % table)]
        except Exception as e:  # noqa: BLE001 - a missing/odd table is not fatal
            log.warning("session-id migration could not scan %s (%s: %s)",
                        table, type(e).__name__, e)
            continue
        for old in found:
            if not isinstance(old, str) or not old:
                continue
            new = deidentify_session_id(old)
            if new == old:
                continue
            try:
                cursor = conn.execute(
                    "UPDATE %s SET session_id=? WHERE session_id=?" % table, (new, old))
                total += max(0, int(cursor.rowcount or 0))
            except sqlite3.IntegrityError:
                # `sessions.session_id` is a PRIMARY KEY: the de-identified id
                # already exists.  Keeping both rows beats merging them blind.
                log.warning("session-id migration skipped a %s row: the "
                            "de-identified id already exists", table)
            except Exception as e:  # noqa: BLE001
                log.warning("session-id migration failed on %s (%s: %s)",
                            table, type(e).__name__, e)
    if total:
        log.info("de-identified %d legacy session-id rows", total)
    return total


# --------------------------------------------------------------------------
# recovery state machine (engagement-recovery KPI: "recovery success > 70%")
# --------------------------------------------------------------------------
def repair_zero_engagement(conn: sqlite3.Connection) -> int:
    """Null out the engagement measurement that was never taken (schema v4).

    Older rows hold the ``0.0`` that averaging an empty list gave, written by
    ``session_finished``, so a session that produced no
    analysis cycle at all -- a covered lens, an audio-only conversation, or the
    whole camera path being down -- is indistinguishable on disk from a session
    where a person sat motionless, so every Engagement column, every per-module
    mean and the engagement KPI read 0%.

    The guard is what makes this safe to run over real data: only rows whose
    session has NO cycle at all are touched.  A genuine measured 0.0 (there are
    cycles, and they really did average zero) keeps its value.
    """

    cursor = conn.execute(
        "UPDATE sessions SET avg_engagement=NULL WHERE avg_engagement=0.0 "
        "AND NOT EXISTS (SELECT 1 FROM cycles c WHERE c.session_id = sessions.session_id)")
    return int(cursor.rowcount or 0)


def close_orphan_sessions(conn: sqlite3.Connection, *, older_than_s: float = 0.0,
                          now: Optional[float] = None) -> int:
    """Close session rows left ``active`` by a process that is gone.

    Called once at hostapp startup, where the rule is unambiguous: nothing is
    running yet, so a row still marked ``active`` belongs to a run that ended
    without finalizing -- a power cut, a ``systemctl restart`` mid-session, an
    OOM.  Left alone they are immortal: counted in every session total and
    excluded from every completion rate for ever.

    ``ended_at`` and ``duration_s`` are deliberately NOT invented -- nobody knows
    when that conversation stopped, and a fabricated duration would land in
    ``avg_duration_s``.  Only the two facts we do know are written: it is over
    (``abandoned``), and the robot did not decide that (``interrupted``).
    """

    stamp = float(now if now is not None else time.time())
    cutoff = stamp - max(0.0, float(older_than_s))
    cursor = conn.execute(
        "UPDATE sessions SET status='abandoned', "
        "end_reason=COALESCE(NULLIF(end_reason,''), 'interrupted') "
        "WHERE status='active' AND (started_at IS NULL OR started_at <= ?)", (cutoff,))
    return int(cursor.rowcount or 0)


class RecoveryTracker:
    """Turns a stream of engagement levels into attention_drop / recovery_success.

    Pure and tiny on purpose — this is the state machine behind a headline KPI,
    so it is unit-tested in isolation rather than inferred from SQL after the
    fact.  Semantics:

    * entering DISENGAGED(1) or DISENGAGING(4) from anything else -> ``attention_drop``
      (a sustained dip counts once, not once per 2 s cycle);
    * reaching ENGAGED(3) within ``window_s`` of that drop -> ``recovery_success``;
    * later than the window, the drop simply expires — the learner re-engaged,
      but not because the robot's nudge worked, and inflating the KPI with those
      would make it meaningless.
    * the FIRST cycle of a session is never a drop, whatever level it carries.
      There is no transition to observe yet, so there is nothing the robot could
      have caused.  This matters: ``EngagementTracker`` commits to a level on
      cycle 1 and never reports UNKNOWN, and a learner quietly listening to the
      scenario scores DISENGAGED there — so counting it would have added one
      synthetic drop to EVERY such session, in BOTH halves of the headline
      engagement-recovery KPI and in the ``recoveryAttempts``/``recoverySuccesses`` pair
      published to Orion.
    """

    def __init__(self, *, window_s: float = RECOVERY_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._last: dict[str, int] = {}
        self._drop_at: dict[str, float] = {}

    def observe(self, session_id: str, level: int, ts: float) -> list[str]:
        """Feed one cycle; returns the event kinds it produced (0, 1 or 2)."""

        out: list[str] = []
        try:
            level = int(level)
            ts = float(ts)
        except (TypeError, ValueError):
            return out
        previous = self._last.get(session_id)
        self._last[session_id] = level
        dropped_at = self._drop_at.get(session_id)
        if dropped_at is not None and ts - dropped_at > self.window_s:
            # The window closed: this drop can no longer be "recovered".
            self._drop_at.pop(session_id, None)
            dropped_at = None
        if level in DROP_LEVELS:
            # `previous is None` == the first cycle of this session: seed the
            # state and report nothing (see the class docstring).
            if previous is not None and previous not in DROP_LEVELS:
                self._drop_at[session_id] = ts
                out.append("attention_drop")
            return out
        if level == ENGAGED_LEVEL and dropped_at is not None:
            self._drop_at.pop(session_id, None)
            out.append("recovery_success")
        return out

    def forget(self, session_id: str) -> None:
        self._last.pop(session_id, None)
        self._drop_at.pop(session_id, None)


# --------------------------------------------------------------------------
# host sampling (heartbeat)
# --------------------------------------------------------------------------
def sample_host() -> tuple[float, int]:
    """(1-min loadavg, MemAvailable MB).  ``(0.0, 0)`` where /proc is absent."""

    load = 0.0
    try:
        load = float(os.getloadavg()[0])  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # Windows dev box
        load = 0.0
    mem_free_mb = 0
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    mem_free_mb = int(int(line.split()[1]) / 1024)
                    break
    except (OSError, ValueError, IndexError):
        mem_free_mb = 0
    return load, mem_free_mb


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------
class MetricsStore:
    """Best-effort SQLite writer + reader.  Construct once per process."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        salt: Optional[str] = None,
        recovery_window_s: float = RECOVERY_WINDOW_S,
    ) -> None:
        self.path = path or os.environ.get("EMORI_DASH_DB", "") or DEFAULT_DB_PATH
        self.salt = salt
        self.recovery = RecoveryTracker(window_s=recovery_window_s)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        # Readers get their own connection + lock so a dashboard aggregate can
        # never park a cycle write, and a read that finds that connection busy
        # takes a private handle rather than queueing behind it.  See `query`.
        self._read_lock = threading.RLock()
        self._read_conn: Optional[sqlite3.Connection] = None
        self._disabled = False
        self._warned = 0
        self._pruned_at: Optional[float] = None

    # --- plumbing ---------------------------------------------------------
    def _warn(self, what: str, exc: BaseException) -> None:
        # Throttled: a broken disk would otherwise write one WARNING per cycle
        # forever and bury every other line in the journal.
        self._warned += 1
        if self._warned <= 5 or self._warned % 200 == 0:
            log.warning("dashboard store %s failed (%s: %s)", what, type(exc).__name__, exc)

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._disabled:
            return None
        if self._conn is not None:
            return self._conn
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL, not FULL: this is telemetry on an SD card, and an fsync per
            # 2 s cycle is a measurable cost on the CM4 for data whose worst-case
            # loss is the last few seconds of a chart.
            conn.execute("PRAGMA synchronous=NORMAL")
            # Read BEFORE the schema pass (which does not touch user_version) so
            # a file written by an older build can be told from a fresh one.
            try:
                row = conn.execute("PRAGMA user_version").fetchone()
                found_version = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001 - an unreadable pragma means "old"
                found_version = 0
            for statement in _SCHEMA:
                conn.execute(statement)
            if found_version < SCHEMA_VERSION:
                # A file whose `sessions` table predates a column CREATE TABLE IF
                # NOT EXISTS can no longer add.  Before any write below, because
                # `session_finished` names every column it sets.
                add_missing_session_columns(conn)
            if found_version < 2:
                # One-time, and cheap on a fresh file (three empty DISTINCT
                # scans).  See :func:`migrate_session_ids` for why it must run
                # against rows already on disk.
                migrate_session_ids(conn)
            if found_version < 4:
                # One indexed UPDATE on a fresh file (no rows match); on a robot
                # that has been running for a while it is the difference between a
                # dashboard of 0% columns and one that says "not measured".
                repaired = repair_zero_engagement(conn)
                if repaired:
                    log.warning("dashboard store: %d session(s) had a fabricated "
                                "0.0 engagement with no cycles -> set to NULL "
                                "(not measured)", repaired)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
            return conn
        except Exception as e:  # noqa: BLE001 - no DB must never break a session
            self._disabled = True
            log.warning("dashboard store unavailable at %s (%s: %s); metrics disabled",
                        self.path, type(e).__name__, e)
            return None

    def _read_connect(self) -> Optional[sqlite3.Connection]:
        """A second, reader-only connection to the same file (or the writer's).

        WAL's whole promise is that readers do not block the writer -- but only
        across CONNECTIONS.  One connection behind one lock would give that
        away: ``summary(days=90)`` scans every cycle row in the
        window off an SD card while ``record_cycle`` -- which ``driver.on_analysis``
        runs before dispatching the behaviour motion -- waits for the same lock.
        A manager refreshing a phone must not stall the robot mid-conversation.

        Best-effort like everything else here: if the second handle cannot be
        opened we fall back to the writer's, which is merely the old behaviour.

        Call with ``_read_lock`` held: this is the SHARED reader, and its warm
        page cache is why it is shared at all.  A caller that finds the lock
        already taken opens a private handle instead (see :meth:`query`).
        """

        if self._read_conn is not None:
            return self._read_conn
        # Ensures the file, the schema and journal_mode=WAL exist before a reader
        # ever looks at them.  Under the WRITE lock, because `_connect` mutates
        # `_conn`/`_disabled`: lock order is always _read_lock -> _lock (only here
        # and in `close`, and no writer ever takes _read_lock), so this cannot
        # invert.  Reached at most once -- afterwards `_read_conn` short-circuits.
        with self._lock:
            writer = self._connect()
        if writer is None:
            return None
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            self._read_conn = conn
            return conn
        except Exception as e:  # noqa: BLE001 - degrade to the writer, never raise
            # Latched, not retried: a second handle that cannot be opened once
            # will not open on the next dashboard refresh either, and retrying
            # per read would put a failing connect() on the reader's hot path.
            self._warn("read_connect", e)
            self._read_conn = writer
            return writer

    def _write(self, statements: Iterable[tuple[str, tuple]], *, what: str) -> bool:
        with self._lock:
            conn = self._connect()
            if conn is None:
                return False
            try:
                for sql, params in statements:
                    conn.execute(sql, params)
                conn.commit()
                return True
            except Exception as e:  # noqa: BLE001 - swallowed by contract
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                self._warn(what, e)
                return False

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def close(self) -> None:
        with self._read_lock:
            reader, self._read_conn = self._read_conn, None
        with self._lock:
            conn, self._conn = self._conn, None
        # Deduplicated by identity: `_read_conn` can BE `_conn` when the second
        # handle could not be opened and `_read_connect` fell back to the writer.
        for handle in {id(h): h for h in (reader, conn) if h is not None}.values():
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    # --- writers ----------------------------------------------------------
    def log_event(self, kind: str, *, session_id: str = "", detail: str = "",
                  ts: Optional[float] = None) -> bool:
        return self._write(
            [("INSERT INTO events (ts, session_id, kind, detail) VALUES (?,?,?,?)",
              (float(ts if ts is not None else time.time()),
               deidentify_session_id(session_id),
               str(kind), str(detail or "")[:400]))],
            what="log_event",
        )

    def upsert_person(self, code: str, kind: str, ts: Optional[float] = None) -> bool:
        now = float(ts if ts is not None else time.time())
        return self._write(
            [("INSERT INTO persons (person_code, kind, first_seen, last_seen) "
              "VALUES (?,?,?,?) ON CONFLICT(person_code) DO UPDATE SET "
              "last_seen=excluded.last_seen, kind=excluded.kind",
              (code, kind, now, now))],
            what="upsert_person",
        )

    def session_started(
        self,
        session_id: str,
        *,
        person_id: str = "",
        lang: str = "",
        module_id: str = "",
        module_title: str = "",
        topic: str = "",
        is_roleplay: bool = True,
        started_at: Optional[float] = None,
    ) -> str:
        """Open a session row (+ person + ``session_started`` event).

        Returns the person_code so a caller can log it without re-deriving.
        """

        now = float(started_at if started_at is not None else time.time())
        code, kind = person_code(person_id, self.salt)
        sid = deidentify_session_id(session_id)
        self.recovery.forget(sid)
        self.upsert_person(code, kind, now)
        self._write(
            [("INSERT OR REPLACE INTO sessions (session_id, started_at, ended_at, "
              "person_code, person_kind, lang, module_id, module_title, topic, "
              "is_roleplay, duration_s, avg_engagement, dominant_style, "
              "performance_scores, analysis_excerpt, status) "
              "VALUES (?,?,NULL,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,'active')",
              (sid, now, code, kind, str(lang or ""), str(module_id or ""),
               str(module_title or ""), str(topic or ""), 1 if is_roleplay else 0))],
            what="session_started",
        )
        self.log_event("session_started", session_id=sid, detail=code, ts=now)
        return code

    def record_cycle(self, session_id: str, signals: Any,
                     ts: Optional[float] = None) -> list[str]:
        """One analysis cycle — the SAME nine metrics that go to FIWARE.

        Deliberately routed through :func:`fiware_schema.metric_values` so the
        local series and the NGSI-LD series can never diverge in meaning.
        Returns the recovery events it produced (also written to ``events``).
        """

        now = float(ts if ts is not None else time.time())
        try:
            values = schema.metric_values(signals)
        except Exception as e:  # noqa: BLE001 - an odd signal object is not fatal
            self._warn("record_cycle(metrics)", e)
            return []
        sid = deidentify_session_id(session_id)
        self._write(
            [("INSERT INTO cycles (session_id, ts, engagement_level, engagement_score, "
              "valence, arousal, escalation, rapport, attention, engagement_trend, "
              "dominant_style) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (sid, now, values["engagementLevel"], values["engagementScore"],
               values["valence"], values["arousal"], values["escalation"],
               values["rapport"], values["attention"], values["engagementTrend"],
               values["dominantStyle"]))],
            what="record_cycle",
        )
        kinds = self.recovery.observe(sid, values["engagementLevel"], now)
        for kind in kinds:
            self.log_event(kind, session_id=sid, ts=now)
        return kinds

    def session_finished(
        self,
        session_id: str,
        *,
        ended_at: Optional[float] = None,
        avg_engagement: Optional[float] = None,
        dominant_style: str = "",
        status: str = "completed",
        results: Any = None,
        duration_s: Optional[float] = None,
        end_reason: str = "",
    ) -> bool:
        """Close the session row.

        ``results`` is the raw Pokamind ``pokamind_session_completed`` payload
        (``stats['results']``); its ``performance_scores`` are stored as JSON and
        the first 600 chars of ``session_analysis`` as the excerpt.  Everything
        else in that payload — transcripts above all — is deliberately dropped.

        ``is_roleplay`` is also taken from that payload when it states one: the row
        is opened with the hostapp's assumption (``start_session`` passes a hard
        ``True``, because at SSO time nothing has yet said what the module is), and
        Pokamind is the only party that knows.  A payload with no opinion leaves the
        stored value untouched.

        ``end_reason`` is the OTHER half of ``status``: who decided the conversation
        was over.  Empty means the learner did (and leaves whatever is already on the
        row); anything else is the robot's own reason -- a watchdog counter name, or
        one of the driver's local endings.  Both are needed because the deployed UX
        is robot-driven: a walk-away that Pokamind still answered with a full
        analysis IS a session that produced its outcome, and it is ALSO a person who
        left the room, and a dashboard that can only read ``status`` cannot report
        the second thing at all.
        """

        now = float(ended_at if ended_at is not None else time.time())
        scores, excerpt = _results_fields(results)
        is_roleplay = (roleplay_flag(results.get("is_roleplay"))
                       if isinstance(results, dict) else None)
        sid = deidentify_session_id(session_id)
        with self._lock:
            conn = self._connect()
            if conn is None:
                return False
            try:
                if duration_s is None:
                    row = conn.execute(
                        "SELECT started_at FROM sessions WHERE session_id=?", (sid,)
                    ).fetchone()
                    started = row["started_at"] if row is not None else None
                    duration_s = max(0.0, now - float(started)) if started else None
                # COALESCE throughout: a finalize that knows less than what is
                # already on the row (the backfill closing a session imported from
                # an outbox report, a local fail-safe finalize with no analysis)
                # must not erase the fields it has no opinion about.
                conn.execute(
                    "UPDATE sessions SET ended_at=?, duration_s=?, "
                    "avg_engagement=COALESCE(?, avg_engagement), "
                    "dominant_style=COALESCE(NULLIF(?,''), dominant_style), "
                    "status=?, "
                    "is_roleplay=COALESCE(?, is_roleplay), "
                    "end_reason=COALESCE(NULLIF(?,''), end_reason), "
                    "performance_scores=COALESCE(?, performance_scores), "
                    "analysis_excerpt=COALESCE(?, analysis_excerpt) WHERE session_id=?",
                    (now, duration_s,
                     None if avg_engagement is None else float(avg_engagement),
                     str(dominant_style or ""), str(status or "completed"),
                     is_roleplay, str(end_reason or "")[:64], scores, excerpt, sid),
                )
                conn.commit()
                ok = True
            except Exception as e:  # noqa: BLE001
                self._warn("session_finished", e)
                ok = False
        self.recovery.forget(sid)
        return ok

    def record_plan_counts(self, session_id: str,
                           counts: Mapping[tuple[str, str], int]) -> bool:
        """How a session's sentences went, by ``(family, outcome)``.

        The driver keeps these in memory and calls this once, when the session ends, so
        a sentence costs the card nothing.  Families and outcomes are short words the
        robot chose, never anything a person said.  Added to what the row holds, so a
        second call for a session cannot lose the first.  Nothing to say writes nothing.
        """

        if not counts:
            return True
        sid = deidentify_session_id(session_id)
        return self._write(
            [("INSERT INTO plan_counts (session_id, family, outcome, n) VALUES (?,?,?,?) "
              "ON CONFLICT(session_id, family, outcome) DO UPDATE SET n=n+excluded.n",
              (sid, str(family)[:32], str(outcome)[:32], int(n)))
             for (family, outcome), n in counts.items()],
            what="record_plan_counts",
        )

    def close_orphan_sessions(self, *, older_than_s: float = 0.0,
                              now: Optional[float] = None) -> int:
        """Close ``active`` rows nobody owns any more.  See the module function.

        Returns the number closed (0 when the store is disabled), and never
        raises: a startup housekeeping pass must not be able to stop the hostapp.
        """

        with self._lock:
            conn = self._connect()
            if conn is None:
                return 0
            try:
                closed = close_orphan_sessions(conn, older_than_s=older_than_s, now=now)
                conn.commit()
                return closed
            except Exception as e:  # noqa: BLE001 - housekeeping is never fatal
                self._warn("close_orphan_sessions", e)
                return 0

    def heartbeat(self, *, ok: bool = True, session_active: bool = False,
                  ts: Optional[float] = None) -> bool:
        load, mem_free_mb = sample_host()
        return self._write(
            [("INSERT OR REPLACE INTO heartbeats (ts, ok, load, mem_free_mb, "
              "session_active) VALUES (?,?,?,?,?)",
              (float(ts if ts is not None else time.time()), 1 if ok else 0,
               load, mem_free_mb, 1 if session_active else 0))],
            what="heartbeat",
        )

    # --- retention --------------------------------------------------------
    def prune(self, *, older_than_days: Optional[float] = None,
              now: Optional[float] = None) -> int:
        """Delete raw cycle samples older than the retention window.

        The store had no pruning at all, and every dashboard aggregate is
        O(cycles-in-window): ``trend(90)``'s per-day cycles roll-up is already
        the only query over 100 ms, and nothing bounded how much larger it could
        get (2000 sessions a year is ~180 MB, and the CM4 reads that off an SD
        card).  This is the bound.

        ONLY ``cycles`` is pruned -- the 2-second raw samples, whose deepest
        consumer is a 90-day window.  ``sessions``, ``events`` (the recovery KPI)
        and ``heartbeats`` (uptime) are small and are kept forever, so the
        per-session history a manager can list, and every KPI but the cycle
        curve of a year-old session, survive a prune untouched.

        Chunked, one transaction per chunk: a first run against a year of
        backlog must not hold the write lock -- and therefore ``record_cycle``,
        which sits on the driver's cycle path -- for more than one chunk.
        Returns the number of rows deleted; best-effort like every other write.
        """

        days = retention_days() if older_than_days is None else float(older_than_days)
        if days <= 0:
            return 0
        cutoff = float(now if now is not None else time.time()) - days * 86400.0
        deleted = 0
        for _ in range(PRUNE_MAX_CHUNKS):
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return deleted
                try:
                    # Rows are written in time order, so the LIMIT'd inner SELECT
                    # finds its chunk at the head of the table rather than
                    # scanning for it (there is no index on `ts` alone).
                    cursor = conn.execute(
                        "DELETE FROM cycles WHERE rowid IN "
                        "(SELECT rowid FROM cycles WHERE ts < ? LIMIT ?)",
                        (cutoff, PRUNE_CHUNK_ROWS),
                    )
                    conn.commit()
                    chunk = max(0, int(cursor.rowcount or 0))
                except Exception as e:  # noqa: BLE001 - swallowed by contract
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    self._warn("prune", e)
                    return deleted
            deleted += chunk
            if chunk < PRUNE_CHUNK_ROWS:
                break
        if deleted:
            log.info("dashboard store pruned %d cycle rows older than %.0f days",
                     deleted, days)
            # The deleted pages are sitting in the WAL; fold them back now rather
            # than leaving the next `record_cycle` to pay for a huge autocheckpoint.
            self._write([("PRAGMA wal_checkpoint(PASSIVE)", ())], what="prune(checkpoint)")
        return deleted

    def maybe_prune(self, *, now: Optional[float] = None) -> int:
        """:meth:`prune`, at most once every :data:`PRUNE_INTERVAL_S`."""

        seconds = float(now if now is not None else time.time())
        with self._lock:
            last = self._pruned_at
            if last is not None and 0 <= seconds - last < PRUNE_INTERVAL_S:
                return 0
            self._pruned_at = seconds
        return self.prune(now=seconds)

    # --- readers (FIWARE summary) -------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run one read-only SELECT and return plain dicts (never raises).

        Uses a reader connection, NOT the writer's -- a 90-day aggregate off an
        SD card must never be on the critical path of a 2-second cycle write.
        ``session_id`` parameters here are the STORED (de-identified) ids: this
        is the raw-SQL seam, so callers that hold a minted id must pass it
        through :func:`deidentify_session_id` first.

        Readers must not park each other either, and the second connection alone
        did not buy that: one ``_read_lock`` around every read meant the driver's
        own ``recovery_counts`` -- which ``_finalize_locked`` runs while holding
        ``driver._lock`` -- waited out whatever 90-day aggregate a manager's phone
        had in flight (measured 0.038 ms idle vs 117.6 ms during a ``trend(90)``
        refresh, with the driver lock held throughout: a delayed "heard you" nod
        and a delayed kiosk page for the next learner).  So the shared handle is
        taken only when it is FREE; a read that finds it busy opens a private
        handle for the duration (~0.2 ms) rather than queueing.  WAL supports any
        number of concurrent readers, and the common case -- one read at a time --
        still reuses the warm shared cache.
        """

        # Non-blocking: whoever holds the shared handle keeps it.  RLock, so a
        # nested read on the same thread would reuse it rather than deadlock.
        if self._read_lock.acquire(blocking=False):
            try:
                return self._select(self._read_connect(), sql, params)
            finally:
                self._read_lock.release()
        # Contended.  `_conn` is not None here (the holder went through
        # `_read_connect`, which opens the writer first), so the file, the schema
        # and journal_mode=WAL all exist and a plain handle is safe to open.
        conn = None if self._disabled or self._conn is None else self._read_open()
        if conn is None:
            # No private handle (out of file descriptors, a read-only mount): the
            # old behaviour -- wait for the shared one -- is still better than
            # dropping the read.
            with self._read_lock:
                return self._select(self._read_connect(), sql, params)
        try:
            return self._select(conn, sql, params)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _read_open(self) -> Optional[sqlite3.Connection]:
        """One NEW read handle on an already-initialised file.  ``None`` on failure."""

        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:  # noqa: BLE001 - the caller degrades, never raises
            self._warn("read_open", e)
            return None

    def _select(self, conn: Optional[sqlite3.Connection], sql: str,
                params: tuple) -> list[dict]:
        if conn is None:
            return []
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        except Exception as e:  # noqa: BLE001
            self._warn("query", e)
            return []

    def recovery_counts(self, session_id: str) -> dict:
        """``{'attempts': n, 'successes': n}`` for one session (FIWARE summary)."""

        rows = self.query(
            "SELECT kind, COUNT(*) AS n FROM events WHERE session_id=? "
            "AND kind IN ('attention_drop','recovery_success') GROUP BY kind",
            (deidentify_session_id(session_id),),
        )
        counts = {row["kind"]: int(row["n"]) for row in rows}
        return {"attempts": counts.get("attention_drop", 0),
                "successes": counts.get("recovery_success", 0)}


def scores_json(scores: Any) -> Optional[str]:
    """``performance_scores`` -> the exact JSON text stored on the session row.

    Public because the backfill writes the same column through the raw ``_write``
    seam (``scripts/dashboard_backfill.py``): two copies of this encoding would
    eventually differ in truncation or escaping, and the dashboard parses the
    column with one ``json.loads``.  ``None`` for anything that is not a
    non-empty mapping -- an empty scores object is *no scores*, never ``{}``,
    because a stored ``{}`` reads as "scored, all competencies missing".
    """

    if not isinstance(scores, dict) or not scores:
        return None
    try:
        return json.dumps(scores, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return None


def roleplay_flag(value: Any) -> Optional[int]:
    """``results['is_roleplay']`` -> 1/0, or ``None`` when it says nothing.

    ``None`` is load-bearing: the session row is opened with the hostapp's
    assumption (``is_roleplay=1``) before Pokamind has said anything, and a
    payload that carries no opinion must leave that value alone rather than
    overwrite it with a fabricated 0.
    """

    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes"):
            return 1
        if text in ("0", "false", "no"):
            return 0
    return None


def results_recorded(results: Any) -> bool:
    """Would :meth:`MetricsStore.session_finished` store an OUTCOME for this payload?

    The one question the completion status turns on (``driver.on_completed``), and
    it is deliberately answered by the SAME function that does the storing.  Asking
    ``bool(results)`` instead let the two disagree, and the disagreement is not
    hypothetical: the recorded completion envelopes in ``artifacts/recordings`` are
    full of ``{'body_language_metrics': ..., 'duration_minutes': ..., 'styles': ...}``
    -- non-empty, and carrying no scores, no analysis and no summary.  Those were
    stamped 'completed' onto a row with NULL performance_scores AND NULL
    analysis_excerpt: a completion with literally nothing to show, counted in the
    numerator of the monthly-completion KPI.
    """

    return any(_results_fields(results))


def _results_fields(results: Any) -> tuple[Optional[str], Optional[str]]:
    """(performance_scores JSON, analysis excerpt) out of a Pokamind payload."""

    if not isinstance(results, dict):
        return None, None
    scores_json_text = scores_json(results.get("performance_scores"))
    analysis = results.get("session_analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        analysis = results.get("summary")
    excerpt = analysis[:600] if isinstance(analysis, str) and analysis.strip() else None
    return scores_json_text, excerpt


# --------------------------------------------------------------------------
# process singleton
# --------------------------------------------------------------------------
_store: Optional[MetricsStore] = None
_store_lock = threading.Lock()


def get_store() -> MetricsStore:
    """The process-wide store.  Lazy: the file is only created on first write."""

    global _store
    with _store_lock:
        if _store is None:
            _store = MetricsStore()
        return _store


def set_store(store: Optional[MetricsStore]) -> None:
    """Replace (or clear, with ``None``) the singleton — tests and the backfill."""

    global _store
    with _store_lock:
        _store = store


def retention_days() -> float:
    """How many days of raw cycle samples to keep.  ``0`` disables pruning.

    Default :data:`DEFAULT_RETENTION_DAYS` (a year) — four times the deepest
    window any dashboard route can ask for, so the bound is invisible to the
    manager and visible only to the SD card.
    """

    raw = (os.environ.get("EMORI_DASH_RETENTION_DAYS", "") or "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_DASH_RETENTION_DAYS=%r is not a number; using %s",
                    raw, DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else 0.0


def heartbeat_interval_s() -> float:
    try:
        value = float(os.environ.get("EMORI_HEARTBEAT_S", "60"))
    except (TypeError, ValueError):
        return 60.0
    return value if value > 0 else 0.0


async def heartbeat_loop(store: MetricsStore, *, interval_s: float,
                         session_active: Optional[Callable[[], bool]] = None) -> None:
    """Write one heartbeat row every ``interval_s`` until cancelled.

    Uptime KPI = present beats / expected beats over a window, which is why the
    row is written on a fixed cadence rather than on activity: a robot that is
    up and idle must still prove it was up.  The write is pushed to a worker
    thread so a slow SD card cannot stall the event loop.
    """

    import asyncio

    while True:
        try:
            active = bool(session_active()) if callable(session_active) else False
        except Exception:  # noqa: BLE001 - an unreadable driver is not a heartbeat failure
            active = False
        try:
            await asyncio.to_thread(store.heartbeat, ok=True, session_active=active)
            # Retention runs from here because this is the one loop that ticks
            # whether or not anyone is using the robot -- and only while NOTHING
            # is: housekeeping deletes must never compete with a live session's
            # cycle writes for the SD card.  Self-throttled to once a day.
            if not active:
                await asyncio.to_thread(store.maybe_prune)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - contract: never break the app lifespan
            log.warning("heartbeat write failed")
        await asyncio.sleep(interval_s)
