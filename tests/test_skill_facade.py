"""The ROS skill facade's decision surface, exercised on a machine with no ROS.

``emori_skill_facade`` is the only ROS component that may turn a decision into motion,
and it does so through a transport - today by POSTing the hostapp's ``/api/skill``.
Every judgement it makes - may this graph send at all, what the body looks like on the
wire, what a 429 / 409 / 423 means, which counter a refusal belongs in, how an outcome
fills an action result - is a pure function in ``contract.py``, and the hop itself is a
swappable object in ``transport.py``.  This file is why that split exists.

ROS 2 Humble runs in the container on the robot and nowhere else: not on the dev boxes,
not on the CM4 host, not in CI.  Without this, the first thing to notice that a refusal
was mapped to the wrong code would be a robot standing in front of a person, and the
symptom would be "it just does not move".

The pure module is loaded by PATH (``ros2_ws`` is not on ``sys.path`` and installing it
would need ROS).  ``facade_node.py`` is loaded the same way with the package root on
``sys.path``, and it imports cleanly here only because its ``rclpy``/``emori_msgs``
imports are guarded - which this file also pins, since removing the guard would silently
turn the node test below into a collection error.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

# The package root (the directory holding the inner python package), so the node's own
# `from emori_skill_facade... import ...` resolves without ROS and without an install.
_PKG_ROOT = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open"
             / "emori_skill_facade")

_FACADE = _PKG_ROOT / "emori_skill_facade" / "contract.py"
_NODE = _PKG_ROOT / "emori_skill_facade" / "facade_node.py"


_MODULE_NAME = "emori_skill_facade_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _FACADE)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `@dataclass` resolves its annotations through
    # `sys.modules[cls.__module__]`; an unregistered module makes the decorator raise.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _on_path() -> None:
    """Put the package root on sys.path for the two modules that are not standalone."""

    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))


def _load_transport():
    _on_path()
    return importlib.import_module("emori_skill_facade.transport")


def _load_node():
    """``facade_node.py`` by path - the guarded rclpy import is the thing under test."""

    _on_path()
    name = "emori_skill_facade_node_under_test"
    spec = importlib.util.spec_from_file_location(name, _NODE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


facade = _load()
transports = _load_transport()


def test_the_module_imports_without_ros_installed():
    """The split is the contract: the deciding half has no ROS import to guard at all.

    ``facade_node.py`` still guards its own (see the node test at the bottom); this
    module must never acquire one, because every test below imports it directly.
    """

    assert facade.SkillOutcome is not None

    tree = ast.parse(_FACADE.read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
    ros = sorted(name for name in imported
                 if name.split(".")[0] in {"rclpy", "emori_msgs", "diagnostic_msgs"})
    assert ros == [], "contract.py must decide without ROS: %s" % ros


# --- who may POST --------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("ros", "ros"),
    ("ROS", "ros"),
    ("  hostapp ", "hostapp"),
    ("none", "none"),
    ("", "hostapp"),
    # A typo is NOT permission: it degrades to the shipped default, exactly as
    # hostapp/skill_api.py motion_owner does.  Both sides must agree or a misspelling
    # opens the robot to a second writer on one of them.
    ("rso", "hostapp"),
    ("ros2", "hostapp"),
])
def test_the_motion_owner_is_normalised_fail_closed(raw, expected):
    assert facade.normalize_owner(raw) == expected


@pytest.mark.parametrize("owner, allowed", [
    ("ros", True), ("hostapp", False), ("none", False), ("", False), ("junk", False),
])
def test_only_the_ros_owner_sends_anything(owner, allowed):
    assert facade.may_post(owner) is allowed


def test_a_suppressed_request_speaks_the_same_vocabulary_the_hostapp_would():
    """The behaviour node reads one error vocabulary whichever side refused."""

    outcome = facade.suppressed_outcome()

    assert (outcome.accepted, outcome.error_code) == (False, "not_owner")
    assert outcome.counter == "suppressed"
    assert outcome.transient is False


# --- the wire bodies -----------------------------------------------------------------


def test_the_bodies_match_the_api_skill_contract():
    """Field names are the contract (hostapp/skill_api.py -> behavior/cues.py
    skill_to_intents); a renamed key is a 400 the graph cannot diagnose."""

    assert facade.replay_motion_body("nod", 0.5) == {
        "skill": "replay_motion", "name": "nod", "intensity": 0.5}
    assert facade.look_at_body(12, -4, 0.8) == {
        "skill": "look_at", "yaw_deg": 12.0, "pitch_deg": -4.0, "duration": 0.8}
    assert facade.set_expression_body("happy", 0.75) == {
        "skill": "set_expression", "label": "happy", "confidence": 0.75}


def test_the_planners_intensity_is_never_smuggled_in_as_a_measured_confidence():
    """SetExpression carries both; only the one the perception side MEASURED goes on
    the wire, so nothing downstream reads a planning number as an observation."""

    body = facade.set_expression_body("sad", 0.2)

    assert set(body) == {"skill", "label", "confidence"}
    assert body["confidence"] == 0.2


def test_the_bodies_are_numbers_even_when_ros_hands_over_something_else():
    """ROS floats arrive as numpy scalars / ints; json.dumps must not choke on them."""

    body = facade.look_at_body(0, 0, 1)

    assert [type(body[k]) for k in ("yaw_deg", "pitch_deg", "duration")] == [float] * 3


# --- what an answer means ------------------------------------------------------------


@pytest.mark.parametrize("status, body, code", [
    # The intake's own body is authoritative: a 403 is three different faults and the
    # operator needs to know which one.
    (403, '{"error_code": "bad_key"}', "bad_key"),
    (403, '{"error_code": "not_loopback"}', "not_loopback"),
    (403, '{"error_code": "skill_api_disabled"}', "skill_api_disabled"),
    (400, '{"error_code": "out_of_range"}', "out_of_range"),
    (409, '{"ok": false, "error_code": "not_owner"}', "not_owner"),
    (423, '{"ok": false, "error_code": "head_reserved"}', "head_reserved"),
    (429, '{"error_code": "rate_limited"}', "rate_limited"),
    # No body, a truncated one, or something that is not the intake at all: the status
    # is still an answer for the three the intake genuinely uses...
    (409, "", "not_owner"),
    (423, None, "head_reserved"),
    (429, "not json at all", "rate_limited"),
    # ...and everything else stays honestly unnamed rather than being given a meaning.
    (500, "", "http_500"),
    (502, "<html>bad gateway</html>", "http_502"),
    (403, "{}", "http_403"),
])
def test_a_refusal_is_named_by_its_body_and_falls_back_to_its_status(status, body, code):
    assert facade.parse_error_code(status, body) == code

    outcome = facade.refusal_outcome(status, body)
    assert (outcome.accepted, outcome.error_code, outcome.counter) == (False, code, "refused")


@pytest.mark.parametrize("code, transient", [
    # Try the next cue: the queue was busy, the head was borrowed, the budget was spent.
    ("rate_limited", True), ("head_reserved", True), ("dropped", True),
    # Repeating these changes nothing until a human changes something.
    ("not_owner", False), ("bad_key", False), ("skill_api_disabled", False),
    ("out_of_range", False), ("unreachable", False), ("http_500", False),
])
def test_only_the_refusals_that_pass_on_their_own_are_marked_transient(code, transient):
    assert facade.SkillOutcome(False, code).transient is transient


def test_a_200_that_went_into_the_queue_is_an_acceptance():
    outcome = facade.accepted_outcome('{"ok": true, "submitted": 3, "dropped": false}')

    assert (outcome.accepted, outcome.error_code, outcome.counter) == (True, "", "accepted")


def test_a_dropped_200_says_why_instead_of_a_bare_no():
    """The queue was busy with motion that outranks a skill request - an honest
    answer, not a failure, and an action result must never say "no" without a reason."""

    outcome = facade.accepted_outcome('{"ok": true, "submitted": 0, "dropped": true}')

    assert (outcome.accepted, outcome.error_code) == (False, "dropped")
    assert outcome.counter == "dropped"
    assert outcome.transient is True


@pytest.mark.parametrize("body", ["", "   ", "not json", "[]", "null"])
def test_an_unreadable_200_body_is_still_an_acceptance(body):
    """The intents were submitted; a body this node cannot parse does not un-submit
    them, and inventing a failure here would make the graph retry motion twice."""

    assert facade.accepted_outcome(body).accepted is True


def test_an_unanswered_hostapp_is_never_confused_with_a_refusal():
    outcome = facade.unreachable_outcome()

    assert (outcome.accepted, outcome.error_code, outcome.counter) == (
        False, "unreachable", "unreachable")


# --- the action results --------------------------------------------------------------


class _Result:
    """Stand-in for ReplayMotion.Result() / LookAt.Result() (rosidl-generated)."""

    def __init__(self) -> None:
        self.completed = False
        self.reached = False
        self.error_code = "unset"


@pytest.mark.parametrize("outcome, done_field, done, code", [
    (facade.accepted_outcome('{"ok": true}'), "completed", True, ""),
    (facade.accepted_outcome('{"dropped": true}'), "completed", False, "dropped"),
    (facade.refusal_outcome(423, ""), "reached", False, "head_reserved"),
    (facade.suppressed_outcome(), "reached", False, "not_owner"),
    (facade.unreachable_outcome(), "completed", False, "unreachable"),
])
def test_an_outcome_fills_the_action_result_the_two_actions_each_declare(
        outcome, done_field, done, code):
    """ReplayMotion says `completed`, LookAt says `reached`; everything else is shared,
    which is why the mapping is written once rather than correctly twice."""

    result = facade.apply_outcome(_Result(), outcome, done_field=done_field)

    assert getattr(result, done_field) is done
    assert result.error_code == code


def test_every_outcome_lands_in_exactly_one_counter():
    """The ledger must add up: five buckets, and no outcome that misses all of them."""

    counters = facade.new_counters()
    outcomes = [
        facade.accepted_outcome('{"ok": true}'),
        facade.accepted_outcome('{"dropped": true}'),
        facade.refusal_outcome(429, ""),
        facade.suppressed_outcome(),
        facade.unreachable_outcome(),
    ]
    for outcome in outcomes:
        assert outcome.counter in counters, outcome
        counters[outcome.counter] += 1

    assert counters == {"posted": 0, "accepted": 1, "dropped": 1, "refused": 1,
                        "suppressed": 1, "unreachable": 1}


# --- the transport: the hop itself, and the plug point it hangs on -------------------


class _Response:
    """Stand-in for the object urlopen hands back (a context manager with .read())."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _answers(body: str):
    def _urlopen(request, timeout=None):
        return _Response(body)
    return _urlopen


def _fails(error: Exception):
    def _urlopen(request, timeout=None):
        raise error
    return _urlopen


def _http_error(status: int, body: bytes):
    return urllib.error.HTTPError("http://127.0.0.1:8095/api/skill", status, "refused",
                                  None, io.BytesIO(body))


@pytest.mark.parametrize("urlopen, accepted, code, counter", [
    (_answers('{"ok": true, "submitted": 2, "dropped": false}'), True, "", "accepted"),
    # A busy queue is an honest 200, and must not be filed as a refusal.
    (_answers('{"ok": true, "submitted": 0, "dropped": true}'), False, "dropped", "dropped"),
    (_fails(_http_error(429, b'{"error_code": "rate_limited"}')),
     False, "rate_limited", "refused"),
    # A refusal with no body still carries its status, which the contract can name.
    (_fails(_http_error(409, b"")), False, "not_owner", "refused"),
    # Nobody answered at all: its own counter, never confused with "they said no".
    (_fails(urllib.error.URLError("connection refused")), False, "unreachable", "unreachable"),
])
def test_the_http_transport_maps_answers_through_the_contract(
        monkeypatch, urlopen, accepted, code, counter):
    """The transport performs the hop and decides nothing: contract.py names the answer.

    It also never raises. A transport that let urlopen's exception family out would turn
    "the hostapp is busy" into a dead action server on the robot.
    """

    seen: list = []

    def _spy(request, timeout=None):
        seen.append((request, timeout))
        return urlopen(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", _spy)

    transport = transports.HostappHttpTransport(
        url="http://127.0.0.1:8095/api/skill", key="s3cret", timeout_s=0.5)
    outcome = transport.submit(facade.replay_motion_body("nod", 0.5), "engagement:nod")

    assert (outcome.accepted, outcome.error_code, outcome.counter) == (
        accepted, code, counter)

    request, timeout = seen[0]
    assert request.full_url == "http://127.0.0.1:8095/api/skill"
    assert request.get_method() == "POST"
    assert timeout == 0.5
    # The secret travels in the header the intake checks - never in the URL or the body.
    headers = {name.lower(): value for name, value in request.headers.items()}
    assert headers["x-emori-skill-key"] == "s3cret"
    assert json.loads(request.data) == {"skill": "replay_motion", "name": "nod",
                                        "intensity": 0.5}


def test_select_transport_refuses_an_unknown_name():
    """The plug point says which wires exist rather than guessing at a new one.

    Unlike the owner knob, there is no safe default to fall back to here: guessing
    would either move the robot over a wire nobody asked for or silently stop moving it.
    """

    with pytest.raises(ValueError) as raised:
        transports.select_transport("arise_v1", url="http://127.0.0.1:8095/api/skill",
                                    key="s3cret", timeout_s=1.0)

    message = str(raised.value)
    assert "arise_v1" in message
    for known in transports.TRANSPORTS:
        assert known in message, message

    # ...and the names that do exist are built, case- and whitespace-insensitively,
    # because they arrive from an env var and a launch argument.
    http = transports.select_transport(" Hostapp_HTTP ", url="http://h/api/skill",
                                       key="k", timeout_s=1.0)
    log = transports.select_transport("log", url="http://h/api/skill", key="k",
                                      timeout_s=1.0)
    assert isinstance(http, transports.HostappHttpTransport)
    assert isinstance(log, transports.LogOnlyTransport)
    assert (http.name, log.name) == ("hostapp_http", "log")
    assert set(transports.TRANSPORTS) == {"hostapp_http", "log"}


# --- the registry: three tiers, and a plugin that cannot shadow a shipped wire -------


class _Recording(transports.SkillTransport):
    """A plugin transport: records the exact call its factory was given."""

    name = "arise_v1"
    calls: list = []

    def __init__(self, **kw) -> None:
        type(self).calls.append(dict(kw))

    def submit(self, body: dict, reason: str):
        return facade.accepted_outcome('{"ok": true}')

    def describe(self) -> str:
        return "arise_v1 -> a wire this package does not contain"


class _Point:
    """Stand-in for an importlib.metadata EntryPoint (name + load())."""

    def __init__(self, name: str, factory) -> None:
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def test_a_registered_transport_is_built_with_the_call_every_factory_gets(monkeypatch):
    """The plug point's whole promise: the facade calls every factory the same way and
    never learns which one speaks which wire.

    ``node`` is in that call because v2's ``ros_native`` lives on the facade node's own
    executor rather than on a socket; the built-ins take it and drop it.
    """

    monkeypatch.setitem(transports.REGISTERED_TRANSPORTS, "arise_v1", _Recording)
    monkeypatch.setattr(_Recording, "calls", [])
    node = object()

    # ...and the name is matched case- and whitespace-insensitively, like the built-ins,
    # because it arrives from an env var and a launch argument.
    built = transports.select_transport("  ARISE_v1 ", url="http://h/api/skill", key="k",
                                        timeout_s=2.0, log=None, node=node)

    assert isinstance(built, _Recording)
    assert _Recording.calls == [{"url": "http://h/api/skill", "key": "k",
                                 "timeout_s": 2.0, "log": None, "node": node}]
    assert "arise_v1" in transports.known_transports()
    # A plugin's wire is NOT one of this package's: what the facade itself contains has
    # to stay readable from the one dict that names it.
    assert set(transports.TRANSPORTS) == {"hostapp_http", "log"}


def test_the_builtins_accept_the_node_and_ignore_it(monkeypatch):
    """One factory signature or it is not a plug point - an HTTP hop has no node."""

    node = object()

    http = transports.select_transport("hostapp_http", url="http://h/api/skill", key="k",
                                       timeout_s=1.0, node=node)
    log = transports.select_transport("log", node=node)

    assert isinstance(http, transports.HostappHttpTransport)
    assert isinstance(log, transports.LogOnlyTransport)
    assert http.url == "http://h/api/skill"
    assert log.submit(facade.replay_motion_body("nod", 0.4), "engagement:nod").accepted


def test_a_plugin_may_not_take_over_a_shipped_transport_name():
    """``hostapp_http`` means "POST the single-writer intake" wherever it is read - in a
    launch file, in a compose env var, in a rehearsal ledger. A plugin that could
    redefine it could move the robot over a wire nobody chose, and the operator would
    read the same name on both sides of the change.
    """

    with pytest.raises(ValueError) as raised:
        transports.register_transport("hostapp_http", lambda **kw: None)

    assert "built-in" in str(raised.value)
    assert transports.TRANSPORTS["hostapp_http"] is transports.HostappHttpTransport

    # A name is required, and a factory has to be callable: both would otherwise fail
    # later, at select time, on a robot.
    with pytest.raises(ValueError):
        transports.register_transport("  ", lambda **kw: None)
    with pytest.raises(TypeError):
        transports.register_transport("arise_v2", "emori_skill_native.native")


def test_an_entry_point_transport_is_found_with_nobody_importing_it(monkeypatch):
    """How ``EMORI_SKILL_TRANSPORT=ros_native`` resolves in the v2 container: the plugin
    needs rclpy and the adapter core, which this package may not import, so the
    dependency points the other way and the facade reads the entry point group."""

    monkeypatch.setattr(_Recording, "calls", [])
    monkeypatch.setattr(transports, "_entry_points",
                        lambda: [_Point("arise_v1", _Recording)])

    built = transports.select_transport("arise_v1", url="http://h/api/skill", key="k",
                                        timeout_s=1.0)

    assert isinstance(built, _Recording)
    assert "arise_v1" in transports.known_transports()


def test_an_entry_point_can_never_shadow_a_shipped_transport(monkeypatch):
    """Tier order is the defence: an installed package cannot silently redefine what
    ``hostapp_http`` does by publishing the same entry point name."""

    monkeypatch.setattr(_Recording, "calls", [])
    monkeypatch.setattr(transports, "_entry_points",
                        lambda: [_Point("hostapp_http", _Recording)])

    built = transports.select_transport("hostapp_http", url="http://h/api/skill", key="k")

    assert isinstance(built, transports.HostappHttpTransport)
    assert _Recording.calls == []


def test_unreadable_plugin_metadata_never_costs_the_facade_its_own_transports(
        monkeypatch):
    """Reading distribution metadata touches the filesystem, and a half-written colcon
    overlay is a Tuesday on a robot. "No plugins found" is a degradation; "the facade
    will not start" is an outage."""

    def _explode(*_a, **_kw):
        raise OSError("install/ was half written")

    monkeypatch.setattr(transports.metadata, "entry_points", _explode)

    assert transports._entry_points() == []
    assert set(transports.TRANSPORTS) <= set(transports.known_transports())
    assert isinstance(transports.select_transport("log"), transports.LogOnlyTransport)


def test_log_only_transport_accepts_and_records():
    """Moves nothing, answers like a healthy intake, and keeps what it was asked.

    This is what the standalone image's smoke test runs: no hostapp to POST to, no
    robot to move, and still the same ledger and action-result code path.
    """

    transport = transports.LogOnlyTransport()
    body = facade.replay_motion_body("nod", 0.4)

    first = transport.submit(body, "engagement:nod")
    second = transport.submit(facade.set_expression_body("happy", 0.8), "cue:levity")

    assert (first.accepted, first.error_code, first.counter) == (True, "", "accepted")
    assert (second.accepted, second.counter) == (True, "accepted")
    assert [recorded["skill"] for recorded, _ in transport.bodies] == [
        "replay_motion", "set_expression"]
    assert [reason for _, reason in transport.bodies] == ["engagement:nod", "cue:levity"]

    # The record is a copy: a caller reusing its dict must not rewrite history.
    body["name"] = "farewell"
    assert transport.bodies[0][0]["name"] == "nod"


def test_the_node_file_imports_without_ros():
    """The guard is the contract: without it this file could not be read off the robot.

    Humble is in the container and nowhere else, so the only chance to notice that the
    node module itself is broken - a typo in an import, a name used before it exists -
    is here, before it becomes a node that never comes up on a robot.
    """

    node = _load_node()

    assert node.ROS_AVAILABLE is False
    assert node.SkillFacadeNode is not None
    # ...and it refuses to pretend: no ROS means no facade, said out loud.
    with pytest.raises(RuntimeError):
        node.main()


def test_destroying_the_node_closes_a_transport_that_has_something_to_close():
    """`ros_native` owns a batch worker THREAD that outlives `rclpy.shutdown()`. Between
    shutdown and process exit it can still be waiting on a future that will never
    complete, and can still send a goal to an action client whose node is being
    destroyed under it. `hostapp_http` owns an httpx client; `log` owns nothing - so
    this asks rather than assumes."""

    node_module = _load_node()

    class _Closable:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    class _Bare:
        """A transport with no `close` at all: `log` is one, and must not raise."""

    for transport, expected in ((_Closable(), 1), (_Bare(), None)):
        node = object.__new__(node_module.SkillFacadeNode)
        node.transport = transport
        assert node_module.SkillFacadeNode.destroy_node(node) is True
        if expected is not None:
            assert transport.closed == expected

    # A transport whose close() raises must not stop the node being destroyed.
    class _Angry:
        def close(self) -> None:
            raise RuntimeError("the socket is already gone")

    class _Log:
        def warn(self, _message: str) -> None:
            self.said = True

    node = object.__new__(node_module.SkillFacadeNode)
    node.transport = _Angry()
    logger = _Log()
    node.get_logger = lambda: logger
    assert node_module.SkillFacadeNode.destroy_node(node) is True
    assert getattr(logger, "said", False) is True


def test_the_http_transport_does_not_raise_when_the_url_itself_is_broken():
    """A scheme-less EMORI_SKILL_URL makes urllib's Request() raise before any socket is
    opened. That must still come back as an outcome: an exception escaping `submit`
    leaves `posted` incremented with nothing counted against it, and the action server
    would answer a default Result whose empty error_code the behaviours node counts
    as `result_ok` -- a total outage that reads as success on both ledgers.
    """

    transport = transports.HostappHttpTransport(url="127.0.0.1:8095/api/skill", key="k")
    outcome = transport.submit(facade.replay_motion_body("nod", 0.5), "engagement:nod")
    assert outcome.accepted is False
    assert outcome.counter == "unreachable"


# --- the two rig-anomaly fixes: a stale WARN, and an unattributable refusal count ------


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def debug(self, message: str) -> None:
        pass


class _SendingTransport:
    """A transport that accepts everything and has no ledger of its own.

    `hostapp_http` and `log` are exactly this shape. Tests that want a ledger attach a
    `counters` attribute, which is how `ros_native` differs.
    """

    def submit(self, body, reason):
        return facade.SkillOutcome(accepted=True, counter="accepted")


def _facade_node(node_module, *, transport_name: str, skill_key: str = "",
                 transport=None):
    """A node instance with only the attributes the method under test reads.

    `object.__new__` rather than a constructor: `SkillFacadeNode.__init__` builds action
    servers on an rclpy executor, and rclpy is not installed anywhere this test runs.
    """

    node = object.__new__(node_module.SkillFacadeNode)
    node._lock = threading.Lock()
    node.counters = facade.new_counters()
    node.motion_owner = "ros"
    node.skill_key = skill_key
    node.transport_name = transport_name
    node.transport = transport if transport is not None else _SendingTransport()
    node._warned_no_key = False
    node._last_unreachable = 0
    node._last_ledger_log = time.monotonic()
    node.url_host = "127.0.0.1:8095"
    node.skill_url = "http://127.0.0.1:8095/api/skill"
    node._logger = _Logger()
    node.get_logger = lambda: node._logger
    return node


def test_the_empty_key_warning_is_silent_under_the_native_transport(monkeypatch):
    """Measured on the robot: `EMORI_SKILL_KEY is empty` at WARN under
    `EMORI_SKILL_TRANSPORT=ros_native`, where there is no hostapp in the path and no key
    to be empty. Noise at WARN level, on a card that is 94% full."""

    node_module = _load_node()

    native = _facade_node(node_module, transport_name="ros_native")
    node_module.SkillFacadeNode._submit(native, {"skill": "replay_motion"}, "cue")
    assert native._logger.warnings == []

    http = _facade_node(node_module, transport_name=node_module.DEFAULT_TRANSPORT)
    node_module.SkillFacadeNode._submit(http, {"skill": "replay_motion"}, "cue")
    assert any("EMORI_SKILL_KEY is empty" in line for line in http._logger.warnings)


def _capture_ledger(monkeypatch, node_module, node) -> dict[str, str]:
    """Run `_publish_ledger` with the diagnostic message types faked in."""

    class _KeyValue:
        def __init__(self, key: str = "", value: str = "") -> None:
            self.key, self.value = key, value

    class _Status:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class _Array:
        def __init__(self) -> None:
            self.header = type("H", (), {"stamp": None})()
            self.status: list = []

    published: list = []
    monkeypatch.setattr(node_module, "KeyValue", _KeyValue, raising=False)
    monkeypatch.setattr(node_module, "DiagnosticStatus",
                        type("S", (), {"OK": 0, "WARN": 1, "ERROR": 2,
                                       "__init__": lambda self, **kw: _Status.__init__(
                                           self, **kw)}), raising=False)
    monkeypatch.setattr(node_module, "DiagnosticArray", _Array, raising=False)

    node._diagnostics = type("P", (), {"publish": lambda _self, msg: published.append(msg)})()
    node.get_clock = lambda: type("C", (), {
        "now": lambda _self: type("T", (), {"to_msg": lambda _s: None})()})()

    node_module.SkillFacadeNode._publish_ledger(node)
    assert published, "the ledger was never published"
    return {kv.key: kv.value for kv in published[0].status[0].values}


def test_the_transports_own_refusal_breakdown_reaches_diagnostics(monkeypatch):
    """`refused=19` on the robot was unattributable above DEBUG: the native transport logs
    the refusal CODE at debug level and INFO-level docker logs drop it, so the only way
    to learn whether nineteen goals were `busy` or `not_owner` was to restart the
    container with a different log level and hope it happened again."""

    node_module = _load_node()
    transport = _SendingTransport()
    transport.counters = lambda: {
        "goals_sent": 21, "goals_completed": 2, "goals_refused_busy": 17,
        "goals_refused_not_owner": 2, "batches_cancelled": 0,
    }
    node = _facade_node(node_module, transport_name="ros_native", transport=transport)

    values = _capture_ledger(monkeypatch, node_module, node)
    assert values["goals_refused_busy"] == "17"
    assert values["goals_refused_not_owner"] == "2"
    assert values["goals_sent"] == "21"
    # ...and the facade's own six counters are still there, unrenamed.
    assert values["posted"] == "0"
    assert values["transport"] == "ros_native"


def test_a_transport_that_exposes_its_ledger_as_a_property_works_too(monkeypatch):
    node_module = _load_node()

    class _PropertyTransport(_SendingTransport):
        counters = {"goals_refused_dropped": 4}

    node = _facade_node(node_module, transport_name="ros_native",
                        transport=_PropertyTransport())
    assert _capture_ledger(monkeypatch, node_module, node)["goals_refused_dropped"] == "4"


def test_a_transport_with_no_ledger_publishes_exactly_what_it_always_did(monkeypatch):
    """`hostapp_http` and `log` have no `counters`, and the ledger must be unchanged for
    them - this is a v1 robot's /diagnostics."""

    node_module = _load_node()
    node = _facade_node(node_module, transport_name=node_module.DEFAULT_TRANSPORT)
    values = _capture_ledger(monkeypatch, node_module, node)
    assert set(values) == set(facade.new_counters()) | {
        "motion_owner", "transport", "url_host"}


def test_a_transport_whose_ledger_raises_does_not_break_diagnostics(monkeypatch):
    """/diagnostics is how an operator triages a robot with no debugger attached. A
    transport bookkeeping bug must not be the thing that takes it away."""

    node_module = _load_node()
    transport = _SendingTransport()

    def _angry():
        raise RuntimeError("the ledger lock is held by a dead thread")

    transport.counters = _angry
    node = _facade_node(node_module, transport_name="ros_native", transport=transport)
    values = _capture_ledger(monkeypatch, node_module, node)
    assert values["transport"] == "ros_native"
