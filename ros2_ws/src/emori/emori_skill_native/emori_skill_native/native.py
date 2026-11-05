"""``ros_native`` - the skill facade's transport for a robot that IS a ROS 2 node.

In v1 a skill request left the graph over HTTP: the facade POSTed the hostapp's
single-writer intake, the hostapp's ``RobotPlayer`` turned it into intents and the
Reachy daemon executed them.  In v2 the robot is a node in this same graph
(``reachy_mini_ros2``), it owns Pollen's backend in-process, and its arbiter holds the
interlocks the hostapp's queue holds on v1 - lanes, preemption, drop-if-busy, head
reservation, owner lock.  So the hop disappears: a request becomes ``GoTo`` and
``PlayRecordedMove`` goals on the robot node's action servers, in the same process
graph, with no HTTP server in the path and no daemon at all.

Three things are deliberate.

**The mapping is the hostapp's own.**  ``skill_to_intents`` (the adapter core) is what
the hostapp's ``/api/skill`` intake calls, so a ``replay_motion nod`` performs the same
gesture whichever side asked for it, and a bounds error is refused with the same
``bad_skill`` / ``out_of_range`` code.  That is the whole reason this package imports
the core and therefore lives under ``ros2_ws/src/emori/``, not with the standalone
packages.

**The refusal vocabulary is shared on purpose.**  The robot node answers ``dropped`` /
``not_owner`` / ``head_reserved`` because those are the words the hostapp intake
answered with, so ``hri_engagement_behaviors`` reads one vocabulary whichever transport
is plugged in and the ``/diagnostics`` ledgers of v1 and v2 rehearsals compare directly.

**One batch at a time, and a newer batch wins.**  A skill request expands to several
intents (a cue plan is a shape, not a pose).  The hostapp paced them by sleeping each
intent's duration on its one player thread; here they are goals, sent one after the
other on one worker thread, and a newer batch cancels what is left of the previous one -
because motion that outlives the decision that asked for it is the thing every lane in
this system exists to prevent.  The transport's own answer is the FIRST goal's result:
that is what says "the robot accepted this", and waiting for the whole shape would hold
an action server open for the length of a gesture.

The top of this module is pure: ``intents_to_requests`` and ``first_result_outcome``
need no ROS, no node and no robot, which is what lets ``tests/test_skill_native.py``
check the units, the antenna order and the lanes on a machine that has none of them.
The ``rclpy`` imports below are guarded for the same reason the facade's are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import queue
import threading
import time

from emori_adapter.behavior import CueConfig, SkillError, skill_to_intents
from emori_skill_facade.contract import SkillOutcome, unreachable_outcome
from emori_skill_facade.transport import DEFAULT_TIMEOUT_S, SkillTransport

try:  # pragma: no cover - present in the v2 container, never on a dev box
    from rclpy.action import ActionClient
    from rclpy.callback_groups import ReentrantCallbackGroup

    from reachy_mini_msgs.action import GoTo, PlayRecordedMove
    from reachy_mini_msgs.msg import BodyTarget
    from std_srvs.srv import Trigger

    ROS_AVAILABLE = True
except ImportError:  # the pure half must import on a machine with no ROS at all
    ROS_AVAILABLE = False

# The robot node's surface. Named here, once, because the contract test
# walks these strings to prove every client in the graph has a server.
GOTO_ACTION = "/reachy_mini/goto"
PLAY_RECORDED_ACTION = "/reachy_mini/play_recorded_move"
SET_TARGET_TOPIC = "/reachy_mini/set_target"
WAKE_UP_SERVICE = "/reachy_mini/wake_up"

# Pollen's official emotion library, the same dataset the hostapp's client plays from.
RECORDED_DATASET = "pollen-robotics/reachy-mini-emotions-library"

# Who this is, to the arbiter. `client` is an identity (the owner lock is per client);
# `lane` is a priority. A skill request is expression: it yields to safety and to the
# interaction mechanics, and it is the lane drop-if-busy applies to.
NATIVE_CLIENT = "ros"
EXPRESSION_LANE = "expression"
SAFETY_LANE = "safety"

# ...except a de-escalation settle, which is not expression at all: it is the calm the
# escalation policy owes a person, and in v1 it was exempted from the ownership gate for
# exactly that reason. These two prefixes are what `hri_engagement_behaviors/policy.py`
# stamps on a `calm_settle` decision; on the safety lane the arbiter cancels the running
# move and flushes queued expression, which is the behaviour v1 got from being exempt.
SAFETY_REASONS = ("engagement:deescalate", "cue:end")

# Intent kinds this transport has nowhere to send. The robot node carries no media (v2
# runs the backend with `--no-media`'s equivalent), so a sound or a wobble is counted and
# dropped rather than silently lost - a rehearsal ledger with `skipped_play_sound` in it
# is the difference between "the audio path is gone" and "the audio path is broken".
SKIPPED_KINDS = ("play_sound", "wobble")

# A goal that is refused answers with a code; a goal that is not completed and says
# nothing at all gets this rather than an invented meaning.
UNNAMED_REFUSAL = "unknown"

# How long past a goal's own duration we wait for its result before calling it a
# timeout: the action round trip plus the arbiter's queue, not the gesture.
RESULT_MARGIN_S = 2.0
# A recorded move's length is the recording's, which the intent cannot know (the hostapp
# guesses it too). Bound the wait instead: the library's longest moves are ~5 s.
RECORDED_BUDGET_S = 15.0
# Slice for the interruptible waits, so a superseding batch is honoured promptly.
_POLL_S = 0.05

# rclpy's logger says `warn`; the stdlib's says `warning` and dropped `warn` in 3.13.
_LOG_ALIASES = {"warn": ("warn", "warning"), "warning": ("warning", "warn")}


def _say(log, level: str, message: str) -> None:
    """Log through whatever logger-shaped object we were given, or stay silent."""

    if log is None:
        return
    for name in _LOG_ALIASES.get(level, (level,)):
        method = getattr(log, name, None)
        if callable(method):
            method(message)
            return


# --- the pure half: intents in, goal specifications out -------------------------------


@dataclass(frozen=True)
class GoalSpec:
    """One robot-node request, as plain data - no ROS type, no node, no socket.

    This is the whole unit conversion and ordering contract in one value, which is why
    it exists instead of building messages inline: ``tests/test_skill_native.py`` can
    assert that 10 mm became 0.01 m and that an authored ``(left, right)`` posture
    reached the wire as ``[right, left]`` without a robot in the room.
    """

    #: goto | set_target | play_recorded | wake_up | skipped
    kind: str
    use_head: bool = False
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)          # metres
    orientation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)   # x, y, z, w
    use_antennas: bool = False
    antennas: tuple[float, float] = (0.0, 0.0)  # radians, [right, left] - the wire order
    use_body_yaw: bool = False
    body_yaw: float = 0.0                       # radians
    duration: float = 0.5                       # seconds
    interpolation: str = "minjerk"
    lane: str = EXPRESSION_LANE
    client: str = NATIVE_CLIENT
    preempt: bool = False
    reason: str = ""
    dataset: str = ""
    move: str = ""
    skipped_kind: str = ""


def rpy_to_quaternion(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    """Radians roll/pitch/yaw -> ``(x, y, z, w)``, in Pollen's own convention.

    Pollen builds every head pose with ``scipy Rotation.from_euler("xyz", [r, p, y])``
    (extrinsic xyz, i.e. ``Rz(yaw) @ Ry(pitch) @ Rx(roll)``) - in ``daemon/app/models.py``
    for the REST pose, in ``utils.create_head_pose`` for the SDK one.  The robot node's
    ``conversions.py`` reads the same ``geometry_msgs/Pose`` back into that matrix, so
    getting this order wrong would not fail: the head would simply tilt where it was
    asked to turn.  ``tests/test_skill_native.py`` pins it against scipy.
    """

    half_r, half_p, half_y = roll * 0.5, pitch * 0.5, yaw * 0.5
    sr, cr = math.sin(half_r), math.cos(half_r)
    sp, cp = math.sin(half_p), math.cos(half_p)
    sy, cy = math.sin(half_y), math.cos(half_y)
    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


def batch_lane(reason: str) -> str:
    """Which arbiter lane a batch belongs to, from the decision's own trace slug.

    Everything this transport sends is expression except the de-escalation settle:
    safety outranks expression, so the calm a person is owed cannot be dropped by the
    drop-if-busy rule that exists to stop expression flooding the queue.
    """

    slug = str(reason or "")
    return SAFETY_LANE if slug.startswith(SAFETY_REASONS) else EXPRESSION_LANE


def _head_fields(head) -> tuple[tuple[float, float, float],
                               tuple[float, float, float, float]]:
    """``HeadPose`` (mm, degrees) -> metres and a quaternion, the one conversion point."""

    position = (float(head.x) / 1000.0, float(head.y) / 1000.0, float(head.z) / 1000.0)
    orientation = rpy_to_quaternion(math.radians(float(head.roll)),
                                    math.radians(float(head.pitch)),
                                    math.radians(float(head.yaw)))
    return position, orientation


def _pose_spec(intent, kind: str, lane: str) -> GoalSpec:
    """The shared body of every intent that commands a pose (goto / look_at / antennas)."""

    head = getattr(intent, "head", None)
    antennas = getattr(intent, "antennas", None)
    body_yaw = getattr(intent, "body_yaw", None)
    position, orientation = _head_fields(head) if head is not None else ((0.0, 0.0, 0.0),
                                                                        (0.0, 0.0, 0.0, 1.0))
    return GoalSpec(
        kind=kind,
        use_head=head is not None,
        position=position,
        orientation=orientation,
        use_antennas=antennas is not None,
        # Authored as (left, right) - how a human reads a gesture - and swapped HERE,
        # at the transport boundary, exactly where `robot/client.py::_antennas_wire`
        # swaps it for the daemon. Every asymmetric gesture (wink, wiggle, curious
        # tilt) lands on the intended physical antenna instead of its mirror image.
        antennas=((float(antennas[1]), float(antennas[0])) if antennas is not None
                  else (0.0, 0.0)),
        use_body_yaw=body_yaw is not None,
        body_yaw=float(body_yaw) if body_yaw is not None else 0.0,
        duration=float(getattr(intent, "duration", 0.5) or 0.0),
        interpolation=str(getattr(intent, "interpolation", "minjerk")),
        lane=lane,
        reason=str(getattr(intent, "reason", "")),
    )


def intents_to_requests(intents, *, reason: str = "", lane: str = "") -> list[GoalSpec]:
    """One skill request's intents -> the goals the robot node will be asked for.

    Pure, and the only place units, antenna order, lanes and the recorded-move dataset
    are decided.  ``lane`` overrides the reason-derived one for a caller that already
    knows (the arbiter's safety lane is not something to infer twice).
    """

    chosen = str(lane or "").strip() or batch_lane(reason)
    specs: list[GoalSpec] = []
    for intent in intents or ():
        kind = str(getattr(intent, "kind", ""))
        if kind in ("goto", "look_at", "antennas"):
            specs.append(_pose_spec(intent, "goto", chosen))
        elif kind == "set_target":
            specs.append(_pose_spec(intent, "set_target", chosen))
        elif kind == "play_recorded":
            specs.append(GoalSpec(kind="play_recorded", dataset=RECORDED_DATASET,
                                  move=str(getattr(intent, "move", "") or ""),
                                  duration=float(getattr(intent, "duration", 0.5) or 0.0),
                                  lane=chosen,
                                  reason=str(getattr(intent, "reason", ""))))
        elif kind == "wake_up":
            specs.append(GoalSpec(kind="wake_up", lane=chosen,
                                  reason=str(getattr(intent, "reason", ""))))
        else:
            # play_sound / wobble / anything a future intent kind adds: counted by name
            # so a ledger says which capability was asked for and is not there.
            specs.append(GoalSpec(kind="skipped", skipped_kind=kind or "unknown",
                                  lane=chosen,
                                  reason=str(getattr(intent, "reason", ""))))
    return specs


def first_result_outcome(result) -> SkillOutcome:
    """The batch's answer, from the FIRST goal's result - or ``None`` for no answer.

    The vocabulary is the facade's: ``dropped`` is an honest 200-shaped answer (the
    arbiter was busy with motion that outranks expression) and gets its own counter,
    ``not_owner`` / ``head_reserved`` are refusals, and a node that never answered is
    ``unreachable`` and never confused with one that said no.
    """

    if result is None:
        return unreachable_outcome()
    if bool(getattr(result, "completed", False)):
        return SkillOutcome(True, "", "accepted")
    code = str(getattr(result, "error_code", "") or "")
    if code == "dropped":
        return SkillOutcome(False, "dropped", "dropped")
    return SkillOutcome(False, code or UNNAMED_REFUSAL, "refused")


# --- filling the ROS messages (pure over any message-shaped object) -------------------


def fill_goto_goal(goal, spec: GoalSpec):
    """Fill a ``reachy_mini_msgs/action/GoTo`` goal from a spec, and return it."""

    goal.use_head = bool(spec.use_head)
    goal.head_pose.position.x, goal.head_pose.position.y, goal.head_pose.position.z = (
        float(spec.position[0]), float(spec.position[1]), float(spec.position[2]))
    (goal.head_pose.orientation.x, goal.head_pose.orientation.y,
     goal.head_pose.orientation.z, goal.head_pose.orientation.w) = (
        float(spec.orientation[0]), float(spec.orientation[1]),
        float(spec.orientation[2]), float(spec.orientation[3]))
    goal.use_antennas = bool(spec.use_antennas)
    goal.antennas = [float(spec.antennas[0]), float(spec.antennas[1])]
    goal.use_body_yaw = bool(spec.use_body_yaw)
    goal.body_yaw = float(spec.body_yaw)
    goal.duration = float(spec.duration)
    goal.interpolation = str(spec.interpolation)
    goal.lane = str(spec.lane)
    goal.client = str(spec.client)
    goal.preempt = bool(spec.preempt)
    goal.reason = str(spec.reason)
    return goal


def fill_recorded_goal(goal, spec: GoalSpec):
    """Fill a ``reachy_mini_msgs/action/PlayRecordedMove`` goal, and return it."""

    goal.dataset = str(spec.dataset or RECORDED_DATASET)
    goal.name = str(spec.move)
    goal.lane = str(spec.lane)
    goal.client = str(spec.client)
    goal.preempt = bool(spec.preempt)
    goal.reason = str(spec.reason)
    return goal


def fill_body_target(msg, spec: GoalSpec):
    """Fill a ``reachy_mini_msgs/msg/BodyTarget`` from a ``set_target`` spec.

    The header stamp is left to the publisher: this function must stay callable with no
    clock, which is what makes it testable off the robot.
    """

    msg.use_head = bool(spec.use_head)
    msg.head_pose.position.x, msg.head_pose.position.y, msg.head_pose.position.z = (
        float(spec.position[0]), float(spec.position[1]), float(spec.position[2]))
    (msg.head_pose.orientation.x, msg.head_pose.orientation.y,
     msg.head_pose.orientation.z, msg.head_pose.orientation.w) = (
        float(spec.orientation[0]), float(spec.orientation[1]),
        float(spec.orientation[2]), float(spec.orientation[3]))
    msg.use_antennas = bool(spec.use_antennas)
    msg.antennas = [float(spec.antennas[0]), float(spec.antennas[1])]
    msg.use_body_yaw = bool(spec.use_body_yaw)
    msg.body_yaw = float(spec.body_yaw)
    return msg


# --- the ROS half --------------------------------------------------------------------


@dataclass
class NativeClients:
    """Everything this transport needs from ROS, in one injectable bundle.

    Clients AND message constructors, because a test on a machine with no ROS has
    neither: the seam has to be here rather than at ``rclpy`` or the mapping above would
    be the only thing ever exercised.
    """

    goto: object | None = None           # ActionClient(GoTo)
    recorded: object | None = None       # ActionClient(PlayRecordedMove)
    wake: object | None = None           # service client (std_srvs/Trigger)
    targets: object | None = None        # publisher (reachy_mini_msgs/BodyTarget)
    goto_goal: object | None = None      # () -> GoTo.Goal()
    recorded_goal: object | None = None  # () -> PlayRecordedMove.Goal()
    body_target: object | None = None    # () -> BodyTarget()
    trigger: object | None = None        # () -> Trigger.Request()


def build_clients(node) -> NativeClients:
    """The real thing: action clients, a service client and a publisher on the node."""

    if not ROS_AVAILABLE:  # pragma: no cover - guarded import, tested off the robot
        raise RuntimeError(
            "emori_skill_native needs ROS 2 (rclpy + reachy_mini_msgs + std_srvs); it "
            "runs inside the v2 container next to the robot node, never on the host")
    # Their own reentrant group: these clients' replies must not queue behind the node's
    # default (mutually exclusive) group, whose timer publishes the facade's ledger. A
    # goal response that cannot be delivered while the batch worker is waiting for it is
    # a deadlock that reads, on the robot, as a robot that stopped moving.
    group = ReentrantCallbackGroup()
    return NativeClients(
        goto=ActionClient(node, GoTo, GOTO_ACTION, callback_group=group),
        recorded=ActionClient(node, PlayRecordedMove, PLAY_RECORDED_ACTION,
                              callback_group=group),
        wake=node.create_client(Trigger, WAKE_UP_SERVICE, callback_group=group),
        targets=node.create_publisher(BodyTarget, SET_TARGET_TOPIC, 10),
        goto_goal=GoTo.Goal,
        recorded_goal=PlayRecordedMove.Goal,
        body_target=BodyTarget,
        trigger=Trigger.Request,
    )


@dataclass
class _Batch:
    """One skill request's goals, and the way to stop them."""

    specs: list[GoalSpec]
    reason: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    first: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))

    def answer(self, outcome: SkillOutcome) -> None:
        """Hand the caller the first goal's outcome, once."""

        try:
            self.first.put_nowait(outcome)
        except queue.Full:  # pragma: no cover - only the first goal answers
            pass


class BatchWorker:
    """Runs one batch at a time; a newer batch cancels what is left of the previous one.

    The hostapp's ``RobotPlayer`` is the shape this mirrors: one thread, one batch, and
    a generation guard so a batch queued before the world changed never executes.  Here
    the newer batch IS the guard - it sets the older one's cancel event, the loop stops
    between goals, and the goal in flight is cancelled through the action server, which
    is the arbiter's own ``stop``.  Without it a 2 s cue plan would still be playing
    while the next sentence's plan started: two writers into one head.
    """

    def __init__(self, run_spec, *, log=None) -> None:
        self._run_spec = run_spec
        self._log = log
        self._lock = threading.Lock()
        self._pending: _Batch | None = None
        self._current: _Batch | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.batches_cancelled = 0
        self._thread = threading.Thread(target=self._loop, name="ros_native_batch",
                                        daemon=True)
        self._thread.start()

    def submit(self, batch: _Batch) -> None:
        """Queue a batch, superseding whatever was running or waiting."""

        with self._lock:
            for older in (self._current, self._pending):
                if older is not None and not older.cancelled.is_set():
                    older.cancelled.set()
                    self.batches_cancelled += 1
            self._pending = batch
        self._wake.set()

    def stop(self, timeout_s: float = 1.0) -> None:
        """Stop the worker; a transport outlives the facade node by no more than this."""

        self._stop.set()
        with self._lock:
            for batch in (self._current, self._pending):
                if batch is not None:
                    batch.cancelled.set()
        self._wake.set()
        self._thread.join(timeout_s)

    def _take(self) -> _Batch | None:
        with self._lock:
            batch, self._pending = self._pending, None
            self._current = batch
            if batch is None:
                self._wake.clear()
            return batch

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(_POLL_S):
                continue
            batch = self._take()
            if batch is None:
                continue
            try:
                self._run(batch)
            except Exception as error:  # noqa: BLE001 - a batch must never kill the thread
                _say(self._log, "warn", f"native batch failed: {error}")
                batch.answer(SkillOutcome(False, "backend_error", "refused"))
            finally:
                with self._lock:
                    if self._current is batch:
                        self._current = None

    def _run(self, batch: _Batch) -> None:
        answered = False
        for spec in batch.specs:
            if batch.cancelled.is_set() or self._stop.is_set():
                break
            outcome = self._run_spec(spec, batch)
            if not answered and outcome is not None:
                answered = True
                batch.answer(outcome)
        if not answered:
            # Superseded (or nothing to do) before the first goal ever answered: that is
            # not a refusal by the robot and must not be counted as one.
            batch.answer(SkillOutcome(False, "cancelled", "refused"))


def _wait(future, timeout_s: float, abort=None):
    """Block on an rclpy future without spinning, and give up on time or on abort.

    The facade node's executor is what completes these futures; calling
    ``spin_until_future_complete`` from inside one of that executor's callbacks
    deadlocks the single-threaded case and re-enters the multi-threaded one.  An event
    fed by ``add_done_callback`` needs neither.
    """

    done = threading.Event()
    future.add_done_callback(lambda _f: done.set())
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while not done.wait(_POLL_S):
        if abort is not None and abort():
            return None
        if time.monotonic() >= deadline:
            return None
    return future.result()


class NativeTransport(SkillTransport):
    """Send a bounded skill request straight to the robot node's action servers.

    No HTTP, no hostapp, no daemon: the single-writer rule is kept by the robot node's
    arbiter, which is the only thing that touches Pollen's backend, and this transport
    is one of its clients (``client="ros"``) exactly as the hostapp is another.
    """

    name = "ros_native"

    def __init__(self, *, node=None, timeout_s: float = DEFAULT_TIMEOUT_S, log=None,
                 config=None, clients: NativeClients | None = None) -> None:
        self._node = node
        self.timeout_s = max(0.1, float(timeout_s))
        self._log = log
        # The SAME limits the hostapp's own cue lane plans against, read once at startup
        # exactly as `skill_api.py` reads them: a robot whose EMORI_CUE_Z_MM was lowered
        # to what its Stewart platform survives must not be overridden by the lane that
        # happens to originate in ROS.
        self.config = config if isinstance(config, CueConfig) else CueConfig.from_env()
        if clients is None and node is not None and ROS_AVAILABLE:  # pragma: no cover
            clients = build_clients(node)
        self.clients = clients if clients is not None else NativeClients()
        self.ledger: dict[str, int] = {
            "goals_sent": 0, "goals_completed": 0, "batches_cancelled": 0,
            "targets_published": 0, "unreachable": 0,
        }
        self._lock = threading.Lock()
        self._worker = BatchWorker(self._run_spec, log=log)

    # --- ledger ---------------------------------------------------------------
    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.ledger[name] = self.ledger.get(name, 0) + amount

    def counters(self) -> dict:
        """The ledger, including the worker's cancellation count."""

        with self._lock:
            counters = dict(self.ledger)
        counters["batches_cancelled"] = self._worker.batches_cancelled
        return counters

    def describe(self) -> str:
        ready = "node attached" if self.clients.goto is not None else "NO NODE (nothing sends)"
        return (f"ros_native -> {GOTO_ACTION} + {PLAY_RECORDED_ACTION} on the robot node "
                f"[lane {EXPRESSION_LANE}, client {NATIVE_CLIENT}, "
                f"timeout {self.timeout_s:.1f}s, {ready}]")

    # --- the one hop, which is no longer a hop --------------------------------
    def submit(self, body: dict, reason: str) -> SkillOutcome:
        """Map one bounded skill request onto goals and answer with the first result."""

        skill = str((body or {}).get("skill", ""))
        try:
            intents = skill_to_intents(skill, dict(body or {}), config=self.config)
        except SkillError as error:
            # The same two codes the hostapp intake answers 400 with, so a behaviour
            # node cannot tell which side did the bounds check - and must not have to.
            self._bump("goals_refused_" + error.code)
            _say(self._log, "debug", f"{skill} refused: {error.code} ({reason})")
            return SkillOutcome(False, error.code, "refused")
        except Exception as error:  # noqa: BLE001 - a mapping fault must not kill the server
            _say(self._log, "warn", f"{skill} could not be planned ({error})")
            return SkillOutcome(False, "backend_error", "refused")

        specs = intents_to_requests(intents, reason=reason)
        for spec in specs:
            if spec.kind == "skipped":
                self._bump("skipped_" + spec.skipped_kind)
        actionable = [spec for spec in specs if spec.kind != "skipped"]
        if not actionable:
            # Nothing was refused and nothing can move: an honest acceptance, so a
            # sound-only request does not read as an outage on the facade's ledger.
            return SkillOutcome(True, "", "accepted")

        if not self._ready(actionable[0]):
            self._bump("unreachable")
            _say(self._log, "warn",
                 f"the robot node did not answer within {self.timeout_s:.1f}s "
                 f"({GOTO_ACTION}); nothing was sent")
            return unreachable_outcome()

        batch = _Batch(specs=actionable, reason=str(reason))
        self._worker.submit(batch)
        budget = self.timeout_s + _result_budget(actionable[0])
        try:
            return batch.first.get(timeout=budget)
        except queue.Empty:
            # The goal was accepted but no result came back inside its own duration plus
            # the round trip. Not "unreachable" (the server answered) and not a refusal
            # anybody made: the arbiter's own word for it.
            return SkillOutcome(False, "timeout", "refused")

    def close(self) -> None:
        """Stop the worker thread. The facade node owns the clients; ROS frees those."""

        self._worker.stop()

    # --- one goal -------------------------------------------------------------
    def _ready(self, spec: GoalSpec) -> bool:
        """Is the server this batch starts with there at all?"""

        if spec.kind == "play_recorded":
            client = self.clients.recorded
        elif spec.kind == "wake_up":
            client = self.clients.wake
        elif spec.kind == "set_target":
            return self.clients.targets is not None
        else:
            client = self.clients.goto
        if client is None:
            return False
        waiter = getattr(client, "wait_for_server", None) or getattr(
            client, "wait_for_service", None)
        if waiter is None:  # pragma: no cover - every rclpy client has one
            return True
        return bool(waiter(timeout_sec=self.timeout_s))

    def _run_spec(self, spec: GoalSpec, batch: _Batch) -> SkillOutcome | None:
        """Perform one spec. ``None`` means "no answer worth reporting" (cancelled)."""

        if spec.kind == "set_target":
            return self._publish_target(spec)
        if spec.kind == "wake_up":
            return self._call_wake_up(batch)
        if spec.kind == "play_recorded":
            return self._send_goal(self.clients.recorded, self.clients.recorded_goal,
                                   fill_recorded_goal, spec, batch)
        return self._send_goal(self.clients.goto, self.clients.goto_goal,
                               fill_goto_goal, spec, batch)

    def _publish_target(self, spec: GoalSpec) -> SkillOutcome:
        publisher, factory = self.clients.targets, self.clients.body_target
        if publisher is None or factory is None:
            return unreachable_outcome()
        message = fill_body_target(factory(), spec)
        stamp = getattr(self._node, "get_clock", None)
        if stamp is not None and hasattr(message, "header"):  # pragma: no cover - needs ROS
            try:
                message.header.stamp = self._node.get_clock().now().to_msg()
            except Exception:  # noqa: BLE001 - a stamp is provenance, never the message
                pass
        publisher.publish(message)
        self._bump("targets_published")
        return SkillOutcome(True, "", "accepted")

    def _call_wake_up(self, batch: _Batch) -> SkillOutcome | None:
        client, factory = self.clients.wake, self.clients.trigger
        if client is None or factory is None:
            return unreachable_outcome()
        self._bump("goals_sent")
        future = client.call_async(factory())
        response = _wait(future, self.timeout_s + RESULT_MARGIN_S,
                         abort=batch.cancelled.is_set)
        if response is None:
            return None if batch.cancelled.is_set() else unreachable_outcome()
        if bool(getattr(response, "success", False)):
            self._bump("goals_completed")
            return SkillOutcome(True, "", "accepted")
        self._bump("goals_refused_wake_up")
        return SkillOutcome(False, "wake_up_refused", "refused")

    def _send_goal(self, client, factory, fill, spec: GoalSpec,
                   batch: _Batch) -> SkillOutcome | None:
        if client is None or factory is None:
            return unreachable_outcome()
        goal = fill(factory(), spec)
        self._bump("goals_sent")
        sent = client.send_goal_async(goal)
        handle = _wait(sent, self.timeout_s, abort=batch.cancelled.is_set)
        if handle is None:
            if batch.cancelled.is_set():
                # The goal was SENT. Giving up on the handle does not un-send it, and
                # the server is free to accept it a moment later - at which point a move
                # the superseded batch asked for starts under the batch that superseded
                # it: two writers into one head, which is the exact failure this worker
                # exists to prevent. So cancel it when the handle finally arrives.
                self._cancel_when_resolved(sent)
                return None
            return unreachable_outcome()
        if not bool(getattr(handle, "accepted", True)):
            # The arbiter answers a refusal through the RESULT (succeed with
            # completed=false), so a rejected goal means the server itself said no.
            self._bump("goals_refused_rejected")
            return SkillOutcome(False, "rejected", "refused")
        wrapped = _wait(handle.get_result_async(), _result_budget(spec),
                        abort=batch.cancelled.is_set)
        if wrapped is None:
            if batch.cancelled.is_set():
                self._cancel(handle)
                return None
            return SkillOutcome(False, "timeout", "refused")
        outcome = first_result_outcome(getattr(wrapped, "result", wrapped))
        if outcome.accepted:
            self._bump("goals_completed")
        else:
            self._bump("goals_refused_" + (outcome.error_code or UNNAMED_REFUSAL))
        return outcome

    def _cancel_when_resolved(self, sent) -> None:
        """Cancel a goal whose handle arrives after its batch was superseded.

        ``_wait`` gave up on the abort flag, not on the future: rclpy will still deliver
        the goal handle on the executor thread whenever the server answers. Attaching a
        second done-callback is how a caller that has already walked away still gets to
        clean up - and it is why ``_wait`` uses ``add_done_callback`` rather than
        consuming the future.
        """

        def _on_handle(future) -> None:
            try:
                handle = future.result()
            except Exception as error:  # noqa: BLE001 - a goal that failed to send
                _say(self._log, "debug",
                     f"a superseded goal never produced a handle: {error}")
                return
            if handle is not None and bool(getattr(handle, "accepted", True)):
                self._cancel(handle)

        try:
            sent.add_done_callback(_on_handle)
        except Exception as error:  # noqa: BLE001 - never worth failing a batch over
            _say(self._log, "debug", f"could not arm a superseded-goal cancel: {error}")

    def _cancel(self, handle) -> None:
        """Cut a goal a newer batch superseded - the arbiter's own ``stop``."""

        cancel = getattr(handle, "cancel_goal_async", None)
        if not callable(cancel):
            return
        try:
            cancel()
        except Exception as error:  # noqa: BLE001 - a failed cancel must not kill the batch
            _say(self._log, "debug", f"cancelling a superseded goal failed: {error}")


def _result_budget(spec: GoalSpec) -> float:
    """How long a goal's result may take: its own length plus the round trip."""

    if spec.kind == "play_recorded":
        return RECORDED_BUDGET_S
    return max(0.0, float(spec.duration)) + RESULT_MARGIN_S


def make_transport(**kw) -> NativeTransport:
    """The factory the entry point publishes.

    ``url`` and ``key`` belong to an HTTP intake and are accepted and ignored so every
    transport shares one factory signature - the facade calls every factory the same
    way and must not learn which ones speak which wire.
    """

    return NativeTransport(node=kw.get("node"),
                           timeout_s=kw.get("timeout_s") or DEFAULT_TIMEOUT_S,
                           log=kw.get("log"),
                           config=kw.get("config"),
                           clients=kw.get("clients"))
