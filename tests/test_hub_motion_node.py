"""``/emori/motion``: the player's state, a gated batch, and what it must never cost a session.

A session's motion runs ``POST /api/cycle`` -> ``LiveDriver.on_utterance`` -> the cue
scheduler -> the player -> the robot client. The first half of this file pins that this node
is not on that path: its tick takes no lock of the player's or the driver's (the test holds
them), a batch that is stuck inside the player holds nothing the tick needs, the core never
imports the hub, and the module holds no lock of its own. The second half is the node: the
state it publishes, the batch it takes and everything it refuses.

No ROS is needed. The node is a fake that an executor's calls are made on directly.
"""

from __future__ import annotations

import ast
import math
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

from emori_hub import facade_kit  # noqa: E402
from emori_hub import motion_node as mn  # noqa: E402
from emori_hub.motion_node import (  # noqa: E402
    MotionFacade,
    build_motion_node,
    intent_from_message,
)

from emori_adapter.hostapp.driver import LiveDriver, RobotPlayer  # noqa: E402
from emori_adapter.robot.client import DryRunRobotClient  # noqa: E402
from tests import hub_node_fakes as fakes  # noqa: E402

_MOTION_SOURCE = Path(mn.__file__)

Intent = fakes.msg_type("MotionIntent")
SubmitBatch = fakes.srv_type("SubmitMotionBatch")


def _types() -> dict[str, Any]:
    return {"MotionPlanEvent": fakes.msg_type("MotionPlanEvent"),
            "PlayerState": fakes.msg_type("PlayerState"), "SubmitMotionBatch": SubmitBatch}


def _intent(**fields: Any) -> Any:
    message = Intent()
    message.kind = "goto"
    message.duration = 0.5
    for key, value in fields.items():
        setattr(message, key, value)
    return message


def _request(*intents: Any, drop_if_busy: bool = False) -> Any:
    request = SubmitBatch.Request()
    request.intents = list(intents)
    request.drop_if_busy = drop_if_busy
    return request


def _wait_until(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    return predicate()


# --- fakes and real objects ------------------------------------------------------------


class FakeDriver:
    """What the facade reads of a driver, and the one entry it submits through."""

    def __init__(self) -> None:
        self.player = SimpleNamespace(busy=False, _q=SimpleNamespace(queue=[]))
        self._legacy_motion_enabled = True
        self._legacy_motion_generation = 0
        self.motion_owner = "hostapp"
        self.reserved = False
        self.tracker = SimpleNamespace(active=False)
        self.cue = SimpleNamespace(generation=0)
        self.calls: list[dict[str, Any]] = []
        self.result = True
        self.fail = False
        self.inside = threading.Event()
        self.release = threading.Event()
        self.block = False

    def head_reserved(self) -> bool:
        return self.reserved

    def submit_legacy_motion(self, intents: list, *, drop_if_busy: bool = False,
                             guard: Any = None, yields: Any = None) -> bool:
        if self.fail:
            raise RuntimeError("the player fell over")
        self.calls.append({"intents": intents, "drop_if_busy": drop_if_busy, "guard": guard,
                           "yields": yields})
        if self.block:
            self.inside.set()
            self.release.wait(10.0)
        return self.result


class _SilentClient:
    """A robot that is never healthy, so a player never leaves its boot and keeps its queue."""

    def health(self) -> bool:
        return False

    def set_motor_mode(self, mode: str) -> None:
        pass

    def wake_up(self) -> None:
        pass

    def apply(self, intent: Any) -> None:
        pass


class _Voice:
    available = True
    onset_count = 7


def _app(driver: Any) -> Any:
    glue = SimpleNamespace(robot_idle=lambda: True, landing_idle=lambda: True)
    return SimpleNamespace(state=SimpleNamespace(driver=driver, glue=glue))


def _facade(driver: Any = None, *, enabled: bool = True, monotonic: Any = time.monotonic):
    driver = driver if driver is not None else FakeDriver()
    published: list[mn.PlayerSnapshot] = []
    facade = MotionFacade(_app(driver), publish_player=published.append,
                          submit_enabled=enabled, monotonic=monotonic)
    return facade, driver, published


@pytest.fixture()
def real():
    """A real player on a robot that never answers, a real driver on it, and the facade."""

    player = RobotPlayer(_SilentClient(), voice=_Voice())
    driver = LiveDriver(DryRunRobotClient(), player=player, fiware_factory=None)
    facade, _driver, published = _facade(driver)
    try:
        yield SimpleNamespace(player=player, driver=driver, facade=facade, published=published)
    finally:
        player.stop()
        player.join(timeout_s=3.0)


# --- not on the session's path ---------------------------------------------------------


def _hold(lock: Any, held: threading.Event, release: threading.Event) -> threading.Thread:
    def run() -> None:
        with lock:
            held.set()
            release.wait(10.0)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.wait(5.0)
    return thread


def test_the_tick_takes_no_lock_of_the_player_or_the_driver(real):
    """The player's state lock, its queue's lock and the driver's motion lock are all held
    by another thread, as they are during a submit. The tick still finishes, and still reads."""

    release = threading.Event()
    holders = [_hold(lock, threading.Event(), release) for lock in (
        real.player._motion_state_lock, real.player._q.mutex, real.driver._legacy_motion_lock)]
    done = threading.Event()
    threading.Thread(target=lambda: (real.facade.tick(), done.set()), daemon=True).start()
    try:
        assert done.wait(2.0), "the state tick waited for a lock the session's motion takes"
        assert len(real.published) == 1
    finally:
        release.set()
        for holder in holders:
            holder.join(5.0)


def test_the_tick_does_not_wait_for_a_batch_stuck_in_the_player():
    driver = FakeDriver()
    driver.block = True
    facade, _driver, published = _facade(driver)
    served = threading.Thread(
        target=lambda: facade.submit_batch(_request(_intent()), SubmitBatch.Response()),
        daemon=True)
    served.start()
    try:
        assert driver.inside.wait(5.0)
        started = time.monotonic()
        facade.tick()
        assert time.monotonic() - started < 1.0
        assert len(published) == 1
    finally:
        driver.release.set()
        served.join(5.0)


def test_the_tick_reads_only_attributes_and_never_calls_into_the_player():
    """The names the state read is allowed to touch, from the source. A call on the player or
    a read through a lock-taking property here would be a lock on the session's path."""

    tree = ast.parse(_MOTION_SOURCE.read_text(encoding="utf-8"))
    reading = {"read", "_get", "tick", "_tick"}
    forbidden = {"_motion_state_lock", "_legacy_motion_lock", "mutex", "qsize", "submit",
                 "submit_legacy_motion", "get", "put", "put_nowait", "acquire", "snapshot",
                 "status", "health"}
    seen: set[str] = set()
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef) and function.name in reading:
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute):
                    seen.add(node.attr)
                    assert node.attr not in forbidden, (
                        f"{function.name} touches {node.attr}, which the tick must not")
    assert {"busy", "_q", "queue", "_legacy_motion_enabled", "_legacy_motion_generation",
            "motion_owner"} <= seen


def test_the_core_never_imports_the_hub():
    """Nothing on the session's path can call a facade if the core cannot name one."""

    offenders = [str(path.relative_to(_ROOT)) for path in (_ROOT / "src").rglob("*.py")
                 if "emori_hub" in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_this_module_holds_no_lock_at_all():
    """The state tick and the batch service share nothing they could wait on each other for."""

    tree = ast.parse(_MOTION_SOURCE.read_text(encoding="utf-8"))
    names = {node.attr if isinstance(node, ast.Attribute) else node.id
             for node in ast.walk(tree) if isinstance(node, (ast.Attribute, ast.Name))}
    assert not names & {"Lock", "RLock", "Condition", "Semaphore", "Event", "Queue"}


# --- the player's state ----------------------------------------------------------------


def test_the_reader_reads_the_real_player_and_driver(real):
    first = real.facade.reader.read()
    assert (first.busy, first.queue_depth, first.legacy_motion_enabled, first.generation,
            first.motion_owner) == (False, 0, True, 0, "hostapp")

    assert real.driver.submit_legacy_motion([_core_intent()])
    real.driver.set_legacy_motion_enabled(False)

    second = real.facade.reader.read()
    assert second.busy is True
    assert second.queue_depth == 1
    assert second.legacy_motion_enabled is False
    assert second.generation == 1
    assert real.facade.reader.unreadable == 0


def _core_intent(**fields: Any) -> Any:
    from emori_adapter.models import MotionIntent

    return MotionIntent(kind="goto", duration=0.5, **fields)


def test_a_value_that_cannot_be_read_is_counted_and_the_rest_still_published():
    driver = FakeDriver()
    driver.player = SimpleNamespace()          # no busy, no queue
    facade, _driver, published = _facade(driver)
    facade.tick()
    assert published[0].legacy_motion_enabled is True
    assert facade.reader.unreadable == 2
    assert facade.stats()["unreadable_values"] == 2


def test_the_state_is_published_on_change_and_not_otherwise():
    facade, driver, published = _facade()
    facade.tick()
    facade.tick()
    facade.tick()
    assert len(published) == 1
    driver.player.busy = True
    facade.tick()
    driver.player._q.queue.extend([1, 2])
    facade.tick()
    driver._legacy_motion_enabled = False
    facade.tick()
    driver._legacy_motion_generation = 4
    facade.tick()
    driver.motion_owner = "ros"
    facade.tick()
    assert [(s.busy, s.queue_depth, s.legacy_motion_enabled, s.generation, s.motion_owner)
            for s in published] == [
        (False, 0, True, 0, "hostapp"), (True, 0, True, 0, "hostapp"),
        (True, 2, True, 0, "hostapp"), (True, 2, False, 0, "hostapp"),
        (True, 2, False, 4, "hostapp"), (True, 2, False, 4, "ros")]


def test_a_failing_publish_is_counted_and_does_not_end_the_tick_for_good(caplog):
    driver = FakeDriver()
    calls = []

    def publish(snapshot: Any) -> None:
        calls.append(snapshot)
        if len(calls) == 1:
            raise RuntimeError("no publisher")

    facade = MotionFacade(_app(driver), publish_player=publish, submit_enabled=False)
    facade.tick()
    assert facade.guard.errors == 1
    facade.tick()          # the failed one was not recorded as sent, so it goes again
    assert len(calls) == 2 and facade.published == 1


def test_a_late_tick_is_counted():
    now = [0.0]
    facade, _driver, _published = _facade(monotonic=lambda: now[0])
    for at in (0.0, 0.2, 0.4, 2.0, 2.2):
        now[0] = at
        facade.tick()
    stats = facade.stats()
    assert stats["late_ticks"] == 1
    assert stats["longest_tick_gap_s"] == pytest.approx(1.6)
    assert facade.health().level == facade_kit.WARN
    assert "Motion is not affected" in facade.health().message


def test_the_message_is_filled_within_its_widths():
    fill = mn.fill_player(fakes.msg_type("PlayerState")(), mn.PlayerSnapshot(
        busy=True, queue_depth=900, legacy_motion_enabled=False, generation=2**32 + 5,
        motion_owner="ros"))
    assert (fill.busy, fill.queue_depth, fill.generation, fill.motion_owner) == (
        True, 255, 5, "ros")


# --- the node --------------------------------------------------------------------------


def _node(driver: Any = None, environ: Any = None):
    bindings, nodes, groups = fakes.bindings(_types())
    app = _app(driver if driver is not None else FakeDriver())
    node = build_motion_node(app, bindings, environ=environ if environ is not None else {})
    return node, app, nodes, groups


def test_the_node_is_emori_motion_with_a_latched_player_topic_and_a_service():
    node, _app_, nodes, _groups = _node()
    assert nodes == [node]
    assert (node.name, node.namespace) == ("motion", "/emori")
    publisher = node.publishers["/emori/motion/player"]
    assert publisher.qos == "latched"
    assert node.publishers["/emori/motion/plan_events"].qos == "default"
    assert list(node.services) == ["/emori/motion/submit_batch"]
    assert [timer.period for timer in node.timers] == [0.2]


def test_the_tick_and_the_service_are_on_different_callback_groups():
    node, _app_, _nodes, groups = _node()
    assert node.timers[0].group is not node.services["/emori/motion/submit_batch"].group
    assert len(groups.made) == 2


def test_the_tick_publishes_a_stamped_message_and_late_joiners_get_it_from_the_latch():
    node, _app_, _nodes, _groups = _node()
    node.timers[0].fire()
    (message,) = node.publishers["/emori/motion/player"].published
    assert message.header.stamp.sec == 0 and message.motion_owner == "hostapp"
    assert message.legacy_motion_enabled is True


def test_the_node_registers_its_counters_for_diagnostics():
    node, app, _nodes, _groups = _node()
    source = facade_kit.diagnostic_sources(app.state)["motion"] if False else \
        facade_kit.diagnostic_sources(app)["motion"]
    health = source()
    assert health.level == facade_kit.OK
    assert health.values["late_ticks"] == 0 and node.facade.stats()["ticks"] == 0


# --- the batch -------------------------------------------------------------------------


def _call(node: Any, *intents: Any, drop_if_busy: bool = False) -> Any:
    return node.services["/emori/motion/submit_batch"].call(
        intents=list(intents), drop_if_busy=drop_if_busy)


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), (" Yes ", True), ("on", True),
    ("", False), ("0", False), ("tru", False), ("2", False)])
def test_only_a_spelled_out_yes_opens_the_batch_service(value, expected):
    assert mn.motion_submit_enabled({mn.MOTION_SUBMIT_VAR: value}) is expected


def test_the_batch_is_refused_unless_the_hub_was_started_with_it_on():
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver)
    response = _call(node, _intent())
    assert (response.accepted, response.error_code) == (False, "disabled")
    assert driver.calls == []
    assert node.facade.stats()["refused_disabled"] == 1


def test_a_refused_batch_has_read_nothing_of_the_player():
    class Untouchable(FakeDriver):
        def __getattribute__(self, name: str) -> Any:
            if name in ("player", "tracker", "cue", "submit_legacy_motion"):
                raise AssertionError(f"a disabled service read {name}")
            return super().__getattribute__(name)

    facade, _d, _p = _facade(Untouchable(), enabled=False)
    response = facade.submit_batch(_request(_intent()), SubmitBatch.Response())
    assert response.error_code == "disabled"


def test_a_batch_is_refused_for_as_long_as_a_session_is_open():
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    driver.tracker.active = True
    response = _call(node, _intent())
    assert (response.accepted, response.error_code) == (False, "session_active")
    assert driver.calls == []
    driver.tracker.active = False
    assert _call(node, _intent()).accepted is True


def test_a_tracker_that_cannot_be_read_counts_as_a_session():
    driver = FakeDriver()
    driver.tracker = None
    facade, _d, _p = _facade(driver)
    assert facade.submit_batch(_request(_intent()), SubmitBatch.Response()).error_code == \
        "session_active"


def test_an_accepted_batch_goes_in_through_the_players_own_entry():
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    response = _call(
        node,
        _intent(has_head=True, x_mm=1.0, y_mm=2.0, z_mm=3.0, roll_deg=4.0, pitch_deg=5.0,
                yaw_deg=6.0, has_antennas=True, antenna_left_rad=0.25, antenna_right_rad=-0.5,
                has_body_yaw=True, body_yaw_rad=0.125, interpolation="linear",
                reason="from the graph", speech=True, pace="voice", antenna_voice=True,
                supersede=True, tint="levity"),
        _intent(kind="play_recorded", move="curious1", duration=2.0),
        drop_if_busy=True)
    assert (response.accepted, response.error_code) == (True, "")
    (call,) = driver.calls
    assert call["drop_if_busy"] is True
    first, second = call["intents"]
    assert (first.kind, first.duration, first.interpolation) == ("goto", 0.5, "linear")
    assert (first.head.x, first.head.z, first.head.pitch, first.head.yaw) == (1.0, 3.0, 5.0, 6.0)
    assert first.antennas == (0.25, -0.5) and first.body_yaw == 0.125
    assert (first.speech, first.pace, first.antenna_voice, first.supersede, first.tint) == (
        True, "voice", True, True, "levity")
    assert first.reason == "from the graph"
    assert (second.kind, second.move) == ("play_recorded", "curious1")
    assert node.facade.stats()["batches_accepted"] == 1


def test_a_part_with_its_flag_off_is_left_uncommanded():
    intent = intent_from_message(_intent(x_mm=9.0, antenna_left_rad=1.0, body_yaw_rad=1.0,
                                         interpolation="", pace=""))
    assert intent.head is None and intent.antennas is None and intent.body_yaw is None
    assert (intent.interpolation, intent.pace, intent.move) == ("minjerk", "fixed", None)


def test_a_zero_duration_is_the_models_default():
    assert intent_from_message(_intent(duration=0.0)).duration == pytest.approx(0.6)


def test_a_batch_gives_way_to_a_sentence_as_a_cycles_motion_does():
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    _call(node, _intent())
    yields = driver.calls[0]["yields"]
    assert yields() is False
    driver.cue.generation += 1
    assert yields() is True


def test_without_motion_sync_a_batch_still_gives_way_to_a_session(monkeypatch):
    monkeypatch.setenv("EMORI_MOTION_SYNC", "0")
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    _call(node, _intent())
    yields = driver.calls[0]["yields"]
    assert yields() is False
    driver.tracker.active = True
    assert yields() is True


def test_with_motion_sync_a_batch_gives_way_to_a_session_too(monkeypatch):
    monkeypatch.delenv("EMORI_MOTION_SYNC", raising=False)
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    _call(node, _intent())
    yields = driver.calls[0]["yields"]
    assert yields() is False
    driver.tracker.active = True
    assert yields() is True


@pytest.mark.parametrize("owner", ["ros", "none"])
def test_a_batch_is_refused_when_the_graph_or_nobody_owns_motion(owner):
    driver = FakeDriver()
    driver.motion_owner = owner
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    answer = _call(node, _intent())
    assert (answer.accepted, answer.error_code) == (False, "not_motion_owner")
    assert driver.calls == []


@pytest.mark.parametrize("robot, landing", [(False, True), (True, False)])
def test_a_batch_is_refused_while_anybody_is_mid_way_to_a_session(robot, landing):
    driver = FakeDriver()
    node, app, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    app.state.glue.robot_idle = lambda: robot
    app.state.glue.landing_idle = lambda: landing
    answer = _call(node, _intent())
    assert (answer.accepted, answer.error_code) == (False, "busy")
    assert driver.calls == []


def test_a_batch_is_refused_while_the_head_is_an_input_device():
    driver = FakeDriver()
    driver.reserved = True
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    answer = _call(node, _intent())
    assert (answer.accepted, answer.error_code) == (False, "head_reserved")
    assert driver.calls == []


@pytest.mark.parametrize("intents, code", [
    ([], "empty"),
    ([lambda: _intent()] * 33, "too_many"),
    ([lambda: _intent(kind="play_sound")], "invalid_intent"),
    ([lambda: _intent(kind="nonsense")], "invalid_intent"),
    ([lambda: _intent(duration=-1.0)], "invalid_intent"),
    ([lambda: _intent(duration=31.0)], "invalid_intent"),
    ([lambda: _intent(has_head=True, x_mm=math.nan)], "invalid_intent"),
    ([lambda: _intent(has_antennas=True, antenna_left_rad=math.inf)], "invalid_intent"),
    ([lambda: _intent(kind="play_recorded")], "invalid_intent"),
    ([lambda: _intent(interpolation="wobbly")], "invalid_intent"),
    ([lambda: _intent(pace="sometimes")], "invalid_intent"),
])
def test_a_batch_that_cannot_be_played_is_refused_whole(intents, code):
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    response = _call(node, *[make() for make in intents])
    assert (response.accepted, response.error_code) == (False, code)
    assert driver.calls == []


def test_one_bad_intent_refuses_the_batch_it_is_in():
    driver = FakeDriver()
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    response = _call(node, _intent(), _intent(duration=99.0))
    assert response.error_code == "invalid_intent" and driver.calls == []


def test_a_player_that_does_not_take_it_is_refused_and_says_why():
    driver = FakeDriver()
    driver.result = False
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    assert _call(node, _intent()).error_code == "refused"
    driver._legacy_motion_enabled = False
    assert _call(node, _intent()).error_code == "motion_disabled"
    assert node.facade.stats()["batches_refused"] == 2


def test_a_driver_without_a_player_refuses():
    driver = FakeDriver()
    driver.player = None
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    assert _call(node, _intent()).error_code == "no_player"


def test_a_player_that_raises_is_an_answer_and_not_a_dead_executor():
    driver = FakeDriver()
    driver.fail = True
    node, _app_, _nodes, _groups = _node(driver, {mn.MOTION_SUBMIT_VAR: "1"})
    response = _call(node, _intent())
    assert (response.accepted, response.error_code) == (False, "internal")
    assert node.facade.guard.errors == 1


def test_every_error_code_the_service_gives_is_listed():
    tree = ast.parse(_MOTION_SOURCE.read_text(encoding="utf-8"))
    (submit,) = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "_submit"]
    words = {node.value for node in ast.walk(submit)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert set(mn.ERROR_CODES) - {"internal"} <= words


def test_the_documented_codes_are_in_the_interface_file():
    text = (_ROOT / "ros2_ws" / "src" / "open" / "emori_msgs" / "srv"
            / "SubmitMotionBatch.srv").read_text(encoding="utf-8")
    assert "EMORI_HUB_MOTION_SUBMIT" in text


# --- the voice mark and the guard, on the real player ----------------------------------


def _speech_request() -> Any:
    return _request(_intent(speech=True, pace="voice", duration=0.5))


def test_a_speech_batch_takes_the_voice_mark_and_the_ownership_guard_of_the_real_player(real):
    real.facade.submit_enabled = True
    before = time.monotonic()
    response = real.facade.submit_batch(_speech_request(), SubmitBatch.Response())
    assert response.accepted is True, response.error_code
    (queued,) = list(real.player._q.queue)
    intents, guard, _plan, mark, speech, yields = queued
    assert speech is True and len(intents) == 1
    assert mark is not None and mark[0] >= before and mark[1] == _Voice.onset_count
    assert yields is not None and yields() is False
    assert guard() is True
    real.driver.set_legacy_motion_enabled(False)
    assert guard() is False, "the batch outlived the driver's revocation of legacy motion"


def test_a_batch_that_is_not_speech_carries_no_voice_mark(real):
    real.facade.submit_enabled = True
    real.facade.submit_batch(_request(_intent()), SubmitBatch.Response())
    (queued,) = list(real.player._q.queue)
    assert queued[3] is None and queued[4] is False


def test_a_full_player_refuses_the_batch(real):
    real.facade.submit_enabled = True
    outcomes = [real.facade.submit_batch(_request(_intent()), SubmitBatch.Response()).error_code
                for _ in range(14)]
    assert outcomes[:12] == [""] * 12
    assert set(outcomes[12:]) == {"refused"}


# --- the plan events -------------------------------------------------------------------


_PLAN_EVENTS = "/emori/motion/plan_events"


def _driver_on_a_dry_robot(monkeypatch: Any) -> LiveDriver:
    monkeypatch.setattr(RobotPlayer, "_WAKE_SETTLE_S", 0.0)
    return LiveDriver(DryRunRobotClient(), fiware_factory=None)


def test_a_sentence_from_the_real_driver_fills_every_field_of_the_message(monkeypatch):
    driver = _driver_on_a_dry_robot(monkeypatch)
    node, _app_, _nodes, _groups = _node(driver)
    try:
        driver.on_utterance({"text": "One plain sentence about the plan.", "seq": 41,
                             "cue_category": "tension"})
        assert _wait_until(lambda: not driver.player.busy, timeout=8.0)
        node.timers[0].fire()
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()

    planned, started, ended = node.publishers[_PLAN_EVENTS].published
    for message in (planned, started, ended):
        assert message.plan_id == planned.plan_id != ""
        assert message.seq == 41
        assert message.family == "tension"
        assert message.family_authored == "tension"
        assert message.family_source == planned.family_source != ""
        assert message.slug == planned.slug != ""
    assert (planned.started, planned.ended, planned.outcome) == (False, False, "")
    assert planned.first_apply_delay_s == -1.0
    assert (started.started, started.ended, started.outcome) == (True, False, "")
    assert started.first_apply_delay_s >= 0.0
    assert (ended.started, ended.ended, ended.outcome) == (True, True, "finished")
    assert ended.first_apply_delay_s == started.first_apply_delay_s
    assert ended.skip_reason == ""
    assert node.facade.stats()["plan_events_published"] == 3


def test_a_skipped_sentence_is_one_message_with_its_reason(monkeypatch):
    driver = _driver_on_a_dry_robot(monkeypatch)
    node, _app_, _nodes, _groups = _node(driver)
    try:
        driver.set_head_reserved(lambda: True)
        driver.on_utterance({"text": "Said while the head is an input.", "seq": 7,
                             "cue_category": "levity"})
        node.timers[0].fire()
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()

    (message,) = node.publishers[_PLAN_EVENTS].published
    assert (message.started, message.ended, message.outcome, message.skip_reason) == (
        False, True, "skipped", "head_reserved")
    assert (message.family, message.family_authored) == ("", "levity")
    assert message.first_apply_delay_s == -1.0


def test_what_the_queue_could_not_hold_is_counted_and_shown_and_the_rest_goes_out(monkeypatch):
    driver = _driver_on_a_dry_robot(monkeypatch)
    node, app, _nodes, _groups = _node(driver)
    try:
        driver.set_head_reserved(lambda: True)
        capacity = node.facade.plans.capacity
        for seq in range(1, capacity + 6):
            driver.on_utterance({"text": f"Sentence number {seq}.", "seq": seq})
        node.timers[0].fire()
    finally:
        driver.shutdown(timeout_s=1.0)
        driver.player.stop()
        driver.player.join()

    assert len(node.publishers[_PLAN_EVENTS].published) == capacity
    stats = node.facade.stats()
    assert stats["plan_events_dropped"] == 5
    assert stats["plan_events_capacity"] == capacity
    health = facade_kit.diagnostic_sources(app)["motion"]()
    assert health.level == facade_kit.WARN
    assert "5 plan events" in health.message
    assert health.values["plan_events_dropped"] == 5


def test_a_publish_that_fails_costs_that_message_and_not_the_tick_or_the_rest():
    stream = _stream_with_two_skips()
    driver = FakeDriver()
    driver.plan_events = stream.stream
    sent: list[Any] = []

    def publish(step: Any) -> None:
        sent.append(step)
        if len(sent) == 1:
            raise RuntimeError("no publisher")

    facade = MotionFacade(_app(driver), publish_player=lambda _s: None, submit_enabled=False,
                          publish_plan_event=publish)
    stream.put_two()
    facade.tick()
    assert len(sent) == 2
    stats = facade.stats()
    assert (stats["plan_events_failed"], stats["plan_events_published"]) == (1, 1)
    assert stats["callback_errors"] == 1
    assert facade.health().level == facade_kit.WARN


def _stream_with_two_skips() -> Any:
    from emori_adapter.plan_events import PLANNED, PlanEventStream

    stream = PlanEventStream()

    def put_two() -> None:
        for plan in (1, 2):
            stream.put((PLANNED, plan, plan, "", "", "", "head_reserved", 0.0, "head_reserved"))

    return SimpleNamespace(stream=stream, put_two=put_two)


def test_a_driver_with_no_stream_means_no_reader_and_a_tick_that_still_publishes_the_state():
    node, _app_, _nodes, _groups = _node(FakeDriver())
    assert node.facade.plans is None
    node.timers[0].fire()
    assert node.publishers[_PLAN_EVENTS].published == []
    assert len(node.publishers["/emori/motion/player"].published) == 1
    assert node.facade.stats()["plan_events_dropped"] == 0


def test_the_events_add_no_wake_up_of_their_own():
    node, _app_, _nodes, _groups = _node()
    assert [timer.period for timer in node.timers] == [mn.STATE_PERIOD_S]


def test_a_message_outside_the_widths_is_clamped():
    from emori_adapter.plan_events import PlanStep

    message = mn.fill_plan_event(fakes.msg_type("MotionPlanEvent")(),
                                 PlanStep(plan_id=3, seq=-4))
    assert (message.plan_id, message.seq) == ("3", 0)
    message = mn.fill_plan_event(fakes.msg_type("MotionPlanEvent")(),
                                 PlanStep(plan_id=3, seq=2**32 + 9))
    assert message.seq == 9
