"""``/emori/gateway``: the hub's status as a service, and ``/diagnostics``.

**The status service.** ``/emori/gateway/status`` answers what ``GET /api/status`` and ``GET
/healthz`` answer, in one JSON string, by calling the functions those two routes are served
with. There is no second copy of either answer to drift. What leaves is the same answer with
three differences, because DDS is not a loopback HTTP port:

* The identifiers of the open session, and the session the motion trace is writing, pass the
  same gate as every other message of the process (``EMORI_ROS_DEIDENTIFY``).
* ``/healthz`` is reduced to the keys it is documented to carry for a person, so the names of
  the credential in force and its fingerprint stay on HTTP.
* Any key anywhere in the answer whose name says it holds a secret is dropped. This is a net
  under the two rules above and not a replacement for them.

The answer is kept for :data:`STATUS_TTL_S` and handed to every caller within that time. The
routes read locks of the core briefly (the watchdog's, the voice tap's), and a client that
calls the service in a loop must not be able to turn that into a load on them.

**Diagnostics.** ``/diagnostics`` (``diagnostic_msgs/DiagnosticArray``, once a second) has
one entry per source: the robot link, the session fan-out, and every source a facade
registered with :func:`~emori_hub.facade_kit.register_diagnostics`. Sources answer from
counters they hold. Nothing here calls the robot, takes a lock of the session core or reads
the driver, so this timer costs the session nothing.

The one thing a person should look for there is the loss counters. The hub is built so that
when a topic and the robot's motion compete for time, the topic gives way, and the topic's
loss is counted: the fan-out's dropped envelopes, a facade's late ticks. A warning there is
a graph that is behind. It is never a head that is.

Nothing here imports ROS or the adapter core when this module is loaded.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from emori_hub.facade_kit import (
    ERROR,
    NODE_NAMESPACE,
    OK,
    STALE,
    WARN,
    Guarded,
    Health,
    NodeBindings,
    TickClock,
    diagnostic_sources,
    find_endpoint,
    load_node_bindings,
    register_diagnostics,
    stamp_header,
)

__all__ = [
    "DIAGNOSTICS_PERIOD_S",
    "DIAGNOSTICS_TOPIC",
    "DiagnosticEntry",
    "GatewayFacade",
    "HARDWARE_ID",
    "NODE_NAME",
    "STATUS_JSON_MAX",
    "STATUS_SERVICE",
    "SESSION_STATUS_TTL_S",
    "STATUS_TTL_S",
    "build_gateway_node",
    "load_gateway_bindings",
    "scrub",
]

log = logging.getLogger("emori.hub.gateway")

NODE_NAME = "gateway"
STATUS_SERVICE = "/emori/gateway/status"
DIAGNOSTICS_TOPIC = "/diagnostics"
HARDWARE_ID = "emori-hub"
DIAGNOSTICS_PERIOD_S = 1.0

#: How long one answer is handed to every caller.
STATUS_TTL_S = 0.5
#: The same while a session is open, so what a caller reads can be this old.
SESSION_STATUS_TTL_S = 5.0
STATUS_JSON_MAX = 65536

#: The keys of ``/healthz`` that are for a person. The route also names the credential in
#: force, and its fingerprint: those stay on HTTP.
_HEALTHZ_KEYS = ("status", "demo", "partner", "robot", "runtime", "roster_staff")
#: A key containing one of these holds, or may hold, a secret.
_SECRET_WORDS = ("token", "secret", "password", "passwd", "credential", "fingerprint", "api_key",
                 "apikey", "salt")
_ENTRY_NAME_PREFIX = "emori_hub/"


@dataclass(frozen=True)
class DiagnosticEntry:
    name: str
    level: int
    message: str
    values: Mapping[str, str]


def scrub(value: Any) -> Any:
    """``value`` without any key, at any depth, whose name says it holds a secret."""

    if isinstance(value, Mapping):
        return {key: scrub(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in _SECRET_WORDS)}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


class GatewayFacade:
    """The status answer and the diagnostics tick, with no ROS in them."""

    def __init__(
        self,
        app: Any,
        *,
        publish_diagnostics: Callable[[list[DiagnosticEntry]], Any],
        privacy: Any,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._publish = publish_diagnostics
        self._privacy = privacy
        self._monotonic = monotonic
        self._healthz = find_endpoint(app, "/healthz")
        self._api_status = find_endpoint(app, "/api/status")
        self.guard = Guarded(log, monotonic=monotonic)
        self.clock = TickClock(DIAGNOSTICS_PERIOD_S, monotonic=monotonic)
        self._cached: Optional[tuple[float, tuple[str, bool, bool, str]]] = None
        self.status_calls = 0
        self.status_built = 0
        self.route_errors = 0
        self.published = 0
        self.source_errors = 0
        register_diagnostics(app, "gateway", self.health)

    # --- the status service --------------------------------------------------------------

    def status(self, _request: Any, response: Any) -> Any:
        """Service body. Never raises."""

        self.status_calls += 1
        answer = self.guard.run("the gateway status service", self._answer,
                                default=("http", False, False, '{"error":"unavailable"}'))
        response.runtime, response.demo, response.robot, response.status_json = answer
        return response

    def _session_open(self) -> bool:
        tracker = getattr(self._app.state.driver, "tracker", None)
        return True if tracker is None else bool(getattr(tracker, "active", True))

    def _answer(self) -> tuple[str, bool, bool, str]:
        now = self._monotonic()
        cached = self._cached
        # Each build makes two status calls to the robot, on the thread that delivers every
        # acknowledgement a sentence's moves wait for, so a session makes them rarely.
        ttl = SESSION_STATUS_TTL_S if self._session_open() else STATUS_TTL_S
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        answer = self._build()
        self._cached = (now, answer)
        self.status_built += 1
        return answer

    def _build(self) -> tuple[str, bool, bool, str]:
        health = self._call(self._healthz, "/healthz")
        status = self._call(self._api_status, "/api/status")
        health = {key: health[key] for key in _HEALTHZ_KEYS if key in health}
        runtime = "ros" if health.get("runtime") == "ros" else "http"
        body = {"healthz": scrub(health), "status": scrub(self._gated(status))}
        text = self._encode(body)
        if len(text) > STATUS_JSON_MAX:
            text = self._encode({"healthz": body["healthz"], "status": {"truncated": True}})
        return runtime, bool(health.get("demo", False)), bool(health.get("robot", False)), text

    def _call(self, endpoint: Optional[Callable[[], Any]], path: str) -> dict:
        """What a route answers, as a dict; empty if there is no route or it failed."""

        if endpoint is None:
            self.route_errors += 1
            return {}
        try:
            answer = endpoint()
        except Exception:  # noqa: BLE001 - the other half is still worth sending
            self.route_errors += 1
            self.guard.note_failure(f"the {path} route")
            return {}
        body = getattr(answer, "body", None)  # a JSONResponse
        if isinstance(body, (bytes, bytearray)):
            try:
                answer = json.loads(body)
            except ValueError:
                answer = {}
        return dict(answer) if isinstance(answer, Mapping) else {}

    def _gated(self, status: Mapping[str, Any]) -> dict:
        gated = dict(status)
        session = gated.get("session")
        if isinstance(session, Mapping):
            person, sid = self._privacy.apply_ids(str(session.get("person_id") or ""),
                                                  str(session.get("session_id") or ""))
            gated["session"] = {**session, "person_id": person, "session_id": sid}
        trace = gated.get("motion_trace")
        if isinstance(trace, Mapping) and trace.get("session"):
            _person, sid = self._privacy.apply_ids("", str(trace["session"]))
            gated["motion_trace"] = {**trace, "session": sid}
        return gated

    @staticmethod
    def _encode(body: Mapping[str, Any]) -> str:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)

    # --- diagnostics ---------------------------------------------------------------------

    def tick(self) -> None:
        """Timer body: collect every source and publish."""

        self.clock.tick()
        self.guard.run("the diagnostics tick", self._tick, default=None)

    def _tick(self) -> None:
        self._publish(self.collect())
        self.published += 1

    def collect(self) -> list[DiagnosticEntry]:
        sources: dict[str, Callable[[], Health]] = {
            "robot_link": self._robot_link,
            "session_fanout": self._fanout,
            **diagnostic_sources(self._app),
        }
        return [self._entry(name, source) for name, source in sorted(sources.items())]

    def _entry(self, name: str, source: Callable[[], Health]) -> DiagnosticEntry:
        try:
            health = source()
        except Exception:  # noqa: BLE001 - one source must not hide the others
            self.source_errors += 1
            self.guard.note_failure(f"the diagnostics source {name}")
            health = Health(STALE, "the source raised")
        return DiagnosticEntry(
            name=_ENTRY_NAME_PREFIX + name, level=int(health.level), message=str(health.message),
            values={str(key): str(value) for key, value in health.values.items()})

    def _robot_link(self) -> Health:
        client = getattr(self._app.state.driver, "client", None)
        link = getattr(client, "link", None)
        if link is None:
            return Health(OK, "the robot is reached over REST",
                          {"runtime": "ros" if getattr(client, "runtime", "") == "ros" else "http"})
        reason = link.down_reason()
        values = {"runtime": "ros", "first_unmatched": link.first_unmatched()}
        return Health(ERROR if reason else OK, reason or "the robot link is up", values)

    def _fanout(self) -> Health:
        fanout = getattr(self._app.state, "fanout", None)
        if fanout is None:
            return Health(OK, "no session fan-out is installed")
        stats = fanout.stats()
        if not stats.get("running", False):
            return Health(ERROR, "the session fan-out is not running", stats)
        problems = []
        if stats.get("dropped"):
            problems.append(f"{stats['dropped']} envelopes dropped on a full queue")
        failures = sum(int(stats.get(key) or 0) for key in
                       ("publish_errors", "feed_errors", "mapping_errors"))
        if failures:
            problems.append(f"{failures} envelopes failed in a sink")
        if (stats.get("busy_for_s") or 0.0) > 5.0:
            problems.append("a sink has held the fan-out for over 5 s")
        if problems:
            return Health(WARN, "; ".join(problems) + ". The loss is on the topic: the "
                          "session's motion does not wait for it.", stats)
        return Health(OK, "the session fan-out is keeping up", stats)

    def stats(self) -> dict[str, Any]:
        return {
            "status_calls": self.status_calls,
            "status_built": self.status_built,
            "route_errors": self.route_errors,
            "diagnostics_published": self.published,
            "late_diagnostics_ticks": self.clock.late,
            "source_errors": self.source_errors,
            "callback_errors": self.guard.errors,
        }

    def health(self) -> Health:
        stats = self.stats()
        problems = []
        if stats["route_errors"]:
            problems.append(f"{stats['route_errors']} status routes failed or are missing")
        if stats["source_errors"]:
            problems.append(f"{stats['source_errors']} diagnostics sources raised")
        if stats["callback_errors"]:
            problems.append(f"{stats['callback_errors']} callback errors")
        if problems:
            return Health(WARN, "; ".join(problems), stats)
        return Health(OK, "the gateway is answering", stats)


# --- the node --------------------------------------------------------------------------


def load_gateway_types() -> Mapping[str, Any]:
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from emori_msgs.srv import GetHubStatus

    return {"GetHubStatus": GetHubStatus, "DiagnosticArray": DiagnosticArray,
            "DiagnosticStatus": DiagnosticStatus, "KeyValue": KeyValue}


def load_gateway_bindings() -> NodeBindings:
    """Import ``rclpy`` and the interfaces. Raises ``ImportError`` where ROS is absent."""

    return load_node_bindings(load_gateway_types)


def _fill_array(ros: NodeBindings, node: Any, entries: list[DiagnosticEntry]) -> Any:
    array = ros.types["DiagnosticArray"]()
    stamp_header(node, array.header)
    statuses = []
    for entry in entries:
        status = ros.types["DiagnosticStatus"]()
        status.level = bytes([max(OK, min(STALE, entry.level))])
        status.name = entry.name
        status.message = entry.message
        status.hardware_id = HARDWARE_ID
        pairs = []
        for key, value in entry.values.items():
            pair = ros.types["KeyValue"]()
            pair.key, pair.value = key, value
            pairs.append(pair)
        status.values = pairs
        statuses.append(status)
    array.status = statuses
    return array


def build_gateway_node(
    app: Any,
    bindings: Optional[NodeBindings] = None,
    privacy: Any = None,
) -> Any:
    """The ``/emori/gateway`` node for ``app``, to be added to the facade executor.

    ``privacy`` is the adapter node's gate; unset, it is read from the environment the way
    the fan-out reads it. ``node.facade`` is the :class:`GatewayFacade`.
    """

    ros = bindings if bindings is not None else load_gateway_bindings()
    if privacy is None:
        from emori_hub.fanout import load_privacy

        privacy = load_privacy()
    node = ros.create_node(NODE_NAME, namespace=NODE_NAMESPACE)
    state_group, command_group = ros.exclusive_group(), ros.exclusive_group()
    publisher = node.create_publisher(ros.types["DiagnosticArray"], DIAGNOSTICS_TOPIC,
                                      ros.default_qos)

    def publish(entries: list[DiagnosticEntry]) -> None:
        publisher.publish(_fill_array(ros, node, entries))

    facade = GatewayFacade(app, publish_diagnostics=publish, privacy=privacy)
    node.create_timer(DIAGNOSTICS_PERIOD_S, facade.tick, callback_group=state_group)
    node.create_service(ros.types["GetHubStatus"], STATUS_SERVICE, facade.status,
                        callback_group=command_group)
    node.facade = facade
    return node
