"""Where a decision stops being ours: the skill facade's transport plug point.

The graph decides in ``hri_engagement_behaviors`` and the decision becomes a bounded
skill request.  Somebody then has to put that request on somebody else's wire.  Today
that is the EMORI hostapp's ``/api/skill`` intake, because the hostapp owns the one
``RobotPlayer`` queue and therefore the robot.

**This is the whole adaptation surface.**  Transports are pluggable; to target another
skill interface, add a class here.  It is one more class in this file plus one launch
argument - not a rewrite of the node, not a change to the behaviour policy, and not a
second component that can move the robot.  That is the only reason the transport is an
object at all instead of five lines of ``urllib`` inside the node.

A transport may also arrive from *outside* this package, and one now does: v2's
``emori_skill_native`` speaks to a native robot node over ROS actions, and it cannot
live here because it needs ``rclpy`` and the adapter core, neither of which this
package is allowed to import.  So the plug point has three tiers, consulted in this
order by :func:`select_transport`: the built-ins below, anything a plugin handed to
:func:`register_transport`, and finally the ``emori_skill_facade.transports`` entry
points.  The entry point is what makes ``EMORI_SKILL_TRANSPORT=ros_native`` work with
nobody importing the plugin first; the explicit call is what makes it work when the
plugin *was* imported but its distribution metadata is missing (a colcon overlay built
from a source tree that was moved, which is a Tuesday on a robot).  A plugin that
cannot be loaded is skipped and said out loud - never raised - because a broken
optional wheel must not be the reason the shipped transports stop existing.

Stdlib only, on purpose: this runs inside the ROS container, where the adapter core is
installed but its optional client stack is not guaranteed, and one missing wheel must
not be the reason the robot stops moving.  Every judgement about what an answer *means*
stays in ``contract.py``; a transport only performs the hop and reports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib import metadata
import json
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from emori_skill_facade.contract import (
    SkillOutcome,
    accepted_outcome,
    refusal_outcome,
    unreachable_outcome,
)

# The hostapp on the robot's own loopback. Host networking is why this is 127.0.0.1 and
# why the intake can refuse everything else.
DEFAULT_SKILL_URL = "http://127.0.0.1:8080/api/skill"
# Short on purpose: a skill request is worth one gesture, and a slow hostapp must never
# become a stalled action server.
DEFAULT_TIMEOUT_S = 1.0
# The shipped transport. Named here so the node, the launch file and the tests agree.
DEFAULT_TRANSPORT = "hostapp_http"
# Where a transport that lives in another package announces itself. A plugin's setup.py
# writes `emori_skill_facade.transports = <name> = <module>:<factory>` and the facade
# finds it with no import of its own - which is the only way a package this one may not
# depend on (rclpy, the adapter core) can still be selected by name from an env var.
ENTRY_POINT_GROUP = "emori_skill_facade.transports"

# rclpy's logger says `warn`; the stdlib's says `warning` and dropped `warn` in 3.13.
# A transport is handed whichever one its caller has, so ask for both names.
_LOG_ALIASES = {"warn": ("warn", "warning"), "warning": ("warning", "warn")}


def _say(log, level: str, message: str) -> None:
    """Log through whatever logger-shaped object we were given, or stay silent."""

    if log is None:
        return
    for name in _LOG_ALIASES.get(level, (level,)):
        method = getattr(log, name, None)
        if callable(method):
            method(message)
            return


def _error_body(error: urllib.error.HTTPError) -> str:
    """The refusal's body, best effort: the status alone is still an answer."""

    try:
        return error.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a body that cannot be read is no body
        return ""


def url_host(url: str) -> str:
    """``host:port`` of a skill URL, for the ledger.

    The path is left out: the one thing an operator reading ``/diagnostics`` needs is
    *which box* this facade is talking to.
    """

    try:
        return urllib.parse.urlsplit(str(url)).netloc or ""
    except ValueError:
        return ""


class SkillTransport(ABC):
    """One bounded skill request in, one :class:`SkillOutcome` out.

    Implementations never raise for a refusal or an outage: a transport that threw
    would turn "the hostapp is busy" into a dead action server.  Everything that can
    go wrong on the hop comes back as an outcome with a counter already on it.
    """

    #: Registry key, and the ``hardware_id`` of the node's /diagnostics status.
    name: str = ""

    @abstractmethod
    def submit(self, body: dict, reason: str) -> SkillOutcome:
        """Perform the hop for one request. ``reason`` is provenance for the trace."""

    @abstractmethod
    def describe(self) -> str:
        """One line for the startup log: where requests go and under what terms."""


class HostappHttpTransport(SkillTransport):
    """POST the EMORI hostapp's ``/api/skill`` - the single-writer intake.

    Never the Reachy daemon.  That one indirection is what keeps the single-writer rule
    intact with ROS in the picture: the hostapp's one ``RobotPlayer`` queue keeps its
    generation guards, its ``drop_if_busy`` interlock, the ``busy`` gate the push-turn
    detector needs, and the add-on ownership check.  A node POSTing the daemon
    directly would bypass all four at once.
    """

    name = "hostapp_http"

    def __init__(self, *, url: str = DEFAULT_SKILL_URL, key: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S, log=None, node=None) -> None:
        # `node` is the facade node itself, offered to every factory so a transport that
        # needs the ROS graph (v2's ros_native) can take it. An HTTP hop has no use for
        # one, and a plug point whose members need special-casing at the call site is not
        # a plug point - so it is accepted and dropped rather than absent.
        del node
        self.url = str(url).strip() or DEFAULT_SKILL_URL
        self.key = str(key or "")
        self.timeout_s = max(0.1, float(timeout_s))
        self._log = log

    def submit(self, body: dict, reason: str) -> SkillOutcome:
        # Everything, including building the request, sits inside the try: a URL with
        # no scheme makes `Request()` itself raise, and an exception escaping here would
        # leave `posted` incremented with no outcome ever counted, while the action
        # server would answer a default Result whose empty error_code reads as success.
        try:
            request = urllib.request.Request(
                self.url, method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-Emori-Skill-Key": self.key},
            )
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return accepted_outcome(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            outcome = refusal_outcome(e.code, _error_body(e))
            _say(self._log, "debug",
                 f"{body.get('skill')} refused: {outcome.error_code} ({reason})")
            return outcome
        except Exception as e:  # noqa: BLE001 - urlopen raises a whole family
            _say(self._log, "warn", f"skill intake unreachable ({e})")
            return unreachable_outcome()

    def describe(self) -> str:
        key = "key set" if self.key else "NO KEY (the intake will answer 403 bad_key)"
        return f"hostapp_http -> POST {self.url} [{key}, timeout {self.timeout_s:.1f}s]"


class LogOnlyTransport(SkillTransport):
    """Accept everything, move nothing, remember what was asked.

    For benches, for the standalone image's smoke test (which has no hostapp to POST to
    and no robot to move), and for reading a behaviour policy's output as a list rather
    than through a packet capture.  It answers the way a healthy intake would, so the
    node's ledger and the action results exercise the same code path they will on a robot.
    """

    name = "log"

    #: Accepted with nothing dropped - the same 200 body the intake sends.
    _ANSWER = '{"ok": true, "submitted": 1, "dropped": false}'

    def __init__(self, *, url: str = "", key: str = "", timeout_s: float = 0.0,
                 log=None, node=None) -> None:
        # url/key/timeout/node are accepted and ignored so every transport shares one
        # factory signature; a plug point whose members need special-casing at the call
        # site is not a plug point.
        del url, key, timeout_s, node
        self._log = log
        self.bodies: list[tuple[dict, str]] = []

    def submit(self, body: dict, reason: str) -> SkillOutcome:
        self.bodies.append((dict(body), str(reason)))
        _say(self._log, "info", f"[log transport] {json.dumps(body)} ({reason})")
        return accepted_outcome(self._ANSWER)

    def describe(self) -> str:
        return ("log -> nothing is sent and nothing moves; every request is accepted "
                "and recorded")


#: The wires this package ships. One entry per format it can speak with nothing
#: installed beyond the stdlib; a plugin's wire never lands here (see
#: ``REGISTERED_TRANSPORTS``), so "what does the facade itself contain" stays readable.
TRANSPORTS: dict[str, type[SkillTransport]] = {
    HostappHttpTransport.name: HostappHttpTransport,
    LogOnlyTransport.name: LogOnlyTransport,
}

#: Wires handed over at runtime by a plugin that imported this module. Separate from
#: ``TRANSPORTS`` on purpose: the built-ins are a promise this package keeps alone.
REGISTERED_TRANSPORTS: dict[str, Callable[..., SkillTransport]] = {}


def register_transport(name: str, factory: Callable[..., SkillTransport]) -> str:
    """Add a transport this package could not contain, and return its registry key.

    Called at import time by a plugin (``emori_skill_native`` does it in its
    ``__init__``), so importing the plugin anywhere in the process is enough to make
    its name selectable - the belt to the entry point's braces, because a colcon
    overlay can be missing its distribution metadata while its modules import fine.

    A built-in name may NOT be taken over.  ``hostapp_http`` means "POST the hostapp's
    single-writer intake" wherever it is read - in a launch file, in a compose env var,
    in a rehearsal ledger - and a plugin that could redefine it could silently move the
    robot over a wire nobody chose.  Re-registering a plugin's own name replaces it,
    which is what a reload during development does and is harmless.
    """

    key_name = (name or "").strip().lower()
    if not key_name:
        raise ValueError("a transport needs a name")
    if not callable(factory):
        raise TypeError("transport factory for %r is not callable: %r" % (key_name, factory))
    if key_name in TRANSPORTS:
        raise ValueError(
            "%r is a built-in transport and cannot be replaced; register under a new name"
            % key_name)
    REGISTERED_TRANSPORTS[key_name] = factory
    return key_name


def _entry_points() -> list:
    """The ``emori_skill_facade.transports`` entry points, or nothing at all.

    Wrapped because reading distribution metadata touches the filesystem: a half-written
    overlay, an unreadable egg-info or an old Python's API change must degrade to "no
    plugins found", never to a facade that will not start.
    """

    try:
        return list(metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception:  # noqa: BLE001 - metadata reading fails in many unrelated ways
        return []


def known_transports() -> list[str]:
    """Every name that could be selected right now, built-in or plugged in."""

    names = set(TRANSPORTS) | set(REGISTERED_TRANSPORTS)
    names |= {str(point.name).strip().lower() for point in _entry_points()}
    return sorted(names)


def _entry_point_factory(key_name: str, log=None):
    """Load the plugin factory published under ``key_name``, or ``None``.

    A plugin that fails to import is reported and skipped rather than raised: the entry
    point group is open, so somebody else's broken package must not take the facade
    down with it.  The caller then reports the name as unknown, which is exactly what
    it is - it could not be loaded.
    """

    for point in _entry_points():
        if str(point.name).strip().lower() != key_name:
            continue
        try:
            return point.load()
        except Exception as error:  # noqa: BLE001 - a plugin can fail any way it likes
            _say(log, "warn", f"skill transport plugin {point.name!r} failed to load: {error}")
    return None


def select_transport(name: str, *, url: str = DEFAULT_SKILL_URL, key: str = "",
                     timeout_s: float = DEFAULT_TIMEOUT_S, log=None,
                     node=None) -> SkillTransport:
    """Build the named transport, or say exactly which names exist.

    Looked up in three tiers - built-ins, plugins that registered themselves, entry
    points - so a shipped name can never be shadowed by an installed package, and a
    plugin needs no import from anybody to be reachable by name.

    Unlike ``normalize_owner``, an unknown name raises rather than silently defaulting:
    the owner knob has a safe fallback ("do not move"), a transport has no such thing.
    Guessing would either move the robot over a wire the operator did not ask for, or
    quietly stop moving it.  The caller decides what to do with the refusal.

    ``node`` is the facade node, passed to every factory; the built-ins ignore it and a
    transport that speaks ROS (v2's ``ros_native``) uses it to own its clients.
    """

    key_name = (name or "").strip().lower()
    factory = TRANSPORTS.get(key_name) or REGISTERED_TRANSPORTS.get(key_name)
    if factory is None:
        factory = _entry_point_factory(key_name, log=log)
    if factory is None:
        raise ValueError(
            "unknown skill transport %r; known transports: %s"
            % (name, ", ".join(known_transports())))
    return factory(url=url, key=key, timeout_s=timeout_s, log=log, node=node)
