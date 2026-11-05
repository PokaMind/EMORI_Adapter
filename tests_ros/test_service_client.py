"""What the robot link assumes of an rclpy service client, against a real one.

``emori_hub.robot_link`` calls ``call_async`` and ``service_is_ready`` from whatever thread
a session happens to be on, while a single executor on its own thread delivers the answers,
and it drops a request nobody waits for any more with ``remove_pending_request``. The unit
tests of the link use fakes with those three methods; these use the real ones.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

import ros_support
from ros_support import Spinner, wait_until

CALLERS = 8
CALLS_PER_CALLER = 10
ANSWER_S = 10.0


class EchoServer:
    """A ``std_srvs/SetBool`` server on its own node and thread.

    It answers ``success = data`` and ``message = "T" or "F"``, so a caller can tell its own
    answer from another's. While :attr:`hold` is clear a request waits in the handler, after
    setting :attr:`entered`; that is how a test makes an answer late.
    """

    def __init__(self, rclpy: Any, context: Any, name: str) -> None:
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.executors import MultiThreadedExecutor
        from std_srvs.srv import SetBool

        self.name = name
        self.hold = threading.Event()
        self.hold.set()
        self.entered = threading.Event()
        self.answered = threading.Event()
        self.handled = 0
        self._lock = threading.Lock()
        self.node = rclpy.create_node(f"echo_{uuid.uuid4().hex[:8]}", context=context)
        self._name = name
        self._group = ReentrantCallbackGroup()
        self._service_type = SetBool
        self._executor_type = MultiThreadedExecutor
        self._context = context
        self.spinner = None

    def start(self) -> "EchoServer":
        # Here and not above: a service is matched as soon as it exists, spinning or not.
        self.node.create_service(self._service_type, self._name, self._handle,
                                 callback_group=self._group)
        executor = self._executor_type(num_threads=4, context=self._context)
        executor.add_node(self.node)
        self.spinner = Spinner(executor, "echo-server").start()
        return self

    def _handle(self, request: Any, response: Any) -> Any:
        self.entered.set()
        self.hold.wait(ANSWER_S)
        with self._lock:
            self.handled += 1
        response.success = bool(request.data)
        response.message = "T" if request.data else "F"
        self.answered.set()
        return response

    def stop(self) -> None:
        if self.node is None:
            return
        if self.spinner is not None:
            self.spinner.stop()
        self.node.destroy_node()
        self.node = None


class Caller:
    """The robot link's side: a client on a node whose executor has a thread of its own."""

    def __init__(self, rclpy: Any, context: Any, name: str) -> None:
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.executors import SingleThreadedExecutor
        from std_srvs.srv import SetBool

        self.request_type = SetBool.Request
        self.node = rclpy.create_node(f"caller_{uuid.uuid4().hex[:8]}", context=context)
        self.client = self.node.create_client(SetBool, name,
                                              callback_group=ReentrantCallbackGroup())
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(self.node)
        self.spinner = Spinner(executor, "caller")

    def start(self) -> "Caller":
        self.spinner.start()
        return self

    def request(self, data: bool) -> Any:
        request = self.request_type()
        request.data = data
        return request

    def stop(self) -> None:
        if self.node is None:
            return
        self.spinner.stop()
        self.node.destroy_node()
        self.node = None


def _unique_name() -> str:
    return f"/emori_test/s{uuid.uuid4().hex[:12]}/echo"


@pytest.fixture
def pair(rclpy: Any, ros_context: Any):
    """A caller and a server on one service name; the server is started by the test."""

    name = _unique_name()
    caller = Caller(rclpy, ros_context, name).start()
    server = EchoServer(rclpy, ros_context, name)
    try:
        yield caller, server
    finally:
        server.hold.set()
        caller.stop()
        server.stop()


def test_service_is_ready_follows_the_server(pair: Any) -> None:
    caller, server = pair
    assert caller.client.service_is_ready() is False
    server.start()
    assert wait_until(caller.client.service_is_ready, ANSWER_S), "the server was never matched"

    # The link reads this from the threads of its callers, while the executor is spinning.
    failures: list[BaseException] = []

    def poll() -> None:
        try:
            for _ in range(200):
                assert caller.client.service_is_ready() is True
        except BaseException as error:  # noqa: BLE001 - reported below
            failures.append(error)

    threads = [threading.Thread(target=poll) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(ANSWER_S)
    assert not failures, failures

    server.stop()
    assert wait_until(lambda: not caller.client.service_is_ready(), ANSWER_S), \
        "a server that went away still reads as matched"


def test_call_async_from_many_threads_each_gets_its_own_answer(pair: Any) -> None:
    caller, server = pair
    server.start()
    assert wait_until(caller.client.service_is_ready, ANSWER_S)

    mismatches: list[str] = []
    failures: list[BaseException] = []

    def call(index: int) -> None:
        try:
            for turn in range(CALLS_PER_CALLER):
                wanted = (index + turn) % 2 == 0
                future = caller.client.call_async(caller.request(wanted))
                done = threading.Event()
                future.add_done_callback(lambda _future, event=done: event.set())
                if not done.wait(ANSWER_S):
                    mismatches.append(f"caller {index} turn {turn}: no answer")
                    continue
                answer = future.result()
                if answer.success is not wanted or answer.message != ("T" if wanted else "F"):
                    mismatches.append(f"caller {index} turn {turn}: got {answer.message!r}")
        except BaseException as error:  # noqa: BLE001 - reported below
            failures.append(error)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(ANSWER_S * 3)
    assert not failures, failures
    assert not mismatches, mismatches
    assert server.handled == CALLERS * CALLS_PER_CALLER
    assert not caller.spinner.errors


def test_remove_pending_request_drops_a_late_answer(pair: Any) -> None:
    caller, server = pair
    server.start()
    assert wait_until(caller.client.service_is_ready, ANSWER_S)

    server.hold.clear()
    future = caller.client.call_async(caller.request(True))
    fired = threading.Event()
    future.add_done_callback(lambda _future: fired.set())
    assert server.entered.wait(ANSWER_S), "the request never reached the server"

    # The caller gave up while the server is still working on it.
    caller.client.remove_pending_request(future)
    server.hold.set()
    assert server.answered.wait(ANSWER_S)
    # Give the response every chance to arrive and be delivered.
    assert not fired.wait(1.0), "an abandoned request was completed by its late answer"
    assert future.done() is False
    assert not caller.spinner.errors

    # The client is still good, and the late answer does not stand in for the next one.
    server.answered.clear()
    again = caller.client.call_async(caller.request(False))
    done = threading.Event()
    again.add_done_callback(lambda _future: done.set())
    assert done.wait(ANSWER_S)
    assert again.result().message == "F"
    assert not caller.spinner.errors


def test_a_fixed_float64_pair_takes_a_list_of_floats(reachy_srv: Any) -> None:
    """``SubmitMove.antennas`` is ``float64[2]``, and the client assigns a list to it."""

    request = reachy_srv.SubmitMove.Request()
    request.antennas = [0.25, -0.5]
    assert list(request.antennas) == [0.25, -0.5]
    # The values survive a second assignment, so nothing aliases a shared array.
    other = reachy_srv.SubmitMove.Request()
    other.antennas = [1.0, 2.0]
    assert list(request.antennas) == [0.25, -0.5]

    # The size is part of the type, not a hint, but only ROS_PYTHON_CHECK_FIELDS says so: on a
    # robot, where it is off, the client has to be right on its own.
    with pytest.raises((AssertionError, ValueError)):
        request.antennas = [0.0, 0.0, 0.0]
    with pytest.raises((AssertionError, ValueError)):
        request.antennas = [0.0]
    assert list(request.antennas) == [0.25, -0.5]
    unchecked = reachy_srv.SubmitMove.Request(check_fields=False)
    unchecked.antennas = [0.0]
    assert list(unchecked.antennas) == [0.0]


def test_the_clients_own_antenna_conversion_fits_the_field(reachy_srv: Any) -> None:
    ros_support.hub_package_on_path()
    client_module = ros_support.need_module("emori_adapter.robot.client")
    wire = client_module._antennas_wire((0.3, -0.1))
    request = reachy_srv.SubmitMove.Request()
    request.antennas = wire
    # Adapter order is (left, right); the wire is [right, left].
    assert list(request.antennas) == pytest.approx([-0.1, 0.3])
