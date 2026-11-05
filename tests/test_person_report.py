"""The person report keeps "nobody measured this" distinct from "we measured zero".

Every assertion here defends one number from one specific way of lying about it. Live,
the only channels that reliably arrive are the 31 communication styles and
``body_language``; the facial / tone / verbal metric dicts of the rich mock never come,
and a lost face nulls the body too. If any of those absences were serialised as ``0.0``,
the management dashboard would read a camera pointed at a shoulder as a miserable learner --
and it would read it confidently, forever, because a plotted zero and a measured zero
are the same pixel.

So the tests below run in BOTH directions on every gated channel: absent must be the
sentinel, and a genuine zero must survive as a zero.
"""

from __future__ import annotations

import typing

import pytest

from emori_adapter.models import AnalysisCycle, ExpressionLabel
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.sources.websocket import cycle_from_envelope
from emori_adapter.translation import (
    EXPRESSION_CONSTANT_NAMES,
    NOT_REPORTED,
    PersonReportFlags,
    person_report,
)

_RICH_FLOATS = ("face_positivity", "voice_warmth", "pitch_variety")
_ALL_FLOATS = ("body_engagement", "body_confidence") + _RICH_FLOATS


def _cycle(**kw) -> AnalysisCycle:
    base = dict(timestamp="2026-08-11T10:00:00.000Z", session_id="s",
                person_id="learner_01", seq=0)
    base.update(kw)
    return AnalysisCycle(**base)


def _first(shape: str) -> AnalysisCycle:
    return next(iter(MockPokamindSource(realtime=False, shape=shape).cycles()))


# --- the two shapes that actually exist ----------------------------------------

def test_the_live_shape_reports_body_and_styles_and_admits_the_rest_is_unknown():
    """This is the shape the robot receives every day. It must not invent three channels."""

    report = person_report(_first("live"))

    assert PersonReportFlags(report.flags) == (PersonReportFlags.BODY
                                               | PersonReportFlags.STYLES)
    for field in _RICH_FLOATS:
        assert getattr(report, field) == NOT_REPORTED, field
    # ...while the channels that DID arrive carry real numbers.
    assert 0.0 <= report.body_engagement <= 1.0
    assert 0.0 <= report.body_confidence <= 1.0
    assert report.posture and report.overall_energy


def test_the_rich_mock_shape_reports_every_channel():
    report = person_report(_first("rich"))

    assert PersonReportFlags(report.flags) == (PersonReportFlags.BODY
                                               | PersonReportFlags.FACE
                                               | PersonReportFlags.TONE
                                               | PersonReportFlags.VERBAL
                                               | PersonReportFlags.STYLES)
    for field in _ALL_FLOATS:
        assert 0.0 <= getattr(report, field) <= 1.0, field


def test_the_null_storm_envelope_degrades_to_styles_only():
    """A lost face nulls every body value; `sanitize_envelope` drops them.

    What is left must say "styles only", not "this learner scored zero on everything".
    ``report_flags == 0`` is the wire's degraded status, so a styles-only cycle is
    explicitly NOT degraded -- something was still observed.
    """

    envelope = {
        "timestamp": "2026-08-11T10:00:02.000Z",
        "communication_styles": {"mirroring": 12.0, "paced": 8.0},
        "body_language": {"posture": None, "head_position": None, "gesture_type": None,
                          "overall_energy": None, "engagement_score": None,
                          "confidence_score": None},
        "body_language_metrics": {"engagement": None, "confidence": None},
    }
    report = person_report(cycle_from_envelope(envelope, session_id="s",
                                               person_id="learner_01"))

    assert PersonReportFlags(report.flags) == PersonReportFlags.STYLES
    assert report.flags != 0, "styles arrived -- the cycle is thin, not degraded"
    for field in _ALL_FLOATS:
        assert getattr(report, field) == NOT_REPORTED, field
    assert (report.posture, report.head_position,
            report.gesture_type, report.overall_energy) == ("", "", "", "")


def test_a_cycle_with_nothing_in_it_is_degraded():
    """`report_flags == 0` is the spec's degraded status -- no third message needed."""

    assert person_report(_cycle()).flags == 0


# --- the sentinel, in both directions -------------------------------------------

@pytest.mark.parametrize(
    "field, section, key",
    [
        ("face_positivity", "facial_metrics", "positivity"),
        ("voice_warmth", "verbal_metrics", "warmth"),
        ("pitch_variety", "tone_metrics", "pitch_variety"),
        ("body_engagement", "body_language_metrics", "engagement"),
        ("body_confidence", "body_language_metrics", "confidence"),
    ],
)
def test_an_unreported_channel_is_never_zero(field, section, key):
    assert getattr(person_report(_cycle()), field) == NOT_REPORTED


@pytest.mark.parametrize(
    "field, section, key",
    [
        ("face_positivity", "facial_metrics", "positivity"),
        ("voice_warmth", "verbal_metrics", "warmth"),
        ("pitch_variety", "tone_metrics", "pitch_variety"),
        ("body_engagement", "body_language_metrics", "engagement"),
        ("body_confidence", "body_language_metrics", "confidence"),
    ],
)
def test_a_reported_zero_survives_as_zero(field, section, key):
    """The other direction, and the one a defensive implementation quietly breaks.

    Coercing 0 to the sentinel "to be safe" throws away a real measurement -- the
    learner who genuinely scored nothing becomes the learner nobody looked at.
    """

    report = person_report(_cycle(**{section: {key: 0.0}}))
    assert getattr(report, field) == 0.0
    assert getattr(report, field) != NOT_REPORTED


def test_a_present_section_missing_its_key_still_reports_nothing_for_that_key():
    """The flag says the modality spoke; it does not say it said THIS.

    `expression._metric` defaults a missing key to 50 because it is blending; the
    report must not, because it is recording.
    """

    report = person_report(_cycle(facial_metrics={"eye_contact": 70.0}))
    assert PersonReportFlags(report.flags) & PersonReportFlags.FACE
    assert report.face_positivity == NOT_REPORTED


def test_junk_and_nan_values_read_as_unreported_rather_than_as_numbers():
    """`body_language` is `dict[str, Any]` -- pydantic does not filter it, so this is
    the one door a non-number can walk through, and NaN walks through all of them."""

    report = person_report(_cycle(body_language={"engagement_score": "n/a",
                                                 "confidence_score": "40"},
                                  tone_metrics={"pitch_variety": float("nan")}))
    assert report.body_engagement == NOT_REPORTED
    assert report.body_confidence == pytest.approx(0.4)  # numeric strings are numbers
    assert report.pitch_variety == NOT_REPORTED


# --- normalisation, ordering, parallel arrays -----------------------------------

def test_percentages_become_fractions_and_stay_clamped():
    report = person_report(_cycle(
        facial_metrics={"positivity": 75.0},
        verbal_metrics={"warmth": 100.0},
        tone_metrics={"pitch_variety": 250.0},      # out of contract, clamped not trusted
        body_language_metrics={"engagement": 62.5, "confidence": -30.0},
    ))
    assert report.face_positivity == pytest.approx(0.75)
    assert report.voice_warmth == pytest.approx(1.0)
    assert report.pitch_variety == pytest.approx(1.0)
    assert report.body_engagement == pytest.approx(0.625)
    assert report.body_confidence == pytest.approx(0.0)


def test_the_body_scores_are_read_from_whichever_place_the_shape_puts_them():
    """Live puts them in `body_language_metrics`; some envelopes inline them instead."""

    inline = person_report(_cycle(body_language={"engagement_score": 80.0,
                                                 "confidence_score": 40.0}))
    assert (inline.body_engagement, inline.body_confidence) == (pytest.approx(0.8),
                                                                pytest.approx(0.4))

    # ...and the metrics dict wins when both are present (it is the live contract's own).
    both = person_report(_cycle(body_language={"engagement_score": 80.0},
                                body_language_metrics={"engagement": 20.0}))
    assert both.body_engagement == pytest.approx(0.2)


def test_top_styles_are_ordered_by_value_then_name_so_a_near_tie_cannot_reshuffle():
    """Live is a DENSE distribution: ties happen every cycle, and value-only sorting
    leaves the order at the mercy of dict insertion, which reads downstream as the
    learner switching style on rounding noise."""

    styles = {"paced": 9.0, "mirroring": 9.0, "abrupt": 12.0, "quiet": 3.0,
              "still": 9.0, "warm": 1.0, "curious": 5.0}
    report = person_report(_cycle(communication_styles=styles))

    assert report.style_names[0] == "abrupt"
    assert report.style_names[1:4] == ["mirroring", "paced", "still"]  # tie -> by name
    assert report.style_values == sorted(report.style_values, reverse=True)

    # Same input, different insertion order -> same answer.
    shuffled = dict(reversed(list(styles.items())))
    assert person_report(_cycle(communication_styles=shuffled)).style_names \
        == report.style_names


def test_the_style_arrays_are_parallel_capped_and_fractional():
    report = person_report(_first("live"))  # dense: all 31 styles arrive
    assert len(report.style_names) == len(report.style_values) == 5
    assert all(0.0 < value <= 1.0 for value in report.style_values)

    empty = person_report(_cycle())
    assert empty.style_names == [] and empty.style_values == []


def test_top_n_is_honoured_and_a_nonsense_n_yields_nothing_rather_than_everything():
    dense = _first("live")
    assert len(person_report(dense, top_n=3).style_names) == 3
    assert person_report(dense, top_n=0).style_names == []
    assert person_report(dense, top_n=-4).style_names == []


def test_the_categorical_body_uses_the_canonical_vocabulary():
    """Folded by `normalize_body`, the same fold `expression._energy` applies, so the
    report and the affect estimate cannot disagree about what "medium" means."""

    report = person_report(_cycle(body_language={
        "posture": "leaning_forward", "head_position": "Straight",
        "gesture_type": "OPEN_PALM", "overall_energy": "medium"}))
    assert report.posture == "lean_forward"
    assert report.head_position == "upright"
    assert report.gesture_type == "open_palm"
    assert report.overall_energy == "moderate"


# --- language -------------------------------------------------------------------

def test_language_is_supplied_by_the_caller_and_never_invented():
    """The live envelope carries `lang` at the top level, where `cycle_from_envelope`
    does not keep it -- so the bridge passes what it learned from the speech lane, and
    an unknown language stays empty rather than defaulting to English."""

    assert person_report(_cycle()).lang == ""
    assert person_report(_cycle(), lang="CA").lang == "ca"
    assert person_report(_cycle(meta={"language": "es"})).lang == "es"
    # An explicit caller value beats the mock's meta.
    assert person_report(_cycle(meta={"language": "es"}), lang="en").lang == "en"


# --- the expression table --------------------------------------------------------

def test_the_expression_constant_table_covers_every_label_we_can_emit():
    """The context node maps these onto hri_msgs class constants for the releases that
    declare `Expression.expression` as a uint8. A label missing from the table is a
    label the robot silently never shows."""

    labels = set(typing.get_args(ExpressionLabel))
    assert set(EXPRESSION_CONSTANT_NAMES) == labels
    assert all(name == name.upper() for name in EXPRESSION_CONSTANT_NAMES.values())
