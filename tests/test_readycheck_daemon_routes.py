"""readycheck's ``daemon_routes`` gate: the RUNNING :8000 has every route this code calls.

``tests/test_daemon_contract.py`` proves each :8000 call against the v2 shim's SOURCE. A
robot runs the shim it was last shipped (or the Pollen daemon its OS came with), which
can be older than the code beside it; this gate is how the robot notices. Every fake
:8000 here is a real FastAPI app behind Starlette's TestClient, so openapi, 405 and 404
are Starlette's own answers -- and one of them is the real v2 shim, loaded by path.
"""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from emori_adapter.hostapp import readycheck  # noqa: E402
from emori_adapter.hostapp.readycheck import (  # noqa: E402
    FAIL,
    PASS,
    SKIP,
    WARN,
    HttpResult,
    RouteNeed,
    RouteProbe,
    SystemProbe,
    collect,
    gate_daemon_routes,
    load_route_manifest,
    needed_routes,
    probe_routes,
)
from test_readycheck import _FakeProbe, _healthy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "emori_adapter" / "hostapp" / "daemon_routes.json"
BASE = "http://127.0.0.1:8000"
NEEDS, _ = load_route_manifest()

# What GET /api/daemon/status says, by who answers :8000.
V2_STATUS = HttpResult(True, 200, {"state": "running", "no_media": True,
                                   "backend": "reachy_mini_ros2"})
V1_STATUS = HttpResult(True, 200, {"state": "running", "no_media": True})


def _app(routes, *, openapi: bool = True, ran: list | None = None,
         hidden: frozenset = frozenset(), pollen: bool = False) -> FastAPI:
    """A :8000 serving exactly ``routes`` ((METHOD, template) pairs).

    Every handler records itself in ``ran``: the probe must never run one. ``pollen``
    adds the CORS middleware Pollen's daemon runs (``allow_methods=["*"]``).
    """

    app = FastAPI(openapi_url="/openapi.json" if openapi else None)
    ran = ran if ran is not None else []
    for method, path in routes:
        app.add_api_route(path, _recorder(ran, (method, path)), methods=[method],
                          include_in_schema=(method, path) not in hidden)
    if pollen:
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                           allow_headers=["*"])
    return app


def _recorder(ran: list, route: tuple[str, str]):
    def endpoint(request: Request) -> dict:
        ran.append(route)
        return {}
    return endpoint


def _routes(needs=NEEDS, *, without=(), swap=None) -> list[tuple[str, str]]:
    """The manifest's routes as a server would register them, minus ``without``."""

    out = [(need.method, need.path) for need in needs if need.path not in without]
    return [swap.get(route, route) for route in out] if swap else out


def _probe(app: FastAPI, needs=NEEDS) -> RouteProbe:
    return probe_routes(TestClient(app, base_url=BASE), BASE, needs)


def _gate(routes: RouteProbe, daemon: HttpResult = V2_STATUS, **overrides):
    return gate_daemon_routes(_healthy(daemon=daemon, daemon_routes=routes, **overrides))


# --- the real v2 shim -----------------------------------------------------------------
_SHIM = ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2" / \
    "rest_shim.py"


def _real_shim() -> FastAPI:
    name = "reachy_mini_rest_shim_for_daemon_routes"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, _SHIM)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    # Building the app touches neither; the probe runs no handler.
    return module.build_app(object(), SimpleNamespace(kind="mockup"))


def test_the_v2_shim_in_this_tree_publishes_openapi_with_every_v2_route():
    """A v2 robot: /openapi.json 200 with 17 paths, the recorded-move route rendered
    as {dataset_name}/{move_name} (converter dropped)."""

    routes = _probe(_real_shim())

    assert routes.openapi is not None
    assert ("POST", "/api/move/play/recorded-move-dataset/{}/{}") in routes.openapi
    # Asked only about what the table lacks: the Wi-Fi/volume routes and nothing else.
    assert {sample for sample, _, _ in routes.options} == {
        need.sample for need in NEEDS if not need.v2}
    assert all(status == 404 for _, status, _ in routes.options)
    assert _gate(routes) == readycheck.GateResult("daemon_routes", PASS, "routes_served_v2")


# --- openapi present ------------------------------------------------------------------
def test_a_daemon_that_lists_every_route_passes_without_one_options():
    routes = _probe(_app(_routes(), pollen=True))

    assert routes.options == ()
    assert (_gate(routes, V1_STATUS).status, _gate(routes, V1_STATUS).code) == (
        PASS, "routes_served")


def test_a_v2_route_the_running_node_lacks_fails_naming_it_and_how_to_ship_the_node():
    routes = _probe(_app(_routes(without=("/api/emori/gaze", "/api/move/set_target"))))

    result = _gate(routes)

    assert (result.status, result.code) == (FAIL, "daemon_route_missing")
    assert "POST /api/emori/gaze" in result.hint and "POST /api/move/set_target" in result.hint
    assert "deploy/robot/ros2_native/README.md" in result.hint
    assert "install-native.sh" in result.hint and "emori/ros2-jazzy-v2:robot" in result.hint
    # The Docker route is running: name only that one.
    docker = _gate(routes, ros2_flavor="v2")
    assert "Route A" in docker.hint and "install-native.sh" not in docker.hint


def test_a_route_pollen_s_daemon_lacks_fails_with_the_v1_fix():
    result = _gate(_probe(_app(_routes(without=("/api/state/full",)), pollen=True)),
                   V1_STATUS)

    assert (result.status, result.code) == (FAIL, "daemon_route_missing")
    assert "GET /api/state/full" in result.hint
    assert "reachy-mini" in result.hint and "restart reachy-mini-daemon" in result.hint
    assert "ros2_native" not in result.hint


def test_each_flavour_is_asked_only_for_what_it_must_serve():
    # v1: Pollen's daemon has no /api/emori/* -- and the client latches those off.
    no_emori = _probe(_app(_routes(without=("/api/emori/gaze", "/api/emori/head_reserved"))))
    assert _gate(no_emori, V1_STATUS).status == PASS
    assert _gate(no_emori, V2_STATUS).code == "daemon_route_missing"
    # v2: the node has no Wi-Fi/volume -- emori-admin answers those itself there.
    host = tuple(need.path for need in NEEDS if need.fallback)
    no_host = _probe(_app(_routes(without=host)))
    assert _gate(no_host, V2_STATUS).status == PASS


def test_a_v1_daemon_without_its_wifi_routes_warns_because_emori_admin_takes_over():
    result = _gate(_probe(_app(_routes(without=("/wifi/status", "/wifi/connect")))),
                   V1_STATUS)

    assert (result.status, result.code) == (WARN, "daemon_route_fallback")
    assert "GET /wifi/status" in result.hint and "netman" in result.hint


def test_an_unknown_answerer_is_held_only_to_what_both_flavours_need():
    routes = _probe(_app(_routes(without=("/api/emori/gaze", "/wifi/status"))))
    silent = HttpResult(True, 503, None)

    assert _gate(routes, silent).status == PASS
    assert _gate(_probe(_app(_routes(without=("/api/move/goto",)))), silent).code == \
        "daemon_route_missing"


def test_path_parameter_names_and_converters_do_not_matter():
    """openapi drops converters and the names are the server's own business."""

    renamed = {("POST", "/api/motors/set_mode/{mode}"): ("POST", "/api/motors/set_mode/{m}"),
               ("POST", "/api/move/play/recorded-move-dataset/{dataset_name:path}/"
                        "{move_name}"):
               ("POST", "/api/move/play/recorded-move-dataset/{ds:path}/{name}")}

    routes = _probe(_app(_routes(swap=renamed)))

    assert routes.options == ()
    assert _gate(routes).status == PASS


def test_a_route_hidden_from_the_schema_is_found_by_options_not_failed():
    hidden = frozenset({("POST", "/api/move/stop")})

    routes = _probe(_app(_routes(), hidden=hidden))

    assert [(sample, status) for sample, status, _ in routes.options] == [
        ("/api/move/stop", 405)]
    assert _gate(routes).status == PASS


def test_the_right_path_under_the_wrong_method_is_missing():
    routes = _probe(_app(_routes(swap={("POST", "/api/move/goto"): ("GET", "/api/move/goto")})))

    [(sample, status, allow)] = routes.options
    assert (sample, status) == ("/api/move/goto", 405)
    assert "GET" in allow and "POST" not in allow
    result = _gate(routes)
    assert (result.status, result.code) == (FAIL, "daemon_route_missing")
    assert "POST /api/move/goto" in result.hint


# --- openapi absent: one OPTIONS per route --------------------------------------------
def test_without_openapi_every_route_is_asked_with_options_and_none_of_them_runs():
    ran: list = []

    routes = _probe(_app(_routes(), openapi=False, ran=ran, pollen=True))

    assert routes.openapi is None
    assert len(routes.options) == len({need.sample for need in NEEDS})
    assert all(status == 405 for _, status, _ in routes.options)
    assert ran == []   # Starlette answered every OPTIONS before any handler
    assert (_gate(routes, V1_STATUS).status, _gate(routes, V2_STATUS).status) == (PASS, PASS)


def test_without_openapi_a_404_on_a_literal_route_is_missing():
    routes = _probe(_app(_routes(without=("/api/move/stop",)), openapi=False))

    result = _gate(routes)

    assert (result.status, result.code) == (FAIL, "daemon_route_missing")
    assert "POST /api/move/stop" in result.hint


def test_without_openapi_the_sample_is_the_code_s_own_call():
    """A path converter swallows the code's literal dataset; the sample fills only the
    run-time value, so a route shaped differently from the shim's still answers 405."""

    swap = {("POST", "/api/move/play/recorded-move-dataset/{dataset_name:path}/{move_name}"):
            ("POST", "/api/move/play/recorded-move-dataset/{org}/{library}/{move}")}

    routes = _probe(_app(_routes(swap=swap), openapi=False))

    assert ("/api/move/play/recorded-move-dataset/pollen-robotics/"
            "reachy-mini-emotions-library/0", 405, "POST") in routes.options
    assert _gate(routes).status == PASS


def test_without_openapi_a_404_on_a_parameter_route_is_inconclusive_not_a_failure():
    """A uuid converter refuses the sample `0`: the route may well exist."""

    swap = {("POST", "/api/motors/set_mode/{mode}"):
            ("POST", "/api/motors/set_mode/{mode:uuid}")}

    unsure = _gate(_probe(_app(_routes(swap=swap), openapi=False)))
    assert (unsure.status, unsure.code) == (WARN, "daemon_routes_unverified")
    assert "POST /api/motors/set_mode/{mode}" in unsure.hint
    # ... while the same server WITH its table settles it.
    assert _gate(_probe(_app(_routes(swap=swap)))).status == PASS
    # And a table that lacks it, plus the 404, is a real miss.
    gone = _probe(_app(_routes(without=("/api/motors/set_mode/{mode}",))))
    assert _gate(gone).code == "daemon_route_missing"


# --- what cannot be asked -------------------------------------------------------------
class _Wedged:
    """No openapi, then no answer at all."""

    def __init__(self) -> None:
        self.options = 0

    def get(self, url):
        return SimpleNamespace(status_code=404, json=lambda: {"detail": "Not Found"})

    def request(self, method, url):
        self.options += 1
        raise TimeoutError("read timeout")


def test_a_server_that_stops_answering_costs_one_timeout_not_one_per_route():
    client = _Wedged()

    routes = probe_routes(client, BASE, NEEDS)

    assert client.options == 1
    assert all(status == 0 for _, status, _ in routes.options)
    assert _gate(routes).code == "daemon_routes_unverified"


def test_a_daemon_that_is_down_skips_and_one_that_vanished_midway_skips_too():
    assert _gate(RouteProbe(needs=NEEDS), HttpResult(False)).code == "daemon_unreachable"

    class _Gone:
        def get(self, url):
            raise ConnectionError("refused")

    routes = probe_routes(_Gone(), BASE, NEEDS)
    assert routes.reached is False
    assert (_gate(routes).status, _gate(routes).code) == (SKIP, "routes_not_probed")
    # An Evidence built without the check (nothing asked) never passes either.
    assert _gate(RouteProbe()).status == SKIP


def test_system_probe_never_raises(monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise RuntimeError("no network stack")

    monkeypatch.setattr(httpx, "Client", boom)

    assert SystemProbe(timeout_s=0.1).daemon_routes(BASE, NEEDS) == RouteProbe(needs=NEEDS)


# --- the manifest ---------------------------------------------------------------------
def test_the_gate_asks_for_exactly_the_shipped_manifest():
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["routes"]
    loaded = {(n.method, n.path, n.call, n.v1, n.v2, n.fallback) for n in NEEDS}

    assert len(NEEDS) == len(entries) == len(loaded)
    assert loaded == {(e["method"], e["path"], e["call"], "v1" in e["needed_on"],
                       "v2" in e["needed_on"], e["fallback"]) for e in entries}
    by_flavour = {flavour: {(e["method"], e["path"]) for e in entries
                            if flavour in e["needed_on"]} for flavour in ("v1", "v2")}
    assert {(n.method, n.path) for n in needed_routes(NEEDS, "node")} == by_flavour["v2"]
    assert {(n.method, n.path) for n in needed_routes(NEEDS, "daemon")} == by_flavour["v1"]
    assert {(n.method, n.path) for n in needed_routes(NEEDS, "")} == \
        by_flavour["v1"] & by_flavour["v2"]


@pytest.mark.parametrize("text, code", [
    (None, "route_manifest_missing"),
    ("{not json", "route_manifest_invalid"),
    ('{"routes": []}', "route_manifest_invalid"),
    ('[{"method": "GET"}]', "route_manifest_invalid"),
    ('{"routes": [{"method": "GET", "path": "/x", "call": "/x", "needed_on": ["v3"]}]}',
     "route_manifest_invalid"),
    ('{"routes": [{"method": "FETCH", "path": "/x", "call": "/x", "needed_on": ["v1"]}]}',
     "route_manifest_invalid"),
])
def test_a_manifest_that_does_not_load_fails_the_gate_and_says_how_to_regenerate(
        tmp_path, text, code):
    path = tmp_path / "daemon_routes.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")

    needs, error = load_route_manifest(path)

    assert (needs, error) == ((), code)
    result = _gate(RouteProbe(manifest_error=error))
    assert (result.status, result.code) == (FAIL, code)
    assert "--write-manifest" in result.hint


def test_a_route_need_s_shape_and_sample():
    need = RouteNeed("POST", "/api/move/play/recorded-move-dataset/{dataset_name:path}/"
                             "{move_name}",
                     "/api/move/play/recorded-move-dataset/pollen-robotics/x/{}?a=1")

    assert need.shape == "/api/move/play/recorded-move-dataset/{}/{}"
    assert need.sample == "/api/move/play/recorded-move-dataset/pollen-robotics/x/0"
    assert need.has_params and not RouteNeed("GET", "/a", "/a").has_params


# --- collect() ------------------------------------------------------------------------
class _RoutesProbe(_FakeProbe):
    def __init__(self, environ, answer: RouteProbe) -> None:
        super().__init__(environ)
        self.asked: list = []
        self._answer = answer

    def daemon_routes(self, base_url, needs):
        self.asked.append((base_url, needs))
        return self._answer


def test_collect_asks_the_configured_daemon_for_the_manifest_once():
    answer = replace(RouteProbe(needs=NEEDS), reached=True, openapi=())
    probe = _RoutesProbe({"REACHY_BASE_URL": "http://192.0.2.10:8000/"}, answer)

    evidence = collect(probe)

    assert probe.asked == [("http://192.0.2.10:8000", NEEDS)]
    assert evidence.daemon_routes is answer


def test_collect_with_a_probe_that_cannot_ask_records_the_manifest_and_skips():
    evidence = collect(_FakeProbe({}))

    assert evidence.daemon_routes == RouteProbe(needs=NEEDS)
    assert gate_daemon_routes(replace(evidence, daemon=V2_STATUS)).code == "routes_not_probed"
