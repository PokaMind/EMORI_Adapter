"""Contract tests: the LITERAL envelopes the Pokamind web app posts to the kiosk.

The live envelope carries ONLY {type, communication_styles, body_language,
body_language_metrics{engagement, confidence}, seq, timestamp} — and every body value
is None whenever body analysis fails (no body data -> every field null).
The parser must never raise on that.
"""

from __future__ import annotations

import pytest

from emori_adapter.constants import COMM_STYLES
from emori_adapter.sources.websocket import cycle_from_envelope, sanitize_envelope


def _live_styles() -> dict[str, float]:
    # All 31 canonical styles, as percentages summing ~100.
    base = round(100.0 / len(COMM_STYLES), 2)
    return {s: base for s in COMM_STYLES}


def live_envelope(seq: int = 3) -> dict:
    """The exact success-path shape the web app relays."""
    return {
        "type": "pokamind_analysis_update",
        "communication_styles": _live_styles(),
        "body_language": {
            "posture": "leaning_forward",
            "head_position": "straight",
            "gesture_type": "open_palm",
            "overall_energy": "medium",
            "engagement_score": 72,
            "confidence_score": 65,
        },
        "body_language_metrics": {"engagement": 72, "confidence": 65},
        "seq": seq,
        "timestamp": "2026-06-11T10:30:24.000Z",
    }


def null_body_envelope(seq: int = 4) -> dict:
    """The failure-path shape: no body data -> every value null."""
    return {
        "type": "pokamind_analysis_update",
        "communication_styles": _live_styles(),
        "body_language": {
            "posture": None,
            "head_position": None,
            "gesture_type": None,
            "overall_energy": None,
            "engagement_score": None,
            "confidence_score": None,
        },
        "body_language_metrics": {"engagement": None, "confidence": None},
        "seq": seq,
        "timestamp": "2026-06-11T10:30:26.000Z",
    }


def test_success_envelope_parses():
    c = cycle_from_envelope(live_envelope())
    assert c.seq == 3
    assert c.communication_styles and len(c.communication_styles) == len(COMM_STYLES)
    assert c.body_language["posture"] == "leaning_forward"
    assert c.body_language_metrics == {"engagement": 72.0, "confidence": 65.0}


def test_null_body_envelope_does_not_crash():
    """THE reproduced live crash: null metric values must be dropped, not 500."""
    c = cycle_from_envelope(null_body_envelope())
    assert c.body_language_metrics == {}
    assert c.body_language == {}
    assert c.seq == 4


def test_partially_null_metrics_keep_good_values():
    env = live_envelope()
    env["body_language_metrics"] = {"engagement": 55, "confidence": None}
    c = cycle_from_envelope(env)
    assert c.body_language_metrics == {"engagement": 55.0}


def test_null_styles_dropped():
    env = live_envelope()
    env["communication_styles"]["steady"] = None
    c = cycle_from_envelope(env)
    assert "steady" not in c.communication_styles
    assert "direct" in c.communication_styles


def test_identity_stamping_overrides_payload():
    env = live_envelope()
    env["session_id"] = "stale-id"
    env["person_id"] = "stale-person"
    c = cycle_from_envelope(env, session_id="anna-001-20260611", person_id="anna-001")
    assert c.session_id == "anna-001-20260611"
    assert c.person_id == "anna-001"


def test_identity_defaults_when_not_stamped():
    c = cycle_from_envelope(live_envelope())
    assert c.session_id == "live-session"
    assert c.person_id == "learner_01"


def test_missing_everything_still_parses():
    c = cycle_from_envelope({"type": "pokamind_analysis_update"})
    assert c.seq == 0
    assert c.timestamp == ""
    assert c.communication_styles == {}


def test_string_numerics_coerced():
    env = live_envelope()
    env["body_language_metrics"] = {"engagement": "72", "confidence": "bad"}
    c = cycle_from_envelope(env)
    assert c.body_language_metrics == {"engagement": 72.0}


def test_sanitize_envelope_is_pure():
    env = null_body_envelope()
    out = sanitize_envelope(env)
    assert env["body_language_metrics"]["engagement"] is None  # input untouched
    assert out["body_language_metrics"] == {}


@pytest.mark.parametrize("bad", [None, [], "x", 42])
def test_non_dict_sections_ignored(bad):
    env = live_envelope()
    env["body_language"] = bad
    env["communication_styles"] = bad
    c = cycle_from_envelope(env)
    assert c.body_language == {}
    assert c.communication_styles == {}


def test_turn_event_constants_exist():
    from emori_adapter.sources import websocket as ws
    assert ws.TTS_ENDED_EVENT == "pokamind_tts_ended"
    assert ws.TURN_ARMED_EVENT == "pokamind_turn_armed"
    assert ws.TURN_SUBMITTED_EVENT == "pokamind_turn_submitted"
    assert ws.TURN_REJECTED_EVENT == "pokamind_turn_rejected"


# --- the per-SENTENCE stream (what the robot's own voice is doing) -----------------
#
# The relay carries every stamped pokamind_* envelope, not just the analysis cycles, and
# the ROS engagement lane needs the utterance ones: it moves the head WHILE a sentence is
# spoken. These pin the parse, because the fields arrive from a cross-origin postMessage
# and reach a ROS topic verbatim.

def _utterance_envelope(**extra) -> dict:
    payload = {"type": "pokamind_ai_utterance", "seq": 4,
               "text": "I really do understand how hard that was.",
               "cue_category": "tension", "cue_body": "gently, almost whispered",
               "lang": "en", "source": "turn",
               "timestamp": "2026-08-11T10:30:24.000Z",
               "session_id": "anna-001-20260811", "person_id": "anna-001"}
    payload.update(extra)
    return payload


def test_an_utterance_envelope_becomes_a_speech_event():
    from emori_adapter.behavior.cues import sentence_duration_s
    from emori_adapter.sources.websocket import speech_event_from_envelope

    event = speech_event_from_envelope(_utterance_envelope())

    assert event.ended is False
    assert event.cue_category == "tension" and event.lang == "en"
    assert event.session_id == "anna-001-20260811" and event.person_id == "anna-001"
    # The duration is Pokamind's OWN playback model, so a consumer can bound a plan to
    # the voice without importing the planner.
    assert event.est_duration == sentence_duration_s(event.text)


def test_the_family_pokamind_wrote_travels_beside_the_one_the_robot_performed():
    from emori_adapter.sources.websocket import speech_event_from_envelope

    event = speech_event_from_envelope(_utterance_envelope(
        cue_category="levity", cue_category_authored="tension"))
    assert (event.cue_category, event.cue_category_authored) == ("levity", "tension")
    assert speech_event_from_envelope(_utterance_envelope()).cue_category_authored == ""


def test_the_end_marker_carries_no_text_and_no_family():
    """`pokamind_tts_ended` says the voice stopped; anything else on it is noise."""

    from emori_adapter.sources.websocket import speech_event_from_envelope

    event = speech_event_from_envelope(
        _utterance_envelope(type="pokamind_tts_ended"), ended=True)

    assert event.ended is True
    assert event.text == "" and event.cue_category == "" and event.est_duration == 0.0


def test_cue_fields_are_bounded_and_never_none():
    """They arrive verbatim from a cross-origin postMessage; nothing legitimate is long."""

    from emori_adapter.sources.websocket import speech_event_from_envelope

    event = speech_event_from_envelope(_utterance_envelope(
        cue_body="x" * 5000, cue_category=None, lang=None, seq="not-a-number"))

    assert len(event.cue_body) == 200
    assert event.cue_category == "" and event.lang == ""
    assert event.seq == 0
