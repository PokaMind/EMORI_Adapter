"""A per-sentence timing trace of the motion path.

Two questions about every spoken sentence can only be answered from the running robot:
how long after its voice did the head move, and did the plan chosen for it perform on
its own audio. This module records what is needed to answer them and nothing else.

Hot paths call :func:`emit`, which appends a tuple to a bounded deque: no formatting, no
I/O, no lock beyond the deque's own. One thread flushes once per second to
``artifacts/motion-trace/<session>.jsonl``. ``scripts/motion_sync_report.py`` joins the
records offline, so the robot never computes a statistic.

Records hold ids, times, families, slugs and intent reasons. They never hold the words
of a sentence, a cue direction or a person's identity, so a trace file can be read or
copied without handling learner data.

Off unless ``EMORI_MOTION_TRACE`` is set and :func:`start` has run. Everything here is
best-effort: a failed write costs records, and can never reach the motion path.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("emori.motion_trace")

DEFAULT_DIRECTORY = "artifacts/motion-trace"
SCHEMA_VERSION = 1

_CAPACITY = 4096
_FLUSH_S = 1.0
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_DIR_BYTES = 50 * 1024 * 1024
# The robot's card is nearly full and SQLite shares it: tracing stops well before it
# could be the reason a database write fails.
_MIN_FREE_BYTES = 500 * 1024 * 1024
_NO_SESSION = "no-session"
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def default_enabled() -> bool:
    """``EMORI_MOTION_TRACE``: off unless set, so tests and dev boxes write nothing."""

    raw = (os.environ.get("EMORI_MOTION_TRACE", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _filename(session: str) -> str:
    return _UNSAFE_FILENAME.sub("_", session)[:80] or _NO_SESSION


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


class MotionTrace:
    """The bounded buffer and its writer. See the module docstring."""

    def __init__(
        self,
        directory: str | os.PathLike = DEFAULT_DIRECTORY,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = _CAPACITY,
        max_file_bytes: int = _MAX_FILE_BYTES,
        max_dir_bytes: int = _MAX_DIR_BYTES,
        min_free_bytes: int = _MIN_FREE_BYTES,
        disk_free: Callable[[Path], int] = _disk_free,
    ) -> None:
        self._directory = Path(directory)
        self._clock = clock
        self._buf: deque = deque(maxlen=capacity)
        self._max_file_bytes = max_file_bytes
        self._max_dir_bytes = max_dir_bytes
        self._min_free_bytes = min_free_bytes
        self._disk_free = disk_free
        self._session = _NO_SESSION
        self._emitted = 0
        self._written = 0
        self._dropped = 0
        self._session_written = 0
        self._warned_low_disk = False
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- hot path -----------------------------------------------------------
    def emit(self, stream: str, fields: dict, *, at: Optional[float] = None) -> None:
        self._emitted += 1
        self._buf.append((self._clock() if at is None else at, stream, fields, self._session))

    # --- session ------------------------------------------------------------
    def set_session(self, session_id: Optional[str]) -> None:
        """Later records go to this session's file; ``None`` or empty closes it."""

        session = _filename(session_id) if session_id else _NO_SESSION
        if session == self._session:
            return
        previous = self._session
        self.flush()
        if previous != _NO_SESSION:
            # One line per session: the journal is volatile and is not the data store.
            log.warning("motion trace: session closed records=%d dropped=%d",
                        self._session_written, self.dropped)
        self._session_written = 0
        self._session = session

    # --- writer -------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="motion-trace", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_s))
        self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(_FLUSH_S):
            self.flush()

    def flush(self) -> int:
        """Write everything buffered; returns the record count written. Never raises."""

        with self._write_lock:
            records: list = []
            while True:
                try:
                    records.append(self._buf.popleft())
                except IndexError:
                    break
            if not records:
                return 0
            try:
                return self._write_locked(records)
            except Exception:  # noqa: BLE001 - tracing must never reach the motion path
                self._dropped += len(records)
                log.debug("motion trace write failed", exc_info=True)
                return 0

    def _write_locked(self, records: list) -> int:
        self._directory.mkdir(parents=True, exist_ok=True)
        if self._disk_free(self._directory) < self._min_free_bytes:
            self._dropped += len(records)
            if not self._warned_low_disk:
                self._warned_low_disk = True
                log.warning("motion trace: under %d MB free, records dropped",
                            self._min_free_bytes // (1024 * 1024))
            return 0
        by_session: dict[str, list[str]] = {}
        for t, stream, fields, session in records:
            line = {"t": round(t, 4), "s": stream, **fields}
            by_session.setdefault(session, []).append(
                json.dumps(line, separators=(",", ":"), ensure_ascii=False, default=str))
        written = 0
        for session, lines in by_session.items():
            path = self._directory / f"{session}.jsonl"
            existed = path.exists()
            if existed and path.stat().st_size >= self._max_file_bytes:
                self._dropped += len(lines)
                continue
            with path.open("a", encoding="utf-8") as fh:
                if not existed:
                    fh.write(json.dumps(self._meta(), separators=(",", ":")) + "\n")
                fh.write("\n".join(lines) + "\n")
            written += len(lines)
            if not existed:
                self._prune_locked(keep=path)
        self._written += written
        self._session_written += written
        return written

    def _meta(self) -> dict:
        # The two clocks together let a report place these monotonic times on a wall
        # clock, and the pid separates two process lifetimes in one directory.
        return {"s": "meta", "schema": SCHEMA_VERSION, "pid": os.getpid(),
                "t": round(self._clock(), 4), "wall": round(time.time(), 3)}

    def _prune_locked(self, *, keep: Path) -> None:
        """Delete the oldest files until the directory fits its budget."""

        files = [p for p in self._directory.glob("*.jsonl") if p != keep]
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files) + keep.stat().st_size
        for path in files:
            if total <= self._max_dir_bytes:
                break
            total -= path.stat().st_size
            path.unlink(missing_ok=True)

    # --- view ---------------------------------------------------------------
    @property
    def dropped(self) -> int:
        """Records lost to a full buffer, a full file, low disk or a failed write."""

        overflowed = self._emitted - self._written - self._dropped - len(self._buf)
        return self._dropped + max(0, overflowed)

    def snapshot(self) -> dict:
        return {"enabled": True, "records": self._written, "dropped": self.dropped,
                "session": self._session}


_trace: Optional[MotionTrace] = None
_plans = itertools.count(1)
_current = threading.local()


def start(directory: str | os.PathLike = DEFAULT_DIRECTORY) -> Optional[MotionTrace]:
    """Start tracing. Safe to call twice; never raises into the caller."""

    global _trace
    if _trace is not None:
        return _trace
    try:
        trace = MotionTrace(directory)
        trace.start()
    except Exception:  # noqa: BLE001 - tracing is optional
        log.warning("motion trace failed to start", exc_info=True)
        return None
    _trace = trace
    return trace


def stop() -> None:
    global _trace
    trace, _trace = _trace, None
    if trace is not None:
        trace.stop()


def enabled() -> bool:
    return _trace is not None


def emit(stream: str, *, at: Optional[float] = None, **fields) -> None:
    trace = _trace
    if trace is not None:
        trace.emit(stream, fields, at=at)


def set_session(session_id: Optional[str]) -> None:
    trace = _trace
    if trace is not None:
        trace.set_session(session_id)


def begin_plan(*, wanted: bool = False) -> int:
    """Name the plan this thread is about to submit; 0 when nothing wants the name.

    The id travels with the batch from the sentence to the player without the planner
    or the scheduler knowing about it, so the join in the report costs them nothing.
    ``wanted`` asks for an id with tracing off, for a caller with another use for it. The
    ids are the same ones either way, so whatever else reads them joins to the trace.
    """

    if _trace is None and not wanted:
        return 0
    plan = next(_plans)
    _current.plan = plan
    return plan


def unused_plan_id() -> int:
    """An id for a sentence with no batch, leaving this thread's current plan as it is."""

    return next(_plans)


def end_plan() -> None:
    _current.plan = 0


def current_plan() -> int:
    return getattr(_current, "plan", 0)


def set_tag(plan: int, index: int) -> None:
    """Name the move this thread is about to send, or clear the name with ``plan`` 0.

    The tag goes out with the command, so the robot's own record of when that move ran can
    be joined back to the plan. Empty while tracing is off, so nothing extra is sent.
    """

    _current.tag = f"{plan}.{index}" if (plan and _trace is not None) else ""


def current_tag() -> str:
    return getattr(_current, "tag", "")


def snapshot() -> dict:
    trace = _trace
    return trace.snapshot() if trace is not None else {"enabled": False}
