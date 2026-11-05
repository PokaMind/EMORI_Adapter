"""What the hub's facade nodes share: the ROS pieces they are made of, guarded callbacks, a
poller thread and the sources of ``/diagnostics``.

A facade is a ROS node that shows part of the hostapp's state and offers part of its commands.
It reads the objects the app already has and calls the public methods the HTTP routes call; it
adds no behaviour of its own to them. It is not part of the session: nothing the core does
waits for it, reads from it or is decided by it. Four rules hold for every one:

* **A facade never adds work to the session's motion path.** That path is the request thread of
  ``POST /api/cycle``, the driver, the cue scheduler, the player and the robot client. A facade
  reads attributes those objects already keep, or runs on a thread of its own. It never holds a
  lock a session thread takes for longer than it takes to copy a value, and it never calls the
  robot client. A reader that could wait on such a lock (the voice tap's, the microphone
  profile's) runs in a :class:`Poller`, which is a thread of its own, so a wait costs that
  thread and no executor thread.
* **A tick reads what is already held.** A timer reads attributes the core has already written.
  A value that cannot be read without a lock is not published, or is published from a counter
  the facade keeps.
* **A callback never raises and never ends the executor.** :class:`Guarded` wraps each one: a
  failure is counted, logged at a bounded rate, and answered with the failure shape the
  interface allows.
* **A loss is counted and shown.** A publisher that raises, a reader that raises and a reader
  that was too slow each add to a named counter in :class:`Counters`. A facade that falls behind
  or refuses a request says so in its counters, and the counters go to ``/diagnostics`` through
  :func:`diagnostic_sources`.

A facade node has no parameter services: nothing sets a parameter on one, and each would add six
services that every executor wake walks through. Nothing here imports ROS when this module is
loaded. :class:`KitBindings` and :class:`NodeBindings` hold what the wiring takes from ``rclpy``,
and a test hands them fakes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "COMPLAINT_PERIOD_S",
    "Counters",
    "ERROR",
    "Guarded",
    "Health",
    "KitBindings",
    "NODE_NAMESPACE",
    "NodeBindings",
    "OK",
    "Poller",
    "STALE",
    "TickClock",
    "WARN",
    "diagnostic_sources",
    "env_flag",
    "facade_node",
    "find_endpoint",
    "load_kit_bindings",
    "load_node_bindings",
    "register_diagnostics",
    "release_with",
    "stamp",
    "stamp_header",
]

log = logging.getLogger("emori.hub.facade")

NODE_NAMESPACE = "/emori"

#: ``diagnostic_msgs/DiagnosticStatus`` levels.
OK, WARN, ERROR, STALE = 0, 1, 2, 3

#: A repeated complaint is logged at most this often, with a count of the ones left out.
COMPLAINT_PERIOD_S = 30.0

#: Where a facade finds the sources of ``/diagnostics`` on the app.
_SOURCES_ATTR = "diagnostic_sources"

_TRUE = frozenset({"1", "true", "yes", "on"})


def env_flag(environ: Mapping[str, str], name: str) -> bool:
    """Only a spelled-out yes turns it on, so a typo cannot."""

    return str(environ.get(name, "")).strip().lower() in _TRUE


def facade_node(name: str, **kwargs: Any) -> Any:
    """A node with no parameter services; see the module docstring."""

    from rclpy.node import Node

    kwargs.setdefault("start_parameter_services", False)
    return Node(name, **kwargs)


@dataclass(frozen=True)
class KitBindings:
    """What a facade takes from ROS besides its own interface types, so a test can hand fakes."""

    create_node: Callable[..., Any]
    #: Callbacks in one never run at the same time: timers and publishers.
    group_exclusive: Callable[[], Any]
    #: Callbacks in one may run at the same time: a service that may block.
    group_reentrant: Callable[[], Any]
    #: Latched: one sample kept for a subscriber that arrives late.
    qos_latched: Any
    #: Reliable, with room for a burst: each message is one event.
    qos_events: Any
    #: Best effort, one sample: only the latest matters.
    qos_state: Any
    String: Any
    Trigger: Any
    SetBool: Any


def load_kit_bindings() -> KitBindings:
    """Import ``rclpy`` and the standard types. Raises ``ImportError`` where ROS is absent."""

    from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import String
    from std_srvs.srv import SetBool, Trigger

    return KitBindings(
        create_node=facade_node,
        group_exclusive=MutuallyExclusiveCallbackGroup,
        group_reentrant=ReentrantCallbackGroup,
        qos_latched=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                               reliability=ReliabilityPolicy.RELIABLE,
                               durability=DurabilityPolicy.TRANSIENT_LOCAL),
        qos_events=QoSProfile(depth=64, history=HistoryPolicy.KEEP_LAST,
                              reliability=ReliabilityPolicy.RELIABLE,
                              durability=DurabilityPolicy.VOLATILE),
        qos_state=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE),
        String=String,
        Trigger=Trigger,
        SetBool=SetBool,
    )


def stamp(node: Any, message: Any) -> Any:
    """Put the node's clock into ``message.header`` and return the message."""

    message.header.stamp = node.get_clock().now().to_msg()
    return message


def release_with(node: Any, close: Callable[[], Any], facade: Any = None) -> Any:
    """Make ``node.destroy_node`` run ``close`` first, and return the node.

    The hub's node groups know a node and nothing else, and they destroy it on the way out. This
    is how what the facade started (its threads, its tee) is stopped by that same call.
    """

    original = node.destroy_node

    def destroy_node(*args: Any, **kwargs: Any) -> Any:
        try:
            close()
        except Exception:  # noqa: BLE001 - the node must still be destroyed
            log.warning("a facade did not close cleanly", exc_info=True)
        return original(*args, **kwargs)

    node.destroy_node = destroy_node
    if facade is not None:
        node.emori_facade = facade
    return node


class Counters:
    """Named counts, safe to add to from any thread. Never raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._complained: dict[str, float] = {}

    def add(self, name: str, amount: int = 1) -> int:
        with self._lock:
            total = self._counts.get(name, 0) + amount
            self._counts[name] = total
            return total

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts.get(name, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def complain(self, name: str, what: str, *, exc_info: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """Count a fault and say so at most once per period for each name."""

        total = self.add(name)
        now = clock()
        with self._lock:
            last = self._complained.get(name)
            if last is not None and now - last < COMPLAINT_PERIOD_S:
                return
            self._complained[name] = now
        log.warning("%s (%d so far)", what, total, exc_info=exc_info)


class Poller:
    """``read`` called every ``period_s`` on a thread of its own, until stopped.

    For a reader that takes a lock another thread also takes. If that wait is long, this thread
    is the only one that pays for it: a callback on an executor would take one of its few
    threads, and a hub thread would take the lock's other users' time. A reader that raises is
    counted and the next period still runs. ``read`` returns a pause in seconds to wait
    before the next period, or ``None``.
    """

    def __init__(self, name: str, period_s: float, read: Callable[[], Optional[float]],
                 counters: Counters, *, wanted: Optional[Callable[[], bool]] = None,
                 idle_s: float = 0.5) -> None:
        self.name = name
        self._period_s = period_s
        self._read = read
        self._counters = counters
        # When given and false, nothing is read, and this is asked again every ``idle_s``.
        self._wanted = wanted
        self._idle_s = idle_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> bool:
        """True when the thread has ended. A reader still waiting on a lock does not hold it."""

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            pause = self._period_s
            try:
                if self._wanted is not None and not self._wanted():
                    pause = self._idle_s
                else:
                    extra = self._read()
                    if extra:
                        pause = max(pause, float(extra))
            except Exception:  # noqa: BLE001 - one bad read must not end the polling
                self._counters.complain(f"{self.name}_errors", f"{self.name}: a read failed")
            self._stop.wait(pause)


@dataclass(frozen=True)
class NodeBindings:
    """Everything a facade takes from ROS, so a test can hand it fakes.

    ``types`` maps a name to the message or service class the facade uses.
    """

    create_node: Callable[..., Any]
    exclusive_group: Callable[[], Any]
    #: Last value kept for a late subscriber (transient local, reliable, depth 1).
    latched_qos: Any
    #: Reliable, depth 10: what ``/diagnostics`` listeners expect.
    default_qos: Any
    types: Mapping[str, Any]


def load_node_bindings(load_types: Callable[[], Mapping[str, Any]]) -> NodeBindings:
    """Import ``rclpy`` and the types ``load_types`` names. ``ImportError`` where ROS is absent."""

    from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

    return NodeBindings(
        create_node=facade_node,
        exclusive_group=MutuallyExclusiveCallbackGroup,
        latched_qos=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                               reliability=ReliabilityPolicy.RELIABLE,
                               durability=DurabilityPolicy.TRANSIENT_LOCAL),
        default_qos=QoSProfile(depth=10, history=HistoryPolicy.KEEP_LAST,
                               reliability=ReliabilityPolicy.RELIABLE),
        types=load_types(),
    )


def stamp_header(node: Any, header: Any) -> None:
    """Put the node's clock on a message header."""

    header.stamp = node.get_clock().now().to_msg()


# --- guarded callbacks -----------------------------------------------------------------


class Guarded:
    """Runs callbacks so that one failing is a count and a log line, never a dead executor.

    Counters are written by whichever executor thread runs the callback and read by anyone,
    without a lock: a torn read costs one off.
    """

    def __init__(self, log: logging.Logger, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._log = log
        self._monotonic = monotonic
        self.errors = 0
        self._last_complaint = float("-inf")
        self._quiet = 0

    def run(self, what: str, fn: Callable[[], Any], *, default: Any = None) -> Any:
        """``fn()``, or ``default`` if it raised."""

        try:
            return fn()
        except Exception:  # noqa: BLE001 - see the class docstring
            self.note_failure(what)
            return default

    def note_failure(self, what: str) -> None:
        """Count and log the exception being handled, for a caller that handles its own."""

        self.errors += 1
        self._complain(what)

    def _complain(self, what: str) -> None:
        now = self._monotonic()
        if now - self._last_complaint < COMPLAINT_PERIOD_S:
            self._quiet += 1
            return
        left_out, self._quiet = self._quiet, 0
        self._last_complaint = now
        self._log.warning("%s failed%s", what,
                          f" ({left_out} like it since the last report)" if left_out else "",
                          exc_info=True)


class TickClock:
    """Notices a timer that is called late, which is a facade that is being starved.

    A tick that arrives more than ``slack`` periods after the one before is counted, and the
    longest gap is kept. Nothing waits on it: it is two reads of the clock.
    """

    def __init__(self, period_s: float, *, slack: float = 3.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._limit = period_s * slack
        self._monotonic = monotonic
        self._last: Optional[float] = None
        self.ticks = 0
        self.late = 0
        self.longest_gap_s = 0.0

    def tick(self) -> None:
        now = self._monotonic()
        last, self._last = self._last, now
        self.ticks += 1
        if last is None:
            return
        gap = now - last
        if gap > self.longest_gap_s:
            self.longest_gap_s = gap
        if gap > self._limit:
            self.late += 1


# --- /diagnostics ----------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """One entry of ``/diagnostics``: a level, a line a person can read, and key/value pairs."""

    level: int = OK
    message: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)


def diagnostic_sources(app: Any) -> dict[str, Callable[[], Health]]:
    """The callables ``/diagnostics`` is built from, by entry name; created on first use.

    Every source is called on the gateway's thread and must answer from counters it already
    holds. Builders run one after another on the thread that starts the hub, so the first
    use needs no lock.
    """

    state = app.state
    sources = getattr(state, _SOURCES_ATTR, None)
    if sources is None:
        sources = {}
        setattr(state, _SOURCES_ATTR, sources)
    return sources


def register_diagnostics(app: Any, name: str, source: Callable[[], Health]) -> None:
    diagnostic_sources(app)[name] = source


# --- the app's own routes --------------------------------------------------------------


def find_endpoint(app: Any, path: str, method: str = "GET") -> Optional[Callable[..., Any]]:
    """The function the app serves ``path`` with, so a service can call what the route calls.

    A route that takes no parameters is a plain function, and calling it from here is the
    call the HTTP handler makes. ``None`` where the app has no such route.
    """

    for route in getattr(app, "routes", ()):
        if getattr(route, "path", None) != path:
            continue
        if method in (getattr(route, "methods", None) or ()):
            endpoint = getattr(route, "endpoint", None)
            if callable(endpoint):
                return endpoint
    return None
