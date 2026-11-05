"""Wi-Fi on a robot without the reachy-mini daemon: the daemon's ``/wifi`` router, over nmcli.

On v2 the ROS 2 node owns ``:8000`` and nothing answers ``/wifi/*`` (see
:mod:`emori_adapter.admin.host_backend`).  This module is the daemon's
``routers/wifi_config.py`` re-done in this process, rule for rule, so that
:mod:`emori_adapter.admin.network` -- and through it the phone page -- cannot tell
the difference:

* modes ``hotspot`` | ``wlan`` | ``disconnected`` | ``busy``; the AP is the
  NetworkManager connection named ``Hotspot``; ``known_networks`` are the Wi-Fi
  connection NAMES except ``Hotspot``; ``connected_network`` is the first active
  one (``Hotspot`` included, as on the daemon);
* every mutation is a background thread behind ONE busy lock -- a second
  mutation while it is held answers 409, ``/wifi/status`` reads ``busy`` --
  and a 200 means "accepted", never "connected";
* ``connect``: create the profile (``nmcli device wifi connect``) when it does not
  exist, else bring it up (the password is then NOT used, as on the daemon); on
  failure keep the error, DELETE that profile and fall back to the hotspot (the
  fallback still runs when the delete fails);
* ``forget``: 400 for ``Hotspot``, 404 for an unknown name, falls back to the
  hotspot when the forgotten network was the live one;
* ``scan_and_list``: rescan, list, de-duplicate in signal order;
* :func:`ensure_wifi_on_startup`: no Wi-Fi at boot -> raise the hotspot, 5 tries
  3 s apart -- the daemon's own boot fallback, which on v2 nothing else runs
  (here "no Wi-Fi" must hold for 40 s first; the function says why).

The hotspot it creates (only ever when no ``Hotspot`` profile exists) carries
``EMORI_AP_SSID``/``EMORI_AP_PASSWORD`` when those are a usable pair, else the
daemon's published defaults -- what a v1 robot did, since emori-admin rebrands
the profile the daemon then reuses.

How nmcli is run, compared with the daemon's ``nmcli`` pip package (which runs
EVERY call as ``sudo nmcli`` with ``LANG=C`` and no timeout):

* ``sudo -n nmcli`` only for what needs root on a session-less system service
  (rescan, the scan list that waits on it, up/delete/connect/hotspot); reads
  (the connection list) run as ``pollen``.  The exact argv set is allowlisted in
  ``deploy/robot/sudoers-emori-admin`` and a test holds the two together.
* ``LANG``/``LC_ALL=C.UTF-8``: untranslated messages like the package's ``C``,
  but an SSID with a non-ASCII character survives the round trip.
* Every call is bounded (an activation gets 120 s: nmcli itself waits up to 90).
* The Wi-Fi password travels in argv, exactly as the package passes it
  (``device wifi connect <ssid> password <psk>``).  ``nmcli --ask`` with the psk
  on stdin would keep it out of argv, but whether nmcli reads a piped stdin
  depends on the NetworkManager version, and a join that
  silently ignores the password is worse than the exposure.  What is done
  instead: the sudoers fragment turns sudo's own command log off for exactly
  these invocations (``!log_allowed``), nothing here logs an argv, and any
  nmcli message that could quote the password is redacted before it is kept.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from emori_adapter.admin import probes

log = logging.getLogger("emori.admin.netman")

HOTSPOT = "Hotspot"
# The daemon's own defaults (wifi_config.py:11-12) for an AP it has to create.
# Pollen Robotics' published vendor default.
DAEMON_AP_SSID = "reachy-mini-ap"
DAEMON_AP_PASSWORD = "reachy-mini"

# The daemon package's own field list for `device wifi list` (nmcli/_device.py).
WIFI_LIST_FIELDS = "IN-USE,SSID,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY"
CONNECTION_FIELDS = "NAME,UUID,TYPE,DEVICE"
# Terse output spells the type out; the table form says "wifi".
WIFI_TYPES = ("802-11-wireless", "wifi")
# An inactive connection's DEVICE: "--" in the table, empty in some terse builds.
NO_DEVICE = ("", "--")
# How nmcli shows a network that does not broadcast its name.
HIDDEN_SSIDS = ("", "--")

READ_TIMEOUT_S = 5.0
RESCAN_TIMEOUT_S = 15.0
# The list waits for the scan the rescan started; on the CM4 that is seconds.
LIST_TIMEOUT_S = 30.0
# nmcli's own activation wait is 90 s; ours must be longer so nmcli reports first.
ACTIVATE_TIMEOUT_S = 120.0
DELETE_TIMEOUT_S = 30.0

BOOT_RETRIES = 5
BOOT_RETRY_DELAY_S = 3.0
# "disconnected" must hold on this many more readings, this far apart, before the
# boot check raises the hotspot (see ensure_wifi_on_startup): 40 s in all.
BOOT_CONFIRM_READINGS = 4
BOOT_CONFIRM_INTERVAL_S = 10.0

BUSY_DETAIL = "Another operation is in progress."

# nmcli exit codes -> the message the daemon's package raises (nmcli/_system.py).
# The daemon keeps str(exception) as its "last error", so these ARE the error
# texts a phone has always shown.
_RC_MESSAGES = {
    2: "Invalid user input, wrong nmcli invocation",
    3: "Timeout expired",
    4: "Connection activation failed",
    5: "Connection deactivation failed",
    6: "Disconnecting device failed",
    7: "Connection deletion failed",
    8: "NetworkManager is not running",
    10: "Connection, device, or access point does not exist",
}


class NmcliError(Exception):
    """One failed nmcli step.  ``str()`` is safe to keep and show (redacted)."""

    def __init__(self, message: str, slug: str = "nmcli_failed") -> None:
        super().__init__(message)
        self.slug = slug


# --------------------------------------------------------------------------
# argv (module-level so the sudoers test can hold the allowlist against them)
# --------------------------------------------------------------------------
def read_argv(*args: str) -> list[str]:
    return ["nmcli", *args]


def root_argv(*args: str) -> list[str]:
    # -n: never prompt. A sudo that wants a password fails at once with a
    # message, instead of hanging a request thread on a terminal that is not there.
    return ["sudo", "-n", "nmcli", *args]


def connections_argv() -> list[str]:
    return read_argv("-t", "-f", CONNECTION_FIELDS, "connection", "show")


def rescan_argv() -> list[str]:
    return root_argv("device", "wifi", "rescan")


def wifi_list_argv() -> list[str]:
    return root_argv("-t", "-f", WIFI_LIST_FIELDS, "device", "wifi", "list")


def up_argv(name: str) -> list[str]:
    # `id` so a profile called "uuid" or "path" is still a name.
    return root_argv("connection", "up", "id", name)


def delete_argv(name: str) -> list[str]:
    return root_argv("connection", "delete", "id", name)


def connect_argv(ssid: str, password: str) -> list[str]:
    if password:
        return root_argv("device", "wifi", "connect", ssid, "password", password)
    # An open network. The daemon passes `password ""` here; nmcli has nothing
    # to do with an empty key on an open AP, and on a protected one it is an
    # invalid psk -- leaving it out gives nmcli's own clearer error instead.
    return root_argv("device", "wifi", "connect", ssid)


def hotspot_argv(ssid: str, password: str) -> list[str]:
    # The package adds --show-secrets, which only makes nmcli PRINT the psk.
    # con-name is spelled out rather than relying on nmcli's default name.
    return root_argv("device", "wifi", "hotspot", "con-name", HOTSPOT,
                     "ssid", ssid, "password", password)


# --------------------------------------------------------------------------
# the privilege check (readycheck, through /healthz/deep)
# --------------------------------------------------------------------------
# `sudo -n -l <command>` asks sudo whether that exact command line WOULD be allowed
# without a password, and runs nothing. A placeholder stands in for the SSID and the
# passphrase: the sudoers grants match those slots with `*`, and a real password has
# no business in sudo's own log for a check.
PRIVILEGE_PLACEHOLDER = "emori-privilege-check"
# The answers that mean "this robot cannot do it until someone fixes sudoers" (readycheck
# FAILs admin_wifi_privileges_missing on them; anything else is a WARN).
PRIVILEGES_MISSING = ("sudo_denied", "sudo_missing")


def privileged_argvs() -> list[list[str]]:
    """Every root command line this backend runs, one per sudoers grant it needs."""

    placeholder = PRIVILEGE_PLACEHOLDER
    return [rescan_argv(), wifi_list_argv(), up_argv(HOTSPOT), delete_argv(placeholder),
            connect_argv(placeholder, placeholder), hotspot_argv(placeholder, placeholder)]


def list_argv(argv: list[str]) -> list[str]:
    """``sudo -n nmcli ...`` -> ``sudo -n -l nmcli ...``: the same command, only asked about."""

    if argv[:2] != ["sudo", "-n"]:
        raise ValueError("not a sudo command line")
    return ["sudo", "-n", "-l", *argv[2:]]


def privileges() -> tuple[bool, str]:
    """Would sudo let this service run every nmcli command joins and the hotspot need?

    ``(True, "")``, or ``(False, slug)``: ``sudo_denied`` (sudo refused one: no grant
    and no passwordless sudo -- /etc/sudoers.d/emori-admin is missing or stale),
    ``sudo_missing`` (no sudo at all), ``privilege_check_failed`` (sudo could not be
    asked: a timeout, an exec error -- not evidence either way). Without these grants
    a v2 robot reads its Wi-Fi fine and fails every join, every hotspot and the boot
    hotspot fallback, so a read-only check would pass it.
    """

    for argv in privileged_argvs():
        cmd = probes.run(list_argv(argv), timeout_s=READ_TIMEOUT_S)
        if cmd.ok:
            continue
        if cmd.error == "missing":
            return False, "sudo_missing"
        if cmd.ran and cmd.returncode == 1:
            return False, "sudo_denied"
        return False, "privilege_check_failed"
    return True, ""


def nmcli_env() -> dict:
    env = dict(os.environ)
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    return env


# --------------------------------------------------------------------------
# running nmcli
# --------------------------------------------------------------------------
def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _failure(cmd: probes.Cmd, *, secret: str = "") -> NmcliError:
    if not cmd.ran:
        if cmd.error == "missing":
            return NmcliError("nmcli (or sudo) is not installed on this robot",
                              "nmcli_missing")
        return NmcliError("nmcli could not be started", "nmcli_failed")
    if cmd.error == "timeout":
        return NmcliError("Timeout expired", "nmcli_timeout")
    code = cmd.returncode
    if code in _RC_MESSAGES:
        return NmcliError(_RC_MESSAGES[code])
    detail = _redact((cmd.stderr or "").strip(), secret)
    if code == 1 and "Scanning not allowed" in detail:
        return NmcliError(detail)
    return NmcliError("Unknown or unspecified error [code:%d, detail:%s]" % (code, detail))


def _exec(argv: list[str], timeout_s: float, *, secret: str = "") -> str:
    cmd = probes.run(argv, timeout_s=timeout_s, env=nmcli_env())
    if not cmd.ok:
        raise _failure(cmd, secret=secret)
    return cmd.stdout or ""


def split_terse(line: str) -> list[str]:
    """One ``nmcli -t`` row into fields: ``\\:`` is a colon, ``\\\\`` a backslash."""

    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Connection:
    name: str
    uuid: str
    type: str
    device: str

    @property
    def active(self) -> bool:
        return self.device not in NO_DEVICE


def parse_connections(text: str) -> list[Connection]:
    """Wi-Fi connections only, from ``nmcli -t -f NAME,UUID,TYPE,DEVICE connection show``."""

    found: list[Connection] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        name, uuid, kind, device = fields[0], fields[1], fields[2], fields[3]
        if kind in WIFI_TYPES and name:
            found.append(Connection(name, uuid, kind, device))
    return found


def wifi_connections() -> list[Connection]:
    return parse_connections(_exec(connections_argv(), READ_TIMEOUT_S))


def parse_wifi_list(text: str) -> list[tuple[str, int]]:
    """``(ssid, signal)`` rows of ``nmcli -t -f IN-USE,SSID,...,SECURITY device wifi list``.

    A row that does not parse is skipped rather than failing the scan (the
    package raised on it, turning one odd beacon into an HTTP 500).
    """

    rows: list[tuple[str, int]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        fields = split_terse(line)
        if len(fields) < 9:
            continue
        try:
            signal = int(fields[7])
        except ValueError:
            signal = -1
        rows.append((fields[1], signal))
    return rows


def ssids_by_signal(rows: list[tuple[str, int]]) -> list[str]:
    """Strongest first, each SSID once, hidden networks left out."""

    ordered = sorted(enumerate(rows), key=lambda item: (-item[1][1], item[0]))
    seen: set[str] = set()
    ssids: list[str] = []
    for _, (ssid, _signal) in ordered:
        if ssid in HIDDEN_SSIDS or ssid in seen:
            continue
        seen.add(ssid)
        ssids.append(ssid)
    return ssids


def scan(*, strict: bool = False) -> list[str]:
    """Rescan, then list -- the daemon's ``scan_available_wifi`` + de-duplication.

    ``strict`` is the boot check, where a refused rescan means "the radio is not
    ready yet, try again" exactly as on the daemon.  From the phone it is not
    fatal: NetworkManager refuses to scan while the robot is its own access
    point, which is precisely when an operator is scanning, and the networks it
    already knows are still the useful answer.
    """

    rescan = probes.run(rescan_argv(), timeout_s=RESCAN_TIMEOUT_S, env=nmcli_env())
    if not rescan.ok:
        refused = _failure(rescan)
        if strict:
            raise refused
        log.info("wifi rescan refused (%s); listing the networks NetworkManager "
                 "already knows", refused)
    return ssids_by_signal(parse_wifi_list(_exec(wifi_list_argv(), LIST_TIMEOUT_S)))


def _mode_of(connections: list[Connection]) -> str:
    if any(c.name == HOTSPOT and c.active for c in connections):
        return "hotspot"
    if any(c.active for c in connections):
        return "wlan"
    return "disconnected"


def mode() -> str:
    if _busy.locked():
        return "busy"
    return _mode_of(wifi_connections())


def status() -> dict:
    """The daemon's ``WifiStatus``: ``mode``, ``known_networks``, ``connected_network``."""

    busy = _busy.locked()
    connections = wifi_connections()
    return {
        "mode": "busy" if busy else _mode_of(connections),
        "known_networks": [c.name for c in connections if c.name != HOTSPOT],
        "connected_network": next((c.name for c in connections if c.active), None),
    }


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------
_busy = threading.Lock()
_error_lock = threading.Lock()
_last_error: Optional[str] = None


def last_error() -> Optional[str]:
    with _error_lock:
        return _last_error


def _set_error(text: Optional[str]) -> None:
    global _last_error
    with _error_lock:
        _last_error = text


def busy() -> bool:
    return _busy.locked()


def _start_thread(target: Callable[[], None]) -> None:
    # NOT a daemon thread, like the daemon's own: a Wi-Fi change cut off halfway
    # by a service restart (profile deleted, hotspot not raised yet) is how a
    # robot ends up on no network at all.
    threading.Thread(target=target, name="admin-wifi", daemon=False).start()


# Tests replace this to run the worker inline.
_spawn: Callable[[Callable[[], None]], None] = _start_thread


def hotspot_identity() -> tuple[str, str]:
    """``EMORI_AP_SSID``/``EMORI_AP_PASSWORD`` when usable, else the daemon's defaults."""

    from emori_adapter.admin import network  # noqa: PLC0415 - network imports this module

    ready, _ = network.watchdog_ap_ready()
    if ready:
        return network.configured_ap()
    return DAEMON_AP_SSID, DAEMON_AP_PASSWORD


def setup_connection(name: str, ssid: str, password: str, *,
                     is_hotspot: bool = False) -> None:
    """The daemon's ``setup_wifi_connection``: create if absent, else bring it up."""

    connections = wifi_connections()
    if not any(c.name == name for c in connections):
        if is_hotspot:
            log.info("no %s profile on this robot: creating it", HOTSPOT)
            _exec(hotspot_argv(ssid, password), ACTIVATE_TIMEOUT_S, secret=password)
            return
        log.info("no saved profile for that network: creating it")
        out = _exec(connect_argv(ssid, password), ACTIVATE_TIMEOUT_S, secret=password)
        if "Connection activation failed:" in out:
            # nmcli can print this and still exit 0; the package treats it as a
            # failure, and so does this.
            raise NmcliError(_RC_MESSAGES[4])
        return
    if not any(c.name == name and c.active for c in connections):
        log.info("bringing up the saved profile%s", " Hotspot" if is_hotspot else "")
        _exec(up_argv(name), ACTIVATE_TIMEOUT_S)
        return
    log.info("that profile is already the active connection")


def remove_connection(name: str) -> None:
    if any(c.name == name for c in wifi_connections()):
        _exec(delete_argv(name), DELETE_TIMEOUT_S)


def _fall_back_to_hotspot() -> None:
    ssid, password = hotspot_identity()
    setup_connection(HOTSPOT, ssid, password, is_hotspot=True)


def _connect_worker(ssid: str, password: str) -> None:
    try:
        _set_error(None)
        setup_connection(ssid, ssid, password)
        log.info("joined the requested Wi-Fi network")
    except NmcliError as e:
        message = _redact(str(e), password)
        _set_error(message)
        log.error("failed to join the requested Wi-Fi network: %s", message)
        log.warning("reverting to the hotspot")
        # Two separate steps, unlike the daemon's one try: a delete that fails
        # (NetworkManager still busy after the failed activation, a timeout)
        # must not also cost the hotspot. On v2 this revert is the only thing
        # that keeps a bad password from stranding the robot off-network.
        try:
            remove_connection(ssid)
        except NmcliError as e_delete:
            log.error("could not delete the failed network's profile: %s",
                      _redact(str(e_delete), password))
        try:
            _fall_back_to_hotspot()
        except NmcliError as again:
            log.error("could not raise the hotspot after the failed join: %s",
                      _redact(str(again), password))


def _forget_worker(name: str) -> None:
    try:
        _set_error(None)
        was_active = any(c.name == name and c.active for c in wifi_connections())
        log.info("forgetting a saved Wi-Fi network")
        remove_connection(name)
        if was_active:
            log.warning("the forgotten network was the live one: falling back to the hotspot")
            _fall_back_to_hotspot()
    except NmcliError as e:
        _set_error(str(e))
        log.error("failed to forget a saved Wi-Fi network: %s", e)


def _hotspot_worker(ssid: str, password: str) -> None:
    try:
        _set_error(None)
        setup_connection(HOTSPOT, ssid, password, is_hotspot=True)
        log.info("hotspot is up")
    except NmcliError as e:
        # The daemon's thread drops this on the floor; the page tells the
        # operator to "check the last error above" after a hotspot request, so
        # it is kept here.
        message = _redact(str(e), password)
        _set_error(message)
        log.error("failed to raise the hotspot: %s", message)


def _run_locked(worker: Callable[[], None]) -> probes.Http:
    """Take the busy lock HERE (409 if held), release it when the worker ends.

    Taking it in the request thread rather than inside the worker closes the
    daemon's small race (two requests both seeing it free); nothing else about
    the behaviour changes.
    """

    if not _busy.acquire(blocking=False):
        return _http(409, {"detail": BUSY_DETAIL})

    def body() -> None:
        try:
            worker()
        except Exception as e:  # noqa: BLE001 - a bug must not leave the lock held
            _set_error("Unexpected error (%s)" % type(e).__name__)
            log.error("Wi-Fi worker crashed (%s)", type(e).__name__)
        finally:
            _busy.release()

    try:
        _spawn(body)
    except Exception as e:  # noqa: BLE001 - e.g. no thread could be started
        if _busy.locked():
            _busy.release()
        log.error("could not start the Wi-Fi worker (%s)", type(e).__name__)
        return _http(500, None, error="worker_failed")
    return _http(200, None)


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------
def _still_disconnected(sleep: Callable[[float], None], readings: int,
                        interval_s: float) -> str:
    """``disconnected`` only if every one of ``readings`` later readings says so.

    Otherwise what the first other reading said (``wlan`` / ``hotspot``), or
    ``busy`` the moment an operator starts a change.
    """

    for _ in range(readings):
        sleep(interval_s)
        if _busy.locked():
            return "busy"
        current = _mode_of(wifi_connections())
        if current != "disconnected":
            return current
    return "disconnected"


def ensure_wifi_on_startup(*, sleep: Callable[[float], None] = time.sleep,
                           retries: int = BOOT_RETRIES,
                           delay_s: float = BOOT_RETRY_DELAY_S,
                           confirm_readings: int = BOOT_CONFIRM_READINGS,
                           confirm_interval_s: float = BOOT_CONFIRM_INTERVAL_S) -> str:
    """No Wi-Fi at startup -> raise the hotspot.  The daemon's own boot fallback.

    Returns what happened (``hotspot`` / ``wlan`` / ``busy`` / ``failed`` ...)
    for the journal.  The scan first is the daemon's "make sure wlan0 is up and
    running": it fails while NetworkManager or the radio is not ready, and it
    gives NetworkManager the scan results it needs to start joining a known
    network, so a robot at its usual site is not pushed onto its hotspot.

    One deliberate difference from the daemon, which raises the hotspot on a
    single "disconnected" reading: here it must hold for ``confirm_readings``
    more readings ``confirm_interval_s`` apart (40 s).  This check runs on
    every emori-admin start, not only at power-on (a deploy, the Services
    tab's self-restart), and once the radio is an access point NetworkManager
    never goes back to the site network by itself.  A start that lands in a
    short drop (roaming, NetworkManager between reconnect attempts) must not
    leave the robot on its hotspot -- the watchdog has its grace period for the
    same reason.  A robot with really no known network in range is on its
    hotspot 40 s later than on v1.  It is NOT limited to the first start after
    power-on (the daemon ran it on every daemon start too): a robot redeployed
    over Ethernet while it has no Wi-Fi must come up on its
    hotspot without a reboot.
    """

    for attempt in range(1, retries + 1):
        try:
            scan(strict=True)
            if _busy.locked():
                return "busy"  # an operator is already changing it
            current = _mode_of(wifi_connections())
            if current == "disconnected":
                current = _still_disconnected(sleep, confirm_readings,
                                              confirm_interval_s)
            if current != "disconnected":
                return current
            if not _busy.acquire(blocking=False):
                return "busy"
            try:
                log.warning("no Wi-Fi connection is active at startup: raising the "
                            "hotspot so the robot can be reached")
                _fall_back_to_hotspot()
            finally:
                _busy.release()
            return "hotspot"
        except NmcliError as e:
            log.warning("Wi-Fi init attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                sleep(delay_s)
    log.error("Wi-Fi initialization failed after %d attempts; emori-admin keeps "
              "running without it", retries)
    return "failed"


# --------------------------------------------------------------------------
# the daemon's routes, answered here
# --------------------------------------------------------------------------
def _http(status_code: int, payload: Any = None, *, error: str = "") -> probes.Http:
    ok = status_code < 400
    return probes.Http(ok=ok, reached=True, status=status_code, payload=payload,
                       content_type="application/json",
                       error="" if ok else (error or "http_%d" % status_code))


def _nmcli_failed(e: NmcliError) -> probes.Http:
    # The daemon would 500 here. The slug says WHAT failed, so the phone shows
    # "nmcli missing" rather than "http 500".
    return _http(500, None, error=e.slug)


def request(method: str, path: str, *, params: Optional[dict] = None) -> probes.Http:
    """Answer one daemon ``/wifi/*`` call as the daemon would.  Never raises."""

    params = params or {}
    route = (method.upper(), path)
    try:
        if route == ("GET", "/wifi/status"):
            return _http(200, status())
        if route == ("GET", "/wifi/error"):
            return _http(200, {"error": last_error()})
        if route == ("POST", "/wifi/reset_error"):
            _set_error(None)
            return _http(200, {"status": "ok"})
        if route == ("POST", "/wifi/scan_and_list"):
            return _http(200, scan())
        if route == ("POST", "/wifi/connect"):
            ssid = str(params.get("ssid") or "")
            password = str(params.get("password") or "")
            if not ssid:
                return _http(422, {"detail": "ssid is required"})
            return _run_locked(lambda: _connect_worker(ssid, password))
        if route == ("POST", "/wifi/forget"):
            name = str(params.get("ssid") or "")
            if name == HOTSPOT:
                return _http(400, {"detail": "Cannot forget Hotspot connection."})
            if not any(c.name == name for c in wifi_connections()):
                return _http(404, {"detail": "Network '%s' not found in saved networks."
                                   % name})
            return _run_locked(lambda: _forget_worker(name))
        if route == ("POST", "/wifi/setup_hotspot"):
            ssid = params.get("ssid")
            password = params.get("password")
            if not ssid or not password:
                ssid, password = hotspot_identity()
            return _run_locked(lambda: _hotspot_worker(str(ssid), str(password)))
    except NmcliError as e:
        log.warning("%s %s failed on the host backend: %s", method.upper(), path, e)
        return _nmcli_failed(e)
    except Exception as e:  # noqa: BLE001 - the recovery tool answers, it does not raise
        log.error("%s %s crashed on the host backend (%s)", method.upper(), path,
                  type(e).__name__)
        return _http(500, None, error="host_error")
    return _http(404, {"detail": "Not Found"})
