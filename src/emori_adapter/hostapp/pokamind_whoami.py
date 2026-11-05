"""``GET /api/robot/whoami`` -- what Pokamind says THIS robot is allowed to do.

The ONE whoami client, used by readycheck's
``pokamind_whoami`` / ``voice_menu_in_scope`` gates and by the hostapp admin API that
the phone's Pokamind tab reads. It authenticates exactly like the control plane: a
fresh 45-second assertion signed by this robot's own per-robot token
(``robot_token.RobotToken.control_assertion``), so it needs no Google credential and
no shared secret -- the robot proves who it is, and Pokamind answers with that robot's
org, scope and the availability of every module it may start.

**Base URL.** ``POKAMIND_BASE`` is the Pokamind API ORIGIN with no ``/api`` suffix
(``https://api.pokamind.com`` in production, ``http://localhost:<port>`` for local
testing): the partner SSO is ``{POKAMIND_BASE}/partner/sso/login`` and the robot routes
are under ``/api``. whoami is ``{POKAMIND_BASE}/api/robot/whoami``. The base must be
``https``; plain ``http`` is accepted only for a loopback host (local testing), because
the assertion is a bearer credential for its 45 seconds.

**Contract 2.** Every request carries ``X-EMORI-Contract: 2``
(:data:`CONTRACT_HEADER`). A server that does not know the header answers today's v1
body (12 top-level keys); one that does MAY add any of four named-optional keys:
``default_language`` and ``app_origin`` at the top level, ``menu_name`` and
``menu_keywords`` in a module row. Nothing else changes on the wire.

The response is parsed STRICTLY against the contract. Every REQUIRED key must be
present with its exact type, and a key that is neither required nor one of the
named-optional keys makes the whole body ``invalid_whoami_response`` (a new server field
is a reviewed client change, not something silently consumed); ``credential`` is an exact
key set; ``features`` is a flat map of named flags passed through unread (an installed
add-on may pin the exact names it needs, see :func:`_features`). The
named-optional keys are ADVISORY: a malformed value is treated as absent and only its
key NAME is recorded in :attr:`Whoami.ignored_keys` -- it never invalidates the body.
No body text and no assertion is ever logged or returned, and every failure is a fixed,
content-free code. :func:`fetch_whoami` never raises.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from emori_adapter.hostapp.extensions import contributed

WHOAMI_PATH = "/api/robot/whoami"
DEFAULT_TIMEOUT_S = 5.0
# The request header that opts this robot into the contract-2 response keys. A server
# that does not know it answers the v1 body, which this parser reads exactly as before.
CONTRACT_HEADER = "X-EMORI-Contract"
CONTRACT_VERSION = "2"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TOP_KEYS = frozenset({
    "robot_id", "display_name", "status", "credential", "org", "partner", "course_id",
    "languages", "pathways", "identity_domains", "modules", "features",
})
_CREDENTIAL_KEYS = frozenset({"kind", "key_fingerprint", "key_version"})
_CREDENTIAL_KINDS = frozenset({"fleet", "registry"})
_MODULE_KEYS = frozenset({"module_id", "title", "category", "language", "available", "problem"})
# Contract 2: named-optional, advisory keys (a malformed value is ignored, by name).
_TOP_OPTIONAL = frozenset({"default_language", "app_origin"})
_MODULE_OPTIONAL = frozenset({"menu_name", "menu_keywords"})
MODULE_PROBLEMS = frozenset({"not_found", "not_visible_to_robot", "language_mismatch"})
# ``features`` is a flat map of named booleans. The kiosk reads none of them; an add-on that
# does names the exact set it expects through its ``whoami_feature_keys`` hook.
_FEATURE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_MAX_FEATURES = 16
_STATUSES = frozenset({"active", "revoked"})
# Module ids are restricted to a safe character set: the same grammar readycheck
# already applies to menu ids.
_MODULE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,1500}\Z")
_MAX_LIST = 500
_MAX_TEXT = 500
_LANGUAGE_RE = re.compile(r"[a-z]{2,3}\Z")
_MAX_ORIGIN = 200
_MAX_MENU_NAME = 60
_MAX_KEYWORDS = 12
_MAX_KEYWORD = 40

# The server's own content-free control-plane codes this client may act on. Anything
# else in a body is ignored: a body can name a failure, never introduce text.
_BODY_CODES = {
    "robot_revoked": "robot_revoked",
    "robot_unknown": "robot_unknown",
    "authentication_failed": "whoami_auth_failed",
    "authentication_unavailable": "whoami_unavailable",
    "robot_control_unavailable": "whoami_unavailable",
}

# Every code a WhoamiResult can carry. "ok" is the only success.
RESULT_CODES = frozenset({
    "ok",
    "no_robot_token",
    "whoami_base_url_invalid",
    "whoami_signing_unavailable",
    "whoami_unreachable",
    "whoami_timeout",
    "whoami_auth_failed",
    "robot_revoked",
    "robot_unknown",
    "whoami_not_supported",
    "whoami_rate_limited",
    "whoami_unavailable",
    "whoami_server_error",
    "whoami_rejected",
    "invalid_whoami_response",
    "whoami_robot_id_mismatch",
    "whoami_key_mismatch",
})


class WhoamiError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in RESULT_CODES else "invalid_whoami_response"
        super().__init__(self.code)


@dataclass(frozen=True)
class WhoamiModule:
    module_id: str
    title: Optional[str]
    category: Optional[str]
    language: Optional[str]
    available: bool
    problem: Optional[str]
    # Contract 2 (advisory; None when absent or malformed): the spoken-menu copy.
    menu_name: Optional[str] = None
    menu_keywords: Optional[tuple[str, ...]] = None

    def to_dict(self) -> dict:
        return {"module_id": self.module_id, "title": self.title, "category": self.category,
                "language": self.language, "available": self.available,
                "problem": self.problem, "menu_name": self.menu_name,
                "menu_keywords": (list(self.menu_keywords)
                                  if self.menu_keywords is not None else None)}


@dataclass(frozen=True)
class Whoami:
    robot_id: str
    display_name: str
    status: str
    credential_kind: str
    key_fingerprint: Optional[str]
    key_version: Optional[int]
    org: str
    partner: str
    course_id: str
    languages: tuple[str, ...]
    pathways: tuple[str, ...]
    identity_domains: tuple[str, ...]
    modules: tuple[WhoamiModule, ...]
    features: dict = field(default_factory=dict)
    # Contract 2 (advisory; None when absent or malformed).
    default_language: Optional[str] = None
    app_origin: Optional[str] = None
    # 2 when the body carried any contract-2 key (valid or not), else 1.
    contract: int = 1
    # The NAMES of contract-2 keys whose value was malformed and therefore ignored,
    # e.g. ``app_origin`` or ``modules[3].menu_name``. Never a value.
    ignored_keys: tuple[str, ...] = ()

    def module(self, module_id: str) -> Optional[WhoamiModule]:
        for module in self.modules:
            if module.module_id == module_id:
                return module
        return None

    def problems(self) -> tuple[WhoamiModule, ...]:
        """Modules in scope that a learner could not actually be started on."""

        return tuple(m for m in self.modules if not m.available or m.problem is not None)

    def to_dict(self) -> dict:
        return {
            "robot_id": self.robot_id,
            "display_name": self.display_name,
            "status": self.status,
            "credential": {"kind": self.credential_kind,
                           "key_fingerprint": self.key_fingerprint,
                           "key_version": self.key_version},
            "org": self.org,
            "partner": self.partner,
            "course_id": self.course_id,
            "languages": list(self.languages),
            "pathways": list(self.pathways),
            "identity_domains": list(self.identity_domains),
            "modules": [m.to_dict() for m in self.modules],
            "features": dict(self.features),
            "default_language": self.default_language,
            "app_origin": self.app_origin,
            "ignored_keys": list(self.ignored_keys),
        }


@dataclass(frozen=True)
class WhoamiResult:
    """One whoami call. ``ok`` only with code ``"ok"`` and a parsed :class:`Whoami`."""

    ok: bool = False
    code: str = "no_robot_token"
    http_status: int = 0
    whoami: Optional[Whoami] = None
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "code": self.code, "http_status": self.http_status,
                "checked_at": self.checked_at,
                "whoami": self.whoami.to_dict() if self.whoami is not None else None}


def _loopback_or_https(parts: Any) -> bool:
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)


def whoami_url(base_url: object) -> str:
    """``{base}/api/robot/whoami`` for a valid API origin, else :class:`WhoamiError`."""

    if type(base_url) is not str:
        raise WhoamiError("whoami_base_url_invalid")
    base = base_url.strip().rstrip("/")
    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise WhoamiError("whoami_base_url_invalid") from None
    if (
        not base
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https"
                or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))
    ):
        raise WhoamiError("whoami_base_url_invalid")
    return base + WHOAMI_PATH


# --------------------------------------------------------------------------- parsing
def _invalid() -> WhoamiError:
    return WhoamiError("invalid_whoami_response")


def _exact(value: object, keys: frozenset[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise _invalid()
    return value


def _features(value: object) -> dict:
    """The ``features`` map: named booleans, passed through unread.

    With an add-on installed that names the flags it needs, the map must hold exactly
    those; otherwise any small set of well-named flags is accepted.
    """

    if type(value) is not dict or len(value) > _MAX_FEATURES:
        raise _invalid()
    pinned = contributed("whoami_feature_keys")
    if pinned is not None:
        _exact(value, frozenset(pinned))
    for name, flag in value.items():
        if type(name) is not str or _FEATURE_NAME_RE.fullmatch(name) is None:
            raise _invalid()
        _flag(flag)
    return value


def _keys(value: object, required: frozenset[str], optional: frozenset[str]) -> dict:
    """Every ``required`` key present, and any other key one of ``optional``."""

    if type(value) is not dict:
        raise _invalid()
    keys = set(value)
    if not required <= keys or not keys <= (required | optional):
        raise _invalid()
    return value


def _has_control(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _base_code(value: str) -> str:
    return value.strip().lower().replace("_", "-").split("-", 1)[0]


# --- contract-2 values: each returns the value, or raises _Malformed (=> ignored) --------
class _Malformed(Exception):
    pass


def _opt_default_language(value: object, languages: tuple[str, ...]) -> Optional[str]:
    """``^[a-z]{2,3}$`` and, when ``languages`` is not empty, one of them (base code)."""

    if value is None:
        return None
    if type(value) is not str or _LANGUAGE_RE.fullmatch(value) is None:
        raise _Malformed()
    if languages and not any(_base_code(item) == value for item in languages):
        raise _Malformed()
    return value


def _opt_app_origin(value: object) -> Optional[str]:
    """``https://<host>[:port]`` (http only for loopback): no path, query, fragment or
    userinfo, at most 200 characters."""

    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _MAX_ORIGIN \
            or value != value.strip() or _has_control(value):
        raise _Malformed()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        raise _Malformed() from None
    if (not host or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment
            or "?" in value or "#" in value or not _loopback_or_https(parts)):
        raise _Malformed()
    return value


def _menu_text(value: object, max_len: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > max_len \
            or "," in value or _has_control(value):
        raise _Malformed()
    return value.strip()


def _opt_menu_name(value: object) -> Optional[str]:
    """1-60 characters, no comma, CR, LF (or any other control character)."""

    if value is None:
        return None
    return _menu_text(value, _MAX_MENU_NAME)


def _opt_menu_keywords(value: object) -> Optional[tuple[str, ...]]:
    """At most 12 strings of 1-40 characters, none holding a comma, CR or LF."""

    if value is None:
        return None
    if type(value) is not list or len(value) > _MAX_KEYWORDS:
        raise _Malformed()
    return tuple(_menu_text(item, _MAX_KEYWORD) for item in value)


def _optional(raw: dict, key: str, check: Any, ignored: list[str], name: str) -> Any:
    """``raw[key]`` checked, None when absent; a malformed value is None and its NAME
    goes into ``ignored``."""

    if key not in raw:
        return None
    try:
        return check(raw[key])
    except _Malformed:
        ignored.append(name)
        return None


def _text(value: object, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if type(value) is not str or len(value) > _MAX_TEXT:
        raise _invalid()
    return value


def _strings(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_LIST:
        raise _invalid()
    return tuple(_text(item) for item in value)  # type: ignore[misc]


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise _invalid()
    return value


def _module(value: object, index: int, ignored: list[str]) -> tuple[WhoamiModule, bool]:
    """One module row, and whether it carried any contract-2 key."""

    raw = _keys(value, _MODULE_KEYS, _MODULE_OPTIONAL)
    module_id = raw["module_id"]
    if type(module_id) is not str or _MODULE_ID_RE.fullmatch(module_id) is None:
        raise _invalid()
    problem = raw["problem"]
    if problem is not None and problem not in MODULE_PROBLEMS:
        raise _invalid()
    prefix = "modules[%d]." % index
    module = WhoamiModule(
        module_id=module_id,
        title=_text(raw["title"], optional=True),
        category=_text(raw["category"], optional=True),
        language=_text(raw["language"], optional=True),
        available=_flag(raw["available"]),
        problem=problem,
        menu_name=_optional(raw, "menu_name", _opt_menu_name, ignored,
                            prefix + "menu_name"),
        menu_keywords=_optional(raw, "menu_keywords", _opt_menu_keywords, ignored,
                                prefix + "menu_keywords"),
    )
    return module, bool(set(raw) & _MODULE_OPTIONAL)


def parse_whoami(raw: object, *, robot_id: str, fingerprint: str = "") -> Whoami:
    """Validate one whoami body against the whoami contract.

    ``robot_id`` / ``fingerprint`` are what the robot SIGNED with: an answer about a
    different robot, or a fleet record holding a different key than the one that
    just authenticated, is reported under its own code instead of being believed.
    """

    value = _keys(raw, _TOP_KEYS, _TOP_OPTIONAL)
    credential = _exact(value["credential"], _CREDENTIAL_KEYS)
    kind = credential["kind"]
    if kind not in _CREDENTIAL_KINDS:
        raise _invalid()
    key_fingerprint = _text(credential["key_fingerprint"], optional=True)
    key_version = credential["key_version"]
    if key_version is not None and (type(key_version) is not int or key_version < 0):
        raise _invalid()
    status = value["status"]
    if status not in _STATUSES:
        raise _invalid()
    modules = value["modules"]
    if type(modules) is not list or len(modules) > _MAX_LIST:
        raise _invalid()
    features_raw = _features(value["features"])
    languages = _strings(value["languages"])
    ignored: list[str] = []
    parsed = [_module(item, index, ignored) for index, item in enumerate(modules)]
    contract_2 = bool(set(value) & _TOP_OPTIONAL) or any(extra for _m, extra in parsed)
    whoami = Whoami(
        robot_id=_text(value["robot_id"]) or "",
        display_name=_text(value["display_name"]) or "",
        status=status,
        credential_kind=kind,
        key_fingerprint=key_fingerprint,
        key_version=key_version,
        org=_text(value["org"]) or "",
        partner=_text(value["partner"]) or "",
        course_id=_text(value["course_id"]) or "",
        languages=languages,
        pathways=_strings(value["pathways"]),
        identity_domains=_strings(value["identity_domains"]),
        modules=tuple(module for module, _extra in parsed),
        features={key: features_raw[key] for key in sorted(features_raw)},
        default_language=_optional(
            value, "default_language",
            lambda item: _opt_default_language(item, languages), ignored,
            "default_language"),
        app_origin=_optional(value, "app_origin", _opt_app_origin, ignored, "app_origin"),
        contract=2 if contract_2 else 1,
        ignored_keys=tuple(ignored),
    )
    if whoami.robot_id != robot_id:
        raise WhoamiError("whoami_robot_id_mismatch")
    if (fingerprint and whoami.credential_kind == "fleet"
            and whoami.key_fingerprint is not None and whoami.key_fingerprint != fingerprint):
        raise WhoamiError("whoami_key_mismatch")
    return whoami


def _status_code(response: Any) -> str:
    status = int(response.status_code)
    body_code = None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a proxy's HTML error page carries no code
        payload = None
    pokamind_said = type(payload) is dict and type(payload.get("error_code")) is str
    if pokamind_said:
        body_code = _BODY_CODES.get(payload["error_code"])
    if status in (401, 403):
        if not pokamind_said:
            # Not Pokamind's answer: a captive portal, a site proxy or a firewall says
            # 401/403 with an HTML page (or nothing). That says nothing about this token,
            # and reading it as a refusal would turn a valid token away (the deploy's
            # token check, the admin page's Replace token) for a network problem.
            return "whoami_unreachable"
        # Pokamind's own JSON: only the exact revocation/unknown codes are believed, a
        # control plane that is down is down, and anything else is "this robot could
        # not authenticate".
        if body_code in ("robot_revoked", "robot_unknown", "whoami_unavailable"):
            return body_code
        return "whoami_auth_failed"
    if status == 404:
        return "whoami_not_supported"
    if status == 429:
        return "whoami_rate_limited"
    if status in (408, 504):
        return "whoami_timeout"
    if status == 503:
        return "whoami_unavailable"
    if status >= 500:
        return "whoami_server_error"
    return "whoami_rejected"


# --------------------------------------------------------------------------- the call
def fetch_whoami(base_url: object, robot_token: Any, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 transport: Any = None, now: Optional[float] = None) -> WhoamiResult:
    """Ask Pokamind who this robot is, signed with ``robot_token``. Never raises."""

    checked_at = float(now if now is not None else time.time())
    if robot_token is None:
        return WhoamiResult(code="no_robot_token", checked_at=checked_at)
    try:
        url = whoami_url(base_url)
    except WhoamiError as e:
        return WhoamiResult(code=e.code, checked_at=checked_at)
    try:
        assertion = robot_token.control_assertion()
        fingerprint = robot_token.fingerprint
    except Exception:  # noqa: BLE001 - PyJWT/cryptography missing or broken
        return WhoamiResult(code="whoami_signing_unavailable", checked_at=checked_at)

    import httpx  # noqa: PLC0415 - core dependency

    try:
        with httpx.Client(timeout=float(timeout_s), transport=transport,
                          follow_redirects=False, trust_env=False) as client:
            response = client.get(url, headers={"Authorization": f"Bearer {assertion}",
                                                "Accept": "application/json",
                                                CONTRACT_HEADER: CONTRACT_VERSION})
    except httpx.TimeoutException:
        return WhoamiResult(code="whoami_timeout", checked_at=checked_at)
    except Exception:  # noqa: BLE001 - never the exception text: it can quote the URL
        return WhoamiResult(code="whoami_unreachable", checked_at=checked_at)
    status = int(response.status_code)
    if not 200 <= status < 300:
        return WhoamiResult(code=_status_code(response), http_status=status,
                            checked_at=checked_at)
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return WhoamiResult(code="invalid_whoami_response", http_status=status,
                            checked_at=checked_at)
    try:
        whoami = parse_whoami(payload, robot_id=robot_token.robot_id, fingerprint=fingerprint)
    except WhoamiError as e:
        return WhoamiResult(code=e.code, http_status=status, checked_at=checked_at)
    return WhoamiResult(ok=True, code="ok", http_status=status, whoami=whoami,
                        checked_at=checked_at)
