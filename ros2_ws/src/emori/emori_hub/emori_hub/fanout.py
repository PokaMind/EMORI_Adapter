"""The session's envelopes, fanned out to the graph on a thread of their own.

Every stamped ``pokamind_*`` envelope the hostapp relays goes through
:meth:`RelayHub.publish`, on the request thread of ``POST /api/cycle``. That thread is the
one a session's motion runs on, so what this module costs it is the whole of the design:

* :meth:`FanOut.offer` is the relay's tap. It takes a short lock that nothing but other
  callers of ``offer`` and ``note`` ever hold, numbers the envelope, and does one
  ``put_nowait``. It parses nothing, logs nothing, waits for nothing and calls into no sink.
  A full queue drops the envelope and counts it. A consumer that is stuck, or gone, changes
  the cost of ``offer`` by nothing.
* Everything else happens on the ``ros-fanout`` thread: parsing, the privacy gate, the
  :class:`SessionEvent` and the in-process feed to the adapter node.

**Loss.** The queue holds :data:`QUEUE_SIZE` envelopes, the relay's own bound. The number is
given when an envelope is offered and not when it is read, so an envelope that was dropped
still used a number, and :attr:`SessionEvent.seq` jumps over it. A gap in ``seq`` is how a
subscriber sees a loss, and :meth:`FanOut.stats` counts it from this side. An envelope that
cannot be read is not skipped either: it is published as a ``hub_envelope_unreadable`` event,
so a gap always means a loss and never a bug.

**What leaves the process.** A :class:`SessionEvent` carries typed fields only. Identifiers
pass the privacy gate (``EMORI_ROS_DEIDENTIFY``) and the robot's sentence passes it
(``EMORI_ROS_SPEECH_TEXT``), both the same gate, and the same salt, that the adapter node's
messages pass. The envelope as received is never published unless ``EMORI_ROS_RAW_ENVELOPES``
is on, and then it is published through the same gate: identifiers replaced, and the
sentence, the cue direction and the learner's transcript removed unless the speech gate is
open. The adapter node is fed the envelope in this process, and it applies its own gate on
what it publishes.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "AI_UTTERANCE_TYPE",
    "COMPLETED_TYPE",
    "FanOut",
    "QUEUE_SIZE",
    "RAW_ENVELOPES_VAR",
    "RAW_LIMIT_CHARS",
    "SessionEvent",
    "UNREADABLE_TYPE",
    "build_fanout",
    "event_from_envelope",
    "fill_message",
    "load_privacy",
    "raw_envelopes_enabled",
]

log = logging.getLogger("emori.hub.fanout")

#: The relay's own bound (``hostapp/relay.py``): behind by this many, the oldest is not kept.
QUEUE_SIZE = 256
RAW_ENVELOPES_VAR = "EMORI_ROS_RAW_ENVELOPES"
#: A raw envelope longer than this is withheld rather than put on the wire whole.
RAW_LIMIT_CHARS = 65536

#: The two envelope types this module reads fields of. They are the adapter core's
#: constants, spelled out so that loading this module needs no core; a test holds them equal.
AI_UTTERANCE_TYPE = "pokamind_ai_utterance"
COMPLETED_TYPE = "pokamind_session_completed"
#: What stands for an envelope that could not be read, so that its number is still used.
UNREADABLE_TYPE = "hub_envelope_unreadable"
#: Types the hub itself puts on the same queue.
_HUB_PREFIX = "hub_"

_TYPE_MAX = 64
_ID_MAX = 200
_LANG_MAX = 16
_SOURCE_MAX = 32
_STATUS_MAX = 32
_CUE_FIELD_MAX = 200
_TEXT_MAX = 2000

#: Taken out of a raw envelope unless the speech gate is open: the robot's sentence, the
#: direction written about it, and what the learner said.
_TEXT_KEYS = ("text", "cue_body", "transcript_delta")

#: A ``[family| direction]`` cue that somehow survived upstream stripping.
_CUE_BRACKETS = re.compile(r"\[[^\]]*\]")

#: How long the consumer waits for work, which is also how soon a stop is noticed.
_POLL_S = 0.25
#: A repeated complaint is logged at most this often, with a count of the ones left out.
_COMPLAINT_PERIOD_S = 30.0
#: Dropped envelopes are reported at most this often, by the consumer.
_DROP_REPORT_PERIOD_S = 5.0

_TRUE = frozenset({"1", "true", "yes", "on"})


# --- the event -------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvent:
    """``emori_msgs/SessionEvent`` without ROS. ``stamp`` and ``source_time`` are epoch seconds."""

    seq: int
    stamp: float
    type: str
    session_id: str = ""
    person_id: str = ""
    lang: str = ""
    source_time: float = 0.0
    text: str = ""
    cue_category: str = ""
    cue_family: str = ""
    cue_family_source: str = ""
    cue_slug: str = ""
    cue_recorded: str = ""
    cue_budget_s: float = 0.0
    est_duration: float = 0.0
    source: str = ""
    has_results: bool = False
    status: str = ""
    payload_json: str = ""


def fill_message(message: Any, event: SessionEvent) -> Any:
    """Copy ``event`` onto a ``SessionEvent`` message and return it."""

    seconds = int(event.stamp)
    message.header.stamp.sec = seconds
    message.header.stamp.nanosec = int((event.stamp - seconds) * 1e9)
    message.seq = event.seq & 0xFFFFFFFF
    message.type = event.type
    message.session_id = event.session_id
    message.person_id = event.person_id
    message.lang = event.lang
    source_seconds = int(event.source_time)
    message.source_time.sec = source_seconds
    message.source_time.nanosec = int(round((event.source_time - source_seconds) * 1e9))
    message.text = event.text
    message.cue_category = event.cue_category
    message.cue_family = event.cue_family
    message.cue_family_source = event.cue_family_source
    message.cue_slug = event.cue_slug
    message.cue_recorded = event.cue_recorded
    message.cue_budget_s = float(event.cue_budget_s)
    message.est_duration = float(event.est_duration)
    message.source = event.source
    message.has_results = bool(event.has_results)
    message.status = event.status
    message.payload_json = event.payload_json
    return message


# --- the privacy gate and the mapping ---------------------------------------------------


def load_privacy(environ: Optional[Mapping[str, str]] = None) -> Any:
    """The adapter node's gate, read from the environment.

    The same class the adapter node uses, so that one switch and one salt govern every
    message of the process. Without it there is no start: a missing gate must never read
    as an open one.
    """

    from emori_adapter_ros.privacy import Privacy

    return Privacy.from_env(environ)


def raw_envelopes_enabled(environ: Mapping[str, str]) -> bool:
    """Only a spelled-out yes opens it, so a typo cannot."""

    return str(environ.get(RAW_ENVELOPES_VAR, "")).strip().lower() in _TRUE


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _seconds(value: Any) -> float:
    """An envelope's own clock as epoch seconds; 0.0 when it carries none."""

    text = _text(value, 64)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _bounded_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0.0 else 0.0


def _default_duration(text: str) -> float:
    from emori_adapter.behavior.cues import sentence_duration_s

    return sentence_duration_s(text) if text else 0.0


def _default_has_results(results: Any) -> bool:
    """The question the driver settles a session's status on, answered by the same function."""

    try:
        from emori_adapter.hostapp.metrics_store import results_recorded
    except ImportError:
        return bool(results)
    return results_recorded(results)


def _gated_raw(envelope: Mapping[str, Any], privacy: Any) -> str:
    """The envelope with the gate applied, as JSON; ``""`` if it is too long to send whole."""

    gated = dict(envelope)
    person, session = privacy.apply_ids(_text(envelope.get("person_id"), _ID_MAX),
                                        _text(envelope.get("session_id"), _ID_MAX))
    if "person_id" in gated:
        gated["person_id"] = person
    if "session_id" in gated:
        gated["session_id"] = session
    if not privacy.speech_text:
        for key in _TEXT_KEYS:
            gated.pop(key, None)
    try:
        encoded = json.dumps(gated, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return encoded if len(encoded) <= RAW_LIMIT_CHARS else ""


def event_from_envelope(
    envelope: Mapping[str, Any],
    *,
    seq: int,
    stamp: float,
    privacy: Any,
    raw: bool = False,
    duration_of: Optional[Callable[[str], float]] = None,
    has_results: Optional[Callable[[Any], bool]] = None,
) -> SessionEvent:
    """One envelope as the event that goes on the topic.

    ``privacy`` is the adapter node's gate. The sentence's duration is worked out from the
    sentence itself, so a closed speech gate withholds the words and not the length.
    """

    kind = _text(envelope.get("type"), _TYPE_MAX)
    if not kind:
        return unreadable_event(seq, stamp)
    person, session = privacy.apply_ids(_text(envelope.get("person_id"), _ID_MAX),
                                        _text(envelope.get("session_id"), _ID_MAX))
    event = SessionEvent(
        seq=seq, stamp=stamp, type=kind, session_id=session, person_id=person,
        lang=_text(envelope.get("lang"), _LANG_MAX),
        source_time=_seconds(envelope.get("timestamp")),
        status=_text(envelope.get("status"), _STATUS_MAX),
    )
    if kind == AI_UTTERANCE_TYPE:
        spoken = _CUE_BRACKETS.sub("", _text(envelope.get("text"), _TEXT_MAX)).strip()
        shown, _body = privacy.apply_speech(spoken, "")
        measure = duration_of if duration_of is not None else _default_duration
        event = replace(
            event,
            text=shown,
            est_duration=_bounded_float(measure(spoken)) if spoken else 0.0,
            cue_category=_text(envelope.get("cue_category"), _CUE_FIELD_MAX),
            cue_family=_text(envelope.get("cue_family"), _CUE_FIELD_MAX),
            cue_family_source=_text(envelope.get("cue_family_source"), _CUE_FIELD_MAX),
            cue_slug=_text(envelope.get("cue_slug"), _CUE_FIELD_MAX),
            cue_recorded=_text(envelope.get("cue_recorded"), _CUE_FIELD_MAX),
            cue_budget_s=_bounded_float(envelope.get("cue_budget_s")),
            source=_text(envelope.get("source"), _SOURCE_MAX),
        )
    elif kind == COMPLETED_TYPE:
        decide = has_results if has_results is not None else _default_has_results
        event = replace(event, has_results=bool(decide(envelope.get("results"))))
    if raw:
        event = replace(event, payload_json=_gated_raw(envelope, privacy))
    return event


def unreadable_event(seq: int, stamp: float) -> SessionEvent:
    return SessionEvent(seq=seq, stamp=stamp, type=UNREADABLE_TYPE)


# --- the fan-out ------------------------------------------------------------------------


class _Stop:
    """What ends the consumer when it is put on the queue."""


_STOP = _Stop()


class FanOut:
    """The queue, the thread behind it, and the tap in front of it.

    ``publish`` takes a :class:`SessionEvent` and ``feed`` takes the parsed envelope. Both
    run on the ``ros-fanout`` thread only, one at a time, in order. A sink that raises costs
    the one envelope, is counted, and the next one still runs.
    """

    def __init__(
        self,
        *,
        publish: Callable[[SessionEvent], Any],
        privacy: Any,
        feed: Optional[Callable[[dict], Any]] = None,
        raw: bool = False,
        queue_size: int = QUEUE_SIZE,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        duration_of: Optional[Callable[[str], float]] = None,
        has_results: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self._publish = publish
        self._privacy = privacy
        self._feed = feed
        self._raw = raw
        self._clock = clock
        self._monotonic = monotonic
        self._duration_of = duration_of
        self._has_results = has_results
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.capacity = queue_size
        # Held by offer() and note() and by nothing else, and for the length of a queue put.
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._offered = 0
        self._dropped = 0
        self._refused = 0
        # Written by the consumer only.
        self._published = 0
        self._fed = 0
        self._publish_errors = 0
        self._feed_errors = 0
        self._mapping_errors = 0
        self._unreadable = 0
        self._raw_withheld = 0
        self._abandoned = 0
        self._busy_since: Optional[float] = None
        self._reported_drops = 0
        self._last_drop_report = float("-inf")
        self._complained: dict[str, float] = {}
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tap: Optional[Callable[[str], Any]] = None
        self._relay: Any = None
        self._previous_tap: Optional[Callable[[str], Any]] = None

    # --- the producer side: every line of it runs on a caller's thread ------------------

    def offer(self, message: str) -> bool:
        """The relay's tap: queue one envelope as the relay received it, or count it dropped."""

        return self._enqueue(message)

    def note(self, kind: str, *, session_id: str = "", person_id: str = "", lang: str = "",
             status: str = "") -> bool:
        """Queue an event the hub itself raised, such as ``hub_session_finalized``.

        It is numbered with the envelopes, so the order a subscriber sees is the order of
        offers. Anything not named ``hub_*`` is refused: ``pokamind_*`` types are the
        relay's.
        """

        if not isinstance(kind, str) or not kind.startswith(_HUB_PREFIX):
            return False
        return self._enqueue({"type": kind, "session_id": session_id, "person_id": person_id,
                              "lang": lang, "status": status})

    def _enqueue(self, payload: Any) -> bool:
        stamp = self._clock()
        with self._lock:
            if self._closed:
                self._refused += 1
                return False
            self._seq += 1
            try:
                self._queue.put_nowait((self._seq, stamp, payload))
            except queue.Full:
                self._dropped += 1
                return False
            self._offered += 1
        return True

    # --- hanging it off the relay -------------------------------------------------------

    def install(self, relay: Any) -> None:
        """Make this the relay's tap, ahead of any tap already there, which still runs."""

        previous = relay.tap

        if previous is None:
            tap: Callable[[str], Any] = self.offer
        else:
            def tap(message: str) -> Any:
                self.offer(message)
                return previous(message)

        self._relay, self._previous_tap, self._tap = relay, previous, tap
        relay.tap = tap

    def uninstall(self) -> None:
        """Put back what the relay had, unless something else has taken the tap since."""

        relay, self._relay = self._relay, None
        if relay is not None and relay.tap is self._tap:
            relay.tap = self._previous_tap
        self._tap = self._previous_tap = None

    # --- the consumer side --------------------------------------------------------------

    def attach_feed(self, feed: Optional[Callable[[dict], Any]]) -> None:
        """Set who is handed the parsed envelopes, or nobody, from the next envelope on.

        The adapter node starts after this queue and stops before it. One attribute write,
        which the consumer reads once per envelope: the tap never sees it, and neither a
        slow feed nor a missing one changes what ``offer`` costs.
        """

        self._feed = feed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ros-fanout", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> bool:
        """Refuse new envelopes, let the one in hand finish, and drop the rest.

        Returns False if the consumer was still inside a sink after ``timeout``. It is a
        daemon thread and goes with the process.
        """

        with self._lock:
            self._closed = True
        self._stopping.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass  # the consumer looks at the flag after every envelope
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            log.warning("the fan-out is still inside a sink after %g s; leaving it", timeout)
            return False
        return True

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_POLL_S)
            except queue.Empty:
                self._report_drops()
                if self._stopping.is_set():
                    return
                continue
            if item is _STOP:
                break
            self._handle(*item)
            self._report_drops()
            if self._stopping.is_set():
                break
        self._abandon_the_rest()
        self._report_drops()

    def _abandon_the_rest(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not _STOP:
                self._abandoned += 1

    def _handle(self, seq: int, stamp: float, payload: Any) -> None:
        self._busy_since = self._monotonic()
        try:
            from_the_relay = not isinstance(payload, dict)
            envelope = self._parse(payload) if from_the_relay else payload
            self._publish_event(seq, stamp, envelope)
            feed = self._feed
            if from_the_relay and envelope is not None and feed is not None:
                self._feed_envelope(feed, envelope)
        except Exception:  # noqa: BLE001 - one envelope must not end the thread
            self._complain("handle", "the fan-out could not handle an envelope")
        finally:
            self._busy_since = None

    def _parse(self, payload: Any) -> Optional[dict]:
        try:
            envelope = json.loads(payload)
        except (TypeError, ValueError):
            return None
        return envelope if isinstance(envelope, dict) else None

    def _publish_event(self, seq: int, stamp: float, envelope: Optional[dict]) -> None:
        event: SessionEvent
        if envelope is None:
            self._unreadable += 1
            event = unreadable_event(seq, stamp)
        else:
            try:
                event = event_from_envelope(
                    envelope, seq=seq, stamp=stamp, privacy=self._privacy, raw=self._raw,
                    duration_of=self._duration_of, has_results=self._has_results)
            except Exception:  # noqa: BLE001 - its number still has to be used
                self._mapping_errors += 1
                self._complain("mapping", "an envelope could not be mapped to an event")
                event = unreadable_event(seq, stamp)
            if event.type == UNREADABLE_TYPE:
                self._unreadable += 1
            if self._raw and not event.payload_json and event.type != UNREADABLE_TYPE:
                self._raw_withheld += 1
        try:
            self._publish(event)
        except Exception:  # noqa: BLE001 - the feed still gets its envelope
            self._publish_errors += 1
            self._complain("publish", "publishing a session event failed")
            return
        self._published += 1

    def _feed_envelope(self, feed: Callable[[dict], Any], envelope: dict) -> None:
        try:
            feed(envelope)
        except Exception:  # noqa: BLE001 - counted, and the next envelope still goes
            self._feed_errors += 1
            self._complain("feed", "feeding the adapter node failed")
            return
        self._fed += 1

    # --- saying what happened -----------------------------------------------------------

    def _complain(self, key: str, what: str) -> None:
        now = self._monotonic()
        if now - self._complained.get(key, float("-inf")) < _COMPLAINT_PERIOD_S:
            return
        self._complained[key] = now
        log.warning("%s; further ones of this kind are counted and not logged for %g s",
                    what, _COMPLAINT_PERIOD_S, exc_info=True)

    def _report_drops(self) -> None:
        """The consumer says what the tap could not, since the tap itself may not log."""

        dropped = self._dropped
        now = self._monotonic()
        if dropped == self._reported_drops or now - self._last_drop_report < _DROP_REPORT_PERIOD_S:
            return
        log.warning("the fan-out queue was full: %d envelopes dropped since the last report "
                    "(%d in all). The gap shows in SessionEvent.seq",
                    dropped - self._reported_drops, dropped)
        self._reported_drops, self._last_drop_report = dropped, now

    def stats(self) -> dict[str, Any]:
        """Counters for the status a person reads. Cheap and lock-free: a torn read costs one off."""

        busy = self._busy_since
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "capacity": self.capacity,
            "depth": self._queue.qsize(),
            "seq": self._seq,
            "offered": self._offered,
            "dropped": self._dropped,
            "refused_after_stop": self._refused,
            "published": self._published,
            "fed": self._fed,
            "unreadable": self._unreadable,
            "mapping_errors": self._mapping_errors,
            "publish_errors": self._publish_errors,
            "feed_errors": self._feed_errors,
            "raw_withheld": self._raw_withheld,
            "abandoned_at_stop": self._abandoned,
            "busy_for_s": None if busy is None else max(0.0, self._monotonic() - busy),
        }


def build_fanout(
    app: Any,
    *,
    publish: Callable[[SessionEvent], Any],
    feed: Optional[Callable[[dict], Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    privacy: Any = None,
) -> FanOut:
    """Start a fan-out and make it the app's relay tap; it is also ``app.state.fanout``.

    The owner of the ``SessionEvent`` publisher passes ``publish``, and calls ``stop()`` on
    the way out. A relay that already has a tap keeps it.
    """

    source = os.environ if environ is None else environ
    fanout = FanOut(publish=publish, feed=feed,
                    privacy=privacy if privacy is not None else load_privacy(source),
                    raw=raw_envelopes_enabled(source))
    fanout.start()
    fanout.install(app.state.hub)
    app.state.fanout = fanout
    return fanout
