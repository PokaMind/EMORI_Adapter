"""Can emori-admin read Wi-Fi status and the speaker level right now?  readycheck asks.

The failure this guards against is an emori-admin whose Wi-Fi and volume calls go to
routes the robot does not serve: the phone's Network tab answers "HTTP 404", and
nothing on the robot says so until somebody needs it.  readycheck therefore asks the
RUNNING service (``GET /healthz/deep``, gated on ``EMORI_ADMIN_TOKEN``, no PIN)
and that route answers with :func:`run`: the same two calls the phone's tabs make,
through the same backend selection (:mod:`.host_backend`) -- so what is proved is the
code and the choice actually in force, a stale process included, and not a fresh
import in readycheck's own process.

Content-free by construction: booleans, the backend's name, the reason it was chosen,
one error word per part, and the deploy marker this process started on (a commit and a
branch name: readycheck FAILs ``admin_stale`` when the marker on disk is newer).  Never
an SSID, a level, a card name or a log line.

Read-only: the Wi-Fi part is ``GET /wifi/status`` (on the host backend a connection
list, ``nmcli connection show`` -- never a rescan, which would disturb the link it is
asking about); the volume part is ``GET /api/volume/current``. On the host backend it
also asks sudo whether the nmcli commands that CHANGE the Wi-Fi (join, hotspot, the boot
fallback) would be allowed -- ``sudo -n -l``, which executes nothing
(:func:`.netman.privileges`): without that grant the reads pass and every join fails.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from emori_adapter.admin import audio, host_backend, netman, network, probes

# The answers that are not "try again later" but "this robot cannot do it at all":
# the route does not exist, or the tool the host backend runs is not installed.
# readycheck FAILs on these (hostapp/readycheck.py keeps the same set).
BACKEND_MISSING = frozenset({host_backend.ROUTE_MISSING, "nmcli_missing", "alsa_missing"})

_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _part(result: probes.Http) -> dict:
    error = host_backend.error_slug(result)
    return {
        "ok": bool(result.ok),
        "route_missing": host_backend.route_missing(result),
        # A slug from a fixed vocabulary; anything else would be content.
        "error": error if not error or _SLUG.fullmatch(error) else "error",
    }


def run(*, commit: str = "") -> dict:
    """``{"ok", "service", "commit", "backend", "reason", "wifi": {...}, "volume": {...}}``.

    ``commit`` is the deploy marker the calling process read when it started.
    """

    # In parallel: on a v1 robot both are daemon calls with their own timeouts, and
    # readycheck waits for the slower one only.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin-deep") as pool:
        wifi = pool.submit(network.wifi_status)
        volume = pool.submit(audio.speaker_read)
        wifi_part, volume_part = _part(wifi.result()), _part(volume.result())
    # Asked AFTER the two calls: a route-missing 404 during them switches the
    # selector, and the answer must name the backend that actually answered.
    decision = host_backend.decision()
    privileges = _privileges() if decision.backend == host_backend.HOST else None
    return {
        "ok": wifi_part["ok"] and volume_part["ok"]
        and (privileges is None or privileges["ok"]),
        "service": "emori-admin",
        "commit": commit,
        "backend": decision.backend,
        "reason": decision.reason,
        "wifi": wifi_part,
        "volume": volume_part,
        # Only on the host backend (None on v1: the daemon runs nmcli as root itself).
        "wifi_privileges": privileges,
    }


def _privileges() -> dict:
    ok, error = netman.privileges()
    return {"ok": ok, "error": error if not error or _SLUG.fullmatch(error) else "error"}
