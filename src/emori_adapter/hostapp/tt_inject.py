"""Turn-taking override channel (adapter half): validated ``window.__EMORI_TT`` overrides.

The in-iframe endpointer reads its per-language control law from
``window.__EMORI_TT[lang]`` and deep-merges it over its build-time defaults.  A headless
kiosk has no console, so this module is the operator's only way in: it owns the
patch, and NOTHING reaches a browser that has not passed ``validate_tt_patch``.

The validator is the security boundary, not a convenience:

* only the kiosk languages ``{"en", "ca"}`` may key a patch
  (``tests/test_catalan_kiosk.py`` pins that set);
* leaves may only be a bool, a finite number, or a short ``[A-Za-z][A-Za-z0-9_-]``
  string -- never a list, a callable, or unbounded nesting;
* ``__proto__`` / ``constructor`` / ``prototype`` keys are rejected outright, because
  the merge assigns ``out[k]`` for every own enumerable key of the override and a
  JSON-parsed ``__proto__`` key would otherwise reach the prototype chain;
* depth, leaf count and serialized size are all bounded.

Everything is inert by default: an empty store means the ``/session`` page has
nothing to post, so the channel does not exist until an operator fills it.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
from urllib.parse import urlsplit

log = logging.getLogger("emori.hostapp.tt")

# The kiosk visitor languages, exactly (pages.UI_STRINGS).  Kept literal rather than
# imported so a future UI-string addition cannot silently widen the patch surface.
TT_LANGS = frozenset({"en", "ca"})

_MAX_DEPTH = 4
_MAX_LEAVES = 64
_MAX_BYTES = 4096
# Config leaf names are camelCase in the endpointer's config.
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
_STR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_BANNED_KEYS = frozenset({"__proto__", "constructor", "prototype"})


class TtPatchError(ValueError):
    """A rejected patch.  ``code`` is a safe lowercase error code (see server.py)."""

    def __init__(self, code: str = "invalid_tt_patch") -> None:
        super().__init__(code)
        self.code = code


def _check_node(node: object, depth: int, leaves: list[int]) -> None:
    if depth > _MAX_DEPTH:
        raise TtPatchError()
    if isinstance(node, bool):          # before int: bool is an int subclass
        leaves[0] += 1
    elif isinstance(node, (int, float)):
        if not math.isfinite(float(node)):
            raise TtPatchError()
        leaves[0] += 1
    elif isinstance(node, str):
        if _STR_RE.fullmatch(node) is None:
            raise TtPatchError()
        leaves[0] += 1
    elif isinstance(node, dict):
        for key, value in node.items():
            if (not isinstance(key, str) or key in _BANNED_KEYS
                    or _KEY_RE.fullmatch(key) is None):
                raise TtPatchError()
            _check_node(value, depth + 1, leaves)
    else:
        raise TtPatchError()
    if leaves[0] > _MAX_LEAVES:
        raise TtPatchError()


def validate_tt_patch(raw: object) -> dict:
    """Return an owned deep copy of ``raw``, or raise ``TtPatchError``."""

    if not isinstance(raw, dict):
        raise TtPatchError()
    out: dict = {}
    leaves = [0]
    for lang, cfg in raw.items():
        if lang not in TT_LANGS or not isinstance(cfg, dict):
            raise TtPatchError()
        _check_node(cfg, 1, leaves)
        out[lang] = copy.deepcopy(cfg)
    if len(json.dumps(out, allow_nan=False).encode("utf-8")) > _MAX_BYTES:
        raise TtPatchError()
    return out


def parse_tt_patch_env(value: str) -> dict:
    """Parse ``EMORI_TT_OVERRIDES``.  A malformed seed is inert, never fatal."""

    text = (value or "").strip()
    if not text:
        return {}
    try:
        return validate_tt_patch(json.loads(text))
    except (TtPatchError, ValueError):
        # Content-free: a rejected patch is never echoed back into the log.
        log.warning("EMORI_TT_OVERRIDES ignored: not a valid __EMORI_TT patch")
        return {}


def _normalize_origin(raw: str) -> str | None:
    parts = urlsplit((raw or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return None
    if not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def frontend_origin(pokamind_base: str) -> str | None:
    """The origin the SESSION DOCUMENT runs on, derived from the API origin.

    The SSO endpoint redirects from the API host to the app host (e.g. api.* -> app.*):
    ``https://api.pokamind.com`` -> ``https://app.pokamind.com``. A development setup
    names its app origin itself (whoami ``app_origin``, or ``EMORI_TT_TARGET_ORIGINS``).
    The endpointer -- and the patch that tunes it -- live in that second document, so
    the API origin alone is never a sufficient target.

    Returns ``None`` when the shape is not recognised; callers then keep only the
    origins they were given explicitly.  A convenience for the deployments we ship,
    NOT a replacement for ``EMORI_TT_TARGET_ORIGINS``.
    """

    origin = _normalize_origin(pokamind_base)
    if origin is None:
        return None
    scheme, _, netloc = origin.partition("://")
    host, sep, port = netloc.partition(":")
    if host.startswith("api."):
        return f"{scheme}://app.{host[4:]}{sep}{port}"
    return None


def target_origins(raw: str, pokamind_base: str,
                   app_origin: str | None = None) -> list[str]:
    """Explicit ``postMessage`` targetOrigins for the patch -- never ``'*'``.

    ``raw`` is ``EMORI_TT_TARGET_ORIGINS`` (comma separated); ``pokamind_base`` is
    always appended because the frame first loads on the SSO origin, and so is the
    FRONTEND origin, because after the SSO 302 the session document runs THERE.

    The frontend origin is ``app_origin`` when this robot's Pokamind record names one
    (whoami ``app_origin``, contract 2). Without it, it is derived from the API origin
    (:func:`frontend_origin`), which is a hostname guess and stays the fallback.

    That origin is not a nicety.  If the frontend origin is not listed the browser
    silently drops the patch: a robot configured with a 1.3 s endpoint silence whose
    page posts the patch only to ``https://api.pokamind.com`` runs the 2.5 s built-in
    default instead.  Deriving it narrows that failure to
    deployments whose hostnames we do not recognise, where ``EMORI_TT_TARGET_ORIGINS``
    is still the answer.

    Fail-closed by design: an unlisted origin means "not delivered", never "delivered
    to the wrong document".
    """

    candidates = list((raw or "").split(","))
    candidates.append(pokamind_base or "")
    named = _normalize_origin(app_origin) if isinstance(app_origin, str) else None
    derived = named or frontend_origin(pokamind_base or "")
    if derived:
        candidates.append(derived)
    out: list[str] = []
    for candidate in candidates:
        origin = _normalize_origin(candidate)
        if origin is not None and origin not in out:
            out.append(origin)
    return out


class TtOverrideStore:
    """The single owner of the live patch, plus a monotonic revision for polling."""

    def __init__(self, seed: str = "") -> None:
        self._lock = threading.Lock()
        self._patch = parse_tt_patch_env(seed)
        # rev 0 == "nothing was ever set"; the page treats 0 as no-op.
        self._rev = 1 if self._patch else 0

    def snapshot(self) -> dict:
        with self._lock:
            return {"rev": self._rev, "patch": copy.deepcopy(self._patch)}

    def set(self, raw: object) -> dict:
        patch = validate_tt_patch(raw)   # validate OUTSIDE the mutation
        with self._lock:
            self._patch = patch
            self._rev += 1
            return {"rev": self._rev, "patch": copy.deepcopy(self._patch)}
