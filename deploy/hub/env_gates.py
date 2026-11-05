#!/usr/bin/env python3
"""Four checks that the hub container runs with the environment the host application has.

The hub gets its environment from a snapshot of what systemd parsed (env-snapshot.py) and
runs on another image, so the two processes can differ in ways nothing else notices: a path
that exists on the host and not in the container, a name that does not resolve, a missing
certificate store, a setting that came out different. Run on the robot, with the host
application running and the hub up:

    python3 /opt/emori/deploy/hub/env_gates.py [--container emori-hub]

1. **Same environment.** The effective ``os.environ`` of a host application process and of
   the hub, after each has loaded its configuration, compared name by name by hash. Names on
   the hub's denylist (``emori_hub.main.ENV_DENYLIST``) are ignored. Every other difference
   must be on ``env_expected.txt`` with its exact value, so the list only widens by a
   reviewed change.
2. **Paths exist.** Every setting whose value is an absolute path names something that
   exists inside the container. With the graph in the hub, its behaviour file must also be
   under ``/opt/emori``: anywhere else it is not the file the host has.
3. **Names resolve.** Every host named in a URL or ``*_HOST`` setting resolves in the
   container whenever it resolves on the host.
4. **Certificates.** The container's default TLS context has a CA store.

Values are never printed, only names. Standard library only; the part that runs inside the
container (``--inside``) uses the hub's own interpreter.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import ipaddress
import json
import os
import posixpath
import socket
import ssl
import subprocess
import sys
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXPECTED = os.path.join(HERE, "env_expected.txt")
DEFAULT_HOSTAPP_UNIT = "emori-hostapp.service"
DEFAULT_HOSTAPP_PYTHON = "/opt/emori/.venv/bin/python"
DEFAULT_CONTAINER = "emori-hub"
HUB_PYTHON = "/opt/emori-hub/venv/bin/python"
HUB_ENTRY = "/usr/local/bin/hub-entry.sh"
HUB_SCRIPT = "/opt/emori/deploy/hub/env_gates.py"

#: The value in env_expected.txt that stands for any value.
ANY_VALUE = "*"
#: The one tree the hub and the host share, so the one place a graph file can come from.
SHARED_ROOT = "/opt/emori/"

_URL_SCHEMES = ("http", "https", "ws", "wss", "tcp", "smtp", "smtps", "imap", "imaps")
_NO_HOST = ("", "localhost", "0.0.0.0", "::")

#: Run in a child started with the host application's environment: it loads the adapter
#: core's configuration, which fills in ``.env`` and the site settings, and prints the result.
_LOAD_CHILD = (
    "import json, os, sys\n"
    "import emori_adapter.config\n"
    "json.dump(dict(os.environ), sys.stdout)\n"
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def digests(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: digest(value) for name, value in environ.items()}


def is_denied(name: str, denylist: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in denylist)


def parse_expected(text: str) -> dict[str, str]:
    """``NAME=value`` per line; ``#`` starts a comment; ``NAME=*`` accepts any value."""

    expected: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ValueError(f"env_expected.txt line {number} is not NAME=value")
        expected[name.strip()] = value
    return expected


# --- 1. the same environment -----------------------------------------------------------


def compare_environments(
    host: Mapping[str, str],
    hub: Mapping[str, str],
    denylist: Sequence[str],
    expected: Mapping[str, str],
) -> list[str]:
    """Problems found comparing two hashed environments; empty when they agree."""

    problems: list[str] = []
    for name in sorted(set(host) | set(hub)):
        if is_denied(name, denylist):
            continue
        on_host, in_hub = host.get(name), hub.get(name)
        if on_host == in_hub:
            continue
        rule = expected.get(name)
        if rule is not None and in_hub is not None and (
                rule == ANY_VALUE or digest(rule) == in_hub):
            continue
        if on_host is None:
            reason = ("set in the hub only, and not on env_expected.txt" if rule is None
                      else "set in the hub only, with another value than env_expected.txt gives")
        elif in_hub is None:
            reason = "set for the host application and missing in the hub"
        else:
            reason = "set to different values"
        problems.append(f"{name}: {reason}")
    return problems


# --- 2. paths exist, 3. names resolve --------------------------------------------------


def absolute_paths(environ: Mapping[str, str], denylist: Sequence[str]) -> dict[str, str]:
    """Settings whose whole value is one absolute path. Lists of paths are not."""

    found: dict[str, str] = {}
    for name, value in environ.items():
        text = value.strip()
        if (is_denied(name, denylist) or len(text) < 2 or not text.startswith("/")
                or any(ch.isspace() or ch == ":" for ch in text)):
            continue
        found[name] = text
    return found


def hostnames(environ: Mapping[str, str], denylist: Sequence[str]) -> dict[str, str]:
    """The host named by each URL setting and each ``*_HOST`` setting, by setting name.

    Addresses, ``localhost`` and the wildcard bind address need no lookup.
    """

    found: dict[str, str] = {}
    for name, value in environ.items():
        if is_denied(name, denylist):
            continue
        text = value.strip()
        host = ""
        if "://" in text:
            parsed = urlparse(text)
            if parsed.scheme.lower() in _URL_SCHEMES:
                host = parsed.hostname or ""
        elif name.endswith("_HOST") and text and not any(ch.isspace() for ch in text):
            host = text
        host = host.lower()
        if host in _NO_HOST:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            found[name] = host
    return found


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return True


# --- the part that runs inside the container -------------------------------------------


def inside_report(
    environ: Mapping[str, str],
    denylist: Sequence[str],
    *,
    exists: Callable[[str], bool] = os.path.exists,
    resolve: Callable[[str], bool] = resolves,
    ca_certs: Callable[[], int] | None = None,
    behavior_yaml: str = "",
) -> dict:
    """Everything the checks need from the hub's own point of view, with no values in it."""

    if ca_certs is None:
        def ca_certs() -> int:
            return len(ssl.create_default_context().get_ca_certs())
    return {
        "denylist": list(denylist),
        "environment": digests(environ),
        "paths": {name: bool(exists(path))
                  for name, path in sorted(absolute_paths(environ, denylist).items())},
        "hosts": {name: bool(resolve(host))
                  for name, host in sorted(hostnames(environ, denylist).items())},
        "ca_certs": int(ca_certs()),
        "graph_behavior_ok": not behavior_yaml or (
            posixpath.normpath(behavior_yaml).startswith(SHARED_ROOT)
            and bool(exists(behavior_yaml))),
    }


def run_inside() -> int:
    """Load the environment the way ``emori_hub.main`` does, then report on it."""

    from emori_hub import main as hub_main

    graph = hub_main.load_env_snapshot(os.environ)
    import emori_adapter.config  # noqa: F401  # loads .env and the site settings into os.environ

    json.dump(inside_report(os.environ, hub_main.ENV_DENYLIST,
                            behavior_yaml=graph.behavior_yaml if graph else ""), sys.stdout)
    return 0


# --- the part that runs on the host ----------------------------------------------------


def evaluate(
    host_environment: Mapping[str, str],
    report: Mapping,
    expected: Mapping[str, str],
    host_resolves: Callable[[str], bool] = resolves,
) -> dict[str, list[str]]:
    """The four checks, each a list of problems; an empty list is a pass.

    ``host_environment`` is the host application's effective environment, with its values:
    the hosts it names are looked up on the host here and compared with what the container
    found, which the report holds.
    """

    denylist = report["denylist"]
    names = hostnames(host_environment, denylist)
    hosts = []
    for name, in_container in sorted(report["hosts"].items()):
        host = names.get(name)
        if host is not None and host_resolves(host) and not in_container:
            hosts.append(f"{name}: resolves on the host and not in the container")
    paths = [f"{name}: names a path that does not exist in the container"
             for name, there in sorted(report["paths"].items()) if not there]
    if not report.get("graph_behavior_ok", True):
        paths.append(f"the graph's behaviour file is not a file under {SHARED_ROOT}")
    return {
        "environment": compare_environments(
            digests(host_environment), report["environment"], denylist, expected),
        "paths": paths,
        "hosts": hosts,
        "certificates": ([] if report["ca_certs"] > 0
                         else ["the container's default TLS context has no CA certificates"]),
    }


def hostapp_environment(pid: int, python: str, root: str) -> dict[str, str]:
    """The host application's effective environment.

    The process environment is what systemd gave it. The configuration it loads afterwards
    (``.env``, the site settings) is not in ``/proc``, so a child is started with that
    environment and loads the same configuration.
    """

    with open(f"/proc/{pid}/environ", "rb") as handle:
        raw = handle.read()
    given = {}
    for item in raw.split(b"\0"):
        name, separator, value = item.partition(b"=")
        if separator and name:
            given[os.fsdecode(name)] = os.fsdecode(value)
    done = subprocess.run([python, "-c", _LOAD_CHILD], env=given, cwd=root,
                          capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"the host application's settings could not be loaded: {tail[0]}")
    return json.loads(done.stdout)


def hub_report(container: str) -> dict:
    command = ["docker", "exec", container, HUB_ENTRY, HUB_PYTHON, HUB_SCRIPT, "--inside"]
    done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"could not read the hub's environment: {tail[0]}")
    return json.loads(done.stdout)


def hostapp_pid(unit: str) -> int:
    done = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                          capture_output=True, text=True, timeout=10, check=False)
    try:
        pid = int(done.stdout.strip())
    except ValueError:
        pid = 0
    if pid <= 0:
        raise RuntimeError(f"{unit} is not running")
    return pid


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment gates for the hub container.")
    parser.add_argument("--inside", action="store_true",
                        help="report from inside the container (used by this script itself)")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--hostapp-unit", default=DEFAULT_HOSTAPP_UNIT)
    parser.add_argument("--hostapp-pid", type=int, default=0,
                        help="a host application process, instead of the unit's main one")
    parser.add_argument("--hostapp-python", default=DEFAULT_HOSTAPP_PYTHON)
    parser.add_argument("--root", default="/opt/emori")
    parser.add_argument("--expected", default=DEFAULT_EXPECTED)
    args = parser.parse_args(argv)

    if args.inside:
        return run_inside()
    try:
        with open(args.expected, encoding="utf-8") as handle:
            expected = parse_expected(handle.read())
        pid = args.hostapp_pid or hostapp_pid(args.hostapp_unit)
        host_environment = hostapp_environment(pid, args.hostapp_python, args.root)
        report = hub_report(args.container)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"env_gates: {error}", file=sys.stderr)
        return 2

    failed = False
    for gate, problems in evaluate(host_environment, report, expected).items():
        print(f"{gate}: {'PASS' if not problems else 'FAIL'}")
        for problem in problems:
            print(f"  {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
