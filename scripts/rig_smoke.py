"""Post-deploy smoke test, run ON the robot: the robot experiences, end to end.

    bash /opt/emori/deploy/robot/rig-smoke.sh                       # all checks
    bash /opt/emori/deploy/robot/rig-smoke.sh --only roleplay --json  # one, machine-readable
    bash /opt/emori/deploy/robot/rig-smoke.sh --lang en             # another kiosk language

It runs with the robot's own interpreter from ``/opt/emori`` (the wrapper changes into
it), so ``load_settings()`` reads ``/opt/emori/.env`` and the robot's per-robot
Pokamind token (``secrets/pokamind_robot_token``) exactly as the host app does.  Every
call to Pokamind is signed with THAT credential.  An installed add-on may ship its own
checks as ``addons/<name>/scripts/addon_smoke.py``; the wrapper runs each after these.

The checks, in the order they run:

``control.whoami``
    ``GET /api/robot/whoami`` with this robot's credential.  Proves Pokamind accepts the
    token's control-plane assertion and names this robot, its course and its modules.
``roleplay.start`` / ``roleplay.end``
    Through the host app's own HTTP API on localhost: picks the FIRST option of the live
    voice menu as the anonymous learner (``/api/voice-select``), sends the kiosk browser
    to the session page it returns (over the kiosk Chromium's DevTools port, exactly the
    navigation the landing page performs itself), and waits for ``/api/status`` to show
    the session open and Pokamind's first turn ARMED.  It then ends the session with the
    session page's own end control (its documented ``E`` key, which relays the same
    ``emori_cmd:end`` the head-nudge end does) and confirms the kiosk is back at ``/``
    with no session active.  **The robot speaks and moves during this check** (greeting,
    the scenario intro, the farewell and the spoken summary); the session counts as one
    anonymous roleplay on the dashboard and, when Pokamind returns results, may email the
    report to ``EMORI_RESULTS_TO``.  It refuses to run while a session is active.

Output: one ``PASS``/``FAIL``/``SKIP`` line per check with a short content-free detail
(status codes, timings, counts, language codes).  Never a token, a credential, an SSO URL,
a transcript or an answer.  Exit 1 when any check FAILs; ``--strict`` also exits 2 when
one SKIPped.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from emori_adapter import robot_token as _robot_token
from emori_adapter.config import load_settings
from emori_adapter.hostapp.lang_state import LangState, allowed_languages, normalize_lang
from emori_adapter.hostapp.pokamind_whoami import fetch_whoami
from emori_adapter.site_settings import DEFAULT_POKAMIND_BASE

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
_LABELS = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}
CHECKS = ("control", "roleplay")

# ----- roleplay -----
_OPEN_PAGE_TIMEOUT_S = 20.0
_HOME_TIMEOUT_S = 45.0  # the session page's own summary cap is 30 s
_STATUS_POLL_S = 1.0
# /api/status counters that prove the Pokamind frame is talking, in display form.
_POKAMIND_COUNTERS = {
    "utterances": "AI line",
    "tts_ended": "AI speech ended",
    "cycles": "analysis cycle",
    "turn_armed": "turn armed",
    "turn_submitted": "turn submitted",
}
_ABORT_REASON = "rig_smoke_abort"
_NOTHING_STARTED = "nothing was started"
# The session page's documented keyboard end control (pages.py: "B=begin S=submit E=end").
_PRESS_END_JS = (
    "(function(){try{window.dispatchEvent(new KeyboardEvent('keydown',"
    "{key:'e',code:'KeyE'}));return true;}catch(e){return false;}})()"
)

_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# Belt and braces for the one rule this tool must never break: whatever reaches a
# detail line is scrubbed of anything credential-shaped before it is printed.
_REDACTIONS = (
    re.compile(r"pkr1\.[^\s,;)]*"),
    re.compile(r"(?i)\b(sso|token|access_token|secret|authorization)=[^\s&,;)]*"),
    re.compile(r"(?i)bearer\s+\S+"),
)
# A long unbroken run that is not a lowercase slug (a robot id is one) reads as a
# token seed, a JWT segment or another credential.
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{40,}")
_SLUG_RE = re.compile(r"[a-z0-9._=-]+")
_MAX_DETAIL = 240


@dataclass(frozen=True)
class Result:
    check: str
    status: str
    detail: str


class SmokeError(RuntimeError):
    """A content-free failure code raised by this tool's own helpers."""

    def __init__(self, code: str) -> None:
        self.error_code = code if _SAFE_CODE_RE.fullmatch(code or "") else "smoke_failed"
        super().__init__(self.error_code)


def safe_detail(text: object) -> str:
    """One printable line, credential-shaped runs removed, bounded."""

    value = " ".join(str(text).split())
    value = "".join(ch for ch in value if ch.isprintable())
    for pattern in _REDACTIONS:
        value = pattern.sub("[redacted]", value)
    value = _LONG_RUN_RE.sub(
        lambda match: match.group(0) if _SLUG_RE.fullmatch(match.group(0)) else "[redacted]",
        value,
    )
    if len(value) > _MAX_DETAIL:
        value = value[: _MAX_DETAIL - 1] + "…"
    return value


def _result(check: str, status: str, detail: str) -> Result:
    return Result(check, status, safe_detail(detail))


def error_code(error: BaseException) -> str:
    """The error's own safe code, else its class name -- never its message."""

    code = getattr(error, "error_code", None)
    if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code):
        return code
    return type(error).__name__



def _base(language: str | None) -> str:
    """The primary subtag of a language tag: ``ca-ES`` -> ``ca``."""

    return re.split(r"[-_]", (language or "").strip().lower())[0]


# --------------------------------------------------------------------------
# control: what Pokamind says this robot is
# --------------------------------------------------------------------------
def credential_kind(token: object) -> str:
    """WHICH credential signs (``robot_token`` or ``none``), never the credential."""

    return "none" if token is None else "robot_token"


def check_control(token: Any, *, base_url: str, clock: Callable[[], float]) -> Result:
    if token is None:
        return _result("control.whoami", SKIP, "no Pokamind token: install it (admin page -> "
                                               "Pokamind -> Replace token)")
    started = clock()
    answer = fetch_whoami(base_url, token)
    if not answer.ok or answer.whoami is None:
        return _result("control.whoami", FAIL,
                       f"GET /api/robot/whoami refused ({answer.code}): Pokamind did not "
                       "accept this robot's credential or is unreachable")
    who = answer.whoami
    return _result("control.whoami", PASS,
                   f"languages={','.join(who.languages) or '-'} modules={len(who.modules)} "
                   f"problems={len(who.problems())} ({_ms(clock() - started)})")


# --------------------------------------------------------------------------
# roleplay: the host app's HTTP API + the kiosk browser
# --------------------------------------------------------------------------
class HostappHttp:
    """JSON over loopback to the host app; never through a proxy."""

    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, request: urllib.request.Request) -> tuple[int, dict | None]:
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                raw = error.read()
            except Exception:  # noqa: BLE001
                raw = b""
        except Exception:  # noqa: BLE001
            return 0, None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        return status, payload if isinstance(payload, dict) else None

    def get_json(self, path: str) -> tuple[int, dict | None]:
        return self._call(urllib.request.Request(self.base_url + path, method="GET"))

    def post_json(self, path: str, body: dict) -> tuple[int, dict | None]:
        return self._call(urllib.request.Request(
            self.base_url + path, method="POST", data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
        ))


class KioskCdp:
    """The kiosk Chromium (start-kiosk.sh ``--remote-debugging-port``) on loopback.

    Three verbs only: where is the kiosk page, send it to a host-app path, press the
    session page's end key.  The page URL can carry a minted SSO login, so only its
    PATH ever leaves this class.
    """

    def __init__(self, cdp_port: int, hostapp_port: int, *, timeout_s: float = 5.0) -> None:
        self._cdp_port = cdp_port
        self._hostapp_port = hostapp_port
        self._timeout_s = timeout_s
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _kiosk_target(self) -> dict | None:
        try:
            with self._opener.open(f"http://127.0.0.1:{self._cdp_port}/json/list",
                                   timeout=self._timeout_s) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            parts = urlsplit(str(target.get("url", "")))
            if (parts.hostname in ("localhost", "127.0.0.1")
                    and parts.port == self._hostapp_port
                    and isinstance(target.get("webSocketDebuggerUrl"), str)):
                return target
        return None

    def current_path(self) -> str | None:
        target = self._kiosk_target()
        if target is None:
            return None
        return urlsplit(str(target.get("url", ""))).path or "/"

    def navigate(self, path: str) -> bool:
        target = self._kiosk_target()
        if target is None:
            return False
        parts = urlsplit(str(target.get("url", "")))
        reply = self._send(target["webSocketDebuggerUrl"], "Page.navigate",
                           {"url": f"{parts.scheme}://{parts.netloc}{path}"})
        return reply is not None and not reply.get("errorText")

    def press_end(self) -> bool:
        target = self._kiosk_target()
        if target is None:
            return False
        reply = self._send(target["webSocketDebuggerUrl"], "Runtime.evaluate",
                           {"expression": _PRESS_END_JS, "returnByValue": True})
        value = ((reply or {}).get("result") or {}).get("value")
        return value is True

    def _send(self, ws_url: str, method: str, params: dict) -> dict | None:
        try:
            from websockets.sync.client import connect
        except Exception:  # noqa: BLE001
            return None
        kwargs: dict[str, object] = {"open_timeout": self._timeout_s,
                                     "close_timeout": self._timeout_s}
        if "proxy" in inspect.signature(connect).parameters:
            kwargs["proxy"] = None
        deadline = time.monotonic() + self._timeout_s
        try:
            with connect(ws_url, **kwargs) as socket:
                socket.send(json.dumps({"id": 1, "method": method, "params": params}))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    message = json.loads(socket.recv(timeout=remaining))
                    if isinstance(message, dict) and message.get("id") == 1:
                        if "error" in message:
                            return None
                        result = message.get("result")
                        return result if isinstance(result, dict) else {}
        except Exception:  # noqa: BLE001 - never surface a URL-bearing exception
            return None


def _counters(status: dict | None) -> dict[str, int]:
    counters = (status or {}).get("counters")
    if not isinstance(counters, dict):
        return {}
    return {str(k): int(v) for k, v in counters.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _delta(now: dict[str, int], baseline: dict[str, int], name: str) -> int:
    return now.get(name, 0) - baseline.get(name, 0)


def _session_id(status: dict | None) -> str | None:
    session = (status or {}).get("session")
    if isinstance(session, dict):
        value = session.get("session_id")
        return value if isinstance(value, str) else ""
    return None


# The flag file of deploy/hub/emori-hub.service: while it exists the hub container, not the
# host application, runs the gateway. EMORI_HUB_FLAG moves it, for the tests.
_HUB_FLAG = "/opt/emori/HUB_IS_ROS"


def gateway_unit() -> str:
    """The systemd unit that runs the host app's gateway on this robot, for the hints."""

    flag = os.environ.get("EMORI_HUB_FLAG", "").strip() or _HUB_FLAG
    return "emori-hub" if os.path.exists(flag) else "emori-hostapp"


def check_roleplay(
    *,
    hostapp: Any,
    kiosk: Any,
    lang: str,
    live_lang: str,
    lang_explicit: bool,
    start_timeout_s: float,
    end_timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[Result]:
    start_name, end_name = "roleplay.start", "roleplay.end"

    def refused(status: str, reason: str) -> list[Result]:
        return [_result(start_name, status, reason),
                _result(end_name, SKIP, _NOTHING_STARTED)]

    code, health = hostapp.get_json("/healthz")
    if code != 200 or health is None:
        return refused(FAIL, f"host app unreachable on {getattr(hostapp, 'base_url', '')} "
                             f"(HTTP {code}; systemctl status {gateway_unit()})")
    if health.get("demo") is True:
        return refused(SKIP, "the host app runs in demo mode: there is no Pokamind session "
                             "to prove")
    if lang_explicit and _base(lang) != _base(live_lang):
        return refused(SKIP, f"the kiosk language is {live_lang}, not {lang}: switch it "
                             "(admin page -> Language) or drop --lang")
    code, status = hostapp.get_json("/api/status")
    if code != 200 or status is None:
        return refused(FAIL, f"/api/status answered HTTP {code}")
    if status.get("session"):
        return refused(SKIP, "a session is already active: not interrupting it; rerun when "
                             "the kiosk is idle")
    code, addon = hostapp.get_json("/api/addon/status")
    if code == 200 and addon is not None and addon.get("mode") not in (None, "idle"):
        return refused(SKIP, f"an add-on mode is active (mode={addon.get('mode')}): rerun "
                             "when it is idle")
    path = kiosk.current_path()
    if path is None:
        return refused(FAIL, "no kiosk page on the DevTools port (systemctl status kiosk)")
    if path.startswith("/session"):
        return refused(SKIP, "the kiosk is showing a session page: rerun when it is back "
                             "at /")
    code, menu = hostapp.get_json("/api/voice-menu")
    options = (menu or {}).get("options") if code == 200 else None
    if not isinstance(options, list) or not options or not isinstance(options[0], dict):
        return refused(FAIL, f"the live voice menu has no options (HTTP {code})")
    key = str(options[0].get("key", ""))
    topic = " ".join(str(options[0].get("name", "")).split())[:40]
    baseline = _counters(status)
    code, selection = hostapp.post_json("/api/voice-select", {"text": key})
    if code != 200 or selection is None:
        return refused(FAIL, f"/api/voice-select answered HTTP {code}")
    if selection.get("error_code"):
        return refused(FAIL, f"/api/voice-select refused ({selection.get('error_code')})")
    if selection.get("demo") is True:
        return refused(SKIP, "the host app has no Pokamind credential: the session would "
                             "be a demo")
    if selection.get("matched") is not True or str(selection.get("key")) != key:
        return refused(FAIL, f"/api/voice-select did not pick option {key} "
                             f"(picked {selection.get('key')})")
    session_url = selection.get("session_url")
    selection = None
    if not isinstance(session_url, str) or not session_url.startswith("/session?"):
        return refused(FAIL, "/api/voice-select returned no session page")
    sid = (parse_qs(urlsplit(session_url).query).get("sid") or [""])[0]
    if not sid:
        return refused(FAIL, "/api/voice-select returned a session page without a sid")
    label = f"option {key} '{topic}' ({_base(live_lang) or live_lang})"
    t0 = clock()
    navigated = kiosk.navigate(session_url)
    session_url = None  # it carries a minted SSO login; nothing below needs it
    if not navigated:
        return refused(FAIL, f"{label}: could not send the kiosk to the session page "
                             "(DevTools)")

    # 1. the page opened: driver.start_session ran for OUR sid
    opened_s = None
    while clock() - t0 < _OPEN_PAGE_TIMEOUT_S:
        _, status = hostapp.get_json("/api/status")
        if _session_id(status) == sid:
            opened_s = clock() - t0
            break
        sleep(_STATUS_POLL_S)
    if opened_s is None:
        kiosk.navigate("/")
        return [_result(start_name, FAIL, f"{label}: the session page did not open within "
                                          f"{_OPEN_PAGE_TIMEOUT_S:.0f} s (kiosk sent back "
                                          "to /)"),
                _result(end_name, SKIP, _NOTHING_STARTED)]

    # 2. Pokamind started it: the frame talks, and the learner's first turn is ARMED
    #    (an `end` before any turn opened is ignored by the frame's kiosk guard).
    first: tuple[str, float] | None = None
    armed_s = None
    ended_early = False
    while clock() - t0 < start_timeout_s:
        code, status = hostapp.get_json("/api/status")
        if code == 200 and status is not None:
            if _session_id(status) != sid:
                ended_early = True
                break
            now = _counters(status)
            if first is None:
                for name, shown in _POKAMIND_COUNTERS.items():
                    if _delta(now, baseline, name) > 0:
                        first = (shown, clock() - t0)
                        break
            if _delta(now, baseline, "turn_armed") > 0:
                armed_s = clock() - t0
                break
        sleep(_STATUS_POLL_S)
    seen = f"first Pokamind event: {first[0]} at {first[1]:.1f} s" if first else (
        "no Pokamind event")
    results: list[Result] = []
    if armed_s is not None:
        results.append(_result(start_name, PASS, f"{label}: page open {opened_s:.1f} s, "
                                                 f"{seen}, learner turn armed {armed_s:.1f} s"))
    elif ended_early:
        _, status = hostapp.get_json("/api/status")
        now = _counters(status)
        why = ("the page reported Pokamind never came up (session_start_failed)"
               if _delta(now, baseline, "session_start_failed") > 0
               else "the session ended before a learner turn opened")
        results.append(_result(start_name, FAIL, f"{label}: {why}; page open "
                                                 f"{opened_s:.1f} s, {seen}"))
        results.append(_end_after(hostapp, kiosk, baseline, how="ended on its own",
                                  clock=clock, sleep=sleep, end_name=end_name,
                                  skip_reason="the session ended on its own"))
        return results
    else:
        results.append(_result(start_name, FAIL, f"{label}: no learner turn armed within "
                                                 f"{start_timeout_s:.0f} s; page open "
                                                 f"{opened_s:.1f} s, {seen}"))

    # 3. end it through the official route, then prove the kiosk is idle again
    if first is None:
        # Nothing exchanged: the abort route for a session Pokamind never started.
        code, reply = hostapp.post_json("/api/session-start-failed",
                                        {"reason": _ABORT_REASON})
        sent = code == 202 and bool((reply or {}).get("aborted"))
        route = "aborted via /api/session-start-failed"
        refusal = f"/api/session-start-failed did not abort it (HTTP {code})"
    else:
        sent = (kiosk.current_path() or "").startswith("/session") and kiosk.press_end()
        route = "ended with the session page's end control"
        refusal = "could not press the session page's end control (DevTools)"
    if not sent:
        results.append(_result(end_name, FAIL, f"{refusal}; nudge the head to end the "
                                               "session, or wait for the watchdog"))
        return results
    results.append(_end_after(hostapp, kiosk, baseline, how=route, clock=clock, sleep=sleep,
                              end_name=end_name, sid=sid, end_timeout_s=end_timeout_s))
    return results


def _end_after(
    hostapp: Any,
    kiosk: Any,
    baseline: dict[str, int],
    *,
    how: str,
    clock: Callable[[], float],
    sleep: Callable[[float], Any],
    end_name: str,
    sid: str | None = None,
    end_timeout_s: float = 0.0,
    skip_reason: str | None = None,
) -> Result:
    """Wait for no session, then for the kiosk at ``/``, then an idle add-on."""

    started = clock()
    ended_s = 0.0
    if sid is not None:
        ended_s = None
        while clock() - started < end_timeout_s:
            code, status = hostapp.get_json("/api/status")
            if code == 200 and status is not None and _session_id(status) != sid:
                ended_s = clock() - started
                break
            sleep(_STATUS_POLL_S)
        if ended_s is None:
            return _result(end_name, FAIL, f"{how}, but the session was still active after "
                                           f"{end_timeout_s:.0f} s (the watchdog will end "
                                           "it; or nudge the head)")
    _, status = hostapp.get_json("/api/status")
    now = _counters(status)
    if _delta(now, baseline, "end_pokamind_failed") > 0:
        outcome = "Pokamind's end chain failed (no results)"
    elif _delta(now, baseline, "completed") > 0:
        outcome = "Pokamind completed it"
    elif _delta(now, baseline, "end_local_fallback") > 0:
        outcome = "finalized locally (Pokamind never answered)"
    elif _delta(now, baseline, "session_start_failed") > 0:
        outcome = "finalized as never started"
    else:
        outcome = "finalized"
    home_started = clock()
    home_s = None
    while clock() - home_started < _HOME_TIMEOUT_S:
        if kiosk.current_path() == "/":
            home_s = clock() - home_started
            break
        sleep(_STATUS_POLL_S)
    if home_s is None:
        kiosk.navigate("/")
        return _result(end_name, FAIL, f"{how}; {outcome}, but the kiosk did not return to / "
                                       f"within {_HOME_TIMEOUT_S:.0f} s (sent it back)")
    code, addon = hostapp.get_json("/api/addon/status")
    if code == 200 and addon is not None and addon.get("mode") not in (None, "idle"):
        return _result(end_name, FAIL, f"{how}; {outcome}; kiosk at /, but an add-on "
                                       f"mode is active ({addon.get('mode')})")
    _, status = hostapp.get_json("/api/status")
    if status is not None and status.get("session"):
        return _result(end_name, FAIL, f"{how}; {outcome}, but another session is active")
    if skip_reason is not None:
        return _result(end_name, SKIP, f"{skip_reason} ({outcome}); kiosk back at / "
                                       f"({home_s:.1f} s)")
    return _result(end_name, PASS, f"{how}: {outcome} {ended_s:.1f} s later; kiosk back at "
                                   f"/ ({home_s:.1f} s), no session active")



# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def exit_code(results: Iterable[Result], *, strict: bool = False) -> int:
    statuses = [result.status for result in results]
    if FAIL in statuses:
        return 1
    if strict and SKIP in statuses:
        return 2
    return 0


def format_text(results: list[Result], header: dict[str, str], notes: list[str]) -> str:
    width = max([len(result.check) for result in results] + [10])
    lines = ["rig-smoke  " + "  ".join(f"{k}={safe_detail(v)}" for k, v in header.items())]
    for result in results:
        lines.append(f"{_LABELS[result.status]}  {result.check:<{width}}  "
                     f"{safe_detail(result.detail)}")
    for note in notes:
        lines.append(f"note: {safe_detail(note)}")
    return "\n".join(lines)


def format_json(results: list[Result], header: dict[str, str], notes: list[str],
                code: int) -> str:
    payload = {key: safe_detail(value) for key, value in header.items()}
    payload["results"] = [
        {**asdict(result), "detail": safe_detail(result.detail)} for result in results
    ]
    payload["notes"] = [safe_detail(note) for note in notes]
    payload["exit_code"] = code
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def notes_for(results: list[Result]) -> list[str]:
    """The side effects this run had, said once at the end of the report."""

    notes = []
    by_name = {result.check: result for result in results}
    end = by_name.get("roleplay.end")
    if end is not None and end.detail != _NOTHING_STARTED:
        notes.append("roleplay: the robot spoke and moved; one anonymous session was added "
                     "to the dashboard, and a results report may have been emailed to "
                     "EMORI_RESULTS_TO")
    return notes


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_only(value: str | None) -> tuple[str, ...]:
    if not value:
        return CHECKS
    chosen = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    unknown = [part for part in chosen if part not in CHECKS]
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(
            f"--only takes {', '.join(CHECKS)} (comma-separated), not {', '.join(unknown)}")
    return chosen


def live_kiosk_language() -> str:
    """The kiosk language the host app is in: its persisted switch, else EMORI_LANG."""

    # server.py's own default (an empty value means unset), and the languages this
    # robot's Pokamind scope offers, exactly as the host app builds its LangState.
    default = (os.environ.get("EMORI_LANG") or "").strip() or "en"
    try:
        current = LangState(default=default, allowed=allowed_languages()).current()
    except Exception:  # noqa: BLE001
        current = default
    return normalize_lang(current) or normalize_lang(default) or "ca"


def _ms(seconds: float) -> str:
    return f"{_ms_int(seconds)} ms"


def _ms_int(seconds: float) -> int:
    return int(round(max(0.0, seconds) * 1000))


def _seconds(seconds: float) -> str:
    return f"{max(0.0, seconds):.1f} s"


def run(args: argparse.Namespace, *, settings: object | None = None) -> tuple[
        list[Result], dict[str, str]]:
    if settings is None:
        load_settings()  # reads /opt/emori/.env into the environment, as the host app does
    live_lang = live_kiosk_language()
    lang = args.lang or live_lang
    selected = args.only
    loaded = _robot_token.load_token()
    token = loaded.token
    header = {
        "robot": str(getattr(token, "robot_id", "") or "?"),
        "credential": credential_kind(token),
        "lang": lang,
    }
    results: list[Result] = []
    if "control" in selected:
        base_url = (os.environ.get("POKAMIND_BASE", "").strip()
                    or DEFAULT_POKAMIND_BASE).rstrip("/")
        try:
            results.append(check_control(token, base_url=base_url, clock=time.monotonic))
        except Exception as error:  # noqa: BLE001
            results.append(_result("control", FAIL, f"unexpected {error_code(error)}"))
    if "roleplay" in selected:
        hostapp_port = int(os.environ.get("HOSTAPP_PORT", "8080"))
        cdp_port = int(os.environ.get("EMORI_KIOSK_CDP_PORT", "9222"))
        try:
            results += check_roleplay(
                hostapp=HostappHttp(f"http://127.0.0.1:{hostapp_port}"),
                kiosk=KioskCdp(cdp_port, hostapp_port),
                lang=lang, live_lang=live_lang, lang_explicit=bool(args.lang),
                start_timeout_s=args.start_timeout, end_timeout_s=args.end_timeout,
            )
        except Exception as error:  # noqa: BLE001
            results.append(_result("roleplay", FAIL, f"unexpected {error_code(error)}"))
    return results, header


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig-smoke",
        description=("EMORI post-deploy smoke test ON the robot: the Pokamind control "
                     "plane and roleplay, end to end, with this robot's own Pokamind "
                     "token. NOT read-only: roleplay makes the robot speak and move."),
    )
    parser.add_argument("--only", type=parse_only, default=CHECKS,
                        help="control and/or roleplay (comma-separated; default all)")
    parser.add_argument("--lang", choices=("ca", "en"), default=None,
                        help="kiosk language for the checks (default: the kiosk's "
                             "live language)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of lines")
    parser.add_argument("--strict", action="store_true", help="exit 2 when a check SKIPs")
    parser.add_argument("--start-timeout", type=float, default=150.0,
                        help="roleplay: seconds for Pokamind to arm the first turn")
    parser.add_argument("--end-timeout", type=float, default=180.0,
                        help="roleplay: seconds for the session to end after the end "
                             "control")
    parser.add_argument("--verbose", action="store_true",
                        help="show the modules' content-free warnings on stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    # The wrapper hands every run the same arguments, an add-on's options included.
    args, ignored = build_parser().parse_known_args(argv)
    if ignored:
        print(f"rig-smoke: not options of this test, ignored: {' '.join(ignored)}",
              file=sys.stderr)
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR,
                        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    results, header = run(args)
    code = exit_code(results, strict=args.strict)
    notes = notes_for(results)
    if args.json:
        print(format_json(results, header, notes, code))
    else:
        print(format_text(results, header, notes))
    return code


if __name__ == "__main__":
    sys.exit(main())
