"""The one thing the phone superadmin cannot do over HTTP or a subprocess.

``emori-admin`` (port 8070) is a SEPARATE process on purpose: it is the recovery
tool, so it must outlive the hostapp it restarts. Everything it needs -- Wi-Fi,
volume, services, logs, readycheck -- is reachable through the Reachy daemon's
REST API or ``sudo systemctl``. The kiosk language is the exception: switching it
is not a setting write but a SEQUENCE over live in-process objects (the language
state, the driver's envelope stamp, the page-reload verb the landing page is
polling for, and a line spoken through the robot's own speaker), and every one of
those objects lives inside the running hostapp.

So the hostapp exposes exactly two routes for it:

* ``GET  /api/admin/summary`` -- what the phone shows on its Language tab;
* ``POST /api/admin/lang``    -- the switch, gated on the robot being idle.

The second exception is THIS robot's own Pokamind token.
``emori-admin`` deliberately holds no robot credential (``admin/probes.py``: "the
admin service must not hold robot credentials just to draw a status dot"), so the
token is read, verified, replaced and -- behind a PIN re-entry on the phone side --
revealed HERE, in the process that already holds it:

* ``GET  /api/admin/pokamind``        -- masked status + the last whoami answer;
* ``POST /api/admin/pokamind/verify`` -- a live ``GET /api/robot/whoami`` (recorded as
  this robot's scope when it verifies);
* ``POST /api/admin/pokamind/token``  -- parse, verify the NEW token live, refuse
  while busy, record the new token's scope, write the token file atomically; the
  caller restarts this unit;
* ``POST /api/admin/pokamind/reveal`` -- the full token, logged without its value.

The third is the badge roster (the phone's People tab, ``roster_admin.py``): personal
data the hostapp already holds and serves to the badge scanner, edited here so the
running roster picks a change up at once:

* ``GET  /api/admin/roster``           -- people (name, e-mail, ref), the e-mail scope;
* ``POST /api/admin/roster/add|update|remove`` -- validated, atomic, live;
* ``POST /api/admin/roster/badge``     -- one badge as SVG (or the code as PNG);
* ``POST /api/admin/roster/sheet``     -- the A4 print view of many badges.

The fourth is the robot-local settings layer (``site_settings``) and what this robot
took from its Pokamind record. The files are written by THIS process (emori-admin
holds no credential and never writes a hostapp setting itself):

* ``GET  /api/admin/site``              -- every setting: value (never a secret's),
  where it comes from, whether the running process differs; restart reasons; the scope;
* ``POST /api/admin/site``              -- save hostapp settings (validated; a new
  ``POKAMIND_BASE`` must answer whoami signed with the current token first);
* ``POST /api/admin/scope/sync``        -- "Sync now": whoami, recorded as the scope;
* ``GET  /api/admin/menu``              -- one language's spoken menu, its source and
  the record's modules; ``POST /api/admin/menu/save|reset|preview``;
* ``POST /api/admin/results-email/test`` -- a fixed test e-mail.

An installed add-on may register routes of its own through ``extension.admin_api()``
(:class:`AdminRouteKit` hands it the gate below); they are mounted behind the same gate.

Nothing here restarts anything. A setting saved here takes effect at the next start of
the hostapp; :class:`SiteRuntime` collects WHY a restart is pending, and emori-admin
either applies it when the robot has been idle (reasons derived from Pokamind) or
waits for the operator's Apply (everything an operator entered).

All are token-gated with ``EMORI_ADMIN_TOKEN``, a shared secret between the two
processes (``deploy/robot/install.sh`` generates one). **Fail closed**: with no
token configured every route here answers 503, exactly as the management
dashboard serves 503 until ``EMORI_DASH_PASSWORD`` is set. A hostapp on a shared
network must never grow an anonymous route that can re-language the kiosk.

Every route but the language pair is, in addition, **this robot only**: it answers 403
``local_only`` to any client that is not loopback. The hostapp listens on
0.0.0.0 for the kiosk, but their one caller is emori-admin on the same box
(``admin/probes.hostapp_base`` is always 127.0.0.1) -- which is where the PIN
re-entry and the wrong-PIN limiter live. Without this, the static admin token
alone would read or overwrite the robot's credential on a shared network.
"""

# NOTE: no `from __future__ import annotations` -- FastAPI resolves handler
# annotations against MODULE globals, and `Request` is imported inside mount()'s
# closure (same as dashboard.py / speech_api.py). With the future import the
# `request: Request` parameter is unresolvable and FastAPI degrades it to a
# required QUERY parameter: every route answers 422 instead of 503/401.

import asyncio
import contextlib
import hmac
import ipaddress
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from emori_adapter import robot_token as _robot_token
from emori_adapter import site_settings
from emori_adapter.hostapp.lang_state import normalize_lang
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult, fetch_whoami

log = logging.getLogger("emori.hostapp.admin_api")

# The header emori-admin sends. Named (not Authorization) so a proxy log or a
# browser devtools screenshot makes it obvious which secret is in play.
TOKEN_HEADER = "X-EMORI-Admin-Token"
TOKEN_ENV = "EMORI_ADMIN_TOKEN"

# How long the People tab reuses a whoami answer (PokamindAdmin.current_whoami).
WHOAMI_MAX_AGE_S = 600.0
WHOAMI_RETRY_S = 60.0

# Restart reasons (SiteRuntime). emori-admin's watcher applies the Pokamind-derived ones by
# itself, after the robot has been idle long enough; the others wait for Apply. An add-on
# may add reasons of its own (any lower-case slug).
REASON_SCOPE_CHANGED = "scope_changed"
REASON_SETTINGS_SAVED = "settings_saved"
RESTART_REASONS = (REASON_SCOPE_CHANGED, REASON_SETTINGS_SAVED)
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_KEY_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_SITE_UPDATES = 64


def _is_loopback(host: object) -> bool:
    """A request from this robot itself (127.0.0.0/8, ::1, or ::ffff:127.x)."""

    try:
        address = ipaddress.ip_address(str(host or "").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped is not None and mapped.is_loopback)


def _expected_token() -> str:
    """The configured shared secret, read PER REQUEST.

    Per request and not at mount time for the same reason dashboard.py re-reads
    its password: an operator who fixes a typo in ``/opt/emori/.env`` and
    restarts the admin unit gets the new secret with no other moving parts, and
    a test can set it with ``monkeypatch.setenv``. Stripped because ``.env``
    values routinely carry trailing whitespace nobody can see.
    """

    return os.environ.get(TOKEN_ENV, "").strip()


def _equal(supplied: Any, expected: Any) -> bool:
    """Constant-time equality for two strings of ANY charset. Never raises.

    Same reasoning as ``dashboard._equal``: ``hmac.compare_digest`` refuses str
    arguments containing non-ASCII, and the supplied side is whatever a client
    puts in the header -- Starlette decodes header bytes as latin-1, so one
    crafted byte would otherwise turn every ``/api/admin/*`` request into an
    unauthenticated 500. Comparing UTF-8 bytes keeps the timing property and
    cannot raise.
    """

    try:
        return hmac.compare_digest(str(supplied).encode("utf-8", "surrogatepass"),
                                   str(expected).encode("utf-8", "surrogatepass"))
    except Exception:  # noqa: BLE001 - a comparison must never reach the client
        return False


def _token_fingerprint(token: Any) -> Optional[str]:
    try:
        value = token.fingerprint
    except Exception:  # noqa: BLE001 - crypto extras missing: compare ids only
        return None
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------- settings view
class _OtherThreadsOnly(logging.Filter):
    """Drop the records the CURRENT thread emits; every other thread logs as usual."""

    def __init__(self) -> None:
        super().__init__()
        self._ident = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread != self._ident


@contextlib.contextmanager
def _quiet(logger: logging.Logger):
    """Silence ``logger`` for this thread only (a status read must not log every poll)."""

    muted = _OtherThreadsOnly()
    logger.addFilter(muted)
    try:
        yield
    finally:
        logger.removeFilter(muted)


def _dotenv_file() -> Optional[str]:
    try:
        from emori_adapter import config  # noqa: PLC0415 - already loaded by the hostapp

        return config.DOTENV_FILE or None
    except Exception:  # noqa: BLE001 - no .env is a valid configuration
        return None


def configured_environment(environ: Optional[Mapping[str, str]] = None, *,
                           pending: Optional[Mapping[str, Optional[str]]] = None,
                           ) -> tuple[dict[str, str], dict[str, str]]:
    """``(environment, {key: source})`` the NEXT start of this process would run with.

    The settings layer applied to a COPY of this process's environment (``.env``, the
    three settings files, this robot's scope snapshot, the hardware profile), so a value
    saved on the admin page, a value an add-on measured and a newer snapshot all show before the
    restart that puts them in force. ``pending`` is a save not yet written, read as if
    it were. Never touches ``os.environ`` and logs nothing (the watcher reads this every
    minute).
    """

    env = dict(os.environ if environ is None else environ)
    with _quiet(site_settings.log):
        sources = site_settings.apply(env, dotenv_file=_dotenv_file(), pending=pending)
    return env, sources


def _has_value(key: str, value: str, placeholders: Optional[Mapping[str, str]] = None) -> bool:
    """Is ``value`` a real setting? Empty is not; neither is a key's own "unset" word."""

    if not value or not value.strip():
        return False
    word = (placeholders or {}).get(key)
    return not (word is not None and value.strip().lower() == word)


def _safe_key(key: object) -> Optional[str]:
    """A key name fit to echo back (a client's own text never is)."""

    return key if isinstance(key, str) and _KEY_NAME_RE.fullmatch(key) else None


# --------------------------------------------------------------------------- site runtime
class SiteRuntime:
    """What the hostapp knows about its site configuration while it runs.

    * WHY a restart is pending (:data:`RESTART_REASONS`): a scope sync that changed a
      derived setting, a setting saved on the admin page, an add-on's own change.
      Collected only -- nothing here restarts anything. emori-admin's
      watcher applies the Pokamind-derived ones when the robot has been idle; the rest
      wait for the operator's Apply. Cleared by the restart itself (a new process).
    * the last scope sync (code, reason, time);
    * THIS robot's scope snapshot (``token`` is the token the process runs with), re-read
      whenever the file changes, and the ``app_origin`` it names.
    """

    def __init__(self, *, token: Any = None, root: Optional[Any] = None) -> None:
        self._token = token
        self._root = root
        self._lock = threading.Lock()
        self._reasons: list[str] = []
        self._last_sync: Optional[dict] = None
        self._stamp: Optional[tuple] = None
        self._snapshot: Optional[dict] = None
        self._status = "not_read"

    @property
    def token(self) -> Any:
        return self._token

    # ----- restart reasons
    def add_reasons(self, reasons: Iterable[str]) -> None:
        with self._lock:
            for reason in reasons or ():
                if isinstance(reason, str) and _REASON_RE.fullmatch(reason) \
                        and reason not in self._reasons:
                    self._reasons.append(reason)
                    log.warning("hostapp restart pending: %s", reason)

    def reasons(self) -> list[str]:
        with self._lock:
            return list(self._reasons)

    @property
    def restart_pending(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    # ----- scope syncs
    def note_sync(self, outcome: Any) -> None:
        """Record a ``pokamind_scope.SyncOutcome`` (on_change; any thread)."""

        code = getattr(outcome, "code", None)
        reason = getattr(outcome, "reason", None)
        checked = getattr(outcome, "checked_at", None)
        with self._lock:
            self._last_sync = {
                "code": code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code)
                else "unknown",
                "reason": reason if isinstance(reason, str)
                and _REASON_RE.fullmatch(reason) else None,
                "at": float(checked) if isinstance(checked, (int, float)) else time.time(),
                "ok": getattr(outcome, "ok", False) is True,
            }
            self._stamp = None  # the snapshot may have changed: read it again
        self.add_reasons(getattr(outcome, "restart_reasons", ()) or ())

    def last_sync(self) -> Optional[dict]:
        with self._lock:
            return dict(self._last_sync) if self._last_sync is not None else None

    # ----- the snapshot
    def _file_stamp(self) -> Optional[tuple]:
        try:
            info = site_settings.site_path(site_settings.SCOPE_FILE, self._root).stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def snapshot(self) -> Optional[dict]:
        """THIS robot's last verified scope snapshot, or None. Not gated on
        ``EMORI_SITE_AUTO`` (callers that DERIVE from it check that themselves)."""

        token = self._token
        if token is None:
            return None
        stamp = self._file_stamp()
        with self._lock:
            if stamp is not None and stamp == self._stamp:
                return self._snapshot
        robot_id = getattr(token, "robot_id", "")
        fingerprint = _token_fingerprint(token)
        try:
            status = site_settings.scope_snapshot_status(robot_id, fingerprint,
                                                         root=self._root)
            snapshot = (site_settings.load_scope_snapshot(robot_id, fingerprint,
                                                          root=self._root)
                        if status == "ok" else None)
        except Exception as exc:  # noqa: BLE001 - a status read never fails a page
            log.warning("scope snapshot unreadable (%s)", type(exc).__name__)
            status, snapshot = "scope_unreadable", None
        with self._lock:
            self._stamp, self._snapshot, self._status = stamp, snapshot, status
        return snapshot

    def scope_status(self) -> str:
        """``no_token``, ``ok`` or a ``site_settings.scope_snapshot_status`` code."""

        if self._token is None:
            return "no_token"
        self.snapshot()
        with self._lock:
            return self._status

    def app_origin(self) -> Optional[str]:
        """The web-app origin this robot's record names (whoami ``app_origin``), or None.

        None with ``EMORI_SITE_AUTO=0``: every value derived from Pokamind is off then.
        """

        if not site_settings.auto_enabled():
            return None
        snapshot = self.snapshot()
        whoami = snapshot.get("whoami") if isinstance(snapshot, dict) else None
        origin = whoami.get("app_origin") if isinstance(whoami, dict) else None
        return origin if isinstance(origin, str) and origin else None


# The page-side helpers for one language's spoken menu (server.py binds them).
@dataclass(frozen=True)
class MenuAccess:
    """``source(lang)`` -> the tier label (``server.menu_source``); ``path(lang)`` -> the
    served file or None (``server._menu_source``); ``site_path(lang)`` -> where a saved
    menu goes (``server.site_menu_path``); ``load(lang)`` -> the LIVE ``VoiceMenu``;
    ``clear()`` drops the hostapp's menu cache."""

    source: Callable[[str], str]
    path: Callable[[str], Optional[str]]
    site_path: Callable[[str], Optional[str]]
    load: Callable[[str], Any]
    clear: Callable[[], None]


# A pasted token is ~110 characters; anything far longer is not one, and is refused
# before it reaches the parser (or a log line).
_MAX_PASTED_TOKEN_CHARS = 512


class PokamindAdmin:
    """The robot-side half of the phone's Pokamind tab. No route wiring, no FastAPI.

    ``load`` is the token THIS process runs with (``robot_token.configured()``, bound
    at start); ``whoami``/``save``/``read_file`` are injectable for tests. ``scope`` is
    this robot's ``pokamind_scope.ScopeSync``: a verified answer is recorded as the
    scope snapshot (Verify), and a replacement records the NEW token's scope before the
    token file is saved, so the restart that follows boots configured. Every answer is
    ``(http_status, body)`` and no body ever contains the token except the one
    :meth:`reveal` returns.
    """

    def __init__(self, *, load: Optional[_robot_token.TokenLoad] = None,
                 base_url: str = "", env_robot_id: str = "",
                 whoami: Callable[[str, Any], WhoamiResult] = fetch_whoami,
                 save: Callable[[Any], Any] = _robot_token.save_token,
                 read_file: Callable[[], _robot_token.TokenLoad] = _robot_token.load_token,
                 scope: Any = None) -> None:
        self._load = load if load is not None else _robot_token.TokenLoad()
        self._base_url = base_url or ""
        self._env_robot_id = _robot_token.robot_slug(env_robot_id or "")
        self._whoami = whoami
        self._save = save
        self._read_file = read_file
        self._scope = scope
        self._lock = threading.Lock()
        self._last: Optional[WhoamiResult] = None
        self._last_at: Optional[float] = None  # time.monotonic() of _last

    @property
    def token(self):
        return self._load.token

    @property
    def scope(self) -> Any:
        return self._scope

    def credential(self) -> str:
        """``robot_token``, or ``none`` (no token: demo sessions only)."""

        return "robot_token" if self.token is not None else "none"

    def _fingerprint(self, token) -> Optional[str]:
        try:
            return token.fingerprint
        except Exception:  # noqa: BLE001 - crypto extras missing: show the rest
            return None

    def _restart_pending(self) -> bool:
        """The token file on disk is not the token this process runs with."""

        try:
            on_disk = self._read_file()
        except Exception:  # noqa: BLE001 - an unreadable file is not a pending restart
            return False
        if on_disk.token is None:
            return False
        return on_disk.token != self.token

    def last_whoami(self) -> Optional[WhoamiResult]:
        with self._lock:
            return self._last

    def status(self) -> tuple[int, dict]:
        token = self.token
        last = self.last_whoami()
        return 200, {
            "configured": token is not None,
            "credential": self.credential(),
            "source": self._load.source,
            # Why a present token is NOT in use (a code, never the value), or null.
            "token_error": self._load.error or None,
            "robot_id": token.robot_id if token is not None else None,
            "masked": token.masked if token is not None else None,
            "fingerprint": self._fingerprint(token) if token is not None else None,
            "env_robot_id": self._env_robot_id or None,
            "mismatch": _robot_token.robot_id_conflict(token, self._env_robot_id),
            "restart_pending": self._restart_pending(),
            "last_whoami": last.to_dict() if last is not None else None,
        }

    def _sync_scope(self, result: WhoamiResult) -> bool:
        """Record a verified answer of the token in force as this robot's scope.

        ``sync_now(result=...)`` rather than a bare record: it also tells the hostapp
        (menu cache, restart reasons) what changed. Never raises.
        """

        scope = self._scope
        if scope is None or not result.ok or result.whoami is None:
            return False
        try:
            sync_now = getattr(scope, "sync_now", None)
            if callable(sync_now):
                return getattr(sync_now("admin_verify", result=result), "ok", False) is True
            return scope.record_verified(result.whoami, self.token) is True
        except Exception as exc:  # noqa: BLE001 - recording never fails the Verify
            log.warning("pokamind scope not recorded on verify (%s)", type(exc).__name__)
            return False

    def verify(self) -> tuple[int, dict]:
        token = self.token
        if token is None:
            return 409, {"error": "no_robot_token"}
        result = self._ask(token)
        with self._lock:
            self._last, self._last_at = result, time.monotonic()
        log.info("pokamind whoami for %s: %s", token.robot_id, result.code)
        recorded = self._sync_scope(result)
        return 200, {"ok": result.ok, "last_whoami": result.to_dict(),
                     "scope_recorded": recorded}

    def current_whoami(self, *, max_age_s: float = WHOAMI_MAX_AGE_S,
                       retry_s: float = WHOAMI_RETRY_S) -> WhoamiResult:
        """This robot's whoami for the People tab's e-mail check: cached, else live.

        A good answer is reused for ``max_age_s`` and a failed one for ``retry_s``, so
        a roster tab opened on a robot with no internet does not wait on Pokamind at
        every tap. A live answer is also what the Pokamind tab's "Last verified" shows.
        """

        token = self.token
        if token is None:
            return WhoamiResult(code="no_robot_token")
        with self._lock:
            last, at = self._last, self._last_at
        if last is not None and at is not None and (
                time.monotonic() - at < (max_age_s if last.ok else retry_s)):
            return last
        result = self._ask(token)
        with self._lock:
            self._last, self._last_at = result, time.monotonic()
        return result

    def _ask(self, token, base_url: Optional[str] = None) -> WhoamiResult:
        try:
            result = self._whoami(self._base_url if base_url is None else base_url, token)
        except Exception:  # noqa: BLE001 - fetch_whoami never raises; an injected one may
            return WhoamiResult(code="whoami_unreachable")
        return result if isinstance(result, WhoamiResult) else WhoamiResult(
            code="invalid_whoami_response")

    def verify_base(self, base_url: str) -> tuple[bool, str]:
        """Does ``base_url`` answer whoami signed with the CURRENT token? ``(ok, code)``.

        The gate in front of saving a new ``POKAMIND_BASE``: an origin is only accepted
        when it is this robot's Pokamind, proven by the token in force (ok, active).
        """

        token = self.token
        if token is None:
            return False, "no_robot_token"
        result = self._ask(token, base_url)
        if not result.ok or result.whoami is None:
            return False, result.code
        if result.whoami.status != "active":
            return False, "robot_not_active"
        return True, "ok"

    def _record_new_scope(self, whoami: Any, new: Any) -> bool:
        scope = self._scope
        if scope is None:
            return False
        try:
            return scope.record_verified(whoami, new) is True
        except Exception as exc:  # noqa: BLE001 - recording never blocks the replacement
            log.warning("pokamind scope of the new token not recorded (%s)",
                        type(exc).__name__)
            return False

    def _restore_scope(self) -> None:
        """After a FAILED save: put the scope of the token still in force back.

        The new token's scope was recorded first (so a successful replacement boots
        configured); a token that could not be saved leaves that snapshot -- and its
        menus -- behind for another robot. One sync with the token in force rewrites
        both, off the request thread. Nothing to do on a robot without a token.
        """

        scope, token = self._scope, self.token
        sync_now = getattr(scope, "sync_now", None)
        if token is None or not callable(sync_now):
            return

        def restore() -> None:
            try:
                sync_now("token_restore")
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning("pokamind scope not restored (%s)", type(exc).__name__)

        threading.Thread(target=restore, name="pokamind-scope-restore", daemon=True).start()

    def replace(self, raw: object, idle: Callable[[], bool]) -> tuple[int, dict]:
        """Install a NEW token: parse -> busy? -> live whoami WITH it -> busy? -> record
        its scope -> save."""

        if type(raw) is not str or len(raw) > _MAX_PASTED_TOKEN_CHARS:
            return 400, {"error": "token_invalid", "code": "token_malformed"}
        try:
            new = _robot_token.parse_token(raw)
        except _robot_token.TokenError as e:
            return 400, {"error": "token_invalid", "code": e.code}
        # Busy is checked before the network call (no point verifying for a robot that
        # will refuse anyway) AND again right before the write, because the call takes
        # seconds and a visitor can start a session in them.
        if not idle():
            return 409, {"error": "busy"}
        result = self._ask(new)
        if not result.ok or result.whoami is None:
            return 422, {"error": "token_not_verified", "code": result.code,
                         "masked": new.masked}
        if result.whoami.status != "active":
            return 422, {"error": "token_not_verified", "code": "robot_not_active",
                         "masked": new.masked}
        if not idle():
            return 409, {"error": "busy"}
        # The new token's scope (and its menus) BEFORE the token file: the restart that
        # follows then boots with the course, language and menus of the new robot.
        scope_recorded = self._record_new_scope(result.whoami, new)
        try:
            path = self._save(new)
        except Exception as e:  # noqa: BLE001 - the phone gets a code, not a traceback
            log.warning("pokamind token save failed (%s)", type(e).__name__)
            if scope_recorded:
                self._restore_scope()
            return 500, {"error": "token_save_failed"}
        previous = self.token
        # Ids and fingerprints only: the token itself never reaches the journal.
        log.warning("pokamind robot token replaced from the admin page: %s fingerprint=%s "
                    "(was %s) -> %s; restart emori-hostapp to use it",
                    new.robot_id, self._fingerprint(new),
                    previous.robot_id if previous is not None else self.credential(), path)
        return 200, {
            "saved": True,
            "restart_required": True,
            "robot_id": new.robot_id,
            "masked": new.masked,
            "fingerprint": self._fingerprint(new),
            "org": result.whoami.org,
            "robot_id_changed": previous is not None and previous.robot_id != new.robot_id,
            "env_robot_id_mismatch": _robot_token.robot_id_conflict(new, self._env_robot_id),
            "scope_recorded": scope_recorded,
        }

    def reveal(self) -> tuple[int, dict]:
        token = self.token
        if token is None:
            return 409, {"error": "no_robot_token"}
        log.warning("pokamind robot token for %s was REVEALED to the admin page "
                    "(value not logged)", token.robot_id)
        return 200, {"robot_id": token.robot_id, "token": token.reveal()}


# --------------------------------------------------------------------------- site settings
def site_status(site: SiteRuntime, *, now: Optional[float] = None,
                placeholders: Optional[Mapping[str, str]] = None) -> dict:
    """The body of ``GET /api/admin/site``. Never a secret's value.

    ``placeholders`` maps a key to the word that stands for "unset" (an add-on's choice).
    """

    env, sources = configured_environment()
    try:
        with _quiet(site_settings.log):  # read every minute: the journal has it already
            problems = site_settings.check_stored_settings()
    except Exception:  # noqa: BLE001 - a diagnostic never fails the page
        problems = {}
    keys = []
    for key, spec in site_settings.REGISTRY.items():
        value = env.get(key) or ""
        is_set = _has_value(key, value, placeholders)
        keys.append({
            "key": key,
            "owner": spec.owner,
            "value": None if spec.secret else (value if value.strip() else None),
            "set": is_set,
            "source": (sources.get(key) or site_settings.SOURCE_ENV) if is_set else None,
            "secret": spec.secret,
            # What POST /api/admin/site accepts: hostapp-owned, not measured-only.
            "editable": spec.editable and spec.owner == "hostapp",
            "restart": spec.restart,
            # Saved (or derived) but not what this process runs with: a restart applies it.
            "running_differs": (os.environ.get(key) or "") != value,
            "problem": problems.get(key),
        })
    snapshot = site.snapshot()
    stamp = now if now is not None else time.time()
    fetched = snapshot.get("fetched_at") if isinstance(snapshot, dict) else None
    whoami = snapshot.get("whoami") if isinstance(snapshot, dict) else None
    whoami = whoami if isinstance(whoami, dict) else {}
    ignored = whoami.get("ignored_keys")
    last = site.last_sync()
    scope = {
        "status": site.scope_status(),
        "auto": site_settings.auto_enabled(),
        "fetched_at": fetched if isinstance(fetched, (int, float)) else None,
        "robot_id": snapshot.get("robot_id") if snapshot else None,
        "digest": snapshot.get("digest") if snapshot else None,
        "contract": snapshot.get("contract") if snapshot else None,
        "stale": bool(isinstance(fetched, (int, float))
                      and stamp - fetched > site_settings.SCOPE_STALE_S),
        # What the record supplies (names and the record's own values, never a secret);
        # the per-key "source" says whether each is in force or overridden locally.
        "derived": site_settings.derive_from_scope(snapshot) if snapshot else {},
        "ignored_keys": [k for k in ignored if isinstance(k, str)]
        if isinstance(ignored, list) else [],
        "last_sync_code": last["code"] if last else None,
        "last_sync_at": last["at"] if last else None,
        "last_sync_reason": last["reason"] if last else None,
    }
    return {
        "keys": keys,
        "restart_pending": site.restart_pending,
        "restart_reasons": site.reasons(),
        # Saved values this process refused at its start (and so is not using).
        "invalid_running": site_settings.invalid_settings(),
        "scope": scope,
    }


def save_site_settings(payload: object, *, pokamind_admin: PokamindAdmin,
                       site: SiteRuntime) -> tuple[int, dict]:
    """``POST /api/admin/site``: save hostapp-owned, editable settings.

    ``{KEY: value | null}``; null or ``""`` removes the saved value. Validated in full
    before anything is written. A new Pokamind origin (``KeySpec.verified_origin``:
    ``POKAMIND_BASE`` and any an add-on registers) is saved without surrounding space or a
    trailing slash, and only when whoami signed with the token in force answers ok and
    active from it.
    A change of the mail server or account removes the saved SMTP password unless the
    same save brings a new one (:func:`_smtp_password_follows_server`).
    """

    if not isinstance(payload, dict) or not payload or len(payload) > _MAX_SITE_UPDATES:
        return 400, {"error": "invalid_body"}
    updates: dict[str, Optional[str]] = {}
    for key, value in payload.items():
        spec = site_settings.REGISTRY.get(key) if isinstance(key, str) else None
        if spec is None or spec.owner != "hostapp" or not spec.editable:
            code = ("key_unknown" if spec is None
                    else "key_wrong_owner" if spec.owner != "hostapp"
                    else "key_measured_only")
            return 400, {"error": "key_not_editable", "key": _safe_key(key), "code": code}
        if value is not None and not isinstance(value, str):
            return 400, {"error": "invalid_value", "key": key, "code": "value_not_text"}
        if value and spec.verified_origin:
            value = value.strip().rstrip("/")
            if not value:
                return 400, {"error": "invalid_value", "key": key, "code": "value_not_origin"}
        if value:
            code = spec.validator(value)
            if code:
                return 400, {"error": "invalid_value", "key": key, "code": code}
        updates[key] = value or None
    out_of_order = _deadline_out_of_order(updates)
    if out_of_order is not None:
        return 400, {"error": "invalid_value", "key": out_of_order,
                     "code": "value_breaks_deadline_order"}
    cleared = _smtp_password_follows_server(updates)
    for key, spec in site_settings.REGISTRY.items():
        origin = updates.get(key)
        if origin and spec.verified_origin:
            ok, code = pokamind_admin.verify_base(origin)
            if not ok:
                log.warning("%s not saved: whoami from the new origin answered %s", key, code)
                return 422, {"error": spec.verified_origin, "code": code}
    try:
        saved = site_settings.save_settings(updates, source=site_settings.SOURCE_ADMIN,
                                            owner="hostapp")
    except site_settings.SettingsError as e:
        if e.code in ("key_unknown", "key_wrong_owner"):
            return 400, {"error": "key_not_editable", "key": _safe_key(e.key),
                         "code": e.code}
        return 400, {"error": "invalid_value", "key": _safe_key(e.key), "code": e.code}
    except OSError as e:
        log.warning("site settings not saved (%s)", type(e).__name__)
        return 500, {"error": "save_failed"}
    restart_required = [key for key in saved
                        if site_settings.REGISTRY[key].restart == "hostapp"]
    if restart_required:
        site.add_reasons([REASON_SETTINGS_SAVED])
    log.warning("site settings saved from the admin page: %s (restart required: %s)",
                " ".join(saved) or "-", " ".join(restart_required) or "-")
    body: dict[str, Any] = {"saved": saved, "restart_required": restart_required}
    if cleared:
        log.warning("site settings: the saved %s was removed: the mail server or "
                    "account changed and no new password came with it", " ".join(cleared))
        body["cleared"] = cleared
    return 200, body


def _deadline_out_of_order(updates: Mapping[str, Optional[str]]) -> Optional[str]:
    """The key this save puts out of the session-start deadlines' order, or None.

    Judged on what the next start would run with (each value from wherever it comes:
    this save, ``.env``, the hardware profile, the code default), and only for an order
    this save takes part in: an older conflict elsewhere does not block an unrelated save.
    """

    if not set(updates) & site_settings.deadline_keys():
        return None
    env, _sources = configured_environment(pending=updates)
    for keys in site_settings.deadline_order_problems(env):
        for key in keys:
            if key in updates:
                return key
    return None


# POST /api/admin/results-email/test: at most this many per window, per process.
RESULTS_TEST_MAX = 5
RESULTS_TEST_WINDOW_S = 600.0

# The settings that say WHICH mail server and account a saved SMTP password belongs to.
_SMTP_SERVER_KEYS = ("EMORI_SMTP_HOST", "EMORI_SMTP_USER")


def _smtp_password_follows_server(updates: dict[str, Optional[str]]) -> list[str]:
    """Drop the SAVED SMTP password when the server or account it was for changes.

    ``EMORI_SMTP_PASS`` is write-only: nobody can read it back, the PIN holder
    included. Pointing ``EMORI_SMTP_HOST`` (or the account) elsewhere and pressing
    "Send test e-mail" would otherwise log in to the new server WITH that password. So
    a save that changes either, without a new password of its own, removes the saved
    one (``updates`` gains ``EMORI_SMTP_PASS: None``). Returns the keys cleared.
    """

    if "EMORI_SMTP_PASS" in updates:
        return []
    try:
        current, _sources = configured_environment()
        saved = site_settings.stored_values()
    except Exception:  # noqa: BLE001 - cannot tell: keep the old behaviour
        return []
    if not (saved.get("EMORI_SMTP_PASS") or "").strip():
        return []
    for key in _SMTP_SERVER_KEYS:
        new = (updates[key] or "").strip() if key in updates else None
        if new is not None and new != (current.get(key) or "").strip():
            updates["EMORI_SMTP_PASS"] = None
            return ["EMORI_SMTP_PASS"]
    return []


# --------------------------------------------------------------------------- menu
def _read_menu_file(path: Optional[str]) -> tuple[Any, bool]:
    """``(data, readable)`` of a menu file (``data`` None when absent or unreadable)."""

    if not path:
        return None, True
    try:
        import yaml  # noqa: PLC0415

        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}, True
    except Exception:  # noqa: BLE001 - unreadable or not YAML: reported, never raised
        return None, False


def _menu_body(menu: Any) -> Optional[dict]:
    """A menu reduced to what the page edits: prompt, options, default."""

    if not isinstance(menu, Mapping):
        return None
    options = menu.get("options")
    clean: dict[str, Any] = {"prompt": menu.get("prompt"), "options": {}}
    if isinstance(options, Mapping):
        for key, option in options.items():
            if isinstance(option, Mapping):
                keywords = option.get("keywords", [])
                clean["options"][str(key)] = {
                    "module_id": option.get("module_id"),
                    "name": option.get("name"),
                    "keywords": list(keywords) if isinstance(keywords, list) else [],
                }
    if menu.get("default") is not None:
        clean["default"] = menu.get("default")
    return clean


def _scope_modules(snapshot: Optional[dict]) -> Optional[dict[str, dict]]:
    """``{module_id: row}`` of the record's modules a learner can be started on."""

    whoami = snapshot.get("whoami") if isinstance(snapshot, dict) else None
    modules = whoami.get("modules") if isinstance(whoami, dict) else None
    if not isinstance(modules, list):
        return None
    return {row["module_id"]: row for row in modules
            if isinstance(row, dict) and isinstance(row.get("module_id"), str)
            and row.get("available") is True and row.get("problem") is None}


def menu_status(lang: str, *, menus: MenuAccess, site: SiteRuntime) -> dict:
    """The body of ``GET /api/admin/menu``."""

    from emori_adapter.hostapp import menu_builder, voice_topics  # noqa: PLC0415

    source = menus.source(lang)
    path = menus.path(lang)
    data, readable = _read_menu_file(path)
    if path is None:
        # The built-in menu: what the robot speaks when no file answers.
        try:
            data = menus.load(lang).model_dump()
        except Exception:  # noqa: BLE001
            data = None
    authored = None
    if isinstance(data, Mapping) and data.get("generated_from") == menu_builder.GENERATED_FROM:
        value = data.get("authored")
        authored = value if isinstance(value, str) else None
    errors = voice_topics.validate_menu_dict(data, lang) if readable else ["menu_unreadable"]
    problems = voice_topics.menu_problems(data, lang) if readable else []
    snapshot = site.snapshot()
    modules = []
    if isinstance(snapshot, dict) and isinstance(snapshot.get("whoami"), dict):
        try:
            modules = menu_builder.module_rows(snapshot["whoami"], lang,
                                               previous_order=snapshot.get("menu_order"))
        except Exception as exc:  # noqa: BLE001 - the tab still shows the menu
            log.warning("menu module rows unavailable (%s)", type(exc).__name__)
    site_path = menus.site_path(lang)
    auto_path = None
    try:
        auto_path = menu_builder.auto_menu_path(lang)
    except Exception:  # noqa: BLE001
        pass
    return {
        "lang": lang,
        "source": source,
        "menu": _menu_body(data),
        "authored": authored,
        "modules": modules,
        "errors": errors,
        "problems": problems,
        "saved": bool(site_path and os.path.exists(site_path)),
        "auto_available": bool(auto_path is not None and auto_path.exists()),
        "auto": site_settings.auto_enabled(),
        "scope_status": site.scope_status(),
    }


def save_menu(payload: object, *, menus: MenuAccess, site: SiteRuntime
              ) -> tuple[int, dict]:
    """``POST /api/admin/menu/save``: validate, scope-check, write the site file, reload."""

    from emori_adapter.hostapp import menu_builder, voice_topics  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    body = payload if isinstance(payload, dict) else {}
    lang = normalize_lang(body.get("lang"))
    target = menus.site_path(lang) if lang else None
    if not lang or not target:
        return 400, {"error": "unsupported_lang"}
    menu = body.get("menu")
    errors = voice_topics.validate_menu_dict(menu, lang)
    if errors:
        return 400, {"error": "menu_invalid", "errors": errors,
                     "problems": voice_topics.menu_problems(menu, lang)}
    clean = _menu_body(menu)
    in_scope = _scope_modules(site.snapshot())
    if in_scope is None:
        # Without this robot's verified record there is no way to know a module id is
        # one Pokamind will start: a saved menu could only fail at the session start.
        return 409, {"error": "scope_unavailable", "code": site.scope_status()}
    outside = sorted(key for key, option in clean["options"].items()
                     if option["module_id"] not in in_scope)
    if outside:
        return 400, {"error": "module_not_in_scope", "options": outside}
    text = ("# This robot's spoken menu, saved on the phone admin page.\n"
            "# It outranks the menu built from the Pokamind record; reset it on the\n"
            "# admin page to go back to that one.\n"
            + yaml.safe_dump(clean, allow_unicode=True, sort_keys=False,
                             default_flow_style=False, width=88))
    try:
        menu_builder.atomic_write_text(target, text)
    except OSError as e:
        log.warning("menu %s not saved (%s)", lang, type(e).__name__)
        return 500, {"error": "save_failed"}
    menus.clear()
    log.warning("spoken menu %s saved from the admin page (%d option(s))", lang,
                len(clean["options"]))
    return 200, {"saved": True, "lang": lang, "source": menus.source(lang)}


def reset_menu(payload: object, *, menus: MenuAccess) -> tuple[int, dict]:
    """``POST /api/admin/menu/reset``: remove the saved site menu, reload."""

    body = payload if isinstance(payload, dict) else {}
    lang = normalize_lang(body.get("lang"))
    target = menus.site_path(lang) if lang else None
    if not lang or not target:
        return 400, {"error": "unsupported_lang"}
    try:
        os.remove(target)
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as e:
        log.warning("menu %s not reset (%s)", lang, type(e).__name__)
        return 500, {"error": "reset_failed"}
    menus.clear()
    if removed:
        log.warning("spoken menu %s reset from the admin page", lang)
    return 200, {"reset": removed, "lang": lang, "source": menus.source(lang)}


# --------------------------------------------------------------------------- routes
@dataclass(frozen=True)
class AdminRouteKit:
    """What an add-on's admin routes are built from (``extension.admin_api().register``).

    ``guard(request)`` is the gate of the credential routes: ``None`` when the request is
    loopback and carries the admin token, else the response to return. ``answer`` turns a
    ``(status, body)`` pair into a JSON response (``no_store`` adds ``Cache-Control``).
    ``idle()`` is the fail-closed "nothing is running" predicate. ``status()`` is the
    add-on's own already-sanitised status projection (``None`` when it has none).
    ``site`` is the :class:`SiteRuntime` (restart reasons, the scope snapshot) and
    ``settings_loader`` returns the running ``config.Settings``.
    """

    guard: Callable[[Any], Any]
    answer: Callable[..., Any]
    idle: Callable[[], bool]
    status: Callable[[], Optional[dict]]
    site: SiteRuntime
    settings_loader: Callable[[], Any]


def mount(app, *, lang_state, driver, mic_profile, robot_idle, set_lang,
          addon_status=None, extension=None, speak=None,
          pokamind: Optional[PokamindAdmin] = None,
          roster: Any = None, scope: Any = None, site: Optional[SiteRuntime] = None,
          menus: Optional[MenuAccess] = None,
          settings_loader: Optional[Callable[[], Any]] = None) -> None:
    """Attach the ``/api/admin/*`` routes.

    Called from ``server.py:_build_app()`` right after ``dashboard.mount(...)``.

    ``set_lang`` is ``server.set_kiosk_language`` -- the ONE implementation of
    the switch sequence, shared with the hidden head-gesture toggle. It is
    injected rather than rebuilt here because ``lang_state.set()`` alone is not
    a language switch: without the driver stamp, the reload verb and the spoken
    confirmation the kiosk keeps serving the old language until it restarts.

    ``robot_idle`` is the strictest idle predicate the hostapp has (a live
    training session, a QR lease, or any add-on mode all answer False); it
    gates the switch, because re-languaging mid-anything swaps the menu, the
    pages and the session claim under a person who is already talking.

    ``addon_status`` is optional and read for the summary's ``addon_mode``
    (the same already-sanitised projection the dashboard gets). ``extension`` is the
    hostapp's :class:`~emori_adapter.hostapp.extensions.HostExtension`: what its
    ``admin_api()`` returns (``None`` for no add-on) registers the add-on's own routes
    (``register(app, kit)``), may veto a restart (``busy()``) and may name the word that
    stands for "unset" per setting (``unset_values()``). ``speak(text, lang)`` is the
    robot's own voice, used ONLY by the menu
    preview: the language switch's spoken confirmation lives inside ``set_lang``, and
    saying it a second time here would duplicate a sequence that must exist once.

    ``pokamind`` is this robot's :class:`PokamindAdmin` (server.py builds it from the
    token the process bound at start). ``None`` mounts one with no token, so the
    Pokamind routes still answer honestly ("none") instead of 404.

    ``roster`` is the People tab's ``roster_admin.RosterAdmin`` (the live roster, its
    file, this robot's whoami). ``None`` mounts no roster routes: they 404, which the
    phone reads as "this robot runs an older build".

    ``scope`` is this robot's ``pokamind_scope.ScopeSync`` ("Sync now"); ``site`` its
    :class:`SiteRuntime` (a fresh one when None); ``menus`` the spoken-menu helpers
    (no menu routes without them); ``settings_loader`` returns the running
    ``config.Settings`` (default ``config.load_settings``).
    """

    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    pokamind_admin = pokamind if pokamind is not None else PokamindAdmin()
    site_runtime = site if site is not None else SiteRuntime(token=pokamind_admin.token)
    if settings_loader is None:
        def settings_loader():
            from emori_adapter.config import load_settings  # noqa: PLC0415

            return load_settings()
    try:
        addon_api = extension.admin_api() if extension is not None else None
    except Exception:  # noqa: BLE001 - a broken add-on never takes the admin routes down
        log.warning("the add-on's admin routes are unavailable", exc_info=True)
        addon_api = None
    test_lock = threading.Lock()
    # "Send test e-mail" logs in to a mail server: a few per window, never a loop (and
    # never a way to probe the site LAN host by host).
    test_times: list[float] = []

    def _guard(request):
        """``None`` = allowed; otherwise the response to return immediately."""

        expected = _expected_token()
        if not expected:
            # Fail closed. Not 401: "not configured" and "wrong secret" are
            # different operator problems and the phone renders different help.
            return JSONResponse({"error": "admin_not_configured"}, status_code=503)
        supplied = (request.headers.get(TOKEN_HEADER, "") or "").strip()
        if not _equal(supplied, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def _pokamind_guard(request):
        """``_guard`` for the credential routes, refusing anything not on this robot.

        Checked FIRST, so a LAN client learns nothing -- not even whether the admin
        token is configured -- from a route that can return or replace the token.
        """

        client = getattr(request, "client", None)
        if not _is_loopback(getattr(client, "host", None)):
            return JSONResponse({"error": "local_only"}, status_code=403)
        return _guard(request)

    def _current_lang() -> str:
        try:
            return lang_state.current()
        except Exception:  # noqa: BLE001 - a diagnostic must not 500 the phone
            return ""

    def _allowed_langs() -> list[str]:
        allowed = getattr(lang_state, "allowed", None)
        try:
            return [code for code in allowed if isinstance(code, str)] \
                if allowed is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _lang_allowed(code: str) -> bool:
        check = getattr(lang_state, "is_allowed", None)
        if not callable(check):
            return True
        try:
            return bool(check(code))
        except Exception:  # noqa: BLE001
            return False

    def _session_active() -> bool:
        tracker = getattr(driver, "tracker", None)
        return bool(tracker is not None and getattr(tracker, "active", False))

    def _addon_projection() -> Optional[dict]:
        if not callable(addon_status):
            return None
        try:
            status = addon_status()
        except Exception:  # noqa: BLE001 - an unreadable add-on is reported as null
            return None
        return status if isinstance(status, dict) else None

    def _addon_mode() -> Optional[str]:
        status = _addon_projection()
        mode = status.get("mode") if isinstance(status, dict) else None
        return mode if isinstance(mode, str) else None

    def _idle() -> bool:
        """Fail CLOSED: an unreadable state counts as busy, never as idle."""

        if not callable(robot_idle):
            return False
        try:
            return bool(robot_idle())
        except Exception:  # noqa: BLE001 - same posture as server._robot_idle
            return False

    def _restart_safe() -> bool:
        """Whether a host-app restart now would interrupt nobody and nothing.

        :func:`_idle` (no session, no QR training lease, no add-on mode -- anything
        unreadable is busy) and nothing the add-on is in the middle of (it may veto
        through ``busy()``; one that cannot say is busy): a restart then would kill
        work that has state to put back.
        """

        if not _idle():
            return False
        if addon_api is None:
            return True
        veto = getattr(addon_api, "busy", None)
        if not callable(veto):
            return True
        try:
            return not veto()
        except Exception:  # noqa: BLE001 - cannot tell: busy
            return False

    def _unset_values() -> Mapping[str, str]:
        reader = getattr(addon_api, "unset_values", None)
        try:
            values = reader() if callable(reader) else None
        except Exception:  # noqa: BLE001 - a setting then reads as set, never a 500
            return {}
        return values if isinstance(values, Mapping) else {}

    def _answer(outcome: tuple[int, dict], *, no_store: bool = False):
        status, body = outcome
        response = JSONResponse(body, status_code=status)
        if no_store:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/admin/summary")
    def admin_summary(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        return {
            "lang": _current_lang(),
            # The languages the Language tab may offer: this robot's Pokamind scope,
            # in its preference order (every shipped language without one).
            "allowed_langs": _allowed_langs(),
            # None = wpctl never took (a dev box, or an audio stack still coming
            # up); the phone renders that as "unknown" rather than a level.
            "mic_profile": getattr(mic_profile, "profile", None),
            "session_active": _session_active(),
            "addon_mode": _addon_mode(),
            # The ONE answer to "may the host app restart now?" (emori-admin's Apply
            # button and its unattended auto-apply): the strictest predicate this
            # process has, fail-closed (_restart_safe).
            "idle": _restart_safe(),
        }

    @app.post("/api/admin/lang")
    async def admin_lang(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
            body = None
        target = normalize_lang(body.get("lang") if isinstance(body, dict) else None)
        if not target or not _lang_allowed(target):
            # normalize_lang only accepts the languages this robot actually ships a
            # voice, a menu and a Vosk model for; the language state refuses one this
            # robot's Pokamind scope does not offer (every session in it would be
            # refused) -- and set_kiosk_language swallows that refusal, so it is
            # answered HERE instead of as an "ok" in the old language.
            return JSONResponse({"error": "unsupported_lang"}, status_code=400)
        current = _current_lang()
        if target == current:
            # No-op FIRST, before the idle gate: nothing is written, nothing is
            # reloaded and nothing is spoken, so there is no state to protect --
            # and the phone's Language tab highlights the current language as a
            # button. Tapping the one that is already lit must never answer
            # "busy" during a session; that reads as "the robot is broken" to an
            # operator who did not ask for a change at all.
            return {"ok": True, "lang": current}
        if not _idle():
            return JSONResponse({"error": "busy"}, status_code=409)
        try:
            switched = set_lang(target)
        except Exception as e:  # noqa: BLE001 - the phone gets an error, not a traceback
            log.warning("admin language switch to %s failed (%s: %s)",
                        target, type(e).__name__, e)
            return JSONResponse({"error": "lang_switch_failed"}, status_code=500)
        return {"ok": True, "lang": normalize_lang(switched) or _current_lang()}

    @app.get("/api/admin/pokamind")
    def admin_pokamind(request: Request):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        return _answer(pokamind_admin.status())

    @app.post("/api/admin/pokamind/verify")
    def admin_pokamind_verify(request: Request):
        # Sync on purpose: the whoami call blocks for up to its timeout, and FastAPI
        # runs a sync handler in the threadpool instead of on the event loop.
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        return _answer(pokamind_admin.verify())

    @app.post("/api/admin/pokamind/token")
    def admin_pokamind_token(request: Request, payload: Optional[dict] = None):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        raw = payload.get("token") if isinstance(payload, dict) else None
        # The same idle rule as the language switch: replacing the credential restarts
        # the hostapp and the kiosk, which must never happen under a visitor.
        return _answer(pokamind_admin.replace(raw, _idle))

    @app.post("/api/admin/pokamind/reveal")
    def admin_pokamind_reveal(request: Request):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        return _answer(pokamind_admin.reveal(), no_store=True)

    # ---------------------------------------------------------------- site settings
    # Loopback + the admin token, like the token routes: a saved value can redirect
    # the robot's Pokamind origin or a trust anchor, and the PIN re-entry
    # for those lives in emori-admin. Values of secret settings never leave here.
    @app.get("/api/admin/site")
    def admin_site(request: Request):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        return _answer((200, site_status(site_runtime, placeholders=_unset_values())),
                       no_store=True)

    @app.post("/api/admin/site")
    def admin_site_save(request: Request, payload: Optional[dict] = None):
        # Sync: a new POKAMIND_BASE is checked with a live whoami first.
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        return _answer(save_site_settings(payload, pokamind_admin=pokamind_admin,
                                          site=site_runtime), no_store=True)

    @app.post("/api/admin/scope/sync")
    async def admin_scope_sync(request: Request):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        sync_now = getattr(scope, "sync_now", None)
        if not callable(sync_now):
            return JSONResponse({"error": "scope_unavailable"}, status_code=503)
        # Off the event loop: it blocks for up to the whoami timeout.
        outcome = await asyncio.to_thread(sync_now, "admin_sync")
        to_dict = getattr(outcome, "to_dict", None)
        if not callable(to_dict):
            return JSONResponse({"error": "scope_unavailable"}, status_code=503)
        return _answer((200, to_dict()), no_store=True)

    # ---------------------------------------------------------------- the spoken menu
    if menus is not None:
        def _menu_lang(raw: object) -> str:
            return normalize_lang(raw) or (_current_lang() if not raw else "")

        @app.get("/api/admin/menu")
        def admin_menu(request: Request, lang: str = ""):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            code = _menu_lang(lang)
            if not code:
                return JSONResponse({"error": "unsupported_lang"}, status_code=400)
            return _answer((200, menu_status(code, menus=menus, site=site_runtime)),
                           no_store=True)

        @app.post("/api/admin/menu/save")
        def admin_menu_save(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(save_menu(payload, menus=menus, site=site_runtime))

        @app.post("/api/admin/menu/reset")
        def admin_menu_reset(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(reset_menu(payload, menus=menus))

        @app.post("/api/admin/menu/preview")
        def admin_menu_preview(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            body = payload if isinstance(payload, dict) else {}
            code = _menu_lang(body.get("lang"))
            if not code:
                return JSONResponse({"error": "unsupported_lang"}, status_code=400)
            if not callable(speak):
                return JSONResponse({"error": "speech_unavailable"}, status_code=503)
            # The robot's own speaker: never under a visitor (same gate as the switch).
            if not _idle():
                return JSONResponse({"error": "busy"}, status_code=409)
            if "menu" in body:
                from emori_adapter.hostapp import voice_topics  # noqa: PLC0415

                errors = voice_topics.validate_menu_dict(body.get("menu"), code)
                if errors:
                    return JSONResponse({"error": "menu_invalid", "errors": errors},
                                        status_code=400)
                prompt = body["menu"]["prompt"]
            else:
                try:
                    prompt = menus.load(code).prompt
                except Exception as e:  # noqa: BLE001 - an unloadable menu is a code
                    log.warning("menu preview %s: menu unloadable (%s)", code,
                                type(e).__name__)
                    return JSONResponse({"error": "menu_unreadable"}, status_code=409)
            # Off the request thread: the robot's voice blocks for the prompt's length.
            threading.Thread(target=speak, args=(prompt, code), name="menu-preview-speak",
                             daemon=True).start()
            return {"spoken": True, "lang": code}

    # ---------------------------------------------------------------- results e-mail
    @app.post("/api/admin/results-email/test")
    def admin_results_email_test(request: Request, payload: Optional[dict] = None):
        denied = _pokamind_guard(request)
        if denied is not None:
            return denied
        from emori_adapter.hostapp import results_email  # noqa: PLC0415

        to = payload.get("to") if isinstance(payload, dict) else None
        if not results_email.is_email(to):
            return JSONResponse({"sent": False, "code": "recipient_invalid"},
                                status_code=400)
        if not test_lock.acquire(blocking=False):
            return JSONResponse({"sent": False, "code": "test_busy"}, status_code=409)
        now = time.monotonic()
        test_times[:] = [t for t in test_times if now - t < RESULTS_TEST_WINDOW_S]
        if len(test_times) >= RESULTS_TEST_MAX:
            test_lock.release()
            return JSONResponse({"sent": False, "code": "test_rate_limited"},
                                status_code=429)
        test_times.append(now)
        try:
            # The settings a restart would use: test the SMTP values before applying them.
            env, _sources = configured_environment()
            sent, code = results_email.send_test(to, environ=env)
        finally:
            test_lock.release()
        return {"sent": sent, "code": code}

    # ---------------------------------------------------------------- add-on routes
    # Behind the same gate as the routes above: the add-on gets the guard, never its own.
    if addon_api is not None and callable(getattr(addon_api, "register", None)):
        try:
            addon_api.register(app, AdminRouteKit(
                guard=_pokamind_guard, answer=_answer, idle=_idle, status=_addon_projection,
                site=site_runtime, settings_loader=settings_loader))
        except Exception:  # noqa: BLE001 - the core routes stay up without the add-on's
            log.warning("the add-on's admin routes were not registered", exc_info=True)

    # ---------------------------------------------------------------- roster
    # The People tab. The roster is personal data (names, e-mail addresses, and the
    # badge ids that log a person in), so these routes are guarded exactly like the
    # token routes: loopback only + the admin token, the PIN being emori-admin's.
    # Handlers are sync: an add may ask Pokamind's whoami (seconds) and FastAPI runs a
    # sync handler in the threadpool, off the event loop the kiosk depends on.
    if roster is not None:
        def _file(outcome: tuple[int, Any, str]):
            status, body, media_type = outcome
            if not isinstance(body, (bytes, bytearray)):
                return _answer((status, body), no_store=True)
            response = Response(content=bytes(body), status_code=status,
                                media_type=media_type)
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/api/admin/roster")
        def admin_roster(request: Request):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(roster.list(), no_store=True)

        @app.post("/api/admin/roster/add")
        def admin_roster_add(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(roster.add(payload), no_store=True)

        @app.post("/api/admin/roster/update")
        def admin_roster_update(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(roster.update(payload), no_store=True)

        @app.post("/api/admin/roster/remove")
        def admin_roster_remove(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _answer(roster.remove(payload), no_store=True)

        @app.post("/api/admin/roster/badge")
        def admin_roster_badge(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _file(roster.badge(payload))

        @app.post("/api/admin/roster/sheet")
        def admin_roster_sheet(request: Request, payload: Optional[dict] = None):
            denied = _pokamind_guard(request)
            if denied is not None:
                return denied
            return _file(roster.sheet(payload))

    if _expected_token():
        log.info("hostapp admin API mounted at /api/admin/*")
    else:
        # WARNING, not INFO: under uvicorn the hostapp's root logger filters
        # INFO, and "the phone cannot switch the language" is otherwise a silent
        # 503 on an appliance with no screen to show it on.
        log.warning("hostapp admin API mounted but EMORI_ADMIN_TOKEN is unset: every "
                    "/api/admin/* request will answer 503 - set it in /opt/emori/.env "
                    "(deploy/robot/install.sh generates one) and restart both units")
