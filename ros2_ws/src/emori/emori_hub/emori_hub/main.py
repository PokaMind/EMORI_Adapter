"""``python -m emori_hub.main``: the hub, one process hosting the hostapp beside ROS.

The hostapp's FastAPI app runs here as it runs under ``python -m
emori_adapter.hostapp.server``, on uvicorn on the main thread, with the same lifespan. rclpy
runs beside it on private executors, each on a thread of its own. The session code above
is not edited for this: it is handed a robot client that goes over ROS, and that is the
difference.

**Starting.** The order is load-bearing:

1. The environment snapshot goes into ``os.environ``, before anything from the adapter core
   is imported, because its modules read the environment as they load.
2. The core loads and logging comes up, in the order the hostapp's own entry point has them.
3. This process takes SIGTERM and SIGINT, then ``rclpy.init`` with rclpy's own handlers off.
4. Node ``/emori/robot_link`` and its executor start. They must be spinning before step 6,
   which pushes both robot gates and starts the motion player.
5. The robot client. Discovery is not awaited, so a robot that is down does not hold the
   start up; the client says what a call does until the link is there.
6. ``_build_app``, with that client.
7. The nodes built on the app go on ``ros-facade`` and ``ros-graph``, three threads each,
   because a service callback may wait on the session lock. ``ros-facade`` has nodes unless
   ``EMORI_HUB_FACADES=0`` and ``ros-graph`` only with ``EMORI_HUB_GRAPH=1``. Neither
   executor is the link's, and nothing on them can starve a response a motion or a gesture
   thread is waiting for. A node that cannot be built costs what it publishes: the kiosk and
   the sessions still run, the journal and ``/api/robot/status`` say which, and readycheck
   fails, so a switch to the hub rolls back.
8. ``serve``, on this thread, until a stop.

**Stopping.** uvicorn stops serving and runs the lifespan's teardown as ever, which ends in
``client.close()``. Only then does ROS go, last started first: graph, facades, client, link,
context. The link goes after the lifespan because everything the lifespan is joining may be
blocked on a response that only the link's thread can deliver. Whatever is on the way out
(a failed start, a server that could not bind) leaves by the same path, so what was started
is what gets stopped. The hub parks nothing: the head holds its last target, as it does
when the hostapp stops.

**Why this process holds the signals before uvicorn does.** After a graceful stop uvicorn
puts back the handlers it found and raises the signal again, expecting that to end the
process. With the default handler that would end it there, past every step of the ROS
teardown, so what it finds here is a handler that only takes a note. That note is also what
a stop during the start leaves, and if it lands in the moment before uvicorn has taken over,
the watch thread passes it on as soon as it has.

**The interpreter's lock.** The executors' scans, the facades' reads and the graph's publishes
are Python, in the process that plans a sentence's moves. CPython asks a thread holding its
lock to give it up only after 5 ms, so :func:`main` makes that :data:`GIL_SWITCH_S`. Each
handover then costs about a millisecond, and a waiting move can lose several if other Python
threads are runnable, since the lock goes to one of them and not necessarily to the move. A C
call that holds the lock, such as a publish, is not interrupted by this. The robot measures what
is left.

**A spin that dies.** A dead executor is the quiet failure: the kiosk still answers and
nothing on the graph moves. So a thread watches every spin. When one ends with nobody asking,
the process asks itself to stop the normal way and exits with :data:`EXIT_SPIN_DIED`, and
the unit's restart builds a new one. A self-inflicted stop has nobody outside to enforce a
limit, so it gets a deadline of its own, and so does the ROS teardown, which also covers a
thread that would hold the interpreter open at exit.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import fnmatch
import gc
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence

from emori_hub.addon_node import RosPorts, addon_builder
from emori_hub.facade_kit import ERROR, Health, register_diagnostics
from emori_hub.gateway_node import build_gateway_node
from emori_hub.gesture_node import build_gesture_node
from emori_hub.graph import GRAPH_BUILDERS
from emori_hub.motion_node import build_motion_node
from emori_hub.session_node import build_session_node
from emori_hub.speech_node import build_speech_node
from emori_hub.spin import SupervisedSpin

__all__ = [
    "ENV_DENYLIST",
    "ENV_JSON_VAR",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_SPIN_DIED",
    "FACADES_VAR",
    "FACADE_NODES",
    "GIL_SWITCH_S",
    "GRAPH_BEHAVIOR_DEFAULT",
    "GRAPH_BEHAVIOR_VAR",
    "GRAPH_NODES",
    "GRAPH_PRIVACY_DEFAULTS",
    "GRAPH_TRANSPORT",
    "GRAPH_TRANSPORT_VAR",
    "GRAPH_VAR",
    "GraphSettings",
    "HubBindings",
    "HubConfigError",
    "Lifecycle",
    "NodeGroup",
    "ROS_ARGS",
    "STOP_BUDGET_S",
    "TEARDOWN_BUDGET_S",
    "TRANSPORT_VAR",
    "load_env_snapshot",
    "graph_params_file",
    "load_hub_bindings",
    "main",
    "robot_transport",
    "ros_arguments",
    "run_hub",
]

log = logging.getLogger("emori.hub")

EXIT_OK = 0
#: It could not start, or the server raised.
EXIT_FAILED = 1
#: An executor died under a running process. The robot node uses the same code when its
#: backend dies: the same "restart me" to the same kind of supervisor.
EXIT_SPIN_DIED = 70

#: Where the unit's environment was written, set by the container. Unset, there is no
#: snapshot and the process takes the environment it was given.
ENV_JSON_VAR = "EMORI_HUB_ENV_JSON"
#: Names the snapshot does not get to set. They describe the host's process, or this
#: container's own Python and ROS, and the host's value would be wrong here.
ENV_DENYLIST = (
    "PATH", "HOME", "LANG", "LC_*", "USER", "LOGNAME", "SHELL", "INVOCATION_ID",
    "JOURNAL_STREAM", "NOTIFY_SOCKET", "SYSTEMD_EXEC_PID", "MANAGERPID", "XDG_RUNTIME_DIR",
    "RUNTIME_DIRECTORY", "PYTHONPATH", "ROS_*", "RMW_*", "FASTRTPS_*", "AMENT_*", "COLCON_*",
    "LD_LIBRARY_PATH",
)
#: A key of the snapshot that holds values for the graph's nodes and not this process.
_SNAPSHOT_GRAPH_BLOCK = "graph"

TRANSPORT_VAR = "EMORI_ROBOT_TRANSPORT"

#: ``0`` hosts no facade node here: the hub is the hostapp over ROS and nothing else on the
#: graph. The way back from them, with a restart.
FACADES_VAR = "EMORI_HUB_FACADES"
#: How long a Python thread keeps the interpreter's lock while another waits. See the module
#: docstring. CPython's own value is 5 ms.
GIL_SWITCH_S = 0.001

#: ``1`` hosts the graph's nodes here, and the motor runs its node alone. Only ``1``, the
#: value ``deploy/robot/hub-stack.sh`` writes and compares with the motor's launch file.
GRAPH_VAR = "EMORI_HUB_GRAPH"
#: What leaves on DDS, each with its value when ``deploy/ros2/.env`` does not say. The gate
#: reads them from the environment, and it covers the session's events and the gateway's status
#: as much as the graph's nodes, so these apply whether or not the graph is hosted here.
GRAPH_PRIVACY_DEFAULTS = {
    "EMORI_ROS_DEIDENTIFY": "1",
    "EMORI_ROS_SPEECH_TEXT": "0",
    "EMORI_ROS_ID_SALT": "",
}
#: The thresholds the engagement node runs on, from ``deploy/ros2/.env``.
GRAPH_BEHAVIOR_VAR = "EMORI_ROS_BEHAVIOR_CONFIG_HOST"
GRAPH_BEHAVIOR_DEFAULT = "/opt/emori/configs/behavior.yaml"
#: The facade reads this from the environment, so it is set for the whole process. The
#: session code then ignores a dead-man's fallback, because the native transport never calls
#: the intake that fallback listens to, and would otherwise open a second writer every session.
GRAPH_TRANSPORT_VAR = "EMORI_SKILL_TRANSPORT"
GRAPH_TRANSPORT = "ros_native"

#: ``--disable-external-lib-logs`` because rcl's own file logger needs a directory this
#: process may not be able to write, and the journal is the debugging surface anyway.
ROS_ARGS = ("--ros-args", "--disable-external-lib-logs")

#: What builds each node on ``ros-facade`` and ``ros-graph``: a callable taking the app. A
#: group with nothing in it has no executor and no thread. The add-on's node is added to these
#: by :func:`main`, because it has to share the ports the app is built with.
FACADE_NODES: tuple[Callable[[Any], Any], ...] = (
    build_session_node,
    build_speech_node,
    build_gesture_node,
    build_motion_node,
    build_gateway_node,
)
GRAPH_NODES: tuple[Callable[[Any], Any], ...] = GRAPH_BUILDERS
EXECUTOR_THREADS = 3

#: A stop the process started itself: the lifespan's own limit, the ROS teardown, and room
#: for uvicorn to close its connections. The same limit the container gets from outside.
STOP_BUDGET_S = 12.0
#: From the server's return to the end of the process, inside what ``STOP_BUDGET_S`` leaves
#: after the lifespan's own limit.
TEARDOWN_BUDGET_S = 3.0
#: One step of the ROS teardown. A step that hangs costs this and the rest still run.
_TEARDOWN_STEP_S = 1.0
#: How soon a dead spin is noticed, and how soon a stop reaches a uvicorn that just started.
_WATCH_POLL_S = 0.25


class HubConfigError(Exception):
    """A configuration this process must not start on. Never carries a setting's value."""


# --- the environment -------------------------------------------------------------------


def _denied(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in ENV_DENYLIST)


@dataclass(frozen=True)
class GraphSettings:
    """The graph is hosted here. ``ignored`` names host settings that lost to the graph's."""

    behavior_yaml: str = GRAPH_BEHAVIOR_DEFAULT
    ignored: tuple[str, ...] = ()


def load_env_snapshot(
    environ: MutableMapping[str, str], ignored: Optional[list[str]] = None,
) -> Optional[GraphSettings]:
    """Put the unit's environment into ``environ``, minus :data:`ENV_DENYLIST`.

    systemd parses the unit's env files and dotenv then fills in only what is missing. A
    container cannot reproduce that with a parser of its own, so the unit writes down what
    systemd parsed and it arrives here as it was. A snapshot that was named and cannot be
    read stops the start: without it this would be a robot with no token, on the wrong
    language, that looks as if it is running.

    Returns the graph's settings with ``EMORI_HUB_GRAPH=1``, and ``None`` otherwise. ``ignored``
    is given the names of host settings that lost, for a warning once logging is up.
    """

    path = environ.get(ENV_JSON_VAR, "")
    block: Optional[dict] = None
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError) as error:
            raise HubConfigError(f"cannot read the environment snapshot {path}: {error}") from error
        if not isinstance(snapshot, dict):
            raise HubConfigError(f"the environment snapshot {path} is not a JSON object")
        for name, value in snapshot.items():
            if name == _SNAPSHOT_GRAPH_BLOCK or _denied(name):
                continue
            if not isinstance(value, str):
                raise HubConfigError(f"the environment snapshot {path}: {name} is not a string")
            environ[name] = value
        given = snapshot.get(_SNAPSHOT_GRAPH_BLOCK)
        block = given if isinstance(given, dict) else {}
    hosted = environ.get(GRAPH_VAR, "").strip() == "1"
    # No snapshot and no graph is a process run by hand, which takes the environment it has.
    lost = _apply_privacy_values(environ, block) if block is not None or hosted else ()
    settings = _apply_graph_values(environ, block, lost) if hosted else None
    if ignored is not None:
        ignored.extend(settings.ignored if settings is not None else lost)
    return settings


def _apply_privacy_values(
    environ: MutableMapping[str, str], block: Optional[dict],
) -> tuple[str, ...]:
    """Set the privacy names, and return those whose value in the host's files lost.

    They come from ``deploy/ros2/.env``, through the snapshot's block, and from nowhere else. A
    different value in the host's files never had any effect on what goes on DDS and must not
    start putting speech text there now that this process publishes. A block that says nothing
    means the defaults, which are the closed ones. With no snapshot at all, as in the simulator,
    this process's environment stands in for the file.
    """

    source: Mapping[str, Any] = environ if block is None else block
    wanted: dict[str, str] = {}
    for name, default in GRAPH_PRIVACY_DEFAULTS.items():
        given = source.get(name)
        wanted[name] = given if isinstance(given, str) else default
    lost = tuple(sorted(name for name, value in wanted.items()
                        if name in environ and environ[name] != value))
    environ.update(wanted)
    return lost


def _apply_graph_values(
    environ: MutableMapping[str, str], block: Optional[dict], lost: tuple[str, ...],
) -> GraphSettings:
    """Set what only the graph's nodes read from the environment, and return the rest."""

    source: Mapping[str, Any] = environ if block is None else block
    behavior = source.get(GRAPH_BEHAVIOR_VAR)
    if environ.get(GRAPH_TRANSPORT_VAR, GRAPH_TRANSPORT) != GRAPH_TRANSPORT:
        lost = (*lost, GRAPH_TRANSPORT_VAR)
    environ[GRAPH_TRANSPORT_VAR] = GRAPH_TRANSPORT
    return GraphSettings(
        behavior_yaml=behavior if isinstance(behavior, str) and behavior
        else GRAPH_BEHAVIOR_DEFAULT,
        ignored=tuple(sorted(lost)),
    )


def robot_transport(environ: Mapping[str, str]) -> str:
    """``ros`` unless told ``http``. Anything else is a typo, and a typo must not pick."""

    transport = environ.get(TRANSPORT_VAR, "").strip().lower() or "ros"
    if transport not in ("ros", "http"):
        raise HubConfigError(f"{TRANSPORT_VAR} must be ros or http")
    return transport


# --- ROS -------------------------------------------------------------------------------


def graph_params_file() -> str:
    """``config/graph.yaml`` as installed. Imports ROS's package index, so only called here."""

    from ament_index_python.packages import get_package_share_directory

    return os.path.join(get_package_share_directory("emori_hub"), "config", "graph.yaml")


def ros_arguments(settings: Optional[GraphSettings],
                  params_file: Callable[[], str]) -> Sequence[str]:
    """What ``rclpy.init`` is given. Every node in this process reads these.

    The behaviour file goes as the engagement node's parameter and not as
    ``EMORI_BEHAVIOR_CONFIG``, because the session code reads that name for its own tuning
    and keeps the value it has. No other node declares ``behavior_yaml``, and a node takes
    no notice of an override it did not declare.
    """

    if settings is None:
        return ROS_ARGS
    return (*ROS_ARGS, "--params-file", params_file(),
            "-p", f"behavior_yaml:={settings.behavior_yaml}")


@dataclass(frozen=True)
class HubBindings:
    """Everything this process takes from ROS, so a test can hand it fakes."""

    init: Callable[[Sequence[str]], None]
    try_shutdown: Callable[[], Any]
    ok: Callable[[], bool]
    fatal: tuple[type[BaseException], ...]
    multi_threaded_executor: Callable[..., Any]
    # robot_link.LinkBindings: what node /emori/robot_link is made of.
    link: Any
    graph_params: Callable[[], str] = graph_params_file


def load_hub_bindings() -> HubBindings:
    """Import ``rclpy``. Raises ``ImportError`` where ROS is absent."""

    import rclpy
    from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
    from rclpy.signals import SignalHandlerOptions

    from emori_hub.robot_link import load_link_bindings

    def init(args: Sequence[str]) -> None:
        # rclpy's handlers would answer SIGINT by shutting the context down under a server
        # that is still running. uvicorn has the signals; ROS goes when it has finished.
        rclpy.init(args=list(args), signal_handler_options=SignalHandlerOptions.NO)

    return HubBindings(
        init=init,
        try_shutdown=rclpy.try_shutdown,
        ok=rclpy.ok,
        fatal=(ExternalShutdownException,),
        multi_threaded_executor=MultiThreadedExecutor,
        link=load_link_bindings(),
    )


class NodeGroup:
    """Nodes on one multi-threaded executor, spun on one supervised thread.

    A callback that raises is logged and the executor goes on, so there is no fault limit
    here: these callbacks are other people's code, and one that fails on every tick is a
    bug in that callback, not a reason to take a session down.
    """

    def __init__(self, name: str, ros: HubBindings,
                 builders: Iterable[Callable[[Any], Any]]) -> None:
        self.name = name
        self._ros = ros
        self._builders = tuple(builders)
        self._nodes: list[Any] = []
        self._executor: Any = None
        self.spin: SupervisedSpin | None = None
        #: The builders that raised, by name.
        self.failed: list[str] = []

    def hosted(self) -> list[str]:
        names = []
        for node in self._nodes:
            read = getattr(node, "get_name", None)
            names.append(str(read()) if callable(read) else repr(node))
        return names

    def start(self, app: Any) -> None:
        for build in self._builders:
            try:
                self._nodes.append(build(app))
            except Exception:  # noqa: BLE001 - a topic is not worth the kiosk and the sessions
                name = getattr(build, "__name__", repr(build))
                self.failed.append(name)
                log.exception("%s: %s failed, so that node will not run", self.name, name)
        if self.failed:
            # A constructor that raised has left a node on the graph that nothing holds and
            # nothing will spin. It would read as present to whoever lists the nodes.
            gc.collect()
        if not self._nodes:
            return
        self._executor = self._ros.multi_threaded_executor(num_threads=EXECUTOR_THREADS)
        for node in self._nodes:
            self._executor.add_node(node)
        self.spin = SupervisedSpin(self.name, self._executor, ok=self._ros.ok,
                                   fatal=self._ros.fatal)
        self.spin.start()

    def stop(self, timeout: float = _TEARDOWN_STEP_S) -> None:
        """Stop spinning, wait out the running callbacks, then release the nodes."""

        if self.spin is not None:
            self.spin.stop(timeout)
        releases = [self._executor.shutdown] if self._executor is not None else []
        releases.extend(node.destroy_node for node in reversed(self._nodes))
        self._executor, self._nodes = None, []
        for release in releases:
            try:
                release()
            except Exception:  # noqa: BLE001 - the rest must still go
                log.warning("%s: a release failed", self.name, exc_info=True)


# --- stopping --------------------------------------------------------------------------


class Lifecycle:
    """How this process stops: the signals, the watch on the spins, and its exit code."""

    def __init__(
        self,
        *,
        exit_now: Callable[[int], Any] = os._exit,
        raise_signal: Callable[[int], Any] = signal.raise_signal,
        poll_s: float = _WATCH_POLL_S,
    ) -> None:
        self.exit_code = EXIT_OK
        self._exit_now = exit_now
        self._raise_signal = raise_signal
        self._poll_s = poll_s
        # One object, so that what signal.getsignal hands back can be compared with it.
        self._handler = self._note_signal
        self._previous: dict[int, Any] = {}
        self._stop_wanted = threading.Event()
        self._spins: list[SupervisedSpin] = []
        self._a_spin_died = False
        self._carried = False
        self._watch_over = threading.Event()
        self._watcher: threading.Thread | None = None
        self._deadlines_off = threading.Event()

    # --- the signals ------------------------------------------------------------------

    def take_signals(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous[signum] = signal.signal(signum, self._handler)

    def give_back_signals(self) -> None:
        previous, self._previous = self._previous, {}
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def _note_signal(self, _signum: int, _frame: Any) -> None:
        self._stop_wanted.set()

    @property
    def stop_wanted(self) -> bool:
        return self._stop_wanted.is_set()

    def _carry_stop(self) -> None:
        """Hand a stop to the server, once only, if it holds the signals and has not heard."""

        if self._carried or not self._stop_wanted.is_set():
            return
        holder = signal.getsignal(signal.SIGTERM)
        if holder == self._handler or not callable(holder):
            return  # not yet, or no longer: the checks in run_hub cover those
        self._carried = True
        self._raise_signal(signal.SIGTERM)

    # --- the watch --------------------------------------------------------------------

    def watch(self, spin: SupervisedSpin) -> None:
        self._spins.append(spin)

    def start_watching(self) -> None:
        self._watcher = threading.Thread(target=self._watch, name="hub-watch", daemon=True)
        self._watcher.start()

    def stop_watching(self) -> None:
        """Before any spin is stopped on purpose, and before the signals are given back."""

        self._watch_over.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(self._poll_s * 4)

    def _watch(self) -> None:
        while not self._watch_over.wait(self._poll_s):
            try:
                self._notice_a_dead_spin()
                self._carry_stop()
            except Exception:  # noqa: BLE001 - this thread is what notices the others
                log.warning("the hub's watch raised; carrying on", exc_info=True)

    def _notice_a_dead_spin(self) -> None:
        if self._a_spin_died:
            return
        for spin in tuple(self._spins):
            why = spin.death()
            if why:
                self._a_spin_died = True
                self.exit_code = EXIT_SPIN_DIED
                log.critical("%s has died: %s. Nothing on it will run again, so the hub is "
                             "stopping with exit code %d and its unit will restart it.",
                             spin.name, why, EXIT_SPIN_DIED)
                self.exit_by(STOP_BUDGET_S, "the stop after a dead executor")
                self._stop_wanted.set()
                return

    # --- the exit code and the deadline -----------------------------------------------

    def note_failure(self, code: int) -> None:
        """A dead spin stays the reason given, if there was one."""

        if self.exit_code == EXIT_OK:
            self.exit_code = code

    def exit_by(self, seconds: float, what: str) -> None:
        """End the process in ``seconds`` if it is still here.

        A daemon thread, so it is still counting while the interpreter waits at exit for a
        thread that will never finish.
        """

        threading.Thread(target=self._exit_at_deadline, args=(seconds, what),
                         name="hub-exit-deadline", daemon=True).start()

    def cancel_deadlines(self) -> None:
        """For a test, which outlives the process it is pretending to be."""

        self._deadlines_off.set()

    def _exit_at_deadline(self, seconds: float, what: str) -> None:
        if self._deadlines_off.wait(seconds):
            return
        stuck = sorted(thread.name for thread in threading.enumerate()
                       if thread.is_alive() and not thread.daemon
                       and thread is not threading.main_thread())
        log.error("%s took longer than %g s; exiting with %d anyway. Threads still holding the "
                  "process: %s", what, seconds, self.exit_code, ", ".join(stuck) or "none")
        self._exit_now(self.exit_code)


class _Started:
    """What has been started, to be stopped last first whatever happened on the way."""

    def __init__(self, step_s: float = _TEARDOWN_STEP_S) -> None:
        self._step_s = step_s
        self._steps: list[tuple[str, Callable[[], Any]]] = []

    def add(self, name: str, stop: Callable[[], Any]) -> None:
        self._steps.append((name, stop))

    def stop_all(self) -> None:
        while self._steps:
            name, stop = self._steps.pop()
            self._one_step(name, stop)

    def _one_step(self, name: str, stop: Callable[[], Any]) -> None:
        """On a thread of its own, because rclpy can block here and the next step must run."""

        done = threading.Event()

        def run() -> None:
            try:
                stop()
            except BaseException:  # noqa: BLE001 - the next step must still run
                log.warning("stopping %s failed", name, exc_info=True)
            finally:
                done.set()

        threading.Thread(target=run, name=f"hub-stop-{name}", daemon=True).start()
        if not done.wait(self._step_s):
            log.warning("%s did not stop within %g s; going on without it", name, self._step_s)


# --- the process -----------------------------------------------------------------------


def _bring_up_and_serve(
    ros: HubBindings,
    host: Any,
    life: Lifecycle,
    started: _Started,
    environ: Mapping[str, str],
    wrap_client: Optional[Callable[[Any], Any]],
    facades: Iterable[Callable[[Any], Any]],
    graph: Iterable[Callable[[Any], Any]],
    graph_settings: Optional[GraphSettings],
    ports: Optional[RosPorts],
) -> None:
    transport = robot_transport(environ)
    deployed = _deployed_marker()
    ros.init(ros_arguments(graph_settings, ros.graph_params))
    started.add("rclpy", ros.try_shutdown)

    if transport == "ros":
        from emori_hub.robot_link import LinkNode, RosRobotClient

        link_node = LinkNode(ros.link)
        started.add("ros-link", link_node.stop)
        link_node.start()
        life.watch(link_node.spin)
        client: Any = RosRobotClient(link_node.link)
    else:
        from emori_adapter.robot.client import HttpRobotClient

        client = HttpRobotClient(host.REACHY_BASE_URL)
    started.add("robot-client", client.close)
    if wrap_client is not None:
        client = wrap_client(client)
        started.add("robot-client-wrapper", client.close)
    log.info("the robot is reached over %s", transport)

    if life.stop_wanted:
        return
    overrides: dict[str, Any] = {"robot_client": client}
    if ports is not None:
        overrides["ros"] = ports
    app = host._build_app(overrides)
    if getattr(app, "state", None) is not None:
        app.state.hub_deployed = deployed

    for name, builders in (("ros-facade", facades), ("ros-graph", graph)):
        if life.stop_wanted:
            return
        group = NodeGroup(name, ros, builders)
        started.add(name, group.stop)
        group.start(app)
        if group.spin is not None:
            life.watch(group.spin)
        _report_nodes(app, group)

    if life.stop_wanted:
        return
    # The motor image has uvloop and httptools and the hostapp's venv does not, so these
    # are named: left on "auto" the two would run a different loop and HTTP parser.
    host.serve(app, loop="asyncio", http="h11")


def _deployed_marker() -> str:
    """The deploy this process started on, for ``/api/robot/status``.

    Read once, here: readycheck compares it with the file as it is now, and a difference is a hub
    that a deploy left running, as it does a hub in a session. The working directory is the
    install root in the container.
    """

    from emori_adapter.admin.system import deployed_marker

    return deployed_marker()


def _report_nodes(app: Any, group: NodeGroup) -> None:
    """Say what this group hosts and what it could not build, for ``/api/robot/status``.

    ``/diagnostics`` says so too while the gateway is hosted, but the gateway is one of these
    nodes, so that cannot be the only place.
    """

    state = getattr(app, "state", None)
    if state is None:
        return
    report = getattr(state, "hub_nodes", None) or {"hosted": [], "failed": []}
    report["hosted"].extend(group.hosted())
    report["failed"].extend(group.failed)
    state.hub_nodes = report
    if group.failed:
        names = ", ".join(group.failed)
        register_diagnostics(app, f"{group.name}_nodes", lambda: Health(
            ERROR, f"{len(group.failed)} nodes could not be built: {names}", {"failed": names}))


def run_hub(
    ros: HubBindings,
    host: Any,
    *,
    wrap_client: Optional[Callable[[Any], Any]] = None,
    facades: Iterable[Callable[[Any], Any]] = (),
    graph: Optional[Iterable[Callable[[Any], Any]]] = None,
    graph_settings: Optional[GraphSettings] = None,
    ports: Optional[RosPorts] = None,
    environ: Optional[Mapping[str, str]] = None,
    lifecycle: Optional[Lifecycle] = None,
) -> int:
    """Run the hub on this thread until it stops, and return its exit code.

    ``host`` is the hostapp's server module and ``wrap_client`` puts a wrapper around the
    robot client whichever transport it is: the one way into this process for a harness
    that records every robot call. The normal entry passes none. ``graph_settings`` is what
    :func:`load_env_snapshot` returned: without any, the graph stays in the motor container
    and ``ros-graph`` has only the nodes ``graph`` names. ``ports`` is what an add-on is
    handed as ``ExtensionContext.ros``.
    """

    if graph is None:
        graph = GRAPH_NODES if graph_settings is not None else ()
    life = lifecycle if lifecycle is not None else Lifecycle()
    started = _Started()
    life.take_signals()
    life.start_watching()
    try:
        _bring_up_and_serve(ros, host, life, started, os.environ if environ is None else environ,
                            wrap_client, facades, graph, graph_settings, ports)
    except SystemExit as stop:
        # uvicorn's way of saying it could not start: it could not bind, or the lifespan's
        # startup failed. The exit code is its own.
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        if code:
            life.note_failure(code)
    except HubConfigError as error:
        log.error("%s", error)
        life.note_failure(EXIT_FAILED)
    except Exception:  # noqa: BLE001 - logged, and what started still has to stop
        log.exception("the hub could not run")
        life.note_failure(EXIT_FAILED)
    finally:
        life.stop_watching()
        life.exit_by(TEARDOWN_BUDGET_S, "the ROS teardown")
        started.stop_all()
        life.give_back_signals()
    log.info("the hub has stopped, exit code %d", life.exit_code)
    return life.exit_code


def main(*, wrap_client: Optional[Callable[[Any], Any]] = None) -> int:
    sys.setswitchinterval(GIL_SWITCH_S)
    ignored: list[str] = []
    try:
        graph_settings = load_env_snapshot(os.environ, ignored)
    except HubConfigError as error:
        print(f"emori-hub: {error}", file=sys.stderr, flush=True)
        return EXIT_FAILED

    # Only now: the core's config applies dotenv and the site's settings as it loads, and
    # the server module reads its constants from the result. Logging comes up after them,
    # as it does in the hostapp's entry point, so a log level set there means the same here.
    from emori_adapter.hostapp import server
    from emori_adapter.logging_setup import setup_journal_logging

    setup_journal_logging()
    for name in ignored:
        log.warning("%s in the host's env files differs from the graph's value and is "
                    "ignored: the graph takes it from deploy/ros2/.env", name)
    try:
        ros = load_hub_bindings()
    except ImportError as error:
        log.error("the hub needs ROS 2 and the robot's interfaces: %s. Without ROS, run "
                  "python -m emori_adapter.hostapp.server", error)
        return EXIT_FAILED
    facades: tuple[Callable[[Any], Any], ...] = ()
    ports: Optional[RosPorts] = None
    if os.environ.get(FACADES_VAR, "").strip() != "0":
        ports = RosPorts()
        facades = (*FACADE_NODES, addon_builder(ports))
    return run_hub(ros, server, wrap_client=wrap_client, facades=facades,
                   graph_settings=graph_settings, ports=ports)


if __name__ == "__main__":
    sys.exit(main())
