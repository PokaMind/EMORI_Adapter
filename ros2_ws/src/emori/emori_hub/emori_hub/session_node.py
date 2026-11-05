"""``/emori/session``: the session as the graph sees it, and the few things the graph may ask of it.

The hostapp's session objects are read where they are and never copied or replaced. This
node reads the driver's own attributes on a timer and publishes what it finds; it asks the
driver for nothing that the kiosk's own pages do not already ask of it.

**What it publishes.** ``/emori/session/state`` (latched) on a change and at least every
:data:`HEARTBEAT_S`, and ``/emori/session/events`` through the fan-out, which this node owns
the publisher of. Identifiers pass the same privacy gate as every other message of the
process.

**What it answers.**

* ``command`` -- ``end`` goes through the watchdog's own end request, so the session's row
  names it (:data:`END_REASON`) and the dead-iframe fail-safe covers it exactly as it covers
  a walk-away. ``begin``, ``reprompt`` and ``end`` are refused when no session is running,
  because the slot they would leave behind is drained by whatever page opens next. ``reload``
  is refused while one is, because it would take the place of a pending ``end``.
* ``start`` -- mints a session through the kiosk's own ``/api/voice-select`` on loopback, so
  every check it makes, the badge handling included, applies unchanged. The page it returns
  is left in the start slot and the landing page is told to reload; its next ``GET /`` is
  redirected there. Refused when anything is running, a badge is held, or an earlier start
  has not been collected.
* ``voice_menu`` -- the spoken menu in a language on offer.
* ``set_tt_overrides`` -- refused unless the same switch that opens the HTTP route is on.

**Nothing here starts, ends or changes a session unless a caller asked.** There is no timer
that acts, no subscription, and nothing on the path a session's sentences take: every
callback runs on the facade executor, and state is read without taking the driver's lock.
The only locks taken on a caller's behalf are the ones the driver's own end request and
command slot take, for a few assignments each.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from emori_hub.facade_kit import facade_node
from emori_hub.fanout import FanOut, build_fanout, fill_message, load_privacy

__all__ = [
    "END_REASON",
    "FINALIZED_HOLD_S",
    "HEARTBEAT_S",
    "POLL_S",
    "SessionBindings",
    "SessionFacade",
    "SessionStateView",
    "StartResult",
    "build_session_node",
    "fill_state_message",
    "load_session_bindings",
]

log = logging.getLogger("emori.hub.session")

NODE_NAME = "session"
NODE_NAMESPACE = "/emori"
STATE_TOPIC = "/emori/session/state"
EVENTS_TOPIC = "/emori/session/events"
COMMAND_SERVICE = "/emori/session/command"
START_SERVICE = "/emori/session/start"
VOICE_MENU_SERVICE = "/emori/session/voice_menu"
TT_OVERRIDES_SERVICE = "/emori/session/set_tt_overrides"

#: What a ``command end`` records as the reason on the session's row.
END_REASON = "ros_end_requested"

#: How often the driver's attributes are looked at, and how long a state may go unrepeated.
POLL_S = 0.25
HEARTBEAT_S = 2.0
#: How long the phase says "finalized" before it goes back to "no session".
FINALIZED_HOLD_S = 5.0
#: A loopback call into the kiosk's own server.
LOOPBACK_TIMEOUT_S = 10.0

TT_INJECT_VAR = "EMORI_TT_INJECT_HTTP"
PORT_VAR = "HOSTAPP_PORT"

PHASE_NO_SESSION = 0
PHASE_BOOTING = 1
PHASE_FRAME_ALIVE = 2
PHASE_EXCHANGED = 3
PHASE_END_REQUESTED = 4
PHASE_FINALIZED = 5

#: The verbs ``command`` knows, spelled as the interface spells them.
VERBS = ("begin", "end", "reprompt", "reload", "abort_unstarted")

#: Error codes of a start that the kiosk's own route may answer with, passed on as they are.
_START_CODES = frozenset({"invalid_qr", "qr_already_claimed", "training_unavailable"})
#: A reason a caller gives is for the journal only, and only when it is plain.
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_LANG_MAX = 16


# --- the state ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionStateView:
    """``emori_msgs/SessionState`` without ROS and without its header."""

    phase: int = PHASE_NO_SESSION
    seq: int = 0
    session_id: str = ""
    person_id: str = ""
    anonymous: bool = False
    topic: str = ""
    module_id: str = ""
    lang: str = ""
    cycles: int = 0
    utterances: int = 0
    exchanged: bool = False
    watchdog_state: str = ""
    idle_for_s: float = 0.0
    last_status: str = ""
    last_end_reason: str = ""
    last_avg_engagement: float = -1.0
    last_dominant_style: str = ""

    def same_as(self, other: "SessionStateView") -> bool:
        """Whether nothing a subscriber acts on differs. Ignores ``seq`` and the idle clock."""

        skipped = ("seq", "idle_for_s")
        return all(getattr(self, f.name) == getattr(other, f.name)
                   for f in fields(self) if f.name not in skipped)


def fill_state_message(message: Any, view: SessionStateView, stamp: Any = None) -> Any:
    """Copy ``view`` onto a ``SessionState`` message and return it.

    ``stamp`` is a ``builtin_interfaces/Time`` taken from the node's clock, if given.
    """

    if stamp is not None:
        message.header.stamp = stamp
    message.phase = int(view.phase)
    message.seq = int(view.seq) & 0xFFFFFFFF
    message.session_id = view.session_id
    message.person_id = view.person_id
    message.anonymous = bool(view.anonymous)
    message.topic = view.topic
    message.module_id = view.module_id
    message.lang = view.lang
    message.cycles = min(int(view.cycles), 0xFFFFFFFF)
    message.utterances = min(int(view.utterances), 0xFFFFFFFF)
    message.exchanged = bool(view.exchanged)
    message.watchdog_state = view.watchdog_state
    message.idle_for_s = float(view.idle_for_s)
    message.last_status = view.last_status
    message.last_end_reason = view.last_end_reason
    message.last_avg_engagement = float(view.last_avg_engagement)
    message.last_dominant_style = view.last_dominant_style
    return message


@dataclass(frozen=True)
class StartResult:
    accepted: bool
    session_id: str = ""
    topic: str = ""
    error_code: str = ""


def _text(value: Any, limit: int = 200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _loopback_post(path: str, payload: Mapping[str, Any], *, port: int,
                   timeout: float = LOOPBACK_TIMEOUT_S) -> tuple[int, dict]:
    """POST JSON to this process's own server. ``(0, {})`` when it cannot be reached.

    The answer carries a signed login for the session it minted, so nothing here logs it.
    A proxy from the environment has no business between a process and its own port.
    """

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (OSError, ValueError):
        return 0, {}
    try:
        body = json.loads(raw)
    except ValueError:
        return status, {}
    return status, body if isinstance(body, dict) else {}


class SessionFacade:
    """What the node does, with no ROS in it: read the session, and answer the four requests.

    ``app`` is the hostapp's FastAPI app. ``post_json`` is the loopback call and ``clock`` the
    monotonic clock, both injectable. Methods are safe to call from any thread of the facade
    executor, and none of them is called from the session's own paths.
    """

    def __init__(
        self,
        app: Any,
        *,
        privacy: Any,
        environ: Optional[Mapping[str, str]] = None,
        post_json: Optional[Callable[[str, Mapping[str, Any]], tuple[int, dict]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._privacy = privacy
        self._environ = os.environ if environ is None else environ
        self._post_json = post_json if post_json is not None else self._default_post
        self._clock = clock
        self._start_lock = threading.Lock()
        # An accepted start the kiosk has not collected: its target -> (session id, badge).
        # The lock covers this and the slot together, for in-memory work only.
        self._minted: dict[str, tuple[str, str]] = {}
        self._minted_lock = threading.Lock()
        self._seen_final: Optional[str] = None
        self._finalized_at: Optional[float] = None
        self._last = {"status": "", "end_reason": "", "avg": -1.0, "style": ""}
        self._seq = 0
        self._published: Optional[SessionStateView] = None
        self._published_at = float("-inf")

    # --- reading ------------------------------------------------------------------------

    @property
    def _state(self) -> Any:
        return self._app.state

    def read(self) -> SessionStateView:
        """The session now, from the driver's own attributes, taking none of its locks."""

        driver = self._state.driver
        tracker = driver.tracker
        info = tracker.info
        self._note_finalize(driver)
        if info is None:
            held = (self._finalized_at is not None
                    and self._clock() - self._finalized_at < FINALIZED_HOLD_S)
            phase = PHASE_FINALIZED if held else PHASE_NO_SESSION
            return self._with_last(SessionStateView(phase=phase))
        exchanged = bool(tracker.exchanged)
        if driver.end_requested_at is not None:
            phase = PHASE_END_REQUESTED
        elif exchanged:
            phase = PHASE_EXCHANGED
        elif driver.frame_alive_at is not None:
            phase = PHASE_FRAME_ALIVE
        else:
            phase = PHASE_BOOTING
        person, session = self._privacy.apply_ids(_text(info.person_id), _text(info.session_id))
        watchdog = getattr(getattr(driver, "watchdog", None), "state", None)
        return self._with_last(SessionStateView(
            phase=phase, session_id=session, person_id=person,
            anonymous=bool(info.anonymous), topic=_text(info.topic),
            module_id=_text(info.module_id), lang=_text(info.lang, _LANG_MAX),
            cycles=int(tracker.cycles), utterances=int(tracker.utterances),
            exchanged=exchanged, watchdog_state=str(getattr(watchdog, "name", "") or ""),
            idle_for_s=round(max(0.0, time.monotonic() - driver.last_activity), 1)))

    def _with_last(self, view: SessionStateView) -> SessionStateView:
        return replace(view, last_status=self._last["status"],
                       last_end_reason=self._last["end_reason"],
                       last_avg_engagement=self._last["avg"],
                       last_dominant_style=self._last["style"])

    def _note_finalize(self, driver: Any) -> None:
        """Learn the outcome of a session that has just closed, once per session."""

        last = getattr(driver, "_last_finalized", None)
        if not last or last[0] == self._seen_final:
            return
        session_id, stats = last[0], last[3] if len(last) > 3 else {}
        self._seen_final = session_id
        self._finalized_at = self._clock()
        average = stats.get("avg_engagement") if isinstance(stats, dict) else None
        style = stats.get("dominant_style") if isinstance(stats, dict) else ""
        status, reason = self._row_outcome(session_id)
        self._last = {
            "status": status, "end_reason": reason,
            "avg": float(average) if isinstance(average, (int, float)) else -1.0,
            "style": _text(style, 64),
        }

    def _row_outcome(self, session_id: str) -> tuple[str, str]:
        """How the row says it ended: the one place that is recorded once the driver is done."""

        metrics = getattr(self._state, "metrics", None)
        query = getattr(metrics, "query", None)
        if not callable(query):
            return "", ""
        try:
            from emori_adapter.hostapp.metrics_store import deidentify_session_id

            rows = query("SELECT status, end_reason FROM sessions WHERE session_id = ?",
                         (deidentify_session_id(session_id),))
        except Exception:  # noqa: BLE001 - an unreadable store leaves the fields empty
            log.debug("the finished session's row could not be read", exc_info=True)
            return "", ""
        if not rows:
            return "", ""
        return _text(rows[0].get("status"), 32), _text(rows[0].get("end_reason"), 64)

    def next_state(self, now: Optional[float] = None) -> Optional[SessionStateView]:
        """The state to publish, or None when the last one is still current and fresh."""

        view = self.read()
        previous = self._published
        stamp = self._clock() if now is None else now
        if previous is not None and view.same_as(previous) and (
                stamp - self._published_at < HEARTBEAT_S):
            return None
        if previous is None or not view.same_as(previous):
            self._seq += 1
        out = replace(view, seq=self._seq)
        self._published, self._published_at = out, stamp
        return out

    # --- command ------------------------------------------------------------------------

    def command(self, verb: str, reason: str = "") -> tuple[bool, str]:
        """``(accepted, error_code)``."""

        verb = verb.strip() if isinstance(verb, str) else ""
        if verb not in VERBS:
            return False, "unknown_verb"
        why = reason if isinstance(reason, str) and _REASON.match(reason) else ""
        driver = self._state.driver
        active = driver.tracker.active
        if verb == "abort_unstarted":
            code = why.lower()
            aborted = driver.abort_unstarted_session(
                code if _SAFE_CODE.match(code) else "ros_abort_unstarted")
            return (True, "") if aborted else (False, "no_unstarted_session")
        if verb == "reload":
            if active or not self._landing_idle():
                return False, "busy"
            driver.enqueue_cmd("reload")
            return True, ""
        if not active:
            return False, "no_session"
        if verb == "end":
            return self._end(driver, why)
        if verb == "reprompt":
            return (True, "") if driver.enqueue_cmd_if_empty("reprompt") else (
                False, "command_pending")
        # One slot, latest wins: a begin must not take the place of an end nobody has read yet.
        if driver.end_requested_at is not None:
            return False, "end_pending"
        return (True, "") if driver.enqueue_cmd_if_empty("begin") else (False, "command_pending")

    def _landing_idle(self) -> bool:
        try:
            return bool(self._state.glue.landing_idle())
        except Exception:  # noqa: BLE001 - an unreadable state is somebody there
            return False

    def _end(self, driver: Any, why: str) -> tuple[bool, str]:
        """End it the way the watchdog ends one, so the row says who asked."""

        ticker = getattr(self._state, "watchdog_ticker", None)
        request = getattr(ticker, "_request_pokamind_end", None)
        if not callable(request):
            return False, "unavailable"
        if driver.end_requested_at is not None:
            return False, "end_pending"
        log.warning("session end requested over ROS%s", f" ({why})" if why else "")
        request(END_REASON)
        return True, ""

    # --- start --------------------------------------------------------------------------

    def start(self, menu_text: str, badge_payload: str = "") -> StartResult:
        text = menu_text.strip() if isinstance(menu_text, str) else ""
        if not text:
            return StartResult(False, error_code="no_match")
        if not self._start_lock.acquire(blocking=False):
            return StartResult(False, error_code="busy")
        try:
            return self._start(text, badge_payload if isinstance(badge_payload, str) else "")
        finally:
            self._start_lock.release()

    def _start(self, text: str, badge: str) -> StartResult:
        state = self._state
        glue = state.glue
        slot = state.start_slot
        self.forget_abandoned()
        if slot.waiting() or not glue.robot_idle() or not glue.landing_idle():
            return StartResult(False, error_code="busy")
        # As the landing page does: the route falls back to a default topic on text it did
        # not recognise, and only a real choice may consume a badge's one claim.
        status, body = self._post_json("/api/voice-select",
                                       {"text": text, "qr": badge, "preview": True})
        error = _preview_error(status, body)
        if error:
            return StartResult(False, error_code=error)
        status, body = self._post_json("/api/voice-select", {"text": text, "qr": badge})
        error = _start_error(status, body)
        if error:
            return StartResult(False, error_code=error)
        url = body.get("session_url")
        if not isinstance(url, str):
            return StartResult(False, error_code="training_unavailable")
        sid = (parse_qs(urlsplit(url).query).get("sid") or [""])[0]
        # Someone may have walked up while the mint was out; their session comes first. Not
        # robot_idle again: this mint's own badge claim reserves selection.
        if state.driver.tracker.active or not glue.landing_idle():
            glue.discard_minted(sid, badge)
            return StartResult(False, error_code="busy")
        try:
            with self._minted_lock:
                self._minted[url] = (sid, badge)
                slot.put(url, leased=bool(badge))
        except ValueError:
            self._minted.pop(url, None)
            glue.discard_minted(sid, badge)
            return StartResult(False, error_code="training_unavailable")
        state.driver.enqueue_cmd("reload")
        _person, session = self._privacy.apply_ids("", _text(sid))
        log.info("session start requested over ROS")
        return StartResult(True, session_id=session, topic=_text(body.get("topic")))

    def forget_abandoned(self) -> None:
        """Undo a start the kiosk never collected. Called from the state tick and from a start."""

        slot = self._state.start_slot
        with self._minted_lock:
            target = slot.abandoned()
            minted = self._minted.pop(target, None) if target is not None else None
            if not slot.waiting():
                self._minted.clear()  # what is left here was collected
        if minted is not None:
            log.info("a session start over ROS was not collected by the kiosk")
            self._state.glue.discard_minted(*minted)

    def _default_post(self, path: str, payload: Mapping[str, Any]) -> tuple[int, dict]:
        port = int(self._environ.get(PORT_VAR, "") or 8080)
        return _loopback_post(path, payload, port=port)

    # --- voice menu and overrides -------------------------------------------------------

    def voice_menu(self, lang: str = "") -> tuple[str, list[str], list[str], list[str]]:
        """``(prompt, grammar, option_keys, option_names)``; all empty for a language not on offer."""

        state = self._state
        code = lang.strip().lower() if isinstance(lang, str) else ""
        if code and not state.lang_state.is_allowed(code):
            return "", [], [], []
        menu, tuning = state.glue.menu_for(code)
        grammar = [word.strip() for word in str(tuning.get("grammar") or "").split(",")
                   if word.strip()]
        keys = [str(key) for key in menu.options]
        names = [str(option.name) for option in menu.options.values()]
        return str(menu.prompt), grammar, keys, names

    def set_tt_overrides(self, patch_json: str) -> tuple[bool, int, str]:
        """``(ok, rev, error_code)``. Closed unless the HTTP route's own switch is on."""

        if str(self._environ.get(TT_INJECT_VAR, "0")) != "1":
            return False, 0, "tt_inject_disabled"
        try:
            patch = json.loads(patch_json)
        except (TypeError, ValueError):
            return False, 0, "invalid_json"
        from emori_adapter.hostapp.tt_inject import TtPatchError

        try:
            snapshot = self._state.glue.tt_store.set(patch)
        except TtPatchError as error:
            return False, 0, _text(getattr(error, "code", ""), 64) or "invalid_patch"
        return True, int(snapshot["rev"]), ""


def _preview_error(status: int, body: Mapping[str, Any]) -> str:
    """The error code of a preview, or ``""`` when the text chose a topic of the menu."""

    if status == 200:
        return "" if body.get("exact") is True else "no_match"
    return _start_error(status, body)


def _start_error(status: int, body: Mapping[str, Any]) -> str:
    """The error code of a voice-select answer, or ``""`` when it minted a session."""

    if status == 200 and body.get("matched") is False:
        return "no_match"
    if status == 200 and body.get("session_url"):
        return ""
    code = _text(body.get("error_code"), 64)
    if code in _START_CODES:
        return code
    if code == "badge_unrecognized":
        return "invalid_qr"
    return "training_unavailable"


# --- the node ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionBindings:
    """Everything the node takes from ROS, so a test can hand it fakes."""

    create_node: Callable[..., Any]
    group_cls: Callable[[], Any]
    latched_qos: Any
    events_qos: Any
    state_type: Any
    event_type: Any
    command_type: Any
    start_type: Any
    voice_menu_type: Any
    tt_overrides_type: Any


def load_session_bindings() -> SessionBindings:
    """Import ``rclpy`` and the interfaces. Raises ``ImportError`` where ROS is absent."""

    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

    from emori_msgs.msg import SessionEvent, SessionState
    from emori_msgs.srv import GetVoiceMenu, SessionCommand, SetTtOverrides, StartSession

    return SessionBindings(
        create_node=facade_node,
        group_cls=ReentrantCallbackGroup,
        latched_qos=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                               reliability=ReliabilityPolicy.RELIABLE,
                               durability=DurabilityPolicy.TRANSIENT_LOCAL),
        events_qos=QoSProfile(depth=256, history=HistoryPolicy.KEEP_LAST,
                              reliability=ReliabilityPolicy.RELIABLE,
                              durability=DurabilityPolicy.VOLATILE),
        state_type=SessionState,
        event_type=SessionEvent,
        command_type=SessionCommand,
        start_type=StartSession,
        voice_menu_type=GetVoiceMenu,
        tt_overrides_type=SetTtOverrides,
    )


def build_session_node(
    app: Any,
    bindings: Optional[SessionBindings] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    post_json: Optional[Callable[[str, Mapping[str, Any]], tuple[int, dict]]] = None,
) -> Any:
    """The ``/emori/session`` node, for a facade group: pass it to the hub as a builder.

    It also starts the fan-out and makes it the relay's tap. Destroying the node stops that.
    """

    ros = bindings if bindings is not None else load_session_bindings()
    source = os.environ if environ is None else environ
    privacy = load_privacy(source)
    facade = SessionFacade(app, privacy=privacy, environ=source, post_json=post_json)
    node = ros.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
    group = ros.group_cls()

    state_pub = node.create_publisher(ros.state_type, STATE_TOPIC, ros.latched_qos)
    events_pub = node.create_publisher(ros.event_type, EVENTS_TOPIC, ros.events_qos)
    fanout: FanOut = build_fanout(
        app, publish=lambda event: events_pub.publish(fill_message(ros.event_type(), event)),
        environ=source, privacy=privacy)

    def release_fanout() -> None:
        fanout.stop()
        fanout.uninstall()
        app.state.fanout = None

    def publish_state() -> None:
        view = facade.next_state()
        if view is not None:
            state_pub.publish(fill_state_message(
                ros.state_type(), view, node.get_clock().now().to_msg()))

    def tick() -> None:
        try:
            facade.forget_abandoned()
            publish_state()
        except Exception:  # noqa: BLE001 - a bad read costs one tick
            log.warning("the session state could not be published", exc_info=True)

    def command(request: Any, response: Any) -> Any:
        try:
            response.accepted, response.error_code = facade.command(request.verb, request.reason)
        except Exception:  # noqa: BLE001 - a caller gets an answer, never a hung call
            log.warning("a session command failed", exc_info=True)
            response.accepted, response.error_code = False, "internal"
        return response

    def start(request: Any, response: Any) -> Any:
        try:
            result = facade.start(request.menu_text, request.badge_payload)
        except Exception:  # noqa: BLE001
            log.warning("a session start failed", exc_info=True)
            result = StartResult(False, error_code="internal")
        response.accepted = result.accepted
        response.session_id = result.session_id
        response.topic = result.topic
        response.error_code = result.error_code
        return response

    def voice_menu(request: Any, response: Any) -> Any:
        try:
            prompt, grammar, keys, names = facade.voice_menu(request.lang)
        except Exception:  # noqa: BLE001
            log.warning("the voice menu could not be read", exc_info=True)
            prompt, grammar, keys, names = "", [], [], []
        response.prompt, response.grammar = prompt, grammar
        response.option_keys, response.option_names = keys, names
        return response

    def set_tt_overrides(request: Any, response: Any) -> Any:
        try:
            response.ok, response.rev, response.error_code = facade.set_tt_overrides(
                request.patch_json)
        except Exception:  # noqa: BLE001
            log.warning("a tt override could not be set", exc_info=True)
            response.ok, response.rev, response.error_code = False, 0, "internal"
        return response

    try:
        node.create_service(ros.command_type, COMMAND_SERVICE, command, callback_group=group)
        node.create_service(ros.start_type, START_SERVICE, start, callback_group=group)
        node.create_service(ros.voice_menu_type, VOICE_MENU_SERVICE, voice_menu,
                            callback_group=group)
        node.create_service(ros.tt_overrides_type, TT_OVERRIDES_SERVICE, set_tt_overrides,
                            callback_group=group)
        tick()
        node.create_timer(POLL_S, tick, callback_group=group)
    except BaseException:
        # A hub runs on without this node, and the fan-out must not run on without it.
        try:
            release_fanout()
        finally:
            node.destroy_node()
        raise

    destroy = node.destroy_node

    def destroy_node() -> Any:
        try:
            release_fanout()
        except Exception:  # noqa: BLE001 - the node must still be released
            log.warning("the fan-out did not stop cleanly", exc_info=True)
        return destroy()

    node.destroy_node = destroy_node
    node.session_facade = facade
    node.fanout = fanout
    return node
