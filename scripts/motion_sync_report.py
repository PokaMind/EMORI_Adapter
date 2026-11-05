"""Per-sentence motion-sync report: joins the streams of a run and applies the gates.

    python scripts/motion_sync_report.py artifacts/motion-sync/<run-id> [<run-id> ...]
    python scripts/motion_sync_report.py <run-dir> ... --gates m1 --json out.json
    python scripts/motion_sync_report.py <run-dir> ... --gates final --against baseline.json

Exit status: 0 when every selected gate passes, 1 when any selected gate fails (or the
comparison against a baseline finds a defect), 3 when none failed but a selected gate cannot
be measured from the streams of these runs. A gate that cannot be measured never passes.
(2 is argparse's own, for a usage error.)

A run is a directory with these files; all times are CLOCK_MONOTONIC seconds on the robot.

``manifest.json``     what the run played: one entry per sentence (seq, case, family, audio,
                      planned event offset, expected family, expected exception), the
                      motion source and the tap-to-speaker offset ``k_ms``.
``trace.jsonl``       the hostapp's own motion trace for the session (``motion_trace.py``).
``joints.jsonl``      ``{"t", "rpy", "ant", "yaw"}`` samples of the measured head pose
                      (roll, pitch, yaw in radians), both antennas and the body yaw.
``moves.jsonl``       a 10 Hz poll of what the robot reports running.
``truth.json``        ``{"sentences": [{"seq", "t_voice", "t_end"}]}`` from the played WAV
                      against a second capture of the speaker monitor (scripted runs).
``published.jsonl``   ``{"t", "seq", "cue_family", "cue_category"}`` as relayed.
``load.jsonl``        1 Hz ``{"t", "load1", "hostapp_cpu", "pipewire_cpu"}``.
``arb.jsonl``         optional: the motor process's own record of when each move was accepted,
                      started and settled, and how many were waiting.
``calibration.json``  optional ``{"k_ms", "note"}``.

A run is *scripted* when the manifest names a played WAV (``wav_start_s``) or ``truth.json``
exists; every stream is then required. Any other run is a *real* session: only the manifest
and the trace are required, and a gate that needs a missing stream is reported as not
measurable. A real session's manifest may list no sentences, because nobody scripted them:
its sentences are then the ones the trace holds. Scripted and real sentences are judged
separately, never pooled.

What is measured
----------------
``t_voice``   scripted: ``truth.json`` plus ``k_ms``. Real: the first voice rise or onset in
              the trace at or after the sentence's event, or the rise it landed on if the
              voice was already up and no earlier sentence owns it, plus ``k_ms``. The row
              says which. This reads late, so a lag can read low: a monitor stamp is a block's
              arrival, and ``k_ms`` includes the microphone's own capture latency. The true
              lag lies between the figure and that figure plus ``k_ms`` and a capture block.
              Only a camera with sound can close the interval.
``t_move``    the first joint sample after the reference time where the head has rotated at
              least 1 degree from the pose at that time, or either antenna is at least 3
              degrees from its own. Head rotation is the angle between the two rotations
              built from roll, pitch and yaw, so wrapped angles need no special case. The
              joint samples are discrete: ``t_move`` is a sample time, so it is late by up to
              one sampling interval, which the report prints. The reference is ``t_voice``,
              moved earlier (at most 0.5 s) when the sentence's first command preceded the
              voice, and moved to the first command when older motion had already moved the
              pose, so another sentence's tail is never mistaken for this one's start. The
              arbiter runs commands one at a time, so when an older command is still running
              at that reference, this sentence's motion cannot have begun: the reference moves
              to when this sentence's first move really started (``arb.jsonl``), or without
              that record to where the queue says the older command ends, and the row is
              marked ``overlapped``.
``lag``       ``t_move - t_voice``. ``chain`` is ``t_move`` minus the tap's own rise edge.
Delay decomposition: event to pickup, pickup to voice seen, voice seen to first command,
the first command's call duration, first command to ``t_move`` (and ``t_voice`` to voice
seen, to show what the detector adds).

What is not measurable from these streams
-----------------------------------------
* SG-5 needs the authored start of each recorded move, which no stream carries.
* SG-6 needs the arbiter's own queue trace (``arb.jsonl``). Without it ``moves.jsonl`` is only
  a proxy: it can show a violation but can never pass the gate.
* SG-11 needs session counters and the captured learner audio, outside a run directory.
* SG-13's CPU half needs a baseline (``--against``); its size half is always measured.
* The trace holds the number of intents in a plan, not the intents. SG-8 checks that the
  command indexes issued are an ordered prefix of ``0..n-1`` (or that and the final settle),
  and that ``n`` equals the planned count. It cannot check kinds, poses or lanes.
* The measured pose at each completed move's end against its planned pose, and a plan
  recomputed from the sentence text, are not checked: neither the target pose nor the text
  is in a run.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA = 1
ARB_TAG = re.compile(r":(\d+)\.(\d+)$")
QUEUE_WAIT_S = 0.02         # a move that took longer than this to start was waiting for another
# A trajectory takes about 100 ms to reach the joints, so nothing a move commands shows sooner
# than the first of these after it starts, and motion before that belongs to something older.
# A move that cut another is read later still, because the cut move's motion runs on a moment.
START_TO_MOTION_S = 0.05
CUT_SETTLE_S = 0.09
CUT_NEAR_S = 0.05           # a move settled this close to the start is the one it cut
HEAD_DEG = 1.0
ANTENNA_DEG = 3.0
LOOKBACK_S = 0.5            # how far before t_voice a sentence's own motion may begin
WINDOW_MAX_S = 5.0          # longest wait for a first motion before "no motion"
TAP_WINDOW_S = 0.5          # scripted runs: the tap edge lies within this of t_voice
POST_SLACK_S = 0.05         # a post belongs to the command call that was running at its time
GAP_LIMIT_S = 0.25          # a longer hole in the joint samples hides a possible motion
TRACE_KB_PER_MIN_LIMIT = 30.0  # 150 KB over five minutes of steady talking
CPU_RISE_LIMIT = 1.0        # percentage points
EXCEPTION_SHARE_LIMIT = 0.05   # exclusions that happened, which could be hiding a fault
DECLARED_SHARE_LIMIT = 0.15    # exclusions the script itself asks for, by design
DEGRADED_SHARE_LIMIT = 0.01
SCRIPTED_MIN = {"sentences": 240, "runs": 4}
REAL_MIN_SENTENCES = 30
DEFAULT_TOLERANCE_MS = 50.0

HEAD_KINDS = ("goto", "look_at", "set_target", "play_recorded", "wake_up")
# What the arbiter runs one at a time, in order: each of these waits for the one before it.
QUEUED_KINDS = ("goto", "look_at", "antennas", "play_recorded", "wake_up")
QUEUE_TAIL_S = 0.05         # a move ends this long after its command says: the control loop
LATE_EVENT_S = 1.0          # how far before its event a sentence's voice may have started
QUIET_FOR_SETTLE_S = 1.0    # silence long enough that a settle must have shown by its end
PAUSE_MIN_S = 0.6           # a silence inside a sentence long enough to settle in
CAPTURE_BLOCK_MS = 20.0     # the monitor capture's block, the most its stamp can read late
QUEUE_FIELDS = ("waiting", "queued", "queue_depth", "pending", "queue")
NAMED_OUTCOMES = ("complete", "voice_end", "no_voice", "preempted", "degraded")

# Each limit is (lowest allowed, highest allowed), None for unbounded.
FINAL_LIMITS: dict[str, tuple[str, dict[str, tuple]]] = {
    "SG-1": ("lag, any motor (ms)", {"median": (-40, 40), "p95": (None, 90),
                                     "max": (None, 150), "min": (-120, None)}),
    "SG-2": ("lag, head (ms)", {"median": (-40, 70), "p95": (None, 120), "max": (None, 200)}),
    "SG-3": ("old motion after the new voice (ms)",
             {"median": (None, 30), "p95": (None, 60), "max": (None, 120)}),
    "SG-4a": ("audible end to settle, inside a reply (s)",
              {"median": (None, 0.45), "p95": (None, 0.65), "max": (None, 0.90)}),
    "SG-4b": ("audible end to settle, end of reply (s)",
              {"median": (None, 0.20), "p95": (None, 0.35), "max": (None, 0.60)}),
}
INTERIM_LIMITS: dict[str, dict[str, tuple]] = {
    "SG-1": {"median": (None, 120), "p95": (None, 200), "max": (None, 350)},
    "SG-2": {"median": (None, 170), "p95": (None, 250), "max": (None, 400)},
    "SG-4a": {"median": (None, 0.60), "p95": (None, 0.80), "max": (None, 1.10)},
}

GATE_ORDER = ["DATA", "SAMPLE", "SG-1", "SG-2", "SG-3", "SG-4a", "SG-4b", "SG-5", "SG-6",
              "SG-7", "SG-8", "SG-9", "SG-10", "SG-11", "SG-12", "SG-13", "EXC"]
GATE_SETS: dict[str, set[str]] = {
    "m0": {"DATA"},
    "m1": {"DATA", "SAMPLE", "SG-1", "SG-2", "SG-3", "SG-4a", "SG-5", "SG-6", "SG-7", "SG-8",
           "SG-9", "SG-10", "SG-12", "EXC"},
    "final": set(GATE_ORDER),
}
GATE_TITLES = {
    "DATA": "data completeness",
    "SAMPLE": "enough sentences and runs",
    "SG-5": "recorded move: voice to its first authored motion",
    "SG-6": "arbiter queue depth during speech",
    "SG-7": "identity: one plan and one outcome per spoken sentence",
    "SG-8": "plan: issued intents are an ordered prefix, a shortfall is named",
    "SG-9": "family: authored, performed and published agree",
    "SG-10": "own audio: first move inside the sentence, no overrun into the next",
    "SG-11": "voice safety (delay line)",
    "SG-12": "family coverage of the voiced time",
    "SG-13": "tracing overhead",
    "EXC": "exceptions: share excluded, and what still holds",
}
PASS, FAIL, NOT_MEASURABLE = "PASS", "FAIL", "NOT MEASURABLE"

# Exception classes: E1 no tracked session, E2 de-escalation cut the plan, E3 a safety move was
# in flight, E4 the learner or the session cut the sentence, E5 a sentence shorter than its
# plan's first move, E6 no audio, E7 no voice tap, E8 what the voice cannot tell apart: a burst
# of events, which the page sends only after a sentence fails and where only the last plan
# runs, or a sentence under the page's own covering line, which moves when the line ends.
# ``timing``: left out of the timing gates. ``plan``: how SG-8 treats it
# ("skip", "normal", "prefix" = structure only, "outcomes" = the outcome must be one of
# ``outcomes``). ``identity``: left out of SG-7. ``family``: "normal", "empty" (nothing may be
# published) or "mapped" (the published family is the performed one).
EXCEPTION_CLASSES: dict[str, dict] = {
    "E1": {"timing": True, "plan": "skip", "identity": True, "family": "empty"},
    "E2": {"timing": True, "plan": "skip", "identity": False, "family": "mapped"},
    "E3": {"timing": True, "plan": "normal", "identity": False, "family": "normal"},
    "E4": {"timing": True, "plan": "outcomes", "identity": False, "family": "normal",
           "outcomes": ("preempted", "stopped")},
    "E5": {"timing": False, "plan": "prefix", "identity": False, "family": "normal"},
    "E6": {"timing": True, "plan": "outcomes", "identity": True, "family": "normal",
           "outcomes": ("no_voice",)},
    "E7": {"timing": True, "plan": "normal", "identity": False, "family": "normal"},
    "E8": {"timing": True, "plan": "skip", "identity": True, "family": "normal"},
}


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------
def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _norm(x: Any) -> str:
    """A family or category: None and the empty string are the same absence."""

    return "" if x is None else str(x).strip().lower()


def percentile(sorted_values: list[float], q: float) -> Optional[float]:
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    a, b = sorted_values[lo], sorted_values[hi]
    if frac == 0 or a == b:
        return a
    if math.isinf(b):
        return b
    return a + (b - a) * frac


def stats(values: Iterable[Optional[float]]) -> dict:
    """median, p95, max, min over the values; ``inf`` counts as a sentence that never moved."""

    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p95": None, "max": None, "min": None, "no_motion": 0}
    return {"n": len(vals), "median": percentile(vals, 0.5), "p95": percentile(vals, 0.95),
            "max": vals[-1], "min": vals[0],
            "no_motion": sum(1 for v in vals if math.isinf(v))}


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def _rotation(roll: float, pitch: float, yaw: float) -> tuple:
    """Rz(yaw) Ry(pitch) Rx(roll) as a row-major 3x3."""

    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return ((cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
            (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
            (-sp, cp * sr, cp * cr))


def rotation_angle(a: tuple, b: tuple) -> float:
    """Angle in radians of the rotation taking ``a`` to ``b``."""

    # R = a^T b ; the angle comes from its trace and its antisymmetric part.
    r = [[sum(a[k][i] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    cos_t = (r[0][0] + r[1][1] + r[2][2] - 1.0) / 2.0
    vx, vy, vz = r[2][1] - r[1][2], r[0][2] - r[2][0], r[1][0] - r[0][1]
    sin_t = 0.5 * math.sqrt(vx * vx + vy * vy + vz * vz)
    return math.atan2(sin_t, cos_t)


def head_angle_deg(rpy_a: tuple, rpy_b: tuple) -> float:
    return math.degrees(rotation_angle(_rotation(*rpy_a), _rotation(*rpy_b)))


def antenna_angle_deg(ant_a: tuple, ant_b: tuple) -> float:
    return max(abs(math.degrees(_wrap(ant_b[0] - ant_a[0]))),
               abs(math.degrees(_wrap(ant_b[1] - ant_a[1]))))


# ---------------------------------------------------------------------------
# joints
# ---------------------------------------------------------------------------
@dataclass
class Departure:
    covered: bool
    head: Optional[float] = None
    antenna: Optional[float] = None

    @property
    def any(self) -> Optional[float]:
        found = [t for t in (self.head, self.antenna) if t is not None]
        return min(found) if found else None


class Joints:
    """Measured head pose and antennas over time."""

    def __init__(self, samples: list[tuple]) -> None:
        samples = sorted(samples, key=lambda s: s[0])
        self.t = [s[0] for s in samples]
        self.rpy = [s[1] for s in samples]
        self.ant = [s[2] for s in samples]

    def __len__(self) -> int:
        return len(self.t)

    def intervals(self) -> list[float]:
        return [b - a for a, b in zip(self.t, self.t[1:])]

    def pose_at(self, t: float) -> Optional[tuple]:
        if not self.t or t < self.t[0] or t > self.t[-1]:
            return None
        i = bisect.bisect_right(self.t, t)
        if i == 0:
            return self.rpy[0], self.ant[0]
        if i >= len(self.t):
            return self.rpy[-1], self.ant[-1]
        t0, t1 = self.t[i - 1], self.t[i]
        f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        rpy = tuple(a + _wrap(b - a) * f for a, b in zip(self.rpy[i - 1], self.rpy[i]))
        ant = tuple(a + _wrap(b - a) * f for a, b in zip(self.ant[i - 1], self.ant[i]))
        return rpy, ant

    def departure(self, base_t: float, until: float) -> Departure:
        """First sample after ``base_t`` (up to ``until``) that left the pose at ``base_t``."""

        base = self.pose_at(base_t)
        if base is None:
            return Departure(False)
        rpy0, ant0 = base
        found = Departure(True)
        prev = base_t
        widest = 0.0
        i = bisect.bisect_right(self.t, base_t)
        while i < len(self.t) and self.t[i] <= until:
            t = self.t[i]
            widest = max(widest, t - prev)
            prev = t
            if found.head is None and head_angle_deg(rpy0, self.rpy[i]) >= HEAD_DEG:
                found.head = t
            if found.antenna is None and antenna_angle_deg(ant0, self.ant[i]) >= ANTENNA_DEG:
                found.antenna = t
            if found.head is not None and found.antenna is not None:
                return found
            i += 1
        if found.any is None:
            # Nothing moved, but only a stream that watched the whole window can say so.
            widest = max(widest, until - prev)
            if until > self.t[-1] + GAP_LIMIT_S or widest > GAP_LIMIT_S:
                found.covered = False
        return found


# ---------------------------------------------------------------------------
# loading a run
# ---------------------------------------------------------------------------
def read_jsonl(path: Path) -> tuple[list[dict], int, bool]:
    """Records, the count of unreadable lines, and whether the file exists."""

    if not path.is_file():
        return [], 0, False
    records: list[dict] = []
    bad = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                bad += 1
    return records, bad, True


def read_json(path: Path) -> tuple[Any, Optional[str]]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (ValueError, OSError) as exc:
        return None, f"unreadable ({type(exc).__name__})"


@dataclass
class Run:
    path: Path
    name: str
    manifest: dict = field(default_factory=dict)
    scripted: bool = False
    trace: list[dict] = field(default_factory=list)
    joints: Optional[Joints] = None
    moves: list[dict] = field(default_factory=list)
    truth: dict[int, tuple[float, float]] = field(default_factory=dict)
    published: list[dict] = field(default_factory=list)
    load: list[dict] = field(default_factory=list)
    arb: dict[int, list[dict]] = field(default_factory=dict)
    arb_cuts: list[float] = field(default_factory=list)
    have: dict[str, bool] = field(default_factory=dict)
    k_ms: Optional[float] = None
    k_source: str = "none"
    voice_delay_s: float = 0.0
    trace_bytes: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def pool(self) -> str:
        return "scripted" if self.scripted else "real"

    @property
    def origin(self) -> float:
        t0 = _num(self.manifest.get("t_wav0"))
        if t0 is not None:
            return t0
        times = [t for t in (_num(r.get("t")) for r in self.trace) if t is not None]
        return min(times) if times else 0.0


def _arb_moves(records: list[dict]) -> dict[int, list[dict]]:
    """The arbiter's records as one entry per move, grouped by the plan its tag names."""

    moves: dict[str, dict] = {}
    for rec in records:
        move_id, t = rec.get("move_id"), _num(rec.get("t"))
        if not move_id or t is None:
            continue
        move = moves.setdefault(str(move_id), {"id": str(move_id), "reason": rec.get("reason", "")})
        event = rec.get("event")
        move[str(event)] = t
        if event == "accepted":
            move["queued"] = _int(rec.get("queued"))
        if event == "settled":
            move["error"] = str(rec.get("error_code") or "")
    plans: dict[int, list[dict]] = defaultdict(list)
    for move in moves.values():
        tag = ARB_TAG.search(str(move["reason"]))
        if tag and "accepted" in move:
            plans[int(tag.group(1))].append(move)
    for moved in plans.values():
        moved.sort(key=lambda m: m["accepted"])
    return dict(plans)


def load_run(path: Path, k_ms: Optional[float] = None) -> Run:
    run = Run(path=path, name=path.name)
    if not path.is_dir():
        run.problems.append(f"{path}: not a directory")
        return run

    manifest, err = read_json(path / "manifest.json")
    run.have["manifest.json"] = err is None
    if err or not isinstance(manifest, dict):
        run.problems.append(f"manifest.json {err or 'is not an object'}")
        manifest = {}
    elif manifest.get("schema") != SCHEMA:
        run.problems.append(f"manifest.json schema {manifest.get('schema')!r}, expected {SCHEMA}")
    run.manifest = manifest
    run.name = str(manifest.get("run_id") or path.name)
    sentences = manifest.get("sentences")
    if manifest and not isinstance(sentences, list):
        run.problems.append("manifest.json has no sentence list")
        manifest["sentences"] = []

    truth_obj, truth_err = read_json(path / "truth.json")
    run.have["truth.json"] = truth_err is None
    wav_played = any(isinstance(s, dict) and _num(s.get("wav_start_s")) is not None
                     for s in manifest.get("sentences", []))
    run.scripted = truth_err is None or wav_played

    def stream(name: str, required: bool) -> list[dict]:
        records, bad, exists = read_jsonl(path / name)
        run.have[name] = exists
        if not exists:
            if required:
                run.problems.append(f"{name} missing")
            return []
        if bad:
            run.problems.append(f"{name}: {bad} unreadable line(s), stream truncated or damaged")
        if not records and required:
            run.problems.append(f"{name} is empty")
        return records

    run.trace = stream("trace.jsonl", True)
    if (path / "trace.jsonl").is_file():
        run.trace_bytes = (path / "trace.jsonl").stat().st_size
    meta = [r for r in run.trace if r.get("s") == "meta"]
    if run.trace and not meta:
        run.problems.append("trace.jsonl has no meta record")
    elif meta and meta[0].get("schema") != 1:
        run.problems.append(f"trace.jsonl schema {meta[0].get('schema')!r}, expected 1")
    run.trace = [r for r in run.trace
                 if _num(r.get("t")) is not None and isinstance(r.get("s"), str)]

    samples = []
    for rec in stream("joints.jsonl", run.scripted):
        t, rpy, ant = _num(rec.get("t")), rec.get("rpy"), rec.get("ant")
        if (t is not None and isinstance(rpy, list) and len(rpy) == 3
                and isinstance(ant, list) and len(ant) == 2
                and all(_num(v) is not None for v in rpy + ant)):
            samples.append((t, tuple(float(v) for v in rpy), tuple(float(v) for v in ant)))
    if samples:
        run.joints = Joints(samples)
    elif run.have.get("joints.jsonl") and run.scripted:
        run.problems.append("joints.jsonl holds no usable sample")

    run.moves = stream("moves.jsonl", run.scripted)
    run.published = stream("published.jsonl", run.scripted)
    run.load = stream("load.jsonl", run.scripted)
    arb_records = stream("arb.jsonl", False)
    run.arb = _arb_moves(arb_records)
    run.arb_cuts = sorted(t for t, e, c in ((_num(r.get("t")), r.get("event"), r.get("error_code"))
                                            for r in arb_records)
                          if t is not None and e == "settled" and c == "preempted")

    if run.scripted:
        if truth_err:
            run.problems.append(f"truth.json {truth_err}")
        elif isinstance(truth_obj, dict) and isinstance(truth_obj.get("sentences"), list):
            for item in truth_obj["sentences"]:
                if not isinstance(item, dict):
                    continue
                tv, te = _num(item.get("t_voice")), _num(item.get("t_end"))
                if tv is not None and te is not None:
                    run.truth[_int(item.get("seq"), -1)] = (tv, te)
            if not run.truth:
                run.problems.append("truth.json holds no usable sentence")
        else:
            run.problems.append("truth.json has no sentence list")

    run.voice_delay_s = (_num(manifest.get("voice_delay_ms")) or 0.0) / 1000.0
    k = _num(manifest.get("k_ms"))
    run.k_source = "manifest" if k is not None else "none"
    cal, cal_err = read_json(path / "calibration.json")
    run.have["calibration.json"] = cal_err is None
    cal_k = _num(cal.get("k_ms")) if cal_err is None and isinstance(cal, dict) else None
    if k is None and cal_k is not None:
        k, run.k_source = cal_k, "calibration"
    if k_ms is not None:
        k, run.k_source = k_ms, "asked for"
        run.notes.append(f"k_ms {k_ms:g} was asked for on the command line, in place of the "
                         "run's own")
    elif k is None:
        run.notes.append("k_ms unknown: the tap-to-speaker offset is taken as 0")
    else:
        if isinstance(cal, dict) and cal.get("note"):
            run.notes.append(f"k_ms is an {cal['note']}")
        if run.scripted:
            run.notes.append(
                "t_voice reads late, so every lag can read low: the true lag lies between the "
                f"figure and the figure plus {k + CAPTURE_BLOCK_MS:.0f} ms (k_ms and one capture "
                "block). Only a camera with sound can close that")
    run.k_ms = k
    return run


# ---------------------------------------------------------------------------
# per-sentence analysis
# ---------------------------------------------------------------------------
@dataclass
class Row:
    run: str
    pool: str
    seq: int
    case: str = ""
    audio: bool = True
    motion_source: str = ""
    plan: int = 0
    planned: Optional[bool] = None
    authored: str = ""
    performed: str = ""
    published: Optional[str] = None
    published_category: Optional[str] = None
    expect_family: Optional[str] = None
    slug: str = ""
    recorded: str = ""
    family_source: str = ""
    kind: str = "none"
    chars: Optional[int] = None
    n_intents: Optional[int] = None
    n_batch: int = 0
    t_event: Optional[float] = None
    offset_ms: Optional[float] = None
    offset_error_ms: Optional[float] = None
    offset_planned_ms: Optional[float] = None
    t_tap: Optional[float] = None
    t_voice: Optional[float] = None
    voice_src: str = ""
    t_end: Optional[float] = None
    bind: str = "none"
    t_pickup: Optional[float] = None
    t_voice_seen: Optional[float] = None
    t_first_cmd: Optional[float] = None
    first_cmd_ms: Optional[float] = None
    t_first_accept: Optional[float] = None
    head_expected: bool = False
    move_status: str = "unmeasured"
    move_note: str = ""
    overlapped: str = ""
    t_arb_accept: Optional[float] = None
    t_arb_start: Optional[float] = None
    t_last_move_end: Optional[float] = None
    t_move: Optional[float] = None
    t_move_head: Optional[float] = None
    lag_ms: Optional[float] = None
    lag_head_ms: Optional[float] = None
    chain_ms: Optional[float] = None
    tail_ms: Optional[float] = None
    own_overrun_ms: Optional[float] = None
    last_cmd_t: Optional[float] = None
    settle_class: str = ""
    settle_s: Optional[float] = None
    settle_note: str = ""
    pauses: list = field(default_factory=list)
    pause_settle_s: Optional[float] = None
    coverage: Optional[float] = None
    outcome: str = "missing"
    exception: str = ""
    exception_source: str = ""
    load1: Optional[float] = None
    load_quartile: str = ""
    cmd_times: list = field(default_factory=list)
    motion_kinds: list = field(default_factory=list)
    segments: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    family_issues: list = field(default_factory=list)
    exception_issues: list = field(default_factory=list)
    signature: str = ""

    @property
    def klass(self) -> Optional[dict]:
        return EXCEPTION_CLASSES.get(self.exception)

    @property
    def timing_eligible(self) -> bool:
        return self.audio and not (self.klass or {}).get("timing", False)


def _is_sustain(rec: dict) -> bool:
    return "sustain" in str(rec.get("reason", "")).lower()


def _is_settle(rec: dict, n: int) -> bool:
    return n > 1 and _int(rec.get("i"), -1) == n - 1 and not _is_sustain(rec)


def _sentences_from_trace(records: list[dict]) -> list[dict]:
    """A real session has no script: its sentences are the ones the driver was told about.

    A page's repeat of a sentence already spoken is not a new one. An empty sentence stays,
    because a dropped cue is a shortfall to report.
    """

    found, seen = [], set()
    for rec in records:
        seq = _int(rec.get("seq"))
        if seq in seen or (rec.get("deduped") and _int(rec.get("chars")) > 0):
            continue
        seen.add(seq)
        found.append({"seq": seq, "case": f"seq{seq}", "chars": rec.get("chars"), "audio": True})
    return found


def _queue_ends(applies: list[dict], cuts: list[dict]) -> list[tuple[float, int, float]]:
    """``(command time, plan, when it finishes)`` for what the arbiter runs in order."""

    cut_at: dict[int, float] = {}
    for cut in cuts:
        cut_at.setdefault(_int(cut.get("plan")), cut["t"])
    found = []
    busy = -math.inf
    for a in sorted(applies, key=lambda a: a["t"]):
        if str(a.get("kind")) not in QUEUED_KINDS or a.get("accepted") is False:
            continue
        plan = _int(a.get("plan"))
        end = max(a["t"], busy) + (_num(a.get("d")) or 0.0)
        if a.get("kind") == "play_recorded" and cut_at.get(plan, -math.inf) >= a["t"]:
            end = min(end, cut_at[plan])
        busy = end
        found.append((a["t"], plan, end))
    return found


def analyse_run(run: Run) -> list[Row]:
    manifest_sentences = [s for s in run.manifest.get("sentences", []) if isinstance(s, dict)]
    by_stream: dict[str, list[dict]] = defaultdict(list)
    for rec in run.trace:
        by_stream[rec["s"]].append(rec)
    for recs in by_stream.values():
        recs.sort(key=lambda r: r["t"])
    from_trace = not manifest_sentences and not run.scripted
    if from_trace:
        manifest_sentences = _sentences_from_trace(by_stream["sentence"])

    sentences: dict[int, list[dict]] = defaultdict(list)
    for rec in by_stream["sentence"]:
        sentences[_int(rec.get("seq"))].append(rec)
    per_plan: dict[str, dict[int, list[dict]]] = {
        name: defaultdict(list)
        for name in ("pickup", "voice_seen", "apply", "recorded_cut", "batch_end")}
    for name, table in per_plan.items():
        for rec in by_stream[name]:
            table[_int(rec.get("plan"))].append(rec)
    posts = by_stream["post"]
    edges = [r for r in by_stream["voice"] if r.get("edge") in ("rise", "onset")]
    edge_times = [r["t"] for r in edges]
    falls = [r for r in by_stream["voice"] if r.get("edge") == "fall"]
    tts_ended = [r["t"] for r in by_stream["tts_ended"]]
    later_activity = sorted([r["t"] for r in by_stream["sentence"]] + tts_ended)

    # An apply owns the failed posts that landed during its call: the record carries no plan.
    all_applies = sorted((a for recs in per_plan["apply"].values() for a in recs),
                         key=lambda a: a["t"])
    apply_starts = [a["t"] for a in all_applies]
    posts_of: dict[int, list[dict]] = defaultdict(list)
    for post in posts:
        ai = bisect.bisect_right(apply_starts, post["t"]) - 1
        if ai >= 0:
            a = all_applies[ai]
            if post["t"] <= a["t"] + (_num(a.get("ms")) or 0.0) / 1000.0 + POST_SLACK_S:
                posts_of[id(a)].append(post)

    published_by_seq: dict[int, list[dict]] = defaultdict(list)
    for rec in run.published:
        published_by_seq[_int(rec.get("seq"), -1)].append(rec)
    k_s = (run.k_ms or 0.0) / 1000.0

    rows: list[Row] = []
    for m in manifest_sentences:
        seq = _int(m.get("seq"), -1)
        row = Row(run=run.name, pool=run.pool, seq=seq, case=str(m.get("case") or f"seq{seq}"),
                  audio=bool(m.get("audio", True)),
                  motion_source=str(run.manifest.get("motion_source") or ""))
        row.chars = _int(m.get("chars"), -1) if m.get("chars") is not None else None
        row.offset_planned_ms = _num(m.get("event_offset_ms"))
        row.pauses = m.get("pauses") or []
        row.expect_family = (None if m.get("expect_family") in (None, "")
                             else _norm(m.get("expect_family")))
        explicit = m.get("expected_exception")
        if explicit:
            row.exception, row.exception_source = str(explicit), "manifest"
            if row.exception not in EXCEPTION_CLASSES:
                row.exception_issues.append(f"unknown exception class {row.exception!r}")
                row.exception = ""
        rows.append(row)

        found = sentences.get(seq, [])
        if not found:
            row.links.append("no sentence record for this seq")
            row.outcome = "missing"
            continue
        if len(found) > 1:
            row.links.append(f"{len(found)} sentence records for this seq")
        rec = found[0]
        row.t_event = rec["t"]
        row.plan = _int(rec.get("plan"))
        row.authored = _norm(rec.get("authored"))
        if row.chars is not None and _int(rec.get("chars"), -2) != row.chars:
            row.links.append(f"chars {rec.get('chars')} in the trace, {row.chars} in the manifest")
        if rec.get("deduped"):
            row.outcome = "deduped"
            row.links.append("sentence was dropped as a duplicate, no plan was made")
            continue

        row.performed = _norm(rec.get("performed"))
        row.slug = str(rec.get("slug") or "")
        row.family_source = str(rec.get("family_source") or "")
        row.recorded = str(rec.get("recorded") or "")
        row.n_intents = _int(rec.get("intents"), -1) if rec.get("intents") is not None else None
        row.planned = bool(rec.get("planned"))
        row.kind = ("recorded" if row.recorded else "family" if row.performed else "talking")
        if not row.planned:
            row.kind = "none"
            row.outcome = "not_owner"
            continue
        _join_plan(row, per_plan, posts_of, later_activity)
        if from_trace and row.outcome == "no_voice":
            row.audio = False      # with no script, the trace is the only word on a voice

    _assign_voice_times(rows, run, edges, edge_times, falls, k_s)
    _check_family(rows, run, published_by_seq)
    _measure_motion(rows, run, by_stream, tts_ended, later_activity, k_s)
    _decompose(rows)
    _attach_load(rows, run)
    _apply_exceptions(rows)
    for row in rows:
        row.signature = _signature(row, per_plan.get("apply", {}))
    return rows


def _join_plan(row: Row, per_plan: dict, posts_of: dict, later: list) -> None:
    plan = row.plan
    pickups = per_plan["pickup"].get(plan, [])
    ends = per_plan["batch_end"].get(plan, [])
    applies = sorted(per_plan["apply"].get(plan, []), key=lambda a: a["t"])
    seen = per_plan["voice_seen"].get(plan, [])
    if plan <= 0:
        row.links.append("sentence has no plan id")
        return
    if len(pickups) != 1:
        row.links.append(f"{len(pickups)} pickup records for plan {plan}")
    if len(ends) != 1:
        row.links.append(f"{len(ends)} batch_end records for plan {plan}")
    if not applies:
        row.links.append(f"no apply records for plan {plan}")
    if len(seen) > 1:
        row.links.append(f"{len(seen)} voice_seen records for plan {plan}")
    if not pickups or not ends:
        row.outcome = "missing"
        return

    pickup, end = pickups[0], ends[0]
    n = _int(pickup.get("n"))
    row.n_batch = n
    row.t_pickup = pickup["t"]
    row.bind = str(seen[0].get("bind")) if seen else "none"
    row.t_voice_seen = seen[0]["t"] if seen else None

    motion = [a for a in applies if not _is_sustain(a) and not _is_settle(a, n)]
    row.cmd_times = [a["t"] for a in applies if not _is_settle(a, n)]
    row.motion_kinds = [str(a.get("kind")) for a in motion]
    moving = [a for a in applies if not _is_settle(a, n)]
    if moving:
        row.last_cmd_t = max(a["t"] for a in moving)
    cmds = [a for a in applies if not _is_settle(a, n)] or applies
    if cmds:
        first = cmds[0]
        row.t_first_cmd = first["t"]
        row.first_cmd_ms = _num(first.get("ms"))
    for a in applies:
        if a.get("accepted") is not False and not posts_of.get(id(a)):
            row.t_first_accept = a["t"]
            break

    issued = [_int(a.get("i"), -1) for a in applies if not _is_sustain(a)]
    while len(issued) > 1 and issued[-1] == issued[-2] == n - 1:
        issued.pop()
    contiguous = issued == list(range(len(issued)))
    skipped = (len(issued) >= 1 and issued[-1] == n - 1 and n > 1
               and issued[:-1] == list(range(len(issued) - 1)))
    if not (contiguous or skipped):
        row.violations.append(f"issued indexes {issued} are not an ordered prefix of 0..{n - 1}")
    if row.n_intents is not None and row.n_intents != n:
        row.violations.append(f"plan has {row.n_intents} intents, the batch carried {n}")
    for a in applies:
        if a.get("accepted") is False:
            row.violations.append(f"recorded move refused at intent {a.get('i')}")
        for p in posts_of.get(id(a), []):
            status = _int(p.get("status"))
            if status == 0 or status >= 400:
                row.violations.append(f"{p.get('path')} answered {status}")

    revoked = bool(end.get("revoked"))
    voice_seen = bool(end.get("voice_seen"))
    complete = contiguous and bool(issued) and issued[-1] == n - 1
    # A plan normally ends revoked: the next sentence or the end of the turn does it, and by
    # then nothing is left of it but the settle, which the turn's own settle replaces.
    performed = contiguous and bool(issued) and issued[-1] >= n - 2
    if pickup.get("speech") and not pickup.get("voice"):
        row.outcome = "degraded"
    elif not row.audio and not voice_seen:
        row.outcome = "no_voice"
        if issued not in ([], [n - 1]):
            row.violations.append("motion was commanded for a sentence whose voice never came")
    elif revoked and not performed:
        row.outcome = "preempted"
        if not any(t > row.t_pickup for t in later):
            row.outcome = "preempted_unexplained"
    elif not voice_seen:
        row.outcome = "no_voice"
        if issued not in ([], [n - 1]):
            row.violations.append("motion was commanded for a sentence whose voice never came")
    elif complete or (revoked and performed):
        row.outcome = "complete"
    elif skipped:
        row.outcome = "voice_end"
    else:
        row.outcome = "stopped"


def _assign_voice_times(rows: list[Row], run: Run, edges: list, edge_times: list,
                        falls: list, k_s: float) -> None:
    audible = [r for r in rows if r.audio and r.t_event is not None]
    if run.scripted:
        for row in audible:
            truth = run.truth.get(row.seq)
            if truth is None:
                row.links.append("no truth record for this seq")
                continue
            row.t_voice, row.t_end = truth[0] + k_s, truth[1] + k_s
            row.voice_src = "truth"
            if row.offset_planned_ms is not None:
                row.offset_error_ms = ((row.t_event - truth[0] + run.voice_delay_s) * 1000.0
                                       - row.offset_planned_ms)
            # The tap hears syllables four times a second: the nearest is this sentence's.
            lo = bisect.bisect_left(edge_times, truth[0] - TAP_WINDOW_S)
            hi = bisect.bisect_right(edge_times, truth[0] + TAP_WINDOW_S)
            if lo < hi:
                row.t_tap = min(edge_times[lo:hi], key=lambda t: abs(t - truth[0]))
        return

    previous = -math.inf
    # A sentence that was dropped or never spoke would take the next one's edge, and that one
    # a later syllable's, on down the reply.
    ordered = sorted((r for r in audible if r.outcome not in ("deduped", "no_voice")),
                     key=lambda r: r.t_event)
    rises = [e["t"] for e in edges if e.get("edge") == "rise"]
    fall_times = sorted(f["t"] for f in falls)
    for row in ordered:
        # An event posted late lands with its voice already up, and the first edge after it
        # would be a later syllable, which reads as a smaller lag.
        r = bisect.bisect_right(rises, row.t_event) - 1
        rise = rises[r] if r >= 0 else -math.inf
        fall_since = bisect.bisect_left(fall_times, rise) < bisect.bisect_right(fall_times,
                                                                                row.t_event)
        if rise > previous and row.t_event - rise <= LATE_EVENT_S and not fall_since:
            row.t_tap = rise
        else:
            i = bisect.bisect_left(edge_times, max(row.t_event, previous + 1e-6))
            if i >= len(edge_times):
                continue
            row.t_tap = edge_times[i]
        previous = row.t_tap
        row.t_voice, row.voice_src = row.t_tap + k_s, "tap"
    taps = [r for r in ordered if r.t_tap is not None]
    for j, row in enumerate(taps):
        bound = taps[j + 1].t_tap if j + 1 < len(taps) else math.inf
        mine = [f for f in falls if row.t_tap < f["t"] < bound and _num(f.get("last")) is not None]
        if mine:
            row.t_end = _num(mine[-1].get("last")) + k_s


def _check_family(rows: list[Row], run: Run, published_by_seq: dict) -> None:
    manifest = {_int(s.get("seq"), -1): s for s in run.manifest.get("sentences", [])
                if isinstance(s, dict)}
    have_published = run.have.get("published.jsonl", False)
    for row in rows:
        if not row.audio:
            continue
        given = manifest.get(row.seq, {}).get("family")
        if row.seq in manifest and row.t_event is not None and row.authored != _norm(given):
            row.family_issues.append(
                f"authored {row.authored or '-'} reached the driver, manifest says "
                f"{_norm(given) or '-'}")
        pubs = published_by_seq.get(row.seq, [])
        if len(pubs) > 1 and len({(_norm(p.get("cue_family")), _norm(p.get("cue_category")))
                                  for p in pubs}) > 1:
            row.family_issues.append("published twice with different families")
        if pubs:
            row.published = _norm(pubs[-1].get("cue_family"))
            row.published_category = _norm(pubs[-1].get("cue_category"))
        elif have_published and row.exception != "E1":
            row.family_issues.append("nothing was published for this seq")
        if row.outcome in ("missing", "deduped") or row.exception == "E1":
            continue
        if row.expect_family is not None and row.performed != row.expect_family:
            row.family_issues.append(
                f"performed {row.performed or '-'}, expected {row.expect_family}")
        if row.published is not None and row.planned:
            if row.published != row.performed:
                row.family_issues.append(
                    f"published family {row.published or '-'} differs from performed "
                    f"{row.performed or '-'}")
            if row.published_category != row.performed:
                row.family_issues.append(
                    f"published category {row.published_category or '-'} differs from performed "
                    f"{row.performed or '-'}")


def _pause_settle(row: Row, joints: Joints, applies: list[dict], origin: float,
                  k_s: float) -> None:
    """How long a silence inside the sentence lasted before the head settled.

    A settle that never showed counts as the whole silence, which is the least it can be.
    """

    for a, b in row.pauses:
        if b - a < PAUSE_MIN_S:
            continue
        start, end = origin + a + k_s, origin + b + k_s
        row.pause_settle_s = b - a
        commands = [c["t"] for c in applies if _int(c.get("plan")) == row.plan
                    and _is_settle(c, row.n_batch) and start - LOOKBACK_S <= c["t"] < end]
        if commands:
            dep = joints.departure(commands[0], end)
            if dep.covered and dep.any is not None:
                row.pause_settle_s = max(0.0, dep.any - start)


def _measure_motion(rows: list[Row], run: Run, by_stream: dict, tts_ended: list,
                    later_activity: list, k_s: float) -> None:
    joints = run.joints
    voiced = sorted((r for r in rows if r.audio and r.t_voice is not None),
                    key=lambda r: r.t_voice)
    next_of: dict[int, Optional[Row]] = {}
    for a, b in zip(voiced, voiced[1:]):
        next_of[id(a)] = b
    plan0 = sorted((a for a in by_stream["apply"] if _int(a.get("plan")) == 0),
                   key=lambda a: a["t"])
    queue = _queue_ends(by_stream["apply"], by_stream["recorded_cut"])

    for k, row in enumerate(voiced):
        nxt = next_of.get(id(row))
        tv = row.t_voice
        if row.t_event is not None:
            row.offset_ms = (row.t_event - tv) * 1000.0
        if joints is None:
            row.move_status = "no joints"
            continue
        if not row.cmd_times:
            # Nothing was commanded for this sentence, so no motion can be its own.
            row.move_status = "no commanded motion"
            row.lag_ms = math.inf
            _settle(row, nxt, joints, tts_ended, plan0, by_stream)
            continue
        until = tv + WINDOW_MAX_S
        if nxt is not None and nxt.t_voice > tv:
            # Motion the next sentence starts ahead of its own voice is not this one's.
            until = min(until, nxt.t_voice)
            if nxt.cmd_times and nxt.cmd_times[0] > tv:
                until = min(until, nxt.cmd_times[0])
        base = tv
        first_cmd = min(row.cmd_times)
        if first_cmd < tv:
            base = max(first_cmd, tv - LOOKBACK_S)
        elif first_cmd > tv:
            earlier = joints.departure(tv, first_cmd)
            if earlier.covered and earlier.any is not None:
                base = first_cmd
                row.move_note = "reference moved to the first command: older motion"
        row.head_expected = any(kind in HEAD_KINDS for kind in row.motion_kinds)
        moves = run.arb.get(row.plan, [])
        if moves:
            row.t_arb_accept = moves[0]["accepted"]
            started = [m["started"] for m in moves if "started" in m]
            row.t_arb_start = started[0] if started else None
            ended = [m["settled"] for m in moves if "settled" in m]
            row.t_last_move_end = max(ended) if ended else None
        if run.arb and row.t_arb_start is None:
            # Commanded, and the robot's own record says none of it ran. Any motion in the
            # joints here is another move's.
            row.move_status = "no move started"
            row.lag_ms = math.inf
            if row.head_expected:
                row.lag_head_ms = math.inf
            _settle(row, nxt, joints, tts_ended, plan0, by_stream)
            continue
        if run.arb:
            cut_one = any(abs(at - row.t_arb_start) <= CUT_NEAR_S for at in run.arb_cuts)
            older_end = row.t_arb_start + (CUT_SETTLE_S if cut_one else START_TO_MOTION_S)
            waited = (row.t_arb_start is not None
                      and row.t_arb_start - row.t_arb_accept > QUEUE_WAIT_S)
        else:
            older_end = max((end for at, plan, end in queue
                             if plan != row.plan and at <= first_cmd),
                            default=-math.inf) + QUEUE_TAIL_S
            waited = older_end > base
        if older_end > base:
            # That command is still running, so a departure from the pose here would be its
            # motion. This sentence's own cannot start before it ends.
            base = older_end
            if waited:
                row.overlapped = "older motion in flight"
                row.move_note = ("reference moved to the end of the older motion queued "
                                 "before it")
            if base >= until:
                row.move_status = "behind older motion for the whole window"
                row.lag_ms = (base - tv) * 1000.0
                if row.head_expected:
                    row.lag_head_ms = row.lag_ms
                _settle(row, nxt, joints, tts_ended, plan0, by_stream)
                continue
        dep = joints.departure(base, until)
        if not dep.covered:
            row.move_status = "joints do not cover the window"
        elif dep.any is None:
            row.move_status = "no motion"
            row.lag_ms = math.inf
            if row.head_expected:
                row.lag_head_ms = math.inf
        else:
            row.move_status = "moved"
            row.t_move, row.t_move_head = dep.any, dep.head
            row.lag_ms = (dep.any - tv) * 1000.0
            if dep.head is not None:
                row.lag_head_ms = (dep.head - tv) * 1000.0
            elif row.head_expected:
                row.lag_head_ms = math.inf
            if row.t_tap is not None:
                row.chain_ms = (dep.any - row.t_tap) * 1000.0

        _settle(row, nxt, joints, tts_ended, plan0, by_stream)

    for k, row in enumerate(voiced):
        # SG-3 reads against every earlier sentence; SG-10 against the next one.
        later = [end for end in ((o.t_last_move_end if run.arb else o.last_cmd_t)
                                 for o in voiced[:k])
                 if end is not None and end > row.t_voice]
        if k > 0:
            row.tail_ms = (max(later) - row.t_voice) * 1000.0 if later else 0.0
        nxt = next_of.get(id(row))
        if nxt is not None and row.last_cmd_t is not None:
            row.own_overrun_ms = max(0.0, (row.last_cmd_t - nxt.t_voice) * 1000.0)
        elif nxt is not None:
            row.own_overrun_ms = 0.0
        _coverage(row, by_stream)
        if joints is not None:
            _pause_settle(row, joints, by_stream["apply"], run.origin, k_s)


def _settle(row: Row, nxt: Optional[Row], joints: Joints, tts_ended: list, plan0: list,
            by_stream: dict) -> None:
    if row.t_end is None:
        row.settle_note = "no audible end"
        return
    boundary = nxt.t_voice if nxt is not None else math.inf
    ends_reply = nxt is None or any(row.t_end - 0.5 <= t < boundary for t in tts_ended)
    row.settle_class = "4b" if ends_reply else "4a"
    n = row.n_batch
    candidates = [a["t"] for a in by_stream["apply"]
                  if _int(a.get("plan")) == row.plan and _is_settle(a, n)
                  and a["t"] >= row.t_end - LOOKBACK_S]
    if ends_reply:
        marks = [t for t in tts_ended if row.t_end - 0.5 <= t < boundary]
        if marks:
            candidates += [a["t"] for a in plan0 if a["t"] >= marks[0] - 0.05
                           and a["t"] < boundary]
    if not candidates:
        row.settle_note = "no settle command"
        if _settle_owed(row, boundary, joints):
            row.settle_s = math.inf
        return
    base = max(min(candidates), row.t_end - LOOKBACK_S)
    until = min(boundary, row.t_end + 3.0)
    dep = joints.departure(base, until)
    if not dep.covered:
        row.settle_note = "joints do not cover the settle"
    elif dep.any is None:
        row.settle_note = "settle not visible: no motion after the command"
        if _settle_owed(row, boundary, joints):
            row.settle_s = math.inf
    else:
        row.settle_s = dep.any - row.t_end


def _settle_owed(row: Row, boundary: float, joints: Joints) -> bool:
    """Should this sentence's settle have shown? A head left away from where it started, with
    a long enough silence after it, and no later sentence that took over from it."""

    if row.outcome == "preempted" or boundary - row.t_end < QUIET_FOR_SETTLE_S:
        return False
    start, end = joints.pose_at(row.t_voice), joints.pose_at(row.t_end)
    if start is None or end is None:
        return False
    return (head_angle_deg(start[0], end[0]) >= HEAD_DEG
            or antenna_angle_deg(start[1], end[1]) >= ANTENNA_DEG)


def _coverage(row: Row, by_stream: dict) -> None:
    if not row.performed or row.t_end is None or row.t_voice is None:
        return
    span = row.t_end - row.t_voice
    if span <= 0:
        return
    n = row.n_batch
    spans = []
    for a in by_stream["apply"]:
        if _int(a.get("plan")) != row.plan or _is_settle(a, n):
            continue
        if _is_sustain(a) and row.performed not in str(a.get("reason", "")).lower():
            continue
        d = _num(a.get("d")) or 0.0
        lo, hi = max(a["t"], row.t_voice), min(a["t"] + d, row.t_end)
        if hi > lo:
            spans.append((lo, hi))
    covered, edge = 0.0, row.t_voice
    for lo, hi in sorted(spans):
        lo = max(lo, edge)
        if hi > lo:
            covered += hi - lo
            edge = hi
    row.coverage = covered / span


def _decompose(rows: list[Row]) -> None:
    for row in rows:
        seg: dict[str, float] = {}

        def put(name: str, a: Optional[float], b: Optional[float], scale: float = 1000.0) -> None:
            if a is not None and b is not None:
                seg[name] = (b - a) * scale

        put("event_to_pickup", row.t_event, row.t_pickup)
        put("pickup_to_voice_seen", row.t_pickup, row.t_voice_seen)
        put("voice_to_voice_seen", row.t_voice, row.t_voice_seen)
        put("voice_seen_to_first_command", row.t_voice_seen, row.t_first_cmd)
        if row.first_cmd_ms is not None:
            seg["command_duration"] = row.first_cmd_ms
        put("first_command_to_move", row.t_first_cmd, row.t_move)
        put("accepted_to_started", row.t_arb_accept, row.t_arb_start)
        put("started_to_move", row.t_arb_start, row.t_move)
        row.segments = seg


def _attach_load(rows: list[Row], run: Run) -> None:
    samples = sorted(((t, v) for t, v in ((_num(r.get("t")), _num(r.get("load1")))
                                           for r in run.load) if t is not None and v is not None))
    if not samples:
        return
    times = [t for t, _ in samples]
    for row in rows:
        at = row.t_voice if row.t_voice is not None else row.t_event
        if at is None:
            continue
        i = bisect.bisect_left(times, at)
        near = [j for j in (i - 1, i) if 0 <= j < len(times)]
        best = min(near, key=lambda j: abs(times[j] - at))
        if abs(times[best] - at) <= 5.0:
            row.load1 = samples[best][1]


def _apply_exceptions(rows: list[Row]) -> None:
    for row in rows:
        if not row.exception:
            if not row.audio:
                row.exception, row.exception_source = "E6", "no audio"
            elif row.outcome == "degraded":
                row.exception, row.exception_source = "E7", "voice tap unavailable"
        klass = row.klass
        if not klass:
            continue
        if klass["family"] == "empty" and (row.published or row.published_category):
            row.exception_issues.append("E1: a family was published")
        if klass["family"] == "mapped" and row.published is not None \
                and row.published != row.performed:
            row.exception_issues.append("E2: the published family is not the performed one")
        if klass["plan"] == "outcomes" and row.outcome not in klass["outcomes"]:
            row.exception_issues.append(
                f"{row.exception}: outcome {row.outcome}, expected {'/'.join(klass['outcomes'])}")
        if (row.exception == "E7" and row.outcome != "degraded"
                and row.exception_source == "manifest"):
            row.exception_issues.append("E7: the voice tap was available")


def _signature(row: Row, applies_by_plan: dict) -> str:
    if row.plan <= 0 or row.outcome in ("missing", "deduped"):
        return f"{row.performed}|{row.outcome}"
    first_of: dict[int, dict] = {}
    for a in sorted(applies_by_plan.get(row.plan, []), key=lambda a: a["t"]):
        if not _is_sustain(a):
            first_of.setdefault(_int(a.get("i")), a)
    steps = ",".join(f"{a.get('kind')}:{a.get('reason')}" for _, a in sorted(first_of.items()))
    return f"{row.performed}|{int(bool(row.planned))}|{row.n_intents}|{row.recorded}|{steps}"


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------
INFO = "INFO"


@dataclass
class Gate:
    id: str
    title: str
    status: str
    detail: list[str] = field(default_factory=list)
    pools: dict = field(default_factory=dict)
    selected: bool = False


def _combine(statuses: list[str]) -> str:
    live = [s for s in statuses if s != INFO]
    if not live:
        return INFO if statuses else NOT_MEASURABLE
    if FAIL in live:
        return FAIL
    if NOT_MEASURABLE in live:
        return NOT_MEASURABLE
    return PASS


def _g(v: Optional[float]) -> str:
    if v is None:
        return "-"
    if math.isinf(v):
        return "no motion"
    return f"{v:.3g}" if abs(v) < 10 else f"{v:.0f}"


def _limit_failures(st: dict, limits: dict, *, full: bool) -> list[str]:
    """Which of median, p95 and worst break their limits; ``full`` adds the worst case."""

    out = []
    keys = ("median", "p95", "max", "min") if full else ("median", "p95")
    for key in keys:
        if key not in limits or st.get(key) is None:
            continue
        lo, hi = limits[key]
        v = st[key]
        if hi is not None and v > hi:
            out.append(f"{key} {_g(v)} above {_g(hi)}")
        if lo is not None and v < lo:
            out.append(f"{key} {_g(v)} below {_g(lo)}")
    return out


def _stat_line(pool: str, st: dict) -> str:
    return (f"{pool}: n={st['n']} median {_g(st['median'])} p95 {_g(st['p95'])} "
            f"worst {_g(st['max'])} best {_g(st['min'])}"
            + (f" ({st['no_motion']} never moved)" if st["no_motion"] else ""))


def _metric_gate(gid: str, title: str, pools: dict, getter: Callable, limits: dict, *,
                 eligible: Callable = lambda r: True, strict: bool = True) -> Gate:
    """A distribution gate: median, p95 and worst of one value per eligible sentence."""

    gate = Gate(gid, title, NOT_MEASURABLE)
    statuses = []
    for pool, rows in pools.items():
        values, unmeasured, reasons = [], 0, Counter()
        for r in rows:
            if not r.timing_eligible or not eligible(r):
                continue
            v = getter(r)
            if v is None:
                unmeasured += 1
                reasons[r.settle_note if gid.startswith("SG-4") else r.move_status] += 1
            else:
                values.append(v)
        st = stats(values)
        gate.pools[pool] = st
        why = ", ".join(f"{k or '?'} x{n}" for k, n in reasons.items())
        if not values:
            statuses.append(NOT_MEASURABLE)
            gate.detail.append(f"{pool}: nothing measured ({why or 'no eligible sentence'})")
            continue
        fails = _limit_failures(st, limits, full=(pool == "scripted"))
        line = _stat_line(pool, st)
        if fails:
            statuses.append(FAIL)
            line += " -> " + "; ".join(fails)
        elif unmeasured and strict:
            statuses.append(NOT_MEASURABLE)
            line += f" -> {unmeasured} eligible sentence(s) unmeasured: {why}"
        else:
            statuses.append(PASS)
            if unmeasured:
                line += f" ({unmeasured} unmeasured: {why})"
        if pool == "real":
            line += " [worst reported, not gated]"
        gate.detail.append(line)
    gate.status = _combine(statuses)
    return gate


def _count_gate(gid: str, title: str, pools: dict, eligible: Callable,
                failure: Callable) -> Gate:
    """A gate that wants every eligible sentence clean; ``failure`` returns the reasons."""

    gate = Gate(gid, title, NOT_MEASURABLE)
    statuses = []
    for pool, rows in pools.items():
        el = [r for r in rows if eligible(r)]
        bad = [(r, failure(r)) for r in el]
        bad = [(r, why) for r, why in bad if why]
        gate.pools[pool] = {"eligible": len(el), "failed": len(bad)}
        if not el:
            statuses.append(NOT_MEASURABLE)
            gate.detail.append(f"{pool}: no eligible sentence")
            continue
        statuses.append(FAIL if bad else PASS)
        gate.detail.append(f"{pool}: {len(el) - len(bad)}/{len(el)} sentences clean")
        for r, why in bad[:12]:
            gate.detail.append(f"  {r.run} seq {r.seq} ({r.case}): " + "; ".join(why))
        if len(bad) > 12:
            gate.detail.append(f"  ... and {len(bad) - 12} more")
    gate.status = _combine(statuses)
    return gate


def _sg7_failure(r: Row) -> list[str]:
    why = list(r.links)
    if not why and r.outcome in ("missing", "deduped"):
        why.append(f"outcome {r.outcome}")
    return why


def _sg8_failure(r: Row) -> list[str]:
    klass = r.klass
    mode = klass["plan"] if klass else "normal"
    if mode == "skip":
        return []
    why = list(r.violations)
    if r.outcome in ("missing", "deduped", "not_owner"):
        why.append(f"outcome {r.outcome}")
    elif mode == "outcomes":
        if r.outcome not in klass["outcomes"]:
            why.append(f"outcome {r.outcome}")
    elif mode == "normal" and r.outcome not in NAMED_OUTCOMES:
        why.append(f"outcome {r.outcome} is not a named shortfall")
    return why


def _gate_data(runs: list[Run], rows: list[Row]) -> Gate:
    gate = Gate("DATA", GATE_TITLES["DATA"], PASS)
    problems = []
    for run in runs:
        problems += [f"{run.name}: {p}" for p in run.problems]
        if not any(r.run == run.name for r in rows):
            problems.append(f"{run.name}: neither the manifest nor the trace lists a sentence")
    for r in rows:
        if r.pool != "scripted" or not r.audio:
            continue
        if r.t_event is not None and r.t_voice is None:
            problems.append(f"{r.run} seq {r.seq}: no truth record, t_voice unknown")
        if r.move_status.startswith("joints do not cover"):
            problems.append(f"{r.run} seq {r.seq}: {r.move_status}")
    gate.detail = problems[:40] or ["every required stream is present and readable"]
    if len(problems) > 40:
        gate.detail.append(f"... and {len(problems) - 40} more")
    if problems:
        gate.status = FAIL
    return gate


def _gate_sample(runs: list[Run], rows: list[Row]) -> Gate:
    gate = Gate("SAMPLE", GATE_TITLES["SAMPLE"], PASS)
    scripted = [r for r in rows if r.pool == "scripted"]
    loaded = [r for r in runs if r.scripted and str(r.manifest.get("profile")) == "loaded"]
    if any(r.scripted for r in runs):
        ok = len(scripted) >= SCRIPTED_MIN["sentences"] and len(loaded) >= SCRIPTED_MIN["runs"]
        gate.detail.append(
            f"scripted: {len(scripted)} sentences over {len(loaded)} loaded run(s); needs "
            f"{SCRIPTED_MIN['sentences']} over {SCRIPTED_MIN['runs']}")
        if not ok:
            gate.status = FAIL
    real = [r for r in rows if r.pool == "real"]
    if any(not r.scripted for r in runs):
        gate.detail.append(f"real: {len(real)} sentences; needs {REAL_MIN_SENTENCES}")
        if len(real) < REAL_MIN_SENTENCES:
            gate.status = FAIL
    if not gate.detail:
        gate.status = NOT_MEASURABLE
    return gate


def _gate_sg6(runs: list[Run], rows: list[Row]) -> Gate:
    gate = Gate("SG-6", GATE_TITLES["SG-6"], NOT_MEASURABLE)
    worst_wait: Optional[float] = None
    worst_running: Optional[float] = None
    if runs and all(run.arb for run in runs):
        return _gate_sg6_measured(runs, rows, gate)
    for run in runs:
        spans = [(r.t_voice, r.t_end) for r in rows if r.run == run.name
                 and r.t_voice is not None and r.t_end is not None]
        for rec in run.moves:
            t = _num(rec.get("t"))
            if t is None or not any(a <= t <= b for a, b in spans):
                continue
            running = _num(rec.get("running"))
            if running is not None:
                worst_running = max(worst_running or 0.0, running)
            for key in QUEUE_FIELDS:
                v = _num(rec.get(key))
                if v is not None:
                    worst_wait = max(worst_wait or 0.0, v)
    gate.detail.append(
        "no arbiter trace in these runs: moves.jsonl is a 10 Hz poll, so queue depth here is a "
        "proxy; it can show a violation but cannot pass the gate")
    if worst_wait is not None:
        gate.detail.append(f"proxy: most waiting during speech {_g(worst_wait)} (limit 1)")
        if worst_wait > 1:
            gate.status = FAIL
    elif worst_running is not None:
        gate.detail.append(f"proxy: most running during speech {_g(worst_running)}; the poll "
                           "carries no waiting count")
    else:
        gate.detail.append("no poll sample fell inside a sentence's voiced interval")
    return gate


def _gate_sg6_measured(runs: list[Run], rows: list[Row], gate: Gate) -> Gate:
    worst = 0
    for run in runs:
        spans = [(r.t_voice, r.t_end) for r in rows if r.run == run.name
                 and r.t_voice is not None and r.t_end is not None]
        for moves in run.arb.values():
            for move in moves:
                if any(a <= move["accepted"] <= b for a, b in spans):
                    worst = max(worst, move.get("queued", 0))
    gate.detail.append(f"arbiter trace: most queued during speech {worst} (limit 1)")
    gate.status = FAIL if worst > 1 else PASS
    return gate


def _cpu_medians(runs: list[Run]) -> dict:
    out = {}
    for key in ("hostapp_cpu", "pipewire_cpu"):
        vals = sorted(v for run in runs for v in (_num(r.get(key)) for r in run.load)
                      if v is not None)
        out[key] = percentile(vals, 0.5)
    return out if any(v is not None for v in out.values()) else {}


def _trace_minutes(run: Run) -> float:
    times = [r["t"] for r in run.trace]
    return max(0.5, (max(times) - min(times)) / 60.0) if times else 0.5


def _gate_sg13(runs: list[Run], baseline: Optional[dict]) -> Gate:
    gate = Gate("SG-13", GATE_TITLES["SG-13"], NOT_MEASURABLE)
    sizes = [(r.name, r.trace_bytes / 1024.0, _trace_minutes(r)) for r in runs
             if r.have.get("trace.jsonl")]
    over = [name for name, kb, minutes in sizes if kb / minutes > TRACE_KB_PER_MIN_LIMIT]
    gate.detail.append(
        "trace size: " + (", ".join(f"{n} {kb:.0f} KB in {minutes:.1f} min"
                                    for n, kb, minutes in sizes) or "no trace")
        + f" (limit {TRACE_KB_PER_MIN_LIMIT:g} KB a minute)")
    cpu = _cpu_medians(runs)
    base_cpu = (baseline or {}).get("cpu")
    cpu_status = NOT_MEASURABLE
    deltas = {k: cpu[k] - base_cpu[k] for k in cpu
              if base_cpu and base_cpu.get(k) is not None and cpu[k] is not None}
    if deltas:
        gate.detail.append("cpu against the baseline: " + ", ".join(
            f"{k} {d:+.1f} points" for k, d in deltas.items()) + f" (limit +{CPU_RISE_LIMIT:g})")
        cpu_status = FAIL if any(d > CPU_RISE_LIMIT for d in deltas.values()) else PASS
    else:
        gate.detail.append("cpu needs a baseline (--against) and load.jsonl: not measured")
    if over or cpu_status == FAIL:
        gate.status = FAIL
    elif sizes and cpu_status == PASS:
        gate.status = PASS
    return gate


def _gate_exceptions(runs: list[Run], rows: list[Row]) -> Gate:
    gate = Gate("EXC", GATE_TITLES["EXC"], PASS)
    for run in runs:
        mine = [r for r in rows if r.run == run.name]
        if not mine:
            continue
        by_class = Counter(r.exception for r in mine if r.exception)
        declared = sum(1 for r in mine if r.exception not in ("", "E1")
                       and r.exception_source == "manifest")
        other = sum(1 for r in mine if r.exception not in ("", "E1")
                    and r.exception_source != "manifest")
        share = other / len(mine)
        declared_share = declared / len(mine)
        gate.detail.append(
            f"{run.name}: {len(mine)} sentences, excluded "
            + (", ".join(f"{c} x{n}" for c, n in sorted(by_class.items())) or "none")
            + f"; share other than E1 and not in the script {share:.1%} "
            + f"(limit {EXCEPTION_SHARE_LIMIT:.0%}), in the script {declared_share:.1%} "
            + f"(limit {DECLARED_SHARE_LIMIT:.0%})")
        if share > EXCEPTION_SHARE_LIMIT or declared_share > DECLARED_SHARE_LIMIT:
            gate.status = FAIL
        degraded = by_class.get("E7", 0)
        if degraded:
            gate.detail.append(f"  warning: {degraded} sentence(s) ran without the voice tap")
            if degraded / len(mine) > DEGRADED_SHARE_LIMIT:
                gate.status = FAIL
                gate.detail.append(f"  more than {DEGRADED_SHARE_LIMIT:.0%} degraded")
    for r in rows:
        for issue in r.exception_issues:
            gate.status = FAIL
            gate.detail.append(f"{r.run} seq {r.seq}: {issue}")
    return gate


def _gate_sg10(pools: dict) -> Gate:
    gate = Gate("SG-10", GATE_TITLES["SG-10"], NOT_MEASURABLE)
    statuses = []
    for pool, rows in pools.items():
        el = []
        for r in rows:
            offset = r.offset_planned_ms if r.offset_planned_ms is not None else r.offset_ms
            if (r.timing_eligible and r.t_voice is not None and offset is not None
                    and -6400 <= offset <= 150):
                el.append(r)
        bad = []
        for r in el:
            why = []
            if r.lag_ms is None:
                why.append(f"first move not measured ({r.move_status})")
            elif not -120 <= r.lag_ms <= 300:
                why.append(f"first move {_g(r.lag_ms)} ms from the sentence's own start")
            if r.own_overrun_ms is not None and r.own_overrun_ms > 120:
                why.append(f"still commanded {_g(r.own_overrun_ms)} ms into the next sentence")
            if why:
                bad.append((r, why))
        gate.pools[pool] = {"eligible": len(el), "failed": len(bad)}
        if not el:
            statuses.append(NOT_MEASURABLE)
            gate.detail.append(f"{pool}: no eligible sentence")
            continue
        gate.detail.append(f"{pool}: {len(el) - len(bad)}/{len(el)} on their own audio"
                           + (" [reported, not gated: the binding is inferred]"
                              if pool == "real" else ""))
        for r, why in bad[:12]:
            gate.detail.append(f"  {r.run} seq {r.seq} ({r.case}): " + "; ".join(why))
        statuses.append(INFO if pool == "real" else (FAIL if bad else PASS))
    gate.status = _combine(statuses)
    return gate


def evaluate(runs: list[Run], rows: list[Row], mode: str,
             baseline: Optional[dict]) -> list[Gate]:
    pools: dict[str, list[Row]] = {}
    for pool in ("scripted", "real"):
        if any(r.pool == pool for r in runs):
            pools[pool] = [r for r in rows if r.pool == pool]
    interim = mode == "m1"
    gates: dict[str, Gate] = {}

    def limits(gid: str) -> dict:
        return INTERIM_LIMITS[gid] if interim and gid in INTERIM_LIMITS else FINAL_LIMITS[gid][1]

    def title(gid: str) -> str:
        return FINAL_LIMITS[gid][0] + (" [interim limits]" if interim and gid in INTERIM_LIMITS
                                       else "")

    gates["DATA"] = _gate_data(runs, rows)
    gates["SAMPLE"] = _gate_sample(runs, rows)
    gates["SG-1"] = _metric_gate("SG-1", title("SG-1"), pools, lambda r: r.lag_ms, limits("SG-1"))
    gates["SG-2"] = _metric_gate("SG-2", title("SG-2"), pools, lambda r: r.lag_head_ms,
                                 limits("SG-2"), eligible=lambda r: r.head_expected)
    gates["SG-3"] = _metric_gate("SG-3", title("SG-3"), pools, lambda r: r.tail_ms,
                                 limits("SG-3"), eligible=lambda r: r.tail_ms is not None)
    gates["SG-4a"] = _metric_gate(
        "SG-4a", title("SG-4a"), pools,
        lambda r: r.settle_s if r.pause_settle_s is None else r.pause_settle_s, limits("SG-4a"),
        eligible=lambda r: r.settle_class == "4a" or r.pause_settle_s is not None, strict=False)
    gates["SG-4b"] = _metric_gate(
        "SG-4b", title("SG-4b"), pools, lambda r: r.settle_s, limits("SG-4b"),
        eligible=lambda r: r.settle_class == "4b", strict=False)
    sg5 = Gate("SG-5", GATE_TITLES["SG-5"], NOT_MEASURABLE, [
        "the authored start of each recorded move is not in any stream of a run"])
    recorded = stats(r.lag_ms for r in rows if r.recorded and r.timing_eligible
                     and r.lag_ms is not None)
    if recorded["n"]:
        sg5.detail.append("information: " + _stat_line(
            "recorded plans, voice to first motion of any kind (ms)", recorded))
    gates["SG-5"] = sg5
    gates["SG-6"] = _gate_sg6(runs, rows)
    gates["SG-7"] = _count_gate(
        "SG-7", GATE_TITLES["SG-7"], pools,
        lambda r: r.audio and not (r.klass or {}).get("identity", False), _sg7_failure)
    gates["SG-8"] = _count_gate(
        "SG-8", GATE_TITLES["SG-8"], pools,
        lambda r: (r.audio or r.exception == "E6") and (r.klass or {}).get("plan") != "skip",
        _sg8_failure)
    gates["SG-9"] = _count_gate(
        "SG-9", GATE_TITLES["SG-9"], pools, lambda r: r.audio and r.t_event is not None,
        lambda r: list(r.family_issues))
    gates["SG-10"] = _gate_sg10(pools)
    gates["SG-11"] = Gate("SG-11", GATE_TITLES["SG-11"], NOT_MEASURABLE, [
        "needs turn_rejected counts, restate checks and the captured learner audio, which a "
        "run directory does not hold"])
    gates["SG-12"] = _metric_gate(
        "SG-12", GATE_TITLES["SG-12"], pools, lambda r: r.coverage,
        {"median": (0.90, None)}, eligible=lambda r: bool(r.performed), strict=False)
    gates["SG-13"] = _gate_sg13(runs, baseline)
    gates["EXC"] = _gate_exceptions(runs, rows)

    ordered = [gates[g] for g in GATE_ORDER]
    for g in ordered:
        g.selected = g.id in GATE_SETS[mode]
    return ordered


# ---------------------------------------------------------------------------
# roll-ups
# ---------------------------------------------------------------------------
def _assign_quartiles(rows: list[Row]) -> None:
    loads = sorted(r.load1 for r in rows if r.load1 is not None)
    if not loads:
        return
    cuts = [percentile(loads, q) for q in (0.25, 0.5, 0.75)]
    for r in rows:
        if r.load1 is not None:
            r.load_quartile = "Q%d" % (1 + sum(1 for c in cuts if r.load1 > c))


def _split(rows: list[Row], getter: Callable) -> dict:
    out: dict[str, Any] = {"all": stats(getter(r) for r in rows)}
    for dim, label in (("motion_source", "source"), ("kind", "plan kind"),
                       ("overlapped", "older motion"), ("load_quartile", "load quartile")):
        groups: dict[str, list[Row]] = defaultdict(list)
        for r in rows:
            groups[getattr(r, dim) or "-"].append(r)
        out[label] = {k: stats(getter(r) for r in v) for k, v in sorted(groups.items())}
    return out


ROLLUP_METRICS: list[tuple[str, Callable, Callable]] = [
    ("lag, any motor (ms)", lambda r: r.lag_ms, lambda r: r.timing_eligible),
    ("lag, head (ms)", lambda r: r.lag_head_ms, lambda r: r.timing_eligible and r.head_expected),
    ("chain: tap rise to move (ms)", lambda r: r.chain_ms, lambda r: r.timing_eligible),
    ("old motion after the new voice (ms)", lambda r: r.tail_ms, lambda r: r.timing_eligible),
    ("end to settle, inside a reply (s)", lambda r: r.settle_s,
     lambda r: r.timing_eligible and r.settle_class == "4a"),
    ("end to settle, end of reply (s)", lambda r: r.settle_s,
     lambda r: r.timing_eligible and r.settle_class == "4b"),
    ("start of a long pause to settle (s)", lambda r: r.pause_settle_s,
     lambda r: r.timing_eligible and r.pause_settle_s is not None),
    ("event offset, measured (ms)", lambda r: r.offset_ms, lambda r: r.audio),
]
SEGMENTS = [("event_to_pickup", "event to pickup"),
            ("pickup_to_voice_seen", "pickup to voice seen"),
            ("voice_to_voice_seen", "t_voice to voice seen"),
            ("voice_seen_to_first_command", "voice seen to first command"),
            ("command_duration", "first command's call duration"),
            ("first_command_to_move", "first command to t_move"),
            ("accepted_to_started", "arbiter: accepted to started"),
            ("started_to_move", "arbiter: started to t_move")]


def rollups(pools: dict[str, list[Row]]) -> dict:
    out: dict[str, Any] = {}
    for pool, rows in pools.items():
        _assign_quartiles(rows)
        block: dict[str, Any] = {"metrics": {}}
        for name, getter, eligible in ROLLUP_METRICS:
            block["metrics"][name] = _split([r for r in rows if eligible(r)], getter)
        block["decomposition"] = {
            label: stats(r.segments.get(key) for r in rows if r.timing_eligible)
            for key, label in SEGMENTS}
        block["bind_kinds"] = dict(Counter(r.bind for r in rows if r.audio and r.plan > 0))
        block["event_offset_error"] = stats(r.offset_error_ms for r in rows)
        out[pool] = block
    return out


# ---------------------------------------------------------------------------
# baseline comparison
# ---------------------------------------------------------------------------
def baseline_view(pools: dict[str, list[Row]], runs: list[Run]) -> dict:
    """What a later run is compared against: structure per case, chain figures, CPU."""

    structure: dict[str, dict[str, list[str]]] = {}
    chain: dict[str, dict] = {}
    for pool, rows in pools.items():
        cases: dict[str, set] = defaultdict(set)
        for r in rows:
            if r.audio and r.signature:
                cases[r.case].add(r.signature)
        structure[pool] = {c: sorted(v) for c, v in sorted(cases.items())}
        st = stats(r.chain_ms for r in rows if r.timing_eligible)
        chain[pool] = {"n": st["n"], "median": st["median"], "p95": st["p95"]}
    return {"structure": structure, "chain": chain, "cpu": _cpu_medians(runs)}


def compare(current: dict, baseline: dict, gates: list[Gate],
            tolerance_ms: float) -> list[tuple[str, str, str]]:
    """(rule, status, detail) for the four rules of a comparison with a baseline."""

    findings: list[tuple[str, str, str]] = []
    for pool, cases in current["structure"].items():
        base_cases = baseline.get("structure", {}).get(pool)
        if base_cases is None:
            findings.append(("structure", NOT_MEASURABLE,
                             f"{pool}: the baseline has no such pool"))
            continue
        diffs = [c for c in sorted(set(cases) | set(base_cases))
                 if cases.get(c) != base_cases.get(c)]
        text = (f"plan, family or order changed in {len(diffs)} case(s): " + ", ".join(diffs[:10])
                if diffs else f"{len(cases)} case(s) identical")
        findings.append(("structure", FAIL if diffs else PASS,
                         f"{pool}: {text} (lane and supersede flag are not in the trace)"))
    failing = [g.id for g in gates if g.selected and g.status == FAIL]
    findings.append(("absolute", FAIL if failing else PASS,
                     ("selected gates failing: " + ", ".join(failing)) if failing
                     else "every selected gate holds on its own"))
    for pool, now in current["chain"].items():
        then = baseline.get("chain", {}).get(pool) or {}
        if now["median"] is None or then.get("median") is None:
            findings.append(("relative", NOT_MEASURABLE, f"{pool}: no chain figure on one side"))
            continue
        for key, allowed in (("median", tolerance_ms / 2.0), ("p95", tolerance_ms)):
            shift = now[key] - then[key]
            label = f"{pool}: chain {key} {then[key]:.0f} -> {now[key]:.0f} ms ({shift:+.0f})"
            if shift > allowed:
                findings.append(("relative", FAIL, label + f", later by more than {allowed:.0f}"))
            elif shift < -allowed:
                findings.append(("improvement", FAIL, label + f", earlier by more than "
                                 f"{allowed:.0f}: explain it before going on, a dropped move "
                                 "also looks faster"))
            else:
                findings.append(("relative", PASS, label))
    return findings


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _clean(obj: Any) -> Any:
    if isinstance(obj, float):
        return None if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    return obj


def _fmt(v: Optional[float], spec: str = "{:.0f}") -> str:
    if v is None:
        return "-"
    if math.isinf(v):
        return "none"
    return spec.format(v)


def _table(headers: list[str], body: list[list[str]]) -> list[str]:
    widths = [max([len(h)] + [len(row[i]) for row in body]) for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
           "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in body]
    return out


def sentence_table(runs: list[Run], rows: list[Row]) -> list[str]:
    origin = {r.name: r.origin for r in runs}
    headers = ["run", "seq", "case", "kind", "family a>p>pub", "tv", "t_voice", "offset",
               "bind", "ev>pk", "pk>vs", "vs>cmd", "call", "cmd>mv", "lag", "head", "chain",
               "tail", "end>settle", "outcome", "exc", "load"]
    body = []
    for r in rows:
        seg = r.segments
        published = "-" if r.published is None else (r.published or "-")
        fam = f"{r.authored or '-'}>{r.performed or '-'}>{published}"
        rel = None if r.t_voice is None else r.t_voice - origin.get(r.run, 0.0)
        body.append([
            r.run[-10:], str(r.seq), r.case[:16], r.kind, fam[:28],
            {"truth": "T", "tap": "M"}.get(r.voice_src, "-"), _fmt(rel, "{:.2f}"),
            _fmt(r.offset_ms), r.bind, _fmt(seg.get("event_to_pickup")),
            _fmt(seg.get("pickup_to_voice_seen")), _fmt(seg.get("voice_seen_to_first_command")),
            _fmt(seg.get("command_duration")), _fmt(seg.get("first_command_to_move")),
            _fmt(r.lag_ms, "{:+.0f}"), _fmt(r.lag_head_ms, "{:+.0f}"), _fmt(r.chain_ms),
            _fmt(r.tail_ms), (r.settle_class + " " if r.settle_class else "")
            + _fmt(r.settle_s, "{:.2f}"),
            r.outcome, r.exception or "-", _fmt(r.load1, "{:.1f}")])
    return _table(headers, body)


def _stat_cells(st: dict) -> list[str]:
    return [str(st["n"]), _g(st["median"]), _g(st["p95"]), _g(st["max"]), _g(st["min"])]


def _run_line(run: Run) -> str:
    resolution = ""
    if run.joints is not None and len(run.joints) > 1:
        iv = sorted(run.joints.intervals())
        med, p95 = percentile(iv, 0.5) * 1000, percentile(iv, 0.95) * 1000
        resolution = (f"; joints {len(run.joints)} samples, interval median {med:.1f} ms, "
                      f"p95 {p95:.1f}, max {iv[-1] * 1000:.1f}: t_move is a sample time, so a "
                      "lag can read late by up to one interval")
    m = run.manifest
    return (f"run {run.name} [{run.pool}] commit {str(m.get('commit'))[:10]} profile "
            f"{m.get('profile')} lang {m.get('lang')} source {m.get('motion_source')} k_ms "
            f"{_fmt(run.k_ms, '{:.1f}')} ({run.k_source}){resolution}")


def render(runs: list[Run], rows: list[Row], gates: list[Gate], roll: dict,
           findings: Optional[list], mode: str, show_rows: bool = True) -> str:
    out: list[str] = [f"motion sync report, gates: {mode}"]
    for run in runs:
        out.append(_run_line(run))
        out += [f"  note: {note}" for note in run.notes]
    out.append("")
    if show_rows:
        out.append("Sentences (ms unless noted; tv: T truth, M tap; ev>pk event to pickup, pk>vs "
                   "pickup to voice seen, vs>cmd voice seen to first command, call: duration of "
                   "that command, cmd>mv: command to t_move; tail is SG-3. When the event comes "
                   "before the voice, ev>pk and pk>vs include the wait for the voice)")
        out += sentence_table(runs, rows)
        out.append("")

    out.append("Roll-ups (worst is the latest value, best the earliest)")
    for pool, block in roll.items():
        out.append(f"[{pool}]")
        body = []
        for name, data in block["metrics"].items():
            body.append([name, "all", *_stat_cells(data["all"])])
            for label in ("source", "plan kind", "load quartile"):
                if len(data[label]) > 1:
                    body += [["", f"{label} {key}", *_stat_cells(st)]
                             for key, st in data[label].items()]
        out += _table(["metric", "split", "n", "median", "p95", "worst", "best"], body)
        out.append("")
        out.append("Delay decomposition, in order along the chain (ms)")
        out += _table(["segment", "n", "median", "p95", "worst", "best"],
                      [[label, *_stat_cells(st)] for label, st in block["decomposition"].items()])
        out.append(f"bind kinds: {block['bind_kinds']}")
        err = block["event_offset_error"]
        out.append("event offset, measured minus planned (ms): "
                   + (f"median {_g(err['median'])}, worst {_g(err['max'])}" if err["n"] else "-"))
        out.append("")

    excluded = [r for r in rows if r.exception]
    out.append(f"Exclusions ({len(excluded)} of {len(rows)} sentences)")
    out += [f"  {r.run} seq {r.seq} ({r.case}): {r.exception} via {r.exception_source}, "
            f"outcome {r.outcome}" for r in excluded] or ["  none"]
    out.append("")

    out.append("Gates")
    for g in gates:
        tag = "" if g.selected else " (information)"
        out.append(f"{g.status:>14}  {g.id:<6} {g.title}{tag}")
        out += [f"{'':>14}    {line}" for line in g.detail]
    if findings is not None:
        out.append("")
        out.append("Against the baseline")
        out += [f"{status:>14}  {rule:<11} {detail}" for rule, status, detail in findings]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
def build_report(paths: list[Path], mode: str = "final", baseline: Optional[dict] = None,
                 tolerance_ms: float = DEFAULT_TOLERANCE_MS,
                 show_rows: bool = True, k_ms: Optional[float] = None) -> tuple[dict, int, str]:
    """(summary for ``--json``, exit status, printable text) for the given run directories."""

    runs = [load_run(Path(p), k_ms) for p in paths]
    rows: list[Row] = []
    for run in runs:
        rows += analyse_run(run)
    pools = {pool: [r for r in rows if r.pool == pool]
             for pool in ("scripted", "real") if any(r.pool == pool for r in rows)}
    roll = rollups(pools)
    gates = evaluate(runs, rows, mode, baseline)
    view = baseline_view(pools, runs)
    findings = compare(view, baseline, gates, tolerance_ms) if baseline is not None else None

    selected = [g for g in gates if g.selected]
    failed = (any(g.status == FAIL for g in selected)
              or any(s == FAIL for _, s, _ in (findings or [])))
    unmeasured = any(g.status == NOT_MEASURABLE for g in selected)
    status = 1 if failed else 3 if unmeasured else 0
    summary = {
        "schema": SCHEMA, "mode": mode, "exit": status,
        "runs": [{"run": r.name, "pool": r.pool, "path": str(r.path), "k_ms": r.k_ms,
                  "k_source": r.k_source, "trace_bytes": r.trace_bytes, "problems": r.problems,
                  "commit": r.manifest.get("commit"), "profile": r.manifest.get("profile"),
                  "motion_source": r.manifest.get("motion_source"),
                  "joint_interval_s": stats(r.joints.intervals()) if r.joints else None}
                 for r in runs],
        "gates": [{"id": g.id, "title": g.title, "status": g.status, "selected": g.selected,
                   "detail": g.detail, "pools": g.pools} for g in gates],
        "rollups": roll,
        "rows": [dict(vars(r)) for r in rows],
        "against": [{"rule": a, "status": b, "detail": c} for a, b, c in findings or []],
        **view,
    }
    return summary, status, render(runs, rows, gates, roll, findings, mode, show_rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-sentence motion-sync report: joins the streams of one or more runs "
                    "and applies the gates.")
    parser.add_argument("runs", nargs="+", type=Path, help="run directories")
    parser.add_argument("--gates", choices=sorted(GATE_SETS), default="final",
                        help="m0: data completeness only, the rest as information; m1: interim "
                             "limits; final: every gate (default)")
    parser.add_argument("--against", type=Path, metavar="BASELINE_JSON",
                        help="a report written earlier with --json, compared rule by rule")
    parser.add_argument("--json", type=Path, metavar="OUT", help="write the full report here")
    parser.add_argument("--tolerance-ms", type=float, default=DEFAULT_TOLERANCE_MS,
                        help="timing tolerance T for --against (default %(default)s)")
    parser.add_argument("--no-rows", action="store_true", help="leave the per-sentence table out")
    parser.add_argument("--k-ms", type=float, metavar="MS",
                        help="read the runs with this monitor-to-audible offset instead of their "
                             "own, which is an upper bound")
    args = parser.parse_args(argv)

    baseline = None
    if args.against is not None:
        baseline, err = read_json(args.against)
        if err or not isinstance(baseline, dict) or "structure" not in baseline:
            print(f"baseline {args.against}: {err or 'not a report written by --json'}",
                  file=sys.stderr)
            return 1
    summary, status, text = build_report(args.runs, args.gates, baseline, args.tolerance_ms,
                                         show_rows=not args.no_rows, k_ms=args.k_ms)
    print(text)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(_clean(summary), indent=1), encoding="utf-8")
    return status


if __name__ == "__main__":
    sys.exit(main())
