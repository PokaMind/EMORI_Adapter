"""What the hub assumes of rclpy executors, against real ones.

The hub's node groups run on ``MultiThreadedExecutor(num_threads=3)``, spun one slice at a
time by ``SupervisedSpin``, and are torn down with ``executor.shutdown()`` after the spin has
stopped. The lifecycle tests drive that code with fake executors; these drive it with
rclpy's.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import ros_support
from ros_support import Spinner, wait_until

HELD_S = 10.0


def _node(rclpy: Any, context: Any, label: str) -> Any:
    return rclpy.create_node(f"{label}_{uuid.uuid4().hex[:8]}", context=context)


def _concurrency(rclpy: Any, context: Any, *, threads: int, parties: int) -> tuple[list, list]:
    """Run ``parties`` one-shot timer callbacks that wait for each other.

    Returns who met and who timed out. The callbacks can only meet if the executor runs
    that many of them at once.
    """

    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor

    node = _node(rclpy, context, "meeting")
    executor = MultiThreadedExecutor(num_threads=threads, context=context)
    executor.add_node(node)
    barrier = threading.Barrier(parties)
    met: list[int] = []
    broken: list[int] = []
    timers: list[Any] = []
    started: set[int] = set()
    guard = threading.Lock()
    group = ReentrantCallbackGroup()

    def make(index: int) -> Any:
        def callback() -> None:
            timers[index].cancel()
            with guard:
                # A timer can be queued twice before its cancel lands; it meets once.
                if index in started:
                    return
                started.add(index)
            try:
                barrier.wait(1.5)
                met.append(index)
            except threading.BrokenBarrierError:
                broken.append(index)
        return callback

    for index in range(parties):
        timers.append(node.create_timer(0.05, make(index), callback_group=group))
    spinner = Spinner(executor, "meeting").start()
    try:
        wait_until(lambda: len(met) + len(broken) >= parties, 8.0)
    finally:
        spinner.stop()
        executor.shutdown()
        node.destroy_node()
    return met, broken


def test_the_thread_count_decides_how_many_callbacks_run_together(
        rclpy: Any, ros_context: Any) -> None:
    met, broken = _concurrency(rclpy, ros_context, threads=3, parties=3)
    assert sorted(met) == [0, 1, 2] and not broken

    # The same callbacks on one thread cannot meet, so the first result is the argument.
    met, broken = _concurrency(rclpy, ros_context, threads=1, parties=2)
    assert not met and broken


def test_supervised_spin_drives_a_multithreaded_executor(rclpy: Any, ros_context: Any) -> None:
    from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor

    ros_support.hub_package_on_path()
    spin_module = ros_support.need_module("emori_hub.spin")

    node = _node(rclpy, ros_context, "ticking")
    ticks: list[int] = []
    node.create_timer(0.02, lambda: ticks.append(1))
    executor = MultiThreadedExecutor(num_threads=3, context=ros_context)
    executor.add_node(node)
    spin = spin_module.SupervisedSpin(
        "ros-test", executor, ok=lambda: rclpy.ok(context=ros_context),
        fatal=(ExternalShutdownException,))
    spin.start()
    try:
        assert wait_until(lambda: len(ticks) >= 3, 8.0), "the timer never fired"
        assert spin.death() == ""
        assert spin.stop(3.0) is True
        assert spin.death() == ""
    finally:
        spin.stop(3.0)
        executor.shutdown()
        node.destroy_node()


def test_a_supervised_spin_reports_a_context_that_went_away(
        rclpy: Any, ros_context: Any) -> None:
    from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor

    ros_support.hub_package_on_path()
    spin_module = ros_support.need_module("emori_hub.spin")

    node = _node(rclpy, ros_context, "idle")
    executor = MultiThreadedExecutor(num_threads=2, context=ros_context)
    executor.add_node(node)
    spin = spin_module.SupervisedSpin(
        "ros-test", executor, ok=lambda: rclpy.ok(context=ros_context),
        fatal=(ExternalShutdownException,))
    spin.start()
    try:
        assert wait_until(lambda: spin.thread is not None and spin.thread.is_alive(), 3.0)
        rclpy.try_shutdown(context=ros_context)
        assert wait_until(spin.death, 5.0) == "the ROS context went away"
    finally:
        spin.stop(3.0)
        executor.shutdown()


def _held_callback(rclpy: Any, context: Any) -> tuple[Any, Any, Spinner, threading.Event,
                                                       threading.Event]:
    """An executor whose only callback is running and will not finish until released."""

    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor

    node = _node(rclpy, context, "held")
    executor = MultiThreadedExecutor(num_threads=2, context=context)
    executor.add_node(node)
    entered, release = threading.Event(), threading.Event()
    timer = None
    guard = threading.Lock()

    def callback() -> None:
        timer.cancel()
        with guard:
            if entered.is_set():
                return
            entered.set()
        release.wait(HELD_S)

    timer = node.create_timer(0.01, callback, callback_group=ReentrantCallbackGroup())
    spinner = Spinner(executor, "held").start()
    assert entered.wait(8.0), "the callback never started"
    # As the hub does: the spin stops first, and the callback is still running.
    assert spinner.stop()
    return node, executor, spinner, entered, release


def test_shutdown_waits_for_a_running_callback(rclpy: Any, ros_context: Any) -> None:
    node, executor, _spinner, _entered, release = _held_callback(rclpy, ros_context)
    result: list[Any] = []
    caller = threading.Thread(target=lambda: result.append(executor.shutdown()), daemon=True)
    caller.start()
    try:
        caller.join(0.5)
        assert caller.is_alive(), "shutdown returned while a callback was still running"
        release.set()
        caller.join(5.0)
        assert not caller.is_alive(), "shutdown did not return once the callback had"
        assert result and result[0] is not False
    finally:
        release.set()
        caller.join(2.0)
        node.destroy_node()


def test_a_timeout_does_not_bound_shutdown_so_the_hub_cannot_rely_on_one(
        rclpy: Any, ros_context: Any) -> None:
    """Jazzy ignores ``timeout_sec`` here and waits for the threads anyway.

    So what bounds a stop in the hub is not this call: each step of the teardown runs on a thread
    of its own with a limit, and the process has a deadline (``main._Started``, ``exit_by``).
    """

    node, executor, _spinner, _entered, release = _held_callback(rclpy, ros_context)
    result: list[Any] = []
    caller = threading.Thread(target=lambda: result.append(executor.shutdown(timeout_sec=0.1)),
                              daemon=True)
    caller.start()
    try:
        caller.join(1.0)
        assert caller.is_alive(), "shutdown now honours its timeout: the hub's budgets can use it"
    finally:
        release.set()
        caller.join(5.0)
        node.destroy_node()


def test_shutdown_can_be_told_not_to_wait_for_the_threads(rclpy: Any, ros_context: Any) -> None:
    node, executor, _spinner, _entered, release = _held_callback(rclpy, ros_context)
    try:
        started = time.monotonic()
        executor.shutdown(wait_for_threads=False)
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        node.destroy_node()
