"""When the hostapp asks the robot node to look at a face, and when it stops asking.

Two gates live on the v2 robot node and neither can be computed there: whether a face
look-at is WANTED right now (``/api/emori/gaze``) and whether a human has hold of the
head (``/api/emori/head_reserved``). The hostapp is the only process that knows either,
so it pushes both -- on an edge, idempotently, and with a forced assertion at startup
because the node's run flag is fail-closed.

The regression this file exists for is the first draft's fatal bug: gaze was computed as
``gaze_allowed() and not head_reserved()``, and ``head_reserved_for_sensing()`` is true
in every add-on state -- so the feature would have been off for exactly the modes that
need it. There is an explicit row for it below.
"""

from __future__ import annotations

from emori_adapter.hostapp.driver import LiveDriver, SessionInfo
from emori_adapter.robot.client import DryRunRobotClient


class _Player:
    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []

    def submit(self, intents, *, drop_if_busy: bool = False, guard=None) -> bool:
        self.batches.append(list(intents))
        return True


def _driver(client=None) -> LiveDriver:
    return LiveDriver(client or DryRunRobotClient(), player=_Player(), fiware_factory=None)


# --- gaze_allowed: when the robot may look at a face -------------------------------


def test_a_training_session_wants_the_robot_to_look_at_the_learner():
    driver = _driver()
    assert driver.gaze_allowed() is False

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    assert driver.gaze_allowed() is True


def test_head_reserved_does_not_force_gaze_off():
    """Regression guard: a conjunction here would have deleted the whole feature.

    ``head_reserved_for_sensing()`` is true whenever push_turn is on and no training
    session runs, so ANDing it into gaze_allowed made the answer False for every add-on
    request -- i.e. gaze would never have run outside a training session at all. The two coexist
    through residual sensing, not through mutual exclusion.
    """

    driver = _driver()
    driver.set_head_reserved(lambda: True)
    driver.set_gaze_requested(True)

    assert driver.head_reserved() is True
    assert driver.gaze_allowed() is True


# --- sync_robot_flags: edges, retries, and the startup assertion -------------------


def test_the_flags_are_pushed_only_on_an_edge():
    client = DryRunRobotClient()
    driver = _driver(client)
    driver.set_head_reserved(lambda: False)

    for _ in range(10):
        driver.sync_robot_flags()

    # Ten reconciler ticks, one POST each: the 10 Hz ticker must cost nothing on a
    # quiet robot.
    assert client.gaze_calls == [False]
    assert client.head_reserved_calls == [False]

    driver.set_gaze_requested(True)          # set_gaze_requested syncs for us
    for _ in range(10):
        driver.sync_robot_flags()
    assert client.gaze_calls == [False, True]
    assert client.head_reserved_calls == [False]


def test_a_changing_predicate_is_carried_by_the_reconciler():
    """head_reserved is a predicate, not an event: only polling can carry it."""

    client = DryRunRobotClient()
    driver = _driver(client)
    held = {"value": False}
    driver.set_head_reserved(lambda: held["value"])

    driver.sync_robot_flags()
    held["value"] = True
    driver.sync_robot_flags()
    driver.sync_robot_flags()
    held["value"] = False
    driver.sync_robot_flags()

    assert client.head_reserved_calls == [False, True, False]


def test_an_unacknowledged_push_is_retried_on_the_next_tick():
    """v1's 404 and a transport blip both answer False, and both must retry.

    Latching the pushed state on a refusal would mean a node that came back up after a
    restart stayed on whatever flag it booted with, for ever, with nothing in the logs.
    """

    class _RefusingClient(DryRunRobotClient):
        def __init__(self) -> None:
            super().__init__()
            self.accept = False

        def set_gaze(self, enabled: bool) -> bool:
            super().set_gaze(enabled)
            return self.accept

    client = _RefusingClient()
    driver = _driver(client)
    driver.set_head_reserved(lambda: False)

    driver.sync_robot_flags()
    driver.sync_robot_flags()
    driver.sync_robot_flags()
    assert client.gaze_calls == [False, False, False]   # retried every tick

    client.accept = True
    driver.sync_robot_flags()
    driver.sync_robot_flags()
    assert client.gaze_calls == [False, False, False, False]  # accepted -> now an edge


def test_force_posts_unconditionally_and_settles_the_pushed_state():
    """Startup: the node's run flag is fail-closed and may be days stale.

    An edge-triggered push has nothing to fire on against a node this process has never
    spoken to, so the hostapp asserts both gates once at boot -- and records them as
    pushed even if the robot is not answering yet, because the reconciler will correct
    that within 100 ms as soon as the answer changes.
    """

    client = DryRunRobotClient()
    driver = _driver(client)
    driver.set_head_reserved(lambda: False)

    driver.sync_robot_flags()
    driver.sync_robot_flags(force=True)
    driver.sync_robot_flags(force=True)

    assert client.gaze_calls == [False, False, False]
    assert client.head_reserved_calls == [False, False, False]


def test_a_client_without_the_v2_routes_is_a_no_op_not_a_crash():
    """A replay fixture or an older fake predates both methods; a sensing tick that
    raised AttributeError would take the gesture thread's tick with it."""

    class _OldClient:
        def read_state(self):
            return None

    driver = _driver(_OldClient())
    driver.set_head_reserved(lambda: False)
    driver.sync_robot_flags()          # must not raise
    driver.sync_robot_flags(force=True)


def test_a_client_that_raises_never_reaches_the_caller():
    class _Exploding(DryRunRobotClient):
        def set_gaze(self, enabled: bool) -> bool:
            raise RuntimeError("node restarting")

    driver = _driver(_Exploding())
    driver.set_head_reserved(lambda: False)
    driver.sync_robot_flags()
    assert driver._pushed_gaze is None           # not acknowledged -> not recorded


# --- the call sites ----------------------------------------------------------------


def test_starting_and_ending_a_session_turn_the_detector_on_and_off():
    """Privacy is fail-closed: an idle kiosk must not be running a face detector."""

    client = DryRunRobotClient()
    driver = _driver(client)
    driver.set_head_reserved(lambda: False)

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    assert client.gaze_calls[-1] is True

    driver.abort_unstarted_session("iframe never came up")
    assert client.gaze_calls[-1] is False


def test_the_gesture_ticker_reconciles_the_flags_every_tick(monkeypatch):
    """The 10 Hz reconciler is what carries head_reserved; without it the gate only
    ever moves when a session or an add-on request happens to fire."""

    monkeypatch.setenv("EMORI_GESTURE_MODE", "push_turn")
    from emori_adapter.hostapp.driver import GestureTicker

    client = DryRunRobotClient()
    driver = _driver(client)
    ticker = GestureTicker(driver, on_direction=lambda _d: None)
    driver.set_head_reserved(ticker.head_reserved_for_sensing)

    class _OneTick:
        def __init__(self) -> None:
            self.ticks = 0

        def wait(self, _timeout):
            self.ticks += 1
            return self.ticks > 1

        def set(self) -> None:
            pass

    ticker._stop = _OneTick()  # noqa: SLF001 - drive exactly one loop iteration
    ticker._run()  # noqa: SLF001

    assert client.head_reserved_calls != []
    assert client.gaze_calls == [False]


def test_an_addon_gaze_request_pushes_only_the_gaze_gate():
    """``set_gaze_requested`` may run while an add-on holds its own locks.

    Asking ``head_reserved()`` here would double the worst case against an unreachable
    node inside a window a shutdown has to get through, for a gate the 10 Hz reconciler
    already carries within ~100 ms. Edges carry gaze; the reconciler
    carries the reservation.
    """

    client = DryRunRobotClient()
    driver = _driver(client)
    asked: list[int] = []
    driver.set_head_reserved(lambda: bool(asked.append(1)))

    driver.set_gaze_requested(True)
    driver.set_gaze_requested(False)

    assert client.gaze_calls == [True, False]
    assert asked == []                   # the predicate was never consulted
    assert client.head_reserved_calls == []


def test_finalizing_a_session_pushes_only_the_gaze_gate():
    """``_finalize_locked`` runs with the driver lock held.

    ``head_reserved()`` calls an injected predicate that reaches into an add-on's
    own lock, so asking it on this path would put a second component's
    lock underneath the driver's -- a lock-ordering edge nothing else in this file has.
    The reservation is a predicate and the 10 Hz reconciler is what carries it; the
    face detector is a privacy gate and goes off on the edge.
    """

    client = DryRunRobotClient()
    driver = _driver(client)
    asked: list[int] = []
    driver.set_head_reserved(lambda: bool(asked.append(1)))

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    before = len(asked)
    driver.abort_unstarted_session("iframe never came up")

    assert client.gaze_calls[-1] is False
    assert len(asked) == before          # the predicate was not consulted on this path


def test_the_finalize_gate_leaves_the_driver_lock_before_it_touches_the_network():
    """A gate POST is network I/O, bounded only by the client's own timeout.

    ``driver._lock`` is on the path of every cycle, gesture and watchdog tick, and
    ``start_session`` already refuses to hold it across any I/O at all for exactly this
    reason. So ``_finalize_locked`` RECORDS the edge and the caller sends it one
    statement after its lock block ends -- which this asserts by having the POST itself
    try to take the lock.
    """

    class _LockProbe(DryRunRobotClient):
        def __init__(self) -> None:
            super().__init__()
            self.driver = None
            self.free: list[bool] = []

        def set_gaze(self, enabled: bool) -> bool:
            acquired = self.driver._lock.acquire(blocking=False)  # noqa: SLF001
            self.free.append(acquired)
            if acquired:
                self.driver._lock.release()  # noqa: SLF001
            return super().set_gaze(enabled)

    client = _LockProbe()
    driver = _driver(client)
    client.driver = driver
    driver.set_head_reserved(lambda: False)

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    driver.abort_unstarted_session("iframe never came up")

    assert client.gaze_calls == [True, False]
    assert client.free == [True, True]   # every POST ran with the driver lock free


def test_a_preempting_session_does_not_flap_the_detector_off_and_on():
    """A learner walks up while the previous session is still open.

    The preempt finalize records "detector off" and ``start_session``'s own sync
    supersedes it before anything is sent, so the arbiter sees one steady True instead
    of an off/on pair it would have had to act on twice.
    """

    client = DryRunRobotClient()
    driver = _driver(client)
    driver.set_head_reserved(lambda: False)

    driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="en"))
    # One analysis cycle: enough for start_session to finalize the previous session
    # rather than quietly reuse it.
    driver.tracker._eng.append(0.5)      # noqa: SLF001
    driver.start_session(SessionInfo(session_id="s2", person_id="p2", lang="en"))

    assert client.gaze_calls == [True]
    assert driver._gaze_push_pending is False    # noqa: SLF001 - drained, not stranded
