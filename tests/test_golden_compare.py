"""``golden_trace compare``: each thing it claims to catch, it catches.

A parity harness that never fails proves nothing, so every claim has a test that plants the
difference and expects the named stream to report it, beside one that shows the same stream
stays quiet on a difference it is meant to allow.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from golden import compare as C  # noqa: E402
from golden.normalise import Normaliser, mask_document  # noqa: E402
from golden.plan import flag_edges, split_plan, steps_from_calls  # noqa: E402
from golden.record import parse_mic_journal  # noqa: E402
from golden.runs import Run, load_run, write_jsonl  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOLERANCES = C.load_tolerances([ROOT / "deploy" / "hub" / "golden_tolerances.yaml"])
SID = "anna-001-20261103-000105-346c623c2873c0ed"
OTHER_SID = "anna-001-20261104-120000-aaaaaaaaaaaaaaaa"


def intent(reason: str = "move", *, kind: str = "goto", pitch: float = 1.0, duration: float = 0.3,
           antennas=(0.5, -0.5), **fields) -> dict:
    return {"kind": kind, "head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": pitch,
                                   "yaw": 0.0},
            "antennas": list(antennas) if antennas else None, "body_yaw": None,
            "duration": duration, "interpolation": "minjerk", "sound": None, "wobble": None,
            "move": None, "priority": 0, "reason": reason, "speech": False, "pace": "fixed",
            "antenna_voice": False, "tint": "", "supersede": False, **fields}


def apply_call(t: float, *args, dur: float = 0.001, **kwargs) -> dict:
    return {"method": "apply", "t_start": t, "dur_s": dur, "args": {"intent": intent(*args, **kwargs)},
            "ret": None}


def call(t: float, method: str, dur: float = 0.001, **args) -> dict:
    return {"method": method, "t_start": t, "dur_s": dur, "args": args, "ret": None}


BASE_CLIENT = [
    call(1.0, "set_gaze", enabled=True),
    call(1.1, "play_recorded_move", name="welcoming1"),
    apply_call(2.0, "look up", pitch=-14.0, duration=0.6),
    apply_call(3.0, "speaking", speech=True, supersede=True, pitch=2.0),
    apply_call(3.4, "speaking (lift)", speech=True, pitch=3.0),
    apply_call(3.8, "cue settle", speech=True, supersede=True, pitch=0.0),
    call(5.0, "set_gaze", enabled=False),
]


def make_run(tmp_path: Path, name: str, *, client=None, meta=None, **streams) -> Run:
    path = tmp_path / name
    path.mkdir(parents=True)
    write_jsonl(path / "client.jsonl", BASE_CLIENT if client is None else client)
    trees = {key: streams.pop(key) for key in ("html", "recordings", "email") if key in streams}
    db = streams.pop("db", None)
    for stream, records in streams.items():
        write_jsonl(path / f"{stream}.jsonl", records)
    for tree, files in trees.items():
        (path / tree).mkdir()
        for filename, text in files.items():
            (path / tree / filename).write_text(text, encoding="utf-8")
    if db is not None:
        (path / "db.json").write_text(json.dumps(db), encoding="utf-8")
    (path / "meta.json").write_text(json.dumps({"mode": "sim", "t0": 0.0, **(meta or {})}),
                                    encoding="utf-8")
    return load_run(path)


def side(tmp_path: Path, label: str, count: int = 3, **kwargs) -> list[Run]:
    return [make_run(tmp_path, f"{label}/run-{index:03d}", **copy.deepcopy(kwargs))
            for index in range(1, count + 1)]


def compared(tmp_path, a: dict, b: dict, *, tolerances=TOLERANCES, count=3, **options):
    return C.compare_scenario("s", side(tmp_path, "a", count, **a), side(tmp_path, "b", count, **b),
                              tolerances, **options)


def streams_of(report: C.Report) -> set[str]:
    return {violation.stream for violation in report.violations}


def test_identical_runs_are_the_same(tmp_path):
    report = compared(tmp_path, {}, {})
    assert report.ok, [str(v) for v in report.violations]


# --- the three rules ------------------------------------------------------------------

T = {"sim": 0.05}


def test_a_shift_inside_the_tolerance_passes():
    assert C.judge("q", "p95", T, "sim", [1.0, 1.01, 1.02], [1.05, 1.05, 1.05]) is None


def test_noise_widens_the_allowance_only_up_to_the_cap():
    # Spread 0.04: 1.5 x spread is 0.06, over T but under 2T, so 0.06 is allowed.
    assert C.judge("q", "p95", T, "sim", [1.00, 1.02, 1.04], [1.07, 1.07, 1.07]) is None
    assert "moved" in C.judge("q", "p95", T, "sim", [1.00, 1.02, 1.04], [1.09, 1.09, 1.09])


def test_a_scenario_too_noisy_to_judge_fails_even_when_nothing_moved():
    problem = C.judge("q", "p95", T, "sim", [1.0, 1.05, 1.2], [1.05, 1.05, 1.05])
    assert "too noisy" in problem


def test_a_systematic_median_shift_cannot_hide_in_the_spread():
    # Allowed by rule 1 (0.03 under T) yet over T/2: the median may not walk.
    assert "T/2" in C.judge("q", "median", T, "sim", [1.0, 1.0, 1.0], [1.03, 1.03, 1.03])
    assert C.judge("q", "p95", T, "sim", [1.0, 1.0, 1.0], [1.03, 1.03, 1.03]) is None


def test_worse_only_lets_it_improve_without_limit():
    rule = {"relative": 0.2, "worse_only": True}
    assert C.judge("q", "p95", rule, "sim", [1.0, 1.0, 1.0], [0.1, 0.1, 0.1]) is None
    assert "moved" in C.judge("q", "p95", rule, "sim", [1.0, 1.0, 1.0], [1.3, 1.3, 1.3])


def test_a_relative_rule_has_a_floor_and_a_mode_without_a_figure_is_not_judged():
    assert C.stat_tolerance({"relative": 0.2, "min_t": 0.005}, "sim", 0.001) == 0.005
    assert C.judge("q", "max", {"robot": 0.15}, "sim", [1.0], [9.0]) is None


def test_too_few_runs_fail_because_the_spread_would_say_nothing(tmp_path):
    report = compared(tmp_path, {}, {}, count=2)
    assert "runs" in streams_of(report)


def test_a_voiced_scenario_needs_more_runs(tmp_path):
    report = compared(tmp_path, {"meta": {"voiced": True}}, {"meta": {"voiced": True}}, count=3)
    assert any("needs 5" in v.detail for v in report.violations)


def test_runs_of_different_modes_or_speeds_are_not_compared(tmp_path):
    report = compared(tmp_path, {}, {"meta": {"mode": "robot"}})
    assert any("different modes" in v.detail for v in report.violations)
    report = compared(tmp_path / "x", {}, {"meta": {"fast": True}})
    assert any("compressed" in v.detail for v in report.violations)


def test_a_missing_side_is_a_violation(tmp_path):
    report = C.compare_scenario("s", side(tmp_path, "a"), [], TOLERANCES)
    assert not report.ok


# --- plan structure -------------------------------------------------------------------


def plan_with(**changes) -> list[dict]:
    client = copy.deepcopy(BASE_CLIENT)
    client[3]["args"]["intent"].update(changes)
    return client


@pytest.mark.parametrize("change", [
    {"kind": "look_at"},
    {"head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 9.0, "yaw": 0.0}},
    {"body_yaw": 0.2},
    {"duration": 0.9},
    {"interpolation": "linear"},
    {"move": "other"},
    {"reason": "something else"},
    {"supersede": False},
    {"antennas": [0.1, 0.1]},
])
def test_each_field_of_a_plan_step_is_compared(tmp_path, change):
    report = compared(tmp_path, {}, {"client": plan_with(**change)}, exact_only=True)
    assert "plan" in streams_of(report), change


def test_floats_compare_within_the_stated_epsilon(tmp_path):
    close = {"head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 2.0 + 5e-7, "yaw": 0.0}}
    assert compared(tmp_path / "1", {}, {"client": plan_with(**close)}, exact_only=True).ok
    far = {"head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 2.0 + 1e-4, "yaw": 0.0}}
    assert "plan" in streams_of(compared(tmp_path / "2", {}, {"client": plan_with(**far)},
                                         exact_only=True))


def test_a_missing_step_an_extra_step_and_a_new_order_are_caught(tmp_path):
    missing = [c for i, c in enumerate(BASE_CLIENT) if i != 2]
    extra = BASE_CLIENT + [apply_call(6.0, "unplanned")]
    swapped = copy.deepcopy(BASE_CLIENT)
    swapped[2]["t_start"], swapped[3]["t_start"] = swapped[3]["t_start"], swapped[2]["t_start"]
    for name, client in (("missing", missing), ("extra", extra), ("swapped", swapped)):
        report = compared(tmp_path / name, {}, {"client": client}, exact_only=True)
        assert "plan" in streams_of(report), name


def test_a_lost_recorded_move_is_caught(tmp_path):
    client = [c for c in BASE_CLIENT if c["method"] != "play_recorded_move"]
    assert "plan" in streams_of(compared(tmp_path, {}, {"client": client}, exact_only=True))


def beats(start: float, count: int, reason: str = "speaking (sustain)") -> list[dict]:
    return [apply_call(start + 0.4 * i, reason, speech=True, pace="voice", antenna_voice=True,
                       pitch=float(i)) for i in range(count)]


def with_sustain(count: int, reason: str = "speaking (sustain)") -> list[dict]:
    return BASE_CLIENT[:5] + beats(3.5, count, reason) + BASE_CLIENT[5:]


@pytest.mark.parametrize("reason", ["speaking (sustain)", "speaking (sustain tension)"])
def test_sustain_beats_may_differ_in_number(tmp_path, reason):
    report = compared(tmp_path, {"client": with_sustain(2, reason)},
                      {"client": with_sustain(7, reason)}, exact_only=True)
    assert report.ok, [str(v) for v in report.violations]


def test_the_settle_the_linger_loop_repeats_comes_out_with_its_beats(tmp_path):
    settle = BASE_CLIENT[5]
    lingered = (BASE_CLIENT[:6] + beats(4.0, 2) + [dict(copy.deepcopy(settle), t_start=4.9)]
                + BASE_CLIENT[6:])
    streams = split_plan(steps_from_calls(lingered))
    assert len(streams.linger_settles) == 1 and len(streams.sustain) == 2
    report = compared(tmp_path, {}, {"client": lingered}, exact_only=True)
    assert report.ok, [str(v) for v in report.violations]


def test_a_plans_own_settle_after_its_beats_stays_in_the_plan(tmp_path):
    # The beats ran before the settle: without it the plan never came back to rest.
    without = BASE_CLIENT[:5] + beats(3.5, 3)
    report = compared(tmp_path, {"client": with_sustain(3)}, {"client": without}, exact_only=True)
    assert "plan" in streams_of(report)


def rate_runs(tmp_path, label: str, beat_count: int) -> list[Run]:
    return side(tmp_path, label, client=with_sustain(beat_count),
                marks=[{"name": "voice_span", "t": 3.0, "dur": 10.0}])


def test_the_sustain_rate_is_compared_as_a_rate(tmp_path):
    report = C.compare_scenario("s", rate_runs(tmp_path, "a", 20), rate_runs(tmp_path, "b", 40),
                                TOLERANCES)
    assert "sustain_beats_per_voice_s" in streams_of(report)
    report = C.compare_scenario("s", rate_runs(tmp_path / "x", "a", 20),
                                rate_runs(tmp_path / "x", "b", 20), TOLERANCES)
    assert "sustain_beats_per_voice_s" not in streams_of(report)


def voiced_step(antennas, t=3.0) -> list[dict]:
    client = copy.deepcopy(BASE_CLIENT)
    client[3] = apply_call(t, "speaking", speech=True, supersede=True, pitch=2.0,
                           antenna_voice=True, antennas=antennas)
    return client


def test_voice_scaled_antennas_may_follow_the_level(tmp_path):
    report = compared(tmp_path, {"client": voiced_step((0.8, -0.8))},
                      {"client": voiced_step((0.45, -0.45))}, exact_only=True)
    assert report.ok, [str(v) for v in report.violations]


@pytest.mark.parametrize("other, word", [
    ((0.3, -0.3), "further apart"),
    ((-0.8, 0.8), "different signs"),
    ((0.8, -0.5), "one level"),
])
def test_voice_scaled_antennas_outside_what_a_level_can_do_fail(tmp_path, other, word):
    report = compared(tmp_path, {"client": voiced_step((0.8, -0.8))},
                      {"client": voiced_step(other)}, exact_only=True)
    assert any(word in v.detail for v in report.violations), [str(v) for v in report.violations]


def clock_change_at(ages: list[float]) -> tuple[list[dict], list[dict]]:
    """A recorded move, then plans at those ages from it, which differ between the sides."""

    a = [call(1.0, "play_recorded_move", name="m")]
    b = [call(1.0, "play_recorded_move", name="m")]
    for index, age in enumerate(ages):
        a.append(apply_call(1.0 + age, f"plan-{index}", pitch=1.0))
        b.append(apply_call(1.0 + age, f"plan-{index}", pitch=5.0))
        a.append(apply_call(1.0 + age + 1.0, f"shared-{index}"))
        b.append(apply_call(1.0 + age + 1.0, f"shared-{index}"))
        a.append(call(1.0 + age + 1.1, "play_recorded_move", name="m"))
        b.append(call(1.0 + age + 1.1, "play_recorded_move", name="m"))
    return a, b


def test_a_plan_changed_at_a_clock_boundary_is_named_once_allowed(tmp_path):
    a, b = clock_change_at([3.05])
    report = compared(tmp_path, {"client": a}, {"client": b}, exact_only=True)
    assert report.ok, [str(v) for v in report.violations]
    assert any("clock boundary" in note and "recorded_gap" in note for note in report.notes)


def test_a_second_clock_change_in_one_scenario_fails(tmp_path):
    a = clock_change_at([3.05])[0]
    b = clock_change_at([3.05])[1]
    tail_a, tail_b = clock_change_at([3.05])
    shift = 10.0
    for record in tail_a + tail_b:
        record["t_start"] += shift
    report = compared(tmp_path, {"client": a + tail_a}, {"client": b + tail_b}, exact_only=True)
    assert any("clock boundary" in v.detail for v in report.violations)


def test_a_plan_change_away_from_any_boundary_fails(tmp_path):
    a, b = clock_change_at([1.5])
    assert "plan" in streams_of(compared(tmp_path, {"client": a}, {"client": b}, exact_only=True))


def speech_plan(beat_count: int, start: float = 3.0) -> list[dict]:
    steps = [apply_call(start + 0.4 * i, f"beat {i}", speech=True, pace="voice", pitch=float(i))
             for i in range(beat_count)]
    steps.append(apply_call(start + 0.4 * beat_count, "settle", speech=True, supersede=True,
                            pitch=0.0))
    return BASE_CLIENT[:3] + steps


def test_a_plan_cut_one_beat_earlier_by_the_voice_stopping_is_named(tmp_path):
    report = compared(tmp_path, {"client": speech_plan(4)}, {"client": speech_plan(3)},
                      exact_only=True)
    assert report.ok, [str(v) for v in report.violations]
    assert any("cut by the voice" in note for note in report.notes)


def test_more_than_a_beat_gone_before_the_cut_fails(tmp_path):
    report = compared(tmp_path, {"client": speech_plan(4)}, {"client": speech_plan(2)},
                      exact_only=True)
    assert "plan" in streams_of(report)


def test_a_beat_lost_from_the_middle_of_a_plan_fails(tmp_path):
    middle = speech_plan(4)
    del middle[4]
    report = compared(tmp_path, {"client": speech_plan(4)}, {"client": middle}, exact_only=True)
    assert "plan" in streams_of(report)


def test_too_many_cuts_in_one_scenario_fail(tmp_path):
    def plans(count: int) -> list[dict]:
        out = BASE_CLIENT[:3]
        for index in range(3):
            base = 3.0 + index * 5.0
            out = out + [apply_call(base + 0.4 * i, f"p{index} beat {i}", speech=True, pitch=float(i))
                         for i in range(count)]
            out = out + [apply_call(base + 3.0, f"p{index} settle", speech=True, supersede=True)]
        return out

    report = compared(tmp_path, {"client": plans(3)}, {"client": plans(2)}, exact_only=True)
    assert any("cut at a different beat" in v.detail for v in report.violations)


# --- idle gestures --------------------------------------------------------------------


def with_idle(times: list[float], reasons=None, offset: float = 0.0) -> list[dict]:
    reasons = reasons or ["idle: stay alive"] * len(times)
    return BASE_CLIENT + [apply_call(t + offset, reason, pitch=float(i))
                          for i, (t, reason) in enumerate(zip(times, reasons))]


def test_idle_gestures_are_not_compared_by_offset_from_process_start(tmp_path):
    report = compared(tmp_path, {"client": with_idle([10.0, 15.0, 20.0])},
                      {"client": with_idle([10.0, 15.0, 20.0], offset=3.0)})
    assert "idle" not in streams_of(report) and "idle_interval_s" not in streams_of(report)


def test_idle_gestures_keep_their_order(tmp_path):
    report = compared(tmp_path, {"client": with_idle([10.0, 15.0])},
                      {"client": with_idle([10.0, 15.0], ["idle: listening", "idle: stay alive"])},
                      exact_only=True)
    assert "idle" in streams_of(report)


def test_idle_gestures_keep_their_interval(tmp_path):
    report = compared(tmp_path, {"client": with_idle([10.0, 15.0, 20.0])},
                      {"client": with_idle([10.0, 17.0, 24.0])})
    assert "idle_interval_s" in streams_of(report)


def test_one_idle_gesture_more_is_the_phase_but_several_are_not(tmp_path):
    assert compared(tmp_path / "1", {"client": with_idle([10.0, 15.0])},
                    {"client": with_idle([10.0, 15.0, 20.0])}, exact_only=True).ok
    assert "idle" in streams_of(compared(tmp_path / "2", {"client": with_idle([10.0])},
                                         {"client": with_idle([10.0, 15.0, 20.0, 25.0])},
                                         exact_only=True))


def test_an_idle_gesture_while_listening_fails_on_either_side(tmp_path):
    marks = [{"name": "listening_start", "t": 9.0}, {"name": "listening_end", "t": 12.0}]
    report = compared(tmp_path, {"marks": marks}, {"client": with_idle([10.0]), "marks": marks},
                      exact_only=True)
    assert any("listening window" in v.detail for v in report.violations)


# --- the arbiter stream ---------------------------------------------------------------


def submits(count: int, **fields) -> list[dict]:
    return [{"t_mono": 1.0 + i, "client": "hostapp", "lane": "interaction", "kind": "goto",
             **fields} for i in range(count)]


# The base plan makes one recorded move and four gotos.
BASE_SUBMITS = 5


def test_a_stack_without_the_arbiter_trace_is_not_a_failure(tmp_path):
    report = compared(tmp_path, {}, {"motion": submits(BASE_SUBMITS)}, exact_only=True)
    assert report.ok, [str(v) for v in report.violations]
    assert any("not applicable" in note for note in report.notes)


def test_one_submit_per_client_call(tmp_path):
    report = compared(tmp_path, {}, {"motion": submits(BASE_SUBMITS - 1)}, exact_only=True)
    assert "motion" in streams_of(report)


def test_the_wrong_lane_and_expression_entries_are_caught(tmp_path):
    report = compared(tmp_path / "1", {}, {"motion": submits(BASE_SUBMITS, lane="engagement")},
                      exact_only=True)
    assert any("lane" in v.detail for v in report.violations)
    entries = submits(BASE_SUBMITS) + [{"t_mono": 9.0, "client": "ros", "lane": "expression"}]
    report = compared(tmp_path / "2", {}, {"motion": entries}, exact_only=True)
    assert any("expression" in v.detail for v in report.violations)


def test_only_accepted_records_of_the_arbiter_count_as_submits(tmp_path):
    entries = submits(BASE_SUBMITS, event="accepted") + submits(BASE_SUBMITS, event="started")
    assert compared(tmp_path, {}, {"motion": entries}, exact_only=True).ok


def test_first_move_times_come_from_the_arbiter_when_both_sides_have_it(tmp_path):
    marks = [{"name": "envelope_post", "t": 1.0}]
    moves = submits(BASE_SUBMITS)
    late = [{**m, "t_mono": m["t_mono"] + 0.5} for m in moves]
    # Client calls identical on both sides; only the arbiter saw the hold.
    report = compared(tmp_path, {"marks": marks, "motion": moves}, {"marks": marks, "motion": late})
    assert "post_to_first_intent_s" in streams_of(report)


# --- exact streams --------------------------------------------------------------------


def http_record(route: str = "/api/x", *, status: int = 200, kind: str = "application/json",
                body=None, **fields) -> dict:
    return {"t": 1.0, "route": route, "method": "GET", "status": status, "content_type": kind,
            "body": {"ok": True} if body is None else body, "latency_s": 0.01, **fields}


@pytest.mark.parametrize("change", [
    {"status": 409}, {"kind": "text/plain"}, {"body": {"ok": False}}, {"route": "/api/y"},
])
def test_http_status_type_body_and_route_are_exact(tmp_path, change):
    report = compared(tmp_path, {"http": [http_record()]}, {"http": [http_record(**change)]},
                      exact_only=True)
    assert "http" in streams_of(report), change


def test_a_binary_body_is_compared_by_its_hash(tmp_path):
    report = compared(tmp_path, {"http": [http_record(kind="audio/wav", sha256="a" * 8)]},
                      {"http": [http_record(kind="audio/wav", sha256="b" * 8)]}, exact_only=True)
    assert "http" in streams_of(report)


def test_session_ids_and_tokens_are_masked_but_their_pattern_is_not(tmp_path):
    jwt = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    a = [http_record(body={"sid": SID, "url": f"/session?sso={jwt}&sid={SID}"}),
         http_record(body={"sid": SID})]
    same = [http_record(body={"sid": OTHER_SID, "url": f"/session?sso=eyJhIjoxfQ.eyJiIjoyfQ.c2ln&sid={OTHER_SID}"}),
            http_record(body={"sid": OTHER_SID})]
    assert compared(tmp_path / "1", {"http": a}, {"http": same}, exact_only=True).ok
    wrong = [http_record(body={"sid": OTHER_SID}), http_record(body={"sid": SID})]
    assert "http" in streams_of(compared(tmp_path / "2", {"http": a[1:] + a[1:]},
                                         {"http": wrong}, exact_only=True))


def test_healthz_runtime_is_the_one_whitelisted_field(tmp_path):
    a = [http_record("/healthz", body={"status": "ok", "runtime": "hostapp"})]
    b = [http_record("/healthz", body={"status": "ok", "runtime": "hub"})]
    assert compared(tmp_path / "1", {"http": a}, {"http": b}, exact_only=True).ok
    c = [http_record("/healthz", body={"status": "bad", "runtime": "hub"})]
    assert "http" in streams_of(compared(tmp_path / "2", {"http": a}, {"http": c}, exact_only=True))
    d = [http_record("/api/status", body={"runtime": "hostapp"})]
    e = [http_record("/api/status", body={"runtime": "hub"})]
    assert "http" in streams_of(compared(tmp_path / "3", {"http": d}, {"http": e}, exact_only=True))


def test_http_latency_may_not_get_worse(tmp_path):
    slow = [http_record(latency_s=0.5)]
    report = compared(tmp_path / "1", {"http": [http_record(latency_s=0.2)]}, {"http": slow})
    assert "http_latency_s" in streams_of(report)
    report = compared(tmp_path / "2", {"http": slow}, {"http": [http_record(latency_s=0.2)]})
    assert "http_latency_s" not in streams_of(report)


def test_a_client_call_may_not_get_slower_per_method(tmp_path):
    def client(apply_s: float) -> list[dict]:
        return [apply_call(1.0 + i, f"m{i}", dur=apply_s) for i in range(5)]

    report = compared(tmp_path, {"client": client(0.05)}, {"client": client(0.2)})
    assert any("[apply]" in v.detail for v in report.violations)


@pytest.mark.parametrize("change, passes", [
    ("<html>a <b>x</b></html>", False),
    (f"<html>session {OTHER_SID} at 2026-11-04T10:00:00Z</html>", True),
])
def test_html_is_byte_exact_after_masking(tmp_path, change, passes):
    original = {"session.html": f"<html>session {SID} at 2026-11-03T09:00:00Z</html>"}
    report = compared(tmp_path, {"html": original}, {"html": {"session.html": change}},
                      exact_only=True)
    assert report.ok is passes


def test_a_page_missing_from_one_side_is_caught(tmp_path):
    report = compared(tmp_path, {"html": {"a.html": "x", "b.html": "y"}},
                      {"html": {"a.html": "x"}}, exact_only=True)
    assert "html" in streams_of(report)


def test_verbs_are_an_exact_sequence_with_a_time(tmp_path):
    verbs = [{"t": 1.0, "cmd": "end"}, {"t": 2.0, "cmd": "home"}]
    order = [{"t": 1.0, "cmd": "home"}, {"t": 2.0, "cmd": "end"}]
    assert "pending" in streams_of(compared(tmp_path / "1", {"pending": verbs}, {"pending": order},
                                            exact_only=True))
    late = [{"t": 1.0, "cmd": "end"}, {"t": 3.0, "cmd": "home"}]
    assert "pending_appearance_s" in streams_of(compared(tmp_path / "2", {"pending": verbs},
                                                         {"pending": late}))


def test_a_timer_driven_scenario_keeps_the_order_and_gives_up_the_time(tmp_path):
    verbs = [{"t": 1.0, "cmd": "end"}]
    late = [{"t": 3.0, "cmd": "end"}]
    meta = {"untimed": ["pending"]}
    report = compared(tmp_path / "1", {"pending": verbs, "meta": meta},
                      {"pending": late, "meta": meta})
    assert "pending_appearance_s" not in streams_of(report)
    report = compared(tmp_path / "2", {"pending": verbs, "meta": meta},
                      {"pending": [{"t": 1.0, "cmd": "home"}], "meta": meta})
    assert "pending" in streams_of(report)


def test_gate_edges_keep_their_order_and_time(tmp_path):
    other = copy.deepcopy(BASE_CLIENT)
    other[-1]["args"]["enabled"] = True
    assert "flags" in streams_of(compared(tmp_path / "1", {}, {"client": other}, exact_only=True))
    late = copy.deepcopy(BASE_CLIENT)
    late[-1]["t_start"] += 1.0
    assert "gate_edge_s" in streams_of(compared(tmp_path / "2", {}, {"client": late}))


def test_a_repeated_gate_push_is_not_an_edge():
    records = [call(1.0, "set_gaze", enabled=True), call(1.1, "set_gaze", enabled=True),
               call(1.2, "set_gaze", enabled=False)]
    assert [edge["value"] for edge in flag_edges(records)] == [True, False]


def test_the_nodes_own_gate_state_keeps_its_order(tmp_path):
    a = [{"t": 1.0, "gaze_run": False}, {"t": 2.0, "gaze_run": True}, {"t": 3.0, "gaze_run": False}]
    never_off = [{"t": 1.0, "gaze_run": False}, {"t": 2.0, "gaze_run": True},
                 {"t": 3.0, "gaze_run": True}]
    assert "sys" in streams_of(compared(tmp_path / "1", {"sys": a}, {"sys": never_off},
                                        exact_only=True))
    slower = [{"t": 1.0, "gaze_run": False}, {"t": 2.0, "gaze_run": False},
              {"t": 3.0, "gaze_run": True}, {"t": 4.0, "gaze_run": False}]
    assert compared(tmp_path / "2", {"sys": a}, {"sys": slower}, exact_only=True).ok


def test_mic_changes_keep_their_order_and_time(tmp_path):
    a = [{"t": 1.0, "profile": "session"}, {"t": 2.0, "profile": "idle"}]
    b = [{"t": 1.0, "profile": "idle"}, {"t": 2.0, "profile": "session"}]
    assert "mic" in streams_of(compared(tmp_path / "1", {"mic": a}, {"mic": b}, exact_only=True))
    late = [{"t": 1.0, "profile": "session"}, {"t": 3.0, "profile": "idle"}]
    assert "mic_change_s" in streams_of(compared(tmp_path / "2", {"mic": a}, {"mic": late}))


def test_the_journal_gives_the_same_mic_stream():
    entries = [{"MESSAGE": "mic profile session (capture gain 1.40)", "__MONOTONIC_TIMESTAMP": "5000000"},
               {"MESSAGE": "mic profile: wpctl failed", "__MONOTONIC_TIMESTAMP": "6000000"},
               {"MESSAGE": "other", "__MONOTONIC_TIMESTAMP": "7000000"}]
    assert parse_mic_journal(entries) == [{"t": 5.0, "profile": "session"}]


@pytest.mark.parametrize("stream", ["events", "fiware", "speech", "offline"])
def test_ordered_streams_are_exact_after_masking(tmp_path, stream):
    a = [{"t": 1.0, "type": "one", "session_id": SID}, {"t": 2.0, "type": "two"}]
    same = [{"t": 9.0, "type": "one", "session_id": OTHER_SID}, {"t": 9.5, "type": "two"}]
    assert compared(tmp_path / "1", {stream: a}, {stream: same}, exact_only=True).ok
    swapped = list(reversed(same))
    assert stream in streams_of(compared(tmp_path / "2", {stream: a}, {stream: swapped},
                                         exact_only=True))
    changed = [{"t": 1.0, "type": "one", "session_id": SID, "cue_family": "tension"},
               {"t": 2.0, "type": "two"}]
    assert stream in streams_of(compared(tmp_path / "3", {stream: a}, {stream: changed},
                                         exact_only=True))


def test_recordings_are_exact_after_masking(tmp_path):
    a = {"session-01.jsonl": json.dumps({"type": "x", "timestamp": "2026-11-03T09:00:00Z",
                                         "session_id": SID})}
    same = {"session-01.jsonl": json.dumps({"type": "x", "timestamp": "2026-11-04T09:00:00Z",
                                            "session_id": OTHER_SID})}
    other = {"session-01.jsonl": json.dumps({"type": "y", "timestamp": "2026-11-03T09:00:00Z",
                                             "session_id": SID})}
    assert compared(tmp_path / "1", {"recordings": a}, {"recordings": same}, exact_only=True).ok
    assert "recordings" in streams_of(compared(tmp_path / "2", {"recordings": a},
                                               {"recordings": other}, exact_only=True))


def test_rendered_email_is_exact_after_masking(tmp_path):
    a = {"session-01.html": f"<p>{SID} scored 64</p>"}
    same = {"session-01.html": f"<p>{OTHER_SID} scored 64</p>"}
    other = {"session-01.html": f"<p>{SID} scored 65</p>"}
    assert compared(tmp_path / "1", {"email": a}, {"email": same}, exact_only=True).ok
    assert "email" in streams_of(compared(tmp_path / "2", {"email": a}, {"email": other},
                                          exact_only=True))


def test_database_rows_are_exact_after_masking(tmp_path):
    row = {"session_id": SID, "status": "completed", "end_reason": None, "cycles": 2}
    a = {"sessions": [row], "events": [{"session_id": SID, "kind": "session_started"}]}
    same = copy.deepcopy(a)
    same["sessions"][0]["session_id"] = OTHER_SID
    same["events"][0]["session_id"] = OTHER_SID
    assert compared(tmp_path / "1", {"db": a}, {"db": same}, exact_only=True).ok
    for number, (table, change) in enumerate((("sessions", {"status": "abandoned"}),
                                              ("sessions", {"end_reason": "walkaway_ends"}),
                                              ("sessions", {"cycles": 3}),
                                              ("events", {"kind": "other"}))):
        changed = copy.deepcopy(a)
        changed[table][0].update(change)
        report = compared(tmp_path / f"c{number}", {"db": a}, {"db": changed}, exact_only=True)
        assert "db" in streams_of(report), change


def test_a_stream_on_one_side_only_is_a_violation(tmp_path):
    report = compared(tmp_path, {"fiware": [{"method": "POST"}]}, {}, exact_only=True)
    assert "fiware" in streams_of(report)


def test_a_reference_that_disagrees_with_itself_cannot_judge(tmp_path):
    a = [make_run(tmp_path, "a/run-001", pending=[{"t": 1.0, "cmd": "end"}]),
         make_run(tmp_path, "a/run-002", pending=[{"t": 1.0, "cmd": "home"}]),
         make_run(tmp_path, "a/run-003", pending=[{"t": 1.0, "cmd": "end"}])]
    b = side(tmp_path, "b", pending=[{"t": 1.0, "cmd": "end"}])
    report = C.compare_scenario("s", a, b, TOLERANCES, exact_only=True)
    assert any("disagrees with itself" in v.detail for v in report.violations)


# --- timed quantities -----------------------------------------------------------------


def paced(gap: float) -> list[dict]:
    return BASE_CLIENT[:3] + [apply_call(3.0 + gap * i, "gesture" + " (part)" * (i % 2), speech=True,
                                         pitch=float(i), duration=0.5) for i in range(6)]


def test_inter_intent_time_is_compared_only_inside_a_batch(tmp_path):
    report = compared(tmp_path / "1", {"client": paced(0.4)}, {"client": paced(0.5)})
    assert "inter_intent_s" in streams_of(report)
    report = compared(tmp_path / "2", {"client": paced(0.4)}, {"client": paced(0.41)})
    assert "inter_intent_s" not in streams_of(report)


def test_a_voice_paced_gap_belongs_to_the_voice(tmp_path):
    def beats_at(gap: float) -> list[dict]:
        return BASE_CLIENT[:3] + [apply_call(3.0 + gap * i, f"beat {i}", speech=True, pace="voice",
                                             pitch=float(i), duration=0.5) for i in range(6)]

    report = compared(tmp_path, {"client": beats_at(0.3)}, {"client": beats_at(0.5)})
    assert "inter_intent_s" not in streams_of(report)


def test_two_gestures_in_a_row_are_not_one_batch(tmp_path):
    def pair(gap: float) -> list[dict]:
        return BASE_CLIENT[:3] + [apply_call(3.0 + gap * i, f"gesture {i}", pitch=float(i), duration=0.5)
                                  for i in range(6)]

    report = compared(tmp_path, {"client": pair(0.4)}, {"client": pair(0.6)})
    assert "inter_intent_s" not in streams_of(report)


def test_a_run_with_a_longer_pause_between_plans_has_no_inter_intent_change(tmp_path):
    a = paced(0.4) + [apply_call(20.0, "gesture", pitch=9.0)]
    b = paced(0.4) + [apply_call(40.0, "gesture", pitch=9.0)]
    assert "inter_intent_s" not in streams_of(compared(tmp_path, {"client": a}, {"client": b}))


@pytest.mark.parametrize("name, quantity", [("envelope_post", "post_to_first_intent_s"),
                                            ("voice_onset", "voice_onset_to_first_move_s")])
def test_first_move_answers_are_compared(tmp_path, name, quantity):
    marks = [{"name": name, "t": 2.9}]
    report = compared(tmp_path / "1", {"marks": marks}, {"marks": [{"name": name, "t": 2.5}]})
    assert quantity in streams_of(report)
    report = compared(tmp_path / "2", {"marks": marks}, {"marks": marks})
    assert quantity not in streams_of(report)


def ticking(interval: float) -> list[dict]:
    return BASE_CLIENT + [call(10.0 + interval * i, "read_state") for i in range(30)]


def test_tick_intervals_are_judged_on_passive_runs_only(tmp_path):
    meta = {"kind": "passive"}
    report = compared(tmp_path / "1", {"client": ticking(0.1), "meta": meta},
                      {"client": ticking(0.2), "meta": meta})
    assert "tick_interval_s" in streams_of(report)
    report = compared(tmp_path / "2", {"client": ticking(0.1)}, {"client": ticking(0.2)})
    assert "tick_interval_s" not in streams_of(report)


def test_a_quantity_with_samples_on_one_side_only_is_caught(tmp_path):
    report = compared(tmp_path, {"marks": [{"name": "voice_onset", "t": 2.9}]}, {"marks": []})
    assert "voice_onset_to_first_move_s" in streams_of(report)


def extra_side(tmp_path: Path, label: str, value: float) -> list[Run]:
    runs = side(tmp_path, label)
    for run in runs:
        run.extra["recorder_quantity_s"] = [value]
    return runs


def test_a_quantity_from_another_recorder_and_tolerance_file_is_judged(tmp_path):
    more = copy.deepcopy(TOLERANCES)
    more["quantities"]["recorder_quantity_s"] = {"median": {"relative": 0.1}}
    report = C.compare_scenario("s", extra_side(tmp_path, "a", 1.0), extra_side(tmp_path, "b", 2.0),
                                more)
    assert "recorder_quantity_s" in streams_of(report)


def test_extra_tolerance_files_merge_over_the_tracked_one(tmp_path):
    extra = tmp_path / "more.yaml"
    extra.write_text("floors: {field_hz: 45}\nquantities:\n  inter_intent_s:\n    max: {sim: 9}\n",
                     encoding="utf-8")
    merged = C.load_tolerances([ROOT / "deploy" / "hub" / "golden_tolerances.yaml", extra])
    assert merged["floors"]["field_hz"] == 45
    assert merged["quantities"]["inter_intent_s"]["max"] == {"robot": 0.150, "sim": 9}
    assert merged["quantities"]["inter_intent_s"]["median"]["sim"] == 0.030


def test_a_floor_on_a_system_field_is_applied(tmp_path):
    tolerances = {**TOLERANCES, "floors": {"field_hz": 45}}
    report = compared(tmp_path, {}, {"sys": [{"t": 1.0, "field_hz": 30}]}, tolerances=tolerances,
                      exact_only=False)
    assert any("field_hz" in v.detail for v in report.violations)


def test_the_self_check_requires_its_witnesses_on_both_sides(tmp_path):
    meta = {"self_check": ["play_recorded_move", "set_gaze"]}
    client = [c for c in BASE_CLIENT if c["method"] != "play_recorded_move"]
    report = compared(tmp_path, {"client": client, "meta": meta}, {"client": client, "meta": meta},
                      exact_only=True)
    assert "self-check" in streams_of(report)


def test_the_spread_report_shows_what_a_side_can_judge(tmp_path):
    runs = [make_run(tmp_path, f"r/run-{i}", http=[http_record(latency_s=value)])
            for i, value in enumerate((0.1, 0.2, 0.9))]
    lines, violations = C.spread_report("s", runs, TOLERANCES)
    assert any(line.startswith("http_latency_s p95") for line in lines)
    assert any("too noisy" in v.detail for v in violations)


# --- the masks ------------------------------------------------------------------------


def test_masks_are_stable_and_idempotent():
    normaliser = Normaliser()
    first = normaliser.text(f"a {SID} b {OTHER_SID} a {SID}")
    assert first == "a <sid:1> b <sid:2> a <sid:1>"
    assert normaliser.text(first) == first


def test_a_badge_bound_session_id_is_one_identity():
    tail = "-rqh-" + "9" * 32
    assert Normaliser().text(SID + tail) == "<sid:1>"


def test_clock_fields_are_masked_and_other_numbers_are_not():
    masked = Normaliser().value({"started_at": 12.5, "timestamp": "x", "t": 3.0, "cycles": 3,
                                 "avg_engagement": 0.5, "started": True, "at": None})
    assert masked == {"started_at": "<time>", "timestamp": "<time>", "t": "<time>", "cycles": 3,
                      "avg_engagement": 0.5, "started": True, "at": None}


def test_a_document_is_masked_by_line():
    text = mask_document(json.dumps({"timestamp": "z", "ok": 1}) + "\nplain 2026-11-03T09:00:00Z\n")
    assert text == '{"ok": 1, "timestamp": "<time>"}\nplain <time>'


# --- the tracked files ----------------------------------------------------------------


def tracked_harness_files() -> list[Path]:
    return [ROOT / "scripts" / "golden_trace.py", ROOT / "deploy" / "hub" / "golden_tolerances.yaml",
            Path(__file__), ROOT / "tests" / "test_golden_scenarios.py",
            *sorted((ROOT / "scripts" / "golden").glob("*.py"))]


def test_the_tolerance_file_holds_relative_rules_only():
    text = (ROOT / "deploy" / "hub" / "golden_tolerances.yaml").read_text(encoding="utf-8")
    for quantity in ("piper_same_sentence_s", "tick_interval_s"):
        assert "relative" in str(TOLERANCES["quantities"][quantity])
    assert "floors" not in TOLERANCES, "a floor is one robot's figure"
    assert not re.search(r"\b(MemAvailable|joint_states|4\.[0-9]+ ?s)\b", text)


def test_the_clock_boundaries_are_the_schedulers_own():
    from emori_adapter.behavior import cue_scheduler

    boundaries = TOLERANCES["plan"]["clock_boundaries_s"]
    assert boundaries["recorded_gap"] == cue_scheduler._RECORDED_GAP_S
    assert boundaries["carry_ttl"] == cue_scheduler._CARRY_TTL_S


def test_the_voice_scale_range_is_the_players_own():
    # driver.py: scale = 0.55 + 0.45 * level, level clamped to 0..1.
    source = (ROOT / "src" / "emori_adapter" / "hostapp" / "driver.py").read_text(encoding="utf-8")
    assert "0.55 + 0.45 * max(0.0, min(1.0, float(voice.level)))" in source
    assert TOLERANCES["plan"]["antenna_voice_range"] == [0.55, 1.0]


def test_tracked_harness_files_carry_no_plan_vocabulary():
    plan_ids = re.compile(r"\b(?:T\d{2,3}|S-\d{2}|SG-\d+|SP-\d+|OP\d+|N\d{2})\b")
    for path in tracked_harness_files():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert not plan_ids.search(text), f"{path.name}: {plan_ids.search(text).group(0)}"


def test_a_floor_on_a_field_nobody_wrote_fails(tmp_path):
    tolerances = {**TOLERANCES, "floors": {"field_hz": 45}}
    report = compared(tmp_path, {}, {"sys": [{"t": 1.0, "other": 3}]}, tolerances=tolerances,
                      exact_only=False)
    assert any("no reading" in v.detail for v in report.violations)
