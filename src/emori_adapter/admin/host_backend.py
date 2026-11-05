"""Who answers the Wi-Fi and volume API on this robot: Pollen's daemon, or this process.

On a v1 robot the reachy-mini daemon on ``:8000`` serves ``/wifi/*`` and
``/api/volume/*``, and emori-admin is a proxy in front of it.  On a v2 robot the
ROS 2 node (``reachy_mini_ros2``) owns ``:8000`` through its REST shim, which
speaks the daemon's motion dialect and nothing else: every ``/wifi`` and
``/api/volume`` call answers 404, and the daemon's boot-time
``ensure_wifi_on_startup()`` no longer runs at all.  So on v2 emori-admin does
that work itself (:mod:`emori_adapter.admin.netman`, :mod:`.mixer`), with the
daemon's own semantics and response shapes, and the decision of which one to
use lives here and nowhere else.

``EMORI_ADMIN_HOST_BACKEND`` = ``auto`` (default) | ``daemon`` | ``host``.

``auto`` picks ``host`` when

* ``GET :8000/api/daemon/status`` reports ``backend: "reachy_mini_ros2"`` -- the
  shim's deliberate tell (Pollen's daemon has no ``backend`` field at all);
* a ``/wifi/*`` or ``/api/volume/*`` call answers a ROUTE-missing 404 (FastAPI's
  bare ``{"detail": "Not Found"}``; the daemon's own "network not in saved
  networks" 404 from ``/wifi/forget`` is a real answer and does not count);
* ``:8000`` does not answer at all and the v2 fence flag
  (``/opt/emori/V2_OWNS_THE_BUS``, ``deploy/robot/ros2_native/``) is raised --
  Pollen's daemon cannot start on such a robot, so the silence is the node
  restarting, and Wi-Fi must not wait for it;

and ``daemon`` otherwise.  An unreachable ``:8000`` without the flag is not
evidence either way: the previous answer is kept (``daemon`` on a fresh start,
which reports ``daemon_down`` exactly as before).

The answer is cached for a minute, so a v1 rollback heals by itself; an answer
made while ``:8000`` was silent is re-checked after a few seconds instead.  The
backend is logged once at INFO, and again whenever it changes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from emori_adapter.admin import probes

log = logging.getLogger("emori.admin.backend")

ENV_KEY = "EMORI_ADMIN_HOST_BACKEND"
AUTO = "auto"
DAEMON = "daemon"
HOST = "host"
MODES = (AUTO, DAEMON, HOST)

# What the v2 shim says in GET /api/daemon/status (rest_shim.BACKEND_MARKER).
V2_BACKEND_MARKER = "reachy_mini_ros2"
# The v2 fence (deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf): while
# this file exists Pollen's daemon cannot start, whatever asks it to.
V2_FENCE_FLAG = "/opt/emori/V2_OWNS_THE_BUS"

DECISION_TTL_S = 60.0
# An answer made while :8000 was silent is a guess; ask again soon.
UNSURE_TTL_S = 5.0
# A daemon that answers 404 on /wifi is a daemon without that router (started
# without --wireless-version). Remember it for longer than one TTL, or every
# re-check would flip back to the daemon and log the same change twice a minute.
ROUTE_MISSING_STICKY_S = 600.0
# The status probe is local and either answers in milliseconds or not at all.
PROBE_TIMEOUT_S = 3.0

# Reasons, as they appear in the journal line.
FORCED = "forced"
V2_STATUS = "v2_status"
ROUTE_MISSING = "route_missing"
FENCE_FLAG = "fence_flag"
DAEMON_STATUS = "daemon_status"
UNREACHABLE = "unreachable"

_WHY = {
    FORCED: "set by %s" % ENV_KEY,
    V2_STATUS: ":8000 is the v2 robot node's REST shim (backend=%s)" % V2_BACKEND_MARKER,
    ROUTE_MISSING: "the daemon on :8000 answers 404 on its Wi-Fi/volume routes",
    FENCE_FLAG: ":8000 is silent and the v2 fence flag %s is raised" % V2_FENCE_FLAG,
    DAEMON_STATUS: "Pollen's reachy-mini daemon answers on :8000",
    UNREACHABLE: ":8000 is not answering (kept the previous choice)",
}


@dataclass(frozen=True)
class Decision:
    backend: str          # DAEMON | HOST
    reason: str           # one of the reason constants above
    decided_at: float

    @property
    def decisive(self) -> bool:
        """False only for a choice made while ``:8000`` was silent and unflagged."""

        return self.reason != UNREACHABLE


def configured_mode() -> str:
    raw = (os.environ.get(ENV_KEY, "") or "").strip().lower()
    return raw if raw in MODES else (AUTO if not raw else "")


def route_missing(result: probes.Http) -> bool:
    """Is this the 404 of a route that does not exist (not a real 404 answer)?

    FastAPI answers an unknown path with exactly ``{"detail": "Not Found"}``.
    The daemon's ``POST /wifi/forget`` also answers 404 -- "Network 'x' not found
    in saved networks." -- and that one is information, not a missing router.
    """

    if not (result.reached and result.status == 404):
        return False
    payload = result.payload
    return not isinstance(payload, dict) or payload.get("detail") == "Not Found"


def error_slug(result: probes.Http, *, when_unreachable: str = "daemon_down") -> str:
    """The one word a failed Wi-Fi/volume call is reported as (``""`` when it worked).

    A route-missing 404 is ``route_missing``, never ``http_404``: the phone maps it
    to "this admin service asked for a route the robot does not have" plus the way
    back in, and readycheck FAILs on it (a bare "HTTP 404" tells the operator
    nothing).
    """

    if result.ok:
        return ""
    if not result.reached:
        return when_unreachable
    if route_missing(result):
        return ROUTE_MISSING
    return result.error or "http_%d" % result.status


def failure(result: probes.Http, *, when_unreachable: str = "daemon_down") -> dict:
    """:func:`probes.fail` for a Wi-Fi/volume call, with :func:`error_slug`'s word."""

    return {"ok": False,
            "error": error_slug(result, when_unreachable=when_unreachable) or "error",
            "status": result.status}


class Selector:
    """The one decision.  Thread-safe; the ``:8000`` probe runs under the lock
    so the overview's parallel sub-probes cost one status call, not three."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 fence_flag: Optional[str] = None) -> None:
        self._clock = clock
        self.fence_flag = fence_flag if fence_flag is not None else V2_FENCE_FLAG
        self._lock = threading.Lock()
        self._decision: Optional[Decision] = None
        self._route_missing_at: Optional[float] = None
        self._logged: Optional[str] = None
        self._warned_bad_value = False

    def reset(self) -> None:
        with self._lock:
            self._decision = None
            self._route_missing_at = None
            self._logged = None
            self._warned_bad_value = False

    # ------------------------------------------------------------------
    def decide(self) -> Decision:
        with self._lock:
            now = self._clock()
            mode = configured_mode()
            if not mode:
                if not self._warned_bad_value:
                    self._warned_bad_value = True
                    log.warning("%s=%r is not one of auto|daemon|host; using auto",
                                ENV_KEY, os.environ.get(ENV_KEY))
                mode = AUTO
            if mode in (DAEMON, HOST):
                decision = Decision(mode, FORCED, now)
            else:
                cached = self._decision
                if cached is not None and cached.reason != FORCED and \
                        now - cached.decided_at < self._ttl(cached):
                    return cached
                decision = self._probe(now, cached)
            self._decision = decision
            self._announce(decision)
            return decision

    def note_route_missing(self, path: str) -> Decision:
        """A ``/wifi`` or ``/api/volume`` call just 404ed: this is a host robot."""

        with self._lock:
            now = self._clock()
            if configured_mode() == DAEMON:
                # Forced means forced: the operator asked for the daemon's answer,
                # 404 included.
                return self._decision or Decision(DAEMON, FORCED, now)
            self._route_missing_at = now
            decision = Decision(HOST, ROUTE_MISSING, now)
            self._decision = decision
            if self._logged != HOST:
                log.info("the daemon has no %s route", path)
            self._announce(decision)
            return decision

    # ------------------------------------------------------------------
    @staticmethod
    def _ttl(decision: Decision) -> float:
        return UNSURE_TTL_S if decision.reason == UNREACHABLE else DECISION_TTL_S

    def _recent_route_missing(self, now: float) -> bool:
        return (self._route_missing_at is not None
                and now - self._route_missing_at < ROUTE_MISSING_STICKY_S)

    def _probe(self, now: float, previous: Optional[Decision]) -> Decision:
        result = probes.daemon("GET", "/api/daemon/status", timeout_s=PROBE_TIMEOUT_S)
        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.reached:
            if payload.get("backend") == V2_BACKEND_MARKER:
                return Decision(HOST, V2_STATUS, now)
            if self._recent_route_missing(now):
                return Decision(HOST, ROUTE_MISSING, now)
            return Decision(DAEMON, DAEMON_STATUS, now)
        if self._fence_raised():
            return Decision(HOST, FENCE_FLAG, now)
        if self._recent_route_missing(now):
            return Decision(HOST, ROUTE_MISSING, now)
        kept = previous.backend if previous is not None else DAEMON
        return Decision(kept, UNREACHABLE, now)

    def _fence_raised(self) -> bool:
        try:
            return os.path.exists(self.fence_flag)
        except (OSError, ValueError):
            return False

    def _announce(self, decision: Decision) -> None:
        if decision.backend == self._logged:
            return
        previous, self._logged = self._logged, decision.backend
        if decision.backend == HOST:
            what = ("emori-admin runs Wi-Fi (nmcli) and volume (amixer) itself")
        else:
            what = "Wi-Fi and volume go through the reachy-mini daemon's API"
        log.info("Wi-Fi/volume backend: %s%s -- %s (%s)", decision.backend,
                 "" if previous is None else " (was %s)" % previous, what,
                 _WHY.get(decision.reason, decision.reason))


_SELECTOR = Selector()


def selector() -> Selector:
    return _SELECTOR


def decision() -> Decision:
    return _SELECTOR.decide()


def current() -> str:
    """``"daemon"`` or ``"host"`` -- the backend every Wi-Fi/volume call uses now."""

    return _SELECTOR.decide().backend


def note_route_missing(path: str) -> Decision:
    return _SELECTOR.note_route_missing(path)


def reset() -> None:
    _SELECTOR.reset()
