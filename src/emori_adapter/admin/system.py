"""Services, logs, machine facts, readycheck, power — all through an allowlist.

This module is the one that runs privileged commands, so every entry point
starts by refusing anything it was not built for:

* **Units come from :data:`ALLOWED_UNITS`, always.**  The unit name is the only
  request-supplied value that reaches an argv anywhere in this service, and it
  is compared against a fixed tuple BEFORE a subprocess exists — not sanitised,
  not escaped, not regex-matched.  Same tuple gates ``/api/logs``.
* **Actions come from :data:`ALLOWED_ACTIONS``** (restart/start/stop), and the
  admin's own unit accepts only ``restart``: an operator who stops the service
  they are holding cannot start it again from the page they just killed.
* **journalctl runs with NO sudo.**  The unit grants
  ``SupplementaryGroups=systemd-journal`` instead, so reading logs stays a
  group membership rather than a privilege escalation.

The self-affecting commands (restart of ``emori-admin``, reboot, poweroff) are
fired from a short-delay background thread so the HTTP response reaches the
phone before the process dies — otherwise every one of them looks like a
network error to the operator who just asked for it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from typing import Optional

from emori_adapter.admin import probes

log = logging.getLogger("emori.admin.system")

SELF_UNIT = "emori-admin.service"
HOSTAPP_UNIT = "emori-hostapp.service"
HUB_UNIT = "emori-hub.service"
# The flag file that decides which of the two runs the kiosk's gateway. With it present
# emori-hub.service starts and emori-hostapp.service is a skipped start; without it the
# other way round. Overridable only so the tests can point it somewhere that is not /opt.
HUB_FLAG_ENV = "EMORI_HUB_FLAG"
HUB_FLAG_DEFAULT = "/opt/emori/HUB_IS_ROS"
# Exactly the units deploy/robot/sudoers-emori-admin grants, in the order the
# UI should list them (the thing that breaks most often first).
ALLOWED_UNITS = (
    HOSTAPP_UNIT,
    HUB_UNIT,
    "emori-kiosk.service",
    "emori-camera-bridge.service",
    "reachy-mini-daemon.service",
    SELF_UNIT,
)
ALLOWED_ACTIONS = ("restart", "start", "stop")

LOG_LINES_DEFAULT = 200
LOG_LINES_MAX = 1000

# A readycheck walks the whole session chain over the network; 180 s is the
# budget and comfortably past the ~40 s a healthy robot takes.
READYCHECK_TIMEOUT_S = 180.0
# Long enough for uvicorn to flush the response and the phone to receive it,
# short enough that nobody thinks the button did nothing.
SELF_ACTION_DELAY_S = 1.0
# The hub's unit holds "started" until its gateway answers /healthz, which takes up to
# about 40 s after a stop of up to 15 s. A blocking `systemctl restart` would outlast
# ACTION_TIMEOUT_S and report a failure for a restart that then succeeds, so the job is
# sent with --no-block and the admin polls for this long instead.
HUB_START_WAIT_S = 90.0
HUB_POLL_S = 2.0
# The way back to the host app, relative to the install root the service runs in.
HUB_STACK_SCRIPT = "deploy/robot/hub-stack.sh"
# The way back runs in this transient unit, and it keeps its outcome in this file.
HUB_STACK_UNIT = "emori-hub-stack.service"
HUB_STACK_RESULT = "artifacts/last-hub-stack.env"
HUB_STACK_TARGET = "artifacts/hub-stack.target"
# The motor container's compose settings. When EMORI_MOTOR_LAUNCH there names the launch file
# with the robot node alone, the graph lives in the hub, and a way back to the host app has
# to recreate the motor container too.
HUB_GRAPH_ENV_FILE = "deploy/ros2/.env"
HUB_GRAPH_MOTOR_LAUNCH = "robot.launch.py"

_sleep = time.sleep
_monotonic = time.monotonic


def unit_allowed(unit: object) -> bool:
    return isinstance(unit, str) and unit in ALLOWED_UNITS


def hub_flag_path() -> str:
    return os.environ.get(HUB_FLAG_ENV, "").strip() or HUB_FLAG_DEFAULT


def hub_mode() -> bool:
    """True while the flag file says the hub, not the host app, runs the gateway."""

    return os.path.exists(hub_flag_path())


def host_unit() -> str:
    """The unit that runs the kiosk's gateway on this robot right now."""

    return HUB_UNIT if hub_mode() else HOSTAPP_UNIT


def listed_units() -> tuple[str, ...]:
    """The allowlisted units that can run here, without the other gateway's unit.

    The unit its condition skips is inactive on purpose; listing it would show a
    healthy robot with a stopped service.
    """

    other = HOSTAPP_UNIT if hub_mode() else HUB_UNIT
    return tuple(unit for unit in ALLOWED_UNITS if unit != other)


def hub_graph_on(directory: Optional[str] = None) -> bool:
    """Whether the motor container runs with the graph moved into the hub.

    Read from the motor's own env file, the file the rollback script changes.
    """

    path = os.path.join(directory or os.getcwd(), HUB_GRAPH_ENV_FILE)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read(65536).splitlines()
    except OSError:
        return False
    value = ""
    for line in lines:
        key, sep, rest = line.strip().partition("=")
        if sep and key.strip() == "EMORI_MOTOR_LAUNCH":
            value = rest.strip().strip("\"'")
    return value == HUB_GRAPH_MOTOR_LAUNCH


def hub_state() -> dict:
    """What the Services tab needs to decide about the way back to the host app."""

    on = hub_mode()
    state = {"hub_mode": on, "hub_graph": hub_graph_on() if on else False}
    # The way back lowers the flag first and with the motor takes minutes after that, so how it
    # is going has to be readable with the flag down as well. A robot that never switched has
    # no result file and nothing to report.
    if on or os.path.exists(os.path.join(os.getcwd(), HUB_STACK_RESULT)):
        state["hub_switch"] = _hub_switch()
    return state


def _hub_switch() -> dict:
    """Whether the way back is running, and how the last run ended.

    The script goes both ways and each run says which, since one going to the hub is none of
    this, and is reported only so that a hub still coming up in it is not offered as failing.
    ``stuck`` is a way back that stopped short, with the flag down: the host app does not answer,
    maybe with the hub's container on the port, or it does and the motor's graph did not come
    back. A further run finishes it. Exit 7 means this whichever way the run set out, since a
    switch to the hub that fails takes the same way back. A run going back that met a session or
    was refused has not finished either, so that does not close the way.
    """

    running = _unit_facts(HUB_STACK_UNIT)[0] in ("active", "activating")
    try:
        with open(os.path.join(os.getcwd(), HUB_STACK_TARGET), encoding="utf-8") as handle:
            target = handle.read().strip()
    except OSError:
        target = ""
    facts: dict[str, str] = {}
    try:
        with open(os.path.join(os.getcwd(), HUB_STACK_RESULT), encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep:
                    facts[key] = value
    except OSError:
        pass
    last_exit, last_target = facts.get("exit", ""), facts.get("target", "")
    stopped_short = ((last_exit == "7" and bool(last_target))
                     or (last_exit in ("3", "5") and last_target == "hostapp"))
    return {"running": running and target == "hostapp",
            "to_hub": running and target == "ros",
            "stuck": (not hub_mode() and stopped_short
                      and (facts.get("reason") == "nodes_missing" or not _host_app_answers())),
            "reason": facts.get("reason", ""), "exit": facts.get("exit", "")}


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------
def _show_blocks(stdout: str) -> list[dict]:
    """Parse ``systemctl show a b c -p ...`` output into one dict per unit.

    systemd separates the property block of each unit with an empty line; a
    value may itself contain ``=``, so only the FIRST one splits.
    """

    blocks: list[dict] = []
    current: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def services() -> dict:
    """One ``systemctl show`` for every allowlisted unit.

    One subprocess, not five: the Home tab polls this every few seconds and a
    CM4 that is also running a session cannot spare five process spawns a poll.
    A unit that does not exist on this machine still comes back (systemd
    reports ``ActiveState=inactive``), which is the honest answer for a robot
    where the camera bridge was never installed.
    """

    units = listed_units()
    result = probes.run(["systemctl", "show", *units, "--no-pager",
                         "--property=Id,ActiveState,SubState,ActiveEnterTimestamp"])
    if not result.ok:
        return {"ok": False, "error": result.error or "systemctl_failed",
                "services": []}
    by_id = {}
    for block in _show_blocks(result.stdout):
        unit_id = block.get("Id", "")
        if unit_id:
            by_id[unit_id] = block
    rows = []
    for unit in units:
        block = by_id.get(unit, {})
        rows.append({
            "unit": unit,
            "active": block.get("ActiveState") or "unknown",
            "sub": block.get("SubState") or "",
            # systemd prints an empty string for a unit that never started;
            # keep it null rather than inventing a timestamp.
            "since": block.get("ActiveEnterTimestamp") or None,
        })
    return {"ok": True, "services": rows, **hub_state()}


def _deferred(argv: list[str], *, delay_s: Optional[float] = None) -> None:
    """Run ``argv`` a moment from now, off the request thread.

    Used only for commands that kill this process (self-restart, reboot,
    poweroff). A daemon thread on purpose: if systemd tears us down first,
    nothing here needs to survive.

    ``probes.run`` is captured HERE, not inside the thread. The difference
    matters in a test suite -- which may well be running ON the robot: a thread
    that resolves the name a second later would resolve it after a monkeypatch
    has been unwound and would then really power the robot off.
    """

    runner = probes.run
    delay = SELF_ACTION_DELAY_S if delay_s is None else float(delay_s)

    def later() -> None:
        time.sleep(delay)
        runner(argv, timeout_s=probes.ACTION_TIMEOUT_S)

    threading.Thread(target=later, name="admin-deferred-action", daemon=True).start()


def service_action(unit: str, action: str) -> dict:
    """``sudo systemctl <action> <unit>`` for an allowlisted pair only."""

    if not unit_allowed(unit):
        return {"ok": False, "error": "unit_not_allowed"}
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "error": "action_not_allowed"}
    if unit == SELF_UNIT and action != "restart":
        # Stopping ourselves from our own UI is a one-way door on a screenless
        # robot: nothing else can bring the service back.
        return {"ok": False, "error": "self_stop_refused"}
    if unit == HUB_UNIT:
        return _hub_action(action)
    argv = ["sudo", "systemctl", action, unit]
    if unit == SELF_UNIT:
        _deferred(argv)
        return {"ok": True, "unit": unit, "action": action, "deferred": True}
    result = probes.run(argv, timeout_s=probes.ACTION_TIMEOUT_S)
    if not result.ok:
        # systemctl's stderr is the actually useful line here ("Failed to
        # restart X: Unit not found"), and it contains no request data.
        return {"ok": False, "error": result.error or "systemctl_failed",
                "detail": (result.stderr or "").strip()[:400]}
    return {"ok": True, "unit": unit, "action": action, "deferred": False}


def _unit_facts(unit: str) -> tuple[str, str]:
    """``(ActiveState, ActiveEnterTimestamp)`` of one unit; empty strings when unknown."""

    result = probes.run(["systemctl", "show", unit, "--no-pager",
                         "--property=ActiveState,ActiveEnterTimestamp"])
    if not result.ok:
        return "", ""
    blocks = _show_blocks(result.stdout)
    block = blocks[0] if blocks else {}
    return block.get("ActiveState", ""), block.get("ActiveEnterTimestamp", "")


def _gateway_answers() -> bool:
    return probes.hostapp("GET", "/healthz", timeout_s=probes.CMD_TIMEOUT_S).ok


def _hub_action(action: str) -> dict:
    """``systemctl --no-block`` for the hub unit, then wait for its gateway to answer.

    The sudoers grant lists exactly this form. A start or restart is done when the unit is
    active again with a new start time and ``/healthz`` answers; a stop is done when
    systemd has queued it.
    """

    state_before, stamp_before = _unit_facts(HUB_UNIT)
    argv = ["sudo", "systemctl", "--no-block", action, HUB_UNIT]
    result = probes.run(argv, timeout_s=probes.ACTION_TIMEOUT_S)
    if not result.ok:
        return {"ok": False, "error": result.error or "systemctl_failed",
                "detail": (result.stderr or "").strip()[:400]}
    outcome = {"ok": True, "unit": HUB_UNIT, "action": action, "deferred": False}
    if action == "stop" or (action == "start" and state_before == "active"):
        return outcome
    deadline = _monotonic() + HUB_START_WAIT_S
    while True:
        state, stamp = _unit_facts(HUB_UNIT)
        if state == "active" and stamp != stamp_before and _gateway_answers():
            return outcome
        if _monotonic() >= deadline:
            return {"ok": False, "error": "hub_not_ready",
                    "detail": "the hub did not answer /healthz within %d s"
                              % int(HUB_START_WAIT_S)}
        _sleep(HUB_POLL_S)


def _host_app_answers() -> bool:
    """The port answers, and not as the hub. The unit cannot say this: one that loses the port to
    a hub's container restarts every few seconds and reads as running between the restarts."""

    answer = probes.hostapp("GET", "/healthz", timeout_s=probes.CMD_TIMEOUT_S)
    return (answer.ok and isinstance(answer.payload, dict)
            and answer.payload.get("runtime") != "ros")


def hub_rollback() -> dict:
    """Run the way back to the host app, for a robot whose hub is failing.

    Refused unless the flag is up and the hub's gateway does not answer: a healthy hub
    is not rolled back from a phone. The script detaches itself, so an ssh or phone
    drop cannot strand it half way, and it refuses a robot with a session in progress.
    It takes minutes with the motor, so this only starts it: ``hub_state`` says how it went.

    A way back that stopped short may be run again with the flag down, and then whatever
    answers may be the hub's container that has to go.
    """

    on = hub_mode()
    stuck = not on and _hub_switch()["stuck"]
    if not on and not stuck:
        return {"ok": False, "error": "not_hub_mode"}
    if not stuck and _gateway_answers():
        return {"ok": False, "error": "hub_healthy"}
    argv = ["bash", os.path.join(os.getcwd(), HUB_STACK_SCRIPT), "apply", "hostapp"]
    # A run that stopped short may have written the settings back before the motor moved, so they
    # cannot say where the graph is. The script reads the running container, and ignores the flag
    # for a motor that needs nothing.
    with_motor = stuck or hub_graph_on()
    if with_motor:
        argv.append("--with-motor")
    argv.append("--no-wait")
    result = probes.run(argv, timeout_s=probes.ACTION_TIMEOUT_S)
    if not result.ok:
        return {"ok": False, "error": result.error or "rollback_failed",
                "detail": (result.stdout or result.stderr or "").strip()[-400:]}
    return {"ok": True, "with_motor": with_motor}


def power(action: str) -> dict:
    """``sudo systemctl reboot|poweroff``, deferred so the answer gets out."""

    if action not in ("reboot", "poweroff"):
        return {"ok": False, "error": "action_not_allowed"}
    log.warning("admin requested %s", action)
    _deferred(["sudo", "systemctl", action])
    return {"ok": True, "action": action, "deferred": True}


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
def clamp_lines(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return LOG_LINES_DEFAULT
    return max(1, min(LOG_LINES_MAX, number))


def logs(unit: str, lines: object = LOG_LINES_DEFAULT) -> dict:
    if not unit_allowed(unit):
        return {"ok": False, "error": "unit_not_allowed"}
    count = clamp_lines(lines)
    # No sudo: SupplementaryGroups=systemd-journal on the unit is what makes
    # this readable, and a journal read must never need root.
    result = probes.run(["journalctl", "-u", unit, "-n", str(count), "--no-pager",
                         "-o", "short-iso"], timeout_s=probes.ACTION_TIMEOUT_S)
    if not result.ok:
        return {"ok": False, "error": result.error or "journalctl_failed",
                "lines": []}
    return {"ok": True, "unit": unit, "lines": result.lines}


# --------------------------------------------------------------------------
# machine facts
# --------------------------------------------------------------------------
_cpu = probes.CpuSampler()


def _uptime_s() -> Optional[float]:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return round(float(fh.readline().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None  # not Linux


def _mem_pct() -> Optional[float]:
    """Used share of RAM, from MemAvailable (not MemFree — page cache is free)."""

    values: dict[str, float] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        values[key.strip()] = float(parts[0])
                    except ValueError:
                        continue
    except OSError:
        return None
    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable")
    if total <= 0 or available is None:
        return None
    return round(100.0 * (1.0 - available / total), 1)


def _disk_pct() -> Optional[float]:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return round(100.0 * usage.used / usage.total, 1)


def _temp_c() -> Optional[float]:
    """SoC temperature from sysfs.  ``None`` off a Pi (and on Windows)."""

    for zone in range(4):
        path = "/sys/class/thermal/thermal_zone%d/temp" % zone
        try:
            with open(path, "r", encoding="utf-8") as fh:
                millidegrees = float(fh.read().strip())
        except (OSError, ValueError):
            continue
        # Some zones report degrees already; anything above 200 must be milli.
        return round(millidegrees / 1000.0 if millidegrees > 200 else millidegrees, 1)
    return None


def _loadavg() -> Optional[list[float]]:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):  # AttributeError: Windows
        return None


# Written by deploy/robot/one-click-deploy.sh ("<sha> (<branch>)") BEFORE it restarts
# anything; the install root is this service's working directory (WorkingDirectory=).
DEPLOYED_MARKER = "DEPLOYED_COMMIT.txt"


def deployed_marker(directory: Optional[str] = None) -> str:
    """The first line of ``DEPLOYED_COMMIT.txt`` (``""`` when there is none).

    emori-admin reads it once, at process start, and ``/healthz/deep`` reports that
    value: readycheck compares it with the file as it is NOW, and a difference is an
    emori-admin that kept running across a deploy (``admin_stale``). readycheck reads
    the file the same way (``hostapp/readycheck.py`` ``_marker_line``; a test holds the
    two together).
    """

    try:
        with open(os.path.join(directory or os.getcwd(), DEPLOYED_MARKER), "r",
                  encoding="utf-8", errors="replace") as fh:
            text = fh.read(4096)
    except OSError:
        return ""
    lines = text.strip().splitlines()
    return lines[0].strip()[:200] if lines else ""


def deployed_commit() -> str:
    """What is actually running, for the System tab and for bug reports.

    ``git describe`` first (the deploy is a checkout at /opt/emori), then the
    marker file the deploy script writes — a robot where git is absent or the
    tree is owned by root still has to be able to answer this.
    """

    result = probes.run(["git", "-C", os.getcwd(), "describe", "--always", "--dirty"])
    if result.ok and result.stdout.strip():
        return result.stdout.strip()
    return deployed_marker()


def info() -> dict:
    """Everything the System tab shows.  Every field independently optional."""

    return {
        "ok": True,
        "uptime_s": _uptime_s(),
        "cpu_pct": _cpu.sample(),
        "mem_pct": _mem_pct(),
        "disk_pct": _disk_pct(),
        "temp_c": _temp_c(),
        "loadavg": _loadavg(),
        "commit": deployed_commit(),
        "python": sys.version.split()[0],
        "hostname": _hostname(),
        # WHICH robot (every fresh one is hostname reachy-mini): the board's serial and
        # Wi-Fi MAC, and the board install.sh recorded when it differs (a moved card).
        **robot_identity_fields(),
    }


def robot_identity_fields() -> dict:
    from emori_adapter import robot_identity  # noqa: PLC0415 - stdlib only

    serial = robot_identity.hardware_serial()
    recorded = robot_identity.read_record(
        os.path.join(os.getcwd(), robot_identity.IDENTITY_FILE)).get("serial", "")
    fields = {"robot_serial": serial or recorded or None,
              "robot_wlan_mac": robot_identity.wlan_mac() or None}
    if serial and recorded and recorded != serial:
        fields["recorded_serial"] = f"{recorded} (this card was installed on another robot)"
    return fields


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # a label must never break the page
        return ""


# --------------------------------------------------------------------------
# readycheck (single-flight, in a thread)
# --------------------------------------------------------------------------
class ReadycheckRunner:
    """Runs ``emori_adapter.hostapp.readycheck --json --no-speak`` at most once.

    Single-flight because the check itself is not free (it walks the Pokamind
    session chain and probes the daemon), and two operators tapping the button
    on two phones must not put two of them on a CM4 that is also running a
    session.

    ``sys.executable`` and not a hardcoded ``/opt/emori/.venv/bin/python``: on
    the robot the admin service IS that interpreter, and on a dev box the
    hardcoded path does not exist.
    """

    def __init__(self, *, timeout_s: float = READYCHECK_TIMEOUT_S) -> None:
        self._timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._running = False
        self._started_at: Optional[float] = None
        self._result: Optional[dict] = None
        self._error: str = ""

    def status(self) -> dict:
        with self._lock:
            return {"running": self._running, "started_at": self._started_at,
                    "result": self._result, "error": self._error or None}

    def start(self) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "already_running",
                        "started_at": self._started_at}
            self._running = True
            self._started_at = time.time()
            self._result = None
            self._error = ""
        threading.Thread(target=self._run, name="admin-readycheck",
                         daemon=True).start()
        return {"ok": True, "running": True, "started_at": self._started_at}

    def _run(self) -> None:
        argv = [sys.executable, "-m", "emori_adapter.hostapp.readycheck",
                "--json", "--no-speak"]
        result = probes.run(argv, timeout_s=self._timeout_s)
        parsed, error = self._parse(result)
        with self._lock:
            self._running = False
            self._result = parsed
            self._error = error

    @staticmethod
    def _parse(result: probes.Cmd) -> tuple[Optional[dict], str]:
        """``--json`` prints a LIST of gate dicts; wrap it with its exit code.

        A non-zero exit is NOT an error here — readycheck exits 1 precisely
        when a gate FAILs, which is the answer the operator asked for.  Only an
        unparseable stdout (crash, missing module, timeout) is an error.
        """

        if not result.ran:
            return None, result.error or "readycheck_unavailable"
        try:
            gates = json.loads(result.stdout or "")
        except (TypeError, ValueError):
            return None, "readycheck_output_unparseable"
        if not isinstance(gates, list):
            return None, "readycheck_output_unparseable"
        counts: dict[str, int] = {}
        for gate in gates:
            if isinstance(gate, dict):
                counts[str(gate.get("status", "?"))] = (
                    counts.get(str(gate.get("status", "?")), 0) + 1)
        return {"gates": gates, "exit_code": result.returncode,
                "counts": counts}, ""


def default_runner_argv() -> list[str]:
    """Exposed for the tests that pin the interpreter resolution rule."""

    return [sys.executable, "-m", "emori_adapter.hostapp.readycheck",
            "--json", "--no-speak"]


__all__ = [
    "ALLOWED_ACTIONS", "ALLOWED_UNITS", "SELF_UNIT", "ReadycheckRunner",
    "HOSTAPP_UNIT", "HUB_UNIT", "clamp_lines", "default_runner_argv", "deployed_commit",
    "host_unit", "hub_mode", "hub_rollback", "info", "listed_units", "logs",
    "power", "service_action", "services", "unit_allowed",
]
