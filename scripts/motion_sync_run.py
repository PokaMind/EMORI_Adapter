"""Run the motion path against known audio on the robot, and record everything that happens.

A scripted run speaks a synthetic fixture through the robot's speaker and posts each
sentence's event to the running hostapp over its real HTTP routes, with the same envelopes
the session page sends, so the code under test is the production path. It needs no network,
no Pokamind and no learner data, and it leaves nothing behind. What it records goes to
``artifacts/motion-sync/<run-id>/`` for ``scripts/motion_sync_report.py``.

    python scripts/motion_sync_run.py --dry-run             # says what it would do
    python scripts/motion_sync_run.py --profile loaded --runs 4 --lang en --confirm-clear
    python scripts/motion_sync_run.py --observe             # record a session someone else runs
    python scripts/motion_sync_run.py --calibrate           # chirp loopback, writes calibration.json
    python scripts/motion_sync_run.py --cleanup <run-id>    # remove a killed run's rows and files

**The head moves and the speaker plays aloud.** The head must be free and nobody touching it.
Nothing on the robot can tell: the hostapp's presence time is set by sessions and analysis
cycles, not by the camera, so an idle robot reports nobody however many people stand around
it. ``--confirm-clear`` is the operator saying so, and a run refuses without it.

How a tracked session opens
    ``POST /api/start`` mints it and ``GET /session?sid=...`` opens it, which is what a page
    load does. The student id carries the prefix ``motion-sync-<run-id>``, so the recording
    and trace files are recognisable by name. The mint signs a login token locally. It is
    dropped unread, and with no browser running there is nothing to send it anywhere.

How it ends
    Opening a second session finalizes the first, because the first has analysis cycles,
    and ``POST /api/session-start-failed`` finalizes the second, because it has exchanged
    nothing. Neither path runs the completion hook, which is the only caller of the results
    mail, and neither writes the results file: only ``pokamind_session_completed`` does
    those, and :func:`post_cycle` refuses to send it. The walk-away timer is avoided
    because it leaves an ``end`` verb waiting for the next learner's page.

What a session writes, and what happens to it
    * dashboard ``sessions``, ``cycles``, ``events`` rows and the ``persons`` row: deleted by
      exact id. The stored id has the prefix cut off, so it comes from the minted one.
    * ``artifacts/recordings/<session>.jsonl``: deleted.
    * ``artifacts/motion-trace/<session>.jsonl``: copied to the run directory, then deleted.
    * ``artifacts/outbox`` and the results file: not written, and an outbox file bearing the
      session's name is deleted if one appears.
    * FIWARE entities: not written. A hostapp with FIWARE on and a broker answering makes
      the run refuse, because no route can turn the writer off for one session.
    * dashboard ``heartbeats``: left alone. They are the robot's uptime series, not this
      run's, and a few of them say a session was active, which it was.
    * the capture gain, the gaze flag and the head-reserved flag go back at the finalize.

Motion sources
    ``--motion-source ros`` (scripted default) pipes ``motion_sync/joints_probe.py`` into the
    motor container. Stamps are taken at reception from the 50 Hz topics, so it resolves
    20 ms with 1-2 ms of transport. It costs one more rclpy process in the container, tens
    of MB, a few percent of a core to deserialize, and one more reader for the node's
    publishers. ``--motion-source http`` (observe default) polls ``/api/state/full`` at 20 Hz
    on one keep-alive connection. It costs no memory but runs inside the motor node's own
    process, and that route's reading is cached for 50 ms, so it resolves about 50 ms. Each
    sample is dated from the route's own read time rather than its arrival, which removes
    the request's latency from it. Good enough for real-session medians, not for the
    scripted gates.

Launching it
    A scripted run needs ``system.slice`` to be representative. Chromium, the hostapp and the
    motor container all share that slice's CPU, and the events have to compete for it the way
    the page's do. From an SSH login the run would sit in ``user.slice``, beside PipeWire,
    with that slice's whole share to itself::

        sudo systemd-run --unit=motion-sync-run --slice=system.slice --collect \\
            --uid=pollen --gid=pollen --working-directory=/opt/emori \\
            --setenv=XDG_RUNTIME_DIR=/run/user/1000 \\
            -p KillMode=mixed -p TimeoutStopSec=120 \\
            /opt/emori/.venv/bin/python /opt/emori/scripts/motion_sync_run.py --runs 4 \\
            --confirm-clear
        journalctl -fu motion-sync-run
        sudo systemctl stop motion-sync-run      # stops cleanly: SIGTERM runs the restore

    Observing a real session takes nothing from it that depends on the slice, so it does not
    need this.

The load stand-in
    Chromium is stopped so that nothing drains the page's verbs, which takes away its CPU.
    ``--profile loaded`` starts a bounded worker in ``system.slice`` in its place. The camera
    bridge keeps running. If a real session's p95 comes out well above the scripted one, the
    stand-in is not representative and the real figure wins.

If the run is killed outright
    A transient timer, armed again for each run, fires after that run's time bound. It runs
    ``--cleanup <run-id>``, which ends the session, takes any verb left for the next page and
    removes the rows and files named in the run's ``ledger.json``, and then starts the kiosk
    whatever that did. The load worker and the probe stop at their own bounds.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent))

from motion_sync import audio_calibrate, fixture, joints_probe  # noqa: E402
from motion_sync.audio_calibrate import StampedCapture, best_offset  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = Path("artifacts/motion-sync")
AUDIO_CACHE = RUNS_DIR / "audio"
TRACE_DIR = Path("artifacts/motion-trace")
RECORDINGS_DIR = Path("artifacts/recordings")
DEFAULT_OUTBOX = "artifacts/outbox"
DEFAULT_DB = "artifacts/dashboard.db"
CALIBRATION_FILE = RUNS_DIR / "calibration.json"

DAEMON_URL = "http://127.0.0.1:8000"
HOSTAPP_FALLBACK_PORT = "8080"
KIOSK_UNIT = "emori-kiosk.service"
HOSTAPP_UNIT = "emori-hostapp.service"
HUB_UNIT = "emori-hub.service"
HUB_CONTAINER = "emori-hub"
LOAD_UNIT = "motion-sync-load"
DEADMAN_UNIT = "motion-sync-kiosk-restore"
DEFAULT_CONTAINER = "emori-ros2-v2"

STUDENT_PREFIX = "motion-sync-"
CLOSING_SUFFIX = "-close"
SYNTHETIC_EMAIL = "motion-sync@example.invalid"
END_REASON = "motion_sync_done"

UTTERANCE = "pokamind_ai_utterance"
ANALYSIS = "pokamind_analysis_update"
TTS_ENDED = "pokamind_tts_ended"
COVER_ON = "cover_on"
COVER_OFF = "cover_off"
# The envelopes a run may send. pokamind_session_completed is not one of them: it is what
# overwrites the results file and sends the report by mail.
ALLOWED_CYCLE_TYPES = frozenset({UTTERANCE, ANALYSIS, TTS_ENDED})

MIN_FREE_BYTES = 600 * 1024 * 1024
# Seen this recently, somebody may be standing in reach of a head about to move.
# Well inside EMORI_WALKAWAY_S (45 s by default), so one lost post cannot end the run.
KEEPALIVE_S = 15.0
PILOT_START_S = 0.3
PILOT_S = 0.2
PILOT_LOW_HZ = 800.0
PILOT_HIGH_HZ = 2400.0
PILOT_AMPLITUDE = 0.2
PILOT_TIMEOUT_S = 6.0
CAPTURE_START_S = 5.0
# The silence between the pilot and the first sentence. The pilot has to be heard, and the
# first event scheduled, before anything else is due.
LEAD_IN_S = 2.0
TAIL_S = 2.5
# An early event lands this long after the previous sentence ended.
EVENT_CLEARANCE_S = 0.4
TTS_ENDED_DELAY_S = 0.05
SETTLE_WAIT_S = 15.0
SETTLE_QUIET_S = 1.0
CLOCK_TOLERANCE_S = 2.0
FIRST_SAMPLE_WAIT_S = 12.0
SILENCE_THRESHOLD = 0.01
TRUTH_SEARCH_S = 0.5
TRUTH_TEMPLATE_S = 1.0
TRUTH_MIN_STRENGTH = 0.3
PURGE_SETTLE_S = 2.0
# What a run may take beyond its audio: session open, ceremonies, cleanup.
RUN_MARGIN_S = 120.0
RESTORE_MARGIN_S = 120.0
CLEANUP_BOUND_S = 90
VERB_ATTEMPTS = 3
OBSERVE_MAX_S = 1800.0
LOAD_QUOTA = "100%"
LOAD_PROCESSES = 2
HOSTAPP_POSTS_THREADS = 4

# The mint keeps letters, digits, dash and underscore, so a run id is made of those alone.
_RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,40}\Z")
_MINTED_TAIL = r"-\d{8}-\d{6}-[0-9a-f]{16}\Z"


class Refused(Exception):
    """The run must not start. Nothing has been touched."""


class RunAborted(Exception):
    """The run cannot go on. The restore still runs."""


class Interrupted(BaseException):
    """SIGTERM or SIGINT. A BaseException, so no ``except Exception`` can swallow it."""


# --------------------------------------------------------------------------------------
# small plumbing
# --------------------------------------------------------------------------------------


# The login that started a run can go away: a hang-up is a stop like the other two.
STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
                     if hasattr(signal, name))


def _say(message: str) -> None:
    try:
        print("motion-sync: " + message, flush=True)
    except (OSError, ValueError):
        pass        # the terminal went with the login, and the restore still has to finish


def _iso_now() -> str:
    return (datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


class Http:
    """One keep-alive connection. Not thread-safe: every thread makes its own.

    Returns ``(status, json or None)``, with status 0 for a transport failure, so a poller
    can note it and carry on.
    """

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        parts = urlsplit(base_url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._timeout = timeout
        self._conn: Optional[http.client.HTTPConnection] = None

    def _request(self, method: str, path: str, body: Optional[bytes]) -> tuple[int, bytes]:
        headers = {"content-type": "application/json"} if body is not None else {}
        for attempt in (0, 1):
            if self._conn is None:
                self._conn = http.client.HTTPConnection(self._host, self._port,
                                                        timeout=self._timeout)
            try:
                self._conn.request(method, path, body=body, headers=headers)
                response = self._conn.getresponse()
                return response.status, response.read()
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt:
                    return 0, b""
        return 0, b""

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    @staticmethod
    def _decoded(status: int, raw: bytes) -> tuple[int, Any]:
        try:
            return status, json.loads(raw)
        except (TypeError, ValueError):
            return status, None

    def get(self, path: str) -> tuple[int, Any]:
        return self._decoded(*self._request("GET", path, None))

    def post(self, path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
        body = json.dumps({} if payload is None else payload).encode("utf-8")
        return self._decoded(*self._request("POST", path, body))


class Machine:
    """Everything a run does to the robot outside the hostapp's HTTP routes.

    The tests hand in a fake, so a refusal or a restore can be checked with no robot.
    """

    def __init__(self, root: Path = ROOT) -> None:
        self.root = Path(root)

    def env(self) -> dict:
        out = dict(os.environ)
        if hasattr(os, "getuid"):
            out.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        return out

    def run(self, argv: list[str], *, timeout: float = 20.0) -> tuple[int, str]:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  env=self.env())
        except (OSError, subprocess.SubprocessError) as e:
            return 127, type(e).__name__
        return done.returncode, done.stdout

    def spawn(self, argv: list[str], **kwargs):
        kwargs.setdefault("env", self.env())
        return subprocess.Popen(argv, **kwargs)

    def which(self, tool: str) -> Optional[str]:
        return shutil.which(tool)

    def free_bytes(self, path: Path) -> int:
        return shutil.disk_usage(path).free

    def unit_state(self, unit: str) -> dict:
        code, out = self.run(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                              "-p", "SubState", "-p", "MainPID"])
        if code != 0:
            return {}
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def process_env(self, pid: str) -> Optional[dict]:
        """A process's environment, for this run's decisions only; never printed or stored."""

        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return None
        pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        return {key.decode("utf-8", "replace"): value.decode("utf-8", "replace")
                for key, value in pairs}

    def broker_answers(self, orion_url: str) -> bool:
        from emori_adapter.fiware import broker_reachable

        return broker_reachable(orion_url)

    def container_env(self, container: str) -> Optional[dict]:
        """A container's environment, or None when docker will not say."""

        code, out = self.run(["docker", "inspect", container, "--format",
                              "{{range .Config.Env}}{{println .}}{{end}}"])
        if code != 0:
            return None
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def container_worker(self, container: str) -> Optional[str]:
        """The pid of the process a container's init runs, or None when there is no saying."""

        code, out = self.run(["docker", "inspect", container, "--format", "{{.State.Pid}}"])
        init = out.strip()
        if code != 0 or not init.isdigit() or init == "0":
            return None
        try:
            children = Path(f"/proc/{init}/task/{init}/children").read_text(encoding="utf-8").split()
        except OSError:
            return None
        return children[0] if children else None

    def current_slice(self) -> str:
        try:
            return Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class JsonlFile:
    """One output stream. Written by one or more threads; flushed on close and now and then."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._count = 0

    def write(self, record: dict) -> None:
        self.write_line(json.dumps(record, separators=(",", ":"), ensure_ascii=False))

    def write_line(self, line: str) -> None:
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(line + "\n")
            self._count += 1
            if self._count % 200 == 0:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class _Periodic:
    """Call a function at a fixed rate on a thread until stopped; a failed call is not fatal."""

    def __init__(self, period_s: float, name: str) -> None:
        self.period_s = period_s
        self.failures = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)

    def tick(self) -> None:
        raise NotImplementedError

    def _loop(self) -> None:
        due = time.monotonic()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one missed sample must not end a recording
                self.failures += 1
            due += self.period_s
            now = time.monotonic()
            if due < now:
                due = now
            self._stop.wait(due - now)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)


# --------------------------------------------------------------------------------------
# what is posted: the page's own envelopes
# --------------------------------------------------------------------------------------


def utterance_envelope(sentence: fixture.Sentence, lang: str) -> dict:
    """What ``useTTSManager`` posts when a sentence starts, and the relay page forwards."""

    envelope = {"type": UTTERANCE, "text": sentence.text, "seq": sentence.seq,
                "source": sentence.source, "lang": lang, "timestamp": _iso_now()}
    if sentence.family:
        envelope["cue_category"] = sentence.family
        if sentence.cue_body:
            envelope["cue_body"] = sentence.cue_body[:200]
    return envelope


def tts_ended_envelope() -> dict:
    return {"type": TTS_ENDED, "timestamp": _iso_now()}


# A person in plain view, engaged and calm. Body metrics are what say somebody is there, and
# a flat style mix keeps escalation low, so these never preempt a sentence.
_CALM_STYLES = {"neutral": 12.0, "steady": 8.0, "paced": 7.0, "measured": 3.0, "smooth": 3.0}
_CALM_BODY = {"posture": "upright", "head_position": "straight", "overall_energy": "medium",
              "gesture_type": "open_palm", "engagement_score": 55.0, "confidence_score": 50.0}


def analysis_envelope(seq: int) -> dict:
    """What ``useMySessionLogic`` posts per analysis cycle."""

    return {"type": ANALYSIS, "communication_styles": dict(_CALM_STYLES),
            "body_language": dict(_CALM_BODY),
            "body_language_metrics": {"engagement": _CALM_BODY["engagement_score"],
                                      "confidence": _CALM_BODY["confidence_score"]},
            "seq": seq, "timestamp": _iso_now()}


def post_cycle(client, envelope: dict) -> tuple[int, Any]:
    """POST one envelope to ``/api/cycle``. Refuses anything a run must never send."""

    kind = envelope.get("type")
    if kind not in ALLOWED_CYCLE_TYPES:
        raise ValueError(f"a motion-sync run does not send {kind!r}")
    return client.post("/api/cycle", envelope)


# --------------------------------------------------------------------------------------
# the running order in time
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """One sentence in the played WAV. All samples, from sample 0 of the file."""

    sentence: fixture.Sentence
    start: int
    end: int
    part_starts: tuple[int, ...]
    event_at_s: float
    event_offset_ms: float


def lay_out(sentences: tuple[fixture.Sentence, ...], lengths: dict[str, tuple[int, ...]],
            rate: int) -> list[Placement]:
    """Place each sentence in the WAV from its rendered length. Pure: samples in, samples out."""

    first = round((PILOT_START_S + PILOT_S + LEAD_IN_S) * rate)
    cursor = first
    placed: list[Placement] = []
    burst_base = 0
    for sentence in sentences:
        gap = sentence.gap_ms / 1000.0
        lead = -sentence.event_offset_ms / 1000.0
        if lead > 0 and gap >= 0:
            # An early event has to land on nothing, or it is another case.
            gap = max(gap, lead + EVENT_CLEARANCE_S)
        start = max(first, cursor + round(gap * rate))
        part_starts = []
        if sentence.audio:
            end = start
            pause = round(sentence.pause_s * rate)
            for index, length in enumerate(lengths[sentence.case]):
                if index:
                    end += pause
                part_starts.append(end)
                end += length
        else:
            end = start + round(fixture.NO_AUDIO_SLOT_S * rate)
        if sentence.burst is not None:
            if sentence.burst == 0:
                burst_base = start
            event_at = burst_base / rate + sentence.burst * fixture.BURST_STEP_MS / 1000.0
            offset_ms = (event_at - start / rate) * 1000.0
        else:
            offset_ms = sentence.event_offset_ms
            event_at = start / rate + offset_ms / 1000.0
        placed.append(Placement(sentence, start, end, tuple(part_starts), event_at, offset_ms))
        cursor = end
    return placed


@dataclass(frozen=True)
class Post:
    """One scheduled post, by offset from sample 0 of the WAV."""

    at_s: float
    kind: str
    sentence: Optional[fixture.Sentence] = None


def build_schedule(placements: list[Placement], rate: int) -> list[Post]:
    """Every post of a run, in time order: events, turn ends, analysis cycles."""

    posts: list[Post] = []
    last = 0.0
    for place in placements:
        sentence = place.sentence
        start_s, end_s = place.start / rate, place.end / rate
        last = max(last, end_s)
        if sentence.posted:
            posts.append(Post(place.event_at_s, UTTERANCE, sentence))
        for at_s in sentence.engagement_at_s:
            posts.append(Post(start_s + at_s, ANALYSIS))
        if sentence.cover:
            posts.append(Post(start_s, COVER_ON))
            posts.append(Post(end_s, COVER_OFF))
        if sentence.turn_end:
            ended = end_s + TTS_ENDED_DELAY_S if sentence.audio else end_s - 1.0
            posts.append(Post(ended, TTS_ENDED))
    keepalive = KEEPALIVE_S
    while keepalive < last + TAIL_S:
        posts.append(Post(keepalive, ANALYSIS))
        keepalive += KEEPALIVE_S
    return sorted(posts, key=lambda post: post.at_s)


def track_seconds(placements: list[Placement], rate: int) -> float:
    return max(place.end for place in placements) / rate + TAIL_S


def play_command(wav: Path, sink: str = "") -> list[str]:
    """Play the track the way the kiosk plays its voice: into the delay sink, when there is one."""

    return ["pw-play"] + (["--target", sink] if sink else []) + [str(wav)]


def voice_sink(hostapp_env: Optional[dict]) -> str:
    """The sink the hostapp's tap listens to, or "" for the speaker itself."""

    env = hostapp_env or {}
    if env.get("EMORI_VOICE_LINK_SINK"):
        return env["EMORI_VOICE_LINK_SINK"]
    return "emori_voice" if env.get("EMORI_VOICE_DELAY_MS", "0") not in ("", "0") else ""


def _pauses(place: Placement, rate: int) -> list[list[float]]:
    """The silences between a sentence's parts, in seconds from sample 0 of the WAV."""

    gap = round(place.sentence.pause_s * rate)
    return [[round((start - gap) / rate, 4), round(start / rate, 4)]
            for start in place.part_starts[1:]]


def build_manifest(*, run_id: str, commit: str, profile: str, lang: str, motion_source: str,
                   k_ms: Optional[float], t_wav0: Optional[float],
                   placements: list[Placement], rate: int, voice_delay_s: float = 0.0) -> dict:
    """``manifest.json``. Sentence text stays in the tracked fixture."""

    sentences = []
    for place in placements:
        sentence = place.sentence
        if not sentence.posted:
            continue
        sentences.append({
            "seq": sentence.seq,
            "case": sentence.case,
            "family": sentence.family,
            "cue_body_given": bool(sentence.cue_body),
            "expect_family": sentence.expect_family,
            "chars": len(sentence.text),
            "audio": sentence.audio,
            "wav_start_s": round(place.start / rate, 4) if sentence.audio else None,
            "wav_end_s": round(place.end / rate, 4) if sentence.audio else None,
            "pauses": _pauses(place, rate),
            "event_offset_ms": round(place.event_offset_ms, 1),
            "t_event_planned": round((t_wav0 or 0.0) - voice_delay_s + place.event_at_s, 4),
            "expected_exception": sentence.expected_exception,
        })
    return {"schema": 1, "run_id": run_id, "commit": commit, "profile": profile, "lang": lang,
            "motion_source": motion_source, "k_ms": k_ms, "t_wav0": t_wav0,
            "voice_delay_ms": round(voice_delay_s * 1000.0, 1), "sentences": sentences}


# --------------------------------------------------------------------------------------
# the audio: one Piper-rendered WAV, one pw-play
# --------------------------------------------------------------------------------------


def make_pilot(rate: int):
    """The sweep at the head of the WAV. A sweep, so its correlation peak is one sample wide."""

    import numpy as np

    count = int(PILOT_S * rate)
    times = np.arange(count) / rate
    sweep = (PILOT_HIGH_HZ - PILOT_LOW_HZ) / PILOT_S
    wave_ = np.sin(2.0 * np.pi * (PILOT_LOW_HZ * times + 0.5 * sweep * times * times))
    fade = max(1, int(0.01 * rate))
    envelope = np.ones(count)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return wave_ * envelope * PILOT_AMPLITUDE


def trim_silence(samples):
    """Cut what Piper leaves around a line, so a part starts on its first audible sample."""

    import numpy as np

    loud = np.flatnonzero(np.abs(samples) >= SILENCE_THRESHOLD)
    if loud.size == 0:
        return samples[:0]
    return samples[int(loud[0]): int(loud[-1]) + 1]


def piper_synth(voice_path: str) -> Callable[[str], tuple[Any, int]]:
    """A text to ``(floats, rate)`` function over one Piper voice."""

    import numpy as np
    from piper import PiperVoice

    voice = PiperVoice.load(voice_path)

    def synthesize(text: str) -> tuple[Any, int]:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            voice.synthesize_wav(text, out)
        buffer.seek(0)
        with wave.open(buffer, "rb") as read:
            rate = read.getframerate()
            raw = read.readframes(read.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype("float64") / 32768.0, rate

    return synthesize


@dataclass
class Rendered:
    """A played WAV and where everything is in it."""

    wav: Path
    rate: int
    placements: list[Placement]

    def samples(self):
        import numpy as np

        with wave.open(str(self.wav), "rb") as read:
            raw = read.readframes(read.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype("float64") / 32768.0


def _cache_key(lang: str, sentences: tuple[fixture.Sentence, ...], voice_id: str) -> str:
    material = {
        "lang": lang, "voice": voice_id,
        "pilot": [PILOT_START_S, PILOT_S, PILOT_LOW_HZ, PILOT_HIGH_HZ, PILOT_AMPLITUDE, LEAD_IN_S],
        "tail": TAIL_S, "threshold": SILENCE_THRESHOLD,
        "sentences": [[s.case, list(s.parts), s.gap_ms, s.event_offset_ms, s.pause_s, s.audio]
                      for s in sentences],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def render(sentences: tuple[fixture.Sentence, ...], lang: str, cache_dir: Path,
           synth_factory: Callable[[], Callable[[str], tuple[Any, int]]],
           voice_id: str = "") -> Rendered:
    """The run's WAV, from the cache when the fixture has not changed.

    Rendered once on the robot and kept, because Piper takes a good fraction of the track's
    length on a CM4. Samples are placed by index, so every time in the manifest is exact.
    """

    import numpy as np

    key = _cache_key(lang, sentences, voice_id)
    wav = cache_dir / f"{lang}-{key}.wav"
    sidecar = cache_dir / f"{lang}-{key}.json"
    if wav.exists() and sidecar.exists():
        info = json.loads(sidecar.read_text(encoding="utf-8"))
        rate = int(info["rate"])
        lengths = {case: tuple(parts) for case, parts in info["lengths"].items()}
        return Rendered(wav, rate, lay_out(sentences, lengths, rate))

    for stale in cache_dir.glob(f"{lang}-*") if cache_dir.exists() else ():
        stale.unlink()
    synth = synth_factory()
    rate = 0
    audio: dict[str, list] = {}
    for sentence in sentences:
        if not sentence.audio:
            continue
        chunks = []
        for part in sentence.parts:
            samples, part_rate = synth(part)
            rate = rate or part_rate
            if part_rate != rate:
                raise RunAborted("a voice changed its sample rate between lines")
            chunks.append(trim_silence(samples))
        audio[sentence.case] = chunks
    lengths = {case: tuple(int(chunk.size) for chunk in chunks) for case, chunks in audio.items()}
    placements = lay_out(sentences, lengths, rate)
    total = round(track_seconds(placements, rate) * rate)
    track = np.zeros(total)
    pilot = make_pilot(rate)
    pilot_at = round(PILOT_START_S * rate)
    track[pilot_at: pilot_at + pilot.size] += pilot
    for place in placements:
        for start, chunk in zip(place.part_starts, audio.get(place.sentence.case, ())):
            track[start: start + chunk.size] += chunk
    pcm = (np.clip(track, -1.0, 1.0) * 32767.0).astype("<i2")
    audio_calibrate.write_wav(wav, pcm, rate)
    sidecar.write_text(json.dumps({"rate": rate, "lengths": {k: list(v) for k, v in
                                                             lengths.items()}}), encoding="utf-8")
    return Rendered(wav, rate, placements)


def find_pilot(capture: StampedCapture, rate: int, *, timeout_s: float = PILOT_TIMEOUT_S,
               sleep: Callable[[float], None] = time.sleep) -> tuple[int, float]:
    """Find the pilot in the live capture: ``(capture index of WAV sample 0, t_wav0)``."""

    import numpy as np

    pilot = make_pilot(rate)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sleep(0.05)
        head = np.frombuffer(capture.head(), dtype="<i2").astype("float64") / 32768.0
        if head.size < pilot.size:
            continue
        at, strength = best_offset(head, pilot)
        if strength >= 0.5 and at + pilot.size + rate // 20 <= head.size:
            zero = at - round(PILOT_START_S * rate)
            return zero, capture.time_of(at) - PILOT_START_S
    raise RunAborted("the pilot tone never reached the speaker monitor")


def compute_truth(capture: StampedCapture, rendered: Rendered, zero_index: int) -> dict:
    """Each sentence's first and last audible sample, from the monitor capture.

    The played WAV is cross-correlated against what the monitor recorded, in a window around
    where the sample offsets say each sentence should be. These are monitor times. The
    report adds the calibration's offset to reach the audible time, so that a run stays
    usable when the calibration improves. A sentence that does not correlate is left out
    and reads as missing, rather than as a time nobody measured.
    """

    captured = audio_calibrate.load_samples(capture.path)
    track = rendered.samples()
    rate = rendered.rate
    search = int(TRUTH_SEARCH_S * rate)
    template = int(TRUTH_TEMPLATE_S * rate)
    rows = []

    def locate(wav_start: int, wav_end: int) -> Optional[int]:
        needle = track[wav_start: wav_end]
        expected = zero_index + wav_start
        low = max(0, expected - search)
        hay = captured[low: expected + needle.size + search]
        at, strength = best_offset(hay, needle)
        return low + at if strength >= TRUTH_MIN_STRENGTH else None

    for place in rendered.placements:
        sentence = place.sentence
        if not (sentence.posted and sentence.audio):
            continue
        first = locate(place.start, min(place.end, place.start + template))
        tail_start = max(place.start, place.end - template)
        last = locate(tail_start, place.end)
        if first is None or last is None:
            continue
        rows.append({"seq": sentence.seq, "t_voice": round(capture.time_of(first), 4),
                     "t_end": round(capture.time_of(last + (place.end - tail_start)), 4)})
    return {"sentences": rows}


# --------------------------------------------------------------------------------------
# motion, queue, relay and load streams
# --------------------------------------------------------------------------------------


class LatestMotion:
    """The newest queue facts from whichever stream has them, for the moves records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fields: dict = {}

    def update(self, fields: dict) -> None:
        with self._lock:
            self._fields.update(fields)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._fields)


def ros_probe_command(container: str, max_s: float) -> list[str]:
    """``docker exec -i`` a Python that reads the probe from stdin and keeps stdin open.

    Keeping stdin open is how the host stops it: ``docker exec`` does not reliably signal a
    process when its client dies, but the pipe closes either way. No ``ros2`` command is
    involved, so the ros2 daemon does not start.
    """

    boot = ("import sys;n=int(sys.stdin.buffer.readline());"
            "exec(compile(sys.stdin.buffer.read(n),'joints_probe','exec'))")
    shell = ("source /opt/ros/jazzy/setup.bash && "
             "{ [ -f /emori/ros2_ws/install/setup.bash ] && "
             "source /emori/ros2_ws/install/setup.bash; true; } && "
             'exec python3 -u -c "$0" "$@"')
    return ["docker", "exec", "-i", "-e", f"ROS_LOG_DIR=/tmp/{joints_probe.MARKER}-log",
            container, "bash", "-c", shell, boot, joints_probe.MARKER,
            "--max-s", str(int(max_s))]


class RosJoints:
    """``--motion-source ros``: the probe in the motor container, one JSON line per tick."""

    name = "ros"

    def __init__(self, machine: Machine, container: str, path: Path, latest: LatestMotion,
                 *, max_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._machine = machine
        self._container = container
        self._path = path
        self._latest = latest
        self._max_s = max_s
        self._clock = clock
        self._proc = None
        self._out: Optional[JsonlFile] = None
        self._thread: Optional[threading.Thread] = None
        self._first = threading.Event()
        self.first_t: Optional[float] = None
        self.first_seen_at: Optional[float] = None
        self.samples = 0

    def _source(self) -> bytes:
        return Path(joints_probe.__file__).read_bytes()

    def start(self) -> None:
        self._out = JsonlFile(self._path)
        self._proc = self._machine.spawn(ros_probe_command(self._container, self._max_s),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
        source = self._source()
        self._proc.stdin.write(b"%d\n" % len(source) + source)
        self._proc.stdin.flush()
        self._thread = threading.Thread(target=self._read, name="joints-ros", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        for raw in self._proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "status" in record:
                self._latest.update(record["status"])
                continue
            if "rpy" not in record:
                continue
            self.samples += 1
            if self.first_t is None:
                self.first_t, self.first_seen_at = record["t"], self._clock()
                self._first.set()
            self._out.write_line(line)

    def wait_first(self, timeout_s: float = FIRST_SAMPLE_WAIT_S) -> None:
        """Raise unless a sample arrives, and arrives on the host's own clock."""

        if not self._first.wait(timeout_s):
            raise RunAborted("no motion sample from /joint_states and the head pose topic")
        if abs(self.first_t - self.first_seen_at) > CLOCK_TOLERANCE_S:
            raise RunAborted("the container's monotonic clock is not the host's")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # The client went; the process it started may not have.
                self._machine.run(["docker", "exec", self._container, "pkill", "-f",
                                   joints_probe.MARKER])
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._out is not None:
            self._out.close()


def joints_from_state(state: dict, *, monotonic_now: float, wall_now: float) -> tuple[dict, str]:
    """One ``/api/state/full`` reply as a joints record: ``(record, the route's own stamp)``.

    The route says when it read the backend, on this machine's wall clock. Subtracting the
    age of that reading from the monotonic clock dates the sample by the read, not by the
    arrival of the reply. ``antennas_position`` is on the wire as ``[right, left]``.
    """

    pose = state["head_pose"]
    antennas = state["antennas_position"]
    stamp = str(state.get("timestamp") or "")
    t = monotonic_now
    try:
        read_at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        if 0.0 <= wall_now - read_at < 1.0:
            t = monotonic_now - (wall_now - read_at)
    except ValueError:
        pass
    record = {"t": round(t, 4),
              "rpy": [round(float(pose[k]), 5) for k in ("roll", "pitch", "yaw")],
              "ant": [round(float(antennas[1]), 5), round(float(antennas[0]), 5)],
              "yaw": round(float(state.get("body_yaw") or 0.0), 5)}
    return record, stamp


class HttpJoints(_Periodic):
    """``--motion-source http``: ``GET /api/state/full`` at 20 Hz on one connection."""

    name = "http"

    def __init__(self, client, path: Path, latest: LatestMotion,
                 *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        super().__init__(0.05, "joints-http")
        self._client = client
        self._path = path
        self._latest = latest
        self._clock = clock
        self._wall = wall
        self._out: Optional[JsonlFile] = None
        self._last_stamp = ""
        self._first = threading.Event()
        self.samples = 0

    def start(self) -> None:
        self._out = JsonlFile(self._path)
        super().start()

    def tick(self) -> None:
        code, state = self._client.get("/api/state/full")
        if code != 200 or not isinstance(state, dict):
            return
        record, stamp = joints_from_state(state, monotonic_now=self._clock(),
                                          wall_now=self._wall())
        motion = state.get("motion")
        if isinstance(motion, dict):
            self._latest.update({key: motion.get(key) for key in
                                 ("active", "head_moving", "lane", "client")})
        # The route caches one reading for 50 ms, and this polls at about that rate.
        if stamp and stamp == self._last_stamp:
            return
        self._last_stamp = stamp
        self._out.write(record)
        self.samples += 1
        self._first.set()

    def wait_first(self, timeout_s: float = FIRST_SAMPLE_WAIT_S) -> None:
        if not self._first.wait(timeout_s):
            raise RunAborted("no motion sample from /api/state/full")

    def stop(self) -> None:
        super().stop()
        self._client.close()
        if self._out is not None:
            self._out.close()


class MovesPoll(_Periodic):
    """``moves.jsonl``: what the robot reports running, at 10 Hz. Also collects ``arb.jsonl``.

    ``queue_depth`` is present when the motion source can read it from the node's status topic.
    """

    def __init__(self, client, path: Path, latest: LatestMotion,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(0.1, "moves-poll")
        self._client = client
        self._path = path
        self._latest = latest
        self._clock = clock
        self._out: Optional[JsonlFile] = None
        self._trace_after: Optional[int] = None

    def start(self) -> None:
        self._out = JsonlFile(self._path)
        code, last = self._client.get("/api/move/trace?after=-1")
        if code == 200 and isinstance(last, list):
            self._trace_after = int(last[0]["seq"]) if last else 0
        super().start()

    def tick(self) -> None:
        code, running = self._client.get("/api/move/running")
        if code != 200 or not isinstance(running, list):
            return
        record = {"t": round(self._clock(), 4), "running": len(running),
                  "ids": [str(move.get("uuid"))[:8] for move in running if isinstance(move, dict)]}
        record.update(self._latest.snapshot())
        self._out.write(record)

    def stop(self) -> None:
        super().stop()
        try:
            self._save_trace()
        finally:
            self._client.close()
            if self._out is not None:
                self._out.close()

    def _save_trace(self) -> None:
        """``arb.jsonl``: the robot's own record of when each move really ran.

        A motor process that has no such record answers 404 here, and the report then falls
        back on working the queue out from the commands.
        """

        if self._trace_after is None:
            return
        code, records = self._client.get(f"/api/move/trace?after={self._trace_after}")
        if code == 200 and isinstance(records, list):
            out = JsonlFile(self._path.with_name("arb.jsonl"))
            for record in records:
                out.write(record)
            out.close()


def published_record(message: str, stamp: float) -> Optional[dict]:
    """One relay message as a ``published.jsonl`` record, keeping only these fields.

    A real session's relay carries the words being spoken, and they must not reach a file.
    """

    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") != UTTERANCE:
        return None
    try:
        seq = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {"t": round(stamp, 4), "seq": seq, "cue_family": data.get("cue_family"),
            "cue_category": data.get("cue_category")}


class RelayListener:
    """``published.jsonl``, from ``/ws/relay``. Listens only: a sent message would be rebroadcast."""

    def __init__(self, url: str, path: Path, *, connect: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._url = url
        self._path = path
        self._connect = connect
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="relay", daemon=True)
        self._out: Optional[JsonlFile] = None

    def _opener(self) -> Callable:
        if self._connect is not None:
            return self._connect
        from websockets.sync.client import connect

        return lambda url: connect(url, open_timeout=3.0, max_size=None)

    def _loop(self) -> None:
        opener = self._opener()
        while not self._stop.is_set():
            try:
                with opener(self._url) as ws:
                    while not self._stop.is_set():
                        try:
                            message = ws.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        record = published_record(message, self._clock())
                        if record is not None:
                            self._out.write(record)
            except Exception:  # noqa: BLE001 - the hostapp restarting costs records, not the run
                self._stop.wait(1.0)

    def start(self) -> None:
        self._out = JsonlFile(self._path)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._out is not None:
            self._out.close()


def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def cpu_jiffies(proc_root: Path, pid: Any) -> Optional[int]:
    """A process's user plus system time, or None when it has gone."""

    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The name in parentheses can hold spaces, so count from after it.
    fields = raw.rsplit(")", 1)[-1].split()
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def find_pipewire(proc_root: Path) -> Optional[str]:
    """The PipeWire daemon's pid. Not its filter chain, which runs the same binary."""

    try:
        entries = sorted((p for p in proc_root.iterdir() if p.name.isdigit()),
                         key=lambda p: int(p.name))
    except OSError:
        return None
    for entry in entries:
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and Path(argv[0].decode("utf-8", "replace")).name == "pipewire" \
                and not any(arg for arg in argv[1:]):
            return entry.name
    return None


class LoadSampler(_Periodic):
    """``load.jsonl`` at 1 Hz: load average, hostapp and PipeWire CPU as percent of one core."""

    def __init__(self, path: Path, hostapp_pid: Any, *, proc_root: Path = Path("/proc"),
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(1.0, "load")
        self._path = path
        self._proc = proc_root
        self._clock = clock
        self._pids = {"hostapp_cpu": hostapp_pid, "pipewire_cpu": find_pipewire(proc_root)}
        self._last: dict[str, tuple[float, int]] = {}
        self._ticks = _clock_ticks()
        self._out: Optional[JsonlFile] = None

    def start(self) -> None:
        self._out = JsonlFile(self._path)
        super().start()

    def tick(self) -> None:
        now = self._clock()
        load1 = float((self._proc / "loadavg").read_text(encoding="utf-8").split()[0])
        record: dict = {"t": round(now, 3), "load1": load1}
        for name, pid in self._pids.items():
            used = cpu_jiffies(self._proc, pid) if pid else None
            percent = 0.0
            if used is not None and name in self._last:
                then, before = self._last[name]
                if now > then:
                    percent = 100.0 * (used - before) / self._ticks / (now - then)
            if used is not None:
                self._last[name] = (now, used)
            record[name] = round(percent, 1)
        self._out.write(record)

    def stop(self) -> None:
        super().stop()
        if self._out is not None:
            self._out.close()


# --------------------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------------------


def student_id(run_id: str, *, closing: bool = False) -> str:
    return STUDENT_PREFIX + run_id + (CLOSING_SUFFIX if closing else "")


def is_own_session(session_id: Any, run_id: str) -> bool:
    """Is this exactly a session this run minted? Nothing else is ever ended or removed."""

    if not isinstance(session_id, str):
        return False
    return re.fullmatch(re.escape(STUDENT_PREFIX + run_id)
                        + "(?:" + re.escape(CLOSING_SUFFIX) + ")?" + _MINTED_TAIL,
                        session_id) is not None


def mint_session(client, run_id: str, *, closing: bool = False) -> str:
    """``POST /api/start``. Returns the session id and nothing else from the reply.

    The reply also holds a signed login. It was made locally and goes no further than this
    function, because the session page is only ever asked for its ``sid``.
    """

    code, reply = client.post("/api/start", {"student_id": student_id(run_id, closing=closing),
                                             "email": SYNTHETIC_EMAIL})
    if code != 200 or not isinstance(reply, dict):
        raise RunAborted(f"POST /api/start answered {code}")
    sids = parse_qs(urlsplit(str(reply.get("session_url", ""))).query).get("sid", [])
    if not sids or not is_own_session(sids[0], run_id):
        raise RunAborted("POST /api/start did not mint a recognisable session")
    return sids[0]


def open_session(client, session_id: str) -> None:
    """``GET /session?sid=...``: the page load is what starts a session."""

    code, _ = client.get("/session?sid=" + quote(session_id, safe=""))
    if code != 200:
        raise RunAborted(f"GET /session answered {code}")


def status_of(client) -> dict:
    code, status = client.get("/api/status")
    if code != 200 or not isinstance(status, dict):
        raise RunAborted(f"GET /api/status answered {code}")
    return status


def active_session_id(status: dict) -> Optional[str]:
    session = status.get("session")
    return session.get("session_id") if isinstance(session, dict) else None


def end_session(client, run_id: str, *, wait: Callable[[float], None] = time.sleep,
                on_minted: Optional[Callable[[str], None]] = None) -> bool:
    """Finalize this run's session with no completion envelope. True when none is left active.

    Opening a session finalizes the one before it, provided that one has analysis cycles, and
    the new one has exchanged nothing, so the start-failed route finalizes it too. A session
    that is not this run's is left exactly as it is.
    """

    current = active_session_id(status_of(client))
    if current is None:
        return True
    if not is_own_session(current, run_id):
        _say("a session this run did not open is active: leaving it alone")
        return False
    post_cycle(client, analysis_envelope(0))
    closing = mint_session(client, run_id, closing=True)
    if on_minted is not None:
        on_minted(closing)
    open_session(client, closing)
    client.post("/api/session-start-failed", {"reason": END_REASON})
    for _ in range(30):
        if active_session_id(status_of(client)) is None:
            return True
        wait(0.1)
    return False


def drain_stale_verbs(client, *, wait: Callable[[float], None] = time.sleep) -> None:
    """Take any ``end`` or ``begin`` left for a page that no longer exists.

    A watchdog or a nudged head during the run queues a verb in the hostapp's one slot, and
    nothing drains it with the kiosk stopped. Left there, it would end the next learner's
    session on that page's first poll. Only ever called with the kiosk stopped or no session
    active, so no page can lose a verb meant for it. ``home`` is not asked for: the next
    session page clears it.
    """

    for attempt in range(VERB_ATTEMPTS):
        if attempt:
            wait(1.0)
        code, _ = client.get("/api/pending-cmd?verbs=end,begin")
        if code == 200:
            return
    raise RunAborted("the hostapp would not hand over its waiting verb")


@dataclass
class Paths:
    """Where a session's durable files are, under the hostapp's working directory."""

    root: Path
    db: Path
    outbox: Path

    @classmethod
    def for_hostapp(cls, root: Path, environ: Optional[dict]) -> "Paths":
        env = environ or {}
        return cls(root=root, db=root / (env.get("EMORI_DASH_DB") or DEFAULT_DB),
                   outbox=root / (env.get("EMORI_OUTBOX") or DEFAULT_OUTBOX))


def _file_stem(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)


def trace_path(paths: Paths, session_id: str) -> Path:
    from emori_adapter.motion_trace import _filename

    return paths.root / TRACE_DIR / f"{_filename(session_id)}.jsonl"


def purge_rows(db_path: Path, session_ids: list[str], run_id: str) -> dict[str, int]:
    """Delete these sessions' dashboard rows, and only those. Refuses an id this run did not mint.

    The store keeps the session id with the prefix cut off, so the prefix cannot be what
    finds a row: each key is derived from the full minted id, by the store's own function,
    which makes it unique by the mint's random part. The person row goes too, unless some
    other session still names it.
    """

    from emori_adapter.hostapp.metrics_store import deidentify_session_id

    removed = {"sessions": 0, "cycles": 0, "events": 0, "persons": 0}
    for session_id in session_ids:
        if not is_own_session(session_id, run_id):
            raise ValueError("refusing to remove a session this run did not mint")
    if not db_path.exists():
        return removed
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        with conn:
            for session_id in session_ids:
                key = deidentify_session_id(session_id)
                codes = [row[0] for row in conn.execute(
                    "SELECT person_code FROM sessions WHERE session_id=?", (key,))]
                for table in ("cycles", "events", "sessions"):
                    cursor = conn.execute(f"DELETE FROM {table} WHERE session_id=?", (key,))
                    removed[table] += max(0, cursor.rowcount)
                for code in codes:
                    cursor = conn.execute(
                        "DELETE FROM persons WHERE person_code=? AND NOT EXISTS "
                        "(SELECT 1 FROM sessions WHERE person_code=?)", (code, code))
                    removed["persons"] += max(0, cursor.rowcount)
    finally:
        conn.close()
    return removed


def purge_files(paths: Paths, session_ids: list[str], run_id: str) -> list[Path]:
    """Delete these sessions' recording, trace and outbox files. Returns what it removed."""

    removed = []
    for session_id in session_ids:
        if not is_own_session(session_id, run_id):
            raise ValueError("refusing to remove a session this run did not mint")
        stem = _file_stem(session_id)
        for path in (paths.root / RECORDINGS_DIR / f"{stem}.jsonl",
                     trace_path(paths, session_id),
                     paths.outbox / f"{stem}.html",
                     paths.outbox / f"{stem}.json"):
            if path.exists():
                path.unlink()
                removed.append(path)
    return removed


def write_ledger(run_dir: Path, run_id: str, session_ids: list[str]) -> None:
    """The ids a killed run would leave behind, written before the session they name opens."""

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ledger.json").write_text(
        json.dumps({"run_id": run_id, "session_ids": session_ids}), encoding="utf-8")


# --------------------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    name: str
    reason: str


@dataclass
class Config:
    profile: str = "loaded"
    runs: int = 1
    lang: str = "en"
    motion_source: str = "ros"
    confirm_clear: bool = False
    voice_sink: str = ""
    dry_run: bool = False
    observe: bool = False
    calibrate: bool = False
    render_only: bool = False
    cleanup: Optional[str] = None
    run_id: Optional[str] = None
    max_s: Optional[float] = None
    wait_s: float = 600.0
    hostapp_url: Optional[str] = None
    daemon_url: str = DAEMON_URL
    container: str = DEFAULT_CONTAINER
    calibration: Optional[Path] = None


def check_session_active(status: Optional[dict]) -> Optional[Refusal]:
    if status is not None and status.get("session"):
        return Refusal("session_active", "a session is running: never interrupt one")
    return None


def check_clear(confirmed: bool) -> Optional[Refusal]:
    if confirmed:
        return None
    return Refusal("not_confirmed_clear", "the head is about to move and the speaker to play, "
                                          "and nothing on the robot can see who is near it: "
                                          "pass --confirm-clear once the head is free")


def check_disk(free_bytes: int) -> Optional[Refusal]:
    if free_bytes < MIN_FREE_BYTES:
        return Refusal("disk_low", f"{free_bytes // (1024 * 1024)} MB free, under "
                                   f"{MIN_FREE_BYTES // (1024 * 1024)} MB")
    return None


def check_robot(status: Optional[dict], daemon_state: Optional[dict]) -> Optional[Refusal]:
    if status is None:
        return Refusal("hostapp_down", "the hostapp does not answer /api/status")
    if not status.get("robot"):
        return Refusal("robot_down", "the hostapp says the robot is not there")
    if not isinstance(daemon_state, dict) or daemon_state.get("state") != "running":
        return Refusal("robot_down", "the robot's control loop is not running")
    backend = daemon_state.get("backend_status")
    if isinstance(backend, dict) and backend.get("motor_control_mode") == "disabled":
        return Refusal("robot_down", "the motors are off")
    return None


def check_kiosk(unit: dict) -> Optional[Refusal]:
    if (unit.get("LoadState"), unit.get("ActiveState"), unit.get("SubState")) \
            != ("loaded", "active", "running"):
        return Refusal("kiosk_state",
                       f"{KIOSK_UNIT} is not loaded, active and running, so this run could "
                       "not put it back as it found it")
    return None


def check_trace(status: Optional[dict]) -> Optional[Refusal]:
    trace = (status or {}).get("motion_trace")
    if not isinstance(trace, dict) or not trace.get("enabled"):
        return Refusal("trace_off", "the hostapp is not tracing (EMORI_MOTION_TRACE): the run "
                                    "would record nothing the report can join")
    return None


def check_fiware(hostapp_env: Optional[dict], broker_answers: Callable[[str], bool],
                 container_env: Optional[dict]) -> Optional[Refusal]:
    """No FIWARE write may happen, and no route can turn the writer off for one session."""

    if hostapp_env is None:
        return Refusal("fiware_unknown", "cannot read the hostapp's environment, so cannot tell "
                                         "whether a session would write FIWARE entities")
    if container_env is None:
        return Refusal("fiware_unknown", "cannot read the motor container's environment, so "
                                         "cannot tell whether its FIWARE bridge is on")
    bridge = str(container_env.get("FIWARE_BRIDGE_ENABLED", "")).strip().lower()
    if bridge in ("1", "true", "yes", "on"):
        return Refusal("fiware_write", "the motor container's FIWARE bridge is on")
    if hostapp_env.get("EMORI_FIWARE", "1") == "0":
        return None
    orion = hostapp_env.get("ORION_URL") or "http://localhost:1026"
    if broker_answers(orion):
        return Refusal("fiware_write", "the hostapp has FIWARE on and its broker answers: a "
                                       "session would write entities. Set EMORI_FIWARE=0 and "
                                       "restart the hostapp, or stop the broker")
    return None


def check_timers(hostapp_env: Optional[dict]) -> Optional[Refusal]:
    """A hostapp set to give up faster than the run's cycles arrive would end the run."""

    env = hostapp_env or {}
    for name in ("EMORI_WALKAWAY_S", "EMORI_ABSENCE_S"):
        try:
            value = float(env.get(name) or 0.0)
        except ValueError:
            continue
        if 0.0 < value <= KEEPALIVE_S + 5.0:
            return Refusal("timer_short", f"{name} is shorter than the run can keep alive")
    return None


def check_addon(hostapp) -> Optional[Refusal]:
    """An add-on mode in progress owns the head, and every sentence would be refused for it."""

    code, status = hostapp.get("/api/addon/status")
    if code == 200 and isinstance(status, dict) and status.get("mode") not in (None, "", "idle"):
        return Refusal("addon_busy", "an add-on mode is running")
    return None


def check_tools(machine: Machine, tools: tuple[str, ...]) -> Optional[Refusal]:
    missing = [tool for tool in tools if machine.which(tool) is None]
    if missing:
        return Refusal("tools_missing", "not on this machine: " + ", ".join(missing))
    return None


def hostapp_environ(machine: Machine) -> tuple[Optional[str], Optional[dict]]:
    """The process to sample as the gateway, and the environment it was started with.

    On a hub the unit's main process is the compose client, which holds what the unit parsed from
    the env files and does none of the work, so the process sampled is the one in the container.
    """

    pid = machine.unit_state(HOSTAPP_UNIT).get("MainPID")
    if pid and pid != "0":
        return pid, machine.process_env(pid)
    client = machine.unit_state(HUB_UNIT).get("MainPID")
    if not client or client == "0":
        return None, None
    return machine.container_worker(HUB_CONTAINER) or client, machine.process_env(client)


def resolve_hostapp_url(cfg: Config, hostapp_env: Optional[dict]) -> str:
    if cfg.hostapp_url:
        return cfg.hostapp_url.rstrip("/")
    port = (hostapp_env or {}).get("HOSTAPP_PORT") or HOSTAPP_FALLBACK_PORT
    return f"http://127.0.0.1:{port}"


def _read_only_status(hostapp) -> Optional[dict]:
    code, status = hostapp.get("/api/status")
    return status if code == 200 and isinstance(status, dict) else None


def scripted_refusals(cfg: Config, machine: Machine, hostapp, daemon,
                      hostapp_env: Optional[dict]) -> list[Refusal]:
    """Every read-only reason a scripted run must not start. Touches nothing."""

    status = _read_only_status(hostapp)
    _, daemon_state = daemon.get("/api/daemon/status")
    tools = ("pw-play", "pw-record", "sudo", "systemd-run", "systemctl")
    if cfg.motion_source == "ros":
        tools += ("docker",)
    container_env = machine.container_env(cfg.container) if machine.which("docker") else {}
    found = [
        check_robot(status, daemon_state),
        check_session_active(status),
        check_clear(cfg.confirm_clear),
        check_disk(machine.free_bytes(machine.root)),
        check_kiosk(machine.unit_state(KIOSK_UNIT)),
        check_trace(status),
        check_fiware(hostapp_env, machine.broker_answers, container_env),
        check_timers(hostapp_env),
        check_addon(hostapp),
        check_tools(machine, tools),
    ]
    return [refusal for refusal in found if refusal is not None]


def _print_refusals(refusals: list[Refusal]) -> None:
    for refusal in refusals:
        _say(f"refused ({refusal.name}): {refusal.reason}")


# --------------------------------------------------------------------------------------
# the kiosk and the load stand-in
# --------------------------------------------------------------------------------------


def load_worker_command(bound_s: float) -> list[str]:
    """A CPU-quota worker in ``system.slice``, where Chromium runs. It stops at its own bound."""

    spin = " ".join(["(while :; do :; done) &"] * LOAD_PROCESSES)
    argv = ["sudo", "-n", "systemd-run", f"--unit={LOAD_UNIT}", "--slice=system.slice",
            "--collect", "-p", f"CPUQuota={LOAD_QUOTA}", "-p", f"RuntimeMaxSec={int(bound_s)}"]
    if hasattr(os, "getuid"):
        argv.append(f"--uid={os.getuid()}")
    return argv + ["/bin/sh", "-c", f"{spin} wait"]


def deadman_command(root: Path, after_s: float, run_id: Optional[str]) -> list[str]:
    """The timer that puts things back if this process dies without its restore.

    A session left open by a killed run would end on its walk-away timer, which leaves an
    ``end`` waiting for the next learner's page, so ``--cleanup`` goes first. The kiosk starts
    whatever that does, and a bound keeps a hung hostapp from holding it back.
    """

    start = f"/bin/systemctl start {KIOSK_UNIT}"
    script = f"exec {start}"
    if run_id and hasattr(os, "getuid"):
        cleanup = shlex.join(["sudo", "-n", "-u", f"#{os.getuid()}", "timeout",
                              str(CLEANUP_BOUND_S), sys.executable,
                              str(root / "scripts" / "motion_sync_run.py"), "--cleanup", run_id])
        script = f"{cleanup}; {script}"
    return ["sudo", "-n", "systemd-run", f"--unit={DEADMAN_UNIT}", f"--on-active={int(after_s)}",
            "--timer-property=AccuracySec=1s", "--collect", "/bin/sh", "-c", script]


class Kiosk:
    """Stop the kiosk for a run and always put it back, even if this process is killed."""

    def __init__(self, machine: Machine, restore_after_s: float) -> None:
        self._machine = machine
        self._restore_after_s = restore_after_s
        self._stopped = False
        self._deadman = False

    def _arm(self, run_id: Optional[str]) -> None:
        code, _ = self._machine.run(
            deadman_command(self._machine.root, self._restore_after_s, run_id))
        if code != 0:
            raise RunAborted("could not arm the timer that brings the kiosk back")

    def rearm(self, run_id: str) -> None:
        """The next run's clock and the next run's cleanup."""

        self._machine.run(["sudo", "-n", "systemctl", "stop", f"{DEADMAN_UNIT}.timer"])
        self._arm(run_id)

    def stop(self, run_id: Optional[str] = None) -> None:
        # The flags go up first: a signal can land inside either call, and putting back
        # what turns out never to have changed costs nothing.
        self._deadman = True
        self._arm(run_id)
        self._stopped = True
        code, _ = self._machine.run(["sudo", "-n", "systemctl", "stop", KIOSK_UNIT], timeout=30.0)
        if code != 0:
            raise RunAborted(f"could not stop {KIOSK_UNIT}")

    def restore(self) -> bool:
        back = True
        if self._stopped:
            self._machine.run(["sudo", "-n", "systemctl", "start", KIOSK_UNIT], timeout=30.0)
            back = self._machine.unit_state(KIOSK_UNIT).get("ActiveState") == "active"
            self._stopped = not back
        if self._deadman and back:
            self._machine.run(["sudo", "-n", "systemctl", "stop", f"{DEADMAN_UNIT}.timer"])
            self._deadman = False
        if not back:
            _say(f"{KIOSK_UNIT} did not come back: start it by hand. The timer stays armed")
        return back


class LoadWorker:
    def __init__(self, machine: Machine, bound_s: float) -> None:
        self._machine = machine
        self._bound_s = bound_s
        self._running = False

    def start(self) -> None:
        self._running = True
        code, _ = self._machine.run(load_worker_command(self._bound_s))
        if code != 0:
            raise RunAborted("could not start the load worker")

    def stop(self) -> None:
        if self._running:
            self._machine.run(["sudo", "-n", "systemctl", "stop", f"{LOAD_UNIT}.service"])
            self._running = False


# --------------------------------------------------------------------------------------
# posting on time
# --------------------------------------------------------------------------------------


class Scheduler:
    """Post each scheduled envelope at its offset from ``t_wav0``.

    The real page never waits for one post before making the next, so the posts go out on
    a small pool, each worker on its own connection. A slow reply cannot make the next event
    late, and three events can really land inside 50 ms.
    """

    def __init__(self, schedule: list[Post], t_wav0: float, client_factory: Callable[[], Any],
                 lang: str, *, clock: Callable[[], float] = time.monotonic,
                 threads: int = HOSTAPP_POSTS_THREADS) -> None:
        self._schedule = schedule
        self._t_wav0 = t_wav0
        self._client_factory = client_factory
        self._lang = lang
        self._clock = clock
        self._threads = threads
        self._queue: queue.Queue = queue.Queue()
        self._abort = threading.Event()
        self._analysis_seq = 0
        self._main = threading.Thread(target=self._dispatch, name="schedule", daemon=True)
        self._workers: list[threading.Thread] = []
        self.failed = 0
        self.worst_late_s = 0.0

    def start(self) -> None:
        for index in range(self._threads):
            worker = threading.Thread(target=self._work, name=f"post-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)
        self._main.start()

    def _dispatch(self) -> None:
        for post in self._schedule:
            due = self._t_wav0 + post.at_s
            while not self._abort.is_set():
                left = due - self._clock()
                if left <= 0.002:
                    break
                self._abort.wait(left - 0.002 if left > 0.004 else 0.0005)
            if self._abort.is_set():
                break
            self.worst_late_s = max(self.worst_late_s, self._clock() - due)
            self._queue.put(self._envelope(post))
        for _ in self._workers:
            self._queue.put(None)

    def _envelope(self, post: Post) -> dict:
        if post.kind in (COVER_ON, COVER_OFF):
            return {"type": post.kind}
        if post.kind == UTTERANCE:
            return utterance_envelope(post.sentence, self._lang)
        if post.kind == TTS_ENDED:
            return tts_ended_envelope()
        self._analysis_seq += 1
        return analysis_envelope(self._analysis_seq)

    def _work(self) -> None:
        client = self._client_factory()
        try:
            while True:
                envelope = self._queue.get()
                if envelope is None:
                    return
                if envelope["type"] in (COVER_ON, COVER_OFF):
                    code, _ = client.post("/api/voice-cover", {"on": envelope["type"] == COVER_ON})
                else:
                    code, _ = post_cycle(client, envelope)
                if code != 200:
                    self.failed += 1
        finally:
            client.close()

    def alive(self) -> bool:
        return self._main.is_alive() or any(w.is_alive() for w in self._workers)

    def join(self, timeout: float) -> None:
        self._main.join(timeout)

    def stop(self) -> None:
        self._abort.set()
        self._main.join(timeout=2.0)
        for worker in self._workers:
            worker.join(timeout=2.0)


# --------------------------------------------------------------------------------------
# one scripted run
# --------------------------------------------------------------------------------------


def _commit(root: Path) -> str:
    deployed = root / "DEPLOYED_COMMIT.txt"
    try:
        return deployed.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        pass
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=5.0)
        return done.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_calibration(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("k_ms"), (int, float)) else None


def _new_run_id(prefix: str = "") -> str:
    return prefix + time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def make_joints(cfg: Config, machine: Machine, path: Path, latest: LatestMotion, max_s: float):
    if cfg.motion_source == "ros":
        return RosJoints(machine, cfg.container, path, latest, max_s=max_s)
    return HttpJoints(Http(cfg.daemon_url), path, latest)


class ScriptedRun:
    """One pass of the fixture. ``execute`` always restores, however it ends."""

    def __init__(self, cfg: Config, machine: Machine, rendered: Rendered, run_id: str,
                 hostapp_factory: Callable[[], Any], daemon_factory: Callable[[], Any],
                 paths: Paths, *, hostapp_pid: Any = None, relay_connect: Optional[Callable] = None,
                 joints_factory: Optional[Callable] = None,
                 capture_factory: Optional[Callable] = None, relay_url: str = "") -> None:
        self.cfg = cfg
        self.machine = machine
        self.rendered = rendered
        self.run_id = run_id
        self.run_dir = machine.root / RUNS_DIR / run_id
        self.paths = paths
        self._hostapp_factory = hostapp_factory
        self._daemon_factory = daemon_factory
        self._hostapp_pid = hostapp_pid
        self._relay_connect = relay_connect
        self._relay_url = relay_url
        self._joints_factory = joints_factory
        self._capture_factory = capture_factory
        self.session_ids: list[str] = []
        self.session_id: Optional[str] = None
        self.finished = False
        self._hostapp = hostapp_factory()
        self._streams: list = []
        self._player = None
        self._capture: Optional[StampedCapture] = None
        self._ahead: Optional[StampedCapture] = None
        self._scheduler: Optional[Scheduler] = None
        self._restored = False
        self.restore_failures: list[str] = []

    # --- setup -------------------------------------------------------------------------
    def _note_session(self, session_id: str) -> None:
        self.session_ids.append(session_id)
        write_ledger(self.run_dir, self.run_id, self.session_ids)

    def _start_streams(self) -> None:
        latest = LatestMotion()
        factory = self._joints_factory or make_joints
        duration = track_seconds(self.rendered.placements, self.rendered.rate)
        joints = factory(self.cfg, self.machine, self.run_dir / "joints.jsonl", latest,
                         duration + RUN_MARGIN_S)
        self._streams.append(joints)
        joints.start()
        joints.wait_first()
        moves = MovesPoll(self._daemon_factory(), self.run_dir / "moves.jsonl", latest)
        self._streams.append(moves)
        moves.start()
        relay = RelayListener(self._relay_url, self.run_dir / "published.jsonl",
                              connect=self._relay_connect)
        self._streams.append(relay)
        relay.start()
        load = LoadSampler(self.run_dir / "load.jsonl", self._hostapp_pid)
        self._streams.append(load)
        load.start()

    def _open(self) -> None:
        self.session_id = mint_session(self._hostapp, self.run_id)
        self._note_session(self.session_id)
        open_session(self._hostapp, self.session_id)
        status = status_of(self._hostapp)
        if active_session_id(status) != self.session_id:
            raise RunAborted("the session did not open as tracked")
        if status.get("fiware"):
            raise RunAborted("the session has a live FIWARE writer")
        # What the page does on the frame's first message, then the first cycle, so the
        # session has cycles and can be finalized by the next one, whenever the run stops.
        self._hostapp.post("/api/frame-alive")
        post_cycle(self._hostapp, analysis_envelope(0))

    def _wait_ceremony_done(self) -> None:
        """Let the greeting finish, so the first sentence does not start under it."""

        daemon = self._daemon_factory()
        quiet_since = None
        deadline = time.monotonic() + SETTLE_WAIT_S
        try:
            while time.monotonic() < deadline:
                code, state = daemon.get("/api/state/full")
                motion = state.get("motion") if code == 200 and isinstance(state, dict) else None
                if isinstance(motion, dict) and motion.get("active"):
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= SETTLE_QUIET_S:
                    return
                self.machine.sleep(0.1)
        finally:
            daemon.close()

    # --- the run -----------------------------------------------------------------------
    def execute(self) -> None:
        try:
            self._run()
            self.finished = True
        finally:
            with signals_deferred():
                self.restore()
            if self.restore_failures:
                raise RunAborted("the restore did not finish: "
                                 + ", ".join(self.restore_failures))

    def _run(self) -> None:
        rendered = self.rendered
        write_ledger(self.run_dir, self.run_id, self.session_ids)
        self._start_streams()
        self._open()
        self._wait_ceremony_done()
        self._start_capture()
        self._player = self.machine.spawn(play_command(rendered.wav, self.cfg.voice_sink),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        zero_index, t_wav0 = find_pilot(self._capture, rendered.rate)
        # The page posts by what it is about to play, ahead of any delay, and the truth is what
        # comes out at the speaker. So events go by the sink's own monitor, and truth by the
        # speaker's. The difference between the two is the delay itself.
        t_events = t_wav0
        if self._ahead is not None:
            _, t_events = find_pilot(self._ahead, rendered.rate)
            self._ahead.stop()
            self._ahead.delete()
            self._ahead = None
        self._write_manifest(t_wav0, t_events)
        schedule = build_schedule(rendered.placements, rendered.rate)
        self._scheduler = Scheduler(schedule, t_events, self._hostapp_factory, self.cfg.lang)
        self._scheduler.start()
        self._watch()
        self._capture.stop()
        truth = compute_truth(self._capture, rendered, zero_index)
        (self.run_dir / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
        missing = sum(1 for p in rendered.placements if p.sentence.posted and p.sentence.audio) \
            - len(truth["sentences"])
        if missing:
            _say(f"{missing} sentences did not correlate and are missing from truth.json")
        _say(f"{self.run_id}: worst post lateness {self._scheduler.worst_late_s * 1000:.1f} ms, "
             f"{self._scheduler.failed} failed posts")

    def _start_capture(self) -> None:
        """Start hearing the monitor, right before the audio, so the pilot is in what is kept."""

        make_capture = self._capture_factory or StampedCapture
        self._capture = make_capture(audio_calibrate.monitor_command(self.rendered.rate),
                                     self.run_dir / "truth-capture.raw", self.rendered.rate,
                                     env=self.machine.env())
        self._capture.start(keep_head_s=PILOT_TIMEOUT_S + 1.0)
        if self.cfg.voice_sink:
            self._ahead = make_capture(audio_calibrate.monitor_command(self.rendered.rate,
                                                                       self.cfg.voice_sink),
                                       self.run_dir / "ahead-capture.raw", self.rendered.rate,
                                       env=self.machine.env())
            self._ahead.start(keep_head_s=PILOT_TIMEOUT_S + 1.0)
        deadline = time.monotonic() + CAPTURE_START_S
        while self._capture.samples() == 0:
            if time.monotonic() >= deadline:
                raise RunAborted("the speaker monitor delivered nothing")
            self.machine.sleep(0.02)

    def _watch(self) -> None:
        """Wait out the audio, and stop if the session ended underneath the run."""

        rate = self.rendered.rate
        end = time.monotonic() + track_seconds(self.rendered.placements, rate) + 5.0
        next_check = 0.0
        while time.monotonic() < end:
            self._scheduler.join(0.2)
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.0
                status = status_of(self._hostapp)
                if active_session_id(status) != self.session_id:
                    raise RunAborted("the session ended during the run")
            played = self._player.poll()
            if played not in (None, 0):
                raise RunAborted("pw-play failed")
            if not self._scheduler.alive() and played is not None:
                return
        raise RunAborted("the audio outlasted its own length")

    def _write_manifest(self, t_wav0: float, t_events: float) -> None:
        calibration_path = self.cfg.calibration or (self.machine.root / CALIBRATION_FILE)
        calibration = load_calibration(calibration_path)
        if calibration is not None:
            (self.run_dir / "calibration.json").write_text(json.dumps(calibration),
                                                           encoding="utf-8")
        manifest = build_manifest(
            run_id=self.run_id, commit=_commit(self.machine.root), profile=self.cfg.profile,
            lang=self.cfg.lang, motion_source=self.cfg.motion_source,
            k_ms=calibration["k_ms"] if calibration else None, t_wav0=round(t_wav0, 4),
            placements=self.rendered.placements, rate=self.rendered.rate,
            voice_delay_s=round(t_wav0 - t_events, 4))
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1),
                                                    encoding="utf-8")

    # --- the restore -------------------------------------------------------------------
    def _step(self, name: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as e:  # noqa: BLE001 - one failed step must not skip the rest
            self.restore_failures.append(name)
            _say(f"restore: {name} failed ({type(e).__name__})")

    def restore(self) -> None:
        """Audio off, session ended, probes stopped, temporary audio deleted, rows removed."""

        if self._restored:
            return
        self._restored = True
        if self._scheduler is not None:
            self._step("scheduler", self._scheduler.stop)
        self._step("player", self._stop_player)
        if self.session_id is not None:
            self._step("session", self._end_session)
            self._step("verbs", lambda: drain_stale_verbs(self._hostapp, wait=self.machine.sleep))
        for stream in reversed(self._streams):
            self._step(type(stream).__name__, stream.stop)
        if self._capture is not None:
            self._step("capture", self._capture.stop)
            self._step("capture file", self._capture.delete)
        if self._ahead is not None:
            self._step("ahead capture", self._ahead.stop)
            self._step("ahead capture file", self._ahead.delete)
        if self.session_ids:
            self._step("trace copy", self._copy_trace)
            self._step("purge", self._purge)
        self._step("connection", self._hostapp.close)

    def _stop_player(self) -> None:
        if self._player is not None and self._player.poll() is None:
            self._player.terminate()
            try:
                self._player.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._player.kill()

    def _end_session(self) -> None:
        for attempt in range(VERB_ATTEMPTS):
            if attempt:
                self.machine.sleep(1.0)
            try:
                if end_session(self._hostapp, self.run_id, wait=self.machine.sleep,
                               on_minted=self._note_session):
                    return
            except RunAborted:
                pass
        raise RunAborted("the session did not end")

    def _copy_trace(self) -> None:
        if self.session_id is None:
            return
        source = trace_path(self.paths, self.session_id)
        if source.exists():
            shutil.copyfile(source, self.run_dir / "trace.jsonl")

    def _purge(self) -> None:
        # A session's last telemetry can land a moment after it ends, on its own thread.
        self.machine.sleep(PURGE_SETTLE_S)
        purge_rows(self.paths.db, self.session_ids, self.run_id)
        purge_files(self.paths, self.session_ids, self.run_id)
        self.machine.sleep(PURGE_SETTLE_S)
        purge_rows(self.paths.db, self.session_ids, self.run_id)


# --------------------------------------------------------------------------------------
# the three ways in, and the signals around them
# --------------------------------------------------------------------------------------


@contextlib.contextmanager
def signals_deferred():
    """Hold the stop signals while a restore runs, and act on them once it has."""

    pending: list[int] = []
    previous = {signum: signal.signal(signum, lambda n, _f: pending.append(n))
                for signum in STOP_SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if pending:
        raise Interrupted(f"signal {pending[0]}")


class Guard:
    """The stop signals and the run's time bound all become :class:`Interrupted`.

    The restore then runs with those signals ignored, because it is what they were asking for.
    """

    def __init__(self, bound_s: float) -> None:
        self._bound_s = bound_s
        self._previous: dict = {}
        self._timer: Optional[threading.Timer] = None

    @staticmethod
    def _raise(signum, _frame) -> None:
        raise Interrupted(f"signal {signum}")

    def _expire(self) -> None:
        _say(f"the run reached its time bound of {self._bound_s:.0f} s")
        os.kill(os.getpid(), signal.SIGTERM)

    def __enter__(self) -> "Guard":
        for signum in STOP_SIGNALS:
            self._previous[signum] = signal.signal(signum, self._raise)
        self._timer = threading.Timer(self._bound_s, self._expire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def hold(self) -> None:
        """From here on nothing interrupts the restore."""

        if self._timer is not None:
            self._timer.cancel()
        for signum in self._previous:
            signal.signal(signum, signal.SIG_IGN)

    def __exit__(self, *_exc) -> None:
        self.hold()
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()


def voice_path(machine: Machine, lang: str, hostapp_env: Optional[dict]) -> Path:
    named = (hostapp_env or {}).get(f"PIPER_VOICE_{lang.upper()}")
    return machine.root / (named or f"voices/{lang}.onnx")


def render_for(cfg: Config, machine: Machine, hostapp_env: Optional[dict]) -> Rendered:
    voice = voice_path(machine, cfg.lang, hostapp_env)
    if not voice.exists():
        raise Refused(f"no Piper voice for {cfg.lang}")
    return render(fixture.build(cfg.lang), cfg.lang, machine.root / AUDIO_CACHE,
                  lambda: piper_synth(str(voice)), voice_id=voice.name)


def describe_plan(cfg: Config) -> list[str]:
    sentences = fixture.build(cfg.lang)
    posted = sum(1 for s in sentences if s.posted)
    return [
        f"would speak {posted} synthetic sentences ({cfg.lang}), {cfg.runs} run(s), "
        f"profile {cfg.profile}, motion source {cfg.motion_source}",
        f"would stop {KIOSK_UNIT}, arming a timer that brings it back if this process dies",
        "would start a bounded CPU worker in system.slice" if cfg.profile == "loaded"
        else "would add no load",
        "would open a tracked session over POST /api/start and GET /session, with no browser",
        "would play one WAV with pw-play and post each event at its offset from the pilot tone",
        "would record joints, moves, published, load and a monitor capture for truth.json",
        "would end the session with no completion envelope, then remove its rows and files",
        f"would start {KIOSK_UNIT} again",
    ]


def run_dry(cfg: Config, machine: Machine, hostapp, daemon, hostapp_env: Optional[dict]) -> int:
    """Say what a run would do and what would refuse it. Reads only."""

    for line in describe_plan(cfg):
        _say(line)
    refusals = scripted_refusals(cfg, machine, hostapp, daemon, hostapp_env)
    if refusals:
        _print_refusals(refusals)
    else:
        _say("nothing would refuse a run right now")
    return 0


def run_scripted(cfg: Config, machine: Machine, *, hostapp_factory: Callable[[], Any],
                 daemon_factory: Callable[[], Any], hostapp_pid: Any,
                 hostapp_env: Optional[dict], relay_url: str,
                 rendered: Optional[Rendered] = None, run_factory: Optional[Callable] = None,
                 joints_factory: Optional[Callable] = None, run_kwargs: Optional[dict] = None,
                 guard_factory: Callable[[float], Guard] = Guard) -> int:
    """Refuse, or stop the kiosk, do the runs, and put everything back."""

    hostapp, daemon = hostapp_factory(), daemon_factory()
    try:
        refusals = scripted_refusals(cfg, machine, hostapp, daemon, hostapp_env)
        if refusals:
            _print_refusals(refusals)
            return 2
        tap = (_read_only_status(hostapp) or {}).get("voice_link")
        if not (isinstance(tap, dict) and tap.get("available")):
            _say("the voice tap is unavailable: every sentence would count as degraded (E7)")
        if rendered is None:
            try:
                rendered = render_for(cfg, machine, hostapp_env)
            except Refused as e:
                _say(f"refused: {e}")
                return 2
    finally:
        hostapp.close()
        daemon.close()
    cfg = dataclasses.replace(cfg, voice_sink=voice_sink(hostapp_env))
    if "system.slice" not in machine.current_slice():
        _say("not in system.slice: the timing will not be representative (see --help)")
    each = track_seconds(rendered.placements, rendered.rate) + RUN_MARGIN_S
    bound = cfg.max_s or each * cfg.runs + RUN_MARGIN_S
    kiosk = Kiosk(machine, min(each, bound) + RESTORE_MARGIN_S)
    worker = LoadWorker(machine, bound)
    base_id = cfg.run_id or _new_run_id()
    run_ids = [base_id] if cfg.runs == 1 else [f"{base_id}-{i + 1}" for i in range(cfg.runs)]
    make_run = run_factory or ScriptedRun
    paths = Paths.for_hostapp(machine.root, hostapp_env)
    code = 0
    with guard_factory(bound) as guard:
        try:
            try:
                # Before anything is stopped: a robot whose topics are silent is refused
                # with the kiosk still up. A scripted run's own source then replaces this.
                verify_motion(cfg, machine, joints_factory)
                kiosk.stop(run_ids[0])
                if cfg.profile == "loaded":
                    worker.start()
                for index, run_id in enumerate(run_ids):
                    if index:
                        kiosk.rearm(run_id)
                    run = make_run(cfg, machine, rendered, run_id, hostapp_factory,
                                   daemon_factory, paths, hostapp_pid=hostapp_pid,
                                   relay_url=relay_url, joints_factory=joints_factory,
                                   **(run_kwargs or {}))
                    run.execute()
                    if machine.free_bytes(machine.root) < MIN_FREE_BYTES:
                        raise RunAborted("the disk fell under its limit during the runs")
                    _say(f"run {run_id} recorded under {RUNS_DIR / run_id}")
            except Refused as e:
                _say(f"refused: {e}")
                code = 2
            except RunAborted as e:
                _say(f"aborted: {e}")
                code = 1
            except Interrupted as e:
                _say(f"stopped: {e}")
                code = 1
        finally:
            guard.hold()
            worker.stop()
            if not kiosk.restore():
                code = 1
    return code


def verify_motion(cfg: Config, machine: Machine, factory: Optional[Callable] = None) -> None:
    """Is motion really arriving? Refuses before anything is stopped, and keeps nothing."""

    workdir = machine.root / RUNS_DIR / f"check-{secrets.token_hex(3)}"
    joints = (factory or make_joints)(cfg, machine, workdir / "joints.jsonl", LatestMotion(), 60.0)
    try:
        joints.start()
        try:
            joints.wait_first()
        except RunAborted as e:
            raise Refused(f"joints_silent: {e}") from e
    finally:
        joints.stop()
        shutil.rmtree(workdir, ignore_errors=True)


def run_observe(cfg: Config, machine: Machine, *, hostapp, daemon_factory: Callable[[], Any],
                hostapp_pid: Any, hostapp_env: Optional[dict], relay_url: str,
                joints_factory: Optional[Callable] = None,
                relay_connect: Optional[Callable] = None,
                guard_factory: Callable[[float], Guard] = Guard) -> int:
    """Record a session somebody else runs, and change nothing.

    No kiosk stop, no audio, no posted event and no cleanup. Only GETs and listeners, then a
    copy of the trace file, whose records hold no words and no identity. The session's id
    can hold a student id, so it is used to find the trace and goes nowhere else.
    """

    refusals = []
    disk = check_disk(machine.free_bytes(machine.root))
    if disk:
        refusals.append(disk)
    status = _read_only_status(hostapp)
    daemon = daemon_factory()
    code, daemon_state = daemon.get("/api/daemon/status")
    daemon.close()
    robot = check_robot(status, daemon_state if code == 200 else None)
    if robot:
        refusals.append(robot)
    if refusals:
        _print_refusals(refusals)
        return 2

    run_id = cfg.run_id or _new_run_id("observe-")
    run_dir = machine.root / RUNS_DIR / run_id
    paths = Paths.for_hostapp(machine.root, hostapp_env)
    bound = cfg.max_s or OBSERVE_MAX_S
    streams: list = []
    session_id: Optional[str] = None
    lang = ""
    with guard_factory(bound + cfg.wait_s) as guard:
        try:
            deadline = time.monotonic() + cfg.wait_s
            while session_id is None:
                session = _read_only_status(hostapp) or {}
                session_id = active_session_id(session)
                lang = str((session.get("session") or {}).get("lang", "")) if session_id else ""
                if session_id is None:
                    if time.monotonic() >= deadline:
                        _say("no session started while waiting")
                        return 2
                    machine.sleep(1.0)
            _say(f"recording a session to {RUNS_DIR / run_id}")
            latest = LatestMotion()
            factory = joints_factory or make_joints
            joints = factory(cfg, machine, run_dir / "joints.jsonl", latest, bound)
            streams.append(joints)
            joints.start()
            moves = MovesPoll(daemon_factory(), run_dir / "moves.jsonl", latest)
            streams.append(moves)
            moves.start()
            relay = RelayListener(relay_url, run_dir / "published.jsonl", connect=relay_connect)
            streams.append(relay)
            relay.start()
            load = LoadSampler(run_dir / "load.jsonl", hostapp_pid)
            streams.append(load)
            load.start()
            ends = time.monotonic() + bound
            while time.monotonic() < ends:
                current = active_session_id(_read_only_status(hostapp) or {})
                if current != session_id:
                    break
                machine.sleep(1.0)
        except Interrupted as e:
            _say(f"stopped: {e}")
        finally:
            guard.hold()
            for stream in reversed(streams):
                stream.stop()
    if session_id is None:
        return 2
    manifest = build_manifest(run_id=run_id, commit=_commit(machine.root), profile="observe",
                              lang=lang, motion_source=cfg.motion_source, k_ms=None,
                              t_wav0=None, placements=[], rate=1)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    # The trace flushes when its session closes, and within a second otherwise.
    machine.sleep(1.5)
    source = trace_path(paths, session_id)
    if source.exists():
        shutil.copyfile(source, run_dir / "trace.jsonl")
    else:
        _say("the session wrote no trace file")
    return 0


def run_calibrate(cfg: Config, machine: Machine, *, hostapp, daemon,
                  guard_factory: Callable[[float], Guard] = Guard,
                  calibrate: Callable[..., dict] = audio_calibrate.calibrate) -> int:
    """The chirp loopback, under the same refusals and the same restore as a run."""

    status = _read_only_status(hostapp)
    _, daemon_state = daemon.get("/api/daemon/status")
    found = [check_robot(status, daemon_state), check_session_active(status),
             check_disk(machine.free_bytes(machine.root)),
             check_kiosk(machine.unit_state(KIOSK_UNIT)),
             check_tools(machine, ("pw-play", "pw-record", "sudo", "systemd-run", "systemctl"))]
    refusals = [refusal for refusal in found if refusal is not None]
    if refusals:
        _print_refusals(refusals)
        return 2
    kiosk = Kiosk(machine, 120.0 + RESTORE_MARGIN_S)
    code = 0
    with guard_factory(120.0) as guard:
        try:
            try:
                kiosk.stop()
                record = calibrate(machine.root / CALIBRATION_FILE,
                                   machine.root / RUNS_DIR / "calibrate-tmp",
                                   env=machine.env())
                _say(f"k_ms {record['k_ms']}: {record['note']}")
            except (RunAborted, ValueError) as e:
                _say(f"aborted: {e}")
                code = 1
            except Interrupted as e:
                _say(f"stopped: {e}")
                code = 1
        finally:
            guard.hold()
            if not kiosk.restore():
                code = 1
    return code


def run_cleanup(cfg: Config, machine: Machine, *, hostapp, hostapp_env: Optional[dict]) -> int:
    """After a run that was killed: end its session if it is still open, and remove its traces."""

    run_id = cfg.cleanup or ""
    if not _RUN_ID.match(run_id):
        _say("not a run id")
        return 2
    run_dir = machine.root / RUNS_DIR / run_id
    try:
        ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _say("that run has no ledger")
        return 2
    session_ids = [sid for sid in ledger.get("session_ids", []) if is_own_session(sid, run_id)]
    try:
        if not end_session(hostapp, run_id, wait=machine.sleep, on_minted=session_ids.append):
            _say("a session is still active: nothing removed")
            return 1
        drain_stale_verbs(hostapp, wait=machine.sleep)
    except RunAborted as e:
        _say(f"cannot reach the hostapp ({e}): removing the files and rows anyway")
    paths = Paths.for_hostapp(machine.root, hostapp_env)
    if session_ids and not (run_dir / "trace.jsonl").exists():
        source = trace_path(paths, session_ids[0])
        if source.exists():
            shutil.copyfile(source, run_dir / "trace.jsonl")
    purge_rows(paths.db, session_ids, run_id)
    purge_files(paths, session_ids, run_id)
    write_ledger(run_dir, run_id, session_ids)
    _say(f"removed the rows and files of {len(session_ids)} session(s)")
    return 0


def parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--profile", choices=("loaded", "quiet"), default="loaded",
                        help="loaded starts the CPU worker that stands in for Chromium")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--lang", choices=fixture.LANGS, default="en")
    parser.add_argument("--motion-source", choices=("ros", "http"), default=None,
                        help="default: ros for a scripted run, http for --observe")
    parser.add_argument("--confirm-clear", action="store_true",
                        help="the head is free and nobody is touching it; a run needs this")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, touch nothing")
    parser.add_argument("--observe", action="store_true",
                        help="record a session someone else runs: no stop, no audio, no posts")
    parser.add_argument("--calibrate", action="store_true", help="the chirp loopback")
    parser.add_argument("--render-only", action="store_true", help="only render and cache the WAV")
    parser.add_argument("--cleanup", metavar="RUN_ID",
                        help="remove what a killed run left, from its ledger")
    parser.add_argument("--run-id")
    parser.add_argument("--max-s", type=float, help="the whole invocation's time bound")
    parser.add_argument("--wait-s", type=float, default=600.0,
                        help="how long --observe waits for a session to start")
    parser.add_argument("--hostapp-url")
    parser.add_argument("--daemon-url", default=DAEMON_URL)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.run_id and not _RUN_ID.match(args.run_id):
        parser.error("--run-id: letters, digits, dot, dash and underscore only")
    source = args.motion_source or ("http" if args.observe else "ros")
    return Config(profile=args.profile, runs=args.runs, lang=args.lang, motion_source=source,
                  confirm_clear=args.confirm_clear, dry_run=args.dry_run, observe=args.observe,
                  calibrate=args.calibrate, render_only=args.render_only, cleanup=args.cleanup,
                  run_id=args.run_id, max_s=args.max_s, wait_s=args.wait_s,
                  hostapp_url=args.hostapp_url, daemon_url=args.daemon_url,
                  container=args.container, calibration=args.calibration)


def main(argv: Optional[list[str]] = None, *, machine: Optional[Machine] = None) -> int:
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    machine = machine or Machine()
    hostapp_pid, hostapp_env = hostapp_environ(machine)
    base = resolve_hostapp_url(cfg, hostapp_env)
    hostapp_factory = lambda: Http(base)  # noqa: E731 - one connection per caller
    daemon_factory = lambda: Http(cfg.daemon_url)  # noqa: E731
    relay_url = "ws://" + base.split("://", 1)[-1] + "/ws/relay"
    if cfg.render_only:
        rendered = render_for(cfg, machine, hostapp_env)
        _say(f"{rendered.wav}: {track_seconds(rendered.placements, rendered.rate):.0f} s")
        return 0
    if cfg.dry_run:
        return run_dry(cfg, machine, hostapp_factory(), daemon_factory(), hostapp_env)
    if cfg.cleanup:
        return run_cleanup(cfg, machine, hostapp=hostapp_factory(), hostapp_env=hostapp_env)
    if cfg.calibrate:
        return run_calibrate(cfg, machine, hostapp=hostapp_factory(), daemon=daemon_factory())
    if cfg.observe:
        return run_observe(cfg, machine, hostapp=hostapp_factory(),
                           daemon_factory=daemon_factory, hostapp_pid=hostapp_pid,
                           hostapp_env=hostapp_env, relay_url=relay_url)
    return run_scripted(cfg, machine, hostapp_factory=hostapp_factory,
                        daemon_factory=daemon_factory, hostapp_pid=hostapp_pid,
                        hostapp_env=hostapp_env, relay_url=relay_url)


if __name__ == "__main__":
    raise SystemExit(main())
