"""The scripted scenarios: what the page does, step by step, on any stack.

A scenario speaks only through :class:`~golden.record.Context`, so the same code runs against
a simulated host and a real one. ``env`` applies only in the simulator, where it shortens the
timers a person would otherwise wait out. On a robot the same steps run against whatever that
robot is configured with, which takes longer and asserts the same order.

Extra scenarios, tolerances and recorders come from ``addons/*/golden/`` when that exists: a
``scenarios.py`` with ``SCENARIOS`` and optionally ``RECORDERS``, and a ``tolerances.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .record import Context, ScenarioFailed

# How long a step waits in the simulator, where the timers are short, and on a robot.
SIM_WAIT_S = 25.0
ROBOT_WAIT_S = 300.0

TURN_ARMED = "pokamind_turn_armed"
TURN_SUBMITTED = "pokamind_turn_submitted"
COMPLETED = "pokamind_session_completed"

# Timers tick once a second from when the process started, so whatever one of them decides
# lands anywhere in that second. Its order stays exact, and its time says nothing.
TIMER_DRIVEN = frozenset({"pending", "flags", "mic"})

# Longer than the greeting a new session opens with, so a first sentence does not start under it.
GREETING_S = 7.5
# A session opened over a live one greets after the first one's farewell has played. How long
# that takes differs by a second between machines, so this leaves room for the slowest.
FAREWELL_S = 4.0

_RESULTS = {
    "styles": {"mirroring": 14.2, "acknowledging": 11.8, "steady": 9.3},
    "body_language_metrics": {"engagement": 64.0, "confidence": 58.5},
    "duration_minutes": 8,
    "performance_scores": {"overall": 71, "listening": 68},
    "analysis": "Kept a steady pace and acknowledged what was said.",
}
_UNREGISTERED_BADGE = "EMORI:nobody-000"
_FIXED_SENTENCE = {"en": "The training room is on the second floor.",
                   "ca": "La sala de formació és al segon pis."}
_FIXED_PCM = bytes(range(256)) * 32


@dataclass(frozen=True)
class Scenario:
    name: str
    run: Callable[[Context], None]
    description: str = ""
    modes: tuple[str, ...] = ("sim", "robot")
    # Set in the simulated host's process before it imports anything.
    env: dict[str, str] = field(default_factory=dict)
    kind: str = "scripted"
    voiced: bool = False
    # Streams whose time this scenario's timers make meaningless. Their order still counts.
    untimed: frozenset[str] = frozenset()
    # Client methods every run must show, or the recording itself is suspect.
    self_check: tuple[str, ...] = ()
    extension: Optional[Callable[[], Any]] = None
    offline: bool = False
    run_offline: Callable[[Path], None] = lambda run_dir: None


def _wait(context: Context) -> float:
    return SIM_WAIT_S if context.mode == "sim" else ROBOT_WAIT_S


def _calm_cycle(seq: int) -> dict:
    import motion_sync_run as sync_run

    return sync_run.analysis_envelope(seq)


def _completion(*, results: bool = True) -> dict:
    envelope: dict[str, Any] = {"type": COMPLETED, "status": "completed"}
    if results:
        envelope["results"] = dict(_RESULTS)
    return envelope


def _target(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _open_anonymous(context: Context, *, page: str = "session") -> str:
    """Mint and open a walk-up session, poll as its page does, and return the ``sid``."""

    minted = context.post("/api/start", {"student_id": "golden"}).json()
    url = minted["session_url"]
    context.page(page, _target(url))
    context.start_poll()
    return minted.get("session_id") or minted.get("sid") or url


def _exchange(context: Context, cycles: int = 2, *, greeting_s: float = GREETING_S) -> None:
    """The frame comes alive and the conversation starts, once the greeting has finished.

    What a cycle moves depends on whether the greeting is still running and how far the
    session's own writer has got, so a cycle during it would make the plan a matter of timing.
    """

    context.post("/api/frame-alive")
    context.hold(greeting_s)
    for seq in range(cycles):
        context.cycle(_calm_cycle(seq))


def _landing(context: Context, name: str = "landing_after") -> None:
    context.stop_poll()
    context.page(name, "/")


# ---------------------------------------------------------------------------------------
# the scenarios
# ---------------------------------------------------------------------------------------


def full_session(context: Context) -> None:
    """Badge, menu, session, a full turn loop, completion with results, summary, landing."""

    badge = context.badge
    context.page("landing", "/")
    context.page("identify", "/identify")
    context.page("manual", "/manual")
    context.post("/api/badge/claim", {"payload": badge})
    context.post("/api/identify", {"payload": badge, "lookup_only": True})
    context.get("/api/voice-menu")
    context.post("/api/voice-select", {"text": "one", "preview": True, "qr": badge})
    selected = context.post("/api/voice-select", {"text": "one", "qr": badge}).json()
    context.page("session", _target(selected["session_url"]))
    context.start_poll()
    _exchange(context)
    context.speak(("normal_a", "family_tension_a"))
    context.cycle({"type": TURN_ARMED}, answers=True)
    context.hold(1.0)
    context.cycle({"type": TURN_SUBMITTED}, answers=True)
    context.hold(1.0)
    context.speak(("normal_b", "family_levity_a"))
    answer = context.cycle(_completion()).json()
    context.get(_target(answer["summary_tts_url"]))
    _landing(context)


def walkaway(context: Context) -> None:
    """Nothing arrives for long enough: the robot asks to end, the grace runs out, ``home``."""

    _open_anonymous(context)
    _exchange(context)
    context.wait_verb("end", _wait(context))
    context.wait_verb("home", _wait(context))
    _landing(context)


def start_deadlines(context: Context) -> None:
    """A session that never starts, by each deadline, then a login the frame could not make."""

    _open_anonymous(context, page="session_page_deadline")
    context.post("/api/session-start-failed", {"reason": "session_never_started"})
    context.stop_poll()
    _open_anonymous(context, page="session_robot_deadline")
    context.wait_verb("end", _wait(context))
    context.wait_verb("home", _wait(context))
    context.stop_poll()
    _open_anonymous(context, page="session_auth_failed")
    context.post("/api/session-start-failed", {"reason": "auth_failed_no_token"})
    _landing(context)


def preempt(context: Context) -> None:
    """A new session over a live one finalizes the first and keeps the second."""

    _open_anonymous(context, page="session_first")
    _exchange(context)
    context.stop_poll()
    _open_anonymous(context, page="session_second")
    _exchange(context, greeting_s=GREETING_S + FAREWELL_S)
    context.cycle(_completion())
    _landing(context)


def late_completion(context: Context) -> None:
    """A completion after the robot gave up is salvaged once; the next one is ignored."""

    _open_anonymous(context)
    _exchange(context)
    context.wait_verb("end", _wait(context))
    context.wait_verb("home", _wait(context))
    context.stop_poll()
    context.cycle(_completion())
    context.cycle(_completion())
    _landing(context)


def _open_turn_nobody_answers(context: Context) -> None:
    _open_anonymous(context)
    _exchange(context)
    context.cycle({"type": TURN_ARMED}, answers=True)


def _force_end(context: Context) -> None:
    context.wait_verb("end", _wait(context))
    context.cycle(_completion())
    _landing(context)


def reprompt_motion(context: Context) -> None:
    """A turn nobody answers: the robot gestures, then asks to end the session."""

    _open_turn_nobody_answers(context)
    _force_end(context)


def reprompt_spoken(context: Context) -> None:
    """The same with the frame speaking the re-prompt, the gesture waiting until it does."""

    _open_turn_nobody_answers(context)
    context.wait_verb("reprompt", _wait(context))
    context.post("/api/emori-debug", {"type": "pokamind_emori_dbg", "stage": "reprompt-start"})
    _force_end(context)


def idle_and_listening(context: Context) -> None:
    """The idle ticker for a minute, then stillness while the mic is open."""

    context.hold(60.0, fast=12.0)
    _open_anonymous(context)
    _exchange(context)
    context.cycle({"type": TURN_ARMED})
    context.mark("listening_start")
    context.hold(15.0, fast=11.0)
    context.mark("listening_end")
    context.cycle(_completion())
    _landing(context)


def _refusing_extension() -> Any:
    """A generic add-on with one opinion: it refuses the second badge."""

    from emori_adapter.hostapp.extensions import BADGE_OK, BADGE_REFUSED, HostExtension

    class Refusing(HostExtension):
        def claim_badge(self, payload: str) -> str:
            return BADGE_REFUSED if payload.endswith("marc-002") else BADGE_OK

    return Refusing()


def badges(context: Context) -> None:
    """Unregistered, already claimed, refused by an extension, and a used login replayed."""

    badge = context.badge
    context.post("/api/badge/claim", {"payload": _UNREGISTERED_BADGE})
    context.post("/api/identify", {"payload": _UNREGISTERED_BADGE})
    context.post("/api/badge/claim", {"payload": badge})
    selected = context.post("/api/identify", {"payload": badge}).json()
    target = _target(selected["session_url"])
    context.page("session", target)
    context.start_poll()
    context.post("/api/identify", {"payload": badge})
    context.post("/api/badge/claim", {"payload": badge})
    context.post("/api/session-start-failed", {"reason": "session_never_started"})
    context.stop_poll()
    context.get(target)
    if context.mode == "sim":
        context.post("/api/badge/claim", {"payload": context.second_badge})
    _landing(context)


def _kiosk_language(context: Context) -> str:
    """What the kiosk speaks now: the switch persists it, and an unswitched one speaks the default."""

    try:
        state = Path(context.host.artifacts) / "kiosk_lang.json"
        return str(json.loads(state.read_text(encoding="utf-8"))["lang"]) or context.lang
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return context.lang


def language_change(context: Context) -> None:
    """A switch from the admin page: the landing page reloads and the robot says so."""

    headers = context.host.admin_headers
    started_in = _kiosk_language(context)
    context.start_poll("reload")
    context.post("/api/admin/lang", {"lang": "en" if started_in == "ca" else "ca"}, headers=headers)
    context.wait_verb("reload", _wait(context))
    context.stop_poll()
    context.page("landing_other_language", "/")
    context.get("/api/voice-menu")
    _open_anonymous(context, page="session_other_language")
    _exchange(context)
    context.cycle({"type": "pokamind_ai_utterance", "text": "Bon dia.", "seq": 1,
                   "source": "turn"})
    context.cycle(_completion())
    context.stop_poll()
    context.start_poll("reload")
    context.post("/api/admin/lang", {"lang": started_in}, headers=headers)
    context.wait_verb("reload", _wait(context))
    _landing(context, "landing_restored")


def _double_turn(*, start: float) -> list[tuple[float, float]]:
    """Yaw over time for a push one way and a push the other, inside the toggle's window."""

    return [(start, 0.45), (start + 0.5, 0.0), (start + 2.0, -0.45), (start + 2.5, 0.0)]


def language_double_turn(context: Context) -> None:
    """The hidden gesture, with the toggle on for this scenario only."""

    context.start_poll("reload")
    context.hold(3.0)
    context.host.robot.script_head(_double_turn(start=0.2))
    context.wait_verb("reload", _wait(context))
    _landing(context)


def speech_engines(context: Context) -> None:
    """Fixed input through both engines, then the interlock that keeps them off a session."""

    sentence = _FIXED_SENTENCE[context.lang]
    context.get("/api/tts", params={"text": sentence, "lang": context.lang})
    context.request("POST", "/api/stt", content=_FIXED_PCM, params={"lang": context.lang})
    context.request("POST", "/api/stt", content=_FIXED_PCM,
                    params={"lang": context.lang, "grammar": "one,two,three"})
    _open_anonymous(context)
    _exchange(context)
    context.get("/api/tts", params={"text": sentence, "lang": context.lang})
    context.cycle(_completion())
    context.get("/api/tts", params={"text": sentence, "lang": context.lang})
    _landing(context)


def host_restart(context: Context) -> None:
    """A restart under a live session: the orphan is closed and the kiosk starts over."""

    _open_anonymous(context)
    _exchange(context)
    context.stop_poll()
    context.host.restart()
    context.get("/healthz")
    context.page("landing_after_restart", "/")
    context.hold(1.0)


# Outlasting its plan, then a pause with the reply still going, so the head settles and picks
# up again. A recorded move cut by the voice stopping, one outlasted by it, and a short line.
SPEECH_CASES = ("long_a", "comma_pause_a", "normal_b", "family_tension_a", "family_long_a",
                "family_levity_a", "normal_a", "one_word_a")


def speech_plans(context: Context) -> None:
    """Plans on a voice: outlasting it (sustain), a pause (linger), and cuts when it stops."""

    _open_anonymous(context)
    _exchange(context)
    context.speak(SPEECH_CASES, continuing=("comma_pause_a",))
    context.cycle(_completion())
    _landing(context)


CUE_FAMILY_CASES = ("family_tension_a", "family_softening_a", "family_release_a",
                    "family_intimacy_a", "family_levity_a", "family_energy_a")


def cue_families(context: Context) -> None:
    """One sentence in each of the six cue families, on a voice: what each is performed as."""

    _open_anonymous(context)
    _exchange(context)
    context.speak(CUE_FAMILY_CASES)
    context.cycle(_completion())
    _landing(context)


# ---------------------------------------------------------------------------------------
# no host: decisions from recorded readings
# ---------------------------------------------------------------------------------------


def gesture_samples(tick_s: float = 0.1) -> list[dict]:
    """A daemon's state answers over a head pushed left, released, then pushed right."""

    def state(yaw: float) -> dict:
        return {"head_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": yaw},
                "body_yaw": 0.0, "antennas_position": [0.0, 0.0]}

    trace = [0.0] * 30
    trace += [0.45 * math.sin(math.pi * i / 10.0) for i in range(11)]
    trace += [0.0] * 30
    trace += [-0.45 * math.sin(math.pi * i / 10.0) for i in range(11)]
    trace += [0.0] * 30
    return [state(yaw) for yaw in trace]


def _http_client_answering(states: list[dict]) -> Any:
    """The shipping client over a transport that answers with the recorded states."""

    import httpx

    from emori_adapter.robot.client import HttpRobotClient

    answers = iter(states)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(answers))

    client = HttpRobotClient("http://recorded.invalid")
    client._http = httpx.Client(base_url="http://recorded.invalid",
                                transport=httpx.MockTransport(respond))
    return client


# Each client a stack can read the head through. A new one joins this table.
OFFLINE_CLIENTS: dict[str, Callable[[list[dict]], Any]] = {"http": _http_client_answering}


def gesture_replay(run_dir: Path) -> None:
    """The same readings through each client: their decisions must be one list of decisions."""

    from emori_adapter.hostapp.gestures import DirectionalGestureDetector, _head_angles

    from .runs import write_jsonl

    states = gesture_samples()
    decisions: dict[str, list[dict]] = {}
    for name, factory in OFFLINE_CLIENTS.items():
        client = factory(states)
        detector = DirectionalGestureDetector()
        fired: list[dict] = []
        for tick in range(len(states)):
            result = detector.update(_head_angles(client.read_state()))
            if result is not None:
                fired.append({"tick": tick, "direction": str(getattr(result, "value", result))})
        client.close()
        decisions[name] = fired
    reference = next(iter(decisions.values()))
    if not reference:
        raise ScenarioFailed("the recorded pushes produced no decision")
    for name, fired in decisions.items():
        if fired != reference:
            raise ScenarioFailed(f"client {name} decided differently: {fired} != {reference}")
    write_jsonl(run_dir / "offline.jsonl", reference)


# These have to outlast the greeting, or the session ends at a point in it decided by a timer.
_SHORT_SESSION = {"EMORI_WALKAWAY_S": "10", "EMORI_END_GRACE_S": "1",
                  "EMORI_END_GRACE_TALKED_S": "1", "EMORI_SESSION_START_S": "8"}
_SHORT_TURN = {"EMORI_TURN_REPROMPT_S": "1", "EMORI_TURN_FIRST_REPROMPT_S": "1",
               "EMORI_TURN_CEILING_S": "3", "EMORI_TURN_FIRST_CEILING_S": "3",
               "EMORI_TURN_ABS_CEILING_S": "4", "EMORI_TURN_PRESENT_CEILING_S": "4",
               "EMORI_TURN_REPROMPT_EVERY_S": "1", "EMORI_TURN_SELF_NOISE_S": "0",
               "EMORI_TURN_MAX_REPROMPTS": "1", "EMORI_WALKAWAY_S": "30",
               "EMORI_END_GRACE_S": "30", "EMORI_END_GRACE_TALKED_S": "30"}
_QUIET_IDLE = {"EMORI_IDLE_AFTER_S": "0"}

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("full_session", full_session, full_session.__doc__ or "", voiced=True,
             self_check=("play_recorded_move", "set_gaze"), env=dict(_QUIET_IDLE)),
    Scenario("walkaway", walkaway, walkaway.__doc__ or "", untimed=TIMER_DRIVEN,
             env={**_SHORT_SESSION, **_QUIET_IDLE}),
    Scenario("start_deadlines", start_deadlines, start_deadlines.__doc__ or "",
             untimed=TIMER_DRIVEN, env={**_SHORT_SESSION, **_QUIET_IDLE}),
    Scenario("preempt", preempt, preempt.__doc__ or "", env=dict(_QUIET_IDLE)),
    Scenario("late_completion", late_completion, late_completion.__doc__ or "",
             untimed=TIMER_DRIVEN, env={**_SHORT_SESSION, **_QUIET_IDLE}),
    Scenario("reprompt_motion", reprompt_motion, reprompt_motion.__doc__ or "",
             untimed=TIMER_DRIVEN,
             env={**_SHORT_TURN, **_QUIET_IDLE, "EMORI_REPROMPT_SPOKEN": "0"}),
    Scenario("reprompt_spoken", reprompt_spoken, reprompt_spoken.__doc__ or "",
             untimed=TIMER_DRIVEN,
             env={**_SHORT_TURN, **_QUIET_IDLE, "EMORI_REPROMPT_SPOKEN": "1"}),
    Scenario("idle_and_listening", idle_and_listening, idle_and_listening.__doc__ or "",
             untimed=TIMER_DRIVEN, env={"EMORI_IDLE_AFTER_S": "2"}),
    Scenario("badges", badges, badges.__doc__ or "", extension=_refusing_extension,
             env=dict(_QUIET_IDLE)),
    Scenario("language_change", language_change, language_change.__doc__ or "",
             env={**_QUIET_IDLE, "EMORI_LANG_TOGGLE": "0"}),
    Scenario("language_double_turn", language_double_turn, language_double_turn.__doc__ or "",
             modes=("sim",), untimed=TIMER_DRIVEN,
             env={**_QUIET_IDLE, "EMORI_LANG_TOGGLE": "1", "EMORI_LANG_TOGGLE_WINDOW_S": "5",
                  "EMORI_GESTURE": "1", "EMORI_GESTURE_MODE": "push_turn"}),
    Scenario("speech_engines", speech_engines, speech_engines.__doc__ or "",
             env=dict(_QUIET_IDLE)),
    Scenario("gesture_replay", lambda context: None, "Recorded head readings through each client.",
             offline=True, run_offline=gesture_replay),
    Scenario("host_restart", host_restart, host_restart.__doc__ or "", untimed=TIMER_DRIVEN,
             env=dict(_QUIET_IDLE)),
    Scenario("speech_plans", speech_plans, speech_plans.__doc__ or "", voiced=True,
             env=dict(_QUIET_IDLE)),
    Scenario("cue_families", cue_families, cue_families.__doc__ or "", voiced=True,
             env=dict(_QUIET_IDLE)),
)


def _load_module(path: Path) -> Any:
    name = f"golden_addon_{path.parent.parent.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def addon_directories(root: Path) -> list[Path]:
    addons = root / "addons"
    return sorted(path for path in addons.glob("*/golden") if path.is_dir()) if addons.is_dir() else []


def addon_scenarios(root: Path) -> tuple[Scenario, ...]:
    """Scenarios from each ``addons/<name>/golden/scenarios.py``."""

    found: list[Scenario] = []
    for directory in addon_directories(root):
        path = directory / "scenarios.py"
        if path.exists():
            found.extend(getattr(_load_module(path), "SCENARIOS", ()))
    return tuple(found)


def addon_recorders(root: Path) -> tuple[Callable[[Path, Any], None], ...]:
    """``recorder(run_dir, host)`` callables, run after each recording."""

    found: list[Callable[[Path, Any], None]] = []
    for directory in addon_directories(root):
        path = directory / "scenarios.py"
        if path.exists():
            found.extend(getattr(_load_module(path), "RECORDERS", ()))
    return tuple(found)


def addon_tolerances(root: Path) -> list[Path]:
    return [directory / "tolerances.yaml" for directory in addon_directories(root)
            if (directory / "tolerances.yaml").exists()]


def all_scenarios(root: Path) -> dict[str, Scenario]:
    return {scenario.name: scenario for scenario in SCENARIOS + addon_scenarios(root)}
