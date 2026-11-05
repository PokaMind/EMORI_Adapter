"""This robot's Pokamind scope: a verified whoami, kept as the last-good snapshot.

The robot configures itself from its own Pokamind record. This module is how that
record reaches the disk: a whoami answer that VERIFIED (signed with this robot's token,
``status: active``, about this robot, holding this key) is written as
``configs/site/pokamind_scope.json`` (:data:`site_settings.SCOPE_FILE`, 0600) together
with the spoken menus built from it (``configs/site/auto/voice_topics.<lang>.yaml``,
``menu_builder.rebuild_auto``). What the snapshot then supplies, and to which settings,
is :mod:`emori_adapter.site_settings`'s business (:func:`site_settings.derive_from_scope`,
only into settings left unset locally, never with ``EMORI_SITE_AUTO=0``).

When it runs (each call is a signed, single-use request and Pokamind rate-limits
them, so nothing here loops freely):

* :meth:`ScopeSync.start_background` -- once ``10 s`` after the hostapp starts, then
  every 6 h; a failed sync is retried after 10 and then 30 minutes, then the 6-hour
  rhythm resumes;
* :meth:`ScopeSync.sync_now` -- the admin page's "Sync now" (at most once per
  :data:`ADMIN_MIN_INTERVAL_S`: a quicker press answers ``sync_throttled`` and asks
  nobody), and Verify, with the answer it already has;
* :meth:`ScopeSync.record_verified` -- a token being replaced on the admin page records
  the new token's scope BEFORE the token file is saved, and the deploy's token check
  does the same (``token_verify --write-scope``), so the restart that follows boots
  fully configured.

A failed or offline sync changes nothing: the last-good snapshot stays. Nothing here
restarts anything: a sync reports what changed (:class:`SyncOutcome`) to ``on_change``,
and the hostapp decides (menus are reloaded live; settings that need a restart wait
for an idle robot).

Logs carry stable codes and key NAMES only -- never a course, a pathway, a name, a
module id or any other value of the record.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from emori_adapter import site_settings
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult, fetch_whoami

log = logging.getLogger("emori.hostapp.pokamind_scope")

INITIAL_DELAY_S = 10.0
INTERVAL_S = 6 * 3600.0
RETRY_S = (600.0, 1800.0)
# The admin page's "Sync now" asks Pokamind at most this often.
ADMIN_MIN_INTERVAL_S = 30.0
THROTTLED_REASONS = frozenset({"admin_sync"})
# The menu files the builder writes, one per language.
MENU_GLOB = "voice_topics.*.yaml"

# Codes a record/sync can end with, besides every pokamind_whoami.RESULT_CODES code a
# failed fetch carries through.
RECORD_CODES = frozenset({
    "ok",
    "no_robot_token",
    "invalid_whoami",
    "robot_not_active",
    "whoami_robot_id_mismatch",
    "whoami_key_mismatch",
    "scope_write_failed",
    "scope_record_failed",
    "sync_throttled",
})
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


@dataclass(frozen=True)
class SyncOutcome:
    """What one sync did. Names and codes only, never a value of the record.

    ``changed_keys``: the settings :func:`site_settings.derive_from_scope` supplies whose
    derived value differs between the previous and the new snapshot (whether or not a
    local value overrides them). ``restart_reasons``: :func:`site_settings.restart_reasons`
    (``scope_changed`` / ``addon_provisioned``; empty with ``EMORI_SITE_AUTO=0`` or
    when every changed key is set locally). ``menus_changed``: a generated menu file was
    written, changed or removed.
    """

    ok: bool
    code: str
    reason: str = ""
    changed_keys: tuple[str, ...] = ()
    restart_reasons: tuple[str, ...] = ()
    menus_changed: bool = False
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code, "reason": self.reason,
                "changed_keys": list(self.changed_keys),
                "restart_reasons": list(self.restart_reasons),
                "menus_changed": self.menus_changed, "checked_at": self.checked_at}


@dataclass(frozen=True)
class _Recorded:
    code: str
    before: Optional[dict] = None
    after: Optional[dict] = None
    menus_changed: bool = False


def next_delay(ok: bool, attempt: int, *, interval_s: float = INTERVAL_S,
               retry_s: tuple[float, ...] = RETRY_S) -> tuple[float, int]:
    """``(seconds until the next sync, the retry index after it)``.

    A success waits ``interval_s``. A failure walks ``retry_s`` (10 min, then 30 min);
    once that ladder is used up the next wait is ``interval_s`` again, and a failure
    after it starts the ladder over. So a robot that is offline costs Pokamind at most
    ``len(retry_s) + 1`` calls per interval.
    """

    if ok:
        return float(interval_s), 0
    if attempt < len(retry_s):
        return float(retry_s[attempt]), attempt + 1
    return float(interval_s), 0


def _fingerprint(token: Any) -> Optional[str]:
    try:
        value = token.fingerprint
    except Exception:  # noqa: BLE001 - no cryptography on this box: compare ids only
        return None
    return value if isinstance(value, str) and value else None


def _default_rebuild(snapshot: dict, out_dir: Path) -> dict:
    """``menu_builder.rebuild_auto``, imported when first needed."""

    try:
        from emori_adapter.hostapp import menu_builder  # noqa: PLC0415
    except ImportError:
        log.warning("pokamind scope: menu_builder_unavailable: menus not rebuilt")
        return {}
    return menu_builder.rebuild_auto(snapshot, out_dir)


def _valid_order(order: object) -> dict[str, list[str]]:
    if not isinstance(order, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for lang, ids in order.items():
        if isinstance(lang, str) and isinstance(ids, (list, tuple)) \
                and all(isinstance(item, str) for item in ids):
            out[lang] = list(ids)
    return out


class ScopeSync:
    """Fetch, verify and record this robot's Pokamind scope. Never raises.

    ``base_url`` is the Pokamind API origin (``POKAMIND_BASE``), ``token`` this robot's
    :class:`robot_token.RobotToken` (None: nothing is ever fetched). ``fetch`` is
    :func:`pokamind_whoami.fetch_whoami`'s signature ``(base_url, token) ->
    WhoamiResult``. ``on_change(outcome)`` is called after EVERY :meth:`sync_now`
    (successful or not; an exception in it is logged and swallowed). ``clock`` stamps the
    snapshot. ``menus_dir`` and ``root`` say where the files go (relative paths resolve
    against the install root, :func:`site_settings.site_path`). ``rebuild(snapshot,
    out_dir) -> {lang: [module ids]}`` builds the menus (default:
    ``menu_builder.rebuild_auto``). ``wait(seconds) -> bool`` is the background thread's
    sleep (True = stop now); tests replace it.
    """

    def __init__(self, base_url: Any, token: Any, *,
                 fetch: Callable[..., WhoamiResult] = fetch_whoami,
                 on_change: Optional[Callable[[SyncOutcome], None]] = None,
                 clock: Callable[[], float] = time.time,
                 menus_dir: str = site_settings.AUTO_MENU_DIR,
                 root: Optional[Any] = None,
                 rebuild: Optional[Callable[[dict, Path], Any]] = None,
                 wait: Optional[Callable[[float], bool]] = None) -> None:
        self._base_url = base_url
        self._token = token
        self._fetch = fetch
        self._on_change = on_change
        self._clock = clock
        self._menus_dir = menus_dir
        self._root = root
        self._rebuild = rebuild if rebuild is not None else _default_rebuild
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self._record_lock = threading.Lock()   # one writer of the snapshot and the menus
        self._sync_lock = threading.Lock()     # one sync at a time (thread vs. admin)
        self._state_lock = threading.Lock()
        self._last: Optional[SyncOutcome] = None
        self._last_asked: Optional[float] = None   # monotonic: the last fetch
        self._monotonic = time.monotonic
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ properties
    @property
    def token(self) -> Any:
        return self._token

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def menus_path(self) -> Path:
        """Where the generated menus are written."""

        return site_settings.site_path(self._menus_dir, self._root)

    def last_outcome(self) -> Optional[SyncOutcome]:
        """The last :meth:`sync_now` outcome of this process, or None."""

        with self._state_lock:
            return self._last

    # ------------------------------------------------------------------ recording
    def _menu_files(self) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        try:
            for path in sorted(self.menus_path().glob(MENU_GLOB)):
                try:
                    found[path.name] = path.read_bytes()
                except OSError:
                    found[path.name] = b""
        except OSError:
            pass
        return found

    def _record(self, whoami: Any, token: Any) -> _Recorded:
        if token is None:
            return _Recorded("no_robot_token")
        if not isinstance(whoami, Whoami):
            return _Recorded("invalid_whoami")
        if whoami.status != "active":
            return _Recorded("robot_not_active")
        robot_id = getattr(token, "robot_id", "")
        if not robot_id or whoami.robot_id != robot_id:
            return _Recorded("whoami_robot_id_mismatch")
        fingerprint = _fingerprint(token)
        if fingerprint and whoami.key_fingerprint and whoami.key_fingerprint != fingerprint:
            return _Recorded("whoami_key_mismatch")
        with self._record_lock:
            previous = site_settings.load_scope_snapshot(robot_id, fingerprint,
                                                         root=self._root)
            snapshot = site_settings.merge_scope_snapshot(
                previous, whoami.to_dict(), contract=whoami.contract,
                key_fingerprint=fingerprint or whoami.key_fingerprint,
                key_version=whoami.key_version, now=float(self._clock()))
            menus_before = self._menu_files()
            try:
                order = _valid_order(self._rebuild(snapshot, self.menus_path()))
            except Exception as exc:  # noqa: BLE001 - the menus never block the record
                log.warning("pokamind scope: menu_rebuild_failed (%s): the menus stay as "
                            "they were", type(exc).__name__)
                order = {}
            if order:
                # A language the builder did not answer for keeps the numbers it had:
                # an error there must not renumber that menu on the next sync.
                merged = dict(snapshot.get("menu_order") or {})
                merged.update(order)
                snapshot["menu_order"] = merged
            menus_changed = self._menu_files() != menus_before
            try:
                site_settings.write_scope_snapshot(snapshot, root=self._root)
            except OSError as exc:
                log.warning("pokamind scope: scope_write_failed (%s): the last-good "
                            "snapshot stays", type(exc).__name__)
                return _Recorded("scope_write_failed", previous, None, menus_changed)
        log.info("pokamind scope recorded (contract %d; ignored keys: %s; menus %s)",
                 whoami.contract, ",".join(whoami.ignored_keys) or "-",
                 "changed" if menus_changed else "unchanged")
        return _Recorded("ok", previous, snapshot, menus_changed)

    def record(self, whoami: Any, token: Any = None) -> str:
        """Record a verified whoami as this robot's scope; the code (``ok`` on success).

        ``token`` is the token that SIGNED the whoami (default: this sync's). Refused
        unless the robot is ``active``, the answer is about ``token``'s robot, and the
        record's key fingerprint (when it names one) is the token's.
        """

        try:
            return self._record(whoami, token if token is not None else self._token).code
        except Exception as exc:  # noqa: BLE001 - recording must never break a caller
            log.warning("pokamind scope: scope_record_failed (%s)", type(exc).__name__)
            return "scope_record_failed"

    def record_verified(self, whoami: Any, token: Any = None) -> bool:
        """:meth:`record`, as True/False. Never raises."""

        return self.record(whoami, token) == "ok"

    # ------------------------------------------------------------------ syncing
    def sync_now(self, reason: str = "manual", *,
                 result: Optional[WhoamiResult] = None) -> SyncOutcome:
        """Ask Pokamind (or use ``result``, a whoami this robot's token just fetched),
        record the answer, and report what changed. Never raises; blocks up to the whoami
        timeout, so call it off an event loop."""

        reason = reason if isinstance(reason, str) and _REASON_RE.fullmatch(reason) \
            else "manual"
        if result is None and reason in THROTTLED_REASONS:
            with self._state_lock:
                asked, last = self._last_asked, self._last
            if asked is not None and self._monotonic() - asked < ADMIN_MIN_INTERVAL_S:
                log.info("pokamind scope sync (%s): sync_throttled", reason)
                return SyncOutcome(False, "sync_throttled", reason,
                                   checked_at=last.checked_at if last else 0.0)
        with self._sync_lock:
            if result is None and self._token is not None:
                with self._state_lock:
                    self._last_asked = self._monotonic()
            outcome = self._sync(reason, result)
        with self._state_lock:
            self._last = outcome
        log.info("pokamind scope sync (%s): %s; changed: %s; restart: %s; menus %s",
                 reason, outcome.code, ",".join(outcome.changed_keys) or "-",
                 ",".join(outcome.restart_reasons) or "-",
                 "changed" if outcome.menus_changed else "unchanged")
        if self._on_change is not None:
            try:
                self._on_change(outcome)
            except Exception as exc:  # noqa: BLE001 - a listener never stops the sync
                log.warning("pokamind scope: on_change failed (%s)", type(exc).__name__)
        return outcome

    def _sync(self, reason: str, result: Optional[WhoamiResult]) -> SyncOutcome:
        now = float(self._clock())
        token = self._token
        if token is None:
            return SyncOutcome(False, "no_robot_token", reason, checked_at=now)
        if result is None:
            try:
                result = self._fetch(self._base_url, token)
            except Exception:  # noqa: BLE001 - fetch_whoami never raises; an injected one may
                result = WhoamiResult(code="whoami_unreachable")
        if not isinstance(result, WhoamiResult):
            result = WhoamiResult(code="invalid_whoami_response")
        if not result.ok or result.whoami is None:
            return SyncOutcome(False, result.code, reason, checked_at=now)
        try:
            recorded = self._record(result.whoami, token)
        except Exception as exc:  # noqa: BLE001
            log.warning("pokamind scope: scope_record_failed (%s)", type(exc).__name__)
            return SyncOutcome(False, "scope_record_failed", reason, checked_at=now)
        if recorded.code != "ok":
            return SyncOutcome(False, recorded.code, reason,
                               menus_changed=recorded.menus_changed, checked_at=now)
        before = site_settings.derive_from_scope(recorded.before)
        after = site_settings.derive_from_scope(recorded.after)
        # The public keys first, then anything an add-on derives, in a stable order.
        keys = site_settings.DERIVED_KEYS + tuple(
            sorted((set(before) | set(after)) - set(site_settings.DERIVED_KEYS)))
        changed = tuple(key for key in keys if before.get(key) != after.get(key))
        try:
            reasons = tuple(site_settings.restart_reasons(recorded.before, recorded.after))
        except Exception as exc:  # noqa: BLE001
            log.warning("pokamind scope: restart_reasons failed (%s)", type(exc).__name__)
            reasons = ()
        return SyncOutcome(True, "ok", reason, changed, reasons, recorded.menus_changed, now)

    # ------------------------------------------------------------------ background
    def start_background(self, initial_delay_s: float = INITIAL_DELAY_S,
                         interval_s: float = INTERVAL_S,
                         retry_s: tuple[float, ...] = RETRY_S) -> bool:
        """Start the daemon thread. False (and nothing started) without a token, or when
        it already runs."""

        if self._token is None:
            log.info("pokamind scope: no_robot_token: no background sync")
            return False
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(float(initial_delay_s), float(interval_s),
                                    tuple(float(s) for s in retry_s)),
            name="pokamind-scope-sync", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Stop the background thread (a sync in flight finishes first)."""

        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self, initial_delay_s: float, interval_s: float,
             retry_s: tuple[float, ...]) -> None:
        delay, attempt, reason = initial_delay_s, 0, "boot"
        while not self._stop.is_set():
            try:
                if self._wait(delay):
                    return
            except Exception:  # noqa: BLE001 - a broken sleep must not spin
                return
            if self._stop.is_set():
                return
            outcome = self.sync_now(reason)
            delay, attempt = next_delay(outcome.ok, attempt, interval_s=interval_s,
                                        retry_s=retry_s)
            reason = "retry" if attempt else "periodic"
