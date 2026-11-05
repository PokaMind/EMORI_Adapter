"""``scripts/golden_ci.py``: what CI does with the committed golden recordings."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import golden_ci  # noqa: E402


def _golden(directory: Path, scenario: str, runs: int = 2, *, fast: bool = True,
            lang: str = "en", meta: bool = True) -> None:
    for number in range(1, runs + 1):
        run = directory / scenario / f"run-{number:03d}"
        run.mkdir(parents=True)
        (run / "client.jsonl").write_text("", encoding="utf-8")
        if meta:
            (run / "meta.json").write_text(json.dumps({"fast": fast, "lang": lang}),
                                           encoding="utf-8")


class _Trace:
    """Stands in for the harness's command line and remembers what it was asked."""

    def __init__(self, record: int = 0, compare: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._codes = {"record": record, "compare": compare}

    def __call__(self, _root: Path, *arguments: str) -> int:
        self.calls.append(arguments)
        return self._codes[arguments[0]]


def test_the_committed_goldens_are_usable_by_the_check():
    """Each is a scenario the harness knows, with the runs its tolerances ask for."""

    assert (golden_ci.DEFAULT_GOLDENS / "README.md").is_file()
    found = {golden.scenario: golden for golden in golden_ci.recorded(golden_ci.DEFAULT_GOLDENS)}
    assert set(found) == {"cue_families", "full_session", "preempt", "speech_plans"}
    assert set(found) <= golden_ci._known_scenarios()
    for golden in found.values():
        assert golden.fast and golden.lang == "en"
    # A voiced scenario needs more runs for its spread to say anything.
    assert found["full_session"].runs == found["speech_plans"].runs == 5
    assert found["cue_families"].runs == 5
    assert found["preempt"].runs == 3


def test_the_committed_goldens_are_small_and_hold_no_secret_or_local_path():
    forbidden = ("pkr1.", "eyJ", "Bearer ", "\\Users\\","/Users/", "/home/", "secrets")
    total = 0
    for path in golden_ci.DEFAULT_GOLDENS.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        assert size < 100_000, f"{path} is {size} bytes"
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path} holds {needle!r}"
    assert total < 3_000_000


def test_with_nothing_recorded_the_check_says_so_and_passes(tmp_path, capsys):
    trace = _Trace()
    assert golden_ci.check(tmp_path / "none", tmp_path / "out", "ci", trace=trace) == 0
    assert "nothing is recorded" in capsys.readouterr().out
    assert trace.calls == []


def test_with_nothing_recorded_the_check_fails_when_recordings_are_required(tmp_path, capsys):
    trace = _Trace()
    assert golden_ci.check(tmp_path / "none", tmp_path / "out", "ci", trace=trace,
                           require=True) == 1
    assert "nothing is recorded" in capsys.readouterr().out
    assert trace.calls == []


def test_required_recordings_that_are_there_are_compared_as_usual(tmp_path):
    _golden(tmp_path / "g", "full_session")
    trace = _Trace()
    assert golden_ci.check(tmp_path / "g", tmp_path / "out", "ci",
                           known=lambda: {"full_session"}, trace=trace, require=True) == 0
    assert [call[0] for call in trace.calls] == ["record", "compare"]


def test_the_command_line_flag_requires_recordings(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    base = ["check", "--goldens", str(empty), "--root", str(tmp_path / "out")]
    assert golden_ci.main(base) == 0
    assert golden_ci.main([*base, "--require-recordings"]) == 1


def test_a_golden_is_recorded_again_the_way_it_was_made_and_compared_exactly(tmp_path):
    _golden(tmp_path / "g", "full_session", runs=3, fast=True, lang="ca")
    trace = _Trace()
    code = golden_ci.check(tmp_path / "g", tmp_path / "out", "ci", known=lambda: {"full_session"},
                           trace=trace)
    assert code == 0
    record, compare = trace.calls
    assert record == ("record", "--label", "ci", "--scenario", "full_session", "--runs", "3",
                      "--lang", "ca", "--fast")
    assert compare[:2] == ("compare", "--a") and "--exact-only" in compare
    assert compare[compare.index("--b") + 1] == "ci"
    assert compare[compare.index("--scenario") + 1] == "full_session"


def test_a_golden_recorded_in_real_time_is_not_recorded_compressed(tmp_path):
    _golden(tmp_path / "g", "preempt", runs=1, fast=False)
    trace = _Trace()
    golden_ci.check(tmp_path / "g", tmp_path / "out", "ci", known=lambda: {"preempt"},
                    trace=trace)
    assert "--fast" not in trace.calls[0]


@pytest.mark.parametrize("record, compare", [(1, 0), (0, 1)])
def test_a_failed_recording_or_a_difference_fails_the_check(tmp_path, record, compare):
    _golden(tmp_path / "g", "full_session")
    trace = _Trace(record=record, compare=compare)
    assert golden_ci.check(tmp_path / "g", tmp_path / "out", "ci",
                           known=lambda: {"full_session"}, trace=trace) == 1


def test_a_failure_in_one_scenario_does_not_hide_the_next(tmp_path):
    _golden(tmp_path / "g", "full_session")
    _golden(tmp_path / "g", "preempt")
    trace = _Trace(compare=1)
    code = golden_ci.check(tmp_path / "g", tmp_path / "out", "ci",
                           known=lambda: {"full_session", "preempt"}, trace=trace)
    assert code == 1
    assert [call[0] for call in trace.calls] == ["record", "compare", "record", "compare"]


def test_a_scenario_the_harness_does_not_know_fails_the_check(tmp_path, capsys):
    _golden(tmp_path / "g", "retired_scenario")
    trace = _Trace()
    assert golden_ci.check(tmp_path / "g", tmp_path / "out", "ci", known=lambda: {"other"},
                           trace=trace) == 1
    assert "retired_scenario" in capsys.readouterr().out
    assert trace.calls == []


def test_a_run_without_meta_fails_the_check(tmp_path, capsys):
    _golden(tmp_path / "g", "full_session", meta=False)
    assert golden_ci.check(tmp_path / "g", tmp_path / "out", "ci", known=lambda: set(),
                           trace=_Trace()) == 1
    assert "no meta.json" in capsys.readouterr().out


def test_runs_that_disagree_on_how_they_were_made_fail_the_check(tmp_path, capsys):
    _golden(tmp_path / "g", "full_session", runs=1, fast=True)
    second = tmp_path / "g" / "full_session" / "run-002"
    second.mkdir()
    (second / "meta.json").write_text(json.dumps({"fast": False, "lang": "en"}), encoding="utf-8")
    assert golden_ci.check(tmp_path / "g", tmp_path / "out", "ci",
                           known=lambda: {"full_session"}, trace=_Trace()) == 1
    assert "disagree on fast" in capsys.readouterr().out


def test_the_scenarios_a_golden_may_name_are_the_harnesss_own():
    """The default ``known`` is the harness's scenario table, not a list kept here."""

    names = golden_ci._known_scenarios()
    assert {"full_session", "preempt"} <= names


def test_the_status_command_lists_what_is_there(tmp_path, capsys):
    _golden(tmp_path / "g", "full_session", runs=2)
    assert golden_ci.status(tmp_path / "g") == 0
    assert "full_session: 2 run(s)" in capsys.readouterr().out
