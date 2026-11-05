"""The person report: what Pokamind actually OBSERVED this cycle — absence included.

:class:`~emori_adapter.translation.Translator` produces the *derived* half of the
signal (engagement, affect, context) — our reading of the learner. This module
produces the other half: the raw observation record, in Pokamind's own vocabulary,
for the ROS4HRI consumers that need to reason about what was measured rather than
about what we concluded from it.

One rule shapes everything here: **absence is a value**. On the LIVE wire only two
channels are reliably present — the 31 communication styles and ``body_language``.
The facial / tone /
verbal metric dicts exist only in the rich mock and the CSV replay, and a lost face
turns every body value into ``null``, which ``sanitize_envelope`` drops — leaving a
styles-only cycle.

A ``face_positivity`` of ``0.0`` is a miserable learner. A ``face_positivity`` that
never arrived is an ordinary Tuesday with the camera looking at a shoulder. Emitting
the second as the first is how a KPI gets poisoned by a channel nobody ever measured,
so every float here stays at :data:`NOT_REPORTED` until a real number arrives, and
:class:`PersonReportFlags` records which channels spoke at all — enough for a
publisher to *withhold* a topic instead of inventing a zero for it.

The module is pure (cycle in, report out, no clock, no I/O) so the ROS nodes that
serialise it are a field assignment each, and every rule above is testable on a dev
box with no ROS installed.
"""

from __future__ import annotations

from enum import IntFlag

from pydantic import BaseModel, Field, model_validator

from emori_adapter.constants import normalize_body
from emori_adapter.models import AnalysisCycle
from emori_adapter.translation.styles import normalize_styles

# The sentinel, in one place. Negative on purpose: every real channel here is a 0..1
# fraction or a non-negative age, so no honest measurement can ever collide with it,
# and a consumer that forgets to check gets an obviously-wrong number rather than a
# plausible one. (`Context.msg` restates it in a comment on every float32 it guards.)
NOT_REPORTED = -1.0


class PersonReportFlags(IntFlag):
    """Which Pokamind channels reported anything at all this cycle.

    Serialised as ``Context.report_flags`` (uint8). ``0`` is the spec's "degraded
    status": the cycle carried no observation of the person whatsoever.
    """

    BODY = 1
    FACE = 2
    TONE = 4
    VERBAL = 8
    STYLES = 16


class PersonReport(BaseModel):
    """One cycle of raw observation, with every unreported channel saying so."""

    # Categorical body language, folded onto the canonical vocabulary by
    # `normalize_body` — the same fold `expression._energy` applies, so the report
    # and the affect estimate can never disagree about what "medium" means.
    posture: str = ""
    head_position: str = ""
    gesture_type: str = ""
    overall_energy: str = ""

    # Numeric body, 0..100 on the wire -> 0..1 here.
    body_engagement: float = NOT_REPORTED
    body_confidence: float = NOT_REPORTED

    # The richer channels: present on the mock/replay shape, absent live.
    face_positivity: float = NOT_REPORTED
    voice_warmth: float = NOT_REPORTED
    pitch_variety: float = NOT_REPORTED

    # Style distribution: top-N by value, descending, ties broken by name.
    style_names: list[str] = Field(default_factory=list)
    style_values: list[float] = Field(default_factory=list)  # 0..1, parallel to names

    flags: int = 0  # PersonReportFlags bitmask
    lang: str = ""

    @model_validator(mode="after")
    def _style_arrays_stay_parallel(self) -> "PersonReport":
        """Two arrays indexed against each other must be built as one thing.

        They land in a ROS message as ``string[]`` / ``float32[]``; a length skew is
        not a crash but a subscriber quietly attributing one learner's number to
        another learner's style name.
        """

        if len(self.style_names) != len(self.style_values):
            raise ValueError("style_names and style_values must be parallel")
        return self


# label -> the hri_msgs/Expression class-constant name, for the releases that declare
# the field as a uint8 with constants instead of a plain string. It lives here, in the
# pure core, so the ROS node's introspection helper is unit-testable with no ROS
# installed — and so the table can be checked against `ExpressionLabel` itself.
EXPRESSION_CONSTANT_NAMES: dict[str, str] = {
    "neutral": "NEUTRAL",
    "happy": "HAPPY",
    "sad": "SAD",
    "angry": "ANGRY",
    "surprised": "SURPRISED",
    "scared": "SCARED",
    "disgusted": "DISGUSTED",
}


def _fraction(metrics: dict, key: str) -> float:
    """A 0..100 metric as a 0..1 fraction, or ``NOT_REPORTED`` if it never arrived.

    Deliberately without a default: `expression._metric` defaults a missing key to 50
    because it is computing a blend and needs *some* number. This function is
    computing a record, and a record must not contain a number nobody measured.
    """

    if not isinstance(metrics, dict) or key not in metrics:
        return NOT_REPORTED
    try:
        value = float(metrics[key])
    except (TypeError, ValueError):
        return NOT_REPORTED
    if value != value:  # NaN: reported, but not a measurement
        return NOT_REPORTED
    return max(0.0, min(1.0, value / 100.0))


def _category(body: dict, key: str) -> str:
    """A categorical body field as a lowercase token, "" when absent or non-textual."""

    value = body.get(key)
    return value.strip().lower() if isinstance(value, str) else ""


def person_report(cycle: AnalysisCycle, *, lang: str = "", top_n: int = 5) -> PersonReport:
    """Build the observation record for one analysis cycle.

    ``lang`` is passed in rather than read from the cycle because the live envelope
    carries the kiosk language at the top level, where ``cycle_from_envelope`` does
    not keep it; the caller that knows it (the ROS bridge, from the speech lane)
    supplies it, and the rich mock's ``meta`` covers the offline shapes.
    """

    styles = normalize_styles(cycle.communication_styles)
    body = normalize_body(cycle.body_language)

    flags = PersonReportFlags(0)
    if styles:
        flags |= PersonReportFlags.STYLES
    # BODY means "the body channel said something", categorical or numeric: a cycle
    # that names a posture but scores nothing still saw the person.
    if body or cycle.body_language_metrics:
        flags |= PersonReportFlags.BODY
    if cycle.facial_metrics:
        flags |= PersonReportFlags.FACE
    if cycle.tone_metrics:
        flags |= PersonReportFlags.TONE
    if cycle.verbal_metrics:
        flags |= PersonReportFlags.VERBAL

    # The two body scores arrive under either name depending on the shape:
    # `body_language_metrics` on the live contract, `body_language.*_score` inline.
    body_engagement = _fraction(cycle.body_language_metrics, "engagement")
    if body_engagement == NOT_REPORTED:
        body_engagement = _fraction(body, "engagement_score")
    body_confidence = _fraction(cycle.body_language_metrics, "confidence")
    if body_confidence == NOT_REPORTED:
        body_confidence = _fraction(body, "confidence_score")

    # Deterministic on purpose: a dense live distribution has near-ties every cycle,
    # and a top-5 list that reshuffles on rounding noise reads downstream as the
    # learner changing style. -value, +name gives one stable order for one input.
    ranked = sorted(
        ((name, value) for name, value in styles.items() if value > 0.0),
        key=lambda item: (-item[1], item[0]),
    )[: max(0, top_n)]

    meta = cycle.meta if isinstance(cycle.meta, dict) else {}
    language = str(lang or meta.get("language") or meta.get("lang") or "").strip().lower()

    return PersonReport(
        posture=_category(body, "posture"),
        head_position=_category(body, "head_position"),
        gesture_type=_category(body, "gesture_type"),
        overall_energy=_category(body, "overall_energy"),
        body_engagement=body_engagement,
        body_confidence=body_confidence,
        face_positivity=_fraction(cycle.facial_metrics, "positivity"),
        voice_warmth=_fraction(cycle.verbal_metrics, "warmth"),
        pitch_variety=_fraction(cycle.tone_metrics, "pitch_variety"),
        style_names=[name for name, _ in ranked],
        style_values=[round(value, 4) for _, value in ranked],
        flags=int(flags),
        lang=language[:16],
    )


__all__ = [
    "EXPRESSION_CONSTANT_NAMES",
    "NOT_REPORTED",
    "PersonReport",
    "PersonReportFlags",
    "person_report",
]
