"""EMORI host app — the robot-side kiosk + Pokamind SSO + the live-session HUB.

This process is the single live driver (hostapp-hub architecture): the kiosk pages
relay Pokamind's postMessages to ``POST /api/cycle``, which stamps the active
session's identity onto each envelope and fans it out to

* the in-process robot driver (Translator -> BehaviorEngine -> RobotPlayer -> daemon),
* FIWARE (per-session NGSI-LD entities, per-cycle historization),
* the ``/ws/relay`` broadcast (consumed by the ROS 2 live publisher and any debugger),
* the JSONL session recorder (replay fixtures), and
* the session tracker (spoken summary + emailed results at completion).

Run it on the robot's CM4 (or the dev box):

    emori-hostapp                      # serves http://0.0.0.0:8080
    # or: python -m emori_adapter.hostapp.server

Pages / endpoints:
  GET  /                 kiosk landing = VERBAL topic selection (screenless, no picker)
  POST /api/voice-select recognised utterance -> module -> {session_url}
  GET  /identify         optional QR staff-badge identity page
  POST /api/identify     QR badge payload -> roster identity (+ greeting gesture)
  POST /api/start        mint a Pokamind partner SSO JWT (signed with this robot's token)
  GET  /session          embeds Pokamind in an iframe + relays postMessages to /api/cycle
  POST /api/cycle        the hub entry point (see above)
  GET  /api/status       live counters (cycles, dedups, parse errors, relay clients...)
  GET  /api/tts          server-side Piper TTS -> WAV (robot speech; pages play it)
  POST /api/stt          server-side Vosk STT (16 kHz PCM in, transcript out)
  POST /api/voice-cover  the page's own line is starting or ending on the speaker
  GET  /api/robot/status the robot's own status for host tools (loopback only)
  GET  /healthz

Config via env (see .env.example): HOSTAPP_HOST, HOSTAPP_PORT, POKAMIND_BASE,
EMORI_POKAMIND_TOKEN_FILE / POKAMIND_ROBOT_TOKEN (THIS robot's own Pokamind token, see
emori_adapter/robot_token.py; the only Pokamind credential), EMORI_PARTNER_NAME,
EMORI_COURSE_ID, EMORI_LANG, HOSTAPP_DEMO,
EMORI_FIWARE, EMORI_RECORD, EMORI_IDLE_AFTER_S, EMORI_SMTP_* (results email),
EMORI_STT_ENERGY_DBFS, EMORI_STT_MIN_VOICED_MS (landing capture energy gate),
EMORI_LANG_STATE_FILE + EMORI_LANG_TOGGLE + EMORI_LANG_TOGGLE_WINDOW_S (the hidden
idle double-turn that re-languages the kiosk without a restart), EMORI_LOG_LEVEL
(journal verbosity; INFO by default -- see main()).

Every one of those is read AFTER the robot-local settings layer
(``emori_adapter.site_settings``, applied when ``config`` is imported) has put the
effective value into the environment: a value saved on the admin page, else ``.env``,
else what this robot's Pokamind record supplies, else the hardware profile, else the
code default here. This process also keeps that record fresh (``pokamind_scope``:
boot +10 s, every 6 h), serves the menus built from it live, and RECORDS -- never acts
on -- why a restart would put a changed value in force (``admin_api.SiteRuntime``).
"""

# NOTE: no `from __future__ import annotations` here — FastAPI must evaluate the
# handler annotations (Request/WebSocket, imported inside _build_app) at def time;
# stringized annotations would resolve against module globals and silently turn
# them into required query params (422 on every page).
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Callable
import hashlib
import hmac
from urllib.parse import quote

# Load .env BEFORE reading any environment variable below (config.load_dotenv side effect).
import emori_adapter.config  # noqa: F401
from emori_adapter.constants import COMM_STYLES
from emori_adapter.behavior import repertoire as R
from emori_adapter import motion_trace, site_settings
from emori_adapter.hostapp import admin_api, dashboard, menu_builder, pages, skill_api
from emori_adapter.hostapp.badge_cue import BadgeCueError, load_badge_cue_wav
from emori_adapter.hostapp.extensions import (
    BADGE_OK,
    ExtensionContext,
    HostExtension,
    claim_timeout_s,
    load_extension,
)
from emori_adapter.hostapp import lang_state as _lang_state
from emori_adapter.hostapp import pokamind_scope
from emori_adapter.hostapp.driver import (
    GestureTicker,
    IdleTicker,
    LiveDriver,
    Recorder,
    SessionInfo,
    WatchdogTicker,
)
from emori_adapter.hostapp.identity import DEFAULT_COURSE_ID, Identity, Roster
from emori_adapter.hostapp.roster_admin import RosterAdmin
from emori_adapter.hostapp.jwt_sso import build_sso_url, mint_robot_sso_jwt
from emori_adapter import robot_token as _robot_token
from emori_adapter.hostapp.lang_state import LangState, normalize_lang, other_lang
from emori_adapter.hostapp import metrics_store
from emori_adapter.hostapp.mic_gate import MicGate
from emori_adapter.hostapp.mic_profile import IDLE_PROFILE, SESSION_PROFILE, MicProfile
from emori_adapter.hostapp.relay import RelayHub
from emori_adapter.hostapp.start_slot import LANDING_IDLE_PHASE, LandingSeen, StartSlot
from emori_adapter.hostapp.tt_inject import TtOverrideStore, TtPatchError, target_origins
from emori_adapter.hostapp.voice_topics import VoiceMenu
from emori_adapter.logging_setup import access_log_enabled, setup_journal_logging
from emori_adapter.robot.client import HttpRobotClient
from emori_adapter.sources.websocket import (
    AI_UTTERANCE_EVENT,
    ANALYSIS_EVENT,
    COMPLETED_EVENT,
    TTS_ENDED_EVENT,
    TURN_ARMED_EVENT,
    TURN_REJECTED_EVENT,
    TURN_SUBMITTED_EVENT,
    USER_SPEAKING_EVENT,
)

log = logging.getLogger("emori.hostapp")

REACHY_BASE_URL = os.environ.get("REACHY_BASE_URL", "http://127.0.0.1:8000")

# The Pokamind API origin: an explicit value (.env or the admin page), else the shipped
# default. Empty means unset. The SSO login lives on the API origin.
POKAMIND_BASE = (os.environ.get("POKAMIND_BASE", "").strip()
                 or site_settings.DEFAULT_POKAMIND_BASE)
PARTNER = os.environ.get("EMORI_PARTNER_NAME", "emori-robot")
# THIS robot's own Pokamind token (robot_token.py): the token file, else
# POKAMIND_ROBOT_TOKEN, else none. Bound ONCE, here -- the admin dashboard replaces it
# by writing the file and restarting this unit. It is the robot's ONLY Pokamind
# credential: it signs every SSO login (EdDSA, kid = its robot_id) and every
# control-plane call. Without a usable one (none installed, or one that does not parse:
# readycheck FAILs pokamind_token_missing / pokamind_token_invalid) the kiosk serves
# demo sessions only and never asks Pokamind for anything.
ROBOT_TOKEN_LOAD = _robot_token.configured()
ROBOT_TOKEN = ROBOT_TOKEN_LOAD.token
# The course an anonymous drop-in (and a badge whose roster names none) is claimed in:
# EMORI_COURSE_ID -- explicit, or derived from this robot's Pokamind record by the
# settings layer -- else identity.DEFAULT_COURSE_ID. One effective course, the same one
# identity.effective_course_id() gives the roster.
COURSE_ID = os.environ.get("EMORI_COURSE_ID", "").strip() or DEFAULT_COURSE_ID
# EMORI session language: 'ca' (Catalan) or 'en' (English).
# Pokamind accepts ca/en for kiosk sessions. Forwarded so the session never inherits a
# stale localStorage language from prior testing.
#
# This is the BOOT DEFAULT only. The live value is LangState (hostapp/lang_state.py): a
# screenless appliance is re-languaged by a hidden idle head gesture, which has to take
# effect for the next page load and the next SSO claim without a restart. Read it via
# ``lang_state.current()``; capturing this constant anywhere freezes the kiosk again.
#
# Unset (or empty), the settings layer fills it from this robot's Pokamind record (its
# default_language, else its first supported language); 'en' when nothing does.
EMORI_LANG = (os.environ.get("EMORI_LANG") or "").strip() or "en"


def demo_default(environ, *, token) -> bool:
    """Demo mode: always without a Pokamind token, else ``HOSTAPP_DEMO`` (default live).

    A robot without its own token has nothing to sign a login with, so it can only run
    demo sessions, whatever HOSTAPP_DEMO says -- reporting ``demo: false`` there would
    tell readycheck and the dashboard a story the kiosk cannot keep. With a token,
    HOSTAPP_DEMO=1 still forces demo (anything else = live).
    """

    if token is None:
        return True
    return environ.get("HOSTAPP_DEMO", "0") == "1"


# Credentials this release no longer reads. An older robot's .env may still carry them;
# they are ignored, and named (never shown) once at start so the line can be deleted.
RETIRED_CREDENTIAL_KEYS = site_settings.RETIRED_CREDENTIAL_KEYS

DEMO = demo_default(os.environ, token=ROBOT_TOKEN)
RESULTS_FILE = os.environ.get("EMORI_RESULTS_FILE", "artifacts/last_results.json")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
# Page-level verb for the pending-cmd channel: "the language under you changed, re-render".
# The LANDING page polls for this one verb and nothing else (pages.VOICE_HTML); the
# /session page polls the same slot for begin/end/home.
PAGE_RELOAD_CMD = "reload"
# Spoken confirmation of a language switch, IN THE LANGUAGE BEING SWITCHED TO -- on a
# screenless robot, hearing the new language IS the confirmation that the switch took.
_LANG_TOGGLE_LINES = {"ca": "Ara en català.", "en": "Now in English."}
# Three words through Piper. The bound exists only so a wedged audio stack cannot keep
# the helper thread alive forever.
_SPEAK_TIMEOUT_S = 20.0
_MAX_QR_CHARS = 512
# This retains only an HMAC fingerprint while the verbal topic picker is
# speaking/listening.  The browser itself debounces repeated camera decodes for
# three seconds; server ownership must last through the 60-second QR-to-session
# reservation so an otherwise valid selection does not lose its training lease.
_QR_HANDOFF_TTL_S = 65.0
_MAX_QR_HANDOFFS = 32
_QR_SESSION_MARKER = "-rqh-"
_SAFE_STATUS_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_QR_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,220}-rqh-[0-9a-f]{32}\Z")


def _decoded_qr_payload(value: object) -> str | None:
    """Accept only one small printable QR value without coercing arbitrary JSON.

    The raw value is deliberately used only for roster lookup and the immediate
    controller event.  It is never placed in a log, response, status object, or
    long-lived server-side record.

    Surrounding whitespace is STRIPPED rather than rejected.  A decoder is entitled to
    hand back the trailing newline a badge generator put in the QR, and refusing it
    produced the least diagnosable failure this kiosk had: a silent 400 that the page
    reported nowhere, on a badge that looks identical to a working one.  Roster lookup
    already strips, so this only aligns the gate with the lookup it guards.
    """

    if type(value) is not str:
        return None
    value = value.strip()
    if not value or len(value) > _MAX_QR_CHARS or not value.isprintable():
        return None
    return value


class _QrPriorityHandoff:
    """Pair a short-lived QR preemption with its later identity navigation.

    Only an HMAC fingerprint is retained for the bounded browser handoff;
    neither the decoded QR value nor an identity is cached.  This prevents the
    page's required ``/api/badge/claim`` then ``/api/identify`` sequence from
    starting two training leases, while refusing a second navigation attempt
    after a card has already been claimed.
    """

    def __init__(self, extension: HostExtension, *, ttl_s: float = _QR_HANDOFF_TTL_S,
                 session_held: Callable[[], bool] = lambda: False) -> None:
        self._extension = extension
        # A badge's session is minted or running: another card must not end it.
        self._session_held = session_held
        self._ttl_s = ttl_s
        self._key = secrets.token_bytes(32)
        self._entries: dict[str, tuple[float, bool]] = {}
        self._lock = threading.RLock()

    def _fingerprint(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def _purge_locked(self, now: float) -> None:
        for key, (deadline, _) in tuple(self._entries.items()):
            if deadline <= now:
                del self._entries[key]

    def _claim_locked(self, payload: str) -> bool:
        """May a card with no live entry start now? One card at a time."""

        if any(not claimed for _, claimed in self._entries.values()):
            return False
        try:
            return not self._session_held() and self._extension.claim_badge(payload) == BADGE_OK
        except Exception:  # noqa: BLE001 - an unconfirmed claim must not bind identity
            log.warning("add-on badge claim failed", exc_info=True)
            return False

    def reserve(self, payload: str) -> str:
        """Send the one priority event for a scanner decode.

        Returns content-free state labels for the HTTP boundary.  ``accepted``
        is intentionally idempotent until identity navigation claims it.
        """

        now = time.monotonic()
        fingerprint = self._fingerprint(payload)
        with self._lock:
            self._purge_locked(now)
            current = self._entries.get(fingerprint)
            if current is not None:
                return "accepted" if not current[1] else "already_claimed"
            if len(self._entries) >= _MAX_QR_HANDOFFS:
                return "unavailable"
            if not self._claim_locked(payload):
                # "refused" and "pending" both stay fail-closed: a scan whose
                # preemption could not be confirmed must not bind identity.
                return "unavailable"
            self._entries[fingerprint] = (now + self._ttl_s, False)
            return "accepted"

    def claim(self, payload: str) -> str:
        """Claim the matching preemption, or atomically create one for legacy UI."""

        now = time.monotonic()
        fingerprint = self._fingerprint(payload)
        with self._lock:
            self._purge_locked(now)
            current = self._entries.get(fingerprint)
            if current is not None:
                if current[1]:
                    return "already_claimed"
                self._entries[fingerprint] = (current[0], True)
                return "claimed"
            if len(self._entries) >= _MAX_QR_HANDOFFS:
                return "unavailable"
            if not self._claim_locked(payload):
                # "refused" and "pending" both stay fail-closed: a scan whose
                # preemption could not be confirmed must not bind identity.
                return "unavailable"
            self._entries[fingerprint] = (now + self._ttl_s, True)
            return "claimed"

    def release(self, payload: str) -> None:
        """Forget an unbindable claim without retaining the source QR value."""

        with self._lock:
            self._entries.pop(self._fingerprint(payload), None)


def _maybe_capture_results(msg: str) -> None:
    """On session completion, persist the results so they can be emailed afterwards."""
    try:
        data = json.loads(msg)
    except (TypeError, ValueError):
        return
    if data.get("type") == COMPLETED_EVENT:
        try:
            os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
            with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass


def _robot_sso_minter(robot_token):
    """``mint_robot_sso_jwt`` bound to this robot's token (600 s, the server's ttl)."""

    def mint(**claims) -> str:
        return mint_robot_sso_jwt(robot_token, **claims)

    return mint


def _load_roster() -> Roster:
    path = os.environ.get("EMORI_ROSTER", "")
    candidates = [path] if path else ["configs/roster.yaml", "configs/roster.example.yaml"]
    for c in candidates:
        if c and os.path.exists(c):
            return Roster.from_yaml(c)
    return Roster.default()


def _roster_write_path() -> str:
    """Where the admin page's People tab writes: ``EMORI_ROSTER``, else roster.yaml.

    Never ``configs/roster.example.yaml``, which a hostapp with no roster of its own
    falls back to READING: that file is tracked, shipped by every deploy, and would be
    overwritten by the next one. The first write seeds roster.yaml from it instead.
    """

    return os.environ.get("EMORI_ROSTER", "").strip() or "configs/roster.yaml"


# --- where the spoken menu comes from ------------------------------------------------
# The tiers a menu can come from, highest authority first (``_menu_tiers``):
#
#   env   EMORI_VOICE_TOPICS_<LANG> / EMORI_VOICE_TOPICS -- an operator's explicit file;
#   site  configs/site/voice_topics.<lang>.yaml -- saved on the phone admin page (or
#         adopted by one-click-deploy from a robot's former configs/prod menus);
#   prod  configs/prod/voice_topics.<lang>.yaml -- a robot's installed production menus,
#         from before configs/site existed (nothing in this repository ships them);
#   auto  configs/site/auto/voice_topics.<lang>.yaml -- built from this robot's Pokamind
#         record on every verified sync (menu_builder), off with EMORI_SITE_AUTO=0;
#   dev   the tracked configs/voice_topics{,.<lang>,.example}.yaml -- development menus;
#   builtin  VoiceMenu.default_menu().
#
# configs/site lives under the install root (site_settings.site_path) and is never
# tracked, so no file-level deploy can overwrite it -- the same reason configs/prod
# outranks the tracked menus. ``configs/prod/voice_topics.yaml`` is the ENGLISH one --
# that is the mapping install.sh always used -- so it is only ever a candidate for that
# language: a Catalan kiosk falling back to it would speak an English menu at a Catalan
# visitor, which is worse than the built-in default because it looks deliberate.
_PROD_MENU_DIR = "configs/prod"
_PROD_MENU_GENERIC_LANG = "en"
# Language codes are used to build an environment variable NAME; anything that is not a
# plain code cannot be one, and must not be pasted into os.environ lookups.
_LANG_CODE_RE = re.compile(r"[a-z0-9]{1,8}\Z")
# The labels menu_source() answers with, in tier order.
MENU_SOURCES = ("env", "site", "prod", "auto", "dev", "builtin")


def _menu_lang_code(lang: str) -> str:
    """``"ca-ES"`` / ``"CA_es"`` -> ``"ca"``; anything unusable -> ``""``."""

    code = str(lang or "").split("-")[0].split("_")[0].strip().lower()
    return code if _LANG_CODE_RE.fullmatch(code) else ""


def menu_override_env(lang: str = "en") -> str:
    """The NAME of the env var redirecting ``lang``'s menu, or ``""`` for none.

    Public because readycheck reports it: an operator-set override is the one
    legitimate reason for the active menu not to be the generated production file, and
    a gate that cannot tell "somebody chose this" from "a deploy overwrote it" has to
    treat both the same way.
    """

    code = _menu_lang_code(lang)
    if code and os.environ.get(f"EMORI_VOICE_TOPICS_{code.upper()}", "").strip():
        return f"EMORI_VOICE_TOPICS_{code.upper()}"
    if os.environ.get("EMORI_VOICE_TOPICS", "").strip():
        return "EMORI_VOICE_TOPICS"
    return ""


def prod_menu_source(lang: str = "en") -> str | None:
    """The INSTALLED production menu for ``lang`` under configs/prod, or None.

    What the menu *should* be on a robot that still has one, independent of what it
    currently *is*: readycheck compares the two.
    """

    code = _menu_lang_code(lang)
    if not code:
        return None
    candidates = [f"{_PROD_MENU_DIR}/voice_topics.{code}.yaml"]
    if code == _PROD_MENU_GENERIC_LANG:
        candidates.append(f"{_PROD_MENU_DIR}/voice_topics.yaml")
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def site_menu_path(lang: str = "en") -> str | None:
    """Where the admin page saves ``lang``'s menu (configs/site/voice_topics.<lang>.yaml
    under the install root), or None for a language this robot does not ship."""

    code = _menu_lang_code(lang)
    if code not in site_settings.SUPPORTED_LANGS:
        return None
    return str(site_settings.site_path(
        f"{site_settings.SITE_MENU_DIR}/voice_topics.{code}.yaml"))


def auto_menu_path(lang: str = "en") -> str | None:
    """Where the menu built from this robot's Pokamind record lives for ``lang``
    (configs/site/auto/voice_topics.<lang>.yaml), or None for an unshipped language."""

    code = _menu_lang_code(lang)
    if code not in site_settings.SUPPORTED_LANGS:
        return None
    return str(menu_builder.auto_menu_path(code))


def _auto_menu_in_force() -> bool:
    """Is the auto tier read? Only with ``EMORI_SITE_AUTO`` on AND a scope snapshot of
    the token this process runs with on disk.

    The built menus belong to the snapshot they were built from. Right after a token is
    replaced on the admin page (before the restart) that snapshot -- and its menus --
    are the NEW robot's, whose module ids the token still in force cannot start; and a
    robot without a token derives nothing from Pokamind at all.
    """

    if not site_settings.auto_enabled() or ROBOT_TOKEN is None:
        return False
    try:
        fingerprint = ROBOT_TOKEN.fingerprint
    except Exception:  # noqa: BLE001 - no cryptography: compare ids only
        fingerprint = None
    try:
        return site_settings.scope_snapshot_status(ROBOT_TOKEN.robot_id, fingerprint) == "ok"
    except Exception:  # noqa: BLE001 - an unreadable snapshot reads as none
        return False


def _menu_tiers(lang: str = "en") -> list[tuple[str, str]]:
    """``[(tier, path), ...]`` for ``lang``'s menu, highest authority first.

    The overrides stay on top so a robot can be pointed elsewhere without a code change --
    ``EMORI_VOICE_TOPICS_<LANG>`` first (per-language, so an override for one language
    cannot silently freeze the other one at the same file when the kiosk toggles), then
    the historical global ``EMORI_VOICE_TOPICS``. An override is the ONLY candidate.

    Then site > prod > auto > dev. The tracked development menus come last because every
    file-level deploy (``sudo tar -x`` over /opt/emori, ``git archive``, rsync) restores
    them: if they won, development module ids would reach production and every session
    would die at init_session. The auto tier is read only while ``EMORI_SITE_AUTO`` is on
    and the snapshot on disk is this token's (:func:`_auto_menu_in_force`): the builder
    writes regardless, the reader decides.
    """

    code = _menu_lang_code(lang) or _PROD_MENU_GENERIC_LANG
    override = os.environ.get(menu_override_env(lang), "").strip()
    if override:
        return [("env", override)]
    prod = [("prod", f"{_PROD_MENU_DIR}/voice_topics.{code}.yaml")]
    if code == _PROD_MENU_GENERIC_LANG:
        prod.append(("prod", f"{_PROD_MENU_DIR}/voice_topics.yaml"))
    dev = [("dev", f"configs/voice_topics.{code}.yaml"),
           ("dev", "configs/voice_topics.yaml"),
           ("dev", "configs/voice_topics.example.yaml")]
    site = site_menu_path(code)
    auto = auto_menu_path(code) if _auto_menu_in_force() else None
    return (([("site", site)] if site else []) + prod
            + ([("auto", auto)] if auto else []) + dev)


def _menu_candidates(lang: str = "en") -> list[str]:
    """The search order for ``lang``'s menu, highest authority first (paths only).

    See :func:`_menu_tiers` for what each one is and why it is where it is.
    """

    return [path for _tier, path in _menu_tiers(lang)]


def _load_menu(lang: str = "en") -> VoiceMenu:
    """Verbal topic menu (spoken number -> Pokamind module), in the kiosk language.

    Reads the first existing candidate of :func:`_menu_candidates`, falling back to the
    built-in default menu when nothing on disk answers.
    """
    source = _menu_source(lang)
    if source:
        return VoiceMenu.from_yaml(source)
    return VoiceMenu.default_menu()


def _menu_source(lang: str = "en") -> str | None:
    """The file _load_menu(lang) will read, or None for the built-in default.

    Split out so readycheck can judge the PROVENANCE of the active menu (a
    development menu carries module ids that production has never heard of --
    shipping one over the robot's menu makes every session die at init_session
    with 'Module not found').
    """

    for c in _menu_candidates(lang):
        if c and os.path.exists(c):
            return c
    return None


def menu_source(lang: str = "en") -> str:
    """Which tier ``lang``'s menu comes from: ``env``, ``site``, ``prod``, ``auto``,
    ``dev`` or ``builtin`` (:data:`MENU_SOURCES`). The admin page's Menu tab, install.sh
    and readycheck report it."""

    for tier, path in _menu_tiers(lang):
        if path and os.path.exists(path):
            return tier
    return "builtin"


def _menu_tuning(menu: VoiceMenu, lang: str) -> dict:
    """Screenless verbal-menu tuning handed to the kiosk page.

    ``grammar`` is the comma-joined constrained Vosk grammar; empty means OPEN recognition
    (no grammar gate), which ``EMORI_MENU_GRAMMAR=0`` restores as a field kill switch
    for a robot whose Vosk lexicon does not cover the menu.

    ``confMin`` was derived on the robot (vosk-en, 16 kHz, real room tone).
    A 0.6 floor rejected every digit the menu asks for: clean "one" scored 0.500
    and "two" 0.333, against topic phrases at 1.000. That was NOT short words scoring
    low: the grammar carried the homophones "won"/"to"/"too", which tie with the number
    exactly, and Vosk's confidence is the split between them. They are now kept out of the grammar
    (voice_topics._GRAMMAR_HOMOPHONES_BY_LANG) and a clean digit scores 1.0. Recognition itself was
    never the problem (the text came back exactly right at -25 and -35 dBFS alike); only the gate
    was, so the menu could not be answered by number at all. 0.30 clears the measured floor. It is a
    floor, NOT the safety net: confidence does not separate a real answer from force-aligned stray
    speech ("first of all" scores 1.000), so the guards that matter are the empty
    -text reject, VoiceMenu.match's ambiguity rule, and the exact-match requirement.
    """
    def _num(name: str, default, cast=float, floor=None):
        try:
            value = cast(os.environ.get(name, "") or default)
        except (TypeError, ValueError):
            log.warning("%s is not a number; using %s", name, default)
            value = cast(default)
        # Clamp, don't trust: EMORI_MENU_MAX_TRIES=0 makes the kiosk give up before it
        # ever re-prompts, and EMORI_MENU_LISTEN_MS=0 spins listen -> onMiss -> listen
        # through every try in milliseconds, so the person never gets to answer.
        if floor is not None and value < floor:
            log.warning("%s=%s is below the usable floor; using %s", name, value, floor)
            value = floor
        return value

    phrases = [] if os.environ.get("EMORI_MENU_GRAMMAR", "1") == "0" \
        else menu.grammar_phrases(lang)
    # End-of-speech flush (B).  POSTed to Vosk by a FIXED 3.5 s interval alone, a one-word
    # answer would wait out the rest of whatever window it landed in, ~1,750 ms of
    # average dead time.  The page flushes on the voiced->silence edge, using the SAME
    # per-20 ms RMS frames and the same EMORI_STT_ENERGY_DBFS floor as the landing-capture window gate.
    #
    #   eosMinVoicedMs  speech required before an edge is believed.  250 ms sits under the
    #                   shortest real menu answer (a lone digit measures ~300-500 ms on the
    #                   robot, per the confMin note above) and over an impulse: a door bang
    #                   or a cough clears the energy floor for ~150-300 ms and must not
    #                   flush a near-empty buffer.
    #   eosSilenceMs    trailing silence required.  700 ms, not "instantly": a visitor who
    #                   hesitates mid-phrase ("uh... three") pauses for a few hundred ms,
    #                   and splitting that answer sends half an utterance to Vosk.  0
    #                   disables the edge entirely and restores the pre-B page.
    #   flushMs         the backstop interval, unchanged at 3.5 s.  It is what makes a room
    #                   that never falls silent unable to wedge the menu: the edge can only
    #                   ever make a flush EARLIER, never replace one.
    #
    # What the backstop does NOT rescue, because it was claimed here and measured false:
    # an energy floor set so high that nothing reads as voiced (EMORI_STT_ENERGY_DBFS=-5).
    # The backstop fires on schedule, but flushStt's energy WINDOW gate then drops the buffer
    # and nothing reaches Vosk at all.  That hole is closed in the page, not here: the energy
    # gate may skip windows but never the FIRST window of a mic session, so a mis-set floor
    # costs the old latency instead of the menu (pages.flushStt, `gateJudged`; the bypass
    # count is readable on the robot as window.__emoriGateBypass).
    #
    # A miss is likewise NOT a consequence of the edge any more: an edge flush may only
    # ACCEPT.  Every negative judgement -- low confidence, a non-exact match -- stays on the
    # backstop window and the listen deadline, at the pre-B rate, because a spurious
    # sub-confidence match in a chattering room must not cost the visitor one of maxTries.
    return {"grammar": ",".join(phrases),
            "confMin": _num("EMORI_MENU_CONF_MIN", 0.3),
            "maxTries": _num("EMORI_MENU_MAX_TRIES", 3, int, floor=1),
            "listenMs": _num("EMORI_MENU_LISTEN_MS", 12000, int, floor=1000),
            "eosSilenceMs": _num("EMORI_MENU_EOS_SILENCE_MS", 700, int, floor=0),
            "eosMinVoicedMs": _num("EMORI_MENU_EOS_MIN_VOICED_MS", 250, int, floor=0),
            "flushMs": _num("EMORI_MENU_FLUSH_MS", 3500, int, floor=500)}


_STT_GATE_DEFAULTS = {"dbfs": -50.0, "min_voiced_ms": 120, "frame_ms": 20}


def _stt_gate_json() -> str:
    """Landing-capture frame-energy gate, rendered into the kiosk page.

    ``EMORI_STT_ENERGY_DBFS`` is the per-20 ms-frame RMS floor a frame must reach to
    count as voiced (-50 dBFS is deliberately conservative: room tone and a
    conversation a few metres away sit below it, someone addressing the robot does
    not).  ``EMORI_STT_MIN_VOICED_MS`` is how much voiced audio a 3.5 s window needs
    before it is POSTed to Vosk at all; <= 0 disables the gate.  Unparseable values
    fall back to the defaults - a bad .env must never mute the kiosk.
    """
    def _num(name, default, cast):
        # Loudly, like _menu_tuning._num above: these two knobs ARE the on-robot tuning
        # path, and on a screenless appliance a silent revert to the default is
        # indistinguishable from a retune that took ("-42dB" and "-38.5db" both do it).
        try:
            return cast(os.environ.get(name, "").strip() or default)
        except (TypeError, ValueError):
            log.warning("%s=%r is not a number; using %s",
                        name, os.environ.get(name, ""), default)
            return default

    def _clamp(name, value, low, high):
        clamped = min(high, max(low, value))
        if clamped != value:
            log.warning("%s=%s is outside %s..%s; using %s", name, value, low, high, clamped)
        return clamped

    dbfs = _clamp("EMORI_STT_ENERGY_DBFS",
                  float(_num("EMORI_STT_ENERGY_DBFS",
                             _STT_GATE_DEFAULTS["dbfs"], float)), -120.0, 0.0)
    voiced = _clamp("EMORI_STT_MIN_VOICED_MS",
                    int(_num("EMORI_STT_MIN_VOICED_MS",
                             _STT_GATE_DEFAULTS["min_voiced_ms"], int)), 0, 3000)
    return json.dumps({"dbfs": dbfs, "min_voiced_ms": voiced,
                       "frame_ms": _STT_GATE_DEFAULTS["frame_ms"]})


# A `<link>` href we are willing to write into the kiosk page unescaped.  The origins come
# from POKAMIND_BASE (an env var, normalised to scheme://netloc by tt_inject) and from the
# frontend origin derived from it, so this is belt-and-braces rather than a real boundary
# -- but a `"` in a deployment's own configuration must not be able to author markup.
_SAFE_ORIGIN_RE = re.compile(r"^https?://[a-z0-9.\-]+(:\d{1,5})?$")


def _preconnect_links(app_origin: str | None = None) -> str:
    """DNS + TLS warm-up for the Pokamind origins the next session will navigate to (C).

    While the visitor is listening to the menu and choosing a topic, nothing is warmed
    toward Pokamind: the SSO redirect then pays DNS + TLS + first bytes cold (several
    hundred ms of the session start).  Both origins matter and for different reasons -- the iframe
    first loads the SSO endpoint on ``POKAMIND_BASE`` and is then 302'd to the FRONTEND
    origin (``tt_inject.frontend_origin``), which is where the session document, its
    bundle and its own API traffic live.

    ``preconnect`` is deliberately emitted WITHOUT ``crossorigin``: an iframe navigation
    uses the credentialed connection pool, and an anonymous preconnect warms the wrong one.

    This is the whole of C.  A hidden pre-boot iframe of the app origin was considered and
    REJECTED, not deferred and not shipped behind a flag: on this kiosk Chromium runs with
    ``--use-fake-ui-for-media-stream``, so a second Pokamind document would be auto-granted
    the microphone the landing page is capturing menu answers with, and it would boot the
    app's auth path at the moment the real session is minting its one-time SSO
    token.  Neither can be shown side-effect-free from this repository (the app is a
    separate codebase), and an env flag for an unproven pre-boot is just a
    switch someone flips on a robot.  A link hint opens a socket and sends no request; that
    is the part that is provably safe, so that is the part that ships.
    """

    # target_origins("", base, app_origin) is exactly "the SSO origin plus the frontend
    # origin it 302s to" (the record's app_origin when it names one), already normalised
    # to scheme://netloc (no path, no credentials, lowercased).
    # The empty first argument drops EMORI_TT_TARGET_ORIGINS on purpose: those exist to
    # widen where a postMessage may be DELIVERED, which is a different question.
    return "".join(f'<link rel="dns-prefetch" href="{o}">'
                   f'<link rel="preconnect" href="{o}">'
                   for o in target_origins("", POKAMIND_BASE, app_origin)
                   if _SAFE_ORIGIN_RE.match(o))


_SESSION_START_MS_DEFAULT = 45000


def _session_start_ms() -> int:
    """How long the /session page waits for the FIRST ``pokamind_*`` message.

    Everything after ``GET /session`` happens inside a cross-origin iframe, so a
    Pokamind that never comes up (a filtered module list, a dead frontend, a rejected
    claim) is indistinguishable from a robot that decided to stop: antennas open from
    the greeting, nothing spoken, no way back.  This is the page's own deadline for
    that, mirrored adapter-side by ``EMORI_SESSION_START_S`` (WatchdogTicker) for a
    kiosk that cannot even POST.  ``EMORI_SESSION_START_MS=0`` disables the page half.
    """

    raw = os.environ.get("EMORI_SESSION_START_MS", "").strip()
    if not raw:
        return _SESSION_START_MS_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        log.warning("EMORI_SESSION_START_MS=%r is not a number; using %d",
                    raw, _SESSION_START_MS_DEFAULT)
        return _SESSION_START_MS_DEFAULT
    if value != 0 and not (5000 <= value <= 600000):
        log.warning("EMORI_SESSION_START_MS=%d is outside 5000..600000 (0 = off); using %d",
                    value, _SESSION_START_MS_DEFAULT)
        return _SESSION_START_MS_DEFAULT
    return value


# How long the language toggle's own spoken line may take, to keep anything else off the speaker.
_LANG_LINE_S = 6.0


def _speak_on_robot(tts, text: str, lang: str, *, run=None) -> bool:
    """Say one line on the robot's OWN speaker. Best-effort; never raises.

    The kiosk pages normally do all the talking (they fetch ``/api/tts`` and play the WAV
    through Chromium). The language toggle cannot: the page that would speak it is showing
    the OLD language and is about to reload. So this synthesises with the same Piper engine
    the pages use and plays it with ``pw-play`` -- exactly what the spoken readycheck does,
    and for the same reason (the appliance runs PipeWire and the daemon is started
    ``--no-media`` so the sink is free).

    Every failure degrades to silence: no Piper voice, no pw-play (a dev box), a busy sink.
    ``run`` is injectable purely so tests never shell out; production passes nothing.
    """

    if tts is None or not text.strip():
        return False
    runner = run if run is not None else subprocess.run
    try:
        path = tts.synth_path(text, lang)
    except Exception as e:  # noqa: BLE001 - engine failure must not kill the caller
        log.warning("could not synthesise %r (%s)", text, e)
        return False
    if not path:
        return False
    argv = ["pw-play"]
    target = os.environ.get("EMORI_PLAY_TARGET", "").strip()
    if target:
        argv.append(f"--target={target}")
    argv.append(str(path))
    try:
        completed = runner(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=_SPEAK_TIMEOUT_S, check=False
        )
    except Exception as e:  # noqa: BLE001 - no pw-play on a dev box is not an error
        log.warning("could not play the robot's own voice (%s: %s)", type(e).__name__, e)
        return False
    return getattr(completed, "returncode", 1) == 0


# Hostname/IP characters and nothing else -- no whitespace, no `@`, no quotes.
# `:` and `%` are here for IPv6 literals and their zone ids; `_` because Windows
# and some LAN DNS hand out names containing it.
_HOSTISH_RE = re.compile(r"[A-Za-z0-9._:%-]{1,253}")


def _netloc_without_userinfo(url: str) -> str:
    """``http://user:pass@orion.example.local:1026/`` -> ``orion.example.local:1026``.

    A configured URL is the one env value that can legally carry a credential,
    and a hostname answers every question a log line asks of it.  Anything
    unparseable degrades to ``"?"`` rather than falling back to the raw string,
    because the unparseable case is exactly where a stray ``@`` lives.

    The scheme-less form is handled explicitly.  ``ORION_URL=orion.example.local:
    1026`` is a perfectly ordinary thing to write in a .env, and ``urlsplit``
    reads that leading ``orion.example.local`` as a SCHEME (it is a valid one), so
    the naive parse produces an empty ``hostname`` and the operator was told
    their broker was at "?" -- exactly the diagnostic this line exists to give
    them.  Re-parsing with a scheme prepended costs one branch and is the same
    idea ``urllib`` itself uses for protocol-relative URLs.
    """

    from urllib.parse import urlsplit

    raw = str(url or "").strip()
    if not raw:
        return "?"
    try:
        parts = urlsplit(raw)
        if not parts.hostname and "//" not in raw:
            # No authority section at all: "host:port", "host:port/path", "host".
            parts = urlsplit("//" + raw)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "?"
    # `//` makes urlsplit call ANYTHING before the first `/` a host, so without
    # this a value that is not a URL at all (a pasted line of prose) would
    # come back out verbatim -- the raw-string fallback the docstring promises not
    # to have. A host is host characters or it is "?".
    if not host or _HOSTISH_RE.fullmatch(host) is None:
        return "?"
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal: unbracketed, "::1:1026" reads as neither
    return f"{host}:{port}" if port else host


def _default_fiware_factory(info: SessionInfo):
    """Build the session's NGSI-LD writer -- or a null one when no broker exists.

    BLOCKING (a 1 s probe + entity creation). Called only from
    ``LiveDriver._start_fiware``, which runs it on the 'fiware-session-start'
    daemon thread, never on the FastAPI event loop.
    """

    from emori_adapter.config import load_settings
    from emori_adapter.fiware import FiwareClient, NullFiwareClient, broker_reachable

    s = load_settings()
    if not broker_reachable(s.orion_url):
        # EMORI_FIWARE=1 ships enabled, and most robots have no Orion-LD on their
        # LAN. Degrade to a silent no-op after ONE cheap probe instead of three
        # timing-out HTTP calls and a wall of WARNINGs at every session start.
        #
        # HOST and port, never the raw ORION_URL: `http://user:pass@orion.example
        # .local:1026` is a legal and common value for a broker behind basic
        # auth, and this is the one place the env value would be printed -- once
        # per session, into a journal that persists across boots. "Which host did
        # we probe" is the entire diagnostic, and it survives the split.
        broker = _netloc_without_userinfo(s.orion_url)
        log.info("no NGSI-LD broker at %s; FIWARE historization skipped this session",
                 broker)
        metrics_store.get_store().log_event(
            "fiware_skipped", session_id=info.session_id, detail=broker)
        return NullFiwareClient()
    # De-identified BEFORE it becomes an NGSI-LD entity id. `_mint_session_id`
    # prefixes the SSO student id, so `urn:ngsi-ld:Session:anna-001-20260730-...`
    # would publish that id -- to Orion, into every QuantumLeap time-series row,
    # and onto any Grafana panel built on them -- while the same session is
    # `S-xxxxxx` everywhere else. Same transform the local store applies, so the
    # two feeds still line up row for row (metrics_store.deidentify_session_id is
    # idempotent and leaves a non-minted id alone).
    #
    # The PERSON is pseudonymised for exactly the same reason: scrubbing the
    # session URN while `urn:ngsi-ld:Person:anna-001` -- plus a `name` attribute
    # reading "Anna Example" -- went to the broker beside it would buy nothing
    # (the Session's `learner` relationship would point straight back at the
    # student id). `person_code` is the SAME `S-xxxxxx` the
    # local dashboard uses, derived from the same stable salt, so the FIWARE feed
    # and the appliance feed still name a returning learner identically and the
    # relationship still joins -- with no re-identifying attribute travelling
    # off the robot. The name is therefore deliberately NOT forwarded: a
    # pseudonymous URN carrying the real name is not pseudonymous at all.
    return FiwareClient(orion_url=s.orion_url, tenant=s.ngsild_tenant,
                        session_id=metrics_store.deidentify_session_id(info.session_id),
                        person_id=metrics_store.person_code(info.person_id)[0],
                        ql_notify_url=os.environ.get("QL_NOTIFY_URL",
                                                     "http://quantumleap:8668/v2/notify"))


def _load_tuning():
    """Load behaviour + translation tuning from configs/behavior.yaml (or
    EMORI_BEHAVIOR_CONFIG) so the documented tuning file reaches the live path."""
    from emori_adapter.behavior import BehaviorConfig
    from emori_adapter.translation import TranslationConfig

    path = os.environ.get("EMORI_BEHAVIOR_CONFIG", "configs/behavior.yaml")
    if path and os.path.exists(path):
        try:
            return BehaviorConfig.from_yaml(path), TranslationConfig.from_yaml(path)
        except Exception as e:  # noqa: BLE001 - bad YAML must not kill the kiosk
            log.warning("could not load %s (%s); using defaults", path, e)
    return None, None


def _mint_session_id(student_id: str, *, qr_bound: bool = False) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe = (re.sub(r"[^A-Za-z0-9_-]", "_", student_id) or "anon")[:160]
    # The timestamp remains operationally useful, but a session URL must not
    # be predictable or collide when one staff member starts two sessions in
    # the same second.  QR-bound URLs carry a random, syntactically distinct
    # one-time namespace: an expired/replayed QR URL can therefore never fall
    # through to a legacy anonymous session after its pending record is gone.
    suffix = f"{_QR_SESSION_MARKER}{secrets.token_hex(16)}" if qr_bound else ""
    return f"{safe}-{stamp}-{secrets.token_hex(8)}{suffix}"


def _is_qr_session_id(value: object) -> bool:
    return type(value) is str and _QR_SESSION_ID_RE.fullmatch(value) is not None


def pokamind_credential() -> str:
    """Which credential this process logs learners in with -- never the credential.

    ``robot_token`` (its own per-robot token) or ``none`` (demo sessions only). Read from
    the module globals at call time, so ``/healthz`` and the admin status always
    describe the process as it runs.
    """

    return "robot_token" if ROBOT_TOKEN is not None else "none"


def pokamind_key_fingerprint() -> str | None:
    """The fingerprint of the token this process signs with, or None without one.

    A fingerprint is derived from the PUBLIC key and is not a secret (the CLI, whoami
    and the admin page all show it). ``pokamind_credential`` alone cannot
    tell a rotated token from the one it replaced -- both are ``robot_token`` -- so
    readycheck compares this with the token on disk.
    """

    if ROBOT_TOKEN is None:
        return None
    try:
        return ROBOT_TOKEN.fingerprint
    except Exception:  # noqa: BLE001 - crypto extras missing: the field is just absent
        return None


def _report_retired_credentials(environ=None) -> list[str]:
    """WARN once per retired credential key still set -- by NAME: no value is ever put
    in a message. Returns the names (for tests). They change nothing: this release
    signs only with the robot's own token."""

    env = os.environ if environ is None else environ
    found = [key for key in RETIRED_CREDENTIAL_KEYS if (env.get(key) or "").strip()]
    for key in found:
        log.warning("%s is set but no longer used: this robot signs only with its own "
                    "Pokamind token. Delete the line from /opt/emori/.env", key)
    return found


def _report_pokamind_credential() -> None:
    """Say once, at build, which credential is in force -- and any robot_id conflict."""

    _report_retired_credentials()
    load = ROBOT_TOKEN_LOAD
    if ROBOT_TOKEN is not None:
        log.info("Pokamind credential: this robot's own token %s (%s)",
                 ROBOT_TOKEN.masked, load.source)
        if _robot_token.robot_id_conflict(ROBOT_TOKEN, os.environ.get("EMORI_ROBOT_ID", "")):
            # ERROR, not WARNING: the robot logs in as the TOKEN's robot, and if that is
            # not this robot the wrong token is installed. Ids only -- never the token.
            log.error("EMORI_ROBOT_ID=%r is not the robot this Pokamind token belongs to "
                      "(%r): the token wins for every Pokamind call. Fix whichever is wrong "
                      "(set EMORI_ROBOT_ID to the token's robot id - do not delete it, the "
                      "dashboard salt falls back to it; docs/deploy-a-new-robot.md); "
                      "readycheck reports robot_id_mismatch",
                      _robot_token.robot_slug(os.environ.get("EMORI_ROBOT_ID", "")),
                      ROBOT_TOKEN.robot_id)
        return
    if load.error:
        log.error("the Pokamind robot token in %s is unusable (%s) and is IGNORED: the "
                  "kiosk serves demo sessions only - replace it from the admin page's "
                  "Pokamind tab (Replace token), or re-deploy with it "
                  "(docs/deploy-a-new-robot.md)",
                  "POKAMIND_ROBOT_TOKEN" if load.source == "env" else load.path, load.error)
    else:
        log.error("this robot has no Pokamind token: the kiosk serves demo sessions only "
                  "and asks Pokamind for nothing - install this robot's Pokamind token: "
                  "admin page -> Pokamind -> Replace token, or Deploy-to-Robot "
                  "(docs/deploy-a-new-robot.md)")


def _build_app(overrides: dict | None = None):
    """Build the FastAPI app. ``overrides`` injects test doubles:

    ``robot_client`` (RobotClient), ``fiware_factory`` (SessionInfo -> client or None),
    ``tts`` / ``stt`` (speech engines), ``idle`` (False disables the IdleTicker),
    ``recorder`` (Recorder), ``on_session_end`` (callable(stats) -> None, e.g. email),
    ``lang_state`` (LangState, e.g. one pointed at a temp state file),
    ``speak`` (callable(text, lang) -> None replacing the robot's own voice),
    ``site_runtime`` (admin_api.SiteRuntime), ``scope_sync`` (a whole
    pokamind_scope.ScopeSync) or ``scope_fetch`` / ``menu_rebuild`` (the default one's
    whoami fetch and menu builder), ``scope_background`` (False: no background sync).
    """
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

    ov = overrides or {}
    _report_pokamind_credential()
    roster = _load_roster()
    # What this process knows about its own site configuration: THIS robot's scope
    # snapshot (the course, languages, menus and app origin its Pokamind record gives)
    # and why a restart is pending. Never restarts anything (admin_api.SiteRuntime).
    site_runtime = (ov["site_runtime"] if "site_runtime" in ov
                    else admin_api.SiteRuntime(token=ROBOT_TOKEN))

    def _scope_languages() -> tuple[str, ...]:
        """The kiosk languages this robot's Pokamind scope offers (all without one)."""

        if not site_settings.auto_enabled() or ROBOT_TOKEN is None:
            return _lang_state.SUPPORTED_LANGS
        return _lang_state.scope_languages(site_runtime.snapshot())

    # The kiosk language is RUNTIME state (a hidden idle head gesture switches it with no
    # restart), so nothing below may capture it: EMORI_LANG is only where a robot that has
    # never been toggled starts. Per _build_app rather than per module so the deployment
    # default is read at build time (and a test can inject its own state file). Only the
    # languages this robot's Pokamind scope offers can be switched to.
    lang_state = (ov["lang_state"] if "lang_state" in ov
                  else LangState(default=EMORI_LANG, allowed=_scope_languages()))
    menu_cache: dict[str, tuple[VoiceMenu, dict]] = {}
    menu_cache_lock = threading.RLock()

    def _clear_menu_cache() -> None:
        """Forget every loaded menu: the next page render reads the files again."""

        with menu_cache_lock:
            menu_cache.clear()

    def _menu_for(lang: str = "") -> tuple[VoiceMenu, dict]:
        """The verbal menu + its page tuning for ``lang`` (default: the LIVE language).

        Cached per language: the two courses are separate YAML files, and after a switch
        both stay warm, so re-rendering the landing page costs no disk read.
        """

        code = lang or lang_state.current()
        with menu_cache_lock:
            entry = menu_cache.get(code)
            if entry is None:
                loaded = _load_menu(code)
                entry = (loaded, _menu_tuning(loaded, code))
                menu_cache[code] = entry
            return entry

    # The language switch and the idle predicate, once _build_app has defined them (a
    # scope sync can only land after that, but nothing here may assume it).
    scope_lang_hooks: dict[str, Callable] = {}

    def _follow_scope_languages() -> None:
        """Make the kiosk's language offer follow a changed scope, live when it can.

        The OFFER changes live (``set_allowed``). The language in force is left alone
        when it is still on offer, and when it is this robot's explicit ``EMORI_LANG``
        (set in .env or on the admin page: explicit wins). Otherwise the kiosk moves to
        :meth:`LangState.fallback` through the one switch sequence
        (``set_kiosk_language``: driver stamp, page reload) -- only while nothing at all
        is running; under a visitor it is a pending restart instead, which applies
        when the robot is idle. A language state that cannot take a new offer while
        running (no ``set_allowed``) gets it at the next start: a pending restart too.
        """

        allowed = tuple(_scope_languages())
        if tuple(getattr(lang_state, "allowed", allowed) or ()) == allowed:
            return
        setter = getattr(lang_state, "set_allowed", None)
        if not callable(setter):
            site_runtime.add_reasons([admin_api.REASON_SCOPE_CHANGED])
            return
        try:
            setter(allowed)
        except Exception as exc:  # noqa: BLE001 - fall back to the restart
            log.warning("kiosk language offer not updated live (%s)", type(exc).__name__)
            site_runtime.add_reasons([admin_api.REASON_SCOPE_CHANGED])
            return
        log.warning("kiosk language offer now %s (Pokamind scope)", "/".join(allowed))
        current = normalize_lang(lang_state.current())
        if not current or current in allowed:
            return
        explicit = (site_settings.effective_sources(os.environ).get("EMORI_LANG")
                    in site_settings.LOCAL_SOURCES)
        if explicit and current == normalize_lang(getattr(lang_state, "default", "")):
            log.warning("kiosk language %s is set on this robot (EMORI_LANG) but is not "
                        "in its Pokamind scope (%s): kept", current, "/".join(allowed))
            return
        fallback = getattr(lang_state, "fallback", None)
        target = fallback() if callable(fallback) else allowed[0]
        switch, idle = scope_lang_hooks.get("switch"), scope_lang_hooks.get("idle")
        try:
            ready = callable(switch) and callable(idle) and bool(idle())
        except Exception:  # noqa: BLE001 - an unreadable state is busy
            ready = False
        if ready:
            switch(target, reason="Pokamind scope")
        else:
            log.warning("kiosk language %s left the Pokamind scope (%s): the robot is "
                        "busy, so it moves to %s at the next restart", current,
                        "/".join(allowed), target)
            site_runtime.add_reasons([admin_api.REASON_SCOPE_CHANGED])

    def _on_scope_change(outcome) -> None:
        """Every scope sync lands here (pokamind_scope.ScopeSync.on_change).

        Runs on the sync's own thread for a background sync, so everything here is
        quick and locked. The menus apply LIVE (the cache is dropped and the next page
        render reads the rebuilt files); the language offer follows; everything else a
        restart puts in force is only RECORDED as a reason. Nothing is restarted here.
        """

        site_runtime.note_sync(outcome)
        # A recorded scope can also change WHETHER the built menus are read (they are,
        # only for the token in force), so any recorded sync drops the cache; it costs
        # one file read at the next page render.
        if getattr(outcome, "menus_changed", False) or getattr(outcome, "ok", False):
            _clear_menu_cache()
        if getattr(outcome, "ok", False):
            _follow_scope_languages()

    # This robot's Pokamind scope: fetched, verified and recorded (with the menus built
    # from it) at boot +10 s, every 6 h, on the admin page's Sync/Verify, and -- for a
    # NEW token -- before a replaced token is saved. Built even without a token, so the
    # first token pasted on a fresh robot still records its scope before the restart.
    scope_kwargs = {"on_change": _on_scope_change}
    if "scope_fetch" in ov:
        scope_kwargs["fetch"] = ov["scope_fetch"]
    if "menu_rebuild" in ov:
        scope_kwargs["rebuild"] = ov["menu_rebuild"]
    scope_sync = (ov["scope_sync"] if "scope_sync" in ov
                  else pokamind_scope.ScopeSync(POKAMIND_BASE, ROBOT_TOKEN, **scope_kwargs))
    # The background loop runs only on a live robot with a token (each sync costs
    # Pokamind one nonce): never in demo mode, never without a credential to sign with.
    scope_background = bool(ov["scope_background"] if "scope_background" in ov
                            else ROBOT_TOKEN is not None and not DEMO)

    hub = RelayHub()
    # Operator-tunable in-iframe turn-taking overrides (window.__EMORI_TT). Seeded
    # from env at boot; empty (and therefore inert) unless a human sets it.
    tt_store = TtOverrideStore(seed=os.environ.get("EMORI_TT_OVERRIDES", ""))

    fiware_enabled = os.environ.get("EMORI_FIWARE", "1") != "0"
    fiware_factory = ov.get(
        "fiware_factory", _default_fiware_factory if fiware_enabled else None
    )
    client = ov.get("robot_client") or HttpRobotClient(REACHY_BASE_URL)
    bcfg, tcfg = _load_tuning()
    # Decide gesture sensing up front: the driver's RobotPlayer needs it so it keeps the
    # HEAD in gravity_compensation (back-drivable) for the head-tilt sensor between moves.
    # Env-gated (EMORI_GESTURE, default on for the real robot). MUST be disabled on the
    # MuJoCo sim: there is no human to nudge the head, and a back-drivable simulated head
    # DRIFTS under gravity_compensation -> the detector reads that drift/settling as taps
    # and fires spurious begin/end commands (observed: gesture_begin/gesture_end climbing
    # into the dozens untouched, ending live sessions the instant they open). Sim launch
    # scripts set EMORI_GESTURE=0. The ``ov`` override still wins for tests.
    gesture_enabled = bool(ov["gesture"]) if "gesture" in ov \
        else os.environ.get("EMORI_GESTURE", "1") != "0"
    # The robot's own voice as a live signal (hostapp/voice_link.py): a PipeWire tap
    # of the speaker output that lets every speech plan start when the voice starts,
    # pace its beats on the words and settle when the voice stops. EMORI_VOICE_LINK=0
    # or no pw-record (dev box, simulator) -> None -> the fixed-duration player.
    if "voice" in ov:
        voice_link = ov["voice"]
    else:
        from emori_adapter.hostapp.voice_link import VoiceLink, default_enabled

        voice_link = VoiceLink() if default_enabled() else None
    driver = LiveDriver(client, player=ov.get("player"),
                        fiware_factory=fiware_factory, default_lang=lang_state.current(),
                        behavior_config=bcfg, translation_config=tcfg,
                        gesture_enabled=gesture_enabled, voice=voice_link)
    # One capture source, two states that need it at incompatible levels: a training
    # session hears a person standing a metre away, an idle robot must not read room
    # tone as speech (measured numbers in mic_profile.py). Every
    # transition goes through this one object so the gain can never be left where the
    # previous mode wanted it. Best-effort: no wpctl -> the level simply doesn't move.
    mic_profile = ov["mic_profile"] if "mic_profile" in ov else MicProfile()

    def _face_age() -> float | None:
        # A face is only tracked during a training session. In an add-on mode the last one
        # seen belongs to somebody earlier, and says nothing about who is there now.
        seen = driver.last_face_seen_at
        if seen is None or not driver.tracker.active:
            return None
        return time.monotonic() - seen

    # Follows the capture profile: it only has a say while a mode holds the microphone.
    mic_gate = ov["mic_gate"] if "mic_gate" in ov else MicGate(
        face_age=_face_age,
        robot_speaking=(voice_link.active if voice_link is not None else None))
    mic_profile.listener = lambda profile: mic_gate.set_active(profile == SESSION_PROFILE)
    recorder = ov.get("recorder") or Recorder(enabled=os.environ.get("EMORI_RECORD", "1") != "0")
    if "on_session_end" in ov:
        on_session_end = ov["on_session_end"]
    else:
        from emori_adapter.hostapp.results_email import deliver_results

        on_session_end = deliver_results
    idle_after = float(os.environ.get("EMORI_IDLE_AFTER_S", "8"))
    # Conversation watchdog poller. Gated like the idle ticker so hostapp tests can
    # disable the live 1s daemon (overrides={"watchdog": False}); a fresh instance per
    # _build_app is required because Thread.start() is single-shot.
    watchdog_ticker = WatchdogTicker(driver) if ov.get("watchdog", True) else None
    # Physical head-tilt gesture sensor (screenless begin/end). Gated like the others so
    # tests can disable the live ~10Hz poller. The driver's RobotPlayer (above) holds the
    # HEAD in gravity_compensation so a touch deflects it; harmless (reads return None)
    # if the daemon can't honour the mode.
    gesture_ticker = GestureTicker(driver) if gesture_enabled else None
    # The per-sentence cue lane and the /api/skill intake must both stand down while
    # the head is an INPUT device: a self-move wipes the push-turn detector's rolling
    # baseline. The ticker is built FROM the driver, so the predicate is bound back
    # into it here rather than passed to its constructor.
    if gesture_ticker is not None:
        driver.set_head_reserved(gesture_ticker.head_reserved_for_sensing)
    # ...and now that the predicate answers honestly, ASSERT both arbiter gates onto the
    # robot node unconditionally. The node's face-look-at run flag is fail-closed (off
    # until somebody asks), and a node that has been up for days may still hold whatever
    # the previous hostapp process left it holding -- so a plain edge-triggered push,
    # which is what every later call is, would have nothing to fire on.
    driver.sync_robot_flags(force=True)
    # The idle scan and push-turn sensing both want the HEAD, and the scan wins by
    # default: it moves yaw further than the selection threshold and each motion
    # resets the detector's baseline, so a human turn is absorbed and nothing
    # happens. Let the sensor tell the scan when the head is an input device.
    idle = (
        IdleTicker(
            driver,
            idle_after_s=idle_after,
            head_reserved=(
                gesture_ticker.head_reserved_for_sensing if gesture_ticker is not None else None
            ),
            still_while_listening=os.environ.get("EMORI_LISTEN_STILL", "1") != "0",
        )
        if ov.get("idle", True) and idle_after > 0
        else None
    )
    def _addon_mic(active: bool) -> None:
        """Session capture gain while an add-on mode holds the mic; idle gain after.

        The restore defers to a live training session -- /session raised the
        profile for its own reasons and owns it until the driver finalizes.
        """

        if active:
            mic_profile.set_profile(SESSION_PROFILE)
        elif not getattr(driver.tracker, "active", False):
            mic_profile.set_profile(IDLE_PROFILE)

    def _training_alive() -> bool:
        """Is the legacy roleplay REALLY running, or only navigated to?

        Nothing outside can see inside the cross-origin Pokamind iframe; the driver's own
        tracker is the one honest fact to read.  ``exchanged`` -- an analysis cycle OR a
        spoken turn -- is the deliberate part: a session that was minted and opened but has
        produced neither is a wedge, while ``cycles`` alone would call a conversation the
        camera stream is not narrating dead.
        """

        tracker = getattr(driver, "tracker", None)
        return bool(tracker is not None and tracker.active and tracker.exchanged)

    extension = ov["extension"] if "extension" in ov else load_extension()

    def _addon_session_ended(session_id: str | None) -> None:
        # Runs inside every finalize, whose remaining work must happen regardless.
        try:
            extension.session_ended(session_id)
        except Exception:  # noqa: BLE001 - an add-on fault must not strand the kiosk
            log.warning("add-on session_ended failed", exc_info=True)

    def _addon_accepts(hook, session_id: str) -> bool:
        """A session bind or open: an add-on that raises has refused."""

        try:
            return hook(session_id) is True
        except Exception:  # noqa: BLE001 - treated as a refusal
            log.warning("add-on session hook failed", exc_info=True)
            return False

    try:
        extension.attach(ExtensionContext(
            driver=driver,
            gesture_ticker=gesture_ticker,
            # The same live switch the pages, the menu and the SSO claim read (one
            # language authority).
            lang_state=lang_state,
            mic_claim=_addon_mic,
            session_alive=_training_alive,
            overrides=ov,
            ros=ov.get("ros"),
        ))
    except Exception:  # noqa: BLE001 - training must remain available
        log.warning("add-on failed to attach; running without it", exc_info=True)
        extension = HostExtension()
    if gesture_ticker is not None:
        gesture_ticker.on_direction = extension.on_head_turn
        gesture_ticker.on_back = extension.on_head_back
        set_training_predicate = getattr(gesture_ticker, "set_training_predicate", None)
        if callable(set_training_predicate):
            set_training_predicate(extension.selection_reserved)

    # A training session can end ROBOT-side: the walk-away fail-safe finalizes inside the
    # driver when Pokamind never answers `emori_cmd:end` within EMORI_END_GRACE_S (a dead
    # iframe).  The /session page has no way to observe that -- it kept polling a dead
    # session forever, the renderer kept spinning, and the kiosk never came back to the
    # badge screen.  So the SAME finalize that notifies any add-on
    # also arms `home`, the page-level verb the page already polls for.
    home_pending = threading.Event()
    # Set for the duration of a finalize the PAGE itself initiated, where `home` would be
    # wrong (see _session_finalized).  Thread-local because that finalize runs on the
    # request thread while the walk-away one runs on the watchdog thread.
    page_owned_end = threading.local()

    def _note_session(kind: str, session_id: str, info: object = None) -> None:
        """Tell the hub's fan-out, when there is one, that a session opened or closed.

        A queue put on a thread that was going to run this anyway, and nothing when no
        fan-out is installed. It never raises: an event is not worth a session.
        """

        fanout = getattr(app.state, "fanout", None)
        if fanout is None:
            return
        try:
            fanout.note(kind, session_id=session_id,
                        person_id=str(getattr(info, "person_id", "") or ""),
                        lang=str(getattr(info, "lang", "") or ""))
        except Exception:  # noqa: BLE001
            log.debug("session event not queued", exc_info=True)

    def _session_finalized(session_id: str) -> None:
        # EVERY ending funnels through here (the page's own completion, walk-away, the
        # watchdog force-end, a new learner preempting the last one), which makes it
        # the one place the session's raised capture gain can be handed back, and it
        # happens before any add-on is told, so none takes the robot back at the raised gain.
        #
        # On its OWN thread: this callback runs inside driver._finalize_locked with
        # driver._lock held, and wpctl is a subprocess with a 5 s timeout -- inline it
        # would freeze every driver entry point (cycles, gestures, the watchdog) for up
        # to 5 s at each session end. MicProfile has its own lock, so a late switch
        # cannot interleave with the next session's 'session' assert out of order worse
        # than wpctl itself resolves.
        def _idle_unless_reclaimed() -> None:
            # This thread can lose the race against the NEXT session's 'session' assert
            # (the mic would stay at idle 0.30 mid-roleplay, every user turn read as
            # silence). If a session is active again, the speaker/mic
            # belong to it - leave the profile alone.
            tracker = getattr(driver, "tracker", None)
            if tracker is not None and tracker.active:
                return
            mic_profile.set_profile(IDLE_PROFILE)

        threading.Thread(target=_idle_unless_reclaimed,
                         name="mic-profile-idle", daemon=True).start()
        _addon_session_ended(session_id)
        last = getattr(driver, "_last_finalized", None)
        closed = last[3].get("session") if last and last[0] == session_id else None
        _note_session("hub_session_finalized", session_id, closed)
        # A page-owned finalize must stay silent: pokamind_session_completed is answered
        # by the page's own finish(), which speaks the summary and then navigates.  A
        # `home` armed here would be picked up by a poll landing between this call and
        # that response -- cutting the summary off and navigating early.
        if getattr(page_owned_end, "active", False):
            return
        home_pending.set()

    set_finalized_callback = getattr(driver, "set_session_finalized_callback", None)
    if callable(set_finalized_callback):
        set_finalized_callback(_session_finalized)
    # One coherent QR window: pending-mint TTL and the handoff fingerprint
    # (deadline + 5 s grace), all from one source.
    qr_nav_timeout_s = claim_timeout_s()

    def _badge_session_held() -> bool:
        tracker = getattr(driver, "tracker", None)
        info = getattr(tracker, "info", None) if tracker is not None else None
        live = (bool(getattr(tracker, "active", False))
                and _QR_SESSION_MARKER in str(getattr(info, "session_id", "") or ""))
        # Only a mint still inside its window: an abandoned one must not refuse every card
        # forever, and the next mint, which sweeps it, is exactly what it would be blocking.
        cutoff = time.time() - qr_nav_timeout_s
        with pending_lock:
            return live or any(key in pending and pending[key].started_at >= cutoff
                               for key in qr_pending)

    qr_handoff = _QrPriorityHandoff(extension, ttl_s=qr_nav_timeout_s + 5.0,
                                    session_held=_badge_session_held)

    # Unregistered-badge cue (default off): a deployment that has explicitly decided to
    # reveal "badge not recognized" gets a reviewed spoken cue + antenna ack.
    # Flag off keeps the opaque 409 that hides roster membership.
    badge_cue_wav: bytes | None = None
    if "badge_cue" in ov:
        badge_cue_wav = ov["badge_cue"]
    elif os.environ.get("EMORI_UNREGISTERED_BADGE_FEEDBACK", "0") == "1":
        try:
            badge_cue_wav = load_badge_cue_wav(
                Path(os.environ.get("EMORI_UNREGISTERED_BADGE_CUE_WAV", "")),
                os.environ.get("EMORI_UNREGISTERED_BADGE_CUE_SHA256", ""),
            )
        except (BadgeCueError, OSError):
            log.warning("unregistered-badge cue unavailable; spoken feedback disabled")

    def _qr_refused(route: str, reason: str) -> None:
        """Say WHY a badge did nothing, in the journal, without saying WHICH badge.

        A scan that the robot refuses is otherwise the quietest failure it has: the
        HTTP body stays opaque on purpose (roster enumeration), the page keeps
        scanning, and the only record of a rejected badge was an access-log 409.  The
        REASON is deployment state, never a payload or an identity, so it is logged at
        WARNING - it is the line an operator standing next to a stuck robot needs.
        """

        log.warning("badge scan refused on %s: %s", route, reason)

    def _unregistered_badge_response(route: str = "qr"):
        if badge_cue_wav is None:
            _qr_refused(route, "training_unavailable (badge not on the roster)")
            return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
        _qr_refused(route, "badge_unrecognized (badge not on the roster)")
        driver.submit_legacy_motion(
            [R.droop_antennas(reason="unregistered badge cue")], drop_if_busy=True
        )
        return JSONResponse({"error_code": "badge_unrecognized"}, status_code=409)

    # Scan greeting (default off): a physical acknowledgement of a successful
    # badge scan at identify time; without it the robot only greets at
    # /session load.  submit_legacy_motion keeps this suppressed whenever an
    # add-on mode owns the robot.
    scan_greeting_enabled = (
        bool(ov["scan_greeting"])
        if "scan_greeting" in ov
        else os.environ.get("EMORI_SCAN_GREETING", "0") == "1"
    )

    # Sessions minted via SSO but whose /session page hasn't loaded yet.
    pending: dict[str, SessionInfo] = {}
    qr_pending: set[str] = set()
    pending_lock = threading.Lock()

    # Speech engines (server-side robot voice + ears). Lazy/optional: the pages fall
    # back to browser speechSynthesis / Web Speech automatically on 503.
    from emori_adapter.hostapp import speech_api

    tts = ov["tts"] if "tts" in ov else speech_api.build_tts()
    stt = ov["stt"] if "stt" in ov else speech_api.build_stt()

    def _speak(text: str, lang: str) -> None:
        _speak_on_robot(tts, text, lang)

    speak = ov["speak"] if "speak" in ov else _speak

    def _robot_idle() -> bool:
        """True only when NOTHING is running on this robot.

        The gate for the hidden language toggle, and deliberately the strictest one in
        here: re-languaging the kiosk mid-anything would swap the menu, the pages and the
        session claim under a person who is already talking to the robot. Every
        uncertainty answers False -- a live training session, a badge lease, a running
        add-on mode, or an add-on that cannot say what state it is in.
        """

        if getattr(driver, "tracker", None) is not None and driver.tracker.active:
            return False
        try:
            return not extension.selection_reserved() and not extension.busy()
        except Exception:  # noqa: BLE001 - an unreadable state stays conservative
            return False

    def set_kiosk_language(target: str, *, reason: str = "") -> str:
        """Re-language the whole kiosk, live. Returns the language now in force.

        The ONE implementation of the switch, shared by the hidden head gesture and the
        phone superadmin's /api/admin/lang: it is a SEQUENCE, not a setter, and half of
        it (a state file with no driver stamp, or no reload verb) leaves the kiosk
        serving the old language until it restarts.

        Both callers run on threads that must not block -- the gesture ticker parked in
        pw-play is a robot that stops sensing for the length of the line it is saying --
        so everything here is either instant or handed to another thread.
        """

        previous = lang_state.current()
        try:
            lang_state.set(target)
        except ValueError:  # gesture: unreachable; api: rejected before it gets here
            log.warning("refusing to switch the kiosk language to %r", target)
            return previous
        # Envelopes that carry no session language of their own are stamped with this;
        # leaving it behind gets a summary read out by the other language's voice.
        driver.default_lang = target
        # The landing page is rendered in the OLD language and has no other way to find
        # out. It polls for exactly this verb (pages.VOICE_HTML) and reloads itself.
        driver.enqueue_cmd(PAGE_RELOAD_CMD)
        log.warning("kiosk language %s -> %s (%s)", previous, target,
                    reason or "idle double-turn gesture")
        line = _LANG_TOGGLE_LINES.get(target, "")
        if line:
            landing_seen.hold(_LANG_LINE_S)
            threading.Thread(target=speak, args=(line, target),
                             name="lang-toggle-speak", daemon=True).start()
        return target

    scope_lang_hooks["switch"] = set_kiosk_language
    scope_lang_hooks["idle"] = _robot_idle

    def _toggle_kiosk_language() -> None:
        """Hidden idle double-turn gesture: switch to the language we are NOT in."""

        set_kiosk_language(other_lang(lang_state.current()))

    if gesture_ticker is not None:
        # Wired here rather than with on_direction above because the switch needs the
        # speech engines, which are built last.
        gesture_ticker.on_lang_toggle = _toggle_kiosk_language
        set_idle_predicate = getattr(gesture_ticker, "set_idle_predicate", None)
        if callable(set_idle_predicate):
            set_idle_predicate(_robot_idle)

    @asynccontextmanager
    async def lifespan(_app):
        """Own every worker in one bounded, restart-safe hostapp lifecycle."""

        heartbeat_task = None
        await hub.startup()
        try:
            # Nothing is running yet, so any session row still marked `active`
            # belongs to a run that died without finalizing (a power cut, a
            # restart mid-conversation). Left alone they are immortal: they stay
            # in progress, are counted in every session total and are excluded
            # from every completion rate. One indexed
            # UPDATE, off the event loop because it is an SD-card write.
            try:
                orphans = await asyncio.to_thread(driver.metrics.close_orphan_sessions)
                if orphans:
                    log.warning("closed %d session(s) left active by a previous run "
                                "(status=abandoned, end_reason=interrupted)", orphans)
            except Exception:  # noqa: BLE001 - housekeeping never blocks boot
                log.warning("could not close orphaned session rows")
            # Boot into the idle profile once: a restart inherits whatever level the
            # previous run (or a human debugging with wpctl) left the source at. to_thread: wpctl is
            # a subprocess with a 5 s timeout and this is the asyncio event loop.
            await asyncio.to_thread(mic_gate.start)
            await asyncio.to_thread(mic_profile.set_profile, IDLE_PROFILE)
            try:
                await extension.start()
            except Exception:  # noqa: BLE001 - training must remain available
                log.warning("add-on startup unavailable")
            # The voice tap first: a sensor, not a worker, and the player reads it
            # from the first speech plan on. Best-effort inside (no pw-record -> a
            # single WARNING, fixed pacing).
            if motion_trace.default_enabled():
                motion_trace.start()
            start_voice = getattr(voice_link, "start", None)
            if callable(start_voice):
                try:
                    start_voice()
                except Exception:  # noqa: BLE001 - the tap is optional
                    log.warning("voice link failed to start; fixed pacing")
            for ticker in (idle, watchdog_ticker, gesture_ticker):
                start = getattr(ticker, "start", None)
                if callable(start):
                    start()
            # This robot's Pokamind scope: a daemon thread whose first sync waits 10 s
            # (the kiosk comes up first) and which never restarts anything itself.
            if scope_background:
                start_scope = getattr(scope_sync, "start_background", None)
                if callable(start_scope):
                    try:
                        start_scope()
                    except Exception:  # noqa: BLE001 - the last-good snapshot stays
                        log.warning("pokamind scope sync could not start")
            # Uptime evidence for the management dashboard: present beats / expected
            # beats. An asyncio task (not another thread) because it is pure I/O on a
            # slow cadence, and its one blocking call already goes to a worker thread.
            heartbeat_s = metrics_store.heartbeat_interval_s()
            if heartbeat_s > 0:
                heartbeat_task = asyncio.create_task(
                    metrics_store.heartbeat_loop(
                        driver.metrics, interval_s=heartbeat_s,
                        session_active=lambda: driver.tracker.active),
                    name="dashboard-heartbeat")
            yield
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Stop add-on ownership first, so the remaining workers have nothing left to do.
            try:
                await extension.shutdown()
            except Exception:  # noqa: BLE001 - continue bounded teardown
                log.warning("add-on shutdown incomplete")
            stop_scope = getattr(scope_sync, "stop", None)
            if callable(stop_scope):
                try:
                    stop_scope(timeout=1.0)
                except Exception:  # noqa: BLE001 - continue bounded teardown
                    log.warning("pokamind scope sync did not stop cleanly")
            motion_trace.stop()
            for ticker in (gesture_ticker, watchdog_ticker, idle, voice_link, mic_gate):
                stop = getattr(ticker, "stop", None)
                if callable(stop):
                    stop()
            for ticker in (gesture_ticker, watchdog_ticker, idle, voice_link):
                join = getattr(ticker, "join", None)
                if callable(join):
                    join(timeout_s=1.0)
            shutdown_driver = getattr(driver, "shutdown", None)
            if callable(shutdown_driver):
                shutdown_driver(timeout_s=1.0)
            player = getattr(driver, "player", None)
            stop_player = getattr(player, "stop", None)
            if callable(stop_player):
                stop_player()
            join_player = getattr(player, "join", None)
            if callable(join_player):
                join_player(timeout_s=2.0)
            await hub.shutdown()
            close_client = getattr(client, "close", None)
            if callable(close_client):
                close_client()

    app = FastAPI(title="EMORI host app", lifespan=lifespan)
    def _live_session_owns_the_speaker() -> bool:
        # Mid-session = a tracked session that has exchanged at least one cycle. The
        # cycles>0 guard keeps the pre-first-cycle window open: the landing page's
        # "Starting <topic>..." announcement and greeting are minted-session territory
        # (start_session ran) but the roleplay has not yet spoken.
        # `exchanged`, not `cycles > 0`: cycles come ONLY from the camera analysis
        # stream, and a real session can have zero cycles for its whole length --
        # the interlock would be open while the roleplay was speaking. The
        # AI's first line counts too, which keeps the pre-roleplay window exactly as
        # open as before (the announcement/greeting precede the iframe's first line).
        tracker = getattr(driver, "tracker", None)
        return bool(tracker is not None and tracker.active and tracker.exchanged)

    def _session_boot_progress():
        # An opening/warmup lifecycle stage arrived from the kiosk: the session
        # is provably booting (a cold server can take 30 s or more to warm up).
        # Count it as activity so the walk-away timer measures from
        # the boot's LAST sign of progress, not from the badge scan.
        if driver.tracker.active:
            driver.touch()

    speech_api.mount(app, tts=tts, stt=stt,
                     session_active=_live_session_owns_the_speaker,
                     on_boot_progress=_session_boot_progress,
                     on_reprompt_stage=getattr(watchdog_ticker, "on_reprompt_stage", None))

    # The ros4hri skill facade's ONLY way to move the robot: it never POSTs the daemon,
    # so the single-writer rule holds and every trap RobotPlayer closes stays closed.
    # Off unless EMORI_SKILL_API=1 AND EMORI_SKILL_KEY is set -- on a shipped robot there
    # is no route at all. Loopback-only and rate-limited; see hostapp/skill_api.py.
    skill_api.mount(app, driver=driver, head_reserved=driver.head_reserved)

    def _addon_status() -> dict | None:
        try:
            return extension.status()
        except Exception:  # noqa: BLE001 - diagnostics must never break a page
            return None

    # Management dashboard: /dashboard + /api/dashboard/*, read-only over the same
    # metrics store the driver writes. Serves a 503 "not configured" page until
    # EMORI_DASH_PASSWORD is set -- there is no anonymous mode.
    dashboard.mount(app, store=driver.metrics, driver=driver,
                    addon_status=_addon_status,
                    # The participation KPI's denominator -- a COUNT, never the
                    # roster: the dashboard must not be able to read a name.
                    # A callable, so a roster reloaded at runtime would be picked
                    # up without restarting the hostapp.
                    roster_size=lambda: roster.staff_count)

    # The phone's Pokamind tab: status, live whoami, replace, reveal. Writes go
    # through THIS process (emori-admin never stores robot credentials), and take
    # effect at the restart emori-admin then asks for -- the same binding rule as
    # every other credential here.
    pokamind_admin = (ov["pokamind_admin"] if "pokamind_admin" in ov
                      else admin_api.PokamindAdmin(
                          load=ROBOT_TOKEN_LOAD,
                          base_url=POKAMIND_BASE,
                          env_robot_id=os.environ.get("EMORI_ROBOT_ID", ""),
                          scope=scope_sync))
    # The phone's People tab: the badge roster, edited in place and reloaded live (the
    # SAME Roster object every badge route above resolves against), each e-mail checked
    # against this robot's Pokamind identity_domains.
    roster_admin = RosterAdmin(roster=roster, path=_roster_write_path(),
                               scope=getattr(pokamind_admin, "current_whoami", None))

    # The phone superadmin (the standalone emori-admin service on :8070) reaches in
    # here for the ONE thing it cannot do over HTTP or a subprocess: the language
    # switch is a sequence over these live in-process objects. Token-gated, and 503
    # until EMORI_ADMIN_TOKEN is set -- there is no anonymous mode.
    admin_api.mount(app, lang_state=lang_state, driver=driver, mic_profile=mic_profile,
                    robot_idle=_robot_idle,
                    # A lambda so the journal still says WHICH input re-languaged the
                    # robot: a phone switch and a head gesture look identical after
                    # the fact, and only one of them means somebody touched the head.
                    set_lang=lambda code: set_kiosk_language(code, reason="phone admin"),
                    addon_status=_addon_status,
                    extension=extension,
                    # The robot's own voice, for the Menu tab's preview only.
                    speak=speak,
                    pokamind=pokamind_admin, roster=roster_admin,
                    # Settings, scope, menus: the files are written HERE (emori-admin
                    # holds no credential); nothing in them restarts this process.
                    scope=scope_sync, site=site_runtime,
                    menus=admin_api.MenuAccess(
                        source=menu_source, path=_menu_source, site_path=site_menu_path,
                        load=lambda code: _menu_for(code)[0], clear=_clear_menu_cache))

    # expose for tests / debugging
    app.state.scope_sync = scope_sync
    app.state.site_runtime = site_runtime
    app.state.driver = driver
    app.state.hub = hub
    app.state.extension = extension
    try:
        extension.register_routes(app)
    except Exception:  # noqa: BLE001 - training must remain available
        log.warning("add-on routes failed to register", exc_info=True)
    app.state.lang_state = lang_state
    app.state.gesture_ticker = gesture_ticker
    app.state.watchdog_ticker = watchdog_ticker
    # Where a start asked for from outside the browser leaves the page it should land on.
    # Empty unless something asked, and `/` is unchanged while it is.
    app.state.start_slot = StartSlot()
    landing_seen = LandingSeen()
    # The management dashboard (mounted separately) reads through this store.
    app.state.metrics = driver.metrics
    # What a process hosting this app reaches for, by name, instead of copying the wiring:
    # the same closures and objects the routes use.
    app.state.glue = SimpleNamespace(
        speak=speak,
        tts=tts,
        stt=stt,
        mic_profile=mic_profile,
        robot_idle=_robot_idle,
        set_language=set_kiosk_language,
        live_session=_live_session_owns_the_speaker,
        tt_store=tt_store,
        menu_for=_menu_for,
        landing_idle=landing_seen.idle,
        discard_minted=lambda session_id, qr="": _discard_minted(session_id, qr),
    )

    def _discard_minted(session_id: str, raw_qr: str = "") -> None:
        """Undo a mint nobody will open, as voice_select does for one it cannot finish."""

        with pending_lock:
            pending.pop(session_id, None)
            bound = session_id in qr_pending
            qr_pending.discard(session_id)
        if bound:
            _addon_session_ended(session_id)
        qr_value = _decoded_qr_payload(raw_qr) if raw_qr else None
        if qr_value is not None:
            qr_handoff.release(qr_value)

    def _session_url(ident: Identity, *, bind_training: bool = False) -> dict | None:
        sid = _mint_session_id(ident.student_id, qr_bound=bind_training)
        # The kiosk's LIVE language, for every session: badge, anonymous drop-in or QR
        # alike. A roster badge does not pin its own `lang` here, because a per-badge
        # language would re-language
        # ONE surface (this session) while the spoken cues, the menu and the voice
        # everyone in the room just heard stay on the switch -- and nobody standing at
        # a screenless robot can see which badge caused it. `identity` still parses the
        # roster field (older rosters carry it); nothing reads it for the session. One
        # value drives BOTH the robot's own session language and the `emori_lang` claim
        # Pokamind opens the session in, so the two can never disagree. Read per claim,
        # so a session started after the toggle gesture opens in the language people
        # just heard.
        lang = lang_state.current()
        info = SessionInfo(
            session_id=sid, person_id=ident.student_id, name=ident.name or "",
            email=ident.email, lang=lang, module_id=ident.module_id or "",
            anonymous=ident.anonymous,
        )
        expired_qr_sessions: list[str] = []
        with pending_lock:
            # Drop stale pending mints (an SSO minted but never opened).
            now = time.time()
            for k, value in tuple(pending.items()):
                timeout_s = qr_nav_timeout_s if k in qr_pending else 600
                if value.started_at >= now - timeout_s:
                    continue
                del pending[k]
                if k in qr_pending:
                    qr_pending.discard(k)
                    expired_qr_sessions.append(k)
            while sid in pending:
                sid = _mint_session_id(ident.student_id, qr_bound=bind_training)
                info.session_id = sid
            pending[sid] = info
            # With the mint, so no other card can see an unmarked one (_badge_session_held).
            if bind_training:
                qr_pending.add(sid)
        for stale_session_id in expired_qr_sessions:
            _addon_session_ended(stale_session_id)
        if bind_training and not _addon_accepts(extension.bind_session, sid):
            with pending_lock:
                pending.pop(sid, None)
                qr_pending.discard(sid)
            return None
        if DEMO or ROBOT_TOKEN is None:
            # No token = nothing to sign a login with: a demo session, never a call to
            # Pokamind (demo_default already forces DEMO; this is the belt).
            return {"demo": True, "anonymous": ident.anonymous, "name": ident.name,
                    "session_url": f"/session?demo=1&sid={sid}"}
        # THIS robot's own token (EdDSA, kid = robot_id, 600 s,
        # aud=pokamind-partner-sso: Pokamind checks the robot's scope before any login).
        mint = _robot_sso_minter(ROBOT_TOKEN)
        try:
            token = mint(student_id=ident.student_id, email=ident.email,
                         course_id=ident.course_id, module_id=ident.module_id,
                         pathway=ident.pathway, name=ident.name or None,
                         # The `emori_kiosk` claim: screenless turn-taking is driven by
                         # EXPLICIT events, not a timer (Begin only AFTER the question's
                         # audio truly ends via pokamind_tts_ended; Submit only during
                         # USER_SPEAKING), so the mic is never opened over the question.
                         # The claim enables the device-check auto-advance, the in-iframe
                         # turn-taking hooks, and end-of-session `results` forwarding.
                         # Pokamind honours it only for partners with kiosk mode enabled,
                         # so other partners are unaffected.
                         extra={"emori_lang": lang, "emori_kiosk": True})
            sso = build_sso_url(POKAMIND_BASE, PARTNER, token)
        except Exception as e:
            if not bind_training:
                raise
            # A claimed badge the login cannot be signed for: leave nothing held.
            log.warning("badge session not minted (%s)", type(e).__name__)
            with pending_lock:
                pending.pop(sid, None)
                qr_pending.discard(sid)
            _addon_session_ended(sid)
            return None
        # URL-encode the SSO URL: it contains its own ?token=...&partner=..., which would
        # otherwise be split off as separate /session params (dropping &partner -> "Missing
        # partner parameter"). URLSearchParams.get('sso') decodes it back in the page.
        return {"demo": False, "anonymous": ident.anonymous, "name": ident.name,
                "session_url": f"/session?sso={quote(sso, safe='')}&sid={sid}", "sso_url": sso}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "demo": DEMO, "partner": PARTNER,
                # WHICH credential is in force, never the credential: readycheck's
                # pokamind_token gate compares it with the token on disk to catch a
                # replaced token the running process has not picked up yet -- the
                # kind for a first token, the key fingerprint for a rotated one.
                "pokamind_credential": pokamind_credential(),
                "pokamind_key_fingerprint": pokamind_key_fingerprint(),
                "roster_staff": len(roster._staff), "robot": driver.client.health(),
                # Which transport reaches the robot: a client that says "ros" is the ROS
                # link, anything else is REST. Read through a wrapper too.
                "runtime": _client_runtime(driver.client)}

    @app.get("/api/robot/status")
    def robot_status(request: Request):
        """The robot's status for tools on this machine, on either transport.

        Loopback only, like the credential routes: it says which service is missing and
        who owns motion, and it has no reason to answer the LAN.
        """
        if not admin_api._is_loopback(getattr(request.client, "host", None)):
            return JSONResponse({"error": "local_only"}, status_code=403)
        read = getattr(driver.client, "robot_status", None)
        if callable(read):
            body = dict(read())
        else:
            body = {"state": "running" if driver.client.health() else "error",
                    "first_unmatched": ""}
        body.update({"runtime": _client_runtime(driver.client),
                     "motion_owner": skill_api.motion_owner(),
                     "skill_transport": (os.environ.get("EMORI_SKILL_TRANSPORT", "")
                                         or "").strip().lower()})
        # Which nodes a hub hosts and which it could not build; not there under the hostapp.
        hub_nodes = getattr(app.state, "hub_nodes", None)
        if hub_nodes is not None:
            body["hub_nodes"] = hub_nodes
        hub_deployed = getattr(app.state, "hub_deployed", None)
        if hub_deployed is not None:
            body["hub_deployed"] = hub_deployed
        return body

    @app.get("/api/status")
    def status():
        st = driver.status()
        st.update({"relay_clients": hub.client_count, "demo": DEMO,
                   "robot": driver.client.health(),
                   # "the robot cannot hear me" is the single most likely on-robot
                   # question, and the capture gain is now mode-dependent: say which
                   # profile is actually applied (None = wpctl never took).
                   "mic_profile": mic_profile.profile,
                   "mic_gate": mic_gate.snapshot(),
                   "tts": getattr(tts, "available", tts is not None),
                   "stt": getattr(stt, "available", stt is not None)})
        # The voice tap, so "is the motion actually locked to the voice on this robot"
        # is one curl away: available (pw-record delivering), active, level, onsets.
        st["motion_trace"] = motion_trace.snapshot()
        snap = getattr(voice_link, "snapshot", None)
        if callable(snap):
            try:
                st["voice_link"] = snap()
            except Exception:  # noqa: BLE001 - status must never fail on a sensor
                st["voice_link"] = {"enabled": True, "available": False}
        else:
            st["voice_link"] = {"enabled": False, "available": False}
        return st

    @app.get("/api/addon/status")
    def addon_status():
        status = _addon_status()
        if status is None:
            return JSONResponse({"error_code": "addon_unavailable"}, status_code=503)
        return JSONResponse(status)

    @app.get("/api/addon/hints")
    def addon_hints():
        """Lines for the landing page; empty with no add-on, or one with nothing to offer."""
        try:
            lines = [str(line) for line in extension.hint_lines(lang_state.current())]
        except Exception:  # noqa: BLE001 - a hint must never break the page
            lines = []
        return JSONResponse({"lines": lines})

    # Latest frame the SCANNER saw, published by the page itself. Memory-only, single slot.
    # /dev/video40 is exclusive_caps=1, so the only other way to look is to stop the kiosk
    # and take the camera off the person holding a badge -- and that shows the raw camera,
    # not the frame the decoder was handed. Gated like the other debug routes: a camera
    # frame is the most sensitive thing here, so it never touches disk.
    _qr_frame: dict[str, bytes] = {}
    _QR_FRAME_MAX = 2 * 1024 * 1024

    def _qr_frame_enabled() -> bool:
        return os.environ.get("EMORI_QR_FRAME_DEBUG", "0") == "1"

    def _decode_qr_jpeg(jpeg: bytes) -> str:
        """Best-effort server-side QR decode of one camera JPEG.

        Returns the payload or ''. Missing libraries degrade to '' - the page's own
        jsQR ladder keeps working exactly as before, this is a strictly-better second
        reader, not a dependency.
        """
        try:
            import io

            import zxingcpp
            from PIL import Image

            results = zxingcpp.read_barcodes(Image.open(io.BytesIO(jpeg)))
            return str(results[0].text) if results else ""
        except Exception:  # noqa: BLE001 - a decode failure must never break the upload
            return ""

    @app.post("/api/qr-frame")
    async def qr_frame_put(request: Request):
        body = await request.body()
        if len(body) > _QR_FRAME_MAX:
            return JSONResponse({"error_code": "frame_too_large"}, status_code=413)
        # Retention stays debug-gated (a stored camera frame is sensitive); DECODING is
        # not retention and always runs. zxing-cpp reads the hand-held frames jsQR cannot:
        # phone-badge frames (small, angled, daylight) decode server-side where the
        # in-page decoder reads nothing.
        if _qr_frame_enabled():
            _qr_frame["jpeg"] = body
        decoded = await asyncio.to_thread(_decode_qr_jpeg, body)
        out = {"ok": True, "bytes": len(body)}
        if decoded:
            out["decoded"] = decoded
        return out

    @app.get("/api/qr-frame")
    def qr_frame_get():
        if not _qr_frame_enabled():
            return JSONResponse({"error_code": "debug_disabled"}, status_code=403)
        frame = _qr_frame.get("jpeg")
        if not frame:
            return JSONResponse({"error_code": "no_frame"}, status_code=404)
        return Response(content=frame, media_type="image/jpeg")

    @app.post("/api/badge/claim")
    async def badge_claim(payload: dict):
        """Accept a decoded QR only after validation and roster lookup.

        The body intentionally contains no camera image; this is the sole
        path from an already-decoded browser QR string into a session claim.  Its response is content-free so a badge value can never
        be reflected through a browser, status page, or log.
        """

        value = _decoded_qr_payload(payload.get("payload"))
        if value is None:
            _qr_refused("badge/claim", "invalid_qr (empty, oversized or unprintable decode)")
            return JSONResponse({"error_code": "invalid_qr"}, status_code=400)
        if roster.resolve(value).anonymous:
            # Do not use an arbitrary camera decode to preempt anything running.
            # With feedback DISABLED this response also does
            # not reveal roster membership; enabling the flag is the recorded
            # decision to reveal it (EMORI_UNREGISTERED_BADGE_FEEDBACK in .env.example).
            return _unregistered_badge_response("badge/claim")
        result = await asyncio.to_thread(qr_handoff.reserve, value)
        if result == "accepted":
            return JSONResponse({"accepted": True}, status_code=202)
        if result == "already_claimed":
            _qr_refused("badge/claim", "qr_already_claimed (this card already started one)")
            return JSONResponse({"error_code": "qr_already_claimed"}, status_code=409)
        _qr_refused("badge/claim", "training_unavailable (the handover was refused)")
        return JSONResponse({"error_code": "training_unavailable"}, status_code=409)

    @app.get("/api/badge-cue")
    def badge_cue():
        """Serve the reviewed unregistered-badge cue (static, content-free)."""

        if badge_cue_wav is None:
            return JSONResponse({"error_code": "cue_unavailable"}, status_code=404)
        return Response(content=badge_cue_wav, media_type="audio/wav")

    @app.get("/api/pending-cmd")
    def pending_cmd(verbs: str = "", phase: str = "", page: str = ""):
        # The kiosk /session page polls this and relays any queued command into the
        # Pokamind iframe. Commands are enqueued adapter-side by the gesture sensor
        # (head-tilt -> begin/end) and the walk-away watchdog. Single-slot, drain-on-read.
        # ``tt_rev`` piggybacks on the SAME 250 ms poll: the page re-fetches
        # /api/tt-overrides only when the revision moves, so a tunable kiosk costs
        # exactly zero extra requests while nobody is tuning it.
        #
        # ``verbs`` narrows the drain to what the CALLER can act on. The landing page
        # polls with ``verbs=reload`` (its language changed underneath it): one shared
        # single slot plus an unfiltered drain is how a second open page silently
        # swallows the `end` a live /session is waiting for.
        if phase:
            landing_seen.note(phase, page)
            if phase != LANDING_IDLE_PHASE and app.state.start_slot.waiting():
                # Someone began using this page after a start from outside was accepted. The
                # person comes first: the start is undone, and the page is not reloaded.
                app.state.start_slot.clear()
                driver.pop_cmd(only=frozenset({PAGE_RELOAD_CMD}))
        wanted = frozenset(verb for verb in verbs.split(",") if verb) or None
        cmd = driver.pop_cmd(only=wanted)
        # `home` is STICKY, unlike the single-slot begin/end verbs, and it OUTRANKS a
        # queued one.  Sticky because it is the only way a page learns its session died
        # robot-side, so one dropped poll (a stalled CM4 renderer, a lost response) would
        # re-wedge exactly the page it exists to free; outranking because once the session
        # is finalized any begin/end still in the slot is addressed to a session that no
        # longer exists (the walk-away path always leaves its own `end` there).  It is
        # cleared when the next /session page opens.  One navigation per page is the
        # page's `over` latch, not the delivery count.
        if home_pending.is_set() and (wanted is None or "home" in wanted):
            cmd = "home"
        if cmd:
            # A served verb navigates or ends a page; without this line the journal
            # cannot distinguish "the page left on its own" from "we told it to"
            # (a /session that bounces home in 0.5 s, with the verb that did it
            # invisible).
            log.warning("pending-cmd served %r (wanted=%s)", cmd,
                        ",".join(sorted(wanted)) if wanted else "any")
        return {"cmd": cmd, "tt_rev": tt_store.snapshot()["rev"]}

    @app.get("/api/tt-overrides")
    def tt_overrides_read():
        # Same-origin read for the /session page. Carries only what an operator
        # explicitly set: no secrets, no session data, nothing to leak.
        return tt_store.snapshot()

    @app.post("/api/tt-overrides")
    def tt_overrides_write(payload: dict):
        # Deployment-gated WRITE: a shipped kiosk must not be re-tunable over the
        # network unless a human opted in, same posture as the other debug routes.
        if os.environ.get("EMORI_TT_INJECT_HTTP", "0") != "1":
            return JSONResponse({"error_code": "tt_inject_disabled"}, status_code=403)
        try:
            return JSONResponse(tt_store.set(payload.get("patch")), status_code=202)
        except TtPatchError as e:
            return JSONResponse({"error_code": e.code}, status_code=400)

    @app.post("/api/cycle")
    def cycle(payload: dict):
        t = payload.get("type")
        if not isinstance(t, str) or not t.startswith("pokamind_"):
            return {"ok": True, "ignored": str(t)}
        stamped = driver.stamp(payload)
        if t == AI_UTTERANCE_EVENT:
            # The driver plans this sentence's motion FIRST and stamps the family it
            # performs (cue_family / cue_family_source / cue_slug / cue_category, with
            # Pokamind's own in cue_category_authored) onto the envelope, THEN the envelope is
            # recorded and fanned out -- so the ROS 2 SpeechCue and the recording
            # describe the motion that actually happened, not only the wire input.
            out = driver.on_utterance(stamped)
            recorder.write(stamped.get("session_id", ""), stamped)
            hub.publish(json.dumps(stamped, ensure_ascii=False))
            return out
        recorder.write(stamped.get("session_id", ""), stamped)
        hub.publish(json.dumps(stamped, ensure_ascii=False))
        if t == ANALYSIS_EVENT:
            return driver.on_analysis(stamped)
        if t == TTS_ENDED_EVENT:
            return driver.on_tts_ended(stamped)
        if t == TURN_ARMED_EVENT:
            return driver.on_turn_armed(stamped)
        if t == TURN_SUBMITTED_EVENT:
            return driver.on_turn_submitted(stamped)
        if t == TURN_REJECTED_EVENT:
            return driver.on_turn_rejected(stamped)
        if t == USER_SPEAKING_EVENT:
            return driver.on_user_speaking_activity(stamped)
        if t == COMPLETED_EVENT:
            _maybe_capture_results(json.dumps(stamped))
            # The page owns this ending: its finish() speaks the summary carried in the
            # response below and navigates itself, so the finalize must not arm `home`.
            page_owned_end.active = True
            try:
                stats = driver.on_completed(stamped)
            finally:
                page_owned_end.active = False
            if stats.get("ignored"):
                # A completion for a session that is gone and cannot be salvaged: no
                # report email, no summary, and nothing for the page to navigate to.
                return {"ok": True, "ignored": stats["ignored"]}
            if on_session_end is not None:
                try:
                    on_session_end(stats)
                except Exception as e:  # noqa: BLE001 - email failure never blocks the kiosk
                    log.warning("session-end hook failed: %s", e)
            summary = stats.get("summary", "")
            return {"ok": True, "completed": True, "summary": summary,
                    "summary_tts_url": f"/api/tts?lang={stats.get('lang', 'en')}"
                                       f"&text={quote(summary, safe='')}",
                    "next_url": "/",
                    "avg_engagement": stats.get("avg_engagement"),
                    "dominant_style": stats.get("dominant_style")}
        return {"ok": True, "relayed": t}

    @app.post("/api/voice-cover")
    def voice_cover(payload: dict):
        hold = getattr(driver.player, "hold_voice", None)
        if callable(hold):
            hold(bool(payload.get("on")))
        return {"ok": True}

    @app.post("/api/session-start-failed")
    def session_start_failed(payload: dict):
        """The /session page waited for the iframe and it never said anything.

        The page owns this deadline (it is the only thing that can see the frame), but
        the ROBOT owns what the failed start left behind: a tracked session, a training
        lease, and the raised session mic gain.  Running the ordinary finalize chain is
        what hands all three back -- ``_session_finalized`` releases the lease and
        returns the mic profile to idle -- so the next badge scan works.

        Deliberately narrow: ``abort_unstarted_session`` refuses once the session has
        exchanged a cycle, so a page that mis-fires against a live conversation cannot
        end it.  Idempotent, and the reason is validated to a safe code before it can
        reach a log line.
        """

        reason = payload.get("reason")
        code = (reason if type(reason) is str and _SAFE_STATUS_CODE_RE.fullmatch(reason)
                else "session_never_started")
        aborted = driver.abort_unstarted_session(code)
        return JSONResponse({"aborted": bool(aborted)}, status_code=202)

    @app.post("/api/frame-alive")
    def frame_alive():
        """The /session page saw the FIRST pokamind_* message from the iframe.

        Mirrors the page's own deadline disarm into the adapter-side watchdog:
        its ``exchanged`` disarm (first analysis cycle or AI line) lags a live
        frame by design, and without this the ticker would kill a healthy boot
        at +57s with no log line.  Alive is not exchanged: the ticker keeps every
        later fail-safe; it only stops claiming a live frame "never started".

        The frame coming alive is also ACTIVITY: a slow Pokamind boot (a cold
        server plus scenario generation) can mount at +40s, and the blind
        EMORI_WALKAWAY_S=45 inactivity timer would then kill the session at
        exactly open+45s -- mid-boot, mid-intro, while the learner stands right
        there. touch() restarts that 45s budget from the mount, which the
        intro's first utterance then keeps refreshing. A frame that mounts and
        THEN goes silent forever is still ended by the same timer 45s later.
        """

        if driver.tracker.active and driver.frame_alive_at is None:
            driver.frame_alive_at = time.monotonic()
            driver.touch()
            log.info("session frame alive (first pokamind_* relayed)")
        return {"ok": True}

    @app.post("/api/start")
    async def start(payload: dict):
        sid = str(payload.get("student_id", "learner_01"))
        ident = Identity(student_id=sid, email=str(payload.get("email") or f"{sid}@example.org"),
                         course_id=COURSE_ID, module_id=payload.get("module_id") or None)
        return JSONResponse(_session_url(ident))

    @app.post("/api/identify")
    async def identify(payload: dict):
        # The kiosk scans a QR staff badge (browser jsQR) and POSTs the decoded payload.
        # A non-empty badge must resolve to staff before it can claim training;
        # only the explicit empty path starts an anonymous shared-robot session.
        raw = payload.get("payload") if "payload" in payload else payload.get("qr", "")
        lookup_only = payload.get("lookup_only") is True
        if raw == "":
            ident = roster.resolve("")
            if lookup_only:
                return JSONResponse({"anonymous": True, "name": ""})
            return JSONResponse(_session_url(ident))
        value = _decoded_qr_payload(raw)
        if value is None:
            _qr_refused("identify", "invalid_qr (empty, oversized or unprintable decode)")
            return JSONResponse({"error_code": "invalid_qr"}, status_code=400)
        ident = roster.resolve(value)
        if ident.anonymous:
            return _unregistered_badge_response("identify")
        if lookup_only:
            result = await asyncio.to_thread(qr_handoff.reserve, value)
            if result == "accepted":
                return JSONResponse({"anonymous": ident.anonymous, "name": ident.name or ""})
            if result == "already_claimed":
                _qr_refused("identify", "qr_already_claimed (this card already started one)")
                return JSONResponse({"error_code": "qr_already_claimed"}, status_code=409)
            _qr_refused("identify", "training_unavailable (the handover was refused)")
            return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
        result = await asyncio.to_thread(qr_handoff.claim, value)
        if result != "claimed":
            code = "qr_already_claimed" if result == "already_claimed" else "training_unavailable"
            _qr_refused("identify", f"{code} (claim refused)")
            return JSONResponse({"error_code": code}, status_code=409)
        out = _session_url(ident, bind_training=True)
        if out is None:
            await asyncio.to_thread(qr_handoff.release, value)
            _qr_refused("identify", "training_unavailable (the training lease could not be bound)")
            return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
        if scan_greeting_enabled:
            driver.submit_legacy_motion(
                R.greeting_gesture(reason="badge scan greeting"), drop_if_busy=True
            )
        return JSONResponse(out)

    @app.get("/api/voice-menu")
    def voice_menu():
        # Expose the spoken menu so the landing page can render the prompt + fallback choices.
        menu, tuning = _menu_for()
        return {"prompt": menu.prompt,
                "grammar": tuning["grammar"],
                "options": [{"key": k, "name": o.name} for k, o in menu.options.items()]}

    @app.post("/api/voice-select")
    async def voice_select(payload: dict):
        # Screenless verbal topic pick: the page sends the recognised utterance; we map it
        # to a menu option and start that Pokamind module directly (no topic screen).
        # The LIVE menu: the page that heard this utterance was rendered from the same one.
        menu, _ = _menu_for()
        text = str(payload.get("text", ""))
        key = menu.match(text)
        exact = key is not None
        if key is None and menu.default in menu.options:
            key = menu.default  # configured fallback when nothing is recognised
        if key is None:
            return JSONResponse({"matched": False, "prompt": menu.prompt})
        opt = menu.options[key]
        # Identity stays anonymous by default; an optional badge payload can attach a staff
        # identity. The chosen module overrides whatever the roster assigned. For an
        # anonymous drop-in (no badge) use the deployment's configured course.
        raw_qr = payload.get("qr", "")
        if raw_qr == "":
            qr_value = None
        else:
            qr_value = _decoded_qr_payload(raw_qr)
            if qr_value is None:
                return JSONResponse({"error_code": "invalid_qr"}, status_code=400)
        ident = roster.resolve(qr_value or "")
        if qr_value is not None and ident.anonymous:
            return _unregistered_badge_response("voice-select")
        upd = {"module_id": opt.module_id}
        if ident.anonymous:
            upd["course_id"] = COURSE_ID
        ident = ident.model_copy(update=upd)
        selection = {"matched": True, "exact": exact, "key": key,
                     "topic": opt.name, "module_id": opt.module_id}
        # Speech recognition can intentionally return a default topic while
        # the page asks the user to try again.  A preview must never consume
        # the QR's one training handoff or mint a throwaway SSO session.
        if payload.get("preview") is True:
            return JSONResponse(selection)
        if qr_value is not None:
            result = await asyncio.to_thread(qr_handoff.claim, qr_value)
            if result != "claimed":
                code = "qr_already_claimed" if result == "already_claimed" else "training_unavailable"
                return JSONResponse({"error_code": code}, status_code=409)
        out = _session_url(ident, bind_training=qr_value is not None)
        if out is None:
            if qr_value is not None:
                await asyncio.to_thread(qr_handoff.release, qr_value)
            return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
        out.update(selection)
        return JSONResponse(out)

    def _page(template: str, lang: str = "", extra: dict[str, str] | None = None) -> str:
        # ``lang`` overrides the kiosk language for ONE page render: a roster badge may
        # pin its own (a bilingual site runs one robot), and the /session page speaks the
        # summary through /api/tts?lang= -- an en summary read by the ca voice otherwise.
        code = lang or lang_state.current()
        # The number WORDS follow the page's own language; how MANY there are follows the
        # kiosk's live menu, which is what this person will actually be asked to choose from.
        menu, _ = _menu_for()
        # ONE pass (pages.fill), every value script-safe JSON (pages.script_json): menu
        # copy comes from the admin page and from Pokamind, and is page DATA, never markup.
        return pages.fill(template, {
            "__LANG__": pages.script_json(code),
            "__STRINGS__": pages.strings_for(code, menu.spoken_numbers(code)),
            "__STT_GATE__": _stt_gate_json(),
            "__DEMO__": "true" if DEMO else "false",
            **(extra or {}),
        })

    @app.get("/", response_class=HTMLResponse)
    def voice_page():  # kiosk landing = verbal topic selection (screenless, no picker)
        # Only onto a free robot: a target left over from a start that lost the race to a
        # learner must not end that learner's session from another tab or a goHome. A start
        # that claimed a badge holds the add-on's reservation itself, and that reservation
        # also keeps any other mode from beginning, so the session is all that can be in its way.
        slot = app.state.start_slot
        free = not getattr(getattr(driver, "tracker", None), "active", False) and (
            slot.leased() or _robot_idle())
        waiting = slot.take() if free else None
        if waiting is not None:
            return RedirectResponse(waiting, status_code=302)
        menu, tuning = _menu_for()
        return _page(pages.VOICE_HTML, extra={
            # C: warm DNS+TLS to Pokamind while the visitor is still choosing.
            "__PRECONNECT__": _preconnect_links(site_runtime.app_origin()),
            "__PROMPT__": pages.script_json(menu.prompt),
            "__MENU_TUNING__": pages.script_json(tuning),
            "__OPTIONS__": pages.script_json(
                [{"key": k, "name": o.name} for k, o in menu.options.items()]),
        })

    @app.get("/identify", response_class=HTMLResponse)
    def identify_page():  # QR staff-badge identity (optional; topic is chosen verbally on /)
        return _page(pages.IDENTIFY_HTML)

    @app.get("/manual", response_class=HTMLResponse)
    def manual():  # typed launcher (dev / fallback)
        return _page(pages.LAUNCHER_HTML)

    @app.get("/session", response_class=HTMLResponse)
    def session(request: Request):
        # The page load is the moment the session truly begins: activate the pending
        # mint (one SSO mint can be abandoned; only an opened session counts).
        sid = request.query_params.get("sid", "")
        qr_session_id = _is_qr_session_id(sid)
        with pending_lock:
            info = pending.pop(sid, None)
            qr_bound = sid in qr_pending
            qr_pending.discard(sid)
        if info is None:
            if qr_session_id:
                return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
            # Direct/legacy open without a mint: an anonymous live session.
            info = SessionInfo(session_id=_mint_session_id("walkin"),
                               person_id="walkin", lang=lang_state.current())
        elif qr_bound:
            if not qr_session_id or not _addon_accepts(extension.session_opened, sid):
                _addon_session_ended(sid)
                return JSONResponse({"error_code": "training_unavailable"}, status_code=409)
        # The kiosk's menu, not the session's language: this module_id was chosen from
        # the menu the landing page offered, which is the live one.
        topic = next((o.name for o in _menu_for()[0].options.values()
                      if o.module_id == info.module_id), "")
        info.topic = topic
        driver.start_session(info)
        app.state.start_slot.clear()
        _note_session("hub_session_opened", info.session_id, info)
        # The learner is STANDING a metre or two away: raise the capture gain to the
        # far-field level (at the idle 0.30 the voice-activity detector scored
        # prob 0.003 on that same voice and the transcription came back empty). AFTER
        # start_session, because start_session may finalize the previous learner and
        # that finalize hands the gain back to idle -- doing it first would immediately
        # undo this.
        mic_profile.set_profile(SESSION_PROFILE)
        # Disarm `home` for the page about to load.  start_session finalizes the PREVIOUS
        # learner's session (driver.start_session, cycles > 0), and a `home` armed by that
        # finalize -- or one left over from an earlier robot-side end -- would be drained
        # by this page's first poll 250 ms from now and send the session that just started
        # straight back to the badge screen.  The page it was meant for is already gone.
        home_pending.clear()
        return _page(pages.SESSION_HTML, info.lang, extra={
            "__DEMO_STYLES__": pages.script_json(list(COMM_STYLES)),
            # A: the confirmation the LANDING page does not hold itself back to speak.
            # It is rendered HERE because this document survives the navigation the
            # landing page performs immediately (pages.START_COVER). Same language
            # resolution as _page() above, so the line, the page chrome and the voice
            # /api/tts is asked for can never disagree.
            "__START_LINE__": pages.script_json(
                pages.start_cover_line(info.lang or lang_state.current(), topic)),
            "__SESSION_START_MS__": str(_session_start_ms()),
            # The web-app origin the record names (whoami app_origin), else the
            # api. -> app. guess from POKAMIND_BASE.
            "__TT_ORIGINS__": pages.script_json(target_origins(
                os.environ.get("EMORI_TT_TARGET_ORIGINS", ""), POKAMIND_BASE,
                site_runtime.app_origin())),
        })

    @app.get("/static/jsqr.js")
    def jsqr():
        path = os.path.join(_STATIC_DIR, "jsqr.js")
        if os.path.exists(path):
            return FileResponse(path, media_type="text/javascript")
        return Response("/* jsQR not vendored; run scripts/get_voice_models to fetch */",
                        status_code=404, media_type="text/javascript")

    @app.websocket("/ws/relay")
    async def relay(ws: WebSocket):
        await ws.accept()
        hub.register(ws)
        try:
            while True:
                msg = await ws.receive_text()
                _maybe_capture_results(msg)
                hub.publish(msg, exclude=ws)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(ws)

    return app


def _client_runtime(client) -> str:
    """``ros`` for the ROS link, ``http`` for every other client."""

    return "ros" if getattr(client, "runtime", "") == "ros" else "http"


def serve(app, **uvicorn_overrides) -> None:
    """Run ``app`` on this thread until it stops: the one place uvicorn is started.

    Every entry point comes through here, so ``HOSTAPP_HOST``, ``HOSTAPP_PORT``,
    ``HOSTAPP_WS`` and the access-log rule mean the same thing in each of them.
    ``uvicorn_overrides`` are passed on and win over these defaults.
    """

    import uvicorn

    host = os.environ.get("HOSTAPP_HOST", "0.0.0.0")
    port = int(os.environ.get("HOSTAPP_PORT", "8080"))
    # Force the wsproto WebSocket backend: uvicorn's ws="auto" prefers the `websockets`
    # library, whose 15.x API is incompatible here and rejects /ws/relay with HTTP 403.
    ws_impl = os.environ.get("HOSTAPP_WS", "wsproto")
    # access_log OFF by default -- the single largest content channel this
    # process had.  uvicorn formats `get_path_with_query_string(scope)` (path AND
    # raw query) into stdout, which systemd hands to journald, so every request
    # wrote down its parameters: `GET /api/tts?lang=ca&text=<the exact sentence
    # the robot is about to speak>` and `POST /api/stt?lang=en&grammar=<the menu
    # words>`. It also drowned the journal -- the kiosk polls
    # /api/addon/hints continuously, which is dozens of lines a minute
    # around the one INFO record being looked for.
    #
    # `access_log=False` and not a log_config override, checked against the
    # installed uvicorn (0.48.0): Config.configure_logging() answers it by
    # clearing the `uvicorn.access` handlers AND setting propagate=False, and the
    # HTTP protocol reads `self.access_logger.hasHandlers()` once per connection
    # -- so with propagation stopped the line is never even formatted, and it
    # cannot reappear through the root handler setup_journal_logging installed.
    # `uvicorn.error` is untouched: startup, shutdown and every unhandled
    # exception traceback still reach the journal.
    #
    # EMORI_LOG_WIRE=1 puts it back for a dev-box triage session. Never on a
    # deployed robot: see logging_setup.access_log_enabled.
    settings = {"host": host, "port": port, "ws": ws_impl,
                "access_log": access_log_enabled()}
    settings.update(uvicorn_overrides)
    uvicorn.run(app, **settings)


def main() -> None:
    # FIRST, and in particular before _build_app(): nothing else in this process
    # configures the ROOT logger.  uvicorn configures only its own loggers (and sets
    # propagate False on them), so every emori_adapter INFO record -- the
    # routine state changes among them -- fell to Python's lastResort handler, which emits WARNING and
    # above, so the journal held WARNING lines and not one INFO.  On an appliance with no screen and
    # no shell, journalctl is the whole debugging surface, so that made the diagnostics unreadable
    # exactly where they are the only evidence.  _build_app() loads the voice menu and the roster,
    # so it must run with logging already up.
    setup_journal_logging()
    serve(_build_app())


if __name__ == "__main__":
    main()
