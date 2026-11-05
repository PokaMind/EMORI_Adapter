"""``emori-admin`` — the standalone, phone-first robot management service.

One deployment problem shaped everything here: a client site, a robot in a box,
an operator with nothing but a phone.  They must be able to put the robot on the
site Wi-Fi, hear the speaker, set the language, restart a service, run the
readiness check and read a log — without SSH, without a laptop, and *while the
thing they are fixing is broken*.

That last clause is why this is its own process on its own port (``:8070``) with
its own systemd unit and no ``After=``/``PartOf=`` on any EMORI unit.  It
restarts the hostapp; it cannot live inside it.

Consequences that run through every line below:

* **Nothing may crash or hang it.**  Every subprocess is an argv list with a
  timeout, every HTTP call is bounded httpx, and both come back as records
  (:mod:`emori_adapter.admin.probes`).  A dead daemon, a missing nmcli, a wedged
  hostapp: all of them are a ``{"ok": false, "error": "..."}`` body, never a
  traceback and never a stuck request thread.
* **Fail closed.**  ``EMORI_ADMIN_PIN`` unset (or not exactly six digits) and the
  entire surface answers 503.  There is no anonymous mode: this service can
  reboot the robot.
* **Handlers are sync ``def``**, so Starlette runs them in its threadpool and one
  20-second Wi-Fi scan cannot stall ``/healthz`` for everyone else.  Only the
  routes that must read a raw request body are ``async``.
* **The UI is one self-contained page** with no external origin of any kind —
  in AP mode there is no internet, so a CDN font would simply hang.

Routes: see docs/robot-admin.md

Env: ``EMORI_ADMIN_PIN``, ``EMORI_ADMIN_SALT``, ``EMORI_ADMIN_HOST``,
``EMORI_ADMIN_PORT``, ``EMORI_ADMIN_TOKEN``, ``EMORI_ADMIN_STATE_FILE``,
``EMORI_AP_SSID``, ``EMORI_AP_PASSWORD``, ``EMORI_ADMIN_WIFI_WATCHDOG``,
``EMORI_ADMIN_WIFI_GRACE_S``, ``EMORI_ADMIN_HOST_BACKEND``,
``EMORI_SITE_AUTO_APPLY_IDLE_S`` (:mod:`.site_apply`), ``EMORI_DASH_PASSWORD`` (reveal
and reset; the hostapp reads it) (plus
``REACHY_BASE_URL`` / ``HOSTAPP_PORT`` /
``POKAMIND_BASE`` / ``EMORI_LOG_LEVEL`` / ``EMORI_LOG_WIRE``, which it shares
with the hostapp).
"""

# NOTE: no `from __future__ import annotations` -- FastAPI evaluates the handler
# annotations (Request, and the `dict` body params) at def time inside
# create_app()'s closure; stringized annotations would resolve against module
# globals and silently turn Request into a required query parameter.

import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Optional

from emori_adapter.admin import audio as audio_mod
from emori_adapter.admin import (auth, backend_check, host_backend, netman, network, probes,
                                 site_apply)
from emori_adapter.admin import system as system_mod
from emori_adapter.admin.state import AdminState

log = logging.getLogger("emori.admin")

DEFAULT_PORT = 8070
DEFAULT_HOST = "0.0.0.0"

# The Home tab polls every 5 s and two phones may be open; 3 s of cache turns
# that into a bounded, predictable load on a CM4 with ~0.9 of a core to spare.
OVERVIEW_TTL_S = 3.0
# ...but the two probes that leave the robot (the Pokamind origin, the
# connectivity check) get a much longer TTL underneath: at 3 s the admin page
# would put a request per 5 s on a remote origin for as long as it stays open,
# and neither answer changes that fast.
REMOTE_TTL_S = 30.0

ENV_TOKEN = "EMORI_ADMIN_TOKEN"
TOKEN_HEADER = "X-EMORI-Admin-Token"
# Every failed /api/ answer carries a short id, and the journal line saying so carries
# the same one: the phone shows a sentence and that id instead of a status code, and
# whoever reads `journalctl -u emori-admin` (the Logs tab) finds the moment it failed.
LOG_ID_HEADER = "X-EMORI-Log-Id"

# The Pokamind tab's hostapp calls wait on a live whoami round trip to Pokamind
# (the hostapp's own budget is 5 s), so they get more than the 5 s default.
POKAMIND_PROXY_TIMEOUT_S = 15.0
# How long the phone shows a revealed token before hiding it again.
REVEAL_VISIBLE_S = 30
# A test e-mail is one SMTP conversation (connect, TLS, login, send: 15 s each at worst).
RESULTS_TEST_TIMEOUT_S = 45.0
# The Home tab's setup checklist fans out to six hostapp routes: cached, like the overview.
SETUP_TTL_S = 15.0
# Site settings (GET/POST /api/settings): the keys whose change needs the PIN typed again,
# because each one moves a trust anchor -- the Pokamind origin the robot signs its calls
# to and the mail server the stored password goes to. An installed add-on names its own
# (``pin_settings``).
PIN_SETTINGS = frozenset({"POKAMIND_BASE",
                          # where the stored SMTP password is sent, as whom, and
                          # whether the server it goes to is checked at all
                          "EMORI_SMTP_HOST", "EMORI_SMTP_PORT", "EMORI_SMTP_USER",
                          "EMORI_SMTP_TLS_VERIFY"})
SETTINGS_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SETTINGS_MAX_KEYS = 64
SETTINGS_VALUE_MAX = 16384
# A menu as the page edits it (six options, short names): far below this.
MENU_BODY_MAX = 65536
# A generated dashboard password: 16 URL-safe characters (96 bits).
DASHBOARD_PASSWORD_BYTES = 12
# What the last deploy's readycheck found (one-click-deploy.sh), relative to the install
# root: the Home checklist's readycheck step before anyone pressed Run here.
DEPLOY_READYCHECK_FILE = "artifacts/last-deploy-readycheck.env"
# A pasted token is ~110 characters. Anything longer is refused here, before it is
# forwarded anywhere.
MAX_TOKEN_CHARS = 512
# The People tab: a person's handle is a 16-hex ref (hostapp roster_admin.badge_ref),
# never the badge payload; a name or an address longer than this is refused unread.
PEOPLE_REF_RE = re.compile(r"[0-9a-f]{16}\Z")
PEOPLE_FIELD_MAX = 512
PEOPLE_MAX_REFS = 1000

LANGS = ("ca", "en")

# Give NetworkManager a moment after boot before touching the Hotspot profile;
# a rebrand racing NM's own startup is how a profile ends up half-written.
AP_REBRAND_DELAY_S = 10.0
# v2 boot fallback: how long to wait for :8000 to say whether it is Pollen's
# daemon (which runs its own boot fallback) or the v2 node (which does not), and
# how long to wait for the AP rebrand before possibly raising that AP.
HOST_WIFI_RESOLVE_S = 120.0
HOST_WIFI_RESOLVE_POLL_S = 5.0
HOST_WIFI_REBRAND_WAIT_S = 60.0

_LOGIN_ERRORS = {
    "bad": "Wrong PIN.",
    "rate": "Too many attempts. Wait a minute and try again.",
}

# Served only when src/emori_adapter/admin/page.py is missing or unimportable.
# Deliberately still a WORKING login + status page rather than an apology: the
# whole point of this service is to be the thing that still works.
_FALLBACK_HTML = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__ROBOT_NAME__ admin</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#101418;
color:#e8eef4}input,button{font:inherit;padding:12px;min-height:44px;width:100%;
box-sizing:border-box;border-radius:8px;border:1px solid #33414d;margin-top:8px;
background:#1b2229;color:#e8eef4}button{background:#2b6cb0;border-color:#2b6cb0}
.e{color:#ffb4a2}code{background:#1b2229;padding:2px 4px;border-radius:4px}</style>
<h1>__ROBOT_NAME__</h1>
<p class="e">__ERROR__</p>
<p>The full admin UI could not be loaded on this build; this minimal page can
still sign you in. The JSON API under <code>/api/</code> is unaffected.</p>
<form onsubmit="return go(event)">
<input id="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
 placeholder="6-digit PIN"><button>Sign in</button></form>
<script>
async function go(e){e.preventDefault();
 const r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({pin:document.getElementById('pin').value})});
 document.querySelector('.e').textContent=r.ok?'Signed in.':'Rejected ('+r.status+').';
 if(r.ok)location.reload(); return false;}
</script>
"""

# The fail-closed page. Deliberately NOT a login form: a PIN box that can never
# succeed sends an operator hunting for the right six digits when the actual
# problem is a missing line in .env, on a robot with no screen to tell them.
_NOT_CONFIGURED_HTML = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__ROBOT_NAME__ admin - not configured</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#101418;
color:#e8eef4}code{background:#1b2229;padding:2px 6px;border-radius:4px}
h1{font-size:1.3rem}</style>
<h1>__ROBOT_NAME__ admin is closed</h1>
<p>__ERROR__</p>
<p>Set a six-digit <code>EMORI_ADMIN_PIN</code> in <code>/opt/emori/.env</code>,
then <code>sudo systemctl restart emori-admin</code>.</p>
"""


# --------------------------------------------------------------------------
# page loading (imported lazily: a broken page must not take the recovery tool off the air)
# --------------------------------------------------------------------------
def _page_constant(name: str) -> Optional[str]:
    """``LOGIN_HTML`` / ``APP_HTML`` from ``.page``, imported LAZILY.

    Lazy so a page that fails to import degrades to the fallback above rather
    than take the recovery tool off the air.
    """

    try:
        from emori_adapter.admin import page  # noqa: PLC0415 - deliberate lazy import
    except Exception:  # noqa: BLE001 - a missing/broken page is not fatal
        log.warning("admin page module unavailable; serving the fallback page")
        return None
    value = getattr(page, name, None)
    return value if isinstance(value, str) and value else None


def _render(template: Optional[str], *, error: str = "",
            parts: Optional[tuple[str, str, str]] = None) -> str:
    """Fill the placeholders.  HTML-escaped: the robot name is operator input.

    ``parts`` is an add-on's ``(tab button, tab view, script)`` (:func:`addon_page_parts`);
    without one the three add-on slots are empty and the page is the core page. The script
    runs in its own ``try``: an error in it must not stop the page's wiring below the slot.
    """

    source = template if template is not None else _FALLBACK_HTML
    tab, view, script = parts or ("", "", "")
    if script:
        script = "try{\n" + script + "\n}catch(e){console.error('add-on script',e);}"
    return (source
            .replace("__ADDON_TAB__", tab)
            .replace("__ADDON_VIEW__", view)
            .replace("__ADDON_JS__", script)
            .replace("__ROBOT_NAME__", html.escape(auth.robot_name()))
            .replace("__ERROR__", html.escape(error)))


def login_html(error_key: str = "") -> str:
    return _render(_page_constant("LOGIN_HTML"),
                   error=_LOGIN_ERRORS.get(error_key, ""))


def app_html(addon: Any = None) -> str:
    return _render(_page_constant("APP_HTML"), parts=addon_page_parts(addon))


# --------------------------------------------------------------------------
# the optional add-on
# --------------------------------------------------------------------------
# An installed add-on publishes ``admin_addon()`` (see ``hostapp.extensions.contributed``):
# an object whose attributes this service reads, each optional.
#
#   tab_id, tab_label   the tab's id (also its URL hash) and its label
#   html                the tab's ``<section class=view id=v-<tab_id> ...>`` markup
#   js                  script run inside the page after its own definitions, with the page's
#                       helpers in scope; it registers the tab (VIEWS, LOADERS, ADDON)
#   pin_settings        setting keys whose change needs the PIN typed again
#   auto_apply_reasons  restart reasons that are applied unattended once the robot is idle
#   overview()          a dict, served as ``addon`` in /api/overview
#   setup(kit)          a dict, served as ``addon`` in /api/setup
#   register(app, kit)  adds the tab's routes
#
# ``kit`` is an :class:`AddonKit`. With no add-on there is no tab, no script and no route.
_TAB_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")


def load_addon() -> Any:
    """The installed add-on's admin descriptor, or None. Never raises."""

    from emori_adapter.hostapp import extensions  # noqa: PLC0415 - stdlib-only module

    return extensions.contributed("admin_addon")


def addon_page_parts(addon: Any) -> Optional[tuple[str, str, str]]:
    """``(tab button, tab view, script)`` of an add-on's tab, or None when it has none."""

    tab_id = getattr(addon, "tab_id", None)
    label = getattr(addon, "tab_label", None)
    view = getattr(addon, "html", None)
    if not (isinstance(tab_id, str) and _TAB_ID_RE.fullmatch(tab_id)
            and isinstance(label, str) and label and isinstance(view, str) and view):
        return None
    script = getattr(addon, "js", None)
    button = ('<button class=tab role=tab type=button data-tab=%s aria-controls=v-%s>%s'
              '<i class=bdg></i></button>' % (tab_id, tab_id, html.escape(label)))
    return button, view, script if isinstance(script, str) else ""


@dataclass(frozen=True)
class AddonKit:
    """What an add-on's routes and probes are built from (``register(app, kit)``).

    ``guard(request)`` is the signed-in-phone gate and ``hostapp_ready(request)`` the same
    plus "the shared admin token is set"; each answers ``None`` when allowed, else the
    response to return. ``hostapp(method, path, json_body=None, timeout_s=...)`` calls the
    hostapp's admin API with that token and ``hostapp_json(path)`` returns a dict body or
    None. ``proxy`` maps the hostapp's answer onto ours, ``as_response`` and ``no_store``
    shape a response, and ``pin_gate(request, supplied, event)`` is the PIN-again check
    (``None`` when it matches). ``setup_changed()`` drops the cached Home checklist.
    """

    guard: Callable[[Any], Any]
    hostapp_ready: Callable[[Any], Any]
    hostapp: Callable[..., Any]
    hostapp_json: Callable[[str], Optional[dict]]
    proxy: Callable[[Any], Any]
    as_response: Callable[[Any], Any]
    no_store: Callable[[Any], Any]
    pin_gate: Callable[[Any, Any, str], Any]
    setup_changed: Callable[[], None]
    proxy_timeout_s: float = POKAMIND_PROXY_TIMEOUT_S
    value_max: int = SETTINGS_VALUE_MAX


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def admin_token() -> str:
    return os.environ.get(ENV_TOKEN, "").strip()


def _hostapp_admin(method: str, path: str, *, json_body: Any = None,
                   timeout_s: float = probes.HTTP_TIMEOUT_S) -> probes.Http:
    return probes.hostapp(method, path, json_body=json_body, timeout_s=timeout_s,
                          headers={TOKEN_HEADER: admin_token()})


def _safe(producer: Callable[[], Any], default: Any = None) -> Any:
    """Run one overview sub-probe.  A failure is that tile going grey, not a 500."""

    try:
        return producer()
    except Exception:  # noqa: BLE001 - one dead probe must not lose the other eight
        log.warning("admin overview sub-probe failed", exc_info=False)
        return default


def _header_host(value: object) -> str:
    """The ``host[:port]`` of an Origin/Referer header, lowercased.

    ``""`` when there is nothing usable to compare — the caller treats that as
    "no browser origin was declared", never as a match.
    """

    from urllib.parse import urlsplit

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        # A bare authority (no scheme) is not a legal Origin, but parsing it as
        # one keeps a malformed header from accidentally comparing equal to "".
        parts = urlsplit(text if "//" in text else "//" + text)
    except ValueError:
        return ""
    return (parts.netloc or "").strip().lower()


def origin_ok(request) -> bool:
    """Same-origin check for every ``/api/*`` request.  The CSRF defence.

    ``SameSite=lax`` on the cookie is NOT enough here, and the reason is a
    detail of the cookie spec: SameSite is scoped to the registrable *site* and
    deliberately ignores the PORT.  ``http://reachy-mini.local:8000`` — the
    Reachy daemon, unauthenticated, on this same host, and historically a
    reflector of query text straight into HTML — is therefore the same *site*
    as ``:8070``, so a Lax cookie IS attached to a request that a page there
    makes.  The routes that read no body (reboot, poweroff, hotspot, test-sound,
    scan, readycheck, logout) are plain CORS-*simple* requests: no preflight
    stands between such a page and powering a robot off.  The response is
    unreadable to the attacker; the side effect is not.

    Rules, in order:

    * **No Origin and no Referer -> allowed.**  That is ``curl``/httpx during a
      field debug, and this service exists to still be usable by hand.  A
      browser always sends ``Origin`` on a cross-origin POST, so nothing an
      attacker controls lands in this branch.
    * Otherwise the declared origin's ``host:port`` must equal the request's own
      ``Host``.  Our own page (``credentials:'same-origin'``) matches; a page on
      any other port or host does not, and neither does ``Origin: null``.

    No proxy sits in front of this service (the unit binds the port directly),
    so ``Host`` is the authority the phone actually typed.
    """

    try:
        raw = (str(request.headers.get("origin", "") or "").strip()
               or str(request.headers.get("referer", "") or "").strip())
        if not raw:
            return True
        # Present but unparseable (``Origin: null``, a bare scheme) is a declared
        # foreign origin, not an absent one: fail closed.
        declared = _header_host(raw)
        host = str(request.headers.get("host", "") or "").strip().lower()
        return bool(declared) and bool(host) and declared == host
    except Exception:  # noqa: BLE001 - a crafted header is a 403, never a 500
        return False


def admin_port() -> int:
    raw = os.environ.get("EMORI_ADMIN_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        log.warning("EMORI_ADMIN_PORT=%r is not a number; using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= value <= 65535:
        log.warning("EMORI_ADMIN_PORT=%d is out of range; using %d", value, DEFAULT_PORT)
        return DEFAULT_PORT
    return value


# --------------------------------------------------------------------------
# site settings: the keys this service owns (secrets/admin.env)
# --------------------------------------------------------------------------
def admin_key_rows(environ=None, root=None) -> list[dict]:
    """GET /api/settings rows for the keys emori-admin writes: whether set, and where from.

    Never a value: every one of them is a secret (the PIN, the hotspot, the dashboard
    password). ``source`` is ``admin`` for a usable value saved on the admin page
    (``secrets/admin.env``), else this process's own layer for it (``env`` for
    ``.env``). ``running_differs``: saved but not what this process runs with yet.
    """

    from emori_adapter import site_settings  # noqa: PLC0415 - lazy, like auth's

    env = os.environ if environ is None else environ
    stored = site_settings.read_env_file(
        site_settings.site_path(site_settings.ADMIN_SECRETS_FILE, root))
    running = site_settings.effective_sources(env)
    rows = []
    for key in site_settings.admin_keys():
        spec = site_settings.REGISTRY[key]
        saved = stored.get(key, "")
        problem = spec.validator(saved) if saved.strip() else None
        current = env.get(key) or ""
        if saved.strip() and problem is None:
            is_set, source, differs = True, site_settings.SOURCE_ADMIN, current != saved
        else:
            is_set = bool(current.strip())
            source = (running.get(key) or site_settings.SOURCE_ENV) if is_set else None
            differs = False
        rows.append({"key": key, "owner": "admin", "value": None, "set": is_set,
                     "source": source, "secret": True, "editable": False,
                     "restart": spec.restart, "running_differs": differs,
                     "problem": problem})
    return rows


def merge_admin_rows(rows: Any, environ=None, root=None) -> list[dict]:
    """The hostapp's key rows with the admin-owned ones answered by THIS service.

    The hostapp lists them too, from its own environment; emori-admin is the process
    that writes and reads them, so its rows replace those. ``running_differs`` of a key
    the HOSTAPP reads (the dashboard password) stays the hostapp's: only it knows what
    it runs with.
    """

    own = {row["key"]: row for row in admin_key_rows(environ, root)}
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        mine = own.pop(row.get("key"), None) if isinstance(row.get("key"), str) else None
        if mine is None:
            out.append(row)
            continue
        if mine["restart"] == "hostapp" and isinstance(row.get("running_differs"), bool):
            mine = dict(mine, running_differs=row["running_differs"] or mine["running_differs"])
        out.append(mine)
    out.extend(own.values())
    return out


# --------------------------------------------------------------------------
# the background workers (module level so they are testable without a
# server, and so a change to one cannot quietly capture request-scoped state)
# --------------------------------------------------------------------------
async def boot_audio_task(admin_state: AdminState) -> None:
    """Re-apply the persisted speaker/mic levels while the daemon comes up.

    Why this exists at all: the mixer is volatile, so before this service a
    reboot could silently leave the robot at volume 0 with nothing on the
    outside to show it.  The daemon's audio
    backend can take tens of seconds to bind its device on a cold boot, which
    is what the retry ladder is for; a robot with nothing persisted returns on
    the first pass and costs nothing.
    """

    for delay in audio_mod.BOOT_RETRY_DELAYS_S:
        if delay:
            await asyncio.sleep(delay)
        # to_thread: these are blocking httpx calls and this is the event loop.
        outcome = await asyncio.to_thread(audio_mod.reapply_persisted, admin_state)
        if not outcome.get("pending"):
            return
    backend = await asyncio.to_thread(host_backend.current)
    if backend == host_backend.HOST:
        log.warning("persisted audio levels could not be re-applied: the robot's "
                    "sound card never accepted them (amixer) - check `aplay -l`")
    else:
        log.warning("persisted audio levels could not be re-applied: the daemon never "
                    "accepted /api/volume/* - check reachy-mini-daemon")


async def ap_rebrand_task(done: Optional[asyncio.Event] = None) -> None:
    """One idempotent attempt to point the AP at EMORI_AP_SSID/PASSWORD."""

    try:
        await asyncio.sleep(AP_REBRAND_DELAY_S)
        outcome = await asyncio.to_thread(network.rebrand_ap)
        log.info("AP rebrand: %s", outcome.get("reason"))
    finally:
        if done is not None:
            done.set()


async def host_wifi_boot_task(rebrand_done: Optional[asyncio.Event] = None) -> None:
    """v2 only: the daemon's boot fallback (no known Wi-Fi -> raise the hotspot).

    On v1 the reachy-mini daemon runs ``ensure_wifi_on_startup()`` when it
    starts, and a robot carried to a new site comes up on its own hotspot. On
    v2 there is no daemon, so nothing would: the robot would boot onto no
    network at all, and the only way back in would be a keyboard. So when the
    Wi-Fi backend resolves to ``host`` this process runs the same check
    (:func:`.netman.ensure_wifi_on_startup`). On a daemon robot it does nothing.

    ``:8000`` may not be up yet when this service starts (the v2 node takes a
    while; with the fence flag raised the answer is immediate anyway), so an
    undecided answer is asked again for up to ``HOST_WIFI_RESOLVE_S``. Then it
    waits for the AP rebrand: the rebrand refuses to touch a LIVE Hotspot
    profile, so an AP raised first would come up under the old name.
    """

    deadline = time.monotonic() + HOST_WIFI_RESOLVE_S
    while True:
        decision = await asyncio.to_thread(host_backend.decision)
        if decision.backend == host_backend.HOST:
            break
        if decision.decisive:
            return  # the daemon is there and runs its own boot fallback
        if time.monotonic() >= deadline:
            log.info("Wi-Fi boot check not run: nothing answered on :8000 within "
                     "%.0fs to say whether this is a daemon robot", HOST_WIFI_RESOLVE_S)
            return
        await asyncio.sleep(HOST_WIFI_RESOLVE_POLL_S)
    if rebrand_done is not None:
        try:
            await asyncio.wait_for(rebrand_done.wait(), HOST_WIFI_REBRAND_WAIT_S)
        except asyncio.TimeoutError:
            pass
    outcome = await asyncio.to_thread(netman.ensure_wifi_on_startup)
    log.info("Wi-Fi boot check (host backend): %s", outcome)


async def wifi_watchdog_task() -> None:
    """Raise the hotspot after a sustained loss of Wi-Fi.

    Only ever by asking the Wi-Fi BACKEND to do it (the daemon on v1, the
    in-process :mod:`.netman` on v2), so that backend's one busy lock still
    serialises every Wi-Fi mutation on the robot.  Boot-time loss is covered by
    ``ensure_wifi_on_startup`` (the daemon's, or :func:`host_wifi_boot_task`);
    this covers the site that changes its Wi-Fi password at 14:00 and would
    otherwise leave the robot unreachable until somebody drives out with a
    keyboard.
    """

    watchdog = network.WifiWatchdog(grace_s=network.watchdog_grace_s())
    while True:
        await asyncio.sleep(network.WATCHDOG_POLL_S)
        result = await asyncio.to_thread(network.wifi_status)
        if not result.ok:
            # A backend that cannot answer is not evidence about the radio.
            # Deliberately does NOT reset the clock: if it comes back still
            # disconnected, the outage did not pause while we were blind.
            continue
        payload = result.payload if isinstance(result.payload, dict) else {}
        # normalize_mode, not the raw field: some daemon builds serialise the
        # enum as {"value": "disconnected"}, and a dict never equals a string --
        # the watchdog would take the healthy branch through the whole outage.
        mode = network.normalize_mode(payload.get("mode"))
        if watchdog.observe(mode, now=time.monotonic()):
            backend = await asyncio.to_thread(host_backend.current)
            log.warning("wifi has been disconnected for %.0fs; asking the %s backend "
                        "to raise the hotspot", watchdog.grace_s, backend)
            await asyncio.to_thread(network.start_hotspot)


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------
class LogIdOnFailure:
    """ASGI middleware: a failed ``/api/`` answer gets a log id, in a header and the journal.

    Pure ASGI rather than ``@app.middleware("http")``: it touches only the response
    start, so a streamed body is passed through untouched. ``401`` is left alone (a
    signed-out phone polls; that is not a failure anyone needs to find). The line names
    the method, the PATH (never the query string) and the status -- no body, which is
    where this service's secrets travel.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_id(message) -> None:
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 200))
                if status >= 400 and status != 401:
                    log_id = secrets.token_hex(4)
                    message = dict(message)
                    message["headers"] = list(message.get("headers") or []) + [
                        (LOG_ID_HEADER.lower().encode("latin-1"), log_id.encode("latin-1"))]
                    log.warning("admin: %s %s answered %d (log id %s)", scope.get("method"),
                                scope.get("path"), status, log_id)
            await send(message)

        await self.app(scope, receive, send_with_id)


def create_app(*, state: Optional[AdminState] = None, background: bool = True,
               apply_watcher: Optional[site_apply.SiteApplyWatcher] = None):
    """Build the FastAPI app.

    ``state`` and ``background`` exist for tests (and for a dev box): the
    lifespan workers all talk to hardware that is not there, and a test suite
    must not spend a grace period waiting for a Wi-Fi watchdog. ``apply_watcher``
    replaces the site auto-apply watcher (a test drives its own).
    """

    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    admin_state = state if state is not None else AdminState()
    addon = load_addon()
    pin_settings = PIN_SETTINGS | frozenset(
        key for key in (getattr(addon, "pin_settings", None) or ()) if isinstance(key, str))
    auto_reasons = site_apply.AUTO_APPLY_REASONS | frozenset(
        r for r in (getattr(addon, "auto_apply_reasons", None) or ()) if isinstance(r, str))
    # Which deploy THIS process is: the marker as it was when it started (main() builds
    # the app once). /healthz/deep reports it; readycheck holds it to the marker on disk.
    started_commit = system_mod.deployed_marker()
    limiter = auth.LoginLimiter()
    readycheck = system_mod.ReadycheckRunner()
    overview_cache = probes.TtlCache(OVERVIEW_TTL_S)
    remote_cache = probes.TtlCache(REMOTE_TTL_S)
    setup_cache = probes.TtlCache(SETUP_TTL_S)
    # Restarts the hostapp, unattended, for Pokamind-derived reasons once the robot has
    # been idle long enough (site_apply). Built here, started in the lifespan: the
    # dashboard-password reset below hands it a reason of its own.
    watcher = apply_watcher if apply_watcher is not None else site_apply.SiteApplyWatcher(
        fetch=lambda method, path: _hostapp_admin(method, path,
                                                  timeout_s=POKAMIND_PROXY_TIMEOUT_S),
        restart=lambda: system_mod.service_action(system_mod.host_unit(), "restart"),
        auto_reasons=auto_reasons)

    # ---------------------------------------------------------------- auth
    def _client(request) -> str:
        client = getattr(request, "client", None)
        return str(getattr(client, "host", "") or "unknown")

    def _client_code(client: str) -> str:
        """The login-audit identifier that goes in the JOURNAL, never the IP.

        Same reasoning (and same shape) as the dashboard's ``_client_code``: an
        address is personal data, the journal is world-readable to anyone
        holding the robot and survives reboots, and what auditing actually asks
        is "is ONE source hammering us" -- which a salted HMAC answers, because
        repeated attempts still group and the rate-limit line still fingerprints
        to the same value as the failure line.  The limiter itself keeps keying
        on the raw address; only the log line is pseudonymised.
        """
        return hmac.new(auth.admin_salt().encode("utf-8"),
                        (client or "").encode("utf-8"),
                        hashlib.sha256).hexdigest()[:8]

    def _authed(request) -> bool:
        pin, problem = auth.configured_pin()
        if problem:
            return False
        return auth.cookie_valid(request.cookies.get(auth.COOKIE_NAME, ""),
                                 pin, auth.admin_salt())

    def _guard(request):
        """``None`` = allowed; otherwise the response to return immediately."""

        if not origin_ok(request):
            # Before anything else, including the PIN check: a cross-origin
            # request is refused whatever its cookie says (see origin_ok).
            log.warning("admin: refused a cross-origin request from %r",
                        request.headers.get("origin") or request.headers.get("referer"))
            return JSONResponse({"ok": False, "error": "bad_origin"}, status_code=403)
        _, problem = auth.configured_pin()
        if problem:
            return JSONResponse({"ok": False, "error": problem}, status_code=503)
        if not _authed(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return None

    def _no_store(response):
        # Nothing this service serves may sit in a phone's back/forward cache:
        # a stale Home tab after a reboot is exactly the wrong information.
        response.headers["Cache-Control"] = "no-store"
        return response

    def _as_response(body):
        return body if not isinstance(body, dict) else JSONResponse(body)

    # ---------------------------------------------------------------- lifespan
    @asynccontextmanager
    async def lifespan(_app):
        tasks = []
        if background:
            tasks.append(asyncio.create_task(boot_audio_task(admin_state),
                                             name="admin-boot-audio"))
            rebrand_done = asyncio.Event()
            tasks.append(asyncio.create_task(ap_rebrand_task(rebrand_done),
                                             name="admin-ap-rebrand"))
            # v2's boot fallback; returns at once on a daemon robot.
            tasks.append(asyncio.create_task(host_wifi_boot_task(rebrand_done),
                                             name="admin-host-wifi-boot"))
            if network.watchdog_enabled():
                ready, reason = network.watchdog_ap_ready()
                if ready:
                    tasks.append(asyncio.create_task(wifi_watchdog_task(),
                                                     name="admin-wifi-watchdog"))
                else:
                    # Loud, and it names the fix: an operator who set the
                    # watchdog flag expects auto-recovery, and a silently
                    # unarmed watchdog is worse than a disabled one.
                    log.warning(
                        "wifi watchdog NOT armed (%s): it would raise an access "
                        "point using the vendor's published default credentials "
                        "(%s / the vendor psk), which is an open door for anyone "
                        "in radio range. Set EMORI_AP_SSID and an >=%d character "
                        "EMORI_AP_PASSWORD in /opt/emori/.env and restart "
                        "emori-admin to arm it.",
                        reason, network.DAEMON_AP_SSID, network.MIN_PSK_LEN)
            # Derived settings (a Pokamind scope change, a dashboard password reset, an
            # add-on's own) restart the hostapp only after
            # the robot has been idle for a while; returns at once when switched off.
            tasks.append(asyncio.create_task(watcher.run(), name="admin-site-apply"))
        try:
            yield
        finally:
            # Bounded teardown: cancel first, then collect. A worker that is
            # mid-``to_thread`` must not keep uvicorn's shutdown waiting.
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # No /docs, /redoc or /openapi.json: FastAPI serves all three WITHOUT the
    # cookie, and on an open AP that is a free, unauthenticated map of every
    # route that can reboot a robot. The routes are documented in docs/robot-admin.md.
    app = FastAPI(title="EMORI admin", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LogIdOnFailure)

    # ---------------------------------------------------------------- pages
    @app.get("/healthz")
    def healthz():
        # Unauthenticated on purpose: readycheck's gate_admin probes it, and a
        # liveness check that needs a secret is a liveness check nobody runs.
        # It reveals the service's own name and whether a usable PIN is set (a robot
        # with none answers 503 on every page, which anyone can see anyway).
        _, problem = auth.configured_pin()
        return {"status": "ok", "service": "emori-admin", "pin_configured": not problem}

    @app.get("/healthz/deep")
    def healthz_deep(request: Request):
        """Can Wi-Fi status and the speaker level be read right now?  readycheck asks.

        Through the same backend selection the phone's tabs use (see
        :mod:`.backend_check`). No PIN -- readycheck has none -- but the shared
        ``EMORI_ADMIN_TOKEN`` (the hostapp's own credential towards this service),
        so a stranger on the AP cannot make the robot run nmcli/amixer on demand.
        The answer is booleans and slugs only.
        """

        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "bad_origin"}, status_code=403)
        expected = admin_token()
        if not expected:
            return JSONResponse({"ok": False, "error": "admin_token_unset"},
                                status_code=503)
        supplied = str(request.headers.get(TOKEN_HEADER, "") or "")
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse({"ok": False, "error": "token_rejected"}, status_code=403)
        return _no_store(JSONResponse(backend_check.run(commit=started_commit)))

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        _, problem = auth.configured_pin()
        if problem:
            return _no_store(HTMLResponse(
                _render(_NOT_CONFIGURED_HTML, error=(
                    "No admin PIN is set on this robot."
                    if problem == auth.NOT_CONFIGURED else
                    "The configured admin PIN is not exactly six digits, so the "
                    "surface stays closed.")),
                status_code=503))
        if not _authed(request):
            return _no_store(HTMLResponse(
                login_html(request.query_params.get("e", ""))))
        return _no_store(HTMLResponse(app_html(addon)))

    @app.post("/api/login")
    async def api_login(request: Request):
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "bad_origin"}, status_code=403)
        pin, problem = auth.configured_pin()
        if problem:
            return JSONResponse({"ok": False, "error": problem}, status_code=503)
        client = _client(request)
        if limiter.blocked(client):
            log.warning("admin login rate-limited for client=%s", _client_code(client))
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        supplied = await _body_pin(request)
        # Format first, then a constant-time compare. The format is public (the
        # login box says "6-digit PIN"), so checking it early leaks nothing --
        # but it still counts as a failure, or a malformed-body flood would be a
        # free way to keep the limiter's window empty.
        if auth.PIN_RE.fullmatch(supplied) is None or not auth.constant_time_equal(supplied, pin):
            limiter.record_failure(client)
            log.warning("admin login failed for client=%s", _client_code(client))
            return JSONResponse({"ok": False, "error": "bad_pin"}, status_code=401)
        limiter.clear(client)
        response = Response(status_code=204)
        response.set_cookie(
            auth.COOKIE_NAME, auth.make_cookie(pin, auth.admin_salt()),
            max_age=auth.COOKIE_TTL_S, httponly=True, samesite="lax", path="/",
            # Not `secure`: this is plain HTTP on a site LAN or on the robot's
            # own AP (there is no certificate for 10.42.0.1), and a secure
            # cookie would simply never come back.
        )
        return response

    @app.post("/api/logout")
    def api_logout(request: Request):
        # Guarded on the origin (but not on the cookie): logging somebody out
        # from a page they did not open is a nuisance a stranger should not be
        # able to cause, while a logout with no session must still succeed.
        if not origin_ok(request):
            return JSONResponse({"ok": False, "error": "bad_origin"}, status_code=403)
        response = Response(status_code=204)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    # ---------------------------------------------------------------- overview
    def _lang_now() -> Optional[str]:
        if not admin_token():
            return None
        result = _hostapp_admin("GET", "/api/admin/summary")
        payload = result.payload if isinstance(result.payload, dict) else {}
        value = payload.get("lang") if result.ok else None
        return str(value) if value in LANGS else None

    def _pokamind_now() -> Optional[dict]:
        """The Home tab's Pokamind-token dot: the hostapp's status, reduced.

        Asked of the hostapp (which holds the token) and never measured here; the
        status route reads the CACHED whoami, so this costs Pokamind nothing.
        """

        if not admin_token():
            return None
        result = _hostapp_admin("GET", "/api/admin/pokamind")
        payload = result.payload if isinstance(result.payload, dict) else None
        if not result.ok or payload is None:
            return None
        last = payload.get("last_whoami") if isinstance(payload.get("last_whoami"), dict) \
            else None
        return {
            "credential": payload.get("credential"),
            "robot_id": payload.get("robot_id"),
            "mismatch": payload.get("mismatch") is True,
            "restart_pending": payload.get("restart_pending") is True,
            "verified": (last.get("ok") is True) if last is not None else None,
        }

    def _addon_overview() -> Optional[dict]:
        reader = getattr(addon, "overview", None)
        value = reader() if callable(reader) else None
        return value if isinstance(value, dict) else None

    def _build_overview() -> dict:
        # Fan the sub-probes out: serially this is daemon + hostapp + pokamind +
        # systemctl + nmcli, i.e. five timeouts deep, and a single dead peer
        # would make the Home tab take longer than its own poll interval.
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="admin-ov") as pool:
            jobs = {
                "network": pool.submit(_safe, lambda: network.status(with_internet=False), {}),
                "internet": pool.submit(
                    _safe, lambda: remote_cache.get("internet", probes.internet_ok), None),
                "services": pool.submit(_safe, system_mod.services, {}),
                "system": pool.submit(_safe, system_mod.info, {}),
                "healthz": pool.submit(_safe, probes.hostapp_healthz, None),
                "daemon": pool.submit(_safe, probes.daemon_ok, False),
                "api_ok": pool.submit(
                    _safe, lambda: remote_cache.get("pokamind", probes.pokamind_api_ok),
                    False),
                "addon": pool.submit(_safe, _addon_overview, None),
                "lang": pool.submit(_safe, _lang_now, None),
                "token": pool.submit(_safe, _pokamind_now, None),
                "speaker": pool.submit(_safe, audio_mod.speaker_get, None),
                "mic": pool.submit(_safe, audio_mod.mic_get, None),
            }
            got = {key: job.result() for key, job in jobs.items()}
        net = got["network"] or {}
        sysinfo = got["system"] or {}
        healthz_payload = got["healthz"]
        return {
            "network": {"mode": net.get("mode"), "ssid": net.get("ssid"),
                        "ip": net.get("ip"), "signal": net.get("signal"),
                        "internet": bool(got["internet"])},
            "services": (got["services"] or {}).get("services", []),
            # Exactly the six keys the contract names; /api/system has the rest.
            "system": {key: sysinfo.get(key) for key in
                       ("uptime_s", "cpu_pct", "mem_pct", "disk_pct", "temp_c", "commit")},
            "hostapp": {"ok": healthz_payload is not None, "healthz": healthz_payload},
            "daemon": {"ok": bool(got["daemon"])},
            "pokamind": {"api_ok": bool(got["api_ok"]),
                         # None = the hostapp could not say (down, or no admin token).
                         "token": got["token"]},
            "addon": got["addon"],
            "lang": got["lang"],
            "audio": {"speaker": got["speaker"], "mic": got["mic"]},
        }

    @app.get("/api/overview")
    def api_overview(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        data = overview_cache.get("overview", _build_overview)
        if not isinstance(data, dict):
            # TtlCache swallows a producer failure into None; the Home tab must
            # get a shape it can branch on rather than a bare `null`.
            return JSONResponse({"ok": False, "error": "overview_failed"},
                                status_code=500)
        return data

    # ---------------------------------------------------------------- network
    def _envelope(body: Any) -> Any:
        """A network helper's envelope, with its ``ok`` reflected in the STATUS.

        The helpers answer ``{"ok": false, "error": "daemon_down"|"http_409"}``
        when the daemon is dead or busy, and the page's one fetch wrapper
        branches on the HTTP status line — as every other route on this service
        already lets it (``_set_level``, ``_lang_proxy``, services, logs). Left
        at 200 these five reported a REJECTED mutation as a success: the phone
        would say "The robot is bringing up its own network, join <ap> and open
        http://10.42.0.1:8070" and send the operator off the site Wi-Fi after a
        hotspot request the daemon never accepted.
        """

        if isinstance(body, dict) and body.get("ok") is False:
            return JSONResponse(body, status_code=502)
        return body

    @app.get("/api/network/status")
    def api_network_status(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        body = network.status(with_internet=False)
        # The connectivity answer comes from the same 30 s cache the overview
        # uses: the Network tab polls this route hard during a join, and the
        # fallback leg of that probe is a real TCP handshake.
        body["internet"] = bool(remote_cache.get("internet", probes.internet_ok))
        return _envelope(body)

    @app.post("/api/network/scan")
    def api_network_scan(request: Request):
        denied = _guard(request)
        return denied if denied is not None else _envelope(network.scan())

    @app.post("/api/network/connect")
    def api_network_connect(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload or {}
        ssid, password = body.get("ssid"), body.get("password", "")
        if not network.valid_ssid(ssid):
            return JSONResponse({"ok": False, "error": "bad_ssid"}, status_code=400)
        if not network.valid_password(password):
            return JSONResponse({"ok": False, "error": "bad_password"}, status_code=400)
        # NOTE: the daemon answers immediately and connects in a thread. The UI
        # must poll /api/network/status until `mode` leaves "busy" and then read
        # /api/network/error -- a 200 here means "accepted", not "connected".
        log.warning("admin: joining wifi network (ssid length %d)", len(ssid))
        return _envelope(network.connect(ssid, password or ""))

    @app.post("/api/network/forget")
    def api_network_forget(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        ssid = (payload or {}).get("ssid")
        if not network.valid_ssid(ssid):
            return JSONResponse({"ok": False, "error": "bad_ssid"}, status_code=400)
        return _envelope(network.forget(ssid))

    @app.post("/api/network/hotspot")
    def api_network_hotspot(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        log.warning("admin: entering AP mode on request")
        return _envelope(network.start_hotspot())

    @app.get("/api/network/error")
    def api_network_error(request: Request):
        denied = _guard(request)
        return denied if denied is not None else _envelope(network.wifi_error())

    # ---------------------------------------------------------------- audio
    @app.get("/api/audio")
    def api_audio(request: Request):
        denied = _guard(request)
        return denied if denied is not None else audio_mod.snapshot(admin_state)

    def _set_level(payload: Optional[dict], setter, field: str) -> Any:
        volume = audio_mod.clamp_percent((payload or {}).get("volume"))
        if volume is None:
            return JSONResponse({"ok": False, "error": "bad_volume"}, status_code=400)
        result = setter(volume)
        if not result.ok:
            return JSONResponse(host_backend.failure(result), status_code=502)
        # Persist only what the hardware actually accepted, so a reboot cannot
        # restore a level the daemon refused.
        try:
            admin_state.update(**{field: volume})
        except (ValueError, OSError) as e:
            log.warning("admin state write failed for %s: %s", field, type(e).__name__)
            return JSONResponse({"ok": False, "error": "state_write_failed",
                                 "volume": volume}, status_code=500)
        return {"ok": True, "volume": volume}

    @app.post("/api/audio/speaker")
    def api_audio_speaker(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        return _set_level(payload, audio_mod.speaker_set, "speaker_volume")

    @app.post("/api/audio/mic")
    def api_audio_mic(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        return _set_level(payload, audio_mod.mic_set, "mic_volume")

    @app.post("/api/audio/test-sound")
    def api_audio_test_sound(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        result = audio_mod.test_sound()
        if not result.ok:
            return JSONResponse(host_backend.failure(result), status_code=502)
        return {"ok": True}

    @app.post("/api/audio/mic-profiles")
    def api_audio_mic_profiles(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload or {}
        fields = {}
        for key, field in (("session", "mic_session_mult"), ("idle", "mic_idle_mult")):
            if key not in body:
                continue
            value = body[key]
            if value is None:
                fields[field] = None  # explicit null = drop the override
                continue
            clamped = audio_mod.clamp_mult(value)
            if clamped is None:
                return JSONResponse({"ok": False, "error": "bad_%s" % key},
                                    status_code=400)
            fields[field] = clamped
        if not fields:
            return JSONResponse({"ok": False, "error": "nothing_to_set"},
                                status_code=400)
        try:
            admin_state.update(**fields)
        except (ValueError, OSError) as e:
            log.warning("admin state write failed for mic profiles: %s", type(e).__name__)
            return JSONResponse({"ok": False, "error": "state_write_failed"},
                                status_code=500)
        # Then make it audible now instead of at the next profile switch.
        return {"ok": True, "persisted": admin_state.get(),
                "immediate": audio_mod.apply_mult_now(admin_state)}

    # ---------------------------------------------------------------- language
    def _lang_proxy(result: probes.Http):
        """Shared mapping of the hostapp's answer onto ours."""

        if not result.reached:
            return JSONResponse({"ok": False, "error": "hostapp_down"}, status_code=502)
        if result.status == 409:
            # The hostapp refuses a language switch mid-session (the kiosk page
            # reloads and the robot speaks a confirmation); that is a real state
            # the operator must see, not an error to smooth over.
            return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
        if result.status == 503:
            return JSONResponse({"ok": False, "error": "hostapp_admin_disabled"},
                                status_code=503)
        if result.status in (401, 403):
            return JSONResponse({"ok": False, "error": "token_rejected"}, status_code=502)
        payload = result.payload if isinstance(result.payload, dict) else {}
        if result.status == 400 and isinstance(payload.get("error"), str):
            # The hostapp's own refusal, e.g. unsupported_lang: a language this robot's
            # Pokamind scope does not offer. The phone has words for it.
            return JSONResponse({"ok": False, "error": payload["error"]}, status_code=400)
        if not result.ok:
            return JSONResponse({"ok": False, "error": result.error}, status_code=502)
        return {"ok": True, **payload}

    @app.get("/api/lang")
    def api_lang(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        if not admin_token():
            return JSONResponse({"ok": False, "error": "admin_token_unset"},
                                status_code=503)
        return _lang_proxy(_hostapp_admin("GET", "/api/admin/summary"))

    @app.post("/api/lang")
    def api_set_lang(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        lang = (payload or {}).get("lang")
        if lang not in LANGS:
            return JSONResponse({"ok": False, "error": "bad_lang"}, status_code=400)
        if not admin_token():
            return JSONResponse({"ok": False, "error": "admin_token_unset"},
                                status_code=503)
        # A pure proxy: `lang_state.set()` alone is NOT a language switch (the
        # kiosk must reload and the robot must say so), and only the hostapp
        # holds those objects.
        return _lang_proxy(_hostapp_admin("POST", "/api/admin/lang",
                                          json_body={"lang": lang}))

    # ---------------------------------------------------------------- pokamind
    # THIS robot's own Pokamind token. Every route is a proxy to the hostapp, which
    # holds the token: this process never stores it, never logs it, and only ever has
    # it in memory for the length of one request (a paste on its way in, a PIN-gated
    # reveal on its way out).
    def _hostapp_proxy(result: probes.Http):
        if not result.reached:
            return JSONResponse({"ok": False, "error": "hostapp_down"}, status_code=502)
        if result.status == 503:
            payload = result.payload if isinstance(result.payload, dict) else {}
            error = payload.get("error")
            if isinstance(error, str) and error and error != "admin_not_configured":
                # A route's own "cannot right now" (speech_unavailable, scope_unavailable),
                # not the hostapp failing closed on the shared token.
                return JSONResponse({"ok": False, **payload}, status_code=503)
            return JSONResponse({"ok": False, "error": "hostapp_admin_disabled"},
                                status_code=503)
        if result.status in (401, 403):
            return JSONResponse({"ok": False, "error": "token_rejected"}, status_code=502)
        payload = result.payload if isinstance(result.payload, dict) else {}
        if 400 <= result.status < 500:
            # The hostapp's own refusals (token_invalid + its code, busy,
            # token_not_verified + the whoami code, no_robot_token) are the message.
            return JSONResponse({"ok": False, **payload}, status_code=result.status)
        if not result.ok:
            return JSONResponse({"ok": False, "error": payload.get("error") or result.error},
                                status_code=502)
        return {"ok": True, **payload}

    def _hostapp_ready(request):
        denied = _guard(request)
        if denied is not None:
            return denied
        if not admin_token():
            return JSONResponse({"ok": False, "error": "admin_token_unset"},
                                status_code=503)
        return None

    @app.get("/api/pokamind")
    def api_pokamind(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _hostapp_proxy(_hostapp_admin("GET", "/api/admin/pokamind"))

    @app.post("/api/pokamind/verify")
    def api_pokamind_verify(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        response = _hostapp_proxy(_hostapp_admin(
            "POST", "/api/admin/pokamind/verify", timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            # The live answer may differ from what the Home dot last showed.
            overview_cache.clear()
            setup_cache.clear()
        return response

    @app.post("/api/pokamind/token")
    def api_pokamind_token(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        token = (payload or {}).get("token")
        if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_CHARS:
            return JSONResponse({"ok": False, "error": "token_invalid",
                                 "code": "token_malformed"}, status_code=400)
        # The PIN again, like POKAMIND_BASE: the token is this
        # robot's root credential, and the record it belongs to configures the whole
        # robot (course, languages, menus, its add-on). A phone left signed in must
        # not be enough to hand the robot to another Pokamind record.
        refused = _pin_gate(request, (payload or {}).get("pin"), "token replace")
        if refused is not None:
            return refused
        # The hostapp parses it, verifies it LIVE with Pokamind, refuses while a
        # visitor is mid-session and writes it atomically -- or says why not.
        response = _hostapp_proxy(_hostapp_admin(
            "POST", "/api/admin/pokamind/token", json_body={"token": token.strip()},
            timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        del token  # nothing below needs it, and nothing below may log it
        if not isinstance(response, dict) or response.get("saved") is not True:
            return response
        # The hostapp binds its token at start, so the new one is in force only after
        # a restart -- the same allowlisted action as Services > Host app > Restart
        # (and the kiosk follows it: PartOf=).
        log.warning("admin: pokamind token replaced for %s; restarting %s",
                    response.get("robot_id"), system_mod.host_unit())
        outcome = system_mod.service_action(system_mod.host_unit(), "restart")
        overview_cache.clear()
        setup_cache.clear()
        response["restarted"] = bool(outcome.get("ok"))
        if not outcome.get("ok"):
            response["restart_error"] = outcome.get("error") or "restart_failed"
        return response

    @app.post("/api/pokamind/reveal")
    async def api_pokamind_reveal(request: Request):
        """The full token, once the operator has typed the PIN AGAIN.

        The session cookie alone is not enough: a phone left unlocked on a
        counter is signed in for 12 hours, and this is the one value on the robot
        that lets somebody else log learners into Pokamind as this robot. Failures
        count against the same limiter as the login itself, and a wrong PIN is a 403
        (not a 401, which the page treats as "signed out").
        """

        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        pin, _ = auth.configured_pin()
        client = _client(request)
        if limiter.blocked(client):
            log.warning("admin token reveal rate-limited for client=%s", _client_code(client))
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        supplied = await _body_pin(request)
        if auth.PIN_RE.fullmatch(supplied) is None or not auth.constant_time_equal(supplied, pin):
            limiter.record_failure(client)
            log.warning("admin token reveal: wrong PIN from client=%s", _client_code(client))
            return JSONResponse({"ok": False, "error": "bad_pin"}, status_code=403)
        limiter.clear(client)
        result = await asyncio.to_thread(_hostapp_admin, "POST", "/api/admin/pokamind/reveal")
        response = _hostapp_proxy(result)
        if not isinstance(response, dict) or not response.get("token"):
            return response
        # The EVENT, never the value: who asked is the auditable part.
        log.warning("admin: pokamind token for %s REVEALED on a phone (client=%s)",
                    response.get("robot_id"), _client_code(client))
        response["visible_s"] = REVEAL_VISIBLE_S
        return _no_store(JSONResponse(response))

    # ---------------------------------------------------------------- people
    # The badge roster (People tab): names, e-mail addresses and the QR badges the
    # robot's camera reads. Same shape as the Pokamind tab and for the same reason:
    # the hostapp holds the roster (and serves it to the badge scanner), so every
    # route here is a proxy to its loopback-only /api/admin/roster*, behind this
    # service's PIN. Nothing here keeps, caches or logs a name or an address.
    def _people_fields(payload: Optional[dict], *names: str,
                       optional: tuple[str, ...] = ()) -> Optional[dict]:
        """Only the expected string fields, bounded, or None (a 400 before forwarding).

        An ``optional`` field is forwarded only when the request carries it, as text or
        null (the person's pathway: absent keeps it, null or "" removes it).
        """

        body = payload if isinstance(payload, dict) else {}
        out = {}
        for name in names:
            value = body.get(name, "")
            if not isinstance(value, str) or len(value) > PEOPLE_FIELD_MAX:
                return None
            out[name] = value
        for name in optional:
            if name not in body:
                continue
            value = body[name]
            if value is not None and (not isinstance(value, str)
                                      or len(value) > PEOPLE_FIELD_MAX):
                return None
            out[name] = value
        return out

    def _people_write(path: str, payload: Optional[dict], *names: str,
                      optional: tuple[str, ...] = ()):
        fields = _people_fields(payload, *names, optional=optional)
        if fields is None:
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        response = _hostapp_proxy(_hostapp_admin(
            "POST", path, json_body=fields, timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            # A whoami the hostapp may just have asked changes the Home tab's token dot.
            overview_cache.clear()
            setup_cache.clear()
        return response

    def _people_file(result: probes.Http, media_type: str, *, filename: str = ""):
        """A badge/sheet answer passed through as-is, or the hostapp's refusal as JSON."""

        if not (result.ok and result.content
                and str(result.content_type or "").startswith(media_type.split(";")[0])):
            return _hostapp_proxy(result) if not result.ok else JSONResponse(
                {"ok": False, "error": "bad_answer"}, status_code=502)
        response = Response(content=result.content, media_type=media_type)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Names are escaped where they are drawn; this is the second lock on the door:
        # nothing in a badge or a sheet may load, post or frame anything.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if filename:
            response.headers["Content-Disposition"] = 'attachment; filename="%s"' % filename
        return _no_store(response)

    @app.get("/api/people")
    def api_people(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _no_store(_as_response(_hostapp_proxy(_hostapp_admin(
            "GET", "/api/admin/roster", timeout_s=POKAMIND_PROXY_TIMEOUT_S))))

    @app.post("/api/people/add")
    def api_people_add(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _people_write("/api/admin/roster/add", payload, "name", "email",
                             optional=("pathway",))

    @app.post("/api/people/update")
    def api_people_update(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _people_write("/api/admin/roster/update", payload, "ref", "name", "email",
                             optional=("pathway",))

    @app.post("/api/people/remove")
    def api_people_remove(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _people_write("/api/admin/roster/remove", payload, "ref")

    @app.get("/api/people/badge")
    def api_people_badge(request: Request):
        """One badge: ``?ref=<ref>&format=svg|png[&download=1]``.

        SVG is the whole badge (code, name, site line); PNG is the code alone. A GET so
        the page can show it in an ``<img>`` and offer it as a plain download link.
        """

        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        ref = request.query_params.get("ref", "")
        fmt = request.query_params.get("format", "svg")
        if not PEOPLE_REF_RE.fullmatch(ref) or fmt not in ("svg", "png"):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        result = _hostapp_admin("POST", "/api/admin/roster/badge",
                                json_body={"ref": ref, "format": fmt},
                                timeout_s=POKAMIND_PROXY_TIMEOUT_S)
        download = request.query_params.get("download") == "1"
        return _people_file(result, "image/svg+xml" if fmt == "svg" else "image/png",
                            filename=("emori-badge-%s.%s" % (ref, fmt)) if download else "")

    @app.get("/api/people/sheet")
    def api_people_sheet(request: Request):
        """The A4 print view: every badge, or only ``?ref=..&ref=..``."""

        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        refs = request.query_params.getlist("ref")
        if len(refs) > PEOPLE_MAX_REFS or not all(PEOPLE_REF_RE.fullmatch(r) for r in refs):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        result = _hostapp_admin("POST", "/api/admin/roster/sheet",
                                json_body={"refs": refs} if refs else {},
                                timeout_s=POKAMIND_PROXY_TIMEOUT_S)
        return _people_file(result, "text/html; charset=utf-8")

    # ---------------------------------------------------------------- PIN again
    def _pin_gate(request, supplied: Any, event: str):
        """``None`` when ``supplied`` is the admin PIN; else the 403/429 to answer.

        For the actions a signed-in phone must confirm by typing the PIN AGAIN (a phone
        left unlocked on a counter is signed in for 12 hours). Same limiter as the login
        and the token reveal, so these are no side door for guessing; a wrong PIN is a
        403, never a 401 (which the page reads as "signed out"). Logs the event name and
        the pseudonymous client, never what was typed.
        """

        pin, _ = auth.configured_pin()
        client = _client(request)
        if limiter.blocked(client):
            log.warning("admin %s rate-limited for client=%s", event, _client_code(client))
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        text = supplied if isinstance(supplied, str) else ""
        if auth.PIN_RE.fullmatch(text) is None or not auth.constant_time_equal(text, pin):
            limiter.record_failure(client)
            log.warning("admin %s: wrong PIN from client=%s", event, _client_code(client))
            return JSONResponse({"ok": False, "error": "bad_pin"}, status_code=403)
        limiter.clear(client)
        return None

    def _restart_self() -> None:
        # service_action defers a self-restart by a moment on a thread of its own, so
        # this only ever runs once the answer is already on its way to the phone.
        outcome = system_mod.service_action(system_mod.SELF_UNIT, "restart")
        if not outcome.get("ok"):
            log.warning("admin: self-restart refused (%s)", outcome.get("error"))

    def _secret_answer(body: dict, *, restart_self: bool = False):
        """A no-store JSON answer; optionally restart this service once it is sent."""

        from starlette.background import BackgroundTask

        response = JSONResponse(body, background=BackgroundTask(_restart_self)
                                if restart_self else None)
        return _no_store(response)

    # ---------------------------------------------------------------- site settings
    # The robot-local settings layer (src/emori_adapter/site_settings.py). The hostapp
    # owns its keys and the files they live in (GET/POST /api/admin/site, loopback +
    # token); this service owns secrets/admin.env (PIN, hotspot, dashboard password)
    # and never forwards one of those keys. No value of a secret ever crosses here.
    @app.get("/api/settings")
    def api_settings(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        response = _hostapp_proxy(_hostapp_admin(
            "GET", "/api/admin/site", timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            response["keys"] = merge_admin_rows(response.get("keys"))
            response["auto_apply"] = watcher.status()
        return _no_store(_as_response(response))

    @app.post("/api/settings")
    def api_settings_save(request: Request, payload: Optional[dict] = None):
        """``{KEY: value | null, ..., "pin": "123456"}``: save hostapp settings.

        The PIN is needed (and checked here, not forwarded) when the body touches one
        of :data:`PIN_SETTINGS`. A key emori-admin owns is refused: those have their own
        routes below.
        """

        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        from emori_adapter import site_settings  # noqa: PLC0415

        body = payload if isinstance(payload, dict) else {}
        values = {key: value for key, value in body.items() if key != "pin"}
        if not values or len(values) > SETTINGS_MAX_KEYS:
            return JSONResponse({"ok": False, "error": "invalid_body"}, status_code=400)
        for key, value in values.items():
            named = key if isinstance(key, str) and SETTINGS_KEY_RE.fullmatch(key) else None
            spec = site_settings.REGISTRY.get(named) if named else None
            if spec is None or spec.owner != "hostapp":
                return JSONResponse({"ok": False, "error": "key_not_editable", "key": named,
                                     "code": "key_unknown" if spec is None
                                     else "key_wrong_owner"}, status_code=400)
            if value is not None and (not isinstance(value, str)
                                      or len(value) > SETTINGS_VALUE_MAX):
                return JSONResponse({"ok": False, "error": "invalid_value", "key": named,
                                     "code": "value_not_text"}, status_code=400)
        gated = sorted(pin_settings & set(values))
        if gated:
            refused = _pin_gate(request, body.get("pin"), "settings change")
            if refused is not None:
                return refused
        response = _hostapp_proxy(_hostapp_admin(
            "POST", "/api/admin/site", json_body=values, timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            setup_cache.clear()
            saved = response.get("saved") if isinstance(response.get("saved"), list) else []
            log.warning("admin: site settings saved from a phone: %s%s",
                        " ".join(k for k in saved if isinstance(k, str)) or "-",
                        " (PIN typed again)" if gated else "")
        return _no_store(_as_response(response))

    def _hostapp_idle() -> tuple[Optional[bool], Any]:
        """``(idle, refusal)`` from GET /api/admin/summary's ``idle``: the hostapp's own
        fail-closed answer (no session, no QR training lease, no add-on mode, nothing the
        add-on is in the middle of).

        ``idle`` is None (with the response to answer) when the hostapp cannot say:
        an unanswered question is never read as "idle".
        """

        result = _hostapp_admin("GET", "/api/admin/summary")
        payload = result.payload if isinstance(result.payload, dict) else None
        if not result.ok or payload is None:
            refusal = _hostapp_proxy(result)
            if isinstance(refusal, dict):  # a 2xx with no usable body
                refusal = JSONResponse({"ok": False, "error": "hostapp_down"},
                                       status_code=502)
            return None, refusal
        return payload.get("idle") is True, None

    @app.post("/api/settings/apply")
    def api_settings_apply(request: Request):
        """Restart the hostapp now, so saved and derived settings take effect.

        Only when the robot is idle (no session, no add-on mode): a restart blanks
        the kiosk for a few seconds and would end a visitor's session.
        """

        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        idle, refusal = _hostapp_idle()
        if idle is None:
            return refusal
        if not idle:
            return JSONResponse({"ok": False, "error": "busy"}, status_code=409)
        log.warning("admin: applying site settings: restarting %s", system_mod.host_unit())
        outcome = system_mod.service_action(system_mod.host_unit(), "restart")
        overview_cache.clear()
        setup_cache.clear()
        if not outcome.get("ok"):
            return JSONResponse({"ok": False, "error": "restart_failed",
                                 "detail": outcome.get("error") or "systemctl_failed"},
                                status_code=502)
        return {"ok": True, "restarted": True}

    @app.post("/api/scope/sync")
    def api_scope_sync(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        response = _hostapp_proxy(_hostapp_admin(
            "POST", "/api/admin/scope/sync", timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            overview_cache.clear()
            setup_cache.clear()
        return _no_store(_as_response(response))

    # ---------------------------------------------------------------- the spoken menu
    def _menu_lang(value: Any, *, required: bool) -> Optional[str]:
        """A supported language code, ``""`` for "the kiosk's" (when allowed), else None."""

        if value in (None, "") and not required:
            return ""
        return value if value in LANGS else None

    def _menu_body(payload: Optional[dict], *, menu: str) -> Optional[dict]:
        """``{lang, menu?}`` for the hostapp, or None (a 400 here). ``menu``: required,
        optional or absent."""

        import json as _json

        body = payload if isinstance(payload, dict) else {}
        lang = _menu_lang(body.get("lang"), required=True)
        if lang is None:
            return None
        out: dict[str, Any] = {"lang": lang}
        if menu != "absent" and ("menu" in body or menu == "required"):
            draft = body.get("menu")
            try:
                size = len(_json.dumps(draft))
            except (TypeError, ValueError):
                return None
            if not isinstance(draft, dict) or size > MENU_BODY_MAX:
                return None
            out["menu"] = draft
        return out

    @app.get("/api/menu")
    def api_menu(request: Request):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        lang = _menu_lang(request.query_params.get("lang", ""), required=False)
        if lang is None:
            return JSONResponse({"ok": False, "error": "bad_lang"}, status_code=400)
        path = "/api/admin/menu" + ("?lang=%s" % lang if lang else "")
        return _no_store(_as_response(_hostapp_proxy(_hostapp_admin(
            "GET", path, timeout_s=POKAMIND_PROXY_TIMEOUT_S))))

    def _menu_write(path: str, payload: Optional[dict], *, menu: str):
        body = _menu_body(payload, menu=menu)
        if body is None:
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        response = _hostapp_proxy(_hostapp_admin(
            "POST", path, json_body=body, timeout_s=POKAMIND_PROXY_TIMEOUT_S))
        if isinstance(response, dict):
            setup_cache.clear()
        return _no_store(_as_response(response))

    @app.post("/api/menu/save")
    def api_menu_save(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _menu_write("/api/admin/menu/save", payload, menu="required")

    @app.post("/api/menu/reset")
    def api_menu_reset(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _menu_write("/api/admin/menu/reset", payload, menu="absent")

    @app.post("/api/menu/preview")
    def api_menu_preview(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        return _menu_write("/api/admin/menu/preview", payload, menu="optional")

    # ---------------------------------------------------------------- the add-on's routes
    kit = AddonKit(
        guard=_guard, hostapp_ready=_hostapp_ready, hostapp=_hostapp_admin,
        hostapp_json=lambda path: _hostapp_json(path), proxy=_hostapp_proxy,
        as_response=_as_response, no_store=_no_store, pin_gate=_pin_gate,
        setup_changed=setup_cache.clear)
    register_addon = getattr(addon, "register", None)
    if callable(register_addon):
        try:
            register_addon(app, kit)
        except Exception:  # noqa: BLE001 - the core service stays up without the add-on's routes
            log.warning("admin: the add-on's routes were not registered", exc_info=True)

    # ---------------------------------------------------------------- results e-mail
    @app.post("/api/results-email/test")
    def api_results_email_test(request: Request, payload: Optional[dict] = None):
        denied = _hostapp_ready(request)
        if denied is not None:
            return denied
        to = (payload or {}).get("to")
        if not isinstance(to, str) or not 0 < len(to.strip()) <= 320:
            return JSONResponse({"ok": False, "sent": False, "error": "recipient_invalid",
                                 "code": "recipient_invalid"}, status_code=400)
        result = _hostapp_admin("POST", "/api/admin/results-email/test",
                                json_body={"to": to.strip()},
                                timeout_s=RESULTS_TEST_TIMEOUT_S)
        payload_in = result.payload if isinstance(result.payload, dict) else {}
        if result.reached and 400 <= result.status < 500 and "error" not in payload_in \
                and isinstance(payload_in.get("code"), str):
            # The hostapp's refusal is {sent: false, code}: the code IS the reason.
            return _no_store(JSONResponse({"ok": False, "error": payload_in["code"],
                                           **payload_in}, status_code=result.status))
        return _no_store(_as_response(_hostapp_proxy(result)))

    # ---------------------------------------------------------------- credentials
    # The values only this robot knows, generated at install: the admin PIN, the
    # recovery hotspot and the dashboard password. Each action needs the PIN typed
    # again; the journal records THAT it happened, never a value; every answer that
    # carries one is no-store. They live in secrets/admin.env (0600), which outranks
    # .env for both services.
    @app.post("/api/pin/change")
    def api_pin_change(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload if isinstance(payload, dict) else {}
        client = _client(request)
        if limiter.blocked(client):
            log.warning("admin PIN change rate-limited for client=%s", _client_code(client))
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        ok, code = auth.change_pin(body.get("current"), body.get("new"))
        if code == "bad_pin":
            limiter.record_failure(client)
            log.warning("admin PIN change: wrong current PIN from client=%s",
                        _client_code(client))
            return JSONResponse({"ok": False, "error": "bad_pin"}, status_code=403)
        if not ok:
            return JSONResponse({"ok": False, "error": code},
                                status_code=500 if code == "pin_save_failed" else 400)
        limiter.clear(client)
        log.warning("admin: PIN changed from a phone (client=%s); emori-admin restarts",
                    _client_code(client))
        response = _secret_answer({"ok": True, "restarting": True}, restart_self=True)
        # This phone stays signed in under the new PIN; every other one signs in again.
        pin, _ = auth.configured_pin()
        response.set_cookie(auth.COOKIE_NAME, auth.make_cookie(pin, auth.admin_salt()),
                            max_age=auth.COOKIE_TTL_S, httponly=True, samesite="lax",
                            path="/")
        return response

    @app.post("/api/credentials/reveal")
    def api_credentials_reveal(request: Request, payload: Optional[dict] = None):
        """``{which: "ap"|"dashboard", pin}``: the hotspot or dashboard credentials, 30 s."""

        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload if isinstance(payload, dict) else {}
        which = body.get("which")
        if which not in ("ap", "dashboard"):
            return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
        refused = _pin_gate(request, body.get("pin"), "%s credential reveal" % which)
        if refused is not None:
            return refused
        if which == "ap":
            ssid, password = network.ap_credentials()
            answer = {"ok": True, "which": which, "ssid": ssid or None, "password": password}
        else:
            password = (os.environ.get("EMORI_DASH_PASSWORD") or "").strip()
            answer = {"ok": True, "which": which, "password": password}
        if not password:
            return JSONResponse({"ok": False, "error": "credential_unset"}, status_code=404)
        log.warning("admin: %s credentials REVEALED on a phone (client=%s)",
                    "hotspot" if which == "ap" else "dashboard",
                    _client_code(_client(request)))
        answer["visible_s"] = REVEAL_VISIBLE_S
        return _secret_answer(answer)

    @app.post("/api/credentials/ap")
    def api_credentials_ap(request: Request, payload: Optional[dict] = None):
        """``{pin, ssid?, password?}``: a new hotspot name and/or password.

        Saved, then emori-admin restarts and rebrands the hotspot at its start -- never
        while the hotspot is the live connection (the phone on it would drop): then it
        is applied at the next start that comes up on the site Wi-Fi.
        """

        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload if isinstance(payload, dict) else {}
        refused = _pin_gate(request, body.get("pin"), "hotspot change")
        if refused is not None:
            return refused
        ssid, password = body.get("ssid"), body.get("password")
        ssid = None if ssid in (None, "") else ssid
        password = None if password in (None, "") else password
        ok, code = network.set_ap_credentials(ssid, password)
        if not ok:
            return JSONResponse({"ok": False, "error": code},
                                status_code=500 if code == "save_failed" else 400)
        return _secret_answer({"ok": True, "restarting": True}, restart_self=True)

    @app.post("/api/credentials/dashboard/reset")
    def api_credentials_dashboard_reset(request: Request, payload: Optional[dict] = None):
        """``{pin}``: a new random dashboard password, shown ONCE in this answer.

        The hostapp reads it at its start, so it is queued for the auto-apply watcher
        (the restart signs every dashboard session out, so it waits for idle).
        """

        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload if isinstance(payload, dict) else {}
        refused = _pin_gate(request, body.get("pin"), "dashboard password reset")
        if refused is not None:
            return refused
        password = secrets.token_urlsafe(DASHBOARD_PASSWORD_BYTES)
        try:
            auth.save_admin_secrets({"EMORI_DASH_PASSWORD": password})
        except (ValueError, OSError) as e:
            log.warning("dashboard password not reset (%s)", type(e).__name__)
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
        os.environ["EMORI_DASH_PASSWORD"] = password
        watcher.queue(site_apply.REASON_DASHBOARD_PASSWORD)
        setup_cache.clear()
        log.warning("admin: dashboard password RESET from a phone (client=%s); the "
                    "hostapp takes it at its next restart", _client_code(_client(request)))
        return _secret_answer({"ok": True, "password": password,
                               "visible_s": REVEAL_VISIBLE_S, "restart_pending": True})

    # ---------------------------------------------------------------- setup checklist
    def _hostapp_json(path: str) -> Optional[dict]:
        if not admin_token():
            return None
        result = _hostapp_admin("GET", path, timeout_s=POKAMIND_PROXY_TIMEOUT_S)
        return result.payload if result.ok and isinstance(result.payload, dict) else None

    def _setup_site() -> Optional[dict]:
        site = _hostapp_json("/api/admin/site")
        if site is None:
            return None
        scope = site.get("scope") if isinstance(site.get("scope"), dict) else {}
        reasons = site.get("restart_reasons")
        return {"status": scope.get("status"), "stale": scope.get("stale") is True,
                "auto": scope.get("auto") is not False,
                "fetched_at": scope.get("fetched_at"),
                "last_sync_code": scope.get("last_sync_code"),
                "restart_pending": site.get("restart_pending") is True,
                "restart_reasons": [r for r in reasons if isinstance(r, str)]
                if isinstance(reasons, list) else []}

    def _setup_menu(lang: str) -> Optional[dict]:
        menu = _hostapp_json("/api/admin/menu?lang=%s" % lang)
        if menu is None:
            return None
        errors = menu.get("errors")
        return {"source": menu.get("source"), "authored": menu.get("authored"),
                "errors": len(errors) if isinstance(errors, list) else 0,
                "saved": menu.get("saved") is True}

    def _setup_people() -> Optional[dict]:
        roster = _hostapp_json("/api/admin/roster")
        people = roster.get("people") if roster is not None else None
        return {"count": len(people)} if isinstance(people, list) else None

    def _setup_addon() -> Optional[dict]:
        reader = getattr(addon, "setup", None)
        value = reader(kit) if callable(reader) else None
        return value if isinstance(value, dict) else None

    def _setup_langs() -> Optional[list]:
        summary = _hostapp_json("/api/admin/summary")
        langs = summary.get("allowed_langs") if summary is not None else None
        return [code for code in langs if code in LANGS] if isinstance(langs, list) else None

    def _setup_readycheck() -> dict:
        status = readycheck.status()
        result = status.get("result") if isinstance(status.get("result"), dict) else None
        counts = result.get("counts") if result is not None else None
        counts = {str(k).lower(): v for k, v in counts.items()} \
            if isinstance(counts, dict) else {}

        def count(name: str) -> int:
            value = counts.get(name)
            return value if isinstance(value, int) else 0

        if result is None and status.get("running") is not True:
            deployed = _deploy_readycheck()
            if deployed is not None:
                return deployed
        return {"running": status.get("running") is True, "ran": result is not None,
                "fail": count("fail"), "warn": count("warn")}

    def _deploy_readycheck() -> Optional[dict]:
        """The readycheck the last deploy ran on this robot (one-click-deploy.sh writes
        artifacts/last-deploy-readycheck.env): counts only, or None when there is none."""

        from emori_adapter import site_settings  # noqa: PLC0415

        try:
            text = site_settings.site_path(DEPLOY_READYCHECK_FILE).read_text(
                encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        fail, warn = values.get("fail", ""), values.get("warn", "")
        if not (fail.isdigit() and warn.isdigit()):
            return None
        return {"running": False, "ran": True, "fail": int(fail), "warn": int(warn),
                "source": "deploy"}

    def _build_setup() -> dict:
        with ThreadPoolExecutor(max_workers=9, thread_name_prefix="admin-setup") as pool:
            jobs = {
                "network": pool.submit(_safe, lambda: network.status(with_internet=False), {}),
                "internet": pool.submit(
                    _safe, lambda: remote_cache.get("internet", probes.internet_ok), None),
                "token": pool.submit(_safe, _pokamind_now, None),
                "site": pool.submit(_safe, _setup_site, None),
                "langs": pool.submit(_safe, _setup_langs, None),
                "people": pool.submit(_safe, _setup_people, None),
                "addon": pool.submit(_safe, _setup_addon, None),
            }
            menus = {lang: pool.submit(_safe, lambda code=lang: _setup_menu(code), None)
                     for lang in LANGS}
            got = {key: job.result() for key, job in jobs.items()}
            menu_got = {lang: job.result() for lang, job in menus.items()}
        net = got["network"] or {}
        return {
            "wifi": {"mode": net.get("mode"), "ssid": net.get("ssid"),
                     "internet": bool(got["internet"])},
            "token": got["token"],
            "scope": got["site"],
            "langs": got["langs"],
            "menus": menu_got,
            "people": got["people"],
            "addon": got["addon"],
            "readycheck": _safe(_setup_readycheck, {"running": False, "ran": False,
                                                    "fail": 0, "warn": 0}),
            "auto_apply": watcher.status(),
        }

    @app.get("/api/setup")
    def api_setup(request: Request):
        """The Home tab's setup checklist, from live state only (nothing on the phone)."""

        denied = _guard(request)
        if denied is not None:
            return denied
        data = setup_cache.get("setup", _build_setup)
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "error": "setup_failed"}, status_code=500)
        return _no_store(JSONResponse(data))

    # ---------------------------------------------------------------- services
    @app.get("/api/services")
    def api_services(request: Request):
        denied = _guard(request)
        # Same reason as the network routes: a systemctl that could not be run
        # is not "no services", which is what an empty list at 200 renders as.
        return denied if denied is not None else _envelope(system_mod.services())

    @app.post("/api/services/action")
    def api_services_action(request: Request, payload: Optional[dict] = None):
        denied = _guard(request)
        if denied is not None:
            return denied
        body = payload or {}
        unit, action = body.get("unit"), body.get("action")
        # Allowlist BEFORE any subprocess exists: `unit` is the only
        # request-supplied value that ever reaches an argv in this service.
        if not system_mod.unit_allowed(unit):
            return JSONResponse({"ok": False, "error": "unit_not_allowed"},
                                status_code=400)
        if action not in system_mod.ALLOWED_ACTIONS:
            return JSONResponse({"ok": False, "error": "action_not_allowed"},
                                status_code=400)
        log.warning("admin: systemctl %s %s", action, unit)
        outcome = system_mod.service_action(unit, action)
        if not outcome.get("ok"):
            status = 400 if outcome.get("error") in (
                "unit_not_allowed", "action_not_allowed", "self_stop_refused") else 502
            return JSONResponse(outcome, status_code=status)
        return outcome

    @app.post("/api/hub/rollback")
    def api_hub_rollback(request: Request):
        """Back to the host app, for a robot whose hub does not come up.

        The phone is the way out for a site with no ssh. Refused unless the hub's flag is
        up and its gateway is silent.
        """

        denied = _guard(request)
        if denied is not None:
            return denied
        log.warning("admin: rolling the gateway back to the host app")
        outcome = system_mod.hub_rollback()
        overview_cache.clear()
        if not outcome.get("ok"):
            status = 409 if outcome.get("error") in ("not_hub_mode", "hub_healthy") else 502
            return JSONResponse(outcome, status_code=status)
        return outcome

    # ---------------------------------------------------------------- readycheck
    @app.post("/api/readycheck/run")
    def api_readycheck_run(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        started = readycheck.start()
        if not started.get("ok"):
            return JSONResponse(started, status_code=409)
        return started

    @app.get("/api/readycheck/status")
    def api_readycheck_status(request: Request):
        denied = _guard(request)
        return denied if denied is not None else readycheck.status()

    # ---------------------------------------------------------------- logs
    @app.get("/api/logs")
    def api_logs(request: Request, unit: str = "", lines: int = system_mod.LOG_LINES_DEFAULT):
        denied = _guard(request)
        if denied is not None:
            return denied
        if not system_mod.unit_allowed(unit):
            return JSONResponse({"ok": False, "error": "unit_not_allowed",
                                 "units": list(system_mod.ALLOWED_UNITS)},
                                status_code=400)
        outcome = system_mod.logs(unit, lines)
        return outcome if outcome.get("ok") else JSONResponse(outcome, status_code=502)

    # ---------------------------------------------------------------- system
    @app.get("/api/system")
    def api_system(request: Request):
        denied = _guard(request)
        return denied if denied is not None else system_mod.info()

    @app.post("/api/system/reboot")
    def api_reboot(request: Request):
        denied = _guard(request)
        return denied if denied is not None else system_mod.power("reboot")

    @app.post("/api/system/poweroff")
    def api_poweroff(request: Request):
        denied = _guard(request)
        return denied if denied is not None else system_mod.power("poweroff")

    # ---------------------------------------------------------------- camera
    @app.get("/api/camera/snapshot")
    def api_camera_snapshot(request: Request):
        """Proxy of the hostapp's ``GET /api/qr-frame`` (server.py:1332).

        That route really does return an ``image/jpeg`` body — but only the LAST
        frame the QR page uploaded, and only while ``EMORI_QR_FRAME_DEBUG`` is
        on (it 403s otherwise, 404s before the first upload). So this is a
        debug aid for "what does the camera see", never a live viewfinder, and
        both refusals are passed through verbatim so the UI can say which one it
        got.
        """

        denied = _guard(request)
        if denied is not None:
            return denied
        result = probes.hostapp("GET", "/api/qr-frame")
        if not result.reached:
            return JSONResponse({"ok": False, "error": "hostapp_down"}, status_code=502)
        if result.status == 403:
            return JSONResponse({"ok": False, "error": "debug_disabled"}, status_code=403)
        if result.status == 404:
            return JSONResponse({"ok": False, "error": "no_frame"}, status_code=404)
        if not result.ok or not result.content:
            return JSONResponse({"ok": False, "error": result.error or "no_frame"},
                                status_code=502)
        return _no_store(Response(content=result.content,
                                  media_type=result.content_type or "image/jpeg"))

    _, problem = auth.configured_pin()
    if problem == auth.NOT_CONFIGURED:
        log.warning("emori-admin is serving 503 on every route: %s is unset - set a "
                    "6-digit PIN in /opt/emori/.env and restart emori-admin",
                    auth.ENV_PIN)
    elif problem == auth.PIN_INVALID:
        log.warning("emori-admin is serving 503 on every route: %s is set but is not "
                    "exactly six digits (a shorter PIN would cut the brute-force "
                    "keyspace this service's rate limits are sized for)", auth.ENV_PIN)
    if not admin_token():
        log.warning("%s is unset: the language switch will report "
                    "hostapp_admin_disabled until install.sh generates one", ENV_TOKEN)
    return app


async def _body_pin(request) -> str:
    """Read ``pin`` from a JSON body (or a urlencoded form).

    Hand-parsed like ``dashboard._form_password``: ``fastapi.Form`` needs
    ``python-multipart``, and the robot venv is deliberately fixed
    (fastapi/uvicorn/pydantic/stdlib only).  The form branch costs five lines
    and makes ``curl -d pin=...`` work from a laptop during a field debug.
    """

    import json as _json

    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    content_type = str(request.headers.get("content-type", "")).lower()
    if "application/json" in content_type or raw.lstrip()[:1] == b"{":
        try:
            payload = _json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            return ""
        return str(payload.get("pin", "")) if isinstance(payload, dict) else ""
    from urllib.parse import parse_qs

    try:
        fields = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    except Exception:  # noqa: BLE001
        return ""
    values = fields.get("pin") or [""]
    return str(values[0])


def main() -> None:
    # Same journal setup and the same knob as hostapp/server.main(): this is a
    # deployed unit too (deploy/robot/emori-admin.service), it had no root
    # handler either, and its whole reason to exist is being readable when
    # everything else is broken. Its own INFO -- the AP-rebrand outcome is the
    # only one today -- reached nothing at all; WARNING and above got through
    # only because Python's lastResort handler emits them.
    #
    # The clamp matters MORE here than in the hostapp, and it is why this is one
    # shared function rather than two ad-hoc setups. This service passes the
    # site's Wi-Fi password to the daemon as a URL parameter
    # (network.connect -> `POST /wifi/connect?ssid=...&password=...`), and httpx
    # logs `request.url` in full at INFO -- so installing a root handler without
    # the clamp would have written a customer's Wi-Fi key into a world-readable
    # journal on the first join, at the DEFAULT level. probes.py already refuses
    # to let exception text out for that exact reason; the clamp closes the other
    # end of the same hole.
    #
    # Audited before making it visible: no PIN, password or PSK is
    # logged anywhere under emori_adapter.admin. `auth` logs the fact of a failed
    # login and its client, never the PIN; `server` logs an SSID's LENGTH on
    # join, never the SSID or the key; `network` names EMORI_AP_SSID (the name it
    # broadcasts, which is public by construction) and only ever the BOUNDS of a
    # bad password, never its value; `probes` answers with a slug derived from
    # the exception TYPE, so an nmcli argv carrying a psk cannot come back as
    # error text.
    #
    # Imported HERE and not at module scope: `create_app()` must stay importable
    # -- and this service must stay startable -- in an environment where
    # `logging_setup`'s own import of rich is missing. This is the recovery tool;
    # nothing it does at import time may be able to stop it running.
    from emori_adapter.logging_setup import access_log_enabled, setup_journal_logging

    import uvicorn

    # .env BEFORE logging, unlike the hostapp -- and the difference is not
    # stylistic. hostapp/server.py imports `emori_adapter.config` at MODULE
    # scope, so its .env is loaded long before main() runs; this service loads it
    # here, so configuring logging first would read EMORI_LOG_LEVEL out of an
    # environment that does not have it yet.
    #
    # And NOT through `emori_adapter.config`, whose import applies the host app's
    # whole settings layer: secrets/site.env (the SMTP password) and the robot's
    # Pokamind token, whose seed it decodes to compare key fingerprints. This
    # service holds no robot credential: load_admin_environment gives it .env
    # without those keys, configs/site/settings.env and its own secrets/admin.env
    # (PIN, hotspot, dashboard password), and derives nothing from Pokamind. The
    # unit no longer hands it .env or secrets/site.env either
    # (deploy/robot/emori-admin.service).
    dotenv_failed = False
    try:
        from emori_adapter import site_settings  # noqa: PLC0415

        site_settings.load_admin_environment()
    except Exception:  # noqa: BLE001 - the process env alone is a valid setup
        dotenv_failed = True

    setup_journal_logging()
    if dotenv_failed:
        # Said after the handler exists, so it is formatted like every other
        # journal line instead of falling to Python's lastResort.
        log.warning("could not load .env; using the process environment as-is")
    host = os.environ.get("EMORI_ADMIN_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    # access_log OFF, for the same two reasons and in that order. Content: this
    # service's secrets travel in JSON BODIES (`POST /api/login` carries the PIN,
    # `POST /api/network/connect` the site password), which uvicorn does not
    # print -- but an access log is a per-request record of who administered this
    # robot and when, and it is exactly the kind of thing that should not
    # accumulate on the box by default. Noise: the phone UI polls
    # `GET /api/overview` every 5 s for as long as it is open, and two phones may
    # be. Same EMORI_LOG_WIRE opt-in, same never-on-a-deployed-robot rule.
    uvicorn.run(create_app(), host=host, port=admin_port(),
                access_log=access_log_enabled())


if __name__ == "__main__":
    main()
