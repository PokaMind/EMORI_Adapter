"""The hostapp's live-session machinery: the single robot driver + session state.

The hostapp is the ONE component that drives the robot during a live session
(ADR: hostapp-hub). Everything here is transport-free and dependency-injected so
the e2e replay test can run it in-process with a DryRun robot client.

Pieces:

* :class:`RobotPlayer`     - background thread that paces gesture sequences on the daemon.
* :class:`UtteranceDeduper`- guards against a transport-level double delivery of the
                             same ``pokamind_ai_utterance`` (the two Pokamind emission
                             sites are phase-exclusive; real duplication should not occur).
* :class:`SessionInfo`     - identity + language of the active session (stamped on envelopes).
* :class:`SessionTracker`  - per-session accumulators -> localized spoken summary.
* :class:`Recorder`        - JSONL recording of every stamped envelope (replay fixtures).
* :class:`LiveDriver`      - ties them together; called from ``/api/cycle``.
* :class:`IdleTicker`      - keeps the robot visibly alive when no cycles arrive.
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
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from emori_adapter.behavior import (
    AffectSnapshot,
    BehaviorConfig,
    BehaviorEngine,
    CueConfig,
    FAREWELL_MOVE,
    GREETING_MOVE,
    CuePlanResult,
    CueScheduler,
)
from emori_adapter import motion_trace
from emori_adapter.plan_events import (
    CUE_MOTION_OFF,
    ENDED,
    PLANNED,
    PREEMPTED,
    STARTED,
    PlanEventStream,
    SessionPlanCounts,
    batch_outcome,
)
from emori_adapter.behavior import repertoire as R
from emori_adapter.behavior.cues import sustain_for
from emori_adapter.hostapp.gestures import GestureDirection
from emori_adapter.hostapp import metrics_store
from emori_adapter.hostapp.skill_api import motion_owner
from emori_adapter.hostapp.metrics_store import MetricsStore, get_store
from emori_adapter.models import DerivedSignals
from emori_adapter.robot.client import motion_sync_enabled
from emori_adapter.sources.websocket import cycle_from_envelope
from emori_adapter.translation import TranslationConfig, Translator

log = logging.getLogger("emori.hostapp.driver")

# Below this the head is only jittering; logging every tick would drown the trace.
_GESTURE_DEBUG_MIN_DELTA_RAD = 0.02
# A standing suppression reports at most this often.
_GESTURE_DEBUG_SUPPRESS_EVERY_S = 3.0
# How long a `motion.residual` keeps the narrow head reservation alive after the last
# one was seen. Twenty sensing ticks: a dropped poll is not a behaviour change, and a
# rollback is felt inside one human reaction time.
_RESIDUAL_LIVE_S = 2.0
# How long after the head stops being deflected it is still "in a human's hands".
# Covers the release stroke of one physical turn, which is the half of the gesture the
# detector is deliberately blind to.
_HEAD_HOLD_RELEASE_S = 1.0


def _motion(state: Optional[dict]) -> Optional[dict]:
    """The v2 shim's ``motion`` block, or None on a v1 robot / Pollen's daemon.

    An extra key on an object Pollen's own model still validates, so every consumer
    that predates it is untouched -- and every consumer that reads it has to treat its
    absence as "this robot cannot tell me, behave exactly as before".
    """

    if not isinstance(state, dict):
        return None
    motion = state.get("motion")
    return motion if isinstance(motion, dict) else None

def _face_fresh(motion: Optional[dict]) -> bool:
    """The v2 node sees a face right now (presence for the conversation watchdog).

    ``face_seen_s`` (seconds since the node's face tracker last selected a face) is the
    real signal; ``gaze_active`` (a gaze head write ACCEPTED within 1 s) is the fallback
    for a node image that predates it -- it goes quiet once the head has arrived on a
    learner who stands still, so it can only under-report presence, never invent it.
    """

    if not isinstance(motion, dict):
        return False
    seen = motion.get("face_seen_s")
    if isinstance(seen, (int, float)) and not isinstance(seen, bool):
        return 0.0 <= float(seen) < 1.0
    return motion.get("gaze_active") is True


# Head pitch (deg) held at session start so the robot faces a STANDING person.
# NEGATIVE is up here: repertoire.nod authors "down" as +16 and farewell's bow as
# +14. greeting_gesture peaks at -8 but SETTLES at +4, which on a table-top robot
# aims at a standing learner's waist -- and outside a session the head is wherever
# the last add-on pose or push-turn left it. -14 lifts the gaze roughly to face
# height while staying inside the repertoire's gentle authored range.
_ATTEND_UP_PITCH_DEG = -14.0
# Only the physical table/person height can settle that number, so it is tunable
# on the robot without a code change (same posture as the gesture thresholds).
_ATTEND_UP_PITCH_MIN_DEG = -25.0


def attend_up_pitch_deg() -> float:
    """Session-start look-up pitch, env-overridable and clamped to a safe range."""

    raw = (os.environ.get("EMORI_ATTEND_UP_PITCH_DEG", "") or "").strip()
    if not raw:
        return _ATTEND_UP_PITCH_DEG
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_ATTEND_UP_PITCH_DEG=%r is not a number; using %s",
                    raw, _ATTEND_UP_PITCH_DEG)
        return _ATTEND_UP_PITCH_DEG
    # A typo'd +40 (wrong sign, wrong magnitude) would drive the Stewart platform
    # into its limit and read as a broken robot on stage; clamp instead.
    clamped = min(0.0, max(_ATTEND_UP_PITCH_MIN_DEG, value))
    if clamped != value:
        log.warning("EMORI_ATTEND_UP_PITCH_DEG=%s is outside %s..0; using %s",
                    value, _ATTEND_UP_PITCH_MIN_DEG, clamped)
    return clamped


# Trajectory-shape guards for the directional detector, tunable on the robot
# because only the physical head's settling time can settle them.  Defaults are
# the measured ones: hold jitter ~0.001 rad, a settling head moves >=0.05 rad
# per 0.1 s tick, a deliberate turn reaches 0.20+ rad.
_GESTURE_SHAPE_DEFAULTS: dict[str, float] = {
    "EMORI_GESTURE_SETTLE_EPSILON_RAD": 0.03,
    "EMORI_GESTURE_SETTLE_TICKS": 3,
    "EMORI_GESTURE_QUIET_EPSILON_RAD": 0.05,
    "EMORI_GESTURE_QUIET_TICKS": 3,
    # The push of the head back that leaves whatever is running. See BackPushDetector.
    "EMORI_GESTURE_BACK_THRESHOLD_RAD": 0.15,
    "EMORI_GESTURE_BACK_HOLD_TICKS": 7,
}


def gesture_shape_param(name: str) -> float:
    """One rise-from-settled knob, env-overridable and loud on a bad value."""

    default = _GESTURE_SHAPE_DEFAULTS[name]
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not math.isfinite(value):
        # float() accepts "nan"/"inf" happily and NaN survives every comparison
        # below (NaN <= 0 is False), so a fat-fingered knob reached the detector:
        # a NaN settle epsilon makes EVERY window count as settled, and
        # int(nan)/int(inf) raises -- from GestureTicker.__init__, i.e. the hostapp
        # does not boot at all. A typo in a tuning knob must never be a dead robot.
        log.warning("%s=%r is not a finite number; using %s", name, raw, default)
        return default
    if value <= 0:
        # 0 would disable exactly the guard these exist for (no settle window,
        # no rest requirement) and read as "the tuning took".
        log.warning("%s=%s must be > 0; using %s", name, value, default)
        return default
    return value


def push_turn_gesture_mode() -> bool:
    """True when gesture sensing reads a TURN from a stiff head.

    ``back_drive`` (legacy) needs the head limp in ``gravity_compensation`` so a
    nudge deflects it.  ``push_turn`` needs the opposite -- stiff motors that
    hold, and spring back to neutral afterwards.

    Shared by the player and the gesture ticker deliberately.  If they disagree
    -- the ticker honouring push_turn while the player still asserts
    gravity_compensation for ANY gesture mode -- and on firmware v1.9.0 that mode
    raises "Gravity compensation mode is only supported with the Placo
    kinematics engine".  The 500 leaves the daemon's motor backend
    ``ready:false``, after which every goto returns 200 with a move id and is
    never executed -- so the head cannot be sprung back to neutral, or
    recentred by an add-on idle pose, and stays wherever it was pushed.
    """

    return os.environ.get("EMORI_GESTURE_MODE", "back_drive").strip().lower() == "push_turn"


# How long the SECOND half of the hidden language toggle may arrive after the first.
# 2.5 s is a deliberate turn-back on a stiff head (which has to be sprung to neutral in
# between, ~0.4 s of motion plus the detector's settle) without being long enough for a
# human to have finished one selection and started thinking about another.
_LANG_TOGGLE_WINDOW_S = 2.5


def lang_toggle_window_s() -> float:
    """Double-turn window, env-overridable (``EMORI_LANG_TOGGLE_WINDOW_S``).

    Loud on a bad value like the other on-robot knobs: this number is also the WORST-CASE
    latency a lone head turn pays while the robot is idle, so a silent
    revert to the default is indistinguishable from a retune that took.
    """

    raw = (os.environ.get("EMORI_LANG_TOGGLE_WINDOW_S", "") or "").strip()
    if not raw:
        return _LANG_TOGGLE_WINDOW_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_LANG_TOGGLE_WINDOW_S=%r is not a number; using %s",
                    raw, _LANG_TOGGLE_WINDOW_S)
        return _LANG_TOGGLE_WINDOW_S
    # <=0 would consume every first turn and release it on the same tick (no toggle is
    # ever possible); anything past a few seconds makes a head turn feel broken.
    clamped = min(10.0, max(0.5, value))
    if clamped != value:
        log.warning("EMORI_LANG_TOGGLE_WINDOW_S=%s is outside 0.5..10; using %s",
                    value, clamped)
    return clamped


# Longest the freeform cue direction may be before the planner sees it. The lexicon
# scan is a substring match over a phrase the LLM authored, so nothing legitimate is
# long -- and this field arrives from a cross-origin postMessage relayed verbatim.
_CUE_FIELD_MAX = 200


def _cue_field(stamped: dict, key: str) -> str:
    """One optional cue field off the utterance envelope, bounded and never None."""

    value = stamped.get(key)
    return value.strip()[:_CUE_FIELD_MAX] if isinstance(value, str) else ""


def _has_results(results) -> bool:
    """Did a ``pokamind_session_completed`` envelope carry an actual outcome?

    The completion status turns on this (see :meth:`LiveDriver.on_completed`), so it
    is decided on exactly the evidence that gets STORED -- competency scores, or an
    analysis/summary excerpt -- and never on ``bool(payload)``. The two are not the
    same question: the recorded envelopes in ``artifacts/recordings`` routinely
    carry ``{'body_language_metrics': ..., 'duration_minutes': ..., 'styles': ...}``,
    which is a truthy dict that ``MetricsStore.session_finished`` stores nothing
    from. Counting those as completions produced rows with NULL scores AND NULL
    excerpt in the numerator of the monthly-completion KPI -- a completion
    with nothing to show -- which is the fiction this whole rule exists to keep out.
    """

    return metrics_store.results_recorded(results)


class RobotPlayer:
    """Plays gesture sequences on the robot in a background thread.

    The daemon's ``goto`` is async (returns immediately), so firing a multi-step gesture
    (nod down-then-up, talking beats) back-to-back makes the robot jump straight to the
    final pose - nothing animates. This player sleeps each move's duration before the next,
    so sequences are actually *visible* in the simulator (and on the real robot). It also
    wakes the robot once the daemon is reachable (the daemon may finish booting after us).

    ``gravity_comp``: when the screenless head-tilt gesture sensor is active, the daemon
    HEAD must be in ``gravity_compensation`` (back-drivable) so a human nudge deflects
    ``head_pose``. The player owns every ``wake_up()``/``apply()`` — and those re-enable
    (stiffen) the motors — so it RE-ASSERTS ``gravity_compensation`` right after the wake
    and after each motion batch drains, leaving the head compliant before the sensor's
    next poll. With ``gravity_comp=False`` the player behaves exactly as before.
    """

    # ``wake_up`` returns before its physical head motion has necessarily settled.
    # Keep the gesture sensor guarded for this measured post-command interval too,
    # before restoring the head to gravity compensation.
    _WAKE_SETTLE_S = 0.6
    _STOP = object()
    # Voice-locked performance (see hostapp/voice_link.py). A speech batch holds its
    # first beat this long at most for the voice to become audible, and jumps to its
    # settle once the voice has been silent this long after having spoken.
    #
    # Measured live: Pokamind posts the sentence event
    # 1.0-3.5 s BEFORE its audio is audible on the turn path (the TTS stream is still
    # buffering when playback "starts") and 6.4 s before on the scenario-intro path,
    # so a 0.8 s hold performed the plan in silence and had nothing left when the
    # voice came. The hold is long because the voice WILL come; a lost voice costs a
    # late plan, which is what happened anyway. And a plan whose estimate ran out
    # while the voice still speaks SUSTAINS -- voice-paced micro-beats until the
    # voice stops -- instead of standing still mid-sentence.
    # 90 s, not 20: the scenario's first-question audio can arrive more than 20 s
    # after its event, and a 20 s plan would have given up. The hold is
    # cheap -- a newer sentence, the end-of-turn settle or the session end revokes
    # the batch within one slice -- so waiting is the right state: the robot IS about
    # to speak, and a held plan performs at the voice instead of never.
    _VOICE_HOLD_S = 90.0
    # Long enough for a breath, short enough that the head settles within the pause.
    _VOICE_END_S = 0.40
    _OLD_VOICE_END_S = 0.55
    # How far before its event a sentence's voice may already have come up: the page's post
    # can land just after its own audio starts. It says which rises count as this sentence's,
    # not how early a plan can start: a syllable of the last sentence inside it, or an onset of
    # that sentence while this one's audio is still to come, starts the plan on that voice. So
    # events posted faster than the voice speaks can run ahead of it, and early is the safe way.
    _BIND_LOOKBACK_S = 0.20
    # The voice has been speaking since this long after the event, without a new onset: the
    # event arrived late, part way into a word. Counted on energy, not on the hangover, or
    # the last sentence's tail would read as this one's voice.
    _BIND_CONTINUED_S = 0.25
    _BIND_LIVE_S = 0.06
    # The longest the page's own line on the speaker can hold up binding, if the page goes
    # away before it says the line has finished.
    _VOICE_COVER_MAX_S = 10.0
    _SUSTAIN_MAX_S = 14.0
    # After a speech batch settled (the voice paused), it lingers this long: if the
    # voice comes back before the next sentence's event (a long sentence with a
    # breath in it -- e.g. 2 s of speech with a still head after a pause),
    # it resumes the voice-paced micro-beats and settles again when the voice stops.
    _LINGER_S = 1.5
    # Every wait is sliced this fine so a preempting sentence (generation guard) or a
    # shutdown is honoured within one slice, not at the end of a 1.2 s goto.
    _SLICE_S = 0.05

    def __init__(self, client, *, gravity_comp: bool = False, voice=None) -> None:
        self._client = client
        self._gravity_comp = gravity_comp
        # The robot's own voice as a live signal (VoiceLink) or None: with it, batches
        # whose intents carry ``speech=True`` start when the voice starts, pace their
        # beats on syllable onsets and settle when the voice stops; without it the
        # player is exactly the fixed-duration sequencer it always was.
        self._voice = voice
        self._sync = motion_sync_enabled()
        self._voice_end_s = self._VOICE_END_S if self._sync else self._OLD_VOICE_END_S
        self._q: queue.Queue = queue.Queue(maxsize=12)
        # Queue size falls to zero as soon as the worker takes a batch, even
        # though apply(), visible settling, and gravity-compliance restore are
        # still in flight. Track that full lifecycle explicitly.
        self._motion_state_lock = threading.Lock()
        self._motion_active = threading.Event()
        self._stop = threading.Event()
        # Cuts a wait short: a newer sentence's plan takes the head at once, not at the end
        # of a slice.
        self._wake_event = threading.Event()
        self._speech_batches = 0
        self._cover_until = 0.0
        # Where a sentence's plan says it started and how it ended (emori_adapter.plan_events).
        # None until something reads it, and then a check before each of those two moments.
        self.plan_events = None
        self._t = threading.Thread(target=self._run, name="robot-player", daemon=True)
        self._t.start()

    def _assert_compliant(self) -> None:
        """Re-put the HEAD into gravity_compensation so it's back-drivable for the
        gesture sensor. No-op unless ``gravity_comp`` is set; never raises."""
        if not self._gravity_comp:
            return
        try:
            self._client.set_motor_mode("gravity_compensation")
        except Exception:  # noqa: BLE001 - a transport blip must not kill the player
            pass

    @property
    def busy(self) -> bool:
        """True when a motion is queued/playing — the gesture sensor suppresses
        detection while the robot is moving its own head (avoids self-triggering)."""
        # Includes in-flight apply, settle, and gravity-compliance restoration;
        # it is deliberately broader than Queue.qsize().
        return self._motion_active.is_set()

    @property
    def voice_available(self) -> bool:
        return self._voice_ready() is not None

    @property
    def performing_speech(self) -> bool:
        """A sentence's plan is waiting or running, including its sustain and linger."""

        with self._motion_state_lock:
            return self._sync and self._speech_batches > 0

    def wake(self) -> None:
        """Something was revoked: stop waiting and look again."""

        self._wake_event.set()

    def hold_voice(self, on: bool) -> float:
        """The page's own line is on the speaker, so what the tap hears is not a sentence.

        Returns the deadline set, which is what :meth:`release_voice` is given.
        """

        self._cover_until = time.monotonic() + self._VOICE_COVER_MAX_S if on else 0.0
        return self._cover_until

    def release_voice(self, held_until: float) -> None:
        """End a hold that is still the caller's, and leave one the page has taken since."""

        if self._cover_until == held_until:
            self._cover_until = 0.0

    def _clear_busy_if_idle(self) -> None:
        """Clear only if a producer did not race in with another batch."""
        with self._motion_state_lock:
            if self._q.empty():
                self._motion_active.clear()

    def submit(
        self,
        intents: list,
        *,
        drop_if_busy: bool = False,
        guard: Callable[[], bool] | None = None,
        yields: Callable[[], bool] | None = None,
    ) -> bool:
        """Queue a batch. ``guard`` false drops what is left of it where it stands.

        ``yields`` true ends it early too, for a batch that gives way to a sentence: it sends
        its last move first, so a head part way through a nod does not wait displaced for
        the sentence's voice.
        """

        intents = [i for i in (intents or []) if i is not None]
        if not intents or self._stop.is_set():
            return False
        if guard is not None and not self._guard_allows(guard):
            return False
        speech = any(getattr(i, "speech", False) for i in intents)
        # From this moment the voice is this sentence's, however long the player takes to
        # reach it behind another batch.
        voice = self._voice_ready() if speech else None
        mark = (time.monotonic(), voice.onset_count) if voice is not None else None
        # Keep live engagement reactions real-time: if a gesture is already queued/playing,
        # drop this engagement update rather than backing up. Speaking is never dropped.
        with self._motion_state_lock:
            if drop_if_busy and self._q.qsize() >= 2:
                return False
            try:
                # Publish the active flag *before* making the batch available
                # to Queue.get(). Otherwise a worker can drain the queue in the
                # interval before the flag is set and gesture sensing sees a
                # false idle state.
                self._motion_active.set()
                self._q.put_nowait((intents, guard, motion_trace.current_plan(), mark, speech,
                                    yields))
                if speech:
                    self._speech_batches += 1
                self._wake_event.set()
                return True
            except queue.Full:
                return False

    def stop(self) -> None:
        """Prevent new motions and wake the worker without waiting for I/O."""

        if self._stop.is_set():
            return
        self._stop.set()
        self._wake_event.set()
        try:
            self._q.put_nowait(self._STOP)
        except queue.Full:
            pass

    def join(self, *, timeout_s: float = 2.0) -> bool:
        """Boundedly join the player; safe to call more than once."""

        if threading.current_thread() is self._t:
            return False
        self._t.join(timeout=max(0.0, timeout_s))
        return not self._t.is_alive()

    @staticmethod
    def _guard_allows(guard: Callable[[], bool]) -> bool:
        try:
            return bool(guard())
        except Exception:  # noqa: BLE001 - unknown legacy ownership stays closed
            return False

    def _run(self) -> None:
        for _ in range(60):  # wake once the daemon is up (it may boot after the host app)
            if self._stop.is_set():
                self._motion_active.clear()
                return
            woke = False
            try:
                if self._client.health():
                    # The boot wake can move the head just like a queued intent.
                    # Publish its active state before sending the command, and retain it
                    # through settling plus gravity-compliance restoration so the gesture
                    # detector cannot interpret the robot's own motion as a user choice.
                    with self._motion_state_lock:
                        self._motion_active.set()
                    try:
                        # v1.9.0 boots motors 'disabled' (its launcher runs
                        # --no-wake-up-on-start) and nothing else ever enables them:
                        # gravity_compensation is a firmware 500 that the client
                        # latches off. Without this explicit enable, a cold-booted
                        # robot answers 200 to every goto and never moves.
                        self._client.set_motor_mode("enabled")
                    except Exception:  # noqa: BLE001 - a blip must not kill the player
                        pass
                    try:
                        self._client.wake_up()
                        woke = True
                    except Exception:  # noqa: BLE001
                        # A partially delivered wake can still have moved/stiffened the
                        # head, so preserve the guard and restore compliance below.
                        pass
                    if self._stop.wait(self._WAKE_SETTLE_S):
                        self._motion_active.clear()
                        return
                    self._assert_compliant()  # wake_up re-stiffens; restore back-drive
                    self._clear_busy_if_idle()
                    if woke:
                        break
            except Exception:  # noqa: BLE001
                pass
            if self._stop.wait(1.0):
                self._motion_active.clear()
                return
        while not self._stop.is_set():
            try:
                queued = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if queued is self._STOP:
                break
            intents, guard, plan, mark, speech, yields = queued
            try:
                if guard is not None and not self._guard_allows(guard):
                    tap = self.plan_events if plan else None
                    if tap is not None:
                        tap.put((ENDED, plan, PREEMPTED, ""))
                    continue
                moved = self._play_batch(list(intents), guard, plan, mark, yields)
                # A self-move re-enables (stiffens) the motors; restore the back-drivable
                # head once this batch finishes so the gesture sensor can sense the next tap.
                if moved:
                    self._assert_compliant()
            finally:
                # This comes after the settle sleep and compliance restore. A
                # concurrent submit shares the same lock, so it cannot be lost
                # between the empty check and a clear.
                if speech:
                    with self._motion_state_lock:
                        self._speech_batches -= 1
                self._clear_busy_if_idle()
        self._motion_active.clear()

    # --- one batch, voice-locked when it performs speech ---------------------
    def _revoked(self, guard) -> bool:
        return self._stop.is_set() or (guard is not None and not self._guard_allows(guard))

    def _voice_ready(self):
        """The VoiceLink when it is actually delivering a signal, else None."""

        voice = self._voice
        try:
            return voice if (voice is not None and voice.available) else None
        except Exception:  # noqa: BLE001 - a broken sensor means fixed pacing, never a crash
            return None

    def _sleep(self, seconds: float, guard, ended=None, voice=None) -> bool:
        """Interruptible wait. True when the batch must be abandoned (stop / preempted).

        Woken by a revoke, or by the voice's next frame when there is a voice, so the NEXT
        sentence's plan takes the head at once, instead of after the tail of a long goto --
        the visible "motion runs on after the voice has moved on". ``ended`` cuts the wait
        short without abandoning the batch: the voice has stopped, so go and settle.
        """

        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._revoked(guard):
                return True
            if ended is not None and ended():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if voice is not None and self._sync:
                voice.wait_for_frame(min(self._SLICE_S, remaining))
            else:
                self._wake_event.wait(min(self._SLICE_S, remaining))
                self._wake_event.clear()

    def _pace_wait(self, it, guard, voice, speech_batch: bool, ended=None) -> bool:
        """Hold after an intent: fixed, or paced on the next syllable onset.

        ``pace="voice"`` (the talking beats) waits a minimum gap and then for the next
        onset of the voice, bounded by a maximum hold -- so the beat after THIS one
        lands on a word, and a pause in the voice becomes a pause in the head.
        """

        if it.kind == "wake_up":
            # The daemon wake endpoint has no duration argument, so a caller-provided
            # MotionIntent duration cannot shorten the measured wake settle guard.
            hold = self._WAKE_SETTLE_S
        elif self._sync:
            # The whole move: the robot runs one at a time, so a beat released sooner only
            # waits behind this one, and each falls further behind the voice than the last.
            hold = max(0.12, float(it.duration or 0.3))
        else:
            hold = min(1.2, max(0.12, float(it.duration or 0.3)))
        if not self._sync:
            ended = None
        if getattr(it, "pace", "fixed") == "voice" and voice is not None and speech_batch:
            min_gap = hold if self._sync else max(0.10, 0.55 * hold)
            max_hold = max(hold, 0.45)
            if self._sleep(min_gap, guard, ended, voice):
                return True
            voice.wait_for_onset(max(0.0, max_hold - min_gap), stop=self._stop,
                                 abort=lambda: self._revoked(guard) or bool(ended and ended()))
            return self._revoked(guard)
        return self._sleep(hold, guard, ended, voice)

    def _apply_traced(self, it, plan: int, index: int) -> None:
        """``client.apply`` with its start and duration in the motion trace."""

        started = time.monotonic()
        motion_trace.set_tag(plan, index)
        try:
            self._client.apply(it)
        finally:
            motion_trace.set_tag(0, 0)
            motion_trace.emit("apply", at=started, plan=plan, i=index, kind=it.kind,
                              reason=it.reason, d=it.duration,
                              ms=round((time.monotonic() - started) * 1000.0))

    def _play_batch(self, intents: list, guard, plan: int = 0, mark=None, yields=None) -> bool:
        """Execute one batch; returns True when the robot was commanded to move."""

        moved = False
        n = len(intents)
        if n == 0:
            return False
        # A batch with no plan id is not a sentence's: the greeting, a cycle's motion, a settle.
        tap = self.plan_events if plan else None
        plan_started = False
        issued = -1
        no_voice = voice_cut = recorded_preempted = recorded_refused = False
        speech_batch = any(getattr(i, "speech", False) for i in intents)
        voice = self._voice_ready() if speech_batch else None
        family = next((i.tint for i in intents if getattr(i, "tint", "")), "")
        batch_t0 = time.monotonic()
        # THIS sentence's voice, not the tail of the previous one: the event for
        # sentence n+1 arrives while sentence n's last syllable is still inside the
        # 200 ms hangover, so the voice counts as ours once a new onset lands after the
        # event, a rise out of silence came just before it, or it has kept speaking for a
        # moment after it. All of that counts from the event, not from when the player got
        # here, which can be later if another batch was still finishing.
        onset0 = voice.onset_count if voice is not None else 0
        bound_from = batch_t0
        if self._sync and mark is not None:
            bound_from, onset0 = mark
        motion_trace.emit("pickup", at=batch_t0, plan=plan, n=n, speech=speech_batch,
                          voice=voice is not None)
        voice_seen = False
        sustain_seq = 0
        sustain_until: float | None = None

        lookback = self._BIND_LOOKBACK_S

        def ours() -> bool:
            nonlocal onset0, bound_from, lookback
            if self._sync and time.monotonic() < self._cover_until:
                # Whatever it hears until the line ends says nothing about this sentence.
                onset0, bound_from, lookback = voice.onset_count, time.monotonic(), 0.0
                return False
            if voice.onset_count > onset0:
                return True
            if not self._sync:
                return voice.active and time.monotonic() - batch_t0 >= 0.25
            return (voice.rose_since(bound_from - lookback)
                    or (voice.silent_for_s() <= self._BIND_LIVE_S
                        and time.monotonic() - bound_from >= self._BIND_CONTINUED_S))

        def note_voice_seen() -> None:
            motion_trace.emit("voice_seen", plan=plan, i=i,
                              bind="onset" if voice.onset_count > onset0 else "continued")

        def ended() -> bool:
            if yields is not None and yields():
                return True
            return (voice is not None and voice_seen
                    and voice.silent_for_s() >= self._voice_end_s)

        i = 0
        while i < n:
            if self._revoked(guard):
                break
            if yields is not None and yields():
                if moved:
                    try:
                        self._apply_traced(intents[n - 1].model_copy(update={"supersede": True}),
                                           plan, n - 1)
                    except Exception:  # noqa: BLE001
                        pass
                break
            it = intents[i]
            if voice is not None:
                if not voice_seen and ours():
                    voice_seen = True
                    note_voice_seen()
                if voice_seen and i < n - 1 and voice.silent_for_s() >= self._voice_end_s:
                    # The sentence is over: whatever remains of the plan is motion after
                    # the voice, so perform the settle NOW and nothing else.
                    voice_cut = True
                    i = n - 1
                    it = intents[i]
                elif i == 0 and not voice_seen:
                    # Motion starts when the voice does, not when the event arrived --
                    # and NEVER before it. Sliced so a preempting sentence is honoured
                    # mid-hold; if the voice never comes, perform nothing but the settle
                    # (motion in silence is exactly what this lane exists to remove).
                    deadline = time.monotonic() + self._VOICE_HOLD_S
                    while not ours() and time.monotonic() < deadline:
                        if self._revoked(guard):
                            break
                        # The next frame, not a slice: this is the one wait whose length
                        # lands directly between the voice and the first move.
                        if self._sync:
                            voice.wait_for_frame(self._SLICE_S)
                        else:
                            self._stop.wait(0.02)
                    if self._revoked(guard):
                        break
                    if ours():
                        voice_seen = True
                        note_voice_seen()
                    else:
                        no_voice = True
                        i = n - 1
                        it = intents[i]
                elif (i == n - 1 and n > 1 and voice_seen
                        and voice.silent_for_s() < self._voice_end_s):
                    # The plan's estimate ran out but the voice is still speaking this
                    # sentence: sustain with voice-paced micro-beats until it stops (or
                    # the next sentence preempts), THEN settle. Bounded so a voice that
                    # never pauses (a long monologue with no event) cannot hold forever.
                    if sustain_until is None:
                        sustain_until = time.monotonic() + self._SUSTAIN_MAX_S
                    if time.monotonic() < sustain_until:
                        beat = sustain_for(family, sustain_seq)
                        sustain_seq += 1
                        scale = 0.55 + 0.45 * max(0.0, min(1.0, float(voice.level)))
                        beat = beat.model_copy(update={"antennas": (beat.antennas[0] * scale,
                                                                    beat.antennas[1] * scale)})
                        try:
                            self._apply_traced(beat, plan, i)
                        except Exception:  # noqa: BLE001
                            pass
                        moved = True
                        if self._pace_wait(beat, guard, voice, speech_batch, ended):
                            break
                        continue          # re-evaluate: voice ended -> settle, else another beat
            issued = i
            if tap is not None and not plan_started and not no_voice:
                plan_started = True
                tap.put((STARTED, plan, time.monotonic()))
            if it.kind == "play_recorded":
                # A Pollen recorded move runs INSIDE the daemon for its full length, so
                # the per-intent sleep cannot pace it. Wait in slices, re-checking the
                # guard and the voice, and STOP the daemon-side move the moment a newer
                # sentence revokes this one or the voice falls silent -- that is what
                # keeps "motion never outlives the voice" true for playback.
                uuid = None
                play_started = time.monotonic()
                motion_trace.set_tag(plan, i)
                try:
                    play = getattr(self._client, "play_recorded_move", None)
                    if callable(play):
                        uuid = play(it.move or "", supersede=speech_batch and i == 0)
                except Exception:  # noqa: BLE001
                    uuid = None
                finally:
                    motion_trace.set_tag(0, 0)
                motion_trace.emit("apply", at=play_started, plan=plan, i=i, kind=it.kind,
                                  reason=it.reason, d=it.duration, move=it.move,
                                  accepted=uuid is not None,
                                  ms=round((time.monotonic() - play_started) * 1000.0))
                moved = True
                recorded_refused = recorded_refused or uuid is None
                if uuid is not None:
                    total = max(0.5, float(it.duration or 0.5))
                    waited = 0.0
                    cut_by = ""
                    while waited < total:
                        step = min(self._SLICE_S if self._sync else 0.1, total - waited)
                        if self._stop.wait(step):
                            cut_by = "stop"
                            break
                        waited += step
                        if guard is not None and not self._guard_allows(guard):
                            cut_by = "preempt"
                            break
                        if yields is not None and yields():
                            cut_by = "preempt"
                            break
                        if voice is not None:
                            if voice.active_since(batch_t0):
                                if not voice_seen:
                                    note_voice_seen()
                                voice_seen = True
                            if (voice_seen and waited >= 0.6
                                    and voice.silent_for_s() >= self._voice_end_s):
                                cut_by = "voice_end"
                                break
                    if cut_by:
                        motion_trace.emit("recorded_cut", plan=plan, by=cut_by,
                                          waited=round(waited, 2))
                        try:
                            stop = getattr(self._client, "stop_recorded_move", None)
                            if callable(stop):
                                stop(uuid)
                        except Exception:  # noqa: BLE001
                            pass
                        if cut_by != "voice_end":
                            recorded_preempted = True
                            break
                        # The voice stopped under the move: settle, skip the rest.
                        voice_cut = True
                        i = n - 1
                        continue
                i += 1
                continue
            if (voice is not None and getattr(it, "antenna_voice", False)
                    and it.antennas is not None):
                # The antennas follow the voice level: a quiet word, quiet antennas.
                scale = 0.55 + 0.45 * max(0.0, min(1.0, float(voice.level)))
                it = it.model_copy(update={"antennas": (it.antennas[0] * scale,
                                                        it.antennas[1] * scale)})
            if speech_batch and (i == 0 or i == n - 1) and not it.supersede:
                # This sentence's first move starts now, not after the last sentence's tail,
                # and a settle is what the head does when the voice has already stopped.
                it = it.model_copy(update={"supersede": True})
            try:
                self._apply_traced(it, plan, i)
            except Exception:  # noqa: BLE001
                pass
            if it.kind in ("goto", "look_at", "set_target", "antennas", "wake_up"):
                moved = True
                # The last move of a plan holds on through the voice's end, because it is the
                # settle. A batch that gives way still has to give way during it.
                if self._pace_wait(it, guard, voice, speech_batch,
                                   ended if i < n - 1 else yields):
                    break
            i += 1
        # Linger: the plan settled because the voice paused, but the sentence may not
        # be over. Until the next sentence's event preempts this batch, a voice that
        # comes back within the linger window gets voice-paced micro-beats again and
        # a fresh settle when it stops -- so a breath mid-sentence never leaves the
        # head still for the rest of it. Bounded by _SUSTAIN_MAX_S from the batch start.
        if (voice is not None and voice_seen and i >= n and n > 1
                and not self._revoked(guard)):
            settle = intents[n - 1]
            hard_stop = batch_t0 + self._SUSTAIN_MAX_S + self._VOICE_HOLD_S
            linger_until = time.monotonic() + self._LINGER_S
            while time.monotonic() < min(linger_until, hard_stop):
                if self._revoked(guard):
                    break
                if voice.silent_for_s() < self._voice_end_s and voice.active:
                    while (voice.silent_for_s() < self._voice_end_s
                           and time.monotonic() < hard_stop and not self._revoked(guard)):
                        beat = sustain_for(family, sustain_seq)
                        sustain_seq += 1
                        scale = 0.55 + 0.45 * max(0.0, min(1.0, float(voice.level)))
                        beat = beat.model_copy(update={"antennas": (beat.antennas[0] * scale,
                                                                    beat.antennas[1] * scale)})
                        try:
                            self._apply_traced(beat, plan, i)
                        except Exception:  # noqa: BLE001
                            pass
                        moved = True
                        if self._pace_wait(beat, guard, voice, speech_batch, ended):
                            break
                    if self._revoked(guard):
                        break
                    if not settle.supersede:
                        settle = settle.model_copy(update={"supersede": True})
                    try:
                        self._apply_traced(settle, plan, n - 1)
                    except Exception:  # noqa: BLE001
                        pass
                    if self._pace_wait(settle, guard, voice, speech_batch):
                        break
                    linger_until = time.monotonic() + self._LINGER_S
                    continue
                self._stop.wait(self._SLICE_S)
        if tap is not None or motion_trace.enabled():
            outcome, skip_reason = batch_outcome(n, issued, no_voice=no_voice, voice_cut=voice_cut,
                                                 preempted=recorded_preempted,
                                                 refused=recorded_refused)
            if tap is not None:
                tap.put((ENDED, plan, outcome, skip_reason))
            if motion_trace.enabled():
                motion_trace.emit("batch_end", plan=plan, i=i, n=n, moved=moved,
                                  revoked=self._revoked(guard), voice_seen=voice_seen,
                                  sustain=sustain_seq, outcome=outcome, skip_reason=skip_reason)
        return moved


class UtteranceDeduper:
    """Drop a transport-level double delivery of the same AI line within ``window_s``.

    A real duplicate should not occur (the intro line and the conversation turns arrive on
    separate paths); this guard exists for literal double delivery only (a re-posted
    message, a page hiccup), which carries the SAME seq and arrives at once — keying on
    (seq, source, cue, text) lets a roleplay character legitimately repeat an identical
    short line ("I see.") moments later without the robot going still. A sentence that is
    only a cue has no text and is still a sentence; one with neither has nothing to say.
    """

    def __init__(self, window_s: float = 10.0) -> None:
        self.window_s = window_s
        self._seen: dict[str, float] = {}

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def is_duplicate(self, text: str, seq: int = 0, now: Optional[float] = None,
                     *, cue: str = "", source: str = "") -> bool:
        now = time.monotonic() if now is None else now
        norm = self._norm(text)
        if not norm and not cue:
            return True
        key = f"{seq}|{source}|{cue}|{norm}"
        # purge expired entries so the dict never grows unbounded
        cutoff = now - self.window_s
        for k in [k for k, ts in self._seen.items() if ts < cutoff]:
            del self._seen[k]
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def reset(self) -> None:
        self._seen.clear()


@dataclass
class SessionInfo:
    """Identity of the active kiosk session, minted at SSO time."""

    session_id: str
    person_id: str
    name: str = ""
    email: str = ""
    lang: str = "en"
    topic: str = ""
    module_id: str = ""
    anonymous: bool = True
    started_at: float = field(default_factory=time.time)


_SUMMARY_TEXT = {
    "en": ("That's the end of our practice. Your engagement averaged {avg} percent, "
           "and you were most often {style}. {tip}"),
    "ca": ("Hem acabat la pràctica. La teva atenció mitjana ha estat del {avg} per cent "
           "i el teu estil més freqüent ha estat {style}. {tip}"),
}
# What the robot says when NOTHING was measured -- no analysis cycle ever arrived,
# so there is no engagement average and no dominant style to report. It must not
# read a percentage out of an empty list (that is what spoke "your engagement
# averaged 0 percent" and stored the 0.0 that made every dashboard engagement
# column read 0%), and it must not narrate the plumbing at the person either: a
# camera path that is down is the operator's problem, not something to say aloud.
_SUMMARY_NO_SIGNAL = {
    "en": "That's the end of our practice. Thanks for taking the time. {tip}",
    "ca": "Hem acabat la pràctica. Gràcies per dedicar-hi el temps. {tip}",
}
_TIPS = {
    "en": {
        "escalation": "You held steady when things heated up - keep that calm, grounded tone.",
        "high": "Strong engagement throughout. Next time, try one more open question.",
        "low": "Attention dipped a few times - slowing down and reflecting back can help.",
        # Nothing was measured: say something true of any practice rather than
        # grade a session on evidence that does not exist.
        "unmeasured": "Short, regular conversations are what build the habit - come back soon.",
    },
    "ca": {
        "escalation": "Has mantingut la calma quan la situació s'ha tensat: conserva aquest to serè.",
        "high": "Molt bona atenció durant tota la sessió. La propera vegada, prova una pregunta oberta més.",
        "low": "L'atenció ha baixat en alguns moments: anar més a poc a poc i parafrasejar pot ajudar.",
        "unmeasured": "Les converses curtes i regulars són les que creen l'hàbit: torna aviat.",
    },
}


class SessionTracker:
    """Accumulates per-cycle signals for the end-of-session spoken summary + email."""

    def __init__(self) -> None:
        self.info: Optional[SessionInfo] = None
        self._eng: list[float] = []
        self._styles: Counter = Counter()
        self._max_escalation = 0.0
        self.utterances = 0

    @property
    def active(self) -> bool:
        return self.info is not None

    @property
    def cycles(self) -> int:
        return len(self._eng)

    @property
    def exchanged(self) -> bool:
        """Any evidence at all that a real conversation is running.

        ``cycles`` is fed EXCLUSIVELY by Pokamind's camera analysis stream
        (``on_analysis``), so a session that is audibly conversing while the
        camera stream produces nothing has none -- which is why the AI's own
        spoken turns count too (``on_utterance``).  Every fail-safe that ends
        a session because "it never started" must ask this, never ``cycles``
        alone, or it ends live conversations.
        """

        return self.cycles > 0 or self.utterances > 0

    def start(self, info: SessionInfo) -> None:
        self.info = info
        self._eng = []
        self._styles = Counter()
        self._max_escalation = 0.0
        self.utterances = 0

    def add(self, sig: DerivedSignals) -> None:
        self._eng.append(sig.engagement_score)
        self._max_escalation = max(self._max_escalation, sig.context.escalation)
        if sig.top_styles:
            self._styles[sig.top_styles[0][0]] += 1

    def finish(self) -> dict:
        """Close the session and return the stats + localized summary text.

        ``avg_engagement`` is ``None`` -- never 0.0 -- when no analysis cycle ever
        arrived. The mean of an empty list is not zero engagement, it is no
        measurement, and the difference is the whole dashboard: the old ``else
        0.0`` was written straight onto the session row by ``session_finished``,
        so stored sessions claimed a measured 0% engagement nobody had
        measured, and the Engagement column of every table, the
        per-module means and the KPI tile all read 0%. ``None`` flows through
        ``session_finished``'s COALESCE and every dashboard aggregate as "no
        evidence", which is what it is.
        """
        avg = sum(self._eng) / len(self._eng) if self._eng else None
        dominant = self._styles.most_common(1)[0][0] if self._styles else "neutral"
        lang = (self.info.lang if self.info else "en") or "en"
        lang = lang if lang in _SUMMARY_TEXT else "en"
        tips = _TIPS[lang]
        if self._max_escalation >= 0.6:
            tip = tips["escalation"]
        elif avg is None:
            tip = tips["unmeasured"]
        elif avg >= 0.6:
            tip = tips["high"]
        else:
            tip = tips["low"]
        if avg is None:
            summary = _SUMMARY_NO_SIGNAL[lang].format(tip=tip)
        else:
            summary = _SUMMARY_TEXT[lang].format(
                avg=round(avg * 100), style=dominant, tip=tip)
        out = {
            "session": self.info,
            "avg_engagement": avg,
            "dominant_style": dominant,
            "max_escalation": self._max_escalation,
            "cycles": self.cycles,
            "utterances": self.utterances,
            "summary": summary,
            "lang": lang,
        }
        self.info = None
        return out


class Recorder:
    """Append every stamped envelope to a per-session JSONL recording.

    These recordings are the deterministic-replay fixtures the e2e harness uses
    ('simulate exactly having the robot'): scripts/replay_session.py POSTs them
    back through /api/cycle with original timing.
    """

    def __init__(self, directory: str = "artifacts/recordings", enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled

    def write(self, session_id: str, envelope: dict) -> None:
        if not self.enabled or not session_id:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
            with open(os.path.join(self.directory, f"{safe}.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        except OSError as e:
            # The errno CLASS only: OSError.__str__ appends `: '<filename>'`, and
            # this basename is the minted session id -- i.e. the SSO student id
            # server.py:_mint_session_id prefixes onto it. "[Errno 28] No space
            # left on device: 'artifacts/recordings/anna-001-...'" would be
            # written the moment someone raises the level to DEBUG to find out
            # why recordings stopped, which is exactly when the card is full.
            # The subclass plus the errno NUMBER is the diagnosis instead --
            # ENOSPC(28) and EROFS(30) are both a bare OSError, so the number is
            # what tells a full card from a read-only one -- and the directory is
            # a constant the reader already has.
            log.debug("recorder write failed in %s (%s errno=%s)",
                      self.directory, type(e).__name__, getattr(e, "errno", "-"))


def owner_fallback_s() -> float:
    """``EMORI_ROS_OWNER_FALLBACK_S`` -- seconds of ROS silence before the hostapp
    takes its expressive lanes back. ``0`` (the default) is OFF.

    Off by default deliberately. With ``EMORI_ROS_MOTION_OWNER=ros`` the honest
    failure of a wedged ROS graph is a robot that stops being expressive, and an
    operator can read that off ``/api/status`` -- whereas a hostapp that silently
    resumed driving would make the demonstration ("movement originates in the ROS
    graph") false without saying so. The knob exists for the unattended venue run,
    where a still robot in front of a person is the worse of the two, and it is the
    site that decides which. Anything unparseable reads as off, never as a window.
    """

    raw = (os.environ.get("EMORI_ROS_OWNER_FALLBACK_S", "") or "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_ROS_OWNER_FALLBACK_S=%r is not a number; fallback stays off",
                    raw)
        return 0.0
    transport = (os.environ.get("EMORI_SKILL_TRANSPORT", "") or "").strip().lower()
    if value > 0 and transport == "ros_native":
        log.warning(
            "EMORI_ROS_OWNER_FALLBACK_S=%s is IGNORED under "
            "EMORI_SKILL_TRANSPORT=ros_native. The native transport never calls "
            "/api/skill, so note_skill_accepted() is never reached and the liveness clock "
            "this window reads stays None for ever -- the window would open on EVERY "
            "session and put a second writer on the head. Use the HTTP transport, or "
            "remove the variable.", raw)
        return 0.0
    return max(0.0, value)


class LiveDriver:
    """Everything ``/api/cycle`` needs: stamp -> translate -> behave -> track.

    Thread-safe: FastAPI may run handlers concurrently in its threadpool, and the
    Translator/BehaviorEngine are stateful.
    """

    def __init__(
        self,
        client,
        *,
        player: Optional[RobotPlayer] = None,
        fiware_factory: Optional[Callable[[SessionInfo], object]] = None,
        default_lang: str = "en",
        behavior_config: Optional[BehaviorConfig] = None,
        translation_config: Optional[TranslationConfig] = None,
        gesture_enabled: bool = False,
        metrics: Optional[MetricsStore] = None,
        voice=None,
    ) -> None:
        self.client = client
        # The robot's own voice as a live signal (hostapp/voice_link.py), handed to the
        # player so every speech plan is performed WHILE the sentence is audible. None
        # (tests, a robot without PipeWire) keeps the fixed-duration sequencer.
        self.voice = voice
        # Local dashboard store. A SECOND, independent sink from FIWARE on purpose:
        # Orion-LD is an optional off-robot dependency, and the on-robot management
        # dashboard has to work on a LAN that has never seen a broker. Every call is
        # best-effort inside MetricsStore, so nothing here needs a try/except.
        self.metrics = metrics if metrics is not None else get_store()
        # When the screenless head-tilt sensor is active, the default player keeps the
        # HEAD in gravity_compensation (back-drivable) between self-moves so a human tap
        # can be sensed. An injected player (tests) is used as-is.
        # gravity_comp ONLY for the back-drive gesture. push_turn is detected on
        # stiff motors, and asserting gravity_compensation there is not merely
        # unnecessary -- on this firmware it 500s and poisons every later move.
        self.player = player or RobotPlayer(
            client, gravity_comp=gesture_enabled and not push_turn_gesture_mode(),
            voice=voice,
        )
        # How each sentence's plan went, as a stream. The driver and the player only ever put
        # on it. The dashboard's counts are one reader and a graph node can be another, and
        # what they do with it happens away from the motion path.
        self.plan_events: Optional[PlanEventStream] = PlanEventStream()
        self._plan_counts = SessionPlanCounts(self.plan_events)
        self.player.plan_events = self.plan_events
        self._bcfg = behavior_config
        self._tcfg = translation_config
        self.translator = Translator(self._tcfg)
        self.engine = BehaviorEngine(self._bcfg)
        self.tracker = SessionTracker()
        self._motion_sync = motion_sync_enabled()
        self.deduper = UtteranceDeduper(window_s=1.5 if self._motion_sync else 10.0)
        # Per-SENTENCE expression lane, alongside the per-CYCLE engine above. Pokamind
        # tags every AI sentence with a [family| direction] cue and forwards the family
        # at that sentence's playback onset, so the robot can move to what the voice is
        # doing WHILE it says it. The two lanes are arbitrated in on_analysis, never
        # merged: see _submit_engagement.
        #
        # Config is read once, here, so a robot env change takes effect on restart -- the
        # same posture as the behaviour/translation tuning above.
        self._cue_config = CueConfig.from_env()
        # hostapp | ros | none. Read once here, like every other motion knob: with the
        # ROS graph owning motion (or nobody owning it) the hostapp's OWN expressive
        # lanes stand down, which is the difference between a documented switch and a
        # comment. Session motion that is not expressive -- greeting, farewell, the
        # add-on poses, push-turn feedback -- is untouched: those are the robot doing its
        # job, not a lane competing for the head.
        self._motion_owner = motion_owner()
        # ...and the dead-man's handle on that knob, read ONCE here like the owner
        # itself. 0 (the shipped default) is off. See owner_fallback_s() above for why
        # the honest failure -- a still robot -- is the default rather than this.
        self._owner_fallback_s = owner_fallback_s()
        # ``time.monotonic()`` of the last ACCEPTED /api/skill request, i.e. the last
        # moment the ROS lane is known to have moved this robot. Plain attributes, read
        # on the per-cycle motion path: a lock here would buy nothing (a float write is
        # atomic, and a fallback decision one cycle late is a non-event) and would put
        # a second lock on the hot path.
        self._last_skill_accepted: Optional[float] = None
        self._owner_fallback_active = False
        # "The head is currently an INPUT device" (push-turn selection). Injected after
        # construction because server.py builds the gesture ticker FROM this driver;
        # None until then, which reads as "not reserved".
        self._head_reserved: Optional[Callable[[], bool]] = None
        # "An add-on wants a face look-at", pushed by the add-on on every
        # state transition. False until one says otherwise, which is also the right answer
        # for a robot with no add-on at all.
        self._gaze_requested = False
        # What we last successfully told the ROBOT, as opposed to what we want. None
        # means "never acknowledged", so the first reconciler tick pushes both.
        self._pushed_gaze: Optional[bool] = None
        self._pushed_head_reserved: Optional[bool] = None
        # A gate edge that was DECIDED under ``_lock`` and therefore could not be sent
        # from there. ``_finalize_locked`` is the only writer; every caller of it drains
        # this after releasing the lock (:meth:`push_deferred_flags`), and the 10 Hz
        # reconciler drains it too simply by syncing, so a caller that forgot costs at
        # most one tick rather than a detector left running on an idle kiosk.
        self._gaze_push_pending = False
        # The learner as of the last cycle. `fresh` False means we have not seen them at
        # all yet this session, which the planner treats as "do not guess" rather than
        # as a perfectly neutral person.
        self._cue_affect = AffectSnapshot()
        # A cue plan is outstanding, i.e. on_tts_ended owes the robot exactly one settle.
        # False for a session that never carried a cue_category, which is what keeps a
        # no-cue session byte-identical to before this lane existed.
        self._cue_open = False
        self.cue = CueScheduler(
            lambda intents, guard: self.expressive_motion_enabled and self.submit_legacy_motion(
                intents, drop_if_busy=False, guard=guard),
            config=self._cue_config,
            head_reserved=self.head_reserved,
            on_revoke=self._wake_player,
            voice_available=lambda: bool(getattr(self.player, "voice_available", False)),
        )
        self.default_lang = default_lang
        self._fiware_factory = fiware_factory
        self.fiware = None
        self._lock = threading.Lock()
        # Training gestures and add-on poses share one physical player, but
        # only add-on feedback may move while an add-on mode owns
        # the robot.  A generation guard is checked again by RobotPlayer just
        # before execution, so a legacy batch already queued at preemption is
        # harmless rather than merely hidden behind a new state flag.
        self._legacy_motion_lock = threading.RLock()
        self._legacy_motion_enabled = True
        self._legacy_motion_generation = 0
        self._fiware_threads: set[threading.Thread] = set()
        self._fiware_threads_lock = threading.Lock()
        self._on_session_finalized: Callable[[str], None] | None = None
        self.last_activity = time.monotonic()
        # MONOTONIC session start (SessionInfo.started_at is wall-clock and is what the
        # pending-mint TTL uses). This is the clock behind "the session opened and the
        # iframe never came up": every other clock here is refreshed by activity, and a
        # session that never starts has none.
        self.session_started_at: Optional[float] = None
        # Set when SOMETHING asked Pokamind to complete the live session -- the
        # watchdog's walk-away/absence/stalled-turn triggers, or a learner's end
        # gesture. WatchdogTicker owns the grace clock that turns an unanswered request
        # into a local finalize, and it reads THIS: a request armed anywhere else has to
        # be visible to it, or that ending has no fail-safe at all (an end gesture with
        # none could never un-wedge a wedged robot).
        self.end_requested_at: Optional[float] = None
        # First pokamind_* postMessage relayed by the /session page (POST
        # /api/frame-alive).  Proof the cross-origin frame is ALIVE, distinct
        # from tracker.exchanged (first analysis cycle / AI line), which can
        # lag a live frame by design -- scenario generation, the device check and the
        # first spoken turn all sit between the two.  The session-start
        # fail-safe must guard "iframe dead", never "iframe slow".
        self.frame_alive_at: Optional[float] = None
        # Presence clock, distinct from last_activity: only a VERIFIED person (a body in
        # the camera analysis, or an accepted turn) refreshes it, so ambient noise can
        # never masquerade as someone standing there. ``_body_ever_seen`` latches the
        # session's first body so an audio-only / camera-off session never absence-ends.
        self.last_presence = time.monotonic()
        self._body_ever_seen = False
        # CAMERA-only "somebody is in front of the robot" clocks for the conversation
        # watchdog. Unlike last_presence they are never refreshed by audio
        # and never seeded by start_session: None means "not seen this session".
        #   body: a Pokamind analysis cycle carrying body metrics (bursty, ~2-60 s apart)
        #   face: the v2 robot node's own face tracker (motion.gaze_active, ~10 Hz poll)
        self.last_body_seen_at: Optional[float] = None
        self.last_face_seen_at: Optional[float] = None
        # Set (under ``_lock``) when the ROBOT asked Pokamind to end the session --
        # walk-away, presence-absence, or a stalled turn. The completion Pokamind
        # then sends back is indistinguishable from a learner finishing normally,
        # so without this the dominant abandonment path lands in ``on_completed``
        # and is stored as 'completed'. Cleared on start and on finalize.
        self._auto_end_reason: Optional[str] = None
        # A person asked to end this session with a head gesture, and whether the page has
        # opened a turn yet: see request_gesture_end.
        self._gesture_end_wanted = False
        self._turn_armed_seen = False
        # The session this driver finalized last: (session_id, wall end, monotonic end,
        # its stats). A completion that lands AFTER the local finalize is matched to it
        # and salvaged instead of thrown away -- see on_completed. Cleared on start.
        self._last_finalized: Optional[tuple[str, float, float, dict]] = None
        try:
            self._late_completion_s = max(0.0, float(
                os.environ.get("EMORI_LATE_COMPLETION_S", "") or 300.0))
        except ValueError:
            self._late_completion_s = 300.0
        self.counters: Counter = Counter()
        # Conversation watchdog: recovers a turn that opened but never submitted.
        # Timers are env-configurable so on-robot tuning needs no code change.
        from emori_adapter.hostapp.watchdog import ConversationWatchdog

        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return float(default)

        # Presence-aware policy (a session must never end on a learner who is there):
        # longer windows, a longer FIRST turn, two perceivable re-prompts, and presence
        # decides whether a quiet/noisy turn may end at all. See hostapp/watchdog.py.
        self.watchdog = ConversationWatchdog(
            reprompt_after_s=_f("EMORI_TURN_REPROMPT_S", 12.0),
            open_ceiling_s=_f("EMORI_TURN_CEILING_S", 35.0),
            abs_ceiling_s=_f("EMORI_TURN_ABS_CEILING_S", 90.0),
            reject_extensions=int(_f("EMORI_TURN_REJECT_EXTENSIONS", 3)),
            first_reprompt_s=_f("EMORI_TURN_FIRST_REPROMPT_S", 20.0),
            first_ceiling_s=_f("EMORI_TURN_FIRST_CEILING_S", 50.0),
            max_reprompts=int(_f("EMORI_TURN_MAX_REPROMPTS", 2)),
            max_reprompts_per_turn=int(_f("EMORI_TURN_MAX_REPROMPTS_PER_TURN", 3)),
            reprompt_every_s=_f("EMORI_TURN_REPROMPT_EVERY_S", 15.0),
            present_ceiling_s=_f("EMORI_TURN_PRESENT_CEILING_S", 180.0),
            present_silence_s=_f("EMORI_TURN_PRESENT_SILENCE_S", 45.0),
            present_quiet_s=_f("EMORI_TURN_PRESENT_QUIET_S", 10.0),
            self_noise_s=_f("EMORI_TURN_SELF_NOISE_S", 3.0),
            armed_ceiling_s=_f("EMORI_TURN_ARMED_CEILING_S", 90.0),
        )
        # Pending command for the kiosk /session page to pick up and relay into the
        # Pokamind iframe (begin/end). Set by the gesture sensor / walk-away watchdog
        # (adapter-side), drained by GET /api/pending-cmd. Single-slot: latest wins.
        self._pending_cmd: Optional[str] = None

    def enqueue_cmd(self, cmd: str) -> None:
        with self._lock:
            self._pending_cmd = cmd

    def enqueue_cmd_if_empty(self, cmd: str) -> bool:
        """Queue an ADVISORY verb (the spoken re-prompt) without ever overwriting a
        pending begin/end: the slot is single and latest-wins, and losing an `end` to a
        re-prompt would lose the learner's own end gesture."""
        with self._lock:
            if self._pending_cmd is not None:
                return False
            self._pending_cmd = cmd
            return True

    def pop_cmd(self, only: Optional[frozenset] = None) -> Optional[str]:
        """Drain the pending verb, or only the ones the caller can act on.

        ``only`` exists because two different pages poll this one slot: /session wants
        begin/end/home, the landing page wants the page-level `reload`. An unfiltered
        drain from either is a verb the other one silently loses.
        """

        with self._lock:
            cmd = self._pending_cmd
            if cmd is None or (only is not None and cmd not in only):
                return None
            self._pending_cmd = None
            return cmd

    @property
    def legacy_motion_enabled(self) -> bool:
        with self._legacy_motion_lock:
            return self._legacy_motion_enabled

    def set_legacy_motion_enabled(self, enabled: bool) -> None:
        """Gate training/idle movement without touching add-on feedback."""

        with self._legacy_motion_lock:
            value = bool(enabled)
            if self._legacy_motion_enabled != value or not value:
                self._legacy_motion_generation += 1
            self._legacy_motion_enabled = value

    @property
    def cue_config(self) -> CueConfig:
        """The cue limits this process planned against (the robot's, not the defaults).

        Public so the ``/api/skill`` intake plans against the SAME numbers -- notably
        ``EMORI_CUE_Z_MM``, which an operator bisects down to whatever this Stewart
        platform survives.
        """

        return self._cue_config

    @property
    def motion_owner(self) -> str:
        """``hostapp`` | ``ros`` | ``none`` -- who may submit EXPRESSIVE motion."""

        return self._motion_owner

    def note_skill_accepted(self) -> None:
        """An /api/skill request was just ACCEPTED: the ROS lane is alive.

        Called from ``skill_api`` on the one path that actually reached the player. A
        refusal is not evidence of life (a 409 says the knob disagrees, a 429 says the
        node is chattier than the queue) and a drop is not either, so neither closes
        the fallback window.
        """

        self._last_skill_accepted = time.monotonic()
        if self._owner_fallback_active:
            self._owner_fallback_active = False
            log.info("ROS motion resumed after %.0fs fallback; hostapp expressive "
                     "lanes stand down again", self._owner_fallback_s)

    def _owner_fallback_open(self) -> bool:
        """Has the ROS lane been silent long enough that the hostapp takes over?

        Four conditions, all of them there to stop the fallback from firing on a robot
        that is simply idle: the window must be configured, a session must be running,
        at least one cycle must have arrived (so there IS something to be expressive
        about), and the silence is measured from the LATER of the session start and the
        last accepted request -- a session that opens 10 minutes after the previous
        one's last skill request has not been let down by anybody yet.
        """

        started = self.session_started_at
        if started is None or not self.tracker.active or self.tracker.cycles <= 0:
            return False
        last = self._last_skill_accepted
        since = started if last is None else max(started, last)
        if (time.monotonic() - since) < self._owner_fallback_s:
            return False
        if not self._owner_fallback_active:
            self._owner_fallback_active = True
            self.counters["owner_fallback_episodes"] += 1
            log.warning("no /api/skill request accepted for %.0fs with "
                        "EMORI_ROS_MOTION_OWNER=ros; the hostapp is taking the "
                        "expressive lanes back until the ROS graph posts again",
                        self._owner_fallback_s)
        return True

    @property
    def expressive_motion_enabled(self) -> bool:
        """False when the ROS graph owns motion (or nobody does).

        Covers exactly the two lanes that compete for the head with the ROS graph: the
        per-sentence cue lane and the per-cycle engagement lane. ``none`` is the
        observation/benchmark posture (docs/configuration.md, ``EMORI_ROS_MOTION_OWNER``),
        so it stands both down too.

        The one exception is ``EMORI_ROS_OWNER_FALLBACK_S``: with ``ros`` and that
        window set, a graph that has stopped posting hands the lanes back here rather
        than leaving a person in front of a robot that has gone still. ``none`` NEVER
        falls back -- it is a deliberate "nothing moves", not a delegation that can
        fail.
        """

        if self._motion_owner == "hostapp":
            return True
        if self._motion_owner != "ros" or self._owner_fallback_s <= 0:
            return False
        return self._owner_fallback_open()

    def set_head_reserved(self, predicate: Optional[Callable[[], bool]]) -> None:
        """Bind "the head is currently an input device" (push-turn selection)."""

        self._head_reserved = predicate if callable(predicate) else None

    def head_reserved(self) -> bool:
        """True while push-turn selection owns the head, so motion must stand down.

        Any robot-authored motion wipes the directional detector's rolling baseline
        (the measured 12 baseline-resets/min trap), so the cue lane and the skill
        intake both ask this before planning anything.
        """

        predicate = self._head_reserved
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - a broken predicate must not stop a session
            return False

    def set_gaze_requested(self, allowed: bool) -> None:
        """An add-on asks for (or releases) a face look-at outside a training session.

        ``gaze_only`` because an add-on may call this while holding its own locks: asking
        ``head_reserved()`` here could double the worst case against an unreachable node
        for a gate the 10 Hz reconciler already carries within ~100 ms. The edges carry
        ``gaze_allowed``, the reconciler carries ``head_reserved``, because only one of the
        two is an event.
        """

        self._gaze_requested = bool(allowed)
        self.sync_robot_flags(gaze_only=True)

    def gaze_allowed(self) -> bool:
        """Is a face look-at wanted right now: a training session, or an add-on mode in
        conversation with somebody.

        Deliberately NOT ANDed with head_reserved(): that predicate is True whenever
        push_turn is on and no session runs, which would refuse every add-on request.  Gaze
        and push_turn coexist through residual sensing -- the robot subtracts its OWN
        command from the measured pose -- not through mutual exclusion.
        """

        if getattr(self.tracker, "active", False):
            return True
        return self._gaze_requested

    def sync_robot_flags(self, *, force: bool = False, gaze_only: bool = False) -> None:
        """Push the two arbiter gates the robot node cannot compute for itself.

        Edge-triggered and idempotent: a POST only happens when the answer CHANGED, so
        the 10 Hz reconciler costs two attribute comparisons on a quiet robot. A client
        that answers False (v1's 404, a transport blip) does not update the pushed
        state, so the next tick retries -- except under ``force``, which is the startup
        assertion onto a node that has been up for days and may hold a stale flag.

        ``force`` is load-bearing rather than decorative: the node's gaze RUN flag is
        fail-closed (default off), so without an unconditional push at startup a robot
        that boots straight into a session would never turn the detector on.

        ``gaze_only`` skips :meth:`head_reserved`, and is for the two callers that run
        underneath somebody else's lock -- :meth:`set_gaze_requested` (an add-on's own
        lock) and :meth:`push_deferred_flags` (immediately after the
        driver lock, where the value was decided). ``head_reserved()`` calls an INJECTED
        predicate that may reach into an add-on's own lock, and it is a
        PREDICATE: the head reservation loses nothing by waiting 100 ms for the
        reconciler, which exists to carry exactly that. Whether a face detector is
        running is not a predicate -- it is a privacy gate that has to go off with the
        session, on the edge, which is why the gaze half is never skipped.
        """

        # Whatever the caller asked for, the gaze gate below is about to be reconciled
        # against the live answer, so a deferred edge has nothing left to say.
        self._gaze_push_pending = False
        if not gaze_only:
            reserved = self.head_reserved()
            if force or reserved != self._pushed_head_reserved:
                if self._push_flag("set_head_reserved", reserved) or force:
                    if reserved != self._pushed_head_reserved:
                        log.info("head reservation pushed to the robot: %s",
                                 "armed" if reserved else "released")
                    self._pushed_head_reserved = reserved
        allowed = self.gaze_allowed()
        if force or allowed != self._pushed_gaze:
            if self._push_flag("set_gaze", allowed) or force:
                self._pushed_gaze = allowed

    def push_deferred_flags(self) -> None:
        """Send the gate edge ``_finalize_locked`` could only record.

        ``_finalize_locked`` decides "the session is over, the detector goes off" while
        holding ``_lock`` -- the lock on the path of every cycle, gesture and watchdog
        tick. POSTing from there would put a bounded-but-real network wait (up to
        ``_FLAG_TIMEOUT_S`` per gate against an unreachable node) underneath it, which
        is the latency bug ``start_session`` already refuses to write ("holding it
        across any I/O at all is how this file grows its next latency bug"). So the
        decision is recorded there and sent HERE, by each caller, one statement after
        its ``with self._lock`` block ends.

        Re-reads :meth:`gaze_allowed` rather than replaying a captured value, which is
        why ``start_session``'s preempt path is not a flap: by the time this runs the
        replacement session is already active, the answer is still True, and the
        edge-triggered push sends nothing at all.
        """

        if self._gaze_push_pending:
            self.sync_robot_flags(gaze_only=True)

    def _push_flag(self, method: str, value: bool) -> bool:
        """One gate POST; False for anything that is not an acknowledgement.

        Resolved by name rather than called directly because this runs on the 10 Hz
        gesture tick against an INJECTED client: a replay fixture or an older fake that
        predates these two routes must stay a no-op, exactly as an unreachable robot
        does, instead of taking the sensor thread down with an AttributeError.
        """

        setter = getattr(self.client, method, None)
        if not callable(setter):
            return False
        try:
            return bool(setter(value))
        except Exception:  # noqa: BLE001 - a gate is never worth a dropped tick
            log.debug("%s(%s) failed", method, value, exc_info=True)
            return False

    def submit_legacy_motion(self, intents: list, *, drop_if_busy: bool = False,
                             guard: Optional[Callable[[], bool]] = None,
                             yields: Optional[Callable[[], bool]] = None) -> bool:
        """Submit training/idle motion only while its generation remains current.

        ``guard`` is an OPTIONAL caller-side predicate, ANDed with the ownership one:
        the cue scheduler passes its per-sentence generation so a newer sentence
        preempts the tail of an older one at the very next dequeue step. Neither guard
        can authorise motion the other has revoked, and a caller that passes none gets
        exactly the previous behaviour.
        """

        with self._legacy_motion_lock:
            if not self._legacy_motion_enabled:
                return False
            generation = self._legacy_motion_generation

        def owns() -> bool:
            with self._legacy_motion_lock:
                if not (self._legacy_motion_enabled
                        and generation == self._legacy_motion_generation):
                    return False
            if guard is None:
                return True
            try:
                return bool(guard())
            except Exception:  # noqa: BLE001 - an unreadable caller guard stays closed
                return False

        try:
            if yields is not None and isinstance(self.player, RobotPlayer):
                return bool(self.player.submit(intents, drop_if_busy=drop_if_busy, guard=owns,
                                               yields=yields))
            return bool(self.player.submit(intents, drop_if_busy=drop_if_busy, guard=owns))
        except TypeError:
            # Existing injected test players predate the guard argument.  They
            # execute synchronously, so checking immediately before submission
            # preserves the relevant ownership boundary in that compatibility
            # seam; production RobotPlayer always receives the dequeue guard.
            return bool(owns() and self.player.submit(intents, drop_if_busy=drop_if_busy))
        except Exception:  # noqa: BLE001 - a failed legacy cue is never fatal
            return False

    def shutdown(self, *, timeout_s: float = 1.0) -> bool:
        """Disable stale legacy motion and join bounded auxiliary workers."""

        self.set_legacy_motion_enabled(False)
        # A session cut off by a stop has no finalize: its row is closed at the next boot, and
        # how its sentences went is written here, if the lock comes free in time. A finalize
        # holds it across a broker call, and a stop has a limit where counts have none.
        session = None
        plan_counts: dict = {}
        if self._lock.acquire(timeout=max(0.0, timeout_s)):
            try:
                session = self.tracker.info if self.tracker.active else None
                plan_counts = self._plan_counts.take() if session is not None else {}
            finally:
                self._lock.release()
        session_id = getattr(session, "session_id", None)
        if type(session_id) is str:
            self.metrics.record_plan_counts(session_id, plan_counts)
        with self._fiware_threads_lock:
            workers = tuple(self._fiware_threads)
        deadline = time.monotonic() + max(0.0, timeout_s)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        return all(not worker.is_alive() for worker in workers)

    def set_session_finalized_callback(self, callback: Callable[[str], None] | None) -> None:
        """Bind one host-lifecycle notification without exposing session data."""

        self._on_session_finalized = callback if callable(callback) else None

    # --- session lifecycle -------------------------------------------------
    def start_session(self, info: SessionInfo) -> None:
        """Called when an SSO URL is minted. Finalizes any session still active
        (the previous learner walked away without finishing)."""
        replaced: Optional[str] = None
        with self._lock:
            if self.tracker.active and self.tracker.cycles > 0:
                log.info("new session while one active -> finalizing previous")
                self._finalize_locked(results=None, end_reason="preempted")
            elif self.tracker.active:
                # Replaced before its first cycle, so it never reaches a finalize: its row is
                # closed at the next boot, and how its sentences went is written below.
                replaced = getattr(self.tracker.info, "session_id", None)
            # Otherwise what is here was said between sessions, and belongs to neither.
            replaced_counts = self._plan_counts.take()
            self.tracker.start(info)
            motion_trace.set_session(info.session_id)
            self.translator = Translator(self._tcfg)
            self.engine = BehaviorEngine(self._bcfg)
            self.deduper.reset()
            # No cue plan, and no memory of the previous learner's affect, may reach
            # this session: the scheduler's generation bump invalidates anything still
            # queued from the one that just ended.
            self.cue.reset()
            self._cue_affect = AffectSnapshot()
            self._cue_open = False
            self.counters["sessions_started"] += 1
            # Fresh learner: re-arm presence (the latch is per-session, not per-process).
            self.last_presence = time.monotonic()
            self._body_ever_seen = False
            self.last_body_seen_at = None
            self.last_face_seen_at = None
            self.session_started_at = time.monotonic()
            # A robot-forced end belongs to the session that was running when it
            # was requested, never to this one.
            self._auto_end_reason = None
            self.end_requested_at = None
            self.frame_alive_at = None
            self._gesture_end_wanted = False
            self._turn_armed_seen = False
            # The previous learner's analysis can no longer be salvaged once a new
            # session owns the page.
            self._last_finalized = None
        # A new learner's first turn gets the first-turn grace. Outside _lock: the
        # watchdog has its own lock and the two are never held together.
        reset_watchdog = getattr(self.watchdog, "on_session_start", None)
        if callable(reset_watchdog):
            reset_watchdog(now=time.monotonic())
        if self._fiware_factory is not None:
            # Entity creation is 3 HTTP calls; never block the SSO redirect on it.
            self._start_fiware(info)
        self.submit_legacy_motion(self._ceremony_motion(
            GREETING_MOVE, R.greeting_gesture(reason="greeting (session start)"),
            reason="greeting (session start)"))
        # ...then LOOK UP and HOLD it. The greeting settles nose-down (+4), and before
        # the session the head sits wherever the last add-on pose or a human's
        # push-turn left it -- so without this the robot opens a conversation staring
        # at the learner's waist. Separate submit, NOT drop_if_busy: it must land after
        # the greeting batch rather than be dropped by it, and it goes through
        # submit_legacy_motion like every other training motion so an add-on mode
        # that owns the robot still suppresses it. Fixed pose on purpose: this is the
        # demo look-up, there is no face tracking.
        self.submit_legacy_motion(
            [R.gaze_to(0.0, pitch_deg=attend_up_pitch_deg(), duration=0.6,
                       reason="look up at the person (session start)")]
        )
        # ...and tell the robot node a person is here, so face look-at may run. The
        # look-up above is the FIXED demo pose; this is what replaces it with the
        # learner's actual face once one is detected, and the relax target if one
        # never is.
        #
        # This is also the drain for the preempt path above (a previous session
        # finalized inside the lock block): a full sync supersedes the deferred gaze
        # edge, and the answer it sends is True, because the replacement session is
        # already active. Turning the detector off and straight back on would have been
        # pure flag churn at the arbiter.
        self.sync_robot_flags()
        self.touch()
        # Telemetry LAST, and outside the lock. Outside the lock because the driver
        # lock is on the path of every cycle, gesture and watchdog tick and holding
        # it across any I/O at all is how this file grows its next latency bug;
        # last because this is three SD-card transactions (person + session + event)
        # and the repo's rule -- stated and honoured in WatchdogTicker and
        # _direction_with_metrics -- is control action first, telemetry after. The
        # greeting is the first thing the learner sees; it must not wait on a card.
        # Safe to defer: the two submits above are queue pushes to the player
        # thread, and the earliest a cycle can arrive for this session is the next
        # /api/cycle POST.
        if type(replaced) is str:
            self.metrics.record_plan_counts(replaced, replaced_counts)
        self.metrics.session_started(
            info.session_id, person_id=info.person_id, lang=info.lang,
            module_id=info.module_id, module_title=info.topic, topic=info.topic,
            is_roleplay=True, started_at=info.started_at,
        )

    def _fiware_start(self, info: SessionInfo) -> None:
        try:
            fw = self._fiware_factory(info)
            fw.ensure_session(info.topic)
            self.fiware = fw
        except Exception as e:  # noqa: BLE001 - FIWARE must never break the session
            log.warning("FIWARE session setup failed: %s", e)

    def _start_fiware(self, info: SessionInfo) -> None:
        def run() -> None:
            try:
                self._fiware_start(info)
            finally:
                with self._fiware_threads_lock:
                    self._fiware_threads.discard(threading.current_thread())

        worker = threading.Thread(target=run, name="fiware-session-start", daemon=True)
        with self._fiware_threads_lock:
            self._fiware_threads.add(worker)
        worker.start()

    # --- envelope handling ---------------------------------------------------
    def stamp(self, payload: dict) -> dict:
        """Stamp the active session identity + language onto an envelope.

        Every relayed envelope carries ids: downstream consumers (the ROS 2 live
        publisher, the recorder) must never have to guess. Without an active
        session the live defaults are used.
        """
        out = dict(payload)
        info = self.tracker.info
        if info is not None:
            out["session_id"] = info.session_id
            out["person_id"] = info.person_id
            out.setdefault("lang", info.lang)
            if info.topic:
                # the live envelope has no meta; the hostapp knows the chosen module
                out["meta"] = {**(out.get("meta") or {}), "topic": info.topic}
        else:
            out.setdefault("session_id", "live-session")
            out.setdefault("person_id", "learner_01")
            out.setdefault("lang", self.default_lang)
        return out

    def on_analysis(self, stamped: dict) -> dict:
        info = self.tracker.info
        try:
            cycle = cycle_from_envelope(
                stamped,
                session_id=info.session_id if info else None,
                person_id=info.person_id if info else None,
            )
        except Exception as e:  # noqa: BLE001 - count, log, never 500 the page
            self.counters["parse_errors"] += 1
            log.warning("unparseable analysis envelope: %s", e)
            return {"ok": False, "error": "unparseable envelope"}
        with self._lock:
            sig = self.translator.translate(cycle)
            intents = self.engine.step(sig)
            self.tracker.add(sig)
            # The learner the AI's next sentence will be talking to. Cheap, and read by
            # the cue planner off the hot path (a cue arrives ~0.3 s after a cycle).
            self._cue_affect = AffectSnapshot(
                valence=sig.valence, arousal=sig.arousal,
                escalation=sig.context.escalation,
                engagement_level=int(sig.engagement_level), fresh=True,
            )
            self.counters["cycles"] += 1
            if info is None:
                # Counted under the lock like every other counter (Counter's
                # increment is not atomic across threads); see the record_cycle
                # comment below for why these are not stored.
                self.counters["cycles_outside_session"] += 1
            if self.fiware is not None:
                try:
                    self.fiware.push(sig)
                except Exception:  # noqa: BLE001
                    pass
        # Presence signal: Pokamind emits body metrics only while it actually sees a
        # person, so a body-bearing cycle is proof someone is there; an empty one is not.
        has_body = bool(getattr(cycle, "body_language_metrics", None)) or bool(
            getattr(cycle, "body_language", None)
        )
        if has_body:
            self.mark_present()
        self._submit_engagement(intents, sig)
        self.touch()
        # Same nine metrics the FIWARE push above carries, written locally so the
        # dashboard survives a robot with no broker; it also emits the
        # attention_drop / recovery_success events behind the engagement-recovery KPI.
        #
        # AFTER the motion dispatch, and outside the driver lock. The rule is the
        # one WatchdogTicker._request_pokamind_end states: control action first,
        # telemetry after. record_cycle is an SD-card write (measured on the robot:
        # p50 0.14 ms, but 0.67 ms p95 on an engagement flap and up to 47 ms when
        # the run lands on a WAL autocheckpoint), so it does not sit between the
        # behaviour decision and the motion that expresses it.
        #
        # ONLY for a session the hostapp actually started. Cycles that arrive with
        # no active session (a kiosk page left open, Pokamind's own post-session
        # traffic) written under driver.stamp()'s 'live-session' placeholder -- or under
        # Pokamind's own id -- would produce a row in `cycles` with no row in `sessions`.
        # Those orphans are invisible to every aggregate
        # that joins sessions (cycles, attention_pct, avg_engagement) but their
        # attention_drop / recovery_success events were counted in the headline
        # recovery KPI, and RecoveryTracker never forgot the phantom id, so every
        # such cycle since boot was one endless "session". Half the recovery events
        # in the dev store came from it.
        if info is not None:
            self.metrics.record_cycle(info.session_id, sig)
        return {"ok": True, "n": self.counters["cycles"], "level": sig.engagement_level.name,
                "reasons": list(dict.fromkeys(i.reason for i in intents))}

    def on_utterance(self, stamped: dict) -> dict:
        text = str(stamped.get("text", "")).strip()
        try:
            seq = int(stamped.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        t_event = time.monotonic()
        authored = _cue_field(stamped, "cue_category")
        if self.deduper.is_duplicate(text, seq=seq, cue=(authored or "") if self._motion_sync else "",
                                     source=str(stamped.get("source") or "")):
            self.counters["utterances_deduped"] += 1
            motion_trace.emit("sentence", at=t_event, plan=0, seq=seq, chars=len(text),
                              authored=authored, deduped=True)
            return {"ok": True, "spoke": False, "deduped": True}
        # The AI is SPEAKING (audio out via the kiosk Chromium) -> the robot "speaks"
        # with talking gestures so it's visibly the one talking. The AI replying means
        # the prior user turn advanced normally: disarm the watchdog (outside _lock; the
        # watchdog has its own lock and we never nest the two).
        self.watchdog.on_ai_utterance(now=time.monotonic())
        beats = max(2, min(8, len(text) // 25)) if text else 3
        # Pokamind extracts the sentence's [family| direction] cue from the RAW text,
        # before the TTS ladder mutates it, and forwards the FAMILY here at that
        # sentence's playback onset (the brackets never leave the iframe, so nothing
        # here can leak one into speech). EVERY sentence now routes through the cue
        # scheduler: a tagged sentence gets its family's plan, an
        # untagged one inherits the turn's carried family at reduced amplitude, and
        # a turn with no family at all gets the seeded full-DOF talking flow --
        # because a reply is many sentences and Pokamind authors ONE cue per reply,
        # the old family-only routing left ~10 of 11 sentences on the fixed
        # antenna-flick loop (measured live, the "robot only moves its antennas"
        # report). Each plan preempts the previous sentence's tail as before. With
        # the EMORI_CUE_MOTION kill-switch off this is the talking gesture it has
        # always been, down to the beat count and the reason string.
        plan_events = self.plan_events
        if self._cue_config.motion:
            plan_id = motion_trace.begin_plan(wanted=plan_events is not None)
            try:
                result = self.cue.on_sentence(
                    text=text, cue_category=authored,
                    cue_body=_cue_field(stamped, "cue_body"),
                    lang=str(stamped.get("lang") or ""), affect=self._cue_affect,
                )
            finally:
                motion_trace.end_plan()
            motion_trace.emit("sentence", at=t_event, plan=plan_id, seq=seq, chars=len(text),
                              authored=authored, performed=result.family, slug=result.reason,
                              family_source=result.source, recorded=result.recorded,
                              planned=result.planned, budget=round(float(result.budget_s), 2),
                              intents=result.intents)
            if plan_events is not None:
                plan_events.put((
                    PLANNED, plan_id, seq, result.family if result.planned else "", authored,
                    result.source if result.planned else "", result.reason, t_event,
                    "" if result.planned else result.reason.partition("/")[0]))
            self._note_cue(result)
            # The family the robot PERFORMS, stamped onto the envelope the relay fans
            # out to the ROS 2 publisher (SpeechCue.cue_category) and the recorder. What
            # Pokamind forwarded stays in ``cue_category_authored``, because a direction
            # can name another family; when the robot read the family from the sentence
            # (or carried it from an earlier one) the wire says so in
            # ``cue_family_source`` -- so the ros4hri graph describes the motion that
            # actually happened, and an owner=ros engagement node would perform the
            # same family from the same message. Never the body, never the text here.
            if authored:
                stamped["cue_category_authored"] = authored
            if result.planned:
                stamped["cue_family"] = result.family
                stamped["cue_family_source"] = result.source
                stamped["cue_slug"] = result.reason
                stamped["cue_budget_s"] = round(float(result.budget_s), 2)
                if result.recorded:
                    stamped["cue_recorded"] = result.recorded
                if authored or result.family:
                    stamped["cue_category"] = result.family
            elif authored and result.reason == "head_reserved":
                # Nobody performs this one: the head is the learner's right now. A sentence
                # this lane did not plan because ROS owns the motion keeps its family, since
                # the ROS node is the one that performs it.
                stamped["cue_category"] = ""
        else:
            self.cue.preempt("no_cue")
            if plan_events is not None:
                plan_events.put((PLANNED, motion_trace.unused_plan_id(), seq, "", authored, "", "",
                                 t_event, CUE_MOTION_OFF))
            if self.expressive_motion_enabled:
                self.submit_legacy_motion(
                    R.talking_gesture(beats=beats, reason="speaking (AI turn)"))
        # The AI is speaking, so somebody is being spoken TO: re-assert both gates here
        # even though every transition that could have changed them already pushed one.
        # A missed edge (a transition that raced a node restart) would otherwise leave
        # the robot talking at the ceiling for the rest of the turn, and this is one
        # comparison against a cached bool on a path that already does three HTTP-scale
        # things.
        self.sync_robot_flags()
        with self._lock:
            self.tracker.utterances += 1
            self.counters["utterances"] += 1
        self.touch()
        return {"ok": True, "spoke": True, "beats": beats, "n": self.counters["utterances"]}

    # --- the two motion lanes ------------------------------------------------
    def _wake_player(self) -> None:
        wake = getattr(self.player, "wake", None)
        if callable(wake):
            wake()

    def _submit_engagement(self, intents: list, sig: DerivedSignals) -> None:
        """Per-cycle motion, arbitrated against the per-sentence cue lane.

        While a cue plan is running the head belongs to the sentence being spoken:
        two sources interleaving into one 12-slot queue is nonsense motion, and the
        ~2 s cycle lane is the one that can wait a sentence out. Everything else the
        cycle produces -- translation, metrics, FIWARE, presence -- still runs; only
        the motion is yielded.

        The single exception is escalation. An agitated learner outranks whatever the
        AI's voice is doing (the healthcare policy ``BehaviorEngine.step`` already
        applies on this lane), so that cycle preempts the cue and its calm settle goes
        through.
        """

        if not self.expressive_motion_enabled:
            # EMORI_ROS_MOTION_OWNER=ros|none: this lane is exactly what the ROS
            # engagement node recomputes and submits through /api/skill, so running
            # both is the double-drive trap the knob exists to prevent.
            with self._lock:
                self.counters["engagement_suppressed_by_owner"] += 1
            return
        if not self.cue.speaking and not getattr(self.player, "performing_speech", False):
            motion_trace.emit("engagement", action="submitted")
            # A sentence that arrives while this runs takes the head from it straight away.
            generation = self.cue.generation
            self.submit_legacy_motion(
                intents, drop_if_busy=True,
                yields=((lambda: self.cue.generation != generation)
                        if self._motion_sync else None))
            return
        if float(sig.context.escalation) >= self._cue_config.deescalate_at:
            motion_trace.emit("engagement", action="escalation")
            self.cue.preempt("escalation")
            with self._lock:
                self._cue_open = False
                self.counters["cue_preempted_by_escalation"] += 1
            # drop_if_busy=False on THIS one path: the preempt above cancelled every
            # queued cue batch as well as the executing one, so a dropped settle would
            # leave the head parked wherever the aborted plan last put it (for tension,
            # sunk toward the shell with drooped antennas) with nothing following it.
            # This is the one cycle that outranks whatever is already queued.
            self.submit_legacy_motion(R.superseding(intents), drop_if_busy=False)
            return
        motion_trace.emit("engagement", action="yielded")
        with self._lock:
            self.counters["engagement_yielded_to_speech"] += 1

    def _ceremony_motion(self, move: tuple, fallback: list, *, reason: str) -> list:
        """Greeting/farewell as one of Pollen's own recordings, when armed.

        These two are the bounded ceremony moments with no sentence budget to
        respect, so the official performance plays whole; the authored gesture
        remains the EMORI_CUE_RECORDED=0 path and the DryRun default.
        """

        if not self._cue_config.recorded:
            return fallback
        from emori_adapter.models import MotionIntent
        name, dur = move
        return [MotionIntent(kind="play_recorded", move=name, duration=float(dur),
                             reason="%s (recorded %s)" % (reason, name)),
                R.settle_to_attend(duration=0.4, reason=reason + " (settle)")]

    def _note_cue(self, result: CuePlanResult) -> None:
        """Counters + the one trace line per planned sentence."""

        with self._lock:
            # `speaking` is CuePlan.speaking: family plans and the expressive talking
            # flow claim the window and owe a settle at end of turn; the legacy
            # fallbacks (motion off / talk off / empty text) do neither.
            if result.planned and result.speaking:
                self._cue_open = True
            self.counters["cue_planned" if result.planned else "cue_suppressed"] += 1
        # WARNING because the robot ships at WARNING and this line is the on-robot proof
        # that the family distribution is real. A slug and three numbers -- never a
        # word the AI said, and the cue body never reaches a log line at all.
        log.warning("cue.plan family=%s intents=%d budget=%.2fs reason=%s",
                    result.family or "-", result.intents, result.budget_s, result.reason)

    def on_completed(self, stamped: dict) -> dict:
        """Pokamind reported the session complete: finalize with its analysis.

        What counts as 'completed' is the monthly-completion KPI, so the rule
        is worth stating exactly. The deployed UX is robot-driven: the learner never
        presses "finish", the WATCHDOG asks Pokamind to complete (walk-away, absence,
        a stalled turn) and Pokamind answers with a full completion envelope
        INCLUDING the analysis, the scores and the summary the learner then receives.
        Requiring a learner-driven ending therefore marked essentially every real
        session 'abandoned', which zeroed completion_pct, the module table and the
        period compare just as surely as the 100% it replaced.

        So: an envelope that arrived WITH results is a completed session however the
        ending was triggered -- the conversation produced its outcome. An envelope
        with NO results while the robot had an auto-end pending is the honest
        abandonment: the robot cut something short and nothing came back from it
        (``session_never_started`` above all). No envelope at all -- the dead-iframe
        local fail-safe in ``WatchdogTicker`` -- is still 'abandoned' via
        ``_finalize_locked``'s default.

        Status answers "did this session produce an outcome". WHO ended it is a
        second, independent fact, and it now travels ON THE ROW as ``end_reason``
        rather than only in an ``events`` row nothing renders: a walk-away that came
        back with an analysis is a completion AND a person who left the room, and
        the KPI page has to be able to say the second thing without joining events.
        The ``events`` row and the ``completed_after_auto_end`` counter are still
        written exactly as before.
        """

        with self._lock:
            if not self.tracker.active:
                # Nothing to finalize: the robot already closed this session locally
                # because the completion took longer than its grace. Such late
                # completions do carry performance_scores, so throwing them away would
                # lose every score.
                return self._late_completion_locked(stamped)
            self.counters["completed"] += 1
            # Read-and-clear under the driver lock, exactly as _request_pokamind_end
            # writes it: the completion handler must never observe a half-written
            # end request, and this session's reason must never colour the next one.
            reason, self._auto_end_reason = self._auto_end_reason, None
            requested_at = self.end_requested_at
            results = stamped.get("results")
            recorded = _has_results(results)
            if reason:
                self.counters["completed_after_auto_end"] += 1
                if not recorded:
                    self.counters["auto_end_without_results"] += 1
                # The one number that says whether EMORI_END_GRACE_TALKED_S is long
                # enough: how long Pokamind's finalize (flush + overview + analysis)
                # took to answer a robot-initiated end. Numbers only.
                log.warning("auto-end completed reason=%s after_s=%s results=%d",
                            reason, "-" if requested_at is None
                            else "%.1f" % (time.monotonic() - requested_at), int(recorded))
            failed = stamped.get("status") == "failed"
            gesture = self._gesture_end_wanted
            if failed:
                # Pokamind says its own finalize failed (the kiosk end chain posts this
                # with no results). Whoever ended it, no outcome came back: that is an
                # abandonment, never the 'completed' a result-less, reason-less
                # envelope would otherwise read as.
                self.counters["end_pokamind_failed"] += 1
            response = self._finalize_locked(
                results=results,
                # A person who ended it and got nothing back did not complete anything.
                status=("completed" if (recorded or not (reason or gesture)) and not failed
                        else "abandoned"),
                end_reason=reason or ("pokamind_failed" if failed
                                      else "gesture_end" if gesture and not recorded else ""),
            )
        # Outside the lock, before the answer goes back to Pokamind: the finalize
        # decided the face detector goes off and could not say so under _lock.
        self.push_deferred_flags()
        return response

    # --- turn-taking events (emori-gated; see hostapp/watchdog.py) -----------
    # Each bumps a Counter under driver._lock (Counter increment is not atomic
    # across threads) and feeds the watchdog OUTSIDE driver._lock (the watchdog
    # has its own lock; the two locks are never held at once -> no deadlock).
    def on_tts_ended(self, stamped: dict) -> dict:
        with self._lock:
            self.counters["tts_ended"] += 1
            settle, self._cue_open = self._cue_open, False
        motion_trace.emit("tts_ended", settle=settle)
        # The voice stopped: kill any cue tail and return to the attend pose, exactly
        # once per speaking run. Nothing is submitted for a session that never carried a
        # cue_category, so the no-cue path stays exactly as it was.
        if settle:
            self.cue.on_tts_ended()
        # AI finished its spoken turn => the turn is about to open. ARMED is inert.
        self.watchdog.on_armed(now=time.monotonic())
        self.touch()
        return {"ok": True}

    def on_turn_armed(self, stamped: dict) -> dict:
        with self._lock:
            self.counters["turn_armed"] += 1
            self._turn_armed_seen = True
            gesture_end = self._gesture_end_wanted
        if gesture_end:
            # The page refuses `end` until its first turn has opened, and says so to nobody
            # who is listening. Ask again each time a turn opens: it ignores a repeat.
            self.enqueue_cmd("end")
            self.arm_end_grace()
        # The mic is live and the user has the floor: open the watchdog window.
        self.watchdog.on_user_speaking(now=time.monotonic())
        # "Your turn" posture so the screenless user knows the mic is live. Distinct
        # from greeting_gesture (reserved for session start) -- a quieter attentive lean.
        self.submit_legacy_motion(R.listening_gesture(reason="your turn (mic armed)"))
        self.touch()
        return {"ok": True}

    def on_turn_submitted(self, stamped: dict) -> dict:
        with self._lock:
            self.counters["turn_submitted"] += 1
        self.watchdog.on_submitted(now=time.monotonic())
        # An accepted turn is a person speaking to the robot: presence, not ambience.
        # Clock only, no latch -- audio must never ARM absence-end (see refresh_presence).
        self.refresh_presence()
        # Acknowledge, in the only channel a screenless robot has left: the speaker is
        # reserved for the session's own voice by the TTS interlock, so this is motion.
        # Fires ONLY here, never in on_turn_rejected -- a blip that the low-content guard
        # threw away must not get a nod, or the robot appears to accept what it discarded.
        # drop_if_busy: a late acknowledgement is worse than none, and this must never
        # queue in front of the reply's own talking_gesture.
        self.submit_legacy_motion(R.thinking_gesture(reason="heard you (turn submitted)"),
                                  drop_if_busy=True)
        self.touch()
        return {"ok": True}

    def on_turn_rejected(self, stamped: dict) -> dict:
        with self._lock:
            self.counters["turn_rejected"] += 1
        # Rejected (e.g. too short): give the speaker a bounded second chance. A
        # rejection is the endpointer saying it heard something and threw it away,
        # and it can throw away a learner's whole answer (the filled-pause detector
        # once scored 1208 ms of speech as one long "uhh"), after which the turn would
        # run out its ceiling and the robot would end the session on someone who was
        # talking to it. The watchdog now refreshes its stall
        # clock for this -- at most `reject_extensions` times per turn, and never
        # past the absolute ceiling, so an empty room still ends.
        # And deliberately NO self.touch() either: a guard-rejected blip
        # is ambient sound the endpointer itself threw away -- a chair, a breath, the
        # robot's own motors -- exactly the category on_user_speaking_activity already
        # keeps off the walk-away clock. Measured live: a silent room produced a
        # reject every ~7 s all session, and each one re-armed the 45 s walk-away, so
        # a session nobody was in could not end by walking away. Presence is a body
        # in the camera or an ACCEPTED turn, never a rejected one.
        self.watchdog.on_turn_rejected(now=time.monotonic())
        return {"ok": True}

    def on_user_speaking_activity(self, stamped: dict) -> dict:
        with self._lock:
            self.counters["user_speaking_activity"] += 1
        # Throttled speaking-onset relay from the kiosk page: refresh the watchdog's
        # stall clock so a long answer is never force-ended mid-sentence.
        # Deliberately NO self.touch() -- ambient VAD is not presence, so a noisy
        # room must not keep resetting the session-level walk-away clock.
        self.watchdog.on_activity(now=time.monotonic())
        return {"ok": True}

    def _finalize_locked(self, results, *, status: str = "abandoned",
                         end_reason: str = "") -> dict:
        """Finish the active session: farewell gesture, summary, FIWARE wrap-up.

        ``status`` is what the dashboard's monthly-completion KPI counts, so it
        defaults to the HONEST answer: only the caller that saw Pokamind report the
        session complete (``on_completed``, which decides on the evidence in that
        envelope) may claim 'completed'. Everything else reaching here ended with
        nothing coming back from Pokamind -- the dead-iframe local fail-safe, a new
        learner preempting one who walked off, an iframe that never started -- and
        those are abandonments whatever triggered them.

        ``end_reason`` is the caller's own name for the ending, and it is stored
        beside the status so a robot-driven ending stays visible after the fact. A
        watchdog auto-end that is still PENDING wins over it: the walk-away that
        asked for this ending is a truer answer than the fallback that executed it.
        Empty on both -> NULL on the row -> the learner drove the ending.
        """

        stats = self.tracker.finish()
        motion_trace.set_session(None)
        # Before the reset below: the pending auto-end IS this session's ending, and
        # clearing it first is how the row would lose the only record of who ended it.
        reason = self._auto_end_reason or str(end_reason or "")
        self._gesture_end_wanted = False
        self._turn_armed_seen = False
        # This session is over however it got here; a pending auto-end must not
        # colour the NEXT one's status, and a grace clock left armed would fire the
        # local fail-safe at the session that starts next.
        self._auto_end_reason = None
        self.end_requested_at = None
        self.session_started_at = None
        self.frame_alive_at = None
        # No sentence's motion may outlive the session it decorated: the generation
        # bump invalidates any queued cue tail before the farewell claims the head.
        self.cue.on_session_end()
        self._cue_open = False
        self._cue_affect = AffectSnapshot()
        self.submit_legacy_motion(self._ceremony_motion(
            FAREWELL_MOVE, R.farewell_gesture(reason="farewell (session end)"),
            reason="farewell (session end)"))
        # The session is over: tracker.active is already False, so this is the edge that
        # switches the face detector off. Without it an ended session would leave the
        # robot tracking whoever walks past an idle kiosk -- fail-closed is the whole
        # privacy argument, so it must not wait for the next session.
        #
        # RECORDED, not sent: this runs with _lock held and a gate POST is network I/O.
        # Every caller drains it with push_deferred_flags() the statement after its lock
        # block ends, and the 10 Hz reconciler drains it too. See push_deferred_flags.
        self._gaze_push_pending = True
        session = stats.get("session")
        session_id = getattr(session, "session_id", None)
        # Close the local row FIRST so the recovery counts below are complete, and so a
        # dashboard refresh landing during a slow FIWARE write already sees the session
        # as finished rather than stuck 'active'.
        #
        # These two calls run with _lock held, which start_session deliberately avoids
        # for its own metrics write. They are bounded here rather than hoisted:
        # session_finished is one short UPDATE against the write lock (contended only
        # by other single-statement writes) and recovery_counts is a SELECT that
        # MetricsStore.query answers on a handle nobody else holds -- a second
        # connection was not enough on its own, because ONE read lock around every
        # read meant this SELECT waited out a manager's 90-day aggregate (measured
        # 117.6 ms with driver._lock held throughout) and that is precisely how this
        # became a latency bug. Hoisting them would mean deferring work out of a
        # function both callers invoke with the lock already held; the FIWARE HTTP
        # write below is the larger remaining item and is tracked with it.
        recovery = {"attempts": 0, "successes": 0}
        if type(session_id) is str:
            self.metrics.session_finished(
                session_id, avg_engagement=stats.get("avg_engagement"),
                dominant_style=str(stats.get("dominant_style") or ""),
                status=status, results=results, end_reason=reason,
            )
            # The session's one write of these: one short transaction, like the UPDATE above.
            self.metrics.record_plan_counts(session_id, self._plan_counts.take())
            recovery = self.metrics.recovery_counts(session_id)
        fw, self.fiware = self.fiware, None
        if fw is not None:
            try:
                fw.write_summary(stats["summary"], stats["avg_engagement"],
                                 recovery_attempts=recovery["attempts"],
                                 recovery_successes=recovery["successes"])
                fw.close()
            except TypeError:
                # An injected/legacy client (tests, the ROS bridge's stub) predates the
                # recovery arguments. The summary still matters more than the counters.
                try:
                    fw.write_summary(stats["summary"], stats["avg_engagement"])
                    fw.close()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        self.translator = Translator(self._tcfg)
        self.engine = BehaviorEngine(self._bcfg)
        self.deduper.reset()
        stats["results"] = results
        if type(session_id) is str:
            self._last_finalized = (session_id, time.time(), time.monotonic(), dict(stats))
        callback = self._on_session_finalized
        if type(session_id) is str and callable(callback):
            try:
                callback(session_id)
            except Exception:  # noqa: BLE001 - lifecycle notification is best-effort
                pass
        return stats

    def _late_completion_locked(self, stamped: dict) -> dict:
        """A completion for a session that is already closed. Caller holds ``_lock``.

        Salvaged only when it carries results and the last finalized session is recent
        (``EMORI_LATE_COMPLETION_S``): the row keeps its ended_at and end_reason and gains
        the status and scores the conversation actually earned. Nothing else of a
        finalize runs again -- no second farewell, no reset of the NEXT session's state.
        Anything else is ignored and said so.
        """

        results = stamped.get("results")
        last = self._last_finalized
        if (last is not None and _has_results(results)
                and stamped.get("status") != "failed"
                and time.monotonic() - last[2] <= self._late_completion_s):
            session_id, ended_wall, ended_mono, stats = last
            self._last_finalized = None       # salvaged once
            self.metrics.session_finished(session_id, ended_at=ended_wall,
                                          status="completed", results=results)
            self.counters["late_completion_salvaged"] += 1
            log.warning("late completion salvaged after_s=%.1f",
                        time.monotonic() - ended_mono)
            salvaged = dict(stats)
            salvaged["results"] = results
            salvaged["late"] = True
            return salvaged
        self.counters["late_completion_ignored"] += 1
        return {"ignored": "no_active_session"}

    def arm_end_grace(self) -> None:
        """Start the dead-iframe grace clock for an ``end`` already queued elsewhere.

        Idempotent: the FIRST request owns the clock, so a person nudging the head
        repeatedly cannot keep pushing the fail-safe out of reach.  ``WatchdogTicker``
        arms it inline for its own triggers; this is the entry point for the ones it
        does not own (the training end gesture).
        """

        with self._lock:
            if self.end_requested_at is None:
                self.end_requested_at = time.monotonic()

    def request_gesture_end(self) -> bool:
        """A person asked to end this session with a head gesture. False if they already had.

        A page still playing its opening refuses `end`, and the grace clock would then stand
        every other watchdog down and finalize later with no analysis, even if the person
        carried on talking. So once the frame is up, the request waits for the first turn
        (on_turn_armed). A frame that never came up has nothing to wait for, and gets the
        same grace clock as any other end.
        """

        with self._lock:
            first = not self._gesture_end_wanted
            self._gesture_end_wanted = True
            ready = self._turn_armed_seen or self.frame_alive_at is None
        if first and ready:
            self.enqueue_cmd("end")
            self.arm_end_grace()
        return first

    def abort_unstarted_session(self, reason: str) -> bool:
        """Finalize a session whose Pokamind iframe never came up.

        ONLY for a session that has exchanged NOTHING: once the tracker has seen a
        cycle or a spoken turn there is a real conversation in front of the robot and
        this must never touch it (``SessionTracker.exchanged`` -- cycles alone is the
        camera stream only, and an audio-only conversation has none).  Returns
        whether it acted, so the caller (and its test) can tell a real abort from the
        idempotent second call -- the kiosk may POST this more than once, and a second
        finalize would end whatever session started in between.
        """

        with self._lock:
            if not (self.tracker.active and not self.tracker.exchanged):
                return False
            self.counters["session_start_failed"] += 1
            info = self.tracker.info
            log.warning("session start failed (%s); finalizing an unstarted session", reason)
            self._finalize_locked(results=None, end_reason="session_start_failed")
        # The gate edge the finalize could only record, sent now the lock is gone.
        self.push_deferred_flags()
        # Telemetry after the control action, outside the lock: the same rule
        # start_session and WatchdogTicker._request_pokamind_end already follow.
        self.metrics.log_event("session_start_failed",
                               session_id=getattr(info, "session_id", "") or "")
        return True

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def mark_present(self) -> None:
        """A body is verifiably in front of the robot (CAMERA body analysis).

        Refreshes the presence clock AND arms the ``_body_ever_seen`` latch. Only
        real camera evidence may arm it: the latch is the whole fail-safe behind
        "audio-only sessions fall through to EMORI_WALKAWAY_S" (docs/configuration.md,
        ``EMORI_ABSENCE_S``),
        because nothing but the camera can keep the clock refreshed afterwards.
        """
        self.last_presence = time.monotonic()
        self.last_body_seen_at = self.last_presence
        self._body_ever_seen = True

    def note_face_seen(self) -> None:
        """The robot node's own face tracker has a face (``motion.gaze_active``).

        Camera evidence for the conversation watchdog only: it neither arms the
        absence-end latch nor touches the walk-away clock (a poster on the wall must
        not be able to keep an empty kiosk alive past the watchdog's hard caps)."""
        if self.tracker.active:
            self.last_face_seen_at = time.monotonic()

    def camera_seen_recently(self, now: float, *, body_window_s: float,
                             face_window_s: float) -> bool:
        """True when a body (Pokamind) or a face (robot node) was seen recently."""
        body, face = self.last_body_seen_at, self.last_face_seen_at
        return bool((body is not None and now - body <= body_window_s)
                    or (face is not None and now - face <= face_window_s))

    def refresh_presence(self) -> None:
        """Non-camera evidence that someone is still there (an accepted turn).

        Refreshes the presence clock ONLY -- deliberately does not arm the latch.
        An accepted turn can suppress a camera false-negative during a real answer,
        but it must never *enable* absence-end: a camera-less session emits empty
        body fields forever, so a latch armed from audio would absence-end the
        learner mid-answer (only turns refresh the clock, and those are 25-60s apart).
        """
        self.last_presence = time.monotonic()

    def status(self) -> dict:
        info = self.tracker.info
        return {
            "counters": dict(self.counters),
            "session": ({"session_id": info.session_id, "person_id": info.person_id,
                         "lang": info.lang, "topic": info.topic, "cycles": self.tracker.cycles}
                        if info else None),
            "fiware": getattr(self.fiware, "enabled", False),
            "idle_for_s": round(time.monotonic() - self.last_activity, 1),
            "last_present_s": round(time.monotonic() - self.last_presence, 1),
            "body_ever_seen": self._body_ever_seen,
            "body_seen_s": (None if self.last_body_seen_at is None
                            else round(time.monotonic() - self.last_body_seen_at, 1)),
            "face_seen_s": (None if self.last_face_seen_at is None
                            else round(time.monotonic() - self.last_face_seen_at, 1)),
            "watchdog": (self.watchdog.snapshot(now=time.monotonic())
                         if callable(getattr(self.watchdog, "snapshot", None)) else None),
        }


class IdleTicker:
    """Background thread: when no cycle has arrived for ``idle_after_s``, submit a
    gentle scan/breathing pose every ``tick_s`` so the robot never freezes mid-session
    (stream stall, learner looked away) or between sessions."""

    def __init__(
        self,
        driver: LiveDriver,
        *,
        idle_after_s: float = 8.0,
        tick_s: float = 5.0,
        head_reserved: Optional[Callable[[], bool]] = None,
        still_while_listening: bool = False,
    ) -> None:
        self.driver = driver
        self.idle_after_s = idle_after_s
        self.tick_s = tick_s
        # Hold the body still while the learner has the floor (EMORI_LISTEN_STILL).
        # Every servo move is broadband noise at the head's own microphone - measured
        # on the robot at -26..-29 dBFS in the 4-8 kHz consonant band, as loud
        # as a learner at 80 cm - and the endpointer is listening to exactly that band.
        # Gaze keeps looking at the learner, so the robot is not frozen, just quiet.
        self.still_while_listening = bool(still_while_listening)
        # When this says the head is currently an INPUT device (push-turn
        # selection), the idle scan stands down. See _tick.
        self._head_reserved = head_reserved
        self._seq = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="idle-ticker", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, *, timeout_s: float = 1.0) -> bool:
        if threading.current_thread() is self._t:
            return False
        self._t.join(timeout=max(0.0, timeout_s))
        return not self._t.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            self._tick()

    def _tick(self) -> None:
        if time.monotonic() - self.driver.last_activity < self.idle_after_s:
            return
        if self._head_is_reserved():
            # The head is the INPUT device right now (push-turn selection), and
            # look_around swings yaw +-18 deg -- wider than the 0.20 rad
            # selection threshold. Worse, ANY motion sets player.busy, which
            # wipes the detector's rolling baseline; the next settled tick then
            # re-anchors it onto wherever the head was left, silently absorbing
            # a human's turn. Measured on the robot: 12 baseline resets a minute,
            # which made the head turn essentially undetectable. Standing still
            # while waiting for input is the correct behaviour; an add-on
            # idle pose may already hold the robot in a deliberate posture.
            return
        if self.still_while_listening and self._listening():
            self.driver.counters["idle_held_listening"] += 1
            return
        self._seq += 1
        self.driver.counters["idle_ticks"] += 1
        if self._listening() or self._awaiting_reply():
            # The learner has the floor (the mic is armed / their turn is open) and is
            # thinking or speaking: an 18-degree left-right scan reads as "not
            # listening", and it was the one robot motion that ran while the mic was
            # open. Hold an attentive pose with a slow breath
            # instead -- alive, still listening, and quiet.
            # Also while the learner's answer is on its way to the AI: the reply can take
            # 5-18 s, and an 18-degree scan in that gap reads as a robot that stopped
            # caring the moment they finished speaking.
            self.driver.submit_legacy_motion(
                [R.attentive_breath(self._seq, reason="idle: listening")], drop_if_busy=True
            )
            return
        self.driver.submit_legacy_motion(
            [R.look_around(self._seq, reason="idle: stay alive")], drop_if_busy=True
        )

    def _listening(self) -> bool:
        """The user's turn is ARMED or OPEN (the conversation watchdog's state)."""

        return self._watchdog_state() in ("ARMED", "OPEN")

    def _awaiting_reply(self) -> bool:
        """The learner's turn was submitted and the AI has not started answering."""

        return self._watchdog_state() == "SUBMITTED"

    def _watchdog_state(self) -> str:
        watchdog = getattr(self.driver, "watchdog", None)
        state = getattr(watchdog, "state", None)
        return getattr(state, "name", "")

    def _head_is_reserved(self) -> bool:
        predicate = self._head_reserved
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - an unknown state keeps the legacy behaviour
            return False


# The adapter-side session-start deadline's code default (the /session page's own is
# server._SESSION_START_MS_DEFAULT). A Reachy Mini Wireless gets its own from the
# hardware profile (site_settings.HARDWARE_PROFILES).
_SESSION_START_S_DEFAULT = 45.0


def _session_start_s() -> float:
    """``EMORI_SESSION_START_S``: empty, invalid or negative is the code default; 0 = off.

    Empty is a value now: ``.env.example`` ships the line blank so the hardware profile
    can fill it, and on a box with no profile the reader must not fail on the blank.
    """

    raw = (os.environ.get("EMORI_SESSION_START_S", "") or "").strip()
    if not raw:
        return _SESSION_START_S_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning("EMORI_SESSION_START_S=%r is not a number; using %s",
                    raw, _SESSION_START_S_DEFAULT)
        return _SESSION_START_S_DEFAULT
    if not math.isfinite(value) or value < 0:
        log.warning("EMORI_SESSION_START_S=%s is not a duration (0 = off); using %s",
                    value, _SESSION_START_S_DEFAULT)
        return _SESSION_START_S_DEFAULT
    return value


class WatchdogTicker:
    """Polls the ConversationWatchdog every ``tick_s``; on 'reprompt' nudges the
    robot, on 'force_end' finalizes the stuck session so the kiosk never wedges
    silent. Mirrors :class:`IdleTicker` exactly (fresh Event + fresh Thread per
    instance -- Thread.start() is single-shot -- daemon, stop() exits the loop).

    Lock discipline: ``watchdog.due`` takes/releases the watchdog's OWN lock
    internally; only AFTER it returns do we acquire ``driver._lock`` for
    finalize, so the two locks are never held at once (no AB/BA deadlock). The
    force_end branch is GUARDED on ``tracker.active`` so a turn that already
    advanced (on_ai_utterance -> IDLE) or a session that already ended can never
    trigger a spurious finalize."""

    def __init__(self, driver: "LiveDriver", *, tick_s: float = 1.0,
                 walkaway_s: Optional[float] = None,
                 absence_s: Optional[float] = None,
                 end_grace_s: Optional[float] = None,
                 session_start_s: Optional[float] = None,
                 end_grace_talked_s: Optional[float] = None,
                 spoken_reprompt: Optional[bool] = None) -> None:
        self.driver = driver
        self.tick_s = tick_s
        # Walk-away end: if a session is active but NOTHING has happened (no analysis
        # cycle, utterance, or turn event) for this long, the learner has left -> end
        # gracefully so the kiosk frees up. Pokamind streams ~2s cycles while a face is
        # present, so silence here means absence. Env-tunable; 0 disables.
        self.walkaway_s = (walkaway_s if walkaway_s is not None
                           else float(os.environ.get("EMORI_WALKAWAY_S", "45")))
        # Presence-based walk-away: the learner is verifiably GONE (no body in the
        # camera analysis for this long, after at least one body this session) -> end
        # much sooner than the blind inactivity timer above. 0 disables, and 0 is the
        # DEFAULT: this is a new session-ending path, so it stays inert until a human
        # sets EMORI_ABSENCE_S (20 is the suggested starting value, to be tuned on-robot
        # against a learner who looks down at notes mid-answer). Until then a departed
        # learner still ends via the blind EMORI_WALKAWAY_S timer as before.
        self.absence_s = (absence_s if absence_s is not None
                          else float(os.environ.get("EMORI_ABSENCE_S", "0")))
        # When an auto-end fires (walk-away or a stalled-turn force-end) we FIRST ask
        # Pokamind to complete the session (emori_cmd:end -> stopRecording ->
        # pokamind_session_completed WITH results) so the learner's analysis + spoken
        # summary + email are produced. A bare local finalize would end the session but
        # throw the analysis away. If Pokamind never completes within end_grace_s (a dead
        # iframe), we finalize locally as a fail-safe so the kiosk never wedges.
        self.end_grace_s = (end_grace_s if end_grace_s is not None
                            else float(os.environ.get("EMORI_END_GRACE_S", "12")))
        # ...but once the session has EXCHANGED anything, Pokamind finishes its own
        # end-of-session work, the analysis among it, BEFORE it posts
        # pokamind_session_completed, and that can take minutes. A 12 s grace then
        # `home`s the page and destroys that iframe mid-analysis: robot-initiated ends
        # would fall back locally at exactly +12.0 s. The short grace stays for a session
        # that never exchanged (a dead iframe has nothing to finish). An explicit
        # end_grace_s with no explicit talked grace keeps one value (tests).
        if end_grace_talked_s is not None:
            self.end_grace_talked_s = end_grace_talked_s
        elif end_grace_s is not None:
            self.end_grace_talked_s = end_grace_s
        else:
            self.end_grace_talked_s = float(os.environ.get("EMORI_END_GRACE_TALKED_S", "150"))
        # Presence for the conversation watchdog: CAMERA evidence only.
        self.body_window_s = float(os.environ.get("EMORI_TURN_BODY_WINDOW_S", "30"))
        self.face_window_s = float(os.environ.get("EMORI_TURN_FACE_WINDOW_S", "8"))
        # Also ask the Pokamind iframe to SPEAK the re-prompt in the session's own
        # voice (emori_cmd:reprompt; the iframe hard-mutes its mic while it speaks).
        # Off by default: an app version that does not understand it ignores unknown
        # commands, so turning it on early is a no-op, not a fault.
        self.spoken_reprompt = (spoken_reprompt if spoken_reprompt is not None
                                else os.environ.get("EMORI_REPROMPT_SPOKEN", "0") == "1")
        # On the spoken path the gesture waits for the iframe's 'reprompt-start' (its
        # mic is muted by then: servo noise measured as loud as a learner at 80 cm must
        # not land on an open mic) -- or this long, for a bundle that ignored the verb.
        self._gesture_lock = threading.Lock()
        self._gesture_due_at: Optional[float] = None
        self.gesture_fallback_s = 4.0
        # Belt-and-braces for the /session page's own start deadline: the session was
        # opened, no cycle ever arrived, and this long has passed. The page normally
        # reports that itself (POST /api/session-start-failed) -- this covers the kiosk
        # that cannot even reach its own origin. 0 disables; NEVER fires once cycles > 0.
        self.session_start_s = (session_start_s if session_start_s is not None
                                else _session_start_s())
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="watchdog-ticker", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, *, timeout_s: float = 1.0) -> bool:
        if threading.current_thread() is self._t:
            return False
        self._t.join(timeout=max(0.0, timeout_s))
        return not self._t.is_alive()

    def _absence_due(self, now: float) -> bool:
        """Person verifiably gone: a body was seen this session and none for absence_s."""
        return (
            self.absence_s > 0
            and getattr(self.driver, "_body_ever_seen", False)
            and now - getattr(self.driver, "last_presence", now) >= self.absence_s
        )

    def _request_pokamind_end(self, counter: str) -> None:
        """Ask Pokamind to complete the session (so its analysis is captured), once.

        Queues the ``end`` command the kiosk /session page relays as ``emori_cmd:end``
        -> stopRecording -> ``pokamind_session_completed`` (with results) -> the driver
        finalizes WITH the analysis. ``touch()`` refreshes the activity clock so the
        walk-away trigger doesn't re-fire while we wait for the completion -- but only
        once the conversation is REAL: before the first cycle there is nothing to
        protect from re-firing, and refreshing the clock there is what let a nudged
        head keep postponing the only recovery a never-started session had."""
        if self.driver.end_requested_at is not None:
            return
        with self.driver._lock:
            self.driver.counters[counter] += 1
            info = self.driver.tracker.info
            # Remember WHY, for the completion Pokamind will send back: it looks
            # exactly like a learner finishing normally, and this is the only
            # evidence that it was not (see LiveDriver.on_completed). Set under
            # the driver lock, beside the counter, so the completion handler can
            # never observe a half-written end request.
            self.driver._auto_end_reason = counter
        # Control action FIRST, telemetry after. The dashboard's own rule is that
        # metrics must never cost a conversation, and log_event is an SD-card write:
        # between the counter above and the command below it would delay the actual
        # "ask Pokamind to finish" by however long the card takes, and leave a window
        # in which the driver reports an ending it has not yet requested.
        self.driver.enqueue_cmd("end")
        self.driver.arm_end_grace()
        if self.driver.tracker.cycles > 0:
            self.driver.touch()
        # Robot-side endings are the operationally interesting ones (a learner who
        # walked away vs a turn that stalled): the counters are process-local and
        # reset on restart, the event row is what the dashboard can trend.
        self.driver.metrics.log_event(
            counter, session_id=getattr(info, "session_id", "") or "")

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            now = time.monotonic()
            if not self.driver.tracker.active:
                # Session ended (Pokamind completed WITH analysis, or a prior fallback).
                self.driver.end_requested_at = None
                with self._gesture_lock:
                    self._gesture_due_at = None
                continue
            self._flush_deferred_gesture(now)
            # Fail-safe: we asked Pokamind to complete but it never did (dead iframe) ->
            # finalize locally so the kiosk never wedges. results=None here (no analysis),
            # but only reached when Pokamind is unreachable.
            end_requested_at = self.driver.end_requested_at
            grace = (self.end_grace_talked_s if self.driver.tracker.exchanged
                     else self.end_grace_s)
            if (end_requested_at is not None
                    and now - end_requested_at >= grace):
                with self.driver._lock:
                    if self.driver.tracker.active:
                        self.driver.counters["end_local_fallback"] += 1
                        # The pending walk-away/absence reason wins inside
                        # _finalize_locked; this names the ending only when the
                        # end request itself came from somewhere else.
                        self.driver._finalize_locked(
                            results=None,
                            end_reason=("gesture_end" if self.driver._gesture_end_wanted
                                        else "end_local_fallback"))
                # Outside the driver lock, as the finalize's own comment requires.
                self.driver.push_deferred_flags()
                self.driver.end_requested_at = None
                self.driver.touch()
                continue
            # An end is already pending Pokamind's completion -> don't stack more triggers.
            if end_requested_at is not None:
                continue
            # The session opened and Pokamind never came up: NOTHING has ever arrived --
            # no analysis cycle and no spoken turn. First, because it is the most
            # specific reason -- the blind walk-away timer would otherwise claim the same
            # window and report the wrong thing. Guarded on `exchanged`, not on cycles
            # alone: cycles come only from the camera analysis stream, so a session that
            # is audibly conversing with a quiet camera would otherwise be killed here.
            # frame_alive_at: the page relayed at least one pokamind_* message,
            # so the frame is provably alive even though nothing has EXCHANGED
            # yet (scenario generation + device grants + the first spoken turn
            # all precede the first cycle/utterance).  Killing here on a live
            # frame would end a healthy boot at +57s with no log line.  A
            # live-but-stuck frame is still bounded by the
            # walk-away and per-turn watchdogs below.
            if (self.session_start_s > 0
                    and not self.driver.tracker.exchanged
                    and self.driver.frame_alive_at is None
                    and self.driver.session_started_at is not None
                    and now - self.driver.session_started_at >= self.session_start_s):
                self._request_pokamind_end("session_never_started")
                continue
            # Presence walk-away: the camera stopped seeing anyone -> end WITH analysis.
            # Checked before the blind inactivity timer because it is the stronger signal.
            if self._absence_due(now):
                self._request_pokamind_end("presence_ends")
                continue
            # Walk-away end (session-level inactivity): no activity for walkaway_s -> ask
            # Pokamind to complete so we capture the analysis (distinct from the per-turn
            # watchdog below). NOT while the session is still BOOTING: on a slow network
            # the iframe's JS can take >45s, and this timer would kill every attempt at
            # exactly open+45s -- "the session froze then said session complete"). Nobody can walk
            # away from a conversation that never started: until the frame is alive or something has
            # exchanged, the pre-boot window belongs to the session-start deadline above (and the
            # page's own), which report the honest reason. With session_start_s disabled the old
            # behaviour stands, so a robot cannot configure itself into an unbounded boot.
            booting = (self.session_start_s > 0
                       and not self.driver.tracker.exchanged
                       and self.driver.frame_alive_at is None
                       and self.driver.session_started_at is not None
                       and now - self.driver.session_started_at < self.session_start_s)
            # ...and NOT while the learner has the floor. An OPEN turn is
            # owned by the conversation watchdog, whose own ceilings (silent stall,
            # unseen ceiling, present cap) already bound an empty room -- and nothing
            # during a quiet turn touches this clock, so it would otherwise end the
            # longer first-turn / present-learner windows at the last re-prompt + 45 s.
            wd = self.driver.watchdog
            turn_open = (getattr(getattr(wd, "state", None), "name", "") == "OPEN"
                         and bool(getattr(wd, "bounded", False)))
            if (self.walkaway_s > 0 and not booting and not turn_open
                    and now - self.driver.last_activity >= self.walkaway_s):
                self._request_pokamind_end("walkaway_ends")
                continue
            present = self._present(now)
            action = wd.due(now=now, present=present)
            if action == "reprompt":
                self._reprompt(now, present)
            elif action == "force_end":
                snap = self._snapshot(now)
                kind = str(snap.get("last_end_kind") or "silent")
                with self.driver._lock:
                    self.driver.counters["watchdog_end_" + kind] += 1
                # The verdict in one line, numbers only: which ceiling, which turn, and
                # the evidence the decision was made on. WARNING: the robot ships at INFO
                # but this is the line a "why did it end?" question starts from.
                log.warning("watchdog end kind=%s first_turn=%d open_s=%s quiet_s=%s "
                            "rejects=%s activity=%s reprompts=%s present=%d",
                            kind, int(bool(snap.get("first_turn"))), snap.get("open_s"),
                            snap.get("quiet_s"), snap.get("rejects"), snap.get("activity"),
                            snap.get("reprompts"), int(present))
                # A turn stalled past a ceiling: ask Pokamind to complete (analysis),
                # falling back to a local finalize only if it never answers.
                self._request_pokamind_end("watchdog_force_ends")

    def _reprompt_gesture(self) -> None:
        self.driver.submit_legacy_motion(R.reprompt_gesture(reason="watchdog: re-prompt"))

    def on_reprompt_stage(self, stage: str) -> None:
        """/api/emori-debug relayed the iframe's spoken re-prompt lifecycle."""
        with self._gesture_lock:
            if self._gesture_due_at is None:
                return
            self._gesture_due_at = None
        with self.driver._lock:
            self.driver.counters["reprompt_" + stage.replace("reprompt-", "")] += 1
        self._reprompt_gesture()

    def _flush_deferred_gesture(self, now: float) -> None:
        with self._gesture_lock:
            due = self._gesture_due_at
            if due is None or now < due:
                return
            self._gesture_due_at = None
        with self.driver._lock:
            self.driver.counters["reprompt_unacked"] += 1
        self._reprompt_gesture()

    def _present(self, now: float) -> bool:
        seen = getattr(self.driver, "camera_seen_recently", None)
        if not callable(seen):
            return False
        try:
            return bool(seen(now, body_window_s=self.body_window_s,
                             face_window_s=self.face_window_s))
        except Exception:  # noqa: BLE001 - unknown presence = not seen (bounded path)
            return False

    def _snapshot(self, now: float) -> dict:
        snap = getattr(self.driver.watchdog, "snapshot", None)
        try:
            return snap(now=now) if callable(snap) else {}
        except Exception:  # noqa: BLE001 - telemetry never blocks the control action
            return {}

    def _reprompt(self, now: float, present: bool) -> None:
        """'Take your time, I'm listening' -- in a form the learner can perceive.

        MOTION: an authored lean-in/tilt with an antenna flick, NOT a recorded move.
        On the v2 node every goto head pose is composed onto the learner's face aim
        (arbiter._anchored), but play_recorded is not (arbiter._execute), so the old
        Pollen `inquiring2` played about neutral and turned the robot AWAY from a
        learner standing to one side -- the opposite of "I'm listening to you".
        VOICE (EMORI_REPROMPT_SPOKEN=1): the iframe speaks the line in the session's
        own voice with its mic hard-muted (the TTS interlock's rule -- only the
        session's voice during a roleplay -- still holds). Advisory: never displaces
        a pending begin/end.
        """
        with self.driver._lock:
            self.driver.counters["watchdog_reprompts"] += 1
        spoken = False
        if self.spoken_reprompt:
            enqueue = getattr(self.driver, "enqueue_cmd_if_empty", None)
            spoken = bool(enqueue("reprompt")) if callable(enqueue) else False
        if spoken:
            with self._gesture_lock:
                self._gesture_due_at = now + self.gesture_fallback_s
        else:
            self._reprompt_gesture()
        snap = self._snapshot(now)
        log.warning("watchdog reprompt n=%s first_turn=%d open_s=%s quiet_s=%s "
                    "present=%d spoken=%d", snap.get("reprompts"),
                    int(bool(snap.get("first_turn"))), snap.get("open_s"),
                    snap.get("quiet_s"), int(present), int(spoken))
        self.driver.touch()


class GestureTicker:
    """Physical head gestures for training and idle directional selection.

    Polls the daemon's reported head pose (``client.read_state`` -> /api/state/full)
    and runs a :class:`GestureDetector`. A human nudge deflects the back-drivable HEAD
    (which the RobotPlayer keeps in daemon motor mode ``gravity_compensation``) beyond
    the resting jitter:
      - no training session -> emit a signed direction to ``on_direction``;
      - training session    -> retain the legacy binary 'end' control.

    Detection is SUPPRESSED while the robot is moving its own head (player busy), so
    greeting/talking/idle gestures can't self-trigger. Mirrors IdleTicker's thread shape.
    Thresholds are env-tunable (EMORI_GESTURE_THRESHOLD_RAD) for on-robot tuning.

    A DOUBLE turn (one way, then the other, inside ``EMORI_LANG_TOGGLE_WINDOW_S``) is the
    hidden idle language toggle -- the only way to re-language a robot with no screen, no
    keyboard and no shell. Its precedence against a single turn is the one
    trade-off in here worth stating outright:

    * a turn cannot be known to be LONE until the window closes, so while the toggle is
      armed (feature flag on, a handler installed, and the robot demonstrably idle) the
      first turn is HELD and its add-on handler runs up to 2.5 s later;
    * the second turn releases it immediately when it is the SAME way (two same-way turns
      are not the pattern), or consumes both when it completes the toggle;
    * everything that is not "armed and idle" -- a live mode, a training lease, a robot with
      no add-on, ``EMORI_LANG_TOGGLE=0`` -- dispatches on the spot exactly as before.

    Holding was chosen over dispatching-then-undoing because an add-on may act on a single
    turn, and a language switch must never be able to trigger that as a side effect.
    """

    def __init__(
        self,
        driver: "LiveDriver",
        *,
        tick_s: float = 0.1,
        on_direction: Optional[Callable[[GestureDirection], None]] = None,
        is_training: Optional[Callable[[], bool]] = None,
        is_idle: Optional[Callable[[], bool]] = None,
        on_lang_toggle: Optional[Callable[[], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        from emori_adapter.config import load_settings
        from emori_adapter.hostapp.gestures import (
            BackPushDetector,
            DirectionalGestureDetector,
            GestureDetector,
            _head_angles,
            _residual_enabled,
        )

        self.driver = driver
        self.tick_s = tick_s
        self._head_angles = _head_angles
        self._residual_enabled = _residual_enabled
        # The callback is deliberately the only add-on integration here.  An add-on owns
        # its own modes, media and robot-authored feedback motion.
        self.on_direction = on_direction
        # The push of the head back. Answers True if it ended something an add-on was
        # running, so the robot only acknowledges an exit that happened.
        self.on_back: Optional[Callable[[], bool]] = None
        self._is_training = is_training
        # The hidden idle language toggle. ``on_lang_toggle`` performs the switch itself
        # (persist + speak + reload the kiosk page); this ticker only recognises the
        # pattern, exactly as it only recognises a direction for on_direction.
        self.on_lang_toggle = on_lang_toggle
        self._is_idle = is_idle if callable(is_idle) else None
        self._clock = clock if callable(clock) else time.monotonic
        # Default OFF.  While it is armed, EVERY lone turn is HELD for the double-turn
        # window before it is dispatched.  Opt in explicitly
        # on a robot that needs to be re-languaged with no screen and no shell.
        self._lang_toggle_enabled = os.environ.get("EMORI_LANG_TOGGLE", "0") == "1"
        self._lang_toggle_window_s = lang_toggle_window_s()
        # (direction, deadline) of a turn waiting to find out whether it is half a
        # toggle or a lone selection.  Guarded by ``_detector_lock``.
        self._deferred_turn: Optional[tuple[GestureDirection, float]] = None
        self.detector = GestureDetector(
            threshold_rad=float(os.environ.get("EMORI_GESTURE_THRESHOLD_RAD", "0.20")),
        )
        gesture_settings = load_settings()
        self.direction_detector = DirectionalGestureDetector(
            yaw_threshold_rad=gesture_settings.directional_yaw_threshold_rad,
            roll_threshold_rad=gesture_settings.directional_roll_threshold_rad,
            ambiguity_ratio=gesture_settings.directional_ambiguity_ratio,
            # Trajectory-shape guards: a selection needs a
            # settle-gated anchor and an observed rest before the rise, or the
            # decaying tail of the robot's own motion reads as a turn.
            settle_epsilon_rad=gesture_shape_param("EMORI_GESTURE_SETTLE_EPSILON_RAD"),
            settle_ticks=int(gesture_shape_param("EMORI_GESTURE_SETTLE_TICKS")),
            quiet_epsilon_rad=gesture_shape_param("EMORI_GESTURE_QUIET_EPSILON_RAD"),
            quiet_ticks=int(gesture_shape_param("EMORI_GESTURE_QUIET_TICKS")),
        )
        # The push of the head back that leaves whatever is running. Added to the gestures
        # above, it replaces none of them. EMORI_GESTURE_BACK_EXIT=0 is the way out of it.
        self._back_exit = os.environ.get("EMORI_GESTURE_BACK_EXIT", "1") != "0"
        self.back_detector = BackPushDetector(
            threshold_rad=gesture_shape_param("EMORI_GESTURE_BACK_THRESHOLD_RAD"),
            hold_ticks=int(gesture_shape_param("EMORI_GESTURE_BACK_HOLD_TICKS")),
        )
        self._motors_enabled = True
        self._recenter_owed = False
        # The legacy screenless 'begin' by idle head-tilt.  Default off: head
        # movement never starts training.  Turning this on is for robots WITHOUT
        # an add-on that takes head turns; it replaces directional selection entirely
        # on that robot.
        self._gesture_begin_enabled = os.environ.get("EMORI_GESTURE_BEGIN", "0") == "1"
        # Gesture sensing mode. ``back_drive`` (default, legacy) expects the head in
        # gravity_compensation so a nudge back-drives it — but that daemon mode returns
        # HTTP 500 on firmware v1.9.0, so the head is stiff and never yields to a nudge.
        # ``push_turn`` works WITH stiff (enabled) motors: a firm human TURN deflects the
        # head 14-28deg (vs 0.05deg hold jitter), well past the directional threshold. A
        # stiff head HOLDS where it is turned, so after a fired turn the ticker springs it
        # back to neutral (unless an add-on pose already took the head) so the next turn
        # re-arms the detector. Measured on the robot.
        self._push_turn = push_turn_gesture_mode()
        # Opt-in per-tick sensing trace (EMORI_GESTURE_DEBUG=1). Off by default:
        # a shipped kiosk must not log head telemetry continuously.
        self._gesture_debug = os.environ.get("EMORI_GESTURE_DEBUG", "0") == "1"
        self._invert_direction = os.environ.get("EMORI_GESTURE_INVERT", "0") == "1"
        self._debug_suppressed_at = 0.0
        # Tracks the busy->idle edge so the tick right after robot-authored motion
        # re-arms the detectors instead of reading the leftover pose.
        self._was_busy = False
        # ``self._clock()`` of the last tick that carried a `motion.residual`, and of the
        # last tick on which the head was demonstrably in a human's hands. Plain floats
        # written by the sensing thread and read by the cue/skill/idle lanes: a float
        # write is atomic and a predicate one tick stale is a non-event, so a lock here
        # would only put contention on the 10 Hz path (the same call this file already
        # makes for _last_skill_accepted).
        self._residual_seen_at: Optional[float] = None
        self._held_at: Optional[float] = None
        # Feedback poses are submitted from controller/runtime threads while
        # this ticker polls in its own thread.  Resetting both detector
        # baselines must therefore be one small atomic operation.
        self._detector_lock = threading.RLock()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="gesture-ticker", daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, *, timeout_s: float = 1.0) -> bool:
        if threading.current_thread() is self._t:
            return False
        self._t.join(timeout=max(0.0, timeout_s))
        return not self._t.is_alive()

    def reset_sensing(self) -> None:
        """Forget both physical-gesture baselines after robot-authored motion.

        An add-on's pose feedback calls this immediately before requesting a pose
        and again after ``RobotPlayer.busy`` falls.  The next settled robot
        sample becomes a baseline rather than a delayed signed turn.

        The directional detector is SUSPENDED rather than reset: this runs
        while a human may still be holding the head, and a full reset would
        clear the guard that makes one physical movement select at most once
        (see DirectionalGestureDetector.suspend_until_settled).
        """

        with self._detector_lock:
            self.detector.reset()
            self.direction_detector.suspend_until_settled()

    def _note_residual(self, motion: Optional[dict]) -> None:
        """Remember that this robot CAN subtract its own command from the sensor.

        One dict probe per tick. ``motion.residual`` only exists on a v2 node whose
        backend can report what it is commanding, so its presence is the single honest
        test for "residual sensing is live" -- and its ABSENCE, two seconds later, is
        what puts the head reservation back to its old wide meaning without anybody
        having to notice a rollback happened.
        """

        if not isinstance(motion, dict) or not isinstance(motion.get("residual"), dict):
            return
        if not self._residual_enabled():
            return
        self._residual_seen_at = self._clock()

    def _note_motors(self, state: Optional[dict]) -> None:
        """Remember whether the motors are holding the head: a limp head can sag."""

        mode = state.get("control_mode") if isinstance(state, dict) else None
        if isinstance(mode, str):
            self._motors_enabled = mode == "enabled"

    def _back_exit_armed(self) -> bool:
        """May a push of the head back end something right now?

        Each unknown answers no: the residual must be live, the motors holding, and either
        a session or an add-on hook must exist. Outside the detector lock, because the
        training predicate reaches into the add-on.
        """

        if not (self._back_exit and self._push_turn and self._motors_enabled):
            return False
        if self._gesture_begin_enabled or not self.residual_sensing_live():
            return False
        if self.driver.tracker.active:
            return True
        return self.on_back is not None and not self._training_reserved()

    def _sense_back_push(self, motion: Optional[dict]) -> bool:
        """Feed one sample to the push detector. True on the tick the push is acted on.

        Runs on every tick, including the ones where the robot is moving its own head.
        """

        if not self._back_exit_armed():
            self.back_detector.reset()
            return False
        residual = motion.get("residual") if isinstance(motion, dict) else None
        pitch = residual.get("pitch") if isinstance(residual, dict) else None
        if isinstance(pitch, bool) or not isinstance(pitch, (int, float)):
            return False
        fired = self.back_detector.update(float(pitch))
        if fired:
            self._back_push_fired()
            return True
        if self._recenter_owed and not self.back_detector.pending:
            # A push too short to count: the head may be parked where it went.
            self._recenter_owed = False
            self._recenter_after_turn()
        return False

    def _back_push_fired(self) -> None:
        self._recenter_owed = False
        if self.driver.tracker.active:
            log.warning("gesture: head pushed back, ending the session")
            self._end_session_by_gesture()
            return
        handler = self.on_back
        try:
            left = bool(handler()) if handler is not None else False
        except Exception:  # noqa: BLE001 - an add-on fault must not stop the sensing thread
            log.warning("gesture: the add-on failed to handle a push back", exc_info=True)
            left = False
        if not left:
            # Nothing to leave. The head may be parked where it was pushed.
            self._recenter_after_turn()
            return
        log.warning("gesture: head pushed back, left the mode that was running")
        self.driver.counters["gesture_exit"] += 1
        self.driver.touch()
        self.driver.submit_legacy_motion(R.exit_ack())

    def _end_session_by_gesture(self) -> None:
        """A person's gesture ends the session: through Pokamind, so the analysis lands."""

        request = getattr(self.driver, "request_gesture_end", None)
        if callable(request) and not request():
            # Two detectors can read one push, and a person can push twice.
            return
        self.driver.counters["gesture_end"] += 1
        if not callable(request):
            self.driver.enqueue_cmd("end")
            arm_grace = getattr(self.driver, "arm_end_grace", None)
            if callable(arm_grace):
                arm_grace()
        # The end can take many seconds to come back from Pokamind, so the person needs to see
        # at once that the push was felt: what the sentence was still going to do stops, and
        # the acknowledgement is next. Not drop_if_busy, because while the robot talks the
        # queue is deep, which is exactly when it matters.
        cue = getattr(self.driver, "cue", None)
        preempt = getattr(cue, "preempt", None)
        if callable(preempt):
            preempt("head pushed back")
        self.driver.submit_legacy_motion(R.exit_ack(reason="gesture ack"))
        # ...but refresh the activity clock only once the conversation is
        # REAL. Before the first cycle a nudge is someone poking a robot that
        # never started, and treating it as activity reset the 45 s walk-away
        # clock -- so trying to un-stick the robot was what kept it stuck.
        if getattr(self.driver.tracker, "cycles", 0) > 0:
            self.driver.touch()

    def residual_sensing_live(self) -> bool:
        """Has a ``motion.residual`` arrived within the last two seconds?

        Fails CLOSED: a v2 node that stops reporting the residual (a backend that
        cannot say, a rollback to v1, EMORI_GESTURE_RESIDUAL=0) puts
        :meth:`head_reserved_for_sensing` straight back to its old wide predicate on
        the next tick. Two seconds is twenty sensing ticks -- long enough that a single
        dropped poll is not a behaviour change, short enough that a rollback is felt
        inside one human reaction time.
        """

        at = self._residual_seen_at
        return at is not None and (self._clock() - at) <= _RESIDUAL_LIVE_S

    def head_held(self) -> bool:
        """Is a human physically deflecting the head RIGHT NOW (or did they just stop)?

        True while the directional detector sees a deflection from its baseline, while
        its post-selection refractory runs, and for ``_HEAD_HOLD_RELEASE_S`` after -- so
        the release stroke of one physical turn is still covered and the head is not
        handed back to the robot mid-gesture.

        The trailing timer lives HERE rather than in the detector because the detector
        counts ticks and this ticker is the only thing in the pair that owns a clock
        (``self._clock``, which the language-toggle tests already drive).
        """

        if getattr(self.direction_detector, "held", False):
            return True
        at = self._held_at
        return at is not None and (self._clock() - at) <= _HEAD_HOLD_RELEASE_S

    def head_reserved_for_sensing(self) -> bool:
        """True while the HEAD is this robot's input device, not an output.

        Two regimes, and the difference is whether the robot can subtract its own
        motion from the sensor:

        * residual sensing LIVE (a v2 node reporting ``motion.residual``): return
          :meth:`head_held`. A hand on the head reserves it; an idle robot does not.
          This is what the name always meant, and it is what finally lets the cue lane
          gesture while the robot talks -- ``CueScheduler.on_sentence`` checks this
          predicate FIRST, so an answer that is True at idle or in an add-on mode
          suppresses every cue (reason=head_reserved) and the robot does not move
          while it speaks.
        * residual sensing UNAVAILABLE (v1, Pollen's daemon, EMORI_GESTURE_RESIDUAL=0):
          the old wide predicate, unchanged. Without the residual, robot-authored
          motion really does wipe the rolling baseline (the measured 12
          baseline-resets/min trap) and standing down really is the only safe answer.
        """

        if not self._push_turn:
            return False
        if getattr(self.driver, "tracker", None) is not None and self.driver.tracker.active:
            return False
        if self._training_reserved():
            return False
        if not self.residual_sensing_live():
            return True
        # Residual sensing lets the robot move while someone MIGHT push -- but at idle,
        # when a push is the only thing it is waiting for, the idle scan still wrecks the
        # input: measured on the robot, each scan left the head tilted (anchor
        # roll 0.28-0.40 rad), so the next push back towards centre read as a release
        # stroke and was eaten, and a push the other way came before the rest the
        # detector needs. From the operator's side: "only right worked, not left".
        # While NOTHING is running, the head waits at home as the input device it is.
        return self.head_held() or self._awaiting_selection()

    def _awaiting_selection(self) -> bool:
        """Nothing is running: the head is waiting for a push-turn, so keep it still."""

        predicate = self._is_idle
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - an idle gate that cannot answer is "not idle"
            return False

    def set_training_predicate(self, predicate: Optional[Callable[[], bool]]) -> None:
        """Install the runtime's QR-reservation gate without exposing internals."""

        self._is_training = predicate if callable(predicate) else None

    def set_idle_predicate(self, predicate: Optional[Callable[[], bool]]) -> None:
        """Install the "nothing is running" gate for the language toggle.

        Installed AFTER construction because the runtime that answers it is built
        from this ticker (server._build_app).  Until it is, the toggle stays
        disarmed: a ticker that cannot prove the robot is idle must not hold a
        selection back, let alone re-language a live session.
        """

        self._is_idle = predicate if callable(predicate) else None

    def _robot_moving(self, motion: Optional[dict] = None) -> tuple[bool, str]:
        """Is the robot MOVING ITS OWN HEAD right now, whoever authored it?

        Only a running head move suppresses. Everything else that might seem to need
        suppression -- a gaze stream, a settled hold pose, the speech wobbler -- is
        handled by residual sensing, which is strictly better because it keeps the
        sensor LIVE. Suppressing on ``gaze_active`` would have starved the detector
        whenever a person was in front of the robot, i.e. exactly when the gesture
        matters: it would have killed a directional gesture with somebody present
        and the training end nudge.

        ``motion.active`` alone is not a suppressor either: an antenna-only cue cannot
        move the measured pose, so standing the sensor down for one would disarm
        selection while the robot is merely giving antenna feedback.

        What is left is the servo tracking error during a COMMANDED move, which the
        residual cannot remove and which is the one window where a robot-authored
        transient really can look like a hand.
        """

        if bool(getattr(self.driver.player, "busy", False)):
            return True, "player.busy"
        if not isinstance(motion, dict):
            return False, ""
        if bool(motion.get("head_moving")):
            return True, f"ros head move ({motion.get('client') or '?'})"
        return False, ""

    def process_pose(self, angles: Optional[tuple[float, float, float]],
                     motion: Optional[dict] = None) -> None:
        """Process one already-read pose sample without doing I/O.

        Keeping this small unit test seam makes the selection boundary explicit:
        training remains on the legacy binary detector, while an idle robot can
        only emit a signed direction to an add-on's controller.  It
        never starts a training session by head movement.

        ``motion`` is the v2 shim's ``/api/state/full`` motion block, or None for a v1
        robot -- in which case every guard below behaves exactly as it did before it
        existed.
        """

        # A turn held for the double-turn window has to be released ON TIME even
        # while the robot is busy springing its own head back after that same
        # turn, so this runs BEFORE every self-motion guard below.  It is one
        # attribute read when nothing is held (i.e. always, with the flag off).
        self._flush_deferred_turn()
        self._note_residual(motion)
        if self._sense_back_push(motion):
            # One physical movement ends one thing: the side detector must not read its
            # release as a turn.
            self.reset_sensing()
            return
        # Treat robot-authored motion as unarmed even when a caller invokes this
        # method directly (the polling loop performs the same fast-path before
        # touching the daemon).
        busy, why = self._robot_moving(motion)
        if busy:
            self._debug_suppressed(why)
            self._was_busy = True
            self.reset_sensing()
            return
        if self._was_busy:
            self._debug_suppressed("busy->idle re-arm")
            # The robot just finished authoring motion: on stiff motors its own
            # gestures leave the head off-neutral. Re-arm from a clean
            # baseline before trusting the pose again. Deliberately NO recenter
            # here - _recenter_head() is robot-authored motion and would set
            # player.busy, re-triggering this branch forever; the deliberate-turn
            # path already recenters after a selection.
            self._was_busy = False
            self.reset_sensing()
            return

        with self._detector_lock:
            if self.driver.tracker.active:
                self._debug_suppressed("training session active")
                # Add-on selection is disabled for training.  Reset each tick so
                # a held or stale idle-side deflection cannot leak across the mode
                # boundary when training ends.
                self.direction_detector.reset()
                if not self.detector.update(angles):
                    return
                self._end_session_by_gesture()
                return

            if self._training_reserved():
                self._debug_suppressed("training reserved (QR lease)")
                # A QR can have claimed training before the browser loads
                # /session.  During that small window directional gestures are
                # neither an add-on selection nor an end command.
                self.detector.reset()
                self.direction_detector.reset()
                return

            if self._gesture_begin_enabled:
                self._debug_suppressed("EMORI_GESTURE_BEGIN=1 owns idle movement")
                # Legacy screenless 'begin' for robots without
                # add-on modes.  The binary detector owns idle head
                # movement again; directional selection stays reset.
                self.direction_detector.reset()
                if not self.detector.update(angles):
                    return
                self.driver.counters["gesture_begin"] += 1
                self.driver.enqueue_cmd("begin")
                self.driver.submit_legacy_motion(
                    [R.perk_antennas(reason="gesture ack")], drop_if_busy=True
                )
                self.driver.touch()
                return

            # A card/QR starts training; a head movement while idle is never a
            # substitute for that identity-bearing action.  Forget the legacy
            # detector so its old baseline cannot affect the next training session.
            self.detector.reset()
            baseline_before = (
                getattr(self.direction_detector, "_baseline", None)
                if self._gesture_debug
                else None
            )
            direction = self.direction_detector.update(angles, armed=True)
            # The sensing thread is the ONLY writer of this stamp, and it is what
            # head_held() -- and through it the narrowed head reservation -- reads.
            if getattr(self.direction_detector, "held", False):
                self._held_at = self._clock()
            # A movement the classifier REFUSED (a nod, opposed axes, the
            # ambiguity band) still latched, and on stiff motors the head is now
            # parked wherever it was pushed.  Nothing else will move it: the
            # selection path never ran, so the recenter that normally re-arms
            # sensing never ran either, and the detector would sit deflected
            # against its own anchor forever.  Answer it with the same recenter.
            vetoed_latch = self.direction_detector.take_vetoed_latch()
            if self._gesture_debug:
                self._log_tick(angles, baseline_before, direction)
        direction = self._oriented(direction)
        if direction is None:
            if vetoed_latch and self.back_detector.pending:
                # Possibly a push back still being held: moving the head under the hand now
                # would fight it. If it never counts, _sense_back_push recentres.
                self._recenter_owed = True
            elif vetoed_latch:
                # Outside the detector lock, like every other motion submission
                # here: _recenter_head reaches into the player.
                log.warning("gesture: head parked by a movement that selected no mode "
                            "-- recentring to re-arm sensing")
                self._recenter_after_turn()
            return
        if self._toggle_armed():
            # An idle robot: this turn may still turn out to be half a language
            # toggle, so it is held rather than dispatched (see the class docstring).
            self._observe_turn(direction)
            return
        self._dispatch_direction(direction, recenter=True)

    def _dispatch_direction(self, direction: object, *, recenter: bool) -> None:
        """Hand one signed turn to the handler and re-arm the stiff head."""

        if direction is None or self.on_direction is None:
            return
        self.driver.touch()
        self.on_direction(direction)
        if recenter:
            # push_turn: a stiff head holds where it was turned. If no add-on pose
            # took over the head (player still idle), spring it back to neutral so the
            # detector re-arms for the next turn. If a pose is in flight (player busy),
            # that pose owns the recenter — do not submit a competing motion.
            self._recenter_after_turn()

    def _recenter_after_turn(self) -> None:
        if self._push_turn and not getattr(self.driver.player, "busy", False):
            self._recenter_head()

    def _toggle_armed(self) -> bool:
        """Whether a turn may currently be part of the hidden language toggle.

        Fail-CLOSED on every unknown: no feature flag, no handler, no idle
        predicate or a predicate that raises all mean "dispatch this turn the way
        the robot always did".  The toggle is a convenience.
        """

        if not self._lang_toggle_enabled or self.on_lang_toggle is None:
            return False
        predicate = self._is_idle
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - an unknown runtime state stays conservative
            return False

    def _observe_turn(self, direction: object) -> None:
        """Feed one turn to the double-turn pattern; hold, release or toggle."""

        self.driver.touch()
        with self._detector_lock:
            held = self._deferred_turn
            # Only two directions exist, so "not the same one" IS "the opposite one".
            toggled = held is not None and held[0] != direction
            if toggled:
                self._deferred_turn = None
                release = None
            else:
                self._deferred_turn = (
                    direction, self._clock() + self._lang_toggle_window_s,
                )
                release = held[0] if held is not None else None
        if toggled:
            self._fire_lang_toggle()
            return
        if release is not None:
            # Two turns the SAME way are not the pattern, so the first one is a
            # plain selection that has already waited long enough.  It dispatches
            # without its own recenter because the one below covers both it and
            # the turn now being held -- and skips itself if the add-on pose
            # that selection just triggered already took the head.
            self._dispatch_direction(release, recenter=False)
        self._recenter_after_turn()

    def _flush_deferred_turn(self) -> None:
        """Release a held turn once its window closes (or drop it if the world moved)."""

        if self._deferred_turn is None:
            return
        # OUTSIDE the detector lock: the predicate reaches into the add-on
        # runtime, and an add-on's pose feedback takes this same lock from that side.
        armed = self._toggle_armed()
        with self._detector_lock:
            held = self._deferred_turn
            if held is None:
                return
            direction, deadline = held
            if armed and self._clock() < deadline:
                return
            self._deferred_turn = None
        if not armed:
            # Something claimed the robot while this turn was held (a QR training
            # lease is the realistic one).  Replaying a selection that is up to a
            # window old into a mode that has since started would tear it down; the
            # human can simply turn again.
            log.warning("gesture: dropped a held turn -- the robot is no longer idle")
            return
        self._dispatch_direction(direction, recenter=False)

    def _fire_lang_toggle(self) -> None:
        """The double turn completed: hand off to the runtime's switch."""

        self.driver.counters["lang_toggle"] += 1
        # The second turn left the stiff head off-neutral and nothing else is going
        # to move it (the handler only persists/speaks/reloads), so re-arm sensing.
        self._recenter_after_turn()
        handler = self.on_lang_toggle
        if handler is None:
            return
        handler()

    def _oriented(self, direction: object) -> object:
        """Map the measured side onto the side the human believes they turned.

        With ``gravity_compensation`` refused (HTTP 500) the head is stiff, so a
        human "turn" does not move the yaw joint -- it shows up as ROLL through
        the neck platform, and the sign of that coupling is a property of this
        mechanism, not of the classifier.  Measured on the robot: a turn the
        operator calls LEFT reports d_roll=+0.236 (i.e. RIGHT), so the pairing
        has to be inverted here or every signed turn reads the opposite way.
        Env-tunable because only the physical robot can settle it.
        """

        if not self._invert_direction or direction is None:
            return direction
        if direction is GestureDirection.LEFT:
            return GestureDirection.RIGHT
        if direction is GestureDirection.RIGHT:
            return GestureDirection.LEFT
        return direction

    def _debug_suppressed(self, reason: str) -> None:
        """Say WHY a tick never reached the directional detector.

        Every guard below returns silently, so a permanently stuck one (a
        never-cleared player.busy, a stale training lease) is indistinguishable
        from a broken sensor: the head turns and absolutely nothing happens.
        Rate-limited so a standing condition reports once in a while.
        """

        if not self._gesture_debug:
            return
        now = time.monotonic()
        if now - self._debug_suppressed_at < _GESTURE_DEBUG_SUPPRESS_EVERY_S:
            return
        self._debug_suppressed_at = now
        # WARNING, not INFO: the hostapp's root logger filters INFO, so an
        # INFO-level trace is invisible in journalctl exactly when it is needed.
        log.warning("gesture: sensing suppressed by %s", reason)

    def _log_tick(
        self,
        angles: Optional[tuple[float, float, float]],
        baseline: Optional[tuple[float, float, float]],
        direction: object,
    ) -> None:
        """Explain one sensing tick when EMORI_GESTURE_DEBUG=1.

        Diagnosing "I turned the head and nothing happened" from the outside is
        guesswork: the pose, the rolling baseline, and the three reject rules
        all live in here.  Only ticks that actually moved are logged, so this
        stays readable at 10 Hz.
        """

        if angles is None:
            return
        if baseline is None:
            # Every anchor is settle-gated now, so this tick either ADOPTED one
            # (the detector has a baseline again) or is still waiting for a head
            # that has stopped moving.  Reporting "baseline established" for
            # both is what made spurious fires unreadable: the
            # trace claimed an anchor while the head was demonstrably in flight.
            adopted = getattr(self.direction_detector, "_baseline", None)
            log.warning(
                "gesture: %s roll/pitch/yaw=%.3f/%.3f/%.3f",
                "baseline established" if adopted is not None
                else "settling (head still moving)",
                *angles,
            )
            return
        d_roll, d_pitch, d_yaw = (angles[i] - baseline[i] for i in range(3))
        moved = max(abs(d_roll), abs(d_pitch), abs(d_yaw))
        if direction is None and moved < _GESTURE_DEBUG_MIN_DELTA_RAD:
            return
        detector = self.direction_detector
        yaw_threshold = getattr(detector, "yaw_threshold_rad", 0.0)
        roll_threshold = getattr(detector, "roll_threshold_rad", 0.0)
        pitch_veto = abs(d_pitch) >= min(yaw_threshold, roll_threshold)
        # How far the ANCHOR itself sits from the remembered neutral. Non-zero
        # means the release-stroke rule is live, which is the difference between
        # "nothing happened" and "that was read as you letting go".
        home = getattr(detector, "_home", None)
        off_home = (
            max(abs(baseline[i] - home[i]) for i in range(3)) if home is not None else 0.0
        )
        log.warning(
            "gesture: d_roll=%+.3f d_pitch=%+.3f d_yaw=%+.3f (yaw_thr=%.2f roll_thr=%.2f) "
            "latched=%s cooldown=%d pitch_veto=%s rest_armed=%s quiet=%d off_home=%.3f -> %s",
            d_roll,
            d_pitch,
            d_yaw,
            yaw_threshold,
            roll_threshold,
            getattr(detector, "_triggered_high", None),
            getattr(detector, "_cooldown", -1),
            pitch_veto,
            # Without these two, "I turned the head and nothing happened" and
            # "the anchor is stale" produce an identical trace.
            getattr(detector, "_rise_armed", None),
            getattr(detector, "_quiet_run", -1),
            off_home,
            getattr(direction, "name", direction),
        )

    def _recenter_head(self) -> None:
        """Return the (stiff) head to neutral so push_turn sensing re-arms."""

        from emori_adapter.behavior.repertoire import ANTENNA_REST
        from emori_adapter.models import HeadPose, MotionIntent

        self.driver.submit_legacy_motion(
            [MotionIntent(kind="goto", head=HeadPose(pitch=4.0, yaw=0.0, roll=0.0),
                          antennas=ANTENNA_REST, body_yaw=0.0, duration=0.4,
                          reason="push-turn recenter")],
            drop_if_busy=True,
        )

    def _training_reserved(self) -> bool:
        predicate = self._is_training
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:  # noqa: BLE001 - an unknown runtime state stays conservative
            return True

    def _sync_driver_flags(self) -> None:
        """Reconcile the two arbiter gates once per tick.

        ``head_reserved_for_sensing`` is a PREDICATE, not an event -- a hand arrives on
        the head and later leaves, and nothing calls anybody when it does -- so the only
        honest way to carry it to the robot node is to ask it at the sensor's own rate.
        Idempotent by construction: ``sync_robot_flags`` POSTs only on a change, so a
        quiet robot pays two attribute comparisons per tick.

        Resolved by name because this ticker is built against injected fakes in several
        test files; a driver that predates the method must be a no-op rather than an
        exception that stops the sensing thread's tick.
        """

        sync = getattr(self.driver, "sync_robot_flags", None)
        if not callable(sync):
            return
        try:
            sync()
        except Exception:  # noqa: BLE001 - a gate is never worth a dropped sensing tick
            log.debug("sync_robot_flags failed", exc_info=True)

    def _read_state(self) -> Optional[dict]:
        """One ``/api/state/full`` poll; None for a robot that will not answer.

        Caught HERE rather than by the loop's outer handler because a read that raises
        must not cost this tick's SUPPRESSION bookkeeping. ``_was_busy`` and the
        deferred-turn window are LOCAL state: losing the busy edge would leave the
        busy->idle re-arm inert for the whole outage, so the first sample after
        the node came back would be trusted against a baseline taken before the robot
        moved -- which is the spurious selection this ticker exists to avoid.

        An unreadable state is simply an absent one, which every consumer downstream
        already handles: ``_head_angles(None)`` is None and both detectors treat a None
        sample as "no sample" rather than as a pose.
        """

        try:
            return self.driver.client.read_state()
        except Exception as e:  # noqa: BLE001 - an unreachable node is not a dead thread
            self._debug_suppressed(f"state read failed: {type(e).__name__}")
            log.debug("gesture state read failed: %s", e)
            return None

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self._sync_driver_flags()
                # The state is read on EVERY tick, including one that is about to be
                # suppressed, and the busy question is asked afterwards. Two reasons,
                # and the second one is why a "skip the HTTP read while busy" fast path
                # is wrong however cheap it looks:
                #
                # * a head move authored in the ROS graph is invisible to
                #   ``player.busy`` and can only be seen in ``motion.head_moving``,
                #   which is the whole reason the shim reports it;
                # * ``_note_residual`` is the liveness clock behind
                #   ``head_reserved_for_sensing``. Not reading during a suppressed tick
                #   does not mean the robot stopped reporting the residual, but the clock
                #   cannot tell the difference -- and a hostapp-authored move routinely
                #   outlasts _RESIDUAL_LIVE_S (a talking_gesture of 8 beats is ~2.4 s
                #   plus settle; a recorded farewell move is longer). The reservation
                #   would silently widen back to "always reserved" mid-move on a
                #   perfectly healthy v2 node, and _sync_driver_flags() above -- which
                #   runs on suppressed ticks too -- would POST set_head_reserved(True)
                #   and cut expression-lane gaze for the rest of the move.
                #
                # Suppression itself is unchanged: self-motion (greeting/talking/idle)
                # must not trigger a gesture, and the detector's baseline re-converges
                # once the robot settles. This fast path skips process_pose entirely,
                # so it must record the busy edge itself or the busy->idle re-arm
                # would never run on the polling path.
                state = self._read_state()
                motion = _motion(state)
                # Presence for the conversation watchdog, for free: the v2 shim's
                # motion block already carries the node's face tracker
                # (gaze.is_active(): a face accepted within the last second). Before
                # the busy fast path, so a learner is still "seen" while the robot
                # talks or re-prompts. Absent on v1 / Pollen's daemon -> never seen.
                if _face_fresh(motion):
                    note = getattr(self.driver, "note_face_seen", None)
                    if callable(note):
                        note()
                self._note_residual(motion)
                self._note_motors(state)
                busy, why = self._robot_moving(motion)
                if busy:
                    self._debug_suppressed(f"{why} (polling fast path)")
                    self._was_busy = True
                    # A push back is the one gesture that works while the robot moves.
                    self._sense_back_push(motion)
                    self.reset_sensing()
                    # This fast path skips process_pose, and the very motion that
                    # makes the player busy is usually the recenter that FOLLOWS a
                    # held turn -- so the double-turn window has to keep running.
                    self._flush_deferred_turn()
                    continue
                angles = self._head_angles(state)
                if angles is None:
                    self._debug_suppressed("daemon returned no head_pose")
                self.process_pose(angles, motion)
            except Exception as e:  # noqa: BLE001 - a sensor blip must never kill the thread
                # A tick that always raises looks exactly like a dead sensor, so
                # surface it at the same rate as the other suppression reasons.
                self._debug_suppressed(f"tick error: {type(e).__name__}")
                log.debug("gesture tick error: %s", e)
