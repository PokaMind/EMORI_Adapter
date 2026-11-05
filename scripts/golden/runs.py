"""One recorded run on disk, and the two sides of a comparison.

    <root>/<label>/<scenario>/run-001/
        meta.json        scenario, mode, whether time was compressed, commit, monotonic start
        client.jsonl     every robot-client call: method, t_start, dur_s, args, ret or error
        motion.jsonl     the arbiter's submits, on a stack that has that trace
        marks.jsonl      what the harness did and when: posts, voice onsets, voice spans
        pending.jsonl    the page's 250 ms poll: t, cmd, tt_rev (only polls that got a verb)
        http.jsonl       route, status, content type, masked body or its hash, latency
        html/            the kiosk pages, masked
        events.jsonl     the relay in arrival order
        recordings/      session recordings, numbered by when each session opened
        db.json          dashboard rows of this run
        fiware.jsonl     calls the stub broker saw
        email/           the outbox of this run
        mic.jsonl        capture profile changes, in order
        speech.jsonl     what the robot was asked to say itself
        sys.jsonl        once a second, node-side state
        offline.jsonl    decisions of a replay that needs no host
        extra/<q>.jsonl  samples of a named quantity, from a recorder that is not part of this file

A stream a run does not have is ``None``, so the comparison can say "not applicable" where
a stack has no such witness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Optional

JSONL_STREAMS = ("client", "motion", "marks", "pending", "http", "events", "fiware", "mic",
                 "speech", "sys", "offline")


def read_jsonl(path: Path) -> list[dict]:
    """A JSON-lines file. A torn last line, from a process that died mid-write, is skipped."""

    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _read_tree(directory: Path) -> Optional[dict[str, str]]:
    if not directory.is_dir():
        return None
    return {str(path.relative_to(directory)).replace("\\", "/"):
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(directory.rglob("*")) if path.is_file()}


@dataclass
class Run:
    path: Path
    meta: dict = field(default_factory=dict)
    streams: dict[str, Optional[list[dict]]] = field(default_factory=dict)
    html: Optional[dict[str, str]] = None
    recordings: Optional[dict[str, str]] = None
    email: Optional[dict[str, str]] = None
    db: Optional[dict] = None
    extra: dict[str, list[float]] = field(default_factory=dict)

    def stream(self, name: str) -> Optional[list[dict]]:
        return self.streams.get(name)

    def records(self, name: str) -> list[dict]:
        return self.streams.get(name) or []

    @property
    def mode(self) -> str:
        return str(self.meta.get("mode", "sim"))

    @property
    def name(self) -> str:
        return self.path.name


def load_run(path: Path) -> Run:
    meta_path = path / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    streams: dict[str, Optional[list[dict]]] = {}
    for name in JSONL_STREAMS:
        file = path / f"{name}.jsonl"
        streams[name] = read_jsonl(file) if file.exists() else None
    db_path = path / "db.json"
    extra = {}
    extra_dir = path / "extra"
    if extra_dir.is_dir():
        for file in sorted(extra_dir.glob("*.jsonl")):
            extra[file.stem] = [float(r["value"]) for r in read_jsonl(file) if "value" in r]
    return Run(path=path, meta=meta, streams=streams, html=_read_tree(path / "html"),
               recordings=_read_tree(path / "recordings"), email=_read_tree(path / "email"),
               db=json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else None,
               extra=extra)


def load_side(side: Path, scenario: str) -> list[Run]:
    directory = side / scenario
    if not directory.is_dir():
        return []
    return [load_run(path) for path in sorted(directory.glob("run-*")) if path.is_dir()]


def scenarios_in(side: Path) -> list[str]:
    if not side.is_dir():
        return []
    return sorted(path.name for path in side.iterdir()
                  if path.is_dir() and any(path.glob("run-*")))


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=_plain)
                         + "\n")


def _plain(value: Any) -> str:
    return str(value)
