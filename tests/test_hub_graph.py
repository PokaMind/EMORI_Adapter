"""The graph's nodes in the hub's process: what they are fed, and what they cannot do.

ROS is not on a development computer, so the real node classes run here on small fakes of
``rclpy`` and the message packages. Nothing needs Docker or a robot.

Three things are held here. The adapter node gets every relayed envelope through the
fan-out, through the same parsing and the same privacy gate as over the socket. That costs
the thread a session's motion runs on one queue put, whatever the node is doing, and what
the node cannot keep up with is counted. And the skill facade, now that it lives beside the
session, still moves nothing unless ``EMORI_ROS_MOTION_OWNER`` says the graph owns motion.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from emori_adapter.hostapp.relay import RelayHub
from emori_adapter.models import AnalysisCycle
from emori_adapter.sources import websocket as ws

_ROOT = Path(__file__).resolve().parents[1]
_WS = _ROOT / "ros2_ws" / "src"
_HUB_PACKAGE = _WS / "emori" / "emori_hub"
_ADAPTER_PACKAGE = _WS / "emori" / "emori_adapter_ros"
_FACADE_PACKAGE = _WS / "open" / "emori_skill_facade"
_GRAPH_YAML = _HUB_PACKAGE / "config" / "graph.yaml"
for _path in (_HUB_PACKAGE, _ADAPTER_PACKAGE, _FACADE_PACKAGE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from emori_hub import graph  # noqa: E402
from emori_hub import main as hub_main  # noqa: E402


def _load_snapshot_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "hub_env_snapshot_graph", _ROOT / "deploy" / "hub" / "env-snapshot.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


snap = _load_snapshot_script()


def _wait_until(predicate: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    return predicate()


# --- the fakes --------------------------------------------------------------------------


class _Message:
    """Any message: fields are set as they are in the real one, stamp fields already there."""

    def __init__(self, **fields: Any) -> None:
        self.header = SimpleNamespace(stamp=None, frame_id="")
        self.source_time = SimpleNamespace(sec=0, nanosec=0)
        self.__dict__.update(fields)


class _Publisher:
    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.sent: list[Any] = []
        self.threads: set[str] = set()

    def publish(self, message: Any) -> None:
        self.threads.add(threading.current_thread().name)
        self.sent.append(message)


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _say(self, line: str) -> None:
        self.lines.append(line)

    info = warn = warning = error = debug = _say


class _Node:
    """rclpy's ``Node``, with a parameter taken from what the graph's YAML gives this name."""

    params_by_node: dict[str, dict[str, Any]] = {}

    def __init__(self, name: str) -> None:
        self.node_name = name
        self.parameters: dict[str, Any] = {}
        self.publishers: dict[str, _Publisher] = {}
        self.timers: list[Any] = []
        self.subscriptions: list[Any] = []
        self.logger = _Logger()
        self.destroyed = False

    def declare_parameter(self, name: str, default: Any = None) -> None:
        self.parameters[name] = self.params_by_node.get(self.node_name, {}).get(name, default)

    def get_parameter(self, name: str) -> Any:
        return SimpleNamespace(value=self.parameters[name])

    def create_publisher(self, _type: Any, topic: str, _qos: Any) -> _Publisher:
        self.publishers[topic] = _Publisher(topic)
        return self.publishers[topic]

    def create_subscription(self, *args: Any, **kwargs: Any) -> Any:
        self.subscriptions.append(args)
        return object()

    def create_timer(self, period: float, callback: Any) -> Any:
        timer = SimpleNamespace(period=period, callback=callback, cancel=lambda: None)
        self.timers.append(timer)
        return timer

    def get_logger(self) -> _Logger:
        return self.logger

    def get_clock(self) -> Any:
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "now"))

    def destroy_node(self) -> bool:
        self.destroyed = True
        return True


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _message_type(name: str) -> type:
    return type(name, (_Message,), {})


def _action_type(name: str) -> type:
    return type(name, (), {"Result": staticmethod(lambda: SimpleNamespace())})


@pytest.fixture()
def fake_ros(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """rclpy and the message packages, the graph's real parameters, for one test."""

    params = yaml.safe_load(_GRAPH_YAML.read_text(encoding="utf-8"))
    monkeypatch.setattr(_Node, "params_by_node", {
        name: section["ros__parameters"] for name, section in params.items()})
    fakes = {
        "rclpy": _module("rclpy", init=lambda **_: None, spin=lambda *_a, **_k: None),
        "rclpy.node": _module("rclpy.node", Node=_Node),
        "rclpy.qos": _module("rclpy.qos", QoSProfile=lambda **_: "qos",
                             DurabilityPolicy=SimpleNamespace(TRANSIENT_LOCAL=1),
                             ReliabilityPolicy=SimpleNamespace(RELIABLE=1)),
        "rclpy.action": _module("rclpy.action", ActionServer=lambda *_a, **_k: object()),
        "rclpy.callback_groups": _module("rclpy.callback_groups",
                                         ReentrantCallbackGroup=lambda: object()),
        "rclpy.executors": _module("rclpy.executors", MultiThreadedExecutor=object),
        "std_msgs": _module("std_msgs"),
        "std_msgs.msg": _module("std_msgs.msg", Bool=_message_type("Bool")),
        "hri_msgs": _module("hri_msgs"),
        "hri_msgs.msg": _module("hri_msgs.msg", EngagementLevel=_message_type("EngagementLevel"),
                                IdsList=_message_type("IdsList")),
        "emori_msgs": _module("emori_msgs"),
        "emori_msgs.msg": _module("emori_msgs.msg", Context=_message_type("Context"),
                                  SpeechCue=_message_type("SpeechCue"),
                                  SetExpression=_message_type("SetExpression")),
        "emori_msgs.action": _module("emori_msgs.action", LookAt=_action_type("LookAt"),
                                     ReplayMotion=_action_type("ReplayMotion")),
        "diagnostic_msgs": _module("diagnostic_msgs"),
        "diagnostic_msgs.msg": _module(
            "diagnostic_msgs.msg", DiagnosticArray=_message_type("DiagnosticArray"),
            DiagnosticStatus=_message_type("DiagnosticStatus"),
            KeyValue=_message_type("KeyValue")),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("emori_adapter_ros.publisher_node", "emori_skill_facade.facade_node"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return fakes["rclpy"]


def _adapter_node(fake_ros: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("EMORI_ROS_ID_SALT", raising=False)
    monkeypatch.delenv("EMORI_ROS_DEIDENTIFY", raising=False)
    monkeypatch.delenv("EMORI_ROS_SPEECH_TEXT", raising=False)
    publisher_node = importlib.import_module("emori_adapter_ros.publisher_node")
    return publisher_node.EmoriAdapterNode()


def _analysis(session: str = "s-1", seq: int = 1, **fields: Any) -> dict:
    return {"type": ws.ANALYSIS_EVENT, "session_id": session, "person_id": "student-7", "seq": seq,
            "timestamp": "2026-01-02T03:04:05Z",
            "body_language": {"posture": "leaning_forward", "engagement_score": 70},
            "body_language_metrics": {"engagement": 70, "confidence": 60}, **fields}


def _utterance(text: str = "Tell me how your week went.", **fields: Any) -> dict:
    return {"type": ws.AI_UTTERANCE_EVENT, "session_id": "s-1", "person_id": "student-7", "seq": 2,
            "text": text, "cue_category": "warm", "cue_body": "kindly", "lang": "en",
            "source": "turn", "timestamp": "2026-01-02T03:04:05Z", **fields}


def _app(fanout: Any = None) -> Any:
    state = SimpleNamespace(hub=RelayHub())
    if fanout is not None:
        state.fanout = fanout
    return SimpleNamespace(state=state)


# --- one parser for the socket and the feed --------------------------------------------


def test_the_mapping_names_a_cycle_a_sentence_an_end_of_turn_and_nothing_else():
    assert isinstance(ws.event_from_envelope(_analysis()), AnalysisCycle)
    sentence = ws.event_from_envelope(_utterance())
    assert isinstance(sentence, ws.SpeechEvent) and sentence.text and not sentence.ended
    ended = ws.event_from_envelope({"type": ws.TTS_ENDED_EVENT, "seq": 9})
    assert isinstance(ended, ws.SpeechEvent) and ended.ended and ended.text == ""
    for kind in (ws.COMPLETED_EVENT, ws.TURN_ARMED_EVENT, ws.TURN_SUBMITTED_EVENT,
                 ws.TURN_REJECTED_EVENT, "pokamind_something_new", None):
        assert ws.event_from_envelope({"type": kind}) is None


def test_a_null_body_value_is_dropped_on_the_shared_path_and_raises_nothing():
    cycle = ws.event_from_envelope(_analysis(body_language_metrics={"engagement": None}))
    assert cycle.body_language_metrics == {}


class _Socket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages

    def __enter__(self) -> "_Socket":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self.messages)


def test_the_socket_reads_with_the_same_mapping_and_still_stops_at_a_completion(monkeypatch):
    envelopes = [_analysis(), _utterance(), {"type": ws.TURN_ARMED_EVENT},
                 {"type": ws.TTS_ENDED_EVENT, "seq": 3}, {"type": ws.COMPLETED_EVENT},
                 _analysis(seq=99)]
    client = _module("websockets.sync.client",
                     connect=lambda _url: _Socket([json.dumps(item) for item in envelopes]))
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)

    read = list(ws.PokamindBridgeSource("ws://relay", max_retries=0).events())

    assert read == [ws.event_from_envelope(item, default_session="live-session")
                    for item in (envelopes[0], envelopes[1], envelopes[3])]


# --- the adapter node on the hub source ------------------------------------------------


def test_the_hub_source_starts_no_reader_no_queue_and_no_timer(fake_ros, monkeypatch):
    before = {thread.name for thread in threading.enumerate()}
    node = _adapter_node(fake_ros, monkeypatch)
    assert node.source_kind == "hub"
    assert node.timers == []
    assert not hasattr(node, "_queue") and not hasattr(node, "_reader")
    assert "pokamind-reader" not in {thread.name for thread in threading.enumerate()} - before


def test_every_cycle_is_published_at_once_and_none_is_sampled(fake_ros, monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    # More than the old reader's queue held, with no timer tick in between.
    for seq in range(1, 101):
        node.feed(_analysis(seq=seq))
    contexts = node.publishers["/humans/persons/learner_01/context"].sent
    assert [message.seq for message in contexts] == list(range(1, 101))
    assert len(node.publishers["/humans/persons/learner_01/engagement_status"].sent) == 100


def test_an_ai_sentence_passes_the_gate_and_keeps_its_length_with_the_gate_closed(fake_ros,
                                                                                  monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    node.feed(_utterance())
    (cue,) = node.publishers["/humans/persons/learner_01/speech_cue"].sent
    assert cue.text == "" and cue.cue_body == ""
    assert cue.est_duration > 0.0
    assert cue.cue_category == "warm"
    assert cue.person_id.startswith("p-") and "student-7" not in cue.person_id
    assert cue.session_id.startswith("p-")


def test_the_end_of_turn_marker_reaches_the_cue_topic(fake_ros, monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    node.feed({"type": ws.TTS_ENDED_EVENT, "seq": 4, "session_id": "s-1"})
    (cue,) = node.publishers["/humans/persons/learner_01/speech_cue"].sent
    assert cue.ended is True


def test_a_completion_publishes_nothing_and_leaves_no_gap_before_the_next_session(fake_ros,
                                                                                  monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    contexts = node.publishers["/humans/persons/learner_01/context"].sent
    node.feed(_analysis(session="s-1"))
    before = {topic: len(publisher.sent) for topic, publisher in node.publishers.items()}
    node.feed({"type": ws.COMPLETED_EVENT, "session_id": "s-1",
               "results": {"overall_score": 90}})
    assert {topic: len(publisher.sent) for topic, publisher in node.publishers.items()} == before
    assert any("live session ended" in line for line in node.logger.lines)
    translator = node._translator
    node.feed(_analysis(session="s-2", seq=1))
    assert len(contexts) == 2
    assert node._translator is not translator, "a new session starts from a fresh translator"


def test_the_events_this_node_has_no_field_for_change_nothing(fake_ros, monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    before = {topic: len(publisher.sent) for topic, publisher in node.publishers.items()}
    for kind in (ws.TURN_ARMED_EVENT, ws.TURN_SUBMITTED_EVENT, ws.TURN_REJECTED_EVENT,
                 "pokamind_user_speaking", "pokamind_something_new"):
        node.feed({"type": kind, "session_id": "s-1"})
    assert {topic: len(publisher.sent) for topic, publisher in node.publishers.items()} == before


def test_freshness_is_reported_on_the_hub_source_as_on_the_socket(fake_ros, monkeypatch):
    node = _adapter_node(fake_ros, monkeypatch)
    node.feed(_analysis())
    (context,) = node.publishers["/humans/persons/learner_01/context"].sent
    assert context.age_s > 0.0


def test_the_demo_source_is_what_an_unset_parameter_still_means(fake_ros, monkeypatch):
    monkeypatch.setattr(_Node, "params_by_node", {})
    node = _adapter_node(fake_ros, monkeypatch)
    assert node.source_kind == "mock"
    assert len(node.timers) == 1


# --- the builders ----------------------------------------------------------------------


def test_the_graph_yaml_names_the_nodes_the_hub_builds_and_feeds_the_adapter_from_the_hub():
    params = yaml.safe_load(_GRAPH_YAML.read_text(encoding="utf-8"))
    assert params["emori_adapter"]["ros__parameters"]["source"] == graph.HUB_SOURCE
    assert set(params) <= {"emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
                           "skill_facade"}


def test_the_graph_yaml_is_installed_where_the_hub_looks_for_it():
    setup = (_HUB_PACKAGE / "setup.py").read_text(encoding="utf-8")
    assert '("share/" + package_name + "/config", ["config/graph.yaml"])' in setup
    main = (_HUB_PACKAGE / "emori_hub" / "main.py").read_text(encoding="utf-8")
    assert 'get_package_share_directory("emori_hub"), "config", "graph.yaml"' in main


@pytest.mark.parametrize("builder, relative, class_name, node_name", [
    (graph.build_adapter_node, "emori/emori_adapter_ros/emori_adapter_ros/publisher_node.py",
     "EmoriAdapterNode", "emori_adapter"),
    (graph.build_context_analyzer,
     "open/hri_context_analyzer/hri_context_analyzer/context_analyzer_node.py",
     "ContextAnalyzerNode", "hri_context_analyzer"),
    (graph.build_behavior_node,
     "open/hri_engagement_behaviors/hri_engagement_behaviors/behavior_node.py",
     "EngagementBehaviorNode", "hri_engagement_behaviors"),
    (graph.build_skill_facade, "open/emori_skill_facade/emori_skill_facade/facade_node.py",
     "SkillFacadeNode", "skill_facade"),
])
def test_each_builder_makes_the_class_that_has_the_name_the_deploy_checks_for(
        builder, relative, class_name, node_name):
    assert builder in graph.GRAPH_BUILDERS
    tree = ast.parse((_WS / relative).read_text(encoding="utf-8"))
    (found,) = [node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name]
    assert any(isinstance(call, ast.Call) and call.args
               and getattr(call.args[0], "value", None) == node_name
               and getattr(call.func, "attr", "") == "__init__" for call in ast.walk(found))
    builder_source = ast.get_source_segment(
        (_HUB_PACKAGE / "emori_hub" / "graph.py").read_text(encoding="utf-8"),
        next(node for node in ast.parse(
            (_HUB_PACKAGE / "emori_hub" / "graph.py").read_text(encoding="utf-8")).body
            if getattr(node, "name", "") == builder.__name__))
    module = relative.split("/")[-1].removesuffix(".py")
    assert f"{module} import {class_name}" in builder_source


def test_the_engagement_node_takes_the_parameter_the_hub_sets():
    source = (_WS / "open" / "hri_engagement_behaviors" / "hri_engagement_behaviors"
              / "behavior_node.py").read_text(encoding="utf-8")
    assert 'declare_parameter("behavior_yaml"' in source
    assert "behavior_yaml:=" in (_HUB_PACKAGE / "emori_hub" / "main.py").read_text(
        encoding="utf-8")


def test_the_fiware_bridge_is_not_one_of_the_graphs_nodes_here():
    assert len(graph.GRAPH_BUILDERS) == 4
    assert "fiware_bridge" not in ast.dump(
        ast.parse((_HUB_PACKAGE / "emori_hub" / "graph.py").read_text(encoding="utf-8")))


def test_the_adapter_builder_refuses_a_node_that_did_not_get_the_hub_source(fake_ros, monkeypatch):
    """A node on its default source would put a made-up person on the real topics."""

    monkeypatch.setattr(_Node, "params_by_node", {})
    monkeypatch.delenv("EMORI_ROS_ID_SALT", raising=False)
    with pytest.raises(RuntimeError, match="source 'mock'"):
        graph.build_adapter_node(_app(), environ={})


def test_the_adapter_node_is_fed_by_a_fan_out_already_on_the_app(fake_ros, monkeypatch):
    from emori_hub.fanout import build_fanout

    app = _app()
    fanout = build_fanout(app, publish=lambda _event: None, environ={})
    try:
        node = graph.build_adapter_node(app, environ={})
        assert app.state.fanout is fanout
        app.state.hub.publish(json.dumps(_analysis()))
        contexts = node.publishers["/humans/persons/learner_01/context"]
        assert _wait_until(lambda: contexts.sent)
        assert contexts.threads == {"ros-fanout"}

        node.destroy_node()
        assert node.destroyed
        assert _wait_until(lambda: fanout.stats()["fed"] == 1)
        app.state.hub.publish(json.dumps(_analysis(seq=2)))
        assert _wait_until(lambda: fanout.stats()["published"] == 2)
        assert fanout.stats()["fed"] == 1, "a destroyed node is not fed"
        assert app.state.hub.tap is not None, "the fan-out is not this node's to remove"
    finally:
        fanout.stop()
        fanout.uninstall()


def test_with_no_session_node_the_adapter_starts_a_fan_out_and_stops_it(fake_ros, monkeypatch):
    app = _app()
    node = graph.build_adapter_node(app, environ={})
    fanout = app.state.fanout
    assert app.state.hub.tap is not None and fanout.stats()["running"]
    app.state.hub.publish(json.dumps(_analysis()))
    assert _wait_until(lambda: node.publishers["/humans/persons/learner_01/context"].sent)
    node.destroy_node()
    assert app.state.hub.tap is None
    assert not fanout.stats()["running"]
    assert node.destroyed


# --- the session's motion path ----------------------------------------------------------


class _StuckNode:
    source_kind = graph.HUB_SOURCE

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads: set[str] = set()
        self.destroyed = False

    def feed(self, _envelope: dict) -> None:
        self.threads.add(threading.current_thread().name)
        self.entered.set()
        self.release.wait(30.0)

    def destroy_node(self) -> bool:
        self.destroyed = True
        return True


def _publish_all(relay: RelayHub, count: int, limit_s: float) -> list[float]:
    """Each publish's time, from a thread of its own so a block fails and does not hang."""

    costs: list[float] = []

    def run() -> None:
        for number in range(count):
            started = time.perf_counter()
            relay.publish(json.dumps(_analysis(seq=number)))
            costs.append(time.perf_counter() - started)

    worker = threading.Thread(target=run, name="fake-request-thread", daemon=True)
    worker.start()
    worker.join(limit_s)
    assert not worker.is_alive(), "the relay's publish blocked behind the adapter node"
    return costs


def test_an_adapter_node_that_is_stuck_costs_the_session_nothing_and_the_loss_is_counted():
    """The relay's tap is on the request thread of POST /api/cycle, so it is on the motion path.

    The node is stuck for the whole test, in the middle of an envelope. Every publish still
    returns, in the time a queue put takes, and each envelope that found the queue full is in
    a number somebody can read.
    """

    app = _app()
    node = _StuckNode()
    built = graph.build_adapter_node(app, node_factory=lambda: node, environ={})
    fanout = app.state.fanout
    try:
        app.state.hub.publish(json.dumps(_analysis()))
        assert node.entered.wait(5.0)

        count = fanout.capacity + 200
        costs = _publish_all(app.state.hub, count, limit_s=5.0)

        assert len(costs) == count
        assert sorted(costs)[len(costs) // 2] < 0.002, "a publish is no longer a queue put"
        stats = fanout.stats()
        assert stats["dropped"] >= 200
        assert stats["offered"] + stats["dropped"] == count + 1
        assert node.threads == {"ros-fanout"}
    finally:
        node.release.set()
        built.destroy_node()


def test_the_tap_the_relay_holds_is_the_fan_outs_offer_and_nothing_of_the_nodes():
    app = _app()
    node = _StuckNode()
    built = graph.build_adapter_node(app, node_factory=lambda: node, environ={})
    try:
        assert app.state.hub.tap == app.state.fanout.offer
    finally:
        node.release.set()
        built.destroy_node()


def test_a_feed_that_raises_is_counted_and_the_next_envelope_still_goes():
    class Flaky(_StuckNode):
        def __init__(self) -> None:
            super().__init__()
            self.fed = 0

        def feed(self, _envelope: dict) -> None:
            self.fed += 1
            if self.fed == 1:
                raise ValueError("bad envelope")

    app = _app()
    node = Flaky()
    built = graph.build_adapter_node(app, node_factory=lambda: node, environ={})
    try:
        for seq in (1, 2):
            app.state.hub.publish(json.dumps(_analysis(seq=seq)))
        assert _wait_until(lambda: node.fed == 2)
        assert _wait_until(lambda: app.state.fanout.stats()["feed_errors"] == 1)
    finally:
        built.destroy_node()


def test_nothing_of_the_graph_module_reaches_for_the_session_or_the_robot():
    tree = ast.parse((_HUB_PACKAGE / "emori_hub" / "graph.py").read_text(encoding="utf-8"))
    imported = {getattr(node, "module", "") or "" for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)}
    assert imported <= {"__future__", "typing", "emori_hub.fanout",
                        "emori_adapter_ros.publisher_node",
                        "hri_context_analyzer.context_analyzer_node",
                        "hri_engagement_behaviors.behavior_node",
                        "emori_skill_facade.facade_node"}
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert names.isdisjoint({"driver", "player", "robot_client", "client", "submit"})


# --- no second source of motion ---------------------------------------------------------


class _SpyTransport:
    name = "ros_native"

    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def describe(self) -> str:
        return "spy"

    def submit(self, body: dict, _reason: str) -> Any:
        from emori_skill_facade.contract import SkillOutcome

        self.submitted.append(body)
        return SkillOutcome(True, "", "accepted")

    def close(self) -> None:
        return None


def _hub_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                     host_env: dict[str, str]) -> None:
    """The real way there: the unit's environment, the snapshot, the hub's load of it."""

    target = tmp_path / "env.json"
    motor = tmp_path / "motor.env"
    motor.write_text("EMORI_ROS_SPEECH_TEXT=0\n", encoding="utf-8")
    assert snap.main(["env-snapshot.py", str(target)], {"EMORI_HUB_GRAPH": "1", **host_env},
                     motor_env=str(motor)) == 0
    environ = {hub_main.ENV_JSON_VAR: str(target)}
    assert hub_main.load_env_snapshot(environ) is not None
    for name in ("EMORI_ROS_MOTION_OWNER", "EMORI_SKILL_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def _facade(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _SpyTransport]:
    module = importlib.import_module("emori_skill_facade.facade_node")
    spy = _SpyTransport()
    asked: list[str] = []

    def select(requested: str, **_kwargs: Any) -> _SpyTransport:
        asked.append(requested)
        return spy

    monkeypatch.setattr(module, "select_transport", select)
    node = graph.build_skill_facade(object())
    assert asked == ["ros_native"]
    return node, spy


def _ask_for_motion(node: Any) -> None:
    node._on_expression(SimpleNamespace(label="happy", confidence=0.9, reason="test"))
    goal = SimpleNamespace(
        request=SimpleNamespace(name="nod", intensity=0.5, yaw_deg=10.0, pitch_deg=0.0,
                                duration=1.0, reason="test"),
        succeed=lambda: None)
    node._execute_replay(goal)
    node._execute_look_at(goal)


def test_with_the_hostapp_as_owner_the_facade_in_the_hub_counts_and_sends_nothing(
        fake_ros, tmp_path, monkeypatch):
    _hub_environment(tmp_path, monkeypatch, {"EMORI_ROS_MOTION_OWNER": "hostapp"})
    node, spy = _facade(monkeypatch)
    _ask_for_motion(node)
    assert node.motion_owner == "hostapp"
    assert spy.submitted == []
    assert node.counters["suppressed"] == 3 and node.counters["posted"] == 0


def test_with_no_owner_written_anywhere_the_facade_still_sends_nothing(
        fake_ros, tmp_path, monkeypatch):
    _hub_environment(tmp_path, monkeypatch, {})
    node, spy = _facade(monkeypatch)
    _ask_for_motion(node)
    assert node.motion_owner == "hostapp"
    assert spy.submitted == []


def test_a_mistyped_owner_sends_nothing(fake_ros, tmp_path, monkeypatch):
    _hub_environment(tmp_path, monkeypatch, {"EMORI_ROS_MOTION_OWNER": "ross"})
    node, spy = _facade(monkeypatch)
    _ask_for_motion(node)
    assert spy.submitted == []


def test_only_an_owner_of_ros_opens_it_so_the_tests_above_can_fail(fake_ros, tmp_path, monkeypatch):
    _hub_environment(tmp_path, monkeypatch, {"EMORI_ROS_MOTION_OWNER": "ros"})
    node, spy = _facade(monkeypatch)
    _ask_for_motion(node)
    assert len(spy.submitted) == 3


def test_neither_the_parameter_file_nor_the_motor_env_block_can_name_the_owner(tmp_path):
    source = yaml.safe_load(_GRAPH_YAML.read_text(encoding="utf-8"))
    for section in source.values():
        assert not {"motion_owner", "transport"} & set(section["ros__parameters"])

    target = tmp_path / "env.json"
    motor = tmp_path / "motor.env"
    motor.write_text("EMORI_ROS_MOTION_OWNER=ros\n", encoding="utf-8")
    assert snap.main(["env-snapshot.py", str(target)],
                     {"EMORI_HUB_GRAPH": "1", "EMORI_ROS_MOTION_OWNER": "hostapp"},
                     motor_env=str(motor)) == 0
    environ = {hub_main.ENV_JSON_VAR: str(target)}
    hub_main.load_env_snapshot(environ)
    assert environ["EMORI_ROS_MOTION_OWNER"] == "hostapp"


def test_the_settings_are_documented_and_say_which_file_they_live_in():
    example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    docs = (_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    for name in ("EMORI_HUB_GRAPH", "EMORI_MOTOR_LAUNCH"):
        assert f"#{name}=" in example
        assert f"### `{name}`" in docs
    assert "EMORI_HUB_GRAPH=1: the hub reads them from that file" in example
    assert "the hub reads them from that\nfile" in docs


def test_the_session_code_ignores_the_owner_fallback_once_the_graph_is_here(tmp_path, monkeypatch):
    """The window would open on every session and put a second writer on the head."""

    from emori_adapter.hostapp.driver import owner_fallback_s

    monkeypatch.setenv("EMORI_ROS_OWNER_FALLBACK_S", "30")
    monkeypatch.setenv("EMORI_SKILL_TRANSPORT", "hostapp_http")
    assert owner_fallback_s() == 30.0

    _hub_environment(tmp_path, monkeypatch, {"EMORI_ROS_OWNER_FALLBACK_S": "30"})

    assert owner_fallback_s() == 0.0
