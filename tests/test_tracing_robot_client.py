"""TracingRobotClient is a pure pass-through that records reason and t_start."""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import shutil
from pathlib import Path

import pytest

from emori_adapter.models import HeadPose, MotionIntent
from emori_adapter.robot.client import DryRunRobotClient, HttpRobotClient, RobotClient
from emori_adapter.robot.tracing import TraceSink, TracingRobotClient

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 0.25
        return self.t


class _Fake:
    """Everything the player and driver call, with arguments kept for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.marker = "fake"

    def health(self):
        self.calls.append(("health",))
        return True

    def wake_up(self):
        self.calls.append(("wake_up",))

    def set_motor_mode(self, mode):
        self.calls.append(("set_motor_mode", mode))

    def apply(self, intent):
        self.calls.append(("apply", intent))

    def read_state(self):
        self.calls.append(("read_state",))
        return {"head_pose": {}}

    def set_gaze(self, enabled):
        self.calls.append(("set_gaze", enabled))
        return True

    def set_head_reserved(self, reserved):
        self.calls.append(("set_head_reserved", reserved))
        return False

    def play_recorded_move(self, name, *, supersede=False):
        self.calls.append(("play_recorded_move", name, supersede))
        return "uuid-1"

    def stop_recorded_move(self, uuid):
        self.calls.append(("stop_recorded_move", uuid))

    def close(self):
        self.calls.append(("close",))


def test_wrapper_has_every_public_method_of_the_clients():
    names = {n for n in dir(RobotClient) if not n.startswith("_")}
    names |= {n for n in dir(HttpRobotClient) if not n.startswith("_") and callable(
        getattr(HttpRobotClient, n))}
    names |= {n for n in dir(DryRunRobotClient) if not n.startswith("_") and callable(
        getattr(DryRunRobotClient, n))}
    for n in sorted(names):
        assert n in vars(TracingRobotClient), f"TracingRobotClient lacks {n}"


def test_play_recorded_move_signature_takes_keyword_supersede():
    sig = inspect.signature(TracingRobotClient.play_recorded_move)
    p = sig.parameters["supersede"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False


def test_every_call_passes_through_and_is_recorded():
    fake = _Fake()
    c = TracingRobotClient(fake, clock=_Clock())
    intent = MotionIntent(kind="goto", head=HeadPose(x=1, y=2, z=3, roll=0, pitch=4, yaw=5),
                          reason="speaking (sustain)", duration=0.4)

    assert c.health() is True
    c.wake_up()
    c.set_motor_mode("enabled")
    c.apply(intent)
    assert c.read_state() == {"head_pose": {}}
    assert c.set_gaze(True) is True
    assert c.set_head_reserved(False) is False
    assert c.play_recorded_move("happy1") == "uuid-1"
    assert c.play_recorded_move("happy2", supersede=True) == "uuid-1"
    c.stop_recorded_move("uuid-1")
    c.close()

    assert fake.calls[3] == ("apply", intent) and fake.calls[3][1] is intent
    assert ("play_recorded_move", "happy1", False) in fake.calls
    assert ("play_recorded_move", "happy2", True) in fake.calls
    recs = c.sink.records
    assert [r["method"] for r in recs] == [
        "health", "wake_up", "set_motor_mode", "apply", "read_state", "set_gaze",
        "set_head_reserved", "play_recorded_move", "play_recorded_move",
        "stop_recorded_move", "close"]
    apply_rec = recs[3]
    assert apply_rec["args"]["intent"]["reason"] == "speaking (sustain)"
    assert apply_rec["args"]["intent"]["kind"] == "goto"
    assert apply_rec["t_start"] == pytest.approx(100.25 + 0.5 * 3)
    assert apply_rec["dur_s"] == pytest.approx(0.25)
    assert recs[4]["ret"] == "state"
    assert recs[5]["args"] == {"enabled": True} and recs[5]["ret"] is True
    assert recs[8]["args"] == {"name": "happy2", "supersede": True}


def test_supersede_is_not_forwarded_when_unset():
    class Old:
        def play_recorded_move(self, name):
            return "u"

    c = TracingRobotClient(Old())
    assert c.play_recorded_move("x") == "u"


def test_exceptions_propagate_and_are_recorded():
    class Boom(_Fake):
        def apply(self, intent):
            raise RuntimeError("daemon gone")

    c = TracingRobotClient(Boom())
    with pytest.raises(RuntimeError, match="daemon gone"):
        c.apply(MotionIntent(kind="goto", reason="r"))
    assert c.sink.records[0]["error"] == "RuntimeError: daemon gone"
    assert c.sink.records[0]["args"]["intent"]["reason"] == "r"


def test_a_missing_optional_method_stays_missing():
    class Bare:
        pass

    c = TracingRobotClient(Bare())
    with pytest.raises(AttributeError):
        c.set_gaze(True)
    assert getattr(c.inner, "set_gaze", None) is None
    assert c.sink.records == []


def test_other_attributes_read_through():
    dry = DryRunRobotClient()
    c = TracingRobotClient(dry)
    c.apply(MotionIntent(kind="goto", reason="x"))
    assert c.applied is dry.applied and len(c.applied) == 1


def test_works_over_the_dry_run_client():
    dry = DryRunRobotClient()
    c = TracingRobotClient(dry)
    assert c.play_recorded_move("m") == "dry-1"
    c.stop_recorded_move("dry-1")
    assert c.set_gaze(True) is True
    assert dry.recorded_played == ["m"] and dry.gaze_calls == [True]


def test_sink_writes_json_lines_and_survives_a_dead_file(tmp_path):
    path = tmp_path / "out" / "client.jsonl"
    sink = TraceSink(path)
    c = TracingRobotClient(_Fake(), sink)
    c.apply(MotionIntent(kind="goto", reason="idle"))
    line = json.loads(path.read_text().splitlines()[0])
    assert line["method"] == "apply" and line["args"]["intent"]["reason"] == "idle"

    sink._fh.close()  # a failing disk must not reach the caller
    assert c.health() is True
    c.close()


def test_passive_recorder_samples(tmp_path):
    mod = _load("golden_passive")
    t = {"now": 0.0}

    def clock():
        return t["now"]

    def sleep(s):
        t["now"] += s

    n = mod.record(tmp_path, [7], seconds=3, interval=1.0, clock=clock, sleep=sleep,
                   sampler=lambda pids: mod.sample(pids, clock, lambda: 123, lambda p: 45))
    rows = [json.loads(x) for x in (tmp_path / "sys.jsonl").read_text().splitlines()]
    assert n == len(rows) == 4
    assert rows[0]["mem_available_kb"] == 123 and rows[0]["pss_kb"] == {"7": 45}


def test_proc_readers(tmp_path):
    mod = _load("golden_passive")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 10 kB\nMemAvailable:    2048 kB\n")
    assert mod.read_mem_available_kb(str(meminfo)) == 2048
    (tmp_path / "9").mkdir()
    (tmp_path / "9" / "smaps_rollup").write_text("Rss: 5 kB\nPss:   77 kB\n")
    assert mod.read_pss_kb(9, str(tmp_path)) == 77
    assert mod.read_pss_kb(10, str(tmp_path)) is None
    assert mod.read_mem_available_kb(str(tmp_path / "none")) is None


def test_golden_host_injects_the_tracing_client(tmp_path, monkeypatch):
    from emori_adapter.hostapp import server

    seen = {}

    def fake_build(overrides=None):
        seen.update(overrides or {})
        return object()

    monkeypatch.setattr(server, "_build_app", fake_build)
    mod = _load("golden_host")
    app, sink = mod.build_traced_app(tmp_path, "http://127.0.0.1:1")
    client = seen["robot_client"]
    assert isinstance(client, TracingRobotClient)
    assert isinstance(client.inner, HttpRobotClient)
    assert client.sink is sink
    assert (tmp_path / "client.jsonl").exists()
    sink.close()


def test_slice_wrapper_dry_run():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    out = subprocess.run([bash, str(ROOT / "scripts" / "golden_in_slice.sh"), "--dry-run",
                          "--", "echo", "hi"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "--slice=system.slice" in out.stdout and "--uid=pollen" in out.stdout
    assert out.stdout.strip().endswith("echo hi")


def test_with_a_file_nothing_is_kept_in_memory(tmp_path):
    from emori_adapter.robot.tracing import TraceSink

    sink = TraceSink(tmp_path / "client.jsonl")
    for index in range(100):
        sink.write({"index": index})
    sink.close()
    assert sink.records == []
    assert len((tmp_path / "client.jsonl").read_text(encoding="utf-8").splitlines()) == 100


def test_without_a_file_records_are_kept_for_the_caller():
    from emori_adapter.robot.tracing import TraceSink

    sink = TraceSink()
    sink.write({"index": 1})
    assert sink.records == [{"index": 1}]
