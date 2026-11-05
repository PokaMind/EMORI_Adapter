"""Bounded, never-raising probes of everything OUTSIDE the admin process.

The admin service is the recovery tool: it is reached for precisely when the
daemon, the hostapp, the network or the audio stack is already broken.  So every
call that leaves this process goes through one of the two primitives here, and
both of them answer with a record instead of raising:

* :func:`run` — subprocess, **argv list only** (never ``shell=True``), always
  with a timeout, output captured;
* :func:`request` — httpx, 5 s by default (20 s for a Wi-Fi scan, which really
  does take that long on the CM4).

Two rules that are not obvious from the signatures:

* **No exception text ever reaches a caller.**  ``error`` is a slug derived from
  the exception TYPE.  ``POST /wifi/connect`` carries the site's Wi-Fi password
  as a URL parameter, and httpx exception strings quote the URL — one
  ``str(e)`` in a 502 body or a journal line would publish it.
* **Every module here imports the MODULE, not the names** (``probes.run(...)``,
  not ``from .probes import run``), so a test can monkeypatch one attribute and
  have the whole service go offline deterministically on a dev box with no
  nmcli, no systemctl and no daemon.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from emori_adapter.site_settings import DEFAULT_POKAMIND_BASE

log = logging.getLogger("emori.admin.probes")

# A dead dependency must be reported as dead within one poll of the phone UI
# (5 s), so the default HTTP budget is 5 s and the default subprocess budget is
# smaller still — nmcli/systemctl answer in milliseconds when they answer.
HTTP_TIMEOUT_S = 5.0
SCAN_TIMEOUT_S = 20.0
CMD_TIMEOUT_S = 5.0
# systemctl restart of a unit with a slow ExecStop can genuinely take seconds.
ACTION_TIMEOUT_S = 30.0
# The internet probe must not be able to hold a request thread for a whole poll
# interval; a TCP handshake either lands fast or the link is not usable anyway.
INTERNET_TIMEOUT_S = 2.0


# --------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Cmd:
    """One finished (or never-started) subprocess.  ``ok`` = ran AND exited 0."""

    ok: bool = False
    ran: bool = False
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""  # "" when it ran; else a slug: missing/timeout/os_error

    @property
    def lines(self) -> list[str]:
        return [line for line in (self.stdout or "").splitlines()]


def run(argv: list[str], *, timeout_s: float = CMD_TIMEOUT_S,
        runner: Optional[Callable[..., Any]] = None,
        env: Optional[dict] = None) -> Cmd:
    """Run ``argv``.  Never raises, never uses a shell, always bounded.

    ``env`` replaces the child's environment when given (nmcli is run with a
    fixed C locale so its messages are the ones the code parses).  Output that
    is not valid text is decoded with replacement characters rather than
    raising: an SSID is arbitrary bytes, and one odd network in radio range
    must not turn a scan into a traceback.
    """

    call = runner if runner is not None else subprocess.run
    kwargs: dict[str, Any] = {"capture_output": True, "text": True,
                              "errors": "replace", "timeout": timeout_s,
                              "check": False}
    if env is not None:
        kwargs["env"] = env
    try:
        completed = call(argv, **kwargs)  # noqa: S603 - argv list, no shell, bounded
    except FileNotFoundError:
        # The dev box (no nmcli/systemctl/journalctl) and a robot missing a tool
        # land here.  Not an error worth a traceback: the feature is absent.
        return Cmd(error="missing")
    except subprocess.TimeoutExpired:
        return Cmd(ran=True, error="timeout")
    except (OSError, subprocess.SubprocessError):
        return Cmd(error="os_error")
    code = int(getattr(completed, "returncode", 0) or 0)
    return Cmd(ok=code == 0, ran=True, returncode=code,
               stdout=getattr(completed, "stdout", "") or "",
               stderr=getattr(completed, "stderr", "") or "",
               error="" if code == 0 else "exit_%d" % code)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Http:
    """One HTTP answer.  ``ok`` = the peer answered AND the status was < 400."""

    ok: bool = False
    reached: bool = False
    status: int = 0
    payload: Any = None       # parsed JSON, only when it parsed to a dict/list
    content: bytes = b""
    content_type: str = ""
    error: str = ""           # "" when ok; else a slug (never exception text)


def _error_slug(exc: BaseException) -> str:
    """A stable slug from the exception TYPE.  Never its text — see module doc."""

    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "Connect" in name:
        return "unreachable"
    return "transport_error"


def request(method: str, url: str, *, timeout_s: float = HTTP_TIMEOUT_S,
            params: Optional[dict] = None, json_body: Any = None,
            headers: Optional[dict] = None) -> Http:
    """One bounded HTTP round trip.  Never raises."""

    import httpx  # core dependency (pyproject.toml)

    try:
        response = httpx.request(method.upper(), url, timeout=timeout_s,
                                 params=params, json=json_body, headers=headers)
    except Exception as e:  # noqa: BLE001 - any transport fault is "not reached"
        return Http(error=_error_slug(e))
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001 - a JPEG body is not JSON, and that is fine
        parsed = None
    status = int(response.status_code)
    return Http(
        ok=status < 400,
        reached=True,
        status=status,
        payload=parsed,
        content=response.content,
        content_type=response.headers.get("content-type", ""),
        error="" if status < 400 else "http_%d" % status,
    )


def fail(result: Http, *, when_unreachable: str) -> dict:
    """The structured failure body a route returns for a bad :class:`Http`.

    ``when_unreachable`` names WHICH peer was silent (``daemon_down`` /
    ``hostapp_down``) so the phone can say something better than "error".
    """

    return {"ok": False,
            "error": when_unreachable if not result.reached else result.error,
            "status": result.status}


# --------------------------------------------------------------------------
# where the two peers live
# --------------------------------------------------------------------------
def daemon_base() -> str:
    """The reachy-mini daemon (:8000).  Same env key the hostapp reads."""

    return os.environ.get("REACHY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def hostapp_base() -> str:
    """The EMORI hostapp.  Derived from HOSTAPP_PORT, exactly like readycheck.

    Never a hardcoded 8090 and deliberately NO admin-specific override key: the
    robot sets the port once in ``.env`` and both processes read the same value,
    so there is no second knob that can drift out of sync with the first.
    """

    port = os.environ.get("HOSTAPP_PORT", "8080").strip() or "8080"
    return "http://127.0.0.1:%s" % port


def daemon(method: str, path: str, **kw) -> Http:
    return request(method, daemon_base() + path, **kw)


def hostapp(method: str, path: str, **kw) -> Http:
    return request(method, hostapp_base() + path, **kw)


# --------------------------------------------------------------------------
# a tiny TTL cache (the /api/overview fan-out)
# --------------------------------------------------------------------------
class TtlCache:
    """Memoise zero-arg probes for ``ttl_s``.  Thread-safe, never raises.

    The producer runs UNDER the lock on purpose: the phone polls the overview
    every 5 s and a second tab (or a stuck request) must not double the load on
    a CM4 that has ~0.9 of one core free.
    """

    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, producer: Callable[[], Any], *,
            now: Optional[float] = None) -> Any:
        seconds = float(now if now is not None else time.monotonic())
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and seconds - hit[0] < self.ttl_s:
                return hit[1]
            try:
                value = producer()
            except Exception:  # noqa: BLE001 - a probe must never 500 the overview
                log.warning("admin probe %s failed", key, exc_info=False)
                value = None
            self._entries[key] = (seconds, value)
            return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# --------------------------------------------------------------------------
# composite probes used by /api/overview
# --------------------------------------------------------------------------
def pokamind_base() -> str:
    """The Pokamind API origin this robot talks to (``POKAMIND_BASE``, else the shipped one).

    An empty value counts as unset, as everywhere in the settings layer.
    """

    return ((os.environ.get("POKAMIND_BASE") or "").strip()
            or DEFAULT_POKAMIND_BASE).rstrip("/")


def internet_ok() -> bool:
    """Is there a route to the outside world?  Cheap, bounded, best-effort.

    NetworkManager already knows (it runs its own periodic connectivity check),
    so the read-only ``nmcli`` answer is free and instant.  Off a robot — or on a
    NetworkManager whose check is disabled and reports ``unknown`` — fall back
    to a TCP handshake against the ONE host the robot actually needs
    (``POKAMIND_BASE``), because "the internet" is not an abstract question
    here: a session cannot start without that origin.
    """

    connectivity = run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"])
    if connectivity.ok:
        state = (connectivity.stdout or "").strip().splitlines()
        value = (state[0] if state else "").strip().lower()
        if value in ("full",):
            return True
        if value in ("none", "portal", "limited"):
            return False
        # "unknown" -> NM's connectivity check is off; fall through and measure.
    return _tcp_reachable(pokamind_base())


def _tcp_reachable(url: str) -> bool:
    parts = urlsplit(url if "//" in url else "https://" + url)
    host = parts.hostname or ""
    if not host:
        return False
    port = parts.port or (80 if parts.scheme == "http" else 443)
    try:
        with socket.create_connection((host, port), timeout=INTERNET_TIMEOUT_S):
            return True
    except OSError:  # incl. DNS failure, refused, timeout
        return False


def pokamind_api_ok() -> bool:
    """Mirrors readycheck's ``gate_pokamind``: reached and not a 5xx."""

    result = request("GET", pokamind_base() + "/")
    return bool(result.reached and result.status < 500)


def hostapp_healthz() -> Optional[dict]:
    """The hostapp's own ``/healthz`` payload, or ``None`` when it is down."""

    result = hostapp("GET", "/healthz")
    return result.payload if (result.ok and isinstance(result.payload, dict)) else None


def hostapp_status() -> Optional[dict]:
    """``GET /api/status`` — read for the live ``mic_profile`` (server.py:1244)."""

    result = hostapp("GET", "/api/status")
    return result.payload if (result.ok and isinstance(result.payload, dict)) else None


def daemon_ok() -> bool:
    """Is the reachy-mini daemon up and running?

    Same route and same assertion the hostapp's own robot client uses
    (``robot/client.py:89`` — ``/api/daemon/status`` with ``state == "running"``).
    NOT ``GET /health-check``: that route is POST-only, conditionally registered,
    and is a watchdog reset rather than a readiness probe.
    """

    result = daemon("GET", "/api/daemon/status")
    payload = result.payload if isinstance(result.payload, dict) else {}
    return bool(result.ok and payload.get("state") == "running")


# --------------------------------------------------------------------------
# CPU sampling (/proc/stat deltas)
# --------------------------------------------------------------------------
@dataclass
class CpuSampler:
    """Busy-% between consecutive calls, from ``/proc/stat``.

    A single ``/proc/stat`` read gives totals since boot, which on a robot that
    has been up for a week is a flat ~4% forever and tells an operator nothing.
    The overview is polled every few seconds, so the delta between two polls IS
    the number they want.  First call (and any non-Linux box) returns ``None``.
    """

    _last: Optional[tuple[int, int]] = field(default=None, repr=False)

    def sample(self) -> Optional[float]:
        fields = self._read()
        if fields is None:
            return None
        total, idle = fields
        previous, self._last = self._last, fields
        if previous is None:
            return None
        delta_total = total - previous[0]
        delta_idle = idle - previous[1]
        if delta_total <= 0:
            return None
        busy = 100.0 * (delta_total - delta_idle) / delta_total
        return round(max(0.0, min(100.0, busy)), 1)

    @staticmethod
    def _read() -> Optional[tuple[int, int]]:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                line = fh.readline()
        except OSError:  # not Linux, or /proc not mounted
            return None
        parts = line.split()
        if not parts or parts[0] != "cpu" or len(parts) < 5:
            return None
        try:
            values = [int(v) for v in parts[1:]]
        except ValueError:
            return None
        # idle + iowait: a robot blocked on its SD card is not doing work.
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
