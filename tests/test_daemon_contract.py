"""Every Reachy-daemon HTTP call this code makes must still be answered on a v2 robot.

On a v2 robot the ROS 2 node ``reachy_mini_ros2`` owns ``:8000`` through its REST shim
(``ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/rest_shim.py``) and Pollen's daemon
is fenced off. A call to a daemon-only route such as ``/wifi/*`` or ``/api/volume/*``
answers 404 there: the phone's Network tab says "HTTP 404" and nothing raises the
hotspot at boot. emori-admin answers those from ``admin/host_backend.py``
(+ ``netman.py``/``mixer.py``); this test is what stops the NEXT daemon-only call from
shipping.

How it works -- all static, with ``ast``, nothing imported and nothing run:

1. **Enumerate** every HTTP call to ``:8000`` made anywhere under ``src/emori_adapter``:

   * emori-admin: every call of ``probes.daemon`` -- found by IMPORT ORIGIN, so an alias
     (``from ...probes import daemon as d``) or a fully qualified
     ``emori_adapter.admin.probes.daemon(...)`` is the same call -- followed through
     wrapper functions that pass ``method``/``path`` on (``network._wifi``,
     ``audio._volume``);
   * the hostapp's robot client (``robot/client.py`` ``HttpRobotClient``): every
     ``self._http``/``self._mode_http`` call, through ``_post``/``_emori_flag``;
   * any URL built on the daemon's base URL (``f"{reachy}/api/..."`` in readycheck,
     ``REACHY_BASE_URL + "/..."``, ``settings.reachy_base_url + ...``, a literal
     ``http://...:8000/...``) that is handed to an HTTP call -- and a URL whose path is a
     parameter (``f"{base}{path}"``) makes that function a wrapper, resolved at its call
     sites. A daemon URL whose path cannot be read, or that goes anywhere but a
     recognised HTTP call, is a problem.

   And the **doors**: every file that can learn the daemon's address or call it at all
   (``REACHY_BASE_URL`` in any form, ``.reachy_base_url``, ``daemon_base``, an import of
   ``admin.probes``, ``.base_url`` of another object, a ``host:8000`` in a string -- an
   f-string, a %-format, browser JS in a page -- or the integer 8000 as a port) must be
   listed in :data:`DOORS`. Inside a door every single USE of the address must be
   accounted for: a binding, the argument of ``HttpRobotClient(...)``, part of an
   enumerated call, or an entry of :data:`DOOR_USES` (text: a hint, a log line). Passing
   ``probes.daemon`` or a wrapper around as a value (``functools.partial``, a callback) is
   a problem: only direct calls can be read. ``scripts/`` and ``deploy/`` are scanned for
   ``:8000`` too (:data:`SCRIPT_DOORS`), and a route a robot-side shell script calls must
   be served on v2.

2. **Check** each call. It passes when it is

   * made through a wrapper that answers from emori-admin's own host backend
     (``host_backend.current``/``route_missing`` + ``netman.request``/``mixer.request``)
     AND that backend's dispatch table answers this exact (method, path) -- such a call
     never reaches the shim on v2, so the shim serving the route does not count; or
   * (any other call) served by the v2 REST shim (its FastAPI route decorators, read with
     ``ast``, path parameters included); or
   * listed in :data:`V1_ONLY` -- a small, commented allowlist of v1-only calls whose
     absence on v2 is handled gracefully, each naming the test that proves it.

   Anything else is a 404 on a v2 robot waiting to happen, and fails.

3. **The manifest.** CI proves the calls against the shim's SOURCE; a robot runs whatever
   shim (or Pollen daemon) it was last shipped, which can be older than the code. So the
   same enumeration also writes :data:`MANIFEST`, every route the code needs from
   ``:8000`` and on which flavour (:func:`route_manifest`), and ``emori-readycheck``'s
   ``daemon_routes`` gate asks the RUNNING ``:8000`` for each of them. The test fails
   when the committed file is not what the code needs today; regenerate it with::

       python tests/test_daemon_contract.py --write-manifest

Run it against another tree (e.g. an old commit in a temp copy)::

    python tests/test_daemon_contract.py <root>
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = "src/emori_adapter"
SHIM = "ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2/rest_shim.py"
# emori-admin's own answer to the daemon's Wi-Fi and volume routes (v2 robots).
HOST_BACKENDS = {"netman": "src/emori_adapter/admin/netman.py",
                 "mixer": "src/emori_adapter/admin/mixer.py"}
PROBES = "src/emori_adapter/admin/probes.py"
PROBES_MODULE = "emori_adapter.admin.probes"
PROBES_DAEMON = PROBES_MODULE + ".daemon"
PROBES_DAEMON_BASE = PROBES_MODULE + ".daemon_base"
# The one class that drives the robot over REST (hostapp + cli + session).
DAEMON_CLIENTS = {"src/emori_adapter/robot/client.py": "HttpRobotClient"}
CLIENT_ORIGINS = frozenset({"emori_adapter.robot.client.HttpRobotClient"})
# Outside the package: what talks to :8000 on a dev box, at deploy time or on the robot.
SCRIPT_DIRS = (("scripts", (".py", ".sh", ".ps1")), ("deploy", (".py", ".sh")))
# The scripts that run ON a robot (v1 or v2): every :8000 route they call must be served.
ROBOT_SCRIPTS = "deploy/"

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")
# Methods of an httpx.Client that send nothing.
CLIENT_QUIET = ("close",)

# Every file that can learn the daemon's address or call it, and how its calls are
# enumerated. A file that references it and is not here fails the test; an entry whose
# file no longer references it fails too (the list stays the truth).
DOORS = {
    PROBES: "probes.daemon(): every emori-admin daemon call goes through it",
    "src/emori_adapter/admin/audio.py": "imports probes: audio._volume -> probes.daemon()",
    "src/emori_adapter/admin/backend_check.py": "imports probes: no daemon call of its own",
    "src/emori_adapter/admin/host_backend.py": "imports probes: probes.daemon() (status)",
    "src/emori_adapter/admin/mixer.py": "imports probes: subprocesses only (probes.run)",
    "src/emori_adapter/admin/netman.py": "imports probes: subprocesses only (probes.run)",
    "src/emori_adapter/admin/network.py": "imports probes: network._wifi -> probes.daemon()",
    "src/emori_adapter/admin/server.py": "imports probes: no daemon call of its own",
    "src/emori_adapter/admin/system.py": "imports probes: subprocesses only (probes.run)",
    "src/emori_adapter/config.py": "Settings.reachy_base_url, handed only to HttpRobotClient",
    "src/emori_adapter/cli.py": "HttpRobotClient(settings.reachy_base_url)",
    "src/emori_adapter/session.py": "HttpRobotClient(s.reachy_base_url)",
    "src/emori_adapter/hostapp/server.py": "REACHY_BASE_URL -> HttpRobotClient",
    "src/emori_adapter/hostapp/readycheck.py": "collect(): probe.http(f'{reachy}/...')",
    "src/emori_adapter/robot/client.py": "HttpRobotClient: self._http / self._mode_http",
}

# Inside a door: every use of the daemon's address that is NOT a call, and why.
#   key:   (file, function qualname or "<module>", where the address goes) -- the failure
#          message prints the exact key to paste
#   value: why that use sends nothing to :8000
_HINT = "operator hint text: shows the daemon URL, sends nothing"
DOOR_USES: dict[tuple[str, str, str], str] = {
    ("src/emori_adapter/cli.py", "run", "f-string in console.print()"):
        "the 'daemon not reachable' warning: shows the URL, sends nothing",
    ("src/emori_adapter/hostapp/readycheck.py", "collect", "Evidence(reachy_base_url=)"):
        "kept in the record for the gates' hint text; the two GETs are enumerated",
    ("src/emori_adapter/hostapp/readycheck.py", "_daemon_start_hint", "f-string in return"):
        _HINT,
    ("src/emori_adapter/hostapp/readycheck.py", "gate_motors", "f-string in GateResult()"):
        _HINT,
    ("src/emori_adapter/hostapp/readycheck.py", "gate_ros2", "f-string in GateResult()"):
        _HINT,
    ("src/emori_adapter/hostapp/readycheck.py", "_gate_ros2_native",
     "f-string in GateResult()"):
        _HINT,
}

# Files outside src/ that reference :8000 (or REACHY_BASE_URL), and why that is fine.
# Every robot-side (deploy/) shell route is also checked against the v2 shim.
_SIM = "dev box: drives the MuJoCo sim daemon or the v2 container on 127.0.0.1:8000"
SCRIPT_DOORS: dict[str, str] = {
    "deploy/robot/install.sh": "post-install status print: GET /api/daemon/status and "
                               "/api/motors/status, both served by the v2 shim",
    "deploy/robot/robot-mode.sh": "status print: GET /api/daemon/status (served on v2)",
    "deploy/robot/ros2_native/install-native.sh": "waits for the v2 shim's "
                                                  "/api/daemon/status, prints motors",
    "deploy/robot/ros2_native/v2-stack.sh": "the deploy's stack switch: GET /api/daemon/"
                                            "status, whose `backend` field tells the v2 "
                                            "shim from Pollen's daemon (served by both)",
    "scripts/deploy_to_robot.py": "the operator's PC: :8000 is one of the ports that say "
                                  "'this host is a robot' (read-only probe)",
    "scripts/launch_kiosk_browser.ps1": _SIM,
    "scripts/rehearse_motion_owner.py": _SIM,
    "scripts/rehearse_on_robot.py": "rehearsal: asks /api/daemon/status which backend "
                                    "owns :8000 before it starts",
    "scripts/golden_host.py": "the hostapp under a recording client: HttpRobotClient, whose "
                              "routes are checked above",
    "scripts/motion_sync_run.py": "scripted motion run: GET /api/daemon/status, "
                                  "/api/state/full, /api/move/running and /api/move/trace, "
                                  "all served by the v2 shim",
    "scripts/ros2_v2_smoke.py": "dev box: smoke-tests the v2 container's own shim",
    "scripts/sim_full_up.ps1": _SIM,
    "scripts/smoke_daemon.py": _SIM + " (HttpRobotClient, whose routes are checked above)",
}

# v1-only daemon calls whose ABSENCE on a v2 robot is handled gracefully.
#   key:   (METHOD, path template, the file that makes the call)
#   value: (why a 404 there is harmless, "tests/<file>.py::<test>" proving it)
# Keep it small: every entry is something that silently does nothing on v2. It is
# empty today -- every call the code makes is served by the shim or by emori-admin.
V1_ONLY: dict[tuple[str, str, str], tuple[str, str]] = {}

# The mirror image, for the manifest: calls the v2 shim serves and Pollen's daemon (v1)
# does not -- EMORI's own `/api/emori/*` -- whose ABSENCE on v1 is handled gracefully.
# Same shape as V1_ONLY. Without it readycheck would fail every v1 robot for a route it
# was never meant to have.
_EMORI_LATCH = ("Pollen's daemon has no /api/emori/*: the robot client latches the route "
                "off on its first 404 and warns once",
                "tests/test_robot_client.py::test_a_404_latches_the_route_off_and_warns"
                "_exactly_once")
V2_ONLY: dict[tuple[str, str, str], tuple[str, str]] = {
    ("POST", "/api/emori/gaze", "src/emori_adapter/robot/client.py"): _EMORI_LATCH,
    ("POST", "/api/emori/head_reserved", "src/emori_adapter/robot/client.py"): _EMORI_LATCH,
}

# Inside a door: uses of the address that ask :8000 WHICH routes exist and run none of
# them -- GET /openapi.json, and OPTIONS, which Starlette answers 405 (the path exists)
# or 404 before any handler is called. They need no route served, so they are not calls
# this test checks; the routes they ask about are the manifest's. Same key as DOOR_USES.
ROUTE_PROBES: dict[tuple[str, str, str], str] = {
    ("src/emori_adapter/hostapp/readycheck.py", "collect", "_probe_daemon_routes()"):
        "the daemon_routes gate: GET /openapi.json, then OPTIONS for any manifest route "
        "it does not list",
}

# Every route the code needs from :8000, for readycheck's daemon_routes gate (module
# doc, point 3). Shipped with the package: the gate reads it on the robot.
MANIFEST = "src/emori_adapter/hostapp/daemon_routes.json"
MANIFEST_COMMAND = "python tests/test_daemon_contract.py --write-manifest"
_MANIFEST_ABOUT = (
    "Every :8000 route this code calls (method + FastAPI path template), generated from "
    "the enumeration tests/test_daemon_contract.py checks. emori-readycheck's "
    "daemon_routes gate asks the RUNNING :8000 for each route needed on its flavour: v1 = "
    "Pollen's reachy-mini daemon, v2 = the robot node's REST shim. fallback: emori-admin "
    "answers the route itself (netman/mixer) when :8000 does not. call: a call the code "
    "makes on it ({} = a value known only at run time). Generated: do not edit.")

HOLE = "{}"


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Call:
    """One HTTP call to the daemon, as the code makes it."""

    method: str
    path: str            # template; "{}" is a value only known at run time
    where: str           # "src/emori_adapter/admin/network.py:221"
    via: str = ""        # the wrapper(s) it goes through, "network._wifi"
    host: str = ""       # "netman" / "mixer": the wrapper answers from emori-admin

    @property
    def file(self) -> str:
        return self.where.rsplit(":", 1)[0]

    def __str__(self) -> str:
        via = " via %s" % self.via if self.via else ""
        return "%s %s (%s%s)" % (self.method, self.path, self.where, via)


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    regex: str

    def serves(self, method: str, path: str) -> bool:
        sample = path.split("?", 1)[0].replace(HOLE, "0")
        return method == self.method and re.fullmatch(self.regex, sample) is not None


@dataclass
class Report:
    calls: list[Call] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    host_tables: dict[str, set] = field(default_factory=dict)
    unserved: list[Call] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def verdict(self, call: Call) -> str:
        if call.host:
            # A host-dispatching wrapper (network._wifi / audio._volume) asks the shim
            # NOTHING on v2: the selector sends the call to netman/mixer, and a daemon
            # 404 is re-asked there too. Its route must be in THAT table, served by the
            # shim or not (e.g. `_volume("POST", "/api/media/play_sound")`).
            table = self.host_tables.get(call.host, set())
            return "host:" + call.host if (call.method, call.path) in table else ""
        if self.serving_route(call) is not None:
            return "shim"
        return ""

    def serving_route(self, call: Call) -> Optional[Route]:
        """The first shim route that serves this method and path (Starlette's order)."""

        return next((route for route in self.routes
                     if route.serves(call.method, call.path)), None)


# --------------------------------------------------------------------------------------
# the tree: a checkout, optionally with some files replaced (the tests' synthetic trees)
# --------------------------------------------------------------------------------------
class Tree:
    """A directory whose files are read through ``overlay`` (rel -> text) first."""

    def __init__(self, root: Path, overlay: Optional[dict[str, str]] = None) -> None:
        self.root = Path(root)
        self.overlay = dict(overlay or {})

    def read(self, rel: str) -> str:
        if rel in self.overlay:
            return self.overlay[rel]
        return (self.root / rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return rel in self.overlay or (self.root / rel).is_file()

    def files(self, top: str, suffixes: tuple[str, ...]) -> list[str]:
        found: set[str] = set()
        base = self.root / top
        if base.is_dir():
            for path in base.rglob("*"):
                if path.suffix in suffixes and "__pycache__" not in path.parts \
                        and path.is_file():
                    found.add(path.relative_to(self.root).as_posix())
        found.update(rel for rel in self.overlay
                     if rel.startswith(top + "/") and Path(rel).suffix in suffixes)
        return sorted(found)


def _tree(where: Union[Path, Tree]) -> Tree:
    return where if isinstance(where, Tree) else Tree(where)


# --------------------------------------------------------------------------------------
# the v2 shim's routes, and emori-admin's own dispatch tables
# --------------------------------------------------------------------------------------
_CONVERTORS = {"path": ".*", "int": "[0-9]+", "float": "[0-9.]+",
               "uuid": "[0-9a-fA-F-]+", "str": "[^/]+", "": "[^/]+"}


def route_regex(path: str) -> str:
    """A FastAPI/Starlette path template as a regex (``{x}``, ``{x:path}``, ...)."""

    out, last = [], 0
    for match in re.finditer(r"\{([A-Za-z_]\w*)(?::([a-z]+))?\}", path):
        out.append(re.escape(path[last:match.start()]))
        out.append(_CONVERTORS.get(match.group(2) or "", "[^/]+"))
        last = match.end()
    out.append(re.escape(path[last:]))
    return "".join(out)


def shim_routes(where: Union[Path, Tree]) -> tuple[list[Route], list[str]]:
    """Every route ``build_app`` registers with ``@app.<verb>("...")``.

    Anything this cannot read -- a router, ``add_api_route``, a mount, a non-literal
    path, a route registered under an ``if`` -- is a problem, never a silent miss:
    a route this test does not see is a call it would wrongly fail, and a route it
    sees but the shim only SOMETIMES serves is a call it would wrongly pass.
    """

    tree = ast.parse(_tree(where).read(SHIM))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "APIRouter":
            problems.append("%s:%d uses APIRouter; teach shim_routes() to read it"
                            % (SHIM, node.lineno))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("add_api_route", "include_router", "mount",
                                       "add_route", "api_route", "route")):
            problems.append("%s:%d registers routes with .%s(); teach shim_routes() "
                            "to read it" % (SHIM, node.lineno, node.func.attr))
    build = next((node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "build_app"), None)
    if build is None:
        return [], ["%s has no build_app(); teach shim_routes() where the routes are" % SHIM]
    routes: list[Route] = []
    direct = {id(stmt) for stmt in build.body}
    for node in ast.walk(build):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"):
                continue
            verb = decorator.func.attr
            if verb not in HTTP_VERBS:
                problems.append("%s:%d @app.%s is not a route this test understands"
                                % (SHIM, decorator.lineno, verb))
                continue
            arg = decorator.args[0] if decorator.args else None
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                problems.append("%s:%d route path is not a literal" % (SHIM, decorator.lineno))
                continue
            if id(node) not in direct:
                problems.append("%s:%d %s %s is registered conditionally; decide whether "
                                "it counts as served" % (SHIM, decorator.lineno,
                                                         verb.upper(), arg.value))
                continue
            routes.append(Route(verb.upper(), arg.value, route_regex(arg.value)))
    if not routes:
        problems.append("%s: no routes found; the shim moved or changed shape" % SHIM)
    return routes, problems


def host_table(where: Union[Path, Tree], rel: str) -> set[tuple[str, str]]:
    """The (method, path) pairs ``request()`` in a host backend module answers.

    Read from its comparisons: ``route == ("GET", "/wifi/status")`` and
    ``route in (("GET", "/a"), ("POST", "/b"))``.
    """

    tree = ast.parse(_tree(where).read(rel))
    request = next((node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "request"), None)
    pairs: set[tuple[str, str]] = set()
    if request is None:
        return pairs

    def pair(node) -> Optional[tuple[str, str]]:
        if (isinstance(node, ast.Tuple) and len(node.elts) == 2
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                        for e in node.elts)):
            method, path = node.elts[0].value, node.elts[1].value
            if path.startswith("/"):
                return method.upper(), path
        return None

    for node in ast.walk(request):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            found = pair(comparator)
            if found:
                pairs.add(found)
            elif isinstance(comparator, ast.Tuple):
                pairs.update(p for p in (pair(e) for e in comparator.elts) if p)
    return pairs


# --------------------------------------------------------------------------------------
# :8000 in text: a URL, browser JS, a %-format, a port
# --------------------------------------------------------------------------------------
# Something host-like right before ":8000" (a name, an address, "]" of an IPv6 literal,
# "}" of a template placeholder) -- so "the shim on :8000" in a sentence is not a door.
_HOST_8000 = re.compile(r"[\w\]}.-]:8000(?!\d)")
_JS_8000 = (
    re.compile(r"""['"`]:['"`]\s*\+\s*['"`]?8000(?!\d)"""),     # ':' + 8000
    re.compile(r"""(?i)port\w*['"`]?\s*[:=]\s*['"`]?8000(?!\d)"""),  # port: 8000, PORT=8000
    re.compile(r""":\$\{\s*8000\s*\}"""),                       # `:${8000}`
)
# A literal daemon URL: "http://127.0.0.1:8000", "localhost:8000/api/x".
_LITERAL_DAEMON = re.compile(r"(?:https?://)?[^/\s'\"`]+:8000(?=/|$)")
_PERCENT_PORT = re.compile(r":%[-+ #0-9.]*[dis]")


def mentions_8000(text: str) -> bool:
    return bool(_HOST_8000.search(text) or any(p.search(text) for p in _JS_8000))


def _is_8000(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in (8000, "8000") \
        and type(node.value) is not bool


def _shape(node: ast.JoinedStr) -> str:
    """An f-string's text with each placeholder as ``{}`` (a literal 8000 as itself)."""

    out = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            out.append(str(value.value))
        elif isinstance(value, ast.FormattedValue) and _is_8000(value.value):
            out.append("8000")
        else:
            out.append("{}")
    return "".join(out)


def _percent_8000(node: ast.BinOp) -> bool:
    """``"http://%s:%d/x" % (host, 8000)``"""

    if not (isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str) and _PERCENT_PORT.search(node.left.value)):
        return False
    right = node.right.elts if isinstance(node.right, (ast.Tuple, ast.List)) else [node.right]
    return any(_is_8000(e) for e in right)


def _format_8000(node: ast.Call) -> bool:
    """``"http://{}:{}".format(host, 8000)``"""

    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "format"
            and isinstance(func.value, ast.Constant) and isinstance(func.value.value, str)
            and ":{" in func.value.value
            and any(_is_8000(a) for a in list(node.args) + [k.value for k in node.keywords]))


def _porty(text: object) -> bool:
    return isinstance(text, str) and "port" in text.lower()


def _port_context(module: "Module", node: ast.Constant) -> bool:
    """Is this integer 8000 a port (a URL part, a port parameter, a (host, port) pair)?"""

    parent = module.parents.get(id(node))
    if isinstance(parent, ast.FormattedValue):
        return True
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        if any(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in parent.elts):
            return True   # ("127.0.0.1", 8000)
        node, parent = parent, module.parents.get(id(parent))   # PORTS = (8000, 8070)
    if isinstance(parent, ast.keyword):
        if _porty(parent.arg):
            return True
        call = module.parents.get(id(parent))
        # argparse: add_argument("--daemon-port", type=int, default=8000)
        return parent.arg == "default" and isinstance(call, ast.Call) and any(
            isinstance(a, ast.Constant) and _porty(a.value) for a in call.args)
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        return any(_porty(ast.unparse(t)) for t in targets)
    if isinstance(parent, ast.arguments):
        positional = parent.posonlyargs + parent.args
        for arg, default in zip(positional[len(positional) - len(parent.defaults):],
                                parent.defaults):
            if default is node:
                return _porty(arg.arg)
        for arg, default in zip(parent.kwonlyargs, parent.kw_defaults):
            if default is node:
                return _porty(arg.arg)
        return False
    if isinstance(parent, ast.Compare):
        return any(_porty(ast.unparse(side)) for side in [parent.left, *parent.comparators])
    if isinstance(parent, ast.Dict):
        for key, value in zip(parent.keys, parent.values):
            if value is node and key is not None:
                return _porty(ast.unparse(key))
    return False


# --------------------------------------------------------------------------------------
# reading the code
# --------------------------------------------------------------------------------------
def _callee_name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _env_lookup(node: ast.AST) -> bool:
    """``os.environ.get("REACHY_BASE_URL", ...)``, ``probe.env(...)``, ``environ[...]``."""

    if isinstance(node, ast.Call):
        return any(isinstance(a, ast.Constant) and a.value == "REACHY_BASE_URL"
                   for a in node.args)
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "REACHY_BASE_URL"
    return False


FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


class Module:
    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        parts = list(Path(rel).with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        self.package_init = parts[-1:] == ["__init__"]
        if self.package_init:
            parts = parts[:-1]
        self.name = ".".join(parts)
        self.tree = ast.parse(source, filename=rel)
        self.nodes = list(ast.walk(self.tree))
        self.parents: dict[int, ast.AST] = {}
        for parent in self.nodes:
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent
        self.docstrings = {id(n.value) for n in self.nodes
                           if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                           and isinstance(n.value.value, str)}
        self.calls = [n for n in self.nodes if isinstance(n, ast.Call)]
        self.modules: dict[str, str] = {}             # alias -> dotted module
        self.names: dict[str, tuple[str, str]] = {}   # alias -> (module, name)
        self.top: dict[str, str] = {}                 # `import a.b` binds `a`
        package = self.name if self.package_init else self.name.rpartition(".")[0]
        for node in self.nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        self.top[head] = head
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                for alias in node.names:
                    local = alias.asname or alias.name
                    # `from pkg import mod` imports a module; `from mod import f` a name.
                    self.modules[local] = base + "." + alias.name
                    self.names[local] = (base, alias.name)
        self.defs: set[str] = set()
        self.constants: dict[str, str] = {}
        self.functions: dict[str, FunctionNode] = {}
        self.owner: dict[int, str] = {}   # id(FunctionDef) -> enclosing class name
        self.class_constants: dict[str, dict[str, str]] = {}
        self.base_url_owners: set[str] = set()   # classes that set their own self.base_url
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                self.defs.update(t.id for t in targets if isinstance(t, ast.Name))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.constants[target.id] = node.value.value
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                consts: dict[str, str] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions["%s.%s" % (node.name, item.name)] = item
                        self.owner[id(item)] = node.name
                    elif isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) \
                            and isinstance(item.value.value, str):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                consts[target.id] = item.value.value
                self.class_constants[node.name] = consts
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Assign):
                        for target in inner.targets:
                            elts = target.elts if isinstance(target, ast.Tuple) else [target]
                            if any(isinstance(e, ast.Attribute) and e.attr == "base_url"
                                   and isinstance(e.value, ast.Name) and e.value.id == "self"
                                   for e in elts):
                                self.base_url_owners.add(node.name)
        self._bases: dict[int, set[str]] = {}
        self._assigned: dict[int, dict[str, list[ast.AST]]] = {}
        self._enclosing: dict[int, Optional[FunctionNode]] = {}
        self.refs: Optional[list] = None   # door_refs(), once

    def line(self, node: ast.AST) -> str:
        return "%s:%d" % (self.rel, getattr(node, "lineno", 0))

    def qualify(self, node: ast.AST) -> str:
        """The dotted origin of a name: ``probes.daemon`` -> ``emori_adapter.admin.probes.daemon``."""

        if isinstance(node, ast.Name):
            if node.id in self.names:
                base, name = self.names[node.id]
                return "%s.%s" % (base, name) if base else name
            if node.id in self.modules:
                return self.modules[node.id]
            if node.id in self.top:
                return self.top[node.id]
            if node.id in self.defs:
                return "%s.%s" % (self.name, node.id)
            return ""
        if isinstance(node, ast.Attribute):
            base = self.qualify(node.value)
            return "%s.%s" % (base, node.attr) if base else ""
        return ""

    def enclosing(self, node: ast.AST) -> Optional[FunctionNode]:
        key = id(node)
        if key not in self._enclosing:
            current = self.parents.get(key)
            while current is not None and not isinstance(current, (ast.FunctionDef,
                                                                   ast.AsyncFunctionDef)):
                current = self.parents.get(id(current))
            self._enclosing[key] = current
        return self._enclosing[key]

    def qualname(self, function: Optional[FunctionNode]) -> str:
        if function is None:
            return "<module>"
        cls = self.owner.get(id(function))
        return "%s.%s" % (cls, function.name) if cls else function.name

    def assignments(self, function: Optional[FunctionNode]) -> dict[str, list[ast.AST]]:
        key = id(function) if function is not None else 0
        if key not in self._assigned:
            table: dict[str, list[ast.AST]] = {}
            for node in ast.walk(function if function is not None else self.tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            table.setdefault(target.id, []).append(node.value)
                elif isinstance(node, ast.AnnAssign) and node.value is not None \
                        and isinstance(node.target, ast.Name):
                    table.setdefault(node.target.id, []).append(node.value)
            self._assigned[key] = table
        return self._assigned[key]

    def bases(self, function: Optional[FunctionNode]) -> set[str]:
        """Names bound to the daemon's base URL, in ``function`` and at module level.

        A name is bound to the base when the value assigned to it IS the base (an env
        lookup of REACHY_BASE_URL, ``.reachy_base_url``, ``daemon_base()``, another such
        name, ``.rstrip("/")`` of one) -- not when it is a URL built on it.
        """

        key = id(function) if function is not None else 0
        if key in self._bases:
            return self._bases[key]
        if function is None:
            names = {local for local, (_, name) in self.names.items()
                     if name == "REACHY_BASE_URL"}
            statements = [n for top in self.tree.body
                          if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                  ast.ClassDef))
                          for n in ast.walk(top)]
        else:
            names = set(self.bases(None))
            statements = list(ast.walk(function))
        pairs = []
        for node in statements:
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if targets:
                    pairs.append((targets, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None \
                    and isinstance(node.target, ast.Name):
                pairs.append(([node.target.id], node.value))
        self._bases[key] = names
        for _ in range(5):
            resolver = Resolver(self, function, bases=names)
            grown = False
            for targets, value in pairs:
                if resolver.is_base(value) and not set(targets) <= names:
                    names.update(targets)
                    grown = True
            if not grown:
                break
        return names


_MODULES: dict[tuple[str, str], Module] = {}


def module_for(rel: str, source: str) -> Module:
    """Parsed once per (file, content): the synthetic trees share every untouched file."""

    key = (rel, hashlib.sha1(source.encode("utf-8")).hexdigest())
    module = _MODULES.get(key)
    if module is None:
        module = _MODULES[key] = Module(rel, source)
    return module


def _params(function: FunctionNode, *, method: bool) -> list[str]:
    names = [a.arg for a in function.args.posonlyargs + function.args.args]
    return names[1:] if method and names else names


def _kwonly(function: FunctionNode) -> list[str]:
    return [a.arg for a in function.args.kwonlyargs]


# --------------------------------------------------------------------------------------
# resolving an argument: a literal, a template, a parameter, or a problem
# --------------------------------------------------------------------------------------
BASE = object()      # the daemon's base URL, in a URL being built


@dataclass(frozen=True)
class Value:
    kind: str        # "const" | "param" | "bad"
    text: str = ""   # the literal / template, the parameter name, or why it is bad


def _split_literal_base(combo: list[object]) -> list[object]:
    """``["http://", HOLE, ":8000/x"]`` -> ``[BASE, "/x"]``: a daemon URL spelled out."""

    if not combo or combo[0] is BASE:
        return combo
    text, spans = "", []
    for index, part in enumerate(combo):
        piece = "\x00" if part is HOLE else ("\x01" if part is BASE else str(part))
        spans.append((len(text), len(text) + len(piece), index))
        text += piece
    match = _LITERAL_DAEMON.match(text)
    if match is None:
        return combo
    end = match.end()
    for start, stop, index in spans:
        if start < end <= stop and isinstance(combo[index], str):
            rest = combo[index][end - start:]
            return [BASE] + ([rest] if rest else []) + list(combo[index + 1:])
    return combo


class Resolver:
    """Resolves expressions inside one function of one module."""

    def __init__(self, module: Module, function: Optional[FunctionNode],
                 bases: Optional[set[str]] = None) -> None:
        self.module = module
        self.function = function
        self.bases = module.bases(function) if bases is None else bases
        self.client_class = DAEMON_CLIENTS.get(module.rel, "")
        method = function is not None and module.owner.get(id(function)) is not None
        self.params = set(_params(function, method=method) + _kwonly(function)) \
            if function is not None else set()
        self.cls = module.owner.get(id(function), "") if function is not None else ""

    # -- the daemon base URL ---------------------------------------------------------
    def is_base(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.bases or node.id == "REACHY_BASE_URL"
        if isinstance(node, ast.Attribute):
            if node.attr in ("reachy_base_url", "REACHY_BASE_URL"):
                return True
            return bool(self.client_class and self.cls == self.client_class
                        and node.attr == "base_url" and isinstance(node.value, ast.Name)
                        and node.value.id == "self")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _LITERAL_DAEMON.match(node.value)
            return match is not None and node.value[match.end():] in ("", "/")
        if isinstance(node, ast.Call):
            func = node.func
            if self.module.qualify(func) == PROBES_DAEMON_BASE or _callee_name(func) == \
                    "daemon_base":
                return True
            # `REACHY.rstrip("/")` is still the base.
            if isinstance(func, ast.Attribute) and func.attr in ("rstrip", "strip"):
                return self.is_base(func.value)
            return _env_lookup(node)
        if isinstance(node, ast.Subscript):
            return _env_lookup(node)
        if isinstance(node, ast.BoolOp):
            return any(self.is_base(value) for value in node.values)
        if isinstance(node, ast.IfExp):
            return self.is_base(node.body) or self.is_base(node.orelse)
        return False

    # -- string parts ---------------------------------------------------------------
    def parts(self, node: ast.AST, depth: int = 0) -> list[list[object]]:
        """Every way ``node`` can read, each as a list of str / HOLE / BASE parts."""

        return [_split_literal_base(c) for c in self._parts(node, depth)]

    def _parts(self, node: ast.AST, depth: int) -> list[list[object]]:
        if depth > 8:
            return [[HOLE]]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [[node.value]]
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return [[str(node.value)]]
        if self.is_base(node):
            return [[BASE]]
        if isinstance(node, ast.JoinedStr):
            combos: list[list[object]] = [[]]
            for value in node.values:
                piece = value.value if isinstance(value, ast.FormattedValue) else value
                options = self._parts(piece, depth + 1)
                combos = [c + o for c in combos for o in options]
            return combos
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._parts(node.left, depth + 1), self._parts(node.right, depth + 1)
            return [a + b for a in left for b in right]
        if isinstance(node, ast.IfExp):
            return self._parts(node.body, depth + 1) + self._parts(node.orelse, depth + 1)
        if isinstance(node, ast.Name):
            if node.id in self.params:
                return [[HOLE]]
            values = self.module.assignments(self.function).get(node.id, []) \
                if self.function is not None else []
            if values:
                out: list[list[object]] = []
                for value in values:
                    out.extend(self._parts(value, depth + 1))
                return out
            if node.id in self.module.constants:
                return [[self.module.constants[node.id]]]
            return [[HOLE]]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in ("self", "cls") and self.cls:
            constant = self.module.class_constants.get(self.cls, {}).get(node.attr)
            if constant is not None:
                return [[constant]]
        return [[HOLE]]

    def path(self, node: Optional[ast.AST]) -> list[Value]:
        """A path argument: a template starting with "/", or a whole parameter."""

        if node is None:
            return [Value("bad", "no path argument")]
        if isinstance(node, ast.Name) and node.id in self.params:
            return [Value("param", node.id)]
        out = []
        for combo in self.parts(node):
            if any(part is BASE for part in combo):
                out.append(Value("bad", "a daemon URL where a path was expected"))
                continue
            text = "".join(HOLE if part is HOLE else str(part) for part in combo)
            if not text.startswith("/"):
                out.append(Value("bad", "path %r does not start with a literal '/'" % text))
            else:
                out.append(Value("const", text))
        return out

    def method(self, node: Optional[ast.AST]) -> list[Value]:
        if node is None:
            return [Value("bad", "no method argument")]
        if isinstance(node, ast.Name) and node.id in self.params:
            return [Value("param", node.id)]
        out = []
        for combo in self.parts(node):
            if len(combo) == 1 and isinstance(combo[0], str):
                out.append(Value("const", combo[0].upper()))
            else:
                out.append(Value("bad", "method is not a literal"))
        return out


# --------------------------------------------------------------------------------------
# sinks and wrappers
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Sink:
    """Something that sends an HTTP request to the daemon when called."""

    module: str                 # dotted module of the function, or "" for a builtin sink
    qualname: str               # "network._wifi", "HttpRobotClient._post"
    method: Value               # const, or param name in the callee
    path: Value
    via: str = ""
    host: str = ""


PROBES_DAEMON_SINK = Sink("", "probes.daemon", Value("param", "method"),
                          Value("param", "path"))


def _arg(call: ast.Call, function: Optional[FunctionNode], name: str,
         *, method: bool) -> Optional[ast.AST]:
    """The expression a call passes for parameter ``name`` of ``function``."""

    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    if function is None:
        return None
    params = _params(function, method=method)
    if name in params:
        index = params.index(name)
        if index < len(call.args) and not any(isinstance(a, ast.Starred)
                                              for a in call.args[: index + 1]):
            return call.args[index]
    return None


def _host_dispatch(module: Module, function: FunctionNode,
                   method_param: str, path_param: str) -> str:
    """"netman"/"mixer" when this wrapper answers from emori-admin's own router.

    It must (a) ask the selector (``host_backend.current``/``decision``), (b) know a
    route-missing 404 when it sees one (``host_backend.route_missing``) and (c) hand
    the SAME method and path to ``<backend>.request``. Without all three a daemon-only
    path through it still 404s on v2.
    """

    attrs = {(n.value.id, n.attr) for n in ast.walk(function)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    selector = any(module.modules.get(alias, "").endswith("admin.host_backend")
                   and attr in ("current", "decision") for alias, attr in attrs)
    missing = any(module.modules.get(alias, "").endswith("admin.host_backend")
                  and attr == "route_missing" for alias, attr in attrs)
    if not (selector and missing):
        return ""
    for node in ast.walk(function):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request" and isinstance(node.func.value, ast.Name)):
            continue
        target = module.modules.get(node.func.value.id, "")
        backend = target.rsplit(".", 1)[-1]
        if backend not in HOST_BACKENDS or not target.endswith("admin." + backend):
            continue
        args = node.args
        if (len(args) >= 2 and isinstance(args[0], ast.Name) and args[0].id == method_param
                and isinstance(args[1], ast.Name) and args[1].id == path_param):
            return backend
    return ""


def _sink_key(module: Module, function: Optional[FunctionNode], func: ast.AST) -> str:
    """The sinks-table key a call's ``func`` would have: its origin, or Class.method."""

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id in ("self", "cls") and function is not None:
        cls = module.owner.get(id(function))
        if cls:
            return "%s.%s.%s" % (module.name, cls, func.attr)
    return module.qualify(func)


def _client_sink(module: Module, call: ast.Call, function: Optional[FunctionNode],
                 clients: dict[str, set[str]]) -> tuple[Optional[Sink], str]:
    """``self._http.post(path, ...)`` inside a daemon client class."""

    func = call.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "self"
            and function is not None):
        return None, ""
    cls = module.owner.get(id(function), "")
    if func.value.attr not in clients.get(cls, set()):
        return None, ""
    verb = func.attr
    if verb in HTTP_VERBS:
        return Sink("", "httpx.Client.%s" % verb, Value("const", verb.upper()),
                    Value("param", "url")), ""
    if verb == "request":
        return Sink("", "httpx.Client.request", Value("param", "method"),
                    Value("param", "url")), ""
    if verb in CLIENT_QUIET:
        return None, ""
    return None, "%s calls self.%s.%s(); teach the contract test what that sends" % (
        module.line(call), func.value.attr, verb)


def _client_attrs(module: Module, cls_name: str) -> set[str]:
    """``self.X = httpx.Client(...)`` attributes of a daemon client class."""

    attrs: set[str] = set()
    for qual, function in module.functions.items():
        if not qual.startswith(cls_name + "."):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if _callee_name(node.value.func) != "Client":
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                            and target.value.id == "self":
                        attrs.add(target.attr)
    return attrs


def _in_sink_body(module: Module, function: Optional[FunctionNode]) -> bool:
    return module.rel == PROBES and function is not None \
        and function.name in ("daemon", "daemon_base")


# --------------------------------------------------------------------------------------
# URLs built on the base
# --------------------------------------------------------------------------------------
def _url_method(module: Module, call: ast.Call, position: int,
                function: Optional[FunctionNode]) -> Optional[Value]:
    """The HTTP method of ``call`` when the URL is its argument at ``position``."""

    name = _callee_name(call.func)
    if name in HTTP_VERBS and position == 0:
        return Value("const", name.upper())
    if name == "http" and position == 0:
        # readycheck's SystemProbe.http is a GET (httpx.get), and so is every fake.
        return Value("const", "GET")
    if name == "request" and position == 1 and call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return Value("const", first.value.upper())
        if isinstance(first, ast.Name) and function is not None \
                and first.id in Resolver(module, function).params:
            return Value("param", first.id)
    return None


def _climb_strings(module: Module, node: ast.AST) -> tuple[ast.AST, Optional[ast.AST]]:
    """Up through the strings ``node`` is part of: (the whole expression, its consumer)."""

    parent = module.parents.get(id(node))
    while isinstance(parent, (ast.FormattedValue, ast.JoinedStr)) or (
            isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))):
        node, parent = parent, module.parents.get(id(parent))
    return node, parent


def _consumers(module: Module, node: ast.AST, function: Optional[FunctionNode],
               ) -> list[tuple[ast.AST, Optional[ast.AST]]]:
    """Where the value of ``node`` goes, following one assignment to a local name."""

    parent = module.parents.get(id(node))
    if isinstance(parent, ast.Assign) and parent.value is node \
            and all(isinstance(t, ast.Name) for t in parent.targets):
        names = {t.id for t in parent.targets}
        scope = function if function is not None else module.tree
        return [_climb_strings(module, use) for use in ast.walk(scope)
                if isinstance(use, ast.Name) and use.id in names
                and isinstance(use.ctx, ast.Load)]
    return [(node, parent)]


def _use_method(module: Module, expression: ast.AST, consumer: Optional[ast.AST],
                function: Optional[FunctionNode]) -> tuple[Optional[ast.Call], Optional[Value]]:
    if isinstance(consumer, ast.keyword):
        call = module.parents.get(id(consumer))
        if consumer.arg != "url" or not isinstance(call, ast.Call):
            return None, None
        return call, _url_method(module, call, 1 if _callee_name(call.func) == "request"
                                 else 0, function)
    if isinstance(consumer, ast.Call) and any(a is expression for a in consumer.args):
        position = next(i for i, a in enumerate(consumer.args) if a is expression)
        return consumer, _url_method(module, consumer, position, function)
    return None, None


def _param_tail(node: ast.AST, resolver: Resolver) -> str:
    """``f"{base}{path}"`` / ``base + path`` with ``path`` a parameter: its name."""

    if isinstance(node, ast.JoinedStr) and len(node.values) == 2 \
            and all(isinstance(v, ast.FormattedValue) for v in node.values):
        head, tail = node.values[0].value, node.values[1].value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        head, tail = node.left, node.right
    else:
        return ""
    if resolver.is_base(head) and isinstance(tail, ast.Name) and tail.id in resolver.params:
        return tail.id
    return ""


@dataclass
class Analysis:
    calls: set = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    sinks: dict[str, Sink] = field(default_factory=dict)
    consumed: set[int] = field(default_factory=set)   # id() of URL nodes fully enumerated


def _url_builds(module: Module, analysis: Analysis) -> None:
    """URLs built on the daemon's base and handed to an HTTP call (readycheck)."""

    for node in module.nodes:
        if isinstance(node, (ast.JoinedStr, ast.BinOp)):
            pass
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in module.docstrings and _LITERAL_DAEMON.match(node.value):
            pass
        else:
            continue
        parent = module.parents.get(id(node))
        if isinstance(parent, (ast.JoinedStr, ast.FormattedValue, ast.BinOp)):
            continue  # part of a larger string; the outer node is looked at instead
        function = module.enclosing(node)
        if _in_sink_body(module, function):
            continue
        resolver = Resolver(module, function)
        combos = [c for c in resolver.parts(node) if c and c[0] is BASE and len(c) > 1]
        if not combos:
            continue  # not a URL on the base (or the bare base itself)
        param = _param_tail(node, resolver)
        literal = sorted({"".join(HOLE if p is HOLE else ("{base}" if p is BASE else str(p))
                                  for p in c[1:])
                          for c in combos if isinstance(c[1], str) and c[1].startswith("/")})
        unreadable = any(not (isinstance(c[1], str) and c[1].startswith("/"))
                         and not (param and c[1:] == [HOLE]) for c in combos)
        uses = [(_use_method(module, expression, consumer, function), expression, consumer)
                for expression, consumer in _consumers(module, node, function)]
        recognised = [(call, method) for (call, method), _, _ in uses if method is not None]
        for call, method in recognised:
            if unreadable:
                analysis.problems.append(
                    "%s: cannot tell which daemon route this calls (a URL on the daemon's "
                    "base whose path is neither a literal '/...' nor a parameter of %s); "
                    "keep daemon paths literal" % (module.line(call),
                                                   module.qualname(function)))
                continue
            if method.kind == "const":
                for template in literal:
                    analysis.calls.add(Call(method.text, template, module.line(call)))
            wrapper_paths = ([Value("param", param)] if param else []) + (
                [Value("const", t) for t in literal] if method.kind == "param" else [])
            if not wrapper_paths:
                continue
            if function is None:
                analysis.problems.append("%s: a daemon URL wrapper at module level?"
                                         % module.line(call))
                continue
            if len(wrapper_paths) > 1:
                analysis.problems.append("%s: %s is a daemon wrapper with more than one "
                                         "path; split it" % (module.line(call),
                                                             module.qualname(function)))
                continue
            qual = module.qualname(function)
            key = "%s.%s" % (module.name, qual)
            analysis.sinks.setdefault(key, Sink(
                module.name, qual, method, wrapper_paths[0],
                via="%s.%s" % (module.name.rsplit(".", 1)[-1], qual)))
        if uses and len(recognised) == len(uses) and not unreadable:
            analysis.consumed.add(id(node))


# --------------------------------------------------------------------------------------
# enumeration
# --------------------------------------------------------------------------------------
def _load(where: Union[Path, Tree]) -> list[Module]:
    tree = _tree(where)
    return [module_for(rel, tree.read(rel)) for rel in tree.files(SRC, (".py",))]


def analyse(modules: list[Module]) -> Analysis:
    """Every HTTP call to the daemon under ``src/emori_adapter`` (see module doc)."""

    by_name = {module.name: module for module in modules}
    analysis = Analysis()
    problems = analysis.problems
    sinks = analysis.sinks
    sinks[PROBES_DAEMON] = PROBES_DAEMON_SINK
    clients: dict[str, dict[str, set[str]]] = {}
    for module in modules:
        cls = DAEMON_CLIENTS.get(module.rel)
        if cls:
            attrs = _client_attrs(module, cls)
            if not attrs:
                problems.append("%s: %s holds no httpx.Client any more; teach the "
                                "contract test how it reaches the daemon" % (module.rel, cls))
            clients[module.rel] = {cls: attrs}
    # The client's own httpx.Client is the client's business: a caller that reaches in
    # (`robot._http.get("/wifi/status")`) is a daemon call nobody enumerates.
    private = {attr for by_class in clients.values() for attrs in by_class.values()
               for attr in attrs}
    for module in modules:
        if module.rel in DAEMON_CLIENTS:
            continue
        for node in module.nodes:
            if isinstance(node, ast.Attribute) and node.attr in private and not (
                    isinstance(node.value, ast.Name) and node.value.id == "self"):
                problems.append("%s reaches into the robot client's own HTTP client (.%s); "
                                "add a method to HttpRobotClient instead"
                                % (module.line(node), node.attr))

    # URL wrappers first: they are sinks like probes.daemon, found by shape, not by call.
    for module in modules:
        _url_builds(module, analysis)

    def record(module: Module, call: ast.Call, function, sink: Sink) -> None:
        method_param = sink.method.text if sink.method.kind == "param" else ""
        path_param = sink.path.text if sink.path.kind == "param" else ""
        callee_fn, is_method = None, False
        if sink.module:
            owner = by_name[sink.module]
            callee_fn = owner.functions.get(sink.qualname)
            is_method = callee_fn is not None and id(callee_fn) in owner.owner
        resolver = Resolver(module, function)
        if sink.method.kind == "const":
            methods = [sink.method]
        else:
            expr = _arg(call, callee_fn, method_param, method=is_method) if callee_fn \
                else (call.args[0] if call.args else None)
            methods = resolver.method(expr)
        if sink.path.kind == "const":
            paths = [sink.path]
        else:
            if callee_fn is not None:
                expr = _arg(call, callee_fn, path_param, method=is_method)
            elif sink.qualname == "httpx.Client.request" or sink is PROBES_DAEMON_SINK:
                expr = next((k.value for k in call.keywords
                             if k.arg in ("url", "path")), None)
                if expr is None and len(call.args) > 1:
                    expr = call.args[1]
            else:
                expr = next((k.value for k in call.keywords if k.arg == "url"), None)
                if expr is None and call.args:
                    expr = call.args[0]
            paths = resolver.path(expr)
        for value in methods + paths:
            if value.kind == "bad":
                problems.append("%s: cannot tell which daemon route this calls (%s); keep "
                                "daemon methods and paths literal" % (module.line(call),
                                                                      value.text))
        methods = [v for v in methods if v.kind != "bad"]
        paths = [v for v in paths if v.kind != "bad"]
        if not methods or not paths:
            return
        if any(v.kind == "param" for v in methods + paths):
            if function is None:
                problems.append("%s: a daemon call at module level with a parameter?"
                                % module.line(call))
                return
            qual = module.qualname(function)
            key = "%s.%s" % (module.name, qual)
            if key in sinks:
                return
            method_value = methods[0] if len(methods) == 1 else None
            path_value = paths[0] if len(paths) == 1 else None
            if method_value is None or path_value is None:
                problems.append("%s: %s is a daemon wrapper with more than one method or "
                                "path; split it" % (module.line(call), qual))
                return
            host = sink.host
            if not host and method_value.kind == "param" and path_value.kind == "param":
                host = _host_dispatch(module, function, method_value.text, path_value.text)
            via = "%s.%s" % (module.name.rsplit(".", 1)[-1], qual)
            sinks[key] = Sink(module.name, qual, method_value, path_value,
                              via=via + (" -> " + sink.via if sink.via else ""), host=host)
            return
        for m in methods:
            for p in paths:
                analysis.calls.add(Call(m.text, p.text, module.line(call), sink.via,
                                        sink.host))

    # Fixpoint: a function that passes its own parameters into a sink is a sink too.
    for _ in range(10):
        before = len(sinks)
        for module in modules:
            client_attrs = clients.get(module.rel, {})
            for node in module.calls:
                function = module.enclosing(node)
                if _in_sink_body(module, function):
                    continue  # the sink's own body
                sink = sinks.get(_sink_key(module, function, node.func))
                if sink is None:
                    sink, problem = _client_sink(module, node, function, client_attrs)
                    if problem:
                        problems.append(problem)
                if sink is not None:
                    record(module, node, function, sink)
        if len(sinks) == before:
            break

    # A sink handed around as a value is a call nobody can read.
    for module in modules:
        for node in module.nodes:
            if not (isinstance(node, (ast.Name, ast.Attribute))
                    and isinstance(node.ctx, ast.Load)):
                continue
            parent = module.parents.get(id(node))
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            if isinstance(parent, ast.Attribute):
                continue  # `probes` in `probes.daemon`: the outer node is looked at
            key = _sink_key(module, module.enclosing(node), node)
            if key in sinks:
                problems.append(
                    "%s passes %s around as a value (functools.partial, a callback, an "
                    "alias): the contract test reads direct calls only, so the route it "
                    "is called with is never checked -- call it directly"
                    % (module.line(node), ast.unparse(node)))
    # The fixpoint revisits every call on each pass; say each problem once.
    analysis.problems = list(dict.fromkeys(problems))
    return analysis


def enumerate_calls(where: Union[Path, Tree], modules: Optional[list[Module]] = None,
                    ) -> tuple[list[Call], list[str]]:
    analysis = analyse(_load(where) if modules is None else modules)
    return (sorted(analysis.calls, key=lambda c: (c.file, c.where, c.method, c.path)),
            analysis.problems)


# --------------------------------------------------------------------------------------
# doors, and every use of the address inside them
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Ref:
    """One reference to the daemon (its address, a way to call it)."""

    kind: str
    node: ast.AST
    use: bool = True     # False: the door itself (an import, a declaration), not a use


def _foreign_base_url(module: Module, node: ast.Attribute) -> bool:
    """``robot.base_url`` -- the robot client's address read from outside it."""

    if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
        function = module.enclosing(node)
        cls = module.owner.get(id(function), "") if function is not None else ""
        return not (cls and (cls in module.base_url_owners
                             or cls == DAEMON_CLIENTS.get(module.rel)))
    return True


def door_refs(module: Module) -> list[Ref]:
    if module.refs is None:
        module.refs = _door_refs(module)
    return module.refs


def _door_refs(module: Module) -> list[Ref]:
    refs: list[Ref] = []
    for node in module.nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if isinstance(node, ast.ImportFrom):
                    base, name = module.names[alias.asname or alias.name]
                    origin = "%s.%s" % (base, name)
                else:
                    name, origin = alias.name.rsplit(".", 1)[-1], alias.name
                if name == "REACHY_BASE_URL":
                    refs.append(Ref("imports REACHY_BASE_URL", node, use=False))
                if origin == PROBES_MODULE or origin.startswith(PROBES_MODULE + "."):
                    refs.append(Ref("imports admin.probes", node, use=False))
            continue
        parent = module.parents.get(id(node))
        if isinstance(node, ast.Constant):
            if id(node) in module.docstrings:
                continue
            if node.value == "REACHY_BASE_URL":
                refs.append(Ref("REACHY_BASE_URL", node))
            elif isinstance(node.value, str) and not isinstance(parent, ast.JoinedStr) \
                    and mentions_8000(node.value):
                refs.append(Ref(":8000", node))
            elif node.value == 8000 and type(node.value) is int and _port_context(module, node):
                refs.append(Ref("port 8000", node))
        elif isinstance(node, ast.JoinedStr):
            if not isinstance(parent, ast.JoinedStr) and mentions_8000(_shape(node)):
                refs.append(Ref(":8000", node))
        elif isinstance(node, ast.BinOp):
            if _percent_8000(node):
                refs.append(Ref(":8000", node))
        elif isinstance(node, ast.Call):
            if _format_8000(node):
                refs.append(Ref(":8000", node))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            origin = module.qualify(node)
            if node.id == "REACHY_BASE_URL":
                refs.append(Ref("REACHY_BASE_URL", node))
            elif origin == PROBES_DAEMON:
                refs.append(Ref("probes.daemon", node))
            elif origin == PROBES_DAEMON_BASE or node.id == "daemon_base":
                refs.append(Ref("daemon_base", node))
            elif node.id in module.bases(module.enclosing(node)):
                refs.append(Ref("base", node))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr in ("REACHY_BASE_URL", "reachy_base_url"):
                refs.append(Ref("REACHY_BASE_URL" if node.attr.isupper()
                                else ".reachy_base_url", node))
            elif node.attr == "base_url" and _foreign_base_url(module, node):
                refs.append(Ref(".base_url", node))
            else:
                origin = module.qualify(node)
                if origin == PROBES_DAEMON:
                    refs.append(Ref("probes.daemon", node))
                elif origin == PROBES_DAEMON_BASE or node.attr == "daemon_base":
                    refs.append(Ref("daemon_base", node))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "reachy_base_url":
            refs.append(Ref(".reachy_base_url", node, use=False))   # a declaration
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "daemon_base" and module.rel != PROBES:
            refs.append(Ref("daemon_base", node, use=False))
    return refs


def door_files(where: Union[Path, Tree], modules: Optional[list[Module]] = None,
               ) -> dict[str, set[str]]:
    """Every file that can learn the daemon's address or call it, and how."""

    found: dict[str, set[str]] = {}
    for module in (_load(where) if modules is None else modules):
        kinds = {ref.kind for ref in door_refs(module) if ref.kind != "base"}
        if kinds:
            found[module.rel] = kinds
    return found


def door_problems(where: Union[Path, Tree], doors: dict[str, str] = DOORS,
                  modules: Optional[list[Module]] = None) -> list[str]:
    found = door_files(where, modules)
    problems = []
    for rel, kinds in sorted(found.items()):
        if rel not in doors:
            problems.append(
                "%s reaches the daemon (%s) and is not in DOORS: a new way to :8000. Route "
                "the call through probes.daemon()/HttpRobotClient, or teach "
                "tests/test_daemon_contract.py to enumerate it and add a DOORS entry"
                % (rel, ", ".join(sorted(kinds))))
    for rel in sorted(set(doors) - set(found)):
        problems.append("DOORS lists %s, which no longer references the daemon's address: "
                        "remove the entry" % rel)
    return problems


def _climb(module: Module, node: ast.AST, consumed: set[int],
           ) -> tuple[Optional[ast.AST], Optional[ast.AST], bool]:
    """From a reference up to what it is part of: (expression, consumer, through text).

    ``(None, None, _)`` when it is part of a URL whose every use was enumerated.
    """

    text = False
    while True:
        if id(node) in consumed:
            return None, None, text
        parent = module.parents.get(id(node))
        if parent is None:
            return node, None, text
        if isinstance(parent, (ast.FormattedValue, ast.JoinedStr)) or (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))):
            text, node = True, parent
            continue
        if isinstance(parent, ast.BoolOp) or (isinstance(parent, ast.IfExp)
                                              and node is not parent.test):
            node = parent
            continue
        if isinstance(parent, ast.Attribute) and parent.attr in ("rstrip", "strip"):
            call = module.parents.get(id(parent))
            if isinstance(call, ast.Call) and call.func is parent:
                node = call
                continue
        if isinstance(parent, ast.Call) and any(a is node for a in parent.args) \
                and _env_lookup(parent):
            node = parent
            continue
        if isinstance(parent, ast.Subscript) and parent.slice is node and _env_lookup(parent):
            node = parent
            continue
        if isinstance(parent, ast.Lambda):
            node = parent
            continue
        if isinstance(parent, ast.keyword) and parent.arg == "default_factory":
            node = module.parents[id(parent)]
            continue
        return node, parent, text


def _context(module: Module, expression: ast.AST, consumer: Optional[ast.AST],
             text: bool) -> str:
    if isinstance(consumer, ast.Call):
        what = "%s()" % ast.unparse(consumer.func)
    elif isinstance(consumer, ast.keyword):
        call = module.parents.get(id(consumer))
        what = "%s(%s=)" % (ast.unparse(call.func) if isinstance(call, ast.Call) else "?",
                            consumer.arg or "**")
    elif isinstance(consumer, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = consumer.targets if isinstance(consumer, ast.Assign) else [consumer.target]
        what = "assign to " + ", ".join(ast.unparse(t) for t in targets)
    elif consumer is None:
        what = "module"
    else:
        what = type(consumer).__name__.lower()
    return ("f-string in " if text else "") + what


def _accounted(module: Module, function: Optional[FunctionNode], expression: ast.AST,
               consumer: Optional[ast.AST]) -> bool:
    """A binding of the base, or the address handed to the robot client."""

    if isinstance(consumer, ast.Assign) and consumer.value is expression:
        names = [t.id for t in consumer.targets if isinstance(t, ast.Name)]
        return len(names) == len(consumer.targets) and set(names) <= module.bases(function)
    if isinstance(consumer, ast.AnnAssign) and consumer.value is expression \
            and isinstance(consumer.target, ast.Name):
        return consumer.target.id == "reachy_base_url" \
            or consumer.target.id in module.bases(function)
    if isinstance(consumer, ast.Call) and consumer.args and consumer.args[0] is expression:
        return module.qualify(consumer.func) in CLIENT_ORIGINS
    if isinstance(consumer, ast.keyword) and consumer.arg == "base_url":
        call = module.parents.get(id(consumer))
        return isinstance(call, ast.Call) and module.qualify(call.func) in CLIENT_ORIGINS
    return False


def use_problems(modules: list[Module], analysis: Analysis, doors: dict[str, str],
                 door_uses: dict[tuple[str, str, str], str],
                 route_probes: Optional[dict[tuple[str, str, str], str]] = None,
                 ) -> list[str]:
    """Inside every door, every use of the daemon's address must be accounted for."""

    route_probes = ROUTE_PROBES if route_probes is None else route_probes
    problems: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for module in modules:
        if module.rel not in doors:
            continue  # door_problems already says so
        client = DAEMON_CLIENTS.get(module.rel, "")
        for ref in door_refs(module):
            if not ref.use:
                continue
            node = ref.node
            function = module.enclosing(node)
            if _in_sink_body(module, function):
                continue
            if client and function is not None and module.owner.get(id(function)) == client:
                continue  # the robot client's own internals; its calls are enumerated
            parent = module.parents.get(id(node))
            if ref.kind in ("probes.daemon", "daemon_base"):
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    if ref.kind == "daemon_base":   # probes.daemon is reported in analyse()
                        problems.append(
                            "%s passes %s around as a value: call it where the URL is "
                            "built" % (module.line(node), ast.unparse(node)))
                    continue
                if ref.kind == "probes.daemon":
                    continue  # a direct call: enumerated
                node = parent   # daemon_base(): the call is the base
            expression, consumer, text = _climb(module, node, analysis.consumed)
            if expression is None or _accounted(module, function, expression, consumer):
                continue
            key = (module.rel, module.qualname(function),
                   _context(module, expression, consumer, text))
            if key in door_uses or key in route_probes:
                used.add(key)
                continue
            combos = Resolver(module, function).parts(expression) if text else []
            url = any(c and c[0] is BASE and len(c) > 1 for c in combos)
            then = ""
            if url and isinstance(consumer, ast.Assign):
                onward = [_context(module, e, c, False)
                          for e, c in _consumers(module, expression, function)]
                then = "; the name then goes to %s" % (", ".join(onward) or "nothing")
            problems.append(
                "%s: %s %s reaches %s, which is not a daemon call the contract test can "
                "read%s. If it is a call, make it with probes.daemon()/HttpRobotClient or "
                "an HTTP call on the base with a literal path; if it only shows the "
                "address, add DOOR_USES[%r] = '<why it sends nothing>'"
                % (module.line(ref.node), "a daemon URL built on" if url else "the daemon's",
                   ref.kind if ref.kind != "base" else "base URL (%s)" % ast.unparse(ref.node),
                   key[2], " (no recognised HTTP call sends this URL%s)" % then if url else "",
                   key))
    for key in sorted(set(door_uses) - used):
        problems.append("DOOR_USES %r matches no use any more: remove it" % (key,))
    for key in sorted(set(route_probes) - used):
        problems.append("ROUTE_PROBES %r matches no use any more: remove it" % (key,))
    return problems


# --------------------------------------------------------------------------------------
# scripts/ and deploy/: what talks to :8000 outside the package
# --------------------------------------------------------------------------------------
_SHELL_PORT = re.compile(r"(?i)(?:port\w*\s*[=:]\s*|--?port\s+)['\"]?8000(?!\d)")
_SHELL_ROUTE = re.compile(r":8000(/[\w\-./{}]*)")
_SHELL_METHOD = re.compile(r"(?:-X|--request)\s*['\"]?([A-Za-z]+)")
_SCRIPT_KINDS = frozenset({":8000", "port 8000", "REACHY_BASE_URL",
                           "imports REACHY_BASE_URL", "imports admin.probes",
                           "probes.daemon", "daemon_base", ".reachy_base_url"})


def _shell_code(lines: list[str]) -> Iterable[tuple[int, str]]:
    """(line number, the code on it) for a .sh/.ps1, comments left out."""

    block = False
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if block:
            block = "#>" not in line
            continue
        if line.startswith("<#"):
            block = "#>" not in line
            continue
        if not line or line.startswith("#"):
            continue
        yield number, re.sub(r"\s#.*$", "", line)


def script_refs(tree: Tree) -> dict[str, list[tuple[int, str]]]:
    """Every file under scripts/ and deploy/ that talks to :8000: (line, code) each."""

    found: dict[str, list[tuple[int, str]]] = {}
    for top, suffixes in SCRIPT_DIRS:
        for rel in tree.files(top, suffixes):
            text = tree.read(rel)
            lines = text.splitlines()
            hits: list[tuple[int, str]] = []
            if rel.endswith(".py"):
                try:
                    module = module_for(rel, text)
                except SyntaxError:
                    found[rel] = [(0, "does not parse")]
                    continue
                numbers = sorted({getattr(r.node, "lineno", 0) for r in door_refs(module)
                                  if r.kind in _SCRIPT_KINDS})
                hits = [(n, lines[n - 1].strip() if 0 < n <= len(lines) else "")
                        for n in numbers]
            else:
                hits = [(n, code) for n, code in _shell_code(lines)
                        if mentions_8000(code) or _SHELL_PORT.search(code)
                        or "REACHY_BASE_URL" in code]
            if hits:
                found[rel] = hits
    return found


def script_problems(tree: Tree, script_doors: dict[str, str],
                    routes: list[Route]) -> list[str]:
    found = script_refs(tree)
    problems = []
    for rel, hits in sorted(found.items()):
        if rel not in script_doors:
            problems.append(
                "%s talks to the daemon's :8000 and is not in SCRIPT_DOORS: %s. If it runs "
                "on a robot, every route it calls must exist on v2 too; add an entry saying "
                "what it does there" % (rel, "; ".join("line %d: %s" % (n, code[:90])
                                                      for n, code in hits[:5])))
        if not rel.startswith(ROBOT_SCRIPTS) or rel.endswith(".py"):
            continue
        for number, code in hits:
            method_match = _SHELL_METHOD.search(code)
            method = method_match.group(1).upper() if method_match else (
                "POST" if re.search(r"\s(?:-d|--data\S*)\s", code) else "GET")
            for path in _SHELL_ROUTE.findall(code):
                path = path.rstrip(".")
                if not any(route.serves(method, path.split("$", 1)[0] or "/")
                           for route in routes):
                    problems.append(
                        "%s:%d calls %s %s on :8000, which a v2 robot does not serve (a "
                        "404 on v2, from a script): ask the robot node's shim for a "
                        "route it has, or teach the shim this one"
                        % (rel, number, method, path))
    for rel in sorted(set(script_doors) - set(found)):
        problems.append("SCRIPT_DOORS lists %s, which no longer talks to :8000: remove it"
                        % rel)
    return problems


# --------------------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------------------
def _is_test(tree: Tree, test: str) -> bool:
    test_file, _, test_name = test.partition("::")
    return bool(test_name and tree.exists(test_file) and re.search(
        r"^def %s\(" % re.escape(test_name), tree.read(test_file), re.M))


def _allowed_calls(tree: Tree, label: str, allow: dict, calls: list[Call],
                   ) -> Iterable[tuple[str, str, list[Call], list[str]]]:
    """Each entry of a V1_ONLY/V2_ONLY table: the calls it matches, and what is wrong
    with it (a malformed entry matches nothing)."""

    for (method, path, rel), entry in sorted(allow.items()):
        problems: list[str] = []
        if not (isinstance(entry, tuple) and len(entry) == 2 and all(entry)):
            problems.append("%s %s %s: needs (why, 'tests/<file>.py::<test>')"
                            % (label, method, path))
            yield method, path, [], problems
            continue
        if not _is_test(tree, entry[1]):
            problems.append("%s %s %s names %r, which is not a test in this tree"
                            % (label, method, path, entry[1]))
        used = [c for c in calls if (c.method, c.path, c.file) == (method, path, rel)]
        if not used:
            problems.append("%s %s %s (%s) matches no call any more: remove it"
                            % (label, method, path, rel))
        yield method, path, used, problems


def allowlist_problems(tree: Tree, allow: dict, calls: Iterable[Call],
                       report: Report) -> list[str]:
    problems = []
    for method, path, used, found in _allowed_calls(tree, "V1_ONLY", allow, list(calls)):
        problems.extend(found)
        if used and all(report.verdict(c) for c in used):
            problems.append("V1_ONLY %s %s is served on v2 now: remove it" % (method, path))
    return problems


def v2_only_problems(tree: Tree, v2_only: dict, calls: Iterable[Call],
                     report: Report) -> list[str]:
    problems = []
    for method, path, used, found in _allowed_calls(tree, "V2_ONLY", v2_only, list(calls)):
        problems.extend(found)
        if used and not all(report.verdict(c) == "shim" for c in used):
            problems.append("V2_ONLY %s %s is not served by the v2 shim either: it is not "
                            "v2-only, it is unserved" % (method, path))
    return problems


def check(root: Path = ROOT, *, allow: Optional[dict] = None,
          doors: Optional[dict] = None, door_uses: Optional[dict] = None,
          script_doors: Optional[dict] = None, v2_only: Optional[dict] = None,
          route_probes: Optional[dict] = None,
          overlay: Optional[dict[str, str]] = None) -> Report:
    tree = Tree(root, overlay)
    allow = V1_ONLY if allow is None else allow
    doors = DOORS if doors is None else doors
    door_uses = DOOR_USES if door_uses is None else door_uses
    script_doors = SCRIPT_DOORS if script_doors is None else script_doors
    v2_only = V2_ONLY if v2_only is None else v2_only
    report = Report()
    report.routes, shim_problems = shim_routes(tree)
    report.problems.extend(shim_problems)
    report.host_tables = {name: host_table(tree, rel) for name, rel in HOST_BACKENDS.items()
                          if tree.exists(rel)}
    modules = _load(tree)
    analysis = analyse(modules)
    report.calls = sorted(analysis.calls, key=lambda c: (c.file, c.where, c.method, c.path))
    report.problems.extend(analysis.problems)
    report.problems.extend(door_problems(tree, doors, modules))
    report.problems.extend(use_problems(modules, analysis, doors, door_uses, route_probes))
    report.problems.extend(script_problems(tree, script_doors, report.routes))
    for call in report.calls:
        if report.verdict(call):
            continue
        if (call.method, call.path, call.file) in allow:
            continue
        report.unserved.append(call)
    report.problems.extend(allowlist_problems(tree, allow, report.calls, report))
    report.problems.extend(v2_only_problems(tree, v2_only, report.calls, report))
    return report


# --------------------------------------------------------------------------------------
# the manifest: what readycheck asks the RUNNING :8000 for
# --------------------------------------------------------------------------------------
def route_manifest(report: Report, *, allow: Optional[dict] = None,
                   v2_only: Optional[dict] = None) -> dict:
    """Every route the enumerated calls need from ``:8000``, and on which flavour.

    One entry per (method, route): the shim's own template when the shim serves the call
    (so ``{dataset_name:path}`` and not the code's literal dataset), else the call's
    path. ``needed_on`` is where a 404 on it breaks something:

    * a call through ``network._wifi``/``audio._volume`` -- v1 only, and with that
      wrapper's ``fallback``: on v2 it never asks the shim, and on a v1 daemon that 404s
      it re-asks netman/mixer;
    * a :data:`V1_ONLY` call -- v1 only; a :data:`V2_ONLY` call -- v2 only;
    * anything else -- both.

    A route several calls share takes the union, and keeps a fallback only when every
    call has the same one.
    """

    allow = V1_ONLY if allow is None else allow
    v2_only = V2_ONLY if v2_only is None else v2_only
    merged: dict[tuple[str, str], dict[str, set]] = {}
    for call in report.calls:
        route = report.serving_route(call)
        bare = call.path.split("?", 1)[0]
        key = (call.method, call.path, call.file)
        if call.host:
            needed_on, fallback = {"v1"}, call.host
        elif key in allow:
            needed_on, fallback = {"v1"}, ""
        elif key in v2_only:
            needed_on, fallback = {"v2"}, ""
        else:
            needed_on, fallback = {"v1", "v2"}, ""
        entry = merged.setdefault((call.method, route.path if route else bare), {
            "needed_on": set(), "fallback": set(), "call": set(), "callers": set()})
        entry["needed_on"] |= needed_on
        entry["fallback"].add(fallback)
        entry["call"].add(bare)
        entry["callers"].add(call.file)
    routes = []
    for (method, path), entry in sorted(merged.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        fallbacks = entry["fallback"]
        routes.append({
            "method": method,
            "path": path,
            "call": min(entry["call"]),
            "needed_on": sorted(entry["needed_on"]),
            "fallback": next(iter(fallbacks)) if len(fallbacks) == 1 else "",
            "callers": sorted(entry["callers"]),
        })
    return {"about": _MANIFEST_ABOUT, "regenerate": MANIFEST_COMMAND, "routes": routes}


def manifest_text(manifest: dict) -> str:
    """JSON with one route per line, so a diff of the file reads as a diff of routes."""

    lines = ["{"]
    lines.extend("  %s: %s," % (json.dumps(key), json.dumps(value))
                 for key, value in manifest.items() if key != "routes")
    routes = [json.dumps(route) for route in manifest["routes"]]
    lines.append('  "routes": [')
    lines.extend("    %s%s" % (route, "," if index < len(routes) - 1 else "")
                 for index, route in enumerate(routes))
    lines.extend(["  ]", "}"])
    return "\n".join(lines) + "\n"


def write_manifest(root: Path, report: Report) -> Path:
    target = Path(root) / MANIFEST
    target.write_text(manifest_text(route_manifest(report)), encoding="utf-8", newline="\n")
    return target


def explain(call: Call) -> str:
    if call.host:
        return ("%s goes through a wrapper that answers from emori-admin's own %s on a v2 "
                "robot, and %s has no %s %s: on v2 it answers 404. "
                "Teach %s.request() the route (whether or not the shim serves "
                "it: this wrapper never asks the shim on v2), or make the call another way."
                % (call, call.host, HOST_BACKENDS[call.host], call.method, call.path,
                   call.host))
    return ("%s is not served by the v2 REST shim (%s), not answered by emori-admin's "
            "host backend (netman/mixer) and not in V1_ONLY. On a v2 robot it answers 404. "
            "Route it through network._wifi / audio._volume and "
            "teach netman/mixer the route, add the route to the shim, or (only if its "
            "absence is harmless) add a V1_ONLY entry naming the test that proves it."
            % (call, SHIM))


# --------------------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------------------
def test_every_daemon_call_this_code_makes_works_on_a_v2_robot():
    report = check(ROOT)

    assert not report.problems, "\n".join(report.problems)
    assert not report.unserved, "\n".join(explain(call) for call in report.unserved)


def test_the_enumeration_sees_every_way_the_code_reaches_the_daemon():
    """A contract that enumerates nothing passes everything: pin a call per access point."""

    calls = enumerate_calls(ROOT)[0]
    seen = {(c.method, c.path, c.file, c.host) for c in calls}
    admin = "src/emori_adapter/admin/"

    # emori-admin, through the two backend-selecting wrappers, and around them.
    assert ("GET", "/wifi/status", admin + "network.py", "netman") in seen
    assert ("POST", "/wifi/scan_and_list", admin + "network.py", "netman") in seen
    assert ("GET", "/api/volume/current", admin + "audio.py", "mixer") in seen
    assert ("POST", "/api/volume/test-sound", admin + "audio.py", "mixer") in seen
    assert ("GET", "/api/daemon/status", admin + "host_backend.py", "") in seen
    assert ("GET", "/api/daemon/status", admin + "probes.py", "") in seen
    # The hostapp's robot client, direct, through _post/_emori_flag, an f-string with a
    # class constant, a path parameter and a conditional path.
    client = "src/emori_adapter/robot/client.py"
    assert ("GET", "/api/state/full", client, "") in seen
    assert ("POST", "/api/move/goto", client, "") in seen
    assert ("POST", "/api/emori/gaze", client, "") in seen
    assert ("POST", "/api/motors/set_mode/{}", client, "") in seen
    assert ("POST", "/api/move/play/recorded-move-dataset/"
            "pollen-robotics/reachy-mini-emotions-library/{}", client, "") in seen
    assert ("POST", "/api/media/wobbling/enable", client, "") in seen
    assert ("POST", "/api/media/wobbling/disable", client, "") in seen
    # readycheck's URLs on REACHY_BASE_URL.
    readycheck = "src/emori_adapter/hostapp/readycheck.py"
    assert ("GET", "/api/daemon/status", readycheck, "") in seen
    assert ("GET", "/api/motors/status", readycheck, "") in seen
    # Hint text that merely MENTIONS a daemon route is not a call.
    assert not any(c.path.startswith("/api/daemon/start") for c in calls)


def test_the_shim_routes_are_read_with_their_path_parameters():
    routes, problems = shim_routes(ROOT)

    assert not problems
    by_path = {(r.method, r.path): r for r in routes}
    recorded = by_path[("POST",
                        "/api/move/play/recorded-move-dataset/{dataset_name:path}/{move_name}")]
    assert recorded.serves("POST", "/api/move/play/recorded-move-dataset/a/b/{}")
    assert not recorded.serves("GET", "/api/move/play/recorded-move-dataset/a/b/{}")
    assert by_path[("POST", "/api/motors/set_mode/{mode}")].serves(
        "POST", "/api/motors/set_mode/{}")
    assert not any(r.path.startswith(("/wifi", "/api/volume")) for r in routes)


def test_the_host_backend_tables_are_the_routes_netman_and_mixer_answer():
    assert host_table(ROOT, HOST_BACKENDS["netman"]) == {
        ("GET", "/wifi/status"), ("GET", "/wifi/error"), ("POST", "/wifi/reset_error"),
        ("POST", "/wifi/scan_and_list"), ("POST", "/wifi/connect"),
        ("POST", "/wifi/forget"), ("POST", "/wifi/setup_hotspot")}
    assert host_table(ROOT, HOST_BACKENDS["mixer"]) == {
        ("GET", "/api/volume/current"), ("GET", "/api/volume/microphone/current"),
        ("POST", "/api/volume/set"), ("POST", "/api/volume/microphone/set"),
        ("POST", "/api/volume/test-sound")}


def test_the_scripts_that_talk_to_the_daemon_are_the_ones_listed():
    found = script_refs(Tree(ROOT))

    assert set(found) == set(SCRIPT_DOORS)
    install = dict(found["deploy/robot/install.sh"])
    assert any("/api/daemon/status" in code for code in install.values())


# --- the test bites ------------------------------------------------------------------
# Synthetic trees: this checkout with a few files replaced (Tree overlay), parsed once.
def _plus(rel: str, text: str, overlay: Optional[dict[str, str]] = None) -> dict[str, str]:
    """``overlay`` + ``text`` appended to ``rel`` (a new file when there is none)."""

    overlay = dict(overlay or {})
    if rel in overlay:
        base = overlay[rel]
    else:
        path = ROOT / rel
        base = path.read_text(encoding="utf-8") if path.is_file() else ""
    body = textwrap.dedent(text).strip() + "\n"
    overlay[rel] = base + "\n\n" + body if base else body
    return overlay


def _replace(rel: str, old: str, new: str,
             overlay: Optional[dict[str, str]] = None) -> dict[str, str]:
    overlay = dict(overlay or {})
    source = overlay.get(rel) or (ROOT / rel).read_text(encoding="utf-8")
    assert old in source, "anchor moved in %s" % rel
    overlay[rel] = source.replace(old, new, 1)
    return overlay


def _unserved(report: Report) -> set[tuple[str, str]]:
    return {(c.method, c.path) for c in report.unserved}


def _problems(report: Report, *words: str) -> list[str]:
    return [p for p in report.problems if all(w in p for w in words)]


def test_a_new_admin_feature_on_a_daemon_only_route_fails():
    report = check(overlay=_plus("src/emori_adapter/admin/system.py", '''
        def installed_apps():
            return probes.daemon("GET", "/api/apps/list-available/installed")
    '''))

    assert ("GET", "/api/apps/list-available/installed") in _unserved(report)


def test_calling_the_wifi_routes_directly_instead_of_through_the_selector_fails():
    """The pre-fix shape: netman answers /wifi/status, but this call never asks it."""

    report = check(overlay=_plus("src/emori_adapter/admin/system.py", '''
        def wifi_mode():
            return probes.daemon("GET", "/wifi/status")
    '''))

    assert [(c.method, c.path, c.file) for c in report.unserved] == [
        ("GET", "/wifi/status", "src/emori_adapter/admin/system.py")]


def test_a_wifi_route_the_host_backend_does_not_answer_fails():
    report = check(overlay=_plus("src/emori_adapter/admin/network.py", '''
        def restart_radio() -> dict:
            return probes.fail(_wifi("POST", "/wifi/restart_radio"), when_unreachable="x")
    '''))

    assert ("POST", "/wifi/restart_radio") in _unserved(report)


def test_a_host_wrapper_call_the_shim_serves_but_the_host_does_not_fails():
    """On v2 `_volume` never asks the shim, so the shim serving
    /api/media/play_sound does not make this call work -- mixer has no such route."""

    report = check(overlay=_plus("src/emori_adapter/admin/audio.py", '''
        def play_chime():
            return _volume("POST", "/api/media/play_sound", json_body={"file": "x.wav"})
    '''))

    unserved = [c for c in report.unserved if c.path == "/api/media/play_sound"]
    assert [(c.method, c.host, c.file) for c in unserved] == [
        ("POST", "mixer", "src/emori_adapter/admin/audio.py")]
    assert "mixer" in explain(unserved[0]) and "never asks the shim" in explain(unserved[0])
    # ... while the same route through probes.daemon() is the shim's, and fine.
    direct = check(overlay=_plus("src/emori_adapter/admin/system.py", '''
        def chime():
            return probes.daemon("POST", "/api/media/play_sound")
    '''))
    assert ("POST", "/api/media/play_sound") not in _unserved(direct)


def test_a_wrapper_without_the_route_missing_fallback_is_not_a_host_dispatch():
    report = check(overlay=_plus("src/emori_adapter/admin/network.py", '''
        def _wifi_no_fallback(method, path):
            if host_backend.current() == host_backend.HOST:
                return netman.request(method, path)
            return probes.daemon(method, path)


        def error_again():
            return _wifi_no_fallback("GET", "/wifi/error")
    '''))

    assert ("GET", "/wifi/error") in _unserved(report)


def test_a_robot_client_route_the_shim_does_not_serve_fails():
    anchor = "    def close(self) -> None:\n        self._http.close()"
    report = check(overlay=_replace(
        "src/emori_adapter/robot/client.py", anchor,
        "    def kinematics(self):\n"
        "        return self._http.get(\"/api/kinematics/info\")\n\n" + anchor))

    assert ("GET", "/api/kinematics/info") in _unserved(report)


def test_a_new_door_to_the_daemon_fails_twice():
    report = check(overlay=_plus("src/emori_adapter/hostapp/volume_peek.py", '''
        import os

        import httpx

        BASE = os.environ.get("REACHY_BASE_URL", "http://127.0.0.1:8000")


        def peek():
            return httpx.get(BASE + "/api/volume/current")
    '''))

    assert _problems(report, "volume_peek.py", "DOORS")
    assert ("GET", "/api/volume/current") in _unserved(report)


def test_a_daemon_call_the_test_cannot_follow_fails():
    overlay = _plus("src/emori_adapter/admin/system.py", '''
        def whatever(table):
            return probes.daemon("GET", table.lookup())
    ''')
    overlay = _plus("src/emori_adapter/hostapp/watchdog.py", '''
        def peek(robot):
            return robot._http.get("/wifi/status")
    ''', overlay)

    report = check(overlay=overlay)

    assert _problems(report, "system.py", "cannot tell which daemon route")
    assert _problems(report, "watchdog.py", "reaches into the robot client")


def test_a_route_the_shim_registers_conditionally_is_not_counted():
    anchor = "    @app.get(\"/api/state/full\")\n"
    overlay = _replace(SHIM, anchor,
                       "    if robot_name:\n        @app.get(\"/api/state/full\")\n"
                       "        def _gated() -> dict:\n            return {}\n" + anchor)

    _, problems = shim_routes(Tree(ROOT, overlay))

    assert any("conditionally" in p for p in problems)


def test_the_v1_only_allowlist_needs_a_real_test_and_a_real_call():
    overlay = _plus("src/emori_adapter/admin/system.py", '''
        def apps():
            return probes.daemon("GET", "/api/apps/list-available/installed")
    ''')
    call = ("GET", "/api/apps/list-available/installed", "src/emori_adapter/admin/system.py")
    proven = {call: ("the Apps card stays empty on v2",
                     "tests/test_daemon_contract.py::test_the_v1_only_allowlist_needs_a_real"
                     "_test_and_a_real_call")}
    unproven = {call: ("the Apps card stays empty on v2", "tests/test_nope.py::test_nope")}
    stale = {("GET", "/api/apps/gone", "src/emori_adapter/admin/system.py"): (
        "gone", "tests/test_daemon_contract.py::test_the_v1_only_allowlist_needs_a_real"
                "_test_and_a_real_call")}

    allowed = check(allow=proven, overlay=overlay)
    assert allowed.unserved == [] and not allowed.problems
    assert _problems(check(allow=unproven, overlay=overlay), "not a test in this tree")
    assert _problems(check(allow=stale, overlay=overlay), "matches no call any more")


# --- the blind spots, each of which would otherwise pass silently --------------------
_RC = "src/emori_adapter/hostapp/readycheck.py"


def test_a_url_wrapper_whose_path_is_a_parameter_is_followed_to_its_callers():
    """(a) `f"{base}{path}"` is followed, though its tail is not a literal '/...'."""

    report = check(overlay=_plus(_RC, '''
        def _daemon_get(probe, path):
            return probe.http(f"{probe.env('REACHY_BASE_URL', '')}{path}")


        def installed_apps(probe):
            return _daemon_get(probe, "/api/apps/list-available/installed")
    '''))

    assert ("GET", "/api/apps/list-available/installed") in _unserved(report)
    assert not report.problems, report.problems


def test_a_daemon_url_whose_path_cannot_be_read_is_a_problem():
    report = check(overlay=_plus(_RC, '''
        def apps(probe, table):
            reachy = probe.env("REACHY_BASE_URL", "http://127.0.0.1:8000")
            return probe.http(f"{reachy}{table.lookup()}")
    '''))

    assert _problems(report, "readycheck.py", "cannot tell which daemon route")


def test_a_daemon_url_that_no_recognised_call_sends_is_a_problem():
    """(a) a URL on the base parked in a dict, or handed to curl, is still seen."""

    report = check(overlay=_plus(_RC, '''
        def apps_request(probe):
            reachy = probe.env("REACHY_BASE_URL", "http://127.0.0.1:8000")
            return {"url": f"{reachy}/api/apps/list-available/installed"}


        def apps_curl(probe):
            url = f"{probe.env('REACHY_BASE_URL', '')}/api/apps/list"
            return subprocess.run(["curl", "-s", url])
    '''))

    assert _problems(report, "readycheck.py", "no recognised HTTP call sends this URL",
                     "dict")
    assert _problems(report, "readycheck.py", "no recognised HTTP call sends this URL",
                     "list")


def test_a_use_of_the_address_that_is_not_a_call_must_be_accounted_for():
    """(b) a door is not listed once: each use of the address is checked."""

    overlay = _plus("src/emori_adapter/hostapp/server.py", '''
        def _poll_in_the_background():
            threading.Thread(target=_poll, args=(REACHY_BASE_URL,)).start()
    ''')
    report = check(overlay=overlay)

    found = _problems(report, "hostapp/server.py", "REACHY_BASE_URL", "DOOR_USES")
    assert found and "'_poll_in_the_background', 'tuple')" in found[0]
    # A DOOR_USES entry for it (with a reason) is how a non-call use is accepted...
    key = ("src/emori_adapter/hostapp/server.py", "_poll_in_the_background", "tuple")
    accepted = check(overlay=overlay, door_uses={**DOOR_USES, key: "test"})
    assert not _problems(accepted, "hostapp/server.py", "REACHY_BASE_URL")
    # ... and one that matches nothing is stale.
    assert _problems(check(door_uses={**DOOR_USES, key: "test"}), "matches no use any more")


@pytest.mark.parametrize("name, source, route", [
    ("an imported REACHY_BASE_URL alias", '''
        import httpx

        from emori_adapter.hostapp.server import REACHY_BASE_URL as R


        def apps():
            return httpx.get(R + "/api/apps/one")
    ''', ("GET", "/api/apps/one")),
    ("server.REACHY_BASE_URL", '''
        import httpx

        from emori_adapter.hostapp import server


        def apps():
            return httpx.get(server.REACHY_BASE_URL + "/api/apps/two")
    ''', ("GET", "/api/apps/two")),
    ("probes.daemon imported under another name", '''
        from emori_adapter.admin.probes import daemon as fetch


        def apps():
            return fetch("GET", "/api/apps/three")
    ''', ("GET", "/api/apps/three")),
    ("a fully qualified probes.daemon", '''
        import emori_adapter.admin.probes


        def apps():
            return emori_adapter.admin.probes.daemon("GET", "/api/apps/four")
    ''', ("GET", "/api/apps/four")),
    ("a literal daemon URL in an f-string", '''
        import httpx


        def apps(host):
            return httpx.post(f"http://{host}:8000/api/apps/five")
    ''', ("POST", "/api/apps/five")),
    ("robot.base_url", '''
        import httpx


        def apps(robot):
            return httpx.get(robot.base_url + "/api/apps/six")
    ''', None),
    ("a %-formatted :8000", '''
        def url(host):
            return "http://%s:%d/api" % (host, 8000)
    ''', None),
    ("a port constant", '''
        DAEMON_PORT = 8000
    ''', None),
    ("curl to localhost:8000", '''
        import subprocess


        def apps():
            return subprocess.run(["curl", "-s", "localhost:8000/api/apps/seven"])
    ''', None),
])
def test_every_way_to_reach_the_daemon_is_a_door(name, source, route):
    """(c)/(d): door detection sees each of these."""

    report = check(overlay=_plus("src/emori_adapter/hostapp/sneaky.py", source))

    assert _problems(report, "hostapp/sneaky.py", "not in DOORS"), name
    if route:
        assert route in _unserved(report), name


def test_browser_js_that_builds_a_daemon_url_is_a_door():
    report = check(overlay=_replace(
        "src/emori_adapter/admin/page.py", "async function api(path,opts){",
        "function daemonUrl(p){return location.protocol+'//'+location.hostname+':'+8000+p;}\n"
        "async function api(path,opts){"))

    assert _problems(report, "admin/page.py", "not in DOORS", ":8000")


def test_probes_daemon_handed_around_as_a_value_is_a_problem():
    report = check(overlay=_plus("src/emori_adapter/admin/system.py", '''
        import functools

        _get = functools.partial(probes.daemon, "GET")


        def apps():
            return _get("/api/apps/eight")
    '''))

    assert _problems(report, "system.py", "passes probes.daemon around as a value")


def test_a_local_function_called_daemon_is_not_probes_daemon():
    """(d) matched by import origin: a name is not enough, either way."""

    report = check(overlay=_plus("src/emori_adapter/admin/system.py", '''
        def daemon(method, path):
            return {"method": method, "path": path}


        def not_a_daemon_call():
            return daemon("GET", "/api/apps/nine")
    '''))

    assert not any(c.path == "/api/apps/nine" for c in report.calls)


def test_a_new_script_on_the_daemon_port_fails():
    overlay = {"scripts/peek_daemon.py": 'DAEMON = "http://127.0.0.1:8000"\n',
               "deploy/robot/peek.sh": "#!/usr/bin/env bash\n"
                                       "# a comment about :8000 is not a call\n"
                                       "curl -s http://127.0.0.1:8000/api/apps/ten\n"}

    report = check(overlay=overlay)

    assert _problems(report, "scripts/peek_daemon.py", "SCRIPT_DOORS")
    assert _problems(report, "deploy/robot/peek.sh", "SCRIPT_DOORS", "line 3")
    assert _problems(report, "deploy/robot/peek.sh:3", "GET /api/apps/ten",
                     "does not serve")
    # A comment that mentions the port is not a reference.
    assert not _problems(report, "deploy/robot/peek.sh", "line 2")


def test_a_robot_script_route_the_shim_serves_passes():
    report = check(overlay={"deploy/robot/peek.sh": "curl -s localhost:8000/api/daemon/status\n"},
                   script_doors={**SCRIPT_DOORS, "deploy/robot/peek.sh": "test"})

    assert not _problems(report, "peek.sh"), report.problems


# --- the manifest readycheck asks the running :8000 about -----------------------------
def test_the_committed_route_manifest_is_what_the_code_needs_today():
    """readycheck's daemon_routes gate checks a robot against this file: a stale one
    checks yesterday's code and passes a robot today's code 404s on."""

    report = check(ROOT)
    committed = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))

    assert committed == route_manifest(report), (
        "%s is stale: regenerate it with `%s` and commit it" % (MANIFEST, MANIFEST_COMMAND))


def test_the_manifest_says_which_flavour_needs_each_route():
    routes = {(r["method"], r["path"]): r for r in route_manifest(check(ROOT))["routes"]}

    # Both flavours: the robot client, readycheck, emori-admin's status probe.
    status = routes[("GET", "/api/daemon/status")]
    assert (status["needed_on"], status["fallback"]) == (["v1", "v2"], "")
    assert "src/emori_adapter/robot/client.py" in status["callers"]
    # The shim's template, with its converter, and a call the code really makes on it.
    played = routes[("POST", "/api/move/play/recorded-move-dataset/"
                             "{dataset_name:path}/{move_name}")]
    assert played["call"] == ("/api/move/play/recorded-move-dataset/pollen-robotics/"
                              "reachy-mini-emotions-library/{}")
    assert played["needed_on"] == ["v1", "v2"]
    assert routes[("POST", "/api/motors/set_mode/{mode}")]["call"] == \
        "/api/motors/set_mode/{}"
    # emori-admin's Wi-Fi and volume: Pollen's daemon only, re-asked of netman/mixer.
    assert (routes[("GET", "/wifi/status")]["needed_on"],
            routes[("GET", "/wifi/status")]["fallback"]) == (["v1"], "netman")
    assert (routes[("POST", "/api/volume/set")]["needed_on"],
            routes[("POST", "/api/volume/set")]["fallback"]) == (["v1"], "mixer")
    # EMORI's own extensions: the v2 node only.
    assert routes[("POST", "/api/emori/gaze")]["needed_on"] == ["v2"]
    assert routes[("POST", "/api/emori/head_reserved")]["needed_on"] == ["v2"]


def test_a_route_with_a_direct_call_has_no_fallback_and_a_v1_only_call_is_v1_only():
    overlay = _plus("src/emori_adapter/admin/system.py", '''
        def wifi_mode():
            return probes.daemon("GET", "/wifi/status")


        def apps():
            return probes.daemon("GET", "/api/apps/list-available/installed")
    ''')
    call = ("GET", "/api/apps/list-available/installed", "src/emori_adapter/admin/system.py")
    allow = {call: ("the Apps card stays empty on v2",
                    "tests/test_daemon_contract.py::test_the_v1_only_allowlist_needs_a_real"
                    "_test_and_a_real_call")}

    routes = {(r["method"], r["path"]): r
              for r in route_manifest(check(allow=allow, overlay=overlay),
                                      allow=allow)["routes"]}

    # system.py's direct call 404s on v1 with nothing to catch it: no fallback any more.
    wifi = routes[("GET", "/wifi/status")]
    assert (wifi["needed_on"], wifi["fallback"]) == (["v1", "v2"], "")
    assert routes[("GET", "/api/apps/list-available/installed")]["needed_on"] == ["v1"]


def test_the_v2_only_list_needs_a_real_test_a_real_call_and_a_shim_route():
    gaze = ("POST", "/api/emori/gaze", "src/emori_adapter/robot/client.py")
    proven = V2_ONLY[gaze]

    assert not _problems(check(v2_only={gaze: proven}), "V2_ONLY")
    assert _problems(check(v2_only={gaze: (proven[0], "tests/test_nope.py::test_nope")}),
                     "V2_ONLY", "not a test in this tree")
    stale = ("POST", "/api/emori/gone", "src/emori_adapter/robot/client.py")
    assert _problems(check(v2_only={stale: proven}), "V2_ONLY", "matches no call any more")
    # A route the shim does not serve is not "v2-only": it is unserved.
    overlay = _plus("src/emori_adapter/admin/system.py", '''
        def apps():
            return probes.daemon("GET", "/api/apps/list-available/installed")
    ''')
    unserved = ("GET", "/api/apps/list-available/installed",
                "src/emori_adapter/admin/system.py")
    assert _problems(check(v2_only={unserved: proven}, overlay=overlay),
                     "V2_ONLY", "not served by the v2 shim either")


def test_readycheck_s_route_probe_is_listed_and_the_list_stays_true():
    """The daemon_routes gate's GET /openapi.json + OPTIONS are accounted for -- and
    only through ROUTE_PROBES: drop the entry and the use of the address fails."""

    assert not _problems(check(), "ROUTE_PROBES")
    assert _problems(check(route_probes={}), "readycheck.py", "_probe_daemon_routes()",
                     "DOOR_USES")
    stale = ("src/emori_adapter/hostapp/readycheck.py", "collect", "nothing()")
    assert _problems(check(route_probes={**ROUTE_PROBES, stale: "test"}),
                     "ROUTE_PROBES", "matches no use any more")


def test_writing_the_manifest_writes_what_the_test_compares(tmp_path):
    report = check(ROOT)
    (tmp_path / MANIFEST).parent.mkdir(parents=True)

    target = write_manifest(tmp_path, report)

    assert json.loads(target.read_text(encoding="utf-8")) == route_manifest(report)
    assert b"\r\n" not in target.read_bytes()


# --- the proof: this contract fails on code that calls daemon-only routes ------------
# A historical commit in which emori-admin called /wifi/* and /api/volume/* straight on
# :8000. Skipped when that commit is not in the clone.
PRE_FIX = "7681145"
DAEMON_ONLY_CALLS = {
    ("GET", "/wifi/status"), ("GET", "/wifi/error"), ("POST", "/wifi/scan_and_list"),
    ("POST", "/wifi/connect"), ("POST", "/wifi/forget"), ("POST", "/wifi/setup_hotspot"),
    ("GET", "/api/volume/current"), ("GET", "/api/volume/microphone/current"),
    ("POST", "/api/volume/set"), ("POST", "/api/volume/microphone/set"),
    ("POST", "/api/volume/test-sound"),
}


def _archive(commit: str, destination: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    known = subprocess.run([git, "-C", str(ROOT), "cat-file", "-e", commit + "^{commit}"],
                           capture_output=True)
    if known.returncode != 0:
        return False
    tar = subprocess.run([git, "-C", str(ROOT), "archive", "--format=tar", commit,
                          SRC, str(Path(SHIM).parent.as_posix())], capture_output=True)
    if tar.returncode != 0:
        return False
    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as archive:
        if sys.version_info >= (3, 12):
            archive.extractall(destination, filter="data")
        else:  # pragma: no cover - the venv is 3.12
            archive.extractall(destination)
    return True


def test_the_contract_fails_on_code_that_calls_daemon_only_routes(tmp_path):
    if not _archive(PRE_FIX, tmp_path):
        pytest.skip("git or commit %s is not available (shallow clone?)" % PRE_FIX)

    report = check(tmp_path, allow={}, doors=DOORS)

    assert not [p for p in report.problems if "cannot tell" in p]
    assert DAEMON_ONLY_CALLS <= _unserved(report)
    assert {c.file for c in report.unserved} == {"src/emori_adapter/admin/network.py",
                                                 "src/emori_adapter/admin/audio.py"}


def main(argv: list[str]) -> int:
    """``python tests/test_daemon_contract.py [--write-manifest] [<root>]``"""

    write = "--write-manifest" in argv[1:]
    rest = [arg for arg in argv[1:] if arg != "--write-manifest"]
    root = Path(rest[0]).resolve() if rest else ROOT
    report = check(root)
    for call in report.calls:
        print("%-6s %-26s %s" % (report.verdict(call) or "-", call.method + " " + call.path,
                                 call.where + (" via " + call.via if call.via else "")))
    print()
    for problem in report.problems:
        print("PROBLEM", problem)
    for call in report.unserved:
        print("UNSERVED", call)
    print("\n%d calls, %d unserved, %d problems" % (len(report.calls), len(report.unserved),
                                                    len(report.problems)))
    if report.unserved or report.problems:
        if write:
            print("not writing %s: fix the contract first" % MANIFEST)
        return 1
    if write:
        target = write_manifest(root, report)
        print("wrote %d routes to %s" % (len(route_manifest(report)["routes"]), target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
