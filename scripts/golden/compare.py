"""Compare two sets of recorded runs: exact where the stack is deterministic, by rule where not.

Exact streams are compared run by run against the first reference run, after masking. The
reference's other runs go through the same check, because a reference that disagrees with
itself cannot judge anything. Timed quantities are compared as the distribution across runs
under the three rules of ``deploy/hub/golden_tolerances.yaml``.

Everything here works on files, so a comparison needs neither a robot nor a running host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import json
import statistics
from typing import Any, Callable, Iterable, Optional

import yaml

from .normalise import Normaliser, mask_document
from .plan import PlanStreams, Step, flag_edges, split_plan, steps_from_calls
from .runs import Run

# Kinds of call that become one arbiter submit. A stop cancels one, and sound or wobble is none.
_SUBMITTING = frozenset({"goto", "look_at", "set_target", "antennas", "play_recorded", "wake_up"})
_HUB_CLIENT = "hostapp"
_HUB_LANE = "interaction"
_REFUSED_LANE = "expression"
_SUBMIT_EVENTS = frozenset({"", "submit", "submitted", "accepted"})
_NODE_GATES = ("gaze_run", "head_reserved")
# How far past a post or a voice onset the first move still counts as its answer.
_FIRST_MOVE_WINDOW_S = 3.0
_DETAIL_CHARS = 200


@dataclass
class Violation:
    stream: str
    detail: str
    run: str = ""

    def __str__(self) -> str:
        where = f" [{self.run}]" if self.run else ""
        return f"{self.stream}{where}: {self.detail}"


@dataclass
class Report:
    scenario: str
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def first_per_stream(self) -> list[Violation]:
        seen: dict[str, Violation] = {}
        for violation in self.violations:
            seen.setdefault(violation.stream, violation)
        return list(seen.values())


# ---------------------------------------------------------------------------------------
# tolerances
# ---------------------------------------------------------------------------------------


def load_tolerances(paths: Iterable[Any]) -> dict:
    """The tolerance files, later ones overriding earlier ones key by key."""

    merged: dict = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            _deep_merge(merged, yaml.safe_load(handle) or {})
    return merged


def _deep_merge(into: dict, more: dict) -> None:
    for key, value in more.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _deep_merge(into[key], value)
        else:
            into[key] = value


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    rank = share * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


_STATS: dict[str, Callable[[list[float]], float]] = {
    "value": lambda values: values[0],
    "median": statistics.median,
    "p95": lambda values: percentile(values, 0.95),
    "p99": lambda values: percentile(values, 0.99),
    "max": max,
}


def stat_tolerance(rule: dict, mode: str, reference: float) -> Optional[float]:
    """T for this stat: absolute by mode, or a share of the reference. None: not judged here."""

    if "relative" in rule:
        return max(float(rule["relative"]) * abs(reference), float(rule.get("min_t", 0.0)))
    value = rule.get(mode)
    return float(value) if value is not None else None


def judge(quantity: str, stat: str, rule: dict, mode: str, reference: list[float],
          candidate: list[float]) -> Optional[str]:
    """Apply the three rules to one stat, one value per run. Returns what failed, or None."""

    centre = statistics.median(reference)
    tolerance = stat_tolerance(rule, mode, centre)
    if tolerance is None:
        return None
    spread = max(reference) - min(reference)
    noise = 1.5 * spread
    label = f"{quantity} {stat}"
    if noise > 2.0 * tolerance:
        return (f"{label} is too noisy to judge: 1.5 x spread {noise:.4f} over 2T "
                f"{2.0 * tolerance:.4f}. Fix the scenario or find the noise")
    allowed = min(2.0 * tolerance, max(tolerance, noise))
    shift = statistics.median(candidate) - centre
    if rule.get("worse_only"):
        shift = max(0.0, shift)
    if abs(shift) > allowed:
        return (f"{label} moved {shift:+.4f} from {centre:.4f}, allowed {allowed:.4f} "
                f"(T {tolerance:.4f}, spread {spread:.4f})")
    if stat in ("median", "value") and abs(shift) > tolerance / 2.0:
        return (f"{label} moved {shift:+.4f} from {centre:.4f}, over T/2 "
                f"{tolerance / 2.0:.4f}: a systematic shift, whatever the noise")
    return None


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= _DETAIL_CHARS else text[:_DETAIL_CHARS] + "..."


def _first_difference(a: list, b: list) -> Optional[str]:
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return f"entry {index}: {_short(left)} != {_short(right)}"
    if len(a) != len(b):
        longer = a if len(a) > len(b) else b
        side = "reference" if len(a) > len(b) else "run"
        return (f"{len(a)} entries against {len(b)}; only the {side} has entry {min(len(a), len(b))}: "
                f"{_short(longer[min(len(a), len(b))])}")
    return None


def _text_difference(a: str, b: str) -> Optional[str]:
    if a == b:
        return None
    limit = min(len(a), len(b))
    at = next((i for i in range(limit) if a[i] != b[i]), limit)
    return f"byte {at}: {_short(a[max(0, at - 40):at + 80])} != {_short(b[max(0, at - 40):at + 80])}"


def _plain_steps(run: Run) -> PlanStreams:
    return split_plan(steps_from_calls(run.records("client")))


# ---------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------


def _within(a: Any, b: Any, eps: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= eps
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_within(a[k], b[k], eps) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_within(x, y, eps) for x, y in zip(a, b))
    return a == b


def _same_step(a: Step, b: Step, eps: float) -> bool:
    if not _within(a.structure, b.structure, eps):
        return False
    return a.voice_scaled or _within(a.antennas, b.antennas, eps)


def _antenna_problem(a: Step, b: Step, low: float, high: float) -> Optional[str]:
    """Do two voice-scaled moves fit one authored angle?

    The player scales before the client sees the move, so the authored angle is not in the
    stream. Two runs agree if some authored angle could give both: same sign, a ratio inside
    what the range allows, and the same share on both antennas, since one level scales both.
    """

    if a.antennas is None or b.antennas is None:
        return None if a.antennas == b.antennas else "antennas on only one side"
    floor = low / high
    for left, right in zip(a.antennas, b.antennas):
        if left == 0.0 and right == 0.0:
            continue
        if left * right <= 0.0:
            return f"antennas {a.antennas} and {b.antennas} have different signs"
        if min(abs(left), abs(right)) / max(abs(left), abs(right)) < floor - 1e-9:
            return (f"antennas {a.antennas} and {b.antennas} are further apart than a voice level "
                    f"can make them ({low} to {high} of the authored angle)")
    cross = a.antennas[0] * b.antennas[1] - a.antennas[1] * b.antennas[0]
    if abs(cross) > 1e-6:
        return f"antennas {a.antennas} and {b.antennas} were not scaled by one level"
    return None


def _clock_boundary(steps: list[Step], index: int, cfg: dict) -> Optional[str]:
    """The boundary this point in the sequence sits next to, if any, as a description."""

    if not steps:
        return None
    index = min(index, len(steps) - 1)
    now = steps[index].t
    window = float(cfg.get("clock_window_s", 0.0))
    picks: dict[str, Callable[[Step], bool]] = {
        "recorded_gap": lambda s: s.kind == "play_recorded",
        "carry_ttl": lambda s: bool(s.structure.get("tint")),
    }
    for name, boundary in (cfg.get("clock_boundaries_s") or {}).items():
        pick = picks.get(name)
        earlier = [s for s in steps[:index] if pick is None or pick(s)]
        if earlier and abs(now - earlier[-1].t - float(boundary)) <= window:
            return f"{name}: {now - earlier[-1].t:.3f}s since the last one"
    return None


def _voice_cut(extra: list[Step], following: Optional[Step], cfg: dict) -> bool:
    """Are these the last beats of a plan, played on one side and cut short on the other?

    When the voice stops the plan jumps to its settle, so the beat in flight at that moment
    can land on either side of the cut. Nothing else may go missing: it has to be a speech
    beat, a settle or the end of the plan has to come next, and only so many in one place.
    """

    if not extra or len(extra) > int(cfg.get("voice_cut_steps", 0)):
        return False
    if not all(s.structure.get("speech") and not s.structure.get("supersede") for s in extra):
        return False
    return (following is None or bool(following.structure.get("supersede"))
            or not following.structure.get("speech"))


def compare_plan(reference: Run, run: Run, cfg: dict) -> tuple[list[Violation], list[str]]:
    """Plan structure, with the differences that are allowed named and counted."""

    eps = float(cfg.get("float_eps", 1e-6))
    low, high = cfg.get("antenna_voice_range", (0.55, 1.0))
    a = _plain_steps(reference).plan
    b = _plain_steps(run).plan
    matcher = difflib.SequenceMatcher(None, [s.key() for s in a], [s.key() for s in b],
                                      autojunk=False)
    violations: list[Violation] = []
    allowed: list[str] = []
    cuts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1
                              and all(_same_step(a[i1 + k], b[j1 + k], eps)
                                      for k in range(i2 - i1))):
            for k in range(i2 - i1):
                problem = (_antenna_problem(a[i1 + k], b[j1 + k], float(low), float(high))
                           if a[i1 + k].voice_scaled else None)
                if problem:
                    violations.append(Violation("plan", f"step {i1 + k} "
                                                f"({a[i1 + k].reason}): {problem}", run.name))
            continue
        if tag in ("delete", "insert"):
            extra, rest, at = (a, i2, i1) if tag == "delete" else (b, j2, j1)
            spare = extra[at:rest]
            if _voice_cut(spare, extra[rest] if rest < len(extra) else None, cfg):
                cuts.append(f"step {i1}: {len(spare)} beat cut by the voice stopping")
                continue
        # One step either way: a boundary explains which beat fell on which side of it, and
        # says nothing about a stretch of plan that changed.
        boundary = ((i2 - i1 <= 1 and j2 - j1 <= 1)
                    and (_clock_boundary(a, i1, cfg) or _clock_boundary(b, j1, cfg)))
        shown_a = [s.structure for s in a[i1:i2]][:1]
        shown_b = [s.structure for s in b[j1:j2]][:1]
        if boundary:
            allowed.append(f"plan changed at step {i1} next to a clock boundary ({boundary})")
        else:
            violations.append(Violation(
                "plan", f"step {i1}: {tag} {i2 - i1} reference steps with {j2 - j1}: "
                f"{_short(shown_a)} != {_short(shown_b)}", run.name))
    limit = int(cfg.get("clock_changes_allowed", 0))
    if len(allowed) > limit:
        violations.append(Violation(
            "plan", f"{len(allowed)} plans changed at a clock boundary, {limit} allowed: "
            f"{'; '.join(allowed)}", run.name))
        allowed = []
    limit = int(cfg.get("voice_cuts_allowed", 0))
    if len(cuts) > limit:
        violations.append(Violation(
            "plan", f"{len(cuts)} plans cut at a different beat, {limit} allowed: {'; '.join(cuts)}",
            run.name))
        cuts = []
    return violations, [f"{run.name}: {note}" for note in allowed + cuts]


def _idle_order(run: Run) -> list[str]:
    return [step.key() for step in _plain_steps(run).idle]


def compare_idle(reference: Run, run: Run) -> list[Violation]:
    """Idle gestures by order. How many fit depends on the 5 s phase, so a prefix is fine."""

    a, b = _idle_order(reference), _idle_order(run)
    shorter = min(len(a), len(b))
    if a[:shorter] != b[:shorter]:
        return [Violation("idle", f"order: {_first_difference(a[:shorter], b[:shorter])}", run.name)]
    if abs(len(a) - len(b)) > 1:
        return [Violation("idle", f"{len(a)} idle gestures against {len(b)}", run.name)]
    return []


def check_listening_still(run: Run) -> list[Violation]:
    """An idle gesture while the mic is open reads as not listening, whichever stack it is."""

    starts = [float(m["t"]) for m in _marks(run, "listening_start")]
    ends = [float(m["t"]) for m in _marks(run, "listening_end")]
    return [Violation("idle", f"{step.reason} at {step.t - start:.2f} s into a listening window",
                      run.name)
            for start, end in zip(starts, ends)
            for step in _plain_steps(run).idle if start <= step.t <= end]


def check_motion(run: Run, *, required: bool = False) -> list[Violation]:
    """On a stack with the arbiter trace: one submit per client call, nothing out of lane."""

    records = run.stream("motion")
    if records is None:
        return [Violation("motion", "no arbiter trace in this run", run.name)] if required else []
    submits = [r for r in records if str(r.get("event", "")) in _SUBMIT_EVENTS]
    problems: list[Violation] = []
    for record in submits:
        client, lane = record.get("client"), record.get("lane")
        if client == _REFUSED_LANE or lane == _REFUSED_LANE:
            problems.append(Violation("motion", f"an expression entry: {_short(record)}", run.name))
        elif client == _HUB_CLIENT and lane != _HUB_LANE:
            problems.append(Violation("motion", f"lane {lane!r} from {client}: {_short(record)}",
                                      run.name))
    ours = sum(1 for r in submits if r.get("client") == _HUB_CLIENT)
    calls = sum(1 for s in steps_from_calls(run.records("client")) if s.kind in _SUBMITTING)
    if ours != calls:
        problems.append(Violation("motion", f"{calls} client calls made {ours} arbiter submits",
                                  run.name))
    return problems


# ---------------------------------------------------------------------------------------
# the exact streams
# ---------------------------------------------------------------------------------------


def _masked_records(records: list[dict], drop: tuple[str, ...]) -> list:
    normaliser = Normaliser(drop)
    return [normaliser.value(record) for record in records]


def _http_view(records: list[dict], cfg: dict) -> list:
    ignore = (cfg.get("ignore_fields") or {})
    normaliser = Normaliser()
    out = []
    for record in records:
        body = record.get("body")
        route = str(record.get("route", ""))
        fields = ignore.get(route)
        if fields and isinstance(body, dict):
            body = {k: v for k, v in body.items() if k not in fields}
        out.append(normaliser.value({
            "route": route, "method": record.get("method"), "status": record.get("status"),
            "content_type": record.get("content_type"), "body": body,
            "sha256": record.get("sha256")}))
    return out


def _tree_view(tree: dict[str, str]) -> dict[str, str]:
    """A directory's files, masked."""

    return {name: mask_document(tree[name]) for name in sorted(tree)}


def _compare_tree(stream: str, a: dict[str, str], b: dict[str, str], run: str) -> list[Violation]:
    left, right = _tree_view(a), _tree_view(b)
    if left.keys() != right.keys():
        return [Violation(stream, f"files {sorted(left)} != {sorted(right)}", run)]
    for name in left:
        difference = _text_difference(left[name], right[name])
        if difference:
            return [Violation(stream, f"{name}: {difference}", run)]
    return []


def _order(stream: str, a: list, b: list, run: str) -> list[Violation]:
    difference = _first_difference(a, b)
    return [Violation(stream, difference, run)] if difference else []


def compare_exact(reference: Run, run: Run, tolerances: dict) -> tuple[list[Violation], list[str]]:
    """Every stream that must match, one pair of runs."""

    plan_cfg = tolerances.get("plan") or {}
    violations, notes = compare_plan(reference, run, plan_cfg)
    violations += compare_idle(reference, run)
    name = run.name
    violations += _order("pending", [r.get("cmd") for r in reference.records("pending")],
                         [r.get("cmd") for r in run.records("pending")], name)
    violations += _order("flags", _edge_view(reference), _edge_view(run), name)
    violations += _order("sys", _node_gate_view(reference), _node_gate_view(run), name)
    violations += _compare_jsonl(reference, run, "mic", ("t",))
    violations += _compare_jsonl(reference, run, "speech", ("t",))
    violations += _compare_jsonl(reference, run, "events", ("t",))
    violations += _compare_jsonl(reference, run, "fiware", ("t",))
    violations += _compare_jsonl(reference, run, "offline", ("t",))
    if _both(reference, run, "http", violations):
        violations += _order("http", _http_view(reference.records("http"), tolerances.get("http") or {}),
                             _http_view(run.records("http"), tolerances.get("http") or {}), name)
    for stream, left, right in (("html", reference.html, run.html),
                                ("recordings", reference.recordings, run.recordings),
                                ("email", reference.email, run.email)):
        if left is None and right is None:
            continue
        if left is None or right is None:
            violations.append(Violation(stream, "recorded on one side only", name))
        else:
            violations += _compare_tree(stream, left, right, name)
    if reference.db is not None or run.db is not None:
        if reference.db is None or run.db is None:
            violations.append(Violation("db", "recorded on one side only", name))
        else:
            normaliser_a, normaliser_b = Normaliser(), Normaliser()
            difference = _first_difference(
                [normaliser_a.value(reference.db)], [normaliser_b.value(run.db)])
            if difference:
                violations.append(Violation("db", _db_difference(
                    normaliser_a.value(reference.db), normaliser_b.value(run.db)), name))
    return violations, notes


def _db_difference(a: dict, b: dict) -> str:
    for table in sorted(set(a) | set(b)):
        difference = _first_difference(a.get(table) or [], b.get(table) or [])
        if difference:
            return f"{table}: {difference}"
    return "differ"


def _edge_view(run: Run) -> list:
    return [(edge["flag"], edge["value"]) for edge in flag_edges(run.records("client"))]


def _node_gate_view(run: Run) -> list:
    """What the node itself says about its gates, as edges: a client call that returned True
    proves nothing about the node. Sampled once a second, so this is order and not time."""

    edges: list = []
    last: dict[str, Any] = {}
    for record in run.records("sys"):
        for name in _NODE_GATES:
            if name in record and last.get(name) != record[name]:
                last[name] = record[name]
                edges.append((name, record[name]))
    return edges


def _both(reference: Run, run: Run, stream: str, violations: list[Violation]) -> bool:
    left, right = reference.stream(stream), run.stream(stream)
    if left is None and right is None:
        return False
    if left is None or right is None:
        violations.append(Violation(stream, "recorded on one side only", run.name))
        return False
    return True


def _compare_jsonl(reference: Run, run: Run, stream: str, drop: tuple[str, ...]) -> list[Violation]:
    found: list[Violation] = []
    if not _both(reference, run, stream, found):
        return found
    return _order(stream, _masked_records(reference.records(stream), drop),
                  _masked_records(run.records(stream), drop), run.name)


# ---------------------------------------------------------------------------------------
# the timed quantities
# ---------------------------------------------------------------------------------------


def _marks(run: Run, name: str) -> list[dict]:
    return [mark for mark in run.records("marks") if mark.get("name") == name]


def _move_times(run: Run, *, use_arbiter: bool) -> list[float]:
    """When moves began: at the arbiter on a stack that has its trace, else at the client."""

    if use_arbiter and run.stream("motion") is not None:
        return sorted(float(r.get("t_mono", r.get("t", 0.0))) for r in run.records("motion")
                      if str(r.get("event", "")) in _SUBMIT_EVENTS
                      and r.get("client") == _HUB_CLIENT)
    return [step.t for step in _plain_steps(run).plan]


def _answers(starts: list[float], moves: list[float]) -> list[float]:
    out = []
    for start in starts:
        later = [t for t in moves if t >= start]
        if later and later[0] - start <= _FIRST_MOVE_WINDOW_S:
            out.append(later[0] - start)
    return out


def _inter_intent(run: Run, cfg: dict) -> dict[str, list[float]]:
    """Pacing inside a batch. The stream has no batch id, so two calls count as one batch when
    the later comes right after the earlier and one's reason extends the other's, as a part
    of a gesture is named after it.

    The voice decides two kinds of gap, so they are left out: a beat that waits for the next
    syllable, and a superseding move, which is a plan's first or the settle it jumps to when
    the voice stops. The onset-to-move time and the beat rate cover those.
    """

    slack = float(cfg.get("batch_slack_s", 0.25))
    plan = _plain_steps(run).plan
    gaps = [later.t - earlier.t for earlier, later in zip(plan, plan[1:])
            if later.t - earlier.t <= earlier.duration + slack
            and earlier.structure.get("pace") != "voice"
            and not later.structure.get("supersede")
            and (later.reason.startswith(earlier.reason) or earlier.reason.startswith(later.reason))]
    return {"": gaps}


def _timed_relative(run: Run, stream: str, values: list[tuple[str, float]]) -> dict[str, list[float]]:
    if stream in (run.meta.get("untimed") or ()):
        return {}
    start = float(run.meta.get("t0", 0.0))
    return {f"{index}:{label}": [t - start] for index, (label, t) in enumerate(values)}


def samples_for(quantity: str, run: Run, tolerances: dict, *, use_arbiter: bool) -> dict[str, list[float]]:
    """A quantity's samples in one run, by key. An unknown name reads a recorder's stream."""

    plan_cfg = tolerances.get("plan") or {}
    if quantity == "inter_intent_s":
        return _inter_intent(run, plan_cfg)
    if quantity == "post_to_first_intent_s":
        starts = [float(m["t"]) for m in _marks(run, "envelope_post")]
        return {"": _answers(starts, _move_times(run, use_arbiter=use_arbiter))}
    if quantity == "voice_onset_to_first_move_s":
        starts = [float(m["t"]) for m in _marks(run, "voice_onset")]
        return {"": _answers(starts, _move_times(run, use_arbiter=use_arbiter))}
    if quantity == "sustain_beats_per_voice_s":
        voiced = sum(float(m.get("dur", 0.0)) for m in _marks(run, "voice_span"))
        return {"": [len(_plain_steps(run).sustain) / voiced]} if voiced > 0 else {}
    if quantity == "idle_interval_s":
        idle = _plain_steps(run).idle
        return {"": [b.t - a.t for a, b in zip(idle, idle[1:])]}
    if quantity == "pending_appearance_s":
        return _timed_relative(run, "pending", [(str(r.get("cmd")), float(r.get("t", 0.0)))
                                                for r in run.records("pending")])
    if quantity == "gate_edge_s":
        return _timed_relative(run, "flags", [(f"{e['flag']}={e['value']}", e["t"])
                                              for e in flag_edges(run.records("client"))])
    if quantity == "mic_change_s":
        return _timed_relative(run, "mic", [(str(r.get("profile")), float(r.get("t", 0.0)))
                                            for r in run.records("mic")])
    if quantity == "http_latency_s":
        return {"": [float(r["latency_s"]) for r in run.records("http") if "latency_s" in r]}
    if quantity == "client_duration_s":
        out: dict[str, list[float]] = {}
        for record in run.records("client"):
            if "dur_s" in record:
                out.setdefault(str(record.get("method")), []).append(float(record["dur_s"]))
        return out
    if quantity == "tick_interval_s":
        ticks = sorted(float(r.get("t_start", 0.0)) for r in run.records("client")
                       if r.get("method") == "read_state")
        return {"": [b - a for a, b in zip(ticks, ticks[1:])]}
    return {"": list(run.extra.get(quantity, []))}


def compare_quantities(reference: list[Run], candidate: list[Run], tolerances: dict,
                       report: Report) -> None:
    mode = reference[0].mode
    kind = str(reference[0].meta.get("kind", "scripted"))
    use_arbiter = all(run.stream("motion") is not None for run in reference + candidate)
    for quantity, spec in (tolerances.get("quantities") or {}).items():
        spec = dict(spec or {})
        only_kind = spec.pop("only_kind", None)
        if only_kind and only_kind != kind:
            continue
        a_runs = [samples_for(quantity, run, tolerances, use_arbiter=use_arbiter) for run in reference]
        b_runs = [samples_for(quantity, run, tolerances, use_arbiter=use_arbiter) for run in candidate]
        for key in sorted({k for sample in a_runs + b_runs for k in sample}):
            a_values = [sample.get(key) or [] for sample in a_runs]
            b_values = [sample.get(key) or [] for sample in b_runs]
            name = f"{quantity}[{key}]" if key else quantity
            if not any(a_values) and not any(b_values):
                continue
            if not all(a_values) or not all(b_values):
                # A count difference is the exact stream's to report, for the quantities it
                # covers. For the others it is the only signal there is.
                report.violations.append(Violation(
                    quantity, f"{name}: samples in only some runs "
                    f"({sum(map(bool, a_values))} of {len(a_values)} reference, "
                    f"{sum(map(bool, b_values))} of {len(b_values)} candidate)"))
                continue
            for stat, rule in spec.items():
                function = _STATS.get(stat)
                if function is None or not isinstance(rule, dict):
                    continue
                problem = judge(name, stat, rule, mode, [function(v) for v in a_values],
                                [function(v) for v in b_values])
                if problem:
                    report.violations.append(Violation(quantity, problem))


def check_floors(runs: list[Run], tolerances: dict, report: Report) -> None:
    """A lower bound on a ``sys.jsonl`` field. None is tracked: bounds are per robot."""

    for field_name, floor in (tolerances.get("floors") or {}).items():
        for run in runs:
            values = [float(r[field_name]) for r in run.records("sys") if field_name in r]
            if not values:
                # A floor on something nobody wrote down would otherwise never fail.
                report.violations.append(Violation(
                    "sys", f"{field_name} has a floor and no reading in this run", run.name))
            elif min(values) < float(floor):
                report.violations.append(Violation(
                    "sys", f"{field_name} fell to {min(values)}, floor {floor}", run.name))


def _required_runs(reference: list[Run], tolerances: dict) -> int:
    runs = tolerances.get("runs") or {}
    voiced = any(run.meta.get("voiced") for run in reference)
    return int(runs.get("voiced" if voiced else "default", 1))


def self_check(run: Run) -> list[Violation]:
    """A run that claims a witness must show it, or a wrapper is dropping calls in both stacks."""

    needed = run.meta.get("self_check") or []
    seen = {str(r.get("method")) for r in run.records("client")}
    return [Violation("self-check", f"no {method} call in this run", run.name)
            for method in needed if method not in seen]


def compare_scenario(scenario: str, reference: list[Run], candidate: list[Run], tolerances: dict,
                     *, exact_only: bool = False, min_runs: Optional[int] = None) -> Report:
    report = Report(scenario)
    if not reference or not candidate:
        report.violations.append(Violation(
            "runs", f"{len(reference)} reference and {len(candidate)} candidate runs"))
        return report
    needed = _required_runs(reference, tolerances) if min_runs is None else min_runs
    for label, runs in (("reference", reference), ("candidate", candidate)):
        if len(runs) < needed:
            report.violations.append(Violation(
                "runs", f"{len(runs)} {label} runs, this scenario needs {needed}: with fewer "
                "the spread across runs says nothing"))
    modes = {run.mode for run in reference + candidate}
    if len(modes) > 1:
        report.violations.append(Violation("runs", f"runs from different modes: {sorted(modes)}"))
        return report
    speeds = {bool(run.meta.get("fast")) for run in reference + candidate}
    if len(speeds) > 1:
        report.violations.append(Violation("runs", "compressed runs against real-time ones"))
        return report
    for run in reference + candidate:
        report.violations += self_check(run)
        report.violations += check_motion(run, required=bool(tolerances.get("motion_required")))
        report.violations += check_listening_still(run)
    base = reference[0]
    for run in reference[1:]:
        found, _ = compare_exact(base, run, tolerances)
        for violation in found:
            report.violations.append(Violation(
                violation.stream, f"the reference disagrees with itself: {violation.detail}",
                violation.run))
    for run in candidate:
        found, notes = compare_exact(base, run, tolerances)
        report.violations += found
        report.notes += notes
    if base.stream("motion") is None or any(run.stream("motion") is None for run in candidate):
        report.notes.append("motion: not applicable, one side has no arbiter trace")
    if not exact_only:
        compare_quantities(reference, candidate, tolerances, report)
        check_floors(reference + candidate, tolerances, report)
    return report


def spread_report(scenario: str, runs: list[Run], tolerances: dict) -> tuple[list[str], list[Violation]]:
    """One side on its own: how far its runs stray, and whether it can judge anything."""

    lines: list[str] = []
    violations: list[Violation] = []
    if not runs:
        return lines, [Violation("runs", f"{scenario}: no runs")]
    for run in runs[1:]:
        found, _ = compare_exact(runs[0], run, tolerances)
        violations += found
    mode = runs[0].mode
    kind = str(runs[0].meta.get("kind", "scripted"))
    use_arbiter = all(run.stream("motion") is not None for run in runs)
    for quantity, spec in (tolerances.get("quantities") or {}).items():
        spec = dict(spec or {})
        only_kind = spec.pop("only_kind", None)
        if only_kind and only_kind != kind:
            continue
        per_run = [samples_for(quantity, run, tolerances, use_arbiter=use_arbiter) for run in runs]
        for key in sorted({k for sample in per_run for k in sample}):
            values = [sample.get(key) or [] for sample in per_run]
            if not all(values):
                continue
            for stat, rule in spec.items():
                function = _STATS.get(stat)
                if function is None or not isinstance(rule, dict):
                    continue
                per_stat = [function(v) for v in values]
                tolerance = stat_tolerance(rule, mode, statistics.median(per_stat))
                if tolerance is None:
                    continue
                spread = max(per_stat) - min(per_stat)
                name = f"{quantity}[{key}]" if key else quantity
                lines.append(f"{name} {stat}: centre {statistics.median(per_stat):.4f} "
                             f"spread {spread:.4f} T {tolerance:.4f}")
                if 1.5 * spread > 2.0 * tolerance:
                    violations.append(Violation(quantity, f"{name} {stat} is too noisy to judge"))
    return lines, violations
