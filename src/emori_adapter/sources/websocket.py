"""Live Pokamind source: parse the staged-contract envelope from the relay WebSocket.

THE CONTRACT (see docs/pokamind-integration.md):

    Pokamind iframe --postMessage--> host /session page --POST /api/cycle--> hostapp
                                                  hostapp --/ws/relay--> THIS source

The hostapp is the producer: it stamps the active session's identity onto every
envelope and broadcasts it on ``/ws/relay`` (see ``emori_adapter.hostapp.relay``).

Live envelope shape (one per ~2 s cycle) — note how THIN it is; there are NO
facial/tone/verbal metric dicts, no prosody, no transcript, no meta::

    {
      "type": "pokamind_analysis_update",
      "communication_styles": {"direct": 3.2, ...},   # all 31 styles, 0..100, sum~100
      "body_language": {"posture": "leaning_forward", "head_position": "straight",
                        "gesture_type": "open_palm", "overall_energy": "medium",
                        "engagement_score": 72, "confidence_score": 65},
      "body_language_metrics": {"engagement": 72, "confidence": 65},
      "seq": 12,
      "timestamp": "2026-06-11T10:30:24.000Z",
      # added by the hostapp before fan-out:
      "session_id": "anna-001-20260611-103004", "person_id": "anna-001", "lang": "en"
    }

CRITICAL: whenever Pokamind's body analysis fails (learner looks away), the server
sends an empty body result and every body value arrives as ``null`` — the parser must drop
nulls, never raise (a raise would 500 /api/cycle and silently freeze the robot).

A ``{"type": "pokamind_session_completed", ...}`` message ends the stream.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Union

from emori_adapter.models import AnalysisCycle
from emori_adapter.sources.base import SessionSource

log = logging.getLogger("emori.source.pokamind")

ANALYSIS_EVENT = "pokamind_analysis_update"
COMPLETED_EVENT = "pokamind_session_completed"
AI_UTTERANCE_EVENT = "pokamind_ai_utterance"
# Turn-taking control events (emori-gated; emitted by the learner session page).
# The kiosk relays each as a pokamind_* postMessage to POST /api/cycle, where
# LiveDriver.on_tts_ended / on_turn_armed / on_turn_submitted / on_turn_rejected
# bump counters and feed the ConversationWatchdog (see hostapp/driver.py).
TTS_ENDED_EVENT = "pokamind_tts_ended"
TURN_ARMED_EVENT = "pokamind_turn_armed"
TURN_SUBMITTED_EVENT = "pokamind_turn_submitted"
TURN_REJECTED_EVENT = "pokamind_turn_rejected"
# Throttled speaking-onset relay from the kiosk page's in-iframe endpointer VAD
# (see hostapp/pages.py `pokamind_emori_vad` branch). Feeds the watchdog's stall
# clock so a long, continuously-spoken answer is never force-ended mid-turn.
USER_SPEAKING_EVENT = "pokamind_user_speaking"

# Envelope sections that are {str: float} metric dicts.
_METRIC_SECTIONS = (
    "communication_styles",
    "facial_metrics",
    "tone_metrics",
    "body_language_metrics",
    "verbal_metrics",
    "prosody",
)


def _clean_metrics(raw: Any) -> dict[str, float]:
    """Keep only numeric (or numeric-string) values; drop nulls and junk."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if v is None:
            continue
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _clean_body(raw: Any) -> dict[str, Any]:
    """Categorical body fields: drop nulls, keep strings/numbers as-is."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if v is not None}


def sanitize_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a null-safe copy of a Pokamind envelope (input is not mutated).

    Pokamind sends ``null`` for every body value when body analysis fails on a
    frame; pydantic's ``dict[str, float]`` rejects None, so nulls are dropped
    here — a no-face frame degrades to a styles-only cycle instead of crashing.
    """
    out = dict(payload)
    for section in _METRIC_SECTIONS:
        if section in out:
            out[section] = _clean_metrics(out[section])
    if "body_language" in out:
        out["body_language"] = _clean_body(out["body_language"])
    if not isinstance(out.get("meta"), dict):
        out.pop("meta", None)
    return out


def cycle_from_envelope(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    person_id: str | None = None,
    default_session: str = "live-session",
    default_person: str = "learner_01",
) -> AnalysisCycle:
    """Parse a Pokamind analysis-update envelope into an :class:`AnalysisCycle`.

    ``session_id``/``person_id`` (the hostapp's stamped identity) take precedence
    over anything in the payload — live envelopes carry no identity of their own,
    and a stale payload id must never override the active session.
    """
    p = sanitize_envelope(payload)
    try:
        seq = int(p.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return AnalysisCycle(
        timestamp=str(p.get("timestamp") or ""),
        session_id=session_id or str(p.get("session_id") or default_session),
        person_id=person_id or str(p.get("person_id") or default_person),
        seq=seq,
        communication_styles=p.get("communication_styles", {}),
        facial_metrics=p.get("facial_metrics", {}),
        tone_metrics=p.get("tone_metrics", {}),
        body_language_metrics=p.get("body_language_metrics", {}),
        verbal_metrics=p.get("verbal_metrics", {}),
        body_language=p.get("body_language", {}),
        prosody=p.get("prosody", {}),
        transcript_delta=str(p.get("transcript_delta") or ""),
        meta=p.get("meta", {}) if isinstance(p.get("meta"), dict) else {},
    )


@dataclass(frozen=True)
class SpeechEvent:
    """ONE sentence of assistant speech off the relay (or its end-of-turn marker).

    The per-CYCLE stream (``AnalysisCycle``) says what the learner is doing; this says
    what the robot's own voice is doing right now, which is what a per-sentence
    expression lane needs. ``ended`` is the ``pokamind_tts_ended`` marker: the AI
    stopped speaking, text is empty and nothing else on the record is meaningful.

    ``est_duration`` is the session's playback estimate (333 ms/word over the
    cue-stripped text, clamped), so a consumer can bound a plan to the voice without
    importing the planner.
    """

    session_id: str = ""
    person_id: str = ""
    seq: int = 0
    text: str = ""
    cue_category: str = ""
    cue_body: str = ""
    lang: str = ""
    source: str = ""
    est_duration: float = 0.0
    timestamp: str = ""
    ended: bool = False
    # The family Pokamind forwarded. ``cue_category`` is the one the robot performed.
    cue_category_authored: str = ""


# What `events()` yields: the per-cycle analysis, or one sentence of AI speech.
RelayEvent = Union[AnalysisCycle, SpeechEvent]


def speech_event_from_envelope(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    person_id: str | None = None,
    default_session: str = "live-session",
    default_person: str = "learner_01",
    ended: bool = False,
) -> SpeechEvent:
    """Parse a ``pokamind_ai_utterance`` / ``pokamind_tts_ended`` envelope.

    Cue fields are bounded exactly as ``LiveDriver`` bounds them: they arrive from a
    cross-origin postMessage relayed verbatim, and nothing legitimate is long.
    """

    def _text(key: str, limit: int = _CUE_FIELD_MAX) -> str:
        value = payload.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    try:
        seq = int(payload.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    text = "" if ended else _text("text", _SPEECH_TEXT_MAX)
    return SpeechEvent(
        session_id=session_id or str(payload.get("session_id") or default_session),
        person_id=person_id or str(payload.get("person_id") or default_person),
        seq=seq,
        text=text,
        cue_category="" if ended else _text("cue_category"),
        cue_body="" if ended else _text("cue_body"),
        lang=_text("lang", 16),
        source=_text("source", 32),
        est_duration=0.0 if ended else _sentence_duration_s(text),
        timestamp=str(payload.get("timestamp") or ""),
        ended=ended,
        cue_category_authored="" if ended else _text("cue_category_authored"),
    )


def event_from_envelope(
    payload: dict[str, Any],
    *,
    default_session: str = "live-session",
    default_person: str = "learner_01",
) -> RelayEvent | None:
    """One relay envelope as the record a ROS consumer reads, or ``None`` if it reads none.

    The envelope types on the wire that are not named here (the turn-taking events, the
    completion marker) carry nothing this record has a field for. Whoever reads the stream
    decides what a completion means to it: over a socket it ends the stream, and a consumer
    fed in-process has no stream to end.
    """

    etype = payload.get("type")
    if etype == ANALYSIS_EVENT:
        return cycle_from_envelope(
            payload, default_session=default_session, default_person=default_person)
    if etype in (AI_UTTERANCE_EVENT, TTS_ENDED_EVENT):
        return speech_event_from_envelope(
            payload, default_session=default_session, default_person=default_person,
            ended=(etype == TTS_ENDED_EVENT))
    return None


# Mirrors LiveDriver._CUE_FIELD_MAX / behavior.cues: one place each bound is stated.
_CUE_FIELD_MAX = 200
_SPEECH_TEXT_MAX = 2000


def _sentence_duration_s(text: str) -> float:
    """The session's playback estimate, imported lazily so this module stays dependency-light."""

    from emori_adapter.behavior.cues import sentence_duration_s

    return sentence_duration_s(text) if text else 0.0


class PokamindBridgeSource(SessionSource):
    """Read per-cycle envelopes from the hostapp relay (``/ws/relay``).

    Reconnects with backoff on connection loss — one WiFi blip must not end the
    robot's reactivity. The stream ends cleanly only on ``pokamind_session_completed``
    or after ``max_retries`` consecutive failed reconnects.
    """

    def __init__(
        self,
        ws_url: str,
        *,
        session_id: str = "live-session",
        person_id: str = "learner_01",
        max_retries: int = 10,
        backoff_s: float = 2.0,
    ) -> None:
        self.ws_url = ws_url
        self.session_id = session_id
        self.person_id = person_id
        self.max_retries = max_retries
        self.backoff_s = backoff_s

    def cycles(self) -> Iterator[AnalysisCycle]:
        """The ``SessionSource`` contract: per-cycle analysis only."""

        for event in self.events():
            if isinstance(event, AnalysisCycle):
                yield event

    def events(self) -> Iterator[RelayEvent]:
        """Every relay event a ROS consumer needs: cycles AND per-sentence speech.

        One socket, one reconnect loop, two kinds of record - because the two streams
        are interleaved on the wire (a sentence's cue arrives ~0.3 s after a cycle) and
        reading them over two connections would put them out of order for no reason.
        """

        try:
            from websockets.sync.client import connect
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("websockets is required for PokamindBridgeSource") from e

        retries = 0
        while True:
            try:
                log.info("connecting to Pokamind relay at %s", self.ws_url)
                with connect(self.ws_url) as ws:
                    retries = 0  # connected: reset the retry budget
                    for message in ws:
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            log.debug("ignoring non-JSON relay message")
                            continue
                        if payload.get("type") == COMPLETED_EVENT:
                            log.info("session completed; ending stream")
                            return
                        event = event_from_envelope(
                            payload,
                            default_session=self.session_id,
                            default_person=self.person_id,
                        )
                        if event is not None:
                            yield event
                # server closed without a completed event -> treat as a drop, reconnect
                log.warning("relay closed without session completion; reconnecting")
            except Exception as e:  # noqa: BLE001 - any transport error -> retry
                log.warning("relay connection lost (%s)", e)
            retries += 1
            if retries > self.max_retries:
                log.error("relay unreachable after %d retries; giving up", self.max_retries)
                return
            time.sleep(min(30.0, self.backoff_s * retries))
