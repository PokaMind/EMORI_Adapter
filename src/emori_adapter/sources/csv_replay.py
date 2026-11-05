"""Replay a recorded CSV export (``data.csv``) as a session source.

A recorded CSV export holds per-frame rows with columns:
``Timestamp, Prosody(JSON), <31 communication-style %>, <52 blendshape flags>``.
This replays those rows as :class:`AnalysisCycle`s, which is handy for
deterministic, offline testing of the translation/behaviour stack against real
captured data (no live session needed).

Body-language metrics / categorical fields live in a separate
``body_language.csv`` export; if a matching file is provided we merge it
row-by-row, otherwise those fields are left empty (the translator degrades
gracefully).
"""

from __future__ import annotations

import csv
import json
import time
from collections.abc import Iterator
from pathlib import Path

from emori_adapter.constants import COMM_STYLES
from emori_adapter.models import AnalysisCycle
from emori_adapter.sources.base import SessionSource

_STYLE_SET = set(COMM_STYLES)


class CsvReplaySource(SessionSource):
    def __init__(
        self,
        data_csv: str | Path,
        *,
        session_id: str = "replay-session",
        person_id: str = "learner_01",
        period_s: float = 2.0,
        realtime: bool = False,
    ) -> None:
        self.path = Path(data_csv)
        self.session_id = session_id
        self.person_id = person_id
        self.period_s = period_s
        self.realtime = realtime

    def cycles(self) -> Iterator[AnalysisCycle]:
        with self.path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            style_cols = [h for h in headers if h in _STYLE_SET]
            for seq, row in enumerate(reader):
                styles = {col: _to_float(row.get(col)) for col in style_cols}
                styles = {k: v for k, v in styles.items() if v is not None}
                prosody = _parse_prosody(row.get("Prosody") or row.get("prosody"))
                yield AnalysisCycle(
                    timestamp=row.get("Timestamp") or row.get("timestamp") or _fallback_stamp(seq, self.period_s),
                    session_id=self.session_id,
                    person_id=self.person_id,
                    seq=seq,
                    communication_styles=styles,
                    prosody=prosody,
                )
                if self.realtime:
                    time.sleep(self.period_s)


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_prosody(raw) -> dict[str, float]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if isinstance(data, dict):
        return {k: float(v) for k, v in data.items() if _is_num(v)}
    if isinstance(data, list):  # some exports store a bare list of 14 floats
        return {f"f{i}": float(v) for i, v in enumerate(data) if _is_num(v)}
    return {}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fallback_stamp(seq: int, period_s: float) -> str:
    secs = int(seq * period_s)
    hh, rem = divmod(secs, 3600)  # carry hours so >60 min sessions stay valid ISO-8601
    mm, ss = divmod(rem, 60)
    return f"2026-06-03T{10 + hh:02d}:{mm:02d}:{ss:02d}.000Z"
