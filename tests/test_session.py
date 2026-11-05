"""Tests for the full on-robot session orchestrator + MockSession."""

from __future__ import annotations

from emori_adapter.robot.client import DryRunRobotClient
from emori_adapter.session import MockSession, RobotSession, SessionEvent


def test_mock_session_emits_analysis_and_utterances():
    src = MockSession(cycles_per_phase=2, realtime=False)
    events = list(src.events())
    kinds = {e.kind for e in events}
    assert kinds == {"analysis", "utterance"}
    assert events[0].kind == "utterance"  # opening line first
    assert any(e.kind == "analysis" and e.cycle is not None for e in events)
    assert sum(e.kind == "utterance" for e in events) >= 2  # opening + at least one AI line


def test_session_drives_motion_and_speech():
    client = DryRunRobotClient()
    src = MockSession(cycles_per_phase=2, realtime=False)
    result = RobotSession(src, client, topic="t").run()
    # motion happened (behaviour intents applied) and the robot "spoke" (talking gestures + utterances)
    assert client.applied, "expected robot motion intents"
    assert result["avg_engagement"] >= 0.0
    assert result["summary"].startswith("That's the end")
    assert "dominant_style" in result


def test_session_event_model():
    e = SessionEvent(kind="utterance", text="hi")
    assert e.kind == "utterance" and e.cycle is None


def test_full_arc_records_escalation_and_dominant():
    client = DryRunRobotClient()
    res = RobotSession(MockSession(realtime=False), client, topic="t").run()
    # the mock arc includes an agitated phase -> escalation should be observed
    assert res["max_escalation"] >= 0.6


def test_session_rerun_resets_state():
    # running the same instance twice must NOT double-count metrics (accumulators reset)
    sess = RobotSession(MockSession(cycles_per_phase=2, realtime=False), DryRunRobotClient(), topic="t")
    first = sess.run()
    second = sess.run()
    assert abs(first["avg_engagement"] - second["avg_engagement"]) < 1e-9
    assert first["max_escalation"] == second["max_escalation"]
