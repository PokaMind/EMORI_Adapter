"""``/emori/addon``: the add-on's status, polled twice a second, and what a bad hook costs.

The seam documents ``status()``, ``busy()`` and ``selection_reserved()`` as callable from any
thread and as not blocking. This node holds the add-on to that without trusting it: every
call is timed, a slow one is counted and shown on ``/diagnostics``, a raising one is read as
"no", and none of it can reach the session's motion, because the node never touches the
driver or the player and a hook stuck in a poll holds a facade executor thread and nothing
else.

No ROS is needed, and no add-on: the extension here is a stand-in with the seam's hooks.
"""

from __future__ import annotations

import ast
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

from emori_hub import addon_node as an  # noqa: E402
from emori_hub import facade_kit, motion_node  # noqa: E402
from emori_hub.addon_node import RosPorts, addon_builder, build_addon_node  # noqa: E402

from emori_adapter.hostapp.extensions import HostExtension  # noqa: E402
from tests import hub_node_fakes as fakes  # noqa: E402

_ADDON_SOURCE = Path(an.__file__)


class Extension(HostExtension):
    """A stand-in with the seam's hooks, each of which can be told to fail or to take time."""

    name = "stand-in"
    api_version = 1

    def __init__(self) -> None:
        self.mode = "idle"
        self.is_busy = False
        self.reserved = False
        self.extra: dict[str, Any] = {"enabled": {"a": True}}
        self.raises: set[str] = set()
        self.calls: list[str] = []
        self.take = 0.0
        self.clock: Any = None
        self.status_value: Any = "dict"

    def _enter(self, hook: str) -> None:
        self.calls.append(hook)
        if hook in self.raises:
            raise RuntimeError(f"{hook} failed")
        if self.clock is not None and self.take:
            self.clock[0] += self.take

    def status(self) -> Any:
        self._enter("status")
        return {"mode": self.mode, **self.extra} if self.status_value == "dict" \
            else self.status_value

    def busy(self) -> bool:
        self._enter("busy")
        return self.is_busy

    def selection_reserved(self) -> bool:
        self._enter("selection_reserved")
        return self.reserved


def _app(extension: Any) -> Any:
    return SimpleNamespace(state=SimpleNamespace(extension=extension))


def _facade(extension: Any = None, ports: Any = None, monotonic: Any = time.monotonic):
    extension = extension if extension is not None else Extension()
    published: list[an.AddonSnapshot] = []
    facade = an.AddonFacade(_app(extension), publish_status=published.append, ports=ports,
                            monotonic=monotonic)
    return facade, extension, published


def _node(extension: Any = None, ports: Any = None):
    bindings, nodes, groups = fakes.bindings({"AddonStatus": fakes.msg_type("AddonStatus")})
    app = _app(extension if extension is not None else Extension())
    return build_addon_node(app, bindings, ports=ports), app, nodes, groups


# --- the poll --------------------------------------------------------------------------


def test_the_first_poll_publishes_what_the_addon_says():
    facade, extension, published = _facade()
    extension.is_busy = True
    extension.reserved = True
    facade.poll()
    (snapshot,) = published
    assert (snapshot.installed, snapshot.name, snapshot.api_version) == (True, "stand-in", 1)
    assert (snapshot.busy, snapshot.selection_reserved, snapshot.mode) == (True, True, "idle")
    assert json.loads(snapshot.status_json) == {"mode": "idle", "enabled": {"a": True}}
    assert extension.calls == ["status", "busy", "selection_reserved"]


def test_the_status_is_the_addons_own_and_is_not_rewritten():
    facade, extension, published = _facade()
    extension.extra = {"enabled": {"x": False}, "note": "naïve", "n": 3}
    facade.poll()
    assert json.loads(published[0].status_json) == {
        "mode": "idle", "enabled": {"x": False}, "note": "naïve", "n": 3}


def test_only_a_change_is_published():
    facade, extension, published = _facade()
    for _ in range(4):
        facade.poll()
    assert len(published) == 1
    extension.mode = "running"
    facade.poll()
    extension.is_busy = True
    facade.poll()
    extension.reserved = True
    facade.poll()
    facade.poll()
    assert [(s.mode, s.busy, s.selection_reserved) for s in published] == [
        ("idle", False, False), ("running", False, False), ("running", True, False),
        ("running", True, True)]


def test_a_plain_host_extension_is_published_as_not_installed():
    facade, _extension, published = _facade(HostExtension())
    facade.poll()
    (snapshot,) = published
    assert (snapshot.installed, snapshot.name, snapshot.api_version) == (False, "", 1)
    assert (snapshot.busy, snapshot.selection_reserved, snapshot.mode,
            snapshot.status_json) == (False, False, "", "")
    assert facade.health().level == facade_kit.OK
    assert "no add-on" in facade.health().message


def test_a_status_that_is_not_a_mapping_is_published_as_none():
    facade, extension, published = _facade()
    extension.status_value = "not a dict"
    facade.poll()
    assert published[0].status_json == "" and published[0].mode == ""


def test_a_status_that_is_too_long_is_counted_and_not_published():
    facade, extension, published = _facade()
    extension.extra = {"blob": "x" * an.STATUS_JSON_MAX}
    facade.poll()
    assert published[0].status_json == "" and facade.stats()["oversize_status"] == 1
    assert facade.health().level == facade_kit.WARN


def test_a_status_json_cannot_hold_is_encoded_with_str():
    facade, extension, published = _facade()
    extension.extra = {"path": Path("a/b")}
    facade.poll()
    assert json.loads(published[0].status_json)["path"] in ("a/b", "a\\b")


# --- a hook that raises or takes its time ----------------------------------------------


@pytest.mark.parametrize("hook", ["status", "busy", "selection_reserved"])
def test_a_hook_that_raises_is_read_as_no_and_the_others_still_answer(hook):
    facade, extension, published = _facade()
    extension.is_busy = True
    extension.reserved = True
    extension.raises = {hook}
    facade.poll()
    (snapshot,) = published
    assert snapshot.busy is (hook != "busy")
    assert snapshot.selection_reserved is (hook != "selection_reserved")
    assert (snapshot.mode == "") is (hook == "status")
    assert facade.stats()["hook_errors"] == 1
    assert facade.health().level == facade_kit.WARN


def test_every_hook_raising_is_still_a_publication_and_never_a_raise():
    facade, extension, published = _facade()
    extension.raises = {"status", "busy", "selection_reserved"}
    facade.poll()
    facade.poll()
    assert len(published) == 1 and facade.stats()["hook_errors"] == 6


def test_a_slow_hook_is_counted_and_the_longest_call_kept():
    now = [0.0]
    facade, extension, published = _facade(monotonic=lambda: now[0])
    extension.clock, extension.take = now, 0.3
    facade.poll()
    stats = facade.stats()
    assert stats["slow_calls"] == 3
    assert stats["longest_call_s"] == pytest.approx(0.3)
    health = facade.health()
    assert health.level == facade_kit.WARN and "must not block" in health.message
    assert len(published) == 1   # slow, but the answer still counts


def test_a_quick_hook_is_not_counted():
    now = [0.0]
    facade, extension, _published = _facade(monotonic=lambda: now[0])
    extension.clock, extension.take = now, 0.01
    facade.poll()
    assert facade.stats()["slow_calls"] == 0


def test_a_late_poll_is_counted():
    now = [0.0]
    facade, _extension, _published = _facade(monotonic=lambda: now[0])
    for at in (0.0, 0.5, 1.0, 4.0):
        now[0] = at
        facade.poll()
    assert facade.stats()["late_polls"] == 1


def test_a_hook_stuck_in_a_poll_holds_nothing_the_motion_state_needs():
    """The hook is the add-on's, and a stuck one is the add-on's fault. It costs the executor
    thread it is on and nothing else: the motion facade's tick finishes while it is stuck."""

    extension = Extension()
    release = threading.Event()
    inside = threading.Event()

    def stuck() -> bool:
        inside.set()
        release.wait(10.0)
        return False

    extension.busy = stuck  # type: ignore[method-assign]
    facade, _extension, _published = _facade(extension)
    poller = threading.Thread(target=facade.poll, daemon=True)
    poller.start()
    try:
        assert inside.wait(5.0)
        driver = SimpleNamespace(player=SimpleNamespace(busy=False, _q=SimpleNamespace(queue=[])),
                                 _legacy_motion_enabled=True, _legacy_motion_generation=0,
                                 motion_owner="hostapp")
        motion = motion_node.MotionFacade(SimpleNamespace(state=SimpleNamespace(driver=driver)),
                                          publish_player=lambda snapshot: None,
                                          submit_enabled=False)
        started = time.monotonic()
        motion.tick()
        assert time.monotonic() - started < 1.0 and motion.published == 1
    finally:
        release.set()
        poller.join(5.0)


# --- the ports the addon is handed -----------------------------------------------------


def test_the_ports_name_the_node_and_ask_for_a_publication_without_calling_the_addon():
    ports = RosPorts()
    facade, extension, published = _facade(ports=ports)
    facade.poll()
    facade.poll()
    assert len(published) == 1
    calls_before = list(extension.calls)

    ports.publish_status()
    assert extension.calls == calls_before, "asking for a publication called into the add-on"
    facade.poll()
    facade.poll()
    assert len(published) == 2, "one request is one publication"
    assert ports.node_name == "/emori/addon"


def test_the_ports_publish_request_never_blocks():
    ports = RosPorts()
    started = time.monotonic()
    for _ in range(10000):
        ports.publish_status()
    assert time.monotonic() - started < 1.0


def test_a_real_app_hands_the_addon_the_ports_it_asks_for_publication_with(monkeypatch, tmp_path):
    """Through ``_build_app``: ``ExtensionContext.ros`` is the object the node answers."""

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "lang.json"))
    monkeypatch.setattr(server, "DEMO", False)
    seen: dict[str, Any] = {}

    class Attaching(Extension):
        def attach(self, ctx: Any) -> None:
            seen["ros"] = ctx.ros

    ports = RosPorts()
    extension = Attaching()
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": SimpleNamespace(busy=False,
                                                                       submit=lambda *a, **k: True),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": extension, "ros": ports, "tts": None, "stt": None})
    assert seen["ros"] is ports
    assert app.state.extension is extension

    bindings, _nodes, _groups = fakes.bindings({"AddonStatus": fakes.msg_type("AddonStatus")})
    node = build_addon_node(app, bindings, ports=ports)
    node.timers[0].fire()
    extension.mode = "changed"
    seen["ros"].publish_status()
    node.timers[0].fire()
    published = node.publishers["/emori/addon/status"].published
    assert [message.mode for message in published] == ["idle", "changed"]


# --- the node --------------------------------------------------------------------------


def test_the_node_is_emori_addon_with_a_latched_status_topic_polled_at_two_hertz():
    node, _app_, nodes, groups = _node()
    assert nodes == [node] and (node.name, node.namespace) == ("addon", "/emori")
    assert node.publishers["/emori/addon/status"].qos == "latched"
    assert [timer.period for timer in node.timers] == [0.5]
    assert node.services == {} and len(groups.made) == 1


def test_the_message_carries_every_field_of_the_interface():
    node, _app_, _nodes, _groups = _node()
    node.timers[0].fire()
    (message,) = node.publishers["/emori/addon/status"].published
    assert (message.installed, message.name, message.api_version, message.mode) == (
        True, "stand-in", 1, "idle")
    assert json.loads(message.status_json)["enabled"] == {"a": True}
    assert message.header.stamp.sec == 0


def test_a_builder_for_the_facade_group_publishes_for_the_ports_it_was_given():
    ports = RosPorts()
    bindings, nodes, _groups = fakes.bindings({"AddonStatus": fakes.msg_type("AddonStatus")})
    node = addon_builder(ports, bindings)(_app(Extension()))
    assert nodes == [node] and node.facade._ports is ports


def test_the_node_registers_its_counters_for_diagnostics():
    node, app, _nodes, _groups = _node()
    node.timers[0].fire()
    health = facade_kit.diagnostic_sources(app)["addon"]()
    assert health.level == facade_kit.OK and health.values["polls"] == 1


# --- not on the session's path ---------------------------------------------------------


def test_this_module_never_names_the_driver_the_player_or_a_lock():
    """It cannot cost the session what it cannot reach."""

    tree = ast.parse(_ADDON_SOURCE.read_text(encoding="utf-8"))
    names = {node.attr if isinstance(node, ast.Attribute) else node.id
             for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Name))}
    assert not names & {"driver", "player", "submit", "submit_legacy_motion", "Lock", "RLock",
                        "Condition", "Event", "Queue", "tracker", "cue"}
