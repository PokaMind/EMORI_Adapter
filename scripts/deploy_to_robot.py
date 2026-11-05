#!/usr/bin/env python3
"""One-click deploy: this checkout -> the Reachy Mini on the same Wi-Fi.

Double-click ``Deploy-to-Robot.cmd`` (Windows) or ``Deploy-to-Robot.command`` (macOS,
Linux) in the repository root; both run this file. From a terminal::

    python scripts/deploy_to_robot.py                     # find, ship, install, check
    python scripts/deploy_to_robot.py --host 192.0.2.10   # skip the search
    python scripts/deploy_to_robot.py --quick             # code only + restart, no apt/pip
    python scripts/deploy_to_robot.py --no-addons         # do not copy addons/ this time
    python scripts/deploy_to_robot.py --remove-addons     # take every add-on off the robot
    python scripts/deploy_to_robot.py --find              # only say where the robot is
    python scripts/deploy_to_robot.py --env robot.env     # replace the robot's .env
    python scripts/deploy_to_robot.py --replace-token     # give it a new Pokamind token
    python scripts/deploy_to_robot.py --v1                # keep Pollen's daemon (no v2)
    python scripts/deploy_to_robot.py --no-hub            # stay on the host app this time
    python scripts/deploy_to_robot.py --host reachy-01 --jump me@bastion  # from afar

In order, it:

1. finds the robot: the host that worked last time, ``reachy-mini.local``, the robot's
   own hotspot (10.42.0.1), then every /24 this computer is on (a Windows Mobile Hotspot
   included). A host counts as a robot when it answers like one on :8000 (Pollen's
   daemon and the v2 shim both serve ``/api/daemon/status``), :8070 (emori-admin) or
   :8090 (the hostapp);
2. makes SSH passwordless with a dedicated key, installed once. That is the only time
   the robot's password is asked for;
3. ships ``git archive HEAD`` into /opt/emori over SSH, which is how this robot has always
   been deployed (LF-only per .gitattributes). Robot-local files (.env, .venv, voice
   models, roster) are not in the archive and are left alone. Each optional add-on in
   this checkout (``addons/<name>/pyproject.toml``; the folder is not part of the
   repository) is shipped as well, to /opt/emori/addons/<name>, and installed by
   install.sh. A checkout without any, or ``--no-addons``, copies none and leaves an add-on
   already on the robot installed; ``--remove-addons`` takes those off;
4. gives a robot with no /opt/emori/.env one: ``robot.local.env`` from the repository
   root if it exists, else .env.example plus two questions (the Pokamind server, which
   defaults to the public one, and the admin PIN, generated when left empty). Nothing
   else is asked: the robot configures itself from its Pokamind token;
5. installs THIS robot's own Pokamind token (``pkr1.<robot-id>.<key>``, issued per robot
   by Pokamind -- docs/deploy-a-new-robot.md) into /opt/emori/secrets/pokamind_robot_token
   over SSH stdin, never on a command line and never into the shared robot.local.env,
   and only after the robot has asked Pokamind about it (deploy/robot/install-token.sh):
   a token Pokamind refuses never replaces the one the robot has. It is asked for only
   when the robot has none yet (``--replace-token`` to change it, ``--token-file`` to
   read it from a file instead of the keyboard);
6. gives a v2 robot its ROS 2 image when it has none. v2 -- the robot as a ROS 2 node,
   Pollen's daemon fenced off the motor bus (deploy/robot/ros2_native/README.md) -- is
   what a robot runs unless its .env says ``EMORI_ROS2_STACK=v1`` (``--v1``/``--v2``
   write it). The image is built HERE, for arm64, from this checkout (Docker with buildx;
   about 45 minutes, asked first) and pulled by the robot from a registry on this
   computer through an SSH reverse tunnel. Without Docker here the robot stays on v1;
7. runs ``deploy/robot/one-click-deploy.sh`` on the robot: install.sh, the ROS 2 stack
   (deploy/robot/ros2_native/v2-stack.sh), then readycheck;
8. puts a v2 robot on the ROS 2 hub: brings the motor image up to this checkout with one
   small layer, builds the hub image on the robot, and switches the robot to it
   (deploy/robot/hub-stack.sh), with the host app left installed as the way back. It never
   interrupts a session. Both builds need Docker here; without it the robot stays as it is,
   and the last screen says so. ``--no-hub`` keeps a robot on the host app;
9. opens the robot's admin page.

Standard library only, because a double-click may find nothing but a bare Python.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import getpass
import hashlib
import io
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/emori"
REMOTE_FILES_LIST = "/tmp/emori-deploy.files"
DEFAULT_USER = "pollen"
DEFAULT_HOSTS = ("reachy-mini.local", "10.42.0.1")
# The robot's three HTTP faces, in the order they are worth asking.
ROBOT_PORTS = (8000, 8070, 8090)
LOCAL_ENV = REPO / "robot.local.env"  # gitignored via *.local.env
STATE_FILE = Path.home() / ".emori" / "deploy.json"
KEY_FILE = Path.home() / ".ssh" / "emori_robot_ed25519"
# Handed to ssh with its own tilde so a profile path with a space in it (a Windows user
# folder named "Jane Doe") never reaches ssh's whitespace-separated UserKnownHostsFile list.
KNOWN_HOSTS = "~/.ssh/emori_known_hosts"
MAX_SCAN_NETWORKS = 6
# The Pokamind API origin every robot uses unless told otherwise. The same literal as
# site_settings.DEFAULT_POKAMIND_BASE, repeated because this script is stdlib-only
# (tests/test_deploy_to_robot.py pins the two together).
DEFAULT_POKAMIND_BASE = "https://api.pokamind.com"
# The Pokamind server is not asked: the public one is the robot's default, and another
# origin is set with --env (or later on the admin page, with the PIN).
ENV_QUESTIONS = (
    ("EMORI_ADMIN_PIN", "Admin PIN, 6 digits (empty = generate one)", "", True),
)
# Keys an older .env template or robot.local.env may still carry, which this script
# never ADDS to a .env: retired credentials this release no longer reads (the robot
# signs only with its own Pokamind token) and the partner, a protocol constant.
RETIRED_ENV_KEYS = frozenset({"EMORI_PARTNER_SECRET", "EMORI_ROBOT_SECRET",
                              "EMORI_PARTNER_NAME"})
# THIS robot's Pokamind token: where it lives on the robot (relative to REMOTE_ROOT, the
# hostapp's EMORI_POKAMIND_TOKEN_FILE default) and what one looks like. The same grammar
# as src/emori_adapter/robot_token.py, repeated here because this script is stdlib-only.
TOKEN_REMOTE_FILE = "secrets/pokamind_robot_token"
_TOKEN_RE = re.compile(r"pkr1\.([a-z0-9][a-z0-9-]{1,61}[a-z0-9])\.([A-Za-z0-9_-]{43})")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")

# LAN addresses must never go through a corporate HTTP proxy.
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DeployError(Exception):
    """A stop with a message the person at the keyboard can act on."""


def say(message: str = "") -> None:
    print(message, flush=True)


def step(title: str) -> None:
    say(f"\n=== {title} ===")


# What to do when a question cannot be asked: every question has a command-line answer.
NO_KEYBOARD_HINT = (
    "run it again in a window you can type in, or answer on the command line: "
    "--host <robot IP or name> for the robot (and --token-file FILE or --no-token for "
    "the Pokamind token, --env FILE for the robot's settings, --yes to skip the last "
    "confirmation)"
)


def _has_keyboard() -> bool:
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
    except (ValueError, OSError):  # a CLOSED stdin raises instead of answering False
        return False
    return _is_console(sys.stdin) if os.name == "nt" else True


def _is_console(stream) -> bool:
    """Whether a Windows stdin is a real console someone can type in.

    Windows calls the NUL device a tty (``< /dev/null`` under Git Bash, ``< NUL`` in
    cmd), and getpass reads the console directly, so a scripted deploy fed NUL waited
    for a key nobody could press. Only a real console answers GetConsoleMode.
    """

    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (AttributeError, ImportError, OSError, ValueError):
        return False


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    question = prompt.strip()
    if not _has_keyboard():
        raise DeployError(f"'{question}' needs an answer, and there is no keyboard "
                          f"attached: {NO_KEYBOARD_HINT}")
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    try:
        answer = reader(f"{prompt}{suffix}: ").strip()
    except (EOFError, OSError, ValueError):
        # A console whose input is closed still says isatty(); input() then raises
        # EOFError, which must end the deploy in a sentence and not a traceback.
        raise DeployError(f"'{question}' needs an answer, but the keyboard input is "
                          f"closed: {NO_KEYBOARD_HINT}") from None
    return answer or default


def confirm(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer.startswith("y")


# --- finding the robot ----------------------------------------------------------------


def _http_json(url: str, timeout: float):
    with _HTTP.open(url, timeout=timeout) as response:
        return json.loads(response.read(65536).decode("utf-8", "replace"))


def identify(host: str, ports=ROBOT_PORTS, timeout: float = 2.0) -> str | None:
    """What answers on ``host``, if it is a robot; ``None`` if it is not one.

    ``ports`` are where the daemon, emori-admin and hostapp faces listen (the tests move
    them). A developer's PC running the simulator answers on :8000 and :8090 too; the
    daemon status says so (``simulation_enabled``/``mockup_sim_enabled``, which the v2
    shim mirrors), so a simulator is never taken for the robot."""

    daemon_port, admin_port, hostapp_port = ports
    daemon = _get_dict(f"http://{host}:{daemon_port}/api/daemon/status", timeout)
    if "state" in daemon:
        if daemon.get("simulation_enabled") or daemon.get("mockup_sim_enabled"):
            return None
        name = daemon.get("robot_name") or "robot"
        if daemon.get("backend") == "reachy_mini_ros2":
            return f"{name} (v2 ROS node)"
        return f"{name} (Pollen daemon)"
    if _get_dict(f"http://{host}:{admin_port}/healthz", timeout).get("service") == "emori-admin":
        return "robot (EMORI admin)"
    if _get_dict(f"http://{host}:{hostapp_port}/healthz", timeout).get("status") == "ok":
        return "robot (EMORI hostapp)"
    return None


def _get_dict(url: str, timeout: float) -> dict:
    """The JSON object at ``url``, or ``{}`` for anything that is not one."""

    try:
        body = _http_json(url, timeout)
    except Exception:  # noqa: BLE001 - refused, timed out, not HTTP, not JSON: all a no
        return {}
    return body if isinstance(body, dict) else {}


_IPV4 = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
# RFC 1918 only. `is_private` also admits 240.0.0.0/4, i.e. every netmask in a listing.
_LAN = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def extract_ipv4s(text: str) -> list[str]:
    """LAN addresses mentioned in ``ipconfig``/``ip``/``ifconfig`` output. Gateways come
    along too, harmlessly: they fall in the same /24 as the interface."""

    found = []
    for raw in _IPV4.findall(text):
        try:
            address = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        if (any(address in lan for lan in _LAN) and not raw.endswith((".0", ".255"))
                and raw not in found):
            found.append(raw)
    return found


def _primary_ipv4() -> str | None:
    """The address this computer would use to reach the internet (no packet is sent)."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def local_ipv4s() -> list[str]:
    # Filtered like the listing: a machine with a public address must never scan
    # somebody else's /24.
    addresses: list[str] = extract_ipv4s(_primary_ipv4() or "")
    if os.name == "nt":
        command = ["ipconfig"]
    elif shutil.which("ip"):
        command = ["ip", "-4", "-o", "addr"]
    else:
        command = ["ifconfig"]
    try:
        listing = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                 errors="replace", check=False).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""
    for address in extract_ipv4s(listing):
        if address not in addresses:
            addresses.append(address)
    return addresses


def networks_to_scan(addresses: list[str],
                     limit: int = MAX_SCAN_NETWORKS) -> list[ipaddress.IPv4Network]:
    """One /24 per local address, the primary interface first, then a Windows Mobile
    Hotspot's (a robot joined to this PC's hotspot lives there), then the rest."""

    networks: list[ipaddress.IPv4Network] = []
    for address in addresses:
        network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if network not in networks:
            networks.append(network)
    hotspot = ipaddress.IPv4Network("192.168.137.0/24")
    if hotspot in networks[1:]:
        networks.remove(hotspot)
        networks.insert(1, hotspot)
    return networks[:limit]


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(networks, own: set[str]) -> list[tuple[str, str]]:
    hosts = [str(h) for network in networks for h in network.hosts() if str(h) not in own]
    if not hosts:
        return []
    say(f"  scanning {len(networks)} network(s): {', '.join(str(n) for n in networks)}")

    def check(host: str):
        # SSH first: a robot without it cannot be deployed to anyway, and one connect per
        # address keeps a silent /24 to about a second.
        if _port_open(host, 22, 0.6):
            kind = identify(host, timeout=1.5)
            if kind:
                return host, kind
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        return [hit for hit in pool.map(check, hosts) if hit]


def _resolves(host: str, timeout: float = 4.0) -> bool:
    """getaddrinfo on a .local name can hang for a long time with no mDNS responder."""

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(socket.getaddrinfo, host, 22)
        try:
            future.result(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - timeout or resolution failure alike
            return False


def find_robot(state: dict) -> str:
    step("1/5  finding the robot")
    candidates = []
    for host in (state.get("host"), *DEFAULT_HOSTS):
        if host and host not in candidates:
            candidates.append(host)
    for host in candidates:
        say(f"  trying {host} ...")
        if _resolves(host):
            kind = identify(host)
            if kind:
                say(f"  found {kind} at {host}")
                return host
    addresses = local_ipv4s()
    hits = scan(networks_to_scan(addresses), set(addresses))
    if len(hits) == 1:
        say(f"  found {hits[0][1]} at {hits[0][0]}")
        return hits[0][0]
    if hits:
        say("  more than one robot answered:")
        for number, (host, kind) in enumerate(hits, 1):
            say(f"    {number}. {host}  {kind}")
        choice = ask("  which one? (number)", "1")
        try:
            return hits[int(choice) - 1][0]
        except (ValueError, IndexError) as exc:
            raise DeployError(f"'{choice}' is not one of the numbers above") from exc
    say("  no robot answered on this computer's networks.")
    say("  Check that the robot is switched on and joined to the SAME Wi-Fi as this")
    say("  computer (or that this computer is on the robot's own hotspot).")
    host = ask("  type the robot's IP address or name (empty = give up)")
    if not host:
        raise DeployError("no robot to deploy to")
    return host


# --- SSH ------------------------------------------------------------------------------


# --jump: ssh's ProxyJump, "[user@]host[:port]", comma-separated hops. Nothing that could
# be read as an ssh option or carry a shell word: it goes on ssh's own command line.
_JUMP_HOP = (r"(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?"
             r"(?:[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")
_JUMP = re.compile(rf"{_JUMP_HOP}(?:,{_JUMP_HOP})*")


def parse_port(value: str) -> int:
    """argparse type for --port: an SSH port, 1-65535."""

    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port (1-65535)")
    return port


def parse_jump(value: str) -> str:
    """argparse type for --jump: ``[user@]host[:port]``, as ssh -J takes it."""

    if not _JUMP.fullmatch(value or ""):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a jump host: [user@]host[:port], e.g. me@bastion.example.org")
    return value


class Robot:
    """The robot over SSH. ``port``/``jump`` reach a robot that is not on this
    computer's network (docs/deploy-a-new-robot.md, "Deploying from another network");
    every ssh this script runs -- the key install, the preflight, the token, the
    ``git archive`` stream and the robot half -- is built here, so all of them take
    the same route."""

    def __init__(self, host: str, user: str, *, port: int | None = None,
                 jump: str | None = None) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.jump = jump

    def route(self) -> list[str]:
        """The options that pick the way to the robot (also shown to the person)."""

        route = []
        if self.port and self.port != 22:
            route += ["-p", str(self.port)]
        if self.jump:
            route += ["-J", self.jump]
        return route

    @property
    def known_hosts_name(self) -> str:
        """How ssh files this robot in known_hosts: ``[host]:port`` off port 22."""

        return f"[{self.host}]:{self.port}" if self.port and self.port != 22 else self.host

    def ssh(self, *, batch: bool = True, tty: bool = False) -> list[str]:
        command = ["ssh", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                   "-o", "StrictHostKeyChecking=accept-new",
                   "-o", f"UserKnownHostsFile={KNOWN_HOSTS}"]
        if KEY_FILE.exists():
            command += ["-i", str(KEY_FILE)]
        if batch:
            command += ["-o", "BatchMode=yes"]
        if tty:
            command.append("-t")
        return command + self.route() + [f"{self.user}@{self.host}"]

    def run(self, remote: str, *, stdin: bytes | None = None, batch: bool = True,
            timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(self.ssh(batch=batch) + [remote], input=stdin,
                              capture_output=True, timeout=timeout, check=False)


def _ensure_key() -> None:
    if KEY_FILE.exists():
        return
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    say(f"  creating an SSH key for the robot: {KEY_FILE}")
    computer = re.sub(r"[^A-Za-z0-9._-]", "-", platform.node()) or "computer"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                    f"emori-deploy@{computer}", "-f", str(KEY_FILE)], check=True)


# The hosts an OpenSSH host-key complaint is ABOUT: "Host key for <host> has changed"
# and its own "ssh-keygen -f <file> -R <host>" advice ("[host]:port" off port 22).
_HOST_KEY_NAMES = (
    re.compile(r"Host key for (\S+) has changed"),
    re.compile(r"""-R\s+['"]?([^'"\s]+)"""),
)


def host_key_hosts(error: str) -> set[str]:
    """Every host ssh's ``error`` names as the one whose key changed."""

    return {name for pattern in _HOST_KEY_NAMES for name in pattern.findall(error or "")}


def connect(robot: Robot) -> None:
    step("2/5  connecting (SSH)")
    for tool in ("ssh", "ssh-keygen"):
        if not shutil.which(tool):
            raise DeployError(
                f"'{tool}' is not installed. On Windows: Settings > Apps > Optional features"
                " > add 'OpenSSH Client'.")
    host_key_reset = key_installed = False
    while True:
        result = robot.run("true", timeout=30)
        if result.returncode == 0:
            say(f"  connected as {robot.user}@{robot.host}")
            return
        error = result.stderr.decode("utf-8", "replace")
        if "IDENTIFICATION HAS CHANGED" in error or "Host key verification failed" in error:
            named = host_key_hosts(error)
            if not named & {robot.host, robot.known_hosts_name} and (robot.jump or named):
                # Through --jump there are two SSH connections, and the jump host's changed
                # key reads exactly like the robot's. Forgetting the ROBOT's pinned key
                # for it would be trusting whatever answers next as the robot.
                whose = ", ".join(sorted(named)) or "ssh did not say whose"
                say(f"  !! an SSH host key changed on the way to {robot.host}"
                    + (f", through the jump host {robot.jump}:" if robot.jump else ":"))
                say(f"     it may be the JUMP HOST's key, not the robot's ({whose}).")
                say("     Nothing was removed and nothing was sent. Ask whoever runs that host")
                say("     whether its key changed; if it did, remove its old entry yourself")
                say("     (ssh-keygen -R <that host>) and deploy again.")
                raise DeployError("an SSH host key changed on the way to the robot; "
                                  "nothing was sent")
            say(f"  !! {robot.host} presents a different SSH host key than last time.")
            say("     Expected after the robot was re-flashed, or when a second robot now")
            say("     answers to the same name. Anything else could be someone else's machine.")
            if host_key_reset or not confirm("  forget the old key and trust this robot?",
                                             default=False):
                raise DeployError("SSH host key changed; nothing was sent")
            subprocess.run(["ssh-keygen", "-R", robot.known_hosts_name, "-f",
                            os.path.expanduser(KNOWN_HOSTS)], capture_output=True, check=False)
            host_key_reset = True
            continue
        if "Permission denied" in error and not key_installed:
            _install_key(robot)
            key_installed = True
            continue
        raise DeployError(f"cannot open SSH to {robot.user}@{robot.host}:\n    {error.strip()}")


_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?")


def _install_key(robot: Robot) -> None:
    _ensure_key()
    public = Path(str(KEY_FILE) + ".pub").read_text(encoding="ascii").strip()
    if not _PUBLIC_KEY.fullmatch(public):
        raise DeployError(f"{KEY_FILE}.pub does not look like an ed25519 public key")
    say("  first time on this robot: installing this computer's key so no password is")
    say(f"  needed again. Enter {robot.user}'s password on the robot when asked.")
    # The key travels inside the command (the pattern above admits no quote), leaving
    # stdin to the console, which is where ssh reads the password from on every OS.
    remote = (f"umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
              f"grep -qxF '{public}' ~/.ssh/authorized_keys "
              f"|| echo '{public}' >> ~/.ssh/authorized_keys")
    result = subprocess.run(robot.ssh(batch=False) + [remote], check=False)
    if result.returncode != 0:
        raise DeployError("could not install the SSH key (wrong password?)")


# --- the robot's state ------------------------------------------------------------------

# What the robot's token file holds, WITHOUT ever printing it: token=yes plus the robot
# id for exactly one pkr1 line (surrounding whitespace forgiven, as robot_token.parse_token
# forgives it), token=invalid for anything else, token=no for none. Only a line that
# passed the grammar is ever cut, and with -s: a plain `cut -d. -f2` prints a line with
# no "." unchanged, which for a hand-edited file holding just the 43-character seed is
# the robot's private key on the deploy console.
TOKEN_PEEK = r"""
root=/opt/emori
tf=$(grep -E '^EMORI_POKAMIND_TOKEN_FILE=' "$root/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ')
case "$tf" in ''|*..*) tf=secrets/pokamind_robot_token ;; esac
case "$tf" in /*) ;; *) tf=$root/$tf ;; esac
token_line='[[:space:]]*pkr1\.[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.[A-Za-z0-9_-]{43}[[:space:]]*'
if [ ! -s "$tf" ]; then
  echo token=no
elif [ ! -r "$tf" ]; then
  echo token=yes
elif [ "$(tr -d '\r' < "$tf" | grep -c .)" = 1 ] \
     && tr -d '\r' < "$tf" | grep -Eqx "$token_line"; then
  echo token=yes
  echo "token_robot=$(tr -d '\r' < "$tf" | grep -Ex "$token_line" | cut -s -d. -f2)"
else
  echo token=invalid
fi
"""

PREFLIGHT = r"""
echo "hostname=$(hostname)"
# WHICH robot: every fresh Reachy Mini is hostname reachy-mini. The CM4 serial and the
# Wi-Fi MAC are on the hardware (src/emori_adapter/robot_identity.py reads the same).
serial=$(tr -d '\000' < /proc/device-tree/serial-number 2>/dev/null)
[ -n "$serial" ] || serial=$(awk -F': *' '/^Serial/{print $2; exit}' /proc/cpuinfo 2>/dev/null)
echo "hw_serial=$(printf '%s' "$serial" | tr -cd '0-9a-fA-F' | tr 'A-F' 'a-f' | cut -c1-32)"
for d in /sys/class/net/*; do
  if [ -d "$d/wireless" ]; then
    echo "wlan_mac=$(tr -cd '0-9a-fA-F:' < "$d/address" 2>/dev/null | tr 'A-F' 'a-f')"
    break
  fi
done
echo "recorded_serial=$(sed -n 's/^serial=//p' /opt/emori/ROBOT_IDENTITY 2>/dev/null | head -n1 | tr -cd '0-9a-f')"
[ -d /opt/emori ] && echo emori_dir=yes || echo emori_dir=no
[ -f /opt/emori/.env ] && echo env=yes || echo env=no
""" + TOKEN_PEEK + r"""
echo "env_robot_id=$(grep -E '^EMORI_ROBOT_ID=' /opt/emori/.env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ')"
sudo -n true 2>/dev/null && echo sudo=yes || echo sudo=no
echo "ntp_sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
echo "free_mb=$(df -Pm /opt 2>/dev/null | awk 'NR==2{print $4}')"
[ -e /opt/emori/V2_OWNS_THE_BUS ] && echo v2_fence=yes || echo v2_fence=no
[ -e /opt/emori/HUB_IS_ROS ] && echo hub_flag=yes || echo hub_flag=no
running=$(sudo -n docker inspect -f '{{.State.Running}}' emori-ros2-v2 2>/dev/null)
echo "v2_running=${running:-no}"
echo "stack_env=$(grep -E '^EMORI_ROS2_STACK=' /opt/emori/.env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r" ')"
command -v docker >/dev/null 2>&1 && echo docker=yes || echo docker=no
echo "v2_image=$(sudo -n docker image inspect -f '{{.Id}}|{{index .Config.Labels "org.pokamind.emori.v2-source"}}|{{.Created}}' emori/ros2-jazzy-v2:robot 2>/dev/null)"
echo "motor_source=$(sudo -n docker image inspect -f '{{index .Config.Labels "org.pokamind.emori.motor-source"}}' emori/ros2-jazzy-v2:robot 2>/dev/null)"
echo "hub_image=$(sudo -n docker image inspect -f '{{.Id}}|{{index .Config.Labels "org.pokamind.emori.hub-source"}}|{{.Created}}' emori/hub:robot 2>/dev/null)"
echo "hub_base=$(sudo -n docker image inspect -f '{{.Id}}|{{index .Config.Labels "org.pokamind.emori.hub-base-source"}}|{{.Created}}' emori/hub-base:robot 2>/dev/null)"
sudo -n docker image inspect emori/ros2-jazzy-v2:robot-base >/dev/null 2>&1 && echo v2_overlay=yes || echo v2_overlay=no
curl -fsS -m 6 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null \
  && echo internet=yes || echo internet=no
last=/opt/emori/artifacts/last-deploy.env
[ -f "$last" ] && sed 's/^/last_/' "$last"
true
"""


def parse_kv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key:
            values[key] = value
    return values


# The robot's clock, read on its own so the reading is bracketed by one short SSH round
# trip (the preflight above also waits on a 6 s internet probe).
CLOCK_READ = "date -u +%s.%N"
# The server rejects a request whose timestamp is too far ahead of its own clock. This
# computer is not Pokamind, so this is a warning; readycheck's clock gate measures
# against Pokamind itself.
CLOCK_LIMIT_S = 2.0


def parse_epoch(text: str) -> tuple[float, float] | None:
    """``date +%s.%N`` -> (seconds, resolution); a date without %N support prints
    ``1700000000.N`` (or ``%N``), which is read as whole seconds."""

    match = re.fullmatch(r"\s*(\d{9,12})(?:\.(\d+|N|%N))?\s*", text or "")
    if match is None:
        return None
    whole, fraction = match.group(1), match.group(2) or ""
    if fraction.isdigit():
        return float(f"{whole}.{fraction}"), 0.0
    return float(whole) + 0.5, 0.5


def clock_skew(robot_epoch: tuple[float, float], sent_at: float,
               received_at: float) -> tuple[float, float]:
    """(skew, uncertainty): robot minus this computer, positive = the robot is ahead.

    The robot read its clock somewhere between ``sent_at`` and ``received_at`` on this
    computer's clock; the middle is the estimate and half the gap (plus the robot's own
    resolution) the uncertainty."""

    seconds, resolution = robot_epoch
    return (seconds - (sent_at + received_at) / 2.0,
            (received_at - sent_at) / 2.0 + resolution)


def measure_clock(robot: Robot) -> tuple[float, float] | None:
    sent_at = time.time()
    try:
        result = robot.run(CLOCK_READ, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    received_at = time.time()
    if result.returncode != 0:
        return None
    epoch = parse_epoch(result.stdout.decode("ascii", "replace"))
    return clock_skew(epoch, sent_at, received_at) if epoch else None


def report_clock(info: dict[str, str], measured: tuple[float, float] | None) -> None:
    ntp = info.get("ntp_sync") or "unknown"
    if measured is None:
        say(f"  robot clock: NTP synchronized {ntp}; could not compare it with this computer")
    else:
        skew, uncertainty = measured
        side = "ahead of" if skew >= 0 else "behind"
        say(f"  robot clock: {abs(skew):.1f} s {side} this computer (+/-{uncertainty:.1f} s),"
            f" NTP synchronized {ntp}")
        if abs(skew) - uncertainty > CLOCK_LIMIT_S:
            say(f"  !! the robot's clock is {abs(skew):.0f} s {side} this computer's. Pokamind")
            say(f"     refuses what a robot signs once its clock is more than "
                f"{CLOCK_LIMIT_S:.0f} s ahead (learner")
            say("     logins at 60 s), which looks like a token problem. On the robot:")
            say("     sudo timedatectl set-ntp true   (and let NTP, UDP 123, out of this network)")
            say("     If THIS computer's clock is the wrong one, readycheck's clock gate says so.")
            return
    if ntp == "no":
        say("  !! NTP has not synchronized the robot's clock (a CM4 has no RTC battery, so it")
        say("     is wrong after every power-off until NTP syncs): sudo timedatectl set-ntp true")
        say("     on the robot, and let NTP (UDP 123) out of this network.")


_SERIAL = re.compile(r"[0-9a-f]{8,32}")
_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def hardware_identity(info: dict[str, str]) -> str:
    """``serial 10000000a1b2c3d4, Wi-Fi dc:a6:32:01:02:03`` -- or "" when unknown."""

    parts = []
    serial = info.get("hw_serial", "")
    if _SERIAL.fullmatch(serial) and serial.strip("0"):
        parts.append(f"serial {serial}")
    mac = info.get("wlan_mac", "")
    if _MAC.fullmatch(mac):
        parts.append(f"Wi-Fi {mac}")
    return ", ".join(parts)


def report_identity(info: dict[str, str]) -> None:
    """Which physical robot answered, and which robot it says it is."""

    hardware = hardware_identity(info)
    say(f"  robot hardware: {hardware or 'no serial or Wi-Fi MAC readable'}")
    names = []
    if info.get("env_robot_id"):
        names.append(f"EMORI_ROBOT_ID={info['env_robot_id']}")
    if info.get("token") == "yes" and info.get("token_robot"):
        names.append(f"holds the token for '{info['token_robot']}'")
    say(f"  robot identity: {'; '.join(names) or 'none yet (a fresh robot)'}")
    recorded = info.get("recorded_serial", "")
    serial = info.get("hw_serial", "")
    if _SERIAL.fullmatch(recorded) and _SERIAL.fullmatch(serial) and recorded != serial:
        say(f"  !! EMORI on this card was installed on ANOTHER board (serial {recorded}):")
        say("     the card moved into this robot body, which now logs in as the robot the")
        say("     card was set up for. Make sure that is what you want before deploying.")


def deploy_question(info: dict[str, str], token: str | None) -> str:
    """The last question names the physical robot: a token on the wrong unit logs that
    unit in as another robot, and every fresh one answers to reachy-mini."""

    hardware = hardware_identity(info)
    if not hardware:
        return "  deploy now?"
    if token is not None:
        robot_id = parse_robot_token(token)[0]
        # A robot that already has an id has a label with it on; a fresh one gets its
        # label after this deploy (docs/deploy-a-new-robot.md), so there is none to check.
        known = info.get("env_robot_id") or info.get("token_robot")
        check = " Check its label." if known else ""
        return (f"  is this robot ({hardware}) the one for '{robot_id}'?{check}"
                " Install the token and deploy now?")
    return f"  deploy to this robot ({hardware}) now?"


def preflight(robot: Robot) -> dict[str, str]:
    result = robot.run("bash -s", stdin=PREFLIGHT.encode())
    if result.returncode != 0:
        raise DeployError("could not read the robot's state:\n    "
                          + result.stderr.decode("utf-8", "replace").strip())
    info = parse_kv(result.stdout.decode("utf-8", "replace"))
    say(f"  robot: {info.get('hostname', '?')}   free disk: {info.get('free_mb', '?')} MB"
        f"   internet: {info.get('internet', '?')}")
    report_clock(info, measure_clock(robot))
    report_identity(info)
    if info.get("last_commit"):
        say(f"  running now: {info.get('last_commit', '')[:10]} ({info.get('last_branch', '?')}),"
            f" deployed {info.get('last_at', '?')} from {info.get('last_from', '?')}")
    if info.get("token") == "yes":
        say(f"  Pokamind token: robot '{info.get('token_robot') or '?'}'")
    elif info.get("token") == "invalid":
        say("  Pokamind token: DAMAGED (the file is not one pkr1 line) - give it a new one")
    elif info.get("emori_dir") == "yes":
        say("  Pokamind token: none yet")
    if info.get("sudo") != "yes":
        raise DeployError(
            f"'{robot.user}' has no passwordless sudo on the robot, which the installer needs.\n"
            f"    On the robot: echo '{robot.user} ALL=(ALL) NOPASSWD:ALL' | "
            f"sudo tee /etc/sudoers.d/010_{robot.user}-nopasswd")
    if info.get("v2_running") == "true" and info.get("v2_fence") != "yes":
        raise DeployError(
            "v2 (emori-ros2-v2) is running but the daemon fence is not up, and a deploy would\n"
            "    restart Pollen's daemon onto the same motor bus. See\n"
            "    deploy/robot/ros2_native/README.md ('Committed to v2') and deploy again.")
    try:
        if int(info.get("free_mb", "0")) < 400:
            say("  !! less than 400 MB free on the robot: the install may run out of space")
    except ValueError:
        pass
    return info


# --- THIS robot's Pokamind token -------------------------------------------------------


def parse_robot_token(value: str) -> tuple[str, str]:
    """``(robot_id, token)`` for a valid ``pkr1.<robot_id>.<seed>``; else DeployError.

    The robot re-checks it (hostapp + readycheck), but a token mangled by a copy-paste
    must be refused HERE, with a sentence, before anything is sent. The key part must be
    the canonical base64url of exactly 32 bytes, like the robot requires.
    """

    text = (value or "").strip()
    match = _TOKEN_RE.fullmatch(text)
    if match is None:
        raise DeployError("that is not a Pokamind robot token: it is one line, "
                          "pkr1.<robot-id>.<43 characters>, as Pokamind printed it")
    seed = match.group(2)
    try:
        raw = base64.urlsafe_b64decode(seed + "=")
    except ValueError:
        raw = b""
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != seed:
        raise DeployError("the key part of that token is damaged (a character lost or "
                          "added in the copy-paste?)")
    return match.group(1), text


def mask_token(token: str) -> str:
    """The only form of a token this script ever prints."""

    robot_id, text = parse_robot_token(token)
    return f"pkr1.{robot_id}...{text[-4:]}"


def robot_slug(value: str) -> str:
    """EMORI_ROBOT_ID as the robot normalises it (robot_token.robot_slug)."""

    slug = _SLUG_UNSAFE.sub("-", (value or "").strip().lower()).strip("-._")
    return re.sub(r"-{2,}", "-", slug)


def robot_id_problems(info: dict[str, str], robot_id: str) -> list[str]:
    """Why a token for ``robot_id`` may belong to another robot than this one."""

    problems = []
    env_robot_id = robot_slug(info.get("env_robot_id", ""))
    if env_robot_id and env_robot_id != robot_id:
        problems.append(f"the robot's .env says EMORI_ROBOT_ID={env_robot_id}")
    current = info.get("token_robot") or ""
    if current and current != robot_id:
        problems.append(f"the robot currently holds the token for '{current}'")
    return problems


def choose_token(args, info: dict[str, str], *, interactive: bool) -> str | None:
    """The token to install on this robot now, or None to leave it as it is.

    Per robot, every time: it is never read from or written to robot.local.env (the
    answers file that is reused for the NEXT robot), and it is only asked for when the
    robot has none yet, unless --replace-token.
    """

    current = info.get("token_robot") or ""
    if args.token_file is not None:
        robot_id, token = parse_robot_token(args.token_file.read_text(encoding="utf-8"))
    elif info.get("token") == "yes" and not args.replace_token:
        say(f"  the robot keeps its own Pokamind token (robot '{current or '?'}')")
        return None
    elif args.no_token:
        say("  no Pokamind token this time (--no-token)")
        return None
    elif not interactive:
        if info.get("token") == "invalid":
            say("  !! the robot's Pokamind token file is DAMAGED and there is no keyboard to ask")
            say("     for a new one (pass --token-file FILE, or use the admin page's Replace token)")
            return None
        say("  !! no keyboard to ask for this robot's Pokamind token: deploying without one")
        say("     (pass --token-file FILE, or set it later on the admin page's Pokamind tab)")
        return None
    else:
        say("  Every robot has its OWN Pokamind token, issued for it in Pokamind")
        say("  (docs/deploy-a-new-robot.md, step 1). It starts with pkr1. and is one line.")
        while True:
            answer = ask("  this robot's Pokamind token (pkr1....; empty = none for now)",
                         secret=True)
            if not answer:
                say("  no token for now: the robot runs demo sessions until its token is")
                say("  pasted on its admin page (Pokamind tab), and then configures itself")
                return None
            try:
                robot_id, token = parse_robot_token(answer)
                break
            except DeployError as exc:
                say(f"    {exc}")
    say(f"  token for robot '{robot_id}' ({mask_token(token)})")
    problems = robot_id_problems(info, robot_id)
    if problems:
        say(f"  !! this token is for '{robot_id}', but {' and '.join(problems)}.")
        say("     A token for the wrong robot logs this robot in as THAT robot.")
        if not interactive:
            say("     A token for another robot needs a human yes at a keyboard: run this")
            say("     again in a window you can type in (--token-file and --yes do not")
            say("     answer this question).")
            raise DeployError(f"token for '{robot_id}' not installed (robot id mismatch)")
        if not confirm("  install it anyway?", default=False):
            raise DeployError(f"token for '{robot_id}' not installed (robot id mismatch)")
    return token


# THIS robot's token goes to deploy/robot/install-token.sh, which the code shipped just
# before: on stdin -- never in the command line, which ssh, the remote shell and `ps`
# all get to see -- into a 0600 candidate file, then the robot's own Python asks
# Pokamind about it (the admin page's whoami) and only then moves it into place. So it
# runs AFTER ship(): on a robot that has never had EMORI, the verifier is not there
# before. A robot whose Python cannot ask yet (no venv before install.sh's step 2) keeps
# the candidate staged (--defer), and install.sh asks once it has built one.
TOKEN_SCRIPT = f"{REMOTE_ROOT}/deploy/robot/install-token.sh"
TOKEN_VERDICT_FILE = f"{REMOTE_ROOT}/artifacts/last-token-verdict.env"
_REFUSED_CODES_HINT = {
    "robot_unknown": "is it issued on the Pokamind POKAMIND_BASE points at, and is the id right?",
    "robot_revoked": "ask Pokamind to reactivate it, or to issue a new robot",
    "whoami_auth_failed": "rotated (use the newest token), another backend, or the robot's "
                          "clock is off (see the robot clock line above)",
    "whoami_key_mismatch": "this token was rotated out: use the newest one",
    "robot_id_mismatch": "the robot's .env names another robot (EMORI_ROBOT_ID)",
}


def install_token(robot: Robot, token: str, *, allow_robot_id_change: bool = False,
                  previous: str = "") -> dict[str, str]:
    """Stage, verify and install; the robot-side verdict as parsed key=values.

    A Pokamind refusal is a DeployError (the robot keeps ``previous``, its old token).
    An unanswered question is a loud warning: on a robot with no token the token goes
    in; on one that has a token, that token stays and the new one waits staged on the
    robot (exit 7) for install.sh to ask about again -- never an unchecked token over a
    working one."""

    robot_id, text = parse_robot_token(token)
    remote = f"bash {TOKEN_SCRIPT} --stdin --defer"
    if allow_robot_id_change:
        remote += " --allow-robot-id-change"
    result = robot.run(remote, stdin=(text + "\n").encode("ascii"), timeout=120)
    out = parse_kv(result.stdout.decode("utf-8", "replace"))
    code = out.get("token_code", "unknown")
    if result.returncode == 5:
        keeps = (f"its token for '{previous}'" if previous else "no token (as before)")
        hint = _REFUSED_CODES_HINT.get(code, out.get("token_reason", ""))
        raise DeployError(
            f"Pokamind REFUSED the token for '{robot_id}' ({code}): {hint}\n"
            f"    It was NOT installed; the robot keeps {keeps}. The new code is on the robot,"
            "\n    but nothing was installed or restarted. Deploy again with the right token,"
            "\n    or with --no-token to deploy without changing it.")
    if result.returncode == 6:
        say(f"  token for robot '{robot_id}' staged on the robot (0600): the installer asks")
        say("  Pokamind about it once it has built the robot's Python, and installs it only then")
        return out
    if result.returncode == 7:
        kept = out.get("token_kept") or previous or "?"
        say(f"  !! Pokamind could not be asked about the new token for '{robot_id}' ({code}):")
        say(f"     it was NOT installed. The robot KEEPS its working token for '{kept}'; the new")
        say("     one waits, staged (0600), on the robot. The installer asks Pokamind again; if")
        say("     it still cannot, deploy again once Pokamind is reachable, or paste the new")
        say("     token on the admin page: Pokamind -> Replace token.")
        return out
    if result.returncode != 0:
        raise DeployError("could not install the Pokamind token on the robot:\n    "
                          + result.stderr.decode("utf-8", "replace").strip())
    if out.get("token_verdict") == "verified":
        org = f", org {out['token_org']}" if out.get("token_org") else ""
        say(f"  Pokamind knows robot '{out.get('token_installed', robot_id)}'{org}: token"
            f" installed ({TOKEN_REMOTE_FILE}, 0600)")
        if out.get("token_scope") == "failed":
            say("  !! its Pokamind record (course, languages, menus) could not be written on"
                " the robot:")
            say("     the robot records it by itself about 10 s after it starts (admin page:"
                " Pokamind -> Sync now).")
    else:
        say(f"  !! Pokamind could not be asked about this token ({code}): installed for robot")
        say(f"     '{out.get('token_installed', robot_id)}' WITHOUT its confirmation. Once the"
            " robot is online:")
        say("     admin page -> Pokamind -> Test connection (or readycheck's pokamind_whoami).")
    if out.get("robot_id_set"):
        say(f"  EMORI_ROBOT_ID set to '{out['robot_id_set']}' (it was empty)")
    return out


def staged_token_outcome(robot: Robot) -> str:
    """After install.sh: one line on what became of a token that was staged for it."""

    try:
        result = robot.run(f"cat {TOKEN_VERDICT_FILE} 2>/dev/null; true", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "Pokamind token: could not read what the installer did with it"
    verdict = parse_kv(result.stdout.decode("utf-8", "replace"))
    robot_id = verdict.get("robot", "?")
    code = verdict.get("code", "unknown")
    if verdict.get("verdict") == "verified":
        if verdict.get("scope") == "failed":
            return (f"Pokamind token: verified and installed for robot '{robot_id}', but its "
                    "Pokamind record could not be written yet - the robot records it about "
                    "10 s after it starts (admin page: Pokamind -> Sync now)")
        return f"Pokamind token: verified and installed for robot '{robot_id}'"
    if verdict.get("verdict") == "unverified":
        return (f"Pokamind token: installed for robot '{robot_id}' WITHOUT Pokamind's "
                f"confirmation ({code})")
    if verdict.get("verdict") == "staged":
        return (f"Pokamind token: NOT installed - Pokamind could not be asked about the new "
                f"token for '{robot_id}' ({code}); the robot KEEPS its working token and the "
                "new one waits staged: deploy again once Pokamind is reachable, or paste it "
                "on the admin page (Pokamind -> Replace token)")
    if verdict.get("verdict") == "superseded":
        return (f"Pokamind token: the one staged for '{robot_id}' was dropped - a newer token "
                "was installed on the robot since (admin page)")
    if verdict.get("verdict") == "refused":
        return (f"Pokamind token: REFUSED by Pokamind ({code}) and NOT installed - see the "
                "installer's log above")
    return ("Pokamind token: still staged on the robot (the installer did not reach it); "
            "the next full deploy verifies and installs it")


# --- the robot's .env -------------------------------------------------------------------


def fill_env(template: str, values: dict[str, str]) -> str:
    """``template`` with each ``KEY=`` line set to ``values[KEY]``; keys the template does
    not have are appended, except a retired one (:data:`RETIRED_ENV_KEYS`), which is only
    ever set where the template already has it. LF only: a CRLF .env leaves a ``\\r`` on
    every value."""

    lines = template.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    seen = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        # Every occurrence: with a duplicated key the LAST assignment is the one dotenv keeps.
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            lines[index] = f"{key}={values[key]}"
            seen.add(key)
    text = "\n".join(lines).rstrip("\n") + "\n"
    for key, value in values.items():
        if key not in seen and key not in RETIRED_ENV_KEYS:
            text += f"{key}={value}\n"
    return text


def generate_admin_pin() -> str:
    """Six random digits: the robot's admin PIN when the person at the keyboard gave none."""

    return f"{secrets.randbelow(10 ** 6):06d}"


def _env_from_questions(*, generated: dict[str, str] | None = None,
                        ask_questions: bool = True) -> str:
    """The new robot's .env text: .env.example plus the answers to ENV_QUESTIONS.

    An empty PIN answer generates one (:func:`generate_admin_pin`); it goes into
    ``generated`` so the deploy's final summary shows it once, and never into the
    robot.local.env saved for the NEXT robot, which gets its own. No credential is
    asked here: the robot's only one, its Pokamind token, has its own prompt.
    ``ask_questions=False`` (``--yes``) takes every default without asking.
    """

    template = (REPO / ".env.example").read_text(encoding="utf-8")
    if not ask_questions or not _has_keyboard():
        # Nothing to ask with: every answer has a default (a generated PIN), so a
        # scripted deploy still gives the robot a complete .env.
        pin = generate_admin_pin()
        if generated is not None:
            generated["EMORI_ADMIN_PIN"] = pin
        say("  This robot has no settings file (.env) yet: .env.example with a generated")
        say("  admin PIN (shown once, at the end of this deploy).")
        return fill_env(template, {"EMORI_ADMIN_PIN": pin})
    say("  This robot has no settings file (.env) yet. One question, then the rest")
    say("  comes from .env.example and, once its token is in, from Pokamind. (Put a")
    say("  finished file at robot.local.env in the repository to skip this next time.)")
    values = {}
    for key, prompt, default, secret in ENV_QUESTIONS:
        while True:
            answer = ask(f"  {prompt}", default, secret)
            if key == "EMORI_ADMIN_PIN" and answer and not re.fullmatch(r"\d{6}", answer):
                say("    the PIN must be exactly 6 digits")
                continue
            break
        values[key] = answer
    shared = dict(values)
    if not values.get("EMORI_ADMIN_PIN"):
        values["EMORI_ADMIN_PIN"] = generate_admin_pin()
        if generated is not None:
            generated["EMORI_ADMIN_PIN"] = values["EMORI_ADMIN_PIN"]
        say("  admin PIN: generated (shown once, at the end of this deploy)")
    text = fill_env(template, values)
    if confirm("  save these settings to robot.local.env for the next robot?", default=False):
        LOCAL_ENV.write_text(fill_env(template, shared), encoding="utf-8", newline="\n")
        say(f"  saved {LOCAL_ENV.name} (gitignored; never a Pokamind token, and a generated"
            " PIN stays this robot's)")
    return text


def _refuse_shared_token(text: str, source: str) -> None:
    """A Pokamind token in a .env that travels between robots is one token on N robots."""

    for line in text.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and key.strip() == "POKAMIND_ROBOT_TOKEN" \
                and value.strip():
            raise DeployError(
                f"{source} sets POKAMIND_ROBOT_TOKEN. A Pokamind token belongs to ONE robot "
                "and must never sit in a settings file that is reused: delete that line and "
                "give the token at the prompt (or with --token-file).")


def prepare_env(info: dict[str, str], env_file: Path | None, *,
                generated: dict[str, str] | None = None,
                ask_questions: bool = True) -> str | None:
    """The .env text this deploy will give the robot, or None when it keeps its own.

    Asks every question it needs NOW, before anything is sent to the robot (a cancel
    or a missing keyboard then costs nothing); with ``ask_questions=False`` (``--yes``)
    it asks none and takes the defaults. Values this deploy GENERATES (an admin PIN) go
    into ``generated``, for the final summary to show once.
    """

    if env_file is None and info.get("env") == "yes":
        say("  the robot keeps its own .env (unchanged)")
        return None
    if env_file is not None:
        text = env_file.read_text(encoding="utf-8")
        _refuse_shared_token(text, str(env_file))
        say(f"  uploading {env_file} as the robot's .env (the old one is kept as .env.bak-*)")
    elif LOCAL_ENV.exists():
        text = LOCAL_ENV.read_text(encoding="utf-8")
        _refuse_shared_token(text, LOCAL_ENV.name)
        say(f"  uploading {LOCAL_ENV.name} as the robot's .env")
    else:
        text = _env_from_questions(generated=generated, ask_questions=ask_questions)
    return fill_env(text, {})  # LF-normalise without changing anything else


def upload_env(robot: Robot, text: str | None) -> None:
    """Write :func:`prepare_env`'s text as the robot's .env (the old one kept as
    ``.env.bak-*``); nothing when it keeps its own."""

    if text is None:
        return
    remote = (f"set -e; cd {REMOTE_ROOT}; umask 077; cat > .env.incoming; "
              "[ -f .env ] && cp -p .env .env.bak-$(date +%Y%m%d-%H%M%S); mv -f .env.incoming .env")
    result = robot.run(remote, stdin=text.encode("utf-8"))
    if result.returncode != 0:
        raise DeployError("could not write the robot's .env:\n    "
                          + result.stderr.decode("utf-8", "replace").strip())


def ensure_env(robot: Robot, info: dict[str, str], env_file: Path | None) -> dict[str, str]:
    """:func:`prepare_env` then :func:`upload_env`. Returns the values this deploy
    GENERATED for the robot (an admin PIN), for the final summary to show once."""

    generated: dict[str, str] = {}
    upload_env(robot, prepare_env(info, env_file, generated=generated))
    return generated


READYCHECK_SUMMARY_FILE = f"{REMOTE_ROOT}/artifacts/last-deploy-readycheck.env"


def readycheck_warnings(robot: Robot) -> list[str]:
    """The gates the deploy's readycheck WARNed about (never changes the verdict)."""

    try:
        result = robot.run(f"cat {READYCHECK_SUMMARY_FILE} 2>/dev/null; true", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    gates = parse_kv(result.stdout.decode("utf-8", "replace")).get("warn_gates", "")
    return [gate for gate in gates.split(",") if re.fullmatch(r"[a-z0-9_]{1,64}", gate)]


# --- shipping the code ------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def describe_checkout() -> dict[str, str]:
    if not shutil.which("git"):
        raise DeployError("'git' is not installed (Windows: https://git-scm.com/download/win)")
    try:
        commit = _git("rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:
        raise DeployError(f"{REPO} is not a git checkout") from exc
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    subject = _git("log", "-1", "--format=%s")
    dirty = [line for line in _git("status", "--porcelain", "--untracked-files=no").splitlines()
             if line.strip()]
    return {"commit": commit, "branch": branch, "subject": subject, "dirty": str(len(dirty))}


# What an add-on ships without: its tests, VCS data and anything built or cached locally.
ADDON_SKIP_TOP = {"tests", ".git", ".venv", "node_modules"}
ADDON_SKIP_ANY = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv"}
ADDON_SKIP_SUFFIX = (".pyc", ".pyo")
_ADDON_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def find_addons() -> list[Path]:
    """Optional add-ons in this checkout: ``addons/<name>/`` holding a pyproject.toml."""

    root = REPO / "addons"
    if not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir())
            if path.is_dir() and (path / "pyproject.toml").is_file()
            and _ADDON_NAME.fullmatch(path.name)]


def addon_tar(addon: Path) -> bytes:
    """``addon`` as a gzip tar (members relative to it), without tests, VCS data or caches.

    Shell scripts and unit files get LF endings and scripts the executable bit, whatever
    the working tree on this computer held.
    """

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(addon.rglob("*")):
            relative = path.relative_to(addon)
            parts = relative.parts
            if (parts[0] in ADDON_SKIP_TOP or ADDON_SKIP_ANY.intersection(parts)
                    or path.name.endswith(ADDON_SKIP_SUFFIX)
                    or any(part.endswith(".egg-info") for part in parts)):
                continue
            info = tarfile.TarInfo(relative.as_posix())
            info.mtime = int(path.stat().st_mtime)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                script = path.suffix in (".sh", ".service")
                if script:
                    data = data.replace(b"\r\n", b"\n")
                info.size = len(data)
                info.mode = 0o755 if path.suffix == ".sh" else 0o644
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def ship_addons(robot: Robot) -> None:
    """Copy each add-on to /opt/emori/addons/<name>, replacing that add-on's folder only.

    The core's file list (REMOTE_FILES_LIST) does not name these files, so the robot half's
    prune of files the archive no longer holds never touches addons/.
    """

    for addon in find_addons():
        name = addon.name
        remote = (
            f"set -e; D={REMOTE_ROOT}/addons; U=$(id -un); sudo -n mkdir -p $D; "
            "T=$(sudo -n mktemp -d $D/.incoming.XXXXXX); "
            "sudo -n tar -xzf - -m --no-same-owner -C $T; sudo -n chmod 755 $T; "
            f"sudo -n rm -rf $D/{name}; sudo -n mv $T $D/{name}; "
            "sudo -n chown -R $U: $D; "
            f"echo shipped=$(find $D/{name} -type f | wc -l)"
        )
        result = subprocess.run(robot.ssh() + [remote], input=addon_tar(addon),
                                capture_output=True, timeout=900, check=False)
        if result.returncode != 0:
            raise DeployError(f"copying the add-on {name} to the robot failed:\n    "
                              + result.stderr.decode("utf-8", "replace").strip())
        shipped = parse_kv(result.stdout.decode("utf-8", "replace")).get("shipped", "?")
        say(f"  add-on {name}: {shipped} files copied to {robot.host}:{REMOTE_ROOT}/addons/{name}")


def remove_addons(robot: Robot) -> None:
    """Uninstall every add-on on the robot and delete /opt/emori/addons and /opt/emori/hub-site.

    hub-site is where the hub container finds an add-on's installed files. Left behind, the
    entry point would still resolve to code that is gone and log a traceback on every lookup.

    A failure is reported and the deploy goes on: the core does not depend on any add-on,
    and one whose package is gone is skipped when the services start.
    """

    remote = (
        f"D={REMOTE_ROOT}/addons; H={REMOTE_ROOT}/hub-site; P={REMOTE_ROOT}/.venv/bin/pip; "
        'for f in "$D"/*/pyproject.toml; do [ -f "$f" ] || continue; '
        """n=$(sed -n "s/^name *= *[\\"']\\([^\\"']*\\).*/\\1/p" "$f" | head -n1); """
        '[ -n "$n" ] || continue; '
        'if [ -x "$P" ] && ! "$P" uninstall -y -q "$n"; then echo "failed=$n"; continue; fi; '
        'echo "removed=$n"; done; '
        'sudo -n rm -rf "$D" || echo "failed=$D"; '
        'sudo -n rm -rf "$H" || echo "failed=$H"'
    )
    try:
        result = subprocess.run(robot.ssh() + [remote], capture_output=True, timeout=300,
                                check=False)
    except subprocess.TimeoutExpired:
        say("  !! removing the add-ons timed out; the deploy goes on with the core")
        return
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    removed = [line.split("=", 1)[1] for line in lines if line.startswith("removed=")]
    failed = [line.split("=", 1)[1] for line in lines if line.startswith("failed=")]
    if result.returncode != 0 or failed:
        say(f"  !! add-ons not fully removed ({', '.join(failed) or 'ssh failed'}); "
            "the deploy goes on with the core")
    say(f"  add-ons on the robot: removed {', '.join(removed) or 'none'}")
    if removed:
        say("  settings they saved stay in configs/site/settings.env and secrets/site.env")


def ship(robot: Robot, *, addons: bool = True) -> None:
    remote = (
        f"set -e; D={REMOTE_ROOT}; U=$(id -un); "
        "if [ ! -d $D ] || [ ! -w $D ]; then sudo -n mkdir -p $D; sudo -n chown $U: $D; fi; "
        # as root, so files an older `sudo tar -x` left root-owned can be replaced; the
        # robot half hands them back to the user.
        f"sudo -n tar -xzvf - -m --no-same-owner -C $D > {REMOTE_FILES_LIST}; "
        f"echo shipped=$(grep -vc '/$' {REMOTE_FILES_LIST})"
    )
    archive = subprocess.Popen(["git", "-C", str(REPO), "archive", "--format=tar.gz", "HEAD"],
                               stdout=subprocess.PIPE)
    try:
        result = subprocess.run(robot.ssh() + [remote], stdin=archive.stdout,
                                capture_output=True, timeout=900, check=False)
    finally:
        archive.stdout.close()
        archive_rc = archive.wait()
    # SSH first: when it dies early, git archive only reports the broken pipe.
    if result.returncode != 0:
        raise DeployError("copying the code to the robot failed:\n    "
                          + result.stderr.decode("utf-8", "replace").strip())
    if archive_rc != 0:
        raise DeployError("git archive failed")
    shipped = parse_kv(result.stdout.decode("utf-8", "replace")).get("shipped", "?")
    say(f"  {shipped} files copied to {robot.host}:{REMOTE_ROOT}")
    if addons:
        ship_addons(robot)


def install(robot: Robot, checkout: dict[str, str], quick: bool,
            stack: str | None = None, hub_source: str = "") -> int:
    flags = [f"--files {REMOTE_FILES_LIST}", f"--commit {checkout['commit']}",
             f"--branch {_shell_word(checkout['branch'])}",
             f"--from {_shell_word(platform.node() or 'computer')}"]
    if hub_source:
        flags.append(f"--hub-source {_shell_word(hub_source)}")
    if quick:
        flags.append("--quick")
    if stack in ("v1", "v2"):
        flags.append(f"--stack {stack}")
    remote = f"bash {REMOTE_ROOT}/deploy/robot/one-click-deploy.sh {' '.join(flags)}"
    tty = _has_keyboard() and sys.stdout.isatty()
    return subprocess.run(robot.ssh(tty=tty) + [remote], check=False).returncode


def _shell_word(value: str) -> str:
    """Branch and computer names reach a remote shell; keep only harmless characters."""

    return re.sub(r"[^A-Za-z0-9._/-]", "_", value) or "unknown"


# --- the ROS 2 stack: v2 unless the robot says v1 ---------------------------------------
#
# The robot half switches stacks (deploy/robot/ros2_native/v2-stack.sh). What it cannot do
# is make its own image: the CM4 has no room to build one, so it is built here for arm64
# and pulled by the robot from a registry on this computer through an SSH reverse tunnel
# (README Route A; never `docker save | docker load`, which needs twice the image's size
# free on the card). Nothing in this section stops a deploy: a robot that cannot get its
# image stays on v1, which works, and the next deploy tries again.

V2_IMAGE = "emori/ros2-jazzy-v2:robot"
V2_DOCKERFILE = "deploy/ros2/Dockerfile.v2.robot"
V2_SOURCE_LABEL = "org.pokamind.emori.v2-source"
# What the image is built from: the Dockerfile and what it COPYs. README.md, which it also
# copies as package metadata, is left out: a docs edit is no reason for a 45-minute build.
V2_IMAGE_INPUTS = (V2_DOCKERFILE, "pyproject.toml", "src", "configs/behavior.yaml",
                   "ros2_ws", "deploy/ros2/models")
V2_STACK = f"{REMOTE_ROOT}/deploy/robot/ros2_native/v2-stack.sh"
V2_RESULT_FILE = f"{REMOTE_ROOT}/artifacts/last-ros2-stack.env"
V2_REGISTRY = "emori-deploy-registry"
# Room a pull needs on the robot beyond the unpacked image (the compressed layer in flight).
V2_PULL_HEADROOM_MB = 400
# A second copy of the 1.7 GB image beside the first, plus the pull's headroom.
V2_FULL_SHIP_MIN_FREE_MB = 2100
STACK_NAMES = {"v1": "v1 (Pollen's daemon owns the motors)",
               "v2": "v2 (the robot is a ROS 2 node)"}


def target_stack(args, info: dict[str, str]) -> str:
    """--v1/--v2, else what the robot's .env says, else v2."""

    if getattr(args, "v1", False):
        return "v1"
    if getattr(args, "v2", False):
        return "v2"
    wanted = info.get("stack_env", "").strip().lower()
    return wanted if wanted in ("v1", "v2") else "v2"


def refuse_v1_in_hub_mode(info: dict[str, str], stack: str) -> None:
    """v1 has no motor node for the hub to talk to, so it cannot be applied under the hub.

    Checked before anything is sent. The way out is to hand the gateway back to the host app
    first (``deploy/robot/hub-stack.sh apply hostapp``) and deploy again.
    """

    if stack == "v1" and info.get("hub_flag") == "yes":
        raise DeployError(
            "this robot runs the hub, and the hub needs the v2 motor node: v1 cannot be\n"
            "    applied under it. On the robot run 'bash /opt/emori/deploy/robot/hub-stack.sh\n"
            "    apply hostapp' (or press 'Back to the host app' on the phone admin page),\n"
            "    then deploy again with --v1.")


def report_stack(info: dict[str, str], stack: str) -> None:
    now = "v2" if info.get("v2_fence") == "yes" else "v1"
    if now == stack:
        say(f"  ROS 2 stack: {STACK_NAMES[now]}")
        return
    keep = "--v1" if now == "v1" else "--v2"
    say(f"  ROS 2 stack: {STACK_NAMES[now]} now; this deploy moves it to {STACK_NAMES[stack]}"
        f" ({keep} keeps it as it is)")


def v2_source_hash() -> str:
    """This checkout's v2 image inputs as one short hash: the label an image built from
    them carries (Dockerfile.v2.robot, ``EMORI_V2_SOURCE``)."""

    lines = [f"{path} {_git('rev-parse', f'HEAD:{path}')}" for path in V2_IMAGE_INPUTS]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def parse_v2_image(value: str) -> dict[str, str] | None:
    """``id|source|created`` from an image inspect; None when there is no image. An image
    built by hand has no source label: ``source`` is then empty."""

    parts = (value or "").strip().split("|")
    if len(parts) != 3 or not parts[0].startswith("sha256:"):
        return None
    source = parts[1] if re.fullmatch(r"[0-9a-f]{16}", parts[1]) else ""
    return {"id": parts[0], "source": source, "created": parts[2][:10]}


def _docker(*args: str, timeout: float = 120,
            capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=capture, timeout=timeout,
                          check=False)


def local_docker() -> bool:
    """Docker with buildx on THIS computer, answering."""

    if not shutil.which("docker"):
        return False
    for _ in range(2):  # Docker Desktop's context file is now and then briefly locked
        try:
            if (_docker("version", "--format", "{{.Server.Version}}", timeout=30).returncode == 0
                    and _docker("buildx", "version", timeout=30).returncode == 0):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    return False


def local_v2_image() -> dict[str, str] | None:
    """The arm64 v2 image on this computer, with ``size_mb``; None when there is none."""

    template = ("{{.Id}}|{{index .Config.Labels \"" + V2_SOURCE_LABEL + "\"}}|{{.Created}}"
                "|{{.Size}}|{{.Architecture}}")
    try:
        result = _docker("image", "inspect", "-f", template, V2_IMAGE, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.decode("utf-8", "replace").strip().split("|")
    if result.returncode != 0 or len(fields) != 5 or fields[4] != "arm64":
        return None
    image = parse_v2_image("|".join(fields[:3]))
    if image is not None:
        image["size_mb"] = str(int(fields[3]) // (1024 * 1024)) if fields[3].isdigit() else "0"
    return image


def free_mb_of(info: dict[str, str]) -> int:
    try:
        return int(info.get("free_mb", "0"))
    except ValueError:
        return 0


def full_ship_refusal(info: dict[str, str]) -> str:
    """Why the whole motor image must not be sent to this robot, or "" when it may.

    A robot running a small overlay on its image keeps that overlay (a full image would
    replace it and its rollback tag), and one short of room cannot hold a second copy of
    1.7 GB while the first is still tagged. Both are decided before anything is sent."""

    if info.get("v2_overlay") == "yes":
        return ("!! the robot runs an overlay on its v2 image, so a full image is not sent;"
                " motor-overlay.sh --rollback goes back to the image underneath")
    free = free_mb_of(info)
    if "free_mb" in info and free < V2_FULL_SHIP_MIN_FREE_MB:
        return (f"!! the robot has {free} MB free and a second v2 image needs"
                f" {V2_FULL_SHIP_MIN_FREE_MB} MB: a full image is not sent")
    return ""


def plan_v2_image(info: dict[str, str], args, *, interactive: bool) -> dict[str, str]:
    """What to do about the robot's v2 image, decided BEFORE anything is sent:
    ``{"action": keep|ship|build|none, "why": <sentence>}``. ``build`` was asked for."""

    wanted = v2_source_hash()
    current = parse_v2_image(info.get("v2_image", ""))
    rebuild = getattr(args, "rebuild_v2_image", False)
    if current is not None and rebuild:
        refusal = full_ship_refusal(info)
        if refusal:
            return {"action": "keep", "why": refusal}
    if current is not None and not rebuild:
        if current["source"] == wanted:
            return {"action": "keep", "why": "v2 image on the robot: built from this checkout"}
        origin = "older sources" if current["source"] else "sources it does not record"
        return {"action": "keep",
                "why": (f"v2 image on the robot: built {current['created']} from {origin}; it"
                        " keeps running it (--rebuild-v2-image builds one from this checkout"
                        " and ships it, about 45 minutes here)")}
    if not local_docker():
        return {"action": "none",
                "why": ("!! this computer has no Docker (with buildx) to build and ship the"
                        " robot's v2 image, so the robot stays on v1 this time. Install Docker"
                        " Desktop and deploy again (deploy/robot/ros2_native/README.md)")}
    if info.get("docker") != "yes" and info.get("internet") != "yes":
        return {"action": "none",
                "why": ("!! v2 needs Docker on the robot, and the robot has no internet to"
                        " install it: it stays on v1 this time")}
    local = local_v2_image()
    if local is not None and local["source"] == wanted and not rebuild:
        return {"action": "ship", "why": f"v2 image: shipping this computer's ({local['size_mb']} MB)"}
    say("  the robot needs a v2 image built from this checkout: Docker builds it here for")
    say("  arm64, about 45 minutes (most of it the ROS 2 workspace under emulation)")
    if interactive and not args.yes and not confirm("  build it now?"):
        return {"action": "none", "why": "v2 image not built: the robot stays on v1 this time"}
    return {"action": "build", "why": "v2 image: building it here, then shipping it"}


def build_v2_image(source: str) -> bool:
    """Build the robot image from the COMMITTED checkout -- what the robot runs -- for
    arm64, labelled with ``source``."""

    with tempfile.TemporaryDirectory(prefix="emori-v2-") as tmp:
        context = Path(tmp) / "context"
        archive_path = Path(tmp) / "checkout.tar"
        subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o",
                        str(archive_path), "HEAD"], check=True)
        with tarfile.open(archive_path) as archive:
            try:
                archive.extractall(context, filter="data")
            except TypeError:  # a Python without extraction filters; our own git archive
                archive.extractall(context)
        command = ["docker", "buildx", "build", "--platform", "linux/arm64",
                   "-f", str(context / V2_DOCKERFILE), "-t", V2_IMAGE,
                   "--build-arg", f"EMORI_V2_SOURCE={source}", "--load", str(context)]
        return subprocess.run(command, check=False).returncode == 0


def _free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_http(url: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(1)
    return False


def ship_v2_image(robot: Robot, image: str = V2_IMAGE) -> bool:
    """Push ``image`` to a registry on this computer and have the robot pull it through
    an SSH reverse tunnel: docker trusts a 127.0.0.1 registry without TLS, and the tunnel
    makes this computer's registry the robot's own 127.0.0.1. A pull writes compressed
    layers and skips every layer the robot already has."""

    port = _free_local_port()
    ref = f"127.0.0.1:{port}/{image}"
    _docker("rm", "-f", V2_REGISTRY, timeout=60)
    started = _docker("run", "-d", "--rm", "--name", V2_REGISTRY,
                      "-p", f"127.0.0.1:{port}:5000", "registry:2", timeout=600)
    if started.returncode != 0:
        say("  !! could not start a local registry (docker run registry:2):\n    "
            + started.stderr.decode("utf-8", "replace").strip())
        return False
    tunnel = None
    try:
        if not _wait_http(f"http://127.0.0.1:{port}/v2/", 30):
            say("  !! the local registry did not answer")
            return False
        say(f"  pushing {image} to a registry on this computer")
        if (_docker("tag", image, ref).returncode != 0
                or _docker("push", "-q", ref, timeout=3600).returncode != 0):
            say("  !! could not push the image to the local registry")
            return False
        ssh = robot.ssh()
        tunnel = subprocess.Popen(
            ssh[:-1] + ["-o", "ExitOnForwardFailure=yes", "-N",
                        "-R", f"{port}:127.0.0.1:{port}", ssh[-1]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(3)
        if tunnel.poll() is not None:
            say("  !! the SSH tunnel to the robot did not open: "
                + tunnel.stderr.read().decode("utf-8", "replace").strip())
            return False
        say("  the robot is pulling it (a first pull takes several minutes)")
        pulled = robot.run(
            f"set -e; sudo -n docker pull -q {ref} >/dev/null; "
            f"sudo -n docker tag {ref} {image}; sudo -n docker rmi {ref} >/dev/null; "
            "echo pulled=yes", timeout=3600)
        if parse_kv(pulled.stdout.decode("utf-8", "replace")).get("pulled") != "yes":
            say("  !! the robot could not pull the image:\n    "
                + pulled.stderr.decode("utf-8", "replace").strip())
            return False
        return True
    finally:
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        _docker("rm", "-f", V2_REGISTRY, timeout=60)
        _docker("rmi", ref, timeout=60)


def give_v2_image(robot: Robot, info: dict[str, str], plan: dict[str, str]) -> None:
    """Carry out ``plan_v2_image``'s plan, after the code is on the robot."""

    if plan["action"] in ("keep", "none"):
        return
    if info.get("docker") != "yes":
        say("  installing Docker on the robot")
        result = robot.run(f"bash {V2_STACK} docker", timeout=1200)
        if parse_kv(result.stdout.decode("utf-8", "replace")).get("docker") != "ready":
            say("  !! Docker did not install on the robot: it stays on v1 this time")
            return
    if plan["action"] == "build":
        if not build_v2_image(v2_source_hash()):
            say("  !! the v2 image did not build (see above): the robot stays on v1 this time")
            return
    local = local_v2_image()
    if local is None:
        say("  !! there is no arm64 v2 image on this computer: the robot stays on v1")
        return
    try:
        free = int(info.get("free_mb", "0"))
    except ValueError:
        free = 0
    need = int(local["size_mb"]) + V2_PULL_HEADROOM_MB
    if parse_v2_image(info.get("v2_image", "")) is None and free < need:
        say(f"  !! the robot has {free} MB free and its first v2 image needs about {need} MB:")
        say("     it stays on v1. Free some space on it and deploy again.")
        return
    if ship_v2_image(robot):
        say("  v2 image is on the robot")
    else:
        say("  the robot stays on v1 this time; deploying again retries")


# --- the hub image ----------------------------------------------------------------------
#
# The hub runs from emori/hub:robot, built ON the robot from a payload of files over the
# motor image already there (deploy/hub/hub-build.sh); no heavy layer is ever sent. The
# image carries emori_hub and the interfaces, and the adapter core it imports comes from
# the checkout under /opt/emori, which this deploy has already copied. The two must agree,
# so the image's source label hashes the three core files the hub imports as well.

HUB_IMAGE = "emori/hub:robot"
HUB_SOURCE_LABEL = "org.pokamind.emori.hub-source"
HUB_BASE_SOURCE_LABEL = "org.pokamind.emori.hub-base-source"
# What the hub image is built from: its Dockerfile and the files it copies.
HUB_IMAGE_INPUTS = ("deploy/hub/Dockerfile.hub", "deploy/hub/hub-entry.sh",
                    "deploy/hub/pw-record.sh",
                    "deploy/hub/fastdds_peers.xml", "deploy/hub/emori-hub-site.pth",
                    "ros2_ws/src/emori/emori_hub", "ros2_ws/src/emori/emori_adapter_ros",
                    "ros2_ws/src/emori/emori_skill_native",
                    "ros2_ws/src/open/hri_context_analyzer",
                    "ros2_ws/src/open/hri_engagement_behaviors",
                    "ros2_ws/src/open/emori_skill_facade",
                    "ros2_ws/src/open/reachy_mini_msgs", "ros2_ws/src/open/emori_msgs")
# Adapter-core files the hub imports from the bind mount: a change to one needs a new hub
# image, or the new core would start under an old hub.
HUB_CORE_INPUTS = ("src/emori_adapter/hostapp/server.py",
                   "src/emori_adapter/hostapp/extensions.py",
                   "src/emori_adapter/robot/client.py",
                   "src/emori_adapter/sources/websocket.py")
# What the base image (system packages and the pinned Python environment) is built from.
HUB_BASE_INPUTS = ("deploy/hub/Dockerfile.hub-base", "deploy/hub/constraints-hub.txt",
                   "deploy/hub/hub-packages.txt")
HUB_PAYLOAD_BUILDER = REPO / "scripts" / "build_hub_payload.py"
HUB_BUILD = f"{REMOTE_ROOT}/deploy/hub/hub-build.sh"
HUB_PAYLOAD_DIR = f"{REMOTE_ROOT}/hub-build"
# The hash the running hub's label must equal for a deploy to restart it.
# Free space a build needs, before the floor kept beside it (hub-build.sh holds the same).
HUB_NEED_TOP_MB = 100
HUB_NEED_BASE_MB = 563
HUB_FLOOR_MB = 400
MOTOR_NEED_MB = 300
MOTOR_LAYER_INPUTS = ("ros2_ws/src/open/reachy_mini_ros2", "ros2_ws/src/open/reachy_mini_msgs",
                      "ros2_ws/src/open/emori_msgs", "deploy/ros2/Dockerfile.v2.robot.delta")
MOTOR_OVERLAY = f"{REMOTE_ROOT}/deploy/robot/ros2_native/motor-overlay.sh"
MOTOR_PAYLOAD_DIR = "/tmp/emori-motor-layer"
HUB_STACK = f"{REMOTE_ROOT}/deploy/robot/hub-stack.sh"
BUILDENV_REPOSITORY = "emori/robot-buildenv"
BUILDENV_CAPTURE = REPO / "scripts" / "capture_robot_base.py"
# The sound reaches the speaker this long after the page plays it, so a move that starts with the
# voice is seen with the word. A robot whose .env names a value keeps it.
VOICE_DELAY_DEFAULT_MS = 100


def _inputs_hash(paths: tuple[str, ...]) -> str:
    lines = [f"{path} {_git('rev-parse', f'HEAD:{path}')}" for path in paths]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def motor_source_hash() -> str:
    """What a motor layer holds, as one short hash. Narrower than the full image's inputs, so
    a change that is not the motor's does not park and wake its motors."""

    return _inputs_hash(MOTOR_LAYER_INPUTS)


def hub_source_hash() -> str:
    """The hub image's inputs, including the core files it imports, as one short hash:
    the label an image built from this checkout carries."""

    return _inputs_hash(HUB_IMAGE_INPUTS + HUB_CORE_INPUTS)


def hub_base_source_hash() -> str:
    return _inputs_hash(HUB_BASE_INPUTS)


def find_addon_hub_wheels() -> list[dict[str, object]]:
    """Per add-on in this checkout, whether it ships what the hub's offline install reads:
    ``deploy/hub-wheels/`` (wheels) beside ``deploy/hub-constraints.txt`` (pins). Nothing
    here names an add-on; the folder names are whatever ``addons/`` holds."""

    found = []
    for addon in find_addons():
        wheels = addon / "deploy" / "hub-wheels"
        found.append({
            "name": addon.name,
            "wheels": wheels.is_dir() and any(wheels.glob("*.whl")),
            "constraints": (addon / "deploy" / "hub-constraints.txt").is_file(),
        })
    return found


def plan_hub_payload(info: dict[str, str], v2_plan: dict[str, str], *,
                     builder: bool | None = None) -> dict[str, str]:
    """What to do about the robot's hub image, decided BEFORE anything is sent:
    ``{"action": keep|top|base|none, "why": <sentence>}``.

    ``top`` copies files over the base already there (a payload of tens of MB at most, and
    compiled nothing unless an interface changed). ``base`` also builds the base. Neither
    moves a heavy layer, and a build that the robot's free
    space cannot hold is not planned: the running hub stays as it is.
    """

    if v2_plan["action"] in ("ship", "build"):
        return {"action": "none",
                "why": "hub image: built once the v2 image is on the robot, later in this deploy"}
    if parse_v2_image(info.get("v2_image", "")) is None:
        return {"action": "none", "why": "hub image: none, the robot has no v2 image"}
    if info.get("docker") != "yes":
        return {"action": "none", "why": "hub image: none, the robot has no Docker"}
    wanted = hub_source_hash()
    current = parse_v2_image(info.get("hub_image", ""))
    if current is not None and current["source"] == wanted:
        return {"action": "keep", "why": "hub image on the robot: built from this checkout"}
    base = parse_v2_image(info.get("hub_base", ""))
    base_current = base is not None and base["source"] == hub_base_source_hash()
    action = "top" if base_current else "base"
    if builder is None:
        builder = HUB_PAYLOAD_BUILDER.is_file()
    if not builder:
        return {"action": "none",
                "why": ("!! hub image: the payload builder (scripts/build_hub_payload.py) is"
                        " not in this checkout, so the running hub stays as it is")}
    need = (HUB_NEED_TOP_MB if action == "top" else HUB_NEED_BASE_MB) + HUB_FLOOR_MB
    free = free_mb_of(info)
    if free < need:
        return {"action": "none",
                "why": (f"!! hub image: the robot has {free} MB free and this build needs"
                        f" {need} MB, so the running hub stays as it is. Free some space and"
                        " deploy again")}
    what = ("files copied over its base" if action == "top"
            else "its base first, then the image")
    return {"action": action, "why": f"hub image: building it on the robot ({what})"}


def plan_motor_layer(info: dict[str, str], v2_plan: dict[str, str], *,
                     builder: bool | None = None) -> dict[str, str]:
    """Does the motor image need this checkout's node on top of it?

    ``{"action": keep|build|none, "why": <sentence>}``. Decided from the source label, which
    the layer itself carries afterwards, so a deploy with nothing new for the motor leaves the
    robot's motors alone: a new layer parks and wakes them.
    """

    if v2_plan["action"] in ("ship", "build"):
        return {"action": "keep", "why": ""}
    current = parse_v2_image(info.get("v2_image", ""))
    if current is None:
        return {"action": "none", "why": ""}
    if info.get("motor_source") == motor_source_hash() or current["source"] == v2_source_hash():
        return {"action": "keep", "why": "motor image on the robot: matches this checkout"}
    if builder is None:
        builder = HUB_PAYLOAD_BUILDER.is_file()
    if not builder:
        return {"action": "none",
                "why": ("!! motor image: the payload builder (scripts/build_hub_payload.py) is"
                        " not in this checkout, so the robot keeps the motor layer it has")}
    free = free_mb_of(info)
    if free < MOTOR_NEED_MB + HUB_FLOOR_MB:
        return {"action": "none",
                "why": (f"!! motor image: the robot has {free} MB free and a new layer needs"
                        f" {MOTOR_NEED_MB + HUB_FLOOR_MB} MB, so it keeps the one it has")}
    return {"action": "build", "why": "motor image: a layer from this checkout goes on top"}


def build_hub_payload(action: str, destination: Path) -> bool:
    """Run the payload builder: ``--layers top|base|motor --output <tar>``."""

    command = [sys.executable, str(HUB_PAYLOAD_BUILDER), "--layers", action,
               "--output", str(destination)]
    return subprocess.run(command, check=False).returncode == 0 and destination.is_file()


def needs_buildenv() -> bool:
    """No build environment here yet: every payload compiles against the robot's own files."""

    if not local_docker():
        return False
    listed = _docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", BUILDENV_REPOSITORY)
    if listed.returncode != 0:
        return False
    return not [line for line in listed.stdout.decode("utf-8", "replace").splitlines()
                if line.strip() and not line.strip().endswith(":<none>")]


def capture_buildenv(robot: Robot) -> bool:
    """Take the motor container's filesystem from the robot. About 1.7 GB over the network."""

    say("  this computer has no build environment yet: copying it from the robot (some minutes)")
    command = [sys.executable, str(BUILDENV_CAPTURE), "--ssh", f"{robot.user}@{robot.host}",
               "--out-dir", str(REPO / "build" / "robot-base")]
    # By the same route as everything else: its key, its known hosts, its port and jump.
    for option in robot.ssh()[1:-1]:
        command.append(f"--ssh-option={option}")
    return subprocess.run(command, check=False).returncode == 0


def ensure_buildenv(robot: Robot) -> bool:
    if needs_buildenv() and not capture_buildenv(robot):
        say("  !! could not copy the build environment from the robot")
        return False
    return True


def give_motor_layer(robot: Robot, plan: dict[str, str]) -> bool:
    """Carry out ``plan_motor_layer``'s plan. Returns whether the motor is up to date after."""

    if plan["action"] != "build":
        return plan["action"] == "keep"
    if not ensure_buildenv(robot):
        return False
    with tempfile.TemporaryDirectory(prefix="emori-motor-") as tmp:
        payload = Path(tmp) / "motor-payload.tar"
        say("  building the motor layer here (minutes, longer when a message changed)")
        if not build_hub_payload("motor", payload):
            say("  !! the motor layer did not build: the robot keeps the one it has")
            return False
        say(f"  sending the motor layer ({payload.stat().st_size // 1024} KB)")
        remote = f"set -e; D={MOTOR_PAYLOAD_DIR}; rm -rf $D; mkdir -p $D; tar -xf - -C $D"
        sent = subprocess.run(robot.ssh() + [remote], input=payload.read_bytes(),
                              capture_output=True, timeout=900, check=False)
    if sent.returncode != 0:
        say("  !! the motor layer did not reach the robot:\n    "
            + sent.stderr.decode("utf-8", "replace").strip())
        return False
    applied = robot.run(
        f"bash {MOTOR_OVERLAY} --delta {MOTOR_PAYLOAD_DIR}/motor --apply"
        f" --source {motor_source_hash()}; code=$?; rm -rf {MOTOR_PAYLOAD_DIR}; exit $code",
        timeout=900)
    if applied.returncode == 3:
        say("  !! the motor layer waits: " + applied.stderr.decode("utf-8", "replace").strip())
        return False
    if applied.returncode == 6:
        say("  !! the node did not come up on the new layer, so it went back to the image it"
            " replaced\n    " + applied.stderr.decode("utf-8", "replace").strip())
        return False
    if applied.returncode != 0:
        say("  !! the motor layer did not apply\n    "
            + applied.stderr.decode("utf-8", "replace").strip())
        return False
    say("  motor layer is on the robot")
    return True


def set_voice_delay_default(robot: Robot) -> None:
    """A robot with no EMORI_VOICE_DELAY_MS gets the default. A value set on it stays."""

    robot.run(f"cd {REMOTE_ROOT} && grep -qE '^EMORI_VOICE_DELAY_MS=' .env || "
              f"{{ [ ! -s .env ] || [ -z \"$(tail -c1 .env)\" ] || echo >> .env; "
              f"echo EMORI_VOICE_DELAY_MS={VOICE_DELAY_DEFAULT_MS} >> .env; "
              "sudo -n systemctl restart emori-voice-delay; }", timeout=60)


# hub-stack.sh's exit codes, as what the person at the computer should know.
HUB_SWITCH_OUTCOMES = {
    0: "ROS 2 hub: the robot now runs on it (the host app stays installed as the way back).",
    3: "ROS 2 hub: !! a session is running, so it did not switch. Deploy again when it is idle.",
    5: "ROS 2 hub: !! not possible yet, nothing changed (reason above). Deploy again once fixed.",
    6: "ROS 2 hub: !! it did not come up, so the robot went back to the host app (see above).",
    7: "ROS 2 hub: !! neither program answered: sudo systemctl status emori-hub emori-hostapp.",
}


def finish_on_hub(robot: Robot) -> tuple[str, bool, bool]:
    """After the install: the motor, the hub image, and the switch.

    Here and not before the install, because on a robot that has only just received its motor
    image, this is the first moment the motor container exists to build against. Answers the
    line for the last screen, whether the robot ended up on the hub from this checkout, and
    whether anything running changed, so that readiness has to be judged again.
    """

    step("putting the robot on the ROS 2 hub")
    info = preflight(robot)
    if parse_v2_image(info.get("v2_image", "")) is None or info.get("v2_fence") != "yes":
        return ("ROS 2 hub: !! not yet. The robot is not on v2 (see above); deploy again once"
                " it is.", False, False)
    keep = {"action": "keep", "why": ""}
    motor = plan_motor_layer(info, keep)
    if not local_docker() and (motor["action"] == "build"
                               or plan_hub_payload(info, keep)["action"] in ("top", "base")):
        return ("ROS 2 hub: !! this computer has no Docker to build with, so the robot stays as"
                " it is. Start Docker Desktop and deploy again.", False, False)
    if motor["why"]:
        say(f"  {motor['why']}")
    if motor["action"] == "none" and motor["why"].startswith("!!"):
        return "ROS 2 hub: !! no motor layer from this checkout (see above).", False, False
    if not give_motor_layer(robot, motor):
        return ("ROS 2 hub: !! the motor layer did not go on (see above), so nothing switched."
                " Deploy again.", False, True)
    changed = motor["action"] == "build"
    # The build took space, and a failed motor can have put the robot back: look again.
    if changed:
        info = preflight(robot)
    hub = plan_hub_payload(info, keep)
    if hub["why"]:
        say(f"  {hub['why']}")
    if hub["action"] == "none":
        return ("ROS 2 hub: !! no hub image from this checkout (see above), so nothing switched.",
                False, changed)
    built = hub["action"] != "keep"
    if built and not (ensure_buildenv(robot) and give_hub_payload(robot, hub)):
        return ("ROS 2 hub: !! no hub image from this checkout (see above), so nothing switched.",
                False, changed)
    if info.get("hub_flag") == "yes":
        if built:
            robot.run(f"cd {REMOTE_ROOT} && sh deploy/hub/hub-refresh.sh restart", timeout=300)
        return "ROS 2 hub: the robot runs on it.", True, changed or built
    set_voice_delay_default(robot)
    switched = robot.run(f"bash {HUB_STACK} apply ros", timeout=1200)
    for line in (switched.stdout + switched.stderr).decode("utf-8", "replace").splitlines():
        if line.strip():
            say(f"    {line.strip()}")
    return (HUB_SWITCH_OUTCOMES.get(
        switched.returncode, f"ROS 2 hub: !! the switch ended with exit {switched.returncode}."),
        switched.returncode == 0, True)


def recheck_readiness(robot: Robot) -> int:
    """The running programs changed after the robot half's readycheck, so that one is stale."""

    say("  readycheck, on the robot as it now runs")
    code = 1
    for attempt in range(2):
        if attempt:
            time.sleep(20)  # right after a restart a gate can still be warming up
        result = robot.run(f"cd {REMOTE_ROOT} && bash deploy/robot/readycheck.sh --no-speak",
                           timeout=300)
        code = result.returncode
        if code == 0:
            break
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("  ~ ") or line.startswith("PASS ") or " FAIL " in line:
            say(f"    {line.strip()}")
    return 0 if code == 0 else 1


def hold_for_docker(plan: dict[str, str]) -> dict[str, str]:
    """A build that needs Docker here is known to fail before anything is sent."""

    if plan["action"] not in ("top", "base") or local_docker():
        return plan
    return {"action": "none",
            "why": ("!! hub image: this computer has no Docker to build with, so it stays"
                    " as it is. Start Docker Desktop and deploy again")}


def give_hub_payload(robot: Robot, plan: dict[str, str], *, addons: bool = True) -> bool:
    """Carry out ``plan_hub_payload``'s plan, after the code is on the robot. Returns
    whether the robot's hub image matches this checkout afterwards.

    The deploy gives the robot half this checkout's hash (``install``), so it can see, before
    it restarts the hub, whether the image underneath was built from the core it is about to run.
    """

    if plan["action"] == "none":
        return False
    wanted = hub_source_hash()
    if addons:
        for entry in find_addon_hub_wheels():
            if entry["constraints"] and not entry["wheels"]:
                say(f"  !! add-on {entry['name']} pins hub dependencies but ships no"
                    " deploy/hub-wheels/*.whl: its hub install finds nothing to install")
    if plan["action"] == "keep":
        return True
    with tempfile.TemporaryDirectory(prefix="emori-hub-") as tmp:
        payload = Path(tmp) / "hub-payload.tar"
        if not build_hub_payload(plan["action"], payload):
            say("  !! the hub payload did not build: the running hub stays as it is")
            return False
        say(f"  sending the hub payload ({payload.stat().st_size // 1024} KB)")
        remote = (f"set -e; D={HUB_PAYLOAD_DIR}; rm -rf $D; mkdir -p $D; tar -xf - -C $D")
        sent = subprocess.run(robot.ssh() + [remote], input=payload.read_bytes(),
                              capture_output=True, timeout=900, check=False)
    if sent.returncode != 0:
        say("  !! the hub payload did not reach the robot:\n    "
            + sent.stderr.decode("utf-8", "replace").strip())
        return False
    words = f"{plan['action']} {wanted}"
    if plan["action"] == "base":
        words += f" {hub_base_source_hash()}"
    built = robot.run(f"bash {HUB_BUILD} {words}", timeout=3600)
    values = parse_kv(built.stdout.decode("utf-8", "replace"))
    if values.get("hub_build") != "ok":
        say("  !! the hub image did not build on the robot: the running hub stays as it is\n    "
            + built.stderr.decode("utf-8", "replace").strip())
        return False
    say("  hub image is built on the robot")
    return True


def stack_outcome(robot: Robot) -> str:
    """The robot half's ROS 2 stack verdict (artifacts/last-ros2-stack.env), as one line."""

    try:
        result = robot.run(f"cat {V2_RESULT_FILE} 2>/dev/null; true", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    values = parse_kv(result.stdout.decode("utf-8", "replace"))
    stack, reason = values.get("stack", ""), values.get("reason", "")
    if stack not in STACK_NAMES:
        return ""
    if reason in ("no_image", "no_docker"):
        return (f"ROS 2 stack: {STACK_NAMES['v1']}. v2 is not possible yet "
                f"({reason.replace('_', ' ')}); deploy again to retry.")
    if reason.endswith(("docker_hung", "container_not_down")):
        return ("ROS 2 stack: !! Docker does not answer or the motor container would not stop, so the"
                " robot stays fenced for v2 with Pollen's daemon off: sudo systemctl restart docker,"
                " then deploy again.")
    if reason.endswith("daemon_not_up"):
        return ("ROS 2 stack: !! Pollen's daemon did not answer on :8000 after the switch to"
                " v1: see 'ROS 2 stack' above (sudo systemctl status reachy-mini-daemon).")
    if reason not in ("switched", "unchanged"):
        return (f"ROS 2 stack: {STACK_NAMES[stack]}. !! v2 did not come up ({reason}), so the"
                " robot went back to v1: see 'ROS 2 stack' above.")
    moved = " (switched by this deploy)" if values.get("changed") == "yes" else ""
    return f"ROS 2 stack: {STACK_NAMES[stack]}{moved}"


# --- main -------------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


HUB_NOT_REACHED = 5
VERDICTS = {
    0: "ROBOT READY. The new version is installed and no readiness gate failed.",
    HUB_NOT_REACHED: "DEPLOYED, BUT NOT ON THE ROS 2 HUB from this checkout (reason below).",
    1: "DEPLOYED, BUT NOT READY: readycheck failed at least one gate (listed above).",
    3: "STOPPED ON THE ROBOT before anything was restarted (reason above).",
    4: "THE ROBOT'S INSTALLER FAILED (first error above). The old services may still be running.",
    255: "THE CONNECTION TO THE ROBOT DROPPED. Deploying again is safe.",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", help="robot IP address or name (default: search)")
    parser.add_argument("--user", default=None,
                        help=f"SSH user on the robot (default: {DEFAULT_USER})")
    parser.add_argument("--port", type=parse_port,
                        help="the robot's SSH port (default 22), e.g. a port forwarded to it")
    parser.add_argument("--jump", type=parse_jump, metavar="USER@HOST",
                        help="reach the robot through this SSH jump host (ssh -J): a machine "
                             "on the robot's network. Needs --host (the robot as the jump "
                             "host sees it)")
    parser.add_argument("--quick", action="store_true",
                        help="code only: restart the EMORI services, skip apt/pip/models/units")
    parser.add_argument("--no-addons", action="store_true",
                        help="do not copy this checkout's addons/ to the robot; an add-on "
                             "already on the robot stays installed (see --remove-addons)")
    parser.add_argument("--remove-addons", action="store_true",
                        help="uninstall every add-on on the robot and delete /opt/emori/addons, "
                             "then deploy the core; copies none from this checkout")
    parser.add_argument("--env", type=Path, help="upload this file as the robot's .env")
    parser.add_argument("--token-file", type=Path,
                        help="read THIS robot's Pokamind token (pkr1....) from a file")
    parser.add_argument("--replace-token", action="store_true",
                        help="ask for a new Pokamind token even if the robot has one")
    parser.add_argument("--no-token", action="store_true",
                        help="do not ask for a Pokamind token this time")
    stack_choice = parser.add_mutually_exclusive_group()
    stack_choice.add_argument("--v1", action="store_true",
                              help="put (or keep) this robot on v1: Pollen's daemon owns the "
                                   "motors. Recorded in its .env (EMORI_ROS2_STACK=v1)")
    stack_choice.add_argument("--v2", action="store_true",
                              help="put this robot on v2, the default: the robot is a ROS 2 "
                                   "node. Recorded in its .env")
    parser.add_argument("--rebuild-v2-image", action="store_true",
                        help="build the v2 image from this checkout and ship it even if the "
                             "robot has one (about 45 minutes; needs Docker here)")
    parser.add_argument("--no-hub", action="store_true",
                        help="leave this robot on the host app: no motor layer, no hub image, "
                             "no switch. Refused on a robot already on the hub")
    parser.add_argument("--find", action="store_true", help="only find the robot and print it")
    parser.add_argument("--no-browser", action="store_true", help="do not open the admin page")
    parser.add_argument("--yes", action="store_true",
                        help="ask nothing: no last confirmation, and a robot without a "
                             ".env gets the defaults (a generated admin PIN)")
    args = parser.parse_args(argv)

    say("EMORI one-click deploy")
    state = _load_state()
    token_outcome = ""
    stack_line = ""
    hub_line = ""
    warnings: list[str] = []
    generated: dict[str, str] = {}
    try:
        if args.env is not None and not args.env.is_file():
            raise DeployError(f"--env {args.env}: no such file")
        if args.token_file is not None:
            if not args.token_file.is_file():
                raise DeployError(f"--token-file {args.token_file}: no such file")
            parse_robot_token(args.token_file.read_text(encoding="utf-8"))  # fail early
        if (args.port or args.jump) and not args.host:
            raise DeployError("--port and --jump reach a robot on another network, which "
                              "this computer cannot search: give --host too (the robot's "
                              "address as seen from the jump host, or its Tailscale name)")
        checkout = describe_checkout()
        host = args.host or find_robot(state)
        if args.find:
            say(host)
            return 0
        robot = Robot(host, args.user or state.get("user") or DEFAULT_USER,
                      port=args.port, jump=args.jump)
        connect(robot)
        state.update(host=host, user=robot.user)
        _save_state(state)

        step("3/5  checking the robot")
        info = preflight(robot)
        say(f"  deploying {checkout['commit'][:10]} ({checkout['branch']}): {checkout['subject']}")
        if checkout["dirty"] != "0":
            say(f"  !! {checkout['dirty']} file(s) have uncommitted changes. Only COMMITTED code")
            say("     is deployed; commit first if the robot needs those changes.")
        if info.get("internet") != "yes" and not args.quick:
            say("  !! the robot has no internet: this will be a QUICK deploy (code + restart).")
        stack = target_stack(args, info)
        refuse_v1_in_hub_mode(info, stack)
        if args.no_hub and info.get("hub_flag") == "yes":
            # The code under a running hub would change with no image built to match it.
            raise DeployError("--no-hub on a robot on the hub: hand it back first "
                              "(deploy/robot/hub-stack.sh apply hostapp), then deploy again")
        report_stack(info, stack)
        interactive = _has_keyboard()
        # Every question BEFORE anything is sent, so a wrong answer costs nothing.
        token = choose_token(args, info, interactive=interactive)
        env_text = prepare_env(info, args.env, generated=generated,
                               ask_questions=not args.yes)
        v2_plan = ({"action": "none", "why": ""} if stack != "v2"
                   else plan_v2_image(info, args, interactive=interactive))
        if v2_plan["why"]:
            say(f"  {v2_plan['why']}")
        hub_plan = ({"action": "none", "why": ""} if stack != "v2" or args.no_hub
                    else hold_for_docker(plan_hub_payload(info, v2_plan)))
        if hub_plan["why"]:
            say(f"  {hub_plan['why']}")
        if not args.yes and interactive and not confirm(deploy_question(info, token)):
            raise DeployError("cancelled; nothing was sent")

        step("4/5  copying the code")
        ship(robot, addons=not (args.no_addons or args.remove_addons))
        if args.remove_addons:
            remove_addons(robot)
        upload_env(robot, env_text)
        env_text = None
        if token is not None:
            # choose_token returned a token for another robot id only after a human
            # said "install it anyway" at the keyboard; the robot re-checks the id
            # against the .env it has NOW and honours only that yes.
            staged = install_token(
                robot, token,
                allow_robot_id_change=bool(robot_id_problems(
                    info, parse_robot_token(token)[0])),
                previous=info.get("token_robot", "") if info.get("token") == "yes" else "")
            token = None  # nothing after this needs it
            # Deferred (no Python yet) or kept staged (Pokamind could not be asked, and
            # the robot has a working token): install.sh step 2b asks again, and the
            # last screen says what became of it.
            deferred = (staged.get("token_verdict") == "deferred"
                        or bool(staged.get("token_staged")))
        else:
            deferred = False
        give_v2_image(robot, info, v2_plan)
        if hub_plan["action"] in ("top", "base") and not ensure_buildenv(robot):
            hub_plan = {"action": "none", "why": ""}
        give_hub_payload(robot, hub_plan, addons=not args.no_addons)

        step("5/5  installing on the robot"
             + ("" if args.quick else " (a full install takes a few minutes)"))
        rc = install(robot, checkout, args.quick,
                     "v1" if args.v1 else "v2" if args.v2 else None, hub_source_hash())
        if deferred:
            token_outcome = staged_token_outcome(robot)
        stack_line = stack_outcome(robot) if rc in (0, 1) else ""
        if rc in (0, 1) and stack == "v2" and not args.no_hub and not args.quick:
            hub_line, on_hub, changed = finish_on_hub(robot)
            if changed:
                rc = recheck_readiness(robot)
            if not on_hub:
                rc = HUB_NOT_REACHED
        warnings = readycheck_warnings(robot) if rc in (0, 1) else []
    except DeployError as exc:
        say(f"\nDEPLOY STOPPED: {exc}")
        return 1
    except KeyboardInterrupt:
        say("\nDEPLOY INTERRUPTED. Deploying again is safe.")
        return 130

    say("\n" + "=" * 72)
    say(VERDICTS.get(rc, f"The robot-side deploy ended with exit code {rc} (see above)."))
    if warnings:
        say(f"  {len(warnings)} readiness warning(s), worth a read above (lines starting '~'):"
            f" {', '.join(warnings)}")
    if token_outcome:
        say(f"  {token_outcome}")
    if stack_line:
        say(f"  {stack_line}")
    if hub_line:
        say(f"  {hub_line}")
    if generated.get("EMORI_ADMIN_PIN"):
        say(f"  admin page PIN (generated for this robot; WRITE IT DOWN, shown once): "
            f"{generated['EMORI_ADMIN_PIN']}")
        say("    change it on the admin page, under Settings")
    route = robot.route()
    login = ["ssh", "-i", str(KEY_FILE), *route, f"{robot.user}@{host}"]
    if route:
        # Plain HTTP must not cross the internet: the admin page and the dashboard come
        # through the same SSH route the deploy took.
        tunnel = login[:-1] + ["-N", "-L", "8070:127.0.0.1:8070", "-L",
                               "8090:127.0.0.1:8090", login[-1]]
        say("  admin page and dashboard: through an SSH tunnel (plain HTTP never crosses")
        say("  the internet). Leave this running:")
        say(f"      {' '.join(tunnel)}")
        say("    then http://localhost:8070 (admin) and http://localhost:8090/dashboard")
    else:
        say(f"  robot admin (phone/laptop):  http://{host}:8070")
        say(f"  dashboard:                   http://{host}:8090/dashboard")
    say("    (its password: shown once at the end of a first install's output above; the")
    say("     admin page reveals or resets it: Settings -> Credentials, with the PIN)")
    say(f"  shell:                       {' '.join(login)}")
    say("=" * 72)
    if rc in (0, 1, HUB_NOT_REACHED) and not args.no_browser and not route:
        with contextlib.suppress(Exception):  # a missing browser is not a failed deploy
            webbrowser.open(f"http://{host}:8070")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
