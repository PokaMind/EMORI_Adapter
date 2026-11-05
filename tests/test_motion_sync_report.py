"""scripts/motion_sync_report.py on synthetic runs: every gate can fail, and no gap hides.

Each test starts from a run that passes every gate the streams can measure, breaks one
thing, and checks the one gate that should notice. The runs are built here from nothing:
a head that moves on a schedule, a trace written in the shape ``motion_trace.py`` writes,
and the manifest, truth, published and load streams around them.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "motion_sync_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("motion_sync_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["motion_sync_report"] = module  # dataclasses resolve string annotations here
    spec.loader.exec_module(module)
    return module


M = _load()

T0 = 1000.0
STEP = 0.02                 # 50 Hz joint samples
HEAD_RAD = 0.2              # a bit over 11 degrees
SPEECH_S = 1.5


def _ramp(t: float, start: float, length: float = 0.3) -> float:
    return HEAD_RAD * min(1.0, max(0.0, (t - start) / length))


class Synth:
    """One synthetic run. ``make()`` fills the stream lists, ``write()`` saves them.

    Sentence i speaks from ``tv`` for 1.5 s, its event arrives 300 ms before the voice, the
    tap sees the voice 120 ms before it is audible (a delay line), the first command goes out
    90 ms before the voice and the head starts moving 30 ms before it. Every fourth sentence
    ends a reply. A passing run therefore has a lag of about +10 ms.
    """

    def __init__(self, n=40, name="run1", scripted=True, source="http", k_ms=0.0,
                 spacing=3.0, move_start=None, apply0=-0.09, settle_gap=None,
                 durations=(0.5, 0.5, 0.7), joint_kind="head", profile="loaded",
                 with_waiting=None, t_base=T0, silent=(), arb=False):
        self.with_arb = arb
        self.n, self.name, self.scripted, self.source = n, name, scripted, source
        self.k_ms, self.spacing, self.profile = k_ms, spacing, profile
        self.move_start = move_start or (lambda i: -0.03)
        self.apply0 = apply0 if callable(apply0) else (lambda i: apply0)
        self.settle_gap = settle_gap or (lambda i: 0.05)
        self.durations = durations
        self.joint_kind = joint_kind
        self.with_waiting = with_waiting
        self.silent = set(silent)         # sentences whose audio never plays
        self.t_base = t_base
        self.make()

    # --- timeline -----------------------------------------------------------
    def tv(self, i: int) -> float:
        return self.t_base + 1.0 + self.spacing * i

    def family(self, i: int) -> str:
        return "agree" if i % 2 == 0 else ""

    def make(self) -> None:
        self.manifest = {"schema": 1, "run_id": self.name, "commit": "abc1234",
                         "profile": self.profile, "lang": "en", "motion_source": self.source,
                         "k_ms": self.k_ms,
                         "t_wav0": self.t_base, "sentences": []}
        self.trace = [{"s": "meta", "t": self.t_base, "schema": 1, "pid": 1, "wall": 0.0}]
        self.truth = {"sentences": []}
        self.published = []
        self.ramps: list[tuple[float, float]] = []   # (start of the way up, start of the way down)
        for i in range(self.n):
            self._sentence(i)
        end = self.tv(self.n) + 2.0
        self.joints = []
        t = self.t_base
        while t < end:
            self.joints.append(self._joint(t))
            t += STEP
        self.moves = []
        t = self.t_base
        while t < end:
            rec = {"t": round(t, 3), "running": 1}
            if self.with_waiting is not None:
                rec["waiting"] = self.with_waiting
            self.moves.append(rec)
            t += 0.1
        self.load = [{"t": self.t_base + k, "load1": 1.0 + (k % 4), "hostapp_cpu": 20.0,
                      "pipewire_cpu": 5.0} for k in range(int(end - self.t_base))]

    def _sentence(self, i: int) -> None:
        seq, plan = i + 1, i + 1
        tv = self.tv(i)
        fam = self.family(i)
        recorded = "wave" if i % 10 == 5 else ""
        te = tv - 0.3
        self.manifest["sentences"].append({
            "seq": seq, "case": f"c{i % 8}", "family": fam or None, "cue_body_given": False,
            "expect_family": fam or None, "chars": 40, "audio": i not in self.silent,
            "wav_start_s": (tv - self.t_base) if self.scripted else None,
            "wav_end_s": (tv + SPEECH_S - self.t_base) if self.scripted else None,
            "event_offset_ms": -300, "t_event_planned": te, "expected_exception": None})
        if self.scripted:
            self.truth["sentences"].append({"seq": seq, "t_voice": tv, "t_end": tv + SPEECH_S})
        add = self.trace.append
        add({"s": "sentence", "t": te, "plan": plan, "seq": seq, "chars": 40,
             "authored": fam or None, "performed": fam, "slug": "talk", "family_source": "authored",
             "recorded": recorded, "planned": True, "budget": 1.5, "intents": 4})
        add({"s": "pickup", "t": te + 0.005, "plan": plan, "n": 4, "speech": True, "voice": True})
        if i in self.silent:
            self._silent_sentence(i, plan, tv)
            return
        tap = tv - 0.12
        add({"s": "voice", "t": tap, "edge": "rise"})
        add({"s": "voice", "t": tv + 0.7, "edge": "onset", "kind": "dip"})
        last = tv + SPEECH_S - 0.12
        add({"s": "voice", "t": last + 0.2, "edge": "fall", "last": last})
        add({"s": "voice_seen", "t": tap + 0.02, "plan": plan, "i": 0, "bind": "onset"})
        steps = [(0, self.apply0(i), self.durations[0]), (1, 0.4, self.durations[1]),
                 (2, 0.8, self.durations[2])]
        for idx, rel, d in steps:
            kind = "play_recorded" if (recorded and idx == 0) else "goto"
            rec = {"s": "apply", "t": tv + rel, "plan": plan, "i": idx, "kind": kind,
                   "reason": "gesture", "d": d, "ms": 12.0}
            if kind == "play_recorded":
                rec.update(move=recorded, accepted=True)
            self._apply(rec)
        settle_t = tv + SPEECH_S + self.settle_gap(i)
        self._apply({"s": "apply", "t": settle_t, "plan": plan, "i": 3, "kind": "goto",
                     "reason": "settle", "d": 0.3, "ms": 12.0})
        add({"s": "batch_end", "t": settle_t + 0.4, "plan": plan, "i": 4, "n": 4, "moved": True,
             "revoked": False, "voice_seen": True, "sustain": 0})
        if seq % 4 == 0:
            add({"s": "tts_ended", "t": tv + SPEECH_S + 0.04, "settle": True})
        self.published.append({"t": tv, "seq": seq, "cue_family": fam or None,
                               "cue_category": fam or None})
        start = self.move_start(i)
        if start is not None:
            self.ramps.append((tv + start, settle_t + 0.03))

    def _silent_sentence(self, i: int, plan: int, tv: float) -> None:
        """No voice ever arrives: the driver performs nothing but the settle."""

        settle_t = tv + SPEECH_S + self.settle_gap(i)
        self._apply({"s": "apply", "t": settle_t, "plan": plan, "i": 3, "kind": "goto",
                     "reason": "settle", "d": 0.3, "ms": 12.0})
        self.trace.append({"s": "batch_end", "t": settle_t + 0.4, "plan": plan, "i": 4, "n": 4,
                           "moved": False, "revoked": False, "voice_seen": False, "sustain": 0})
        self.published.append({"t": tv, "seq": i + 1, "cue_family": self.family(i) or None,
                               "cue_category": self.family(i) or None})

    def _apply(self, rec: dict) -> None:
        self.trace.append(rec)
        if self.source == "http":
            self.trace.append({"s": "post", "t": rec["t"] + 0.004, "path": "/api/move/goto",
                               "status": 200})

    def _joint(self, t: float) -> dict:
        amount = sum(_ramp(t, up) - _ramp(t, down) for up, down in self.ramps)
        head = [0.0, amount, 0.0] if self.joint_kind == "head" else [0.0, 0.0, 0.0]
        ant = [amount, 0.0] if self.joint_kind == "antenna" else [0.0, 0.0]
        return {"t": round(t, 4), "rpy": head, "ant": ant, "yaw": 0.0}

    def arb_records(self, *, start_after=0.01, queued=1) -> list[dict]:
        out = []
        for seq, rec in enumerate(r for r in self.trace if r["s"] == "apply"):
            base = {"seq": seq, "move_id": f"m{seq}", "kind": rec["kind"], "client": "hostapp",
                    "lane": "interaction", "reason": f"rest:goto:{rec['plan']}.{rec['i']}",
                    "error_code": ""}
            out.append({**base, "t": rec["t"], "event": "accepted", "queued": queued})
            out.append({**base, "t": rec["t"] + start_after, "event": "started", "queued": 0})
            out.append({**base, "t": rec["t"] + start_after + rec["d"], "event": "settled",
                        "queued": 0})
        return out

    # --- helpers ------------------------------------------------------------
    def records(self, stream: str, plan: int | None = None) -> list[dict]:
        return [r for r in self.trace if r["s"] == stream
                and (plan is None or r.get("plan") == plan)]

    def drop(self, stream: str, plan: int) -> None:
        self.trace = [r for r in self.trace if not (r["s"] == stream and r.get("plan") == plan)]

    def write(self, root: Path) -> Path:
        path = root / self.name
        path.mkdir(parents=True)

        def lines(items):
            return "\n".join(json.dumps(r) for r in items) + "\n"

        (path / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (path / "trace.jsonl").write_text(lines(self.trace), encoding="utf-8")
        (path / "published.jsonl").write_text(lines(self.published), encoding="utf-8")
        if self.with_arb:
            (path / "arb.jsonl").write_text(lines(self.arb_records()), encoding="utf-8")
        if self.scripted:
            (path / "truth.json").write_text(json.dumps(self.truth), encoding="utf-8")
        for fname, items in (("joints.jsonl", self.joints), ("moves.jsonl", self.moves),
                             ("load.jsonl", self.load)):
            if self.scripted or fname != "moves.jsonl":
                (path / fname).write_text(lines(items), encoding="utf-8")
        return path


def report(tmp_path, *runs, mode="final", baseline=None, tolerance=50.0):
    paths = [r.write(tmp_path) if isinstance(r, Synth) else r for r in runs]
    summary, code, text = M.build_report(paths, mode, baseline, tolerance)
    return summary, code, text


def gate(summary, gid):
    return next(g for g in summary["gates"] if g["id"] == gid)


def status(summary, gid):
    return gate(summary, gid)["status"]


def row(summary, seq, run=None):
    return next(r for r in summary["rows"] if r["seq"] == seq and (run is None or r["run"] == run))


# ---------------------------------------------------------------------------
# a passing run
# ---------------------------------------------------------------------------
def test_a_good_run_passes_every_gate_the_streams_can_measure(tmp_path):
    summary, code, text = report(tmp_path, Synth())
    by = {g["id"]: g["status"] for g in summary["gates"]}
    assert by["DATA"] == by["SG-1"] == by["SG-2"] == by["SG-3"] == M.PASS
    assert by["SG-4a"] == by["SG-4b"] == by["SG-7"] == by["SG-8"] == M.PASS
    assert by["SG-9"] == by["SG-10"] == by["SG-12"] == by["EXC"] == M.PASS
    # Not measurable from these streams, and never a pass.
    assert by["SG-5"] == by["SG-6"] == by["SG-11"] == by["SG-13"] == M.NOT_MEASURABLE
    assert "motion sync report" in text and "Delay decomposition" in text
    # Forty sentences are fewer than a scripted baseline needs.
    assert by["SAMPLE"] == M.FAIL
    json.dumps(M._clean(summary), allow_nan=False)


def test_m0_asks_only_for_data_completeness(tmp_path):
    synth = Synth(move_start=lambda i: 0.4)          # a late head: information at M0
    summary, code, _ = report(tmp_path, synth, mode="m0")
    assert status(summary, "SG-1") == M.FAIL
    assert [g["id"] for g in summary["gates"] if g["selected"]] == ["DATA"]
    assert code == 0


def test_a_gate_that_cannot_be_measured_gives_its_own_exit_status(tmp_path):
    _, code, _ = report(tmp_path, Synth(), mode="m1")
    assert code in (1, 3)                            # SAMPLE fails on 40 sentences
    big = [Synth(n=60, name=f"r{k}") for k in range(4)]
    summary, code, _ = report(tmp_path, *big, mode="m1")
    assert status(summary, "SAMPLE") == M.PASS
    assert status(summary, "SG-5") == status(summary, "SG-6") == M.NOT_MEASURABLE
    assert code == 3


def test_a_plan_revoked_with_only_its_settle_left_is_complete(tmp_path):
    # The end of the turn revokes every plan that sustains up to it. That is the normal end.
    synth = _without_settle(Synth(), {6})
    next(r for r in synth.trace if r["s"] == "batch_end" and r["plan"] == 6).update(
        revoked=True, i=3)
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 6)["outcome"] == "complete"


def test_a_sentence_with_no_audio_is_no_voice_even_when_the_next_one_revokes_it(tmp_path):
    synth = Synth(silent=(3,))
    next(r for r in synth.trace if r["s"] == "batch_end" and r["plan"] == 4).update(revoked=True)
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 4)["outcome"] == "no_voice"
    assert not any("E6" in line for line in gate(summary, "EXC")["detail"][1:])


def test_the_chain_starts_from_the_edge_nearest_this_sentence(tmp_path):
    synth = Synth()
    # A syllable of the sentence before, inside the window, must not stand in for this start.
    synth.trace.append({"s": "voice", "t": synth.tv(3) - 0.45, "edge": "onset", "kind": "jump"})
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 4)["t_tap"] == pytest.approx(synth.tv(3) - 0.12, abs=1e-3)


def test_the_event_offset_error_is_against_the_monitor_not_the_room(tmp_path):
    synth = Synth(k_ms=150.0)
    for rec in synth.truth["sentences"]:
        rec["t_voice"] -= 0.15
        rec["t_end"] -= 0.15
    for s, rec in zip(synth.manifest["sentences"], synth.truth["sentences"]):
        s["event_offset_ms"] = -150.0
    summary, _, _ = report(tmp_path, synth)
    assert abs(summary["rollups"]["scripted"]["event_offset_error"]["median"]) < 1.0


def _observed(n=30, name="live", **kwargs):
    # What ``--observe`` writes: nobody scripted these sentences, so the manifest has none.
    real = Synth(n=n, name=name, scripted=False, k_ms=120.0, source="http", **kwargs)
    real.manifest["sentences"] = []
    return real


def test_a_real_session_with_no_script_is_read_from_its_trace(tmp_path):
    real = _observed()
    summary, _, _ = report(tmp_path, real)
    assert status(summary, "DATA") == M.PASS and status(summary, "SAMPLE") == M.PASS
    assert len(summary["rows"]) == 30
    r = row(summary, 3)
    assert r["voice_src"] == "tap" and r["lag_ms"] is not None
    assert status(summary, "SG-9") == M.PASS        # no script, so nothing to disagree with


def test_a_run_with_no_sentence_anywhere_is_a_data_problem(tmp_path):
    real = _observed()
    real.trace = [r for r in real.trace if r["s"] != "sentence"]
    summary, code, _ = report(tmp_path, real)
    assert status(summary, "DATA") == M.FAIL and code == 1


def test_a_repeat_of_a_spoken_sentence_is_not_a_new_row(tmp_path):
    real = _observed()
    real.trace.append({"s": "sentence", "t": real.tv(29) + 3.0, "plan": 0, "seq": 99, "chars": 40,
                       "authored": None, "deduped": True})
    summary, _, _ = report(tmp_path, real)
    assert len(summary["rows"]) == 30


def test_an_event_posted_late_takes_the_rise_it_landed_on(tmp_path):
    real = _observed()
    # The event arrives 400 ms after the voice rose, before the sentence's mid-way onset.
    for rec in real.records("sentence"):
        if rec["seq"] == 4:
            rec["t"] = real.tv(3) - 0.12 + 0.4
    summary, _, _ = report(tmp_path, real)
    r = row(summary, 4)
    assert r["t_tap"] == pytest.approx(real.tv(3) - 0.12, abs=1e-3)


def test_a_rise_an_earlier_sentence_owns_is_not_taken_again(tmp_path):
    real = _observed()
    for rec in real.records("sentence"):      # two events on the one voice
        if rec["seq"] == 5:
            rec["t"] = real.tv(3) + 0.1
    summary, _, _ = report(tmp_path, real)
    assert row(summary, 5)["t_tap"] > row(summary, 4)["t_tap"]


def test_a_sentence_that_never_spoke_does_not_take_the_next_ones_voice(tmp_path):
    real = _observed(silent=(5,))
    summary, _, _ = report(tmp_path, real)
    silent, after = row(summary, 6), row(summary, 7)
    assert silent["exception"] == "E6" and silent["t_voice"] is None
    assert after["t_tap"] == pytest.approx(real.tv(6) - 0.12, abs=1e-3)


def test_the_report_says_which_way_the_voice_time_can_be_wrong(tmp_path):
    synth = Synth(k_ms=41.0)
    path = synth.write(tmp_path)
    (path / "calibration.json").write_text(json.dumps({"k_ms": 41.0, "note": "upper bound on it"}),
                                           encoding="utf-8")
    _, _, text = report(tmp_path, path)
    assert "k_ms is an upper bound on it" in text
    assert "plus 61 ms" in text


def test_scripted_and_real_runs_are_never_pooled(tmp_path):
    real = Synth(n=30, name="live", scripted=False, k_ms=120.0, source="ros")
    summary, _, _ = report(tmp_path, Synth(n=30), real, mode="final")
    assert set(gate(summary, "SG-1")["pools"]) == {"scripted", "real"}
    assert {r["pool"] for r in summary["rows"]} == {"scripted", "real"}


# ---------------------------------------------------------------------------
# lag
# ---------------------------------------------------------------------------
def test_lag_is_measured_from_the_voice_and_reports_the_joint_interval(tmp_path):
    summary, _, text = report(tmp_path, Synth())
    lags = [r["lag_ms"] for r in summary["rows"]]
    assert all(-10 <= v <= 30 for v in lags)
    assert "interval median 20.0 ms" in text
    r = row(summary, 3)
    assert r["voice_src"] == "truth" and r["move_status"] == "moved"
    assert r["chain_ms"] == pytest.approx(r["lag_ms"] + 120.0, abs=1.0)


def test_a_late_head_fails_sg1_and_sg2_but_meets_the_interim_limits(tmp_path):
    late = Synth(move_start=lambda i: 0.07, apply0=0.0)      # about +100 ms
    summary, code, _ = report(tmp_path, late, mode="final")
    assert status(summary, "SG-1") == status(summary, "SG-2") == M.FAIL
    assert code == 1
    summary, _, _ = report(tmp_path / "again", late, mode="m1")
    assert status(summary, "SG-1") == status(summary, "SG-2") == M.PASS


def test_one_very_late_sentence_fails_the_worst_case_only(tmp_path):
    synth = Synth(move_start=lambda i: 0.35 if i == 7 else -0.03)
    summary, _, _ = report(tmp_path, synth)
    g = gate(summary, "SG-1")
    assert g["status"] == M.FAIL
    verdict = g["detail"][0].split("->")[1]
    assert "max" in verdict and "median" not in verdict and "p95" not in verdict


def test_a_head_that_moves_long_before_its_voice_fails_the_early_limit(tmp_path):
    synth = Synth(move_start=lambda i: -0.2 if i == 7 else -0.03,
                  apply0=-0.25)
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 8)["lag_ms"] < -120
    assert "min" in gate(summary, "SG-1")["detail"][0].split("->")[1]


def test_a_sentence_with_no_motion_never_passes(tmp_path):
    synth = Synth(move_start=lambda i: None if i == 4 else -0.03)
    summary, _, _ = report(tmp_path, synth)
    r = row(summary, 5)
    assert r["move_status"] == "no motion" and r["lag_ms"] == math.inf
    for gid in ("SG-1", "SG-2", "SG-10"):
        assert status(summary, gid) == M.FAIL
    assert "never moved" in gate(summary, "SG-1")["detail"][0]


def test_an_antenna_only_motion_counts_for_sg1_and_not_sg2(tmp_path):
    summary, _, _ = report(tmp_path, Synth(joint_kind="antenna", move_start=lambda i: -0.09))
    assert status(summary, "SG-1") == M.PASS
    assert status(summary, "SG-2") == M.FAIL


def test_a_pose_still_moving_from_the_last_sentence_is_not_this_ones_first_move(tmp_path):
    # Sentence 1 settles so late that its head is still returning when sentence 2 begins.
    # Sentence 2's own first command comes 200 ms after its voice and waits for that settle,
    # which runs to about 300 ms, so its head moves 330 ms after its voice.
    late_first = lambda i: 0.2 if i == 1 else -0.09          # noqa: E731
    synth = Synth(n=3, apply0=late_first, move_start=lambda i: 0.33 if i == 1 else -0.03,
                  settle_gap=lambda i: 1.45 if i == 0 else 0.05)
    summary, _, _ = report(tmp_path, synth)
    r = row(summary, 2)
    assert "older motion" in r["move_note"]
    assert 330 <= r["lag_ms"] <= 390
    calm = Synth(n=3, name="calm", apply0=late_first,
                 move_start=lambda i: 0.33 if i == 1 else -0.03)
    summary, _, _ = report(tmp_path, calm)
    assert row(summary, 2)["move_note"] == ""
    assert 330 <= row(summary, 2)["lag_ms"] <= 390


def test_motion_still_in_flight_cannot_hide_a_late_first_move(tmp_path):
    # Each sentence speaks straight after the last, and the last one's settle goes out before
    # its voice ends, so that settle is still in the queue at this one's first command, which
    # itself goes out before the voice. The head then starts 430 ms after the voice.
    late = Synth(n=6, spacing=SPEECH_S, settle_gap=lambda i: -0.2, durations=(0.2, 0.2, 0.2),
                 move_start=lambda i: 0.43 if i else -0.03)
    summary = report(tmp_path, late)[0]
    for seq in range(2, 7):
        r = row(summary, seq)
        assert r["overlapped"] and "queued before" in r["move_note"]
        assert 430 <= r["lag_ms"] <= 500, (seq, r["lag_ms"])


def test_a_first_move_behind_a_queue_that_outlasts_the_sentence_is_late_not_missing(tmp_path):
    long_move = Synth(n=3, spacing=SPEECH_S, durations=(0.5, 0.5, 5.0))
    summary, _, _ = report(tmp_path, long_move)
    r = row(summary, 2)
    assert r["move_status"] == "behind older motion for the whole window"
    assert r["lag_ms"] > 1000.0 and not math.isinf(r["lag_ms"])


def test_a_recorded_move_cut_short_frees_the_queue(tmp_path):
    synth = Synth(n=3, spacing=SPEECH_S, durations=(5.0, 0.0, 0.0))
    synth.manifest["sentences"][0]["expected_exception"] = None
    for rec in synth.records("apply", 1):
        rec["kind"] = "play_recorded"
        rec["accepted"] = True
    synth.trace.append({"s": "recorded_cut", "t": synth.tv(0) + 0.5, "plan": 1, "by": "voice_end",
                        "waited": 0.5})
    summary, _, _ = report(tmp_path, synth)
    assert not row(summary, 2)["overlapped"]


def test_motion_that_finished_in_time_is_not_overlapped(tmp_path):
    summary, _, _ = report(tmp_path, Synth())
    assert not any(r["overlapped"] for r in summary["rows"])


def test_joints_that_do_not_cover_the_voice_are_a_data_problem(tmp_path):
    synth = Synth()
    synth.joints = [j for j in synth.joints if j["t"] < synth.tv(20)]
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "DATA") == M.FAIL
    assert any("do not cover" in line for line in gate(summary, "DATA")["detail"])
    assert row(summary, 30)["lag_ms"] is None


# ---------------------------------------------------------------------------
# rotation arithmetic
# ---------------------------------------------------------------------------
def test_head_rotation_ignores_angle_wrapping():
    # 3.1 and -3.1 rad of yaw are 0.083 rad apart across the wrap.
    wrapped = math.degrees(2 * math.pi - 6.2)
    assert M.head_angle_deg((0, 0, 3.1), (0, 0, -3.1)) == pytest.approx(wrapped, abs=1e-6)
    assert M.head_angle_deg((0.1, 0.2, 0.3), (0.1, 0.2, 0.3)) == pytest.approx(0.0, abs=1e-6)
    assert M.head_angle_deg((0, 0, 0), (0, math.radians(1.0), 0)) == pytest.approx(1.0, abs=1e-6)
    assert M.antenna_angle_deg((3.1, 0), (-3.1, 0)) == pytest.approx(wrapped, abs=1e-6)


def test_the_head_threshold_is_one_degree_and_the_antenna_threshold_three():
    pitch = [0.0, 0.0, 0.0, 0.9, 0.9, 1.1, 1.1, 1.1, 1.1, 1.1]
    samples = [(t / 50.0, (0.0, math.radians(pitch[t]), 0.0), (0.0, 0.0)) for t in range(10)]
    dep = M.Joints(samples).departure(0.0, 0.2)
    assert dep.head == pytest.approx(5 / 50.0) and dep.antenna is None
    swing = [0.0, 0.0, 2.9, 2.9, 3.1, 3.1, 3.1, 3.1, 3.1, 3.1]
    samples = [(t / 50.0, (0.0, 0.0, 0.0), (math.radians(swing[t]), 0.0)) for t in range(10)]
    dep = M.Joints(samples).departure(0.0, 0.2)
    assert dep.antenna == pytest.approx(4 / 50.0) and dep.head is None


def test_percentile_and_stats_handle_a_sentence_that_never_moved():
    assert M.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    st = M.stats([10.0, 20.0, math.inf])
    assert st["max"] == math.inf and st["no_motion"] == 1 and st["median"] == 20.0
    assert M.stats([])["n"] == 0


# ---------------------------------------------------------------------------
# SG-3, SG-4, SG-12
# ---------------------------------------------------------------------------
def test_old_motion_commanded_after_the_new_voice_fails_sg3_and_sg10(tmp_path):
    synth = Synth()
    for i in range(synth.n - 1):
        synth.trace.append({"s": "apply", "t": synth.tv(i + 1) + 0.2, "plan": i + 1, "i": 3,
                            "kind": "goto", "reason": "speaking (sustain)", "d": 0.2, "ms": 9.0})
        synth.trace.append({"s": "post", "t": synth.tv(i + 1) + 0.204, "path": "/x", "status": 200})
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-3") == M.FAIL
    assert status(summary, "SG-10") == M.FAIL
    assert row(summary, 6)["tail_ms"] == pytest.approx(200.0, abs=1.0)
    assert row(summary, 1)["tail_ms"] is None        # nothing came before the first sentence


def test_a_late_settle_inside_a_reply_fails_sg4a_only(tmp_path):
    synth = Synth(settle_gap=lambda i: 0.05 if (i + 1) % 4 == 0 else 0.7)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-4a") == M.FAIL
    assert status(summary, "SG-4b") == M.PASS
    assert row(summary, 1)["settle_class"] == "4a" and row(summary, 4)["settle_class"] == "4b"


def test_a_late_settle_at_the_end_of_a_reply_fails_sg4b_only(tmp_path):
    synth = Synth(settle_gap=lambda i: 0.4 if (i + 1) % 4 == 0 else 0.05)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-4b") == M.FAIL
    assert status(summary, "SG-4a") == M.PASS


def test_the_arbiter_trace_makes_the_queue_gate_measurable(tmp_path):
    summary, _, _ = report(tmp_path, Synth(arb=True))
    assert status(summary, "SG-6") == M.PASS
    assert "most queued during speech 1" in " ".join(gate(summary, "SG-6")["detail"])


def test_a_queue_that_grows_during_speech_fails(tmp_path):
    synth = Synth(arb=True)
    records = synth.arb_records(queued=3)
    synth.arb_records = lambda: records
    summary, code, _ = report(tmp_path, synth)
    assert status(summary, "SG-6") == M.FAIL and code == 1


def test_with_an_arbiter_trace_the_reference_is_when_the_move_really_started(tmp_path):
    # The first command goes out before the voice and waits 350 ms for an older move to end.
    late = Synth(n=6, spacing=SPEECH_S, settle_gap=lambda i: -0.2, durations=(0.2, 0.2, 0.2),
                 move_start=lambda i: 0.3 if i else -0.03, arb=True)
    records = late.arb_records()
    for rec in records:
        if rec["event"] == "started" and rec["reason"].endswith(".0") \
                and not rec["reason"].endswith(":1.0"):
            rec["t"] += 0.35
    late.arb_records = lambda: records
    summary = report(tmp_path, late)[0]
    r = row(summary, 3)
    assert r["overlapped"] and 300 <= r["lag_ms"] <= 420, r["lag_ms"]
    assert r["segments"]["accepted_to_started"] == pytest.approx(360, abs=2)


def test_a_plan_whose_moves_never_started_never_moved(tmp_path):
    # The joints show motion, and the arbiter says none of this sentence's moves ran.
    synth = Synth(n=6, arb=True)
    records = [r for r in synth.arb_records()
               if not (r["event"] == "started" and r["reason"].split(":")[-1].startswith("3."))]
    synth.arb_records = lambda: records
    summary = report(tmp_path, synth)[0]
    r = row(summary, 3)
    assert r["move_status"] == "no move started" and math.isinf(r["lag_ms"])


def test_with_no_wait_a_trace_does_not_mark_older_motion(tmp_path):
    summary = report(tmp_path, Synth(arb=True))[0]
    assert not any(r["overlapped"] for r in summary["rows"])


def test_old_motion_after_the_new_voice_reads_from_when_it_really_ended(tmp_path):
    synth = Synth(n=4, arb=True)
    records = synth.arb_records()
    for rec in records:                # the first sentence's settle runs on into the second
        if rec["event"] == "settled" and rec["reason"].endswith(":1.3"):
            rec["t"] = synth.tv(1) + 0.2
    synth.arb_records = lambda: records
    summary = report(tmp_path, synth)[0]
    assert row(summary, 2)["tail_ms"] == pytest.approx(200, abs=5)


def _with_pause(synth, i, length):
    # A silence inside sentence i, starting 700 ms into its voice.
    start = synth.tv(i) + 0.7 - synth.t_base
    synth.manifest["sentences"][i]["pauses"] = [[start, start + length]]
    return synth


def test_a_settle_inside_a_long_pause_feeds_sg4a(tmp_path):
    # The settle goes out 250 ms into the pause, and the head leaves about 50 ms later.
    synth = _with_pause(Synth(settle_gap=lambda i: -0.55 if i == 2 else 0.05), 2, 0.7)
    summary, _, _ = report(tmp_path, synth)
    assert 0.28 <= row(summary, 3)["pause_settle_s"] <= 0.36
    assert any("n=" in line for line in gate(summary, "SG-4a")["detail"])


def test_a_long_pause_with_no_settle_counts_as_the_whole_pause(tmp_path):
    summary, _, _ = report(tmp_path, _with_pause(Synth(), 2, 0.7))
    assert row(summary, 3)["pause_settle_s"] == pytest.approx(0.7)


def test_a_late_settle_in_a_long_pause_fails_sg4a(tmp_path):
    synth = Synth()
    for i in range(4):
        _with_pause(synth, i * 3, 1.0)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-4a") == M.FAIL


def test_a_short_pause_asks_for_no_settle(tmp_path):
    summary, _, _ = report(tmp_path, _with_pause(Synth(), 2, 0.4))
    assert row(summary, 3)["pause_settle_s"] is None


def _without_settle(synth, plans):
    synth.trace = [r for r in synth.trace
                   if not (r["s"] == "apply" and r.get("i") == 3 and r.get("plan") in plans)]
    return synth


def test_a_head_left_away_with_no_settle_command_fails(tmp_path):
    synth = _without_settle(Synth(), {1})
    summary, code, _ = report(tmp_path, synth)
    assert math.isinf(row(summary, 1)["settle_s"])
    assert status(summary, "SG-4a") == M.FAIL and code == 1


def test_a_settle_commanded_but_never_seen_fails(tmp_path):
    synth = Synth(move_start=lambda i: 0.05)
    # The head goes up and stays up: the command went out and nothing came of it.
    synth.ramps[0] = (synth.ramps[0][0], synth.tv(1) + 0.5)
    synth.joints = [synth._joint(r["t"]) for r in synth.joints]
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 1)["settle_note"].startswith("settle not visible")
    assert math.isinf(row(summary, 1)["settle_s"])


def test_a_head_already_at_rest_owes_no_settle(tmp_path):
    synth = _without_settle(Synth(move_start=lambda i: None if i == 0 else -0.03), {1})
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 1)["settle_s"] is None


def test_a_next_sentence_straight_after_owes_no_settle(tmp_path):
    synth = _without_settle(Synth(n=4, spacing=SPEECH_S + 0.2), {1})
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 1)["settle_s"] is None


def test_m1_uses_the_interim_settle_limit(tmp_path):
    synth = Synth(settle_gap=lambda i: 0.5)          # about 0.55 s: late for final, fine interim
    summary, _, _ = report(tmp_path, synth, mode="final")
    assert status(summary, "SG-4a") == M.FAIL
    summary, _, _ = report(tmp_path / "m1", synth, mode="m1")
    assert status(summary, "SG-4a") == M.PASS


def test_thin_family_coverage_fails_sg12(tmp_path):
    summary, _, _ = report(tmp_path, Synth(durations=(0.2, 0.2, 0.2)))
    assert status(summary, "SG-12") == M.FAIL
    assert row(summary, 1)["coverage"] < 0.9


# ---------------------------------------------------------------------------
# SG-7: identity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", ["pickup", "batch_end", "apply"])
def test_a_missing_link_fails_sg7(tmp_path, stream):
    synth = Synth()
    synth.drop(stream, 3)
    summary, code, _ = report(tmp_path, synth)
    assert status(summary, "SG-7") == M.FAIL
    assert any("seq 3" in line for line in gate(summary, "SG-7")["detail"])
    assert code == 1


def test_a_manifest_sentence_the_driver_never_saw_fails_sg7(tmp_path):
    synth = Synth()
    synth.trace = [r for r in synth.trace if not (r["s"] == "sentence" and r["seq"] == 9)]
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-7") == M.FAIL
    assert row(summary, 9)["outcome"] == "missing"


def test_a_sentence_in_the_trace_twice_is_not_matched_silently(tmp_path):
    synth = Synth()
    synth.trace.append(dict(next(r for r in synth.trace if r["s"] == "sentence")))
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-7") == M.FAIL


def test_a_deduplicated_spoken_sentence_fails_sg7(tmp_path):
    synth = Synth()
    for r in synth.trace:
        if r["s"] == "sentence" and r["seq"] == 2:
            r.update(plan=0, deduped=True)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-7") == M.FAIL
    assert row(summary, 2)["outcome"] == "deduped"


def test_the_trace_must_describe_the_sentence_the_manifest_named(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "sentence" and r["seq"] == 4)["chars"] = 99
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-7") == M.FAIL


# ---------------------------------------------------------------------------
# SG-8: the plan
# ---------------------------------------------------------------------------
def test_intents_out_of_order_fail_sg8(tmp_path):
    synth = Synth()
    applies = synth.records("apply", 5)
    applies[1]["i"], applies[2]["i"] = 2, 1
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL
    assert "ordered prefix" in " ".join(gate(summary, "SG-8")["detail"])


def test_a_skipped_intent_fails_sg8(tmp_path):
    synth = Synth()
    synth.trace = [r for r in synth.trace
                   if not (r["s"] == "apply" and r["plan"] == 5 and r["i"] == 1)]
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL


def test_a_plan_whose_batch_has_a_different_count_fails_sg8(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "sentence" and r["seq"] == 6)["intents"] = 5
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL


def test_a_refused_move_is_not_an_exception(tmp_path):
    synth = Synth()
    post = next(r for r in synth.trace if r["s"] == "post" and r["t"] > synth.tv(6))
    post["status"] = 503
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL
    assert "answered 503" in " ".join(gate(summary, "SG-8")["detail"])
    synth = Synth(name="b")
    next(r for r in synth.trace if r["s"] == "post" and r["t"] > synth.tv(6))["status"] = 0
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL


def test_a_refused_recorded_move_fails_sg8(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "apply" and r.get("move")).update(accepted=False)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL
    assert "recorded move refused" in " ".join(gate(summary, "SG-8")["detail"])


def _preempt(synth: Synth, seq: int) -> None:
    """Sentence ``seq`` was cut after its second command by the next sentence."""

    plan = seq
    synth.trace = [r for r in synth.trace if not (
        r["s"] in ("apply", "post") and r.get("plan", plan) == plan
        and r["t"] > synth.tv(seq - 1) + 0.6)]
    end = next(r for r in synth.trace if r["s"] == "batch_end" and r["plan"] == plan)
    end.update(revoked=True, i=2)


def test_a_preempted_sentence_has_a_named_outcome_and_a_clean_prefix(tmp_path):
    synth = Synth()
    _preempt(synth, 6)
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 6)["outcome"] == "preempted"
    assert status(summary, "SG-8") == M.PASS


def test_a_preemption_nothing_explains_fails_sg8(tmp_path):
    synth = Synth(n=3)
    _preempt(synth, 3)
    synth.trace = [r for r in synth.trace if r["s"] != "tts_ended"]
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 3)["outcome"] == "preempted_unexplained"
    assert status(summary, "SG-8") == M.FAIL


def test_the_scripts_own_exclusions_do_not_count_as_hidden_faults(tmp_path):
    synth = Synth(n=40)
    for k in (1, 2, 3):            # 7.5%: over the 5% for what happened, under the 15% for design
        synth.manifest["sentences"][k]["expected_exception"] = "E5"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.PASS


def test_a_script_can_only_excuse_so_much_of_itself(tmp_path):
    synth = Synth(n=40)
    for k in range(7):             # 17.5%
        synth.manifest["sentences"][k]["expected_exception"] = "E5"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.FAIL


def test_a_voiceless_sentence_may_only_settle(tmp_path):
    synth = Synth()
    synth.trace = [r for r in synth.trace if not (
        r["s"] == "voice_seen" and r["plan"] == 5)
        and not (r["s"] in ("apply", "post") and r.get("plan", 5) == 5
                 and r.get("i", 3) < 3 and r["t"] < synth.tv(4) + 1.4)]
    next(r for r in synth.trace if r["s"] == "batch_end" and r["plan"] == 5)["voice_seen"] = False
    summary, _, _ = report(tmp_path, synth)
    assert row(summary, 5)["outcome"] == "no_voice"
    assert status(summary, "SG-8") == M.PASS             # a named shortfall
    assert status(summary, "SG-1") == M.FAIL             # but the sentence did not move


def test_motion_for_a_sentence_the_driver_says_had_no_voice_fails_sg8(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "batch_end" and r["plan"] == 5)["voice_seen"] = False
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-8") == M.FAIL


# ---------------------------------------------------------------------------
# SG-9: family
# ---------------------------------------------------------------------------
def test_a_published_family_that_differs_from_the_performed_one_fails_sg9(tmp_path):
    synth = Synth()
    synth.published[2]["cue_family"] = "emphasis"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL
    assert "published family emphasis" in " ".join(gate(summary, "SG-9")["detail"])


def test_a_published_category_that_differs_fails_sg9(tmp_path):
    synth = Synth()
    synth.published[4]["cue_category"] = "emphasis"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL


def test_the_expected_family_is_checked_when_given(tmp_path):
    synth = Synth()
    synth.manifest["sentences"][2]["expect_family"] = "emphasis"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL
    synth = Synth(name="b")
    synth.manifest["sentences"][2]["expect_family"] = None   # not given: only the chain is checked
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.PASS


def test_a_sentence_that_was_never_published_fails_sg9(tmp_path):
    synth = Synth()
    del synth.published[7]
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL
    assert "nothing was published" in " ".join(gate(summary, "SG-9")["detail"])


def test_an_authored_family_that_changed_on_the_way_in_fails_sg9(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "sentence" and r["seq"] == 1)["authored"] = "emphasis"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL


# ---------------------------------------------------------------------------
# SG-10, SG-5, SG-6, SG-11, SG-13
# ---------------------------------------------------------------------------
def test_a_move_outside_its_own_sentence_fails_sg10_but_only_where_scripted(tmp_path):
    synth = Synth(move_start=lambda i: 0.45 if i == 3 else -0.03, apply0=0.4)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-10") == M.FAIL
    real = Synth(n=30, name="live", scripted=False, k_ms=120.0, source="ros",
                 move_start=lambda i: 0.45 if i == 3 else -0.03)
    summary, _, _ = report(tmp_path, real)
    assert status(summary, "SG-10") == M.INFO


def test_the_arbiter_queue_is_a_proxy_that_can_fail_but_never_pass(tmp_path):
    summary, _, _ = report(tmp_path, Synth(with_waiting=0))
    assert status(summary, "SG-6") == M.NOT_MEASURABLE
    assert "proxy" in " ".join(gate(summary, "SG-6")["detail"])
    summary, _, _ = report(tmp_path / "w", Synth(with_waiting=2))
    assert status(summary, "SG-6") == M.FAIL


def test_overhead_checks_the_trace_size_and_needs_a_baseline_for_cpu(tmp_path):
    synth = Synth()
    for k in range(5000):
        synth.trace.append({"s": "engagement", "t": T0 + 5 + k * 1e-4, "action": "submitted"})
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-13") == M.FAIL

    first, _, _ = report(tmp_path / "a", Synth())
    assert status(first, "SG-13") == M.NOT_MEASURABLE
    baseline = M._clean(first)
    summary, _, _ = report(tmp_path / "b", Synth(), baseline=baseline)
    assert status(summary, "SG-13") == M.PASS
    heavier = Synth(name="heavy")
    for rec in heavier.load:
        rec["pipewire_cpu"] = 7.0
    summary, _, _ = report(tmp_path / "c", heavier, baseline=baseline)
    assert status(summary, "SG-13") == M.FAIL


def test_voice_safety_and_recorded_starts_are_never_passed_from_a_run(tmp_path):
    summary, _, _ = report(tmp_path, Synth())
    assert status(summary, "SG-11") == status(summary, "SG-5") == M.NOT_MEASURABLE
    assert any("information:" in line for line in gate(summary, "SG-5")["detail"])


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------
def test_exceptions_beyond_five_percent_fail_the_run_unless_they_are_e1(tmp_path):
    synth = Synth(silent=(3, 11))                                # E6, 2 of 40 = 5%
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.PASS
    assert {r["seq"] for r in summary["rows"] if r["exception"] == "E6"} == {4, 12}

    synth = Synth(name="b", silent=(3, 11, 19))                  # 3 of 40 = 7.5%
    summary, code, text = report(tmp_path, synth)
    assert status(summary, "EXC") == M.FAIL and code == 1
    assert "E6" in text and "no audio" in text                    # every exclusion is listed

    synth = Synth(name="c")
    for k in range(10):
        synth.manifest["sentences"][k]["expected_exception"] = "E1"
        synth.published[k]["cue_family"] = synth.published[k]["cue_category"] = None
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.PASS


def test_an_exception_still_has_to_hold(tmp_path):
    synth = Synth()
    synth.manifest["sentences"][2]["expected_exception"] = "E1"   # E1 says nothing is published
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.FAIL
    assert "E1: a family was published" in " ".join(gate(summary, "EXC")["detail"])

    synth = Synth(name="b")
    synth.manifest["sentences"][2]["expected_exception"] = "E4"   # but the plan completed
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.FAIL


def test_an_excepted_sentence_leaves_the_timing_gates_and_a_preemption_is_not_a_fault(tmp_path):
    synth = Synth()
    _preempt(synth, 6)
    synth.manifest["sentences"][5]["expected_exception"] = "E4"
    synth.published[5]["cue_family"] = synth.published[5]["cue_category"] = None
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.PASS
    assert status(summary, "SG-8") == M.PASS
    assert row(summary, 6)["exception"] == "E4"


def test_a_run_without_the_voice_tap_is_a_degraded_exception(tmp_path):
    synth = Synth()
    next(r for r in synth.trace if r["s"] == "pickup" and r["plan"] == 3)["voice"] = False
    summary, _, text = report(tmp_path, synth)
    assert row(summary, 3)["exception"] == "E7"
    assert status(summary, "EXC") == M.FAIL            # one in forty is more than 1%
    assert "without the voice tap" in text
    big = Synth(n=150, name="big")
    next(r for r in big.trace if r["s"] == "pickup" and r["plan"] == 3)["voice"] = False
    summary, _, text = report(tmp_path, big)
    assert status(summary, "EXC") == M.PASS            # a warning, under 1%
    assert "without the voice tap" in text


# ---------------------------------------------------------------------------
# data completeness
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["joints.jsonl", "moves.jsonl", "truth.json",
                                  "published.jsonl", "load.jsonl", "manifest.json",
                                  "trace.jsonl"])
def test_a_missing_file_fails_data_completeness(tmp_path, name):
    path = Synth().write(tmp_path)
    (path / name).unlink()
    summary, code, _ = report(tmp_path, path, mode="m0")
    assert status(summary, "DATA") == M.FAIL
    assert code == 1
    assert any(name in line for line in gate(summary, "DATA")["detail"])


def test_an_empty_stream_fails_data_completeness(tmp_path):
    path = Synth().write(tmp_path)
    (path / "joints.jsonl").write_text("", encoding="utf-8")
    summary, _, _ = report(tmp_path, path, mode="m0")
    assert status(summary, "DATA") == M.FAIL
    assert any("joints.jsonl is empty" in line for line in gate(summary, "DATA")["detail"])


def test_a_truncated_trace_fails_and_costs_the_sentences_it_lost(tmp_path):
    path = Synth().write(tmp_path)
    text = (path / "trace.jsonl").read_text(encoding="utf-8")
    cut = text[: int(len(text) * 0.6)]
    (path / "trace.jsonl").write_text(cut, encoding="utf-8")        # ends mid-record
    summary, code, _ = report(tmp_path, path, mode="m0")
    assert status(summary, "DATA") == M.FAIL and code == 1
    assert "unreadable" in " ".join(gate(summary, "DATA")["detail"])
    full, _, _ = report(tmp_path / "x", path, mode="final")
    assert status(full, "SG-7") == M.FAIL                            # the lost sentences show


def test_a_trace_with_no_meta_or_the_wrong_schema_is_refused(tmp_path):
    synth = Synth()
    synth.trace = synth.trace[1:]
    summary, _, _ = report(tmp_path, synth, mode="m0")
    assert status(summary, "DATA") == M.FAIL
    synth = Synth(name="b")
    synth.trace[0]["schema"] = 9
    summary, _, _ = report(tmp_path, synth, mode="m0")
    assert status(summary, "DATA") == M.FAIL


def test_a_truth_file_that_lacks_a_sentence_fails_data_completeness(tmp_path):
    synth = Synth()
    del synth.truth["sentences"][5]
    summary, _, _ = report(tmp_path, synth, mode="m0")
    assert status(summary, "DATA") == M.FAIL
    assert any("seq 6" in line for line in gate(summary, "DATA")["detail"])


def test_a_directory_that_is_not_a_run_is_reported_and_not_skipped(tmp_path):
    summary, code, _ = report(tmp_path, tmp_path / "nowhere", mode="m0")
    assert status(summary, "DATA") == M.FAIL and code == 1


# ---------------------------------------------------------------------------
# real sessions
# ---------------------------------------------------------------------------
def test_a_real_session_takes_t_voice_from_the_tap_and_k(tmp_path):
    real = Synth(n=30, name="live", scripted=False, k_ms=120.0, source="ros")
    summary, _, _ = report(tmp_path, real)
    r = row(summary, 4)
    assert r["voice_src"] == "tap"
    assert r["t_voice"] == pytest.approx(real.tv(3), abs=1e-3)
    assert r["t_end"] == pytest.approx(real.tv(3) + SPEECH_S, abs=1e-3)
    assert -10 <= r["lag_ms"] <= 30
    assert status(summary, "SAMPLE") == M.PASS            # 30 real sentences
    assert status(summary, "DATA") == M.PASS


def test_a_real_session_without_joints_reports_the_lag_as_not_measurable(tmp_path):
    real = Synth(n=30, name="live", scripted=False, k_ms=120.0, source="ros")
    path = real.write(tmp_path)
    (path / "joints.jsonl").unlink()
    (path / "load.jsonl").unlink()
    summary, _, _ = report(tmp_path, path)
    assert status(summary, "DATA") == M.PASS
    assert status(summary, "SG-1") == M.NOT_MEASURABLE
    assert status(summary, "SG-3") == M.PASS               # needs only the voice times


def test_an_unknown_k_is_said_so_and_taken_as_zero(tmp_path):
    real = Synth(n=30, name="live", scripted=False, k_ms=None, source="ros")
    real.manifest["k_ms"] = None
    _, _, text = report(tmp_path, real)
    assert "k_ms unknown" in text


# ---------------------------------------------------------------------------
# decomposition and roll-ups
# ---------------------------------------------------------------------------
def test_the_delay_decomposition_follows_the_chain(tmp_path):
    summary, _, text = report(tmp_path, Synth())
    seg = row(summary, 3)["segments"]
    assert seg["event_to_pickup"] == pytest.approx(5.0, abs=0.5)
    assert seg["voice_seen_to_first_command"] == pytest.approx(10.0, abs=0.5)
    assert seg["command_duration"] == 12.0
    assert seg["voice_to_voice_seen"] == pytest.approx(-100.0, abs=0.5)
    block = summary["rollups"]["scripted"]
    assert block["decomposition"]["event to pickup"]["n"] == 40
    assert block["bind_kinds"] == {"onset": 40}
    assert "first command to t_move" in text


def test_roll_ups_split_by_source_plan_kind_and_load_quartile(tmp_path):
    summary, _, _ = report(tmp_path, Synth())
    lag = summary["rollups"]["scripted"]["metrics"]["lag, any motor (ms)"]
    assert set(lag["source"]) == {"http"}
    assert {"recorded", "family", "talking"} <= set(lag["plan kind"])
    assert set(lag["load quartile"]) == {"Q1", "Q2", "Q3", "Q4"}
    assert lag["all"]["n"] == 40


# ---------------------------------------------------------------------------
# comparing with a baseline
# ---------------------------------------------------------------------------
def _baseline(tmp_path, synth=None):
    summary, _, _ = report(tmp_path / "base", synth or Synth())
    return json.loads(json.dumps(M._clean(summary), allow_nan=False))


def _findings(summary):
    return {(f["rule"], f["status"]) for f in summary["against"]}


def test_a_run_compared_with_itself_matches(tmp_path):
    summary, code, text = report(tmp_path / "now", Synth(), mode="m0", baseline=_baseline(tmp_path))
    assert {s for _, s in _findings(summary)} == {M.PASS}
    assert code == 0 and "Against the baseline" in text


def test_a_changed_plan_structure_is_a_defect(tmp_path):
    base = _baseline(tmp_path)
    other = Synth()
    second = next(r for r in other.trace
                  if r["s"] == "apply" and r["plan"] == 1 and r["i"] == 1)
    second["kind"] = "antennas"
    summary, code, _ = report(tmp_path / "now", other, mode="m0", baseline=base)
    assert ("structure", M.FAIL) in _findings(summary) and code == 1


def test_a_changed_family_is_a_structure_defect(tmp_path):
    base = _baseline(tmp_path)
    other = Synth()
    for r in other.trace:
        if r["s"] == "sentence" and r["seq"] == 1:
            r["performed"] = "emphasis"
    summary, code, _ = report(tmp_path / "now", other, mode="m0", baseline=base)
    assert ("structure", M.FAIL) in _findings(summary) and code == 1


def test_a_chain_shift_beyond_half_the_tolerance_is_a_defect_even_when_the_gates_hold(tmp_path):
    base = _baseline(tmp_path)
    slower = Synth(move_start=lambda i: -0.03 + 0.04, apply0=-0.05)      # chain +40 ms
    summary, code, _ = report(tmp_path / "now", slower, mode="m0", baseline=base)
    assert ("relative", M.FAIL) in _findings(summary)
    assert status(summary, "SG-1") == M.PASS and code == 1
    near = Synth(move_start=lambda i: -0.03 + 0.02)                      # +20 ms, inside T/2
    summary, code, _ = report(tmp_path / "ok", near, mode="m0", baseline=base)
    assert {s for _, s in _findings(summary)} == {M.PASS} and code == 0


def test_a_shift_for_the_better_stops_until_explained(tmp_path):
    base = _baseline(tmp_path, Synth(move_start=lambda i: 0.0, apply0=-0.05))
    summary, code, _ = report(tmp_path / "now", Synth(move_start=lambda i: -0.07), mode="m0",
                              baseline=base)
    assert ("improvement", M.FAIL) in _findings(summary) and code == 1


def test_the_absolute_rule_reports_the_selected_gates_failing(tmp_path):
    base = _baseline(tmp_path)
    late = Synth(move_start=lambda i: 0.07, apply0=0.0)
    summary, code, _ = report(tmp_path / "now", late, mode="final", baseline=base)
    assert ("absolute", M.FAIL) in _findings(summary) and code == 1


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def test_the_command_line_prints_a_table_writes_json_and_returns_the_exit_status(
        tmp_path, capsys):
    path = Synth().write(tmp_path)
    out = tmp_path / "out" / "report.json"
    code = M.main([str(path), "--gates", "m0", "--json", str(out)])
    text = capsys.readouterr().out
    assert code == 0
    assert "Sentences (" in text and "Gates" in text and "PASS  DATA" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and len(data["rows"]) == 40 and data["exit"] == 0

    code = M.main([str(path), "--gates", "m0", "--no-rows", "--against", str(out)])
    text = capsys.readouterr().out
    assert code == 0 and "Sentences (" not in text and "Against the baseline" in text


def test_a_baseline_that_is_not_a_report_is_an_error(tmp_path, capsys):
    path = Synth().write(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert M.main([str(path), "--against", str(bad)]) == 1
    assert "not a report" in capsys.readouterr().err


def test_a_sentence_cut_short_without_a_named_cause_fails_sg8_unless_it_is_e5(tmp_path):
    def cut(synth: Synth) -> Synth:
        synth.trace = [r for r in synth.trace if not (
            r["s"] in ("apply", "post") and r.get("plan", 7) == 7
            and r["t"] > synth.tv(6) + 1.4)]
        return synth

    summary, _, _ = report(tmp_path, cut(Synth()))
    assert row(summary, 7)["outcome"] == "stopped"
    assert status(summary, "SG-8") == M.FAIL

    shorter = cut(Synth(name="b"))
    shorter.manifest["sentences"][6]["expected_exception"] = "E5"
    summary, _, _ = report(tmp_path, shorter)
    assert status(summary, "SG-8") == M.PASS
    assert row(summary, 7)["exception"] == "E5"


def test_a_safety_move_in_flight_leaves_the_timing_gates_but_not_the_plan_check(tmp_path):
    synth = Synth(move_start=lambda i: 0.4 if i == 3 else -0.03,
                  apply0=lambda i: 0.3 if i == 3 else -0.09)
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-1") == M.FAIL
    synth.manifest["sentences"][3]["expected_exception"] = "E3"
    summary, _, _ = report(tmp_path / "e3", synth)
    assert status(summary, "SG-1") == M.PASS
    assert status(summary, "SG-8") == M.PASS


def test_a_cut_plan_may_publish_the_mapped_family_but_not_a_different_one(tmp_path):
    synth = Synth()
    synth.manifest["sentences"][2]["expected_exception"] = "E2"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "EXC") == M.PASS and status(summary, "SG-9") == M.PASS
    synth = Synth(name="b")
    synth.manifest["sentences"][2]["expected_exception"] = "E2"
    synth.published[2]["cue_family"] = synth.published[2]["cue_category"] = "emphasis"
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-9") == M.FAIL


def test_the_offset_can_be_read_differently_from_the_command_line(tmp_path):
    path = Synth(k_ms=0.0).write(tmp_path)
    plain, _, _ = M.build_report([path], "final")
    moved, _, text = M.build_report([path], "final", k_ms=100.0)
    assert row(moved, 3)["lag_ms"] == pytest.approx(row(plain, 3)["lag_ms"] - 100.0, abs=1.0)
    assert "k_ms 100 was asked for" in text


def test_a_trace_that_grows_too_fast_fails_the_overhead_gate(tmp_path):
    synth = Synth(n=6)
    for k in range(4000):
        synth.trace.append({"s": "voice", "t": synth.tv(0), "edge": "onset", "kind": "dip",
                            "pad": "x" * 60})
    summary, _, _ = report(tmp_path, synth)
    assert status(summary, "SG-13") == M.FAIL
    assert "KB a minute" in " ".join(gate(summary, "SG-13")["detail"])


def test_a_pose_still_slowing_from_a_cut_move_is_not_this_sentences_first_motion(tmp_path):
    def run_with(name, *, cut):
        synth = Synth(n=3, name=name, arb=True)
        records = synth.arb_records()
        if cut:      # any move, with or without a plan, settled as cut when sentence 2 starts
            records.append({"seq": 9999, "t": synth.tv(1) - 0.08, "event": "settled",
                            "move_id": "other", "kind": "goto", "client": "hostapp",
                            "lane": "interaction", "reason": "rest:goto",
                            "error_code": "preempted", "queued": 0})
        synth.arb_records = lambda: records
        return row(report(tmp_path, synth)[0], 2)["lag_ms"]

    assert run_with("cut", cut=True) > run_with("plain", cut=False)
