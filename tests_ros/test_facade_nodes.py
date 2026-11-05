"""The hub's facade nodes and graph nodes against a real rclpy, over real DDS.

``tests/test_hub_*`` build every node on fakes of ``rclpy`` and of the message packages. A fake
cannot say that ``Node(name, namespace=..., start_parameter_services=False)`` is accepted, that
a service answers with the interface's fields, that rosidl takes the value a node assigns to a
field (it rejects a wrong type at assignment), that a latched sample reaches a late subscriber,
or that destroying the nodes in the hub's order leaves nothing running. These tests build the
real app (``server._build_app``, no lifespan, a dry-run robot client), then the nodes the hub
builds, with their default loaders and no fakes, through ``main.NodeGroup``, which gives them
the multi-threaded executor and the supervised spin thread. A client node on its own context
talks to them over DDS on the private domain of ``ros_support.domain_id()``.

The nodes use rclpy's default context, as they do in the hub, so each test brings the default
context up and down; the client has a context of its own, as another process would.
"""

from __future__ import annotations

import array
import importlib
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

import pytest

import ros_support
from ros_support import Spinner, wait_until

ROOT = ros_support.ROOT
SRC = ROOT / "src"
ADAPTER_ROS = ROOT / "ros2_ws" / "src" / "emori" / "emori_adapter_ros"
OPEN = ROOT / "ros2_ws" / "src" / "open"
GRAPH_YAML = ros_support.HUB_PACKAGE / "config" / "graph.yaml"
BEHAVIOR_YAML = ROOT / "configs" / "behavior.yaml"

ANSWER_S = 20.0
#: Longer than every poll period of the facades (the slowest is 0.5 s), so a latched sample
#: is the only way a subscriber that comes after it can have it.
SETTLE_S = 1.5

FACADE_NAMES = {"session", "speech", "gesture", "motion", "gateway", "addon"}

#: What the hub's unit gives these tests, and the switches that must be at their defaults.
BASE_ENV = {
    "EMORI_ADDONS": "off",
    "EMORI_RECORD": "0",
    "EMORI_ROS_DEIDENTIFY": "1",
    "EMORI_ROS_SPEECH_TEXT": "0",
    "EMORI_ROS_ID_SALT": "facade-test-salt",
}
DEFAULT_OFF = (
    "EMORI_ROS_RAW_ENVELOPES", "EMORI_HUB_GESTURE_INJECT", "EMORI_HUB_MOTION_SUBMIT",
    "EMORI_TT_INJECT_HTTP", "EMORI_HUB_FACADES", "EMORI_HUB_GRAPH", "EMORI_HUB_ENV_JSON",
    "POKAMIND_ROBOT_TOKEN", "EMORI_ROS_MOTION_OWNER", "EMORI_SKILL_KEY", "EMORI_SKILL_TRANSPORT",
    "EMORI_BEHAVIOR_CONFIG",
)


# --- what the tests import ---------------------------------------------------------------


def _importable_from(module: str, directory: Path) -> Any:
    """``module``, from the image if it is installed there and from the checkout if not."""

    try:
        return importlib.import_module(module)
    except ImportError:
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    return ros_support.need_module(module)


@pytest.fixture
def mods(rclpy: Any, reachy_srv: Any) -> SimpleNamespace:
    """Every module the tests use, or a skip (a failure under ``EMORI_REQUIRE_ROS``)."""

    ros_support.hub_package_on_path()
    for name in ("emori_msgs.msg", "emori_msgs.srv", "emori_msgs.action", "std_msgs.msg",
                 "std_srvs.srv", "diagnostic_msgs.msg", "action_msgs.msg"):
        ros_support.need_module(name)
    _importable_from("emori_adapter", SRC)
    _importable_from("emori_adapter_ros.privacy", ADAPTER_ROS)
    return SimpleNamespace(
        rclpy=rclpy,
        hub=ros_support.need_module("emori_hub.main"),
        addon_node=ros_support.need_module("emori_hub.addon_node"),
        motion_node=ros_support.need_module("emori_hub.motion_node"),
        server=ros_support.need_module("emori_adapter.hostapp.server"),
        metrics_store=ros_support.need_module("emori_adapter.hostapp.metrics_store"),
        robot_token=ros_support.need_module("emori_adapter.robot_token"),
        client=ros_support.need_module("emori_adapter.robot.client"),
        websocket=ros_support.need_module("emori_adapter.sources.websocket"),
        Privacy=ros_support.need_module("emori_adapter_ros.privacy").Privacy,
        msg=ros_support.need_module("emori_msgs.msg"),
        srv=ros_support.need_module("emori_msgs.srv"),
        action=ros_support.need_module("emori_msgs.action"),
        std_msgs=ros_support.need_module("std_msgs.msg"),
        std_srvs=ros_support.need_module("std_srvs.srv"),
        diag=ros_support.need_module("diagnostic_msgs.msg"),
        goal=ros_support.need_module("action_msgs.msg").GoalStatus,
        qos=ros_support.need_module("rclpy.qos"),
    )


class _Stt:
    """A speech-to-text engine that answers at once; ``/emori/speech/recognize`` calls it."""

    available = True

    def transcribe(self, pcm: bytes, lang: str) -> str:
        return "hello there"

    def transcribe_grammar(self, pcm: bytes, phrases: list, lang: str) -> str:
        return phrases[0]

    def transcribe_grammar_conf(self, pcm: bytes, phrases: list, lang: str) -> tuple:
        return phrases[-1], 0.75


# --- the stack ---------------------------------------------------------------------------


def _recording(builders: Iterable[Callable[[Any], Any]], nodes: dict) -> tuple:
    """The same builders, each noting the node it returned under the node's own name."""

    def wrap(build: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def make(app: Any) -> Any:
            node = build(app)
            nodes[node.get_name()] = node
            return node

        make.__name__ = getattr(build, "__name__", "builder")
        return make

    return tuple(wrap(build) for build in builders)


class Stack:
    """The hub's nodes on their executor, and a client node on a context of its own."""

    def __init__(self, mods: SimpleNamespace, app: Any, ports: Any, bindings: Any,
                 group: Any, nodes: dict, context: Any) -> None:
        self.mods = mods
        self.app = app
        self.ports = ports
        self.bindings = bindings
        self.group = group
        self.nodes = nodes
        self.client_node = mods.rclpy.create_node("facade_test_client", context=context)
        from rclpy.executors import SingleThreadedExecutor

        executor = SingleThreadedExecutor(context=context)
        executor.add_node(self.client_node)
        self.spinner = Spinner(executor, "facade-test-client").start()
        self._clients: list[Any] = []
        self._closed = False
        self.group_stopped = False
        self.after_close: list[Callable[[], Any]] = []

    # --- talking to the nodes ------------------------------------------------------

    def client(self, srv_type: Any, name: str) -> Any:
        client = self.client_node.create_client(srv_type, name)
        self._clients.append(client)
        assert wait_until(client.service_is_ready, ANSWER_S), f"{name} never appeared"
        return client

    def call(self, client: Any, request: Any, timeout: float = ANSWER_S) -> Any:
        future = client.call_async(request)
        assert wait_until(future.done, timeout), f"no answer from {client.srv_name}"
        return future.result()

    def subscribe(self, msg_type: Any, topic: str, qos: Any) -> list:
        received: list[Any] = []
        self.client_node.create_subscription(msg_type, topic, received.append, qos)
        return received

    def qos(self, *, latched: bool = False, reliable: bool = True, depth: int = 10) -> Any:
        q = self.mods.qos
        return q.QoSProfile(
            depth=depth, history=q.HistoryPolicy.KEEP_LAST,
            reliability=(q.ReliabilityPolicy.RELIABLE if reliable
                         else q.ReliabilityPolicy.BEST_EFFORT),
            durability=(q.DurabilityPolicy.TRANSIENT_LOCAL if latched
                        else q.DurabilityPolicy.VOLATILE))

    # --- stopping -------------------------------------------------------------------

    def stop_group(self) -> None:
        """The hub's own teardown of its nodes: spin stops, the executor, then each node."""

        if not self.group_stopped:
            self.group_stopped = True
            self.group.stop()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        steps: list[Callable[[], Any]] = [
            self.spinner.stop, self.client_node.destroy_node, self.stop_group,
            self.bindings.try_shutdown, *self.after_close]
        for step in steps:
            try:
                step()
            except BaseException as error:  # noqa: BLE001 - every step still has to run
                errors.append(error)
        if errors:
            raise errors[0]


@pytest.fixture
def make_stack(mods: SimpleNamespace, ros_context: Any, monkeypatch: pytest.MonkeyPatch,
               tmp_path: Path):
    """``start(...)`` builds the app and the nodes and returns a :class:`Stack`; all are closed."""

    stacks: list[Stack] = []

    def start(*, env: Optional[dict] = None, builders: Optional[Iterable[Callable]] = None,
              graph_settings: Any = None, gesture: bool = True) -> Stack:
        # The suite's own isolation (tests/conftest.py): no state of this computer reaches
        # the app, and nothing it writes goes outside the test's directory.
        monkeypatch.setenv("ROS_DOMAIN_ID", str(ros_support.domain_id()))
        monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
        monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(tmp_path / "no-token"))
        monkeypatch.setenv("EMORI_DASH_DB", str(tmp_path / "dashboard.db"))
        for name in DEFAULT_OFF:
            monkeypatch.delenv(name, raising=False)
        for name, value in {**BASE_ENV, **(env or {})}.items():
            monkeypatch.setenv(name, value)

        server = mods.server
        mods.robot_token.reset_configured()
        monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", mods.robot_token.TokenLoad())
        monkeypatch.setattr(server, "ROBOT_TOKEN", None)
        monkeypatch.setattr(server, "DEMO", True)
        monkeypatch.setattr(server, "PARTNER", "test-partner")
        monkeypatch.setattr(server, "COURSE_ID", "emori-roleplay")
        mods.metrics_store.set_store(None)

        # What main() does, in main()'s order: rclpy first, the app, then the nodes.
        bindings = mods.hub.load_hub_bindings()
        bindings.init(mods.hub.ros_arguments(graph_settings, lambda: str(GRAPH_YAML)))
        ports = mods.addon_node.RosPorts()
        group = None
        try:
            app = server._build_app(overrides={
                "robot_client": mods.client.DryRunRobotClient(), "fiware_factory": None,
                "idle": False, "gesture": gesture, "tts": None, "stt": _Stt(), "voice": None,
                "ros": ports})
            nodes: dict[str, Any] = {}
            chosen = builders if builders is not None else (
                *mods.hub.FACADE_NODES, mods.addon_node.addon_builder(ports))
            group = mods.hub.NodeGroup("ros-facade", bindings, _recording(chosen, nodes))
            group.start(app)
            stack = Stack(mods, app, ports, bindings, group, nodes, ros_context)
        except BaseException:
            if group is not None:
                group.stop()
            bindings.try_shutdown()
            raise
        stack.after_close.extend([lambda: mods.metrics_store.set_store(None),
                                  mods.robot_token.reset_configured])
        stacks.append(stack)
        return stack

    yield start
    errors: list[BaseException] = []
    for stack in reversed(stacks):
        try:
            stack.close()
        except BaseException as error:  # noqa: BLE001 - the others still close
            errors.append(error)
    if errors:
        raise errors[0]


def _trouble(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the hub's loggers said that is a failure: an error, or a warning with a traceback."""

    return [f"{r.name}: {r.getMessage()}" for r in caplog.records
            if r.name.startswith("emori.hub") and (r.exc_info or r.levelno >= logging.ERROR)]


# --- the nodes come up -------------------------------------------------------------------


def test_the_six_facade_nodes_come_up_on_their_real_bindings(make_stack, caplog):
    stack = make_stack()
    assert stack.group.failed == [], "a builder raised with the real rclpy"
    assert set(stack.nodes) == FACADE_NAMES
    assert stack.group.spin is not None and stack.group.spin.death() == ""

    expected = {(name, "/emori") for name in FACADE_NAMES}
    assert wait_until(
        lambda: expected <= set(stack.client_node.get_node_names_and_namespaces()), ANSWER_S), (
        f"the client sees {stack.client_node.get_node_names_and_namespaces()}")

    # start_parameter_services=False: the nodes that go through facade_kit have none.
    for name in ("speech", "gesture", "motion", "gateway"):
        services = wait_until(lambda n=name: _services_of(stack, n), ANSWER_S)
        assert services, f"/emori/{name} offers no service at all"
        assert not [s for s, _ in services if s.endswith("/get_parameters")], (
            f"/emori/{name} started parameter services: {services}")
    assert _trouble(caplog) == []


def _services_of(stack: "Stack", name: str) -> list:
    try:
        return list(stack.client_node.get_service_names_and_types_by_node(name, "/emori"))
    except Exception:  # noqa: BLE001 - the node may not have reached the client's graph yet
        return []


# --- services ----------------------------------------------------------------------------


def test_the_session_services_answer_with_the_interfaces_fields(make_stack, mods):
    stack = make_stack()
    srv, driver = mods.srv, stack.app.state.driver

    command = stack.client(srv.SessionCommand, "/emori/session/command")
    for verb in ("end", "begin", "reprompt"):
        answer = stack.call(command, srv.SessionCommand.Request(verb=verb, reason="test"))
        assert (answer.accepted, answer.error_code) == (False, "no_session"), verb
    answer = stack.call(command, srv.SessionCommand.Request(verb="dance"))
    assert (answer.accepted, answer.error_code) == (False, "unknown_verb")
    assert driver.pop_cmd() is None, "a refused command left a verb in the driver's slot"

    start = stack.client(srv.StartSession, "/emori/session/start")
    answer = stack.call(start, srv.StartSession.Request(menu_text="anything", badge_payload=""))
    assert (answer.accepted, answer.session_id, answer.topic, answer.error_code) == (
        False, "", "", "busy"), "no landing page has been seen, so the kiosk is not free"
    answer = stack.call(start, srv.StartSession.Request(menu_text="  "))
    assert (answer.accepted, answer.error_code) == (False, "no_match")
    assert not driver.tracker.active and not stack.app.state.start_slot.waiting()
    assert driver.pop_cmd() is None

    menu = stack.client(srv.GetVoiceMenu, "/emori/session/voice_menu")
    answer = stack.call(menu, srv.GetVoiceMenu.Request(lang=""))
    prompt, grammar, keys, names = stack.nodes["session"].session_facade.voice_menu("")
    assert (answer.prompt, list(answer.grammar), list(answer.option_keys),
            list(answer.option_names)) == (prompt, grammar, keys, names)
    assert len(answer.option_keys) == len(answer.option_names)
    answer = stack.call(menu, srv.GetVoiceMenu.Request(lang="zz"))
    assert (answer.prompt, list(answer.grammar), list(answer.option_keys),
            list(answer.option_names)) == ("", [], [], []), "a language not on offer has no menu"

    overrides = stack.client(srv.SetTtOverrides, "/emori/session/set_tt_overrides")
    answer = stack.call(overrides, srv.SetTtOverrides.Request(patch_json="{}"))
    assert (answer.ok, answer.rev, answer.error_code) == (False, 0, "tt_inject_disabled")


def test_the_speech_services_answer_with_the_interfaces_fields(make_stack, mods):
    stack = make_stack()
    srv = mods.srv
    recognize = stack.client(srv.Recognize, "/emori/speech/recognize")
    pcm = array.array("B", bytes(6400))

    def ask(request: Any) -> Any:
        # An engine that has answered is a thread that is about to end; a request that comes
        # in that instant is told "busy", and asking again is what a caller does.
        deadline = time.monotonic() + 3.0
        while True:
            answer = stack.call(recognize, request)
            if answer.error_code != "busy" or time.monotonic() > deadline:
                return answer

    answer = ask(srv.Recognize.Request(pcm_s16le_16k=pcm, lang="en", grammar=[]))
    assert (answer.ok, answer.text, answer.conf, answer.error_code) == (
        True, "hello there", -1.0, ""), "open mode carries conf -1.0"
    answer = ask(srv.Recognize.Request(pcm_s16le_16k=pcm, lang="en", grammar=["yes", "no"]))
    assert (answer.ok, answer.text, answer.conf, answer.error_code) == (True, "no", 0.75, "")
    answer = ask(srv.Recognize.Request(pcm_s16le_16k=array.array("B", bytes(100)), lang="en"))
    assert (answer.ok, answer.text, answer.conf, answer.error_code) == (True, "", -1.0, ""), (
        "under 0.1 s of audio is an empty text, not an engine call")

    language = stack.client(srv.SetLanguage, "/emori/speech/set_language")
    current = stack.app.state.lang_state.current()
    answer = stack.call(language, srv.SetLanguage.Request(lang="zz"))
    assert (answer.ok, answer.lang, answer.error_code) == (False, current, "unsupported_lang")
    answer = stack.call(language, srv.SetLanguage.Request(lang=current))
    assert (answer.ok, answer.lang, answer.error_code) == (True, current, "")
    assert stack.app.state.lang_state.current() == current


def test_the_gesture_motion_and_gateway_services_answer_with_the_interfaces_fields(
        make_stack, mods):
    stack = make_stack()
    srv, msg, std = mods.srv, mods.msg, mods.std_srvs

    reset = stack.client(std.Trigger, "/emori/gesture/reset_sensing")
    answer = stack.call(reset, std.Trigger.Request())
    assert (answer.success, answer.message) == (True, "")
    inject = stack.client(std.SetBool, "/emori/gesture/inject_turn")
    answer = stack.call(inject, std.SetBool.Request(data=True))
    assert (answer.success, answer.message) == (False, "inject_disabled")

    batch = stack.client(srv.SubmitMotionBatch, "/emori/motion/submit_batch")
    request = srv.SubmitMotionBatch.Request(
        intents=[msg.MotionIntent(kind="goto", has_head=True, yaw_deg=10.0, duration=1.0)],
        drop_if_busy=True)
    answer = stack.call(batch, request)
    assert (answer.accepted, answer.error_code) == (False, "disabled")
    assert stack.nodes["motion"].facade.refused == {"disabled": 1}
    assert stack.nodes["motion"].facade.accepted == 0

    status = stack.client(srv.GetHubStatus, "/emori/gateway/status")
    for _ in range(2):
        answer = stack.call(status, srv.GetHubStatus.Request())
    assert (answer.runtime, answer.demo, answer.robot) == ("http", True, True)
    body = json.loads(answer.status_json)
    assert set(body) == {"healthz", "status"}
    assert body["healthz"]["status"] == "ok" and body["healthz"]["partner"] == "test-partner"
    assert "pokamind_credential" not in body["healthz"], "the credential left the process"
    assert not _secret_keys(body), _secret_keys(body)
    assert stack.nodes["gateway"].facade.status_calls == 2
    assert stack.nodes["gateway"].facade.route_errors == 0, "a status route is missing"


def _secret_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}/{key}"
            if any(word in str(key).lower() for word in ("token", "secret", "password",
                                                         "credential", "fingerprint", "salt")):
                found.append(where)
            found.extend(_secret_keys(item, where))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_keys(item, f"{path}[{index}]"))
    return found


# --- Say ---------------------------------------------------------------------------------


def _say(stack: Stack, client: Any, text: str, lang: str = "en") -> SimpleNamespace:
    from emori_msgs.action import Say

    stages: list[str] = []
    goal = Say.Goal()
    goal.text, goal.lang = text, lang
    sent = client.send_goal_async(goal, feedback_callback=lambda fb: stages.append(
        fb.feedback.stage))
    assert wait_until(sent.done, ANSWER_S), "the goal was never answered"
    handle = sent.result()
    outcome = SimpleNamespace(accepted=handle.accepted, stages=stages)
    if handle.accepted:
        done = handle.get_result_async()
        assert wait_until(done.done, ANSWER_S), "an accepted goal never produced a result"
        wrapped = done.result()
        outcome.status = wrapped.status
        outcome.played = wrapped.result.played
        outcome.error_code = wrapped.result.error_code
    return outcome


def test_say_over_a_real_action_client_is_accepted_and_settles_with_a_result(
        make_stack, mods, caplog):
    from rclpy.action import ActionClient

    stack = make_stack()
    client = ActionClient(stack.client_node, mods.action.Say, "/emori/speech/say")
    try:
        assert wait_until(client.server_is_ready, ANSWER_S), "the Say action never appeared"

        # No landing page has been seen, so the gate is closed: the goal is accepted, runs, and
        # ends with the refusal as its result. That is the whole of the server's path short of
        # the speaker.
        refused = _say(stack, client, "Hello there")
        assert refused.accepted
        assert (refused.played, refused.error_code) == (False, "kiosk_busy")
        assert refused.status == mods.goal.STATUS_ABORTED
        assert set(refused.stages) <= {"synth", "play"}, refused.stages

        too_long = _say(stack, client, "x" * 201)
        assert (too_long.accepted, too_long.played, too_long.error_code) == (
            True, False, "too_long")
        empty = _say(stack, client, "   ")
        assert (empty.accepted, empty.played, empty.error_code) == (True, False, "play_failed")
        assert empty.status == mods.goal.STATUS_ABORTED
    finally:
        client.destroy()

    counters = stack.nodes["speech"].emori_facade.counters.snapshot()
    assert counters.get("say_requests") == 3, counters
    assert counters.get("say_refused_kiosk_busy") == 1, counters
    for fault in ("say_goal_errors", "say_feedback_errors", "say_errors"):
        assert not counters.get(fault), f"{fault}: {counters}"
    assert _trouble(caplog) == []


# --- topics ------------------------------------------------------------------------------


def test_latched_topics_reach_a_subscriber_that_comes_late(make_stack, mods, caplog):
    stack = make_stack()
    msg = mods.msg
    # The player is busy for the first second after the app is built; a state that changes
    # is published again, and what is wanted here is a state that has stopped changing.
    assert wait_until(lambda: not stack.app.state.driver.player.busy, ANSWER_S)
    time.sleep(SETTLE_S)
    motion, addon = stack.nodes["motion"].facade, stack.nodes["addon"].facade
    published = (motion.published, addon.published)
    assert published[0] >= 1 and published[1] == 1

    latched = stack.qos(latched=True, depth=1)
    state = stack.subscribe(msg.SessionState, "/emori/session/state", latched)
    language = stack.subscribe(msg.KioskLanguage, "/emori/language", latched)
    mic = stack.subscribe(mods.std_msgs.String, "/emori/audio/mic_profile", latched)
    gesture = stack.subscribe(msg.GestureState, "/emori/gesture/state", latched)
    player = stack.subscribe(msg.PlayerState, "/emori/motion/player", latched)
    status = stack.subscribe(msg.AddonStatus, "/emori/addon/status", latched)
    for name, received in (("session state", state), ("language", language),
                           ("mic profile", mic), ("gesture state", gesture),
                           ("player state", player), ("add-on status", status)):
        assert wait_until(lambda r=received: r, ANSWER_S), f"no latched {name} for a late subscriber"

    first = state[0]
    assert first.phase == msg.SessionState.PHASE_NO_SESSION and first.seq >= 1
    assert (first.session_id, first.person_id, first.topic, first.lang) == ("", "", "", "")
    assert first.last_avg_engagement == -1.0 and first.header.stamp.sec > 0

    lang_state = stack.app.state.lang_state
    assert language[0].current == lang_state.current()
    assert list(language[0].allowed) == list(lang_state.allowed)
    assert mic[0].data == (stack.app.state.glue.mic_profile.profile or "")

    assert gesture[0].mode in ("push_turn", "back_drive")
    assert gesture[0].moving_reason == ("player.busy" if gesture[0].robot_moving else "")
    assert gesture[0].selection_reserved is False

    reader = mods.motion_node.PlayerReader(stack.app.state.driver).read()
    assert (player[0].busy, player[0].queue_depth, player[0].legacy_motion_enabled,
            player[0].generation, player[0].motion_owner) == (
        False, 0, reader.legacy_motion_enabled, reader.generation, reader.motion_owner)
    assert motion.reader.unreadable == 0, "a value of the player's state could not be read"
    assert (motion.published, addon.published) == published, (
        "something was published after the subscribers came: the samples were not the latched ones")

    assert (status[0].installed, status[0].name, status[0].busy,
            status[0].selection_reserved, status[0].mode) == (False, "", False, False, "")

    # publish_status() asks for a publication on the next poll, which a live subscriber gets.
    stack.ports.publish_status()
    assert wait_until(lambda: len(status) >= 2, ANSWER_S), "a requested status was not published"
    assert _trouble(caplog) == []


def test_a_sentence_reaches_the_plan_events_topic_in_the_real_message(make_stack, mods, caplog):
    # No gesture sensor: on a push-turn robot the head is an input device while it is idle, and
    # a sentence would be skipped, which is one message and not the three this test reads.
    stack = make_stack(gesture=False)
    driver = stack.app.state.driver
    assert not driver.head_reserved()
    assert wait_until(lambda: not driver.player.busy, ANSWER_S)
    received = stack.subscribe(mods.msg.MotionPlanEvent, "/emori/motion/plan_events",
                               stack.qos(depth=10))
    time.sleep(SETTLE_S)

    driver.on_utterance({"text": "One plain sentence about the plan.", "seq": 41,
                         "cue_category": "tension"})
    assert wait_until(lambda: any(m.ended for m in received), ANSWER_S), (
        "the plan's end never reached the topic")

    planned, started, ended = received[0], received[1], received[-1]
    assert len(received) == 3
    for message in received:
        assert message.plan_id == planned.plan_id != ""
        assert message.seq == 41
        assert message.family == message.family_authored == "tension"
        assert message.family_source and message.slug
        assert message.header.stamp.sec > 0
    assert (planned.started, planned.ended, planned.first_apply_delay_s) == (False, False, -1.0)
    assert (started.started, started.ended) == (True, False)
    assert started.first_apply_delay_s >= 0.0
    assert (ended.started, ended.ended, ended.outcome, ended.skip_reason) == (
        True, True, "finished", "")

    stats = stack.nodes["motion"].facade.stats()
    assert stats["plan_events_dropped"] == 0 and stats["plan_events_failed"] == 0
    assert _trouble(caplog) == []


def test_the_voice_topic_is_published_to_a_subscriber(make_stack, mods):
    stack = make_stack()
    voice = stack.subscribe(mods.msg.VoiceState, "/emori/speech/voice",
                            stack.qos(reliable=False, depth=1))
    assert wait_until(lambda: voice, ANSWER_S), "nothing on /emori/speech/voice"
    message = voice[0]
    assert (message.enabled, message.available, message.active) == (False, False, False)
    assert (message.level, message.rms, message.noise_rms) == (0.0, 0.0, 0.0)
    assert (message.onsets, message.frames, message.silent_for_s) == (0, 0, -1.0)
    assert message.header.stamp.sec > 0


# --- the session's events ----------------------------------------------------------------


def _envelope(kind: str, **fields: Any) -> dict:
    return {"type": kind, "session_id": "s-1234", "person_id": "student-7", "lang": "en",
            **fields}


def test_a_relayed_envelope_reaches_the_events_topic_as_a_typed_gapless_event(
        make_stack, mods, caplog):
    stack = make_stack(env={"EMORI_ROS_SPEECH_TEXT": "1"})
    privacy = mods.Privacy.from_env()
    assert privacy.speech_text and privacy.deidentify
    relay = stack.app.state.hub
    events = stack.subscribe(mods.msg.SessionEvent, "/emori/session/events",
                             stack.qos(depth=256))
    assert wait_until(
        lambda: stack.nodes["session"].count_subscribers("/emori/session/events") >= 1,
        ANSWER_S), "the events publisher never matched the subscriber"

    # The first envelopes may go before the writer and the reader have matched, and a
    # volatile topic does not keep them: send probes until one arrives, then let the rest of
    # them land. What comes after is counted from there.
    deadline = time.monotonic() + ANSWER_S
    while not events and time.monotonic() < deadline:
        relay.publish(json.dumps(_envelope("pokamind_probe")))
        time.sleep(0.2)
    assert events, "no probe reached the topic"
    time.sleep(1.0)
    mark, last = len(events), events[-1].seq

    stamp = "2026-01-02T03:04:05Z"
    epoch = int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp())
    relay.publish(json.dumps(_envelope(
        "pokamind_ai_utterance", text="[nod| slow] Let us start with how your week went.",
        seq=3, source="turn", timestamp=stamp, cue_category="warm", cue_family="affirm",
        cue_family_source="model", cue_slug="nod-slow", cue_recorded="yes", cue_budget_s=2.5)))
    relay.publish(json.dumps(_envelope("pokamind_session_completed",
                                       results={"summary": "Went well."}, status="completed")))
    relay.publish("{this is not json")
    assert stack.nodes["session"].fanout.note(
        "hub_session_finalized", session_id="s-1234", person_id="student-7", lang="en",
        status="completed")
    assert wait_until(lambda: len(events) >= mark + 4, ANSWER_S), (
        "events after the probes: " + repr([e.type for e in events[mark:]]))
    tail = events[mark:mark + 4]

    assert [e.seq for e in tail] == list(range(last + 1, last + 5)), "a gap or a repeat in seq"
    person, session = privacy.apply_ids("student-7", "s-1234")
    assert person != "student-7" and session != "s-1234"
    utterance, completed, unreadable, finalized = tail

    assert utterance.type == "pokamind_ai_utterance"
    assert (utterance.person_id, utterance.session_id, utterance.lang) == (person, session, "en")
    assert utterance.text == "Let us start with how your week went."
    assert (utterance.cue_category, utterance.cue_family, utterance.cue_family_source,
            utterance.cue_slug, utterance.cue_recorded, utterance.source) == (
        "warm", "affirm", "model", "nod-slow", "yes", "turn")
    assert utterance.cue_budget_s == 2.5 and utterance.est_duration > 0.0
    assert (utterance.source_time.sec, utterance.source_time.nanosec) == (epoch, 0)
    assert utterance.header.stamp.sec > 0
    assert (utterance.has_results, utterance.status, utterance.payload_json) == (False, "", "")

    assert completed.type == "pokamind_session_completed"
    assert (completed.has_results, completed.status) == (True, "completed")
    assert unreadable.type == "hub_envelope_unreadable" and unreadable.text == ""
    assert finalized.type == "hub_session_finalized"
    assert (finalized.person_id, finalized.session_id, finalized.status) == (
        person, session, "completed")

    stats = stack.nodes["session"].fanout.stats()
    assert stats["running"] and stats["dropped"] == 0 and stats["publish_errors"] == 0
    assert stats["mapping_errors"] == 0, stats
    assert _trouble(caplog) == []


# --- /diagnostics and the gateway --------------------------------------------------------


def test_diagnostics_carries_the_entries_the_gateway_names(make_stack, mods, caplog):
    stack = make_stack()
    arrays = stack.subscribe(mods.diag.DiagnosticArray, "/diagnostics", stack.qos(depth=10))
    assert wait_until(lambda: arrays, ANSWER_S), "no /diagnostics in 20 s"
    time.sleep(0.2)
    latest = arrays[-1]

    registered = set(stack.app.state.diagnostic_sources)
    assert registered == {"gateway", "motion", "addon"}
    expected = {"emori_hub/" + name for name in ("robot_link", "session_fanout", *registered)}
    assert {status.name for status in latest.status} == expected
    assert latest.header.stamp.sec > 0

    by_name = {status.name: status for status in latest.status}
    for status in latest.status:
        assert status.hardware_id == "emori-hub"
        assert _level(status) < 2, f"{status.name}: {status.message}"
        assert status.message
    assert _level(by_name["emori_hub/session_fanout"]) == 0
    assert _level(by_name["emori_hub/robot_link"]) == 0
    values = {pair.key: pair.value for pair in by_name["emori_hub/session_fanout"].values}
    assert values["running"] == "True" and values["capacity"] == "256"
    link = {pair.key: pair.value for pair in by_name["emori_hub/robot_link"].values}
    assert link == {"runtime": "http"}
    motion = {pair.key: pair.value for pair in by_name["emori_hub/motion"].values}
    assert motion["submit_enabled"] == "False" and motion["callback_errors"] == "0"
    assert motion["plan_events_dropped"] == "0" and motion["plan_events_capacity"] == "256"
    assert _trouble(caplog) == []


def _level(status: Any) -> int:
    level = status.level
    return level[0] if isinstance(level, (bytes, bytearray)) else int(level)


# --- the gesture tee ---------------------------------------------------------------------


def test_a_turn_handed_in_is_published_and_still_reaches_the_ticker_hook(make_stack, mods):
    stack = make_stack(env={"EMORI_HUB_GESTURE_INJECT": "1"},
                       builders=(mods.hub.build_gesture_node,))
    assert stack.group.failed == []
    ticker = stack.app.state.gesture_ticker
    assert getattr(ticker.on_direction, "emori_tee", False), "the hook was not wrapped"

    kinds = mods.msg.GestureEvent
    events = stack.subscribe(kinds, "/emori/gesture/events", stack.qos(depth=64))
    inject = stack.client(mods.std_srvs.SetBool, "/emori/gesture/inject_turn")
    assert wait_until(
        lambda: stack.nodes["gesture"].count_subscribers("/emori/gesture/events") >= 1,
        ANSWER_S)

    deadline = time.monotonic() + ANSWER_S
    while not events and time.monotonic() < deadline:
        answer = stack.call(inject, mods.std_srvs.SetBool.Request(data=True))
        assert (answer.success, answer.message) == (True, "")
        time.sleep(0.3)
    assert events, "no head-turn event reached the topic"
    assert all((e.kind, e.direction) == (kinds.KIND_HEAD_TURN, "left") for e in events)

    seen = len(events)
    stack.call(inject, mods.std_srvs.SetBool.Request(data=False))
    assert wait_until(lambda: len(events) > seen, ANSWER_S)
    assert (events[-1].kind, events[-1].direction) == (kinds.KIND_HEAD_TURN, "right")
    counters = stack.nodes["gesture"].emori_facade.counters.snapshot()
    assert counters["injected_turns"] >= 2 and not counters.get("turn_publish_errors")


# --- stopping ----------------------------------------------------------------------------


def test_destroying_the_nodes_the_way_the_hub_does_leaves_no_exception_and_the_tee_off(
        make_stack, mods, caplog):
    stack = make_stack()
    app, extension = stack.app, stack.app.state.extension
    ticker = app.state.gesture_ticker
    fanout = stack.nodes["session"].fanout
    assert getattr(ticker.on_direction, "emori_tee", False)
    assert app.state.hub.tap is not None and fanout.stats()["running"]
    # Traffic while it stops: the state topics are live and an envelope is in the queue.
    app.state.hub.publish(json.dumps(_envelope("pokamind_ai_utterance", text="One.")))
    time.sleep(0.5)
    names = {t.name for t in threading.enumerate()}
    assert {"ros-fanout", "ros-speech-state"} <= names, names

    stack.stop_group()

    assert not getattr(ticker.on_direction, "emori_tee", False), "the tee is still installed"
    assert ticker.on_direction == extension.on_head_turn, "the hook is not the one it was"
    assert app.state.hub.tap is None, "the relay still has the fan-out's tap"
    assert fanout.stats()["running"] is False
    mine = {"ros-fanout", "ros-speech-voice", "ros-speech-state"}
    assert wait_until(lambda: not ({t.name for t in threading.enumerate()} & mine), 5.0), (
        "a thread of the nodes outlived them: "
        f"{ {t.name for t in threading.enumerate()} & mine }")
    assert stack.group.spin.death() == "", "the spin ended on its own, not because it was asked"
    assert wait_until(
        lambda: not {n for n, ns in stack.client_node.get_node_names_and_namespaces()
                     if ns == "/emori"}, ANSWER_S), "nodes are still on the graph"
    assert mods.rclpy.ok(), "stopping the nodes took the context with it"
    assert _trouble(caplog) == []


# --- the graph's four nodes --------------------------------------------------------------


def test_the_graph_nodes_come_up_hosted_and_a_fed_envelope_reaches_the_adapters_topics(
        make_stack, mods, caplog):
    ros_support.need_module("hri_msgs.msg")
    _importable_from("hri_context_analyzer", OPEN / "hri_context_analyzer")
    _importable_from("hri_engagement_behaviors", OPEN / "hri_engagement_behaviors")
    _importable_from("emori_skill_facade", OPEN / "emori_skill_facade")
    from hri_msgs.msg import EngagementLevel, IdsList

    # What main() derives from the unit's environment when EMORI_HUB_GRAPH=1: the privacy
    # defaults, the skill transport, and the engagement node's tuning file.
    env = {"EMORI_HUB_GRAPH": "1", "EMORI_ROS_BEHAVIOR_CONFIG_HOST": str(BEHAVIOR_YAML),
           "EMORI_ROS_ID_SALT": "facade-test-salt"}
    settings = mods.hub.load_env_snapshot(env)
    assert settings is not None and settings.behavior_yaml == str(BEHAVIOR_YAML)
    assert env["EMORI_SKILL_TRANSPORT"] == "ros_native"

    stack = make_stack(env=env, builders=mods.hub.GRAPH_NODES, graph_settings=settings)
    assert stack.group.failed == []
    assert set(stack.nodes) == {"emori_adapter", "hri_context_analyzer",
                                "hri_engagement_behaviors", "skill_facade"}
    adapter = stack.nodes["emori_adapter"]
    assert adapter.source_kind == "hub", "config/graph.yaml did not reach the adapter node"
    assert adapter.get_parameter("person_id").value == "learner_01"
    assert not hasattr(adapter, "_reader") and not hasattr(adapter, "_queue")
    behaviours = stack.nodes["hri_engagement_behaviors"]
    assert behaviours.get_parameter("behavior_yaml").value == str(BEHAVIOR_YAML)
    assert stack.nodes["skill_facade"].transport_name == "ros_native"
    assert stack.nodes["skill_facade"].motion_owner != "ros", "the graph must not own motion"

    expected = {(name, "/") for name in stack.nodes}
    assert wait_until(
        lambda: expected <= set(stack.client_node.get_node_names_and_namespaces()), ANSWER_S)

    base = "/humans/persons/learner_01"
    latched = stack.qos(latched=True, depth=1)
    tracked = stack.subscribe(IdsList, "/humans/persons/tracked", latched)
    anonymous = stack.subscribe(mods.std_msgs.Bool, f"{base}/anonymous", latched)
    contexts = stack.subscribe(mods.msg.Context, f"{base}/context", stack.qos())
    levels = stack.subscribe(EngagementLevel, f"{base}/engagement_status", stack.qos())
    cues = stack.subscribe(mods.msg.SpeechCue, f"{base}/speech_cue", stack.qos())
    assert wait_until(lambda: tracked and anonymous, ANSWER_S), "the latched topics are silent"
    assert list(tracked[0].ids) == ["learner_01"] and anonymous[0].data is False

    ws, relay = mods.websocket, stack.app.state.hub
    privacy = mods.Privacy.from_env()
    person, session = privacy.apply_ids("student-7", "s-1")
    sent = {"analysis": 0, "utterance": 0}
    deadline = time.monotonic() + ANSWER_S
    while not (contexts and levels and cues) and time.monotonic() < deadline:
        sent["analysis"] += 1
        sent["utterance"] += 1
        relay.publish(json.dumps({
            "type": ws.ANALYSIS_EVENT, "session_id": "s-1", "person_id": "student-7",
            "seq": sent["analysis"], "timestamp": "2026-01-02T03:04:05Z",
            "body_language": {"posture": "leaning_forward", "engagement_score": 70},
            "body_language_metrics": {"engagement": 70, "confidence": 60}}))
        relay.publish(json.dumps({
            "type": ws.AI_UTTERANCE_EVENT, "session_id": "s-1", "person_id": "student-7",
            "seq": sent["utterance"], "text": "Tell me how your week went.",
            "cue_category": "warm", "cue_body": "kindly", "lang": "en", "source": "turn",
            "timestamp": "2026-01-02T03:04:05Z"}))
        time.sleep(0.2)
    assert contexts, "no emori_msgs/Context from a fed analysis envelope"
    assert levels, "no engagement_status from a fed analysis envelope"
    assert cues, "no emori_msgs/SpeechCue from a fed utterance"

    for context in contexts:
        assert (context.person_id, context.session_id) == (person, session)
        assert 1 <= context.seq <= sent["analysis"]
    for cue in cues:
        assert (cue.person_id, cue.session_id) == (person, session)
        assert (cue.text, cue.cue_body) == ("", ""), "the speech gate is closed by default"
        assert (cue.cue_category, cue.lang, cue.source) == ("warm", "en", "turn")
        assert cue.est_duration > 0.0 and not cue.ended

    stats = stack.app.state.fanout.stats()
    assert stats["running"] and stats["feed_errors"] == 0 and stats["publish_errors"] == 0
    assert stats["fed"] >= len(contexts) + len(cues)

    stack.stop_group()
    assert stack.app.state.hub.tap is None and stack.app.state.fanout.stats()["running"] is False
    assert _trouble(caplog) == []
