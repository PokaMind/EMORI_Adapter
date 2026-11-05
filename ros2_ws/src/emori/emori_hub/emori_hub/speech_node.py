"""``/emori/speech``: the robot's voice and ears, and the kiosk language, on the graph.

The node shows what the hostapp already does and offers the same commands, through the same
objects the HTTP routes use (``app.state.glue``). It adds no speech behaviour of its own.

``recognize`` and ``set_language`` share one callback group, so they never run at the same time
and a long recognition delays a language switch and nothing else. ``say`` has a group of its
own. Nothing runs in a timer: the topics are published by the node's own threads.

====================  ===========================================  =====================
Interface             Is                                           Same as
====================  ===========================================  =====================
``say`` (action)      one sentence on the robot's own speaker      the language toggle's
                                                                   line, ``GET /api/tts``
                                                                   for the synthesis
``recognize`` (srv)   16 kHz mono PCM to text, open or on a        ``POST /api/stt``
                      grammar with a confidence
``set_language``      the kiosk language, the whole switch         ``POST /api/admin/lang``
``voice``             the robot's voice as the speaker shows it,   ``/api/status``
                      10 Hz, only while somebody subscribes        ``voice_link``
``/emori/language``   the kiosk language and those on offer,       ``/api/admin/summary``
                      latched
``/emori/audio/       the microphone profile in force, latched     ``/api/admin/summary``
mic_profile``
====================  ===========================================  =====================

**Say takes the speaker under the HTTP path's rules.** It is refused (``session_active``) while
a training session is running at all, and (``addon_busy``) while an add-on mode holds the robot
or a selection is reserved. An add-on mode that listens while the robot speaks would take a
sentence it did not expect for a participant's. The gate is asked again after synthesis and
before the sound starts, because synthesis takes seconds and a session can start in them. Only
one Say plays at a time
(``speaker_busy``). While it plays, the player is told the voice on the speaker is not a
sentence of a session (:meth:`RobotPlayer.hold_voice`, as the kiosk page does for its own line),
so the tap that starts a sentence's motion on the voice does not take this one for it. The hold
lasts the player's own limit unless it is cleared, and it is cleared when the sound ends.
Refusal codes are the action's (``session_active``, ``addon_busy``, ``no_voice``,
``play_failed``) and ``speaker_busy``.

**Recognize** returns ``conf`` of -1.0 in open mode, as on the wire. It is bounded by the same
time limit as the HTTP route; an engine that does not answer in time is left running and a
second recognition is refused (``busy``) until it has ended, so a wedged engine costs one
thread, not one per call. Samples longer than :data:`MAX_PCM_BYTES` are refused (``too_long``).
Nothing the learner said is logged: a grammar match is logged as its slot and its score.

**Where the work runs.** Nothing here is on the path a session's motion takes: no robot client
call, no driver lock, nothing the cue scheduler or the player waits on. The commands run on the
node's own callback groups. What reads a lock another thread also takes (the voice tap's, the
microphone profile's, the language state's) runs on a thread of this node's own
(:class:`facade_kit.Poller`), and the voice is read only while somebody subscribes. A voice read
that takes longer than :data:`VOICE_SLOW_S` backs the topic off to :data:`VOICE_BACKOFF_S` and is
counted in ``voice_slow``: the topic gives way, the session does not.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from emori_hub.facade_kit import (
    Counters,
    KitBindings,
    Poller,
    load_kit_bindings,
    release_with,
    stamp,
)

__all__ = [
    "MAX_PCM_BYTES",
    "MIN_PCM_BYTES",
    "NODE_NAME",
    "NODE_NAMESPACE",
    "SpeechBindings",
    "SpeechFacade",
    "VOICE_BACKOFF_S",
    "VOICE_SLOW_S",
    "build_speech_node",
    "load_speech_bindings",
]

log = logging.getLogger("emori.hub.speech")

NODE_NAME = "speech"
NODE_NAMESPACE = "/emori"
SAY_ACTION = "/emori/speech/say"
RECOGNIZE_SERVICE = "/emori/speech/recognize"
SET_LANGUAGE_SERVICE = "/emori/speech/set_language"
VOICE_TOPIC = "/emori/speech/voice"
LANGUAGE_TOPIC = "/emori/language"
MIC_PROFILE_TOPIC = "/emori/audio/mic_profile"

#: Under 0.1 s of 16 kHz signed 16-bit samples the HTTP route answers an empty text without
#: asking the engine, and so does this.
MIN_PCM_BYTES = 3200
#: Thirty seconds. The engine is bounded in time as well; this bounds what one call can hold.
MAX_PCM_BYTES = 16000 * 2 * 30

VOICE_PERIOD_S = 0.1
STATE_PERIOD_S = 0.5
#: One voice read longer than this means the tap's lock was contended.
VOICE_SLOW_S = 0.002
#: What the voice topic waits after a slow read. The topic loses, and ``voice_slow`` counts it.
VOICE_BACKOFF_S = 1.0

_SAY_REFUSED_PREFIX = "say_refused_"
#: The speaker's limit is 20 s, and the pages' own rule of thumb is 95 ms a character.
MAX_SAY_CHARS = 200
#: How often a playing sentence looks at the gate again, and renews its hold on the voice tap.
_PLAY_POLL_S = 0.1
_HOLD_RENEW_S = 3.0


class _Refused(Exception):
    """The gate closed between synthesis and the sound."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SpeechBindings:
    """Everything this node takes from ROS, so a test can hand it fakes."""

    kit: KitBindings
    action_server_cls: Any
    goal_response: Any
    cancel_response: Any
    Say: Any
    Recognize: Any
    SetLanguage: Any
    VoiceState: Any
    KioskLanguage: Any


def load_speech_bindings() -> SpeechBindings:
    """Import ``rclpy`` and the interfaces. Raises ``ImportError`` where ROS is absent."""

    from rclpy.action import ActionServer, CancelResponse, GoalResponse

    from emori_msgs.action import Say
    from emori_msgs.msg import KioskLanguage, VoiceState
    from emori_msgs.srv import Recognize, SetLanguage

    return SpeechBindings(
        kit=load_kit_bindings(),
        action_server_cls=ActionServer,
        goal_response=GoalResponse,
        cancel_response=CancelResponse,
        Say=Say,
        Recognize=Recognize,
        SetLanguage=SetLanguage,
        VoiceState=VoiceState,
        KioskLanguage=KioskLanguage,
    )


class _StagedTts:
    """The app's synthesiser, saying when synthesis begins."""

    def __init__(self, tts: Any, on_stage: Callable[[str], Any]) -> None:
        self._tts = tts
        self._on_stage = on_stage

    def synth_path(self, text: str, lang: str = "en") -> Any:
        self._on_stage("synth")
        return self._tts.synth_path(text, lang)


class SpeechFacade:
    """The ``/emori/speech`` node. Public methods are what its callbacks call."""

    def __init__(
        self,
        app: Any,
        bindings: SpeechBindings,
        *,
        speak: Optional[Callable[..., bool]] = None,
        play: Optional[Callable[..., Any]] = None,
        stt_timeout_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        glue = getattr(app.state, "glue", None)
        if glue is None:
            raise RuntimeError("the app has no glue: build it with the hub's server module")
        self._ros = bindings
        self._glue = glue
        self._driver = app.state.driver
        self._extension = app.state.extension
        self._lang_state = app.state.lang_state
        self._speak = speak
        self._play = play if play is not None else self._play_while_free
        self._popen = subprocess.Popen
        self._held_until: Optional[float] = None
        self._stt_timeout_s = stt_timeout_s
        self._clock = clock
        self.counters = Counters()
        self._say_lock = threading.Lock()
        self._recognize_thread: Optional[threading.Thread] = None
        self._last_language: Optional[tuple[str, tuple[str, ...]]] = None
        self._last_mic: Optional[str] = None
        self._mic_published = False
        self._closed = False

        kit = bindings.kit
        node = kit.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
        self.node = node
        command_group = kit.group_exclusive()
        say_group = kit.group_reentrant()
        self._voice_pub = node.create_publisher(bindings.VoiceState, VOICE_TOPIC, kit.qos_state)
        self._language_pub = node.create_publisher(bindings.KioskLanguage, LANGUAGE_TOPIC,
                                                   kit.qos_latched)
        self._mic_pub = node.create_publisher(kit.String, MIC_PROFILE_TOPIC, kit.qos_latched)
        node.create_service(bindings.Recognize, RECOGNIZE_SERVICE, self._on_recognize,
                            callback_group=command_group)
        node.create_service(bindings.SetLanguage, SET_LANGUAGE_SERVICE, self._on_set_language,
                            callback_group=command_group)
        self._say_server = bindings.action_server_cls(
            node, bindings.Say, SAY_ACTION, execute_callback=self._execute_say,
            goal_callback=lambda _goal: bindings.goal_response.ACCEPT,
            cancel_callback=lambda _goal: bindings.cancel_response.REJECT,
            callback_group=say_group)
        self._pollers = [
            Poller("ros-speech-voice", VOICE_PERIOD_S, self._read_voice, self.counters,
                   wanted=lambda: self._voice_pub.get_subscription_count() > 0),
            Poller("ros-speech-state", STATE_PERIOD_S, self._read_state, self.counters),
        ]

    # --- starting and stopping ------------------------------------------------------

    def start(self) -> None:
        for poller in self._pollers:
            poller.start()

    def close(self) -> None:
        """Stop the threads and the action server. Never raises."""

        if self._closed:
            return
        self._closed = True
        for poller in self._pollers:
            poller.stop()
        destroy = getattr(self._say_server, "destroy", None)
        if callable(destroy):
            try:
                destroy()
            except Exception:  # noqa: BLE001 - the node is going anyway
                log.warning("the say action server did not close cleanly", exc_info=True)

    def stats(self) -> dict[str, Any]:
        return self.counters.snapshot()

    # --- say -----------------------------------------------------------------------

    def _refusal(self) -> str:
        """Why the speaker is not free, or ``""``. Anything it cannot tell is a refusal."""

        try:
            if self._glue.live_session():
                return "session_active"
            tracker = getattr(self._driver, "tracker", None)
            if tracker is not None and tracker.active:
                return "session_active"
        except Exception:  # noqa: BLE001 - a gate that cannot answer is closed
            return "session_active"
        try:
            if self._extension.busy() or self._extension.selection_reserved():
                return "addon_busy"
        except Exception:  # noqa: BLE001 - same
            return "addon_busy"
        # The tracker says nothing about the summary a finished session is still speaking,
        # or a menu that is being read to someone with the microphone open after it.
        try:
            if not self._glue.landing_idle():
                return "kiosk_busy"
        except Exception:  # noqa: BLE001 - same
            return "kiosk_busy"
        return ""

    def _hold_voice(self) -> None:
        hold = getattr(getattr(self._driver, "player", None), "hold_voice", None)
        if callable(hold):
            try:
                self._held_until = hold(True)
            except Exception:  # noqa: BLE001 - the sentence still plays
                self.counters.complain("hold_voice_errors", "could not hold the voice tap")

    def _release_voice(self) -> None:
        """Only a hold still ours: the page may have taken one for its own line since."""

        held, self._held_until = self._held_until, None
        release = getattr(getattr(self._driver, "player", None), "release_voice", None)
        if held is None or not callable(release):
            return
        try:
            release(held)
        except Exception:  # noqa: BLE001 - the hold runs out by itself
            self.counters.complain("hold_voice_errors", "could not release the voice tap")

    def _play_while_free(self, argv: Sequence[str], *, timeout: float, **_: Any) -> Any:
        """``subprocess.run`` for the player, stopped as soon as the speaker is wanted."""

        process = self._popen(list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = renewed = self._clock()
        try:
            while True:
                try:
                    return subprocess.CompletedProcess(argv, process.wait(_PLAY_POLL_S))
                except subprocess.TimeoutExpired:
                    pass
                now = self._clock()
                code = self._refusal()
                if code:
                    self.counters.add("say_cut")
                    raise _Refused(code)
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if now - renewed >= _HOLD_RENEW_S:
                    renewed = now
                    self._hold_voice()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(1.0)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _speak_function(self) -> Callable[..., bool]:
        if self._speak is not None:
            return self._speak
        from emori_adapter.hostapp.server import _speak_on_robot

        return _speak_on_robot

    def say(self, text: str, lang: str = "",
            on_stage: Optional[Callable[[str], Any]] = None) -> tuple[bool, str]:
        """Say ``text`` on the robot's speaker. Returns ``(played, error_code)``. Never raises."""

        self.counters.add("say_requests")
        report = on_stage if on_stage is not None else (lambda _stage: None)
        text = (text or "").strip()
        if not text:
            return self._say_ended(False, "play_failed")
        if len(text) > MAX_SAY_CHARS:
            return self._say_ended(False, "too_long")
        code = self._refusal()
        if code:
            return self._say_ended(False, code)
        tts = self._glue.tts
        if tts is None:
            return self._say_ended(False, "no_voice")
        if not self._say_lock.acquire(blocking=False):
            return self._say_ended(False, "speaker_busy")
        held = False
        late: list[str] = []
        try:
            language = (lang or "").strip() or self._lang_state.current()

            def play(*args: Any, **kwargs: Any) -> Any:
                nonlocal held
                again = self._refusal()
                if again:
                    late.append(again)
                    raise _Refused(again)
                report("play")
                self._hold_voice()
                held = True
                try:
                    return self._play(*args, **kwargs)
                except _Refused as cut:
                    late.append(str(cut))
                    raise

            try:
                played = bool(self._speak_function()(
                    _StagedTts(tts, report), text, language, run=play))
            except Exception:  # noqa: BLE001 - a say never raises into the executor
                self.counters.complain("say_errors", "a say failed")
                played = False
            if played:
                return self._say_ended(True, "")
            return self._say_ended(False, late[0] if late else "play_failed")
        finally:
            if held:
                self._release_voice()
            self._say_lock.release()

    def _say_ended(self, played: bool, code: str) -> tuple[bool, str]:
        self.counters.add("say_played" if played else _SAY_REFUSED_PREFIX + code)
        if not played:
            log.info("say not played: %s", code)
        return played, code

    def _execute_say(self, goal_handle: Any) -> Any:
        request = goal_handle.request
        result = self._ros.Say.Result()

        def feedback(stage: str) -> None:
            try:
                message = self._ros.Say.Feedback()
                message.stage = stage
                goal_handle.publish_feedback(message)
            except Exception:  # noqa: BLE001 - feedback is a courtesy
                self.counters.complain("say_feedback_errors", "could not publish say feedback")

        played, code = self.say(request.text, request.lang, feedback)
        result.played = played
        result.error_code = code
        try:
            (goal_handle.succeed if played else goal_handle.abort)()
        except Exception:  # noqa: BLE001 - the result below is still returned
            self.counters.complain("say_goal_errors", "could not settle a say goal")
        return result

    # --- recognize -----------------------------------------------------------------

    def _timeout_s(self) -> float:
        if self._stt_timeout_s is not None:
            return self._stt_timeout_s
        from emori_adapter.hostapp import speech_api

        return float(speech_api._STT_TIMEOUT_S)

    def recognize(self, pcm: bytes, lang: str = "",
                  grammar: Sequence[str] = ()) -> tuple[bool, str, float, str]:
        """Speech to text. Returns ``(ok, text, conf, error_code)``. Never raises."""

        self.counters.add("recognize_requests")
        stt = self._glue.stt
        if stt is None:
            return self._recognized(False, "", -1.0, "stt_unavailable")
        if len(pcm) > MAX_PCM_BYTES:
            return self._recognized(False, "", -1.0, "too_long")
        if len(pcm) < MIN_PCM_BYTES:
            return self._recognized(True, "", -1.0, "")
        pcm = pcm[: len(pcm) // 2 * 2]
        language = (lang or "").strip() or self._lang_state.current()
        phrases = [str(item).strip() for item in grammar if str(item).strip()]
        previous = self._recognize_thread
        if previous is not None and previous.is_alive():
            return self._recognized(False, "", -1.0, "busy")
        duration_ms = len(pcm) // (16000 * 2 // 1000)
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                scorer = getattr(stt, "transcribe_grammar_conf", None)
                if phrases and scorer is not None:
                    outcome["text"], outcome["conf"] = scorer(pcm, phrases, language)
                elif phrases:
                    outcome["text"], outcome["conf"] = (
                        stt.transcribe_grammar(pcm, phrases, language), -1.0)
                else:
                    outcome["text"], outcome["conf"] = stt.transcribe(pcm, language), -1.0
            except BaseException as error:  # noqa: BLE001 - reported below, from the caller
                outcome["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=work, name="ros-recognize", daemon=True)
        self._recognize_thread = thread
        thread.start()
        if not done.wait(self._timeout_s()):
            log.warning("recognize timed out; the engine is left running")
            return self._recognized(False, "", -1.0, "stt_timeout")
        if "error" in outcome:
            log.warning("recognize failed: %s", type(outcome["error"]).__name__)
            return self._recognized(False, "", -1.0, "stt_failed")
        text = str(outcome.get("text", ""))
        conf = float(outcome.get("conf", -1.0))
        if phrases:
            # The slot and not the words: a grammar is a closed set, but a matched word is
            # still something a person said out loud in front of the robot.
            slot = phrases.index(text) if text in phrases else -1
            log.warning("ros menu stt: lang=%s dur_ms=%d slot=%d/%d conf=%.3f", language,
                        duration_ms, slot, len(phrases), conf)
        else:
            log.info("ros open stt: lang=%s dur_ms=%d chars=%d", language, duration_ms, len(text))
        return self._recognized(True, text, conf, "")

    def _recognized(self, ok: bool, text: str, conf: float, code: str) -> tuple[bool, str, float, str]:
        self.counters.add("recognized" if ok else "recognize_refused_" + code)
        return ok, text, conf, code

    def _on_recognize(self, request: Any, response: Any) -> Any:
        try:
            pcm = bytes(request.pcm_s16le_16k)
            ok, text, conf, code = self.recognize(pcm, request.lang, list(request.grammar))
        except Exception:  # noqa: BLE001 - a service never raises into the executor
            self.counters.complain("recognize_errors", "a recognize failed")
            ok, text, conf, code = False, "", -1.0, "stt_failed"
        response.ok, response.text, response.conf, response.error_code = ok, text, float(conf), code
        return response

    # --- set_language --------------------------------------------------------------

    def set_language(self, lang: str) -> tuple[bool, str, str]:
        """Switch the kiosk language as the phone admin does. ``(ok, lang_now, error_code)``."""

        from emori_adapter.hostapp.lang_state import normalize_lang

        self.counters.add("set_language_requests")
        current = self._current_language()
        target = normalize_lang(lang)
        check = getattr(self._lang_state, "is_allowed", None)
        if not target or (callable(check) and not check(target)):
            return False, current, "unsupported_lang"
        if target == current:
            # Before the idle gate: nothing is written, reloaded or spoken, so there is
            # nothing to protect.
            return True, current, ""
        try:
            # The switch reloads the page and speaks a line, so nobody may be using it.
            idle = bool(self._glue.robot_idle()) and bool(self._glue.landing_idle())
        except Exception:  # noqa: BLE001 - an unreadable state is busy
            idle = False
        if not idle:
            return False, current, "busy"
        try:
            switched = self._glue.set_language(target, reason="ros set_language")
        except Exception:  # noqa: BLE001 - the caller gets a code, not a traceback
            self.counters.complain("set_language_errors", "the language switch failed")
            return False, self._current_language(), "lang_switch_failed"
        return True, normalize_lang(switched) or self._current_language(), ""

    def _current_language(self) -> str:
        try:
            return str(self._lang_state.current())
        except Exception:  # noqa: BLE001
            return ""

    def _on_set_language(self, request: Any, response: Any) -> Any:
        try:
            ok, lang, code = self.set_language(request.lang)
        except Exception:  # noqa: BLE001
            self.counters.complain("set_language_errors", "a set_language failed")
            ok, lang, code = False, self._current_language(), "lang_switch_failed"
        response.ok, response.lang, response.error_code = ok, lang, code
        return response

    # --- what is published ---------------------------------------------------------

    def _publish(self, publisher: Any, message: Any, counter: str) -> bool:
        try:
            publisher.publish(message)
        except Exception:  # noqa: BLE001 - a topic gives way; the count says so
            self.counters.complain(counter, f"could not publish ({counter})")
            return False
        return True

    def _read_voice(self) -> Optional[float]:
        message = self._ros.VoiceState()
        voice = getattr(self._driver, "voice", None)
        started = self._clock()
        if voice is None:
            snapshot: Mapping[str, Any] = {"enabled": False, "available": False}
        else:
            reader = getattr(voice, "snapshot", None)
            snapshot = reader() if callable(reader) else {"enabled": True, "available": False}
        took = self._clock() - started
        silent = snapshot.get("silent_for_s")
        message.enabled = bool(snapshot.get("enabled", False))
        message.available = bool(snapshot.get("available", False))
        message.active = bool(snapshot.get("active", False))
        message.level = float(snapshot.get("level", 0.0))
        message.rms = float(snapshot.get("rms", 0.0))
        message.noise_rms = float(snapshot.get("noise_rms", 0.0))
        message.onsets = int(snapshot.get("onsets", 0))
        message.frames = int(snapshot.get("frames", 0))
        message.silent_for_s = -1.0 if silent is None else float(silent)
        stamp(self.node, message)
        self._publish(self._voice_pub, message, "voice_publish_errors")
        if took > VOICE_SLOW_S:
            self.counters.add("voice_slow")
            return VOICE_BACKOFF_S
        return None

    def _read_state(self) -> None:
        current = self._current_language()
        allowed = tuple(str(code) for code in (getattr(self._lang_state, "allowed", ()) or ()))
        if (current, allowed) != self._last_language:
            message = self._ros.KioskLanguage()
            message.current = current
            message.allowed = list(allowed)
            stamp(self.node, message)
            if self._publish(self._language_pub, message, "language_publish_errors"):
                self._last_language = (current, allowed)
        profile = getattr(self._glue.mic_profile, "profile", None)
        profile = profile if isinstance(profile, str) else ""
        if not self._mic_published or profile != self._last_mic:
            message = self._ros_string()
            message.data = profile
            if self._publish(self._mic_pub, message, "mic_publish_errors"):
                self._last_mic, self._mic_published = profile, True

    def _ros_string(self) -> Any:
        return self._ros.kit.String()


def build_speech_node(app: Any, *, bindings: Optional[SpeechBindings] = None) -> Any:
    """The ``/emori/speech`` node for ``app``, started. Destroying it stops what it started."""

    facade = SpeechFacade(app, bindings if bindings is not None else load_speech_bindings())
    facade.start()
    return release_with(facade.node, facade.close, facade)
