"""Record one run of a scenario: act as the page, and write down everything each witness saw.

The harness goes through the same routes the kiosk uses, polling ``/api/pending-cmd`` every
250 ms. On a robot the kiosk must be stopped first, or it would drain the same verbs.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

from .normalise import Normaliser, mask_document, mask_html
from .runs import write_jsonl

POLL_S = 0.25
_REQUEST_TIMEOUT_S = 30.0
_RELAY_WAIT_S = 5.0
# Long enough for the dashboard writer to finish what a finalize handed it.
_SETTLE_S = 0.6
DEFAULT_DB = "dashboard.db"
_SESSION_COLUMNS = ("session_id", "status", "end_reason", "lang", "module_id", "topic",
                    "is_roleplay", "avg_engagement", "dominant_style", "performance_scores")


class Refused(Exception):
    """A scenario cannot run this way, and says why."""


class ScenarioFailed(Exception):
    """What the scenario waited for never happened."""


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._handle = path.open("a", encoding="utf-8", newline="\n")

    def write(self, record: dict) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                              default=str) + "\n")
                self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


class PagePoller:
    """The session page's poll. A ``home`` ends it, because the page navigates away."""

    def __init__(self, base_url: str, writer: _JsonlWriter, verbs: str = "") -> None:
        self._http = httpx.Client(base_url=base_url, timeout=_REQUEST_TIMEOUT_S)
        self._writer = writer
        self._params = {"verbs": verbs} if verbs else {}
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self.verbs: list[str] = []
        self._rev: Any = None
        self._thread = threading.Thread(target=self._loop, name="page-poll", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                body = self._http.get("/api/pending-cmd", params=self._params).json()
            except (httpx.HTTPError, ValueError):
                continue    # a host restarting costs polls, as it costs the real page
            cmd = body.get("cmd")
            if cmd:
                self._writer.write({"t": time.monotonic(), "cmd": cmd, "tt_rev": body.get("tt_rev")})
                with self._cv:
                    self.verbs.append(cmd)
                    self._cv.notify_all()
                if cmd == "home":
                    return

    def wait_for(self, verb: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while verb not in self.verbs:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.2))
            return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._http.close()


class RelayTap:
    """``/ws/relay`` in arrival order. Listens only: a sent message would be rebroadcast."""

    def __init__(self, url: str, writer: _JsonlWriter) -> None:
        self._url = url
        self._writer = writer
        self._normaliser = Normaliser()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="relay-tap", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self._url, open_timeout=3.0, max_size=None) as socket:
                    while not self._stop.is_set():
                        try:
                            message = socket.recv(timeout=0.2)
                        except TimeoutError:
                            continue
                        try:
                            envelope = json.loads(message)
                        except ValueError:
                            continue
                        if isinstance(envelope, dict):
                            self._writer.write({"t": time.monotonic(),
                                                **self._normaliser.value(envelope)})
            except Exception:  # noqa: BLE001 - a restarting host costs events, not the run
                self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)


class _PostClient:
    """What the motion-sync scheduler posts through, each post also going in ``http.jsonl``."""

    def __init__(self, context: "Context") -> None:
        self._context = context

    def post(self, path: str, body: dict) -> tuple[int, Any]:
        response = self._context.post(path, body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    def close(self) -> None:
        return


class Context:
    """What a scenario works with. It holds no knowledge of which stack is underneath."""

    def __init__(self, *, host: Any, run_dir: Path, fast: bool = False, lang: str = "en",
                 badge: str = "", second_badge: str = "", track: Any = None) -> None:
        self.host = host
        self.run_dir = run_dir
        self.fast = fast
        self.lang = lang
        self.badge = badge
        self.second_badge = second_badge
        self.mode = host.mode
        self.track = track
        self.t0 = time.monotonic()
        self._http = httpx.Client(base_url=host.base_url, timeout=_REQUEST_TIMEOUT_S)
        self._log = _JsonlWriter(run_dir / "http.jsonl")
        self._marks = _JsonlWriter(run_dir / "marks.jsonl")
        self._pending = _JsonlWriter(run_dir / "pending.jsonl")
        self._events = _JsonlWriter(run_dir / "events.jsonl")
        self._normaliser = Normaliser()
        self._mask_lock = threading.Lock()
        self._poller: Optional[PagePoller] = None
        self._relay: Optional[RelayTap] = None

    # --- time ---------------------------------------------------------------------------
    def hold(self, seconds: float, *, fast: Optional[float] = None) -> None:
        """Wait. A compressed run takes the shorter figure the scenario gives."""

        time.sleep(fast if (self.fast and fast is not None) else seconds)

    def wait_for(self, what: str, check: Callable[[], bool], timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while not check():
            if time.monotonic() >= deadline:
                raise ScenarioFailed(f"{what} did not happen within {timeout_s:.0f} s")
            time.sleep(0.05)

    def mark(self, name: str, *, t: Optional[float] = None, **fields: Any) -> None:
        self._marks.write({"name": name, "t": time.monotonic() if t is None else t, **fields})

    # --- talking to the host ------------------------------------------------------------
    def request(self, method: str, target: str, *, json_body: Any = None, content: Any = None,
                headers: Optional[dict] = None, page: Optional[str] = None,
                params: Optional[dict] = None) -> httpx.Response:
        started = time.monotonic()
        response = self._http.request(method, target, json=json_body, content=content,
                                      headers=headers, params=params)
        latency = time.monotonic() - started
        kind = response.headers.get("content-type", "").split(";")[0].strip()
        record: dict[str, Any] = {"t": started, "route": urlsplit(target).path, "method": method,
                                  "status": response.status_code, "content_type": kind,
                                  "latency_s": latency}
        with self._mask_lock:
            if page is not None and kind == "text/html":
                directory = self.run_dir / "html"
                directory.mkdir(exist_ok=True)
                (directory / f"{page}.html").write_text(mask_html(response.text, self._normaliser),
                                                        encoding="utf-8", newline="\n")
                record["body"] = f"<html:{page}>"
            elif kind == "application/json":
                try:
                    record["body"] = self._normaliser.value(response.json())
                except ValueError:
                    record["body"] = self._normaliser.text(response.text)
            elif kind.startswith("text/"):
                record["body"] = self._normaliser.text(response.text)
            else:
                import hashlib

                record["sha256"] = hashlib.sha256(response.content).hexdigest()
                record["bytes"] = len(response.content)
        self._log.write(record)
        return response

    def get(self, target: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", target, **kwargs)

    def post(self, target: str, body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.request("POST", target, json_body=body if body is not None else {}, **kwargs)

    def page(self, name: str, target: str) -> httpx.Response:
        return self.get(target, page=name)

    def cycle(self, envelope: dict, *, answers: bool = False) -> httpx.Response:
        """One envelope to ``/api/cycle``. ``answers``: the robot moves on it without a voice."""

        if answers:
            self.mark("envelope_post")
        return self.post("/api/cycle", envelope)

    def status(self) -> dict:
        """``/api/status``, outside ``http.jsonl``: how often it is read depends on timing."""

        return self._http.get("/api/status").json()

    def sample(self, quantity: str, value: float) -> None:
        directory = self.run_dir / "extra"
        directory.mkdir(exist_ok=True)
        with (directory / f"{quantity}.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"value": value}) + "\n")

    # --- the page -----------------------------------------------------------------------
    def start_poll(self, verbs: str = "") -> PagePoller:
        self.stop_poll()
        self._poller = PagePoller(self.host.base_url, self._pending, verbs)
        self._poller.start()
        return self._poller

    def stop_poll(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller = None

    def wait_verb(self, verb: str, timeout_s: float) -> None:
        if self._poller is None:
            raise ScenarioFailed(f"waiting for {verb} with no page polling")
        if not self._poller.wait_for(verb, timeout_s):
            raise ScenarioFailed(f"the verb {verb!r} never came within {timeout_s:.0f} s; "
                                 f"saw {self._poller.verbs}")

    def post_client(self) -> _PostClient:
        return _PostClient(self)

    def speak(self, cases: tuple[str, ...], *, continuing: tuple[str, ...] = ()) -> None:
        """Say these sentences of the fixture, with their events posted around the voice.

        ``continuing``: those of them after which the reply goes on, so no end of turn follows.
        """

        if self.track is None:
            raise Refused("this run has no voice")
        failed = self.track.play(cases, self.lang, self.post_client, self.mark,
                                 continuing=continuing)
        if failed:
            raise ScenarioFailed(f"{failed} posts of the voice track failed")

    # --- the relay ----------------------------------------------------------------------
    def start_relay(self) -> None:
        url = self.host.base_url.replace("http", "ws", 1) + "/ws/relay"
        self._relay = RelayTap(url, self._events)
        self._relay.start()
        deadline = time.monotonic() + _RELAY_WAIT_S
        while time.monotonic() < deadline:
            try:
                if self.status().get("relay_clients", 0) >= 1:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)

    def close(self) -> None:
        self.stop_poll()
        if self._relay is not None:
            self._relay.stop()
        self._http.close()
        for writer in (self._log, self._marks, self._pending, self._events):
            writer.close()


# ---------------------------------------------------------------------------------------
# what a host leaves on disk
# ---------------------------------------------------------------------------------------


def _since(directory: Path, wall0: float, pattern: str = "*") -> list[Path]:
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob(pattern)
             if path.is_file() and path.stat().st_mtime >= wall0 - 1.0]
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def dump_db(database: Path, wall0: float) -> Optional[dict]:
    """The dashboard rows of this run: sessions, how many cycles each, and event kinds in order."""

    if not database.exists():
        return None
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        columns = ", ".join(_SESSION_COLUMNS)
        sessions = [dict(row) for row in connection.execute(
            f"SELECT {columns} FROM sessions WHERE started_at >= ? ORDER BY started_at, rowid",  # noqa: S608
            (wall0 - 1.0,))]
        for row in sessions:
            count = connection.execute("SELECT COUNT(*) FROM cycles WHERE session_id = ?",
                                       (row["session_id"],)).fetchone()[0]
            row["cycles"] = count
        events = [{"session_id": row["session_id"], "kind": row["kind"]}
                  for row in connection.execute(
                      "SELECT session_id, kind FROM events WHERE ts >= ? ORDER BY ts, rowid",
                      (wall0 - 1.0,))]
    finally:
        connection.close()
    return {"sessions": sessions, "events": events}


def collect_artifacts(artifacts: Path, run_dir: Path, wall0: float, *,
                      database: Optional[Path] = None) -> None:
    """Copy what this run left behind, masked, numbered in the order it was written."""

    recordings = _since(artifacts / "recordings", wall0, "*.jsonl")
    if recordings:
        target = run_dir / "recordings"
        target.mkdir(exist_ok=True)
        for index, path in enumerate(recordings, 1):
            (target / f"session-{index:02d}.jsonl").write_text(
                mask_document(path.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8", newline="\n")
    outbox = _since(artifacts / "outbox", wall0)
    if outbox:
        target = run_dir / "email"
        target.mkdir(exist_ok=True)
        stems: dict[str, int] = {}
        for path in outbox:
            number = stems.setdefault(path.stem, len(stems) + 1)
            (target / f"session-{number:02d}{path.suffix}").write_text(
                mask_document(path.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8", newline="\n")
    traces = _since(artifacts / "motion-trace", wall0, "*.jsonl")
    if traces:
        target = run_dir / "trace"
        target.mkdir(exist_ok=True)
        # Its name is a session id, and a session id can hold a student id.
        for index, path in enumerate(traces, 1):
            (target / f"session-{index:02d}.jsonl").write_text(
                mask_document(path.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8", newline="\n")
    db = dump_db(database or artifacts / DEFAULT_DB, wall0)
    if db is not None:
        (run_dir / "db.json").write_text(json.dumps(Normaliser().value(db), indent=1, sort_keys=True),
                                         encoding="utf-8", newline="\n")


def parse_mic_journal(entries: list[dict]) -> list[dict]:
    """``journalctl -o json`` entries to ``mic.jsonl``: only the ``mic profile <name>`` lines."""

    out = []
    for entry in entries:
        message = str(entry.get("MESSAGE", ""))
        marker = message.find("mic profile ")
        if marker < 0:
            continue
        words = message[marker + len("mic profile "):].split()
        if len(words) >= 2 and words[1].startswith("(capture"):
            stamp = float(entry.get("__MONOTONIC_TIMESTAMP", 0)) / 1_000_000.0
            out.append({"t": stamp, "profile": words[0]})
    return out


class RobotHost:
    """A hostapp already running, started by the recording launcher in its own window.

    ``client_dir`` is that launcher's output directory. What it holds grows across runs,
    so each run copies only what was added since the run started.
    """

    mode = "robot"
    voice = None
    robot = None

    def __init__(self, *, base_url: str, root: Path, client_dir: Optional[Path] = None,
                 restart_command: Optional[list[str]] = None, admin_token: str = "",
                 arbiter_url: str = "", journal_unit: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.artifacts = root / "artifacts"
        self._client_dir = client_dir
        self._restart_command = restart_command
        self._admin_token = admin_token
        self._arbiter_url = arbiter_url.rstrip("/")
        self._journal_unit = journal_unit
        self._offsets: dict[str, int] = {}
        self._arbiter_after = 0
        self._wall0 = 0.0

    @property
    def admin_headers(self) -> dict[str, str]:
        from emori_adapter.hostapp import admin_api

        return {admin_api.TOKEN_HEADER: self._admin_token}

    def start(self) -> None:
        self._wall0 = time.time()
        if self._client_dir is not None:
            for name in ("client.jsonl", "sys.jsonl"):
                path = self._client_dir / name
                self._offsets[name] = path.stat().st_size if path.exists() else 0
        if self._arbiter_url:
            last = httpx.get(f"{self._arbiter_url}/api/move/trace", params={"after": -1},
                             timeout=5.0).json()
            self._arbiter_after = int(last[-1]["seq"]) if last else 0

    def stop(self) -> None:
        return

    def restart(self) -> None:
        if not self._restart_command:
            raise Refused("this scenario needs --restart-command on a robot")
        subprocess.run(self._restart_command, check=True, timeout=120)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/healthz", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise ScenarioFailed("the host did not come back after its restart")

    def collect(self, run_dir: Path) -> None:
        if self._client_dir is not None:
            for name, offset in self._offsets.items():
                path = self._client_dir / name
                if not path.exists():
                    continue
                with path.open("rb") as source:
                    source.seek(offset)
                    (run_dir / name).write_bytes(source.read())
        if self._arbiter_url:
            records = httpx.get(f"{self._arbiter_url}/api/move/trace",
                                params={"after": self._arbiter_after}, timeout=5.0).json()
            write_jsonl(run_dir / "motion.jsonl",
                        [{**record, "t_mono": record.get("t")} for record in records
                         if record.get("event") == "accepted"])
        if self._journal_unit:
            done = subprocess.run(["journalctl", "-u", self._journal_unit, "-o", "json",
                                   "--since", f"@{int(self._wall0)}"],
                                  capture_output=True, text=True, timeout=30, check=False)
            entries = []
            for line in done.stdout.splitlines():
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
            write_jsonl(run_dir / "mic.jsonl", parse_mic_journal(entries))


def _commit(root: Path) -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def record_run(scenario: Any, *, host: Any, run_dir: Path, label: str = "", fast: bool = False,
               lang: str = "en", badge: str = "", second_badge: str = "", track: Any = None,
               root: Optional[Path] = None, recorders: tuple = ()) -> dict:
    """One run from start to finish. Returns its ``meta.json``, which is written last."""

    run_dir.mkdir(parents=True, exist_ok=True)
    wall0 = time.time()
    meta = {"schema": 1, "scenario": scenario.name, "label": label, "mode": host.mode,
            "kind": scenario.kind, "fast": fast, "voiced": scenario.voiced,
            "untimed": sorted(scenario.untimed), "self_check": list(scenario.self_check),
            "lang": lang, "wall0": wall0, "commit": _commit(root) if root else ""}
    if scenario.offline:
        meta["t0"] = time.monotonic()
        scenario.run_offline(run_dir)
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return meta
    host.start()
    context = Context(host=host, run_dir=run_dir, fast=fast, lang=lang, badge=badge,
                      second_badge=second_badge, track=track)
    meta["t0"] = context.t0
    try:
        context.start_relay()
        scenario.run(context)
        time.sleep(_SETTLE_S)
    finally:
        context.close()
        try:
            host.collect(run_dir)
            collect_artifacts(host.artifacts, run_dir, wall0)
            for recorder in recorders:
                recorder(run_dir, host)
        finally:
            host.stop()
            (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta
