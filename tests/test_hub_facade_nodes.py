"""The hub's speech and gesture nodes: what they offer, what they refuse, what they cost a session.

No ROS is needed. The nodes are built on fake bindings, over an app whose speech engines, add-on
and driver are fakes, and over the real ``GestureTicker`` and ``LangState`` where those are what
is being tested.

The first group is about the path a session's motion takes (``POST /api/cycle``, the driver, the
cue scheduler, the player, the robot client). The nodes are shown to leave it alone in three
ways: a driver and a player that record every name they are asked for show that the nodes read
only what they are meant to; the source of both modules is held to a list of names that belong
to that path; and a voice tap that never answers shows that the thread waiting on it is the only
one that is stuck.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from emori_hub import facade_kit  # noqa: E402
from emori_hub import gesture_node as gn  # noqa: E402
from emori_hub import main as hub  # noqa: E402
from emori_hub import speech_node as sn  # noqa: E402

from emori_adapter.hostapp.driver import GestureTicker, RobotPlayer  # noqa: E402
from emori_adapter.hostapp.gestures import GestureDirection  # noqa: E402
from emori_adapter.hostapp.lang_state import LangState  # noqa: E402


def _wait_until(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    return predicate()


# --- ROS, faked ------------------------------------------------------------------------


class _Msg:
    FIELDS: dict[str, Any] = {}

    def __init__(self) -> None:
        self.header = SimpleNamespace(stamp=None)
        for name, value in self.FIELDS.items():
            setattr(self, name, list(value) if isinstance(value, list) else value)


def _msg(name: str, fields: dict[str, Any], **constants: int) -> type:
    return type(name, (_Msg,), {"FIELDS": fields, **constants})


GestureEvent = _msg("GestureEvent", {"kind": 0, "direction": ""}, KIND_HEAD_TURN=0,
                    KIND_END_NUDGE=1, KIND_BEGIN=2, KIND_LANG_TOGGLE=3)
GestureState = _msg("GestureState", {"mode": "", "head_reserved": False, "residual_live": False,
                                     "head_held": False, "robot_moving": False,
                                     "moving_reason": "", "selection_reserved": False})
VoiceState = _msg("VoiceState", {"enabled": False, "available": False, "active": False,
                                 "level": 0.0, "rms": 0.0, "noise_rms": 0.0, "onsets": 0,
                                 "frames": 0, "silent_for_s": 0.0})
KioskLanguage = _msg("KioskLanguage", {"current": "", "allowed": []})
String = _msg("String", {"data": ""})


class _Reply:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class Recognize:
    class Request:
        def __init__(self, pcm: bytes = b"", lang: str = "", grammar: list | None = None):
            self.pcm_s16le_16k, self.lang, self.grammar = pcm, lang, grammar or []

    Response = _Reply


class SetLanguage:
    class Request:
        def __init__(self, lang: str = "") -> None:
            self.lang = lang

    Response = _Reply


class SetBool:
    class Request:
        def __init__(self, data: bool = False) -> None:
            self.data = data

    Response = _Reply


class Trigger:
    class Request:
        pass

    Response = _Reply


class Say:
    class Goal:
        def __init__(self, text: str = "", lang: str = "") -> None:
            self.text, self.lang = text, lang

    class Result:
        def __init__(self) -> None:
            self.played, self.error_code = False, ""

    class Feedback:
        def __init__(self) -> None:
            self.stage = ""


class FakePublisher:
    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.messages: list[Any] = []
        self.fail = False
        self.subscribers = 0
        self.log: list[Any] | None = None

    def publish(self, message: Any) -> None:
        if self.fail:
            raise RuntimeError("the middleware refused")
        self.messages.append(message)
        if self.log is not None:
            self.log.append(("publish", self.topic))

    def get_subscription_count(self) -> int:
        return self.subscribers


class FakeNode:
    def __init__(self, name: str, namespace: str) -> None:
        self.name, self.namespace = name, namespace
        self.publishers: dict[str, FakePublisher] = {}
        self.services: dict[str, tuple[Callable, Any]] = {}
        self.timers: list[tuple[float, Callable, Any]] = []
        self.destroyed = 0

    def create_publisher(self, _type: Any, topic: str, qos: Any) -> FakePublisher:
        publisher = FakePublisher(topic)
        publisher.qos = qos
        self.publishers[topic] = publisher
        return publisher

    def create_service(self, _type: Any, name: str, callback: Callable, *,
                       callback_group: Any = None) -> None:
        self.services[name] = (callback, callback_group)

    def create_timer(self, period: float, callback: Callable, *, callback_group: Any = None):
        self.timers.append((period, callback, callback_group))
        return object()

    def get_clock(self) -> Any:
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "stamp"))

    def destroy_node(self) -> None:
        self.destroyed += 1


class FakeActionServer:
    def __init__(self, node: FakeNode, type_: Any, name: str, *, execute_callback: Callable,
                 goal_callback: Callable, cancel_callback: Callable, callback_group: Any) -> None:
        self.node, self.name = node, name
        self.execute = execute_callback
        self.goal_callback, self.cancel_callback = goal_callback, cancel_callback
        self.callback_group = callback_group
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class FakeGoal:
    def __init__(self, text: str, lang: str = "") -> None:
        self.request = Say.Goal(text, lang)
        self.feedback: list[str] = []
        self.outcome = ""

    def publish_feedback(self, message: Any) -> None:
        self.feedback.append(message.stage)

    def succeed(self) -> None:
        self.outcome = "succeeded"

    def abort(self) -> None:
        self.outcome = "aborted"


def _kit() -> facade_kit.KitBindings:
    return facade_kit.KitBindings(
        create_node=lambda name, namespace="": FakeNode(name, namespace),
        group_exclusive=lambda: SimpleNamespace(kind="exclusive"),
        group_reentrant=lambda: SimpleNamespace(kind="reentrant"),
        qos_latched="latched", qos_events="events", qos_state="state",
        String=String, Trigger=Trigger, SetBool=SetBool)


def _speech_bindings() -> sn.SpeechBindings:
    return sn.SpeechBindings(
        kit=_kit(), action_server_cls=FakeActionServer,
        goal_response=SimpleNamespace(ACCEPT="accept", REJECT="reject"),
        cancel_response=SimpleNamespace(ACCEPT="accept", REJECT="reject"),
        Say=Say, Recognize=Recognize, SetLanguage=SetLanguage, VoiceState=VoiceState,
        KioskLanguage=KioskLanguage)


def _gesture_bindings() -> gn.GestureBindings:
    return gn.GestureBindings(kit=_kit(), GestureEvent=GestureEvent, GestureState=GestureState)


# --- the app, faked --------------------------------------------------------------------


class Tracker:
    def __init__(self) -> None:
        self.active = False
        self.exchanged = False


class Player:
    """What the nodes may touch of the player: ``busy``, ``hold_voice``, ``release_voice``."""

    def __init__(self) -> None:
        self.busy = False
        self.holds: list[bool] = []
        self.released: list[float] = []

    def hold_voice(self, on: bool) -> float:
        self.holds.append(on)
        return float(len(self.holds))

    def release_voice(self, held_until: float) -> None:
        self.released.append(held_until)
        self.holds.append(False)


class Extension:
    def __init__(self) -> None:
        self.is_busy = False
        self.reserved = False
        self.raises = False

    def busy(self) -> bool:
        if self.raises:
            raise RuntimeError("the add-on cannot say")
        return self.is_busy

    def selection_reserved(self) -> bool:
        return self.reserved


class Tts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.on_synth: Callable[[], Any] = lambda: None

    def synth_path(self, text: str, lang: str = "en") -> str:
        self.calls.append((text, lang))
        self.on_synth()
        return "voice.wav"


class Stt:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.release = threading.Event()
        self.release.set()
        self.fail = False

    def _wait(self) -> None:
        self.release.wait(10)
        if self.fail:
            raise RuntimeError("the engine broke")

    def transcribe(self, pcm: bytes, lang: str) -> str:
        self.calls.append(("open", len(pcm), lang))
        self._wait()
        return "open words"

    def transcribe_grammar_conf(self, pcm: bytes, phrases: list, lang: str):
        self.calls.append(("grammar", len(pcm), lang, tuple(phrases)))
        self._wait()
        return phrases[1], 0.875


class Voice:
    def __init__(self) -> None:
        self.snapshots = 0
        self.block = threading.Event()
        self.block.set()

    def snapshot(self) -> dict:
        self.snapshots += 1
        self.block.wait(10)
        return {"enabled": True, "available": True, "active": True, "level": 0.5, "rms": 120.0,
                "noise_rms": 8.0, "onsets": 3, "frames": 99, "silent_for_s": None}


class MicProfile:
    def __init__(self) -> None:
        self.profile: str | None = None


class _RecordingDriver:
    """A driver that remembers every name asked of it, to show what a node reads."""

    ALLOWED = ("tracker", "player", "voice", "counters", "touch")

    def __init__(self) -> None:
        self.tracker = Tracker()
        self.player = Player()
        self.voice = Voice()
        self.counters: Counter = Counter()
        self.touched = 0
        self.asked: set[str] = set()

    def touch(self) -> None:
        self.touched += 1

    def __getattr__(self, name: str) -> Any:  # only called for names that are not set
        self.asked.add(name)
        raise AttributeError(name)


class _Glue:
    def __init__(self, tts: Tts | None, stt: Stt | None, mic: MicProfile) -> None:
        self.tts, self.stt, self.mic_profile = tts, stt, mic
        self.session_live = False
        self.idle = True
        self.landing = True
        self.switched: list[tuple] = []
        self.switch_raises = False
        self._lang_state: LangState | None = None

    def live_session(self) -> bool:
        return self.session_live

    def robot_idle(self) -> bool:
        return self.idle

    def landing_idle(self) -> bool:
        return self.landing

    def set_language(self, target: str, *, reason: str = "") -> str:
        if self.switch_raises:
            raise RuntimeError("could not write the state")
        self.switched.append((target, reason))
        assert self._lang_state is not None
        return self._lang_state.set(target)


def _app(tmp_path: Path, *, with_ticker: bool = False, on_direction: Any = "hook") -> SimpleNamespace:
    driver = _RecordingDriver()
    lang_state = LangState(default="en", path=str(tmp_path / "lang.json"), allowed=("en", "ca"))
    glue = _Glue(Tts(), Stt(), MicProfile())
    glue._lang_state = lang_state
    extension = Extension()
    ticker = None
    if with_ticker:
        hook = (lambda direction: None) if on_direction == "hook" else on_direction
        ticker = GestureTicker(driver, on_direction=hook)
    state = SimpleNamespace(glue=glue, driver=driver, extension=extension,
                            lang_state=lang_state, gesture_ticker=ticker)
    return SimpleNamespace(state=state)


def _speech(app: SimpleNamespace, **kwargs: Any) -> sn.SpeechFacade:
    played: list[list] = kwargs.pop("played", [])
    returncode = kwargs.pop("returncode", 0)

    def play(argv: list, **_kw: Any) -> Any:
        played.append(argv)
        return SimpleNamespace(returncode=returncode)

    kwargs.setdefault("play", play)
    facade = sn.SpeechFacade(app, _speech_bindings(), **kwargs)
    facade.played = played  # type: ignore[attr-defined]
    return facade


# --- speech: say -----------------------------------------------------------------------


def test_the_speech_node_offers_the_interfaces_in_the_design(tmp_path):
    facade = _speech(_app(tmp_path))
    node = facade.node

    assert (node.name, node.namespace) == ("speech", "/emori")
    assert set(node.publishers) == {"/emori/speech/voice", "/emori/language",
                                    "/emori/audio/mic_profile"}
    assert set(node.services) == {"/emori/speech/recognize", "/emori/speech/set_language"}
    assert facade._say_server.name == "/emori/speech/say"
    assert node.publishers["/emori/speech/voice"].qos == "state"
    assert node.publishers["/emori/language"].qos == "latched"
    assert node.publishers["/emori/audio/mic_profile"].qos == "latched"
    # The two blocking services share one exclusive group; the sentence has a group of its own.
    recognize_group = node.services["/emori/speech/recognize"][1]
    assert recognize_group is node.services["/emori/speech/set_language"][1]
    assert recognize_group.kind == "exclusive"
    assert facade._say_server.callback_group.kind == "reentrant"
    # Nothing runs in an executor timer.
    assert node.timers == []


def test_say_plays_the_sentence_holds_the_voice_tap_and_lets_go(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)
    stages: list[str] = []

    played, code = facade.say("Now in English.", "en", stages.append)

    assert (played, code) == (True, "")
    assert stages == ["synth", "play"]
    assert app.state.glue.tts.calls == [("Now in English.", "en")]
    assert facade.played[0][0] == "pw-play" and facade.played[0][-1] == "voice.wav"
    # The player is told before the sound starts and again when it has ended.
    assert app.state.driver.player.holds == [True, False]


def test_say_is_refused_unless_the_kiosk_is_on_an_idle_landing_page(tmp_path):
    """A finished session's summary, or a menu read to someone with the microphone open."""

    app = _app(tmp_path)
    app.state.glue.landing = False
    facade = _speech(app)
    assert facade.say("hello", "en") == (False, "kiosk_busy")
    assert app.state.glue.tts.calls == [] and facade.played == []


def test_say_is_refused_over_its_length(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)
    assert facade.say("x" * (sn.MAX_SAY_CHARS + 1), "en") == (False, "too_long")
    assert app.state.glue.tts.calls == []
    assert facade.say("x" * sn.MAX_SAY_CHARS, "en") == (True, "")


class _Sound:
    """A player process, ending when told."""

    def __init__(self, ends_after: int = 1 << 30) -> None:
        self.waits = 0
        self.ends_after = ends_after
        self.terminated = False

    def wait(self, timeout: float) -> int:
        self.waits += 1
        if self.terminated or self.waits > self.ends_after:
            return 0
        raise subprocess.TimeoutExpired("pw-play", timeout)

    def poll(self) -> Any:
        return 0 if self.terminated or self.waits > self.ends_after else None

    def terminate(self) -> None:
        self.terminated = True

    kill = terminate


def _playing(app: SimpleNamespace, sound: _Sound, **kwargs: Any) -> sn.SpeechFacade:
    facade = sn.SpeechFacade(app, _speech_bindings(), **kwargs)
    facade._popen = lambda *_a, **_k: sound  # type: ignore[assignment,method-assign]
    return facade


def test_a_sentence_that_plays_to_its_end_is_played(tmp_path):
    app = _app(tmp_path)
    sound = _Sound(ends_after=3)
    assert _playing(app, sound).say("hello", "en") == (True, "")
    assert not sound.terminated
    assert app.state.driver.player.holds == [True, False]


def test_a_session_opening_stops_a_sentence_that_is_playing(tmp_path):
    app = _app(tmp_path)
    sound = _Sound()
    original = sound.wait

    def wait(timeout: float) -> int:
        if sound.waits == 2:
            app.state.driver.tracker.active = True
        return original(timeout)

    sound.wait = wait  # type: ignore[method-assign]
    facade = _playing(app, sound)
    assert facade.say("hello", "en") == (False, "session_active")
    assert sound.terminated
    assert facade.stats()["say_cut"] == 1
    assert app.state.driver.player.released == [1.0], "its own hold goes with it"


def test_a_sentence_that_outlasts_its_time_is_stopped(tmp_path):
    app = _app(tmp_path)
    now = [0.0]

    def clock() -> float:
        now[0] += 30.0
        return now[0]

    sound = _Sound()
    facade = _playing(app, sound, clock=clock)
    assert facade.say("hello", "en") == (False, "play_failed")
    assert sound.terminated


def test_a_long_sentence_renews_its_hold_and_releases_the_last_one(tmp_path):
    app = _app(tmp_path)
    now = [0.0]

    def clock() -> float:
        now[0] += sn._HOLD_RENEW_S
        return now[0]

    sound = _Sound(ends_after=2)
    assert _playing(app, sound, clock=clock).say("hello", "en") == (True, "")
    player = app.state.driver.player
    assert player.holds.count(True) >= 2
    assert player.released == [float(player.holds.count(True))], "the newest hold, not the first"


def test_a_cover_the_page_took_meanwhile_is_not_cleared():
    player = RobotPlayer.__new__(RobotPlayer)
    player._cover_until = 0.0
    ours = player.hold_voice(True)
    player.release_voice(ours)
    assert player._cover_until == 0.0
    ours = player.hold_voice(True)
    the_pages = player._cover_until = ours + 1.0
    player.release_voice(ours)
    assert player._cover_until == the_pages


def test_say_without_a_language_speaks_the_kiosk_language(tmp_path):
    app = _app(tmp_path)
    app.state.lang_state.set("ca")
    facade = _speech(app)

    facade.say("Ara en catala.", "")

    assert app.state.glue.tts.calls == [("Ara en catala.", "ca")]


@pytest.mark.parametrize("how", ["live_session", "tracker"])
def test_say_is_refused_while_a_session_owns_the_speaker(tmp_path, how):
    app = _app(tmp_path)
    if how == "live_session":
        app.state.glue.session_live = True
    else:
        app.state.driver.tracker.active = True
    facade = _speech(app)

    assert facade.say("hello", "en") == (False, "session_active")

    # Refused before anything is made: no synthesis, no sound, no hold on the voice tap.
    assert app.state.glue.tts.calls == []
    assert facade.played == []
    assert app.state.driver.player.holds == []


@pytest.mark.parametrize("busy, reserved, raises", [(True, False, False), (False, True, False),
                                                    (False, False, True)])
def test_say_is_refused_while_an_addon_holds_the_speaker(tmp_path, busy, reserved, raises):
    """A mode that listens while the robot speaks would take a foreign sentence for a participant's."""

    app = _app(tmp_path)
    app.state.extension.is_busy, app.state.extension.reserved = busy, reserved
    app.state.extension.raises = raises
    facade = _speech(app)

    assert facade.say("hello", "en") == (False, "addon_busy")

    assert app.state.glue.tts.calls == []
    assert facade.played == []
    assert app.state.driver.player.holds == []
    assert facade.stats()["say_refused_addon_busy"] == 1


def test_say_asks_again_after_synthesis_and_does_not_start_the_sound(tmp_path):
    """Synthesis takes seconds; a mode can start in them."""

    app = _app(tmp_path)
    app.state.glue.tts.on_synth = lambda: setattr(app.state.extension, "is_busy", True)
    facade = _speech(app)
    stages: list[str] = []

    assert facade.say("hello", "en", stages.append) == (False, "addon_busy")

    assert stages == ["synth"]
    assert facade.played == []
    assert app.state.driver.player.holds == []


def test_say_without_a_voice_engine(tmp_path):
    app = _app(tmp_path)
    app.state.glue.tts = None

    assert _speech(app).say("hello", "en") == (False, "no_voice")


def test_a_sound_that_does_not_play_still_lets_the_voice_tap_go(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app, returncode=1)

    assert facade.say("hello", "en") == (False, "play_failed")
    assert app.state.driver.player.holds == [True, False]


def test_an_empty_sentence_is_not_played(tmp_path):
    app = _app(tmp_path)

    assert _speech(app).say("   ", "en") == (False, "play_failed")
    assert app.state.glue.tts.calls == []


def test_a_second_say_is_refused_while_one_is_playing(tmp_path):
    app = _app(tmp_path)
    inside, release = threading.Event(), threading.Event()
    facade = sn.SpeechFacade(app, _speech_bindings(),
                             play=lambda *_a, **_k: (inside.set(), release.wait(10),
                                                     SimpleNamespace(returncode=0))[2])
    first: list[tuple] = []
    thread = threading.Thread(target=lambda: first.append(facade.say("one", "en")))
    thread.start()
    assert inside.wait(5)

    assert facade.say("two", "en") == (False, "speaker_busy")
    release.set()
    thread.join(5)

    assert first == [(True, "")]
    assert app.state.driver.player.holds == [True, False]


def test_the_action_reports_stages_and_settles_the_goal(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)
    goal = FakeGoal("hello", "en")

    result = facade._say_server.execute(goal)

    assert (result.played, result.error_code) == (True, "")
    assert goal.feedback == ["synth", "play"] and goal.outcome == "succeeded"

    app.state.extension.is_busy = True
    refused = FakeGoal("hello", "en")
    result = facade._say_server.execute(refused)
    assert (result.played, result.error_code) == (False, "addon_busy")
    assert refused.outcome == "aborted"


def test_the_action_accepts_goals_and_declines_to_cancel_a_sound_already_playing(tmp_path):
    server = _speech(_app(tmp_path))._say_server

    assert server.goal_callback(object()) == "accept"
    assert server.cancel_callback(object()) == "reject"


# --- speech: recognize -----------------------------------------------------------------


def test_recognize_in_open_mode_has_no_confidence(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)

    result = facade.recognize(b"\0" * 6400, "", [])

    assert result == (True, "open words", -1.0, "")
    assert app.state.glue.stt.calls == [("open", 6400, "en")]


def test_recognize_on_a_grammar_reports_the_score_and_logs_the_slot_only(tmp_path, caplog):
    app = _app(tmp_path)
    facade = _speech(app)

    with caplog.at_level(logging.INFO, logger="emori.hub.speech"):
        ok, text, conf, code = facade.recognize(b"\0" * 6400, "ca", ["one", "two", "three"])

    assert (ok, text, conf, code) == (True, "two", 0.875, "")
    assert app.state.glue.stt.calls == [("grammar", 6400, "ca", ("one", "two", "three"))]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "slot=1/3" in logged and "conf=0.875" in logged
    assert "two" not in logged.replace("slot=1/3", "")


def test_recognize_does_not_log_what_was_said_in_open_mode(tmp_path, caplog):
    facade = _speech(_app(tmp_path))

    with caplog.at_level(logging.DEBUG, logger="emori.hub.speech"):
        facade.recognize(b"\0" * 6400, "en", [])

    assert "open words" not in " ".join(record.getMessage() for record in caplog.records)


def test_recognize_ignores_a_blip_and_refuses_what_is_too_long_or_has_no_engine(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)

    assert facade.recognize(b"\0" * 3000, "en", []) == (True, "", -1.0, "")
    assert facade.recognize(b"\0" * (sn.MAX_PCM_BYTES + 2), "en", []) == (False, "", -1.0,
                                                                          "too_long")
    assert app.state.glue.stt.calls == []
    app.state.glue.stt = None
    assert facade.recognize(b"\0" * 6400, "en", []) == (False, "", -1.0, "stt_unavailable")


def test_recognize_reports_an_engine_that_fails(tmp_path):
    app = _app(tmp_path)
    app.state.glue.stt.fail = True

    assert _speech(app).recognize(b"\0" * 6400, "en", []) == (False, "", -1.0, "stt_failed")


def test_a_wedged_engine_costs_one_thread_and_the_next_call_is_refused_until_it_ends(tmp_path):
    app = _app(tmp_path)
    app.state.glue.stt.release.clear()
    facade = _speech(app, stt_timeout_s=0.05)

    assert facade.recognize(b"\0" * 6400, "en", []) == (False, "", -1.0, "stt_timeout")
    assert facade.recognize(b"\0" * 6400, "en", []) == (False, "", -1.0, "busy")
    assert len(app.state.glue.stt.calls) == 1

    app.state.glue.stt.release.set()
    _wait_until(lambda: not facade._recognize_thread.is_alive())
    assert facade.recognize(b"\0" * 6400, "en", [])[0] is True


def test_the_recognize_service_fills_the_response(tmp_path):
    facade = _speech(_app(tmp_path))
    callback = facade.node.services["/emori/speech/recognize"][0]

    response = callback(Recognize.Request(bytes(6400), "en", ["a", "b"]), Recognize.Response())

    assert (response.ok, response.text, response.conf, response.error_code) == (True, "b", 0.875, "")


# --- speech: set_language --------------------------------------------------------------


def test_set_language_does_the_whole_switch_through_the_apps_own_function(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)

    assert facade.set_language("ca") == (True, "ca", "")
    assert app.state.glue.switched == [("ca", "ros set_language")]


def test_set_language_refuses_what_the_robot_does_not_offer(tmp_path):
    app = _app(tmp_path)

    assert _speech(app).set_language("de") == (False, "en", "unsupported_lang")
    assert _speech(app).set_language("") == (False, "en", "unsupported_lang")
    assert app.state.glue.switched == []


def test_set_language_to_the_language_in_force_is_ok_even_when_busy(tmp_path):
    app = _app(tmp_path)
    app.state.glue.idle = False

    assert _speech(app).set_language("en") == (True, "en", "")
    assert app.state.glue.switched == []


def test_set_language_is_busy_unless_the_robot_is_idle(tmp_path):
    app = _app(tmp_path)
    app.state.glue.idle = False

    assert _speech(app).set_language("ca") == (False, "en", "busy")
    assert app.state.glue.switched == []


def test_set_language_reports_a_switch_that_failed(tmp_path):
    app = _app(tmp_path)
    app.state.glue.switch_raises = True

    assert _speech(app).set_language("ca") == (False, "en", "lang_switch_failed")


def test_the_set_language_service_fills_the_response(tmp_path):
    facade = _speech(_app(tmp_path))
    callback = facade.node.services["/emori/speech/set_language"][0]

    response = callback(SetLanguage.Request("ca"), SetLanguage.Response())

    assert (response.ok, response.lang, response.error_code) == (True, "ca", "")


# --- speech: what is published ---------------------------------------------------------


def test_the_language_and_the_mic_profile_are_published_on_change_only(tmp_path):
    app = _app(tmp_path)
    facade = _speech(app)
    language = facade.node.publishers["/emori/language"]
    mic = facade.node.publishers["/emori/audio/mic_profile"]

    facade._read_state()
    facade._read_state()
    assert [(m.current, m.allowed) for m in language.messages] == [("en", ["en", "ca"])]
    assert [m.data for m in mic.messages] == [""]

    app.state.lang_state.set("ca")
    app.state.glue.mic_profile.profile = "session"
    facade._read_state()
    facade._read_state()
    assert [m.current for m in language.messages] == ["en", "ca"]
    assert [m.data for m in mic.messages] == ["", "session"]
    assert language.messages[0].header.stamp == "stamp"


def test_a_topic_that_cannot_be_published_is_counted_and_tried_again(tmp_path):
    facade = _speech(_app(tmp_path))
    language = facade.node.publishers["/emori/language"]
    language.fail = True

    facade._read_state()
    facade._read_state()
    assert facade.stats()["language_publish_errors"] == 2

    language.fail = False
    facade._read_state()
    assert len(language.messages) == 1


def test_the_voice_is_read_only_while_somebody_subscribes(tmp_path):
    app = _app(tmp_path)
    voice = app.state.driver.voice
    facade = _speech(app)
    publisher = facade.node.publishers["/emori/speech/voice"]
    facade.start()
    try:
        time.sleep(0.8)
        assert voice.snapshots == 0 and publisher.messages == []

        publisher.subscribers = 1
        assert _wait_until(lambda: len(publisher.messages) >= 2)
        message = publisher.messages[-1]
        assert (message.enabled, message.available, message.active) == (True, True, True)
        assert message.silent_for_s == -1.0 and message.onsets == 3

        publisher.subscribers = 0
        time.sleep(0.7)
        seen = voice.snapshots
        time.sleep(0.4)
        assert voice.snapshots == seen
    finally:
        facade.close()


def test_a_slow_voice_read_backs_the_topic_off_and_is_counted(tmp_path):
    ticks = iter(range(0, 1000))
    slow = [True]

    def clock() -> float:
        # Two reads of the clock per voice read: the second is later by a slow or a quick amount.
        step = next(ticks)
        return step * (sn.VOICE_SLOW_S * 4 if slow[0] else sn.VOICE_SLOW_S / 100)

    facade = _speech(_app(tmp_path), clock=clock)

    assert facade._read_voice() == sn.VOICE_BACKOFF_S
    assert facade.stats()["voice_slow"] == 1

    slow[0] = False
    assert facade._read_voice() is None
    assert facade.stats()["voice_slow"] == 1


def test_a_robot_without_a_voice_tap_says_so(tmp_path):
    app = _app(tmp_path)
    app.state.driver.voice = None
    facade = _speech(app)

    facade._read_voice()

    message = facade.node.publishers["/emori/speech/voice"].messages[0]
    assert (message.enabled, message.available) == (False, False)


# --- the path a session's motion takes -------------------------------------------------

#: Names that belong to that path. Neither node may refer to one, as a name or an attribute.
_MOTION_PATH_NAMES = {
    "on_utterance", "on_sentence", "on_analysis", "plan_cue", "_play_batch", "submit",
    "submit_legacy_motion", "apply", "play_recorded_move", "robot_client", "set_target",
    "RobotPlayer", "CueScheduler", "enqueue_cmd", "read_state", "set_head_reserved",
}


@pytest.mark.parametrize("module", [sn, gn, facade_kit], ids=lambda m: m.__name__.split(".")[-1])
def test_the_nodes_name_nothing_on_the_motion_path(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    mentioned = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    mentioned |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    mentioned |= {node.name for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.ClassDef))}

    assert mentioned & _MOTION_PATH_NAMES == set()


def test_the_nodes_read_only_what_they_are_meant_to_of_the_driver_and_the_player(tmp_path):
    """Everything both nodes do, against a driver that records any name it is asked for."""

    app = _app(tmp_path, with_ticker=True)
    driver = app.state.driver
    speech = _speech(app)
    gesture = gn.GestureFacade(app, _gesture_bindings(), environ={gn.INJECT_VAR: "1"})
    speech.node.publishers["/emori/speech/voice"].subscribers = 1

    speech.say("hello", "en")
    speech.recognize(b"\0" * 6400, "en", ["a", "b"])
    speech.set_language("ca")
    speech._read_voice()
    speech._read_state()
    gesture.inject_turn(True)
    gesture.reset_sensing()
    gesture._tick_events()
    gesture._tick_state()
    gesture.close()
    speech.close()

    assert driver.asked == set()
    assert driver.player.holds == [True, False]


def test_a_voice_tap_that_never_answers_stops_its_own_thread_and_nothing_else(tmp_path):
    app = _app(tmp_path)
    voice = app.state.driver.voice
    voice.block.clear()
    facade = _speech(app)
    publisher = facade.node.publishers["/emori/speech/voice"]
    publisher.subscribers = 1
    facade.start()
    try:
        assert _wait_until(lambda: voice.snapshots == 1)

        # The language is published by another thread, and a sentence and a recognition are
        # answered by the callers' own.
        assert _wait_until(lambda: len(facade.node.publishers["/emori/language"].messages) == 1)
        assert facade.say("hello", "en") == (True, "")
        assert facade.recognize(b"\0" * 6400, "en", [])[0] is True
        assert app.state.driver.player.holds == [True, False]
        assert publisher.messages == []
    finally:
        voice.block.set()
        facade.close()


def test_closing_stops_the_threads(tmp_path):
    facade = _speech(_app(tmp_path))
    facade.start()
    assert _wait_until(lambda: facade.node.publishers["/emori/language"].messages)

    facade.close()
    facade.close()

    assert _wait_until(lambda: not [t for t in threading.enumerate()
                                    if t.name.startswith("ros-speech") and t.is_alive()])
    assert facade._say_server.destroyed is True


# --- gesture ---------------------------------------------------------------------------


def _gesture(app: SimpleNamespace, **environ: str) -> gn.GestureFacade:
    return gn.GestureFacade(app, _gesture_bindings(), environ=environ)


def test_the_gesture_node_offers_the_interfaces_in_the_design(tmp_path):
    facade = _gesture(_app(tmp_path, with_ticker=True))
    node = facade.node

    assert (node.name, node.namespace) == ("gesture", "/emori")
    assert set(node.publishers) == {"/emori/gesture/events", "/emori/gesture/state"}
    assert node.publishers["/emori/gesture/events"].qos == "events"
    assert node.publishers["/emori/gesture/state"].qos == "latched"
    assert set(node.services) == {"/emori/gesture/reset_sensing", "/emori/gesture/inject_turn"}
    # One timer, because each wake-up costs the whole process; the services run apart from it.
    assert [period for period, _cb, _group in node.timers] == [0.2]
    assert node.services["/emori/gesture/reset_sensing"][1] is not node.timers[0][2]


def test_one_tick_publishes_both_the_events_and_the_state(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    facade = _gesture(app)
    _period, tick, _group = facade.node.timers[0]
    app.state.driver.counters["gesture_end"] = 1
    tick()
    assert len(facade.node.publishers["/emori/gesture/events"].messages) == 1
    assert len(facade.node.publishers["/emori/gesture/state"].messages) == 1


def test_an_injected_turn_is_refused_during_a_badges_lease(tmp_path):
    turns: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=turns.append)
    app.state.gesture_ticker._training_reserved = lambda: True
    facade = gn.GestureFacade(app, _gesture_bindings(), environ={gn.INJECT_VAR: "1"})
    assert facade.inject_turn(True) == (False, "training_reserved")
    assert turns == []


def test_a_head_turn_is_published_and_then_the_hook_runs(tmp_path):
    order: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=lambda d: order.append(("hook", d)))
    facade = _gesture(app)
    facade.node.publishers["/emori/gesture/events"].log = order
    ticker = app.state.gesture_ticker

    returned = ticker.on_direction(GestureDirection.LEFT)

    assert returned is None
    assert order == [("publish", "/emori/gesture/events"), ("hook", GestureDirection.LEFT)]
    event = facade.node.publishers["/emori/gesture/events"].messages[0]
    assert (event.kind, event.direction, event.header.stamp) == (GestureEvent.KIND_HEAD_TURN,
                                                                 "left", "stamp")


def test_a_publish_that_raises_does_not_stop_the_hook(tmp_path):
    calls: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=lambda d: calls.append(d) or "handled")
    facade = _gesture(app)
    facade.node.publishers["/emori/gesture/events"].fail = True

    result = app.state.gesture_ticker.on_direction(GestureDirection.RIGHT)

    assert calls == [GestureDirection.RIGHT] and result == "handled"
    assert facade.stats()["turn_publish_errors"] == 1


def test_a_hook_that_raises_still_raises_after_the_event_is_published(tmp_path):
    def hook(_direction: Any) -> None:
        raise ValueError("the add-on refused")

    app = _app(tmp_path, with_ticker=True, on_direction=hook)
    facade = _gesture(app)

    with pytest.raises(ValueError):
        app.state.gesture_ticker.on_direction(GestureDirection.LEFT)

    assert len(facade.node.publishers["/emori/gesture/events"].messages) == 1


def test_the_ticker_reaches_the_tee_when_it_recognises_a_turn(tmp_path):
    calls: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=calls.append)
    facade = _gesture(app)

    app.state.gesture_ticker._dispatch_direction(GestureDirection.LEFT, recenter=False)

    assert calls == [GestureDirection.LEFT]
    assert len(facade.node.publishers["/emori/gesture/events"].messages) == 1
    assert app.state.driver.touched == 1


def test_there_is_no_tee_where_there_is_no_hook(tmp_path):
    app = _app(tmp_path, with_ticker=True, on_direction=None)
    facade = _gesture(app)

    assert app.state.gesture_ticker.on_direction is None
    assert facade.stats()["tee_installed"] is False


def test_a_hook_is_not_wrapped_twice(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    first = _gesture(app)
    second = _gesture(app)

    app.state.gesture_ticker.on_direction(GestureDirection.LEFT)

    assert len(first.node.publishers["/emori/gesture/events"].messages) == 1
    assert second.node.publishers["/emori/gesture/events"].messages == []


def test_taking_the_tee_off_gives_back_the_hook_it_found(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    original = app.state.gesture_ticker.on_direction
    facade = _gesture(app)
    assert app.state.gesture_ticker.on_direction is not original

    facade.close()
    facade.close()

    assert app.state.gesture_ticker.on_direction is original


def test_a_hook_replaced_after_the_tee_is_left_alone_on_close(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    facade = _gesture(app)
    replacement = lambda direction: None  # noqa: E731
    app.state.gesture_ticker.on_direction = replacement

    facade.close()

    assert app.state.gesture_ticker.on_direction is replacement


def test_the_drivers_counters_become_events_without_missing_one(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    app.state.driver.counters["gesture_end"] = 5   # before the node: not an event
    facade = _gesture(app)
    events = facade.node.publishers["/emori/gesture/events"]

    facade._tick_events()
    assert events.messages == []

    app.state.driver.counters["gesture_end"] += 2
    app.state.driver.counters["gesture_begin"] += 1
    app.state.driver.counters["lang_toggle"] += 1
    facade._tick_events()
    facade._tick_events()

    assert [m.kind for m in events.messages] == [GestureEvent.KIND_END_NUDGE] * 2 + [
        GestureEvent.KIND_BEGIN, GestureEvent.KIND_LANG_TOGGLE]
    assert {m.direction for m in events.messages} == {""}


def test_the_state_is_published_on_change_only(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    facade = _gesture(app)
    state = facade.node.publishers["/emori/gesture/state"]

    facade._tick_state()
    facade._tick_state()
    assert len(state.messages) == 1
    first = state.messages[0]
    assert first.mode in ("push_turn", "back_drive")
    assert (first.robot_moving, first.moving_reason, first.selection_reserved) == (False, "", False)

    app.state.driver.player.busy = True
    app.state.extension.reserved = True
    facade._tick_state()
    facade._tick_state()
    assert len(state.messages) == 2
    last = state.messages[1]
    assert (last.robot_moving, last.moving_reason, last.selection_reserved) == (
        True, "player.busy", True)


def test_a_robot_without_a_sensor_reports_it_is_off(tmp_path):
    app = _app(tmp_path)
    facade = _gesture(app)

    facade._tick_state()

    assert facade.node.publishers["/emori/gesture/state"].messages[0].mode == "off"
    callback = facade.node.services["/emori/gesture/reset_sensing"][0]
    response = callback(Trigger.Request(), Trigger.Response())
    assert (response.success, response.message) == (False, "no_gesture_sensor")


def test_reset_sensing_forgets_the_baselines(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    ticker = app.state.gesture_ticker
    calls: list[str] = []
    ticker.reset_sensing = lambda: calls.append("reset")
    facade = _gesture(app)
    callback = facade.node.services["/emori/gesture/reset_sensing"][0]

    response = callback(Trigger.Request(), Trigger.Response())

    assert calls == ["reset"] and response.success is True


def test_inject_is_refused_unless_it_is_switched_on(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    facade = _gesture(app)
    callback = facade.node.services["/emori/gesture/inject_turn"][0]

    response = callback(SetBool.Request(True), SetBool.Response())

    assert (response.success, response.message) == (False, "inject_disabled")
    assert facade.node.publishers["/emori/gesture/events"].messages == []
    assert app.state.driver.touched == 0


@pytest.mark.parametrize("flag", ["", "0", "no", "enabled"])
def test_only_a_spelled_out_yes_switches_inject_on(tmp_path, flag):
    facade = _gesture(_app(tmp_path, with_ticker=True), **{gn.INJECT_VAR: flag})

    assert facade.inject_turn(True) == (False, "inject_disabled")


def test_an_injected_turn_reaches_the_hook_through_the_tee(tmp_path):
    calls: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=calls.append)
    facade = _gesture(app, **{gn.INJECT_VAR: "1"})
    callback = facade.node.services["/emori/gesture/inject_turn"][0]

    left = callback(SetBool.Request(True), SetBool.Response())
    right = callback(SetBool.Request(False), SetBool.Response())

    assert (left.success, right.success) == (True, True)
    assert calls == [GestureDirection.LEFT, GestureDirection.RIGHT]
    events = facade.node.publishers["/emori/gesture/events"].messages
    assert [m.direction for m in events] == ["left", "right"]
    assert app.state.driver.touched == 2


def test_an_injected_turn_is_refused_in_a_session_where_a_turn_means_end(tmp_path):
    calls: list[Any] = []
    app = _app(tmp_path, with_ticker=True, on_direction=calls.append)
    app.state.driver.tracker.active = True
    facade = _gesture(app, **{gn.INJECT_VAR: "1"})

    assert facade.inject_turn(True) == (False, "session_active")
    assert calls == []


def test_an_inject_that_raises_is_a_refusal_and_not_a_crash(tmp_path):
    def hook(_direction: Any) -> None:
        raise RuntimeError("boom")

    app = _app(tmp_path, with_ticker=True, on_direction=hook)
    facade = _gesture(app, **{gn.INJECT_VAR: "1"})
    callback = facade.node.services["/emori/gesture/inject_turn"][0]

    response = callback(SetBool.Request(True), SetBool.Response())

    assert (response.success, response.message) == (False, "inject_failed")


# --- the builders and the hub ----------------------------------------------------------


def test_the_builders_make_nodes_the_hub_can_run_and_destroy(tmp_path):
    app = _app(tmp_path, with_ticker=True)
    original = app.state.gesture_ticker.on_direction
    executors: list[Any] = []

    class _Executor:
        def __init__(self, num_threads: int | None = None) -> None:
            self.nodes: list[Any] = []
            self.is_shut = False
            executors.append(self)

        def add_node(self, node: Any) -> None:
            self.nodes.append(node)

        def spin_once(self, timeout_sec: float = 0.0) -> None:
            time.sleep(0.002)

        def shutdown(self) -> None:
            self.is_shut = True

    ros = hub.HubBindings(init=lambda _a: None, try_shutdown=lambda: None, ok=lambda: True,
                          fatal=(), multi_threaded_executor=_Executor, link=None)
    speech_bindings, gesture_bindings = _speech_bindings(), _gesture_bindings()
    group = hub.NodeGroup("ros-facade", ros, [
        lambda a: sn.build_speech_node(a, bindings=speech_bindings),
        lambda a: gn.build_gesture_node(a, bindings=gesture_bindings),
    ])

    group.start(app)
    try:
        nodes = executors[0].nodes
        assert [n.name for n in nodes] == ["speech", "gesture"]
        assert app.state.gesture_ticker.on_direction is not original
        assert _wait_until(lambda: nodes[0].publishers["/emori/language"].messages)
        assert nodes[0].emori_facade.stats().get("say_requests", 0) == 0
    finally:
        group.stop()

    assert all(node.destroyed == 1 for node in nodes)
    assert app.state.gesture_ticker.on_direction is original
    assert _wait_until(lambda: not [t for t in threading.enumerate()
                                    if t.name.startswith("ros-speech") and t.is_alive()])


def test_a_poller_survives_a_read_that_raises(tmp_path):
    counters = facade_kit.Counters()
    reads: list[int] = []

    def read() -> None:
        reads.append(1)
        if len(reads) == 1:
            raise RuntimeError("the first read fails")

    poller = facade_kit.Poller("ros-test-poll", 0.005, read, counters)
    poller.start()
    try:
        assert _wait_until(lambda: len(reads) >= 3)
        assert counters.get("ros-test-poll_errors") == 1
    finally:
        assert poller.stop() is True


def test_a_speech_node_needs_the_apps_glue(tmp_path):
    app = _app(tmp_path)
    del app.state.glue

    with pytest.raises(RuntimeError, match="glue"):
        sn.SpeechFacade(app, _speech_bindings())
