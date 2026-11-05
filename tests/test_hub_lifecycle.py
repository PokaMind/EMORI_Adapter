"""The hub process: what starts when, what a stop leaves behind, what a dead spin does.

Two benches. In this process, ROS and the server are fakes, so every step can be watched and
any step can fail. In a child process, uvicorn is real and so are its signals, because the
fault that matters most here is one a fake would not have: uvicorn raises the signal again
after its graceful stop, and with the wrong handler in place that ends the process in the
middle of its teardown. A child that dies that way takes only itself with it, and the test
sees that the last lines never arrived.

    python tests/test_hub_lifecycle.py <scenario>

runs one child scenario by hand. No ROS is needed for any of this.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
import dataclasses
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HUB = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from emori_hub import facade_kit  # noqa: E402
from emori_hub import main as hub  # noqa: E402
from emori_hub import robot_link  # noqa: E402
from emori_hub.spin import SupervisedSpin  # noqa: E402

_TERM = signal.SIGTERM


@pytest.fixture(autouse=True)
def _the_processes_switch_interval_is_put_back():
    before = sys.getswitchinterval()
    yield
    sys.setswitchinterval(before)


#: Killing a spin's thread is the point of these tests, and pytest reports a thread that dies.
_A_THREAD_DIES = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


def _wait_until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    return predicate()


def _hub_threads() -> list[str]:
    names = ("ros-link", "ros-facade", "ros-graph", "hub-watch", "hub-stop-", "robot-link-replay")
    return sorted(thread.name for thread in threading.enumerate()
                  if thread.is_alive() and thread.name.startswith(names))


# --- fakes -----------------------------------------------------------------------------


class _ContextGone(Exception):
    """Stands for ``ExternalShutdownException``."""


class _Executor:
    """As an rclpy one, a callback's exception comes back out of the call that spins it."""

    def __init__(self, events: list[str], name: str, num_threads: int | None = None) -> None:
        self.events = events
        self.name = name
        self.num_threads = num_threads
        self.nodes: list[Any] = []
        self.spins = 0
        self.fail_next: BaseException | None = None
        self.fail_always: Exception | None = None
        self.on_shutdown: Callable[[], Any] = lambda: None

    def add_node(self, node: Any) -> None:
        self.nodes.append(node)

    def spin_once(self, timeout_sec: float = 0.0) -> None:
        self.spins += 1
        if self.fail_always is not None:
            raise self.fail_always
        fail, self.fail_next = self.fail_next, None
        if fail is not None:
            raise fail
        time.sleep(0.002)

    def shutdown(self) -> None:
        self.events.append(f"{self.name} executor shutdown")
        self.on_shutdown()


class _LinkNode:
    def __init__(self, events: list[str], name: str, *, namespace: str = "") -> None:
        self.events = events
        self.fullname = f"{namespace}/{name}"
        events.append(f"node {self.fullname}")

    def create_client(self, _type: Any, _name: str, *, callback_group: Any = None) -> Any:
        return SimpleNamespace(service_is_ready=lambda: False)

    def create_subscription(self, *_args: Any, **_kwargs: Any) -> Any:
        return object()

    def destroy_node(self) -> None:
        self.events.append(f"destroy {self.fullname}")


class _GroupNode:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.on_destroy: Callable[[], Any] = lambda: None
        events.append(f"node {name}")

    def destroy_node(self) -> None:
        self.events.append(f"destroy {self.name}")
        self.on_destroy()


class _Ros:
    """What :func:`load_hub_bindings` returns, with a record of each call."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.context_ok = True
        self.init_args: Any = None
        self.link_node_fails = False
        self.init_fails = False
        self.link_executor = _Executor(events, "ros-link")
        self.group_executors: list[_Executor] = []
        services = {name: SimpleNamespace(Request=SimpleNamespace)
                    for name in robot_link.SERVICE_NAMES}
        self.bindings = hub.HubBindings(
            init=self._init,
            try_shutdown=self._shutdown,
            ok=lambda: self.context_ok,
            fatal=(_ContextGone,),
            multi_threaded_executor=self._group_executor,
            link=robot_link.LinkBindings(
                create_node=self._create_link_node, executor_cls=lambda: self.link_executor,
                group_cls=object, state_qos="state-qos", ok=lambda: self.context_ok,
                fatal=(_ContextGone,), Header=object, services=services),
        )

    def _init(self, args: Any) -> None:
        if self.init_fails:
            self.events.append("boom")
            raise RuntimeError("rclpy.init failed")
        self.init_args = args
        self.events.append("rclpy init")

    def _shutdown(self) -> None:
        self.context_ok = False
        self.events.append("rclpy shutdown")

    def _create_link_node(self, name: str, *, namespace: str = "") -> _LinkNode:
        if self.link_node_fails:
            self.events.append("boom")
            raise RuntimeError("no node")
        return _LinkNode(self.events, name, namespace=namespace)

    def _group_executor(self, *, num_threads: int) -> _Executor:
        executor = _Executor(self.events, f"group{len(self.group_executors)}", num_threads)
        self.group_executors.append(executor)
        return executor


class _Wrapper:
    """A harness's wrapper: records when it is closed, and closes what it wraps."""

    def __init__(self, inner: Any, events: list[str]) -> None:
        self.inner = inner
        self.events = events

    def close(self) -> None:
        self.events.append("client close")
        self.inner.close()


class _Host:
    """The server module as far as this process uses it."""

    REACHY_BASE_URL = "http://127.0.0.1:9"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.overrides: dict[str, Any] = {}
        self.serve_kwargs: dict[str, Any] = {}
        self.app = object()
        self.during_build: Callable[[], Any] = lambda: None
        self.during_serve: Callable[[], Any] = lambda: None

    def _build_app(self, overrides: dict[str, Any]) -> Any:
        self.events.append("build app")
        self.overrides = overrides
        self.during_build()
        return self.app

    def serve(self, app: Any, **kwargs: Any) -> None:
        assert app is self.app
        self.events.append("serve")
        self.serve_kwargs = kwargs
        self.during_serve()
        self.events.append("serve returned")


class _MiniUvicorn:
    """uvicorn's handling of signals and nothing else of it: takes them, stops, raises again."""

    def __init__(self, host: _Host) -> None:
        self.host = host
        self.holding = threading.Event()
        self.captured: list[int] = []

    def _handle(self, signum: int, _frame: Any) -> None:
        self.captured.append(signum)

    def __call__(self) -> None:
        originals = {signum: signal.signal(signum, self._handle)
                     for signum in (signal.SIGTERM, signal.SIGINT)}
        self.holding.set()
        try:
            deadline = time.monotonic() + 20.0
            while not self.captured and time.monotonic() < deadline:
                time.sleep(0.005)
            self.host.events.append("lifespan stopped")
        finally:
            for signum, handler in originals.items():
                signal.signal(signum, handler)
        for signum in reversed(self.captured):
            signal.raise_signal(signum)


class _Bench:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.ros = _Ros(self.events)
        self.host = _Host(self.events)
        self.exits: list[int] = []
        self.life = hub.Lifecycle(exit_now=self.exits.append, poll_s=0.01)

    def node(self, name: str) -> Callable[[Any], Any]:
        """A builder for a node on a group, which also records the app it was given."""

        def build(app: Any) -> _GroupNode:
            assert app is self.host.app
            return _GroupNode(self.events, name)

        return build

    def run(self, **kwargs: Any) -> int:
        kwargs.setdefault("environ", {})
        return hub.run_hub(self.ros.bindings, self.host, lifecycle=self.life, **kwargs)

    def wrap(self, inner: Any) -> _Wrapper:
        self.wrapped = _Wrapper(inner, self.events)
        return self.wrapped

    def tail_after(self, event: str) -> list[str]:
        return self.events[self.events.index(event) + 1:]


@pytest.fixture()
def bench():
    """A bench whose signals land on a harmless handler if the code under test forgets."""

    forgot: list[int] = []
    originals = {signum: signal.signal(signum, lambda number, _frame: forgot.append(number))
                 for signum in (signal.SIGTERM, signal.SIGINT)}
    made = _Bench()
    try:
        yield made
    finally:
        made.life.cancel_deadlines()
        for signum, handler in originals.items():
            signal.signal(signum, handler)
    assert forgot == [], "a signal reached the handler the process started with"


# --- the environment -------------------------------------------------------------------


def _snapshot(tmp_path: Path, content: Any) -> dict[str, str]:
    path = tmp_path / "env.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8")
    return {hub.ENV_JSON_VAR: str(path)}


def test_the_snapshot_reaches_the_environment_and_wins_over_what_is_there(tmp_path):
    environ = _snapshot(tmp_path, {"EMORI_LANG": "ca", "POKAMIND_BASE": "https://example.test"})
    environ["EMORI_LANG"] = "en"
    hub.load_env_snapshot(environ)
    assert environ["EMORI_LANG"] == "ca"
    assert environ["POKAMIND_BASE"] == "https://example.test"


@pytest.mark.parametrize("name", [
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL", "INVOCATION_ID",
    "JOURNAL_STREAM", "NOTIFY_SOCKET", "SYSTEMD_EXEC_PID", "MANAGERPID", "XDG_RUNTIME_DIR",
    "RUNTIME_DIRECTORY", "PYTHONPATH", "ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "RMW_IMPLEMENTATION",
    "FASTRTPS_DEFAULT_PROFILES_FILE", "AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH", "LD_LIBRARY_PATH",
])
def test_the_snapshot_does_not_set_what_describes_the_hosts_process(tmp_path, name):
    environ = _snapshot(tmp_path, {name: "from the host", "EMORI_LANG": "ca"})
    environ[name] = "from the container"
    hub.load_env_snapshot(environ)
    assert environ[name] == "from the container"
    assert environ["EMORI_LANG"] == "ca"


def test_the_denylist_matches_a_whole_name():
    assert not hub._denied("HOME_ASSISTANT")
    assert not hub._denied("EMORI_ROS_MOTION_OWNER")
    assert hub._denied("ROS_ANYTHING")


def test_no_snapshot_named_is_no_snapshot():
    environ = {"EMORI_LANG": "en"}
    hub.load_env_snapshot(environ)
    assert environ == {"EMORI_LANG": "en"}


def test_the_block_is_not_put_in_the_environment_as_itself(tmp_path):
    environ = _snapshot(tmp_path, {"EMORI_LANG": "ca", "graph": {"EMORI_ROS_SPEECH_TEXT": "1"}})
    hub.load_env_snapshot(environ)
    assert environ["EMORI_LANG"] == "ca"
    assert "graph" not in environ
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "1", "what leaves on DDS is the block's to say"


@pytest.mark.parametrize("content, says", [
    ("{ not json", "cannot read"),
    ('["a list"]', "not a JSON object"),
    ({"POKAMIND_TOKEN": 12345}, "POKAMIND_TOKEN is not a string"),
])
def test_a_snapshot_that_cannot_be_used_stops_the_start_and_names_no_value(tmp_path, content, says):
    environ = _snapshot(tmp_path, content)
    with pytest.raises(hub.HubConfigError, match=says) as raised:
        hub.load_env_snapshot(environ)
    assert "12345" not in str(raised.value)


def test_a_snapshot_that_is_not_there_stops_the_start():
    with pytest.raises(hub.HubConfigError, match="cannot read"):
        hub.load_env_snapshot({hub.ENV_JSON_VAR: "/nowhere/env.json"})


def test_main_refuses_a_bad_snapshot_before_it_loads_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(hub.ENV_JSON_VAR, str(tmp_path / "absent.json"))
    monkeypatch.setattr(hub, "run_hub", lambda *a, **k: pytest.fail("it started"))
    assert hub.main() == hub.EXIT_FAILED
    assert "environment snapshot" in capsys.readouterr().err


@pytest.mark.parametrize("value, transport", [
    (None, "ros"), ("", "ros"), ("ros", "ros"), ("http", "http"), (" HTTP ", "http"),
])
def test_the_transport_is_ros_unless_told_http(value, transport):
    environ = {} if value is None else {hub.TRANSPORT_VAR: value}
    assert hub.robot_transport(environ) == transport


def test_an_unknown_transport_starts_nothing(bench):
    code = bench.run(environ={hub.TRANSPORT_VAR: "dds"})
    assert code == hub.EXIT_FAILED
    assert bench.events == []


# --- the start -------------------------------------------------------------------------


def test_the_start_runs_in_order(bench):
    assert bench.run(facades=[bench.node("session")], graph=[bench.node("skill")]) == hub.EXIT_OK
    assert bench.events[:bench.events.index("serve")] == [
        "rclpy init",
        "node /emori/robot_link",
        "build app",
        "node session",
        "node skill",
    ]


def test_rclpy_is_started_with_the_arguments_this_process_needs(bench):
    bench.run()
    assert list(bench.ros.init_args) == ["--ros-args", "--disable-external-lib-logs"]


def test_the_link_is_spinning_before_the_app_is_built(bench):
    """Building the app pushes both gates and starts the player: both talk over the link."""

    seen = {}

    def during_build() -> None:
        seen["spun"] = _wait_until(lambda: bench.ros.link_executor.spins > 0)
        seen["thread"] = _hub_threads()

    bench.host.during_build = during_build
    bench.run()
    assert seen["spun"]
    assert "ros-link" in seen["thread"]


def test_the_app_is_given_the_ros_client_and_the_link_it_was_built_on(bench):
    bench.run()
    client = bench.host.overrides["robot_client"]
    assert isinstance(client, robot_link.RosRobotClient)
    assert client.runtime == "ros"
    assert set(bench.host.overrides) == {"robot_client"}


def test_uvicorn_is_told_the_loop_and_parser_and_keeps_its_own_websocket_setting(bench):
    bench.run()
    assert bench.host.serve_kwargs == {"loop": "asyncio", "http": "h11"}


def test_the_groups_are_three_threads_each_with_their_nodes_on_them(bench):
    seen = {}

    def during_serve() -> None:
        assert _wait_until(lambda: all(executor.spins for executor in bench.ros.group_executors))
        seen["threads"] = _hub_threads()

    bench.host.during_serve = during_serve
    bench.run(facades=[bench.node("session"), bench.node("speech")], graph=[bench.node("skill")])
    facade, graph = bench.ros.group_executors
    assert (facade.num_threads, graph.num_threads) == (3, 3)
    assert [node.name for node in facade.nodes] == ["session", "speech"]
    assert [node.name for node in graph.nodes] == ["skill"]
    assert {"ros-facade", "ros-graph", "ros-link"} <= set(seen["threads"])


def test_a_group_with_no_nodes_has_no_executor_and_no_thread(bench):
    bench.host.during_serve = lambda: bench.events.append(",".join(_hub_threads()))
    bench.run()
    assert bench.ros.group_executors == []
    assert "ros-facade" not in " ".join(bench.events)


def test_over_http_there_is_no_link_node_and_the_client_is_rests(bench):
    from emori_adapter.robot.client import HttpRobotClient

    code = bench.run(environ={hub.TRANSPORT_VAR: "http"})
    assert code == hub.EXIT_OK
    client = bench.host.overrides["robot_client"]
    assert isinstance(client, HttpRobotClient)
    assert not getattr(client, "runtime", "")
    assert bench.events == ["rclpy init", "build app", "serve", "serve returned", "rclpy shutdown"]


def test_the_wrapper_hook_goes_around_either_client(bench):
    bench.run(wrap_client=bench.wrap)
    assert bench.host.overrides["robot_client"] is bench.wrapped
    assert isinstance(bench.wrapped.inner, robot_link.RosRobotClient)
    assert "client close" in bench.events


def test_the_normal_entry_passes_no_wrapper(monkeypatch):
    seen = {}

    def run_hub(_ros, _host, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(hub, "run_hub", run_hub)
    monkeypatch.setattr(hub, "load_hub_bindings", lambda: object())
    monkeypatch.setattr("emori_adapter.logging_setup.setup_journal_logging", lambda: True)
    monkeypatch.delenv(hub.ENV_JSON_VAR, raising=False)
    monkeypatch.delenv(hub.FACADES_VAR, raising=False)
    assert hub.main() == 0
    assert seen["wrap_client"] is None and seen["graph_settings"] is None


def test_the_normal_entry_hosts_the_facades_and_gives_the_add_on_its_ports(monkeypatch):
    seen = {}

    def run_hub(_ros, _host, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(hub, "run_hub", run_hub)
    monkeypatch.setattr(hub, "load_hub_bindings", lambda: object())
    monkeypatch.setattr("emori_adapter.logging_setup.setup_journal_logging", lambda: True)
    monkeypatch.delenv(hub.ENV_JSON_VAR, raising=False)
    monkeypatch.delenv(hub.FACADES_VAR, raising=False)
    assert hub.main() == 0
    assert seen["facades"][:-1] == hub.FACADE_NODES and len(seen["facades"]) == 6
    assert isinstance(seen["ports"], hub.RosPorts)
    assert sys.getswitchinterval() == hub.GIL_SWITCH_S


def test_no_facade_is_hosted_when_told_so(monkeypatch):
    seen = {}

    def run_hub(_ros, _host, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(hub, "run_hub", run_hub)
    monkeypatch.setattr(hub, "load_hub_bindings", lambda: object())
    monkeypatch.setattr("emori_adapter.logging_setup.setup_journal_logging", lambda: True)
    monkeypatch.delenv(hub.ENV_JSON_VAR, raising=False)
    monkeypatch.setenv(hub.FACADES_VAR, "0")
    assert hub.main() == 0
    assert seen["facades"] == () and seen["ports"] is None


def test_the_add_on_is_handed_the_ports_when_the_app_is_built(bench):
    ports = hub.RosPorts()
    assert bench.run(ports=ports) == hub.EXIT_OK
    assert bench.host.overrides["ros"] is ports


def test_a_node_that_cannot_be_built_costs_its_topics_and_not_the_kiosk(bench, caplog):
    def boom(_app: Any) -> None:
        raise RuntimeError("a package is missing")

    bench.host.app = SimpleNamespace(state=SimpleNamespace())
    code = bench.run(facades=[bench.node("session"), boom], graph=[boom])
    assert code == hub.EXIT_OK
    assert "serve" in bench.events and "destroy session" in bench.events
    assert "boom failed" in caplog.text
    sources = facade_kit.diagnostic_sources(bench.host.app)
    assert sources["ros-facade_nodes"]().level == facade_kit.ERROR
    assert "boom" in sources["ros-graph_nodes"]().message
    assert bench.host.app.state.hub_nodes["failed"] == ["boom", "boom"]


def test_the_hub_says_which_deploy_it_started_on(monkeypatch, tmp_path):
    """Read at the start, so that a deploy after it shows as a difference."""

    (tmp_path / "DEPLOYED_COMMIT.txt").write_text("abc1234 (x)\nmore\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert hub._deployed_marker() == "abc1234 (x)"


def test_a_group_with_nothing_left_starts_no_executor(bench):
    def boom(_app: Any) -> None:
        raise RuntimeError("a package is missing")

    bench.host.app = SimpleNamespace(state=SimpleNamespace())
    assert bench.run(facades=[boom]) == hub.EXIT_OK
    assert not any("group" in event for event in bench.events)


def test_main_warns_of_a_host_setting_the_graph_ignores_by_name_and_never_by_value(
        tmp_path, monkeypatch, caplog):
    seen = {}

    def run_hub(_ros, _host, **kwargs):
        seen.update(kwargs)
        return 0

    path = tmp_path / "env.json"
    path.write_text(json.dumps({
        hub.GRAPH_VAR: "1", "EMORI_ROS_ID_SALT": "a-secret-salt",
        "graph": {"EMORI_ROS_ID_SALT": "another-secret-salt"}}), encoding="utf-8")
    monkeypatch.setattr(hub, "run_hub", run_hub)
    monkeypatch.setattr(hub, "load_hub_bindings", lambda: object())
    monkeypatch.setattr("emori_adapter.logging_setup.setup_journal_logging", lambda: True)
    monkeypatch.setenv(hub.ENV_JSON_VAR, str(path))
    # Only so that what the snapshot sets is put back after this test.
    for name in (hub.GRAPH_VAR, "EMORI_ROS_ID_SALT", "EMORI_ROS_DEIDENTIFY",
                 "EMORI_ROS_SPEECH_TEXT", "EMORI_SKILL_TRANSPORT"):
        monkeypatch.setenv(name, "")

    assert hub.main() == 0

    assert seen["graph_settings"].behavior_yaml == hub.GRAPH_BEHAVIOR_DEFAULT
    assert "EMORI_ROS_ID_SALT" in caplog.text
    assert "secret" not in caplog.text
    assert os.environ["EMORI_ROS_ID_SALT"] == "another-secret-salt"


def test_main_says_what_is_missing_when_ros_is_not_there(monkeypatch, caplog):
    def absent():
        raise ImportError("No module named 'rclpy'")

    monkeypatch.setattr(hub, "load_hub_bindings", absent)
    monkeypatch.setattr("emori_adapter.logging_setup.setup_journal_logging", lambda: True)
    monkeypatch.delenv(hub.ENV_JSON_VAR, raising=False)
    assert hub.main() == hub.EXIT_FAILED
    assert "rclpy" in caplog.text
    assert "emori_adapter.hostapp.server" in caplog.text


# --- the graph -------------------------------------------------------------------------


_GRAPH_PARAMS = "/share/emori_hub/config/graph.yaml"


def _with_graph_params(bench: _Bench) -> None:
    bench.ros.bindings = dataclasses.replace(bench.ros.bindings,
                                             graph_params=lambda: _GRAPH_PARAMS)


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", "2"])
def test_only_a_one_hosts_the_graph(tmp_path, value):
    environ = _snapshot(tmp_path, {hub.GRAPH_VAR: value, "EMORI_LANG": "ca",
                                   "graph": {"EMORI_ROS_SPEECH_TEXT": "1"}})
    assert hub.load_env_snapshot(environ) is None
    assert environ["EMORI_LANG"] == "ca"
    assert "EMORI_SKILL_TRANSPORT" not in environ, "the graph's transport comes with the graph"
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "1" and environ["EMORI_ROS_DEIDENTIFY"] == "1"


def test_the_graphs_privacy_values_win_over_what_the_host_files_say(tmp_path):
    environ = _snapshot(tmp_path, {
        hub.GRAPH_VAR: "1", "EMORI_ROS_SPEECH_TEXT": "1", "EMORI_ROS_DEIDENTIFY": "0",
        "graph": {"EMORI_ROS_DEIDENTIFY": "1", "EMORI_ROS_SPEECH_TEXT": "0",
                  "EMORI_ROS_ID_SALT": "salt-from-the-motor-file"}})
    settings = hub.load_env_snapshot(environ)
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "0"
    assert environ["EMORI_ROS_DEIDENTIFY"] == "1"
    assert environ["EMORI_ROS_ID_SALT"] == "salt-from-the-motor-file"
    assert settings.ignored == ("EMORI_ROS_DEIDENTIFY", "EMORI_ROS_SPEECH_TEXT")


def test_a_site_may_open_the_gate_only_in_the_file_that_says_so(tmp_path):
    environ = _snapshot(tmp_path, {hub.GRAPH_VAR: "1",
                                   "graph": {"EMORI_ROS_SPEECH_TEXT": "1"}})
    settings = hub.load_env_snapshot(environ)
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "1"
    assert settings.ignored == ()


@pytest.mark.parametrize("block", [{}, "not a block", None])
def test_a_block_that_says_nothing_means_the_closed_defaults(tmp_path, block):
    content = {hub.GRAPH_VAR: "1", "EMORI_ROS_SPEECH_TEXT": "1", "EMORI_ROS_DEIDENTIFY": "0"}
    if block is not None:
        content["graph"] = block
    environ = _snapshot(tmp_path, content)
    settings = hub.load_env_snapshot(environ)
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "0"
    assert environ["EMORI_ROS_DEIDENTIFY"] == "1"
    assert environ["EMORI_ROS_ID_SALT"] == ""
    assert settings.behavior_yaml == hub.GRAPH_BEHAVIOR_DEFAULT


def test_with_no_snapshot_the_process_environment_stands_in_for_the_file():
    environ = {hub.GRAPH_VAR: "1", "EMORI_ROS_SPEECH_TEXT": "1",
               hub.GRAPH_BEHAVIOR_VAR: "/opt/emori/configs/tuned.yaml"}
    settings = hub.load_env_snapshot(environ)
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "1"
    assert environ["EMORI_ROS_DEIDENTIFY"] == "1"
    assert settings.behavior_yaml == "/opt/emori/configs/tuned.yaml"
    assert settings.ignored == ()


def test_the_transport_is_set_for_the_process_and_a_different_one_is_named(tmp_path):
    environ = _snapshot(tmp_path, {hub.GRAPH_VAR: "1", "EMORI_SKILL_TRANSPORT": "hostapp_http"})
    settings = hub.load_env_snapshot(environ)
    assert environ["EMORI_SKILL_TRANSPORT"] == "ros_native"
    assert "EMORI_SKILL_TRANSPORT" in settings.ignored


def test_the_behaviour_path_is_a_parameter_and_the_live_path_keeps_its_own(tmp_path):
    environ = _snapshot(tmp_path, {
        hub.GRAPH_VAR: "1", "EMORI_BEHAVIOR_CONFIG": "configs/behavior.yaml",
        "graph": {hub.GRAPH_BEHAVIOR_VAR: "/opt/emori/configs/tuned.yaml"}})
    settings = hub.load_env_snapshot(environ)
    assert settings.behavior_yaml == "/opt/emori/configs/tuned.yaml"
    assert environ["EMORI_BEHAVIOR_CONFIG"] == "configs/behavior.yaml"
    assert hub.GRAPH_BEHAVIOR_VAR not in environ


def test_without_the_graph_rclpy_gets_nothing_of_it_and_no_group_is_made(bench):
    bench.run()
    assert list(bench.ros.init_args) == list(hub.ROS_ARGS)
    assert bench.ros.group_executors == []


def test_with_the_graph_rclpy_gets_the_parameter_file_and_the_behaviour_file(bench):
    _with_graph_params(bench)
    bench.run(graph=[bench.node("skill")],
              graph_settings=hub.GraphSettings(behavior_yaml="/opt/emori/configs/tuned.yaml"))
    assert list(bench.ros.init_args) == [
        "--ros-args", "--disable-external-lib-logs", "--params-file", _GRAPH_PARAMS,
        "-p", "behavior_yaml:=/opt/emori/configs/tuned.yaml"]


def test_the_toggle_puts_the_four_on_their_own_executor_and_none_on_the_links(bench, monkeypatch):
    names = ("adapter", "analyzer", "behaviours", "facade")
    monkeypatch.setattr(hub, "GRAPH_NODES", tuple(bench.node(name) for name in names))
    _with_graph_params(bench)
    seen = {}

    def during_serve() -> None:
        assert _wait_until(lambda: all(executor.spins for executor in bench.ros.group_executors))
        seen["threads"] = _hub_threads()

    bench.host.during_serve = during_serve
    assert bench.run(graph_settings=hub.GraphSettings()) == hub.EXIT_OK

    (group,) = bench.ros.group_executors
    assert group is not bench.ros.link_executor
    assert group.num_threads == 3
    assert [node.name for node in group.nodes] == list(names)
    assert not any(isinstance(node, _GroupNode) for node in bench.ros.link_executor.nodes)
    assert {"ros-graph", "ros-link"} <= set(seen["threads"])
    assert bench.events.index("build app") < bench.events.index("node adapter")
    assert bench.events.index("node facade") < bench.events.index("serve")


def test_the_graph_stops_before_the_link_and_after_the_server(bench, monkeypatch):
    monkeypatch.setattr(hub, "GRAPH_NODES", (bench.node("adapter"),))
    _with_graph_params(bench)
    bench.run(graph_settings=hub.GraphSettings())
    tail = bench.tail_after("serve returned")
    assert tail.index("destroy adapter") < tail.index("destroy /emori/robot_link")


def test_a_graph_node_that_cannot_be_built_leaves_the_kiosk_serving(bench, monkeypatch):
    def build(_app):
        raise RuntimeError("no such package")

    monkeypatch.setattr(hub, "GRAPH_NODES", (build,))
    _with_graph_params(bench)
    assert bench.run(graph_settings=hub.GraphSettings()) == hub.EXIT_OK
    assert "serve" in bench.events


def test_the_real_graph_is_what_the_toggle_uses():
    from emori_hub import graph

    assert hub.GRAPH_NODES == graph.GRAPH_BUILDERS


# --- the stop --------------------------------------------------------------------------


def test_a_stop_goes_last_started_first_after_the_server_is_done(bench):
    code = bench.run(wrap_client=bench.wrap, facades=[bench.node("session"), bench.node("speech")],
                     graph=[bench.node("skill")])
    assert code == hub.EXIT_OK
    assert bench.tail_after("serve returned") == [
        "group1 executor shutdown",
        "destroy skill",
        "group0 executor shutdown",
        "destroy speech",
        "destroy session",
        "client close",
        "ros-link executor shutdown",
        "destroy /emori/robot_link",
        "rclpy shutdown",
    ]


def test_a_stop_leaves_nothing_running(bench):
    executors: list[_Executor] = []

    def during_serve() -> None:
        executors.extend([bench.ros.link_executor, *bench.ros.group_executors])
        assert _wait_until(lambda: all(executor.spins for executor in executors))
        assert {"ros-link", "ros-facade", "ros-graph", "hub-watch"} <= set(_hub_threads())

    before = signal.getsignal(_TERM)
    bench.host.during_serve = during_serve
    code = bench.run(wrap_client=bench.wrap, facades=[bench.node("session")],
                     graph=[bench.node("skill")])
    assert code == hub.EXIT_OK

    assert _wait_until(lambda: not _hub_threads()), _hub_threads()
    counts = [executor.spins for executor in executors]
    time.sleep(0.05)
    assert [executor.spins for executor in executors] == counts, "something is still spinning"
    assert bench.ros.context_ok is False
    assert bench.wrapped.inner._closed.is_set()
    assert bench.events.count("rclpy shutdown") == 1
    assert signal.getsignal(_TERM) is before
    assert bench.exits == []


def test_a_clean_stop_is_not_a_death(bench, caplog):
    with caplog.at_level(logging.WARNING):
        code = bench.run(facades=[bench.node("session")])
    assert code == hub.EXIT_OK
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("lands_in", ["the app", "a facade", "a graph node"])
def test_a_stop_during_the_start_never_serves(bench, lands_in):
    def stop(_app: Any = None) -> _GroupNode:
        signal.raise_signal(_TERM)
        return _GroupNode(bench.events, "the one the stop landed in")

    facades, graph = [bench.node("session")], [bench.node("skill")]
    if lands_in == "the app":
        bench.host.during_build = stop
    elif lands_in == "a facade":
        facades = [stop]
    else:
        graph = [stop]

    assert bench.run(facades=facades, graph=graph) == hub.EXIT_OK
    assert "serve" not in bench.events
    if lands_in == "the app":
        assert "node session" not in bench.events
    if lands_in != "a graph node":
        assert "node skill" not in bench.events
    assert bench.events[-1] == "rclpy shutdown"
    assert _wait_until(lambda: not _hub_threads()), _hub_threads()


def test_the_signal_is_what_stops_the_server_and_its_second_raise_changes_nothing(bench):
    uvicorn = _MiniUvicorn(bench.host)

    def during_serve() -> None:
        def later() -> None:
            uvicorn.holding.wait(5.0)
            signal.raise_signal(_TERM)

        threading.Thread(target=later, daemon=True).start()
        uvicorn()

    bench.host.during_serve = during_serve
    code = bench.run(wrap_client=bench.wrap)
    assert code == hub.EXIT_OK
    assert bench.tail_after("lifespan stopped")[:2] == ["serve returned", "client close"]
    assert bench.events[-1] == "rclpy shutdown"


def test_a_stop_noted_just_before_uvicorn_takes_over_still_reaches_it(bench):
    uvicorn = _MiniUvicorn(bench.host)

    def during_serve() -> None:
        signal.raise_signal(_TERM)  # lands on this process's own handler
        time.sleep(0.05)
        uvicorn()

    bench.host.during_serve = during_serve
    assert bench.run() == hub.EXIT_OK
    assert uvicorn.captured == [_TERM], "the stop was lost and the server would run on"


@pytest.mark.parametrize("fail_at, stopped", [
    ("init", []),
    ("link node", ["rclpy shutdown"]),
    ("wrapper", ["ros-link executor shutdown", "destroy /emori/robot_link", "rclpy shutdown"]),
    ("build", ["client close", "ros-link executor shutdown", "destroy /emori/robot_link",
               "rclpy shutdown"]),
    ("serve", ["group1 executor shutdown", "destroy skill", "group0 executor shutdown",
               "destroy session", "client close", "ros-link executor shutdown",
               "destroy /emori/robot_link", "rclpy shutdown"]),
])
def test_a_failure_at_any_step_stops_what_had_started(bench, caplog, fail_at, stopped):
    def boom(*_args: Any) -> None:
        bench.events.append("boom")
        raise RuntimeError(f"failed at {fail_at}")

    wrap_client: Any = bench.wrap
    facades = [bench.node("session")]
    graph = [bench.node("skill")]
    if fail_at == "init":
        bench.ros.init_fails = True
    elif fail_at == "link node":
        bench.ros.link_node_fails = True
    elif fail_at == "wrapper":
        wrap_client = boom
    elif fail_at == "build":
        bench.host.during_build = boom
    elif fail_at == "serve":
        bench.host.during_serve = boom

    code = bench.run(wrap_client=wrap_client, facades=facades, graph=graph)

    assert code == hub.EXIT_FAILED
    assert "the hub could not run" in caplog.text
    assert bench.tail_after("boom") == stopped
    assert _wait_until(lambda: not _hub_threads()), _hub_threads()


@pytest.mark.parametrize("code, expected", [
    (3, 3),  # the lifespan's startup failed
    (1, 1),  # the port is taken
    (0, hub.EXIT_OK),
    (None, hub.EXIT_OK),
    ("words", hub.EXIT_FAILED),
])
def test_the_servers_own_exit_code_is_the_processs_and_teardown_still_runs(bench, code, expected):
    def exits() -> None:
        raise SystemExit(code)

    bench.host.during_serve = exits
    assert bench.run() == expected
    assert bench.events[-1] == "rclpy shutdown"
    assert _wait_until(lambda: not _hub_threads()), _hub_threads()


def test_a_step_that_raises_does_not_stop_the_steps_after_it(bench, caplog):
    def broken() -> None:
        raise RuntimeError("already gone")

    def during_serve() -> None:
        bench.ros.group_executors[0].on_shutdown = broken

    bench.host.during_serve = during_serve
    assert bench.run(facades=[bench.node("session")]) == hub.EXIT_OK
    assert bench.tail_after("serve returned") == [
        "group0 executor shutdown", "destroy session", "ros-link executor shutdown",
        "destroy /emori/robot_link", "rclpy shutdown"]
    assert "a release failed" in caplog.text


def test_a_step_that_hangs_costs_its_limit_and_the_steps_after_it_run(bench, caplog):
    release = threading.Event()

    def during_serve() -> None:
        bench.ros.group_executors[0].on_shutdown = release.wait

    bench.host.during_serve = during_serve
    try:
        started = time.monotonic()
        assert bench.run(facades=[bench.node("session")]) == hub.EXIT_OK
        took = time.monotonic() - started
    finally:
        release.set()
    assert bench.events[-1] == "rclpy shutdown"
    assert "ros-link executor shutdown" in bench.events
    assert "ros-facade did not stop within" in caplog.text
    assert took < hub.TEARDOWN_BUDGET_S


def test_the_teardown_fits_the_limit_the_container_is_given():
    from tests.test_hostapp_unit_stop import _SERVER, _function, _join_budget

    lifespan = _function(_SERVER, "lifespan")
    teardown = [node for node in ast.walk(lifespan) if isinstance(node, ast.Try)
                and node.finalbody]
    assert _join_budget(teardown[0].finalbody) + hub.TEARDOWN_BUDGET_S < hub.STOP_BUDGET_S


# --- the deadline ----------------------------------------------------------------------


def test_the_deadline_ends_a_process_a_stuck_thread_would_hold_open(caplog):
    exits: list[int] = []
    life = hub.Lifecycle(exit_now=exits.append)
    life.note_failure(7)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, name="stuck-worker", daemon=False)
    stuck.start()
    try:
        life.exit_by(0.01, "the teardown")
        assert _wait_until(lambda: exits)
    finally:
        release.set()
        stuck.join()
    assert exits == [7]
    assert "the teardown took longer" in caplog.text
    assert "stuck-worker" in caplog.text


def test_a_cancelled_deadline_does_nothing():
    exits: list[int] = []
    life = hub.Lifecycle(exit_now=exits.append)
    life.exit_by(30.0, "the teardown")
    thread = next(thread for thread in threading.enumerate() if thread.name == "hub-exit-deadline")
    life.cancel_deadlines()
    thread.join(5.0)
    assert not thread.is_alive()
    assert exits == []


# --- a spin that dies ------------------------------------------------------------------


def _serving_until_told(bench: _Bench, ready: Callable[[], None]) -> None:
    uvicorn = _MiniUvicorn(bench.host)

    def during_serve() -> None:
        def later() -> None:
            uvicorn.holding.wait(5.0)
            ready()

        threading.Thread(target=later, daemon=True).start()
        uvicorn()

    bench.host.during_serve = during_serve


def _kill(bench: _Bench, how: str, executor: _Executor) -> None:
    if how == "thread":  # gets past every except clause in the loop
        executor.fail_next = SystemExit()
    elif how == "fatal":
        executor.fail_next = _ContextGone()
    elif how == "context":
        bench.ros.context_ok = False
    elif how == "broken":
        executor.fail_always = RuntimeError("the wait set is gone")


@_A_THREAD_DIES
@pytest.mark.parametrize("how, why", [
    ("thread", "its thread ended"),
    ("fatal", "the ROS context went away"),
    ("context", "the ROS context went away"),
    ("broken", "raised 40 times in a row"),
])
def test_a_dead_link_stops_the_hub_with_the_code_that_gets_it_restarted(bench, caplog, how, why):
    _serving_until_told(bench, lambda: _kill(bench, how, bench.ros.link_executor))
    code = bench.run(wrap_client=bench.wrap)

    assert code == hub.EXIT_SPIN_DIED
    assert "ros-link has died" in caplog.text
    assert why in caplog.text
    assert bench.tail_after("lifespan stopped")[:2] == ["serve returned", "client close"]
    assert bench.events[-1] == "rclpy shutdown"
    assert _wait_until(lambda: not _hub_threads()), _hub_threads()
    assert bench.exits == []


@_A_THREAD_DIES
@pytest.mark.parametrize("group", ["ros-facade", "ros-graph"])
@pytest.mark.parametrize("how", ["thread", "fatal"])
def test_a_dead_group_stops_the_hub_too(bench, caplog, group, how):
    executor_index = 0 if group == "ros-facade" else 1
    _serving_until_told(bench, lambda: _kill(bench, how, bench.ros.group_executors[executor_index]))
    code = bench.run(facades=[bench.node("session")], graph=[bench.node("skill")])
    assert code == hub.EXIT_SPIN_DIED
    assert f"{group} has died" in caplog.text
    assert _wait_until(lambda: not _hub_threads()), _hub_threads()


@_A_THREAD_DIES
def test_a_spin_that_dies_during_the_start_means_no_server_and_the_same_code(bench):
    def during_build() -> None:
        bench.ros.link_executor.fail_next = SystemExit()
        assert _wait_until(lambda: bench.life.stop_wanted)

    bench.host.during_build = during_build
    assert bench.run(facades=[bench.node("session")]) == hub.EXIT_SPIN_DIED
    assert "serve" not in bench.events
    assert bench.events[-1] == "rclpy shutdown"


@_A_THREAD_DIES
def test_a_dead_spin_is_the_reason_given_even_if_the_server_then_fails_too(bench):
    def during_serve() -> None:
        bench.ros.link_executor.fail_next = SystemExit()
        assert _wait_until(lambda: bench.life.stop_wanted)
        raise SystemExit(3)

    bench.host.during_serve = during_serve
    assert bench.run() == hub.EXIT_SPIN_DIED


@_A_THREAD_DIES
def test_a_stop_after_a_dead_spin_has_a_deadline_of_its_own(bench, monkeypatch):
    armed: list[tuple[float, str]] = []
    monkeypatch.setattr(bench.life, "exit_by", lambda seconds, what: armed.append((seconds, what)))
    _serving_until_told(bench, lambda: _kill(bench, "thread", bench.ros.link_executor))
    bench.run()
    assert armed[0] == (hub.STOP_BUDGET_S, "the stop after a dead executor")
    assert armed[1] == (hub.TEARDOWN_BUDGET_S, "the ROS teardown")


def test_a_callback_that_raises_is_logged_and_nobody_stops(bench, caplog):
    def during_serve() -> None:
        link = bench.ros.link_executor
        facade = bench.ros.group_executors[0]
        link.fail_next = RuntimeError("one bad callback")
        facade.fail_next = RuntimeError("one bad callback")
        assert _wait_until(lambda: link.spins > 5 and facade.spins > 5)
        time.sleep(0.1)
        assert not bench.life.stop_wanted

    bench.host.during_serve = during_serve
    assert bench.run(facades=[bench.node("session")]) == hub.EXIT_OK
    assert "carrying on" in caplog.text
    assert "has died" not in caplog.text


def test_a_facade_that_fails_on_every_tick_does_not_take_the_hub_down(bench):
    """It looks like a broken executor from here, and it is one bad callback."""

    def during_serve() -> None:
        facade = bench.ros.group_executors[0]
        facade.fail_always = RuntimeError("this callback always fails")
        spins = facade.spins
        assert _wait_until(lambda: facade.spins > spins + 45)
        assert not bench.life.stop_wanted

    bench.host.during_serve = during_serve
    assert bench.run(facades=[bench.node("session")]) == hub.EXIT_OK


# --- the supervised spin ---------------------------------------------------------------


def _spinning(executor: _Executor, **kwargs: Any) -> SupervisedSpin:
    kwargs.setdefault("ok", lambda: True)
    kwargs.setdefault("fatal", (_ContextGone,))
    spin = SupervisedSpin("test-spin", executor, **kwargs)
    spin.start()
    return spin


def test_a_spin_says_nothing_before_it_starts_or_while_it_runs():
    executor = _Executor([], "x")
    spin = SupervisedSpin("test-spin", executor, ok=lambda: True)
    assert spin.death() == ""
    spin.start()
    assert _wait_until(lambda: executor.spins > 2)
    assert spin.death() == ""
    assert spin.stop() is True


def test_a_stopped_spin_is_not_a_dead_one_whatever_else_went_away():
    executor = _Executor([], "x")
    context = {"ok": True}
    spin = _spinning(executor, ok=lambda: context["ok"])
    assert spin.stop() is True
    context["ok"] = False
    assert spin.death() == ""
    assert not spin.thread.is_alive()


def test_a_clean_spin_between_faults_resets_the_limit():
    executor = _Executor([], "x")
    spin = _spinning(executor, fault_limit=3)
    for _ in range(6):
        spins = executor.spins
        executor.fail_next = RuntimeError("one")
        assert _wait_until(lambda: executor.spins > spins + 2)
    assert spin.death() == ""
    spin.stop()


def test_the_limit_counts_faults_in_a_row_and_ends_the_spin():
    executor = _Executor([], "x")
    executor.fail_always = RuntimeError("broken")
    spin = _spinning(executor, fault_limit=3)
    assert _wait_until(spin.death)
    assert spin.death() == "its executor raised 3 times in a row"
    assert executor.spins == 3
    assert _wait_until(lambda: not spin.thread.is_alive())


def test_without_a_limit_faults_go_on_and_are_logged_at_a_bounded_rate(caplog):
    executor = _Executor([], "x")
    executor.fail_always = RuntimeError("broken")
    with caplog.at_level(logging.WARNING, logger="emori.hub.spin"):
        spin = _spinning(executor)
        assert _wait_until(lambda: executor.spins > 3)
        spin.stop()
    assert spin.death() == ""
    assert len([record for record in caplog.records if "carrying on" in record.message]) == 1


def test_a_complaint_after_the_period_counts_what_it_left_out(caplog):
    now = [0.0]
    spin = SupervisedSpin("test-spin", object(), ok=lambda: True, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="emori.hub.spin"):
        spin._complain(RuntimeError("first"))
        spin._complain(RuntimeError("second"))
        spin._complain(RuntimeError("third"))
        now[0] = 31.0
        spin._complain(RuntimeError("fourth"))
    assert [record.getMessage() for record in caplog.records] == [
        "test-spin: the executor raised; carrying on (0 not logged since the last)",
        "test-spin: the executor raised; carrying on (2 not logged since the last)",
    ]


def test_a_spin_stuck_in_its_executor_is_reported_by_stop():
    release = threading.Event()

    class Stuck:
        def spin_once(self, timeout_sec: float = 0.0) -> None:
            release.wait()

    spin = SupervisedSpin("test-spin", Stuck(), ok=lambda: True)
    spin.start()
    try:
        assert spin.stop(timeout=0.01) is False
    finally:
        release.set()
    assert spin.thread.join(2.0) is None


# --- nothing loads early ---------------------------------------------------------------


def test_main_imports_no_ros_and_no_core_when_it_loads():
    """The core reads the environment as it loads, so loading it here would miss the snapshot."""

    code = ("import sys; import emori_hub.main; "
            "print([name for name in sys.modules if name.split('.')[0] in "
            "('rclpy', 'emori_adapter', 'reachy_mini_msgs', 'std_msgs', 'std_srvs')])")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_ROOT / "src"), str(_HUB)])}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_main_loads_the_snapshot_before_anything_of_the_core():
    tree = ast.parse((_HUB / "emori_hub" / "main.py").read_text(encoding="utf-8"))
    main = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    order: list[str] = []
    for statement in main.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "load_env_snapshot":
                order.append("snapshot")
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("emori_adapter"):
                order.append("core")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_hub":
                order.append("run")
    assert order[0] == "snapshot"
    assert order[-1] == "run"
    assert "core" in order


def test_the_hub_goes_through_the_one_place_uvicorn_is_started():
    source = (_HUB / "emori_hub" / "main.py").read_text(encoding="utf-8")
    assert "import uvicorn" not in source
    assert "uvicorn.run" not in source


def test_the_bindings_refuse_to_load_without_ros():
    try:
        import rclpy  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            hub.load_hub_bindings()
    else:  # pragma: no cover - only in the hub's image
        assert hub.load_hub_bindings().multi_threaded_executor


def test_rclpy_is_told_to_leave_the_signals_alone(monkeypatch):
    calls: list[dict[str, Any]] = []
    no = object()

    def module(name: str, **attributes: Any) -> None:
        made = ModuleType(name)
        made.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, made)

    class Fatal(Exception):
        pass

    module("rclpy", init=lambda **kwargs: calls.append(kwargs), try_shutdown=lambda: None,
           ok=lambda: True, __path__=[])
    module("rclpy.executors", ExternalShutdownException=Fatal, MultiThreadedExecutor=object,
           SingleThreadedExecutor=object)
    module("rclpy.signals", SignalHandlerOptions=SimpleNamespace(NO=no))
    module("rclpy.callback_groups", ReentrantCallbackGroup=object)
    module("rclpy.node", Node=object)
    qos = SimpleNamespace(KEEP_LAST=1, BEST_EFFORT=1, VOLATILE=1)
    module("rclpy.qos", DurabilityPolicy=qos, HistoryPolicy=qos, ReliabilityPolicy=qos,
           QoSProfile=lambda **kwargs: kwargs)
    module("std_msgs", __path__=[])
    module("std_msgs.msg", Header=object)
    module("std_srvs", __path__=[])
    module("std_srvs.srv", SetBool=object)
    module("reachy_mini_msgs", __path__=[])
    module("reachy_mini_msgs.srv", GetFullState=object, GetRobotStatus=object, PlaySound=object,
           SetMotorMode=object, StopMove=object, SubmitMove=object)

    bindings = hub.load_hub_bindings()
    bindings.init(hub.ROS_ARGS)
    assert calls == [{"args": list(hub.ROS_ARGS), "signal_handler_options": no}]
    assert bindings.fatal == (Fatal,)


# --- the harness's way in --------------------------------------------------------------


def _load_golden_host() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("golden_host_under_test",
                                                  _ROOT / "scripts" / "golden_host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_harness_records_the_hubs_robot_calls_through_the_wrapper_hook(tmp_path, monkeypatch):
    from emori_adapter.robot.client import DryRunRobotClient
    from emori_adapter.robot.tracing import TracingRobotClient

    seen = {}

    def run(*, wrap_client):
        client = wrap_client(DryRunRobotClient())
        client.health()
        seen["client"] = client
        return 5

    monkeypatch.setattr(hub, "main", run)
    assert _load_golden_host().run_traced_hub(tmp_path) == 5
    assert isinstance(seen["client"], TracingRobotClient)
    lines = (tmp_path / "client.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["method"] == "health"


def test_the_harness_flag_runs_the_hub_and_returns_its_code(tmp_path, monkeypatch):
    module = _load_golden_host()
    called = []
    monkeypatch.setattr(module, "run_traced_hub", lambda out: called.append(out) or 70)
    assert module.main(["--out", str(tmp_path), "--hub"]) == 70
    assert called == [tmp_path]


# --- real uvicorn, in a child ----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run_child(scenario: str, tmp_path: Path, *, timeout: float = 60.0) -> tuple[int, list[str], str]:
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(_ROOT / "src"), str(_HUB)]),
           "HOSTAPP_HOST": "127.0.0.1",
           "HOSTAPP_PORT": str(_free_port()),
           "PYTHONIOENCODING": "utf-8",
           "PYTHONUNBUFFERED": "1"}
    env.pop(hub.ENV_JSON_VAR, None)
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), scenario],
                          capture_output=True, encoding="utf-8", errors="replace", env=env,
                          cwd=str(tmp_path), timeout=timeout)
    return done.returncode, done.stdout.splitlines(), done.stderr


#: Everything after the server has stopped, in order.
_ROS_TEARDOWN = [
    "group1 executor shutdown",
    "destroy skill",
    "group0 executor shutdown",
    "destroy session",
    "client close",
    "ros-link executor shutdown",
    "destroy /emori/robot_link",
    "rclpy shutdown",
]


def test_sigterm_with_real_uvicorn_runs_the_whole_teardown_and_exits_zero(tmp_path):
    """uvicorn raises SIGTERM again as it returns. Had that reached the default handler, the
    process would have ended there, with the lines below missing and a non-zero status."""

    code, lines, stderr = _run_child("sigterm", tmp_path)
    assert code == 0, stderr
    assert lines[lines.index("lifespan start"):] == [
        "lifespan start", "serving", "lifespan stop", *_ROS_TEARDOWN, "returned 0", "threads []"]


def test_sigint_with_real_uvicorn_is_the_same_stop(tmp_path):
    code, lines, stderr = _run_child("sigint", tmp_path)
    assert code == 0, stderr
    assert lines[-len(_ROS_TEARDOWN) - 2:] == [*_ROS_TEARDOWN, "returned 0", "threads []"]


def test_a_dead_link_under_real_uvicorn_ends_the_process_with_the_restart_code(tmp_path):
    code, lines, stderr = _run_child("spin-dies", tmp_path)
    assert code == hub.EXIT_SPIN_DIED, stderr
    assert "ros-link has died" in stderr
    assert lines[lines.index("lifespan stop"):] == [
        "lifespan stop", *_ROS_TEARDOWN, "returned 70", "threads []"]


def test_a_stop_landing_in_the_moment_before_uvicorn_takes_over_still_stops_it(tmp_path):
    code, lines, stderr = _run_child("stop-before-serve", tmp_path)
    assert code == 0, stderr
    assert "lifespan stop" in lines or "lifespan start" not in lines
    assert lines[-len(_ROS_TEARDOWN) - 2:] == [*_ROS_TEARDOWN, "returned 0", "threads []"]


def test_a_signal_during_the_start_ends_the_process_without_serving(tmp_path):
    code, lines, stderr = _run_child("stop-during-build", tmp_path)
    assert code == 0, stderr
    assert "lifespan start" not in lines
    assert lines[-2:] == ["returned 0", "threads []"]
    assert "rclpy shutdown" in lines


def test_a_lifespan_that_fails_to_start_exits_with_uvicorns_code_after_the_teardown(tmp_path):
    code, lines, stderr = _run_child("lifespan-fails", tmp_path)
    assert code == 3, stderr
    assert lines[-len(_ROS_TEARDOWN) - 2:] == [*_ROS_TEARDOWN, "returned 3", "threads []"]


def test_a_port_already_taken_exits_with_uvicorns_code_after_the_teardown(tmp_path):
    """The port is the last line of defence between two hub units: the second binder exits."""

    code, lines, stderr = _run_child("port-taken", tmp_path)
    assert code == 1, stderr
    assert lines[-len(_ROS_TEARDOWN) - 2:] == [*_ROS_TEARDOWN, "returned 1", "threads []"]


def test_a_thread_that_will_not_finish_cannot_hold_the_process_open(tmp_path):
    """A stuck callback in a pool is a thread the interpreter waits for at exit, for ever."""

    started = time.monotonic()
    code, lines, stderr = _run_child("stuck-at-exit", tmp_path)
    assert code == 0, stderr
    assert "returned 0" in lines
    assert "the ROS teardown took longer" in stderr
    assert "stuck-worker" in stderr
    assert time.monotonic() - started < 60.0


def test_the_whole_entry_point_serves_the_real_app_and_stops(tmp_path):
    code, lines, stderr = _run_child("real-app", tmp_path)
    assert code == 0, stderr
    body = json.loads(next(line for line in lines if line.startswith("{")))
    assert body["runtime"] == "ros"
    assert body["status"] == "ok"
    assert body["robot"] is False, "the link is down, so the robot is not healthy"
    assert "snapshot-url" in lines, "the snapshot did not reach the server's constants"
    assert lines[-5:] == ["ros-link executor shutdown", "destroy /emori/robot_link",
                          "rclpy shutdown", "returned 0", "threads []"]


def _module_run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    full = {**os.environ, **env, "PYTHONPATH": os.pathsep.join([str(_ROOT / "src"), str(_HUB)])}
    return subprocess.run([sys.executable, "-m", "emori_hub.main"], capture_output=True, text=True,
                          env=full, cwd=str(tmp_path), timeout=60)


def test_the_module_refuses_a_bad_snapshot_and_exits_one(tmp_path):
    done = _module_run(tmp_path, **{hub.ENV_JSON_VAR: str(tmp_path / "absent.json")})
    assert done.returncode == hub.EXIT_FAILED
    assert "emori-hub: cannot read the environment snapshot" in done.stderr
    assert "Traceback" not in done.stderr


def test_the_module_says_what_is_missing_without_ros_and_exits_one(tmp_path):
    try:
        import rclpy  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - only in the hub's image
        pytest.skip("ROS is here")
    done = _module_run(tmp_path, **{hub.ENV_JSON_VAR: ""})
    assert done.returncode == hub.EXIT_FAILED
    assert "the hub needs ROS 2" in done.stdout + done.stderr


# --- the child -------------------------------------------------------------------------


class _Printing(list):
    """A list of events that also go to stdout, so that a process that dies has told."""

    def append(self, event: str) -> None:
        super().append(event)
        print(event, flush=True)


def _when_listening(port: int, then: Callable[[], Any]) -> None:
    def wait() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.02)
        then()

    threading.Thread(target=wait, name="child-when-listening", daemon=True).start()


def _small_app(events: list[str], overrides: dict[str, Any], *, lifespan_fails: bool) -> Any:
    from starlette.applications import Starlette

    @asynccontextmanager
    async def lifespan(_app: Any):
        events.append("lifespan start")
        if lifespan_fails:
            raise RuntimeError("the lifespan's startup failed")
        try:
            yield
        finally:
            events.append("lifespan stop")
            await asyncio.to_thread(overrides["robot_client"].inner.close)

    return Starlette(lifespan=lifespan)


def _child_real_app(port: int) -> int:
    from urllib.request import urlopen

    snapshot = Path("env.json")
    snapshot.write_text(json.dumps({
        "REACHY_BASE_URL": "http://snapshot.invalid:1",
        "HOSTAPP_DEMO": "1", "EMORI_GESTURE": "0", "EMORI_FIWARE": "0", "EMORI_RECORD": "0",
        "EMORI_VOICE_LINK": "0", "EMORI_ADDONS": "off", "EMORI_HEARTBEAT_S": "0",
        "EMORI_IDLE_AFTER_S": "0", "EMORI_DASH_DB": str(Path("metrics.db").resolve()),
        "EMORI_LANG_STATE_FILE": str(Path("lang-state").resolve()),
    }), encoding="utf-8")
    os.environ[hub.ENV_JSON_VAR] = str(snapshot.resolve())

    events = _Printing()
    ros = _Ros(events)
    assert not [name for name in sys.modules if name.startswith("emori_adapter.hostapp")]

    def fetch_then_stop() -> None:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=20) as answer:
            print(answer.read().decode("utf-8"), flush=True)
        if sys.modules["emori_adapter.hostapp.server"].REACHY_BASE_URL == "http://snapshot.invalid:1":
            print("snapshot-url", flush=True)
        signal.raise_signal(_TERM)

    hub.load_hub_bindings = lambda: ros.bindings
    _when_listening(port, fetch_then_stop)
    code = hub.main()
    print(f"returned {code}", flush=True)
    print(f"threads {_hub_threads()}", flush=True)
    return code


def _child(scenario: str) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    port = int(os.environ["HOSTAPP_PORT"])
    if scenario == "real-app":
        return _child_real_app(port)

    from emori_adapter.hostapp import server

    events = _Printing()
    ros = _Ros(events)
    overrides: dict[str, Any] = {}

    def build(given: dict[str, Any]) -> Any:
        overrides.update(given)
        if scenario == "stop-during-build":
            signal.raise_signal(_TERM)
        return _small_app(events, overrides, lifespan_fails=scenario == "lifespan-fails")

    def serve(app: Any, **kwargs: Any) -> None:
        if scenario == "stop-before-serve":
            signal.raise_signal(_TERM)
        server.serve(app, **kwargs)

    host = SimpleNamespace(REACHY_BASE_URL="http://127.0.0.1:9", _build_app=build, serve=serve)

    held_port = None
    if scenario == "port-taken":
        held_port = socket.socket()
        held_port.bind(("127.0.0.1", port))
        held_port.listen()

    def when_serving() -> None:
        events.append("serving")
        if scenario == "sigterm":
            signal.raise_signal(signal.SIGTERM)
        elif scenario == "sigint":
            signal.raise_signal(signal.SIGINT)
        elif scenario == "spin-dies":
            ros.link_executor.fail_next = SystemExit()
        elif scenario == "stuck-at-exit":
            threading.Thread(target=threading.Event().wait, name="stuck-worker",
                             daemon=False).start()
            signal.raise_signal(signal.SIGTERM)

    if scenario not in ("port-taken", "lifespan-fails", "stop-during-build", "stop-before-serve"):
        _when_listening(port, when_serving)

    def builder(name: str) -> Callable[[Any], Any]:
        return lambda _app: _GroupNode(events, name)

    code = hub.run_hub(ros.bindings, host, facades=[builder("session")], graph=[builder("skill")],
                       environ={}, wrap_client=lambda inner: _Wrapper(inner, events))
    if held_port is not None:
        held_port.close()
    print(f"returned {code}", flush=True)
    print(f"threads {_hub_threads()}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(_child(sys.argv[1]))
