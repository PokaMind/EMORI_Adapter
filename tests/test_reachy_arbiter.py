"""The motion arbiter: every refusal rule, every preemption, on a host with no ROS.

``reachy_mini_ros2/arbiter.py`` is where the arbitration that on v1 lives inside the
host application's ``RobotPlayer`` lives on v2 - lanes, drop-if-busy, the head
reservation, the owner lock, the latched motor-mode refusal.  It is the one piece of the
native robot node that decides something, and the consequences of getting it wrong are
not test failures: a dropped calm-settle is a distressed learner watching a robot do a
cheerful gesture, and a missing owner check is two components fighting over nine servos.

None of it can be exercised where it runs. ROS 2 Jazzy and ``reachy_mini`` live in the
v2 container and on the robot; no dev box and no CI has either. So the module imports
neither (its two ``reachy_mini`` enums are resolved lazily and degrade to the plain
strings they already are), and this file drives it on a real asyncio loop in a real
thread against a ``FakeBackend`` that records what it was asked to do.

Loaded by PATH, like ``tests/test_skill_facade.py``: ``ros2_ws`` is not on ``sys.path``
and putting it there would need ROS.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "arbiter.py")
_MODULE_NAME = "reachy_mini_arbiter_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`; an unregistered module makes the decorator raise.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


arbiter_module = _load()
MotionArbiter = arbiter_module.MotionArbiter
MotionRequest = arbiter_module.MotionRequest


class FakeBackend:
    """Pollen's ``Backend``, reduced to the six members the arbiter touches.

    ``hold`` is the whole reason this is not a mock: several rules only exist *while a
    move is running*, and a test that cannot hold a move open has to race the clock to
    observe them.
    """

    def __init__(self, duration: float = 0.02, mode: str = "enabled") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.targets: list[tuple] = []
        self.duration = duration
        self.hold = False
        self.fail = False
        # `enabled` by default because that is the robot the node hands the arbiter:
        # `motors_enabled_on_start` runs before the first request can arrive. The
        # arbiter now REFUSES a goto whose motors are not under position control, so a
        # fake that starts disabled would be testing a sleeping robot everywhere.
        self.mode = mode
        self.refuse_modes: set[str] = set()
        self.is_move_running = False

    # --- the four coroutines the arbiter awaits ---------------------------
    async def goto_target(self, head=None, antennas=None, duration=0.5, method=None,
                          body_yaw=None):
        await self._perform("goto_target", head=head, antennas=antennas,
                            duration=duration, method=method, body_yaw=body_yaw)

    async def wake_up(self):
        await self._perform("wake_up")

    async def goto_sleep(self):
        await self._perform("goto_sleep")

    async def play_move(self, move):
        await self._perform("play_move", move=move)

    async def _perform(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if self.fail:
            raise RuntimeError("the backend said no")
        self.is_move_running = True
        try:
            deadline = time.monotonic() + 10.0
            while self.hold and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            await asyncio.sleep(self.duration)
        finally:
            self.is_move_running = False

    # --- the synchronous members ------------------------------------------
    def set_target(self, head=None, antennas=None, body_yaw=None):
        if self.fail:
            raise RuntimeError("the backend said no")
        self.targets.append((head, antennas, body_yaw))

    def get_motor_control_mode(self):
        return self.mode

    def set_motor_control_mode(self, mode):
        name = str(getattr(mode, "value", mode))
        if name in self.refuse_modes:
            raise RuntimeError(f"firmware refuses {name}")
        self.mode = name


class Rig:
    """An arbiter with a live loop in a thread, plus the helpers every test needs."""

    def __init__(self, backend: FakeBackend, **kwargs) -> None:
        self.backend = backend
        self.loop = asyncio.new_event_loop()
        running = threading.Event()

        def _serve() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(running.set)
            self.loop.run_forever()

        self.thread = threading.Thread(target=_serve, daemon=True)
        self.thread.start()
        assert running.wait(5.0), "the arbiter loop never started"
        self.arbiter = MotionArbiter(backend, loop=self.loop, **kwargs)

    def close(self) -> None:
        self.backend.hold = False
        self.arbiter.close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(3.0)
        self.loop.close()

    def until(self, predicate, timeout: float = 3.0, what: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out waiting for {what or predicate}: "
                             f"{self.arbiter.status()}")

    def until_busy(self, timeout: float = 3.0) -> None:
        self.until(lambda: self.arbiter.status()["busy"], timeout, "a move to start")

    def until_queue(self, depth: int, timeout: float = 3.0) -> None:
        self.until(lambda: self.arbiter.status()["queue_depth"] == depth, timeout,
                   f"queue depth {depth}")


@pytest.fixture
def rig():
    backend = FakeBackend()
    rig = Rig(backend)
    try:
        yield rig
    finally:
        rig.close()


@pytest.fixture
def ros_rig():
    """An arbiter whose expression lane belongs to the ROS graph, not the host app."""

    backend = FakeBackend()
    rig = Rig(backend, expression_owner="ros")
    try:
        yield rig
    finally:
        rig.close()


def _goto(**kwargs) -> MotionRequest:
    defaults = {"kind": "goto", "duration": 0.02,
                "head_pose": np.eye(4), "antennas": (0.1, -0.1)}
    defaults.update(kwargs)
    return MotionRequest(**defaults)


# --- 1. every refusal rule, in the order submit applies them -------------------

def test_an_unknown_lane_is_refused_by_name(rig):
    outcome = rig.arbiter.submit(_goto(lane="urgent"))
    assert (outcome.accepted, outcome.error_code) == (False, "bad_lane")
    assert rig.arbiter.counters["refused_bad_lane"] == 1


def test_an_empty_lane_means_the_default_rather_than_a_wrong_one(rig):
    """A ROS message whose `lane` was never set says "" - unspecified, not invalid."""

    outcome = rig.arbiter.submit(_goto(lane="", client=""))
    assert outcome.accepted
    assert rig.arbiter.wait(outcome.move_id, 3.0).accepted


def test_the_owner_lock_refuses_everyone_else(rig):
    assert rig.arbiter.acquire_owner("ros", 30.0) is True
    refused = rig.arbiter.submit(_goto(client="hostapp"))
    assert (refused.accepted, refused.error_code) == (False, "not_owner")
    assert rig.arbiter.submit(_goto(client="ros")).accepted


def test_safety_outranks_the_owner_lock(rig):
    """A calm-settle is not an expression, and must not be refused for being another
    client's. Safety is the one lane ownership does not gate."""

    rig.arbiter.acquire_owner("ros", 30.0)
    assert rig.arbiter.submit(_goto(client="hostapp", lane="safety")).accepted


def test_a_second_acquire_by_the_holder_renews_rather_than_fails(rig):
    assert rig.arbiter.acquire_owner("ros", 30.0) is True
    assert rig.arbiter.acquire_owner("ros", 30.0) is True
    assert rig.arbiter.acquire_owner("hostapp", 30.0) is False


def test_the_expression_lane_belongs_to_exactly_one_client(rig):
    """`expression_owner` is EMORI_ROS_MOTION_OWNER, enforced next to the robot."""

    assert rig.arbiter.status()["expression_owner"] == "hostapp"
    refused = rig.arbiter.submit(_goto(lane="expression", client="ros"))
    assert (refused.accepted, refused.error_code) == (False, "not_owner")
    assert rig.arbiter.submit(_goto(lane="expression", client="hostapp")).accepted


def test_expression_owner_none_refuses_everybody(rig):
    backend = FakeBackend()
    closed = Rig(backend, expression_owner="none")
    try:
        for client in ("hostapp", "ros", "anyone"):
            outcome = closed.arbiter.submit(_goto(lane="expression", client=client))
            assert (outcome.accepted, outcome.error_code) == (False, "not_owner"), client
        # ...and interaction still works, because "nobody may be expressive" is not
        # "nobody may move the robot".
        assert closed.arbiter.submit(_goto(lane="interaction")).accepted
    finally:
        closed.close()


def test_an_unrecognised_expression_owner_reads_as_hostapp(rig):
    """Fail closed, exactly as `normalize_owner` does for the same knob in v1."""

    assert arbiter_module.normalize_expression_owner("ROS") == "ros"
    assert arbiter_module.normalize_expression_owner("nonsense") == "hostapp"
    assert arbiter_module.normalize_expression_owner("") == "hostapp"
    assert arbiter_module.normalize_expression_owner(None) == "hostapp"


def test_a_reserved_head_refuses_expression_and_nothing_else(rig):
    """The head is an INPUT device during a push turn. Expression must not fight a
    person's hands - but a recenter (interaction) and a settle (safety) still must run,
    or reserving the head would leave it wherever it was left."""

    rig.arbiter.set_head_reserved(True)
    refused = rig.arbiter.submit(_goto(lane="expression"))
    assert (refused.accepted, refused.error_code) == (False, "head_reserved")
    assert rig.arbiter.submit(_goto(lane="interaction")).accepted
    assert rig.arbiter.submit(_goto(lane="safety")).accepted

    rig.arbiter.set_head_reserved(False)
    assert rig.arbiter.submit(_goto(lane="expression")).accepted


def test_expression_is_dropped_once_two_are_already_waiting(rig):
    """`drop_if_busy`, to the number the host application uses. A third queued
    expression is not more expressive, it is one that will land after the moment."""

    rig.backend.hold = True
    assert rig.arbiter.submit(_goto(lane="expression")).accepted
    rig.until_busy()
    assert rig.arbiter.submit(_goto(lane="expression")).accepted
    assert rig.arbiter.submit(_goto(lane="expression")).accepted
    rig.until_queue(2)

    dropped = rig.arbiter.submit(_goto(lane="expression"))
    assert (dropped.accepted, dropped.error_code) == (False, "dropped")
    # The interaction lane is not subject to drop-if-busy: conversation mechanics are
    # not affect, and they are already one-per-event upstream.
    assert rig.arbiter.submit(_goto(lane="interaction")).accepted


def test_a_full_queue_is_refused_as_such(rig):
    backend = FakeBackend()
    small = Rig(backend, queue_size=2)
    try:
        backend.hold = True
        assert small.arbiter.submit(_goto()).accepted
        small.until_busy()
        assert small.arbiter.submit(_goto()).accepted
        assert small.arbiter.submit(_goto()).accepted
        small.until_queue(2)
        refused = small.arbiter.submit(_goto())
        assert (refused.accepted, refused.error_code) == (False, "queue_full")
    finally:
        small.close()


def test_set_target_is_refused_busy_while_a_move_runs(rig):
    """Pollen's daemon answers {"status":"ignored","reason":"move_running"}; we call
    that `busy` and the shim translates it back. Queueing a target would replay a stale
    wish seconds after it stopped being the caller's wish."""

    assert rig.arbiter.set_target(np.eye(4), (0.0, 0.0), 0.0).accepted
    assert rig.backend.targets

    rig.backend.hold = True
    assert rig.arbiter.submit(_goto()).accepted
    rig.until_busy()
    refused = rig.arbiter.set_target(np.eye(4))
    assert (refused.accepted, refused.error_code) == (False, "busy")
    assert len(rig.backend.targets) == 1

    rig.backend.hold = False
    rig.until(lambda: not rig.arbiter.status()["busy"], what="the move to finish")
    assert rig.arbiter.set_target(np.eye(4)).accepted


def test_a_backend_that_raises_is_an_answer_not_a_crash(rig):
    rig.backend.fail = True
    outcome = rig.arbiter.submit(_goto())
    assert outcome.accepted
    assert rig.arbiter.wait(outcome.move_id, 3.0).error_code == "backend_error"
    assert rig.arbiter.counters["failed"] == 1

    assert rig.arbiter.set_target(np.eye(4)).error_code == "backend_error"


def test_every_refusal_code_is_in_the_published_vocabulary(rig):
    """The shim maps these to HTTP codes and the actions return them verbatim; a code
    invented here and nowhere else is a caller that cannot branch on it."""

    seen = set()
    rig.arbiter.set_head_reserved(True)
    seen.add(rig.arbiter.submit(_goto(lane="nope")).error_code)
    seen.add(rig.arbiter.submit(_goto(lane="expression")).error_code)
    seen.add(rig.arbiter.submit(_goto(lane="expression", client="ros")).error_code)
    seen.add(rig.arbiter.set_motor_mode("levitate").error_code)
    assert seen <= set(arbiter_module.REFUSALS)


# --- 2. order, preemption and cancellation -------------------------------------

def test_requests_run_first_in_first_out(rig):
    rig.backend.hold = True
    ids = [rig.arbiter.submit(_goto(reason=f"#{n}")).move_id for n in range(4)]
    rig.until_busy()
    rig.until_queue(3)
    rig.backend.hold = False
    for move_id in ids:
        assert rig.arbiter.wait(move_id, 5.0).accepted
    assert [name for name, _ in rig.backend.calls] == ["goto_target"] * 4


def test_safety_cancels_the_running_move_and_flushes_queued_expression(rig):
    """The whole reason the safety lane exists: it happens now, not after the three
    cheerful gestures somebody queued a second ago."""

    rig.backend.hold = True
    expressive = rig.arbiter.submit(_goto(lane="expression")).move_id
    rig.until_busy()
    queued_expression = rig.arbiter.submit(_goto(lane="expression")).move_id
    queued_interaction = rig.arbiter.submit(_goto(lane="interaction")).move_id
    rig.until_queue(2)

    settle = rig.arbiter.submit(_goto(lane="safety", reason="engagement:deescalate"))
    assert settle.accepted
    assert rig.arbiter.wait(expressive, 3.0).error_code == "preempted"
    assert rig.arbiter.wait(queued_expression, 3.0).error_code == "preempted"

    # The safety move goes to the FRONT: it runs before the interaction request that
    # was already waiting.
    rig.backend.hold = False
    assert rig.arbiter.wait(settle.move_id, 5.0).accepted
    assert rig.arbiter.wait(queued_interaction, 5.0).accepted
    assert rig.arbiter.counters["preempted"] == 2


def test_safety_does_not_flush_queued_interaction(rig):
    """Interaction is the mechanics of a conversation - a recenter, a wake. Flushing it
    would leave the robot mid-gesture with nothing to finish the motion."""

    rig.backend.hold = True
    rig.arbiter.submit(_goto(lane="interaction"))
    rig.until_busy()
    queued = rig.arbiter.submit(_goto(lane="interaction")).move_id
    rig.until_queue(1)

    rig.arbiter.submit(_goto(lane="safety"))
    rig.backend.hold = False
    assert rig.arbiter.wait(queued, 5.0).accepted


def test_interaction_preempt_cuts_expression_and_only_expression(rig):
    rig.backend.hold = True
    expressive = rig.arbiter.submit(_goto(lane="expression")).move_id
    rig.until_busy()
    assert rig.arbiter.submit(_goto(lane="interaction", preempt=True)).accepted
    assert rig.arbiter.wait(expressive, 3.0).error_code == "preempted"

    # Now the same request against a running INTERACTION move: nothing is cut.
    rig.backend.hold = True
    rig.until_busy()
    running = rig.arbiter.status()["running_move_id"]
    assert rig.arbiter.submit(_goto(lane="interaction", preempt=True)).accepted
    time.sleep(0.05)
    assert rig.arbiter.status()["running_move_id"] == running
    rig.backend.hold = False
    assert rig.arbiter.wait(running, 5.0).accepted


def test_preempt_false_leaves_a_running_expression_alone(rig):
    rig.backend.hold = True
    expressive = rig.arbiter.submit(_goto(lane="expression")).move_id
    rig.until_busy()
    assert rig.arbiter.submit(_goto(lane="interaction", preempt=False)).accepted
    time.sleep(0.05)
    assert rig.arbiter.status()["running_move_id"] == expressive
    rig.backend.hold = False
    assert rig.arbiter.wait(expressive, 5.0).accepted


def test_stop_by_id_cancels_exactly_that_move(rig):
    rig.backend.hold = True
    first = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    second = rig.arbiter.submit(_goto()).move_id
    rig.until_queue(1)

    assert rig.arbiter.stop(second) == 1
    assert rig.arbiter.wait(second, 3.0).error_code == "cancelled"
    rig.backend.hold = False
    assert rig.arbiter.wait(first, 5.0).accepted
    assert rig.arbiter.stop(second) == 0


def test_stop_with_no_id_cancels_everything(rig):
    rig.backend.hold = True
    ids = [rig.arbiter.submit(_goto()).move_id for _ in range(3)]
    rig.until_busy()
    rig.until_queue(2)

    assert rig.arbiter.stop("") == 3
    for move_id in ids:
        assert rig.arbiter.wait(move_id, 3.0).error_code == "cancelled"
    rig.until(lambda: rig.arbiter.running() == [], what="the arbiter to go idle")
    assert rig.arbiter.counters["cancelled"] == 3


def test_running_lists_the_move_in_flight_and_the_ones_behind_it(rig):
    rig.backend.hold = True
    first = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    second = rig.arbiter.submit(_goto()).move_id
    rig.until_queue(1)
    assert rig.arbiter.running() == [first, second]
    rig.backend.hold = False
    rig.until(lambda: rig.arbiter.running() == [], what="both moves to finish")


# --- 3. wait, and what each outcome means --------------------------------------

def test_wait_reports_completion_cancellation_preemption_and_the_unknown(rig):
    completed = rig.arbiter.submit(_goto())
    assert rig.arbiter.wait(completed.move_id, 3.0) == arbiter_module.Outcome(
        True, "", completed.move_id)

    rig.backend.hold = True
    cancelled = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    rig.arbiter.stop(cancelled)
    assert rig.arbiter.wait(cancelled, 3.0).error_code == "cancelled"

    rig.backend.hold = True
    preempted = rig.arbiter.submit(_goto(lane="expression")).move_id
    rig.until_busy()
    rig.arbiter.submit(_goto(lane="safety"))
    assert rig.arbiter.wait(preempted, 3.0).error_code == "preempted"

    assert rig.arbiter.wait("never-existed", 0.05).error_code == "timeout"


def test_wait_times_out_without_disturbing_the_move(rig):
    rig.backend.hold = True
    move_id = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    assert rig.arbiter.wait(move_id, 0.05).error_code == "timeout"
    assert rig.arbiter.status()["busy"] is True
    rig.backend.hold = False
    assert rig.arbiter.wait(move_id, 5.0).accepted


# --- 4. what reaches the backend ----------------------------------------------

def test_a_goto_reaches_the_backend_with_its_units_intact(rig):
    pose = np.eye(4)
    pose[:3, 3] = [0.0, 0.0, 0.02]
    outcome = rig.arbiter.submit(MotionRequest(
        kind="goto", head_pose=pose, antennas=(0.3, -0.3), body_yaw=0.1,
        duration=0.03, interpolation="linear"))
    assert rig.arbiter.wait(outcome.move_id, 3.0).accepted

    name, kwargs = rig.backend.calls[-1]
    assert name == "goto_target"
    assert np.allclose(kwargs["head"], pose)
    # [right, left], the daemon's wire order, as a numpy array because that is what
    # Pollen's `goto_target` expects.
    assert np.allclose(kwargs["antennas"], [0.3, -0.3])
    assert kwargs["body_yaw"] == pytest.approx(0.1)
    assert kwargs["duration"] == pytest.approx(0.03)
    assert str(getattr(kwargs["method"], "value", kwargs["method"])) == "linear"


def test_an_unknown_interpolation_falls_back_to_minjerk_rather_than_failing(rig):
    outcome = rig.arbiter.submit(_goto(interpolation="bouncy"))
    assert rig.arbiter.wait(outcome.move_id, 3.0).accepted
    method = rig.backend.calls[-1][1]["method"]
    assert str(getattr(method, "value", method)) == "minjerk"


def test_wake_up_and_goto_sleep_are_the_backends_own_coroutines(rig):
    for kind in ("wake_up", "goto_sleep"):
        outcome = rig.arbiter.submit(MotionRequest(kind=kind))
        assert rig.arbiter.wait(outcome.move_id, 3.0).accepted
    assert [name for name, _ in rig.backend.calls] == ["wake_up", "goto_sleep"]


def test_a_recorded_move_is_looked_up_in_the_named_dataset(rig):
    looked_up: list[str] = []

    class Library:
        def get(self, name):
            return f"move:{name}"

    def factory(dataset: str):
        looked_up.append(dataset)
        return Library()

    backend = FakeBackend()
    with_moves = Rig(backend, recorded_moves_factory=factory)
    try:
        outcome = with_moves.arbiter.submit(MotionRequest(
            kind="play_recorded", dataset="pollen-robotics/reachy-mini-emotions-library",
            move="happy"))
        assert with_moves.arbiter.wait(outcome.move_id, 5.0).accepted
        assert looked_up == ["pollen-robotics/reachy-mini-emotions-library"]
        assert backend.calls[-1] == ("play_move", {"move": "move:happy"})
    finally:
        with_moves.close()


def test_a_node_with_no_recorded_moves_refuses_rather_than_pretends(rig):
    outcome = rig.arbiter.submit(MotionRequest(kind="play_recorded", move="happy"))
    assert rig.arbiter.wait(outcome.move_id, 3.0).error_code == "backend_error"


def test_an_unknown_kind_is_a_backend_error_not_a_silent_success(rig):
    outcome = rig.arbiter.submit(MotionRequest(kind="teleport"))
    assert rig.arbiter.wait(outcome.move_id, 3.0).error_code == "backend_error"


def test_set_target_reaches_the_backend_with_a_numpy_antenna_array(rig):
    pose = np.eye(4)
    assert rig.arbiter.set_target(pose, (0.2, -0.2), 0.05).accepted
    head, antennas, body_yaw = rig.backend.targets[-1]
    assert np.allclose(head, pose)
    assert np.allclose(antennas, [0.2, -0.2])
    assert body_yaw == pytest.approx(0.05)


def test_set_target_attributed_to_another_client_obeys_the_owner_lock(ros_rig):
    """A BodyTarget arriving over ROS is the graph's, not the host application's."""

    ros_rig.arbiter.acquire_owner("ros", 30.0)
    assert ros_rig.arbiter.set_target(np.eye(4), client="ros").accepted
    assert ros_rig.arbiter.set_target(np.eye(4)).error_code == "not_owner"


# --- 5. the owner lock over time ----------------------------------------------

def test_the_owner_lock_expires_on_its_ttl(rig):
    """A client that dies holding the lock must not leave the robot deaf for good."""

    assert rig.arbiter.acquire_owner("ros", 0.15) is True
    assert rig.arbiter.submit(_goto(client="hostapp")).error_code == "not_owner"
    rig.until(lambda: rig.arbiter.status()["owner"] == "", timeout=3.0,
              what="the owner lock to expire")
    assert rig.arbiter.submit(_goto(client="hostapp")).accepted


def test_a_zero_ttl_holds_the_lock_until_it_is_released(rig):
    assert rig.arbiter.acquire_owner("ros", 0.0) is True
    time.sleep(0.05)
    assert rig.arbiter.status()["owner"] == "ros"
    assert rig.arbiter.release_owner("hostapp") is False
    assert rig.arbiter.release_owner("ros") is True
    assert rig.arbiter.status()["owner"] == ""
    assert rig.arbiter.release_owner("ros") is False


def test_an_empty_client_releases_whatever_is_held(rig):
    """The service spells "release" as an empty client, and the caller genuinely may
    not know who the holder is."""

    rig.arbiter.acquire_owner("ros", 30.0)
    assert rig.arbiter.release_owner("") is True
    assert rig.arbiter.status()["owner"] == ""


def test_an_empty_client_cannot_take_the_lock(rig):
    assert rig.arbiter.acquire_owner("", 30.0) is False
    assert rig.arbiter.status()["owner"] == ""


# --- 6. motor mode, and the refusal that must be latched -----------------------

def test_a_mode_the_firmware_refuses_is_asked_for_exactly_once(rig):
    """The robot's v1.9.0 firmware answers `gravity_compensation` with an empty 500 and
    then wedges. Retrying in a loop spends the serial bus and risks the wedge again -
    the host application latches it, and so must this."""

    rig.backend.mode = "disabled"
    rig.backend.refuse_modes = {"gravity_compensation"}
    assert rig.arbiter.set_motor_mode("gravity_compensation").error_code == "backend_error"
    assert rig.arbiter.set_motor_mode("gravity_compensation").error_code == "backend_error"
    attempts = [mode for mode in (rig.backend.mode,)]
    assert attempts == ["disabled"]  # never applied
    # A different mode is unaffected: one latched refusal is not a dead robot.
    assert rig.arbiter.set_motor_mode("enabled").accepted
    assert rig.arbiter.status()["motor_mode"] == "enabled"


def test_an_unknown_mode_is_refused_without_touching_the_backend(rig):
    assert rig.arbiter.set_motor_mode("levitate").error_code == "backend_error"
    assert rig.backend.mode == "enabled"   # untouched
    assert set(arbiter_module.MOTOR_MODES) == {"enabled", "disabled",
                                               "gravity_compensation"}


def test_a_goto_is_refused_when_the_motors_went_limp_under_it():
    """The bug this exists for, in the order it actually happens.

    Pollen's `goto_sleep` ends with `set_motor_control_mode(Disabled)`
    (`daemon/backend/abstract.py`:1315), so the mode changes behind the arbiter's back.
    An arbiter that cached the mode it last SET would report `enabled` forever, wave the
    next `goto` through, and Pollen's control loop would ignore every position target
    while the action returned `completed: true`. A caller cannot tell that apart from a
    move that worked, which is the whole problem.
    """

    class SleepsLimp(FakeBackend):
        async def goto_sleep(self):
            await self._perform("goto_sleep")
            self.mode = "disabled"          # exactly what Pollen does

    backend = SleepsLimp()
    rig = Rig(backend)
    try:
        first = rig.arbiter.submit(_goto())
        assert rig.arbiter.wait(first.move_id, 3.0).accepted
        assert rig.arbiter.status()["motor_mode"] == "enabled"

        asleep = rig.arbiter.submit(MotionRequest(kind="goto_sleep"))
        assert rig.arbiter.wait(asleep.move_id, 3.0).accepted
        # Read THROUGH, not from a cache: nobody told the arbiter the mode changed.
        assert rig.arbiter.status()["motor_mode"] == "disabled"

        # Accepted (the queue is the right place for it) and then refused when it came
        # up to run, which is the only moment the answer is true.
        limp = rig.arbiter.submit(_goto())
        assert limp.accepted
        outcome = rig.arbiter.wait(limp.move_id, 3.0)
        assert (outcome.accepted, outcome.error_code) == (False, "motors_disabled")
        assert outcome.error_code in arbiter_module.OUTCOMES
        assert [name for name, _ in backend.calls] == ["goto_target", "goto_sleep"], \
            "the refused goto must never reach the backend"
        assert rig.arbiter.counters["refused_motors_disabled"] == 1
    finally:
        rig.close()


def test_a_recorded_move_is_refused_by_the_same_gate():
    """`play_recorded` writes a trajectory the same way a goto does, and a dataset load
    is the expensive half - refusing before it is also refusing before a download."""

    class Library:
        def get(self, name):
            return f"move:{name}"

    backend = FakeBackend(mode="disabled")
    rig = Rig(backend, recorded_moves_factory=lambda dataset: Library())
    try:
        outcome = rig.arbiter.submit(MotionRequest(kind="play_recorded", move="happy"))
        assert outcome.accepted
        assert rig.arbiter.wait(outcome.move_id, 3.0).error_code == "motors_disabled"
        assert backend.calls == []
    finally:
        rig.close()


def test_wake_up_re_enables_the_motors_before_it_moves():
    """The daemon's own wake path is `set_motor_control_mode(Enabled)` and THEN
    `wake_up()` (`daemon/daemon.py`:474). Mirrored, because a wake is exactly the
    request that arrives at a robot `goto_sleep` left limp - and a wake that played its
    whole trajectory into a de-torqued robot would be silent and complete."""

    modes: list[str] = []

    class RecordsModes(FakeBackend):
        def set_motor_control_mode(self, mode):
            modes.append(str(getattr(mode, "value", mode)))
            super().set_motor_control_mode(mode)

    backend = RecordsModes(mode="disabled")
    rig = Rig(backend)
    try:
        outcome = rig.arbiter.submit(MotionRequest(kind="wake_up"))
        assert rig.arbiter.wait(outcome.move_id, 3.0).accepted
        assert modes == ["enabled"]
        assert backend.mode == "enabled"
        # ...and now a goto is allowed again: the wake is the way out of the refusal.
        moved = rig.arbiter.submit(_goto())
        assert rig.arbiter.wait(moved.move_id, 3.0).accepted
    finally:
        rig.close()


def test_an_unreadable_motor_mode_is_not_a_refusal():
    """A backend that cannot answer is not a backend saying no. Refusing on silence
    would turn an unrelated read failure into a robot that looks asleep."""

    class Mute(FakeBackend):
        def get_motor_control_mode(self):
            raise RuntimeError("the serial bus is busy")

    backend = Mute()
    rig = Rig(backend)
    try:
        outcome = rig.arbiter.submit(_goto())
        assert rig.arbiter.wait(outcome.move_id, 3.0).accepted
    finally:
        rig.close()


def test_the_motor_mode_comes_from_the_reader_the_node_wires_in():
    """The node passes `backend_host.motor_mode`, which is the one place a mode is read
    - so the shim, /diagnostics and MotionStatus cannot disagree about it."""

    reads: list[int] = []

    def reader() -> str:
        reads.append(1)
        return "gravity_compensation"

    rig = Rig(FakeBackend(), motor_mode_reader=reader)
    try:
        assert rig.arbiter.status()["motor_mode"] == "gravity_compensation"
        assert reads, "status() must ASK, every time"
        outcome = rig.arbiter.submit(_goto())
        assert rig.arbiter.wait(outcome.move_id, 3.0).error_code == "motors_disabled"
    finally:
        rig.close()


def test_a_preempting_interaction_request_jumps_queued_expression_work(ros_rig):
    """Cutting the running expression short only to queue behind the two expressions
    waiting behind it would delay the recenter by exactly as long as the caller said it
    could not wait."""

    ros_rig.backend.hold = True
    running = ros_rig.arbiter.submit(_goto(lane="expression", client="ros")).move_id
    ros_rig.until_busy()
    queued = [ros_rig.arbiter.submit(_goto(lane="expression", client="ros")).move_id
              for _ in range(2)]
    ros_rig.until_queue(2)

    jumper = ros_rig.arbiter.submit(_goto(lane="interaction", preempt=True)).move_id
    assert ros_rig.arbiter.wait(running, 3.0).error_code == "preempted"
    # Running first, then the queue in order: the preempting request is at the front.
    ros_rig.until(lambda: ros_rig.arbiter.status()["running_move_id"] == jumper,
                  what="the preempting request to run next")
    assert ros_rig.arbiter.running() == [jumper, *queued]

    ros_rig.backend.hold = False
    assert ros_rig.arbiter.wait(jumper, 5.0).accepted


def test_a_preempting_request_still_waits_behind_earlier_interaction(ros_rig):
    """It reorders exactly ONE lane against exactly one other.

    Ahead of queued expression, and behind interaction that was asked for first -
    otherwise `preempt` would quietly become a way for the conversation mechanics to
    reorder themselves, and two `preempt=True` requests would race.
    """

    ros_rig.backend.hold = True
    running = ros_rig.arbiter.submit(_goto(lane="interaction")).move_id
    ros_rig.until_busy()
    earlier = ros_rig.arbiter.submit(_goto(lane="interaction")).move_id
    expressive = ros_rig.arbiter.submit(_goto(lane="expression", client="ros")).move_id
    ros_rig.until_queue(2)

    jumper = ros_rig.arbiter.submit(_goto(lane="interaction", preempt=True)).move_id
    ros_rig.until_queue(3)

    # Nothing was cut: the running move is interaction, not expression.
    assert ros_rig.arbiter.running() == [running, earlier, jumper, expressive]
    assert ros_rig.arbiter.counters["preempted"] == 0


# --- 7. status, listeners and counters -----------------------------------------

def test_status_carries_every_field_the_message_declares(rig):
    state = rig.arbiter.status()
    # `running_client` / `running_lane` are NOT MotionStatus fields: they are for
    # /diagnostics and the rehearsal, which want to know WHO is moving the robot rather
    # than only that something is.
    assert set(state) == {"busy", "running_move_id", "running_client", "running_lane",
                          "queue_depth", "owner", "head_reserved", "expression_owner",
                          "motor_mode", "backend", "counters"}


def test_listeners_fire_on_a_change_and_not_on_every_refusal(rig):
    seen: list[dict] = []
    rig.arbiter.add_status_listener(seen.append)

    rig.arbiter.set_head_reserved(True)
    assert seen and seen[-1]["head_reserved"] is True

    before = len(seen)
    for _ in range(5):
        rig.arbiter.submit(_goto(lane="expression"))  # all refused: head_reserved
    assert len(seen) == before, "a refusal changes no MotionStatus field"

    rig.arbiter.acquire_owner("ros", 30.0)
    assert seen[-1]["owner"] == "ros"


def test_a_listener_that_raises_does_not_wedge_the_robot(rig):
    def explode(_state):
        raise RuntimeError("the publisher is gone")

    rig.arbiter.add_status_listener(explode)
    rig.arbiter.set_head_reserved(True)
    outcome = rig.arbiter.submit(_goto())
    assert rig.arbiter.wait(outcome.move_id, 3.0).accepted


def test_the_counters_add_up(rig):
    rig.backend.hold = True
    accepted = [rig.arbiter.submit(_goto(client="ros", lane="interaction")).move_id
                for _ in range(3)]
    rig.until_busy()
    rig.until_queue(2)
    assert rig.arbiter.submit(_goto(lane="nope")).error_code == "bad_lane"
    assert rig.arbiter.submit(_goto(lane="expression", client="ros")).error_code == "not_owner"
    rig.arbiter.stop(accepted[-1])
    rig.backend.hold = False
    for move_id in accepted[:-1]:
        assert rig.arbiter.wait(move_id, 5.0).accepted
    rig.until(lambda: rig.arbiter.counters["cancelled"] == 1, what="the cancel to book")

    counters = rig.arbiter.counters
    assert counters["submitted"] == 5
    assert counters["accepted"] == 3
    assert counters["refused_bad_lane"] == 1
    assert counters["refused_not_owner"] == 1
    assert counters["completed"] == 2
    assert counters["cancelled"] == 1
    # Per-client, which is what the rehearsal's assertion table reads.
    assert counters["accepted_ros"] == 3
    assert counters["completed_ros"] == 2
    # The two identities the /diagnostics ledger is read with. A motor-mode refusal has
    # its own counter precisely so it cannot break the first one.
    refused = sum(counters[f"refused_{code}"] for code in arbiter_module.REFUSALS)
    assert counters["accepted"] + refused == counters["submitted"]
    assert (counters["completed"] + counters["cancelled"] + counters["preempted"]
            + counters["failed"]) == counters["accepted"]

    assert rig.arbiter.set_motor_mode("levitate").error_code == "backend_error"
    assert rig.arbiter.set_motor_mode("enabled").accepted
    counters = rig.arbiter.counters
    assert (counters["motor_mode_refused"], counters["motor_mode_set"]) == (1, 1)
    refused = sum(counters[f"refused_{code}"] for code in arbiter_module.REFUSALS)
    assert counters["accepted"] + refused == counters["submitted"]


def test_the_backend_name_is_derived_and_overridable(rig):
    class MujocoBackend(FakeBackend):
        pass

    derived = Rig(MujocoBackend())
    try:
        assert derived.arbiter.status()["backend"] == "mujoco"
        derived.arbiter.backend_name = "mockup"
        assert derived.arbiter.status()["backend"] == "mockup"
    finally:
        derived.close()


# --- 8. the module must stay pure ----------------------------------------------

def test_the_arbiter_imports_no_ros_and_no_robot_library_at_module_level():
    """It is loaded by path here, and the whole decision surface has to be testable on
    a box with neither ROS nor `reachy_mini`. The two `reachy_mini` enums it can use are
    resolved inside functions and degrade to the strings they already are."""

    import ast

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert "rclpy" not in top_level
    assert "reachy_mini" not in top_level
    assert top_level <= {"__future__", "asyncio", "collections", "dataclasses",
                         "threading", "time", "typing", "uuid", "numpy"}, top_level


# --- 9. sub-clients, the motion snapshot, and what "moving" means ---------------
# The face look-at component writes `set_target` on the expression lane under its own
# client name so its accepts and refusals get their own lines on /diagnostics, and the
# host application's gesture ticker polls "is the robot moving its own head" at 10 Hz.
# Both of those are new decision surface in this module, so both are pinned here.


def test_a_sub_client_inherits_its_owners_expression_rights(ros_rig):
    """`ros:gaze` is the ROS graph's gaze writer, not a fourth party.

    Without `owner_of` a client named `ros:gaze` is refused `not_owner` for ever - it
    does not string-equal `ros` - and the feature never moves the head once, on a robot
    configured exactly as the design says to configure it.
    """

    assert ros_rig.arbiter.set_target(np.eye(4), client="ros:gaze",
                                      lane="expression").accepted
    assert ros_rig.arbiter.submit(_goto(client="ros:gaze", lane="expression")).accepted


def test_a_sub_client_is_refused_when_its_owner_does_not_own_expression(rig):
    """The shipped default is `expression_owner=hostapp`, and under it gaze must be
    refused by name rather than quietly allowed through a naming trick."""

    refused = rig.arbiter.set_target(np.eye(4), client="ros:gaze", lane="expression")
    assert (refused.accepted, refused.error_code) == (False, "not_owner")
    assert rig.arbiter.set_target(np.eye(4), client="hostapp:cue",
                                  lane="expression").accepted


def test_bare_client_names_still_mean_exactly_what_they_did(rig, ros_rig):
    assert arbiter_module.owner_of("hostapp") == "hostapp"
    assert arbiter_module.owner_of("ros") == "ros"
    assert arbiter_module.owner_of("ROS:Gaze") == "ros"
    assert arbiter_module.owner_of("") == arbiter_module.DEFAULT_CLIENT

    assert rig.arbiter.submit(_goto(client="hostapp", lane="expression")).accepted
    assert (rig.arbiter.submit(_goto(client="ros", lane="expression")).error_code
            == "not_owner")
    assert ros_rig.arbiter.submit(_goto(client="ros", lane="expression")).accepted


def test_a_gaze_target_is_gated_by_the_head_reservation_and_by_busy(ros_rig):
    """The two refusals the gaze loop is written around, in the order it meets them."""

    ros_rig.arbiter.set_head_reserved(True)
    refused = ros_rig.arbiter.set_target(np.eye(4), client="ros:gaze", lane="expression")
    assert (refused.accepted, refused.error_code) == (False, "head_reserved")
    ros_rig.arbiter.set_head_reserved(False)

    ros_rig.backend.hold = True
    assert ros_rig.arbiter.submit(_goto(client="ros", lane="interaction")).accepted
    ros_rig.until_busy()
    refused = ros_rig.arbiter.set_target(np.eye(4), client="ros:gaze", lane="expression")
    assert (refused.accepted, refused.error_code) == (False, "busy")

    ros_rig.backend.hold = False
    ros_rig.until(lambda: not ros_rig.arbiter.status()["busy"], what="the move to settle")
    assert ros_rig.arbiter.set_target(np.eye(4), client="ros:gaze",
                                      lane="expression").accepted


def test_the_sub_clients_counters_are_its_own(ros_rig):
    """`accepted_ros` is what the motion-owner rehearsal reads; a 5 Hz gaze stream
    landing in it would drown the handful of moves the rehearsal is counting."""

    for _ in range(3):
        assert ros_rig.arbiter.set_target(np.eye(4), client="ros:gaze",
                                          lane="expression").accepted
    assert ros_rig.arbiter.submit(_goto(client="ros", lane="interaction")).accepted

    counters = ros_rig.arbiter.counters
    assert counters["accepted_ros:gaze"] == 3
    assert counters["completed_ros:gaze"] == 3
    assert counters["accepted_ros"] == 1


def test_per_client_refusals_are_a_second_view_and_not_part_of_the_ledger(rig):
    """`refused_<client>` answers "who is being turned away"; the two identities the
    /diagnostics ledger is read with must survive it untouched."""

    for _ in range(4):
        rig.arbiter.set_target(np.eye(4), client="ros:gaze", lane="expression")
    assert rig.arbiter.submit(_goto(client="ros", lane="expression")).error_code \
        == "not_owner"

    counters = rig.arbiter.counters
    assert counters["refused_ros:gaze"] == 4
    assert counters["refused_ros"] == 1
    refused = sum(counters[f"refused_{code}"] for code in arbiter_module.REFUSALS)
    assert counters["accepted"] + refused == counters["submitted"]
    assert (counters["completed"] + counters["cancelled"] + counters["preempted"]
            + counters["failed"]) == counters["accepted"]


def test_a_client_named_after_a_refusal_code_does_not_corrupt_the_ledger(rig):
    """The one way the second view could break the first. No shipped client is named
    `busy`; the guard exists so a future one cannot silently unbalance the sum."""

    rig.backend.hold = True
    assert rig.arbiter.submit(_goto(client="busy")).accepted
    rig.until_busy()
    assert rig.arbiter.set_target(np.eye(4), client="busy").error_code == "busy"

    counters = rig.arbiter.counters
    assert counters["refused_busy"] == 1, "one refusal, not two"
    refused = sum(counters[f"refused_{code}"] for code in arbiter_module.REFUSALS)
    assert counters["accepted"] + refused == counters["submitted"]


def test_status_names_who_is_moving_the_robot(rig):
    idle = rig.arbiter.status()
    assert (idle["running_client"], idle["running_lane"]) == ("", "")

    rig.backend.hold = True
    assert rig.arbiter.submit(_goto(client="hostapp", lane="interaction")).accepted
    rig.until_busy()
    running = rig.arbiter.status()
    assert (running["running_client"], running["running_lane"]) == ("hostapp",
                                                                   "interaction")

    rig.backend.hold = False
    rig.until(lambda: not rig.arbiter.status()["busy"], what="the move to finish")
    assert rig.arbiter.status()["running_client"] == ""


def test_the_motion_snapshot_reads_nothing_from_the_backend():
    """Polled at 10 Hz on the CM4 that is running the 50 Hz control loop. `status()`
    reads the LIVE motor mode - one backend call per ask - and copies every counter;
    this must do neither, or the gesture sensor's poll becomes a serial-bus cost."""

    class CountingBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.mode_reads = 0

        def get_motor_control_mode(self):
            self.mode_reads += 1
            return self.mode

    backend = CountingBackend()
    counting = Rig(backend)
    try:
        backend.mode_reads = 0
        for _ in range(10):
            counting.arbiter.motion_snapshot()
        assert backend.mode_reads == 0

        counting.arbiter.status()
        assert backend.mode_reads == 1
    finally:
        counting.close()


def test_the_snapshot_says_idle_when_nothing_is_running(rig):
    assert rig.arbiter.motion_snapshot() == {
        "active": False, "head_moving": False, "client": "", "lane": "",
        "head_reserved": False}


def test_an_antenna_only_goto_is_active_but_not_a_head_move(rig):
    """`feedback.py` draws exactly this line: an antenna cue cannot move the measured
    head pose, so suppressing the gesture sensor for one would disarm mode selection
    while the robot is only giving antenna feedback."""

    rig.backend.hold = True
    assert rig.arbiter.submit(_goto(head_pose=None, body_yaw=None,
                                    antennas=(0.4, -0.4))).accepted
    rig.until_busy()
    snapshot = rig.arbiter.motion_snapshot()
    assert snapshot["active"] is True
    assert snapshot["head_moving"] is False
    assert (snapshot["client"], snapshot["lane"]) == ("hostapp", "interaction")


@pytest.mark.parametrize("request_kwargs", [
    {"kind": "goto", "head_pose": None, "body_yaw": 0.2},
    {"kind": "wake_up"},
    {"kind": "goto_sleep"},
])
def test_everything_that_can_move_the_head_says_so(rig, request_kwargs):
    rig.backend.hold = True
    assert rig.arbiter.submit(MotionRequest(duration=0.02, **request_kwargs)).accepted
    rig.until_busy()
    assert rig.arbiter.motion_snapshot()["head_moving"] is True


def test_a_head_goto_is_a_head_move(rig):
    rig.backend.hold = True
    assert rig.arbiter.submit(_goto(antennas=None)).accepted
    rig.until_busy()
    assert rig.arbiter.motion_snapshot()["head_moving"] is True


def test_a_head_write_keeps_head_moving_true_for_the_settle_window(rig):
    """The servos are still settling for a few hundred ms after the
    command stops, and the residual push-turn sensor read that transient as a hand on
    the head. `head_moving` therefore outlives the write by HEAD_SETTLE_S."""

    assert rig.arbiter.set_target(np.eye(4)).accepted
    assert rig.arbiter.motion_snapshot()["head_moving"] is True
    time.sleep(arbiter_module.HEAD_SETTLE_S + 0.05)
    assert rig.arbiter.motion_snapshot()["head_moving"] is False


def test_an_antenna_only_write_does_not_claim_the_head(rig):
    """An antenna wiggle cannot move the measured head pose, so it must not suppress
    the gesture sensor either (the same line `_moves_head` draws for batches)."""

    assert rig.arbiter.set_target(None, (0.1, 0.1), None).accepted
    assert rig.arbiter.motion_snapshot()["head_moving"] is False


def test_the_snapshot_says_whether_the_head_is_reserved(rig):
    assert rig.arbiter.motion_snapshot()["head_reserved"] is False
    rig.arbiter.set_head_reserved(True)
    assert rig.arbiter.motion_snapshot()["head_reserved"] is True
    rig.arbiter.set_head_reserved(False)


# --- the face anchor: speech gestures are played where the person is --------------

def _rpy(matrix):
    m = np.asarray(matrix, dtype=float)[:3, :3]
    return (float(np.arctan2(m[2, 1], m[2, 2])), float(np.arcsin(-m[2, 0])),
            float(np.arctan2(m[1, 0], m[0, 0])))


def _pose(roll=0.0, pitch=0.0, yaw=0.0, z=0.0):
    sr, cr, sp, cp = np.sin(roll), np.cos(roll), np.sin(pitch), np.cos(pitch)
    sy, cy = np.sin(yaw), np.cos(yaw)
    m = np.eye(4)
    m[:3, :3] = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                 [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                 [-sp, cp * sr, cp * cr]]
    m[2, 3] = z
    return m


def _goto_heads(backend):
    return [kw["head"] for name, kw in backend.calls if name == "goto_target"]


def test_a_gesture_is_played_around_the_face_and_keeps_its_shape(rig):
    """A learner standing 17 deg to one side, with every talking beat authored about
    yaw 0, leaves the robot talking to the wall. Composed onto the aim, the
    beat keeps its own tilt, nod and lift and is centred on them."""

    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze")
    beat = _pose(roll=0.05, pitch=0.1, yaw=0.08, z=0.01)
    assert rig.arbiter.submit(_goto(head_pose=beat)).accepted
    rig.until(lambda: _goto_heads(rig.backend), what="the goto to run")

    played = _goto_heads(rig.backend)[0]
    roll, pitch, yaw = _rpy(played)
    assert yaw == pytest.approx(0.3 + 0.08, abs=0.02)
    assert pitch == pytest.approx(-0.2 + 0.1, abs=0.02)
    assert roll == pytest.approx(0.05, abs=0.02)
    assert played[2, 3] == pytest.approx(0.01)          # the lift is not rotated away
    assert rig.arbiter.counters["anchored"] == 1
    np.testing.assert_allclose(beat, _pose(roll=0.05, pitch=0.1, yaw=0.08, z=0.01))


def test_no_face_means_the_pose_is_played_exactly_as_authored(rig):
    rig.arbiter.set_pose_anchor(lambda: None, exempt_client="ros:gaze")
    beat = _pose(pitch=0.1, yaw=0.08)
    assert rig.arbiter.submit(_goto(head_pose=beat)).accepted
    rig.until(lambda: _goto_heads(rig.backend), what="the goto to run")
    np.testing.assert_allclose(_goto_heads(rig.backend)[0], beat)
    assert rig.arbiter.counters["anchored"] == 0


def test_safety_and_gaze_itself_are_never_re_aimed(rig):
    """A calm-settle goes where it was told, and gaze's targets already ARE the aim -
    composing them onto it again would double every turn of the head."""

    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze")
    settle = _pose(pitch=0.03)
    assert rig.arbiter.submit(_goto(head_pose=settle, lane="safety")).accepted
    rig.until(lambda: _goto_heads(rig.backend), what="the settle to run")
    np.testing.assert_allclose(_goto_heads(rig.backend)[0], settle)

    rig.until(lambda: not rig.arbiter.status()["busy"], what="the settle to finish")
    aim = _pose(pitch=-0.2, yaw=0.3)
    assert rig.arbiter.set_target(head_pose=aim, client="ros:gaze",
                                  lane="interaction").accepted
    np.testing.assert_allclose(rig.backend.targets[-1][0], aim)
    assert rig.arbiter.counters["anchored"] == 0


def test_a_streamed_target_from_another_author_is_re_aimed_too(rig):
    rig.arbiter.set_pose_anchor(lambda: (0.0, 0.3), exempt_client="ros:gaze")
    assert rig.arbiter.set_target(head_pose=_pose(yaw=0.05)).accepted
    assert _rpy(rig.backend.targets[-1][0])[2] == pytest.approx(0.35, abs=1e-6)


def test_a_broken_anchor_never_costs_the_move(rig):
    def broken():
        raise RuntimeError("gaze thread gone")

    rig.arbiter.set_pose_anchor(broken, exempt_client="ros:gaze")
    beat = _pose(yaw=0.08)
    assert rig.arbiter.submit(_goto(head_pose=beat)).accepted
    rig.until(lambda: _goto_heads(rig.backend), what="the goto to run")
    np.testing.assert_allclose(_goto_heads(rig.backend)[0], beat)


# --- recorded moves: Pollen's library, played where the person is -----------------
# `play_recorded` handing the library object to Pollen untouched would play every `rec-*`
# cue about neutral with the learner to one side, and snap onto its first
# frame. The arbiter now wraps it (`_PlacedMove`). Pollen's `play_move` reads nothing but
# `duration`, `sound_path` and `evaluate(t)`, so these tests read exactly those.

_FACE = (-0.35, 0.2)   # a standing adult at 80 cm: the pitch sits on the -0.35 clamp


class _FakeMove:
    """Pollen's ``RecordedMove``, reduced to what ``play_move`` reads.

    It MOVES from t = 0 - nod, sway, antennas and body yaw all changing - because a move
    that sits still for its first 0.3 s cannot tell an ease-in that keeps the authored
    motion from one that flattens it. ``evaluate`` raises at ``t >= duration`` exactly
    like recorded_move.py:122-123.
    """

    duration = 1.2
    sound_path = "/opt/hf/emotions/displeased2.wav"
    description = "a displeased shake"

    def evaluate(self, t):
        if t >= self.duration:
            raise ValueError("the time t is out of range")
        head = _pose(roll=0.05, pitch=0.1 + 0.2 * t, yaw=0.08 + 0.3 * t, z=0.01)
        return head, np.array([0.2 + 0.5 * t, -0.2 - 0.5 * t]), 0.1 + 0.4 * t


class _Log:
    """The rclpy logger's three levels, recorded."""

    def __init__(self) -> None:
        self.lines: dict[str, list[str]] = {"info": [], "warn": [], "debug": []}

    def info(self, message):
        self.lines["info"].append(message)

    def warn(self, message):
        self.lines["warn"].append(message)

    def debug(self, message):
        self.lines["debug"].append(message)


@pytest.fixture
def recorded_rig():
    """``build(move, **arbiter_kwargs)``: a Rig whose emotions library answers ``move``."""

    rigs: list[Rig] = []

    def build(move, **kwargs):
        class Library:
            def get(self, name):
                return move

        kwargs.setdefault("log", _Log())
        kwargs.setdefault("recorded_skip_lead_in", False)
        built = Rig(FakeBackend(), recorded_moves_factory=lambda dataset: Library(),
                    **kwargs)
        rigs.append(built)
        return built

    yield build
    for built in rigs:
        built.close()


def _play(rig, **kwargs):
    """Submit one recorded move, see it complete, and return what reached ``play_move``."""

    request = {"kind": "play_recorded", "move": "displeased2"}
    request.update(kwargs)
    outcome = rig.arbiter.submit(MotionRequest(**request))
    assert outcome.accepted, outcome
    assert rig.arbiter.wait(outcome.move_id, 5.0).accepted
    return [kw["move"] for name, kw in rig.backend.calls if name == "play_move"][-1]


def _composed(move, t, pitch=0.0, yaw=0.0):
    """The authored frame at ``t``, its rotation composed onto ``(pitch, yaw)``."""

    head, antennas, body_yaw = move.evaluate(t)
    head = np.array(head, dtype=float)
    head[:3, :3] = _pose(pitch=pitch, yaw=yaw)[:3, :3] @ head[:3, :3]
    return head, np.asarray(antennas, dtype=float), float(body_yaw)


def _held_on_the_face():
    """The start state: head commanded onto the face, antennas and body at rest."""

    return _pose(pitch=_FACE[0], yaw=_FACE[1]), np.array([0.0, 0.0]), 0.0


def _angle(a, b):
    rel = np.asarray(a)[:3, :3].T @ np.asarray(b)[:3, :3]
    return float(np.arccos(np.clip((np.trace(rel) - 1.0) / 2.0, -1.0, 1.0)))


def test_a_recorded_move_is_played_around_the_face_in_yaw(recorded_rig):
    """`play_recorded` must be composed onto the face aim too, or a displeased2 or an
    inquiring1 is played at the wall while the talking beats around it are played at
    the learner."""

    move = _FakeMove()
    rig = recorded_rig(move)
    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze")
    played = _play(rig)

    head, antennas, body_yaw = played.evaluate(0.0)
    roll, pitch, yaw = _rpy(head)
    assert yaw == pytest.approx(0.3 + 0.08, abs=1e-9)
    assert pitch == pytest.approx(0.1, abs=1e-9)        # yaw only: the nod is its own
    assert roll == pytest.approx(0.05, abs=1e-9)
    assert head[2, 3] == pytest.approx(0.01)            # the lift is not rotated away
    authored = move.evaluate(0.0)
    np.testing.assert_allclose(antennas, authored[1])
    assert body_yaw == pytest.approx(authored[2])
    # Pollen reads exactly these, and gets the library's own answers.
    assert played.duration == move.duration
    assert played.sound_path == move.sound_path
    assert played.description == move.description
    counters = rig.arbiter.counters
    assert counters["anchored_recorded"] == counters["anchored"] == 1
    assert counters["blended_recorded"] == 0


def test_full_mode_composes_the_pitch_too(recorded_rig):
    rig = recorded_rig(_FakeMove())
    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze",
                                recorded="full")
    _, pitch, yaw = _rpy(_play(rig).evaluate(0.0)[0])
    assert pitch == pytest.approx(-0.2 + 0.1, abs=0.02)
    assert yaw == pytest.approx(0.3 + 0.08, abs=0.02)


def test_the_aim_is_read_once_when_the_move_starts(recorded_rig):
    """A move re-aimed at every 3 Hz detection would wobble, and would stop being the
    gesture that was authored."""

    asked: list[int] = []

    def anchor():
        asked.append(1)
        return (-0.2, 0.3) if len(asked) == 1 else (0.1, -0.4)

    move = _FakeMove()
    rig = recorded_rig(move)
    rig.arbiter.set_pose_anchor(anchor, exempt_client="ros:gaze")
    played = _play(rig)
    for t in (0.0, 0.4, 0.8, 1.1):
        assert _rpy(played.evaluate(t)[0])[2] == pytest.approx(0.3 + 0.08 + 0.3 * t,
                                                               abs=1e-9)
    assert len(asked) == 1


def test_no_face_and_no_start_state_plays_the_library_object_itself(recorded_rig):
    move = _FakeMove()
    rig = recorded_rig(move)
    rig.arbiter.set_pose_anchor(lambda: None, exempt_client="ros:gaze")
    assert _play(rig) is move
    counters = rig.arbiter.counters
    assert counters["anchored"] == counters["blended_recorded"] == 0


def test_safety_and_gaze_recorded_moves_are_never_re_aimed(recorded_rig):
    move = _FakeMove()
    rig = recorded_rig(move)
    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze")
    assert _play(rig, lane="safety") is move
    assert _play(rig, client="ros:gaze") is move
    counters = rig.arbiter.counters
    assert counters["anchored_recorded"] == counters["anchored"] == 0


def test_a_broken_anchor_still_plays_the_recorded_move_as_authored(recorded_rig):
    def broken():
        raise RuntimeError("gaze thread gone")

    move = _FakeMove()
    rig = recorded_rig(move)
    rig.arbiter.set_pose_anchor(broken, exempt_client="ros:gaze")
    assert _play(rig) is move                         # ...and it completed
    assert rig.arbiter.counters["completed"] == 1


@pytest.mark.parametrize("mode", ["off", "sideways"])
def test_off_and_an_unknown_mode_play_recorded_moves_as_authored(recorded_rig, mode):
    move = _FakeMove()
    log = _Log()
    rig = recorded_rig(move, log=log)
    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze",
                                recorded=mode)
    assert _play(rig) is move
    assert rig.arbiter.counters["anchored_recorded"] == 0
    # A typo in deploy/ros2/.env says so, rather than quietly meaning something.
    assert any("sideways" in line for line in log.lines["warn"]) == (mode == "sideways")
    # The mode is about RECORDED moves: a speech goto is still played at the face.
    assert rig.arbiter.submit(_goto(head_pose=_pose(yaw=0.08))).accepted
    rig.until(lambda: rig.arbiter.counters["anchored"] == 1, what="the goto to anchor")


def test_a_recorded_move_starts_exactly_where_the_head_is(recorded_rig):
    """Pollen's `play_move` defaults to no initial goto and writes the first frame as it
    is: with the head held on a face that was a 17.7 deg median, 46.4 deg worst, snap
    (probe of the recorded-move dataset)."""

    move = _FakeMove()
    start = _held_on_the_face()
    rig = recorded_rig(move, start_state_reader=lambda: start)
    rig.arbiter.set_pose_anchor(lambda: _FACE, exempt_client="ros:gaze")
    played = _play(rig)

    head, antennas, body_yaw = played.evaluate(0.0)
    np.testing.assert_allclose(head, start[0], atol=1e-9)
    np.testing.assert_allclose(antennas, start[1], atol=1e-9)
    assert body_yaw == pytest.approx(start[2], abs=1e-9)
    # ...and gets onto the move smoothly: no 10 ms tick is a fifth of the jump it replaced.
    jump = _angle(start[0], _composed(move, 0.0, yaw=_FACE[1])[0])
    steps = [_angle(played.evaluate(t)[0], played.evaluate(t + 0.01)[0])
             for t in np.arange(0.0, 0.4, 0.01)]
    assert jump > 0.3
    assert max(steps) < 0.2 * jump
    counters = rig.arbiter.counters
    assert counters["blended_recorded"] == counters["anchored_recorded"] == 1


def test_after_the_ease_in_the_move_is_exactly_the_composed_move(recorded_rig):
    move = _FakeMove()
    rig = recorded_rig(move, start_state_reader=_held_on_the_face)
    rig.arbiter.set_pose_anchor(lambda: _FACE, exempt_client="ros:gaze")
    played = _play(rig)
    for t in (0.3, 0.31, 0.6, 1.0, 1.19):
        head, antennas, body_yaw = played.evaluate(t)
        want = _composed(move, t, yaw=_FACE[1])
        np.testing.assert_allclose(head, want[0], atol=1e-12)
        np.testing.assert_allclose(antennas, want[1], atol=1e-12)
        assert body_yaw == pytest.approx(want[2], abs=1e-12)


def test_a_head_already_on_the_first_frame_keeps_every_frame_of_the_move(recorded_rig):
    """The ease-in is an OFFSET that decays, not a slerp from a fixed start towards the
    moving move. The slerp damps the move's own first 0.3 s, and 13 of the 73 library
    moves travel more than 3 deg in it (fear1 17 deg, furious1 10.9). With nothing to
    correct, nothing may change - at any t."""

    move = _FakeMove()
    first = _composed(move, 0.0, yaw=_FACE[1])
    rig = recorded_rig(move, start_state_reader=lambda: first)
    rig.arbiter.set_pose_anchor(lambda: _FACE, exempt_client="ros:gaze")
    played = _play(rig)
    assert rig.arbiter.counters["blended_recorded"] == 1        # it WAS eased in...
    for t in np.arange(0.0, 0.35, 0.01):
        head, antennas, body_yaw = played.evaluate(t)
        want = _composed(move, t, yaw=_FACE[1])                 # ...by nothing at all
        np.testing.assert_allclose(head, want[0], atol=1e-9)
        np.testing.assert_allclose(antennas, want[1], atol=1e-9)
        assert body_yaw == pytest.approx(want[2], abs=1e-9)


def test_a_start_state_that_cannot_be_read_costs_the_ease_in_and_nothing_else(
        recorded_rig):
    def unreadable():
        raise RuntimeError("the backend has not ticked yet")

    move = _FakeMove()
    rig = recorded_rig(move, start_state_reader=unreadable)
    rig.arbiter.set_pose_anchor(lambda: (-0.2, 0.3), exempt_client="ros:gaze")
    played = _play(rig)
    np.testing.assert_allclose(played.evaluate(0.0)[0],
                               _composed(move, 0.0, yaw=0.3)[0], atol=1e-12)
    counters = rig.arbiter.counters
    assert (counters["anchored_recorded"], counters["blended_recorded"]) == (1, 0)


def test_a_zero_blend_turns_the_ease_in_off(recorded_rig):
    read: list[int] = []

    def reader():
        read.append(1)
        return _held_on_the_face()

    move = _FakeMove()
    rig = recorded_rig(move, recorded_blend_s=0.0, start_state_reader=reader)
    assert _play(rig) is move
    assert read == []
    assert rig.arbiter.counters["blended_recorded"] == 0


def test_the_end_of_move_race_cannot_fail_a_move_that_played_in_full(recorded_rig):
    """Pollen samples `t` after its loop check, and the real library raises for
    `t >= duration`: all 8 real moves checked raise at `duration + 2 ms`."""

    move = _FakeMove()
    with pytest.raises(ValueError):
        move.evaluate(move.duration + 0.002)
    rig = recorded_rig(move, start_state_reader=_held_on_the_face)
    rig.arbiter.set_pose_anchor(lambda: _FACE, exempt_client="ros:gaze")
    played = _play(rig)
    head = played.evaluate(move.duration + 0.002)[0]
    np.testing.assert_allclose(head, _composed(move, move.duration - 1e-3,
                                               yaw=_FACE[1])[0])
    np.testing.assert_allclose(played.evaluate(-0.01)[0], played.evaluate(0.0)[0])


class _NoDuration:
    def evaluate(self, t):
        return np.eye(4), None, None


class _EvaluateIsNotCallable:
    duration = 1.0
    evaluate = None


@pytest.mark.parametrize("move", ["move:happy", _NoDuration(), _EvaluateIsNotCallable()],
                         ids=["no-evaluate", "no-duration", "evaluate-not-callable"])
def test_a_move_that_cannot_be_placed_is_handed_over_unchanged(recorded_rig, move):
    rig = recorded_rig(move, start_state_reader=_held_on_the_face)
    rig.arbiter.set_pose_anchor(lambda: _FACE, exempt_client="ros:gaze")
    assert _play(rig) is move
    counters = rig.arbiter.counters
    assert counters["anchored"] == counters["blended_recorded"] == 0


def test_every_recorded_move_says_how_it_was_placed_in_one_line(recorded_rig):
    """The face aim is exposed nowhere else. This line is how the robot finally measures
    it, and how a `cue.plan ... rec-*` journal line is matched to what the node did."""

    log = _Log()
    rig = recorded_rig(_FakeMove(), log=log, start_state_reader=_held_on_the_face)
    aims = [(-0.2, 0.3)]
    rig.arbiter.set_pose_anchor(lambda: aims[0], exempt_client="ros:gaze")
    _play(rig)
    aims[0] = None
    _play(rig, move="inquiring1", client="verify-p10")

    lines = [line for line in log.lines["info"] if line.startswith("recorded move")]
    assert len(lines) == 2
    assert "displeased2 (hostapp/interaction)" in lines[0]
    assert "mode=yaw aim=(-0.200,+0.300) rot_yaw=+0.300 rot_pitch=+0.000" in lines[0]
    assert "blend=0.30s" in lines[0]
    assert "start_jump=" in lines[0] and "n/a" not in lines[0]
    assert "inquiring1 (verify-p10/interaction)" in lines[1]
    assert "aim=none rot_yaw=+0.000 rot_pitch=+0.000" in lines[1]


# --- supersede: a client's newest move replaces what it said before ---------------------------

def test_a_superseding_move_cuts_the_running_one_and_clears_the_queue(rig):
    rig.backend.hold = True
    running = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    waiting = rig.arbiter.submit(_goto()).move_id
    rig.until_queue(1)

    newest = rig.arbiter.submit(_goto(supersede=True))

    assert newest.accepted
    assert rig.arbiter.wait(running, 3.0).error_code == "preempted"
    assert rig.arbiter.wait(waiting, 3.0).error_code == "preempted"
    rig.backend.hold = False
    assert rig.arbiter.wait(newest.move_id, 5.0).accepted


def test_a_superseding_move_leaves_another_client_alone(rig):
    rig.backend.hold = True
    theirs = rig.arbiter.submit(_goto(client="other")).move_id
    rig.until_busy()
    assert rig.arbiter.submit(_goto(client="other")).accepted
    rig.until_queue(1)

    mine = rig.arbiter.submit(_goto(client="mine", supersede=True))

    assert mine.accepted
    time.sleep(0.05)
    assert rig.arbiter.status()["running_move_id"] == theirs
    rig.backend.hold = False
    assert rig.arbiter.wait(theirs, 5.0).accepted


def test_a_superseding_move_never_cuts_a_wake_up(rig):
    rig.backend.hold = True
    waking = rig.arbiter.submit(MotionRequest(kind="wake_up")).move_id
    rig.until_busy()

    assert rig.arbiter.submit(_goto(supersede=True)).accepted
    time.sleep(0.05)

    assert rig.arbiter.status()["running_move_id"] == waking
    rig.backend.hold = False
    assert rig.arbiter.wait(waking, 5.0).accepted


def test_a_superseding_move_never_cuts_safety(rig):
    rig.backend.hold = True
    safe = rig.arbiter.submit(_goto(lane="safety")).move_id
    rig.until_busy()

    mine = rig.arbiter.submit(_goto(supersede=True)).move_id
    time.sleep(0.05)

    assert rig.arbiter.status()["running_move_id"] == safe
    rig.backend.hold = False
    assert rig.arbiter.wait(safe, 5.0).accepted
    assert rig.arbiter.wait(mine, 5.0).accepted


def test_a_full_queue_does_not_refuse_the_move_that_clears_it(rig):
    rig.backend.hold = True
    rig.arbiter.submit(_goto())
    rig.until_busy()
    for _ in range(rig.arbiter._queue_size):
        assert rig.arbiter.submit(_goto()).accepted
    assert rig.arbiter.submit(_goto()).error_code == "queue_full"
    assert rig.arbiter.submit(_goto(supersede=True)).accepted
    rig.backend.hold = False


def test_without_supersede_a_move_still_waits_its_turn(rig):
    rig.backend.hold = True
    running = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    assert rig.arbiter.submit(_goto()).accepted
    time.sleep(0.05)
    assert rig.arbiter.status()["running_move_id"] == running
    rig.backend.hold = False
    assert rig.arbiter.wait(running, 5.0).accepted


# --- the trace: when a move really started and ended ------------------------------------------

def test_the_trace_says_when_a_move_started_and_ended(rig):
    move = rig.arbiter.submit(_goto(reason="rest:goto:7.0")).move_id
    assert rig.arbiter.wait(move, 5.0).accepted
    time.sleep(0.02)

    records = [r for r in rig.arbiter.trace_since() if r["move_id"] == move]

    assert [r["event"] for r in records] == ["accepted", "started", "settled"]
    assert records[0]["t"] <= records[1]["t"] <= records[2]["t"]
    assert all(r["reason"] == "rest:goto:7.0" and r["kind"] == "goto" for r in records)
    assert records[2]["error_code"] == ""


def test_the_trace_records_a_cut_move_and_how_many_were_waiting(rig):
    rig.backend.hold = True
    running = rig.arbiter.submit(_goto()).move_id
    rig.until_busy()
    second = rig.arbiter.submit(_goto()).move_id
    rig.until_queue(1)
    rig.arbiter.submit(_goto(supersede=True))
    assert rig.arbiter.wait(running, 3.0).error_code == "preempted"
    rig.backend.hold = False

    records = rig.arbiter.trace_since()

    assert [r["queued"] for r in records if r["event"] == "accepted" and r["move_id"] == second] \
        == [1]
    cut = [r for r in records if r["event"] == "settled" and r["move_id"] == running]
    assert cut[0]["error_code"] == "preempted"


def test_the_trace_only_returns_what_is_new(rig):
    assert rig.arbiter.wait(rig.arbiter.submit(_goto()).move_id, 5.0).accepted
    time.sleep(0.02)
    seen = rig.arbiter.trace_since()
    last = seen[-1]["seq"]
    assert rig.arbiter.trace_since(last) == []
    assert rig.arbiter.wait(rig.arbiter.submit(_goto()).move_id, 5.0).accepted
    assert all(r["seq"] > last for r in rig.arbiter.trace_since(last))


# --- a recording starts at its first motion -----------------------------------------------------

class _Recording:
    """Holds still for ``still`` seconds, then the antennas swing."""

    def __init__(self, still: float, duration: float = 3.0) -> None:
        self.still, self.duration = still, duration
        self.asked: list[float] = []

    def evaluate(self, t: float):
        self.asked.append(t)
        swing = max(0.0, t - self.still) * 2.0
        return np.eye(4), np.array([swing, -swing]), 0.0


def test_the_first_motion_of_a_recording_is_found():
    found = arbiter_module._first_motion_s(_Recording(0.8))
    assert 0.8 < found < 0.86


def test_a_recording_that_never_moves_is_played_from_the_top():
    assert arbiter_module._first_motion_s(_Recording(99.0)) == 0.0


def test_a_placed_move_starts_past_the_stillness_and_is_shorter_for_it():
    move = _Recording(0.8)
    placed = arbiter_module._PlacedMove(move, skip_s=0.75)
    assert placed.duration == pytest.approx(2.25)
    move.asked.clear()
    placed.evaluate(0.0)
    assert move.asked[-1] == pytest.approx(0.75)
    placed.evaluate(placed.duration + 1.0)         # clamped, as a move's own end always was
    assert move.asked[-1] < 3.0


def test_the_lead_in_is_worked_out_once_per_recording(rig):
    move = _Recording(0.8)
    req = MotionRequest(kind="play_recorded", dataset="d", move="m")
    first = rig.arbiter._lead_in_to_skip(req, move)
    assert first == pytest.approx(0.8 - arbiter_module.LEAD_IN_KEPT_S, abs=0.05)
    asked = len(move.asked)
    assert rig.arbiter._lead_in_to_skip(req, move) == first
    assert len(move.asked) == asked


def test_the_kill_switch_plays_a_recording_from_the_top(rig):
    rig.arbiter._recorded_skip_lead_in = False
    req = MotionRequest(kind="play_recorded", dataset="d", move="m")
    assert rig.arbiter._lead_in_to_skip(req, _Recording(0.8)) == 0.0


def test_a_recording_that_breaks_the_search_is_played_from_the_top(rig):
    class Broken(_Recording):
        def evaluate(self, t):
            raise ValueError("no frames")

    req = MotionRequest(kind="play_recorded", dataset="d", move="broken")
    assert rig.arbiter._lead_in_to_skip(req, Broken(0.8)) == 0.0


def test_a_recording_reaches_pollen_past_its_stillness(recorded_rig):
    rig = recorded_rig(_Recording(0.8), recorded_skip_lead_in=True)
    played = _play(rig)
    assert played.duration == pytest.approx(3.0 - (0.8 - arbiter_module.LEAD_IN_KEPT_S), abs=0.05)


def test_the_trace_can_say_where_it_is_without_handing_it_all_over(rig):
    assert rig.arbiter.trace_since(-1) == []
    assert rig.arbiter.wait(rig.arbiter.submit(_goto()).move_id, 5.0).accepted
    time.sleep(0.02)
    assert [r["seq"] for r in rig.arbiter.trace_since(-1)] == [rig.arbiter.trace_since()[-1]["seq"]]


def test_a_cancel_cannot_get_ahead_of_a_task_that_has_not_started(rig):
    # The moment between a move becoming current and its task existing, held open. A cancel
    # delivered to a task before its first step would leave the arbiter wedged for good.
    real = rig.loop.create_task

    def slow(coro):
        time.sleep(0.05)
        return real(coro)

    rig.loop.create_task = slow
    first = rig.arbiter.submit(_goto()).move_id
    time.sleep(0.01)
    second = rig.arbiter.submit(_goto(supersede=True)).move_id
    rig.loop.create_task = real

    assert rig.arbiter.wait(first, 3.0).error_code in ("preempted", "")
    assert rig.arbiter.wait(second, 3.0).accepted
    assert rig.arbiter.status()["running_move_id"] in ("", None)


class _RawRecording(_Recording):
    """The shape of Pollen's RecordedMove: raw frames, and an ``evaluate`` nobody should call."""

    def __init__(self, still: float, duration: float = 3.0) -> None:
        super().__init__(still, duration)
        self.timestamps = [k * 0.01 for k in range(300)]
        self.trajectory = [{"head": np.eye(4).tolist(),
                            "antennas": [max(0.0, t - still) * 2.0, 0.0]}
                           for t in self.timestamps]


def test_the_first_motion_is_read_off_the_raw_frames_without_evaluating_any():
    move = _RawRecording(0.8)
    found = arbiter_module._first_motion_s(move)
    assert 0.8 < found < 0.85
    assert move.asked == []


def test_the_head_turning_counts_as_the_first_motion():
    move = _RawRecording(9.0)
    pitch = np.radians(2.0)
    turned = np.eye(4)
    turned[:3, :3] = [[np.cos(pitch), 0, np.sin(pitch)], [0, 1, 0], [-np.sin(pitch), 0, np.cos(pitch)]]
    for frame in move.trajectory[50:]:
        frame["head"] = turned.tolist()
    assert arbiter_module._first_motion_s(move) == pytest.approx(0.5)
    assert move.asked == []


def test_malformed_frames_fall_back_on_evaluating():
    move = _RawRecording(0.8)
    move.trajectory[3] = {"nothing": 1}
    assert 0.8 < arbiter_module._first_motion_s(move) < 0.86
    assert move.asked != []


# --- a head the IK refuses --------------------------------------------------------------

def _below(limit: float):
    """An IK that takes a head up to ``limit`` above neutral, whatever its rotation."""

    return lambda head, body_yaw: float(head[2, 3]) <= limit


def test_a_head_out_of_reach_keeps_its_rotation_and_comes_down_into_reach():
    head = _pose(roll=0.2, pitch=0.1, yaw=0.3, z=0.03)
    before = head.copy()

    fitted = arbiter_module._fit_into_reach(head, 0.0, _below(0.02))

    np.testing.assert_allclose(fitted[:3, :3], before[:3, :3])
    assert 0.0195 <= fitted[2, 3] <= 0.02
    np.testing.assert_array_equal(head, before)


def test_a_head_in_reach_comes_back_as_it_went_in():
    head = _pose(pitch=0.1, z=0.01)
    assert arbiter_module._fit_into_reach(head, 0.0, _below(0.02)) is head


def test_a_rotation_out_of_reach_at_any_height_is_left_for_the_backend_to_refuse():
    head = _pose(pitch=0.1, z=0.03)
    assert arbiter_module._fit_into_reach(head, 0.0, lambda *_: False) is head


def test_an_ik_that_raises_costs_the_move_nothing():
    def broken(*_):
        raise RuntimeError("no kinematics")

    head = _pose(pitch=0.1, z=0.03)
    assert arbiter_module._fit_into_reach(head, 0.0, broken) is head


_LEGS = (np.full(6, -1.0), np.full(6, 1.0))


@pytest.mark.parametrize("joints,reachable", [
    (None, False),
    ([0.0, np.nan, 0.0, 0.0, 0.0, 0.0, 0.0], False),
    ([0.0, 0.5, -0.5, 0.5, -0.5, 0.5, -0.5], True),
    ([0.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0], True),
    ([0.0, 1.2, 0.0, 0.0, 0.0, 0.0, 0.0], False),
    ([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.2], False),
    ([2.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], True),
])
def test_a_head_is_in_reach_when_the_ik_takes_it_and_each_leg_stays_within_its_servo(joints, reachable):
    """The body's own joint comes first and is not a leg: it turns all the way round."""

    check = arbiter_module.head_reach_check(lambda head, yaw: joints, _LEGS)
    assert check(np.eye(4), 0.0) is reachable


def test_the_servo_limits_are_the_motor_configs_steps_in_radians():
    class Motor:
        def __init__(self, low, high):
            self.angle_limit_min, self.angle_limit_max = low, high

    motors = {f"stewart_{i}": Motor(1138 + i, 2958 - i) for i in range(1, 7)}
    motors["body_rotation"] = Motor(0, 4095)

    low, high = arbiter_module.stewart_limits(motors)

    assert low.shape == high.shape == (6,)
    assert low[0] == pytest.approx((1139 - 2048) * 2 * np.pi / 4096)
    assert high[5] == pytest.approx((2952 - 2048) * 2 * np.pi / 4096)


@pytest.fixture
def real_reach():
    """Pollen's IK and motor config as the robot node reads them: (joints_for, reachable)."""

    kinematics = pytest.importorskip("reachy_mini.kinematics")
    parser = pytest.importorskip("reachy_mini.utils.hardware_config.parser")
    reachy_mini = pytest.importorskip("reachy_mini")
    config = Path(reachy_mini.__file__).parent / "assets" / "config" / "hardware_config.yaml"
    ik = kinematics.AnalyticalKinematics()

    def joints_for(head, body_yaw):
        return ik.ik(head, body_yaw=float(body_yaw))

    limits = arbiter_module.stewart_limits(parser.parse_yaml_config(str(config)).motors)
    return joints_for, arbiter_module.head_reach_check(joints_for, limits)


def test_pollens_ik_takes_what_the_fit_makes_of_a_head_it_refuses(real_reach):
    joints_for, reachable = real_reach
    head = _pose(pitch=0.1, z=0.03)
    assert np.any(np.isnan(joints_for(head, 0.0)))

    fitted = arbiter_module._fit_into_reach(head, 0.0, reachable)

    assert reachable(fitted, 0.0)
    np.testing.assert_allclose(fitted[:3, :3], head[:3, :3])
    assert 0.0 < fitted[2, 3] < head[2, 3]


def test_a_head_the_ik_takes_and_a_servo_cannot_follow_is_drawn_in_too(real_reach):
    """The IK takes this one, and a fit that asked only the IK would have sent it as it is."""

    joints_for, reachable = real_reach
    head = _pose(pitch=-0.2, z=0.02)
    assert not np.any(np.isnan(joints_for(head, 0.0)))
    assert not reachable(head, 0.0)

    fitted = arbiter_module._fit_into_reach(head, 0.0, reachable)

    assert fitted is not head and reachable(fitted, 0.0)
    np.testing.assert_allclose(fitted[:3, :3], head[:3, :3])


def test_every_frame_of_a_placed_move_goes_out_in_reach_with_the_rest_as_authored():
    move = _FakeMove()
    placed = arbiter_module._PlacedMove(move, head_reachable=_below(0.005))

    for t in np.arange(0.0, move.duration - 0.01, 0.1):
        head, antennas, body_yaw = placed.evaluate(t)
        authored_head, authored_antennas, authored_yaw = move.evaluate(t)
        assert head[2, 3] <= 0.005
        np.testing.assert_allclose(head[:3, :3], authored_head[:3, :3])
        np.testing.assert_allclose(antennas, authored_antennas)
        assert body_yaw == pytest.approx(authored_yaw)


def test_the_fit_covers_the_ease_in_as_well():
    """It runs on the frame that goes out, so a start the ease-in pulls out of reach is fitted too."""

    start = (_pose(z=0.03), np.zeros(2), 0.0)
    placed = arbiter_module._PlacedMove(_FakeMove(), start=start, blend_s=0.3,
                                        head_reachable=_below(0.02))
    assert placed.evaluate(0.0)[0][2, 3] <= 0.02


def test_the_arbiter_fits_a_move_it_would_otherwise_hand_over_as_it_is(recorded_rig):
    """With no aim, no start state and no lead-in there was nothing to wrap it for."""

    move = _FakeMove()
    rig = recorded_rig(move, recorded_blend_s=0.0, head_reachable=_below(0.005))
    assert _play(rig).evaluate(0.2)[0][2, 3] <= 0.005


def test_with_no_fit_a_move_with_nothing_else_to_add_is_still_handed_over_as_it_is(recorded_rig):
    move = _FakeMove()
    assert _play(recorded_rig(move, recorded_blend_s=0.0)) is move


def _placed_with_gap(degrees: float, blend: float = 0.3):
    angle = np.radians(degrees)
    start_head = np.eye(4)
    start_head[:3, :3] = [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                          [-np.sin(angle), 0, np.cos(angle)]]
    placed = arbiter_module._PlacedMove(_Recording(0.0), start=(start_head, np.zeros(2), 0.0),
                                        blend_s=blend)
    placed.fit_blend()
    return placed


def test_a_small_gap_gets_a_short_ease_and_a_large_one_keeps_its_own():
    assert _placed_with_gap(2.0)._blend_s == pytest.approx(arbiter_module.BLEND_MIN_S)
    assert _placed_with_gap(12.0)._blend_s == pytest.approx(0.2)
    assert _placed_with_gap(40.0)._blend_s == pytest.approx(0.3)


def test_a_blend_that_is_off_stays_off():
    assert _placed_with_gap(12.0, blend=0.0)._blend_s == 0.0
