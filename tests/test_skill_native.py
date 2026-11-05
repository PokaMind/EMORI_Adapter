"""The v2 skill transport, exercised on a machine with no ROS and no robot.

``emori_skill_native`` is what makes the ROS graph able to move a robot that is itself a
node: a bounded skill request becomes ``GoTo`` / ``PlayRecordedMove`` goals on
``reachy_mini_ros2`` instead of an HTTP POST to the host application.  Everything that
can be wrong about that is silent on a robot:

* metres vs. millimetres and radians vs. degrees - the head moves, just not where asked;
* the antenna order (authored ``(left, right)``, wire ``[right, left]``) - every
  asymmetric gesture becomes its own mirror image, which reads as "quirky", not "broken";
* the lane - a de-escalation settle on the expression lane is exactly the one the
  drop-if-busy rule throws away, so the calm a person is owed silently stops happening;
* the outcome mapping - a refusal whose code is lost reads as success on both ledgers;
* a batch that is not cancelled when the next one arrives - two writers into one head.

ROS 2 runs in the v2 container and nowhere else: not on the dev boxes, not on the CM4
host, not in CI.  So the module is loaded BY PATH with its ``rclpy`` imports guarded, and
every ROS object it needs (action clients, message constructors) is injected as a fake -
which is the reason ``NativeClients`` exists as a bundle at all.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path
import sys
import threading
import time
import types

import pytest

from emori_adapter.models import HeadPose, MotionIntent

_WS = Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
_FACADE_ROOT = _WS / "open" / "emori_skill_facade"
_NATIVE_ROOT = _WS / "emori" / "emori_skill_native"


def _on_path() -> None:
    """Both package roots on sys.path: neither is installed and ROS is not here."""

    for root in (_FACADE_ROOT, _NATIVE_ROOT):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))


def _load():
    _on_path()
    # Importing the package runs its __init__, which registers the transport - that
    # import-time registration is itself part of the contract (see the registry tests).
    return (importlib.import_module("emori_skill_native.native"),
            importlib.import_module("emori_skill_facade.transport"),
            importlib.import_module("emori_skill_facade.contract"))


native, transports, contract = _load()


# --- fakes: the three ROS shapes this transport touches -------------------------------


class _Future:
    """rclpy's Future, as far as this transport uses it: a callback and a result."""

    def __init__(self, result=None, *, done: bool = True) -> None:
        self._result = result
        self._done = done
        self._callbacks: list = []

    def add_done_callback(self, callback) -> None:
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def result(self):
        return self._result

    def set_result(self, value) -> None:
        self._result, self._done = value, True
        for callback in list(self._callbacks):
            callback(self)


def _result(completed: bool = True, error_code: str = "", move_id: str = "m1"):
    """A GoTo/PlayRecordedMove Result, wrapped the way get_result_async wraps it."""

    return types.SimpleNamespace(
        status=4,
        result=types.SimpleNamespace(completed=completed, error_code=error_code,
                                     move_id=move_id))


class _GoalHandle:
    def __init__(self, result=None, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.cancelled = False
        self._future = _Future(result, done=result is not None)

    def get_result_async(self) -> _Future:
        return self._future

    def cancel_goal_async(self) -> _Future:
        self.cancelled = True
        return _Future(None)

    def finish(self, result) -> None:
        self._future.set_result(result)


class _ActionClient:
    """Records goals; answers immediately, or hangs until the test finishes a goal.

    ``hang`` holds the RESULT (the goal is accepted, the move never ends).
    ``hang_acceptance`` holds the goal HANDLE itself - the server has not said yes or no
    yet - which is the window where a superseded batch can walk away from a goal that is
    about to be accepted behind its back.
    """

    def __init__(self, *, ready: bool = True, hang: bool = False,
                 hang_acceptance: bool = False, result=None,
                 accepted: bool = True) -> None:
        self.ready = ready
        self.hang = hang
        self.hang_acceptance = hang_acceptance
        self.accepted = accepted
        self.answer = result if result is not None else _result()
        self.goals: list = []
        self.handles: list[_GoalHandle] = []
        self.sent: list[_Future] = []

    def wait_for_server(self, timeout_sec=None) -> bool:
        return self.ready

    def send_goal_async(self, goal) -> _Future:
        self.goals.append(goal)
        handle = _GoalHandle(None if self.hang else self.answer, accepted=self.accepted)
        self.handles.append(handle)
        future = _Future(None, done=False) if self.hang_acceptance else _Future(handle)
        self.sent.append(future)
        return future

    def accept_now(self, index: int = 0) -> _GoalHandle:
        """Let the server answer the goal it was sitting on."""

        handle = self.handles[index]
        self.sent[index].set_result(handle)
        return handle


class _ServiceClient:
    def __init__(self, *, ready: bool = True, success: bool = True) -> None:
        self.ready = ready
        self.success = success
        self.calls: list = []

    def wait_for_service(self, timeout_sec=None) -> bool:
        return self.ready

    def call_async(self, request) -> _Future:
        self.calls.append(request)
        return _Future(types.SimpleNamespace(success=self.success, message=""))


class _Publisher:
    def __init__(self) -> None:
        self.messages: list = []

    def publish(self, message) -> None:
        self.messages.append(message)


def _pose_msg():
    return types.SimpleNamespace(
        position=types.SimpleNamespace(x=0.0, y=0.0, z=0.0),
        orientation=types.SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0))


def _goto_goal():
    """Stand-in for `reachy_mini_msgs/action/GoTo`.Goal() - every field the .action names."""

    return types.SimpleNamespace(
        use_head=False, head_pose=_pose_msg(), use_antennas=False, antennas=[],
        use_body_yaw=False, body_yaw=0.0, duration=0.0, interpolation="", lane="",
        client="", preempt=False, reason="")


def _recorded_goal():
    return types.SimpleNamespace(dataset="", name="", lane="", client="", preempt=False,
                                 reason="")


def _body_target():
    return types.SimpleNamespace(
        header=types.SimpleNamespace(stamp=None), use_head=False, head_pose=_pose_msg(),
        use_antennas=False, antennas=[], use_body_yaw=False, body_yaw=0.0)


def _clients(**kw):
    """A full bundle, with everything the transport asks the ROS side for."""

    bundle = native.NativeClients(
        goto=kw.pop("goto", None) or _ActionClient(),
        recorded=kw.pop("recorded", None) or _ActionClient(),
        wake=kw.pop("wake", None) or _ServiceClient(),
        targets=kw.pop("targets", None) or _Publisher(),
        goto_goal=_goto_goal, recorded_goal=_recorded_goal, body_target=_body_target,
        trigger=lambda: types.SimpleNamespace())
    assert not kw, kw
    return bundle


def _transport(clients=None, **kw):
    return native.NativeTransport(node=None, timeout_s=kw.pop("timeout_s", 0.3),
                                  clients=clients if clients is not None else _clients(),
                                  **kw)


def _until(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- the pure half: units, order, lanes ----------------------------------------------


def test_the_module_imports_without_ros_and_says_so_when_asked_to_build_clients():
    """The guard is the contract: without it this file could not be read off the robot."""

    assert native.ROS_AVAILABLE is False
    with pytest.raises(RuntimeError):
        native.build_clients(object())


def test_a_head_pose_reaches_the_goal_in_metres_and_radians():
    """mm and degrees are the adapter's units; the robot node speaks SI and quaternions.

    Getting this wrong does not fail: 10 mm sent as 10 m is refused by the kinematics,
    and 15 deg sent as 15 rad is a head that spins to some unrelated pose.
    """

    intent = MotionIntent(kind="goto", head=HeadPose(x=5, y=-2, z=10, yaw=15),
                          duration=0.4, reason="skill look_at")

    spec = native.intents_to_requests([intent])[0]

    assert spec.kind == "goto"
    assert spec.use_head is True
    assert spec.position == pytest.approx((0.005, -0.002, 0.010))
    assert spec.orientation == pytest.approx(
        native.rpy_to_quaternion(0.0, 0.0, math.radians(15)))
    assert spec.duration == pytest.approx(0.4)


def test_the_quaternion_is_the_convention_pollen_builds_every_head_pose_with():
    """`Rotation.from_euler("xyz", [roll, pitch, yaw])` - the daemon's models.py and
    `utils.create_head_pose` both use it, and the robot node reads the Pose back into
    that same matrix. A different order would tilt where it was told to turn."""

    scipy_rotation = pytest.importorskip("scipy.spatial.transform").Rotation

    for roll, pitch, yaw in [(0.0, 0.0, 0.0), (0.2, -0.1, 0.3), (-0.4, 0.25, -0.6)]:
        expected = scipy_rotation.from_euler("xyz", [roll, pitch, yaw]).as_quat()
        assert native.rpy_to_quaternion(roll, pitch, yaw) == pytest.approx(
            tuple(float(v) for v in expected), abs=1e-12)


def test_antennas_authored_left_right_reach_the_wire_as_right_left():
    """The behaviour layer authors gestures the way a human reads them; the wire is
    `[right, left]` (index 0 = RIGHT). Swapped here, exactly where the hostapp's
    `_antennas_wire` swaps it for the daemon, or every wink lands on the wrong antenna."""

    intent = MotionIntent(kind="antennas", antennas=(0.8, -0.2), duration=0.25)

    spec = native.intents_to_requests([intent])[0]

    assert spec.use_antennas is True
    assert spec.antennas == pytest.approx((-0.2, 0.8))


@pytest.mark.parametrize("intent, kind, skipped", [
    (MotionIntent(kind="goto", head=HeadPose(pitch=4)), "goto", ""),
    (MotionIntent(kind="look_at", head=HeadPose(yaw=10)), "goto", ""),
    (MotionIntent(kind="antennas", antennas=(0.3, 0.3)), "goto", ""),
    (MotionIntent(kind="set_target", head=HeadPose(z=3)), "set_target", ""),
    (MotionIntent(kind="play_recorded", move="happy1"), "play_recorded", ""),
    (MotionIntent(kind="wake_up"), "wake_up", ""),
    # No media in the v2 node: counted by name rather than silently lost, so a ledger
    # says "the audio path is gone" instead of nothing at all.
    (MotionIntent(kind="play_sound", sound="hm.wav"), "skipped", "play_sound"),
    (MotionIntent(kind="wobble", wobble=True), "skipped", "wobble"),
])
def test_every_intent_kind_lands_on_the_surface_that_can_perform_it(intent, kind, skipped):
    spec = native.intents_to_requests([intent])[0]

    assert (spec.kind, spec.skipped_kind) == (kind, skipped)
    if kind == "play_recorded":
        assert spec.dataset == "pollen-robotics/reachy-mini-emotions-library"
        assert spec.move == "happy1"


@pytest.mark.parametrize("reason, lane", [
    # The calm a person is owed outranks expression: on the expression lane it is the
    # first thing drop-if-busy throws away, which is the one refusal that must not
    # happen. v1 got this by exempting calm-settle from the ownership gate.
    ("engagement:deescalate", "safety"),
    ("cue:end", "safety"),
    ("engagement:engaged", "expression"),
    ("cue:levity", "expression"),
    ("", "expression"),
])
def test_a_de_escalation_settle_is_the_one_batch_that_goes_on_the_safety_lane(reason, lane):
    assert native.batch_lane(reason) == lane

    specs = native.intents_to_requests(
        [MotionIntent(kind="goto", head=HeadPose(pitch=2))], reason=reason)

    assert specs[0].lane == lane
    # One identity for the arbiter's owner lock, and never a preempt from this side: the
    # safety lane already cancels what is running, and expression must never cut in.
    assert (specs[0].client, specs[0].preempt) == ("ros", False)


def test_the_goal_carries_every_field_the_action_declares():
    """A rosidl goal starts fully default-constructed: a field this transport forgets is
    not an error, it is a zero - a duration of 0 s, or an empty lane the arbiter refuses
    as `bad_lane`, both of which look like "the robot ignored us"."""

    intent = MotionIntent(kind="goto", head=HeadPose(z=10, yaw=15), antennas=(0.2, -0.2),
                          body_yaw=0.1, duration=0.35, interpolation="linear",
                          reason="skill replay nod")

    goal = native.fill_goto_goal(_goto_goal(), native.intents_to_requests(
        [intent], reason="engagement:nod")[0])

    assert (goal.use_head, goal.use_antennas, goal.use_body_yaw) == (True, True, True)
    assert goal.head_pose.position.z == pytest.approx(0.010)
    assert goal.head_pose.orientation.w == pytest.approx(math.cos(math.radians(15) / 2))
    assert goal.antennas == pytest.approx([-0.2, 0.2])
    assert goal.body_yaw == pytest.approx(0.1)
    assert (goal.duration, goal.interpolation) == (pytest.approx(0.35), "linear")
    assert (goal.lane, goal.client, goal.preempt) == ("expression", "ros", False)
    assert goal.reason == "skill replay nod"


def test_a_pose_less_intent_says_so_instead_of_commanding_the_origin():
    """`use_head=False` is the difference between "leave the head alone" and "move the
    head to 0,0,0", which on a Stewart platform is a visible snap to centre."""

    goal = native.fill_goto_goal(_goto_goal(), native.intents_to_requests(
        [MotionIntent(kind="antennas", antennas=(0.5, 0.5))])[0])

    assert goal.use_head is False
    assert goal.head_pose.orientation.w == pytest.approx(1.0)
    assert goal.use_body_yaw is False


def test_a_recorded_move_goal_names_pollens_own_dataset():
    goal = native.fill_recorded_goal(_recorded_goal(), native.intents_to_requests(
        [MotionIntent(kind="play_recorded", move="happy1")], reason="cue:levity")[0])

    assert (goal.dataset, goal.name) == ("pollen-robotics/reachy-mini-emotions-library",
                                         "happy1")
    assert (goal.lane, goal.client) == ("expression", "ros")


def test_a_streaming_target_carries_the_same_units_as_a_goal():
    message = native.fill_body_target(_body_target(), native.intents_to_requests(
        [MotionIntent(kind="set_target", head=HeadPose(z=6), antennas=(0.1, -0.1))])[0])

    assert message.use_head is True
    assert message.head_pose.position.z == pytest.approx(0.006)
    assert message.antennas == pytest.approx([-0.1, 0.1])


# --- what an answer means -------------------------------------------------------------


@pytest.mark.parametrize("result, accepted, code, counter", [
    (types.SimpleNamespace(completed=True, error_code=""), True, "", "accepted"),
    # `dropped` is an honest answer, not a failure: the arbiter was busy with motion that
    # outranks expression. Its own counter, exactly as the HTTP transport files it.
    (types.SimpleNamespace(completed=False, error_code="dropped"), False, "dropped",
     "dropped"),
    (types.SimpleNamespace(completed=False, error_code="not_owner"), False, "not_owner",
     "refused"),
    (types.SimpleNamespace(completed=False, error_code="head_reserved"), False,
     "head_reserved", "refused"),
    (types.SimpleNamespace(completed=False, error_code="queue_full"), False, "queue_full",
     "refused"),
    # Not completed and nothing said: honestly unnamed rather than given a meaning.
    (types.SimpleNamespace(completed=False, error_code=""), False, "unknown", "refused"),
    # Nobody answered at all - never confused with "they said no".
    (None, False, "unreachable", "unreachable"),
])
def test_the_first_goals_result_is_the_answer_in_the_facades_own_vocabulary(
        result, accepted, code, counter):
    outcome = native.first_result_outcome(result)

    assert (outcome.accepted, outcome.error_code, outcome.counter) == (
        accepted, code, counter)
    assert outcome.counter in contract.COUNTER_NAMES


def test_the_refusal_vocabulary_is_the_one_the_hostapp_intake_answered_with():
    """Shared on purpose: `hri_engagement_behaviors` reads one set of words whichever
    transport is plugged in, and a v1 rehearsal ledger compares with a v2 one."""

    assert native.first_result_outcome(
        types.SimpleNamespace(completed=False, error_code="dropped")).transient is True
    assert native.first_result_outcome(
        types.SimpleNamespace(completed=False, error_code="not_owner")).transient is False


# --- the transport, end to end with fake clients --------------------------------------


def test_a_skill_request_becomes_goals_and_the_first_result_is_the_transports_answer():
    clients = _clients()
    transport = _transport(clients)
    try:
        outcome = transport.submit(
            {"skill": "replay_motion", "name": "nod", "intensity": 0.5}, "engagement:nod")

        assert (outcome.accepted, outcome.counter) == (True, "accepted")
        assert _until(lambda: transport.counters()["goals_completed"] >= 1)
        assert clients.goto.goals, "no goal ever reached the robot node"
        assert {goal.lane for goal in clients.goto.goals} == {"expression"}
        assert {goal.client for goal in clients.goto.goals} == {"ros"}
    finally:
        transport.close()


def test_a_refused_goal_comes_back_with_the_code_the_arbiter_used():
    clients = _clients(goto=_ActionClient(result=_result(False, "dropped")))
    transport = _transport(clients)
    try:
        outcome = transport.submit(
            {"skill": "look_at", "yaw_deg": 10, "pitch_deg": 0, "duration": 0.3}, "cue:x")

        assert (outcome.accepted, outcome.error_code, outcome.counter) == (
            False, "dropped", "dropped")
        assert transport.counters()["goals_refused_dropped"] >= 1
    finally:
        transport.close()


def test_an_action_server_that_is_not_there_is_unreachable_and_nothing_is_sent():
    """Never a refusal: "the robot node is not up" and "the robot said no" are different
    faults, and only one of them is fixed by starting a container."""

    clients = _clients(goto=_ActionClient(ready=False))
    transport = _transport(clients)
    try:
        outcome = transport.submit({"skill": "replay_motion", "name": "nod"}, "e:nod")

        assert (outcome.accepted, outcome.error_code, outcome.counter) == (
            False, "unreachable", "unreachable")
        assert clients.goto.goals == []
        assert transport.counters()["unreachable"] == 1
    finally:
        transport.close()


@pytest.mark.parametrize("body, code", [
    ({"skill": "teleport"}, "bad_skill"),
    ({"skill": "replay_motion", "name": "backflip"}, "out_of_range"),
    ({"skill": "look_at", "yaw_deg": 90.0}, "out_of_range"),
    ({"skill": "set_expression", "label": "smug"}, "out_of_range"),
])
def test_a_bad_request_is_refused_with_the_code_the_hostapp_intake_would_have_used(
        body, code):
    """The bounds check is the core's, not this transport's, so a behaviour node cannot
    tell which side checked - and must not have to."""

    clients = _clients()
    transport = _transport(clients)
    try:
        outcome = transport.submit(body, "cue:x")

        assert (outcome.accepted, outcome.error_code, outcome.counter) == (
            False, code, "refused")
        assert clients.goto.goals == [], "a refused request must reach no robot"
        assert transport.counters()["goals_refused_" + code] == 1
    finally:
        transport.close()


def test_a_kind_the_v2_node_cannot_perform_is_counted_by_name_and_not_an_outage(
        monkeypatch):
    """A request that expands to nothing performable was not refused by anybody: saying
    "unreachable" here would put a network fault on the ledger for a missing speaker."""

    clients = _clients()
    transport = _transport(clients)
    monkeypatch.setattr(native, "intents_to_requests", lambda *a, **k: [
        native.GoalSpec(kind="skipped", skipped_kind="play_sound"),
        native.GoalSpec(kind="skipped", skipped_kind="wobble")])
    try:
        outcome = transport.submit({"skill": "replay_motion", "name": "nod"}, "e:nod")

        assert (outcome.accepted, outcome.counter) == (True, "accepted")
        assert clients.goto.goals == []
        counters = transport.counters()
        assert (counters["skipped_play_sound"], counters["skipped_wobble"]) == (1, 1)
    finally:
        transport.close()


def test_a_newer_batch_cancels_what_is_left_of_the_previous_one():
    """The worker is the hostapp `RobotPlayer`'s shape: one batch at a time, and a batch
    queued before the world changed never finishes. Without it a 2 s cue plan is still
    playing while the next sentence's plan starts - two writers into one head."""

    started = threading.Event()
    release = threading.Event()
    ran: list = []

    def _run_spec(spec, batch):
        ran.append(spec.reason)
        if spec.reason == "a0":
            started.set()
            release.wait(2.0)
        return contract.SkillOutcome(True, "", "accepted")

    worker = native.BatchWorker(_run_spec)
    try:
        first = native._Batch(
            specs=[native.GoalSpec(kind="goto", reason=name) for name in
                   ("a0", "a1", "a2")], reason="first")
        worker.submit(first)
        assert started.wait(2.0), "the worker never started the first batch"

        second = native._Batch(specs=[native.GoalSpec(kind="goto", reason="b0")],
                               reason="second")
        worker.submit(second)
        release.set()

        assert _until(lambda: "b0" in ran)
        # a1/a2 belonged to a decision that has been superseded; they must never run.
        assert "a1" not in ran and "a2" not in ran
        assert worker.batches_cancelled == 1
        assert first.first.get(timeout=1.0).accepted is True
    finally:
        release.set()
        worker.stop()


def test_a_goal_in_flight_is_cancelled_when_a_newer_batch_arrives():
    """Cancelling through the action server IS the arbiter's `stop`: the move stops on
    the robot, rather than this side merely losing interest in the answer."""

    clients = _clients(goto=_ActionClient(hang=True))
    transport = _transport(clients, timeout_s=0.2)
    answers: list = []

    def _submit(reason):
        answers.append(transport.submit(
            {"skill": "replay_motion", "name": "nod"}, reason))

    first = threading.Thread(target=_submit, args=("engagement:nod",), daemon=True)
    second = threading.Thread(target=_submit, args=("cue:levity",), daemon=True)
    first.start()
    try:
        assert _until(lambda: bool(clients.goto.handles)), "no goal was ever sent"

        second.start()

        assert _until(lambda: clients.goto.handles[0].cancelled)
        assert _until(lambda: transport.counters()["batches_cancelled"] == 1)
        # The superseded batch answers `cancelled`, not a refusal the robot never made.
        assert _until(lambda: bool(answers))
        assert answers[0].error_code == "cancelled"
    finally:
        transport.close()
        first.join(2.0)
        second.join(2.0)


def test_a_goal_accepted_after_its_batch_was_superseded_is_still_cancelled():
    """The orphan. `_wait` gives up on the batch's cancel flag, but giving up on the
    FUTURE does not un-send the goal: the server is free to accept it a moment later,
    and then a move the superseded batch asked for starts under the batch that
    superseded it. Two writers into one head, by the exact route this worker exists to
    close - just half a round trip later than the case above.
    """

    goto = _ActionClient(hang_acceptance=True)
    clients = _clients(goto=goto)
    transport = _transport(clients, timeout_s=0.2)
    answers: list = []

    def _submit(reason):
        answers.append(transport.submit(
            {"skill": "replay_motion", "name": "nod"}, reason))

    first = threading.Thread(target=_submit, args=("engagement:nod",), daemon=True)
    second = threading.Thread(target=_submit, args=("cue:levity",), daemon=True)
    first.start()
    try:
        assert _until(lambda: bool(goto.sent)), "no goal was ever sent"
        handle = goto.handles[0]
        assert handle.cancelled is False

        # Supersede it while the server has still not answered the FIRST goal.
        second.start()
        assert _until(lambda: transport.counters()["batches_cancelled"] == 1)
        assert _until(lambda: bool(answers) and answers[0].error_code == "cancelled")

        # ...and only now does the server accept it. Nobody is waiting any more, and
        # that is precisely why it would otherwise run unopposed.
        goto.accept_now(0)
        assert _until(lambda: handle.cancelled), \
            "a goal accepted after its batch was superseded was left running"
    finally:
        transport.close()
        first.join(2.0)
        second.join(2.0)


def test_a_goal_the_server_rejects_after_the_batch_was_superseded_is_not_cancelled():
    """A rejected goal is not running, so cancelling it would be a request the server
    has to answer with an error for no reason."""

    goto = _ActionClient(hang_acceptance=True, accepted=False)
    clients = _clients(goto=goto)
    transport = _transport(clients, timeout_s=0.2)
    batch = native._Batch(specs=[native.GoalSpec(kind="goto", reason="x")], reason="x")
    batch.cancelled.set()

    assert transport._send_goal(goto, _goto_goal, native.fill_goto_goal,
                                batch.specs[0], batch) is None
    goto.accept_now(0)
    assert goto.handles[0].cancelled is False
    transport.close()


# --- the registry: how the facade finds this transport at all -------------------------


def test_importing_the_package_registers_the_transport_by_name():
    """The belt to the entry point's braces: an overlay whose distribution metadata is
    unreadable still has a selectable `ros_native` if anything imported the package."""

    importlib.import_module("emori_skill_native")

    assert transports.REGISTERED_TRANSPORTS["ros_native"] is native.make_transport
    assert "ros_native" in transports.known_transports()
    # ...and it did not become a built-in: what this package ships stays readable.
    assert set(transports.TRANSPORTS) == {"hostapp_http", "log"}


def test_select_transport_builds_the_native_transport_and_hands_it_the_node():
    """`node=` is the whole reason the factory signature grew one: an HTTP transport
    needs a URL, a ROS transport needs the node its clients live on."""

    importlib.import_module("emori_skill_native")
    node = object()

    built = transports.select_transport("  ROS_Native ", url="http://h/api/skill",
                                        key="k", timeout_s=0.5, node=node)
    try:
        assert isinstance(built, native.NativeTransport)
        assert built.name == "ros_native"
        assert built._node is node
        assert built.timeout_s == pytest.approx(0.5)
        assert "ros_native" in built.describe()
    finally:
        built.close()


def test_an_entry_point_resolves_the_transport_with_nobody_importing_it(monkeypatch):
    """The route the container actually takes: `EMORI_SKILL_TRANSPORT=ros_native` with
    no import of this package anywhere in the facade."""

    monkeypatch.delitem(transports.REGISTERED_TRANSPORTS, "ros_native", raising=False)
    point = types.SimpleNamespace(name="ros_native", load=lambda: native.make_transport)
    monkeypatch.setattr(transports, "_entry_points", lambda: [point])

    built = transports.select_transport("ros_native", node=None, timeout_s=0.4)
    try:
        assert isinstance(built, native.NativeTransport)
        assert "ros_native" in transports.known_transports()
    finally:
        built.close()


def test_a_plugin_that_cannot_be_loaded_is_named_and_skipped_rather_than_raised(
        monkeypatch):
    """Somebody else's broken package must not take the facade down: the name is then
    honestly unknown, which is what it is - it could not be loaded."""

    monkeypatch.delitem(transports.REGISTERED_TRANSPORTS, "ros_native", raising=False)

    def _explode():
        raise ImportError("no rclpy here")

    point = types.SimpleNamespace(name="ros_native", load=_explode)
    monkeypatch.setattr(transports, "_entry_points", lambda: [point])
    said: list = []

    with pytest.raises(ValueError) as raised:
        transports.select_transport(
            "ros_native", log=types.SimpleNamespace(warn=said.append))

    assert "ros_native" in str(raised.value)
    assert said and "failed to load" in said[0]
