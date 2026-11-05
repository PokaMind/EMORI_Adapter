"""PIN auth for the standalone ``emori-admin`` service.

This is a deliberate COPY of ``hostapp/dashboard.py``'s auth block, not an
import of it: importing that module drags the whole hostapp (metrics store,
dashboard page, translator) into the admin process, and the ONE promise this
service makes is that it keeps working when the hostapp does not.  Two ~60-line
copies of an HMAC cookie is a cheaper price than that coupling.

What is kept identical (and why):

* **Fail closed.**  ``EMORI_ADMIN_PIN`` unset -> the whole surface answers 503
  "not configured".  There is no anonymous mode; the robot ships with the key
  unset, exactly like ``EMORI_DASH_PASSWORD``.
* **Constant-time compares via** :func:`_equal`, never ``hmac.compare_digest``
  directly: compare_digest refuses ``str`` with non-ASCII, and one crafted
  latin-1 cookie byte would otherwise turn every request into a 500.
* **12 h HMAC cookie**, keyed by PIN *and* salt, so the PIN is never in the
  cookie and rotating either invalidates every outstanding session.
* **Two-level rate limit** (per-IP under a global ceiling) — see
  :class:`LoginLimiter`.

What is different: the secret is a **6-digit PIN**, i.e. a 10^6 keyspace typed
on a phone keypad.  That is only safe *because* of the global ceiling below:
60 guesses/minute exhausts 10^6 in ~11 days no matter how many source
addresses an attacker owns.  A PIN that is not exactly 6 digits is therefore
treated as MISCONFIGURED (503 + a loud log), never as "a shorter password" —
silently accepting ``1234`` would quietly cut that number to 100 seconds.

The PIN can be changed from the page (:func:`change_pin`, the current PIN required): the
new one goes to ``secrets/admin.env``, which outranks ``.env`` (the robot-local settings
layer, :mod:`emori_adapter.site_settings`).

Env:
  ``EMORI_ADMIN_PIN``   the 6-digit PIN (empty/malformed -> 503, no service)
  ``EMORI_ADMIN_SALT``  cookie-HMAC salt (empty -> a generated, persisted random
                        salt beside the admin state file + warning; it must be
                        HIGH-ENTROPY, see :func:`_persisted_fallback_salt`)
  ``EMORI_ROBOT_NAME``  display name on the pages (falls back to EMORI_ROBOT_ID)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Optional

from emori_adapter.admin import state

log = logging.getLogger("emori.admin.auth")

ENV_PIN = "EMORI_ADMIN_PIN"
ENV_SALT = "EMORI_ADMIN_SALT"

COOKIE_NAME = "emori_admin"
COOKIE_TTL_S = 12 * 3600
# Clock skew guard: a cookie stamped in the future is a forged/replayed one.
COOKIE_FUTURE_SLACK_S = 120.0

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 60.0
# Ceiling across ALL clients in the same window (see :class:`LoginLimiter`).
LOGIN_MAX_FAILURES_GLOBAL = 60

# Exactly six digits. Not ``\d`` under re.UNICODE semantics by accident: ``\d``
# also matches the digits of other scripts, which no phone keypad on site
# will ever produce and which would make two visually different PINs
# compare unequal for reasons nobody could debug on a screenless robot.
PIN_RE = re.compile(r"[0-9]{6}")

# Reasons the surface refuses to serve, as stable slugs for the UI/logs.
NOT_CONFIGURED = "admin_not_configured"
PIN_INVALID = "admin_pin_invalid"

_salt_warned = False
# One read of the generated fallback salt per process: it is consulted on every
# authenticated request and cannot change without a restart.
_fallback_salt_cache = ""


def configured_pin() -> tuple[str, str]:
    """``(pin, problem)``.  ``problem == ""`` means the surface may serve.

    Never raises and never logs the PIN itself.
    """

    value = os.environ.get(ENV_PIN, "").strip()
    if not value:
        return "", NOT_CONFIGURED
    if PIN_RE.fullmatch(value) is None:
        return "", PIN_INVALID
    return value, ""


def save_admin_secrets(updates: dict, *, root: Any = None) -> list[str]:
    """Write ``updates`` into ``secrets/admin.env`` (merged, atomic, 0600). Returns keys.

    The robot-local settings layer owns the file and its rules
    (:func:`emori_adapter.site_settings.save_settings` with ``owner="admin"``): every key
    must be one emori-admin writes and every value must pass that key's validator, or
    nothing is written (``SettingsError``). An ``OSError`` propagates. The file is read
    by both units at their next start (``EnvironmentFile=``) and outranks ``.env``.

    Imported lazily: the recovery tool must start even if that layer cannot be imported.
    """

    from emori_adapter import site_settings  # noqa: PLC0415 - see the docstring

    return site_settings.save_settings(updates, source=site_settings.SOURCE_ADMIN,
                                       owner="admin", root=root)


# What change_pin answers, besides "ok".
PIN_CHANGE_CODES = ("bad_pin", "pin_invalid", "pin_unchanged", "pin_save_failed")


def change_pin(current: Any, new: Any, *, root: Any = None) -> tuple[bool, str]:
    """Replace the admin PIN: ``(ok, code)``, code ``ok`` or one of :data:`PIN_CHANGE_CODES`.

    ``current`` must be the PIN in force (``bad_pin`` otherwise, compared in constant
    time; the caller counts it against the login limiter). ``new`` must be exactly six
    digits (``pin_invalid``) and differ from it (``pin_unchanged``). The new PIN is
    written to ``secrets/admin.env``, merged with what that file already holds, and put
    in force in this process at once; the caller restarts the service so every reader
    starts from the file. Neither PIN is ever logged.
    """

    pin, problem = configured_pin()
    supplied = current if isinstance(current, str) else ""
    if problem or PIN_RE.fullmatch(supplied) is None or not _equal(supplied, pin):
        return False, "bad_pin"
    if not isinstance(new, str) or PIN_RE.fullmatch(new) is None:
        return False, "pin_invalid"
    if _equal(new, pin):
        return False, "pin_unchanged"
    try:
        save_admin_secrets({ENV_PIN: new}, root=root)
    except (ValueError, OSError) as e:  # site_settings.SettingsError is a ValueError
        log.warning("admin PIN not changed: %s could not be written (%s)",
                    "secrets/admin.env", type(e).__name__)
        return False, "pin_save_failed"
    # In force now: every session cookie was signed with the old PIN and stops working.
    os.environ[ENV_PIN] = new
    log.warning("admin PIN changed from the admin page; every phone signs in again")
    return True, "ok"


def _fallback_salt_path() -> str:
    """Where the generated fallback salt lives: beside the admin state file."""

    state_file = (os.environ.get(state.ENV_STATE_FILE, "").strip()
                  or state.DEFAULT_STATE_FILE)
    return os.path.join(os.path.dirname(state_file) or ".", "admin_cookie_salt")


def _persisted_fallback_salt() -> str:
    """A random 256-bit salt on disk, minted once.  ``""`` when impossible.

    This is what keeps a captured cookie from being an OFFLINE ORACLE FOR THE
    PIN.  The cookie is ``issued.HMAC(HMAC(salt, pin), issued)`` and the PIN is
    six digits by mandate, so an attacker who knows the salt tries all 10^6 in
    about a second on a laptop and recovers the PIN itself — which survives
    cookie expiry, logout, reboot and re-entry, and defeats the whole rate-limit
    argument this module's docstring rests on.  A salt derived from the robot id
    (which the unauthenticated login page prints) is exactly "known".

    Random but PERSISTED: a per-boot salt would sign every operator out every
    time they restart the service they restart things with.  Never raises; a
    read-only filesystem degrades to the old derivation with a loud warning
    rather than taking the recovery tool off the air.
    """

    global _fallback_salt_cache
    if _fallback_salt_cache:
        return _fallback_salt_cache
    path = _fallback_salt_path()
    minted = secrets.token_hex(32)
    for attempt in (0, 1):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
            if len(existing) >= 32:
                _fallback_salt_cache = existing
                return existing
        except OSError:
            pass
        if attempt:  # the file is there but unusable; do not spin
            return ""
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # O_EXCL + 0600: two processes racing at boot must not each mint a
            # salt and invalidate the other's cookies.
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(minted + "\n")
            _fallback_salt_cache = minted
            return minted
        except FileExistsError:
            continue  # somebody else won the race; read theirs
        except OSError:
            return ""
    return ""


def admin_salt() -> str:
    """The cookie-HMAC salt.  ``EMORI_ADMIN_SALT`` is the real answer.

    Unset, we mint and persist a random one (see
    :func:`_persisted_fallback_salt`) and warn ONCE.  Only when even that is
    impossible — a read-only or unwritable state directory — do we fall back to
    the deterministic ``EMORI_ROBOT_ID`` derivation, which is stable but
    guessable and therefore makes a captured cookie crackable back to the PIN.
    """

    global _salt_warned
    configured = os.environ.get(ENV_SALT, "").strip()
    if configured:
        return configured
    persisted = _persisted_fallback_salt()
    if not _salt_warned:
        _salt_warned = True
        if persisted:
            log.warning(
                "%s is unset; using the generated salt in %s (stable across "
                "restarts). Set %s in /opt/emori/.env on any deployed robot.",
                ENV_SALT, _fallback_salt_path(), ENV_SALT)
        else:
            log.warning(
                "%s is unset AND %s could not be written: the cookie key falls "
                "back to a GUESSABLE robot-id derivation, which lets anyone who "
                "captures one cookie recover the 6-digit PIN offline. Set %s in "
                "/opt/emori/.env and restart emori-admin.",
                ENV_SALT, _fallback_salt_path(), ENV_SALT)
    return persisted or ("emori-admin-fallback:"
                         + os.environ.get("EMORI_ROBOT_ID", "").strip())


def robot_name() -> str:
    return (os.environ.get("EMORI_ROBOT_NAME", "").strip()
            or os.environ.get("EMORI_ROBOT_ID", "").strip()
            or "EMORI")


def _cookie_key(pin: str, salt: str) -> bytes:
    """The HMAC key the session cookie is signed with.

    Derived from the PIN AND the salt so that (a) the cookie value never
    contains the PIN, (b) changing either invalidates every outstanding cookie,
    and (c) two robots that happen to share a PIN do not accept each other's
    cookies.  One HMAC, not PBKDF2: nothing here is a stored password hash —
    the secret only ever lives in memory from the environment and this key is
    recomputed per request.

    **The salt carries the entropy here, and that is load-bearing.**  The PIN
    side of this key is a mandated 10^6 keyspace, so a signed cookie plus a
    KNOWN salt is an offline oracle: try every PIN, keep the one whose tag
    matches, and the attacker has the credential itself rather than a session
    that expires.  :func:`admin_salt` therefore never returns a value an
    outsider can derive unless the robot's filesystem refuses to keep one.
    """

    return hmac.new(("emori-admin-cookie-v1|" + salt).encode("utf-8"),
                    pin.encode("utf-8"), hashlib.sha256).digest()


def _equal(supplied: Any, expected: Any) -> bool:
    """Constant-time equality for two strings of ANY charset.  Never raises.

    ``hmac.compare_digest`` refuses str arguments containing non-ASCII, and the
    supplied side here is whatever a client sends — Starlette decodes header
    bytes as latin-1, so one crafted byte would otherwise turn every request
    into an unauthenticated 500 on the tool an operator reaches for when
    everything else is already broken.  Comparing UTF-8 bytes keeps the timing
    property and cannot raise.
    """

    try:
        return hmac.compare_digest(str(supplied).encode("utf-8", "surrogatepass"),
                                   str(expected).encode("utf-8", "surrogatepass"))
    except Exception:  # noqa: BLE001 - a comparison must never reach the client
        return False


# Public name for the other admin modules: nothing outside this file may reach
# for ``hmac.compare_digest`` directly (see the docstring above for why).
constant_time_equal = _equal


def _sign(key: bytes, issued_at: int) -> str:
    # 128 bits of tag is far past what a LAN/AP admin page needs and keeps the
    # cookie short enough to read in a phone browser's inspector while debugging.
    return hmac.new(key, str(issued_at).encode("ascii"), hashlib.sha256).hexdigest()[:32]


def make_cookie(pin: str, salt: str, *, now: Optional[float] = None) -> str:
    issued = int(now if now is not None else time.time())
    return "%d.%s" % (issued, _sign(_cookie_key(pin, salt), issued))


def cookie_valid(value: str, pin: str, salt: str, *,
                 now: Optional[float] = None, ttl_s: float = COOKIE_TTL_S) -> bool:
    """Constant-time signature check + 12 h expiry.  Never raises."""

    if not pin or not value or "." not in value:
        return False
    stamp, _, signature = value.partition(".")
    try:
        issued = int(stamp)
        expected = _sign(_cookie_key(pin, salt), issued)
    except Exception:  # noqa: BLE001 - a hostile cookie is a 401, never a 500
        return False
    if not _equal(signature, expected):
        return False
    seconds = float(now if now is not None else time.time())
    return -COOKIE_FUTURE_SLACK_S <= (seconds - issued) <= ttl_s


class LoginLimiter:
    """Per-IP failure counter (5/minute) UNDER a global ceiling (60/minute).

    The per-IP bucket alone is not a limit at all on the network this ships
    onto: an open AP the operator's phone joins, or an unfirewalled site LAN.
    An attacker adds 500 secondary addresses to their interface and gets 2,500
    guesses a minute at a SIX-DIGIT PIN — the whole keyspace in under seven
    hours.  The global ceiling makes the total rate independent of how many
    source addresses the attacker owns, which is what keeps a 10^6 keyspace
    defensible at all.

    The tradeoff is explicit: while an attack is in progress a legitimate
    operator who mistypes can be told to wait too.  60 failures/minute is far
    above anything a human at a phone produces, the block lifts after one
    window, and being briefly rate-limited beats handing over reboot,
    Wi-Fi and service control of a robot.

    In-memory on purpose — one admin process, and a restart clearing the
    counters is not the threat this defends against (an attacker who can
    restart the service already owns the robot).
    """

    def __init__(self, *, max_failures: int = LOGIN_MAX_FAILURES,
                 window_s: float = LOGIN_WINDOW_S,
                 max_failures_global: int = LOGIN_MAX_FAILURES_GLOBAL) -> None:
        self.max_failures = int(max_failures)
        self.max_failures_global = int(max_failures_global)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._all: list[float] = []

    def _recent(self, client: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(client, ()) if now - t < self.window_s]
        if recent:
            self._fails[client] = recent
        else:
            self._fails.pop(client, None)
        return recent

    def blocked(self, client: str, *, now: Optional[float] = None) -> bool:
        seconds = float(now if now is not None else time.time())
        with self._lock:
            # The global list is pruned FIRST and is O(failures-in-window), not
            # O(distinct-IPs): once the ceiling is reached we answer without
            # touching ``_fails`` at all.
            self._all = [t for t in self._all if seconds - t < self.window_s]
            if len(self._all) >= self.max_failures_global:
                return True
            # Bounded memory: a scan of a dict that only grows with distinct
            # attacking IPs, and the global ceiling above bounds how many of
            # those can accumulate inside one window.
            for other in [k for k in self._fails if k != client]:
                self._recent(other, seconds)
            return len(self._recent(client, seconds)) >= self.max_failures

    def record_failure(self, client: str, *, now: Optional[float] = None) -> None:
        seconds = float(now if now is not None else time.time())
        with self._lock:
            self._fails.setdefault(client, []).append(seconds)
            self._all.append(seconds)

    def clear(self, client: str) -> None:
        # Deliberately does NOT clear the global bucket: one correct PIN among
        # an attacker's guesses must not reset the ceiling for everyone.
        with self._lock:
            self._fails.pop(client, None)
