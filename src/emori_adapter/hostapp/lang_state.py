"""The kiosk language as RUNTIME state, so a standalone robot can be switched.

Reading ``EMORI_LANG`` once at import would freeze it into the menu, the pages, the
``emori_lang`` SSO claim and the TTS/STT language for the life of the process.
That is correct for a robot someone can SSH into; it is wrong for the appliance this
has become -- it boots to idle in Catalan, has no screen, no keyboard and no shell,
and a visitor-facing language switch must not need a shell. Switching it had to mean editing
``/opt/emori/.env`` and restarting the unit.

So the language lives here instead:

* ``current()`` is what every consumer asks, per page load / per claim, so a switch
  takes effect without a restart -- and it is the ONLY session-language authority:
  no per-badge roster ``lang`` or bundle value may override it.  An add-on
  whose server authorizes a fixed language may speak that one instead; that is not
  an override of the switch;
* ``set()`` persists to a small JSON file (``EMORI_LANG_STATE_FILE``, by default
  alongside the other robot-owned runtime files under ``artifacts/``) so the switch
  survives the power cut that ends every appliance's day;
* ``EMORI_LANG`` remains the value a robot that has never been toggled starts from;
* the languages on offer are this robot's Pokamind scope (:func:`allowed_languages`):
  Pokamind refuses a session language outside the robot's record, so a persisted
  switch to a language the record no longer lists is ignored at start, and
  :meth:`LangState.set` refuses one.

Deliberately BEST-EFFORT on the write: a read-only or full SD card is a reason to
come back up in the old language after a reboot, never a reason for the gesture a
human just performed to do nothing at all.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger("emori.hostapp.lang")

# The two languages this appliance actually ships: Piper voices, Vosk models,
# pages.UI_STRINGS and the Pokamind courses exist for exactly these.
SUPPORTED_LANGS = ("ca", "en")
# The default deployment language (Catalan); English is the other shipped language.
PRODUCTION_LANG = "ca"

_STATE_FILE_ENV = "EMORI_LANG_STATE_FILE"
# Next to artifacts/last_results.json and artifacts/tts_cache: one directory is all
# a deployment has to keep writable for the robot's own state.
DEFAULT_STATE_FILE = "artifacts/kiosk_lang.json"


def normalize_lang(value: object) -> str:
    """``" EN "`` -> ``"en"``; anything this robot does not ship -> ``""``."""

    if not isinstance(value, str):
        return ""
    code = value.strip().lower()
    return code if code in SUPPORTED_LANGS else ""


def _base_code(value: str) -> str:
    return value.strip().lower().replace("_", "-").split("-", 1)[0]


def scope_languages(snapshot: Optional[Mapping[str, Any]]) -> tuple[str, ...]:
    """The supported kiosk languages a scope snapshot's ``whoami.languages`` lists.

    Base codes (``ca-ES`` -> ``ca``), in the record's order (its preference order),
    intersected with :data:`SUPPORTED_LANGS`. All of :data:`SUPPORTED_LANGS` when there
    is no snapshot, the record lists no language (Pokamind then does not restrict the
    language), or it lists none this robot ships (nothing would start anyway, and
    readycheck says so: refusing every language would only add a dead switch). Then a
    language the record offers no module in is left out, when it offers modules in
    another (:func:`_with_modules`).
    """

    whoami = snapshot.get("whoami") if isinstance(snapshot, Mapping) else None
    languages = whoami.get("languages") if isinstance(whoami, Mapping) else None
    if not isinstance(languages, (list, tuple)):
        return SUPPORTED_LANGS
    allowed: list[str] = []
    for item in languages:
        code = _base_code(item) if isinstance(item, str) else ""
        if code in SUPPORTED_LANGS and code not in allowed:
            allowed.append(code)
    if not allowed:
        if languages:
            log.warning("this robot's Pokamind record lists no kiosk language this robot "
                        "ships (%s): every shipped language stays on offer",
                        "/".join(SUPPORTED_LANGS))
        allowed = list(SUPPORTED_LANGS)
    return _with_modules(tuple(allowed), whoami)


def _with_modules(allowed: tuple[str, ...], whoami: Mapping[str, Any]) -> tuple[str, ...]:
    """``allowed`` without the languages the record offers no module in.

    Only when it offers modules in at least one of them: a kiosk switched into a
    language with no module of this robot's would serve a menu whose every option
    fails. Nothing is dropped when that would leave nothing (readycheck reports it),
    nor when an offered module names no language (it may be any of them:
    ``site_settings.offered_module_languages`` then cannot tell).
    """

    from emori_adapter import site_settings  # noqa: PLC0415 - site_settings never imports hostapp

    offered = site_settings.offered_module_languages(whoami)
    kept = tuple(code for code in allowed if code in offered)
    if not offered or not kept or kept == allowed:
        return allowed
    log.warning("kiosk language offer %s: this robot's Pokamind record has no module in "
                "%s", "/".join(kept), "/".join(c for c in allowed if c not in kept))
    return kept


def allowed_languages(*, snapshot: Optional[Mapping[str, Any]] = None,
                      root: Optional[Any] = None) -> tuple[str, ...]:
    """The kiosk languages this robot may offer: its Pokamind scope's, in record order.

    ``snapshot`` defaults to THIS robot's last verified scope (the token this process
    runs with, ``site_settings.load_scope_snapshot``). All of :data:`SUPPORTED_LANGS`
    without a token or a snapshot, with ``EMORI_SITE_AUTO=0``, or when anything about
    reading the scope fails. Never raises.
    """

    try:
        if snapshot is None:
            from emori_adapter import robot_token, site_settings  # noqa: PLC0415

            if not site_settings.auto_enabled():
                return SUPPORTED_LANGS
            token = robot_token.configured().token
            if token is None:
                return SUPPORTED_LANGS
            try:
                fingerprint: Optional[str] = token.fingerprint
            except Exception:  # noqa: BLE001 - no cryptography: compare ids only
                fingerprint = None
            snapshot = site_settings.load_scope_snapshot(token.robot_id, fingerprint,
                                                         root=root)
        return scope_languages(snapshot)
    except Exception as exc:  # noqa: BLE001 - the kiosk must always have a language
        log.warning("kiosk language scope unreadable (%s): every shipped language stays "
                    "on offer", type(exc).__name__)
        return SUPPORTED_LANGS


def other_lang(value: object) -> str:
    """The language the hidden toggle switches TO.

    Total on purpose: it is called from the gesture thread, where a raise would be
    swallowed and the robot would simply stop responding to the gesture. A robot
    configured with a language we do not ship still has to toggle somewhere, and
    that somewhere is production Catalan.
    """

    return "en" if normalize_lang(value) == PRODUCTION_LANG else PRODUCTION_LANG


class LangState:
    """The live kiosk language, persisted across restarts.

    Read from request threads (page renders, SSO claims) and written from the
    gesture thread, so every access is under one lock. The read is CACHED: it sits
    on the page-render path, and re-reading a file per render buys nothing when the
    only writer is this same object. An outside edit therefore needs ``reload()``.
    """

    def __init__(self, *, default: str = "", path: str = "",
                 allowed: Optional[Iterable[str]] = None) -> None:
        # NOT normalised: ``EMORI_LANG`` is the deployment's word. Normalising it
        # here would silently re-language a robot that was never toggled at all.
        self._default = (default or os.environ.get("EMORI_LANG", "") or "en").strip() or "en"
        self._path = (path or os.environ.get(_STATE_FILE_ENV, "").strip()
                      or DEFAULT_STATE_FILE)
        # The languages on offer (allowed_languages(): this robot's Pokamind scope). None
        # or nothing usable: every shipped language.
        codes = tuple(dict.fromkeys(
            code for code in (normalize_lang(item) for item in (allowed or ())) if code))
        self._allowed = codes or SUPPORTED_LANGS
        self._lock = threading.RLock()
        self._current: Optional[str] = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def allowed(self) -> tuple[str, ...]:
        """The kiosk languages on offer, in preference order."""

        return self._allowed

    def is_allowed(self, lang: object) -> bool:
        return normalize_lang(lang) in self._allowed

    def set_allowed(self, allowed: Optional[Iterable[str]]) -> tuple[str, ...]:
        """Replace the languages on offer, live (a scope sync changed the record's list).

        Normalised like the constructor: ``None`` or nothing usable means every shipped
        language. ONLY the offer changes: the language in force stays what it is, even
        when it is no longer on offer -- moving the kiosk to another language is a
        sequence (the driver stamp, the page reload, an idle gate) that belongs to the
        caller (server._follow_scope_languages), and an explicit ``EMORI_LANG`` is never
        overridden from here. :meth:`set` refuses the dropped language from now on. The
        state file is never written here: a later start reads it again and ignores an
        out-of-scope value by itself. Returns the new offer.
        """

        codes = tuple(dict.fromkeys(
            code for code in (normalize_lang(item) for item in (allowed or ())) if code))
        with self._lock:
            self._allowed = codes or SUPPORTED_LANGS
            return self._allowed

    def fallback(self) -> str:
        """The language to move to when the one in force is not on offer: the
        deployment default when it is on offer, else the first language on offer."""

        with self._lock:
            default = normalize_lang(self._default)
            return default if default in self._allowed else self._allowed[0]

    @property
    def default(self) -> str:
        """The deployment's ``EMORI_LANG``: what an un-toggled robot serves."""

        return self._default

    def current(self) -> str:
        """The language every consumer must ask for, per page load / per claim."""

        with self._lock:
            if self._current is None:
                self._current = self._read() or self._default
            return self._current

    def set(self, lang: str) -> str:
        """Switch the kiosk language and persist it. Returns the normalised code.

        Raises ``ValueError("unsupported_lang")`` for a language this robot does not
        ship, or one outside :attr:`allowed` (this robot's Pokamind scope, which would
        refuse the session): a kiosk left in a language with no voice, or with no
        course, must be loud rather than silent.
        """

        code = normalize_lang(lang)
        if not code or code not in self._allowed:
            raise ValueError("unsupported_lang")
        with self._lock:
            self._current = code
            self._write(code)
        return code

    def reload(self) -> str:
        """Drop the cache and re-read the file (an operator edited it by hand)."""

        with self._lock:
            self._current = None
        return self.current()

    # ----- persistence (best-effort) ------------------------------------------

    def _read(self) -> str:
        try:
            with open(self._path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return ""                      # never toggled: the default is correct
        except (OSError, ValueError) as e:
            # A truncated file is what a power cut mid-write leaves behind. Say so
            # once at WARNING (uvicorn filters INFO) and fall back rather than
            # bricking every page render on it.
            log.warning("kiosk language state %s unreadable (%s); using %s",
                        self._path, e, self._default)
            return ""
        code = normalize_lang(payload.get("lang") if isinstance(payload, dict) else payload)
        if not code:
            log.warning("kiosk language state %s holds no supported language; using %s",
                        self._path, self._default)
        elif code not in self._allowed:
            # Switched earlier to a language this robot's Pokamind record no longer
            # lists: Pokamind would refuse every session in it.
            log.warning("kiosk language state %s holds %s, which this robot's Pokamind "
                        "scope does not offer (%s); using %s", self._path, code,
                        "/".join(self._allowed), self._default)
            return ""
        elif code != self._default:
            # The one line that makes this file discoverable. Without it, a robot whose
            # language was toggled earlier comes up in the "wrong" language and the
            # only visible configuration (EMORI_LANG in /opt/emori/.env) says otherwise
            # -- and journalctl is the only window an appliance has.
            log.warning("kiosk language is %s, not EMORI_LANG=%s: %s holds an earlier "
                        "toggle; delete that file to go back to the deployment default",
                        code, self._default, self._path)
        return code

    def _write(self, code: str) -> bool:
        """Replace the state file atomically. Never raises."""

        directory = os.path.dirname(self._path) or "."
        temp = ""
        try:
            os.makedirs(directory, exist_ok=True)
            # Write-then-rename: a power cut during the write leaves the PREVIOUS
            # language intact instead of a half-file the next boot cannot parse.
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".kiosk_lang-", suffix=".tmp",
                                             delete=False) as handle:
                temp = handle.name
                json.dump({"lang": code}, handle)
            os.replace(temp, self._path)
            return True
        except OSError as e:
            log.warning("could not persist the kiosk language to %s (%s); "
                        "the switch holds until the next restart", self._path, e)
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
            return False
