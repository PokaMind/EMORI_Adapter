"""EMORI adapter node: session analysis -> ROS4HRI topics.

It drives a :class:`SessionSource` through the pure-Python
:class:`~emori_adapter.translation.Translator` and publishes, per ~2 s cycle:

* ``/humans/persons/tracked``                (hri_msgs/IdsList, latched)
* ``/humans/persons/<id>/anonymous``         (std_msgs/Bool, latched - REP-155 person well-formedness)
* ``/humans/persons/<id>/engagement_status`` (hri_msgs/EngagementLevel)
* ``/humans/persons/<id>/context``           (emori_msgs/Context - the full structured
  signal: the derived engagement/affect/context of v1, plus the v2 PERSON REPORT - what
  the session reported, with every channel it did not report carrying the -1.0
  sentinel rather than a zero that would read as a real measurement)

and, on the LIVE source only, per SENTENCE rather than per cycle:

* ``/humans/persons/<id>/speech_cue``        (emori_msgs/SpeechCue - what the robot's own
  voice is doing right now, republished at that sentence's playback onset, plus the
  end-of-turn marker). The mock arc has no voice, so it publishes none - a subscriber
  that sees no cue is a session with no speech, never a fabricated one.

Sources (the ``source`` parameter):

* ``mock``     - the deterministic engagement arc (the standalone demo).
* ``pokamind`` - LIVE data from the live relay of the robot hub: connects to the
  hostapp relay (``ws_url``, default
  ``ws://host.docker.internal:8080/ws/relay``) where ``/api/cycle`` broadcasts every
  stamped envelope of the real kiosk session. The reader thread reconnects across
  sessions, and a session-id change resets the stateful Translator.
* ``hub``      - the same envelopes with no socket: this node lives in the hub's process and
  is handed each one through :meth:`EmoriAdapterNode.feed`, on the hub's fan-out thread.
  There is no reader, no queue of its own and no timer, so nothing is sampled, and no gap
  opens between one session and the next. This node stays the privacy gate on that path:
  the envelope arrives whole, and only what passes the gate is published.

ROS person-id note: the robot serves ONE learner at a time, so the ROS4HRI topic
namespace uses the STABLE ``person_id`` parameter (default ``learner_01``) while the
TRUE per-session identity (roster student id + minted session id, stamped by the
hostapp) is carried inside ``emori_msgs/Context.person_id``/``session_id`` — the
FIWARE bridge keys its entities off those fields, and downstream ROS nodes keep a
stable topic graph.

Privacy (``privacy.py``, read once at start): those in-message identities are
PSEUDONYMISED by default and the robot's own speech text is WITHHELD by default,
because ``ROS_LOCALHOST_ONLY=1`` is the only other thing guarding this wire and it is a
network setting, not a data-protection control. Two consequences worth stating rather
than discovering:

* the topic NAMESPACE is untouched — ``/humans/persons/learner_01/...`` is a stable
  parameter, not an identity, and pseudonymising it would churn the graph every
  session for no privacy gain;
* ``fiware_bridge`` keys its NGSI-LD entities off the in-message ids, so with the
  default posture it historises pseudonyms. That is correct on the robot, where the
  bridge is off and the hostapp does the historisation with the real ids; a deployment
  that wants the bridge to write roster ids must set ``EMORI_ROS_DEIDENTIFY=0`` and
  own that decision.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from datetime import datetime

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from std_msgs.msg import Bool
from hri_msgs.msg import EngagementLevel, IdsList
from emori_msgs.msg import Context, SpeechCue

from emori_adapter.models import AnalysisCycle
from emori_adapter.sources.mock import MockPokamindSource
from emori_adapter.sources.websocket import COMPLETED_EVENT, SpeechEvent, event_from_envelope
from emori_adapter.translation import NOT_REPORTED, Translator, person_report

from emori_adapter_ros.privacy import Privacy

_LATCHED = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                      reliability=ReliabilityPolicy.RELIABLE)

# A `[family| direction]` cue that somehow survived upstream stripping.
_CUE_BRACKETS = re.compile(r"\[[^\]]*\]")

# The sources whose envelopes carry a real clock and a real session.
_LIVE_SOURCES = ("pokamind", "hub")


def _epoch_seconds(timestamp: str) -> float:
    """The ENVELOPE's own clock as epoch seconds; 0.0 when it carries none.

    0.0 is deliberate: a subscriber correlating against the upstream envelope's clock
    must be able to tell "no source time" from a plausible-looking one this node invented.
    """

    text = (timestamp or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _age_seconds(timestamp: str) -> float:
    """How stale this observation is, in seconds; ``NOT_REPORTED`` without a stamp.

    Measured against the WALL clock (``time.time()``), not the node clock: the number
    being subtracted is the upstream envelope's clock, and a node running on sim time
    would otherwise report an age drawn from two different timelines.

    A missing or unparseable stamp yields the sentinel rather than 0.0 — "arrived this
    instant" is exactly the wrong thing to tell a consumer that is deciding whether the
    person data is fresh enough to act on.
    """

    seconds = _epoch_seconds(timestamp)
    if seconds <= 0.0:
        return NOT_REPORTED
    return max(0.0, time.time() - seconds)


class EmoriAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("emori_adapter")
        self.declare_parameter("source", "mock")  # mock | pokamind | hub
        self.declare_parameter("ws_url", "ws://host.docker.internal:8080/ws/relay")
        self.declare_parameter("person_id", "learner_01")
        self.declare_parameter("session_id", "sim-session")
        self.declare_parameter("cycle_period_s", 2.0)
        self.declare_parameter("cycles_per_phase", 3)
        self.declare_parameter("frame_id", "camera")
        self.declare_parameter("loop", True)
        self.declare_parameter("mock_shape", "rich")  # rich | live

        self.source_kind = str(self.get_parameter("source").value)
        self.person_id = self.get_parameter("person_id").value
        self.session_id = self.get_parameter("session_id").value
        self.frame_id = self.get_parameter("frame_id").value
        self.loop = bool(self.get_parameter("loop").value)
        period = float(self.get_parameter("cycle_period_s").value)
        cpp = int(self.get_parameter("cycles_per_phase").value)

        # Read ONCE, before a single message exists: a posture that could change
        # mid-session would produce a capture half of which is pseudonymised.
        self._privacy = Privacy.from_env()
        self.get_logger().info(f"privacy gate: {self._privacy.describe()}")
        if self._privacy.deidentify and self._privacy.ephemeral:
            self.get_logger().info(
                "EMORI_ROS_ID_SALT is unset, so the salt is random per process: "
                "pseudonyms are NOT linkable across restarts (set it to correlate runs)")

        base = f"/humans/persons/{self.person_id}"
        self.pub_tracked = self.create_publisher(IdsList, "/humans/persons/tracked", _LATCHED)
        self.pub_anon = self.create_publisher(Bool, f"{base}/anonymous", _LATCHED)
        self.pub_eng = self.create_publisher(EngagementLevel, f"{base}/engagement_status", 10)
        self.pub_ctx = self.create_publisher(Context, f"{base}/context", 10)
        # Per-sentence, not per-cycle: the engagement node moves the head WHILE a
        # sentence is spoken, so this topic exists even on the mock source (where it
        # simply never publishes) rather than appearing only in one mode.
        self.pub_cue = self.create_publisher(SpeechCue, f"{base}/speech_cue", 10)

        # Announce the tracked person once (latched).
        self.pub_tracked.publish(IdsList(ids=[self.person_id]))
        self.pub_anon.publish(Bool(data=False))

        self._translator = Translator()
        self._live_session = ""  # session-id of the live stream (Translator reset key)
        # The kiosk language rides the SPEECH lane (the analysis envelope drops it), so
        # Context.lang stays "" until the robot has said something — never guessed.
        self._lang = ""

        if self.source_kind == "hub":
            self.get_logger().info("emori_adapter LIVE: fed in-process by the hub "
                                   f"(ROS person '{self.person_id}')")
        elif self.source_kind == "pokamind":
            self._queue: queue.Queue = queue.Queue(maxsize=32)
            self._stop = threading.Event()
            ws_url = str(self.get_parameter("ws_url").value)
            self._reader = threading.Thread(target=self._read_live, args=(ws_url,),
                                            name="pokamind-reader", daemon=True)
            self._reader.start()
            # Poll faster than the cycle cadence so live latency stays low.
            self._timer = self.create_timer(max(0.2, period / 4.0), self._tick_live)
            self.get_logger().info(f"emori_adapter LIVE: relaying {ws_url} "
                                   f"(ROS person '{self.person_id}')")
        else:
            self._source = MockPokamindSource(
                session_id=self.session_id, person_id=self.person_id,
                cycles_per_phase=cpp, realtime=False,
                shape=str(self.get_parameter("mock_shape").value),
            )
            self._it = self._source.cycles()
            self._timer = self.create_timer(period, self._tick_mock)
            self.get_logger().info(
                f"emori_adapter publishing for person '{self.person_id}' every {period:.1f}s (mock)"
            )

    # --- live source -------------------------------------------------------
    def _read_live(self, ws_url: str) -> None:
        """Forever: stream one session's cycles, then reconnect for the next."""
        from emori_adapter.sources.websocket import PokamindBridgeSource

        while not self._stop.is_set():
            src = PokamindBridgeSource(ws_url, session_id=self.session_id,
                                       person_id=self.person_id, max_retries=1_000_000)
            try:
                # events(), not cycles(): the same socket also carries every sentence of
                # AI speech, and the two are interleaved on the wire.
                for event in src.events():
                    try:
                        self._queue.put_nowait(event)
                    except queue.Full:
                        pass  # behind: drop the oldest semantics aren't worth a lock
            except Exception as e:  # noqa: BLE001
                self.get_logger().warn(f"live source error: {e}")
            self.get_logger().info("live session ended; waiting for the next one")

    def _tick_live(self) -> None:
        try:
            event = self._queue.get_nowait()
        except queue.Empty:
            return
        self._on_live_event(event)

    def feed(self, envelope: dict) -> None:
        """One relayed envelope, published now. The ``hub`` source's way in.

        The hub calls this on its fan-out thread, one envelope at a time and in order, and
        never on the thread that relayed the envelope, so whatever this costs is not the
        session's. An exception is the caller's to count: it costs this envelope.
        """

        event = event_from_envelope(envelope, default_session=self.session_id,
                                    default_person=self.person_id)
        if event is not None:
            self._on_live_event(event)
        elif envelope.get("type") == COMPLETED_EVENT:
            self.get_logger().info("live session ended; waiting for the next one")

    def _on_live_event(self, event) -> None:
        if isinstance(event, SpeechEvent):
            self._publish_speech_cue(event)
            return
        cycle = event
        if cycle.session_id != self._live_session:
            # New kiosk session -> fresh Translator (engagement bands, baselines).
            self._live_session = cycle.session_id
            self._translator = Translator()
            self._lang = ""  # a new learner may be a new language; never inherit one
            # The log goes to /rosout, so it says only what the topics may say.
            person, session = self._privacy.apply_ids(cycle.person_id, cycle.session_id)
            self.get_logger().info(f"new live session: {session} (person {person})")
        self._publish(cycle)

    # --- per-sentence speech ------------------------------------------------
    def _publish_speech_cue(self, event: SpeechEvent) -> None:
        """Republish one sentence of AI speech (or the end-of-turn marker)."""

        msg = SpeechCue()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.person_id, msg.session_id = self._privacy.apply_ids(
            event.person_id, event.session_id)
        msg.seq = max(0, int(event.seq))
        # Belt and braces: the bracket vocabulary is creative direction for the LLM and
        # is stripped upstream, before synthesis. If one ever survived the hop it must
        # not reach a subscriber that might render or speak it. Strip FIRST, then let
        # the privacy gate decide whether the sentence travels at all -- with the gate
        # closed (the default) both come out empty.
        msg.text, msg.cue_body = self._privacy.apply_speech(
            _CUE_BRACKETS.sub("", event.text).strip(), event.cue_body)
        # Never gated: the behaviour lane plans the head from the FAMILY and the budget,
        # neither of which says anything about the learner.
        msg.cue_category = event.cue_category
        msg.cue_category_authored = event.cue_category_authored
        msg.lang = event.lang
        self._lang = event.lang or self._lang
        msg.source = event.source
        msg.est_duration = float(event.est_duration)
        seconds = _epoch_seconds(event.timestamp)
        msg.source_time.sec = int(seconds)
        msg.source_time.nanosec = int(round((seconds - int(seconds)) * 1e9))
        msg.ended = bool(event.ended)
        self.pub_cue.publish(msg)
        self.get_logger().debug(
            f"speech_cue seq={msg.seq} family={msg.cue_category or '(none)'} "
            f"ended={msg.ended} est={msg.est_duration:.2f}s")

    # --- mock source -------------------------------------------------------
    def _tick_mock(self) -> None:
        try:
            cycle = next(self._it)
        except StopIteration:
            if not self.loop:
                self.get_logger().info("session source exhausted; stopping")
                self._timer.cancel()
                return
            try:
                self._it = self._source.cycles()  # restart the arc (Translator state persists)
                cycle = next(self._it)
            except StopIteration:
                self.get_logger().error("source empty even after restart; disabling timer")
                self._timer.cancel()
                return
        self._publish(cycle)

    # --- publish -----------------------------------------------------------
    def _publish(self, cycle: AnalysisCycle) -> None:
        sig = self._translator.translate(cycle)
        stamp = self.get_clock().now().to_msg()

        eng = EngagementLevel()
        eng.header.stamp = stamp
        eng.header.frame_id = self.frame_id
        eng.level = int(sig.engagement_level)
        self.pub_eng.publish(eng)

        ctx = Context()
        ctx.header.stamp = stamp
        ctx.header.frame_id = self.frame_id
        ctx.person_id, ctx.session_id = self._privacy.apply_ids(
            sig.person_id, sig.session_id)
        ctx.seq = int(sig.seq)
        ctx.engagement_score = float(sig.engagement_score)
        ctx.engagement_level = int(sig.engagement_level)
        ctx.valence = float(sig.valence)
        ctx.arousal = float(sig.arousal)
        ctx.expression = sig.expression
        ctx.expression_confidence = float(sig.expression_confidence)
        ctx.engagement_trend = float(sig.context.engagement_trend)
        ctx.escalation = float(sig.context.escalation)
        ctx.rapport = float(sig.context.rapport)
        ctx.attention = float(sig.context.attention)
        ctx.dominant_style = sig.context.dominant_style
        ctx.topic = sig.context.topic
        ctx.note = sig.context.note

        # --- v2: the raw observation record alongside the interpretation ----------
        # `person_report` is pure and does the one thing that matters here: a channel
        # the session did not report leaves the sentinel in place. Nothing below may
        # substitute a zero for it, which is why every float is copied verbatim.
        report = person_report(cycle, lang=self._lang)
        ctx.posture = report.posture
        ctx.head_position = report.head_position
        ctx.gesture_type = report.gesture_type
        ctx.overall_energy = report.overall_energy
        ctx.body_engagement = float(report.body_engagement)
        ctx.body_confidence = float(report.body_confidence)
        ctx.face_positivity = float(report.face_positivity)
        ctx.voice_warmth = float(report.voice_warmth)
        ctx.pitch_variety = float(report.pitch_variety)
        ctx.style_names = list(report.style_names)
        ctx.style_values = [float(v) for v in report.style_values]
        ctx.report_flags = int(report.flags) & 0xFF  # uint8 on the wire
        ctx.lang = report.lang
        # Freshness is a claim about the SOURCE's clock against ours, and only the live
        # source has one. The mock arc stamps from a FIXED epoch, so `time.time()` minus
        # it is a number that grows by a day every day and is large, positive and
        # therefore not the sentinel - a consumer doing what age_s is for would reject
        # every cycle of the demo graph, and rqt_plot would show freshness climbing in a
        # straight line. Withholding the claim is the honest answer for a fixture.
        ctx.age_s = (_age_seconds(cycle.timestamp) if self.source_kind in _LIVE_SOURCES
                     else NOT_REPORTED)
        self.pub_ctx.publish(ctx)

        # keep the latched tracked list fresh for late subscribers
        self.pub_tracked.publish(IdsList(ids=[self.person_id]))
        self.get_logger().info(
            f"cyc {sig.seq:02d} eng={sig.engagement_score:.2f} "
            f"{sig.engagement_level.name} val={sig.valence:+.2f} esc={sig.context.escalation:.2f} "
            # flags=0 is the degraded cycle -- worth being able to count in the journal
            # rather than inferring from a run of suspiciously identical numbers.
            f"flags={ctx.report_flags:02d}"
        )

    def destroy_node(self) -> bool:
        if hasattr(self, "_stop"):
            self._stop.set()
        return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = EmoriAdapterNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
