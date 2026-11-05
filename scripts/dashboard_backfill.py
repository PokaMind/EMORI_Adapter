"""Populate the dashboard DB from artifacts the robot already wrote.

For robots with sessions recorded before the dashboard existed. Every session
leaves two files behind: an outbox report (``artifacts/outbox/<session>.json``,
the exact would-be email) and a JSONL envelope recording
(``artifacts/recordings/<session>.jsonl``, the replay fixture). Together they
carry enough to reconstruct the session row and its per-cycle series, so such a
robot does not open its first dashboard empty.

    python scripts/dashboard_backfill.py                    # default artifacts/
    python scripts/dashboard_backfill.py --db /tmp/d.db --outbox out --recordings rec
    python scripts/dashboard_backfill.py --dry-run

Idempotent: sessions/persons go in with INSERT OR IGNORE, and a session whose
cycles are already present is skipped rather than duplicated (the cycles table is
deliberately not uniquely keyed -- two live cycles CAN share a timestamp, and
silently dropping a real one would be worse than a re-import you can control).

Stdlib + emori_adapter only; runs on the robot venv.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The hostapp gets the robot's .env for free: emori_adapter.config calls
# load_dotenv() at import and hostapp/server.py imports it.  THIS script's import
# chain does not, and metrics_store reads os.environ directly -- so without this
# line `python scripts/dashboard_backfill.py` on the robot sees neither
# EMORI_DASH_SALT nor EMORI_DASH_DB nor EMORI_ROBOT_ID, derives every person code
# from a different salt than the live sessions did, and writes them to a
# different file.  Badge learner `anna` would then appear as two unrelated people
# either side of the backfill -- exactly what .env.example warns about.
import emori_adapter.config  # noqa: E402,F401 - imported for its load_dotenv()
from emori_adapter.hostapp.metrics_store import (  # noqa: E402
    MetricsStore,
    deidentify_session_id,
    person_code,
    roleplay_flag,
    scores_json,
)

# `<student_id>-<YYYYmmdd>-<HHMMSS>` plus whatever the later id generations append
# (a 16-hex nonce, and for QR-bound sessions an `-rqh-<32hex>` namespace).
_SESSION_ID_RE = re.compile(r"^(?P<pid>.+)-(?P<d>\d{8})-(?P<t>\d{6})(?:-.*)?$")
_ANALYSIS_TYPE = "pokamind_analysis_update"
_COMPLETED_TYPE = "pokamind_session_completed"


def parse_session_id(session_id: str) -> tuple[str, Optional[float]]:
    """``(person_id, started_at epoch)`` from a minted session id."""

    m = _SESSION_ID_RE.match(session_id)
    if m is None:
        return session_id, None
    try:
        stamp = time.strptime(m.group("d") + m.group("t"), "%Y%m%d%H%M%S")
        started = float(calendar.timegm(stamp))  # ids are minted in UTC
    except ValueError:
        started = None
    return m.group("pid"), started


def _iso_to_epoch(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        import datetime as dt

        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def import_outbox(store: MetricsStore, directory: Path, *, dry_run: bool = False) -> int:
    """Session rows from the outbox reports. Returns how many were inserted."""

    inserted = 0
    for path in sorted(directory.glob("*.json")):
        session_id = path.stem
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        person_id, started = parse_session_id(session_id)
        code, kind = person_code(person_id, store.salt)
        if dry_run:
            inserted += 1
            continue
        if started is not None:
            store.upsert_person(code, kind, started)
        # The outcome fields, when the report carried them (newer results_email
        # versions write them).  An older file simply has neither key, and then
        # this imports exactly what it always did: NULL scores, and the hostapp's
        # is_roleplay assumption of 1.  Never a 0 or a `{}` invented here -- a
        # backfilled session with no scores must read "not scored" in the
        # dashboard, not "scored zero".
        scores = scores_json(doc.get("performance_scores"))
        roleplay = roleplay_flag(doc.get("is_roleplay"))
        # `_write` is the raw seam, so the de-identification the public writers
        # apply has to be done here by hand -- the person_id was already taken
        # out of the FILENAME above, and the row must not carry it too.
        ok = store._write(  # noqa: SLF001 - the backfill is part of the store's own package
            [("INSERT OR IGNORE INTO sessions (session_id, started_at, ended_at, "
              "person_code, person_kind, lang, module_id, module_title, topic, "
              "is_roleplay, duration_s, avg_engagement, dominant_style, "
              "performance_scores, analysis_excerpt, status) "
              "VALUES (?,?,NULL,?,?,'','','','',?,NULL,?,?,?,?,'completed')",
              (deidentify_session_id(session_id), started, code, kind,
               1 if roleplay is None else roleplay,
               _as_float(doc.get("avg_engagement")),
               str(doc.get("dominant_style") or ""),
               scores,
               (doc.get("summary") or "")[:600] or None))],
            what="backfill_session",
        )
        if ok:
            inserted += 1
    return inserted


def _as_float(value: object) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def import_recordings(store: MetricsStore, directory: Path, *,
                      dry_run: bool = False) -> int:
    """Per-cycle rows by replaying each recording through the LIVE translator.

    Not a re-derivation: it is the same Translator the driver runs, fed the same
    envelopes in the same order, so a backfilled series and a live one mean
    exactly the same thing. A recording whose envelopes no longer parse is
    skipped whole rather than half-imported.
    """

    from emori_adapter.sources.websocket import cycle_from_envelope
    from emori_adapter.translation import Translator

    imported = 0
    for path in sorted(directory.glob("*.jsonl")):
        session_id = path.stem
        # `query` is raw SQL over the STORED ids, so the de-identified form is
        # what the cycles carry -- comparing the filename here would never match
        # and the importer would duplicate every session on a re-run.
        if store.query("SELECT 1 FROM cycles WHERE session_id=? LIMIT 1",
                       (deidentify_session_id(session_id),)):
            continue
        person_id, started = parse_session_id(session_id)
        translator = Translator()
        rows: list[tuple[float, object]] = []
        completed: Optional[dict] = None
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        envelope = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(envelope, dict):
                        continue
                    kind = envelope.get("type")
                    if kind == _COMPLETED_TYPE:
                        completed = envelope
                        continue
                    if kind != _ANALYSIS_TYPE:
                        continue
                    try:
                        cycle = cycle_from_envelope(envelope, session_id=session_id,
                                                    person_id=person_id)
                        signals = translator.translate(cycle)
                    except Exception:  # noqa: BLE001 - one bad envelope, keep going
                        continue
                    ts = _iso_to_epoch(envelope.get("timestamp"))
                    if ts is None:
                        ts = (started or 0.0) + 2.0 * len(rows)
                    rows.append((ts, signals))
        except OSError:
            continue
        if not rows or dry_run:
            imported += 1 if rows else 0
            continue
        # The session row may not exist (a recording with no outbox report) --
        # create a minimal one so the cycles are never orphaned in the UI.
        code, kind = person_code(person_id, store.salt)
        store.upsert_person(code, kind, started or rows[0][0])
        store._write(  # noqa: SLF001
            [("INSERT OR IGNORE INTO sessions (session_id, started_at, person_code, "
              "person_kind, lang, module_id, module_title, topic, is_roleplay, status) "
              "VALUES (?,?,?,?,'','','','',1,'completed')",
              (deidentify_session_id(session_id), started or rows[0][0], code, kind))],
            what="backfill_session_stub",
        )
        for ts, signals in rows:
            store.record_cycle(session_id, signals, ts=ts)
        # A backfilled session's end is the last cycle we have evidence for.
        store.session_finished(
            session_id, ended_at=rows[-1][0],
            results=(completed or {}).get("results"),
            status="completed",
        )
        imported += 1
    return imported


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--db", default=os.environ.get("EMORI_DASH_DB", "") or None)
    ap.add_argument("--outbox", default=os.environ.get("EMORI_OUTBOX", "artifacts/outbox"))
    ap.add_argument("--recordings", default="artifacts/recordings")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    store = MetricsStore(args.db)
    sessions = import_outbox(store, Path(args.outbox), dry_run=args.dry_run)
    recordings = import_recordings(store, Path(args.recordings), dry_run=args.dry_run)
    print(f"sessions from outbox : {sessions}")
    print(f"recordings imported  : {recordings}")
    print(f"database             : {store.path}{' (dry run)' if args.dry_run else ''}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
