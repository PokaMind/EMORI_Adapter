"""The robot-local settings layer: which value each setting takes, and where it came from.

A robot deployed from the public tree configures itself. A person enters only what is
genuinely local (Wi-Fi, the robot's Pokamind token, people and badges, a few optional
overrides on the phone admin page); everything else comes from this robot's Pokamind
record or is generated on the robot. Each setting takes the FIRST of, in order:

1. ``admin``    -- saved on the phone admin page (:data:`SETTINGS_FILE`,
   :data:`SITE_SECRETS_FILE`, :data:`ADMIN_SECRETS_FILE`); ``calibration`` and
   ``calibration_fallback`` are admin-saved values written by the one-tap calibration;
2. ``env``      -- an explicit value in ``/opt/emori/.env`` (or the process environment);
3. ``pokamind`` -- derived from the last verified whoami snapshot
   (:data:`SCOPE_FILE`, :func:`derive_from_scope`);
4. ``hardware`` -- the installed robot's hardware profile (:data:`HARDWARE_PROFILES`);
5. ``default``  -- a shipped default injected on an installed robot, else the code
   default of whoever reads the variable.

An EMPTY value means unset, so a lower layer fills it; a setting may name one more value
that counts as unset (:attr:`KeySpec.unset_values`). ``EMORI_SITE_AUTO=0`` switches the
Pokamind layer off. The hardware layer and the shipped defaults apply only when
``EMORI_HARDWARE_PROFILE`` is set, i.e. on a robot ``install.sh`` installed. The
Pokamind layer applies wherever a token and a synced record exist, a development box
included: set ``EMORI_SITE_AUTO=0`` there to keep the environment exactly as ``.env``
has it.

:func:`apply` runs once per process, from :mod:`emori_adapter.config` right after
``.env`` is loaded, and writes the result into ``os.environ``: every reader of a
variable sees the effective value without knowing this module exists. It also records
which layer each value came from in ``EMORI_SITE_SOURCES`` (key names and layer names
only, never a value). A child process inherits that marker; its own :func:`apply` first
removes every inherited value the marker says did NOT come from ``.env`` and derives them
again, so a readycheck spawned by emori-admin reports the same sources as the hostapp.

emori-admin holds no robot credential: it applies its settings with
:func:`load_admin_environment` (``.env`` without the keys of
:func:`admin_foreign_keys` or any other credential-shaped key it does not own,
``configs/site/settings.env``, ``secrets/admin.env``; never ``secrets/site.env``, never
the token, so no Pokamind-derived value either).

Nothing here logs a value: key names and stable codes only.

An installed add-on (:mod:`emori_adapter.hostapp.extensions`) extends the layer through
static hooks: ``settings_registry`` (its settings, read when this module is imported),
``session_deadlines``, ``hardware_defaults``, ``pinned_settings``, ``scope_features``,
``scope_settings``, ``scope_restart_reasons``. Without one nothing changes.

This module must not import :mod:`emori_adapter.hostapp` beyond that stdlib-only seam
(config imports it before anything else exists); it may import
:mod:`emori_adapter.robot_token`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional
from urllib.parse import urlsplit

from emori_adapter import robot_token
from emori_adapter.hostapp.extensions import addons_switched_off, contributed

log = logging.getLogger("emori.site_settings")

# --------------------------------------------------------------------------- constants
DEFAULT_POKAMIND_BASE = "https://api.pokamind.com"

# Paths are relative to the install root (robot_token.install_root(): /opt/emori).
SETTINGS_FILE = "configs/site/settings.env"   # hostapp-written, non-secret settings
SITE_SECRETS_FILE = "secrets/site.env"        # hostapp-written: secret values (the SMTP password, an add-on's keys)
ADMIN_SECRETS_FILE = "secrets/admin.env"      # emori-admin-written: PIN, hotspot, dashboard
SOURCES_SIDECAR = "configs/site/sources.json"  # {key: "calibration"|"calibration_fallback"}
SCOPE_FILE = "configs/site/pokamind_scope.json"
AUTO_MENU_DIR = "configs/site/auto"
SITE_MENU_DIR = "configs/site"

SOURCES_ENV = "EMORI_SITE_SOURCES"
AUTO_ENV = "EMORI_SITE_AUTO"
PROFILE_ENV = "EMORI_HARDWARE_PROFILE"

# Which process applies the layer: the host app (and every tool) sees everything; the
# phone admin service sees only what it serves (load_admin_environment).
SERVICE_HOSTAPP = "hostapp"
SERVICE_ADMIN = "admin"

# Credentials this release no longer reads. An older robot's .env may still carry them:
# they are ignored (the host app names them once at start, never their value).
RETIRED_CREDENTIAL_KEYS = ("EMORI_PARTNER_SECRET", "EMORI_ROBOT_SECRET")
# Secrets in .env that belong to the host app or the ROS graph, never to emori-admin.
_HOSTAPP_ENV_SECRETS = ("EMORI_SKILL_KEY", "EMORI_ROS_ID_SALT", "EMORI_DASH_SALT")

SNAPSHOT_SCHEMA = 1
# A snapshot older than this is stale (readycheck WARNs scope_stale).
SCOPE_STALE_S = 7 * 24 * 3600

# The kiosk languages this appliance ships. Duplicated from hostapp.lang_state (this
# module must not import hostapp) and pinned equal to it by tests/test_site_settings.py.
SUPPORTED_LANGS = ("ca", "en")

SOURCE_ENV = "env"
SOURCE_ADMIN = "admin"
SOURCE_CALIBRATION = "calibration"
SOURCE_CALIBRATION_FALLBACK = "calibration_fallback"
SOURCE_POKAMIND = "pokamind"
SOURCE_HARDWARE = "hardware"
SOURCE_DEFAULT = "default"
SOURCES = (SOURCE_ADMIN, SOURCE_CALIBRATION, SOURCE_CALIBRATION_FALLBACK, SOURCE_ENV,
           SOURCE_POKAMIND, SOURCE_HARDWARE, SOURCE_DEFAULT)
CALIBRATION_SOURCES = frozenset({SOURCE_CALIBRATION, SOURCE_CALIBRATION_FALLBACK})
# The layers a person set on THIS robot: they outrank anything derived.
LOCAL_SOURCES = frozenset({SOURCE_ENV, SOURCE_ADMIN}) | CALIBRATION_SOURCES

# The kiosk endpointer patch a Reachy Mini Wireless runs in both languages. minAnswerMs is how
# much detected speech makes a turn. In a real room, background sound reached 672-1376 ms of it
# with nobody speaking to the robot, while real answers ran from about 3.5 s.
_REACHY_MINI_TT = {"filler": {"enabled": False},
                   "guard": {"maxFires": 30, "minAnswerMs": 1000}}

# What an installed robot's hardware supplies when nothing above says otherwise: the
# expression and session-timing values a Reachy Mini Wireless has been
# measured to need, each read by its own module (docs/configuration.md says why for each).
# Every other robot, the simulator included, keeps the code defaults. An add-on supplies
# its own per-profile values through its ``hardware_defaults`` hook.
HARDWARE_PROFILES: dict[str, dict[str, str]] = {
    "reachy-mini-wireless": {
        # The CM4 loads the Pokamind session page slowly: a cold session's first AI line
        # arrived 48.7 s after the page opened, past the 45 s code default.
        "EMORI_SESSION_START_MS": "120000",
        "EMORI_SESSION_START_S": "130",
        # Expression motion a robot in service has run without a motor error.
        "EMORI_CUE_BODY_YAW": "1",
        "EMORI_CUE_Z_MM": "18",
        "EMORI_CUE_AMP": "1.6",
        "EMORI_TT_OVERRIDES": json.dumps({"en": _REACHY_MINI_TT, "ca": _REACHY_MINI_TT},
                                         separators=(",", ":")),
    },
}

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_EMAIL_RE = re.compile(r"[^@\s,;<>\"]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+")
_HOST_RE = re.compile(r"[A-Za-z0-9.:\[\]-]{1,255}")
_PIN_RE = re.compile(r"[0-9]{6}")  # = admin.auth.PIN_RE
_MAX_FILE_BYTES = 1 << 20


class SettingsError(ValueError):
    """A refused setting: a stable ``code`` and the ``key`` it is about (never a value)."""

    def __init__(self, code: str, key: str = "") -> None:
        super().__init__(f"{code}: {key}" if key else code)
        self.code = code
        self.key = key


# --------------------------------------------------------------------------- validators
# Each takes the value as text and returns None when it is acceptable, else a stable slug.
Validator = Callable[[str], Optional[str]]


# Unicode categories no setting value may hold: controls (Cc: NUL, CR, LF, VT, FF, the
# C1 range with NEL), format characters (Cf: bidi overrides, zero-width marks) and the
# line/paragraph separators (Zl, Zp). Several of them END A LINE for str.splitlines(),
# so a value carrying one would read back as two settings.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _has_control(value: str) -> bool:
    return any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in value)


def has_control(value: str) -> bool:
    """True when ``value`` holds a character no setting value may hold (see above)."""

    return _has_control(value)


def _text(max_len: int) -> Validator:
    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        if len(value) > max_len:
            return "value_out_of_range"
        return None
    return check


# Characters a display name may not hold: it is shown on pages (escaped there too) and
# spoken, and none of these belongs in a name.
_NAME_REFUSED = frozenset('<>"`')


def _name(max_len: int) -> Validator:
    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        if any(ch in _NAME_REFUSED for ch in value):
            return "value_has_markup"
        if len(value) > max_len:
            return "value_out_of_range"
        return None
    return check


def valid_display_name(value: object, max_len: int = 64) -> bool:
    """A robot display name the pages can show: non-empty text, no markup or controls."""

    return (isinstance(value, str) and bool(value.strip())
            and _name(max_len)(value) is None)


def _origin(value: str) -> Optional[str]:
    """An https origin (http only for loopback): the rule whoami_url applies.

    An ORIGIN: scheme, host and an optional port -- no path, query, fragment or
    userinfo (one trailing ``/`` is tolerated). ``value_not_origin`` for a URL with a
    path; ``value_not_https`` for everything else that is not one.
    """

    if _has_control(value):
        return "value_has_newline"
    base = value.strip().rstrip("/")
    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        return "value_not_https"
    if (not base or not parts.netloc or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or "?" in base or "#" in base
            or not (parts.scheme == "https"
                    or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))):
        return "value_not_https"
    if parts.path:
        return "value_not_origin"
    return None


def _url(value: str) -> Optional[str]:
    """An http(s) URL with a host (FIWARE endpoints live on the robot or the LAN)."""

    if _has_control(value):
        return "value_has_newline"
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return "value_not_url"
    if parts.scheme not in ("http", "https") or not parts.hostname \
            or parts.username is not None or parts.password is not None or len(value) > 500:
        return "value_not_url"
    return None


def _host(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if _HOST_RE.fullmatch(value.strip()) else "value_not_url"


def _choice(*choices: str) -> Validator:
    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        return None if value.strip() in choices else "value_not_choice"
    return check


def _lang(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if value.strip().lower() in SUPPORTED_LANGS else "value_not_choice"


def _int_range(low: int, high: int, *, off: bool = False) -> Validator:
    """A whole number in ``[low, high]``; ``off`` also accepts 0 (its reader's "off")."""

    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        text = value.strip()
        if re.fullmatch(r"[0-9]{1,9}", text) is None:
            return "value_out_of_range"
        number = int(text)
        if not (low <= number <= high or (off and number == 0)):
            return "value_out_of_range"
        return None
    return check


def _number(low: float, high: float, *, off: bool = False) -> Validator:
    """A plain decimal in ``[low, high]`` (no sign, exponent, inf or nan); ``off`` also
    accepts 0."""

    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        text = value.strip()
        if re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6})?", text) is None:
            return "value_out_of_range"
        number = float(text)
        if not (low <= number <= high or (off and number == 0)):
            return "value_out_of_range"
        return None
    return check


# The kiosk turn-taking patch's rules, = hostapp.tt_inject's _MAX_BYTES, _MAX_DEPTH,
# _MAX_LEAVES, _KEY_RE, _STR_RE and _BANNED_KEYS (duplicated: this module must not import
# hostapp; tests/test_site_settings.py feeds both validators one corpus).
TT_PATCH_MAX_BYTES = 4096
_TT_MAX_DEPTH = 4
_TT_MAX_LEAVES = 64
_TT_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
_TT_STR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_TT_BANNED_KEYS = frozenset({"__proto__", "constructor", "prototype"})


def _tt_leaves(node: object, depth: int) -> Optional[int]:
    """How many leaves ``node`` holds, or None for one the kiosk's validator refuses."""

    if depth > _TT_MAX_DEPTH:
        return None
    if isinstance(node, bool):          # before int: bool is an int subclass
        return 1
    if isinstance(node, (int, float)):
        try:
            return 1 if math.isfinite(float(node)) else None
        except OverflowError:           # an integer too large for a float
            return None
    if isinstance(node, str):
        return 1 if _TT_STR_RE.fullmatch(node) else None
    if not isinstance(node, dict):
        return None
    total = 0
    for key, child in node.items():
        if key in _TT_BANNED_KEYS or _TT_KEY_RE.fullmatch(key) is None:
            return None
        leaves = _tt_leaves(child, depth + 1)
        if leaves is None:
            return None
        total += leaves
    return total


def _tt_patch(value: str) -> Optional[str]:
    """``EMORI_TT_OVERRIDES``: a JSON object keyed by kiosk language, each an object.

    Judged by hostapp.tt_inject's own rules (key and string grammar, banned keys, leaf
    types, depth, leaf count, size): the host app refuses any other patch as inert, and
    a saved value outranks the hardware profile's, so a value this let through would
    silently cost the robot its turn-taking tuning.
    """

    if _has_control(value):
        return "value_has_newline"
    if len(value.encode("utf-8")) > TT_PATCH_MAX_BYTES:
        return "value_out_of_range"
    try:
        patch = json.loads(value)
    except ValueError:
        return "value_not_tt_patch"
    if (type(patch) is not dict or not patch
            or any(lang not in SUPPORTED_LANGS or type(part) is not dict
                   for lang, part in patch.items())):
        return "value_not_tt_patch"
    leaves = 0
    for part in patch.values():
        found = _tt_leaves(part, 1)
        if found is None:
            return "value_not_tt_patch"
        leaves += found
        if leaves > _TT_MAX_LEAVES:
            return "value_not_tt_patch"
    try:
        size = len(json.dumps(patch, allow_nan=False).encode("utf-8"))
    except ValueError:
        return "value_not_tt_patch"
    return "value_out_of_range" if size > TT_PATCH_MAX_BYTES else None


def _port(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    text = value.strip()
    if re.fullmatch(r"[0-9]{1,5}", text) is None or not 1 <= int(text) <= 65535:
        return "value_not_port"
    return None


def _bool(*accepted: str) -> Validator:
    def check(value: str) -> Optional[str]:
        if _has_control(value):
            return "value_has_newline"
        return None if value.strip() in accepted else "value_not_bool"
    return check


def _email(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if _EMAIL_RE.fullmatch(value.strip()) else "value_not_email"


def _email_list(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    for item in value.split(","):
        if item.strip() and _EMAIL_RE.fullmatch(item.strip()) is None:
            return "value_not_email"
    return None


def _pin(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if _PIN_RE.fullmatch(value) else "value_out_of_range"


def _ssid(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if 1 <= len(value.encode("utf-8")) <= 32 else "value_out_of_range"


def _ap_password(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    printable = all(" " <= ch <= "~" for ch in value)
    return None if printable and 8 <= len(value) <= 63 else "value_out_of_range"


def _password(value: str) -> Optional[str]:
    if _has_control(value):
        return "value_has_newline"
    return None if 8 <= len(value) <= 128 else "value_out_of_range"


# --------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class KeySpec:
    """One setting the robot-local layer manages.

    ``owner`` is the process that writes it (``hostapp``: :data:`SETTINGS_FILE` /
    :data:`SITE_SECRETS_FILE`; ``admin``: :data:`ADMIN_SECRETS_FILE`). ``restart`` is what
    has to restart before a new value is in force. ``editable`` False: written only by a
    dedicated route (the calibration confirm), never by a generic settings save.
    ``file`` is the one file a saved value lives in. ``unset_values``: spellings (lower
    case) that count as "no value" besides an empty one, so a lower layer fills it.
    ``verified_origin``: non-empty for an origin of this robot's Pokamind, saved only when
    whoami signed with the token in force answers from it; the value is the error a save
    answers when it does not.
    """

    key: str
    owner: str
    secret: bool
    editable: bool
    restart: str
    validator: Validator
    file: str = SETTINGS_FILE
    unset_values: frozenset = frozenset()
    verified_origin: str = ""


def _spec(key: str, validator: Validator, *, owner: str = "hostapp", secret: bool = False,
          editable: bool = True, restart: str = "hostapp", file: str = "",
          unset_values: frozenset = frozenset(), verified_origin: str = "") -> KeySpec:
    if not file:
        file = ADMIN_SECRETS_FILE if owner == "admin" else SETTINGS_FILE
    return KeySpec(key, owner, secret, editable, restart, validator, file, unset_values,
                   verified_origin)


# What an add-on builds the entries of its ``settings_registry`` hook from.
make_spec = _spec
text_check = _text
choice_check = _choice
int_range_check = _int_range
number_check = _number
bool_check = _bool
origin_check = _origin


REGISTRY: dict[str, KeySpec] = {spec.key: spec for spec in (
    # --- Pokamind ---
    _spec("POKAMIND_BASE", _origin, verified_origin="base_not_verified"),
    # --- derived from this robot's Pokamind record when unset ---
    _spec("EMORI_ROBOT_NAME", _name(64)),
    _spec("EMORI_COURSE_ID", _text(500)),
    _spec("EMORI_PATHWAYS", _text(8000)),
    _spec("EMORI_LANG", _lang),
    # --- hardware tuning (the hardware profile when unset; else each reader's default).
    # The bounds are the readers' own, so a saved value is one the reader keeps.
    _spec("EMORI_SESSION_START_MS", _int_range(5000, 600000, off=True)),
    _spec("EMORI_SESSION_START_S", _number(5.0, 600.0, off=True)),
    _spec("EMORI_CUE_BODY_YAW", _bool("0", "1")),
    _spec("EMORI_CUE_Z_MM", _number(0.0, 20.0)),
    _spec("EMORI_CUE_AMP", _number(0.5, 2.0)),
    _spec("EMORI_TT_OVERRIDES", _tt_patch),
    # --- results e-mail ---
    _spec("EMORI_SMTP_HOST", _host),
    _spec("EMORI_SMTP_PORT", _port),
    _spec("EMORI_SMTP_USER", _text(320)),
    _spec("EMORI_SMTP_FROM", _email),
    _spec("EMORI_SMTP_PASS", _text(512), secret=True, file=SITE_SECRETS_FILE),
    _spec("EMORI_SMTP_TLS_VERIFY", _bool("0", "1")),
    _spec("EMORI_RESULTS_TO", _email_list),
    _spec("EMORI_RESULTS_BCC", _email_list),
    # --- analytics and data ---
    _spec("EMORI_FIWARE", _bool("0", "1")),
    _spec("ORION_URL", _url),
    _spec("NGSILD_TENANT", _text(64)),
    _spec("QL_NOTIFY_URL", _url),
    _spec("EMORI_RECORD", _bool("0", "1")),
    _spec("EMORI_DASH_RETENTION_DAYS", _int_range(0, 3650)),
    # --- written by emori-admin ---
    _spec("EMORI_ADMIN_PIN", _pin, owner="admin", secret=True, restart="admin"),
    _spec("EMORI_AP_SSID", _ssid, owner="admin", secret=True, restart="admin"),
    _spec("EMORI_AP_PASSWORD", _ap_password, owner="admin", secret=True, restart="admin"),
    _spec("EMORI_DASH_PASSWORD", _password, owner="admin", secret=True, restart="hostapp"),
)}

# The keys derive_from_scope can fill, and which of them only a restart puts in force. An
# add-on may derive more (its ``scope_settings`` hook); those are reported alongside.
DERIVED_KEYS = ("EMORI_COURSE_ID", "EMORI_PATHWAYS", "EMORI_ROBOT_NAME", "EMORI_LANG")


def hostapp_keys() -> tuple[str, ...]:
    return tuple(key for key, spec in REGISTRY.items() if spec.owner == "hostapp")


def admin_keys() -> tuple[str, ...]:
    return tuple(key for key, spec in REGISTRY.items() if spec.owner == "admin")


# --------------------------------------------------------------------------- deadline order
# The two deadlines a session start runs against, and their readers' code defaults
# (server._SESSION_START_MS_DEFAULT and driver._SESSION_START_S_DEFAULT; pinned by
# tests/test_site_settings.py). The page's own deadline speaks its line and goes home
# first; the adapter's backstop is for a page that cannot. An add-on may name deadlines of
# its own that must stay behind both (its ``session_deadlines`` hook).
DEADLINE_DEFAULTS: dict[str, float] = {
    "EMORI_SESSION_START_MS": 45000.0,
    "EMORI_SESSION_START_S": 45.0,
}


def _is_deadline_pair(value: object) -> bool:
    """Two finite numbers: what a ``session_deadlines`` entry must be to be used at all."""

    return (isinstance(value, (tuple, list)) and len(value) == 2
            and all(isinstance(part, (int, float)) and not isinstance(part, bool)
                    and math.isfinite(part) for part in value))


def _addon_deadlines() -> dict[str, tuple[float, float]]:
    """``{key: (code default in s, least margin in s)}`` for the add-on's registered keys."""

    found = contributed("session_deadlines")
    if not isinstance(found, Mapping):
        return {}
    return {key: (float(value[0]), float(value[1])) for key, value in found.items()
            if key in REGISTRY and _is_deadline_pair(value)}


def deadline_keys() -> frozenset[str]:
    """Every setting the session-start deadline order takes in."""

    return frozenset(DEADLINE_DEFAULTS) | frozenset(_addon_deadlines())


def _deadline_s(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    value = float(raw) if raw and REGISTRY[key].validator(raw) is None else default
    return value / 1000.0 if key == "EMORI_SESSION_START_MS" else value


def deadline_order_problems(env: Mapping[str, str]) -> list[tuple[str, ...]]:
    """The session-start deadlines ``env`` puts out of order, each as the keys involved.

    In order: the page's (``EMORI_SESSION_START_MS`` / 1000) <= the adapter's
    (``EMORI_SESSION_START_S``), and each deadline an add-on names at least its margin
    behind the later of the two. A deadline that is 0 (off) takes no part; one that does
    not validate is its reader's default. The keys are listed later deadline first.
    """

    screen = _deadline_s(env, "EMORI_SESSION_START_MS",
                         DEADLINE_DEFAULTS["EMORI_SESSION_START_MS"])
    robot = _deadline_s(env, "EMORI_SESSION_START_S",
                        DEADLINE_DEFAULTS["EMORI_SESSION_START_S"])
    problems: list[tuple[str, ...]] = []
    if screen > 0 and robot > 0 and robot < screen:
        problems.append(("EMORI_SESSION_START_S", "EMORI_SESSION_START_MS"))
    for key, (default, margin) in _addon_deadlines().items():
        later = _deadline_s(env, key, default)
        if later > 0 and later < max(screen, robot) + margin:
            problems.append((key, "EMORI_SESSION_START_S", "EMORI_SESSION_START_MS"))
    return problems


def validate(key: str, value: str) -> Optional[str]:
    """``None`` when ``value`` is acceptable for ``key``, else a stable slug.

    ``key_unknown`` for a name that is not a site setting.
    """

    spec = REGISTRY.get(key)
    if spec is None:
        return "key_unknown"
    if not isinstance(value, str):
        return "value_out_of_range"
    return spec.validator(value)


# --------------------------------------------------------------------------- paths
def find_install_dotenv(start: str) -> str:
    """The nearest ``.env`` walking up from the directory ``start``, or ``""``.

    Explicit, and from a FILE's location: python-dotenv's ``find_dotenv()`` walks from
    the working directory instead whenever ``__main__`` has no file (``python -c``, a
    REPL) or a tracer is set (a debugger, coverage), so the same tool read a different
    ``.env`` depending on how it was started. ``abspath`` and not
    ``resolve``: :func:`emori_adapter.robot_token.install_root` walks the same way, and
    the two must name the same directory.
    """

    directory = os.path.abspath(start)
    while True:
        candidate = os.path.join(directory, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return ""
        directory = parent


def base_dir(root: Optional[os.PathLike | str] = None) -> Path:
    """What every relative path here is relative to: ``root``, else the install root.

    Tests point the whole module elsewhere with ``root=`` or by monkeypatching this
    function (every helper resolves through it).
    """

    if root is not None:
        return Path(root)
    return robot_token.install_root() or Path(os.getcwd())


def site_path(relative: str, root: Optional[os.PathLike | str] = None) -> Path:
    """``relative`` (one of the constants above) as an absolute path in this install."""

    path = Path(relative)
    return path if path.is_absolute() else base_dir(root) / path


def is_installed_robot(environ: Optional[Mapping[str, str]] = None) -> bool:
    """An install.sh-installed robot: ``EMORI_HARDWARE_PROFILE`` is set."""

    return bool(hardware_profile(environ))


def hardware_profile(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(PROFILE_ENV, "") or "").strip()


def auto_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """False when ``EMORI_SITE_AUTO=0``: no value (and no menu) is derived from Pokamind."""

    env = os.environ if environ is None else environ
    return (env.get(AUTO_ENV, "1") or "1").strip() != "0"


def _is_set(key: str, value: Optional[str]) -> bool:
    """Empty means unset; so does a spelling the setting names (:attr:`KeySpec.unset_values`)."""

    if value is None or not value.strip():
        return False
    spec = REGISTRY.get(key)
    if spec is not None and value.strip().lower() in spec.unset_values:
        return False
    return True


# --------------------------------------------------------------------------- env files
def _chmod(path: os.PathLike | str, mode: int) -> None:
    """Best effort: on Windows ``os.chmod`` only toggles the read-only bit."""

    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _atomic_write(target: Path, data: bytes) -> None:
    """``data`` into ``target`` atomically, 0600 from its first byte.

    Same-directory temp file + fsync + ``os.replace`` (robot_token._write_private's
    pattern). A file under ``secrets/`` gets its directory created 0700.
    """

    directory = target.parent
    if directory.name == "secrets":
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _chmod(directory, 0o700)
    else:
        directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp",
                                     dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _chmod(target, 0o600)
    if os.name == "posix":  # the rename itself must survive a power cut too
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _env_lines(text: str) -> list[str]:
    """``text`` cut into lines exactly where systemd's ``EnvironmentFile`` cuts it.

    CR and LF only -- never ``str.splitlines()``, which also breaks on VT, FF, the
    C1 separators and U+2028/U+2029: a value holding one would otherwise read back
    here as two settings while systemd hands the process one.
    """

    return _LINE_BREAK.split(text)


def _parse_env_text(text: str, target: object = "settings") -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(_env_lines(text), 1):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or _KEY_RE.fullmatch(key) is None:
            log.warning("%s line %d is not KEY=VALUE: ignored", target, number)
            continue
        if key not in REGISTRY:
            # An add-on's setting while that add-on is not loaded: a save keeps its line
            # (:func:`_kept_lines`).
            log.debug("%s sets %s, which is not a site setting: ignored", target, key)
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    return values


def read_env_file(path: os.PathLike | str) -> dict[str, str]:
    """``KEY=VALUE`` lines of a settings file; ``{}`` when it is missing or unreadable.

    Blank lines and lines starting with ``#`` or ``;`` are skipped. One pair of double
    quotes around a value is removed. A name that is not a site setting is ignored, logged
    at DEBUG by key (never its value). Lines end at CR or LF only, as systemd reads the
    same file (:func:`_env_lines`).
    """

    target = Path(path)
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            log.warning("site settings file %s is too large: ignored", target)
            return {}
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        log.warning("site settings file %s cannot be read: ignored", target)
        return {}
    return _parse_env_text(text, target)


def _kept_lines(path: os.PathLike | str) -> tuple[str, ...]:
    """The ``KEY=VALUE`` lines of a settings file that are not site settings, verbatim.

    What a save carries over untouched, so a setting of an add-on that is not loaded right
    now is still there when it is. Only a save reads these: nothing that reports settings
    ever sees them. Same limits as :func:`read_env_file`.
    """

    target = Path(path)
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            return ()
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    kept = []
    for line in _env_lines(text):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        key, sep, _value = line.partition("=")
        key = key.strip()
        if sep and _KEY_RE.fullmatch(key) is not None and key not in REGISTRY:
            kept.append(line)
    return tuple(kept)


def write_env_file(path: os.PathLike | str, mapping: Mapping[str, str],
                   kept_lines: tuple[str, ...] = ()) -> None:
    """Write ``mapping`` as ``KEY=VALUE`` lines, atomically, file 0600.

    ``kept_lines`` (:func:`_kept_lines`) go first, exactly as they were read.

    Refuses (``SettingsError``) a key that is not a variable name, and any control,
    format or line-separator character in a value (:func:`has_control`): one line per
    value is what systemd's ``EnvironmentFile`` and :func:`read_env_file` both read. A
    value that itself starts and ends with a double quote is written inside one more
    pair, so it reads back unchanged. The text is parsed back before it is written and
    refused (``value_has_newline``) unless it reads back as exactly ``mapping``.
    """

    lines = []
    for key in sorted(mapping):
        value = mapping[key]
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise SettingsError("key_invalid")
        if not isinstance(value, str):
            raise SettingsError("value_not_text", key)
        if _has_control(value):
            raise SettingsError("value_has_newline", key)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = f'"{value}"'
        lines.append(f"{key}={value}\n")
    text = "".join(f"{line}\n" for line in kept_lines) + "".join(lines)
    # What a later read (and systemd) will see must be exactly what was asked for: no
    # value may smuggle a second KEY=VALUE line into the file.
    read_back = _parse_env_text(text, path)
    expected = {key: value for key, value in mapping.items() if key in REGISTRY}
    if read_back != expected:
        raise SettingsError("value_has_newline")
    _atomic_write(Path(path), text.encode("utf-8"))


def read_sources_sidecar(root: Optional[os.PathLike | str] = None) -> dict[str, str]:
    """``{key: "calibration"|"calibration_fallback"}``: saved keys the calibration wrote."""

    path = site_path(SOURCES_SIDECAR, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("%s cannot be read: calibration sources ignored", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: source for key, source in data.items()
            if key in REGISTRY and source in CALIBRATION_SOURCES}


def write_sources_sidecar(mapping: Mapping[str, str],
                          root: Optional[os.PathLike | str] = None) -> None:
    """Write the sidecar. An entry for a key that is not a site setting stays as it was."""

    clean: dict[str, str] = {}
    try:
        data = json.loads(site_path(SOURCES_SIDECAR, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        clean.update({key: source for key, source in data.items()
                      if key not in REGISTRY and _KEY_RE.fullmatch(key) is not None
                      and source in CALIBRATION_SOURCES})
    clean.update({key: source for key, source in mapping.items()
                  if key in REGISTRY and source in CALIBRATION_SOURCES})
    _atomic_write(site_path(SOURCES_SIDECAR, root),
                  (json.dumps(clean, sort_keys=True, indent=1) + "\n").encode("utf-8"))


def stored_values(root: Optional[os.PathLike | str] = None) -> dict[str, str]:
    """Every value saved on this robot (the three files), keyed by setting, raw.

    A key found in a file that is not its own is left out (it is never applied either).
    Secret values are included: callers that show anything must mask them.
    """

    out: dict[str, str] = {}
    for relative in (SETTINGS_FILE, SITE_SECRETS_FILE, ADMIN_SECRETS_FILE):
        for key, value in read_env_file(site_path(relative, root)).items():
            if REGISTRY[key].file == relative:
                out[key] = value
    return out


def check_stored_settings(root: Optional[os.PathLike | str] = None) -> dict[str, str]:
    """``{key: slug}`` for every saved value that would be refused (and so is not applied).

    ``key_wrong_file`` for a setting saved in a file that is not its own.
    """

    problems: dict[str, str] = {}
    for relative in (SETTINGS_FILE, SITE_SECRETS_FILE, ADMIN_SECRETS_FILE):
        for key, value in read_env_file(site_path(relative, root)).items():
            spec = REGISTRY[key]
            if spec.file != relative:
                problems[key] = "key_wrong_file"
            elif value.strip():
                code = spec.validator(value)
                if code:
                    problems[key] = code
    return problems


_save_lock = threading.Lock()


def save_settings(updates: Mapping[str, Optional[str]], *, source: str = SOURCE_ADMIN,
                  owner: str = "hostapp",
                  root: Optional[os.PathLike | str] = None) -> list[str]:
    """Save ``updates`` (``None`` or ``""`` removes a key) into the files they belong in.

    Every key must be a setting owned by ``owner`` and every value must validate, or
    nothing is written (``SettingsError`` with the slug and the key). ``source`` labels
    the values: ``admin`` (default), or ``calibration``/``calibration_fallback`` for the
    calibration confirm, recorded in :data:`SOURCES_SIDECAR`. Returns the keys saved.
    Takes effect at the next start of the process that reads them (:func:`apply`).
    """

    if source != SOURCE_ADMIN and source not in CALIBRATION_SOURCES:
        raise SettingsError("source_invalid")
    by_file: dict[str, dict[str, Optional[str]]] = {}
    for key, value in updates.items():
        spec = REGISTRY.get(key)
        if spec is None:
            raise SettingsError("key_unknown", key)
        if spec.owner != owner:
            raise SettingsError("key_wrong_owner", key)
        if value is not None and not isinstance(value, str):
            raise SettingsError("value_not_text", key)
        if value:
            code = spec.validator(value)
            if code:
                raise SettingsError(code, key)
        by_file.setdefault(spec.file, {})[key] = value or None
    with _save_lock:
        for relative, changes in by_file.items():
            path = site_path(relative, root)
            current = read_env_file(path)
            for key, value in changes.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            write_env_file(path, current, _kept_lines(path))
        sidecar = read_sources_sidecar(root)
        changed = False
        for key, value in updates.items():
            if value and source in CALIBRATION_SOURCES:
                changed |= sidecar.get(key) != source
                sidecar[key] = source
            elif key in sidecar:
                del sidecar[key]
                changed = True
        if changed:
            write_sources_sidecar(sidecar, root)
    log.info("site settings saved (%s): %s", source, " ".join(sorted(updates)) or "(none)")
    return sorted(updates)


# --------------------------------------------------------------------------- scope
def snapshot_digest(whoami_dict: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a whoami body."""

    canonical = json.dumps(whoami_dict, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_scope_file(root: Optional[os.PathLike | str] = None
                    ) -> tuple[Optional[dict], str]:
    """``(snapshot, "ok")`` or ``(None, code)``: ``scope_missing``, ``scope_unreadable``
    or ``scope_schema_unknown``. Whose snapshot it is is not checked here."""

    path = site_path(SCOPE_FILE, root)
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None, "scope_unreadable"
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "scope_missing"
    except (OSError, ValueError):
        return None, "scope_unreadable"
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA \
            or not isinstance(data.get("whoami"), dict):
        return None, "scope_schema_unknown"
    return data, "ok"


def _checked_scope(token_robot_id: str, token_fingerprint: Optional[str],
                   root: Optional[os.PathLike | str]) -> tuple[Optional[dict], str]:
    data, code = read_scope_file(root)
    if data is None:
        return None, code
    if not token_robot_id or data.get("robot_id") != token_robot_id:
        return None, "scope_robot_mismatch"
    known = data.get("key_fingerprint")
    if token_fingerprint and known and known != token_fingerprint:
        return None, "scope_fingerprint_mismatch"
    return data, "ok"


def scope_snapshot_status(token_robot_id: str, token_fingerprint: Optional[str] = None, *,
                          root: Optional[os.PathLike | str] = None) -> str:
    """``ok``, or why :func:`load_scope_snapshot` returns None: ``scope_missing``,
    ``scope_unreadable``, ``scope_schema_unknown``, ``scope_robot_mismatch`` or
    ``scope_fingerprint_mismatch``."""

    return _checked_scope(token_robot_id, token_fingerprint, root)[1]


def load_scope_snapshot(token_robot_id: str, token_fingerprint: Optional[str] = None, *,
                        root: Optional[os.PathLike | str] = None) -> Optional[dict]:
    """THIS robot's last-good snapshot, or None.

    None when the file is missing, unreadable or of an unknown schema, when it is about
    another robot_id, and when both fingerprints are known and differ (a replaced token).
    """

    return _checked_scope(token_robot_id, token_fingerprint, root)[0]


# What :func:`merge_scope_snapshot` writes. Any other key is one it does not read.
_SNAPSHOT_KEYS = frozenset({"schema", "fetched_at", "robot_id", "key_fingerprint",
                            "key_version", "contract", "digest", "whoami",
                            "addon_provisioned", "menu_order"})


def merge_scope_snapshot(previous: Optional[Mapping[str, Any]],
                         whoami_dict: Mapping[str, Any], *, contract: int,
                         key_fingerprint: Optional[str], key_version: Optional[int],
                         now: float) -> dict:
    """The snapshot to write after a verified whoami.

    ``addon_provisioned`` is what an installed add-on's ``scope_features`` hook says of
    the record's ``features``; the hook also sees the previous snapshot when it is for the
    same robot_id and credential (key_version, and the fingerprint when both are known),
    so it can keep the answer true once given; a new credential starts over. With no
    add-on to answer, what the same credential's snapshot held is carried unread, flag
    included, for when the add-on is back.
    ``menu_order`` (the menu builder's record of which module had which number) is
    carried from the previous snapshot of the same robot.
    """

    whoami = dict(whoami_dict)
    robot_id = whoami.get("robot_id")
    prev = previous if isinstance(previous, Mapping) else {}
    same_robot = bool(prev) and prev.get("robot_id") == robot_id
    prev_fingerprint = prev.get("key_fingerprint")
    same_credential = (same_robot and prev.get("key_version") == key_version
                       and (not prev_fingerprint or not key_fingerprint
                            or prev_fingerprint == key_fingerprint))
    features = whoami.get("features")
    features = features if isinstance(features, Mapping) else {}
    answer = contributed("scope_features", dict(features),
                         dict(prev) if same_credential else None)
    if answer is None:
        provisioned = bool(same_credential and prev.get("addon_provisioned") is True)
        carried = {key: value for key, value in prev.items() if key not in _SNAPSHOT_KEYS} \
            if same_credential else {}
    else:
        provisioned, carried = bool(answer), {}
    menu_order = prev.get("menu_order") if same_robot else None
    return {
        **carried,
        "schema": SNAPSHOT_SCHEMA,
        "fetched_at": now,
        "robot_id": robot_id,
        "key_fingerprint": key_fingerprint,
        "key_version": key_version,
        "contract": contract,
        "digest": snapshot_digest(whoami),
        "whoami": whoami,
        "addon_provisioned": provisioned,
        "menu_order": dict(menu_order) if isinstance(menu_order, Mapping) else {},
    }


def write_scope_snapshot(snapshot: Mapping[str, Any], *,
                         root: Optional[os.PathLike | str] = None) -> Path:
    """Write the snapshot to :data:`SCOPE_FILE` atomically, 0600. Raises OSError."""

    path = site_path(SCOPE_FILE, root)
    text = json.dumps(snapshot, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    _atomic_write(path, text.encode("utf-8"))
    return path


def _base_code(value: str) -> str:
    return value.strip().lower().replace("_", "-").split("-", 1)[0]


def _clean_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _has_control(value)


def _scope_pathways(whoami: Mapping[str, Any]) -> tuple[list[str], int]:
    """The record's pathways usable in the comma-separated allowlist, and how many real
    pathways cannot be listed there (a comma or a control character in them). An empty
    entry names no pathway a badge could carry and is neither."""

    kept: list[str] = []
    skipped = 0
    pathways = whoami.get("pathways")
    for item in pathways if isinstance(pathways, list) else []:
        if not isinstance(item, str) or not item.strip():
            continue
        if _clean_text(item) and "," not in item:
            kept.append(item)
        else:
            skipped += 1
    return kept, skipped


def offered_module_languages(whoami: Mapping[str, Any]) -> dict[str, int]:
    """``{supported base code: number of modules the record offers in it}``.

    A module counts when it is ``available``, has no ``problem`` and a language whose
    base code is a supported kiosk language -- the rule the menu builder offers
    modules by (hostapp.menu_builder, pinned equal by tests/test_site_settings.py).
    Languages with none are absent. ``{}`` -- "cannot tell" -- when the record lists no
    module at all, or offers one that names no language (it may be any of them).
    """

    counts: dict[str, int] = {}
    modules = whoami.get("modules") if isinstance(whoami, Mapping) else None
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping) or module.get("available") is not True \
                or module.get("problem") is not None:
            continue
        language = module.get("language")
        if not isinstance(language, str) or not language.strip():
            return {}
        code = _base_code(language)
        if code in SUPPORTED_LANGS:
            counts[code] = counts.get(code, 0) + 1
    return counts


def scope_language(whoami: Mapping[str, Any]) -> str:
    """The boot language the record asks for, as a supported base code, or ``""``.

    ``default_language`` when it is supported (and one of ``languages`` when that list
    is not empty), else the first ``languages`` entry whose base code is supported.
    When the record offers modules in some supported language, a language it offers
    none in is passed over (booting there would serve no menu of this robot's own);
    and a record that names no language at all boots in the language most of its
    modules are in.
    """

    languages = whoami.get("languages")
    languages = [item for item in languages if isinstance(item, str)] \
        if isinstance(languages, list) else []
    offered = offered_module_languages(whoami)
    default = whoami.get("default_language")

    def pick(usable: Callable[[str], bool]) -> str:
        if isinstance(default, str) and usable(_base_code(default)) and (
                not languages or any(_base_code(item) == _base_code(default)
                                     for item in languages)):
            return _base_code(default)
        for item in languages:
            if usable(_base_code(item)):
                return _base_code(item)
        return ""

    chosen = pick(lambda code: code in SUPPORTED_LANGS and (not offered or code in offered))
    if chosen:
        return chosen
    if not languages and offered:
        # No language listed (Pokamind does not restrict it): the one most of the
        # record's modules are in, a tie going to the shipped order.
        return max(SUPPORTED_LANGS, key=lambda code: (offered.get(code, 0),
                                                      -SUPPORTED_LANGS.index(code)))
    # No listed language has a module of its own: the record's choice, as before.
    return pick(lambda code: code in SUPPORTED_LANGS)


def derive_from_scope(snapshot: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """The settings this robot's Pokamind record supplies, from a snapshot (or ``{}``).

    * ``EMORI_COURSE_ID``: ``course_id`` when non-empty;
    * ``EMORI_PATHWAYS``: the pathways joined by commas -- only when EVERY one can be
      listed. A pathway that itself holds a comma cannot be, and an allowlist without
      it would drop the claim of every badge carrying it, so then nothing is derived
      (no allowlist: every roster pathway is passed on, as without a record);
    * ``EMORI_ROBOT_NAME``: ``display_name`` when non-empty, not just the robot id, and
      free of markup and control characters (:func:`valid_display_name`);
    * ``EMORI_LANG``: :func:`scope_language`;
    * whatever an installed add-on's ``scope_settings`` hook derives from the snapshot.
    """

    if not isinstance(snapshot, Mapping):
        return {}
    whoami = snapshot.get("whoami")
    if not isinstance(whoami, Mapping):
        return {}
    out: dict[str, str] = {}
    course = whoami.get("course_id")
    if _clean_text(course):
        out["EMORI_COURSE_ID"] = course
    pathways, skipped = _scope_pathways(whoami)
    if pathways and not skipped:
        out["EMORI_PATHWAYS"] = ",".join(pathways)
    name = whoami.get("display_name")
    if valid_display_name(name) and name.strip() != whoami.get("robot_id"):
        out["EMORI_ROBOT_NAME"] = name.strip()
    language = scope_language(whoami)
    if language:
        out["EMORI_LANG"] = language
    extra = contributed("scope_settings", dict(snapshot))
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            if key in REGISTRY and key not in out and isinstance(value, str):
                out[key] = value
    return out


def restart_reasons(snapshot_before: Optional[Mapping[str, Any]],
                    snapshot_after: Optional[Mapping[str, Any]], *,
                    sources: Optional[Mapping[str, str]] = None,
                    environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Why the hostapp should restart (when idle) after the snapshot changed, or ``[]``.

    ``scope_changed``: a derived value that only a restart puts in force changed, for a
    key no local value overrides, plus the reasons an installed add-on's
    ``scope_restart_reasons`` hook names (``addon_provisioned``). Nothing when
    ``EMORI_SITE_AUTO=0``.
    ``sources`` defaults to :func:`effective_sources` (this process's layers).
    """

    if not auto_enabled(environ):
        return []
    layers = effective_sources(environ) if sources is None else sources

    def local(key: str) -> bool:
        return layers.get(key) in LOCAL_SOURCES

    before = derive_from_scope(snapshot_before)
    after = derive_from_scope(snapshot_after)
    reasons: list[str] = []
    for key in DERIVED_KEYS:
        if REGISTRY[key].restart == "live":
            continue
        if before.get(key) != after.get(key) and not local(key):
            reasons.append("scope_changed")
            break
    extra = contributed(
        "scope_restart_reasons", dict(snapshot_before) if snapshot_before else None,
        dict(snapshot_after) if snapshot_after else None,
        frozenset(key for key, source in layers.items() if source in LOCAL_SOURCES))
    if isinstance(extra, (list, tuple)):
        reasons.extend(reason for reason in extra if isinstance(reason, str))
    return reasons


# --------------------------------------------------------------------------- apply
_state_lock = threading.Lock()
_last_sources: Optional[dict[str, str]] = None
_last_invalid: dict[str, str] = {}
_last_scope_status = "not_applied"


def _token_identity(env: Mapping[str, str], root: Optional[os.PathLike | str]
                    ) -> tuple[str, Optional[str]]:
    """``(robot_id, fingerprint)`` of the token this process would load, or ``("", None)``."""

    load = robot_token.load_token(env, cwd=str(base_dir(root)) if root is not None else None)
    token = load.token
    if token is None:
        return "", None
    try:
        fingerprint: Optional[str] = token.fingerprint
    except Exception:  # noqa: BLE001 - no cryptography on this box: compare ids only
        fingerprint = None
    return token.robot_id, fingerprint


def _restore_from_dotenv(env: MutableMapping[str, str], keys: list[str],
                         dotenv_file: Optional[str]) -> None:
    """Put back ``.env``'s own value of keys whose inherited value was just removed.

    ``load_dotenv`` never overrides a variable that is already set, so a child process
    that inherited a derived value did not load ``.env``'s value of that key.
    """

    if not keys or not dotenv_file:
        return
    try:
        from dotenv import dotenv_values  # noqa: PLC0415

        values = dotenv_values(dotenv_file)
    except Exception:  # noqa: BLE001 - a broken .env is config's business, not ours
        return
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            env[key] = value


def _undo_unit_overlay(env: MutableMapping[str, str], sources: dict[str, str], key: str,
                       file_value: str, dotenv_file: Optional[str],
                       cache: dict[str, Optional[dict]]) -> None:
    """Take back a saved value this layer refused, when systemd already applied it.

    The units load the settings files after ``.env`` (``EnvironmentFile=``), so under
    systemd a refused, empty or misplaced saved value is ALREADY in the environment,
    over ``.env``'s own. When the environment still holds exactly that value, ``.env``'s
    value is put back (or the key removed): a refused value must never be in force.
    """

    if env.get(key) != file_value:
        return
    if "values" not in cache:
        cache["values"] = None
        if dotenv_file:
            try:
                from dotenv import dotenv_values  # noqa: PLC0415

                cache["values"] = dotenv_values(dotenv_file)
            except Exception:  # noqa: BLE001 - a broken .env is config's business
                cache["values"] = None
    own = (cache["values"] or {}).get(key)
    if isinstance(own, str) and own.strip():
        env[key] = own
        sources[key] = SOURCE_ENV
    else:
        env.pop(key, None)
        sources.pop(key, None)


def _apply(env: MutableMapping[str, str], root: Optional[os.PathLike | str],
           dotenv_file: Optional[str], service: str = SERVICE_HOSTAPP,
           pending: Optional[Mapping[str, Optional[str]]] = None,
           ) -> tuple[dict[str, str], dict[str, str], str]:
    sources: dict[str, str] = {}
    invalid: dict[str, str] = {}
    admin = service == SERVICE_ADMIN
    foreign = admin_foreign_keys() if admin else frozenset()

    # 1. Values a parent process put here from a layer other than .env: derive again.
    inherited = env.pop(SOURCES_ENV, None)
    if inherited is not None:
        try:
            marker = json.loads(inherited)
        except ValueError:
            marker = {}
        stripped = []
        if isinstance(marker, dict):
            for key, source in marker.items():
                if key in REGISTRY and source != SOURCE_ENV and key in env:
                    del env[key]
                    stripped.append(key)
        _restore_from_dotenv(env, stripped, dotenv_file)

    # 2. Explicit values (.env or the process environment).
    for key in REGISTRY:
        if _is_set(key, env.get(key)):
            sources[key] = SOURCE_ENV

    # 3. Values saved on this robot (admin page, calibration) outrank .env.
    sidecar = read_sources_sidecar(root)
    dotenv_cache: dict[str, Optional[dict]] = {}
    # emori-admin never reads the host app's secrets file.
    files = ((SETTINGS_FILE, ADMIN_SECRETS_FILE) if admin
             else (SETTINGS_FILE, SITE_SECRETS_FILE, ADMIN_SECRETS_FILE))
    for relative in files:
        stored = read_env_file(site_path(relative, root))
        # A save not yet written, read as if it were (apply's ``pending``).
        for key, value in (pending or {}).items():
            if key in REGISTRY and REGISTRY[key].file == relative:
                if value:
                    stored[key] = value
                else:
                    stored.pop(key, None)
        for key, value in stored.items():
            spec = REGISTRY[key]
            if spec.file != relative:
                invalid[key] = "key_wrong_file"
                log.warning("%s is saved in %s, which is not its file: ignored",
                            key, relative)
                _undo_unit_overlay(env, sources, key, value, dotenv_file, dotenv_cache)
                continue
            if not value.strip():
                _undo_unit_overlay(env, sources, key, value, dotenv_file, dotenv_cache)
                continue
            code = spec.validator(value)
            if code:
                invalid[key] = code
                log.warning("saved setting %s refused (%s): not applied", key, code)
                _undo_unit_overlay(env, sources, key, value, dotenv_file, dotenv_cache)
                continue
            env[key] = value
            sources[key] = sidecar.get(key, SOURCE_ADMIN)

    # 4. THIS robot's Pokamind record, for every derivable key still unset. Not for
    # emori-admin: telling which robot a snapshot belongs to takes the token, which
    # that service never loads (it derives nothing from Pokamind).
    scope_status = "auto_off"
    if admin:
        scope_status = "not_derived"
    elif auto_enabled(env):
        robot_id, fingerprint = _token_identity(env, root)
        if not robot_id:
            scope_status = "no_token"
        else:
            snapshot, scope_status = _checked_scope(robot_id, fingerprint, root)
            if snapshot is not None:
                _kept, skipped = _scope_pathways(snapshot["whoami"])
                if skipped:
                    log.info("site settings: %d Pokamind pathway(s) hold a comma or are "
                             "empty and cannot be listed in EMORI_PATHWAYS: no pathway "
                             "allowlist is derived", skipped)
                for key, value in derive_from_scope(snapshot).items():
                    if not _is_set(key, env.get(key)):
                        env[key] = value
                        sources[key] = SOURCE_POKAMIND

    # 5-7. An installed robot only: its hardware, then the shipped defaults.
    profile = hardware_profile(env)
    if profile:
        defaults = HARDWARE_PROFILES.get(profile)
        if defaults is None:
            log.warning("%s names an unknown hardware profile: no hardware defaults",
                        PROFILE_ENV)
        hardware = dict(defaults or {})
        added = contributed("hardware_defaults", profile, dict(env))
        if isinstance(added, Mapping):
            hardware.update({key: value for key, value in added.items()
                             if key in REGISTRY and isinstance(value, str)})
        for key, value in hardware.items():
            if not _is_set(key, env.get(key)):
                env[key] = value
                sources[key] = SOURCE_HARDWARE
        pinned = contributed("pinned_settings", dict(env))
        if isinstance(pinned, Mapping):
            for key, value in pinned.items():
                if (key in REGISTRY and key not in foreign and isinstance(value, str)
                        and value and not _is_set(key, env.get(key))):
                    env[key] = value
                    sources[key] = SOURCE_DEFAULT

    # 8. The marker a child process strips: names and layers, never a value.
    marker_out = {key: source for key, source in sources.items() if source != SOURCE_ENV}
    if marker_out:
        env[SOURCES_ENV] = json.dumps(marker_out, sort_keys=True, separators=(",", ":"))
    return sources, invalid, scope_status


def apply(environ: Optional[MutableMapping[str, str]] = None, *,
          root: Optional[os.PathLike | str] = None,
          dotenv_file: Optional[str] = None,
          service: str = SERVICE_HOSTAPP,
          pending: Optional[Mapping[str, Optional[str]]] = None) -> dict[str, str]:
    """Put the effective value of every site setting into ``environ``; never raises.

    Called once per process by :mod:`emori_adapter.config`, right after ``.env`` is
    loaded (``dotenv_file`` is that file). Returns ``{key: source}`` for every setting
    that has a value. On any error ``environ`` is left as it was and ``{}`` is returned.
    Only a call on ``os.environ`` itself updates what :func:`effective_sources`,
    :func:`invalid_settings` and :func:`last_scope_status` report. ``service`` is
    :data:`SERVICE_ADMIN` only from :func:`load_admin_environment`. ``pending``
    (``{key: value | None}``, as :func:`save_settings` takes it) is read as if already
    saved, to see what a save would put in force before it is written; such a call
    never updates what this process reports.
    """

    global _last_sources, _last_invalid, _last_scope_status
    env = os.environ if environ is None else environ
    names = set(REGISTRY) | {SOURCES_ENV}
    before = {name: env.get(name) for name in names}
    try:
        sources, invalid, scope_status = _apply(env, root, dotenv_file, service, pending)
    except Exception as exc:  # noqa: BLE001 - this layer must never stop a process
        for name, value in before.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        log.warning("site_settings_apply_failed (%s): the environment is used as loaded",
                    type(exc).__name__)
        return {}
    if env is os.environ and pending is None:
        with _state_lock:
            _last_sources = dict(sources)
            _last_invalid = dict(invalid)
            _last_scope_status = scope_status
    pairs = " ".join(f"{key}={sources[key]}" for key in sorted(sources))
    log.info("site settings: %s (scope %s)", pairs or "(none set)", scope_status)
    return dict(sources)


def admin_foreign_keys() -> frozenset[str]:
    """The keys emori-admin never holds, whatever file or parent process carries them.

    The robot's Pokamind token (``POKAMIND_ROBOT_TOKEN``; the token FILE is never
    opened), every key of :data:`SITE_SECRETS_FILE` (the SMTP password, a pasted
    key set), the host app's own secrets in ``.env`` and the retired
    credentials. Its own PIN, hotspot, dashboard password, admin token and salt are not
    among them.
    """

    return frozenset({robot_token.TOKEN_ENV, *_HOSTAPP_ENV_SECRETS,
                      *RETIRED_CREDENTIAL_KEYS,
                      *(key for key, spec in REGISTRY.items()
                        if spec.file == SITE_SECRETS_FILE)})


# A credential-shaped name: a TOKEN, SECRET, PASSWORD, KEY or SALT word in it.
_CREDENTIAL_NAME_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|SECRETS|PASSWORD|PASSWD|PASS|PWD|KEY|KEYS|SALT|"
    r"CREDENTIAL|CREDENTIALS)(?:_|$)")


def admin_own_credentials() -> frozenset[str]:
    """The credential-shaped keys emori-admin holds: what it serves and its own token."""

    return frozenset({*admin_keys(), "EMORI_ADMIN_TOKEN", "EMORI_ADMIN_SALT"})


def admin_refuses(key: str) -> bool:
    """True for a key emori-admin never holds.

    The keys of :func:`admin_foreign_keys`, plus ANY credential-shaped name that is not
    one of :func:`admin_own_credentials`. The second rule covers what no list can name:
    a leftover credential an older robot's ``.env`` still carries, which this release
    no longer reads, never reaches emori-admin or a process it starts.
    """

    if key in admin_foreign_keys():
        return True
    return bool(_CREDENTIAL_NAME_RE.search(key)) and key not in admin_own_credentials()


def load_admin_environment(environ: Optional[MutableMapping[str, str]] = None, *,
                           root: Optional[os.PathLike | str] = None,
                           dotenv_file: Optional[str] = None) -> dict[str, str]:
    """emori-admin's settings: what it serves and nothing it does not hold.

    ``.env`` (``dotenv_file``, else the install's own, found by
    :func:`find_install_dotenv` from this package, else ``./.env``) is loaded WITHOUT
    the keys :func:`admin_refuses` (the foreign keys and any other credential-shaped
    name that is not its own), values already in the environment winning
    (python-dotenv's own rule); then those keys are removed from ``environ`` whatever
    put them there (a parent process, an old unit file); then :func:`apply` runs for
    :data:`SERVICE_ADMIN` (``configs/site/settings.env`` and ``secrets/admin.env`` only,
    no token, nothing derived from Pokamind). Returns :func:`apply`'s sources. Never
    raises; a ``.env`` that cannot be read leaves the process environment as it is.
    """

    env = os.environ if environ is None else environ
    if dotenv_file is None:
        dotenv_file = (find_install_dotenv(os.path.dirname(os.path.abspath(__file__)))
                       or (os.path.abspath(".env") if os.path.isfile(".env") else ""))
    if dotenv_file:
        try:
            from dotenv import dotenv_values  # noqa: PLC0415

            values = dotenv_values(dotenv_file)
        except Exception as exc:  # noqa: BLE001 - the process env alone is a valid setup
            log.warning("emori-admin: could not read .env (%s): using the process "
                        "environment as-is", type(exc).__name__)
            values = {}
        for key, value in values.items():
            if admin_refuses(key) or value is None or key in env:
                continue
            env[key] = value
    _drop_refused(env)
    sources = apply(env, root=root, dotenv_file=dotenv_file or None, service=SERVICE_ADMIN)
    # apply() may put a value back from .env (an inherited, stripped key): never one of
    # these.
    _drop_refused(env)
    return sources


def _drop_refused(env: MutableMapping[str, str]) -> None:
    for key in [key for key in list(env) if admin_refuses(key)]:
        env.pop(key, None)


def effective_sources(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """``{key: source}`` for every site setting that has a value in this process.

    A key that is absent has no value: its reader's code default applies. Without
    ``environ`` it is what this process's :func:`apply` found; otherwise it is computed
    from ``environ`` and its ``EMORI_SITE_SOURCES`` marker.
    """

    if environ is None:
        with _state_lock:
            if _last_sources is not None:
                return dict(_last_sources)
    env = os.environ if environ is None else environ
    try:
        marker = json.loads(env.get(SOURCES_ENV) or "{}")
    except ValueError:
        marker = {}
    marker = marker if isinstance(marker, dict) else {}
    out: dict[str, str] = {}
    for key in REGISTRY:
        if marker.get(key) in SOURCES and marker.get(key) != SOURCE_ENV:
            out[key] = marker[key]
        elif _is_set(key, env.get(key)):
            out[key] = SOURCE_ENV
    return out


def invalid_settings() -> dict[str, str]:
    """``{key: slug}`` for saved values this process's :func:`apply` refused."""

    with _state_lock:
        return dict(_last_invalid)


def last_scope_status() -> str:
    """What this process's :func:`apply` found about the scope snapshot.

    ``ok``, ``no_token``, ``auto_off``, ``not_applied``, or a
    :func:`scope_snapshot_status` code.
    """

    with _state_lock:
        return _last_scope_status


# --------------------------------------------------------------------------- add-on
def _merge_extension_registry() -> None:
    """Add the installed add-on's settings to :data:`REGISTRY` (a name already there wins).

    Runs once, at the end of this module's import, so every reader sees one complete
    registry. The hook runs while this module is only just defined: it builds its entries
    with :class:`KeySpec` and the ``*_check`` helpers above and imports nothing heavier.
    """

    # config imports this module before it loads .env, so a switch-off set only there is
    # read here; the process environment wins over it, as load_dotenv leaves it.
    switch = os.environ.get("EMORI_ADDONS")
    if switch is None:
        dotenv_file = find_install_dotenv(os.path.dirname(os.path.abspath(__file__)))
        if dotenv_file:
            try:
                from dotenv import dotenv_values  # noqa: PLC0415

                switch = dotenv_values(dotenv_file).get("EMORI_ADDONS")
            except Exception:  # noqa: BLE001 - a broken .env is config's business
                switch = None
    if addons_switched_off(switch):
        return
    specs = contributed("settings_registry")
    for spec in specs if isinstance(specs, (list, tuple)) else ():
        if isinstance(spec, KeySpec) and spec.key not in REGISTRY:
            REGISTRY[spec.key] = spec


_merge_extension_registry()
