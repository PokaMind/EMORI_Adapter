"""The scenarios behind ``golden_trace record``, and the parts they stand on.

Each scenario really runs: a hostapp in a process of its own, a fake voice, a stub broker, and
the harness on its HTTP routes acting as the page. They run side by side, once, and each test
then checks that its scenario did what it says it does, so a route rename or a changed timer
shows up here instead of as an empty comparison in a maintenance window.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import golden_trace  # noqa: E402
from golden import compare as C  # noqa: E402
from golden import record, scenarios, voice_track  # noqa: E402
from golden.fake_voice import FakeVoice  # noqa: E402
from golden.plan import split_plan, steps_from_calls  # noqa: E402
from golden.runs import load_run, read_jsonl  # noqa: E402
from golden.stub_orion import StubOrion  # noqa: E402

SIM_SCENARIOS = [scenario.name for scenario in scenarios.SCENARIOS]


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Every scenario, compressed, once. Name to its run, or to why it did not finish."""

    root = tmp_path_factory.mktemp("golden")

    def run(name: str):
        run_dir = root / name / "run-001"
        code = golden_trace._spawn_sim_run(name, run_dir, label="test", fast=True, lang="en")
        return name, code, run_dir

    with ThreadPoolExecutor(len(SIM_SCENARIOS)) as pool:
        done = list(pool.map(run, SIM_SCENARIOS))
    return {name: (code, run_dir) for name, code, run_dir in done}


def run_of(recorded, name: str):
    code, run_dir = recorded[name]
    assert code == 0, f"{name} did not finish"
    return load_run(run_dir)


def verbs(run) -> list:
    return [record_.get("cmd") for record_ in run.records("pending")]


def endings(run) -> list[tuple]:
    return [(row["status"], row["end_reason"]) for row in run.db["sessions"]]


def statuses(run, route: str) -> list[int]:
    return [r["status"] for r in run.records("http") if r["route"] == route]


def bodies(run, route: str) -> list:
    return [r.get("body") for r in run.records("http") if r["route"] == route]


def reasons(run) -> list[str]:
    return [step.reason for step in steps_from_calls(run.records("client"))]


def test_every_scenario_finishes_in_the_simulator(recorded):
    assert {name: code for name, (code, _) in recorded.items() if code} == {}


def test_full_session_shows_every_witness(recorded):
    run = run_of(recorded, "full_session")
    assert C.self_check(run) == []
    methods = {r["method"] for r in run.records("client")}
    assert {"play_recorded_move", "set_gaze", "apply"} <= methods
    assert sorted(run.html) == ["identify.html", "landing.html", "landing_after.html",
                                "manual.html", "session.html"]
    assert endings(run) == [("completed", None)]
    assert run.db["sessions"][0]["cycles"] >= 2
    assert run.recordings and run.email
    assert [r["method"] for r in run.records("fiware")][0] == "POST"
    assert [r["profile"] for r in run.records("mic")] == ["idle", "session", "idle"]
    types = [r["type"] for r in run.records("events")]
    assert "pokamind_ai_utterance" in types and types[-1] == "pokamind_session_completed"
    assert len(C._marks(run, "voice_onset")) == 4
    assert len(C._marks(run, "envelope_post")) == 2
    answer = bodies(run, "/api/cycle")[-1]
    assert answer["completed"] is True and answer["summary"]
    assert statuses(run, "/api/tts") == [200]


def test_nothing_secret_or_clock_shaped_reaches_a_run(recorded):
    run = run_of(recorded, "full_session")
    session_url = next(body["session_url"] for body in bodies(run, "/api/voice-select")
                       if isinstance(body, dict) and "session_url" in body)
    assert "<sid:" in session_url
    html = run.html["session.html"]
    for text in (html, json.dumps(run.db), json.dumps(run.records("events"))):
        assert "20261" not in text.replace("2026-", ""), "a minted session id survived the masks"
        assert "eyJ" not in text


def test_walkaway_asks_to_end_then_sends_the_page_home(recorded):
    run = run_of(recorded, "walkaway")
    assert verbs(run) == ["end", "home"]
    assert endings(run) == [("abandoned", "walkaway_ends")]
    assert "pending" in run.meta["untimed"]


def test_start_deadlines_end_each_way(recorded):
    run = run_of(recorded, "start_deadlines")
    assert verbs(run) == ["end", "home"]
    assert endings(run) == [("abandoned", "session_start_failed"),
                            ("abandoned", "session_never_started"),
                            ("abandoned", "session_start_failed")]
    assert [body["aborted"] for body in bodies(run, "/api/session-start-failed")] == [True, True]


def test_preempt_closes_the_first_and_keeps_the_second(recorded):
    run = run_of(recorded, "preempt")
    assert endings(run) == [("abandoned", "preempted"), ("completed", None)]


def test_a_late_completion_is_salvaged_once_then_ignored(recorded):
    run = run_of(recorded, "late_completion")
    assert verbs(run) == ["end", "home"]
    assert endings(run) == [("completed", "walkaway_ends")]
    salvaged, ignored = bodies(run, "/api/cycle")[-2:]
    assert salvaged.get("completed") is True
    assert ignored == {"ignored": "no_active_session", "ok": True}
    assert run.email


def test_reprompt_gestures_then_force_ends(recorded):
    run = run_of(recorded, "reprompt_motion")
    assert verbs(run) == ["end"]
    assert "watchdog: re-prompt" in reasons(run)
    assert endings(run) == [("completed", "watchdog_force_ends")]


def test_spoken_reprompt_asks_the_frame_first(recorded):
    run = run_of(recorded, "reprompt_spoken")
    assert verbs(run) == ["reprompt", "end"]
    assert statuses(run, "/api/emori-debug") == [200]
    assert "watchdog: re-prompt" in reasons(run)


def test_idle_ticks_and_listening_stays_still(recorded):
    run = run_of(recorded, "idle_and_listening")
    idle = split_plan(steps_from_calls(run.records("client"))).idle
    assert len(idle) >= 2 and {step.reason for step in idle} == {"idle: stay alive"}
    assert C.samples_for("idle_interval_s", run, {}, use_arbiter=False)[""]
    assert len(C._marks(run, "listening_start")) == 1
    assert C.check_listening_still(run) == []


def test_badges_each_refuse_their_own_way(recorded):
    run = run_of(recorded, "badges")
    claims = statuses(run, "/api/badge/claim")
    assert claims == [409, 202, 409, 409]
    assert bodies(run, "/api/badge/claim")[2] == {"error_code": "qr_already_claimed"}
    assert bodies(run, "/api/badge/claim")[3] == {"error_code": "training_unavailable"}
    assert statuses(run, "/session") == [200, 409], "a used login must not open again"


def test_language_change_reloads_speaks_and_stamps(recorded):
    run = run_of(recorded, "language_change")
    assert verbs(run) == ["reload", "reload"]
    assert [r["lang"] for r in run.records("speech")] == ["ca", "en"]
    assert run.db["sessions"][0]["lang"] == "ca"
    assert all(r["lang"] == "ca" for r in run.records("events")
               if r["type"] == "pokamind_ai_utterance")


def test_the_double_turn_toggles_with_the_toggle_on(recorded):
    run = run_of(recorded, "language_double_turn")
    assert verbs(run) == ["reload"]
    assert [r["lang"] for r in run.records("speech")] == ["ca"]
    by_name = scenarios.all_scenarios(golden_trace.ROOT)
    assert by_name["language_double_turn"].env["EMORI_LANG_TOGGLE"] == "1"
    assert by_name["language_change"].env["EMORI_LANG_TOGGLE"] == "0"


def test_speech_engines_answer_the_same_bytes_and_stand_down_in_session(recorded):
    run = run_of(recorded, "speech_engines")
    tts = [r for r in run.records("http") if r["route"] == "/api/tts"]
    assert [r["status"] for r in tts] == [200, 409, 200]
    assert tts[0]["sha256"] == tts[2]["sha256"] and tts[0]["bytes"] > 0
    assert len(statuses(run, "/api/stt")) == 2


def test_a_gesture_replay_needs_no_host_and_decides_both_directions(recorded):
    run = run_of(recorded, "gesture_replay")
    assert run.stream("client") is None
    assert {r["direction"] for r in run.records("offline")} == {"left", "right"}


def test_a_restart_closes_the_orphan(recorded):
    run = run_of(recorded, "host_restart")
    assert statuses(run, "/healthz") == [200]
    assert "landing_after_restart.html" in run.html
    assert run.db["sessions"][0]["status"] == "abandoned"
    assert run.db["sessions"][0]["end_reason"] is not None


def test_speech_plans_reach_every_path_the_voice_can_take(recorded):
    run = run_of(recorded, "speech_plans")
    steps = steps_from_calls(run.records("client"))
    plan = split_plan(steps)
    assert plan.sustain, "no sentence outlasted its plan"
    assert plan.linger_settles, "no pause was lingered over"
    assert any("sustain tension" in step.reason for step in plan.sustain), "no tinted sustain"
    assert sum(step.kind == "play_recorded" for step in steps) >= 3
    assert sum(step.kind == "stop_recorded" for step in steps) >= 1, "no recorded move was cut"
    assert any(step.voice_scaled for step in plan.plan), "no voice-scaled antennas"
    onset = C.samples_for("voice_onset_to_first_move_s", run, {}, use_arbiter=False)[""]
    assert onset and min(onset) < 0.5
    rate = C.samples_for("sustain_beats_per_voice_s", run, {}, use_arbiter=False)[""]
    assert rate and rate[0] > 0


def test_a_run_compares_the_same_as_itself(recorded):
    tolerances = C.load_tolerances([golden_trace.DEFAULT_TOLERANCE])
    for name in ("walkaway", "badges", "speech_plans", "gesture_replay"):
        run = run_of(recorded, name)
        report = C.compare_scenario(name, [run], [run], tolerances, min_runs=1)
        assert report.ok, (name, [str(v) for v in report.violations])


def test_self_check_proves_it_can_catch_what_it_claims(capsys):
    assert golden_trace.main(["self-check"]) == 0
    out = capsys.readouterr().out
    assert "NOT CAUGHT" not in out
    assert out.count(": caught") == 5


# --- the command line -----------------------------------------------------------------


def _hand_made_run(path: Path, cmd: str) -> None:
    path.mkdir(parents=True)
    (path / "client.jsonl").write_text("", encoding="utf-8")
    (path / "pending.jsonl").write_text(json.dumps({"t": 1.0, "cmd": cmd}) + "\n", encoding="utf-8")
    (path / "meta.json").write_text(json.dumps({"mode": "sim", "t0": 0.0}), encoding="utf-8")


def test_compare_exits_non_zero_and_prints_the_first_divergence_per_stream(tmp_path, capsys):
    for index in (1, 2, 3):
        _hand_made_run(tmp_path / "a" / "s" / f"run-{index:03d}", "end")
        _hand_made_run(tmp_path / "b" / "s" / f"run-{index:03d}", "end")
        _hand_made_run(tmp_path / "c" / "s" / f"run-{index:03d}", "home")
    root = ["--root", str(tmp_path)]
    assert golden_trace.main([*root, "compare", "--a", "a", "--b", "b"]) == 0
    assert "s: same" in capsys.readouterr().out
    assert golden_trace.main([*root, "compare", "--a", "a", "--b", "c"]) == 1
    out = capsys.readouterr().out
    assert "pending" in out and "end" in out and "home" in out
    assert golden_trace.main([*root, "compare", "--a", "a", "--b", "missing"]) == 1


def test_spread_reports_a_side_that_disagrees_with_itself(tmp_path, capsys):
    _hand_made_run(tmp_path / "a" / "s" / "run-001", "end")
    _hand_made_run(tmp_path / "a" / "s" / "run-002", "home")
    assert golden_trace.main(["--root", str(tmp_path), "spread", "--a", "a"]) == 1
    assert "pending" in capsys.readouterr().out


def test_record_refuses_an_unknown_scenario_and_an_unconfirmed_robot(tmp_path, capsys):
    root = ["--root", str(tmp_path)]
    assert golden_trace.main([*root, "record", "--label", "x", "--scenario", "nope"]) == 2
    assert golden_trace.main([*root, "record", "--label", "x", "--scenario", "walkaway",
                              "--base-url", "http://127.0.0.1:1", "--host-root", str(tmp_path)]) == 1
    assert "--confirm-clear" in capsys.readouterr().out


def test_record_refuses_a_sim_only_scenario_on_a_robot(tmp_path, capsys):
    code = golden_trace.main(["--root", str(tmp_path), "record", "--label", "x", "--scenario",
                              "language_double_turn", "--base-url", "http://127.0.0.1:1"])
    assert code == 2
    assert "does not run" in capsys.readouterr().out


def test_list_names_every_scenario(capsys):
    assert golden_trace.main(["list"]) == 0
    out = capsys.readouterr().out
    for name in SIM_SCENARIOS:
        assert name in out


# --- what lives in an add-on directory stays there ------------------------------------


def test_scenarios_tolerances_and_recorders_load_from_any_addon_directory(tmp_path):
    golden = tmp_path / "addons" / "sample" / "golden"
    golden.mkdir(parents=True)
    (golden / "scenarios.py").write_text(textwrap.dedent("""
        from golden.scenarios import Scenario
        def _run(context):
            context.get("/healthz")
        def _note(run_dir, host):
            (run_dir / "noted").write_text("yes")
        SCENARIOS = (Scenario("sample_mode", _run, "A scenario of its own."),)
        RECORDERS = (_note,)
    """), encoding="utf-8")
    (golden / "tolerances.yaml").write_text("quantities: {sample_s: {median: {relative: 0.1}}}\n",
                                            encoding="utf-8")
    found = scenarios.all_scenarios(tmp_path)
    assert "sample_mode" in found and "full_session" in found
    assert len(scenarios.addon_recorders(tmp_path)) == 1
    assert scenarios.addon_tolerances(tmp_path) == [golden / "tolerances.yaml"]


def test_no_addon_directory_is_nothing_extra(tmp_path):
    assert scenarios.addon_scenarios(tmp_path) == ()
    assert scenarios.addon_recorders(tmp_path) == ()
    assert scenarios.addon_tolerances(tmp_path) == []
    assert tuple(scenarios.all_scenarios(tmp_path)) == tuple(SIM_SCENARIOS)


# --- the fake voice -------------------------------------------------------------------


def test_the_fake_voice_has_the_surface_of_the_real_one():
    from emori_adapter.hostapp.voice_link import VoiceLink

    wanted = {name for name in vars(VoiceLink) if not name.startswith("_")} - {"feed"}
    assert wanted <= set(dir(FakeVoice)), wanted - set(dir(FakeVoice))


def test_the_fake_voice_answers_like_a_voice():
    voice = FakeVoice()
    assert voice.silent_for_s() == float("inf") and not voice.active
    start = time.monotonic()
    voice.rise()
    assert voice.active and voice.onset_count == 1
    assert voice.rose_since(start) and voice.active_since(start)
    assert voice.silent_for_s() < 0.01
    voice.syllable()
    assert voice.onset_count == 2
    voice.hush()
    assert not voice.active
    time.sleep(0.03)
    assert voice.silent_for_s() > 0.0
    assert not voice.rose_since(time.monotonic())


def test_the_fake_voice_follows_its_script():
    voice = FakeVoice(syllable_s=0.05)
    voice.play([(0.05, 0.15), (0.25, 0.3)])
    assert voice.wait_active(1.0)
    assert voice.wait_for_onset(1.0)
    assert voice.wait_done(2.0)
    assert not voice.active and voice.onset_count >= 3


def test_a_waiting_player_can_be_released():
    voice = FakeVoice()
    stop = threading.Event()
    stop.set()
    assert voice.wait_active(5.0, stop=stop) is False
    assert voice.wait_for_onset(5.0, stop=stop) is False
    assert voice.wait_for_onset(5.0, abort=lambda: True) is False


# --- the stub broker ------------------------------------------------------------------


def test_the_stub_broker_is_reachable_and_records_the_writer(tmp_path):
    from emori_adapter.fiware import broker_reachable

    stub = StubOrion(tmp_path / "fiware.jsonl")
    url = stub.start()
    try:
        assert broker_reachable(url)
        assert httpx.post(f"{url}/ngsi-ld/v1/entities/", json={"id": "urn:x"}).status_code == 201
        session = "s-20261103-000105-346c623c2873c0ed"
        answer = httpx.post(f"{url}/ngsi-ld/v1/entities/urn:{session}/attrs",
                            json={"a": {"value": 1, "observedAt": "2026-11-03T09:00:00Z"}})
        assert answer.status_code == 204
        assert httpx.get(f"{url}/ngsi-ld/v1/subscriptions/").json() == []
    finally:
        stub.stop()
    calls = read_jsonl(tmp_path / "fiware.jsonl")
    assert [c["method"] for c in calls] == ["POST", "POST", "GET"]
    assert calls[1]["path"] == "/ngsi-ld/v1/entities/urn:<sid:1>/attrs"
    assert calls[1]["body"]["a"] == {"value": 1, "observedAt": "<time>"}


# --- the voice track ------------------------------------------------------------------


def test_a_track_takes_its_sentences_from_the_motion_sync_fixture():
    from motion_sync import fixture

    sentences = voice_track.sentences_for("en", ("normal_a", "comma_pause_a"),
                                          continuing=("comma_pause_a",))
    assert [s.case for s in sentences] == ["normal_a", "comma_pause_a"]
    assert sentences[0] in fixture.build("en")
    assert sentences[1].turn_end is False and sentences[0].turn_end is True
    with pytest.raises(KeyError):
        voice_track.sentences_for("en", ("no_such_case",))


def test_a_track_is_laid_out_by_the_motion_sync_player():
    sentences = voice_track.sentences_for("en", ("normal_a", "comma_pause_a"))
    lengths = voice_track.estimated_lengths(sentences, rate=1000)
    plan = voice_track.plan_track(sentences, lengths, 1000)
    assert plan.placements == voice_track.sync_run.lay_out(sentences, lengths, 1000)
    assert plan.schedule == voice_track.sync_run.build_schedule(plan.placements, 1000)
    spans = plan.spans()
    assert len(spans) == 3, "one span per spoken part"
    assert spans[2][0] - spans[1][1] == pytest.approx(sentences[1].pause_s, abs=0.002)
    marks = []
    voice_track.mark_voice(plan, 100.0, lambda name, **fields: marks.append((name, fields)))
    assert sum(name == "voice_span" for name, _ in marks) == 3
    onsets = [fields["t"] for name, fields in marks if name == "voice_onset"]
    assert onsets == [100.0 + place.part_starts[0] / 1000 for place in plan.placements]


def test_speech_goes_faster_only_when_asked():
    sentences = voice_track.sentences_for("en", ("normal_a",))
    normal = voice_track.estimated_lengths(sentences)["normal_a"][0]
    fast = voice_track.estimated_lengths(sentences, speed=2.0)["normal_a"][0]
    assert fast == pytest.approx(normal / 2, abs=1)


class _FakeCapture:
    made: list = []

    def __init__(self, command, path, rate, *, env=None) -> None:
        self.command = command
        self.stopped = self.deleted = False
        _FakeCapture.made.append(self)

    def start(self, *, keep_head_s: float = 0.0) -> None:
        return

    def samples(self) -> int:
        return 1

    def stop(self) -> None:
        self.stopped = True

    def delete(self) -> None:
        self.deleted = True


class _FakeMachine:
    def __init__(self) -> None:
        self.spawned: list = []

    def env(self) -> dict:
        return {}

    def spawn(self, argv, **kwargs):
        self.spawned.append(argv)
        return SimpleNamespace(wait=lambda timeout=None: 0, poll=lambda: 0, terminate=lambda: None)


def test_a_robot_track_plays_one_wav_and_times_events_from_hearing_it(tmp_path, monkeypatch):
    sync_run = voice_track.sync_run
    sentences = voice_track.sentences_for("en", ("normal_a",))
    placements = sync_run.lay_out(sentences, voice_track.estimated_lengths(sentences, rate=1000), 1000)
    wav = tmp_path / "track.wav"
    rendered = sync_run.Rendered(wav, 1000, placements)
    monkeypatch.setattr(sync_run, "render", lambda *a, **k: rendered)
    now = time.monotonic()
    monkeypatch.setattr(sync_run, "find_pilot", lambda capture, rate: (0, now))
    posted: list = []

    class Client:
        def post(self, path, body):
            posted.append((path, body["type"]))
            return 200, {}

        def close(self):
            return

    # Compress the run: everything scheduled happens at once.
    monkeypatch.setattr(sync_run, "build_schedule", lambda places, rate: [
        sync_run.Post(0.0, sync_run.UTTERANCE, places[0].sentence),
        sync_run.Post(0.0, sync_run.TTS_ENDED)])
    voice = tmp_path / "en.onnx"
    voice.write_bytes(b"")
    machine = _FakeMachine()
    _FakeCapture.made = []
    track = voice_track.RobotTrack(root=tmp_path, run_dir=tmp_path, sink="delay_sink",
                                   voice_file=voice, machine=machine, capture_factory=_FakeCapture)
    marks: list = []
    failed = track.play(("normal_a",), "en", Client, lambda name, **fields: marks.append(name))
    assert failed == 0
    assert machine.spawned == [["pw-play", "--target", "delay_sink", str(wav)]]
    assert len(_FakeCapture.made) == 2, "the speaker, and the sink ahead of it"
    assert all(capture.stopped and capture.deleted for capture in _FakeCapture.made)
    assert sorted(kind for _, kind in posted) == [sync_run.UTTERANCE, sync_run.TTS_ENDED]
    assert marks.count("voice_onset") == 1 and marks.count("voice_span") == 1


def test_a_robot_track_with_no_voice_refuses(tmp_path):
    track = voice_track.RobotTrack(root=tmp_path, run_dir=tmp_path, voice_file=tmp_path / "none.onnx",
                                   machine=_FakeMachine(), capture_factory=_FakeCapture)
    with pytest.raises(voice_track.sync_run.Refused):
        track.play(("normal_a",), "en", lambda: None, lambda *a, **k: None)


def test_two_running_orders_do_not_evict_each_others_audio(tmp_path, monkeypatch):
    sync_run = voice_track.sync_run
    seen: list = []
    monkeypatch.setattr(sync_run, "render", lambda sentences, lang, cache, *a, **k: seen.append(cache))
    voice = tmp_path / "en.onnx"
    voice.write_bytes(b"")
    track = voice_track.RobotTrack(root=tmp_path, run_dir=tmp_path, voice_file=voice,
                                   machine=_FakeMachine(), capture_factory=_FakeCapture)
    track._render(voice_track.sentences_for("en", ("normal_a",)), "en")
    track._render(voice_track.sentences_for("en", ("normal_b",)), "en")
    assert seen[0] != seen[1]
    assert sync_run.AUDIO_CACHE not in [Path(*cache.parts[-2:]) for cache in seen]


# --- a running host, on a robot -------------------------------------------------------


def test_a_robot_host_copies_only_what_this_run_added(tmp_path, monkeypatch):
    client_dir = tmp_path / "out"
    client_dir.mkdir()
    (client_dir / "client.jsonl").write_text('{"method": "before"}\n', encoding="utf-8")
    trace = [{"seq": 7, "t": 5.0, "event": "accepted", "client": "hostapp", "lane": "interaction"},
             {"seq": 8, "t": 5.1, "event": "started", "client": "hostapp", "lane": "interaction"}]
    asked: list = []

    def fake_get(url, *, params=None, timeout=None):
        asked.append(params)
        return SimpleNamespace(json=lambda: [{"seq": 6}] if params["after"] == -1 else trace)

    monkeypatch.setattr(record.httpx, "get", fake_get)
    host = record.RobotHost(base_url="http://127.0.0.1:1/", root=tmp_path, client_dir=client_dir,
                            arbiter_url="http://127.0.0.1:2/")
    host.start()
    with (client_dir / "client.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"method": "after"}\n')
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    host.collect(run_dir)
    assert [r["method"] for r in read_jsonl(run_dir / "client.jsonl")] == ["after"]
    assert asked == [{"after": -1}, {"after": 6}]
    motion = read_jsonl(run_dir / "motion.jsonl")
    assert [r["t_mono"] for r in motion] == [5.0], "only the accepted records are submits"


def test_a_robot_host_restart_needs_a_command(tmp_path):
    host = record.RobotHost(base_url="http://127.0.0.1:1", root=tmp_path)
    with pytest.raises(record.Refused):
        host.restart()


def test_a_robot_host_restart_runs_it_and_waits(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(record.subprocess, "run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(record.httpx, "get",
                        lambda url, timeout=None: SimpleNamespace(status_code=200))
    record.RobotHost(base_url="http://127.0.0.1:1", root=tmp_path,
                     restart_command=["restart-it"]).restart()
    assert calls == [["restart-it"]]


def test_the_database_dump_keeps_only_this_run(tmp_path):
    import sqlite3

    path = tmp_path / "dashboard.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions (session_id TEXT, started_at REAL, status TEXT, end_reason TEXT,
          lang TEXT, module_id TEXT, topic TEXT, is_roleplay INT, avg_engagement REAL,
          dominant_style TEXT, performance_scores TEXT);
        CREATE TABLE cycles (session_id TEXT);
        CREATE TABLE events (ts REAL, session_id TEXT, kind TEXT, detail TEXT);
        INSERT INTO sessions (session_id, started_at, status) VALUES ('old', 10, 'completed');
        INSERT INTO sessions (session_id, started_at, status) VALUES ('new', 200, 'abandoned');
        INSERT INTO cycles VALUES ('new'), ('new'), ('old');
        INSERT INTO events VALUES (10, 'old', 'kind-old', ''), (200, 'new', 'kind-new', 'private');
    """)
    connection.commit()
    connection.close()
    db = record.dump_db(path, 100.0)
    assert [row["session_id"] for row in db["sessions"]] == ["new"]
    assert db["sessions"][0]["cycles"] == 2
    assert db["events"] == [{"session_id": "new", "kind": "kind-new"}]
    assert record.dump_db(tmp_path / "absent.db", 0.0) is None


def test_the_script_runs_as_a_script():
    done = subprocess.run([sys.executable, str(golden_trace.ROOT / "scripts" / "golden_trace.py"),
                           "list"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(golden_trace.ROOT / "src")})
    assert done.returncode == 0 and "full_session" in done.stdout
