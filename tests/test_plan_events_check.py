"""The S4 gate's comparison of the motion plan topic with the motion trace."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan_events_check.py"
_SPEC = importlib.util.spec_from_file_location("plan_events_check", _SCRIPT)
check = importlib.util.module_from_spec(_SPEC)
sys.modules["plan_events_check"] = check
_SPEC.loader.exec_module(check)


def _sentence(plan=1, planned=True, performed="tension", authored="tension", slug="tension/a"):
    return {"s": "sentence", "plan": plan, "seq": plan, "planned": planned, "performed": performed,
            "authored": authored, "slug": slug}


def _batch(plan=1, outcome="finished", skip_reason=""):
    return {"s": "batch_end", "plan": plan, "outcome": outcome, "skip_reason": skip_reason}


def _steps(plan=1, family="tension", authored="tension", slug="tension/a", outcome="finished",
           skip_reason=""):
    base = {"plan_id": str(plan), "seq": plan, "family": family, "family_authored": authored,
            "slug": slug, "skip_reason": "", "outcome": ""}
    return [{**base, "ended": False}, {**base, "started": True, "ended": False},
            {**base, "ended": True, "outcome": outcome, "skip_reason": skip_reason}]


def test_a_plan_they_agree_on():
    assert check.disagreements(_steps(), [_sentence(), _batch()]) == []


def test_the_family_on_the_topic_must_be_the_one_performed():
    problems = check.disagreements(_steps(family="levity"), [_sentence(), _batch()])
    assert len(problems) == 1 and "family" in problems[0] and "levity" in problems[0]


def test_a_sentence_nothing_was_planned_for_has_no_family_on_the_topic():
    trace = [_sentence(planned=False, slug="not_owner")]
    steps = _steps(family="", slug="not_owner", outcome="skipped", skip_reason="not_owner")[-1:]
    assert check.disagreements(steps, trace) == []


def test_the_outcome_and_its_reason_must_match():
    problems = check.disagreements(
        _steps(outcome="preempted"), [_sentence(), _batch(outcome="voice_end")])
    assert any("outcome" in problem for problem in problems)
    problems = check.disagreements(
        _steps(outcome="skipped", skip_reason="refused"),
        [_sentence(), _batch(outcome="skipped", skip_reason="no_voice")])
    assert any("skip_reason" in problem for problem in problems)


def test_a_plan_on_one_side_only_is_reported():
    problems = check.disagreements([], [_sentence()])
    assert problems == ["plan 1: in the trace and never on the topic"]
    problems = check.disagreements(_steps(), [])
    assert problems == ["plan 1: on the topic and not in the trace"]


def test_a_planned_sentence_with_no_end_on_either_side_is_reported():
    assert "no batch_end" in check.disagreements(_steps(), [_sentence()])[0]
    assert "ending messages" in check.disagreements(_steps()[:2], [_sentence(), _batch()])[0]


def test_batches_with_no_plan_id_are_not_sentences():
    assert check.disagreements(_steps(), [_sentence(), _batch(), _batch(plan=0)]) == []


@pytest.fixture
def files(tmp_path):
    def write(events, trace):
        first, second = tmp_path / "events.jsonl", tmp_path / "trace.jsonl"
        first.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
        second.write_text("".join(json.dumps(row) + "\n" for row in trace), encoding="utf-8")
        return str(first), str(second)

    return write


def test_the_exit_code_says_whether_they_agree(files, capsys):
    assert check.main(["compare", *files(_steps(), [_sentence(), _batch()])]) == 0
    assert "0 disagreements" in capsys.readouterr().out
    assert check.main(["compare", *files(_steps(family="x"), [_sentence(), _batch()])]) == 1


def test_nothing_to_compare_is_not_agreement(files, capsys):
    assert check.main(["compare", *files([], [_sentence()])]) == 1
    assert "nothing to compare" in capsys.readouterr().out
    assert check.main(["compare", *files(_steps(), [])]) == 1
