"""Apply what Pokamind (or the robot itself) changed, once the robot is idle.

Some settings take effect only when the hostapp starts: a value derived from this
robot's Pokamind record that changed at a scope sync (``scope_changed``), a dashboard
password reset on this page (``dashboard_password``), a change an installed add-on
asks to have applied unattended. The hostapp collects the first in
``GET /api/admin/site`` (``restart_pending``, ``restart_reasons``) and never restarts
itself. :class:`SiteApplyWatcher` is the one thing that may restart it UNATTENDED:

* only for :data:`AUTO_APPLY_REASONS` (and the add-on's own, ``auto_reasons``). A setting
  an operator saved on the admin page (``settings_saved``) waits for the Apply button:
  the person who made it decides when the kiosk blanks;
* only after the robot has been idle (``GET /api/admin/summary`` ``idle``: no
  session, no QR training lease, no add-on mode, nothing the add-on is in the middle of --
  the hostapp's own fail-closed answer) for :func:`idle_threshold_s` CONSECUTIVE seconds,
  default 600, as seen by polls every :data:`POLL_S` seconds. Any busy reading, an
  unreachable hostapp, or nothing pending starts the count again;
* at most once per :data:`MIN_INTERVAL_S` (30 minutes), whatever happens;
* ``EMORI_SITE_AUTO_APPLY_IDLE_S=0`` switches it off (the Apply button still works).

Nothing here talks to the network or runs a command by itself: the HTTP call and the
restart are injected (emori-admin passes its token-carrying hostapp call and the
allowlisted ``systemctl restart``), so this module stays a pure decision a test can
drive with a fake clock. It logs reasons and decision codes, never a value.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("emori.admin.site_apply")

ENV_IDLE_S = "EMORI_SITE_AUTO_APPLY_IDLE_S"
DEFAULT_IDLE_S = 600.0
POLL_S = 60.0
MIN_INTERVAL_S = 1800.0
# The reasons applied without a person: derived from Pokamind, or generated here.
AUTO_APPLY_REASONS = frozenset({"scope_changed", "dashboard_password"})
# The dashboard password lives in secrets/admin.env, which this service writes; the
# hostapp reads it at start. Its row in GET /api/admin/site says whether the running
# hostapp still has the old one (running_differs).
DASHBOARD_PASSWORD_KEY = "EMORI_DASH_PASSWORD"
REASON_DASHBOARD_PASSWORD = "dashboard_password"

# tick() answers one of these (for the log line and the page).
DECISIONS = ("disabled", "hostapp_unreachable", "nothing_pending", "manual_reasons",
             "busy", "waiting_idle", "rate_limited", "applied", "restart_failed")


def idle_threshold_s(environ=None) -> float:
    """``EMORI_SITE_AUTO_APPLY_IDLE_S``: seconds of continuous idle; 0 = never auto-apply.

    Unset, empty, negative or not a number -> the default (600), with a WARNING for the
    two that are typos.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_IDLE_S, "") or "").strip()
    if not raw:
        return DEFAULT_IDLE_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s is not a number; using %.0f s", ENV_IDLE_S, DEFAULT_IDLE_S)
        return DEFAULT_IDLE_S
    if value != value or value < 0:  # NaN or negative
        log.warning("%s is negative; using %.0f s", ENV_IDLE_S, DEFAULT_IDLE_S)
        return DEFAULT_IDLE_S
    return value


def _payload(result: Any) -> Optional[dict]:
    """The JSON body of a successful call (a ``probes.Http``-shaped object), or None."""

    if result is None or getattr(result, "ok", False) is not True:
        return None
    payload = getattr(result, "payload", None)
    return payload if isinstance(payload, dict) else None


_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
# A pending restart the hostapp names no reason for: never applied unattended.
REASON_UNSPECIFIED = "unspecified"


def _safe_reason(value: object) -> bool:
    return isinstance(value, str) and _REASON_RE.match(value) is not None


class SiteApplyWatcher:
    """Decides when to restart the hostapp for derived settings (see the module doc).

    ``fetch(method, path)`` returns a ``probes.Http``-shaped answer from the hostapp
    (``ok``, ``payload``). ``restart()`` restarts the hostapp and returns
    ``{"ok": bool, ...}`` (``system.service_action``). ``clock`` is monotonic seconds.
    ``auto_reasons`` is the set applied without a person (default
    :data:`AUTO_APPLY_REASONS`).
    """

    def __init__(self, *, fetch: Callable[[str, str], Any], restart: Callable[[], Any],
                 clock: Callable[[], float] = time.monotonic,
                 idle_s: Optional[float] = None, poll_s: float = POLL_S,
                 min_interval_s: float = MIN_INTERVAL_S,
                 auto_reasons: Optional[frozenset] = None) -> None:
        self._auto_reasons = (AUTO_APPLY_REASONS if auto_reasons is None
                              else frozenset(auto_reasons))
        self._fetch = fetch
        self._restart = restart
        self._clock = clock
        self.idle_s = idle_threshold_s() if idle_s is None else max(0.0, float(idle_s))
        self.poll_s = float(poll_s)
        self.min_interval_s = float(min_interval_s)
        self._lock = threading.Lock()
        self._queued: list[str] = []
        self._idle_since: Optional[float] = None
        self._last_applied: Optional[float] = None
        self._last_decision = "not_run"
        self._last_reasons: list[str] = []

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return self.idle_s > 0

    def queue(self, reason: str) -> None:
        """A reason this service knows of and the hostapp cannot (``dashboard_password``)."""

        if not _safe_reason(reason):
            return
        with self._lock:
            if reason not in self._queued:
                self._queued.append(reason)

    def queued(self) -> list[str]:
        with self._lock:
            return list(self._queued)

    def status(self) -> dict:
        """For the page: whether it is on, the threshold, and what it decided last."""

        with self._lock:
            now = self._clock()
            return {
                "enabled": self.enabled,
                "idle_s": self.idle_s,
                "idle_for_s": (round(now - self._idle_since, 1)
                               if self._idle_since is not None else None),
                "last_decision": self._last_decision,
                "reasons": list(self._last_reasons),
                "applied_ago_s": (round(now - self._last_applied, 1)
                                  if self._last_applied is not None else None),
            }

    # ------------------------------------------------------------------ one poll
    def _reasons(self, site: dict) -> list[str]:
        listed = site.get("restart_reasons")
        reasons = [r for r in (listed if isinstance(listed, list) else []) if _safe_reason(r)]
        if site.get("restart_pending") is True and not reasons:
            reasons.append(REASON_UNSPECIFIED)
        keys = site.get("keys") if isinstance(site.get("keys"), list) else []
        row = next((k for k in keys if isinstance(k, dict)
                    and k.get("key") == DASHBOARD_PASSWORD_KEY), None)
        with self._lock:
            if row is not None and row.get("running_differs") is False:
                # The running hostapp already has the password on file: nothing to apply.
                self._queued = [r for r in self._queued if r != REASON_DASHBOARD_PASSWORD]
            local = list(self._queued)
        if row is not None and row.get("running_differs") is True:
            local.append(REASON_DASHBOARD_PASSWORD)
        for reason in local:
            if reason not in reasons:
                reasons.append(reason)
        return reasons

    def _decide(self, decision: str, reasons: list[str], *, keep_idle: bool = False) -> str:
        with self._lock:
            if not keep_idle:
                self._idle_since = None
            self._last_decision = decision
            self._last_reasons = list(reasons)
        return decision

    def tick(self) -> str:
        """One poll: maybe restart the hostapp. Returns a :data:`DECISIONS` code."""

        if not self.enabled:
            return self._decide("disabled", [])
        site = _payload(self._fetch("GET", "/api/admin/site"))
        if site is None:
            return self._decide("hostapp_unreachable", [])
        reasons = self._reasons(site)
        if not reasons:
            return self._decide("nothing_pending", [])
        if any(reason not in self._auto_reasons for reason in reasons):
            # Something a person saved is pending too: the Apply button restarts for both.
            return self._decide("manual_reasons", reasons)
        summary = _payload(self._fetch("GET", "/api/admin/summary"))
        if summary is None:
            return self._decide("hostapp_unreachable", reasons)
        if summary.get("idle") is not True:
            # The hostapp's own fail-closed predicate (no session, no QR lease, no add-on
            # mode, nothing the add-on is mid-way through); absent (an older hostapp) is busy.
            return self._decide("busy", reasons)
        now = self._clock()
        with self._lock:
            if self._idle_since is None:
                self._idle_since = now
            idle_for = now - self._idle_since
            last = self._last_applied
        if idle_for < self.idle_s:
            return self._decide("waiting_idle", reasons, keep_idle=True)
        if last is not None and now - last < self.min_interval_s:
            return self._decide("rate_limited", reasons, keep_idle=True)
        with self._lock:
            self._last_applied = now
        try:
            outcome = self._restart()
        except Exception as e:  # noqa: BLE001 - a failed restart is logged, never raised
            log.warning("auto-apply restart failed (%s)", type(e).__name__)
            outcome = {"ok": False}
        if not (isinstance(outcome, dict) and outcome.get("ok")):
            log.warning("auto-apply of %s: the hostapp restart failed (%s)",
                        " ".join(reasons),
                        (outcome or {}).get("error") if isinstance(outcome, dict) else "-")
            return self._decide("restart_failed", reasons)
        with self._lock:
            self._queued = []
        log.warning("auto-applied %s: the hostapp restarted after %.0f s idle",
                    " ".join(reasons), idle_for)
        return self._decide("applied", reasons)

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        """Poll forever (the emori-admin lifespan cancels it). Never raises out."""

        if not self.enabled:
            log.info("site auto-apply is off (%s=0): derived changes wait for Apply",
                     ENV_IDLE_S)
            return
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                decision = await asyncio.to_thread(self.tick)
            except Exception as e:  # noqa: BLE001 - one bad poll must not end the loop
                log.warning("site auto-apply poll failed (%s)", type(e).__name__)
                continue
            if decision not in ("nothing_pending", "waiting_idle", "hostapp_unreachable"):
                log.info("site auto-apply: %s", decision)


__all__ = ["AUTO_APPLY_REASONS", "DEFAULT_IDLE_S", "DECISIONS", "ENV_IDLE_S",
           "MIN_INTERVAL_S", "POLL_S", "SiteApplyWatcher", "idle_threshold_s"]
