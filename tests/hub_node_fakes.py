"""Fakes for the hub's facade nodes: a node, its timers, services and publishers, and messages.

Every message and service here is built from the real ``.msg`` and ``.srv`` files in
``emori_msgs``, and refuses an attribute the file does not declare, a value of the wrong
type and an integer outside its width. A facade that writes a field the interface lacks, or
a queue depth of 300 into a ``uint8``, fails in a test and not on the robot.

The node is driven by calling its timers and services directly, which is what an executor
does. Nothing here needs ROS.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from emori_hub.facade_kit import NodeBindings

_MSGS = Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "emori_msgs"

_PRIMITIVES: dict[str, tuple[Any, Any]] = {
    "bool": (bool, False),
    "string": (str, ""),
    "float64": (float, 0.0),
    "float32": (float, 0.0),
    "uint8": (int, 0),
    "uint32": (int, 0),
    "int32": (int, 0),
    "byte": (bytes, b"\x00"),
}
_INT_RANGES = {"uint8": (0, 255), "uint32": (0, 2**32 - 1), "int32": (-(2**31), 2**31 - 1)}


class Message:
    """An interface instance that accepts only its own fields, with their types."""

    def __init__(self, name: str, fields: dict[str, Any], kinds: dict[str, Any],
                 ranges: dict[str, tuple[int, int]]) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_kinds", kinds)
        object.__setattr__(self, "_ranges", ranges)
        for key, value in fields.items():
            object.__setattr__(self, key, value)

    def __setattr__(self, key: str, value: Any) -> None:
        kinds = self._kinds
        if key not in kinds:
            raise AttributeError(f"{key} is not a field of {self._name}")
        kind = kinds[key]
        if isinstance(kind, tuple):
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"{key} must be a list")
            for item in value:
                self._check(key, kind[0], item, None)
        else:
            self._check(key, kind, value, self._ranges.get(key))
        object.__setattr__(self, key, value)

    @staticmethod
    def _check(key: str, kind: Any, value: Any, limits: tuple[int, int] | None) -> None:
        if kind is object:
            return
        if kind is float and isinstance(value, bool):
            raise TypeError(f"{key} must be a float")
        if kind is int and isinstance(value, bool):
            raise TypeError(f"{key} must be an int")
        if kind is float and isinstance(value, int):
            raise TypeError(f"{key} must be a float, not an int")
        if not isinstance(value, kind):
            raise TypeError(f"{key} must be {kind.__name__}, got {type(value).__name__}")
        if limits is not None and not limits[0] <= value <= limits[1]:
            raise ValueError(f"{key} is outside {limits}")


def _lines(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]


def _build(name: str, body: list[str]) -> Message:
    fields: dict[str, Any] = {}
    kinds: dict[str, Any] = {}
    ranges: dict[str, tuple[int, int]] = {}
    for line in body:
        if "=" in line:  # a constant
            continue
        type_name, field = line.split()[:2]
        if type_name.endswith("[]"):
            fields[field], kinds[field] = [], (object,)
        elif type_name in _PRIMITIVES:
            kinds[field], fields[field] = _PRIMITIVES[type_name]
            if type_name in _INT_RANGES:
                ranges[field] = _INT_RANGES[type_name]
        elif type_name == "builtin_interfaces/Time":
            fields[field] = _build("Time", ["int32 sec", "uint32 nanosec"])
            kinds[field] = object
        elif type_name == "std_msgs/Header":
            fields[field] = _build("Header", ["builtin_interfaces/Time stamp", "string frame_id"])
            kinds[field] = object
        else:
            fields[field] = _build(type_name, _lines(_MSGS / "msg" / f"{type_name}.msg"))
            kinds[field] = object
    return Message(name, fields, kinds, ranges)


def msg_type(name: str) -> Callable[[], Message]:
    body = _lines(_MSGS / "msg" / f"{name}.msg")
    return lambda: _build(name, body)


def srv_type(name: str) -> Any:
    body = _lines(_MSGS / "srv" / f"{name}.srv")
    split = body.index("---")
    request, response = body[:split], body[split + 1:]
    return SimpleNamespace(Request=lambda: _build(f"{name}.Request", request),
                           Response=lambda: _build(f"{name}.Response", response))


def diagnostic_types() -> dict[str, Callable[[], Message]]:
    """``diagnostic_msgs`` as they are generated: ``level`` is a single byte."""

    return {
        "KeyValue": lambda: _build("KeyValue", ["string key", "string value"]),
        "DiagnosticStatus": lambda: _build("DiagnosticStatus", [
            "byte level", "string name", "string message", "string hardware_id",
            "KeyValue[] values"]),
        "DiagnosticArray": lambda: _build("DiagnosticArray", [
            "std_msgs/Header header", "DiagnosticStatus[] status"]),
    }


# --- the node --------------------------------------------------------------------------


class FakePublisher:
    def __init__(self, type_: Any, topic: str, qos: Any) -> None:
        self.type, self.topic, self.qos = type_, topic, qos
        self.published: list[Any] = []

    def publish(self, message: Any) -> None:
        self.published.append(message)


class FakeTimer:
    def __init__(self, period: float, callback: Callable[[], Any], group: Any) -> None:
        self.period, self.callback, self.group = period, callback, group

    def fire(self) -> None:
        self.callback()


class FakeService:
    def __init__(self, type_: Any, name: str, callback: Callable[[Any, Any], Any],
                 group: Any) -> None:
        self.type, self.name, self.callback, self.group = type_, name, callback, group

    def call(self, **fields: Any) -> Any:
        request = self.type.Request()
        for key, value in fields.items():
            setattr(request, key, value)
        return self.callback(request, self.type.Response())


class _Clock:
    def __init__(self) -> None:
        self.sec = 100

    def now(self) -> Any:
        return SimpleNamespace(to_msg=lambda: _build("Time", ["int32 sec", "uint32 nanosec"]))


class FakeNode:
    def __init__(self, name: str, namespace: str = "") -> None:
        self.name, self.namespace = name, namespace
        self.publishers: dict[str, FakePublisher] = {}
        self.timers: list[FakeTimer] = []
        self.services: dict[str, FakeService] = {}
        self.destroyed = False

    def create_publisher(self, type_: Any, topic: str, qos: Any) -> FakePublisher:
        publisher = FakePublisher(type_, topic, qos)
        self.publishers[topic] = publisher
        return publisher

    def create_timer(self, period: float, callback: Callable[[], Any], callback_group: Any = None):
        timer = FakeTimer(period, callback, callback_group)
        self.timers.append(timer)
        return timer

    def create_service(self, type_: Any, name: str, callback: Callable[[Any, Any], Any],
                       callback_group: Any = None) -> FakeService:
        service = FakeService(type_, name, callback, callback_group)
        self.services[name] = service
        return service

    def get_clock(self) -> _Clock:
        return _Clock()

    def destroy_node(self) -> None:
        self.destroyed = True


class Groups:
    """Hands out callback groups, so a test can see which callback is on which."""

    def __init__(self) -> None:
        self.made: list[SimpleNamespace] = []

    def __call__(self) -> SimpleNamespace:
        group = SimpleNamespace(index=len(self.made))
        self.made.append(group)
        return group


def bindings(types: dict[str, Any]) -> tuple[NodeBindings, list[FakeNode], Groups]:
    """Bindings whose nodes are :class:`FakeNode`; also the list of nodes made."""

    nodes: list[FakeNode] = []
    groups = Groups()

    def create_node(name: str, namespace: str = "") -> FakeNode:
        node = FakeNode(name, namespace)
        nodes.append(node)
        return node

    return (NodeBindings(create_node=create_node, exclusive_group=groups,
                         latched_qos="latched", default_qos="default", types=types),
            nodes, groups)
