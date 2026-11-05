"""``/emori/gateway``: the status service, ``/diagnostics``, and the three facades together.

The status service calls the functions ``GET /healthz`` and ``GET /api/status`` are served
with, so the first tests build a real app and compare. What leaves is that answer minus the
credential in force and with the session's identifiers through the privacy gate. The
diagnostics are built from counters the facades hold, so the tick can be shown to read
nothing of the session's. The last test runs a real sentence through a real route while all
three nodes tick on other threads, and requires the motion it plans to be the motion it
plans alone.

No ROS is needed. The nodes are fakes that an executor's calls are made on directly.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
_ROS_PACKAGE = _ROOT / "ros2_ws" / "src" / "emori" / "emori_adapter_ros"
for _path in (_HUB, _ROS_PACKAGE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from emori_adapter_ros.privacy import Privacy  # noqa: E402
from emori_hub import addon_node, facade_kit, motion_node  # noqa: E402
from emori_hub import gateway_node as gn  # noqa: E402
from emori_hub.fanout import FanOut  # noqa: E402
from emori_hub.gateway_node import GatewayFacade, build_gateway_node, scrub  # noqa: E402

from emori_adapter.hostapp import server  # noqa: E402
from emori_adapter.hostapp.driver import SessionInfo  # noqa: E402
from emori_adapter.hostapp.extensions import HostExtension  # noqa: E402
from emori_adapter.robot.client import DryRunRobotClient  # noqa: E402
from tests import hub_node_fakes as fakes  # noqa: E402

_GATEWAY_SOURCE = Path(gn.__file__)
SALT = "gateway-test-salt"


def _privacy(*, deidentify: bool = True) -> Privacy:
    return Privacy(deidentify=deidentify, speech_text=False, salt=SALT)


def _types() -> dict[str, Any]:
    return {"GetHubStatus": fakes.srv_type("GetHubStatus"), **fakes.diagnostic_types()}


def _route(path: str, answer: Any) -> Any:
    calls: list[int] = []

    def endpoint() -> Any:
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return SimpleNamespace(path=path, methods={"GET"}, endpoint=endpoint, calls=calls)


def _fake_app(healthz: Any = None, status: Any = None, client: Any = None, fanout: Any = None):
    routes = [_route("/healthz", healthz if healthz is not None else
                     {"status": "ok", "demo": False, "robot": True, "runtime": "http"}),
              _route("/api/status", status if status is not None else {"counters": {}})]
    state = SimpleNamespace(driver=SimpleNamespace(
        client=client if client is not None else DryRunRobotClient(),
        tracker=SimpleNamespace(active=False)))
    if fanout is not None:
        state.fanout = fanout
    return SimpleNamespace(routes=routes, state=state)


def _facade(app: Any, privacy: Any = None, monotonic: Any = time.monotonic):
    published: list[list[gn.DiagnosticEntry]] = []
    facade = GatewayFacade(app, publish_diagnostics=published.append,
                           privacy=privacy if privacy is not None else _privacy(),
                           monotonic=monotonic)
    return facade, published


def _status(facade: GatewayFacade) -> Any:
    return facade.status(None, fakes.srv_type("GetHubStatus").Response())


class _Link:
    def __init__(self, reason: str = "", unmatched: str = "") -> None:
        self.reason, self.unmatched = reason, unmatched

    def down_reason(self) -> str:
        return self.reason

    def first_unmatched(self) -> str:
        return self.unmatched


# --- the status service on a real app --------------------------------------------------


def _build_real_app() -> Any:
    plans: list[list] = []
    player = SimpleNamespace(busy=False, plans=plans)
    player.submit = lambda intents, *, drop_if_busy=False, guard=None, yields=None: (
        plans.append(list(intents)) or True)
    return server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": player, "fiware_factory": None,
        "idle": False, "watchdog": False, "gesture": False, "tts": None, "stt": None,
        "extension": HostExtension()})


@pytest.fixture()
def real_app(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "lang.json"))
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "PARTNER", "test-partner")
    monkeypatch.setattr(server, "POKAMIND_BASE", "http://localhost:4000")
    return _build_real_app()


def test_the_service_answers_what_healthz_and_api_status_answer(real_app):
    facade, _published = _facade(real_app)
    response = _status(facade)
    body = json.loads(response.status_json)
    assert (response.runtime, response.demo, response.robot) == ("http", False, True)

    status = real_app.state.driver.status()
    assert body["status"]["counters"] == status["counters"]
    assert set(body["healthz"]) == {"status", "demo", "partner", "robot", "runtime",
                                    "roster_staff"}
    assert body["healthz"]["partner"] == "test-partner"


def test_nothing_the_api_status_route_returns_is_lost_to_the_secret_net(real_app):
    """The net for key names must not eat a real field: every key of the route is in the answer."""

    route = facade_kit.find_endpoint(real_app, "/api/status")
    expected = set(route())
    facade, _published = _facade(real_app)
    assert set(json.loads(_status(facade).status_json)["status"]) == expected


def test_the_credential_in_force_and_its_fingerprint_stay_on_http(real_app):
    healthz = facade_kit.find_endpoint(real_app, "/healthz")()
    assert "pokamind_credential" in healthz and "pokamind_key_fingerprint" in healthz
    facade, _published = _facade(real_app)
    text = _status(facade).status_json
    assert "pokamind_credential" not in text and "fingerprint" not in text


def test_the_open_sessions_ids_pass_the_privacy_gate(real_app):
    real_app.state.driver.start_session(SessionInfo(session_id="s-1", person_id="student-9",
                                                    lang="en", topic="intro"))
    facade, _published = _facade(real_app, _privacy())
    text = _status(facade).status_json
    session = json.loads(text)["status"]["session"]
    expected_person, expected_session = _privacy().apply_ids("student-9", "s-1")
    assert (session["person_id"], session["session_id"]) == (expected_person, expected_session)
    assert "student-9" not in text and "s-1" not in text.replace(expected_session, "")
    assert (session["lang"], session["topic"]) == ("en", "intro")


def test_the_trace_session_is_pseudonymised_too():
    status = {"session": None, "motion_trace": {"enabled": True, "session": "raw-session-id"}}
    facade, _published = _facade(_fake_app(status=status))
    trace = json.loads(_status(facade).status_json)["status"]["motion_trace"]
    assert trace["session"] == _privacy().apply_ids("", "raw-session-id")[1]
    assert "raw-session-id" not in json.dumps(trace)


def test_ids_are_the_raw_ones_only_when_the_operator_turned_the_gate_off():
    status = {"session": {"session_id": "s-1", "person_id": "p-1", "lang": "en"}}
    facade, _published = _facade(_fake_app(status=status), _privacy(deidentify=False))
    session = json.loads(_status(facade).status_json)["status"]["session"]
    assert (session["session_id"], session["person_id"]) == ("s-1", "p-1")


def test_a_session_with_no_ids_stays_empty_and_not_a_pseudonym():
    status = {"session": {"session_id": "", "person_id": "", "lang": "en"}}
    facade, _published = _facade(_fake_app(status=status))
    session = json.loads(_status(facade).status_json)["status"]["session"]
    assert session["session_id"] == "" and session["person_id"] == ""


# --- the status service, unit ----------------------------------------------------------


def test_runtime_demo_and_robot_come_from_healthz():
    app = _fake_app(healthz={"status": "ok", "demo": True, "robot": False, "runtime": "ros"})
    response = _status(_facade(app)[0])
    assert (response.runtime, response.demo, response.robot) == ("ros", True, False)


def test_an_unknown_runtime_reads_as_http():
    app = _fake_app(healthz={"runtime": "something else"})
    assert _status(_facade(app)[0]).runtime == "http"


def test_one_answer_is_handed_to_every_caller_within_its_time():
    now = [0.0]
    app = _fake_app()
    facade, _published = _facade(app, monotonic=lambda: now[0])
    for _ in range(50):
        _status(facade)
    assert [len(route.calls) for route in app.routes] == [1, 1]
    now[0] = gn.STATUS_TTL_S + 0.01
    _status(facade)
    assert [len(route.calls) for route in app.routes] == [2, 2]
    assert facade.stats()["status_calls"] == 51 and facade.stats()["status_built"] == 2


def test_a_session_makes_the_robot_calls_rarely():
    now = [0.0]
    app = _fake_app()
    facade, _published = _facade(app, monotonic=lambda: now[0])
    app.state.driver.tracker.active = True
    _status(facade)
    now[0] = gn.STATUS_TTL_S + 0.01
    _status(facade)
    assert [len(route.calls) for route in app.routes] == [1, 1], "still inside a session's time"
    now[0] = gn.SESSION_STATUS_TTL_S + 0.01
    _status(facade)
    assert [len(route.calls) for route in app.routes] == [2, 2]


def test_a_route_that_raises_costs_that_half_and_not_the_answer():
    app = _fake_app(status=RuntimeError("boom"))
    facade, _published = _facade(app)
    response = _status(facade)
    body = json.loads(response.status_json)
    assert response.robot is True and body["status"] == {} and body["healthz"]["status"] == "ok"
    assert facade.stats()["route_errors"] == 1
    assert facade.health().level == facade_kit.WARN


def test_an_app_with_no_such_routes_still_answers():
    app = SimpleNamespace(routes=[], state=SimpleNamespace(driver=SimpleNamespace(client=None)))
    response = _status(_facade(app)[0])
    assert (response.runtime, response.robot) == ("http", False)
    assert json.loads(response.status_json) == {"healthz": {}, "status": {}}


def test_a_json_response_is_read_by_its_body():
    class Response:
        body = b'{"error_code":"addon_unavailable"}'

    app = _fake_app(status=Response())
    assert json.loads(_status(_facade(app)[0]).status_json)["status"] == {
        "error_code": "addon_unavailable"}


def test_an_answer_that_is_too_long_keeps_healthz_and_says_it_was_cut():
    app = _fake_app(status={"blob": "x" * gn.STATUS_JSON_MAX})
    body = json.loads(_status(_facade(app)[0]).status_json)
    assert body["status"] == {"truncated": True} and body["healthz"]["status"] == "ok"


def test_the_secret_net_drops_a_key_at_any_depth():
    cleaned = scrub({"ok": 1, "robot_token": "t", "nested": {"api_key": "k", "fine": 2,
                                                              "list": [{"password": "p", "x": 1}]},
                     "Pokamind_Credential": "c", "salt": "s"})
    assert cleaned == {"ok": 1, "nested": {"fine": 2, "list": [{"x": 1}]}}


def test_the_service_never_raises_whatever_the_routes_do():
    class Hostile(dict):
        def __iter__(self):
            raise RuntimeError("no")

    app = _fake_app(healthz=Hostile(), status=Hostile())
    response = _status(_facade(app)[0])
    assert response.runtime in ("http", "ros") and response.status_json


# --- diagnostics -----------------------------------------------------------------------


def _entries(facade: GatewayFacade) -> dict[str, gn.DiagnosticEntry]:
    return {entry.name: entry for entry in facade.collect()}


def test_the_fan_out_is_ok_while_it_keeps_up_and_warns_when_it_drops():
    stats = {"running": True, "capacity": 256, "depth": 0, "dropped": 0, "publish_errors": 0,
             "feed_errors": 0, "mapping_errors": 0, "busy_for_s": None}
    fanout = SimpleNamespace(stats=lambda: dict(stats))
    facade, _published = _facade(_fake_app(fanout=fanout))
    assert _entries(facade)["emori_hub/session_fanout"].level == facade_kit.OK
    stats["dropped"] = 3
    entry = _entries(facade)["emori_hub/session_fanout"]
    assert entry.level == facade_kit.WARN
    assert "3 envelopes dropped" in entry.message and "motion does not wait" in entry.message
    assert entry.values["dropped"] == "3"


def test_a_fan_out_that_is_not_running_is_an_error():
    fanout = SimpleNamespace(stats=lambda: {"running": False})
    facade, _published = _facade(_fake_app(fanout=fanout))
    assert _entries(facade)["emori_hub/session_fanout"].level == facade_kit.ERROR


def test_a_sink_that_holds_the_fan_out_is_a_warning():
    fanout = SimpleNamespace(stats=lambda: {"running": True, "busy_for_s": 9.0})
    facade, _published = _facade(_fake_app(fanout=fanout))
    assert _entries(facade)["emori_hub/session_fanout"].level == facade_kit.WARN


def test_a_real_fan_outs_stats_are_what_the_gateway_reads():
    fanout = FanOut(publish=lambda event: None, privacy=_privacy())
    fanout.start()
    try:
        facade, _published = _facade(_fake_app(fanout=fanout))
        assert _entries(facade)["emori_hub/session_fanout"].level == facade_kit.OK
    finally:
        fanout.stop()


def test_the_robot_link_is_down_with_its_reason_or_up():
    link = _Link("service /reachy_mini/submit_move is not matched", "submit_move")
    app = _fake_app(client=SimpleNamespace(runtime="ros", link=link))
    entry = _entries(_facade(app)[0])["emori_hub/robot_link"]
    assert entry.level == facade_kit.ERROR
    assert "submit_move" in entry.message and entry.values["first_unmatched"] == "submit_move"
    link.reason = link.unmatched = ""
    entry = _entries(_facade(app)[0])["emori_hub/robot_link"]
    assert entry.level == facade_kit.OK and entry.values["runtime"] == "ros"


def test_over_rest_the_robot_link_says_so_and_asks_nothing():
    asked: list[str] = []

    class Rest:
        runtime = "http"

        def health(self) -> bool:
            asked.append("health")
            return True

    entry = _entries(_facade(_fake_app(client=Rest()))[0])["emori_hub/robot_link"]
    assert entry.level == facade_kit.OK and entry.values["runtime"] == "http"
    assert asked == [], "the diagnostics tick called the robot"


def test_the_ros_client_exposes_its_link_for_the_gateway():
    from emori_hub.robot_link import RosRobotClient

    client = object.__new__(RosRobotClient)
    link = _Link()
    client._link = link
    assert client.link is link


def test_a_registered_source_is_an_entry_and_a_raising_one_hides_no_other():
    app = _fake_app()
    facade_kit.register_diagnostics(app, "b_ok", lambda: facade_kit.Health(
        facade_kit.WARN, "slow", {"n": 4}))

    def broken() -> facade_kit.Health:
        raise RuntimeError("source fell over")

    facade_kit.register_diagnostics(app, "a_broken", broken)
    facade, _published = _facade(app)
    entries = _entries(facade)
    assert entries["emori_hub/a_broken"].level == facade_kit.STALE
    assert (entries["emori_hub/b_ok"].level, entries["emori_hub/b_ok"].values) == (
        facade_kit.WARN, {"n": "4"})
    assert "emori_hub/robot_link" in entries and "emori_hub/gateway" in entries
    assert facade.stats()["source_errors"] == 1
    assert list(entries) == sorted(entries)


def test_the_tick_reads_no_session_state():
    """Nothing on the tick reads the driver beyond its client, so it takes no session lock."""

    class Driver:
        client = DryRunRobotClient()

        def __getattribute__(self, name: str) -> Any:
            if name not in ("client",):
                raise AssertionError(f"the diagnostics tick read driver.{name}")
            return super().__getattribute__(name)

    app = SimpleNamespace(routes=[], state=SimpleNamespace(driver=Driver()))
    facade, published = _facade(app)
    facade.tick()
    assert len(published) == 1


# --- the node --------------------------------------------------------------------------


def _node(app: Any = None, privacy: Any = None):
    bindings, nodes, groups = fakes.bindings(_types())
    node = build_gateway_node(app if app is not None else _fake_app(), bindings,
                              privacy=privacy if privacy is not None else _privacy())
    return node, nodes, groups


def test_the_node_is_emori_gateway_with_the_status_service_and_the_diagnostics_topic():
    node, nodes, groups = _node()
    assert nodes == [node] and (node.name, node.namespace) == ("gateway", "/emori")
    assert list(node.services) == ["/emori/gateway/status"]
    assert node.publishers["/diagnostics"].qos == "default"
    assert [timer.period for timer in node.timers] == [1.0]
    assert node.timers[0].group is not node.services["/emori/gateway/status"].group
    assert len(groups.made) == 2


def test_the_service_fills_every_field_of_the_interface():
    node, _nodes, _groups = _node(_fake_app(
        healthz={"demo": True, "robot": True, "runtime": "ros", "status": "ok"}))
    response = node.services["/emori/gateway/status"].call()
    assert (response.runtime, response.demo, response.robot) == ("ros", True, True)
    assert json.loads(response.status_json)["healthz"]["status"] == "ok"


def test_the_diagnostic_array_is_filled_as_rclpy_wants_it():
    app = _fake_app()
    facade_kit.register_diagnostics(app, "x", lambda: facade_kit.Health(
        facade_kit.WARN, "careful", {"count": 5, "name": "v"}))
    node, _nodes, _groups = _node(app)
    node.timers[0].fire()
    (array,) = node.publishers["/diagnostics"].published
    assert array.header.stamp.sec == 0
    statuses = {status.name: status for status in array.status}
    status = statuses["emori_hub/x"]
    assert status.level == bytes([facade_kit.WARN]) and status.message == "careful"
    assert status.hardware_id == "emori-hub"
    assert {pair.key: pair.value for pair in status.values} == {"count": "5", "name": "v"}
    assert all(isinstance(s.level, bytes) and len(s.level) == 1 for s in array.status)


def test_the_gateway_registers_its_own_counters():
    node, _nodes, _groups = _node()
    node.timers[0].fire()
    names = [status.name for status in node.publishers["/diagnostics"].published[0].status]
    assert "emori_hub/gateway" in names


def test_this_module_takes_no_lock_and_calls_no_player():
    import ast

    tree = ast.parse(_GATEWAY_SOURCE.read_text(encoding="utf-8"))
    names = {node.attr if isinstance(node, ast.Attribute) else node.id
             for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Name))}
    assert not names & {"Lock", "RLock", "Condition", "Queue", "player", "submit",
                        "submit_legacy_motion"}


# --- all three facades beside a session ------------------------------------------------


def _hub_nodes(app: Any, extension: Any) -> list[Any]:
    bindings_motion, _n1, _g1 = fakes.bindings({
        "PlayerState": fakes.msg_type("PlayerState"),
        "MotionPlanEvent": fakes.msg_type("MotionPlanEvent"),
        "SubmitMotionBatch": fakes.srv_type("SubmitMotionBatch")})
    bindings_addon, _n2, _g2 = fakes.bindings({"AddonStatus": fakes.msg_type("AddonStatus")})
    bindings_gateway, _n3, _g3 = fakes.bindings(_types())
    return [
        motion_node.build_motion_node(app, bindings_motion, environ={}),
        addon_node.build_addon_node(app, bindings_addon),
        build_gateway_node(app, bindings_gateway, privacy=_privacy()),
    ]


def _sentence_plan(app: Any) -> list[tuple]:
    from fastapi.testclient import TestClient

    plans = app.state.driver.player.plans
    with TestClient(app) as client:
        response = client.post("/api/cycle", json={
            "type": "pokamind_ai_utterance", "seq": 1, "source": "turn", "lang": "en",
            "text": "I am so very sorry for all of this."})
        assert response.status_code == 200 and response.json().get("ok") is True
    return [(i.kind, i.reason, i.speech, i.pace, i.supersede, i.tint)
            for batch in plans for i in batch]


def test_a_sentence_is_planned_to_the_same_motion_with_every_facade_ticking(real_app):
    """Each run is on an app of its own, because planning remembers what it varied last."""

    alone = _sentence_plan(real_app)
    assert alone, "the sentence planned no motion at all, so there is nothing to compare"
    assert _sentence_plan(_build_real_app()) == alone, "planning is not repeatable"

    ticking = _build_real_app()
    nodes = _hub_nodes(ticking, ticking.state.extension)
    timers = [timer for node in nodes for timer in node.timers]
    status = nodes[2].services["/emori/gateway/status"]
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            for timer in timers:
                timer.fire()
            status.call()

    threads = [threading.Thread(target=churn, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        assert _sentence_plan(ticking) == alone
    finally:
        stop.set()
        for thread in threads:
            thread.join(10.0)
    for node in nodes:
        assert node.facade.guard.errors == 0
