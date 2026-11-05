"""What a process hosting the hostapp app relies on: ``serve``, the glue, the transport report.

The app runs unchanged under ``python -m emori_adapter.hostapp.server`` and under a ROS
process. These are the few seams the second one reaches for, checked on the first, which
needs no ROS at all.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import extensions, server
from emori_adapter.hostapp.extensions import ExtensionContext, HostExtension
from emori_adapter.hostapp.relay import RelayHub
from emori_adapter.robot.client import DryRunRobotClient, RobotClient
from emori_adapter.robot.tracing import TracingRobotClient


class _RosClient(DryRunRobotClient):
    """Stands in for the ROS client: it says what it is and answers ``robot_status``."""

    runtime = "ros"

    def robot_status(self) -> dict:
        return {"state": "error", "error": "no heartbeat", "first_unmatched": "get_status"}


class _Extension(HostExtension):
    def __init__(self) -> None:
        self.context = None

    def attach(self, ctx: ExtensionContext) -> None:
        self.context = ctx


def _app(monkeypatch, robot, **overrides):
    monkeypatch.setattr(server, "DEMO", True)
    return server._build_app(overrides={
        "robot_client": robot, "fiware_factory": None, "idle": False, "tts": None,
        "stt": None, "watchdog": False, "gesture": False, "on_session_end": None,
        "recorder": type("NullRec", (), {"write": staticmethod(lambda *a, **k: None)})(),
        **overrides})


@pytest.fixture()
def local_client():
    def build(app) -> TestClient:
        return TestClient(app, client=("127.0.0.1", 50000))
    return build


# --- serve ----------------------------------------------------------------------------


def test_serve_reads_the_environment_and_overrides_win(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setenv("HOSTAPP_HOST", "127.0.0.2")
    monkeypatch.setenv("HOSTAPP_PORT", "8123")
    monkeypatch.setenv("HOSTAPP_WS", "none")
    monkeypatch.delenv("EMORI_LOG_WIRE", raising=False)

    server.serve("the-app")
    server.serve("the-app", loop="asyncio", http="h11", port=9000)

    assert calls[0] == ("the-app", {"host": "127.0.0.2", "port": 8123, "ws": "none",
                                    "access_log": False})
    assert calls[1][1] == {"host": "127.0.0.2", "port": 9000, "ws": "none",
                           "access_log": False, "loop": "asyncio", "http": "h11"}


def test_main_starts_through_serve(monkeypatch):
    served = []
    monkeypatch.setattr(server, "setup_journal_logging", lambda: None)
    monkeypatch.setattr(server, "_build_app", lambda: "the-app")
    monkeypatch.setattr(server, "serve", lambda app, **kw: served.append((app, kw)))
    server.main()
    assert served == [("the-app", {})]


# --- the transport report -------------------------------------------------------------


@pytest.mark.parametrize("robot, runtime", [
    (DryRunRobotClient(), "http"),
    (TracingRobotClient(DryRunRobotClient()), "http"),
    (_RosClient(), "ros"),
    (TracingRobotClient(_RosClient()), "ros"),
])
def test_healthz_says_which_transport_reaches_the_robot(monkeypatch, local_client, robot, runtime):
    with local_client(_app(monkeypatch, robot)) as http:
        body = http.get("/healthz").json()
    assert body["runtime"] == runtime
    assert body["robot"] is True


def test_robot_status_answers_only_this_machine(monkeypatch):
    with TestClient(_app(monkeypatch, _RosClient()), client=("192.168.1.20", 50000)) as http:
        response = http.get("/api/robot/status")
    assert response.status_code == 403
    assert response.json() == {"error": "local_only"}


def test_robot_status_over_the_ros_link_names_what_is_missing(monkeypatch, local_client):
    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "hostapp")
    monkeypatch.setenv("EMORI_SKILL_TRANSPORT", "ROS_native")
    with local_client(_app(monkeypatch, TracingRobotClient(_RosClient()))) as http:
        body = http.get("/api/robot/status").json()
    assert body == {"state": "error", "error": "no heartbeat", "first_unmatched": "get_status",
                    "runtime": "ros", "motion_owner": "hostapp", "skill_transport": "ros_native"}


def test_robot_status_over_rest_reports_health_alone(monkeypatch, local_client):
    monkeypatch.delenv("EMORI_ROS_MOTION_OWNER", raising=False)
    monkeypatch.delenv("EMORI_SKILL_TRANSPORT", raising=False)
    with local_client(_app(monkeypatch, DryRunRobotClient())) as http:
        body = http.get("/api/robot/status").json()
    assert body == {"state": "running", "first_unmatched": "", "runtime": "http",
                    "motion_owner": "hostapp", "skill_transport": ""}


# --- the glue -------------------------------------------------------------------------


def test_the_glue_exports_what_the_routes_use(monkeypatch):
    speak = lambda text, lang: None  # noqa: E731
    app = _app(monkeypatch, DryRunRobotClient(), speak=speak)
    glue = app.state.glue
    assert glue.speak is speak
    assert glue.tts is None and glue.stt is None
    assert glue.mic_profile is not None
    assert glue.robot_idle() is True
    assert glue.live_session() is False
    assert callable(glue.set_language)
    assert glue.tt_store.snapshot()["rev"] >= 0
    menu, _tuning = glue.menu_for()
    assert menu is not None


# --- the seam -------------------------------------------------------------------------


def test_the_extension_context_gains_ros_with_the_api_version_unchanged():
    """Additive with a default, so an add-on built against this version keeps working."""

    assert extensions.API_VERSION == 1
    context = ExtensionContext(driver=None, gesture_ticker=None, lang_state=None,
                               mic_claim=lambda on: None, session_alive=lambda: False,
                               overrides={})
    assert context.ros is None


def test_the_app_hands_an_add_on_the_hosts_ros_ports(monkeypatch):
    ports, extension = object(), _Extension()
    _app(monkeypatch, DryRunRobotClient(), extension=extension, ros=ports)
    assert extension.context.ros is ports

    extension = _Extension()
    _app(monkeypatch, DryRunRobotClient(), extension=extension)
    assert extension.context.ros is None


def test_the_recorded_move_pair_is_part_of_the_protocol():
    """The player resolves these by name, so a client without them drops every recorded
    cue with no error. Being in the Protocol is what makes that a stated requirement."""

    for method in ("play_recorded_move", "stop_recorded_move"):
        assert method in vars(RobotClient)


# --- the relay tap --------------------------------------------------------------------


def test_the_tap_sees_what_the_hostapp_publishes_in_order():
    seen: list[str] = []
    hub = RelayHub(tap=seen.append)

    async def run() -> None:
        await hub.startup()
        assert hub.publish("a") is True
        assert hub.publish("b") is True
        await hub.shutdown()

    asyncio.run(run())
    assert seen == ["a", "b"]


def test_the_tap_runs_without_the_broadcast_and_can_be_set_later():
    seen: list[str] = []
    hub = RelayHub()
    assert hub.publish("before") is False
    hub.tap = seen.append
    assert hub.publish("after") is False  # no loop: nothing broadcast, the tap still told
    assert seen == ["after"]


def test_the_tap_does_not_carry_a_frame_a_client_sent():
    seen: list[str] = []
    hub = RelayHub(tap=seen.append)
    hub.publish("from a socket", exclude=object())
    assert seen == []


def test_a_failing_tap_does_not_reach_the_publisher(caplog):
    def broken(_message: str) -> None:
        raise RuntimeError("the tap fell over")

    hub = RelayHub(tap=broken)
    assert hub.publish("x") is False
    assert "relay tap failed" in caplog.text
