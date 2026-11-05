"""The on-robot dashboard store: pseudonymity, the recovery KPI state machine,
failure isolation, thread safety, the driver wiring and the backfill importer.

The store's whole contract is "records everything, breaks nothing", so the tests
that matter most here are the ones that make it fail.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from emori_adapter.hostapp.metrics_store import (
    SCHEMA_VERSION,
    MetricsStore,
    RecoveryTracker,
    deidentify_session_id,
    migrate_session_ids,
    person_code,
    resolve_salt,
)
from emori_adapter.models import ContextSignal, DerivedSignals, EngagementLevel

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _store(tmp_path: Path, **kw) -> MetricsStore:
    return MetricsStore(str(tmp_path / "dash.db"), salt="unit-test-salt", **kw)


def _signals(level: EngagementLevel = EngagementLevel.ENGAGED, score: float = 0.7,
             style: str = "mirroring") -> DerivedSignals:
    return DerivedSignals(
        timestamp="2026-07-30T10:00:00Z", session_id="s1", person_id="p1", seq=1,
        engagement_score=score, engagement_level=level, valence=0.2, arousal=0.1,
        expression="happy", expression_confidence=0.4, top_styles=[(style, 0.3)],
        context=ContextSignal(engagement_trend=0.05, escalation=0.1, rapport=0.6,
                              attention=0.8, dominant_style=style),
    )


# --- schema ---------------------------------------------------------------
def test_schema_is_created_with_user_version_and_wal(tmp_path):
    store = _store(tmp_path)
    store.log_event("boot")
    store.close()

    conn = sqlite3.connect(tmp_path / "dash.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"sessions", "cycles", "events", "heartbeats", "persons",
                "plan_counts"} <= tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_cycles_session_cov" in indexes
        assert "idx_events_ts" in indexes
        assert "idx_events_session_ts" in indexes
        assert "idx_sessions_started" in indexes
        # The cycles index carries the two measures the dashboard aggregates, so
        # summary()/trend() never leave it for the 179 MB table b-tree.  It
        # REPLACES (session_id, ts) -- a strict prefix of it -- rather than
        # joining it, so record_cycle still maintains exactly one index.
        assert "idx_cycles_session_ts" not in indexes
        plan = " | ".join(row[-1] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT AVG(c.engagement_score), AVG(c.attention) "
            "FROM cycles c JOIN sessions s ON s.session_id = c.session_id "
            "WHERE s.started_at >= 0"))
        assert "COVERING INDEX idx_cycles_session_cov" in plan, plan
    finally:
        conn.close()


def test_an_existing_database_migrates_to_the_covering_index(tmp_path):
    """The index swap has to happen on the robot's live file, not just a fresh one."""

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cycles (session_id TEXT, ts REAL, engagement_level INTEGER, "
                 "engagement_score REAL, valence REAL, arousal REAL, escalation REAL, "
                 "rapport REAL, attention REAL, engagement_trend REAL, dominant_style TEXT)")
    conn.execute("CREATE INDEX idx_cycles_session_ts ON cycles(session_id, ts)")
    conn.execute("INSERT INTO cycles (session_id, ts) VALUES ('s1', 1.0)")
    conn.commit()
    conn.close()

    store = MetricsStore(str(path), salt="unit-test-salt")
    store.log_event("boot")
    store.close()

    conn = sqlite3.connect(path)
    try:
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_cycles_session_cov" in indexes
        assert "idx_cycles_session_ts" not in indexes
        assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_drill_in_events_query_has_an_index(tmp_path):
    """`WHERE session_id=? ORDER BY ts` must not scan the never-pruned events table."""

    store = _store(tmp_path)
    store.log_event("boot", session_id="s1")
    store.close()

    conn = sqlite3.connect(tmp_path / "dash.db")
    try:
        plan = " | ".join(row[-1] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT ts, kind FROM events WHERE session_id = 's1' "
            "ORDER BY ts LIMIT 400"))
        assert "idx_events_session_ts" in plan, plan
        assert "SCAN events" not in plan, plan
        # ...and the ORDER BY comes off the index, not a temp b-tree.
        assert "TEMP B-TREE" not in plan.upper(), plan
    finally:
        conn.close()


# --- the one-time session-id de-identification ----------------------------
LEGACY_ID = "anna-001-20260730-101500-deadbeefdeadbeef"
CLEAN_ID = "20260730-101500-deadbeefdeadbeef"


def _legacy_db(path: Path) -> None:
    """A pre-de-identification file: real rows, and user_version=1."""

    store = MetricsStore(str(path), salt="unit-test-salt")
    store.log_event("boot")          # creates the schema
    store.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO sessions (session_id, started_at, person_code, "
                     "person_kind, status) VALUES (?,?,?,?,?)",
                     (LEGACY_ID, 1000.0, "S-abc123", "badge", "completed"))
        conn.execute("INSERT INTO events (ts, session_id, kind, detail) "
                     "VALUES (?,?,?,?)", (1001.0, LEGACY_ID, "attention_drop", ""))
        conn.execute("INSERT INTO cycles (session_id, ts, engagement_level, "
                     "engagement_score, attention) VALUES (?,?,?,?,?)",
                     (LEGACY_ID, 1002.0, 3, 0.7, 0.8))
        # A hand-made id that was never minted from an identity must be left alone.
        conn.execute("INSERT INTO sessions (session_id, started_at, person_code, "
                     "person_kind, status) VALUES (?,?,?,?,?)",
                     ("unit-test-session", 1000.0, "walkup", "walkup", "completed"))
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    finally:
        conn.close()


def test_opening_a_legacy_store_deidentifies_the_session_ids_on_disk(tmp_path):
    """The student id was in rows already written; the fix has to reach them.

    Every route hands the stored session id back beside that person's S-xxxxxx
    code, so a legacy row is a 1:1 map from the pseudonym to the human.
    """

    path = tmp_path / "legacy.db"
    _legacy_db(path)

    store = MetricsStore(str(path), salt="unit-test-salt")
    store.log_event("boot")
    store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for table in ("sessions", "events", "cycles"):
            ids = {r[0] for r in conn.execute("SELECT session_id FROM %s" % table)}
            assert LEGACY_ID not in ids, table
            assert "anna-001" not in " ".join(ids), table
            assert CLEAN_ID in ids, table
        # The cycles rows moved WITH their session: leaving them behind would
        # orphan the curve from every join-through-sessions aggregate.
        assert conn.execute(
            "SELECT COUNT(*) FROM cycles c JOIN sessions s "
            "ON s.session_id = c.session_id").fetchone()[0] == 1
        # A never-minted id is not a shape this may touch.
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE "
                            "session_id='unit-test-session'").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_migration_is_idempotent_and_does_not_rerun(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path)

    first = MetricsStore(str(path), salt="unit-test-salt")
    first.log_event("boot")
    first.close()
    # Re-opening the (already stamped) file must not rewrite anything...
    second = MetricsStore(str(path), salt="unit-test-salt")
    second.log_event("boot")
    second.close()

    conn = sqlite3.connect(path)
    try:
        assert {r[0] for r in conn.execute("SELECT session_id FROM sessions")} == \
            {CLEAN_ID, "unit-test-session"}
        # ...and running the pass by hand a third time is still a no-op, because
        # deidentify_session_id never matches its own output.
        assert migrate_session_ids(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    finally:
        conn.close()


def test_the_migration_leaves_a_colliding_row_alone(tmp_path):
    """Two ids that de-identify to the same value: keep both, never replace."""

    path = tmp_path / "collide.db"
    _legacy_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO sessions (session_id, started_at, person_code, "
                     "person_kind, status) VALUES (?,?,?,?,?)",
                     (CLEAN_ID, 999.0, "S-other", "badge", "completed"))
        conn.commit()
        assert migrate_session_ids(conn) >= 2       # the events + cycles rows
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT session_id, person_code FROM sessions")}
        assert rows[CLEAN_ID] == "S-other", "the pre-existing row was overwritten"
        assert LEGACY_ID in rows, "the colliding row was dropped instead of kept"
    finally:
        conn.close()


# --- pseudonymisation -----------------------------------------------------
def test_person_code_is_stable_pseudonymous_and_salted():
    a = person_code("anna-001", "salt-a")
    b = person_code("anna-001", "salt-a")
    c = person_code("anna-001", "salt-b")
    assert a == b, "a person code must be stable, or progression breaks every restart"
    assert a != c, "the code must depend on the salt"
    code, kind = a
    assert kind == "badge"
    assert code.startswith("S-") and len(code) == 8
    assert "anna" not in code


def test_anonymous_walkups_collapse_to_one_bucket():
    assert person_code("robot-lobby-01", "s") == ("walkup", "walkup")
    assert person_code("walkin", "s") == ("walkup", "walkup")
    assert person_code("", "s") == ("walkup", "walkup")


def test_no_name_or_email_ever_reaches_the_database(tmp_path):
    store = _store(tmp_path)
    store.session_started("anna-001-20260730-100000-abc", person_id="anna-001",
                          lang="ca", module_id="m1", module_title="Difficult news",
                          topic="Difficult news")
    store.record_cycle("anna-001-20260730-100000-abc", _signals())
    store.session_finished(
        "anna-001-20260730-100000-abc", avg_engagement=0.6, dominant_style="mirroring",
        results={"performance_scores": {"communication_clarity": 82},
                 "session_analysis": "The learner handled it calmly."},
    )
    store.close()

    blob = Path(tmp_path / "dash.db").read_bytes().decode("latin-1")
    assert "anna@" not in blob
    # The SSO student id travels inside the MINTED SESSION ID too, and a
    # session_id sitting next to a person_code is a map back from every
    # pseudonym to a person. Not one byte of it may be on disk.
    assert "anna-001" not in blob
    # The raw person_id is the SSO student id and must not be stored either.
    rows = _store(tmp_path).query(
        "SELECT session_id, person_code, person_kind FROM sessions")
    assert rows[0]["person_code"].startswith("S-")
    assert rows[0]["person_kind"] == "badge"
    assert rows[0]["session_id"] == "20260730-100000-abc"


def test_deidentify_session_id_strips_only_the_minted_identity_prefix():
    # Every generation of `_mint_session_id` output.
    assert deidentify_session_id(
        "anna-001-20260728-233738-77b605ba5b094769") == "20260728-233738-77b605ba5b094769"
    assert deidentify_session_id(
        "anna-001-20260728-233738-77b605ba5b094769-rqh-1c6d5649ced02fe5"
    ) == "20260728-233738-77b605ba5b094769-rqh-1c6d5649ced02fe5"
    assert deidentify_session_id("anna-001-20260611-145851") == "20260611-145851"
    assert deidentify_session_id(
        "robot-lobby-01-20260728-233738-abc") == "20260728-233738-abc"
    # Ids not minted from an identity are stored as they are.
    assert deidentify_session_id("sess-a") == "sess-a"
    assert deidentify_session_id("live-session") == "live-session"
    assert deidentify_session_id("") == ""
    # Idempotent: writers call it and then hand the result to log_event, which
    # calls it again.
    for sid in ("anna-001-20260728-233738-77b605ba5b094769",
                "anna-001-20260611-145851", "sess-a"):
        once = deidentify_session_id(sid)
        assert deidentify_session_id(once) == once


def test_every_minted_session_id_is_de_identified(tmp_path):
    """The one producer of session ids the robot runs, against the one consumer."""

    from emori_adapter.hostapp.server import _mint_session_id

    for student_id in ("anna-001", "robot-lobby-01", "user@example.org"):
        for qr in (False, True):
            minted = _mint_session_id(student_id, qr_bound=qr)
            stored = deidentify_session_id(minted)
            assert stored != minted
            safe = student_id.replace(".", "_").replace("@", "_")
            assert safe not in stored, (student_id, minted, stored)


def test_missing_salt_falls_back_deterministically(monkeypatch, caplog):
    import emori_adapter.hostapp.metrics_store as ms

    monkeypatch.delenv("EMORI_DASH_SALT", raising=False)
    monkeypatch.setenv("EMORI_ROBOT_ID", "lobby-01")
    monkeypatch.setattr(ms, "_salt_warned", False)
    with caplog.at_level("WARNING"):
        first = resolve_salt()
    assert "lobby-01" in first
    assert any("EMORI_DASH_SALT" in r.message for r in caplog.records)
    assert resolve_salt() == first, "the fallback must be stable across restarts"


# --- recovery state machine (engagement-recovery KPI) ----------------------
def test_recovery_tracker_pairs_a_drop_with_a_recovery():
    t = RecoveryTracker(window_s=90.0)
    assert t.observe("s", EngagementLevel.ENGAGED, 0.0) == []
    assert t.observe("s", EngagementLevel.DISENGAGING, 10.0) == ["attention_drop"]
    assert t.observe("s", EngagementLevel.ENGAGED, 40.0) == ["recovery_success"]


def test_a_sustained_dip_counts_once_not_once_per_cycle():
    t = RecoveryTracker()
    t.observe("s", EngagementLevel.ENGAGED, 0.0)
    kinds = [t.observe("s", EngagementLevel.DISENGAGED, ts) for ts in (2.0, 4.0, 6.0, 8.0)]
    assert kinds == [["attention_drop"], [], [], []]


def test_recovery_outside_the_window_is_not_a_success():
    t = RecoveryTracker(window_s=90.0)
    t.observe("s", EngagementLevel.ENGAGED, -2.0)
    t.observe("s", EngagementLevel.DISENGAGED, 0.0)
    # 91 s later the learner is engaged again, but not because of the nudge.
    assert t.observe("s", EngagementLevel.ENGAGED, 91.0) == []


def test_the_first_cycle_of_a_session_is_never_a_drop():
    """The headline recovery KPI must not be seeded with a fiction.

    ``EngagementTracker.update`` never returns UNKNOWN -- it commits to a level
    on cycle 1 -- and a learner sitting still listening to the scenario scores
    DISENGAGED there.  Counting that as an ``attention_drop`` added one
    fabricated pair per session to BOTH halves of ``recovery_pct`` (and to the
    ``recoveryAttempts``/``recoverySuccesses`` published to Orion).
    """

    t = RecoveryTracker()
    assert t.observe("s1", EngagementLevel.DISENGAGED, 0.0) == []
    # ...and no "recovery" manufactured by that phantom drop 20 s later.
    assert t.observe("s1", EngagementLevel.ENGAGED, 20.0) == []
    # A genuine drop AFTER a first observation still counts, in the same session.
    assert t.observe("s1", EngagementLevel.DISENGAGING, 30.0) == ["attention_drop"]
    assert t.observe("s1", EngagementLevel.ENGAGED, 40.0) == ["recovery_success"]


def test_sessions_are_independent_and_unknown_is_not_a_drop():
    t = RecoveryTracker()
    assert t.observe("s", EngagementLevel.UNKNOWN, 0.0) == []
    t.observe("a", EngagementLevel.ENGAGED, 0.5)
    assert t.observe("a", EngagementLevel.DISENGAGED, 1.0) == ["attention_drop"]
    # Another session's ENGAGED must not consume session a's pending drop.
    assert t.observe("b", EngagementLevel.ENGAGED, 2.0) == []
    assert t.observe("a", EngagementLevel.ENGAGED, 3.0) == ["recovery_success"]


def test_a_real_first_cycle_from_the_live_contract_is_not_a_drop(tmp_path):
    """End to end, on the envelope shape the live contract actually delivers."""

    from emori_adapter.sources.websocket import cycle_from_envelope
    from emori_adapter.translation import Translator

    cycle = cycle_from_envelope({
        "type": "pokamind_analysis_update",
        "timestamp": "2026-07-30T10:00:00Z",
        "body_language_metrics": {"engagement": 5, "posture": 10},
        "communication_styles": {"passive": 80, "assertive": 5},
    }, session_id="s1", person_id="anna-001")
    signals = Translator().translate(cycle)
    assert signals.engagement_level == EngagementLevel.DISENGAGED, (
        "a learner sitting still on cycle 1 is DISENGAGED, never UNKNOWN")

    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001", started_at=0.0)
    assert store.record_cycle("s1", signals, ts=2.0) == []
    assert store.recovery_counts("s1") == {"attempts": 0, "successes": 0}


def test_record_cycle_emits_recovery_events_and_counts_them(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001", started_at=90.0)
    store.record_cycle("s1", _signals(EngagementLevel.ENGAGED), ts=100.0)
    store.record_cycle("s1", _signals(EngagementLevel.DISENGAGING), ts=110.0)
    store.record_cycle("s1", _signals(EngagementLevel.ENGAGED), ts=130.0)
    assert store.recovery_counts("s1") == {"attempts": 1, "successes": 1}
    kinds = [r["kind"] for r in store.query(
        "SELECT kind FROM events WHERE session_id='s1' ORDER BY ts, rowid")]
    assert kinds == ["session_started", "attention_drop", "recovery_success"]


# --- session rows ---------------------------------------------------------
def test_cycle_row_carries_the_same_nine_metrics_as_fiware(tmp_path):
    from emori_adapter import fiware_schema as schema

    store = _store(tmp_path)
    signals = _signals()
    store.record_cycle("s1", signals, ts=5.0)
    row = store.query("SELECT * FROM cycles")[0]
    values = schema.metric_values(signals)
    assert row["engagement_level"] == values["engagementLevel"]
    assert row["engagement_score"] == pytest.approx(values["engagementScore"])
    assert row["attention"] == pytest.approx(values["attention"])
    assert row["engagement_trend"] == pytest.approx(values["engagementTrend"])
    assert row["dominant_style"] == values["dominantStyle"]


def test_session_lifecycle_and_results_capture(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001", lang="ca", module_id="m1",
                          module_title="Difficult news", topic="Difficult news",
                          started_at=1000.0)
    assert store.query("SELECT status FROM sessions")[0]["status"] == "active"

    scores = {"communication_clarity": 82, "engagement_presence": 71,
              "emotional_intelligence": 66, "reasoning_quality": 78,
              "technique_application": 60, "adaptability": 74}
    store.session_finished("s1", ended_at=1600.0, avg_engagement=0.63,
                           dominant_style="steady",
                           results={"performance_scores": scores,
                                    "session_analysis": "x" * 900})
    row = store.query("SELECT * FROM sessions")[0]
    assert row["status"] == "completed"
    assert row["duration_s"] == pytest.approx(600.0)
    assert row["avg_engagement"] == pytest.approx(0.63)
    assert row["dominant_style"] == "steady"
    assert json.loads(row["performance_scores"]) == scores
    assert len(row["analysis_excerpt"]) == 600
    person = store.query("SELECT * FROM persons")[0]
    assert person["kind"] == "badge" and person["first_seen"] == 1000.0


@pytest.mark.parametrize("payload, expected", [
    # Pokamind said so, either way: its word replaces the hostapp's assumption.
    ({"is_roleplay": False}, 0),
    ({"is_roleplay": True}, 1),
    ({"is_roleplay": "false"}, 0),
    # No opinion -> the row keeps what start_session wrote.  Never a fabricated 0.
    ({}, 1),
    ({"is_roleplay": None}, 1),
    ({"is_roleplay": "sometimes"}, 1),
    (None, 1),
])
def test_is_roleplay_is_stamped_from_the_completion_payload(tmp_path, payload, expected):
    """`start_session` has to guess (it runs at SSO time); the payload knows."""

    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001", is_roleplay=True, started_at=1000.0)
    store.session_finished("s1", ended_at=1600.0, results=payload)

    assert store.query("SELECT is_roleplay FROM sessions")[0]["is_roleplay"] == expected


def test_a_session_opened_as_not_roleplay_is_not_flipped_by_a_silent_payload(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001", is_roleplay=False, started_at=1000.0)
    store.session_finished("s1", ended_at=1600.0, results={"summary": "x"})

    assert store.query("SELECT is_roleplay FROM sessions")[0]["is_roleplay"] == 0


def test_heartbeat_row_is_written(tmp_path):
    store = _store(tmp_path)
    store.heartbeat(ok=True, session_active=True, ts=42.0)
    row = store.query("SELECT * FROM heartbeats")[0]
    assert row["ok"] == 1 and row["session_active"] == 1
    assert row["load"] >= 0.0 and row["mem_free_mb"] >= 0


# --- failure isolation ----------------------------------------------------
def test_an_unusable_database_never_raises(tmp_path):
    # A directory where the DB file should be: every open fails, forever.
    bad = tmp_path / "dash.db"
    bad.mkdir()
    store = MetricsStore(str(bad), salt="s")
    assert store.session_started("s1", person_id="p").startswith("S-")
    assert store.record_cycle("s1", _signals()) == []
    assert store.session_finished("s1") is False
    assert store.heartbeat() is False
    assert store.log_event("x") is False
    assert store.query("SELECT 1") == []
    assert store.recovery_counts("s1") == {"attempts": 0, "successes": 0}
    assert store.enabled is False


def test_a_broken_signal_object_is_swallowed(tmp_path):
    store = _store(tmp_path)
    assert store.record_cycle("s1", object()) == []
    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == 0


# --- thread safety --------------------------------------------------------
def test_concurrent_writers_from_many_threads(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001")
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                store.record_cycle("s1", _signals(), ts=float(n * 100 + i))
                store.log_event("tick", session_id="s1", detail=str(n))
                store.heartbeat(ts=float(n * 1000 + i))
        except BaseException as e:  # noqa: BLE001 - the point of the test
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == 160
    assert store.query(
        "SELECT COUNT(*) AS n FROM events WHERE kind='tick'")[0]["n"] == 160


def test_a_dashboard_read_never_parks_a_cycle_write(tmp_path):
    """WAL only keeps readers off the writer ACROSS CONNECTIONS.

    ``driver.on_analysis`` writes every cycle to the store, and
    ``summary(days=90)`` scans every cycle row in the window off an SD card.
    Sharing one lock between the two means a manager refreshing a phone stalls
    the robot mid-conversation.
    """

    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001")
    assert store._read_connect() is not store._connect()  # noqa: SLF001

    written = threading.Event()

    def write() -> None:
        store.record_cycle("s1", _signals(), ts=1.0)
        written.set()

    # Stand in for a long-running aggregate: hold the READ lock for its duration.
    with store._read_lock:  # noqa: SLF001
        worker = threading.Thread(target=write)
        worker.start()
        worker.join(timeout=10)
    assert written.is_set(), "a cycle write parked behind a dashboard read"
    # ...and the reader still sees it, on its own connection.
    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == 1


def test_a_dashboard_read_never_parks_another_read(tmp_path):
    """The second connection fixed writer-vs-reader.  READER-vs-reader remained.

    ``driver._finalize_locked`` calls ``recovery_counts`` -- a read -- while
    holding ``driver._lock``, the lock every cycle, gesture and watchdog tick
    needs.  With one lock around every read, that call waited out whatever
    90-day aggregate a manager's phone had in flight (measured 0.038 ms idle vs
    117.6 ms during a ``trend(90)`` refresh), and the driver was parked with it:
    a late "heard you" nod, a late behaviour motion, a late kiosk page for the
    next learner.
    """

    store = _store(tmp_path)
    store.session_started("s1", person_id="anna-001")
    store.log_event("attention_drop", session_id="s1")
    started, release = threading.Event(), threading.Event()

    def slow_aggregate() -> None:
        # A real long read on the SHARED handle: the SQL blocks inside sqlite
        # exactly as a 220k-row scan does, so the lock is held for its duration.
        conn = store._read_connect()  # noqa: SLF001
        conn.create_function("emori_block", 0,
                             lambda: (started.set(), release.wait(10.0))[0] or 1)
        store.query("SELECT emori_block() AS n")

    worker = threading.Thread(target=slow_aggregate, daemon=True)
    worker.start()
    assert started.wait(10.0), "the stand-in aggregate never started"
    try:
        counts = store.recovery_counts("s1")   # would block until release otherwise
    finally:
        release.set()
        worker.join(timeout=10)
    assert counts == {"attempts": 1, "successes": 0}


def test_the_driver_finalizes_while_a_dashboard_aggregate_is_in_flight(tmp_path):
    """The same invariant where it actually bites: through driver._lock."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="s1", person_id="anna-001"))
    driver.on_analysis(driver.stamp(json.loads(
        FIXTURES.joinpath("live_session.jsonl").read_text(
            encoding="utf-8").splitlines()[0])))
    started, release = threading.Event(), threading.Event()

    def slow_aggregate() -> None:
        conn = store._read_connect()  # noqa: SLF001
        conn.create_function("emori_block", 0,
                             lambda: (started.set(), release.wait(10.0))[0] or 1)
        store.query("SELECT emori_block() AS n")

    worker = threading.Thread(target=slow_aggregate, daemon=True)
    worker.start()
    assert started.wait(10.0)
    finished = threading.Event()

    def finalize() -> None:
        driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                          "results": {"session_analysis": "ok"}}))
        finished.set()

    ender = threading.Thread(target=finalize, daemon=True)
    ender.start()
    ender.join(timeout=5)
    parked = not finished.is_set()
    release.set()
    worker.join(timeout=10)
    ender.join(timeout=10)
    assert not parked, "the driver was parked behind a dashboard read"


# --- retention ------------------------------------------------------------
def test_prune_bounds_the_cycle_table_and_keeps_everything_else(tmp_path):
    """Nothing pruned the store, and every aggregate is O(cycles-in-window)."""


    store = _store(tmp_path)
    now = 1785000000.0
    store.session_started("old", person_id="anna-001", started_at=now - 400 * 86400)
    store.session_started("new", person_id="anna-001", started_at=now - 86400)
    for i in range(30):
        store.record_cycle("old", _signals(), ts=now - 400 * 86400 + i)
    for i in range(10):
        store.record_cycle("new", _signals(), ts=now - 86400 + i)
    store.log_event("attention_drop", session_id="old", ts=now - 400 * 86400)
    store.heartbeat(ts=now - 400 * 86400)

    assert store.prune(older_than_days=365, now=now) == 30
    assert store.query("SELECT session_id, COUNT(*) AS n FROM cycles "
                       "GROUP BY session_id") == [{"session_id": "new", "n": 10}]
    # The history a manager can list, the recovery KPI and the uptime series all
    # survive: only the raw 2-second samples are ever dropped.
    assert store.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 2
    assert store.query("SELECT COUNT(*) AS n FROM events "
                       "WHERE kind='attention_drop'")[0]["n"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM heartbeats")[0]["n"] == 1
    # Idempotent, and self-throttled to once a day.
    assert store.prune(older_than_days=365, now=now) == 0
    assert store.maybe_prune(now=now) >= 0
    assert store.maybe_prune(now=now + 60) == 0


def test_retention_is_env_tunable_and_zero_disables_it(tmp_path, monkeypatch):
    import emori_adapter.hostapp.metrics_store as ms

    monkeypatch.delenv("EMORI_DASH_RETENTION_DAYS", raising=False)
    assert ms.retention_days() == ms.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("EMORI_DASH_RETENTION_DAYS", "not-a-number")
    assert ms.retention_days() == ms.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("EMORI_DASH_RETENTION_DAYS", "0")
    assert ms.retention_days() == 0.0

    store = _store(tmp_path)
    store.session_started("old", person_id="anna-001", started_at=0.0)
    store.record_cycle("old", _signals(), ts=1.0)
    assert store.prune(now=1785000000.0) == 0, "0 must disable pruning entirely"
    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == 1


# --- driver wiring --------------------------------------------------------
def test_driver_records_a_whole_session(tmp_path):
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    class _Player:
        busy = False

        def submit(self, intents, *, drop_if_busy=False, guard=None):
            return True

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), player=_Player(), fiware_factory=None,
                        metrics=store)
    info = SessionInfo(session_id="anna-001-20260730-100000-abc", person_id="anna-001",
                       name="Anna Example", email="anna@example.org", lang="ca",
                       module_id="m1", topic="Difficult news", anonymous=False)
    driver.start_session(info)

    envelope = json.loads(FIXTURES.joinpath("live_session.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    for _ in range(3):
        driver.on_analysis(driver.stamp(envelope))
    driver.on_completed(driver.stamp({
        "type": "pokamind_session_completed",
        "results": {"performance_scores": {"adaptability": 55},
                    "session_analysis": "Steady throughout."},
    }))

    row = store.query("SELECT * FROM sessions")[0]
    # The minted id carries the SSO student id; the stored one must not.
    assert row["session_id"] == "20260730-100000-abc"
    assert row["status"] == "completed"
    assert row["lang"] == "ca" and row["module_title"] == "Difficult news"
    assert row["is_roleplay"] == 1
    assert json.loads(row["performance_scores"]) == {"adaptability": 55}
    assert row["analysis_excerpt"] == "Steady throughout."
    assert "Example" not in json.dumps(row, ensure_ascii=False)
    assert "anna-001" not in json.dumps(row, ensure_ascii=False)
    assert store.query(
        "SELECT COUNT(*) AS n FROM cycles WHERE session_id=?",
        (deidentify_session_id(info.session_id),))[0]["n"] == 3
    # The driver's own finalize path reads recovery counts back by the RAW id --
    # de-identification has to be symmetric or the FIWARE summary reads zeros.
    assert store.recovery_counts(info.session_id) == store.recovery_counts(
        "20260730-100000-abc")


def test_only_a_pokamind_completion_counts_as_completed(tmp_path):
    """The monthly-completion KPI is worthless if every ending is 'completed'."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)

    driver.start_session(SessionInfo(session_id="walked-off", person_id="anna-001"))
    driver.on_analysis(driver.stamp(json.loads(
        FIXTURES.joinpath("live_session.jsonl").read_text(
            encoding="utf-8").splitlines()[0])))
    # A new learner preempts the last one -> the old session was NOT completed.
    driver.start_session(SessionInfo(session_id="finished", person_id="bob-002"))
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                      "results": {"session_analysis": "ok"}}))

    status = {r["session_id"]: r["status"]
              for r in store.query("SELECT session_id, status FROM sessions")}
    assert status == {"walked-off": "abandoned", "finished": "completed"}


def test_watchdog_auto_end_is_recorded_as_an_event(tmp_path):
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="s1", person_id="anna-001"))
    ticker = WatchdogTicker(driver)
    ticker._request_pokamind_end("walkaway_ends")  # noqa: SLF001 - the unit under test

    kinds = [r["kind"] for r in store.query("SELECT kind FROM events")]
    assert "walkaway_ends" in kinds


@pytest.mark.parametrize("reason", ["walkaway_ends", "presence_ends",
                                    "watchdog_force_ends"])
def test_a_robot_forced_end_that_came_back_with_analysis_is_a_completion(tmp_path, reason):
    """The deployed UX is robot-driven auto-end-WITH-analysis.

    Nobody presses "finish": the watchdog asks Pokamind to complete, and Pokamind
    answers with the whole outcome -- scores, analysis, the summary the learner is
    then told.  Calling that 'abandoned' marked essentially every real session
    abandoned and zeroed completion_pct, the module table and the period compare.
    The session produced its outcome, so it completed; WHO ended it is the events
    row, which is still written and is what a walk-away trend must be read from.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="left-early", person_id="anna-001"))
    driver.on_analysis(driver.stamp(json.loads(
        FIXTURES.joinpath("live_session.jsonl").read_text(
            encoding="utf-8").splitlines()[0])))
    WatchdogTicker(driver)._request_pokamind_end(reason)  # noqa: SLF001
    # Pokamind answers the robot's own 'end' with a full completion payload.
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                      "results": {"session_analysis": "ok"}}))

    row = store.query("SELECT status, analysis_excerpt FROM sessions")[0]
    assert row["status"] == "completed", "an ending WITH analysis produced an outcome"
    assert row["analysis_excerpt"] == "ok"
    # The reason is still counted and still an events row -- the only evidence that
    # the robot, not the learner, decided the conversation was over.
    assert driver.counters["completed_after_auto_end"] == 1
    assert driver.counters.get("auto_end_without_results", 0) == 0
    assert {r["kind"] for r in store.query("SELECT kind FROM events")} >= {reason}


@pytest.mark.parametrize("payload, expected", [
    # What actually gets STORED: scores, or an analysis/summary excerpt.
    ({"performance_scores": {"adaptability": 55}}, True),
    ({"session_analysis": "Steady."}, True),
    ({"summary": "Done."}, True),
    # The shape a recorded completion envelope carries:
    # truthy, and session_finished stores NOTHING out of it.  Stamped 'completed',
    # it would be a row with NULL scores AND NULL excerpt -- a completion with
    # nothing to show, in the KPI numerator.
    ({"body_language_metrics": {"posture": "open"}, "duration_minutes": 4.2,
      "styles": ["mirroring"]}, False),
    ({"performance_scores": {}}, False),
    ({"session_analysis": "   "}, False),
    ({}, False),
    (None, False),
    # Not a mapping at all: nothing to extract, whatever its truthiness.
    (False, False), (0, False), ("results", False), (["scores"], False),
])
def test_results_are_recorded_only_when_something_is_actually_stored(payload, expected):
    """The status question is answered by the function that does the storing."""

    from emori_adapter.hostapp.metrics_store import _results_fields, results_recorded

    assert results_recorded(payload) is expected
    assert any(_results_fields(payload)) is expected


def test_an_envelope_the_store_keeps_nothing_from_is_not_a_completion(tmp_path):
    """A body-language-only completion.

    The watchdog force-ends, Pokamind answers with a truthy payload that carries no
    scores, no analysis and no summary, and the row it produces has NULL in both
    outcome columns.  Calling that 'completed' put an empty row in the numerator of
    the monthly-completion KPI, and `auto_end_without_results` stayed 0 so
    nothing even counted it.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="walked-off", person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end("walkaway_ends")  # noqa: SLF001
    driver.on_completed(driver.stamp({
        "type": "pokamind_session_completed",
        "results": {"body_language_metrics": {"posture": "open"},
                    "duration_minutes": 4.2, "styles": ["mirroring"]},
    }))

    row = store.query(
        "SELECT status, performance_scores, analysis_excerpt, end_reason "
        "FROM sessions")[0]
    assert row["status"] == "abandoned"
    assert (row["performance_scores"], row["analysis_excerpt"]) == (None, None)
    assert row["end_reason"] == "walkaway_ends"
    assert driver.counters["auto_end_without_results"] == 1


@pytest.mark.parametrize("results", [None, {}], ids=["no_results_key", "empty_results"])
def test_a_forced_end_with_nothing_to_show_for_it_is_abandoned(tmp_path, results):
    """The other half of the rule: an auto-end whose envelope carried no outcome.

    ``session_never_started`` is the canonical one -- the iframe never came up, the
    watchdog asked for an end, and the completion that came back has no analysis,
    no scores and no summary.  Nothing was produced, so nothing completed.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="never-started", person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end("session_never_started")  # noqa: SLF001
    envelope = {"type": "pokamind_session_completed"}
    if results is not None:
        envelope["results"] = results
    driver.on_completed(driver.stamp(envelope))

    row = store.query("SELECT status, performance_scores FROM sessions")[0]
    assert row["status"] == "abandoned"
    # Never a fabricated 0: an unscored session is NULL, not zero.
    assert row["performance_scores"] is None
    assert driver.counters["auto_end_without_results"] == 1
    assert {r["kind"] for r in store.query("SELECT kind FROM events")} >= {
        "session_never_started"}


def test_a_learner_driven_completion_needs_no_results_payload(tmp_path):
    """No auto-end pending -> Pokamind's word is enough, payload or not.

    The completion arrives without `results` whenever the emori_kiosk claim is not
    forwarding them; that is a missing analysis, not an abandoned conversation.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="learner-ended", person_id="anna-001"))
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed"}))

    assert store.query("SELECT status FROM sessions")[0]["status"] == "completed"
    assert driver.counters.get("completed_after_auto_end", 0) == 0


def test_the_local_fail_safe_without_any_envelope_is_still_abandoned(tmp_path):
    """No ``pokamind_session_completed`` at all: the dead-iframe finalize."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="dead-iframe", person_id="anna-001"))
    with driver._lock:  # noqa: SLF001 - the watchdog's own fail-safe call
        driver._finalize_locked(results=None)  # noqa: SLF001

    assert store.query("SELECT status FROM sessions")[0]["status"] == "abandoned"


def test_the_next_learner_after_an_auto_end_can_still_complete(tmp_path):
    """The auto-end reason belongs to ONE session; it must not leak forward.

    The first session is auto-ended and Pokamind sends nothing back (abandoned);
    the second is a plain completion with no payload either.  Only a leaked reason
    could make the second one abandoned too.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="left-early", person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end("walkaway_ends")  # noqa: SLF001
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed"}))
    driver.start_session(SessionInfo(session_id="finished", person_id="bob-002"))
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed"}))

    status = {r["session_id"]: r["status"]
              for r in store.query("SELECT session_id, status FROM sessions")}
    assert status == {"left-early": "abandoned", "finished": "completed"}


# --- who ended it, kept ON THE ROW ---------------------------------------------------
# `status` alone cannot answer it: the deployed UX is robot-driven, so a walk-away that
# Pokamind analysed is stored 'completed' exactly like a learner finishing.  The KPI
# tiles, the module table and the period compare all read `sessions` and none of them
# joins `events`, so a site of walk-aways rendered as a 100% completion rate.

@pytest.mark.parametrize("reason", ["walkaway_ends", "presence_ends",
                                    "watchdog_force_ends"])
def test_a_robot_driven_ending_is_named_on_the_session_row(tmp_path, reason):
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="left-early", person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end(reason)  # noqa: SLF001
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                      "results": {"session_analysis": "ok"}}))

    row = store.query("SELECT status, end_reason FROM sessions")[0]
    # Both facts, side by side: it produced an outcome AND the robot ended it.
    assert (row["status"], row["end_reason"]) == ("completed", reason)


def test_a_learner_driven_ending_leaves_the_reason_null(tmp_path):
    """NULL is the discriminator: no reason means nobody but the learner stopped."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="finished", person_id="anna-001"))
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                      "results": {"session_analysis": "ok"}}))

    assert store.query("SELECT end_reason FROM sessions")[0]["end_reason"] is None


def test_the_driver_names_its_own_local_endings_too(tmp_path):
    """A preempted session and one whose iframe never opened are robot endings."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="walked-off", person_id="anna-001"))
    driver.on_analysis(driver.stamp(json.loads(
        FIXTURES.joinpath("live_session.jsonl").read_text(
            encoding="utf-8").splitlines()[0])))
    # A new learner arrives while the last one's session is still open.
    driver.start_session(SessionInfo(session_id="never-opened", person_id="bob-002"))
    driver.abort_unstarted_session("frame_never_alive")

    reasons = {r["session_id"]: r["end_reason"]
               for r in store.query("SELECT session_id, end_reason FROM sessions")}
    assert reasons == {"walked-off": "preempted",
                       "never-opened": "session_start_failed"}


def test_the_pending_watchdog_reason_outranks_the_fallback_that_executed_it(tmp_path):
    """The dead-iframe fail-safe fires because of the walk-away; say the walk-away."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="left-early", person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end("walkaway_ends")  # noqa: SLF001
    with driver._lock:  # noqa: SLF001 - exactly the ticker's own fail-safe call
        driver._finalize_locked(results=None,  # noqa: SLF001
                                end_reason="end_local_fallback")

    row = store.query("SELECT status, end_reason FROM sessions")[0]
    assert (row["status"], row["end_reason"]) == ("abandoned", "walkaway_ends")


def test_a_dashboard_db_written_before_end_reason_existed_gains_the_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS cannot add a column to a deployed file.

    Without the ALTER, every ``session_finished`` on a robot that had been recording
    would name a column that is not there -- and this store swallows write failures,
    so the session row would simply never be closed at all.
    """

    from emori_adapter.hostapp.metrics_store import SCHEMA_VERSION, MetricsStore

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    # The v2 sessions table, verbatim minus the new column.
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_at REAL, "
                 "ended_at REAL, person_code TEXT, person_kind TEXT, lang TEXT, "
                 "module_id TEXT, module_title TEXT, topic TEXT, is_roleplay INTEGER, "
                 "duration_s REAL, avg_engagement REAL, dominant_style TEXT, "
                 "performance_scores TEXT, analysis_excerpt TEXT, status TEXT)")
    conn.execute("INSERT INTO sessions (session_id, started_at, status) "
                 "VALUES ('legacy', 1000.0, 'completed')")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    store = MetricsStore(str(path), salt="unit-test-salt")
    store.session_started("fresh", person_id="anna-001", started_at=2000.0)
    assert store.session_finished("fresh", ended_at=2600.0, status="abandoned",
                                  end_reason="walkaway_ends") is True

    rows = {r["session_id"]: r["end_reason"]
            for r in store.query("SELECT session_id, end_reason FROM sessions")}
    # The historical row keeps NULL: nothing ever recorded who ended it, and a
    # migration must not invent an answer.
    assert rows == {"legacy": None, "fresh": "walkaway_ends"}
    store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_cycles_arriving_with_no_session_are_not_stored(tmp_path):
    """A cycle with no session became a phantom endless 'live-session'.

    ``driver.stamp`` defaults the id to 'live-session' (and on a robot the
    envelope may carry Pokamind's own id), so every out-of-session cycle since
    boot was written under one id with no row in `sessions`.  Those rows are
    invisible to every aggregate that joins sessions -- but their
    attention_drop / recovery_success events were counted in the headline
    recovery KPI, and RecoveryTracker never forgot the id.
    """

    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    envelopes = [json.loads(line) for line in
                 FIXTURES.joinpath("live_session.jsonl").read_text(
                     encoding="utf-8").splitlines()
                 if '"pokamind_analysis_update"' in line]
    assert len(envelopes) > 5
    for envelope in envelopes:
        assert driver.on_analysis(driver.stamp(envelope))["ok"] is True

    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 0
    # Still translated, still driving the robot, still counted in /api/status --
    # only the store stays clean.
    assert driver.counters["cycles"] == len(envelopes)
    assert driver.counters["cycles_outside_session"] == len(envelopes)


def test_telemetry_never_precedes_the_motion_it_describes(tmp_path):
    """Control action first, telemetry after -- the rule the repo states elsewhere.

    ``record_cycle`` (an SD-card write: 0.14 ms typical, 0.67 ms p95 on an
    engagement flap, up to 47 ms when it lands on a WAL autocheckpoint) sat
    between the behaviour decision and the motion that expresses it, and
    ``start_session``'s three transactions sat in front of the greeting.
    """

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    order: list[str] = []

    class _Player:
        busy = False

        def submit(self, intents, *, drop_if_busy=False, guard=None):
            order.append("motion")
            return True

    class _Store(MetricsStore):
        def session_started(self, *a, **kw):
            order.append("telemetry")
            return super().session_started(*a, **kw)

        def record_cycle(self, *a, **kw):
            order.append("telemetry")
            return super().record_cycle(*a, **kw)

    store = _Store(str(tmp_path / "dash.db"), salt="unit-test-salt")
    driver = LiveDriver(DryRunRobotClient(), player=_Player(), fiware_factory=None,
                        metrics=store)
    driver.start_session(SessionInfo(session_id="s1", person_id="anna-001"))
    assert order and order[0] == "motion", "the greeting waited on the SD card"
    assert order[-1] == "telemetry"

    order.clear()
    driver.on_analysis(driver.stamp(json.loads(
        FIXTURES.joinpath("live_session.jsonl").read_text(
            encoding="utf-8").splitlines()[0])))
    assert order == ["motion", "telemetry"]


def test_fiware_factory_runs_off_the_event_loop(monkeypatch, tmp_path):
    """The 1 s broker probe must never execute on the asyncio loop."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    seen: list[str] = []

    def factory(info):
        seen.append(threading.current_thread().name)

        class _Null:
            enabled = False

            def ensure_session(self, topic=""):
                return None

        return _Null()

    driver = LiveDriver(DryRunRobotClient(), fiware_factory=factory,
                        metrics=_store(tmp_path))
    driver.start_session(SessionInfo(session_id="s1", person_id="p"))
    driver.shutdown(timeout_s=5.0)
    assert seen == ["fiware-session-start"]
    assert threading.main_thread().name not in seen


def test_missing_broker_degrades_to_a_null_client_and_an_event(monkeypatch, tmp_path):
    import emori_adapter.fiware as fiware_module
    from emori_adapter.fiware import NullFiwareClient
    from emori_adapter.hostapp import metrics_store as ms
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.driver import SessionInfo

    store = _store(tmp_path)
    ms.set_store(store)
    monkeypatch.setattr(fiware_module, "broker_reachable", lambda url, timeout=1.0: False)
    try:
        client = server._default_fiware_factory(  # noqa: SLF001 - the unit under test
            SessionInfo(session_id="s1", person_id="p"))
    finally:
        ms.set_store(None)
    assert isinstance(client, NullFiwareClient)
    # Asserted as a SET, not a one-element list. `_default_fiware_factory` writes
    # through the process singleton, and this test is the only place that
    # temporarily repoints it -- so a `fiware-session-start` daemon thread left
    # running by an earlier hostapp test (same code path, same event, no broker on
    # localhost either) can land a second identical row inside this window. What is
    # under test is that degrading to a null client records a fiware_skipped event
    # and nothing else; the row count is a scheduling artifact, not the behaviour.
    kinds = [r["kind"] for r in store.query("SELECT kind FROM events")]
    assert kinds and set(kinds) == {"fiware_skipped"}


def test_the_ngsi_ld_session_urn_carries_no_student_id(monkeypatch):
    """`urn:ngsi-ld:Session:<id>` is published to Orion and historized forever.

    The local store has de-identified the id since day one, but the FIWARE writer
    was handed the raw minted one -- so the same session was `S-xxxxxx` on the
    dashboard and `anna-001-...` in every QuantumLeap row and Grafana panel built
    on it.
    """

    import emori_adapter.fiware as fiware_module
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.driver import SessionInfo

    monkeypatch.setattr(fiware_module, "broker_reachable", lambda url, timeout=1.0: True)
    client = server._default_fiware_factory(  # noqa: SLF001 - the unit under test
        SessionInfo(session_id="anna-001-20260730-101500-deadbeefdeadbeef",
                    person_id="anna-001"))
    try:
        assert client.session_urn == \
            "urn:ngsi-ld:Session:20260730-101500-deadbeefdeadbeef"
        assert "anna-001" not in client.session_urn
        # ...and it still lines up with the local store row for the same session.
        assert client.session_id == deidentify_session_id(
            "anna-001-20260730-101500-deadbeefdeadbeef")
    finally:
        getattr(client, "_http", None) and client._http.close()  # noqa: SLF001


def test_the_ngsi_ld_person_urn_is_the_same_pseudonym_the_dashboard_uses(monkeypatch):
    """The session URN was scrubbed and the PERSON one was not.

    `urn:ngsi-ld:Person:anna-001` plus a `name` attribute reading "Anna Example"
    went to the broker beside the scrubbed session, and the Session's `learner`
    relationship pointed straight back at it -- so the scrub bought nothing, and
    an off-robot QuantumLeap/Grafana deployment held the SSO id and the real name
    of every staff member who ever practised.  `S-xxxxxx` is the code the local
    dashboard already shows for the same person, from the same stable salt, so
    the two feeds still agree about a returning learner.
    """

    import emori_adapter.fiware as fiware_module
    from emori_adapter import fiware_schema as schema
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.driver import SessionInfo

    monkeypatch.setenv("EMORI_DASH_SALT", "test-salt")
    monkeypatch.setattr(fiware_module, "broker_reachable", lambda url, timeout=1.0: True)
    client = server._default_fiware_factory(  # noqa: SLF001 - the unit under test
        SessionInfo(session_id="anna-001-20260730-101500-deadbeefdeadbeef",
                    person_id="anna-001", name="Anna Example"))
    try:
        code = person_code("anna-001")[0]
        assert client.person_urn == f"urn:ngsi-ld:Person:{code}"
        assert "anna-001" not in client.person_urn
        # A pseudonymous URN carrying the real name is not pseudonymous, so the
        # name is deliberately not forwarded either.
        assert client.person_name == ""
        entity = schema.person_entity(client.person_id, name=client.person_name)
        assert "name" not in entity
        # ...and the Session -> Person relationship still resolves.
        session = schema.session_entity(client.session_id, client.person_id)
        assert session["learner"]["object"] == entity["id"]
    finally:
        getattr(client, "_http", None) and client._http.close()  # noqa: SLF001


def test_a_missing_broker_is_named_by_host_never_by_its_configured_url(monkeypatch,
                                                                       tmp_path,
                                                                       caplog):
    """`http://user:pass@orion.example.local:1026` is a legal ORION_URL for a broker
    behind basic auth, and this line fires once per session on the majority fleet
    that has no broker at all -- into a journal that persists across boots, and
    into the dashboard's own events table."""

    import logging

    import emori_adapter.fiware as fiware_module
    from emori_adapter.hostapp import metrics_store as ms
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.driver import SessionInfo

    store = _store(tmp_path)
    ms.set_store(store)
    monkeypatch.setenv("ORION_URL", "http://ops:hunter2@orion.example.local:1026")
    monkeypatch.setattr(fiware_module, "broker_reachable", lambda url, timeout=1.0: False)
    caplog.set_level(logging.INFO, logger="emori.hostapp")
    try:
        server._default_fiware_factory(  # noqa: SLF001 - the unit under test
            SessionInfo(session_id="s1", person_id="p"))
        details = [r["detail"] for r in store.query(
            "SELECT detail FROM events WHERE kind='fiware_skipped'")]
    finally:
        ms.set_store(None)

    logged = "\n".join(caplog.messages)
    assert "orion.example.local:1026" in logged   # which host did we probe
    assert "hunter2" not in logged
    assert details and all("hunter2" not in d for d in details)


def test_an_unparseable_broker_url_degrades_rather_than_falling_back_to_the_raw_value():
    """The unparseable case is exactly where a stray `@` lives."""

    from emori_adapter.hostapp.server import _netloc_without_userinfo

    assert _netloc_without_userinfo("http://orion:1026") == "orion:1026"
    assert _netloc_without_userinfo("http://orion.example.local") == "orion.example.local"
    assert _netloc_without_userinfo("") == "?"
    assert _netloc_without_userinfo(None) == "?"
    assert _netloc_without_userinfo("http://u:p@host:not-a-port") == "?"
    assert _netloc_without_userinfo("http://") == "?"
    # `//` makes urlsplit call anything up to the first `/` a host, so a value
    # that is not a URL must be REJECTED and not echoed back into the journal --
    # the raw-string fallback this function promises not to have.
    assert _netloc_without_userinfo("not a url") == "?"
    assert _netloc_without_userinfo("Anna Example <anna@example.org>") == "?"


def test_a_scheme_less_broker_url_still_names_the_host():
    """`ORION_URL=orion.example.local:1026` is an ordinary thing to write in a .env.

    ``urlsplit`` reads that leading `orion.example.local` as a SCHEME -- it is a
    valid one -- so the naive parse finds no hostname and the operator was told
    their broker lived at "?", which is exactly the diagnostic this line exists
    to give them.  Credentials are still dropped on this path.
    """

    from emori_adapter.hostapp.server import _netloc_without_userinfo

    assert _netloc_without_userinfo("orion.example.local:1026") == "orion.example.local:1026"
    assert _netloc_without_userinfo("orion.example.local") == "orion.example.local"
    assert _netloc_without_userinfo("192.168.1.7:1026") == "192.168.1.7:1026"
    assert _netloc_without_userinfo("//orion:1026") == "orion:1026"
    assert _netloc_without_userinfo("ops:hunter2@orion") == "orion"
    # An IPv6 literal keeps its brackets, or "::1:1026" reads as neither a host
    # nor a port.
    assert _netloc_without_userinfo("http://[::1]:1026") == "[::1]:1026"


def test_a_head_turn_with_no_add_on_logs_nothing(tmp_path, monkeypatch):
    """The shipped configuration senses head turns and has no add-on: a turn is a no-op.

    Add-on usage is logged as ``ext_<mode>`` only by an add-on that acted on the turn, so
    an accidental head bump must never reach the dashboard's add-on tile.
    """

    from emori_adapter.hostapp import metrics_store as ms
    from emori_adapter.hostapp import server
    from emori_adapter.hostapp.extensions import HostExtension
    from emori_adapter.hostapp.gestures import GestureDirection
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    store = MetricsStore(str(tmp_path / "none.db"), salt="s")
    ms.set_store(store)
    try:
        app = server._build_app({  # noqa: SLF001 - the wiring under test
            "robot_client": DryRunRobotClient(), "fiware_factory": None,
            "idle": False, "watchdog": False, "gesture": True,
            "extension": HostExtension(), "tts": None, "stt": None,
        })
        app.state.gesture_ticker.on_direction(GestureDirection.LEFT)
        app.state.gesture_ticker.on_direction(GestureDirection.RIGHT)
    finally:
        ms.set_store(None)

    assert store.query("SELECT kind FROM events WHERE kind LIKE 'ext\\_%' ESCAPE '\\'") == []


# --- backfill -------------------------------------------------------------
def test_backfill_imports_outbox_and_recordings_idempotently(tmp_path, monkeypatch):
    monkeypatch.setenv("EMORI_DASH_SALT", "backfill-salt")
    from scripts.dashboard_backfill import import_outbox, import_recordings, parse_session_id

    session_id = "anna-001-20260603-100000"
    outbox = tmp_path / "outbox"
    recordings = tmp_path / "recordings"
    outbox.mkdir()
    recordings.mkdir()
    (outbox / f"{session_id}.json").write_text(json.dumps({
        "subject": "EMORI session report", "to": ["anna@example.org"],
        "avg_engagement": 0.5733, "dominant_style": "mirroring",
        "summary": "That's the end of our practice.",
    }), encoding="utf-8")
    shutil.copy(FIXTURES / "live_session.jsonl", recordings / f"{session_id}.jsonl")

    assert parse_session_id(session_id)[0] == "anna-001"

    store = MetricsStore(str(tmp_path / "dash.db"))
    assert import_outbox(store, outbox) == 1
    assert import_recordings(store, recordings) == 1

    row = store.query("SELECT * FROM sessions")[0]
    # person_id is recovered from the FILENAME and pseudonymised; the row itself
    # keeps only the de-identified id.
    assert row["session_id"] == "20260603-100000"
    assert "anna-001" not in row["session_id"]
    assert row["person_code"].startswith("S-")
    assert row["avg_engagement"] == pytest.approx(0.5733)
    assert row["dominant_style"] == "mirroring"
    cycles = store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"]
    assert cycles > 0
    # The report's recipient address must not have travelled into the DB.
    assert "example.org" not in Path(tmp_path / "dash.db").read_bytes().decode("latin-1")

    # Re-running must change nothing.
    import_outbox(store, outbox)
    import_recordings(store, recordings)
    assert store.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM cycles")[0]["n"] == cycles


def test_backfill_imports_the_scores_a_new_outbox_file_carries(tmp_path, monkeypatch):
    """The outbox is the only record of a pre-dashboard session's competency scores."""

    monkeypatch.setenv("EMORI_DASH_SALT", "backfill-salt")
    from scripts.dashboard_backfill import import_outbox

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    scores = {"communication_clarity": 80, "adaptability": 55}
    (outbox / "anna-001-20260603-100000.json").write_text(json.dumps({
        "subject": "EMORI session report", "to": ["anna@example.org"],
        "avg_engagement": 0.57, "dominant_style": "mirroring", "summary": "Done.",
        "performance_scores": scores, "is_roleplay": False,
    }), encoding="utf-8")

    store = MetricsStore(str(tmp_path / "dash.db"))
    assert import_outbox(store, outbox) == 1

    row = store.query("SELECT performance_scores, is_roleplay FROM sessions")[0]
    assert json.loads(row["performance_scores"]) == scores
    assert row["is_roleplay"] == 0
    # And the imported session is now visible to the progression table even though
    # no recording was replayed for it (no cycles).
    from emori_adapter.hostapp import dashboard

    assert len(dashboard.persons(store, 90, now=1780000000.0 + 90 * 86400)["persons"]) == 1

    # Idempotent: a second pass must not double the row or change it (INSERT OR
    # IGNORE; the return value counts attempted writes, as it always has).
    import_outbox(store, outbox)
    assert store.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1
    assert store.query("SELECT performance_scores FROM sessions")[0][
        "performance_scores"] == json.dumps(scores, ensure_ascii=False)


@pytest.mark.parametrize("doc, expect_scores, expect_roleplay", [
    # The OLD file shape (no outcome keys at all): imports exactly as it always did.
    ({"summary": "Done.", "avg_engagement": 0.5}, None, 1),
    # Present but empty / junk: never a fabricated {} or 0.
    ({"summary": "Done.", "performance_scores": {}}, None, 1),
    ({"summary": "Done.", "performance_scores": "not a dict",
      "is_roleplay": "maybe"}, None, 1),
])
def test_backfill_tolerates_outbox_files_written_before_the_scores_existed(
        tmp_path, monkeypatch, doc, expect_scores, expect_roleplay):
    monkeypatch.setenv("EMORI_DASH_SALT", "backfill-salt")
    from scripts.dashboard_backfill import import_outbox

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "anna-001-20260603-100000.json").write_text(json.dumps(doc),
                                                          encoding="utf-8")

    store = MetricsStore(str(tmp_path / "dash.db"))
    assert import_outbox(store, outbox) == 1

    row = store.query("SELECT performance_scores, is_roleplay FROM sessions")[0]
    assert row["performance_scores"] is expect_scores
    assert row["is_roleplay"] == expect_roleplay


def test_backfill_loads_dotenv_so_it_shares_the_hostapp_salt():
    """Without .env the backfill derives EVERY code from a different salt.

    ``emori_adapter.config`` runs ``load_dotenv()`` at import and hostapp/server.py
    imports it, so the hostapp process has /opt/emori/.env in os.environ.  The
    backfill's own import chain (metrics_store, sources.websocket, translation)
    does not, and ``resolve_salt``/``MetricsStore.__init__`` read os.environ
    directly -- so a robot backfilled without this would show every returning
    learner twice, once per salt.  Checked in a FRESH interpreter: in-process,
    another test has long since imported config.
    """

    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         " import scripts.dashboard_backfill;"
         " assert 'dotenv' in sys.modules, 'python-dotenv never imported';"
         " assert 'emori_adapter.config' in sys.modules, 'config never imported';"
         " print('ok')",
         str(root)],
        capture_output=True, text=True, cwd=str(root), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_backfill_parses_every_generation_of_session_id():
    from scripts.dashboard_backfill import parse_session_id

    for sid, expected in (
        ("anna-001-20260611-145851", "anna-001"),
        ("anna-001-20260728-233738-77b605ba5b094769", "anna-001"),
        ("anna-001-20260728-233738-77b605ba5b094769-rqh-1c6d5649ced02fe5", "anna-001"),
        ("robot-lobby-01-20260728-233738-abc", "robot-lobby-01"),
    ):
        person_id, started = parse_session_id(sid)
        assert person_id == expected
        assert started is not None
    assert parse_session_id("not-a-session-id") == ("not-a-session-id", None)


# --------------------------------------------------------------------------
# the 0.0 that was never measured, and the sessions nobody closed
# --------------------------------------------------------------------------
def test_a_session_with_no_cycles_stores_no_engagement_rather_than_zero(tmp_path):
    """``SessionTracker.finish`` does not average an empty list to 0.0.

    Written onto the row it is indistinguishable from a person who sat
    motionless, and it would make every Engagement column read 0%.
    """

    from emori_adapter.hostapp.driver import SessionInfo, SessionTracker

    tracker = SessionTracker()
    tracker.start(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    stats = tracker.finish()

    assert stats["avg_engagement"] is None
    # ...and the spoken summary cannot read a percentage out of nothing.
    assert "0 percent" not in stats["summary"]
    assert "percent" not in stats["summary"]

    store = _store(tmp_path)
    store.session_started("s1", person_id="p1", started_at=100.0)
    store.session_finished("s1", ended_at=200.0,
                           avg_engagement=stats["avg_engagement"], status="abandoned")
    row = store.query("SELECT avg_engagement FROM sessions WHERE session_id='s1'")[0]
    assert row["avg_engagement"] is None
    store.close()


def test_a_measured_zero_is_still_a_measurement(tmp_path):
    """The mirror image, and the reason the repair is guarded on `cycles`."""

    from emori_adapter.hostapp.driver import SessionInfo, SessionTracker

    tracker = SessionTracker()
    tracker.start(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    tracker.add(_signals(level=EngagementLevel.DISENGAGED, score=0.0))
    stats = tracker.finish()

    assert stats["avg_engagement"] == 0.0
    assert "0 percent" in stats["summary"]


def test_the_v4_migration_nulls_the_fabricated_zero_and_keeps_the_measured_one(tmp_path):
    """Run against rows already on disk that carry the fabricated zero."""

    from emori_adapter.hostapp.metrics_store import repair_zero_engagement

    store = _store(tmp_path)
    # `ghost` never produced a cycle; `real` produced one and genuinely averaged 0.
    for sid in ("ghost", "real"):
        store.session_started(sid, person_id="p1", started_at=100.0)
    store.record_cycle("real", _signals(level=EngagementLevel.DISENGAGED, score=0.0),
                       ts=101.0)
    for sid in ("ghost", "real"):
        store.session_finished(sid, ended_at=200.0, avg_engagement=0.0,
                               status="completed")
    store.close()

    conn = sqlite3.connect(tmp_path / "dash.db")
    try:
        assert repair_zero_engagement(conn) == 1
        conn.commit()
        rows = dict(conn.execute(
            "SELECT session_id, avg_engagement FROM sessions").fetchall())
    finally:
        conn.close()
    assert rows["ghost"] is None
    assert rows["real"] == 0.0


def test_reopening_the_store_repairs_an_old_file_once(tmp_path):
    """The migration is wired to the schema version, not to a manual call."""

    store = _store(tmp_path)
    store.session_started("ghost", person_id="p1", started_at=100.0)
    store.session_finished("ghost", ended_at=200.0, avg_engagement=0.0)
    store.close()
    # Pretend the file was written by a build that predates the repair.
    conn = sqlite3.connect(tmp_path / "dash.db")
    conn.execute("UPDATE sessions SET avg_engagement=0.0")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    reopened = _store(tmp_path)
    reopened.log_event("boot")          # forces _connect()
    row = reopened.query("SELECT avg_engagement FROM sessions")[0]
    reopened.close()
    assert row["avg_engagement"] is None


def test_a_session_left_active_by_a_dead_process_is_closed_at_startup(tmp_path):
    """A row a dead process left "in progress" stays that way for ever.

    It is counted in every session total and excluded from every completion
    rate, for ever, because nothing ever finalizes it.
    """

    store = _store(tmp_path)
    store.session_started("dead", person_id="p1", started_at=100.0)
    store.session_started("also-dead", person_id="p2", started_at=200.0)
    store.session_finished("also-dead", ended_at=300.0, status="completed")

    assert store.close_orphan_sessions(now=10_000.0) == 1

    rows = {r["session_id"]: r for r in store.query(
        "SELECT session_id, status, end_reason, ended_at, duration_s FROM sessions")}
    assert rows["dead"]["status"] == "abandoned"
    assert rows["dead"]["end_reason"] == "interrupted"
    # Nobody knows when it stopped, so nothing is invented: a fabricated duration
    # would land in avg_duration_s and a fabricated ended_at in the timeline.
    assert rows["dead"]["ended_at"] is None and rows["dead"]["duration_s"] is None
    # A session that finished normally is untouched.
    assert rows["also-dead"]["status"] == "completed"
    # Idempotent: a second boot finds nothing left to close.
    assert store.close_orphan_sessions(now=10_000.0) == 0
    store.close()


def test_closing_orphans_keeps_a_reason_the_robot_already_recorded(tmp_path):
    store = _store(tmp_path)
    store.session_started("s1", person_id="p1", started_at=100.0)
    store.query("SELECT 1")
    store._write([("UPDATE sessions SET end_reason='walkaway_ends' "
                   "WHERE session_id='s1'", ())], what="test")

    assert store.close_orphan_sessions(now=10_000.0) == 1
    row = store.query("SELECT status, end_reason FROM sessions")[0]
    assert (row["status"], row["end_reason"]) == ("abandoned", "walkaway_ends")
    store.close()


# --- a completion that arrives after the local finalize ------------------------------

def _robot_ended_and_finalized_locally(tmp_path, sid="late-1"):
    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo, WatchdogTicker
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id=sid, person_id="anna-001"))
    WatchdogTicker(driver)._request_pokamind_end("watchdog_force_ends")  # noqa: SLF001
    # The grace ran out before Pokamind answered: the ticker's local fail-safe.
    with driver._lock:  # noqa: SLF001
        driver._finalize_locked(results=None, end_reason="end_local_fallback")  # noqa: SLF001
    return driver, store


def _row(store, sid):
    return store.query("SELECT status, end_reason, performance_scores FROM sessions "
                       "WHERE session_id=?", (sid,))[0]


def test_a_late_completion_with_scores_is_salvaged_onto_the_closed_row(tmp_path):
    """A completion that carries performance_scores must not be thrown away because it
    landed after the 12 s grace had finalized the session locally."""

    driver, store = _robot_ended_and_finalized_locally(tmp_path)
    assert _row(store, "late-1")["status"] == "abandoned"

    stats = driver.on_completed(driver.stamp({
        "type": "pokamind_session_completed",
        "results": {"session_analysis": "ok", "performance_scores": {"empathy": 7}}}))

    row = _row(store, "late-1")
    assert row["status"] == "completed"
    assert row["end_reason"] == "watchdog_force_ends"      # who ended it is kept
    assert json.loads(row["performance_scores"]) == {"empathy": 7}
    assert stats.get("late") is True and stats["results"]["performance_scores"]
    assert driver.counters["late_completion_salvaged"] == 1
    assert driver.counters["completed"] == 0               # no second finalize ran
    # Salvaged ONCE: a duplicate completion is ignored, not re-applied.
    again = driver.on_completed(driver.stamp({
        "type": "pokamind_session_completed",
        "results": {"session_analysis": "again", "performance_scores": {"empathy": 1}}}))
    assert again == {"ignored": "no_active_session"}
    assert json.loads(_row(store, "late-1")["performance_scores"]) == {"empathy": 7}


def test_a_late_completion_without_results_or_too_late_is_ignored(tmp_path, monkeypatch):
    driver, store = _robot_ended_and_finalized_locally(tmp_path)
    empty = driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                              "results": {"duration_minutes": 2}}))
    assert empty == {"ignored": "no_active_session"}
    assert _row(store, "late-1")["status"] == "abandoned"

    driver2, store2 = _robot_ended_and_finalized_locally(tmp_path / "b", sid="late-2")
    # The window has already passed. Negative, not 0.0: on Windows time.monotonic() can
    # report 0.0 s elapsed between the finalize and this call, and `elapsed <= 0.0`
    # would then salvage the completion this test needs ignored.
    driver2._late_completion_s = -1.0  # noqa: SLF001
    stale = driver2.on_completed(driver2.stamp({
        "type": "pokamind_session_completed",
        "results": {"session_analysis": "ok", "performance_scores": {"empathy": 7}}}))
    assert stale == {"ignored": "no_active_session"}
    assert _row(store2, "late-2")["status"] == "abandoned"
    assert driver2.counters["late_completion_ignored"] == 1


def test_a_failed_pokamind_finalize_is_an_abandonment_not_a_completion(tmp_path):
    """The kiosk end chain reports {status:'failed'} with no results. Read as a
    result-less, reason-less envelope that was 'completed' - and fired the report."""

    from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
    from emori_adapter.robot.client import DryRunRobotClient

    store = _store(tmp_path)
    driver = LiveDriver(DryRunRobotClient(), fiware_factory=None, metrics=store)
    driver.start_session(SessionInfo(session_id="failed-1", person_id="anna-001"))
    driver.on_completed(driver.stamp({"type": "pokamind_session_completed",
                                      "status": "failed"}))
    row = _row(store, "failed-1")
    assert row["status"] == "abandoned" and row["end_reason"] == "pokamind_failed"
    assert driver.counters["end_pokamind_failed"] == 1


# --- how a session's sentences went -------------------------------------------
def test_plan_counts_are_one_row_per_family_and_outcome_under_the_stored_id(tmp_path):
    store = _store(tmp_path)
    minted = "anna-001-20260730-101500-deadbeefdeadbeef"
    assert store.record_plan_counts(minted, {("tension", "finished"): 3, ("", "skipped"): 1})
    rows = store.query("SELECT session_id, family, outcome, n FROM plan_counts "
                       "ORDER BY family DESC")
    assert rows == [
        {"session_id": "20260730-101500-deadbeefdeadbeef", "family": "tension",
         "outcome": "finished", "n": 3},
        {"session_id": "20260730-101500-deadbeefdeadbeef", "family": "", "outcome": "skipped",
         "n": 1}]
    store.close()


def test_a_second_write_for_a_session_adds_and_loses_nothing(tmp_path):
    store = _store(tmp_path)
    store.record_plan_counts("s", {("energy", "preempted"): 2})
    store.record_plan_counts("s", {("energy", "preempted"): 3, ("energy", "finished"): 1})
    rows = {(r["family"], r["outcome"]): r["n"]
            for r in store.query("SELECT family, outcome, n FROM plan_counts")}
    assert rows == {("energy", "preempted"): 5, ("energy", "finished"): 1}
    store.close()


def test_nothing_to_record_writes_nothing(tmp_path):
    store = _store(tmp_path)
    store._write = None          # would raise if it were reached
    assert store.record_plan_counts("s", {}) is True


def test_a_store_that_cannot_write_plan_counts_says_so_and_does_not_raise(tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    store = MetricsStore(str(blocked / "dash.db"), salt="unit-test-salt")
    assert store.record_plan_counts("s", {("energy", "finished"): 1}) is False


def test_a_dashboard_db_from_before_plan_counts_gains_the_table(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_at REAL, "
                 "ended_at REAL, person_code TEXT, person_kind TEXT, lang TEXT, "
                 "module_id TEXT, module_title TEXT, topic TEXT, is_roleplay INTEGER, "
                 "duration_s REAL, avg_engagement REAL, dominant_style TEXT, "
                 "performance_scores TEXT, analysis_excerpt TEXT, status TEXT, end_reason TEXT)")
    conn.execute("INSERT INTO sessions (session_id, started_at, status) "
                 "VALUES ('legacy', 1000.0, 'completed')")
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()

    store = MetricsStore(str(path), salt="unit-test-salt")
    assert store.record_plan_counts("fresh", {("levity", "finished"): 2}) is True
    assert store.query("SELECT session_id, n FROM plan_counts") == [
        {"session_id": "fresh", "n": 2}]
    assert store.query("SELECT session_id FROM sessions") == [{"session_id": "legacy"}]
    store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()
