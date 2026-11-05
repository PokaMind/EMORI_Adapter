"""``/emori/addon``: whether an add-on is installed and what it says it is doing.

The add-on is a plugin of this process, not a ROS node of its own. This node asks it three
questions twice a second and publishes the answers on ``/emori/addon/status``, latched, when
they change::

    status()               what the add-on says about itself, already free of content
    busy()                 a mode is running
    selection_reserved()   a badge scan owns the head

The seam documents each of them as callable from any thread and as one that must not block.
They are called here on the facade executor, on the tick of a callback group of their own, so
a hook that took its time would hold one of that executor's threads and nothing else: no
thread of the robot link, and no lock of the session core. Each call is timed. One that took
longer than :data:`SLOW_CALL_S` is counted, and the longest is kept, so an add-on that
breaks its side of the rule shows on ``/diagnostics`` under ``addon`` and not as a graph that
is quietly late. A hook that raises is counted and read as "no" for that question.

``RosPorts`` is what the add-on is handed as ``ExtensionContext.ros``. ``publish_status()``
asks for a publication on the next poll and returns at once; it never calls into the
add-on and never touches ROS. The same object is given to :func:`addon_builder`, so that the
node that answers it is the node that was named.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from emori_hub.facade_kit import (
    NODE_NAMESPACE,
    OK,
    WARN,
    Guarded,
    Health,
    NodeBindings,
    TickClock,
    load_node_bindings,
    register_diagnostics,
    stamp_header,
)

__all__ = [
    "AddonFacade",
    "AddonSnapshot",
    "NODE_NAME",
    "POLL_PERIOD_S",
    "RosPorts",
    "SLOW_CALL_S",
    "STATUS_JSON_MAX",
    "STATUS_TOPIC",
    "addon_builder",
    "build_addon_node",
    "load_addon_bindings",
]

log = logging.getLogger("emori.hub.addon")

NODE_NAME = "addon"
STATUS_TOPIC = "/emori/addon/status"

#: 2 Hz.
POLL_PERIOD_S = 0.5
#: A hook that takes longer than this has not kept its promise not to block.
SLOW_CALL_S = 0.25
#: The status of an add-on is a few lines. Longer than this is not published.
STATUS_JSON_MAX = 16384
_NAME_MAX = 64
_MODE_MAX = 64


@dataclass(frozen=True)
class AddonSnapshot:
    installed: bool = False
    name: str = ""
    api_version: int = 0
    busy: bool = False
    selection_reserved: bool = False
    mode: str = ""
    status_json: str = ""


class RosPorts:
    """What the add-on gets as ``ExtensionContext.ros`` in a hub.

    ``node_name`` is the node the add-on's status is published from. ``publish_status`` can
    be called from any thread: it sets a flag the next poll reads.
    """

    node_name = f"{NODE_NAMESPACE}/{NODE_NAME}"

    def __init__(self) -> None:
        self._requested = False

    def publish_status(self) -> None:
        self._requested = True

    def take_request(self) -> bool:
        """Read and clear the flag. One writer sets it, the poll clears it: a request that
        lands between the two is published by the poll after, and none is lost."""

        asked, self._requested = self._requested, False
        return asked


class AddonFacade:
    """The poll, with no ROS in it."""

    def __init__(
        self,
        app: Any,
        *,
        publish_status: Callable[[AddonSnapshot], Any],
        ports: Optional[RosPorts] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._extension = app.state.extension
        self._publish = publish_status
        self._ports = ports if ports is not None else RosPorts()
        self._monotonic = monotonic
        self.guard = Guarded(log, monotonic=monotonic)
        self.clock = TickClock(POLL_PERIOD_S, monotonic=monotonic)
        self._last: Optional[AddonSnapshot] = None
        self.published = 0
        self.hook_errors = 0
        self.slow_calls = 0
        self.longest_call_s = 0.0
        self.oversize = 0

    def poll(self) -> None:
        """Timer body: ask the add-on, and publish if the answer changed or was asked for."""

        self.clock.tick()
        self.guard.run("the add-on poll", self._poll, default=None)

    def _poll(self) -> None:
        snapshot = self._ask()
        asked = self._ports.take_request()
        if snapshot == self._last and not asked:
            return
        self._publish(snapshot)
        self._last = snapshot
        self.published += 1

    def _timed(self, hook: Callable[[], Any], neutral: Any) -> Any:
        started = self._monotonic()
        try:
            return hook()
        except Exception:  # noqa: BLE001 - an add-on that raises has answered "no"
            self.hook_errors += 1
            self.guard.note_failure("an add-on hook")
            return neutral
        finally:
            took = self._monotonic() - started
            if took > self.longest_call_s:
                self.longest_call_s = took
            if took > SLOW_CALL_S:
                self.slow_calls += 1

    def _ask(self) -> AddonSnapshot:
        extension = self._extension
        name = str(getattr(extension, "name", "") or "")[:_NAME_MAX]
        status = self._timed(extension.status, None)
        busy = bool(self._timed(extension.busy, False))
        reserved = bool(self._timed(extension.selection_reserved, False))
        mode = ""
        status_json = ""
        if isinstance(status, Mapping):
            mode = str(status.get("mode", "") or "")[:_MODE_MAX]
            status_json = self._encode(status)
        return AddonSnapshot(
            installed=bool(name), name=name, api_version=_api_version(extension),
            busy=busy, selection_reserved=reserved, mode=mode, status_json=status_json)

    def _encode(self, status: Mapping[str, Any]) -> str:
        try:
            text = json.dumps(status, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            self.hook_errors += 1
            return ""
        if len(text) > STATUS_JSON_MAX:
            self.oversize += 1
            return ""
        return text

    def stats(self) -> dict[str, Any]:
        return {
            "polls": self.clock.ticks,
            "late_polls": self.clock.late,
            "longest_poll_gap_s": round(self.clock.longest_gap_s, 3),
            "published": self.published,
            "hook_errors": self.hook_errors,
            "slow_calls": self.slow_calls,
            "longest_call_s": round(self.longest_call_s, 3),
            "oversize_status": self.oversize,
            "callback_errors": self.guard.errors,
        }

    def health(self) -> Health:
        stats = self.stats()
        problems = []
        if stats["slow_calls"]:
            problems.append(f"{stats['slow_calls']} add-on hook calls took over "
                            f"{SLOW_CALL_S:g} s, though a hook must not block")
        if stats["hook_errors"]:
            problems.append(f"{stats['hook_errors']} hook errors")
        if stats["oversize_status"]:
            problems.append(f"{stats['oversize_status']} statuses too long to publish")
        if problems:
            return Health(WARN, "; ".join(problems), stats)
        return Health(OK, "the add-on is polled" if self._last and self._last.installed
                      else "no add-on is installed", stats)


def _api_version(extension: Any) -> int:
    try:
        return max(0, int(getattr(extension, "api_version", 0) or 0)) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


# --- the node --------------------------------------------------------------------------


def load_addon_types() -> Mapping[str, Any]:
    from emori_msgs.msg import AddonStatus

    return {"AddonStatus": AddonStatus}


def load_addon_bindings() -> NodeBindings:
    """Import ``rclpy`` and the interface. Raises ``ImportError`` where ROS is absent."""

    return load_node_bindings(load_addon_types)


def fill_status(message: Any, snapshot: AddonSnapshot) -> Any:
    message.installed = snapshot.installed
    message.name = snapshot.name
    message.api_version = snapshot.api_version
    message.busy = snapshot.busy
    message.selection_reserved = snapshot.selection_reserved
    message.mode = snapshot.mode
    message.status_json = snapshot.status_json
    return message


def build_addon_node(
    app: Any,
    bindings: Optional[NodeBindings] = None,
    ports: Optional[RosPorts] = None,
) -> Any:
    """The ``/emori/addon`` node for ``app``, to be added to the facade executor.

    ``node.facade`` is the :class:`AddonFacade`.
    """

    ros = bindings if bindings is not None else load_addon_bindings()
    node = ros.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
    group = ros.exclusive_group()
    status_type = ros.types["AddonStatus"]
    publisher = node.create_publisher(status_type, STATUS_TOPIC, ros.latched_qos)

    def publish(snapshot: AddonSnapshot) -> None:
        message = fill_status(status_type(), snapshot)
        stamp_header(node, message.header)
        publisher.publish(message)

    facade = AddonFacade(app, publish_status=publish, ports=ports)
    node.create_timer(POLL_PERIOD_S, facade.poll, callback_group=group)
    register_diagnostics(app, "addon", facade.health)
    node.facade = facade
    return node


def addon_builder(ports: RosPorts, bindings: Optional[NodeBindings] = None) -> Callable[[Any], Any]:
    """A builder for the facade group that publishes for ``ports``, which the add-on holds."""

    return lambda app: build_addon_node(app, bindings, ports=ports)
