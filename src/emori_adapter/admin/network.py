"""Wi-Fi for the phone-first admin surface: the daemon's Wi-Fi API + read-only nmcli.

Division of labour, and it is deliberate:

* **Every MUTATION goes through ONE Wi-Fi backend** with the daemon's semantics:
  mutations serialised behind one busy lock, a failed ``connect`` reverting to
  the hotspot by itself, a boot with no known network raising the hotspot
  before anything else runs.  A second writer racing that lock is how a robot
  ends up on no network at all with the operator's phone already disconnected.
  On v1 that backend IS the reachy-mini daemon (``:8000 /wifi/*``).  On v2 the
  daemon is gone and the ROS 2 node's shim on ``:8000`` has no ``/wifi`` routes,
  so the same router runs in this process (:mod:`.netman`); which one is in
  force is decided in exactly one place (:mod:`.host_backend`), and every call
  below goes through :func:`_wifi`, which picks it.  The shapes are the
  daemon's either way, so nothing above this module knows which one answered.
* **Reads that the daemon does not offer** (our IP, the AP's signal strength,
  the Hotspot profile's SSID) come from ``nmcli -t`` -- read-only, no sudo, and
  every one of them optional: a dev box has no nmcli and simply reports
  ``None``.
* **The ONE exception** is the AP rebrand (:func:`rebrand_ap`), which modifies
  the existing NetworkManager ``Hotspot`` profile so that every fallback
  afterwards raises OUR ssid instead of ``reachy-mini-ap``.  It is guarded three
  ways: it needs both env keys, it is idempotent, and it refuses while that
  profile is the ACTIVE connection -- rewriting the AP an operator is standing
  on would drop their phone mid-session.

The mutations are fire-and-forget threads: a 200 from ``connect`` means
"accepted", never "connected".  Callers poll ``/wifi/status`` for ``mode``
leaving ``busy`` and then read ``/wifi/error``.  Nothing here pretends
otherwise.

Env: ``EMORI_AP_SSID`` / ``EMORI_AP_PASSWORD`` (rebrand; changed from the page with
:func:`set_ap_credentials`, which writes ``secrets/admin.env``),
``EMORI_ADMIN_WIFI_WATCHDOG`` / ``EMORI_ADMIN_WIFI_GRACE_S`` (watchdog),
``EMORI_ADMIN_HOST_BACKEND`` (which backend; see :mod:`.host_backend`).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from emori_adapter.admin import host_backend, netman, probes

log = logging.getLogger("emori.admin.network")

# The NetworkManager profile the daemon activates for AP mode; it already exists
# on the robot, which is why rebranding it is enough.
HOTSPOT_PROFILE = "Hotspot"
# The daemon's own defaults (wifi_config.py:11-12) — what the AP is called when
# EMORI_AP_SSID is not set.
# Pollen Robotics' published vendor default.
DAEMON_AP_SSID = "reachy-mini-ap"

# IEEE 802.11: an SSID is 32 bytes, a WPA2 passphrase 8..63 ASCII characters.
MAX_SSID_LEN = 32
MIN_PSK_LEN = 8
MAX_PSK_LEN = 63

WATCHDOG_DEFAULT_GRACE_S = 180.0
# How often the watchdog asks. Far below the grace period so a transient
# `busy` reading cannot be mistaken for the whole outage.
WATCHDOG_POLL_S = 20.0


# --------------------------------------------------------------------------
# validation (the only request data that reaches a subprocess is NONE of it —
# ssid/password travel to the daemon as URL params; these bounds exist so a
# 2 MB "ssid" cannot be forwarded either)
# --------------------------------------------------------------------------
def valid_ssid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    return len(value.encode("utf-8")) <= MAX_SSID_LEN


def valid_password(value: object) -> bool:
    """Empty is legal (an open network); otherwise it must be a usable WPA psk."""

    if value is None or value == "":
        return True
    if not isinstance(value, str):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    return MIN_PSK_LEN <= len(value) <= MAX_PSK_LEN


# --------------------------------------------------------------------------
# read-only nmcli
# --------------------------------------------------------------------------
def _wifi_device() -> tuple[str, str]:
    """``(device, connection)`` for the wifi interface, or ``("", "")``."""

    result = probes.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                         "device", "status"])
    if not result.ok:
        return "", ""
    for line in result.lines:
        parts = line.split(":")
        if len(parts) >= 4 and parts[1] == "wifi":
            return parts[0], parts[3]
    return "", ""


def ip_address() -> Optional[str]:
    """The wifi interface's IPv4 address, without the /prefix.  ``None`` if unknown."""

    device, _ = _wifi_device()
    if not device:
        return None
    result = probes.run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device])
    if not result.ok:
        return None
    for line in result.lines:
        _, _, value = line.partition(":")
        address = value.strip().split("/")[0]
        if address:
            return address
    return None


def signal_pct() -> Optional[int]:
    """Signal strength of the AP we are joined to, 0-100.  ``None`` in AP mode.

    ``--rescan no`` is load-bearing: without it every status poll would trigger
    a full scan, which takes seconds and disrupts the link it is measuring.
    """

    result = probes.run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi",
                         "list", "--rescan", "no"])
    if not result.ok:
        return None
    for line in result.lines:
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == "yes":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def hotspot_profile_ssid() -> Optional[str]:
    """The SSID the ``Hotspot`` profile currently broadcasts (no sudo needed)."""

    result = probes.run(["nmcli", "-t", "-g", "802-11-wireless.ssid",
                         "connection", "show", HOTSPOT_PROFILE])
    if not result.ok:
        return None
    value = (result.stdout or "").strip()
    return value or None


def hotspot_profile_psk() -> Optional[str]:
    """The Hotspot psk.  Needs sudo (it is a secret) — ``None`` when unreadable.

    Only ever compared against what we are about to write, never returned
    through a route.
    """

    result = probes.run(["sudo", "nmcli", "-s", "-g",
                         "802-11-wireless-security.psk", "connection", "show",
                         HOTSPOT_PROFILE])
    if not result.ok:
        return None
    return (result.stdout or "").strip() or None


def hotspot_is_active() -> bool:
    """Is the ``Hotspot`` profile the connection currently up?"""

    result = probes.run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    if not result.ok:
        return False
    return any(line.strip() == HOTSPOT_PROFILE for line in result.lines)


# --------------------------------------------------------------------------
# the AP identity we hand the operator
# --------------------------------------------------------------------------
def configured_ap() -> tuple[str, str]:
    return (os.environ.get("EMORI_AP_SSID", "").strip(),
            os.environ.get("EMORI_AP_PASSWORD", "").strip())


def ap_credentials() -> tuple[str, str]:
    """``(ssid, password)`` of the robot's own hotspot, as this process has them (env).

    ``secrets/admin.env`` (written by :func:`set_ap_credentials`) outranks ``.env`` and
    is in this environment from the service's start, plus any change made since.
    """

    return configured_ap()


def valid_ap_password(value: object) -> bool:
    """A WPA2 passphrase the hotspot can use: 8..63 printable ASCII characters."""

    return (isinstance(value, str) and MIN_PSK_LEN <= len(value) <= MAX_PSK_LEN
            and all(" " <= ch <= "~" for ch in value))


def set_ap_credentials(ssid: object = None, password: object = None, *,
                       root=None) -> tuple[bool, str]:
    """Save a new hotspot name and/or password: ``(ok, code)``.

    ``None`` keeps that half. Codes: ``ok``, ``nothing_to_set``, ``bad_ssid`` (not 1..32
    bytes, or a control character), ``bad_password`` (not 8..63 printable ASCII),
    ``save_failed``. Written to ``secrets/admin.env`` (merged, 0600) and put in force in
    this process; the hotspot itself is rebranded by :func:`rebrand_ap` when emori-admin
    next starts (the caller restarts it), and never while the hotspot is the live
    connection. Logs which half changed, never a value.
    """

    from emori_adapter.admin import auth  # noqa: PLC0415 - auth imports nothing of ours

    updates: dict[str, str] = {}
    # No space at either end: the value is read back stripped (configured_ap, and
    # systemd's EnvironmentFile), so the hotspot would broadcast something else than
    # what the operator wrote down.
    if ssid is not None:
        if not valid_ssid(ssid) or ssid != ssid.strip():
            return False, "bad_ssid"
        updates["EMORI_AP_SSID"] = ssid
    if password is not None:
        if not valid_ap_password(password) or password != password.strip():
            return False, "bad_password"
        updates["EMORI_AP_PASSWORD"] = password
    if not updates:
        return False, "nothing_to_set"
    try:
        auth.save_admin_secrets(updates, root=root)
    except (ValueError, OSError) as e:  # SettingsError is a ValueError
        log.warning("hotspot credentials not saved (%s)", type(e).__name__)
        return False, "save_failed"
    os.environ.update(updates)
    log.warning("hotspot %s changed from the admin page; applied when emori-admin "
                "restarts", " and ".join(("name" if k == "EMORI_AP_SSID" else "password")
                                         for k in sorted(updates)))
    return True, "ok"


def ap_ssid() -> str:
    """What the phone should look for in the Wi-Fi list.

    Observed first (the profile is the truth), then the configured value (what
    it is about to become), then the daemon's stock name — the operator needs a
    string to search for even on a box where nmcli cannot be read.
    """

    return hotspot_profile_ssid() or configured_ap()[0] or DAEMON_AP_SSID


# --------------------------------------------------------------------------
# the Wi-Fi backend (the daemon's /wifi router, wherever it runs)
# --------------------------------------------------------------------------
def _wifi(method: str, path: str, *, params: Optional[dict] = None,
          timeout_s: float = probes.HTTP_TIMEOUT_S) -> probes.Http:
    """One daemon ``/wifi/*`` call, answered by whichever backend is in force.

    A daemon that answers with a ROUTE-missing 404 is a robot whose ``:8000`` has
    no Wi-Fi router (the v2 shim): the selector learns it, and the same call is
    answered by the host at once, so the operator's first tap already works.
    """

    if host_backend.current() == host_backend.HOST:
        return netman.request(method, path, params=params)
    result = probes.daemon(method, path, params=params, timeout_s=timeout_s)
    if (host_backend.route_missing(result)
            and host_backend.note_route_missing(path).backend == host_backend.HOST):
        return netman.request(method, path, params=params)
    return result


def wifi_status() -> probes.Http:
    return _wifi("GET", "/wifi/status")


def wifi_error() -> dict:
    result = _wifi("GET", "/wifi/error")
    if not result.ok:
        return host_backend.failure(result)
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {"ok": True, "error_text": payload.get("error")}


def scan() -> dict:
    """``POST /wifi/scan_and_list``.  20 s: a CM4 rescan really does take that long."""

    result = _wifi("POST", "/wifi/scan_and_list", timeout_s=probes.SCAN_TIMEOUT_S)
    if not result.ok:
        return host_backend.failure(result)
    ssids = result.payload if isinstance(result.payload, list) else []
    return {"ok": True, "ssids": [str(s) for s in ssids if s]}


def connect(ssid: str, password: str) -> dict:
    """Ask the Wi-Fi backend to join ``ssid``.  Accepted != connected (see module doc)."""

    result = _wifi("POST", "/wifi/connect",
                   params={"ssid": ssid, "password": password or ""})
    if not result.ok:
        return host_backend.failure(result)
    # The backend spawns a thread and returns immediately; the UI must poll
    # /api/network/status until `mode` leaves "busy", then read the error route.
    return {"ok": True, "accepted": True}


def forget(ssid: str) -> dict:
    result = _wifi("POST", "/wifi/forget", params={"ssid": ssid})
    if not result.ok:
        body = host_backend.failure(result)
        if body["error"] == "http_404":
            # The backend's own answer ("Network 'x' not found in saved networks."),
            # not a missing route: the list the phone shows is out of date.
            body["error"] = "not_saved"
        return body
    return {"ok": True, "accepted": True}


def start_hotspot() -> dict:
    """Enter AP mode NOW.

    Our ssid/password are passed even though the backend reuses the existing
    ``Hotspot`` profile when it exists: if that profile were ever deleted, the
    backend would recreate it, and it must come back with OUR identity rather
    than silently reverting to ``reachy-mini-ap`` on a site whose staff were
    told a different name.
    """

    ssid, password = configured_ap()
    params = None
    if ssid and len(password) >= MIN_PSK_LEN:
        params = {"ssid": ssid, "password": password}
    result = _wifi("POST", "/wifi/setup_hotspot", params=params)
    if not result.ok:
        return host_backend.failure(result)
    return {"ok": True, "accepted": True, "ap_ssid": ap_ssid()}


# --------------------------------------------------------------------------
# the composed status the UI draws
# --------------------------------------------------------------------------
def normalize_mode(value: object) -> Optional[str]:
    """The daemon's ``mode`` as a plain string, whichever shape it arrived in.

    The daemon serialises ``WifiMode`` as its VALUE (``"wlan"``), but a pydantic
    enum can come back as ``{"value": "wlan"}`` depending on the daemon build.
    EVERY reader must go through here: a second reader comparing the raw field
    against ``"disconnected"`` would silently take the healthy branch forever on
    a build that objectifies it — which is how the watchdog would never fire on
    the very outage it exists for, with no log line to say so.
    """

    if isinstance(value, dict):
        value = value.get("value")
    if value is None or value == "":
        return None
    return str(value)


def status(*, with_internet: bool = True) -> dict:
    """Everything the Network tab's header card needs, best-effort per field."""

    result = wifi_status()
    payload = result.payload if isinstance(result.payload, dict) else {}
    mode = normalize_mode(payload.get("mode"))
    known = payload.get("known_networks")
    return {
        "ok": bool(result.ok),
        "error": host_backend.error_slug(result),
        "mode": mode,
        "ssid": payload.get("connected_network"),
        "known_networks": [str(s) for s in known] if isinstance(known, list) else [],
        "ip": ip_address(),
        "signal": signal_pct(),
        "internet": probes.internet_ok() if with_internet else None,
        "ap_ssid": ap_ssid(),
    }


# --------------------------------------------------------------------------
# AP rebrand (startup, idempotent)
# --------------------------------------------------------------------------
def rebrand_ap() -> dict:
    """Point the ``Hotspot`` profile at EMORI_AP_SSID/PASSWORD.  Never raises.

    Returns ``{"applied": bool, "reason": str}`` — the reason is what gets
    logged, because the silent case ("already correct") and the refused case
    ("it is live right now") must be distinguishable in a journal.
    """

    ssid, password = configured_ap()
    if not ssid or not password:
        return {"applied": False, "reason": "not_configured"}
    if not valid_ssid(ssid) or len(password) < MIN_PSK_LEN or len(password) > MAX_PSK_LEN:
        log.warning("EMORI_AP_SSID/EMORI_AP_PASSWORD are not a usable WPA2 pair "
                    "(ssid <= %d bytes, password %d..%d chars); leaving the stock AP",
                    MAX_SSID_LEN, MIN_PSK_LEN, MAX_PSK_LEN)
        return {"applied": False, "reason": "invalid_config"}

    current_ssid = hotspot_profile_ssid()
    if current_ssid is None:
        # No profile (or no nmcli): the backend creates one on its next fallback,
        # and start_hotspot() passes our identity when it does.
        return {"applied": False, "reason": "profile_unreadable"}
    if current_ssid == ssid:
        current_psk = hotspot_profile_psk()
        if current_psk is None or current_psk == password:
            # Unreadable psk (no sudo on a dev box) counts as done: re-writing
            # the profile on every boot to chase a value we cannot observe
            # would be a non-idempotent loop, and the ssid — the part the
            # operator sees — already matches.
            return {"applied": False, "reason": "already_current"}

    if hotspot_is_active():
        # The operator is very probably standing on this AP right now. Changing
        # its ssid/psk under them drops their phone and leaves them on a robot
        # they can no longer reach. Try again on a boot that comes up on wifi.
        log.warning("AP rebrand deferred: the Hotspot profile is the ACTIVE "
                    "connection and rewriting it would disconnect whoever is on it")
        return {"applied": False, "reason": "active_hotspot"}

    result = probes.run(["sudo", "nmcli", "connection", "modify", HOTSPOT_PROFILE,
                         "802-11-wireless.ssid", ssid,
                         "802-11-wireless-security.psk", password],
                        timeout_s=probes.ACTION_TIMEOUT_S)
    if not result.ok:
        log.warning("AP rebrand failed (%s); the stock AP name stays in place",
                    result.error or "nmcli error")
        return {"applied": False, "reason": result.error or "nmcli_failed"}
    log.warning("AP rebranded: the Hotspot profile now broadcasts %r", ssid)
    return {"applied": True, "reason": "modified"}


# --------------------------------------------------------------------------
# watchdog
# --------------------------------------------------------------------------
def watchdog_enabled() -> bool:
    return os.environ.get("EMORI_ADMIN_WIFI_WATCHDOG", "1").strip() != "0"


def watchdog_ap_ready() -> tuple[bool, str]:
    """May the watchdog raise the AP *unattended*?  ``(ready, reason)``.

    The watchdog's entire action is "broadcast an access point", so the identity
    of that access point is a precondition, not a detail.  With ``EMORI_AP_SSID``
    / ``EMORI_AP_PASSWORD`` unset the AP is the vendor's: the daemon's own
    ``HOTSPOT_SSID``/``HOTSPOT_PASSWORD`` constants (``reachy-mini-ap`` /
    ``reachy-mini``, wifi_config.py:11-12) — published in a pip-installable
    package — and on a robot where the ``Hotspot`` profile already exists the
    daemon reuses that profile's stock psk whatever we pass it.

    Automatically moving the robot onto a network whose passphrase is printed in
    the vendor's source hands anyone in radio range L3 access to the daemon on
    :8000 (unauthenticated: motors, wifi, HF token), the hostapp on :8090, this
    service on :8070, and — WPA2-PSK with a known passphrase being decryptable
    by anyone who catches the handshake — the plaintext ``emori_admin`` cookie of
    whoever is administering the robot.  Three minutes of induced radio trouble
    would be enough to ask for it.

    So the automatic path is gated on an AP identity we control.  The MANUAL
    "Start hotspot now" button is deliberately not: that is an operator standing
    in front of the robot choosing it, which is the recovery flow the whole
    service exists for.
    """

    ssid, password = configured_ap()
    if not ssid or not password:
        return False, "ap_not_configured"
    if not valid_ssid(ssid) or not MIN_PSK_LEN <= len(password) <= MAX_PSK_LEN:
        return False, "ap_invalid"
    return True, "ok"


def watchdog_grace_s() -> float:
    raw = os.environ.get("EMORI_ADMIN_WIFI_GRACE_S", "").strip()
    if not raw:
        return WATCHDOG_DEFAULT_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("EMORI_ADMIN_WIFI_GRACE_S=%r is not a number; using %s",
                    raw, WATCHDOG_DEFAULT_GRACE_S)
        return WATCHDOG_DEFAULT_GRACE_S
    # A grace period under a poll interval would fire on the first sample and
    # turn every momentary reassociation into an AP flap.
    return max(WATCHDOG_POLL_S * 2, value)


class WifiWatchdog:
    """Decides WHEN a sustained disconnection should raise the hotspot.

    Pure and clock-injected so the 180-second decision is a unit test rather
    than a three-minute one.  It only ever answers "trigger now"; the caller
    then asks the Wi-Fi BACKEND to set up the hotspot (the daemon on v1,
    :mod:`.netman` on v2 -- never an nmcli call of its own), so that backend's
    one busy lock still serialises everything.

    Boot-time loss is not this object's job — ``ensure_wifi_on_startup`` covers
    it (the daemon's on v1, :func:`.netman.ensure_wifi_on_startup` started by
    emori-admin on v2).  This covers the mid-day case:
    the site changes its Wi-Fi password at 14:00 and the robot would otherwise
    sit unreachable until someone drives out with a keyboard.
    """

    def __init__(self, *, grace_s: float = WATCHDOG_DEFAULT_GRACE_S) -> None:
        self.grace_s = float(grace_s)
        self._down_since: Optional[float] = None

    @property
    def down_since(self) -> Optional[float]:
        return self._down_since

    def observe(self, mode: Optional[str], *, now: float) -> bool:
        """Feed one ``/wifi/status`` reading.  True = raise the hotspot now."""

        if mode != "disconnected":
            # "busy" is a mutation in flight and "hotspot" is the goal state;
            # neither is an outage, and both reset the clock.
            self._down_since = None
            return False
        if self._down_since is None:
            self._down_since = now
            return False
        if now - self._down_since < self.grace_s:
            return False
        # Re-arm rather than latch: if setup_hotspot silently failed, a robot
        # left stranded must get another attempt one grace period later.
        self._down_since = now
        return True
